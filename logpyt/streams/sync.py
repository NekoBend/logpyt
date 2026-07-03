"""Log stream implementation for capturing and processing ADB logcat output."""

from __future__ import annotations

import contextlib
import logging
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal, TextIO

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


class PidMonitor:
    """Monitors PIDs for specific packages using ADB."""

    def __init__(
        self,
        adb_path: str,
        device_id: str | None,
        packages: list[str],
        poll_interval: float = 2.0,
        max_poll_interval: float = 30.0,
        poll_backoff_factor: float = 2.0,
        max_resolves_per_cycle: int = 32,
    ) -> None:
        """Initialize the PID monitor.

        Args:
            adb_path: Path to ADB executable.
            device_id: Target device serial ID.
            packages: List of package names to monitor.
            poll_interval: Base interval in seconds between PID polls.
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
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._resolve_cursor = 0

    def start(self) -> None:
        """Start the monitoring thread."""
        if not self.packages:
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="PidMonitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the monitoring thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def get_package(self, pid: int) -> str | None:
        """Get the package name for a given PID.

        Args:
            pid: The process ID.

        Returns:
            The package name if found, None otherwise.
        """
        with self._lock:
            return self._pid_map.get(pid)

    def _run(self) -> None:
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
                pids = self._resolve_pids(package)
                for pid in pids:
                    resolved_map[pid] = package

            if cycle_limit == total:
                new_map = resolved_map
            else:
                polled_package_set = set(polled_packages)
                with self._lock:
                    new_map = {
                        pid: package
                        for pid, package in self._pid_map.items()
                        if package not in polled_package_set
                    }
                new_map.update(resolved_map)

            if total:
                self._resolve_cursor = (self._resolve_cursor + cycle_limit) % total

            changed = self._update_pid_map(new_map)

            if changed:
                current_interval = self.poll_interval
            else:
                current_interval = min(
                    self.max_poll_interval,
                    current_interval * self.poll_backoff_factor,
                )

            self._stop_event.wait(current_interval)

    def _update_pid_map(self, new_map: dict[int, str]) -> bool:
        """Apply PID map changes in place.

        This avoids replacing the full mapping object when there are no changes,
        while still preserving exact add/update/remove semantics.

        Args:
            new_map: Latest PID to package mapping snapshot.

        Returns:
            True if at least one mapping changed, False otherwise.
        """
        with self._lock:
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

    def _resolve_pids(self, package: str) -> list[int]:
        """Resolve PIDs for a package using ADB."""
        cmd = build_pidof_command(self.adb_path, self.device_id, package)

        try:
            # Use a timeout to prevent hanging
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
            if result.returncode == 0 and result.stdout.strip():
                return [
                    int(p) for p in result.stdout.split() if p.isdigit() and len(p) <= 7
                ]
        except Exception as exc:  # noqa: BLE001
            # A polling monitor must never crash: any failure (device disconnected,
            # command failed, malformed output) degrades to returning no PIDs.
            logging.getLogger("logpyt").debug(
                "pidof resolution failed for %s: %s", package, exc
            )
        return []


class StreamHandle:
    """Handle to control a LogStream instance."""

    def __init__(self, stream: LogStream) -> None:
        """Initialize the handle.

        Args:
            stream: The LogStream instance to control.
        """
        self._stream = stream

    def stop(self) -> None:
        """Stop the log stream gracefully."""
        self._stream.stop()

    def kill(self) -> None:
        """Kill the log stream immediately."""
        self._stream.kill()

    def pause(self) -> None:
        """Pause dispatching of log entries."""
        self._stream.pause()

    def resume(self) -> None:
        """Resume dispatching of log entries."""
        self._stream.resume()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the stream to finish."""
        self._stream.join(timeout)

    @property
    def state(self) -> StreamState:
        """Get the current state of the stream."""
        return self._stream.state


# Type aliases for callbacks
LogCallback = Callable[[Any, StreamHandle], None]
StateCallback = Callable[[StreamState], None]
ErrorCallback = Callable[[Exception], None]
LifecycleCallback = Callable[[], None]


class LogStream:
    """Manages an ADB logcat subprocess and processes its output.

    This class handles the lifecycle of the `adb logcat` process, parses its output
    into `LogEntry` objects, applies filters and groupers, and dispatches the results
    to user-provided callbacks.

    Usage:
        ```python
        from logpyt import LogStream, LogEntry, StreamHandle

        def my_callback(entry: LogEntry, handle: StreamHandle) -> None:
            if entry.tag == "MyApp":
                print(f"MyApp Log: {entry.message}")

        stream = LogStream(stdout_callback=my_callback)
        stream.start()

        # Do other work...

        stream.stop()
        stream.join()
        ```

        Concise grouping usage:
        ```python
        # Group logs by PID and TID if within 50ms, handling interleaved logs
        stream = LogStream(
            stdout_callback=my_callback,
            group_keys=["pid", "tid"],
            group_interval=50.0,
            group_mode="windowed"
        )
        ```
    """

    def __init__(
        self,
        adb_path: str | None = None,
        device_id: str | None = None,
        parser: LogParser | None = None,
        filter_by: Filter | None = None,
        group_by: LogGrouper | None = None,
        stdout_callback: LogCallback | None = None,
        stderr_callback: LogCallback | None = None,
        on_start: LifecycleCallback | None = None,
        on_stop: LifecycleCallback | None = None,
        on_state: StateCallback | None = None,
        on_error: ErrorCallback | None = None,
        logcat_args: Sequence[str] | None = None,
        group_keys: Sequence[str] | None = None,
        group_interval: float | None = None,
        group_mode: Literal["consecutive", "windowed"] = "consecutive",
        auto_reconnect: bool = False,  # noqa: FBT001, FBT002 (existing public signature)
        reconnect_delay: float = 1.0,
        max_queue_size: int = 10000,
        read_timeout: float | None = None,
        queue_full_warning_interval: float = 5.0,
        queue_overflow_policy: Literal["drop_newest", "drop_oldest"] = "drop_newest",
        pid_poll_interval: float = 2.0,
        pid_max_poll_interval: float = 30.0,
        pid_poll_backoff_factor: float = 2.0,
        pid_max_resolves_per_cycle: int = 32,
    ) -> None:
        """Initialize the LogStream.

        Args:
            adb_path: Path to ADB executable. If None, resolved automatically.
            device_id: Target device serial ID.
            parser: Parser to convert lines to LogEntry objects.
            filter_by: Filter to apply to log entries.
            group_by: Grouper to group log entries.
            stdout_callback: Callback for processed stdout entries.
                Signature: `Callable[[LogEntry, StreamHandle], None]`.
                The second argument is the `StreamHandle`, which allows controlling
                the stream (e.g., `handle.stop()`) from within the callback.
            stderr_callback: Callback for processed stderr entries.
                Signature: `Callable[[LogEntry, StreamHandle], None]`.
                The second argument is the `StreamHandle`, which allows controlling
                the stream (e.g., `handle.stop()`) from within the callback.
            on_start: Hook called when stream starts.
            on_stop: Hook called when stream stops.
            on_state: Hook called when state changes.
            on_error: Hook called when an error occurs.
            logcat_args: Additional arguments for `adb logcat`.
            group_keys: Shortcut to create a grouper. Fields to group by.
            group_interval: Shortcut to create a grouper. Time threshold in ms.
            group_mode: Shortcut to create a grouper. "consecutive" or "windowed".
            auto_reconnect: Whether to automatically reconnect when the process exits.
            reconnect_delay: Delay in seconds before reconnecting.
            max_queue_size: Maximum size of the callback queue. Defaults to 10000.
            read_timeout: Timeout in seconds for reading from the stream.
            queue_full_warning_interval: Minimum interval in seconds between
                repeated queue-full warnings. Defaults to 5.0.
            queue_overflow_policy: Policy for callback queue overflow.
                "drop_newest" drops incoming data, "drop_oldest" evicts the
                oldest queued item.
            pid_poll_interval: Base interval in seconds for PID monitoring.
            pid_max_poll_interval: Maximum PID poll interval when idle.
            pid_poll_backoff_factor: Backoff factor for unchanged PID snapshots.
            pid_max_resolves_per_cycle: Maximum packages to resolve per PID poll cycle.
        """
        self.adb_path = adb_path or resolve_adb()
        self.device_id = device_id
        self.parser = parser or ThreadTimeLogParser()
        self.filter_by = filter_by
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay
        self.max_queue_size = max_queue_size
        self.read_timeout = read_timeout
        self._queue_full_warning_interval = max(0.0, queue_full_warning_interval)
        self.queue_overflow_policy = queue_overflow_policy
        self.pid_poll_interval = pid_poll_interval
        self.pid_max_poll_interval = pid_max_poll_interval
        self.pid_poll_backoff_factor = pid_poll_backoff_factor
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

        self._exceptions: queue.Queue[Exception] = queue.Queue()
        # Serializes grouper access: stdout and stderr reader threads share one grouper.
        self._grouper_lock = threading.Lock()
        self._callback_queue: queue.Queue[
            tuple[list[LogEntry | list[LogEntry]], str] | None
        ] = queue.Queue(maxsize=self.max_queue_size)
        self._pid_monitor: PidMonitor | None = None
        if isinstance(self.filter_by, Filter) and self.filter_by.packages:
            self._pid_monitor = PidMonitor(
                self.adb_path,
                self.device_id,
                list(self.filter_by.packages),
                poll_interval=self.pid_poll_interval,
                max_poll_interval=self.pid_max_poll_interval,
                poll_backoff_factor=self.pid_poll_backoff_factor,
                max_resolves_per_cycle=self.pid_max_resolves_per_cycle,
            )

        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._callback_thread: threading.Thread | None = None
        self._connection_thread: threading.Thread | None = None
        self._state = StreamState.IDLE
        self._state_lock = threading.RLock()
        self._handle = StreamHandle(self)
        self._stop_event = threading.Event()

        self._last_activity: float = 0.0
        self._activity_lock = threading.Lock()
        self._watchdog_thread: threading.Thread | None = None
        self._last_queue_full_warning: dict[str, float] = {}
        self._queue_warning_lock = threading.Lock()

    @property
    def state(self) -> StreamState:
        """Current state of the stream."""
        with self._state_lock:
            return self._state

    def _set_state(self, new_state: StreamState) -> None:
        """Update state and trigger callback."""
        with self._state_lock:
            if self._state == new_state:
                return
            self._state = new_state

        if self.on_state:
            try:
                self.on_state(new_state)
            except Exception:
                # Don't let a user on_state callback error crash the stream.
                logging.getLogger("logpyt").exception("on_state callback failed")

    def start(self) -> None:
        """Start the log stream."""
        with self._state_lock:
            if self._state not in {StreamState.IDLE, StreamState.STOPPED}:
                raise LogStreamInternalError(
                    f"Cannot start stream from state {self._state}"
                )
            self._set_state(StreamState.STARTING)

        self._stop_event.clear()
        self._connection_thread = threading.Thread(
            target=self._connection_manager,
            name="LogStream-ConnectionManager",
            daemon=True,
        )
        self._connection_thread.start()

    def _connection_manager(self) -> None:  # noqa: PLR0912, PLR0915 (lifecycle state machine; refactor deferred)
        """Manage the ADB process lifecycle and reconnection."""
        while not self._stop_event.is_set():
            cmd = [self.adb_path]
            if self.device_id:
                cmd.extend(["-s", self.device_id])
            cmd.append("logcat")
            cmd.extend(self.logcat_args)

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,  # Line buffered
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception as e:  # noqa: BLE001 (process spawn may fail arbitrarily; must not crash manager)
                logging.getLogger("logpyt").debug("Failed to start adb process: %s", e)
                self._set_state(StreamState.STOPPED)
                self._exceptions.put(e)
                self._invoke_callback(self.on_error, e)
                # If we can't even start the process, we probably shouldn't loop
                # infinitely fast unless it's a transient error.
                # For now, let's treat start failure as fatal or subject to the
                # reconnect delay when auto_reconnect is True.
                if self.auto_reconnect and not self._stop_event.is_set():
                    self._set_state(StreamState.RECONNECTING)
                    if self._stop_event.wait(self.reconnect_delay):
                        break
                    continue
                else:
                    break

            # stop() may have set the event during Popen() above; it terminated
            # the previous handle, not this fresh one, so terminate now to avoid
            # orphaning it on the wait() below.
            if self._stop_event.is_set():
                with contextlib.suppress(ProcessLookupError):
                    self._process.terminate()

            self._stdout_thread = threading.Thread(
                target=self._read_loop,
                args=(self._process.stdout, "stdout"),
                name="LogStream-Stdout",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_loop,
                args=(self._process.stderr, "stderr"),
                name="LogStream-Stderr",
                daemon=True,
            )

            self._stdout_thread.start()
            self._stderr_thread.start()

            self._callback_thread = threading.Thread(
                target=self._callback_loop,
                name="LogStream-Callback",
                daemon=True,
            )
            self._callback_thread.start()

            if self._pid_monitor:
                self._pid_monitor.start()

            if self.read_timeout:
                with self._activity_lock:
                    self._last_activity = time.time()
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog_loop,
                    name="LogStream-Watchdog",
                    daemon=True,
                )
                self._watchdog_thread.start()

            self._set_state(StreamState.RUNNING)
            self._invoke_callback(self.on_start)

            # Wait for process to exit
            return_code = self._process.wait()

            # Wait for threads to finish reading
            if self._stdout_thread:
                self._stdout_thread.join()
            if self._stderr_thread:
                self._stderr_thread.join()

            # Watchdog thread will exit when process exits or stop event is set
            if self._watchdog_thread:
                self._watchdog_thread.join(timeout=1.0)

            # Signal callback thread to stop
            self._enqueue_callback_item(None, source=None, force=True)
            if self._callback_thread:
                self._callback_thread.join(timeout=0.5)

            if self._pid_monitor:
                self._pid_monitor.stop()

            if self._stop_event.is_set():
                break

            if isinstance(return_code, int) and return_code != 0:
                err = LogStreamInternalError(
                    f"adb logcat exited with code {return_code}"
                )
                self._exceptions.put(err)
                self._invoke_callback(self.on_error, err)
                break

            if not self.auto_reconnect:
                break

            self._set_state(StreamState.RECONNECTING)
            if self._stop_event.wait(self.reconnect_delay):
                break

        # Final cleanup
        with self._state_lock:
            if self._state != StreamState.KILLED:
                self._set_state(StreamState.STOPPED)
                self._invoke_callback(self.on_stop)

    def stop(self) -> None:
        """Stop the log stream gracefully."""
        with self._state_lock:
            if self._state in {
                StreamState.STOPPED,
                StreamState.KILLED,
                StreamState.IDLE,
            }:
                return
            self._set_state(StreamState.STOPPING)

        self._stop_event.set()
        if self._pid_monitor:
            self._pid_monitor.stop()
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()

    def kill(self) -> None:
        """Kill the log stream immediately."""
        with self._state_lock:
            self._set_state(StreamState.KILLED)

        self._stop_event.set()
        if self._pid_monitor:
            self._pid_monitor.stop()
        if self._process:
            self._process.kill()

    def pause(self) -> None:
        """Pause dispatching of log entries.

        Entries read from the device while paused are dropped, not buffered: a
        live stream cannot buffer unboundedly. Call resume() to continue.
        """
        with self._state_lock:
            if self._state == StreamState.RUNNING:
                self._set_state(StreamState.PAUSED)

    def resume(self) -> None:
        """Resume dispatching of log entries."""
        with self._state_lock:
            if self._state == StreamState.PAUSED:
                self._set_state(StreamState.RUNNING)

    @staticmethod
    def _invoke_callback(callback: Callable[..., None] | None, *args: object) -> None:
        """Invoke a user lifecycle/error callback without letting it break teardown."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logging.getLogger("logpyt").exception("LogStream lifecycle callback raised")

    def join(self, timeout: float | None = None) -> None:
        """Wait for the stream to finish.

        Args:
            timeout: Maximum time to wait in seconds.

        Raises:
            LogStreamTimeoutError: If timeout expires.
            LogStreamKilledError: If stream was killed.
            LogStreamInternalError: If an internal error occurred.
        """
        if not self._connection_thread:
            return

        deadline = None if timeout is None else time.monotonic() + timeout

        self._connection_thread.join(timeout=timeout)
        if self._connection_thread.is_alive():
            raise LogStreamTimeoutError("Timeout waiting for connection thread")

        if self._callback_thread and self._callback_thread.is_alive():
            if deadline is None:
                self._callback_thread.join()
            else:
                # Honor the remaining timeout budget for the callback phase too, so
                # a blocked user callback cannot make join(timeout=...) hang.
                remaining = max(deadline - time.monotonic(), 0.0)
                self._callback_thread.join(timeout=remaining)
                if self._callback_thread.is_alive():
                    raise LogStreamTimeoutError("Timeout waiting for callback thread")

        if self.state == StreamState.KILLED:
            raise LogStreamKilledError("Stream was killed")

        # Check for exceptions
        if not self._exceptions.empty():
            # Raise the first exception found
            # We wrap it in LogStreamInternalError if it's not already a LogStreamError
            # Peek (non-destructive) so re-invoking join() surfaces the same terminal
            # error, matching AsyncLogStream.join().
            exc = self._exceptions.queue[0]
            if isinstance(exc, LogStreamError):
                raise exc
            raise LogStreamInternalError(
                f"Internal error in stream thread: {exc}"
            ) from exc

    def _watchdog_loop(self) -> None:
        """Monitor stream activity and kill process if hung."""
        if not self.read_timeout:
            return

        while not self._stop_event.is_set():
            # Check if process is still running
            if self._process is None or self._process.poll() is not None:
                break

            with self._activity_lock:
                last = self._last_activity

            if time.time() - last > self.read_timeout:
                logging.getLogger("logpyt").warning(
                    "LogStream read timeout (%ss). Killing ADB process.",
                    self.read_timeout,
                )
                if self._process:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        self._process.kill()
                break

            time.sleep(1.0)

    def _warn_queue_full_once(self, message: str, key: str | None = None) -> None:
        """Emit queue-full warning with optional rate limiting.

        Rate limiting is keyed on ``key`` (falling back to ``message``) so a
        message carrying a variable count still de-duplicates on a stable key.
        """
        interval = self._queue_full_warning_interval
        if interval <= 0.0:
            logging.getLogger("logpyt").warning(message)
            return

        dedup_key = key if key is not None else message
        now = time.monotonic()
        should_log = False
        with self._queue_warning_lock:
            last = self._last_queue_full_warning.get(dedup_key)
            if last is None or (now - last) >= interval:
                self._last_queue_full_warning[dedup_key] = now
                should_log = True

        if should_log:
            logging.getLogger("logpyt").warning(message)

    def _read_loop(self, pipe: TextIO | None, source: str) -> None:
        """Internal loop to read from a pipe."""
        if pipe is None:
            return

        try:
            while not self._stop_event.is_set():
                if self._process and self._process.poll() is not None:
                    break

                line = pipe.readline()
                if not line:
                    # EOF
                    break

                if self.read_timeout:
                    with self._activity_lock:
                        self._last_activity = time.time()

                if self.state == StreamState.PAUSED:
                    continue

                self._process_line(line, source)
        except Exception as e:  # noqa: BLE001 (read/parse errors must not crash the reader thread)
            logging.getLogger("logpyt").debug("Read loop error on %s: %s", source, e)
            self._exceptions.put(e)
            self._invoke_callback(self.on_error, e)
            # Ensure process is terminated so connection manager doesn't hang
            if self._process:
                self._process.terminate()
        finally:
            if source == "stdout" and self.group_by:
                with self._grouper_lock:
                    flushed = self.group_by.flush()
                if flushed:
                    self._enqueue_callback_item((flushed, source), source=source)

    def _callback_loop(self) -> None:
        """Internal loop to process callbacks."""
        while True:
            try:
                item = self._callback_queue.get()
                if item is None:
                    break

                items, source = item
                callback = (
                    self.stdout_callback if source == "stdout" else self.stderr_callback
                )
                if callback:
                    for entry in items:
                        try:
                            callback(entry, self._handle)
                        except Exception as e:  # noqa: BLE001 (user callback must not crash the dispatch loop)
                            logging.getLogger("logpyt").debug(
                                "User callback raised: %s", e
                            )
                            self._invoke_callback(self.on_error, e)
            except Exception as e:  # noqa: BLE001 (dispatch loop must not crash on unexpected errors)
                # Should not happen, but if it does, log it
                logging.getLogger("logpyt").debug("Callback loop error: %s", e)
                self._invoke_callback(self.on_error, e)

    def _enqueue_callback_item(
        self,
        item: tuple[list[LogEntry | list[LogEntry]], str] | None,
        source: str | None,
        force: bool = False,  # noqa: FBT001, FBT002 (existing public signature)
    ) -> None:
        """Enqueue callback work honoring overflow policy."""
        try:
            self._callback_queue.put_nowait(item)
            return
        except queue.Full:
            pass

        if force:
            try:
                self._callback_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._callback_queue.put_nowait(item)
            except queue.Full:
                return
            return

        if self.queue_overflow_policy == "drop_oldest":
            try:
                self._callback_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._callback_queue.put_nowait(item)
                return
            except queue.Full:
                pass

        if source == "stdout" and self.group_by:
            dropped = (
                sum(len(x) if isinstance(x, list) else 1 for x in item[0])
                if item is not None
                else 0
            )
            self._warn_queue_full_once(
                f"LogStream callback queue full; dropping {dropped} grouped entries.",
                key="queue-full-grouped",
            )
        else:
            self._warn_queue_full_once(
                "LogStream callback queue is full. Dropping log entry."
            )

    def _process_line(self, line: str, source: str) -> None:
        """Process a single raw line."""
        # 1. Parse
        try:
            if source == "stdout":
                entry = self.parser.parse_stdout(line)
            else:
                entry = self.parser.parse_stderr(line)
        except Exception as e:  # noqa: BLE001 (parser errors must not crash the reader thread)
            logging.getLogger("logpyt").debug("Parser error on %s line: %s", source, e)
            self._exceptions.put(e)
            self._invoke_callback(self.on_error, e)
            if self._process:
                self._process.terminate()
            return

        if self._pid_monitor:
            pkg = self._pid_monitor.get_package(entry.pid)
            if pkg:
                entry.meta["package"] = pkg

        # 2. Filter
        if self.filter_by and not self.filter_by(entry):
            return

        # 3. Group
        # Grouping applies to stdout only (the shared grouper is flushed on stdout
        # EOF); feeding stderr through it would flush/misroute stdout groups.
        items_to_emit: list[LogEntry | list[LogEntry]]
        if self.group_by and source == "stdout":
            with self._grouper_lock:
                items_to_emit = self.group_by.process(entry)
        else:
            items_to_emit = [entry]

        # 4. Dispatch
        if items_to_emit:
            self._enqueue_callback_item((items_to_emit, source), source=source)

    def __enter__(self) -> StreamHandle:
        """Start the stream and return the handle."""
        self.start()
        return self._handle

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Stop the stream on exit."""
        self.stop()
        # We don't necessarily join here, as stop() is async, but usually
        # context managers clean up resources. stop() terminates the process.
        # We might want to wait for it to actually close?
        # Let's do a quick join with timeout to ensure cleanup.
        with contextlib.suppress(LogStreamTimeoutError, LogStreamKilledError):
            self.join(timeout=1.0)
