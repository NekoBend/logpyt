"""The device facts logpyt 3.0.0 relies on, written as assertions on recordings.

A recording is the JSON that ``tools/device_probe.py`` writes (one per probe:
``tests/fixtures/device/api<level>/<KEY>_<slug>.json``). The same ``check_*``
function runs in normal CI on the stored fixture (``tests/test_device_fixtures.py``)
and against a live device on a fresh recording (``tests/device/``), so a
regression on a device shows up as the same failed assertion.

The regular expressions here are the contract, written from the AOSP format
strings and the recordings, independently of the probe tool's own parsing.
``FINDINGS.md`` next to the fixtures explains what each fact means for the
design.

Only API 24 and 36 were recorded, so an expectation that differs between them
is keyed on those two levels: on any other level the check runs its common
assertions and then skips with the reason (:func:`recorded_level`). Facts of
the emulator image (ring sizes) are compared only on the build that was
recorded (:func:`skip_unless_recorded_build`).
"""

from __future__ import annotations

import calendar
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "device"
LEVELS = (24, 36)
DIVIDER = "--------- beginning of "

# The U-1 (a) wire format: -v threadtime -v year -v usec -v zone -v uid.
U1A_MODIFIERS = ["threadtime", "year", "usec", "zone", "uid"]
U1A_LINE = re.compile(
    r"^(?P<date>\d{4}-\d\d-\d\d) (?P<time>\d\d:\d\d:\d\d\.\d{6}) (?P<zone>[+-]\d{4}) "
    r"+(?P<uid>\S+) +(?P<pid>\d+) +(?P<tid>\d+) (?P<level>[VDIWEF]) "
    r"(?P<tag>.*?) *: (?P<msg>.*)$"
)
# The uid column: a number, an app or isolated name (later releases), or a
# short AID name (5 characters at most: logprint.c).
UID_TOKEN = re.compile(r"^(?:\d+|u\d+_[ai]\d+|[a-z_]{1,5})$")
_THREADTIME_TAIL = r" +\d+ +\d+ [VDIWEF] .*?: "
SHAPES = {
    "threadtime": re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}" + _THREADTIME_TAIL),
    "year": re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}" + _THREADTIME_TAIL),
    "year_usec": re.compile(
        r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{6}" + _THREADTIME_TAIL
    ),
    "year_nsec": re.compile(
        r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{9}" + _THREADTIME_TAIL
    ),
    "year_zone": re.compile(
        r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} [+-]\d{4}" + _THREADTIME_TAIL
    ),
    "epoch": re.compile(r"^ *\d+\.\d{3}" + _THREADTIME_TAIL),
    "epoch_usec": re.compile(r"^ *\d+\.\d{6}" + _THREADTIME_TAIL),
    "uid": re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} +\S+" + _THREADTIME_TAIL),
}
PROCESS_LINE = re.compile(r"^[VDIWEF]\( *\d+\) .*  \((?P<tag>[^()]*)\)$")
PM_U_LINE = re.compile(r"^package:(?P<name>\S+) uid:(?P<uid>\d+)$")
# Design 2.7 (F5-3): the Package condition's name grammar.
PACKAGE_NAME = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*(:[A-Za-z0-9_.]+)?$"
)
CHATTY = re.compile(r"^uid=\d+(\([^)]*\))?( \S+)? expire \d+ lines?$")


# -- loading -----------------------------------------------------------------


def level_dir(level: int) -> Path:
    """Return the fixture directory of an API level."""
    return FIXTURES / f"api{level}"


def load(directory: Path, key: str) -> dict[str, Any]:
    """Load the recording ``<key>_*.json`` from ``directory``."""
    matches = sorted(directory.glob(f"{key}_*.json"))
    assert len(matches) == 1, f"expected one {key}_*.json in {directory}, got {matches}"
    return json.loads(matches[0].read_text(encoding="utf-8"))


def step(rec: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the step called ``name``."""
    found = [s for s in rec["steps"] if s["name"] == name]
    assert found, (
        f"{rec['id']}: no step {name!r} in {[s['name'] for s in rec['steps']]}"
    )
    return found[0]


def body(lines: list[str]) -> list[str]:
    """``lines`` without ``--------- beginning of <buffer>`` dividers."""
    return [line for line in lines if not line.startswith("--------- ")]


def api_of(rec: dict[str, Any]) -> int:
    """Return the API level the recording was made on."""
    return int(rec["device"]["ro.build.version.sdk"])


def recorded_level(rec: dict[str, Any], what: str) -> int:
    """Return the API level if it was recorded (24 or 36); skip otherwise."""
    api = api_of(rec)
    if api not in LEVELS:
        pytest.skip(
            f"{rec['id']}: {what} was recorded only on API {LEVELS}, not on API {api}"
        )
    return api


BUILD_PROPS = ("ro.build.version.sdk", "ro.product.cpu.abi", "ro.build.fingerprint")


def skip_unless_recorded_build(rec: dict[str, Any], what: str) -> None:
    """Skip unless ``rec`` comes from the build its level's recording was made on."""
    api = recorded_level(rec, what)
    stored = load(level_dir(api), rec["id"])["device"]
    differs = {p for p in BUILD_PROPS if rec["device"].get(p) != stored.get(p)}
    if differs:
        pytest.skip(
            f"{rec['id']}: {what} holds for the recorded emulator build only "
            f"({', '.join(sorted(differs))} differ)"
        )


def u1a(line: str) -> re.Match[str]:
    """Match ``line`` against the U-1 (a) format, failing with the line."""
    m = U1A_LINE.match(line)
    assert m is not None, f"not a U-1 (a) line: {line!r}"
    return m


def epoch_us(line: str) -> int:
    """Return the epoch microseconds of a U-1 (a) line (zone applied)."""
    m = u1a(line)
    year, month, day = (int(x) for x in m["date"].split("-"))
    clock, usec = m["time"].split(".")
    hour, minute, second = (int(x) for x in clock.split(":"))
    zone = m["zone"]
    sign = 1 if zone[0] == "+" else -1
    offset = sign * (int(zone[1:3]) * 3600 + int(zone[3:5]) * 60)
    seconds = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    return (seconds - offset) * 1_000_000 + int(usec)


def parse_t(value: str) -> int:
    """Parse a ``-T <sec>.<usec>`` value into epoch microseconds."""
    seconds, _, fraction = value.partition(".")
    assert len(fraction) == 6, f"-T value without six fraction digits: {value!r}"
    return int(seconds) * 1_000_000 + int(fraction)


# -- R1-R6 -------------------------------------------------------------------


def _check_shape(rec: dict[str, Any], name: str, shape: str) -> None:
    lines = body(step(rec, name)["stdout"])
    assert lines, f"R1 {name}: no lines"
    for line in lines:
        assert SHAPES[shape].match(line), f"R1 {name}: {line!r} is not {shape}"


def check_r1(rec: dict[str, Any]) -> None:
    """R1: each ``-v`` combination prints the documented shape."""
    expected = {
        "threadtime": "threadtime",
        "default": "threadtime",
        "threadtime+year": "year",
        "threadtime+year+usec": "year_usec",
        "threadtime+epoch": "epoch",
        "threadtime+epoch+usec": "epoch_usec",
        "threadtime+uid": "uid",
        # Two zone-selecting words toggle the zone off again (logprint.c:258).
        "threadtime+year+UTC+zone": "year",
    }
    for name, shape in expected.items():
        _check_shape(rec, name, shape)
    api = recorded_level(rec, "-v nsec")
    # nsec does not exist on 7.0: the unknown word is taken as a time zone.
    _check_shape(rec, "threadtime+year+nsec", {24: "year_zone", 36: "year_nsec"}[api])


def _ms_key(line: str) -> str | None:
    return line[:23] if SHAPES["year_usec"].match(line) else None


def _us_key(line: str) -> str | None:
    return line[:26] if SHAPES["year_usec"].match(line) else None


def check_r2(rec: dict[str, Any]) -> None:
    """R2: ``-T`` in date form is inclusive; epoch form accepted; digits count."""
    reference = body(step(rec, "reference")["stdout"])
    for name, key in (("T_ms", _ms_key), ("T_usec", _us_key)):
        t = rec["facts"][name]["T"]
        returned = body(step(rec, name)["stdout"])
        at_t = Counter(line for line in reference if key(line) == t)
        assert at_t, f"R2 {name}: the boundary {t} is not in the reference"
        assert not at_t - Counter(returned), f"R2 {name}: -T {t} is not inclusive"
        first = next(k for line in returned if (k := key(line)) is not None)
        assert first >= t, f"R2 {name}: first line {first} is before -T {t}"
    epoch = step(rec, "T_epoch_ms")
    assert epoch["rc"] == 0
    stamps = [float(line.split()[0]) for line in body(epoch["stdout"])]
    assert stamps, "R2: -T <sec>.<ms> returned nothing"
    assert stamps[0] >= float(rec["facts"]["T_epoch_ms"]["T"])
    # A digits-only -T value is a line count, never a time (it would be 1970).
    assert 0 < rec["facts"]["T_digits_5_lines"] < 100


def check_r3(rec: dict[str, Any]) -> None:
    """R3: ``adb shell pidof`` hands its argument to the device shell (F5-3)."""
    meta = step(rec, "metachar")
    assert meta["rc"] == 0
    assert meta["stdout"] == ["INJECTED_VIA_SEMICOLON"], (
        "the shell ran the injected echo"
    )
    two = step(rec, "two_names_one_arg")
    assert len(two["stdout"][0].split()) == 2, (
        "one argument with a space named two processes"
    )
    unknown = step(rec, "unknown")
    assert unknown["rc"] == 1
    assert unknown["stdout"] == []


def check_r4(rec: dict[str, Any]) -> None:
    """R4: for an absent serial, logcat waits; get-state and shell fail fast."""
    for name in ("logcat_dump", "logcat_live"):
        s = step(rec, name)
        assert s["timed_out"], f"R4 {name}: adb logcat exited instead of waiting"
        assert s["stderr"] == ["- waiting for device -"]
    for name in ("get_state", "shell_date"):
        s = step(rec, name)
        assert s["rc"] == 1, f"R4 {name}: rc {s['rc']}"
        assert s["elapsed_s"] < 5
        assert any("not found" in line for line in s["stderr"])


def check_r5(rec: dict[str, Any]) -> None:
    """R5: ring sizes per buffer; collisions and identical lines at one timestamp."""
    stats = rec["facts"]["backlog"]
    assert stats["lines"] > 1000
    assert stats["parsed"] == stats["lines"]
    assert stats["ms_shared_fraction"] >= stats["usec_shared_fraction"]
    # The ring size is a setting of the emulator image (36 raises it after boot).
    skip_unless_recorded_build(rec, "the ring size")
    size = {24: "256Kb", 36: "2 MiB"}[api_of(rec)]
    rings = step(rec, "ring_default")["stdout"]
    for buffer in ("main", "system", "crash"):
        assert any(
            line.startswith(f"{buffer}: ring buffer is {size}") for line in rings
        ), f"R5: {buffer} ring is not {size}: {rings}"


def check_u1a_lines(lines: list[str]) -> Counter[str]:
    """Every line matches the U-1 (a) format; return the uid tokens seen."""
    tokens: Counter[str] = Counter()
    for line in body(lines):
        m = u1a(line)
        assert UID_TOKEN.match(m["uid"]), (
            f"unexpected uid column {m['uid']!r}: {line!r}"
        )
        tokens[m["uid"]] += 1
    return tokens


def check_r6(rec: dict[str, Any]) -> None:
    """R6 (open check 1): the U-1 (a) argv prints zone and uid on every line."""
    dump = step(rec, "dump")
    argv = dump["argv"]
    modifiers = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-v"]
    assert modifiers == U1A_MODIFIERS
    assert rec["facts"]["unmatched_lines"] == 0
    tokens = check_u1a_lines(dump["stdout"])
    assert any(t.isdigit() and int(t) >= 10000 for t in tokens), "no app uid lines"
    assert any(not t.isdigit() for t in tokens), "no AID name in the uid column"
    live = step(rec, "live")
    assert len(body(live["stdout"])) >= 10
    check_u1a_lines(live["stdout"])
    for zone in rec["facts"]["zones"]:
        assert re.fullmatch(r"[+-]\d{4}", zone)


# -- C02-C12 -----------------------------------------------------------------


def check_c02(rec: dict[str, Any]) -> None:
    """C02: ``-T <sec>.<usec>`` returns every line at T, none at T exactly +1 us."""
    t = parse_t(rec["facts"]["T"])
    reference = body(step(rec, "reference")["stdout"])
    at_t = Counter(line for line in reference if epoch_us(line) == t)
    assert at_t, "the boundary timestamp is not in the reference"
    for name in ("dump_from_T", "live_from_T"):
        returned = body(step(rec, name)["stdout"])
        assert not at_t - Counter(returned), f"C02 {name}: not inclusive at T"
        assert epoch_us(returned[0]) == t, f"C02 {name}: first line is not at T"
    above = body(step(rec, "dump_from_T_plus_1us")["stdout"])
    assert not any(epoch_us(line) == t for line in above), "-T T+1us returned T"
    early = rec["facts"]["before_ring_start"]
    assert early["rc"] == 0
    assert early["same_first_entry"], "-T before the ring start returns the whole ring"
    # The since="now" order (marker, then -T <clock - 1 s>) delivered every time.
    assert rec["facts"]["marker_written_then_reader_delivered"] == [True, True, True]


def check_c03(rec: dict[str, Any]) -> None:
    """C03: app lines carry numeric uids; the package-to-uid command per level."""
    app_lines = body(step(rec, "dump")["stdout"])
    assert app_lines
    for line in app_lines:
        assert u1a(line)["uid"].isdigit(), f"app uid printed as a name: {line!r}"
    listing = step(rec, "pm_list_packages_U")
    no_u_option = listing["stdout"] == ["Error: Unknown option: -U"]
    if no_u_option:
        assert listing["rc"] == 0, "the unsupported option still exits 0"
        names = [
            line.removeprefix("package:")
            for line in step(rec, "pm_list_packages")["stdout"]
        ]
    else:
        names = []
        for line in listing["stdout"]:
            m = PM_U_LINE.match(line)
            assert m, f"pm list packages -U line {line!r}"
            names.append(m["name"])
        filtered = step(rec, "pm_list_packages_U_filter")["stdout"]
        assert len(filtered) > 1, "the filter argument is a substring match"
    assert len(names) > 50
    for name in names:
        assert PACKAGE_NAME.match(name), f"package name {name!r} fails the F5-3 grammar"
    dumpsys = step(rec, "dumpsys_package_packages")["stdout"]
    keys = {
        line.strip().partition("=")[0]
        for line in dumpsys
        if line.strip().startswith(("userId=", "appId="))
    }
    assert len(keys) == 1, f"dumpsys package packages: uid keys {keys}"
    mapping = rec["facts"]["app_uid_tokens_to_packages"]
    assert mapping
    assert all(mapping.values()), "every app uid in the log maps to a package"
    api = recorded_level(rec, "the package-to-uid commands")
    assert no_u_option is (api == 24), "pm list packages -U exists on 36 only"
    assert keys == {{24: "userId", 36: "appId"}[api]}


def check_c08(rec: dict[str, Any]) -> None:
    """C08: ``-v process`` ends each line with two spaces and ``(tag)``."""
    lines = body(step(rec, "process")["stdout"])
    assert lines
    for line in lines:
        assert PROCESS_LINE.match(line), f"-v process line {line!r}"


def check_c10(rec: dict[str, Any]) -> None:
    """C10: device clock commands; output starts within a second of the spawn."""
    facts = rec["facts"]
    assert facts["date_s"].isdigit()
    assert all(x is not None and x < 2 for x in facts["spawn_to_first_byte_s"])
    quiet = facts["quiet_reader"]
    assert quiet["marker_write_to_arrival_s"] is not None
    assert quiet["marker_write_to_arrival_s"] < 2
    api = recorded_level(rec, "date +%s.%N and the filtered reader's output")
    assert facts["date_N_supported"] is (api == 36)
    before = quiet["lines_before_marker"]
    if api == 24:
        # 7.0 prints one divider per buffer as soon as logd delivers an entry of
        # that buffer at or after T, whatever the filter (C12 tags_silent shows
        # the same). The tool logs an unmatched main line before it connects,
        # so the main divider comes at once; without an entry at or after T,
        # 7.0 prints nothing (FINDINGS.md, check 10).
        assert before, "7.0 printed no divider before the first matching line"
        assert all(line.startswith(DIVIDER) for line in before), before
        assert f"{DIVIDER}main" in before
    else:
        assert before == [], "16 prints nothing before the first matching line"


def check_c11(rec: dict[str, Any]) -> None:
    """C11: logwrapper tags and bursts; ``log -t`` writes to the main buffer only."""
    facts = rec["facts"]
    assert facts["lines_in_system"] == 0
    assert facts["lines_in_crash"] == 0
    main = [u1a(line) for line in step(rec, "buffer_main")["stdout"]]
    burst = [m for m in main if m["msg"].startswith("lp-burst-")]
    assert len(burst) == 8
    assert len({(m["pid"], m["tid"], m["tag"], m["msg"]) for m in burst}) == 1
    assert {m["tag"] for m in burst} == {"sh"}
    assert any(m["tag"] == "echo" and m["msg"].startswith("lp-lw-") for m in main)
    marker = [m for m in main if m["tag"] == "logpyt"]
    assert len(marker) == 1
    assert marker[0]["msg"].split() == [marker[0]["msg"].strip()], "one token"


def check_c12(rec: dict[str, Any]) -> None:
    """C12: the host's ANDROID_LOG_TAGS reaches the device logcat."""
    facts = rec["facts"]
    assert facts["tags_silent"]["lines"] == 0
    assert set(facts["tags_logpyt_only"]["top_tags"]) == {"logpyt"}
    assert facts["tags_removed"]["distinct_tags"] > 5
    assert facts["tags_silent_with_filterspec"]["distinct_tags"] > 5, (
        "a filterspec on the command line overrides ANDROID_LOG_TAGS"
    )


def check_c06(rec: dict[str, Any]) -> None:
    """C06: chatty notices on 7.0 (none on 16): text, tag, timestamp position."""
    api = recorded_level(rec, "chatty notices")
    facts = rec["facts"]
    if api == 36:
        # The flood made logd prune (the oldest own lines are gone), and the
        # dump still holds no notice line.
        assert facts["own_seq_kept_range"][0] > 0, "the ring did not prune"
        assert facts["notices"] == 0
        return
    assert facts["notices"] > 0
    details = facts["notice_details"]
    for item in details:
        m = u1a(item["line"])
        assert m["tag"] == "chatty"
        assert m["level"] == "I"
        assert CHATTY.match(m["msg"]), m["msg"]
        # None (or no key, earlier tool revisions): the first or last row.
        if item.get("between_list_neighbors") is not None:
            assert item["between_list_neighbors"], "the notice has a pruned line's time"
    assert any(item.get("between_list_neighbors") for item in details)
    assert facts["notices_in_resume_from_1s_before_first_notice"] > 0


def check_c05(rec: dict[str, Any]) -> None:
    """C05: a reader that falls behind loses an unbroken tail, then gets EOF.

    The control reader gets every line, so nothing was lost at the source. The
    reader that stops reading (``fast``, ``slow``) or reads slowly
    (``slow_reader``) gets either every line, or an unbroken prefix and then
    EOF (rc 1) once it has drained what was buffered: never a hole followed by
    newer lines, the end marker included.

    Not shown (FINDINGS.md, check 5): which logd branch dropped the reader. A
    skip followed by a release also leaves one missing tail, so the silent
    skip is not excluded. Whether a notice marks the loss is not observable
    either: both readers filter ``-s sh:V``, which drops ``chatty`` lines.
    """
    facts = rec["facts"]
    for run in ("fast", "slow", "slow_reader"):
        result = facts[run]
        control, stalled = result["control_reader"], result["stalled_reader"]
        assert control["missing"] == 0, f"C05 {run}: the source lost lines"
        assert control["saw_end"]
        # adb blocks writing into our full pipe whatever logd does meanwhile.
        assert result["stalled_reader_rc_during_stall"] is None, (
            "adb still blocked on our pipe"
        )
        assert stalled["duplicates"] == control["duplicates"] == 0
        # Besides our own lines, only dividers pass the sh:V filter.
        assert all(line.startswith("--------- ") for line in stalled["other_lines"])
        reader = step(rec, f"{run}_stalled_reader")
        if stalled["missing"] == 0:
            assert stalled["saw_end"]
            continue
        assert stalled["missing_range_count"] == 1, f"C05 {run}: a hole, then lines"
        assert stalled["missing_ranges"][0][1] == result["lines_written"] - 1
        assert not stalled["saw_end"], f"C05 {run}: the end marker came after the hole"
        assert result["stalled_reader_rc_after_resume"] == 1, "disconnected (EOF)"
        assert reader["rc"] == 1
        assert any("nexpected EOF" in line for line in reader["stderr"])
    for run in ("fast", "slow"):
        assert facts[run]["stalled_reader"]["missing"] > 0, (
            f"C05 {run} did not overflow"
        )


def check_c04(rec: dict[str, Any]) -> None:
    """C04: how a running ``adb logcat`` ends on each kind of disconnect."""
    facts = rec["facts"]
    killed = facts["device_logcat_killed"]
    assert killed["exited_by_itself"]
    assert killed["rc"] == 143, "128 + SIGTERM, passed through the shell protocol"
    for case in ("device_logcat_killed", "adb_reconnect", "server_restart", "unplug"):
        result = facts[case]
        assert result["exited_by_itself"], f"C04 {case}: adb logcat kept running"
        assert result["seconds_to_exit"] < 5
        assert result["stderr"] == [], f"C04 {case}: adb printed {result['stderr']}"
        if case != "device_logcat_killed":
            assert result["rc"] == 255, f"C04 {case}: rc {result['rc']}"
    for case in ("get_state_after_unplug", "shell_date_after_unplug"):
        assert facts[case]["rc"] == 1
        assert any("device offline" in line for line in facts[case]["stderr"])
    waiting = facts["logcat_after_unplug"]
    assert not waiting["exited"], "adb logcat waits for the unplugged serial"
    assert waiting["stderr"] == ["- waiting for device -"]


def _lines(rec: dict[str, Any], name: str) -> list[re.Match[str]]:
    return [u1a(line) for line in body(step(rec, name)["stdout"])]


def check_c07(rec: dict[str, Any]) -> None:
    """C07: default buffers; where native crash, Java crash and ANR lines go.

    The test app (``com.android.settings``) runs as the system uid 1000, so
    these recordings cannot tell the app's uid from that of the crash_dump and
    DEBUG lines for an app with an app uid (FINDINGS.md, open items).
    """
    api = recorded_level(rec, "the default buffers and the crash lines")
    buffers = [line.split(":")[0] for line in step(rec, "buffers_default")["stdout"]]
    assert (
        buffers
        == {
            24: ["main", "system", "crash"],
            36: ["main", "system", "crash", "kernel"],
        }[api]
    )
    pid = str(rec["facts"]["native_crash"]["pid"])
    crash = _lines(rec, "native_crash_crash")
    fatal = [m for m in crash if m["tag"] == "libc" and "Fatal signal 11" in m["msg"]]
    assert len(fatal) == 1
    assert fatal[0]["level"] == "F"
    assert fatal[0]["pid"] == pid, "libc logs from the crashing process"
    assert fatal[0]["uid"] == "1000", "with the app's uid (Settings runs as system)"
    debug = [m for m in crash if m["tag"] == "DEBUG"]
    assert any(">>> com.android.settings <<<" in m["msg"] for m in debug)
    assert all(m["pid"] != pid for m in debug), "the tombstone comes from another pid"
    am_crash = step(rec, "am_crash")
    no_am_crash = am_crash["stderr"][:1] == ["usage: am [subcommand] [options]"]
    java = [
        m
        for m in _lines(rec, "java_crash_am_crash_crash")
        if m["tag"] == "AndroidRuntime" and m["level"] == "E"
    ]
    if api == 24 and no_am_crash:
        # 7.0 has no `am crash` (it printed its usage): no Java crash induced.
        assert java == []
    else:
        java_pid = str(rec["facts"]["java_crash_am_crash"]["pid"])
        assert len(java) >= 2, "am crash logged no Java crash block"
        assert java[0]["msg"] == "FATAL EXCEPTION: main"
        assert java[1]["msg"] == f"Process: com.android.settings, PID: {java_pid}"
        assert {(m["pid"], m["uid"]) for m in java} == {(java_pid, "1000")}, (
            "the app logs its Java crash from its own pid and uid"
        )
        assert len({(m["date"], m["time"], m["pid"]) for m in java}) == 1, (
            "the whole trace is one entry: every line has the same header"
        )
    anr = [
        m
        for name in ("anr_main", "anr_system")
        for m in _lines(rec, name)
        if m["msg"].startswith("ANR in com.android.settings")
    ]
    if anr or api != 24:  # on 7.0, SIGSTOP plus input induced no ANR line
        assert len(anr) == 1
        assert anr[0]["tag"] == "ActivityManager"
        assert anr[0]["level"] == "E"
        assert anr[0]["pid"] != str(rec["facts"]["anr"]["pid"]), "system_server logs it"


def check_c09(rec: dict[str, Any]) -> None:
    """C09: a server started by our client has its own session and no client pipe.

    Measured on the Linux server host only; the Windows case (does the server
    inherit the client's pipe handles?) is open (FINDINGS.md).
    """
    runs = rec["facts"]["result"]["runs"]
    assert [run["client_new_session"] for run in runs] == [False, True]
    for run in runs:
        assert not run["client_hung"]
        assert run["client_rc"] == 0
        assert "* daemon started successfully" in run["client_stderr"]
        (server,) = run["servers"]
        assert server["ppid"] == 1
        assert server["pgid"] == server["sid"] == server["pid"], "setsid()"
        assert not server["same_pgid_as_client"]
        assert not server["holds_client_pipe"]
        assert server["fd0"] == "/dev/null"


def check_fixture_hygiene(directory: Path) -> None:
    """No host paths or adb install lines in the recordings of ``directory``."""
    for path in directory.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        for needle in ("/tmp/claude", "/home/dev", "Installed as", "C:\\\\Users"):  # noqa: S108
            assert needle not in text, f"{path.name} contains {needle!r}"


CHECKS = {
    "R1": check_r1,
    "R2": check_r2,
    "R3": check_r3,
    "R4": check_r4,
    "R5": check_r5,
    "R6": check_r6,
    "C02": check_c02,
    "C03": check_c03,
    "C04": check_c04,
    "C05": check_c05,
    "C06": check_c06,
    "C07": check_c07,
    "C08": check_c08,
    "C10": check_c10,
    "C11": check_c11,
    "C12": check_c12,
}
