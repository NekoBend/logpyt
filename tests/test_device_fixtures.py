"""The recorded device facts (``tests/fixtures/device``) hold, without a device.

Each recording is checked with the same assertions the live device tier runs
(``tests/device_facts.py``), plus the properties that only the stored
recording can show, such as an edge case that a live run hits only sometimes.
"""

from __future__ import annotations

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
    rec["facts"]["fast"]["stalled_reader"]["saw_end"] = True
    with pytest.raises(AssertionError, match="end marker came after the hole"):
        facts.check_c05(rec)


def test_c06_check_accepts_a_notice_at_either_end_of_the_dump():
    rec = load(level_dir(24), "C06")
    details = rec["facts"]["notice_details"]
    del details[0]["between_list_neighbors"]  # earlier tool revisions
    details[1]["between_list_neighbors"] = None  # the current tool
    facts.check_c06(rec)


def test_c10_check_accepts_any_dividers_before_the_marker_on_24_only():
    rec = load(level_dir(24), "C10")
    quiet = rec["facts"]["quiet_reader"]
    quiet["lines_before_marker"] = [
        "--------- beginning of system",
        "--------- beginning of main",
    ]
    facts.check_c10(rec)
    quiet["lines_before_marker"] = []
    with pytest.raises(AssertionError, match="no divider"):
        facts.check_c10(rec)


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
