"""Log parsers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import cast

from ..models import LogEntry, LogLevel

_LOCAL_TIMEZONE = datetime.now(UTC).astimezone().tzinfo or UTC


def _naive_now() -> datetime:
    """Return the current local wall-clock time as a naive datetime."""
    return datetime.now(_LOCAL_TIMEZONE).replace(tzinfo=None)


class LogParser:
    """Base class for parsing logs.

    This class provides a standard interface for parsing log lines from
    stdout and stderr. To implement a custom parser, subclass this class
    and override the `parse_common` method (or `parse_stdout`/`parse_stderr`
    if you need stream-specific logic).

    Examples:
        class MyCustomParser(LogParser):
            def parse_common(self, line: str, source: str) -> LogEntry:
                # Custom parsing logic here
                return LogEntry(...)
    """

    def __init__(
        self,
        default_timestamp: datetime | None = None,
        default_year: int | None = None,
    ) -> None:
        """Initialize the parser.

        Args:
            default_timestamp: A default timestamp to use if the log line
                does not contain one. If None, defaults to datetime.now()
                when needed.
            default_year: A default year to use if the log line contains
                a date without a year (e.g., "11-19"). If None, defaults
                to the current year.
                WARNING: Defaulting to the current year may be incorrect when
                parsing logs from a different year (e.g., past logs).
                Users should provide this explicitly if known.
        """
        self.default_timestamp = default_timestamp
        self.default_year = default_year or _naive_now().year

    def _get_default_timestamp(self) -> datetime:
        """Get the default timestamp to use when none is present in the log."""
        return self.default_timestamp or _naive_now()

    def parse_stdout(self, line: str) -> LogEntry:
        """Parse a line from stdout.

        Args:
            line: The log line from stdout.

        Returns:
            A LogEntry object.
        """
        return self.parse_common(line, source="stdout")

    def parse_stderr(self, line: str) -> LogEntry:
        """Parse a line from stderr.

        Args:
            line: The log line from stderr.

        Returns:
            A LogEntry object.
        """
        return self.parse_common(line, source="stderr")

    def parse_common(self, line: str, source: str) -> LogEntry:
        """Common parsing logic.

        This method is called by `parse_stdout` and `parse_stderr`.
        Subclasses should override this method to implement custom parsing logic.
        The default implementation creates a basic LogEntry with the current time
        and the raw line as the message.

        Args:
            line: The log line to parse.
            source: The source of the log line (e.g., "stdout", "stderr").

        Returns:
            A LogEntry object.
        """
        # Default behavior: treat the whole line as a message
        # Map source to a default level
        level: LogLevel = "E" if source == "stderr" else "I"

        return LogEntry(
            timestamp=self._get_default_timestamp(),
            pid=0,
            tid=0,
            level=level,
            tag="",
            message=line.strip(),
            raw=line,
            meta={"source": source},
        )


class ThreadTimeLogParser(LogParser):
    """Parser for threadtime log format.

    Format: date time pid tid level tag: message
    Example: 11-19 12:34:56.789  1234  5678 D MyTag   : Hello World

    Note:
        The `default_year` parameter in `__init__` defaults to the current year.
        This may be incorrect when parsing logs from a different year.
        It is recommended to provide `default_year` explicitly if parsing
        historical logs.
    """

    # Regex pattern for threadtime format
    # Group 1: Date (MM-DD)
    # Group 2: Time (HH:MM:SS.mmm)
    # Group 3: PID
    # Group 4: TID
    # Group 5: Level
    # Group 6: Tag
    # Group 7: Message
    _PATTERN = re.compile(
        r"^(\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d{3})\s+(\d{1,7})\s+(\d{1,7})\s+([A-Z])\s+(.*?):(?:\s+(.*))?$"
    )

    def __init__(
        self,
        default_timestamp: datetime | None = None,
        default_year: int | None = None,
    ) -> None:
        """Initialize the threadtime parser (tracks month for year rollover)."""
        super().__init__(default_timestamp, default_year)
        self._last_month: int | None = None
        self._year_offset = 0

    def _parse_timestamp(self, date_str: str, time_str: str) -> datetime:
        """Parse threadtime timestamp without strptime for hot-path performance.

        The threadtime format carries no year; the year is rolled forward when the
        month decreases relative to the previous line, so a Dec -> Jan boundary
        stays chronologically ordered.
        """
        month = int(date_str[0:2])
        day = int(date_str[3:5])
        hour = int(time_str[0:2])
        minute = int(time_str[3:5])
        second = int(time_str[6:8])
        microsecond = int(time_str[9:12]) * 1000
        offset = self._year_offset
        if self._last_month is not None and month < self._last_month:
            offset += 1
        timestamp = datetime(
            self.default_year + offset,
            month,
            day,
            hour,
            minute,
            second,
            microsecond,
            tzinfo=_LOCAL_TIMEZONE,
        ).replace(tzinfo=None)
        # Commit rollover state only after a valid date was constructed.
        self._year_offset = offset
        self._last_month = month
        return timestamp

    def parse_stdout(self, line: str) -> LogEntry:
        """Parse a line from stdout using threadtime format.

        Args:
            line: The log line from stdout.

        Returns:
            A LogEntry object.
        """
        # Strip whitespace from ends to ensure clean matching
        clean_line = line.strip()
        # perf: early-reject fast path; boolean-expr count is intentional
        if (
            len(clean_line) < 24  # noqa: PLR0916
            or clean_line[2] != "-"
            or clean_line[5] != " "
            or not clean_line[0:2].isdigit()
            or not clean_line[3:5].isdigit()
            or ":" not in clean_line
        ):
            return super().parse_stdout(line)

        match = self._PATTERN.match(clean_line)
        if not match:
            return super().parse_stdout(line)

        date_str, time_str, pid_str, tid_str, level_str, tag, message = match.groups()

        # A malformed calendar value (e.g. month 13) means this is not a real
        # threadtime line: fall back to raw rather than inventing a timestamp.
        try:
            timestamp = self._parse_timestamp(date_str, time_str)
        except ValueError:
            return super().parse_stdout(line)

        if level_str not in {"V", "D", "I", "W", "E", "F"}:
            return super().parse_stdout(line)

        # pid/tid are bounded to <= 7 digits by the regex, so int() cannot blow up.
        pid = int(pid_str)
        tid = int(tid_str)
        # The message group is optional (empty-message lines), so it may be None.
        message = message or ""

        # level_str was validated against the allowed set above.
        level = cast("LogLevel", level_str)

        return LogEntry(
            timestamp=timestamp,
            pid=pid,
            tid=tid,
            level=level,
            tag=tag.strip(),
            message=message,
            raw=line,
            meta={"source": "stdout", "parser": "ThreadTimeLogParser"},
        )


class BriefLogParser(LogParser):
    """Parser for brief log format.

    Format: priority/tag(pid): message
    Example: D/HeadsetProfile( 2034): routeCall()
    """

    # Regex pattern for brief format
    # Group 1: Level
    # Group 2: Tag
    # Group 3: PID
    # Group 4: Message
    _PATTERN = re.compile(r"^([VDIWEF])/(.+)\(\s*(\d{1,7})\):\s+(.*)$")

    def parse_stdout(self, line: str) -> LogEntry:
        """Parse a line from stdout using brief format.

        Args:
            line: The log line from stdout.

        Returns:
            A LogEntry object.
        """
        clean_line = line.strip()
        # perf: early-reject fast path; boolean-expr count is intentional
        if (
            len(clean_line) < 8  # noqa: PLR0916
            or clean_line[0] not in "VDIWEF"
            or clean_line[1] != "/"
            or "(" not in clean_line
            or ")" not in clean_line
            or ":" not in clean_line
        ):
            return super().parse_stdout(line)

        match = self._PATTERN.match(clean_line)
        if not match:
            return super().parse_stdout(line)

        level_str, tag, pid_str, message = match.groups()

        return LogEntry(
            timestamp=self._get_default_timestamp(),
            pid=int(pid_str),
            tid=0,
            level=cast("LogLevel", level_str),
            tag=tag.strip(),
            message=message,
            raw=line,
            meta={"source": "stdout", "parser": "BriefLogParser"},
        )


class ProcessLogParser(LogParser):
    """Parser for process log format.

    Format: priority(pid) message
    Example: I(  596) System.exit called, status: 0
    """

    # Regex pattern for process format
    # Group 1: Level
    # Group 2: PID
    # Group 3: Message
    _PATTERN = re.compile(r"^([VDIWEF])\(\s*(\d{1,7})\)\s+(.*)$")

    def parse_stdout(self, line: str) -> LogEntry:
        """Parse a line from stdout using process format.

        Args:
            line: The log line from stdout.

        Returns:
            A LogEntry object.
        """
        clean_line = line.strip()
        if (
            len(clean_line) < 5
            or clean_line[0] not in "VDIWEF"
            or clean_line[1] != "("
            or ")" not in clean_line
        ):
            return super().parse_stdout(line)

        match = self._PATTERN.match(clean_line)
        if not match:
            return super().parse_stdout(line)

        level_str, pid_str, message = match.groups()

        return LogEntry(
            timestamp=self._get_default_timestamp(),
            pid=int(pid_str),
            tid=0,
            level=cast("LogLevel", level_str),
            tag="",
            message=message,
            raw=line,
            meta={"source": "stdout", "parser": "ProcessLogParser"},
        )


class TagLogParser(LogParser):
    """Parser for tag log format.

    Format: priority/tag: message
    Example: D/HeadsetProfile: routeCall()
    """

    # Regex pattern for tag format
    # Group 1: Level
    # Group 2: Tag
    # Group 3: Message
    _PATTERN = re.compile(r"^([VDIWEF])/(.*?):\s+(.*)$")

    def parse_stdout(self, line: str) -> LogEntry:
        """Parse a line from stdout using tag format.

        Args:
            line: The log line from stdout.

        Returns:
            A LogEntry object.
        """
        clean_line = line.strip()
        if (
            len(clean_line) < 5
            or clean_line[0] not in "VDIWEF"
            or clean_line[1] != "/"
            or ":" not in clean_line
        ):
            return super().parse_stdout(line)

        match = self._PATTERN.match(clean_line)
        if not match:
            return super().parse_stdout(line)

        level_str, tag, message = match.groups()

        return LogEntry(
            timestamp=self._get_default_timestamp(),
            pid=0,
            tid=0,
            level=cast("LogLevel", level_str),
            tag=tag.strip(),
            message=message,
            raw=line,
            meta={"source": "stdout", "parser": "TagLogParser"},
        )


class RawLogParser(LogParser):
    """Parser for raw log format.

    Format: message
    Example: routeCall()
    """

    def parse_stdout(self, line: str) -> LogEntry:
        """Parse a line from stdout using raw format.

        Args:
            line: The log line from stdout.

        Returns:
            A LogEntry object.
        """
        # Raw parser treats everything as the message
        # This is essentially the same as the base class default,
        # but explicit for the 'raw' format type.
        return LogEntry(
            timestamp=self._get_default_timestamp(),
            pid=0,
            tid=0,
            level="I",  # Default level
            tag="",
            message=line.strip(),
            raw=line,
            meta={"source": "stdout", "parser": "RawLogParser"},
        )
