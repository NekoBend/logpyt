"""Tests for log stream."""

import time
from datetime import datetime
from unittest.mock import Mock

import pytest

from logpyt.exceptions import LogStreamInternalError
from logpyt.filters import Filter
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
    assert stream.state in (StreamState.STOPPING, StreamState.STOPPED)

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
        assert handle.state in (StreamState.STOPPING, StreamState.STOPPED)


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
        timestamp=datetime.now(),
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
    # Should see STARTING -> RUNNING -> RECONNECTING -> RUNNING ... -> STOPPING -> STOPPED
    assert StreamState.STARTING in state_changes
    assert StreamState.RUNNING in state_changes
    assert StreamState.RECONNECTING in state_changes
    assert StreamState.STOPPED in state_changes

    # Verify that RECONNECTING appears between RUNNING states
    # Filter to just RUNNING and RECONNECTING to check the sequence
    run_reconnect_seq = [
        s for s in state_changes if s in (StreamState.RUNNING, StreamState.RECONNECTING)
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
