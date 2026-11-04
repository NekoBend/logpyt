"""Tests for consecutive grouping, ported from the 2.x LogGrouper to GroupBy."""

import contextlib
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from logpyt import GroupBy
from logpyt._core.grouping import Grouper
from logpyt.models import LogEntry, LogGroup

type Mode = Literal["consecutive", "windowed"]


@pytest.fixture
def base_time() -> datetime:
    """Provide a base time for tests."""
    return datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)


def create_entry(
    timestamp: datetime, pid: int = 100, tid: int = 200, message: str = "msg"
) -> LogEntry:
    """Helper to create a log entry."""
    return LogEntry(
        timestamp=timestamp,
        pid=pid,
        tid=tid,
        level="D",
        tag="Test",
        message=message,
        raw="",
    )


def test_grouper_basic_grouping(base_time: datetime) -> None:
    """Test basic grouping by PID/TID within threshold."""
    grouper = Grouper(GroupBy(keys=("pid", "tid"), gap_ms=100))

    e1 = create_entry(base_time, message="1")
    e2 = create_entry(base_time + timedelta(milliseconds=50), message="2")
    # 100 ms after e2: the gap is between an entry and the group's newest one.
    e3 = create_entry(base_time + timedelta(milliseconds=150), message="3")

    assert grouper.add(e1, 0.0) == []  # Buffered
    assert grouper.add(e2, 0.0) == []  # 50 ms <= 100 ms: same group
    assert grouper.add(e3, 0.0) == []  # 100 ms <= 100 ms: same group

    final = grouper.flush()
    assert len(final) == 1
    group = final[0]
    assert isinstance(group, LogGroup)
    assert group.key == (100, 200)
    assert group.reason == "end"
    assert [entry.message for entry in group] == ["1", "2", "3"]


def test_grouper_threshold_exceeded(base_time: datetime) -> None:
    """Test grouping breaks when threshold is exceeded."""
    grouper = Grouper(GroupBy(keys=("pid",), gap_ms=100))

    e1 = create_entry(base_time, message="1")
    e2 = create_entry(
        base_time + timedelta(milliseconds=200), message="2"
    )  # Diff 200 > 100

    grouper.add(e1, 0.0)
    res = grouper.add(e2, 0.0)

    # Should emit the first group (e1) and buffer e2
    assert len(res) == 1
    assert [entry.message for entry in res[0]] == ["1"]
    assert res[0].reason == "gap"

    # Flush remaining
    final = grouper.flush()
    assert len(final) == 1
    assert [entry.message for entry in final[0]] == ["2"]


def test_grouper_key_change(base_time: datetime) -> None:
    """Test grouping breaks when key changes."""
    grouper = Grouper(GroupBy(keys=("pid",), gap_ms=1000))

    e1 = create_entry(base_time, pid=100, message="1")
    e2 = create_entry(base_time + timedelta(milliseconds=10), pid=101, message="2")

    grouper.add(e1, 0.0)
    res = grouper.add(e2, 0.0)

    # Should emit e1 because PID changed
    assert len(res) == 1
    assert res[0][0].pid == 100
    assert res[0].reason == "key"

    final = grouper.flush()
    assert final[0][0].pid == 101


def test_grouper_max_group_size_force_flushes(base_time: datetime) -> None:
    """A hot key cannot grow one group past max_group_size; it force-flushes."""
    grouper = Grouper(GroupBy(keys=("pid",), gap_ms=10000, max_group_size=3))

    emitted: list[LogGroup] = []
    for i in range(7):
        entry = create_entry(base_time + timedelta(milliseconds=i), message=str(i))
        emitted.extend(grouper.add(entry, 0.0))

    # 7 same-key entries within threshold, cap 3 -> two groups of 3 auto-flushed.
    assert [len(g) for g in emitted] == [3, 3]
    assert {g.reason for g in emitted} == {"size"}

    # The 7th entry remains buffered until an explicit flush.
    final = grouper.flush()
    assert len(final) == 1
    assert len(final[0]) == 1


_MODES: list[Mode] = ["consecutive", "windowed"]


@contextlib.contextmanager
def _host_zone(zone: str) -> Iterator[None]:
    """Set the host-local zone to the POSIX TZ string ``zone`` for the block.

    The grouping engine reads the host zone while it groups, so the entries
    must be built and grouped inside the block. A pinned zone shows a missing
    conversion on a UTC host too, such as a CI runner. Windows has no
    ``time.tzset()``: there the block runs in the host zone, where the tests'
    outcomes hold as well, only without the pinned zone's sharpness.
    """
    if sys.platform == "win32":
        yield
    else:
        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("TZ", zone)
                time.tzset()
                yield
        finally:
            time.tzset()


@pytest.mark.parametrize("mode", _MODES)
def test_grouper_takes_aware_and_naive_timestamps_in_one_stream(mode: Mode) -> None:
    """A zoned line (aware) next to an unparsed one (naive host-local) groups.

    The naive time is read as host-local, so the same moment in both forms is
    0 ms apart; the host zone is EST5, five hours behind UTC.
    """
    with _host_zone("EST5"):
        aware = datetime(2026, 11, 4, 4, 21, 44, tzinfo=UTC)
        naive = aware.astimezone().replace(tzinfo=None)
        entries = [
            _mixed_entry(aware),
            _mixed_entry(naive),
            _mixed_entry(aware + timedelta(milliseconds=10)),
        ]
        spec = GroupBy(keys=("tag",), gap_ms=1000, mode=mode)

        emitted = list(spec.groups(entries))

    assert [list(group) for group in emitted] == [entries]


@pytest.mark.parametrize("mode", _MODES)
def test_grouper_takes_a_naive_first_timestamp_then_aware(mode: Mode) -> None:
    """An aware entry after a naive one groups; the host zone is EST5."""
    with _host_zone("EST5"):
        aware = datetime(2026, 11, 4, 4, 21, 44, tzinfo=UTC)
        naive = aware.astimezone().replace(tzinfo=None)
        entries = [_mixed_entry(naive), _mixed_entry(aware), _mixed_entry(naive)]
        spec = GroupBy(keys=("tag",), gap_ms=1000, mode=mode)

        emitted = list(spec.groups(entries))

    assert [list(group) for group in emitted] == [entries]


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "change",
    [
        datetime(2026, 11, 1, 6, 0, tzinfo=UTC),  # 02:00 EDT becomes 01:00 EST
        datetime(2026, 3, 8, 7, 0, tzinfo=UTC),  # 02:00 EST becomes 03:00 EDT
    ],
    ids=["fall-back", "spring-forward"],
)
def test_aware_entries_after_a_naive_one_ignore_host_dst(
    mode: Mode, change: datetime
) -> None:
    """Aware entries 20 ms apart across a DST change of the host zone group.

    A naive first entry (a divider) puts the run on wall-clock time until the
    first aware entry; from then on aware entries are compared exactly, so
    the host's one-hour wall-clock jump splits nothing.
    """
    with _host_zone("EST5EDT,M3.2.0,M11.1.0"):
        step = timedelta(milliseconds=20)
        aware = [change - step, change, change + step]
        divider = _mixed_entry(aware[0].astimezone().replace(tzinfo=None), pid=9)
        entries = [divider, *(_mixed_entry(ts) for ts in aware)]
        spec = GroupBy(keys=("pid",), gap_ms=100, mode=mode)

        emitted = list(spec.groups(entries))

    assert [list(group) for group in emitted] == [entries[:1], entries[1:]]


@pytest.mark.parametrize("zone", ["EST5", "JST-9"], ids=["behind-utc", "ahead-utc"])
def test_windowed_groups_expire_on_the_utc_timeline_after_the_switch(
    zone: str,
) -> None:
    """The first aware entry moves the open naive groups and their expiries.

    In a zone ahead of UTC the naive expiry is later than the true one, so a
    heap entry that was not rebuilt would keep the naive group open.
    """
    with _host_zone(zone):
        start = datetime(2026, 11, 4, 4, 21, 44, tzinfo=UTC)
        grouper = Grouper(GroupBy(keys=("pid",), gap_ms=100, mode="windowed"))
        grouper.add(_mixed_entry(start.astimezone().replace(tzinfo=None)), 0.0)

        closed = grouper.add(
            _mixed_entry(start + timedelta(milliseconds=200), pid=2), 0.0
        )

    assert [(group.key, group.reason) for group in closed] == [((1,), "gap")]


def _mixed_entry(timestamp: datetime, pid: int = 1) -> LogEntry:
    return LogEntry(
        timestamp=timestamp, pid=pid, tid=pid, level="I", tag="T", message="m", raw="m"
    )
