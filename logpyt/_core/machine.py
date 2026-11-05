"""The stream core: one run of a live stream as a pure state machine (design 3, 5.2).

`StreamCore` owns every decision of a run: the lifecycle of design 3.2 (each
row of its transition table and the rules G1-G6), every timer, the reconnect
budget, flow control, framing, parsing, ``where``, grouping, the outcome latch
and the statistics. A driver (PR-12 threads, PR-13 asyncio) runs adb and the
consumer, feeds every event to `StreamCore.handle` with the monotonic time,
and carries out the commands it returns (`logpyt._core.protocol`).

Package attribution is the policy of `logpyt._core.attribution` (design
5.8): the uid lookups, the ordered hold and the tri-state evaluation of
``where``; the core runs its lookups, fires its deadline and accounts for the
lines it holds, and counts in ``unattributed`` the lines that went on before a
lookup answer named their package (a reinstall under a new uid). The start and
resume positions are a minimal policy behind a small interface that PR-14
replaces (`logpyt._core.resume`).

Facts the code relies on:

- Every [D] event of a connection that is not the live one is ignored (G1).
  One-shot commands belong to a state, the lookup to the run; a result that
  nobody waits for is ignored (G5).
- Each state change is a `StateChange` record in the outbox, in order with the
  items (G3). The order of commands matters only around
  `DiscardUndelivered` and `Finished`, so records go out in one `Deliver`
  before each of them and at the end of `handle`.
- Timers are computed from the state, not stored as events: a timer that its
  state does not arm cannot fire (G5). After the event, `handle` fires every
  timer that is due, earliest first, so a busy stream that never sends `Tick`
  still fires them.
- Admission (G6) counts what the core delivered against what the consumer side
  reported; intake closes when the outbox is full and reopens at half. While a
  kill or a failed consumer discards the undelivered items, or no consumer is
  attached, nothing blocks, so the connection always drains and ends.
- A run that would end STOPPED does not end while an attached consumer has
  records it has not finished (design A.21): the core counts the records it
  emits and compares them with the reported ``handled`` plus
  ``events_coalesced``, never with what was merely taken. The run concludes
  first (groups flushed, the connection retired, pending commands abandoned,
  timers disarmed) and waits in STOPPING(drain), which reads nothing, emits
  nothing and arms no timer; only the terminal `StateChange` and `Finished`
  wait for the consumer. KILLED and FAILED runs never wait.
- Lines held for attribution count against the outbox (design 5.10). They
  go on before the end of their connection (design 3.6: entries come before
  the change to RECONNECTING), when a consumer detaches, and when the run
  concludes, before the groups are flushed (design A.21); a kill or a failed
  run discards and counts them instead.
- Line conservation: every framed stdout line is delivered (admitted to the
  outbox, as an entry or a group member), filtered, discarded and counted in
  ``undelivered``, counted in ``before_start`` (or by PR-14 in
  ``deduplicated``/``below_cursor``), or still held for attribution or in an
  open group. `StreamCore.unaccounted_lines` is that balance; it is always 0.
"""

from __future__ import annotations

import itertools
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, assert_never

from logpyt._core.accounting import Pausable, StderrLog, Tally
from logpyt._core.argv import logcat_argv
from logpyt._core.attribution import Attribution
from logpyt._core.framing import Framer, decode_line
from logpyt._core.grouping import Grouper
from logpyt._core.mailbox import Admission, item_size
from logpyt._core.pipeline import Pipeline
from logpyt._core.protocol import (
    CallbackFailed,
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
    KillRequest,
    OutboxCounts,
    RunCommand,
    Spawn,
    Spawned,
    SpawnFailed,
    Start,
    StopRequest,
    Terminate,
    Tick,
)
from logpyt._core.resume import Positioner
from logpyt.adb import (
    _describe,
    _device_clock_argv,
    _get_state_argv,
    _is_marker,
    _log_marker_argv,
    _new_marker,
    _parse_device_clock,
    _parse_get_state,
    _parse_log_marker,
)
from logpyt.exceptions import (
    AdbError,
    AdbExitError,
    CallbackError,
    ReadTimeoutError,
    StreamError,
)
from logpyt.parsers import ParseContext, ThreadTimeLogParser
from logpyt.streams._events import StateChange, StreamState, StreamStats

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from logpyt._core.argv import Position
    from logpyt._core.attribution import Verdict
    from logpyt._core.framing import FramedLine
    from logpyt._core.protocol import (
        Command,
        CommandKind,
        Event,
        Item,
        Pipe,
        Record,
    )
    from logpyt.adb import _AdbResult
    from logpyt.config import StreamConfig
    from logpyt.exceptions import CallbackStage
    from logpyt.filters import Condition
    from logpyt.groupers import GroupBy
    from logpyt.models import LogEntry

__all__ = ["Mode", "StreamCore"]

S = StreamState
TERMINAL = frozenset({S.STOPPED, S.KILLED, S.FAILED})
# Design 5.6: the one-shot runner's timeout for the clock, markers, state check
# and lookup.
COMMAND_TIMEOUT = 5.0
_PIPES: tuple[Pipe, Pipe] = ("stdout", "stderr")
# The commands that belong to CONNECTING (G5); "lookup" belongs to the run.
_CONNECTING_KINDS = frozenset({"clock", "start_marker", "state"})


class Mode(StrEnum):
    """What STOPPING is doing (design 3.1; ``drain``: design A.21)."""

    STOP = "stop"
    FLUSH = "flush"
    KILL = "kill"
    FAIL = "fail"
    # The data side ended; the attached consumer still has records to finish.
    DRAIN = "drain"


type _Timer = Literal[
    "connect",
    "read",
    "exit_drain",
    "stop_grace",
    "backoff",
    "stable",
    "stall",
    "flush",
    "hold",
    "idle",
    "stderr_report",
]


@dataclass(slots=True, eq=False)
class _Request:
    """A one-shot command the core waits for."""

    kind: CommandKind
    argv: tuple[str, ...]
    connection: int | None = None
    token: str | None = None  # the marker a marker command writes
    lookup: int | None = None  # the attribution's id of a lookup


@dataclass(slots=True, eq=False)
class _Connection:
    """One attempt: from the clock read to the end of its adb process."""

    id: int
    pipeline: Pipeline
    stderr_tail: deque[str]
    framers: dict[Pipe, Framer]
    argv: tuple[str, ...] = ()
    spawn_issued: bool = False
    pid: int | None = None
    output: bool = False  # a stdout byte was read
    returncode: int | None = None
    eof: set[Pipe] = field(default_factory=set)
    marker: str | None = None  # the start marker this connection waits for
    clock: int | None = None  # the device clock of the attempt
    connect_at: float | None = None  # connect_timeout deadline
    exit_drain: Pausable | None = None
    stop_grace: float | None = None
    last_activity: float = 0.0
    killed: bool = False  # Kill(c) was issued
    # Why the stall ended it: held lines alone kept the outbox full, or not.
    stalled: Literal["consumer", "attribution"] | None = None
    # Why the core ended it: a read timeout or a failed state check.
    cause: tuple[Literal["timeout", "device"], str] | None = None

    @property
    def alive(self) -> bool:
        """Spawned and not yet exited."""
        return self.pid is not None and self.returncode is None


class StreamCore:
    """One run of a live stream (see the module docs).

    Args:
        config: The validated configuration.
        adb_path: The resolved adb executable.
        where: The condition each entry must match; None delivers every entry.
        group_by: Deliver groups instead of entries.
        parse_context: Makes the parse context of each connection; its
            ``now()`` is the only wall-clock time the core uses (the
            timestamps of unrecognised lines, design A.19).
        new_marker: Makes the start and flush marker tokens; tests pass a
            deterministic one.

    Raises:
        ValueError: ``group_by.max_group_size`` exceeds ``buffer_entries``
            (design 2.8), or a name ``where.packages()`` returns is not a
            package name (F5-3), a process name (``<package>:<process>``)
            included: attribution maps a uid to an app.
        TypeError: A name ``where.packages()`` returns is not a str, or a
            ``packages()`` override returns one str (or bytes) instead of a
            collection of names.

    """

    def __init__(
        self,
        config: StreamConfig,
        *,
        adb_path: str,
        where: Condition | None = None,
        group_by: GroupBy | None = None,
        parse_context: Callable[[], ParseContext] = ParseContext,
        new_marker: Callable[[Literal["start", "flush"]], str] = _new_marker,
    ) -> None:
        if group_by is not None and group_by.max_group_size > config.buffer_entries:
            msg = (
                f"group_by.max_group_size ({group_by.max_group_size}) exceeds "
                f"buffer_entries ({config.buffer_entries}): one group must fit "
                "the outbox"
            )
            raise ValueError(msg)
        self._config = config
        self._adb = adb_path
        self._where = where
        self._grouper = None if group_by is None else Grouper(group_by)
        self._parse_context = parse_context
        self._new_marker = new_marker
        self._parser = ThreadTimeLogParser()
        self._attribution = Attribution(
            where,
            adb_path=adb_path,
            serial=config.serial,
            package_hold=config.package_hold,
        )
        self._positioner = Positioner(config.since)
        self._admission = Admission(config.buffer_entries, config.buffer_bytes)

        self._state = S.IDLE
        self._mode: Mode | None = None
        self._now = float("-inf")
        self._conn: _Connection | None = None
        self._number = 0  # the current attempt's connection number
        self._requests: dict[int, _Request] = {}
        self._request_ids = itertools.count(1)
        self._out: list[Command] = []
        self._records: list[Record] = []

        self._outcome: StreamError | None = None
        self._kill_requested = False
        self._detached = False
        self._started = not self._positioner.start_marker  # start marker read
        self._read_any = False  # a stdout line was read
        # Reconnect budget (design 5.6).
        reconnect = config.reconnect
        self._attempts = 0
        self._delay = reconnect.delay if reconnect is not None else 0.0
        self._backoff_at: float | None = None
        self._stable_at: float | None = None
        self._resume_on_intake = False  # RECONNECTING after a stalled connection
        # stop(flush=).
        self._flush: Pausable | None = None
        self._flush_token: str | None = None
        # Intake and backpressure.
        self._intake_was = True
        self._reopened_at = float("-inf")
        self._bp_since: float | None = None
        self._bp_total = 0.0
        self._counts = OutboxCounts()
        self._emitted = 0  # records sent in Deliver commands (A.21)
        self._tally = Tally()
        self._warned_discard = False
        self._discard_reason = "the consumer detached"  # of the outbox's discards
        self._stderr_log = StderrLog()

    # -- public interface ----------------------------------------------------------

    @property
    def state(self) -> StreamState:
        """The current state."""
        return self._state

    @property
    def mode(self) -> Mode | None:
        """What STOPPING is doing; None in every other state."""
        return self._mode if self._state is S.STOPPING else None

    @property
    def outcome(self) -> StreamError | None:
        """The latched error (G4), None while none is latched."""
        return self._outcome

    @property
    def connection(self) -> int | None:
        """The live connection's number, or None (G1: others are retired)."""
        return None if self._conn is None else self._conn.id

    @property
    def intake_open(self) -> bool:
        """Whether the driver may hand over `Data` and `Eof` now.

        False while the outbox is full (until the low-water mark) and while
        the first package lookup runs. A kill and a failed consumer discard
        what is read, and a detached consumer takes nothing, so then intake
        stays open: the connection drains and ends. True in STOPPING(drain):
        a retired reader blocked on the data inbox is drained and ignored (G1).
        """
        if self._state in TERMINAL or self._mode in {Mode.KILL, Mode.FAIL, Mode.DRAIN}:
            return True
        if not self._attribution.ready:
            return False
        return self._detached or self._admission.open

    @property
    def concluded(self) -> bool:
        """Whether the run's data side has ended: terminal, or STOPPING(drain).

        Nothing more is read or emitted but the terminal `StateChange`;
        ``wait_running()`` returns (or raises the latched outcome) from here
        (design A.21).
        """
        return self._state in TERMINAL or (
            self._state is S.STOPPING and self._mode is Mode.DRAIN
        )

    def late_failure(self, event: CallbackFailed) -> str | None:
        """The ERROR message for a callback failure that cannot become the outcome.

        None while handling ``event`` would latch it (G4): the run is live and
        no error is latched. Otherwise the run is terminal (``handle`` returns
        nothing, G2) or another error is latched (the first error wins, 4.3;
        the callback_failed row still discards and kills): the outcome does
        not change, and the engine logs this message at ERROR with
        ``event.error``'s traceback before ``join()`` returns (design A.21).
        """
        if self._state not in TERMINAL and self._outcome is None:
            return None
        verdict = (
            f"after the run ended {self._state}"
            if self._state in TERMINAL
            else "after the run had already failed"
        )
        return (
            f"{event.stage} raised {type(event.error).__name__}: {event.error} "
            f"{verdict}; the outcome is unchanged"
        )

    def next_deadline(self) -> float | None:
        """The earliest armed timer, in the monotonic time of ``handle``."""
        deadlines = self._deadlines()
        return min(deadlines.values()) if deadlines else None

    def armed_timers(self) -> frozenset[str]:
        """The names of the armed timers (for tests and diagnostics)."""
        return frozenset(self._deadlines())

    def handle(self, event: Event, now: float) -> list[Command]:
        """Take one event at monotonic time ``now``; return the commands to run.

        ``now`` never goes back: an earlier value counts as the latest one.

        Raises:
            AssertionError: The event cannot occur in this state (design 3.2:
                for example ``Spawned`` of the live connection while RUNNING).

        """
        now = max(now, self._now)
        self._now = now
        if self._state in TERMINAL:
            self._terminal(event)
            return []
        self._dispatch(event, now)
        self._settle(now)
        self._fire_due(now)
        self._flush_records()
        out, self._out = self._out, []
        return out

    def stats(self) -> StreamStats:
        """A snapshot of the run's counters (design 2.5, 4.4)."""
        pressure = self._bp_total
        if self._bp_since is not None:
            pressure += self._now - self._bp_since
        return self._tally.snapshot(
            connections=self._number,
            counts=self._counts,
            backpressure_seconds=pressure,
            killed=self._state is S.KILLED,
            failed=self._state is S.FAILED,
        )

    def unaccounted_lines(self) -> int:
        """The line-conservation balance (see the module docs); always 0."""
        held = self._attribution.held
        if self._grouper is not None:
            held += self._grouper.open_entries
        accounted = self._tally.accounted(self._admission.admitted_entries, held)
        return self._tally.lines - accounted

    # -- dispatch --------------------------------------------------------------------

    def _terminal(self, event: Event) -> None:
        """G2: in a terminal state only the outbox accounting changes."""
        if isinstance(event, ConsumerProgress | ConsumerDetached):
            if isinstance(event, ConsumerDetached):
                self._detached = True
            self._progress(event.counts)
            if self._outcome is not None:
                self._outcome.stats = self.stats()

    def _dispatch(self, event: Event, now: float) -> None:
        if self._state is S.IDLE:
            if isinstance(event, Start):
                self._start(now)
            # Everything else is a no-op (D-6) or cannot occur before start.
            return
        match event:
            case Start():
                msg = f"Start in {self._state}: start() is for an IDLE stream only"
                raise AssertionError(msg)
            case StopRequest(flush=flush):
                self._stop(flush, now)
            case KillRequest():
                self._kill()
            case CallbackFailed(stage=stage, error=error):
                self._callback_failed(stage, error)
            case ConsumerDetached(counts=counts):
                self._consumer_detached(counts, now)
            case ConsumerProgress(counts=counts):
                self._progress(counts)
            case CommandDone(request=request, result=result):
                self._command_done(request, result, now)
            case Spawned() | SpawnFailed() | Data() | Eof() | Exited():
                self._connection_event(event, now)
            case Tick():
                pass
            case _:
                assert_never(event)

    def _connection_event(
        self, event: Spawned | SpawnFailed | Data | Eof | Exited, now: float
    ) -> None:
        conn = self._conn
        if conn is None or event.connection != conn.id:
            return  # G1: a retired connection
        state = self._state
        match event:
            case Spawned(pid=pid):
                self._check(
                    event,
                    possible=conn.spawn_issued
                    and conn.pid is None
                    and state is not S.RUNNING,
                )
                conn.pid = pid
                if state is S.CONNECTING:
                    conn.connect_at = now + self._config.connect_timeout
                elif self._mode in {Mode.STOP, Mode.FLUSH}:
                    self._terminate(now)
                else:
                    self._kill_child()
            case SpawnFailed(error=error):
                self._check(
                    event,
                    possible=conn.spawn_issued
                    and conn.pid is None
                    and state is not S.RUNNING,
                )
                self._spawn_failed(error, now)
            case Data(pipe=pipe, chunk=chunk):
                self._check(
                    event, possible=conn.pid is not None and pipe not in conn.eof
                )
                conn.last_activity = now
                if pipe == "stdout":
                    # Output that comes after the core gave the connection up
                    # (Kill after a failed state check) does not make it RUNNING.
                    if (
                        state is S.CONNECTING
                        and conn.marker is None
                        and not conn.killed
                    ):
                        self._enter_running(now)
                    self._first_output(conn, now)
                self._lines_of(pipe, conn.framers[pipe].feed(chunk), now)
            case Eof(pipe=pipe):
                self._check(
                    event, possible=conn.pid is not None and pipe not in conn.eof
                )
                self._lines_of(pipe, conn.framers[pipe].finish(), now)
                conn.eof.add(pipe)
                self._maybe_ended(conn, now)
            case Exited(returncode=returncode):
                self._check(event, possible=conn.alive)
                conn.returncode = returncode
                # An exited adb needs no state check: EOF or exit_drain ends it.
                conn.connect_at = None
                conn.exit_drain = Pausable(
                    self._config.exit_drain, now, running=self.intake_open
                )
                self._maybe_ended(conn, now)
            case _:
                assert_never(event)

    def _check(self, event: Event, *, possible: bool) -> None:
        """Fail on an event that cannot occur here (design 3.2, last paragraph)."""
        if not possible:
            msg = f"{type(event).__name__} cannot occur in {self._describe()}"
            raise AssertionError(msg)

    def _describe(self) -> str:
        mode = self.mode
        return f"{self._state}({mode})" if mode is not None else str(self._state)

    # -- control rows ----------------------------------------------------------------

    def _start(self, now: float) -> None:
        """IDLE start: CONNECTING, the clock read, the lookup of watched packages."""
        self._attribution.begin(now)
        self._begin_attempt(now)
        self._next_lookup(now)

    def _stop(self, flush: float | None, now: float) -> None:
        state = self._state
        if flush is not None and state in {S.CONNECTING, S.RECONNECTING}:
            # No connection reads the marker here: the flush cannot run, so the
            # capture is incomplete whichever way the stop and a connection
            # end raced (design A.20).
            self._tally.flush_failed = True
        if state is S.CONNECTING:
            self._stop_connecting(now)
        elif state is S.RUNNING:
            if flush is None:
                self._enter_stopping(Mode.STOP)
                self._terminate(now)
            else:
                self._enter_stopping(Mode.FLUSH)
                token = self._new_marker("flush")
                self._flush_token = token
                self._run(
                    "flush_marker",
                    _log_marker_argv(self._adb, self._config.serial, token),
                    token=token,
                )
                # A negative or NaN bound waits for nothing; math.inf waits
                # without a bound (no flush timer).
                bound = flush if flush > 0 else 0.0
                self._flush = (
                    None
                    if math.isinf(bound)
                    else Pausable(bound, now, running=self.intake_open)
                )
        elif state is S.RECONNECTING:
            self._finish()
        # STOPPING: nothing (a stop is already under way).

    def _stop_connecting(self, now: float) -> None:
        """CONNECTING stop: STOPPING(stop) with a child; without one the run concludes.

        It ends STOPPED, or waits in STOPPING(drain) for the consumer (A.21).
        """
        conn = self._conn
        if conn is not None and conn.spawn_issued:
            self._enter_stopping(Mode.STOP)
            if conn.pid is not None:
                self._terminate(now)
        else:
            self._finish()

    def _kill(self) -> None:
        self._kill_requested = True
        if self._childless():
            self._discard_undelivered()
            self._finish()
            return
        self._enter_stopping(Mode.KILL)
        self._kill_child()
        self._discard_undelivered()

    def _callback_failed(self, stage: CallbackStage, error: BaseException) -> None:
        """A user callback raised: latch CallbackError and end the run (fail fast)."""
        self._latch(self._callback_error(stage, error))
        consumer = stage in {"on_item", "on_event"}
        if consumer:
            # The consumer takes nothing more; where/group_by failures keep
            # the items accepted before them (F3-1).
            self._discard_undelivered()
        if self._childless():
            self._finish()
            return
        self._enter_stopping(Mode.FAIL)
        self._kill_child()

    def _childless(self) -> bool:
        """No adb process can end the run: RECONNECTING, early CONNECTING, the drain."""
        conn = self._conn
        return (
            self._state is S.RECONNECTING
            or (self._state is S.CONNECTING and (conn is None or not conn.spawn_issued))
            or self._mode is Mode.DRAIN
        )

    def _consumer_detached(self, counts: OutboxCounts, now: float) -> None:
        """No consumer is left: the rest is discarded and counted (design 3.5)."""
        self._detached = True
        self._discard_reason = "the consumer detached"
        # The counts include what the detach discarded: the WARNING follows
        # what was discarded, not the core's estimate of the fill.
        self._progress(counts)
        state = self._state
        if state in TERMINAL:
            return  # the detach's counts showed every record handled (A.21)
        if self._mode is Mode.DRAIN:
            self._end()
            return
        # Nobody will take the held lines: they go on now and are discarded
        # and counted by the outbox's rule (design 3.5).
        self._release_held(now, force=True)
        state = self._state
        if state in TERMINAL:
            return  # where raised on a released line: the run failed
        if state is S.CONNECTING:
            self._stop_connecting(now)
        elif state is S.RECONNECTING:
            self._finish()
        elif state is S.RUNNING or self._mode is Mode.FLUSH:
            self._enter_stopping(Mode.STOP)
            self._terminate(now)
        elif self._dropping():
            self._discard_pending()

    def _progress(self, counts: OutboxCounts) -> None:
        """G6: the outbox's latest counts update admission.

        Entries the outbox discarded (a detach, or items put into a detached
        outbox) get the run's one discard WARNING here, while the run is live.
        In STOPPING(drain) the run ends once the consumer has handled every
        record (design A.21).
        """
        merged = self._counts.newest(counts)
        discarded = merged.discarded_entries > self._counts.discarded_entries
        self._counts = merged
        self._admission.release(merged)
        if discarded and self._state not in TERMINAL:
            self._warn_discard(self._discard_reason)
        if self.mode is Mode.DRAIN and self._drained():
            self._end()

    # -- one-shot commands ---------------------------------------------------------

    def _run(
        self,
        kind: CommandKind,
        argv: tuple[str, ...],
        *,
        connection: int | None = None,
        token: str | None = None,
        lookup: int | None = None,
    ) -> None:
        request = next(self._request_ids)
        self._requests[request] = _Request(kind, argv, connection, token, lookup)
        self._out.append(RunCommand(request, kind, argv, COMMAND_TIMEOUT))

    def _next_lookup(self, now: float) -> None:
        """Run the lookup the attribution asks for now, if any (one at a time)."""
        lookup = self._attribution.next_lookup(now)
        if lookup is not None:
            self._run("lookup", lookup.argv, lookup=lookup.ident)

    def _command_done(
        self, request: int, result: _AdbResult | Exception, now: float
    ) -> None:
        pending = self._requests.pop(request, None)
        if pending is None:
            return  # G5: nobody waits for it any more
        conn = self._conn
        match pending.kind:
            case "lookup":
                self._lookup_done(pending, result, now)
            case "flush_marker":
                self._flush_marker_done(result, now)
            case "clock" | "start_marker" | "state":
                if conn is None or pending.connection != conn.id:
                    return
                if pending.kind == "clock":
                    self._clock_done(conn, pending, result)
                elif pending.kind == "start_marker":
                    self._start_marker_done(conn, pending, result)
                else:
                    self._state_checked(conn, result, now)
            case _:
                assert_never(pending.kind)

    def _clock_done(
        self, conn: _Connection, request: _Request, result: _AdbResult | Exception
    ) -> None:
        """CONNECTING clock done: record it; the start marker or the spawn."""
        try:
            seconds = _parse_device_clock(_success(result))
        except (AdbError, TimeoutError) as exc:
            self._attempt_failed(request, exc)
            return
        conn.clock = seconds
        for gap in self._positioner.clock(seconds, resume=self._read_any):
            self._tally.gaps += 1
            self._records.append(gap)
        if not self._started:
            token = self._new_marker("start")
            self._run(
                "start_marker",
                _log_marker_argv(self._adb, self._config.serial, token),
                connection=conn.id,
                token=token,
            )
        else:
            self._spawn(conn, self._positioner.position(resume=self._read_any))

    def _start_marker_done(
        self, conn: _Connection, request: _Request, result: _AdbResult | Exception
    ) -> None:
        """CONNECTING start marker done: spawn at ``-T <clock - 1 s>``."""
        try:
            _parse_log_marker(_success(result))
        except (AdbError, TimeoutError) as exc:
            self._attempt_failed(request, exc)
            return
        conn.marker = request.token
        self._spawn(conn, self._positioner.position(resume=self._read_any))

    def _state_checked(
        self, conn: _Connection, result: _AdbResult | Exception, now: float
    ) -> None:
        """CONNECTING state check: RUNNING for a quiet device, else end the attempt."""
        try:
            device_state = _parse_get_state(_success(result))
        except (AdbError, TimeoutError) as exc:
            reason = f"adb get-state failed: {exc}"
        else:
            if device_state == "device":
                if not conn.alive or conn.killed:
                    # adb exited (or was killed) while the check ran: the
                    # connection-ended row decides, not a quiet device.
                    return
                if conn.marker is None:
                    self._enter_running(now)
                else:
                    conn.connect_at = now + self._config.connect_timeout
                return
            reason = "not found" if device_state is None else device_state
        conn.cause = ("device", f"device not available: {reason}")
        self._kill_child()

    def _lookup_done(
        self, request: _Request, result: _AdbResult | Exception, now: float
    ) -> None:
        """lookup done: update the uid map, open intake, release held lines.

        The lines that went on without the package the answer names, and
        whose verdict depended on it, count as unattributed now (design 4.4,
        5.8).
        """
        assert request.lookup is not None  # noqa: S101  (set by _next_lookup)
        answer = self._attribution.lookup_done(request.lookup, result)
        for warning in answer.warnings:
            self._out.append(Diagnostic(logging.WARNING, warning))
        self._tally.unattributed += answer.unattributed
        self._attribution_changed(now)

    def _attribution_changed(self, now: float) -> None:
        """After a lookup result or the attribution's deadline (design 3.2 rows).

        The next lookup starts and the held lines that may go on do; in
        STOPPING(kill or fail) held lines and open groups are discarded and
        counted instead, and no lookup starts.
        """
        if self._dropping():
            self._discard_pending()
            return
        self._next_lookup(now)
        self._release_held(now)

    def _flush_marker_done(self, result: _AdbResult | Exception, now: float) -> None:
        """STOPPING(flush) flush marker done; a failure stops without the marker."""
        try:
            _parse_log_marker(_success(result))
        except (AdbError, TimeoutError):
            if self._mode is Mode.FLUSH:
                self._tally.flush_failed = True
                self._end_flush(now)

    # -- attempts and connections ------------------------------------------------

    def _begin_attempt(self, now: float) -> None:
        """Enter CONNECTING with a new connection and read the device clock."""
        del now
        self._number += 1
        conn = _Connection(
            id=self._number,
            # The attribution sets each line's package (design 5.8).
            pipeline=Pipeline(self._parser, self._parse_context()),
            stderr_tail=deque(maxlen=self._config.stderr_tail),
            framers={pipe: Framer(self._config.max_line_bytes) for pipe in _PIPES},
        )
        self._conn = conn
        self._set_state(S.CONNECTING)
        self._run(
            "clock",
            _device_clock_argv(self._adb, self._config.serial),
            connection=conn.id,
        )

    def _spawn(self, conn: _Connection, position: Position | None) -> None:
        conn.argv = logcat_argv(self._adb, self._config, position)
        conn.spawn_issued = True
        self._out.append(Spawn(conn.id, conn.argv))

    def _enter_running(self, now: float) -> None:
        """CONNECTING -> RUNNING: the first output, or a quiet device that is there."""
        conn = self._conn
        assert conn is not None  # noqa: S101  (RUNNING has a live connection)
        conn.connect_at = None
        conn.last_activity = now
        self._set_state(S.RUNNING)
        if conn.output:
            self._arm_stable(now)

    def _first_output(self, conn: _Connection, now: float) -> None:
        """stable_after counts from a connection's first output (design 5.6)."""
        if not conn.output:
            conn.output = True
            if self._state is S.RUNNING:
                self._arm_stable(now)

    def _arm_stable(self, now: float) -> None:
        reconnect = self._config.reconnect
        if reconnect is not None:
            self._stable_at = now + reconnect.stable_after

    def _attempt_failed(self, request: _Request, error: Exception) -> None:
        """The clock read or the start marker failed: the device is not available."""
        self._requests_of_connecting_dropped()
        returncode = error.returncode if isinstance(error, AdbError) else None
        stderr = error.stderr if isinstance(error, AdbError) else ""
        argv = (
            error.argv if isinstance(error, AdbError) and error.argv else request.argv
        )
        # An adb that cannot be started is not retried, like a spawn (A-12).
        could_not_start = isinstance(error, AdbError) and returncode is None
        if could_not_start:
            # The runner's message names the command: "could not start adb ...".
            message = str(error) or "could not start adb"
        elif isinstance(error, TimeoutError):
            message = (
                f"device not available: {_describe(request.argv)} timed out "
                f"after {COMMAND_TIMEOUT:g} s"
            )
        else:
            message = "device not available"
        cause = AdbExitError(
            message,
            argv=argv,
            returncode=returncode,
            stderr=stderr,
            stats=self.stats(),
        )
        cause.__cause__ = error
        self._conn = None
        self._attempt_ended(cause, stalled=None, retry=not could_not_start)

    def _spawn_failed(self, error: OSError, now: float) -> None:
        """spawn_failed: CONNECTING ends FAILED, never retried (A-12).

        STOPPING concludes: it ends (G4), or STOPPING(stop or flush) waits in
        STOPPING(drain) for the consumer (A.21).
        """
        conn = self._conn
        assert conn is not None  # noqa: S101  (checked by the caller)
        self._conn = None
        if self._state is S.STOPPING:
            self._stopping_ended(now)
            return
        cause = AdbExitError(
            f"could not start adb: {error}",
            argv=conn.argv,
            returncode=None,
            stderr="",
            stats=self.stats(),
        )
        cause.__cause__ = error
        self._latch(cause)
        self._finish()

    def _maybe_ended(self, conn: _Connection, now: float) -> None:
        if conn.returncode is not None and len(conn.eof) == len(_PIPES):
            self._connection_ended(now)

    def _connection_ended(self, now: float) -> None:
        """The connection ended: exited and both pipes at EOF, or exit_drain expired."""
        conn = self._conn
        assert conn is not None  # noqa: S101  (only a live connection ends)
        self._conn = None  # retired (G1)
        if self._state is not S.STOPPING:
            # Its held lines go before the change to RECONNECTING (design 3.6):
            # what is still unknown is evaluated without a package.
            self._release_held(now, force=True)
            if self._state in TERMINAL:
                return  # where raised on a released line: the run failed
        if self._state is S.STOPPING:
            self._stopping_ended(now)
            return
        self._flush_groups()
        if conn.stalled is not None:
            self._attempt_ended(None, stalled=conn.stalled, retry=True)
            return
        self._attempt_ended(self._end_cause(conn), stalled=None, retry=True)

    def _end_cause(self, conn: _Connection) -> StreamError:
        """The error of a connection that ended unrequested (design 3.2)."""
        tail = "\n".join(conn.stderr_tail)
        if conn.cause is not None and conn.cause[0] == "timeout":
            error: StreamError = ReadTimeoutError(conn.cause[1], stats=self.stats())
            if tail:
                error.add_note(f"adb stderr:\n{tail}")
            return error
        message = (
            conn.cause[1]
            if conn.cause is not None
            else f"adb logcat exited with status {conn.returncode}"
        )
        return AdbExitError(
            message,
            argv=conn.argv,
            returncode=conn.returncode,
            stderr=tail,
            stats=self.stats(),
        )

    def _attempt_ended(
        self,
        cause: StreamError | None,
        *,
        stalled: Literal["consumer", "attribution"] | None,
        retry: bool,
    ) -> None:
        """RECONNECTING with attempts left or after a stall; else FAILED."""
        number = self._number
        reconnect = self._config.reconnect
        left = reconnect is not None and (
            reconnect.max_attempts is None or self._attempts < reconnect.max_attempts
        )
        if stalled is not None or (retry and left):
            self._set_state(S.RECONNECTING, error=cause)
            if stalled is not None:
                self._resume_on_intake = True
                why = (
                    "lines waited for package attribution (held lines count "
                    "against the outbox); it resumes once they have gone on"
                    if stalled == "attribution"
                    else "the consumer fell behind; it resumes when the consumer "
                    "catches up"
                )
                self._out.append(
                    Diagnostic(
                        logging.INFO,
                        f"logcat connection {number} was disconnected because {why}",
                    )
                )
            else:
                self._backoff_at = self._now + self._delay
                self._out.append(
                    Diagnostic(
                        logging.WARNING,
                        f"logcat connection {number} ended, reconnecting in "
                        f"{self._delay:g} s: {cause}",
                    )
                )
            return
        assert cause is not None  # noqa: S101  (only a stall has no cause)
        if retry and reconnect is not None:
            attempts = self._attempts
            cause.add_note(f"after {attempts} attempt{'' if attempts == 1 else 's'}")
        self._latch(cause)
        self._finish()

    def _stopping_ended(self, now: float) -> None:
        """STOPPING: the connection ended, or its spawn failed: the run concludes.

        It ends (G4), or STOPPING(stop or flush) waits in STOPPING(drain) for
        the consumer (A.21).
        """
        del now
        if self._mode is Mode.FLUSH:
            self._tally.flush_failed = True
        self._finish()

    def _finish(self) -> None:
        """The run concludes; it ends now, or drains first (design A.21).

        The conclusion is the last point where records are emitted: groups
        are flushed, the suppressed stderr count is logged, the connection is
        retired (G1), every pending command is abandoned (G5) and every timer
        is disarmed. A run that would end STOPPED with an attached consumer
        that has not handled every record waits in STOPPING(drain); the state
        changes to STOPPING only when it was not STOPPING already.
        """
        # Lines held for attribution are released (or discarded and counted)
        # first: they go through where and grouping before the groups are
        # flushed and before the drain check (design A.21).
        self._conclude_held()
        if self._state in TERMINAL:
            return  # where raised on a released line: that failure ended it
        self._flush_groups()
        self._out += self._stderr_log.pending()
        self._conn = None
        self._requests.clear()
        self._attribution.close()
        self._backoff_at = self._stable_at = None
        self._flush = None
        if self._bp_since is not None:
            self._bp_total += self._now - self._bp_since
            self._bp_since = None
        if (
            self._outcome is None
            and not self._kill_requested
            and not self._detached
            and not self._drained()
        ):
            self._enter_stopping(Mode.DRAIN)
            return
        self._end()

    def _drained(self) -> bool:
        """Whether the consumer handled every record the core emitted (A.21).

        A coalesced state change absorbed one record, so it counts as handled.
        Discards never matter: only a kill, a consumer failure and a detach
        discard, and each of them ends the run without a drain.
        """
        counts = self._counts
        emitted = self._emitted + len(self._records)
        return counts.handled + counts.events_coalesced >= emitted

    def _end(self) -> None:
        """Enter the terminal state of G4 and emit Finished."""
        final = (
            S.FAILED
            if self._outcome is not None
            else S.KILLED
            if self._kill_requested
            else S.STOPPED
        )
        self._set_state(final, error=self._outcome)
        self._mode = None
        if self._outcome is not None:
            self._outcome.stats = self.stats()
        self._flush_records()
        self._out.append(Finished(self._outcome))

    # -- STOPPING ----------------------------------------------------------------------

    def _enter_stopping(self, mode: Mode) -> None:
        if self._state is not S.STOPPING:
            self._set_state(S.STOPPING)
        if self._mode is Mode.FLUSH and mode is not Mode.FLUSH:
            self._flush = None
            self._requests = {
                key: value
                for key, value in self._requests.items()
                if value.kind != "flush_marker"
            }
        self._mode = mode

    def _terminate(self, now: float) -> None:
        """Terminate(c) and arm stop_grace, while the child runs."""
        conn = self._conn
        if conn is None or not conn.alive:
            return  # not spawned yet (the spawned row sends it) or exited
        self._out.append(Terminate(conn.id))
        if conn.stop_grace is None:
            conn.stop_grace = now + self._config.stop_grace

    def _kill_child(self) -> None:
        """Kill(c) while the child runs."""
        conn = self._conn
        if conn is None or not conn.alive:
            return  # not spawned yet (the spawned row sends it) or exited
        conn.killed = True
        self._out.append(Kill(conn.id))

    def _end_flush(self, now: float) -> None:
        """STOPPING(flush) -> STOPPING(stop): Terminate(c)."""
        self._enter_stopping(Mode.STOP)
        self._terminate(now)

    def _discard_undelivered(self) -> None:
        self._discard_reason = (
            "kill()"
            if self._mode is Mode.KILL or self._kill_requested
            else "the consumer failed"
        )
        # The estimate of the fill can only be too high, and the control inbox
        # hands over a waiting ConsumerProgress before any request, so it
        # counts every report posted before this one. The report of what the
        # discard removed comes too late for a WARNING when the discard ends
        # the run (G2), though still before Finished is carried out.
        if self._admission.queued_entries:
            self._warn_discard(self._discard_reason)
        self._flush_records()
        self._out.append(DiscardUndelivered())

    def _warn_discard(self, reason: str) -> None:
        if not self._warned_discard:
            self._warned_discard = True
            self._out.append(
                Diagnostic(
                    logging.WARNING,
                    f"discarding undelivered items ({reason}); they are counted in "
                    "stats.undelivered",
                )
            )

    # -- lines -----------------------------------------------------------------------

    def _dropping(self) -> bool:
        """Whether read lines are discarded: STOPPING(kill or fail)."""
        return self._state is S.STOPPING and self._mode in {Mode.KILL, Mode.FAIL}

    def _drop(self, entries: int, reason: str) -> None:
        if entries:
            self._tally.dropped += entries
            self._warn_discard(reason)

    def _lines_of(self, pipe: Pipe, lines: Iterable[FramedLine], now: float) -> None:
        if pipe == "stderr":
            for line in lines:
                self._stderr_line(decode_line(line.data), now)
            return
        for line in lines:
            self._tally.lines += 1
            self._stdout_line(line, now)
        if (
            self._attribution.watching
            and self._state not in TERMINAL
            and not self._dropping()
        ):
            # A line from an unknown app uid asks for a re-lookup (design 5.8).
            self._next_lookup(now)

    def _stdout_line(self, line: FramedLine, now: float) -> None:
        conn = self._conn
        assert conn is not None  # noqa: S101  (stdout lines come from the live one)
        marker = conn.marker
        if marker is not None:
            # Before the start marker nothing belongs to the capture, also
            # while a kill or a failure discards the rest (design A.20).
            self._before_start(conn, marker, line, now)
            return
        if line.truncated:
            self._tally.truncated += 1
        if self._dropping():
            self._drop(1, "kill()" if self._mode is Mode.KILL else "the run failed")
            return
        entry = conn.pipeline.entry(line)
        if entry is None:  # a buffer divider: logcat's own line
            self._tally.filtered += 1
            return
        if not entry.parsed:
            self._tally.unparsed += 1
        self._read_any = True  # a line past the start: later attempts resume
        verdict = self._positioner.screen(entry)
        if verdict == "deduplicated":
            self._tally.deduplicated += 1
            return
        if verdict == "below_cursor":
            self._tally.below_cursor += 1
            return
        self._positioner.observe(entry)
        token = self._flush_token
        if (
            token is not None
            and entry.parsed
            and _is_marker(entry.tag, entry.message, token)
        ):
            self._tally.filtered += 1
            if self._mode is Mode.FLUSH:
                self._end_flush(now)
            return
        self._accept(entry, now)

    def _before_start(
        self, conn: _Connection, marker: str, line: FramedLine, now: float
    ) -> None:
        """since="now": a line up to and including the start marker (design 2.5).

        It counts in ``before_start`` only, a divider, an unparsed or a cut
        line included: none of them belongs to the capture.
        """
        self._tally.before_start += 1
        entry = conn.pipeline.entry(line)
        if entry is None:  # a buffer divider
            return
        # Every line read from stdout moves the resume cursor (design 5.6).
        self._positioner.observe(entry)
        if not entry.parsed or not _is_marker(entry.tag, entry.message, marker):
            return
        conn.marker = None
        self._started = True
        # The marker is read: a later attempt resumes after it instead of
        # starting at its own clock.
        self._read_any = True
        if self._state is S.CONNECTING and not conn.killed:
            self._enter_running(now)

    def _accept(self, entry: LogEntry, now: float) -> None:
        """A line for ``where``: attributed (and maybe held) first when packages
        are watched (design 5.8).
        """
        attribution = self._attribution
        if attribution.watching:
            try:
                verdict = attribution.admit(entry, now, force=self._detached)
            except Exception as exc:  # noqa: BLE001  (fail fast: CallbackError)
                self._stage_failed("where", exc)
                return
            if verdict is None:
                self._held_changed()
                return
            self._take(verdict, now)
            return
        where = self._where
        matched = True
        if where is not None:
            try:
                matched = bool(where(entry))
            except Exception as exc:  # noqa: BLE001  (fail fast: CallbackError)
                self._stage_failed("where", exc)
                return
        self._judged(entry, now, matched=matched, unattributed=False)

    def _take(self, verdict: Verdict, now: float) -> None:
        """A line the attribution let go on, with its verdict."""
        self._judged(
            verdict.entry,
            now,
            matched=verdict.matched,
            unattributed=verdict.unattributed,
        )

    def _judged(
        self, entry: LogEntry, now: float, *, matched: bool, unattributed: bool
    ) -> None:
        """After ``where``: grouping, then the outbox."""
        if self._dropping():
            self._drop(1, "the run failed")
            return
        if unattributed:
            self._tally.unattributed += 1
        if not matched:
            self._tally.filtered += 1
            return
        grouper = self._grouper
        if grouper is None:
            self._emit(entry)
            return
        try:
            groups = grouper.add(entry, now)
        except Exception as exc:  # noqa: BLE001  (fail fast: CallbackError)
            self._stage_failed("group_by", exc)
            return
        for group in groups:
            self._emit(group)

    def _stage_failed(self, stage: CallbackStage, error: Exception) -> None:
        """A where/group_by failure [P]: the line is discarded and the run fails."""
        self._drop(1, f"{stage} raised")
        self._callback_failed(stage, error)

    def _emit(self, item: Item) -> None:
        """Append an item to the outbox records; discard it for a detached consumer."""
        entries, nbytes = item_size(item)
        if self._detached:
            self._drop(entries, "the consumer detached")
            return
        self._admission.admit(entries, nbytes)
        self._records.append(item)

    def _flush_groups(self) -> None:
        """Close the open groups: delivered, or discarded in STOPPING(kill or fail)."""
        grouper = self._grouper
        if grouper is None or not grouper.open_groups:
            return
        groups = grouper.flush()
        if self._dropping():
            self._drop(sum(len(group) for group in groups), "the run ended")
            return
        for group in groups:
            self._emit(group)

    def _release_held(self, now: float, *, force: bool = False) -> None:
        """Held lines that may go on do, in order; or they are discarded.

        ``force``: every held line goes on, what is still unknown evaluated
        without a package (a connection end, a detach, the conclusion). In
        STOPPING(kill or fail), and once where raised on a released line,
        held lines and open groups are discarded and counted.
        """
        attribution = self._attribution
        while attribution.held and self._state not in TERMINAL and not self._dropping():
            try:
                verdict = attribution.release(now, force=force)
            except Exception as exc:  # noqa: BLE001  (fail fast: CallbackError)
                self._held_changed()
                self._stage_failed("where", exc)
                break
            # Before the line reaches the outbox, so that it never counts twice.
            self._held_changed()
            if verdict is None:
                return
            self._take(verdict, now)
        if self._dropping():
            self._discard_pending()

    def _discard_pending(self) -> None:
        """STOPPING(kill or fail): held lines and open groups are discarded, counted."""
        self._drop(self._attribution.discard(), "the run ended")
        self._held_changed()
        self._flush_groups()

    def _conclude_held(self) -> None:
        """The conclusion's part of the hold (design A.21).

        Held lines go on, evaluated without a package where it is still
        unknown, unless the run was killed or failed: then they are discarded
        and counted.
        """
        attribution = self._attribution
        if attribution.held and not (
            self._dropping() or self._kill_requested or self._outcome is not None
        ):
            self._release_held(self._now, force=True)
            if self._state in TERMINAL:
                return
        if attribution.held:
            reason = "kill()" if self._kill_requested else "the run failed"
            self._drop(attribution.discard(), reason)
            self._held_changed()

    def _held_changed(self) -> None:
        """Held lines count against the outbox (design 5.10)."""
        attribution = self._attribution
        self._admission.hold(attribution.held, attribution.held_bytes)

    def _stderr_line(self, text: str, now: float) -> None:
        """Log an adb stderr line, at most 10 per second (design 5.9); keep the tail."""
        self._tally.stderr_lines += 1
        conn = self._conn
        if conn is not None:
            conn.stderr_tail.append(text)
        self._out += self._stderr_log.line(text, now)

    # -- state, latch, records -------------------------------------------------------

    def _set_state(self, new: StreamState, *, error: StreamError | None = None) -> None:
        """Change state: its StateChange (G3); disarm the old state's work (G5)."""
        old = self._state
        if old is new:
            return
        self._state = new
        self._records.append(StateChange(old, new, self._number, error))
        if old is S.CONNECTING:
            self._requests_of_connecting_dropped()
            if self._conn is not None:
                self._conn.connect_at = None
        elif old is S.RUNNING:
            self._stable_at = None
        elif old is S.RECONNECTING:
            self._backoff_at = None
            self._resume_on_intake = False

    def _requests_of_connecting_dropped(self) -> None:
        self._requests = {
            key: value
            for key, value in self._requests.items()
            if value.kind not in _CONNECTING_KINDS
        }

    def _latch(self, error: StreamError) -> None:
        """G4: the first fatal error is kept and never replaced."""
        if self._outcome is None:
            self._outcome = error

    def _callback_error(
        self, stage: CallbackStage, error: BaseException
    ) -> CallbackError:
        result = CallbackError(
            f"{stage} raised {type(error).__name__}: {error}",
            stage=stage,
            stats=self.stats(),
        )
        result.__cause__ = error
        return result

    def _flush_records(self) -> None:
        if self._records:
            self._emitted += len(self._records)
            self._out.append(Deliver(tuple(self._records)))
            self._records = []

    # -- intake and timers -------------------------------------------------------------

    def _gating(self) -> bool:
        """Whether a full outbox closes intake now (not while discarding)."""
        return (
            self._state not in TERMINAL
            and self._state is not S.IDLE
            and self._mode not in {Mode.KILL, Mode.FAIL, Mode.DRAIN}
            and not self._detached
        )

    def _settle(self, now: float) -> None:
        """Follow intake changes: backpressure time, paused timers, the stall resume."""
        pressed = self._gating() and not self._admission.open
        if pressed and self._bp_since is None:
            self._bp_since = now
        elif not pressed and self._bp_since is not None:
            self._bp_total += now - self._bp_since
            self._bp_since = None
        intake = self.intake_open
        if intake != self._intake_was:
            self._intake_was = intake
            timers = [self._flush]
            if self._conn is not None:
                timers.append(self._conn.exit_drain)
            for timer in timers:
                if timer is None:
                    continue
                if intake:
                    timer.resume(now)
                else:
                    timer.pause(now)
            if intake:
                self._reopened_at = now
        if self._state is S.RECONNECTING and self._resume_on_intake and intake:
            # RECONNECTING after a stalled connection: intake reopened.
            self._resume_on_intake = False
            self._tally.reconnects += 1
            self._begin_attempt(now)
            self._settle(now)

    def _deadlines(self) -> dict[_Timer, float]:
        """Every armed timer of the current state (G5)."""
        found: dict[_Timer, float | None] = {}
        state = self._state
        if self.mode is Mode.DRAIN:
            return {}  # A.21: the data side ended; only the consumer is waited for
        config = self._config
        conn = self._conn
        usable = False  # a live adb that the core has not killed
        if conn is not None:
            if state is S.CONNECTING:
                found["connect"] = conn.connect_at
            if conn.exit_drain is not None:
                found["exit_drain"] = conn.exit_drain.deadline()
            if state is S.STOPPING:
                found["stop_grace"] = conn.stop_grace
            usable = conn.alive and not conn.killed
            if (
                state is S.RUNNING
                and usable
                and config.read_timeout is not None
                and self.intake_open
            ):
                start = max(conn.last_activity, self._reopened_at)
                found["read"] = start + config.read_timeout
            stall_state = state is S.RUNNING or self._mode is Mode.FLUSH
            if (
                stall_state
                and usable
                and config.stall_timeout is not None
                and self._bp_since is not None
            ):
                found["stall"] = self._bp_since + config.stall_timeout
        if state is S.RECONNECTING:
            found["backoff"] = self._backoff_at
        if state is S.RUNNING and usable:
            # A connection whose adb exited or was killed is ending: it does
            # not reset the reconnect budget, however long its end takes.
            found["stable"] = self._stable_at
        if self._mode is Mode.FLUSH and self._flush is not None:
            found["flush"] = self._flush.deadline()
        if state not in TERMINAL and state is not S.IDLE:
            found["hold"] = self._attribution.deadline()
            if self._grouper is not None and state is not S.CONNECTING:
                found["idle"] = self._grouper.idle_deadline()
            # Not a table event: the count of rate-limited stderr lines is
            # logged when their window ends (design 5.9).
            found["stderr_report"] = self._stderr_log.due()
        return {name: when for name, when in found.items() if when is not None}

    def _fire_due(self, now: float) -> None:
        """Fire every timer due at ``now``, earliest first."""
        fired: set[tuple[_Timer, float]] = set()
        while self._state not in TERMINAL:
            due = [
                (when, name) for name, when in self._deadlines().items() if when <= now
            ]
            if not due:
                return
            when, name = min(due)
            if (name, when) in fired:
                msg = f"timer {name} did not disarm after firing at {when}"
                raise AssertionError(msg)
            fired.add((name, when))
            self._fire(name, now)
            self._settle(now)

    def _fire(self, name: _Timer, now: float) -> None:
        match name:
            case "connect" | "read" | "exit_drain" | "stop_grace" | "stall":
                conn = self._conn
                assert conn is not None  # noqa: S101  (connection timers need one)
                self._fire_connection_timer(name, conn, now)
            case "backoff":
                self._backoff_at = None
                self._attempts += 1
                reconnect = self._config.reconnect
                assert reconnect is not None  # noqa: S101  (backoff needs it)
                self._delay = min(reconnect.max_delay, self._delay * reconnect.backoff)
                self._tally.reconnects += 1
                self._begin_attempt(now)
            case "stable":
                self._stable_at = None
                self._attempts = 0
                reconnect = self._config.reconnect
                assert reconnect is not None  # noqa: S101  (stable_after needs it)
                self._delay = reconnect.delay
            case "flush":
                self._tally.flush_timeout = True
                self._end_flush(now)
            case "hold":
                self._attribution.expire(now)
                self._attribution_changed(now)
            case "idle":
                assert self._grouper is not None  # noqa: S101  (idle needs groups)
                if self._dropping():
                    self._discard_pending()
                else:
                    for group in self._grouper.flush_idle(now):
                        self._emit(group)
            case "stderr_report":
                self._out += self._stderr_log.pending()
            case _:
                assert_never(name)

    def _fire_connection_timer(
        self,
        name: Literal["connect", "read", "exit_drain", "stop_grace", "stall"],
        conn: _Connection,
        now: float,
    ) -> None:
        match name:
            case "connect":
                # CONNECTING without output for connect_timeout: is the device there?
                conn.connect_at = None
                self._run(
                    "state",
                    _get_state_argv(self._adb, self._config.serial),
                    connection=conn.id,
                )
            case "read":
                seconds = self._config.read_timeout
                conn.cause = (
                    "timeout",
                    f"no output on stdout or stderr for {seconds:g} s",
                )
                self._kill_child()
            case "exit_drain":
                self._drain_expired(conn)
                self._connection_ended(now)
            case "stop_grace":
                conn.stop_grace = None
                self._kill_child()
            case "stall":
                self._stalled(conn, now)
            case _:
                assert_never(name)

    def _drain_expired(self, conn: _Connection) -> None:
        """Log a connection that exit_drain ends with a pipe open (design 5.10)."""
        still_open = [pipe for pipe in _PIPES if pipe not in conn.eof]
        pipes = " and ".join(still_open)
        noun = "pipe" if len(still_open) == 1 else "pipes"
        self._out.append(
            Diagnostic(
                logging.WARNING,
                f"logcat connection {conn.id}: adb exited, but its {pipes} {noun} "
                f"stayed open for exit_drain ({self._config.exit_drain:g} s), "
                "probably held by a process adb started; the rest of its output "
                "is ignored",
            )
        )

    def _stalled(self, conn: _Connection, now: float) -> None:
        """Intake stayed closed for stall_timeout (design 5.6, A-19)."""
        if self._mode is Mode.FLUSH:
            # A stalled reader is not resumed while stopping.
            self._tally.flush_failed = True
            self._end_flush(now)
            return
        # Held lines count against the outbox (design 5.10), so they alone can
        # keep intake closed: the INFO at the connection's end says which.
        conn.stalled = "attribution" if self._admission.held_back() else "consumer"
        self._tally.stall_resumes += 1
        self._kill_child()


def _success(result: _AdbResult | Exception) -> _AdbResult:
    """The result of a one-shot command, or raise what the runner raised.

    Raises:
        AdbError: The runner raised it.
        TimeoutError: The runner raised it.

    """
    if isinstance(result, AdbError | TimeoutError):
        raise result
    if isinstance(result, Exception):
        msg = f"one-shot command failed: {type(result).__name__}: {result}"
        raise AdbError(msg, argv=(), returncode=None) from result
    return result
