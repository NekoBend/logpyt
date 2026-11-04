"""Tests for the line pipeline (design 5.2, 5.5, 2.8; F5-4 and F6-5 are in
tests/test_readers.py, where read_file applies them).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from logpyt import GroupBy, LogEntry, LogGroup, Package, Tag, Where
from logpyt._core import pipeline as pipeline_module
from logpyt._core.framing import FramedLine
from logpyt._core.pipeline import Pipeline
from logpyt.parsers import (
    BriefLogParser,
    ParseContext,
    RawLogParser,
    ThreadTimeLogParser,
    context,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from logpyt import Condition
    from logpyt.parsers import LogParser

APP_UID = 10065
APP = "com.example.app"


def _line(
    message: str, *, uid: str = str(APP_UID), pid: int = 1234, tag: str = "App"
) -> FramedLine:
    stamp = "2026-11-04 05:11:16.661446 +0000"
    text = f"{stamp} {uid:>5} {pid:5d} {pid:5d} I {tag}: {message}"
    return FramedLine(text.encode())


def _pipeline(
    *,
    parser: LogParser | None = None,
    where: Condition | None = None,
    group_by: GroupBy | None = None,
    uid_packages: dict[int, str] | None = None,
) -> Pipeline:
    return Pipeline(
        ThreadTimeLogParser() if parser is None else parser,
        ParseContext(year=2026),
        where=where,
        group_by=group_by,
        uid_packages=uid_packages,
    )


def _entries(pipe: Pipeline, lines: Sequence[FramedLine]) -> list[LogEntry]:
    out: list[LogEntry] = []
    for line in lines:
        for item in pipe.push(line):
            assert isinstance(item, LogEntry)
            out.append(item)
    assert pipe.finish() == ()
    return out


@pytest.mark.parametrize(
    "parser", [ThreadTimeLogParser(), RawLogParser()], ids=["threadtime", "raw"]
)
def test_divider_is_no_entry_and_not_unparsed(parser: LogParser) -> None:
    """logcat's buffer dividers are dropped before parsing, for any parser."""
    pipe = _pipeline(parser=parser)
    lines = [
        FramedLine(b"--------- beginning of main"),
        _line("a"),
        FramedLine(b"--------- switch to crash"),
    ]

    entries = _entries(pipe, lines)

    assert [e.raw for e in entries] == [lines[1].data.decode()]
    assert (pipe.lines, pipe.dividers, pipe.unparsed) == (3, 2, 0)


def test_dash_message_is_not_a_divider() -> None:
    """Only logcat's two divider shapes are dropped; a raw message that merely
    starts with dashes is a line like any other.
    """
    pipe = _pipeline(parser=RawLogParser())
    texts = [
        "--------- beginning of",
        "--------- hello world",
        "--------- main",
        # The divider shape followed by more text: a prefix match would drop
        # these without counting them.
        "--------- beginning of main loop",
        "--------- switch to crash: x",
        "--------- beginning of Main",
    ]

    entries = _entries(pipe, [FramedLine(t.encode()) for t in texts])

    assert [e.message for e in entries] == texts
    assert pipe.dividers == 0


def test_unrecognised_line_becomes_unparsed_entry() -> None:
    """A line the parser rejects is an entry with parsed=False, counted."""
    pipe = _pipeline()

    (entry,) = _entries(pipe, [FramedLine(b"not a log line \x1b[31m\x00")])

    assert not entry.parsed
    assert entry.message == entry.raw == "not a log line \x1b[31m\x00"
    assert pipe.unparsed == 1


def test_cut_line_gives_a_truncated_entry() -> None:
    """A cut line still parses; its entry says it was cut, and so does a fallback."""
    pipe = _pipeline()
    cut = FramedLine(_line("start of a long message").data, truncated=True)
    junk = FramedLine(b"junk", truncated=True)

    entries = _entries(pipe, [cut, _line("whole"), junk])

    assert [(e.parsed, e.truncated) for e in entries] == [
        (True, True),
        (True, False),
        (False, True),
    ]
    assert entries[0].message == "start of a long message"
    assert pipe.truncated == 2


@pytest.mark.parametrize("char", ["日", "\U0001f600"], ids=["3b", "4b"])
def test_cut_line_ends_before_a_split_character(char: str) -> None:
    """A line cut inside a character ends before it, with no U+FFFD; an
    uncut line that ends inside one keeps the U+FFFD, the file holds it.
    """
    full = _line("x" + char * 2).data
    for dropped in range(1, len(char.encode())):
        data = full[:-dropped]
        pipe = _pipeline()

        cut, whole = _entries(
            pipe, [FramedLine(data, truncated=True), FramedLine(data)]
        )

        assert (cut.message, cut.truncated) == ("x" + char, True)
        assert cut.raw == data.decode(errors="ignore")
        assert whole.message == "x" + char + "\ufffd"


def test_uid_map_attributes_the_entries_it_names() -> None:
    """An entry whose uid the map names gets that package; others stay None."""
    pipe = _pipeline(uid_packages={APP_UID: APP, 1000: "android"})
    lines = [
        _line("app"),
        _line("system", uid="1000"),
        _line("root", uid="root"),
        _line("other", uid="10066"),
        FramedLine(b"unparsed"),
    ]

    entries = _entries(pipe, lines)

    assert [e.package for e in entries] == [APP, "android", None, None, None]
    assert [e.uid for e in entries] == [APP_UID, 1000, 0, 10066, None]


def test_uid_map_matches_secondary_users_by_app_id() -> None:
    """A secondary user's uid matches its app id, uid modulo 100000 (design 5.8),
    whether the column prints it as a number or as a u<user>_a<n> name.
    """
    pipe = _pipeline(uid_packages={APP_UID: APP, 10005: "com.example.five"})
    lines = [
        _line("user 0"),
        _line("user 10", uid="1010065"),
        _line("user 1 name", uid="u1_a5"),
        _line("other app of user 10", uid="1010066"),
    ]

    entries = _entries(pipe, lines)

    assert [(e.uid, e.package) for e in entries] == [
        (APP_UID, APP),
        (1_010_065, APP),
        (110_005, "com.example.five"),
        (1_010_066, None),
    ]


def test_uid_map_key_for_a_full_uid_wins_over_the_app_id() -> None:
    """A key for one user's uid beats the app-id match for that user only."""
    pipe = _pipeline(uid_packages={APP_UID: APP, 1_010_065: "com.work"})
    lines = [
        _line("user 0"),
        _line("user 10", uid="1010065"),
        _line("user 11", uid="1110065"),
    ]

    entries = _entries(pipe, lines)

    assert [e.package for e in entries] == [APP, "com.work", APP]


def test_uid_map_overrides_the_parser_package() -> None:
    """A package the parser set is replaced by the map's, for a mapped uid."""

    class _Parser:
        logcat_format = "x"

        def parse(self, line: str, ctx: ParseContext) -> LogEntry | None:
            return LogEntry(
                timestamp=ctx.now(),
                pid=1,
                tid=1,
                level="I",
                tag="T",
                message=line,
                raw=line,
                uid=APP_UID if line == "mapped" else 10066,
                package="com.parser",
            )

    pipe = _pipeline(parser=_Parser(), uid_packages={APP_UID: APP})

    entries = _entries(pipe, [FramedLine(b"mapped"), FramedLine(b"unmapped")])

    assert [e.package for e in entries] == [APP, "com.parser"]


def test_without_a_uid_map_nothing_is_attributed() -> None:
    """No map, no package: a file carries no attribution of its own."""
    (entry,) = _entries(_pipeline(), [_line("app")])

    assert entry.package is None
    assert entry.uid == APP_UID


def test_attribution_runs_before_where() -> None:
    """where sees the attributed package, so a Package condition can match."""
    pipe = _pipeline(where=Package(APP) | Tag("Keep"), uid_packages={APP_UID: APP})
    lines = [
        _line("mine"),
        _line("theirs", uid="10066"),
        _line("kept", uid="10066", tag="Keep"),
    ]

    entries = _entries(pipe, lines)

    assert [e.message for e in entries] == ["mine", "kept"]
    assert (pipe.lines, pipe.rejected) == (3, 1)


def test_where_sees_the_truncation_flag() -> None:
    """The cut flag is set before where runs."""
    pipe = _pipeline(where=Where(lambda e: e.truncated))
    lines = [_line("whole"), FramedLine(_line("cut").data, truncated=True)]

    assert [e.message for e in _entries(pipe, lines)] == ["cut"]


def test_group_by_groups_and_flushes_at_finish() -> None:
    """With group_by the pipeline yields groups; finish() closes the rest ("end")."""
    spec = GroupBy(keys=("pid",), gap_ms=1000.0)
    pipe = _pipeline(group_by=spec, where=~Tag("Drop"))
    lines = [
        _line("a1", pid=1),
        _line("dropped", pid=2, tag="Drop"),
        _line("a2", pid=1),
        _line("b1", pid=2),
    ]

    groups: list[LogGroup] = []
    for line in lines:
        for item in pipe.push(line):
            assert isinstance(item, LogGroup)
            groups.append(item)
    groups += pipe.finish()

    assert [([e.message for e in g], g.reason) for g in groups] == [
        (["a1", "a2"], "key"),
        (["b1"], "end"),
    ]


def test_parser_exception_propagates() -> None:
    """A raising parser is not caught: offline runs in the caller's loop."""

    class _BoomError(ValueError):
        pass

    class _Raising:
        logcat_format = "x"

        def parse(self, line: str, ctx: ParseContext) -> LogEntry | None:
            del line, ctx
            raise _BoomError

    with pytest.raises(_BoomError):
        _pipeline(parser=_Raising()).push(FramedLine(b"a"))


def test_where_exception_propagates() -> None:
    """A raising where is not caught either (design 4.2, read_file row)."""

    class _BoomError(ValueError):
        pass

    def _explode(entry: LogEntry) -> bool:
        raise _BoomError(entry.message)

    pipe = _pipeline(where=Where(_explode))

    with pytest.raises(_BoomError, match="a"):
        pipe.push(_line("a"))
    assert pipe.rejected == 0


def test_wall_time_comes_only_from_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline reads wall-clock time only through ctx.now(): with a context
    that fixes it, no line reads the host clock (design 5.2; F6-4 keeps the
    default context on the host clock).
    """
    fixed = datetime(2000, 1, 1, 12, 0)  # noqa: DTZ001  (naive like host time)

    class _FixedContext(ParseContext):
        def now(self) -> datetime:
            return fixed

    def _no_host_clock() -> datetime:
        msg = "the host clock was read"
        raise AssertionError(msg)

    monkeypatch.setattr(context, "host_now", _no_host_clock)
    cases: list[tuple[LogParser, bytes]] = [
        (ThreadTimeLogParser(), b"not a log line"),  # the fallback entry
        (BriefLogParser(), b"I/Tag( 1): brief"),
        (RawLogParser(), b"raw"),
        # The first yearless line without year or anchor: placed before now.
        (ThreadTimeLogParser(), b"11-19 12:34:56.789  1  1 D T: yearless"),
    ]

    entries = [
        Pipeline(parser, _FixedContext()).entry(FramedLine(data))
        for parser, data in cases
    ]

    assert [e.timestamp if e else None for e in entries] == [
        fixed,
        fixed,
        fixed,
        datetime(1999, 11, 19, 12, 34, 56, 789000),  # noqa: DTZ001  (1999, not 2000)
    ]
    assert [e.parsed if e else None for e in entries] == [False, True, True, True]


def test_parser_returning_no_entry_raises_type_error() -> None:
    """A parser must return a LogEntry or None; anything else is a TypeError."""

    class _Wrong:
        logcat_format = "x"

        def parse(self, line: str, ctx: ParseContext) -> object:
            del ctx
            return {"line": line}

    pipe = _pipeline(parser=_Wrong())  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match=r"_Wrong\.parse returned dict"):
        pipe.push(FramedLine(b"a"))


def test_entries_with_a_uid_are_counted() -> None:
    """with_uid counts the entries whose uid is known, before where rejects
    them; a uid name logpyt does not know, a line without the column, an
    unparsed line and a divider carry none.
    """
    pipe = _pipeline(where=Tag("Nothing"))
    lines = [
        _line("number"),
        _line("name", uid="root"),
        _line("unknown name", uid="zzz"),
        FramedLine(b"11-19 12:34:56.789  1  1 D T: no uid column"),
        FramedLine(b"junk"),
        FramedLine(b"--------- beginning of main"),
    ]

    assert _entries(pipe, lines) == []
    assert (pipe.with_uid, pipe.rejected) == (2, 5)


def test_line_conservation() -> None:
    """Every pushed line is a divider, a rejected entry or a delivered entry."""
    pipe = _pipeline(where=Tag("App"))
    lines = [
        FramedLine(b"--------- beginning of system"),
        _line("a"),
        _line("b", tag="Other"),
        FramedLine(b"junk"),
        _line("c"),
    ]

    delivered = _entries(pipe, lines)

    assert pipe.lines == pipe.dividers + pipe.rejected + len(delivered)
    assert (pipe.dividers, pipe.rejected, len(delivered), pipe.unparsed) == (1, 2, 2, 1)


def test_module_surface() -> None:
    """pipeline.py names its public names in __all__."""
    assert pipeline_module.__all__ == ["Pipeline"]
