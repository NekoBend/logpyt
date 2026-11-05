"""The mailbox between a stream's engine and the rest: outbox, admission, control inbox.

Design 5.2 and 5.10. Pure data structures with no locking: a driver wraps
them in its one lock (sync) or uses them from its event loop (async) and adds
the wake-ups.

- `Outbox`: the bounded queue from the engine to the consumer. It holds items
  (entries and groups) and the events between them (G3) in one order. It never
  refuses a record, so the end of a run always fits (X-2): the bound comes from
  `Admission`, which closes the engine's intake when the items reach the
  capacity, so the outbox holds at most the capacity plus the lines of one
  chunk, plus the members of the groups that close at once: an idle flush
  (also while intake is closed), the expiries and evictions that one entry
  causes, a connection end or a stop. Open groups are bounded by
  ``GroupBy.max_total_entries`` and ``max_total_bytes``, so they move from the
  grouper to the outbox without growing the total. Events live outside the
  data capacity;
  when more than ``max_pending_events`` wait, consecutive state changes merge
  into the latest one. A `Gap` and a terminal state change never merge. A
  merged change keeps the ``old`` of the first change it replaces and the
  ``new``, ``connection`` and ``error`` of the latest, so the chain of states
  stays unbroken, and ``old`` can equal ``new`` (RUNNING -> RECONNECTING ->
  CONNECTING -> RUNNING merges into RUNNING -> RUNNING with the latest
  connection number). The consumer settles each record it finished with
  (`Outbox.done`, like ``queue.Queue.task_done``); the cumulative ``handled``
  count lets the core end a STOPPED run only once every record it emitted
  was handled, never merely taken (design A.21).
- `Admission`: the engine's copy of the outbox fill, counted from what the core
  delivered and what the consumer side reported in `ConsumerProgress`, plus the
  lines the core holds for package attribution (design 5.10: held lines count
  against the outbox). Intake closes when the outbox is full
  (``buffer_entries`` entries or ``buffer_bytes`` bytes) and reopens at the
  low-water mark, half of both (G6).
- `ControlInbox`: the requests and results that reach the engine outside the
  data pipes. Each kind of request keeps one record at most, so a flood of
  ``stop()`` calls or progress reports cannot grow it (design 6.5: no unbounded
  control records).

Sizes count entries (a group counts its members) and the bytes of
`logpyt._core.grouping.entry_bytes`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING

from logpyt._core.grouping import entry_bytes
from logpyt._core.protocol import (
    CallbackFailed,
    CommandDone,
    ConsumerDetached,
    ConsumerProgress,
    Exited,
    KillRequest,
    OutboxCounts,
    Spawned,
    SpawnFailed,
    Start,
    StopRequest,
)
from logpyt.models import LogEntry, LogGroup
from logpyt.streams._events import StateChange, StreamState

if TYPE_CHECKING:
    from collections.abc import Iterable

    from logpyt._core.protocol import Event, Item, Record
    from logpyt.streams._events import Gap

__all__ = [
    "MAX_PENDING_EVENTS",
    "Admission",
    "ControlInbox",
    "Outbox",
    "item_size",
]

# Design 5.10: events waiting in the outbox beyond this many coalesce.
MAX_PENDING_EVENTS = 256

_TERMINAL = frozenset({StreamState.STOPPED, StreamState.KILLED, StreamState.FAILED})


def item_size(item: Item) -> tuple[int, int]:
    """``(entries, bytes)`` of an item: a group counts each member."""
    if isinstance(item, LogGroup):
        return len(item.entries), sum(entry_bytes(entry) for entry in item.entries)
    return 1, entry_bytes(item)


class Admission:
    """Whether the engine may read more data into the outbox (G6).

    The engine adds what it delivers (`admit`); the consumer side reports what
    left the outbox (`release`, cumulative). The queued amount is the
    difference, so no count is shared between threads. Lines that the core
    holds for package attribution count against the capacity too (`hold`,
    design 5.10): they are on their way to the outbox.

    Args:
        buffer_entries: Capacity in entries, >= 1.
        buffer_bytes: Capacity in bytes, >= 1.

    """

    __slots__ = (
        "_admitted_bytes",
        "_admitted_entries",
        "_buffer_bytes",
        "_buffer_entries",
        "_held_bytes",
        "_held_entries",
        "_open",
        "_removed_bytes",
        "_removed_entries",
    )

    def __init__(self, buffer_entries: int, buffer_bytes: int) -> None:
        self._buffer_entries = buffer_entries
        self._buffer_bytes = buffer_bytes
        self._admitted_entries = 0
        self._admitted_bytes = 0
        self._removed_entries = 0
        self._removed_bytes = 0
        self._held_entries = 0
        self._held_bytes = 0
        self._open = True

    @property
    def open(self) -> bool:
        """False from the moment the outbox is full until the low-water mark."""
        return self._open

    @property
    def admitted_entries(self) -> int:
        """Entries delivered so far."""
        return self._admitted_entries

    @property
    def queued_entries(self) -> int:
        """Entries delivered and not yet reported as taken or discarded."""
        return self._admitted_entries - self._removed_entries

    @property
    def queued_bytes(self) -> int:
        """Bytes delivered and not yet reported as taken or discarded."""
        return self._admitted_bytes - self._removed_bytes

    @property
    def held_entries(self) -> int:
        """Lines held for package attribution, as last reported by `hold`."""
        return self._held_entries

    def full(self) -> bool:
        """Whether the queued items and held lines reach the capacity, in entries
        or bytes.
        """
        return (
            self.queued_entries + self._held_entries >= self._buffer_entries
            or self.queued_bytes + self._held_bytes >= self._buffer_bytes
        )

    def low(self) -> bool:
        """Whether the queued items and held lines are at or below half of both
        capacities.
        """
        return (
            2 * (self.queued_entries + self._held_entries) <= self._buffer_entries
            and 2 * (self.queued_bytes + self._held_bytes) <= self._buffer_bytes
        )

    def held_back(self) -> bool:
        """Whether held lines are what keeps the outbox from its low-water mark:
        some are held, and the queued items alone are at or below half of both
        capacities.
        """
        return (
            self._held_entries > 0
            and 2 * self.queued_entries <= self._buffer_entries
            and 2 * self.queued_bytes <= self._buffer_bytes
        )

    def admit(self, entries: int, nbytes: int) -> None:
        """Count delivered items; close when the outbox is full."""
        self._admitted_entries += entries
        self._admitted_bytes += nbytes
        if self._open and self.full():
            self._open = False

    def release(self, counts: OutboxCounts) -> None:
        """Take the consumer side's latest counts; reopen at the low-water mark.

        Counts only grow, so an older report than one already seen changes
        nothing.
        """
        self._removed_entries = max(self._removed_entries, counts.removed_entries)
        self._removed_bytes = max(self._removed_bytes, counts.removed_bytes)
        if not self._open and self.low():
            self._open = True

    def hold(self, entries: int, nbytes: int) -> None:
        """Set the lines held for attribution; close when full, reopen when low.

        The core reports the totals after every change of its hold and before
        it delivers a line that left the hold, so a line never counts twice.
        """
        self._held_entries = entries
        self._held_bytes = nbytes
        if self._open:
            if self.full():
                self._open = False
        elif self.low():
            self._open = True


class Outbox:
    """The queue from the engine to the consumer (see the module docs).

    Engine side: `put` (the `Deliver` command), `discard`
    (`DiscardUndelivered`), `close` (`Finished`). Consumer side: `pop`,
    `done`, `detach`. `counts` changes on both sides (a put into a detached outbox
    and a coalesced event count too), and the driver reports it to the core
    after each change (`ConsumerProgress`, `ConsumerDetached`).

    Args:
        max_pending_events: Events that may wait before consecutive state
            changes coalesce (design 5.10: 256).

    """

    __slots__ = (
        "_bytes",
        "_closed",
        "_counts",
        "_detached",
        "_entries",
        "_events",
        "_in_hand",
        "_max_events",
        "_queue",
    )

    def __init__(self, *, max_pending_events: int = MAX_PENDING_EVENTS) -> None:
        self._queue: deque[Record] = deque()
        self._max_events = max_pending_events
        self._entries = 0
        self._bytes = 0
        self._events = 0
        self._counts = OutboxCounts()
        self._detached = False
        self._closed = False
        self._in_hand = False

    @property
    def entries(self) -> int:
        """Entries waiting; a group counts its members."""
        return self._entries

    @property
    def nbytes(self) -> int:
        """Bytes of the entries waiting."""
        return self._bytes

    @property
    def events(self) -> int:
        """Events waiting (state changes and gaps)."""
        return self._events

    @property
    def detached(self) -> bool:
        """Whether `detach` was called: everything put is discarded."""
        return self._detached

    @property
    def closed(self) -> bool:
        """Whether the run ended (`close`); nothing more will be put."""
        return self._closed

    @property
    def ended(self) -> bool:
        """Whether the consumer has seen everything: closed and empty."""
        return self._closed and not self._queue

    def __len__(self) -> int:
        """Records waiting, items and events."""
        return len(self._queue)

    def counts(self) -> OutboxCounts:
        """The cumulative counters, for a `ConsumerProgress` report."""
        return self._counts

    def put(self, records: Iterable[Record]) -> None:
        """Append ``records`` in order; never refuses one (see the module docs).

        Raises:
            RuntimeError: The outbox is closed.

        """
        if self._closed:
            msg = "Outbox.put() after close(): the run has ended"
            raise RuntimeError(msg)
        for record in records:
            if isinstance(record, LogEntry | LogGroup):
                self._put_item(record)
            elif not self._detached:
                self._put_event(record)

    def discard(self) -> None:
        """Discard the items waiting and count them; keep the events.

        The consumer still sees the state changes, the terminal one included
        (after ``kill()``; a consumer whose callback raised takes nothing more,
        design A.21).
        """
        kept: deque[Record] = deque()
        entries = nbytes = 0
        for record in self._queue:
            if isinstance(record, LogEntry | LogGroup):
                size = item_size(record)
                entries += size[0]
                nbytes += size[1]
            else:
                kept.append(record)
        self._queue = kept
        self._count_discarded(entries, nbytes)
        self._entries = 0
        self._bytes = 0

    def detach(self) -> None:
        """No consumer is left: discard everything waiting and all later items.

        Discarded items are counted; events are dropped.
        """
        self._detached = True
        self.discard()
        self._queue.clear()
        self._events = 0

    def close(self) -> None:
        """Mark the end of the run: `ended` once the consumer took the rest."""
        self._closed = True

    @property
    def in_hand(self) -> bool:
        """Whether the last record popped is not settled yet (`done`)."""
        return self._in_hand

    def done(self) -> None:
        """The consumer finished with the record it popped last (design A.21).

        The next step of the delivery iterator calls it: the record's
        callback returned (and an awaitable it returned completed), none
        applies, or a ``BaseException`` that is not a callback failure
        (``KeyboardInterrupt``, a real cancellation) interrupted it and the
        consumer asked for the next record. Never for a record whose callback
        raised, nor for an item skipped under the kill flag (F2-7): that one
        stays unsettled, so the core cannot end the run STOPPED before its
        `CallbackFailed` or `KillRequest`.

        Raises:
            RuntimeError: No record is in hand.

        """
        if not self._in_hand:
            msg = "Outbox.done() without a record in hand"
            raise RuntimeError(msg)
        self._in_hand = False
        self._counts = replace(self._counts, handled=self._counts.handled + 1)

    def pop(self) -> Record | None:
        """Take the next record, or None when none waits.

        A record still in hand that was not settled stays unsettled for good;
        a conforming consumer settles first, pops on past an item it skipped
        under the kill flag (F2-7) on purpose, and after its own failure pops
        nothing more (design A.21).
        """
        if not self._queue:
            return None
        record = self._queue.popleft()
        self._in_hand = True
        if isinstance(record, LogEntry | LogGroup):
            entries, nbytes = item_size(record)
            self._entries -= entries
            self._bytes -= nbytes
            counts = self._counts
            self._counts = replace(
                counts,
                taken_entries=counts.taken_entries + entries,
                taken_bytes=counts.taken_bytes + nbytes,
            )
        else:
            self._events -= 1
        return record

    def _put_item(self, item: Item) -> None:
        entries, nbytes = item_size(item)
        if self._detached:
            self._count_discarded(entries, nbytes)
            return
        self._queue.append(item)
        self._entries += entries
        self._bytes += nbytes

    def _put_event(self, event: StateChange | Gap) -> None:
        if (
            isinstance(event, StateChange)
            and self._events >= self._max_events
            and event.new not in _TERMINAL
            and self._queue
        ):
            tail = self._queue[-1]
            if isinstance(tail, StateChange) and tail.new not in _TERMINAL:
                # Coalesce into the latest change; keep the chain of states
                # unbroken by starting where the replaced change started.
                self._queue[-1] = replace(event, old=tail.old)
                self._counts = replace(
                    self._counts, events_coalesced=self._counts.events_coalesced + 1
                )
                return
        self._queue.append(event)
        self._events += 1

    def _count_discarded(self, entries: int, nbytes: int) -> None:
        if not entries:
            return
        counts = self._counts
        self._counts = replace(
            counts,
            discarded_entries=counts.discarded_entries + entries,
            discarded_bytes=counts.discarded_bytes + nbytes,
        )


class ControlInbox:
    """The events that reach the engine outside the data pipes (design 5.3, F2-2).

    `post` accepts every event except `Data`, `Eof` and `Tick` (the data inbox
    and the clock are the driver's). Each request kind keeps one record:

    - `Start` once; the first `StopRequest` (later ones change nothing in
      the core); `KillRequest` as a flag; the first `CallbackFailed` (the
      first error is the one latched, G4); the latest `ConsumerDetached` and
      one `ConsumerProgress` with the newest counts of every report posted
      (`OutboxCounts.newest`: the consumer side and the engine side both
      report, so the last one posted need not be the newest). The counts of
      a `ConsumerDetached` go into that `ConsumerProgress` too.
    - `Spawned`, `SpawnFailed`, `Exited` and `CommandDone` in arrival order;
      a connection has one spawn result and one exit, and few one-shot
      commands run at a time (see `RunCommand`), so this queue stays short.

    `take` returns them in this order: `ConsumerProgress`, `Start`,
    `CallbackFailed`, `KillRequest`, `StopRequest`, `ConsumerDetached`, then
    the results. Progress comes first because its counts are cumulative: the
    core then decides on a request with the freshest outbox fill (whether a
    kill or a failure actually discards anything), and what a waiting detach
    discarded is counted, with its WARNING, before a stop that waits with it
    ends the run. Requests that race each other end in the same state in
    either order (G4) while a child runs. Where the run ends at once
    (RECONNECTING, CONNECTING without a child, STOPPING(drain)) the first
    one handled decides and a later one changes nothing (G2): a kill before
    a `CallbackFailed` ends KILLED, and the failure is late
    (`StreamCore.late_failure`); a stop that finds the consumer done ends
    STOPPED before a kill. Waiting together here, the failure comes before
    the kill and the kill before the stop. In STOPPING(drain) a report can
    itself end the run STOPPED (design A.21), so a waiting kill, handed over
    after it even when posted first, changes nothing; that STOPPED is
    truthful because a consumer settles only the records it finished with,
    never an item it skipped under the kill flag (`Outbox.done`). The stop
    comes before the detach, so a raced ``stop(flush=...)`` still sets
    ``flush_failed`` as it would alone. A
    result that waits together with a request is taken after it, even if it
    arrived first. Both orders are valid, because the two are concurrent
    (the poster of the request cannot have seen the result: state is
    published only after `StreamCore.handle`), but they can end differently,
    and the table defines both outcomes. For example, a ``stop()`` that races
    a spawn failure in CONNECTING ends STOPPED (the STOPPING spawn_failed
    row) instead of FAILED.
    """

    __slots__ = (
        "_detached",
        "_failed",
        "_kill",
        "_progress",
        "_results",
        "_start",
        "_stop",
    )

    def __init__(self) -> None:
        self._start: Start | None = None
        self._failed: CallbackFailed | None = None
        self._kill: KillRequest | None = None
        self._stop: StopRequest | None = None
        self._detached: ConsumerDetached | None = None
        self._results: deque[Spawned | SpawnFailed | Exited | CommandDone] = deque()
        self._progress: ConsumerProgress | None = None

    def __len__(self) -> int:
        """The events waiting."""
        slots = (
            self._start,
            self._failed,
            self._kill,
            self._stop,
            self._detached,
            self._progress,
        )
        return len(self._results) + sum(slot is not None for slot in slots)

    def post(self, event: Event) -> None:
        """Add ``event``, merged with a waiting one of its kind (see the class docs).

        Raises:
            TypeError: ``event`` is `Data`, `Eof` or `Tick`.

        """
        match event:
            case Start():
                self._start = event
            case CallbackFailed():
                if self._failed is None:
                    self._failed = event
            case KillRequest():
                self._kill = event
            case StopRequest():
                if self._stop is None:
                    self._stop = event
            case ConsumerDetached(counts=counts):
                self._detached = event
                self._merge_progress(counts)
            case ConsumerProgress(counts=counts):
                self._merge_progress(counts)
            case Spawned() | SpawnFailed() | Exited() | CommandDone():
                self._results.append(event)
            case _:
                msg = f"{type(event).__name__} is not a control event"
                raise TypeError(msg)

    def _merge_progress(self, counts: OutboxCounts) -> None:
        waiting = self._progress
        self._progress = ConsumerProgress(
            counts if waiting is None else waiting.counts.newest(counts)
        )

    def take(self) -> Event | None:
        """Remove and return the next event, or None when none waits."""
        for name in ("_progress", "_start", "_failed", "_kill", "_stop", "_detached"):
            event = getattr(self, name)
            if event is not None:
                setattr(self, name, None)
                return event
        if self._results:
            return self._results.popleft()
        return None
