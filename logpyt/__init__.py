"""logpyt package.

This package provides tools for parsing, filtering, grouping, and handling
structured log entries from ADB logcat output. It allows you to treat log streams
as observable events, making it easier to build tools that react to specific log
patterns.

Quick Start:
    ```python
    from logpyt import LogStream, MinLevel, Package

    # Print the error lines of one app until Ctrl+C.
    with LogStream(where=Package("com.example.app") & MinLevel("E")) as stream:
        for entry in stream:
            print(entry.timestamp, entry.level, entry.tag, entry.message)
    ```

    A callback can take the entries instead (``on_item=entries.append``);
    ``stream.stop()`` and ``stream.join()`` end the run.
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
from .filters import (
    And,
    AnrCondition,
    Condition,
    CrashCondition,
    Filter,
    Level,
    MessageContains,
    MinLevel,
    Not,
    Or,
    Package,
    Tag,
    Where,
)
from .groupers import GroupBy
from .models import LogEntry, LogGroup
from .parsers import LogParser, ThreadTimeLogParser
from .readers import read_file
from .streams import AsyncLogStream, AsyncStreamHandle, LogStream

# 3.0 stream events. StreamState stays in logpyt.streams._events until the async
# driver switches over too (PR-13, design A.3): logpyt.streams.StreamState is still
# the 2.x enum that AsyncLogStream reports (design section 8.1).
from .streams._events import Gap, StateChange, StreamEvent, StreamStats
from .utils import enable_debug

__all__ = [
    "AdbError",
    "AdbExitError",
    "And",
    "AnrCondition",
    "AsyncLogStream",
    "AsyncStreamHandle",
    "CallbackError",
    "Condition",
    "CrashCondition",
    "Filter",
    "Gap",
    "GroupBy",
    "IncompleteCaptureError",
    "Level",
    "LogEntry",
    "LogGroup",
    "LogParser",
    "LogStream",
    "LogStreamError",
    "LogStreamInternalError",
    "LogStreamKilledError",
    "LogStreamPidResolveError",
    "LogStreamStderrError",
    "LogStreamTimeoutError",
    "LogpytError",
    "MessageContains",
    "MinLevel",
    "Not",
    "Or",
    "Package",
    "ReadTimeoutError",
    "Reconnect",
    "StateChange",
    "StreamConfig",
    "StreamError",
    "StreamEvent",
    "StreamStats",
    "Tag",
    "ThreadTimeLogParser",
    "UsageError",
    "Where",
    "enable_debug",
    "export_logs",
    "read_file",
]
