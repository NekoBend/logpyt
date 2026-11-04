"""CrashCondition and AnrCondition against the device recordings of PR-02.

``tests/fixtures/device/api{24,36}/C07_crash_anr.json`` holds the lines Android
logged for a native crash (``kill -SEGV``), a Java crash (``am crash``, API 36
only: 7.0 has no ``am crash``) and an ANR (API 36 only: 7.0 logged none) of
``com.android.settings`` (FINDINGS.md, open check 7). Every logcat line of the
recording is parsed with ``ThreadTimeLogParser``, as a live stream or
``read_file`` would, and the conditions must pick exactly the lines design 2.7
and amendment A.11 name: the Java ``Process: <name>, PID: <pid>`` line, the
native tombstone line ``pid: .., tid: .., name: ..  >>> <name> <<<`` (DEBUG, F)
and ``ANR in <name> (...)`` (ActivityManager, E).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from logpyt import read_file
from logpyt.filters import AnrCondition, CrashCondition, Package
from logpyt.parsers import ParseContext, ThreadTimeLogParser
from tests.device_facts import LEVELS, body, level_dir, load

if TYPE_CHECKING:
    from pathlib import Path

    from logpyt.filters import Condition
    from logpyt.models import LogEntry

APP = "com.android.settings"
# Names that share a prefix with APP but are other packages.
NEAR_MISSES = (
    "com.android",
    "com.android.setting",
    "com.android.settings.x",
    "android",
)
RECORDED_YEAR = 2026

type Match = tuple[str, str, str, str]  # (step, tag, level, message)


def _recording(level: int) -> dict[str, Any]:
    return load(level_dir(level), "C07")


def _logcat_lines(rec: dict[str, Any]) -> list[tuple[str, str]]:
    """``(step name, line)`` of every logcat line in the recording, in step order."""
    return [
        (st["name"], line)
        for st in rec["steps"]
        if "logcat" in st["argv"] and "-g" not in st["argv"]
        for line in body(st["stdout"])
    ]


def _entries(rec: dict[str, Any]) -> list[tuple[str, LogEntry]]:
    parser = ThreadTimeLogParser()
    parsed = []
    for name, line in _logcat_lines(rec):
        entry = parser.parse(line, ParseContext(year=RECORDED_YEAR))
        assert entry is not None, f"{name}: {line!r}"
        parsed.append((name, entry))
    return parsed


def _matches(condition: Condition, entries: list[tuple[str, LogEntry]]) -> list[Match]:
    return [
        (name, entry.tag, entry.level, entry.message)
        for name, entry in entries
        if condition(entry)
    ]


def _native_crash(rec: dict[str, Any]) -> Match:
    pid = rec["facts"]["native_crash"]["pid"]
    message = f"pid: {pid}, tid: {pid}, name: ndroid.settings  >>> {APP} <<<"
    return ("native_crash_crash", "DEBUG", "F", message)


def _java_crash(rec: dict[str, Any]) -> Match:
    pid = rec["facts"]["java_crash_am_crash"]["pid"]
    message = f"Process: {APP}, PID: {pid}"
    return ("java_crash_am_crash_crash", "AndroidRuntime", "E", message)


def _anr() -> Match:
    return ("anr_system", "ActivityManager", "E", f"ANR in {APP} ({APP}/.Settings)")


def _expected_crashes(level: int, rec: dict[str, Any]) -> list[Match]:
    # API 24 recorded no Java crash (no `am crash` on 7.0).
    return {24: [_native_crash(rec)], 36: [_native_crash(rec), _java_crash(rec)]}[level]


def _expected_anrs(level: int) -> list[Match]:
    # API 24 logged no `ANR in` line for the stopped app (FINDINGS.md, check 7).
    return {24: [], 36: [_anr()]}[level]


@pytest.mark.parametrize("level", LEVELS)
def test_crash_condition_with_package_finds_the_recorded_crashes(level: int) -> None:
    """CrashCondition(package=app) matches the native tombstone line (A.11) and the
    Java crash's Process line, and nothing else, without attribution.
    """
    rec = _recording(level)
    entries = _entries(rec)

    assert _matches(CrashCondition(package=APP), entries) == _expected_crashes(
        level, rec
    )
    assert all(entry.package is None for _, entry in entries)


@pytest.mark.parametrize("level", LEVELS)
def test_native_tombstone_is_not_logged_by_the_crashed_pid(level: int) -> None:
    """The tombstone line comes from crash_dump/debuggerd, not from the app's pid, so
    only its text names the app: pid-based attribution would miss it.
    """
    rec = _recording(level)
    crashed_pid = rec["facts"]["native_crash"]["pid"]
    native = [
        entry
        for _, entry in _entries(rec)
        if entry.tag == "DEBUG" and CrashCondition(package=APP)(entry)
    ]

    assert len(native) == 1
    assert native[0].pid != crashed_pid


@pytest.mark.parametrize("level", LEVELS)
def test_crash_condition_without_package_matches_fatal_exception(level: int) -> None:
    """CrashCondition() matches only the FATAL EXCEPTION line of the Java crash."""
    rec = _recording(level)
    expected = {
        24: [],
        36: [
            (
                "java_crash_am_crash_crash",
                "AndroidRuntime",
                "E",
                "FATAL EXCEPTION: main",
            )
        ],
    }[level]

    assert _matches(CrashCondition(), _entries(rec)) == expected


@pytest.mark.parametrize("level", LEVELS)
def test_anr_condition_finds_the_recorded_anr(level: int) -> None:
    """AnrCondition(package=app) and AnrCondition() match the ActivityManager ANR line,
    not WindowManager's ``ANR in Window{...}`` line at level I.
    """
    rec = _recording(level)
    entries = _entries(rec)

    assert _matches(AnrCondition(package=APP), entries) == _expected_anrs(level)
    assert _matches(AnrCondition(), entries) == _expected_anrs(level)


@pytest.mark.parametrize("level", LEVELS)
def test_conditions_with_a_near_miss_package_match_nothing(level: int) -> None:
    """A package that is a prefix or an extension of the app's name matches none of
    the recorded lines (the exact-name rule of design 2.7).
    """
    entries = _entries(_recording(level))

    for package in NEAR_MISSES:
        assert _matches(CrashCondition(package=package), entries) == [], package
        assert _matches(AnrCondition(package=package), entries) == [], package


@pytest.mark.parametrize("level", LEVELS)
def test_package_condition_matches_no_unattributed_line(level: int) -> None:
    """Package(app) matches nothing in a recording: no line carries attribution."""
    assert _matches(Package(APP), _entries(_recording(level))) == []


@pytest.mark.parametrize("level", LEVELS)
def test_read_file_filters_a_recording_by_crash_or_anr(
    level: int, tmp_path: Path
) -> None:
    """read_file with CrashCondition(package=) | AnrCondition(package=) yields the
    crash and ANR lines of the app from a log file: the conditions work offline.
    """
    rec = _recording(level)
    path = tmp_path / "c07.log"
    path.write_text(
        "".join(f"{line}\n" for _, line in _logcat_lines(rec)), encoding="utf-8"
    )
    condition = CrashCondition(package=APP) | AnrCondition(package=APP)

    found = [(e.tag, e.level, e.message) for e in read_file(path, filter_by=condition)]

    expected = [
        (tag, lvl, message)
        for _, tag, lvl, message in _matches(condition, _entries(rec))
    ]
    assert sorted(found) == sorted(expected)
    assert len(found) == len(_expected_crashes(level, rec)) + len(_expected_anrs(level))
