# logpyt

Structured logging toolkit around Android logcat streams.

[![CI](https://github.com/NekoBend/logpyt/actions/workflows/ci.yml/badge.svg)](https://github.com/NekoBend/logpyt/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)

## Features

- Live `adb logcat` streaming - synchronous (`LogStream`) and asyncio (`AsyncLogStream`).
- Multiple logcat format parsers: threadtime (default), brief, process, tag, raw.
- Composable filtering: conditions combined with `&`, `|`, `~`, plus a simple `Filter`.
- Built-in crash / ANR detection and time-window grouping.
- Offline log-file analysis and JSON / CSV export.

## Installation

Requires **Python 3.12+** and the Android `adb` executable (auto-discovered by
`logpyt.adb.resolve_adb()`, or pass `adb_path=` explicitly).

```bash
uv pip install git+https://github.com/NekoBend/logpyt.git
```

## Usage Examples

### Basic Usage

Capture logcat output using `LogStream`.

```python
from logpyt import LogStream, LogEntry, StreamHandle
from logpyt.adb import resolve_adb

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

Every condition (`Tag`, `Level`, `MinLevel`, `Package`, `MessageContains`,
`CrashCondition`, `AnrCondition`, `Where`) is a filter, and conditions combine with
`&`, `|` and `~`. `Filter` is the AND of keyword criteria. Levels are letters or
names in any case (`"E"`, `"error"`, `"Warning"`); invalid values raise at once.
A `Package` anywhere in the condition makes the stream resolve that package's pids.

```python
from logpyt import (
    AnrCondition, CrashCondition, Filter, LogStream, MessageContains, MinLevel,
    Package, Tag,
)

# Simple Filter (AND logic)
simple_filter = Filter(
    package="com.example.app",
    min_level="warning",
    tag="MyApp"
)

# Conditions combined with &, | and ~
condition = (
    (Package("com.example.app") & Tag("MyApp"))
    | MessageContains("CRITICAL")
)

# The app's crashes (Java and native) and ANRs, read from the lines' text:
# no package attribution needed, so this also works on log files.
crash_filter = (
    CrashCondition(package="com.example.app") | AnrCondition(package="com.example.app")
)

with LogStream(
    filter_by=condition,
    # ... callbacks ...
) as stream:
    stream.join()
```

### Grouping

Group logs by fields (e.g., PID, TID) that arrive within a time gap. A stream
given `group_by=GroupBy(...)` delivers `LogGroup` objects: a sequence of the
entries plus `key` and the `reason` the group closed (`"gap"`, `"key"`,
`"size"`, `"evicted"`, `"idle"` or `"end"`). `mode="consecutive"` keeps one
group open; `mode="windowed"` keeps one per key, so interleaved threads group
apart. Memory is bounded (`max_group_size`, `max_total_entries`,
`max_total_bytes`), and invalid arguments raise at once.

```python
from logpyt import GroupBy, LogGroup, LogStream, StreamHandle, read_file

# Group logs by PID and TID if they occur within 50ms of each other
group_by = GroupBy(keys=("pid", "tid"), gap_ms=50.0, mode="windowed")

def group_callback(group: LogGroup, handle: StreamHandle):
    print(f"Received group of {len(group)} logs ({group.reason})")
    for entry in group:
        print(f"  - {entry.message}")

with LogStream(
    group_by=group_by,
    stdout_callback=group_callback,
    # ...
) as stream:
    stream.join()

# The same grouping offline
for group in read_file("app.log", group_by=group_by):
    print(group.key, len(group))
```

### Offline Analysis

Read and process log files offline. `read_file` yields the entries lazily, in
one pass per call; every argument after the path is keyword-only. A line ends
at LF only (CR LF and CR CR LF files read like LF files, and a CR inside a
message stays in it), a UTF-8 BOM at the start is skipped, and a line longer
than `max_line_bytes` (64 KiB) becomes one entry with `truncated=True`.

```python
from logpyt import MinLevel, Package, read_file
from logpyt.parsers import BriefLogParser

# Iterate over the entries (memory efficient); list() keeps them
for entry in read_file("app.log", where=MinLevel("W")):
    print(f"{entry.timestamp}: {entry.message}")

# Another logcat format: pass its parser
entries = list(read_file("brief.log", parser=BriefLogParser()))

# A file carries no package names: a Package condition needs the uid of each
# package, for lines captured with `-v uid` (it raises ValueError without them).
# Another user's lines (uid 1010065 for user 10) match the app id 10065 too.
app_lines = read_file(
    "capture.log",
    where=Package("com.example.app"),
    uid_packages={10065: "com.example.app"},
)
```

### Exporters

Export logs to JSON or CSV formats.

```python
from logpyt import read_file, export_logs

# read_file yields lazily; materialize with list() so it can be exported twice.
entries = list(read_file("app.log"))

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

- **`LogEntry`**: Structured representation of a log line (timestamp, pid, tid, level, tag, message, etc.). Use `.json_payload()` to extract JSON content from the message.
- **`LogParser`**: Parses raw logcat lines into `LogEntry` objects.
- **`LogStream` / `AsyncLogStream`**: Manages the ADB subprocess and dispatching of logs.
- **`StreamHandle` / `AsyncStreamHandle`**: Safe handle to control the stream (stop, kill, pause, resume) from callbacks.
- **`Condition` / `Filter`**: Predicates to filter log entries (`&`, `|`, `~`).
- **`GroupBy` / `LogGroup`**: Groups logs by keys and time gaps; a group is a sequence of entries with its key and close reason.
- **`logpyt.adb`**:
  - `resolve_adb()`: Finds the ADB executable.
  - `list_devices()`, `wait_for_device()`, `adb_connect()`: Device helpers (async twins included).
- **Offline and export**:
  - `read_file()`: Reads a log file offline, as entries or, with `group_by=`, groups.
  - `export_logs()`: Exports logs to JSON/CSV.

## License

MIT - see [LICENSE](LICENSE).
