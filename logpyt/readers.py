"""Offline reading: `read_file` (design 2.8).

A log file is read as bytes and goes through the same stages as a live stream's
output: the byte framer (``logpyt._core.framing``: LF-only lines, a line cap, a
BOM stripped at offset 0), then the line pipeline (``logpyt._core.pipeline``:
decode, parse, attribution from ``uid_packages``, ``where``, ``group_by``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, overload

from ._core.framing import DEFAULT_MAX_LINE_BYTES, MIN_MAX_LINE_BYTES, Framer
from ._core.pipeline import Pipeline
from .adb import _check_package_name
from .filters import _require_condition
from .groupers import GroupBy
from .parsers import ParseContext, ThreadTimeLogParser

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .filters import Condition
    from .models import LogEntry, LogGroup
    from .parsers import LogParser

__all__ = ["read_file"]

logger = logging.getLogger(__name__)

# The size of one read from the file. The framer bounds what it keeps of a
# line, so the chunk size bounds nothing and only sets the syscall rate.
_CHUNK_BYTES = 64 * 1024


@overload
def read_file(
    path: str | os.PathLike[str],
    *,
    parser: LogParser | None = None,
    where: Condition | None = None,
    group_by: None = None,
    year: int | None = None,
    uid_packages: Mapping[int, str] | None = None,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[LogEntry]: ...
@overload
def read_file(
    path: str | os.PathLike[str],
    *,
    parser: LogParser | None = None,
    where: Condition | None = None,
    group_by: GroupBy,
    year: int | None = None,
    uid_packages: Mapping[int, str] | None = None,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[LogGroup]: ...
def read_file(
    path: str | os.PathLike[str],
    *,
    parser: LogParser | None = None,
    where: Condition | None = None,
    group_by: GroupBy | None = None,
    year: int | None = None,
    uid_packages: Mapping[int, str] | None = None,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[LogEntry | LogGroup]:
    """Read a log file lazily: its entries, or its groups with ``group_by``.

    The arguments are checked when ``read_file`` is called; the file is opened
    at the first ``next()`` and read in one pass. Every call is a parse session
    of its own, so reading a file twice resolves its yearless dates the same
    way (F6-1). Lines without a timestamp (brief, process, tag and raw lines)
    and unrecognised lines are dated with the host clock when they are read,
    so a second read gives them other timestamps.

    How lines are read:

    - A line ends at LF only. The CRs before the LF are stripped (CR LF and CR
      CR LF files read like LF files); a CR inside a line stays in its message.
    - A UTF-8 BOM at the start of the file is skipped. Each line is decoded as
      UTF-8; an undecodable byte becomes U+FFFD.
    - A line longer than ``max_line_bytes`` keeps its first ``max_line_bytes``
      bytes and gives an entry with ``truncated=True``; the next line is read
      as usual. When the cut falls inside a character, the entry ends before
      that character (at most 3 bytes are dropped), not with a U+FFFD.
    - logcat's ``--------- beginning of <buffer>`` dividers are skipped.
    - A line the parser does not recognise is yielded as an entry with
      ``parsed=False`` whose message is the whole line.
    - With ``uid_packages``, an entry whose ``uid`` the map names gets that
      ``package``. A secondary user's uid (>= 100000) that the map does not
      name matches by its app id, uid modulo 100000, as live streams match
      (design 5.8): ``{10065: pkg}`` also attributes ``1010065`` (user 10),
      while a key for ``1010065`` itself wins over it. A file carries no
      other attribution.

    At the end of a read, or when the iterator is closed early, one WARNING on
    logger ``logpyt.readers`` reports the lines the parser did not recognise
    (a format mismatch, F6-5), and another the cut lines, each only if there
    were any. With ``uid_packages``, a third one says so when lines were
    parsed but none of them carries a uid (a file without a ``-v uid``
    column): no entry could be attributed. At the call, one WARNING names
    each package of ``where`` that no ``uid_packages`` value names: no line
    can carry that package.

    Args:
        path: The log file.
        parser: Splits a line into an entry; None means `ThreadTimeLogParser`.
        where: Only entries that meet this condition are yielded (or grouped).
        group_by: Yield `LogGroup` objects, as `GroupBy.groups` would; the
            groups still open at the end of the file close with reason
            ``"end"``. None yields the entries.
        year: The year of the first line that carries no year (yearless
            ``-v threadtime``). None places it no later than two days after
            the file's modification time (or the host clock when the
            modification time is not a valid local datetime).
        uid_packages: uid -> package name, for lines with a ``-v uid`` column;
            a key is a full uid or an app id (uid modulo 100000). Required
            when ``where`` names packages (`Condition.packages`): without it
            no line could carry a package.
        max_line_bytes: The longest line kept whole, in bytes; >= 4096.

    Returns:
        An iterator over the entries, or over the groups with ``group_by``.

    Raises:
        TypeError: An argument has the wrong type: ``where`` is not a
            `Condition` (wrap a function in `Where`), ``group_by`` not a
            `GroupBy`, ``parser`` is a class or has no ``parse`` method, a
            ``uid_packages`` key is not an int.
        ValueError: ``where`` names packages and ``uid_packages`` is None
            (F5-4); ``year`` is outside 1..9999; a ``uid_packages`` key is
            negative or a value is not a package name; ``max_line_bytes`` is
            below 4096; ``path`` contains a NUL character.
        OSError: From the first ``next()``: the file cannot be opened or read.

    Exceptions raised by a custom parser or condition propagate unchanged and
    end the read.

    """
    file_path = Path(path)
    if "\x00" in str(file_path):
        # open() would raise this ValueError only at the first next().
        msg = "path must not contain a NUL character"
        raise ValueError(msg)
    checked_parser = _check_parser(parser)
    checked_where = None if where is None else _require_condition(where, "where")
    if group_by is not None and not isinstance(group_by, GroupBy):
        msg = f"group_by must be a GroupBy, not {type(group_by).__name__}"
        raise TypeError(msg)
    ParseContext(year=year)  # a bad year fails here, not at the first line
    uids = _check_uid_packages(uid_packages)
    _check_max_line_bytes(max_line_bytes)
    if checked_where is not None:
        _check_package_condition(checked_where, uids)
    return _read(
        file_path,
        checked_parser,
        where=checked_where,
        group_by=group_by,
        year=year,
        uid_packages=uids,
        max_line_bytes=max_line_bytes,
    )


def _read(
    path: Path,
    parser: LogParser,
    *,
    where: Condition | None,
    group_by: GroupBy | None,
    year: int | None,
    uid_packages: Mapping[int, str] | None,
    max_line_bytes: int,
) -> Iterator[LogEntry | LogGroup]:
    """The generator behind `read_file`, for checked arguments."""
    framer = Framer(max_line_bytes, strip_bom=True)
    with path.open("rb", buffering=0) as file:
        # The mtime anchors the first yearless line only without year=.
        anchor = None if year is not None else _mtime(file.fileno())
        pipeline = Pipeline(
            parser,
            ParseContext(year=year, anchor=anchor),
            where=where,
            group_by=group_by,
            uid_packages=uid_packages,
        )
        try:
            while chunk := file.read(_CHUNK_BYTES):
                for line in framer.feed(chunk):
                    yield from pipeline.push(line)
            for line in framer.finish():
                yield from pipeline.push(line)
            yield from pipeline.finish()
        finally:
            _report(
                path,
                parser,
                pipeline,
                max_line_bytes,
                mapped=uid_packages is not None,
            )


def _report(
    path: Path,
    parser: LogParser,
    pipeline: Pipeline,
    max_line_bytes: int,
    *,
    mapped: bool,
) -> None:
    """Log one WARNING each for unrecognised lines, for cut lines and, with a
    uid map (``mapped``), for parsed lines none of which carries a uid.
    """
    if pipeline.unparsed:
        logger.warning(
            "%d of %d lines in %s are not in the %r format of %s; "
            "they were read as unparsed entries (parsed=False).",
            pipeline.unparsed,
            pipeline.lines,
            path,
            getattr(parser, "logcat_format", "?"),
            type(parser).__name__,
        )
    if pipeline.truncated:
        logger.warning(
            "%d of %d lines in %s are longer than max_line_bytes=%d; "
            "their entries hold at most the first %d bytes (truncated=True).",
            pipeline.truncated,
            pipeline.lines,
            path,
            max_line_bytes,
            max_line_bytes,
        )
    parsed = pipeline.lines - pipeline.dividers - pipeline.unparsed
    if mapped and parsed and not pipeline.with_uid:
        # A file without a -v uid column: the map attributes nothing, and a
        # Package condition matches no line; the F5-4 check at the call
        # cannot tell, it reads no line.
        logger.warning(
            "uid_packages was given, but none of the %d parsed lines in %s "
            "carries a uid (logcat -v uid): no entry was attributed.",
            parsed,
            path,
        )


def _mtime(fd: int) -> datetime | None:
    """The modification time of the open file ``fd``, as naive host-local time.

    None when it cannot be read or is no datetime in host-local time (an mtime
    far outside the years 1..9999, or one the platform's time functions
    refuse); the first yearless line is then placed before the host clock.
    """
    try:
        moment = datetime.fromtimestamp(os.fstat(fd).st_mtime, tz=UTC)
        return moment.astimezone().replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return None


# -- argument checks --------------------------------------------------------------


def _check_parser(parser: LogParser | None) -> LogParser:
    """``parser``, or a `ThreadTimeLogParser` for None.

    Raises:
        TypeError: ``parser`` is a class, not an instance, or has no callable
            ``parse`` (a 2.x parser has ``parse_stdout`` instead).

    """
    if parser is None:
        return ThreadTimeLogParser()
    if isinstance(parser, type):
        # A class has a callable parse too; it would fail at the first line.
        msg = (
            f"parser must be a parser instance, not the class {parser.__name__}: "
            f"pass {parser.__name__}()"
        )
        raise TypeError(msg)
    if not callable(getattr(parser, "parse", None)):
        msg = (
            "parser must have a parse(line, ctx) method (the LogParser "
            f"protocol); {type(parser).__name__} has none"
        )
        raise TypeError(msg)
    return parser


def _check_uid_packages(value: Mapping[int, str] | None) -> dict[int, str] | None:
    """A copy of ``value``, checked: int uids >= 0 to package names.

    The copy keeps a read from seeing later changes to the caller's mapping.

    Raises:
        TypeError: ``value`` is not a mapping, a key is not an int (a bool is
            not), or a value is not a str.
        ValueError: A key is negative, or a value is not a package name.

    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        msg = (
            "uid_packages must be a mapping of uid to package name, "
            f"not {type(value).__name__}"
        )
        raise TypeError(msg)
    checked: dict[int, str] = {}
    for uid, package in value.items():
        if isinstance(uid, bool) or not isinstance(uid, int):
            msg = f"uid_packages keys must be int uids, not {type(uid).__name__}"
            raise TypeError(msg)
        if uid < 0:
            msg = f"uid_packages keys must be uids >= 0, not {uid}"
            raise ValueError(msg)
        checked[uid] = _check_package_name(package)
    return checked


def _check_max_line_bytes(value: object) -> None:
    """Check that ``value`` is an int of at least 4096.

    Raises:
        TypeError: ``value`` is not an int (a bool is not).
        ValueError: ``value`` is below 4096.

    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"max_line_bytes must be an int, not {type(value).__name__}"
        raise TypeError(msg)
    if value < MIN_MAX_LINE_BYTES:
        msg = f"max_line_bytes must be at least {MIN_MAX_LINE_BYTES}, not {value}"
        raise ValueError(msg)


def _check_package_condition(where: Condition, uids: Mapping[int, str] | None) -> None:
    """Check the packages ``where`` names against the uid map.

    Without a map a file attributes no line, so a condition that names packages
    is refused (F5-4). With one, a WARNING names each package that no map value
    names: its `Package` leaves can never match. That is a warning, not an
    error, because ``~Package(name)`` or an `Or` still selects lines.

    Raises:
        ValueError: ``uids`` is None and ``where.packages()`` is not empty.

    """
    # An override at the root may return any iterable; the walk normalises
    # only the overrides below the root.
    names = frozenset(where.packages())
    if not names:
        return
    if uids is not None:
        mapped = set(uids.values())
        for name in sorted(names - mapped):
            logger.warning(
                "where names package %r, but no uid in uid_packages maps to it: "
                "no line can carry that package.",
                name,
            )
        return
    listed = ", ".join(repr(name) for name in sorted(names))
    msg = (
        f"where depends on the package of each line ({listed}), but a log "
        "file carries no package attribution: pass uid_packages={uid: "
        "package} to attribute lines by their -v uid column, or use "
        "CrashCondition(package=)/AnrCondition(package=), which read the "
        "message text"
    )
    raise ValueError(msg)
