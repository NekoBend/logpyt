"""Tests for LogFileReader and read_file."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from logpyt.filters import Filter
from logpyt.models import LogEntry
from logpyt.parsers.logcat import ThreadTimeLogParser
from logpyt.readers import LogFileReader, read_file


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
    # We expect 3 entries because ThreadTimeLogParser falls back to basic parsing
    # for lines that don't match the regex.
    assert len(entries) == 3

    # Verify first entry (Valid ThreadTime)
    assert entries[0].tag == "TagA"
    assert entries[0].message == "Message A"
    assert entries[0].level == "D"

    # Verify second entry (Fallback)
    assert entries[1].message == "Invalid Line That Does Not Match Regex"
    assert entries[1].level == "I"  # Default for stdout
    assert entries[1].tag == ""

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

    def side_effect(line: str) -> LogEntry:
        if "Line 2" in line:
            raise ValueError("Parsing failed")
        # Return a dummy entry for other lines
        return LogEntry(
            timestamp=datetime.now(),
            pid=0,
            tid=0,
            level="I",
            tag="Test",
            message=line.strip(),
            raw=line,
        )

    mock_parser.parse_stdout.side_effect = side_effect

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
