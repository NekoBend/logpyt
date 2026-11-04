"""The per-call parse session: yearless-date resolution and host time.

Parsers are stateless (design 2.8). Everything that depends on the lines read
so far lives in a :class:`ParseContext`, and every read of a file or every
connection of a stream gets a fresh one, so reading a file twice gives the same
timestamps (F6-1).
"""

from __future__ import annotations

import calendar
from datetime import MAXYEAR, MINYEAR, UTC, datetime, timedelta

__all__ = ["ParseContext"]

# A date without a year is placed in a year next to the previous line's: the
# candidates are that year and the years around it (D-12).
_NEIGHBOUR_YEARS = (-1, 0, 1)
# Feb 29 exists only in leap years, so its candidates are the leap years
# within this many years of the reference (D-12).
_LEAP_SEARCH_YEARS = 4
# The first line looks back further for Feb 29 (A.15): leap years are up to
# eight years apart around a century that is no leap year (2096, 2104).
_FIRST_LINE_LEAP_SEARCH_YEARS = 8
# Half a year: a candidate in the reference's own year that is this close is
# the nearest one without trying the neighbours (fast path).
_SAME_YEAR_SPAN = timedelta(days=182)
# The first yearless line is not placed later than the anchor plus this: the
# anchor (file mtime or the host clock) is when the lines were already written,
# and two days cover any offset between the device's and the host's time zone.
_ANCHOR_TOLERANCE = timedelta(days=2)


def host_now() -> datetime:
    """The host's local wall-clock time, as a naive datetime.

    The local UTC offset is looked up on every call (F6-4): a process that runs
    across a DST change gets the new offset, not the one of import time.
    """
    return datetime.now(UTC).astimezone().replace(tzinfo=None)


def _naive_local(moment: datetime) -> datetime:
    """``moment`` as a naive host-local wall-clock time."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def _local_or_wall_clock(moment: datetime) -> datetime:
    """``moment`` as naive host-local time, or its own wall clock when the
    host-local zone cannot represent it (an anchor near year 1 or 9999).
    """
    try:
        return _naive_local(moment)
    except (OverflowError, ValueError, OSError):
        return moment.replace(tzinfo=None)


def _candidate_years(
    month: int,
    day: int,
    year: int,
    spread: tuple[int, ...],
    leap_search: int = _LEAP_SEARCH_YEARS,
) -> list[int]:
    """The years a ``month``-``day`` date may be placed in, near ``year``."""
    if month == 2 and day == 29:
        span = range(year - leap_search, year + leap_search + 1)
        years = [y for y in span if calendar.isleap(y)]
    else:
        years = [year + delta for delta in spread]
    return [y for y in years if MINYEAR <= y <= MAXYEAR]


class ParseContext:
    """One parse session: the state a stateless parser may need between lines.

    A context resolves dates printed without a year (``-v threadtime`` without
    ``-v year``) and supplies the host time for lines that carry no timestamp.
    Create one per read of a file or per stream connection; a context must not
    be shared by two sessions that run at the same time.

    Year resolution (D-12, F6-1, F6-2):

    - The first yearless line is placed in ``year`` when it is given. Otherwise
      it gets the latest year that puts it no later than ``anchor`` (the file
      mtime offline; the host clock when ``anchor`` is None) plus two days,
      because the anchor is a moment after the lines were written.
    - Every later line gets the year, out of the previous line's year and the
      years before and after it, that puts it nearest to the previous line. A
      Dec 31 line after a Jan 1 line therefore stays in the old year, and an
      interleaved New Year never drifts.
    - Feb 29 is placed in the nearest leap year within four years.
    - Only years 1..9999 are candidates; a date with no valid candidate (for
      example ``02-30``) is not resolved, so the line does not parse.

    A line that carries its year (``-v year``) moves the reference too, so a
    yearless line after it is placed next to it.

    """

    __slots__ = ("_anchor", "_previous", "_year")

    def __init__(
        self, *, year: int | None = None, anchor: datetime | None = None
    ) -> None:
        """Start a parse session.

        Args:
            year: The year of the first line without a year (``read_file(year=)``).
                Takes precedence over ``anchor``.
            anchor: A moment after the first yearless line was written, such as
                the file's modification time. An aware datetime is converted to
                host-local time, the time base of yearless lines. None means the
                host clock when the first yearless line is parsed.

        Raises:
            ValueError: If ``year`` is outside 1..9999.
            TypeError: If ``year`` is not an int or ``anchor`` not a datetime.

        """
        if year is not None:
            if isinstance(year, bool) or not isinstance(year, int):
                msg = f"year must be an int, not {type(year).__name__}"
                raise TypeError(msg)
            if not MINYEAR <= year <= MAXYEAR:
                msg = f"year must be in {MINYEAR}..{MAXYEAR}, got {year}"
                raise ValueError(msg)
        if anchor is not None and not isinstance(anchor, datetime):
            msg = f"anchor must be a datetime, not {type(anchor).__name__}"
            raise TypeError(msg)
        self._year = year
        self._anchor = None if anchor is None else _local_or_wall_clock(anchor)
        self._previous: datetime | None = None

    @property
    def year(self) -> int | None:
        """The year of the first yearless line, or None."""
        return self._year

    @property
    def anchor(self) -> datetime | None:
        """The naive host-local moment the first yearless line is placed before.

        An aware anchor the host-local zone cannot represent keeps its wall
        clock.
        """
        return self._anchor

    def now(self) -> datetime:  # noqa: PLR6301  (per-session interface; no state yet)
        """The session's wall-clock time: host-local time, naive.

        Brief, process, tag and raw lines and the fallback entries of
        unrecognised lines get it as their timestamp, and the first yearless
        line is placed before it when there is neither ``year`` nor ``anchor``.
        It is computed on every call (F6-4), and it is the session's only
        clock read.
        """
        return host_now()

    def resolve_yearless(
        self,
        month: int,
        day: int,
        hour: int,
        minute: int,
        second: int,
        microsecond: int,
    ) -> datetime | None:
        """Place a date printed without a year in a year (see the class docs).

        Args:
            month: 1..12.
            day: 1..31.
            hour: 0..23.
            minute: 0..59.
            second: 0..59.
            microsecond: 0..999999.

        Returns:
            The naive timestamp, or None when no candidate year makes it a valid
            date. A None result leaves the session unchanged.

        """
        previous = self._previous
        if previous is not None:
            chosen = self._nearest(
                previous, month, day, hour, minute, second, microsecond
            )
        elif self._year is not None:
            chosen = self._in_year(
                self._year, month, day, hour, minute, second, microsecond
            )
        else:
            anchor = self._anchor if self._anchor is not None else self.now()
            chosen = self._before(anchor, month, day, hour, minute, second, microsecond)
        if chosen is not None:
            self._previous = chosen
        return chosen

    def observe(self, timestamp: datetime) -> None:
        """Make ``timestamp``, read from a line with a year, the reference.

        A parser calls this for lines that carry their year, so a yearless
        line that follows is placed next to them.
        """
        self._previous = timestamp.replace(tzinfo=None)

    # -- candidate selection ---------------------------------------------------------

    @staticmethod
    def _build(
        year: int, month: int, day: int, hour: int, minute: int, second: int, us: int
    ) -> datetime | None:
        try:
            return datetime(year, month, day, hour, minute, second, us)  # noqa: DTZ001  (yearless lines are naive device time)
        except ValueError:
            return None

    @classmethod
    def _nearest(
        cls,
        reference: datetime,
        month: int,
        day: int,
        hour: int,
        minute: int,
        second: int,
        us: int,
    ) -> datetime | None:
        """The candidate nearest to ``reference``; the earlier one on a tie."""
        if not (month == 2 and day == 29):
            same = cls._build(reference.year, month, day, hour, minute, second, us)
            if same is not None and abs(same - reference) <= _SAME_YEAR_SPAN:
                return same  # the other years are at least 183 days away
        best: datetime | None = None
        for year in _candidate_years(month, day, reference.year, _NEIGHBOUR_YEARS):
            candidate = cls._build(year, month, day, hour, minute, second, us)
            if candidate is not None and (
                best is None or abs(candidate - reference) < abs(best - reference)
            ):
                best = candidate
        return best

    @classmethod
    def _in_year(
        cls,
        year: int,
        month: int,
        day: int,
        hour: int,
        minute: int,
        second: int,
        us: int,
    ) -> datetime | None:
        """The first line with an explicit ``year``: that year, or for Feb 29 in
        a non-leap year the nearest leap year (the earlier one on a tie)."""
        exact = cls._build(year, month, day, hour, minute, second, us)
        if exact is not None or not (month == 2 and day == 29):
            return exact
        reference = cls._build(year, 2, 28, hour, minute, second, us)
        if reference is None:  # the time of day is impossible too
            return None
        return cls._nearest(reference, month, day, hour, minute, second, us)

    @classmethod
    def _before(
        cls,
        anchor: datetime,
        month: int,
        day: int,
        hour: int,
        minute: int,
        second: int,
        us: int,
    ) -> datetime | None:
        """The first line with an anchor: the latest candidate not later than
        the anchor plus the tolerance; the earliest one if all are later."""
        try:
            latest = anchor + _ANCHOR_TOLERANCE
        except OverflowError:
            latest = datetime.max  # noqa: DTZ901  (naive, like the candidates)
        candidates = [
            candidate
            for year in _candidate_years(
                month, day, anchor.year, _NEIGHBOUR_YEARS, _FIRST_LINE_LEAP_SEARCH_YEARS
            )
            if (candidate := cls._build(year, month, day, hour, minute, second, us))
            is not None
        ]
        if not candidates:
            return None
        before = [candidate for candidate in candidates if candidate <= latest]
        return max(before) if before else min(candidates)
