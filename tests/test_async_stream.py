import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from logpyt.models import LogEntry
from logpyt.streams.async_stream import AsyncLogStream, AsyncPidMonitor, StreamState

# Fixtures


async def wait_for_state(stream, state):
    while stream.state != state:
        await asyncio.sleep(0.01)


@pytest.fixture
def mock_resolve_adb():
    with patch("logpyt.streams.async_stream.resolve_adb", return_value="adb") as mock:
        yield mock


@pytest.fixture
def mock_process():
    process = MagicMock()
    process.stdout = AsyncMock()
    process.stderr = AsyncMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"", b""))
    process.terminate = MagicMock()
    process.kill = MagicMock()
    process.wait = AsyncMock(return_value=0)
    return process


@pytest.fixture
def mock_create_subprocess(mock_process):
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock:
        mock.return_value = mock_process
        yield mock


# Tests for AsyncLogStream


@pytest.mark.asyncio
async def test_lifecycle(mock_resolve_adb, mock_create_subprocess, mock_process):
    """Test start, stop, and join lifecycle of AsyncLogStream."""
    # Setup mock stdout to return EOF immediately to avoid infinite loops if not stopped
    mock_process.stdout.readline.return_value = b""
    mock_process.stderr.readline.return_value = b""

    stream = AsyncLogStream()

    # Make the process wait block so we can catch the RUNNING state
    async def delayed_wait():
        await asyncio.sleep(0.5)
        return 0

    mock_process.wait.side_effect = delayed_wait

    # Test Start
    await stream.start()

    # Wait for state to become RUNNING
    try:
        await asyncio.wait_for(wait_for_state(stream, StreamState.RUNNING), timeout=1.0)
    except asyncio.TimeoutError:
        pass  # Assertion will fail below if state is wrong

    assert stream.state == StreamState.RUNNING
    mock_create_subprocess.assert_called_once()
    args = mock_create_subprocess.call_args[0]
    assert args[0] == "adb"
    assert args[1] == "logcat"

    # Test Stop
    await stream.stop()
    # State might be STOPPING or STOPPED depending on race, but eventually STOPPED
    assert stream.state in (StreamState.STOPPING, StreamState.STOPPED)
    mock_process.terminate.assert_called_once()

    # Test Join
    await stream.join(timeout=1.0)
    assert stream.state == StreamState.STOPPED


@pytest.mark.asyncio
async def test_callbacks(mock_resolve_adb, mock_create_subprocess, mock_process):
    """Verify stdout_callback is called with parsed LogEntry."""
    # Mock stdout to return one line then EOF
    log_line = b"Test log message\n"
    mock_process.stdout.readline.side_effect = [log_line, b""]
    mock_process.stderr.readline.return_value = b""

    # Mock callback
    callback = AsyncMock()

    stream = AsyncLogStream(stdout_callback=callback)

    await stream.start()

    # Wait for the stream to process the line
    # We can join, but since readline returns EOF, the loop should finish naturally
    await stream.join(timeout=1.0)

    assert callback.call_count == 1
    call_args = callback.call_args
    entry = call_args[0][0]
    handle = call_args[0][1]

    assert isinstance(entry, LogEntry)
    assert entry.message == "Test log message"
    assert handle._stream == stream


@pytest.mark.asyncio
async def test_context_manager(mock_resolve_adb, mock_create_subprocess, mock_process):
    """Verify async with AsyncLogStream(...) works."""
    mock_process.stdout.readline.return_value = b""
    mock_process.stderr.readline.return_value = b""

    async with AsyncLogStream() as stream:
        # Make the process wait block so we can catch the RUNNING state
        async def delayed_wait():
            await asyncio.sleep(0.5)
            return 0

        mock_process.wait.side_effect = delayed_wait

        # Wait for state to become RUNNING
        try:
            await asyncio.wait_for(
                wait_for_state(stream, StreamState.RUNNING), timeout=1.0
            )
        except asyncio.TimeoutError:
            pass

        assert stream.state == StreamState.RUNNING
        mock_create_subprocess.assert_called_once()

    # After exit, it should be stopped
    assert stream.state == StreamState.STOPPED
    mock_process.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_pid_monitor(mock_resolve_adb):
    """Verify AsyncPidMonitor resolves PIDs correctly."""

    # We need to mock create_subprocess_exec specifically for the pidof call
    # The AsyncPidMonitor calls: adb shell pidof <package>

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        # Setup the mock process for pidof
        pidof_process = MagicMock()
        pidof_process.returncode = 0
        # communicate returns (stdout, stderr)
        pidof_process.communicate = AsyncMock(return_value=(b"1234 5678\n", b""))

        mock_exec.return_value = pidof_process

        monitor = AsyncPidMonitor(
            adb_path="adb", device_id=None, packages=["com.example.app"]
        )

        await monitor.start()

        # Give the monitor loop a moment to run
        await asyncio.sleep(0.1)

        # Check if PIDs are resolved
        pkg1 = await monitor.get_package(1234)
        pkg2 = await monitor.get_package(5678)
        pkg3 = await monitor.get_package(9999)

        assert pkg1 == "com.example.app"
        assert pkg2 == "com.example.app"
        assert pkg3 is None

        await monitor.stop()


@pytest.mark.asyncio
async def test_async_pid_monitor_update_no_change_keeps_map_object():
    """No-op update should keep existing map object and report no change."""
    monitor = AsyncPidMonitor(adb_path="adb", device_id=None, packages=["pkg"])
    monitor._pid_map = {1234: "pkg"}
    original = monitor._pid_map

    changed = await monitor._update_pid_map({1234: "pkg"})

    assert changed is False
    assert monitor._pid_map is original
    assert monitor._pid_map == {1234: "pkg"}


@pytest.mark.asyncio
async def test_async_pid_monitor_update_applies_add_remove_and_change():
    """Changed snapshot should be applied with add/remove semantics."""
    monitor = AsyncPidMonitor(adb_path="adb", device_id=None, packages=["pkg"])
    monitor._pid_map = {
        1111: "old.pkg",
        2222: "pkg",
    }

    changed = await monitor._update_pid_map(
        {
            2222: "pkg",
            3333: "pkg",
        }
    )

    assert changed is True
    assert monitor._pid_map == {
        2222: "pkg",
        3333: "pkg",
    }


@pytest.mark.asyncio
@patch("logpyt.streams.async_stream.AsyncPidMonitor._resolve_pids", new_callable=AsyncMock)
async def test_async_pid_monitor_run_loop_adaptive_backoff_when_unchanged(mock_resolve):
    """Poll interval should back off up to max when snapshots do not change."""
    monitor = AsyncPidMonitor(
        adb_path="adb",
        device_id=None,
        packages=["pkg"],
        poll_interval=1.0,
        max_poll_interval=4.0,
        poll_backoff_factor=2.0,
    )
    mock_resolve.return_value = []

    intervals: list[float] = []

    async def fake_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        intervals.append(timeout)
        if len(intervals) >= 3:
            monitor._stop_event.set()
            return True
        raise asyncio.TimeoutError

    with patch("logpyt.streams.async_stream.asyncio.wait_for", side_effect=fake_wait_for):
        await monitor._run()

    assert intervals == [2.0, 4.0, 4.0]


@pytest.mark.asyncio
@patch("logpyt.streams.async_stream.AsyncPidMonitor._resolve_pids", new_callable=AsyncMock)
@patch("logpyt.streams.async_stream.AsyncPidMonitor._update_pid_map", new_callable=AsyncMock)
async def test_async_pid_monitor_run_loop_resets_backoff_on_change(
    mock_update, mock_resolve
):
    """Poll interval should reset to base quickly when a change is detected."""
    monitor = AsyncPidMonitor(
        adb_path="adb",
        device_id=None,
        packages=["pkg"],
        poll_interval=1.0,
        max_poll_interval=4.0,
        poll_backoff_factor=2.0,
    )
    mock_resolve.return_value = []
    mock_update.side_effect = [False, False, True]

    intervals: list[float] = []

    async def fake_wait_for(awaitable, timeout):
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        intervals.append(timeout)
        if len(intervals) >= 3:
            monitor._stop_event.set()
            return True
        raise asyncio.TimeoutError

    with patch("logpyt.streams.async_stream.asyncio.wait_for", side_effect=fake_wait_for):
        await monitor._run()

    assert intervals == [2.0, 4.0, 1.0]


@pytest.mark.asyncio
async def test_stream_with_pid_monitor(
    mock_resolve_adb, mock_create_subprocess, mock_process
):
    """Verify AsyncLogStream integrates with AsyncPidMonitor when filters are present."""
    from logpyt.filters import Filter

    # Setup mocks
    # We need to handle two types of subprocess calls: logcat and pidof
    # We can use side_effect on create_subprocess_exec to return different mocks

    logcat_process = mock_process
    logcat_process.stderr.readline.return_value = b""

    pidof_process = MagicMock()
    pidof_process.returncode = 0
    pidof_process.communicate = AsyncMock(return_value=(b"1234\n", b""))

    # We need to delay the log line to allow the PID monitor to run and populate the map
    called = False

    async def delayed_readline(*args, **kwargs):
        nonlocal called
        if not called:
            called = True
            await asyncio.sleep(0.1)
            return b"Log line from PID 1234\n"
        return b""

    logcat_process.stdout.readline.side_effect = delayed_readline

    def side_effect(*args, **kwargs):
        # args[0] is the program, but here it's passed as *cmd so args will be the command parts
        # create_subprocess_exec(program, *args, ...)
        # But the code calls it as: create_subprocess_exec(*cmd, ...)
        # So the first arg is the adb path

        cmd_args = args
        if "logcat" in cmd_args:
            return logcat_process
        if "pidof" in cmd_args:
            return pidof_process
        return MagicMock()  # Fallback

    mock_create_subprocess.side_effect = side_effect

    # Create a filter that requires PID monitoring
    flt = Filter(package="com.example.app")

    # Mock parser to return a LogEntry with PID 1234
    mock_parser = MagicMock()
    mock_parser.parse_stdout.return_value = LogEntry(
        timestamp=datetime.now(),
        pid=1234,
        tid=1,
        level="D",
        tag="Test",
        message="Log line",
        raw="Log line from PID 1234",
        meta={},
    )

    callback = AsyncMock()

    stream = AsyncLogStream(filter_by=flt, parser=mock_parser, stdout_callback=callback)

    await stream.start()

    # Wait for processing
    await asyncio.sleep(0.2)
    await stream.stop()
    await stream.join()

    # Verify callback was called
    assert callback.call_count == 1
    entry = callback.call_args[0][0]

    # Verify package was added to meta (AsyncPidMonitor should have found it)
    assert entry.meta.get("package") == "com.example.app"


@pytest.mark.asyncio
async def test_auto_reconnect(mock_resolve_adb, mock_create_subprocess):
    """Test that the stream automatically reconnects when the process exits."""

    # Setup mock to return a process that exits immediately
    def create_mock_process(*args, **kwargs):
        process = MagicMock()
        process.stdout = AsyncMock()
        process.stdout.readline.return_value = b""  # EOF immediately
        process.stderr = AsyncMock()
        process.stderr.readline.return_value = b""
        process.wait = AsyncMock(return_value=0)
        process.terminate = MagicMock()
        process.kill = MagicMock()
        return process

    mock_create_subprocess.side_effect = create_mock_process

    state_changes = []

    async def on_state(state):
        state_changes.append(state)

    # Use a short reconnect delay to speed up the test
    stream = AsyncLogStream(
        auto_reconnect=True,
        reconnect_delay=0.01,
        on_state=on_state,
    )

    await stream.start()

    # Wait for a few reconnections
    await asyncio.sleep(0.1)

    await stream.stop()
    await stream.join(timeout=1.0)

    # Verify create_subprocess_exec was called multiple times
    assert mock_create_subprocess.call_count >= 2

    # Verify state transitions
    assert StreamState.STARTING in state_changes
    assert StreamState.RUNNING in state_changes
    assert StreamState.RECONNECTING in state_changes
    assert StreamState.STOPPED in state_changes

    # Verify sequence
    run_reconnect_seq = [
        s for s in state_changes if s in (StreamState.RUNNING, StreamState.RECONNECTING)
    ]
    # Should look like [RUNNING, RECONNECTING, RUNNING, ...]
    assert len(run_reconnect_seq) >= 3


@pytest.mark.asyncio
async def test_ingestion_not_blocked_by_slow_callback(
    mock_resolve_adb, mock_create_subprocess, mock_process
):
    """Ensure parsing/ingestion continues while callback is awaiting."""
    mock_process.stdout.readline.side_effect = [b"line-1\n", b"line-2\n", b""]
    mock_process.stderr.readline.return_value = b""

    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()

    async def slow_callback(entry, handle):
        if entry.message == "first":
            callback_entered.set()
            await release_callback.wait()

    mock_parser = MagicMock()
    mock_parser.parse_stdout.side_effect = [
        LogEntry(
            timestamp=datetime.now(),
            pid=1,
            tid=1,
            level="D",
            tag="T",
            message="first",
            raw="line-1",
            meta={},
        ),
        LogEntry(
            timestamp=datetime.now(),
            pid=1,
            tid=1,
            level="D",
            tag="T",
            message="second",
            raw="line-2",
            meta={},
        ),
    ]

    stream = AsyncLogStream(
        parser=mock_parser,
        stdout_callback=slow_callback,
        callback_queue_size=8,
    )

    await stream.start()
    await asyncio.wait_for(callback_entered.wait(), timeout=1.0)

    # Ingestion should have parsed line-2 already, despite callback waiting.
    await asyncio.sleep(0.05)
    assert mock_parser.parse_stdout.call_count == 2

    release_callback.set()
    await stream.join(timeout=1.0)
