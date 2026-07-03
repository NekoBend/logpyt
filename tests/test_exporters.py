"""Tests for the exporters module."""

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from logpyt.exporters import CsvLogExporter, JsonLogExporter, export_logs
from logpyt.models.entry import LogEntry


@pytest.fixture
def sample_entries() -> list[LogEntry]:
    """Create a list of sample LogEntry objects."""
    return [
        LogEntry(
            timestamp=datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None),
            pid=1001,
            tid=2001,
            level="D",
            tag="TestTag",
            message="Debug message",
            raw="raw log line 1",
            meta={"source": "main"},
        ),
        LogEntry(
            timestamp=datetime(2023, 1, 1, 12, 0, 1, tzinfo=UTC).replace(tzinfo=None),
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


def test_json_exporter_ensure_ascii(tmp_path: Path) -> None:
    """Test JSON exporter honors ensure_ascii setting."""
    output_file = tmp_path / "logs_ascii.json"
    entries = [
        LogEntry(
            timestamp=datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None),
            pid=1001,
            tid=2001,
            level="I",
            tag="Unicode",
            message="テスト",
            raw="raw log line",
            meta={},
        )
    ]

    exporter = JsonLogExporter(indent=2, ensure_ascii=True)
    exporter.export(entries, output_file)

    content = output_file.read_text(encoding="utf-8")
    assert "\\u30c6" in content


def test_json_exporter_compact_without_indent(
    tmp_path: Path, sample_entries: list[LogEntry]
) -> None:
    """Test JSON exporter writes compact JSON when indent is None."""
    output_file = tmp_path / "logs_compact.json"
    exporter = JsonLogExporter(indent=None)
    exporter.export(sample_entries, output_file)

    content = output_file.read_text(encoding="utf-8")
    data = json.loads(content)

    assert isinstance(data, list)
    assert len(data) == 2
    assert "\n" not in content
    assert '": ' not in content


def test_json_exporter_default_is_compact_for_cost_control(
    tmp_path: Path, sample_entries: list[LogEntry]
) -> None:
    """Default JSON export should be compact to minimize output size."""
    output_file = tmp_path / "logs_default.json"

    exporter = JsonLogExporter()
    exporter.export(sample_entries, output_file)

    content = output_file.read_text(encoding="utf-8")
    data = json.loads(content)

    assert isinstance(data, list)
    assert len(data) == 2
    assert "\n" not in content
    assert '": ' not in content


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


def test_csv_exporter_sanitizes_formula_injection(tmp_path: Path) -> None:
    """CSV export prefixes a quote to values that start with a formula char."""
    output_file = tmp_path / "logs_injection.csv"
    entries = [
        LogEntry(
            timestamp=datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None),
            pid=1001,
            tid=2001,
            level="W",
            tag="@evil",
            message="=cmd|calc!A1",
            raw="raw log line",
            meta={"k": "=danger"},
        )
    ]

    exporter = CsvLogExporter()
    exporter.export(entries, output_file)

    with output_file.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 1
    row = rows[0]
    assert row["message"] == "'=cmd|calc!A1"
    assert row["tag"] == "'@evil"
    # The serialized meta JSON string also starts with a risky char ('{' is
    # safe, but the round-trip remains valid JSON after any prefixing).
    assert json.loads(row["meta"]) == {"k": "=danger"}


def test_csv_exporter_sanitizes_whitespace_led_formula(tmp_path: Path) -> None:
    """A formula char behind leading whitespace is still neutralized.

    Spreadsheets trim leading whitespace before evaluating a cell, so " =cmd"
    and "\t=cmd" are as dangerous as "=cmd" and must also be quoted.
    """
    output_file = tmp_path / "logs_ws_injection.csv"
    entries = [
        LogEntry(
            timestamp=datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None),
            pid=1001,
            tid=2001,
            level="W",
            tag="\t@evil",
            message=" =cmd|calc!A1",
            raw="raw log line",
            meta={"source": "stdout"},
        )
    ]

    exporter = CsvLogExporter()
    exporter.export(entries, output_file)

    with output_file.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 1
    row = rows[0]
    assert row["message"] == "' =cmd|calc!A1"
    assert row["tag"] == "'\t@evil"


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
