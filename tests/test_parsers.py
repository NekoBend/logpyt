"""Tests for log parsers."""

from datetime import UTC, datetime

from logpyt.parsers import BriefLogParser, LogParser, ThreadTimeLogParser


def test_log_parser_parse_stdout() -> None:
    """Test parsing stdout line."""
    parser = LogParser()
    line = "some log line"
    entry = parser.parse_stdout(line)

    assert entry.message == "some log line"
    assert entry.raw == "some log line"
    assert entry.level == "I"
    assert entry.meta["source"] == "stdout"
    assert isinstance(entry.timestamp, datetime)


def test_log_parser_parse_stderr() -> None:
    """Test parsing stderr line."""
    parser = LogParser()
    line = "error log line"
    entry = parser.parse_stderr(line)

    assert entry.message == "error log line"
    assert entry.raw == "error log line"
    assert entry.level == "E"
    assert entry.meta["source"] == "stderr"
    assert isinstance(entry.timestamp, datetime)


def test_log_parser_parse_common_override() -> None:
    """Test that parse_common can be overridden."""

    class CustomParser(LogParser):
        def parse_common(self, line, source):
            entry = super().parse_common(line, source)
            entry.message = f"[{source}] {line}"
            return entry

    parser = CustomParser()
    entry = parser.parse_stdout("test")
    assert entry.message == "[stdout] test"


def test_thread_time_parser_success() -> None:
    """Test parsing a valid threadtime log line."""
    parser = ThreadTimeLogParser()
    line = "11-19 12:34:56.789  1234  5678 D MyTag   : Hello World"
    entry = parser.parse_stdout(line)

    assert entry.pid == 1234
    assert entry.tid == 5678
    assert entry.level == "D"
    assert entry.tag == "MyTag"
    assert entry.message == "Hello World"
    assert entry.timestamp.month == 11
    assert entry.timestamp.day == 19
    assert entry.timestamp.hour == 12
    assert entry.timestamp.minute == 34
    assert entry.timestamp.second == 56
    assert entry.timestamp.microsecond == 789000
    assert entry.timestamp.year == datetime.now(UTC).astimezone().year
    assert entry.meta["parser"] == "ThreadTimeLogParser"


def test_thread_time_parser_empty_message() -> None:
    """An empty-message threadtime line keeps its structured fields (not raw)."""
    parser = ThreadTimeLogParser()
    entry = parser.parse_stdout("11-19 12:34:56.789  1234  5678 D MyTag   : ")

    assert entry.pid == 1234
    assert entry.tid == 5678
    assert entry.level == "D"
    assert entry.tag == "MyTag"
    assert entry.message == ""
    assert entry.meta["parser"] == "ThreadTimeLogParser"


def test_thread_time_parser_oversized_pid_falls_back() -> None:
    """A pathologically long PID must not crash int(); the line falls back to raw."""
    line = "11-19 12:34:56.789  " + "9" * 5000 + "  5678 D Tag: msg"

    entry = ThreadTimeLogParser().parse_stdout(line)

    # The bounded regex (\d{1,7}) rejects the huge PID -> raw fallback, no ValueError.
    assert entry.pid == 0
    assert entry.tag == ""
    assert entry.message == line
    assert entry.meta.get("parser") is None


def test_thread_time_parser_year_rollover() -> None:
    """The year rolls forward when the month decreases (Dec -> Jan)."""
    parser = ThreadTimeLogParser(default_year=2024)
    dec = parser.parse_stdout("12-31 23:59:59.000  1  1 D T: end of year")
    jan = parser.parse_stdout("01-01 00:00:01.000  1  1 D T: new year")

    assert dec.timestamp.year == 2024
    assert jan.timestamp.year == 2025
    assert jan.timestamp > dec.timestamp


def test_thread_time_parser_invalid_date_falls_back() -> None:
    """An impossible calendar date (month 13) falls back to raw, not a fake 'now'."""
    line = "13-01 12:00:00.000  1  1 D T: m"

    entry = ThreadTimeLogParser().parse_stdout(line)

    assert entry.pid == 0
    assert entry.tag == ""
    assert entry.message == line
    assert entry.meta.get("parser") is None


def test_brief_parser_tag_with_parens() -> None:
    """Brief parser handles a tag that itself contains parentheses."""
    entry = BriefLogParser().parse_stdout("D/Foo(Bar)( 2034): routeCall()")

    assert entry.level == "D"
    assert entry.tag == "Foo(Bar)"
    assert entry.pid == 2034
    assert entry.message == "routeCall()"


def test_thread_time_parser_fallback() -> None:
    """Test fallback when regex doesn't match."""
    parser = ThreadTimeLogParser()
    line = "Invalid format line"
    entry = parser.parse_stdout(line)

    assert entry.message == "Invalid format line"
    assert entry.level == "I"  # Default for stdout
    assert "parser" not in entry.meta


def test_thread_time_parser_invalid_level_falls_back() -> None:
    """Threadtime parser should fallback when level is outside VDIWEF."""
    parser = ThreadTimeLogParser()
    line = "11-19 12:34:56.789  1234  5678 G MyTag   : Hello World"

    entry = parser.parse_stdout(line)

    assert entry.message == line
    assert entry.level == "I"
    assert "parser" not in entry.meta


def test_thread_time_parser_tag_with_spaces() -> None:
    """Test parsing a log line where the tag contains spaces."""
    parser = ThreadTimeLogParser()
    line = "11-19 12:34:56.789  1234  5678 D My Tag With Spaces : Hello World"
    entry = parser.parse_stdout(line)

    assert entry.tag == "My Tag With Spaces"
    assert entry.message == "Hello World"
    assert entry.pid == 1234
    assert entry.tid == 5678
    assert entry.level == "D"


def test_thread_time_parser_message_with_colons() -> None:
    """Test parsing a log line where the message contains colons."""
    parser = ThreadTimeLogParser()
    line = "11-19 12:34:56.789  1234  5678 D MyTag   : Message: with: colons"
    entry = parser.parse_stdout(line)

    assert entry.tag == "MyTag"
    assert entry.message == "Message: with: colons"
    assert entry.pid == 1234
    assert entry.tid == 5678
    assert entry.level == "D"


def test_thread_time_parser_with_wrapped_whitespace() -> None:
    """Threadtime parser should still parse lines after trimming wrappers."""
    parser = ThreadTimeLogParser(default_year=2026)
    line = "  11-19 12:34:56.789  1234  5678 D MyTag   : Hello World   "
    entry = parser.parse_stdout(line)

    assert entry.pid == 1234
    assert entry.tid == 5678
    assert entry.level == "D"
    assert entry.tag == "MyTag"
    assert entry.message == "Hello World"
    assert entry.timestamp.year == 2026


def test_brief_log_parser_success() -> None:
    """Test parsing a valid brief log line."""
    from logpyt.parsers import BriefLogParser

    parser = BriefLogParser()
    line = "D/HeadsetProfile( 2034): routeCall()"
    entry = parser.parse_stdout(line)

    assert entry.level == "D"
    assert entry.tag == "HeadsetProfile"
    assert entry.pid == 2034
    assert entry.message == "routeCall()"
    assert entry.meta["parser"] == "BriefLogParser"
    assert isinstance(entry.timestamp, datetime)


def test_brief_log_parser_fallback() -> None:
    """Test fallback for brief log parser."""
    from logpyt.parsers import BriefLogParser

    parser = BriefLogParser()
    line = "Invalid format"
    entry = parser.parse_stdout(line)

    assert entry.message == "Invalid format"
    assert entry.level == "I"
    assert "parser" not in entry.meta


def test_process_log_parser_success() -> None:
    """Test parsing a valid process log line."""
    from logpyt.parsers import ProcessLogParser

    parser = ProcessLogParser()
    line = "I(  596) System.exit called, status: 0"
    entry = parser.parse_stdout(line)

    assert entry.level == "I"
    assert entry.pid == 596
    assert entry.message == "System.exit called, status: 0"
    assert entry.tag == ""
    assert entry.meta["parser"] == "ProcessLogParser"
    assert isinstance(entry.timestamp, datetime)


def test_process_log_parser_fallback() -> None:
    """Test fallback for process log parser."""
    from logpyt.parsers import ProcessLogParser

    parser = ProcessLogParser()
    line = "Invalid format"
    entry = parser.parse_stdout(line)

    assert entry.message == "Invalid format"
    assert entry.level == "I"
    assert "parser" not in entry.meta


def test_tag_log_parser_success() -> None:
    """Test parsing a valid tag log line."""
    from logpyt.parsers import TagLogParser

    parser = TagLogParser()
    line = "D/HeadsetProfile: routeCall()"
    entry = parser.parse_stdout(line)

    assert entry.level == "D"
    assert entry.tag == "HeadsetProfile"
    assert entry.message == "routeCall()"
    assert entry.pid == 0
    assert entry.meta["parser"] == "TagLogParser"
    assert isinstance(entry.timestamp, datetime)


def test_tag_log_parser_fallback() -> None:
    """Test fallback for tag log parser."""
    from logpyt.parsers import TagLogParser

    parser = TagLogParser()
    line = "Invalid format"
    entry = parser.parse_stdout(line)

    assert entry.message == "Invalid format"
    assert entry.level == "I"
    assert "parser" not in entry.meta


def test_raw_log_parser_success() -> None:
    """Test parsing a raw log line."""
    from logpyt.parsers import RawLogParser

    parser = RawLogParser()
    line = "routeCall()"
    entry = parser.parse_stdout(line)

    assert entry.message == "routeCall()"
    assert entry.level == "I"
    assert entry.pid == 0
    assert entry.tag == ""
    assert entry.meta["parser"] == "RawLogParser"
    assert isinstance(entry.timestamp, datetime)


def test_brief_log_parser_non_brief_prefix_fallback() -> None:
    """Brief parser should keep fallback semantics for non-brief lines."""
    from logpyt.parsers import BriefLogParser

    parser = BriefLogParser()
    line = "X/not-brief( 2034): routeCall()"
    entry = parser.parse_stdout(line)

    assert entry.message == line
    assert entry.level == "I"
    assert "parser" not in entry.meta


def test_thread_time_parser_missing_colon_separator_fallback() -> None:
    """Fallback for date-like lines without a tag/message separator."""
    parser = ThreadTimeLogParser()
    line = "11-19 12:34:56.789  1234  5678 D MyTag Hello World"
    entry = parser.parse_stdout(line)

    assert entry.message == line
    assert entry.level == "I"
    assert "parser" not in entry.meta


def test_brief_log_parser_missing_open_paren_fallback() -> None:
    """Brief parser should fallback when mandatory opening parenthesis is missing."""
    from logpyt.parsers import BriefLogParser

    parser = BriefLogParser()
    line = "D/HeadsetProfile 2034): routeCall()"
    entry = parser.parse_stdout(line)

    assert entry.message == line
    assert entry.level == "I"
    assert "parser" not in entry.meta


def test_process_log_parser_missing_close_paren_fallback() -> None:
    """Process parser should fallback when closing parenthesis is missing."""
    from logpyt.parsers import ProcessLogParser

    parser = ProcessLogParser()
    line = "I(  596 System.exit called, status: 0"
    entry = parser.parse_stdout(line)

    assert entry.message == line
    assert entry.level == "I"
    assert "parser" not in entry.meta
