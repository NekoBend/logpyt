"""Self-tests for the real-subprocess fake adb harness (tests/fake_adb.py)."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from typing import TYPE_CHECKING

import pytest

from logpyt.streams import AsyncLogStream, LogStream
from tests.conftest import (
    FakeAdb,
    err,
    lines,
    out,
    raw,
    run,
    threadtime,
    touch,
    wait,
)

if TYPE_CHECKING:
    from logpyt.models import LogEntry


def _adb(
    fake: FakeAdb, *args: str, timeout: float = 10.0
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [fake.path, *args], capture_output=True, timeout=timeout, check=False
    )


def test_logcat_steps_and_exit_code(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(out("a\n"), err("e\n"), raw(b"\xff\r\n"), exit_code=3))

    result = _adb(fake_adb, "logcat")

    assert result.returncode == 3
    assert result.stdout == b"a\n\xff\r\n"
    assert result.stderr == b"e\n"


def test_runs_are_used_per_invocation_and_the_last_repeats(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(out("first\n")), run(out("second\n")))

    outputs = [_adb(fake_adb, "logcat").stdout for _ in range(3)]

    assert outputs == [b"first\n", b"second\n", b"second\n"]


def test_lines_step_formats_each_index(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(lines(1000, "line {i}\n")))

    result = _adb(fake_adb, "logcat")

    produced = result.stdout.decode().splitlines()
    assert produced == [f"line {i}" for i in range(1000)]


def test_pidof_is_keyed_by_package_and_unknown_commands_exit_1(
    fake_adb: FakeAdb,
) -> None:
    fake_adb.pidof("com.a", run(out("11 12\n")))

    assert _adb(fake_adb, "-s", "SER", "shell", "pidof", "com.a").stdout == b"11 12\n"
    missing = _adb(fake_adb, "shell", "pidof", "com.b")
    assert missing.returncode == 1
    assert missing.stdout == b""


def test_calls_record_arguments(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run())

    _adb(fake_adb, "-s", "SER", "logcat", "-v", "threadtime")

    (call,) = fake_adb.calls("logcat")
    assert call["args"] == ["-s", "SER", "logcat", "-v", "threadtime"]
    assert call["index"] == 0


def test_wait_and_touch_synchronize_with_the_test(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(out("before\n"), touch("ready"), wait("go"), out("after\n")))
    proc = subprocess.Popen([fake_adb.path, "logcat"], stdout=subprocess.PIPE)
    try:
        fake_adb.wait_for("ready")
        assert proc.poll() is None
        fake_adb.release("go")
        stdout, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert stdout == b"before\nafter\n"


def test_hang_runs_until_killed(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(touch("up"), hang=True))
    proc = subprocess.Popen([fake_adb.path, "logcat"])
    try:
        fake_adb.wait_for("up")
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.2)
    finally:
        proc.kill()
        proc.wait()


def test_sync_stream_reads_from_the_fake_adb(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(out(threadtime("hello")), wait("done")))
    received: list[LogEntry] = []
    got_one = threading.Event()

    def on_entry(entry: LogEntry, _handle: object) -> None:
        received.append(entry)
        got_one.set()

    stream = LogStream(stdout_callback=on_entry)
    stream.start()
    try:
        assert got_one.wait(10)
    finally:
        fake_adb.release("done")
        stream.join(timeout=10)

    assert [e.message for e in received] == ["hello"]
    assert fake_adb.calls("logcat")[0]["args"] == ["logcat"]


@pytest.mark.asyncio
async def test_async_stream_reads_from_the_fake_adb(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(out(threadtime("hello")), wait("done")))
    received: list[LogEntry] = []
    got_one = asyncio.Event()

    async def on_entry(entry: LogEntry, _handle: object) -> None:
        received.append(entry)
        got_one.set()

    stream = AsyncLogStream(stdout_callback=on_entry)
    await stream.start()
    try:
        await asyncio.wait_for(got_one.wait(), 10)
    finally:
        fake_adb.release("done")
        await stream.join(timeout=10)

    assert [e.message for e in received] == ["hello"]
