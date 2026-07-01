from datetime import UTC, datetime, timedelta

from logpyt.groupers import WindowedLogGrouper
from logpyt.models import LogEntry


def test_interleaved_groups() -> None:  # noqa: PLR0914 - timing scenario keeps all entries/results in one scope
    grouper = WindowedLogGrouper(by=["tag"], threshold_ms=100.0)
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
    res1 = grouper.process(e1)
    assert len(res1) == 0

    res2 = grouper.process(e2)
    assert len(res2) == 0

    res3 = grouper.process(e3)
    assert len(res3) == 0

    res4 = grouper.process(e4)
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

    res5 = grouper.process(e5)
    # Should flush BOTH Tag1 and Tag2 groups because e5 is far ahead of both.
    assert len(res5) == 2

    # Sort to ensure order
    groups = sorted(res5, key=lambda g: g[0].tag)
    groups_list: list[list[LogEntry]] = [g for g in groups if isinstance(g, list)]

    # Tag1 Group
    assert groups_list[0][0].tag == "Tag1"
    assert len(groups_list[0]) == 2
    assert groups_list[0][0].message == "Msg1"
    assert groups_list[0][1].message == "Msg3"

    # Tag2 Group
    assert groups_list[1][0].tag == "Tag2"
    assert len(groups_list[1]) == 2
    assert groups_list[1][0].message == "Msg2"
    assert groups_list[1][1].message == "Msg4"

    # Flush remaining (e5)
    res_flush = grouper.flush()
    assert len(res_flush) == 1
    flush_group = res_flush[0]
    assert isinstance(flush_group, list)
    assert flush_group[0].message == "Msg5"
