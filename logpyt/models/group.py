"""The group record: entries that `logpyt.GroupBy` put together (design 2.6)."""

from collections.abc import Hashable, Iterator, Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal, get_args, overload

from .entry import LogEntry

__all__ = ["GroupReason", "LogGroup"]

type GroupReason = Literal["gap", "key", "size", "evicted", "idle", "end"]

_REASONS: frozenset[str] = frozenset(get_args(GroupReason.__value__))


@dataclass(frozen=True, slots=True)
class LogGroup(Sequence[LogEntry]):
    """Entries grouped by `logpyt.GroupBy`, with the reason the group closed.

    A group is a read-only sequence of its entries, in the order they arrived:
    ``len(group)``, ``group[0]``, ``for entry in group`` and ``entry in group``
    work as on a tuple, and a slice is a tuple of entries. It is not hashable,
    because its entries are not.

    ``reason`` says why the group closed. In ``"consecutive"`` mode one group
    is open at a time, the group of the last entry; in ``"windowed"`` mode one
    group is open per key.

    - ``"gap"``: consecutive: the next entry has the same key but is more
      than ``gap_ms`` after (or before) the group's newest member. Windowed:
      an entry of any key is more than ``gap_ms`` after the group's newest
      member, or an entry of the group's key is more than ``gap_ms`` before
      it.
    - ``"key"``: consecutive only: the next entry has another key.
    - ``"size"``: the group reached ``max_group_size`` entries (both modes);
      it closes at once, so ``max_group_size=1`` gives groups of one entry.
    - ``"evicted"``: closed early, least recently extended group first, so
      that the open groups stay within ``max_total_entries`` and
      ``max_total_bytes`` (both modes) and ``max_groups`` (windowed). An
      entry larger than ``max_total_bytes`` by itself closes at once as a
      group of its own, after the open group of its key (both ``"evicted"``,
      or ``"size"`` for the entry when ``max_group_size`` is 1); the groups of
      other keys stay open.
    - ``"idle"``: no entry joined the group for ``idle_flush_ms`` of
      host-monotonic time (a live stream only; never in ``GroupBy.groups``).
    - ``"end"``: the input ended (end of file, the stream stopped, the end of
      the entries given to ``GroupBy.groups``) and the group was still open.

    Attributes:
        key: The values of the ``GroupBy.keys`` fields, in that order, shared
            by every entry of the group; ``()`` when ``keys`` is empty. The
            tuple is hashable.
        entries: The `LogEntry` objects, at least one, in arrival order.
        reason: Why the group closed, one of the reasons above.

    """

    key: tuple[Hashable, ...]
    entries: tuple[LogEntry, ...]
    reason: GroupReason

    # The generated hash would hash the entries, which raises.
    __hash__: ClassVar[None] = None

    def __post_init__(self) -> None:
        """Check the field types, then the values (design 4.2).

        Raises:
            TypeError: ``key`` or ``entries`` is not a tuple, a member of
                ``entries`` is not a `LogEntry`, ``key`` holds an unhashable
                value, or ``reason`` is not a str.
            ValueError: ``entries`` is empty or ``reason`` is unknown.

        """
        for name in ("key", "entries"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                msg = f"LogGroup.{name} must be a tuple, not {type(value).__name__}"
                raise TypeError(msg)
        for entry in self.entries:
            if not isinstance(entry, LogEntry):
                msg = f"LogGroup.entries must hold LogEntry, not {type(entry).__name__}"
                raise TypeError(msg)
        try:
            hash(self.key)
        except TypeError as exc:
            msg = f"LogGroup.key must hold hashable values: {exc}"
            raise TypeError(msg) from None
        if not isinstance(self.reason, str):
            msg = f"LogGroup.reason must be a str, not {type(self.reason).__name__}"
            raise TypeError(msg)
        if not self.entries:
            msg = "LogGroup.entries must hold at least one entry"
            raise ValueError(msg)
        if self.reason not in _REASONS:
            expected = ", ".join(sorted(_REASONS))
            msg = f"LogGroup.reason must be one of {expected}, not {self.reason!r}"
            raise ValueError(msg)

    def __len__(self) -> int:
        """The number of entries."""
        return len(self.entries)

    @overload
    def __getitem__(self, index: int) -> LogEntry: ...
    @overload
    def __getitem__(self, index: slice) -> tuple[LogEntry, ...]: ...
    def __getitem__(self, index: int | slice) -> LogEntry | tuple[LogEntry, ...]:
        """The entry at ``index``, or a tuple of the entries in a slice."""
        return self.entries[index]

    def __iter__(self) -> Iterator[LogEntry]:
        """The entries in arrival order."""
        return iter(self.entries)

    def __reversed__(self) -> Iterator[LogEntry]:
        """The entries in reverse arrival order."""
        return reversed(self.entries)

    def __contains__(self, value: object) -> bool:
        """Whether an entry equal to ``value`` is in the group."""
        return value in self.entries
