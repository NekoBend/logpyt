"""The events and commands between the stream core and its drivers (design 5.2).

A driver feeds `StreamCore.handle` one event at a time, with the host-monotonic
time, and carries out the commands it returns, in order. Both sides are plain
frozen dataclasses: the core reads no clock, starts no process and touches no
queue, so a test drives it with scripted events and a fake clock.

Events (13; the budget is 16):

- control, from the stream's owner, any thread or a callback: `Start`,
  `StopRequest`, `KillRequest`;
- from the driver's workers, each tagged with its connection or request:
  `Spawned`, `SpawnFailed`, `Data`, `Eof`, `Exited`, `CommandDone`;
- from the consumer side: `ConsumerProgress`, `ConsumerDetached`,
  `CallbackFailed`;
- `Tick`: nothing happened, but `StreamCore.next_deadline` has passed.

Commands (8; the budget is 8): `Spawn`, `Terminate`, `Kill`, `RunCommand`,
`Deliver`, `DiscardUndelivered`, `Diagnostic`, `Finished`.

Both unions are matched exhaustively with `typing.assert_never`, so adding a
member is a type error until every consumer handles it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from logpyt.adb import _AdbResult
    from logpyt.exceptions import StreamError
    from logpyt.models import LogEntry, LogGroup
    from logpyt.streams._events import Gap, StateChange

__all__ = [
    "ADB_LOGGER",
    "STREAM_LOGGER",
    "CallbackFailed",
    "Command",
    "CommandDone",
    "CommandKind",
    "ConsumerDetached",
    "ConsumerProgress",
    "Data",
    "Deliver",
    "Diagnostic",
    "DiscardUndelivered",
    "Eof",
    "Event",
    "Exited",
    "Finished",
    "Item",
    "Kill",
    "KillRequest",
    "OutboxCounts",
    "Pipe",
    "Record",
    "RunCommand",
    "Spawn",
    "SpawnFailed",
    "Spawned",
    "Start",
    "StopRequest",
    "Terminate",
    "Tick",
]

# The loggers a `Diagnostic` names: adb's stderr lines (design 5.9) and the
# stream's own messages.
ADB_LOGGER = "logpyt.adb"
STREAM_LOGGER = "logpyt.stream"

type Pipe = Literal["stdout", "stderr"]
# The one-shot adb commands of a run (design 5.6, 5.8, 2.3).
type CommandKind = Literal["clock", "start_marker", "state", "lookup", "flush_marker"]
# What the consumer receives: items, and the events interleaved with them (G3).
type Item = LogEntry | LogGroup
type Record = LogEntry | LogGroup | StateChange | Gap


@dataclass(frozen=True, slots=True)
class OutboxCounts:
    """Cumulative counters of an `Outbox`, as the driver reports them.

    Every field only grows, so of two reports of one outbox the field-wise
    maximum (`newest`) is the newer state, whatever order they were posted
    in; a control inbox keeps one of them at most.

    Attributes:
        taken_entries: Entries handed to the consumer; a group counts its
            members.
        taken_bytes: Their bytes (`logpyt._core.grouping.entry_bytes`).
        discarded_entries: Entries the outbox discarded (kill, a failed or a
            detached consumer).
        discarded_bytes: Their bytes.
        events_coalesced: State changes merged into a later one (design 5.10).

    """

    taken_entries: int = 0
    taken_bytes: int = 0
    discarded_entries: int = 0
    discarded_bytes: int = 0
    events_coalesced: int = 0

    @property
    def removed_entries(self) -> int:
        """Entries that left the outbox, taken or discarded."""
        return self.taken_entries + self.discarded_entries

    @property
    def removed_bytes(self) -> int:
        """Bytes that left the outbox, taken or discarded."""
        return self.taken_bytes + self.discarded_bytes

    def newest(self, other: OutboxCounts) -> OutboxCounts:
        """The field-wise maximum of two reports of the same outbox."""
        return OutboxCounts(
            taken_entries=max(self.taken_entries, other.taken_entries),
            taken_bytes=max(self.taken_bytes, other.taken_bytes),
            discarded_entries=max(self.discarded_entries, other.discarded_entries),
            discarded_bytes=max(self.discarded_bytes, other.discarded_bytes),
            events_coalesced=max(self.events_coalesced, other.events_coalesced),
        )


# -- events ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Start:
    """``start()`` was called on an IDLE stream."""


@dataclass(frozen=True, slots=True)
class StopRequest:
    """``stop()`` was called.

    Attributes:
        flush: ``stop(flush=f)``: write a flush marker and read until it, for
            at most ``f`` seconds of open intake; None for a plain stop. The
            core waits for nothing when ``f`` is negative or NaN (the flush
            times out at once) and without a bound when ``f`` is infinite.

    """

    flush: float | None = None


@dataclass(frozen=True, slots=True)
class KillRequest:
    """``kill()`` was called."""


@dataclass(frozen=True, slots=True)
class Spawned:
    """The `Spawn` of ``connection`` started adb as process ``pid``."""

    connection: int
    pid: int


@dataclass(frozen=True, slots=True)
class SpawnFailed:
    """The `Spawn` of ``connection`` raised ``error``; no process exists."""

    connection: int
    error: OSError


@dataclass(frozen=True, slots=True)
class Data:
    """A non-empty chunk read from one pipe of ``connection``."""

    connection: int
    pipe: Pipe
    chunk: bytes


@dataclass(frozen=True, slots=True)
class Eof:
    """``pipe`` of ``connection`` reached end of file, after its last `Data`."""

    connection: int
    pipe: Pipe


@dataclass(frozen=True, slots=True)
class Exited:
    """The adb process of ``connection`` exited with ``returncode``.

    Its pipes may still hold data: the end of the connection is the exit plus
    EOF on both pipes, or the exit plus ``exit_drain`` (design 3.2).
    """

    connection: int
    returncode: int


@dataclass(frozen=True, slots=True)
class CommandDone:
    """The `RunCommand` with id ``request`` finished.

    Attributes:
        request: The id from the `RunCommand`.
        result: What the one-shot runner returned, or what it raised:
            ``AdbError`` (``returncode`` None when adb could not start) or the
            builtin ``TimeoutError``.

    """

    request: int
    result: _AdbResult | Exception


@dataclass(frozen=True, slots=True)
class ConsumerProgress:
    """The outbox's counters changed; ``counts`` is its latest state.

    They change on both sides, and the core learns them only from this event
    (and from `ConsumerDetached`), so the driver reports
    ``ConsumerProgress(outbox.counts())``:

    - on the consumer side, in the same critical section (lock or task step)
      as the `Outbox.pop` that took the items, so a request posted after it
      (``kill()``, a `CallbackFailed`, a `ConsumerDetached`) never comes
      first;
    - on the engine side, after carrying out the commands of a
      `StreamCore.handle` call whenever they changed the counters: a
      `DiscardUndelivered`, a `Deliver` into a detached outbox (counted as
      discarded), a coalesced state change;
    - on the engine side, always, before carrying out a `Finished` (see
      there), handed straight to `StreamCore.handle`.

    Otherwise ``stats.undelivered``, ``delivered`` and ``events_coalesced``
    miss what the outbox did.
    """

    counts: OutboxCounts


@dataclass(frozen=True, slots=True)
class ConsumerDetached:
    """No consumer is attached any more: a ``break``, a closed iterator.

    The consumer side calls `Outbox.detach` first; ``counts`` is the outbox's
    state after it, the entries the detach discarded included. A
    `ControlInbox` hands these counts over before any waiting request.
    """

    counts: OutboxCounts


@dataclass(frozen=True, slots=True)
class CallbackFailed:
    """``on_item`` or ``on_event`` raised ``error`` in the consumer context.

    ``where`` and ``group_by`` run inside the core, which catches their
    exceptions itself.
    """

    stage: Literal["on_item", "on_event"]
    error: BaseException


@dataclass(frozen=True, slots=True)
class Tick:
    """No event; the time passed `StreamCore.next_deadline`."""


type Event = (
    Start
    | StopRequest
    | KillRequest
    | Spawned
    | SpawnFailed
    | Data
    | Eof
    | Exited
    | CommandDone
    | ConsumerProgress
    | ConsumerDetached
    | CallbackFailed
    | Tick
)


# -- commands --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spawn:
    """Start ``argv`` (an ``adb logcat``) as ``connection``.

    The driver answers with `Spawned` or `SpawnFailed`, then feeds the pipes as
    `Data` and `Eof` and the exit as `Exited`.
    """

    connection: int
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Terminate:
    """Ask the adb process of ``connection`` to end (SIGTERM; a kill on Windows)."""

    connection: int


@dataclass(frozen=True, slots=True)
class Kill:
    """Kill the adb process of ``connection`` (its pid only, never its group)."""

    connection: int


@dataclass(frozen=True, slots=True)
class RunCommand:
    """Run one short adb command through the one-shot runner.

    The driver answers with ``CommandDone(request, ...)``. A result the core no
    longer waits for is ignored (G5), so the driver never needs to cancel one;
    an abandoned command still runs until it ends, at most ``timeout`` plus the
    runner's reap. Two kinds are abandoned while the run goes on: a state
    check (the first output, the end of its connection, or a stop, kill,
    failure or detach came first) and a flush marker whose flush ended
    another way first (the marker read, the flush timer, a stall, a detach,
    a kill or a failure). STOPPING starts no other one-shot command, so
    besides the lookup, one clock, marker or state command of the current
    state (or the abandoned flush marker) runs, plus the abandoned state
    checks still within their timeout and reap. State checks start at least
    ``connect_timeout`` apart, so with the defaults (10 s against 5 s plus a
    5 s reap) that is one at most.

    Attributes:
        request: The id the result must carry.
        kind: What the command is for.
        argv: The command line, the adb path first.
        timeout: Seconds before the runner kills the command.

    """

    request: int
    kind: CommandKind
    argv: tuple[str, ...]
    timeout: float


@dataclass(frozen=True, slots=True)
class Deliver:
    """Append ``records`` to the outbox, in order: items and the events between them."""

    records: tuple[Record, ...]


@dataclass(frozen=True, slots=True)
class DiscardUndelivered:
    """Discard the items waiting in the outbox (`Outbox.discard`); keep the events.

    The driver then reports the outbox's counts (`ConsumerProgress`), so the
    discarded entries reach ``stats.undelivered``: after the commands of the
    batch, or before the `Finished` that ends it.
    """


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Log ``message`` at ``level`` (a `logging` level) on ``logger``."""

    level: int
    message: str
    logger: str = STREAM_LOGGER


@dataclass(frozen=True, slots=True)
class Finished:
    """The run ended; ``outcome`` is the latched error, None unless FAILED.

    Always the last command of the run, after the `Deliver` that carries the
    terminal `StateChange`. The driver carries out the commands before it in
    order, then hands ``ConsumerProgress(outbox.counts())`` to
    `StreamCore.handle` (always: the counts also cover consumer-side changes
    whose own reports still wait), and only then carries out ``Finished``. In
    a terminal state ``handle`` returns no commands and updates only the
    accounting and ``outcome.stats``, so this is safe, and a discard in the
    last batch reaches ``stats`` and ``outcome.stats`` before anyone can see
    the end.

    The consumer side keeps reporting after it (G2: accounting only).
    ``join()`` returns, and the final ``stats`` are published, only once the
    core has handled the consumer side's last report: the `ConsumerDetached`,
    or the `ConsumerProgress` posted after the consumer took the terminal
    `StateChange` (design 3.2: the consumer took the last item or is
    detached).
    """

    outcome: StreamError | None


type Command = (
    Spawn
    | Terminate
    | Kill
    | RunCommand
    | Deliver
    | DiscardUndelivered
    | Diagnostic
    | Finished
)
