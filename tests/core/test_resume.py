"""The resume planner alone (design 5.6, A.6, A.7): `logpyt._core.resume`.

The planner is pure, so these tests drive it the way the core does (a clock
read, a position, then each stdout line with its host time) with a fake host
clock. The rule tests pin one rule each; the random tests check the window
against the whole history of lines read, and play a simulated logd (a ring
in arrival order that honors ``-T``, ring rotation, chatty pruning, outages,
readers that logd drops, clock steps) to check the guarantees of answer 4:
no line is delivered twice, and every line lost is inside a reported `Gap`
or counted as reported by the device.
"""

from __future__ import annotations

import bisect
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from logpyt._core.resume import (
    US_PER_SECOND,
    ResumePlanner,
    chatty_lines,
    micros,
    moment,
)
from logpyt.models import LogEntry
from logpyt.streams._events import Gap

S = US_PER_SECOND
BASE = micros(datetime(2026, 11, 4, 5, 11, 16, tzinfo=UTC))
CLOCK = BASE // S + 1  # whole seconds, just after BASE
WALL = datetime(2026, 11, 4, 5, 11, 16)  # noqa: DTZ001  (fallback entries are naive)


def stamp(ts: int) -> str:
    return moment(ts).strftime("%Y-%m-%d %H:%M:%S.%f +0000")


def entry(text: str, ts: int, *, tag: str = "App", parsed: bool = True) -> LogEntry:
    """A live line at epoch microsecond ``ts``; an unparsed one has no time."""
    raw = f"{stamp(ts)} 10065  1234  1234 I {tag}: {text}" if parsed else text
    return LogEntry(
        timestamp=moment(ts) if parsed else WALL,
        pid=1234,
        tid=1234,
        uid=10065,
        uid_raw="10065",
        level="I",
        tag=tag,
        message=text,
        raw=raw,
        parsed=parsed,
    )


def notice(lines: int, ts: int, *, word: str = "expire") -> LogEntry:
    """A logd chatty notice (FINDINGS.md, check 6)."""
    unit = "line" if lines == 1 else "lines"
    return entry(
        f"uid=10014(com.google.android.gms) {word} {lines} {unit}", ts, tag="chatty"
    )


def planner(
    since: Literal["backlog", "now"] | datetime = "backlog",
    *,
    overlap: float = 2.0,
    max_lines: int = 32_768,
    max_bytes: int = 4 * 1024 * 1024,
    keep: bool = True,
) -> ResumePlanner:
    return ResumePlanner(
        since,
        overlap=overlap,
        max_lines=max_lines,
        max_bytes=max_bytes,
        keep=keep,
    )


def connect(
    plan: ResumePlanner, clock: int = CLOCK, *, at: float = 0.0
) -> tuple[Gap | None, int | None]:
    """A clock read and a spawn: the gap and the -T as epoch microseconds."""
    gap = plan.clock(clock, issued=at)
    position = plan.position()
    return gap, None if position is None else position[0] * S + position[1]


def screen(plan: ResumePlanner, *lines: LogEntry, at: float = 0.0) -> list[str]:
    return [plan.screen(line, len(line.raw) + 1, at) for line in lines]


def gaps(plan: ResumePlanner) -> list[Gap]:
    return [item for item in plan.take() if isinstance(item, Gap)]


# -- positions -------------------------------------------------------------------------


def test_backlog_starts_without_T_and_resumes_at_hw_minus_overlap() -> None:  # noqa: N802
    plan = planner()
    assert connect(plan) == (None, None)
    screen(plan, entry("a", BASE), entry("b", BASE + 10))

    gap, request = connect(plan)

    assert gap is None
    assert request == BASE + 10 - 2 * S


def test_nothing_read_resumes_at_the_initial_position() -> None:
    """Design 5.6 step 4: an attempt that read nothing changes nothing."""
    since = moment(BASE - 5 * S)
    plan = planner(since)
    assert connect(plan) == (None, BASE - 5 * S)

    assert connect(plan, CLOCK + 30) == (None, BASE - 5 * S)
    backlog = planner()
    connect(backlog)
    assert connect(backlog, CLOCK + 30) == (None, None)


def test_request_is_never_below_the_start() -> None:
    """A replay must not hand out lines from before since= as new ones."""
    plan = planner(moment(BASE))
    connect(plan)
    screen(plan, entry("a", BASE + S))

    assert connect(plan)[1] == BASE


def test_since_datetime_drops_lines_below_it_until_the_first_at_it() -> None:
    """A.7: logd can return lines below -T; the first connection drops them."""
    plan = planner(moment(BASE))
    connect(plan)

    verdicts = screen(
        plan,
        entry("early", BASE - 1),
        entry("at", BASE),
        entry("late-stored", BASE - 5),
    )

    assert verdicts == ["below_cursor", "new", "new"]


def test_since_now_starts_at_each_clock_until_the_marker() -> None:
    plan = planner("now")
    assert plan.start_marker
    assert not plan.started
    assert connect(plan) == (None, (CLOCK - 1) * S)
    plan.before_start(entry("old", BASE - S), 80, 0.0)

    assert connect(plan, CLOCK + 9) == (None, (CLOCK + 8) * S)
    assert plan.cursor is None  # the earlier attempt's lines are forgotten
    plan.before_start(entry("old", BASE), 80, 0.0)
    plan.marker(entry("start-1", BASE + 5, tag="logpyt"), 80, 0.0)

    assert plan.started
    assert plan.cursor == BASE + 5
    # The floor is that connection's own -T, below hw - overlap here.
    assert connect(plan, CLOCK + 20)[1] == (CLOCK + 8) * S


def test_cap_info_before_a_missed_marker_is_dropped() -> None:
    """A cap INFO queued before the start marker no longer holds once the next
    attempt starts over at its own clock: it is never taken."""
    plan = planner("now", max_lines=2)
    connect(plan)
    for i in range(4):
        plan.before_start(entry(f"pre{i}", BASE + i), 80, 0.0)
    assert any(isinstance(item, str) for item in plan.out)

    connect(plan, CLOCK + 5)  # that connection ended before its marker
    plan.marker(entry("start-2", BASE + 5 * S, tag="logpyt"), 80, 0.0)

    assert plan.take() == []


def test_marker_timestamp_is_the_start_for_chatty_notices() -> None:
    plan = planner("now")
    connect(plan)
    plan.marker(entry("start-1", BASE, tag="logpyt"), 80, 0.0)

    screen(plan, notice(4, BASE - 1), notice(2, BASE))

    assert (plan.backlog_pruned, plan.device_lost) == (4, 2)


# -- the window and the dedupe ---------------------------------------------------------


def test_replay_is_dropped_by_occurrence_count() -> None:
    """Two identical log calls in one microsecond are both kept, once each."""
    plan = planner()
    connect(plan)
    twin = entry("twin", BASE)
    assert screen(plan, twin, twin) == ["new", "new"]

    connect(plan)

    assert screen(plan, twin, twin, twin) == ["deduplicated", "deduplicated", "new"]


def test_matched_copies_are_used_up_before_the_phase_ends() -> None:
    """Design 5.6 step 2: a match decrements its element. The window holds an
    older line the replay lacks, so the phase is still open when a third
    identical copy comes: it is new, not a replay."""
    plan = planner()
    connect(plan)
    twin = entry("twin", BASE)
    screen(plan, entry("a", BASE - 1), twin, twin)
    connect(plan)

    verdicts = screen(plan, twin, twin, twin, entry("b", BASE + 5))

    assert verdicts == ["deduplicated", "deduplicated", "new", "new"]
    assert plan.take() == []


def test_below_request_is_below_cursor_only_inside_the_phase() -> None:
    plan = planner()
    connect(plan)
    screen(plan, entry("a", BASE + 3 * S))
    connect(plan)

    verdicts = screen(
        plan,
        entry("old", BASE),
        entry("a", BASE + 3 * S),  # the whole window matched: the phase ends
        entry("old2", BASE),
    )

    assert verdicts == ["below_cursor", "deduplicated", "new"]
    assert not plan.screening


def test_phase_ends_at_the_first_line_above_hw() -> None:
    plan = planner()
    connect(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + 1))
    connect(plan)

    verdicts = screen(
        plan, entry("b", BASE + 1), entry("next", BASE + 2), entry("a", BASE)
    )

    assert verdicts == ["deduplicated", "new", "new"]  # "a" came too late
    assert plan.take() == []  # the position (b) was replayed: no gap


def test_new_lines_inside_the_phase_go_on() -> None:
    plan = planner()
    connect(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + 5))
    connect(plan)

    verdicts = screen(plan, entry("late", BASE + 2), entry("b", BASE + 5))

    assert verdicts == ["new", "deduplicated"]


def test_unreplayed_position_is_ring_rotated() -> None:
    plan = planner()
    connect(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + 7), entry("c", BASE + 7))
    connect(plan)

    verdicts = screen(plan, entry("b", BASE + 7), entry("d", BASE + S))

    assert verdicts == ["deduplicated", "new"]
    assert gaps(plan) == [
        Gap(moment(BASE + 7), moment(BASE + S), lost=None, reason="ring_rotated")
    ]


def test_replayed_chatty_notice_covers_the_position() -> None:
    """A notice at or below hw in the replay: logd pruned lines we had read."""
    plan = planner()
    connect(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + 7))
    connect(plan)

    verdicts = screen(plan, notice(3, BASE), entry("d", BASE + S))

    assert verdicts == ["deduplicated", "new"]
    assert plan.take() == []
    assert plan.device_lost == 0


def test_ending_inside_the_phase_keeps_the_window() -> None:
    plan = planner()
    connect(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + 1))
    connect(plan)
    assert screen(plan, entry("a", BASE)) == ["deduplicated"]

    connect(plan)  # that connection ended inside its phase

    assert screen(plan, entry("a", BASE), entry("b", BASE + 1)) == [
        "deduplicated",
        "deduplicated",
    ]


def test_line_cap_moves_the_request_and_counts_the_resume() -> None:
    plan = planner(max_lines=2)
    connect(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + 1), entry("c", BASE + 2))
    (info,) = plan.take()
    assert isinstance(info, str)
    assert "resume_window_lines=2" in info
    screen(plan, entry("d", BASE + 3))
    assert plan.take() == []  # one INFO per connection
    assert plan.window_lines == 2

    _gap, request = connect(plan)

    assert request == BASE + 1 + 1  # max_evicted + 1 us
    assert plan.overlap_shortened == 0  # counted once the resume reads a line
    assert screen(plan, entry("b", BASE + 1)) == ["below_cursor"]
    assert plan.overlap_shortened == 1


def test_caps_evict_the_smallest_timestamps_first() -> None:
    """Design 5.6: a line read below hw (a small step back) is evicted before
    hw, so the request stays at or below hw and the ring check stays on."""
    plan = planner(max_lines=2)
    connect(plan)
    screen(plan, entry("a", BASE), entry("c", BASE + 20))
    screen(plan, entry("d", BASE + 5), entry("e", BASE + 6))  # stepped back

    _gap, request = connect(plan)

    assert request == BASE + 5 + 1  # a and d were evicted
    replay = [entry("c", BASE + 20), entry("d", BASE + 5), entry("e", BASE + 6)]
    verdicts = screen(plan, *replay, entry("f", BASE + 30))
    assert verdicts == ["deduplicated", "below_cursor", "deduplicated", "new"]
    assert gaps(plan) == []


def test_byte_cap_below_one_line_keeps_hw() -> None:
    """A line larger than resume_window_bytes at hw stays: the resume can
    still see that the ring lost it."""
    plan = planner(max_bytes=40)
    connect(plan)
    screen(plan, entry("a", BASE))
    assert plan.window_lines == 1

    _gap, request = connect(plan)

    assert request == BASE - 2 * S
    assert screen(plan, entry("b", BASE + S)) == ["new"]
    assert gaps(plan) == [
        Gap(moment(BASE), moment(BASE + S), lost=None, reason="ring_rotated")
    ]


def test_line_cap_below_the_lines_at_hw_keeps_them() -> None:
    """More lines in hw's microsecond than resume_window_lines: they all stay
    (the window exceeds the cap by them), and the replay is checked."""
    plan = planner(max_lines=2)
    connect(plan)
    screen(plan, entry("w", BASE - 1), *[entry(f"x{i}", BASE) for i in range(3)])
    assert plan.window_lines == 3

    _gap, request = connect(plan)

    assert request == BASE  # w was evicted
    replay = [entry(f"x{i}", BASE) for i in range(3)]
    assert screen(plan, *replay, entry("y", BASE + 1)) == [
        "deduplicated",
        "deduplicated",
        "deduplicated",
        "new",
    ]
    assert gaps(plan) == []


def test_byte_cap_evicts_the_oldest() -> None:
    plan = planner(max_bytes=200)
    connect(plan)
    lines = [entry(f"m{i}", BASE + i) for i in range(4)]
    screen(plan, *lines)

    assert plan.window_bytes <= 200
    assert connect(plan)[1] == BASE + (4 - plan.window_lines)


def test_unparsed_line_inherits_the_preceding_timestamp() -> None:
    plan = planner()
    connect(plan)
    garbage = entry("garbage", 0, parsed=False)
    assert screen(plan, garbage) == ["new"]  # no time yet: never windowed
    assert plan.window_lines == 0
    screen(plan, entry("a", BASE), garbage)
    assert plan.window_lines == 2

    connect(plan)

    assert screen(plan, entry("a", BASE), garbage, entry("b", BASE + 1)) == [
        "deduplicated",
        "deduplicated",
        "new",
    ]


def test_unparsed_line_opening_a_replay_is_matched_by_content() -> None:
    """A replay that starts at an unparsed line: it has no time to inherit,
    but the window holds it under the time it inherited when it was read."""
    plan = planner()
    connect(plan)
    first = entry("garbage-1", 0, parsed=False)
    second = entry("garbage-2", 0, parsed=False)
    screen(plan, entry("a", BASE), first, second, entry("b", BASE + 1))
    connect(plan)

    verdicts = screen(plan, first, second, entry("b", BASE + 1), entry("c", BASE + 2))

    assert verdicts == ["deduplicated", "deduplicated", "deduplicated", "new"]
    connect(plan)
    other = entry("other", 0, parsed=False)
    assert screen(plan, other) == ["new"]  # not in the window


def test_window_is_not_kept_without_a_resume() -> None:
    plan = planner(keep=False)
    connect(plan)
    screen(plan, entry("a", BASE))

    assert plan.window_lines == 0
    assert plan.cursor is None


# -- the clock check -------------------------------------------------------------------


def test_clock_step_back_reanchors_once_and_restarts_hw() -> None:
    plan = planner()
    connect(plan, at=0.0)
    old = [entry("a", BASE), entry("b", BASE + S // 2)]
    screen(plan, *old, at=10.0)
    clock = CLOCK - 3600

    gap, request = connect(plan, clock, at=14.0)

    assert gap == Gap(
        moment(BASE + S // 2), moment(clock * S), lost=None, reason="clock_moved_back"
    )
    assert request == clock * S - 4 * S - 2 * S  # N - outage - overlap
    # A second attempt (the first read nothing) reports the step only once.
    assert connect(plan, clock + 1, at=15.0) == (None, (clock + 1) * S - 7 * S)
    verdicts = screen(
        plan, old[1], entry("new", clock * S), old[0], entry("old", BASE - S), at=15.5
    )
    assert verdicts == ["deduplicated", "new", "deduplicated", "deduplicated"]
    assert plan.cursor == clock * S


def test_old_lines_stay_dropped_until_the_clock_passes_them() -> None:
    """After a re-anchor logd returns the old lines on every request above the
    new times; they are replays, so they neither go on nor move hw."""
    plan = planner()
    connect(plan, at=0.0)
    screen(plan, entry("a", BASE), at=1.0)
    clock = CLOCK - 3600
    connect(plan, clock, at=2.0)
    screen(plan, entry("n1", clock * S), at=2.5)

    _gap, request = connect(plan, clock + 1, at=3.0)

    assert request == clock * S - 2 * S
    assert screen(plan, entry("a", BASE), entry("n1", clock * S), at=3.1) == [
        "deduplicated",
        "deduplicated",
    ]
    assert plan.take() == []
    # Once the device clock has passed the old times, nothing is dropped so.
    connect(plan, BASE // S + 1, at=4.0)
    assert screen(plan, entry("again", BASE - 1), at=4.1) == ["new"]


@pytest.mark.parametrize("since", ["now", "datetime"])
def test_resume_after_reanchor_requests_the_new_times(since: str) -> None:
    """The first connection's -T is the floor of every later request, until a
    re-anchor: the new times lie below it, so the next plain resume requests
    the new hw - overlap and the lines after it go on."""
    plan = planner("now" if since == "now" else moment(BASE))
    connect(plan, at=0.0)
    if since == "now":
        plan.marker(entry("start-1", BASE, tag="logpyt"), 80, 0.0)
    screen(plan, entry("a", BASE + S), at=1.0)
    clock = CLOCK - 3600
    gap, _request = connect(plan, clock, at=3.0)
    assert gap is not None
    new = entry("n", clock * S)
    assert screen(plan, new, at=3.1) == ["new"]

    _gap, request = connect(plan, clock + 2, at=5.0)

    assert request == clock * S - 2 * S
    assert screen(plan, new, entry("m", clock * S + S), at=5.1) == [
        "deduplicated",
        "new",
    ]


def test_unread_line_before_the_step_is_delivered_once() -> None:
    """A line logged after the last read and before the clock moved back is
    above the old hw: it is delivered, but hw and the window restart at the
    new clock's first line, so the next resume neither re-anchors again nor
    replays the new lines, and drops that line as a replay."""
    plan = planner()
    connect(plan, at=0.0)
    old = [entry("a", BASE), entry("b", BASE + S // 2)]
    screen(plan, *old, at=1.0)
    clock = CLOCK - 3600
    unread = [entry("u1", BASE + S), entry("u2", BASE + S), entry("u3", BASE + S)]
    gap, _request = connect(plan, clock, at=5.0)
    assert gap is not None
    assert gap.reason == "clock_moved_back"
    new = [entry("o", clock * S - S // 2), entry("n1", clock * S + 1)]

    verdicts = screen(plan, *old, *unread[:2], at=5.1)
    assert verdicts == ["deduplicated", "deduplicated", "new", "new"]
    assert plan.cursor == BASE + S // 2  # unchanged: the old era's

    gap, _request = connect(plan, clock + 1, at=6.0)  # ended inside the old lines

    assert gap is None
    verdicts = screen(plan, *old, *unread, *new, at=6.1)
    assert verdicts == [*["deduplicated"] * 4, "new", "new", "new"]
    assert plan.cursor == clock * S + 1  # restarted at the new clock's lines

    gap, request = connect(plan, clock + 3, at=8.0)

    assert gap is None
    assert request == clock * S + 1 - 2 * S
    later = entry("later", clock * S + 2 * S)
    verdicts = screen(plan, *old, *unread, *new, later, at=8.1)
    assert verdicts == [*["deduplicated"] * 7, "new"]
    assert plan.take() == []


def test_medium_step_matches_the_whole_old_window() -> None:
    """A step back a little more than the overlap: lines of the old window
    stamped between the clock read and the newest time the clock can show
    come after the first line above the clock read; they are still replays.
    (The line that left the window comes again: design 5.6's exception.)"""
    plan = planner()
    t0 = (CLOCK + 10) * S
    connect(plan, CLOCK + 10, at=0.0)
    read = [entry(f"r{i}", t0 + 8 * S + i * S // 2) for i in range(6)]  # 8.0-10.5
    screen(plan, *read, at=10.5)
    clock = CLOCK + 10 + 7  # the clock read shows 7.x s at host 11.0
    gap, request = connect(plan, clock, at=11.0)
    assert gap is not None
    assert request == t0 + 7 * S - S // 2 - 2 * S
    outage = [entry("n1", t0 + 6 * S), entry("n2", t0 + 6 * S + 300_000)]

    verdicts = screen(plan, *read, *outage, at=11.05)

    assert verdicts == ["new", *["deduplicated"] * 5, "new", "new"]


def test_chatty_notice_after_a_step_below_the_start_is_a_loss() -> None:
    """A re-anchor below S lowers S to its request: a notice the new clock
    stamped is a reported loss, not backlog."""
    plan = planner()
    connect(plan, at=0.0)  # S is this clock read
    screen(plan, entry("a", CLOCK * S + 1), at=1.0)
    clock = CLOCK - 3600
    connect(plan, clock, at=3.0)
    screen(plan, entry("n", clock * S), at=3.1)

    assert screen(plan, notice(7, clock * S + 5), at=3.2) == ["new"]

    assert (plan.device_lost, plan.backlog_pruned) == (7, 0)
    assert gaps(plan) == [
        Gap(moment(clock * S), moment(clock * S + 5), 7, "device_reported")
    ]


def test_small_clock_step_is_absorbed_by_the_overlap() -> None:
    plan = planner()
    connect(plan)
    screen(plan, entry("a", (CLOCK + 1) * S))

    assert connect(plan, CLOCK) == (None, (CLOCK - 1) * S)


def test_whole_second_clock_is_no_step_back() -> None:
    """The clock read is whole seconds: hw at .9 s and a clock that reads the
    same second did not move back, even with an overlap below 1 s."""
    plan = planner(overlap=0.5)
    connect(plan)
    screen(plan, entry("a", (CLOCK - 1) * S + 900_000))

    gap, request = connect(plan, CLOCK - 1)

    assert gap is None
    assert request == (CLOCK - 1) * S + 400_000


# -- chatty notices --------------------------------------------------------------------


def test_chatty_notice_in_the_backlog_is_backlog_pruned() -> None:
    plan = planner()
    connect(plan)  # S is this clock read

    assert screen(plan, notice(5, BASE - 60 * S)) == ["new"]

    assert (plan.backlog_pruned, plan.device_lost) == (5, 0)
    assert plan.take() == []


def test_chatty_notice_after_the_start_is_device_lost() -> None:
    plan = planner()
    connect(plan)
    screen(plan, entry("a", CLOCK * S))

    assert screen(plan, notice(1, CLOCK * S + 5)) == ["new"]

    assert plan.device_lost == 1
    assert gaps(plan) == [
        Gap(moment(CLOCK * S), moment(CLOCK * S + 5), 1, "device_reported")
    ]


def test_identical_notice_is_not_counted() -> None:
    plan = planner()
    connect(plan)

    screen(plan, notice(3, CLOCK * S + 1, word="identical"))

    assert (plan.backlog_pruned, plan.device_lost) == (0, 0)
    assert plan.take() == []


@pytest.mark.parametrize(
    ("message", "lines"),
    [
        (
            "uid=10014(com.google.android.gms) com.google.process.gapps expire 4 lines",
            4,
        ),
        ("uid=10014 com.google.android.gms.persistent expire 2 lines", 2),
        ("uid=10014(com.google.android.gms) expire 1 line", 1),
        ("uid=1000(system) Binder:1234_5 identical 3 lines", None),
        ("expire 3 lines", None),
    ],
    ids=["process", "thread", "one", "identical", "bare"],
)
def test_chatty_text(message: str, lines: int | None) -> None:
    """The texts FINDINGS.md check 6 recorded on API 24."""
    assert chatty_lines(entry(message, BASE, tag="chatty")) == lines
    assert chatty_lines(entry(message, BASE)) is None  # another tag


# -- random sequences ------------------------------------------------------------------

_SEEDS = range(60)


@pytest.mark.parametrize("seed", _SEEDS, ids=[f"seed{seed}" for seed in _SEEDS])
def test_window_holds_every_line_at_or_above_the_request(seed: int) -> None:
    """At every resume, the phase expects exactly the lines read at or above
    T_req, which is max(hw - overlap, max_evicted + 1 us, start)."""
    rng = random.Random(seed)  # noqa: S311  (a test, not cryptography)
    since: Literal["backlog"] | datetime = rng.choice(["backlog", moment(BASE + 3 * S)])
    plan = planner(
        since,
        overlap=rng.choice([0.5, 2.0]),
        max_lines=rng.choice([3, 50, 32_768]),
        max_bytes=rng.choice([400, 4096, 1 << 22]),
    )
    history: list[tuple[int, str]] = []  # lines read as new, in order
    ts = BASE
    connect(plan)
    for _ in range(40):
        for _ in range(rng.randint(0, 25)):
            ts += rng.choice([0, 0, 1, 7, 100_000, 900_000])
            line = entry(f"m{rng.randint(0, 3)}", ts - rng.choice([0, 0, 0, 2 * S]))
            if plan.screen(line, len(line.raw) + 1, 0.0) == "new":
                history.append((micros(line.timestamp), line.raw))
        stamps = [item[0] for item in (*plan._window, *plan._late)]
        if plan.window_lines > plan._max_lines or plan.window_bytes > plan._max_bytes:
            assert set(stamps) == {plan.cursor}  # only hw's lines exceed a cap
        connect(plan, ts // S + 1)
        phase = plan._phase
        hw = plan.cursor
        if hw is None:
            continue
        assert phase is not None
        expect = hw - plan._overlap
        if plan._cap_evicted is not None:
            expect = max(expect, plan._cap_evicted + 1)
        if plan._floor is not None:
            expect = max(expect, plan._floor)
        assert phase.floor == expect
        wanted = Counter(item for item in history if item[0] >= expect)
        assert {key: n for key, n in phase.expected.items() if n} == dict(wanted)
        assert phase.at_position == sum(n for (t, _), n in wanted.items() if t == hw)


@dataclass(eq=False)
class _Line:
    """One line in the simulated ring."""

    order: int  # arrival order; a notice takes the order of its newest line
    ts: int
    entry: LogEntry
    replaced: list[_Line] = field(default_factory=list)  # a notice's lines


class _Device:
    """A simulated logd: one ring in arrival order that honors -T (inclusive).

    Time moves only while the device logs: ``elapsed`` (host seconds) and the
    device clock advance together, and a clock step moves the clock alone. A
    chatty notice replaces a run of lines at the position of the newest one,
    with its timestamp (design 5.6 assumes a notice at or below hw replaces
    lines the reader has read).
    """

    def __init__(self, rng: random.Random, capacity: int) -> None:
        self.rng = rng
        self.capacity = capacity
        self.ring: list[_Line] = []
        self.logged: list[_Line] = []
        self.notices: list[_Line] = []
        self.clock = BASE
        self.elapsed = 0.0
        self.order = 0
        self.rotated = -1  # the highest order ring rotation removed

    def log(self, count: int) -> None:
        rng = self.rng
        for _ in range(count):
            advance = rng.choice([0, 0, 1, 3, 50_000, 400_000])
            self.clock += advance
            self.elapsed += advance / S
            last = self.logged[-1] if self.logged else None
            text = f"seq{self.order}"
            same = advance == 0 and last is not None and last.ts == self.clock
            if same and last is not None and rng.random() < 0.5:
                text = last.entry.message  # a byte-identical repeat
            self.order += 1
            item = _Line(self.order, self.clock, entry(text, self.clock))
            self.ring.append(item)
            self.logged.append(item)
            while len(self.ring) > self.capacity:  # ring rotation
                self.rotated = max(self.rotated, self.ring.pop(0).order)

    def prune(self) -> None:
        """Chatty pruning: a run of lines becomes one notice."""
        lines = [i for i, item in enumerate(self.ring) if not item.replaced]
        if len(lines) < 4:
            return
        first = self.rng.choice(lines[:-2])
        run: list[_Line] = []
        for item in self.ring[first : first + self.rng.randint(1, 3)]:
            if item.replaced:
                break
            run.append(item)
        newest = run[-1]
        unit = "line" if len(run) == 1 else "lines"
        note = entry(f"uid=10065 expire {len(run)} {unit}", newest.ts, tag="chatty")
        item = _Line(newest.order, newest.ts, note, run)
        self.ring[first : first + len(run)] = [item]
        self.notices.append(item)

    def start(self, request: int | None) -> int:
        """The ring index a reader at -T ``request`` starts at."""
        if request is None:
            return 0
        for index, item in enumerate(self.ring):
            if item.ts >= request:
                return index
        return len(self.ring)

    def position(self, index: int) -> int:
        """The order just before ring index ``index`` (a reader's position)."""
        if index < len(self.ring):
            return self.ring[index].order - 1
        return self.order

    def after(self, order: int) -> int:
        """The ring index of the first line after ``order``."""
        return bisect.bisect_right(self.ring, order, key=lambda item: item.order)


@dataclass
class _Reader:
    """The planner driven like the core, against a `_Device`."""

    device: _Device
    plan: ResumePlanner
    delivered: Counter[str] = field(default_factory=Counter)
    reported: list[Gap] = field(default_factory=list)
    notices_read: set[int] = field(default_factory=set)
    # Lines logged before the first clock read are not the capture's.
    first: int | None = None
    # The raw text of the lines at hw of every resume, with the most copies of
    # it that ring rotation had removed when such a resume spawned: as many
    # later copies can match their count (see `_explained`).
    at_positions: dict[str, int] = field(default_factory=dict)

    def connect(self) -> int:
        """A clock read and a spawn; the ring index the connection starts at."""
        device = self.device
        if self.first is None:
            self.first = device.order
        gap = self.plan.clock(device.clock // S, issued=device.elapsed)
        if gap is not None:
            self.reported.append(gap)
        self.plan.position()
        phase = self.plan._phase
        if phase is not None and phase.kind == "resume":
            for ts, raw in phase.expected:
                if ts == phase.position:
                    cut = sum(
                        item.entry.raw == raw and item.order <= device.rotated
                        for item in device.logged
                    )
                    self.at_positions[raw] = max(self.at_positions.get(raw, 0), cut)
        return device.start(self.plan._plan.request)

    def read(self, item: _Line) -> None:
        verdict = self.plan.screen(
            item.entry, len(item.entry.raw) + 1, self.device.elapsed
        )
        if item.replaced:
            self.notices_read.add(id(item))
        if verdict == "new":
            self.delivered[item.entry.raw] += 1
        self.reported += gaps(self.plan)

    def check(self) -> None:
        """No duplicate; every line not delivered is a reported loss.

        Byte-identical copies are told apart only by count: of the copies of
        one text, as many as are missing must each lie inside a reported Gap
        or have been replaced by a notice the stream read.
        """
        logged = Counter(item.entry.raw for item in self.device.logged)
        logged.update(item.entry.raw for item in self.device.notices)
        for raw, count in self.delivered.items():
            assert count <= logged[raw], f"delivered {count} times: {raw}"
        first = self.first or 0
        copies: dict[str, list[_Line]] = {}
        for line in self.device.logged:
            copies.setdefault(line.entry.raw, []).append(line)
        for raw, lines in copies.items():
            # Lines in the ring before the capture started may be missing.
            inside = [line for line in lines if line.order > first]
            missing = logged[raw] - self.delivered[raw] - (len(lines) - len(inside))
            if missing <= 0:
                continue
            explained = sum(self._explained(line) for line in inside)
            if self.delivered[raw]:
                # Byte-identical copies at hw: when rotation takes the first of
                # them during an outage, the replay's later copies match their
                # count, so at most as many copies as rotation cut before that
                # resume spawned are lost unseen (the screen knows lines by
                # content and occurrence, design 5.6).
                explained += self.at_positions.get(raw, 0)
            assert explained >= missing, f"lost without a Gap: {raw}"

    def _explained(self, line: _Line) -> bool:
        """Inside a reported Gap, or replaced by a notice the stream read."""
        for gap in self.reported:
            after, before = gap.after, gap.before
            if after is None or before is None:
                continue
            if micros(after) <= line.ts <= micros(before):
                return True
        return any(
            line in note.replaced and id(note) in self.notices_read
            for note in self.device.notices
        )


_SIM_SEEDS = range(200)


@pytest.mark.parametrize("seed", _SIM_SEEDS, ids=[f"seed{seed}" for seed in _SIM_SEEDS])
def test_simulated_resumes_are_duplicate_free_and_report_losses(seed: int) -> None:
    """Answer 4 against a simulated logd (design 5.6, A.6).

    Readers end at random; logd drops a reader whose next line the ring
    rotated away, after an unbroken prefix (A.6); outages let the ring
    rotate; chatty pruning replaces lines with notices; lines share
    timestamps and bytes. Then no line is delivered more often than it was
    logged, and a line that was not delivered lies inside a reported Gap or
    was replaced by a notice that the stream read.
    """
    rng = random.Random(seed)  # noqa: S311  (a test, not cryptography)
    device = _Device(rng, capacity=rng.choice([8, 40, 400]))
    device.log(rng.randint(0, 30))
    # A cap below the lines at one timestamp keeps them all: the caps never
    # evict hw, so the resume can check the ring (see the module docs).
    reader = _Reader(device, planner(max_lines=rng.choice([3, 16, 32_768])))
    for attempt in range(rng.randint(1, 12)):
        last = device.position(reader.connect())  # the reader is before it
        if attempt == 0:
            # The first connection reads a line before anything rotates: a
            # ring that rotates before the capture read anything leaves no
            # position to check (design 5.6 step 4).
            device.log(1)
            item = device.ring[device.after(last)]
            last = item.order
            reader.read(item)
        for _ in range(rng.randint(0, 60)):
            if rng.random() < 0.3:
                device.log(rng.randint(1, 20))  # the device logs while we read
            if rng.random() < 0.05:
                device.prune()
            if device.rotated > last:
                break  # logd dropped the reader: EOF after its prefix (A.6)
            index = device.after(last)
            if index >= len(device.ring):
                continue
            item = device.ring[index]
            last = item.order
            reader.read(item)
        # An outage: the device keeps logging (the ring may rotate) and prunes.
        device.log(rng.choice([0, 3, device.capacity // 2, 2 * device.capacity]))
        if rng.random() < 0.3:
            device.prune()
    # A last connection reads what the ring still holds.
    index = reader.connect()
    for item in device.ring[index:]:
        reader.read(item)

    reader.check()


def test_simulated_clock_steps_deliver_no_duplicate() -> None:
    """Steps back of an hour during outages: each is one clock_moved_back Gap,
    the re-anchored resume requests the new times, the old lines that logd
    keeps returning are dropped, and no line is lost or comes twice."""
    rng = random.Random(7)  # noqa: S311  (a test, not cryptography)
    device = _Device(rng, capacity=60)
    reader = _Reader(device, planner())
    device.log(20)
    for step in range(30):
        index = reader.connect()
        for item in device.ring[index:]:
            reader.read(item)
        if step % 7 == 3:
            device.clock -= 3600 * S  # the step happens during this outage
        device.log(10)
    index = reader.connect()
    for item in device.ring[index:]:
        reader.read(item)

    reader.check()
    steps = [gap for gap in reader.reported if gap.reason == "clock_moved_back"]
    assert len(steps) == 4
    assert [gap for gap in reader.reported if gap not in steps] == []
    logged = Counter(item.entry.raw for item in device.logged)
    assert reader.delivered == logged


def test_micros_and_moment_round_trip() -> None:
    when = datetime(2026, 11, 4, 14, 0, 0, 999_999, tzinfo=UTC) + timedelta(days=1)
    assert moment(micros(when)) == when
