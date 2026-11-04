"""Behavior of AsyncLogStream against a real fake-adb child process.

These tests pin what a user of ``AsyncLogStream`` relies on, through the public
API only (constructor arguments, start/stop/kill/join, callbacks, hooks,
exceptions, ``async with``), so they must survive an internal rewrite.

Known bugs are written for the correct behavior and marked
``xfail(strict=True, raises=AssertionError)`` with the plan id from
``.docs/review-findings-2.0.1.md``, so a harness error or timeout is reported
as a failure, not as the expected defect. Where the correct behavior is still
an open API decision (``D-*``), only what is clearly required is asserted.

Interleavings "during the spawn" use the harness spawn gate
(``FakeAdb.hold_spawn``), and "adb is gone" checks use the harness child
registry (``FakeAdb.async_survivors``), so both hold on every platform.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import TYPE_CHECKING

import pytest

from logpyt import AsyncLogStream, GroupBy, LogStreamError, LogStreamKilledError
from logpyt.filters import Condition
from logpyt.streams import StreamState
from tests.conftest import (
    FakeAdb,
    async_wait_until,
    err,
    lines,
    out,
    run,
    sleep,
    threadtime,
    touch,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from logpyt import AsyncStreamHandle, LogEntry, LogGroup
    from tests.conftest import SpawnedChild

# Generous bound for "the stream finishes" when nothing should block it. It also
# bounds every start()/stop()/kill() call, so a hang fails the test, not CI.
JOIN_TIMEOUT = 10.0
# Bound for "the stream ends promptly" when, without the fix, it never ends.
PROMPT_END_TIMEOUT = 2.0
# Sync stop() escalates terminate() -> kill() after 2 s; leave margin for that.
ESCALATION_TIMEOUT = 4.0

TERMINAL_STATES = frozenset({StreamState.STOPPED, StreamState.KILLED})


class _Hung:
    """Outcome of an await that did not finish within its bound."""

    def __repr__(self) -> str:
        return "<join did not return>"


HUNG = _Hung()


async def outcome_of(
    awaitable: Awaitable[object], timeout: float
) -> LogStreamError | _Hung | None:
    """Await ``awaitable``, bounded by ``timeout``, and report how it ended."""
    try:
        await asyncio.wait_for(awaitable, timeout)
    except TimeoutError:
        return HUNG
    except LogStreamError as exc:
        return exc
    return None


async def join_outcome(
    stream: AsyncLogStream, timeout: float = JOIN_TIMEOUT
) -> LogStreamError | _Hung | None:
    """Join the stream, bounded by ``timeout``, and report how it ended.

    ``join()`` is called without its own timeout so that a hang (``HUNG``) can
    never be confused with a ``LogStreamTimeoutError`` raised by the stream.
    """
    return await outcome_of(stream.join(), timeout)


async def bounded(awaitable: Awaitable[object], timeout: float = JOIN_TIMEOUT) -> None:
    """Await a start()/stop()/kill() call, failing instead of hanging."""
    await asyncio.wait_for(awaitable, timeout)


async def shutdown(stream: AsyncLogStream) -> None:
    """Kill the stream and wait (bounded) for it, whatever state it is in."""
    with contextlib.suppress(LogStreamError, TimeoutError):
        await asyncio.wait_for(stream.kill(), JOIN_TIMEOUT)
    with contextlib.suppress(LogStreamError, TimeoutError):
        await asyncio.wait_for(stream.join(), JOIN_TIMEOUT)


async def logcat_survivors(
    fake: FakeAdb, timeout: float = PROMPT_END_TIMEOUT
) -> list[SpawnedChild]:
    """Fake ``adb logcat`` children still alive after up to ``timeout``.

    Reads the harness child registry, so it checks on every platform and
    never takes an exit status away from the library.
    """
    return await fake.async_survivors(timeout, command="logcat")


class Recorder:
    """Collects what the stream hands to its callbacks."""

    def __init__(self) -> None:
        self.entries: list[LogEntry] = []
        self.stderr_entries: list[LogEntry] = []
        self.errors: list[Exception] = []
        self.got_entry = asyncio.Event()

    async def on_stdout(self, entry: LogEntry, _handle: AsyncStreamHandle) -> None:
        self.entries.append(entry)
        self.got_entry.set()

    async def on_stderr(self, entry: LogEntry, _handle: AsyncStreamHandle) -> None:
        self.stderr_entries.append(entry)

    async def on_error(self, exc: Exception) -> None:
        self.errors.append(exc)

    @property
    def messages(self) -> list[str]:
        return [entry.message for entry in self.entries]

    async def first_entry(self) -> None:
        await asyncio.wait_for(self.got_entry.wait(), JOIN_TIMEOUT)


def _stream(rec: Recorder, **kwargs) -> AsyncLogStream:
    return AsyncLogStream(
        stdout_callback=rec.on_stdout,
        stderr_callback=rec.on_stderr,
        on_error=rec.on_error,
        **kwargs,
    )


# -- delivery -------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_line_is_delivered_in_order_when_adb_exits_right_away(
    fake_adb: FakeAdb,
) -> None:
    """Every line adb wrote before exiting reaches stdout_callback, in order.

    The queue is sized above the line count so this pins EOF/tail handling
    only; queue overflow with default settings is X-1 (tested separately).
    """
    count = 2000
    fake_adb.logcat(run(lines(count, threadtime("m{i}"))))
    rec = Recorder()
    stream = _stream(rec, callback_queue_size=4096)
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
    finally:
        await shutdown(stream)

    assert outcome is None
    assert rec.messages == [f"m{i}" for i in range(count)]
    assert rec.errors == []


@pytest.mark.asyncio
async def test_bare_carriage_return_inside_a_message_stays_one_entry(
    fake_adb: FakeAdb,
) -> None:
    """A bare \\r inside a message does not split it into a second entry."""
    forged = threadtime("forged", pid=6666, tid=6666, level="E", tag="Evil").rstrip()
    fake_adb.logcat(run(out(threadtime(f"before\r{forged}"))))
    rec = Recorder()
    stream = _stream(rec)
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
    finally:
        await shutdown(stream)

    assert outcome is None
    assert len(rec.entries) == 1
    (entry,) = rec.entries
    assert (entry.pid, entry.tag) == (1234, "Tag")
    assert entry.message.startswith("before\r")
    assert entry.message.endswith("forged")


@pytest.mark.asyncio
async def test_stderr_output_reaches_only_stderr_callback(fake_adb: FakeAdb) -> None:
    """adb's stderr text reaches stderr_callback, and stdout_callback never sees it."""
    fake_adb.logcat(
        run(err("adb: warning from stderr\n"), out(threadtime("from stdout")))
    )
    rec = Recorder()
    stream = _stream(rec)
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
    finally:
        await shutdown(stream)

    assert outcome is None
    assert rec.messages == ["from stdout"]
    assert [e.message for e in rec.stderr_entries] == ["adb: warning from stderr"]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="X-1: default queue drops a burst; ingestion starves the dispatcher",
)
@pytest.mark.asyncio
async def test_burst_of_10000_lines_is_fully_delivered_with_defaults(
    fake_adb: FakeAdb,
) -> None:
    """A 10,000-line burst waiting at start is fully delivered to a trivial callback.

    ``adb logcat`` dumps its whole ring buffer on start (tens of thousands of
    lines on a booted emulator, .docs/device-probes-2026-11-04.md), so this is
    default use. The spawn gate holds the spawn until adb has written the
    whole burst: asyncio keeps reading adb's pipe meanwhile, so all of the
    burst except what the OS pipe still holds is already buffered when the
    stream reads its first line. Without the gate, the lines read per
    event-loop turn follow the platform's pipe size (Windows CI passed it).
    """
    count = 10_000
    fake_adb.logcat(run(lines(count, threadtime("m{i}")), touch("burst-written")))
    delivered: list[str] = []

    async def on_entry(entry: LogEntry, _handle: AsyncStreamHandle) -> None:
        delivered.append(entry.message)

    stream = AsyncLogStream(stdout_callback=on_entry)
    gate = fake_adb.hold_spawn(command="logcat")
    try:
        await bounded(stream.start())
        await gate.async_wait_entered()
        await fake_adb.async_wait_for("burst-written")
        gate.release()
        outcome = await join_outcome(stream)
    finally:
        gate.release()
        await shutdown(stream)

    assert outcome is None
    assert len(delivered) == count
    assert delivered == [f"m{i}" for i in range(count)]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F1-3: EOF dispatcher drain is capped at 2 s and discards the rest",
)
@pytest.mark.asyncio
async def test_all_accepted_entries_are_delivered_after_eof_with_a_slow_callback(
    fake_adb: FakeAdb,
) -> None:
    """Entries read before adb exits are all delivered, however slow the callback."""
    count = 30
    fake_adb.logcat(run(lines(count, threadtime("m{i}"))))
    delivered: list[str] = []

    async def slow_callback(entry: LogEntry, _handle: AsyncStreamHandle) -> None:
        await asyncio.sleep(0.1)
        delivered.append(entry.message)

    stream = AsyncLogStream(stdout_callback=slow_callback)
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
    finally:
        await shutdown(stream)

    assert outcome is None
    assert delivered == [f"m{i}" for i in range(count)]


@pytest.mark.asyncio
async def test_group_by_delivers_log_groups_to_the_callback(fake_adb: FakeAdb) -> None:
    """With group_by, stdout_callback receives LogGroup objects (design 2.6).

    Two pids interleave, a line one second later expires both, and the group
    still open when adb exits is delivered with reason "end".
    """
    fake_adb.logcat(
        run(
            out(threadtime("a1", pid=1, tid=1, time_="03:04:05.000")),
            out(threadtime("b1", pid=2, tid=2, time_="03:04:05.001")),
            out(threadtime("a2", pid=1, tid=1, time_="03:04:05.002")),
            out(threadtime("late", pid=3, tid=3, time_="03:04:06.000")),
        )
    )
    rec = Recorder()
    groups: list[LogGroup] = []

    async def on_group(group: LogGroup, _handle: AsyncStreamHandle) -> None:
        groups.append(group)

    stream = AsyncLogStream(
        group_by=GroupBy(keys=("pid",), gap_ms=100.0, mode="windowed"),
        stdout_callback=on_group,
        on_error=rec.on_error,
    )
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
    finally:
        await shutdown(stream)

    assert outcome is None
    assert rec.errors == []
    assert [(g.key, [e.message for e in g], g.reason) for g in groups] == [
        ((2,), ["b1"], "gap"),
        ((1,), ["a1", "a2"], "gap"),
        ((3,), ["late"], "end"),
    ]


# -- read_timeout -----------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F4-1: read_timeout is per pipe, so the silent stderr kills the stream",
)
@pytest.mark.asyncio
async def test_read_timeout_does_not_end_a_stream_whose_stdout_is_active(
    fake_adb: FakeAdb,
) -> None:
    """A stream whose stdout keeps producing lines outlives read_timeout.

    stderr stays silent, as it does for a healthy ``adb logcat``. Lines come
    every 0.3 s, far inside read_timeout, and keep coming past it.
    """
    ticks = 7  # last tick at ~1.8 s, after read_timeout has elapsed
    steps = [out(threadtime("tick 0"))]
    for i in range(1, ticks):
        steps += [sleep(0.3), out(threadtime(f"tick {i}"))]
    fake_adb.logcat(run(*steps))
    rec = Recorder()
    stream = _stream(rec, read_timeout=1.5)
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
    finally:
        await shutdown(stream)

    assert rec.messages == [f"tick {i}" for i in range(ticks)]
    assert rec.errors == []
    assert outcome is None


@pytest.mark.asyncio
async def test_read_timeout_ends_a_silent_stream_and_join_raises(
    fake_adb: FakeAdb,
) -> None:
    """When adb writes nothing for read_timeout, the stream ends and join() raises.

    The exact exception type is open (D-3); a LogStreamError subclass other
    than the user-kill error is required, and the silent adb must be gone.
    """
    fake_adb.logcat(run(hang=True))
    rec = Recorder()
    stream = _stream(rec, read_timeout=0.3)
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
        survivors = await logcat_survivors(fake_adb)
    finally:
        await shutdown(stream)

    assert outcome is not HUNG
    assert isinstance(outcome, LogStreamError)
    assert not isinstance(outcome, LogStreamKilledError)
    assert survivors == []


# -- exit status ------------------------------------------------------------

# A failed adb logcat for the exit-status tests. Not the absent-device case:
# real ``adb logcat`` blocks waiting for an absent device instead of exiting
# (.docs/device-probes-2026-11-04.md). 255 is the one non-zero logcat exit
# status observed on a device (cause unknown); the stderr text is neutral.
FAILED_LOGCAT_EXIT = 255
FAILED_LOGCAT_STDERR = "fake adb: logcat failed\n"


@pytest.mark.asyncio
async def test_non_zero_adb_exit_makes_join_raise(fake_adb: FakeAdb) -> None:
    """When adb exits with a non-zero status, join() raises a LogStreamError.

    The failure is reported as an error, not as a user kill.
    """
    fake_adb.logcat(run(err(FAILED_LOGCAT_STDERR), exit_code=FAILED_LOGCAT_EXIT))
    rec = Recorder()
    stream = _stream(rec)
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
    finally:
        await shutdown(stream)

    assert isinstance(outcome, LogStreamError)
    assert not isinstance(outcome, LogStreamKilledError)


@pytest.mark.asyncio
async def test_zero_adb_exit_makes_join_return_normally(fake_adb: FakeAdb) -> None:
    """When adb exits with status 0, join() returns and the stream is STOPPED."""
    fake_adb.logcat(run(out(threadtime("bye"))))
    rec = Recorder()
    stream = _stream(rec)
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
        state = stream.state
    finally:
        await shutdown(stream)

    assert outcome is None
    assert state is StreamState.STOPPED
    assert rec.messages == ["bye"]
    assert rec.errors == []


# -- stop / kill --------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_ends_a_stream_whose_adb_never_exits(fake_adb: FakeAdb) -> None:
    """stop() ends a stream whose adb would run forever; join() then returns.

    The adb child is gone afterwards, and a requested stop is not an error.
    """
    fake_adb.logcat(run(out(threadtime("alive")), hang=True))
    rec = Recorder()
    stream = _stream(rec)
    try:
        await bounded(stream.start())
        await rec.first_entry()
        await bounded(stream.stop())
        outcome = await join_outcome(stream)
        state = stream.state
        survivors = await logcat_survivors(fake_adb)
    finally:
        await shutdown(stream)

    assert outcome is None
    assert state is StreamState.STOPPED
    assert survivors == []
    assert rec.errors == []


@pytest.mark.asyncio
async def test_kill_of_a_running_stream_makes_join_raise_killed(
    fake_adb: FakeAdb,
) -> None:
    """kill() ends a running stream and its adb; join() raises LogStreamKilledError."""
    fake_adb.logcat(run(out(threadtime("alive")), hang=True))
    rec = Recorder()
    stream = _stream(rec)
    try:
        await bounded(stream.start())
        await rec.first_entry()
        await bounded(stream.kill())
        outcome = await join_outcome(stream)
        state = stream.state
        survivors = await logcat_survivors(fake_adb)
    finally:
        await shutdown(stream)

    assert isinstance(outcome, LogStreamKilledError)
    assert state is StreamState.KILLED
    assert survivors == []


@pytest.mark.asyncio
async def test_kill_right_after_start_makes_join_raise_killed(
    fake_adb: FakeAdb,
) -> None:
    """kill() right after start() returns makes join() raise LogStreamKilledError.

    In 2.0.0, start() only schedules the spawn and kill() does not yield, so
    this kill lands before any adb is spawned and there is no child to check.
    A kill during the spawn and one after it are pinned, each with an "adb is
    gone" check, by the next test and by
    test_kill_of_a_running_stream_makes_join_raise_killed.
    """
    fake_adb.logcat(run(out(threadtime("alive")), hang=True))
    rec = Recorder()
    stream = _stream(rec)
    try:
        await bounded(stream.start())
        await bounded(stream.kill())
        outcome = await join_outcome(stream)
        state = stream.state
    finally:
        await shutdown(stream)

    assert isinstance(outcome, LogStreamKilledError)
    assert state is StreamState.KILLED


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F2-1: RUNNING after spawn overwrites a kill() issued during the spawn",
)
@pytest.mark.asyncio
async def test_kill_while_adb_is_being_spawned_makes_join_raise_killed(
    fake_adb: FakeAdb,
) -> None:
    """kill() while adb is being spawned makes join() raise LogStreamKilledError.

    The spawn gate holds the spawn after the adb child exists but before the
    library receives it, so kill() lands inside the spawn every time. The adb
    spawned meanwhile must not outlive the stream.
    """
    fake_adb.logcat(run(out(threadtime("alive")), hang=True))
    rec = Recorder()
    stream = _stream(rec)
    gate = fake_adb.hold_spawn(command="logcat")
    try:
        await bounded(stream.start())
        await gate.async_wait_entered()
        await bounded(stream.kill())
        gate.release()
        outcome = await join_outcome(stream)
        survivors = await logcat_survivors(fake_adb)
    finally:
        gate.release()
        await shutdown(stream)

    assert isinstance(outcome, LogStreamKilledError)
    assert survivors == []


async def _stop_then_join(stream: AsyncLogStream) -> None:
    await stream.stop()
    await stream.join()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM cannot be ignored on Windows (terminate() is kill())",
)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F2-10: async stop() never escalates terminate() to kill()",
)
@pytest.mark.asyncio
async def test_stop_ends_an_adb_that_ignores_sigterm(fake_adb: FakeAdb) -> None:
    """stop() still ends an adb that ignores SIGTERM, within a bounded time.

    The bound covers stop() and join() together, so it holds whether a rewrite
    escalates inside stop() or in the background.
    """
    fake_adb.logcat(run(touch("sigterm-ignored"), hang=True, ignore_sigterm=True))
    rec = Recorder()
    stream = _stream(rec)
    try:
        await bounded(stream.start())
        await fake_adb.async_wait_for("sigterm-ignored")
        outcome = await outcome_of(_stop_then_join(stream), ESCALATION_TIMEOUT)
        # Asserted before shutdown(), whose kill() would end adb anyway.
        assert outcome is not HUNG
        assert await logcat_survivors(fake_adb) == []
    finally:
        await shutdown(stream)


# -- user code that raises ------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F2-5: a raising on_state(STARTING) leaves the stream stuck in STARTING",
)
@pytest.mark.asyncio
async def test_raising_on_state_starting_hook_does_not_strand_the_stream(
    fake_adb: FakeAdb,
) -> None:
    """An on_state hook that raises on STARTING does not leave the stream stuck there.

    Either start() swallows the hook error and the stream runs, or start()
    reports it (as the hook's error or a LogStreamError) and the stream can be
    started again.
    """
    fake_adb.logcat(run(out(threadtime("ran"))))
    rec = Recorder()
    raised: list[StreamState] = []

    async def on_state(state: StreamState) -> None:
        if state is StreamState.STARTING and not raised:
            raised.append(state)
            msg = "hook bug"
            raise RuntimeError(msg)

    stream = _stream(rec, on_state=on_state)
    try:
        try:
            await bounded(stream.start())
        except (RuntimeError, LogStreamError):
            assert stream.state is not StreamState.STARTING
            await bounded(stream.start())
        outcome = await join_outcome(stream)
        state = stream.state
    finally:
        await shutdown(stream)

    assert raised == [StreamState.STARTING]
    assert outcome is None
    assert state is StreamState.STOPPED
    assert rec.messages == ["ran"]


class _ExplodingCondition(Condition):
    """A user condition that fails on one particular entry."""

    def __call__(self, entry: LogEntry) -> bool:
        if entry.message == "m1":
            msg = "filter bug"
            raise ValueError(msg)
        return True


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F3-1: a filter_by exception ends the reader but leaves adb running",
)
@pytest.mark.asyncio
async def test_raising_filter_ends_the_stream(fake_adb: FakeAdb) -> None:
    """A filter_by predicate that raises ends the stream and its adb instead of hanging.

    Whether join() then raises or returns is not asserted.
    """
    fake_adb.logcat(run(lines(3, threadtime("m{i}")), hang=True))
    rec = Recorder()
    stream = _stream(rec, filter_by=_ExplodingCondition())
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream, timeout=PROMPT_END_TIMEOUT)
        # Asserted before shutdown(), whose kill() would end adb anyway.
        assert outcome is not HUNG
        assert stream.state in TERMINAL_STATES
        assert await logcat_survivors(fake_adb) == []
    finally:
        await shutdown(stream)


@pytest.mark.asyncio
async def test_raising_data_callback_reports_to_on_error_and_stream_continues(
    fake_adb: FakeAdb,
) -> None:
    """A raising stdout_callback reports to on_error, and later entries still arrive.

    Whether join() then raises is open (D-2) and not asserted.
    """
    fake_adb.logcat(run(lines(5, threadtime("m{i}"))))
    seen: list[str] = []
    errors: list[Exception] = []
    boom = ValueError("callback bug")

    async def on_entry(entry: LogEntry, _handle: AsyncStreamHandle) -> None:
        seen.append(entry.message)
        if entry.message == "m1":
            raise boom

    async def on_error(exc: Exception) -> None:
        errors.append(exc)

    stream = AsyncLogStream(stdout_callback=on_entry, on_error=on_error)
    try:
        await bounded(stream.start())
        outcome = await join_outcome(stream)
    finally:
        await shutdown(stream)

    assert outcome is not HUNG
    assert seen == [f"m{i}" for i in range(5)]
    assert errors == [boom]
    assert errors[0] is boom


# -- reconnect and restart ------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_reconnect_spawns_a_new_adb_after_a_failed_exit(
    fake_adb: FakeAdb,
) -> None:
    """With auto_reconnect, a failed adb exit spawns a new adb whose lines arrive.

    The reconnect's logcat arguments are not pinned: F1-6 may add ``-T``.
    The join() outcome after a recovered failure is open (D-5).
    """
    fake_adb.logcat(
        run(out(threadtime("first")), exit_code=1),
        run(out(threadtime("second")), hang=True),
    )
    rec = Recorder()
    stream = _stream(rec, auto_reconnect=True, reconnect_delay=0.05)
    try:
        await bounded(stream.start())
        await async_wait_until(lambda: len(rec.entries) >= 2, timeout=JOIN_TIMEOUT)
        logcat_calls = fake_adb.calls("logcat")
    finally:
        await shutdown(stream)

    assert rec.messages == ["first", "second"]
    assert [call["index"] for call in logcat_calls] == [0, 1]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F2-4: start() never clears the previous run's errors",
)
@pytest.mark.asyncio
async def test_restart_after_a_failed_run_joins_cleanly_when_the_new_run_succeeds(
    fake_adb: FakeAdb,
) -> None:
    """After a failed run, a restarted run whose adb exits 0 lets join() return."""
    fake_adb.logcat(
        run(err(FAILED_LOGCAT_STDERR), exit_code=FAILED_LOGCAT_EXIT),
        run(out(threadtime("second run"))),
    )
    rec = Recorder()
    stream = _stream(rec)
    try:
        await bounded(stream.start())
        first = await join_outcome(stream)
        await bounded(stream.start())
        second = await join_outcome(stream)
    finally:
        await shutdown(stream)

    assert isinstance(first, LogStreamError)
    assert rec.messages == ["second run"]
    assert second is None


# -- context manager ------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaving_async_with_stops_the_stream(fake_adb: FakeAdb) -> None:
    """Leaving an ``async with`` block stops a running stream and its adb."""
    fake_adb.logcat(run(out(threadtime("inside")), hang=True))
    rec = Recorder()
    stream = _stream(rec)

    async def use_stream() -> tuple[StreamState, StreamState]:
        async with stream as handle:
            await rec.first_entry()
            inside = handle.state
        return inside, handle.state

    try:
        inside, after = await asyncio.wait_for(use_stream(), JOIN_TIMEOUT)
        survivors = await logcat_survivors(fake_adb)
    finally:
        await shutdown(stream)

    assert inside is StreamState.RUNNING
    assert after is StreamState.STOPPED
    assert rec.messages == ["inside"]
    assert survivors == []
