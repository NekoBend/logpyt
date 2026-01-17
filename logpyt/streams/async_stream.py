"""Async log stream implementation for capturing and processing ADB logcat output."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from ..exceptions import (
    LogStreamError,
    LogStreamInternalError,
    LogStreamKilledError,
    LogStreamTimeoutError,
)
from ..filters import Filter
from ..groupers import LogGrouper, WindowedLogGrouper
from ..models import LogEntry
from ..parsers import LogParser
from ..utils import resolve_adb
from .common import StreamState, build_pidof_command

# Configure module logger
logger = logging.getLogger(__name__)


class AsyncPidMonitor:
    """Monitors PIDs for specific packages using ADB asynchronously."""

    def __init__(
        self,
        adb_path: str,
        device_id: str | None,
        packages: list[str],
        poll_interval: float = 5.0,
    ) -> None:
        """Initialize the PID monitor.

        Args:
            adb_path: Path to ADB executable.
            device_id: Target device serial ID.
            packages: List of package names to monitor.
            poll_interval: Interval in seconds between PID polls.
        """
        self.adb_path = adb_path
        self.device_id = device_id
        self.packages = list(set(packages))
        self.poll_interval = poll_interval
        self._pid_map: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the monitoring task."""
        if not self.packages:
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="AsyncPidMonitor")

    async def stop(self) -> None:
        """Stop the monitoring task."""
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if self._task and not self._task.done():
                    self._task.cancel()
                    try:
                        await self._task
                    except asyncio.CancelledError:
                        pass

    async def get_package(self, pid: int) -> str | None:
        """Get the package name for a given PID.

        Args:
            pid: The process ID.

        Returns:
            The package name if found, None otherwise.
        """
        async with self._lock:
            return self._pid_map.get(pid)

    async def _run(self) -> None:
        """Internal loop to poll PIDs."""
        while not self._stop_event.is_set():
            new_map: dict[int, str] = {}
            for package in self.packages:
                pids = await self._resolve_pids(package)
                for pid in pids:
                    new_map[pid] = package

            async with self._lock:
                self._pid_map = new_map

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval
                )
            except asyncio.TimeoutError:
                continue

    async def _resolve_pids(self, package: str) -> list[int]:
        """Resolve PIDs for a package using ADB."""
        cmd = build_pidof_command(self.adb_path, self.device_id, package)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)

            if process.returncode == 0 and stdout:
                output = stdout.decode("utf-8", errors="replace").strip()
                if output:
                    return [int(p) for p in output.split()]
        except Exception:
            # Ignore errors (e.g. device disconnected, command failed)
            pass
        return []


class AsyncStreamHandle:
    """Handle to control an AsyncLogStream instance."""

    def __init__(self, stream: AsyncLogStream) -> None:
        """Initialize the handle.

        Args:
            stream: The AsyncLogStream instance to control.
        """
        self._stream = stream

    async def stop(self) -> None:
        """Stop the log stream gracefully."""
        await self._stream.stop()

    async def kill(self) -> None:
        """Kill the log stream immediately."""
        await self._stream.kill()

    async def pause(self) -> None:
        """Pause dispatching of log entries."""
        await self._stream.pause()

    async def resume(self) -> None:
        """Resume dispatching of log entries."""
        await self._stream.resume()

    async def join(self, timeout: float | None = None) -> None:
        """Wait for the stream to finish."""
        await self._stream.join(timeout)

    @property
    def state(self) -> StreamState:
        """Get the current state of the stream."""
        return self._stream.state


# Type aliases for callbacks
AsyncLogCallback = Callable[[Any, AsyncStreamHandle], Awaitable[None]]
AsyncStateCallback = Callable[[StreamState], Awaitable[None]]
AsyncErrorCallback = Callable[[Exception], Awaitable[None]]
AsyncLifecycleCallback = Callable[[], Awaitable[None]]


class AsyncLogStream:
    """Manages an ADB logcat subprocess and processes its output asynchronously.

    This class handles the lifecycle of the `adb logcat` process using asyncio,
    parses its output into `LogEntry` objects, applies filters and groupers,
    and dispatches the results to user-provided async callbacks.

    Usage:
        ```python
        from logpyt.streams.async_stream import AsyncLogStream, AsyncStreamHandle
        from logpyt.models import LogEntry

        async def my_callback(entry: LogEntry, handle: AsyncStreamHandle) -> None:
            if entry.tag == "MyApp":
                print(f"MyApp Log: {entry.message}")

        async def main():
            async with AsyncLogStream(stdout_callback=my_callback) as stream:
                await asyncio.sleep(10)
        ```
    """

    def __init__(
        self,
        adb_path: str | None = None,
        device_id: str | None = None,
        parser: LogParser | None = None,
        filter_by: Filter | None = None,
        group_by: LogGrouper | None = None,
        stdout_callback: AsyncLogCallback | None = None,
        stderr_callback: AsyncLogCallback | None = None,
        on_start: AsyncLifecycleCallback | None = None,
        on_stop: AsyncLifecycleCallback | None = None,
        on_state: AsyncStateCallback | None = None,
        on_error: AsyncErrorCallback | None = None,
        logcat_args: Sequence[str] | None = None,
        group_keys: Sequence[str] | None = None,
        group_interval: float | None = None,
        group_mode: Literal["consecutive", "windowed"] = "consecutive",
        auto_reconnect: bool = False,
        reconnect_delay: float = 1.0,
        pid_poll_interval: float = 5.0,
        read_timeout: float | None = None,
    ) -> None:
        """Initialize the AsyncLogStream.

        Args:
            adb_path: Path to ADB executable. If None, resolved automatically.
            device_id: Target device serial ID.
            parser: Parser to convert lines to LogEntry objects.
            filter_by: Filter to apply to log entries.
            group_by: Grouper to group log entries.
            stdout_callback: Async callback for processed stdout entries.
            stderr_callback: Async callback for processed stderr entries.
            on_start: Async hook called when stream starts.
            on_stop: Async hook called when stream stops.
            on_state: Async hook called when state changes.
            on_error: Async hook called when an error occurs.
            logcat_args: Additional arguments for `adb logcat`.
            group_keys: Shortcut to create a grouper. Fields to group by.
            group_interval: Shortcut to create a grouper. Time threshold in ms.
            group_mode: Shortcut to create a grouper. "consecutive" or "windowed".
            auto_reconnect: Whether to automatically reconnect when the process exits.
            reconnect_delay: Delay in seconds before reconnecting.
            pid_poll_interval: Interval in seconds for PID monitoring.
            read_timeout: Timeout in seconds for reading from the stream.
        """
        self.adb_path = adb_path or resolve_adb()
        self.device_id = device_id
        self.parser = parser or LogParser()
        self.filter_by = filter_by
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay
        self.pid_poll_interval = pid_poll_interval
        self.read_timeout = read_timeout

        # Handle grouper shortcut
        self.group_by = group_by
        if self.group_by is None and group_keys and group_interval is not None:
            if group_mode == "windowed":
                self.group_by = WindowedLogGrouper(
                    by=group_keys,
                    threshold_ms=group_interval,
                    emit_mode="group",
                )
            else:
                self.group_by = LogGrouper(
                    by=group_keys,
                    threshold_ms=group_interval,
                    emit_mode="group",
                )

        self.stdout_callback = stdout_callback
        self.stderr_callback = stderr_callback
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_state = on_state
        self.on_error = on_error
        self.logcat_args = list(logcat_args) if logcat_args else []

        self._pid_monitor: AsyncPidMonitor | None = None
        if isinstance(self.filter_by, Filter) and self.filter_by.packages:
            packages = list(self.filter_by.packages)
            self._pid_monitor = AsyncPidMonitor(
                self.adb_path,
                self.device_id,
                packages,
                poll_interval=self.pid_poll_interval,
            )

        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._state = StreamState.IDLE
        self._state_lock = asyncio.Lock()
        self._handle = AsyncStreamHandle(self)
        self._stop_event = asyncio.Event()
        self._exceptions: list[Exception] = []

    @property
    def state(self) -> StreamState:
        """Current state of the stream."""
        # Note: Accessing state without lock for property read is generally safe
        # for simple enums, but strictly speaking should be locked.
        # However, we can't make property async.
        return self._state

    async def _set_state(self, new_state: StreamState) -> None:
        """Update state and trigger callback."""
        async with self._state_lock:
            if self._state == new_state:
                return
            self._state = new_state

        if self.on_state:
            try:
                await self.on_state(new_state)
            except Exception as e:
                logger.error(f"Error in on_state callback: {e}")

    async def start(self) -> None:
        """Start the log stream."""
        async with self._state_lock:
            if self._state != StreamState.IDLE and self._state != StreamState.STOPPED:
                raise LogStreamInternalError(
                    f"Cannot start stream from state {self._state}"
                )
            self._state = StreamState.STARTING
            # We manually trigger callback outside lock to avoid deadlocks if callback calls back

        if self.on_state:
            await self.on_state(StreamState.STARTING)

        self._stop_event.clear()
        self._connection_task = asyncio.create_task(
            self._connection_manager(), name="AsyncLogStream-ConnectionManager"
        )

    async def _connection_manager(self) -> None:
        """Manage the ADB process lifecycle and reconnection."""
        while not self._stop_event.is_set():
            await self._run_process_lifecycle()

            if self._stop_event.is_set():
                break

            if not self.auto_reconnect:
                break

            await self._set_state(StreamState.RECONNECTING)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.reconnect_delay
                )
            except asyncio.TimeoutError:
                pass  # Timeout, continue to reconnect
            except asyncio.CancelledError:
                break

        # Final cleanup
        async with self._state_lock:
            if self._state != StreamState.KILLED:
                # Don't use _set_state here to avoid potential deadlock if we are cancelling?
                # Actually _set_state is fine as long as we are not holding lock when calling callbacks
                pass

        if self.state != StreamState.KILLED:
            await self._set_state(StreamState.STOPPED)
            if self.on_stop:
                await self.on_stop()

    async def _run_process_lifecycle(self) -> None:
        """Run a single lifecycle of the ADB process."""
        cmd = [self.adb_path]
        if self.device_id:
            cmd.extend(["-s", self.device_id])
        cmd.append("logcat")
        cmd.extend(self.logcat_args)

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            await self._set_state(StreamState.STOPPED)
            if self.on_error:
                await self.on_error(e)
            return

        if self._process.stdout:
            self._stdout_task = asyncio.create_task(
                self._read_loop(self._process.stdout, "stdout"),
                name="AsyncLogStream-Stdout",
            )
        if self._process.stderr:
            self._stderr_task = asyncio.create_task(
                self._read_loop(self._process.stderr, "stderr"),
                name="AsyncLogStream-Stderr",
            )

        if self._pid_monitor:
            await self._pid_monitor.start()

        await self._set_state(StreamState.RUNNING)
        if self.on_start:
            await self.on_start()

        # Wait for process to exit
        try:
            await self._process.wait()
        except asyncio.CancelledError:
            # If connection manager is cancelled, we should stop everything
            return

        # Wait for tasks to finish reading
        tasks = []
        if self._stdout_task:
            tasks.append(self._stdout_task)
        if self._stderr_task:
            tasks.append(self._stderr_task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._pid_monitor:
            await self._pid_monitor.stop()

    async def stop(self) -> None:
        """Stop the log stream gracefully."""
        async with self._state_lock:
            if self._state in (
                StreamState.STOPPED,
                StreamState.KILLED,
                StreamState.IDLE,
            ):
                return
            # Don't set state here, let _set_state handle it to trigger callbacks

        await self._set_state(StreamState.STOPPING)

        self._stop_event.set()
        if self._pid_monitor:
            await self._pid_monitor.stop()

        if self._process:
            try:
                self._process.terminate()
                # We don't await wait() here immediately, join() does that
            except ProcessLookupError:
                pass

    async def kill(self) -> None:
        """Kill the log stream immediately."""
        await self._set_state(StreamState.KILLED)

        self._stop_event.set()
        if self._pid_monitor:
            await self._pid_monitor.stop()

        if self._process:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass

    async def pause(self) -> None:
        """Pause dispatching of log entries."""
        async with self._state_lock:
            if self._state == StreamState.RUNNING:
                self._state = StreamState.PAUSED
                # Trigger callback manually if needed, or use _set_state

        # Using _set_state is safer for callbacks
        if self.state == StreamState.RUNNING:
            await self._set_state(StreamState.PAUSED)

    async def resume(self) -> None:
        """Resume dispatching of log entries."""
        if self.state == StreamState.PAUSED:
            await self._set_state(StreamState.RUNNING)

    async def join(self, timeout: float | None = None) -> None:
        """Wait for the stream to finish.

        Args:
            timeout: Maximum time to wait in seconds.

        Raises:
            LogStreamTimeoutError: If timeout expires.
            LogStreamKilledError: If stream was killed.
            LogStreamInternalError: If an internal error occurred.
        """
        if not self._connection_task:
            return

        try:
            await asyncio.wait_for(self._connection_task, timeout=timeout)
        except asyncio.TimeoutError:
            raise LogStreamTimeoutError("Timeout waiting for connection task")

        if self.state == StreamState.KILLED:
            raise LogStreamKilledError("Stream was killed")

        if self._exceptions:
            exc = self._exceptions[0]
            if isinstance(exc, LogStreamError):
                raise exc
            raise LogStreamInternalError(
                f"Internal error in stream task: {exc}"
            ) from exc

    async def _read_loop(self, stream: asyncio.StreamReader, source: str) -> None:
        """Internal loop to read from a stream."""
        try:
            while not self._stop_event.is_set():
                if self.read_timeout:
                    try:
                        line_bytes = await asyncio.wait_for(
                            stream.readline(), timeout=self.read_timeout
                        )
                    except asyncio.TimeoutError:
                        # Terminate process to unblock connection manager
                        if self._process:
                            try:
                                self._process.terminate()
                            except ProcessLookupError:
                                pass
                        raise
                else:
                    line_bytes = await stream.readline()

                if not line_bytes:
                    # EOF
                    break

                line = line_bytes.decode("utf-8", errors="replace")

                if self.state == StreamState.PAUSED:
                    continue

                await self._process_line(line, source)
        except asyncio.TimeoutError as e:
            if self.on_error:
                await self.on_error(e)
        except Exception as e:
            self._exceptions.append(e)
            if self.on_error:
                await self.on_error(e)
        finally:
            if source == "stdout":
                # Flush grouper
                if self.group_by:
                    flushed = self.group_by.flush()
                    if flushed and self.stdout_callback:
                        for item in flushed:
                            try:
                                await self.stdout_callback(item, self._handle)
                            except Exception as e:
                                if self.on_error:
                                    await self.on_error(e)

    async def _process_line(self, line: str, source: str) -> None:
        """Process a single raw line."""
        # 1. Parse
        # Note: Parsing is CPU-bound. For extremely high throughput, this could be
        # offloaded to an executor, but for typical logcat usage, the overhead of
        # context switching per line outweighs the benefit.
        try:
            if source == "stdout":
                entry = self.parser.parse_stdout(line)
            else:
                entry = self.parser.parse_stderr(line)
        except Exception:
            return

        if self._pid_monitor:
            pkg = await self._pid_monitor.get_package(entry.pid)
            if pkg:
                entry.meta["package"] = pkg

        # 2. Filter
        if self.filter_by and not self.filter_by(entry):
            return

        # 3. Group
        # Note: Grouping is also CPU-bound but stateful.
        items_to_emit: list[LogEntry | list[LogEntry]] = []
        if self.group_by:
            # LogGrouper is synchronous, which is fine as it's CPU bound and fast
            items_to_emit = self.group_by.process(entry)
        else:
            items_to_emit = [entry]

        # 4. Dispatch
        callback = self.stdout_callback if source == "stdout" else self.stderr_callback
        if callback:
            for item in items_to_emit:
                try:
                    await callback(item, self._handle)
                except Exception as e:
                    if self.on_error:
                        await self.on_error(e)

    async def __aenter__(self) -> AsyncStreamHandle:
        """Start the stream and return the handle."""
        await self.start()
        return self._handle

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Stop the stream on exit."""
        await self.stop()
        try:
            await self.join(timeout=1.0)
        except (LogStreamTimeoutError, LogStreamKilledError):
            pass
