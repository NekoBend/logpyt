"""Behavior of the offline components through the public API.

Covers ``LogFileReader`` / ``read_file``, the format parsers, ``LogEntry``'s
JSON payload, ``export_logs`` and the groupers. No adb is involved; inputs are
real files under ``tmp_path`` or ``LogEntry`` objects built by the test.

Known bugs are written for the correct behavior and marked
``xfail(strict=True)`` with the plan id from
``.docs/review-findings-2.0.1.md``.
"""

from __future__ import annotations

import csv
import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

import pytest

from logpyt import (
    Filter,
    LogEntry,
    LogFileReader,
    LogGrouper,
    ThreadTimeLogParser,
    WindowedLogGrouper,
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


def _groups(grouper: LogGrouper, entries: Iterable[LogEntry]) -> list[list[LogEntry]]:
    """Feed ``entries`` then flush; return the emitted groups (emit_mode='group')."""
    emitted: list[LogEntry | list[LogEntry]] = []
    for entry in entries:
        emitted.extend(grouper.process(entry))
    emitted.extend(grouper.flush())
    groups: list[list[LogEntry]] = []
    for item in emitted:
        assert isinstance(item, list), f"emit_mode='group' emitted {item!r}"
        groups.append(item)
    return groups


def _messages(groups: list[list[LogEntry]]) -> list[list[str]]:
    return [[entry.message for entry in group] for group in groups]


def _built_or_rejected(
    factory: Callable[[], LogGrouper],
    rejected: tuple[type[Exception], ...] = (ValueError,),
) -> LogGrouper | None:
    """Build a grouper; None when construction rejects the arguments."""
    try:
        return factory()
    except rejected:
        return None


class _SessionEntry(LogEntry):
    """A LogEntry carrying an optional custom field, as a custom parser may build."""

    session: str | None = None


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
    return list(read_file(path, parser=ThreadTimeLogParser(default_year=2026)))


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

    entries = list(read_file(path, parser=ThreadTimeLogParser(default_year=2025)))

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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F1-2: universal newlines rewrite a CRLF line's raw to LF",
)
def test_reader_crlf_raw_is_not_rewritten(tmp_path: Path) -> None:
    """raw of a CRLF line is the original line, not rewritten to end in LF.

    Whether raw keeps the CRLF or drops the terminator is an open design point;
    rewriting CRLF to LF is wrong either way (LogEntry.raw is documented as the
    unmodified line).
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
    parser = ThreadTimeLogParser(default_year=year)
    return [e.timestamp.year for e in read_file(path, parser=parser)]


def test_threadtime_new_year_rolls_the_year_forward(tmp_path: Path) -> None:
    """Lines 12-31 then 01-01 are dated in the default year and the next year."""
    stamps = [("12-31", "23:59:59.990"), ("01-01", "00:00:00.010")]

    assert _years(tmp_path, stamps, 2024) == [2024, 2025]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F6-1: every Dec->Jan flip adds a year; late Dec lines drift forward",
)
def test_threadtime_interleaved_new_year_does_not_drift(tmp_path: Path) -> None:
    """Interleaved 12-31 / 01-01 lines around New Year do not gain a year per flip."""
    stamps = [
        ("12-31", "23:59:59.990"),
        ("01-01", "00:00:00.010"),
        ("12-31", "23:59:59.995"),
        ("01-01", "00:00:00.020"),
    ]

    assert _years(tmp_path, stamps, 2024) == [2024, 2025, 2024, 2025]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F6-1: year-rollover state persists on the parser across iterations",
)
def test_reader_iterated_twice_gives_same_timestamps(tmp_path: Path) -> None:
    """Iterating the same LogFileReader twice yields the same timestamps both times."""
    path = _write(
        tmp_path,
        (
            threadtime("old", date="12-31", time_="23:59:59.990")
            + threadtime("new", date="01-01", time_="00:00:00.010")
        ).encode("utf-8"),
    )
    reader = LogFileReader(path, parser=ThreadTimeLogParser(default_year=2024))

    first = [e.timestamp for e in reader]
    second = [e.timestamp for e in reader]

    assert len(first) == 2
    assert second == first


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F6-2: Feb 29 in a non-leap default year falls back to a raw entry",
)
def test_threadtime_feb29_in_non_leap_year_keeps_fields(tmp_path: Path) -> None:
    """A 02-29 line with a non-leap default_year still yields its pid/level/tag.

    The timestamp policy is an open decision (MASTER-e / S-parsers-models-3:
    nearest leap year, or a flagged Feb 28 / Mar 1), so only a date taken from
    the line itself is pinned, not the exact day or year.
    """
    path = _write(
        tmp_path,
        threadtime(
            "boom", pid=77, tid=78, level="E", tag="AndroidRuntime", date="02-29"
        ).encode("utf-8"),
    )

    (entry,) = read_file(path, parser=ThreadTimeLogParser(default_year=2025))

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

_F6_3 = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F6-3: Brief/Process/Tag drop every field for an empty-message line",
)

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
        marks=_F6_3,
    ),
    pytest.param(
        ProcessLogParser,
        "E( 1234) ",
        {"level": "E", "pid": 1234, "message": ""},
        id="process",
        marks=_F6_3,
    ),
    pytest.param(
        TagLogParser,
        "E/MyTag   : ",
        {"level": "E", "tag": "MyTag", "message": ""},
        id="tag",
        marks=_F6_3,
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
    """A line with an empty message still yields its level, tag and pid.

    threadtime already does (finding A4); brief / process / tag do not (F6-3).
    """
    entry = _parse_one(tmp_path, parser_cls(), line)

    assert {name: getattr(entry, name) for name in expected} == expected


# -- LogEntry.json_payload ---------------------------------------------------------


def test_json_payload_finds_embedded_object() -> None:
    """json_payload parses the JSON object embedded in a log message."""
    entry = _entry('state changed {"a": 1, "b": [true, null]} (done)')

    assert entry.json_payload == {"a": 1, "b": [True, None]}


def test_json_payload_is_none_without_json() -> None:
    """json_payload is None for a message that contains no JSON."""
    assert _entry("no braces or brackets here").json_payload is None


# The F8-1 run happens on a thread with this C stack: on 3.14 the decoder's
# nesting limit follows the stack size, so a fixed size keeps the limit (and the
# defect's cost) independent of the runner's stack rlimit. 8 MiB matches Linux's
# default main-thread stack (ulimit -s 8192), which the test used to run on.
_DEEP_STACK_BYTES = 8 * 1024 * 1024
_DEEP_MIN_DEPTH = 2**8
_DEEP_MAX_DEPTH = 2**20
_DEEP_CANDIDATES = 4096  # candidate starts extract_json tries (finding F8-1)
_DEEP_RUN_TIMEOUT = 90.0


def _parse_recurses(text: str) -> bool:
    """Does one stdlib JSON parse of ``text`` hit RecursionError?"""
    try:
        json.loads(text)
    except RecursionError:
        return True
    except ValueError:
        return False
    return False


def _one_parse_seconds(text: str) -> float:
    """Best-of-5 cost of one stdlib JSON parse of ``text``."""
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        _parse_recurses(text)
        best = min(best, time.perf_counter() - start)
    return best


class _DeepBracketsRun(NamedTuple):
    length: int
    budget: float
    elapsed: float
    payload: object


def _deep_brackets_run() -> _DeepBracketsRun | None:
    """Time json_payload on '[' nested past this thread's decoder limit.

    The depth is the first power of two from ``_DEEP_MIN_DEPTH`` whose parse
    hits RecursionError, plus ``_DEEP_CANDIDATES`` levels, so every candidate
    start the scanner tries still has that many levels ahead and recurses too.
    None when nothing up to ``_DEEP_MAX_DEPTH`` levels recurses.
    """
    depth = _DEEP_MIN_DEPTH
    while depth <= _DEEP_MAX_DEPTH and not _parse_recurses("[" * depth):
        depth *= 2
    if depth > _DEEP_MAX_DEPTH:
        return None
    message = "[" * (depth + _DEEP_CANDIDATES)
    budget = min(1.0, 50 * _one_parse_seconds(message) + 0.02)

    payload: object = "not computed"
    elapsed = float("inf")
    for _ in range(3):  # re-measure only a near miss (scheduler noise)
        entry = _entry(message)
        start = time.perf_counter()
        payload = entry.json_payload
        elapsed = time.perf_counter() - start
        if elapsed <= budget or elapsed > 5 * budget:
            break
    return _DeepBracketsRun(len(message), budget, elapsed, payload)


def _call_with_stack[T](
    fn: Callable[[], T], *, what: str, stack_bytes: int, timeout: float
) -> T:
    """Return ``fn()`` run on a new thread with a ``stack_bytes`` stack.

    The join is bounded by ``timeout``; an exception from ``fn`` is re-raised.
    """
    results: list[T] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            results.append(fn())
        except BaseException as exc:  # noqa: BLE001  (re-raised by the caller)
            errors.append(exc)

    previous = threading.stack_size(stack_bytes)
    try:
        worker = threading.Thread(target=target, name=what, daemon=True)
        worker.start()
    finally:
        threading.stack_size(previous)
    worker.join(timeout)
    if worker.is_alive():
        pytest.fail(f"{what} still running after {timeout:.0f}s")
    if errors:
        raise errors[0]
    return results[0]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F8-1: extract_json re-parses deep nesting up to 4096 times",
)
def test_json_payload_on_deep_brackets_returns_quickly() -> None:
    """json_payload on '[' nested past the decoder's limit returns None quickly.

    The budget is "within 1 s and within ~50 single parse attempts of the same
    text (+20 ms)". The relative part keeps the check meaningful on every
    Python version: the C decoder's nesting limit, hence the cost of one
    attempt, differs a lot between 3.12, 3.13 and 3.14 (on 3.12/3.13 the bug
    costs well under 1 s but still ~4096 attempts).

    The depth is probed, not fixed. On 3.14 the limit follows the C stack, not
    sys.getrecursionlimit(): a fixed 65536 decoded without RecursionError on
    the ubuntu py3.14 CI cell, which never reaches the slow path, and with an
    unlimited stack no depth recurses at all. So the run uses a thread with a
    fixed stack (``_DEEP_STACK_BYTES``) and a depth that recurses there.
    """
    run = _call_with_stack(
        _deep_brackets_run,
        what="deep-brackets json_payload run",
        stack_bytes=_DEEP_STACK_BYTES,
        timeout=_DEEP_RUN_TIMEOUT,
    )
    if run is None:
        pytest.skip(f"no RecursionError up to {_DEEP_MAX_DEPTH} nested '[' here")

    assert run.payload is None
    assert run.elapsed <= run.budget, (
        f"json_payload on {run.length} nested '[' took {run.elapsed:.3f}s"
        f" (budget {run.budget:.3f}s)"
    )


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="F8-2: a >4300-digit integer raises ValueError out of json_payload",
)
def test_json_payload_with_huge_integer_does_not_raise() -> None:
    """A message holding a 5000-digit integer does not make json_payload raise."""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)  # CPython's default limit, made explicit
    try:
        payload = _entry("count=[" + "1" * 5000 + "]").json_payload
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
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F8-3: the scan resumes at exc.pos + 1, past a candidate at exc.pos",
)
def test_json_payload_finds_object_inside_failed_candidate(
    message: str, expected: dict[str, int]
) -> None:
    """json_payload finds a complete object where the outer candidate failed.

    The decoder stops the outer candidate at the nested '{' (``exc.pos``) and
    the scan resumes one character later, skipping it. Resuming at
    ``max(start + 1, exc.pos)`` (N-utils-io-package-4) recovers both cases.
    """
    assert _entry(message).json_payload == expected


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
    assert _entry(message).json_payload is None


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


# -- groupers ----------------------------------------------------------------------


def test_log_grouper_splits_on_key_change_and_time_gap() -> None:
    """LogGrouper groups consecutive same-key entries; a key change or gap splits."""
    grouper = LogGrouper(by=["pid"], threshold_ms=10.0, emit_mode="group")
    entries = [
        _entry("a", ms=0, pid=1),
        _entry("b", ms=5, pid=1),
        _entry("c", ms=6, pid=2),
        _entry("d", ms=500, pid=2),
    ]

    assert _messages(_groups(grouper, entries)) == [["a", "b"], ["c"], ["d"]]


def test_log_grouper_caps_group_size() -> None:
    """LogGrouper never emits a group larger than max_group_size."""
    grouper = LogGrouper(
        by=["pid"], threshold_ms=100.0, emit_mode="group", max_group_size=3
    )
    entries = [_entry(str(i), ms=i) for i in range(7)]

    groups = _groups(grouper, entries)

    assert [len(g) for g in groups] == [3, 3, 1]
    assert [e.message for g in groups for e in g] == [str(i) for i in range(7)]


def test_windowed_grouper_keeps_interleaved_keys_apart() -> None:
    """WindowedLogGrouper keeps interleaved keys in separate groups."""
    grouper = WindowedLogGrouper(by=["pid"], threshold_ms=100.0, emit_mode="group")
    entries = [
        _entry("a1", ms=0, pid=1),
        _entry("b1", ms=1, pid=2),
        _entry("a2", ms=2, pid=1),
        _entry("b2", ms=3, pid=2),
    ]

    groups = _groups(grouper, entries)

    assert sorted(_messages(groups)) == [["a1", "a2"], ["b1", "b2"]]


def test_windowed_grouper_emits_window_after_threshold() -> None:
    """A window is emitted when a later entry arrives after its threshold elapsed."""
    grouper = WindowedLogGrouper(by=["pid"], threshold_ms=10.0, emit_mode="group")

    assert grouper.process(_entry("a", ms=0)) == []
    emitted = grouper.process(_entry("b", ms=50))

    assert [[e.message for e in cast("list[LogEntry]", g)] for g in emitted] == [["a"]]
    assert _messages(_groups(grouper, [])) == [["b"]]


@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="F7-1: expiry-heap ties compare group keys; None vs str raises TypeError",
)
def test_windowed_grouper_tie_with_unorderable_keys() -> None:
    """Same-timestamp entries whose keys cannot be ordered (None vs str) still group.

    Reached through the public API by grouping on an optional field of a
    LogEntry subclass, as a custom parser may produce.
    """
    grouper = WindowedLogGrouper(by=["session"], threshold_ms=100.0, emit_mode="group")
    entries = [
        _SessionEntry(
            timestamp=_T0,
            pid=1,
            tid=1,
            level="I",
            tag="T",
            message=name,
            raw=name,
            session=session,
        )
        for name, session in (("none", None), ("s1", "s1"))
    ]

    groups = _groups(grouper, entries)

    assert sorted(_messages(groups)) == [["none"], ["s1"]]


_GROUPER_CLASSES = [
    pytest.param(
        LogGrouper,
        id="LogGrouper",
        marks=pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason="F7-2: the cap is checked only from the 2nd entry; groups of 2",
        ),
    ),
    pytest.param(WindowedLogGrouper, id="WindowedLogGrouper"),
]


@pytest.mark.parametrize("grouper_cls", _GROUPER_CLASSES)
@pytest.mark.parametrize("max_group_size", [1, 0, -1])
def test_grouper_tiny_max_group_size_is_rejected_or_respected(
    grouper_cls: type[LogGrouper], max_group_size: int
) -> None:
    """max_group_size <= 1 is either rejected or yields groups of one entry."""
    grouper = _built_or_rejected(
        lambda: grouper_cls(
            by=["pid"],
            threshold_ms=100.0,
            emit_mode="group",
            max_group_size=max_group_size,
        )
    )
    if grouper is None:
        return

    groups = _groups(grouper, [_entry(str(i), ms=i) for i in range(4)])

    assert [len(g) for g in groups] == [1, 1, 1, 1]


@pytest.mark.parametrize("max_groups", [0, -1])
@pytest.mark.xfail(
    strict=True,
    raises=StopIteration,
    reason="F7-2: max_groups<=0 makes process() raise StopIteration",
)
def test_windowed_grouper_nonpositive_max_groups_is_rejected_or_handled(
    max_groups: int,
) -> None:
    """max_groups <= 0 is either rejected or still delivers every entry once."""
    grouper = _built_or_rejected(
        lambda: WindowedLogGrouper(
            by=["pid"], threshold_ms=100.0, emit_mode="group", max_groups=max_groups
        )
    )
    if grouper is None:
        return

    groups = _groups(grouper, [_entry("a", ms=0, pid=1), _entry("b", ms=1, pid=2)])

    assert sorted(e.message for g in groups for e in g) == ["a", "b"]


@pytest.mark.parametrize("grouper_cls", [LogGrouper, WindowedLogGrouper])
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F7-2: by='pid' becomes ('p', 'i', 'd'), a constant key",
)
def test_grouper_by_bare_string_is_rejected_or_means_that_field(
    grouper_cls: type[LogGrouper],
) -> None:
    """by='pid' (a bare string) is either rejected or groups by the pid field."""
    grouper = _built_or_rejected(
        lambda: grouper_cls(by="pid", threshold_ms=100.0, emit_mode="group"),
        rejected=(TypeError, ValueError),
    )
    if grouper is None:
        return

    groups = _groups(grouper, [_entry("a", ms=0, pid=1), _entry("b", ms=1, pid=2)])

    assert sorted(_messages(groups)) == [["a"], ["b"]]


@pytest.mark.parametrize("grouper_cls", [LogGrouper, WindowedLogGrouper])
@pytest.mark.xfail(
    strict=True,
    raises=pytest.fail.Exception,
    reason="F7-2: an emit_mode typo is accepted and silently behaves as 'entry'",
)
def test_grouper_rejects_unknown_emit_mode(grouper_cls: type[LogGrouper]) -> None:
    """An unknown emit_mode (a typo such as 'groups') is rejected at construction."""
    typo: Any = "groups"

    with pytest.raises((ValueError, TypeError)):
        grouper_cls(by=["pid"], threshold_ms=100.0, emit_mode=typo)
