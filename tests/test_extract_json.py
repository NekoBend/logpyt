"""Work bounds of ``extract_json`` (F8-1..F8-3), asserted on work counters.

``logpyt._json._extract`` reports what one call did (candidates tried, decoder
calls and the characters handed to them, depth-check tokens and characters),
so the bounds are checked as counts, never as wall time.
"""

from __future__ import annotations

import json
import sys

import pytest

from logpyt import _json
from logpyt.utils import extract_json


def _run(text: str) -> tuple[object, _json._Work]:
    work = _json._Work()
    return _json._extract(text, work), work


def _depth(value: object) -> int:
    depth = 0
    while isinstance(value, list) and value:
        depth += 1
        value = value[0]
    return depth + (1 if isinstance(value, list) else 0)


def test_deep_brackets_are_skipped_in_one_step() -> None:
    """100 000 nested '[' cost one short decode, not one per bracket (F8-1).

    The decode confirms that the decoder takes bracket 129. Every bracket of
    the run but the last _MAX_DEPTH is then just as deep, and the run is
    longer than the candidate cap, so the search ends there.
    """
    value, work = _run("[" * 100_000)

    assert value is None
    assert work.decodes == 1
    assert work.depth_tokens == 1
    assert work.decoded_chars == _json._MAX_DEPTH + 1
    assert work.depth_chars == _json._MAX_DEPTH + 1


def test_deep_run_shorter_than_the_cap_ends_in_one_more_decode() -> None:
    """A deep run is skipped up to its last _MAX_DEPTH brackets, decoded once more."""
    value, work = _run("[" * 4000)

    assert value is None
    assert work.decodes == 2
    assert work.skipped_deep == 4000 - _json._MAX_DEPTH
    assert work.decoded_chars <= 2 * (_json._MAX_DEPTH + 1)


def test_deep_brackets_with_spaces_stay_within_the_candidate_cap() -> None:
    """'[ [ [ ...' costs one depth token and one short decode per candidate."""
    value, work = _run("[ " * 50_000)

    assert value is None
    assert work.candidates <= _json._MAX_CANDIDATES
    assert work.decodes <= _json._MAX_CANDIDATES
    assert work.depth_tokens <= _json._MAX_CANDIDATES
    assert work.decoded_chars <= _json._MAX_CANDIDATES * 2 * (_json._MAX_DEPTH + 1)


def test_deep_chain_of_keys_stops_at_the_token_budget() -> None:
    """A deep key chain ('[{"a":' repeated) ends at the depth-token budget."""
    value, work = _run('[{"a":' * 20_000)

    assert value is None
    assert work.depth_tokens <= _json._MAX_DEPTH_TOKENS


def test_value_at_the_depth_limit_is_decoded_whole() -> None:
    """A value nesting exactly _MAX_DEPTH levels is found, brackets elsewhere or not."""
    limit = _json._MAX_DEPTH
    nested = "[" * limit + "]" * limit

    assert _depth(extract_json(nested)) == limit
    assert _depth(extract_json(nested + " [" * 10)) == limit


def test_value_deeper_than_the_limit_is_not_decoded_whole() -> None:
    """Past _MAX_DEPTH the outer value is skipped; the search goes on inside it."""
    limit = _json._MAX_DEPTH
    value, work = _run("[" * (limit + 50) + "]" * (limit + 50))

    assert _depth(value) == limit
    assert work.decodes == 2  # the outer value's depth, then the inner value


def test_brackets_inside_strings_do_not_count_as_depth() -> None:
    """A string full of '[' does not make its object too deep."""
    text = '{"s": "' + "[" * 1000 + '"}'

    assert extract_json(text) == {"s": "[" * 1000}


def test_failing_candidates_resume_at_the_error_position() -> None:
    """The scan resumes at exc.pos, so an inner object costs one more decode (F8-3)."""
    value, work = _run('{{"a": 1}}')

    assert value == {"a": 1}
    assert (work.candidates, work.decodes) == (2, 2)


def test_depth_checked_candidates_resume_at_the_error_position() -> None:
    """With the depth check on (over _MAX_DEPTH brackets), F8-3 holds as well."""
    value, work = _run('{{"a": 1}} ' + "[" * 200)

    assert value == {"a": 1}
    assert work.candidates == 2


def test_candidate_is_too_deep_only_where_the_decoder_reaches_the_limit() -> None:
    """Brackets after the decoder's error position do not make a candidate deep.

    The decoder rejects '{"k": [1] x' at the x, so the search goes on there
    and never inside that object ([1]), with or without deep brackets later.
    """
    rejected = '{"k": [1] x'
    found = rejected + ' {"ok": 1} '
    deep = " " + "[" * 200

    assert extract_json(rejected) is None
    assert extract_json(rejected + deep) is None
    assert extract_json(found) == {"ok": 1}
    assert extract_json(found + deep) == {"ok": 1}


def test_candidate_rejected_between_checkpoints_is_not_deep() -> None:
    """A rejection after the last checkpoint is found at the depth limit.

    The decoder passes the checkpoint at '"a"', rejects the x, and the deep
    run comes before the next checkpoint is due. Judged deep, the candidate
    would be skipped to its next bracket and [1] returned.
    """
    text = "[" + " " * 50 + '"a", [1] x '

    assert extract_json(text) is None
    assert extract_json(text + "[" * 200) is None


def test_brackets_the_decoder_rejects_early_cost_one_decode_each() -> None:
    """'[x' repeated is rejected at each x; a value after them is still found.

    1500 candidates fit the candidate cap; each one costs a decode of a few
    characters and two depth tokens, so the depth budgets are not reached.
    """
    value, work = _run("[x" * 1500 + '{"a":1}')

    assert value == {"a": 1}
    assert work.candidates == 1501
    assert work.depth_tokens <= 2 * work.candidates + 1
    assert work.decoded_chars <= 4 * work.candidates


def test_depth_check_reads_each_long_string_about_once() -> None:
    """Long strings between brackets are not re-read for every candidate (F8-1).

    Each '["aaa..."' is rejected at the next '[', and the check stops there
    with it, instead of reading on to the depth limit through 128 strings.
    """
    text = ('["' + "a" * 2000 + '"') * 100
    value, work = _run(text)

    assert value is None
    assert work.candidates == 100
    assert work.depth_chars <= 2 * len(text)
    assert work.decoded_chars <= 2 * len(text)


def test_long_string_after_rejected_brackets_is_read_about_once() -> None:
    """A long string after many rejected brackets costs about its length (F8-1)."""
    text = "[x" * 128 + '"' + "[" * 1000 + "a" * 200_000 + '"' + "]" * 128
    value, work = _run(text)

    assert value is None
    assert work.depth_chars <= 2 * len(text)
    assert work.decoded_chars <= 2 * len(text)


def test_rescanning_deep_values_stops_at_the_character_budget() -> None:
    """Deep values with long strings in them end at the character budget (F8-1).

    Every candidate is a deep value whose check reads past 128 strings, and
    the next candidate is nested in it; the budget, not the text, ends the
    search.
    """
    text = ('["' + "a" * 200 + '",') * 4000
    value, work = _run(text)

    assert value is None
    assert work.candidates < 100
    assert (
        work.depth_chars + work.decoded_chars
        <= _json._MAX_WORK_CHARS + _json._MAX_SCAN_CHARS
    )


def test_decode_attempts_are_capped() -> None:
    """At most _MAX_CANDIDATES candidates are tried per call."""
    value, work = _run("{x}" * (_json._MAX_CANDIDATES + 500))

    assert value is None
    assert work.candidates == _json._MAX_CANDIDATES
    assert work.decodes == _json._MAX_CANDIDATES


def test_unclosed_brackets_cost_one_short_decode_each() -> None:
    """Many unclosed '{': each is rejected at its first checkpoint, the cap ends it."""
    cap = _json._MAX_CANDIDATES
    value, work = _run("{x" * (cap + 500))

    assert value is None
    assert (work.candidates, work.decodes) == (cap, cap)
    assert work.depth_tokens <= 2 * cap
    assert work.decoded_chars <= 3 * cap


@pytest.mark.parametrize(
    "suffix",
    [pytest.param("", id="plain"), pytest.param(" " + "[" * 200, id="depth-checked")],
)
def test_huge_integer_rejects_only_its_candidate(suffix: str) -> None:
    """An integer over the digit limit is a rejected candidate, not an error (F8-2)."""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        value = extract_json("n=[" + "1" * 5000 + '] then {"ok": 1}' + suffix)
    finally:
        sys.set_int_max_str_digits(previous)

    assert value == {"ok": 1}


def test_recursion_error_rejects_only_its_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RecursionError (little stack left in the caller) is a rejected candidate."""
    decode = json.JSONDecoder.raw_decode

    def shallow(self: json.JSONDecoder, s: str, idx: int = 0) -> tuple[object, int]:
        if s.startswith("[[", idx):
            raise RecursionError
        return decode(self, s, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", shallow)

    assert extract_json('[[1]] and {"ok": 1}') == [1]
    assert extract_json("[[1]]") == [1]


@pytest.mark.parametrize(
    "prefix",
    [pytest.param("", id="plain"), pytest.param("[" * 200 + " ", id="depth-checked")],
)
def test_decoder_never_reads_past_the_scan_window(prefix: str) -> None:
    """A value that starts in the window but ends after it is not found."""
    filler = "x" * (_json._MAX_SCAN_CHARS - 20 - len(prefix))
    inside = prefix + filler + '{"key": "' + "v" * 40 + '"}'

    assert extract_json(inside) is None
    assert extract_json(prefix + filler + '{"a": 1}') == {"a": 1}


def test_empty_text_does_no_work() -> None:
    """An empty message returns None at once."""
    assert extract_json("") is None
    value, work = _run("no brackets at all")
    assert value is None
    assert work.candidates == 0


def test_shallow_candidates_around_a_huge_integer_stay_in_the_work_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With at most _MAX_DEPTH brackets no depth check runs, but what the decoder
    reads still counts (F8-1): an integer over the digit limit fails without a
    position, and each candidate would read it again.
    """
    decode = json.JSONDecoder.raw_decode
    seen: list[int] = []

    def counting(self: json.JSONDecoder, s: str, idx: int = 0) -> tuple[object, int]:
        seen.append(len(s) - idx)
        return decode(self, s, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", counting)
    text = "[" * _json._MAX_DEPTH + "1" * 900_000 + "]" * _json._MAX_DEPTH

    value, work = _run(text)

    assert value is None
    assert len(seen) < _json._MAX_DEPTH
    assert sum(seen) <= _json._MAX_WORK_CHARS + _json._MAX_SCAN_CHARS
    assert work.decoded_chars >= sum(seen) - _json._MAX_SCAN_CHARS
