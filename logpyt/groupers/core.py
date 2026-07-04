"""Log grouping functionality."""

from __future__ import annotations

import heapq
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from ..models import LogEntry

# Type definitions
EmitMode = Literal["entry", "group"]


class LogGrouper:
    """Groups consecutive log entries based on fields and time threshold.

    This class is useful for reconstructing fragmented logs or grouping related logs
    that occur close together in time.

    Boundary semantics:
        ``threshold_ms`` bounds only the gap between an entry and the immediately
        preceding entry in the same group; it is applied inclusively
        (``abs(time_diff) <= threshold_ms``), so an entry arriving exactly
        ``threshold_ms`` after the previous one stays in the same group. It does
        NOT bound the total time span of a group: a long run of entries each
        within ``threshold_ms`` of its predecessor forms one group whose overall
        span may greatly exceed ``threshold_ms`` (i.e. the window "drifts"
        forward with each entry). This is intentional; groups are defined by
        inter-entry gaps, not by an absolute maximum duration.

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
        max_group_size: int | None = 10000,
    ) -> None:
        """Initialize the LogGrouper.

        Args:
            by: Sequence of field names to use as grouping keys. Each must name a
                hashable scalar LogEntry field (e.g. "pid", "tid", "tag", "level");
                do not use "meta" (a dict), which is unhashable and cannot be a key.
            threshold_ms: Time threshold in milliseconds.
            emit_mode: Emission mode ("entry" or "group").
            max_group_size: Hard cap on entries per group. When a group reaches
                this many entries it is force-flushed and a new group started,
                bounding memory under a hot (possibly adversarial) key. Set to
                None to disable the cap. Defaults to 10000.
        """
        self.by = tuple(by)
        self.threshold_ms = threshold_ms
        self.emit_mode = emit_mode
        self.max_group_size = max_group_size
        self._buffer: list[LogEntry] = []
        self._last_key: tuple[Any, ...] | None = None
        # Canonicalize frequently repeated keys to avoid repeated hash work
        # in downstream dict/OrderedDict operations.
        self._key_cache: OrderedDict[tuple[Any, ...], tuple[Any, ...]] = OrderedDict()
        self._key_cache_max = 256

    def _get_key(self, entry: LogEntry) -> tuple[Any, ...]:
        """Extract grouping key from a log entry.

        Args:
            entry: The log entry to extract the key from.

        Returns:
            A tuple of values corresponding to the 'by' fields.
        """
        raw_key = tuple(getattr(entry, field, None) for field in self.by)
        cached_key = self._key_cache.get(raw_key)
        if cached_key is not None:
            self._key_cache.move_to_end(raw_key)
            return cached_key

        self._key_cache[raw_key] = raw_key
        if len(self._key_cache) > self._key_cache_max:
            self._key_cache.popitem(last=False)
        return raw_key

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
        keys_match = current_key is self._last_key or current_key == self._last_key

        # 2. Time difference must be within threshold
        last_entry = self._buffer[-1]
        time_diff = (entry.timestamp - last_entry.timestamp).total_seconds() * 1000
        # We assume logs are mostly ordered, but take abs just in case of slight jitter,
        # though strictly speaking "consecutive" usually implies forward flow.
        # If the new entry is OLDER than the last one by more than threshold,
        # it definitely breaks.
        # If it is NEWER by more than threshold, it breaks.
        within_threshold = abs(time_diff) <= self.threshold_ms

        if keys_match and within_threshold:
            self._buffer.append(entry)
            if (
                self.max_group_size is not None
                and len(self._buffer) >= self.max_group_size
            ):
                # Cap group size so a hot key cannot grow one group without bound.
                emitted.extend(self.flush())
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

    Boundary semantics (differs subtly from ``LogGrouper``):
        A window for a key is closed lazily via an expiry heap. Each entry sets
        that key's expiry to ``entry_ts + threshold_ms``. A window is flushed
        only when a later entry arrives with ``current_ts > expiry`` (a STRICT
        comparison). Consequently a same-key entry arriving *exactly*
        ``threshold_ms`` after the previous one (``current_ts == expiry``) does
        NOT close the window; it extends the same group. This matches
        ``LogGrouper``'s inclusive ``abs(diff) <= threshold_ms`` rule at the
        exact boundary, and both close the group once the gap strictly exceeds
        ``threshold_ms``. As with ``LogGrouper``, ``threshold_ms`` bounds only
        the inter-entry gap, not a group's total span, so a busy window drifts
        forward as long as entries keep arriving within threshold.

    Eviction:
        At most ``max_groups`` windows are kept open concurrently. When a new,
        distinct key would exceed ``max_groups``, the least-recently-updated
        (LRU) window is flushed and emitted to make room, regardless of whether
        its threshold has elapsed. Recency is tracked by insertion/update order
        in the underlying ``OrderedDict`` of buffers.
    """

    def __init__(
        self,
        by: Sequence[str],
        threshold_ms: float,
        emit_mode: EmitMode = "group",
        max_groups: int = 1000,
        max_group_size: int | None = 10000,
    ) -> None:
        """Initialize the WindowedLogGrouper.

        Args:
            by: Sequence of field names to use as grouping keys.
            threshold_ms: Time threshold in milliseconds.
            emit_mode: Emission mode ("entry" or "group").
            max_groups: Maximum number of active groups to maintain.
            max_group_size: Hard cap on entries per window; a window reaching it
                is force-flushed, bounding memory under a hot key. None disables
                the cap. Defaults to 10000.
        """
        super().__init__(by, threshold_ms, emit_mode, max_group_size)
        self._buffers: OrderedDict[tuple[Any, ...], list[LogEntry]] = OrderedDict()
        self._heap: list[tuple[float, tuple[Any, ...]]] = []
        self._expiries: dict[tuple[Any, ...], float] = {}
        self.max_groups = max_groups
        self._anchor: datetime | None = None

    def _relative_ms(self, ts: datetime) -> float:
        """Milliseconds from the first seen timestamp (DST-safe naive delta).

        Uses naive datetime subtraction (matching ``LogGrouper``) rather than
        ``datetime.timestamp()`` so a local DST offset discontinuity cannot
        distort window expiry comparisons.
        """
        if self._anchor is None:
            self._anchor = ts
        return (ts - self._anchor).total_seconds() * 1000.0

    def _maybe_compact_heap(self) -> None:
        """Compact stale heap entries when lazy invalidation grows too much."""
        if len(self._heap) <= self.max_groups:
            return
        if len(self._heap) <= (len(self._expiries) * 2):
            return

        compacted = [
            (expiry, key)
            for key, expiry in self._expiries.items()
            if key in self._buffers
        ]
        heapq.heapify(compacted)
        self._heap = compacted

    def process(self, entry: LogEntry) -> list[LogEntry | list[LogEntry]]:
        """Process a new log entry and update groups.

        Args:
            entry: The new log entry to process.

        Returns:
            A list of emitted items.
        """
        current_key = self._get_key(entry)
        emitted: list[LogEntry | list[LogEntry]] = []
        current_ts = self._relative_ms(entry.timestamp)

        # Check for timeouts using heap
        while self._heap:
            expiry, key = self._heap[0]
            if expiry >= current_ts:
                break

            heapq.heappop(self._heap)

            current_expiry = self._expiries.get(key)
            if current_expiry is None:
                continue

            # Skip stale heap entries (older expiries for the same key).
            if expiry != current_expiry:
                continue

            if current_expiry < current_ts:
                emitted.extend(self._flush_key(key))

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

        # Update expiry for the key (lazy heap invalidation via _expiries map).
        new_expiry = current_ts + self.threshold_ms
        self._expiries[current_key] = new_expiry
        heapq.heappush(self._heap, (new_expiry, current_key))
        self._maybe_compact_heap()

        # Cap per-window size so a hot (possibly adversarial) key cannot grow one
        # window without bound. _flush_key drops the buffer + its expiry; the
        # stale heap entry is cleared lazily.
        if (
            self.max_group_size is not None
            and current_key in self._buffers
            and len(self._buffers[current_key]) >= self.max_group_size
        ):
            emitted.extend(self._flush_key(current_key))

        return emitted

    def _flush_key(self, key: tuple[Any, ...]) -> list[LogEntry | list[LogEntry]]:
        """Flush a specific buffer by key."""
        if key not in self._buffers:
            return []

        buffer = self._buffers.pop(key)
        self._expiries.pop(key, None)
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
        self._expiries.clear()
        return emitted
