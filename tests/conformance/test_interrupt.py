"""Ctrl+C conformance: a KeyboardInterrupt reaches a waiting main thread (design 9.2).

Every wait of a user-facing call is sliced to 0.25 s (design 5.3), because on
Windows a blocking lock acquire cannot be interrupted by Ctrl+C.
``_thread.interrupt_main()`` simulates a SIGINT the same way on every
platform: it marks the signal as pending, and the main thread raises
KeyboardInterrupt only when it next runs Python code, so a wait that never
woke up would hold it back on Linux too. Scenario (a) of design 2.11: the
interrupt propagates unchanged and with-exit kills adb (design 3.4), within
1 s of the interrupt. An interrupt that lands in the with-block exit's own
wait kills too: no adb child remains once the with-block was left (design
9.2), and a killed run starts no new ``on_item`` call after it; one that was
running finishes, since the exit waits at most 1 s. A run that had already
ended FAILED is not killed (design A.23): the items it left are still
delivered after the with-block (A.21).

An `AsyncLogStream` has no thread wait for Ctrl+C to land in: under
``asyncio.run`` SIGINT cancels the main task, ``async with`` kills adb on that
cancellation, and ``asyncio.run`` raises KeyboardInterrupt once the task
ended (design 2.11, 3.4). The driver-parametrized tests therefore run for
the thread driver only, and `test_ctrl_c_under_asyncio_run_kills_adb` runs
the asyncio loop in the main thread instead.
"""

from __future__ import annotations

import _thread
import asyncio
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import logpyt
from logpyt import AsyncLogStream
from logpyt.exceptions import StreamError
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, config
from tests.conftest import (
    FakeAdb,
    async_wait_until,
    lines,
    logcat_line,
    out,
    run,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from logpyt import LogEntry

# Design 9.2: the with-block exits within 1 s of the interrupt.
WITHIN = 1.0
# Design 3.4: an interrupted with-exit kills, then joins for at most 1 s; a
# consumer stuck in on_item keeps that join waiting for all of it.
KILL_WAIT = 1.0


_LIBRARY = Path(logpyt.__file__).resolve().parent


def _main_thread_waits() -> bool:
    """Whether the main thread is blocked in a `threading` wait that logpyt called.

    Its innermost Python frame is ``Condition.wait`` (the lock acquire below
    it is C code), and a frame further out is library code, so the interrupt
    lands in the library's wait, not in a wait of the test's own helpers.
    """
    main = threading.main_thread().ident
    frame = sys._current_frames().get(main) if main is not None else None
    if frame is None or frame.f_code.co_name != "wait":
        return False
    if frame.f_code.co_filename != threading.__file__:
        return False
    while frame is not None:
        if Path(frame.f_code.co_filename).resolve().is_relative_to(_LIBRARY):
            return True
        frame = frame.f_back
    return False


# Where the innermost Python frame of a thread whose event loop waits for I/O
# lies: the selector's select() (selectors.py), or the proactor's _poll() on
# Windows (asyncio/windows_events.py); the wait itself is C code below it.
_LOOP_WAITS = frozenset({"selectors.py", "windows_events.py"})


def _main_loop_waits() -> bool:
    """Whether the event loop in the main thread waits for I/O, so every task
    on it, the test body's main task included, is suspended in an await.
    """
    main = threading.main_thread().ident
    frame = sys._current_frames().get(main) if main is not None else None
    return frame is not None and Path(frame.f_code.co_filename).name in _LOOP_WAITS


class _Interrupter:
    """Calls ``_thread.interrupt_main()`` once ``waiting()`` says the main
    thread blocks in a wait (by default a library thread wait).

    It fires at most once, and never after `join` (the end of the test body),
    so a stray KeyboardInterrupt cannot reach pytest itself. ``give_up``, if
    set, runs instead when the main thread never waited, so that the body
    ends (and the test fails on ``sent_at``) instead of waiting forever.
    """

    def __init__(self, waiting: Callable[[], bool] = _main_thread_waits) -> None:
        self.armed = threading.Event()
        self.sent_at: float | None = None
        self.give_up: Callable[[], object] | None = None
        self._waiting = waiting
        self._lock = threading.Lock()
        self._done = False
        self._thread = threading.Thread(
            target=self._run, name="test: interrupter", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        if not self.armed.wait(TIMEOUT):
            return
        try:
            wait_until(lambda: self._done or self._waiting(), timeout=TIMEOUT)
        except TimeoutError:
            if self.give_up is not None:
                self.give_up()
            return  # the test fails on sent_at being None
        with self._lock:
            if self._done:
                return
            self._done = True
            self.sent_at = time.monotonic()
            _thread.interrupt_main()

    def join(self) -> None:
        with self._lock:
            self._done = True
        self.armed.set()
        self._thread.join(TIMEOUT)


def _require_sigint_in_the_main_thread() -> None:
    """Skip a test that cannot interrupt the main thread here."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("interrupt_main() interrupts the main thread only")
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("SIGINT is not handled by Python here: interrupt_main() is a no-op")


@pytest.fixture
def interrupter(driver: Driver) -> _Interrupter:
    """An interrupter for a test that runs in the main thread under SIGINT's default."""
    if driver.name == "async":
        pytest.skip(
            "an async stream has no sliced thread wait for Ctrl+C to land in: "
            "under asyncio.run, SIGINT cancels the main task, and with-exit "
            "kills on that cancellation (design 2.11, 3.4); "
            "test_ctrl_c_under_asyncio_run_kills_adb runs the loop in the "
            "main thread"
        )
    _require_sigint_in_the_main_thread()
    return _Interrupter()


@pytest.mark.parametrize("where", ["iterate", "join", "wait_running"])
def test_ctrl_c_in_a_wait_kills_adb_and_propagates(
    driver: Driver, fake_adb: FakeAdb, interrupter: _Interrupter, where: str
) -> None:
    """Ctrl+C while the main thread waits in ``for entry in stream``, in
    ``join()``, or in ``wait_running()``: KeyboardInterrupt comes out of the
    with-block unchanged within 1 s, the run ends KILLED and no adb is left.
    """
    if where == "wait_running":
        fake_adb.logcat(run(touch("spawned"), hang=True))  # never RUNNING
    else:
        fake_adb.logcat(run(out(logcat_line("a")), touch("spawned"), wait("never")))
    entries: list[LogEntry] = []
    push = where == "join"
    stream = driver.open(config(), on_item=entries.append if push else None)

    def body() -> None:
        with driver.session(stream):
            fake_adb.wait_for("spawned")
            if where == "iterate":
                pull = driver.iterate(stream)
                assert pull.next().message == "a"
                interrupter.armed.set()
                pull.next()  # adb writes nothing more: it waits here
            elif where == "join":
                wait_until(lambda: entries, timeout=TIMEOUT)
                interrupter.armed.set()
                driver.join(stream, timeout=None)
            else:
                interrupter.armed.set()
                driver.wait_running(stream, timeout=None)

    try:
        with pytest.raises(KeyboardInterrupt) as info:
            body()
        ended_at = time.monotonic()
    finally:
        interrupter.join()

    assert interrupter.sent_at is not None, "the main thread never waited"
    assert ended_at - interrupter.sent_at < WITHIN
    assert not getattr(info.value, "__notes__", None)  # unchanged (design 3.4)
    assert stream.state is StreamState.KILLED
    assert fake_adb.survivors() == []


def _script_exit_case(fake_adb: FakeAdb, body: str) -> int:
    """Script adb for `test_ctrl_c_in_the_with_exit_kills`; the lines it writes."""
    if body == "flush":
        # The marker command succeeds, but the marker never reaches the log.
        fake_adb.command("shell", run())
        fake_adb.logcat(run(out(logcat_line("m0")), touch("sent"), wait("never")))
        return 1
    count = 50
    fake_adb.logcat(
        run(lines(count, logcat_line("m{i}")), touch("sent"), wait("never"))
    )
    return count


@pytest.mark.parametrize("body", ["drain", "flush", "raised"])
def test_ctrl_c_in_the_with_exit_kills(
    driver: Driver, fake_adb: FakeAdb, interrupter: _Interrupter, body: str
) -> None:
    """Ctrl+C while the main thread waits in the with-block exit itself: the
    exit kills before KeyboardInterrupt leaves it, so the run ends KILLED, no
    adb is left and no on_item call starts after the with-block (design 9.2,
    3.4).

    drain: the body ended, adb was stopped, and the exit waits for an on_item
    that is stuck with a backlog behind it; flush: the body called
    ``stop(flush=...)`` and the marker never shows, so adb keeps running;
    raised: the body raised, and the exit waits while the consumer may still
    take items (``close_timeout``). A stuck on_item is released only after
    the with-block was left, so a backlog it then took would show in
    ``calls``.
    """
    count = _script_exit_case(fake_adb, body)
    busy = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        busy.set()
        if body != "flush":
            release.wait(TIMEOUT)

    stream = driver.open(config(close_timeout=TIMEOUT), on_item=on_item)
    error = ValueError("the body")

    def run_body() -> None:
        with driver.session(stream):
            assert busy.wait(TIMEOUT)
            fake_adb.wait_for("sent")
            wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
            if body == "flush":
                driver.stop(stream, flush=TIMEOUT)
            interrupter.armed.set()
            if body == "raised":
                raise error

    try:
        with pytest.raises(KeyboardInterrupt) as info:
            run_body()
        ended_at = time.monotonic()
        # Checked while on_item is still stuck: the exit itself killed.
        assert stream.state is StreamState.KILLED
        assert fake_adb.survivors() == []
    finally:
        interrupter.join()
        release.set()
        driver.kill(stream)  # a no-op once KILLED; ends the run of a failed check
    driver.join(stream)

    assert interrupter.sent_at is not None, "the main thread never waited"
    assert ended_at - interrupter.sent_at < WITHIN + KILL_WAIT
    if body == "raised":
        assert info.value.__context__ is error
    assert calls == ["m0"]
    assert "killed" in stream.stats.incomplete


def test_ctrl_c_in_the_exit_of_a_failed_run(
    driver: Driver, fake_adb: FakeAdb, interrupter: _Interrupter
) -> None:
    """Ctrl+C in the with-block exit of a run that already ended FAILED, while
    on_item is stuck with items behind it: the kill does nothing (kill()
    after the end, design A.23), so the run stays FAILED, KeyboardInterrupt
    leaves the block once the exit's 1 s wait is over, no adb is left, and
    the items the run left are delivered after the block (A.21).
    """
    count = 5
    fake_adb.logcat(run(lines(count, logcat_line("m{i}")), exit_code=1))
    busy = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        busy.set()
        release.wait(TIMEOUT)

    stream = driver.open(config(), on_item=on_item)

    def run_body() -> None:
        with driver.session(stream):
            assert busy.wait(TIMEOUT)
            wait_until(lambda: stream.outcome is not None, timeout=TIMEOUT)
            interrupter.armed.set()

    try:
        with pytest.raises(KeyboardInterrupt):
            run_body()
        ended_at = time.monotonic()
        # Checked while on_item is still stuck in its first call.
        assert stream.state is StreamState.FAILED
        assert calls == ["m0"]
        assert fake_adb.survivors() == []
    finally:
        interrupter.join()
        release.set()
    with pytest.raises(StreamError) as info:
        driver.join(stream)

    assert info.value is stream.outcome
    assert interrupter.sent_at is not None, "the main thread never waited"
    assert ended_at - interrupter.sent_at < WITHIN + KILL_WAIT
    assert calls == [f"m{i}" for i in range(count)]
    assert not stream.stats.killed


@pytest.mark.parametrize("where", ["iterate", "join", "wait_running"])
def test_ctrl_c_under_asyncio_run_kills_adb(fake_adb: FakeAdb, where: str) -> None:
    """Scenario (a) of design 2.11 for AsyncLogStream: Ctrl+C while the main
    task of ``asyncio.run`` waits in ``async for``, in ``join()`` or in
    ``wait_running()``. asyncio.run turns SIGINT into a cancellation of the
    main task, ``async with`` kills adb on it (design 3.4), and asyncio.run
    raises KeyboardInterrupt, unchanged and within 1 s of the interrupt
    (design 9.2): the run ends KILLED and no adb is left.

    The interrupt is sent once the loop waits for I/O, so the main task is
    suspended in the stream's wait. Without a real signal, Linux runs the
    handler when the loop next wakes; the exit watcher wakes it every 50 ms
    (design 5.4). On Windows the proactor loop's wakeup fd wakes it at once.
    """
    _require_sigint_in_the_main_thread()
    if where == "wait_running":
        fake_adb.logcat(run(touch("spawned"), hang=True))  # never RUNNING
    else:
        fake_adb.logcat(run(out(logcat_line("a")), touch("spawned"), wait("never")))
    entries: list[LogEntry] = []
    push = where == "join"
    stream = AsyncLogStream(config(), on_item=entries.append if push else None)
    interrupter = _Interrupter(_main_loop_waits)
    interrupter.give_up = stream.kill

    async def body() -> None:
        async with stream:
            await fake_adb.async_wait_for("spawned")
            if where == "iterate":
                iterator = aiter(stream)
                assert (await anext(iterator)).message == "a"
                interrupter.armed.set()
                await anext(iterator)  # adb writes nothing more: it waits here
            elif where == "join":
                await async_wait_until(lambda: entries, timeout=TIMEOUT)
                interrupter.armed.set()
                await stream.join()
            else:
                interrupter.armed.set()
                await stream.wait_running()

    try:
        with pytest.raises(KeyboardInterrupt) as info:
            asyncio.run(body())
        ended_at = time.monotonic()
    finally:
        interrupter.join()

    assert interrupter.sent_at is not None, "the main task never waited"
    assert ended_at - interrupter.sent_at < WITHIN
    assert not getattr(info.value, "__notes__", None)  # unchanged (design 3.4)
    assert stream.state is StreamState.KILLED
    assert fake_adb.survivors() == []
