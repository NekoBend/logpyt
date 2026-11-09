"""Device gates of the duplicate-free resume (design 9.4, the F1-6 row; PR-14).

Run per API level on the emulator, with the tunnel to the adb server up::

    LOGPYT_DEVICE=<serial> LOGPYT_DEVICE_API=24 pytest -m device -s \\
        tests/device/test_resume_gate.py

The conftest skips every test here without ``LOGPYT_DEVICE`` or without the
tunnel (127.0.0.1:5037), and the streams' adb children get
``ADB_SERVER_SOCKET=tcp:127.0.0.1:5037``, with which the adb client never
starts a server of its own (FINDINGS.md, "The adb server is never started").
The machinery and the pass criteria are in ``resume_gate.py``.

Environment:

- ``LOGPYT_GATE_SECONDS``: how long the resume gate runs (default 600, the 10
  minutes per level of PR-14; 3600 before the release);
- ``LOGPYT_GATE_STALL``: the overwrite gate's consumer stall (default 60);
- ``LOGPYT_VM_SSH`` and ``LOGPYT_VM_ADB``: the ssh wrapper and the VM's adb
  (``tools/device_probe.py --vm-ssh/--vm-adb``). With both, the resume gate
  also restarts the adb server inside emuvm; without them that disconnect
  kind is left out, and the test records so. The unplug (``emu kill``) of
  check 4 is not one of the gate's disconnects: the emulator stays down.

Each gate records its numbers with ``record_property`` (``--junitxml``) and
the run without the stall rule prints its recording (``-s``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import secrets
import shutil
import time
from typing import TYPE_CHECKING

import pytest

from logpyt import AsyncLogStream, LogStream, StreamConfig, StreamStats, Tag
from tests.conftest import wait_until
from tests.device.resume_gate import (
    GATE_SECONDS,
    OVERWRITE_BATCH,
    SLACK,
    STALL_SECONDS,
    WAIT,
    Capture,
    DeviceBench,
    notice_times,
    run_overwrite_gate,
    run_resume_gate,
)
from tools import device_probe

if TYPE_CHECKING:
    from collections.abc import Callable

    from logpyt import LogEntry

API_WITH_NOTICES = 24  # FINDINGS.md check 6: 36 writes no chatty notices
# Design A.25: on 36 no two writer lines shared a microsecond, in over 900 000
# lines, 80 000 lines/s included, so no burst can collide there.
API_WITH_COLLISIONS = 24
SINCE_TAG = "lpsince"


@pytest.fixture
def bench(
    probe_ctx: device_probe.Context, monkeypatch: pytest.MonkeyPatch
) -> DeviceBench:
    """The device bench; the library's adb children cannot start a server."""
    monkeypatch.setenv("ADB_SERVER_SOCKET", probe_ctx.adb.server_socket)
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    ssh = os.environ.get("LOGPYT_VM_SSH", "").strip()
    vm_adb = os.environ.get("LOGPYT_VM_ADB", "").strip()
    vm = device_probe.Vm(ssh, vm_adb) if ssh and vm_adb and shutil.which(ssh) else None
    return DeviceBench(probe_ctx, vm)


@pytest.mark.watchdog(GATE_SECONDS + SLACK)
def test_sequence_has_no_duplicates(
    bench: DeviceBench, record_property: Callable[[str, object], None]
) -> None:
    """F1-6 resume gate (design 9.4): across 10 disconnects that keep the ring,
    no seq is delivered twice, every burst comes exactly K times or lies in a
    Gap, at least one burst had a same-microsecond collision where the level's
    clock lets them happen (A.9, A.25), and no disconnect left a ring_rotated
    or clock_moved_back Gap.
    """
    capture, report, stream, made = run_resume_gate(bench, GATE_SECONDS)

    stats = stream.stats
    record_property("api", bench.ctx.api)
    record_property("disconnects", made)
    record_property("report", report.summary())
    record_property("stats", dataclasses.asdict(stats))
    if "server_restart" not in bench.kinds:
        record_property("left_out", "server_restart (no LOGPYT_VM_SSH/LOGPYT_VM_ADB)")
    assert report.failures == []
    assert [g.reason for g in capture.gaps if g.reason != "device_reported"] == []
    if bench.ctx.api == API_WITH_COLLISIONS:
        assert report.collided_bursts >= 1
    assert stats.deduplicated > 0
    assert stats.reconnects >= len(made)
    assert stream.outcome is None


@pytest.mark.watchdog(STALL_SECONDS + 6 * WAIT + SLACK)
def test_stalled_consumer_overwrite_is_reported(
    bench: DeviceBench, record_property: Callable[[str, object], None]
) -> None:
    """Overwrite gate (design 9.4, A.6): a consumer stalls while the writer
    wraps the ring; the stall resume (the default stall_timeout) turns the
    overwrite into a reported Gap, with no duplicate and every missing seq
    inside a Gap.
    """
    capture, report, stream = run_overwrite_gate(
        bench,
        STALL_SECONDS,
        stall_timeout=1.0,
        batch=OVERWRITE_BATCH.get(bench.ctx.api, 10),
    )

    stats = stream.stats
    record_property("api", bench.ctx.api)
    record_property("report", report.summary())
    record_property("stats", dataclasses.asdict(stats))
    assert report.failures == []
    assert stats.stall_resumes >= 1
    # Without a ring_rotated Gap the ring did not wrap: the run proves nothing.
    assert any(g.reason == "ring_rotated" for g in capture.gaps)
    assert stream.outcome is None


@pytest.mark.watchdog(STALL_SECONDS + 6 * WAIT + SLACK)
def test_stalled_consumer_without_stall_rule_is_recorded(
    bench: DeviceBench, record_property: Callable[[str, object], None]
) -> None:
    """The same run with ``stall_timeout=None``: recorded, not gated (design
    9.4). It documents what a stalled reader gets without the stall rule (A.6:
    a prefix, then EOF and status 1, an ordinary end without ``reconnect``).
    """
    capture, report, stream = run_overwrite_gate(
        bench,
        STALL_SECONDS,
        stall_timeout=None,
        batch=OVERWRITE_BATCH.get(bench.ctx.api, 10),
    )

    outcome = stream.outcome
    record = {
        "api": bench.ctx.api,
        "state": str(stream.state),
        "outcome": repr(outcome),
        "returncode": getattr(outcome, "returncode", None),
        "delivered_seqs": capture.count("seq"),
        "gaps": [g.reason for g in capture.gaps],
        "report": report.summary(),
    }
    for key, value in record.items():
        record_property(key, value)
    print(f"stall_timeout=None run: {record}")


def _since_now_sync(
    bench: DeviceBench, config: StreamConfig, rid: str
) -> tuple[list[str], StreamStats]:
    got: list[str] = []
    stream = LogStream(
        config,
        where=Tag(SINCE_TAG),
        on_item=lambda e: got.append(e.message.rstrip(" ")),
    )
    stream.start()
    try:
        stream.wait_running(WAIT)
        bench.log(SINCE_TAG, f"after-{rid}")
        wait_until(lambda: f"after-{rid}" in got, timeout=WAIT)
    finally:
        stream.stop()
        stream.join(WAIT)
    return got, stream.stats


def _since_now_async(
    bench: DeviceBench, config: StreamConfig, rid: str
) -> tuple[list[str], StreamStats]:
    got: list[str] = []

    def on_item(entry: LogEntry) -> None:
        got.append(entry.message.rstrip(" "))

    async def main() -> StreamStats:
        stream = AsyncLogStream(config, where=Tag(SINCE_TAG), on_item=on_item)
        stream.start()
        try:
            await stream.wait_running(WAIT)
            await asyncio.to_thread(bench.log, SINCE_TAG, f"after-{rid}")
            deadline = time.monotonic() + WAIT
            while f"after-{rid}" not in got and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        finally:
            stream.stop()
            await stream.join(WAIT)
        return stream.stats

    return got, asyncio.run(main())


@pytest.mark.watchdog(5 * WAIT + SLACK)
@pytest.mark.parametrize("driver", ["sync", "async"])
def test_since_now_captures_from_wait_running(bench: DeviceBench, driver: str) -> None:
    """``since="now"`` (design 2.3, 9.4): a line logged before start() is not
    delivered; a line logged right after wait_running() returns is.
    """
    rid = secrets.token_hex(4)
    bench.log(SINCE_TAG, f"before-{rid}")
    config = StreamConfig(adb_path=bench.adb_path, serial=bench.serial, since="now")
    run = _since_now_sync if driver == "sync" else _since_now_async

    got, stats = run(bench, config, rid)

    assert f"before-{rid}" not in got
    assert got.count(f"after-{rid}") == 1
    assert stats.before_start >= 1  # the start marker at least


@pytest.mark.watchdog(10 * 60.0)
def test_chatty_notice_carries_the_newest_pruned_time(
    bench: DeviceBench, record_property: Callable[[str, object], None]
) -> None:
    """Stage-1 D3, open check 6 (API 24): the resume treats a chatty notice at
    or below hw as a replay, which is sound only if a notice carries the time
    of the newest line it replaced. A live stream records every writer line
    with its time; the writer's flood makes logd prune them; a dump shows
    where each notice stands and which of the lines read live it replaced.
    """
    if bench.ctx.api != API_WITH_NOTICES:
        pytest.skip("API 36 writes no chatty notices (FINDINGS.md check 6)")
    rid = secrets.token_hex(4)
    capture = Capture(rid)
    stream = LogStream(
        StreamConfig(adb_path=bench.adb_path, serial=bench.serial, since="now"),
        where=Tag("sh"),
        on_item=capture.on_item,
    )
    stream.start()
    try:
        stream.wait_running(WAIT)
        bench.start_writer(rid, count=30_000, burst=0, pause=False)
        wait_until(lambda: capture.ended() is not None, timeout=5 * WAIT)
    finally:
        stream.stop()
        stream.join(WAIT)
    live = {s.n: s.ts for s in capture.lines if s.kind == "seq"}
    verdicts = notice_times(bench.dump(), rid, live)
    record_property("notices", verdicts)
    if not verdicts:
        pytest.skip("logd pruned none of the writer's lines with a notice")
    assert all(v == "newest" for v in verdicts), verdicts
