"""Bounded search for a JSON value inside a log message (F8-1..F8-3).

``extract_json`` is public as ``logpyt.utils.extract_json``; it lives here so
the record model does not import the adb helpers.

The search tries each ``{`` or ``[`` in order (a candidate) and returns the
first value the decoder accepts there. After a failed candidate it resumes at
the decoder's error position, never past it (F8-3), so a value that starts
exactly there is still found. Any ``ValueError`` of a candidate, such as an
integer over Python's digit limit, only rejects that candidate (F8-2).

Messages come from the device and are untrusted, so the work per call is
bounded (F8-1):

- Only the first ``_MAX_SCAN_CHARS`` characters are searched, and the decoder
  never reads past them.
- At most ``_MAX_CANDIDATES`` candidates are tried.
- A value that nests deeper than ``_MAX_DEPTH`` is rejected as if the decoder
  had failed at its first character, so the search goes on at the next
  bracket, as the old scanner did after a RecursionError. The decoder's cost
  grows with the depth, and the old scanner paid it for up to 4096 candidates.
  A run of adjacent brackets that are all that deep is skipped in one step.
- A message with no more than ``_MAX_DEPTH`` brackets in total cannot nest
  deeper, and its candidates go to the decoder directly; what the decoder
  reads counts against ``_MAX_WORK_CHARS`` as below. In any other message
  a depth check reads each candidate's strings and brackets (one token each)
  and hands the decoder only text it has read: up to the end of the value, up
  to the bracket that passes the depth limit, or up to a checkpoint. A
  candidate is too deep only when the decoder accepts that bracket. The
  checkpoints come at doubling distances and stop the check as soon as the
  decoder fails, so a candidate the decoder rejects early costs about what its
  decode costs.
- The depth check reads at most ``_MAX_DEPTH_TOKENS`` tokens per call, and at
  most ``_MAX_WORK_CHARS`` characters together with the text it hands to the
  decoder (the last token may go past that); a message that needs more gives
  None.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["extract_json"]

_MAX_SCAN_CHARS: Final = 1_000_000
_MAX_CANDIDATES: Final = 4_096
_MAX_DEPTH: Final = 128
_MAX_DEPTH_TOKENS: Final = 1 << 17
_MAX_WORK_CHARS: Final = 4 * _MAX_SCAN_CHARS

_DECODER = json.JSONDecoder()
_OPENER = re.compile(r"[\[{]")
# Where the nesting depth may change outside strings.
_STRUCTURAL = re.compile(r'["\[\]{}]')
# A string from its opening quote, as the decoder ends it; an unterminated
# one runs to the end of the text.
_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"?', re.DOTALL)
# Up to _MAX_DEPTH + 1 opening brackets with JSON whitespace between them (so
# one token can pass the limit).
_RUN = re.compile(rf"[\[{{](?:[ \t\n\r]*[\[{{]){{0,{_MAX_DEPTH}}}")
# Adjacent opening brackets: each of them but the last _MAX_DEPTH starts a
# value that nests deeper than the limit or that the decoder rejects.
_ADJACENT = re.compile(r"[\[{]+")
# Stands in for a string at a checkpoint: valid wherever a string is.
_EMPTY_STRING = '""'
# The outcome of a candidate that nests deeper than _MAX_DEPTH.
_DEEP: Final = -1


@dataclass(slots=True)
class _Work:
    """What one ``extract_json`` call did; read by the tests."""

    candidates: int = 0
    decodes: int = 0
    decoded_chars: int = 0
    depth_tokens: int = 0
    depth_chars: int = 0
    skipped_deep: int = 0


class _BudgetExhaustedError(Exception):
    """The depth check would exceed the call's token or character budget."""


def _charge(work: _Work, chars: int) -> None:
    """Count ``chars`` characters read by the depth check.

    Raises:
        _BudgetExhaustedError: When the call's character budget is used up.

    """
    work.depth_chars += chars
    if work.depth_chars + work.decoded_chars > _MAX_WORK_CHARS:
        raise _BudgetExhaustedError


def _decode(doc: str, idx: int, work: _Work) -> tuple[bool, Any]:
    """Decode the value at ``doc[idx]``.

    Returns:
        ``(True, value)``, or ``(False, position)`` with the position in ``doc``
        where the decoder failed (``idx`` for an error without a position).

    """
    work.decodes += 1
    try:
        value, _end = _DECODER.raw_decode(doc, idx)
    except json.JSONDecodeError as exc:
        return False, exc.pos
    except (ValueError, RecursionError):
        # ValueError: e.g. an integer over the digit limit (F8-2).
        # RecursionError: little stack left in the caller.
        return False, idx
    return True, value


def _decode_direct(text: str, start: int, work: _Work) -> tuple[bool, Any]:
    """Decode the candidate at ``text[start]`` in place (no depth check needed).

    The decoder reads up to the end of the value or up to its error position;
    an error without a position (an integer over the digit limit) is charged
    the rest of the text, which the decoder may have read.

    Returns:
        ``(True, value)``, or ``(False, position)`` with the position to resume
        at, never before ``start + 1``.

    Raises:
        _BudgetExhaustedError: When the call's character budget is used up
            before this decode.

    """
    if work.depth_chars + work.decoded_chars >= _MAX_WORK_CHARS:
        raise _BudgetExhaustedError
    work.decodes += 1
    try:
        value, end = _DECODER.raw_decode(text, start)
    except json.JSONDecodeError as exc:
        work.decoded_chars += exc.pos - start + 1
        return False, max(start + 1, exc.pos)
    except (ValueError, RecursionError):
        # ValueError: e.g. an integer over the digit limit (F8-2).
        # RecursionError: little stack left in the caller.
        work.decoded_chars += len(text) - start
        return False, start + 1
    work.decoded_chars += end - start
    return True, value


def _decode_span(
    text: str, start: int, stop: int, work: _Work, tail: str = ""
) -> tuple[bool, Any]:
    """Decode ``text[start:stop] + tail``; a failure position is relative to ``start``.

    Raises:
        _BudgetExhaustedError: When the call's character budget is used up.

    """
    size = stop - start + len(tail)
    if work.depth_chars + work.decoded_chars + size > _MAX_WORK_CHARS:
        raise _BudgetExhaustedError
    work.decoded_chars += size
    return _decode(text[start:stop] + tail, 0, work)


def _rejected_at(start: int, offset: int) -> tuple[bool, int]:
    """The outcome of a candidate the decoder rejected ``offset`` characters in."""
    return False, max(start + 1, start + offset)


def _settle(text: str, start: int, stop: int, work: _Work) -> tuple[bool, Any]:
    """Decode ``text[start:stop]``, all of the candidate there is: its outcome."""
    ok, result = _decode_span(text, start, stop, work)
    return (True, result) if ok else _rejected_at(start, result)


def _checkpoint(
    text: str, start: int, here: int, work: _Work
) -> tuple[bool, Any] | None:
    """Does the decoder take the string or bracket at ``text[here]``?

    Returns:
        None when it does; otherwise the candidate's outcome.

    """
    if text[here] == '"':
        ok, result = _decode_span(text, start, here, work, _EMPTY_STRING)
        accepted = here - start + len(_EMPTY_STRING)
    else:
        ok, result = _decode_span(text, start, here + 1, work)
        accepted = here - start + 1
    if ok:  # the value closed before the cut
        return True, result
    if result < accepted:
        return _rejected_at(start, result)
    return None


def _opener_run(text: str, here: int) -> tuple[int, int]:
    """The end of the run of opening brackets at ``text[here]`` and their number."""
    run = _RUN.match(text, here)
    run_end = here + 1 if run is None else run.end()  # always a match
    return run_end, text.count("[", here, run_end) + text.count("{", here, run_end)


def _at_the_limit(
    text: str, start: int, here: int, run_end: int, before: int, work: _Work
) -> tuple[bool, Any]:
    """The outcome of a candidate whose depth passes the limit in the run at ``here``.

    ``before`` brackets of the run keep the depth within the limit; the next
    one passes it, and the candidate is too deep if the decoder takes it.
    """
    limit = here
    for index, opener in enumerate(_OPENER.finditer(text, here, run_end)):
        if index == before:
            limit = opener.start()
            break
    _charge(work, limit + 1 - here)
    ok, result = _decode_span(text, start, limit + 1, work)
    if ok:
        return True, result
    if result <= limit - start:
        return _rejected_at(start, result)
    return False, _DEEP


def _examine(text: str, start: int, work: _Work) -> tuple[bool, Any]:
    """Decode the candidate at ``text[start]`` with its depth checked.

    The decoder only ever gets text the check has read, cut right after an
    opening bracket or with a string replaced by ``""``. Up to the cut it then
    behaves as on the whole text, and it fails exactly at the cut when it
    takes what is there; any earlier failure is the one the whole text gives.

    Returns:
        ``(True, value)``; ``(False, index)`` with the index to resume the
        search at; or ``(False, _DEEP)``.

    Raises:
        _BudgetExhaustedError: When the call's budget is used up.

    """
    depth = 0
    pos = start
    checked = 0  # distance from start of the last checkpoint the decoder passed
    while True:
        if work.depth_tokens >= _MAX_DEPTH_TOKENS:
            raise _BudgetExhaustedError
        work.depth_tokens += 1
        found = _STRUCTURAL.search(text, pos)
        if found is None:  # the text ends inside the value
            _charge(work, len(text) - pos)
            return _settle(text, start, len(text), work)
        here = found.start()
        char = text[here]
        _charge(work, here - pos)
        if char in "]}":
            _charge(work, 1)
            pos = here + 1
            depth -= 1
            if depth == 0:  # the value closes
                return _settle(text, start, pos, work)
            continue
        if here > start and here - start >= 2 * checked:
            outcome = _checkpoint(text, start, here, work)
            if outcome is not None:
                return outcome
            checked = here - start
        if char == '"':
            string = _STRING.match(text, here)
            pos = len(text) if string is None else string.end()  # always a match
            _charge(work, pos - here)
            continue
        run_end, openers = _opener_run(text, here)
        if depth + openers > _MAX_DEPTH:
            return _at_the_limit(text, start, here, run_end, _MAX_DEPTH - depth, work)
        _charge(work, run_end - here)
        pos = run_end
        depth += openers


def _extract(text: str, work: _Work) -> Any | None:  # noqa: ANN401  (arbitrary parsed JSON)
    if len(text) > _MAX_SCAN_CHARS:
        text = text[:_MAX_SCAN_CHARS]
    check_depth = text.count("[") + text.count("{") > _MAX_DEPTH
    idx = 0
    while work.candidates < _MAX_CANDIDATES:
        found = _OPENER.search(text, idx)
        if found is None:
            return None
        start = found.start()
        work.candidates += 1
        if check_depth:
            try:
                ok, result = _examine(text, start, work)
            except _BudgetExhaustedError:
                return None
        else:
            try:
                ok, result = _decode_direct(text, start, work)
            except _BudgetExhaustedError:
                return None
        if ok:
            return result
        if result != _DEEP:
            idx = result
            continue
        # Reject the candidate as if the decoder had failed at its first
        # character, and with it every candidate of the same run of adjacent
        # brackets that is just as deep, in one step.
        run = _ADJACENT.match(text, start)
        idx = start + 1 if run is None else max(start + 1, run.end() - _MAX_DEPTH)
        skipped = idx - start
        if work.candidates - 1 + skipped >= _MAX_CANDIDATES:
            return None  # the cap would be reached inside the run
        work.skipped_deep += skipped
        work.candidates += skipped - 1
    return None


def extract_json(text: str) -> Any | None:  # noqa: ANN401  (arbitrary parsed JSON)
    """Return the first JSON object or array found in ``text``.

    The search is bounded (see the module documentation); a value the bounds
    leave out gives None, never an exception.

    Args:
        text: The text to search, usually a log message.

    Returns:
        The parsed object (dict) or array (list), or None.

    """
    if not text:
        return None
    return _extract(text, _Work())
