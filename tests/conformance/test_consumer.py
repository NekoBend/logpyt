"""Consumer conformance: the end of a run and its consumer (design A.21).

The consumer contract of ``logpyt._core.protocol`` as a user sees it, for
every driver (see ``conftest.py``): a run that would end STOPPED waits until
the attached consumer finished every record (the drain), a consumer whose
callback raised takes nothing more, a failure the outcome cannot take is
logged, and kill() ends a run whatever the consumer does. A.21 also lists
``test_never_iterated_pull_stop_join_returns`` (in ``test_lifecycle.py``) and
the async-only ``test_anext_cancel_in_on_event_settles`` and
``test_anext_cancel_at_checkpoint_loses_nothing``, which come with the async
driver (PR-13).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

import pytest

from logpyt import CallbackError, LogEntry, StateChange
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, Pull, config
from tests.conftest import (
    FakeAdb,
    device_date,
    lines,
    logcat_line,
    out,
    run,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from logpyt import StreamEvent
    from logpyt.streams import LogStream

S = StreamState
_DISCARD_FAILED = (
    "discarding undelivered items (the consumer failed); they are counted in "
    "stats.undelivered"
)


def _adb_ended(fake: FakeAdb) -> None:
    """Wait until every fake adb logcat has exited and its pipes were closed.

    The stream's readers close a pipe at its EOF (X-5), so the connection end
    is then on its way to the core: a run that stop() ends enters the drain.
    """

    def ended() -> bool:
        children = fake.children("logcat")
        return bool(children) and all(
            not child.alive()
            and all(
                p is None or p.closed for p in (child.popen.stdout, child.popen.stderr)
            )
            for child in children
        )

    wait_until(ended, timeout=TIMEOUT)


def _ended(stream: LogStream[LogEntry]) -> bool:
    return stream.state in {S.STOPPED, S.KILLED, S.FAILED}


def test_last_item_failure_after_exit_fails(driver: Driver, fake_adb: FakeAdb) -> None:
    """stop(), then adb's end, then on_item raises on the last item: FAILED.

    The run does not end STOPPED while its consumer has items in hand, so a
    failure on the last item counts however late it comes (design A.21, G4).
    """
    fake_adb.logcat(run(lines(3, logcat_line("m{i}")), wait("never")))
    busy = threading.Event()
    release = threading.Event()

    def on_item(entry: LogEntry) -> None:
        if entry.message == "m2":
            busy.set()
            release.wait(TIMEOUT)
            msg = "the last item"
            raise ValueError(msg)

    stream = driver.open(config(), on_item=on_item)
    driver.start(stream)
    try:
        assert busy.wait(TIMEOUT)
        driver.stop(stream)
        _adb_ended(fake_adb)
        assert stream.state is S.STOPPING  # the drain waits for the consumer
    finally:
        release.set()

    with pytest.raises(CallbackError) as info:
        driver.join(stream)

    assert info.value.stage == "on_item"
    assert isinstance(info.value.__cause__, ValueError)
    assert stream.state is S.FAILED
    assert stream.outcome is info.value
    stats = stream.stats
    assert (stats.lines, stats.delivered, stats.undelivered) == (3, 3, 0)
    assert "failed" in stats.incomplete


@pytest.mark.parametrize("stage", ["on_item", "on_event", "pull_on_event"])
def test_failed_consumer_takes_nothing_more(
    driver: Driver, fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    """After its callback raised, the consumer calls no callback again, the
    terminal change included; what it did not take is discarded and counted
    (fail fast, design 2.4, A.21). A pull iterator raises the outcome once,
    then ends.
    """
    caplog.set_level(logging.WARNING, logger="logpyt.stream")
    count = 10
    fake_adb.logcat(run(lines(count, logcat_line("m{i}")), wait("never")))
    calls: list[str] = []
    stream: LogStream[LogEntry]

    def raise_once_all_read() -> None:
        wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
        msg = "boom"
        raise ValueError(msg)

    def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        if stage == "on_item" and entry.message == "m3":
            raise_once_all_read()

    def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange):
            calls.append(f"{event.old}->{event.new}")
            if stage != "on_item" and event.new is S.RUNNING:
                raise_once_all_read()

    if stage == "pull_on_event":
        stream = driver.open(config(), on_event=on_event)
        driver.start(stream)
        pull = driver.iterate(stream)
        with pytest.raises(CallbackError) as info:
            pull.next()
        with pytest.raises(StopIteration):
            pull.next()
        with pytest.raises(CallbackError) as again:
            driver.join(stream)
        assert again.value is info.value  # the same outcome object (design 4.2)
    else:
        stream = driver.open(config(), on_item=on_item, on_event=on_event)
        driver.start(stream)
        with pytest.raises(CallbackError) as info:
            driver.join(stream)

    # Nothing after the failure: no item, no STOPPING or FAILED change.
    taken = ["m0", "m1", "m2", "m3"] if stage == "on_item" else []
    assert calls == ["IDLE->CONNECTING", "CONNECTING->RUNNING", *taken]
    assert info.value.stage == ("on_item" if stage == "on_item" else "on_event")
    stats = stream.stats
    assert (stats.lines, stats.delivered, stats.undelivered) == (
        count,
        len(taken),
        count - len(taken),
    )
    warned = [r.getMessage() for r in caplog.records if r.name == "logpyt.stream"]
    assert warned.count(_DISCARD_FAILED) == 1, warned
    assert stream.state is S.FAILED
    assert fake_adb.survivors() == []


@pytest.mark.parametrize("end", [S.STOPPED, S.KILLED], ids=["stopped", "killed"])
def test_terminal_on_event_failure_is_logged(
    driver: Driver,
    fake_adb: FakeAdb,
    caplog: pytest.LogCaptureFixture,
    end: StreamState,
) -> None:
    """on_event raising on the terminal change cannot become the outcome: the
    run has ended. The failure is logged at ERROR with its traceback, before
    join() returns, and join() does not raise it (design A.21, 4.2).
    """
    caplog.set_level(logging.ERROR, logger="logpyt.stream")
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    error = RuntimeError("terminal hook")
    entries: list[LogEntry] = []

    def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange) and event.new is end:
            raise error

    stream = driver.open(config(), on_item=entries.append, on_event=on_event)
    driver.start(stream)
    wait_until(lambda: entries, timeout=TIMEOUT)
    if end is S.STOPPED:
        driver.stop(stream)
    else:
        driver.kill(stream)
    driver.join(stream)

    assert stream.state is end
    assert stream.outcome is None
    logged = [r for r in caplog.records if r.name == "logpyt.stream" and r.exc_info]
    assert len(logged) == 1, [r.getMessage() for r in caplog.records]
    record = logged[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is error
    assert record.getMessage() == (
        f"on_event raised RuntimeError: terminal hook after the run ended {end}; "
        "the outcome is unchanged"
    )


def test_pull_body_exception_detaches(driver: Driver, fake_adb: FakeAdb) -> None:
    """A loop body that raises: with-exit detaches the exiting thread's
    iterator at once, so the run ends STOPPED without waiting close_timeout,
    the rest is discarded and counted, and the body's exception propagates
    unchanged (design 3.4, 3.5, A.21).

    A kept iterator is not iterated any more; had with-exit waited for it,
    it would have killed the run after ``close_timeout`` (KILLED).
    """
    count = 50
    fake_adb.logcat(
        run(lines(count, logcat_line("m{i}")), touch("sent"), wait("never"))
    )
    stream = driver.open(config(buffer_entries=10, close_timeout=TIMEOUT))
    error = ValueError("the body")

    pulls: list[Pull] = []

    def body() -> None:
        with driver.session(stream):
            pull = driver.iterate(stream)
            pulls.append(pull)  # kept: only with-exit can detach it
            assert pull.next().message == "m0"
            fake_adb.wait_for("sent")
            raise error

    began = time.monotonic()
    with pytest.raises(ValueError, match="the body") as info:
        body()
    lasted = time.monotonic() - began
    (pull,) = pulls

    assert info.value is error
    assert not getattr(error, "__notes__", None)  # the run did not fail
    assert stream.state is S.STOPPED
    assert lasted < TIMEOUT
    stats = stream.stats
    assert (stats.lines, stats.delivered, stats.undelivered) == (count, 1, count - 1)
    assert stats.incomplete == ("undelivered",)
    with pytest.raises(StopIteration):
        pull.next()
    assert fake_adb.survivors() == []


@pytest.mark.parametrize("phase", ["clock", "spawned"])
def test_wait_running_owner_stop_before_running(
    driver: Driver, fake_adb: FakeAdb, phase: str
) -> None:
    """The owner of an attached iterator stops the stream before RUNNING; its
    wait_running() returns at the run's conclusion instead of waiting for its
    own iterator, which the drain waits for (design A.21, 3.5).

    clock: stopped while the clock read runs, so no adb logcat is spawned;
    spawned: adb logcat runs but has printed nothing.
    """
    if phase == "clock":
        fake_adb.date(run(touch("reading"), wait("clock"), out(device_date(0))))
        signal = "reading"
    else:
        fake_adb.logcat(run(touch("spawned"), hang=True))
        signal = "spawned"
    stream = driver.open(config())
    driver.start(stream)
    try:
        pull = driver.iterate(stream)
        fake_adb.wait_for(signal)
        driver.stop(stream)
        driver.wait_running(stream)
        assert stream.state is S.STOPPING  # the drain waits for the iterator
        assert pull.rest() == []
    finally:
        fake_adb.release("clock")
    driver.join(stream)

    assert stream.state is S.STOPPED
    assert stream.stats.lines == 0
    expected = [] if phase == "clock" else ["logcat"]
    assert [call["command"] for call in fake_adb.calls("logcat")] == expected
    assert fake_adb.survivors() == []


@pytest.mark.parametrize("how", ["kill", "with_exit"])
def test_kill_ends_a_stuck_drain(driver: Driver, fake_adb: FakeAdb, how: str) -> None:
    """A consumer stuck in its callback keeps a stopped run in the drain; kill()
    ends it KILLED at once and discards the rest, whatever the consumer does
    (design A.21, 3.4). with_exit: a body that raised kills it once the
    consumer took nothing for ``close_timeout``.

    join() still waits for the stuck callback to return (the consumer's
    detach), so the test releases it once the run is KILLED.
    """
    fake_adb.logcat(run(lines(3, logcat_line("m{i}")), touch("sent"), wait("never")))
    busy = threading.Event()
    release = threading.Event()
    taken: list[str] = []

    def on_item(entry: LogEntry) -> None:
        taken.append(entry.message)
        busy.set()
        release.wait(TIMEOUT)

    stream = driver.open(config(close_timeout=0.2), on_item=on_item)
    try:
        if how == "kill":
            driver.start(stream)
            assert busy.wait(TIMEOUT)
            fake_adb.wait_for("sent")
            wait_until(lambda: stream.stats.lines == 3, timeout=TIMEOUT)
            driver.stop(stream)
            _adb_ended(fake_adb)
            assert stream.state is S.STOPPING
            driver.kill(stream)
        else:
            error = RuntimeError("the body")

            def body() -> None:
                with driver.session(stream):
                    assert busy.wait(TIMEOUT)
                    fake_adb.wait_for("sent")
                    wait_until(lambda: stream.stats.lines == 3, timeout=TIMEOUT)
                    raise error

            with pytest.raises(RuntimeError) as info:
                body()
            assert info.value is error
        wait_until(lambda: _ended(stream), timeout=TIMEOUT)
        assert taken == ["m0"]  # still stuck in m0
        assert stream.state is S.KILLED
    finally:
        release.set()
    driver.join(stream)

    assert taken == ["m0"]
    assert stream.outcome is None
    stats = stream.stats
    assert (stats.lines, stats.delivered, stats.undelivered) == (3, 1, 2)
    assert {"undelivered", "killed"} <= set(stats.incomplete)
    assert fake_adb.survivors() == []


@pytest.mark.parametrize("phase", ["live", "drain"])
def test_kill_flag_skip_is_killed(
    driver: Driver, fake_adb: FakeAdb, phase: str
) -> None:
    """kill() from on_item while items are queued: under the kill flag the
    consumer takes none of them, calls no on_item again, and the run ends
    KILLED, never STOPPED and complete (F2-7, design A.21).

    live: killed while adb runs; drain: after stop(), once adb ended, so the
    run waits for the consumer when the kill comes. Nothing is settled past
    the kill, so only the kill can end the run (protocol module docs).
    ``test_kill_flag_skips_count_undelivered`` checks the counters and the
    WARNING.
    """
    count = 200
    fake_adb.logcat(
        run(lines(count, logcat_line("m{i}")), touch("sent"), wait("never"))
    )
    proceed = threading.Event()
    calls: list[str] = []
    changes: list[tuple[StreamState, StreamState]] = []
    stream: LogStream[LogEntry]

    def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        proceed.wait(TIMEOUT)
        stream.kill()

    def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange):
            changes.append((event.old, event.new))

    stream = driver.open(config(), on_item=on_item, on_event=on_event)
    driver.start(stream)
    try:
        fake_adb.wait_for("sent")
        wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
        if phase == "drain":
            driver.stop(stream)
            _adb_ended(fake_adb)
            assert stream.state is S.STOPPING
    finally:
        proceed.set()
    driver.join(stream)

    assert calls == ["m0"]
    assert stream.state is S.KILLED
    assert stream.outcome is None
    assert changes[-1][1] is S.KILLED
    stats = stream.stats
    assert (stats.delivered, stats.undelivered) == (1, count - 1)
    assert "killed" in stats.incomplete
    assert not stats.complete
    assert fake_adb.survivors() == []


_DISCARD_KILLED = (
    "discarding undelivered items (kill()); they are counted in stats.undelivered"
)


@pytest.mark.parametrize("mode", ["live", "drain", "pull"])
def test_kill_flag_skips_count_undelivered(
    driver: Driver, fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    """kill() from the consumer while items are queued: the queued items are
    discarded, counted in ``undelivered`` and announced by the discard
    WARNING, as when another thread kills (design 2.5, 3.4, 4.4).

    live and drain as in `test_kill_flag_skip_is_killed`; pull: kill() from
    the loop body. Before PR-12 settled it, the consumer popped and skipped
    the whole queue right after its kill(), before the engine took the
    request, so the split was (count, 0) and no WARNING came.
    """
    caplog.set_level(logging.WARNING, logger="logpyt.stream")
    count = 200
    fake_adb.logcat(
        run(lines(count, logcat_line("m{i}")), touch("sent"), wait("never"))
    )
    proceed = threading.Event()
    calls: list[str] = []
    stream: LogStream[LogEntry]

    def on_item(entry: LogEntry) -> None:
        calls.append(entry.message)
        proceed.wait(TIMEOUT)
        stream.kill()

    if mode == "pull":
        stream = driver.open(config())
        driver.start(stream)
        fake_adb.wait_for("sent")
        wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
        calls.extend(entry.message for entry in driver.kill_in_the_loop(stream))
    else:
        stream = driver.open(config(), on_item=on_item)
        driver.start(stream)
        try:
            fake_adb.wait_for("sent")
            wait_until(lambda: stream.stats.lines == count, timeout=TIMEOUT)
            if mode == "drain":
                driver.stop(stream)
                _adb_ended(fake_adb)
        finally:
            proceed.set()
    driver.join(stream)

    assert calls == ["m0"]
    assert stream.state is S.KILLED
    stats = stream.stats
    assert (stats.delivered, stats.undelivered) == (1, count - 1)
    assert "undelivered" in stats.incomplete
    warned = [r.getMessage() for r in caplog.records if r.name == "logpyt.stream"]
    assert warned.count(_DISCARD_KILLED) == 1, warned
