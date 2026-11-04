"""Tests for LogFileReader and read_file."""

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from logpyt.filters import Filter
from logpyt.models import LogEntry
from logpyt.parsers.logcat import ThreadTimeLogParser
from logpyt.readers import LogFileReader, read_file

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def sample_log_content() -> str:
    """Provide sample log content with mixed valid and invalid lines."""
    return (
        "11-19 12:34:56.789  1000  2000 D TagA   : Message A\n"
        "Invalid Line That Does Not Match Regex\n"
        "11-19 12:34:57.000  1000  2000 I TagB   : Message B\n"
    )


@pytest.fixture
def sample_log_file(tmp_path: Path, sample_log_content: str) -> Path:
    """Create a temporary log file with sample content."""
    log_file = tmp_path / "test.log"
    log_file.write_text(sample_log_content, encoding="utf-8")
    return log_file


def test_log_file_reader_basic_reading(sample_log_file: Path) -> None:
    """Test that LogFileReader reads and parses lines correctly."""
    # Arrange
    parser = ThreadTimeLogParser()
    reader = LogFileReader(sample_log_file, parser=parser)

    # Act
    entries = list(reader)

    # Assert
    # We expect 3 entries: a line the parser does not recognise is yielded as
    # an unparsed entry holding the whole line.
    assert len(entries) == 3

    # Verify first entry (Valid ThreadTime)
    assert entries[0].tag == "TagA"
    assert entries[0].message == "Message A"
    assert entries[0].level == "D"

    # Verify second entry (Fallback)
    assert entries[1].message == "Invalid Line That Does Not Match Regex"
    assert entries[1].level == "I"  # Default for stdout
    assert entries[1].tag == ""
    assert not entries[1].parsed
    assert entries[0].parsed
    assert entries[2].parsed

    # Verify third entry (Valid ThreadTime)
    assert entries[2].tag == "TagB"
    assert entries[2].message == "Message B"
    assert entries[2].level == "I"


def test_log_file_reader_filtering(sample_log_file: Path) -> None:
    """Test that LogFileReader applies filters correctly."""
    # Arrange
    parser = ThreadTimeLogParser()
    # Filter for TagA only
    log_filter = Filter(tag="TagA")
    reader = LogFileReader(sample_log_file, parser=parser, filter_by=log_filter)

    # Act
    entries = list(reader)

    # Assert
    assert len(entries) == 1
    assert entries[0].tag == "TagA"
    assert entries[0].message == "Message A"


def test_log_file_reader_error_handling(tmp_path: Path) -> None:
    """Test that LogFileReader skips lines that raise exceptions during parsing."""
    # Arrange
    log_file = tmp_path / "error.log"
    log_file.write_text("Line 1\nLine 2\nLine 3", encoding="utf-8")

    # Create a mock parser that raises an exception for the second line
    mock_parser = MagicMock()

    def side_effect(line: str, ctx: object) -> LogEntry:
        del ctx
        if "Line 2" in line:
            raise ValueError("Parsing failed")
        # Return a dummy entry for other lines
        return LogEntry(
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            pid=0,
            tid=0,
            level="I",
            tag="Test",
            message=line.strip(),
            raw=line,
        )

    mock_parser.parse.side_effect = side_effect

    reader = LogFileReader(log_file, parser=mock_parser)

    # Act
    entries = list(reader)

    # Assert
    assert len(entries) == 2
    assert entries[0].message == "Line 1"
    assert entries[1].message == "Line 3"


def test_read_file_convenience_function(sample_log_file: Path) -> None:
    """Test the read_file convenience function."""
    # Arrange
    parser = ThreadTimeLogParser()

    # Act
    entries = list(read_file(sample_log_file, parser=parser))

    # Assert
    assert isinstance(entries, list)
    assert len(entries) == 3
    assert entries[0].tag == "TagA"


def test_read_file_not_found() -> None:
    """Test that FileNotFoundError is raised for non-existent files."""
    # Arrange
    non_existent_file = Path("non_existent_file.log")

    # Act & Assert
    with pytest.raises(FileNotFoundError):
        list(read_file(non_existent_file))


def test_log_file_reader_parse_error_logging_throttled(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """Test parse-error logging is throttled to avoid log storms."""
    log_file = tmp_path / "throttle.log"
    log_file.write_text("bad1\nbad2\nbad3\nbad4\nbad5\n", encoding="utf-8")

    mock_parser = MagicMock()
    mock_parser.parse.side_effect = ValueError("Parsing failed")

    # Keep monotonic time fixed so all errors fall into the same interval.
    monkeypatch.setattr("logpyt.readers.time.monotonic", lambda: 123.0)

    reader = LogFileReader(
        log_file,
        parser=mock_parser,
        parse_error_log_interval=60.0,
    )

    with caplog.at_level(logging.WARNING):
        entries = list(reader)

    assert entries == []

    error_logs = [r for r in caplog.records if "Failed to parse line:" in r.message]
    summary_logs = [r for r in caplog.records if "Suppressed" in r.message]

    assert len(error_logs) == 1
    assert len(summary_logs) == 1
    assert "4 parse errors" in summary_logs[0].message


def test_log_file_reader_parse_error_logging_sanitizes_and_caps_preview(
    tmp_path: Path,
    caplog,
) -> None:
    """Parse error log preview should sanitize control chars and cap length."""
    log_file = tmp_path / "malformed.log"
    long_line = "BAD\x00\x1b[31mCTRL" + ("X" * 1000) + "\n"
    log_file.write_text(long_line, encoding="utf-8")

    mock_parser = MagicMock()
    mock_parser.parse.side_effect = ValueError("boom")

    reader = LogFileReader(
        log_file,
        parser=mock_parser,
        parse_error_log_interval=0.0,
    )

    with caplog.at_level(logging.ERROR):
        entries = list(reader)

    assert entries == []

    error_logs = [r for r in caplog.records if "Failed to parse line:" in r.message]
    assert len(error_logs) == 1

    message = error_logs[0].message
    assert "\x00" not in message
    assert "\x1b" not in message
    assert len(message) <= 240


def _write_lines(path: Path, *lines: str) -> Path:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


def test_format_mismatch_warns_once(tmp_path: Path, caplog) -> None:
    """Lines in another format give one WARNING per read, with their count (F6-5).

    logcat's buffer dividers are not counted; the lines are still yielded, as
    unparsed entries.
    """
    log_file = _write_lines(
        tmp_path / "brief.log",
        "--------- beginning of main",
        "D/Tag( 1234): one",
        "D/Tag( 1234): two",
        "11-19 12:34:56.789  1000  2000 D TagA   : threadtime",
        "D/Tag( 1234): three",
    )
    reader = LogFileReader(log_file, year=2026)

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        entries = list(reader)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]

    assert [e.parsed for e in entries] == [False, False, False, True, False]
    assert [e.message for e in entries][:2] == [
        "--------- beginning of main",
        "D/Tag( 1234): one",
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert message.startswith("3 of 5 lines in ")
    assert "'threadtime'" in message
    assert "ThreadTimeLogParser" in message

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        list(reader)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_matching_file_and_dividers_do_not_warn(tmp_path: Path, caplog) -> None:
    """A file in the parser's format, dividers included, logs no warning."""
    log_file = _write_lines(
        tmp_path / "ok.log",
        "--------- beginning of main",
        "11-19 12:34:56.789  1000  2000 D TagA   : a",
        "--------- switch to crash",
        "11-19 12:34:57.000  1000  2000 E TagB   : b",
    )

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        entries = list(read_file(log_file, year=2026))

    assert [e.parsed for e in entries] == [False, True, False, True]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_mismatch_warning_is_logged_when_the_read_stops_early(
    tmp_path: Path, caplog
) -> None:
    """Closing the reader early still reports the lines read so far."""
    log_file = _write_lines(tmp_path / "raw.log", "not a log line", "neither")

    with caplog.at_level(logging.WARNING, logger="logpyt.readers"):
        entries = cast("Generator[LogEntry]", read_file(log_file))
        next(entries)
        entries.close()

    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.getMessage().startswith("1 of 1 lines in ")


def _set_mtime(path: Path, moment: datetime) -> None:
    os.utime(path, (moment.timestamp(), moment.timestamp()))


def test_yearless_lines_are_dated_before_the_file_mtime(tmp_path: Path) -> None:
    """Without year=, the first yearless line is placed before the file's mtime.

    A capture across New Year saved on Jan 2 starts in the old year; a March
    capture copied in November (a later mtime) stays in that year's March,
    although the next March would be nearer to the mtime.
    """
    new_year = _write_lines(
        tmp_path / "new_year.log",
        "12-30 08:00:00.000  1000  2000 D T: a",
        "12-31 23:59:59.000  1000  2000 D T: b",
        "01-01 00:00:01.000  1000  2000 D T: c",
    )
    _set_mtime(new_year, datetime(2027, 1, 2, 12, 0, tzinfo=UTC))
    spring = _write_lines(
        tmp_path / "spring.log",
        "03-15 10:00:00.000  1000  2000 D T: a",
        "03-15 10:00:01.000  1000  2000 D T: b",
    )
    _set_mtime(spring, datetime(2026, 11, 4, 12, 0, tzinfo=UTC))

    assert [e.timestamp.year for e in read_file(new_year)] == [2026, 2026, 2027]
    assert [e.timestamp.year for e in read_file(spring)] == [2026, 2026]


def test_year_argument_dates_the_first_yearless_line(tmp_path: Path) -> None:
    """year= gives the first yearless line its year, whatever the mtime."""
    log_file = _write_lines(
        tmp_path / "y.log",
        "12-31 23:59:59.000  1000  2000 D T: a",
        "01-01 00:00:01.000  1000  2000 D T: b",
    )

    assert [e.timestamp.year for e in read_file(log_file, year=2019)] == [2019, 2020]


@pytest.mark.parametrize("year", [0, 10_000])
def test_year_out_of_range_is_rejected(tmp_path: Path, year: int) -> None:
    """A year outside 1..9999 raises ValueError when the reader is created."""
    with pytest.raises(ValueError, match="year"):
        LogFileReader(tmp_path / "x.log", year=year)


@pytest.mark.parametrize(
    "mtime",
    [
        pytest.param(253_402_300_800.0, id="year-10000"),
        pytest.param(1e20, id="past-time_t"),
        pytest.param(-1e20, id="before-time_t"),
    ],
)
def test_mtime_without_a_datetime_does_not_fail_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mtime: float
) -> None:
    """An mtime outside what datetime holds is no error, with or without year=.

    With year= the mtime is not used at all; without it the first yearless
    line is placed before the host clock instead. The mtime is patched in,
    because a file system may refuse to store such a value.
    """
    log_file = _write_lines(tmp_path / "far.log", "12-31 23:59:59.000  1  1 D T: a")
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mtime=mtime))

    with_year = list(read_file(log_file, year=2024))
    without = list(read_file(log_file))

    assert [e.timestamp.year for e in with_year] == [2024]
    assert len(without) == 1
    assert without[0].parsed
    assert without[0].timestamp <= datetime.now(UTC).astimezone().replace(
        tzinfo=None
    ) + timedelta(days=2)
