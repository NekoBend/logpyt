"""Tests for the outbox, admission and the control inbox (design 5.2, 5.10; G6, X-2)."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from logpyt import GroupBy
from logpyt._core.grouping import entry_bytes
from logpyt._core.mailbox import MAX_PENDING_EVENTS, Admission, ControlInbox, Outbox
from logpyt._core.protocol import (
    CallbackFailed,
    CommandDone,
    ConsumerDetached,
    ConsumerProgress,
    Data,
    Eof,
    Exited,
    KillRequest,
    OutboxCounts,
    Spawned,
    Start,
    StopRequest,
    Tick,
)
from logpyt.exceptions import AdbExitError
from logpyt.models import LogEntry, LogGroup
from logpyt.streams._events import Gap, StateChange, StreamState, StreamStats
from tests.core.harness import CLOCK_OK, Harness, lines

if TYPE_CHECKING:
    from collections.abc import Sequence

S = StreamState


def _entry(message: str = "m") -> LogEntry:
    return LogEntry(
        timestamp=datetime(2026, 11, 4, tzinfo=UTC),
        pid=1,
        tid=1,
        level="I",
        tag="T",
        message=message,
        raw=f"raw {message}",
    )


def _change(old: StreamState, new: StreamState, connection: int = 1) -> StateChange:
    return StateChange(old, new, connection, None)


def _counts(entries: int, nbytes: int) -> OutboxCounts:
    return OutboxCounts(taken_entries=entries, taken_bytes=nbytes)


# -- Admission -------------------------------------------------------------------------


def test_admission_closes_at_capacity_in_entries() -> None:
    admission = Admission(buffer_entries=4, buffer_bytes=1_000)

    admission.admit(3, 30)
    assert admission.open
    admission.admit(1, 10)

    assert not admission.open
    assert (admission.queued_entries, admission.queued_bytes) == (4, 40)


def test_admission_closes_at_capacity_in_bytes() -> None:
    admission = Admission(buffer_entries=1_000, buffer_bytes=100)

    admission.admit(1, 99)
    assert admission.open
    admission.admit(1, 1)

    assert not admission.open


def test_admission_reopens_at_half_of_both() -> None:
    """The low-water mark is half of buffer_entries and half of buffer_bytes."""
    admission = Admission(buffer_entries=10, buffer_bytes=100)
    admission.admit(10, 100)

    admission.release(_counts(4, 60))  # 6 entries, 40 bytes: entries above half
    assert not admission.open
    admission.release(_counts(5, 60))  # 5 entries, 40 bytes
    assert admission.open


def test_admission_ignores_an_older_report() -> None:
    """Counts are cumulative: a stale report never moves the fill back up."""
    admission = Admission(buffer_entries=2, buffer_bytes=100)
    admission.admit(2, 20)
    admission.release(_counts(2, 20))
    assert admission.open

    admission.release(_counts(1, 10))

    assert admission.queued_entries == 0


def test_admission_of_one_entry_reopens_when_empty() -> None:
    admission = Admission(buffer_entries=1, buffer_bytes=10_000)
    admission.admit(1, 10)
    assert not admission.open

    admission.release(_counts(1, 10))

    assert admission.open


def test_discards_release_admission() -> None:
    """Discarded entries leave the outbox too: they count as removed."""
    admission = Admission(buffer_entries=2, buffer_bytes=10_000)
    admission.admit(2, 20)

    admission.release(OutboxCounts(discarded_entries=2, discarded_bytes=20))

    assert admission.open


# -- Outbox ----------------------------------------------------------------------------


def test_outbox_keeps_items_and_events_in_order() -> None:
    outbox = Outbox()
    first, second = _entry("a"), _entry("b")
    change = _change(S.RUNNING, S.RECONNECTING)

    outbox.put([first, change, second])

    assert [outbox.pop() for _ in range(4)] == [first, change, second, None]


def test_outbox_counts_entries_and_bytes_of_items_only() -> None:
    """Events live outside the data capacity; a group counts its members."""
    outbox = Outbox()
    entries = (_entry("a"), _entry("bb"))
    group = LogGroup(key=(), entries=entries, reason="end")

    outbox.put([_change(S.IDLE, S.CONNECTING), group, _entry("c")])

    assert outbox.entries == 3
    assert outbox.nbytes == sum(map(entry_bytes, entries)) + entry_bytes(_entry("c"))
    assert outbox.events == 1
    assert outbox.pop() == _change(S.IDLE, S.CONNECTING)
    assert outbox.pop() is group
    assert outbox.counts() == _counts(2, sum(map(entry_bytes, entries)))


def _assert_chain(records: Sequence[object]) -> None:
    """Each change starts where the one before it ended."""
    changes = [record for record in records if isinstance(record, StateChange)]
    assert len(changes) == len(records)
    for before, after in itertools.pairwise(changes):
        assert after.old is before.new, f"broken chain: {before} then {after}"


def _three_pending() -> Outbox:
    outbox = Outbox(max_pending_events=3)
    outbox.put([
        _change(S.IDLE, S.CONNECTING),
        _change(S.CONNECTING, S.RECONNECTING),
        _change(S.RECONNECTING, S.CONNECTING, 2),
    ])  # fmt: skip
    return outbox


@pytest.mark.parametrize("merges", [1, 2])
def test_consecutive_state_changes_coalesce_beyond_the_limit(merges: int) -> None:
    """Beyond 256 pending events, consecutive changes merge into the latest one."""
    outbox = _three_pending()
    later = [
        _change(S.CONNECTING, S.RECONNECTING, 2),
        _change(S.RECONNECTING, S.CONNECTING, 3),
    ][:merges]

    for change in later:
        outbox.put([change])

    assert outbox.events == 3
    assert outbox.counts().events_coalesced == merges
    taken = [outbox.pop() for _ in range(3)]
    assert outbox.pop() is None
    # The chain of states stays unbroken: old of the replaced change is kept,
    # new, connection and error are the latest change's.
    _assert_chain(taken)
    assert taken[-1] == replace(later[-1], old=S.RECONNECTING)
    assert MAX_PENDING_EVENTS == 256


def test_a_merged_change_can_return_to_its_own_state() -> None:
    """A merge can give old == new: a reconnect seen as RUNNING -> RUNNING.

    It keeps the latest connection number and error (documented on
    `StateChange`); it reaches the consumer like any other change.
    """
    outbox = Outbox(max_pending_events=1)
    outbox.put([_change(S.CONNECTING, S.RUNNING)])
    outbox.put([_entry()])
    assert isinstance(outbox.pop(), StateChange)
    assert isinstance(outbox.pop(), LogEntry)
    error = AdbExitError(
        "adb logcat exited with status 1", argv=(), returncode=1, stats=StreamStats()
    )

    outbox.put([
        StateChange(S.RUNNING, S.RECONNECTING, 1, error),
        _change(S.RECONNECTING, S.CONNECTING, 2),
        _change(S.CONNECTING, S.RUNNING, 2),
    ])  # fmt: skip

    assert outbox.counts().events_coalesced == 2
    assert outbox.pop() == StateChange(S.RUNNING, S.RUNNING, 2, None)
    assert outbox.pop() is None


def test_gap_terminal_and_separated_changes_never_coalesce() -> None:
    outbox = Outbox(max_pending_events=1)
    gap = Gap(after=None, before=None, lost=None, reason="ring_rotated")
    outbox.put([_change(S.IDLE, S.CONNECTING)])

    outbox.put([gap, gap])  # Gap: never
    outbox.put([_change(S.CONNECTING, S.RUNNING)])  # after a Gap: not consecutive
    outbox.put([_entry(), _change(S.RUNNING, S.STOPPING)])  # after an item
    outbox.put([_change(S.STOPPING, S.STOPPED)])  # terminal: never

    assert outbox.counts().events_coalesced == 0
    assert outbox.events == 6


def test_discard_drops_items_and_keeps_events() -> None:
    """DiscardUndelivered (kill, a failed consumer): items go, counted; events stay."""
    outbox = Outbox()
    change = _change(S.RUNNING, S.STOPPING)
    items = [_entry("a"), _entry("b")]
    outbox.put([items[0], change, items[1]])

    outbox.discard()

    assert outbox.entries == 0
    assert outbox.pop() is change
    assert outbox.pop() is None
    counts = outbox.counts()
    assert counts.discarded_entries == 2
    assert counts.discarded_bytes == sum(map(entry_bytes, items))
    assert counts.removed_entries == 2


def test_detach_discards_everything_and_counts_later_items() -> None:
    """A detached consumer (3.5): queued and later items are discarded, counted."""
    outbox = Outbox()
    outbox.put([_change(S.IDLE, S.CONNECTING), _entry()])

    outbox.detach()
    outbox.put([_entry(), _change(S.RUNNING, S.STOPPING)])

    assert outbox.detached
    assert len(outbox) == 0
    assert outbox.counts().discarded_entries == 2


def test_close_marks_the_end() -> None:
    outbox = Outbox()
    outbox.put([_entry()])

    outbox.close()

    assert outbox.closed
    assert not outbox.ended
    outbox.pop()
    assert outbox.ended
    with pytest.raises(RuntimeError, match="after close"):
        outbox.put([_entry()])


def test_terminal_group_survives_full_outbox() -> None:
    """X-2: the groups a stop flushes and the terminal change fit a full outbox.

    No sentinel takes a slot and nothing is evicted: the outbox never refuses
    a record, and the consumer receives the last group and the terminal state
    change after every earlier item.
    """
    harness = Harness.make(
        buffer_entries=3,
        group_by=GroupBy(
            keys=(), gap_ms=60_000.0, max_group_size=3, idle_flush_ms=None
        ),
    )
    harness.connecting_spawned()
    harness.stdout(lines(6))  # two full groups of 3: the outbox is full
    assert not harness.core.intake_open
    harness.stdout(lines(2, "tail"))  # in the outbox's overshoot: an open group
    assert harness.outbox.entries == 6

    harness.send(StopRequest())
    harness.end_connection(0)

    assert harness.core.state is S.STOPPED
    records = []
    while (record := harness.outbox.pop()) is not None:
        records.append(record)
    groups = [record for record in records if isinstance(record, LogGroup)]
    assert [group.reason for group in groups] == ["size", "size", "end"]
    assert [entry.message for entry in groups[-1]] == ["tail0", "tail1"]
    assert records[-1] == StateChange(S.STOPPING, S.STOPPED, 1, None)
    assert harness.outbox.ended
    assert harness.outbox.counts().discarded_entries == 0


# -- ControlInbox --------------------------------------------------------------------


def test_control_inbox_takes_in_priority_order() -> None:
    inbox = ControlInbox()
    progress = ConsumerProgress(_counts(1, 1))
    result = CommandDone(1, CLOCK_OK)
    failed = CallbackFailed("on_item", ValueError("x"))
    for event in (progress, result, ConsumerDetached(), StopRequest(), KillRequest()):
        inbox.post(event)
    inbox.post(failed)
    inbox.post(Start())

    assert len(inbox) == 7
    taken = [inbox.take() for _ in range(8)]

    assert taken == [
        Start(),
        failed,
        KillRequest(),
        StopRequest(),
        ConsumerDetached(),
        result,
        progress,
        None,
    ]


def test_control_inbox_keeps_one_record_per_request() -> None:
    """Bounded: the first stop and failure, the latest progress, one of each flag."""
    inbox = ControlInbox()
    first = CallbackFailed("on_item", ValueError("first"))
    for index in range(1000):
        inbox.post(StopRequest(flush=float(index)))
        inbox.post(KillRequest())
        inbox.post(ConsumerProgress(_counts(index, index)))
        inbox.post(ConsumerDetached())
    inbox.post(first)
    inbox.post(CallbackFailed("on_event", ValueError("second")))

    assert len(inbox) == 5
    assert inbox.take() is first
    assert inbox.take() == KillRequest()
    assert inbox.take() == StopRequest(flush=0.0)
    assert inbox.take() == ConsumerDetached()
    assert inbox.take() == ConsumerProgress(_counts(999, 999))


def test_control_inbox_keeps_results_in_order() -> None:
    inbox = ControlInbox()
    events = [Spawned(1, 10), Exited(1, 0), CommandDone(3, CLOCK_OK)]
    for event in events:
        inbox.post(event)

    assert [inbox.take() for _ in events] == events


@pytest.mark.parametrize(
    "event",
    [Data(1, "stdout", b"x"), Eof(1, "stdout"), Tick()],
    ids=["data", "eof", "tick"],
)
def test_control_inbox_refuses_data_and_ticks(event: object) -> None:
    with pytest.raises(TypeError, match="not a control event"):
        ControlInbox().post(event)  # ty: ignore[invalid-argument-type]
