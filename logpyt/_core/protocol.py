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
    """Cumulative counters of an `Outbox`, as the consumer side reports them.

    Every field only grows, so the latest report replaces any earlier one and a
    control inbox keeps one of them at most.

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
    """The outbox changed on the consumer side; ``counts`` is its latest state."""

    counts: OutboxCounts


@dataclass(frozen=True, slots=True)
class ConsumerDetached:
    """No consumer is attached any more: a ``break``, a closed iterator."""


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
    longer waits for is ignored (G5), so the driver never needs to cancel one.

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
    """Discard the items waiting in the outbox (`Outbox.discard`); keep the events."""


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
    terminal `StateChange`.
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
