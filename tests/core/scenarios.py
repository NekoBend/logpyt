"""Scenarios for the state x event product test and the random event sequences.

`VARIANTS` builds, for every state of design 3.1 (STOPPING split by mode, the
``drain`` mode of design A.21 included),
the cores that the product test probes: one per sub-context an event needs
(a pending clock read, a pending spawn, an exited child, a full outbox, a line
held for package attribution with its re-lookup in flight...).
`stimulus` turns an event kind of the transition table into the event that
probes it. `RandomDriver` plays a plausible driver against a core.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from logpyt import GroupBy, Package, Tag, Where
from logpyt._core.machine import Mode
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
from logpyt.exceptions import CallbackError
from logpyt.models import LogEntry, LogGroup
from logpyt.streams._events import StateChange, StreamState
from tests.core.harness import (
    APP,
    APP_UID,
    CLOCK_FAILED,
    CLOCK_OK,
    LOOKUP_ABSENT,
    LOOKUP_FAILED,
    LOOKUP_OK,
    MARKER_FAILED,
    MARKER_OK,
    STAMP,
    STATE_DEVICE,
    STATE_OFFLINE,
    Harness,
    line,
    lookup,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from logpyt._core.protocol import Command, CommandKind, Event, Pipe, Record
    from logpyt.adb import _AdbResult

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
    "STOPPING(drain)",
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
    "lookup_failed",
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
    "lookup_failed": ("lookup", LOOKUP_FAILED),
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
        "lookup_failed",
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
            "lookup_failed",
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
            "lookup_failed",
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
            "lookup_failed",
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
    # Design A.21: the data side ended; only requests and the consumer count.
    "STOPPING(drain)": frozenset(
        {
            "stop",
            "stop_flush",
            "kill",
            "consumer_progress",
            "consumer_detached",
            "callback_failed",
        }
    ),
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
    """RUNNING: plain, full outbox, exited child, lookup still pending, or a
    line held for attribution with its re-lookup in flight.
    """
    if stage == "held":
        harness = _make()
        harness.start()
        harness.answer("lookup", LOOKUP_ABSENT)  # APP not installed at start
        harness.answer("clock", CLOCK_OK)
        harness.spawned()
        harness.stdout(line("first", uid=1000))  # RUNNING; a system uid
        harness.stdout(line("held", tag="Other"))  # its verdict needs APP
        harness.until_deadline()  # the re-lookup starts (and the idle flush)
        assert harness.core._attribution.held == 1
        assert "lookup" in {cmd.kind for cmd in harness.pending.values()}
        return harness
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
    harness = _running(stage if stage in {"lookup", "held"} else "plain")
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


def _draining(stage: str) -> Harness:
    """STOPPING(drain): records wait for the consumer after the data side ended."""
    if stage == "connecting":
        harness = _connecting("clock")  # no child: CONNECTING -> STOPPING
    elif stage == "reconnecting":
        harness = _reconnecting("error")  # RECONNECTING -> STOPPING
    elif stage == "spawn_failed":
        harness = _connecting("spawn")  # STOPPING(stop) before Spawned
    else:
        harness = _running("plain")
    harness.send(StopRequest(flush=5.0) if stage == "ended-flush" else StopRequest())
    if stage == "spawn_failed":
        harness.spawn_failed()
    elif stage.startswith("ended"):
        harness.end_connection(0)
    # The conclusion abandoned every pending command (G5): a probe answers
    # them only in their late form.
    waited = harness.core._requests
    harness.pending = {
        key: cmd for key, cmd in harness.pending.items() if key in waited
    }
    return harness


def _terminal(state: str) -> Harness:
    harness = _running("plain")
    harness.send(
        _request({"STOPPED": "stop", "KILLED": "kill", "FAILED": "fail"}[state])
    )
    harness.end_connection(0)
    if state == "STOPPED":
        harness.consume()  # design A.21: the drain ends once all is handled
    return harness


VARIANTS: dict[str, tuple[Callable[[], Harness], ...]] = {
    "IDLE": (_make,),
    "CONNECTING": tuple(
        (lambda stage=stage: _connecting(stage))
        for stage in ("clock", "marker", "spawn", "spawned", "state", "exited")
    ),
    "RUNNING": tuple(
        (lambda stage=stage: _running(stage))
        for stage in ("plain", "full", "exited", "lookup", "held")
    ),
    "RECONNECTING": tuple(
        (lambda stage=stage: _reconnecting(stage))
        for stage in ("error", "lookup", "stall", "held")
    ),
    **{
        f"STOPPING({mode})": tuple(
            (lambda mode=mode, stage=stage: _stopping(mode, stage)) for stage in stages
        )
        for mode, stages in {
            "stop": ("plain", "spawn", "exited", "lookup", "held"),
            "flush": ("plain", "full", "exited", "lookup", "held"),
            "kill": ("plain", "spawn", "exited", "lookup", "graceful", "held"),
            "fail": ("plain", "spawn", "exited", "lookup", "graceful", "held"),
        }.items()
    },
    "STOPPING(drain)": tuple(
        (lambda stage=stage: _draining(stage))
        for stage in (
            "ended-stop",
            "ended-flush",
            "spawn_failed",
            "reconnecting",
            "connecting",
        )
    ),
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
        (S.RECONNECTING, S.STOPPING),  # design A.21: the drain
        (S.RECONNECTING, S.STOPPED),
        (S.RECONNECTING, S.KILLED),
        (S.RECONNECTING, S.FAILED),
        (S.STOPPING, S.STOPPED),
        (S.STOPPING, S.KILLED),
        (S.STOPPING, S.FAILED),
    }
)
TERMINAL = frozenset({S.STOPPED, S.KILLED, S.FAILED})


# Package attribution (design 5.8): the uids of the random lines (APP's, another
# app's, APP's on a secondary user, system uids), and a lookup that names
# another uid for APP (an install during the capture).
_UIDS: tuple[int | str, ...] = (APP_UID, APP_UID, 10_070, 1_010_065, 1000, "root")
# Line times: STAMP mostly, a microsecond later, a second later, 3 s earlier.
_STAMPS = (
    STAMP,
    STAMP,
    STAMP,
    STAMP.replace(".661446", ".661447"),
    STAMP.replace(":16.", ":17."),
    STAMP.replace(":16.", ":13."),
)
OTHER_LOOKUP = lookup(10_070)
# The other watched packages of some runs, so that re-lookups of several
# packages take turns (X-3) and packages share a uid; a lookup answers one of
# these uids (None: not installed) for the package it asked for. An answer of
# 10_070 for a package mapped to APP_UID is a reinstall under a new uid.
OTHER_APP = "com.example.other"
THIRD_APP = "com.example.third"
_ANSWER_UIDS = (APP_UID, None, None, 10_070)
_LOOKUP_ANSWERS: dict[str, tuple[_AdbResult, ...]] = {
    package: tuple(lookup(uid, package) for uid in _ANSWER_UIDS)
    for package in (APP, OTHER_APP, THIRD_APP)
}

# How likely a consumer callback raises while the run drains (design A.21),
# per record and on the last item: high enough that the seeds exercise both.
# DRAIN_ODDS also holds for the events a KILLED run leaves the consumer, so
# that late failures after a kill occur (StreamCore.late_failure).
DRAIN_ODDS = 0.2
LAST_ODDS = 0.7
# The weight of kill() among a step's actions while the run drains, so that
# the seeds reach the drain's kill row as well as its consumer exits.
DRAIN_KILL_WEIGHT = 0.6


class RandomDriver:
    """Plays a plausible driver against a core, from one seed.

    It answers commands with random results, feeds random output (whole and
    split lines, dividers, garbage, oversized lines, the marker echoes) only
    while intake is open, takes records as a consumer at random, issues stop,
    kill, detach and callback failures at random, and moves the clock. After
    every event it checks the global rules (see `send`).

    Package attribution (design 5.8): ``where`` watches APP in most runs, and
    one or two other packages in some; the lines come from APP's uid, another
    app uid, a secondary user and system uids; a lookup answers APP's uid,
    another uid, "not installed" or fails;
    and now and then a lookup's result is delivered again late (stale: X-3),
    which must change nothing. So lines are held, re-looked-up, released,
    expire, and go through the connection end and the conclusion while held.

    The consumer follows the contract of design A.21 (`consume`): each step
    settles the record in hand, pops the next one and reports; a callback
    raises now and then, and the consumer then takes nothing more and
    detaches once the outbox is closed; at its end it detaches. While the
    run drains, kill() is likelier (`DRAIN_KILL_WEIGHT`). The seeds enter the
    drain from STOPPING(stop), CONNECTING and RECONNECTING; the entry from
    STOPPING(flush) is left to the product test (`VARIANTS`, ended-flush).
    """

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)  # noqa: S311  (a test, not cryptography)
        rng = self.rng
        where = None
        self.raising = rng.random() < 0.3
        if rng.random() < 0.7:
            # A raising where also watches APP: it raises on released lines too.
            where = (
                Package(APP) | Where(self._where)
                if self.raising
                else rng.choice(
                    [
                        WHERE,
                        Package(APP),
                        ~Package(APP),
                        Package(APP) | Package(OTHER_APP),
                        Package(APP) | Package(OTHER_APP) | Package(THIRD_APP),
                    ]
                )
            )
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
        self.stamps = random.Random(seed + 100_000)  # noqa: S311  (a test)
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
            package_hold=rng.choice([0.0, 1.0, 3.0, 10.0]),
            exit_drain=0.5,
            stop_grace=0.5,
        )
        self.retired: set[int] = set()
        self.outcome: object = None
        self.changes: list[StateChange] = []
        self.finished_at: int | None = None
        self.steps = 0
        # The consumer (design A.21): the record popped last, and its failure.
        self.hand: Record | None = None
        self.failed_on: Record | None = None  # its callback raised
        self.attached_at_finished = False
        # Per CallbackFailed the core got: "outcome" (latched), or "late".
        self.failures: list[str] = []
        self.drain_failures = 0  # handled in STOPPING(drain)
        self.last_item_failures = 0  # ... on the last item emitted
        self.drained = 0  # runs that waited in STOPPING(drain)
        self.drain_kills = 0  # kill() handled in STOPPING(drain)
        self.drain_progress_ends = 0  # drains a consumer report ended
        # Package attribution (design 5.8).
        self.answered_lookups: list[int] = []  # request ids, for stale copies
        self.lookup_weight = rng.choice([3.0, 0.3])
        self.max_held = 0
        self.stale_results = 0
        self.failed_while_held = 0  # lookups that failed with lines held
        self.held_at_connection_end = 0  # events that retired a connection
        self.held_at_conclusion = 0  # ... that concluded the run
        # Lines that went on before an answer (design 5.8, D2): the most
        # waiting at once, and those an answer named.
        self.max_unsettled = 0
        self.named_by_an_answer = 0

    def _where(self, entry: LogEntry) -> bool:
        if entry.message == "boom":
            raise RuntimeError("where raised")
        return entry.tag != "Other"

    # -- one step --------------------------------------------------------------------

    def run(self, steps: int) -> None:
        """Play ``steps`` random steps, then end the run; the consumer finishes."""
        self.send(Start())
        for _ in range(steps):
            if self.harness.core.state in TERMINAL:
                break
            self.step()
        self.finish()

    def send(self, event: Event, at: float | None = None) -> list[Command]:
        """Hand one event to the core and check the global rules on the result."""
        harness = self.harness
        core = harness.core
        before = core.state
        draining = core.mode is Mode.DRAIN
        live_before = core.connection
        held_before = core._attribution.held
        concluded_before = core.concluded
        issued = len(harness.commands)
        late = core.late_failure(event) if isinstance(event, CallbackFailed) else None
        if isinstance(event, CallbackFailed) and core.mode is Mode.DRAIN:
            self.drain_failures += 1
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
            box = harness.outbox
            self.attached_at_finished = not box.detached
            if core.state is S.STOPPED and not box.detached:
                # A.21: an attached consumer finished every earlier record.
                assert (len(box), box.in_hand) == (1, False), "STOPPED unhandled"
        self._count_drain(event, draining=draining)
        if isinstance(event, CallbackFailed):
            # A.21: a failure handled before the end is the outcome (G4);
            # one the core cannot latch is late (the engine logs it).
            if late is None:
                outcome = core.outcome
                assert isinstance(outcome, CallbackError), "a live failure was lost"
                assert outcome.__cause__ is event.error, "not the latched failure"
            self.failures.append("outcome" if late is None else "late")
        live = harness.core.connection
        if live_before is not None and live != live_before:
            self.retired.add(live_before)
            self.held_at_connection_end += held_before > 0
        if core.concluded and not concluded_before:
            self.held_at_conclusion += held_before > 0
        self._attribution_reach()
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

    def _attribution_reach(self) -> None:
        """The most lines held, and waiting for an answer, at once (5.8)."""
        attribution = self.harness.core._attribution
        self.max_held = max(self.max_held, attribution.held)
        self.max_unsettled = max(self.max_unsettled, attribution.unsettled)

    def _count_drain(self, event: Event, *, draining: bool) -> None:
        """Count drains, and how ``event`` ended one (for the reach test)."""
        core = self.harness.core
        if core.mode is Mode.DRAIN and not draining:
            self.drained += 1
        if draining and core.state in TERMINAL:
            if isinstance(event, KillRequest):
                self.drain_kills += 1
            elif isinstance(event, ConsumerProgress):
                self.drain_progress_ends += 1

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
        if self.answered_lookups:
            weighted.append((0.3, self.stale))
        kinds = {command.kind for command in harness.pending.values()}
        if kinds - {"lookup"}:
            weighted.append((3.0, self.answer_other))
        if "lookup" in kinds:
            # Slow re-lookups in some runs, so that lines are held at a
            # connection end and at the conclusion too; the first lookups,
            # which intake waits for, are not slowed.
            slow = core._attribution.ready
            weight = self.lookup_weight if slow else 3.0
            weighted.append((weight, self.answer_lookup))
        if conn is not None and conn.spawn_issued and conn.pid is None:
            weighted.append((3.0, self.spawn))
        if conn is not None and conn.pid is not None and core.intake_open:
            weighted += [(8.0, self.output), (0.2, self.close_pipe)]
        if conn is not None and conn.alive:
            weighted.append((0.25, self.exit))
        if core.mode is Mode.DRAIN:
            weighted.append((DRAIN_KILL_WEIGHT, self.kill))
        actions = [action for _, action in weighted]
        rng.choices(actions, weights=[weight for weight, _ in weighted])[0]()

    def advance(self) -> None:
        step = self.rng.choice([0.01, 0.05, 0.1, 0.3, 0.6, 2.0])
        self.send(Tick(), at=self.harness.now + step)

    def consume(self) -> None:
        """Up to four steps of the delivery iterator (design A.21, C1).

        Each step settles the record in hand (its callback returned), pops
        the next one and reports; the reports of one action merge into one,
        as in a control inbox. The callback of the last record popped is
        still running when the action ends. Now and then the callback of
        the record in hand raises (`_failure_odds`, `fail`). At the end of
        the records the consumer detaches; after its failure it takes
        nothing more and detaches once the outbox is closed.
        """
        outbox = self.harness.outbox
        if outbox.detached:
            return
        if self.failed_on is not None:
            if outbox.closed:
                self.detach()
            return
        for _ in range(self.rng.randint(0, 4)):
            if outbox.in_hand:
                if self.rng.random() < self._failure_odds():
                    self.report()
                    self.fail()
                    return
                outbox.done()
            self.hand = outbox.pop()
            if self.hand is None:
                if outbox.ended:
                    self.report()
                    self.detach()
                    return
                break
        self.report()

    def _failure_odds(self) -> float:
        """How likely the callback of the record in hand raises.

        5% per record in a run with ``raising`` callbacks. While the run
        drains (design A.21), DRAIN_ODDS in every run, and LAST_ODDS on the
        last item, so that the seeds exercise the failures the drain exists
        for; after a kill ended the run, DRAIN_ODDS on the events left (a
        late failure: the run stays KILLED).
        """
        if self.harness.core.state is S.KILLED:
            return DRAIN_ODDS
        if self.harness.core.mode is Mode.DRAIN:
            record = self.hand
            last = (
                isinstance(record, LogEntry | LogGroup)
                and self.harness.outbox.entries == 0
            )
            return LAST_ODDS if last else DRAIN_ODDS
        return 0.05 if self.raising else 0.0

    def kill(self) -> None:
        """``kill()`` now (the drain's kill row; `control` covers the rest)."""
        self.send(KillRequest())

    def report(self) -> None:
        """The consumer side's ``ConsumerProgress`` with the outbox counts."""
        outbox = self.harness.outbox
        self.harness.reported = outbox.counts()
        self.send(ConsumerProgress(outbox.counts()))

    def fail(self) -> None:
        """The callback of the record in hand raised (design A.21, C3).

        The record stays unsettled; the consumer posts one CallbackFailed
        and takes nothing more.
        """
        record = self.hand
        assert record is not None
        assert self.harness.outbox.in_hand
        self.failed_on = record
        core = self.harness.core
        if (
            core.mode is Mode.DRAIN
            and isinstance(record, LogEntry | LogGroup)
            and self.harness.outbox.entries == 0
        ):
            self.last_item_failures += 1
        stage = "on_item" if isinstance(record, LogEntry | LogGroup) else "on_event"
        self.send(CallbackFailed(stage, ValueError("callback")))

    def detach(self) -> None:
        """The consumer detaches: `Outbox.detach`, then ``ConsumerDetached``."""
        outbox = self.harness.outbox
        outbox.detach()
        self.harness.reported = outbox.counts()
        self.send(ConsumerDetached(outbox.counts()))

    def answer_lookup(self) -> None:
        self.answer(lookup=True)

    def answer_other(self) -> None:
        self.answer(lookup=False)

    def answer(self, *, lookup: bool | None = None) -> None:
        """Answer a pending command: any, a lookup (True) or another (False).

        `step` asks for another command (lookups have their own weight); the
        end of the run answers any.
        """
        rng = self.rng
        pending = [
            command
            for command in self.harness.pending.values()
            if lookup is None or (command.kind == "lookup") is lookup
        ]
        command = rng.choice(pending)
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
                answers = _LOOKUP_ANSWERS[command.argv[-1]]
                index = rng.randrange(len(answers)) if good else 0  # as choice
                outcome = answers[index] if good else LOOKUP_FAILED
                self.answered_lookups.append(command.request)
                if not good and self.harness.core._attribution.held:
                    self.failed_while_held += 1
                if good:
                    self._answer_lookup(command, outcome, _ANSWER_UIDS[index])
                    return
            case _:
                outcome = MARKER_OK if good else TimeoutError("marker")
        self.send(CommandDone(command.request, outcome))

    def _answer_lookup(
        self, command: RunCommand, outcome: _AdbResult, uid: int | None
    ) -> None:
        """A lookup answer that names ``uid``: it counts at least the lines of
        that uid that went on and depend on the package (design 5.8, D2),
        and none of them waits any more.
        """
        core = self.harness.core
        attribution = core._attribution
        package = command.argv[-1]
        pending = core._requests.get(command.request)
        flight = attribution._flight
        owing = None
        if (
            uid is not None
            and pending is not None
            and flight is not None
            and pending.lookup == flight.ident
        ):
            owing = attribution._owing.get(uid)
        named = (
            0
            if owing is None
            else sum(count for names, count in owing.lines.items() if package in names)
        )
        before = core.stats().unattributed
        self.send(CommandDone(command.request, outcome))
        assert core.stats().unattributed - before >= named, "a named line not counted"
        after = attribution._owing.get(uid) if uid is not None else None
        if after is not None:
            assert all(package not in names for names in after.lines), "still waits"
        self.named_by_an_answer += named

    def stale(self) -> None:
        """A late copy of an answered lookup's result: it changes nothing (X-3)."""
        rng = self.rng
        attribution = self.harness.core._attribution
        before = (attribution.uid_packages, attribution.held, attribution.deadline())
        request = rng.choice(self.answered_lookups)
        outcome = rng.choice([LOOKUP_OK, LOOKUP_ABSENT, OTHER_LOOKUP, LOOKUP_FAILED])
        self.send(CommandDone(request, outcome))
        after = (attribution.uid_packages, attribution.held, attribution.deadline())
        assert after == before, "a stale lookup result changed the attribution"
        self.stale_results += 1

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
        uid = rng.choice(_UIDS)
        text = f"m{rng.randint(0, 99)}"
        # Times around STAMP from a generator of their own, so the other
        # choices stay as they were: replays, lines below a resume's request
        # and lines past its position (design 5.6).
        when = self.stamps.choice(_STAMPS)
        return line(text, tag=tag, pid=rng.choice([1, 2]), uid=uid, stamp=when)

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
        """stop, stop(flush), kill, a detach, or a callback failure now."""
        rng = self.rng
        outbox = self.harness.outbox
        choice = rng.choice(["stop", "flush", "kill", "detach", "fail"])
        if choice == "stop":
            self.send(StopRequest())
        elif choice == "flush":
            self.send(StopRequest(flush=rng.choice([0.2, 2.0])))
        elif choice == "kill":
            self.send(KillRequest())
        elif outbox.detached or self.failed_on is not None:
            return  # no consumer, or one that takes nothing more (C3)
        elif choice == "detach":
            self.detach()  # break, close(), or a dropped iterator
        else:
            # The callback of the record in hand raises; with none in hand,
            # the next record's callback does.
            if not outbox.in_hand:
                self.hand = outbox.pop()
                self.report()
                if self.hand is None:
                    return
            self.fail()

    # -- the end -----------------------------------------------------------------

    def finish(self) -> None:
        """Stop, let every connection end, and let the consumer finish."""
        harness = self.harness
        core = harness.core
        if core.state not in TERMINAL:
            self.send(StopRequest())
        for _ in range(1000):
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
        # The consumer takes the rest and detaches at its end, or detaches
        # after its own failure (design A.21, C6). Bounded, so a core that
        # ends without Finished fails here instead of hanging the run.
        for _ in range(10_000):
            if harness.outbox.detached:
                break
            self.consume()
        else:
            msg = (
                f"the consumer never detached in {state_key(harness)} "
                f"(Finished carried out: {len(harness.finished)})"
            )
            raise AssertionError(msg)
