"""Lifecycle conformance: start, iterate, stop, kill, join, with-exit, single use.

Design 2.3, 3.2-3.5, 4.2 and D-6, for every driver (see ``conftest.py``).
"""

from __future__ import annotations

import errno
import gc
import logging
import threading
import time
from typing import TYPE_CHECKING

import pytest

from logpyt import AdbExitError, Reconnect, StreamError, UsageError
from logpyt._core.machine import StreamCore
from logpyt._core.protocol import Data
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, config
from tests.conftest import (
    FakeAdb,
    err,
    lines,
    logcat_line,
    out,
    run,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from logpyt._core.protocol import Command, Event
    from logpyt.models import LogEntry

S = StreamState


def test_start_iterate_stop(driver: Driver, fake_adb: FakeAdb) -> None:
    """Pull: the lines in order; after stop() the iteration ends; STOPPED, complete."""
    fake_adb.logcat(run(lines(3, logcat_line("m{i}")), wait("never")))
    stream = driver.open(config())
    assert stream.state is S.IDLE

    driver.start(stream)
    pull = driver.iterate(stream)
    first = [pull.next().message for _ in range(3)]
    driver.stop(stream)
    rest = pull.rest()
    driver.join(stream)

    assert first == ["m0", "m1", "m2"]
    assert rest == []
    assert stream.state is S.STOPPED
    assert stream.outcome is None
    stats = stream.stats
    assert (stats.connections, stats.lines, stats.delivered) == (1, 3, 3)
    assert stats.complete
    assert [call["command"] for call in fake_adb.calls()] == ["date", "logcat"]
    assert fake_adb.survivors() == []


def test_with_block_stops_and_joins(driver: Driver, fake_adb: FakeAdb) -> None:
    """A clean with-exit stops adb and waits for the dispatcher (design 3.4)."""
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    entries: list[LogEntry] = []
    stream = driver.open(config(), on_item=entries.append)

    with driver.session(stream):
        driver.wait_running(stream)
        wait_until(lambda: entries, timeout=TIMEOUT)

    assert stream.state is S.STOPPED
    assert [entry.message for entry in entries] == ["a"]
    assert fake_adb.survivors() == []


def test_kill_discards_backlog_and_join_is_prompt(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """kill() while the consumer is busy: the backlog is discarded and counted,
    join() returns without waiting for it, and the end is KILLED, not an error
    (F2-7, D-6).

    This proves the ``DiscardUndelivered`` half of the F2-7 row: the consumer
    is released only after the discard. The kill-flag half (the consumer
    skips what is still queued when it next steps) is
    ``test_consumer.py::test_kill_flag_skip_is_killed``.
    """
    fake_adb.logcat(run(lines(2000, logcat_line("m{i}")), touch("sent"), wait("never")))
    busy = threading.Event()
    release = threading.Event()
    taken: list[str] = []

    def on_item(entry: LogEntry) -> None:
        taken.append(entry.message)
        busy.set()
        release.wait(TIMEOUT)

    stream = driver.open(config(), on_item=on_item)
    driver.start(stream)
    try:
        assert busy.wait(TIMEOUT)
        fake_adb.wait_for("sent")
        wait_until(lambda: stream.stats.lines == 2000, timeout=TIMEOUT)
        driver.kill(stream)
        wait_until(lambda: stream.stats.undelivered > 0, timeout=TIMEOUT)
    finally:
        release.set()
    driver.join(stream)

    assert stream.state is S.KILLED
    assert stream.outcome is None
    assert taken == ["m0"]
    stats = stream.stats
    assert (stats.delivered, stats.undelivered) == (1, 1999)
    assert {"undelivered", "killed"} <= set(stats.incomplete)
    assert fake_adb.survivors() == []


def test_never_iterated_pull_stop_join_returns(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """A pull stream nobody iterated has no consumer: stop(); join() returns
    instead of waiting on its full outbox, and the lines are counted as
    undelivered (design 3.5, A.21).
    """
    fake_adb.logcat(run(lines(50, logcat_line("m{i}")), touch("sent"), wait("never")))
    stream = driver.open(config(buffer_entries=10))
    driver.start(stream)
    fake_adb.wait_for("sent")
    wait_until(lambda: stream.stats.lines >= 10, timeout=TIMEOUT)

    driver.stop(stream)
    driver.join(stream)

    assert stream.state is S.STOPPED
    stats = stream.stats
    assert (stats.lines, stats.delivered, stats.undelivered) == (50, 0, 50)
    assert stats.incomplete == ("undelivered",)
    with pytest.raises(UsageError, match="discarded the rest"):
        driver.iterate(stream)


def test_nonzero_exit_without_reconnect_fails(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """An unrequested end without reconnect is the outcome; join() raises the
    same object every time (design 4.2).
    """
    fake_adb.logcat(run(out(logcat_line("last")), err("boom\n"), exit_code=3))
    stream = driver.open(config())
    driver.start(stream)

    with pytest.raises(AdbExitError) as first:
        driver.join(stream)
    with pytest.raises(AdbExitError) as second:
        driver.join(stream)

    error = first.value
    assert second.value is error
    assert stream.outcome is error
    assert error.returncode == 3
    assert error.stderr == "boom"
    assert "logcat" in error.argv
    assert stream.state is S.FAILED
    assert error.stats.lines == 1
    assert "failed" in stream.stats.incomplete


def test_spawn_failure_fails_and_is_never_retried(
    driver: Driver, fake_adb: FakeAdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A logcat that cannot start ends FAILED at once, even with reconnect (A-12)."""
    del fake_adb  # the clock read still goes to the fake adb
    error = PermissionError(errno.EACCES, "Permission denied")
    tried = driver.refuse_logcat_spawn(monkeypatch, error)
    stream = driver.open(config(reconnect=Reconnect(delay=0.01)))
    driver.start(stream)

    with pytest.raises(AdbExitError, match="could not start adb") as info:
        driver.join(stream)

    assert info.value.returncode is None
    assert info.value.__cause__ is error
    assert len(tried) == 1
    assert stream.stats.connections == 1


def test_missing_adb_fails_at_the_clock_read(driver: Driver, tmp_path: Path) -> None:
    """No adb at all: the first one-shot command cannot start; FAILED, never
    retried (A-12, design A.20).
    """
    missing = tmp_path / "no-such-adb"
    stream = driver.open(config(adb_path=str(missing), reconnect=Reconnect(delay=0.01)))
    driver.start(stream)

    with pytest.raises(AdbExitError, match="could not start adb") as info:
        driver.join(stream)

    assert info.value.returncode is None
    assert stream.stats.connections == 1


def test_join_timeout_raises_timeout_error_while_the_consumer_is_busy(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """join(timeout) expires with the builtin TimeoutError, on time; the run
    goes on and ends once the consumer finished (D-3, design A.21).

    "On time" has a bound generous for a loaded CI runner that still fails a
    timeout honored late.
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    busy = threading.Event()
    release = threading.Event()

    def on_item(entry: LogEntry) -> None:
        del entry
        busy.set()
        release.wait(TIMEOUT)

    stream = driver.open(config(), on_item=on_item)
    driver.start(stream)
    try:
        assert busy.wait(TIMEOUT)
        driver.stop(stream)
        began = time.monotonic()
        with pytest.raises(TimeoutError) as info:
            driver.join(stream, timeout=0.05)
        took = time.monotonic() - began
    finally:
        release.set()
    driver.join(stream)

    assert not isinstance(info.value, StreamError)
    assert took < 1.0
    assert stream.state is S.STOPPED


def test_adb_stderr_is_logged_and_kept_in_the_error(
    driver: Driver, fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture
) -> None:
    """adb stderr is never an entry: it is logged on logpyt.adb and its tail is
    kept in the error (D-9, design 5.9).
    """
    caplog.set_level(logging.WARNING, logger="logpyt.adb")
    fake_adb.logcat(run(err("adb: first\n"), err("adb: second\n"), exit_code=1))
    stream = driver.open(config(stderr_tail=1))
    driver.start(stream)

    with pytest.raises(AdbExitError) as info:
        driver.join(stream)

    assert info.value.stderr == "adb: second"
    logged = [r.getMessage() for r in caplog.records if r.name == "logpyt.adb"]
    assert logged == ["adb: first", "adb: second"]
    assert (stream.stats.stderr_lines, stream.stats.lines) == (2, 0)


def test_start_twice_raises_and_clone_runs_again(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """A stream runs once; clone() is a fresh IDLE stream (design 3.3)."""
    fake_adb.logcat(run(out(logcat_line("once")), wait("never")))
    entries: list[LogEntry] = []
    stream = driver.open(config(), on_item=entries.append)
    with driver.session(stream):
        wait_until(lambda: entries, timeout=TIMEOUT)

    with pytest.raises(UsageError, match=r"runs once - use stream\.clone"):
        driver.start(stream)
    twin = stream.clone()
    assert twin.state is S.IDLE
    assert twin.config is stream.config
    with driver.session(twin):
        wait_until(lambda: len(entries) == 2, timeout=TIMEOUT)

    assert [entry.message for entry in entries] == ["once", "once"]
    assert (stream.state, twin.state) == (S.STOPPED, S.STOPPED)


def test_stop_and_kill_before_start_do_nothing(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """stop() and kill() in IDLE are no-ops; the stream can still run (D-6)."""
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    entries: list[LogEntry] = []
    stream = driver.open(config(), on_item=entries.append)

    driver.stop(stream)
    driver.kill(stream)
    assert stream.state is S.IDLE
    assert fake_adb.calls() == []

    with driver.session(stream):
        wait_until(lambda: entries, timeout=TIMEOUT)
    assert stream.state is S.STOPPED


def test_break_out_of_a_for_loop_detaches(driver: Driver, fake_adb: FakeAdb) -> None:
    """A real ``break`` out of the loop drops the iterator, which detaches at
    once (it is a class with ``__del__``, not a generator): stop(); join()
    returns STOPPED, and the rest is discarded and counted (design 3.4, 3.5).
    """
    count = 50
    fake_adb.logcat(
        run(lines(count, logcat_line("m{i}")), touch("sent"), wait("never"))
    )
    stream = driver.open(config())
    driver.start(stream)
    fake_adb.wait_for("sent")
    wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)

    first = driver.first_by_break(stream)
    driver.stop(stream)
    driver.join(stream)

    assert first.message == "m0"
    assert stream.state is S.STOPPED
    stats = stream.stats
    assert (stats.lines, stats.delivered, stats.undelivered) == (count, 1, count - 1)
    assert stats.incomplete == ("undelivered",)
    assert fake_adb.survivors() == []


@pytest.mark.parametrize("mode", ["pull", "push"])
def test_closed_generator_exit_stops(
    driver: Driver, fake_adb: FakeAdb, mode: str
) -> None:
    """A with-block inside a generator that its caller closes early: the
    GeneratorExit leaves the block like a body exception, not like Ctrl+C, so
    the run ends STOPPED, never KILLED (design 3.4).

    pull: the generator's own iterator detaches at once and the rest is
    discarded and counted, as after a ``break``; push: the dispatcher keeps
    taking items, so every line is delivered. The GeneratorExit itself
    propagates, so the generator closes cleanly.
    """
    count = 50
    fake_adb.logcat(
        run(lines(count, logcat_line("m{i}")), touch("sent"), wait("never"))
    )
    entries: list[LogEntry] = []
    push = mode == "push"
    stream = driver.open(
        config(close_timeout=TIMEOUT), on_item=entries.append if push else None
    )

    def messages() -> Generator[str, None, None]:
        with driver.session(stream):
            fake_adb.wait_for("sent")
            wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
            if push:
                yield "started"
                return
            pull = driver.iterate(stream)
            while True:
                yield pull.next().message

    generator = messages()
    first = next(generator)
    generator.close()

    assert first == ("started" if push else "m0")
    assert stream.state is S.STOPPED
    stats = stream.stats
    if push:
        assert (stats.delivered, stats.undelivered) == (count, 0)
        assert len(entries) == count
        assert stats.complete
    else:
        assert (stats.delivered, stats.undelivered) == (1, count - 1)
        assert stats.incomplete == ("undelivered",)
    assert fake_adb.survivors() == []


def test_dropped_running_stream_kills_adb(driver: Driver, fake_adb: FakeAdb) -> None:
    """A running stream that is garbage-collected kills its adb (design 3.4:
    ``weakref.finalize``).
    """
    fake_adb.logcat(run(out(logcat_line("a")), touch("spawned"), wait("never")))
    stream = driver.open(config())
    driver.start(stream)
    fake_adb.wait_for("spawned")
    assert fake_adb.live_children("logcat")

    del stream
    gc.collect()

    assert fake_adb.survivors() == []


def test_dropped_failed_stream_logs_its_outcome(
    driver: Driver, fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture
) -> None:
    """A stream that ended FAILED and is garbage-collected before anyone saw
    the outcome logs it at ERROR, so the failure is not lost (design 3.4).
    """
    caplog.set_level(logging.ERROR, logger="logpyt.stream")
    fake_adb.logcat(run(err("boom\n"), exit_code=3))

    def run_and_drop() -> None:
        stream = driver.open(config())
        driver.start(stream)
        wait_until(lambda: stream.state is S.FAILED, timeout=TIMEOUT)

    run_and_drop()
    gc.collect()

    def logged() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "logpyt.stream" and r.levelno == logging.ERROR
        ]

    wait_until(logged, timeout=TIMEOUT)
    (message,) = logged()
    assert message.startswith(
        f"{driver.noun} was garbage-collected and its run ended FAILED without "
        "anyone seeing it: AdbExitError: "
    )


@pytest.mark.parametrize("outcome", ["unseen", "seen"])
def test_clean_with_exit_raises_an_unseen_outcome(
    driver: Driver, fake_adb: FakeAdb, outcome: str
) -> None:
    """A with-block whose body finished raises the latched outcome if no one
    has seen it yet; once join() raised it, the exit does not raise it again
    (design 3.4).
    """
    fake_adb.logcat(run(err("boom\n"), exit_code=3))
    stream = driver.open(config())
    seen = outcome == "seen"
    raised: list[AdbExitError] = []

    def body() -> None:
        with driver.session(stream):
            wait_until(lambda: stream.state is S.FAILED, timeout=TIMEOUT)
            if seen:
                with pytest.raises(AdbExitError) as info:
                    driver.join(stream)
                raised.append(info.value)

    if seen:
        body()
        assert raised == [stream.outcome]
    else:
        with pytest.raises(AdbExitError) as info:
            body()
        assert info.value is stream.outcome
    assert stream.state is S.FAILED


def test_body_exception_carries_the_failed_outcome(
    driver: Driver, fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture
) -> None:
    """A body that raised while the run ended FAILED: the body's exception
    propagates unchanged, with the outcome added as a note and logged at
    ERROR with its traceback (design 3.4).
    """
    caplog.set_level(logging.ERROR, logger="logpyt.stream")
    fake_adb.logcat(run(err("boom\n"), exit_code=3))
    stream = driver.open(config())
    error = ValueError("the body")

    def body() -> None:
        with driver.session(stream):
            wait_until(lambda: stream.state is S.FAILED, timeout=TIMEOUT)
            raise error

    with pytest.raises(ValueError, match="the body") as info:
        body()

    outcome = stream.outcome
    assert isinstance(outcome, AdbExitError)
    assert info.value is error
    assert error.__notes__ == [
        f"logpyt: the stream ended FAILED too: AdbExitError: {outcome}"
    ]
    logged = [r for r in caplog.records if r.name == "logpyt.stream" and r.exc_info]
    assert [r.getMessage() for r in logged] == [
        f"the {driver.stream_class} failed while its with-block raised ValueError"
    ]
    assert logged[0].exc_info is not None
    assert logged[0].exc_info[1] is outcome


@pytest.mark.parametrize("kind", ["error", "exit"])
def test_engine_crash_raises_from_every_wait(
    driver: Driver,
    fake_adb: FakeAdb,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kind: str,
) -> None:
    """A logpyt bug that escapes the core ends the run visibly: join() raises
    that same exception every time, so does assert_complete(), the engine
    logs it at ERROR, and no adb is left once join() returned (it is killed
    and reaped first). ``exit``: a BaseException (here SystemExit) that is
    not an Exception, which the engine catches too.

    No driver invents a state (design 5.2): ``state`` stays the last one the
    core published, here CONNECTING, since the crash comes with the first
    output. ``stats`` names the reason ``failed`` (design 4.4: a fixture that
    checks only ``stats.complete`` saves the log of a failed capture).
    """
    caplog.set_level(logging.ERROR, logger="logpyt.stream")
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    error: BaseException = (
        RuntimeError("a core bug") if kind == "error" else SystemExit(7)
    )
    real = StreamCore.handle

    def handle(core: StreamCore, event: Event, now: float) -> list[Command]:
        if isinstance(event, Data):
            raise error
        return real(core, event, now)

    monkeypatch.setattr(StreamCore, "handle", handle)
    stream = driver.open(config())
    driver.start(stream)

    with pytest.raises(type(error)) as info:
        driver.join(stream)
    assert fake_adb.live_children() == []  # checked at once: no wait
    assert info.value is error
    with pytest.raises(type(error)) as info:
        driver.join(stream)
    assert info.value is error
    with pytest.raises(type(error)) as info:
        stream.assert_complete()
    assert info.value is error

    logged = [r for r in caplog.records if r.name == "logpyt.stream" and r.exc_info]
    assert [r.getMessage() for r in logged] == [
        f"the {driver.stream_class} engine failed; the run is abandoned"
    ]
    assert logged[0].exc_info is not None
    assert logged[0].exc_info[1] is error
    assert stream.state is S.CONNECTING
    assert stream.stats.incomplete == ("failed",)
    assert not stream.stats.complete


def test_reader_crash_raises_from_every_wait(
    driver: Driver,
    fake_adb: FakeAdb,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A logpyt bug in a pipe reader ends the run like an engine crash (design
    A.23): join() raises that same exception every time, so does
    assert_complete(), it is logged at ERROR once, and no adb is left once
    join() returned. Before, the reader ended silently, the run went on
    without its stdout, and a stop() ended it STOPPED and complete. ``stats``
    names the reason ``failed`` (design 4.4).
    """
    caplog.set_level(logging.ERROR, logger="logpyt.stream")
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    error = RuntimeError("a reader bug")
    driver.break_reader(monkeypatch, error)
    stream = driver.open(config())
    driver.start(stream)

    with pytest.raises(RuntimeError) as info:
        driver.join(stream)
    assert fake_adb.live_children() == []  # checked at once: no wait
    assert info.value is error
    with pytest.raises(RuntimeError) as info:
        driver.join(stream)
    assert info.value is error
    with pytest.raises(RuntimeError) as info:
        stream.assert_complete()
    assert info.value is error

    logged = [r for r in caplog.records if r.name == "logpyt.stream" and r.exc_info]
    assert len(logged) == 1, [r.getMessage() for r in logged]
    assert logged[0].exc_info is not None
    assert logged[0].exc_info[1] is error
    stats = stream.stats
    assert stats.incomplete == ("failed",)
    assert not stats.complete
