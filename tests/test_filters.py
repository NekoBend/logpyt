"""Tests for log filters."""

from datetime import datetime

import pytest

from logpyt.filters import (
    AdvancedFilter,
    AnrCondition,
    CrashCondition,
    Filter,
    Level,
    MessageContains,
    Package,
    Tag,
)
from logpyt.models import LogEntry


@pytest.fixture
def sample_entry() -> LogEntry:
    """Provide a sample log entry."""
    return LogEntry(
        timestamp=datetime.now(),
        pid=123,
        tid=456,
        level="D",
        tag="MyApp",
        message="Something happened",
        raw="raw",
        meta={"package": "com.example.app"},
    )


def test_filter_simple_match(sample_entry: LogEntry) -> None:
    """Test simple Filter matching."""
    f = Filter(tag="MyApp", level="D")
    assert f(sample_entry) is True


def test_filter_simple_mismatch(sample_entry: LogEntry) -> None:
    """Test simple Filter mismatch."""
    f = Filter(tag="OtherApp")
    assert f(sample_entry) is False


def test_filter_multiple_conditions(sample_entry: LogEntry) -> None:
    """Test Filter with multiple conditions (AND logic)."""
    f = Filter(tag="MyApp", level="D", message_contains="Something")
    assert f(sample_entry) is True

    f_fail = Filter(tag="MyApp", level="E")  # Level mismatch
    assert f_fail(sample_entry) is False


def test_filter_list_values(sample_entry: LogEntry) -> None:
    """Test Filter with list of values (OR logic within field)."""
    f = Filter(tag=["MyApp", "OtherApp"])
    assert f(sample_entry) is True


def test_advanced_filter_operators(sample_entry: LogEntry) -> None:
    """Test AdvancedFilter with logical operators."""
    c1 = Tag("MyApp")
    c2 = Level("E")
    c3 = MessageContains("happened")

    # (Tag=MyApp AND MessageContains=happened) OR Level=E
    # Should match because first part matches
    f = AdvancedFilter((c1 & c3) | c2)
    assert f(sample_entry) is True

    # (Tag=MyApp AND Level=E)
    # Should fail
    f2 = AdvancedFilter(c1 & c2)
    assert f2(sample_entry) is False

    # NOT Tag=MyApp
    # Should fail
    f3 = AdvancedFilter(~c1)
    assert f3(sample_entry) is False


def test_package_condition(sample_entry: LogEntry) -> None:
    """Test Package condition."""
    c = Package("com.example.app")
    assert c.check(sample_entry) is True

    c2 = Package("com.other.app")
    assert c2.check(sample_entry) is False


def test_tag_condition(sample_entry: LogEntry) -> None:
    """Test Tag condition."""
    c = Tag("MyApp")
    assert c.check(sample_entry) is True


def test_level_condition(sample_entry: LogEntry) -> None:
    """Test Level condition."""
    c = Level("D")
    assert c.check(sample_entry) is True


def test_message_contains_condition(sample_entry: LogEntry) -> None:
    """Test MessageContains condition."""
    c = MessageContains(["Something", "Nothing"])
    assert c.check(sample_entry) is True  # "Something" is in message

    c2 = MessageContains("Error")
    assert c2.check(sample_entry) is False


def test_crash_condition(sample_entry: LogEntry) -> None:
    """Test CrashCondition."""
    # Match
    crash_entry = sample_entry.model_copy(
        update={
            "tag": "AndroidRuntime",
            "level": "E",
            "message": "FATAL EXCEPTION: main",
        }
    )
    assert CrashCondition().check(crash_entry) is True

    # Mismatch - wrong tag
    no_crash = sample_entry.model_copy(
        update={
            "tag": "MyApp",
            "level": "E",
            "message": "FATAL EXCEPTION: main",
        }
    )
    assert CrashCondition().check(no_crash) is False

    # Mismatch - wrong level
    no_crash_level = sample_entry.model_copy(
        update={
            "tag": "AndroidRuntime",
            "level": "D",
            "message": "FATAL EXCEPTION: main",
        }
    )
    assert CrashCondition().check(no_crash_level) is False

    # Mismatch - wrong message
    no_crash_msg = sample_entry.model_copy(
        update={
            "tag": "AndroidRuntime",
            "level": "E",
            "message": "Just an error",
        }
    )
    assert CrashCondition().check(no_crash_msg) is False


def test_anr_condition(sample_entry: LogEntry) -> None:
    """Test AnrCondition."""
    # Match
    anr_entry = sample_entry.model_copy(
        update={
            "tag": "ActivityManager",
            "level": "E",
            "message": "ANR in com.example.app",
        }
    )
    assert AnrCondition().check(anr_entry) is True

    # Mismatch - wrong tag
    no_anr = sample_entry.model_copy(
        update={
            "tag": "MyApp",
            "level": "E",
            "message": "ANR in com.example.app",
        }
    )
    assert AnrCondition().check(no_anr) is False

    # Mismatch - wrong level
    no_anr_level = sample_entry.model_copy(
        update={
            "tag": "ActivityManager",
            "level": "I",
            "message": "ANR in com.example.app",
        }
    )
    assert AnrCondition().check(no_anr_level) is False

    # Mismatch - wrong message (must start with "ANR in")
    no_anr_msg = sample_entry.model_copy(
        update={
            "tag": "ActivityManager",
            "level": "E",
            "message": "Something about ANR",
        }
    )
    assert AnrCondition().check(no_anr_msg) is False
