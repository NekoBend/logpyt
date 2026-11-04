from datetime import UTC, datetime, timedelta

from logpyt import GroupBy
from logpyt._core.grouping import Grouper
from logpyt.models import LogEntry


def test_interleaved_groups() -> None:
    grouper = Grouper(GroupBy(keys=("tag",), gap_ms=100.0, mode="windowed"))
    base_time = datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)

    # Create interleaved entries
    # Group A: Tag1
    # Group B: Tag2

    e1 = LogEntry(
        timestamp=base_time,
        pid=1,
        tid=1,
        level="D",
        tag="Tag1",
        message="Msg1",
        raw="raw1",
    )
    e2 = LogEntry(
        timestamp=base_time + timedelta(milliseconds=10),
        pid=1,
        tid=1,
        level="D",
        tag="Tag2",
        message="Msg2",
        raw="raw2",
    )
    e3 = LogEntry(
        timestamp=base_time + timedelta(milliseconds=20),
        pid=1,
        tid=1,
        level="D",
        tag="Tag1",
        message="Msg3",
        raw="raw3",
    )
    e4 = LogEntry(
        timestamp=base_time + timedelta(milliseconds=30),
        pid=1,
        tid=1,
        level="D",
        tag="Tag2",
        message="Msg4",
        raw="raw4",
    )

    # Process entries
    res1 = grouper.add(e1, 0.0)
    assert len(res1) == 0

    res2 = grouper.add(e2, 0.0)
    assert len(res2) == 0

    res3 = grouper.add(e3, 0.0)
    assert len(res3) == 0

    res4 = grouper.add(e4, 0.0)
    assert len(res4) == 0

    # Now trigger a timeout for Tag1
    # e5 is 150ms after e3 (Tag1's last entry)
    e5 = LogEntry(
        timestamp=base_time + timedelta(milliseconds=170),
        pid=1,
        tid=1,
        level="D",
        tag="Tag1",
        message="Msg5",
        raw="raw5",
    )

    res5 = grouper.add(e5, 0.0)
    # Should flush BOTH Tag1 and Tag2 groups because e5 is far ahead of both,
    # in expiry order: Tag1 (20 + 100 ms) before Tag2 (30 + 100 ms).
    assert len(res5) == 2
    assert {group.reason for group in res5} == {"gap"}

    # Tag1 Group
    assert res5[0].key == ("Tag1",)
    assert [entry.message for entry in res5[0]] == ["Msg1", "Msg3"]

    # Tag2 Group
    assert res5[1].key == ("Tag2",)
    assert [entry.message for entry in res5[1]] == ["Msg2", "Msg4"]

    # Flush remaining (e5)
    res_flush = grouper.flush()
    assert len(res_flush) == 1
    assert res_flush[0].reason == "end"
    assert res_flush[0][0].message == "Msg5"
