"""Log parsers."""

from __future__ import annotations

import re
from datetime import datetime

from ..models import LogEntry, LogLevel


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
        self.default_year = default_year or datetime.now().year

    def _get_default_timestamp(self) -> datetime:
        """Get the default timestamp to use when none is present in the log."""
        return self.default_timestamp or datetime.now()

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
        r"^(\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d{3})\s+(\d+)\s+(\d+)\s+([A-Z])\s+(.*?):\s+(.*)$"
    )

    def parse_stdout(self, line: str) -> LogEntry:
        """Parse a line from stdout using threadtime format.

        Args:
            line: The log line from stdout.

        Returns:
            A LogEntry object.
        """
        # Strip whitespace from ends to ensure clean matching
        clean_line = line.strip()
        match = self._PATTERN.match(clean_line)
        if not match:
            return super().parse_stdout(line)

        date_str, time_str, pid_str, tid_str, level_str, tag, message = match.groups()

        # Parse timestamp
        # Use configured default year
        timestamp_str = f"{self.default_year}-{date_str} {time_str}"
        try:
            timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            # Fallback if timestamp parsing fails, though regex ensures format
            timestamp = self._get_default_timestamp()

        # Cast fields
        pid = int(pid_str)
        tid = int(tid_str)

        # Ensure level is treated as LogLevel
        # In practice, this will be one of V, D, I, W, E, F
        level: LogLevel = level_str  # type: ignore

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
    _PATTERN = re.compile(r"^([VDIWEF])/([^(]+)\(\s*(\d+)\):\s+(.*)$")

    def parse_stdout(self, line: str) -> LogEntry:
        """Parse a line from stdout using brief format.

        Args:
            line: The log line from stdout.

        Returns:
            A LogEntry object.
        """
        clean_line = line.strip()
        match = self._PATTERN.match(clean_line)
        if not match:
            return super().parse_stdout(line)

        level_str, tag, pid_str, message = match.groups()

        return LogEntry(
            timestamp=self._get_default_timestamp(),
            pid=int(pid_str),
            tid=0,
            level=level_str,  # type: ignore
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
    _PATTERN = re.compile(r"^([VDIWEF])\(\s*(\d+)\)\s+(.*)$")

    def parse_stdout(self, line: str) -> LogEntry:
        """Parse a line from stdout using process format.

        Args:
            line: The log line from stdout.

        Returns:
            A LogEntry object.
        """
        clean_line = line.strip()
        match = self._PATTERN.match(clean_line)
        if not match:
            return super().parse_stdout(line)

        level_str, pid_str, message = match.groups()

        return LogEntry(
            timestamp=self._get_default_timestamp(),
            pid=int(pid_str),
            tid=0,
            level=level_str,  # type: ignore
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
        match = self._PATTERN.match(clean_line)
        if not match:
            return super().parse_stdout(line)

        level_str, tag, message = match.groups()

        return LogEntry(
            timestamp=self._get_default_timestamp(),
            pid=0,
            tid=0,
            level=level_str,  # type: ignore
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
