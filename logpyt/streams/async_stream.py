"""Async log stream implementation for capturing and processing ADB logcat output."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from logpyt.exceptions import (
    LogStreamError,
    LogStreamInternalError,
    LogStreamKilledError,
    LogStreamTimeoutError,
)
from logpyt.filters import Filter
from logpyt.groupers import LogGrouper, WindowedLogGrouper
from logpyt.parsers import LogParser, ThreadTimeLogParser
from logpyt.utils import resolve_adb

from .common import StreamState, build_pidof_command

if TYPE_CHECKING:
    from types import TracebackType

    from logpyt.models import LogEntry

# Configure module logger
logger = logging.getLogger(__name__)

_DISPATCH_STOP = object()

# Max bytes buffered while reading a single logcat line. The asyncio default is
# 64 KiB, which a single crafted/huge line can exceed and permanently stall the
# reader; a generous limit plus drain-and-continue avoids that DoS.
_READ_LIMIT = 8 * 1024 * 1024
# Upper bound on the graceful callback drain during dispatcher shutdown, so a stuck
# user callback cannot hang teardown.
_DISPATCH_DRAIN_TIMEOUT = 2.0


class AsyncPidMonitor:
    """Monitors PIDs for specific packages using ADB asynchronously."""

    def __init__(
        self,
        adb_path: str,
        device_id: str | None,
        packages: list[str],
        poll_interval: float = 5.0,
        max_poll_interval: float = 30.0,
        poll_backoff_factor: float = 2.0,
        max_resolves_per_cycle: int = 32,
    ) -> None:
        """Initialize the PID monitor.

        Args:
            adb_path: Path to ADB executable.
            device_id: Target device serial ID.
            packages: List of package names to monitor.
            poll_interval: Interval in seconds between PID polls.
            max_poll_interval: Maximum poll interval in seconds when idle.
            poll_backoff_factor: Multiplier applied after unchanged polls.
            max_resolves_per_cycle: Maximum packages to resolve per poll cycle.
        """
        self.adb_path = adb_path
        self.device_id = device_id
        self.packages = list(set(packages))
        self.poll_interval = max(0.1, poll_interval)
        self.max_poll_interval = max(self.poll_interval, max_poll_interval)
        self.poll_backoff_factor = max(1.0, poll_backoff_factor)
        self.max_resolves_per_cycle = max(1, max_resolves_per_cycle)
        self._pid_map: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._resolve_cursor = 0

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
            except (TimeoutError, asyncio.CancelledError):
                if self._task and not self._task.done():
                    self._task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._task

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
        current_interval = self.poll_interval
        while not self._stop_event.is_set():
            packages = self.packages
            total = len(packages)
            cycle_limit = min(total, self.max_resolves_per_cycle)
            polled_packages: list[str] = []
            resolved_map: dict[int, str] = {}
            for offset in range(cycle_limit):
                package = packages[(self._resolve_cursor + offset) % total]
                polled_packages.append(package)
                pids = await self._resolve_pids(package)
                for pid in pids:
                    resolved_map[pid] = package

            if cycle_limit == total:
                new_map = resolved_map
            else:
                # Only reconcile PIDs for packages polled this cycle; keep mappings
                # for unpolled packages so throttled polling stays correct.
                polled_package_set = set(polled_packages)
                async with self._lock:
                    new_map = {
                        pid: package
                        for pid, package in self._pid_map.items()
                        if package not in polled_package_set
                    }
                new_map.update(resolved_map)

            if total:
                self._resolve_cursor = (self._resolve_cursor + cycle_limit) % total

            changed = await self._update_pid_map(new_map)

            if changed:
                current_interval = self.poll_interval
            else:
                current_interval = min(
                    self.max_poll_interval,
                    current_interval * self.poll_backoff_factor,
                )

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=current_interval
                )
            except TimeoutError:
                continue

    async def _update_pid_map(self, new_map: dict[int, str]) -> bool:
        """Apply PID map changes in place.

        This avoids replacing the full mapping object when there are no changes,
        while preserving the same add/update/remove semantics.

        Args:
            new_map: Latest PID to package mapping snapshot.

        Returns:
            True if at least one mapping changed, False otherwise.
        """
        async with self._lock:
            if self._pid_map == new_map:
                return False

            current_pids = set(self._pid_map)
            new_pids = set(new_map)

            for stale_pid in current_pids - new_pids:
                del self._pid_map[stale_pid]

            for pid, package in new_map.items():
                if self._pid_map.get(pid) != package:
                    self._pid_map[pid] = package

            return True

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
                    return [
                        int(p) for p in output.split() if p.isdigit() and len(p) <= 7
                    ]
        except Exception as exc:  # noqa: BLE001
            # A polling monitor must never crash: any failure (device disconnected,
            # command failed, malformed output) degrades to returning no PIDs.
            logger.debug("pidof resolution failed for %s: %s", package, exc)
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
        auto_reconnect: bool = False,  # noqa: FBT001, FBT002  existing public signature
        reconnect_delay: float = 1.0,
        pid_poll_interval: float = 5.0,
        pid_max_poll_interval: float = 30.0,
        pid_poll_backoff_factor: float = 2.0,
        read_timeout: float | None = None,
        callback_queue_size: int = 1024,
        callback_queue_policy: Literal["drop_newest", "drop_oldest"] = "drop_newest",
        pid_max_resolves_per_cycle: int = 32,
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
            pid_max_poll_interval: Maximum PID poll interval when idle.
            pid_poll_backoff_factor: Backoff factor for unchanged PID snapshots.
            read_timeout: Timeout in seconds for reading from the stream.
            callback_queue_size: Max buffered callback items per stream source.
                Uses backpressure when full.
            callback_queue_policy: Policy for callback queue overflow.
                "drop_newest" drops incoming data, "drop_oldest" evicts the
                oldest queued item.
            pid_max_resolves_per_cycle: Maximum packages to resolve per PID poll cycle.
        """
        self.adb_path = adb_path or resolve_adb()
        self.device_id = device_id
        self.parser = parser or ThreadTimeLogParser()
        self.filter_by = filter_by
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay
        self.pid_poll_interval = pid_poll_interval
        self.pid_max_poll_interval = pid_max_poll_interval
        self.pid_poll_backoff_factor = pid_poll_backoff_factor
        self.read_timeout = read_timeout
        self.callback_queue_size = callback_queue_size
        self.callback_queue_policy = callback_queue_policy
        self.pid_max_resolves_per_cycle = pid_max_resolves_per_cycle

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
                max_poll_interval=self.pid_max_poll_interval,
                poll_backoff_factor=self.pid_poll_backoff_factor,
                max_resolves_per_cycle=self.pid_max_resolves_per_cycle,
            )

        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._stdout_dispatch_task: asyncio.Task[None] | None = None
        self._stderr_dispatch_task: asyncio.Task[None] | None = None
        self._stdout_dispatch_queue: asyncio.Queue[object] | None = None
        self._stderr_dispatch_queue: asyncio.Queue[object] | None = None
        self._state = StreamState.IDLE
        self._state_lock = asyncio.Lock()
        self._handle = AsyncStreamHandle(self)
        self._stop_event = asyncio.Event()
        self._exceptions: list[Exception] = []

    def _build_dispatch_queue(self) -> asyncio.Queue[object]:
        """Create a callback dispatch queue with optional backpressure."""
        if self.callback_queue_size <= 0:
            return asyncio.Queue()
        return asyncio.Queue(maxsize=self.callback_queue_size)

    async def _dispatch_item(
        self,
        item: LogEntry | list[LogEntry],
        source: str,
        callback: AsyncLogCallback,
    ) -> None:
        """Dispatch an item to callback queue or directly when queue is unavailable."""
        queue = (
            self._stdout_dispatch_queue
            if source == "stdout"
            else self._stderr_dispatch_queue
        )
        if queue is None:
            await callback(item, self._handle)
            return
        # Never block ingestion on a full queue: apply the overflow policy instead.
        if queue.full():
            if self.callback_queue_policy == "drop_oldest":
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                    queue.task_done()
            else:
                # drop_newest: drop the incoming item rather than blocking the reader.
                logger.debug("Dispatch queue full; dropping newest %s item", source)
                return
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(item)

    async def _callback_dispatch_loop(
        self,
        queue: asyncio.Queue[object],
        callback: AsyncLogCallback,
    ) -> None:
        """Consume queued callback items sequentially."""
        while True:
            item = await queue.get()
            try:
                if item is _DISPATCH_STOP:
                    return
                await callback(item, self._handle)
            except Exception as e:  # noqa: BLE001  user callback must not crash dispatcher
                logger.debug("Error in dispatch callback: %s", e)
                await self._invoke_callback(self.on_error, e)
            finally:
                queue.task_done()

    async def _stop_dispatcher(self, source: str) -> None:
        """Gracefully stop callback dispatcher for a source."""
        queue = (
            self._stdout_dispatch_queue
            if source == "stdout"
            else self._stderr_dispatch_queue
        )
        task = (
            self._stdout_dispatch_task
            if source == "stdout"
            else self._stderr_dispatch_task
        )
        if queue is None or task is None:
            return
        # Enqueue the stop sentinel without blocking: if the queue is full (slow or
        # stuck callback under backpressure), evict one item to make room so shutdown
        # cannot deadlock on put().
        try:
            queue.put_nowait(_DISPATCH_STOP)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
                queue.task_done()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(_DISPATCH_STOP)
        # Bound the graceful drain so a stuck user callback cannot hang shutdown.
        try:
            await asyncio.wait_for(queue.join(), timeout=_DISPATCH_DRAIN_TIMEOUT)
        except TimeoutError:
            logger.debug("Dispatcher drain timed out for %s; cancelling", source)
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if source == "stdout":
            self._stdout_dispatch_queue = None
            self._stdout_dispatch_task = None
        else:
            self._stderr_dispatch_queue = None
            self._stderr_dispatch_task = None

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
            except Exception as e:  # noqa: BLE001  user callback must not crash stream
                logger.error("Error in on_state callback: %s", e)

    async def start(self) -> None:
        """Start the log stream."""
        async with self._state_lock:
            if self._state not in {StreamState.IDLE, StreamState.STOPPED}:
                raise LogStreamInternalError(
                    f"Cannot start stream from state {self._state}"
                )
            self._state = StreamState.STARTING
            # We manually trigger the callback outside the lock to avoid
            # deadlocks if the callback calls back into the stream.

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
            except TimeoutError:
                pass  # Timeout, continue to reconnect
            except asyncio.CancelledError:
                break

        # Final cleanup
        async with self._state_lock:
            if self._state != StreamState.KILLED:
                # _set_state is fine here as long as we are not holding the
                # lock when calling callbacks.
                pass

        if self.state != StreamState.KILLED:
            await self._set_state(StreamState.STOPPED)
            await self._invoke_callback(self.on_stop)

    async def _run_process_lifecycle(self) -> None:  # noqa: PLR0912  sequential process lifecycle setup/teardown
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
                limit=_READ_LIMIT,
            )
        except Exception as e:  # noqa: BLE001  subprocess spawn failure must not crash manager
            logger.debug("Failed to start adb logcat subprocess: %s", e)
            self._exceptions.append(e)
            await self._set_state(StreamState.STOPPED)
            await self._invoke_callback(self.on_error, e)
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

        if self.stdout_callback:
            self._stdout_dispatch_queue = self._build_dispatch_queue()
            self._stdout_dispatch_task = asyncio.create_task(
                self._callback_dispatch_loop(
                    self._stdout_dispatch_queue,
                    self.stdout_callback,
                ),
                name="AsyncLogStream-StdoutDispatcher",
            )

        if self.stderr_callback:
            self._stderr_dispatch_queue = self._build_dispatch_queue()
            self._stderr_dispatch_task = asyncio.create_task(
                self._callback_dispatch_loop(
                    self._stderr_dispatch_queue,
                    self.stderr_callback,
                ),
                name="AsyncLogStream-StderrDispatcher",
            )

        if self._pid_monitor:
            await self._pid_monitor.start()

        await self._set_state(StreamState.RUNNING)
        await self._invoke_callback(self.on_start)

        # Wait for process to exit
        try:
            return_code = await self._process.wait()
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

        if self.stdout_callback:
            await self._stop_dispatcher("stdout")

        if self.stderr_callback:
            await self._stop_dispatcher("stderr")

        if self._pid_monitor:
            await self._pid_monitor.stop()

        if not self._stop_event.is_set() and return_code != 0:
            error = LogStreamInternalError(f"adb logcat exited with code {return_code}")
            self._exceptions.append(error)
            await self._invoke_callback(self.on_error, error)

    async def stop(self) -> None:
        """Stop the log stream gracefully."""
        async with self._state_lock:
            if self._state in {
                StreamState.STOPPED,
                StreamState.KILLED,
                StreamState.IDLE,
            }:
                return
            # Don't set state here, let _set_state handle it to trigger callbacks

        await self._set_state(StreamState.STOPPING)

        self._stop_event.set()
        if self._pid_monitor:
            await self._pid_monitor.stop()

        if self._process:
            # We don't await wait() here immediately, join() does that.
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()

    async def kill(self) -> None:
        """Kill the log stream immediately."""
        await self._set_state(StreamState.KILLED)

        self._stop_event.set()
        if self._pid_monitor:
            await self._pid_monitor.stop()

        if self._process:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()

    async def pause(self) -> None:
        """Pause dispatching of log entries."""
        async with self._state_lock:
            should_pause = self._state == StreamState.RUNNING

        if should_pause:
            await self._set_state(StreamState.PAUSED)

    async def resume(self) -> None:
        """Resume dispatching of log entries."""
        if self.state == StreamState.PAUSED:
            await self._set_state(StreamState.RUNNING)

    @staticmethod
    async def _invoke_callback(
        callback: Callable[..., Awaitable[None]] | None, *args: object
    ) -> None:
        """Invoke a user lifecycle/error callback without letting it break teardown."""
        if callback is None:
            return
        try:
            await callback(*args)
        except Exception:
            logger.exception("AsyncLogStream lifecycle callback raised")

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
        except TimeoutError as err:
            raise LogStreamTimeoutError("Timeout waiting for connection task") from err

        if self.state == StreamState.KILLED:
            raise LogStreamKilledError("Stream was killed")

        if self._exceptions:
            exc = self._exceptions[0]
            if isinstance(exc, LogStreamTimeoutError) and self._stop_event.is_set():
                return
            if isinstance(exc, LogStreamError):
                raise exc
            raise LogStreamInternalError(
                f"Internal error in stream task: {exc}"
            ) from exc

    async def _read_loop(self, stream: asyncio.StreamReader, source: str) -> None:  # noqa: PLR0912  read/timeout/flush handling kept inline
        """Internal loop to read from a stream."""
        try:  # noqa: PLR1702  nested read/timeout handling kept inline
            while not self._stop_event.is_set():
                try:
                    if self.read_timeout:
                        try:
                            line_bytes = await asyncio.wait_for(
                                stream.readline(), timeout=self.read_timeout
                            )
                        except TimeoutError:
                            # Terminate process to unblock connection manager
                            if self._process:
                                with contextlib.suppress(ProcessLookupError):
                                    self._process.terminate()
                            raise
                    else:
                        line_bytes = await stream.readline()
                except ValueError:
                    # Oversized line (no newline within the read buffer limit): drop
                    # the buffered chunk and keep reading rather than aborting the loop.
                    logger.warning(
                        "Dropping oversized %s data exceeding the read buffer limit",
                        source,
                    )
                    with contextlib.suppress(Exception):
                        await stream.read(_READ_LIMIT)
                    continue

                if not line_bytes:
                    # EOF
                    break

                line = line_bytes.decode("utf-8", errors="replace")

                if self.state == StreamState.PAUSED:
                    continue

                await self._process_line(line, source)
        except TimeoutError as e:
            self._exceptions.append(LogStreamTimeoutError(str(e) or "Read timeout"))
            await self._invoke_callback(self.on_error, e)
        except Exception as e:  # noqa: BLE001  read/parse errors must not crash the loop
            logger.debug("Error in read loop for %s: %s", source, e)
            self._exceptions.append(e)
            await self._invoke_callback(self.on_error, e)
        finally:
            if source == "stdout" and self.group_by:
                # Flush grouper
                flushed = self.group_by.flush()
                if flushed and self.stdout_callback:
                    for item in flushed:
                        try:
                            await self._dispatch_item(
                                item,
                                source="stdout",
                                callback=self.stdout_callback,
                            )
                        except Exception as e:  # noqa: BLE001  callback must not crash flush
                            logger.debug("Error dispatching flushed item: %s", e)
                            await self._invoke_callback(self.on_error, e)

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
        except Exception as e:  # noqa: BLE001  parser errors must not crash the stream
            logger.debug("Error parsing %s line: %s", source, e)
            self._exceptions.append(e)
            await self._invoke_callback(self.on_error, e)
            if self._process:
                with contextlib.suppress(ProcessLookupError):
                    self._process.terminate()
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
        # LogGrouper is synchronous, which is fine as it's CPU bound and fast.
        # Grouping applies to stdout only (the grouper is flushed on stdout EOF);
        # feeding stderr through the shared grouper would flush/misroute stdout groups.
        items_to_emit: list[LogEntry | list[LogEntry]]
        if self.group_by and source == "stdout":
            items_to_emit = self.group_by.process(entry)
        else:
            items_to_emit = [entry]

        # 4. Dispatch
        callback = self.stdout_callback if source == "stdout" else self.stderr_callback
        if callback:
            for item in items_to_emit:
                try:
                    await self._dispatch_item(item, source=source, callback=callback)
                except Exception as e:  # noqa: BLE001  dispatch must not crash the stream
                    logger.debug("Error dispatching %s item: %s", source, e)
                    await self._invoke_callback(self.on_error, e)

    async def __aenter__(self) -> AsyncStreamHandle:
        """Start the stream and return the handle."""
        await self.start()
        return self._handle

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Stop the stream on exit."""
        await self.stop()
        await self.join(timeout=1.0)
