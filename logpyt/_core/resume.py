"""Where each connection starts reading (design 5.6): the PR-10 minimal policy.

The stream core asks this interface at the points of the transition table
(design 3.2): the device clock read of every attempt, the ``-T`` position of
every spawn, and each stdout line before ``where``. PR-14 replaces the policy
behind it with the resume planner (the read cursor and the occurrence window,
dedupe and ``below_cursor``, the clock check and re-anchor, gap detection, the
chatty-notice rules); the core's side of the interface stays.

What this minimal policy does:

- ``since="backlog"``: no ``-T``; ``since=<aware datetime>``: ``-T`` at it.
- ``since="now"``: the core writes the start marker after the clock read and
  spawns at ``-T <clock - 1 s>`` (the marker handling is the core's); every
  later connection starts there too. The core passes ``resume=True`` from the
  moment the marker is read, the marker itself counting as a line read, so a
  connection that read only the marker does not move the start to the next
  attempt's clock.
- A reconnect starts at the first connection's position, so it replays what
  the earlier connections read (no dedupe yet; with ``since="now"`` the lines
  before the start marker and the marker itself come back as entries), and
  the clock read is only recorded: no ``Gap`` is detected. Every stall resume
  is such a reconnect, and it is on by default (``stall_timeout=1.0``), so
  until PR-14 a stall resume delivers the replayed lines again.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from logpyt._core.argv import epoch_position

if TYPE_CHECKING:
    from logpyt._core.argv import Position
    from logpyt.models import LogEntry
    from logpyt.streams._events import Gap

__all__ = ["Positioner", "Verdict"]

# What the resume screen decides for a line (PR-14 adds the drops).
type Verdict = Literal["new", "deduplicated", "below_cursor"]


class Positioner:
    """The start and resume positions of one run (see the module docs).

    Args:
        since: ``StreamConfig.since``.

    """

    __slots__ = ("_clock", "_since")

    def __init__(self, since: Literal["backlog", "now"] | datetime) -> None:
        self._since = since
        self._clock: int | None = None

    @property
    def start_marker(self) -> bool:
        """Whether the start is placed by a start marker (``since="now"``)."""
        return self._since == "now"

    def clock(self, seconds: int, *, resume: bool) -> list[Gap]:
        """Record the device clock of an attempt; return the gaps it shows.

        Args:
            seconds: The device time in whole epoch seconds.
            resume: Whether an earlier connection read lines already: with
                ``since="now"`` the start marker or a line after it.

        """
        if not resume or self._clock is None:
            self._clock = seconds
        return []

    def position(self, *, resume: bool) -> Position | None:
        """The ``-T`` position of the attempt whose clock was read last."""
        del resume  # PR-14: the resume request
        since = self._since
        if isinstance(since, datetime):
            return epoch_position(since)
        if since == "now" and self._clock is not None:
            return (self._clock - 1, 0)
        return None

    def screen(self, entry: LogEntry) -> Verdict:  # noqa: PLR6301  (PR-14 interface)
        """Decide whether a stdout line is new or a replay (always new here)."""
        del entry
        return "new"

    def observe(self, entry: LogEntry) -> None:
        """Track a stdout line for the next resume (nothing to track here)."""
