"""A scripted driver for `StreamCore` tests: a fake clock, a real `Outbox`.

`Harness` carries out the commands the core returns the way a driver would:
`Deliver`, `DiscardUndelivered` and `Finished` go to an `Outbox`, every other
command is recorded so a test can answer it (`answer`, `spawned`) or check it.
Like a driver (`logpyt._core.protocol.ConsumerProgress`), it reports the
outbox's counts after carrying out commands that changed them and always
before it carries out `Finished`, and the consumer side reports when it takes
(`consume`, `take`), settles (`ack`) or detaches (`detach`). `consume` is a
consumer whose callbacks return: it settles each record (`Outbox.done`, design
A.21); `take` leaves the record in hand, its callback still to run.
``at_finished`` keeps what a ``join()`` returning at `Finished` would see.
Time only moves when a test moves it (`advance`, ``at=``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, assert_never

from logpyt._core.machine import StreamCore
from logpyt._core.mailbox import Outbox
from logpyt._core.protocol import (
    CommandDone,
    ConsumerDetached,
    ConsumerProgress,
    Data,
    Deliver,
    Diagnostic,
    DiscardUndelivered,
    Eof,
    Exited,
    Finished,
    Kill,
    OutboxCounts,
    RunCommand,
    Spawn,
    Spawned,
    SpawnFailed,
    Start,
    Terminate,
    Tick,
)
from logpyt.adb import _AdbResult
from logpyt.config import StreamConfig
from logpyt.models import LogEntry, LogGroup
from logpyt.parsers import ParseContext
from logpyt.streams._events import Gap, StateChange, StreamState

if TYPE_CHECKING:
    from collections.abc import Iterable

    from logpyt._core.protocol import Command, CommandKind, Event, Pipe, Record
    from logpyt.filters import Condition
    from logpyt.groupers import GroupBy
    from logpyt.streams._events import StreamStats

ADB = "/fake/adb"
APP = "com.example.app"
APP_UID = 10065
CLOCK = 1_793_756_125
STAMP = "2026-11-04 05:11:16.661446 +0000"
# The wall-clock time of unparsed lines: fixed, so no test reads the host clock.
WALL = datetime(2026, 11, 4, 5, 11, 16)  # noqa: DTZ001  (fallback entries are naive)


class FixedContext(ParseContext):
    """A parse context whose ``now()`` is `WALL`."""

    def now(self) -> datetime:
        return WALL


def line(
    message: str,
    *,
    tag: str = "App",
    uid: int | str = APP_UID,
    pid: int = 1234,
    level: str = "I",
    stamp: str = STAMP,
) -> bytes:
    """One stdout line of the live wire format, with its LF."""
    return f"{stamp} {uid!s:>5} {pid:5d} {pid:5d} {level} {tag}: {message}\n".encode()


def lines(count: int, prefix: str = "m") -> bytes:
    """``count`` stdout lines ``m0``, ``m1``, ..."""
    return b"".join(line(f"{prefix}{index}") for index in range(count))


def result(
    returncode: int = 0, stdout: str = "", stderr: str = "", argv: tuple[str, ...] = ()
) -> _AdbResult:
    return _AdbResult(argv=argv, returncode=returncode, stdout=stdout, stderr=stderr)


CLOCK_OK = result(stdout=f"{CLOCK}\n")
CLOCK_FAILED = result(1, stderr="error: device offline\n")
MARKER_OK = result()
MARKER_FAILED = result(1, stderr="error: device offline\n")
STATE_DEVICE = result(stdout="device\n")
STATE_OFFLINE = result(1, stderr="error: device offline\n")


def lookup(uid: int | None, package: str = APP, *, key: str = "userId") -> _AdbResult:
    """A ``dumpsys package`` answer: ``package`` installed with ``uid``, or absent.

    ``key`` is ``userId`` (API 24) or ``appId`` (API 36, design A.8).
    """
    if uid is None:
        return result(stdout="Packages:\n")
    return result(stdout=f"Packages:\n  Package [{package}] (1a2b):\n    {key}={uid}\n")


LOOKUP_OK = lookup(APP_UID)
LOOKUP_ABSENT = lookup(None)  # the package is not installed
LOOKUP_FAILED = result(1, stderr="error: device offline\n")


def config(**fields: Any) -> StreamConfig:  # noqa: ANN401
    return StreamConfig(adb_path=ADB, **fields)


@dataclass  # noqa: PLR0904  (one helper per event a driver sends)
class Harness:
    """One core and the driver state around it (see the module docs)."""

    core: StreamCore
    now: float = 100.0
    outbox: Outbox = field(default_factory=Outbox)
    commands: list[Command] = field(default_factory=list)
    finished: list[Finished] = field(default_factory=list)
    taken: list[Record] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # The answers still owed: request id -> RunCommand; connection -> Spawn.
    pending: dict[int, RunCommand] = field(default_factory=dict)
    spawns: dict[int, Spawn] = field(default_factory=dict)
    signals: list[Terminate | Kill] = field(default_factory=list)
    markers: int = 0
    # The outbox counts the core was last told about.
    reported: OutboxCounts = field(default_factory=OutboxCounts)
    # When Finished was carried out: the core's stats and the outcome's.
    at_finished: tuple[StreamStats, StreamStats | None] | None = None

    @classmethod
    def make(
        cls,
        *,
        where: Condition | None = None,
        group_by: GroupBy | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> Harness:
        holder: list[Harness] = []

        def new_marker(kind: str) -> str:
            harness = holder[0]
            harness.markers += 1
            return f"{kind}-{harness.markers:032x}"

        core = StreamCore(
            config(**fields),
            adb_path=ADB,
            where=where,
            group_by=group_by,
            parse_context=FixedContext,
            new_marker=new_marker,
        )
        harness = cls(core)
        holder.append(harness)
        return harness

    # -- sending -------------------------------------------------------------------

    def send(self, event: Event, *, at: float | None = None) -> list[Command]:
        """Hand ``event`` to the core at ``at`` (default: now); run the commands."""
        if at is not None:
            self.now = at
        out = self.core.handle(event, self.now)
        self.run(out)
        assert self.core.unaccounted_lines() == 0
        self.check_hold()
        return out

    def check_hold(self) -> None:
        """The hold's counts match its lines, and Admission counts them (5.10)."""
        attribution = self.core._attribution
        admission = self.core._admission
        assert attribution.held_bytes == sum(held.nbytes for held in attribution._queue)
        assert admission.held_entries == attribution.held
        assert admission._held_bytes == attribution.held_bytes

    def advance(self, seconds: float) -> list[Command]:
        """Move the clock and send a Tick."""
        return self.send(Tick(), at=self.now + seconds)

    def until_deadline(self) -> list[Command]:
        """Jump to the next deadline and send a Tick there."""
        deadline = self.core.next_deadline()
        assert deadline is not None
        return self.send(Tick(), at=max(deadline, self.now))

    def run(self, commands: Iterable[Command]) -> None:
        for command in commands:
            assert not self.finished, f"{command} after Finished"
            self.commands.append(command)
            match command:
                case Deliver(records=records):
                    self.outbox.put(records)
                case DiscardUndelivered():
                    self.outbox.discard()
                case Finished(outcome=outcome):
                    # The engine side's last report comes before the end.
                    self.report()
                    self.finished.append(command)
                    self.outbox.close()
                    self.at_finished = (
                        self.core.stats(),
                        None if outcome is None else outcome.stats,
                    )
                case RunCommand(request=request):
                    self.pending[request] = command
                case Spawn(connection=connection):
                    self.spawns[connection] = command
                case Terminate() | Kill():
                    self.signals.append(command)
                case Diagnostic():
                    self.diagnostics.append(command)
                case _:
                    assert_never(command)
        # The engine side's report: a discard, a put into a detached outbox
        # or a coalesced event changed the counts.
        if self.outbox.counts() != self.reported:
            self.report()

    def report(self) -> list[Command]:
        """Send ``ConsumerProgress`` with the outbox's counts."""
        self.reported = self.outbox.counts()
        return self.send(ConsumerProgress(self.reported))

    # -- answering -----------------------------------------------------------------

    def request(self, kind: CommandKind) -> RunCommand:
        """The latest unanswered RunCommand of ``kind``."""
        found = [cmd for cmd in self.pending.values() if cmd.kind == kind]
        assert found, f"no pending {kind} command in {list(self.pending.values())}"
        return found[-1]

    def answer(
        self, kind: CommandKind, outcome: _AdbResult | Exception
    ) -> list[Command]:
        command = self.request(kind)
        del self.pending[command.request]
        return self.send(CommandDone(command.request, outcome))

    def spawned(self, pid: int = 4242) -> list[Command]:
        connection = self.core.connection
        assert connection is not None
        assert connection in self.spawns, "no Spawn for the live connection"
        return self.send(Spawned(connection, pid))

    def spawn_failed(self, error: OSError | None = None) -> list[Command]:
        connection = self.core.connection
        assert connection is not None
        return self.send(SpawnFailed(connection, error or FileNotFoundError("adb")))

    def stdout(self, chunk: bytes) -> list[Command]:
        return self.data("stdout", chunk)

    def stderr(self, chunk: bytes) -> list[Command]:
        return self.data("stderr", chunk)

    def data(self, pipe: Pipe, chunk: bytes) -> list[Command]:
        connection = self.core.connection
        assert connection is not None
        return self.send(Data(connection, pipe, chunk))

    def eof(self, *pipes: Pipe) -> list[Command]:
        connection = self.core.connection
        assert connection is not None
        out: list[Command] = []
        for pipe in pipes or ("stdout", "stderr"):
            out += self.send(Eof(connection, pipe))
        return out

    def exited(self, returncode: int = 1) -> list[Command]:
        connection = self.core.connection
        assert connection is not None
        return self.send(Exited(connection, returncode))

    def end_connection(self, returncode: int = 1) -> list[Command]:
        """The adb process exits and both pipes reach EOF."""
        out = self.exited(returncode)
        if self.core.connection is not None:
            out += self.eof()
        return out

    # -- scenarios -------------------------------------------------------------------

    def start(self) -> list[Command]:
        return self.send(Start())

    def connecting_spawned(self) -> Harness:
        """IDLE -> CONNECTING with a spawned child that printed nothing yet."""
        if self.core.state is StreamState.IDLE:
            self.start()
        if self.core.connection is not None and self.core.connection in self.spawns:
            return self
        self.answer("clock", CLOCK_OK)
        if self.core._conn is not None and self.core._conn.spawn_issued:
            self.spawned()
            return self
        self.answer("start_marker", MARKER_OK)
        self.spawned()
        return self

    def running(self, chunk: bytes = b"") -> Harness:
        """Up to RUNNING: the first stdout output (``chunk``, else one line)."""
        self.connecting_spawned()
        conn = self.core._conn
        assert conn is not None
        if conn.marker is not None:
            self.stdout(line(conn.marker, tag="logpyt"))
        self.stdout(chunk or line("first"))
        assert self.core.state is StreamState.RUNNING
        return self

    # -- consumer --------------------------------------------------------------------

    def consume(self, limit: int | None = None) -> list[Record]:
        """Take and finish records, up to ``limit`` items (all by default).

        A record still in hand (`take`) is settled first; each record taken
        here is popped and settled at once (`Outbox.done`), and the counts are
        reported once at the end. Events are taken as they come; only items
        count against ``limit``.
        """
        if self.outbox.in_hand:
            self.outbox.done()
        taken: list[Record] = []
        items = 0
        while limit is None or items < limit:
            record = self.outbox.pop()
            if record is None:
                break
            self.outbox.done()
            taken.append(record)
            if isinstance(record, LogEntry | LogGroup):
                items += 1
        self.taken += taken
        self.report()
        return taken

    def take(self) -> Record | None:
        """Pop one record and report it; its callback is still to run (A.21)."""
        record = self.outbox.pop()
        if record is not None:
            self.taken.append(record)
        self.report()
        return record

    def ack(self) -> list[Command]:
        """The callback of the record in hand returned: `Outbox.done`, report."""
        self.outbox.done()
        return self.report()

    def detach(self) -> list[Command]:
        """The consumer goes away: `Outbox.detach`, then ``ConsumerDetached``."""
        self.outbox.detach()
        self.reported = self.outbox.counts()
        return self.send(ConsumerDetached(self.reported))

    # -- reading results ----------------------------------------------------------

    def states(self) -> list[tuple[StreamState, StreamState]]:
        """Every state change delivered so far, in order."""
        return [
            (record.old, record.new)
            for command in self.commands
            if isinstance(command, Deliver)
            for record in command.records
            if isinstance(record, StateChange)
        ]

    def changes(self) -> list[StateChange]:
        return [
            record
            for command in self.commands
            if isinstance(command, Deliver)
            for record in command.records
            if isinstance(record, StateChange)
        ]

    def delivered(self) -> list[LogEntry | LogGroup]:
        """Every item the core delivered, in order."""
        return [
            record
            for command in self.commands
            if isinstance(command, Deliver)
            for record in command.records
            if isinstance(record, LogEntry | LogGroup)
        ]

    def messages(self) -> list[str]:
        out: list[str] = []
        for item in self.delivered():
            if isinstance(item, LogGroup):
                out += [entry.message for entry in item]
            else:
                out.append(item.message)
        return out

    def gaps(self) -> list[Gap]:
        return [
            record
            for command in self.commands
            if isinstance(command, Deliver)
            for record in command.records
            if isinstance(record, Gap)
        ]


def kinds(commands: Iterable[Command]) -> list[str]:
    """The command class names, Diagnostic left out, for compact assertions."""
    return [type(cmd).__name__ for cmd in commands if not isinstance(cmd, Diagnostic)]
