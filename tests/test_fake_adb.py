"""Self-tests for the real-subprocess fake adb harness (tests/fake_adb.py)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from logpyt import adb
from logpyt.exceptions import AdbError
from logpyt.parsers import ParseContext, ThreadTimeLogParser
from logpyt.streams import AsyncLogStream, LogStream
from tests.conftest import (
    WATCHDOG_ENV,
    FakeAdb,
    device_date,
    dumpsys_package,
    err,
    hold_stdout,
    lines,
    logcat_line,
    not_found,
    out,
    raw,
    run,
    touch,
    wait,
    wait_until,
    waiting_for_device,
)

if TYPE_CHECKING:
    from logpyt.models import LogEntry

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FAKE_ADB_SCRIPT = Path(__file__).with_name("fake_adb.py")
_DEVICE_FIXTURES = Path(__file__).with_name("fixtures") / "device"
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


def _lookup(fake: FakeAdb, package: str) -> int | None:
    """The uid the library's own lookup (design 5.8, A.8) reads from the fake."""
    argv = adb._package_uid_argv(fake.path, None, package)
    return adb._parse_package_uid(adb._run_adb(argv, timeout=TIMEOUT), package)


def test_dumpsys_is_keyed_by_package_and_unconfigured_packages_exit_1(
    fake_adb: FakeAdb,
) -> None:
    fake_adb.dumpsys("com.a", run(out(dumpsys_package("com.a", 10065))))
    fake_adb.command("default", run(out("fallback\n")))

    assert _lookup(fake_adb, "com.a") == 10065
    missing = _adb(fake_adb, "-s", "SER", "shell", "dumpsys", "package", "com.b")
    other = _adb(fake_adb, "shell", "dumpsys", "activity")

    assert (missing.returncode, missing.stdout) == (1, b"")  # no default
    assert other.stdout == b"fallback\n"  # not a lookup: the shell's runs
    keys = [(call["command"], call["key"]) for call in fake_adb.calls()]
    assert keys == [
        ("dumpsys", "dumpsys:com.a"),
        ("dumpsys", "dumpsys:com.b"),
        ("shell", "shell"),
    ]
    assert [child.args for child in fake_adb.children("dumpsys")] == [
        ("shell", "dumpsys", "package", "com.a"),
        ("-s", "SER", "shell", "dumpsys", "package", "com.b"),
    ]


def test_dumpsys_runs_are_used_per_lookup_of_each_package(fake_adb: FakeAdb) -> None:
    """Each package counts its own lookups: an install changes the answer."""
    fake_adb.dumpsys(
        "com.a",
        run(out(dumpsys_package("com.a"))),
        run(out(dumpsys_package("com.a", 10123, key="appId"))),
    )
    fake_adb.dumpsys("com.b", run(err("error: device offline\n"), exit_code=1))

    answers = [_lookup(fake_adb, "com.a")]
    with pytest.raises(AdbError, match="device offline"):
        _lookup(fake_adb, "com.b")
    answers += [_lookup(fake_adb, "com.a"), _lookup(fake_adb, "com.a")]

    assert answers == [None, 10123, 10123]  # the last run repeats
    assert [call["index"] for call in fake_adb.calls("dumpsys")] == [0, 0, 1, 2]


@pytest.mark.parametrize("key", ["userId", "appId"])
def test_dumpsys_package_text_is_read_like_the_device_output(key: str) -> None:
    """The helper's text parses as the recorded API 24 / 36 output does (A.8)."""
    installed = dumpsys_package("com.a", 10077, key=key)
    absent = dumpsys_package("com.a")

    def parse(stdout: str, package: str = "com.a") -> int | None:
        result = adb._AdbResult(argv=(), returncode=0, stdout=stdout, stderr="")
        return adb._parse_package_uid(result, package)

    assert parse(installed) == 10077
    assert parse(installed, "com.a.b") is None
    assert parse(absent) is None


# -- the device clock, the state check and an absent device (design 3.2, 5.6) ---


def _clock(fake: FakeAdb) -> int:
    """The device clock the library's own clock read gets from the fake."""
    argv = adb._device_clock_argv(fake.path, None)
    return adb._parse_device_clock(adb._run_adb(argv, timeout=TIMEOUT))


def _state(fake: FakeAdb, serial: str | None = None) -> str | None:
    """The device state the library's own state check gets from the fake."""
    argv = adb._get_state_argv(fake.path, serial)
    return adb._parse_get_state(adb._run_adb(argv, timeout=TIMEOUT))


def test_shell_date_answers_the_host_clock_unless_scripted(fake_adb: FakeAdb) -> None:
    """Every stream connection reads the clock first, so the fake has an answer."""
    fake_adb.command("default", run(err("fallback\n"), exit_code=4))
    before = int(time.time())

    answer = _clock(fake_adb)

    assert before <= answer <= int(time.time())
    assert [(c["command"], c["key"]) for c in fake_adb.calls()] == [("date", "date")]
    assert fake_adb.children()[0].args == ("shell", "date", "+%s")


def test_shell_date_runs_are_used_per_clock_read(fake_adb: FakeAdb) -> None:
    fake_adb.date(
        run(out(device_date(1_793_756_125))),
        run(err("adb: device offline\n"), exit_code=1),
    )

    first = _clock(fake_adb)
    with pytest.raises(AdbError, match="device offline"):
        _clock(fake_adb)
    with pytest.raises(AdbError, match="device offline"):
        _clock(fake_adb)  # the last run repeats

    assert first == 1_793_756_125
    assert [call["index"] for call in fake_adb.calls("date")] == [0, 1, 2]


def test_get_state_runs_answer_the_state_check(fake_adb: FakeAdb) -> None:
    fake_adb.get_state(
        run(out("device\n")),
        run(err("error: device offline\n"), exit_code=1),
        not_found("emulator-9999"),
    )

    states = [_state(fake_adb, "emulator-9999") for _ in range(3)]

    assert states == ["device", "offline", None]
    calls = fake_adb.calls("get-state")
    assert [call["args"] for call in calls] == [
        ["-s", "emulator-9999", "get-state"]
    ] * 3


def test_waiting_for_device_writes_its_notice_and_never_exits(
    fake_adb: FakeAdb,
) -> None:
    """The hang of an adb that waits for an absent device: stderr only."""
    fake_adb.logcat(waiting_for_device())
    proc = subprocess.Popen(
        [fake_adb.path, "-s", "emulator-9999", "logcat"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stderr is not None
        notice = proc.stderr.readline()
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.2)
    finally:
        _end(proc)
    assert proc.stdout is not None
    assert proc.stdout.read() == b""
    assert notice == b"- waiting for device -\n"


@pytest.mark.parametrize("level", ["api24", "api36"])
def test_device_absent_answers_like_the_recorded_absent_serial(
    fake_adb: FakeAdb, level: str
) -> None:
    """device_absent() reproduces R4_absent_serial.json (FINDINGS.md, check 4)."""
    recording = _DEVICE_FIXTURES / level / "R4_absent_serial.json"
    facts = json.loads(recording.read_text(encoding="utf-8"))["facts"]
    serial = "emulator-9999"
    fake_adb.device_absent(serial)

    state = _adb(fake_adb, "-s", serial, "get-state")
    date = _adb(fake_adb, "-s", serial, "shell", "date")
    logcat = subprocess.Popen(
        [fake_adb.path, "-s", serial, "logcat"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert logcat.stderr is not None
        notice = logcat.stderr.readline().decode().rstrip("\n")
        assert logcat.poll() is None
    finally:
        _end(logcat)

    assert (state.returncode, state.stderr.decode().splitlines()) == (
        facts["get_state"]["rc"],
        facts["get_state"]["stderr"],
    )
    assert (date.returncode, date.stderr.decode().splitlines()) == (
        facts["shell_date"]["rc"],
        facts["shell_date"]["stderr"],
    )
    assert [notice] == facts["logcat_live"]["stderr"]
    assert _state(fake_adb, serial) is None
    with pytest.raises(AdbError, match="not found"):
        _clock(fake_adb)


def test_logcat_line_is_the_live_wire_format() -> None:
    """The helper's lines parse as the U-1 format: year, usec, zone and uid."""
    parser = ThreadTimeLogParser()
    app = parser.parse(logcat_line("hi", uid=10065, pid=42, tag="App"), ParseContext())
    system = parser.parse(logcat_line("sys", uid="root"), ParseContext())

    assert app is not None
    assert system is not None
    assert (app.uid, app.pid, app.tag, app.message) == (10065, 42, "App", "hi")
    assert app.timestamp.isoformat() == "2026-11-04T05:11:16.661446+00:00"
    assert (system.uid, system.uid_raw) == (0, "root")


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


def test_hold_stdout_keeps_the_pipe_open_after_adb_exits(fake_adb: FakeAdb) -> None:
    """A grandchild holds adb's stdout: the pipe ends only after it let go.

    A reader that reads to EOF does not reach it while adb has exited and
    the grandchild still holds the pipe; after the release, the text it then
    writes comes through the same pipe and EOF follows.
    """
    fake_adb.logcat(run(out("adb\n"), hold_stdout("go", then="grandchild\n")))
    proc = subprocess.Popen(
        [fake_adb.path, "logcat"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL
    )
    assert proc.stdout is not None
    pipe = proc.stdout
    chunks: list[bytes] = []
    eof = threading.Event()

    def read_to_eof() -> None:
        chunks.append(pipe.read())
        eof.set()

    reader = threading.Thread(target=read_to_eof, daemon=True)
    reader.start()
    try:
        assert proc.wait(timeout=TIMEOUT) == 0
        assert not eof.wait(0.2)  # adb exited; the grandchild holds the pipe
        fake_adb.release("go")
        assert eof.wait(TIMEOUT)
    finally:
        fake_adb.release("go")
        _end(proc)
        reader.join(TIMEOUT)
        pipe.close()
    assert b"".join(chunks) == b"adb\ngrandchild\n"


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
    fake_adb.logcat(run(out(logcat_line("hello")), wait("done")))
    received: list[LogEntry] = []
    got_one = threading.Event()

    def on_entry(entry: LogEntry) -> None:
        received.append(entry)
        got_one.set()

    stream = LogStream(on_item=on_entry)
    stream.start()
    try:
        assert got_one.wait(TIMEOUT)
        stream.stop()
        stream.join(timeout=TIMEOUT)
    finally:
        fake_adb.release("done")
        stream.kill()

    assert [e.message for e in received] == ["hello"]
    # The clock read, then the spawn: the library owns the rest of argv.
    assert [call["command"] for call in fake_adb.calls()] == ["date", "logcat"]
    assert "logcat" in fake_adb.calls("logcat")[0]["args"]


@pytest.mark.asyncio
async def test_async_stream_reads_from_the_fake_adb(fake_adb: FakeAdb) -> None:
    fake_adb.logcat(run(out(logcat_line("hello")), wait("done")))
    received: list[LogEntry] = []
    got_one = asyncio.Event()

    async def on_entry(entry: LogEntry) -> None:
        received.append(entry)
        got_one.set()

    stream = AsyncLogStream(on_item=on_entry)
    stream.start()
    try:
        await asyncio.wait_for(got_one.wait(), TIMEOUT)
        stream.stop()
        await stream.join(timeout=TIMEOUT)
    finally:
        fake_adb.release("done")
        stream.kill()

    assert [e.message for e in received] == ["hello"]
    # The clock read, then the spawn: the library owns the rest of argv.
    assert [call["command"] for call in fake_adb.calls()] == ["date", "logcat"]


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
