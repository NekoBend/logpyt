"""Conformance tests: the behavior every stream driver shares (design 7, 8.3).

A test takes the ``driver`` fixture and makes its lifecycle calls through it
(construct, start, stop, kill, join, wait_running, iterate, the with-block),
so one test body serves every driver: ``"sync"`` (`LogStream`) and
``"async"`` (`AsyncLogStream`). Callbacks are plain functions, which both
drivers take.

`AsyncDriver` runs one event loop per test in a helper thread. Each thread of
the test body stands for one task of an async program: its calls run, in
order, in one task on the loop (`_Agent`), so the task that creates an
iterator owns it (design 3.5) as the thread does for `LogStream`. ``stop()``
and ``kill()`` are called from the test thread itself, through the stream's
thread-safe path; the loop runs in debug mode, where asyncio raises for a
call that touches it from another thread. A plain Python callback runs in a
worker thread (``asyncio.to_thread``), so the stream awaits it like an async
callback and the loop runs on while it blocks on the test's events, as
`LogStream`'s engine does while its dispatcher thread blocks; a builtin such as
``list.append`` runs on the loop itself. A driver call made inside such a
callback runs in a task of its own that inherits the callback's context, as
a ``join()`` awaited by an async callback would.

Every call that waits is bounded: ``join`` and ``wait_running`` by their own
timeout, and a pull ``next()`` or a with-block exit by `_bounded`, which kills
the stream when the call has not returned within `TIMEOUT`, so a hang fails
the test instead of waiting for the hang watchdog.

The stall resume runs with its default (``stall_timeout=1.0``): since PR-14
a resumed connection drops its replay (design 5.6), also the scripted fake
adb's, which replays a run from its start whatever ``-T`` says. The fake adb
answers the clock read that precedes every connection with the host clock
(``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import functools
import inspect
import os
import sys
import threading
import weakref
from typing import TYPE_CHECKING, Any, cast

import pytest

from logpyt import adb
from logpyt.config import StreamConfig
from logpyt.streams import AsyncLogStream, LogStream, async_stream, sync
from tests.conftest import wait_until

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

# The drivers every conformance test runs for.
DRIVERS = ("sync", "async")

# Upper bound for every wait; a healthy run finishes far sooner.
TIMEOUT = 10.0


def config(**fields: Any) -> StreamConfig:  # noqa: ANN401
    """A `StreamConfig` for these tests (the defaults, stall resume included)."""
    return StreamConfig(**fields)


def _bounded[R](stream: LogStream[Any], call: Callable[[], R], what: str) -> R:
    """Run ``call``; kill ``stream`` and fail if it has not returned in TIMEOUT."""
    fired = threading.Event()

    def expire() -> None:
        fired.set()
        stream.kill()

    timer = threading.Timer(TIMEOUT, expire)
    timer.daemon = True
    timer.start()
    message = f"{what} did not return within {TIMEOUT} s; the stream was killed"
    try:
        result = call()
    except BaseException:
        timer.cancel()
        if fired.is_set():
            pytest.fail(message)
        raise
    timer.cancel()
    if fired.is_set():
        pytest.fail(message)
    return result


class Pull:
    """A pull iterator of a stream, with bounded steps."""

    def __init__(self, stream: LogStream[Any]) -> None:
        self._stream = stream
        self._iterator: Any = iter(stream)

    def next(self) -> Any:  # noqa: ANN401
        """The next item; StopIteration at the end."""
        return _bounded(self._stream, lambda: next(self._iterator), "next()")

    def rest(self) -> list[Any]:
        """Every item up to the end of the iteration."""
        return _bounded(self._stream, lambda: list(self._iterator), "iteration")

    def close(self) -> None:
        """Detach the iterator (design 3.5)."""
        self._iterator.close()


class SyncDriver:
    """The thread driver: each call is the `LogStream` method itself."""

    name = "sync"
    # How the stream names itself in its log messages.
    noun = "a LogStream"
    stream_class = "LogStream"
    # The fds a live connection holds: its stdout and stderr pipes.
    connection_fds = 2

    def open(
        self,
        config: StreamConfig | None = None,
        /,
        **kwargs: Any,  # noqa: ANN401  (the LogStream keywords, passed on)
    ) -> LogStream[Any]:
        """Construct a stream (it spawns nothing)."""
        return LogStream(config, **kwargs)

    def start(self, stream: LogStream[Any]) -> None:
        stream.start()

    def stop(self, stream: LogStream[Any], *, flush: float | None = None) -> None:
        stream.stop(flush=flush)

    def kill(self, stream: LogStream[Any]) -> None:
        stream.kill()

    def join(self, stream: LogStream[Any], timeout: float | None = TIMEOUT) -> None:
        """``join(timeout)``; None waits without a bound (the interrupt tests)."""
        stream.join(timeout)

    def wait_running(
        self, stream: LogStream[Any], timeout: float | None = TIMEOUT
    ) -> None:
        """``wait_running(timeout)``; None waits without a bound."""
        stream.wait_running(timeout)

    def iterate(self, stream: LogStream[Any]) -> Pull:
        """The stream's pull iterator."""
        return Pull(stream)

    def first_by_break(self, stream: LogStream[Any]) -> Any:  # noqa: ANN401
        """``for item in stream: break``: the first item, by a real loop that
        drops its iterator at the ``break`` (no `Pull` keeps it).
        """

        def first() -> Any:  # noqa: ANN401
            for item in stream:
                return item
            msg = "the iteration ended before the first item"
            raise AssertionError(msg)

        return _bounded(stream, first, "the first next()")

    def kill_in_the_loop(self, stream: LogStream[Any]) -> list[Any]:
        """``for item in stream: stream.kill()``: the items the loop body got.

        Nothing that could let another thread run comes between the kill()
        and the next step of the loop (the bound is armed once, around it).
        """

        def loop() -> list[Any]:
            got: list[Any] = []
            for item in stream:
                got.append(item)
                stream.kill()
            return got

        return _bounded(stream, loop, "the iteration")

    @contextlib.contextmanager
    def session(self, stream: LogStream[Any]) -> Iterator[LogStream[Any]]:
        """``with stream:``; the exit is bounded like every other wait, also
        after the body raised (the body's exception then propagates).
        """
        stream.__enter__()
        try:
            yield stream
        except BaseException as exc:
            leave = functools.partial(
                stream.__exit__, type(exc), exc, exc.__traceback__
            )
            _bounded(stream, leave, "the with-block exit")
            raise
        _bounded(
            stream, lambda: stream.__exit__(None, None, None), "the with-block exit"
        )

    def refuse_logcat_spawn(
        self, monkeypatch: pytest.MonkeyPatch, error: OSError
    ) -> list[tuple[str, ...]]:
        """Make every ``adb logcat`` spawn raise ``error``; return the argvs tried.

        The one-shot commands (the clock read) still run.
        """
        tried: list[tuple[str, ...]] = []
        real = adb._spawn_adb

        def spawn(argv: tuple[str, ...]) -> Any:  # noqa: ANN401
            if "logcat" in argv:
                tried.append(tuple(argv))
                raise error
            return real(argv)

        monkeypatch.setattr(adb, "_spawn_adb", spawn)
        return tried

    def consumer_identity(self) -> object:
        """What identifies the consumer context the caller runs in: its thread.

        The `threading.Thread` object, not its ident, which the OS hands out
        again once a thread ended (design A.23). A caller keeps the objects,
        so two contexts never share one.
        """
        return threading.current_thread()

    def wait_concluded(self, stream: LogStream[Any]) -> None:
        """Wait until the run concluded: terminal, or waiting in the drain (A.21).

        adb's end alone does not show that the core has handled it; this
        waits for what the engine published (`StreamCore.concluded`).
        """
        wait_until(lambda: stream._run.concluded, timeout=TIMEOUT)

    def break_reader(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        """Make the stdout reader of every connection raise ``error`` (a logpyt
        bug) at its second read; the first read returns what adb wrote.
        """
        real = cast("Callable[..., None]", sync._Run._read)

        class Broken:
            def __init__(self, pipe: Any) -> None:  # noqa: ANN401
                self._pipe = pipe
                self._reads = 0

            def read(self, size: int) -> bytes:
                self._reads += 1
                if self._reads == 2:
                    raise error
                return self._pipe.read(size)

            def close(self) -> None:
                self._pipe.close()

        def read(run: Any, connection: int, name: str, pipe: Any) -> None:  # noqa: ANN401
            real(run, connection, name, Broken(pipe) if name == "stdout" else pipe)

        monkeypatch.setattr(sync._Run, "_read", read)

    def hold_after_delivery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        hold: Callable[[Sequence[object]], None],
    ) -> None:
        """Call ``hold(records)`` right after the driver handed records to its
        consumer side, before it goes on (as a preemption there would): a test
        holds the engine to check what a consumer reads meanwhile. The records
        that end the run go out with the outcome and are not held.
        """
        real = cast("Callable[..., bool]", sync._Run._deliver)

        def deliver(run: Any, records: tuple[object, ...]) -> bool:  # noqa: ANN401
            changed = real(run, records)
            hold(records)
            return changed

        monkeypatch.setattr(sync._Run, "_deliver", deliver)


# The consumer task whose plain callback runs in this worker thread
# (`AsyncDriver` offloads Python callbacks with asyncio.to_thread).
_CONSUMER: contextvars.ContextVar[asyncio.Task[Any] | None] = contextvars.ContextVar(
    "conformance_consumer", default=None
)


def _child_watcher_fds() -> int:
    """The fds asyncio's child watcher holds per live child: a pidfd on Linux."""
    if sys.platform != "linux":
        return 0
    try:
        fd = os.pidfd_open(os.getpid())
    except OSError:
        return 0  # asyncio then watches children with a thread
    os.close(fd)
    return 1


def _is_async(callback: object) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        type(callback).__call__
    )


def _offloaded(callback: object) -> object:
    """``callback``, or for a plain Python function an async callback that runs
    it in a worker thread (see the module docs).

    A builtin (``list.append``), an async callback and anything that is not
    callable (the constructor refuses it) are passed on unchanged.
    """
    if (
        callback is None
        or not callable(callback)
        or inspect.isbuiltin(callback)
        or _is_async(callback)
    ):
        return callback
    plain = cast("Callable[[object], object]", callback)

    async def in_a_thread(record: object) -> object:
        token = _CONSUMER.set(asyncio.current_task())
        try:
            return await asyncio.to_thread(plain, record)
        finally:
            _CONSUMER.reset(token)

    return in_a_thread


type _Job = tuple[Callable[[], object], concurrent.futures.Future[Any]]


class _Agent:
    """One task on the loop that runs the calls of one test thread, in order.

    A call returns a value or an awaitable, which the task awaits. The task
    keeps no reference to a call once it finished, so a test can drop a
    stream it made.
    """

    def __init__(self) -> None:  # on the loop
        self.jobs: asyncio.Queue[_Job] = asyncio.Queue()
        self.task = asyncio.get_running_loop().create_task(
            self._serve(), name="conformance: agent"
        )

    async def _serve(self) -> None:
        while True:
            make, future = await self.jobs.get()
            try:
                if future.set_running_or_notify_cancel():
                    await _settle(make, future)
            finally:
                del make, future


async def _settle(
    make: Callable[[], object], future: concurrent.futures.Future[Any]
) -> None:
    """Run one call and hand its result or exception to ``future``."""
    try:
        result = make()
        if inspect.isawaitable(result):
            result = await result
    except BaseException as exc:  # handed to the test thread
        future.set_exception(exc)
        task = asyncio.current_task()
        if isinstance(exc, asyncio.CancelledError) and task and task.cancelling():
            raise
    else:
        future.set_result(result)


class AsyncDriver:
    """The asyncio driver, on an event loop of its own (see the module docs).

    ``close()`` cancels whatever still runs on the loop and closes it.
    """

    name = "async"
    noun = "an AsyncLogStream"
    stream_class = "AsyncLogStream"
    # Its stdout and stderr pipes, plus the pidfd asyncio's child watcher
    # holds for a live child on Linux.
    connection_fds = 2 + _child_watcher_fds()

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        # Debug mode makes asyncio raise for a call that touches the loop from
        # another thread, which stop() and kill() from the test thread must
        # never make; the long slow-callback limit keeps it from logging the
        # callbacks this adapter blocks on purpose.
        self.loop.set_debug(True)
        self.loop.slow_callback_duration = 60.0
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="conformance: event loop", daemon=True
        )
        self._thread.start()
        self._lock = threading.Lock()
        self._agents: weakref.WeakKeyDictionary[threading.Thread, _Agent] = (
            weakref.WeakKeyDictionary()
        )

    # -- running calls on the loop --------------------------------------------------

    def _agent(self) -> _Agent:
        thread = threading.current_thread()
        with self._lock:
            agent = self._agents.get(thread)
        if agent is None:

            async def make() -> _Agent:  # on the loop, for get_running_loop()
                return _Agent()

            agent = asyncio.run_coroutine_threadsafe(make(), self.loop).result(TIMEOUT)
            with self._lock:
                self._agents[thread] = agent
        return agent

    def _submit(self, make: Callable[[], object]) -> concurrent.futures.Future[Any]:
        if threading.current_thread() is self._thread:
            msg = "a driver call on the event loop's own thread would deadlock"
            raise AssertionError(msg)
        if _CONSUMER.get() is not None:
            # Inside a stream callback: a task of its own, in the callback's
            # context (``run_coroutine_threadsafe`` copies this thread's).
            async def call() -> object:
                result = make()
                return await result if inspect.isawaitable(result) else result

            return asyncio.run_coroutine_threadsafe(call(), self.loop)
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        agent = self._agent()
        self.loop.call_soon_threadsafe(agent.jobs.put_nowait, (make, future))
        return future

    def call[R](
        self,
        stream: AsyncLogStream[Any],
        make: Callable[[], R],
        what: str,
        timeout: float = TIMEOUT,
    ) -> Any:  # noqa: ANN401  (the call's result, awaited)
        """Run ``make()`` (awaiting what it returns) on the loop, bounded like
        `_bounded`: the stream is killed and the test fails after ``timeout``.
        """
        future = self._submit(make)
        try:
            return future.result(timeout)
        except TimeoutError:
            if future.done():
                raise  # the call's own TimeoutError
        stream.kill()
        with contextlib.suppress(BaseException):
            future.result(TIMEOUT)
        pytest.fail(f"{what} did not return within {timeout} s; the stream was killed")

    def close(self) -> None:
        """Cancel every task still on the loop, then stop and close it."""

        async def shutdown() -> None:
            current = asyncio.current_task()
            tasks = [task for task in asyncio.all_tasks() if task is not current]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=TIMEOUT)
            await self.loop.shutdown_asyncgens()
            # Bounded: a test's callback still blocked in a worker thread
            # keeps that thread past the test.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.loop.shutdown_default_executor(), TIMEOUT)

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(3 * TIMEOUT)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(TIMEOUT)
            if not self._thread.is_alive():
                self.loop.close()

    # -- the Driver interface ---------------------------------------------------------

    def open(
        self,
        config: StreamConfig | None = None,
        /,
        **kwargs: Any,  # noqa: ANN401  (the AsyncLogStream keywords, passed on)
    ) -> Any:  # noqa: ANN401  (an AsyncLogStream, used where a LogStream is)
        """Construct a stream (it spawns nothing); plain callbacks are offloaded."""
        for key in ("on_item", "on_event"):
            if key in kwargs:
                kwargs[key] = _offloaded(kwargs[key])
        return AsyncLogStream(config, **kwargs)

    def start(self, stream: Any) -> None:  # noqa: ANN401
        self.call(stream, stream.start, "start()")

    def stop(self, stream: Any, *, flush: float | None = None) -> None:  # noqa: ANN401
        stream.stop(flush=flush)  # thread-safe (call_soon_threadsafe)

    def kill(self, stream: Any) -> None:  # noqa: ANN401
        stream.kill()

    def join(self, stream: Any, timeout: float | None = TIMEOUT) -> None:  # noqa: ANN401
        """``await join(timeout)``; None waits without a bound of its own."""
        bound = (TIMEOUT if timeout is None else timeout) + TIMEOUT
        self.call(stream, lambda: stream.join(timeout), "join()", bound)

    def wait_running(self, stream: Any, timeout: float | None = TIMEOUT) -> None:  # noqa: ANN401
        """``await wait_running(timeout)``; None waits without a bound of its own."""
        bound = (TIMEOUT if timeout is None else timeout) + TIMEOUT
        self.call(stream, lambda: stream.wait_running(timeout), "wait_running()", bound)

    def iterate(self, stream: Any) -> AsyncPull:  # noqa: ANN401
        """The stream's pull iterator, created (and so owned) by this thread's task."""
        return AsyncPull(self, stream)

    def first_by_break(self, stream: Any) -> Any:  # noqa: ANN401
        """``async for item in stream: break``, by a real loop."""

        async def first() -> Any:  # noqa: ANN401
            async for item in stream:
                return item
            msg = "the iteration ended before the first item"
            raise AssertionError(msg)

        return self.call(stream, first, "the first next()")

    def kill_in_the_loop(self, stream: Any) -> list[Any]:  # noqa: ANN401
        """``async for item in stream: stream.kill()``: the items the body got.

        The kill() is made on the loop, and nothing awaits between it and the
        next step of the loop.
        """

        async def loop() -> list[Any]:
            got: list[Any] = []
            async for item in stream:
                got.append(item)
                stream.kill()
            return got

        return self.call(stream, loop, "the iteration")

    @contextlib.contextmanager
    def session(self, stream: Any) -> Iterator[Any]:  # noqa: ANN401
        """``async with stream:`` in this thread's task; the exit is bounded."""
        self.call(stream, stream.__aenter__, "the with-block entry")
        try:
            yield stream
        except BaseException as exc:
            leave = functools.partial(
                stream.__aexit__, type(exc), exc, exc.__traceback__
            )
            self.call(stream, leave, "the with-block exit")
            raise
        leave = functools.partial(stream.__aexit__, None, None, None)
        self.call(stream, leave, "the with-block exit")

    def refuse_logcat_spawn(
        self, monkeypatch: pytest.MonkeyPatch, error: OSError
    ) -> list[tuple[str, ...]]:
        """Make every ``adb logcat`` spawn raise ``error``; return the argvs tried."""
        tried: list[tuple[str, ...]] = []
        real = adb._async_spawn_adb

        async def spawn(argv: Sequence[str]) -> Any:  # noqa: ANN401
            if "logcat" in argv:
                tried.append(tuple(argv))
                raise error
            return await real(argv)

        monkeypatch.setattr(adb, "_async_spawn_adb", spawn)
        return tried

    def consumer_identity(self) -> object:
        """The consumer context the caller runs in: its task.

        In an offloaded callback, the task that awaits it; on the loop, the
        running task; elsewhere the thread, which no task is.
        """
        task = _CONSUMER.get()
        if task is not None:
            return task
        with contextlib.suppress(RuntimeError):
            running = asyncio.current_task()
            if running is not None:
                return running
        return threading.current_thread()

    def wait_concluded(self, stream: Any) -> None:  # noqa: ANN401
        """Wait until the run concluded: terminal, or waiting in the drain (A.21)."""
        wait_until(lambda: stream._run.concluded, timeout=TIMEOUT)

    def break_reader(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        """Make the stdout reader of every connection raise ``error`` (a logpyt
        bug) at its second read; the first read returns what adb wrote.
        """
        real = cast("Callable[..., Any]", async_stream._Run._read)

        class Broken:
            def __init__(self, reader: Any) -> None:  # noqa: ANN401
                self._reader = reader
                self._reads = 0

            async def read(self, size: int) -> bytes:
                self._reads += 1
                if self._reads == 2:
                    raise error
                return await self._reader.read(size)

        async def read(
            run: Any,  # noqa: ANN401
            connection: int,
            name: str,
            reader: Any,  # noqa: ANN401
            proc: Any,  # noqa: ANN401
        ) -> None:
            wrapped = Broken(reader) if name == "stdout" else reader
            await real(run, connection, name, wrapped, proc)

        monkeypatch.setattr(async_stream._Run, "_read", read)

    def hold_after_delivery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        hold: Callable[[Sequence[object]], None],
    ) -> None:
        """Call ``hold(records)`` after the engine handed records to its consumer
        side, before it takes its next event: in asyncio the engine can lose the
        loop only at an ``await``, and its next one is in `_Run._take`. ``hold``
        runs in a worker thread, so the consumer runs meanwhile. The records
        that end the run go out with the outcome and are not held.
        """
        real_deliver = cast("Callable[..., bool]", async_stream._Run._deliver)
        real_take = cast("Callable[..., Any]", async_stream._Run._take)
        delivered: list[tuple[object, ...]] = []

        def deliver(run: Any, records: tuple[object, ...]) -> bool:  # noqa: ANN401
            changed = real_deliver(run, records)
            delivered.append(records)
            return changed

        async def take(run: Any) -> Any:  # noqa: ANN401
            while delivered:
                await asyncio.to_thread(hold, delivered.pop(0))
            return await real_take(run)

        monkeypatch.setattr(async_stream._Run, "_deliver", deliver)
        monkeypatch.setattr(async_stream._Run, "_take", take)


class AsyncPull(Pull):
    """A pull iterator of an `AsyncLogStream`, stepped by the calling thread's task."""

    def __init__(self, driver: AsyncDriver, stream: AsyncLogStream[Any]) -> None:
        self._driver = driver
        self._async_stream = stream
        self._iterator = driver.call(stream, stream.__aiter__, "aiter()")

    def next(self) -> Any:  # noqa: ANN401
        """The next item; StopIteration at the end."""
        iterator = self._iterator
        try:
            return self._driver.call(
                self._async_stream, lambda: anext(iterator), "next()"
            )
        except StopAsyncIteration:
            raise StopIteration from None

    def rest(self) -> list[Any]:
        """Every item up to the end of the iteration."""
        iterator = self._iterator

        async def rest() -> list[Any]:
            return [item async for item in iterator]

        return self._driver.call(self._async_stream, rest, "iteration")

    def close(self) -> None:
        """Detach the iterator (design 3.5)."""
        self._driver.call(self._async_stream, self._iterator.aclose, "aclose()")


type Driver = SyncDriver | AsyncDriver


@pytest.fixture(params=DRIVERS)
def driver(request: pytest.FixtureRequest) -> Iterator[Driver]:
    """The driver a conformance test runs for (see the module docs)."""
    if request.param == "sync":
        yield SyncDriver()
        return
    on_a_loop = AsyncDriver()
    try:
        yield on_a_loop
    finally:
        on_a_loop.close()
