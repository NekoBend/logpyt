"""Tests for utility functions."""

import json
import logging

import pytest

import logpyt.utils
from logpyt.utils import enable_debug, extract_json


def test_enable_debug_configures_logger_without_duplicate_handlers() -> None:
    """enable_debug sets the level and attaches exactly one handler (idempotent)."""
    logger = logging.getLogger("logpyt")
    # Snapshot and clear any pre-existing state so this test is deterministic.
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    logger.handlers.clear()

    try:
        enable_debug("DEBUG")
        assert logger.level == logging.DEBUG
        assert len(logger.handlers) == 1

        # Calling again must NOT add a duplicate handler (enable_debug finds its
        # own handler by name) and must still apply the requested level.
        enable_debug("INFO")
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
    finally:
        # Restore original logger state so this does not leak into other tests.
        logger.handlers.clear()
        logger.handlers.extend(saved_handlers)
        logger.setLevel(saved_level)


@pytest.fixture
def logpyt_logger():
    """The ``logpyt`` logger, with its handlers and level restored afterwards."""
    logger = logging.getLogger("logpyt")
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    try:
        yield logger
    finally:
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)


def _debug_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if h.get_name() == "logpyt.enable_debug"]


def test_enable_debug_lowercase(logpyt_logger) -> None:
    """Level names are case-insensitive, and the default is DEBUG (F9-5)."""
    enable_debug("debug")
    assert logpyt_logger.level == logging.DEBUG
    enable_debug("Warning")
    assert logpyt_logger.level == logging.WARNING
    enable_debug(" error ")
    assert logpyt_logger.level == logging.ERROR
    enable_debug()
    assert logpyt_logger.level == logging.DEBUG
    enable_debug(logging.INFO)
    assert logpyt_logger.level == logging.INFO
    assert len(_debug_handlers(logpyt_logger)) == 1


def test_enable_debug_adds_its_handler_beside_others_once(logpyt_logger) -> None:
    """The NullHandler installed at import, or an application's handler, does not
    stop enable_debug from adding its own stderr handler, and only once.
    """
    assert any(isinstance(h, logging.NullHandler) for h in logpyt_logger.handlers)
    application = logging.StreamHandler()
    logpyt_logger.addHandler(application)

    enable_debug()
    enable_debug("info")

    debug = _debug_handlers(logpyt_logger)
    assert len(debug) == 1
    assert isinstance(debug[0], logging.StreamHandler)
    assert application in logpyt_logger.handlers


def test_enable_debug_records_reach_its_handler(logpyt_logger, capsys) -> None:
    """A DEBUG record of a logpyt module is printed on stderr after enable_debug()."""
    # A fresh handler binds the stderr that capsys installed for this test.
    logpyt_logger.handlers[:] = [
        h for h in logpyt_logger.handlers if h not in _debug_handlers(logpyt_logger)
    ]
    enable_debug()
    logging.getLogger("logpyt.adb").debug("probe %d", 42)
    assert "logpyt.adb - DEBUG - probe 42" in capsys.readouterr().err


def test_enable_debug_finds_a_custom_level_in_any_case(
    logpyt_logger, monkeypatch
) -> None:
    """A level registered in mixed case is found by every spelling (2.x took the
    registered one, through ``Logger.setLevel``).
    """
    # addLevelName writes these registries; the copies are dropped afterwards.
    monkeypatch.setattr(logging, "_nameToLevel", dict(logging._nameToLevel))
    monkeypatch.setattr(logging, "_levelToName", dict(logging._levelToName))
    logging.addLevelName(5, "Trace")
    logging.addLevelName(7, "VERBOSE")

    for name in ("Trace", "trace", "TRACE", " tRaCe "):
        enable_debug(name)
        assert logpyt_logger.level == 5, name
    enable_debug("verbose")
    assert logpyt_logger.level == 7


@pytest.mark.parametrize("level", ["verbose", "", "DEBUG2", -1])
def test_enable_debug_rejects_an_unknown_level(logpyt_logger, level) -> None:
    saved = logpyt_logger.level
    with pytest.raises(ValueError, match="level"):
        enable_debug(level)
    assert logpyt_logger.level == saved


@pytest.mark.parametrize("level", [True, 10.0, None])
def test_enable_debug_rejects_a_level_of_another_type(logpyt_logger, level) -> None:
    with pytest.raises(TypeError, match="level"):
        enable_debug(level)


def test_utils_exports_only_its_own_helpers() -> None:
    """The adb helpers moved to logpyt.adb (design 2.10)."""
    assert sorted(logpyt.utils.__all__) == ["enable_debug", "extract_json"]
    for name in ("resolve_adb", "list_devices", "wait_for_device", "adb_connect"):
        assert not hasattr(logpyt.utils, name)


def test_extract_json_simple_object() -> None:
    """Test extracting a simple JSON object."""
    text = '{"key": "value"}'
    assert extract_json(text) == {"key": "value"}


def test_extract_json_simple_array() -> None:
    """Test extracting a simple JSON array."""
    text = "[1, 2, 3]"
    assert extract_json(text) == [1, 2, 3]


def test_extract_json_embedded() -> None:
    """Test extracting JSON embedded in text."""
    text = 'Prefix: {"key": "value"} Suffix'
    assert extract_json(text) == {"key": "value"}


def test_extract_json_embedded_array() -> None:
    """Test extracting JSON array embedded in text."""
    text = "Prefix: [1, 2, 3] Suffix"
    assert extract_json(text) == [1, 2, 3]


def test_extract_json_nested() -> None:
    """Test extracting nested JSON."""
    text = '{"a": {"b": [1, 2]}}'
    assert extract_json(text) == {"a": {"b": [1, 2]}}


def test_extract_json_multiple_candidates() -> None:
    """Test extracting when multiple candidates exist (picks first valid)."""
    # First one is valid
    text = '{"a": 1} and {"b": 2}'
    assert extract_json(text) == {"a": 1}


def test_extract_json_first_invalid_second_valid() -> None:
    """Test extracting when first candidate is invalid."""
    # {invalid} is not valid JSON, should skip and find {"valid": 1}
    text = 'Start {invalid} Middle {"valid": 1} End'
    assert extract_json(text) == {"valid": 1}


def test_extract_json_brackets_in_text() -> None:
    """Test extracting when text contains brackets but no JSON."""
    text = "This is [not json] and {neither is this}"
    assert extract_json(text) is None


def test_extract_json_no_json() -> None:
    """Test extracting from text with no JSON."""
    text = "Just plain text"
    assert extract_json(text) is None


def test_extract_json_deeply_nested_does_not_raise() -> None:
    """Pathologically nested brackets must not escape as RecursionError.

    Untrusted device output like '[' * N drives the JSON decoder past its
    recursion limit; extract_json must swallow it and return None rather than
    crashing a caller of LogEntry.json_payload.
    """
    assert extract_json("[" * 6000) is None
    assert extract_json("{" * 6000) is None


def test_extract_json_incomplete() -> None:
    """Test extracting incomplete JSON."""
    text = '{"key": "val"'
    assert extract_json(text) is None


def test_extract_json_scan_guard_caps_decode_attempts(mocker) -> None:
    """Ensure pathological candidates are bounded by internal attempt guard."""
    from logpyt import utils

    bad_text = "{" * (utils._EXTRACT_JSON_MAX_CANDIDATES + 100)

    def _always_fail_decode(doc: str, idx: int = 0):
        raise json.JSONDecodeError("invalid", doc, idx)

    mock_decode = mocker.patch(
        "logpyt.utils.json.JSONDecoder.raw_decode",
        side_effect=_always_fail_decode,
    )

    assert extract_json(bad_text) is None
    assert mock_decode.call_count == utils._EXTRACT_JSON_MAX_CANDIDATES


def test_extract_json_scan_guard_limits_search_range() -> None:
    """Ensure JSON beyond scan limit is not searched to avoid worst-case scans."""
    from logpyt import utils

    text = "x" * utils._EXTRACT_JSON_MAX_SCAN_CHARS + '{"after_limit": true}'

    assert extract_json(text) is None
