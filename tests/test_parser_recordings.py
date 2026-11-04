"""The parsers against the device recordings of PR-02 (``tests/fixtures/device``).

Every logcat line stored in the recordings of API 24 and API 36 is parsed with
the parser of the format it was printed in. Where the U-1 (a) argv printed the
line, the fields are compared with ``tests.device_facts.U1A_LINE``, a pattern
written independently from the AOSP format strings, so a parser change that
splits a recorded line differently fails here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from logpyt.parsers import ParseContext, ProcessLogParser, ThreadTimeLogParser
from logpyt.parsers._fallback import is_divider
from logpyt.parsers._uid import AID_NAMES
from tests.device_facts import (
    LEVELS,
    PROCESS_LINE,
    U1A_LINE,
    U1A_MODIFIERS,
    body,
    level_dir,
    load,
    step,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from logpyt.models import LogEntry
    from logpyt.parsers import LogParser

# The year of every recording (2026-11-04), for the formats that print none.
RECORDED_YEAR = 2026


def _modifiers(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-v"]


def _logcat_steps(level: int) -> Iterator[tuple[str, dict[str, Any]]]:
    """``(recording file, step)`` of every step that printed logcat lines."""
    for path in sorted(level_dir(level).glob("*.json")):
        rec = load(level_dir(level), path.name.partition("_")[0])
        for st in rec["steps"]:
            argv = st.get("argv") or []
            # -g prints ring sizes, not log lines.
            if "logcat" in argv and "-g" not in argv and st.get("stdout"):
                yield path.name, st


def _parser_for(argv: list[str]) -> LogParser:
    return (
        ProcessLogParser() if "process" in _modifiers(argv) else ThreadTimeLogParser()
    )


def _parse_all(lines: list[str], parser: LogParser) -> list[LogEntry]:
    ctx = ParseContext(year=RECORDED_YEAR)
    entries = []
    for line in body(lines):
        entry = parser.parse(line, ctx)
        assert entry is not None, f"{type(parser).__name__} rejected {line!r}"
        entries.append(entry)
    return entries


def _expected_uid(token: str) -> int | None:
    return int(token) if token.isdigit() else AID_NAMES.get(token)


@pytest.mark.parametrize("level", LEVELS)
def test_every_recorded_logcat_line_parses(level: int) -> None:
    """Every stored line of every logcat step parses; only dividers do not."""
    counted = 0
    for name, st in _logcat_steps(level):
        parser = _parser_for(st["argv"])
        ctx = ParseContext(year=RECORDED_YEAR)
        for line in st["stdout"]:
            entry = parser.parse(line, ctx)
            if is_divider(line):
                assert entry is None, f"{name} {st['name']}: divider parsed {line!r}"
                continue
            assert entry is not None, f"{name} {st['name']}: rejected {line!r}"
            assert entry.raw == line
            counted += 1
    assert counted > 500


@pytest.mark.parametrize("level", LEVELS)
def test_u1a_lines_split_like_the_independent_pattern(level: int) -> None:
    """Lines printed with the U-1 (a) argv parse to the fields U1A_LINE finds."""
    checked = 0
    for name, st in _logcat_steps(level):
        if _modifiers(st["argv"]) != U1A_MODIFIERS:
            continue
        entries = _parse_all(st["stdout"], ThreadTimeLogParser())
        for line, entry in zip(body(st["stdout"]), entries, strict=True):
            m = U1A_LINE.match(line)
            assert m is not None, f"{name}: {line!r}"
            stamp = datetime.strptime(
                f"{m['date']} {m['time']} {m['zone']}", "%Y-%m-%d %H:%M:%S.%f %z"
            )
            assert entry.timestamp == stamp
            assert entry.timestamp.utcoffset() == stamp.utcoffset()
            assert entry.uid_raw == m["uid"]
            assert entry.uid == _expected_uid(m["uid"]), line
            assert (entry.pid, entry.tid) == (int(m["pid"]), int(m["tid"]))
            assert (entry.level, entry.tag, entry.message) == (
                m["level"],
                m["tag"],
                m["msg"],
            )
            checked += 1
    assert checked > 300


@pytest.mark.parametrize("level", LEVELS)
def test_every_recorded_uid_token_is_mapped(level: int) -> None:
    """Every uid column seen on the device maps to a uid (design 5.8).

    App uids print as numbers on both levels; system uids as numbers or as the
    short AID names logcat knows. None of them is left unknown.
    """
    rec = load(level_dir(level), "R6")
    tokens = set(rec["facts"]["uid_tokens"])
    entries = _parse_all(step(rec, "dump")["stdout"], ThreadTimeLogParser())
    tokens |= {e.uid_raw for e in entries if e.uid_raw is not None}

    names = {t for t in tokens if not t.isdigit()}
    assert names, "no uid name recorded"
    assert names <= set(AID_NAMES), f"names without a uid: {names - set(AID_NAMES)}"
    assert any(e.uid is not None and e.uid >= 10000 for e in entries)
    assert all(e.uid is not None for e in entries)


@pytest.mark.parametrize("level", LEVELS)
def test_output_shapes_give_the_same_instant(level: int) -> None:
    """R1: one entry printed in each threadtime shape has one timestamp.

    The emulators run in UTC, so naive device time equals UTC. Shapes with
    milliseconds agree to the millisecond, the others to the microsecond; on
    API 24 ``-v nsec`` is read as a zone name and prints ms plus ``+0000``.
    """
    rec = load(level_dir(level), "R1")
    seen: dict[tuple[object, ...], list[tuple[str, datetime]]] = defaultdict(list)
    for st in rec["steps"]:
        for entry in _parse_all(st["stdout"], ThreadTimeLogParser()):
            ts = entry.timestamp
            utc = ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts
            key = (entry.pid, entry.tid, entry.level, entry.tag, entry.message)
            seen[key].append((st["name"], utc))
    compared = 0
    for stamps in seen.values():
        if len(stamps) < 2:
            continue
        to_ms = {
            utc - timedelta(microseconds=utc.microsecond % 1000) for _, utc in stamps
        }
        assert len(to_ms) == 1, stamps
        compared += 1
    assert compared >= 2
    uid_step = step(rec, "threadtime+uid")
    assert all(
        e.uid is not None for e in _parse_all(uid_step["stdout"], ThreadTimeLogParser())
    )
    epoch_step = step(rec, "threadtime+epoch+usec")
    assert all(
        e.timestamp.tzinfo is UTC
        for e in _parse_all(epoch_step["stdout"], ThreadTimeLogParser())
    )


@pytest.mark.parametrize("level", LEVELS)
def test_process_lines_take_their_tag_from_the_suffix(level: int) -> None:
    """C08: ``-v process`` lines end with two spaces and ``(tag)``."""
    rec = load(level_dir(level), "C08")
    lines = body(step(rec, "process")["stdout"])
    entries = _parse_all(lines, ProcessLogParser())

    for line, entry in zip(lines, entries, strict=True):
        m = PROCESS_LINE.match(line)
        assert m is not None
        assert entry.tag == m["tag"]
        assert line == f"{entry.level}({entry.pid:5d}) {entry.message}  ({entry.tag})"
    assert any("(" in entry.message for entry in entries)


def test_kernel_lines_on_36_parse_with_empty_or_spaced_tags() -> None:
    """API 36 reads the kernel buffer: empty tags and tags with a space parse."""
    rec = load(level_dir(36), "R6")
    entries = _parse_all(step(rec, "live")["stdout"], ThreadTimeLogParser())
    tags = {e.tag for e in entries}

    assert "" in tags
    assert any(" " in tag for tag in tags)


@pytest.mark.parametrize(("level", "suffix"), [(24, " "), (36, "")])
def test_message_keeps_the_trailing_space_of_log_t(level: int, suffix: str) -> None:
    """C11: ``log -t`` on API 24 appends a space; the message keeps it (D-13)."""
    rec = load(level_dir(level), "C11")
    entries = _parse_all(step(rec, "buffer_main")["stdout"], ThreadTimeLogParser())
    (marker,) = [e for e in entries if e.tag == "logpyt"]

    assert marker.message.startswith("lp-mark-")
    assert marker.message == marker.message.rstrip(" ") + suffix
