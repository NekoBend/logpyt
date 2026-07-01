"""Tests for log stream."""

import logging
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from logpyt.exceptions import (
    LogStreamError,
    LogStreamInternalError,
    LogStreamTimeoutError,
)
from logpyt.filters import Filter
from logpyt.groupers import LogGrouper
from logpyt.models import LogEntry
from logpyt.streams import LogStream, StreamState


@pytest.fixture
def mock_popen(mocker):
    """Mock subprocess.Popen."""
    mock = mocker.patch("subprocess.Popen")
    process_mock = Mock()
    process_mock.stdout = Mock()
    process_mock.stderr = Mock()
    process_mock.stdout.readline.side_effect = ["log line 1\n", "log line 2\n", ""]
    process_mock.stderr.readline.return_value = ""
    process_mock.poll.return_value = None
    process_mock.terminate.return_value = None
    process_mock.kill.return_value = None
    mock.return_value = process_mock
    return mock


def test_stream_lifecycle(mock_popen, mocker) -> None:
    """Test start, stop, and join of LogStream."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    stream = LogStream()
    assert stream.state == StreamState.IDLE

    # Make the process wait block so we can catch the RUNNING state
    mock_popen.return_value.wait.side_effect = lambda *args, **kwargs: time.sleep(0.5)

    stream.start()

    # Wait for state to become RUNNING
    start_time = time.time()
    while stream.state != StreamState.RUNNING:
        if time.time() - start_time > 1.0:
            raise TimeoutError("Timed out waiting for RUNNING state")
        time.sleep(0.01)

    assert stream.state == StreamState.RUNNING
    mock_popen.assert_called_once()

    stream.stop()
    # State might be STOPPING or STOPPED depending on thread timing
    assert stream.state in {StreamState.STOPPING, StreamState.STOPPED}

    stream.join(timeout=1.0)
    # After join, threads should be dead


def test_stream_callbacks(mock_popen, mocker) -> None:
    """Test that callbacks are invoked."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    stdout_cb = Mock()
    stream = LogStream(stdout_callback=stdout_cb)

    stream.start()
    stream.join(timeout=1.0)

    assert stdout_cb.call_count == 2
    # Verify first call argument
    args, _ = stdout_cb.call_args_list[0]
    entry = args[0]
    assert isinstance(entry, LogEntry)
    assert entry.message == "log line 1"


def test_stream_context_manager(mock_popen, mocker) -> None:
    """Test using LogStream as a context manager."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    with LogStream() as handle:
        # Make the process wait block so we can catch the RUNNING state
        mock_popen.return_value.wait.side_effect = lambda *args, **kwargs: time.sleep(
            0.5
        )

        # Wait for state to become RUNNING
        start_time = time.time()
        while handle.state != StreamState.RUNNING:
            if time.time() - start_time > 1.0:
                raise TimeoutError("Timed out waiting for RUNNING state")
            time.sleep(0.01)

        assert handle.state == StreamState.RUNNING
        handle.stop()
        assert handle.state in {StreamState.STOPPING, StreamState.STOPPED}


def test_stream_error_callback(mocker) -> None:
    """Test error callback when Popen fails."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")
    mocker.patch("subprocess.Popen", side_effect=OSError("Failed"))

    error_cb = Mock()
    stream = LogStream(on_error=error_cb)

    stream.start()

    # Wait for error callback
    start_time = time.time()
    while error_cb.call_count == 0:
        if time.time() - start_time > 1.0:
            break
        time.sleep(0.01)

    error_cb.assert_called_once()


def test_stream_startup_failure_is_raised_from_join(mocker) -> None:
    """Startup failures should be surfaced by join()."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")
    mocker.patch("subprocess.Popen", side_effect=OSError("adb start failed"))

    stream = LogStream()
    stream.start()

    with pytest.raises(LogStreamInternalError, match="adb start failed"):
        stream.join(timeout=1.0)


def test_stream_join_raises_exception(mock_popen, mocker) -> None:
    """Test that exceptions in threads are propagated to join()."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    # Mock stdout readline to raise an exception
    process_mock = mock_popen.return_value
    process_mock.stdout.readline.side_effect = RuntimeError("Thread error")

    stream = LogStream()
    stream.start()

    # join() should raise the exception wrapped in LogStreamInternalError
    with pytest.raises(LogStreamInternalError) as excinfo:
        stream.join(timeout=1.0)

    assert "Thread error" in str(excinfo.value)


def test_stream_parse_error_propagates_to_join(mock_popen, mocker) -> None:
    """Parser exceptions should be visible to join() callers."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    process_mock = mock_popen.return_value
    process_mock.stdout.readline.side_effect = ["bad line\n", ""]
    process_mock.stderr.readline.return_value = ""

    parser = Mock()
    parser.parse_stdout.side_effect = ValueError("parse failed")

    stream = LogStream(parser=parser)
    stream.start()

    with pytest.raises(LogStreamInternalError, match="parse failed"):
        stream.join(timeout=1.0)


def test_stream_non_zero_exit_is_raised_from_join(mock_popen, mocker) -> None:
    """Non-zero ADB process exit should be surfaced by join()."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    process_mock = mock_popen.return_value
    process_mock.stdout.readline.return_value = ""
    process_mock.stderr.readline.return_value = "adb: device offline\n"
    process_mock.wait.return_value = 1
    process_mock.poll.return_value = 1

    stream = LogStream()
    stream.start()

    with pytest.raises(LogStreamInternalError):
        stream.join(timeout=1.0)


def test_stream_package_resolution(mock_popen, mocker) -> None:
    """Test that package name is resolved and added to LogEntry."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    # Mock PidMonitor
    mock_pid_monitor_cls = mocker.patch("logpyt.streams.sync.PidMonitor")
    mock_pid_monitor = mock_pid_monitor_cls.return_value
    mock_pid_monitor.get_package.return_value = "com.example"

    # Setup stream with package filter
    pkg_filter = Filter(package=["com.example"])

    # Capture entries
    entries = []

    def callback(entry, handle):
        entries.append(entry)

    stream = LogStream(filter_by=pkg_filter, stdout_callback=callback)

    # Mock parser to return an entry with a specific PID
    mock_parser = Mock()
    mock_entry = LogEntry(
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        pid=1234,
        tid=1234,
        level="D",
        tag="Tag",
        message="msg",
        raw="raw",
    )
    mock_parser.parse_stdout.return_value = mock_entry
    stream.parser = mock_parser

    stream.start()
    stream.join(timeout=1.0)

    # Verify PidMonitor was initialized
    mock_pid_monitor_cls.assert_called_once()

    # Verify PidMonitor.get_package was called
    mock_pid_monitor.get_package.assert_called_with(1234)

    # Verify entry has package info
    assert len(entries) > 0
    assert entries[0].meta["package"] == "com.example"


def test_stream_package_filter_drops_non_matching_entries(mock_popen, mocker) -> None:
    """A package Filter must DROP entries whose package does not match.

    Extends test_stream_package_resolution (happy path only) by proving that
    entries resolving to a different package never reach the callback, while
    matching ones do.
    """
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    # PidMonitor resolves PID 1234 -> matching package, PID 9999 -> other package.
    mock_pid_monitor_cls = mocker.patch("logpyt.streams.sync.PidMonitor")
    mock_pid_monitor = mock_pid_monitor_cls.return_value
    mock_pid_monitor.get_package.side_effect = lambda pid: {
        1234: "com.example",
        9999: "com.other",
    }.get(pid)

    pkg_filter = Filter(package=["com.example"])

    entries: list[LogEntry] = []

    def callback(entry, handle):
        del handle
        entries.append(entry)

    stream = LogStream(filter_by=pkg_filter, stdout_callback=callback)

    # Two raw lines; parser maps them to a matching and a non-matching PID.
    process_mock = mock_popen.return_value
    process_mock.stdout.readline.side_effect = ["match\n", "drop\n", ""]
    process_mock.stderr.readline.return_value = ""

    def parse_stdout(line: str) -> LogEntry:
        pid = 1234 if line.strip() == "match" else 9999
        return LogEntry(
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            pid=pid,
            tid=pid,
            level="D",
            tag="Tag",
            message=line.strip(),
            raw=line.strip(),
        )

    mock_parser = Mock()
    mock_parser.parse_stdout.side_effect = parse_stdout
    stream.parser = mock_parser

    stream.start()
    stream.join(timeout=1.0)

    # Only the matching-package entry reaches the callback; the other is dropped.
    assert [e.message for e in entries] == ["match"]
    assert all(e.meta.get("package") == "com.example" for e in entries)


def test_stream_grouper_flushes_buffered_entries_on_stdout_eof(
    mock_popen, mocker
) -> None:
    """Buffered grouped entries must be flushed to the callback on stdout EOF.

    Covers commit 2d8c57d: a group_by grouper buffers consecutive same-key
    entries without emitting; when stdout reaches EOF the read loop's finally
    block flushes the buffer so the grouped entries are still delivered.
    """
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    # Large threshold + same key => both entries buffer, nothing emitted early.
    grouper = LogGrouper(by=["tag"], threshold_ms=10_000.0, emit_mode="group")

    received: list[list[LogEntry]] = []

    def callback(item: list[LogEntry], handle) -> None:
        del handle
        received.append(item)

    stream = LogStream(group_by=grouper, stdout_callback=callback)

    process_mock = mock_popen.return_value
    process_mock.stdout.readline.side_effect = ["line-1\n", "line-2\n", ""]
    process_mock.stderr.readline.return_value = ""

    base_ts = datetime.now(UTC).replace(tzinfo=None)

    def parse_stdout(line: str) -> LogEntry:
        return LogEntry(
            timestamp=base_ts,
            pid=1,
            tid=1,
            level="D",
            tag="SameTag",
            message=line.strip(),
            raw=line.strip(),
        )

    mock_parser = Mock()
    mock_parser.parse_stdout.side_effect = parse_stdout
    stream.parser = mock_parser

    stream.start()
    stream.join(timeout=1.0)

    # emit_mode="group" delivers the buffered group as a single list argument.
    assert len(received) == 1
    group = received[0]
    assert isinstance(group, list)
    assert [entry.message for entry in group] == ["line-1", "line-2"]


def test_auto_reconnect(mocker) -> None:
    """Test that the stream automatically reconnects when the process exits."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    # Mock Popen to return a process that finishes immediately
    mock_popen = mocker.patch("subprocess.Popen")

    # Create a factory for mock processes
    def create_mock_process(*args, **kwargs):
        process = Mock()
        process.stdout.readline.return_value = ""  # EOF immediately
        process.stderr.readline.return_value = ""
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.terminate.return_value = None
        process.kill.return_value = None
        return process

    mock_popen.side_effect = create_mock_process

    state_changes = []

    def on_state(state):
        state_changes.append(state)

    # Use a short reconnect delay to speed up the test
    stream = LogStream(
        auto_reconnect=True,
        reconnect_delay=0.01,
        on_state=on_state,
    )

    stream.start()

    # Wait for a few reconnections
    # The loop is: Start -> Wait Process -> Process Exit -> Wait Delay -> Start ...
    # 0.01s delay is very short, so 0.1s sleep should be enough for multiple cycles
    time.sleep(0.1)

    stream.stop()
    stream.join(timeout=1.0)

    # Verify Popen was called multiple times (initial + at least one reconnect)
    assert mock_popen.call_count >= 2

    # Verify state transitions
    # Should see STARTING -> RUNNING -> RECONNECTING -> RUNNING ...
    # -> STOPPING -> STOPPED
    assert StreamState.STARTING in state_changes
    assert StreamState.RUNNING in state_changes
    assert StreamState.RECONNECTING in state_changes
    assert StreamState.STOPPED in state_changes

    # Verify that RECONNECTING appears between RUNNING states
    # Filter to just RUNNING and RECONNECTING to check the sequence
    run_reconnect_seq = [
        s for s in state_changes if s in {StreamState.RUNNING, StreamState.RECONNECTING}
    ]
    # Should look like [RUNNING, RECONNECTING, RUNNING, RECONNECTING, ...]
    # Just check that we have at least one transition from RECONNECTING to RUNNING
    # Note: The first state is RUNNING. Then it exits -> RECONNECTING -> RUNNING.
    assert len(run_reconnect_seq) >= 3


def test_read_timeout(mock_popen, mocker) -> None:
    """Test that read timeout terminates the process."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    # Mock Popen
    process_mock = mock_popen.return_value

    # Mock readline to block forever (simulate hang)
    # We use a side effect that sleeps longer than the timeout
    def blocking_readline(*args, **kwargs):
        time.sleep(2.0)
        return ""

    process_mock.stdout.readline.side_effect = blocking_readline

    # Mock wait to block until terminated
    process_mock.wait.side_effect = lambda *args, **kwargs: time.sleep(0.5)

    # Mock poll to return None (running) initially
    process_mock.poll.return_value = None

    # Use a small timeout, but we know watchdog sleeps 1s
    stream = LogStream(read_timeout=0.1)
    stream.start()

    # Wait for watchdog to fire (it sleeps 1s, so wait > 1s)
    time.sleep(1.5)

    stream.stop()
    stream.join(timeout=1.0)

    # Verify terminate was called
    process_mock.terminate.assert_called()


def test_queue_full_warning_rate_limited(caplog, mocker) -> None:
    """Test queue-full warnings are rate-limited in sync stream."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    stream = LogStream(max_queue_size=1, queue_full_warning_interval=60.0)

    # Fill queue to force queue.Full for subsequent puts.
    dummy_entry = LogEntry(
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        pid=1,
        tid=1,
        level="I",
        tag="T",
        message="M",
        raw="M",
    )
    stream._callback_queue.put_nowait(([dummy_entry], "stdout"))

    with caplog.at_level(logging.WARNING, logger="logpyt"):
        stream._process_line("line one\n", "stdout")
        stream._process_line("line two\n", "stdout")
        stream._process_line("line three\n", "stdout")

    queue_warnings = [
        rec for rec in caplog.records if "callback queue is full" in rec.message
    ]
    assert len(queue_warnings) == 1


def test_stop_does_not_hang_when_callback_queue_is_full(mock_popen, mocker) -> None:
    """Stopping should complete even if callback queue is saturated."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    process_mock = mock_popen.return_value
    process_mock.stdout.readline.side_effect = [
        "line-1\n",
        "line-2\n",
        "line-3\n",
        "",
    ]
    process_mock.stderr.readline.return_value = ""
    process_mock.wait.side_effect = lambda *args, **kwargs: time.sleep(0.1)

    callback_entered = threading.Event()
    release_callback = threading.Event()

    def blocking_callback(entry, handle):
        if entry.message == "line-1":
            callback_entered.set()
            release_callback.wait(timeout=2.0)

    stream = LogStream(
        stdout_callback=blocking_callback,
        max_queue_size=1,
    )

    try:
        stream.start()
        assert callback_entered.wait(timeout=1.0)

        deadline = time.time() + 1.0
        while stream._callback_queue.qsize() < 1 and time.time() < deadline:
            time.sleep(0.01)

        assert stream._callback_queue.qsize() == 1

        stream.stop()
        stream.join(timeout=0.5)
    finally:
        release_callback.set()
        stream.stop()
        with suppress(LogStreamError):
            stream.join(timeout=1.0)


def test_join_waits_for_callback_thread_completion(mock_popen, mocker) -> None:
    """join() should not return while callback worker is still running."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    process_mock = mock_popen.return_value
    process_mock.stdout.readline.side_effect = ["line-1\n", ""]
    process_mock.stderr.readline.return_value = ""
    process_mock.wait.side_effect = lambda *args, **kwargs: time.sleep(0.1)

    callback_started = threading.Event()
    release_callback = threading.Event()

    def blocking_callback(entry, handle):
        del entry, handle
        callback_started.set()
        release_callback.wait(timeout=3.0)

    stream = LogStream(
        stdout_callback=blocking_callback,
        max_queue_size=1,
    )

    try:
        stream.start()
        assert callback_started.wait(timeout=1.0)

        stream.stop()
        stream.join(timeout=1.0)

        assert stream._callback_thread is not None
        assert not stream._callback_thread.is_alive()
    finally:
        release_callback.set()
        stream.stop()
        with suppress(LogStreamError):
            stream.join(timeout=1.0)


def test_join_timeout_honored_when_callback_thread_blocked(mock_popen, mocker) -> None:
    """join(timeout=...) should time out even if callback thread is blocked."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    process_mock = mock_popen.return_value
    process_mock.stdout.readline.side_effect = ["line-1\n", ""]
    process_mock.stderr.readline.return_value = ""
    process_mock.wait.return_value = 0

    callback_started = threading.Event()
    release_callback = threading.Event()

    def blocking_callback(entry, handle):
        del entry, handle
        callback_started.set()
        release_callback.wait(timeout=1.0)

    stream = LogStream(
        stdout_callback=blocking_callback,
        max_queue_size=1,
    )

    try:
        stream.start()
        assert callback_started.wait(timeout=1.0)

        stream.stop()

        assert stream._connection_thread is not None
        stream._connection_thread.join(timeout=1.0)
        assert not stream._connection_thread.is_alive()

        assert stream._callback_thread is not None
        assert stream._callback_thread.is_alive()

        start = time.monotonic()
        with pytest.raises(LogStreamTimeoutError):
            stream.join(timeout=0.05)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5
    finally:
        release_callback.set()
        stream.stop()
        with suppress(LogStreamError):
            stream.join(timeout=1.0)


def test_sync_queue_overflow_policy_drop_oldest_is_configurable(mocker) -> None:
    """Queue overflow policy should allow replacing stale queued items."""
    mocker.patch("logpyt.streams.sync.resolve_adb", return_value="adb")

    stream = LogStream(
        max_queue_size=1,
        queue_overflow_policy="drop_oldest",
    )

    old_entry = LogEntry(
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        pid=1,
        tid=1,
        level="I",
        tag="T",
        message="old",
        raw="old",
    )
    stream._callback_queue.put_nowait(([old_entry], "stdout"))

    parser = Mock()
    parser.parse_stdout.return_value = LogEntry(
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        pid=1,
        tid=1,
        level="I",
        tag="T",
        message="new",
        raw="new",
    )
    stream.parser = parser

    stream._process_line("new\n", "stdout")

    callback_item = stream._callback_queue.get_nowait()
    assert callback_item is not None
    queued_items, source = callback_item
    assert source == "stdout"
    assert len(queued_items) == 1
    emitted = queued_items[0]
    assert isinstance(emitted, LogEntry)
    assert emitted.message == "new"
