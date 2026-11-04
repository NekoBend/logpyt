"""Tests for the byte framer (design 5.5; F1-2, F1-4, F1-7, X-4).

Each input is also fed in every two-chunk split and byte by byte: a line, a
CR LF, a multibyte character or a BOM split across chunks must frame exactly as
the whole input does.
"""

from __future__ import annotations

import random

import pytest

from logpyt._core import framing
from logpyt._core.framing import DEFAULT_MAX_LINE_BYTES, FramedLine, Framer, decode_line
from logpyt.parsers import ParseContext, ThreadTimeLogParser

BOM = b"\xef\xbb\xbf"
LIMIT = 8

type Lines = list[tuple[bytes, bool]]


def _frame(chunks: list[bytes], *, limit: int = LIMIT, strip_bom: bool = True) -> Lines:
    framer = Framer(limit, strip_bom=strip_bom)
    lines = [line for chunk in chunks for line in framer.feed(chunk)]
    lines += framer.finish()
    return [(line.data, line.truncated) for line in lines]


def _splits(data: bytes) -> list[list[bytes]]:
    """``data`` whole, in every two-chunk split, and byte by byte."""
    two = [[data[:i], data[i:]] for i in range(len(data) + 1)]
    return [[data], *two, [data[i : i + 1] for i in range(len(data))]]


def _check(
    data: bytes, expected: Lines, *, limit: int = LIMIT, bom: bool = True
) -> None:
    for chunks in _splits(data):
        assert _frame(chunks, limit=limit, strip_bom=bom) == expected, chunks


def _reference(data: bytes, limit: int, *, strip_bom: bool) -> Lines:
    """The framing rules of design 5.5, applied to the whole input at once."""
    if strip_bom:
        data = data.removeprefix(BOM)
    parts = data.split(b"\n")
    last = parts.pop()
    if last:
        parts.append(last)
    return [
        (content[:limit], len(content) > limit)
        for content in (part.rstrip(b"\r") for part in parts)
    ]


# -- the proving tests of design section 7 -----------------------------------------


def test_bare_cr_stays_in_message() -> None:
    """A bare CR inside a line is content: it never starts a forged entry (F1-2)."""
    forged = "01-15 00:00:00.000  1000  1000 E AndroidRuntime: FATAL EXCEPTION: main"
    line = f"01-02 03:04:05.678  4321  4321 I App: hello\r{forged}"
    data = f"{line}\n".encode()

    for chunks in _splits(data):
        assert _frame(chunks, limit=DEFAULT_MAX_LINE_BYTES) == [(line.encode(), False)]
    entry = ThreadTimeLogParser().parse(line, ParseContext(year=2026))
    assert entry is not None
    assert (entry.pid, entry.tag) == (4321, "App")
    assert entry.message == f"hello\r{forged}"


def test_oversized_line_keeps_following_lines() -> None:
    """A line over the cap keeps its first bytes, flagged, and the lines after it
    are kept: one physical line never becomes two (F1-4, X-4).
    """
    data = b"0123456789abcdef\nnext\n" + b"x" * 30 + b"\r\nlast"

    _check(
        data,
        [
            (b"01234567", True),
            (b"next", False),
            (b"xxxxxxxx", True),
            (b"last", False),
        ],
    )


# -- terminators -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "end",
    [b"\n", b"\r\n", b"\r\r\n", b"\r\r\r\n"],
    ids=["lf", "crlf", "crcrlf", "3cr-lf"],
)
def test_crs_before_lf_are_the_terminator(end: bytes) -> None:
    """LF ends a line; every CR right before it is stripped with it."""
    _check(b"one" + end + b"two" + end, [(b"one", False), (b"two", False)])


def test_cr_alone_never_ends_a_line() -> None:
    """Old Mac CR line ends are not line ends: CR only is one line."""
    _check(b"a\rb\rc\n", [(b"a\rb\rc", False)])


def test_cr_at_chunk_end_is_content_when_text_follows() -> None:
    """A CR at the end of a chunk is decided by the next byte, not by the chunk."""
    assert _frame([b"a\r", b"b\n"]) == [(b"a\rb", False)]
    assert _frame([b"a\r", b"\n"]) == [(b"a", False)]
    assert _frame([b"a\r", b"\r", b"\r", b"\nb\n"]) == [(b"a", False), (b"b", False)]


def test_lf_split_from_its_line() -> None:
    """A line whose LF comes in the next chunk is emitted once the LF arrives."""
    framer = Framer(LIMIT)
    assert framer.feed(b"abc") == []
    assert framer.feed(b"\n") == [FramedLine(b"abc")]
    assert framer.finish() == []


def test_empty_lines_are_lines() -> None:
    """An empty line is a line; the end of the input after an LF is not."""
    _check(b"\n\r\na\n\n", [(b"", False), (b"", False), (b"a", False), (b"", False)])


# -- the last line ---------------------------------------------------------------------


def test_partial_last_line_is_emitted_at_eof() -> None:
    """Bytes after the last LF are the last line, emitted by finish()."""
    framer = Framer(LIMIT)
    assert framer.feed(b"a\nb") == [FramedLine(b"a")]
    assert framer.finish() == [FramedLine(b"b")]
    assert framer.finish() == []


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", []),
        (b"a\n", [(b"a", False)]),
        (b"a\r", [(b"a", False)]),
        (b"a\r\r", [(b"a", False)]),
        (b"a\n\r", [(b"a", False), (b"", False)]),
    ],
    ids=["empty", "lf", "cr", "crcr", "lone-cr"],
)
def test_crs_at_eof_end_the_last_line(data: bytes, expected: Lines) -> None:
    """CRs at the very end are stripped like a terminator."""
    _check(data, expected)


def test_feed_after_finish_raises() -> None:
    """The input has ended: more bytes are a caller error, not a new line."""
    framer = Framer(LIMIT)
    framer.finish()
    with pytest.raises(RuntimeError, match="after finish"):
        framer.feed(b"a\n")


# -- the line cap ----------------------------------------------------------------------


def test_line_of_exactly_the_cap_is_whole() -> None:
    """The cap is inclusive: a line of max_line_bytes is not cut, one more byte is."""
    _check(b"12345678\n123456789\n", [(b"12345678", False), (b"12345678", True)])


def test_terminator_crs_do_not_count_toward_the_cap() -> None:
    """CRs before the LF are no content, however many there are."""
    _check(b"12345678" + b"\r" * 20 + b"\nok\n", [(b"12345678", False), (b"ok", False)])


def test_bare_crs_count_toward_the_cap() -> None:
    """CRs followed by more content are content and count toward the cap."""
    _check(b"1234567\r\rx\nok\n", [(b"1234567\r", True), (b"ok", False)])
    _check(b"1\r\r\r\r\r\r\r\rx\n", [(b"1\r\r\r\r\r\r\r", True)])


def test_cut_line_is_emitted_before_its_lf() -> None:
    """A line over the cap is delivered at once; its rest up to the LF is dropped."""
    framer = Framer(LIMIT)
    assert framer.feed(b"0123456789") == [FramedLine(b"01234567", truncated=True)]
    assert framer.feed(b"more and more") == []
    assert framer.feed(b"tail\nnext") == []
    assert framer.finish() == [FramedLine(b"next")]


def test_cut_line_at_eof_is_not_emitted_twice() -> None:
    """A cut line whose LF never comes was delivered already."""
    framer = Framer(LIMIT)
    assert framer.feed(b"0123456789") == [FramedLine(b"01234567", truncated=True)]
    assert framer.finish() == []


def test_open_line_is_bounded_by_the_cap() -> None:
    """The framer keeps at most max_line_bytes of a line (design 5.10), also for a
    run of CRs that may still be the terminator.
    """
    framer = Framer(LIMIT)
    framer.feed(b"1234567")
    for _ in range(1000):
        framer.feed(b"\r" * 1000)
        assert len(framer._buf) <= LIMIT
    assert framer.feed(b"\n") == [FramedLine(b"1234567")]
    for _ in range(100):
        framer.feed(b"y" * 1000)
        assert len(framer._buf) <= LIMIT


def test_default_cap_is_64_kib() -> None:
    """The default line cap of design 2.8."""
    assert DEFAULT_MAX_LINE_BYTES == 64 * 1024
    big = b"z" * (DEFAULT_MAX_LINE_BYTES + 1)
    framer = Framer()
    assert framer.feed(big + b"\n") == [FramedLine(big[:-1], truncated=True)]


# -- BOM -------------------------------------------------------------------------------


def test_bom_at_offset_0_is_stripped() -> None:
    """A UTF-8 BOM at the start is no content (F1-7)."""
    _check(BOM + b"a\nb\n", [(b"a", False), (b"b", False)])
    _check(BOM, [])
    _check(BOM + b"\n", [(b"", False)])


def test_bom_after_offset_0_is_content() -> None:
    """Only offset 0 is special: a BOM later on, or a second one, stays."""
    _check(b"a\n" + BOM + b"b\n", [(b"a", False), (BOM + b"b", False)])
    _check(BOM + BOM + b"a\n", [(BOM + b"a", False)])


def test_bom_split_across_the_first_chunks() -> None:
    """The BOM is recognised one byte at a time too."""
    assert _frame([b"\xef", b"\xbb", b"\xbf", b"a\n"]) == [(b"a", False)]
    assert _frame([b"\xef\xbb", b"\xbfa\n"]) == [(b"a", False)]
    assert _frame([b"", b"\xef", b"", b"\xbb\xbfa"]) == [(b"a", False)]


def test_bom_prefix_that_is_no_bom_is_content() -> None:
    """Bytes that only start like a BOM are content, also when the input ends."""
    _check(b"\xef\xbbX\n", [(b"\xef\xbbX", False)])
    assert _frame([b"\xef", b"\xbb"]) == [(b"\xef\xbb", False)]
    assert _frame([b"\xef"]) == [(b"\xef", False)]


def test_bom_kept_without_strip_bom() -> None:
    """Live pipes frame without the BOM rule: the bytes stay."""
    _check(BOM + b"a\n", [(BOM + b"a", False)], bom=False)


# -- decoding --------------------------------------------------------------------------


def test_multibyte_character_split_across_chunks_decodes() -> None:
    """A UTF-8 character split by a chunk boundary survives: decoding is per line."""
    text = "é日本\U0001f600 end"
    data = f"{text}\n".encode()
    for chunks in _splits(data):
        lines = _frame(chunks, limit=DEFAULT_MAX_LINE_BYTES)
        assert [decode_line(line) for line, _ in lines] == [text]


def test_invalid_utf8_becomes_replacement_characters() -> None:
    """An undecodable byte becomes U+FFFD; the rest of the line stays."""
    assert decode_line(b"bad \xff byte") == "bad � byte"
    assert decode_line(b"cut \xe6\x97") == "cut �"


# -- every rule at once ----------------------------------------------------------------


@pytest.mark.parametrize("seed", range(20))
def test_random_input_frames_like_the_reference(seed: int) -> None:
    """Random bytes in random chunks frame like the rules applied to the whole input."""
    rng = random.Random(seed)  # noqa: S311  (seeded test input, not secrets)
    alphabet = [b"a", b"\r", b"\n", b"\xc3\xa9", BOM, b"\xef"]
    data = b"".join(rng.choices(alphabet, weights=[6, 3, 3, 1, 1, 1], k=400))
    limit = rng.randint(1, 12)
    strip_bom = rng.random() < 0.5
    chunks = []
    rest = data
    while rest:
        size = rng.randint(0, 9)
        chunks.append(rest[:size])
        rest = rest[size:]

    framed = _frame(chunks, limit=limit, strip_bom=strip_bom)

    assert framed == _reference(data, limit, strip_bom=strip_bom)


def test_module_surface() -> None:
    """framing.py names its public names in __all__."""
    assert sorted(framing.__all__) == [
        "DEFAULT_MAX_LINE_BYTES",
        "FramedLine",
        "Framer",
        "MIN_MAX_LINE_BYTES",
        "decode_line",
    ]
