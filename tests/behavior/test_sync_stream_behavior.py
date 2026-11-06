"""Behavior of the thread-based ``LogStream`` against a real fake-adb child.

These tests pin what a user of the public API relies on (constructor
arguments, start/stop/kill/join, callbacks, events, exceptions), so they must
survive an internal rewrite: no private attributes, no mocked internals, only
the ``fake_adb`` fixture from ``tests/conftest.py``.

They were written against the 2.x ``LogStream`` and are ported to the 3.0 API
(design 8.3): ``StreamConfig`` (D-1), one-argument ``on_item`` and ``on_event``
callbacks instead of ``stdout_callback(entry, handle)`` and the
``on_start``/``on_stop``/``on_state`` hooks (design 2.4, D-2), and the
exceptions of design 4.1. Each test keeps the observable behavior it pinned;
where 3.0 changes that behavior on purpose, the test asserts the new behavior
and its docstring or a comment names the design section. The 2.0.x defects
these tests were marked ``xfail`` for (F1-1, F1-2, X-1, X-4, F2-4, F2-1,
F4-2 of ``.docs/review-findings-2.0.1.md``) are fixed by the 3.0 driver, so
their markers are gone (design 8.3). The fake adb prints the live wire format
(``logcat_line``, design 5.6).

A precondition (``_require``), a harness timeout, and a lifecycle call that
does not return (``_bounded``) fail the test outright with ``pytest.fail``.

Every lifecycle call (start/stop/kill/join, the ``with`` block) runs through
``_bounded``. A deadlock in the library then fails its test within a few
seconds instead of waiting for the hang watchdog to end the whole run.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

from logpyt import (
    AdbExitError,
    GroupBy,
    LogStream,
    ReadTimeoutError,
    Reconnect,
    StateChange,
    StreamConfig,
    StreamError,
    UsageError,
    Where,
)
from logpyt.streams._events import StreamState
from tests.conftest import (
    FakeAdb,
    err,
    lines,
    logcat_line,
    out,
    run,
    sleep,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from logpyt import StreamEvent
    from logpyt.models import LogEntry, LogGroup

S = StreamState

# Upper bound for every wait, join and lifecycle call. A healthy run finishes
# far sooner; the bound only exists so a regression fails the test instead of
# hanging CI.
TIMEOUT = 10.0

# join(timeout=TIMEOUT) itself runs under a bound this much longer, so a join()
# that ignores its own timeout still fails the test.
_JOIN_SLACK = 5.0

_ENDED = frozenset({S.STOPPED, S.KILLED, S.FAILED})

_POSIX_SIGNALS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs POSIX signals: on Windows terminate() is kill() and "
    "SIGTERM cannot be ignored",
)


class _Collector:
    """An ``on_item`` callback that records every entry it receives."""

    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    def __call__(self, entry: LogEntry) -> None:
        self.entries.append(entry)

    def messages(self) -> list[str]:
        return [entry.message for entry in list(self.entries)]


class _Call[T]:
    """A call running on its own daemon thread, collected with a bound.

    A library call that deadlocks then fails the test (``pytest.fail``)
    instead of blocking the test thread. The stuck thread is left behind; the
    fake_adb finalizer kills the adb child it may be waiting on.
    """

    def __init__(self, call: Callable[[], T], what: str) -> None:
        self.what = what
        self._results: list[T] = []
        self._errors: list[BaseException] = []
        self._thread = threading.Thread(
            target=self._run, args=(call,), name=f"test: {what}", daemon=True
        )
        self._thread.start()

    def _run(self, call: Callable[[], T]) -> None:
        try:
            self._results.append(call())
        except BaseException as exc:  # noqa: BLE001 (re-raised by result() in the test thread)
            self._errors.append(exc)

    def result(self, timeout: float = TIMEOUT) -> T:
        """Return the call's result (or raise its exception) within ``timeout``."""
        self._thread.join(timeout)
        if self._thread.is_alive():
            pytest.fail(f"{self.what} did not return within {timeout} s")
        if self._errors:
            raise self._errors[0]
        return self._results[0]


def _bounded[T](call: Callable[[], T], what: str, timeout: float = TIMEOUT) -> T:
    """Run ``call`` on a helper thread; fail the test unless it returns in time."""
    return _Call(call, what).result(timeout)


def _start(stream: LogStream[LogEntry] | LogStream[LogGroup]) -> None:
    _bounded(stream.start, "start()")


def _stop(stream: LogStream[LogEntry]) -> None:
    _bounded(stream.stop, "stop()")


def _kill(stream: LogStream[LogEntry]) -> None:
    _bounded(stream.kill, "kill()")


def _require(condition: object, message: str) -> None:
    """Fail the test outright when a precondition does not hold."""
    if not condition:
        pytest.fail(message)


def _join(stream: LogStream[LogEntry] | LogStream[LogGroup]) -> StreamError | None:
    """Join within ``TIMEOUT``; return the outcome join() raised, or None.

    join()'s own expiry is the builtin TimeoutError (D-3) and fails the test,
    so a hung stream is never mistaken for a stream error.
    """
    try:
        _bounded(lambda: stream.join(timeout=TIMEOUT), "join()", TIMEOUT + _JOIN_SLACK)
    except TimeoutError as exc:
        pytest.fail(f"join() did not finish within {TIMEOUT} s ({exc!r})")
    except StreamError as exc:
        return exc
    return None


def _teardown(stream: LogStream[LogEntry] | LogStream[LogGroup]) -> None:
    """Kill the stream and wait for it, so no thread or child outlives a test.

    Both are no-ops before start() (D-6), where join() raises UsageError.
    """
    _bounded(stream.kill, "kill()")
    with contextlib.suppress(StreamError, UsageError):
        _bounded(
            lambda: stream.join(timeout=TIMEOUT),
            "join() after kill()",
            TIMEOUT + _JOIN_SLACK,
        )


def _missing(received: list[str], expected: list[str]) -> str:
    """Describe which expected messages did not arrive (for assert messages)."""
    seen = set(received)
    head = [i for i, message in enumerate(expected) if message not in seen][:3]
    return f"{len(received)}/{len(expected)} delivered; first missing indexes {head}"


def _changes(events: list[StreamEvent | str]) -> list[object]:
    """The state changes as ``(old, new)``, other records unchanged."""
    return [(e.old, e.new) if isinstance(e, StateChange) else e for e in events]


# -- delivery and framing -------------------------------------------------


def test_every_stdout_line_reaches_the_callback_in_order(fake_adb: FakeAdb) -> None:
    """Every line adb writes to stdout reaches on_item once, in order."""
    count = 2000
    fake_adb.logcat(run(lines(count, logcat_line("{i}")), wait("done")))
    collector = _Collector()
    stream = LogStream(on_item=collector)

    _start(stream)
    try:
        with contextlib.suppress(TimeoutError):
            wait_until(lambda: len(collector.entries) >= count, timeout=TIMEOUT)
    finally:
        _teardown(stream)

    expected = [str(i) for i in range(count)]
    received = collector.messages()
    assert received == expected, _missing(received, expected)


def test_no_line_is_lost_when_adb_exits_before_the_stream_read_it_all(
    fake_adb: FakeAdb,
) -> None:
    """Every line adb wrote is delivered even when adb exits right after writing.

    2.x took a user parser costing ~2 ms per line to keep adb's output waiting
    in the pipe; 3.0 owns the parser (U-1), so a ``where`` holds the engine
    at the first line until adb has exited. adb writes 20 lines 10 ms apart,
    which the stream's reader takes one by one until its data inbox (16
    reads) is full and it waits, then 40 lines at once, which stay in the
    pipe until adb has exited. 60 lines fit in a pipe on every platform, so
    adb can always exit.

    3.0 ends a run that adb ended with exit status 0 FAILED (design 3.2,
    "rc 0 included": a follow-mode logcat never ends by itself), so join()
    raises AdbExitError(returncode=0) after the last line.
    """
    first = [
        step for i in range(20) for step in (out(logcat_line(f"{i}")), sleep(0.01))
    ]
    fake_adb.logcat(run(*first, lines(40, logcat_line("b{i}")), exit_code=0))
    exited = threading.Event()

    def held_until_adb_exited(entry: LogEntry) -> bool:
        del entry
        if not exited.is_set():
            wait_until(lambda: not fake_adb.live_children("logcat"), timeout=TIMEOUT)
            exited.set()
        return True

    collector = _Collector()
    stream = LogStream(where=Where(held_until_adb_exited), on_item=collector)

    _start(stream)
    try:
        error = _join(stream)
    finally:
        _teardown(stream)

    _require(exited.is_set(), "adb did not exit while the stream held its output")
    _require(
        isinstance(error, AdbExitError) and error.returncode == 0,
        f"join() raised {error!r}, not AdbExitError(returncode=0)",
    )
    expected = [str(i) for i in range(20)] + [f"b{i}" for i in range(40)]
    received = collector.messages()
    assert received == expected, _missing(received, expected)


def test_a_bare_cr_inside_a_message_does_not_forge_a_second_entry(
    fake_adb: FakeAdb,
) -> None:
    """A bare CR inside a message keeps the whole line one entry; nothing is forged.

    The message keeps its text after the CR, so truncating at the CR does not
    pass either.
    """
    forged = logcat_line(
        "FATAL EXCEPTION: main", pid=1000, level="E", tag="AndroidRuntime"
    ).rstrip("\n")
    fake_adb.logcat(
        run(
            out(logcat_line(f"hi\r{forged}", pid=4321, tag="Evil")),
            out(logcat_line("after", tag="Next")),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = LogStream(on_item=collector)

    _start(stream)
    try:
        wait_until(lambda: "after" in collector.messages(), timeout=TIMEOUT)
    finally:
        _teardown(stream)

    assert [(e.pid, e.tag, e.message) for e in collector.entries] == [
        (4321, "Evil", f"hi\r{forged}"),
        (1234, "Next", "after"),
    ]


def test_crlf_line_endings_yield_messages_without_the_cr(fake_adb: FakeAdb) -> None:
    """Lines ending in CRLF are delivered as single entries whose message has no CR.

    The last line is a sentinel: once it arrived, every entry the earlier lines
    produced has arrived too, so an extra (empty) entry per CR cannot hide.
    """
    fake_adb.logcat(
        run(
            out(logcat_line("first").replace("\n", "\r\n")),
            out(logcat_line("second").replace("\n", "\r\n")),
            out(logcat_line("last", tag="End").replace("\n", "\r\n")),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = LogStream(on_item=collector)

    _start(stream)
    try:
        wait_until(
            lambda: any(e.tag == "End" for e in list(collector.entries)),
            timeout=TIMEOUT,
        )
    finally:
        _teardown(stream)

    assert collector.messages() == ["first", "second", "last"]


def test_stderr_lines_are_logged_not_delivered(
    fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture
) -> None:
    """adb's stderr lines never reach the item callback.

    3.0 has no ``stderr_callback``: adb stderr is never parsed or delivered as
    entries (D-9); each line is logged on ``logpyt.adb`` and counted in
    ``stats.stderr_lines`` (design 5.9), which is what this test now checks
    in its place.
    """
    caplog.set_level(logging.WARNING, logger="logpyt.adb")
    fake_adb.logcat(
        run(
            err("error: device offline\n"),
            err("second error\n"),
            out(logcat_line("on stdout")),
            wait("done"),
        )
    )
    stdout_entries = _Collector()
    stream = LogStream(on_item=stdout_entries)

    _start(stream)
    try:
        # Wait for the last line of each pipe: anything misrouted would have
        # been dispatched by then.
        wait_until(
            lambda: (
                stream.stats.stderr_lines == 2
                and "on stdout" in stdout_entries.messages()
            ),
            timeout=TIMEOUT,
        )
    finally:
        _teardown(stream)

    assert stdout_entries.messages() == ["on stdout"]
    logged = [r.getMessage() for r in caplog.records if r.name == "logpyt.adb"]
    assert logged == ["error: device offline", "second error"]


def test_a_50k_burst_is_delivered_in_order_when_the_queue_can_hold_it(
    fake_adb: FakeAdb,
) -> None:
    """A 50,000-line dump reaches a fast callback completely and in order.

    The outbox (``buffer_entries``, 2.x ``max_queue_size``) is sized to hold
    the whole burst, so this pins reading and dispatch at volume, not
    backpressure; X-1 has its own test below. adb stays alive after the burst
    so the end of the connection plays no part here.
    """
    count = 50_000
    fake_adb.logcat(run(lines(count, logcat_line("{i}")), wait("done")))
    collector = _Collector()
    stream = LogStream(StreamConfig(buffer_entries=count), on_item=collector)

    _start(stream)
    try:
        with contextlib.suppress(TimeoutError):
            wait_until(lambda: len(collector.entries) >= count, timeout=TIMEOUT)
    finally:
        _teardown(stream)

    received = collector.messages()
    expected = [str(i) for i in range(count)]
    assert received == expected, _missing(received, expected)


def test_no_entry_of_a_burst_is_lost_while_the_callback_is_behind(
    fake_adb: FakeAdb,
) -> None:
    """With the default buffers, a burst is delivered in full while the callback lags.

    adb writes one line, and the callback stalls on it before adb writes
    anything more. The callback stays stalled until adb has written all of a
    20,000-line burst, or until no further chunk of the burst was written for
    ``held_back_after`` seconds: a stream that holds adb back (backpressure)
    stops adb's progress, one that drops has overflowed by then. Only
    complete, in-order delivery is pinned (X-1).

    The stall resume keeps its default: should intake stay closed for
    ``stall_timeout``, the resumed connection's replay of the scripted run is
    dropped (design 5.6), so the burst still comes once. The ``end`` lines
    are written after the callback resumed; they only tell the test that
    delivery has settled.
    """
    chunks, chunk = 20, 1_000
    held_back_after = 2.0
    stall_bound = 6 * TIMEOUT  # resume is always set in the finally below
    burst = [
        step
        for k in range(chunks)
        for step in (
            lines(chunk, logcat_line(f"{k}:{{i}}", tag="Burst")),
            touch(f"chunk {k}"),
        )
    ]
    endings = [
        step
        for k in range(20)
        for step in (out(logcat_line(f"end {k}", tag="End")), sleep(0.05))
    ]
    fake_adb.logcat(
        run(
            out(logcat_line("stall here", tag="Stall")),
            wait("burst"),
            *burst,
            wait("resumed"),
            *endings,
            wait("done"),
        )
    )
    received: list[tuple[str, str]] = []
    stalled = threading.Event()
    resume = threading.Event()
    settled = threading.Event()

    def lagging(entry: LogEntry) -> None:
        if entry.tag == "Stall":
            stalled.set()
        resume.wait(stall_bound)
        received.append((entry.tag, entry.message))
        if entry.tag == "End":
            settled.set()

    stream = LogStream(StreamConfig(), on_item=lagging)

    _start(stream)
    try:
        _require(stalled.wait(TIMEOUT), "the callback never received the first line")
        fake_adb.release("burst")
        for k in range(chunks):
            try:
                fake_adb.wait_for(f"chunk {k}", timeout=held_back_after)
            except TimeoutError:
                break  # adb is held back: the stream applies backpressure
        resume.set()
        fake_adb.release("resumed")
        settled_in_time = settled.wait(TIMEOUT)
    finally:
        resume.set()
        _teardown(stream)

    expected = [f"{k}:{i}" for k in range(chunks) for i in range(chunk)]
    delivered = [message for tag, message in received if tag == "Burst"]
    assert delivered == expected, _missing(delivered, expected)
    assert settled_in_time, "no line written after the burst reached the callback"


def test_a_line_longer_than_the_read_cap_is_not_split_into_entries(
    fake_adb: FakeAdb,
) -> None:
    """A 9 MiB line yields one entry, not several (X-4).

    3.0 keeps the first ``max_line_bytes`` (64 KiB) of it and flags the entry
    ``truncated`` (design 5.5); no fragment of the rest becomes an entry.
    """
    header = logcat_line("").rstrip("\n")
    fake_adb.logcat(
        run(
            out(header),
            lines(9 * 1024, "x" * 1024),
            out("\n"),
            out(logcat_line("after", tag="Next")),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = LogStream(on_item=collector)

    _start(stream)
    try:
        wait_until(lambda: "after" in collector.messages(), timeout=TIMEOUT)
    finally:
        _teardown(stream)

    assert [e.tag for e in collector.entries] == ["Tag", "Next"]
    assert [e.truncated for e in collector.entries] == [True, False]


def _grouped_lines() -> list[str]:
    """Two pids interleaved, then a line one second later that expires both."""
    return [
        logcat_line("a1", pid=1, stamp="2026-11-04 03:04:05.000000 +0000"),
        logcat_line("b1", pid=2, stamp="2026-11-04 03:04:05.001000 +0000"),
        logcat_line("a2", pid=1, stamp="2026-11-04 03:04:05.002000 +0000"),
        logcat_line("late", pid=3, stamp="2026-11-04 03:04:06.000000 +0000"),
    ]


def test_group_by_delivers_log_groups_to_the_callback(fake_adb: FakeAdb) -> None:
    """With group_by, on_item receives LogGroup objects (design 2.6).

    ``idle_flush_ms=None``: 3.0 also closes a group after ``gap_ms`` of host
    time without a new member (D-10); off here, the groups close by the
    device timestamps alone, as in 2.x, however slowly the lines arrive.
    """
    fake_adb.logcat(run(*(out(line) for line in _grouped_lines()), wait("done")))
    groups: list[LogGroup] = []
    stream = LogStream(
        group_by=GroupBy(
            keys=("pid",), gap_ms=100.0, mode="windowed", idle_flush_ms=None
        ),
        on_item=groups.append,
    )

    _start(stream)
    try:
        wait_until(lambda: len(groups) >= 2, timeout=TIMEOUT)
    finally:
        _teardown(stream)

    # Closed by "late" in expiry order: b1 (05.001 + 100 ms) before a2 (05.002).
    # Whether the kill still delivers the open "late" group is not pinned here.
    assert [(g.key, [e.message for e in g], g.reason) for g in groups[:2]] == [
        ((2,), ["b1"], "gap"),
        ((1,), ["a1", "a2"], "gap"),
    ]


# -- exit status and join() -----------------------------------------------


def test_a_zero_adb_exit_is_the_outcome(fake_adb: FakeAdb) -> None:
    """adb exiting with status 0 ends the run; join() reports it.

    Changed in 3.0: every unrequested end of adb, exit status 0 included, is
    the outcome of a run without reconnect (design 3.2: a follow-mode logcat
    never ends by itself), so join() raises AdbExitError(returncode=0) and
    the run ends FAILED. ``stop()`` is the normal end (see the stop tests).
    """
    fake_adb.logcat(run(out(logcat_line("only line")), exit_code=0))
    collector = _Collector()
    stream = LogStream(on_item=collector)

    _start(stream)
    try:
        error = _join(stream)
    finally:
        _teardown(stream)

    assert isinstance(error, AdbExitError)
    assert error.returncode == 0
    assert collector.messages() == ["only line"]
    assert len(fake_adb.calls("logcat")) == 1  # adb really ran, exactly once


def test_a_non_zero_adb_exit_makes_join_raise(fake_adb: FakeAdb) -> None:
    """When adb exits non-zero, join() raises AdbExitError (D-2), not a kill."""
    fake_adb.logcat(run(err("error: device offline\n"), exit_code=1))
    stream = LogStream()

    _start(stream)
    try:
        error = _join(stream)
    finally:
        _teardown(stream)

    assert isinstance(error, AdbExitError)
    assert error.returncode == 1
    assert error.stderr == "error: device offline"
    assert stream.state is S.FAILED  # nobody called kill()
    assert len(fake_adb.calls("logcat")) == 1  # adb really ran, exactly once


def test_a_restart_after_a_failed_run_joins_cleanly_when_it_succeeds(
    fake_adb: FakeAdb,
) -> None:
    """After a run that failed, a fresh run of the same stream joins without error.

    3.0 streams run once (design 3.3): start() on a FAILED stream raises
    UsageError, and ``clone()`` is the restart, a fresh stream with the same
    config and callbacks (F2-4). The restart must be a real second run: adb
    records a second invocation, whose line reaches the callback; it is then
    stopped, the normal end of a 3.0 run.
    """
    fake_adb.logcat(
        run(exit_code=1),
        run(out(logcat_line("second run")), wait("never")),
    )
    seen = threading.Event()

    def on_entry(entry: LogEntry) -> None:
        if entry.message == "second run":
            seen.set()

    stream = LogStream(on_item=on_entry)
    twin = stream.clone()

    try:
        _start(stream)
        first = _join(stream)
        with pytest.raises(UsageError, match="runs once"):
            stream.start()
        _start(twin)
        second_seen = seen.wait(TIMEOUT)
        _stop(twin)
        second = _join(twin)
    finally:
        _teardown(stream)
        _teardown(twin)

    _require(
        isinstance(first, AdbExitError),
        f"the failed first run joined with {first!r}, not an AdbExitError",
    )
    indexes = [call["index"] for call in fake_adb.calls("logcat")]
    _require(indexes == [0, 1], f"adb logcat invocations {indexes}, expected [0, 1]")
    _require(second_seen, "the restarted run's line never reached the callback")
    assert second is None, f"the clean restarted run's join() raised {second!r}"
    assert (stream.state, twin.state) == (S.FAILED, S.STOPPED)


# -- stop, kill and lifecycle ---------------------------------------------


def test_stop_ends_a_hanging_stream_and_join_returns(fake_adb: FakeAdb) -> None:
    """stop() ends a stream whose adb never exits, and join() then returns normally."""
    fake_adb.logcat(run(touch("spawned"), hang=True))
    stream = LogStream()

    _start(stream)
    try:
        fake_adb.wait_for("spawned", timeout=TIMEOUT)
        _stop(stream)
        assert _join(stream) is None
    finally:
        _teardown(stream)

    assert stream.state is S.STOPPED
    assert fake_adb.survivors() == []


@_POSIX_SIGNALS
def test_stop_ends_the_stream_even_when_adb_ignores_sigterm(fake_adb: FakeAdb) -> None:
    """stop() still ends the stream when adb ignores SIGTERM, and join() returns.

    3.0 kills adb once ``stop_grace`` passed after the terminate (F2-10); it
    is short here so the test does not wait the 2 s default.
    """
    fake_adb.logcat(run(touch("ignoring"), hang=True, ignore_sigterm=True))
    stream = LogStream(StreamConfig(stop_grace=0.2))

    _start(stream)
    try:
        fake_adb.wait_for("ignoring", timeout=TIMEOUT)
        _stop(stream)
        assert _join(stream) is None
    finally:
        _teardown(stream)

    assert stream.state is S.STOPPED
    assert fake_adb.survivors() == []


def test_handle_stop_from_a_callback_ends_the_stream(fake_adb: FakeAdb) -> None:
    """stop() called inside the item callback ends the stream; join() returns.

    3.0 callbacks take one argument and reach the stream by closure; there is
    no handle object (design 2.4).
    """
    fake_adb.logcat(run(out(logcat_line("stop now")), hang=True))
    seen: list[str] = []
    stream: LogStream[LogEntry]

    def stop_on_first(entry: LogEntry) -> None:
        seen.append(entry.message)
        stream.stop()

    stream = LogStream(on_item=stop_on_first)

    _start(stream)
    try:
        assert _join(stream) is None
    finally:
        _teardown(stream)

    assert seen == ["stop now"]
    assert stream.state is S.STOPPED


def test_leaving_the_with_block_stops_the_stream(fake_adb: FakeAdb) -> None:
    """Leaving a ``with LogStream(...)`` block stops adb; join() then returns."""
    fake_adb.logcat(run(touch("spawned"), hang=True))
    stream = LogStream()

    def use_and_leave() -> None:
        with stream:
            fake_adb.wait_for("spawned", timeout=TIMEOUT)

    try:
        # Entering starts the stream; leaving runs stop() and waits for the end.
        _bounded(use_and_leave, "the with block", 2 * TIMEOUT)
        assert _join(stream) is None
    finally:
        _teardown(stream)

    assert stream.state is S.STOPPED
    assert fake_adb.survivors() == []


def test_kill_ends_killed_and_join_returns(fake_adb: FakeAdb) -> None:
    """kill() ends a running stream at once.

    Changed in 3.0: a kill is not an error (D-6). The stream ends KILLED and
    join() returns; there is no LogStreamKilledError (design 4.1).
    """
    fake_adb.logcat(run(touch("spawned"), hang=True))
    stream = LogStream()

    _start(stream)
    try:
        fake_adb.wait_for("spawned", timeout=TIMEOUT)
        _kill(stream)
        error = _join(stream)
    finally:
        _teardown(stream)

    assert error is None
    assert stream.state is S.KILLED
    assert "killed" in stream.stats.incomplete
    assert fake_adb.survivors() == []


def test_kill_right_after_start_makes_join_raise_killed_error(
    fake_adb: FakeAdb,
) -> None:
    """kill() while the adb spawn is in progress still ends the run KILLED (F2-1).

    The spawn gate holds the spawn after the adb child exists but before the
    spawn call returns it to the library, so kill() lands inside the spawn on
    every run. The child spawned during the kill must not outlive the stream.
    Changed in 3.0: KILLED is not an error, so join() returns (D-6; the F2-1
    row of design 7: "ported: KILLED state, no raise").
    """
    fake_adb.logcat(run(hang=True))
    stream = LogStream()

    try:
        with fake_adb.hold_spawn(command="logcat") as gate:
            starting = _Call(stream.start, "start()")
            gate.wait_entered(timeout=TIMEOUT)
            _kill(stream)  # adb exists, but the library has not received it
        starting.result()
        error = _join(stream)
        survivors = fake_adb.survivors(TIMEOUT, command="logcat")
    finally:
        _teardown(stream)

    _require(survivors == [], f"adb outlived the killed stream: {survivors}")
    assert error is None, f"join() gave {error!r}"
    assert stream.state is S.KILLED


def test_hooks_fire_in_order_for_a_normal_run(fake_adb: FakeAdb) -> None:
    """The state changes reach on_event in order, interleaved with the items.

    3.0 replaces the ``on_state``/``on_start``/``on_stop`` hooks with
    ``on_event`` (D-2), which receives each `StateChange` in order with the
    items (design 2.4, 3.6). adb ending on its own (exit status 0) is the
    outcome of the run (design 3.2), so the last change is RUNNING -> FAILED
    and carries the AdbExitError join() raises.
    """
    fake_adb.logcat(run(out(logcat_line("x")), exit_code=0))
    events: list[StreamEvent | str] = []
    stream = LogStream(
        on_item=lambda entry: events.append(entry.message), on_event=events.append
    )

    _start(stream)
    try:
        error = _join(stream)
        recorded = list(events)
    finally:
        _teardown(stream)

    assert _changes(recorded) == [
        (S.IDLE, S.CONNECTING),
        (S.CONNECTING, S.RUNNING),
        "x",
        (S.RUNNING, S.FAILED),
    ]
    last = recorded[-1]
    assert isinstance(last, StateChange)
    assert isinstance(error, AdbExitError)
    assert last.error is error


# -- read_timeout and auto_reconnect --------------------------------------


def test_read_timeout_spares_a_stream_with_regular_stdout_and_silent_stderr(
    fake_adb: FakeAdb,
) -> None:
    """Stdout every 0.1 s keeps a 0.5 s read_timeout from firing; stderr is silent.

    The test stops the stream once it saw every tick (3.0's normal end; adb
    exiting by itself would be the AdbExitError outcome, design 3.2), and
    join() returns without a ReadTimeoutError.
    """
    count = 15
    steps = []
    for i in range(count):
        steps += [out(logcat_line(f"tick {i}")), sleep(0.1)]
    fake_adb.logcat(run(*steps, wait("done")))
    collector = _Collector()
    stream = LogStream(StreamConfig(read_timeout=0.5), on_item=collector)

    _start(stream)
    try:
        wait_until(
            lambda: len(collector.entries) >= count or stream.state in _ENDED,
            timeout=TIMEOUT,
        )
        _stop(stream)
        error = _join(stream)
    finally:
        _teardown(stream)

    assert error is None
    assert collector.messages() == [f"tick {i}" for i in range(count)]


def test_read_timeout_ends_a_silent_stream_and_join_raises(fake_adb: FakeAdb) -> None:
    """A stream silent past read_timeout ends and join() raises ReadTimeoutError (D-3).

    adb prints one line first: 3.0 arms ``read_timeout`` only once adb has
    printed (RUNNING, design 5.7). An adb silent from the start is waiting
    for a device and ends by ``connect_timeout`` instead
    (``tests/conformance/test_reconnect.py::test_adb_waiting_without_output_ends_as_device_not_available``).
    """
    read_timeout = 0.5
    fake_adb.logcat(run(out(logcat_line("only")), hang=True))
    stream = LogStream(StreamConfig(read_timeout=read_timeout))

    began = time.monotonic()
    _start(stream)
    try:
        error = _join(stream)
        lasted = time.monotonic() - began
    finally:
        _teardown(stream)

    assert isinstance(error, ReadTimeoutError)
    assert not isinstance(error, TimeoutError)
    # Ended by the timeout, not by some earlier failure, and not reconnected.
    assert lasted >= read_timeout
    assert len(fake_adb.calls("logcat")) == 1
    assert fake_adb.survivors() == []


def test_auto_reconnect_respawns_adb_after_a_non_zero_exit(fake_adb: FakeAdb) -> None:
    """With reconnect, a non-zero exit respawns adb logcat and its lines arrive (F4-2).

    2.x ``auto_reconnect=True, reconnect_delay=0.05`` is
    ``StreamConfig(reconnect=Reconnect(delay=0.05))`` (D-1).
    """
    fake_adb.logcat(
        run(out(logcat_line("first")), exit_code=1),
        run(out(logcat_line("second")), wait("done")),
    )
    collector = _Collector()
    settled = threading.Event()

    def on_entry(entry: LogEntry) -> None:
        collector(entry)
        if entry.message == "second":
            settled.set()

    def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange) and event.new in {S.FAILED, S.KILLED}:
            settled.set()

    stream = LogStream(
        StreamConfig(reconnect=Reconnect(delay=0.05)),
        on_item=on_entry,
        on_event=on_event,
    )

    _start(stream)
    try:
        _require(
            settled.wait(TIMEOUT),
            "neither the reconnected run's line nor the run's end arrived",
        )
        spawned = len(fake_adb.calls("logcat"))
    finally:
        _teardown(stream)

    assert spawned == 2
    assert collector.messages() == ["first", "second"]
