"""Tests for the drain of design A.21: a STOPPED end waits for the consumer.

A run that would end STOPPED waits in STOPPING(drain) while an attached
consumer has records it has not finished (`OutboxCounts.handled`), so a
callback failure on any record taken before the end ends FAILED, in every
order of the two sides and of the control inbox. Here: the paths into the
drain, its rows, what never drains, the late failures, and every
interleaving of the last record. `tests.core.test_drain_model` checks the
drain exhaustively over the real core.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Literal

import pytest

from logpyt import GroupBy, Package
from logpyt._core.machine import Mode
from logpyt._core.mailbox import ControlInbox, Outbox
from logpyt._core.protocol import (
    CallbackFailed,
    ConsumerDetached,
    ConsumerProgress,
    Eof,
    Exited,
    Finished,
    KillRequest,
    StopRequest,
)
from logpyt.config import Reconnect
from logpyt.exceptions import AdbExitError, CallbackError
from logpyt.models import LogEntry, LogGroup
from logpyt.streams._events import StateChange, StreamState
from tests.core.harness import APP, CLOCK_OK, Harness, kinds, line, lines

if TYPE_CHECKING:
    from collections.abc import Callable

    from logpyt._core.protocol import Record

S = StreamState
type _Stage = Literal["on_item", "on_event"]


def _failed(stage: _Stage = "on_item") -> CallbackFailed:
    return CallbackFailed(stage, ValueError("boom"))


def _discard_warnings(harness: Harness) -> list[str]:
    return [cmd.message for cmd in harness.diagnostics if "discarding" in cmd.message]


def _warning(reason: str) -> str:
    return (
        f"discarding undelivered items ({reason}); they are counted in "
        "stats.undelivered"
    )


def _stopped_with_queue() -> Harness:
    """stop(), then adb ends with 6 entries queued: STOPPING(drain).

    The records: IDLE -> CONNECTING, CONNECTING -> RUNNING, "first",
    RUNNING -> STOPPING, m0 .. m4 (the stop came before the data).
    """
    harness = Harness.make().running()
    harness.send(StopRequest())
    harness.stdout(lines(5))
    harness.end_connection(0)
    return harness


def _settle_until(harness: Harness, found: Callable[[Record], bool]) -> Record:
    """Settle records until ``found`` holds for one; it stays in hand."""
    while (record := harness.take()) is not None:
        if found(record):
            return record
        harness.ack()
    msg = "no such record"
    raise AssertionError(msg)


# -- the paths into the drain ------------------------------------------------------


def test_drain_waits_for_the_consumer() -> None:
    """A.21: no STOPPED, no Finished, no timer while records wait; then both.

    The data side has ended: no connection, no timer, intake open, concluded.
    The consumer's report of its last done ends the run, and the stats are
    final at Finished: taking the terminal change and detaching change none.
    """
    harness = _stopped_with_queue()
    core = harness.core

    assert core.state is S.STOPPING
    assert core.mode is Mode.DRAIN
    assert harness.finished == []
    assert core.connection is None
    assert core.next_deadline() is None
    assert core.intake_open
    assert core.concluded
    assert harness.advance(3600.0) == []
    assert core.mode is Mode.DRAIN

    harness.consume()

    assert core.state is S.STOPPED
    assert harness.finished == [Finished(None)]
    assert harness.changes()[-1].old is S.STOPPING
    stats = core.stats()
    assert (stats.delivered, stats.undelivered, stats.complete) == (6, 0, True)
    assert harness.at_finished is not None
    assert harness.at_finished[0] == stats
    assert (len(harness.outbox), harness.outbox.in_hand) == (1, False)
    harness.consume()
    harness.detach()
    assert core.stats() == stats


def test_drain_full_outbox() -> None:
    """A drain that starts with a full outbox: intake open, no backpressure.

    The intake of a retired connection is drained and ignored (G1), and the
    backpressure interval closed at the conclusion: ``backpressure_seconds``
    stays at the 2 s before it while the consumer takes its time.
    """
    harness = Harness.make(buffer_entries=4, stall_timeout=None).running()
    harness.send(StopRequest())
    harness.stdout(lines(6))
    assert not harness.core.intake_open  # full: backpressure from here
    harness.advance(2.0)
    harness.end_connection(0)
    core = harness.core

    assert core.mode is Mode.DRAIN
    assert core.intake_open
    assert core.stats().backpressure_seconds == pytest.approx(2.0)
    harness.advance(100.0)
    assert core.intake_open
    assert core.stats().backpressure_seconds == pytest.approx(2.0)

    harness.consume()

    assert core.state is S.STOPPED
    assert core.stats().backpressure_seconds == pytest.approx(2.0)


@pytest.mark.parametrize("what", ["group", "partial-line"])
def test_conclusion_records_drain(what: str) -> None:
    """Records emitted at the conclusion count against the drain.

    The consumer handled everything before the deciding batch, which emits
    one more item: the open group, flushed when the connection ends, or the
    partial last line, framed by stdout's EOF that ends it. The run waits
    for that item, so a failure on it ends FAILED.
    """
    if what == "group":
        group_by = GroupBy(keys=(), gap_ms=60_000.0, idle_flush_ms=None)
        harness = Harness.make(group_by=group_by).running()
        harness.stdout(lines(2))
        harness.send(StopRequest())
        harness.consume()
        harness.end_connection(0)
    else:
        harness = Harness.make().running()
        harness.send(StopRequest())
        harness.consume()
        harness.stdout(line("tail")[:-1])  # no LF yet: nothing framed
        harness.consume()
        connection = harness.core.connection
        assert connection is not None
        harness.send(Exited(connection, 0))
        harness.send(Eof(connection, "stderr"))
        harness.send(Eof(connection, "stdout"))  # frames "tail", ends it

    assert harness.core.state is S.STOPPING
    assert harness.core.mode is Mode.DRAIN
    assert (len(harness.outbox), harness.outbox.events) == (1, 0)
    record = harness.take()
    if what == "group":
        assert isinstance(record, LogGroup)
        assert [entry.message for entry in record] == ["first", "m0", "m1"]
    else:
        assert isinstance(record, LogEntry)
        assert record.message == "tail"
    harness.send(_failed())
    assert harness.core.state is S.FAILED
    assert isinstance(harness.core.outcome, CallbackError)


@pytest.mark.parametrize("where", ["reconnecting", "connecting"])
def test_stop_without_a_child_drains(where: str) -> None:
    """RECONNECTING stop, and CONNECTING stop before the spawn, enter the drain.

    The dead connection's entries are still queued: the new transitions
    RECONNECTING -> STOPPING and CONNECTING -> STOPPING without a child.
    """
    harness = Harness.make(reconnect=Reconnect(delay=1.0), stall_timeout=None)
    harness.running()
    harness.stdout(lines(5))
    harness.end_connection(1)
    if where == "connecting":
        harness.until_deadline()  # backoff: the next attempt reads the clock
        assert harness.core.state is S.CONNECTING

    out = harness.send(StopRequest())

    assert kinds(out) == ["Deliver"]
    assert harness.changes()[-1].new is S.STOPPING
    assert harness.core.mode is Mode.DRAIN
    assert harness.core.connection is None
    assert harness.core.next_deadline() is None
    harness.take()  # IDLE -> CONNECTING; its on_event raises
    harness.send(_failed("on_event"))
    assert harness.core.state is S.FAILED


def test_connecting_stop_drains_events() -> None:
    """CONNECTING stop before the spawn, with a watched package: events only.

    The drain arms no timer, the package hold included, and an on_event
    failure on IDLE -> CONNECTING still ends FAILED.
    """
    harness = Harness.make(where=Package(APP))
    harness.start()
    assert harness.core.armed_timers() == {"hold"}

    out = harness.send(StopRequest())

    assert harness.states() == [(S.IDLE, S.CONNECTING), (S.CONNECTING, S.STOPPING)]
    assert kinds(out) == ["Deliver"]
    assert harness.core.mode is Mode.DRAIN
    assert harness.core.armed_timers() == frozenset()
    harness.take()
    harness.send(_failed("on_event"))
    outcome = harness.core.outcome
    assert harness.core.state is S.FAILED
    assert isinstance(outcome, CallbackError)
    assert outcome.stage == "on_event"


def test_stopping_spawn_failed_drains() -> None:
    """STOPPING(stop) whose spawn fails, with the previous connection's entries.

    The spawn_failed row enters the drain without a state change; a failure
    in it ends FAILED.
    """
    harness = Harness.make(reconnect=Reconnect(delay=1.0), stall_timeout=None)
    harness.running()
    harness.stdout(lines(3))
    harness.end_connection(1)
    harness.until_deadline()  # backoff: CONNECTING, the clock read
    harness.answer("clock", CLOCK_OK)  # Spawn issued
    harness.send(StopRequest())
    assert (harness.core.state, harness.core.mode) == (S.STOPPING, Mode.STOP)

    out = harness.spawn_failed()

    assert out == []
    assert harness.core.state is S.STOPPING
    assert harness.core.mode is Mode.DRAIN
    assert harness.core.concluded
    assert harness.core.next_deadline() is None
    harness.take()
    harness.send(_failed("on_event"))
    assert harness.core.state is S.FAILED
    assert isinstance(harness.core.outcome, CallbackError)


@pytest.mark.parametrize("where", ["stopping", "reconnecting"])
def test_drain_ends_at_once(where: str) -> None:
    """A consumer that kept up and reported: the deciding batch ends the run.

    From RECONNECTING the change is RECONNECTING -> STOPPED, with no
    STOPPING in between (a regression guard: as before A.21).
    """
    harness = Harness.make(reconnect=Reconnect(delay=1.0), stall_timeout=None)
    harness.running()
    if where == "stopping":
        harness.send(StopRequest())
        harness.consume()
        out = harness.end_connection(0)
    else:
        harness.end_connection(1)
        harness.consume()
        out = harness.send(StopRequest())

    assert kinds(out)[-2:] == ["Deliver", "Finished"]
    assert harness.core.state is S.STOPPED
    if where == "reconnecting":
        assert harness.states()[-1] == (S.RECONNECTING, S.STOPPED)
        assert (S.RECONNECTING, S.STOPPING) not in harness.states()


# -- the rows of STOPPING(drain) ---------------------------------------------------


@pytest.mark.parametrize("at", ["first", "last"])
@pytest.mark.parametrize("stage", ["on_item", "on_event"])
def test_drain_failure_ends_failed(stage: _Stage, at: str) -> None:
    """A callback failure on a record taken in the drain ends FAILED (G4).

    The consumer settles records up to the first or last record of the
    stage's kind, takes it, posts CallbackFailed and takes nothing more: the
    items it did not take are discarded and counted, with the WARNING.
    """
    harness = _stopped_with_queue()
    kind = (LogEntry, LogGroup) if stage == "on_item" else StateChange
    total = len(harness.delivered()) if stage == "on_item" else len(harness.changes())
    seen = 0

    def target(record: Record) -> bool:
        nonlocal seen
        if not isinstance(record, kind):
            return False
        seen += 1
        return at == "first" or seen == total

    _settle_until(harness, target)
    left = harness.outbox.entries

    out = harness.send(_failed(stage))

    assert harness.core.state is S.FAILED
    outcome = harness.core.outcome
    assert isinstance(outcome, CallbackError)
    assert outcome.stage == stage
    assert harness.finished == [Finished(outcome)]
    assert kinds(out) == ["DiscardUndelivered", "Deliver", "Finished"]
    assert harness.at_finished is not None
    stats = harness.at_finished[0]
    assert "failed" in stats.incomplete
    assert stats.undelivered == left
    assert stats.delivered == 6 - left
    expected = [_warning("the consumer failed")] if left else []
    assert _discard_warnings(harness) == expected


def test_drain_failure_on_the_stopping_change() -> None:
    """on_event raising on RUNNING -> STOPPING, the last record before the end."""
    harness = Harness.make().running()
    harness.stdout(lines(2))
    harness.send(StopRequest())
    harness.end_connection(0)
    _settle_until(harness, lambda r: isinstance(r, StateChange) and r.new is S.STOPPING)
    assert len(harness.outbox) == 0  # the consumer took everything

    harness.send(_failed("on_event"))

    assert harness.core.state is S.FAILED
    assert isinstance(harness.core.outcome, CallbackError)
    # Nothing was left to discard: the capture is only failed.
    assert harness.core.stats().incomplete == ("failed",)


def test_drain_kill_ends_killed() -> None:
    """kill() in the drain: DiscardUndelivered, KILLED, counted at Finished."""
    harness = _stopped_with_queue()
    harness.consume(2)

    out = harness.send(KillRequest())

    assert kinds(out) == ["DiscardUndelivered", "Deliver", "Finished"]
    assert harness.core.state is S.KILLED
    assert harness.at_finished is not None
    stats = harness.at_finished[0]
    assert (stats.delivered, stats.undelivered) == (2, 4)
    assert set(stats.incomplete) == {"undelivered", "killed"}
    assert _discard_warnings(harness) == [_warning("kill()")]


@pytest.mark.parametrize("where", ["drain", "live"])
def test_kill_flag_takes_nothing_and_ends_killed(where: str) -> None:
    """kill() while items wait: the consumer takes none of them (F2-7).

    Under the kill flag the consumer's step settles the record in hand,
    takes no item and reports. That report, which the control inbox hands
    over before the waiting KillRequest, shows the items unhandled, so it
    cannot end the run STOPPED and complete. The kill discards them, counted
    in ``undelivered`` with the kill() WARNING (design 3.4). drain: kill() in
    STOPPING(drain); live: in STOPPING(stop), with the connection end handled
    before the control inbox.
    """
    harness = Harness.make().running()
    harness.consume()
    harness.send(StopRequest())
    harness.stdout(lines(4))
    if where == "drain":
        harness.end_connection(0)
        assert harness.core.mode is Mode.DRAIN
    inbox = ControlInbox()
    inbox.post(KillRequest())  # kill(): the flag is set, the request posted
    outbox = harness.outbox
    assert outbox.entries == 4
    taken = outbox.counts().taken_entries  # what the consumer took before
    # The step under the flag: no item is popped; the counts are reported.
    inbox.post(ConsumerProgress(outbox.counts()))
    if where == "live":
        harness.end_connection(0)  # the data side comes first
        assert harness.core.mode is Mode.DRAIN

    while (event := inbox.take()) is not None:
        harness.send(event)

    assert harness.core.state is S.KILLED
    assert harness.finished == [Finished(None)]
    stats = harness.core.stats()
    assert (stats.delivered, stats.undelivered) == (taken, 4)
    assert set(stats.incomplete) == {"undelivered", "killed"}
    assert _discard_warnings(harness) == [_warning("kill()")]


def test_drain_detach_ends_stopped() -> None:
    """A detach in the drain ends STOPPED; its discards are in the stats at Finished."""
    harness = _stopped_with_queue()
    harness.consume(2)

    out = harness.detach()

    assert kinds(out) == ["Deliver", "Finished"]
    assert harness.core.state is S.STOPPED
    assert harness.at_finished is not None
    assert harness.at_finished[0].undelivered == 4
    assert harness.at_finished[0].incomplete == ("undelivered",)
    assert _discard_warnings(harness) == [_warning("the consumer detached")]


@pytest.mark.parametrize("flush", [None, 1.0], ids=["stop", "flush"])
def test_drain_second_stop_is_noop(flush: float | None) -> None:
    """STOPPING(any) stop, stop(flush): none, in the drain too."""
    harness = _stopped_with_queue()

    assert harness.send(StopRequest(flush=flush)) == []
    assert harness.core.state is S.STOPPING
    assert harness.core.mode is Mode.DRAIN
    assert not harness.core.stats().flush_failed


def test_drain_counts_coalesced_events() -> None:
    """Merged state changes count as handled; the drain still ends (5.10).

    The consumer handled fewer records than the core emitted: a drain that
    compared ``handled`` alone would never end.
    """
    harness = Harness.make(reconnect=Reconnect(delay=0.01), stall_timeout=None)
    harness.outbox = Outbox(max_pending_events=2)
    harness.running()
    for _ in range(4):  # RUNNING -> RECONNECTING -> CONNECTING -> RUNNING
        harness.end_connection(1)
        harness.until_deadline()
        harness.running()
    harness.send(StopRequest())
    harness.end_connection(0)
    assert harness.core.state is S.STOPPING
    assert harness.outbox.counts().events_coalesced > 0
    assert harness.core.mode is Mode.DRAIN

    harness.consume()

    assert harness.core.state is S.STOPPED
    counts = harness.outbox.counts()
    emitted = harness.core._emitted
    # Every record but the terminal change, which still waits.
    assert counts.handled + counts.events_coalesced == emitted - 1
    assert counts.handled < emitted - 1


def test_pull_break_on_the_last_item() -> None:
    """A loop that breaks on the last item: the detach ends the drain (3.5).

    The item was taken, so nothing is discarded and the capture is complete;
    Finished comes at the detach, with the final stats.
    """
    harness = _stopped_with_queue()
    while (record := harness.take()) is not None and harness.outbox:
        harness.ack()  # the next __next__ settles the previous record
    assert isinstance(record, LogEntry)
    assert harness.core.state is S.STOPPING

    harness.detach()

    assert harness.core.state is S.STOPPED
    stats = harness.core.stats()
    assert (stats.delivered, stats.undelivered, stats.complete) == (6, 0, True)
    assert harness.at_finished is not None
    assert harness.at_finished[0] == stats


# -- the race: taken is not handled ------------------------------------------------


def _last_record_in_hand() -> Harness:
    """The drain with one entry left: the consumer pops it, its callback runs."""
    harness = Harness.make().running()
    harness.consume()
    harness.send(StopRequest())
    harness.stdout(lines(1))
    harness.end_connection(0)
    assert harness.core.state is S.STOPPING
    assert harness.core.mode is Mode.DRAIN
    while len(harness.outbox) > 1:
        harness.outbox.pop()
        harness.outbox.done()
    assert isinstance(harness.outbox.pop(), LogEntry)
    assert harness.outbox.entries == 0
    return harness


@pytest.mark.parametrize("order", ["report-first", "both-waiting"])
def test_last_item_failure_wins(order: str) -> None:
    """The last item's pop report shows nothing queued; its on_item still fails.

    report-first: the core handles the report before the CallbackFailed is
    posted. both-waiting: the report, an engine-side snapshot and the
    CallbackFailed wait together; the control inbox hands progress over
    first. Either way the drain holds until the failure: FAILED.
    """
    harness = _last_record_in_hand()
    counts = harness.outbox.counts()
    seen: list[str] = []
    if order == "report-first":
        harness.send(ConsumerProgress(counts))
        seen.append(f"{harness.core.state}/{harness.core.mode}")
        harness.send(_failed())
        seen.append(f"{harness.core.state}/{harness.core.mode}")
    else:
        inbox = ControlInbox()
        inbox.post(ConsumerProgress(counts))  # the pop's report
        inbox.post(ConsumerProgress(harness.outbox.counts()))  # the engine's
        inbox.post(_failed())
        while (event := inbox.take()) is not None:
            harness.send(event)
            seen.append(f"{type(event).__name__}:{harness.core.state}")

    expected = {
        "report-first": ["STOPPING/drain", "FAILED/None"],
        "both-waiting": ["ConsumerProgress:STOPPING", "CallbackFailed:FAILED"],
    }[order]
    assert seen == expected
    assert isinstance(harness.core.outcome, CallbackError)


def test_engine_report_cannot_end_the_drain() -> None:
    """An engine-side snapshot between the pop and the done leaks only taken.

    It shows the last item taken but not handled: the run stays in the drain
    until the consumer's done.
    """
    harness = _last_record_in_hand()
    harness.report()  # the pop's report
    assert harness.core.mode is Mode.DRAIN

    assert harness.send(ConsumerProgress(harness.outbox.counts())) == []

    assert harness.core.mode is Mode.DRAIN
    harness.ack()
    assert harness.core.state is S.STOPPED


@pytest.mark.parametrize("where", ["live", "drain"])
def test_failed_consumer_stats(where: str) -> None:
    """on_item fails on m0 of 4 entries; the consumer takes nothing more (C3).

    What it took is delivered, the rest undelivered, with the WARNING of a
    failed consumer (not of a detach).
    """
    harness = Harness.make().running()
    harness.consume()  # "first" and the events before it
    if where == "drain":
        harness.send(StopRequest())
        harness.stdout(lines(4))
        harness.end_connection(0)
        assert harness.core.state is S.STOPPING
        assert harness.core.mode is Mode.DRAIN
    else:
        harness.stdout(lines(4))
    _settle_until(harness, lambda r: isinstance(r, LogEntry) and r.message == "m0")

    harness.send(_failed())
    if where == "live":
        harness.end_connection(-9)  # the Kill of STOPPING(fail)

    assert harness.core.state is S.FAILED
    assert harness.at_finished is not None
    stats = harness.at_finished[0]
    assert (stats.delivered, stats.undelivered) == (2, 3)
    assert _discard_warnings(harness) == [_warning("the consumer failed")]
    harness.detach()  # once the outbox is closed (C3)
    assert harness.core.stats() == stats


# -- what never drains, and late failures -------------------------------------------


def test_kill_does_not_wait_for_the_consumer() -> None:
    """kill() ends at the connection end; a later on_event failure is late.

    KILLED is never complete (4.4), so the failure is logged, not latched.
    """
    harness = Harness.make().running()
    harness.stdout(lines(3))
    harness.send(KillRequest())

    harness.end_connection(-9)

    assert harness.core.state is S.KILLED
    assert len(harness.outbox) > 0  # events still wait for the consumer
    harness.take()
    event = _failed("on_event")
    message = harness.core.late_failure(event)
    assert message is not None
    assert "after the run ended KILLED" in message
    assert harness.send(event) == []
    assert harness.core.state is S.KILLED
    assert harness.core.outcome is None


def test_failed_end_does_not_drain() -> None:
    """With an error latched the verdict is fixed: FAILED at once, items kept."""
    harness = Harness.make().running()
    harness.stdout(lines(3))

    harness.end_connection(1)  # no reconnect

    assert harness.core.state is S.FAILED
    outcome = harness.core.outcome
    assert isinstance(outcome, AdbExitError)
    assert harness.outbox.entries == 4
    harness.take()
    event = _failed()
    assert harness.core.late_failure(event) is not None
    assert harness.send(event) == []
    assert harness.core.outcome is outcome


def test_concluded() -> None:
    """concluded: the data side ended, terminal or STOPPING(drain) (A.21)."""
    harness = Harness.make(reconnect=Reconnect(delay=1.0), stall_timeout=None)
    harness.start()
    observed = [(harness.core.state, harness.core.concluded)]
    harness.running()
    observed.append((harness.core.state, harness.core.concluded))
    harness.end_connection(1)
    observed.append((harness.core.state, harness.core.concluded))
    harness.until_deadline()
    harness.answer("clock", CLOCK_OK)
    harness.spawned()
    harness.send(StopRequest())  # STOPPING(stop) with a child
    observed.append((harness.core.state, harness.core.concluded))
    harness.end_connection(0)
    assert harness.core.state is S.STOPPING
    assert harness.core.mode is Mode.DRAIN
    observed.append((harness.core.state, harness.core.concluded))
    harness.consume()
    observed.append((harness.core.state, harness.core.concluded))

    assert observed == [
        (S.CONNECTING, False),
        (S.RUNNING, False),
        (S.RECONNECTING, False),
        (S.STOPPING, False),
        (S.STOPPING, True),
        (S.STOPPED, True),
    ]
    for request in (KillRequest(), _failed()):
        harness = Harness.make().running()
        harness.send(request)
        assert not harness.core.concluded
        harness.end_connection(-9)
        assert harness.core.state in {S.KILLED, S.FAILED}
        assert harness.core.concluded


def _ended(final: str) -> Harness:
    harness = Harness.make().running()
    if final == "STOPPED":
        harness.send(StopRequest())
    elif final == "KILLED":
        harness.send(KillRequest())
    harness.end_connection(1 if final == "FAILED" else 0)
    harness.consume()
    assert harness.core.state == final
    return harness


def test_late_failure_message() -> None:
    """late_failure: None while the failure can latch; else the ERROR message."""
    event = _failed("on_event")
    harness = Harness.make().running()
    assert harness.core.late_failure(event) is None
    drain = _stopped_with_queue()
    assert drain.core.mode is Mode.DRAIN
    assert drain.core.late_failure(event) is None

    for final in ("STOPPED", "KILLED", "FAILED"):
        message = _ended(final).core.late_failure(event)
        assert message == (
            f"on_event raised ValueError: boom after the run ended {final}; "
            "the outcome is unchanged"
        )
    harness.send(_failed())
    assert harness.core.mode is Mode.FAIL
    assert harness.core.late_failure(event) == (
        "on_event raised ValueError: boom after the run had already failed; "
        "the outcome is unchanged"
    )


# -- every interleaving of the last record ------------------------------------------

# Consumer steps for the last record: pop (and report it in that section),
# then: done (and report); a failure (CallbackFailed; nothing more is taken);
# the next __next__ (done, pop of nothing, report); a break (detach); or a
# failure followed by the detach, which waits until the outbox is closed (C3).
_CONSUMERS = {
    "push-ok": ("pop", "done"),
    "push-fail": ("pop", "fail"),
    "pull-ok": ("pop", "next"),
    "pull-break": ("pop", "detach"),
    "fail-close-detach": ("pop", "fail", "close-detach"),
}
# Engine steps: the connection end (data inbox) and an engine-side report of
# the outbox counts (it can carry the pop's taken count before the callback).
_ENGINE = ("end", "report")
# After each step the engine takes nothing, one control event, or all.
_TAKES = ("none", "one", "all")


def _schedules(steps: int) -> list[tuple[str, ...]]:
    consumer = [f"c{index}" for index in range(steps)]
    out: list[tuple[str, ...]] = []
    for order in itertools.permutations((*consumer, *_ENGINE)):
        if [step for step in order if step in consumer] != consumer:
            continue
        for takes in itertools.product(_TAKES, repeat=len(order)):
            schedule: list[str] = []
            for step, take in zip(order, takes, strict=True):
                schedule.append(step)
                if take != "none":
                    schedule.append(f"take-{take}")
            out.append(tuple(schedule))
    return out


_SCHEDULES = {steps: _schedules(steps) for steps in (2, 3)}


def _play(consumer: str, schedule: tuple[str, ...]) -> Harness:
    """Run one schedule; the records before the last one were handled."""
    harness = Harness.make().running()
    harness.send(StopRequest())
    harness.stdout(lines(2))  # delivered while stopping: the last record is m1
    while len(harness.outbox) > 1:
        harness.outbox.pop()
        harness.outbox.done()
    harness.report()
    inbox = ControlInbox()
    outbox = harness.outbox
    waiting_close = False

    def post_counts() -> None:
        inbox.post(ConsumerProgress(outbox.counts()))

    def pop() -> None:
        assert isinstance(outbox.pop(), LogEntry)
        post_counts()

    def done() -> None:
        outbox.done()
        assert outbox.pop() is None
        post_counts()

    def fail() -> None:
        inbox.post(_failed())

    def detach() -> None:
        outbox.detach()
        inbox.post(ConsumerDetached(outbox.counts()))

    def close_detach() -> None:
        nonlocal waiting_close
        if outbox.closed:
            detach()
        else:
            waiting_close = True

    def take(limit: int | None) -> None:
        count = 0
        while (limit is None or count < limit) and (event := inbox.take()):
            harness.send(event)
            count += 1

    actions: dict[str, Callable[[], object]] = {
        "pop": pop,
        "done": done,
        "next": done,
        "fail": fail,
        "detach": detach,
        "close-detach": close_detach,
    }
    steps: dict[str, Callable[[], object]] = {
        **{f"c{i}": actions[name] for i, name in enumerate(_CONSUMERS[consumer])},
        "end": lambda: harness.end_connection(0),
        "report": post_counts,
        "take-one": lambda: take(1),
        "take-all": lambda: take(None),
    }
    for step in schedule:
        steps[step]()
    take(None)  # the engine catches up with everything posted
    if waiting_close:
        assert outbox.closed, schedule  # the failure ended the run
        detach()
        take(None)
    return harness


@pytest.mark.parametrize("consumer", list(_CONSUMERS))
def test_last_record_every_order(consumer: str) -> None:
    """A.21 race proof: pop, callback, reports, failure and inbox order.

    For every interleaving of the consumer's steps for the last record with
    the connection end, an engine-side report and the engine's takes from the
    control inbox (which hands progress over first): a failure always ends
    FAILED, a success always ends STOPPED, each with one Finished.
    """
    verdicts: set[tuple[StreamState, str | None]] = set()
    for schedule in _SCHEDULES[len(_CONSUMERS[consumer])]:
        harness = _play(consumer, schedule)
        outcome = harness.core.outcome
        stage = outcome.stage if isinstance(outcome, CallbackError) else None
        verdicts.add((harness.core.state, stage))
        assert len(harness.finished) == 1, schedule
        if consumer == "fail-close-detach":
            assert harness.outbox.detached, schedule
    if "fail" in consumer:
        assert verdicts == {(S.FAILED, "on_item")}
    else:
        assert verdicts == {(S.STOPPED, None)}
