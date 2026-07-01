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

__version__ = "1.0.0"

from .exceptions import (
    LogStreamError,
    LogStreamInternalError,
    LogStreamKilledError,
    LogStreamPidResolveError,
    LogStreamStderrError,
    LogStreamTimeoutError,
)
from .exporters import export_logs
from .filters import AdvancedFilter, Filter
from .groupers import LogGrouper
from .models import LogEntry
from .parsers import LogParser, ThreadTimeLogParser
from .readers import LogFileReader, read_file
from .streams import AsyncLogStream, AsyncStreamHandle, LogStream, StreamHandle
from .utils import enable_debug, list_devices, resolve_adb

__all__ = [
    "AdvancedFilter",
    "AsyncLogStream",
    "AsyncStreamHandle",
    "Filter",
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
    "StreamHandle",
    "ThreadTimeLogParser",
    "enable_debug",
    "export_logs",
    "list_devices",
    "read_file",
    "resolve_adb",
]
