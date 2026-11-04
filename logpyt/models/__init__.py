"""Data models: the log entry and the group of entries."""

from .entry import LogEntry, LogLevel
from .group import LogGroup

__all__ = ["LogEntry", "LogGroup", "LogLevel"]
