"""Module for exporting log entries to various formats."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Protocol

from logpyt.models.entry import LogEntry


class LogExporter(Protocol):
    """Interface for log exporters."""

    def export(self, entries: Iterable[LogEntry], destination: str | Path) -> None:
        """Export log entries to a destination.

        Args:
            entries: An iterable of LogEntry objects to export.
            destination: The file path to write the exported logs to.
        """
        ...


class JsonLogExporter:
    """Exports log entries to a JSON file."""

    def __init__(self, indent: int | None = 4, ensure_ascii: bool = False) -> None:
        """Initialize the JSON exporter.

        Args:
            indent: Number of spaces for indentation. Defaults to 4.
            ensure_ascii: If True, non-ASCII characters are escaped. Defaults to False.
        """
        self.indent = indent
        self.ensure_ascii = ensure_ascii

    def export(self, entries: Iterable[LogEntry], destination: str | Path) -> None:
        """Export log entries to a JSON file.

        Args:
            entries: An iterable of LogEntry objects to export.
            destination: The file path to write the JSON output to.
        """
        dest_path = Path(destination)

        with dest_path.open("w", encoding="utf-8") as f:
            # Stream the output as a JSON array to avoid loading all logs into memory
            f.write("[\n")
            first = True
            for entry in entries:
                if not first:
                    f.write(",\n")

                # Use model_dump_json for efficient serialization
                f.write(entry.model_dump_json(indent=self.indent))
                first = False
            f.write("\n]")


class CsvLogExporter:
    """Exports log entries to a CSV file."""

    def __init__(self, delimiter: str = ",", quotechar: str = '"') -> None:
        """Initialize the CSV exporter.

        Args:
            delimiter: A one-character string used to separate fields. Defaults to ",".
            quotechar: A one-character string used to quote fields containing special characters. Defaults to '"'.
        """
        self.delimiter = delimiter
        self.quotechar = quotechar
        # Dynamically determine field names from LogEntry model
        self.fieldnames = list(LogEntry.model_fields.keys())

    def export(self, entries: Iterable[LogEntry], destination: str | Path) -> None:
        """Export log entries to a CSV file.

        Args:
            entries: An iterable of LogEntry objects to export.
            destination: The file path to write the CSV output to.
        """
        dest_path = Path(destination)

        with dest_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=self.fieldnames,
                delimiter=self.delimiter,
                quotechar=self.quotechar,
            )
            writer.writeheader()

            for entry in entries:
                row = entry.model_dump(mode="json")
                # Serialize meta dictionary to JSON string for CSV compatibility
                if "meta" in row and isinstance(row["meta"], dict):
                    row["meta"] = json.dumps(row["meta"], ensure_ascii=False)

                # Ensure we only write fields defined in fieldnames
                # (though LogEntry shouldn't have extra fields usually)
                filtered_row = {k: row.get(k) for k in self.fieldnames}
                writer.writerow(filtered_row)


def export_logs(
    entries: Iterable[LogEntry],
    destination: str | Path,
    format: Literal["json", "csv"] = "json",
) -> None:
    """Export logs to a file in the specified format.

    Args:
        entries: An iterable of LogEntry objects to export.
        destination: The file path to write the exported logs to.
        format: The format to export to ("json" or "csv"). Defaults to "json".

    Raises:
        ValueError: If an unsupported format is specified.
    """
    exporter: LogExporter

    if format == "json":
        exporter = JsonLogExporter()
    elif format == "csv":
        exporter = CsvLogExporter()
    else:
        raise ValueError(f"Unsupported export format: {format}")

    exporter.export(entries, destination)
