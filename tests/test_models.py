"""Tests for logpyt models."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from logpyt.models import LogEntry


def test_log_entry_initialization() -> None:
    """Test LogEntry initialization."""
    now = datetime.now(UTC).replace(tzinfo=None)
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
    now = datetime.now(UTC).replace(tzinfo=None)
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
    now = datetime.now(UTC).replace(tzinfo=None)
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
    now = datetime.now(UTC).replace(tzinfo=None)
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


def test_log_entry_to_json_ensure_ascii() -> None:
    """Test LogEntry.to_json() honors the ensure_ascii parameter."""
    now = datetime.now(UTC).replace(tzinfo=None)
    entry = LogEntry(
        timestamp=now,
        pid=123,
        tid=456,
        level="I",
        tag="Tag",
        message="日本語",
        raw="raw log line",
    )

    # ensure_ascii=True escapes non-ASCII characters.
    escaped = entry.to_json(ensure_ascii=True)
    assert "日" not in escaped
    assert "\\u65e5" in escaped
    assert json.loads(escaped)["message"] == "日本語"

    # ensure_ascii=False keeps non-ASCII characters literal.
    literal = entry.to_json(ensure_ascii=False)
    assert "日" in literal
    assert json.loads(literal)["message"] == "日本語"


def test_log_entry_json_payload() -> None:
    """Test LogEntry.json_payload() (a method since 3.0)."""
    now = datetime.now(UTC).replace(tzinfo=None)

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
    assert entry1.json_payload() == {"foo": "bar"}

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
    assert entry2.json_payload() is None


def _entry(**overrides: object) -> LogEntry:
    fields: dict[str, object] = {
        "timestamp": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "pid": 1,
        "tid": 2,
        "level": "I",
        "tag": "Tag",
        "message": "m",
        "raw": "r",
    }
    fields.update(overrides)
    return LogEntry.model_validate(fields)


def test_json_payload_after_model_copy() -> None:
    """json_payload() reads the message of the copy, not a cached value (F8-4)."""
    entry = _entry(message='{"old": 1}')
    assert entry.json_payload() == {"old": 1}

    copy = entry.model_copy(update={"message": '{"new": 2}'})

    assert copy.json_payload() == {"new": 2}
    assert entry.json_payload() == {"old": 1}


def test_log_entry_new_fields_default_to_a_plain_parsed_line() -> None:
    """uid, uid_raw and package are None; parsed is True, truncated False."""
    entry = _entry()

    assert (entry.uid, entry.uid_raw, entry.package) == (None, None, None)
    assert entry.parsed is True
    assert entry.truncated is False
    assert entry.meta == {}
    assert list(entry.to_dict()) == [
        "timestamp",
        "pid",
        "tid",
        "uid",
        "uid_raw",
        "level",
        "tag",
        "message",
        "raw",
        "package",
        "parsed",
        "truncated",
        "meta",
    ]


def test_log_entry_is_frozen() -> None:
    """Fields cannot be assigned; model_copy(update=) derives a changed entry."""
    entry = _entry()

    with pytest.raises(ValidationError, match="frozen"):
        entry.message = "changed"
    assert entry.model_copy(update={"package": "com.example"}).package == "com.example"
    assert entry.package is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"pid": "1"}, id="pid-str"),
        pytest.param({"pid": True}, id="pid-bool"),
        pytest.param({"tid": 2.0}, id="tid-float"),
        pytest.param({"uid": "10065"}, id="uid-str"),
        pytest.param({"timestamp": "2026-01-02T03:04:05"}, id="timestamp-str"),
        pytest.param({"level": "X"}, id="level-unknown"),
        pytest.param({"level": "error"}, id="level-name"),
        pytest.param({"parsed": 1}, id="parsed-int"),
        pytest.param({"meta": [("k", "v")]}, id="meta-pairs"),
    ],
)
def test_log_entry_is_strict(overrides: dict[str, Any]) -> None:
    """A value of the wrong type is rejected, not coerced (D-13)."""
    with pytest.raises(ValidationError):
        _entry(**overrides)


def test_log_entry_is_not_hashable() -> None:
    """hash() raises TypeError, as in 2.x: frozen, but meta is a dict."""
    entry = _entry()

    with pytest.raises(TypeError, match="unhashable"):
        hash(entry)
    assert LogEntry.__hash__ is None


def test_model_copy_does_not_validate() -> None:
    """pydantic's model_copy(update=) keeps a value the constructor rejects.

    The LogEntry docs say so: model_* methods are not part of the entry's API
    (design 2.6) and the strict validation is the constructor's. Should the
    copy ever validate, this test and that note change together.
    """
    entry = _entry()

    copied = entry.model_copy(update={"pid": "x"})

    assert copied.pid == "x"
    with pytest.raises(ValidationError):
        _entry(pid="x")


def test_to_json_writes_non_json_meta_as_repr() -> None:
    """A meta value JSON cannot hold is written as its repr()."""

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    entry = _entry(meta={"obj": Opaque(), "n": 1})

    assert json.loads(entry.to_json())["meta"] == {"obj": "<opaque>", "n": 1}


def test_to_json_options_are_keyword_only() -> None:
    """to_json takes indent and ensure_ascii as keywords only."""
    with pytest.raises(TypeError):
        _entry().to_json(True)  # noqa: FBT003  # ty: ignore[too-many-positional-arguments]
