"""The recorded device facts (``tests/fixtures/device``) hold, without a device.

Each recording is checked with the same assertions the live device tier runs
(``tests/device_facts.py``), plus the properties that only the stored
recording can show, such as an edge case that a live run hits only sometimes.
"""

from __future__ import annotations

import json

import pytest

from tests import device_facts as facts
from tests.device_facts import FIXTURES, LEVELS, body, level_dir, load, step, u1a

# Recordings kept besides the one per check: R2x is the clock step after boot.
EXTRA_KEYS = {24: {"R2x"}, 36: set()}


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("key", sorted(facts.CHECKS))
def test_recording_holds_its_facts(level, key):
    rec = load(level_dir(level), key)
    assert facts.api_of(rec) == level
    facts.CHECKS[key](rec)


@pytest.mark.parametrize("level", LEVELS)
def test_every_level_holds_exactly_the_expected_recordings(level):
    names = [path.name for path in level_dir(level).glob("*.json")]
    keys = [name.partition("_")[0] for name in names]
    assert len(keys) == len(set(keys)), f"two recordings share a key: {names}"
    assert set(keys) == set(facts.CHECKS) | EXTRA_KEYS[level]
    host = [path.name.partition("_")[0] for path in (FIXTURES / "host").glob("*.json")]
    assert host == ["C09"]


def test_server_started_by_our_client_shares_neither_group_nor_pipes():
    facts.check_c09(load(FIXTURES / "host", "C09"))


def test_recordings_stay_small_and_hold_no_host_paths():
    files = list(FIXTURES.rglob("*.json"))
    assert files
    assert sum(path.stat().st_size for path in files) < 1_000_000
    for directory in {path.parent for path in files}:
        facts.check_fixture_hygiene(directory)


@pytest.mark.parametrize(
    "text",
    [
        "/home/alice/android-sdk/platform-tools/adb kill-server",
        "HOME=/home/bob",
        "/Users/carol/Library/Android/sdk/platform-tools/adb",
        "/root/android-sdk/platform-tools/adb",
        "C:\\Users\\dave\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe",
        "c:/users/erin/adb.exe",
        "/tmp/claude-1000/scratchpad/adb",  # noqa: S108
        "Installed as /opt/android/platform-tools/adb",
    ],
)
def test_hygiene_check_rejects_host_paths(tmp_path, text):
    path = tmp_path / "C04_test.json"
    path.write_text(json.dumps({"steps": [{"argv": [text]}]}), encoding="utf-8")
    with pytest.raises(AssertionError, match=r"C04_test\.json contains"):
        facts.check_fixture_hygiene(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "~/android-sdk/platform-tools/adb kill-server",
        "<vm-adb> kill-server; sleep 1; <vm-adb> start-server",
        "/data/user/0/com.android.settings",
        "/system/bin/logcat",
        "/data/home/x",
        "/sysroot/lib",
    ],
)
def test_hygiene_check_accepts_device_paths_and_the_placeholder(tmp_path, text):
    path = tmp_path / "C04_test.json"
    path.write_text(json.dumps({"steps": [{"argv": [text]}]}), encoding="utf-8")
    facts.check_fixture_hygiene(tmp_path)


def test_findings_name_every_recording():
    findings = (FIXTURES / "FINDINGS.md").read_text(encoding="utf-8")
    for path in FIXTURES.rglob("*.json"):
        assert path.name in findings, f"FINDINGS.md does not mention {path.name}"


@pytest.mark.parametrize("level", LEVELS)
def test_u1a_zone_and_uid_columns_parse_on_every_recorded_line(level):
    # The parser of PR-05 starts from these lines: every one has a zone and a uid.
    rec = load(level_dir(level), "R6")
    lines = body(step(rec, "dump")["stdout"])
    assert len(lines) > 100
    zones = {u1a(line)["zone"] for line in lines}
    assert zones == {"+0000"}, "the emulators run in UTC"
    levels = {u1a(line)["level"] for line in lines}
    assert {"V", "D", "I", "W", "E"} <= levels


def test_kernel_lines_on_36_have_empty_or_spaced_tags():
    # API 36 reads the kernel buffer by default; kmsg lines can have an empty tag
    # or a tag with a space, and the U-1 (a) pattern still splits them.
    rec = load(level_dir(36), "R6")
    tags = {u1a(line)["tag"] for line in body(step(rec, "live")["stdout"])}
    assert "" in tags
    assert any(" " in tag for tag in tags)


def test_log_t_message_has_a_trailing_space_on_24_only():
    for level, expected in ((24, " "), (36, "")):
        rec = load(level_dir(level), "C11")
        marker = next(
            line for line in step(rec, "buffer_main")["stdout"] if " logpyt  : " in line
        )
        assert marker.endswith(expected), (level, marker)
        if not expected:
            assert not marker.endswith(" ")


def test_api24_clock_step_after_boot_returns_lines_below_t():
    # 40 s after boot the 7.0 emulator stepped its clock back by about 0.33 s
    # (AlarmManagerService "Setting time of day"). A -T read then started at
    # the first entry newer than T in logd's list order and also returned the
    # older entries logged after the step: lines below T, then the boundary.
    rec = load(level_dir(24), "R2x")
    t_ms = rec["facts"]["T_ms"]
    assert t_ms["lines_below_boundary"] == 40
    assert t_ms["boundary_lines_missing"] == 0, "computed by the tool on all 159 lines"
    returned = body(step(rec, "T_ms")["stdout"])
    assert "Setting time of day" in returned[1]
    stamps = [line[:23] for line in returned]
    assert stamps[0] > t_ms["T"]
    assert sum(1 for stamp in stamps if stamp < t_ms["T"]) == 36, "of the 40 stored"


def test_api24_reader_newer_than_the_ring_can_miss_the_next_line():
    # A live -T reader whose T is newer than every entry missed the first line
    # logged after it connected on 7.0 (not on 16); "since=now" writes its
    # marker before the reader connects, which never missed (check_c02).
    runs24 = load(level_dir(24), "C02")["facts"]["T_after_newest_entry"]
    assert any("m1" not in run["markers_delivered"] for run in runs24)
    assert all({"m2", "m3"} <= set(run["markers_delivered"]) for run in runs24)
    runs36 = load(level_dir(36), "C02")["facts"]["T_after_newest_entry"]
    assert all(run["markers_delivered"] == ["m1", "m2", "m3"] for run in runs36)


def test_pm_list_packages_filter_is_a_substring_match_on_36():
    rec = load(level_dir(36), "C03")
    names = [
        line.split()[0].removeprefix("package:")
        for line in step(rec, "pm_list_packages_U_filter")["stdout"]
    ]
    assert "com.android.settings" in names
    assert any(name != "com.android.settings" for name in names)


def test_shared_uid_packages_exist_on_both_levels():
    for level in LEVELS:
        mapping = load(level_dir(level), "C03")["facts"]["app_uid_tokens_to_packages"]
        assert any(len(names) > 1 for names in mapping.values()), level


def test_flood_lines_are_dropped_at_the_source_and_reported_as_liblog_events():
    # An unthrottled writer loses lines inside liblog (EAGAIN on logd's socket);
    # the count is logged as "liblog" events in the events buffer, not in main.
    for level in LEVELS:
        rec = load(level_dir(level), "C06")
        drops = rec["facts"]["liblog_drop_events"]
        assert drops["lines_dropped"] > 0, level


def test_c05_check_rejects_a_hole_followed_by_newer_lines():
    rec = load(level_dir(24), "C05")
    stalled = rec["facts"]["fast"]["stalled_reader"]
    stalled["saw_end"] = True
    stalled["lines_read"] += 1  # the end marker line
    with pytest.raises(AssertionError, match="end marker came after the hole"):
        facts.check_c05(rec)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r["control_reader"].update(received=149999), "received \\+ missing"),
        (
            lambda r: r["stalled_reader"].update(missing_ranges=[[74146, 149990]]),
            "lengths",
        ),
        (
            lambda r: r["stalled_reader"].update(
                received=74145, missing=75855, missing_ranges=[[74146, 149999]]
            ),
            "lengths",
        ),
        (
            lambda r: r["stalled_reader"].update(lines_read=74000),
            "fewer lines read",
        ),
    ],
)
def test_c05_check_reconciles_the_sequence_summary(change, message):
    rec = load(level_dir(24), "C05")
    change(rec["facts"]["fast"])
    with pytest.raises(AssertionError, match=message):
        facts.check_c05(rec)


def test_c05_check_needs_the_hole_to_start_where_the_prefix_ends():
    rec = load(level_dir(24), "C05")
    stalled = rec["facts"]["fast"]["stalled_reader"]
    # Consistent counts, but lines 0-9 missing and the rest of the tail came.
    stalled["missing_ranges"] = [[0, 75853]]
    with pytest.raises(AssertionError, match="not the tail after the prefix"):
        facts.check_c05(rec)


def test_c06_check_accepts_a_notice_at_either_end_of_the_dump():
    rec = load(level_dir(24), "C06")
    details = rec["facts"]["notice_details"]
    del details[0]["between_list_neighbors"]  # earlier tool revisions
    details[1]["between_list_neighbors"] = None  # the current tool
    facts.check_c06(rec)


def _lose_since_now_marker(rec):
    reader = step(rec, "marker_then_reader_1")
    reader["stdout"] = reader["stdout"][:1]  # the divider only
    reader["stdout_total_lines"] = 1
    rec["facts"]["marker_written_then_reader_delivered"][1] = False


def _dup_quiet_marker(rec):
    reader = step(rec, "quiet_filter")
    reader["stdout"].append(reader["stdout"][-1])
    reader["stdout_total_lines"] += 1


def _set_lines_before_marker(rec, lines, first_byte_s):
    # The quiet reader's output, its byte count and its first byte, consistently.
    reader = step(rec, "quiet_filter")
    marker = body(reader["stdout"])
    reader["stdout"] = [*lines, *marker]
    reader["stdout_total_lines"] = len(reader["stdout"])
    reader["bytes_read"] = sum(len(line.encode()) + 1 for line in reader["stdout"])
    reader["first_byte_s"] = first_byte_s
    quiet = rec["facts"]["quiet_reader"]
    quiet["lines_before_marker"] = list(lines)
    quiet["bytes_before_marker"] = sum(len(line.encode()) + 1 for line in lines)


def test_c10_check_accepts_any_dividers_before_the_marker_on_24_only():
    rec = load(level_dir(24), "C10")
    dividers = ["--------- beginning of system", "--------- beginning of main"]
    _set_lines_before_marker(rec, dividers, 0.05)
    facts.check_c10(rec)
    _set_lines_before_marker(rec, [], 4.03)
    with pytest.raises(AssertionError, match="no divider"):
        facts.check_c10(rec)


@pytest.mark.parametrize(
    ("level", "change", "message"),
    [
        # A partial line before the marker: bytes, not only lines.
        (
            36,
            lambda r: r["facts"]["quiet_reader"].update(bytes_before_marker=9),
            "not whole lines",
        ),
        # Nothing before the marker, but a first byte before it was written.
        (
            36,
            lambda r: step(r, "quiet_filter").update(first_byte_s=0.05),
            "before the marker was written",
        ),
        (24, _dup_quiet_marker, "past the filter"),
        (
            24,
            lambda r: step(r, "log_marker").update(argv=["shell", "log -t x other"]),
            "marker's tag",
        ),
        # %N support comes from the date output, not the stored flag.
        (36, lambda r: r["facts"].update(date_N_supported=False), "disagrees"),
        (24, lambda r: step(r, "date_s_N").update(rc=1), "rc 1"),
        (
            24,
            lambda r: r["facts"].update(spawn_to_first_byte_s=[0.1, 0.1, 0.1]),
            "disagrees with the steps",
        ),
    ],
)
def test_c10_check_rejects_an_inconsistent_recording(level, change, message):
    rec = load(level_dir(level), "C10")
    change(rec)
    with pytest.raises(AssertionError, match=message):
        facts.check_c10(rec)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        # A failed or empty T + 1 us read no longer passes vacuously.
        (lambda r: step(r, "dump_from_T_plus_1us").update(rc=1), "rc 1"),
        (lambda r: step(r, "dump_from_T_plus_1us").update(stdout=[]), "no line"),
        (lambda r: step(r, "dump_from_T").update(stdout=[]), "no line"),
        # A reference line after T that the T + 1 us read did not return.
        (
            lambda r: step(r, "dump_from_T_plus_1us")["stdout"].pop(2),
            "not returned",
        ),
        # A line at T in the T + 1 us read.
        (
            lambda r: step(r, "dump_from_T_plus_1us")["stdout"].insert(
                1, step(r, "dump_from_T")["stdout"][1]
            ),
            "a line below -T",
        ),
        (lambda r: step(r, "dump_from_2001")["stdout"].pop(1), "skipped entries"),
        (lambda r: step(r, "dump_without_T").update(rc=1), "ring start: rc"),
        # The stored flag must agree with the captured lines ...
        (
            lambda r: step(r, "marker_then_reader_1").update(
                stdout=[], stdout_total_lines=0
            ),
            "flags disagree",
        ),
        # ... and the marker must have come.
        (_lose_since_now_marker, "not delivered"),
        (lambda r: step(r, "marker_first_2").update(rc=1), "rc 1"),
    ],
)
@pytest.mark.parametrize("level", LEVELS)
def test_c02_check_reads_the_captured_lines(level, change, message):
    rec = load(level_dir(level), "C02")
    change(rec)
    with pytest.raises(AssertionError, match=message):
        facts.check_c02(rec)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: step(r, "dumpsys_package").update(rc=1), "rc 1"),
        (lambda r: step(r, "dumpsys_package").update(stdout=[]), "records"),
        # The package's own uid differs from its record in the full listing.
        (
            lambda r: step(r, "dumpsys_package")["stdout"].__setitem__(
                1, "    appId=1001"
            ),
            "com.android.settings",
        ),
        (lambda r: r["facts"].update(dumpsys_packages=249), "record count"),
    ],
)
def test_c03_check_validates_the_package_lookup(change, message):
    rec = load(level_dir(36), "C03")
    change(rec)
    with pytest.raises(AssertionError, match=message):
        facts.check_c03(rec)


def test_c03_dumpsys_lists_records_and_fewer_distinct_names_on_36():
    # Three packages have two records each in the API 36 listing (FINDINGS).
    rec = load(level_dir(36), "C03")
    records = facts._dumpsys_records(step(rec, "dumpsys_package_packages")["stdout"])
    names = [name for name, _, _ in records]
    assert (len(names), len(set(names))) == (252, 249)
    assert rec["facts"]["dumpsys_packages"] == 252, "the stored count is of records"


@pytest.mark.parametrize("key", ["R1", "C03", "C10"])
def test_level_specific_checks_skip_on_an_unrecorded_level(key):
    rec = load(level_dir(36), key)
    rec["device"]["ro.build.version.sdk"] = "30"
    with pytest.raises(pytest.skip.Exception, match="recorded only on API"):
        facts.CHECKS[key](rec)


def test_ring_sizes_are_checked_on_the_recorded_build_only():
    rec = load(level_dir(36), "R5")
    rec["device"]["ro.build.fingerprint"] = "google/oriole/oriole:16/BP2A/1:user/rel"
    with pytest.raises(pytest.skip.Exception, match=r"ro\.build\.fingerprint differ"):
        facts.check_r5(rec)


@pytest.mark.parametrize("level", LEVELS)
def test_a_fresh_backlog_shares_timestamps_and_holds_identical_lines(level):
    # Resume needs an occurrence count per timestamp (design 5.6): in the
    # backlog after boot many lines share a microsecond, and byte-identical
    # lines at one timestamp exist.
    rec = load(level_dir(level), "R5")
    stats = rec["facts"]["backlog"]
    assert 0.2 < stats["usec_shared_fraction"] < 1
    assert stats["identical_groups"] > 0
    identical = body(step(rec, "backlog")["stdout"])
    assert len(identical) >= 2
    assert len(set(identical)) == 1
