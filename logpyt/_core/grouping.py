"""The grouping state of one stream run or one `GroupBy.groups` call.

Design 2.8, D-10 and D-11. Entries go in, closed `LogGroup` objects come out.
Gap decisions read only the entries' timestamps, as whole microseconds;
`Grouper.flush_idle` reads only the host-monotonic time its caller passes. The
stream core drives that idle flush from its timer (PR-10); the 2.x streams
flush only at the end of the input.

Timestamps sit on one timeline per run. It counts naive times by wall clock
until the first aware timestamp, then switches once to a UTC epoch, converting
the open groups; after that a naive time is read as host-local. So aware
entries are compared exactly, and the host zone's DST changes move only the
naive ones (unparsed lines: a buffer divider, a malformed line).

A group stays open while entries keep arriving within ``gap_ms`` of its newest
member, in either direction; the boundary is inclusive. Its expiry is
``newest + gap``, which is ``max(old, ts + gap)``, so an older out-of-order
entry never moves it back. In windowed mode the open groups wait in a heap of
``(expiry_us, seq, key)`` entries: ``seq`` is unique, so two equal expiries
never compare their keys, which may not be orderable (F7-1: ``None`` vs a str).
A group's heap entry is replaced only when it reaches the top of the heap
after the group was extended, so the groups one entry expires close in the
order of their expiry. Entries of closed groups are dropped when they
outnumber the open groups, so the heap stays within twice the open groups plus
a constant.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from operator import attrgetter
from typing import TYPE_CHECKING

from logpyt.models import LogGroup

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable

    from logpyt.groupers import GroupBy
    from logpyt.models import LogEntry
    from logpyt.models.group import GroupReason

__all__ = ["Grouper", "entry_bytes", "gap_micros"]

type _Key = tuple[Hashable, ...]

_MICROSECOND = timedelta(microseconds=1)
_AWARE_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NAIVE_EPOCH = _AWARE_EPOCH.replace(tzinfo=None)
# Heap entries of closed groups are dropped once the heap holds more than
# twice the open groups plus this many.
_HEAP_SLACK = 64


def entry_bytes(entry: LogEntry) -> int:
    """The size of ``entry`` against ``GroupBy.max_total_bytes``.

    The UTF-8 length of ``raw`` plus one byte for its line terminator: what the
    line took on the wire or in the file. It is deterministic and the same on
    every platform; the Python objects of an entry take more memory than that.
    """
    raw = entry.raw
    size = len(raw) if raw.isascii() else len(raw.encode("utf-8", "surrogatepass"))
    return size + 1


def gap_micros(gap_ms: float) -> int:
    """``gap_ms`` in whole microseconds, rounded down.

    Timestamps have microsecond resolution, so a distance of whole microseconds
    is at most ``gap_ms`` exactly when it is at most this value. The decimal
    digits of a float are used (``0.57`` is 570, not its binary value
    0.56999...), so no float rounding moves the boundary (F7-3).
    """
    exact = Decimal(gap_ms) if isinstance(gap_ms, int) else Decimal(repr(float(gap_ms)))
    return math.floor(exact * 1000)


def _aware_micros(ts: datetime) -> int:
    """``ts`` in microseconds since the UTC epoch; a naive ``ts`` is host-local.

    Zoned and epoch lines carry aware timestamps, while unparsed entries (a
    buffer divider, a malformed line) carry naive host-local ones, so one
    stream can mix both. A naive time the host-local zone cannot represent is
    read as UTC instead.
    """
    if ts.tzinfo is None:
        try:
            ts = ts.astimezone()
        except (OverflowError, ValueError, OSError):
            ts = ts.replace(tzinfo=UTC)
    return (ts - _AWARE_EPOCH) // _MICROSECOND


def _no_key(entry: LogEntry) -> _Key:
    del entry
    return ()


def _key_getter(keys: tuple[str, ...]) -> Callable[[LogEntry], _Key]:
    """A function returning the values of the ``keys`` fields as a tuple."""
    if not keys:
        return _no_key
    if len(keys) == 1:
        field = attrgetter(keys[0])

        def one(entry: LogEntry) -> _Key:
            return (field(entry),)

        return one
    return attrgetter(*keys)


@dataclass(slots=True, eq=False)
class _Open:
    """An open group."""

    key: _Key
    entries: list[LogEntry]
    newest: int  # the newest member's timestamp, in microseconds
    size: int  # the members' entry_bytes
    touched: float  # host-monotonic seconds of the last join
    seq: int = -1  # the group's live heap entry (windowed)


class Grouper:
    """The open groups of one run, for one `GroupBy`.

    Not thread-safe: one caller at a time, as the stream core runs it.
    """

    def __init__(self, spec: GroupBy) -> None:
        """Start with no open group.

        Args:
            spec: The validated grouping spec.

        """
        self._spec = spec
        self._windowed = spec.mode == "windowed"
        self._gap = gap_micros(spec.gap_ms)
        idle_ms = spec.idle_flush_ms
        if isinstance(idle_ms, str):  # "gap"
            idle_ms = spec.gap_ms
        self._idle = None if idle_ms is None else idle_ms / 1000
        self._key_of = _key_getter(spec.keys)
        # Least recently extended first: the eviction and the idle order.
        self._open: OrderedDict[_Key, _Open] = OrderedDict()
        self._heap: list[tuple[int, int, _Key]] = []
        self._seq = itertools.count()
        self._entries = 0
        self._bytes = 0
        self._now = -math.inf
        # The timeline: None before the first entry, False while it counts
        # naive wall-clock time, True once it counts from the UTC epoch.
        self._aware: bool | None = None

    @property
    def open_groups(self) -> int:
        """The number of open groups."""
        return len(self._open)

    @property
    def open_entries(self) -> int:
        """The entries in open groups."""
        return self._entries

    @property
    def open_bytes(self) -> int:
        """The `entry_bytes` of the entries in open groups."""
        return self._bytes

    def add(self, entry: LogEntry, now: float) -> list[LogGroup]:
        """Add ``entry`` and return the groups that closed, in closing order.

        Args:
            entry: The next entry.
            now: Host-monotonic seconds; only `flush_idle` reads it. A value
                below an earlier one counts as the earlier one. A caller that
                never calls `flush_idle` may pass any constant.

        Returns:
            The groups closed by this entry: windowed groups it expired, the
            group a key change or gap ended, groups evicted to make room, and
            the entry's own group when it reached a cap. An entry larger than
            ``max_total_bytes`` closes its key's open group and then itself,
            alone; the groups of other keys stay open.

        """
        now = self._tick(now)
        ts = self._micros(entry.timestamp)
        key = self._key_of(entry)
        size = entry_bytes(entry)
        spec = self._spec
        groups = self._open
        closed: list[LogGroup] = []
        if self._windowed:
            self._expire(ts, closed)
        elif groups and key not in groups:
            self._close(next(iter(groups.values())), "key", closed)
        group = groups.get(key)
        if group is not None and abs(ts - group.newest) > self._gap:
            self._close(group, "gap", closed)
            group = None
        if size > spec.max_total_bytes:
            # No group can hold it, so it closes alone at once, after the open
            # group of its key so that the key's entries keep their order. The
            # other groups stay open: they fit the budget without it.
            if group is not None:
                self._close(group, "evicted", closed)
            reason: GroupReason = "size" if spec.max_group_size == 1 else "evicted"
            closed.append(LogGroup(key=key, entries=(entry,), reason=reason))
            return closed
        while groups and (
            self._entries >= spec.max_total_entries
            or self._bytes + size > spec.max_total_bytes
            or (key not in groups and len(groups) >= spec.max_groups)
        ):
            self._close(next(iter(groups.values())), "evicted", closed)
        group = groups.get(key)
        if group is None:
            group = _Open(key, [entry], ts, size, now)
            groups[key] = group
            if self._windowed:
                self._push(group)
        else:
            group.entries.append(entry)
            group.newest = max(group.newest, ts)
            group.size += size
            group.touched = now
            groups.move_to_end(key)
        self._entries += 1
        self._bytes += size
        if len(group.entries) >= spec.max_group_size:
            self._close(group, "size", closed)
        return closed

    def flush_idle(self, now: float) -> list[LogGroup]:
        """Close every group no entry joined for ``idle_flush_ms``.

        Args:
            now: Host-monotonic seconds. A group closes once ``now`` reaches
                the time of its last join plus ``idle_flush_ms`` (inclusive,
                so a timer armed at `idle_deadline` always closes it).

        Returns:
            The idle groups, least recently extended first, with reason
            ``"idle"``; none when ``idle_flush_ms`` is None.

        """
        now = self._tick(now)
        closed: list[LogGroup] = []
        while (deadline := self.idle_deadline()) is not None and deadline <= now:
            self._close(next(iter(self._open.values())), "idle", closed)
        return closed

    def idle_deadline(self) -> float | None:
        """The host-monotonic time at which `flush_idle` next closes a group.

        None when no group is open or ``idle_flush_ms`` is None.
        """
        if self._idle is None or not self._open:
            return None
        return next(iter(self._open.values())).touched + self._idle

    def flush(self) -> list[LogGroup]:
        """Close every open group with reason ``"end"``.

        Returns:
            The groups, least recently extended first.

        """
        closed: list[LogGroup] = []
        while self._open:
            self._close(next(iter(self._open.values())), "end", closed)
        self._heap.clear()
        return closed

    def _tick(self, now: float) -> float:
        self._now = max(self._now, now)
        return self._now

    def _micros(self, ts: datetime) -> int:
        """``ts`` in microseconds on the run's timeline (module documentation).

        The first aware timestamp after naive ones switches the timeline from
        wall clock to the UTC epoch, so a zoned line next to a naive divider
        never raises and a DST change of the host zone never splits aware
        entries.
        """
        aware = self._aware
        if ts.tzinfo is not None:
            if aware is not True:
                if aware is False:
                    self._switch_to_aware()
                self._aware = True
            return (ts - _AWARE_EPOCH) // _MICROSECOND
        if aware:
            return _aware_micros(ts)
        self._aware = False
        return (ts - _NAIVE_EPOCH) // _MICROSECOND

    def _switch_to_aware(self) -> None:
        """Move the open groups from the wall-clock timeline to the UTC one."""
        for group in self._open.values():
            wall = _NAIVE_EPOCH + group.newest * _MICROSECOND
            group.newest = _aware_micros(wall)
        if self._windowed:
            self._rebuild_heap()

    def _expire(self, ts: int, closed: list[LogGroup]) -> None:
        """Close the windowed groups whose expiry is before ``ts``.

        They close in the order of their expiry: a group extended since its
        heap entry was pushed is pushed again with its current expiry first.
        """
        heap = self._heap
        while heap and heap[0][0] < ts:
            expiry, seq, key = heapq.heappop(heap)
            group = self._open.get(key)
            if group is None or group.seq != seq:
                continue  # the entry of a group that closed
            if group.newest + self._gap != expiry:
                self._push(group)
            else:
                self._close(group, "gap", closed)

    def _push(self, group: _Open) -> None:
        """Give ``group`` a heap entry for its current expiry."""
        group.seq = next(self._seq)
        heap = self._heap
        heapq.heappush(heap, (group.newest + self._gap, group.seq, group.key))
        if len(heap) > 2 * len(self._open) + _HEAP_SLACK:
            self._rebuild_heap()

    def _rebuild_heap(self) -> None:
        """One heap entry per open group, for its current expiry."""
        gap = self._gap
        heap = self._heap
        heap[:] = [(g.newest + gap, g.seq, g.key) for g in self._open.values()]
        heapq.heapify(heap)

    def _close(self, group: _Open, reason: GroupReason, closed: list[LogGroup]) -> None:
        del self._open[group.key]
        self._entries -= len(group.entries)
        self._bytes -= group.size
        closed.append(
            LogGroup(key=group.key, entries=tuple(group.entries), reason=reason)
        )
