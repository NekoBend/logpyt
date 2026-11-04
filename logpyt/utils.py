"""Helpers outside the streams: debug logging and JSON extraction.

The adb helpers live in `logpyt.adb` since 3.0.0. Importing logpyt installs a
`logging.NullHandler` on the ``logpyt`` logger, so an application that
configured no logging sees none of logpyt's records; `enable_debug` shows them.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

__all__ = ["enable_debug", "extract_json"]

_LOGGER_NAME = "logpyt"
# The name enable_debug gives its handler, so a second call finds it.
_DEBUG_HANDLER_NAME = "logpyt.enable_debug"
_DEBUG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_debug_lock = threading.Lock()

# A library leaves logging configuration to the application: without a handler
# of its own, logpyt's WARNING records would reach logging.lastResort and print
# to stderr in an application that configured nothing.
_logpyt_logger = logging.getLogger(_LOGGER_NAME)
if not any(isinstance(h, logging.NullHandler) for h in _logpyt_logger.handlers):
    _logpyt_logger.addHandler(logging.NullHandler())

# Safeguards for pathological extract_json inputs.
_EXTRACT_JSON_MAX_SCAN_CHARS = 1_000_000
_EXTRACT_JSON_MAX_CANDIDATES = 4_096


def enable_debug(level: str | int = "DEBUG") -> None:
    """Show logpyt's log records on stderr, from ``level`` up.

    Sets the level of the ``logpyt`` logger and adds one stderr handler the
    first time; a later call only changes the level. The root logger is left
    alone, and records still propagate to it, so a handler the application put
    there shows them as well.

    Args:
        level: A level name in any case (``"debug"``, ``"Warning"``) or a level
            number (``logging.DEBUG``). The default shows logpyt's DEBUG
            diagnostics too (F9-5).

    Raises:
        ValueError: ``level`` is no level name, or a negative number.
        TypeError: ``level`` is neither a str nor an int.
    """
    number = _level_number(level)
    logger = logging.getLogger(_LOGGER_NAME)
    with _debug_lock:
        logger.setLevel(number)
        if not any(h.get_name() == _DEBUG_HANDLER_NAME for h in logger.handlers):
            handler = logging.StreamHandler()
            handler.set_name(_DEBUG_HANDLER_NAME)
            handler.setFormatter(logging.Formatter(_DEBUG_FORMAT))
            logger.addHandler(handler)


def _level_number(level: str | int) -> int:
    """The level number of a case-insensitive level name or a level number.

    A name is looked up as written first, then in any case, so a custom level
    registered in mixed case (``logging.addLevelName(5, "Trace")``) is found too.
    """
    if isinstance(level, bool) or not isinstance(level, str | int):
        msg = f"level must be a level name or number, not {type(level).__name__}"
        raise TypeError(msg)
    if isinstance(level, int):
        if level < 0:
            msg = f"level must not be negative, got {level}"
            raise ValueError(msg)
        return level
    names = logging.getLevelNamesMapping()
    name = level.strip()
    number = names.get(name)
    if number is None:
        folded = {key.upper(): value for key, value in names.items()}
        number = folded.get(name.upper())
    if number is None:
        msg = (
            f"unknown logging level {level!r}; use one of "
            f"{', '.join(sorted(names, key=names.__getitem__))} (any case) or a number"
        )
        raise ValueError(msg)
    return number


def extract_json(text: str) -> Any | None:  # noqa: ANN401
    """Extract and parse the first valid JSON object or array found in the text.

    Args:
        text: The text to search.

    Returns:
        The parsed JSON object (dict or list) if found, otherwise None.

    Notes:
        Scanning is bounded to protect against worst-case inputs with huge numbers
        of JSON-like delimiters that repeatedly fail to parse.

    """
    if not text:
        return None

    decoder = json.JSONDecoder()
    idx = 0
    attempts = 0
    scan_limit = min(len(text), _EXTRACT_JSON_MAX_SCAN_CHARS)

    while idx < scan_limit and attempts < _EXTRACT_JSON_MAX_CANDIDATES:
        # Find next potential start
        next_brace = text.find("{", idx, scan_limit)
        next_bracket = text.find("[", idx, scan_limit)

        if next_brace == -1 and next_bracket == -1:
            return None

        if next_brace == -1:
            start = next_bracket
        elif next_bracket == -1:
            start = next_brace
        else:
            start = min(next_brace, next_bracket)

        try:
            obj, _ = decoder.raw_decode(text, idx=start)
            return obj
        except json.JSONDecodeError as exc:
            # Failed to parse from this position.
            # Heuristic: if decoder reports a later error position, skip there
            # to reduce repeated scans over known-invalid spans.
            attempts += 1
            next_idx = max(start + 1, exc.pos + 1)
            idx = min(next_idx, scan_limit)
        except RecursionError:
            # Pathologically nested brackets (untrusted device output) exceed the
            # decoder's recursion limit; skip this candidate rather than letting
            # the error escape into a caller of LogEntry.json_payload.
            attempts += 1
            idx = min(start + 1, scan_limit)

    return None
