"""``tools/device_probe.py`` without a device: its guard and its pure helpers."""

from __future__ import annotations

import calendar
import dataclasses
import socket
import subprocess
import sys
import time

import pytest

from tests.device_facts import FIXTURES, body, level_dir, load, step
from tools import device_probe
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

# A closed loopback port refuses at once on Linux; on Windows a refused
# connect is retried for about 2 s, so the tests give it less.
FAST_CONNECT = 0.2


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
        return 0, b"", b"", 0.0

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
        return 0, b"a\nb\r\n", b"", 0.01

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
    rc, _, _, elapsed = device_probe._exec(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
    )
    assert rc is None
    assert elapsed < 10


def test_exec_kills_its_child_when_interrupted(monkeypatch):
    # The child runs in its own session, so a terminal Ctrl-C never reaches
    # it: _exec must end it itself.
    children: list[subprocess.Popen[bytes]] = []

    class Interrupted(subprocess.Popen):
        def communicate(self, *args, **kwargs):
            children.append(self)
            raise KeyboardInterrupt

    monkeypatch.setattr(device_probe.subprocess, "Popen", Interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            device_probe._exec(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=20
            )
        (child,) = children
        assert child.returncode is not None, "the child was killed and reaped"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


def _fake_get_state(rc, stdout):
    def fake_exec(argv, *, timeout, env=None, stdin=None):
        assert argv[-1] == "get-state"
        return rc, stdout, b"error: device 'emulator-5554 36' not found\n", 0.01

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
