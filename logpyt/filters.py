"""Log filtering logic."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .models import LogEntry, LogLevel


@lru_cache(maxsize=256)
def _compile_contains_regex(patterns: tuple[str, ...]) -> re.Pattern[str] | None:
    """Compile a cached regex for literal substring matching.

    Args:
        patterns: Literal substring patterns.

    Returns:
        Compiled regex pattern, or None when no patterns are provided.

    """
    if not patterns:
        return None
    escaped = "|".join(re.escape(pattern) for pattern in patterns)
    return re.compile(escaped)


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
        """Return the AND combination of this and another condition.

        Returns:
            The combined condition.

        """
        return And(self, other)

    def __or__(self, other: Condition) -> Condition:
        """Return the OR combination of this and another condition.

        Returns:
            The combined condition.

        """
        return Or(self, other)

    def __invert__(self) -> Condition:
        """Return the negation of this condition.

        Returns:
            The combined condition.

        """
        return Not(self)


class And(Condition):
    """Logical AND combination of conditions."""

    def __init__(self, *conditions: Condition) -> None:
        """Initialize with one or more conditions."""
        self.conditions = conditions

    def check(self, entry: LogEntry) -> bool:
        """Return True if all conditions are met.

        Returns:
            Whether all conditions are satisfied.

        """
        return all(c.check(entry) for c in self.conditions)


class Or(Condition):
    """Logical OR combination of conditions."""

    def __init__(self, *conditions: Condition) -> None:
        """Initialize with one or more conditions."""
        self.conditions = conditions

    def check(self, entry: LogEntry) -> bool:
        """Return True if any condition is met.

        Returns:
            Whether any condition is satisfied.

        """
        return any(c.check(entry) for c in self.conditions)


class Not(Condition):
    """Logical NOT of a condition."""

    def __init__(self, condition: Condition) -> None:
        """Initialize with the condition to negate."""
        self.condition = condition

    def check(self, entry: LogEntry) -> bool:
        """Return True if the condition is not met.

        Returns:
            Whether the condition is not satisfied.

        """
        return not self.condition.check(entry)


class _FieldMatchCondition(Condition):
    """Base class for conditions that match against a set of values."""

    def __init__(self, values: str | Iterable[str]) -> None:
        """Initialize with value(s) to match against."""
        if isinstance(values, str):
            self.values = {values}
        else:
            self.values = set(values)

    @abstractmethod
    def _get_value(self, entry: LogEntry) -> str | None: ...

    def check(self, entry: LogEntry) -> bool:
        """Return True if the entry's field matches any of the values.

        Returns:
            Whether the field value matches.

        """
        val = self._get_value(entry)
        if val is None:
            return False
        return val in self.values


class Package(_FieldMatchCondition):
    """Matches the package name (requires metadata)."""

    def _get_value(self, entry: LogEntry) -> str | None:  # noqa: PLR6301
        # Polymorphic override of _FieldMatchCondition._get_value; called via
        # self._get_value() in the base check(). Must stay an instance method.
        return entry.meta.get("package")


class Tag(_FieldMatchCondition):
    """Matches the log tag."""

    def _get_value(self, entry: LogEntry) -> str:  # noqa: PLR6301
        # Polymorphic override of _FieldMatchCondition._get_value; called via
        # self._get_value() in the base check(). Must stay an instance method.
        return entry.tag


class Level(_FieldMatchCondition):
    """Matches the log level."""

    def __init__(self, values: LogLevel | Iterable[LogLevel]) -> None:
        """Initialize with log level(s) to match against.

        Typing the parameter as ``LogLevel`` lets a type checker catch an
        invalid literal such as ``Level("ERROR")`` at check time.
        """
        super().__init__(values)

    def _get_value(self, entry: LogEntry) -> str:  # noqa: PLR6301
        # Polymorphic override of _FieldMatchCondition._get_value; called via
        # self._get_value() in the base check(). Must stay an instance method.
        return entry.level


class MessageContains(Condition):
    """Checks if the message contains any of the specified strings."""

    def __init__(self, patterns: str | Iterable[str]) -> None:
        """Initialize with pattern(s) to search for."""
        if isinstance(patterns, str):
            self.patterns = [patterns]
        else:
            self.patterns = list(patterns)
        self._compiled_pattern = _compile_contains_regex(tuple(self.patterns))

    def check(self, entry: LogEntry) -> bool:
        """Return True if the message contains any pattern.

        Returns:
            Whether a pattern was found.

        """
        if self._compiled_pattern is None:
            return False
        return self._compiled_pattern.search(entry.message) is not None


class CrashCondition(Condition):
    """Matches Android application crashes (FATAL EXCEPTION)."""

    def __init__(
        self,
        tag: str = "AndroidRuntime",
        level: str = "E",
        message_pattern: str = "FATAL EXCEPTION",
    ) -> None:
        """Initialize crash detection parameters."""
        self.tag = tag
        self.level = level
        self.message_pattern = message_pattern

    def check(self, entry: LogEntry) -> bool:
        """Return True if the entry matches a crash pattern.

        Returns:
            Whether a crash was detected.

        """
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
        """Initialize ANR detection parameters."""
        self.tag = tag
        self.level = level
        self.message_prefix = message_prefix

    def check(self, entry: LogEntry) -> bool:
        """Return True if the entry matches an ANR pattern.

        Returns:
            Whether an ANR was detected.

        """
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

    This filter allows you to specify multiple criteria
    (package, tag, level, message content).
    A log entry must match ALL specified criteria to pass
    the filter. If a criterion accepts
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
        level: LogLevel | list[LogLevel] | None = None,
        message_contains: str | list[str] | None = None,
    ) -> None:
        """Initialize the filter.

        An empty collection (e.g. ``tag=[]``) is treated the same as ``None``:
        it imposes no constraint on that field, rather than rejecting every
        entry. A filter whose criteria are all ``None`` or empty passes every
        entry.

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
        self._compiled_message_pattern = (
            _compile_contains_regex(tuple(self.message_patterns))
            if self.message_patterns is not None
            else None
        )

    @staticmethod
    def _to_set(value: str | Iterable[str] | None) -> set[str] | None:
        # An empty collection means "no constraint", identical to None.
        if value is None:
            return None
        if isinstance(value, str):
            return {value}
        result = set(value)
        return result or None

    @staticmethod
    def _to_list(value: str | Iterable[str] | None) -> list[str] | None:
        # An empty collection means "no constraint", identical to None.
        if value is None:
            return None
        if isinstance(value, str):
            return [value]
        result = list(value)
        return result or None

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
            if self._compiled_message_pattern is None:
                return False
            if self._compiled_message_pattern.search(entry.message) is None:
                return False

        return True
