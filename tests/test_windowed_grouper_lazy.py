import datetime

from logpyt.groupers import WindowedLogGrouper
from logpyt.models import LogEntry


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


def test_sliding_window_correctness():
    """Verify that groups extend correctly with lazy heap updates."""
    threshold_ms = 100.0
    grouper = WindowedLogGrouper(
        by=["pid", "tid"], threshold_ms=threshold_ms, emit_mode="group"
    )

    start_ts = datetime.datetime.now(datetime.UTC).replace(microsecond=0, tzinfo=None)
    pid, tid = 100, 200

    # 1. Start a group at T=0
    # Expiry should be T=100
    e1 = create_log_entry(start_ts, pid, tid, "msg1")
    emitted = grouper.process(e1)
    assert len(emitted) == 0

    # 2. Update at T=50.
    # New expiry should be T=150.
    # Heap still has (100, key) because of lazy push optimization.
    e2 = create_log_entry(
        start_ts + datetime.timedelta(milliseconds=50), pid, tid, "msg2"
    )
    emitted = grouper.process(e2)
    assert len(emitted) == 0

    # 3. Update at T=120.
    # Current TS (120) > Heap Expiry (100).
    # Consumer loop should run, pop (100, key).
    # It should check buffer, see last entry at T=50, calculate real_expiry=150.
    # 150 > 120, so it re-pushes (150, key).
    # Then it processes T=120 update.
    e3 = create_log_entry(
        start_ts + datetime.timedelta(milliseconds=120), pid, tid, "msg3"
    )
    emitted = grouper.process(e3)

    # Should NOT flush prematurely
    assert len(emitted) == 0

    # 4. Wait until T=300 (gap of 180ms from T=120).
    # Should flush because T=120 + 100 = 220 < 300.
    e4 = create_log_entry(
        start_ts + datetime.timedelta(milliseconds=300), pid, tid, "msg4"
    )
    emitted = grouper.process(e4)

    assert len(emitted) == 1
    group = emitted[0]
    assert isinstance(group, list)
    assert len(group) == 3
    assert group[0].message == "msg1"
    assert group[1].message == "msg2"
    assert group[2].message == "msg3"


def test_windowed_grouper_lru_eviction_on_max_groups():
    """A 3rd distinct key over max_groups=2 evicts (flushes) the LRU key."""
    threshold_ms = 1000.0
    grouper = WindowedLogGrouper(
        by=["tid"], threshold_ms=threshold_ms, emit_mode="group", max_groups=2
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

    assert grouper.process(e_a) == []
    assert grouper.process(e_b) == []

    # Inserting the 3rd distinct key exceeds max_groups=2 and must evict the LRU
    # key (tid=10, "A"), emitting its group.
    emitted = grouper.process(e_c)

    assert len(emitted) == 1
    group = emitted[0]
    assert isinstance(group, list)
    assert len(group) == 1
    assert group[0].message == "A"
    assert group[0].tid == 10

    # The two most-recent keys remain open.
    assert set(grouper._buffers.keys()) == {(20,), (30,)}


def test_windowed_grouper_emit_mode_entry_flattens_on_flush():
    """emit_mode='entry' yields flat LogEntry items, not list-of-list, on flush."""
    grouper = WindowedLogGrouper(by=["tid"], threshold_ms=100.0, emit_mode="entry")

    start_ts = datetime.datetime.now(datetime.UTC).replace(microsecond=0, tzinfo=None)

    e1 = create_log_entry(start_ts, pid=1, tid=10, msg="m1")
    e2 = create_log_entry(
        start_ts + datetime.timedelta(milliseconds=10), pid=1, tid=10, msg="m2"
    )

    assert grouper.process(e1) == []
    assert grouper.process(e2) == []

    emitted = grouper.flush()

    # Two flat LogEntry items, not a single list containing a list.
    assert len(emitted) == 2
    messages = []
    for item in emitted:
        assert isinstance(item, LogEntry)
        messages.append(item.message)
    assert messages == ["m1", "m2"]


def test_windowed_grouper_limits_heap_growth_for_hot_key_updates():
    """Repeated updates for one key should not grow heap without bound."""
    grouper = WindowedLogGrouper(
        by=["pid", "tid"],
        threshold_ms=100.0,
        emit_mode="group",
        max_groups=64,
    )

    start_ts = datetime.datetime.now(datetime.UTC).replace(microsecond=0, tzinfo=None)

    for i in range(300):
        entry = create_log_entry(
            start_ts + datetime.timedelta(milliseconds=i),
            pid=100,
            tid=200,
            msg=f"hot-{i}",
        )
        grouper.process(entry)

    assert len(grouper._buffers) == 1
    assert len(grouper._heap) <= 64
