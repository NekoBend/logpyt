"""Tests for log filters: the conditions of design section 2.7."""

import contextlib
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import logpyt
from logpyt import filters
from logpyt.filters import (
    And,
    AnrCondition,
    Condition,
    CrashCondition,
    Filter,
    Level,
    MessageContains,
    MinLevel,
    Not,
    Or,
    Package,
    Tag,
    Where,
)
from logpyt.models import LogEntry
from logpyt.readers import read_file
from logpyt.streams import AsyncLogStream, LogStream
from logpyt.streams.common import build_pidof_command

# Design 2.1: the condition names of the public surface.
CONDITION_NAMES = [
    "And",
    "AnrCondition",
    "Condition",
    "CrashCondition",
    "Filter",
    "Level",
    "MessageContains",
    "MinLevel",
    "Not",
    "Or",
    "Package",
    "Tag",
    "Where",
]


@pytest.fixture
def sample_entry() -> LogEntry:
    """Provide a sample log entry."""
    return LogEntry(
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        pid=123,
        tid=456,
        level="D",
        tag="MyApp",
        message="Something happened",
        raw="raw",
        package="com.example.app",
    )


def _entry(
    *, tag: str = "T", level: str = "E", message: str = "m", package: str | None = None
) -> LogEntry:
    return LogEntry(
        timestamp=datetime(2026, 11, 4, tzinfo=UTC),
        pid=1,
        tid=1,
        level=level,  # ty: ignore[invalid-argument-type]
        tag=tag,
        message=message,
        raw=message,
        package=package,
    )


# -- 2.x behavior, ported to calling the condition ------------------------------------


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
    """Conditions combined with &, | and ~ filter directly (2.x: AdvancedFilter)."""
    c1 = Tag("MyApp")
    c2 = Level("E")
    c3 = MessageContains("happened")

    # (Tag=MyApp AND MessageContains=happened) OR Level=E
    # Should match because first part matches
    f = (c1 & c3) | c2
    assert f(sample_entry) is True

    # (Tag=MyApp AND Level=E)
    # Should fail
    f2 = c1 & c2
    assert f2(sample_entry) is False

    # NOT Tag=MyApp
    # Should fail
    f3 = ~c1
    assert f3(sample_entry) is False


def test_package_condition(sample_entry: LogEntry) -> None:
    """Test Package condition."""
    c = Package("com.example.app")
    assert c(sample_entry) is True

    c2 = Package("com.other.app")
    assert c2(sample_entry) is False


def test_tag_condition(sample_entry: LogEntry) -> None:
    """Test Tag condition."""
    c = Tag("MyApp")
    assert c(sample_entry) is True


def test_level_condition(sample_entry: LogEntry) -> None:
    """Test Level condition."""
    c = Level("D")
    assert c(sample_entry) is True


def test_message_contains_condition(sample_entry: LogEntry) -> None:
    """Test MessageContains condition."""
    c = MessageContains(["Something", "Nothing"])
    assert c(sample_entry) is True  # "Something" is in message

    c2 = MessageContains("Error")
    assert c2(sample_entry) is False


def test_message_contains_is_case_sensitive(sample_entry: LogEntry) -> None:
    """MessageContains should remain case-sensitive."""
    c = MessageContains("something")
    assert c(sample_entry) is False


def test_message_contains_treats_regex_chars_as_literals(
    sample_entry: LogEntry,
) -> None:
    """Regex meta characters in pattern should be matched literally."""
    entry = sample_entry.model_copy(update={"message": "a.b[c](d){e}?"})
    c = MessageContains("a.b[c](d){e}?")
    assert c(entry) is True


def test_message_contains_empty_pattern_still_matches(sample_entry: LogEntry) -> None:
    """Empty pattern should preserve Python substring semantics."""
    c = MessageContains("")
    assert c(sample_entry) is True


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
    assert CrashCondition()(crash_entry) is True

    # Mismatch - wrong tag
    no_crash = sample_entry.model_copy(
        update={
            "tag": "MyApp",
            "level": "E",
            "message": "FATAL EXCEPTION: main",
        }
    )
    assert CrashCondition()(no_crash) is False

    # Mismatch - wrong level
    no_crash_level = sample_entry.model_copy(
        update={
            "tag": "AndroidRuntime",
            "level": "D",
            "message": "FATAL EXCEPTION: main",
        }
    )
    assert CrashCondition()(no_crash_level) is False

    # Mismatch - wrong message
    no_crash_msg = sample_entry.model_copy(
        update={
            "tag": "AndroidRuntime",
            "level": "E",
            "message": "Just an error",
        }
    )
    assert CrashCondition()(no_crash_msg) is False


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
    assert AnrCondition()(anr_entry) is True

    # Mismatch - wrong tag
    no_anr = sample_entry.model_copy(
        update={
            "tag": "MyApp",
            "level": "E",
            "message": "ANR in com.example.app",
        }
    )
    assert AnrCondition()(no_anr) is False

    # Mismatch - wrong level
    no_anr_level = sample_entry.model_copy(
        update={
            "tag": "ActivityManager",
            "level": "I",
            "message": "ANR in com.example.app",
        }
    )
    assert AnrCondition()(no_anr_level) is False

    # Mismatch - wrong message (must start with "ANR in")
    no_anr_msg = sample_entry.model_copy(
        update={
            "tag": "ActivityManager",
            "level": "E",
            "message": "Something about ANR",
        }
    )
    assert AnrCondition()(no_anr_msg) is False


# -- the Condition type ---------------------------------------------------------------


def test_names_are_exported() -> None:
    """logpyt.filters and logpyt export design 2.1's condition names; AdvancedFilter
    is gone (any condition is a filter).
    """
    assert sorted(filters.__all__) == CONDITION_NAMES
    for name in CONDITION_NAMES:
        assert name in logpyt.__all__
        assert getattr(logpyt, name) is getattr(filters, name)
    assert "AdvancedFilter" not in logpyt.__all__
    assert not hasattr(filters, "AdvancedFilter")


def test_every_condition_is_a_condition(sample_entry: LogEntry) -> None:
    """Every class is a Condition, called with an entry for a bool."""
    conditions: list[Condition] = [
        Tag("MyApp"),
        Package("com.example.app"),
        Level("D"),
        MinLevel("D"),
        MessageContains("Something"),
        CrashCondition(),
        AnrCondition(),
        Where(lambda entry: entry.pid == 123),
        Tag("MyApp") & Level("D"),
        Tag("MyApp") | Level("D"),
        ~Tag("MyApp"),
        Filter(tag="MyApp"),
    ]
    for condition in conditions:
        assert isinstance(condition, Condition)
        assert condition(sample_entry) in {True, False}
    assert isinstance(Filter(), And)


def test_custom_condition_implements_call(sample_entry: LogEntry) -> None:
    """A subclass implements __call__ and combines like the built-in conditions; a
    2.x subclass that only defines check() cannot be instantiated.
    """

    class Odd(Condition):
        def __call__(self, entry: LogEntry) -> bool:
            return entry.pid % 2 == 1

    class Legacy(Condition):
        def check(self, entry: LogEntry) -> bool:
            return entry.pid > 0

    assert (Odd() & Tag("MyApp"))(sample_entry) is True
    assert (~Odd() | Tag("Other"))(sample_entry) is False
    with pytest.raises(TypeError, match="abstract"):
        Legacy()


def test_overriding_combination_subclasses_are_called() -> None:
    """A subclass of And, Or or Not that overrides __call__ is called wherever it is
    nested and is never flattened into its parent; one that does not override it
    is taken apart like the built-in.
    """
    entry = _entry(tag="a")
    a, b, c, x, y, z = Tag("a"), Tag("b"), Tag("c"), Tag("x"), Tag("y"), Tag("z")
    calls: list[str] = []

    class Never(Not):
        __slots__ = ()

        def __call__(self, entry: LogEntry, /) -> bool:
            del entry
            return False

    class AlwaysOr(Or):
        __slots__ = ()

        def __call__(self, entry: LogEntry, /) -> bool:
            del entry
            return True

    class Logged(And):
        __slots__ = ()

        def __call__(self, entry: LogEntry, /) -> bool:
            calls.append("logged")
            return super().__call__(entry)

    class Plain(And):
        __slots__ = ()

    never = Never(b)
    assert never(entry) is False
    assert (x | never)(entry) is False
    assert (a & never)(entry) is False
    assert ((x & y) | never)(entry) is False
    assert (a & ~never)(entry) is True

    always = AlwaysOr(x, y)
    assert Or(always, z).conditions == (always, z)
    assert (a & always)(entry) is True
    assert (a & ~(x | always))(entry) is False

    logged = Logged(a, a | b)
    assert And(logged, a).conditions == (logged, a)
    assert (x | And(logged, a))(entry) is True
    assert calls == ["logged"]
    assert logged(entry) is True
    assert calls == ["logged", "logged"]

    assert And(a, Plain(b, c)).conditions == (a, b, c)


def test_conditions_have_no_check_method() -> None:
    """``check()`` of 2.x is gone: a condition is called."""
    assert not hasattr(Tag("a"), "check")
    assert not hasattr(Filter(tag="a"), "check")


# -- combinations ---------------------------------------------------------------------


def test_and_or_flatten_same_type_children() -> None:
    """And absorbs And children (Filter included), Or absorbs Or children; a child
    of the other kind stays one child.
    """
    a, b, c, d = Tag("a"), Tag("b"), Tag("c"), Tag("d")

    assert (a & b & c & d).conditions == (a, b, c, d)
    assert (a | b | c | d).conditions == (a, b, c, d)
    assert And(a, And(b, c), d).conditions == (a, b, c, d)
    assert Or(Or(a, b), Or(c, d)).conditions == (a, b, c, d)

    mixed = (a & b) | (c & d)
    assert type(mixed) is Or
    assert [type(child) for child in mixed.conditions] == [And, And]

    combined = Filter(tag="x", level="E") & d
    assert type(combined) is And
    assert [type(child) for child in combined.conditions] == [Tag, Level, Tag]

    assert type(~a & b) is And
    assert (~a & b).conditions[1] is b


def test_and_or_short_circuit_from_left_to_right(sample_entry: LogEntry) -> None:
    """And stops at the first False and Or at the first True, flat or nested."""
    calls: list[str] = []

    def probe(name: str, *, verdict: bool) -> Where:
        def check(entry: LogEntry) -> bool:
            del entry
            calls.append(name)
            return verdict

        return Where(check)

    def run(condition: Condition) -> tuple[bool, list[str]]:
        calls.clear()
        return condition(sample_entry), list(calls)

    t = probe
    assert run(t("a", verdict=True) & t("b", verdict=False) & t("c", verdict=True)) == (
        False,
        ["a", "b"],
    )
    assert run(t("a", verdict=False) | t("b", verdict=True) | t("c", verdict=True)) == (
        True,
        ["a", "b"],
    )
    nested = (
        (t("a", verdict=False) & t("b", verdict=True))
        | (t("c", verdict=True) & ~t("d", verdict=False))
        | t("e", verdict=True)
    )
    assert run(nested) == (True, ["a", "c", "d"])
    assert run(~(t("a", verdict=True) | t("b", verdict=True))) == (False, ["a"])
    assert run(Not(Not(t("a", verdict=True)))) == (True, ["a"])


def test_empty_and_or_raise() -> None:
    """And() and Or() without conditions raise; Filter() is the empty And."""
    with pytest.raises(ValueError, match="at least one condition"):
        And()
    with pytest.raises(ValueError, match="at least one condition"):
        Or()
    assert Filter().conditions == ()


def test_combinations_take_only_conditions() -> None:
    """A bare function or another object is not a condition: wrap it in Where."""

    def is_error(entry: LogEntry) -> bool:
        return entry.level == "E"

    with pytest.raises(TypeError, match=r"Where\(fn\)"):
        And(Tag("a"), is_error)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="not int"):
        Or(Tag("a"), 1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="must be a Condition"):
        Not("a")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        Tag("a") & is_error  # ty: ignore[unsupported-operator]
    with pytest.raises(TypeError):
        is_error | Tag("a")  # ty: ignore[unsupported-operator]
    assert (Tag("a") & Where(is_error))(_entry(tag="a")) is True


def _python_depth() -> int:
    frame = sys._getframe()
    depth = 0
    while frame is not None:
        depth += 1
        frame = frame.f_back
    return depth


@contextlib.contextmanager
def _recursion_headroom(frames: int) -> Iterator[None]:
    """Allow only ``frames`` more Python frames than the caller has."""
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(_python_depth() + frames)
    try:
        yield
    finally:
        sys.setrecursionlimit(limit)


def test_10000_alternating_and_or_terms_do_not_recurse() -> None:
    """A left-nested chain of 10000 alternating & and | terms (no flattening is
    possible) is built, evaluated, walked by packages() and printed with 100
    frames to spare: none of them recurses (F7-4).
    """
    terms: list[Condition] = []
    for i in range(10_001):
        kind = (i * i + i // 7) % 4  # a fixed, irregular mix
        if kind == 0:
            terms.append(Tag("a"))
        elif kind == 1:
            terms.append(Tag("b"))
        elif kind == 2:
            terms.append(~Tag("a"))
        else:
            terms.append(Package(f"com.example.p{i}"))
    names = {
        name for term in terms if isinstance(term, Package) for name in term.values
    }

    entries = [_entry(tag="a"), _entry(tag="b"), _entry(tag="c")]
    expected = []
    for entry in entries:
        verdict = terms[0](entry)
        for i, term in enumerate(terms[1:]):
            verdict = (
                (verdict and term(entry)) if i % 2 == 0 else (verdict or term(entry))
            )
        expected.append(verdict)
    assert set(expected) == {True, False}, "the chain must yield both verdicts"

    with _recursion_headroom(100):
        condition = terms[0]
        for i, term in enumerate(terms[1:]):
            condition = (condition & term) if i % 2 == 0 else (condition | term)
        verdicts = [condition(entry) for entry in entries]
        packages = condition.packages()
        text = repr(condition)
        negated = condition
        for _ in range(10_000):
            negated = ~negated
        negated_verdicts = [negated(entry) for entry in entries]
        negated_text = repr(negated)

    assert verdicts == expected
    assert packages == names
    assert text.startswith("Or(And(Or(And(")
    assert text.count("Package(") == len(names)
    assert negated_verdicts == expected  # an even number of ~
    assert negated_text.startswith("Not(Not(Not(")
    assert negated_text.endswith(")" * 10_001)


def test_same_operator_chain_of_10000_is_one_junction() -> None:
    """``a & b & ...`` with 10000 operators is one And of 10001 conditions."""
    condition: Condition = Tag("t0")
    for i in range(1, 10_001):
        condition &= Tag(f"t{i}")
    assert isinstance(condition, And)
    assert len(condition.conditions) == 10_001
    assert condition(_entry(tag="t0")) is False


def _tag_is_b(entry: LogEntry) -> bool:
    return entry.tag == "b"


# A step of a chain: ("&", term) or ("|", term) combines the chain with a term,
# ("~", None) negates it and ("where", None) wraps it in Where.
type _Step = tuple[str, Condition | None]


def _mixed_steps() -> list[_Step]:
    """10000 steps of a fixed, irregular mix; one term is a Where of a function."""
    steps: list[_Step] = []
    for i in range(10_000):
        kind = (i * i + i // 7) % 6
        if kind == 0:
            steps.append(("&", Tag("a")))
        elif kind == 1:
            steps.append(("|", Tag("b")))
        elif kind == 2:
            steps.append(("~", None))
        elif kind == 3:
            steps.append(("|", Package(f"com.example.p{i}")))
        elif kind == 4:
            steps.append(("&", Where(_tag_is_b)))  # a plain function: a leaf
        else:
            steps.append(("where", None))
    return steps


def _chain(start: Condition, steps: list[_Step]) -> Condition:
    """The condition that ``steps`` build on ``start``, one operator at a time."""
    chain = start
    for kind, term in steps:
        if kind == "~":
            chain = ~chain
        elif kind == "where":
            chain = Where(chain)
        elif kind == "&" and term is not None:
            chain &= term
        elif term is not None:
            chain |= term
    return chain


def _chain_verdict(start: Condition, steps: list[_Step], entry: LogEntry) -> bool:
    """What ``_chain(start, steps)`` must answer for ``entry``, step by step."""
    verdict = start(entry)
    for kind, term in steps:
        if kind == "~":
            verdict = not verdict
        elif kind == "&" and term is not None:
            verdict = verdict and term(entry)
        elif kind == "|" and term is not None:
            verdict = verdict or term(entry)
    return verdict


def test_10000_nested_where_do_not_recurse() -> None:
    """10000 Where(...) around a condition, and a chain of 10000 &, |, ~ and Where
    steps, are built, evaluated, walked by packages() and printed with 100 frames
    to spare (F7-4); Where(c) matches like c.
    """
    entries = [
        _entry(tag="a"),
        _entry(tag="b"),
        _entry(tag="c"),
        _entry(tag="c", package="com.a"),
    ]
    steps = _mixed_steps()
    names = {
        name for _, term in steps if isinstance(term, Package) for name in term.values
    }
    wheres = sum(kind == "where" or isinstance(term, Where) for kind, term in steps)
    expected = [_chain_verdict(Tag("a"), steps, entry) for entry in entries]
    assert set(expected) == {True, False}, "the chain must yield both verdicts"

    with _recursion_headroom(100):
        nested: Condition = Package("com.a") | Tag("a")
        for _ in range(10_000):
            nested = Where(nested)
        nested_verdicts = [nested(entry) for entry in entries]
        nested_packages = nested.packages()
        nested_text = repr(nested)

        chain = _chain(Tag("a"), steps)
        verdicts = [chain(entry) for entry in entries]
        packages = chain.packages()
        text = repr(chain)

    assert nested_verdicts == [True, False, False, True]
    assert nested_packages == frozenset({"com.a"})
    assert nested_text == (
        "Where(" * 10_000 + "Or(Package('com.a'), Tag('a'))" + ")" * 10_000
    )
    assert verdicts == expected
    assert packages == names
    assert text.count("Where(") == wheres
    assert text.count("Package(") == len(names)


def test_where_of_a_leaf_is_a_plain_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Where of a leaf condition, a function or a flat Or, alone or in a flat And, is
    evaluated by plain calls, as cheaply as before Where nodes nested without
    recursion; a Where of a Where, alone or in an Or, takes the tree walk.
    """
    walked: list[Condition] = []
    evaluate = filters._evaluate

    def walk(root: Condition, entry: LogEntry) -> bool:
        walked.append(root)
        return evaluate(root, entry)

    monkeypatch.setattr(filters, "_evaluate", walk)
    entry, a, b = _entry(tag="a"), Tag("a"), Tag("b")
    assert Where(a)(entry) is True
    assert Where(_tag_is_b)(entry) is False
    assert Where(a | b)(entry) is True
    assert (a & Where(a) & Where(_tag_is_b))(entry) is False
    assert walked == []

    twice = Where(Where(a))
    either = Tag("c") | Where(Where(a))
    assert twice(entry) is True
    assert either(entry) is True
    assert walked == [twice, either]


def test_overrides_of_call_and_repr_are_not_taken_apart() -> None:
    """A Where subclass that overrides __call__ is called, at any depth; an And, Or,
    Not or Where subclass that overrides __repr__ prints itself when nested, unless
    an And or Or of its own kind absorbs it, and one whose override calls
    super().__repr__() prints its own tree.
    """
    entry = _entry(tag="a")

    class Inverted(Where):
        __slots__ = ()

        def __call__(self, entry: LogEntry, /) -> bool:
            return not super().__call__(entry)

    inverted = Inverted(Tag("a"))
    assert inverted(entry) is False
    assert (inverted | Tag("b"))(entry) is False
    assert Where(Where(inverted))(entry) is False
    assert (~Where(inverted) & Tag("a"))(entry) is True
    assert Inverted(Inverted(Where(inverted)))(entry) is False

    class Named(Where):
        __slots__ = ()

        def __repr__(self) -> str:
            return "<named>"

    class Short(And):
        __slots__ = ()

        def __repr__(self) -> str:
            return f"Short({len(self.conditions)})"

    class Bare(Not):
        __slots__ = ()

        def __repr__(self) -> str:
            return "Bare"

    class Marked(Or):
        __slots__ = ()

        def __repr__(self) -> str:
            return "!" + super().__repr__()

    assert repr(Named(Tag("a")) | Tag("b")) == "Or(<named>, Tag('b'))"
    assert repr(Where(Named(Tag("a")))) == "Where(<named>)"
    assert repr(~Short(Tag("a"), Tag("b"))) == "Not(Short(2))"
    # an And of its own kind absorbs it, so its __repr__ is not asked
    assert (
        repr(Short(Tag("a"), Tag("b")) & Tag("c"))
        == "And(Tag('a'), Tag('b'), Tag('c'))"
    )
    assert repr(Where(Bare(Tag("a"))) & Tag("b")) == "And(Where(Bare), Tag('b'))"
    assert repr(Marked(Tag("a"), Where(~Tag("b")))) == (
        "!Marked(Tag('a'), Where(Not(Tag('b'))))"
    )
    assert repr(Where(Filter(tag="a"))) == "Where(Filter(tag='a'))"


# -- packages() -----------------------------------------------------------------------


class _App(Condition):
    """A condition of a user's own: it wraps a Package where the walk cannot see it,
    so it names the package in packages().
    """

    __slots__ = ("_inner",)

    def __init__(self, name: str) -> None:
        self._inner = Package(name)

    def __call__(self, entry: LogEntry, /) -> bool:
        return self._inner(entry)

    def packages(self) -> frozenset[str]:
        return self._inner.values


class _WithLauncher(And):
    """An And that also needs the launcher's lines attributed."""

    __slots__ = ()

    def packages(self) -> frozenset[str]:
        return super().packages() | {"com.launcher"}


def test_packages_takes_the_override_of_a_nested_condition() -> None:
    """For a condition anywhere in the tree whose class overrides packages(), under &,
    |, ~ and Where alike, packages() takes what the override returns and does not
    look inside it; an override may add to super().packages().
    """
    app = _App("com.a")
    assert app.packages() == frozenset({"com.a"})
    assert (app & Tag("x")).packages() == frozenset({"com.a"})
    assert (Tag("x") | app).packages() == frozenset({"com.a"})
    assert (~app).packages() == frozenset({"com.a"})
    assert Where(app).packages() == frozenset({"com.a"})
    assert (Where(~app & Tag("x")) | Package("com.b")).packages() == frozenset(
        {"com.a", "com.b"}
    )

    class FirstOnly(Or):
        __slots__ = ()

        def packages(self) -> frozenset[str]:
            return self.conditions[0].packages()

    first = FirstOnly(Package("com.a"), Package("com.b"))
    assert first.packages() == frozenset({"com.a"})
    assert (first & Tag("x")).packages() == frozenset({"com.a"})
    assert Where(~first).packages() == frozenset({"com.a"})

    launcher = _WithLauncher(Package("com.a"), Tag("x"))
    assert launcher.packages() == frozenset({"com.a", "com.launcher"})
    assert (Tag("y") | ~launcher).packages() == frozenset({"com.a", "com.launcher"})


def test_combinations_keep_children_that_override_packages() -> None:
    """An And or Or child whose class overrides packages() stays one child instead of
    contributing its conditions, so its override still answers; it matches like the
    built-in.
    """
    a, x, error = Package("com.a"), Tag("x"), Level("E")
    launcher = _WithLauncher(a, x)
    assert (launcher & error).conditions == (launcher, error)
    assert And(error, launcher).conditions == (error, launcher)
    assert (launcher & error).packages() == frozenset({"com.a", "com.launcher"})
    assert (launcher & error)(_entry(tag="x", package="com.a")) is True
    assert (launcher & error)(_entry(tag="y", package="com.a")) is False

    class OrLauncher(Or):
        __slots__ = ()

        def packages(self) -> frozenset[str]:
            return super().packages() | {"com.launcher"}

    either = OrLauncher(a, x)
    assert (error | either).conditions == (error, either)
    assert (error | either).packages() == frozenset({"com.a", "com.launcher"})
    assert (Level("I") | either)(_entry(tag="x")) is True


@pytest.mark.parametrize(
    "stream_type", [LogStream, AsyncLogStream], ids=["sync", "async"]
)
def test_streams_monitor_nested_overrides(
    stream_type: type[LogStream | AsyncLogStream],
) -> None:
    """The 2.x streams start a pid monitor for the packages that overrides of
    packages() name anywhere in the condition, not only at its root.
    """
    condition = (_App("com.a") & Level("E")) | ~Where(_App("com.b"))
    stream = stream_type(adb_path="adb", filter_by=condition)
    monitor = stream._pid_monitor
    assert monitor is not None
    assert sorted(monitor.packages) == ["com.a", "com.b"]


class _Once(Condition):
    """A leaf whose packages() fails the test when one walk asks it twice."""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, entry: LogEntry, /) -> bool:
        return entry.package == "com.a"

    def packages(self) -> frozenset[str]:
        self.calls += 1
        if self.calls > 1:
            pytest.fail("packages() walked a shared condition twice")
        return frozenset({"com.a"})


def test_packages_walks_a_shared_subtree_once() -> None:
    """packages() walks a condition that several parents share once:
    c = (c & x) | (c & y) nested 40 times has 123 conditions but 2**40 paths to the
    bottom, whose packages() is asked once. Evaluation still calls a shared
    condition once per path, in order.
    """
    leaf = _Once()
    x, y = Tag("x"), Tag("y")
    shared: Condition = leaf
    for depth in range(1, 41):
        shared = (shared & x) | (shared & y)
        if depth in {1, 3, 40}:
            leaf.calls = 0
            assert shared.packages() == frozenset({"com.a"}), depth
            assert leaf.calls == 1, depth

    calls: list[str] = []

    def probe(name: str, *, verdict: bool) -> Where:
        def check(entry: LogEntry) -> bool:
            del entry
            calls.append(name)
            return verdict

        return Where(check)

    once = probe("s", verdict=True)
    paths = (once & probe("x", verdict=False)) | (once & probe("y", verdict=True))
    assert Where(paths)(_entry()) is True
    assert calls == ["s", "x", "s", "y"]


class _AskedOnce(_WithLauncher):
    """A _WithLauncher that fails the test when one packages() call asks it twice."""

    __slots__ = ("asks",)

    def __init__(self, *conditions: Condition) -> None:
        super().__init__(*conditions)
        self.asks = 0

    def packages(self) -> frozenset[str]:
        self.asks += 1
        if self.asks > 1:
            pytest.fail("packages() asked a shared override twice")
        return super().packages()


def test_packages_asks_a_shared_override_once() -> None:
    """One packages() call asks an override that several parents share once, also
    when the parents are overrides that call super().packages():
    c = A(c, x) | A(c, y) nested 40 times has 80 overrides but 2**41 - 2 paths to
    them. Every call asks again, also after an override raised.
    """
    x, y = Tag("x"), Tag("y")
    shared: Condition = Package("com.a")
    overrides: list[_AskedOnce] = []
    for _ in range(40):
        left, right = _AskedOnce(shared, x), _AskedOnce(shared, y)
        overrides += [left, right]
        shared = left | right
    for _ in range(2):
        for node in overrides:
            node.asks = 0
        assert shared.packages() == frozenset({"com.a", "com.launcher"})
        assert {node.asks for node in overrides} == {1}

    class Fails(Condition):
        __slots__ = ("error",)

        def __init__(self) -> None:
            self.error = True

        def __call__(self, entry: LogEntry, /) -> bool:
            return False

        def packages(self) -> frozenset[str]:
            if self.error:
                raise LookupError
            return frozenset()

    # asked at both ends, so it is asked before Fails whichever end comes first
    asked, fails = _AskedOnce(Package("com.b"), x), Fails()
    condition = Or(asked, fails, asked)
    with pytest.raises(LookupError):
        condition.packages()
    assert asked.asks == 1
    fails.error = False
    asked.asks = 0
    assert condition.packages() == frozenset({"com.b", "com.launcher"})
    assert asked.asks == 1


class _Named(Condition):
    """A leaf that names its package in packages()."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, entry: LogEntry, /) -> bool:
        return entry.package == self.name

    def packages(self) -> frozenset[str]:
        return frozenset({self.name})


class _Rebuilt(_Named):
    """A leaf whose packages() builds a new condition with an override each time."""

    __slots__ = ()

    def packages(self) -> frozenset[str]:
        return (Tag("q") | _Named(self.name)).packages()


def test_packages_does_not_mix_up_conditions_built_during_the_call() -> None:
    """An override that a packages() call asked is not taken for a condition built
    later in the same call, which could otherwise reuse its id().
    """
    names = [f"com.p{i}" for i in range(20)]
    condition = Or(*(_Rebuilt(name) for name in names))
    assert condition.packages() == frozenset(names)


def test_packages_collects_every_package_in_the_tree() -> None:
    """packages() is the union of every Package name, under Or, Not and a Where of a
    condition too; the text-based package= of Crash/Anr and a Where of a plain
    function never count.
    """
    condition = (
        (Package(["com.a", "com.b"]) & Level("E"))
        | ~Package("com.c")
        | Filter(package="com.d:remote", tag="x")
        | Not(Not(Tag("y") | Package("com.e")))
        | CrashCondition(package="com.f")
        | AnrCondition(package="com.g")
        | Where(lambda entry: entry.package == "com.h")
        | Where(Package("com.i"))
        | Where(Where(~Package("com.j") & Tag("z")))
    )
    assert condition.packages() == frozenset(
        {"com.a", "com.b", "com.c", "com.d:remote", "com.e", "com.i", "com.j"}
    )
    assert Package("com.a").packages() == frozenset({"com.a"})
    assert Filter(package=["com.a", "com.b"]).packages() == frozenset(
        {"com.a", "com.b"}
    )
    assert (Tag("a") | CrashCondition(package="com.f")).packages() == frozenset()
    assert Filter().packages() == frozenset()


# -- validation -----------------------------------------------------------------------


@pytest.mark.parametrize("factory", [Tag, Package, Level, MessageContains])
@pytest.mark.parametrize(
    "empty",
    [[], (), set(), frozenset(), {}],
    ids=["list", "tuple", "set", "fset", "dict"],
)
def test_empty_collection_raises(
    factory: Callable[..., Condition], empty: object
) -> None:
    """An empty collection raises ValueError (D-13); in 2.0.0 it meant no constraint."""
    with pytest.raises(ValueError, match="empty collection"):
        factory(empty)


@pytest.mark.parametrize("factory", [Tag, Package, Level, MessageContains])
def test_values_must_be_strings(factory: Callable[..., Condition]) -> None:
    """Values are str: a non-str item or a non-iterable raises TypeError."""
    with pytest.raises(TypeError, match="must be str"):
        factory(["E", 1])
    with pytest.raises(TypeError, match="iterable of str"):
        factory(None)
    with pytest.raises(TypeError, match="must be str"):
        factory(b"E")


def test_a_str_is_one_value() -> None:
    """A str is one value, never a collection of its characters."""
    assert Tag("ab").values == frozenset({"ab"})
    assert Level("error").values == frozenset({"E"})
    assert MessageContains("ab").patterns == ("ab",)
    assert Tag(iter(["a", "b"])).values == frozenset({"a", "b"})


LEVEL_SPELLINGS = [
    ("V", "V"),
    ("v", "V"),
    ("verbose", "V"),
    ("VERBOSE", "V"),
    ("D", "D"),
    ("d", "D"),
    ("Debug", "D"),
    ("I", "I"),
    ("i", "I"),
    ("info", "I"),
    ("W", "W"),
    ("w", "W"),
    ("warning", "W"),
    ("Warning", "W"),
    ("warn", "W"),
    ("WARN", "W"),
    ("E", "E"),
    ("e", "E"),
    ("error", "E"),
    ("ERROR", "E"),
    ("F", "F"),
    ("f", "F"),
    ("fatal", "F"),
    ("FATAL", "F"),
]


@pytest.mark.parametrize(("spelling", "letter"), LEVEL_SPELLINGS)
def test_level_accepts_letters_and_names(spelling: str, letter: str) -> None:
    """Level and MinLevel take a letter or a name, in any case (D-13)."""
    assert Level(spelling).values == frozenset({letter})
    assert MinLevel(spelling).level == letter
    assert Level(spelling)(_entry(level=letter)) is True


@pytest.mark.parametrize(
    "spelling",
    [
        "",
        "X",
        "A",
        "S",
        "silent",
        "assert",
        "ASSERT",
        "ERR",
        "warnings",
        " E",
        "E ",
        "error!",
        chr(0x130),
    ],
)
def test_level_rejects_anything_else(spelling: str) -> None:
    """Anything but a level letter or name raises ValueError (D-13), at once."""
    with pytest.raises(ValueError, match="is not a log level"):
        Level(spelling)
    with pytest.raises(ValueError, match="is not a log level"):
        MinLevel(spelling)
    with pytest.raises(ValueError, match="is not a log level"):
        Filter(level=["E", spelling])


def test_min_level_matches_that_level_and_above() -> None:
    """MinLevel("W") matches W, E and F (V < D < I < W < E < F)."""
    order = ["V", "D", "I", "W", "E", "F"]
    for floor in order:
        condition = MinLevel(floor)
        matched = [level for level in order if condition(_entry(level=level))]
        assert matched == order[order.index(floor) :]


def test_min_level_takes_one_level() -> None:
    """MinLevel takes one level, not a collection."""
    with pytest.raises(TypeError, match="must be a str"):
        MinLevel(["E"])  # ty: ignore[invalid-argument-type]


VALID_PACKAGES = [
    "com.example",
    "com.example.app",
    "a",
    "A_b.c9",
    "com.android.settings",
    "com.example.app:remote",
    "com.example:remote.sub_1",
]
INVALID_PACKAGES = [
    "",
    "com.example;reboot",
    "com.example && id",
    "$(id)",
    "`id`",
    "com example",
    "com.example\n",
    "1com.example",
    "com..example",
    "com.example.",
    ".com.example",
    "com.1example",
    "com-example",
    "com.example:",
    "com.example:re mote",
    "com.example:a:b",
    chr(0xFF43) + "om.example",  # a fullwidth letter
]


@pytest.mark.parametrize("name", VALID_PACKAGES)
def test_package_accepts_package_names(name: str) -> None:
    """Package names follow design 2.7's grammar (F5-3), ":process" included."""
    assert Package(name).values == frozenset({name})
    assert Filter(package=name).packages() == frozenset({name})
    assert CrashCondition(package=name).package == name
    assert AnrCondition(package=name).package == name


@pytest.mark.parametrize("name", INVALID_PACKAGES, ids=range(len(INVALID_PACKAGES)))
def test_package_rejects_other_strings(name: str) -> None:
    """A string outside the grammar, such as shell syntax, raises ValueError (F5-3):
    it would reach the device shell through ``adb shell``.
    """
    with pytest.raises(ValueError, match="is not a package name"):
        Package(name)
    with pytest.raises(ValueError, match="is not a package name"):
        Package(["com.example", name])
    with pytest.raises(ValueError, match="is not a package name"):
        Filter(package=name)
    with pytest.raises(ValueError, match="is not a package name"):
        CrashCondition(package=name)
    with pytest.raises(ValueError, match="is not a package name"):
        AnrCondition(package=name)


def test_package_matches_only_attributed_entries() -> None:
    """Package reads LogEntry.package; an entry without one never matches."""
    assert Package("com.example")(_entry(package="com.example")) is True
    assert Package("com.example")(_entry(package="com.example.app")) is False
    assert Package("com.example")(_entry()) is False


# -- Filter ---------------------------------------------------------------------------


@pytest.mark.parametrize("keyword", ["package", "tag", "level", "message_contains"])
def test_filter_empty_collection_raises(keyword: str) -> None:
    """Filter builds the condition of each keyword, so an empty collection raises like
    it does there (D-13); in 2.0.0 it meant no constraint.
    """
    criteria: dict[str, Any] = {keyword: []}
    with pytest.raises(ValueError, match="empty collection"):
        Filter(**criteria)


def test_filter_without_criteria_matches_every_entry(sample_entry: LogEntry) -> None:
    """Filter() has no criteria and matches every entry, attributed or not, also
    nested under Not, Or and And.
    """
    assert Filter()(sample_entry) is True
    assert Filter()(sample_entry.model_copy(update={"package": None})) is True
    assert (~Filter())(sample_entry) is False
    assert (Filter() | Tag("zz"))(sample_entry) is True
    assert (Tag("zz") | ~Filter())(sample_entry) is False
    assert (Filter() & Tag("zz"))(sample_entry) is False
    assert (Filter() & Filter())(sample_entry) is True


def test_filter_is_the_and_of_its_criteria() -> None:
    """Filter is an And of Package, Tag, Level, MinLevel and MessageContains."""
    f = Filter(
        package="com.example",
        tag=["A", "B"],
        level=["warning", "E"],
        min_level="I",
        message_contains="boom",
    )
    assert isinstance(f, And)
    assert [type(c) for c in f.conditions] == [
        Package,
        Tag,
        Level,
        MinLevel,
        MessageContains,
    ]
    hit = _entry(tag="B", level="W", message="a boom", package="com.example")
    assert f(hit) is True
    for change in (
        {"tag": "C"},
        {"level": "E", "message": "quiet"},
        {"package": None},
        {"package": "com.other"},
    ):
        assert f(hit.model_copy(update=change)) is False
    assert Filter(min_level="E")(_entry(level="F")) is True
    assert Filter(min_level="E")(_entry(level="W")) is False


def test_filter_is_keyword_only() -> None:
    """Filter takes keywords only (2.x also took them by position)."""
    with pytest.raises(TypeError):
        Filter("com.example")  # ty: ignore[too-many-positional-arguments]


# -- Where ----------------------------------------------------------------------------


def test_where_wraps_a_function(sample_entry: LogEntry) -> None:
    """Where(fn) matches when fn(entry) is true, and combines like any condition."""
    assert Where(lambda entry: entry.pid == 123)(sample_entry) is True
    assert Where(lambda entry: entry.pid == 1)(sample_entry) is False
    truthy = Where(lambda entry: entry.message)  # ty: ignore[invalid-argument-type]
    assert truthy(sample_entry) is True
    assert (Tag("MyApp") & ~Where(lambda entry: entry.tid > 500))(sample_entry) is True
    assert Where(len).packages() == frozenset()  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="needs a callable"):
        Where("MyApp")  # ty: ignore[invalid-argument-type]


# -- CrashCondition and AnrCondition with package= -----------------------------------


def _java(message: str, *, tag: str = "AndroidRuntime", level: str = "E") -> LogEntry:
    return _entry(tag=tag, level=level, message=message)


def _native(name: str, *, tag: str = "DEBUG", level: str = "F") -> LogEntry:
    message = f"pid: 5042, tid: 5042, name: ndroid.settings  >>> {name} <<<"
    return _entry(tag=tag, level=level, message=message)


def test_crash_condition_with_package_matches_the_java_process_line() -> None:
    """With package=, the Java crash's ``Process: <name>, PID: <pid>`` line matches when
    <name> is the package or one of its ':' processes; FATAL EXCEPTION does not.
    """
    crash = CrashCondition(package="com.example.app")
    assert crash(_java("Process: com.example.app, PID: 5576")) is True
    assert crash(_java("Process: com.example.app:remote, PID: 5576")) is True
    assert crash(_java("Process: com.example.application, PID: 5576")) is False
    assert crash(_java("Process: com.example, PID: 5576")) is False
    assert crash(_java("Process: com.example.app.x, PID: 5576")) is False
    assert crash(_java("FATAL EXCEPTION: main")) is False
    assert crash(_java("Process: com.example.app, PID: 5576", level="W")) is False
    assert crash(_java("Process: com.example.app, PID: 5576", tag="Other")) is False
    assert crash(_java("Process: com.example.app")) is False
    assert crash(_java("Caused by: Process: com.example.app, PID: 5576")) is False
    secondary = CrashCondition(package="com.example.app:remote")
    assert secondary(_java("Process: com.example.app:remote, PID: 1")) is True
    assert secondary(_java("Process: com.example.app, PID: 1")) is False


def test_crash_condition_with_package_matches_the_native_tombstone_line() -> None:
    """With package=, the tombstone line ``pid: .., tid: .., name: ..  >>> <name> <<<``
    (DEBUG, F) matches by the same name rule (A.11), whatever tag/level say.
    """
    crash = CrashCondition(package="com.example.app")
    assert crash(_native("com.example.app")) is True
    assert crash(_native("com.example.app:remote")) is True
    assert crash(_native("com.example.application")) is False
    assert crash(_native("com.example.app", level="E")) is False
    assert crash(_native("com.example.app", tag="libc")) is False
    abort = "Abort message: pid: 1, tid: 1, name: x  >>> com.example.app <<<"
    assert crash(_entry(tag="DEBUG", level="F", message=abort)) is False
    custom = CrashCondition(package="com.example.app", tag="MyRuntime", level="W")
    assert custom(_native("com.example.app")) is True
    assert custom(_java("Process: com.example.app, PID: 1", tag="MyRuntime", level="W"))
    assert not custom(_java("Process: com.example.app, PID: 1"))
    assert CrashCondition()(_native("com.example.app")) is False


def test_crash_condition_without_package_is_unchanged() -> None:
    """Without package=, CrashCondition matches FATAL EXCEPTION as in 2.x."""
    assert CrashCondition()(_java("FATAL EXCEPTION: main")) is True
    assert CrashCondition()(_java("Process: com.example.app, PID: 1")) is False
    custom = CrashCondition(tag="Crash", level="fatal", message_pattern="boom")
    assert custom(_java("big boom", tag="Crash", level="F")) is True
    assert custom(_java("big boom", tag="Crash", level="E")) is False


def test_anr_condition_with_package_matches_the_named_process() -> None:
    """With package=, ``ANR in <name>`` matches when <name>, which ends at a space,
    '(' or the end, is the package or one of its ':' processes.
    """
    anr = AnrCondition(package="com.example.app")

    def line(message: str, *, tag: str = "ActivityManager") -> LogEntry:
        return _entry(tag=tag, level="E", message=message)

    assert anr(line("ANR in com.example.app (com.example.app/.Main)")) is True
    assert anr(line("ANR in com.example.app")) is True
    assert anr(line("ANR in com.example.app(com.example.app/.Main)")) is True
    assert anr(line("ANR in com.example.app:remote (x)")) is True
    assert anr(line("ANR in com.example.application (x)")) is False
    assert anr(line("ANR in com.example (x)")) is False
    assert anr(line("ANR in Window{1 u0 com.example.app/.Main}")) is False
    assert anr(line("ANR in com.example.app", tag="WindowManager")) is False
    assert anr(line("Reason: ANR in com.example.app")) is False
    assert anr(line("ANR incom.example.app")) is False
    prefixed = AnrCondition(package="com.example.app", message_prefix="ANR in ")
    assert prefixed(line("ANR in com.example.app")) is True
    assert AnrCondition()(line("ANR in com.example.application")) is True


def test_crash_and_anr_take_keywords_and_validate_them() -> None:
    """The 2.x keywords stay, package= is new, all are keyword-only, levels are
    validated like Level's.
    """
    assert CrashCondition(level="error").level == "E"
    assert AnrCondition(level="Warning").level == "W"
    with pytest.raises(ValueError, match="is not a log level"):
        CrashCondition(level="ERR")
    with pytest.raises(ValueError, match="is not a log level"):
        AnrCondition(level="")
    with pytest.raises(TypeError):
        CrashCondition("AndroidRuntime")  # ty: ignore[too-many-positional-arguments]
    with pytest.raises(TypeError):
        AnrCondition("ActivityManager")  # ty: ignore[too-many-positional-arguments]
    with pytest.raises(TypeError, match="tag must be a str"):
        CrashCondition(tag=None)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="message_prefix must be a str"):
        AnrCondition(message_prefix=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="must be a str"):
        CrashCondition(package=["com.example"])  # ty: ignore[invalid-argument-type]


# -- repr -----------------------------------------------------------------------------


def test_repr_shows_the_tree() -> None:
    """repr shows each condition as the call that builds it (sets sorted)."""
    assert repr(Tag("a")) == "Tag('a')"
    assert repr(Tag(["b", "a"])) == "Tag(['a', 'b'])"
    assert repr(Package("com.example")) == "Package('com.example')"
    assert repr(Level(["error", "v"])) == "Level(['V', 'E'])"
    assert repr(MinLevel("warn")) == "MinLevel('W')"
    assert repr(MessageContains(["y", "x"])) == "MessageContains(['y', 'x'])"
    assert (
        repr(~Tag("a") | (Tag("b") & Level("E")))
        == "Or(Not(Tag('a')), And(Tag('b'), Level('E')))"
    )
    assert (
        repr(Filter(package="com.example", min_level="W"))
        == "Filter(package='com.example', min_level='W')"
    )
    assert repr(Filter()) == "Filter()"
    assert repr(And(Filter())) == "And(Filter())"
    assert repr(Filter() & Filter()) == "And(Filter(), Filter())"
    assert repr(Filter() & Tag("t")) == "And(Filter(), Tag('t'))"
    assert repr(Filter(tag="a") & Tag("t")) == "And(Tag('a'), Tag('t'))"
    assert repr(Filter(tag="a") | Tag("b")) == "Or(Filter(tag='a'), Tag('b'))"
    assert repr(CrashCondition()) == "CrashCondition()"
    assert repr(CrashCondition(package="com.example")) == (
        "CrashCondition(package='com.example')"
    )
    assert repr(AnrCondition(level="warning")) == "AnrCondition(level='W')"
    assert repr(Where(len)).startswith("Where(<built-in function len>")  # ty: ignore[invalid-argument-type]


# -- the 2.x entry points take any Condition (X-7) -------------------------------------


def test_entry_points_take_any_condition(tmp_path: Path) -> None:
    """LogStream, AsyncLogStream (filter_by=) and read_file (where=) take any
    Condition; a bare function raises TypeError that points to Where, read_file
    when it is called.
    """
    path = tmp_path / "app.log"
    path.write_text(
        "11-04 05:11:32.542  669  5396 E ActivityManager: "
        "ANR in com.android.settings (com.android.settings/.Settings)\n"
        "11-04 05:11:32.543  669  5396 I ActivityManager: idle\n",
        encoding="utf-8",
    )
    anr = AnrCondition(package="com.android.settings")
    assert [e.level for e in read_file(path, where=anr, year=2026)] == ["E"]
    assert [e.level for e in read_file(path, where=~anr, year=2026)] == ["I"]
    assert LogStream(adb_path="adb", filter_by=anr | Tag("x")).filter_by is not None
    assert AsyncLogStream(adb_path="adb", filter_by=Where(bool)).filter_by is not None

    def keep(entry: LogEntry) -> bool:
        return entry.level == "E"

    with pytest.raises(TypeError, match=r"filter_by must be a Condition.*Where"):
        LogStream(adb_path="adb", filter_by=keep)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match=r"filter_by must be a Condition.*Where"):
        AsyncLogStream(adb_path="adb", filter_by=keep)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match=r"where must be a Condition.*Where"):
        read_file(path, where=keep)  # ty: ignore[invalid-argument-type]


def test_pidof_names_are_validated_where_they_reach_adb() -> None:
    """A name that is not a package name never reaches ``adb shell pidof`` (F5-3),
    even when it bypasses Package's own check: the streams refuse it when they are
    built, and the 2.x pidof builder refuses it too.
    """

    class Smuggler(Condition):
        def __call__(self, entry: LogEntry, /) -> bool:
            del entry
            return True

        def packages(self) -> frozenset[str]:
            return frozenset({"com.a;reboot"})

    with pytest.raises(ValueError, match="is not a package name"):
        LogStream(adb_path="adb", filter_by=Smuggler())
    with pytest.raises(ValueError, match="is not a package name"):
        AsyncLogStream(adb_path="adb", filter_by=Smuggler())
    with pytest.raises(ValueError, match="is not a package name"):
        build_pidof_command("adb", None, "com.a;reboot")
    assert build_pidof_command("adb", "emulator-5554", "com.a:remote") == [
        "adb",
        "-s",
        "emulator-5554",
        "shell",
        "pidof",
        "com.a:remote",
    ]
