"""Entries for lines a parser did not recognise, shared by every reader."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import LogEntry

if TYPE_CHECKING:
    from ..models import LogLevel
    from .context import ParseContext

__all__ = ["is_divider", "strip_terminator", "unparsed_entry"]

# logcat's buffer dividers ("--------- beginning of main", "--------- switch
# to crash"): output of logcat itself, not a log line of any format.
_DIVIDER_PREFIX = "--------- "


def strip_terminator(line: str) -> str:
    """``line`` without its line terminator: one LF, or one CR LF.

    Nothing else is stripped (D-13): a bare CR inside the line and every other
    character stay.
    """
    if line.endswith("\n"):
        return line[:-2] if line.endswith("\r\n") else line[:-1]
    return line


def is_divider(text: str) -> bool:
    """Is ``text`` (a line without its terminator) a logcat buffer divider?"""
    return text.startswith(_DIVIDER_PREFIX)


def unparsed_entry(line: str, ctx: ParseContext, *, level: LogLevel = "I") -> LogEntry:
    """The fallback entry for a line no parser recognised (``parsed=False``).

    Its message and raw are the line without its terminator; it has no pid, tid
    or tag, and its timestamp is the host time (:meth:`ParseContext.now`).
    """
    text = strip_terminator(line)
    return LogEntry(
        timestamp=ctx.now(),
        pid=0,
        tid=0,
        level=level,
        tag="",
        message=text,
        raw=text,
        parsed=False,
    )
