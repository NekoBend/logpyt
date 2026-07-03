# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-07-03

### Breaking Changes

- Removed the deprecated `LogStreamStdErrError` alias. Use the canonical
  `LogStreamStderrError` instead.
- `JsonLogExporter`/`export_logs` now produce **compact JSON by default**
  (`indent=None`). Pass `indent=` explicitly to restore pretty-printed output.

### Added

- Re-export `AsyncLogStream`, `AsyncStreamHandle`, `LogFileReader`, `read_file`,
  `export_logs`, and `WindowedLogGrouper` from the top-level `logpyt` package.
- Configurable parse-error log throttling in `LogFileReader`.
- Configurable stream callback-queue overflow policy and PID-resolution controls.
- A `hatchling` build backend so the project is installable and publishable.
- GitHub Actions CI: `ruff` lint + format check, `ty` type check, and `pytest`
  across Ubuntu/Windows and Python 3.12-3.14.

### Changed

- Set supported Python to `>=3.12` (previously `>=3.13`).
- The default log parser is now `ThreadTimeLogParser` (was a raw pass-through
  base parser), matching `adb logcat`'s default output format.
- Adopted a strict `ruff` lint configuration and upgraded the `ruff`/`ty`
  toolchain.
- Performance optimizations across parsers (hot-path parsing, early-reject),
  groupers (windowed-grouper heap compaction), PID monitors (adaptive polling
  backoff, fewer map rebuilds), and exporters (compact JSON/CSV serialization).

### Fixed

- Hardened ADB subprocess output handling: decode with `errors="replace"` and
  tolerate malformed `pidof` output instead of raising.
- `resolve_adb` validates that the resolved path is an executable file and uses
  `pathlib`.
- Surface previously-silent PID-resolution failures via debug logging.
- Sanitize control characters in reader parse-error previews.
- Deterministic fallback on invalid threadtime log levels.
- Hardened sync/async stream lifecycle, the async pause-state race, and
  grouper flush-on-shutdown.
- Honor `join(timeout=...)` during the callback-drain phase, preserve PID
  mappings under throttled async polling, and make async dispatcher shutdown
  non-blocking on a full queue.
- Robustness against malformed device output: bound parsed PID/TID length (a
  pathological value could crash `int()` and tear down the stream), parse
  empty-message lines, fall back to raw on impossible dates, enlarge the async
  read buffer so oversized lines no longer permanently stall the reader, and
  length-guard `pidof` output.
- Guard user lifecycle callbacks (`on_start`/`on_stop`/`on_error`) so a raising
  callback cannot corrupt stream state or leak the subprocess; serialize the
  shared grouper in the sync stream; make `drop_newest` actually drop instead of
  blocking ingestion; bound the async dispatcher drain so a stuck callback cannot
  hang shutdown; make sync `join()` re-raise non-destructively like the async one.
- Treat an empty filter criterion as "no constraint" (was "match nothing"),
  sanitize CSV output against spreadsheet formula injection, and honor
  `LogEntry.to_json(ensure_ascii=...)`.
- Restrict grouping to stdout so a stderr line can no longer flush and misroute
  a buffered stdout group to the wrong callback.
- Validate the log level before parsing a threadtime timestamp so a rejected
  line cannot corrupt year-rollover state; only a genuine Dec->Jan boundary now
  advances the year, so out-of-order buffer jitter no longer causes permanent
  timestamp drift.
- Parse threadtime lines with microsecond (`.ffffff`) precision instead of
  dropping them to the raw fallback.
- Terminate an adb process spawned while `stop()` runs (during start/reconnect)
  so it cannot be orphaned and leak.
- Close a CSV formula-injection bypass where leading whitespace before `=+-@`
  evaded sanitization; report the dropped count when a grouped batch overflows.
- Treat empty low-level filter conditions (`Tag([])`, `Level([])`,
  `MessageContains([])`) as "no constraint", consistent with `Filter`.

[2.0.0]: https://github.com/NekoBend/logpyt/compare/v1.0.0...v2.0.0
