"""Windowed grouping, ported from the 2.x WindowedLogGrouper to GroupBy."""

import datetime
from typing import Any

from logpyt import GroupBy
from logpyt._core.grouping import Grouper
from logpyt.models import LogEntry, LogGroup


def create_log_entry(ts, pid, tid, msg):
    return LogEntry(
        timestamp=ts,
        pid=pid,
        tid=tid,
        level="D",
        tag="Tag",
        message=msg,
        raw=f"{ts} {pid} {tid} D Tag: {msg}",
    )


def _windowed(**changes: Any) -> Grouper:  # noqa: ANN401  (any GroupBy keyword)
    fields: dict[str, Any] = {"keys": ("pid", "tid"), "mode": "windowed", **changes}
    return Grouper(GroupBy(**fields))


def test_sliding_window_correctness():
    """Verify that groups extend correctly with lazy heap updates."""
    grouper = _windowed(gap_ms=100.0)

    start_ts = datetime.datetime.now(datetime.UTC).replace(microsecond=0, tzinfo=None)
    pid, tid = 100, 200

    # 1. Start a group at T=0
    # Expiry should be T=100
    e1 = create_log_entry(start_ts, pid, tid, "msg1")
    emitted = grouper.add(e1, 0.0)
    assert len(emitted) == 0

    # 2. Update at T=50.
    # New expiry should be T=150.
    # The heap still has (100, seq, key): it is replaced lazily.
    e2 = create_log_entry(
        start_ts + datetime.timedelta(milliseconds=50), pid, tid, "msg2"
    )
    emitted = grouper.add(e2, 0.0)
    assert len(emitted) == 0

    # 3. Update at T=120.
    # Current TS (120) > Heap Expiry (100).
    # The expiry loop pops (100, seq, key), finds the group's real expiry
    # (150) > 120, and re-pushes it. Then T=120 joins the group.
    e3 = create_log_entry(
        start_ts + datetime.timedelta(milliseconds=120), pid, tid, "msg3"
    )
    emitted = grouper.add(e3, 0.0)

    # Should NOT flush prematurely
    assert len(emitted) == 0

    # 4. Wait until T=300 (gap of 180ms from T=120).
    # Should flush because T=120 + 100 = 220 < 300.
    e4 = create_log_entry(
        start_ts + datetime.timedelta(milliseconds=300), pid, tid, "msg4"
    )
    emitted = grouper.add(e4, 0.0)

    assert len(emitted) == 1
    group = emitted[0]
    assert isinstance(group, LogGroup)
    assert group.reason == "gap"
    assert [entry.message for entry in group] == ["msg1", "msg2", "msg3"]


def test_windowed_grouper_max_group_size_force_flushes():
    """A hot key cannot grow one window past max_group_size; it force-flushes."""
    grouper = _windowed(gap_ms=100000.0, max_group_size=3)
    start_ts = datetime.datetime.now(datetime.UTC).replace(microsecond=0, tzinfo=None)

    emitted: list[LogGroup] = []
    for i in range(7):
        entry = create_log_entry(
            start_ts + datetime.timedelta(milliseconds=i), 100, 200, f"msg{i}"
        )
        emitted.extend(grouper.add(entry, 0.0))

    # 7 same-key entries within threshold, cap 3 -> two full windows auto-flushed.
    assert [len(g) for g in emitted] == [3, 3]
    assert {g.reason for g in emitted} == {"size"}

    # The 7th entry remains buffered until an explicit flush.
    final = grouper.flush()
    assert len(final) == 1
    assert len(final[0]) == 1


def test_windowed_grouper_lru_eviction_on_max_groups():
    """A 3rd distinct key over max_groups=2 evicts (flushes) the LRU key."""
    grouper = Grouper(
        GroupBy(keys=("tid",), gap_ms=1000.0, mode="windowed", max_groups=2)
    )

    start_ts = datetime.datetime.now(datetime.UTC).replace(microsecond=0, tzinfo=None)

    # Three DISTINCT keys, all within threshold of each other so the heap-based
    # timeout never fires; any flush must come from LRU eviction alone.
    e_a = create_log_entry(start_ts, pid=1, tid=10, msg="A")
    e_b = create_log_entry(
        start_ts + datetime.timedelta(milliseconds=10), pid=1, tid=20, msg="B"
    )
    e_c = create_log_entry(
        start_ts + datetime.timedelta(milliseconds=20), pid=1, tid=30, msg="C"
    )

    assert grouper.add(e_a, 0.0) == []
    assert grouper.add(e_b, 0.0) == []

    # Inserting the 3rd distinct key exceeds max_groups=2 and must evict the LRU
    # key (tid=10, "A"), emitting its group.
    emitted = grouper.add(e_c, 0.0)

    assert len(emitted) == 1
    group = emitted[0]
    assert group.reason == "evicted"
    assert group.key == (10,)
    assert [entry.message for entry in group] == ["A"]

    # The two most-recent keys remain open.
    assert set(grouper._open) == {(20,), (30,)}


def test_windowed_grouper_limits_heap_growth_for_hot_key_updates():
    """Repeated updates for one key should not grow heap without bound."""
    grouper = _windowed(gap_ms=100.0, max_groups=64)

    start_ts = datetime.datetime.now(datetime.UTC).replace(microsecond=0, tzinfo=None)

    for i in range(300):
        entry = create_log_entry(
            start_ts + datetime.timedelta(milliseconds=i),
            pid=100,
            tid=200,
            msg=f"hot-{i}",
        )
        grouper.add(entry, 0.0)

    assert grouper.open_groups == 1
    assert len(grouper._heap) == 1  # one entry per open group, replaced lazily
