"""Resume conformance: duplicate-free resume against a simulated device (design 5.6).

The F1-6 row of the coverage matrix (design 7), for every driver (see
``conftest.py``). The fake adb simulates the device (``FakeAdb.device``,
``tests/fake_adb.py``): a ring that honors ``-T`` (inclusive, epoch
``<sec>.<usec>``; later entries follow in arrival order, also lower ones), a
device clock that ``shell date`` answers and the test steps, ``shell log``
echoed into the running logcat, a ring that overwrites what a stalled reader
has not read, and logd's chatty pruning (``device_expire``).

A connection is ended on purpose by releasing its ``stream`` step's ``until``
signal, after which the fake exits with the status of a disconnect (143 for a
killed device logcat, 255 for the rest: FINDINGS.md check 4). A second run
that starts with ``wait("go")`` holds the resumed connection after its spawn,
so the test can change the device during the outage; the resumed connection's
``-T`` is fixed by then (its clock read and spawn came first).

Timestamps are epoch microseconds throughout; the lines a test logs with
``at=`` carry exactly that time, the others the device clock.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from logpyt import Gap, LogEntry, Reconnect, StateChange
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, config
from tests.conftest import (
    FakeAdb,
    clock,
    expire_notice,
    run,
    stream,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

S = StreamState
US = 1_000_000
OVERLAP = 2 * US  # the default resume_overlap
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _us(when: datetime) -> int:
    return (when - _EPOCH) // _MICROSECOND


def _t_value(micros: int) -> str:
    """The library's ``-T`` form of epoch ``micros`` (design 5.6)."""
    return f"{micros // US}.{micros % US:06d}"


def _requested(call: dict[str, Any]) -> int | None:
    """The ``-T`` of a logcat call as epoch microseconds; None without one."""
    args: list[str] = call["args"]
    if "-T" not in args:
        return None
    seconds, _, fraction = args[args.index("-T") + 1].partition(".")
    return int(seconds) * US + int(fraction)


def _entries(records: Iterable[object]) -> list[LogEntry]:
    return [r for r in records if isinstance(r, LogEntry)]


def _messages(records: Iterable[object]) -> list[str]:
    return [entry.message for entry in _entries(records)]


def _gaps(records: Iterable[object]) -> list[Gap]:
    return [r for r in records if isinstance(r, Gap)]


def _changes(records: Iterable[object]) -> list[StateChange]:
    return [r for r in records if isinstance(r, StateChange)]


def _seq(entry: LogEntry) -> int:
    return int(entry.message.split(" ", 1)[0])


def _open(driver: Driver, records: list[object], **fields: Any) -> Any:  # noqa: ANN401
    """A stream that appends its items and events, in order, to ``records``."""
    fields.setdefault("reconnect", Reconnect(delay=0.01))
    return driver.open(
        config(**fields), on_item=records.append, on_event=records.append
    )


def _logcat_calls(fake: FakeAdb, count: int) -> list[dict[str, Any]]:
    """Wait for the ``count``-th adb logcat to start; return the logcat calls."""
    wait_until(lambda: len(fake.calls("logcat")) >= count, timeout=TIMEOUT)
    return fake.calls("logcat")


def _read(records: list[object], count: int) -> None:
    wait_until(lambda: len(_entries(records)) >= count, timeout=TIMEOUT)


def _outside_gaps(
    fake: FakeAdb, delivered: Sequence[int], gaps: Sequence[Gap]
) -> list[int]:
    """The sequence numbers the device logged, the stream did not deliver, and
    no reported Gap covers (its ``after`` to its ``before``, inclusive)."""
    stamps: dict[int, int] = {}
    for micros, line in fake.device_entries():
        message = line.rpartition(": ")[2]
        head = message.split(" ", 1)[0]
        if head.isdigit():
            stamps[int(head)] = micros
    spans = [
        (-1 if gap.after is None else _us(gap.after), _us(gap.before))
        for gap in gaps
        if gap.before is not None
    ]
    seen = set(delivered)
    return [
        seq
        for seq, micros in sorted(stamps.items())
        if seq not in seen and not any(low <= micros <= high for low, high in spans)
    ]


def _assert_conserved(stream: Any) -> None:  # noqa: ANN401
    """Line conservation (design A.20): every framed stdout line is counted once."""
    stats = stream.stats
    assert stats.lines == (
        stats.delivered
        + stats.undelivered
        + stats.filtered
        + stats.before_start
        + stats.deduplicated
        + stats.below_cursor
    )


def test_resume_argv_has_epoch_T(driver: Driver, fake_adb: FakeAdb) -> None:  # noqa: N802
    """A resume requests ``-T <sec>.<usec:06d>`` at hw - resume_overlap, after the
    library's options: the epoch form with six fraction digits (a digits-only
    value is a line count), exact to 1 us, with no offset (design 5.6 step 1,
    A.7). The first connection of ``since="backlog"`` has no ``-T``.
    """
    fake_adb.device()
    fake_adb.logcat(run(stream(until="cut"), exit_code=255), run(stream()))
    second = fake_adb.device_now() // US - 1
    hw = second * US + 500_123  # hw - 1.5 s has the fraction .000123
    fake_adb.device_log("a", at=hw - 1)
    fake_adb.device_log("b", at=hw)
    records: list[object] = []
    stream_ = _open(driver, records, resume_overlap=1.5)
    driver.start(stream_)
    _read(records, 2)
    fake_adb.release("cut")
    first, resumed = _logcat_calls(fake_adb, 2)
    fake_adb.device_log("c")
    _read(records, 3)
    driver.stop(stream_)
    driver.join(stream_)

    assert "-T" not in first["args"]
    assert resumed["args"] == [*first["args"], "-T", f"{second - 1}.000123"]
    assert _requested(resumed) == hw - 1_500_000
    assert _messages(records) == ["a", "b", "c"]
    stats = stream_.stats
    assert (stats.reconnects, stats.deduplicated, stats.gaps) == (1, 2, 0)
    assert stats.complete
    assert fake_adb.survivors() == []


def test_boundary_duplicates_dropped_by_occurrence(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """Identical lines in one microsecond are told apart by occurrence count.

    The first connection ends after two of three byte-identical lines at hw;
    the resume drops two copies as the replay and delivers the third, so all
    three identical log calls arrive once each (design 5.6 step 2; the burst
    lines of the 9.4 resume gate).
    """
    fake_adb.device()
    fake_adb.logcat(run(stream(lines=3), exit_code=255), run(stream()))
    t = fake_adb.device_now() - US
    fake_adb.device_log("before", at=t - 1)
    fake_adb.device_log("twin", "twin", "twin", at=t)
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    first, resumed = _logcat_calls(fake_adb, 2)
    fake_adb.device_log("after")
    _read(records, 5)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == ["before", "twin", "twin", "twin", "after"]
    twins = [e for e in _entries(records) if e.message == "twin"]
    assert {_us(e.timestamp) for e in twins} == {t}
    assert len({e.raw for e in twins}) == 1  # byte-identical
    assert "-T" not in first["args"]
    assert _requested(resumed) == t - OVERLAP
    stats = stream_.stats
    assert (stats.deduplicated, stats.below_cursor, stats.gaps) == (3, 0, 0)
    _assert_conserved(stream_)
    assert fake_adb.survivors() == []


def test_rotated_twins_keep_their_count(driver: Driver, fake_adb: FakeAdb) -> None:
    """A match uses up its window element (design 5.6 step 2): the ring rotates
    the oldest line read away, so the phase is still open when the third
    identical line comes, and it is delivered, not taken for a replay.
    """
    fake_adb.device(ring=4)
    fake_adb.logcat(run(stream(lines=3), exit_code=255), run(wait("go"), stream()))
    t = fake_adb.device_now() - US
    fake_adb.device_log("before", at=t - 1)
    fake_adb.device_log("twin", "twin", "twin", at=t)
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    _logcat_calls(fake_adb, 2)
    fake_adb.device_log("after")  # the ring of 4 drops "before"
    fake_adb.release("go")
    _read(records, 5)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == ["before", "twin", "twin", "twin", "after"]
    stats = stream_.stats
    assert (stats.deduplicated, stats.below_cursor, stats.gaps) == (2, 0, 0)
    _assert_conserved(stream_)


def test_ring_rotated_reports_gap(driver: Driver, fake_adb: FakeAdb) -> None:
    """The ring overwrote hw during the outage: the resume reports
    Gap(ring_rotated, after=hw, before=<first new line>) before that line, and
    the capture is incomplete (design 5.6 step 3, 4.4).
    """
    fake_adb.device(ring=8)
    fake_adb.logcat(run(stream(until="cut"), exit_code=255), run(wait("go"), stream()))
    t = fake_adb.device_now() - US
    for i in range(5):
        fake_adb.device_log(str(i), at=t + i)
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    _read(records, 5)
    fake_adb.release("cut")
    _logcat_calls(fake_adb, 2)
    for i in range(5, 20):  # 15 lines: the ring of 8 keeps 12..19
        fake_adb.device_log(str(i), at=t + 100 + i)
    fake_adb.release("go")
    wait_until(lambda: "19" in _messages(records), timeout=TIMEOUT)
    driver.stop(stream_)
    driver.join(stream_)

    delivered = [_seq(e) for e in _entries(records)]
    assert delivered == [*range(5), *range(12, 20)]
    (gap,) = _gaps(records)
    assert gap.reason == "ring_rotated"
    assert gap.lost is None
    assert gap.after is not None
    assert gap.before is not None
    assert (_us(gap.after), _us(gap.before)) == (t + 4, t + 112)
    following = records[records.index(gap) + 1]
    assert isinstance(following, LogEntry)
    assert following.message == "12"
    assert _outside_gaps(fake_adb, delivered, [gap]) == []
    stats = stream_.stats
    assert stats.gaps == 1
    assert "gap" in stats.incomplete
    _assert_conserved(stream_)


def test_clock_step_back_reanchors_and_reports_gap(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """The device clock moved back by more than resume_overlap during the
    outage: the resume reports Gap(clock_moved_back, after=hw, before=N) and
    requests N - outage - resume_overlap, so the lines logged under the new
    clock are read; the replay of the old lines is dropped (design 5.6).
    """
    fake_adb.device()
    fake_adb.logcat(run(stream(until="cut"), exit_code=255), run(wait("go"), stream()))
    fake_adb.device_log("a")
    fake_adb.device_log("b")
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    _read(records, 2)
    hw = _us(_entries(records)[-1].timestamp)
    fake_adb.step_clock(-3600)
    stepped = fake_adb.device_now() // US * US
    fake_adb.release("cut")
    resumed = _logcat_calls(fake_adb, 2)[1]
    (after_step,) = fake_adb.device_log("after-step")
    fake_adb.release("go")
    _read(records, 3)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == ["a", "b", "after-step"]
    (gap,) = _gaps(records)
    assert gap.reason == "clock_moved_back"
    assert gap.lost is None
    assert gap.after is not None
    assert gap.before is not None
    assert _us(gap.after) == hw
    clock = _us(gap.before)  # N: the resumed attempt's clock read
    assert stepped <= clock <= stepped + 5 * US
    request = _requested(resumed)
    assert request is not None
    # N - outage - resume_overlap; the outage (last line read to the clock
    # read) is short here.
    assert clock - OVERLAP - 5 * US <= request <= clock - OVERLAP
    assert _us(_entries(records)[-1].timestamp) < hw  # read under the new clock
    assert after_step.endswith(": after-step")
    stats = stream_.stats
    assert (stats.gaps, stats.deduplicated) == (1, 2)
    assert "gap" in stats.incomplete
    _assert_conserved(stream_)


def test_line_before_the_step_delivered_once(driver: Driver, fake_adb: FakeAdb) -> None:
    """A line logged after the cut and before the clock step is stamped above
    hw: the re-anchored resume delivers it once and restarts hw at the new
    clock's lines, so the next resume neither reports the step again nor
    replays them (design 5.6). The resume's clock read waits for the step.
    """
    fake_adb.device()
    fake_adb.date(run(clock()), run(wait("stepped"), clock()), run(clock()))
    fake_adb.logcat(
        run(stream(until="cut1"), exit_code=255),
        run(wait("go"), stream(until="cut2"), exit_code=255),
        run(stream()),
    )
    fake_adb.device_log("a")
    fake_adb.device_log("b")
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    _read(records, 2)
    fake_adb.release("cut1")
    wait_until(lambda: len(fake_adb.calls("date")) >= 2, timeout=TIMEOUT)
    fake_adb.device_log("unread")  # the first connection has ended
    fake_adb.step_clock(-3600)
    fake_adb.release("stepped")
    _logcat_calls(fake_adb, 2)
    fake_adb.device_log("after-step")
    fake_adb.release("go")
    _read(records, 4)
    fake_adb.release("cut2")
    _logcat_calls(fake_adb, 3)
    fake_adb.device_log("later")
    _read(records, 5)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == ["a", "b", "unread", "after-step", "later"]
    assert [gap.reason for gap in _gaps(records)] == ["clock_moved_back"]
    # a and b, then a, b, unread and after-step.
    assert stream_.stats.deduplicated == 2 + 4
    _assert_conserved(stream_)


def test_old_lines_not_replayed_after_reanchor(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """After a re-anchor, the lines read before the step stay in logd's ring
    stamped above the new clock, so every later ``-T`` returns them first. Until
    the device clock passes them they are dropped as replays (deduplicated),
    and the step is reported once (stage-1 decision D2, design 5.6).
    """
    fake_adb.device()
    fake_adb.logcat(
        run(stream(until="cut1"), exit_code=255),
        run(stream(until="cut2"), exit_code=255),
        run(stream()),
    )
    fake_adb.device_log("a")
    fake_adb.device_log("b")
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    _read(records, 2)
    fake_adb.step_clock(-3600)
    fake_adb.release("cut1")
    _logcat_calls(fake_adb, 2)
    fake_adb.device_log("c")
    _read(records, 3)
    fake_adb.release("cut2")
    _logcat_calls(fake_adb, 3)
    fake_adb.device_log("d")
    _read(records, 4)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == ["a", "b", "c", "d"]
    assert [gap.reason for gap in _gaps(records)] == ["clock_moved_back"]
    # The second resume: a and b (above the new clock), then c (the window).
    assert stream_.stats.deduplicated == 2 + 3
    _assert_conserved(stream_)


def test_live_clock_step_then_caught_up_outage_reports_one_gap(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """A clock step back seen live (design 5.6, A.25): Gap(clock_moved_back,
    after=hw, before=<the first new line>) right before that line, which is
    delivered, and a re-anchor at it. The outage that follows ends after the
    device clock has passed the old lines again, so the resume's clock check
    sees no step. Its -T lies in the new times, so logd returns the old lines
    first (one of them older than the window), then the new ones read
    before: none of them comes twice, and only the outage's lines are new.
    """
    fake_adb.device()
    fake_adb.date(run(clock()), run(wait("caught-up"), clock()))
    fake_adb.logcat(run(stream(until="cut"), exit_code=255), run(stream()))
    t = fake_adb.device_now()
    fake_adb.device_log("a0", at=t - 3 * US)  # outside the window at the step
    fake_adb.device_log("a", at=t - US // 2)
    fake_adb.device_log("b", at=t)
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    _read(records, 3)
    fake_adb.step_clock(-3600)
    fake_adb.device_log("c1", "c2")
    _read(records, 5)
    fake_adb.release("cut")
    wait_until(lambda: len(fake_adb.calls("date")) >= 2, timeout=TIMEOUT)
    fake_adb.step_clock(3600 + 2)  # the clock has caught up: past b
    fake_adb.device_log("o1", "o2")
    fake_adb.release("caught-up")
    resumed = _logcat_calls(fake_adb, 2)[1]
    _read(records, 7)
    fake_adb.device_log("end")
    wait_until(lambda: "end" in _messages(records), timeout=TIMEOUT)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == ["a0", "a", "b", "c1", "c2", "o1", "o2", "end"]
    entries = _entries(records)
    (gap,) = _gaps(records)
    assert gap.reason == "clock_moved_back"
    assert gap.lost is None
    assert gap.after == entries[2].timestamp  # b
    assert gap.before == entries[3].timestamp  # c1
    assert records[records.index(gap) + 1] is entries[3]
    assert _requested(resumed) == _us(entries[4].timestamp) - OVERLAP
    stats = stream_.stats
    # a0 (an old line), a and b (the old window), c1 and c2 (the new one).
    assert (stats.reconnects, stats.deduplicated, stats.below_cursor) == (1, 5, 0)
    assert stats.gaps == 1
    _assert_conserved(stream_)
    assert fake_adb.survivors() == []


def test_late_line_after_the_replayed_window_is_below_cursor(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """The replay's phase ends at the first line above hw (design A.25): a line
    read before and stored 0.7 s late, which logd returns after the window's
    last element, lies below the request (resume_overlap 0.5 s) and is
    dropped as below_cursor, not delivered again. 0.7 s is inside the step
    bound max(resume_overlap, 1 s): no Gap.
    """
    fake_adb.device()
    fake_adb.logcat(run(stream(until="cut"), exit_code=255), run(stream()))
    t = fake_adb.device_now() - US
    fake_adb.device_log("a", at=t)
    fake_adb.device_log("late", at=t - 700_000)
    records: list[object] = []
    stream_ = _open(driver, records, resume_overlap=0.5)
    driver.start(stream_)
    _read(records, 2)
    fake_adb.release("cut")
    resumed = _logcat_calls(fake_adb, 2)[1]
    fake_adb.device_log("after")
    wait_until(lambda: "after" in _messages(records), timeout=TIMEOUT)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == ["a", "late", "after"]
    assert _requested(resumed) == t - 500_000
    stats = stream_.stats
    assert (stats.deduplicated, stats.below_cursor, stats.gaps) == (1, 1, 0)
    _assert_conserved(stream_)


def test_chatty_notice_in_backlog_or_replay_not_counted(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """Chatty ``expire N lines`` notices (FINDINGS.md check 6, API 24):

    - below the start position S (the first clock read): backlog_pruned, no Gap;
    - logged during the capture: device_lost and Gap(device_reported) right
      before the notice, which is delivered like any line;
    - the same notice in a resume's replay: dropped, not counted again;
    - a notice logd wrote during the outage in place of the line at hw: a
      replay of pruning after we read it, proof that logd holds the
      position, so no ring_rotated Gap (design 5.6).
    """
    fake_adb.device()
    fake_adb.logcat(run(stream(until="cut"), exit_code=255), run(wait("go"), stream()))
    now = fake_adb.device_now()
    fake_adb.device_log("old", at=now - 20 * US)
    fake_adb.device_log(expire_notice(3), tag="chatty", at=now - 10 * US)
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    driver.wait_running(stream_)  # the clock read (S) came first
    fake_adb.device_log("a")
    fake_adb.device_log(expire_notice(2), tag="chatty")
    fake_adb.device_log("b")
    fake_adb.device_log("c")
    _read(records, 6)
    fake_adb.release("cut")
    _logcat_calls(fake_adb, 2)
    last = len(fake_adb.device_entries()) - 1
    pruned = fake_adb.device_expire(last)  # logd pruned c, the line at hw
    fake_adb.device_log("d")
    fake_adb.release("go")
    wait_until(lambda: "d" in _messages(records), timeout=TIMEOUT)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == [
        "old",
        expire_notice(3),
        "a",
        expire_notice(2),
        "b",
        "c",
        "d",
    ]
    assert pruned.endswith(expire_notice(1))
    (gap,) = _gaps(records)
    notice = records[records.index(gap) + 1]
    assert isinstance(notice, LogEntry)
    assert notice.message == expire_notice(2)
    assert gap.reason == "device_reported"
    assert gap.lost == 2
    assert gap.after is not None
    assert gap.before == notice.timestamp
    assert gap.after == _entries(records)[2].timestamp  # "a"
    stats = stream_.stats
    assert (stats.backlog_pruned, stats.device_lost, stats.gaps) == (3, 2, 1)
    # The replay: a, the notice of 2, b, and the notice in place of c.
    assert stats.deduplicated == 4
    assert {"gap", "device_lost"} <= set(stats.incomplete)
    _assert_conserved(stream_)


def test_stalled_reader_is_resumed_and_overwrite_reported(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """A consumer that stops taking items closes intake; after stall_timeout the
    core kills adb (stall_resumes) and resumes once intake reopens, without
    ``reconnect``: RECONNECTING with error=None, then the ring check reports
    what the ring overwrote meanwhile as Gap(ring_rotated). Nothing is
    delivered twice, and every missing line lies inside the Gap (design 5.6).
    """
    fake_adb.device(ring=3000)
    payload = "x" * 500
    release = threading.Event()
    records: list[object] = []

    def on_item(entry: LogEntry) -> None:
        release.wait(TIMEOUT)  # stalled until the overwrite is logged
        records.append(entry)

    stream_ = driver.open(
        config(buffer_entries=50, stall_timeout=0.3),
        on_item=on_item,
        on_event=records.append,
    )
    try:
        driver.start(stream_)
        # 1 MB and more: adb blocks on the pipe behind the stalled consumer.
        fake_adb.device_log(*[f"{i} {payload}" for i in range(2000)])
        wait_until(lambda: stream_.stats.stall_resumes == 1, timeout=TIMEOUT)
        wait_until(lambda: not fake_adb.live_children("logcat"), timeout=TIMEOUT)
        # The ring (3000) overwrites the reader's position while it is down.
        fake_adb.device_log(*[f"{i} {payload}" for i in range(2000, 6000)])
    finally:
        release.set()
    wait_until(lambda: any(_seq(e) == 5999 for e in _entries(records)), timeout=TIMEOUT)
    driver.stop(stream_)
    driver.join(stream_)

    delivered = [_seq(e) for e in _entries(records)]
    assert len(delivered) == len(set(delivered))
    assert delivered == sorted(delivered)
    assert delivered[-3000:] == list(range(3000, 6000))  # the ring at the resume
    gaps = _gaps(records)
    assert [gap.reason for gap in gaps] == ["ring_rotated"]
    assert _outside_gaps(fake_adb, delivered, gaps) == []
    resumed = [c for c in _changes(records) if c.new is S.RECONNECTING]
    assert [(c.connection, c.error) for c in resumed] == [(1, None)]
    stats = stream_.stats
    assert (stats.stall_resumes, stats.reconnects, stats.gaps) == (1, 1, 1)
    assert stats.backpressure_seconds > 0
    assert stream_.outcome is None
    _assert_conserved(stream_)


@pytest.mark.parametrize("api", [24, 36])
def test_since_now_discards_up_to_start_marker(
    driver: Driver, fake_adb: FakeAdb, api: int
) -> None:
    """``since="now"``: the start marker (``shell log``, echoed into the running
    logcat; API 24 appends a space to it) ends the discarded prefix, counted in
    before_start; a line logged before start() is never delivered, a line
    logged right after wait_running() returns is, and a reconnect does not
    bring the prefix back (design 2.3, 5.6, A.7).
    """
    fake_adb.device(log_space=api == 24)
    fake_adb.logcat(run(stream(until="cut"), exit_code=255), run(stream()))
    fake_adb.device_log("before start")
    records: list[object] = []
    stream_ = _open(driver, records, since="now")
    driver.start(stream_)
    driver.wait_running(stream_)
    fake_adb.device_log("after wait_running")
    _read(records, 1)
    fake_adb.release("cut")
    first, resumed = _logcat_calls(fake_adb, 2)
    fake_adb.device_log("after the reconnect")
    _read(records, 2)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == ["after wait_running", "after the reconnect"]
    (marker_call,) = fake_adb.calls("log")
    *command, token = marker_call["args"]
    assert command == ["shell", "log", "-t", "logpyt"]
    assert re.fullmatch(r"start-[0-9a-f]{32}", token)
    log = fake_adb.device_entries()
    (marker_at,) = [i for i, (_, line) in enumerate(log) if token in line]
    assert log[marker_at][1].endswith(f"logpyt: {token}" + " " * (api == 24))
    start = _requested(first)
    assert start is not None
    assert start % US == 0  # the clock read (whole seconds) - 1 s
    prefix = [micros for micros, _ in log[:marker_at] if micros >= start]
    resumed_at = _requested(resumed)
    assert resumed_at is not None
    assert resumed_at >= start  # never below the start
    stats = stream_.stats
    assert stats.before_start == len(prefix) + 1  # the marker included
    assert stats.deduplicated >= 1
    _assert_conserved(stream_)


def test_logd_drop_after_backpressure_resumes(
    driver: Driver, fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture
) -> None:
    """A reader that falls behind the ring gets an unbroken prefix, then EOF
    and status 1, before the stall rule fires (API 24 and 36, design A.6).
    With the outbox full while that adb ran, the end resumes like a stall,
    also without ``reconnect``: RECONNECTING with error=None and an INFO, no
    attempt and no stall counted; the resume's ring check reports the hole.
    """
    caplog.set_level(logging.INFO, logger="logpyt")
    fake_adb.device(ring=5000)
    payload = "x" * 1000
    release = threading.Event()
    records: list[object] = []

    def on_item(entry: LogEntry) -> None:
        release.wait(TIMEOUT)
        records.append(entry)

    stream_ = driver.open(
        config(buffer_entries=50, stall_timeout=30.0),
        on_item=on_item,
        on_event=records.append,
    )
    try:
        driver.start(stream_)
        fake_adb.device_log(*[f"{i} {payload}" for i in range(4000)])
        wait_until(lambda: stream_.stats.lines >= 50, timeout=TIMEOUT)
        # The ring overwrites the entry the reader would write next.
        fake_adb.device_log(*[f"{i} {payload}" for i in range(4000, 10_000)])
    finally:
        release.set()
    wait_until(lambda: any(_seq(e) == 9999 for e in _entries(records)), timeout=TIMEOUT)
    driver.stop(stream_)
    driver.join(stream_)

    first = fake_adb.children("logcat")[0]
    status = (
        first.popen.returncode if first.process is None else first.process.returncode
    )
    assert status == 1
    delivered = [_seq(e) for e in _entries(records)]
    assert len(delivered) == len(set(delivered))
    assert delivered[-5000:] == list(range(5000, 10_000))
    gaps = _gaps(records)
    assert [gap.reason for gap in gaps] == ["ring_rotated"]
    assert _outside_gaps(fake_adb, delivered, gaps) == []
    resumed = [c for c in _changes(records) if c.new is S.RECONNECTING]
    assert [(c.connection, c.error) for c in resumed] == [(1, None)]
    assert any(
        "logcat connection 1 ended with status 1 soon after reading" in message
        for message in caplog.messages
    )
    stats = stream_.stats
    assert (stats.stall_resumes, stats.reconnects, stats.gaps) == (0, 1, 1)
    assert stream_.state is S.STOPPED
    assert stream_.outcome is None
    _assert_conserved(stream_)


def test_overlap_shortened_by_the_window_cap(
    driver: Driver, fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture
) -> None:
    """A burst that overflows resume_window_lines inside resume_overlap: the
    resume requests the highest evicted time + 1 us, so the dedupe stays
    exact; overlap_shortened counts it, one INFO per connection, and the
    capture stays complete (design 5.6, 4.4).
    """
    caplog.set_level(logging.INFO, logger="logpyt")
    fake_adb.device()
    fake_adb.logcat(run(stream(until="cut"), exit_code=255), run(stream()))
    t = fake_adb.device_now() - US
    for i in range(10):
        fake_adb.device_log(str(i), at=t + i)
    records: list[object] = []
    stream_ = _open(driver, records, resume_window_lines=4)
    driver.start(stream_)
    _read(records, 10)
    fake_adb.release("cut")
    resumed = _logcat_calls(fake_adb, 2)[1]
    fake_adb.device_log("10")
    _read(records, 11)
    driver.stop(stream_)
    driver.join(stream_)

    assert [_seq(e) for e in _entries(records)] == list(range(11))
    assert _requested(resumed) == t + 5 + 1  # lines 0..5 were evicted
    capped = [m for m in caplog.messages if "resume window reached its cap" in m]
    assert [m.split(":")[0] for m in capped] == [
        "logcat connection 1",
        "logcat connection 2",
    ]
    stats = stream_.stats
    assert (stats.overlap_shortened, stats.deduplicated, stats.gaps) == (1, 4, 0)
    assert stats.complete
    _assert_conserved(stream_)


def test_line_below_request_is_below_cursor(driver: Driver, fake_adb: FakeAdb) -> None:
    """logd returns entries stored after the first one at or above ``-T`` even
    when their time is lower (a line stored late, a clock step after boot):
    the resume drops them as below_cursor, not as replays (design 5.6 step
    2, A.7). The line is 1.5 s late, inside the step bound max(resume_overlap,
    1 s): one stored later than that is taken for a clock step (A.25).
    """
    fake_adb.device()
    fake_adb.logcat(run(stream(until="cut"), exit_code=255), run(stream()))
    t = fake_adb.device_now() - 2 * US
    fake_adb.device_log("a", at=t)
    fake_adb.device_log("late", at=t - 1_500_000)
    fake_adb.device_log("b", at=t + US)  # -T is t - 1 s
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    _read(records, 3)
    fake_adb.release("cut")
    _logcat_calls(fake_adb, 2)
    fake_adb.device_log("c")
    _read(records, 4)
    driver.stop(stream_)
    driver.join(stream_)

    assert _messages(records) == ["a", "late", "b", "c"]
    stats = stream_.stats
    assert (stats.below_cursor, stats.deduplicated, stats.gaps) == (1, 2, 0)
    assert stats.complete
    _assert_conserved(stream_)


def test_reconnect_keeping_ring_has_no_gap(driver: Driver, fake_adb: FakeAdb) -> None:
    """Disconnects that keep logd's ring (a killed device logcat: 143; adb
    reconnect, a server restart: 255; FINDINGS.md check 4), with lines logged
    during each outage: every line arrives exactly once, no Gap, and the
    capture is complete (design 5.6, 4.4, 9.4 resume gate).
    """
    fake_adb.device()
    fake_adb.logcat(
        run(stream(until="cut0"), exit_code=143),
        run(stream(until="cut1"), exit_code=255),
        run(stream(until="cut2"), exit_code=255),
        run(stream()),
    )
    records: list[object] = []
    stream_ = _open(driver, records)
    driver.start(stream_)
    logged = 0
    for cut in range(3):
        fake_adb.device_log(*[str(logged + i) for i in range(20)])
        logged += 20
        _read(records, logged)
        fake_adb.release(f"cut{cut}")
        fake_adb.device_log(*[str(logged + i) for i in range(10)])  # the outage
        logged += 10
        _logcat_calls(fake_adb, cut + 2)
    fake_adb.device_log(*[str(logged + i) for i in range(10)])
    logged += 10
    _read(records, logged)
    driver.stop(stream_)
    driver.join(stream_)

    assert [_seq(e) for e in _entries(records)] == list(range(logged))
    stats = stream_.stats
    assert (stats.reconnects, stats.gaps) == (3, 0)
    assert stats.deduplicated > 0
    assert stats.complete
    causes = [c.error for c in _changes(records) if c.new is S.RECONNECTING]
    assert [getattr(cause, "returncode", None) for cause in causes] == [143, 255, 255]
    _assert_conserved(stream_)
