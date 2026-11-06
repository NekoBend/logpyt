"""Async-only conformance: what only the asyncio driver can get wrong (design 5.4).

`AsyncLogStream` runs its engine, readers, exit watchers, one-shot commands
and dispatcher as tasks on the loop that started it. These tests drive it
from asyncio directly instead of through the ``driver`` fixture, because
they cancel its awaits, run it under more than one ``asyncio.run`` and watch
what another task of the same loop sees:

- F2-9: a clone runs under a second ``asyncio.run``; nothing loop-bound is
  made before ``start()``.
- F3-2: the engine cancelled, as a loop shutdown does, kills adb at once and
  starts nothing more; ``tests/test_async_shutdown.py`` ends ``asyncio.run``
  with a stream running, and the interpreter with a loop in a thread, in a
  child interpreter. Cancelled once the run finished, it keeps the outcome
  (G4). A stream dropped while its loop is stopped is killed (design 3.4).
- Ctrl+C and ``sys.exit()``: a KeyboardInterrupt or SystemExit raised in a
  task of the run, the push callbacks and ``where`` included, leaves the loop
  at once, kills adb and ends the run with it; one raised by on_event in pull
  mode propagates from ``__anext__()``. One in the engine still gets adb
  reaped under ``asyncio.run``, and a crash it cut short keeps it as the
  end of the run. One in the pipe readers of a one-shot command (adb's own
  code) is not covered here.
- F3-3: a task cancelled while it waits for a stopped run gets its
  CancelledError, and the run's one-shot adb commands are still reaped. A
  cancellation of the with-block exit's own wait kills, and keeps the body's
  exception as its ``__context__``.
- F3-7: a task created inside a callback inherits its mark, so its waits
  raise UsageError while the run lives, and return once it has ended; the
  callback's own waits raise after the end too.
- F2-7: kill() from an async on_item, on the loop or from a thread, stops
  delivery before it returns; a kill() from a thread racing a pop. stop()
  from on_item on the loop.
- F3-6: a callback's own CancelledError, raised while its task is not being
  cancelled, is a callback failure.
- A.21: a cancelled ``__anext__()`` cancels only its wait; the record in hand
  is settled by the next step, whether ``on_event`` or the once-per-batch
  checkpoint was interrupted.
- X-1: another task keeps getting turns while the stream takes a burst.
- X-5: the end closes a pipe a grandchild holds, and leaves no task.
- design 4.2: ``start()`` needs a running loop, and on Windows the proactor
  loop; waits and iteration on another loop or thread, and two tasks in one
  iterator, raise UsageError.

Until PR-14 a reconnect replays from the first position (design A.20), so
`config` turns the stall resume off.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import gc
import itertools
import logging
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from logpyt import (
    AdbExitError,
    AsyncLogStream,
    CallbackError,
    IncompleteCaptureError,
    LogEntry,
    Package,
    Reconnect,
    StateChange,
    Tag,
    UsageError,
    Where,
    adb,
)
from logpyt._core.machine import StreamCore
from logpyt._core.mailbox import Outbox
from logpyt._core.protocol import Data, Exited
from logpyt.streams import StreamState, async_stream
from tests.conformance.conftest import TIMEOUT, config
from tests.conftest import (
    FakeAdb,
    async_wait_until,
    hold_stdout,
    lines,
    logcat_line,
    out,
    run,
    touch,
    wait,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from logpyt import StreamEvent

S = StreamState

# How long a child that should be gone may take to end.
PROMPT = 2.0
# Design 5.4: one pipe read of the async driver.
READ_SIZE = 65536
# X-1: the longest turn another task may wait for while the stream takes a
# burst. Runs measured about 10 ms (Linux, Python 3.14, also on one CPU); a
# stream that never yielded would hold the loop for the whole burst.
LATENCY_BOUND = 0.1
PACKAGE = "com.example.app"


async def _bounded[R](
    awaitable: Awaitable[R], what: str, timeout: float = TIMEOUT
) -> R:
    """Await ``awaitable`` in this task; fail the test if it takes over ``timeout``.

    The call's own TimeoutError (an expired ``join(timeout)``) is re-raised.
    """
    scope = asyncio.timeout(timeout)
    try:
        async with scope:
            return await awaitable
    except TimeoutError:
        if scope.expired():
            pytest.fail(f"{what} did not finish within {timeout} s")
        raise


async def _end(stream: AsyncLogStream[LogEntry]) -> None:
    """Kill the stream and wait for its end, so no task or child outlives the test.

    Whatever the wait raises (an outcome, UsageError before start(), the
    CancelledError of a cancelled engine) is the test's business elsewhere.
    """
    stream.kill()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(stream.join(), TIMEOUT)


def _messages(entries: list[LogEntry]) -> list[str]:
    return [entry.message for entry in list(entries)]


async def _received(entries: list[LogEntry], message: str) -> None:
    await _bounded(
        async_wait_until(lambda: message in _messages(entries), timeout=TIMEOUT),
        f"the delivery of {message!r}",
    )


async def _next(iterator: AsyncIterator[LogEntry]) -> LogEntry:
    return await anext(iterator)


async def _rest(
    iterator: AsyncIterator[LogEntry], into: list[str] | None = None
) -> list[str]:
    """The messages of the rest of the iteration, appended to ``into`` as they come."""
    messages = [] if into is None else into
    async for entry in iterator:
        messages.append(entry.message)
    return messages


def _run_tasks() -> list[asyncio.Task[object]]:
    """The tasks of a stream run on this loop (they are named ``logpyt-...``)."""
    return [
        task for task in asyncio.all_tasks() if task.get_name().startswith("logpyt-")
    ]


def _task(name: str) -> asyncio.Task[object]:
    """The task of the stream's run named ``name`` (``logpyt-engine``, ...)."""
    (task,) = [task for task in _run_tasks() if task.get_name() == name]
    return task


async def _join_error(stream: AsyncLogStream[LogEntry]) -> BaseException | None:
    """What ``join()`` raises, as a value (a CancelledError too); None if it returns.

    A cancellation of this task itself propagates.
    """
    try:
        await _bounded(stream.join(), "join()")
    except BaseException as exc:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        return exc
    return None


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel what is left on ``loop``, as ``asyncio.run`` does, then close it."""
    tasks = asyncio.all_tasks(loop)
    for task in tasks:
        task.cancel()
    if tasks:
        loop.run_until_complete(asyncio.wait(tasks, timeout=TIMEOUT))
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()


def _interrupt(kind: str) -> KeyboardInterrupt | SystemExit:
    """A new interrupt of ``kind``: ``ctrl_c`` (KeyboardInterrupt) or ``exit``
    (the SystemExit of ``sys.exit(3)``, as a SIGTERM handler raises it).
    """
    return KeyboardInterrupt() if kind == "ctrl_c" else SystemExit(3)


# The two interrupts that leave the loop (design A.24): Ctrl+C and sys.exit().
INTERRUPTS = ("ctrl_c", "exit")


def _interrupt_of(call: Callable[[], object]) -> BaseException | None:
    """The KeyboardInterrupt or SystemExit ``call()`` raised, or None.

    Its traceback is dropped: the frames of the loop it passed through would
    keep the task it came from alive, and with it asyncio's report of an
    exception nobody retrieved, which the test looks for.
    """
    try:
        call()
    except (KeyboardInterrupt, SystemExit) as exc:
        exc.__traceback__ = None
        return exc
    return None


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The messages of the records logged at ERROR or above, by any logger."""
    return [
        f"{record.name}: {record.getMessage()}"
        for record in caplog.records
        if record.levelno >= logging.ERROR
    ]


# -- F2-9: a second asyncio.run ---------------------------------------------------


def test_clone_runs_under_a_second_asyncio_run(fake_adb: FakeAdb) -> None:
    """A stream that ran under one ``asyncio.run`` has a clone that runs under
    the next one (F2-9).

    2.x bound its asyncio primitives to the first loop in ``__init__``, so a
    restart under another loop failed and its stop() then leaked adb. 3.0
    makes them in ``start()`` (design 3.3, 5.4): the clone, made on the first
    loop once that run ended, runs on the second. There the ended stream's
    join() returns, a second start() raises UsageError, and stop() and kill()
    do nothing (its loop is closed); no adb is left.
    """
    fake_adb.logcat(
        run(out(logcat_line("first")), wait("never")),
        run(out(logcat_line("second")), wait("never")),
    )
    entries: list[LogEntry] = []
    stream = AsyncLogStream(config(), on_item=entries.append)  # no loop runs yet

    async def first_run() -> AsyncLogStream[LogEntry]:
        async with stream:
            await _received(entries, "first")
        return stream.clone()

    twin = asyncio.run(_bounded(first_run(), "the first run"))

    async def second_run() -> None:
        stream.stop()
        stream.kill()
        with pytest.raises(UsageError, match="runs once"):
            stream.start()
        await _bounded(stream.join(), "join() of the ended run")
        async with twin:
            await _received(entries, "second")

    asyncio.run(_bounded(second_run(), "the second run"))

    assert _messages(entries) == ["first", "second"]
    assert (stream.state, twin.state) == (S.STOPPED, S.STOPPED)
    assert [call["index"] for call in fake_adb.calls("logcat")] == [0, 1]
    assert fake_adb.survivors() == []


# -- F3-2: the engine cancelled ---------------------------------------------------


@pytest.mark.parametrize("phase", ["running", "backoff"])
@pytest.mark.asyncio
async def test_cancel_engine_reaps_adb_and_does_not_respawn(
    fake_adb: FakeAdb, phase: str
) -> None:
    """Cancelling the engine task, as a loop shutdown does, kills adb at once
    and starts nothing more, although reconnect is configured (F3-2, design
    5.4).

    running: adb runs; backoff: adb failed and the run waits to reconnect.
    2.x skipped its teardown when its connection task was cancelled in
    ``process.wait()`` and respawned adb. Afterwards no task of the run is
    left, so nothing can respawn, and every wait ends: join() raises the
    CancelledError that ended the engine, and the capture is not complete
    (the crash rule of design A.23, 4.4).
    """
    if phase == "running":
        first, delay = run(out(logcat_line("a")), wait("never")), 0.05
    else:
        first, delay = run(out(logcat_line("a")), exit_code=1), TIMEOUT
    fake_adb.logcat(first, run(out(logcat_line("again")), wait("never")))
    entries: list[LogEntry] = []
    stream = AsyncLogStream(
        config(reconnect=Reconnect(delay=delay, max_delay=delay)),
        on_item=entries.append,
    )
    stream.start()
    try:
        await _received(entries, "a")
        if phase == "backoff":
            await async_wait_until(
                lambda: stream.state is S.RECONNECTING, timeout=TIMEOUT
            )
        (engine,) = [
            task for task in _run_tasks() if task.get_name() == "logpyt-engine"
        ]
        engine.cancel()
        await asyncio.wait({engine}, timeout=TIMEOUT)
        await async_wait_until(lambda: not _run_tasks(), timeout=TIMEOUT)
        survivors = await fake_adb.async_survivors(PROMPT)
        with pytest.raises(asyncio.CancelledError):
            await _bounded(stream.join(), "join()")
    finally:
        await _end(stream)

    assert engine.cancelled()
    assert survivors == []
    assert len(fake_adb.calls("logcat")) == 1
    assert _messages(entries) == ["a"]
    assert not stream.stats.complete


@pytest.mark.parametrize("first", ["engine", "consumer"])
@pytest.mark.parametrize("end", ["killed", "failed"])
@pytest.mark.asyncio
async def test_cancel_after_finished_keeps_outcome(
    fake_adb: FakeAdb, end: str, first: str
) -> None:
    """The engine cancelled once the run finished, while the consumer is still
    in its callback (a loop shutdown cancels both), keeps what the run ended
    with: its state, outcome and stats are final at ``Finished`` (G4, design
    A.21). A KILLED run's join() returns and assert_complete() names the
    kill; a FAILED run's join() and assert_complete() raise its outcome
    (design 4.2). The crash rule (A.23) is for a run that had not finished:
    it made them raise the shutdown's CancelledError, and counted a killed
    capture as failed.

    first: which of the two tasks is cancelled first. The dispatcher's detach,
    which it reports as it is cancelled, is counted either way.
    """
    count = 5
    tail = {"exit_code": 1} if end == "failed" else {}
    steps = () if end == "failed" else (wait("never"),)
    fake_adb.logcat(run(lines(count, logcat_line("m{i}")), *steps, **tail))
    busy = asyncio.Event()
    calls: list[str] = []

    async def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        busy.set()
        await asyncio.Event().wait()  # until the dispatcher is cancelled

    stream = AsyncLogStream(config(), on_item=on_item)
    stream.start()
    try:
        await _bounded(busy.wait(), "the first on_item")
        await async_wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
        if end == "killed":
            stream.kill()
        await async_wait_until(lambda: stream._run.finished, timeout=TIMEOUT)
        before = (stream.state, stream.outcome, stream.stats.failed)
        engine, dispatcher = _task("logpyt-engine"), _task("logpyt-dispatcher")
        for task in (engine, dispatcher) if first == "engine" else (dispatcher, engine):
            task.cancel()
        await asyncio.wait({engine, dispatcher}, timeout=TIMEOUT)
        error = await _join_error(stream)
        survivors = await fake_adb.async_survivors(PROMPT)
    finally:
        await _end(stream)

    assert engine.cancelled()
    assert (stream.state, stream.outcome, stream.stats.failed) == before
    stats = stream.stats
    assert calls == ["m0"]
    assert stats.delivered + stats.undelivered == count
    if end == "killed":
        assert stream.state is S.KILLED
        assert error is None
        with pytest.raises(IncompleteCaptureError) as info:
            stream.assert_complete()
        assert "killed" in info.value.reasons
    else:
        assert stream.state is S.FAILED
        assert isinstance(error, AdbExitError)
        assert error is stream.outcome
        with pytest.raises(AdbExitError) as raised:
            stream.assert_complete()
        assert raised.value is error
    assert survivors == []


@pytest.mark.asyncio
async def test_cancel_in_reap_keeps_outcome(
    fake_adb: FakeAdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine cancelled while it reaps the one-shot commands of a run that
    finished STOPPED (a loop shutdown right after the end) keeps the outcome
    and the stats (G4, design 4.4): join() returns and the capture stays
    complete. It counted the capture as crashed: failed, not complete.

    The uid lookup's adb never answers, so it still runs at the end; the
    engine is held where it waits for that reap, and cancelled there.
    """
    fake_adb.dumpsys(PACKAGE, run(touch("lookup"), wait("never")))
    fake_adb.logcat(run(out(logcat_line("a", tag="Mine")), wait("never")))
    reaping = asyncio.Event()

    async def reap(run: object) -> None:
        del run
        reaping.set()
        await asyncio.Event().wait()  # until the engine is cancelled

    monkeypatch.setattr(async_stream._Run, "_reap_commands", reap)
    entries: list[LogEntry] = []
    stream = AsyncLogStream(
        config(package_hold=0.1),
        where=Package(PACKAGE) | Tag("Mine"),
        on_item=entries.append,
    )
    stream.start()
    try:
        await _bounded(fake_adb.async_wait_for("lookup"), "the uid lookup")
        await _received(entries, "a")
        stream.stop()
        await _bounded(reaping.wait(), "the reap of the lookup")
        before = stream.stats
        engine = _task("logpyt-engine")
        engine.cancel()
        await asyncio.wait({engine}, timeout=TIMEOUT)
        error = await _join_error(stream)
        survivors = await fake_adb.async_survivors(PROMPT)
    finally:
        await _end(stream)

    assert engine.cancelled()
    assert error is None
    assert stream.state is S.STOPPED
    assert before.complete, before.incomplete
    assert stream.stats == before
    stream.assert_complete()
    assert survivors == []


# -- design 3.4: a stream dropped while its loop is stopped ----------------------


@pytest.mark.parametrize("policy", ["reconnect", "none"])
def test_drop_while_loop_stopped_kills(
    fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture, policy: str
) -> None:
    """A RUNNING stream dropped while its loop is stopped, between two
    ``Runner.run()`` calls (as a program does that calls
    ``run_until_complete`` more than once on one loop), is killed (design
    3.4), as a dropped `LogStream` is.

    Its finalizer kills adb at once and posts the kill, which the loop runs
    first when it runs again, before it reads the end of that adb. So the run
    ends KILLED: nothing is respawned although reconnect is configured
    (F3-2), on_item gets nothing more, and no ERROR blames the run for an
    adb exit the library caused. It ended FAILED, 'garbage-collected ...
    without anyone seeing it', or reconnected and went on delivering for a
    stream nobody held.
    """
    caplog.set_level(logging.WARNING)
    count = 3
    fake_adb.logcat(
        run(lines(count, logcat_line("m{i}")), wait("never")),
        run(out(logcat_line("again")), wait("never")),
    )
    reconnect = Reconnect(delay=0.05, max_delay=0.05) if policy == "reconnect" else None
    entries: list[LogEntry] = []
    held = [AsyncLogStream(config(reconnect=reconnect), on_item=entries.append)]
    dropped = held[0]._run
    with asyncio.Runner() as runner:

        async def first() -> None:
            held[0].start()
            await async_wait_until(lambda: len(entries) == count, timeout=TIMEOUT)

        runner.run(_bounded(first(), "the first run"))
        held.clear()
        gc.collect()  # the finalizer runs while the loop is stopped

        async def second() -> StreamState:
            with contextlib.suppress(TimeoutError):
                await async_wait_until(lambda: dropped.ended, timeout=TIMEOUT)
            state = dropped.state
            dropped.kill()  # a run that went on is ended for the cleanup
            await async_wait_until(lambda: dropped.ended, timeout=TIMEOUT)
            return state

        state = runner.run(_bounded(second(), "the second run", 3 * TIMEOUT))

    assert state is S.KILLED
    assert len(fake_adb.calls("logcat")) == 1
    assert _messages(entries) == [f"m{i}" for i in range(count)]
    assert fake_adb.survivors(PROMPT) == []
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned == []


# -- F3-3: a cancellation through the stop --------------------------------------


@pytest.mark.parametrize("waits_in", ["exit", "join"])
@pytest.mark.asyncio
async def test_cancelled_error_propagates_through_stop(
    fake_adb: FakeAdb, waits_in: str
) -> None:
    """A task cancelled while it waits for a stopped run gets its CancelledError
    (F3-3, design 5.4), and the run's one-shot adb commands, which the engine
    owns, are still reaped. 2.x's AsyncPidMonitor.stop() swallowed it.

    The uid lookup's adb never answers and on_item is stuck, so the stopped
    run waits for its consumer (the drain, A.21) while the lookup runs.
    exit: the task waits in the with-block exit; its cancellation kills the
    run before it propagates (design A.23), which also ends the lookup's adb.
    join: the task awaits join() after stop(); the cancellation ends only
    that wait, and the run ends STOPPED once on_item returns.
    """
    fake_adb.dumpsys(PACKAGE, run(touch("lookup"), wait("never")))
    fake_adb.logcat(run(out(logcat_line("a", tag="Mine")), wait("never")))
    busy = asyncio.Event()
    release = asyncio.Event()
    taken: list[str] = []

    async def on_item(entry: LogEntry) -> None:
        taken.append(entry.message)
        busy.set()
        await release.wait()

    # Tag("Mine") decides each line without its package: nothing waits for
    # the lookup once package_hold opened intake.
    stream = AsyncLogStream(
        config(package_hold=0.1), where=Package(PACKAGE) | Tag("Mine"), on_item=on_item
    )

    async def waiter() -> None:
        if waits_in == "exit":
            async with stream:
                await busy.wait()
            return
        stream.start()
        await busy.wait()
        stream.stop()
        await stream.join()

    task = asyncio.create_task(waiter())
    try:
        await _bounded(fake_adb.async_wait_for("lookup"), "the uid lookup")
        await _bounded(busy.wait(), "the first on_item")
        await async_wait_until(lambda: stream.state is S.STOPPING, timeout=TIMEOUT)
        lookup_ran = bool(fake_adb.live_children("dumpsys"))
        task.cancel()
        await asyncio.wait({task}, timeout=TIMEOUT)
        cancelled = task.cancelled()
        state_after = stream.state
        if waits_in == "exit":  # killed: the engine reaps the lookup's adb
            await fake_adb.async_survivors(PROMPT, command="dumpsys")
        lookup_after = bool(fake_adb.live_children("dumpsys"))
        release.set()
        await _bounded(stream.join(), "join()")
        survivors = await fake_adb.async_survivors(PROMPT)
    finally:
        release.set()
        task.cancel()
        await _end(stream)

    assert lookup_ran, "the lookup's adb was not running when the task was cancelled"
    assert cancelled, "the library swallowed the task's cancellation"
    if waits_in == "exit":
        assert (state_after, lookup_after) == (S.KILLED, False)
        assert stream.state is S.KILLED
    else:
        assert (state_after, lookup_after) == (S.STOPPING, True)
        assert stream.state is S.STOPPED
    assert taken == ["a"]
    assert survivors == []


async def _in_except_clause(
    work: Callable[[], Awaitable[None]], handled: BaseException | None
) -> None:
    """Await ``work()``; inside an ``except`` clause that handles ``handled``,
    when it is given.
    """
    if handled is None:
        await work()
        return
    try:
        raise handled
    except type(handled):
        await work()


@pytest.mark.parametrize("body", ["raised", "clean"])
@pytest.mark.asyncio
async def test_cancel_in_exit_kills(fake_adb: FakeAdb, body: str) -> None:
    """A task cancelled while its with-block exit waits kills the run before
    the cancellation propagates (design 3.4, A.23): the state is KILLED right
    after, the stuck on_item is the last call (no item is taken after the
    block), and the capture names the kill. on_item is stuck, so the exit
    waits: up to close_timeout after the body raised an Exception (raised),
    or for the end of a stopped run (clean).

    The CancelledError keeps as its ``__context__`` the exception the block
    was handling, as with Ctrl+C in the thread driver's exit: the body's
    exception (raised), or the one the ``except`` clause around a clean block
    handles. asyncio throws the cancellation into the exit's wait, and the
    exit awaits its kill before it re-raises, so Python does not chain it.
    """
    count = 50
    fake_adb.logcat(run(lines(count, logcat_line("m{i}")), wait("never")))
    busy = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []
    escaped: list[BaseException] = []

    async def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        busy.set()
        await release.wait()

    stream = AsyncLogStream(config(close_timeout=TIMEOUT), on_item=on_item)
    error = ValueError("the body failed")
    handled = LookupError("around the block") if body == "clean" else None

    async def block() -> None:
        try:
            async with stream:
                await busy.wait()
                await async_wait_until(
                    lambda: stream.stats.lines == count, timeout=TIMEOUT
                )
                if body == "raised":
                    raise error
        except asyncio.CancelledError as exc:
            escaped.append(exc)
            raise

    task = asyncio.create_task(_in_except_clause(block, handled))
    try:
        await _bounded(busy.wait(), "the first on_item")
        await async_wait_until(lambda: stream.state is S.STOPPING, timeout=TIMEOUT)
        task.cancel()
        await asyncio.wait({task}, timeout=TIMEOUT)
        cancelled = task.cancelled()
        state_after = stream.state
        release.set()
        await _bounded(stream.join(), "join()")
        survivors = await fake_adb.async_survivors(PROMPT)
    finally:
        release.set()
        task.cancel()
        await _end(stream)

    assert cancelled, "the library swallowed the task's cancellation"
    assert state_after is S.KILLED
    assert calls == ["m0"]
    assert "killed" in stream.stats.incomplete
    assert survivors == []
    assert len(escaped) == 1
    assert escaped[0].__context__ is (handled or error)


# -- F2-7: kill() from on_item ----------------------------------------------------


@pytest.mark.parametrize("caller", ["loop", "thread"])
@pytest.mark.asyncio
async def test_kill_stops_delivery_at_once(fake_adb: FakeAdb, caller: str) -> None:
    """kill() from an async on_item stops delivery before it returns (F2-7,
    design 3.4): the dispatcher takes no item after it, and the queued lines
    are discarded and counted; the run ends KILLED.

    on_item waits until every line was read, then calls kill() on the loop,
    or in a helper thread that it waits for without yielding to the loop (as
    a callback whose worker thread finished before the loop looked), so the
    dispatcher's next step comes before the kill request reached the engine.
    """
    count = 50
    fake_adb.logcat(run(lines(count, logcat_line("m{i}")), wait("never")))
    calls: list[str] = []

    async def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        await async_wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
        if caller == "loop":
            stream.kill()
            return
        killer = threading.Thread(target=stream.kill, name="test: killer")
        killer.start()
        killer.join(TIMEOUT)  # no await: the loop runs nothing meanwhile

    stream = AsyncLogStream(config(), on_item=on_item)
    stream.start()
    try:
        await _bounded(stream.join(), "join()")
    finally:
        await _end(stream)

    assert calls == ["m0"]
    assert stream.state is S.KILLED
    stats = stream.stats
    assert (stats.delivered, stats.undelivered) == (1, count - 1)
    assert fake_adb.survivors() == []


@pytest.mark.asyncio
async def test_thread_kill_racing_a_pop(
    fake_adb: FakeAdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kill() from another thread while the consumer's step has looked at the
    kill flag but not popped yet: no item is popped once kill() returned
    (F2-7, design 3.4), as with the thread driver, whose kill() and step
    share a lock. The step pops at most the item it was popping when kill()
    set the flag (the events after it still come); the run ends KILLED and
    every line is counted.

    The pop is held while the killer thread runs (up to 0.2 s, without
    yielding to the loop), so a kill() that does not wait for that step
    returns before the pop.
    """
    count = 20
    fake_adb.logcat(run(lines(count, logcat_line("m{i}")), wait("never")))
    returned = threading.Event()
    armed = threading.Event()
    late: list[str] = []  # the items popped by a pop begun after kill() returned
    killers: list[threading.Thread] = []
    real_pop = Outbox.pop

    def kill() -> None:
        stream.kill()
        returned.set()

    def pop(outbox: Outbox) -> object:
        if armed.is_set() and not killers:
            killer = threading.Thread(target=kill, name="test: killer")
            killers.append(killer)
            killer.start()
            killer.join(0.2)  # no await: the loop runs nothing meanwhile
        began_late = returned.is_set()
        record = real_pop(outbox)
        if began_late and isinstance(record, LogEntry):
            late.append(record.message)
        return record

    monkeypatch.setattr(Outbox, "pop", pop)
    calls: list[str] = []

    async def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        if len(calls) == 1:
            await async_wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
            armed.set()  # the next step's pop races a kill()

    stream = AsyncLogStream(config(), on_item=on_item)
    stream.start()
    try:
        await _bounded(stream.join(), "join()")
    finally:
        await _end(stream)
        for killer in killers:
            killer.join(TIMEOUT)

    assert killers, "no pop raced the kill()"
    assert late == []
    # m1 was being popped when kill() set the flag. A later item can only
    # come from a pop that began before kill() returned (a lock without
    # handoff, as on Python 3.12, may let the loop take it again first).
    assert calls[:2] == ["m0", "m1"]
    assert stream.state is S.KILLED
    stats = stream.stats
    assert (stats.delivered, stats.undelivered) == (len(calls), count - len(calls))


@pytest.mark.parametrize("kind", ["plain", "async"])
@pytest.mark.asyncio
async def test_stop_in_on_item_on_the_loop(fake_adb: FakeAdb, kind: str) -> None:
    """stream.stop() called by on_item on the loop, as a plain or an async
    function, ends the run STOPPED (design 2.4: a callback reaches its
    stream by closure, and ``stream.stop()`` is safe there): join() returns,
    and every line read before the stop is delivered, in order.
    """
    count = 5
    fake_adb.logcat(run(lines(count, logcat_line("m{i}")), wait("never")))
    calls: list[str] = []

    def plain(entry: LogEntry) -> None:
        calls.append(entry.message)
        if entry.message == "m2":
            stream.stop()

    async def awaited(entry: LogEntry) -> None:
        await asyncio.sleep(0)
        plain(entry)

    stream = AsyncLogStream(config(), on_item=plain if kind == "plain" else awaited)
    stream.start()
    try:
        await _bounded(stream.join(), "join()")
    finally:
        await _end(stream)

    stats = stream.stats
    assert stream.state is S.STOPPED
    assert "m2" in calls
    assert calls == [f"m{i}" for i in range(stats.lines)]
    assert (stats.delivered, stats.undelivered) == (stats.lines, 0)


# -- F3-6: a callback's own CancelledError -------------------------------------


class _Raiser:
    """The callbacks of the F3-6 test: they record what they get and raise a
    CancelledError of their own on their stage's record: on_item on the
    second entry, on_event on the change to RUNNING.
    """

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.calls: list[str] = []
        self.stray = asyncio.CancelledError("not a cancellation of any task")

    def plain(self, item: LogEntry | StreamEvent) -> None:
        if isinstance(item, LogEntry):
            self.calls.append(item.message)
            if self.stage == "on_item" and item.message == "m1":
                raise self.stray
        elif isinstance(item, StateChange):
            self.calls.append(item.new)
            if self.stage != "on_item" and item.new is S.RUNNING:
                raise self.stray

    async def awaited(self, item: LogEntry | StreamEvent) -> None:
        await asyncio.sleep(0)
        self.plain(item)

    def stream(self, kind: str) -> AsyncLogStream[LogEntry]:
        """The stream of the test, with the ``kind`` of callback for its stage."""
        callback: Callable[[LogEntry | StreamEvent], object] = (
            self.awaited if kind == "async" else self.plain
        )
        if self.stage == "on_item":
            return AsyncLogStream(config(), on_item=callback)
        if self.stage == "on_event":
            return AsyncLogStream(config(), on_item=callback, on_event=callback)
        return AsyncLogStream(config(), on_event=callback)


@pytest.mark.parametrize("kind", ["async", "plain"])
@pytest.mark.parametrize("stage", ["on_item", "on_event", "pull_on_event"])
@pytest.mark.asyncio
async def test_callback_cancelled_error_is_callback_error(
    fake_adb: FakeAdb, stage: str, kind: str
) -> None:
    """A callback that raises CancelledError while its task is not being
    cancelled fails the run with CallbackError (F3-6, design 4.2); 2.x's
    dispatcher died of it silently and dropped every later entry.

    on_item raises on the second entry, on_event on the change to RUNNING,
    as a plain or an async function, on the dispatcher or (pull_on_event)
    inside ``__anext__()``. The error chains that CancelledError, join() and
    the iteration raise it, no callback runs after it (fail fast, design 2.4),
    adb is gone, and the consumer's own task is not cancelled.
    """
    fake_adb.logcat(run(lines(3, logcat_line("m{i}")), wait("never")))
    raiser = _Raiser(stage)
    stream = raiser.stream(kind)
    stream.start()
    iterated: list[str] = []
    from_iteration: CallbackError | None = None
    try:
        if stage == "pull_on_event":
            with pytest.raises(CallbackError) as raised:
                await _bounded(_rest(aiter(stream), iterated), "the iteration")
            from_iteration = raised.value
        with pytest.raises(CallbackError) as info:
            await _bounded(stream.join(), "join()")
        survivors = await fake_adb.async_survivors(PROMPT)
    finally:
        await _end(stream)

    error = info.value
    assert error.stage == ("on_item" if stage == "on_item" else "on_event")
    assert error.__cause__ is raiser.stray
    if stage == "pull_on_event":
        assert from_iteration is error
    expected: list[str] = (
        ["m0", "m1"] if stage == "on_item" else [S.CONNECTING, S.RUNNING]
    )
    assert raiser.calls == expected
    assert iterated == []
    assert stream.state is S.FAILED
    assert survivors == []
    current = asyncio.current_task()
    assert current is not None
    assert current.cancelling() == 0


@pytest.mark.parametrize("kind", INTERRUPTS)
@pytest.mark.asyncio
async def test_pull_on_event_interrupt_settles(fake_adb: FakeAdb, kind: str) -> None:
    """KeyboardInterrupt or SystemExit from on_event in pull mode propagates
    from ``__anext__()`` (design A.23: only an Exception there is a callback
    failure), and the next ``__anext__()`` settles that event instead of
    handing it to on_event again, then returns the next item. The stopped
    run then ends STOPPED with nothing lost.
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    interrupt = _interrupt(kind)
    seen: list[StreamState] = []

    def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange):
            seen.append(event.new)
            if event.new is S.RUNNING and seen.count(S.RUNNING) == 1:
                raise interrupt

    stream = AsyncLogStream(config(), on_event=on_event)
    stream.start()
    try:
        iterator = aiter(stream)
        with pytest.raises(type(interrupt)) as info:
            await _bounded(_next(iterator), "the first __anext__()")
        entry = await _bounded(_next(iterator), "the next __anext__()")
        stream.stop()
        rest = await _bounded(_rest(iterator), "the rest of the iteration")
        await _bounded(stream.join(), "join()")
    finally:
        await _end(stream)

    assert info.value is interrupt
    assert entry.message == "a"
    assert rest == []
    assert seen == [S.CONNECTING, S.RUNNING, S.STOPPING, S.STOPPED]
    assert stream.stats.complete


@pytest.mark.parametrize("kind", INTERRUPTS)
def test_on_item_interrupt_leaves_the_loop(
    fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    """KeyboardInterrupt or SystemExit from an async push callback leaves
    ``asyncio.run``: Ctrl+C or ``sys.exit()`` on the loop's thread is not a
    callback failure in the async driver (design A.24; the thread driver
    keeps A.23's rule, a callback failure, because a thread would drop a
    SystemExit). The run ends with it: join() raises it and the capture is
    failed; no adb is left, and asyncio does not report the interrupt once
    more, as a task exception never retrieved, when the dispatcher task is
    collected.
    """
    caplog.set_level(logging.WARNING)
    fake_adb.logcat(run(lines(3, logcat_line("m{i}")), wait("never")))
    interrupt = _interrupt(kind)
    calls: list[str] = []

    async def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        raise interrupt

    stream = AsyncLogStream(config(), on_item=on_item)

    async def main() -> None:
        async with stream:
            await stream.join()

    async def joined() -> BaseException | None:
        try:
            await stream.join()
        except BaseException as exc:  # noqa: BLE001  (the test looks at it)
            return exc
        return None

    raised = _interrupt_of(lambda: asyncio.run(_bounded(main(), "the run")))
    gc.collect()  # a task whose exception nobody retrieved reports it now
    error = asyncio.run(joined())

    assert raised is interrupt
    assert error is interrupt
    assert calls == ["m0"]
    assert stream.stats.failed
    assert fake_adb.survivors(PROMPT) == []
    assert _errors(caplog) == []


@dataclass(frozen=True, slots=True)
class _UserCode:
    """The user code of the stream in `test_interrupt_in_a_task_leaves_the_loop`."""

    where: Where | None = None
    on_item: Callable[[LogEntry], object] | None = None
    on_event: Callable[[StreamEvent], object] | None = None


def _interrupt_in(
    where: str, monkeypatch: pytest.MonkeyPatch, interrupt: BaseException
) -> _UserCode:
    """Raise ``interrupt`` once in the task of the run that ``where`` names,
    as Python raises Ctrl+C (or a SIGTERM handler's ``sys.exit()``) in
    whatever frame runs on the main thread; the user code the stream takes.

    ``where``, ``on_item`` and ``on_event`` (on the dispatcher) raise it
    themselves, in the engine and the dispatcher; the other cases patch the
    task's code.
    """
    raised: list[bool] = []

    def once() -> None:
        if not raised:
            raised.append(True)
            raise interrupt

    def condition(entry: LogEntry) -> bool:
        del entry
        once()
        return True

    def on_item(entry: LogEntry) -> None:
        del entry
        once()

    def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange) and event.new is S.RUNNING:
            once()

    if where == "where":
        return _UserCode(where=Where(condition))
    if where == "on_item":
        return _UserCode(on_item=on_item)
    if where == "on_event":
        return _UserCode(on_item=lambda _: None, on_event=on_event)
    if where == "reader":
        real_put = async_stream._Run._put

        async def put(run: Any, event: Any) -> bool:  # noqa: ANN401
            once()
            return await real_put(run, event)

        monkeypatch.setattr(async_stream._Run, "_put", put)
    elif where == "exit":
        real_post = async_stream._Run._post

        def post(run: Any, event: Any) -> str | None:  # noqa: ANN401
            if isinstance(event, Exited):
                once()
            return real_post(run, event)

        monkeypatch.setattr(async_stream._Run, "_post", post)
    elif where == "spawn":
        real_spawn = adb._async_spawn_adb

        async def spawn(argv: Sequence[str]) -> Any:  # noqa: ANN401
            if "logcat" in argv:
                once()
            return await real_spawn(argv)

        monkeypatch.setattr(adb, "_async_spawn_adb", spawn)
    elif where == "command":
        real_command = adb._async_run_adb

        async def command(argv: Sequence[str], **options: Any) -> Any:  # noqa: ANN401
            once()
            return await real_command(argv, **options)

        monkeypatch.setattr(adb, "_async_run_adb", command)
    else:
        real_handle = StreamCore.handle

        def handle(core: StreamCore, event: Any, now: float) -> Any:  # noqa: ANN401
            if isinstance(event, Data):
                once()
            return real_handle(core, event, now)

        monkeypatch.setattr(StreamCore, "handle", handle)
    return _UserCode()


@pytest.mark.parametrize(
    "where",
    ["reader", "exit", "spawn", "command", "engine", "where", "on_item", "on_event"],
)
@pytest.mark.parametrize("kind", INTERRUPTS)
def test_interrupt_in_a_task_leaves_the_loop(
    fake_adb: FakeAdb,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    where: str,
) -> None:
    """A KeyboardInterrupt or SystemExit raised in a task of the run (Ctrl+C
    on the loop's thread, or a SIGTERM handler's ``sys.exit()``, lands in
    whatever frame runs there) leaves the loop at once: in a reader, an exit
    watcher, a spawn, a one-shot command (the clock read), the engine (the
    core, or the user's ``where``) or the dispatcher (``on_item``, or
    ``on_event`` in push mode).

    It is no logpyt bug and no callback failure, so nothing is logged:
    neither 'the run is abandoned' nor a task exception never retrieved. adb
    is killed before the loop stops, every wait ends, nothing is respawned
    although reconnect is configured, and the run ends as for a cancelled
    engine: join() raises that interrupt, and the capture is failed (design
    A.23, A.24). A worker swallowed a KeyboardInterrupt and the program went
    on; a SystemExit was logged as a crash, or failed the run with
    CallbackError from a callback; one from the dispatcher only detached it,
    and adb ran until the loop's teardown.
    """
    caplog.set_level(logging.WARNING)
    interrupt = _interrupt(kind)
    user = _interrupt_in(where, monkeypatch, interrupt)
    first = run(out(logcat_line("a")), exit_code=1)
    if where != "exit":
        first = run(out(logcat_line("a")), wait("never"))
    fake_adb.logcat(first, run(out(logcat_line("again")), wait("never")))
    stream = AsyncLogStream(
        config(reconnect=Reconnect(delay=0.05, max_delay=0.05)),
        where=user.where,
        on_item=user.on_item,
        on_event=user.on_event,
    )

    async def main() -> None:
        stream.start()
        # The interrupt ends the run; without it this returns normally.
        await async_wait_until(lambda: stream._run.ended, timeout=TIMEOUT)

    async def joined() -> BaseException | None:
        try:
            await stream.join()
        except BaseException as exc:  # noqa: BLE001  (the test looks at it)
            return exc
        return None

    loop = asyncio.new_event_loop()
    try:
        raised = _interrupt_of(lambda: loop.run_until_complete(main()))
        ended = stream._run.ended
        survivors = fake_adb.survivors(PROMPT)
        _close_loop(loop)
        loop = asyncio.new_event_loop()
        error = loop.run_until_complete(joined())
    finally:
        _close_loop(loop)
    gc.collect()  # a task whose exception nobody retrieved reports it now

    assert raised is interrupt, "the interrupt did not leave the loop"
    assert ended, "a wait for the run did not end"
    assert survivors == []
    assert len(fake_adb.calls("logcat")) == (0 if where in {"spawn", "command"} else 1)
    assert error is interrupt
    assert stream.stats.failed
    assert not stream.stats.complete
    assert _errors(caplog) == []


@pytest.mark.parametrize("where", ["engine", "where"])
@pytest.mark.parametrize("kind", INTERRUPTS)
def test_engine_interrupt_reaps_adb(
    fake_adb: FakeAdb, monkeypatch: pytest.MonkeyPatch, kind: str, where: str
) -> None:
    """An interrupt in the engine (the core, or the user's ``where``) under
    ``asyncio.run`` leaves the loop before the engine's end ran; that end is
    under way already, so the loop's teardown, which cancels what is left,
    runs it to its end: the killed logcat child is reaped before the loop
    closes. Before, nothing awaited the child any more, and it stayed a
    zombie with its pidfd and its transport open.

    `SpawnedChild.alive` counts an exited child that nobody reaped as gone,
    so this reads ``returncode``, which asyncio sets once it reaped it.
    """
    interrupt = _interrupt(kind)
    user = _interrupt_in(where, monkeypatch, interrupt)
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    stream = AsyncLogStream(config(), where=user.where)

    async def main() -> None:
        async with stream:
            await stream.join()

    raised = _interrupt_of(lambda: asyncio.run(_bounded(main(), "the run")))
    codes = [
        None if child.process is None else child.process.returncode
        for child in fake_adb.children("logcat")
    ]

    assert raised is interrupt
    assert len(codes) == 1
    assert None not in codes, "the killed logcat child was not reaped"
    assert fake_adb.survivors(PROMPT) == []


@pytest.mark.parametrize("kind", INTERRUPTS)
def test_crash_keeps_an_earlier_end(
    fake_adb: FakeAdb,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    """A push callback's interrupt while a crash (a logpyt bug in the core)
    reaps adb ends the run first, and the crash's own end, once its reap is
    over, keeps that end: join() raises the interrupt before and after the
    reap, the same object every time (design 4, A.23). The bug is still
    logged. Before, the crash replaced the interrupt once its reap was over,
    and join() raised the bug from then on.
    """
    caplog.set_level(logging.WARNING)
    fake_adb.logcat(
        run(
            out(logcat_line("m0")),
            wait("more"),
            out(logcat_line("m1")),
            wait("never"),
        )
    )
    interrupt = _interrupt(kind)
    bug = RuntimeError("a bug in the core")
    crashing = asyncio.Event()
    real_handle = StreamCore.handle
    real_crashed = async_stream._Run._crashed

    def handle(core: StreamCore, event: Any, now: float) -> Any:  # noqa: ANN401
        if isinstance(event, Data) and b"m1" in event.chunk:
            raise bug
        return real_handle(core, event, now)

    async def crashed(run: Any, error: BaseException) -> None:  # noqa: ANN401
        crashing.set()
        await real_crashed(run, error)

    async def on_item(entry: LogEntry) -> None:
        del entry
        fake_adb.release("more")
        await crashing.wait()  # the crash has begun its reap
        raise interrupt

    monkeypatch.setattr(StreamCore, "handle", handle)
    monkeypatch.setattr(async_stream._Run, "_crashed", crashed)
    stream = AsyncLogStream(config(), on_item=on_item)

    async def main() -> None:
        stream.start()
        await async_wait_until(lambda: stream._run.ended, timeout=TIMEOUT)

    async def reaped() -> None:
        await async_wait_until(lambda: not _run_tasks(), timeout=TIMEOUT)

    loop = asyncio.new_event_loop()
    try:
        raised = _interrupt_of(lambda: loop.run_until_complete(main()))
        before = loop.run_until_complete(_join_error(stream))
        loop.run_until_complete(reaped())
        after = loop.run_until_complete(_join_error(stream))
        survivors = fake_adb.survivors(PROMPT)
    finally:
        _close_loop(loop)

    assert raised is interrupt
    assert before is interrupt
    assert after is interrupt, "the crash replaced the end of the run"
    assert stream.stats.failed
    assert survivors == []
    assert _errors(caplog) == [
        "logpyt.stream: the AsyncLogStream engine failed; the run is abandoned"
    ]


# -- A.21: a cancelled __anext__ -------------------------------------------------


@pytest.mark.asyncio
async def test_anext_cancel_in_on_event_settles(fake_adb: FakeAdb) -> None:
    """``__anext__()`` cancelled while its on_event awaits: the cancellation
    propagates and cancels only that wait (design 5.4); the next
    ``__anext__()`` settles the event in hand instead of handing it out again,
    so the stopped run's drain ends (A.21) and nothing is lost.

    An unsettled event would keep the drain waiting forever; one handed out
    again would reach on_event twice.
    """
    fake_adb.logcat(
        run(out(logcat_line("a")), wait("b"), out(logcat_line("b")), wait("never"))
    )
    entered = asyncio.Event()
    events: list[tuple[StreamState, StreamState]] = []

    async def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange):
            events.append((event.old, event.new))
            if event.new is S.RUNNING:
                entered.set()
                await asyncio.Event().wait()  # until cancelled

    stream = AsyncLogStream(config(), on_event=on_event)
    stream.start()
    try:
        iterator = aiter(stream)
        step = asyncio.create_task(_next(iterator))
        await _bounded(entered.wait(), "on_event of the change to RUNNING")
        step.cancel()
        await asyncio.wait({step}, timeout=TIMEOUT)
        cancelled = step.cancelled()
        state_after = stream.state
        first = await _bounded(_next(iterator), "the next __anext__()")
        fake_adb.release("b")
        second = await _bounded(_next(iterator), "the second item")
        stream.stop()
        rest = await _bounded(_rest(iterator), "the rest of the iteration")
        await _bounded(stream.join(), "join()")
    finally:
        await _end(stream)

    assert cancelled
    assert state_after is S.RUNNING
    assert [first.message, second.message] == ["a", "b"]
    assert rest == []
    assert events == [
        (S.IDLE, S.CONNECTING),
        (S.CONNECTING, S.RUNNING),
        (S.RUNNING, S.STOPPING),
        (S.STOPPING, S.STOPPED),
    ]
    assert stream.state is S.STOPPED
    stats = stream.stats
    assert (stats.delivered, stats.undelivered) == (2, 0)
    assert stats.complete


@pytest.mark.asyncio
async def test_anext_cancel_at_checkpoint_loses_nothing(fake_adb: FakeAdb) -> None:
    """``__anext__()`` cancelled at the once-per-batch checkpoint (X-1, design
    5.4) loses no record: the checkpoint comes before the step settles the
    record in hand and pops the next (A.21), so the cancelled call took
    nothing and the next call goes on where it stopped.

    Every line is in the outbox before the iteration starts, so a call
    suspends only at a checkpoint. Each call runs one step on its own; one
    that has not returned by then is cancelled there and the next call is
    made normally. Every line arrives once, in order, and the stopped run
    ends STOPPED with nothing undelivered.
    """
    count = 300
    fake_adb.logcat(run(lines(count, logcat_line("m{i}")), wait("never")))
    stream = AsyncLogStream(config())
    stream.start()
    messages: list[str] = []
    cancelled = 0
    try:
        await async_wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
        iterator = aiter(stream)
        while len(messages) < count:
            step = asyncio.create_task(_next(iterator))
            await asyncio.sleep(0)  # the step runs until it returns or suspends
            if not step.done():
                step.cancel()
                await asyncio.wait({step}, timeout=TIMEOUT)
                assert step.cancelled()
                cancelled += 1
                step = asyncio.create_task(_next(iterator))
            entry = await _bounded(step, f"item {len(messages)}")
            messages.append(entry.message)
        stream.stop()
        rest = await _bounded(_rest(iterator), "the rest of the iteration")
        await _bounded(stream.join(), "join()")
    finally:
        await _end(stream)

    assert cancelled >= 1, "no __anext__() suspended at a checkpoint"
    assert messages == [f"m{i}" for i in range(count)]
    assert rest == []
    assert stream.state is S.STOPPED
    stats = stream.stats
    assert (stats.delivered, stats.undelivered) == (count, 0)
    assert stats.complete


# -- X-1: another task's turns during a burst ---------------------------------


class _Taker:
    """The consumer of the burst test: counts what it takes, until ``total``.

    As ``on_item`` it waits for ``gate`` first; as a pull loop it starts
    only once the test starts it, so both leave the backlog in the outbox.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.taken = 0
        self.gate = asyncio.Event()
        self.finished = asyncio.Event()

    def _took(self) -> None:
        self.taken += 1
        if self.taken == self.total:
            self.finished.set()

    async def on_item(self, entry: LogEntry) -> None:
        del entry
        if not self.gate.is_set():
            await self.gate.wait()
        self._took()

    async def pull(self, stream: AsyncLogStream[LogEntry]) -> None:
        async for _ in stream:
            self._took()


class _Turns:
    """Another task of the loop: at each of its turns it notes the loop's
    clock, what the consumer took and what the stream read; it yields at
    once (``asyncio.sleep(0)``), so it has one turn per pass of the loop.
    """

    def __init__(self, stream: AsyncLogStream[LogEntry], taker: _Taker) -> None:
        self._stream = stream
        self._taker = taker
        self.seen: list[tuple[float, int, int]] = []

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._taker.finished.is_set():
            self.seen.append((loop.time(), self._taker.taken, self._stream.stats.lines))
            await asyncio.sleep(0)

    def most(self, field: int) -> float:
        """The largest change of ``seen[...][field]`` from one turn to the next."""
        return max(b[field] - a[field] for a, b in itertools.pairwise(self.seen))


@pytest.mark.parametrize("mode", ["pull", "push"])
@pytest.mark.asyncio
async def test_other_task_latency_under_burst(fake_adb: FakeAdb, mode: str) -> None:
    """Another task of the loop keeps getting turns while the stream takes a
    burst (X-1, design 5.4); 2.x's readers never yielded, so a burst starved
    everything else on the loop.

    The consumer first faces a nearly full outbox (9 000 lines), then a
    50 000-line burst arrives while it keeps up. A task that yields at every
    pass of the loop notes the clock and two counters (`_Turns`): the
    records the consumer took and the lines the engine handled. The consumer
    yields once per batch and the engine once per chunk, so between two of
    its turns each takes at most two reads' worth, and no turn waits longer
    than LATENCY_BOUND. The readers' own yield after each read (design 5.4)
    is not observable here: a reader without it is held back by the data
    inbox and by ``StreamReader.read``, which suspends once its buffer is
    empty. The garbage collector is off while it measures: its pauses are
    not the driver's.
    """
    backlog, burst = 9_000, 50_000
    fake_adb.logcat(
        run(
            lines(backlog, logcat_line("m{i}")),
            wait("burst"),
            lines(burst, logcat_line("b{i}")),
            wait("never"),
        )
    )
    chunk_lines = READ_SIZE // len(logcat_line("b0").encode()) + 1
    taker = _Taker(backlog + burst)
    stream = AsyncLogStream(config(), on_item=taker.on_item if mode == "push" else None)
    turns = _Turns(stream, taker)
    stream.start()
    consumer: asyncio.Task[None] | None = None
    try:
        await async_wait_until(lambda: stream.stats.lines == backlog, timeout=TIMEOUT)
        gc.disable()
        try:
            other = asyncio.create_task(turns.run())
            if mode == "pull":
                consumer = asyncio.create_task(taker.pull(stream))
            taker.gate.set()
            fake_adb.release("burst")
            await _bounded(taker.finished.wait(), "the burst", 3 * TIMEOUT)
            await other
        finally:
            gc.enable()
        stream.stop()
        if consumer is not None:
            await _bounded(consumer, "the end of the iteration")
        await _bounded(stream.join(), "join()")
    finally:
        if consumer is not None:
            consumer.cancel()
        await _end(stream)

    worst, taken, read = turns.most(0), turns.most(1), turns.most(2)
    assert taken <= 2 * chunk_lines, f"the consumer took {taken} items in one turn"
    assert read <= 2 * chunk_lines, f"the stream read {read} lines in one turn"
    assert worst < LATENCY_BOUND, f"another task waited {worst * 1000:.1f} ms"
    assert taker.taken == taker.total
    assert (stream.stats.delivered, stream.state) == (taker.total, S.STOPPED)


# -- X-5: the end closes the pipes ---------------------------------------------


@pytest.mark.asyncio
async def test_end_closes_a_held_pipe(fake_adb: FakeAdb) -> None:
    """The end of a run whose retired connection's stdout a grandchild still
    holds closes that pipe and leaves no task of the run, before the
    grandchild lets go (X-5, design 5.4, 5.10): the engine's end cancels the
    reader, which closes its pipe. A reader left running would keep the
    pipe's fd and its task for as long as the grandchild lives, after join()
    returned.
    """
    fake_adb.logcat(
        run(
            out(logcat_line("a")),
            hold_stdout("let go", then=logcat_line("late")),
            exit_code=0,
        )
    )
    entries: list[LogEntry] = []
    stream = AsyncLogStream(config(exit_drain=0.2), on_item=entries.append)
    stream.start()
    try:
        with pytest.raises(AdbExitError):
            await _bounded(stream.join(), "join()")
        (child,) = fake_adb.children("logcat")

        def closed() -> bool:
            return child.pipe_closed(child.popen.stdout)

        # join() returns at the end; the tasks are cancelled right after it.
        with contextlib.suppress(TimeoutError):
            await async_wait_until(
                lambda: closed() and not _run_tasks(), timeout=TIMEOUT
            )
        pipe_closed = closed()
        left = sorted(task.get_name() for task in _run_tasks())
    finally:
        fake_adb.release("let go")
        await _end(stream)

    assert _messages(entries) == ["a"]
    assert pipe_closed, "the held stdout pipe was still open after the end"
    assert left == []


# -- design 4.2: where start() may run ---------------------------------------------


def test_start_needs_a_running_loop(fake_adb: FakeAdb) -> None:
    """start() with no running loop in its thread raises UsageError (design
    2.3, 4.2): from plain code, and from a worker thread of a program whose
    loop runs. It spawns nothing, the stream stays IDLE, and it still starts
    on the loop afterwards.
    """
    fake_adb.logcat(run(wait("never")))
    stream = AsyncLogStream(config())

    with pytest.raises(UsageError, match="needs a running event loop"):
        stream.start()

    async def main() -> None:
        with pytest.raises(UsageError, match="needs a running event loop"):
            await asyncio.to_thread(stream.start)
        assert stream.state is S.IDLE
        assert fake_adb.calls() == []
        stream.start()
        stream.stop()
        await _bounded(stream.join(), "join()")

    asyncio.run(_bounded(main(), "the run"))

    assert stream.state is S.STOPPED


def test_start_on_a_windows_selector_loop_raises_usage_error(
    fake_adb: FakeAdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows, start() on a selector event loop raises UsageError naming
    the proactor loop, which asyncio needs there for subprocesses (design
    4.2, 9.2). Elsewhere the selector loop is made to look like Windows's by
    patching ``sys.platform`` around the call. The refused stream spawned
    nothing, stays IDLE, and runs on ``asyncio.run``'s loop (the proactor
    loop on Windows).
    """
    fake_adb.logcat(run(wait("never")))
    stream = AsyncLogStream(config())
    selector = asyncio.SelectorEventLoop()

    async def refused() -> None:
        with monkeypatch.context() as patch:
            if sys.platform != "win32":
                patch.setattr(sys, "platform", "win32")
            stream.start()

    try:
        with pytest.raises(UsageError, match="proactor event loop"):
            selector.run_until_complete(refused())
    finally:
        selector.close()
    assert stream.state is S.IDLE
    assert fake_adb.calls() == []

    async def main() -> None:
        stream.start()
        stream.stop()
        await _bounded(stream.join(), "join()")

    asyncio.run(_bounded(main(), "the run"))

    assert stream.state is S.STOPPED


def _on_another_loop(wait: Callable[[], Awaitable[None]]) -> BaseException | None:
    """Await ``wait()`` under an ``asyncio.run`` of this thread; what it raised."""

    async def main() -> None:
        await wait()

    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001  (the test looks at it)
        return exc
    return None


@pytest.mark.parametrize("what", ["join", "wait_running"])
@pytest.mark.asyncio
async def test_wait_on_another_loop_raises(fake_adb: FakeAdb, what: str) -> None:
    """join() and wait_running() awaited on another event loop than the run's,
    while the run goes on, raise UsageError (design 4.2): the run's waiters
    belong to its loop. wait_running() raises it also once the run was
    RUNNING, where it would return at once.
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    stream = AsyncLogStream(config())
    stream.start()
    try:
        await _bounded(stream.wait_running(TIMEOUT), "wait_running()")
        call = stream.join if what == "join" else stream.wait_running
        error = await asyncio.to_thread(_on_another_loop, lambda: call(TIMEOUT))
        state = stream.state
    finally:
        await _end(stream)

    assert isinstance(error, UsageError), error
    assert "outside the event loop" in str(error)
    assert state is S.RUNNING


@pytest.mark.asyncio
async def test_aiter_off_the_loop_raises(fake_adb: FakeAdb) -> None:
    """``aiter(stream)`` in another thread than the loop's raises UsageError
    (design 3.5): the iterator belongs to a task of the run's loop. The
    stream can still be iterated on its loop.
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    stream = AsyncLogStream(config())
    stream.start()
    try:
        with pytest.raises(UsageError, match="outside the event loop"):
            await asyncio.to_thread(aiter, stream)
        entry = await _bounded(_next(aiter(stream)), "the first item")
    finally:
        await _end(stream)

    assert entry.message == "a"


@pytest.mark.asyncio
async def test_second_anext_raises(fake_adb: FakeAdb) -> None:
    """A second ``__anext__()`` while one is pending in another task raises
    UsageError at once instead of waiting beside it (design 3.5: one
    consumer); the pending one then gets the item.
    """
    fake_adb.logcat(run(wait("go"), out(logcat_line("a")), wait("never")))
    stream = AsyncLogStream(config())
    stream.start()
    try:
        iterator = aiter(stream)
        pending = asyncio.create_task(_next(iterator))
        await async_wait_until(lambda: stream.state is S.CONNECTING, timeout=TIMEOUT)
        await asyncio.sleep(0)  # the pending step waits for a record
        with pytest.raises(UsageError, match="two tasks at once"):
            await _bounded(_next(iterator), "the second __anext__()", 1.0)
        fake_adb.release("go")
        entry = await _bounded(pending, "the pending __anext__()")
    finally:
        await _end(stream)

    assert entry.message == "a"


# -- F3-7: waits from a task a callback created --------------------------------


async def _try_waits(
    stream: AsyncLogStream[LogEntry], name: str, said: dict[str, str]
) -> None:
    """Await join() and wait_running(); record in ``said`` what each did."""
    for what in ("join", "wait_running"):
        wait = stream.join if what == "join" else stream.wait_running
        try:
            await _bounded(wait(TIMEOUT), f"{what}() in the {name} task")
        except UsageError:
            said[f"{name} {what}"] = "UsageError"
        except BaseException as exc:  # noqa: BLE001  (the test looks at it)
            said[f"{name} {what}"] = type(exc).__name__
        else:
            said[f"{name} {what}"] = "returned"


@pytest.mark.asyncio
async def test_callback_task_waits_after_the_end(fake_adb: FakeAdb) -> None:
    """A task created inside a callback inherits the callback's mark with its
    context (F3-7): while the run lives, its join() and wait_running() raise
    UsageError, because the callback may await that task (design 3.6, 4.2).
    Once the run has ended there is nothing to deadlock on: they return. A
    task created with an empty context (``context=contextvars.Context()``)
    is no stream code: its waits wait for the end.
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    ended = asyncio.Event()
    said: dict[str, str] = {}
    later: list[asyncio.Task[None]] = []
    stream: AsyncLogStream[LogEntry]

    async def after_the_end() -> None:
        await ended.wait()
        await _try_waits(stream, "after", said)

    async def on_item(entry: LogEntry) -> None:
        del entry
        # The case F3-7 is for: the callback awaits a task that waits.
        await asyncio.create_task(_try_waits(stream, "live", said))
        later.append(asyncio.create_task(after_the_end()))
        later.append(
            asyncio.create_task(
                _try_waits(stream, "fresh", said), context=contextvars.Context()
            )
        )
        stream.stop()

    stream = AsyncLogStream(config(), on_item=on_item)
    stream.start()
    try:
        await _bounded(stream.join(), "join()")
        ended.set()
        await _bounded(asyncio.gather(*later), "the tasks on_item created")
    finally:
        ended.set()
        await _end(stream)

    assert said == {
        "live join": "UsageError",
        "live wait_running": "UsageError",
        "fresh join": "returned",
        "fresh wait_running": "returned",
        "after join": "returned",
        "after wait_running": "returned",
    }
    assert stream.state is S.STOPPED


@pytest.mark.parametrize("mode", ["push", "pull"])
@pytest.mark.asyncio
async def test_callback_waits_after_the_end_raise(fake_adb: FakeAdb, mode: str) -> None:
    """A callback's own join() and wait_running() raise UsageError after the
    run has ended too, as in the thread driver (F3-7): only a task created
    inside a callback is let wait once the run ended. The run ends while
    on_item (push) or on_event (pull, inside ``__anext__()``) awaits: its
    engine is cancelled, as a loop shutdown does (F3-2).
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    inside = asyncio.Event()
    go = asyncio.Event()
    said: dict[str, str] = {}
    stream: AsyncLogStream[LogEntry]

    async def callback(record: object) -> None:
        if inside.is_set():
            return
        if mode == "pull" and not (
            isinstance(record, StateChange) and record.new is S.RUNNING
        ):
            return
        inside.set()
        await go.wait()
        await _try_waits(stream, "own", said)

    if mode == "push":
        stream = AsyncLogStream(config(), on_item=callback)
    else:
        stream = AsyncLogStream(config(), on_event=callback)
    stream.start()
    pull = asyncio.create_task(_rest(aiter(stream))) if mode == "pull" else None
    try:
        await _bounded(inside.wait(), "the callback")
        _task("logpyt-engine").cancel()
        await async_wait_until(lambda: stream._run.ended, timeout=TIMEOUT)
        go.set()
        if pull is not None:
            await asyncio.wait({pull}, timeout=TIMEOUT)
        await async_wait_until(lambda: len(said) == 2, timeout=TIMEOUT)
    finally:
        go.set()
        if pull is not None:
            pull.cancel()
        await _end(stream)

    assert said == {"own join": "UsageError", "own wait_running": "UsageError"}
