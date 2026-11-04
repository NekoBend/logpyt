"""Stream lifecycle states, events and statistics of logpyt 3.0 (design section 2.5).

Nothing emits these yet. Until the driver rewrites switch over (design section 8.1),
the 2.x streams keep reporting `logpyt.streams.common.StreamState`.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from logpyt.exceptions import StreamError

__all__ = [
    "Gap",
    "GapReason",
    "IncompleteReason",
    "StateChange",
    "StreamEvent",
    "StreamState",
    "StreamStats",
]


class StreamState(StrEnum):
    """Lifecycle state of a stream run (design section 3.1).

    ``STOPPED``, ``KILLED`` and ``FAILED`` are terminal. Each value equals its
    name, so ``str(state)`` and log messages read like the documentation.
    """

    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    KILLED = "KILLED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class StateChange:
    """The stream moved from one state to another.

    Attributes:
        old: The state before the change.
        new: The state after it.
        connection: 1-based number of the adb connection the change belongs to.
        error: The cause of a change to ``RECONNECTING`` (None for a stall
            resume), or the terminal outcome on a change to ``FAILED``; None
            otherwise. It has no default, so every producer states it.
    """

    old: StreamState
    new: StreamState
    connection: int
    error: StreamError | None


type GapReason = Literal["ring_rotated", "clock_moved_back", "device_reported"]


@dataclass(frozen=True, slots=True)
class Gap:
    """The device lost lines that logpyt cannot recover (design section 5.6).

    Attributes:
        after: Device timestamp of the last line read before the loss, if known.
        before: Device timestamp of the first line read after it, if known.
        lost: Number of lost lines when logd reported it; None when unknown.
        reason: How the loss was detected.
    """

    after: datetime | None
    before: datetime | None
    lost: int | None
    reason: GapReason


# What `on_event` receives, in emission order and interleaved with the items.
type StreamEvent = StateChange | Gap

type IncompleteReason = Literal[
    "gap",
    "device_lost",
    "unattributed",
    "undelivered",
    "truncated",
    "flush_timeout",
    "flush_failed",
    "killed",
    "failed",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamStats:
    """Immutable snapshot of a stream run's counters (design sections 2.5 and 4.4).

    Every field defaults to zero (or False), so ``StreamStats()`` is the snapshot
    of a run that has not started. Fields are keyword-only because most of them
    are counters of the same type.

    Attributes:
        connections: adb logcat connections started.
        reconnects: Reconnects after a connection ended (section 5.6).
        stall_resumes: Deliberate disconnects of a stalled reader (section 5.6).
        backpressure_seconds: Total time intake was closed for a full outbox.
        lines: Framed stdout lines.
        stderr_lines: Framed stderr lines.
        delivered: Items handed to the consumer.
        filtered: Lines the ``where`` condition rejected, the flush marker included.
        deduplicated: Resume replays recognised in the resume window.
        below_cursor: Resume lines older than the ``-T`` request (section 5.6).
        before_start: With ``since="now"``: lines up to and including the start
            marker.
        undelivered: Items discarded by ``kill()``, a detached consumer or a
            failed consumer.
        unparsed: Lines the parser could not split into fields.
        truncated: Lines cut at ``max_line_bytes``.
        unattributed: Lines evaluated with an unknown package.
        gaps: `Gap` events.
        device_lost: Lines the device reported as lost.
        backlog_pruned: Lines logd reported pruned before the capture started.
        overlap_shortened: Resumes whose overlap the window caps cut short.
        events_coalesced: Pending state changes merged into the latest one
            because too many events waited (section 5.10).
        flush_timeout: ``stop(flush=...)`` did not read its marker in time.
        flush_failed: ``stop(flush=...)`` could not finish: the marker command
            failed, the connection ended before the marker, or the reader stalled.
        killed: The run ended ``KILLED``.
        failed: The run ended ``FAILED``.
    """

    connections: int = 0
    reconnects: int = 0
    stall_resumes: int = 0
    backpressure_seconds: float = 0.0
    lines: int = 0
    stderr_lines: int = 0
    delivered: int = 0
    filtered: int = 0
    deduplicated: int = 0
    below_cursor: int = 0
    before_start: int = 0
    undelivered: int = 0
    unparsed: int = 0
    truncated: int = 0
    unattributed: int = 0
    gaps: int = 0
    device_lost: int = 0
    backlog_pruned: int = 0
    overlap_shortened: int = 0
    events_coalesced: int = 0
    flush_timeout: bool = False
    flush_failed: bool = False
    killed: bool = False
    failed: bool = False

    @property
    def incomplete(self) -> tuple[IncompleteReason, ...]:
        """Every reason the capture may miss device lines; empty means complete.

        The order is that of design section 4.4. A snapshot taken before the
        run ended shows only the reasons found so far. Reconnects, stall
        resumes, ``below_cursor``, ``backlog_pruned`` and ``overlap_shortened``
        are not reasons, because none of them proves a loss.
        """
        checks: tuple[tuple[IncompleteReason, bool], ...] = (
            ("gap", self.gaps > 0),
            ("device_lost", self.device_lost > 0),
            ("unattributed", self.unattributed > 0),
            ("undelivered", self.undelivered > 0),
            ("truncated", self.truncated > 0),
            ("flush_timeout", self.flush_timeout),
            ("flush_failed", self.flush_failed),
            ("killed", self.killed),
            ("failed", self.failed),
        )
        return tuple(reason for reason, found in checks if found)

    @property
    def complete(self) -> bool:
        """Whether no `incomplete` reason applies; False for a failed run."""
        return not self.incomplete
