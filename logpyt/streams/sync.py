"""Log stream implementation for capturing and processing ADB logcat output."""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal, TextIO

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


class PidMonitor:
    """Monitors PIDs for specific packages using ADB."""

    def __init__(
        self, adb_path: str, device_id: str | None, packages: list[str]
    ) -> None:
        """Initialize the PID monitor.

        Args:
            adb_path: Path to ADB executable.
            device_id: Target device serial ID.
            packages: List of package names to monitor.
        """
        self.adb_path = adb_path
        self.device_id = device_id
        self.packages = list(set(packages))
        self._pid_map: dict[int, str] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

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
        while not self._stop_event.is_set():
            new_map: dict[int, str] = {}
            for package in self.packages:
                pids = self._resolve_pids(package)
                for pid in pids:
                    new_map[pid] = package

            with self._lock:
                self._pid_map = new_map

            self._stop_event.wait(2.0)

    def _resolve_pids(self, package: str) -> list[int]:
        """Resolve PIDs for a package using ADB."""
        cmd = build_pidof_command(self.adb_path, self.device_id, package)

        try:
            # Use a timeout to prevent hanging
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
            if result.returncode == 0 and result.stdout.strip():
                return [int(p) for p in result.stdout.split()]
        except Exception:
            # Ignore errors (e.g. device disconnected, command failed)
            pass
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
        auto_reconnect: bool = False,
        reconnect_delay: float = 1.0,
        max_queue_size: int = 10000,
        read_timeout: float | None = None,
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
        """
        self.adb_path = adb_path or resolve_adb()
        self.device_id = device_id
        self.parser = parser or LogParser()
        self.filter_by = filter_by
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay
        self.max_queue_size = max_queue_size
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

        self._exceptions: queue.Queue[Exception] = queue.Queue()
        self._callback_queue: queue.Queue[
            tuple[list[LogEntry | list[LogEntry]], str] | None
        ] = queue.Queue(maxsize=self.max_queue_size)
        self._pid_monitor: PidMonitor | None = None
        if isinstance(self.filter_by, Filter) and self.filter_by.packages:
            self._pid_monitor = PidMonitor(
                self.adb_path, self.device_id, list(self.filter_by.packages)
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
                # Don't let callback errors crash the stream, but maybe log it?
                # For now, just ignore or print to stderr if debug
                pass

    def start(self) -> None:
        """Start the log stream."""
        with self._state_lock:
            if self._state != StreamState.IDLE and self._state != StreamState.STOPPED:
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

    def _connection_manager(self) -> None:
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
            except Exception as e:
                self._set_state(StreamState.STOPPED)
                self._exceptions.put(e)
                if self.on_error:
                    self.on_error(e)
                # If we can't even start the process, we probably shouldn't loop infinitely fast
                # unless it's a transient error.
                # For now, let's treat start failure as fatal or subject to reconnect delay?
                # If auto_reconnect is True, we should probably wait and retry.
                if self.auto_reconnect and not self._stop_event.is_set():
                    self._set_state(StreamState.RECONNECTING)
                    if self._stop_event.wait(self.reconnect_delay):
                        break
                    continue
                else:
                    break

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
            if self.on_start:
                self.on_start()

            # Wait for process to exit
            self._process.wait()

            # Wait for threads to finish reading
            if self._stdout_thread:
                self._stdout_thread.join()
            if self._stderr_thread:
                self._stderr_thread.join()

            # Watchdog thread will exit when process exits or stop event is set
            if self._watchdog_thread:
                self._watchdog_thread.join(timeout=1.0)

            # Signal callback thread to stop
            self._callback_queue.put(None)
            if self._callback_thread:
                self._callback_thread.join()

            if self._pid_monitor:
                self._pid_monitor.stop()

            if self._stop_event.is_set():
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
                if self.on_stop:
                    self.on_stop()

    def stop(self) -> None:
        """Stop the log stream gracefully."""
        with self._state_lock:
            if self._state in (
                StreamState.STOPPED,
                StreamState.KILLED,
                StreamState.IDLE,
            ):
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
        """Pause dispatching of log entries."""
        with self._state_lock:
            if self._state == StreamState.RUNNING:
                self._set_state(StreamState.PAUSED)

    def resume(self) -> None:
        """Resume dispatching of log entries."""
        with self._state_lock:
            if self._state == StreamState.PAUSED:
                self._set_state(StreamState.RUNNING)

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

        self._connection_thread.join(timeout=timeout)
        if self._connection_thread.is_alive():
            raise LogStreamTimeoutError("Timeout waiting for connection thread")

        if self.state == StreamState.KILLED:
            raise LogStreamKilledError("Stream was killed")

        # Check for exceptions
        if not self._exceptions.empty():
            # Raise the first exception found
            # We wrap it in LogStreamInternalError if it's not already a LogStreamError
            exc = self._exceptions.get()
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
                    f"LogStream read timeout ({self.read_timeout}s). Killing ADB process."
                )
                if self._process:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                break

            time.sleep(1.0)

    def _read_loop(self, pipe: TextIO | None, source: str) -> None:
        """Internal loop to read from a pipe."""
        if pipe is None:
            return

        try:
            while not self._stop_event.is_set():
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
        except Exception as e:
            self._exceptions.put(e)
            if self.on_error:
                self.on_error(e)
            # Ensure process is terminated so connection manager doesn't hang
            if self._process:
                self._process.terminate()
        finally:
            # If this is the main thread (stdout) and we exit, we should probably ensure
            # the state transitions to STOPPED if it wasn't KILLED.
            # However, both threads run independently.
            # We'll let the process exit determine the final state usually,
            # but here we just finish the thread.
            pass

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
                        except Exception as e:
                            if self.on_error:
                                self.on_error(e)
            except Exception as e:
                # Should not happen, but if it does, log it
                if self.on_error:
                    self.on_error(e)

    def _process_line(self, line: str, source: str) -> None:
        """Process a single raw line."""
        # 1. Parse
        try:
            if source == "stdout":
                entry = self.parser.parse_stdout(line)
            else:
                entry = self.parser.parse_stderr(line)
        except Exception:
            # If parsing fails, maybe fallback or ignore?
            # For now, let's assume parser handles it or returns a basic entry
            # If parser raises, we catch it here to not kill the thread
            return

        if self._pid_monitor:
            pkg = self._pid_monitor.get_package(entry.pid)
            if pkg:
                entry.meta["package"] = pkg

        # 2. Filter
        if self.filter_by and not self.filter_by(entry):
            return

        # 3. Group
        items_to_emit: list[LogEntry | list[LogEntry]] = []
        if self.group_by:
            items_to_emit = self.group_by.process(entry)
        else:
            items_to_emit = [entry]

        # 4. Dispatch
        if items_to_emit:
            try:
                self._callback_queue.put_nowait((items_to_emit, source))
            except queue.Full:
                logging.getLogger("logpyt").warning(
                    "LogStream callback queue is full. Dropping log entry."
                )

    def __enter__(self) -> StreamHandle:
        """Start the stream and return the handle."""
        self.start()
        return self._handle

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Stop the stream on exit."""
        self.stop()
        # We don't necessarily join here, as stop() is async, but usually context managers
        # clean up resources. stop() terminates the process.
        # We might want to wait for it to actually close?
        # Let's do a quick join with timeout to ensure cleanup
        try:
            self.join(timeout=1.0)
        except (LogStreamTimeoutError, LogStreamKilledError):
            pass
