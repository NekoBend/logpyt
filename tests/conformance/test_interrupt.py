"""Ctrl+C conformance: a KeyboardInterrupt reaches a waiting main thread (design 9.2).

Every wait of a user-facing call is sliced to 0.25 s (design 5.3), because on
Windows a blocking lock acquire cannot be interrupted by Ctrl+C.
``_thread.interrupt_main()`` simulates a SIGINT the same way on every
platform: it marks the signal as pending, and the main thread raises
KeyboardInterrupt only when it next runs Python code, so a wait that never
woke up would hold it back on Linux too. Scenario (a) of design 2.11: the
interrupt propagates unchanged and with-exit kills adb (design 3.4), within
1 s of the interrupt. An interrupt that lands in the with-block exit's own
wait kills too, so neither adb nor ``on_item`` outlives the with-block
(design 9.2: no adb child remains).
"""

from __future__ import annotations

import _thread
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import logpyt
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, config
from tests.conftest import (
    FakeAdb,
    lines,
    logcat_line,
    out,
    run,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
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


class _Interrupter:
    """Calls ``_thread.interrupt_main()`` once the main thread blocks in a wait.

    It fires at most once, and never after `join` (the end of the test body),
    so a stray KeyboardInterrupt cannot reach pytest itself.
    """

    def __init__(self) -> None:
        self.armed = threading.Event()
        self.sent_at: float | None = None
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
            wait_until(lambda: self._done or _main_thread_waits(), timeout=TIMEOUT)
        except TimeoutError:
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


@pytest.fixture
def interrupter() -> _Interrupter:
    """An interrupter for a test that runs in the main thread under SIGINT's default."""
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("interrupt_main() interrupts the main thread only")
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("SIGINT is not handled by Python here: interrupt_main() is a no-op")
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
