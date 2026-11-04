"""Conditions: predicates over log entries (design section 2.7).

A `Condition` is called with a `LogEntry` and returns whether the entry
matches. Conditions combine with ``&`` (`And`), ``|`` (`Or`) and ``~`` (`Not`);
any condition, combined or not, is what a stream or reader filters by.

- Field conditions: `Tag`, `Package`, `Level`, `MinLevel` and `MessageContains`.
  `Filter` is the `And` of the field conditions given as keywords.
- Event conditions: `CrashCondition` and `AnrCondition` match the lines Android
  logs for an app crash and for an ANR; with ``package=`` they name the app from
  the line's text, so they need no package attribution and work offline.
- `Where` wraps any function of an entry.

Constructors validate their arguments, so a typo fails where it is written
(decision D-13): an empty collection, an unknown level and a string that is not
a package name raise ValueError, a value of the wrong type raises TypeError.

`And` and `Or` absorb children of their own kind (``a & b & c`` is one `And`
of three), and every tree is evaluated, walked by `Condition.packages` and
printed by ``repr`` without recursion, so mixed ``&``/``|`` chains of any
length work (F7-4). `And` and `Or` short-circuit from left to right, like
`all` and `any`. A subclass of `And`, `Or` or `Not` that overrides
``__call__`` is called like any other condition, never taken apart.

`Package` matches ``LogEntry.package``, which only package attribution sets. A
stream attributes the packages of `Condition.packages`: every `Package` in the
tree, under `Or`, `Not` and a `Where` that wraps a condition too (F5-1). A log
file carries no attribution, so a `Package` matches none of its entries.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import TYPE_CHECKING, Final

from .adb import _check_package_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from .models import LogEntry, LogLevel

__all__ = [
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

# The levels from the lowest priority to the highest.
_LEVELS: Final[tuple[LogLevel, ...]] = ("V", "D", "I", "W", "E", "F")
# D-13: a level is its letter or its name, in any case. W has two names: "warn"
# (Android's Log.WARN and ANDROID_LOG_WARN) and "warning" (design 2.7). Java's
# name for F, "assert", is not taken: a name added later breaks nobody, a name
# removed later does.
_LEVEL_NAMES: Final[dict[str, LogLevel]] = {
    "v": "V",
    "verbose": "V",
    "d": "D",
    "debug": "D",
    "i": "I",
    "info": "I",
    "w": "W",
    "warn": "W",
    "warning": "W",
    "e": "E",
    "error": "E",
    "f": "F",
    "fatal": "F",
}

# The Java crash block's process line (RuntimeInit), e.g.
# "Process: com.android.settings, PID: 5576" (tests/fixtures/device, C07).
_JAVA_CRASH_PROCESS: Final = re.compile(r"Process: (?P<name>[^\s,]+), PID: [0-9]+")
# A.11: the tombstone line of a native crash, logged as DEBUG at level F, e.g.
# "pid: 5042, tid: 5042, name: ndroid.settings  >>> com.android.settings <<<".
_NATIVE_CRASH_TAG: Final = "DEBUG"
_NATIVE_CRASH_LEVEL: Final = "F"
_NATIVE_CRASH_PROCESS: Final = re.compile(
    r"pid: [0-9]+, tid: [0-9]+, name: .*  >>> (?P<name>\S+) <<<"
)


class Condition(ABC):
    """A predicate over log entries: ``condition(entry)`` is True when it matches.

    Subclass it and implement ``__call__`` for a condition of your own, or wrap
    a function in `Where`. The entry is passed by position, so an override may
    name its parameter as it likes. Evaluating a built-in condition changes
    nothing that a result depends on, so one condition may be evaluated by
    several threads at once.
    """

    __slots__ = ()

    @abstractmethod
    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether ``entry`` matches."""

    def __and__(self, other: Condition) -> And:
        """Return the condition that both match."""
        if not isinstance(other, Condition):
            return NotImplemented
        return And(self, other)

    def __or__(self, other: Condition) -> Or:
        """Return the condition that either matches."""
        if not isinstance(other, Condition):
            return NotImplemented
        return Or(self, other)

    def __invert__(self) -> Not:
        """Return the condition that this one does not match."""
        return Not(self)

    def packages(self) -> frozenset[str]:
        """Return the names of every `Package` in this condition's tree.

        `Package` conditions under `Or` and `Not` count too: a stream must
        attribute a package's lines for ``~Package(name)`` to leave them out.
        So does a condition wrapped in `Where`; a plain function is opaque.
        """
        names: set[str] = set()
        pending: list[Condition] = [self]
        while pending:
            node = pending.pop()
            if isinstance(node, Package):
                names.update(node.values)
            elif isinstance(node, _Junction):
                pending.extend(node.conditions)
            elif isinstance(node, Not):
                pending.append(node.condition)
            elif isinstance(node, Where) and isinstance(node.fn, Condition):
                pending.append(node.fn)
        return frozenset(names)


class _Junction(Condition):
    """`And` or `Or`: a sequence of conditions evaluated from left to right."""

    __slots__ = ("_flat", "conditions")

    conditions: tuple[Condition, ...]
    # Whether no And, Or or Not that `_combines` is among the conditions, so that
    # a plain loop evaluates the junction; None until the first call. Computed
    # lazily because a chain a & b & c & ... builds a junction per operator.
    _flat: bool | None

    def __init__(self, conditions: tuple[Condition, ...]) -> None:
        self.conditions = conditions
        self._flat = None

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether ``entry`` matches."""
        flat = self._flat
        if flat is None:
            flat = not any(_combines(c) for c in self.conditions)
            self._flat = flat
        if not flat:
            return _evaluate(self, entry)
        decisive = isinstance(self, Or)
        for condition in self.conditions:
            if bool(condition(entry)) is decisive:
                return decisive
        return not decisive

    def __repr__(self) -> str:
        return _render(self)


class And(_Junction):
    """Matches when every one of its conditions matches.

    ``And(a, b)`` is ``a & b``. An `And` among the conditions (`Filter`
    included) contributes its own conditions instead of itself, unless it has
    none (``Filter()``) or overrides ``__call__``.
    """

    __slots__ = ()

    def __init__(self, *conditions: Condition) -> None:
        """Combine one or more conditions.

        Raises:
            ValueError: No condition was given.
            TypeError: An argument is not a `Condition`.

        """
        super().__init__(_flatten(And, conditions))


class Or(_Junction):
    """Matches when at least one of its conditions matches.

    ``Or(a, b)`` is ``a | b``. An `Or` among the conditions contributes its own
    conditions instead of itself, unless it overrides ``__call__``.
    """

    __slots__ = ()

    def __init__(self, *conditions: Condition) -> None:
        """Combine one or more conditions.

        Raises:
            ValueError: No condition was given.
            TypeError: An argument is not a `Condition`.

        """
        super().__init__(_flatten(Or, conditions))


class Not(Condition):
    """Matches when its condition does not. ``Not(a)`` is ``~a``."""

    __slots__ = ("condition",)

    condition: Condition

    def __init__(self, condition: Condition) -> None:
        """Negate ``condition``.

        Raises:
            TypeError: ``condition`` is not a `Condition`.

        """
        self.condition = _require_condition(condition, "Not()'s argument")

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether ``entry`` does not match the negated condition."""
        return _evaluate(self, entry)

    def __repr__(self) -> str:
        return _render(self)


def _combines(node: Condition) -> bool:
    """Whether ``node`` is an `And`, `Or` or `Not` that its built-in ``__call__`` runs.

    Only such a node is taken apart (flattened and evaluated as a tree node); a
    subclass that overrides ``__call__`` is called like any other condition.
    """
    if isinstance(node, _Junction):
        return type(node).__call__ is _Junction.__call__
    return isinstance(node, Not) and type(node).__call__ is Not.__call__


class Tag(Condition):
    """Matches entries whose tag is one of ``values`` (exact, case-sensitive)."""

    __slots__ = ("values",)

    values: frozenset[str]

    def __init__(self, values: str | Iterable[str]) -> None:
        """Match one tag (a str) or any of a collection of tags.

        Raises:
            ValueError: ``values`` is an empty collection.
            TypeError: A value is not a str.

        """
        self.values = frozenset(_strings(values, "Tag"))

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether the entry's tag is one of the values."""
        return entry.tag in self.values

    def __repr__(self) -> str:
        return f"Tag({_argument_repr(self)})"


class Package(Condition):
    """Matches entries attributed to one of the ``values`` packages.

    It reads ``LogEntry.package``, which package attribution sets; an entry
    without one never matches. A stream attributes every package that a
    `Package` in its condition names (`Condition.packages`).
    """

    __slots__ = ("values",)

    values: frozenset[str]

    def __init__(self, values: str | Iterable[str]) -> None:
        """Match one package (a str) or any of a collection of packages.

        A name is dot-separated segments of ASCII letters, digits and ``_``,
        each starting with a letter, optionally followed by ``:<process>``
        (F5-3): such a name reaches ``adb shell`` and the device shell parses it.

        Raises:
            ValueError: ``values`` is an empty collection or holds a string that
                is not a package name.
            TypeError: A value is not a str.

        """
        names = _strings(values, "Package")
        self.values = frozenset(_check_package_name(name) for name in names)

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether the entry is attributed to one of the packages."""
        return entry.package in self.values

    def __repr__(self) -> str:
        return f"Package({_argument_repr(self)})"


class Level(Condition):
    """Matches entries whose level is one of ``values``.

    A level is given as its letter (``V``, ``D``, ``I``, ``W``, ``E``, ``F``) or
    its name (``verbose``, ``debug``, ``info``, ``warning`` or ``warn``,
    ``error``, ``fatal``), in any case (decision D-13).
    """

    __slots__ = ("values",)

    values: frozenset[LogLevel]

    def __init__(self, values: str | Iterable[str]) -> None:
        """Match one level (a str) or any of a collection of levels.

        Raises:
            ValueError: ``values`` is an empty collection or holds a string that
                is not a level.
            TypeError: A value is not a str.

        """
        self.values = frozenset(_level(value) for value in _strings(values, "Level"))

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether the entry's level is one of the values."""
        return entry.level in self.values

    def __repr__(self) -> str:
        return f"Level({_argument_repr(self)})"


class MinLevel(Condition):
    """Matches entries at ``level`` or a higher priority (V < D < I < W < E < F).

    ``level`` is given like a `Level` value: a letter or a name, in any case.
    """

    __slots__ = ("_levels", "level")

    level: LogLevel
    _levels: frozenset[LogLevel]

    def __init__(self, level: str) -> None:
        """Match ``level`` and every level above it.

        Raises:
            ValueError: ``level`` is not a level.
            TypeError: ``level`` is not a str.

        """
        self.level = _level(level)
        self._levels = frozenset(_LEVELS[_LEVELS.index(self.level) :])

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether the entry's level is ``level`` or higher."""
        return entry.level in self._levels

    def __repr__(self) -> str:
        return f"MinLevel({_argument_repr(self)})"


class MessageContains(Condition):
    """Matches entries whose message contains one of ``patterns``.

    Patterns are literal substrings, matched case-sensitively; regular
    expression characters have no special meaning. The empty pattern is in
    every message.
    """

    __slots__ = ("_regex", "patterns")

    patterns: tuple[str, ...]
    _regex: re.Pattern[str]

    def __init__(self, patterns: str | Iterable[str]) -> None:
        """Match one substring (a str) or any of a collection of substrings.

        Raises:
            ValueError: ``patterns`` is an empty collection.
            TypeError: A pattern is not a str.

        """
        self.patterns = _strings(patterns, "MessageContains")
        self._regex = _contains_regex(self.patterns)

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether the message contains one of the patterns."""
        return self._regex.search(entry.message) is not None

    def __repr__(self) -> str:
        return f"MessageContains({_argument_repr(self)})"


class CrashCondition(Condition):
    """Matches the line Android logs when an app crashes.

    Without ``package``, as in 2.x: an entry with tag ``tag`` and level
    ``level`` whose message contains ``message_pattern`` (the
    ``FATAL EXCEPTION: <thread>`` line of a Java crash, for any app).

    With ``package``, the line that names the crashed process, read from the
    message, so no package attribution is needed and it works offline:

    - a Java crash: the ``Process: <name>, PID: <pid>`` line, with tag ``tag``
      and level ``level`` (``message_pattern`` is not used: the
      ``FATAL EXCEPTION`` line does not name the process);
    - a native crash: the tombstone line
      ``pid: <pid>, tid: <tid>, name: <thread>  >>> <name> <<<``, which
      Android logs with tag ``DEBUG`` and level ``F`` whatever ``tag`` and
      ``level`` say.

    ``<name>`` matches when it is ``package`` or starts with ``package + ":"``
    (a secondary process of the app), so ``com.example.app`` does not match
    ``com.example.application``.
    """

    __slots__ = ("level", "message_pattern", "package", "tag")

    package: str | None
    tag: str
    level: LogLevel
    message_pattern: str

    def __init__(
        self,
        *,
        package: str | None = None,
        tag: str = "AndroidRuntime",
        level: str = "E",
        message_pattern: str = "FATAL EXCEPTION",
    ) -> None:
        """Configure the crash lines to match.

        Args:
            package: The app (package name, optionally ``:<process>``) whose
                crashes match; None matches every app's Java crash.
            tag: The tag of the Java crash lines.
            level: The level of the Java crash lines, given like a `Level`
                value.
            message_pattern: The substring that marks the Java crash line when
                ``package`` is None.

        Raises:
            ValueError: ``package`` is not a package name, or ``level`` is not
                a level.
            TypeError: An argument is not a str (or None for ``package``).

        """
        self.package = None if package is None else _check_package_name(package)
        self.tag = _string(tag, "tag")
        self.level = _level(level)
        self.message_pattern = _string(message_pattern, "message_pattern")

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether the entry is a crash line (of ``package``, if set)."""
        package = self.package
        if package is None:
            return (
                entry.tag == self.tag
                and entry.level == self.level
                and self.message_pattern in entry.message
            )
        if entry.tag == self.tag and entry.level == self.level:
            found = _JAVA_CRASH_PROCESS.match(entry.message)
            if found is not None and _names_package(found["name"], package):
                return True
        if entry.tag == _NATIVE_CRASH_TAG and entry.level == _NATIVE_CRASH_LEVEL:
            found = _NATIVE_CRASH_PROCESS.match(entry.message)
            return found is not None and _names_package(found["name"], package)
        return False

    def __repr__(self) -> str:
        return _keywords_repr(
            self,
            package=(self.package, None),
            tag=(self.tag, "AndroidRuntime"),
            level=(self.level, "E"),
            message_pattern=(self.message_pattern, "FATAL EXCEPTION"),
        )


class AnrCondition(Condition):
    """Matches the line Android logs when an app does not respond (ANR).

    An entry with tag ``tag`` and level ``level`` whose message starts with
    ``message_prefix``. system_server logs the line, not the app, so
    ``Package(app) & AnrCondition()`` does not find the app's ANRs; use
    ``AnrCondition(package=app)``.

    With ``package``, the text after the prefix names the process
    (``ANR in <name> (<component>)``): ``<name>`` ends at a space, ``(`` or the
    end of the message, and matches when it is ``package`` or starts with
    ``package + ":"``. No package attribution is needed and it works offline.
    """

    __slots__ = ("_named", "level", "message_prefix", "package", "tag")

    package: str | None
    tag: str
    level: LogLevel
    message_prefix: str
    _named: re.Pattern[str]

    def __init__(
        self,
        *,
        package: str | None = None,
        tag: str = "ActivityManager",
        level: str = "E",
        message_prefix: str = "ANR in",
    ) -> None:
        """Configure the ANR line to match.

        Args:
            package: The app (package name, optionally ``:<process>``) whose
                ANRs match; None matches every app's ANR.
            tag: The tag of the ANR line.
            level: The level of the ANR line, given like a `Level` value.
            message_prefix: The text the ANR line starts with, before the
                process name.

        Raises:
            ValueError: ``package`` is not a package name, or ``level`` is not
                a level.
            TypeError: An argument is not a str (or None for ``package``).

        """
        self.package = None if package is None else _check_package_name(package)
        self.tag = _string(tag, "tag")
        self.level = _level(level)
        self.message_prefix = _string(message_prefix, "message_prefix")
        # The name follows the prefix after at least one space, unless the
        # prefix already ends with one.
        gap = r"\s*" if not message_prefix or message_prefix[-1].isspace() else r"\s+"
        self._named = re.compile(re.escape(message_prefix) + gap + r"(?P<name>[^\s(]+)")

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether the entry is an ANR line (of ``package``, if set)."""
        if entry.tag != self.tag or entry.level != self.level:
            return False
        if self.package is None:
            return entry.message.startswith(self.message_prefix)
        found = self._named.match(entry.message)
        return found is not None and _names_package(found["name"], self.package)

    def __repr__(self) -> str:
        return _keywords_repr(
            self,
            package=(self.package, None),
            tag=(self.tag, "ActivityManager"),
            level=(self.level, "E"),
            message_prefix=(self.message_prefix, "ANR in"),
        )


class Where(Condition):
    """Matches entries for which ``fn(entry)`` is true."""

    __slots__ = ("fn",)

    fn: Callable[[LogEntry], bool]

    def __init__(self, fn: Callable[[LogEntry], bool]) -> None:
        """Wrap ``fn``, a function of one `LogEntry`.

        A package that ``fn`` compares ``entry.package`` with is not attributed:
        only `Package` conditions tell a stream which packages to attribute,
        those of a `Condition` given as ``fn`` included.

        Raises:
            TypeError: ``fn`` is not callable.

        """
        if not callable(fn):
            msg = f"Where() needs a callable, not {type(fn).__name__}"
            raise TypeError(msg)
        self.fn = fn

    def __call__(self, entry: LogEntry, /) -> bool:
        """Return whether ``fn(entry)`` is true."""
        return bool(self.fn(entry))

    def __repr__(self) -> str:
        return f"Where({self.fn!r})"


class Filter(And):
    """The `And` of field conditions, given as keywords.

    ``Filter(package=p, tag=t, level=l, min_level=m, message_contains=s)`` is
    ``Package(p) & Tag(t) & Level(l) & MinLevel(m) & MessageContains(s)``
    without the parts that are None. Each keyword takes what that condition
    takes and validates it the same way, so an empty collection raises.
    ``Filter()`` matches every entry.

    Examples:
        Tag "MyApp" at level W or above:

        >>> f = Filter(tag="MyApp", min_level="W")

        Tag "MyApp" or "MyService", and "Error" in the message:

        >>> f = Filter(tag=["MyApp", "MyService"], message_contains="Error")

    """

    __slots__ = ("_keywords",)

    _keywords: tuple[str, ...]

    def __init__(
        self,
        *,
        package: str | Iterable[str] | None = None,
        tag: str | Iterable[str] | None = None,
        level: str | Iterable[str] | None = None,
        min_level: str | None = None,
        message_contains: str | Iterable[str] | None = None,
    ) -> None:
        """Combine the given criteria with AND.

        Args:
            package: As `Package`.
            tag: As `Tag`.
            level: As `Level`.
            min_level: As `MinLevel`.
            message_contains: As `MessageContains`.

        Raises:
            ValueError: A criterion is invalid (see the condition it builds).
            TypeError: A criterion has the wrong type.

        """
        parts: list[tuple[str, Condition]] = []
        if package is not None:
            parts.append(("package", Package(package)))
        if tag is not None:
            parts.append(("tag", Tag(tag)))
        if level is not None:
            parts.append(("level", Level(level)))
        if min_level is not None:
            parts.append(("min_level", MinLevel(min_level)))
        if message_contains is not None:
            parts.append(("message_contains", MessageContains(message_contains)))
        # Filter() is the empty And, which matches every entry; And() itself
        # refuses no arguments, so the junction is set up directly.
        _Junction.__init__(self, tuple(condition for _, condition in parts))
        self._keywords = tuple(keyword for keyword, _ in parts)

    def __repr__(self) -> str:
        arguments = ", ".join(
            f"{keyword}={_argument_repr(condition)}"
            for keyword, condition in zip(self._keywords, self.conditions, strict=True)
        )
        return f"Filter({arguments})"


# -- helpers ------------------------------------------------------------------------


def _require_condition(value: object, what: str) -> Condition:
    """Return ``value`` if it is a `Condition`.

    Raises:
        TypeError: ``value`` is not a `Condition`; the message suggests `Where`
            for a plain function.

    """
    if not isinstance(value, Condition):
        msg = f"{what} must be a Condition, not {type(value).__name__}"
        if callable(value):
            msg += "; wrap a function in Where(fn)"
        raise TypeError(msg)
    return value


def _flatten(
    kind: type[And] | type[Or], conditions: tuple[Condition, ...]
) -> tuple[Condition, ...]:
    """``conditions`` with each child of type ``kind`` replaced by its children.

    A ``kind`` child is already flat, so one level is enough. A child without
    children (``Filter()``) stays, so that ``repr`` still shows it, and so does
    one that overrides ``__call__`` (`_combines`).

    Raises:
        ValueError: ``conditions`` is empty.
        TypeError: An item is not a `Condition`.

    """
    if not conditions:
        msg = f"{kind.__name__}() needs at least one condition"
        raise ValueError(msg)
    flat: list[Condition] = []
    for condition in conditions:
        if (
            isinstance(condition, kind)
            and condition.conditions
            and _combines(condition)
        ):
            flat.extend(condition.conditions)
        else:
            flat.append(_require_condition(condition, f"{kind.__name__}()'s argument"))
    return tuple(flat)


def _evaluate(root: Condition, entry: LogEntry) -> bool:
    """Evaluate the tree ``root`` for ``entry`` with an explicit stack (F7-4).

    `And` and `Or` evaluate their children from left to right and stop at the
    first verdict that decides them (False for `And`, True for `Or`), like
    `all` and `any`; every other condition is called, and so is a combination
    below ``root`` that overrides ``__call__`` (`_combines`). ``root`` itself
    is always taken apart: an override that calls ``super().__call__`` gets
    here with ``root`` set to itself.
    """
    # A junction's frame is (its children, the index of the next one, the verdict
    # that decides it); a Not's frame has no children and flips the verdict.
    frames: list[tuple[tuple[Condition, ...] | None, int, bool]] = []
    node = root
    verdict = False
    while True:
        # Down the leftmost path to a verdict.
        while True:
            combines = node is root or _combines(node)
            if combines and isinstance(node, _Junction):
                children = node.conditions
                decisive = isinstance(node, Or)
                if not children:
                    verdict = not decisive
                    break
                frames.append((children, 1, decisive))
                node = children[0]
            elif combines and isinstance(node, Not):
                frames.append((None, 0, False))
                node = node.condition
            else:
                verdict = bool(node(entry))
                break
        # Up to the first junction that the verdict does not decide.
        while frames:
            children, index, decisive = frames[-1]
            if children is None:
                frames.pop()
                verdict = not verdict
            elif verdict is decisive or index == len(children):
                frames.pop()
            else:
                frames[-1] = (children, index + 1, decisive)
                node = children[index]
                break
        else:
            return verdict


def _render(root: Condition) -> str:
    """The repr of the tree ``root``, built with an explicit stack (F7-4)."""
    parts: list[str] = []
    pending: list[Condition | str] = [root]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, Filter) or not isinstance(item, _Junction | Not):
            parts.append(repr(item))
            continue
        children = item.conditions if isinstance(item, _Junction) else (item.condition,)
        pending.append(")")
        for index in range(len(children) - 1, -1, -1):
            pending.append(children[index])
            if index:
                pending.append(", ")
        pending.append(f"{type(item).__name__}(")
    return "".join(parts)


def _argument_repr(condition: Condition) -> str:
    """The constructor argument of a field condition: one value, or a list."""
    values: tuple[str, ...]
    if isinstance(condition, MinLevel):
        return repr(condition.level)
    if isinstance(condition, MessageContains):
        values = condition.patterns
    elif isinstance(condition, Level):
        values = tuple(sorted(condition.values, key=_LEVELS.index))
    elif isinstance(condition, Tag | Package):
        values = tuple(sorted(condition.values))
    else:
        return repr(condition)
    return repr(values[0]) if len(values) == 1 else repr(list(values))


def _keywords_repr(
    condition: Condition, **keywords: tuple[str | None, str | None]
) -> str:
    """``Name(keyword=value, ...)`` for the keywords whose value is not the default."""
    arguments = ", ".join(
        f"{keyword}={value!r}"
        for keyword, (value, default) in keywords.items()
        if value != default
    )
    return f"{type(condition).__name__}({arguments})"


def _strings(values: str | Iterable[str], owner: str) -> tuple[str, ...]:
    """``values`` as a tuple of str: one str is one value.

    Raises:
        ValueError: ``values`` is an empty collection.
        TypeError: ``values`` is not a str or an iterable of str.

    """
    if isinstance(values, str):
        items: tuple[str, ...] = (values,)
    else:
        try:
            items = tuple(values)
        except TypeError:
            kind = type(values).__name__
            msg = f"{owner}() takes a str or an iterable of str, not {kind}"
            raise TypeError(msg) from None
    for item in items:
        if not isinstance(item, str):
            msg = f"{owner}() values must be str, not {type(item).__name__}"
            raise TypeError(msg)
    if not items:
        msg = f"{owner}() got an empty collection; give at least one value"
        raise ValueError(msg)
    return items


def _string(value: str, keyword: str) -> str:
    """Return ``value`` if it is a str.

    Raises:
        TypeError: ``value`` is not a str.

    """
    if not isinstance(value, str):
        msg = f"{keyword} must be a str, not {type(value).__name__}"
        raise TypeError(msg)
    return value


def _level(value: str) -> LogLevel:
    """The letter of the level ``value`` names (D-13).

    Raises:
        ValueError: ``value`` is not a level letter or name.
        TypeError: ``value`` is not a str.

    """
    if not isinstance(value, str):
        msg = f"a level must be a str, not {type(value).__name__}"
        raise TypeError(msg)
    letter = _LEVEL_NAMES.get(value.lower()) if value.isascii() else None
    if letter is None:
        msg = (
            f"{value!r} is not a log level: use V, D, I, W, E or F, or verbose, "
            "debug, info, warning (warn), error or fatal, in any case"
        )
        raise ValueError(msg)
    return letter


def _names_package(name: str, package: str) -> bool:
    """Whether the process ``name`` is ``package`` or one of its ``:`` processes."""
    return name == package or name.startswith(package + ":")


@lru_cache(maxsize=256)
def _contains_regex(patterns: tuple[str, ...]) -> re.Pattern[str]:
    """One regex that finds any of the literal ``patterns``."""
    return re.compile("|".join(re.escape(pattern) for pattern in patterns))
