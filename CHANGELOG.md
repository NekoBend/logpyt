# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-07-02

### Breaking Changes

- Removed the deprecated `LogStreamStdErrError` alias. Use the canonical
  `LogStreamStderrError` instead.
- `JsonLogExporter`/`export_logs` now produce **compact JSON by default**
  (`indent=None`). Pass `indent=` explicitly to restore pretty-printed output.

### Added

- Re-export `AsyncLogStream`, `AsyncStreamHandle`, `LogFileReader`, `read_file`,
  and `export_logs` from the top-level `logpyt` package.
- Configurable parse-error log throttling in `LogFileReader`.
- Configurable stream callback-queue overflow policy and PID-resolution controls.
- A `hatchling` build backend so the project is installable and publishable.
- GitHub Actions CI: `ruff` lint + format check, `ty` type check, and `pytest`
  across Ubuntu/Windows and Python 3.12-3.14.

### Changed

- Set supported Python to `>=3.12` (previously `>=3.13`).
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

[2.0.0]: https://github.com/NekoBend/logpyt/compare/v1.0.0...v2.0.0
