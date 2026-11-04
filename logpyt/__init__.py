"""logpyt package.

This package provides tools for parsing, filtering, grouping, and handling
structured log entries from ADB logcat output. It allows you to treat log streams
as observable events, making it easier to build tools that react to specific log
patterns.

Quick Start:
    ```python
    import logging
    from logpyt import LogStream, LogEntry, StreamHandle

    # Configure logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    def on_log(entry: LogEntry, handle: StreamHandle) -> None:
        logger.info("[%s] %s: %s", entry.level, entry.tag, entry.message)
        if "FATAL ERROR" in entry.message:
            logger.warning("Stopping stream...")
            handle.stop()

    # Start capturing logs
    with LogStream(stdout_callback=on_log) as stream:
        stream.join()
    ```
"""

__version__ = "2.0.0"

from .config import Reconnect, StreamConfig
from .exceptions import (
    AdbError,
    AdbExitError,
    CallbackError,
    IncompleteCaptureError,
    LogpytError,
    LogStreamError,
    LogStreamInternalError,
    LogStreamKilledError,
    LogStreamPidResolveError,
    LogStreamStderrError,
    LogStreamTimeoutError,
    ReadTimeoutError,
    StreamError,
    UsageError,
)
from .exporters import export_logs
from .filters import AdvancedFilter, Filter
from .groupers import LogGrouper, WindowedLogGrouper
from .models import LogEntry
from .parsers import LogParser, ThreadTimeLogParser
from .readers import LogFileReader, read_file
from .streams import AsyncLogStream, AsyncStreamHandle, LogStream, StreamHandle

# 3.0 stream events. StreamState stays in logpyt.streams._events until the driver
# rewrites switch over: logpyt.streams.StreamState is still the 2.x enum that the
# current streams report (design section 8.1).
from .streams._events import Gap, StateChange, StreamEvent, StreamStats
from .utils import enable_debug, list_devices, resolve_adb

__all__ = [
    "AdbError",
    "AdbExitError",
    "AdvancedFilter",
    "AsyncLogStream",
    "AsyncStreamHandle",
    "CallbackError",
    "Filter",
    "Gap",
    "IncompleteCaptureError",
    "LogEntry",
    "LogFileReader",
    "LogGrouper",
    "LogParser",
    "LogStream",
    "LogStreamError",
    "LogStreamInternalError",
    "LogStreamKilledError",
    "LogStreamPidResolveError",
    "LogStreamStderrError",
    "LogStreamTimeoutError",
    "LogpytError",
    "ReadTimeoutError",
    "Reconnect",
    "StateChange",
    "StreamConfig",
    "StreamError",
    "StreamEvent",
    "StreamHandle",
    "StreamStats",
    "ThreadTimeLogParser",
    "UsageError",
    "WindowedLogGrouper",
    "enable_debug",
    "export_logs",
    "list_devices",
    "read_file",
    "resolve_adb",
]
