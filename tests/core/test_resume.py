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


def underway(plan: ResumePlanner, *, at: float = 0.0) -> tuple[Gap | None, int | None]:
    """A first clock read well before the lines a test reads, so that they are
    the capture's and a step among them is reported (design A.25: a line
    logged before the first clock read lies across a step from before it)."""
    return connect(plan, BASE // S - 4, at=at - 10.0)


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


@pytest.mark.parametrize("bound", ["overlap", "cap"])
def test_late_line_after_the_last_match_is_below_cursor(bound: str) -> None:
    """The phase ends at the first line above hw, not at the last match: a
    line read before and stored late below the request, which the replay
    returns after the window's last element, is dropped as below_cursor. Its
    timestamp is within max(resume_overlap, 1 s) of hw, so it is no step."""
    if bound == "overlap":  # T_req = hw - 0.5 s; the step bound is hw - 1 s
        plan = planner(overlap=0.5)
        lines = [entry("a", BASE), entry("b", BASE + 400_000)]
        late = entry("late", BASE - 300_000)
    else:  # the cap evicts "late": T_req = its time + 1 us
        plan = planner(max_lines=2)
        lines = [entry("a", BASE + 1), entry("b", BASE + 2)]
        late = entry("late", BASE)
    connect(plan)
    assert screen(plan, *lines, late) == ["new", "new", "new"]
    plan.take()
    connect(plan)

    verdicts = screen(plan, *lines, late)

    assert verdicts == ["deduplicated", "deduplicated", "below_cursor"]
    assert plan.screening  # every element matched, but no line above hw yet
    assert screen(plan, entry("c", BASE + S)) == ["new"]
    assert not plan.screening
    assert gaps(plan) == []


def test_step_after_the_replay_is_seen_live() -> None:
    """A phase that never sees a line above hw does not drop new lines
    forever: once the replay is over (every element matched), a line below
    hw - max(resume_overlap, 1 s) is a clock step seen live, while inside
    the replay it is below_cursor (design A.7: logd returns such lines)."""
    plan = planner()
    underway(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + S), at=1.0)
    connect(plan, at=2.0)
    far = BASE - 3600 * S

    assert screen(plan, entry("a", BASE), entry("x", far), at=2.1) == [
        "deduplicated",
        "below_cursor",
    ]
    assert plan.take() == []
    verdicts = screen(plan, entry("b", BASE + S), entry("y", far + 1), at=2.2)

    assert verdicts == ["deduplicated", "new"]
    assert gaps(plan) == [
        Gap(moment(BASE + S), moment(far + 1), lost=None, reason="clock_moved_back")
    ]
    assert plan.cursor == far + 1
    assert not plan.screening


def test_reanchor_delivers_the_lines_below_its_request_after_the_old_window() -> None:
    """A clock step back while the reader lags behind by more than the
    overlap, and the connection ends before it read a line logged after the
    step. The re-anchor requests N - outage - overlap, the outage counted
    from the last line read, which lies above those lines: logd returns them
    after the old window, whose replay ends the below_cursor rule (they were
    never read). The lines logged before the step and never read come once.
    Then a line below the new hw - max(resume_overlap, 1 s) is a step seen
    live."""
    plan = planner()
    t0 = BASE + 1000 * S
    connect(plan, t0 // S, at=0.0)
    read = [entry(f"a{i}", t0 + i * S // 10) for i in range(10)]
    screen(plan, *read, at=0.9)
    lagged = [entry(f"p{i}", t0 + S + i * S // 10) for i in range(5)]
    screen(plan, *lagged[:3], at=5.0)  # logged at host 1.0-1.2, read at 5.0
    back = t0 - 30 * S  # the device clock steps back 30 s at host 1.5
    after = [entry(f"e{i}", back + 3 * S // 2 + i * S // 10) for i in range(35)]

    gap, request = connect(plan, (back + 6 * S) // S, at=6.0)

    hw = t0 + S + 2 * S // 10
    assert gap == Gap(moment(hw), moment(back + 6 * S), None, "clock_moved_back")
    assert request == back + 6 * S - S - 2 * S  # N - outage - overlap
    verdicts = screen(plan, *read, *lagged, *after, at=6.5)
    assert verdicts == [*["deduplicated"] * 13, *["new"] * 37]
    assert plan.take() == []
    new_hw = micros(after[-1].timestamp)
    assert plan.cursor == new_hw
    assert screen(plan, entry("low", back), at=6.6) == ["new"]
    assert gaps(plan) == [Gap(moment(new_hw), moment(back), None, "clock_moved_back")]


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
    # An old line ahead of the new one, and the lines read after it.
    verdicts = screen(
        plan, entry("old", BASE - S), entry("new", clock * S), *old, at=15.5
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
    # Once the device clock has passed the old times, a line the new clock
    # stamped among them is new: the replay reaches it after the old line
    # (the old window) and the new one.
    connect(plan, BASE // S + 1, at=4.0)
    replay = [entry("a", BASE), entry("n1", clock * S), entry("again", BASE - 1)]
    assert screen(plan, *replay, at=4.1) == ["deduplicated", "deduplicated", "new"]


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


def test_live_step_back_reports_a_gap_and_restarts_hw() -> None:
    """A line read outside a phase stamped below hw - max(resume_overlap, 1 s)
    is a clock step seen live: Gap(clock_moved_back, after=hw, before=its
    time) at once, the line is delivered, and hw and the window restart at
    it. The next resume requests the new times, drops the old lines (above
    every time the clock can show) and replays the new ones."""
    plan = planner()
    underway(plan)
    old = [entry("a", BASE), entry("b", BASE + S // 2)]
    screen(plan, *old, at=1.0)
    stepped = BASE - 3600 * S
    new = [entry("c", stepped), entry("d", stepped + 1)]

    assert screen(plan, *new, at=2.0) == ["new", "new"]

    assert gaps(plan) == [
        Gap(
            moment(BASE + S // 2), moment(stepped), lost=None, reason="clock_moved_back"
        )
    ]
    assert plan.cursor == stepped + 1
    gap, request = connect(plan, stepped // S + 3, at=4.0)
    assert gap is None
    assert request == stepped + 1 - 2 * S
    verdicts = screen(plan, *old, *new, entry("e", stepped + 2 * S), at=4.1)
    assert verdicts == [*["deduplicated"] * 4, "new"]
    assert plan.take() == []


@pytest.mark.parametrize("where", ["below", "above"])
def test_step_from_before_the_capture_gives_no_gap(where: str) -> None:
    """A backlog that reads across a step from before the first clock read
    loses nothing: no Gap, and S stays (user decision, design A.25). The line
    at hw is stamped below that read's second, or above every time that clock
    could show when it was read; hw and the window restart at the new line."""
    old = BASE - 60 * S if where == "below" else BASE + 3600 * S
    new = old - 90 * S if where == "below" else BASE - 10 * S
    plan = planner()
    connect(plan)
    start = plan._start

    assert screen(plan, entry("old", old), entry("new", new)) == ["new", "new"]

    assert plan.take() == []
    assert plan.cursor == new
    assert plan._start == start
    # The next resume requests the new times and replays neither era twice.
    gap, request = connect(plan, CLOCK + 1, at=2.0)
    assert gap is None
    assert request == new - 2 * S
    verdicts = screen(plan, entry("old", old), entry("new", new), at=2.1)
    assert verdicts == ["deduplicated", "deduplicated"]
    assert plan.take() == []


def test_chatty_notice_after_a_step_from_before_the_capture_is_backlog() -> None:
    """S stays at the first clock read, so a notice stamped below it is
    backlog pruning, not a loss of this capture."""
    plan = planner()
    connect(plan)
    screen(plan, entry("old", BASE - 60 * S), entry("new", BASE - 150 * S))
    notice = entry("uid=10065 expire 3 lines", BASE - 149 * S, tag="chatty")

    assert screen(plan, notice) == ["new"]

    assert (plan.backlog_pruned, plan.device_lost) == (3, 0)
    assert plan.take() == []


def test_resume_across_a_step_from_before_the_capture_gives_no_gap() -> None:
    """A connection that ended on the far side of a step from before the
    capture: the resume's clock read is below hw, but no later than the first
    read says it should be, so it re-anchors with no Gap, and S stays."""
    plan = planner()
    connect(plan)
    start = plan._start
    old = BASE + 3600 * S
    screen(plan, entry("old", old), at=0.5)

    gap, request = connect(plan, CLOCK + 1, at=1.0)

    assert gap is None
    assert request == (CLOCK + 1) * S - round(0.5 * S) - 2 * S
    screen(plan, entry("old", old), at=1.1)
    assert screen(plan, entry("new", (CLOCK + 1) * S), at=1.2) == ["new"]
    assert plan.take() == []
    assert plan._start == start


def test_clock_read_that_fell_since_the_first_is_reported() -> None:
    """The line at hw predates the capture, but the clock read shows the clock
    moved back since the first read: that step is the capture's, so the Gap
    comes, and later steps are reported whatever their times."""
    plan = planner()
    connect(plan, at=0.0)
    screen(plan, entry("old", BASE - 60 * S), at=1.0)

    gap, _ = connect(plan, CLOCK - 3600, at=10.0)

    assert gap is not None
    assert gap.reason == "clock_moved_back"
    assert plan._step_reported


def test_step_in_the_first_second_is_not_reported() -> None:
    """The known cost (design A.25): with nothing read from past the first
    clock read's second, a step seen in a line cannot be told from one before
    the capture. Every line is still delivered, once."""
    plan = planner()
    connect(plan)
    t = CLOCK * S  # inside the first read's second

    assert screen(plan, entry("a", t), entry("b", t - 3600 * S)) == ["new", "new"]

    assert plan.take() == []
    assert plan.cursor == t - 3600 * S


def test_marker_after_a_step_from_before_the_capture_restarts_the_window() -> None:
    """since="now": lines ahead of the marker from before a step (stamped above
    every time the clock can show) give no Gap. The marker is windowed, later
    requests stay at or above it, and a resume replays nothing as new."""
    plan = planner("now")
    connect(plan)
    marker_at = CLOCK * S + 1000
    plan.before_start(entry("old", BASE + 3600 * S), 40, 0.1)
    plan.marker(entry("start-marker", marker_at, tag="logpyt"), 40, 0.2)
    new = marker_at + S // 2

    assert screen(plan, entry("a", new), at=0.3) == ["new"]

    assert plan.take() == []
    assert plan.cursor == new
    gap, request = connect(plan, CLOCK + 1, at=1.0)
    assert gap is None
    assert request == marker_at
    verdicts = screen(
        plan,
        entry("old", BASE + 3600 * S),
        entry("start-marker", marker_at, tag="logpyt"),
        entry("a", new),
        at=1.1,
    )
    assert verdicts == ["deduplicated"] * 3


def test_live_step_bound_is_max_of_overlap_and_1s() -> None:
    """Lines up to max(resume_overlap, 1 s) below hw are no step (stored late,
    or the whole-second bound of the resume's clock check); below that, a
    line also raises the Gap: one stored that late cannot be told from a
    step (the known cost of detecting steps live)."""
    plan = planner()
    underway(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + 3 * S))
    assert screen(plan, entry("late", BASE + S)) == ["new"]  # 2 s late
    assert plan.take() == []
    assert screen(plan, entry("later", BASE + S - 1)) == ["new"]
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]
    small = planner(overlap=0.5)
    underway(small)
    screen(small, entry("a", BASE + S))
    assert screen(small, entry("late", BASE + 1)) == ["new"]  # 1 s - 1 us late
    assert small.take() == []
    assert screen(small, entry("later", BASE - 1)) == ["new"]
    assert [gap.reason for gap in gaps(small)] == ["clock_moved_back"]


def test_live_step_lowers_the_start_and_the_floor() -> None:
    """A step seen live lowers S and the first connection's -T (the floor) to
    its own request, as the resume's re-anchor does: a chatty notice the new
    clock stamped is a loss, and the next resume requests the new times."""
    plan = planner(moment(BASE))
    underway(plan)
    screen(plan, entry("a", BASE + S), at=1.0)
    stepped = BASE - 3600 * S
    screen(plan, entry("n", stepped), at=2.0)
    plan.take()

    assert screen(plan, notice(7, stepped + 5), at=2.1) == ["new"]

    assert (plan.device_lost, plan.backlog_pruned) == (7, 0)
    assert gaps(plan) == [
        Gap(moment(stepped), moment(stepped + 5), 7, "device_reported")
    ]
    _gap, request = connect(plan, stepped // S + 2, at=4.0)
    assert request == stepped + 5 - 2 * S


def test_live_step_keeps_the_old_window_for_the_next_resume() -> None:
    """After a step seen live, the next resume's -T lies below the old lines,
    so logd returns them first. Those the device clock can have stamped are
    told by the old window, kept for that: no ring check fires on them, and
    the new lines that follow are replays."""
    plan = planner()
    underway(plan)
    old = [entry("a", BASE), entry("b", BASE + S)]
    screen(plan, *old, at=1.0)
    new = entry("c", BASE - 2 * S)
    assert screen(plan, new, at=2.0) == ["new"]  # 3 s below hw: a step
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]

    gap, request = connect(plan, BASE // S - 1, at=3.0)  # a and b <= N + 2 s

    assert gap is None
    assert request == BASE - 4 * S
    verdicts = screen(plan, *old, new, entry("d", BASE - S + 1), at=3.1)
    assert verdicts == ["deduplicated", "deduplicated", "deduplicated", "new"]
    assert plan.take() == []


def test_old_lines_outside_the_window_dropped_before_new_ones() -> None:
    """The resume after a step: logd returns the old lines first, from the
    first one at or above -T. A line not in either window and stamped below
    the new hw cannot have been logged after hw: it is an old one, so the
    replay is known to be in the old lines, and every line up to the old hw
    that matches no window is dropped (deduplicated) until the replay
    reaches the new lines (design 5.6's exception does not occur)."""
    plan = planner()
    underway(plan)
    old = [entry("a0", BASE - 3 * S), entry("a", BASE), entry("b", BASE + S)]
    screen(plan, *old, at=1.0)
    stepped = BASE - 2 * S - S // 2
    new = [entry("c", stepped)]
    screen(plan, *new, at=2.0)
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]

    gap, request = connect(plan, BASE // S - 1, at=3.0)

    assert gap is None
    assert request == stepped - 2 * S
    verdicts = screen(plan, *old, *new, entry("d", BASE - S), at=3.1)
    assert verdicts == [*["deduplicated"] * 4, "new"]
    assert plan.take() == []


def test_live_step_then_the_clock_catches_up() -> None:
    """A step seen live, then an outage that ends after the device clock has
    passed the old hw: the resume's clock check sees no step, its -T lies in
    the new times, and logd returns every old line at or above it first.
    Every old line is in the clock's past, so they are dropped, the new
    lines read before are replays, and only the outage's lines are new (the
    verifier's probe: 50 duplicates and no Gap before)."""
    plan = planner()
    t0 = CLOCK * S
    connect(plan, CLOCK, at=0.0)
    old = [entry(f"old{k}", t0 + k * 100_000) for k in range(101)]  # 0.0-10.0 s
    after = [entry(f"after{k}", t0 + k * 100_000 + 50) for k in range(50)]
    screen(plan, *old, at=10.0)
    assert screen(plan, *after, at=15.0) == ["new"] * 50  # 10 s back: 0.0-4.9 s
    assert gaps(plan) == [
        Gap(moment(t0 + 10 * S), moment(t0 + 50), lost=None, reason="clock_moved_back")
    ]
    outage = [entry(f"outage{k}", t0 + 5 * S + k * 100_000 + 50) for k in range(50)]

    gap, request = connect(plan, CLOCK + 10, at=20.0)

    assert gap is None
    assert request == t0 + 4_900_050 - 2 * S
    ring = [*old, *after, *outage]
    assert request is not None
    first = next(i for i, line in enumerate(ring) if micros(line.timestamp) >= request)
    replay = ring[first:]
    verdicts = screen(plan, *replay, at=20.1)
    delivered = [
        line.message for line, v in zip(replay, verdicts, strict=True) if v == "new"
    ]
    assert delivered == [line.message for line in outage]
    assert plan.take() == []


def _from(ring: list[LogEntry], request: int | None) -> list[LogEntry]:
    """What logd returns for ``-T request``: from the first line at or above it."""
    assert request is not None
    first = next(i for i, line in enumerate(ring) if micros(line.timestamp) >= request)
    return ring[first:]


@pytest.mark.parametrize(
    ("first", "step"),
    [("in_order", 30), ("late", 30), ("second_step", 30), ("late", 3600)],
)
def test_stepped_replay_that_lost_the_new_lines_delivers_the_later_ones(
    first: str, step: int
) -> None:
    """A step seen live, then logd drops the reader (A.6) after its ring lost
    every line read since the step. The replay's first line comes in order,
    50 ms late, or after a second step of 0.5 s. A line below the new hw, and
    after it the lines the device logged before the clock read (below N +
    1 s), can be old ones and are dropped; the ring check at the first line
    after them reports the hole, and every line from N + 1 s on is
    delivered (they were dropped up to the old hw: 30 s or an hour)."""
    plan = planner()
    t0 = BASE + 1000 * S
    connect(plan, t0 // S - 4, at=-10.0)
    screen(plan, *[entry(f"a{i}", t0 + i * S // 10) for i in range(10)], at=0.9)
    back = t0 - step * S  # the device clock steps back at host 1.0
    read = [entry(f"b{i}", back + S + i * S // 10) for i in range(3)]
    assert screen(plan, *read, at=1.2) == ["new"] * 3
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]
    hw = back + S + 2 * S // 10
    shift = -S // 2 if first == "second_step" else 0  # the second step, host 1.25
    outage = [
        entry(f"c{i}", back + shift + 13 * S // 10 + i * S // 10) for i in range(10)
    ]
    if first == "late":
        outage.insert(0, entry("late", hw - 50_000))
    clock = (back + shift + 5 * S // 2) // S  # read at host 2.5

    gap, request = connect(plan, clock, at=2.5)

    assert gap is None
    assert request == hw - 2 * S
    assert plan._phase is not None
    assert plan._phase.stepped
    verdicts = screen(plan, *outage, at=2.6)
    live = [entry(f"d{i}", clock * S + S + i * S // 10) for i in range(100)]
    for line in live:  # host = device time - back - shift
        verdicts.append(
            plan.screen(
                line, len(line.raw) + 1, (micros(line.timestamp) - back - shift) / S
            )
        )
    dropped = [
        line.message
        for line, verdict in zip([*outage, *live], verdicts, strict=True)
        if verdict != "new"
    ]
    if first == "in_order":
        assert dropped == []
        before = outage[0]
    else:
        assert dropped == [line.message for line in outage]  # all below N + 1 s
        before = live[0]
    assert gaps(plan) == [Gap(moment(hw), before.timestamp, None, "ring_rotated")]


def test_caught_up_replay_that_lost_the_new_lines_drops_the_outage_with_a_gap() -> None:
    """A step seen live, an outage until the device clock has passed the old
    hw, and a replay that lost every line read since the step: every line up
    to the old hw was logged before the clock read, so the lines the outage
    logged among the old times are dropped as old ones (they were never
    read), and the Gap(ring_rotated) at the first line above the old hw
    holds them in its range (documented: an old line cannot be told from
    them)."""
    plan = planner()
    t0 = BASE + 1000 * S
    connect(plan, t0 // S, at=0.0)
    screen(plan, *[entry(f"a{i}", t0 + i * S // 10) for i in range(10)], at=0.9)
    back = t0 - 30 * S  # the device clock steps back 30 s at host 1.0
    screen(plan, *[entry(f"b{i}", back + S + i * S // 10) for i in range(3)], at=1.2)
    plan.take()
    outage = [entry(f"c{i}", back + 30 * S + i * S // 20) for i in range(20)]

    _gap, request = connect(plan, (back + 41 * S) // S, at=41.0)

    assert request == back + S + 2 * S // 10 - 2 * S
    verdicts = screen(plan, *outage, at=41.1)
    assert verdicts == [*["deduplicated"] * 19, "new"]  # c19 is above the old hw
    (gap,) = gaps(plan)
    assert gap == Gap(
        moment(back + S + 2 * S // 10), outage[-1].timestamp, None, "ring_rotated"
    )
    assert gap.after is not None
    assert gap.before is not None
    assert all(gap.after <= line.timestamp <= gap.before for line in outage)


def test_live_step_ceiling_drops_old_lines_that_left_the_window() -> None:
    """After a step seen live, the old hw is the ceiling: a later replay's
    old lines stamped above every time the device clock can show are
    dropped, also the ones that left the old window, with no Gap."""
    plan = planner()
    underway(plan)
    old = [entry("a0", BASE - 3 * S), entry("a", BASE), entry("b", BASE + S)]
    screen(plan, *old, at=1.0)
    stepped = BASE - 3600 * S
    assert screen(plan, entry("c", stepped), at=2.0) == ["new"]
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]

    gap, _request = connect(plan, stepped // S + 3, at=4.0)

    assert gap is None
    verdicts = screen(plan, *old, entry("c", stepped), entry("d", stepped + S), at=4.1)
    assert verdicts == [*["deduplicated"] * 4, "new"]
    assert plan.take() == []


def test_live_step_ceiling_drops_the_whole_ring_of_old_lines() -> None:
    """The resume 3 s after a step of an hour seen live: every old line in
    the ring lies above -T, so logd returns the whole ring (200 lines left
    the old window); only the lines logged during the outage are new."""
    plan = planner()
    connect(plan, BASE // S - 20, at=0.0)
    old = [entry(f"old{k}", BASE - 20 * S + k * S // 10) for k in range(201)]
    screen(plan, *old, at=20.0)
    stepped = BASE - 3600 * S
    new = [entry(f"new{k}", stepped + k * S // 10) for k in range(10)]
    screen(plan, *new, at=21.0)
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]
    outage = [entry(f"out{k}", stepped + S + k * S // 10) for k in range(10)]

    gap, request = connect(plan, (stepped + S) // S + 3, at=25.0)

    assert gap is None
    replay = _from([*old, *new, *outage], request)
    assert len(replay) == 221
    verdicts = screen(plan, *replay, at=25.1)
    assert verdicts == [*["deduplicated"] * 211, *["new"] * 10]
    assert plan.take() == []


def test_unproven_old_line_is_delivered_again_with_one_ring_check() -> None:
    """Design 5.6's exception at the default caps: a step of 6 s seen live and
    a resume soon after. The replay's first old line left the old window, is
    above the new hw and below every time the device clock can show:
    nothing proves it old, so it comes again, and its ring check reports a
    Gap(ring_rotated) that lost nothing. The old window and the new one are
    then replays."""
    plan = planner()
    connect(plan, at=0.0)
    old = [entry("a0", BASE - 3 * S), entry("a", BASE), entry("b", BASE + S)]
    screen(plan, *old, at=1.0)
    new = [entry("c", BASE - 6 * S), entry("d", BASE - 5 * S - S // 2)]
    assert screen(plan, *new, at=2.0) == ["new", "new"]
    plan.take()

    gap, _request = connect(plan, (BASE - 5 * S) // S, at=4.0)

    assert gap is None
    verdicts = screen(plan, *old, *new, entry("e", BASE - 4 * S), at=4.1)
    assert verdicts == ["new", *["deduplicated"] * 4, "new"]
    assert gaps(plan) == [
        Gap(moment(BASE - 5 * S - S // 2), moment(BASE - 3 * S), None, "ring_rotated")
    ]


def test_replay_is_over_once_every_line_at_hw_was_replayed() -> None:
    """The replay of a resume is over once every line at hw was replayed,
    also when an element below hw never comes: a line below hw -
    max(resume_overlap, 1 s) is then a step seen live, not below_cursor."""
    plan = planner()
    underway(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + S), at=1.0)
    connect(plan, at=2.0)
    far = BASE - 3600 * S

    verdicts = screen(plan, entry("b", BASE + S), entry("y", far), at=2.2)

    assert verdicts == ["deduplicated", "new"]
    assert gaps(plan) == [Gap(moment(BASE + S), moment(far), None, "clock_moved_back")]


def test_old_windows_are_released_once_hw_passes_them() -> None:
    """Every line stored more than max(resume_overlap, 1 s) late is taken for
    a step and keeps the window read before it; hw passes that window by the
    overlap a moment later, and from then on no request reaches it: it is
    released, so the kept windows stay bounded on a connection that never
    resumes (design 5.10)."""
    plan = planner()
    connect(plan, at=0.0)
    kept = []
    ts = BASE
    for k in range(1030):  # 103 s of lines, every 50th one 3 s late
        ts += S // 10
        line = entry(f"m{k}", ts - 3 * S if k % 50 == 49 else ts)
        screen(plan, line, at=k / 10)
        kept.append(len(plan._old or ()))
    assert len([gap for gap in gaps(plan) if gap.reason == "clock_moved_back"]) == 20
    assert max(kept) <= 21  # one window: 2 s of lines
    assert plan._old is None
    assert plan._old_top is None


def test_old_windows_are_released_by_a_request_above_them() -> None:
    """A resume whose request lies above every line read before the step
    (the caps moved it) releases the windows they left."""
    plan = planner(max_lines=3)
    underway(plan)
    screen(plan, entry("a", BASE), entry("b", BASE + S), at=1.0)
    screen(plan, entry("c", BASE - 10 * S), at=2.0)
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]
    up = [entry(f"n{k}", BASE + S + k * S // 10) for k in (5, 6, 7, 10)]
    screen(plan, *up, at=3.0)  # hw - overlap is the old hw - 1 s
    assert plan._old is not None

    _gap, request = connect(plan, BASE // S + 3, at=4.0)

    assert request == BASE + S + S // 2 + 1  # the caps evicted n5
    assert plan._old is None
    assert plan._old_top is None


@pytest.mark.parametrize("ahead", ["late_line", "notice"])
def test_late_line_taken_for_a_step_keeps_the_caps_bound(ahead: str) -> None:
    """A line 2.5 s late is taken for a step (the known cost of seeing steps
    live), with a cap that evicted inside the overlap. The next resume's
    request still skips what the caps evicted from the old window, so the
    replay neither meets the line below the request or the replayed notice
    stored ahead of the old lines (either would end the drop of old lines)
    nor delivers the capped-out lines or the start marker again."""
    plan = planner("now", max_lines=16)
    t0 = BASE + 1000 * S
    connect(plan, t0 // S - 4, at=-10.0)
    pre = [entry(f"p{i}", t0 - S // 2 + i * S // 10) for i in range(5)]
    if ahead == "late_line":
        pre.append(entry("q", t0 - 3 * S))  # stored late, below the request
    else:
        pre.append(notice(3, t0 - S // 20))
    for line in pre:
        plan.before_start(line, len(line.raw) + 1, 0.0)
    marker = entry("start-1", t0, tag="logpyt")
    plan.marker(marker, len(marker.raw) + 1, 0.0)
    live = [entry(f"l{i}", t0 + i * S // 100) for i in range(40)]
    screen(plan, *live, at=0.4)
    late = entry("x", t0 + 4 * S // 10 - 5 * S // 2)
    assert screen(plan, late, at=0.41) == ["new"]
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]
    more = [entry(f"l{i}", t0 + i * S // 100 + S // 100) for i in range(40, 50)]
    screen(plan, *more, at=0.5)

    gap, request = connect(plan, (t0 + S) // S, at=1.5)

    assert gap is None
    assert request == t0 + 23 * S // 100 + 1  # l23 was the last one capped out
    replay = _from([*pre, marker, *live, late, *more], request)
    assert [line.message for line in replay][:1] == ["l24"]
    verdicts = screen(plan, *replay, entry("y", t0 + 6 * S // 10), at=1.6)
    assert verdicts == [
        *["deduplicated"] * 16,
        "below_cursor",
        *["deduplicated"] * 10,
        "new",
    ]
    assert plan.overlap_shortened == 1
    assert gaps(plan) == []


def test_since_now_each_attempt_is_the_first_clock_read() -> None:
    """Every attempt before the marker starts over at its own clock (design
    A.25): the capture's first clock read is the one of the marker's attempt,
    so a step during the capture is reported although the clock moved forward
    between the attempts."""
    plan = planner("now")
    plan.clock(CLOCK, issued=0.0)  # its marker write fails
    plan.clock(CLOCK + 3600, issued=10.0)
    plan.position()
    plan.marker(entry("start-2", (CLOCK + 3600) * S, tag="logpyt"), 40, 10.5)
    screen(plan, entry("a", (CLOCK + 3620) * S), at=30.0)

    gap, _request = connect(plan, CLOCK + 3610, at=31.0)  # 10 s back

    assert gap is not None
    assert gap.reason == "clock_moved_back"


def test_outage_counts_from_the_marker_read() -> None:
    """The marker and the lines ahead of it are lines read: a re-anchor right
    after the marker counts its outage from the marker's read."""
    plan = planner("now")
    plan.clock(CLOCK, issued=0.0)
    plan.position()
    plan.marker(entry("start-1", CLOCK * S + 5, tag="logpyt"), 40, 2.0)

    _gap, request = connect(plan, CLOCK - 3600, at=7.0)

    assert request == (CLOCK - 3600) * S - 5 * S - 2 * S


@pytest.mark.parametrize("case", ["reanchor", "resume", "overlap", "now"])
def test_request_is_never_negative(case: str) -> None:
    """Near the epoch a request can fall below 0, an invalid -T: it stays at 0
    (design A.25). The re-anchor's outage and the overlap are host times, so
    they can exceed the clock; every attempt would ask for the same bad value."""
    plan = planner("now" if case == "now" else "backlog")
    if case == "reanchor":
        connect(plan, 100, at=0.0)
        screen(plan, entry("a", 100 * S), at=1.0)
        request = connect(plan, 5, at=11.0)[1]
    elif case == "resume":
        connect(plan, 1, at=0.0)
        screen(plan, entry("a", 3 * S // 2), at=1.0)
        request = connect(plan, 2, at=2.0)[1]
    elif case == "overlap":
        plan = planner(overlap=1e12)
        connect(plan, at=0.0)
        screen(plan, entry("a", BASE), at=1.0)
        request = connect(plan, at=2.0)[1]
    else:
        request = connect(plan, 0, at=0.0)[1]

    assert request == 0


# -- forward clock steps ---------------------------------------------------------------


def _reanchored(plan: ResumePlanner) -> tuple[list[LogEntry], int]:
    """A capture, a step back of an hour during an outage and its re-anchor at
    host 5.0, whose replay shows the lines read and one of the new clock."""
    underway(plan)
    old = [entry("a", BASE), entry("b", BASE + S // 2)]
    screen(plan, *old, at=1.0)
    clock = CLOCK - 3600
    gap, _request = connect(plan, clock, at=5.0)
    assert gap is not None
    new = entry("c", clock * S)
    assert screen(plan, *old, new, at=5.1) == ["deduplicated", "deduplicated", "new"]
    return [*old, new], clock


@pytest.mark.parametrize("where", ["below_the_read", "among_the_read"])
def test_forward_step_below_the_old_hw_goes_on(where: str) -> None:
    """5.6's device with a wrong clock that then syncs: a line above every time
    the clock can show, 1.3 s after the clock read, past the first new line, is
    no old line: it goes on, moves hw, and the next resume replays it once."""
    plan = planner()
    (*old, new), clock = _reanchored(plan)
    forward = (clock + 1800) * S if where == "below_the_read" else BASE - S
    lines = [entry(f"f{i}", forward + i) for i in range(3)]

    assert screen(plan, *lines, at=6.3) == ["new"] * 3

    assert plan.take() == []
    assert plan.cursor == forward + 2
    gap, request = connect(plan, forward // S + 2, at=7.0)
    assert gap is None
    assert request == forward + 2 - 2 * S
    later = entry("g", forward + S)
    verdicts = screen(plan, *old, new, *lines, later, at=7.1)
    assert verdicts == [
        *["deduplicated"] * 2,
        "below_cursor",
        *["deduplicated"] * 3,
        "new",
    ]
    assert plan.take() == []


def test_forward_step_past_the_old_hw_ends_the_ceiling() -> None:
    """The same past the old hw: the clock passed the old times, and the next
    request lies above every line read before the step."""
    plan = planner()
    _lines, _clock = _reanchored(plan)
    lines = [entry(f"f{i}", BASE + 10 * S + i) for i in range(3)]

    assert screen(plan, *lines, at=6.3) == ["new"] * 3

    assert plan.cursor == BASE + 10 * S + 2
    assert plan._ceiling is None
    gap, request = connect(plan, BASE // S + 12, at=7.0)
    assert gap is None
    assert request == BASE + 8 * S + 2
    assert screen(plan, *lines, entry("g", BASE + 11 * S), at=7.1) == [
        *["deduplicated"] * 3,
        "new",
    ]
    assert plan.take() == []


def test_forward_step_within_the_slack_comes_once() -> None:
    """Within 1 s of the clock read such a line could be a ring in time order
    returning old lines, but those end at the line at hw, which was replayed: it
    is delivered, moves nothing, and later replays match it."""
    plan = planner()
    (*old, new), clock = _reanchored(plan)
    line = entry("f", BASE - S)  # among the times read before the step

    assert screen(plan, line, at=5.5) == ["new"]

    assert plan.cursor == clock * S
    connect(plan, clock + 1, at=5.8)
    verdicts = screen(plan, *old, new, line, at=5.9)
    assert verdicts == ["deduplicated"] * 4
    assert plan.take() == []


@pytest.mark.parametrize("where", ["above", "below"])
def test_forward_step_ahead_of_any_new_line_comes_once(where: str) -> None:
    """The line at hw was replayed and no line of the new clock came: lines
    above the clock go on as lines logged before the step, however late. They
    move nothing and later replays match them, whichever the resume's kind."""
    plan = planner()
    underway(plan)
    old = [entry("a", BASE), entry("b", BASE + S // 2)]
    screen(plan, *old, at=1.0)
    clock = CLOCK - 3600
    gap, _request = connect(plan, clock, at=5.0)
    assert gap is not None
    forward = BASE + 2 * S if where == "above" else BASE - S
    lines = [entry(f"f{i}", forward + i) for i in range(3)]

    verdicts = screen(plan, *old, *lines, at=7.0)

    assert verdicts == [*["deduplicated"] * 2, *["new"] * 3]
    assert plan.cursor == BASE + S // 2
    seconds = BASE // S + 3 if where == "above" else clock + 1
    gap, _request = connect(plan, seconds, at=8.0)
    assert gap is None
    later = entry("g", seconds * S + S // 2)
    replay = screen(plan, *old, *lines, later, at=8.1)
    assert replay == [*["deduplicated"] * 5, "new"]
    assert plan.take() == []


def test_forward_step_after_a_live_step_back_needs_no_slack() -> None:
    """A connection that crossed the step back in its own lines has read past
    the lines before it: a line above the clock is a forward step at once."""
    plan = planner()
    underway(plan)
    old = entry("a", BASE)
    screen(plan, old, at=1.0)
    connect(plan, BASE // S, at=1.0)  # the clock read is 0.2 s old at the line
    assert screen(plan, old, at=1.0) == ["deduplicated"]
    back = entry("c", BASE - 3600 * S)
    assert screen(plan, back, at=1.1) == ["new"]
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]
    line = entry("f", BASE + 10 * S)

    assert screen(plan, line, at=1.2) == ["new"]

    assert plan.cursor == BASE + 10 * S
    gap, request = connect(plan, BASE // S + 11, at=2.0)
    assert gap is None
    assert request == BASE + 8 * S
    assert screen(plan, line, at=2.1) == ["deduplicated"]
    assert plan.take() == []


def test_drift_after_a_reanchor_keeps_the_lines() -> None:
    """A device clock that gains on the host after a re-anchor leaves the
    estimate behind: its lines are forward steps, not old lines."""
    plan = planner()
    (*_lines, last), clock = _reanchored(plan)
    ts = micros(last.timestamp)
    verdicts = []
    for k in range(1, 1081):  # 3 h of lines; the clock gains 3 s
        line = entry(f"x{k}", ts + k * 10 * S + k * 2778)
        verdicts.append(plan.screen(line, 80, 5.1 + k * 10.0))

    assert verdicts == ["new"] * 1080
    assert plan.take() == []
    assert plan.cursor == ts + 1080 * 10 * S + 1080 * 2778
    assert clock * S == ts


def test_forward_step_with_the_read_lines_gone_is_the_limit() -> None:
    """The documented limit (design A.25): the ring lost the lines read before
    and no new line came, so a line within the times read before the step
    cannot be told from an old line that left the window. Past the first new
    line and the slack it goes on."""
    plan = planner()
    underway(plan)
    screen(plan, entry("a0", BASE - 5 * S), entry("a", BASE), at=1.0)
    clock = CLOCK - 3600
    connect(plan, clock, at=5.0)

    assert screen(plan, entry("f", BASE - S), at=6.3) == ["deduplicated"]

    assert screen(plan, entry("n", clock * S + 1), at=6.4) == ["new"]
    assert screen(plan, entry("f2", BASE - S + 1), at=6.5) == ["new"]


# -- a re-anchor's replay --------------------------------------------------------------


def test_step_between_attempts_is_reported_again() -> None:
    """A repeated attempt at the same hw reports nothing for the same step, but
    its clock read can show a second one. The lines logged between the steps
    are not old lines: they lie below every line read before."""
    plan = planner()
    underway(plan)
    old = entry("a", BASE)
    screen(plan, old, at=1.0)
    clock = CLOCK - 3600

    first, _request = connect(plan, clock, at=5.0)
    same, _request = connect(plan, clock + 1, at=6.0)
    second, _request = connect(plan, clock - 3599, at=7.0)
    third, _request = connect(plan, clock - 3598, at=8.0)

    assert first == Gap(moment(BASE), moment(clock * S), None, "clock_moved_back")
    assert same is None
    assert second == Gap(
        moment(BASE), moment((clock - 3599) * S), None, "clock_moved_back"
    )
    assert third is None
    middle = entry("m", (clock + 1) * S)
    assert screen(plan, old, middle, at=8.1) == ["deduplicated", "new"]
    assert plan.cursor == (clock + 1) * S


def test_repeated_attempt_counts_the_outage_from_the_line_at_hw() -> None:
    """An attempt that only replayed old lines does not shorten the next one's
    outage: the new lines lie below a request counted from those reads."""
    plan = planner()
    underway(plan)
    old = [entry("a", BASE), entry("b", BASE + S // 2)]
    screen(plan, *old, at=1.0)
    clock = CLOCK - 3600
    connect(plan, clock, at=5.0)
    assert screen(plan, old[0], at=5.5) == ["deduplicated"]

    _gap, request = connect(plan, clock + 1, at=6.0)

    assert request == (clock + 1) * S - 5 * S - 2 * S


def test_lines_past_the_read_ones_are_delivered() -> None:
    """Two steps in one outage, one replay across them: once the line at hw was
    replayed, a line above the clock and within the times read before was not
    read (the first step's lines). It comes once, and hw stays."""
    plan = planner()
    underway(plan)
    old = [entry("a", BASE), entry("b", BASE + S)]
    screen(plan, *old, at=1.0)
    clock = BASE // S - 20
    gap, _request = connect(plan, clock, at=5.0)
    assert gap is not None
    middle = [entry("m0", BASE - S), entry("m1", BASE + S // 2)]
    near = entry("z", clock * S)

    assert screen(plan, *old, *middle, near, at=5.1) == [
        "deduplicated",
        "deduplicated",
        "new",
        "new",
        "new",
    ]

    assert plan.cursor == clock * S
    assert plan.take() == []
    connect(plan, clock + 1, at=5.2)
    assert screen(plan, *old, *middle, near, at=5.3) == ["deduplicated"] * 5


def test_line_above_the_read_ones_ends_their_replay() -> None:
    """The ring lost the lines at hw, but a line above every line read shows the
    replay is past them: an unmatched line among their times, after it, was not
    read. The re-anchor's below_cursor rule ends with it too."""
    plan = planner()
    underway(plan)
    screen(plan, entry("a0", BASE - S), entry("a", BASE), at=1.0)
    clock = CLOCK - 3600
    _gap, request = connect(plan, clock, at=5.0)
    assert request is not None
    above = entry("u", BASE + S)
    among = entry("m", BASE - S // 2)
    below = entry("n", request - S)

    assert screen(plan, above, among, below, at=5.1) == ["new", "new", "new"]


def test_line_below_every_line_read_goes_on() -> None:
    """A second step whose clock read cannot show it (the clock is above the new
    hw), and a ring that lost the lines read: a line above the clock and below
    every line read before the first step was never read. It goes on, and the
    ring check reports the hole. Taking it for an old line lost it silently."""
    plan = planner()
    underway(plan)
    screen(plan, entry("a", BASE), at=1.0)
    clock = CLOCK - 3600
    connect(plan, clock, at=5.0)
    screen(plan, entry("n", clock * S), at=5.1)
    plan.take()
    connect(plan, clock + 10, at=15.0)
    line = entry("u", (clock + 40) * S)

    assert screen(plan, line, at=15.1) == ["new"]

    assert gaps(plan) == [
        Gap(moment(clock * S), line.timestamp, None, "ring_rotated"),
    ]
    assert plan.cursor == (clock + 40) * S


def test_line_below_the_start_stays_dropped_after_a_step() -> None:
    """With a start position the ring holds older lines that were never read and
    are not the capture's: a re-anchor's request can reach them, and they are
    dropped like old lines, not delivered as lines below every line read."""
    plan = planner(moment(BASE))
    connect(plan, BASE // S, at=0.0)
    screen(plan, entry("a", BASE + 2 * S), at=1.0)
    plan.take()
    clock = BASE // S - 3600
    gap, request = connect(plan, clock, at=5.0)
    assert gap is not None
    assert request is not None

    assert screen(plan, entry("before", BASE - 60 * S), at=5.1) == ["deduplicated"]
    assert plan.cursor == BASE + 2 * S


def test_times_read_stay_known_after_the_old_windows_went() -> None:
    """hw passing the old lines frees their windows, but a later step back makes
    requests reach them again: they are still old lines, not new ones below
    every line read."""
    plan = planner()
    underway(plan)
    first = entry("a0", BASE - 20 * S)  # below every line of the next era
    screen(plan, first, entry("a", BASE), at=1.0)
    screen(plan, entry("b", BASE - 10 * S), at=2.0)
    screen(plan, entry("c", BASE + 10 * S), at=3.0)
    assert plan._old is None  # hw passed the old window
    screen(plan, entry("d", BASE - 3600 * S), at=4.0)
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"] * 2

    gap, _request = connect(plan, (BASE - 3600 * S) // S + 1, at=5.0)

    assert gap is None
    assert screen(plan, first, at=5.1) == ["deduplicated"]
    assert plan.take() == []


@pytest.mark.parametrize("case", ["kept", "lost"])
def test_step_from_before_the_capture_checks_the_ring(case: str) -> None:
    """A silent re-anchor has no Gap to mark the point: when the ring lost the
    lines at hw before the first line that goes on, Gap(ring_rotated) reports
    the hole, and S stays. No line of the new clock was read, so none below the
    request is dropped. With the lines at hw there, nothing is reported."""
    plan = planner()
    connect(plan, at=0.0)
    start = plan._start
    far = BASE + 3600 * S
    read = [entry(f"o{i}", far + i) for i in range(3)]
    screen(plan, *read, at=0.5)
    gap, request = connect(plan, CLOCK + 1, at=1.0)
    assert gap is None
    assert request is not None
    below = entry("low", request - S)
    replay = read if case == "kept" else []

    verdicts = screen(plan, *replay, below, entry("n", CLOCK * S), at=1.1)

    assert verdicts == [*["deduplicated"] * len(replay), "new", "new"]
    assert plan._start == start
    if case == "kept":
        assert plan.take() == []
    else:
        assert gaps(plan) == [
            Gap(moment(far + 2), below.timestamp, None, "ring_rotated")
        ]


def test_repeated_attempt_that_lost_the_delivered_lines_reports_it() -> None:
    """An attempt delivered lines logged before the step, then the ring rotated
    in the next outage: the lines between the last one delivered and the first
    line of the next replay lie behind the step's Gap, so their loss gets its
    own Gap, once."""
    plan = planner()
    underway(plan)
    old = [entry("a", BASE), entry("b", BASE + S // 2)]
    screen(plan, *old, at=1.0)
    clock = CLOCK - 3600
    gap, _request = connect(plan, clock, at=5.0)
    assert gap is not None
    unread = entry("u", BASE + S)
    assert screen(plan, *old, unread, at=5.1)[-1] == "new"

    gap, _request = connect(plan, clock + 1, at=6.0)

    assert gap is None
    after = [entry("r0", clock * S), entry("r1", clock * S + 1)]
    assert screen(plan, *after, at=6.1) == ["new", "new"]
    assert gaps(plan) == [
        Gap(unread.timestamp, after[0].timestamp, None, "ring_rotated")
    ]


def test_resume_with_hw_behind_delivered_lines_checks_the_ring() -> None:
    """The clock read is back within reach of hw, but no line of the new clock came
    yet, so hw lies behind the lines delivered from before the step: a line above
    them with the lines at hw gone is the first line of the ring check."""
    plan = planner()
    underway(plan)
    first = entry("a", BASE)
    screen(plan, first, at=1.0)
    gap, _request = connect(plan, BASE // S - 50, at=5.0)
    assert gap is not None
    assert screen(plan, first, entry("u", BASE + 100 * S), at=5.1) == [
        "deduplicated",
        "new",
    ]
    gap, _request = connect(plan, BASE // S - 1, at=55.0)
    assert gap is None
    line = entry("v", BASE + 150 * S)

    assert screen(plan, line, at=55.1) == ["new"]

    assert gaps(plan) == [Gap(first.timestamp, line.timestamp, None, "ring_rotated")]


def test_delivered_lines_are_no_bound_for_dropping_live_ones() -> None:
    """The same resume once the replay proved old lines, and live lines stamped
    below those delivered from before the step: only a line read before the step
    is dropped by its time. The first line above them is the ring check."""
    plan = planner()
    underway(plan)
    first = entry("a", BASE)
    screen(plan, first, at=1.0)
    gap, _request = connect(plan, BASE // S - 60, at=5.0)
    assert gap is not None
    unread = entry("u", BASE + 20 * S)
    assert screen(plan, first, unread, at=5.1) == ["deduplicated", "new"]
    assert connect(plan, BASE // S - 1, at=65.0)[0] is None
    below = entry("r", BASE - S - S // 2)
    line = entry("v", BASE + S // 2)

    assert screen(plan, below, line, at=65.1) == ["deduplicated", "new"]

    assert gaps(plan) == [Gap(first.timestamp, line.timestamp, None, "ring_rotated")]
    assert plan.cursor == line.timestamp.timestamp() * S
    assert screen(plan, entry("w", BASE + S), at=65.2) == ["new"]


def test_delivered_lines_the_caps_evicted_leave_the_request_at_hw() -> None:
    """They lie above hw, so a request above them would be above hw: the resume
    could not check the ring, and the live lines below it would go as
    below_cursor with no Gap."""
    plan = planner(max_lines=4)
    underway(plan)
    first = entry("a", BASE)
    screen(plan, first, at=1.0)
    gap, _request = connect(plan, BASE // S - 60, at=5.0)
    assert gap is not None
    unread = [entry(f"u{i}", BASE + (i + 1) * S) for i in range(10)]
    assert screen(plan, first, *unread, at=5.1) == ["deduplicated", *["new"] * 10]

    gap, request = connect(plan, BASE // S - 1, at=65.0)

    assert (gap, request) == (None, BASE - 2 * S)
    line = entry("v", BASE + S // 2)
    assert screen(plan, line, at=65.1) == ["new"]
    assert gaps(plan) == [Gap(first.timestamp, line.timestamp, None, "ring_rotated")]


def test_delivered_lines_the_caps_evicted_come_again_at_the_resume() -> None:
    """The documented limit (design A.25): the old window no longer holds them,
    and the clock can show their times, so they are not dropped by time."""
    plan = planner(max_lines=4)
    underway(plan)
    first = entry("a", BASE)
    screen(plan, first, at=1.0)
    gap, _request = connect(plan, BASE // S - 60, at=5.0)
    assert gap is not None
    unread = [entry(f"u{i}", BASE + (i + 1) * S // 10) for i in range(10)]
    screen(plan, first, *unread, at=5.1)
    connect(plan, BASE // S - 1, at=65.0)

    verdicts = screen(plan, first, *unread, at=65.1)

    assert verdicts == [
        "deduplicated",
        *["new"] * 6,
        *["deduplicated"] * 4,
    ]
    assert gaps(plan) == []


def test_unread_lines_after_a_silent_reanchor_report_the_hole_once() -> None:
    """Several lines go on after the lines at hw were lost: one Gap, at the first."""
    plan = planner()
    connect(plan, at=0.0)
    far = BASE + 3600 * S
    screen(plan, entry("o", far), at=0.5)
    gap, _request = connect(plan, CLOCK + 1, at=1.0)
    assert gap is None
    unread = [entry(f"u{i}", far + (i + 1) * S) for i in range(3)]

    assert screen(plan, *unread, at=1.1) == ["new"] * 3

    assert gaps(plan) == [Gap(moment(far), unread[0].timestamp, None, "ring_rotated")]


def test_unread_lines_stay_matched_once_the_clock_passed_the_old_hw() -> None:
    """A line logged before the step is delivered once, also when a later clock
    read passes the old hw and clears the ceiling: the replay matches it."""
    plan = planner()
    underway(plan)
    old = entry("a", BASE)
    screen(plan, old, at=1.0)
    unread = [entry("u1", BASE + S // 10), entry("u2", BASE + S // 10)]
    gap, _request = connect(plan, BASE // S - 4, at=2.0)
    assert gap is not None
    new = entry("n", BASE - 4 * S + S // 2)
    assert screen(plan, old, *unread, new, at=2.1) == [
        "deduplicated",
        "new",
        "new",
        "new",
    ]

    gap, _request = connect(plan, BASE // S + 1, at=7.0)

    assert gap is None
    assert plan._ceiling is None
    later = entry("p", BASE + S + 1)
    assert screen(plan, old, *unread, new, later, at=7.1) == [
        *["deduplicated"] * 4,
        "new",
    ]
    assert plan.take() == []


def test_unread_lines_at_different_times_are_dropped_by_later_replays() -> None:
    """Every pre-step line delivered is a replay for later connections, also below
    the highest one delivered, over two reconnects after the step."""
    plan = planner()
    connect(plan, at=0.0)
    old = entry("a", BASE + 2 * S)
    screen(plan, old, at=3.0)
    unread = [entry("u1", BASE + 3 * S), entry("u2", BASE + 3 * S + 10)]
    clock = CLOCK - 3600
    gap, _request = connect(plan, clock, at=5.0)
    assert gap is not None
    assert screen(plan, old, *unread, at=5.1) == ["deduplicated", "new", "new"]

    connect(plan, clock + 1, at=6.0)
    assert screen(plan, old, *unread, at=6.1) == ["deduplicated"] * 3
    connect(plan, clock + 2, at=7.0)
    assert screen(plan, old, *unread, at=7.1) == ["deduplicated"] * 3


def test_unread_line_below_the_highest_delivered_comes_once() -> None:
    """A connection ended after the higher of two lines logged before the step
    (out of time order): the next replay still delivers the lower one, which it
    was never handed before."""
    plan = planner()
    connect(plan, at=0.0)
    old = entry("a", BASE + 2 * S)
    screen(plan, old, at=3.0)
    high, low = entry("u1", BASE + 3 * S + 10), entry("u2", BASE + 3 * S)
    clock = CLOCK - 3600
    connect(plan, clock, at=5.0)
    assert screen(plan, old, high, at=5.1) == ["deduplicated", "new"]

    connect(plan, clock + 1, at=6.0)

    assert screen(plan, old, high, low, at=6.1) == [
        "deduplicated",
        "deduplicated",
        "new",
    ]


@pytest.mark.parametrize(
    ("since", "hops"),
    [
        ("backlog", "two_back"),
        ("backlog", "back_forward_back"),
        ("datetime", "two_back"),
        ("datetime", "back_forward_back"),
    ],
)
def test_steps_before_the_capture_lose_no_line(since: str, hops: str) -> None:
    """A connection that reads across two steps from before the capture crosses
    the first in its own lines, so what follows is no replay: the middle lines
    are not old ones, however far above the clock (nothing lost, no Gap)."""
    far = [entry(f"e{i}", BASE + 7200 * S + i) for i in range(2)]
    middle = [entry(f"m{i}", BASE + 3600 * S + i) for i in range(3)]
    if hops == "back_forward_back":
        middle += [entry(f"h{i}", BASE + 4200 * S + i) for i in range(2)]
    near = [entry(f"n{i}", CLOCK * S + i) for i in range(2)]
    plan = planner("backlog" if since == "backlog" else moment(BASE - S))
    connect(plan, at=0.0)
    start = plan._start

    verdicts = screen(plan, *far, *middle, *near, at=0.1)

    assert verdicts == ["new"] * (len(far) + len(middle) + 2)
    assert plan.take() == []
    assert plan._start == start
    assert plan.cursor == CLOCK * S + 1
    gap, _request = connect(plan, CLOCK + 1, at=1.0)
    assert gap is None
    replay = screen(plan, *far, *middle, *near, at=1.1)
    assert replay == ["deduplicated"] * len(replay)
    assert plan.take() == []


@pytest.mark.parametrize("sizes", [(3, 20), (3600, 3600)], ids=["small", "large"])
def test_two_steps_in_one_outage_lose_no_line(sizes: tuple[int, int]) -> None:
    """One replay reads across two steps back: the lines between them are never
    old lines, whichever times they carry."""
    first, second = sizes
    plan = planner()
    underway(plan)
    old = [entry("a", BASE), entry("b", BASE + S // 2)]
    screen(plan, *old, at=1.0)
    unread = entry("u", BASE + S)
    middle = [entry(f"m{i}", BASE + S - first * S + i * S // 10) for i in range(30)]
    clock = (micros(middle[-1].timestamp) - second * S) // S
    near = entry("z", clock * S + 1)
    gap, _request = connect(plan, clock, at=5.0)
    assert gap is not None

    verdicts = screen(plan, *old, unread, *middle, near, at=5.1)

    assert verdicts == [*["deduplicated"] * 2, *["new"] * 32]


def test_old_line_ahead_of_the_line_at_hw_stays_dropped() -> None:
    """The control of the rules above: before the line at hw is replayed, an
    unmatched line up to the highest time read is an old one, also after a line
    of the new clock (a ring in time order)."""
    plan = planner()
    underway(plan)
    read = [entry("a0", BASE - 3 * S), entry("a", BASE), entry("b", BASE + S)]
    screen(plan, *read, at=1.0)
    clock = CLOCK - 3600
    connect(plan, clock, at=5.0)

    verdicts = screen(plan, entry("n", clock * S), read[0], read[1], read[2], at=5.1)

    assert verdicts == ["new", "deduplicated", "deduplicated", "deduplicated"]
    assert plan.take() == []


# -- bounds ----------------------------------------------------------------------------


def test_old_windows_stay_within_the_caps_over_many_steps() -> None:
    """One window per step would grow without bound while the clock stays below
    the old top (a harness that resets the date before each test): all of them
    together stay within the caps (design 5.10), the newest step's window whole,
    and a later replay still drops the rest by time."""
    plan = planner(max_lines=100)
    underway(plan)
    host = 0.0
    first = entry("first", BASE + 3 * S)
    screen(plan, first, at=host)
    for step in range(30):
        stepped = BASE - step * 3600 * S
        lines = [entry(f"s{step}-{i}", stepped + i) for i in range(100)]
        host += 1.0
        screen(plan, *lines, at=host)
        assert plan._old is not None
        assert sum(plan._old.values()) <= 101
    plan.take()
    gap, _request = connect(plan, BASE // S - 29 * 3600 + 1, at=host + 1)
    assert gap is None
    assert plan._old is not None
    newest = BASE - 28 * 3600 * S + 99
    assert (newest, entry("s28-99", newest).raw) in plan._old
    assert (BASE + 3 * S, first.raw) not in plan._old
    assert screen(plan, first, at=host + 1.1) == ["deduplicated"]


@pytest.mark.parametrize("cap", ["lines", "bytes"])
def test_old_windows_stay_within_the_caps_with_lines_stored_late(cap: str) -> None:
    """Lines stored later than the step bound recurring faster than the overlap
    keep hw near the old windows, so they are never freed by time: the caps hold
    (with the window's own caps, about twice them in all)."""
    plan = (
        planner(max_lines=500)
        if cap == "lines"
        else planner(max_bytes=500 * len(entry("m0", BASE).raw))
    )
    underway(plan)
    largest = 0
    for k in range(10_000):
        ts = BASE + k * 1000
        late = k % 1000 == 999
        line = entry(f"m{k}", ts - 3 * S if late else ts)
        plan.screen(line, len(line.raw) + 1, k / 1000)
        if late:
            assert plan._old is not None
            largest = max(largest, sum(plan._old.values()))
    assert len([g for g in gaps(plan) if g.reason == "clock_moved_back"]) == 10
    assert 400 <= largest <= 501
    assert plan._old_bytes == sum(item[2] for item in plan._old_lines)


def test_old_window_keeps_a_line_at_hw_over_the_byte_cap() -> None:
    """Like the window, the old window keeps the lines at the hw of the step: a
    replay is still told by them."""
    plan = planner(max_bytes=40)
    underway(plan)
    line = entry("a", BASE)
    screen(plan, line, at=1.0)
    clock = CLOCK - 3600
    connect(plan, clock, at=5.0)

    assert plan._old == {(BASE, line.raw): 1}
    assert screen(plan, line, at=5.1) == ["deduplicated"]


def test_caps_bound_a_run_of_unparsed_lines() -> None:
    """Unparsed lines inherit hw, which the caps never evict, so a long run of
    them would not be bounded (design 5.10): over a cap they are not windowed,
    one INFO says so, and the ring check does not count them. A replay of one
    can then come again (documented)."""
    plan = planner(max_lines=100, max_bytes=20_000)
    connect(plan)
    parsed = entry("a", BASE)
    screen(plan, parsed)
    run = [entry(f"garbage{k}", 0, parsed=False) for k in range(5000)]

    assert screen(plan, *run) == ["new"] * 5000

    assert plan.window_lines == 100
    assert plan.window_bytes <= 20_000
    assert [isinstance(item, str) for item in plan.take()] == [True]
    connect(plan)
    assert plan._phase is not None
    assert plan._phase.at_position == 100
    verdicts = screen(plan, parsed, *run[:99], run[100], entry("b", BASE + 1))
    assert verdicts == [*["deduplicated"] * 100, "new", "new"]
    assert gaps(plan) == []


def test_unparsed_line_alone_at_hw_stays_over_the_caps() -> None:
    """The first new line after a re-anchor can be an unparsed one that inherits a
    replayed line's time, larger than the byte cap: hw needs a line in the deque,
    so it stays, and the lines after it, below it or above it, go on."""
    plan = planner(max_bytes=200)
    connect(plan, at=0.0)
    replayed = entry("y", BASE + 8 * S)
    screen(plan, entry("first", BASE + 2 * S), at=4.0)
    screen(plan, replayed, entry("a", BASE + 10 * S), at=5.0)
    assert connect(plan, BASE // S + 7, at=5.1)[0] is None
    assert screen(plan, replayed, at=5.2) == ["deduplicated"]
    hw = BASE + 8 * S

    assert screen(plan, entry("x" * 500, 0, parsed=False), at=5.3) == ["new"]

    assert (plan.cursor, plan.window_lines) == (hw, 1)
    assert screen(plan, entry("below", hw - 10), at=5.4) == ["new"]
    assert plan.cursor == hw
    assert screen(plan, entry("above", hw + 10), at=5.5) == ["new"]
    assert plan.cursor == hw + 10
    assert plan.window_bytes <= 200


def test_parsed_lines_at_hw_stay_over_the_caps() -> None:
    """The control: lines stamped hw that have their own time stay, as before."""
    plan = planner(max_lines=2)
    connect(plan)
    lines = [entry(f"x{i}", BASE) for i in range(5)]

    screen(plan, *lines)

    assert plan.window_lines == 5


@pytest.mark.parametrize(
    ("case", "text"),
    [("lost", "no header at all"), ("first", "no header at all")],
    ids=["left_the_overlap", "opened_the_connection"],
)
def test_unparsed_line_outside_the_window_comes_again(case: str, text: str) -> None:
    """The documented limit (design A.25): an unparsed line whose inherited time
    left the overlap, or that opened a connection, is not in the window at the
    resume, and logd returns it by its own time."""
    plan = planner()
    connect(plan)
    garbage = entry(text, 0, parsed=False)
    if case == "lost":
        screen(plan, entry("p1", BASE - 10 * S), garbage, entry("p2", BASE))
    else:
        screen(plan, garbage, entry("p2", BASE))
    connect(plan)

    assert screen(plan, garbage, entry("p2", BASE)) == ["new", "deduplicated"]
    assert plan.take() == []


# -- what is reported ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "read", "reported"),
    [(5.5, 5, False), (100.0, 50, True), (100.0, 98, False), (100.0, 97, True)],
    ids=["whole_seconds", "fell", "bound", "below_bound"],
)
def test_clock_fell_since_the_first_read(
    host: float, read: int, *, reported: bool
) -> None:
    """The line at hw predates the capture, so a clock read below it is silent
    unless it lies below N0 + elapsed - max(overlap, 1 s) (user decision, design
    A.25): the margin keeps a whole-second clock quiet, the elapsed time counts."""
    plan = planner()
    connect(plan, at=0.0)
    screen(plan, entry("old", BASE + 3600 * S), at=0.5)

    gap, _request = connect(plan, CLOCK + read, at=host)

    assert (gap is not None) is reported
    assert plan._step_reported is reported


@pytest.mark.parametrize(
    ("read", "reported"),
    [(35_991, False), (35_990, True)],
    ids=["within_200ppm", "below_it"],
)
def test_clock_fell_allows_for_a_slow_device_clock(
    read: int, *, reported: bool
) -> None:
    """Ten hours on: 200 ppm of them (7.2 s) on top of the bound of 2 s. A device
    clock that loses that little shows no step back since the first read."""
    plan = planner()
    connect(plan, at=0.0)
    screen(plan, entry("old", BASE + 86_400 * S), at=0.5)

    gap, _request = connect(plan, CLOCK + read, at=36_000.0)

    assert (gap is not None) is reported


@pytest.mark.parametrize(
    ("gain", "reported"),
    [(1.5, True), (2.2, True), (3.0, True), (8.0, False)],
    ids=["69ppm", "102ppm", "139ppm", "370ppm"],
)
def test_drift_leaves_a_live_step_reported(gain: float, *, reported: bool) -> None:
    """A device clock that gains on the host over six hours: up to 200 ppm its
    lines are still the capture's, so a live step gets its Gap. Above that, a
    line looks logged before the first clock read (the cost in design A.25)."""
    plan = planner()
    connect(plan, BASE // S, at=0.0)
    ts = BASE
    for k in range(1, 217):
        ts = BASE + k * 100 * S + round(gain * S * k / 216)
        screen(plan, entry(f"x{k}", ts), at=k * 100.0)
    plan.take()

    assert screen(plan, entry("s", ts - 3600 * S), at=21601.0) == ["new"]

    assert bool(gaps(plan)) is reported


def test_chatty_notice_on_the_far_side_of_a_step_is_backlog() -> None:
    """A notice above every time the first clock read could show, with no step
    reported, was logged before the capture across a step from before it: not a
    loss of this capture (user decision, design A.25: S stays)."""
    plan = planner()
    connect(plan, at=0.0)
    far = BASE + 3600 * S

    assert screen(plan, notice(2, far), entry("near", CLOCK * S), at=0.1) == [
        "new",
        "new",
    ]

    assert (plan.backlog_pruned, plan.device_lost) == (2, 0)
    assert plan.take() == []


def test_chatty_notice_above_the_clock_after_a_reported_step_is_a_loss() -> None:
    """Once a step during the capture was reported, the rule is off."""
    plan = planner()
    connect(plan, at=0.0)
    screen(plan, entry("a", CLOCK * S + S), at=2.0)
    assert connect(plan, CLOCK - 3600, at=3.0)[0] is not None
    screen(plan, entry("a", CLOCK * S + S), entry("n", (CLOCK - 3600) * S), at=3.1)

    plan.take()

    screen(plan, notice(2, (CLOCK + 3600) * S), at=3.2)

    assert (plan.backlog_pruned, plan.device_lost) == (0, 2)
    assert [gap.reason for gap in gaps(plan)] == ["device_reported"]


def test_chatty_notice_below_since_is_backlog_too() -> None:
    """With since=<datetime>, a notice the device returns below -T is dropped
    (below_cursor) and still counts as backlog pruning, as with "backlog"."""
    plan = planner(moment(BASE))
    connect(plan)

    assert screen(plan, notice(7, BASE - 5 * S), entry("a", BASE)) == [
        "below_cursor",
        "new",
    ]

    assert (plan.backlog_pruned, plan.device_lost) == (7, 0)
    assert plan.take() == []


def test_stepped_replay_with_proof_drops_old_lines_above_the_clock_read() -> None:
    """A line more than 1 s below the new hw proves the replay is in the old lines
    (the device's late lines were about 86 ms late), so the unmatched old lines
    from N + 1 s up are dropped too, not delivered again."""
    plan = planner()
    connect(plan, BASE // S - 5, at=-5.0)
    old = [entry(f"o{i}", BASE + i * S) for i in range(121)]
    screen(plan, *old, at=120.5)
    new = [entry("n1", BASE + 5 * S), entry("n2", BASE + 8 * S + S // 2)]
    screen(plan, *new, at=121.0)
    assert [gap.reason for gap in gaps(plan)] == ["clock_moved_back"]

    gap, request = connect(plan, BASE // S + 9, at=124.0)  # above the new hw

    assert gap is None
    assert request == BASE + 6 * S + S // 2
    assert screen(plan, *old[7:15], at=124.1) == ["deduplicated"] * 8
    assert plan.cursor == BASE + 8 * S + S // 2


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


def _read_checked(
    plan: ResumePlanner, line: LogEntry, host: float, bound: int
) -> tuple[str, bool]:
    """Screen ``line``; whether it was a step seen live, checked against the rule."""
    stamped = micros(line.timestamp)
    before = plan.cursor
    verdict = plan.screen(line, len(line.raw) + 1, host)
    steps = [
        item
        for item in plan.take()
        if isinstance(item, Gap) and item.reason == "clock_moved_back"
    ]
    if not steps:
        return verdict, False
    assert before is not None
    assert steps == [Gap(moment(before), moment(stamped), None, "clock_moved_back")]
    assert stamped < before - bound
    assert verdict == "new"
    assert plan.cursor == stamped
    return verdict, True


def _check_resume(
    plan: ResumePlanner, history: list[LogEntry], request: int | None, host: float
) -> None:
    """The phase of a resume expects the lines of ``history`` at or above T_req."""
    phase = plan._phase
    hw = plan.cursor
    assert phase is not None
    assert hw is not None
    expect = hw - plan._overlap
    for evicted in (plan._cap_evicted, plan._old_evicted):
        # The old window's cap bound counts once the new hw passed it.
        if evicted is not None and expect <= evicted < hw:
            expect = evicted + 1
    if plan._floor is not None:
        expect = max(expect, plan._floor)
    assert phase.floor == request == expect
    replay = [line for line in history if micros(line.timestamp) >= expect]
    wanted = Counter((micros(line.timestamp), line.raw) for line in replay)
    assert {key: n for key, n in phase.expected.items() if n} == dict(wanted)
    assert phase.at_position == sum(n for (t, _), n in wanted.items() if t == hw)
    # logd returns them first: all replays.
    assert screen(plan, *replay, at=host) == ["deduplicated"] * len(replay)


@pytest.mark.parametrize("seed", _SEEDS, ids=[f"seed{seed}" for seed in _SEEDS])
def test_window_holds_every_line_at_or_above_the_request(seed: int) -> None:
    """At every resume, the phase expects exactly the lines read since the
    last clock step at or above T_req, which is max(hw - overlap, max_evicted
    + 1 us, start); the resume's replay of them is dropped. A line read below
    hw - max(overlap, 1 s) after the replay is a step seen live: one
    clock_moved_back Gap, and hw and the window restart at it (the clock
    steps back by 3 s or an hour; a line 2 s late is a step too when the
    overlap is 0.5 s)."""
    rng = random.Random(seed)  # noqa: S311  (a test, not cryptography)
    since: Literal["backlog"] | datetime = rng.choice(["backlog", moment(BASE + 3 * S)])
    plan = planner(
        since,
        overlap=rng.choice([0.5, 2.0]),
        max_lines=rng.choice([3, 50, 32_768]),
        max_bytes=rng.choice([400, 4096, 1 << 22]),
    )
    bound = max(plan._overlap, S)  # max(resume_overlap, 1 s)
    history: list[LogEntry] = []  # lines read as new since the last step
    ts = BASE
    host = 0.0  # the host clock runs with the device clock, but never steps
    underway(plan, at=host)
    for _ in range(40):
        for _ in range(rng.randint(0, 25)):
            advance = rng.choice([0, 0, 1, 7, 100_000, 900_000])
            ts += advance
            host += advance / S
            if rng.random() < 0.03:
                ts -= rng.choice([3 * S, 3600 * S])  # the device clock steps back
            line = entry(f"m{rng.randint(0, 3)}", ts - rng.choice([0, 0, 0, 2 * S]))
            verdict, stepped = _read_checked(plan, line, host, bound)
            if stepped:
                history = []
            if verdict == "new":
                history.append(line)
        if plan.window_lines > plan._max_lines or plan.window_bytes > plan._max_bytes:
            # Only hw's lines exceed a cap.
            assert {item[0] for item in (*plan._window, *plan._late)} == {plan.cursor}
        gap, request = connect(plan, ts // S + 1, at=host)
        assert gap is None
        if plan.cursor is not None:
            _check_resume(plan, history, request, host)


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
    device clock advance together, and a clock step (`step`) moves the clock
    alone. A chatty notice replaces a run of lines at the position of the newest one,
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
        self.steps = 0  # clock steps back

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

    def wait(self, duration: int) -> None:
        """Time passes with nothing logged: the clock and the host run on."""
        self.clock += duration
        self.elapsed += duration / S

    def step(self, back: int) -> None:
        """The device clock steps back by ``back`` microseconds."""
        self.clock -= back
        self.steps += 1

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
    # Every clock_moved_back Gap: when it came (in `events`), its before and
    # after, the window read before that step (see `_window`), and whether it
    # was seen live; and when each text was first delivered. Design 5.6's
    # exception (see `_repeated`).
    steps: list[tuple[int, int, int, frozenset[tuple[int, str]], bool]] = field(
        default_factory=list
    )
    first_delivered: dict[str, int] = field(default_factory=dict)
    read_lines: list[tuple[int, str]] = field(default_factory=list)
    events: int = 0
    repeated: int = 0  # deliveries the exception explains
    # The holes a Gap reports, in arrival order: from the highest line
    # delivered before the replay that reports it began (a connection that
    # ends inside its replay leaves the next one to check the same ring) to
    # the first line read after it. A clock step makes times useless for
    # that: a hole can hold lines stamped below hw. (A resume's re-anchor
    # checks no ring: its Gap marks the point, design 5.6.)
    marks: list[list[int | None]] = field(default_factory=list)
    highest: int = 0  # the highest arrival order delivered
    replay_from: int = 0  # `highest` when the replay being screened began

    def connect(self) -> int:
        """A clock read and a spawn; the ring index the connection starts at."""
        device = self.device
        if self.first is None:
            self.first = device.order
        phase = self.plan._phase
        if phase is None or phase.kind != "resume" or getattr(phase, "passed", False):
            self.replay_from = self.highest
        gap = self.plan.clock(device.clock // S, issued=device.elapsed)
        self.plan.position()
        if gap is not None:
            self.marks.append([self.highest, None])
            self._report([gap], live=False)
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
        for mark in self.marks:
            low = mark[0]
            if mark[1] is None and low is not None and item.order > low:
                mark[1] = item.order
        verdict = self.plan.screen(
            item.entry, len(item.entry.raw) + 1, self.device.elapsed
        )
        if item.replaced:
            self.notices_read.add(id(item))
        if verdict == "new":
            self.highest = max(self.highest, item.order)
            self.delivered[item.entry.raw] += 1
            self.first_delivered.setdefault(item.entry.raw, self.events)
            self.read_lines.append((item.ts, item.entry.raw))
            self.events += 1
        found = gaps(self.plan)
        if any(gap.reason == "ring_rotated" for gap in found):
            self.marks.append([self.replay_from, item.order])
        self._report(found, live=True)

    def _report(self, found: list[Gap], *, live: bool) -> None:
        for gap in found:
            if gap.reason == "clock_moved_back":
                assert gap.after is not None
                assert gap.before is not None
                after = micros(gap.after)
                self.steps.append(
                    (self.events, micros(gap.before), after, self._window(after), live)
                )
            self.events += 1
        self.reported += found

    def _window(self, top: int) -> frozenset[tuple[int, str]]:
        """What the resume window holds of the lines read, when ``top`` is hw: those
        within the overlap, at most the cap on lines, the latest ones."""
        low = top - self.plan._overlap
        lines = [key for key in self.read_lines if low <= key[0] <= top]
        return frozenset(lines[-self.plan._max_lines :])

    def check(self) -> None:
        """No duplicate but design 5.6's exception; every line not delivered
        is a reported loss.

        Byte-identical copies are told apart only by count: of the copies of
        one text, as many as are missing must each lie inside a reported Gap
        or have been replaced by a notice the stream read.
        """
        logged = Counter(item.entry.raw for item in self.device.logged)
        logged.update(item.entry.raw for item in self.device.notices)
        stamps = {
            item.entry.raw: item.ts
            for item in (*self.device.logged, *self.device.notices)
        }
        for raw, count in self.delivered.items():
            if count > logged[raw]:
                assert self._repeated(raw, stamps[raw]), (
                    f"delivered {count} times: {raw}"
                )
                self.repeated += count - logged[raw]
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

    def _repeated(self, raw: str, ts: int) -> bool:
        """Design 5.6's one exception to answer 4: a line read before a clock
        step back, no longer in the window then (`_window`, modelled here, not
        read from the planner), and stamped in the re-anchored range (from the
        Gap's before to its after) can be delivered again."""
        first = self.first_delivered[raw]
        return any(
            when > first and before <= ts <= after and (ts, raw) not in kept
            for when, before, after, kept, _live in self.steps
        )

    def _explained(self, line: _Line) -> bool:
        """Inside a reported Gap, or replaced by a notice the stream read."""
        for low, high in self.marks:
            if (
                low is not None
                and low < line.order
                and (high is None or line.order < high)
            ):
                return True
        if not self.steps:  # without a step, times order the log
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


def _simulate(seed: int, *, steps: bool) -> _Reader:
    """Play the planner against a simulated logd (see the tests below)."""
    rng = random.Random(seed)  # noqa: S311  (a test, not cryptography)
    # The clock steps draw from their own sequence: without them, a seed
    # plays as it always did.
    stepper = random.Random(seed + 1_000_000)  # noqa: S311
    device = _Device(rng, capacity=rng.choice([8, 40, 400]))
    device.log(rng.randint(0, 30))
    # A cap below the lines at one timestamp keeps them all: the caps never
    # evict hw, so the resume can check the ring (see the module docs).
    reader = _Reader(device, planner(max_lines=rng.choice([3, 16, 32_768])))
    attempts = rng.randint(1, 12)
    # With steps, a run steps at least once: from connection `forced` on, the
    # reader first catches up, then the clock steps.
    forced = stepper.randrange(attempts) if steps else attempts
    for attempt in range(attempts):
        last = device.position(reader.connect())  # the reader is before it
        if attempt == 0:
            # A step is reported once a line from past the first clock read's
            # second was read (design A.25).
            device.wait(S if steps else 0)
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
            if steps and not device.steps and attempt >= forced:
                last = _catch_up(device, reader, last)
            if (
                steps
                and device.after(last) >= len(device.ring)
                and (not device.steps or stepper.random() < 0.1)
            ):
                # The clock steps back while the reader has caught up: it
                # reads the next line at once (a step seen live).
                _step(device, reader, stepper)
                last = device.ring[-1].order
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
    if steps and not device.steps:
        _step(device, reader, stepper)  # its connections all ended at once
    return reader


def _catch_up(device: _Device, reader: _Reader, last: int) -> int:
    """The reader reads the rest of the ring, unless logd dropped it (A.6)."""
    if device.rotated > last:
        return last  # the caller's loop ends the connection
    for item in device.ring[device.after(last) :]:
        last = item.order
        reader.read(item)
    return last


def _step(device: _Device, reader: _Reader, stepper: random.Random) -> None:
    """The clock steps back; the caught-up reader reads the next line at once."""
    device.step(stepper.choice([3, 30, 3600]) * S)
    device.log(1)
    reader.read(device.ring[-1])


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
    reader = _simulate(seed, steps=False)

    reader.check()
    assert reader.steps == []
    assert reader.repeated == 0


@pytest.mark.parametrize(
    "seed", _SIM_SEEDS, ids=[f"steps{seed}" for seed in _SIM_SEEDS]
)
def test_simulated_clock_steps_keep_to_the_documented_exception(seed: int) -> None:
    """The simulated logd of the test above, with clock steps back by 3 s,
    30 s or an hour while a reader that has caught up reads on (design
    A.25); every case steps at least once (from a random connection on, the
    reader catches up and the clock steps). Every step is a
    clock_moved_back Gap, seen live; the resumes
    after it, whose -T lies below the old lines, deliver no line twice but
    as design 5.6's exception allows, and lose none outside a reported Gap.
    (Steps during an outage: test_simulated_clock_steps_deliver_no_duplicate.)
    """
    reader = _simulate(seed, steps=True)

    reader.check()
    assert reader.device.steps > 0
    assert len(reader.steps) >= reader.device.steps
    # A second Gap for one step comes only with old lines that left the window
    # delivered again (a limit in design A.25: it over-reports, it loses nothing).
    assert len(reader.steps) == reader.device.steps or reader.repeated > 0
    assert all(live for *_, live in reader.steps)


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
