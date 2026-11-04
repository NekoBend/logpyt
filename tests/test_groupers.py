"""Tests for log groupers."""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from logpyt.groupers import LogGrouper, WindowedLogGrouper
from logpyt.models import LogEntry


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
    grouper = LogGrouper(by=("pid", "tid"), threshold_ms=100, emit_mode="group")

    e1 = create_entry(base_time, message="1")
    e2 = create_entry(base_time + timedelta(milliseconds=50), message="2")
    e3 = create_entry(
        base_time + timedelta(milliseconds=150), message="3"
    )  # Still within 100ms of e2? No, threshold is between consecutive logs.

    # Process e1
    res1 = grouper.process(e1)
    assert res1 == []  # Buffered

    # Process e2 (diff 50ms <= 100ms) -> Same group
    res2 = grouper.process(e2)
    assert res2 == []  # Buffered

    # Process e3 (diff 100ms <= 100ms from e2) -> Same group
    res3 = grouper.process(e3)
    assert res3 == []

    # Flush
    final = grouper.flush()
    assert len(final) == 1
    group = cast("list[LogEntry]", final[0])
    assert isinstance(group, list)
    assert len(group) == 3
    assert group[0].message == "1"
    assert group[1].message == "2"
    assert group[2].message == "3"


def test_grouper_threshold_exceeded(base_time: datetime) -> None:
    """Test grouping breaks when threshold is exceeded."""
    grouper = LogGrouper(by=("pid",), threshold_ms=100, emit_mode="group")

    e1 = create_entry(base_time, message="1")
    e2 = create_entry(
        base_time + timedelta(milliseconds=200), message="2"
    )  # Diff 200 > 100

    grouper.process(e1)
    res = grouper.process(e2)

    # Should emit the first group (e1) and buffer e2
    assert len(res) == 1
    group1 = cast("list[LogEntry]", res[0])
    assert len(group1) == 1
    assert group1[0].message == "1"

    # Flush remaining
    final = grouper.flush()
    assert len(final) == 1
    group2 = cast("list[LogEntry]", final[0])
    assert group2[0].message == "2"


def test_grouper_key_change(base_time: datetime) -> None:
    """Test grouping breaks when key changes."""
    grouper = LogGrouper(by=("pid",), threshold_ms=1000, emit_mode="group")

    e1 = create_entry(base_time, pid=100, message="1")
    e2 = create_entry(base_time + timedelta(milliseconds=10), pid=101, message="2")

    grouper.process(e1)
    res = grouper.process(e2)

    # Should emit e1 because PID changed
    assert len(res) == 1
    group1 = cast("list[LogEntry]", res[0])
    assert group1[0].pid == 100

    final = grouper.flush()
    group2 = cast("list[LogEntry]", final[0])
    assert group2[0].pid == 101


def test_grouper_emit_mode_entry(base_time: datetime) -> None:
    """Test emit_mode='entry'."""
    grouper = LogGrouper(by=("pid",), threshold_ms=100, emit_mode="entry")

    e1 = create_entry(base_time, message="1")
    e2 = create_entry(base_time + timedelta(milliseconds=200), message="2")

    grouper.process(e1)
    res = grouper.process(e2)

    # Should emit e1 as a single item, not a list
    assert len(res) == 1
    entry = cast("LogEntry", res[0])
    assert isinstance(entry, LogEntry)
    assert entry.message == "1"


def test_grouper_max_group_size_force_flushes(base_time: datetime) -> None:
    """A hot key cannot grow one group past max_group_size; it force-flushes."""
    grouper = LogGrouper(
        by=("pid",), threshold_ms=10000, emit_mode="group", max_group_size=3
    )

    emitted: list[LogEntry | list[LogEntry]] = []
    for i in range(7):
        entry = create_entry(base_time + timedelta(milliseconds=i), message=str(i))
        emitted.extend(grouper.process(entry))

    # 7 same-key entries within threshold, cap 3 -> two groups of 3 auto-flushed.
    groups = [g for g in emitted if isinstance(g, list)]
    assert len(groups) == 2
    assert all(len(g) == 3 for g in groups)

    # The 7th entry remains buffered until an explicit flush.
    final = grouper.flush()
    assert len(final) == 1
    assert len(cast("list[LogEntry]", final[0])) == 1


@pytest.mark.parametrize("grouper_cls", [LogGrouper, WindowedLogGrouper])
def test_grouper_takes_aware_and_naive_timestamps_in_one_stream(
    grouper_cls: type[LogGrouper],
) -> None:
    """A zoned line (aware) next to an unparsed one (naive host-local) groups.

    The naive time is read as host-local, so the same moment in both forms is
    0 ms apart, on any host zone.
    """
    aware = datetime(2026, 11, 4, 4, 21, 44, tzinfo=UTC)
    naive = aware.astimezone().replace(tzinfo=None)
    entries = [
        _mixed_entry(aware),
        _mixed_entry(naive),
        _mixed_entry(aware + timedelta(milliseconds=10)),
    ]
    grouper = grouper_cls(by=["tag"], threshold_ms=1000, emit_mode="group")

    emitted = [item for entry in entries for item in grouper.process(entry)]
    emitted += grouper.flush()

    assert emitted == [entries]


def _mixed_entry(timestamp: datetime) -> LogEntry:
    return LogEntry(
        timestamp=timestamp, pid=1, tid=1, level="I", tag="T", message="m", raw="m"
    )
