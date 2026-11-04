"""The 3.0 stream states, events and stats (design sections 2.5, 3.1 and 4.4)."""

from __future__ import annotations

import dataclasses
import pickle  # noqa: S403  round-trips objects the test made
from datetime import UTC, datetime

import pytest

from logpyt import Gap, ReadTimeoutError, StateChange, StreamEvent, StreamStats
from logpyt.streams import StreamState as LegacyStreamState
from logpyt.streams._events import StreamState

DESIGN_STATS_FIELDS = [
    "connections",
    "reconnects",
    "stall_resumes",
    "backpressure_seconds",
    "lines",
    "stderr_lines",
    "delivered",
    "filtered",
    "deduplicated",
    "below_cursor",
    "before_start",
    "undelivered",
    "unparsed",
    "truncated",
    "unattributed",
    "gaps",
    "device_lost",
    "backlog_pruned",
    "overlap_shortened",
    "events_coalesced",
]
# The incompleteness reasons that no counter of the design's list can express.
OUTCOME_FLAGS = ["flush_timeout", "flush_failed", "killed", "failed"]
REASONS_IN_ORDER = [
    ("gaps", 1, "gap"),
    ("device_lost", 3, "device_lost"),
    ("unattributed", 1, "unattributed"),
    ("undelivered", 2, "undelivered"),
    ("truncated", 1, "truncated"),
    ("flush_timeout", True, "flush_timeout"),
    ("flush_failed", True, "flush_failed"),
    ("killed", True, "killed"),
    ("failed", True, "failed"),
]
# Every counter that makes a capture incomplete, with its reason (design 4.4).
COUNTER_REASONS = [
    ("gaps", "gap"),
    ("device_lost", "device_lost"),
    ("unattributed", "unattributed"),
    ("undelivered", "undelivered"),
    ("truncated", "truncated"),
]
_T0 = datetime(2026, 11, 4, 2, 48, 35, 826893, tzinfo=UTC)
_EMPTY = StreamStats()


# -- StreamState -----------------------------------------------------------------


def test_stream_state_has_the_designed_members_in_order() -> None:
    assert [state.name for state in StreamState] == [
        "IDLE",
        "CONNECTING",
        "RUNNING",
        "RECONNECTING",
        "STOPPING",
        "STOPPED",
        "KILLED",
        "FAILED",
    ]


@pytest.mark.parametrize("state", list(StreamState))
def test_stream_state_value_and_str_equal_the_name(state: StreamState) -> None:
    assert state.value == state.name == str(state) == f"{state}"
    assert StreamState(state.name) is state


def test_the_2x_stream_state_is_untouched_during_the_coexistence_window() -> None:
    assert LegacyStreamState is not StreamState
    assert "PAUSED" in LegacyStreamState.__members__
    assert "STARTING" in LegacyStreamState.__members__


# -- StateChange and Gap ---------------------------------------------------------


def test_state_change_error_is_required() -> None:
    change = StateChange(StreamState.IDLE, StreamState.CONNECTING, 1, None)
    assert change.error is None
    assert change == StateChange(
        old=StreamState.IDLE, new=StreamState.CONNECTING, connection=1, error=None
    )
    with pytest.raises(TypeError, match="error"):
        StateChange(StreamState.IDLE, StreamState.CONNECTING, 1)  # ty: ignore[missing-argument]


@pytest.mark.parametrize(
    ("event", "field"),
    [
        (StateChange(StreamState.RUNNING, StreamState.STOPPING, 1, None), "connection"),
        (Gap(after=_T0, before=_T0, lost=None, reason="ring_rotated"), "lost"),
    ],
    ids=["StateChange", "Gap"],
)
def test_events_are_frozen_slotted_and_hashable(event: StreamEvent, field: str) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(event, field, 2)
    assert not hasattr(event, "__dict__")
    assert hash(event) == hash(dataclasses.replace(event))
    assert pickle.loads(pickle.dumps(event)) == event  # noqa: S301


def test_stream_event_is_the_union_of_state_change_and_gap() -> None:
    assert StreamEvent.__value__ == StateChange | Gap


def _route(event: StreamEvent) -> str:
    """Return what the scenario (c) monitor of design section 2.11 would log."""
    match event:
        case StateChange(new=StreamState.RECONNECTING, error=None):
            return "consumer fell behind"
        case StateChange(new=StreamState.RECONNECTING, error=error):
            return f"connection lost: {error}"
        case StateChange(new=StreamState.RUNNING, connection=n) if n > 1:
            return f"resumed {n}"
        case Gap(reason=reason, lost=lost):
            return f"gap {reason} {'unknown' if lost is None else lost}"
        case _:
            return "other"


@pytest.mark.parametrize(
    ("event", "logged"),
    [
        (
            StateChange(StreamState.RUNNING, StreamState.RECONNECTING, 1, None),
            "consumer fell behind",
        ),
        (
            StateChange(
                StreamState.RUNNING,
                StreamState.RECONNECTING,
                1,
                ReadTimeoutError("no output for 60 s", stats=StreamStats()),
            ),
            "connection lost: no output for 60 s",
        ),
        (
            StateChange(StreamState.CONNECTING, StreamState.RUNNING, 2, None),
            "resumed 2",
        ),
        (StateChange(StreamState.CONNECTING, StreamState.RUNNING, 1, None), "other"),
        (Gap(None, _T0, None, "clock_moved_back"), "gap clock_moved_back unknown"),
        (Gap(_T0, _T0, 12, "device_reported"), "gap device_reported 12"),
    ],
)
def test_events_support_the_scenario_c_match_patterns(
    event: StreamEvent, logged: str
) -> None:
    assert _route(event) == logged


# -- StreamStats -----------------------------------------------------------------


def test_stream_stats_has_the_designed_fields_plus_the_outcome_flags() -> None:
    names = [field.name for field in dataclasses.fields(StreamStats)]
    assert names == DESIGN_STATS_FIELDS + OUTCOME_FLAGS


def test_empty_stream_stats_is_zero_and_complete() -> None:
    stats = StreamStats()
    assert all(not getattr(stats, name) for name in DESIGN_STATS_FIELDS)
    assert stats.incomplete == ()
    assert stats.complete is True


def test_stream_stats_is_keyword_only_and_frozen() -> None:
    with pytest.raises(TypeError):
        StreamStats(1)  # ty: ignore[too-many-positional-arguments]
    stats = StreamStats(lines=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        stats.lines = 2  # ty: ignore[invalid-assignment]
    assert not hasattr(stats, "__dict__")


@pytest.mark.parametrize(("field", "value", "reason"), REASONS_IN_ORDER)
def test_each_incompleteness_reason_comes_from_its_field(
    field: str, value: object, reason: str
) -> None:
    stats = dataclasses.replace(_EMPTY, **{field: value})
    assert stats.incomplete == (reason,)
    assert stats.complete is False


@pytest.mark.parametrize(("field", "reason"), COUNTER_REASONS)
@pytest.mark.parametrize("value", [1, 7])
def test_any_positive_counter_value_makes_the_capture_incomplete(
    field: str, reason: str, value: int
) -> None:
    # The threshold is "> 0" for every counter (design 4.4): one lost line counts.
    assert dataclasses.replace(_EMPTY, **{field: value}).incomplete == (reason,)


def test_incompleteness_reasons_keep_the_documented_order() -> None:
    stats = dataclasses.replace(
        _EMPTY, **{field: value for field, value, _ in REASONS_IN_ORDER}
    )
    assert stats.incomplete == tuple(reason for _, _, reason in REASONS_IN_ORDER)


@pytest.mark.parametrize(
    "field",
    [
        "connections",
        "reconnects",
        "stall_resumes",
        "backpressure_seconds",
        "lines",
        "stderr_lines",
        "delivered",
        "filtered",
        "deduplicated",
        "below_cursor",
        "before_start",
        "unparsed",
        "backlog_pruned",
        "overlap_shortened",
        "events_coalesced",
    ],
)
def test_counters_that_prove_no_loss_keep_the_capture_complete(field: str) -> None:
    assert dataclasses.replace(_EMPTY, **{field: 5}).complete is True
