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

    start_ts = datetime.datetime.now().replace(microsecond=0)
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
