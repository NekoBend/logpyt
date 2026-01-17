"""Data models for log entries."""

from __future__ import annotations

from datetime import datetime
from functools import cached_property
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..utils import extract_json

# Type definitions
LogLevel = Literal["V", "D", "I", "W", "E", "F"]


class LogEntry(BaseModel):
    """A structured log entry representing a single line of log output.

    This class encapsulates all standard fields found in common log formats (like Android's logcat),
    along with the raw log line and any additional metadata.

    Attributes:
        timestamp: The datetime object representing when the log entry was created.
        pid: Process ID (integer) that generated the log.
        tid: Thread ID (integer) that generated the log.
        level: Log severity level. Must be one of:
            - "V": Verbose
            - "D": Debug
            - "I": Info
            - "W": Warning
            - "E": Error
            - "F": Fatal
        tag: A short string tag identifying the component or category (e.g., "ActivityManager").
        message: The main content of the log message.
        raw: The original, unmodified raw string of the log line.
        meta: A dictionary for additional metadata (e.g., source stream, package name).
            Defaults to an empty dict.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    timestamp: datetime
    pid: int
    tid: int
    level: LogLevel
    tag: str
    message: str
    raw: str
    meta: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert the log entry to a dictionary.

        Returns:
            A dictionary representation of the log entry.
        """
        return self.model_dump()

    def to_json(self, ensure_ascii: bool = False, indent: int | None = None) -> str:
        """Convert the log entry to a JSON string.

        Args:
            ensure_ascii: If True, non-ASCII characters are escaped.
                Note: When using model_dump_json, ensure_ascii is not directly supported
                in the same way as json.dumps in all Pydantic versions, but we prioritize
                performance.
            indent: If specified, formats the JSON with the given indentation.

        Returns:
            A JSON string representation of the log entry.
        """
        # Use model_dump_json for better performance (avoids double serialization)
        return self.model_dump_json(indent=indent)

    @cached_property
    def json_payload(self) -> Any | None:
        """Extract JSON payload from the log message.

        Returns:
            The parsed JSON object/array if found in the message, otherwise None.
        """
        return extract_json(self.message)
