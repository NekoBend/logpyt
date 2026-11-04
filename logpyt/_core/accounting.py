"""Counters, the adb stderr log limit and pausable timers of one stream run.

Pure helpers of `logpyt._core.machine` (design 2.5, 4.4, 5.9, 5.10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from logpyt._core.protocol import ADB_LOGGER, Diagnostic, OutboxCounts
from logpyt.streams._events import StreamStats

__all__ = ["STDERR_LINES_PER_SECOND", "Pausable", "StderrLog", "Tally"]

# Design 5.9: adb stderr lines logged per second; the rest are counted.
STDERR_LINES_PER_SECOND = 10


@dataclass(slots=True)
class Tally:
    """The counters the core keeps itself; `snapshot` adds the rest.

    ``dropped`` counts the lines the core discarded (kill, a failed run, a
    detached consumer); the outbox's own discards come in its `OutboxCounts`.
    """

    reconnects: int = 0
    stall_resumes: int = 0
    lines: int = 0
    stderr_lines: int = 0
    filtered: int = 0
    deduplicated: int = 0
    below_cursor: int = 0
    before_start: int = 0
    dropped: int = 0
    unparsed: int = 0
    truncated: int = 0
    unattributed: int = 0
    gaps: int = 0
    flush_timeout: bool = False
    flush_failed: bool = False

    def accounted(self, admitted: int, held: int) -> int:
        """Lines whose fate is known: admitted to the outbox, held, or counted."""
        return (
            admitted
            + held
            + self.dropped
            + self.filtered
            + self.before_start
            + self.deduplicated
            + self.below_cursor
        )

    def snapshot(
        self,
        *,
        connections: int,
        counts: OutboxCounts,
        backpressure_seconds: float,
        killed: bool,
        failed: bool,
    ) -> StreamStats:
        """The `StreamStats` of the run, with the outbox's latest counts."""
        return StreamStats(
            connections=connections,
            reconnects=self.reconnects,
            stall_resumes=self.stall_resumes,
            backpressure_seconds=backpressure_seconds,
            lines=self.lines,
            stderr_lines=self.stderr_lines,
            delivered=counts.taken_entries,
            filtered=self.filtered,
            deduplicated=self.deduplicated,
            below_cursor=self.below_cursor,
            before_start=self.before_start,
            undelivered=self.dropped + counts.discarded_entries,
            unparsed=self.unparsed,
            truncated=self.truncated,
            unattributed=self.unattributed,
            gaps=self.gaps,
            events_coalesced=counts.events_coalesced,
            flush_timeout=self.flush_timeout,
            flush_failed=self.flush_failed,
            killed=killed,
            failed=failed,
        )


class StderrLog:
    """adb stderr lines as WARNING diagnostics, at most 10 per second (design 5.9).

    A window opens at the first line logged; lines beyond the limit inside it
    are counted, and the count is reported when the next window opens or at
    the end of the run (`pending`).
    """

    __slots__ = ("_logged", "_suppressed", "_window")

    def __init__(self) -> None:
        self._window: float | None = None
        self._logged = 0
        self._suppressed = 0

    def line(self, text: str, now: float) -> list[Diagnostic]:
        """The diagnostics for one stderr line read at ``now``."""
        out: list[Diagnostic] = []
        window = self._window
        if window is None or now >= window + 1.0:
            out += self.pending()
            self._window = now
            self._logged = 0
        if self._logged < STDERR_LINES_PER_SECOND:
            self._logged += 1
            out.append(Diagnostic(logging.WARNING, text, ADB_LOGGER))
        else:
            self._suppressed += 1
        return out

    def pending(self) -> list[Diagnostic]:
        """The report of the lines not logged since the last one, if any."""
        count, self._suppressed = self._suppressed, 0
        if not count:
            return []
        message = f"{count} more adb stderr lines were not logged (rate limit)"
        return [Diagnostic(logging.WARNING, message, ADB_LOGGER)]


class Pausable:
    """A timer that counts only while intake is open (exit_drain, the flush timer).

    Args:
        duration: Seconds of open intake until it fires.
        now: The monotonic time it is armed.
        running: Whether intake is open at ``now``.

    """

    __slots__ = ("_left", "_since")

    def __init__(self, duration: float, now: float, *, running: bool) -> None:
        self._left = duration
        self._since: float | None = now if running else None

    def pause(self, now: float) -> None:
        """Intake closed at ``now``: stop counting."""
        if self._since is not None:
            self._left -= now - self._since
            self._since = None

    def resume(self, now: float) -> None:
        """Intake opened at ``now``: count again."""
        if self._since is None:
            self._since = now

    def deadline(self) -> float | None:
        """When it fires; None while paused."""
        return None if self._since is None else self._since + self._left
