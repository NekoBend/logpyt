"""Data models for log entries."""

from __future__ import annotations

import json
from datetime import datetime
from functools import cached_property
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..utils import extract_json

# Type definitions
LogLevel = Literal["V", "D", "I", "W", "E", "F"]


class LogEntry(BaseModel):
    """A structured log entry representing a single line of log output.

    This class encapsulates all standard fields found in common log
    formats (like Android's logcat), along with the raw log line and
    any additional metadata.

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
        tag: A short string tag identifying the component or
            category (e.g., "ActivityManager").
        message: The main content of the log message.
        raw: The original, unmodified raw string of the log line.
        meta: A dictionary for additional metadata (e.g., source stream, package name).
            Defaults to an empty dict. A dict passed in is stored by reference
            (not deep-copied); pass a fresh dict per entry if you mutate it later.

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

    def to_json(
        self,
        ensure_ascii: bool = False,  # noqa: FBT001, FBT002  (existing public signature)
        indent: int | None = None,
    ) -> str:
        """Convert the log entry to a JSON string.

        Args:
            ensure_ascii: If True, non-ASCII characters are escaped as ``\\uXXXX``
                sequences. If False (default), they are kept literally.
            indent: If specified, formats the JSON with the given indentation.

        Returns:
            A JSON string representation of the log entry.

        """
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=ensure_ascii,
            indent=indent,
        )

    @cached_property
    def json_payload(self) -> Any | None:  # noqa: ANN401  (arbitrary parsed JSON)
        """Extract JSON payload from the log message.

        Returns:
            The parsed JSON object/array if found in the message,
            otherwise None.

        """
        return extract_json(self.message)
