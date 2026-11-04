"""Tests for GroupBy, LogGroup and the grouping engine (design 2.6, 2.8, D-10, D-11).

The engine (`logpyt._core.grouping.Grouper`) is driven directly where a test
needs the host clock (`flush_idle`) or the open state; everything else goes
through the public `GroupBy.groups`.
"""

from __future__ import annotations

import dataclasses
import decimal
import math
import random
from collections.abc import Hashable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

import logpyt
from logpyt import GroupBy, LogEntry, LogGroup, groupers, models
from logpyt._core import grouping
from logpyt._core.grouping import Grouper, entry_bytes, gap_micros
from logpyt.models import group as group_module

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from logpyt.groupers import GroupKey

_T0 = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_MODES = ["consecutive", "windowed"]


def _entry(
    message: str = "m",
    *,
    us: int = 0,
    pid: int = 1,
    tag: str = "T",
    uid: int | None = None,
    package: str | None = None,
    raw: str | None = None,
) -> LogEntry:
    return LogEntry(
        timestamp=_T0 + timedelta(microseconds=us),
        pid=pid,
        tid=pid,
        uid=uid,
        level="I",
        tag=tag,
        message=message,
        raw=message if raw is None else raw,
        package=package,
    )


def _ms(value: float) -> int:
    """Milliseconds as whole microseconds, for `_entry(us=...)`."""
    return round(value * 1000)


def _spec(**changes: Any) -> GroupBy:  # noqa: ANN401  (any GroupBy keyword)
    fields: dict[str, Any] = {"keys": ("pid",), "gap_ms": 100.0, **changes}
    return GroupBy(**fields)


def _summary(groups: Iterable[LogGroup]) -> list[tuple[list[str], str]]:
    return [([entry.message for entry in group], group.reason) for group in groups]


# -- F7-3: integer microseconds, inclusive boundary --------------------------------


@pytest.mark.parametrize("mode", _MODES)
def test_exact_threshold_is_inclusive(mode: str) -> None:
    """The review's repro: gap 1884.0 ms, entries at +123 ms and +2007 ms.

    2.x computed float milliseconds from the first entry and got 2007.0 for
    the expiry against 2007.0000000000002 for the entry, so the windowed
    grouper split exactly at the boundary.
    """
    spec = _spec(gap_ms=1884.0, mode=mode)
    entries = [_entry("t0"), _entry("t123", us=_ms(123)), _entry("t2007", us=_ms(2007))]
    later = [*entries[:2], _entry("over", us=_ms(2007) + 1)]

    assert _summary(spec.groups(entries)) == [(["t0", "t123", "t2007"], "end")]
    assert _summary(spec.groups(later)) == [(["t0", "t123"], "gap"), (["over"], "end")]


@pytest.mark.parametrize("mode", _MODES)
def test_exact_threshold_never_splits_in_a_sweep(mode: str) -> None:
    """Every integer gap 1..2000 ms, ms-aligned and with a sub-ms offset.

    The first entry (another key) is only the time origin 2.x measured from.
    2.x split at the exact boundary in 12 of 10000 ms-aligned and 652 of 6000
    sub-ms cases (survey 2026-11-03).
    """
    for gap in range(1, 2001):
        spec = _spec(gap_ms=float(gap), mode=mode)
        for offset in (_ms(123), 123_457):
            entries = [
                _entry("origin", pid=2),
                _entry("b", us=offset),
                _entry("c", us=offset + gap * 1000),
            ]
            groups = [list(group) for group in spec.groups(entries)]
            assert entries[1:] in groups, (gap, offset)


@pytest.mark.parametrize(
    ("gap_ms", "joins", "splits"),
    [
        (0.57, 570, 571),
        (1.1, 1100, 1101),
        (0.0005, 0, 1),
        (3, 3000, 3001),
        (1.005, 1005, 1006),  # 1.005 * 1000 is 1004.9999999999999 as a float
        (0.0019, 1, 2),  # 1.9 us rounds down, not to the nearest
    ],
    ids=["0.57", "1.1", "half-us", "int", "1.005", "1.9us"],
)
def test_gap_ms_is_compared_in_whole_microseconds(
    gap_ms: float, joins: int, splits: int
) -> None:
    """The decimal value of gap_ms is the boundary, never its float rounding."""
    spec = _spec(gap_ms=gap_ms)

    joined = list(spec.groups([_entry("a"), _entry("b", us=joins)]))
    split = list(spec.groups([_entry("a"), _entry("b", us=splits)]))

    assert gap_micros(gap_ms) == joins
    assert [len(group) for group in joined] == [2]
    assert [len(group) for group in split] == [1, 1]


_TRAPS = [decimal.Inexact, decimal.Rounded, decimal.FloatOperation]


@pytest.mark.parametrize(
    "context",
    [
        decimal.Context(),
        decimal.Context(prec=3),
        decimal.Context(prec=3, rounding=decimal.ROUND_CEILING),
        decimal.Context(prec=3, traps=_TRAPS),
        decimal.Context(traps=_TRAPS),
    ],
    ids=["default", "prec3", "ceiling", "traps-prec3", "traps"],
)
def test_gap_ignores_the_decimal_context(context: decimal.Context) -> None:
    """The caller's decimal context neither moves the gap nor raises.

    The spec is validated and the entries are grouped inside the context. A
    multiplication in it would round 123.456 ms to 123000 us at ``prec=3``
    (124000 when rounding up) and raise when Inexact or Rounded is trapped;
    the default 28 digits round an int of 31 digits.
    """
    near = [_entry("a"), _entry("b", us=123_456)]
    far = [_entry("a"), _entry("b", us=123_457)]
    huge = 10**30 + 1

    with decimal.localcontext(context) as active:
        spec = _spec(gap_ms=123.456, idle_flush_ms=0.5)
        joined = [len(group) for group in spec.groups(near)]
        split = [len(group) for group in spec.groups(far)]
        micros = [gap_micros(gap) for gap in (123.456, 123.556, huge, 0.57)]
        huge_spec = _spec(gap_ms=huge)

    assert (joined, split) == ([2], [1, 1])
    assert micros == [123_456, 123_556, huge * 1000, 570]
    assert huge_spec.gap_ms == huge
    assert not any(active.flags.values())


# -- GroupBy validation (F7-2, design 4.2) -----------------------------------------


def test_keys_bare_string_raises_type_error() -> None:
    with pytest.raises(TypeError, match=r"keys=\('pid',\)"):
        _spec(keys="pid")


@pytest.mark.parametrize(
    ("keys", "error"),
    [
        (("pdi",), ValueError),
        (("pid", "meta"), ValueError),
        (("pid", "pid"), ValueError),
        ((1,), TypeError),
        ({"pid"}, TypeError),
        (None, TypeError),
        (b"pid", TypeError),
    ],
    ids=["typo", "meta", "repeated", "int", "set", "none", "bytes"],
)
def test_bad_keys_raise(keys: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        _spec(keys=keys)


def test_keys_list_is_stored_as_a_tuple() -> None:
    spec = _spec(keys=["tag", "pid"])

    assert spec.keys == ("tag", "pid")
    assert spec == _spec(keys=("tag", "pid"))
    assert hash(spec) == hash(_spec(keys=("tag", "pid")))


def test_every_key_field_is_accepted() -> None:
    keys: tuple[GroupKey, ...] = ("pid", "tid", "uid", "tag", "level", "package")
    entry = _entry(uid=10123, package="com.example")

    (group,) = GroupBy(keys=keys, gap_ms=1.0).groups([entry])

    assert group.key == (1, 1, 10123, "T", "I", "com.example")


def test_empty_keys_group_by_time_alone() -> None:
    spec = _spec(keys=())
    entries = [_entry("a", pid=1), _entry("b", pid=2), _entry("c", pid=1, us=_ms(500))]

    groups = list(spec.groups(entries))

    assert _summary(groups) == [(["a", "b"], "gap"), (["c"], "end")]
    assert [group.key for group in groups] == [(), ()]


_HUGE = 10**400  # an int too large for a float


_BAD_VALUES: list[tuple[str, object, type[Exception]]] = [
    ("gap_ms", 0, ValueError),
    ("gap_ms", -1.0, ValueError),
    ("gap_ms", math.nan, ValueError),
    ("gap_ms", math.inf, ValueError),
    ("gap_ms", "100", TypeError),
    ("gap_ms", True, TypeError),
    ("gap_ms", None, TypeError),
    ("gap_ms", _HUGE, ValueError),
    ("mode", "windows", ValueError),
    ("mode", "Windowed", ValueError),
    ("mode", 1, TypeError),
    ("idle_flush_ms", 0, ValueError),
    ("idle_flush_ms", -5.0, ValueError),
    ("idle_flush_ms", math.inf, ValueError),
    ("idle_flush_ms", "never", ValueError),
    ("idle_flush_ms", False, TypeError),
    ("idle_flush_ms", _HUGE, ValueError),
    ("max_group_size", 0, ValueError),
    ("max_group_size", -1, ValueError),
    ("max_group_size", 2.0, TypeError),
    ("max_group_size", True, TypeError),
    ("max_groups", 0, ValueError),
    ("max_total_entries", 0, ValueError),
    ("max_total_bytes", 0, ValueError),
    ("max_total_bytes", "8M", TypeError),
]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    _BAD_VALUES,
    ids=[
        f"{field}={'10**400' if value is _HUGE else repr(value)}"
        for field, value, _ in _BAD_VALUES
    ],
)
def test_bad_values_raise(field: str, value: object, error: type[Exception]) -> None:
    with pytest.raises(error, match=field):
        _spec(**{field: value})


def test_defaults_follow_the_design() -> None:
    spec = GroupBy(keys=("pid",), gap_ms=50)

    assert dataclasses.asdict(spec) == {
        "keys": ("pid",),
        "gap_ms": 50,
        "mode": "consecutive",
        "idle_flush_ms": "gap",
        "max_group_size": 1000,
        "max_groups": 1000,
        "max_total_entries": 20_000,
        "max_total_bytes": 8 * 1024 * 1024,
    }


def test_group_by_is_frozen_and_keyword_only() -> None:
    spec = _spec()
    positional: Any = GroupBy

    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.gap_ms = 5.0  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        positional(("pid",), 100.0)
    with pytest.raises(TypeError, match="emit_mode"):
        _spec(emit_mode="group")
    with pytest.raises(ValueError, match="gap_ms"):
        dataclasses.replace(spec, gap_ms=0)
    assert not hasattr(spec, "__dict__")


# -- reasons: when each one is produced, per mode -------------------------------------


def test_consecutive_key_change_closes_with_key() -> None:
    entries = [_entry("a", pid=1), _entry("b", pid=2), _entry("c", pid=1)]

    groups = list(_spec().groups(entries))

    assert _summary(groups) == [(["a"], "key"), (["b"], "key"), (["c"], "end")]
    assert [group.key for group in groups] == [(1,), (2,), (1,)]


@pytest.mark.parametrize("mode", _MODES)
def test_gap_closes_forward_and_backward(mode: str) -> None:
    """An entry of the key more than gap_ms after or before the newest member."""
    entries = [
        _entry("a", us=_ms(1000)),
        _entry("b", us=_ms(1150)),  # 150 ms after a: a new group
        _entry("c", us=_ms(1000)),  # 150 ms before b: a new group
    ]

    groups = _spec(mode=mode).groups(entries)

    assert _summary(groups) == [(["a"], "gap"), (["b"], "gap"), (["c"], "end")]


def test_windowed_entry_of_another_key_expires_old_groups() -> None:
    entries = [
        _entry("a1", pid=1),
        _entry("b1", pid=2, us=_ms(50)),
        _entry("a2", pid=1, us=_ms(60)),
        _entry("c1", pid=3, us=_ms(155)),  # past b1 + 100 ms, not past a2 + 100 ms
        _entry("c2", pid=3, us=_ms(400)),  # past every other group
    ]

    groups = _spec(mode="windowed").groups(entries)

    assert _summary(groups) == [
        (["b1"], "gap"),
        (["a1", "a2"], "gap"),
        (["c1"], "gap"),
        (["c2"], "end"),
    ]


def test_windowed_never_closes_with_key() -> None:
    entries = [_entry(str(i), pid=i % 3, us=i) for i in range(30)]

    groups = list(_spec(mode="windowed").groups(entries))

    assert {group.reason for group in groups} == {"end"}
    assert sorted(len(group) for group in groups) == [10, 10, 10]


def test_windowed_older_entry_does_not_move_the_expiry_back() -> None:
    """D-11: expiry = max(old, ts + gap); 2.x lowered it to 950 + 100."""
    entries = [
        _entry("a1", pid=1, us=_ms(1000)),
        _entry("a0", pid=1, us=_ms(950)),  # older, within the gap: joins
        _entry("b", pid=2, us=_ms(1060)),  # before 1100: a stays open
        _entry("a2", pid=1, us=_ms(1090)),
    ]

    groups = _spec(mode="windowed").groups(entries)

    assert sorted(_summary(groups)) == [(["a1", "a0", "a2"], "end"), (["b"], "end")]


@pytest.mark.parametrize("mode", _MODES)
def test_size_closes_at_max_group_size(mode: str) -> None:
    entries = [_entry(str(i), us=i) for i in range(5)]

    groups = _spec(mode=mode, max_group_size=2).groups(entries)

    assert _summary(groups) == [
        (["0", "1"], "size"),
        (["2", "3"], "size"),
        (["4"], "end"),
    ]


@pytest.mark.parametrize("mode", _MODES)
def test_size_cap_applies_to_the_first_entry(mode: str) -> None:
    """F7-2: max_group_size=1 gives groups of one; 2.x LogGrouper gave pairs."""
    entries = [_entry(str(i), us=i) for i in range(3)]
    source = iter(entries)

    groups = _spec(mode=mode, max_group_size=1).groups(source)
    first = next(groups)

    assert _summary([first]) == [(["0"], "size")]
    assert next(source).message == "1"  # delivered before the next entry was read
    assert _summary(groups) == [(["2"], "size")]


def test_windowed_max_groups_evicts_the_least_recently_extended() -> None:
    entries = [
        _entry("a1", pid=1),
        _entry("b1", pid=2, us=1),
        _entry("a2", pid=1, us=2),  # a is now the most recent
        _entry("c1", pid=3, us=3),  # a third key: b goes
    ]

    groups = _spec(mode="windowed", max_groups=2).groups(entries)

    assert _summary(groups) == [
        (["b1"], "evicted"),
        (["a1", "a2"], "end"),
        (["c1"], "end"),
    ]


@pytest.mark.parametrize("mode", _MODES)
def test_max_total_entries_evicts(mode: str) -> None:
    entries = [_entry(str(i), pid=1, us=i) for i in range(5)]

    groups = _spec(mode=mode, max_total_entries=2).groups(entries)

    assert _summary(groups) == [
        (["0", "1"], "evicted"),
        (["2", "3"], "evicted"),
        (["4"], "end"),
    ]


def test_windowed_max_total_entries_evicts_lru_first() -> None:
    entries = [
        _entry("a1", pid=1),
        _entry("b1", pid=2, us=1),
        _entry("b2", pid=2, us=2),
        _entry("a2", pid=1, us=3),  # 4 open entries: the limit
        _entry("c1", pid=3, us=4),  # b was extended least recently
    ]

    groups = _spec(mode="windowed", max_total_entries=4).groups(entries)

    assert _summary(groups) == [
        (["b1", "b2"], "evicted"),
        (["a1", "a2"], "end"),
        (["c1"], "end"),
    ]


@pytest.mark.parametrize("mode", _MODES)
def test_max_total_bytes_evicts(mode: str) -> None:
    """Each entry counts len(raw) + 1 bytes: three 9-byte entries fit in 27."""
    entries = [_entry(f"line-{i:03d}", us=i) for i in range(4)]

    groups = _spec(mode=mode, max_total_bytes=27).groups(entries)

    assert _summary(groups) == [
        (["line-000", "line-001", "line-002"], "evicted"),
        (["line-003"], "end"),
    ]


@pytest.mark.parametrize("mode", _MODES)
def test_entry_larger_than_max_total_bytes_is_evicted_alone(mode: str) -> None:
    entries = [_entry("a"), _entry("x" * 50, us=1), _entry("b", us=2)]

    groups = _spec(mode=mode, max_total_bytes=10).groups(entries)

    assert _summary(groups) == [
        (["a"], "evicted"),
        (["x" * 50], "evicted"),
        (["b"], "end"),
    ]


@pytest.mark.parametrize("mode", _MODES)
def test_entry_larger_than_max_total_bytes_closes_in_its_own_add(mode: str) -> None:
    grouper = Grouper(_spec(mode=mode, max_total_bytes=10))
    grouper.add(_entry("a"), 0.0)

    closed = grouper.add(_entry("x" * 50, us=1), 0.0)

    assert _summary(closed) == [(["a"], "evicted"), (["x" * 50], "evicted")]
    assert (grouper.open_entries, grouper.open_bytes, grouper.open_groups) == (0, 0, 0)


def test_entry_larger_than_max_total_bytes_leaves_other_keys_open() -> None:
    """Windowed: only the entry's own key closes; the others fit without it."""
    grouper = Grouper(_spec(mode="windowed", max_total_bytes=100))
    for pid, message in [(1, "a1"), (2, "b1"), (3, "c1")]:
        grouper.add(_entry(message, pid=pid), 0.0)

    other = grouper.add(_entry("x" * 200, pid=4, us=1), 0.0)
    own = grouper.add(_entry("y" * 200, pid=2, us=2), 0.0)

    assert _summary(other) == [(["x" * 200], "evicted")]
    assert _summary(own) == [(["b1"], "evicted"), (["y" * 200], "evicted")]
    assert grouper.add(_entry("a2", pid=1, us=3), 0.0) == []
    assert _summary(grouper.flush()) == [(["c1"], "end"), (["a1", "a2"], "end")]


def test_entry_larger_than_max_total_bytes_at_max_group_size_one() -> None:
    groups = _spec(max_group_size=1, max_total_bytes=10).groups([_entry("x" * 50)])

    assert _summary(groups) == [(["x" * 50], "size")]


@pytest.mark.parametrize("mode", _MODES)
def test_open_groups_stay_within_the_totals(mode: str) -> None:
    spec = _spec(mode=mode, gap_ms=10_000.0, max_total_entries=50, max_total_bytes=400)
    grouper = Grouper(spec)
    delivered = 0

    for i in range(2000):
        entry = _entry("x" * (i % 13), pid=i % 7, us=i)
        delivered += sum(len(group) for group in grouper.add(entry, 0.0))
        assert grouper.open_entries <= 50
        assert grouper.open_bytes <= 400
        assert grouper.open_groups <= 7

    delivered += sum(len(group) for group in grouper.flush())
    assert delivered == 2000
    assert (grouper.open_entries, grouper.open_bytes, grouper.open_groups) == (0, 0, 0)


@pytest.mark.parametrize("mode", _MODES)
def test_end_closes_open_groups_lru_first(mode: str) -> None:
    grouper = Grouper(_spec(mode=mode, keys=()))
    grouper.add(_entry("a"), 0.0)

    assert _summary(grouper.flush()) == [(["a"], "end")]
    assert grouper.flush() == []


class _Reference:
    """The grouping rules written plainly: no heap, totals recounted each time."""

    def __init__(self, spec: GroupBy) -> None:
        self.spec = spec
        self.gap = gap_micros(spec.gap_ms)
        self.open: dict[tuple[Any, ...], list[LogEntry]] = {}  # LRU order

    @staticmethod
    def micros(entry: LogEntry) -> int:
        return (entry.timestamp - _T0) // timedelta(microseconds=1)

    def newest(self, entries: list[LogEntry]) -> int:
        return max(self.micros(entry) for entry in entries)

    def close(
        self, key: tuple[Any, ...], reason: str, out: list[tuple[Any, ...]]
    ) -> None:
        out.append((key, [entry.message for entry in self.open.pop(key)], reason))

    def add(self, entry: LogEntry) -> list[tuple[Any, ...]]:
        spec, out = self.spec, []
        ts, key = self.micros(entry), (entry.package,)
        if spec.mode == "windowed":
            for old in [
                k for k, g in self.open.items() if self.newest(g) + self.gap < ts
            ]:
                self.close(old, "gap", out)
        elif self.open and key not in self.open:
            self.close(next(iter(self.open)), "key", out)
        if key in self.open and abs(ts - self.newest(self.open[key])) > self.gap:
            self.close(key, "gap", out)
        if entry_bytes(entry) > spec.max_total_bytes:  # too large for any group
            if key in self.open:
                self.close(key, "evicted", out)
            reason = "size" if spec.max_group_size == 1 else "evicted"
            out.append((key, [entry.message], reason))
            return out
        while self.open and (
            sum(map(len, self.open.values())) >= spec.max_total_entries
            or sum(entry_bytes(e) for g in self.open.values() for e in g)
            + entry_bytes(entry)
            > spec.max_total_bytes
            or (key not in self.open and len(self.open) >= spec.max_groups)
        ):
            self.close(next(iter(self.open)), "evicted", out)
        self.open[key] = [*self.open.pop(key, []), entry]
        if len(self.open[key]) >= spec.max_group_size:
            self.close(key, "size", out)
        return out


def _random_entries(rng: random.Random, count: int) -> Iterator[LogEntry]:
    """Mostly forward with jumps and steps back; runs of one package or None.

    About one line in a hundred is larger than any byte budget the test draws.
    """
    us, package = 0, None
    for i in range(count):
        step = rng.choice([0, 0, 1, 50, 400, 2_500, 30_000, 250_000, -900, -5_000])
        us = max(0, us + step)
        if rng.random() < 0.4:
            package = rng.choice([None, "a", "b", "c"])
        size = 1000 if rng.random() < 0.01 else rng.randint(0, 30)
        yield _entry(str(i), us=us, package=package, raw="x" * size)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("seed", range(6))
def test_engine_matches_a_reference_model(mode: str, seed: int) -> None:
    """Seeded random input with steps back, None keys and small caps."""
    rng = random.Random(seed)  # noqa: S311  (seeded test input, not secrets)
    spec = _spec(
        keys=("package",),
        mode=mode,
        gap_ms=rng.choice([0.5, 3.0, 20.0, 200.0]),
        max_group_size=rng.randint(1, 30),
        max_groups=rng.randint(1, 4),
        max_total_entries=rng.randint(5, 80),
        max_total_bytes=rng.randint(20, 900),
    )
    grouper, reference = Grouper(spec), _Reference(spec)
    for i, entry in enumerate(_random_entries(rng, 1500)):
        closed = grouper.add(entry, 0.0)
        got = [
            (group.key, [e.message for e in group], group.reason) for group in closed
        ]
        expected = reference.add(entry)
        assert sorted(got, key=repr) == sorted(expected, key=repr), i
        assert [g for g in got if g[2] != "gap"] == [
            g for g in expected if g[2] != "gap"
        ]
        # The groups an entry expires close in expiry order (ties: heap order).
        expired = [
            max(map(reference.micros, group))
            for group in closed
            if group.reason == "gap" and group.key != (entry.package,)
        ]
        assert expired == sorted(expired), i
        assert grouper.open_entries == sum(map(len, reference.open.values()))
        assert grouper.open_bytes == sum(
            entry_bytes(e) for g in reference.open.values() for e in g
        )


# -- F7-1: unorderable keys never compare -----------------------------------------


@pytest.mark.parametrize(
    ("keys", "values"),
    [(("uid",), {"uid": [None, 10123]}), (("package",), {"package": [None, "com.x"]})],
    ids=["uid", "package"],
)
def test_equal_expiries_with_unorderable_keys(
    keys: tuple[GroupKey, ...], values: dict[str, list[Any]]
) -> None:
    """Heap entries are (expiry_us, seq, key): equal expiries compare seq."""
    ((field, choices),) = values.items()
    entries = [
        _entry(str(i), us=(i // 8) * 1000, **{field: choices[i % 2]})
        for i in range(400)
    ]
    spec = GroupBy(keys=keys, gap_ms=0.5, mode="windowed", max_group_size=3)

    groups = list(spec.groups(entries))

    assert sum(len(group) for group in groups) == 400
    assert {group.reason for group in groups} == {"gap", "size", "end"}


def _assert_heap_bound(grouper: Grouper) -> None:
    """Between calls: at most twice the open groups plus 64, none if none open."""
    open_groups = grouper.open_groups
    assert len(grouper._heap) <= (2 * open_groups + 64 if open_groups else 0)


def test_windowed_heap_stays_bounded_with_closed_groups() -> None:
    """Groups closed by size leave heap entries behind; they are compacted."""
    grouper = Grouper(_spec(mode="windowed", gap_ms=3_600_000.0, max_group_size=1))

    for i in range(5000):
        grouper.add(_entry(str(i), pid=i % 5, us=i), 0.0)
        _assert_heap_bound(grouper)


def test_windowed_expiry_survives_heap_compaction() -> None:
    """A group still expires by gap after its heap entry was rebuilt."""
    grouper = Grouper(_spec(mode="windowed", gap_ms=10_000.0, max_group_size=2))
    grouper.add(_entry("cold", pid=1), 0.0)
    for i in range(400):  # 200 size closes leave 200 heap entries behind
        grouper.add(_entry(f"hot{i}", pid=2, us=1 + i), 0.0)
        _assert_heap_bound(grouper)

    closed = grouper.add(_entry("late", pid=3, us=_ms(20_000)), 0.0)

    assert _summary(closed) == [(["cold"], "gap")]


@pytest.mark.parametrize("kept", [0, 1], ids=["all", "most"])
def test_windowed_heap_is_compacted_after_idle_closures(kept: int) -> None:
    """flush_idle closing 1000 groups, all or all but one, leaves no stale entry.

    The kept group still expires by gap from its rebuilt heap entry.
    """
    grouper = Grouper(_spec(mode="windowed", gap_ms=3_600_000.0, idle_flush_ms=100.0))
    for i in range(1000):
        grouper.add(_entry(str(i), pid=i, us=i), 0.0)
    for i in range(kept):
        grouper.add(_entry("again", pid=i, us=1000), 0.5)  # extended, not pushed

    closed = grouper.flush_idle(0.5)

    assert len(closed) == 1000 - kept
    assert grouper.open_groups == kept
    _assert_heap_bound(grouper)
    if kept:
        late = _entry("late", pid=5000, us=_ms(3_600_000) + 1001)
        assert _summary(grouper.add(late, 0.5)) == [(["0", "again"], "gap")]


def test_windowed_heap_is_compacted_after_evictions() -> None:
    """One entry evicting 999 groups by bytes, then joining its own, is no push."""
    grouper = Grouper(_spec(mode="windowed", gap_ms=3_600_000.0, max_total_bytes=2100))
    for i in range(1000):
        grouper.add(_entry("a", pid=i, us=i), 0.0)  # 2 bytes each

    closed = grouper.add(_entry("b", pid=999, us=1000, raw="x" * 2097), 0.0)

    assert [group.reason for group in closed] == ["evicted"] * 999
    assert (grouper.open_groups, grouper.open_bytes) == (1, 2100)
    _assert_heap_bound(grouper)
    assert len(grouper._heap) == 1


# -- D-10: idle flush on the host-monotonic clock ----------------------------------


def test_flush_idle_closes_after_idle_flush_ms_inclusive() -> None:
    grouper = Grouper(_spec(idle_flush_ms=250.0))
    grouper.add(_entry("a"), 10.0)

    assert grouper.idle_deadline() == pytest.approx(10.25)
    assert grouper.flush_idle(10.249) == []
    assert _summary(grouper.flush_idle(10.25)) == [(["a"], "idle")]
    assert grouper.idle_deadline() is None
    assert grouper.flush_idle(99.0) == []


def test_idle_flush_ms_gap_uses_gap_ms() -> None:
    grouper = Grouper(_spec(gap_ms=40.0))
    grouper.add(_entry("a"), 1.0)

    assert grouper.idle_deadline() == pytest.approx(1.04)
    assert _summary(grouper.flush_idle(1.04)) == [(["a"], "idle")]


def test_idle_flush_ms_none_never_closes() -> None:
    grouper = Grouper(_spec(idle_flush_ms=None))
    grouper.add(_entry("a"), 0.0)

    assert grouper.idle_deadline() is None
    assert grouper.flush_idle(1e9) == []
    assert _summary(grouper.flush()) == [(["a"], "end")]


def test_windowed_flush_idle_closes_only_idle_groups_in_order() -> None:
    grouper = Grouper(_spec(mode="windowed", gap_ms=10_000.0, idle_flush_ms=100.0))
    grouper.add(_entry("a1", pid=1), 0.0)
    grouper.add(_entry("b1", pid=2), 0.05)
    grouper.add(_entry("c1", pid=3), 0.08)
    grouper.add(_entry("a2", pid=1), 0.09)  # a is the most recent now

    assert grouper.idle_deadline() == pytest.approx(0.15)
    assert _summary(grouper.flush_idle(0.185)) == [(["b1"], "idle"), (["c1"], "idle")]
    assert grouper.idle_deadline() == pytest.approx(0.19)
    assert _summary(grouper.flush_idle(0.2)) == [(["a1", "a2"], "idle")]


def test_gap_reads_timestamps_and_idle_reads_the_host_clock() -> None:
    """Hours of host time between entries do not split a group; idle does."""
    grouper = Grouper(_spec(idle_flush_ms=None))
    grouper.add(_entry("a"), 0.0)
    assert grouper.add(_entry("b", us=1), 7200.0) == []

    idle = Grouper(_spec(idle_flush_ms=1000.0))
    idle.add(_entry("a"), 0.0)
    assert idle.add(_entry("b", us=_ms(10_000)), 0.0) != []  # a 10 s timestamp gap
    assert idle.idle_deadline() == pytest.approx(1.0)


def test_host_clock_going_back_counts_as_the_last_value() -> None:
    grouper = Grouper(_spec(idle_flush_ms=1000.0))
    grouper.add(_entry("a"), 5.0)
    grouper.add(_entry("b", us=1), 3.0)

    assert grouper.idle_deadline() == pytest.approx(6.0)
    assert grouper.flush_idle(4.0) == []
    assert _summary(grouper.flush_idle(6.0)) == [(["a", "b"], "idle")]


def test_groups_never_closes_idle() -> None:
    spec = _spec(idle_flush_ms=0.001)
    entries = [_entry(str(i), us=i) for i in range(3)]

    assert _summary(spec.groups(entries)) == [(["0", "1", "2"], "end")]


# -- GroupBy.groups ------------------------------------------------------------------


def test_groups_is_lazy_and_starts_afresh() -> None:
    pulled: list[str] = []
    entries = [_entry("a"), _entry("b", us=_ms(500)), _entry("c", us=_ms(501))]

    def source() -> Iterator[LogEntry]:
        for entry in entries:
            pulled.append(entry.message)
            yield entry

    spec = _spec()
    groups = spec.groups(source())

    assert pulled == []
    assert _summary([next(groups)]) == [(["a"], "gap")]
    assert pulled == ["a", "b"]
    assert _summary(groups) == [(["b", "c"], "end")]
    assert _summary(spec.groups(entries)) == _summary(spec.groups(entries))


def test_groups_rejects_an_item_that_is_not_an_entry() -> None:
    items: list[Any] = [_entry("a"), "02-01 03:04:05.678 1 1 I T: x"]

    with pytest.raises(TypeError, match="LogEntry"):
        list(_spec().groups(items))


def test_key_follows_the_order_of_keys() -> None:
    (group,) = GroupBy(keys=("tag", "pid"), gap_ms=1.0).groups([_entry(pid=7)])

    assert group.key == ("T", 7)


# -- LogGroup ------------------------------------------------------------------------


def test_group_is_a_sequence_of_its_entries() -> None:
    """The 3.0 form of 2.x emit_mode='entry': a group flattens by iteration."""
    a, b, c = _entry("a"), _entry("b", us=1), _entry("c", us=2)
    group = LogGroup(key=(1,), entries=(a, b, c), reason="end")

    assert isinstance(group, Sequence)
    assert len(group) == 3
    assert list(group) == [a, b, c]
    assert [entry for item in [group] for entry in item] == [a, b, c]
    assert group[0] is a
    assert group[-1] is c
    assert group[1:] == (b, c)
    assert list(reversed(group)) == [c, b, a]
    assert b in group
    assert _entry("b", us=1) in group  # equal to b, another object
    assert _entry("other") not in group
    assert group.index(b) == 1
    assert group.count(a) == 1


def test_group_is_frozen_comparable_and_unhashable() -> None:
    a = _entry("a")
    group = LogGroup(key=(1,), entries=(a,), reason="gap")

    assert group == LogGroup(key=(1,), entries=(a,), reason="gap")
    assert group != LogGroup(key=(1,), entries=(a,), reason="end")
    with pytest.raises(dataclasses.FrozenInstanceError):
        group.reason = "end"  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError, match="unhashable"):
        hash(group)
    assert LogGroup.__hash__ is None  # not only because LogEntry is unhashable
    assert not isinstance(group, Hashable)
    assert not hasattr(group, "__dict__")


@pytest.mark.parametrize(
    ("changes", "error", "match"),
    [
        ({"entries": []}, TypeError, "entries must be a tuple"),
        ({"entries": ()}, ValueError, "at least one"),
        ({"entries": (42,)}, TypeError, "LogEntry, not int"),
        ({"entries": ("x", 42)}, TypeError, "LogEntry, not str"),
        ({"key": [1]}, TypeError, "key must be a tuple"),
        ({"key": ([],)}, TypeError, "hashable"),
        ({"key": ((1, {}),)}, TypeError, "hashable"),
        ({"reason": "timeout"}, ValueError, "one of"),
        ({"reason": 1}, TypeError, "str, not int"),
        ({"reason": None}, TypeError, "str, not NoneType"),
        ({"entries": (), "reason": 1}, TypeError, "reason"),  # types first
    ],
    ids=[
        "list",
        "empty",
        "member",
        "members",
        "key-list",
        "key-list-item",
        "key-nested",
        "reason",
        "reason-int",
        "reason-none",
        "types-first",
    ],
)
def test_group_checks_its_fields(
    changes: dict[str, Any], error: type[Exception], match: str
) -> None:
    """Design 4.2: TypeError for a type, ValueError for a value."""
    fields: dict[str, Any] = {"key": (1,), "entries": (_entry(),), "reason": "end"}

    with pytest.raises(error, match=match) as caught:
        LogGroup(**{**fields, **changes})

    assert type(caught.value) is error


def test_entry_bytes_counts_the_utf8_line_and_its_terminator() -> None:
    assert entry_bytes(_entry(raw="")) == 1
    assert entry_bytes(_entry(raw="abc")) == 4
    assert entry_bytes(_entry(raw="日本")) == 7
    assert entry_bytes(_entry(raw="\ud800")) == 4  # a lone surrogate does not raise


def test_exports() -> None:
    """Design 2.1: GroupBy and LogGroup are public; the 2.x groupers are gone."""
    assert {"GroupBy", "LogGroup"} <= set(logpyt.__all__)
    assert {"LogGrouper", "WindowedLogGrouper"}.isdisjoint(logpyt.__all__)
    assert logpyt.GroupBy is groupers.GroupBy
    assert logpyt.LogGroup is models.LogGroup is group_module.LogGroup
    assert sorted(groupers.__all__) == ["GroupBy", "GroupKey"]
    assert sorted(models.__all__) == ["LogEntry", "LogGroup", "LogLevel"]
    assert sorted(group_module.__all__) == ["GroupReason", "LogGroup"]
    assert sorted(grouping.__all__) == ["Grouper", "entry_bytes", "gap_micros"]
    for name in ("LogGrouper", "WindowedLogGrouper", "EmitMode"):
        assert not hasattr(groupers, name)
