"""``tools/device_probe.py`` without a device: its guard, its children, its helpers.

Also the bounded client wait of ``tools/adb_server_group.py`` (C09).
"""

from __future__ import annotations

import calendar
import contextlib
import dataclasses
import json
import os
import signal
import socket
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from tests.conftest import _FAKE_PYTHON
from tests.device_facts import (
    FIXTURES,
    body,
    check_fixture_hygiene,
    level_dir,
    load,
    step,
)
from tools import adb_server_group, device_probe
from tools.device_probe import (
    Adb,
    LiveReader,
    TunnelDownError,
    boundary_facts,
    epoch_arg,
    missing_ranges,
    parse_u1a,
    pick_boundary,
)

if TYPE_CHECKING:
    from pathlib import Path

# A closed loopback port refuses at once on Linux; on Windows a refused
# connect is retried for about 2 s, so the tests give it less.
FAST_CONNECT = 0.2
# The children below use only the standard library: the base interpreter runs
# them under -I, one process per spawn even in a Windows venv, whose
# sys.executable is a launcher that starts the real interpreter as a second
# process (tests/conftest.py).
PYTHON = _FAKE_PYTHON
SLEEPER = [PYTHON, "-I", "-c", "import time; time.sleep(30)"]


@pytest.fixture
def dead_server() -> tuple[str, int]:
    """A loopback address on which nothing listens."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return ("127.0.0.1", port)


@pytest.fixture
def no_spawn(monkeypatch):
    """Fail the test if anything spawns a process."""

    def refuse(*args, **kwargs):
        pytest.fail(f"adb was spawned: {args!r}")

    monkeypatch.setattr(device_probe.subprocess, "Popen", refuse)


def test_adb_run_refuses_to_spawn_when_the_server_port_is_closed(dead_server, no_spawn):
    adb = Adb("adb", "emulator-5554", server=dead_server, connect_timeout=FAST_CONNECT)
    with pytest.raises(TunnelDownError, match="cannot start a server"):
        adb.run("devices", ["devices"])
    with pytest.raises(TunnelDownError):
        adb.shell("date", "date")


def test_live_reader_refuses_to_spawn_when_the_server_port_is_closed(
    dead_server, no_spawn
):
    adb = Adb("adb", "emulator-5554", server=dead_server, connect_timeout=FAST_CONNECT)
    with pytest.raises(TunnelDownError):
        LiveReader(adb, "live", ["logcat"])


def test_time_limit_caps_every_timeout_and_then_refuses_to_spawn(monkeypatch):
    timeouts: list[float] = []

    def fake_exec(argv, *, timeout, env=None, stdin=None):
        timeouts.append(timeout)
        return 0, b"", b"", 0.0, True

    monkeypatch.setattr(device_probe, "server_reachable", lambda *a, **k: True)
    monkeypatch.setattr(device_probe, "_exec", fake_exec)
    adb = Adb("adb", "emulator-5554")
    ctx = device_probe.Context(adb=adb, api=24, device={})
    with ctx.time_limit(5):
        adb.run("x", ["devices"], timeout=180)
        adb.run("y", ["devices"], timeout=1)
        adb.deadline = time.monotonic() - 1
        with pytest.raises(device_probe.TimeLimitError):
            adb.run("z", ["devices"])
        with pytest.raises(device_probe.TimeLimitError):
            LiveReader(adb, "live", ["logcat"])
    assert adb.deadline is None, "the limit ends with the block"
    adb.run("after", ["devices"], timeout=30)
    assert timeouts[0] <= 5
    assert timeouts[1:] == [1, 30]


def test_server_reachable_sees_a_listening_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        assert device_probe.server_reachable(sock.getsockname())


def test_child_environment_points_at_the_server_and_drops_tags(monkeypatch):
    monkeypatch.setenv("ANDROID_LOG_TAGS", "*:S")
    monkeypatch.setenv("ANDROID_SERIAL", "emulator-1")
    env = Adb("adb", "x", server=("127.0.0.1", 5037)).env()
    assert env["ADB_SERVER_SOCKET"] == "tcp:127.0.0.1:5037"
    assert "ANDROID_LOG_TAGS" not in env
    assert "ANDROID_SERIAL" not in env
    env = Adb("adb", "x").env({"ANDROID_LOG_TAGS": "logpyt:V *:S"})
    assert env["ANDROID_LOG_TAGS"] == "logpyt:V *:S"


def test_adb_run_records_the_arguments_without_the_executable(monkeypatch):
    def fake_exec(argv, *, timeout, env=None, stdin=None):
        assert argv[0] == "/path/to/adb"
        return 0, b"a\nb\r\n", b"", 0.01, True

    monkeypatch.setattr(device_probe, "server_reachable", lambda *a, **k: True)
    monkeypatch.setattr(device_probe, "_exec", fake_exec)
    result = Adb("/path/to/adb", "emulator-5556").run("x", ["logcat", "-d"])
    assert result.argv == ["-s", "emulator-5556", "logcat", "-d"]
    assert result.stdout == ["a", "b\r"], "LF framing keeps a trailing CR"
    assert result.to_json()["stdout_total_lines"] == 2


def test_epoch_arg_always_has_six_fraction_digits():
    # A digits-only -T value is a line count (R2), so every value needs the dot.
    for us in (0, 1, 999_999, 1_000_000, 1_793_760_496_667_655):
        value = epoch_arg(us)
        seconds, dot, fraction = value.partition(".")
        assert dot == "."
        assert len(fraction) == 6
        assert int(seconds) * 1_000_000 + int(fraction) == us


def test_parse_u1a_applies_the_zone():
    line = "2026-11-04 11:48:35.826893 +0900 10014  2267  3283 I Vision  : msg: x"
    parsed = parse_u1a(line)
    assert parsed is not None
    assert epoch_arg(parsed.us) == "1793760515.826893"
    assert (parsed.uid, parsed.pid, parsed.tid) == ("10014", 2267, 3283)
    assert (parsed.level, parsed.tag, parsed.msg) == ("I", "Vision", "msg: x")
    assert parse_u1a("2026-11-04 02:48:35.826893  2267  3283 I Vision  : x") is None


@pytest.mark.parametrize(("zone", "offset_s"), [("+0530", 19800), ("-0330", -12600)])
def test_parse_u1a_applies_zone_minutes(zone, offset_s):
    line = f"2026-11-04 11:48:35.000001 {zone}  1000  1  1 I tag : m"
    parsed = parse_u1a(line)
    assert parsed is not None
    local = calendar.timegm((2026, 11, 4, 11, 48, 35, 0, 0, 0))
    assert parsed.us == (local - offset_s) * 1_000_000 + 1


@pytest.mark.parametrize("level", [24, 36])
def test_parse_u1a_reads_every_recorded_line(level):
    lines = body(step(load(level_dir(level), "R6"), "dump")["stdout"])
    assert all(parse_u1a(line) is not None for line in lines)


def test_pick_boundary_prefers_a_shared_timestamp():
    assert pick_boundary([1, 2, 3, 4, 4, 5, 6, 7, 8]) == 4
    # The shared key wins even when it is not the first of the middle third.
    assert pick_boundary([1, 2, 3, 4, 5, 5, 6, 7, 8]) == 5
    assert pick_boundary([1, 2, 3, 4, 5, 6]) in {3, 4}


def test_boundary_facts_counts_missing_and_lower_lines():
    reference = [(5, "a"), (5, "b"), (6, "c")]
    returned = [(4, "x"), (5, "a"), (6, "c")]
    facts = boundary_facts(reference, returned, 5)
    assert facts["boundary_lines_in_reference"] == 2
    assert facts["boundary_lines_missing"] == 1
    assert facts["lines_below_boundary"] == 1


def test_missing_ranges():
    assert missing_ranges(bytearray([1, 0, 0, 1, 0])) == [[1, 2], [4, 4]]
    assert missing_ranges(bytearray([1, 1])) == []


HEAD = "2026-11-04 04:56:44.924776 +0000 shell  4160  4160 I sh      : "


def test_seq_sink_marks_received_lines_gaps_and_duplicates():
    prefix = "lp-st-ab12cd34fast-"
    sink = device_probe._SeqSink(prefix, 6)
    # API 24's `log -t` and logwrapper lines may end with a space (C11).
    for line in (
        "--------- beginning of main",
        f"{HEAD}{prefix}start ",
        f"{HEAD}{prefix}0 ",
        f"{HEAD}{prefix}1",
        f"{HEAD}{prefix}1",
        f"{HEAD}{prefix}3 ",
        f"{HEAD}{prefix}5",
        f"{HEAD}{prefix}99",
        f"{HEAD}{prefix}end ",
    ):
        sink(line)
    facts = sink.facts(9)
    assert facts["saw_start"]
    assert facts["saw_end"]
    assert facts["duplicates"] == 1
    assert facts["received"] == 4
    assert facts["missing_ranges"] == [[2, 2], [4, 4]]
    assert facts["missing"] == 2
    assert facts["missing_range_count"] == 2
    assert facts["other_lines"] == ["--------- beginning of main"]
    assert facts["lines_read"] == 9


def _u1a(uid: str, level: str, msg: str = "m") -> str:
    return f"2026-11-04 04:22:19.000000 +0000 {uid:>5}   100   100 {level} tag : {msg}"


def test_sample_keeps_every_uid_token_and_level_past_its_limit():
    lines = [
        *(_u1a("1000", "I") for _ in range(50)),
        _u1a("drm", "F"),
        _u1a("10095", "I", "x" * 500),  # too long for the sample, but a new token
        _u1a("1000", "W"),
    ]
    sample = device_probe.sample_u1a(lines, limit=3)
    parsed = [p for line in sample if (p := parse_u1a(line)) is not None]
    assert {p.uid for p in parsed} == {"1000", "drm", "10095"}
    assert {p.level for p in parsed} == {"I", "F", "W"}
    assert len(sample) == 3 + 3, "three lines up to the limit, then one per new token"


def test_notice_details_mark_the_first_and_last_row():
    lines = [
        "2026-11-04 04:21:49.499885 +0000 10014  1987  1987 I chatty  : uid=10014 x "
        "expire 4 lines",
        _u1a("1000", "I"),
        "2026-11-04 04:22:20.000000 +0000 10014  1987  1987 I chatty  : uid=10014 x "
        "expire 1 line",
    ]
    rows = [(i, p) for i, line in enumerate(lines) if (p := parse_u1a(line))]
    details, _ = device_probe._notice_details(lines, rows, [rows[0], rows[2]])
    assert [item["between_list_neighbors"] for item in details] == [None, None]
    assert [item["count"] for item in details] == [4, 1]


def test_every_probe_has_a_unique_key_and_slug():
    keys = [probe.key for probe in device_probe.PROBES]
    slugs = [probe.slug for probe in device_probe.PROBES]
    assert len(keys) == len(set(keys))
    assert len(slugs) == len(set(slugs))
    assert {p.key for p in device_probe.PROBES if p.needs} == {"C04", "C07", "C09"}
    # C04 kills the emulator, so nothing may run after it.
    assert device_probe.PROBES[-1].key == "C04"


GATED = {"C04", "C07", "C09"}


@pytest.mark.parametrize(
    ("flags", "runs"),
    [
        ([], set()),
        (["--root"], {"C07"}),
        (["--vm-ssh", "vm-wrapper"], set()),
        (["--disruptive"], {"C04"}),
        (["--vm-ssh", "vm-wrapper", "--disruptive"], {"C04", "C09"}),
        (["--root", "--vm-ssh", "vm-wrapper", "--disruptive"], GATED),
    ],
)
@pytest.mark.parametrize("out", [False, True])
def test_main_runs_a_gated_probe_only_with_its_flags(
    monkeypatch, tmp_path, flags, runs, out
):
    ran: list[str] = []
    written: list[tuple[str, object]] = []

    def stub(key):
        def run(ctx):
            ran.append(key)
            return device_probe.Recording(ctx, key, "stub")

        return run

    probes = tuple(dataclasses.replace(p, run=stub(p.key)) for p in device_probe.PROBES)
    monkeypatch.setattr(device_probe, "PROBES", probes)
    monkeypatch.setattr(device_probe, "PROBES_BY_KEY", {p.key: p for p in probes})
    ctx = device_probe.Context(adb=Adb("adb", "emulator-5554"), api=24, device={})
    monkeypatch.setattr(device_probe.Context, "connect", lambda adb, **kwargs: ctx)

    def fake_write(rec, directory, slug):
        written.append((rec.key, directory))
        return directory / f"{rec.key}_{slug}.json"

    monkeypatch.setattr(device_probe, "write_recording", fake_write)
    argv = ["--serial", "emulator-5554", *flags]
    if out:
        argv += ["--out", str(tmp_path)]
    assert device_probe.main(argv) == 0
    expected = [p.key for p in probes if p.key not in GATED or p.key in runs]
    assert ran == expected, "every ungated probe, the granted ones, in order"
    level_dir_ = tmp_path if out else FIXTURES / "api24"
    host_dir = tmp_path / "host" if out else FIXTURES / "host"
    for key, directory in written:
        assert directory == (host_dir if key == "C09" else level_dir_), key


def test_exec_kills_a_command_that_outlives_its_timeout():
    rc, _, _, elapsed, drained = device_probe._exec(SLEEPER, timeout=0.5)
    assert rc is None
    assert drained
    assert elapsed < 10


def test_exec_kills_its_child_when_interrupted(monkeypatch):
    # The child runs in its own session, so a terminal Ctrl-C never reaches
    # it: _exec must end it itself.
    children: list[subprocess.Popen[bytes]] = []

    class Interrupted(subprocess.Popen):
        def wait(self, timeout=None):
            if self not in children:  # the first wait: _exec's wait for the exit
                children.append(self)
                raise KeyboardInterrupt
            return super().wait(timeout)

    monkeypatch.setattr(device_probe.subprocess, "Popen", Interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            device_probe._exec(SLEEPER, timeout=20)
        (child,) = children
        assert child.returncode is not None, "the child was killed and reaped"
        assert child.stdout is not None
        assert child.stderr is not None
        assert child.stdout.closed
        assert child.stderr.closed
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def _fake_get_state(rc, stdout):
    def fake_exec(argv, *, timeout, env=None, stdin=None):
        assert argv[-1] == "get-state"
        return rc, stdout, b"error: device 'emulator-5554 36' not found\n", 0.01, True

    return fake_exec


@pytest.mark.parametrize(
    ("rc", "stdout"),
    [
        # A wrong serial makes every adb command wait for the device (R4), so
        # the context checks get-state first and fails at once.
        (1, b""),
        # rc 0 is not enough: a device in recovery is attached but has no logd.
        (0, b"recovery\n"),
    ],
)
def test_connect_refuses_a_serial_that_is_not_attached(monkeypatch, rc, stdout):
    monkeypatch.setattr(device_probe, "server_reachable", lambda *a, **k: True)
    monkeypatch.setattr(device_probe, "_exec", _fake_get_state(rc, stdout))
    with pytest.raises(device_probe.DeviceNotReadyError, match="not attached"):
        device_probe.Context.connect(Adb("adb", "emulator-5554 36"))


# -- the adb client never starts a server --------------------------------------

# Modeled on the 37.0.1 client in the container with 127.0.0.1:5037 closed
# (FINDINGS.md, "The adb server is never started"): an unset spec, an empty
# host or "localhost" starts a server (here: a marker file); for any other
# host the server is remote, and a refused connection ends the client, rc 1.
FAKE_CLIENT = r"""
import os
import socket
import sys
from pathlib import Path

record = Path(sys.argv[1])
spec = os.environ.get("ADB_SERVER_SOCKET", "")
record.write_text(spec, encoding="utf-8")
host, _, port = spec.removeprefix("tcp:").rpartition(":")
if host in ("", "localhost"):
    (record.parent / "server-started").touch()
    sys.stderr.write(f"* daemon not running; starting now at {spec or 'tcp:5037'}\n")
    sys.exit(0)
try:
    socket.create_connection((host, int(port)), timeout=5).close()
except OSError as exc:
    sys.stderr.write("* cannot start server on remote host\n")
    sys.stderr.write(f"error: cannot connect to daemon at {spec}: {exc}\n")
    sys.exit(1)
print("List of devices attached")
"""


@pytest.fixture
def fake_client(tmp_path, monkeypatch) -> Path:
    """Spawns of ``fake-adb`` run FAKE_CLIENT; returns the file of its spec."""
    script = tmp_path / "fake_adb_client.py"
    script.write_text(FAKE_CLIENT, encoding="utf-8")
    record = tmp_path / "spec.txt"
    real = subprocess.Popen

    class Rewrite(real):
        def __init__(self, args, *rest, **kwargs):
            if args[0] == "fake-adb":
                args = [PYTHON, "-I", str(script), str(record), *args[1:]]
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(device_probe.subprocess, "Popen", Rewrite)
    return record


@pytest.fixture
def tunnel_drops_after_the_check(monkeypatch):
    """A listening "tunnel" that passes the port check and closes right after it."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    real = device_probe.server_reachable

    def check_then_drop(server=device_probe.SERVER, timeout=2.0):
        up = real(server, timeout)
        listener.close()
        return up

    monkeypatch.setattr(device_probe, "server_reachable", check_then_drop)
    yield listener.getsockname()
    listener.close()


@pytest.mark.parametrize("spec", [None, "tcp:5037", "tcp:localhost:5037"])
def test_the_fake_client_starts_a_server_where_adb_does(tmp_path, fake_client, spec):
    # Without this, the two tests below could not tell a numeric spec from one
    # that lets adb start a server.
    env = {k: v for k, v in os.environ.items() if k != "ADB_SERVER_SOCKET"}
    if spec is not None:
        env["ADB_SERVER_SOCKET"] = spec
    script = str(tmp_path / "fake_adb_client.py")
    subprocess.run(
        [PYTHON, "-I", script, str(fake_client), "devices"],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert (tmp_path / "server-started").exists()


def test_a_tunnel_lost_after_the_check_stops_the_run_without_a_server(
    fake_client, tunnel_drops_after_the_check
):
    host, port = tunnel_drops_after_the_check
    adb = Adb("fake-adb", "emulator-5554", server=(host, port))
    with pytest.raises(TunnelDownError, match="could not reach the adb server"):
        adb.run("devices", ["devices"])
    assert fake_client.read_text(encoding="utf-8") == f"tcp:{host}:{port}"
    assert not (fake_client.parent / "server-started").exists()


def test_a_live_reader_whose_tunnel_was_lost_after_the_check_raises(
    fake_client, tunnel_drops_after_the_check
):
    host, port = tunnel_drops_after_the_check
    live = LiveReader(Adb("fake-adb", "emulator-5554", server=(host, port)), "l", [])
    assert live.wait_exit(30) == 1
    with pytest.raises(TunnelDownError, match="could not reach the adb server"):
        live.stop()
    assert fake_client.read_text(encoding="utf-8") == f"tcp:{host}:{port}"
    assert not (fake_client.parent / "server-started").exists()


def test_every_child_gets_a_numeric_server_socket(monkeypatch):
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:localhost:5037")
    assert Adb("adb", "x").env()["ADB_SERVER_SOCKET"] == "tcp:127.0.0.1:5037"
    adb = Adb("adb", "x", server=("127.0.0.1", 6001))
    assert adb.server_socket == "tcp:127.0.0.1:6001"
    env = adb.env({"ANDROID_LOG_TAGS": None})
    assert env["ADB_SERVER_SOCKET"] == "tcp:127.0.0.1:6001"


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "", "emuvm", "::1"])
def test_a_server_host_that_is_not_an_ipv4_address_is_refused(host):
    # adb starts a server of its own for an empty host or "localhost", and a
    # name could resolve to either.
    with pytest.raises(ValueError, match="not an IPv4 address"):
        Adb("adb", "x", server=(host, 5037))


@pytest.mark.parametrize("key", ["ADB_SERVER_SOCKET", "adb_server_socket"])
@pytest.mark.parametrize("value", [None, "tcp:localhost:5037", "tcp:5037"])
def test_no_override_changes_or_removes_the_server_socket(
    monkeypatch, no_spawn, key, value
):
    monkeypatch.setattr(device_probe, "server_reachable", lambda *a, **k: True)
    adb = Adb("adb", "emulator-5554")
    with pytest.raises(ValueError, match="cannot be overridden"):
        adb.env({key: value})
    with pytest.raises(ValueError, match="cannot be overridden"):
        adb.run("x", ["devices"], env={key: value})


# -- children, their descendants and their pipes ---------------------------------


@pytest.mark.parametrize("fail_at", [0, 1, 2])
@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_live_reader_leaves_no_child_when_its_construction_fails(
    monkeypatch, fail_at, error
):
    monkeypatch.setattr(device_probe, "server_reachable", lambda *a, **k: True)
    children: list[subprocess.Popen[Any]] = []
    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        children.append(real_popen(SLEEPER, **kwargs))
        return children[-1]

    started: list[threading.Thread] = []
    real_start = threading.Thread.start

    def failing_start(thread):
        if len(started) == fail_at:
            raise error
        started.append(thread)
        real_start(thread)

    monkeypatch.setattr(device_probe.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(threading.Thread, "start", failing_start)
    try:
        with pytest.raises(error):
            LiveReader(Adb("adb", "emulator-5554"), "live", ["logcat"])
    finally:
        monkeypatch.undo()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    (child,) = children
    assert child.returncode is not None, "the child was killed and reaped"
    assert child.stdout is not None
    assert child.stderr is not None
    assert child.stdout.closed
    assert child.stderr.closed
    assert len(started) == fail_at
    assert not any(thread.is_alive() for thread in started), "a thread was left"


# A child whose own child keeps the child's stdout and stderr open for 60 s:
# it writes the grandchild's pid to argv[1], prints a line, sleeps argv[2] s.
HOLDER = r"""
import subprocess
import sys
import time
from pathlib import Path

grandchild = subprocess.Popen(
    [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
done = Path(sys.argv[1])
part = done.with_suffix(".part")
part.write_text(str(grandchild.pid), encoding="ascii")
part.replace(done)
print("grandchild up", flush=True)
time.sleep(float(sys.argv[2]))
"""


def _holder(pidfile: Path, sleep_s: float) -> list[str]:
    return [PYTHON, "-I", "-c", HOLDER, str(pidfile), str(sleep_s)]


def _wait_for(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} did not appear"
        time.sleep(0.01)


@pytest.fixture
def pidfile(tmp_path):
    """The pid file of a HOLDER grandchild, which is killed after the test."""
    path = tmp_path / "grandchild.pid"
    yield path
    with contextlib.suppress(OSError, ValueError):
        pid = int(path.read_text(encoding="ascii"))
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def test_exec_bounds_the_wait_for_pipes_a_descendant_keeps_open(monkeypatch, pidfile):
    children: list[subprocess.Popen[bytes]] = []

    class ReadyFirst(subprocess.Popen):
        """The timeout starts once the grandchild holds the pipes."""

        def wait(self, timeout=None):
            if self not in children:
                children.append(self)
                _wait_for(pidfile)
            return super().wait(timeout)

    monkeypatch.setattr(device_probe.subprocess, "Popen", ReadyFirst)
    rc, out, _, elapsed, drained = device_probe._exec(
        _holder(pidfile, 30), timeout=0.3, drain=0.5
    )
    monkeypatch.undo()
    (child,) = children
    assert rc is None, "the child outlived its timeout"
    assert child.returncode is not None, "the child was killed and reaped"
    assert not drained, "the grandchild still holds the pipes"
    assert b"grandchild up" in out, "the output read before the deadline is kept"
    assert elapsed < 10, "it did not wait for the grandchild (60 s)"
    if os.name != "nt":  # on Windows the reader keeps a pipe it still reads
        assert child.stdout is not None
        assert child.stdout.closed


def test_live_reader_stop_bounds_the_wait_for_pipes_a_descendant_keeps_open(
    monkeypatch, pidfile
):
    monkeypatch.setattr(device_probe, "server_reachable", lambda *a, **k: True)
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        device_probe.subprocess,
        "Popen",
        lambda argv, **kwargs: real_popen(_holder(pidfile, 30), **kwargs),
    )
    live = LiveReader(Adb("adb", "emulator-5554"), "live", ["logcat"])
    _wait_for(pidfile)
    assert live.wait_lines(1, timeout=20)
    started = time.monotonic()
    result = live.stop(drain=0.5)
    assert time.monotonic() - started < 10, "it did not wait for the grandchild"
    assert live.proc.returncode is not None, "the child was ended and reaped"
    assert result.rc is None
    assert [line.rstrip("\r") for line in result.stdout] == ["grandchild up"]
    assert not result.drained
    assert result.to_json()["drained"] is False


def test_a_step_reports_incomplete_draining_only_when_it_happened():
    step_ = device_probe.Step("x", "adb", [], 0, 0.0, [], [])
    assert "drained" not in step_.to_json()
    assert dataclasses.replace(step_, drained=False).to_json()["drained"] is False


def test_c09_client_wait_returns_at_once_without_a_descendant():
    proc = subprocess.Popen(
        [PYTHON, "-I", "-c", "import sys; sys.stderr.write('x')"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert adb_server_group.finish_client(proc, timeout=20) == (b"x", False, True)


@pytest.mark.skipif(
    os.name == "nt",
    reason="adb_server_group.py runs on the Linux server host only (it reads /proc)",
)
@pytest.mark.parametrize(("client_sleep_s", "killed"), [(30, True), (0, False)])
def test_c09_client_wait_is_bounded_and_kills_only_the_client(
    pidfile, client_sleep_s, killed
):
    proc = subprocess.Popen(
        _holder(pidfile, client_sleep_s),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for(pidfile)
        if not killed:
            proc.wait(20)  # the client exits by itself; its child keeps the pipes
        started = time.monotonic()
        _, hung, drained = adb_server_group.finish_client(proc, timeout=0.3, drain=0.5)
        assert time.monotonic() - started < 5
        assert hung
        assert not drained
        assert proc.returncode == (-signal.SIGKILL if killed else 0)
        assert proc.stdout is not None
        assert proc.stderr is not None
        assert proc.stdout.closed
        assert proc.stderr.closed
        # The descendant (the adb server's place) is still running.
        os.kill(int(pidfile.read_text(encoding="ascii")), 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# -- recordings: VM paths, C04 exit times, C10 clock ---------------------------


def test_vm_steps_store_a_placeholder_for_the_vm_adb_path(monkeypatch, tmp_path):
    path = "/home/alice/android-sdk/platform-tools/adb"
    command = f"{path} kill-server; sleep 1; {path} start-server"
    ran: list[list[str]] = []

    def fake_exec(argv, *, timeout, env=None, stdin=None):
        ran.append(list(argv))
        return 0, f"{path} ok\n".encode(), f"sh: {path}: x\n".encode(), 0.01, True

    monkeypatch.setattr(device_probe, "_exec", fake_exec)
    step_ = device_probe.Vm("vm-wrapper", path).run("server_restart", command)
    assert ran == [["vm-wrapper", command]], "the VM runs the real path"
    assert step_.argv == ["<vm-adb> kill-server; sleep 1; <vm-adb> start-server"]
    assert step_.stdout == ["<vm-adb> ok"]
    assert step_.stderr == ["sh: <vm-adb>: x"]
    ctx = device_probe.Context(adb=Adb("adb", "emulator-5554"), api=36, device={})
    rec = device_probe.Recording(ctx, "C04", "test")
    rec.add(step_)
    device_probe.write_recording(rec, tmp_path, "test")
    check_fixture_hygiene(tmp_path)
    rec.add(dataclasses.replace(step_, argv=[command]))
    device_probe.write_recording(rec, tmp_path, "test")
    with pytest.raises(AssertionError, match="/home/alice"):
        check_fixture_hygiene(tmp_path)


# Prints a line, then exits (rc 255) as soon as argv[1] exists.
EXITS_ON_FLAG = r"""
import sys
import time
from pathlib import Path

print("first line", flush=True)
flag = Path(sys.argv[1])
deadline = time.monotonic() + 20
while not flag.exists() and time.monotonic() < deadline:
    time.sleep(0.005)
sys.exit(255)
"""


def test_stream_case_stamps_an_exit_during_the_action(monkeypatch, tmp_path):
    flag = tmp_path / "act"
    monkeypatch.setattr(device_probe, "server_reachable", lambda *a, **k: True)
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        device_probe.subprocess,
        "Popen",
        lambda argv, **kwargs: real_popen(
            [PYTHON, "-I", "-c", EXITS_ON_FLAG, str(flag)], **kwargs
        ),
    )
    ctx = device_probe.Context(adb=Adb("adb", "emulator-5554"), api=36, device={})
    rec = device_probe.Recording(ctx, "C04", "test")

    def action():
        flag.touch()  # adb logcat exits now ...
        time.sleep(1.0)  # ... while the action goes on (a server restart)

    device_probe._stream_case(ctx, rec, "case", action)
    fact = rec.data["facts"]["case"]
    assert fact["exited_by_itself"]
    assert fact["rc"] == 255
    assert fact["action_s"] >= 0.9  # a coarse monotonic clock on Windows
    assert fact["seconds_to_exit"] < 0.5, "the exit, not the end of the action"


def test_c10_stores_the_host_interval_of_the_clock_read(monkeypatch):
    outputs = {"date +%s": b"1793766304\n", "date +%s.%N": b"1793766304.N\n"}

    def fake_exec(argv, *, timeout, env=None, stdin=None):
        return 0, outputs[argv[-1]], b"", 0.01, True

    monkeypatch.setattr(device_probe, "server_reachable", lambda *a, **k: True)
    monkeypatch.setattr(device_probe, "_exec", fake_exec)
    ctx = device_probe.Context(adb=Adb("adb", "emulator-5554"), api=24, device={})
    rec = device_probe.Recording(ctx, "C10", "test")
    ticks = iter([1793766299.9, 1793766300.1])
    device_probe._clock_facts(ctx, rec, clock=lambda: next(ticks))
    facts = rec.data["facts"]
    assert (facts["host_before_s"], facts["host_after_s"]) == (
        1793766299.9,
        1793766300.1,
    )
    # [d - host_after, d + 1 - host_before] for d = 1793766304
    assert facts["device_minus_host_bounds_s"] == pytest.approx([3.9, 5.1])
    assert facts["device_minus_host_s"] == pytest.approx(4.0)
    assert facts["date_N_supported"] is False
    json.dumps(facts)  # storable as it is
