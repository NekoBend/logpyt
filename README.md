# logpyt

Structured logging toolkit around Android logcat streams.

## Installation

```bash
uv pip install git+https://github.com/NekoBend/logpyt.git
```

## Usage Examples

### Basic Usage

Capture logcat output using `LogStream`.

```python
from logpyt import LogStream, LogEntry, StreamHandle
from logpyt.utils import resolve_adb

def stdout_callback(entry: LogEntry, handle: StreamHandle):
    print(f"[{entry.timestamp}] {entry.level}/{entry.tag}: {entry.message}")

def stderr_callback(entry: LogEntry, handle: StreamHandle):
    print(f"ERROR: {entry.raw}")

with LogStream(
    adb_path=resolve_adb(),
    stdout_callback=stdout_callback,
    stderr_callback=stderr_callback,
) as stream:
    print("Capturing logs... (Press Ctrl+C to stop)")
    try:
        stream.join()
    except KeyboardInterrupt:
        stream.stop()
```

### Parsers

`logpyt` supports multiple log formats. The default is `ThreadTimeLogParser`.

```python
from logpyt import LogStream
from logpyt.parsers import (
    ThreadTimeLogParser,  # Default: date time pid tid level tag: message
    BriefLogParser,       # priority/tag(pid): message
    ProcessLogParser,     # priority(pid) message
    TagLogParser,         # priority/tag: message
    RawLogParser          # message only
)

# Use a specific parser
with LogStream(
    parser=BriefLogParser(),
    # ...
) as stream:
    stream.join()
```

### Filtering

Use `Filter` for simple AND conditions, or `AdvancedFilter` for complex logic.

```python
from logpyt import LogStream
from logpyt.filters import (
    Filter, AdvancedFilter, Package, Tag, MessageContains,
    CrashCondition, AnrCondition
)

# Simple Filter (AND logic)
simple_filter = Filter(
    package="com.example.app",
    level=["E", "W"],
    tag="MyApp"
)

# Advanced Filter (DSL with &, |, ~)
advanced_filter = AdvancedFilter(
    (Package("com.example.app") & Tag("MyApp"))
    | MessageContains("CRITICAL")
)

# Detect Crashes or ANRs
crash_filter = AdvancedFilter(CrashCondition() | AnrCondition())

with LogStream(
    filter_by=advanced_filter,
    # ... callbacks ...
) as stream:
    stream.join()
```

### Grouping

Group logs by fields (e.g., PID, TID) within a time threshold.

```python
from logpyt import LogStream, LogEntry, StreamHandle
from logpyt.groupers import LogGrouper

# Group logs by PID and TID if they occur within 50ms of each other
grouper = LogGrouper(
    by=("pid", "tid"),
    threshold_ms=50.0,
    emit_mode="group"  # Emits list[LogEntry] instead of single LogEntry
)

def group_callback(entries: list[LogEntry], handle: StreamHandle):
    print(f"Received group of {len(entries)} logs")
    for entry in entries:
        print(f"  - {entry.message}")

with LogStream(
    group_by=grouper,
    stdout_callback=group_callback,
    # ...
) as stream:
    stream.join()
```

### Offline Analysis

Read and process log files offline.

```python
from logpyt import read_file, LogFileReader
from logpyt.parsers import ThreadTimeLogParser

# Read all logs into a list
entries = read_file("app.log", parser=ThreadTimeLogParser())

# Iterate over logs (memory efficient)
reader = LogFileReader("app.log", parser=ThreadTimeLogParser())
for entry in reader:
    print(f"{entry.timestamp}: {entry.message}")
```

### Exporters

Export logs to JSON or CSV formats.

```python
from logpyt import read_file, export_logs

entries = read_file("app.log")

# Export to JSON
export_logs(entries, "output.json", format="json")

# Export to CSV
export_logs(entries, "output.csv", format="csv")
```

### AsyncIO Support

Use `AsyncLogStream` for non-blocking, high-performance log capturing with `asyncio`.

```python
import asyncio
from logpyt import AsyncLogStream, AsyncStreamHandle, LogEntry

async def async_callback(entry: LogEntry, handle: AsyncStreamHandle):
    print(f"Async Log: {entry.message}")
    if "STOP" in entry.message:
        await handle.stop()

async def main():
    async with AsyncLogStream(
        stdout_callback=async_callback
    ) as stream:
        # Stream runs in background
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
```

## Components Overview

- **`LogEntry`**: Structured representation of a log line (timestamp, pid, tid, level, tag, message, etc.). Use `.json_payload` to extract JSON content from the message.
- **`LogParser`**: Parses raw logcat lines into `LogEntry` objects.
- **`LogStream` / `AsyncLogStream`**: Manages the ADB subprocess and dispatching of logs.
- **`StreamHandle` / `AsyncStreamHandle`**: Safe handle to control the stream (stop, kill, pause, resume) from callbacks.
- **`Filter` / `AdvancedFilter`**: Predicates to filter log entries.
- **`LogGrouper`**: Groups consecutive logs based on keys and time thresholds.
- **`utils`**:
  - `resolve_adb()`: Finds the ADB executable.
  - `list_devices()`: Lists connected devices.
  - `read_file()` / `LogFileReader`: Tools for offline log analysis.
  - `export_logs()`: Exports logs to JSON/CSV.
