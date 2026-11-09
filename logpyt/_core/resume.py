"""Where each connection starts reading, and what a resumed one replays (design 5.6).

`ResumePlanner` is the resume planner of one stream run. It is pure: the core
(`logpyt._core.machine`) calls it at the points of the transition table
(design 3.2) and passes every time it needs, the host-monotonic seconds of
its ``handle()`` call; the planner reads no clock and does no I/O.

The core's side of the interface:

- `ResumePlanner.clock` at the device clock read of every attempt (it may
  return a ``clock_moved_back`` `Gap`), then `ResumePlanner.position` for
  the ``-T`` of the attempt's spawn, which also opens the connection's screen
  phase;
- with ``since="now"``, until the start marker is read: every stdout line
  goes to `ResumePlanner.before_start`, the marker to `ResumePlanner.marker`;
- every other stdout line (dividers excepted) to `ResumePlanner.screen`, which
  says whether the line is new or dropped as a replay (``deduplicated``) or
  below the request (``below_cursor``);
- after each call, `ResumePlanner.out` holds the `Gap` events and the INFO
  messages to emit, in order; the core takes them with `ResumePlanner.take`.

The design's terms, in integer epoch microseconds:

- the start position ``S`` (``since="backlog"``: the first clock read;
  ``since=<datetime>``: that time; ``since="now"``: the start marker's
  timestamp) classifies chatty notices;
- the cursor ``hw`` is the highest device timestamp read from stdout
  (filtered and unparsed lines included). An unparsed line inherits the
  timestamp of the preceding parsed line of its connection, and a line with no
  timestamp (an unparsed line before any parsed one) is neither windowed nor
  screened;
- the window ``W`` holds ``(timestamp, raw)`` of every line read with a
  timestamp ``>= hw - resume_overlap``: a deque of the lines read at or above
  the ``hw`` of their time (so its timestamps never decrease) and a min-heap of
  the lines read below it. Lines older than the overlap leave both at once,
  and the caps (``resume_window_lines``, ``resume_window_bytes``) evict the
  smallest timestamps first (design 5.6), remembering the highest evicted
  one. Every line read with a timestamp ``>= max(hw - resume_overlap, that +
  1 us)`` is therefore in the window, so the dedupe is exact. The caps never
  evict a parsed line stamped ``hw``: the request stays at or below ``hw``, so
  the resume can check the ring, and the window may exceed a cap by the lines of
  that one microsecond. Unparsed lines that inherit ``hw`` do not exceed a cap,
  but for one alone at ``hw``: over it they are not windowed, and their replay
  can be delivered again. A cap
  eviction always takes a line inside the overlap; the first one of a connection
  gives one INFO, and a resume whose request it moves counts in
  ``overlap_shortened``. The window is kept only when ``reconnect`` or
  ``stall_timeout`` is set: without either a run never resumes.

A resume (design 5.6 steps 1-3) requests
``T_req = max(hw - resume_overlap, max_evicted + 1 us, start)`` with ``start``
the first connection's own ``-T`` (``since=<datetime>`` or the start
marker's ``-T``; nothing for ``"backlog"``), so a replay never returns lines
from before the capture's start as new. A re-anchor lowers ``start`` to its
own request: the clock moved back, and the new times lie below the old start.
The connection's screen phase then drops a line below ``T_req``
(``below_cursor``), a line that matches an element of the window
(``deduplicated``, by occurrence count, so equal lines at one timestamp are
told apart; an unparsed line before any parsed one has no time and is
matched by content alone), and a chatty notice at or below ``hw``
(``deduplicated``, and proof that logd still holds the position). It ends at
the first line above ``hw``, not at the last match (design A.25): a line read
before and stored late below ``T_req``, which the replay returns after the
window's last element, is ``below_cursor`` and not delivered again. A line at
``hw`` neither replayed nor covered by such a notice then gives
``Gap(ring_rotated)``. Once the replay of the lines read is over (every line
at ``hw`` replayed or covered, or every element), a line below ``hw -
max(resume_overlap, 1 s)`` is a clock step seen live (below), not
``below_cursor``: so after its replay a phase that sees no line above ``hw``
drops as ``below_cursor`` only lines less than that bound below ``hw``. A
replay that never finishes is the exception (see the limits below). A
connection that ends inside its phase leaves the window as it was, so the
next resume screens the same way. With ``since=<datetime>`` the first
connection drops lines below the start until the first one at or above it
(``below_cursor``): the device can return lines below ``-T`` (design A.7).

The clock check of every resume compares the attempt's clock ``N`` (whole
seconds: ``adb shell date +%s``) with ``hw``. The clock moved back when
``N < hw - max(resume_overlap, 1 s)``: with whole seconds a clock that did
not move can read up to 1 s below ``hw``, so the bound is never below the
clock's resolution (the same as design 5.6's ``N < hw - resume_overlap`` for
every ``resume_overlap >= 1``). The resume then emits
``Gap(clock_moved_back)``, once per step: a repeated attempt at the same ``hw``
emits it again only when its clock read shows another step since the attempt
before. It requests ``N - outage - resume_overlap`` (the outage measured
host-monotonically from the read of the line at ``hw``, so an attempt that only
replayed old lines does not shorten it; never below 0, like every request: a
negative ``-T`` is invalid), and screens the replay against the old window (no
ring check: the Gap marks the point) for the whole connection; its
``below_cursor`` rule ends once the lines at ``hw`` were replayed, or at the
first new line logged after the clock read (from ``N + 1 s`` on). A line below
the request after them is one the new clock stamped before it, which was never
read: the outage counts from the line at ``hw``, so a reader that lagged behind
the step requests too little. From then on, until the device clock passes the
old ``hw``, a line stamped above every time the device clock can show
(``N + 1 s`` plus the host time since the clock read with room for drift, plus
1 s of slack) that matches no window was logged before the step, or the clock
has moved forward since (below). Up to the highest time read before the step,
while the replay has not passed the lines read before (their last ones, or a
line above them), it was read before: logd returns it on every request above
the new times, so it is dropped (``deduplicated``). Past them, it was logged
between the last read and the step, or across a second step: it is delivered
once, and later replays match it like the old windows' lines. Neither kind
moves ``hw`` or the window: they restart at the first new line the device
clock can have stamped. With ``since="backlog"``, a line below every line read
before the step was never read, so it goes on by the usual rules whatever its
time (with a start position the ring also holds older lines that are not the
capture's: those are dropped by the rule above). Without these rules
every later reconnect would deliver the old lines again and re-anchor once
more. Once a re-anchored phase's new lines began, a line below the new ``hw -
max(resume_overlap, 1 s)`` is a step seen live. The highest time the caps
evicted from the old window bounds later requests too, once the new ``hw`` lies
above it (a step whose clock did not move, such as a line stored late, keeps
the old times).

A forward step after a step back (design A.25; 5.6's device with a wrong clock
that then syncs): a line above every time the clock can show, on a connection
that crossed the step back in its own lines or that read a line of the new
clock over 1 s after its clock read, goes on like any line: it moves ``hw`` and
enters the window, and the estimate of the clock restarts from it (at or above
the old ``hw``, so does what a clock read that passes it ends). The 1 s is for a
ring in time order, which returns old lines right after the new ones; such a
replay read more slowly than that delivers again the old lines that left the
window (design 5.6's exception). Earlier than that, or ahead of any new line,
such a line goes on as a line logged before the step: delivered once, not moving
``hw``, and matched by later replays. The limit: when the ring lost the lines
read before, such a line stamped within the times read before the step is dropped
as an old one until the 1 s is over and a new line came.

A step seen live (design A.25): a parsed line that the screen does not drop,
read outside a replay of the lines read before (no phase, or its replay is
over) and stamped below ``hw - max(resume_overlap, 1 s)``, is a clock step
back: ``Gap(clock_moved_back, after=hw, before=<its time>)`` at once, the
line is delivered, and the stream re-anchors there by the rules above: the
window is kept as the old window, the old ``hw`` is the ceiling, ``S`` and
the start floor drop to that line's time minus the overlap, and ``hw`` and
the window restart at the line. Without it, a step during a connection
followed by an outage that ends after the clock caught up would replay the
lines read since the step with no Gap (the resume's clock check sees no
step). A line stored that late is taken for a step too: the device's
out-of-order lines recorded so far were about 86 ms late.

A step from before the capture (user decision, design A.25): the ring can
still hold a backward step that happened before the first clock read, and a
backlog or the first lines of a run read across it. Nothing is lost when one
connection reads across it, so when the line at ``hw`` was logged before that
read (stamped below ``N0 + 1 s``, or above every time that clock could show when
the line was read, with 1 s of slack and 200 ppm of the time since for drift),
the stream re-anchors the same way with no Gap, and ``S`` stays. With no Gap to
mark the point, such a resume checks the ring: if the lines at ``hw`` are gone
at the first line that goes on, ``Gap(ring_rotated)`` reports the hole. A clock
read below ``hw - max(resume_overlap, 1 s)`` that also shows the clock moved back
since ``N0`` is always reported. A chatty notice above every time that clock
could show, with no step reported, is backlog too. Costs: a step during the
capture goes unreported when no line stamped from ``N0 + 1 s`` on was read
before it (its first second, or a device silent since the start) and the next
clock read does not show it; a device clock stepped forward during the capture,
drift above 200 ppm, or a host whose monotonic clock stopped while the device
ran (from memory: Linux suspend) make a later step look old in the same way, and
a real notice look like backlog. A step of a few seconds before the capture
can fall outside both tests (``hw`` above ``N0 + 1 s`` and not above what the clock
could show): it then gives ``Gap(clock_moved_back)`` with nothing lost, and at a
prompt resume a ``Gap(ring_rotated)`` that lost nothing with old lines delivered
again; with ``since="now"`` those can be lines from before the marker, which
design 5.6's "cannot leak in" does not cover.

After a step (seen live or at a resume), until a request lies above the old
``hw`` (the highest line read before it), a resume's ``-T`` lies below lines
read before the step, and logd returns them first, from the first one at or
above ``-T`` in arrival order, then the new ones. Such a stepped phase also
matches the old windows (kept until then, by content). Until the replay
reaches the new lines (a match of the new window, a line below the request,
a notice at or below ``hw``, a line above the old ``hw``, or the ring check)
it drops as an old line (``deduplicated``) a line that matches neither
window and is stamped at most the old ``hw`` when:

- it is stamped below the new ``hw``;
- the replay showed an old line for sure: a match of an old window, a line
  above every time the clock can show, or a line more than 1 s below the
  new ``hw`` (the device's lines stored late were about 86 ms late);
- or the device logged it before the clock read or within its second
  (stamped below ``N + 1 s``), and the replay showed a line less than 1 s
  below the new ``hw`` (an old one, a line stored late, or one after a second
  small step) or the clock read was at or above the old ``hw`` (every line up
  to the old ``hw`` was then logged before it).

Logd returns no new line before the old ones it holds, so such a line can be
a new one only when the ring lost every line read since the step; the ring
check at the first line above the new ``hw`` that is not dropped then
reports the hole. Any other line is new: the ring check runs on the first
one above the new ``hw``, and the phase goes on matching both windows for
its connection.

Chatty notices (``uid=... expire N lines``, tag ``chatty``; FINDINGS.md
check 6 confirms the text on API 24): below ``S`` (which a re-anchor lowers
to its request, like ``start``) they count in ``backlog_pruned``; in a
resume's phase at or below ``hw`` they are a replay (above); otherwise
``device_lost += N`` and ``Gap(device_reported)`` comes right before the
notice, which is delivered like any line. ``identical N lines`` notices are
ordinary lines and count nowhere. The replay rule assumes that a notice
carries the time of the newest line it replaced: FINDINGS.md check 6 shows
only that its time lies between its neighbours', and a notice with an older
line's time that also replaced unread lines after ``hw`` would hide their
loss.

What the screen cannot tell (besides design 5.6's list): it knows lines by
content and occurrence count, so when ring rotation during an outage takes
some of several byte-identical lines at ``hw``, the replay's later copies
match their count and as many unread copies are lost without a Gap. The
other way round, when rotation takes only some of the lines at ``hw`` that
were read, and none that were not, the ring check still reports
``Gap(ring_rotated)``: the screen does not know logd's buffers (logd prunes
each buffer on its own, so a line kept at ``hw`` does not prove that an
unread line of another buffer is kept). An unparsed line whose inherited time
left the overlap, or that had no time to inherit (it opened a connection),
comes again at a resume, with no Gap: logd returns it by its own time, which
this line does not show. So does one the caps did not window (above). Two steps
in one outage whose second one a clock read cannot show, with a ring that lost
the lines read before, can drop lines the first step's Gap holds in its range.
Lines delivered ahead of any new line and evicted by the caps can come again at
a repeated re-anchor or at the resume after it. After a step, when the first old
line of a resume's replay has left the old window and nothing proves the old
lines first (it is stamped above the new ``hw`` and not above every time the
device clock can show, ``N + 2 s`` plus the host time since the clock read, and
the clock read is below the old ``hw``: any step size meets this once an outage
lasts until the clock enters the old lines' times), the old lines that left the
window are delivered again (design 5.6's exception), the ring check on the first
of them can report a ``Gap(ring_rotated)`` that lost nothing, and the new lines
that follow can raise a second ``Gap(clock_moved_back)``. The same holds for the
old lines stamped from ``N + 1 s`` up to that newest time when the only hint
before them was a line less than 1 s below the new ``hw``. When the ring lost
every line read since the step during the outage, the replay's lines that the
device logged before the clock read or within its second are dropped as old ones
(``deduplicated``) if the first is stamped below the new ``hw`` (a line stored
late, or one after a second small step), or at once when the clock read is at or
above the old ``hw``; they were never read. The ``Gap(ring_rotated)`` at the first
line after them reports the hole; those stamped below the new ``hw`` lie outside
its range. When the first is more than 1 s below the new ``hw`` (a second step
back of more than 1 s, up to ``max(resume_overlap, 1 s)``), the drop lasts until
a line above the old ``hw``. A replay that lost a line at ``hw`` without a notice and
meets a clock step back before any line above ``hw`` never finishes: it
drops every line below the request (``below_cursor``) until a line above
``hw``, whose ``Gap(ring_rotated)`` range does not hold them.

Costs per line (design risk 16): a timestamp conversion, a deque append and
amortized O(1) evictions (a line read below ``hw`` goes to the heap:
O(log n)); one comparison for a step seen live; in a screen phase one dict
lookup more; a chatty check is one string comparison unless the tag is
``chatty``. A resume costs O(len(W)) once, to count the window into the
phase's dict. After a step, the windows of the lines read before it and the
pre-step lines delivered since are kept until a request lies above the old
``hw`` or ``hw`` passes it by ``resume_overlap``: at most the caps over all steps
(the first to arrive leave first, and the highest time evicted bounds later
requests), so the resume window uses at most about twice its caps. A resume
copies the part at or above its request.
"""

from __future__ import annotations

import heapq
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import chain
from typing import TYPE_CHECKING, Literal

from logpyt.streams._events import Gap

if TYPE_CHECKING:
    from logpyt._core.argv import Position
    from logpyt.models import LogEntry

__all__ = [
    "CHATTY_TAG",
    "ResumePlanner",
    "Verdict",
    "chatty_lines",
    "micros",
    "moment",
]

# What the screen decides for a stdout line (design 5.6 step 2).
type Verdict = Literal["new", "deduplicated", "below_cursor"]

US_PER_SECOND = 1_000_000
# adb shell date +%s prints whole seconds (FINDINGS.md, check 10).
CLOCK_RESOLUTION = US_PER_SECOND
# Room for clock drift and the clock command's own time in the estimate of
# the newest time the device clock can show after a re-anchor.
CLOCK_SLACK = US_PER_SECOND
# Room for the drift of the device clock against the host's monotonic clock in
# the same estimate: a share of the time since the clock read (a device that
# gains 100 ppm is 2 s ahead after six hours).
CLOCK_DRIFT = 200e-6
CHATTY_TAG = "chatty"
# logd's pruning notice (FINDINGS.md, check 6; tests/device_facts.py CHATTY).
_EXPIRE = re.compile(r"uid=\d+(?:\([^)]*\))?(?: \S+)? expire (\d+) lines?")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
# Bounds that no epoch-microsecond timestamp reaches (a re-anchored phase
# whose below_cursor rule has ended).
_LOWEST = -(1 << 63)
_HIGHEST = 1 << 63


def micros(when: datetime) -> int:
    """``when`` (aware) as integer epoch microseconds."""
    return (when - _EPOCH) // _MICROSECOND


def moment(us: int) -> datetime:
    """Integer epoch microseconds as an aware UTC datetime."""
    return _EPOCH + timedelta(microseconds=us)


def _highest_shown(clock: int, read_at: float, at: float) -> int:
    """The newest time a clock read as ``clock`` at ``read_at`` can show at ``at``."""
    elapsed = max(at - read_at, 0.0) * (1 + CLOCK_DRIFT)
    return clock + CLOCK_RESOLUTION + CLOCK_SLACK + round(elapsed * US_PER_SECOND)


def chatty_lines(entry: LogEntry) -> int | None:
    """The N of a chatty ``expire N lines`` notice; None for any other line."""
    if entry.tag != CHATTY_TAG or not entry.parsed:
        return None
    found = _EXPIRE.fullmatch(entry.message.rstrip(" "))
    return None if found is None else int(found.group(1))


type _Kind = Literal["start", "resume", "reanchor"]


@dataclass(slots=True, eq=False)
class _Plan:
    """What the attempt whose clock was read last requests (design 5.6)."""

    kind: _Kind | None  # None: no screen phase (backlog, or before the marker)
    request: int | None  # T_req or the start position; None reads the backlog
    shortened: bool = False  # the caps moved the request (overlap_shortened)
    silent: bool = False  # a re-anchor at a step from before the capture


@dataclass(slots=True, eq=False)
class _Phase:
    """The screen phase of one connection (design 5.6 steps 2-3, A.25).

    ``floor``: lines below it are ``below_cursor``. ``expected``: the window's
    elements at or above ``floor``, with their occurrence counts
    (``remaining`` of them unmatched). A resume's phase ends at the first
    line above ``end_above`` (``hw``) that is not a match, with the ring
    check; a re-anchored one only drops its ``below_cursor`` rule there, or
    once the lines at ``hw`` were replayed, and keeps matching for its
    connection.
    ``position`` is ``hw``, ``at_position`` its unmatched elements: the ring
    check of a resume, and of a re-anchor with no Gap to mark the point
    (``silent``), and the end of the replay of a re-anchor's read lines.
    ``raised``: the highest line a re-anchor's earlier attempt delivered from
    those logged before the step, which its replay must return too.
    ``loose``: the expected times of each raw text, built for the first
    unparsed line that has no time to inherit.

    A resume whose request lies at or below a line read before a clock step
    is ``stepped``: logd returns those old lines first. ``old`` holds the old
    windows' elements at or above ``floor`` (``old_left`` of them unmatched).
    Until the replay reaches the new lines (``era``), a line that matches
    neither window and is stamped at most the old hw is an old one when it
    is stamped below ``position``, once the replay showed an old line for
    sure (``evidence``), or when the device logged it before the clock read
    and the replay showed a line just below ``position`` (``hinted``) or the
    clock read was ``caught_up`` with the old hw. ``kept_left`` counts the
    unmatched old lines at the hw kept last, the last ones read before the
    step. ``passed``: the ring check was done; a stepped phase then keeps
    matching both windows.
    """

    kind: _Kind
    floor: int
    end_above: int
    expected: dict[tuple[int, str], int]
    remaining: int
    position: int
    at_position: int
    shortened: bool = False
    covered: bool = False  # a replayed chatty notice at or below hw
    begun: bool = False  # a line was screened in this phase
    loose: dict[str, list[int]] | None = None
    old: dict[tuple[int, str], int] = field(default_factory=dict)
    old_left: int = 0
    stepped: bool = False
    caught_up: bool = False
    evidence: bool = False
    hinted: bool = False
    era: bool = False
    passed: bool = False
    silent: bool = False
    raised: int | None = None
    kept_left: int = 0


class ResumePlanner:
    """The start and resume positions and the replay screen of one run.

    Args:
        since: ``StreamConfig.since``.
        overlap: ``resume_overlap`` in seconds.
        max_lines: ``resume_window_lines``.
        max_bytes: ``resume_window_bytes``.
        keep: Whether the window is kept (``reconnect`` or ``stall_timeout``
            is set, design 2.2); without it a run never resumes.

    """

    __slots__ = (
        "_bytes",
        "_cap_evicted",
        "_ceiling",
        "_clock",
        "_clock_at",
        "_crossed",
        "_era_low",
        "_first_at",
        "_first_clock",
        "_floor",
        "_gapped",
        "_hw",
        "_hw_at",
        "_keep",
        "_kept",
        "_last_read_at",
        "_last_ts",
        "_late",
        "_max_bytes",
        "_max_lines",
        "_new_era",
        "_noted",
        "_old",
        "_old_bytes",
        "_old_evicted",
        "_old_lines",
        "_old_low",
        "_old_top",
        "_overlap",
        "_pending_restart",
        "_phase",
        "_plan",
        "_prev_ts",
        "_raised",
        "_read_over",
        "_read_top",
        "_since",
        "_start",
        "_started",
        "_step",
        "_step_reported",
        "_window",
        "backlog_pruned",
        "device_lost",
        "out",
        "overlap_shortened",
    )

    def __init__(
        self,
        since: Literal["backlog", "now"] | datetime,
        *,
        overlap: float,
        max_lines: int,
        max_bytes: int,
        keep: bool,
    ) -> None:
        self._since = since
        self._overlap = round(overlap * US_PER_SECOND)
        # A clock read, or a line read, this far below hw shows a step back:
        # never less than the clock read's resolution (whole seconds).
        self._step = max(self._overlap, CLOCK_RESOLUTION)
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._keep = keep
        self._started = since != "now"
        # The start position S, and the first connection's own -T (the floor
        # of every later request; None for "backlog").
        self._start: int | None = None
        self._floor: int | None = None
        if isinstance(since, datetime):
            self._start = self._floor = micros(since)
        self._hw: int | None = None
        # W: the lines read at or above the hw of their time (timestamps
        # never decrease), and a min-heap of those read below it.
        self._window: deque[tuple[int, str, int]] = deque()
        self._late: list[tuple[int, str, int]] = []
        self._bytes = 0
        self._cap_evicted: int | None = None
        self._last_read_at: float | None = None
        self._last_ts: int | None = None  # the last parsed line of the connection
        self._prev_ts: int | None = None  # the last timestamp read (Gap.after)
        # The latest clock read, whole seconds as microseconds, and when its
        # command was issued (host-monotonic seconds); a forward step seen in
        # a line moves them too.
        self._clock: int | None = None
        self._clock_at = 0.0
        self._plan = _Plan(None, None)
        self._phase: _Phase | None = None
        self._gapped: int | None = None  # the hw a clock_moved_back Gap named
        # The capture's first clock read and when it was issued: a line at hw
        # logged before it is on the far side of a step from before the capture.
        self._first_clock: int | None = None
        self._first_at = 0.0
        self._hw_at = 0.0  # when the line at hw was read (host-monotonic)
        self._step_reported = False  # a step during the capture was reported
        # After a re-anchor, until the device clock passes it: the highest
        # timestamp read before the step.
        self._ceiling: int | None = None
        # The highest line logged before the step that a re-anchored
        # connection delivered ahead of its first new line.
        self._raised: int | None = None
        # A re-anchored connection restarts hw and the window at its first
        # new line.
        self._pending_restart = False
        # What this connection has shown after a step: it crossed a step
        # back in its own lines, it read past the lines read before the step,
        # it read a line of the new clock. See `_stepped`.
        self._crossed = False
        self._read_over = False
        self._new_era = False
        # After a step back, until a request lies above every line read
        # before it or hw passes them by the overlap: the windows those lines
        # left and the pre-step lines delivered since (counts by (timestamp,
        # raw), and each line in arrival order for the caps), their highest
        # time, the highest time read before a step (the bound of the drop
        # by time, `_old_line`), the hw whose window was added last, and the
        # highest time the caps evicted from them.
        self._old: dict[tuple[int, str], int] | None = None
        self._old_lines: deque[tuple[int, str, int]] = deque()
        self._old_bytes = 0
        # No line read since hw last restarted, and none read before the
        # last step, is stamped below these. The second is of use only when
        # the first connection read the ring from its start: with a start
        # position the ring also holds older lines that were never read and
        # are not the capture's.
        self._era_low: int = _HIGHEST
        self._old_low: int = _HIGHEST if since == "backlog" else _LOWEST
        self._old_top: int | None = None
        self._read_top: int | None = None
        self._kept: int | None = None
        self._old_evicted: int | None = None
        self._noted = False  # this connection's cap INFO was queued
        self.out: list[Gap | str] = []
        self.backlog_pruned = 0
        self.device_lost = 0
        self.overlap_shortened = 0

    # -- the core's interface --------------------------------------------------------

    @property
    def start_marker(self) -> bool:
        """Whether the start is placed by a start marker (``since="now"``)."""
        return self._since == "now"

    @property
    def started(self) -> bool:
        """Whether the capture has started: True unless the start marker is unread."""
        return self._started

    @property
    def cursor(self) -> int | None:
        """``hw`` in epoch microseconds; None until a line was read (for tests)."""
        return self._hw

    @property
    def window_lines(self) -> int:
        """The number of lines in the window (for tests and diagnostics)."""
        return len(self._window) + len(self._late)

    @property
    def window_bytes(self) -> int:
        """The bytes the window counts (for tests and diagnostics)."""
        return self._bytes

    @property
    def screening(self) -> bool:
        """Whether the live connection is inside its screen phase (for tests)."""
        return self._phase is not None

    def take(self) -> list[Gap | str]:
        """The Gaps and INFO messages queued since the last call, in order."""
        out, self.out = self.out, []
        return out

    def clock(self, seconds: int, *, issued: float) -> Gap | None:
        """Record the device clock of an attempt and plan its request.

        Args:
            seconds: The device time in whole epoch seconds.
            issued: When the clock command was issued (host-monotonic).

        Returns:
            ``Gap(clock_moved_back)`` when the device clock moved back since
            ``hw`` was read; once per step, however many attempts it takes.

        """
        clock = seconds * US_PER_SECOND
        previous, previous_at = self._clock, self._clock_at
        self._clock, self._clock_at = clock, issued
        if self._ceiling is not None and clock >= self._ceiling:
            self._ceiling = None  # the device clock passed the old times
        if not self._started:
            # since="now" before the marker: every attempt starts over at its
            # own clock with a new marker; nothing read so far is the capture's,
            # so a cap INFO queued before the marker no longer holds.
            self._reset_window()
            self.out = [item for item in self.out if not isinstance(item, str)]
            self._plan = _Plan(None, max(clock - US_PER_SECOND, 0))
            self._first_clock, self._first_at = clock, issued
            return None
        if self._first_clock is None:
            self._first_clock, self._first_at = clock, issued
        if self._start is None:
            self._start = clock  # since="backlog": S is the first clock read
        hw = self._hw
        if hw is None or not self._keep:
            # Nothing read yet: the resume uses the initial position (step 4).
            kind: _Kind | None = "start" if self._floor is not None else None
            self._plan = _Plan(kind, self._floor)
            return None
        if clock < hw - self._step:
            # From the read of the line at hw: an earlier attempt that only
            # replayed lines read before the step moved nothing.
            outage = max(issued - self._hw_at, 0.0)
            request = clock - round(outage * US_PER_SECOND) - self._overlap
            request = max(request, 0)
            fell = self._clock_fell(clock, issued, self._first_clock, self._first_at)
            silent = self._predates(hw) and not fell
            self._plan = _Plan("reanchor", request, silent=silent)
            if silent:
                return None  # from before the capture
            if self._gapped == hw and not self._clock_fell(
                clock, issued, previous, previous_at
            ):
                return None  # reported already: no second step since then
            self._gapped = hw
            self._step_reported = True
            return Gap(
                after=moment(hw),
                before=moment(clock),
                lost=None,
                reason="clock_moved_back",
            )
        request = max(hw - self._overlap, 0)
        shortened = False
        for evicted in (self._cap_evicted, self._old_evicted):
            # The old window lacks what the caps evicted from it too: once the
            # new times passed that, the request skips it (a step whose clock
            # did not move, such as a line stored late, keeps those times).
            # It stays below hw, so the resume can check the ring.
            if evicted is not None and request <= evicted < hw:
                request = evicted + 1
                shortened = True
        if self._floor is not None and self._floor > request:
            request = self._floor
        if self._old_top is not None and request > self._old_top:
            self._drop_old()  # logd no longer returns a line read before the step
        self._plan = _Plan("resume", request, shortened=shortened)
        return None

    def position(self) -> Position | None:
        """The ``-T`` of the attempt whose clock was read last; opens its phase."""
        plan = self._plan
        self._noted = False
        self._last_ts = None
        self._pending_restart = False
        self._crossed = self._read_over = self._new_era = False
        self._phase = self._open(plan)
        if plan.request is None:
            return None
        return divmod(plan.request, US_PER_SECOND)

    def before_start(self, entry: LogEntry, nbytes: int, now: float) -> None:
        """A stdout line before the start marker: windowed, nothing else."""
        ts = self._stamp(entry)
        self._last_read_at = now
        if ts is not None:
            self._prev_ts = ts
            if self._keep:
                stamped = entry.parsed and entry.timestamp.tzinfo is not None
                self._add(ts, entry.raw, nbytes, stamped=stamped)

    def marker(self, entry: LogEntry, nbytes: int, now: float) -> None:
        """The start marker was read: ``S`` is its timestamp (design 5.6).

        Lines ahead of it stamped above it by more than the step bound lie on
        the far side of a step from before the capture (design A.25): the
        marker restarts the window, so it is windowed, and later requests stay
        at or above it, because the lines ahead of it were not all windowed.
        """
        ts = self._stamp(entry)
        hw = self._hw
        stepped = ts is not None and hw is not None and ts < hw - self._step
        if stepped:
            assert ts is not None  # noqa: S101  (narrowed above)
            self._reanchor_live(ts, report=False)
        self.before_start(entry, nbytes, now)
        self._started = True
        self._floor = ts if stepped else self._plan.request
        self._start = self._prev_ts if self._prev_ts is not None else self._floor

    def screen(self, entry: LogEntry, nbytes: int, now: float) -> Verdict:
        """Decide a stdout line of a started capture (design 5.6 steps 2-3).

        A new line moves the cursor and enters the window; a chatty notice
        among the new lines is counted (``backlog_pruned``, or
        ``device_lost`` with a `Gap` queued before it). This runs on every
        line (design risk 16): the common path is a timestamp, a few
        comparisons and the window's append.
        """
        self._last_read_at = now
        when = entry.timestamp
        parsed = entry.parsed and when.tzinfo is not None
        if parsed:
            ts = (when - _EPOCH) // _MICROSECOND
            self._last_ts = ts
        else:
            ts = self._last_ts  # an unparsed line inherits the last time
            if ts is None:
                # No timestamp to window by; a replay is told by content.
                phase = self._phase
                return "new" if phase is None else self._unstamped(phase, entry)
        prev = self._prev_ts
        self._prev_ts = ts
        unread = False
        if self._phase is not None or self._ceiling is not None:
            verdict = self._replay_verdict(entry, ts, now, parsed=parsed)
            if verdict == "unread":
                unread = True
            elif verdict is not None:
                return verdict
        hw = self._hw
        # A line in time order (at or above hw) skips the rest at once.
        if (
            hw is not None
            and ts < hw
            and parsed
            and not unread
            and self._moved_back(ts)
        ):
            # A clock step back seen live (design A.25).
            self._reanchor_live(ts, report=not self._predates(hw))
        if entry.tag == CHATTY_TAG:
            self._chatty(entry, ts, prev)
        if unread:
            # Delivered once; it moves neither hw nor the window, which belong
            # to the new clock's times.
            self._pre_step(ts, entry.raw, nbytes)
            return "new"
        if self._keep:
            if self._pending_restart:
                # The re-anchored connection's first new line: hw and the
                # window restart from here (design 5.6).
                self._pending_restart = False
                self._restart_window()
            self._add(ts, entry.raw, nbytes, stamped=parsed)
        self._new_era = True
        return "new"

    def _replay_verdict(
        self, entry: LogEntry, ts: int, now: float, *, parsed: bool
    ) -> Verdict | Literal["unread"] | None:
        """A line of a connection in its phase, or of any connection after a step.

        ``"unread"``: logged before the step and never read, delivered once.
        None: it goes on by the usual rules.
        """
        phase = self._phase
        if phase is not None:
            self._begin(phase)
            if ts >= phase.floor and self._match(phase, (ts, entry.raw)):
                return "deduplicated"
        step = None if self._ceiling is None else self._stepped(ts, now)
        if phase is not None:
            verdict = self._screened(phase, entry, ts, step, parsed=parsed)
            if verdict is not None:
                return verdict
        if step == "old":
            return "deduplicated"  # logged before the clock moved back
        if self._pending_restart:
            self._hole(ts)
        return step

    def _stepped(self, ts: int, now: float) -> Literal["old", "unread"] | None:
        """After a step: what a line that matches no window and is above the clock is.

        Stamped above every time the device clock can show, it was logged
        before the clock moved back, or the clock moved forward since it was
        read. None: it is below every line read before the step and the ring
        was read from its start, so it was never read and goes by the usual
        rules (hw, the ring check, a step seen live); the new clock can have
        stamped it; or it did after a forward step (`_moved_forward`):

        - this connection crossed a step back in its own lines: it reads in
          arrival order past the lines read before, so it is a forward step;
        - it read a line of the new clock, and the host time since the clock
          read is past the slack: logd returns no line from before the step
          after one of the new clock, but for a ring in time order, whose rest
          comes right after the new lines. A forward step within the slack, or
          ahead of any new line, goes on like a line logged before the step.

        Otherwise ``"old"``, a line read before that left the old windows
        (at most the highest time read before the step, while the replay has
        not passed those lines), or ``"unread"``: delivered once and matched
        from then on.
        """
        if ts < self._old_low or ts <= self._newest(now):
            return None
        if self._crossed or (
            self._new_era and now > self._clock_at + CLOCK_SLACK / US_PER_SECOND
        ):
            self._moved_forward(ts, now)
            return None
        if self._read_over:
            return "unread"
        ceiling = self._ceiling
        if ceiling is not None and ts <= ceiling:
            return "old"
        # Above every line read: the replay passed them.
        self._read_over = True
        phase = self._phase
        if phase is not None and phase.kind == "reanchor":
            phase.floor, phase.end_above = _LOWEST, _HIGHEST
        return "unread"

    def _moved_forward(self, ts: int, now: float) -> None:
        """The clock moved forward past every time it could show: go by this line.

        It goes on like any line, moves hw and enters the window, and the
        estimate of the clock restarts from it. A forward step past the old
        times ends what a clock read at or above them ends.
        """
        clock = ts - ts % US_PER_SECOND
        self._clock, self._clock_at = clock, now
        if self._ceiling is not None and clock >= self._ceiling:
            self._ceiling = None
        phase = self._phase
        if phase is not None:
            phase.era = True  # no drop of old lines: the replay is past them

    def _hole(self, ts: int) -> None:
        """The first line that goes on after a re-anchor's replay: the ring check.

        A re-anchor with no Gap has nothing to mark the point, and a repeated
        attempt that follows delivered pre-step lines lies behind its Gap: when
        the ring lost the position, ``Gap(ring_rotated)`` says so, once.
        """
        phase = self._phase
        if phase is None:
            return
        after = None
        raised = phase.raised
        if phase.silent and phase.at_position:
            after = phase.position
        elif raised is not None and any(
            count and key[0] == raised for key, count in phase.expected.items()
        ):
            after = raised
        phase.silent = False
        phase.raised = None
        if after is not None:
            self.out.append(Gap(moment(after), moment(ts), None, "ring_rotated"))

    def _chatty(self, entry: LogEntry, ts: int, prev: int | None) -> None:
        """Count a new chatty notice: backlog below S, else a reported loss."""
        lost = chatty_lines(entry)
        if lost is None:
            return
        start = self._start
        if (start is not None and ts < start) or self._above_capture(ts):
            self.backlog_pruned += lost
            return
        self.device_lost += lost
        self.out.append(
            Gap(
                after=None if prev is None else moment(prev),
                before=moment(ts),
                lost=lost,
                reason="device_reported",
            )
        )

    # -- the screen phase --------------------------------------------------------------

    def _open(self, plan: _Plan) -> _Phase | None:
        """The phase of a connection that requests ``plan``."""
        kind, request = plan.kind, plan.request
        if kind is None or request is None:
            return None
        if kind == "start":
            # since=<datetime>, nothing read yet: lines below the start are
            # dropped until the first one at or above it (design A.7).
            return _Phase(kind, request, request - 1, {}, 0, 0, 0)
        hw = self._hw
        assert hw is not None  # noqa: S101  (a resume has read lines)
        if kind == "reanchor":
            clock = self._clock
            assert clock is not None  # noqa: S101  (set by clock())
            self._keep_old(hw)
            assert self._old is not None  # noqa: S101  (kept just now)
            expected = {key: n for key, n in self._old.items() if key[0] >= request}
            self._ceiling = hw if self._ceiling is None else max(hw, self._ceiling)
            self._pending_restart = True
            # The new times lie below the old start: later requests and the
            # chatty notices go by the re-anchored request instead.
            if self._floor is not None:
                self._floor = min(self._floor, request)
            if self._start is not None and not plan.silent:
                self._start = min(self._start, request)
            # The replay is over at the first new line logged after the
            # clock read, or once the lines at hw were replayed: a line below
            # the request after them is one the new clock stamped before it
            # (a reader that lagged behind the step was never sent it). With
            # no Gap, no line of the new clock was read yet: no floor.
            phase = _Phase(
                kind,
                _LOWEST if plan.silent else request,
                clock + CLOCK_RESOLUTION - 1,
                expected,
                sum(expected.values()),
                hw,
                sum(n for key, n in expected.items() if key[0] == hw),
            )
            phase.silent = plan.silent
            phase.raised = self._raised
            return phase
        expected = {}
        at_position = 0
        for ts, raw, _size in chain(self._window, self._late):
            if ts >= request:
                key = (ts, raw)
                expected[key] = expected.get(key, 0) + 1
                at_position += ts == hw
        phase = _Phase(
            kind,
            request,
            hw,
            expected,
            sum(expected.values()),
            hw,
            at_position,
            shortened=plan.shortened,
        )
        top, old = self._old_top, self._old
        if top is not None and old is not None and request <= top:
            # logd returns the lines read before the step first (design A.25).
            phase.stepped = True
            phase.old = {key: n for key, n in old.items() if key[0] >= request}
            phase.old_left = sum(phase.old.values())
            kept = self._kept
            phase.kept_left = sum(n for key, n in phase.old.items() if key[0] == kept)
            phase.caught_up = self._clock is not None and self._clock >= top
        else:
            self._read_over = True  # logd no longer returns a line read before
        return phase

    def _screened(
        self,
        phase: _Phase,
        entry: LogEntry,
        ts: int,
        step: Literal["old", "unread"] | None,
        *,
        parsed: bool,
    ) -> Verdict | None:
        """The phase's verdict on a line that matched no window; None: not its to drop.

        ``step``: a line logged before the clock moved back, which the caller
        drops or delivers; it never ends the phase.
        """
        if ts < phase.floor:
            if phase.stepped:
                phase.era = True  # the new lines below the request
            if parsed and step is None and self._moved_back(ts):
                return None  # the replay is over: a step seen live
            if phase.kind == "start":
                # Below S: a notice among them is backlog (design 5.6).
                self.backlog_pruned += chatty_lines(entry) or 0
            return "below_cursor"
        if step is not None:
            phase.evidence = True
            if step == "unread" and phase.kind == "resume":
                self._passed(phase, ts)  # hw lags behind it: the ring check
            return None
        return self._unmatched(phase, entry, ts)

    def _match(self, phase: _Phase, key: tuple[int, str]) -> bool:
        """Whether the line is a replayed element of the window or an old one."""
        count = phase.expected.get(key)
        if count:
            self._matched(phase, key, count)
            phase.era = True
            if phase.kind == "resume":
                self._new_era = True  # a line of the window read since the step
            return True
        count = phase.old.get(key)
        if count:
            self._matched_old(phase, key, count)
            return True
        return False

    def _unmatched(self, phase: _Phase, entry: LogEntry, ts: int) -> Verdict | None:
        """A line at or above the floor that matches no window."""
        above = ts > phase.end_above
        if (
            not above
            and not phase.passed
            and phase.kind == "resume"
            and chatty_lines(entry) is not None
        ):
            phase.covered = True  # logd pruned lines we read: it holds the position
            phase.era = True  # a notice of the new lines, which a stepped one reached
            if ts == phase.position:
                phase.at_position = 0
            return "deduplicated"
        if phase.stepped and not phase.era and self._old_line(phase, ts):
            return "deduplicated"  # read before the step (design A.25)
        if above:
            self._passed(phase, ts)
        return None

    def _old_line(self, phase: _Phase, ts: int) -> bool:
        """Whether a line that matches no window is dropped as an old one.

        The replay of a stepped resume returns the old lines first (from the
        first one at or above the request, in arrival order), then the new
        ones, and logd returns no new line before the old ones it holds: a
        new line ahead of every line of the new window means that the ring
        lost all of them, and the ring check at the first line above the new
        hw that is not dropped reports that. A line above the old hw (the
        highest line read before a step) is not an old one: the replay is past
        them, and a pre-step line delivered above it is matched by content. At
        most the old hw, the line is old when:

        - it is stamped below the new hw (else a line stored late, or one
          after a second small step, logged after the lines read since the
          step that the ring lost);
        - the replay showed an old line for sure: a match of an old window,
          a line above every time the clock can show, or a line more than
          1 s below the new hw (the device's lines stored late were about
          86 ms late);
        - or the device logged it before the clock read or within its
          second (stamped below N + 1 s), and the replay showed a line less
          than 1 s below the new hw or the clock read was at or above the old
          hw (every line up to the old hw was then logged before it). From N
          + 1 s on it can be a line logged after the clock read.
        """
        top = self._read_top
        if top is None or ts > top:
            phase.era = True
            return False
        position = phase.position
        if ts < position:
            if ts < position - CLOCK_RESOLUTION:
                phase.evidence = True
            else:
                phase.hinted = True
            return True
        if phase.evidence:
            return True
        clock = self._clock
        return (
            (phase.hinted or phase.caught_up)
            and clock is not None
            and ts < clock + CLOCK_RESOLUTION
        )

    def _passed(self, phase: _Phase, ts: int) -> None:
        """The first new line above the phase's end, at ``ts``.

        A resume's phase ends with the ring check (design 5.6 step 3). A
        re-anchored one only drops its below_cursor rule: the old window stays
        matched for the whole connection, also above the clock read. So does
        a stepped resume's: old lines can still follow the line that ended it.
        """
        if phase.kind == "reanchor":
            phase.floor, phase.end_above = _LOWEST, _HIGHEST
            if not phase.remaining:
                self._phase = None
            return
        if phase.kind == "resume" and phase.at_position and not phase.covered:
            self.out.append(
                Gap(
                    after=moment(phase.position),
                    before=moment(ts),
                    lost=None,
                    reason="ring_rotated",
                )
            )
        if phase.stepped and (phase.remaining or phase.old_left):
            # Old lines can still follow the line that ended it, and the new
            # lines below the request after them.
            phase.end_above = _HIGHEST
            phase.passed = phase.era = True
            phase.at_position = 0
            return
        self._phase = None

    def _over(self, phase: _Phase) -> bool:
        """Whether the phase's replay of the lines read before is over.

        A resume's is over once every line at hw was replayed (or covered by
        a chatty notice), every element was, or a line above hw came; a
        re-anchored one's once its new lines began (its below_cursor rule
        ends with the replay of the window read before the step, or at the
        first new line logged after the clock read).
        """
        if phase.kind == "resume":
            return phase.passed or not phase.at_position or not phase.remaining
        if phase.kind == "reanchor":
            return not self._pending_restart
        return True

    def _begin(self, phase: _Phase) -> None:
        if not phase.begun:
            phase.begun = True
            if phase.shortened:
                self.overlap_shortened += 1  # this resume took place

    def _matched(self, phase: _Phase, key: tuple[int, str], count: int) -> None:
        """A replayed element of the window: one occurrence fewer.

        The phase goes on (design A.25): a resume's ends at the first line
        above hw, a re-anchored one's with its connection, or once it has
        nothing left to match or drop. A re-anchored one drops its
        below_cursor rule once the lines at hw were replayed: they were the
        last read before the step, so the lines below the request that follow
        were never read.
        """
        phase.expected[key] = count - 1
        phase.remaining -= 1
        if key[0] == phase.position:
            phase.at_position -= 1
            if phase.kind == "reanchor" and not phase.at_position:
                phase.floor, phase.end_above = _LOWEST, _HIGHEST
                self._read_over = True
        if not phase.remaining and phase.floor == _LOWEST and not phase.old_left:
            self._phase = None

    def _matched_old(self, phase: _Phase, key: tuple[int, str], count: int) -> None:
        """A replayed line of an old window: proof of the old lines."""
        phase.old[key] = count - 1
        phase.old_left -= 1
        phase.evidence = True
        if key[0] == self._kept:
            phase.kept_left -= 1
            if not phase.kept_left:
                self._read_over = True  # the last lines read before the step
        if not phase.remaining and phase.floor == _LOWEST and not phase.old_left:
            self._phase = None

    def _unstamped(self, phase: _Phase, entry: LogEntry) -> Verdict:
        """An unparsed line before any parsed one of a screened connection.

        It has no time to inherit, but the window holds it under the time it
        inherited when it was read: a raw text the phase still expects (in
        the window, or in an old one) is a replay, matched at its lowest
        expected time, which the next unparsed lines inherit.
        """
        self._begin(phase)
        loose = phase.loose
        if loose is None:
            loose = phase.loose = {}
            for (ts, raw), count in chain(phase.expected.items(), phase.old.items()):
                if count:
                    loose.setdefault(raw, []).extend([ts] * count)
            for stamps in loose.values():
                stamps.sort(reverse=True)
        raw = entry.raw
        stamps = loose.get(raw)
        while stamps:
            ts = stamps.pop()
            key = (ts, raw)
            count = phase.expected.get(key)
            if count:
                self._last_ts = self._prev_ts = ts
                self._matched(phase, key, count)
                return "deduplicated"
            count = phase.old.get(key)
            if count:
                self._last_ts = self._prev_ts = ts
                self._matched_old(phase, key, count)
                return "deduplicated"
        return "new"

    # -- clock steps -------------------------------------------------------------------

    def _moved_back(self, ts: int) -> bool:
        """Whether a line the screen did not drop shows a clock step back.

        It is stamped below ``hw - max(resume_overlap, 1 s)`` and read outside
        a replay of the lines read before (design A.25): the bound of the
        resume's clock check. A line stored that late is taken for a step
        too; the device's out-of-order lines seen so far were about 86 ms
        late.
        """
        hw = self._hw
        if hw is None or ts >= hw - self._step or self._pending_restart:
            return False
        phase = self._phase
        return phase is None or self._over(phase)

    def _predates(self, hw: int) -> bool:
        """Whether the line at ``hw`` was logged before the capture (design A.25).

        It is stamped below the first clock read's second, or above every time
        that clock could show when the line was read. Once a step during the
        capture was reported, times below the first read are the capture's.
        """
        first = self._first_clock
        if first is None or self._step_reported:
            return False
        if hw < first + CLOCK_RESOLUTION:
            return True
        return hw > _highest_shown(first, self._first_at, self._hw_at)

    def _above_capture(self, ts: int) -> bool:
        """Whether a line just read lies on the far side of a step from before it.

        Stamped above every time the first clock read could show by now, with
        no step reported: logged before the capture. A forward step during the
        capture looks the same (design A.25).
        """
        first, at = self._first_clock, self._last_read_at
        if first is None or at is None or self._step_reported:
            return False
        return ts > _highest_shown(first, self._first_at, at)

    def _clock_fell(
        self, clock: int, issued: float, read: int | None, read_at: float
    ) -> bool:
        """Whether a clock read shows the clock moved back since an earlier read."""
        if read is None:
            return False
        elapsed = max(issued - read_at, 0.0) * (1 - CLOCK_DRIFT)
        return clock < read + round(elapsed * US_PER_SECOND) - self._step

    def _reanchor_live(self, ts: int, *, report: bool = True) -> None:
        """A clock step back seen live at a line stamped ``ts``: re-anchor there.

        ``Gap(clock_moved_back, after=hw, before=ts)`` at once, and the rules
        of the resume's re-anchor: the old window is kept for the replays of
        later connections, the old hw is the ceiling, the start and the
        floor of later requests drop to this line's request, and hw and the
        window restart at the line, which the caller delivers. A step from
        before the capture (``report`` False) has no Gap, and ``S`` stays:
        what lies below it is not this capture's.
        """
        hw = self._hw
        assert hw is not None  # noqa: S101  (checked by the callers)
        if report:
            self.out.append(
                Gap(
                    after=moment(hw),
                    before=moment(ts),
                    lost=None,
                    reason="clock_moved_back",
                )
            )
            self._gapped = hw
            self._step_reported = True
        self._keep_old(hw)
        self._crossed = True
        self._ceiling = hw if self._ceiling is None else max(hw, self._ceiling)
        low = ts - self._overlap
        if self._floor is not None:
            self._floor = min(self._floor, low)
        if report and self._start is not None:
            self._start = min(self._start, low)
        phase = self._phase
        if phase is not None:
            # Its replay is over, but what it still expects can follow: it
            # only matches from now on.
            phase.floor, phase.end_above = _LOWEST, _HIGHEST
            phase.passed = phase.era = True
            phase.at_position = 0
            if not phase.remaining and not phase.old_left:
                self._phase = None
        self._restart_window()

    def _restart_window(self) -> None:
        """hw and the window restart at a step; the old one keeps its caps' bound."""
        evicted, old = self._cap_evicted, self._old_evicted
        if evicted is not None and (old is None or evicted > old):
            self._old_evicted = evicted
        self._raised = None
        self._reset_window()

    def _drop_old(self) -> None:
        """No request reaches a line read before the step any more."""
        self._old = self._old_top = self._read_top = None
        self._kept = self._old_evicted = None
        self._old_lines = deque()
        self._old_bytes = 0

    def _keep_old(self, hw: int) -> None:
        """Keep the window of the lines read before a step for later replays."""
        if self._kept == hw:
            return  # an earlier attempt at this step kept it already
        self._old_low = min(self._old_low, self._era_low)
        for ts, raw, size in chain(self._window, self._late):
            self._remember(ts, raw, size)
        if self._old is None:
            self._old = {}
        self._old_top = hw if self._old_top is None else max(hw, self._old_top)
        self._read_top = hw if self._read_top is None else max(hw, self._read_top)
        self._kept = hw
        self._trim_old(hw)

    def _pre_step(self, ts: int, raw: str, nbytes: int) -> None:
        """A line logged before the step was delivered: replays match it from now on."""
        self._remember(ts, raw, nbytes)
        # A request at or below it still has to match it; the drop by time keeps
        # to the lines read (`_read_top`): the ring can have lost this one.
        top = self._old_top
        self._old_top = ts if top is None else max(ts, top)
        # A later replay drops it by time too, once the caps evicted it.
        if self._ceiling is not None and ts > self._ceiling:
            self._ceiling = ts
        if self._pending_restart and (self._raised is None or ts > self._raised):
            self._raised = ts
        self._trim_old(None)

    def _remember(self, ts: int, raw: str, size: int) -> None:
        """One line more among those a replay can return from before a step."""
        old = self._old
        if old is None:
            old = self._old = {}
        key = (ts, raw)
        old[key] = old.get(key, 0) + 1
        self._old_lines.append((ts, raw, size))
        self._old_bytes += size

    def _trim_old(self, keep: int | None) -> None:
        """The caps on what is kept from before a step (design 5.10).

        They evict the first lines to arrive, of the oldest step: a replay
        drops those by time (above every time the clock can show), and the
        smallest times can be the newest step's. The lines stamped ``keep``,
        the hw of the step that just came, stay, as in the window. The highest
        time evicted bounds later requests.
        """
        lines, old = self._old_lines, self._old
        if old is None:
            return
        while (
            len(lines) > self._max_lines or self._old_bytes > self._max_bytes
        ) and lines[0][0] != keep:
            ts, raw, size = lines.popleft()
            self._old_bytes -= size
            key = (ts, raw)
            count = old[key]
            if count == 1:
                del old[key]
            else:
                old[key] = count - 1
            if self._old_evicted is None or ts > self._old_evicted:
                self._old_evicted = ts

    def _newest(self, now: float) -> int:
        """The newest time the device clock can show at ``now`` (after a re-anchor)."""
        clock = self._clock
        assert clock is not None  # noqa: S101  (a re-anchor read the clock)
        return _highest_shown(clock, self._clock_at, now)

    # -- the window --------------------------------------------------------------------

    def _stamp(self, entry: LogEntry) -> int | None:
        """The line's timestamp for windowing; unparsed lines inherit the last one."""
        when = entry.timestamp
        if entry.parsed and when.tzinfo is not None:
            ts = (when - _EPOCH) // _MICROSECOND
            self._last_ts = ts
            return ts
        return self._last_ts

    def _add(self, ts: int, raw: str, nbytes: int, *, stamped: bool = True) -> None:
        """A line read: move hw, window it, evict by time and by the caps.

        ``stamped``: the line has its own time, not one it inherited.
        """
        hw = self._hw
        window, late = self._window, self._late
        if hw is None:
            # A later line below this one by more than this is a step.
            self._era_low = ts - self._step
        if hw is None or ts >= hw:
            hw = self._hw = ts
            self._hw_at = self._last_read_at or 0.0
            window.append((ts, raw, nbytes))
            top = self._old_top
            if top is not None and ts - self._overlap > top:
                # Every later request lies above the lines read before the
                # step (a re-anchor's below them: the ceiling covers those).
                self._drop_old()
        elif ts >= hw - self._overlap:
            heapq.heappush(late, (ts, raw, nbytes))
        else:
            return
        low = hw - self._overlap
        size = self._bytes + nbytes
        # The line at hw is in the deque, so it is never empty here.
        while window[0][0] < low:
            size -= window.popleft()[2]
        while late and late[0][0] < low:
            size -= heapq.heappop(late)[2]
        self._bytes = size
        if len(window) + len(late) > self._max_lines or size > self._max_bytes:
            self._evict(ts == hw and not stamped)

    def _evict(self, inherited: bool) -> None:  # noqa: FBT001  (private, one caller)
        """The caps: evict the smallest timestamps first (design 5.6), never hw.

        Remembers the highest evicted time. The lines at hw stay, so the
        request stays at or below hw and the resume can check the ring. An
        unparsed line just added at hw (``inherited``) has no time of its own,
        so it does not stay over the caps: a long run of them would not be
        bounded, and the ring check never needs one (design 5.10). Its replay
        can then be delivered again. The only line in the deque stays: hw has
        a line there.
        """
        window, late, hw = self._window, self._late, self._hw
        while (
            len(window) + len(late) > self._max_lines or self._bytes > self._max_bytes
        ):
            if late and late[0][0] <= window[0][0]:
                evicted, _raw, size = heapq.heappop(late)  # below hw
            elif window[0][0] != hw:
                evicted, _raw, size = window.popleft()
            else:
                if inherited and len(window) > 1:  # only lines stamped hw left
                    self._bytes -= window.pop()[2]
                    self._note_cap()
                break
            self._bytes -= size
            if self._cap_evicted is None or evicted > self._cap_evicted:
                self._cap_evicted = evicted
            self._note_cap()

    def _note_cap(self) -> None:
        """The INFO of the first cap eviction of a connection."""
        if not self._noted:
            self._noted = True
            self.out.append(
                "the resume window reached its cap (resume_window_lines="
                f"{self._max_lines}, resume_window_bytes={self._max_bytes}) "
                "inside resume_overlap: the next resume requests a shorter "
                "overlap"
            )

    def _reset_window(self) -> None:
        self._window = deque()
        self._late = []
        self._bytes = 0
        self._hw = None
        self._cap_evicted = None
