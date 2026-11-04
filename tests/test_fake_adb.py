"""Self-tests for the real-subprocess fake adb harness (tests/fake_adb.py)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from logpyt.streams import AsyncLogStream, LogStream
from tests.conftest import (
    WATCHDOG_ENV,
    FakeAdb,
    err,
    lines,
    out,
    raw,
    run,
    threadtime,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from logpyt.models import LogEntry

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FAKE_ADB_SCRIPT = Path(__file__).with_name("fake_adb.py")
TIMEOUT = 10.0


def _adb(
    fake: FakeAdb, *args: str, timeout: float = TIMEOUT
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [fake.path, *args], capture_output=True, timeout=timeout, check=False
    )


def _end(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` and reap it, bounded."""
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=TIMEOUT)


# -- child behavior -----------------------------------------------------------


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


def test_pidof_is_keyed_by_package_and_unconfigured_packages_exit_1(
    fake_adb: FakeAdb,
) -> None:
    fake_adb.pidof("com.a", run(out("11 12\n")))

    assert _adb(fake_adb, "-s", "SER", "shell", "pidof", "com.a").stdout == b"11 12\n"
    missing = _adb(fake_adb, "shell", "pidof", "com.b")
    assert missing.returncode == 1
    assert missing.stdout == b""


def test_unconfigured_commands_exit_1_until_a_default_run_is_set(
    fake_adb: FakeAdb,
) -> None:
    unknown = _adb(fake_adb, "devices")
    assert (unknown.returncode, unknown.stdout) == (1, b"")

    fake_adb.command("default", run(out("fallback\n"), exit_code=4))
    fallback = _adb(fake_adb, "devices")

    assert (fallback.returncode, fallback.stdout) == (4, b"fallback\n")


def test_calls_record_arguments(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run())

    _adb(fake_adb, "-s", "SER", "logcat", "-v", "threadtime")

    (call,) = fake_adb.calls("logcat")
    assert call["args"] == ["-s", "SER", "logcat", "-v", "threadtime"]
    assert call["index"] == 0
    assert call["seq"] == 0
    assert call["pid"] == fake_adb.children("logcat")[0].pid


def test_wait_and_touch_synchronize_with_the_test(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(out("before\n"), touch("ready"), wait("go"), out("after\n")))
    proc = subprocess.Popen([fake_adb.path, "logcat"], stdout=subprocess.PIPE)
    try:
        fake_adb.wait_for("ready")
        assert proc.poll() is None
        fake_adb.release("go")
        stdout, _ = proc.communicate(timeout=TIMEOUT)
    finally:
        _end(proc)
    assert stdout == b"before\nafter\n"


def test_hang_runs_until_killed(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(touch("up"), hang=True))
    proc = subprocess.Popen([fake_adb.path, "logcat"])
    try:
        fake_adb.wait_for("up")
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.2)
    finally:
        _end(proc)


def test_a_child_killed_at_spawn_does_not_break_later_invocations(
    fake_adb: FakeAdb,
) -> None:
    """Killing children at arbitrary points leaves no lock or counter behind."""
    fake_adb.logcat(run(out("x\n")))
    for _ in range(5):
        _end(subprocess.Popen([fake_adb.path, "logcat"]))

    result = _adb(fake_adb, "logcat")

    assert (result.returncode, result.stdout) == (0, b"x\n")
    assert [c.index for c in fake_adb.children("logcat")] == [0, 1, 2, 3, 4, 5]
    assert fake_adb.calls("logcat")[-1]["index"] == 5


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows does not re-parent an orphan; only the hang limit ends it",
)
def test_a_hanging_child_exits_once_its_parent_is_gone(fake_adb: FakeAdb) -> None:
    """A hang=True child spawned by a process that then exits does not linger.

    The middle process exits right after spawning; the fake adb inherited its
    stdout pipe, so ``run()`` sees EOF only once the orphan has exited too.
    """
    fake_adb.logcat(run(hang=True))
    spawn_and_exit = (
        "import os, subprocess, sys\n"
        "script, state = sys.argv[1:]\n"
        "child = subprocess.Popen([sys.executable, '-I', script,"
        " '--fake-state', state, '--fake-index', '0', '--fake-seq', '999',"
        " '--fake-parent', str(os.getpid()), 'logcat'])\n"
        "print(child.pid, flush=True)\n"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", spawn_and_exit, _FAKE_ADB_SCRIPT, fake_adb.state],
        stdout=subprocess.PIPE,
        timeout=30,
        check=True,
    )

    assert result.stdout.strip().isdigit()
    assert [call["seq"] for call in fake_adb.calls("logcat")] == [999]


# -- library smoke tests ------------------------------------------------------


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
        assert got_one.wait(TIMEOUT)
    finally:
        fake_adb.release("done")
        stream.join(timeout=TIMEOUT)

    assert [e.message for e in received] == ["hello"]
    # Only that the spawn reached the fake: the library owns the rest of argv.
    assert "logcat" in fake_adb.calls("logcat")[0]["args"]


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
        await asyncio.wait_for(got_one.wait(), TIMEOUT)
    finally:
        fake_adb.release("done")
        await stream.join(timeout=TIMEOUT)

    assert [e.message for e in received] == ["hello"]


# -- child registry -----------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_tracks_sync_and_async_children(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(hang=True))
    fake_adb.pidof("com.a", run(out("1\n")))
    sync_proc = subprocess.Popen([fake_adb.path, "-s", "SER", "logcat"])
    try:
        async_proc = await asyncio.create_subprocess_exec(
            fake_adb.path, "shell", "pidof", "com.a", stdout=asyncio.subprocess.PIPE
        )
        seen = [
            (c.pid, c.command, c.key, c.index, c.args, c.is_async)
            for c in fake_adb.children()
        ]
        assert seen == [
            (sync_proc.pid, "logcat", "logcat", 0, ("-s", "SER", "logcat"), False),
            (
                async_proc.pid,
                "pidof",
                "pidof:com.a",
                0,
                ("shell", "pidof", "com.a"),
                True,
            ),
        ]
        (pidof,) = fake_adb.children("pidof")
        assert pidof.process is async_proc

        stdout, _ = await asyncio.wait_for(async_proc.communicate(), TIMEOUT)
        assert stdout == b"1\n"
        assert await fake_adb.async_survivors(command="pidof") == []
        assert [c.pid for c in fake_adb.live_children()] == [sync_proc.pid]
    finally:
        _end(sync_proc)

    assert await fake_adb.async_survivors() == []
    assert fake_adb.survivors(timeout=0) == []


@pytest.mark.asyncio
async def test_live_children_never_takes_an_async_exit_status(
    fake_adb: FakeAdb,
) -> None:
    """Probing an exited asyncio child leaves its exit status for asyncio."""
    fake_adb.logcat(run(wait("go"), exit_code=7))
    proc = await asyncio.create_subprocess_exec(fake_adb.path, "logcat")
    assert [c.pid for c in fake_adb.live_children()] == [proc.pid]

    fake_adb.release("go")
    # Blocking poll: the event loop cannot run, so asyncio has not reaped the
    # child when the probe first sees it gone.
    wait_until(lambda: not fake_adb.live_children(), timeout=TIMEOUT)

    assert await asyncio.wait_for(proc.wait(), TIMEOUT) == 7


def test_close_kills_live_children_and_later_spawns_fail(fake_adb: FakeAdb) -> None:
    """The fixture finalizer's cleanup, called early (it is idempotent)."""
    fake_adb.logcat(run(touch("up"), hang=True))
    proc = subprocess.Popen([fake_adb.path, "logcat"])
    try:
        fake_adb.wait_for("up")

        fake_adb.close()

        assert proc.poll() is not None
        assert fake_adb.live_children() == []
        with pytest.raises(FileNotFoundError):
            subprocess.Popen([fake_adb.path, "logcat"])
    finally:
        _end(proc)


# -- spawn gate ---------------------------------------------------------------


def test_hold_spawn_blocks_a_sync_spawn_until_released(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(touch("ran"), hang=True))
    spawned: list[subprocess.Popen[bytes]] = []

    def spawn() -> None:
        spawned.append(subprocess.Popen([fake_adb.path, "logcat"]))

    spawner = threading.Thread(target=spawn)
    try:
        with fake_adb.hold_spawn() as gate:
            spawner.start()
            (held,) = gate.wait_entered()
            fake_adb.wait_for("ran")  # the child itself runs while it is held

            assert held.command == "logcat"
            assert held.alive()
            assert spawned == []  # the caller has not got its Popen yet
            assert spawner.is_alive()
        spawner.join(TIMEOUT)

        (proc,) = spawned
        assert proc.pid == held.pid
        assert gate.released
    finally:
        spawner.join(TIMEOUT)
        for proc in spawned:
            _end(proc)


@pytest.mark.asyncio
async def test_hold_spawn_blocks_an_async_spawn_until_released(
    fake_adb: FakeAdb,
) -> None:
    fake_adb.logcat(run(hang=True))
    gate = fake_adb.hold_spawn(command="logcat")
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(fake_adb.path, "logcat"))
    try:
        (held,) = await gate.async_wait_entered()

        assert held.is_async
        assert held.alive()
        assert not spawn.done()

        gate.release()
        proc = await asyncio.wait_for(spawn, TIMEOUT)
        assert proc.pid == held.pid
    finally:
        gate.release()
        if not spawn.done():
            spawn.cancel()
    proc.kill()
    assert await asyncio.wait_for(proc.wait(), TIMEOUT) is not None


@pytest.mark.asyncio
async def test_a_spawn_cancelled_at_the_gate_kills_its_child(
    fake_adb: FakeAdb,
) -> None:
    fake_adb.logcat(run(hang=True))
    gate = fake_adb.hold_spawn()
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(fake_adb.path, "logcat"))
    try:
        await gate.async_wait_entered()
        spawn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await spawn
    finally:
        gate.release()

    assert await fake_adb.async_survivors(timeout=TIMEOUT) == []


def test_hold_spawn_lets_other_commands_through(fake_adb: FakeAdb) -> None:
    fake_adb.command("devices", run(out("List of devices attached\n")))

    with fake_adb.hold_spawn(command="logcat") as gate:
        result = _adb(fake_adb, "devices")  # from this thread: would block if held

    assert result.stdout == b"List of devices attached\n"
    assert gate.entered == 0


def test_only_one_spawn_gate_is_armed_at_a_time(fake_adb: FakeAdb) -> None:
    first = fake_adb.hold_spawn()
    with pytest.raises(RuntimeError, match="release the previous spawn gate"):
        fake_adb.hold_spawn()

    first.release()
    second = fake_adb.hold_spawn()

    assert second is not first
    second.release()


def test_close_releases_a_held_spawn(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(hang=True))
    spawned: list[subprocess.Popen[bytes]] = []
    gate = fake_adb.hold_spawn()
    spawner = threading.Thread(
        target=lambda: spawned.append(subprocess.Popen([fake_adb.path, "logcat"]))
    )
    spawner.start()
    try:
        gate.wait_entered()

        fake_adb.close()
        spawner.join(TIMEOUT)

        assert not spawner.is_alive()
        assert gate.released
        assert fake_adb.live_children() == []
    finally:
        gate.release()
        spawner.join(TIMEOUT)
        for proc in spawned:
            _end(proc)


def test_an_unreleased_gate_times_out_and_fails_the_teardown(
    fake_adb: FakeAdb,
) -> None:
    fake_adb.logcat(run())
    fake_adb.hold_spawn(timeout=0.2)

    started = time.monotonic()
    result = _adb(fake_adb, "logcat")  # held by this very thread: times out

    assert result.returncode == 0
    # Python 3.12 on Windows reads the monotonic clock from GetTickCount64, in
    # 15.6 ms ticks, and the gate's wait can end by that coarse clock too, so
    # allow two ticks (CI measured 0.188 s once).
    tick = time.get_clock_info("monotonic").resolution
    assert time.monotonic() - started >= 0.2 - 2 * tick
    with pytest.raises(AssertionError, match="spawn gate never released"):
        fake_adb.close()


# -- hang watchdog ------------------------------------------------------------


def test_watchdog_ends_a_hung_test_with_tracebacks(tmp_path: Path) -> None:
    """A test past the limit dumps its traceback to stderr and ends pytest.

    Runs a hanging test in a child pytest that loads this conftest as a
    plugin, with a 1 s limit.
    """
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    hung = tmp_path / "test_hung.py"
    hung.write_text(
        "import time\n\n\ndef test_hangs_forever():\n    time.sleep(60)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env[WATCHDOG_ENV] = "1"

    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.conftest",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            f"--basetemp={tmp_path / 'inner'}",
            str(hung),
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 1, result.stderr.decode(errors="replace")
    assert b"Timeout (0:00:01)!" in result.stderr
    assert b"test_hangs_forever" in result.stderr
    assert elapsed < 30
