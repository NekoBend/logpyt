"""Behavior of the thread-based ``LogStream`` against a real fake-adb child.

These tests pin what a user of the public API relies on (constructor
arguments, start/stop/kill/join, callbacks, hooks, exceptions), so they must
survive an internal rewrite: no private attributes, no mocked internals, only
the ``fake_adb`` fixture from ``tests/conftest.py``.

Known 2.0.x bugs are written for the correct behavior and marked
``xfail(strict=True, raises=AssertionError)`` with their id from
``.docs/review-findings-2.0.1.md``. Only the assertion on the defect's
observable is a bare ``assert``. A precondition (``_require``), a harness
timeout, and a lifecycle call that does not return (``_bounded``) fail the
test outright, so none of them can count as the known defect.

Every lifecycle call (start/stop/kill/join, the ``with`` block) runs through
``_bounded``. A deadlock in the library then fails its test within a few
seconds instead of waiting for the hang watchdog to end the whole run.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

from logpyt import (
    GroupBy,
    LogStream,
    LogStreamError,
    LogStreamKilledError,
    ThreadTimeLogParser,
)
from logpyt.streams import StreamState
from tests.conftest import (
    FakeAdb,
    err,
    lines,
    out,
    run,
    sleep,
    threadtime,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from logpyt.models import LogEntry, LogGroup
    from logpyt.parsers import ParseContext
    from logpyt.streams import StreamHandle

# Upper bound for every wait, join and lifecycle call. A healthy run finishes
# far sooner; the bound only exists so a regression fails the test instead of
# hanging CI.
TIMEOUT = 10.0

# join(timeout=TIMEOUT) itself runs under a bound this much longer, so a join()
# that ignores its own timeout still fails the test.
_JOIN_SLACK = 5.0

_POSIX_SIGNALS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs POSIX signals: on Windows terminate() is kill() and "
    "SIGTERM cannot be ignored",
)


class _Collector:
    """A data callback that records every entry it receives."""

    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    def __call__(self, entry: LogEntry, _handle: StreamHandle) -> None:
        self.entries.append(entry)

    def messages(self) -> list[str]:
        return [entry.message for entry in list(self.entries)]


class _SlowParser(ThreadTimeLogParser):
    """A user parser that spends ~2 ms per stdout line, like a heavy custom one."""

    def parse(self, line: str, ctx: ParseContext, /) -> LogEntry | None:
        time.sleep(0.002)
        return super().parse(line, ctx)


class _Call[T]:
    """A call running on its own daemon thread, collected with a bound.

    A library call that deadlocks then fails the test (``pytest.fail``, which
    an ``xfail(raises=AssertionError)`` mark does not absorb) instead of
    blocking the test thread. The stuck thread is left behind; the fake_adb
    finalizer kills the adb child it may be waiting on.
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


def _start(stream: LogStream) -> None:
    _bounded(stream.start, "start()")


def _stop(stream: LogStream) -> None:
    _bounded(stream.stop, "stop()")


def _kill(stream: LogStream) -> None:
    _bounded(stream.kill, "kill()")


def _require(condition: object, message: str) -> None:
    """Fail the test outright when a precondition does not hold.

    Unlike ``assert`` this does not raise AssertionError, so a strict
    ``xfail(raises=AssertionError)`` test reports the failure instead of
    counting it as the known defect.
    """
    if not condition:
        pytest.fail(message)


def _join(stream: LogStream) -> LogStreamError | None:
    """Join within ``TIMEOUT``; return the error join() raised, or None.

    Running out of the budget fails the test whatever exception type join()
    uses for it, so a hung stream is never mistaken for a stream error.
    """
    deadline = time.monotonic() + TIMEOUT
    try:
        _bounded(lambda: stream.join(timeout=TIMEOUT), "join()", TIMEOUT + _JOIN_SLACK)
    except LogStreamError as exc:
        if time.monotonic() >= deadline:
            pytest.fail(f"join() did not finish within {TIMEOUT} s ({exc!r})")
        return exc
    return None


def _teardown(stream: LogStream) -> None:
    """Kill the stream and wait for it, so no thread or child outlives a test."""
    _kill(stream)
    deadline = time.monotonic() + TIMEOUT
    with contextlib.suppress(LogStreamError):
        _bounded(
            lambda: stream.join(timeout=TIMEOUT),
            "join() after kill()",
            TIMEOUT + _JOIN_SLACK,
        )
    if time.monotonic() >= deadline:
        pytest.fail(f"the stream did not finish within {TIMEOUT} s after kill()")


def _missing(received: list[str], expected: list[str]) -> str:
    """Describe which expected messages did not arrive (for assert messages)."""
    seen = set(received)
    head = [i for i, message in enumerate(expected) if message not in seen][:3]
    return f"{len(received)}/{len(expected)} delivered; first missing indexes {head}"


# -- delivery and framing -------------------------------------------------


def test_every_stdout_line_reaches_the_callback_in_order(fake_adb: FakeAdb) -> None:
    """Every line adb writes to stdout reaches stdout_callback once, in order."""
    count = 2000
    fake_adb.logcat(run(lines(count, threadtime("{i}")), wait("done")))
    collector = _Collector()
    stream = LogStream(stdout_callback=collector)

    _start(stream)
    try:
        with contextlib.suppress(TimeoutError):
            wait_until(lambda: len(collector.entries) >= count, timeout=TIMEOUT)
    finally:
        _teardown(stream)

    expected = [str(i) for i in range(count)]
    received = collector.messages()
    assert received == expected, _missing(received, expected)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F1-1: the sync reader stops at process exit and drops the "
    "output still buffered in the pipe",
)
def test_no_line_is_lost_when_adb_exits_before_the_stream_read_it_all(
    fake_adb: FakeAdb,
) -> None:
    """Every line adb wrote is delivered even when adb exits right after writing.

    A user parser costing ~2 ms per line keeps adb's output waiting in the
    pipe when adb exits (as a fast ``logcat -d`` dump does); 200 lines keep
    the whole drain under 0.5 s.
    """
    count = 200
    fake_adb.logcat(run(lines(count, threadtime("{i}")), exit_code=0))
    collector = _Collector()
    stream = LogStream(stdout_callback=collector, parser=_SlowParser())

    _start(stream)
    try:
        error = _join(stream)
    finally:
        _teardown(stream)

    _require(error is None, f"join() raised {error!r} after adb exited 0")
    expected = [str(i) for i in range(count)]
    received = collector.messages()
    assert received == expected, _missing(received, expected)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F1-2: universal newlines split a message at a bare CR, so the "
    "text after it is parsed as a forged entry",
)
def test_a_bare_cr_inside_a_message_does_not_forge_a_second_entry(
    fake_adb: FakeAdb,
) -> None:
    """A bare CR inside a message keeps the whole line one entry; nothing is forged.

    The message keeps its text after the CR, so truncating at the CR does not
    pass either.
    """
    forged = threadtime(
        "FATAL EXCEPTION: main", pid=1000, tid=1000, level="E", tag="AndroidRuntime"
    ).rstrip("\n")
    fake_adb.logcat(
        run(
            out(threadtime(f"hi\r{forged}", pid=4321, tid=4321, tag="Evil")),
            out(threadtime("after", tag="Next")),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = LogStream(stdout_callback=collector)

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
            out(threadtime("first").replace("\n", "\r\n")),
            out(threadtime("second").replace("\n", "\r\n")),
            out(threadtime("last", tag="End").replace("\n", "\r\n")),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = LogStream(stdout_callback=collector)

    _start(stream)
    try:
        wait_until(
            lambda: any(e.tag == "End" for e in list(collector.entries)),
            timeout=TIMEOUT,
        )
    finally:
        _teardown(stream)

    assert collector.messages() == ["first", "second", "last"]


def test_stderr_output_reaches_only_stderr_callback(fake_adb: FakeAdb) -> None:
    """adb's stderr lines go to stderr_callback and never to stdout_callback."""
    fake_adb.logcat(
        run(
            err("error: device offline\n"),
            err("second error\n"),
            out(threadtime("on stdout")),
            wait("done"),
        )
    )
    stdout_entries = _Collector()
    stderr_entries = _Collector()
    stream = LogStream(stdout_callback=stdout_entries, stderr_callback=stderr_entries)

    _start(stream)
    try:
        # Wait for the last line of each pipe: anything misrouted would have
        # been dispatched by then.
        wait_until(
            lambda: (
                "second error" in stderr_entries.messages()
                and "on stdout" in stdout_entries.messages()
            ),
            timeout=TIMEOUT,
        )
    finally:
        _teardown(stream)

    assert stdout_entries.messages() == ["on stdout"]
    assert stderr_entries.messages() == ["error: device offline", "second error"]


def test_a_50k_burst_is_delivered_in_order_when_the_queue_can_hold_it(
    fake_adb: FakeAdb,
) -> None:
    """A 50,000-line dump reaches a fast callback completely and in order.

    The callback queue is sized to hold the whole burst, so this pins reading
    and dispatch at volume, not queue overflow. With the default queue the
    outcome depends on CPU scheduling: whenever the reader thread outruns even
    a fast callback, the burst loses entries (X-1, seen on a 2-core run). The
    xfail test below pins X-1. adb stays alive after the burst so a loss at
    process exit (F1-1) cannot show up here.
    """
    count = 50_000
    fake_adb.logcat(run(lines(count, threadtime("{i}")), wait("done")))
    collector = _Collector()
    stream = LogStream(stdout_callback=collector, max_queue_size=count)

    _start(stream)
    try:
        with contextlib.suppress(TimeoutError):
            wait_until(lambda: len(collector.entries) >= count, timeout=TIMEOUT)
    finally:
        _teardown(stream)

    received = collector.messages()
    expected = [str(i) for i in range(count)]
    assert received == expected, _missing(received, expected)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="X-1: the default callback queue drops new entries once a burst "
    "outruns the callback, instead of holding the reader back",
)
def test_no_entry_of_a_burst_is_lost_while_the_callback_is_behind(
    fake_adb: FakeAdb,
) -> None:
    """With default settings, a burst is delivered in full while the callback lags.

    adb writes one line, and the callback stalls on it before adb writes
    anything more. The callback stays stalled until adb has written all of a
    20,000-line burst to the pipe. By then the stream has read all but what a
    pipe holds (about 64 KiB, under 2,000 of these lines) and consumed none of
    it, so a default queue that drops has overflowed on any machine speed.
    This is how a native adb's ring-buffer dump outruns a callback thread that
    is not running yet. A stream that instead holds adb back (backpressure)
    stops adb's progress. The callback is then resumed once no further chunk of
    the burst was written for ``held_back_after`` seconds. A chunk of 1,000
    lines takes milliseconds, so a dropping stream never gets that far.
    Backpressure and a larger default queue both satisfy this test (X-1 design
    note); only complete, in-order delivery is pinned.

    The ``end`` lines are written after the callback resumed. They only tell the
    test that delivery has settled. They are spread over a second so that one
    of them gets through a dropping queue once it has drained; their timing
    never decides the outcome.
    """
    chunks, chunk = 20, 1_000
    held_back_after = 2.0
    stall_bound = 6 * TIMEOUT  # resume is always set in the finally below
    burst = [
        step
        for k in range(chunks)
        for step in (
            lines(chunk, threadtime(f"{k}:{{i}}", tag="Burst")),
            touch(f"chunk {k}"),
        )
    ]
    endings = [
        step
        for k in range(20)
        for step in (out(threadtime(f"end {k}", tag="End")), sleep(0.05))
    ]
    fake_adb.logcat(
        run(
            out(threadtime("stall here", tag="Stall")),
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

    def lagging(entry: LogEntry, _handle: StreamHandle) -> None:
        if entry.tag == "Stall":
            stalled.set()
        resume.wait(stall_bound)
        received.append((entry.tag, entry.message))
        if entry.tag == "End":
            settled.set()

    stream = LogStream(stdout_callback=lagging)

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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="X-4: readline(_READ_LIMIT) splits one over-long physical line "
    "into several entries",
)
def test_a_line_longer_than_the_read_cap_is_not_split_into_entries(
    fake_adb: FakeAdb,
) -> None:
    """A 9 MiB line (over the 8 MiB per-line read cap) yields one entry, not several.

    Whether an over-cap message is kept whole or truncated is not pinned; only
    that no fragment of it is parsed as an entry of its own.
    """
    header = threadtime("").rstrip("\n")
    fake_adb.logcat(
        run(
            out(header),
            lines(9 * 1024, "x" * 1024),
            out("\n"),
            out(threadtime("after", tag="Next")),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = LogStream(stdout_callback=collector)

    _start(stream)
    try:
        wait_until(lambda: "after" in collector.messages(), timeout=TIMEOUT)
    finally:
        _teardown(stream)

    assert [e.tag for e in collector.entries] == ["Tag", "Next"]


def _grouped_lines() -> list[str]:
    """Two pids interleaved, then a line one second later that expires both."""
    return [
        threadtime("a1", pid=1, tid=1, time_="03:04:05.000"),
        threadtime("b1", pid=2, tid=2, time_="03:04:05.001"),
        threadtime("a2", pid=1, tid=1, time_="03:04:05.002"),
        threadtime("late", pid=3, tid=3, time_="03:04:06.000"),
    ]


def test_group_by_delivers_log_groups_to_the_callback(fake_adb: FakeAdb) -> None:
    """With group_by, stdout_callback receives LogGroup objects (design 2.6)."""
    fake_adb.logcat(run(*(out(line) for line in _grouped_lines()), wait("done")))
    groups: list[LogGroup] = []
    stream = LogStream(
        group_by=GroupBy(keys=("pid",), gap_ms=100.0, mode="windowed"),
        stdout_callback=lambda group, _handle: groups.append(group),
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


def test_a_zero_adb_exit_makes_join_return_normally(fake_adb: FakeAdb) -> None:
    """When adb exits with status 0, join() returns without raising."""
    fake_adb.logcat(run(out(threadtime("only line")), exit_code=0))
    stream = LogStream()

    _start(stream)
    try:
        assert _join(stream) is None
    finally:
        _teardown(stream)

    assert len(fake_adb.calls("logcat")) == 1  # adb really ran, exactly once


def test_a_non_zero_adb_exit_makes_join_raise(fake_adb: FakeAdb) -> None:
    """When adb exits non-zero, join() raises a LogStreamError (type: D-2)."""
    fake_adb.logcat(run(err("error: device offline\n"), exit_code=1))
    stream = LogStream()

    _start(stream)
    try:
        error = _join(stream)
    finally:
        _teardown(stream)

    assert isinstance(error, LogStreamError)
    assert not isinstance(error, LogStreamKilledError)  # nobody called kill()
    assert len(fake_adb.calls("logcat")) == 1  # adb really ran, exactly once


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F2-4: start() keeps the previous run's error, so a clean "
    "restarted run's join() re-raises it",
)
def test_a_restart_after_a_failed_run_joins_cleanly_when_it_succeeds(
    fake_adb: FakeAdb,
) -> None:
    """After a run that failed, a restarted run that exits 0 joins without error.

    The restart must be a real second run: adb records a second invocation,
    and that invocation does not exit until its line has reached the callback.
    A start() that clears the old error but launches nothing cannot pass, and
    neither can a loss at process exit (F1-1).
    """
    fake_adb.logcat(
        run(exit_code=1),
        run(out(threadtime("second run")), wait("second run seen"), exit_code=0),
    )
    seen = threading.Event()

    def on_entry(entry: LogEntry, _handle: StreamHandle) -> None:
        if entry.message == "second run":
            seen.set()

    stream = LogStream(stdout_callback=on_entry)

    try:
        _start(stream)
        first = _join(stream)
        _start(stream)
        second_seen = seen.wait(TIMEOUT)
        fake_adb.release("second run seen")
        second = _join(stream)
    finally:
        _teardown(stream)

    _require(
        isinstance(first, LogStreamError),
        f"the failed first run joined with {first!r}, not a LogStreamError",
    )
    indexes = [call["index"] for call in fake_adb.calls("logcat")]
    _require(indexes == [0, 1], f"adb logcat invocations {indexes}, expected [0, 1]")
    _require(second_seen, "the restarted run's line never reached the callback")
    assert second is None, f"the clean restarted run's join() raised {second!r}"


# -- stop, kill and lifecycle ---------------------------------------------


def test_stop_ends_a_hanging_stream_and_join_returns(fake_adb: FakeAdb) -> None:
    """stop() ends a stream whose adb never exits, and join() then returns normally."""
    fake_adb.logcat(run(hang=True))
    started = threading.Event()
    stream = LogStream(on_start=started.set)

    _start(stream)
    try:
        assert started.wait(TIMEOUT)
        _stop(stream)
        assert _join(stream) is None
    finally:
        _teardown(stream)


@_POSIX_SIGNALS
def test_stop_ends_the_stream_even_when_adb_ignores_sigterm(fake_adb: FakeAdb) -> None:
    """stop() still ends the stream when adb ignores SIGTERM, and join() returns."""
    fake_adb.logcat(run(touch("ignoring"), hang=True, ignore_sigterm=True))
    started = threading.Event()
    stream = LogStream(on_start=started.set)

    _start(stream)
    try:
        assert started.wait(TIMEOUT)
        fake_adb.wait_for("ignoring", timeout=TIMEOUT)
        _stop(stream)
        assert _join(stream) is None
    finally:
        _teardown(stream)


def test_handle_stop_from_a_callback_ends_the_stream(fake_adb: FakeAdb) -> None:
    """Calling handle.stop() inside stdout_callback ends the stream; join() returns."""
    fake_adb.logcat(run(out(threadtime("stop now")), hang=True))
    seen: list[str] = []

    def stop_on_first(entry: LogEntry, handle: StreamHandle) -> None:
        seen.append(entry.message)
        handle.stop()

    stream = LogStream(stdout_callback=stop_on_first)

    _start(stream)
    try:
        assert _join(stream) is None
    finally:
        _teardown(stream)

    assert seen == ["stop now"]


def test_leaving_the_with_block_stops_the_stream(fake_adb: FakeAdb) -> None:
    """Leaving a ``with LogStream(...)`` block stops adb; join() then returns."""
    fake_adb.logcat(run(hang=True))
    started = threading.Event()
    stream = LogStream(on_start=started.set)

    def use_and_leave() -> bool:
        with stream:
            return started.wait(TIMEOUT)

    try:
        # Entering waits up to TIMEOUT for on_start; leaving runs stop().
        assert _bounded(use_and_leave, "the with block", 2 * TIMEOUT)
        assert _join(stream) is None
    finally:
        _teardown(stream)


def test_kill_makes_join_raise_killed_error(fake_adb: FakeAdb) -> None:
    """kill() ends a running stream and join() raises LogStreamKilledError."""
    fake_adb.logcat(run(hang=True))
    started = threading.Event()
    stream = LogStream(on_start=started.set)

    _start(stream)
    try:
        assert started.wait(TIMEOUT)
        _kill(stream)
        error = _join(stream)
    finally:
        _teardown(stream)

    assert isinstance(error, LogStreamKilledError)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F2-1: a kill() during spawn is overwritten by RUNNING, so "
    "join() returns normally",
)
def test_kill_right_after_start_makes_join_raise_killed_error(
    fake_adb: FakeAdb,
) -> None:
    """kill() while start()'s adb spawn is in progress still makes join() raise.

    The spawn gate holds the spawn after the adb child exists but before the
    spawn call returns it to the library, so kill() lands inside the spawn on
    every run. start() runs on a helper thread, so this also holds for a stream
    that spawns adb inside start() itself. The child spawned during the kill
    must not outlive the stream either.
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
    assert isinstance(error, LogStreamKilledError), f"join() gave {error!r}"


def test_hooks_fire_in_order_for_a_normal_run(fake_adb: FakeAdb) -> None:
    """A run ending on its own reports STARTING, RUNNING, on_start, STOPPED, on_stop."""
    fake_adb.logcat(run(out(threadtime("x")), exit_code=0))
    events: list[object] = []
    stream = LogStream(
        on_state=events.append,
        on_start=lambda: events.append("on_start"),
        on_stop=lambda: events.append("on_stop"),
    )

    _start(stream)
    try:
        assert _join(stream) is None
        recorded = list(events)  # before teardown's kill() adds KILLED
    finally:
        _teardown(stream)

    assert recorded == [
        StreamState.STARTING,
        StreamState.RUNNING,
        "on_start",
        StreamState.STOPPED,
        "on_stop",
    ]


# -- read_timeout and auto_reconnect --------------------------------------


def test_read_timeout_spares_a_stream_with_regular_stdout_and_silent_stderr(
    fake_adb: FakeAdb,
) -> None:
    """Stdout every 0.1 s keeps a 0.5 s read_timeout from firing; stderr is silent.

    adb exits 0 only after the test saw every tick, so a loss at process exit
    (F1-1) cannot fail this test.
    """
    count = 15
    steps = []
    for i in range(count):
        steps += [out(threadtime(f"tick {i}")), sleep(0.1)]
    fake_adb.logcat(run(*steps, wait("done"), exit_code=0))
    collector = _Collector()
    ended = threading.Event()
    stream = LogStream(stdout_callback=collector, on_stop=ended.set, read_timeout=0.5)

    _start(stream)
    try:
        wait_until(
            lambda: len(collector.entries) >= count or ended.is_set(),
            timeout=TIMEOUT,
        )
        fake_adb.release("done")
        error = _join(stream)
    finally:
        _teardown(stream)

    assert error is None
    assert collector.messages() == [f"tick {i}" for i in range(count)]


def test_read_timeout_ends_a_silent_stream_and_join_raises(fake_adb: FakeAdb) -> None:
    """A stream silent past read_timeout ends and join() raises (error type: D-3)."""
    read_timeout = 0.5
    fake_adb.logcat(run(hang=True))
    stream = LogStream(read_timeout=read_timeout)

    began = time.monotonic()
    _start(stream)
    try:
        error = _join(stream)
        lasted = time.monotonic() - began
    finally:
        _teardown(stream)

    assert isinstance(error, LogStreamError)
    # Ended by the timeout, not by some earlier failure, and not reconnected.
    assert lasted >= read_timeout
    assert len(fake_adb.calls("logcat")) == 1


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F4-2: sync treats every non-zero adb exit as terminal and never reconnects",
)
def test_auto_reconnect_respawns_adb_after_a_non_zero_exit(fake_adb: FakeAdb) -> None:
    """With auto_reconnect, a non-zero exit respawns adb logcat and its lines arrive."""
    fake_adb.logcat(
        run(out(threadtime("first")), exit_code=1),
        run(out(threadtime("second")), wait("done")),
    )
    collector = _Collector()
    settled = threading.Event()

    def on_entry(entry: LogEntry, handle: StreamHandle) -> None:
        collector(entry, handle)
        if entry.message == "second":
            settled.set()

    stream = LogStream(
        stdout_callback=on_entry,
        on_stop=settled.set,
        auto_reconnect=True,
        reconnect_delay=0.05,
    )

    _start(stream)
    try:
        _require(
            settled.wait(TIMEOUT),
            "neither the reconnected run's line nor on_stop arrived",
        )
        spawned = len(fake_adb.calls("logcat"))
    finally:
        _teardown(stream)

    assert spawned == 2
    assert "second" in collector.messages()
