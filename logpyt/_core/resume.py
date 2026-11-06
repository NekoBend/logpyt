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
  evict a line stamped ``hw``: the request stays at or below ``hw``, so the
  resume can check the ring, and the window may exceed a cap by the lines of
  that one microsecond. A cap eviction always takes a line inside the
  overlap; the first one of a connection gives one INFO, and a resume whose
  request it moves counts in ``overlap_shortened``. The window is kept only
  when ``reconnect`` or ``stall_timeout`` is set: without either a run never
  resumes.

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
the first line above ``hw`` or once every element was matched; a line at
``hw`` neither replayed nor covered by such a notice then gives
``Gap(ring_rotated)``. A connection that ends inside its phase leaves the
window as it was, so the next resume screens the same way. With
``since=<datetime>`` the first connection drops lines below the start until
the first one at or above it (``below_cursor``): the device can return
lines below ``-T`` (design A.7).

The clock check of every resume compares the attempt's clock ``N`` (whole
seconds: ``adb shell date +%s``) with ``hw``. The clock moved back when
``N < hw - max(resume_overlap, 1 s)``: with whole seconds a clock that did
not move can read up to 1 s below ``hw``, so the bound is never below the
clock's resolution (the same as design 5.6's ``N < hw - resume_overlap`` for
every ``resume_overlap >= 1``). The resume then emits
``Gap(clock_moved_back)``, requests ``N - outage - resume_overlap`` (the
outage measured host-monotonically from the last line read), and screens the
replay against the old window (no ring check: the Gap marks the point) for
the whole connection; its ``below_cursor`` rule ends at the first new line
logged after the clock read. From then on, until the device clock passes the
old ``hw``, a line stamped above every time the device clock can show
(``N + 1 s`` plus the host time since the clock read, plus 1 s of slack) was
logged before the step. At most the old ``hw``, it was read before: logd
returns it on every request above the new times, so it is dropped
(``deduplicated``). Above the old ``hw``, it was logged between the last read
and the step: it is delivered once, and later replays drop it like the lines
read before (by time, and by content at the highest such time, so lines of
one microsecond are told apart). Neither kind moves ``hw`` or the window:
they restart at the first new line the device clock can have stamped.
Without these rules every later reconnect would deliver the old lines again
and re-anchor once more.

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
unread line of another buffer is kept). After a re-anchor, when a
connection ends inside the lines logged between the last read and the step,
a later one drops as a replay an unread such line stamped below the highest
one delivered (lines out of time order).

Costs per line (design risk 16): a timestamp conversion, a deque append and
amortized O(1) evictions (a line read below ``hw`` goes to the heap:
O(log n)); in a screen phase one dict lookup more; a chatty check is one
string comparison unless the tag is ``chatty``. A resume costs O(len(W))
once, to count the window into the phase's dict.
"""

from __future__ import annotations

import heapq
import re
from collections import deque
from dataclasses import dataclass
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


@dataclass(slots=True, eq=False)
class _Phase:
    """The screen phase of one connection (design 5.6 steps 2-3).

    ``floor``: lines below it are ``below_cursor``. ``expected``: the window's
    elements at or above ``floor``, with their occurrence counts. The phase
    ends at the first line that is not a match and is above ``end_above``, or
    when a match leaves ``remaining`` at 0; a re-anchored one only drops its
    ``below_cursor`` rule there and keeps matching. ``position`` is ``hw`` for
    the ring check (``kind == "resume"`` only), ``at_position`` its unmatched
    elements. ``loose``: the expected times of each raw text, built for the
    first unparsed line that has no time to inherit.
    """

    kind: _Kind
    floor: int
    end_above: int
    expected: dict[tuple[int, str], int]
    remaining: int
    position: int
    at_position: int
    shortened: bool
    covered: bool = False  # a replayed chatty notice at or below hw
    begun: bool = False  # a line was screened in this phase
    loose: dict[str, list[int]] | None = None


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
        "_floor",
        "_gapped",
        "_hw",
        "_keep",
        "_last_read_at",
        "_last_ts",
        "_late",
        "_max_bytes",
        "_max_lines",
        "_noted",
        "_overlap",
        "_pending_restart",
        "_phase",
        "_plan",
        "_prev_ts",
        "_raised",
        "_raised_at",
        "_replay_at",
        "_replay_ts",
        "_since",
        "_start",
        "_started",
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
        # command was issued (host-monotonic seconds).
        self._clock: int | None = None
        self._clock_at = 0.0
        self._plan = _Plan(None, None)
        self._phase: _Phase | None = None
        self._gapped: int | None = None  # the hw a clock_moved_back Gap named
        # After a re-anchor, until the device clock passes it: the highest
        # timestamp read before the step.
        self._ceiling: int | None = None
        # The lines logged between the last read and the step, delivered
        # after a re-anchor: the highest time, and the count of each raw
        # text at it; the next connection's copy of both.
        self._raised: int | None = None
        self._raised_at: dict[str, int] = {}
        self._replay_ts: int | None = None
        self._replay_at: dict[str, int] = {}
        # A re-anchored connection restarts hw and the window at its first
        # new line.
        self._pending_restart = False
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
        self._clock, self._clock_at = clock, issued
        if self._ceiling is not None and clock >= self._ceiling:
            self._ceiling = None  # the device clock passed the old times
            self._raised, self._raised_at = None, {}
        if not self._started:
            # since="now" before the marker: every attempt starts over at its
            # own clock with a new marker; nothing read so far is the capture's,
            # so a cap INFO queued before the marker no longer holds.
            self._reset_window()
            self.out = [item for item in self.out if not isinstance(item, str)]
            self._plan = _Plan(None, clock - US_PER_SECOND)
            return None
        if self._start is None:
            self._start = clock  # since="backlog": S is the first clock read
        hw = self._hw
        if hw is None or not self._keep:
            # Nothing read yet: the resume uses the initial position (step 4).
            kind: _Kind | None = "start" if self._floor is not None else None
            self._plan = _Plan(kind, self._floor)
            return None
        if clock < hw - max(self._overlap, CLOCK_RESOLUTION):
            last = self._last_read_at
            outage = 0.0 if last is None else max(issued - last, 0.0)
            request = clock - round(outage * US_PER_SECOND) - self._overlap
            self._plan = _Plan("reanchor", request)
            if self._gapped == hw:
                return None  # an earlier attempt reported this step already
            self._gapped = hw
            return Gap(
                after=moment(hw),
                before=moment(clock),
                lost=None,
                reason="clock_moved_back",
            )
        request = hw - self._overlap
        shortened = False
        evicted = self._cap_evicted
        if evicted is not None and evicted + 1 > request:
            request = evicted + 1
            shortened = True
        if self._floor is not None and self._floor > request:
            request = self._floor
        self._plan = _Plan("resume", request, shortened=shortened)
        return None

    def position(self) -> Position | None:
        """The ``-T`` of the attempt whose clock was read last; opens its phase."""
        plan = self._plan
        self._noted = False
        self._last_ts = None
        self._pending_restart = False
        raised = self._raised
        if raised is not None and self._ceiling is not None:
            # Below the highest pre-step line delivered, a pre-step line is a
            # replay; at it, the ones delivered are told apart by content.
            self._ceiling = max(self._ceiling, raised - 1)
        self._replay_ts, self._replay_at = raised, dict(self._raised_at)
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
                self._add(ts, entry.raw, nbytes)

    def marker(self, entry: LogEntry, nbytes: int, now: float) -> None:
        """The start marker was read: ``S`` is its timestamp (design 5.6)."""
        self.before_start(entry, nbytes, now)
        self._started = True
        self._floor = self._plan.request
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
        if entry.parsed and when.tzinfo is not None:
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
        step = None if self._ceiling is None else self._stepped(ts, entry.raw, now)
        phase = self._phase
        if phase is not None:
            verdict = self._screened(phase, entry, ts, settled=step is not None)
            if verdict is not None:
                return verdict
        if step == "old":
            return "deduplicated"  # logged before the clock moved back
        if entry.tag == CHATTY_TAG:
            self._chatty(entry, ts, prev)
        if step == "unread":
            # Delivered once; it moves neither hw nor the window, which belong
            # to the new clock's times.
            self._pre_step(ts, entry.raw)
            return "new"
        if self._keep:
            if self._pending_restart:
                # The re-anchored connection's first new line: hw and the
                # window restart from here (design 5.6).
                self._pending_restart = False
                self._reset_window()
            self._add(ts, entry.raw, nbytes)
        return "new"

    def _stepped(
        self, ts: int, raw: str, now: float
    ) -> Literal["old", "unread"] | None:
        """After a re-anchor: whether a line was logged before the clock moved back.

        Stamped above every time the device clock can show, it was: read
        before (at most the old hw, or delivered after the re-anchor:
        ``"old"``), or logged after the last read and before the step
        (``"unread"``). None: the new clock can have stamped it.
        """
        if ts <= self._newest(now):
            return None
        ceiling = self._ceiling
        if (ceiling is not None and ts <= ceiling) or self._replayed(ts, raw):
            return "old"
        return "unread"

    def _chatty(self, entry: LogEntry, ts: int, prev: int | None) -> None:
        """Count a new chatty notice: backlog below S, else a reported loss."""
        lost = chatty_lines(entry)
        if lost is None:
            return
        start = self._start
        if start is not None and ts < start:
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
            return _Phase(kind, request, request - 1, {}, 0, 0, 0, shortened=False)
        hw = self._hw
        assert hw is not None  # noqa: S101  (a resume has read lines)
        expected: dict[tuple[int, str], int] = {}
        at_position = 0
        for ts, raw, _size in chain(self._window, self._late):
            if ts >= request:
                key = (ts, raw)
                expected[key] = expected.get(key, 0) + 1
                at_position += ts == hw
        remaining = sum(expected.values())
        if kind == "reanchor":
            clock = self._clock
            assert clock is not None  # noqa: S101  (set by clock())
            self._ceiling = hw if self._ceiling is None else max(hw, self._ceiling)
            self._pending_restart = True
            # The new times lie below the old start: later requests and the
            # chatty notices go by the re-anchored request instead.
            if self._floor is not None:
                self._floor = min(self._floor, request)
            if self._start is not None:
                self._start = min(self._start, request)
            # The replay is over at the first new line logged after the
            # clock read.
            end_above = clock + CLOCK_RESOLUTION - 1
            return _Phase(
                kind, request, end_above, expected, remaining, hw, 0, shortened=False
            )
        return _Phase(
            kind,
            request,
            hw,
            expected,
            remaining,
            hw,
            at_position,
            shortened=plan.shortened,
        )

    def _screened(
        self, phase: _Phase, entry: LogEntry, ts: int, *, settled: bool
    ) -> Verdict | None:
        """The phase's verdict on a line; None: not the phase's to drop.

        ``settled``: a line logged before the clock moved back, which the
        caller drops or delivers; it never ends the phase.
        """
        self._begin(phase)
        if ts < phase.floor:
            return "below_cursor"
        key = (ts, entry.raw)
        count = phase.expected.get(key)
        if count:
            self._matched(phase, key, count)
            return "deduplicated"
        if settled:
            return None
        if ts > phase.end_above:
            self._passed(phase, ts)
            return None
        if phase.kind == "resume" and chatty_lines(entry) is not None:
            phase.covered = True  # logd pruned lines we read: it holds the position
            return "deduplicated"
        return None

    def _passed(self, phase: _Phase, ts: int) -> None:
        """The first new line above the phase's end, at ``ts``.

        A resume's phase ends with the ring check (design 5.6 step 3). A
        re-anchored one only drops its below_cursor rule: the old window stays
        matched for the whole connection, also above the clock read.
        """
        if phase.kind == "reanchor":
            phase.floor, phase.end_above = _LOWEST, _HIGHEST
            return
        self._phase = None
        if phase.kind == "resume" and phase.at_position and not phase.covered:
            self.out.append(
                Gap(
                    after=moment(phase.position),
                    before=moment(ts),
                    lost=None,
                    reason="ring_rotated",
                )
            )

    def _begin(self, phase: _Phase) -> None:
        if not phase.begun:
            phase.begun = True
            if phase.shortened:
                self.overlap_shortened += 1  # this resume took place

    def _matched(self, phase: _Phase, key: tuple[int, str], count: int) -> None:
        """A replayed element of the window: one occurrence fewer."""
        phase.expected[key] = count - 1
        phase.remaining -= 1
        if key[0] == phase.position:
            phase.at_position -= 1
        if not phase.remaining:
            self._phase = None

    def _unstamped(self, phase: _Phase, entry: LogEntry) -> Verdict:
        """An unparsed line before any parsed one of a screened connection.

        It has no time to inherit, but the window holds it under the time it
        inherited when it was read: a raw text the phase still expects is a
        replay, matched at its lowest expected time, which the next unparsed
        lines inherit.
        """
        self._begin(phase)
        loose = phase.loose
        if loose is None:
            loose = phase.loose = {}
            for (ts, raw), count in phase.expected.items():
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
        return "new"

    def _replayed(self, ts: int, raw: str) -> bool:
        """A pre-step line at the highest time delivered: one of those delivered."""
        if ts != self._replay_ts:
            return False
        count = self._replay_at.get(raw)
        if not count:
            return False
        self._replay_at[raw] = count - 1
        return True

    def _pre_step(self, ts: int, raw: str) -> None:
        """A line logged between the last read and the step was delivered."""
        raised = self._raised
        if raised is None or ts > raised:
            self._raised, self._raised_at = ts, {raw: 1}
        elif ts == raised:
            self._raised_at[raw] = self._raised_at.get(raw, 0) + 1

    def _newest(self, now: float) -> int:
        """The newest time the device clock can show at ``now`` (after a re-anchor)."""
        clock = self._clock
        assert clock is not None  # noqa: S101  (a re-anchor read the clock)
        elapsed = max(now - self._clock_at, 0.0)
        return clock + CLOCK_RESOLUTION + CLOCK_SLACK + round(elapsed * US_PER_SECOND)

    # -- the window --------------------------------------------------------------------

    def _stamp(self, entry: LogEntry) -> int | None:
        """The line's timestamp for windowing; unparsed lines inherit the last one."""
        when = entry.timestamp
        if entry.parsed and when.tzinfo is not None:
            ts = (when - _EPOCH) // _MICROSECOND
            self._last_ts = ts
            return ts
        return self._last_ts

    def _add(self, ts: int, raw: str, nbytes: int) -> None:
        """A line read: move hw, window it, evict by time and by the caps."""
        hw = self._hw
        window, late = self._window, self._late
        if hw is None or ts >= hw:
            hw = self._hw = ts
            window.append((ts, raw, nbytes))
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
            self._evict()

    def _evict(self) -> None:
        """The caps: evict the smallest timestamps first (design 5.6), never hw.

        Remembers the highest evicted time. The lines at hw stay, so the
        request stays at or below hw and the resume can check the ring.
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
                break  # only lines stamped hw are left
            self._bytes -= size
            if self._cap_evicted is None or evicted > self._cap_evicted:
                self._cap_evicted = evicted
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
