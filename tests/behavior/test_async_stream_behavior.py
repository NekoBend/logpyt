"""Behavior of AsyncLogStream against a real fake-adb child process.

These tests pin what a user of ``AsyncLogStream`` relies on, through the public
API only (constructor arguments, start/stop/kill/join, callbacks, events,
exceptions, ``async with``), so they must survive an internal rewrite.

They were written against the 2.x ``AsyncLogStream`` and are ported to the 3.0
API (design 8.3), as ``tests/behavior/test_sync_stream_behavior.py`` was:
``StreamConfig`` (D-1), one-argument ``on_item`` and ``on_event`` callbacks,
plain or async (design 2.3), instead of ``stdout_callback(entry, handle)``,
``on_error`` and the ``on_state`` hooks (design 2.4, D-2), the exceptions of
design 4.1, ``start()``, ``stop()`` and ``kill()`` as plain calls and
``join()`` awaited. Each test keeps the observable behavior it pinned; where
3.0 changes that behavior on purpose, the test asserts the new behavior and
its docstring names the design section. The 2.0.x defects these tests were
marked ``xfail`` for (X-1, F1-3, F4-1, F2-1, F2-10, F2-5, F3-1, F2-4 of
``.docs/review-findings-2.0.1.md``) are fixed by the 3.0 driver, so their
markers are gone (design 8.3). The fake adb prints the live wire format
(``logcat_line``, design 5.6).

Interleavings "during the spawn" use the harness spawn gate
(``FakeAdb.hold_spawn``), and "adb is gone" checks use the harness child
registry (``FakeAdb.async_survivors``), so both hold on every platform. Every
join is bounded (`_join`), so a hang fails its test instead of waiting for
the hang watchdog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from typing import TYPE_CHECKING

import pytest

from logpyt import (
    AdbExitError,
    AsyncLogStream,
    CallbackError,
    GroupBy,
    ReadTimeoutError,
    Reconnect,
    StateChange,
    StreamConfig,
    StreamError,
    UsageError,
)
from logpyt.filters import Condition
from logpyt.streams import StreamState
from tests.conftest import (
    FakeAdb,
    async_wait_until,
    err,
    lines,
    logcat_line,
    out,
    run,
    sleep,
    touch,
    wait,
)

if TYPE_CHECKING:
    from logpyt import LogEntry, LogGroup, StreamEvent
    from tests.conftest import SpawnedChild

S = StreamState

# Upper bound for every wait and join. A healthy run finishes far sooner; the
# bound only exists so a regression fails the test instead of hanging CI.
TIMEOUT = 10.0
# Bound for "the stream ends promptly" when, without the fix, it never ends.
PROMPT_END_TIMEOUT = 2.0

_POSIX_SIGNALS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGTERM cannot be ignored on Windows (terminate() is kill())",
)


class _Collector:
    """A plain ``on_item`` callback that records every entry it receives."""

    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    def __call__(self, entry: LogEntry) -> None:
        self.entries.append(entry)

    def messages(self) -> list[str]:
        return [entry.message for entry in list(self.entries)]

    async def received(self, message: str) -> None:
        """Wait (bounded) until ``message`` was delivered; fail the test if not."""
        try:
            await async_wait_until(lambda: message in self.messages(), timeout=TIMEOUT)
        except TimeoutError:
            pytest.fail(f"{message!r} was not delivered within {TIMEOUT} s")


async def _join(
    stream: AsyncLogStream[LogEntry] | AsyncLogStream[LogGroup],
    timeout: float = TIMEOUT,
) -> StreamError | None:
    """Await join() within ``timeout``; return the outcome it raised, or None.

    join() is awaited without its own timeout, so a hang is never mistaken
    for the builtin TimeoutError of an expired join (D-3).
    """
    try:
        async with asyncio.timeout(timeout):
            await stream.join()
    except TimeoutError:
        pytest.fail(f"join() did not finish within {timeout} s")
    except StreamError as exc:
        return exc
    return None


async def _teardown(
    stream: AsyncLogStream[LogEntry] | AsyncLogStream[LogGroup],
) -> None:
    """Kill the stream and wait for it, so no task or child outlives a test.

    Both are no-ops before start() (D-6), where join() raises UsageError.
    """
    stream.kill()
    with contextlib.suppress(StreamError, UsageError, TimeoutError):
        async with asyncio.timeout(TIMEOUT):
            await stream.join()


async def logcat_survivors(
    fake: FakeAdb, timeout: float = PROMPT_END_TIMEOUT
) -> list[SpawnedChild]:
    """Fake ``adb logcat`` children still alive after up to ``timeout``."""
    return await fake.async_survivors(timeout, command="logcat")


# -- delivery -------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_line_is_delivered_in_order_when_adb_exits_right_away(
    fake_adb: FakeAdb,
) -> None:
    """Every line adb wrote before exiting reaches on_item, in order, before
    the outcome (F1-1).

    Changed in 3.0: adb ending by itself, exit status 0 included, is the
    outcome of a run without reconnect (design 3.2), so join() raises
    AdbExitError(returncode=0) once the last line was delivered.
    """
    count = 2000
    fake_adb.logcat(run(lines(count, logcat_line("m{i}"))))
    collector = _Collector()
    stream = AsyncLogStream(on_item=collector)
    try:
        stream.start()
        error = await _join(stream)
    finally:
        await _teardown(stream)

    assert isinstance(error, AdbExitError)
    assert error.returncode == 0
    assert collector.messages() == [f"m{i}" for i in range(count)]


@pytest.mark.asyncio
async def test_bare_carriage_return_inside_a_message_stays_one_entry(
    fake_adb: FakeAdb,
) -> None:
    """A bare \\r inside a message does not split it into a second entry."""
    forged = logcat_line("forged", pid=6666, level="E", tag="Evil").rstrip("\n")
    fake_adb.logcat(
        run(
            out(logcat_line(f"before\r{forged}")),
            out(logcat_line("after", tag="Next")),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = AsyncLogStream(on_item=collector)
    try:
        stream.start()
        await collector.received("after")
    finally:
        await _teardown(stream)

    assert [(e.pid, e.tag, e.message) for e in collector.entries] == [
        (1234, "Tag", f"before\r{forged}"),
        (1234, "Next", "after"),
    ]


@pytest.mark.asyncio
async def test_stderr_lines_are_logged_not_delivered(
    fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture
) -> None:
    """adb's stderr lines never reach the item callback.

    3.0 has no ``stderr_callback``: adb stderr is never parsed or delivered as
    entries (D-9); each line is logged on ``logpyt.adb`` and counted in
    ``stats.stderr_lines`` (design 5.9), which is what this test checks.
    """
    caplog.set_level(logging.WARNING, logger="logpyt.adb")
    fake_adb.logcat(
        run(
            err("adb: warning from stderr\n"),
            out(logcat_line("from stdout")),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = AsyncLogStream(on_item=collector)
    try:
        stream.start()
        await async_wait_until(
            lambda: (
                stream.stats.stderr_lines == 1 and "from stdout" in collector.messages()
            ),
            timeout=TIMEOUT,
        )
    finally:
        await _teardown(stream)

    assert collector.messages() == ["from stdout"]
    logged = [r.getMessage() for r in caplog.records if r.name == "logpyt.adb"]
    assert logged == ["adb: warning from stderr"]


@pytest.mark.asyncio
async def test_burst_of_10000_lines_is_fully_delivered_with_defaults(
    fake_adb: FakeAdb,
) -> None:
    """A 10,000-line burst waiting at start is fully delivered to a trivial callback
    (X-1).

    ``adb logcat`` dumps its whole ring buffer on start, so this is default
    use. The spawn gate holds the spawn until adb has written the first
    part of the burst, so it is buffered when the stream reads its first
    line. 2.x buffered the whole burst meanwhile (an 8 MiB StreamReader
    limit); 3.0 bounds what it buffers (design 5.10), so adb is held back
    once asyncio's pipe buffer and the OS pipe are full, and the rest of the
    burst follows while the stream reads. 3.0 holds adb back instead of
    dropping (backpressure), and its readers yield to the loop after each
    read (design 5.4). adb stays alive, and the test stops the stream once
    every line arrived (3.0's normal end).
    """
    count, first = 10_000, 1_000  # the first part fits in the buffers
    fake_adb.logcat(
        run(
            lines(first, logcat_line("m{i}")),
            touch("burst-written"),
            lines(count - first, logcat_line(f"m{{i}}+{first}")),
            wait("done"),
        )
    )
    delivered: list[str] = []

    async def on_entry(entry: LogEntry) -> None:
        delivered.append(entry.message)

    stream = AsyncLogStream(on_item=on_entry)
    gate = fake_adb.hold_spawn(command="logcat")
    try:
        stream.start()
        await gate.async_wait_entered()
        await fake_adb.async_wait_for("burst-written")
        gate.release()
        with contextlib.suppress(TimeoutError):
            await async_wait_until(lambda: len(delivered) >= count, timeout=TIMEOUT)
        stream.stop()
        error = await _join(stream)
    finally:
        gate.release()
        await _teardown(stream)

    assert error is None
    assert len(delivered) == count
    expected = [f"m{i}" for i in range(first)]
    expected += [f"m{i}+{first}" for i in range(count - first)]
    assert delivered == expected


@pytest.mark.asyncio
async def test_all_accepted_entries_are_delivered_after_eof_with_a_slow_callback(
    fake_adb: FakeAdb,
) -> None:
    """Entries read before adb exits are all delivered, however slow the callback
    (F1-3): there is no drain timer.

    adb's exit with status 0 is the run's AdbExitError outcome (design 3.2),
    and the items of a FAILED run are still delivered (design A.21), so
    join() raises only after the last one.
    """
    count = 30
    fake_adb.logcat(run(lines(count, logcat_line("m{i}"))))
    delivered: list[str] = []

    async def slow_callback(entry: LogEntry) -> None:
        await asyncio.sleep(0.1)
        delivered.append(entry.message)

    stream = AsyncLogStream(on_item=slow_callback)
    try:
        stream.start()
        error = await _join(stream, timeout=TIMEOUT + count * 0.1)
    finally:
        await _teardown(stream)

    assert isinstance(error, AdbExitError)
    assert error.returncode == 0
    assert delivered == [f"m{i}" for i in range(count)]


@pytest.mark.asyncio
async def test_group_by_delivers_log_groups_to_the_callback(fake_adb: FakeAdb) -> None:
    """With group_by, on_item receives LogGroup objects (design 2.6).

    Two pids interleave, and a line one second later expires both.
    ``idle_flush_ms=None``: the groups close by the device timestamps alone
    (D-10), however slowly the lines arrive.
    """
    fake_adb.logcat(
        run(
            out(logcat_line("a1", pid=1, stamp="2026-11-04 03:04:05.000000 +0000")),
            out(logcat_line("b1", pid=2, stamp="2026-11-04 03:04:05.001000 +0000")),
            out(logcat_line("a2", pid=1, stamp="2026-11-04 03:04:05.002000 +0000")),
            out(logcat_line("late", pid=3, stamp="2026-11-04 03:04:06.000000 +0000")),
            wait("done"),
        )
    )
    groups: list[LogGroup] = []

    async def on_group(group: LogGroup) -> None:
        groups.append(group)

    stream = AsyncLogStream(
        group_by=GroupBy(
            keys=("pid",), gap_ms=100.0, mode="windowed", idle_flush_ms=None
        ),
        on_item=on_group,
    )
    try:
        stream.start()
        await async_wait_until(lambda: len(groups) >= 2, timeout=TIMEOUT)
        stream.stop()
        error = await _join(stream)
    finally:
        await _teardown(stream)

    assert error is None
    # Closed by "late" in expiry order; the stop flushes the open "late" group.
    assert [(g.key, [e.message for e in g], g.reason) for g in groups] == [
        ((2,), ["b1"], "gap"),
        ((1,), ["a1", "a2"], "gap"),
        ((3,), ["late"], "end"),
    ]


# -- read_timeout -----------------------------------------------------------


@pytest.mark.asyncio
async def test_read_timeout_does_not_end_a_stream_whose_stdout_is_active(
    fake_adb: FakeAdb,
) -> None:
    """A stream whose stdout keeps producing lines outlives read_timeout (F4-1).

    stderr stays silent, as it does for a healthy ``adb logcat``: 3.0 has one
    activity clock for both pipes (design 5.7). Lines come every 0.3 s, far
    inside read_timeout, and keep coming past it; the test then stops the
    stream, the normal end of a 3.0 run.
    """
    ticks = 7  # last tick at ~1.8 s, after read_timeout has elapsed
    steps = [out(logcat_line("tick 0"))]
    for i in range(1, ticks):
        steps += [sleep(0.3), out(logcat_line(f"tick {i}"))]
    fake_adb.logcat(run(*steps, wait("done")))
    collector = _Collector()
    stream = AsyncLogStream(StreamConfig(read_timeout=1.5), on_item=collector)
    try:
        stream.start()
        await collector.received(f"tick {ticks - 1}")
        stream.stop()
        error = await _join(stream)
    finally:
        await _teardown(stream)

    assert error is None
    assert collector.messages() == [f"tick {i}" for i in range(ticks)]


@pytest.mark.asyncio
async def test_read_timeout_ends_a_silent_stream_and_join_raises(
    fake_adb: FakeAdb,
) -> None:
    """When adb writes nothing for read_timeout, the stream ends and join() raises
    ReadTimeoutError, which is no builtin TimeoutError (D-3); the silent adb is
    gone.

    adb prints one line first: 3.0 arms ``read_timeout`` only once adb has
    printed (RUNNING, design 5.7).
    """
    fake_adb.logcat(run(out(logcat_line("only")), hang=True))
    stream = AsyncLogStream(StreamConfig(read_timeout=0.3))
    try:
        stream.start()
        error = await _join(stream)
        survivors = await logcat_survivors(fake_adb)
    finally:
        await _teardown(stream)

    assert isinstance(error, ReadTimeoutError)
    assert not isinstance(error, TimeoutError)
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
    """When adb exits with a non-zero status, join() raises AdbExitError (D-2),
    with adb's status and stderr; the run ends FAILED, not KILLED.
    """
    fake_adb.logcat(run(err(FAILED_LOGCAT_STDERR), exit_code=FAILED_LOGCAT_EXIT))
    stream = AsyncLogStream()
    try:
        stream.start()
        error = await _join(stream)
    finally:
        await _teardown(stream)

    assert isinstance(error, AdbExitError)
    assert error.returncode == FAILED_LOGCAT_EXIT
    assert error.stderr == FAILED_LOGCAT_STDERR.strip()
    assert stream.state is S.FAILED


@pytest.mark.asyncio
async def test_zero_adb_exit_is_the_outcome(fake_adb: FakeAdb) -> None:
    """adb exiting with status 0 ends the run; join() reports it.

    Changed in 3.0: every unrequested end of adb, exit status 0 included, is
    the outcome of a run without reconnect (design 3.2: a follow-mode logcat
    never ends by itself), so join() raises AdbExitError(returncode=0) and
    the run ends FAILED. ``stop()`` is the normal end (see the stop tests).
    """
    fake_adb.logcat(run(out(logcat_line("bye"))))
    collector = _Collector()
    stream = AsyncLogStream(on_item=collector)
    try:
        stream.start()
        error = await _join(stream)
    finally:
        await _teardown(stream)

    assert isinstance(error, AdbExitError)
    assert error.returncode == 0
    assert stream.state is S.FAILED
    assert collector.messages() == ["bye"]


# -- stop / kill --------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_ends_a_stream_whose_adb_never_exits(fake_adb: FakeAdb) -> None:
    """stop() ends a stream whose adb would run forever; join() then returns.

    The adb child is gone afterwards, and a requested stop is not an error.
    """
    fake_adb.logcat(run(out(logcat_line("alive")), hang=True))
    collector = _Collector()
    stream = AsyncLogStream(on_item=collector)
    try:
        stream.start()
        await collector.received("alive")
        stream.stop()
        error = await _join(stream)
        survivors = await logcat_survivors(fake_adb)
    finally:
        await _teardown(stream)

    assert error is None
    assert stream.state is S.STOPPED
    assert survivors == []


@pytest.mark.asyncio
async def test_kill_ends_killed_and_join_returns(fake_adb: FakeAdb) -> None:
    """kill() ends a running stream and its adb at once.

    Changed in 3.0: a kill is not an error (D-6). The stream ends KILLED and
    join() returns; there is no LogStreamKilledError (design 4.1).
    """
    fake_adb.logcat(run(out(logcat_line("alive")), hang=True))
    collector = _Collector()
    stream = AsyncLogStream(on_item=collector)
    try:
        stream.start()
        await collector.received("alive")
        stream.kill()
        error = await _join(stream)
        survivors = await logcat_survivors(fake_adb)
    finally:
        await _teardown(stream)

    assert error is None
    assert stream.state is S.KILLED
    assert "killed" in stream.stats.incomplete
    assert survivors == []


@pytest.mark.asyncio
async def test_kill_right_after_start_makes_join_raise_killed(
    fake_adb: FakeAdb,
) -> None:
    """kill() right after start() returns ends the run KILLED.

    start() only starts the engine, so this kill lands before any adb logcat
    is spawned; the clock read that comes first is killed with the run. A kill
    during the spawn is pinned by the next test. Changed in 3.0: KILLED is not
    an error, so join() returns (D-6; the F2-1 row of design 7).
    """
    fake_adb.logcat(run(out(logcat_line("alive")), hang=True))
    stream = AsyncLogStream()
    try:
        stream.start()
        stream.kill()
        error = await _join(stream)
        survivors = await fake_adb.async_survivors(PROMPT_END_TIMEOUT)
    finally:
        await _teardown(stream)

    assert error is None
    assert stream.state is S.KILLED
    assert survivors == []


@pytest.mark.asyncio
async def test_kill_while_adb_is_being_spawned_makes_join_raise_killed(
    fake_adb: FakeAdb,
) -> None:
    """kill() while adb is being spawned still ends the run KILLED (F2-1).

    The spawn gate holds the spawn after the adb child exists but before the
    library receives it, so kill() lands inside the spawn every time. The adb
    spawned meanwhile must not outlive the stream. Changed in 3.0: KILLED is
    not an error, so join() returns (D-6).
    """
    fake_adb.logcat(run(out(logcat_line("alive")), hang=True))
    stream = AsyncLogStream()
    gate = fake_adb.hold_spawn(command="logcat")
    try:
        stream.start()
        await gate.async_wait_entered()
        stream.kill()  # adb exists, but the library has not received it
        gate.release()
        error = await _join(stream)
        survivors = await logcat_survivors(fake_adb, TIMEOUT)
    finally:
        gate.release()
        await _teardown(stream)

    assert error is None
    assert stream.state is S.KILLED
    assert survivors == []


@_POSIX_SIGNALS
@pytest.mark.asyncio
async def test_stop_ends_an_adb_that_ignores_sigterm(fake_adb: FakeAdb) -> None:
    """stop() still ends an adb that ignores SIGTERM, within a bounded time.

    3.0 kills adb once ``stop_grace`` passed after the terminate, in both
    drivers (F2-10); it is short here so the test does not wait the 2 s
    default.
    """
    fake_adb.logcat(run(touch("sigterm-ignored"), hang=True, ignore_sigterm=True))
    stream = AsyncLogStream(StreamConfig(stop_grace=0.2))
    try:
        stream.start()
        await fake_adb.async_wait_for("sigterm-ignored")
        stream.stop()
        error = await _join(stream, timeout=PROMPT_END_TIMEOUT + 1.0)
        # Asserted before the teardown, whose kill() would end adb anyway.
        assert await logcat_survivors(fake_adb) == []
    finally:
        await _teardown(stream)

    assert error is None
    assert stream.state is S.STOPPED


# -- user code that raises ------------------------------------------------------


@pytest.mark.asyncio
async def test_raising_on_state_starting_hook_does_not_strand_the_stream(
    fake_adb: FakeAdb,
) -> None:
    """An on_event callback that raises on IDLE -> CONNECTING does not leave the
    stream stuck there (F2-5).

    Ported to ``on_event`` (design 7, F2-5): start() runs no user code, so it
    returns; the failure ends the run FAILED with CallbackError, without a
    stranded adb, and a clone (whose callback no longer raises) runs.
    """
    # The failed run ends before it spawns adb logcat, or kills the one it
    # spawned; every invocation prints the same line.
    fake_adb.logcat(run(out(logcat_line("ran")), wait("done")))
    raised: list[StreamState] = []

    async def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange) and event.new is S.CONNECTING and not raised:
            raised.append(event.new)
            msg = "hook bug"
            raise RuntimeError(msg)

    collector = _Collector()
    stream = AsyncLogStream(on_item=collector, on_event=on_event)
    twin = stream.clone()
    try:
        stream.start()
        first = await _join(stream)
        twin.start()
        await collector.received("ran")
        twin.stop()
        second = await _join(twin)
        survivors = await logcat_survivors(fake_adb)
    finally:
        await _teardown(stream)
        await _teardown(twin)

    assert raised == [S.CONNECTING]
    assert isinstance(first, CallbackError)
    assert first.stage == "on_event"
    assert stream.state is S.FAILED
    assert second is None
    assert twin.state is S.STOPPED
    assert collector.messages() == ["ran"]
    assert survivors == []


class _ExplodingCondition(Condition):
    """A user condition that fails on one particular entry."""

    def __call__(self, entry: LogEntry, /) -> bool:
        if entry.message == "m1":
            msg = "filter bug"
            raise ValueError(msg)
        return True


@pytest.mark.asyncio
async def test_raising_filter_ends_the_stream(fake_adb: FakeAdb) -> None:
    """A ``where`` condition that raises ends the stream and its adb instead of
    hanging (F3-1): join() raises CallbackError(stage="where"), and the items
    accepted before it are delivered.
    """
    fake_adb.logcat(run(lines(3, logcat_line("m{i}")), hang=True))
    collector = _Collector()
    stream = AsyncLogStream(where=_ExplodingCondition(), on_item=collector)
    try:
        stream.start()
        error = await _join(stream, timeout=PROMPT_END_TIMEOUT)
        # Asserted before the teardown, whose kill() would end adb anyway.
        assert await logcat_survivors(fake_adb) == []
    finally:
        await _teardown(stream)

    assert isinstance(error, CallbackError)
    assert error.stage == "where"
    assert isinstance(error.__cause__, ValueError)
    assert stream.state is S.FAILED
    assert collector.messages() == ["m0"]


@pytest.mark.asyncio
async def test_raising_data_callback_ends_the_run_with_callback_error(
    fake_adb: FakeAdb,
) -> None:
    """A raising on_item ends the run: join() raises CallbackError(stage="on_item")
    chaining the callback's exception, no later entry reaches the callback, and
    adb is gone.

    Changed in 3.0: there is no ``on_error``, and a raising callback does not
    let the stream continue. User code fails fast (design 2.4, D-2); a
    monitor that wants to continue wraps its own callback. adb keeps running
    after its lines here, so the callback's failure, not adb's exit, is the
    outcome.
    """
    fake_adb.logcat(run(lines(5, logcat_line("m{i}")), wait("done")))
    seen: list[str] = []
    boom = ValueError("callback bug")

    async def on_entry(entry: LogEntry) -> None:
        seen.append(entry.message)
        if entry.message == "m1":
            raise boom

    stream = AsyncLogStream(on_item=on_entry)
    try:
        stream.start()
        error = await _join(stream)
        survivors = await logcat_survivors(fake_adb)
    finally:
        await _teardown(stream)

    assert isinstance(error, CallbackError)
    assert error.stage == "on_item"
    assert error.__cause__ is boom
    assert seen == ["m0", "m1"]
    assert stream.state is S.FAILED
    assert survivors == []


# -- reconnect and restart ------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_reconnect_spawns_a_new_adb_after_a_failed_exit(
    fake_adb: FakeAdb,
) -> None:
    """With reconnect, a failed adb exit spawns a new adb whose lines arrive (F4-2).

    2.x ``auto_reconnect=True, reconnect_delay=0.05`` is
    ``StreamConfig(reconnect=Reconnect(delay=0.05))`` (D-1).
    """
    fake_adb.logcat(
        run(out(logcat_line("first")), exit_code=1),
        run(out(logcat_line("second")), hang=True),
    )
    collector = _Collector()
    stream = AsyncLogStream(
        StreamConfig(reconnect=Reconnect(delay=0.05)), on_item=collector
    )
    try:
        stream.start()
        await collector.received("second")
        logcat_calls = fake_adb.calls("logcat")
    finally:
        await _teardown(stream)

    assert collector.messages() == ["first", "second"]
    assert [call["index"] for call in logcat_calls] == [0, 1]


@pytest.mark.asyncio
async def test_restart_after_a_failed_run_joins_cleanly_when_the_new_run_succeeds(
    fake_adb: FakeAdb,
) -> None:
    """After a run that failed, a fresh run of the same stream joins without error.

    3.0 streams run once (design 3.3): start() on a FAILED stream raises
    UsageError, and ``clone()`` is the restart, a fresh stream with the same
    config and callbacks (F2-4). The restarted run is stopped, the normal end
    of a 3.0 run.
    """
    fake_adb.logcat(
        run(err(FAILED_LOGCAT_STDERR), exit_code=FAILED_LOGCAT_EXIT),
        run(out(logcat_line("second run")), wait("done")),
    )
    collector = _Collector()
    stream = AsyncLogStream(on_item=collector)
    twin = stream.clone()
    try:
        stream.start()
        first = await _join(stream)
        with pytest.raises(UsageError, match="runs once"):
            stream.start()
        twin.start()
        await collector.received("second run")
        twin.stop()
        second = await _join(twin)
    finally:
        await _teardown(stream)
        await _teardown(twin)

    assert isinstance(first, AdbExitError)
    assert [call["index"] for call in fake_adb.calls("logcat")] == [0, 1]
    assert collector.messages() == ["second run"]
    assert second is None
    assert (stream.state, twin.state) == (S.FAILED, S.STOPPED)


# -- context manager ------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaving_async_with_stops_the_stream(fake_adb: FakeAdb) -> None:
    """Leaving an ``async with`` block stops a running stream and its adb."""
    fake_adb.logcat(run(out(logcat_line("inside")), hang=True))
    collector = _Collector()
    stream = AsyncLogStream(on_item=collector)

    async def use_stream() -> tuple[StreamState, StreamState]:
        async with stream as entered:
            assert entered is stream
            await collector.received("inside")
            inside = entered.state
        return inside, stream.state

    try:
        async with asyncio.timeout(TIMEOUT):
            inside, after = await use_stream()
        survivors = await logcat_survivors(fake_adb)
    finally:
        await _teardown(stream)

    assert inside is S.RUNNING
    assert after is S.STOPPED
    assert collector.messages() == ["inside"]
    assert survivors == []
