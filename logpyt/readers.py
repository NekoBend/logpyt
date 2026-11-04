"""Log file readers."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .parsers import LogParser, ParseContext, ThreadTimeLogParser
from .parsers._fallback import is_divider, unparsed_entry

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
        *,
        year: int | None = None,
    ) -> None:
        """Initialize the reader.

        Args:
            file_path: Path to the log file.
            parser: Parser to use. If None, defaults to ThreadTimeLogParser.
                A line the parser does not recognise is yielded as an entry
                with ``parsed=False`` whose message is the whole line.
            filter_by: Optional filter to apply to entries. Only entries
                passing the filter will be yielded.
            parse_error_log_interval: Minimum interval in seconds between
                repeated parse error logs. Defaults to 5.0.
            year: The year of the first line that carries no year (yearless
                ``-v threadtime``). None places it before the file's
                modification time (see ``ParseContext``), or before the host
                clock when the modification time is no valid local datetime.
        """
        self.file_path = Path(file_path)
        self.parser = parser or ThreadTimeLogParser()
        self.filter_by = filter_by
        ParseContext(year=year)  # a bad year fails here, not at the first line
        self.year = year
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
        using the configured parser, in a parse session of its own: every
        iteration starts afresh, so iterating twice gives the same entries.
        If a filter is set, only matching entries are yielded.

        Lines the parser does not recognise (logcat's ``--------- beginning
        of`` dividers excepted) are counted, and one WARNING per iteration
        reports how many there were (F6-5).

        Yields:
            Parsed LogEntry objects that match the filter.

        Raises:
            FileNotFoundError: If the file does not exist.
            PermissionError: If the file cannot be read.
        """
        unparsed = 0
        lines = 0
        try:
            with self.file_path.open("r", encoding="utf-8", errors="replace") as f:
                # The mtime anchors the first yearless line only without year=.
                anchor = None if self.year is not None else _mtime(f.fileno())
                ctx = ParseContext(year=self.year, anchor=anchor)
                for line in f:
                    lines += 1
                    try:
                        entry = self.parser.parse(line, ctx)
                    except Exception as e:  # noqa: BLE001
                        # Log parse errors with throttling to avoid log storms
                        self._log_parse_error(line, e)
                        continue
                    if entry is None:
                        entry = unparsed_entry(line, ctx)
                        if not is_divider(entry.raw):
                            unparsed += 1

                    # Apply filter if present
                    if self.filter_by and not self.filter_by(entry):
                        continue

                    yield entry
        finally:
            if unparsed:
                logger.warning(
                    "%d of %d lines in %s are not in the %r format of %s; "
                    "they were read as unparsed entries (parsed=False).",
                    unparsed,
                    lines,
                    self.file_path,
                    getattr(self.parser, "logcat_format", "?"),
                    type(self.parser).__name__,
                )

        if self._suppressed_parse_errors:
            logger.warning(
                "Suppressed %d parse errors while reading %s.",
                self._suppressed_parse_errors,
                self.file_path,
            )
            self._suppressed_parse_errors = 0


def _mtime(fd: int) -> datetime | None:
    """The modification time of the open file ``fd``, as naive host-local time.

    None when it cannot be read or is no datetime in host-local time (an mtime
    far outside the years 1..9999, or one the platform's time functions
    refuse); the first yearless line is then placed before the host clock.
    """
    try:
        moment = datetime.fromtimestamp(os.fstat(fd).st_mtime, tz=UTC)
        return moment.astimezone().replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return None


def read_file(
    file_path: str | Path,
    parser: LogParser | None = None,
    filter_by: Filter | None = None,
    *,
    year: int | None = None,
) -> Iterator[LogEntry]:
    """Read all log entries from a file.

    This is a convenience function wrapping LogFileReader.

    Args:
        file_path: Path to the log file.
        parser: Parser to use.
        filter_by: Optional filter.
        year: The year of the first line that carries no year; None places it
            before the file's modification time.

    Returns:
        Iterator of LogEntry objects.
    """
    yield from LogFileReader(file_path, parser, filter_by, year=year)
