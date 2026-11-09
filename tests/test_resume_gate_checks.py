"""The pass criteria of the resume device gates, without a device (design 9.4).

``tests/device/resume_gate.py`` judges what a stream delivered from a writer
of seq lines and identical bursts; these cases pin that judgement on
synthetic deliveries, so a gate run on the emulator can only fail on what the
device did. The dry runs play the gates themselves on the fake adb: there is
no device in CI, and the release needs them again (design 8.4).
"""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from logpyt import Gap, LogEntry
from logpyt.streams._events import StreamState
from tests.conftest import run, stream
from tests.device.resume_gate import (
    BURST,
    PERIOD,
    Capture,
    Seen,
    check_sequence,
    notice_times,
    run_overwrite_gate,
    run_resume_gate,
    writer_command,
)
from tests.fake_adb import device_line

if TYPE_CHECKING:
    from tests.conftest import FakeAdb

T0 = 1_793_769_076_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _at(micros: int) -> datetime:
    return _EPOCH + timedelta(microseconds=micros)


def _writer(count: int, burst: int) -> list[Seen]:
    """What a writer of ``count`` seqs logs, at 100 us per seq line."""
    lines: list[Seen] = []
    for n in range(count):
        base = T0 + 100 * n
        lines.append(Seen("seq", n, base))
        lines.extend(Seen("burst", n, base + 1 + i // 2) for i in range(burst))
    return lines


def test_complete_delivery_passes() -> None:
    report = check_sequence(_writer(5, 4), [], count=5, burst=4)

    assert report.failures == []
    assert report.collided_bursts == 5  # two copies per microsecond


def test_duplicate_seq_and_extra_burst_fail() -> None:
    lines = _writer(3, 2)
    lines += [Seen("seq", 1, T0 + 100), Seen("burst", 2, T0 + 201)]

    report = check_sequence(lines, [], count=3, burst=2)

    assert report.duplicate_seqs == [1]
    assert report.duplicate_bursts == [2]
    assert len(report.failures) == 2


def test_missing_lines_need_a_gap_between_their_neighbours() -> None:
    lines = [
        s
        for s in _writer(6, 2)
        if not (s.n in {2, 3} or (s.n == 4 and s.kind == "burst"))
    ]
    gap = Gap(
        after=_at(T0 + 101), before=_at(T0 + 400), lost=None, reason="ring_rotated"
    )

    uncovered = check_sequence(lines, [], count=6, burst=2)
    covered = check_sequence(lines, [gap], count=6, burst=2)

    assert uncovered.uncovered == ["seq 2", "seq 3", "burst 2", "burst 3", "burst 4"]
    assert covered.failures == []
    assert (covered.missing_seqs, covered.incomplete_bursts) == (2, 3)


def test_partly_delivered_burst_gap_after_a_copy() -> None:
    """A burst with copies 1-3 delivered and the rest lost: a Gap that starts
    at copy 3 (a chatty notice in place of the rest) covers it."""
    lines = [s for s in _writer(2, 8) if not (s.n == 0 and s.kind == "burst")]
    lines[1:1] = [Seen("burst", 0, T0 + 1 + i) for i in range(3)]
    gap = Gap(after=_at(T0 + 3), before=_at(T0 + 4), lost=5, reason="device_reported")

    report = check_sequence(lines, [gap], count=2, burst=8)

    assert report.failures == []
    assert report.incomplete_bursts == 1


def test_gap_elsewhere_covers_nothing() -> None:
    lines = [s for s in _writer(6, 0) if s.n != 3]
    gap = Gap(
        after=_at(T0 + 500), before=_at(T0 + 600), lost=None, reason="ring_rotated"
    )

    assert check_sequence(lines, [gap], count=6, burst=0).uncovered == ["seq 3"]


def test_missing_tail_needs_a_gap_after_the_last_line() -> None:
    lines = _writer(3, 0)
    gap = Gap(
        after=_at(T0 + 200), before=_at(T0 + 900), lost=None, reason="ring_rotated"
    )

    assert check_sequence(lines, [], count=5, burst=0).uncovered == ["seq 3", "seq 4"]
    assert check_sequence(lines, [gap], count=5, burst=0).failures == []


def test_writer_command_is_one_detached_loop() -> None:
    command = writer_command("0123abcd", count=10, burst=3, pause=True)

    assert command.startswith(
        "rm -f /data/local/tmp/lp-stop-0123abcd; nohup logwrapper sh -c '"
    )
    assert command.endswith("' > /dev/null 2>&1 &")
    loop = command.split("'")[1]
    assert "'" not in loop
    assert loop.count("echo burst=0123abcd-$i;") == 3
    assert "sleep 0.01;" in loop
    # logwrapper can exit without reading what the pipe still holds (A.25).
    assert re.search(r"; sleep 1; echo end=0123abcd-\$i; sleep 1$", loop)


def test_writer_loop_counts_and_honors_the_stop_file() -> None:
    """The order the criteria read (each seq, then its bursts), the count, and the
    stop file the gates use to end it."""
    command = writer_command("0123abcd", count=10, burst=2, pause=True, batch=2)

    loop = command.split("'")[1]
    one = (
        "echo seq=0123abcd-$i; echo burst=0123abcd-$i; echo burst=0123abcd-$i; "
        "i=$((i+1)); "
    )
    assert (
        "i=0; while [ $i -lt 10 ] && [ ! -e /data/local/tmp/lp-stop-0123abcd ]; "
        f"do {one}{one}sleep 0.01; done; "
    ) in loop


def _line(message: str, ts: int, *, tag: str = "sh") -> LogEntry:
    raw = device_line(ts, message, tag=tag, uid=2000)
    return LogEntry(
        timestamp=_at(ts),
        pid=1234,
        tid=1234,
        uid=2000,
        uid_raw="2000",
        level="I",
        tag=tag,
        message=message,
        raw=raw,
        parsed=True,
    )


def test_capture_keeps_the_lines_of_its_run() -> None:
    """Its own run's lines, with API 24's trailing space; other lines are counted
    apart. ``ended`` is the count the end line names."""
    capture = Capture("0123abcd")

    capture.on_item(_line("seq=0123abcd-0 ", T0))
    capture.on_item(_line("burst=0123abcd-0", T0 + 1))
    capture.on_item(_line("seq=ffffffff-7", T0 + 2))
    capture.on_item(_line("other", T0 + 3))
    assert capture.ended() is None
    capture.on_item(_line("seq=0123abcd-1", T0 + 4))
    capture.on_item(_line("end=0123abcd-2 ", T0 + 5))

    assert capture.lines == [
        Seen("seq", 0, T0),
        Seen("burst", 0, T0 + 1),
        Seen("seq", 1, T0 + 4),
        Seen("end", 2, T0 + 5),
    ]
    assert capture.other_lines == 2
    assert (capture.count("seq"), capture.ended()) == (2, 2)


def test_capture_stalls_once() -> None:
    """The overwrite gate's slow consumer stops at the first writer line only."""
    capture = Capture("0123abcd", stall=0.05)
    capture.abort.set()  # not the time, only the count of stalls matters

    capture.on_item(_line("other", T0))
    assert not capture.stalled.is_set()
    capture.on_item(_line("seq=0123abcd-0", T0 + 1))
    assert capture.stalled.is_set()
    assert capture.resumed.is_set()
    capture.resumed.clear()
    capture.on_item(_line("seq=0123abcd-1", T0 + 2))

    assert not capture.resumed.is_set()


def test_notice_times_by_the_replaced_lines() -> None:
    """A notice stands where its lines stood; its time against theirs."""
    rid = "0123abcd"

    def seq(n: int, ts: int) -> str:
        return device_line(ts, f"seq={rid}-{n}", tag="sh", uid=2000)

    def notice(ts: int) -> str:
        return device_line(ts, "uid=2000 expire 2 lines", tag="chatty", uid=2000)

    live = {n: T0 + 10 * n for n in range(9)}
    dump = [
        seq(0, live[0]),
        notice(live[2]),  # replaced 1 and 2: the newest one's time
        seq(3, live[3]),
        notice(live[4]),  # replaced 4 and 5: the oldest one's time
        seq(6, live[6]),
        notice(live[7] + 1),  # nothing of the writer replaced: left out
        seq(7, live[7]),
        seq(8, live[8]),
    ]

    assert notice_times(dump, rid, live) == ["newest", "oldest"]


@pytest.mark.parametrize("where", ["ahead", "behind"])
def test_notice_times_leave_out_other_uids(where: str) -> None:
    """A hole can hold other uids' notices, stamped by their own lines (check 6
    recorded 52 notices of 3 uids in one dump): they say nothing of the writer's,
    wherever they stand."""
    rid = "0123abcd"
    live = {n: T0 + 10 * n for n in range(4)}
    mine = device_line(live[2], "uid=2000 expire 2 lines", tag="chatty", uid=2000)
    other = device_line(
        live[1] - 3, "uid=10014 expire 3 lines", tag="chatty", uid=10014
    )
    hole = [other, mine] if where == "ahead" else [mine, other]

    dump = [
        device_line(live[0], f"seq={rid}-0", tag="sh", uid=2000),
        *hole,
        device_line(live[3], f"seq={rid}-3", tag="sh", uid=2000),
    ]

    assert notice_times(dump, rid, live) == ["newest"]


def test_notice_times_other_uid_alone_is_no_verdict() -> None:
    rid = "0123abcd"
    live = {n: T0 + 10 * n for n in range(4)}

    dump = [
        device_line(live[0], f"seq={rid}-0", tag="sh", uid=2000),
        device_line(live[1], "uid=1000 expire 3 lines", tag="chatty", uid=1000),
        device_line(live[3], f"seq={rid}-3", tag="sh", uid=2000),
    ]

    assert notice_times(dump, rid, live) == []


def test_notice_times_several_writer_notices_in_one_hole() -> None:
    """Each at its own lines' time: they ascend and the last is the newest, so the
    hole keeps the rule; out of order they do not."""
    rid = "0123abcd"
    live = {n: T0 + 10 * n for n in range(4)}

    def dump(*times: int) -> list[str]:
        return [
            device_line(live[0], f"seq={rid}-0", tag="sh", uid=2000),
            *[
                device_line(ts, "uid=2000(shell) expire 1 line", tag="chatty", uid=2000)
                for ts in times
            ],
            device_line(live[3], f"seq={rid}-3", tag="sh", uid=2000),
        ]

    assert notice_times(dump(live[1], live[2]), rid, live) == ["newest"]
    assert notice_times(dump(live[2], live[1]), rid, live) == ["other"]


def test_report_of_a_run_that_ended_first_does_not_claim_a_count() -> None:
    """Without the end line the delivered prefix was judged: nothing is known of
    what the writer logged after it."""
    report = check_sequence(_writer(3, 0), [], count=3, burst=0)
    assert report.summary()["missing_seqs"] == 0

    report.count_known = False

    assert report.summary()["missing_seqs"] is None


# -- dry runs of the gates on the fake adb ---------------------------------------------


class FakeBench:
    """`Bench` on the fake adb: a writer thread, and ``stream`` runs it can cut.

    The writer logs at a rate set by the time, not by its ticks, so a loaded
    machine changes its bursts and not how fast it wraps the ring.
    """

    kinds: tuple[str, ...] = ("logcat_killed", "adb_reconnect")

    def __init__(self, fake: FakeAdb) -> None:
        self.fake = fake
        self.adb_path: str = fake.path
        self.serial: str | None = None
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.cuts = 0

    def start_writer(
        self, rid: str, *, count: int, burst: int, pause: bool, batch: int = 1
    ) -> None:
        del pause  # always paced here
        self.thread = threading.Thread(
            target=self._write, args=(rid, count, burst, batch), daemon=True
        )
        self.thread.start()

    def _line(self, message: str, ts: int | None = None) -> None:
        self.fake.device_log(message, tag="sh", uid="shell", pid=4242, at=ts)

    def _write(self, rid: str, count: int, burst: int, batch: int) -> None:
        began = time.monotonic()
        i = 0
        self._line(f"seq=ffffffff-{i}")  # another run's line
        while i < count and not self.stop.is_set():
            due = int((time.monotonic() - began) / PERIOD) * batch
            for _ in range(max(due - i, 0)):
                # One microsecond, and API 24's trailing space on some.
                now = self.fake.device_now()
                self.fake.device_log(
                    f"seq={rid}-{i} ",
                    *[f"burst={rid}-{i}"] * burst,
                    tag="sh",
                    uid="shell",
                    pid=4242,
                    at=now,
                )
                i += 1
            self.stop.wait(PERIOD)
        self._line(f"end={rid}-{i}")

    def stop_writer(self, rid: str) -> None:
        del rid
        self.stop.set()
        if self.thread is not None:
            self.thread.join(30.0)

    def disconnect(self, kind: str) -> None:
        del kind
        self.fake.release(f"c{self.cuts}")
        self.cuts += 1

    def log(self, tag: str, message: str) -> None:
        self.fake.device_log(message, tag=tag)

    def dump(self) -> list[str]:
        return [line for _, line in self.fake.device_entries()]


@pytest.mark.watchdog(120)
def test_resume_gate_dry_run(fake_adb: FakeAdb) -> None:
    """The resume gate's whole scenario on the fake adb: it passes, and its
    disconnects and collided bursts happened."""
    fake_adb.device()
    fake_adb.logcat(
        *[run(stream(until=f"c{i}"), exit_code=143 + 112 * (i % 2)) for i in range(2)],
        run(stream()),
    )

    capture, report, stream_, made = run_resume_gate(
        FakeBench(fake_adb), 2.0, disconnects=2
    )

    assert report.failures == []
    assert made == ["logcat_killed", "adb_reconnect"]
    assert report.missing_seqs == 0
    assert report.collided_bursts == capture.count("seq")
    assert capture.count("burst") == BURST * capture.count("seq")
    assert capture.other_lines >= 1
    assert [gap.reason for gap in capture.gaps] == []
    assert stream_.stats.reconnects == 2
    assert stream_.stats.deduplicated > 0
    assert stream_.outcome is None
    assert fake_adb.survivors() == []


@pytest.mark.watchdog(120)
def test_overwrite_gate_dry_run(fake_adb: FakeAdb) -> None:
    """The overwrite gate on the fake adb: a small outbox and a stall that lasts
    while the writer wraps the ring; the ring is large against the writer's rate,
    so the reader keeps up after the stall."""
    fake_adb.device(ring=400)
    fake_adb.logcat(run(stream()))

    capture, report, stream_ = run_overwrite_gate(
        FakeBench(fake_adb),
        4.0,
        stall_timeout=0.5,
        batch=2,  # 200 lines/s: the ring holds 2 s, and wraps during the stall
        tail=1.0,
        buffer_entries=100,
    )

    assert report.failures == []
    assert report.count_known
    assert stream_.stats.stall_resumes >= 1
    assert any(gap.reason == "ring_rotated" for gap in capture.gaps)
    assert report.missing_seqs > 0
    assert stream_.outcome is None
    assert stream_.state is StreamState.STOPPED
    assert fake_adb.survivors() == []
