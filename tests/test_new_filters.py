from datetime import datetime

from logpyt.filters import AnrCondition, CrashCondition
from logpyt.models import LogEntry


def test_crash_condition():
    condition = CrashCondition()
    now = datetime.now()

    # Match
    entry1 = LogEntry(
        timestamp=now,
        tag="AndroidRuntime",
        level="E",
        message="FATAL EXCEPTION: main",
        pid=123,
        tid=123,
        raw="raw log",
    )
    assert condition.check(entry1) is True

    # No match - wrong tag
    entry2 = LogEntry(
        timestamp=now,
        tag="MyApp",
        level="E",
        message="FATAL EXCEPTION: main",
        pid=123,
        tid=123,
        raw="raw log",
    )
    assert condition.check(entry2) is False

    # No match - wrong level
    entry3 = LogEntry(
        timestamp=now,
        tag="AndroidRuntime",
        level="I",
        message="FATAL EXCEPTION: main",
        pid=123,
        tid=123,
        raw="raw log",
    )
    assert condition.check(entry3) is False

    # No match - wrong message
    entry4 = LogEntry(
        timestamp=now,
        tag="AndroidRuntime",
        level="E",
        message="Some other error",
        pid=123,
        tid=123,
        raw="raw log",
    )
    assert condition.check(entry4) is False


def test_anr_condition():
    condition = AnrCondition()
    now = datetime.now()

    # Match
    entry1 = LogEntry(
        timestamp=now,
        tag="ActivityManager",
        level="E",
        message="ANR in com.example.app",
        pid=123,
        tid=123,
        raw="raw log",
    )
    assert condition.check(entry1) is True

    # No match - wrong tag
    entry2 = LogEntry(
        timestamp=now,
        tag="MyApp",
        level="E",
        message="ANR in com.example.app",
        pid=123,
        tid=123,
        raw="raw log",
    )
    assert condition.check(entry2) is False

    # No match - wrong level
    entry3 = LogEntry(
        timestamp=now,
        tag="ActivityManager",
        level="I",
        message="ANR in com.example.app",
        pid=123,
        tid=123,
        raw="raw log",
    )
    assert condition.check(entry3) is False

    # No match - wrong message start
    entry4 = LogEntry(
        timestamp=now,
        tag="ActivityManager",
        level="E",
        message="Something ANR in com.example.app",
        pid=123,
        tid=123,
        raw="raw log",
    )
    assert condition.check(entry4) is False
