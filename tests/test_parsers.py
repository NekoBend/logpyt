"""Tests for log parsers."""

from __future__ import annotations

import sys
import time
import typing
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

import pytest

from logpyt.models import LogEntry
from logpyt.parsers import (
    BriefLogParser,
    LogParser,
    ParseContext,
    ProcessLogParser,
    RawLogParser,
    TagLogParser,
    ThreadTimeLogParser,
)
from logpyt.parsers._fallback import is_divider, strip_terminator, unparsed_entry
from logpyt.parsers._uid import AID_NAMES, parse_uid
from logpyt.readers import read_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from logpyt.models import LogLevel


def _ctx(year: int = 2026) -> ParseContext:
    return ParseContext(year=year)


def _parse(parser: LogParser, line: str, ctx: ParseContext | None = None) -> LogEntry:
    entry = parser.parse(line, ctx or _ctx())
    assert entry is not None, f"{type(parser).__name__} rejected {line!r}"
    return entry


def _years(ctx: ParseContext, *dates: str) -> list[str]:
    """Resolve ``MM-DD`` dates in order; the resolved ``YYYY-MM-DD`` (or None)."""
    out: list[str] = []
    for date in dates:
        month, day = (int(part) for part in date.split("-"))
        ts = ctx.resolve_yearless(month, day, 12, 0, 0, 0)
        out.append("None" if ts is None else ts.date().isoformat())
    return out


# -- the protocol and the fallback entry -------------------------------------------


def test_unrecognised_line_becomes_unparsed_entry() -> None:
    """The fallback for an unrecognised line keeps it whole, as an I entry."""
    ctx = _ctx()
    before = datetime.now(UTC).astimezone().replace(tzinfo=None)

    entry = unparsed_entry("some log line\r\n", ctx)

    assert entry.parsed is False
    assert (entry.message, entry.raw) == ("some log line", "some log line")
    assert (entry.level, entry.tag, entry.pid, entry.tid) == ("I", "", 0, 0)
    assert entry.timestamp.tzinfo is None
    assert (
        before - timedelta(seconds=5)
        <= entry.timestamp
        <= before + timedelta(seconds=60)
    )
    assert unparsed_entry("adb: error", ctx, level="E").level == "E"


def test_custom_parser_implements_the_protocol(tmp_path: Path) -> None:
    """Any class with logcat_format and parse(line, ctx) is a parser."""

    class PipeParser:
        logcat_format = "pipe"

        def parse(self, line: str, ctx: ParseContext) -> LogEntry | None:
            text = strip_terminator(line)
            if text.count("|") != 2:
                return None
            tag, level, message = text.split("|")
            if level not in {"V", "D", "I", "W", "E", "F"}:
                return None
            return LogEntry(
                timestamp=ctx.now(),
                pid=0,
                tid=0,
                level=cast("LogLevel", level),
                tag=tag,
                message=message,
                raw=text,
            )

    parser: LogParser = PipeParser()
    path = tmp_path / "pipe.log"
    path.write_text("App|E|boom\nnot a pipe line\n", encoding="utf-8")

    entries = list(read_file(path, parser=parser))

    assert [(e.tag, e.level, e.message, e.parsed) for e in entries] == [
        ("App", "E", "boom", True),
        ("", "I", "not a pipe line", False),
    ]


def test_logparser_subclass_without_its_members_cannot_be_instantiated() -> None:
    """A 2.x-style subclass (parse_stdout only) raises instead of parsing nothing."""

    class Old(LogParser):
        def parse_stdout(self, line: str) -> LogEntry | None:
            del line
            return None

    with pytest.raises(TypeError, match="abstract"):
        Old()


def test_logparser_subclass_with_both_members_works() -> None:
    """Naming LogParser as the base is allowed when both members are defined."""

    class Upper(LogParser):
        logcat_format = "upper"

        def parse(self, line: str, ctx: ParseContext, /) -> LogEntry | None:
            return unparsed_entry(line.upper(), ctx)

    entry = Upper().parse("x\n", _ctx())

    assert entry is not None
    assert entry.message == "X"


@pytest.mark.parametrize(
    "parse",
    [
        pytest.param(LogParser.parse, id="LogParser"),
        pytest.param(ThreadTimeLogParser.parse, id="ThreadTimeLogParser"),
        pytest.param(BriefLogParser.parse, id="BriefLogParser"),
        pytest.param(ProcessLogParser.parse, id="ProcessLogParser"),
        pytest.param(TagLogParser.parse, id="TagLogParser"),
        pytest.param(RawLogParser.parse, id="RawLogParser"),
    ],
)
def test_parse_type_hints_resolve(parse: Callable[..., object]) -> None:
    """typing.get_type_hints() resolves parse() (ParseContext is a runtime name)."""
    hints = typing.get_type_hints(parse)

    assert hints["ctx"] is ParseContext
    assert hints["line"] is str


def test_logcat_format_names() -> None:
    """Each parser names the ``adb logcat -v`` format it reads."""
    assert [
        p.logcat_format
        for p in (
            ThreadTimeLogParser(),
            BriefLogParser(),
            ProcessLogParser(),
            TagLogParser(),
            RawLogParser(),
        )
    ] == ["threadtime", "brief", "process", "tag", "raw"]


@pytest.mark.parametrize(
    "parser_cls",
    [ThreadTimeLogParser, BriefLogParser, ProcessLogParser, TagLogParser, RawLogParser],
)
def test_parsers_hold_no_state(parser_cls: type[LogParser]) -> None:
    """A parser has no instance state; sessions live in ParseContext (F6-1)."""
    parser = parser_cls()

    assert type(parser).__slots__ == ()
    assert not hasattr(parser, "__dict__")


def test_one_parser_serves_independent_sessions() -> None:
    """Two contexts used in turn with one parser keep their own year state."""
    parser = ThreadTimeLogParser()
    a, b = ParseContext(year=2024), ParseContext(year=2030)

    first = [
        _parse(parser, "12-31 23:59:59.000  1  1 D T: x", a),
        _parse(parser, "06-01 00:00:00.000  1  1 D T: x", b),
        _parse(parser, "01-01 00:00:01.000  1  1 D T: x", a),
        _parse(parser, "06-02 00:00:00.000  1  1 D T: x", b),
    ]

    assert [e.timestamp.year for e in first] == [2024, 2030, 2025, 2030]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("text\n", "text"),
        ("text\r\n", "text"),
        ("text", "text"),
        ("a\rb\n", "a\rb"),
        ("text\r", "text\r"),
        ("text\n\n", "text\n"),
        ("", ""),
    ],
)
def test_only_the_terminator_is_stripped(line: str, expected: str) -> None:
    """One LF or CR LF is the terminator; nothing else is removed (D-13)."""
    assert strip_terminator(line) == expected


def test_divider_lines_are_recognised() -> None:
    """logcat's buffer dividers are told apart from log lines."""
    assert is_divider("--------- beginning of main")
    assert is_divider("--------- switch to crash")
    assert not is_divider("-------- not quite")
    assert not is_divider("11-19 12:34:56.789  1  1 D T: --------- beginning of")


# -- ThreadTimeLogParser -------------------------------------------------------------


def test_thread_time_parser_success() -> None:
    """Test parsing a valid threadtime log line."""
    line = "11-19 12:34:56.789  1234  5678 D MyTag   : Hello World\n"
    entry = _parse(ThreadTimeLogParser(), line)

    assert (entry.pid, entry.tid, entry.level) == (1234, 5678, "D")
    assert (entry.tag, entry.message) == ("MyTag", "Hello World")
    assert entry.timestamp == datetime(2026, 11, 19, 12, 34, 56, 789000)  # noqa: DTZ001
    assert entry.raw == line.rstrip("\n")
    assert (entry.uid, entry.uid_raw, entry.package) == (None, None, None)
    assert entry.parsed is True
    assert entry.meta == {}


def test_thread_time_parser_empty_message() -> None:
    """An empty-message threadtime line keeps its structured fields (not raw)."""
    for line in (
        "11-19 12:34:56.789  1234  5678 D MyTag   : ",
        "11-19 12:34:56.789  1234  5678 D MyTag   :",  # trailing space stripped
    ):
        entry = _parse(ThreadTimeLogParser(), line)

        assert (entry.pid, entry.tid, entry.level) == (1234, 5678, "D")
        assert (entry.tag, entry.message) == ("MyTag", "")
        assert entry.parsed is True


def test_thread_time_parser_oversized_pid_falls_back() -> None:
    """A pathologically long PID must not crash int(); the line is not parsed."""
    line = "11-19 12:34:56.789  " + "9" * 5000 + "  5678 D Tag: msg"

    assert ThreadTimeLogParser().parse(line, _ctx()) is None


def test_thread_time_parser_year_rollover() -> None:
    """The year rolls forward when the month decreases (Dec -> Jan)."""
    parser, ctx = ThreadTimeLogParser(), ParseContext(year=2024)
    dec = _parse(parser, "12-31 23:59:59.000  1  1 D T: end of year", ctx)
    jan = _parse(parser, "01-01 00:00:01.000  1  1 D T: new year", ctx)

    assert dec.timestamp.year == 2024
    assert jan.timestamp.year == 2025
    assert jan.timestamp > dec.timestamp


def test_thread_time_parser_invalid_date_falls_back() -> None:
    """An impossible calendar date (month 13, Feb 30) is not parsed."""
    parser, ctx = ThreadTimeLogParser(), _ctx()

    assert parser.parse("13-01 12:00:00.000  1  1 D T: m", ctx) is None
    assert parser.parse("02-30 12:00:00.000  1  1 D T: m", ctx) is None
    assert parser.parse("2026-02-29 12:00:00.000  1  1 D T: m", ctx) is None
    assert parser.parse("11-19 24:00:00.000  1  1 D T: m", ctx) is None


def test_brief_parser_tag_with_parens() -> None:
    """Brief parser handles a tag that itself contains parentheses."""
    entry = _parse(BriefLogParser(), "D/Foo(Bar)( 2034): routeCall()")

    assert entry.level == "D"
    assert entry.tag == "Foo(Bar)"
    assert entry.pid == 2034
    assert entry.message == "routeCall()"


def test_brief_parser_message_with_paren_digits_does_not_steal_tag() -> None:
    """A message containing '( <digits> ):' must not hijack the tag/pid split.

    Regression: a greedy tag group backtracked to the last '( <digits> ):' in
    the line, mis-splitting tag/pid/message.
    """
    entry = _parse(BriefLogParser(), "D/Tag( 100): calling foo( 200): done")

    assert entry.tag == "Tag"
    assert entry.pid == 100
    assert entry.message == "calling foo( 200): done"


def test_thread_time_parser_fallback() -> None:
    """A line that is not threadtime is not parsed."""
    assert ThreadTimeLogParser().parse("Invalid format line", _ctx()) is None


def test_thread_time_parser_invalid_level_falls_back() -> None:
    """Threadtime parser rejects a level outside VDIWEF."""
    line = "11-19 12:34:56.789  1234  5678 G MyTag   : Hello World"

    assert ThreadTimeLogParser().parse(line, _ctx()) is None


def test_thread_time_parser_invalid_level_does_not_corrupt_year_rollover() -> None:
    """A valid-date line rejected for a bad level must not advance rollover state.

    Regression: the year-rollover state was committed inside _parse_timestamp
    before the level was validated, so a valid-date/invalid-level line would
    mutate _last_month/_year_offset and spuriously roll subsequent accepted lines.
    """
    parser, ctx = ThreadTimeLogParser(), ParseContext(year=2024)
    dec = _parse(parser, "12-31 23:59:59.000  1  1 D T: end of year", ctx)
    # Valid date, invalid level 'G' -> rejected WITHOUT touching the session.
    rejected = parser.parse("01-01 00:00:01.000  1  1 G T: bad level", ctx)
    no_tag = parser.parse("01-01 00:00:01.000  1  1 D no separator", ctx)
    dec2 = _parse(parser, "12-15 12:00:00.000  1  1 D T: still same year", ctx)

    assert dec.timestamp.year == 2024
    assert rejected is None
    assert no_tag is None
    assert dec2.timestamp.year == 2024


def test_rejected_threadtime_lines_do_not_move_the_year_reference() -> None:
    """Lines rejected after their date matched leave the session unchanged.

    The rejected lines are dated mid-year: had either one moved the reference
    to 06-30 (in 2025, the year nearest to Dec 31), the 12-15 line after them
    would be placed in 2025 instead of 2024.
    """
    parser, ctx = ThreadTimeLogParser(), ParseContext(year=2024)
    _parse(parser, "12-31 23:59:59.000  1  1 D T: a", ctx)

    assert parser.parse("06-30 12:00:00.000  1  1 D no separator", ctx) is None
    assert parser.parse("06-30 12:00:00.000 +2400  1  1 D T: x", ctx) is None
    assert _parse(parser, "12-15 12:00:00.000  1  1 D T: b", ctx).timestamp.year == 2024


def test_thread_time_parser_out_of_order_month_does_not_roll_year() -> None:
    """A backward month jump that is not Dec->Jan must not advance the year.

    logcat merges ring buffers and is not strictly month-monotonic; one
    out-of-order line (Jun then May) must not permanently roll the year forward.
    """
    parser, ctx = ThreadTimeLogParser(), ParseContext(year=2024)
    jun = _parse(parser, "06-15 10:00:00.000  1  1 I T: a", ctx)
    may = _parse(parser, "05-15 10:00:00.000  1  1 I T: b", ctx)  # out-of-order
    jun2 = _parse(parser, "06-16 10:00:00.000  1  1 I T: c", ctx)

    assert jun.timestamp.year == 2024
    assert may.timestamp.year == 2024  # not rolled to 2025 by the backward jump
    assert jun2.timestamp.year == 2024  # drift did not become permanent


def test_thread_time_parser_microsecond_precision() -> None:
    """Threadtime lines with 6-digit fractional seconds parse (not raw fallback)."""
    entry = _parse(
        ThreadTimeLogParser(), "11-19 12:34:56.123456  1234  5678 D Tag: msg"
    )

    assert entry.parsed is True
    assert entry.timestamp.microsecond == 123456
    assert entry.tag == "Tag"
    assert entry.message == "msg"


def test_thread_time_parser_tag_with_spaces() -> None:
    """Test parsing a log line where the tag contains spaces."""
    line = "11-19 12:34:56.789  1234  5678 D My Tag With Spaces : Hello World"
    entry = _parse(ThreadTimeLogParser(), line)

    assert entry.tag == "My Tag With Spaces"
    assert entry.message == "Hello World"
    assert entry.pid == 1234
    assert entry.tid == 5678
    assert entry.level == "D"


def test_thread_time_parser_message_with_colons() -> None:
    """Test parsing a log line where the message contains colons."""
    line = "11-19 12:34:56.789  1234  5678 D MyTag   : Message: with: colons"
    entry = _parse(ThreadTimeLogParser(), line)

    assert entry.tag == "MyTag"
    assert entry.message == "Message: with: colons"
    assert entry.pid == 1234
    assert entry.tid == 5678
    assert entry.level == "D"


def test_thread_time_parser_with_wrapped_whitespace() -> None:
    """Leading spaces are accepted; the message keeps its trailing spaces (D-13)."""
    line = "  11-19 12:34:56.789  1234  5678 D MyTag   : Hello World   "
    entry = _parse(ThreadTimeLogParser(), line)

    assert entry.pid == 1234
    assert entry.tid == 5678
    assert entry.level == "D"
    assert entry.tag == "MyTag"
    assert entry.message == "Hello World   "
    assert entry.raw == line
    assert entry.timestamp.year == 2026


def test_threadtime_message_keeps_leading_whitespace() -> None:
    """Only the one separator space goes: a stack frame keeps its tab (D-13)."""
    entry = _parse(
        ThreadTimeLogParser(),
        "11-19 12:34:56.789  1234  5678 E AndroidRuntime: \tat com.example.A.b(A:1)",
    )

    assert entry.message == "\tat com.example.A.b(A:1)"


def test_threadtime_bare_cr_stays_in_the_message() -> None:
    """A bare CR inside the line is message text, not a line end."""
    entry = _parse(ThreadTimeLogParser(), "11-19 12:34:56.789  1  1 I T: a\rb\r\n")

    assert entry.message == "a\rb"
    assert entry.raw == "11-19 12:34:56.789  1  1 I T: a\rb"


_UTC = UTC
_IST = timezone(timedelta(hours=5, minutes=30))
_PST = timezone(-timedelta(hours=8))


@pytest.mark.parametrize(
    ("time_column", "expected"),
    [
        pytest.param(
            "11-04 04:22:22.688",
            datetime(2026, 11, 4, 4, 22, 22, 688000),  # noqa: DTZ001
            id="threadtime",
        ),
        pytest.param(
            "11-04 04:22:22.688198",
            datetime(2026, 11, 4, 4, 22, 22, 688198),  # noqa: DTZ001
            id="usec",
        ),
        pytest.param(
            "11-04 04:22:22.688198123",
            datetime(2026, 11, 4, 4, 22, 22, 688198),  # noqa: DTZ001
            id="nsec",
        ),
        pytest.param(
            "11-04 04:22:22.688198999",
            datetime(2026, 11, 4, 4, 22, 22, 688198),  # noqa: DTZ001
            id="nsec-truncated-not-rounded",
        ),
        pytest.param(
            "2025-11-04 04:22:22.688",
            datetime(2025, 11, 4, 4, 22, 22, 688000),  # noqa: DTZ001
            id="year",
        ),
        pytest.param(
            "2025-11-04 04:22:22.688198",
            datetime(2025, 11, 4, 4, 22, 22, 688198),  # noqa: DTZ001
            id="year-usec",
        ),
        pytest.param(
            "11-04 04:22:22.688 +0530",
            datetime(2026, 11, 4, 4, 22, 22, 688000, tzinfo=_IST),
            id="zone",
        ),
        pytest.param(
            "2025-11-04 04:22:22.688 -0800",
            datetime(2025, 11, 4, 4, 22, 22, 688000, tzinfo=_PST),
            id="year-zone",
        ),
        pytest.param(
            "2025-11-04 04:22:22.688198 +0000",
            datetime(2025, 11, 4, 4, 22, 22, 688198, tzinfo=_UTC),
            id="year-usec-zone",
        ),
        pytest.param(
            "         1793766142.688",
            datetime(2026, 11, 4, 4, 22, 22, 688000, tzinfo=_UTC),
            id="epoch",
        ),
        pytest.param(
            "         1793766142.688198",
            datetime(2026, 11, 4, 4, 22, 22, 688198, tzinfo=_UTC),
            id="epoch-usec",
        ),
        pytest.param(
            "    40.123456",
            datetime(1970, 1, 1, 0, 0, 40, 123456, tzinfo=_UTC),
            id="monotonic-usec",
        ),
    ],
)
@pytest.mark.parametrize(
    ("uid_column", "uid", "uid_raw"),
    [
        pytest.param("", None, None, id="no-uid"),
        pytest.param(" 10065", 10065, "10065", id="uid-number"),
        pytest.param("  root", 0, "root", id="uid-name"),
        pytest.param("      ", None, None, id="uid-blank"),
    ],
)
def test_threadtime_parses_every_modifier_combination(
    time_column: str,
    expected: datetime,
    uid_column: str,
    uid: int | None,
    uid_raw: str | None,
) -> None:
    """Every time shape logcat prints, with and without a uid column (2.8)."""
    line = f"{time_column}{uid_column}  1599  1868 D ActivityManager: msg -- 2710"
    entry = _parse(ThreadTimeLogParser(), line)

    assert entry.timestamp == expected
    assert (entry.timestamp.tzinfo is None) == (expected.tzinfo is None)
    if expected.tzinfo is not None:
        assert entry.timestamp.utcoffset() == expected.utcoffset()
    assert (entry.uid, entry.uid_raw) == (uid, uid_raw)
    assert (entry.pid, entry.tid, entry.level) == (1599, 1868, "D")
    assert (entry.tag, entry.message) == ("ActivityManager", "msg -- 2710")


def test_threadtime_parses_the_live_wire_format() -> None:
    """The U-1 (a) shape: -v threadtime -v year -v usec -v zone -v uid."""
    line = (
        "2026-11-04 04:21:44.870000 +0000  logd  1254  1254 I auditd  : "
        "type=1403 audit(0.0:2): policy loaded"
    )
    entry = _parse(ThreadTimeLogParser(), line)

    assert entry.timestamp == datetime(2026, 11, 4, 4, 21, 44, 870000, tzinfo=UTC)
    assert (entry.uid, entry.uid_raw) == (1036, "logd")
    assert (entry.pid, entry.tid, entry.level) == (1254, 1254, "I")
    assert (entry.tag, entry.message) == (
        "auditd",
        "type=1403 audit(0.0:2): policy loaded",
    )


@pytest.mark.parametrize(
    ("rest", "tag", "message"),
    [
        pytest.param(
            "I         : Linux version 6.6.66 (kleaf@build-host)",
            "",
            "Linux version 6.6.66 (kleaf@build-host)",
            id="empty-tag",
        ),
        pytest.param(
            "I Command line: 8250.nr_uarts=1 clocksource=pit",
            "Command line",
            "8250.nr_uarts=1 clocksource=pit",
            id="tag-with-space",
        ),
    ],
)
def test_threadtime_kernel_lines(rest: str, tag: str, message: str) -> None:
    """Kernel lines (API 36) can have an empty tag or a tag with a space."""
    line = f"2026-11-04 04:21:44.774120 +0000  root     0     0 {rest}"
    entry = _parse(ThreadTimeLogParser(), line)

    assert (entry.tag, entry.message) == (tag, message)
    assert (entry.pid, entry.tid, entry.uid) == (0, 0, 0)


@pytest.mark.parametrize("zone", ["+2400", "-2400", "+0060", "+9999"])
def test_threadtime_rejects_an_impossible_zone(zone: str) -> None:
    """A zone offset no time zone can have makes the line unparsed."""
    line = f"2026-11-04 04:22:22.688 {zone}  1599  1868 D T: m"

    assert ThreadTimeLogParser().parse(line, _ctx()) is None


def test_threadtime_rejects_epoch_beyond_year_9999() -> None:
    """Epoch seconds past datetime's range make the line unparsed, not raise."""
    line = "999999999999.000  1599  1868 D T: m"

    assert ThreadTimeLogParser().parse(line, _ctx()) is None


# -- uid column (design 5.8) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "uid"),
    [
        ("10065", 10065),
        ("0", 0),
        ("1000", 1000),
        ("4294967295", 4294967295),
        ("4294967296", None),
        ("12345678901", None),
        ("root", 0),
        ("radio", 1001),
        ("media", 1013),
        ("logd", 1036),
        ("shell", 2000),
        ("uwb", 1083),
        ("u0_a65", 10065),
        ("u0_a1", 10001),
        ("u10_a5", 1010005),
        ("u0_i5", 90005),
        ("u2_i0", 290000),
        ("system", None),
        ("oem", None),
        ("u0_x5", None),
        ("u0_a", None),
        ("ua_a5", None),
        ("u0a5", None),
        ("١٢٣", None),
    ],
)
def test_uid_column_rules(token: str, uid: int | None) -> None:
    """Digits, app and isolated names, short AID names; anything else is None."""
    assert parse_uid(token) == uid


def test_aid_table_holds_only_names_logcat_can_print() -> None:
    """logcat prints a uid name only when it has at most 5 characters."""
    assert AID_NAMES
    assert all(len(name) <= 5 for name in AID_NAMES)
    assert len(set(AID_NAMES.values())) == len(AID_NAMES)
    assert all(uid < 10000 for uid in AID_NAMES.values())


# -- Year resolution (D-12, F6-1, F6-2) ---------------------------------------------


@pytest.mark.parametrize(
    ("anchor", "dates", "expected"),
    [
        pytest.param(
            datetime(2026, 1, 2),  # noqa: DTZ001
            ("12-30", "12-31", "01-01", "01-02"),
            ["2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02"],
            id="jan-start-dec-backlog",
        ),
        pytest.param(
            datetime(2025, 12, 31),  # noqa: DTZ001
            ("12-31", "01-01", "12-31", "01-01", "01-02"),
            ["2025-12-31", "2026-01-01", "2025-12-31", "2026-01-01", "2026-01-02"],
            id="dec-jan-interleave",
        ),
        pytest.param(
            datetime(2025, 12, 31),  # noqa: DTZ001
            ("12-31", "02-01"),
            ["2025-12-31", "2026-02-01"],
            id="dec-feb-gap",
        ),
        pytest.param(
            datetime(2025, 12, 31),  # noqa: DTZ001
            ("12-31", "11-30", "01-01"),
            ["2025-12-31", "2025-11-30", "2026-01-01"],
            id="dec-nov-jan",
        ),
        pytest.param(
            datetime(2026, 3, 1),  # noqa: DTZ001
            ("02-28", "02-29", "03-01"),
            ["2026-02-28", "2024-02-29", "2024-03-01"],
            id="feb29-in-a-2026-run",
        ),
        pytest.param(
            datetime(2028, 3, 1),  # noqa: DTZ001
            ("02-28", "02-29", "03-01"),
            ["2028-02-28", "2028-02-29", "2028-03-01"],
            id="feb29-in-a-2028-run",
        ),
        pytest.param(
            datetime(9999, 12, 31),  # noqa: DTZ001
            ("12-31", "01-01"),
            ["9999-12-31", "9999-01-01"],
            id="year-9999-edge",
        ),
        pytest.param(
            datetime(1, 1, 1),  # noqa: DTZ001
            ("12-31",),
            ["0001-12-31"],
            id="year-1-edge",
        ),
        pytest.param(
            datetime(2026, 11, 4),  # noqa: DTZ001
            ("03-15", "03-16"),
            ["2026-03-15", "2026-03-16"],
            id="first-line-before-the-anchor",
        ),
        pytest.param(
            datetime(2026, 11, 4),  # noqa: DTZ001
            ("11-05", "11-06"),
            ["2026-11-05", "2026-11-06"],
            id="first-line-within-two-days-after-the-anchor",
        ),
        pytest.param(
            datetime(2026, 11, 4),  # noqa: DTZ001
            ("11-07",),
            ["2025-11-07"],
            id="first-line-more-than-two-days-after-the-anchor",
        ),
    ],
)
def test_yearless_dates_resolve_against_the_previous_line(
    anchor: datetime, dates: tuple[str, ...], expected: list[str]
) -> None:
    """The nearest-year resolver: prototype cases plus the anchor rule."""
    assert _years(ParseContext(anchor=anchor), *dates) == expected


@pytest.mark.parametrize(
    ("year", "dates", "expected"),
    [
        pytest.param(2024, ("12-31", "01-01"), ["2024-12-31", "2025-01-01"], id="dec"),
        pytest.param(2025, ("01-01",), ["2025-01-01"], id="exact"),
        pytest.param(
            2025, ("02-29", "03-01"), ["2024-02-29", "2024-03-01"], id="feb29"
        ),
        pytest.param(2027, ("02-29",), ["2028-02-29"], id="feb29-next-leap"),
        pytest.param(2026, ("02-29",), ["2024-02-29"], id="feb29-nearer-earlier"),
    ],
)
def test_year_argument_places_the_first_line(
    year: int, dates: tuple[str, ...], expected: list[str]
) -> None:
    """year= is the first line's year; Feb 29 takes the nearest leap year."""
    assert _years(ParseContext(year=year), *dates) == expected


def test_equally_near_years_resolve_to_the_earlier_one() -> None:
    """A real tie: 01-01 00:00 is 182.5 days from 2025-07-02 12:00 both ways."""
    ctx = ParseContext(year=2025)
    ctx.resolve_yearless(7, 2, 12, 0, 0, 0)

    assert ctx.resolve_yearless(1, 1, 0, 0, 0, 0) == datetime(2025, 1, 1)  # noqa: DTZ001


def test_same_year_candidate_over_half_a_year_away_is_not_taken() -> None:
    """From 2024-12-31, 06-15 is 199 days back but 166 days ahead: 2025."""
    ctx = ParseContext(year=2024)
    ctx.resolve_yearless(12, 31, 12, 0, 0, 0)

    assert ctx.resolve_yearless(6, 15, 12, 0, 0, 0) == datetime(2025, 6, 15, 12)  # noqa: DTZ001


def test_unresolvable_date_leaves_the_session_unchanged() -> None:
    """A date with no valid year (02-30) gives None and does not move the reference."""
    ctx = ParseContext(year=2024)

    assert _years(ctx, "12-31", "02-30", "01-01") == [
        "2024-12-31",
        "None",
        "2025-01-01",
    ]


def test_line_with_a_year_moves_the_reference() -> None:
    """A yearless line after a -v year line is placed next to it."""
    parser, ctx = ThreadTimeLogParser(), ParseContext(year=2000)
    _parse(parser, "2031-12-31 23:59:59.000  1  1 D T: dated", ctx)

    entry = _parse(parser, "01-01 00:00:01.000  1  1 D T: yearless", ctx)

    assert entry.timestamp.year == 2032


def test_anchor_defaults_to_the_host_clock() -> None:
    """Without year= or anchor=, the first yearless line is placed before now."""
    now = datetime.now(UTC).astimezone().replace(tzinfo=None)
    ctx = ParseContext()
    entry = _parse(
        ThreadTimeLogParser(), now.strftime("%m-%d %H:%M:%S.000  1  1 D T: x"), ctx
    )

    assert abs(entry.timestamp - now) < timedelta(seconds=5)


def test_aware_anchor_is_converted_to_host_local_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An aware anchor is compared in host-local time, like yearless lines.

    The host zone is set to EST5 (five hours behind UTC, no DST), so the
    conversion shows on a UTC host too, such as a CI runner.
    """
    if sys.platform == "win32":
        pytest.skip(
            "time.tzset() is POSIX-only; the Windows jobs have no in-process TZ check"
        )
    else:
        try:
            monkeypatch.setenv("TZ", "EST5")
            time.tzset()
            ctx = ParseContext(anchor=datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
        finally:
            monkeypatch.undo()
            time.tzset()

        assert ctx.anchor == datetime(2026, 6, 1, 7, 0)  # noqa: DTZ001


@pytest.mark.parametrize("year", [0, -1, 10000])
def test_year_out_of_range_is_rejected(year: int) -> None:
    """year= must be a datetime year (1..9999)."""
    with pytest.raises(ValueError, match="year"):
        ParseContext(year=year)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"year": "2024"}, id="year-str"),
        pytest.param({"year": True}, id="year-bool"),
        pytest.param({"anchor": "2024-01-01"}, id="anchor-str"),
    ],
)
def test_context_arguments_are_type_checked(kwargs: dict[str, Any]) -> None:
    """A year or anchor of the wrong type raises TypeError."""
    with pytest.raises(TypeError):
        ParseContext(**kwargs)


# -- host time (F6-4) ----------------------------------------------------------------


def test_fallback_time_follows_dst(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host time is looked up per call, so a changed UTC offset applies at once.

    2.0.0 captured the local offset at import (logcat.py:11), so after a DST
    change every host timestamp was an hour off. Switching TZ between two
    offsets in-process shows the per-call lookup.
    """
    if sys.platform == "win32":
        pytest.skip(
            "time.tzset() is POSIX-only; the Windows jobs have no in-process TZ check"
        )
    else:
        ctx = ParseContext()
        try:
            monkeypatch.setenv("TZ", "UTC0")
            time.tzset()
            utc_now = ctx.now()
            monkeypatch.setenv("TZ", "EST5")  # five hours behind UTC
            time.tzset()
            eastern_now = ctx.now()
            fallback = unparsed_entry("not a log line", ctx)
            brief = _parse(BriefLogParser(), "D/Tag( 1): m", ctx)
        finally:
            monkeypatch.undo()
            time.tzset()

        shift = utc_now - eastern_now
        assert timedelta(hours=5) - timedelta(seconds=30) < shift
        assert shift < timedelta(hours=5) + timedelta(seconds=30)
        assert abs(fallback.timestamp - eastern_now) < timedelta(seconds=30)
        assert abs(brief.timestamp - eastern_now) < timedelta(seconds=30)


# -- Brief / Process / Tag / Raw -----------------------------------------------------


def test_brief_log_parser_success() -> None:
    """Test parsing a valid brief log line."""
    entry = _parse(BriefLogParser(), "D/HeadsetProfile( 2034): routeCall()")

    assert entry.level == "D"
    assert entry.tag == "HeadsetProfile"
    assert entry.pid == 2034
    assert entry.message == "routeCall()"
    assert entry.parsed is True
    assert isinstance(entry.timestamp, datetime)


def test_brief_log_parser_fallback() -> None:
    """Test fallback for brief log parser."""
    assert BriefLogParser().parse("Invalid format", _ctx()) is None


def test_brief_ambiguity_documented() -> None:
    """BriefLogParser documents how a tag containing '( <pid>): ' splits (F6-6)."""
    doc = BriefLogParser.__doc__ or ""

    assert "Ambiguity (F6-6)" in doc
    assert "( <digits>): " in doc
    entry = _parse(BriefLogParser(), "D/Odd( 7): tag( 100): message")
    assert (entry.tag, entry.pid, entry.message) == ("Odd", 7, "tag( 100): message")


def test_process_log_parser_success() -> None:
    """Test parsing a valid process log line."""
    entry = _parse(ProcessLogParser(), "I(  596) System.exit called, status: 0")

    assert entry.level == "I"
    assert entry.pid == 596
    assert entry.message == "System.exit called, status: 0"
    assert entry.tag == ""
    assert entry.parsed is True
    assert isinstance(entry.timestamp, datetime)


@pytest.mark.parametrize(
    ("line", "tag", "message"),
    [
        pytest.param(
            "I( 1599) Killing 2271:com.google.android.deskclock/u0a64 (adj 906): "
            "empty #18  (ActivityManager)",
            "ActivityManager",
            "Killing 2271:com.google.android.deskclock/u0a64 (adj 906): empty #18",
            id="logcat-shape",
        ),
        pytest.param(
            "W( 42) a  (b) c  (Tag)", "Tag", "a  (b) c", id="message-with-parens"
        ),
        pytest.param("E( 1234)   (MyTag)", "MyTag", "", id="empty-message"),
        pytest.param("I( 1) x  ()", "", "x", id="empty-tag"),
    ],
)
def test_process_parser_takes_the_tag_from_the_suffix(
    line: str, tag: str, message: str
) -> None:
    """-v process ends with two spaces and (tag): the last such group (C08)."""
    entry = _parse(ProcessLogParser(), line)

    assert (entry.tag, entry.message) == (tag, message)


def test_process_log_parser_fallback() -> None:
    """Test fallback for process log parser."""
    assert ProcessLogParser().parse("Invalid format", _ctx()) is None


def test_tag_log_parser_success() -> None:
    """Test parsing a valid tag log line."""
    entry = _parse(TagLogParser(), "D/HeadsetProfile: routeCall()")

    assert entry.level == "D"
    assert entry.tag == "HeadsetProfile"
    assert entry.message == "routeCall()"
    assert entry.pid == 0
    assert entry.parsed is True
    assert isinstance(entry.timestamp, datetime)


def test_tag_log_parser_fallback() -> None:
    """Test fallback for tag log parser."""
    assert TagLogParser().parse("Invalid format", _ctx()) is None


def test_raw_log_parser_success() -> None:
    """Test parsing a raw log line."""
    entry = _parse(RawLogParser(), "  routeCall()  \n")

    assert entry.message == "  routeCall()  "
    assert entry.raw == "  routeCall()  "
    assert entry.level == "I"
    assert entry.pid == 0
    assert entry.tag == ""
    assert entry.parsed is True
    assert isinstance(entry.timestamp, datetime)


@pytest.mark.parametrize(
    ("parser_cls", "line", "fields"),
    [
        pytest.param(
            BriefLogParser, "E/MyTag   ( 1234): ", ("E", "MyTag", 1234), id="brief"
        ),
        pytest.param(
            BriefLogParser, "E/MyTag( 1234):", ("E", "MyTag", 1234), id="brief-"
        ),
        pytest.param(ProcessLogParser, "E( 1234) ", ("E", "", 1234), id="process"),
        pytest.param(ProcessLogParser, "E( 1234)", ("E", "", 1234), id="process-"),
        pytest.param(TagLogParser, "E/MyTag   : ", ("E", "MyTag", 0), id="tag"),
        pytest.param(TagLogParser, "E/MyTag:", ("E", "MyTag", 0), id="tag-"),
    ],
)
def test_empty_message_keeps_fields(
    parser_cls: type[LogParser], line: str, fields: tuple[str, str, int]
) -> None:
    """Empty messages parse, with or without the trailing space (F6-3)."""
    entry = _parse(parser_cls(), line)

    assert (entry.level, entry.tag, entry.pid, entry.message) == (*fields, "")


def test_brief_log_parser_non_brief_prefix_fallback() -> None:
    """Brief parser rejects a line whose level is not VDIWEF."""
    assert BriefLogParser().parse("X/not-brief( 2034): routeCall()", _ctx()) is None


def test_thread_time_parser_missing_colon_separator_fallback() -> None:
    """Date-like lines without a tag/message separator are not parsed."""
    line = "11-19 12:34:56.789  1234  5678 D MyTag Hello World"

    assert ThreadTimeLogParser().parse(line, _ctx()) is None


def test_brief_log_parser_missing_open_paren_fallback() -> None:
    """Brief parser rejects a line without the opening parenthesis."""
    assert BriefLogParser().parse("D/HeadsetProfile 2034): routeCall()", _ctx()) is None


def test_process_log_parser_missing_close_paren_fallback() -> None:
    """Process parser rejects a line without the closing parenthesis."""
    assert (
        ProcessLogParser().parse("I(  596 System.exit called, status: 0", _ctx())
        is None
    )


@pytest.mark.parametrize(
    "line",
    [
        "D/" + " " * 50_000,
        "11-19 12:34:56.789  1  1 D " + " " * 50_000,
        " " * 50_000,
        "D(" + " " * 50_000,
        "D/(" + " (" * 25_000,
    ],
)
@pytest.mark.parametrize(
    "parser_cls", [ThreadTimeLogParser, BriefLogParser, ProcessLogParser, TagLogParser]
)
def test_long_runs_of_spaces_parse_in_linear_time(
    parser_cls: type[LogParser], line: str
) -> None:
    """No pattern backtracks quadratically on a long run of spaces."""
    start = time.perf_counter()
    parser_cls().parse(line, _ctx())
    assert time.perf_counter() - start < 0.5
