"""Scenarios for the state x event product test and the random event sequences.

`VARIANTS` builds, for every state of design 3.1 (STOPPING split by mode),
the cores that the product test probes: one per sub-context an event needs
(a pending clock read, a pending spawn, an exited child, a full outbox...).
`stimulus` turns an event kind of the transition table into the event that
probes it. `RandomDriver` plays a plausible driver against a core.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from logpyt import GroupBy, Package, Tag, Where
from logpyt._core.protocol import (
    CallbackFailed,
    CommandDone,
    ConsumerDetached,
    ConsumerProgress,
    Data,
    Eof,
    Exited,
    Finished,
    Kill,
    KillRequest,
    RunCommand,
    Spawn,
    Spawned,
    SpawnFailed,
    Start,
    StopRequest,
    Terminate,
    Tick,
)
from logpyt.adb import _MARKER_TAG
from logpyt.config import Reconnect
from logpyt.streams._events import StateChange, StreamState
from tests.core.harness import (
    APP,
    CLOCK_FAILED,
    CLOCK_OK,
    LOOKUP_FAILED,
    LOOKUP_OK,
    MARKER_FAILED,
    MARKER_OK,
    STATE_DEVICE,
    STATE_OFFLINE,
    Harness,
    line,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from logpyt._core.protocol import Command, CommandKind, Event, Pipe
    from logpyt.adb import _AdbResult
    from logpyt.models import LogEntry

S = StreamState

STATES = (
    "IDLE",
    "CONNECTING",
    "RUNNING",
    "RECONNECTING",
    "STOPPING(stop)",
    "STOPPING(flush)",
    "STOPPING(kill)",
    "STOPPING(fail)",
    "STOPPED",
    "KILLED",
    "FAILED",
)
CONTROL = ("start", "stop", "stop_flush", "kill")
CONNECTION = ("spawned", "spawn_failed", "data_stdout", "data_stderr", "eof", "exited")
RESULTS = (
    "clock_done",
    "clock_failed",
    "marker_done",
    "marker_failed",
    "state_device",
    "state_other",
    "lookup_done",
    "flush_marker_done",
    "flush_marker_failed",
)
CONSUMER = ("consumer_progress", "consumer_detached", "callback_failed")
TIMERS = {
    "connect_timeout": "connect",
    "read_timeout": "read",
    "exit_drain": "exit_drain",
    "stop_grace": "stop_grace",
    "backoff_due": "backoff",
    "stable_after": "stable",
    "stall": "stall",
    "flush_timer": "flush",
    "hold_expired": "hold",
    "idle_flush": "idle",
}
KINDS = (*CONTROL, *CONNECTION, *RESULTS, *CONSUMER, *TIMERS)

_RESULT: dict[str, tuple[CommandKind, _AdbResult]] = {
    "clock_done": ("clock", CLOCK_OK),
    "clock_failed": ("clock", CLOCK_FAILED),
    "marker_done": ("start_marker", MARKER_OK),
    "marker_failed": ("start_marker", MARKER_FAILED),
    "state_device": ("state", STATE_DEVICE),
    "state_other": ("state", STATE_OFFLINE),
    "lookup_done": ("lookup", LOOKUP_OK),
    "flush_marker_done": ("flush_marker", MARKER_OK),
    "flush_marker_failed": ("flush_marker", MARKER_FAILED),
}

# The rows of design 3.2 per state, as event kinds. G6: consumer_progress
# always updates admission, so it is a row wherever the run is live.
_STOPPING_ROWS = frozenset(
    {
        "stop",
        "stop_flush",
        "kill",
        "spawned",
        "spawn_failed",
        "data_stdout",
        "data_stderr",
        "eof",
        "exited",
        "lookup_done",
        "consumer_progress",
        "consumer_detached",
        "callback_failed",
        "exit_drain",
        "stop_grace",
        "hold_expired",
        "idle_flush",
    }
)
ROWS: dict[str, frozenset[str]] = {
    "IDLE": frozenset(CONTROL),
    "CONNECTING": frozenset(
        {
            "stop",
            "stop_flush",
            "kill",
            "spawned",
            "spawn_failed",
            "data_stdout",
            "data_stderr",
            "eof",
            "exited",
            "clock_done",
            "clock_failed",
            "marker_done",
            "marker_failed",
            "state_device",
            "state_other",
            "lookup_done",
            "consumer_progress",
            "consumer_detached",
            "callback_failed",
            "connect_timeout",
            "exit_drain",
            "hold_expired",
        }
    ),
    "RUNNING": frozenset(
        {
            "stop",
            "stop_flush",
            "kill",
            "data_stdout",
            "data_stderr",
            "eof",
            "exited",
            "lookup_done",
            "consumer_progress",
            "consumer_detached",
            "callback_failed",
            "read_timeout",
            "exit_drain",
            "stable_after",
            "stall",
            "hold_expired",
            "idle_flush",
        }
    ),
    "RECONNECTING": frozenset(
        {
            "stop",
            "stop_flush",
            "kill",
            "lookup_done",
            "consumer_progress",
            "consumer_detached",
            "callback_failed",
            "backoff_due",
            "hold_expired",
            "idle_flush",
        }
    ),
    "STOPPING(stop)": _STOPPING_ROWS,
    "STOPPING(flush)": (_STOPPING_ROWS - {"spawned", "spawn_failed"})
    | {"flush_marker_done", "flush_marker_failed", "flush_timer", "stall"},
    "STOPPING(kill)": _STOPPING_ROWS,
    "STOPPING(fail)": _STOPPING_ROWS,
    "STOPPED": frozenset({"consumer_progress", "consumer_detached"}),
    "KILLED": frozenset({"consumer_progress", "consumer_detached"}),
    "FAILED": frozenset({"consumer_progress", "consumer_detached"}),
}
_LIVE = ("CONNECTING", "RUNNING", "RECONNECTING", *(s for s in STATES if "(" in s))
# Cells that fail an assertion in the core: start() outside IDLE (the driver
# refuses it first), and a spawn result for a connection that already runs.
IMPOSSIBLE: frozenset[tuple[str, str]] = frozenset(
    {(state, "start") for state in _LIVE}
    | {
        ("RUNNING", "spawned"),
        ("RUNNING", "spawn_failed"),
        ("STOPPING(flush)", "spawned"),
        ("STOPPING(flush)", "spawn_failed"),
    }
)
# Rows of the table whose timer cannot be armed in that state: groups are
# flushed when a connection ends, so RECONNECTING holds none; STOPPING(flush)
# has not sent Terminate, so stop_grace is not armed yet.
UNREACHABLE: frozenset[tuple[str, str]] = frozenset(
    {("RECONNECTING", "idle_flush"), ("STOPPING(flush)", "stop_grace")}
)

WHERE = Package(APP) | Tag("App")


def _make(**fields: object) -> Harness:
    settings: dict[str, object] = {
        "reconnect": Reconnect(),
        "read_timeout": 5.0,
        "stall_timeout": 1.0,
        "package_hold": 60.0,
    }
    settings.update(fields)
    return Harness.make(
        where=WHERE,
        group_by=GroupBy(
            keys=(), gap_ms=60_000.0, idle_flush_ms=500.0, max_group_size=2
        ),
        **settings,
    )


def _connecting(stage: str, **fields: object) -> Harness:
    """CONNECTING at ``stage``: clock, marker, spawn, spawned, state, exited."""
    harness = _make(since="now" if stage == "marker" else "backlog", **fields)
    harness.start()
    if stage == "clock":
        return harness
    harness.answer("lookup", LOOKUP_OK)
    harness.answer("clock", CLOCK_OK)
    if stage in {"marker", "spawn"}:
        return harness
    harness.spawned()
    harness.stderr(b"- waiting for device -\n")
    if stage == "state":
        harness.until_deadline()
    elif stage == "exited":
        harness.exited(1)
    return harness


def _running(stage: str) -> Harness:
    """RUNNING: plain, full outbox, exited child, or lookup still pending."""
    if stage == "lookup":
        harness = _make()
        harness.start()
        harness.answer("clock", CLOCK_OK)
        harness.spawned()
        harness.until_deadline()  # connect_timeout: the state check
        harness.answer("state", STATE_DEVICE)  # a quiet device: RUNNING
        return harness
    harness = _make(buffer_entries=2) if stage == "full" else _make()
    harness.start()
    harness.answer("lookup", LOOKUP_OK)
    harness.answer("clock", CLOCK_OK)
    harness.spawned()
    harness.stdout(line("first"))
    if stage == "full":
        harness.stdout(line("second"))
    elif stage == "exited":
        harness.exited(0)
    return harness


def _reconnecting(stage: str) -> Harness:
    if stage == "stall":
        harness = _running("full")
        harness.until_deadline()  # stall: Kill(c)
        harness.end_connection(-9)
        return harness
    harness = _running("lookup") if stage == "lookup" else _running("plain")
    harness.end_connection(1)
    return harness


def _request(mode: str) -> Event:
    return {
        "stop": StopRequest(),
        "flush": StopRequest(flush=5.0),
        "kill": KillRequest(),
        "fail": CallbackFailed("on_item", ValueError("boom")),
    }[mode]


def _stopping(mode: str, stage: str) -> Harness:
    """STOPPING(mode) reached from ``stage``."""
    if stage == "spawn":
        harness = _connecting("spawn")
    elif stage == "graceful":
        harness = _running("plain")
        harness.send(StopRequest())  # Terminate: stop_grace armed
    else:
        harness = _running(stage)
    harness.send(_request(mode))
    if stage == "exited-after":
        harness.exited(0)
    return harness


def _terminal(state: str) -> Harness:
    harness = _running("plain")
    harness.send(
        _request({"STOPPED": "stop", "KILLED": "kill", "FAILED": "fail"}[state])
    )
    harness.end_connection(0)
    return harness


VARIANTS: dict[str, tuple[Callable[[], Harness], ...]] = {
    "IDLE": (_make,),
    "CONNECTING": tuple(
        (lambda stage=stage: _connecting(stage))
        for stage in ("clock", "marker", "spawn", "spawned", "state", "exited")
    ),
    "RUNNING": tuple(
        (lambda stage=stage: _running(stage))
        for stage in ("plain", "full", "exited", "lookup")
    ),
    "RECONNECTING": tuple(
        (lambda stage=stage: _reconnecting(stage))
        for stage in ("error", "lookup", "stall")
    ),
    **{
        f"STOPPING({mode})": tuple(
            (lambda mode=mode, stage=stage: _stopping(mode, stage)) for stage in stages
        )
        for mode, stages in {
            "stop": ("plain", "spawn", "exited", "lookup"),
            "flush": ("plain", "full", "exited", "lookup"),
            "kill": ("plain", "spawn", "exited", "lookup", "graceful"),
            "fail": ("plain", "spawn", "exited", "lookup", "graceful"),
        }.items()
    },
    **{
        state: ((lambda state=state: _terminal(state)),)
        for state in ("STOPPED", "KILLED", "FAILED")
    },
}


def state_key(harness: Harness) -> str:
    """The product test's name of the core's state."""
    mode = harness.core.mode
    state = harness.core.state
    return f"{state}({mode})" if mode is not None else str(state)


def stimulus(harness: Harness, kind: str) -> tuple[Event, bool, float | None]:
    """The event that probes ``kind``, whether it applies here, and its time.

    An event that does not apply is the form a driver could still deliver
    late: an event of a retired connection, a result nobody waits for.
    """
    core = harness.core
    conn = core._conn
    connection = conn.id if conn is not None else 1
    if kind in TIMERS:
        deadline = dict(core._deadlines()).get(TIMERS[kind])
        return Tick(), deadline is not None, deadline
    if kind in _RESULT:
        command_kind, outcome = _RESULT[kind]
        pending = [cmd for cmd in harness.pending.values() if cmd.kind == command_kind]
        if pending:
            return CommandDone(pending[-1].request, outcome), True, None
        issued = [
            cmd.request
            for cmd in harness.commands
            if isinstance(cmd, RunCommand) and cmd.kind == command_kind
        ]
        return CommandDone(issued[-1] if issued else 10_000, outcome), False, None
    spawn_pending = conn is not None and conn.spawn_issued and conn.pid is None
    spawned = conn is not None and conn.pid is not None
    events: dict[str, tuple[Event, bool]] = {
        "start": (Start(), True),
        "stop": (StopRequest(), True),
        "stop_flush": (StopRequest(flush=5.0), True),
        "kill": (KillRequest(), True),
        "consumer_progress": (ConsumerProgress(harness.outbox.counts()), True),
        "consumer_detached": (ConsumerDetached(harness.outbox.counts()), True),
        "callback_failed": (CallbackFailed("on_event", ValueError("x")), True),
        "spawned": (Spawned(connection, 77), spawn_pending),
        "spawn_failed": (SpawnFailed(connection, OSError("x")), spawn_pending),
        "data_stdout": (Data(connection, "stdout", line("probe")), spawned),
        "data_stderr": (Data(connection, "stderr", b"probe\n"), spawned),
        "eof": (Eof(connection, "stdout"), spawned),
        "exited": (Exited(connection, 0), conn is not None and conn.alive),
    }
    event, applies = events[kind]
    return event, applies, None


# -- random sequences --------------------------------------------------------------

# The transitions of design 3.2, as (old, new) state pairs.
TRANSITIONS = frozenset(
    {
        (S.IDLE, S.CONNECTING),
        (S.CONNECTING, S.RUNNING),
        (S.CONNECTING, S.RECONNECTING),
        (S.CONNECTING, S.STOPPING),
        (S.CONNECTING, S.STOPPED),
        (S.CONNECTING, S.KILLED),
        (S.CONNECTING, S.FAILED),
        (S.RUNNING, S.STOPPING),
        (S.RUNNING, S.RECONNECTING),
        (S.RUNNING, S.FAILED),
        (S.RECONNECTING, S.CONNECTING),
        (S.RECONNECTING, S.STOPPED),
        (S.RECONNECTING, S.KILLED),
        (S.RECONNECTING, S.FAILED),
        (S.STOPPING, S.STOPPED),
        (S.STOPPING, S.KILLED),
        (S.STOPPING, S.FAILED),
    }
)
TERMINAL = frozenset({S.STOPPED, S.KILLED, S.FAILED})


class RandomDriver:
    """Plays a plausible driver against a core, from one seed.

    It answers commands with random results, feeds random output (whole and
    split lines, dividers, garbage, oversized lines, the marker echoes) only
    while intake is open, takes records as a consumer at random, issues stop,
    kill, detach and callback failures at random, and moves the clock. After
    every step it checks the global rules (see `step`).
    """

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)  # noqa: S311  (a test, not cryptography)
        rng = self.rng
        where = None
        self.raising = rng.random() < 0.3
        if rng.random() < 0.7:
            where = Where(self._where) if self.raising else WHERE
        buffer_entries = rng.choice([1, 3, 50])
        group_by = None
        if rng.random() < 0.5:
            group_by = GroupBy(
                keys=rng.choice([(), ("tag",), ("pid",)]),
                gap_ms=rng.choice([1.0, 50.0, 5000.0]),
                idle_flush_ms=rng.choice([None, 100.0, "gap"]),
                max_group_size=rng.choice(
                    [1, 3, 50][: [1, 3, 50].index(buffer_entries) + 1]
                ),
            )
        self.queue: list[bytes] = []  # stdout bytes read but not yet handed over
        self.queue_of: int | None = None
        self.harness = Harness.make(
            where=where,
            group_by=group_by,
            since=rng.choice(["backlog", "now"]),
            reconnect=rng.choice(
                [None, Reconnect(max_attempts=5, delay=0.2, stable_after=1.0)]
            ),
            read_timeout=rng.choice([None, 0.5, 4.0]),
            stall_timeout=rng.choice([None, 0.5]),
            buffer_entries=buffer_entries,
            max_line_bytes=4096,
            package_hold=rng.choice([0.0, 1.0]),
            exit_drain=0.5,
            stop_grace=0.5,
        )
        self.retired: set[int] = set()
        self.outcome: object = None
        self.changes: list[StateChange] = []
        self.finished_at: int | None = None
        self.steps = 0

    def _where(self, entry: LogEntry) -> bool:
        if entry.message == "boom":
            raise RuntimeError("where raised")
        return entry.tag != "Other"

    # -- one step --------------------------------------------------------------------

    def run(self, steps: int) -> None:
        """Play ``steps`` random steps, then end the run and drain the consumer."""
        self.send(Start())
        for _ in range(steps):
            if self.harness.core.state in TERMINAL:
                break
            self.step()
        self.finish()

    def send(self, event: Event, at: float | None = None) -> list[Command]:
        """Hand one event to the core and check the global rules on the result."""
        harness = self.harness
        before = harness.core.state
        live_before = harness.core.connection
        issued = len(harness.commands)
        out = harness.send(event, at=at)
        # With the harness's own reports of the outbox counts after ``out``.
        out = harness.commands[issued:]
        self.steps += 1
        if before in TERMINAL:
            assert out == [], "a terminal core returned commands (G2)"
        for command in out:
            if isinstance(command, Spawn | Terminate | Kill):
                assert command.connection not in self.retired, (
                    f"{command} for a retired connection (G1)"
                )
        if any(isinstance(command, Finished) for command in out):
            assert self.finished_at is None, "a second Finished"
            self.finished_at = self.steps
            assert isinstance(out[-1], Finished), "Finished is not the last command"
        live = harness.core.connection
        if live_before is not None and live != live_before:
            self.retired.add(live_before)
        outcome = harness.core.outcome
        if self.outcome is None:
            self.outcome = outcome
        assert outcome is self.outcome, "the latched outcome changed (G4)"
        new = harness.changes()[len(self.changes) :]
        for change in new:
            assert (change.old, change.new) in TRANSITIONS, f"not a table row: {change}"
            previous = self.changes[-1].new if self.changes else S.IDLE
            assert change.old is previous, f"broken chain: {previous} then {change}"
            self.changes.append(change)
        return out

    def step(self) -> None:
        """One random action a driver could take now."""
        rng = self.rng
        harness = self.harness
        core = harness.core
        conn = core._conn
        weighted: list[tuple[float, Callable[[], object]]] = [
            (2.0, self.advance),
            (3.0, self.consume),
            (0.15, self.control),
        ]
        if harness.pending:
            weighted.append((3.0, self.answer))
        if conn is not None and conn.spawn_issued and conn.pid is None:
            weighted.append((3.0, self.spawn))
        if conn is not None and conn.pid is not None and core.intake_open:
            weighted += [(8.0, self.output), (0.2, self.close_pipe)]
        if conn is not None and conn.alive:
            weighted.append((0.25, self.exit))
        actions = [action for _, action in weighted]
        rng.choices(actions, weights=[weight for weight, _ in weighted])[0]()

    def advance(self) -> None:
        step = self.rng.choice([0.01, 0.05, 0.1, 0.3, 0.6, 2.0])
        self.send(Tick(), at=self.harness.now + step)

    def consume(self) -> None:
        """The consumer takes up to four records and reports its progress."""
        outbox = self.harness.outbox
        if outbox.detached:
            return
        for _ in range(self.rng.randint(0, 4)):
            if outbox.pop() is None:
                break
        self.harness.reported = outbox.counts()
        self.send(ConsumerProgress(outbox.counts()))

    def answer(self) -> None:
        rng = self.rng
        command = rng.choice(list(self.harness.pending.values()))
        del self.harness.pending[command.request]
        # The first attempt's clock and start marker rarely fail, so that few
        # runs end before they read anything.
        first = self.harness.core.stats().connections == 1 and command.kind in {
            "clock",
            "start_marker",
        }
        good = rng.random() < (0.97 if first else 0.85)
        outcome: _AdbResult | Exception
        match command.kind:
            case "clock":
                outcome = CLOCK_OK if good else CLOCK_FAILED
            case "state":
                outcome = STATE_DEVICE if good else STATE_OFFLINE
            case "lookup":
                outcome = LOOKUP_OK if good else LOOKUP_FAILED
            case _:
                outcome = MARKER_OK if good else TimeoutError("marker")
        self.send(CommandDone(command.request, outcome))

    def spawn(self) -> None:
        connection = self.harness.core.connection
        assert connection is not None
        if self.rng.random() < 0.02:
            self.send(SpawnFailed(connection, FileNotFoundError("adb")))
        else:
            self.send(Spawned(connection, 1000 + connection))

    def output(self) -> None:
        """Read random output: stderr lines, or stdout lines split anywhere."""
        rng = self.rng
        core = self.harness.core
        conn = core._conn
        assert conn is not None
        if self.queue_of != conn.id:
            self.queue, self.queue_of = [], conn.id
        open_pipes = [pipe for pipe in ("stdout", "stderr") if pipe not in conn.eof]
        if not open_pipes:
            return
        pipe = rng.choice(open_pipes)
        if pipe == "stderr":
            self.send(Data(conn.id, "stderr", b"adb: note\n" * rng.randint(1, 15)))
            return
        if "stdout" in conn.eof:
            return
        if not self.queue:
            chunk = b"".join(self._line() for _ in range(rng.randint(1, 6)))
            markers = [m for m in (conn.marker, core._flush_token) if m is not None]
            if markers and rng.random() < 0.3:
                chunk += line(rng.choice(markers), tag=_MARKER_TAG)
            cut = rng.randint(1, len(chunk))
            self.queue = [part for part in (chunk[:cut], chunk[cut:]) if part]
        # Like a driver's data inbox: in order, and only while intake is open.
        while self.queue and core._conn is conn and core.intake_open:
            self.send(Data(conn.id, "stdout", self.queue.pop(0)))

    def _line(self) -> bytes:
        rng = self.rng
        pick = rng.random()
        if pick < 0.05:
            return b"--------- beginning of main\n"
        if pick < 0.1:
            return b"garbage that is not threadtime\n"
        if pick < 0.12:
            return line("x" * 5000)
        if pick < 0.125 and self.raising:
            return line("boom")
        tag = rng.choice(["App", "App", "Other"])
        return line(f"m{rng.randint(0, 99)}", tag=tag, pid=rng.choice([1, 2]))

    def close_pipe(self) -> None:
        conn = self.harness.core._conn
        assert conn is not None
        open_pipes: list[Pipe] = [
            pipe for pipe in ("stdout", "stderr") if pipe not in conn.eof
        ]
        if self.queue and self.queue_of == conn.id:
            open_pipes = [pipe for pipe in open_pipes if pipe != "stdout"]
        if open_pipes:
            self.send(Eof(conn.id, self.rng.choice(open_pipes)))

    def exit(self) -> None:
        connection = self.harness.core.connection
        assert connection is not None
        self.send(Exited(connection, self.rng.choice([0, 1, -9])))

    def control(self) -> None:
        rng = self.rng
        outbox = self.harness.outbox
        event = rng.choice(
            [
                StopRequest(),
                StopRequest(flush=rng.choice([0.2, 2.0])),
                KillRequest(),
                None,  # the consumer detaches
                CallbackFailed("on_item", ValueError("callback")),
            ]
        )
        if event is None:
            outbox.detach()
            self.harness.reported = outbox.counts()
            event = ConsumerDetached(outbox.counts())
        self.send(event)

    # -- the end -----------------------------------------------------------------

    def finish(self) -> None:
        """Stop, let every connection end, and drain the consumer."""
        harness = self.harness
        core = harness.core
        if core.state not in TERMINAL:
            self.send(StopRequest())
        for _ in range(200):
            if core.state in TERMINAL:
                break
            conn = core._conn
            if conn is not None and conn.spawn_issued and conn.pid is None:
                self.spawn()
            elif conn is not None and conn.alive:
                self.exit()
            elif conn is not None and conn.pid is not None and core.intake_open:
                if self.queue and self.queue_of == conn.id:
                    self.output()
                self.close_pipe()
            elif harness.pending:
                self.answer()
            else:
                self.consume()
                self.send(Tick(), at=harness.now + 1.0)
        assert core.state in TERMINAL, f"the run did not end: {state_key(harness)}"
        while not harness.outbox.ended and not harness.outbox.detached:
            self.consume()
