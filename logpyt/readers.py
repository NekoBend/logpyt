"""Log file readers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from .parsers import LogParser

if TYPE_CHECKING:
    from .filters import Filter
    from .models import LogEntry

logger = logging.getLogger(__name__)


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
    ) -> None:
        """Initialize the reader.

        Args:
            file_path: Path to the log file.
            parser: Parser to use. If None, uses default LogParser which
                treats each line as a raw message.
            filter_by: Optional filter to apply to entries. Only entries
                passing the filter will be yielded.
        """
        self.file_path = Path(file_path)
        self.parser = parser or LogParser()
        self.filter_by = filter_by

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
                except Exception as e:
                    # Log the error instead of silently swallowing it
                    logger.error(
                        "Failed to parse line: %s - Error: %s", line.strip(), e
                    )
                    continue

                # Apply filter if present
                if self.filter_by and not self.filter_by(entry):
                    continue

                yield entry


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
    reader = LogFileReader(file_path, parser, filter_by)
    yield from reader
