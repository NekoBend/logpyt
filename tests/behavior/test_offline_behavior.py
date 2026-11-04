"""Behavior of the offline components through the public API.

Covers ``LogFileReader`` / ``read_file``, the format parsers, ``LogEntry``'s
JSON payload, ``export_logs`` and ``GroupBy``. No adb is involved; inputs are
real files under ``tmp_path`` or ``LogEntry`` objects built by the test.

Known bugs are written for the correct behavior and marked
``xfail(strict=True)`` with the plan id from
``.docs/review-findings-2.0.1.md``.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest

from logpyt import (
    Filter,
    GroupBy,
    LogEntry,
    LogFileReader,
    LogGroup,
    ThreadTimeLogParser,
    export_logs,
    read_file,
)
from logpyt.parsers import BriefLogParser, ProcessLogParser, TagLogParser
from tests.conftest import threadtime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path

    from logpyt import LogParser
    from logpyt.models import LogLevel

ExportFormat = Literal["json", "csv"]
GroupMode = Literal["consecutive", "windowed"]

_T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_FORMULA_PREFIXES = ("=", "+", "-", "@")


# -- helpers -------------------------------------------------------------------


def _write(tmp_path: Path, data: bytes, name: str = "app.log") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _entry(
    message: str = "m",
    *,
    ms: float = 0.0,
    pid: int = 1234,
    tag: str = "Tag",
    level: LogLevel = "I",
) -> LogEntry:
    return LogEntry(
        timestamp=_T0 + timedelta(milliseconds=ms),
        pid=pid,
        tid=pid,
        level=level,
        tag=tag,
        message=message,
        raw=message,
    )


def _groups(spec: GroupBy, entries: Iterable[LogEntry]) -> list[LogGroup]:
    """Group ``entries`` offline; every group, the ones still open at the end too."""
    groups = list(spec.groups(entries))
    for group in groups:
        assert isinstance(group, LogGroup), f"GroupBy.groups yielded {group!r}"
    return groups


def _messages(groups: Iterable[LogGroup]) -> list[list[str]]:
    return [[entry.message for entry in group] for group in groups]


def _built_or_rejected(
    factory: Callable[[], GroupBy],
    rejected: tuple[type[Exception], ...] = (ValueError,),
) -> GroupBy | None:
    """Build a spec; None when construction rejects the arguments."""
    try:
        return factory()
    except rejected:
        return None


class _ExportFailedError(Exception):
    """Raised by the test's entry source part-way through an export."""


def _entries_then_failure(entries: Iterable[LogEntry]) -> Iterator[LogEntry]:
    yield from entries
    raise _ExportFailedError


def _sample_entries(tmp_path: Path) -> list[LogEntry]:
    data = (
        threadtime("first entry", pid=101, tid=102, level="D", tag="Alpha")
        + threadtime(
            'second, with "quotes" and é日本',
            pid=201,
            tid=202,
            level="E",
            tag="Beta",
            time_="03:04:06.500",
        )
    ).encode("utf-8")
    path = _write(tmp_path, data, name="sample.log")
    return list(read_file(path, year=2026))


# -- LogFileReader / read_file -------------------------------------------------


def test_reader_yields_threadtime_fields(tmp_path: Path) -> None:
    """Reading a threadtime file yields one entry per line with all its fields."""
    path = _write(
        tmp_path,
        (
            threadtime("hello world", pid=111, tid=222, level="W", tag="MyTag")
            + threadtime("bye", pid=333, tid=444, level="E", tag="Other", date="11-19")
        ).encode("utf-8"),
    )

    entries = list(read_file(path, year=2025))

    assert [(e.pid, e.tid, e.level, e.tag, e.message) for e in entries] == [
        (111, 222, "W", "MyTag", "hello world"),
        (333, 444, "E", "Other", "bye"),
    ]
    ts = entries[0].timestamp
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute, ts.second) == (
        2025,
        1,
        2,
        3,
        4,
        5,
    )
    assert ts.microsecond == 678000


def test_reader_filter_by_yields_only_matching_entries(tmp_path: Path) -> None:
    """A reader with filter_by yields only the entries the filter accepts."""
    path = _write(
        tmp_path,
        (
            threadtime("info", level="I")
            + threadtime("error", level="E")
            + threadtime("warn", level="W")
        ).encode("utf-8"),
    )

    entries = list(LogFileReader(path, filter_by=Filter(level="E")))

    assert [e.message for e in entries] == ["error"]


def test_reader_keeps_non_threadtime_line_as_message(tmp_path: Path) -> None:
    """A line that is not in threadtime format is still yielded, its text as message."""
    path = _write(
        tmp_path,
        b"--------- beginning of main\n" + threadtime("real").encode("utf-8"),
    )

    entries = list(read_file(path))

    assert [e.message for e in entries] == ["--------- beginning of main", "real"]


def test_reader_invalid_utf8_does_not_abort_reading(tmp_path: Path) -> None:
    """Undecodable bytes in a line do not stop the file from being read."""
    path = _write(
        tmp_path,
        threadtime("bad \xff byte").encode("latin-1")
        + threadtime("after", pid=4321).encode("utf-8"),
    )

    entries = list(read_file(path))

    assert [e.pid for e in entries] == [1234, 4321]
    assert entries[0].message.startswith("bad ")
    assert entries[0].message.endswith(" byte")
    assert entries[1].message == "after"


def test_reader_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    """Reading a file that does not exist raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        list(read_file(tmp_path / "missing.log"))


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F1-2: universal newlines split a line at a bare CR inside a message",
)
def test_reader_bare_cr_cannot_forge_a_second_entry(tmp_path: Path) -> None:
    """A bare CR inside a message stays part of that one entry (no forged entry)."""
    forged = "01-15 00:00:00.000  1000  1000 E AndroidRuntime: FATAL EXCEPTION: main"
    path = _write(
        tmp_path,
        threadtime(f"hello\r{forged}", pid=4321, tag="App").encode("utf-8"),
    )

    entries = list(read_file(path))

    assert [(e.pid, e.tag) for e in entries] == [(4321, "App")]
    assert entries[0].message.startswith("hello")
    assert entries[0].message.endswith("FATAL EXCEPTION: main")


def test_reader_crlf_file_has_clean_message_and_raw(tmp_path: Path) -> None:
    """In a CRLF file each line is one entry: message has no CR, raw the line text."""
    first = threadtime("hello", pid=11).rstrip("\n")
    second = threadtime("world", pid=22).rstrip("\n")
    path = _write(tmp_path, f"{first}\r\n{second}\r\n".encode())

    entries = list(read_file(path))

    assert [(e.pid, e.message) for e in entries] == [(11, "hello"), (22, "world")]
    assert [e.raw.rstrip("\r\n") for e in entries] == [first, second]


def test_reader_crlf_raw_is_not_rewritten(tmp_path: Path) -> None:
    """raw of a CRLF line is the original line, not rewritten to end in LF.

    raw is the line without its terminator (design 2.6), so a CRLF line and an
    LF line give the same raw; keeping a rewritten LF would be wrong.
    """
    line = threadtime("hello").rstrip("\n")
    path = _write(tmp_path, f"{line}\r\n".encode())

    (entry,) = read_file(path)

    assert entry.raw in {f"{line}\r\n", line}


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F1-2: universal newlines turn each CR CR LF into an extra empty entry",
)
def test_reader_cr_cr_lf_file_has_no_phantom_entries(tmp_path: Path) -> None:
    """A file with CR CR LF line ends yields one entry per line, no empty extras."""
    first = threadtime("hello", pid=11).rstrip("\n")
    second = threadtime("world", pid=22).rstrip("\n")
    path = _write(tmp_path, f"{first}\r\r\n{second}\r\r\n".encode())

    entries = list(read_file(path))

    assert [(e.pid, e.message) for e in entries] == [(11, "hello"), (22, "world")]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F1-7: a UTF-8 BOM is kept, so the first line falls back to a raw entry",
)
def test_reader_utf8_bom_keeps_first_entry(tmp_path: Path) -> None:
    """A UTF-8 BOM at the start of a file does not lose or corrupt the first entry."""
    path = _write(
        tmp_path,
        b"\xef\xbb\xbf"
        + (
            threadtime("first", level="E", tag="MyTag")
            + threadtime("second", level="W", tag="MyTag")
        ).encode("utf-8"),
    )

    entries = list(read_file(path))

    assert [(e.pid, e.level, e.tag, e.message) for e in entries] == [
        (1234, "E", "MyTag", "first"),
        (1234, "W", "MyTag", "second"),
    ]


# -- ThreadTimeLogParser year inference ------------------------------------------


def _years(tmp_path: Path, stamps: list[tuple[str, str]], year: int) -> list[int]:
    data = "".join(
        threadtime(f"line {i}", date=date, time_=time_)
        for i, (date, time_) in enumerate(stamps)
    ).encode("utf-8")
    path = _write(tmp_path, data, name="years.log")
    return [e.timestamp.year for e in read_file(path, year=year)]


def test_threadtime_new_year_rolls_the_year_forward(tmp_path: Path) -> None:
    """Lines 12-31 then 01-01 are dated in the default year and the next year."""
    stamps = [("12-31", "23:59:59.990"), ("01-01", "00:00:00.010")]

    assert _years(tmp_path, stamps, 2024) == [2024, 2025]


def test_threadtime_interleaved_new_year_does_not_drift(tmp_path: Path) -> None:
    """Interleaved 12-31 / 01-01 lines around New Year do not gain a year per flip."""
    stamps = [
        ("12-31", "23:59:59.990"),
        ("01-01", "00:00:00.010"),
        ("12-31", "23:59:59.995"),
        ("01-01", "00:00:00.020"),
    ]

    assert _years(tmp_path, stamps, 2024) == [2024, 2025, 2024, 2025]


def test_reader_iterated_twice_gives_same_timestamps(tmp_path: Path) -> None:
    """Iterating the same LogFileReader twice yields the same timestamps both times.

    The second file spans eight months in steps under half a year. A session
    carried over from the first pass would place the second pass's Jan 1 next
    to the Sep 1 it ended on, in 2025; a fresh session per iteration and per
    read_file call keeps every pass in 2024 (F6-1).
    """
    path = _write(
        tmp_path,
        (
            threadtime("old", date="12-31", time_="23:59:59.990")
            + threadtime("new", date="01-01", time_="00:00:00.010")
        ).encode("utf-8"),
    )
    reader = LogFileReader(path, year=2024)

    first = [e.timestamp for e in reader]
    second = [e.timestamp for e in reader]

    assert len(first) == 2
    assert second == first

    span = _write(
        tmp_path,
        "".join(
            threadtime(f"line {i}", date=date, time_="12:00:00.000")
            for i, date in enumerate(("01-01", "05-01", "09-01"))
        ).encode("utf-8"),
        name="span.log",
    )
    span_reader = LogFileReader(span, year=2024)
    passes = [[e.timestamp.year for e in span_reader] for _ in range(2)]
    passes += [[e.timestamp.year for e in read_file(span, year=2024)] for _ in range(2)]

    assert passes == [[2024, 2024, 2024]] * 4


def test_threadtime_feb29_in_non_leap_year_keeps_fields(tmp_path: Path) -> None:
    """A 02-29 line read with a non-leap year= still yields its pid/level/tag.

    Only a date taken from the line itself is pinned, not the exact day or
    year (the resolver picks the nearest leap year: D-12).
    """
    path = _write(
        tmp_path,
        threadtime(
            "boom", pid=77, tid=78, level="E", tag="AndroidRuntime", date="02-29"
        ).encode("utf-8"),
    )

    (entry,) = read_file(path, year=2025)

    assert (entry.pid, entry.tid, entry.level, entry.tag, entry.message) == (
        77,
        78,
        "E",
        "AndroidRuntime",
        "boom",
    )
    ts = entry.timestamp
    assert (ts.month, ts.day) in {(2, 28), (2, 29), (3, 1)}


# -- Brief / Process / Tag parsers -------------------------------------------------

_FORMAT_CASES = [
    pytest.param(
        BriefLogParser,
        "W/MyTag   ( 1234): hello there",
        {"level": "W", "tag": "MyTag", "pid": 1234, "message": "hello there"},
        id="brief",
    ),
    pytest.param(
        ProcessLogParser,
        "W( 1234) hello there",
        {"level": "W", "pid": 1234, "message": "hello there"},
        id="process",
    ),
    pytest.param(
        TagLogParser,
        "W/MyTag   : hello there",
        {"level": "W", "tag": "MyTag", "message": "hello there"},
        id="tag",
    ),
]

_EMPTY_MESSAGE_CASES = [
    pytest.param(
        ThreadTimeLogParser,
        threadtime("", pid=1234, tid=99, level="E", tag="MyTag").rstrip("\n"),
        {"level": "E", "tag": "MyTag", "pid": 1234, "tid": 99, "message": ""},
        id="threadtime",
    ),
    pytest.param(
        BriefLogParser,
        "E/MyTag   ( 1234): ",
        {"level": "E", "tag": "MyTag", "pid": 1234, "message": ""},
        id="brief",
    ),
    pytest.param(
        ProcessLogParser,
        "E( 1234) ",
        {"level": "E", "pid": 1234, "message": ""},
        id="process",
    ),
    pytest.param(
        TagLogParser,
        "E/MyTag   : ",
        {"level": "E", "tag": "MyTag", "message": ""},
        id="tag",
    ),
]


def _parse_one(tmp_path: Path, parser: LogParser, line: str) -> LogEntry:
    path = _write(tmp_path, f"{line}\n".encode(), name="format.log")
    (entry,) = read_file(path, parser=parser)
    return entry


@pytest.mark.parametrize(("parser_cls", "line", "expected"), _FORMAT_CASES)
def test_format_parser_extracts_fields(
    tmp_path: Path,
    parser_cls: type[LogParser],
    line: str,
    expected: dict[str, object],
) -> None:
    """Brief / process / tag parsers extract the fields their format carries."""
    entry = _parse_one(tmp_path, parser_cls(), line)

    assert {name: getattr(entry, name) for name in expected} == expected


@pytest.mark.parametrize(("parser_cls", "line", "expected"), _EMPTY_MESSAGE_CASES)
def test_format_parser_empty_message_keeps_fields(
    tmp_path: Path,
    parser_cls: type[LogParser],
    line: str,
    expected: dict[str, object],
) -> None:
    """A line with an empty message still yields its level, tag and pid (F6-3)."""
    entry = _parse_one(tmp_path, parser_cls(), line)

    assert {name: getattr(entry, name) for name in expected} == expected


# -- LogEntry.json_payload() -------------------------------------------------------


def test_json_payload_finds_embedded_object() -> None:
    """json_payload parses the JSON object embedded in a log message."""
    entry = _entry('state changed {"a": 1, "b": [true, null]} (done)')

    assert entry.json_payload() == {"a": 1, "b": [True, None]}


def test_json_payload_is_none_without_json() -> None:
    """json_payload is None for a message that contains no JSON."""
    assert _entry("no braces or brackets here").json_payload() is None


# json_payload() on deep brackets (F8-1): the cost of one stdlib decode attempt
# grows with the nesting depth it descends (on 3.14 also with the C stack), so
# the work counter is the depth each attempt faces, not wall time.
_DEEP_BRACKETS = 100_000
_MAX_DEPTH_PER_ATTEMPT = 1024


def test_json_payload_on_deep_brackets_returns_quickly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """json_payload() on 100 000 nested '[' gives None without deep decoding.

    The message is all '[', so a decode attempt at offset ``i`` of a text
    made of it descends at most ``len(text) - i`` levels, the text being the
    message or the part of it the scanner hands the decoder. The 2.0.0 scanner
    started up to 4096 attempts at nearly full depth; a bounded scan hands the
    decoder no attempt deeper than ``_MAX_DEPTH_PER_ATTEMPT``. Counting depth
    instead of time keeps the check exact on every Python version and stack
    size.
    """
    message = "[" * _DEEP_BRACKETS
    depths: list[int] = []
    decode = json.JSONDecoder.raw_decode

    def counted(self: json.JSONDecoder, s: str, idx: int = 0) -> tuple[Any, int]:
        depths.append(len(s) - idx)
        return decode(self, s, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", counted)

    payload = _entry(message).json_payload()

    assert payload is None
    assert max(depths, default=0) <= _MAX_DEPTH_PER_ATTEMPT, (
        f"{len(depths)} decode attempts, the deepest {max(depths)} levels"
    )


def test_json_payload_with_huge_integer_does_not_raise() -> None:
    """A message with a 5000-digit integer does not make json_payload() raise (F8-2)."""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)  # CPython's default limit, made explicit
    try:
        payload = _entry("count=[" + "1" * 5000 + "]").json_payload()
    finally:
        sys.set_int_max_str_digits(previous)

    assert payload is None or isinstance(payload, list)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param('{{"a": 1}}', {"a": 1}, id="doubled-brace"),
        pytest.param('[1, 2 {"k": 1}', {"k": 1}, id="missing-delimiter"),
    ],
)
def test_json_payload_finds_object_inside_failed_candidate(
    message: str, expected: dict[str, int]
) -> None:
    """json_payload() finds a complete object where the outer candidate failed.

    The decoder stops the outer candidate at the nested '{' (``exc.pos``); the
    scan resumes there, at ``max(start + 1, exc.pos)`` (F8-3), not one past it.
    """
    assert _entry(message).json_payload() == expected


@pytest.mark.parametrize(
    "message",
    [
        pytest.param('{"outer": {"ok":1}', id="truncated-object"),
        pytest.param('[1, {"ok": 1}', id="truncated-array"),
    ],
)
def test_json_payload_is_none_for_truncated_outer_container(message: str) -> None:
    """A truncated outer container gives None, even around a complete object.

    Pins the top-level-candidate heuristic: the decoder reports a truncated
    container's error at the end of the message, the scan resumes there, and
    inner values are never tried. N-utils-io-package-4 keeps this behavior
    (its F8-3 fix still gives None here) and plans to document it; it is not
    part of F8-3.
    """
    assert _entry(message).json_payload() is None


# -- export_logs -------------------------------------------------------------------


def test_export_json_round_trips_fields(tmp_path: Path) -> None:
    """export_logs(format='json') writes an array that loads back to the same fields."""
    entries = _sample_entries(tmp_path)
    dest = tmp_path / "out.json"

    export_logs(entries, dest, format="json")

    data = json.loads(dest.read_text(encoding="utf-8"))
    assert [
        (d["pid"], d["tid"], d["level"], d["tag"], d["message"], d["raw"]) for d in data
    ] == [(e.pid, e.tid, e.level, e.tag, e.message, e.raw) for e in entries]
    assert [datetime.fromisoformat(d["timestamp"]) for d in data] == [
        e.timestamp for e in entries
    ]


def test_export_csv_round_trips_fields(tmp_path: Path) -> None:
    """export_logs(format='csv') writes a header and one row of fields per entry."""
    entries = _sample_entries(tmp_path)
    dest = tmp_path / "out.csv"

    export_logs(entries, dest, format="csv")

    with dest.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [
        (int(r["pid"]), int(r["tid"]), r["level"], r["tag"], r["message"], r["raw"])
        for r in rows
    ] == [(e.pid, e.tid, e.level, e.tag, e.message, e.raw) for e in entries]
    assert [datetime.fromisoformat(r["timestamp"]) for r in rows] == [
        e.timestamp for e in entries
    ]


def test_export_csv_neutralizes_formula_cells(tmp_path: Path) -> None:
    """A message starting with '=' is not written as a live spreadsheet formula."""
    message = '=HYPERLINK("http://example.invalid","click")'
    dest = tmp_path / "out.csv"

    export_logs([_entry(message)], dest, format="csv")

    with dest.open(encoding="utf-8", newline="") as f:
        (row,) = csv.DictReader(f)
    assert message in row["message"]
    assert not row["message"].lstrip().startswith(_FORMULA_PREFIXES)


def test_export_unsupported_format_raises_value_error(tmp_path: Path) -> None:
    """An unsupported export format raises ValueError and writes no file."""
    dest = tmp_path / "out.xml"

    with pytest.raises(ValueError, match="xml"):
        export_logs([_entry()], dest, format=cast("Any", "xml"))

    assert not dest.exists()


@pytest.mark.parametrize("fmt", ["json", "csv"])
@pytest.mark.parametrize(
    "entries_before_failure",
    [
        pytest.param(0, id="before-first-entry"),
        pytest.param(1, id="midway"),
    ],
)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F9-4: exporters truncate the destination before the export succeeds",
)
def test_export_error_keeps_previous_destination(
    tmp_path: Path, fmt: ExportFormat, entries_before_failure: int
) -> None:
    """An export whose input fails leaves the previous valid file untouched.

    The source fails before its first entry (a wrong input path) or after one
    entry (an error midway); fewer entries than the previous export, so a
    partial file can never equal the previous one by accident.
    """
    entries = _sample_entries(tmp_path)
    dest = tmp_path / f"out.{fmt}"
    export_logs(entries, dest, format=fmt)
    previous = dest.read_bytes()

    with pytest.raises(_ExportFailedError):
        export_logs(
            _entries_then_failure(entries[:entries_before_failure]), dest, format=fmt
        )

    assert dest.read_bytes() == previous


# -- grouping ------------------------------------------------------------------------


def test_log_grouper_splits_on_key_change_and_time_gap() -> None:
    """Consecutive grouping: a key change or a time gap splits the group."""
    spec = GroupBy(keys=("pid",), gap_ms=10.0)
    entries = [
        _entry("a", ms=0, pid=1),
        _entry("b", ms=5, pid=1),
        _entry("c", ms=6, pid=2),
        _entry("d", ms=500, pid=2),
    ]

    groups = _groups(spec, entries)

    assert _messages(groups) == [["a", "b"], ["c"], ["d"]]
    assert [group.reason for group in groups] == ["key", "gap", "end"]


def test_log_grouper_caps_group_size() -> None:
    """A group never holds more than max_group_size entries."""
    spec = GroupBy(keys=("pid",), gap_ms=100.0, max_group_size=3)
    entries = [_entry(str(i), ms=i) for i in range(7)]

    groups = _groups(spec, entries)

    assert [len(g) for g in groups] == [3, 3, 1]
    assert [e.message for g in groups for e in g] == [str(i) for i in range(7)]
    assert [group.reason for group in groups] == ["size", "size", "end"]


def test_windowed_grouper_keeps_interleaved_keys_apart() -> None:
    """Windowed grouping keeps interleaved keys in separate groups."""
    spec = GroupBy(keys=("pid",), gap_ms=100.0, mode="windowed")
    entries = [
        _entry("a1", ms=0, pid=1),
        _entry("b1", ms=1, pid=2),
        _entry("a2", ms=2, pid=1),
        _entry("b2", ms=3, pid=2),
    ]

    groups = _groups(spec, entries)

    assert sorted(_messages(groups)) == [["a1", "a2"], ["b1", "b2"]]


def test_windowed_grouper_emits_window_after_threshold() -> None:
    """A window is emitted when a later entry arrives after its threshold elapsed."""
    spec = GroupBy(keys=("pid",), gap_ms=10.0, mode="windowed")
    pulled: list[str] = []

    def source() -> Iterator[LogEntry]:
        for entry in (_entry("a", ms=0), _entry("b", ms=50)):
            pulled.append(entry.message)
            yield entry

    groups = spec.groups(source())
    first = next(groups)

    assert (_messages([first]), first.reason, pulled) == ([["a"]], "gap", ["a", "b"])
    assert _messages(groups) == [["b"]]


@pytest.mark.parametrize("field", ["package", "uid"])
def test_windowed_grouper_tie_with_unorderable_keys(
    field: Literal["package", "uid"],
) -> None:
    """Same-timestamp entries whose keys cannot be ordered (None vs str) still group.

    Reached through the public API by grouping on an optional field: ``package``
    (None or a name) or ``uid`` (None or a number).
    """
    spec = GroupBy(keys=(field,), gap_ms=100.0, mode="windowed")
    value: Any = "com.example" if field == "package" else 10123
    entries = [
        _entry("none").model_copy(update={field: None}),
        _entry("set").model_copy(update={field: value}),
    ]

    groups = _groups(spec, entries)

    assert sorted(_messages(groups)) == [["none"], ["set"]]


@pytest.mark.parametrize("mode", ["consecutive", "windowed"])
@pytest.mark.parametrize("max_group_size", [1, 0, -1])
def test_grouper_tiny_max_group_size_is_rejected_or_respected(
    mode: GroupMode, max_group_size: int
) -> None:
    """max_group_size 1 yields groups of one entry; 0 and -1 are rejected."""
    spec = _built_or_rejected(
        lambda: GroupBy(
            keys=("pid",), gap_ms=100.0, mode=mode, max_group_size=max_group_size
        )
    )
    if max_group_size < 1:
        assert spec is None
        return
    assert spec is not None

    groups = _groups(spec, [_entry(str(i), ms=i) for i in range(4)])

    assert [len(g) for g in groups] == [1, 1, 1, 1]


@pytest.mark.parametrize("max_groups", [0, -1])
def test_windowed_grouper_nonpositive_max_groups_is_rejected_or_handled(
    max_groups: int,
) -> None:
    """max_groups <= 0 is rejected at construction."""
    with pytest.raises(ValueError, match="max_groups"):
        GroupBy(keys=("pid",), gap_ms=100.0, mode="windowed", max_groups=max_groups)


@pytest.mark.parametrize("mode", ["consecutive", "windowed"])
def test_grouper_by_bare_string_is_rejected_or_means_that_field(
    mode: GroupMode,
) -> None:
    """keys='pid' (a bare string) is rejected with TypeError (design 4.2)."""
    bare: Any = "pid"

    with pytest.raises(TypeError, match="keys"):
        GroupBy(keys=bare, gap_ms=100.0, mode=mode)


@pytest.mark.parametrize("mode", ["consecutive", "windowed"])
def test_grouper_rejects_unknown_emit_mode(mode: str) -> None:
    """emit_mode is gone (design 2.1): passing it, or a mode typo, is rejected."""
    removed: dict[str, Any] = {"emit_mode": "group"}
    typo: Any = mode[:-1]  # "consecutiv", "windowe"

    with pytest.raises(TypeError, match="emit_mode"):
        GroupBy(keys=("pid",), gap_ms=100.0, **removed)
    with pytest.raises(ValueError, match="mode"):
        GroupBy(keys=("pid",), gap_ms=100.0, mode=typo)
