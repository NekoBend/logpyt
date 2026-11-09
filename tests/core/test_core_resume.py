"""The core's handling of resume (design 5.6, A.6, A.7): `StreamCore` with the planner.

The planner's rules are tested alone in tests/core/test_resume.py; these tests
check what the core makes of them: the ``-T`` of each spawn, the counters and
the line-conservation balance, the order of `Gap` events among the items, the
stall resume and the end that logd causes for a reader behind its ring, the
start marker, and the window's INFO.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from logpyt._core.protocol import (
    Deliver,
    Diagnostic,
    Kill,
    RunCommand,
    Spawn,
    StopRequest,
)
from logpyt._core.resume import micros, moment
from logpyt.adb import _MARKER_TAG, _AdbResult
from logpyt.config import Reconnect
from logpyt.exceptions import AdbExitError, ReadTimeoutError
from logpyt.filters import Package
from logpyt.groupers import GroupBy
from logpyt.models import LogEntry, LogGroup
from logpyt.streams._events import Gap, StateChange, StreamState
from tests.core.harness import (
    APP,
    CLOCK,
    LOOKUP_OK,
    Harness,
    kinds,
    line,
    result,
)

S = StreamState
US = 1_000_000
HW = micros(datetime(2026, 11, 4, 5, 11, 16, 661_446, tzinfo=UTC))  # STAMP


def stamp(ts: int) -> str:
    return moment(ts).strftime("%Y-%m-%d %H:%M:%S.%f +0000")


def at(message: str, ts: int, *, tag: str = "App") -> bytes:
    """One stdout line logged at epoch microsecond ``ts``."""
    return line(message, tag=tag, stamp=stamp(ts))


def chatty(lines: int, ts: int) -> bytes:
    return at(
        f"uid=10014(com.google.android.gms) expire {lines} lines", ts, tag="chatty"
    )


def clock(seconds: int = CLOCK) -> _AdbResult:
    return result(stdout=f"{seconds}\n")


def spawn_T(harness: Harness) -> str | None:  # noqa: N802
    """The -T value of the live connection's Spawn, if any."""
    connection = harness.core.connection
    assert connection is not None
    argv = harness.spawns[connection].argv
    return argv[argv.index("-T") + 1] if "-T" in argv else None


def reconnect(harness: Harness, seconds: int = CLOCK, *, after: float = 1.0) -> None:
    """End the live connection, wait out the backoff, read the clock, spawn."""
    harness.end_connection(1)
    harness.advance(after)
    harness.answer("clock", clock(seconds))
    harness.spawned()


def records(harness: Harness) -> list[object]:
    """Every record the core emitted, in order (items, changes, gaps)."""
    return [
        record
        for command in harness.commands
        if isinstance(command, Deliver)
        for record in command.records
    ]


# -- the request -----------------------------------------------------------------------


def test_resume_argv_has_epoch_T_at_hw_minus_overlap() -> None:  # noqa: N802
    """Design 5.6 step 1 and A.7: -T <sec>.<usec:06d> = hw - overlap, no offset."""
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running(at("a", HW))
    assert spawn_T(harness) is None  # since="backlog"

    reconnect(harness)

    seconds, micro = divmod(HW - 2 * US, US)
    assert spawn_T(harness) == f"{seconds}.{micro:06d}"


def test_replay_is_deduplicated_and_new_lines_go_on() -> None:
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running(
        at("a", HW) + at("twin", HW + 1) + at("twin", HW + 1)
    )
    reconnect(harness)

    harness.stdout(
        at("old", HW - 3 * US)
        + at("a", HW)
        + at("twin", HW + 1)
        + at("twin", HW + 1)
        + at("b", HW + 2)
    )

    assert harness.messages() == ["a", "twin", "twin", "b"]
    stats = harness.core.stats()
    assert (stats.deduplicated, stats.below_cursor, stats.gaps) == (3, 1, 0)
    assert stats.lines == 3 + 5
    assert harness.core.unaccounted_lines() == 0
    assert stats.incomplete == ()


def test_ring_rotated_gap_comes_before_the_first_new_line() -> None:
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running(
        at("a", HW) + at("b", HW + 5)
    )
    reconnect(harness)

    harness.stdout(at("c", HW + US))

    gap = Gap(moment(HW + 5), moment(HW + US), lost=None, reason="ring_rotated")
    emitted = records(harness)
    assert emitted.index(gap) == len(emitted) - 2  # right before "c"
    assert isinstance(emitted[-1], LogEntry)
    assert emitted[-1].message == "c"
    stats = harness.core.stats()
    assert stats.gaps == 1
    assert stats.incomplete == ("gap",)


def test_clock_step_back_reanchors_with_the_host_outage() -> None:
    """The clock read of the resume shows the device clock far below hw: a Gap
    at the clock read, then -T N - outage - overlap (outage on the host clock)."""
    harness = Harness.make(reconnect=Reconnect(delay=5.0))
    harness.running(at("a", HW))
    started = harness.now  # the last line was read here
    harness.end_connection(1)
    harness.advance(5.0)  # backoff; the clock read is issued now
    outage = harness.now - started
    stepped = CLOCK - 3600

    harness.answer("clock", clock(stepped))

    gap = Gap(moment(HW), moment(stepped * US), lost=None, reason="clock_moved_back")
    assert harness.gaps() == [gap]
    assert harness.changes()[-1] == StateChange(S.RECONNECTING, S.CONNECTING, 2, None)
    request = stepped * US - round(outage * US) - 2 * US
    seconds, micro = divmod(request, US)
    assert spawn_T(harness) == f"{seconds}.{micro:06d}"
    harness.spawned()
    harness.stdout(at("old", HW - US) + at("now", stepped * US) + at("a", HW))
    assert harness.messages() == ["a", "now"]
    assert harness.core.stats().deduplicated == 2


def test_outage_runs_to_the_issue_time_of_the_clock_read() -> None:
    """Design A.25: the outage ends when the clock command was issued, not when it
    answered; the answer's delay is not a time the device logged unread lines."""
    harness = Harness.make(reconnect=Reconnect(delay=5.0))
    harness.running(at("a", HW))
    started = harness.now
    harness.end_connection(1)
    harness.advance(5.0)  # the backoff: the clock read is issued now
    issued = harness.now
    harness.advance(3.0)
    stepped = CLOCK - 3600

    harness.answer("clock", clock(stepped))

    request = stepped * US - round((issued - started) * US) - 2 * US
    seconds, micro = divmod(request, US)
    assert spawn_T(harness) == f"{seconds}.{micro:06d}"


def test_live_clock_step_back_reports_a_gap_before_the_line() -> None:
    """A line stamped more than max(resume_overlap, 1 s) below hw on a live
    connection is a clock step seen live: Gap(clock_moved_back) right before
    it, the line delivered; the next resume requests the new times and drops
    the replay of both eras (design 5.6, A.25)."""
    hw = (CLOCK + 1) * US  # past the first clock read's second: the capture's
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running(at("a", hw))
    stepped = hw - 3600 * US

    harness.stdout(at("b", stepped))

    gap = Gap(moment(hw), moment(stepped), lost=None, reason="clock_moved_back")
    emitted = records(harness)
    assert emitted.index(gap) == len(emitted) - 2  # right before "b"
    assert isinstance(emitted[-1], LogEntry)
    assert emitted[-1].message == "b"
    stats = harness.core.stats()
    assert stats.gaps == 1
    assert stats.incomplete == ("gap",)
    reconnect(harness, stepped // US + 5)
    seconds, micro = divmod(stepped - 2 * US, US)
    assert spawn_T(harness) == f"{seconds}.{micro:06d}"
    harness.stdout(at("a", hw) + at("b", stepped) + at("c", stepped + US))
    assert harness.messages() == ["a", "b", "c"]
    stats = harness.core.stats()
    assert (stats.gaps, stats.deduplicated, stats.below_cursor) == (1, 2, 0)
    assert harness.core.unaccounted_lines() == 0


@pytest.mark.parametrize("found", ["device_reported", "clock_moved_back"])
def test_gap_found_mid_connection_precedes_the_open_group(found: str) -> None:
    """With group_by, a Gap found mid-connection is emitted when the planner
    finds it, and a group is delivered when it closes: the group that holds
    the Gap's ``after`` line comes after the Gap (documented, design A.25)."""
    harness = Harness.make(
        reconnect=Reconnect(delay=1.0),
        group_by=GroupBy(keys=("tag",), gap_ms=60_000.0, idle_flush_ms=None),
    ).running(at("a", (CLOCK + 1) * US))  # past the first clock read's second
    t = (CLOCK + 1) * US
    line_ = chatty(3, t + 5) if found == "device_reported" else at("b", t - 3600 * US)

    harness.stdout(line_)

    emitted = records(harness)
    (gap,) = [record for record in emitted if isinstance(record, Gap)]
    assert gap.reason == found
    assert gap.after == moment(t)  # the time of "a"
    (group,) = [record for record in emitted if isinstance(record, LogGroup)]
    assert [entry.message for entry in group] == ["a"]
    assert emitted.index(gap) < emitted.index(group)


@pytest.mark.parametrize("found", ["device_reported", "clock_moved_back"])
def test_gap_found_mid_connection_precedes_held_lines(found: str) -> None:
    """A line held for package attribution (``where`` with Package, design
    5.8) is delivered when the lookup answers, but a Gap found meanwhile is
    emitted when the planner finds it: the held line that holds the Gap's
    ``after`` time comes after the Gap (documented on Gap)."""
    harness = Harness.make(where=Package(APP), reconnect=Reconnect(delay=1.0))
    harness.connecting_spawned()
    t = (CLOCK + 1) * US  # past the first clock read's second
    harness.stdout(at("a", t))  # held while the first lookup runs
    found_in = (
        chatty(5, t + 10) if found == "device_reported" else at("b", t - 3600 * US)
    )
    harness.stdout(found_in)

    harness.answer("lookup", LOOKUP_OK)

    emitted = [r for r in records(harness) if isinstance(r, LogEntry | Gap)]
    (gap,) = [r for r in emitted if isinstance(r, Gap)]
    assert gap.reason == found
    assert gap.after == moment(t)  # the time of "a"
    first = next(r for r in emitted if isinstance(r, LogEntry))
    assert first.message == "a"
    assert emitted.index(gap) < emitted.index(first)


def test_late_line_after_the_replay_is_below_cursor() -> None:
    """The replay's phase ends at the first line above hw: a line read before,
    stored late below the request and returned after the window's last
    element, is not delivered again (resume_overlap 0.5 s: the line is 0.7 s
    late, inside the 1 s step bound, outside the window)."""
    harness = Harness.make(reconnect=Reconnect(delay=1.0), resume_overlap=0.5)
    harness.running(at("a", HW) + at("late", HW - 700_000))
    reconnect(harness)

    harness.stdout(at("a", HW) + at("late", HW - 700_000) + at("b", HW + US))

    assert harness.messages() == ["a", "late", "b"]
    stats = harness.core.stats()
    assert (stats.deduplicated, stats.below_cursor, stats.gaps) == (1, 1, 0)
    assert harness.core.unaccounted_lines() == 0


# -- chatty notices --------------------------------------------------------------------


def test_chatty_notice_after_the_start_is_a_reported_loss() -> None:
    harness = Harness.make().running(at("a", CLOCK * US))

    harness.stdout(chatty(7, CLOCK * US + 3))

    assert harness.gaps() == [
        Gap(moment(CLOCK * US), moment(CLOCK * US + 3), 7, "device_reported")
    ]
    assert harness.messages()[-1].startswith("uid=10014")  # delivered as a line
    stats = harness.core.stats()
    assert (stats.device_lost, stats.gaps, stats.backlog_pruned) == (7, 1, 0)
    assert stats.incomplete == ("gap", "device_lost")


def test_chatty_notice_in_the_backlog_or_replay_is_not_a_loss() -> None:
    """Below S (the first clock read): backlog_pruned; at or below hw in a
    resume's replay: a replay of pruning after we read those lines."""
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running(
        chatty(4, HW - 3600 * US) + at("a", HW) + at("b", HW + 9)
    )
    reconnect(harness)

    harness.stdout(chatty(2, HW) + at("c", HW + US))  # "a" and "b" were pruned

    stats = harness.core.stats()
    assert (stats.backlog_pruned, stats.device_lost, stats.gaps) == (4, 0, 0)
    assert stats.deduplicated == 1
    assert harness.messages()[-1] == "c"
    assert stats.incomplete == ()


# -- the stall resume and logd's end of a reader behind its ring -----------------------


def test_stall_resume_requests_hw_and_reports_the_overwrite() -> None:
    """Design 5.6: a stalled reader is killed and resumed at hw - overlap, with
    no attempt and no error; the replay shows the ring overwrote hw."""
    harness = Harness.make(buffer_entries=4, stall_timeout=1.0)
    harness.running(at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3))
    harness.advance(1.0)  # stall: Kill(c)
    harness.end_connection(-9)
    assert harness.changes()[-1] == StateChange(S.RUNNING, S.RECONNECTING, 1, None)
    harness.consume()  # intake reopens: CONNECTING at once
    assert harness.core.state is S.CONNECTING
    harness.answer("clock", clock())
    seconds, micro = divmod(HW + 3 - 2 * US, US)
    assert spawn_T(harness) == f"{seconds}.{micro:06d}"
    harness.spawned()

    harness.stdout(at("x", HW + 2 * US))  # the ring holds nothing we read

    stats = harness.core.stats()
    assert (stats.stall_resumes, stats.reconnects, stats.gaps) == (1, 1, 1)
    assert harness.core._attempts == 0
    assert harness.gaps()[0].reason == "ring_rotated"


@pytest.mark.parametrize(
    "reconnect", [None, Reconnect(max_attempts=1)], ids=["none", "set"]
)
def test_end_after_backpressure_resumes_like_a_stall(
    reconnect: Reconnect | None,
) -> None:
    """A.6: logd ended the reader (EOF, rc 1) after the outbox was full; the
    stall timer had not fired. The run resumes without an attempt or error,
    also without reconnect, and the resume's screen runs."""
    harness = Harness.make(buffer_entries=4, stall_timeout=5.0, reconnect=reconnect)
    harness.running(at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3))
    assert not harness.core.intake_open  # full: the reader is behind
    harness.advance(0.5)
    harness.consume()  # intake reopens before stall_timeout

    out = harness.end_connection(1)

    assert harness.core.state is S.CONNECTING  # intake is open: at once
    assert harness.changes()[-2] == StateChange(S.RUNNING, S.RECONNECTING, 1, None)
    assert any(
        isinstance(cmd, Diagnostic)
        and cmd.level == logging.INFO
        and "ended with status 1 soon after reading from it was paused" in cmd.message
        for cmd in out
    )
    stats = harness.core.stats()
    assert (stats.stall_resumes, stats.reconnects) == (0, 1)
    assert harness.core._attempts == 0
    harness.answer("clock", clock())
    assert spawn_T(harness) is not None


def test_end_after_backpressure_without_the_stall_rule_fails() -> None:
    """stall_timeout=None turns the rule off: the end is an adb error."""
    harness = Harness.make(buffer_entries=4, stall_timeout=None)
    harness.running(at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3))
    harness.consume()

    harness.end_connection(1)

    assert harness.core.state is S.FAILED
    assert isinstance(harness.core.outcome, AdbExitError)


def test_end_after_the_first_lookup_resumes_like_a_stall() -> None:
    """A.6: the first package lookup closed intake, so the reader stopped as
    behind a full outbox; an end with status 1 soon after resumes too."""
    harness = Harness.make(where=Package(APP))
    harness.connecting_spawned()
    assert not harness.core.intake_open  # the first lookup runs
    harness.advance(1.5)
    harness.answer("lookup", LOOKUP_OK)
    harness.stdout(at("a", HW))

    harness.end_connection(1)

    assert harness.core.state is S.CONNECTING
    assert harness.changes()[-2] == StateChange(S.RUNNING, S.RECONNECTING, 1, None)
    assert not harness.core.stats().backpressure_seconds  # not the outbox


def test_end_long_after_backpressure_is_an_ordinary_end() -> None:
    """A.6's resume needs intake closed recently: an end with status 1 after
    intake stayed open for stall_timeout and more is an adb error."""
    harness = Harness.make(buffer_entries=4, stall_timeout=1.0)
    harness.running(at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3))
    assert not harness.core.intake_open  # full once
    harness.consume()
    for i in range(3):
        harness.advance(1.0)
        harness.stdout(at(f"x{i}", HW + US + i))
        harness.consume()

    harness.end_connection(1)

    assert harness.core.state is S.FAILED
    assert isinstance(harness.core.outcome, AdbExitError)


def test_end_without_backpressure_is_an_ordinary_end() -> None:
    harness = Harness.make(stall_timeout=1.0)
    harness.running(at("a", HW))

    harness.end_connection(1)

    assert harness.core.state is S.FAILED


def _behind(**fields: Any) -> Harness:  # noqa: ANN401
    """A running connection whose outbox was full, reopened 0.5 s ago."""
    harness = Harness.make(
        buffer_entries=4,
        stall_timeout=5.0,
        reconnect=Reconnect(delay=1.0, max_attempts=3),
        **fields,
    )
    harness.running(at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3))
    harness.advance(0.5)
    harness.consume()
    return harness


def test_status_1_after_backpressure_is_the_control() -> None:
    harness = _behind()

    harness.end_connection(1)

    assert harness.changes()[-2] == StateChange(S.RUNNING, S.RECONNECTING, 1, None)
    assert harness.core.stats().reconnects == 1


def test_status_255_after_backpressure_is_an_ordinary_end() -> None:
    """A.6 is status 1 only: 255 is a disconnect (FINDINGS.md check 4), with its
    error, its backoff and no reconnect counted yet."""
    harness = _behind()

    harness.end_connection(255)

    assert harness.core.state is S.RECONNECTING
    assert isinstance(harness.changes()[-1].error, AdbExitError)
    assert harness.core.stats().reconnects == 0


def test_read_timeout_ended_with_status_1_stays_a_timeout() -> None:
    """A.6 needs no cause from the core: an adb the read timeout killed that ends
    with status 1 (from memory, any kill on Windows) is still a read timeout."""
    harness = _behind(read_timeout=2.0)
    assert [cmd for cmd in harness.advance(2.0) if isinstance(cmd, Kill)]

    harness.end_connection(1)

    assert harness.core.state is S.RECONNECTING
    assert isinstance(harness.changes()[-1].error, ReadTimeoutError)
    assert harness.core.stats().reconnects == 0


# -- a line cut at the end of a connection ---------------------------------------------


def test_stall_kill_cuts_a_line_and_the_replay_returns_it() -> None:
    """A stall kill under a flood cuts adb's write in a line. The resume's replay
    returns the whole line, so the cut one is dropped and counted: it moves no
    hw, enters no window, and neither reaches the consumer nor costs a Gap
    (design A.25, 4.4: a stall resume without a gap keeps a capture complete)."""
    harness = Harness.make(buffer_entries=4, stall_timeout=1.0)
    whole = at("e", HW + 4)
    harness.running(
        at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3) + whole[:40]
    )
    harness.advance(1.0)  # stall: Kill(c)

    harness.end_connection(-9)

    assert harness.core.stats().deduplicated == 1
    assert harness.core._planner.cursor == HW + 3
    harness.consume()
    harness.answer("clock", clock())
    harness.spawned()
    replay = at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3)
    harness.stdout(replay + whole + at("f", HW + 5))
    harness.consume()
    assert harness.messages() == ["a", "b", "c", "d", "e", "f"]
    assert harness.gaps() == []
    stats = harness.core.stats()
    assert stats.incomplete == ()
    assert stats.lines == stats.delivered + stats.deduplicated


def test_cut_line_of_a_connection_that_reconnects_is_dropped() -> None:
    """The same at any end the run resumes from."""
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running(at("a", HW))
    whole = at("message", HW + 1)
    harness.stdout(whole[:-4])

    reconnect(harness)
    harness.stdout(at("a", HW) + whole)

    assert harness.messages() == ["a", "message"]
    assert harness.gaps() == []
    assert harness.core.stats().incomplete == ()


def test_cut_line_at_a_resume_after_backpressure_is_dropped() -> None:
    """A.6 resumes also without reconnect, so the replay returns the whole line."""
    harness = Harness.make(buffer_entries=4, stall_timeout=5.0, reconnect=None)
    harness.running(at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3))
    harness.advance(0.5)
    harness.consume()
    whole = at("e", HW + 4)
    harness.stdout(whole[:-1])

    harness.end_connection(1)

    assert harness.core.state is S.CONNECTING
    assert harness.messages() == ["a", "b", "c", "d"]
    assert harness.core.stats().deduplicated == 1
    harness.answer("clock", clock())
    harness.spawned()
    harness.stdout(at("d", HW + 3) + whole)
    assert harness.messages() == ["a", "b", "c", "d", "e"]


def test_cut_line_of_a_run_that_ends_is_emitted() -> None:
    """Design 5.5: the partial last line of a run that ends here is emitted, also
    when it ends FAILED."""
    harness = Harness.make(stall_timeout=1.0).running(at("a", HW))
    harness.stdout(at("b", HW + 1)[:-1])

    harness.end_connection(1)

    assert harness.core.state is S.FAILED
    assert harness.messages() == ["a", "b"]
    assert harness.core.stats().deduplicated == 0


def test_cut_line_of_a_stopping_run_is_emitted() -> None:
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running(at("a", HW))
    harness.stdout(at("b", HW + 1)[:-1])
    harness.send(StopRequest())

    harness.end_connection(0)

    assert harness.messages() == ["a", "b"]


def test_cut_line_before_the_start_marker_counts_in_before_start() -> None:
    harness = Harness.make(since="now", reconnect=Reconnect(delay=1.0))
    harness.connecting_spawned()
    harness.stdout(at("old", HW)[:30])

    harness.end_connection(255)

    stats = harness.core.stats()
    assert (stats.lines, stats.before_start, stats.deduplicated) == (1, 1, 0)


def test_replay_counts_no_truncated_and_no_unparsed() -> None:
    """Only a line of the capture counts: a replay of a cut or an unparsed line
    does not add to the counters."""
    harness = Harness.make(reconnect=Reconnect(delay=1.0), max_line_bytes=4096)
    lines = at("a", HW) + b"no header at all\n" + at("x" * 5000, HW + 1)
    harness.running(lines)
    stats = harness.core.stats()
    assert (stats.truncated, stats.unparsed) == (1, 1)

    reconnect(harness)
    harness.stdout(lines)

    stats = harness.core.stats()
    assert (stats.truncated, stats.unparsed, stats.deduplicated) == (1, 1, 3)


def test_cut_line_below_since_is_no_reason_of_incompleteness() -> None:
    """A line below since= is dropped before the capture starts: it says nothing
    about the capture, cut or not (design 4.4)."""
    harness = Harness.make(since=moment(HW), max_line_bytes=4096)
    harness.connecting_spawned()

    harness.stdout(at("x" * 5000, HW - US) + b"no header\n" + at("a", HW))

    stats = harness.core.stats()
    assert (stats.truncated, stats.unparsed, stats.below_cursor) == (0, 0, 2)
    assert stats.incomplete == ()


# -- since, the window, the INFO -------------------------------------------------------


def test_since_now_start_marker_is_S_and_the_floor() -> None:  # noqa: N802
    """Lines up to the marker are before_start (a chatty notice there counts
    nowhere else); the marker's timestamp is S; a resume never requests below
    the first connection's -T."""
    harness = Harness.make(since="now", reconnect=Reconnect(delay=1.0))
    harness.connecting_spawned()
    conn = harness.core._conn
    assert conn is not None
    marker = conn.marker
    assert marker is not None
    harness.stdout(
        chatty(3, HW - US)
        + at(marker + " ", HW, tag=_MARKER_TAG)  # API 24 appends a space
        + chatty(1, HW - 1)
        + at("live", HW + 1)
    )
    assert harness.core.state is S.RUNNING

    reconnect(harness, CLOCK + 60)

    stats = harness.core.stats()
    assert (stats.before_start, stats.backlog_pruned, stats.device_lost) == (2, 1, 0)
    assert spawn_T(harness) == f"{CLOCK - 1}.000000"
    replay = at(marker + " ", HW, tag=_MARKER_TAG) + at("live", HW + 1)
    harness.stdout(replay + at("n", HW + 2))
    assert harness.messages()[-2:] == ["live", "n"]  # no marker, no replay


def test_since_datetime_drops_lines_below_it() -> None:
    """A.7: the device can return lines below -T; they are below_cursor."""
    harness = Harness.make(since=moment(HW))
    harness.connecting_spawned()
    seconds, micro = divmod(HW, US)
    assert spawn_T(harness) == f"{seconds}.{micro:06d}"

    harness.stdout(at("early", HW - 1) + at("at", HW) + at("late", HW - 1))

    assert harness.messages() == ["at", "late"]
    assert harness.core.stats().below_cursor == 1


def test_window_cap_info_once_per_connection_and_shortened_resume() -> None:
    harness = Harness.make(reconnect=Reconnect(delay=1.0), resume_window_lines=2)
    harness.running(at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3))
    infos = [
        cmd
        for cmd in harness.diagnostics
        if cmd.level == logging.INFO and "resume_window_lines=2" in cmd.message
    ]
    assert len(infos) == 1
    assert infos[0].message.startswith("logcat connection 1: ")

    reconnect(harness)

    seconds, micro = divmod(HW + 2, US)  # max_evicted + 1 us
    assert spawn_T(harness) == f"{seconds}.{micro:06d}"
    harness.stdout(at("c", HW + 2))
    assert harness.core.stats().overlap_shortened == 1
    assert harness.core.stats().incomplete == ()


def test_window_is_kept_only_when_the_run_can_resume() -> None:
    kept = Harness.make(stall_timeout=1.0).running(at("a", HW))
    dropped = Harness.make(stall_timeout=None).running(at("a", HW))

    assert kept.core._planner.window_lines == 1
    assert dropped.core._planner.window_lines == 0


def test_stalled_connection_is_killed_once() -> None:
    harness = Harness.make(buffer_entries=4, stall_timeout=1.0)
    harness.running(at("a", HW) + at("b", HW + 1) + at("c", HW + 2) + at("d", HW + 3))

    out = harness.advance(1.0)

    assert kinds(out) == ["Kill"]
    assert [cmd for cmd in out if isinstance(cmd, Kill)] == [Kill(1)]
    assert not [cmd for cmd in out if isinstance(cmd, RunCommand | Spawn)]
