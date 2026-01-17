"""Log grouping functionality."""

from __future__ import annotations

import heapq
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any, Literal

from ..models import LogEntry

# Type definitions
EmitMode = Literal["entry", "group"]


class LogGrouper:
    """Groups consecutive log entries based on fields and time threshold.

    This class is useful for reconstructing fragmented logs or grouping related logs
    that occur close together in time.

    Attributes:
        by: Fields to group by (e.g., ("pid", "tid")).
        threshold_ms: Maximum time difference in milliseconds between
            consecutive logs to be considered part of the same group.
        emit_mode: How to emit the grouped logs.
            - "entry": Emit entries individually (flattened list).
            - "group": Emit the whole group as a list of entries.

    Examples:
        Group logs by Thread ID (tid) if they occur within 10ms of each other:
        >>> grouper = LogGrouper(by=["tid"], threshold_ms=10.0)
        >>> grouped_logs = grouper.process(entry)

        Group logs by Process ID (pid) and Tag:
        >>> grouper = LogGrouper(by=["pid", "tag"], threshold_ms=50.0)
    """

    def __init__(
        self,
        by: Sequence[str],
        threshold_ms: float,
        emit_mode: EmitMode = "entry",
    ) -> None:
        """Initialize the LogGrouper.

        Args:
            by: Sequence of field names to use as grouping keys.
            threshold_ms: Time threshold in milliseconds.
            emit_mode: Emission mode ("entry" or "group").
        """
        self.by = tuple(by)
        self.threshold_ms = threshold_ms
        self.emit_mode = emit_mode
        self._buffer: list[LogEntry] = []
        self._last_key: tuple[Any, ...] | None = None

    def _get_key(self, entry: LogEntry) -> tuple[Any, ...]:
        """Extract grouping key from a log entry.

        Args:
            entry: The log entry to extract the key from.

        Returns:
            A tuple of values corresponding to the 'by' fields.
        """
        return tuple(getattr(entry, field, None) for field in self.by)

    def process(self, entry: LogEntry) -> list[LogEntry | list[LogEntry]]:
        """Process a new log entry and update groups.

        Args:
            entry: The new log entry to process.

        Returns:
            A list of emitted items. Depending on emit_mode, this will be
            a list of LogEntry objects or a list containing a list of LogEntry objects.
        """
        current_key = self._get_key(entry)
        emitted: list[LogEntry | list[LogEntry]] = []

        if not self._buffer:
            self._buffer.append(entry)
            self._last_key = current_key
            return emitted

        # Check if entry belongs to the current group
        # 1. Key must match
        keys_match = current_key == self._last_key

        # 2. Time difference must be within threshold
        last_entry = self._buffer[-1]
        time_diff = (entry.timestamp - last_entry.timestamp).total_seconds() * 1000
        # We assume logs are mostly ordered, but take abs just in case of slight jitter,
        # though strictly speaking "consecutive" usually implies forward flow.
        # If the new entry is OLDER than the last one by more than threshold, it definitely breaks.
        # If it is NEWER by more than threshold, it breaks.
        within_threshold = abs(time_diff) <= self.threshold_ms

        if keys_match and within_threshold:
            self._buffer.append(entry)
        else:
            # Flush current group
            emitted.extend(self.flush())
            # Start new group
            self._buffer.append(entry)
            self._last_key = current_key

        return emitted

    def flush(self) -> list[LogEntry | list[LogEntry]]:
        """Force emit any buffered groups.

        Returns:
            A list of emitted items.
        """
        if not self._buffer:
            return []

        result: list[LogEntry | list[LogEntry]]
        if self.emit_mode == "group":
            # Create a copy of the list to emit
            result = [list(self._buffer)]
        else:
            # "entry" mode: emit individual entries
            result = list(self._buffer)

        self._buffer.clear()
        self._last_key = None
        return result


class WindowedLogGrouper(LogGrouper):
    """Groups logs by key, maintaining multiple active windows.

    Unlike LogGrouper which only tracks the last seen key, this grouper
    maintains separate buffers for each unique key combination. A group is
    flushed when a new log for that specific key arrives after the threshold
    has passed, or when flush() is called.

    This is useful for interleaved logs where multiple processes/threads are
    logging simultaneously.
    """

    def __init__(
        self,
        by: Sequence[str],
        threshold_ms: float,
        emit_mode: EmitMode = "group",
        max_groups: int = 1000,
    ) -> None:
        """Initialize the WindowedLogGrouper.

        Args:
            by: Sequence of field names to use as grouping keys.
            threshold_ms: Time threshold in milliseconds.
            emit_mode: Emission mode ("entry" or "group").
            max_groups: Maximum number of active groups to maintain.
        """
        super().__init__(by, threshold_ms, emit_mode)
        self._buffers: OrderedDict[tuple[Any, ...], list[LogEntry]] = OrderedDict()
        self._heap: list[tuple[float, tuple[Any, ...]]] = []
        self.max_groups = max_groups

    def process(self, entry: LogEntry) -> list[LogEntry | list[LogEntry]]:
        """Process a new log entry and update groups.

        Args:
            entry: The new log entry to process.

        Returns:
            A list of emitted items.
        """
        current_key = self._get_key(entry)
        emitted: list[LogEntry | list[LogEntry]] = []
        current_ts = entry.timestamp.timestamp() * 1000.0

        # Check for timeouts using heap
        while self._heap:
            expiry, key = self._heap[0]
            if expiry >= current_ts:
                break

            heapq.heappop(self._heap)

            if key not in self._buffers:
                continue

            buffer = self._buffers[key]
            last_entry_ts = buffer[-1].timestamp.timestamp() * 1000.0
            real_expiry = last_entry_ts + self.threshold_ms

            if real_expiry < current_ts:
                emitted.extend(self._flush_key(key))
            else:
                # Re-push with updated expiry
                heapq.heappush(self._heap, (real_expiry, key))

        # Add the current entry to its corresponding buffer
        if current_key in self._buffers:
            self._buffers.move_to_end(current_key)
            self._buffers[current_key].append(entry)
        else:
            # Enforce max groups limit
            if len(self._buffers) >= self.max_groups:
                # Flush the least recently used group
                lru_key = next(iter(self._buffers))
                emitted.extend(self._flush_key(lru_key))

            self._buffers[current_key] = [entry]

            # Update heap with new expiry for this key
            new_expiry = current_ts + self.threshold_ms
            heapq.heappush(self._heap, (new_expiry, current_key))

        return emitted

    def _flush_key(self, key: tuple[Any, ...]) -> list[LogEntry | list[LogEntry]]:
        """Flush a specific buffer by key."""
        if key not in self._buffers:
            return []

        buffer = self._buffers.pop(key)
        if not buffer:
            return []

        if self.emit_mode == "group":
            return [buffer]
        else:
            # Explicitly type the result to satisfy invariance
            result: list[LogEntry | list[LogEntry]] = list(buffer)
            return result

    def flush(self) -> list[LogEntry | list[LogEntry]]:
        """Force emit all buffered groups.

        Returns:
            A list of emitted items.
        """
        emitted: list[LogEntry | list[LogEntry]] = []
        # Create a list of keys to avoid runtime error during iteration
        keys = list(self._buffers.keys())
        for key in keys:
            emitted.extend(self._flush_key(key))
        self._heap.clear()
        return emitted
