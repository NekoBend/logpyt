"""Log filtering logic."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from .models import LogEntry


class Condition(ABC):
    """Abstract base class for filter conditions."""

    @abstractmethod
    def check(self, entry: LogEntry) -> bool:
        """Check if the log entry satisfies the condition.

        Args:
            entry: The log entry to check.

        Returns:
            True if the condition is met, False otherwise.
        """
        ...

    def __and__(self, other: Condition) -> Condition:
        return And(self, other)

    def __or__(self, other: Condition) -> Condition:
        return Or(self, other)

    def __invert__(self) -> Condition:
        return Not(self)


class And(Condition):
    """Logical AND combination of conditions."""

    def __init__(self, *conditions: Condition) -> None:
        self.conditions = conditions

    def check(self, entry: LogEntry) -> bool:
        return all(c.check(entry) for c in self.conditions)


class Or(Condition):
    """Logical OR combination of conditions."""

    def __init__(self, *conditions: Condition) -> None:
        self.conditions = conditions

    def check(self, entry: LogEntry) -> bool:
        return any(c.check(entry) for c in self.conditions)


class Not(Condition):
    """Logical NOT of a condition."""

    def __init__(self, condition: Condition) -> None:
        self.condition = condition

    def check(self, entry: LogEntry) -> bool:
        return not self.condition.check(entry)


class _FieldMatchCondition(Condition):
    """Base class for conditions that match against a set of values."""

    def __init__(self, values: str | Iterable[str]) -> None:
        if isinstance(values, str):
            self.values = {values}
        else:
            self.values = set(values)

    @abstractmethod
    def _get_value(self, entry: LogEntry) -> Any: ...

    def check(self, entry: LogEntry) -> bool:
        val = self._get_value(entry)
        if val is None:
            return False
        return val in self.values


class Package(_FieldMatchCondition):
    """Matches the package name (requires metadata)."""

    def _get_value(self, entry: LogEntry) -> str | None:
        return entry.meta.get("package")


class Tag(_FieldMatchCondition):
    """Matches the log tag."""

    def _get_value(self, entry: LogEntry) -> str:
        return entry.tag


class Level(_FieldMatchCondition):
    """Matches the log level."""

    def _get_value(self, entry: LogEntry) -> str:
        return entry.level


class MessageContains(Condition):
    """Checks if the message contains any of the specified strings."""

    def __init__(self, patterns: str | Iterable[str]) -> None:
        if isinstance(patterns, str):
            self.patterns = [patterns]
        else:
            self.patterns = list(patterns)

    def check(self, entry: LogEntry) -> bool:
        return any(pattern in entry.message for pattern in self.patterns)


class CrashCondition(Condition):
    """Matches Android application crashes (FATAL EXCEPTION)."""

    def __init__(
        self,
        tag: str = "AndroidRuntime",
        level: str = "E",
        message_pattern: str = "FATAL EXCEPTION",
    ) -> None:
        self.tag = tag
        self.level = level
        self.message_pattern = message_pattern

    def check(self, entry: LogEntry) -> bool:
        return (
            entry.tag == self.tag
            and entry.level == self.level
            and self.message_pattern in entry.message
        )


class AnrCondition(Condition):
    """Matches Android ANRs (Application Not Responding)."""

    def __init__(
        self,
        tag: str = "ActivityManager",
        level: str = "E",
        message_prefix: str = "ANR in",
    ) -> None:
        self.tag = tag
        self.level = level
        self.message_prefix = message_prefix

    def check(self, entry: LogEntry) -> bool:
        return (
            entry.tag == self.tag
            and entry.level == self.level
            and entry.message.startswith(self.message_prefix)
        )


class AdvancedFilter:
    """A filter that uses a composable condition tree."""

    def __init__(self, condition: Condition) -> None:
        """Initialize with a root condition.

        Args:
            condition: The root condition object.
        """
        self.condition = condition

    def __call__(self, entry: LogEntry) -> bool:
        """Check if the entry matches the filter.

        Args:
            entry: The log entry.

        Returns:
            True if it matches, False otherwise.
        """
        return self.condition.check(entry)


class Filter:
    """A simple filter combining criteria with AND logic.

    This filter allows you to specify multiple criteria (package, tag, level, message content).
    A log entry must match ALL specified criteria to pass the filter. If a criterion accepts
    a list of values (e.g., `tag=["TagA", "TagB"]`), the entry matches if it matches ANY
    value in that list (OR logic within the field).

    Examples:
        Filter by tag "MyApp":
        >>> f = Filter(tag="MyApp")

        Filter by tag "MyApp" AND level "E" (Error):
        >>> f = Filter(tag="MyApp", level="E")

        Filter by tag "MyApp" OR "MyService", AND message contains "Error":
        >>> f = Filter(tag=["MyApp", "MyService"], message_contains="Error")
    """

    def __init__(
        self,
        package: str | list[str] | None = None,
        tag: str | list[str] | None = None,
        level: str | list[str] | None = None,
        message_contains: str | list[str] | None = None,
    ) -> None:
        """Initialize the filter.

        Args:
            package: Package name(s) to match. Requires 'package' in LogEntry.meta.
            tag: Tag(s) to match.
            level: Log level(s) to match (e.g., "E", "W").
            message_contains: Substring(s) to look for in the message.
        """
        self.packages = self._to_set(package)
        self.tags = self._to_set(tag)
        self.levels = self._to_set(level)
        self.message_patterns = self._to_list(message_contains)

    def _to_set(self, value: str | list[str] | None) -> set[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return {value}
        return set(value)

    def _to_list(self, value: str | list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return [value]
        return list(value)

    def __call__(self, entry: LogEntry) -> bool:
        """Check if the entry matches all criteria.

        Args:
            entry: The log entry.

        Returns:
            True if it matches, False otherwise.
        """
        if self.packages is not None:
            pkg = entry.meta.get("package")
            if pkg is None or pkg not in self.packages:
                return False

        if self.tags is not None and entry.tag not in self.tags:
            return False

        if self.levels is not None and entry.level not in self.levels:
            return False

        if self.message_patterns is not None:
            msg = entry.message
            if not any(p in msg for p in self.message_patterns):
                return False

        return True
