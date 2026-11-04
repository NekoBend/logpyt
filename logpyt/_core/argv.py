"""The ``adb logcat`` command line of a live stream (design 5.6, U-1).

::

    [adb, -s serial?, logcat, -v threadtime, -v year, -v usec, -v zone, -v uid,
     -T <sec>.<usec>?, *logcat_args, logpyt:V?]

The library's options come first and ``logpyt:V`` last, so the command does
not depend on the device's ``getopt`` permuting options that follow a
filterspec. ``logpyt:V`` is appended when ``logcat_args`` holds a filterspec
or ``-s``, so the start and flush markers (tag ``logpyt``) pass the user's
filter. The zone modifier is a toggle on the device (nougat logprint.c:258),
so the command names exactly one.

Every ``-T`` value is ``<seconds>.<microseconds:06d>``: a value of digits only
is a line count, not a time (n-logcat.cpp:672-693), and would replay the
whole ring buffer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from logpyt.adb import _MARKER_TAG, _adb_argv
from logpyt.config import _parse_logcat_args

if TYPE_CHECKING:
    from logpyt.config import StreamConfig

__all__ = ["WIRE_FORMAT", "epoch_position", "format_position", "logcat_argv"]

# U-1 (a): the live output format logpyt owns and parses.
WIRE_FORMAT: tuple[str, ...] = (
    "-v",
    "threadtime",
    "-v",
    "year",
    "-v",
    "usec",
    "-v",
    "zone",
    "-v",
    "uid",
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_PER_SECOND = 1_000_000

# A device position for -T: whole epoch seconds and microseconds.
type Position = tuple[int, int]


def epoch_position(moment: datetime) -> Position:
    """``moment`` (aware) as epoch ``(seconds, microseconds)``.

    Raises:
        ValueError: ``moment`` is naive.

    """
    if moment.tzinfo is None:
        msg = "a device position needs an aware datetime"
        raise ValueError(msg)
    micros = (moment - _EPOCH) // _MICROSECOND
    return divmod(micros, _PER_SECOND)


def format_position(position: Position) -> str:
    """The ``-T`` value of ``position``: ``<seconds>.<microseconds:06d>``.

    Raises:
        ValueError: The microseconds are outside 0..999999.

    """
    seconds, micros = position
    if not 0 <= micros < _PER_SECOND:
        msg = f"microseconds must be in 0..999999, got {micros}"
        raise ValueError(msg)
    return f"{seconds}.{micros:06d}"


def logcat_argv(
    adb_path: str, config: StreamConfig, position: Position | None
) -> tuple[str, ...]:
    """The command line of one connection (see the module docs).

    Args:
        adb_path: The resolved adb executable.
        config: The stream's validated configuration.
        position: Where the connection starts (``-T``); None reads the device
            backlog.

    """
    args: list[str] = ["logcat", *WIRE_FORMAT]
    if position is not None:
        args += ["-T", format_position(position)]
    args += config.logcat_args
    if _parse_logcat_args(config.logcat_args).filtered:
        args.append(f"{_MARKER_TAG}:V")
    return _adb_argv(adb_path, config.serial, *args)
