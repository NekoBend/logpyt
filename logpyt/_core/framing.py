"""Byte framing: the bytes of a file or a pipe in, physical lines out (design 5.5).

One framer serves files (`logpyt.read_file`) and, from PR-12 on, every pipe of a
live stream, so both read lines by the same rules:

- Lines end at ``b"\\n"`` only. Every ``b"\\r"`` right before it is part of the
  terminator and stripped, so CR LF and the CR CR LF of old Windows adb both end a
  line; a bare CR inside a line stays in it (F1-2: no forged second entry).
- A line longer than ``max_line_bytes`` keeps its first ``max_line_bytes``, is
  emitted at once with ``truncated=True``, and the rest of it up to the next LF is
  discarded: the following lines are kept, and one physical line never becomes
  two (F1-4, X-4).
- The bytes after the last LF are the last line, emitted by `Framer.finish` at EOF.
- With ``strip_bom=True`` (files), a UTF-8 BOM is stripped at offset 0 only (F1-7).

The framer works on bytes and is incremental: chunks of any size go in, and a
line, a CR LF or a BOM split across chunks comes out as if read in one piece. It
holds at most ``max_line_bytes`` bytes of the open line (design 5.10); CRs that
may still turn out to be the terminator are counted, not stored.

Decoding comes after framing, per line (`decode_line`): LF never occurs inside a
UTF-8 multibyte sequence, so a character split across chunks survives. The cap
counts bytes, so it can cut a character; the decoded text of a cut line ends
before that character instead of with a U+FFFD for it.
"""

from __future__ import annotations

import codecs
from typing import NamedTuple

__all__ = [
    "DEFAULT_MAX_LINE_BYTES",
    "MIN_MAX_LINE_BYTES",
    "FramedLine",
    "Framer",
    "decode_line",
]

# Design 2.2 and 2.8: the default line cap of StreamConfig.max_line_bytes and of
# read_file(max_line_bytes=), and the smallest cap both accept: a smaller one
# would cut ordinary logcat lines (a log message holds up to about 4 KiB).
DEFAULT_MAX_LINE_BYTES = 64 * 1024
MIN_MAX_LINE_BYTES = 4096

_LF = b"\n"
_CR = b"\r"
_BOM = b"\xef\xbb\xbf"
_UTF8_DECODER = codecs.getincrementaldecoder("utf-8")


class FramedLine(NamedTuple):
    """One physical line.

    Attributes:
        data: The line without its terminator and without the CRs before it; at
            most ``max_line_bytes`` bytes.
        truncated: True when the line was longer than ``max_line_bytes`` and
            ``data`` holds only its first ``max_line_bytes`` bytes.

    """

    data: bytes
    truncated: bool = False


def decode_line(data: bytes, *, cut: bool = False) -> str:
    """Decode one framed line: UTF-8, an undecodable byte becomes U+FFFD.

    With ``cut=True`` (the data of a `FramedLine` with ``truncated=True``), an
    unfinished UTF-8 sequence at the very end, at most 3 bytes, is dropped
    instead of replaced: the cap cut it off from the rest of its character.
    Python's incremental decoder decides what is unfinished; it also holds
    back ``ED A0``..``ED BF``, the start of an encoded surrogate, which is
    dropped too. Every other undecodable byte becomes U+FFFD as usual.
    """
    if not cut:
        return data.decode("utf-8", "replace")
    # final=False holds back an unfinished sequence at the end; nothing follows
    # it, so it is never decoded.
    return _UTF8_DECODER("replace").decode(data, final=False)


class Framer:
    """Splits a byte stream into `FramedLine` objects (see the module docs).

    Feed every chunk to `feed` in order, then call `finish` once at EOF. Not
    thread-safe: one caller at a time, as the stream core or `read_file` runs it.

    Args:
        max_line_bytes: The longest line kept whole, in bytes; >= 1. Trusted:
            the public entry points validate it.
        strip_bom: Strip a UTF-8 BOM at offset 0 (files).

    """

    __slots__ = (
        "_buf",
        "_crs",
        "_cut",
        "_finished",
        "_head",
        "_limit",
        "_open",
    )

    def __init__(
        self, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES, *, strip_bom: bool = False
    ) -> None:
        self._limit = max_line_bytes
        # The open line: the bytes read since the last LF. _buf holds its content
        # (at most _limit bytes); _crs counts the CRs after it, which are content
        # if more content follows and the terminator if the LF follows.
        self._buf = bytearray()
        self._crs = 0
        self._open = False  # a byte of the open line was read (a lone CR too)
        # The open line was cut and emitted; its bytes up to the next LF are
        # discarded.
        self._cut = False
        # The bytes read at offset 0 while they may still be a BOM; None once
        # the start of the input is decided or without strip_bom.
        self._head: bytes | None = b"" if strip_bom else None
        self._finished = False

    def feed(self, chunk: bytes) -> list[FramedLine]:
        """Read ``chunk`` and return the lines it completes, in order.

        A line is complete at its LF, or as soon as it exceeds ``max_line_bytes``
        (then cut, see the module docs).

        Raises:
            RuntimeError: `finish` was called already.

        """
        if self._finished:
            msg = "Framer.feed() after finish(): the input has ended"
            raise RuntimeError(msg)
        if self._head is not None:
            chunk = self._skip_bom(chunk)
        lines: list[FramedLine] = []
        if not chunk:
            return lines
        parts = chunk.split(_LF)
        rest = parts.pop()  # the bytes after the chunk's last LF
        if parts:
            whole = parts
            if self._open or self._cut:
                self._end_line(parts[0], lines)
                whole = parts[1:]
            limit = self._limit
            for part in whole:
                # A line wholly inside this chunk: the common case, kept short.
                content = part.rstrip(_CR)
                if len(content) > limit:
                    lines.append(FramedLine(content[:limit], truncated=True))
                else:
                    lines.append(FramedLine(content))
        self._extend(rest, lines)
        return lines

    def finish(self) -> list[FramedLine]:
        """End the input: return the last line if bytes follow the last LF.

        CRs at the very end are stripped like a terminator. A later call returns
        nothing.
        """
        if self._finished:
            return []
        self._finished = True
        lines: list[FramedLine] = []
        head, self._head = self._head, None
        if head:  # the input ended inside what could have been a BOM: content
            self._extend(head, lines)
        if self._open and not self._cut:
            lines.append(FramedLine(bytes(self._buf)))
        self._reset()
        return lines

    def _skip_bom(self, chunk: bytes) -> bytes:
        """``chunk`` without a BOM at offset 0, once the first bytes decide it.

        Returns b"" while the bytes read so far are a proper prefix of the BOM;
        they are kept in ``_head`` until the next chunk or `finish` decides.
        """
        head = (self._head or b"") + chunk
        if len(head) < len(_BOM) and _BOM.startswith(head):
            self._head = head
            return b""
        self._head = None
        return head.removeprefix(_BOM)

    def _end_line(self, segment: bytes, lines: list[FramedLine]) -> None:
        """End the open line with ``segment``, the bytes before an LF."""
        self._extend(segment, lines)
        if self._cut:
            # The line was emitted cut, earlier or by this segment; the LF ends
            # its discarded rest.
            self._cut = False
            return
        lines.append(FramedLine(bytes(self._buf)))
        self._reset()

    def _extend(self, segment: bytes, lines: list[FramedLine]) -> None:
        """Add ``segment``, bytes without an LF, to the open line.

        A line that becomes longer than the limit is emitted cut at once, and
        the framer discards its rest up to the next LF.
        """
        if not segment or self._cut:
            return
        self._open = True
        content = segment.rstrip(_CR)
        if not content:  # only CRs: content or terminator, the next byte decides
            self._crs += len(segment)
            return
        buf = self._buf
        room = self._limit - len(buf)
        crs = self._crs
        if crs + len(content) > room:
            # The pending CRs are content now. min() keeps a long run of them
            # from being built in memory only to be cut.
            kept_crs = min(crs, room)
            buf += _CR * kept_crs
            buf += content[: room - kept_crs]
            lines.append(FramedLine(bytes(buf), truncated=True))
            self._reset()
            self._cut = True
            return
        if crs:
            buf += _CR * crs
        buf += content
        self._crs = len(segment) - len(content)

    def _reset(self) -> None:
        """Forget the open line."""
        self._buf.clear()
        self._crs = 0
        self._open = False
