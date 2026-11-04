"""Tests for read_file (design 2.8; F1-2, F1-4, F1-7, F5-4, F6-5, X-4, X-6)."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from logpyt import readers
from logpyt.filters import Filter, Package, Tag, Where
from logpyt.groupers import GroupBy
from logpyt.models import LogEntry, LogGroup
from logpyt.parsers import BriefLogParser, ParseContext
from logpyt.parsers.logcat import ThreadTimeLogParser
from logpyt.readers import read_file
from tests.device_facts import LEVELS, body, level_dir, load

if TYPE_CHECKING:
    from collections.abc import Generator

    from logpyt.filters import Condition

APP_UID = 10065
APP = "com.example.app"
CAP = 4096  # the smallest max_line_bytes
BOM = "\ufeff"  # U+FEFF, the UTF-8 BOM once encoded


@pytest.fixture
def sample_log_content() -> str:
    """Provide sample log content with mixed valid and invalid lines."""
    return (
        "11-19 12:34:56.789  1000  2000 D TagA   : Message A\n"
        "Invalid Line That Does Not Match Regex\n"
        "11-19 12:34:57.000  1000  2000 I TagB   : Message B\n"
    )


@pytest.fixture
def sample_log_file(tmp_path: Path, sample_log_content: str) -> Path:
    """Create a temporary log file with sample content."""
    log_file = tmp_path / "test.log"
    log_file.write_text(sample_log_content, encoding="utf-8")
    return log_file


def _write_lines(path: Path, *lines: str) -> Path:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def _uid_line(message: str, *, uid: str = str(APP_UID), tag: str = "App") -> str:
    """A line of the live wire format (U-1): year, usec, zone and uid."""
    return f"2026-11-04 05:11:16.661446 +0000 {uid:>5}  1234  1234 I {tag}: {message}"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# -- reading ---------------------------------------------------------------------------


def test_read_file_yields_parsed_and_unparsed_entries(sample_log_file: Path) -> None:
    """Every line gives an entry; an unrecognised one has parsed=False."""
    entries = list(read_file(sample_log_file, parser=ThreadTimeLogParser()))

    assert [(e.tag, e.message, e.level, e.parsed) for e in entries] == [
        ("TagA", "Message A", "D", True),
        ("", "Invalid Line That Does Not Match Regex", "I", False),
        ("TagB", "Message B", "I", True),
    ]


def test_read_file_convenience_function(sample_log_file: Path) -> None:
    """read_file with an explicit parser returns a lazy iterator of entries."""
    parser = ThreadTimeLogParser()

    entries = list(read_file(sample_log_file, parser=parser))

    assert isinstance(entries, list)
    assert len(entries) == 3
    assert entries[0].tag == "TagA"


def test_read_file_not_found() -> None:
    """Test that FileNotFoundError is raised for non-existent files."""
    non_existent_file = Path("non_existent_file.log")

    with pytest.raises(FileNotFoundError):
        list(read_file(non_existent_file))


def test_file_is_opened_at_the_first_next(tmp_path: Path) -> None:
    """The call checks the arguments only; opening the file waits for next()."""
    lazy = read_file(tmp_path / "later.log")
    _write_lines(tmp_path / "later.log", "11-19 12:34:56.789  1  1 D T: a")

    assert [e.message for e in lazy] == ["a"]


def test_where_filters_entries(sample_log_file: Path) -> None:
    """Only the entries where accepts are yielded."""
    entries = list(read_file(sample_log_file, where=Filter(tag="TagA")))

    assert [(e.tag, e.message) for e in entries] == [("TagA", "Message A")]


def test_parser_exception_propagates(tmp_path: Path) -> None:
    """A custom parser that raises ends the read with its exception, unchanged."""
    log_file = _write_lines(tmp_path / "error.log", "Line 1", "Line 2", "Line 3")

    class _BoomError(ValueError):
        pass

    class _Parser:
        logcat_format = "lines"

        def parse(self, line: str, ctx: ParseContext) -> LogEntry:
            if "Line 2" in line:
                raise _BoomError(line)
            return LogEntry(
                timestamp=ctx.now(),
                pid=0,
                tid=0,
                level="I",
                tag="Test",
                message=line,
                raw=line,
            )

    lazy = read_file(log_file, parser=_Parser())

    assert next(lazy).message == "Line 1"
    with pytest.raises(_BoomError, match="Line 2"):
        next(lazy)
    assert list(lazy) == []


def test_where_exception_propagates(tmp_path: Path) -> None:
    """A where that raises ends the read with its exception, unchanged."""
    log_file = _write_lines(tmp_path / "where.log", "11-19 12:34:56.789  1  1 D T: a")

    class _BoomError(ValueError):
        pass

    def _explode(entry: LogEntry) -> bool:
        raise _BoomError(entry.message)

    lazy = read_file(log_file, where=Where(_explode))

    with pytest.raises(_BoomError, match="a"):
        next(lazy)
    assert next(lazy, None) is None


def test_unparsed_line_becomes_fallback_entry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A line the parser rejects is yielded whole as a parsed=False entry (X-6).

    Nothing previews, sanitizes or logs the line itself: control characters
    stay in the entry, and the only log record is the one mismatch WARNING.
    """
    line = "BAD\x00\x1b[31mCTRL" + "X" * 3000
    log_file = _write_lines(tmp_path / "malformed.log", line)

    with caplog.at_level(logging.DEBUG, logger="logpyt"):
        (entry,) = read_file(log_file)

    assert (entry.parsed, entry.message, entry.raw) == (False, line, line)
    assert (entry.pid, entry.tid, entry.tag, entry.level) == (0, 0, "", "I")
    assert len(caplog.records) == 1
    assert "\x1b" not in caplog.records[0].getMessage()


def test_divider_lines_are_skipped(tmp_path: Path) -> None:
    """logcat's buffer dividers are not entries (design 5.5)."""
    log_file = _write_lines(
        tmp_path / "dividers.log",
        "--------- beginning of main",
        "11-19 12:34:56.789  1000  2000 D TagA   : a",
        "--------- switch to crash",
    )

    assert [e.message for e in read_file(log_file, year=2026)] == ["a"]


# -- the format-mismatch warning (F6-5) -----------------------------------------------


def test_format_mismatch_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Lines in another format give one WARNING per read, with their count (F6-5).

    logcat's buffer dividers are neither entries nor counted as unparsed, but
    they are lines of the file; the other lines are yielded as unparsed entries.
    """
    log_file = _write_lines(
        tmp_path / "brief.log",
        "--------- beginning of main",
        "D/Tag( 1234): one",
        "D/Tag( 1234): two",
        "11-19 12:34:56.789  1000  2000 D TagA   : threadtime",
        "D/Tag( 1234): three",
    )

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        entries = list(read_file(log_file, year=2026))
    warnings = _warnings(caplog)

    assert [e.parsed for e in entries] == [False, False, True, False]
    assert [e.message for e in entries][:2] == [
        "D/Tag( 1234): one",
        "D/Tag( 1234): two",
    ]
    assert len(warnings) == 1
    assert warnings[0].startswith("3 of 5 lines in ")
    assert "'threadtime'" in warnings[0]
    assert "ThreadTimeLogParser" in warnings[0]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        list(read_file(log_file, year=2026))
    assert len(_warnings(caplog)) == 1


def test_matching_file_and_dividers_do_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file in the parser's format, dividers included, logs no warning."""
    log_file = _write_lines(
        tmp_path / "ok.log",
        "--------- beginning of main",
        "11-19 12:34:56.789  1000  2000 D TagA   : a",
        "--------- switch to crash",
        "11-19 12:34:57.000  1000  2000 E TagB   : b",
    )

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        entries = list(read_file(log_file, year=2026))

    assert [e.parsed for e in entries] == [True, True]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_mismatch_warning_is_logged_when_the_read_stops_early(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Closing the reader early still reports the lines read so far."""
    log_file = _write_lines(tmp_path / "raw.log", "not a log line", "neither")

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        entries = cast("Generator[LogEntry]", read_file(log_file))
        next(entries)
        entries.close()

    (warning,) = _warnings(caplog)
    assert warning.startswith("1 of 1 lines in ")


def test_other_parser_reads_its_format_without_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """parser= selects the format; a file in that format does not warn."""
    log_file = _write_lines(tmp_path / "brief.log", "D/Tag( 1234): one")

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        (entry,) = read_file(log_file, parser=BriefLogParser())

    assert (entry.pid, entry.tag, entry.message) == (1234, "Tag", "one")
    assert _warnings(caplog) == []


# -- framing in files (F1-2, F1-4, F1-7, X-4) ------------------------------------------


def test_oversized_line_is_one_truncated_entry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A line over max_line_bytes is one entry with truncated=True, holding its first
    max_line_bytes bytes, and the next line is read as usual (F1-4, X-4).
    """
    head = "11-19 12:34:56.789  1000  2000 D Long: "
    long_line = head + "x" * (2 * CAP)
    log_file = _write_lines(
        tmp_path / "long.log", long_line, "11-19 12:34:57.000  1  1 I Next: after"
    )

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        entries = list(read_file(log_file, year=2026, max_line_bytes=CAP))

    assert [(e.tag, e.truncated) for e in entries] == [("Long", True), ("Next", False)]
    assert entries[0].raw == long_line[:CAP]
    assert entries[1].message == "after"
    (warning,) = _warnings(caplog)
    assert warning.startswith("1 of 2 lines in ")
    assert f"max_line_bytes={CAP}" in warning


def test_line_of_exactly_max_line_bytes_is_whole(tmp_path: Path) -> None:
    """The cap counts the line without its terminator, CR LF included."""
    head = "11-19 12:34:56.789  1000  2000 D Edge: "
    line = head + "y" * (CAP - len(head))
    log_file = tmp_path / "edge.log"
    log_file.write_bytes(f"{line}\r\n{line}y\r\n".encode())

    entries = list(read_file(log_file, year=2026, max_line_bytes=CAP))

    assert [(len(e.raw), e.truncated) for e in entries] == [(CAP, False), (CAP, True)]


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 4096])
def test_small_reads_give_the_same_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    """Chunk boundaries inside a BOM, a CR LF or a character change nothing."""
    data = (
        b"\xef\xbb\xbf"
        + "11-19 12:34:56.789  1  1 D T: café 日本\r\n".encode()
        + b"11-19 12:34:57.000  2  2 I T: bare\rcr\r\r\n"
        + b"11-19 12:34:58.000  3  3 W T: no lf at the end"
    )
    log_file = tmp_path / "chunks.log"
    log_file.write_bytes(data)
    expected = [(e.pid, e.message) for e in read_file(log_file, year=2026)]
    monkeypatch.setattr(readers, "_CHUNK_BYTES", chunk)

    entries = [(e.pid, e.message) for e in read_file(log_file, year=2026)]

    assert entries == expected
    assert entries == [
        (1, "café 日本"),
        (2, "bare\rcr"),
        (3, "no lf at the end"),
    ]


def test_bom_after_offset_0_stays(tmp_path: Path) -> None:
    """Only a BOM at the very start of the file is skipped (F1-7)."""
    line = "11-19 12:34:56.789  1  1 D T: a"
    log_file = tmp_path / "bom.log"
    log_file.write_bytes(f"{line}\n{BOM}{line}\n".encode())

    entries = list(read_file(log_file, year=2026))

    assert [e.parsed for e in entries] == [True, False]
    assert entries[1].raw == f"{BOM}{line}"


# -- package attribution (F5-4) --------------------------------------------------------


@pytest.mark.parametrize(
    "where",
    [
        Package(APP),
        Filter(package=APP),
        ~Package(APP),
        Tag("x") | Package(APP),
        Where(Package(APP) & Tag("x")),
    ],
    ids=["package", "filter", "not", "or", "where"],
)
def test_package_condition_without_uid_map_raises(
    tmp_path: Path, where: Condition
) -> None:
    """A condition that names packages needs uid_packages: without the map it
    raises ValueError at the call, before the file is opened (F5-4).
    """
    missing = tmp_path / "missing.log"

    with pytest.raises(ValueError, match=r"'com\.example\.app'.*uid_packages"):
        read_file(missing, where=where)


def test_package_condition_with_uid_map_yields_that_package(tmp_path: Path) -> None:
    """With uid_packages, Package matches the lines of the uids the map names."""
    log_file = _write_lines(
        tmp_path / "uid.log",
        _uid_line("mine 1"),
        _uid_line("theirs", uid="10066"),
        _uid_line("system", uid="1000"),
        _uid_line("mine 2"),
    )

    entries = list(read_file(log_file, where=Package(APP), uid_packages={APP_UID: APP}))

    assert [(e.message, e.uid, e.package) for e in entries] == [
        ("mine 1", APP_UID, APP),
        ("mine 2", APP_UID, APP),
    ]


def test_uid_map_without_where_attributes_every_entry(tmp_path: Path) -> None:
    """The map attributes entries for any use (export, group_by=("package",))."""
    log_file = _write_lines(
        tmp_path / "uid.log", _uid_line("a"), _uid_line("b", uid="shell")
    )

    entries = list(read_file(log_file, uid_packages={APP_UID: APP, 2000: "shell.app"}))

    assert [(e.uid_raw, e.package) for e in entries] == [
        (str(APP_UID), APP),
        ("shell", "shell.app"),
    ]


def test_uid_map_matches_secondary_users_by_app_id(tmp_path: Path) -> None:
    """A secondary user's line matches the app id the map names (design 5.8)."""
    log_file = _write_lines(
        tmp_path / "uid.log",
        _uid_line("user 0"),
        _uid_line("user 10", uid="1010065"),
        _uid_line("other", uid="1010066"),
    )

    entries = list(read_file(log_file, where=Package(APP), uid_packages={APP_UID: APP}))

    assert [(e.message, e.uid) for e in entries] == [
        ("user 0", APP_UID),
        ("user 10", 1_010_065),
    ]


def test_uid_map_is_copied_at_the_call(tmp_path: Path) -> None:
    """Changing the caller's mapping after the call changes nothing."""
    log_file = _write_lines(tmp_path / "uid.log", _uid_line("a"))
    uids = {APP_UID: APP}
    lazy = read_file(log_file, uid_packages=uids)
    uids[APP_UID] = "com.other"

    assert [e.package for e in lazy] == [APP]


@pytest.mark.parametrize("level", LEVELS)
def test_uid_map_attributes_a_recorded_dump(tmp_path: Path, level: int) -> None:
    """The C03 recording of the U-1 wire format: each app line gets the package
    that the device's dumpsys gave its uid.
    """
    rec = load(level_dir(level), "C03")
    dump = next(st for st in rec["steps"] if st["name"] == "dump")
    log_file = _write_lines(tmp_path / "c03.log", *dump["stdout"])
    owners: dict[str, list[str]] = rec["facts"]["app_uid_tokens_to_packages"]
    uids = {int(uid): names[0] for uid, names in owners.items()}

    entries = list(read_file(log_file, uid_packages=uids))
    one = min(uids.items())
    mine = list(read_file(log_file, where=Package(one[1]), uid_packages=uids))

    assert len(entries) == len(body(dump["stdout"])) == len(owners)
    assert all(e.parsed and e.package == uids[cast("int", e.uid)] for e in entries)
    assert [(e.uid, e.package) for e in mine] == [one]


# -- grouping -------------------------------------------------------------------------


def test_group_by_yields_groups_and_flushes_at_the_end(tmp_path: Path) -> None:
    """group_by= groups like GroupBy.groups; the open groups close with "end"."""
    log_file = _write_lines(
        tmp_path / "groups.log",
        "11-19 12:34:56.000  1  1 D T: a1",
        "11-19 12:34:56.010  1  1 D T: a2",
        "11-19 12:34:56.020  2  2 D T: b1",
        "11-19 12:34:56.030  2  2 D Drop: b-dropped",
        "11-19 12:34:56.040  1  1 D T: a3",
    )
    spec = GroupBy(keys=("pid",), gap_ms=1000.0, mode="windowed")

    groups = list(read_file(log_file, group_by=spec, where=~Tag("Drop"), year=2026))
    offline = list(spec.groups(read_file(log_file, where=~Tag("Drop"), year=2026)))

    assert all(isinstance(group, LogGroup) for group in groups)
    assert [([e.message for e in g], g.reason) for g in groups] == [
        (["b1"], "end"),
        (["a1", "a2", "a3"], "end"),
    ]
    assert [(g.key, g.entries, g.reason) for g in groups] == [
        (g.key, g.entries, g.reason) for g in offline
    ]


def test_package_grouping_with_a_uid_map(tmp_path: Path) -> None:
    """group_by keys "package" reads the attributed package."""
    log_file = _write_lines(
        tmp_path / "uid.log",
        _uid_line("mine"),
        _uid_line("theirs", uid="10066"),
        _uid_line("mine again"),
    )
    spec = GroupBy(keys=("package",), gap_ms=1000.0, mode="windowed")

    groups = list(read_file(log_file, group_by=spec, uid_packages={APP_UID: APP}))

    # The least recently extended group closes first at the end.
    assert [(g.key, len(g)) for g in groups] == [((None,), 1), ((APP,), 2)]


# -- year ------------------------------------------------------------------------------


def _set_mtime(path: Path, moment: datetime) -> None:
    os.utime(path, (moment.timestamp(), moment.timestamp()))


def test_yearless_lines_are_dated_before_the_file_mtime(tmp_path: Path) -> None:
    """Without year=, the first yearless line is placed before the file's mtime.

    A capture across New Year saved on Jan 2 starts in the old year; a March
    capture copied in November (a later mtime) stays in that year's March,
    although the next March would be nearer to the mtime.
    """
    new_year = _write_lines(
        tmp_path / "new_year.log",
        "12-30 08:00:00.000  1000  2000 D T: a",
        "12-31 23:59:59.000  1000  2000 D T: b",
        "01-01 00:00:01.000  1000  2000 D T: c",
    )
    _set_mtime(new_year, datetime(2027, 1, 2, 12, 0, tzinfo=UTC))
    spring = _write_lines(
        tmp_path / "spring.log",
        "03-15 10:00:00.000  1000  2000 D T: a",
        "03-15 10:00:01.000  1000  2000 D T: b",
    )
    _set_mtime(spring, datetime(2026, 11, 4, 12, 0, tzinfo=UTC))

    assert [e.timestamp.year for e in read_file(new_year)] == [2026, 2026, 2027]
    assert [e.timestamp.year for e in read_file(spring)] == [2026, 2026]


def test_year_argument_dates_the_first_yearless_line(tmp_path: Path) -> None:
    """year= gives the first yearless line its year, whatever the mtime."""
    log_file = _write_lines(
        tmp_path / "y.log",
        "12-31 23:59:59.000  1000  2000 D T: a",
        "01-01 00:00:01.000  1000  2000 D T: b",
    )

    assert [e.timestamp.year for e in read_file(log_file, year=2019)] == [2019, 2020]


@pytest.mark.parametrize("year", [0, 10_000])
def test_year_out_of_range_is_rejected(tmp_path: Path, year: int) -> None:
    """A year outside 1..9999 raises ValueError when read_file is called."""
    with pytest.raises(ValueError, match="year"):
        read_file(tmp_path / "x.log", year=year)


@pytest.mark.parametrize(
    "mtime",
    [
        pytest.param(253_402_300_800.0, id="year-10000"),
        pytest.param(1e20, id="past-time_t"),
        pytest.param(-1e20, id="before-time_t"),
    ],
)
def test_mtime_without_a_datetime_does_not_fail_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mtime: float
) -> None:
    """An mtime outside what datetime holds is no error, with or without year=.

    With year= the mtime is not used at all; without it the first yearless
    line is placed before the host clock instead. The mtime is patched in,
    because a file system may refuse to store such a value.
    """
    log_file = _write_lines(tmp_path / "far.log", "12-31 23:59:59.000  1  1 D T: a")
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mtime=mtime))

    with_year = list(read_file(log_file, year=2024))
    without = list(read_file(log_file))

    assert [e.timestamp.year for e in with_year] == [2024]
    assert len(without) == 1
    assert without[0].parsed
    assert without[0].timestamp <= datetime.now(UTC).astimezone().replace(
        tzinfo=None
    ) + timedelta(days=2)


# -- argument checks -------------------------------------------------------------------


def test_arguments_are_keyword_only(tmp_path: Path) -> None:
    """Only the path is positional (design 2.8)."""
    with pytest.raises(TypeError):
        read_file(tmp_path / "x.log", ThreadTimeLogParser())  # ty: ignore[no-matching-overload]
    with pytest.raises(TypeError, match="filter_by"):
        read_file(tmp_path / "x.log", filter_by=Tag("x"))  # ty: ignore[no-matching-overload]


@pytest.mark.parametrize(
    ("kwargs", "error", "match"),
    [
        ({"where": lambda e: True}, TypeError, r"where must be a Condition.*Where"),
        ({"group_by": {"keys": ("pid",)}}, TypeError, "group_by must be a GroupBy"),
        ({"parser": object()}, TypeError, r"parse\(line, ctx\)"),
        (
            {"parser": ThreadTimeLogParser},
            TypeError,
            r"instance.*ThreadTimeLogParser\(\)",
        ),
        ({"year": "2026"}, TypeError, "year"),
        ({"uid_packages": [(1, APP)]}, TypeError, "mapping"),
        ({"uid_packages": {"10065": APP}}, TypeError, "int uids"),
        ({"uid_packages": {True: APP}}, TypeError, "int uids"),
        ({"uid_packages": {APP_UID: 7}}, TypeError, "str"),
        ({"uid_packages": {-1: APP}}, ValueError, ">= 0"),
        ({"uid_packages": {APP_UID: "com.x; reboot"}}, ValueError, "package name"),
        ({"max_line_bytes": True}, TypeError, "int"),
        ({"max_line_bytes": 4096.0}, TypeError, "int"),
        ({"max_line_bytes": CAP - 1}, ValueError, "at least 4096"),
    ],
    ids=[
        "where-fn",
        "group-by",
        "parser",
        "parser-cls",
        "year-str",
        "uids-list",
        "uid-str",
        "uid-bool",
        "pkg-int",
        "uid-neg",
        "pkg-bad",
        "cap-bool",
        "cap-float",
        "cap-small",
    ],
)
def test_bad_arguments_raise_at_the_call(
    tmp_path: Path, kwargs: dict[str, Any], error: type[Exception], match: str
) -> None:
    """Every argument is checked when read_file is called, before any I/O."""
    with pytest.raises(error, match=match):
        read_file(tmp_path / "missing.log", **kwargs)


def test_empty_uid_map_and_smallest_cap_are_accepted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """{} is a map (no uid attributed), and 4096 is a valid cap."""
    log_file = _write_lines(tmp_path / "uid.log", _uid_line("a"))

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        entries = list(
            read_file(log_file, where=Package(APP), uid_packages={}, max_line_bytes=CAP)
        )

    assert entries == []
    assert _warnings(caplog) == [
        f"where names package {APP!r}, but no uid in uid_packages maps to it: "
        "no line can carry that package."
    ]


def test_unmapped_package_warns_at_the_call_and_still_reads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A package of where that no map value names gets one WARNING each, at the
    call; the read goes on, so ~Package and Or still select lines.
    """
    log_file = _write_lines(
        tmp_path / "uid.log", _uid_line("mine"), _uid_line("other", uid="10066")
    )
    where = ~Package((APP, "com.b")) | Package("com.c")

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        lazy = read_file(log_file, where=where, uid_packages={APP_UID: APP})
        warned = _warnings(caplog)
        entries = list(lazy)

    assert [w.split(",")[0] for w in warned] == [
        "where names package 'com.b'",
        "where names package 'com.c'",
    ]
    assert [e.message for e in entries] == ["other"]
    assert _warnings(caplog) == warned  # nothing more while reading


def test_mapped_packages_do_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No warning when every package of where is a map value."""
    log_file = _write_lines(tmp_path / "uid.log", _uid_line("a"))

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        entries = list(
            read_file(log_file, where=Package(APP), uid_packages={APP_UID: APP})
        )

    assert [e.package for e in entries] == [APP]
    assert _warnings(caplog) == []


def test_module_surface() -> None:
    """readers.py exports read_file only: LogFileReader is gone (design 2.1)."""
    assert readers.__all__ == ["read_file"]
    assert not hasattr(readers, "LogFileReader")
