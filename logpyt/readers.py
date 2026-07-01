"""Log file readers."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .parsers import LogParser

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .filters import Filter
    from .models import LogEntry

logger = logging.getLogger(__name__)
_PARSE_ERROR_PREVIEW_LIMIT = 160


class LogFileReader:
    """Reads and parses log entries from a file.

    This class allows iterating over log entries from a file, applying
    parsing and filtering on the fly.
    """

    def __init__(
        self,
        file_path: str | Path,
        parser: LogParser | None = None,
        filter_by: Filter | None = None,
        parse_error_log_interval: float = 5.0,
    ) -> None:
        """Initialize the reader.

        Args:
            file_path: Path to the log file.
            parser: Parser to use. If None, uses default LogParser which
                treats each line as a raw message.
            filter_by: Optional filter to apply to entries. Only entries
                passing the filter will be yielded.
            parse_error_log_interval: Minimum interval in seconds between
                repeated parse error logs. Defaults to 5.0.
        """
        self.file_path = Path(file_path)
        self.parser = parser or LogParser()
        self.filter_by = filter_by
        self._parse_error_log_interval = max(0.0, parse_error_log_interval)
        self._last_parse_error_log_ts = 0.0
        self._suppressed_parse_errors = 0

    @staticmethod
    def _sanitize_line_preview(
        line: str, max_len: int = _PARSE_ERROR_PREVIEW_LIMIT
    ) -> str:
        """Sanitize untrusted log line content for diagnostics."""
        stripped = line.rstrip("\r\n")
        sanitized = "".join(
            ch if (ch.isprintable() and ch != "\x1b") else "?" for ch in stripped
        )
        if len(sanitized) > max_len:
            return f"{sanitized[: max_len - 3]}..."
        return sanitized

    def _log_parse_error(self, line: str, error: Exception) -> None:
        """Log parse errors with optional rate limiting."""
        preview = self._sanitize_line_preview(line)
        interval = self._parse_error_log_interval
        if interval <= 0.0:
            logger.error("Failed to parse line: %s - Error: %s", preview, error)
            return

        now = time.monotonic()
        if (now - self._last_parse_error_log_ts) >= interval:
            if self._suppressed_parse_errors:
                logger.warning(
                    "Suppressed %d parse errors while reading %s.",
                    self._suppressed_parse_errors,
                    self.file_path,
                )
                self._suppressed_parse_errors = 0

            logger.error("Failed to parse line: %s - Error: %s", preview, error)
            self._last_parse_error_log_ts = now
            return

        self._suppressed_parse_errors += 1

    def __iter__(self) -> Iterator[LogEntry]:
        """Iterate over log entries in the file.

        Opens the file and reads it line by line. Each line is parsed
        using the configured parser. If a filter is set, only matching
        entries are yielded.

        Yields:
            Parsed LogEntry objects that match the filter.

        Raises:
            FileNotFoundError: If the file does not exist.
            PermissionError: If the file cannot be read.
        """
        with self.file_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    # Parse the line
                    # We use parse_stdout as the default for file lines
                    entry = self.parser.parse_stdout(line)
                except Exception as e:  # noqa: BLE001
                    # Log parse errors with throttling to avoid log storms
                    self._log_parse_error(line, e)
                    continue

                # Apply filter if present
                if self.filter_by and not self.filter_by(entry):
                    continue

                yield entry

        if self._suppressed_parse_errors:
            logger.warning(
                "Suppressed %d parse errors while reading %s.",
                self._suppressed_parse_errors,
                self.file_path,
            )
            self._suppressed_parse_errors = 0


def read_file(
    file_path: str | Path,
    parser: LogParser | None = None,
    filter_by: Filter | None = None,
) -> Iterator[LogEntry]:
    """Read all log entries from a file.

    This is a convenience function wrapping LogFileReader.

    Args:
        file_path: Path to the log file.
        parser: Parser to use.
        filter_by: Optional filter.

    Returns:
        Iterator of LogEntry objects.
    """
    yield from LogFileReader(file_path, parser, filter_by)
