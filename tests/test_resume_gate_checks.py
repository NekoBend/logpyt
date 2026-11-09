"""The pass criteria of the resume device gates, without a device (design 9.4).

``tests/device/resume_gate.py`` judges what a stream delivered from a writer
of seq lines and identical bursts; these cases pin that judgement on
synthetic deliveries, so a gate run on the emulator can only fail on what the
device did.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from logpyt import Gap
from tests.device.resume_gate import Seen, check_sequence, notice_times, writer_command
from tests.fake_adb import device_line

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
