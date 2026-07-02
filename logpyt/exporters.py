"""Module for exporting log entries to various formats."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from logpyt.models.entry import LogEntry

if TYPE_CHECKING:
    from collections.abc import Iterable

# Characters that trigger formula evaluation in Excel/Sheets when they lead a
# cell value. Log content is attacker-controllable, so such values are
# neutralized by prefixing a single quote before writing the CSV row.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _sanitize_csv_value(value: str) -> str:
    """Neutralize CSV formula injection by prefixing risky leading characters.

    Args:
        value: The string cell value to sanitize.

    Returns:
        The value unchanged, or prefixed with a single quote if it starts with
        a character that spreadsheet applications interpret as a formula.
    """
    if value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


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

    def __init__(
        self,
        indent: int | None = None,
        ensure_ascii: bool = False,  # noqa: FBT001, FBT002  (existing public signature)
    ) -> None:
        """Initialize the JSON exporter.

        Args:
            indent: Number of spaces for indentation. Defaults to None (compact).
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
            compact_mode = self.indent is None
            separators: tuple[str, str] | None = (",", ":") if compact_mode else None
            write = f.write

            write("[" if compact_mode else "[\n")
            first = True
            for entry in entries:
                if not first:
                    write("," if compact_mode else ",\n")

                # Use json.dumps to honor ensure_ascii
                write(
                    json.dumps(
                        entry.model_dump(mode="json"),
                        ensure_ascii=self.ensure_ascii,
                        indent=self.indent,
                        separators=separators,
                    )
                )
                first = False
            write("]" if compact_mode else "\n]")


class CsvLogExporter:
    """Exports log entries to a CSV file."""

    def __init__(self, delimiter: str = ",", quotechar: str = '"') -> None:
        """Initialize the CSV exporter.

        Args:
            delimiter: A one-character string used to separate fields. Defaults to ",".
            quotechar: A one-character string used to quote fields containing
                special characters. Defaults to '"'.
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
            dumps = json.dumps

            for entry in entries:
                row = entry.model_dump(mode="json")
                # Serialize meta dictionary to JSON string for CSV compatibility
                if "meta" in row and isinstance(row["meta"], dict):
                    row["meta"] = dumps(
                        row["meta"], ensure_ascii=False, separators=(",", ":")
                    )

                # Neutralize CSV formula injection in all string cell values
                # (message, tag, the serialized meta, etc.).
                for key, cell in row.items():
                    if isinstance(cell, str):
                        row[key] = _sanitize_csv_value(cell)

                writer.writerow(row)


def export_logs(
    entries: Iterable[LogEntry],
    destination: str | Path,
    format: Literal["json", "csv"] = "json",  # noqa: A002  (public param)
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
