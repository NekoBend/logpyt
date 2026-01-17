"""Tests for the exporters module."""

import csv
import json
from datetime import datetime
from pathlib import Path

import pytest

from logpyt.exporters import CsvLogExporter, JsonLogExporter, export_logs
from logpyt.models.entry import LogEntry


@pytest.fixture
def sample_entries() -> list[LogEntry]:
    """Create a list of sample LogEntry objects."""
    return [
        LogEntry(
            timestamp=datetime(2023, 1, 1, 12, 0, 0),
            pid=1001,
            tid=2001,
            level="D",
            tag="TestTag",
            message="Debug message",
            raw="raw log line 1",
            meta={"source": "main"},
        ),
        LogEntry(
            timestamp=datetime(2023, 1, 1, 12, 0, 1),
            pid=1002,
            tid=2002,
            level="E",
            tag="ErrorTag",
            message="Error message",
            raw="raw log line 2",
            meta={"error_code": 123},
        ),
    ]


def test_json_exporter(tmp_path: Path, sample_entries: list[LogEntry]) -> None:
    """Test exporting logs to JSON."""
    output_file = tmp_path / "logs.json"
    exporter = JsonLogExporter(indent=2)
    exporter.export(sample_entries, output_file)

    assert output_file.exists()

    with output_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    assert isinstance(data, list)
    assert len(data) == 2

    entry1 = data[0]
    assert entry1["pid"] == 1001
    assert entry1["tag"] == "TestTag"
    assert entry1["level"] == "D"
    assert entry1["meta"] == {"source": "main"}
    # Check timestamp serialization (ISO format)
    assert entry1["timestamp"] == "2023-01-01T12:00:00"


def test_csv_exporter(tmp_path: Path, sample_entries: list[LogEntry]) -> None:
    """Test exporting logs to CSV."""
    output_file = tmp_path / "logs.csv"
    exporter = CsvLogExporter()
    exporter.export(sample_entries, output_file)

    assert output_file.exists()

    with output_file.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 2

    row1 = rows[0]
    assert row1["pid"] == "1001"
    assert row1["tag"] == "TestTag"
    assert row1["level"] == "D"
    # Check meta serialization (JSON string)
    assert json.loads(row1["meta"]) == {"source": "main"}
    assert row1["timestamp"] == "2023-01-01T12:00:00"


def test_export_logs_json(tmp_path: Path, sample_entries: list[LogEntry]) -> None:
    """Test the export_logs helper function with JSON format."""
    output_file = tmp_path / "helper_logs.json"
    export_logs(sample_entries, output_file, format="json")

    assert output_file.exists()
    with output_file.open("r") as f:
        data = json.load(f)
    assert len(data) == 2


def test_export_logs_csv(tmp_path: Path, sample_entries: list[LogEntry]) -> None:
    """Test the export_logs helper function with CSV format."""
    output_file = tmp_path / "helper_logs.csv"
    export_logs(sample_entries, output_file, format="csv")

    assert output_file.exists()
    with output_file.open("r") as f:
        reader = csv.reader(f)
        # Header + 2 rows
        assert len(list(reader)) == 3


def test_export_logs_invalid_format(
    tmp_path: Path, sample_entries: list[LogEntry]
) -> None:
    """Test that export_logs raises ValueError for invalid format."""
    output_file = tmp_path / "invalid.txt"
    with pytest.raises(ValueError, match="Unsupported export format"):
        export_logs(sample_entries, output_file, format="xml")  # type: ignore
