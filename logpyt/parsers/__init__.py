"""Parsers for logcat's text formats (offline files; design 2.8).

A parser turns one line into a :class:`~logpyt.models.LogEntry`, or returns
None for a line it does not recognise. Parsers are stateless; a
:class:`ParseContext` holds what one session (one read of a file, one stream
connection) needs between lines.
"""

from .context import ParseContext
from .logcat import (
    BriefLogParser,
    LogParser,
    ProcessLogParser,
    RawLogParser,
    TagLogParser,
    ThreadTimeLogParser,
)

__all__ = [
    "BriefLogParser",
    "LogParser",
    "ParseContext",
    "ProcessLogParser",
    "RawLogParser",
    "TagLogParser",
    "ThreadTimeLogParser",
]
