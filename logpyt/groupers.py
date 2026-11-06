"""Grouping: `GroupBy`, how a stream or a list of entries is grouped (design 2.8).

A `GroupBy` is a frozen spec; it holds no state. A stream given ``group_by=``
delivers `logpyt.LogGroup` objects instead of entries, and
`GroupBy.groups` groups any iterable of entries offline. Every run starts with
no open group.

Entries with equal values in the ``keys`` fields share a group while they
arrive no more than ``gap_ms`` apart: the gap between an entry and the newest
member of the group, by the entries' timestamps, in either direction, with the
boundary included. A group has no maximum duration; a steady trickle within
``gap_ms`` keeps it open up to ``max_group_size`` entries.

- ``mode="consecutive"``: one group is open, the group of the last entry; an
  entry with another key closes it (reason ``"key"``).
- ``mode="windowed"``: one group is open per key, so interleaved keys group
  apart; every entry also closes the groups whose newest member is more than
  ``gap_ms`` older than it.

`logpyt.LogGroup` documents every reason a group closes with. Memory is
bounded in both modes (design 5.10): the open groups hold at most
``max_total_entries`` entries and ``max_total_bytes`` bytes (the UTF-8 size of
each entry's ``raw`` line plus one byte), and the least recently extended group
is closed early (``"evicted"``) to stay within them. An entry larger than
``max_total_bytes`` by itself is delivered alone at once.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast, get_args

from logpyt._core.grouping import Grouper
from logpyt.models import LogEntry

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from logpyt.models import LogGroup

__all__ = ["GroupBy", "GroupKey"]

type GroupKey = Literal["pid", "tid", "uid", "tag", "level", "package"]

_KEY_NAMES: tuple[str, ...] = get_args(GroupKey.__value__)
_MODES = ("consecutive", "windowed")
_SIZES = ("max_group_size", "max_groups", "max_total_entries", "max_total_bytes")


@dataclass(frozen=True, kw_only=True, slots=True)
class GroupBy:
    """How entries are grouped: by which fields, and how far apart in time.

    Every argument is keyword-only and checked here, so a mistake fails where
    it is written (D-13): a value of the wrong type raises TypeError, a value
    out of range ValueError.

    Attributes:
        keys: The `LogEntry` fields whose values a group shares, in the order
            of `LogGroup.key`: any of ``"pid"``, ``"tid"``, ``"uid"``,
            ``"tag"``, ``"level"`` and ``"package"``, each at most once.
            The type is a tuple; a list is accepted at run time and stored as
            a tuple, but a type checker rejects it. A bare str
            (``keys="pid"``) raises TypeError. ``()`` groups by time alone.
        gap_ms: The largest gap, in milliseconds, between an entry and the
            newest member of its group; > 0. Timestamps have microsecond
            resolution and are compared as whole microseconds, so the boundary
            is exact: an entry exactly ``gap_ms`` after the newest member
            joins the group.
        mode: ``"consecutive"`` (one open group) or ``"windowed"`` (one open
            group per key); see the module documentation.
        idle_flush_ms: A live stream closes a group no entry joined for this
            many milliseconds of host-monotonic time (reason ``"idle"``), so a
            crash group is delivered without waiting for an unrelated line
            (D-10); > 0. ``"gap"`` (the default) uses ``gap_ms``, None turns
            it off. `groups` reads no clock and ignores it. ``LogStream``
            and ``AsyncLogStream`` apply it (the stream core's timer).
        max_group_size: A group closes when it reaches this many entries
            (reason ``"size"``); >= 1. The 3.0 streams also require it to be
            at most ``StreamConfig.buffer_entries``, since a group counts its
            members in their buffer (design 2.8).
        max_groups: Windowed: at most this many groups are open; a new key
            beyond it closes the least recently extended group (reason
            ``"evicted"``); >= 1. Consecutive mode has one open group.
        max_total_entries: The open groups hold at most this many entries;
            >= 1 (D-11).
        max_total_bytes: The open groups hold at most this many bytes,
            counted as the UTF-8 size of each entry's ``raw`` plus one byte
            for its line terminator; >= 1 (D-11). The Python objects take more
            memory than that. An entry larger than this by itself closes at
            once as a group of its own; `logpyt.LogGroup` gives the reasons.

    """

    keys: tuple[GroupKey, ...]
    gap_ms: float
    mode: Literal["consecutive", "windowed"] = "consecutive"
    idle_flush_ms: float | Literal["gap"] | None = "gap"
    max_group_size: int = 1000
    max_groups: int = 1000
    max_total_entries: int = 20_000
    max_total_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        """Check every field; store ``keys`` as a tuple.

        Raises:
            TypeError: A field has the wrong type, ``keys`` is a str.
            ValueError: A field is out of range, ``keys`` names an unknown or
                repeated field, ``mode`` is unknown.

        """
        object.__setattr__(self, "keys", _check_keys(self.keys))
        _check_positive("gap_ms", self.gap_ms)
        if not isinstance(self.mode, str):
            msg = f"GroupBy.mode must be a str, not {type(self.mode).__name__}"
            raise TypeError(msg)
        if self.mode not in _MODES:
            msg = f"GroupBy.mode must be 'consecutive' or 'windowed', not {self.mode!r}"
            raise ValueError(msg)
        idle = self.idle_flush_ms
        if isinstance(idle, str):
            if idle != "gap":
                msg = (
                    "GroupBy.idle_flush_ms must be a number, 'gap' or None, "
                    f"not {idle!r}"
                )
                raise ValueError(msg)
        elif idle is not None:
            _check_positive("idle_flush_ms", idle)
        for name in _SIZES:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                msg = f"GroupBy.{name} must be an int, not {type(value).__name__}"
                raise TypeError(msg)
            if value < 1:
                msg = f"GroupBy.{name} must be at least 1, not {value}"
                raise ValueError(msg)

    def groups(self, entries: Iterable[LogEntry]) -> Iterator[LogGroup]:
        """Group ``entries`` offline, lazily.

        Each group is yielded as soon as an entry closes it, and the groups
        still open at the end follow with reason ``"end"``. No host clock is
        read, so no group closes as ``"idle"``. Each call starts afresh.

        Args:
            entries: Entries in the order they were logged, such as the output
                of `logpyt.read_file`.

        Yields:
            The groups, in the order they closed.

        Raises:
            TypeError: An item of ``entries`` is not a `LogEntry`.

        """
        grouper = Grouper(self)
        for entry in entries:
            if not isinstance(entry, LogEntry):
                msg = f"GroupBy.groups takes LogEntry items, not {type(entry).__name__}"
                raise TypeError(msg)
            yield from grouper.add(entry, 0.0)
        yield from grouper.flush()


def _check_keys(value: object) -> tuple[GroupKey, ...]:
    """``value`` as a tuple of field names.

    Raises:
        TypeError: ``value`` is a str or not a sequence, or a name is not a str.
        ValueError: A name is unknown or repeated.

    """
    if isinstance(value, str | bytes | bytearray):
        msg = (
            "GroupBy.keys takes a tuple of field names, not one string: "
            f"write keys=({value!r},), not keys={value!r}"
        )
        raise TypeError(msg)
    if not isinstance(value, Sequence):
        msg = (
            f"GroupBy.keys takes a tuple of field names, not {type(value).__name__}: "
            "their order is the order of LogGroup.key"
        )
        raise TypeError(msg)
    keys = tuple(value)
    for name in keys:
        if not isinstance(name, str):
            msg = f"GroupBy.keys names must be str, not {type(name).__name__}"
            raise TypeError(msg)
        if name not in _KEY_NAMES:
            expected = ", ".join(_KEY_NAMES)
            msg = f"GroupBy.keys: {name!r} is not one of {expected}"
            raise ValueError(msg)
        if keys.count(name) > 1:
            msg = f"GroupBy.keys names {name!r} more than once"
            raise ValueError(msg)
    return cast("tuple[GroupKey, ...]", keys)


def _check_positive(name: str, value: object) -> None:
    """Check that ``value`` is a finite int or float above 0.

    An int must also fit in a float, since the grouping engine divides it.

    Raises:
        TypeError: ``value`` is not an int or float (a bool is not).
        ValueError: ``value`` is not above 0, is infinite or NaN, or is an int
            too large for a float.

    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"GroupBy.{name} must be a number, not {type(value).__name__}"
        raise TypeError(msg)
    if isinstance(value, int):
        try:
            float(value)
        except OverflowError:
            # The value itself is not shown: it may have more digits than
            # str() converts.
            msg = (
                f"GroupBy.{name} must be finite, not an int of "
                f"{value.bit_length()} bits"
            )
            raise ValueError(msg) from None
    elif not math.isfinite(value):
        msg = f"GroupBy.{name} must be finite, not {value}"
        raise ValueError(msg)
    if value <= 0:
        msg = f"GroupBy.{name} must be above 0, not {value}"
        raise ValueError(msg)
