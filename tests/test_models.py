"""Tests for logpyt models."""

import json
from datetime import datetime

from logpyt.models import LogEntry


def test_log_entry_initialization() -> None:
    """Test LogEntry initialization."""
    now = datetime.now()
    entry = LogEntry(
        timestamp=now,
        pid=123,
        tid=456,
        level="D",
        tag="TestTag",
        message="Test message",
        raw="raw log line",
        meta={"key": "value"},
    )

    assert entry.timestamp == now
    assert entry.pid == 123
    assert entry.tid == 456
    assert entry.level == "D"
    assert entry.tag == "TestTag"
    assert entry.message == "Test message"
    assert entry.raw == "raw log line"
    assert entry.meta == {"key": "value"}


def test_log_entry_to_dict() -> None:
    """Test LogEntry.to_dict()."""
    now = datetime.now()
    entry = LogEntry(
        timestamp=now,
        pid=123,
        tid=456,
        level="I",
        tag="TestTag",
        message="Test message",
        raw="raw log line",
    )

    data = entry.to_dict()
    assert data["timestamp"] == now
    assert data["pid"] == 123
    assert data["tid"] == 456
    assert data["level"] == "I"
    assert data["tag"] == "TestTag"
    assert data["message"] == "Test message"
    assert data["raw"] == "raw log line"
    assert data["meta"] == {}


def test_log_entry_to_json() -> None:
    """Test LogEntry.to_json()."""
    now = datetime.now()
    entry = LogEntry(
        timestamp=now,
        pid=123,
        tid=456,
        level="E",
        tag="TestTag",
        message="Test message",
        raw="raw log line",
        meta={"foo": "bar"},
    )

    json_str = entry.to_json()
    data = json.loads(json_str)

    assert data["timestamp"] == now.isoformat()
    assert data["pid"] == 123
    assert data["level"] == "E"
    assert data["meta"] == {"foo": "bar"}


def test_log_entry_to_json_formatting() -> None:
    """Test LogEntry.to_json() with formatting options."""
    now = datetime.now()
    entry = LogEntry(
        timestamp=now,
        pid=123,
        tid=456,
        level="E",
        tag="TestTag",
        message="Test message",
        raw="raw log line",
    )

    json_str = entry.to_json(indent=2)
    assert "\n" in json_str
    assert '  "pid": 123' in json_str


def test_log_entry_json_payload() -> None:
    """Test LogEntry.json_payload property."""
    now = datetime.now()

    # Case 1: Valid JSON in message
    entry1 = LogEntry(
        timestamp=now,
        pid=123,
        tid=456,
        level="I",
        tag="Tag",
        message='Some prefix {"foo": "bar"} suffix',
        raw="raw",
    )
    assert entry1.json_payload == {"foo": "bar"}

    # Case 2: No JSON in message
    entry2 = LogEntry(
        timestamp=now,
        pid=123,
        tid=456,
        level="I",
        tag="Tag",
        message="Just text",
        raw="raw",
    )
    assert entry2.json_payload is None
