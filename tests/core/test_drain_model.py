"""An exhaustive check of the drain of design A.21 over the real core.

Depth-first search with state hashing over every interleaving of these
actors, each step atomic (one critical section of the driver contract):

- the consumer, the delivery iterator of protocol.py's module docs. "next":
  settle the record in hand if its callback returned, pop, report
  ``ConsumerProgress``; with nothing popped, detach if the outbox ended.
  "call": run the callback of the popped record: it returns; or, at record
  ``fail_at``, it raises (``fail-wait``: post `CallbackFailed`, take nothing
  more, detach once the outbox is closed) or the loop breaks at the first
  item from there on (``break``: detach). Once ``kill()`` set the kill flag
  (F2-7), an item is skipped instead: no callback, and never settled;
- the engine: take one control event (`ControlInbox` order), hand it to the
  core and carry out the commands like a driver: the engine-side report after
  a batch that changed the counts, and the report handed to the core right
  before `Finished` closes the outbox;
- one engine-side snapshot of the counts posted at any instant;
- adb: EOF on stdout, EOF on stderr (the data inbox, straight to the core)
  and the exit; ``stop()`` in the scenarios that post it, and optionally
  ``kill()``.

Every end state must satisfy the race proof of A.21 (`World.check`), and
every state of the drain its invariants (`_check_drain`).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Self

import pytest

from logpyt._core.machine import Mode
from logpyt._core.mailbox import ControlInbox
from logpyt._core.protocol import (
    CallbackFailed,
    ConsumerDetached,
    ConsumerProgress,
    Deliver,
    DiscardUndelivered,
    Eof,
    Exited,
    Finished,
    KillRequest,
    StopRequest,
)
from logpyt.config import Reconnect
from logpyt.exceptions import CallbackError
from logpyt.models import LogEntry, LogGroup
from logpyt.streams._events import StateChange, StreamState
from tests.core.harness import Harness, lines

if TYPE_CHECKING:
    from logpyt._core.protocol import Command, Event, Record
    from logpyt.streams._events import StreamStats

S = StreamState
TERMINAL = frozenset({S.STOPPED, S.KILLED, S.FAILED})
_MODES = ("ok", "fail-wait", "break")
_POSITIONS = range(6)  # the consumer's record index of a failure or a break


class _Count:
    """A copyable stand-in for the core's ``itertools.count`` request ids."""

    def __init__(self, start: int) -> None:
        self.n = start

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> int:
        self.n += 1
        return self.n - 1


def _terminal_change(record: Record | None) -> bool:
    return isinstance(record, StateChange) and record.new in TERMINAL


def _item(record: Record | None) -> bool:
    return isinstance(record, LogEntry | LogGroup)


class World:
    """One state of the model: the core, the outbox, the inbox and the actors."""

    def __init__(
        self, scenario: str, mode: str, fail_at: int | None, *, kill: bool
    ) -> None:
        self.scenario = scenario
        self.mode = mode
        self.fail_at = fail_at
        self.external: list[str] = []
        if scenario == "stop-data":
            harness = Harness.make().running()
            harness.consume()
            harness.send(StopRequest())
            harness.stdout(lines(1))
            self.external = ["eof-out", "eof-err", "exit"]
        else:
            harness = Harness.make(reconnect=Reconnect(delay=5.0), stall_timeout=None)
            harness.running()
            harness.consume()
            harness.stdout(lines(1))
            harness.end_connection(1)
            assert harness.core.state is S.RECONNECTING
            if scenario == "connecting":
                harness.advance(5.0)
                assert harness.core.state is S.CONNECTING
            self.external = ["stop"]
        if kill:
            self.external.append("kill")
        core = harness.core
        # itertools.count cannot be deep-copied (Python 3.14): a copyable one.
        core._request_ids = _Count(next(core._request_ids))  # ty: ignore[invalid-assignment]
        self.harness = harness
        self.inbox = ControlInbox()
        self.snapshots = 1  # engine-side reports still to post at any instant
        self.trace: list[str] = []
        # The consumer.
        self.popped = 0
        self.popped_items = 0
        self.pending: tuple[int, Record] | None = None  # popped, callback to run
        self.owe_done = False  # the callback returned: settle at the next step
        self.waiting_close = False  # failed: detach once the outbox is closed
        self.consumer_done = False
        self.failed: str | None = None
        self.failed_on_terminal = False
        self.broke = False
        self.skipped = 0  # items skipped under the kill flag (F2-7)
        self.saw_terminal = False
        self.items_total = harness.outbox.entries
        self.base_taken = harness.outbox.counts().taken_entries
        # What the engine saw.
        self.finished = 0
        self.at_finished: StreamStats | None = None
        self.box_at_finished: tuple[int, bool] | None = None
        self.posted_failures = 0
        self.taken_failures = 0
        self.late = 0
        self.lost = 0  # live failures that did not become the outcome
        self.drain_emitted: int | None = None

    # -- the engine -----------------------------------------------------------------

    def carry_out(self, commands: list[Command]) -> None:
        harness = self.harness
        outbox = harness.outbox
        before = outbox.counts()
        for command in commands:
            match command:
                case Deliver(records=records):
                    outbox.put(records)
                case DiscardUndelivered():
                    outbox.discard()
                case Finished():
                    out = harness.core.handle(ConsumerProgress(outbox.counts()), 0.0)
                    assert out == [], out
                    self.finished += 1
                    self.at_finished = harness.core.stats()
                    self.box_at_finished = (len(outbox), outbox.in_hand)
                    outbox.close()
                    before = outbox.counts()
                case _:
                    pass
        if outbox.counts() != before:
            self.inbox.post(ConsumerProgress(outbox.counts()))

    def handle(self, event: Event) -> None:
        core = self.harness.core
        if isinstance(event, CallbackFailed):
            self.taken_failures += 1
            late = core.late_failure(event) is not None
            assert late == (core.state in TERMINAL or core.outcome is not None)
            self.carry_out(core.handle(event, 0.0))
            if late:
                self.late += 1
            else:
                outcome = core.outcome
                if not (
                    isinstance(outcome, CallbackError)
                    and outcome.__cause__ is event.error
                ):
                    self.lost += 1
            return
        self.carry_out(core.handle(event, 0.0))

    # -- the steps --------------------------------------------------------------------

    def enabled(self) -> list[str]:
        steps: list[str] = []
        if len(self.inbox):
            steps.append("take")
        steps += self.external
        if self.snapshots:
            steps.append("snapshot")
        if not self.consumer_done:
            outbox = self.harness.outbox
            if self.waiting_close:
                if outbox.closed:
                    steps.append("final-detach")
            elif self.pending is not None:
                steps.append("call")
            elif self.owe_done or len(outbox) or outbox.ended:
                steps.append("next")
        return steps

    def detach(self) -> None:
        outbox = self.harness.outbox
        outbox.detach()
        self.inbox.post(ConsumerDetached(outbox.counts()))
        self.consumer_done = True

    def step(self, name: str) -> None:
        harness = self.harness
        outbox = harness.outbox
        self.trace.append(name)
        match name:
            case "take":
                event = self.inbox.take()
                assert event is not None
                self.handle(event)
            case "snapshot":
                self.snapshots -= 1
                self.inbox.post(ConsumerProgress(outbox.counts()))
            case "eof-out" | "eof-err":
                self.external.remove(name)
                pipe = "stdout" if name == "eof-out" else "stderr"
                # The data inbox: a retired connection's EOF is ignored (G1).
                self.carry_out(harness.core.handle(Eof(1, pipe), 0.0))
            case "exit":
                self.external.remove(name)
                self.inbox.post(Exited(1, 0))
            case "stop":
                self.external.remove(name)
                self.inbox.post(StopRequest())
            case "kill":
                self.external.remove(name)
                self.inbox.post(KillRequest())
            case "final-detach":
                self.waiting_close = False
                self.detach()
            case "next":
                self.next()
            case "call":
                self.call()
            case _:
                raise AssertionError(name)

    def next(self) -> None:
        """One iterator step: settle, pop, report; the end detaches (C1, C6)."""
        outbox = self.harness.outbox
        if self.owe_done:
            outbox.done()
            self.owe_done = False
        record = outbox.pop()
        self.inbox.post(ConsumerProgress(outbox.counts()))
        if record is None:
            if outbox.ended:
                self.detach()
            return
        self.pending = (self.popped, record)
        self.popped += 1
        if _item(record):
            self.popped_items += 1

    def call(self) -> None:
        """The callback of the popped record: it returns, raises, or breaks."""
        assert self.pending is not None
        index, record = self.pending
        self.pending = None
        if _terminal_change(record):
            self.saw_terminal = True
        if "kill" in self.trace and _item(record):
            # kill() set the flag: the item is skipped, its callback never
            # runs, and it is never settled (F2-7).
            self.skipped += 1
            return
        at = self.fail_at
        if self.mode == "break" and _item(record) and at is not None and index >= at:
            self.broke = True
            self.detach()
            return
        if self.mode == "fail-wait" and index == self.fail_at:
            stage = "on_item" if _item(record) else "on_event"
            self.failed = stage
            self.failed_on_terminal = _terminal_change(record)
            self.inbox.post(CallbackFailed(stage, ValueError(f"boom {index}")))
            self.posted_failures += 1
            self.waiting_close = True  # C3: take nothing more
            return
        self.owe_done = True

    # -- the properties ---------------------------------------------------------------

    def check_drain(self) -> None:
        """STOPPING(drain): concluded, no timer, open intake, nothing emitted."""
        core = self.harness.core
        if core.mode is not Mode.DRAIN:
            return
        assert core.concluded
        assert core.next_deadline() is None, core.armed_timers()
        assert core.intake_open
        assert core.connection is None
        emitted = core._emitted + len(core._records)
        if self.drain_emitted is None:
            self.drain_emitted = emitted
        assert emitted == self.drain_emitted, "a record emitted in the drain"

    def check(self) -> None:
        """The race proof of A.21 at an end state.

        Raises:
            AssertionError: A rule is broken.

        """
        core = self.harness.core
        assert core.state in TERMINAL, f"the run did not end: {core.state} {core.mode}"
        assert self.finished == 1, f"{self.finished} Finished"
        assert self.consumer_done, "the consumer is still attached at the end"
        assert not self.lost, "a live CallbackFailed did not become the outcome"
        assert self.posted_failures == self.taken_failures, "a CallbackFailed lost"
        if self.failed_on_terminal:
            assert self.late == 1, "a failure on the terminal change was not late"
        real = self.failed is not None and not self.failed_on_terminal
        if "kill" in self.trace:
            # Only the KillRequest ends a run whose consumer skipped an item.
            if core.state is S.STOPPED:
                assert not self.skipped, "STOPPED after a skip under the kill flag"
            # A failure taken after the kill is late: KILLED, logged (A.21).
            if real:
                assert core.state is not S.STOPPED, "a failure and a kill: STOPPED"
                if core.state is S.KILLED:
                    assert self.late == 1, "KILLED with a failure that was not late"
        elif real:
            self._check_failed()
        else:
            self._check_stopped()

    def _check_failed(self) -> None:
        """A failure on a record before the end: FAILED, the rest counted."""
        core = self.harness.core
        outcome = core.outcome
        assert core.state is S.FAILED, f"a failure on {self.fail_at}: {core.state}"
        assert isinstance(outcome, CallbackError)
        assert outcome.stage == self.failed, f"the wrong stage {outcome.stage}"
        stats = core.stats()
        handed = stats.delivered - self.base_taken
        assert (handed, stats.undelivered) == (
            self.popped_items,
            self.items_total - self.popped_items,
        ), f"stats {stats.delivered}/{stats.undelivered} after a failure"

    def _check_stopped(self) -> None:
        """No failure: STOPPED; unless it broke, complete and final at Finished."""
        core = self.harness.core
        assert core.state is S.STOPPED, f"no failure, but {core.state}"
        if self.broke:
            return
        stats = core.stats()
        assert self.at_finished == stats, "the stats changed after Finished"
        assert self.box_at_finished == (1, False), (
            f"(records, in hand) at Finished: {self.box_at_finished}"
        )
        assert not stats.incomplete, stats.incomplete
        assert not stats.undelivered, stats.undelivered
        assert self.saw_terminal, "the consumer never took the terminal StateChange"


def _record_key(record: Record) -> tuple[object, ...]:
    if isinstance(record, StateChange):
        return ("change", record.old, record.new)
    if isinstance(record, LogEntry):
        return ("entry", record.message)
    return (type(record).__name__,)


def _signature(world: World) -> tuple[object, ...]:
    """Everything that decides the future of ``world``."""
    harness = world.harness
    core = harness.core
    outbox = harness.outbox
    conn = core._conn
    inbox = world.inbox
    outcome = core.outcome
    return (
        core.state,
        core.mode,
        type(outcome).__name__,
        getattr(outcome, "stage", None),
        core._counts,
        core._emitted,
        len(core._records),
        core._detached,
        core._kill_requested,
        core._admission.admitted_entries,
        core._admission.queued_entries,
        None
        if conn is None
        else (conn.id, frozenset(conn.eof), conn.returncode, conn.killed),
        tuple(_record_key(record) for record in outbox._queue),
        outbox.counts(),
        outbox.in_hand,
        outbox.closed,
        outbox.detached,
        None if inbox._progress is None else inbox._progress.counts,
        None if inbox._failed is None else inbox._failed.stage,
        inbox._stop is not None,
        inbox._detached is not None,
        inbox._kill is not None,
        tuple(type(result).__name__ for result in inbox._results),
        world.popped,
        world.popped_items,
        None
        if world.pending is None
        else (world.pending[0], _record_key(world.pending[1])),
        world.owe_done,
        world.waiting_close,
        world.consumer_done,
        world.failed,
        world.failed_on_terminal,
        world.broke,
        world.skipped,
        world.saw_terminal,
        world.finished,
        world.at_finished,
        world.box_at_finished,
        tuple(world.external),
        world.snapshots,
        world.late,
        world.taken_failures,
        world.lost,
        "kill" in world.trace,
        world.drain_emitted,
    )


def _explore(world: World) -> tuple[int, int]:
    """Every schedule from ``world``: (distinct states, end states)."""
    seen: set[tuple[object, ...]] = set()
    ends = 0
    stack = [world]
    while stack:
        state = stack.pop()
        signature = _signature(state)
        if signature in seen:
            continue
        seen.add(signature)
        steps = state.enabled()
        if not steps:
            ends += 1
            try:
                state.check()
            except AssertionError as exc:
                pytest.fail(f"{exc}; schedule: {' '.join(state.trace)}")
            continue
        assert len(state.trace) <= 120, f"no end: {' '.join(state.trace)}"
        for name in steps:
            following = copy.deepcopy(state)
            try:
                following.step(name)
                following.check_drain()
            except AssertionError as exc:
                schedule = " ".join(following.trace)
                pytest.fail(f"{exc}; schedule: {schedule}")
            stack.append(following)
    return len(seen), ends


@pytest.mark.parametrize("scenario", ["stop-data", "reconnecting", "connecting"])
def test_drain_model(scenario: str) -> None:
    """A.21 race proof, exhaustively: no schedule breaks a rule of the drain.

    For one entry and every consumer (callbacks that return; a failure at
    each record position, the terminal change included, after which nothing
    more is taken; a break at each position), with and without a kill: the
    run ends once, the consumer ends detached, every CallbackFailed the core
    gets is the outcome or late, a failure on a record before the end ends
    FAILED (or KILLED after a kill, late), a run whose consumer skipped an
    item under the kill flag never ends STOPPED, the stats of a STOPPED run
    are final at Finished with only the terminal change queued, and the drain
    arms no timer and emits nothing.
    """
    configurations = [
        (mode, fail_at, kill)
        for mode in _MODES
        for fail_at in ((None,) if mode == "ok" else _POSITIONS)
        for kill in (False, True)
    ]
    states = ends = 0
    for mode, fail_at, kill in configurations:
        explored, finished = _explore(World(scenario, mode, fail_at, kill=kill))
        states += explored
        ends += finished
    assert ends > len(configurations)
    assert states > ends
