"""Package attribution of a live stream (design 5.8, D-8, 5.10, A.21).

The stream core (`logpyt._core.machine`) calls this policy at the points of the
transition table (design 3.2): the first lookups at start, ``lookup done``,
``hold_expired`` (the policy's one deadline), every stdout line before
``where``, the end of a connection and the run's conclusion. Like the rest of
the core it reads no clock and runs nothing: the core passes the monotonic
time in and runs the lookups that `Attribution.next_lookup` asks for, and
adds the lines that `Attribution.lookup_done` found unattributed
(`Answer.unattributed`) to ``stats.unattributed``.

Watched packages are ``where.packages()``. A uid names an app, not one of its
processes, so they are package names: they are checked when the attribution
is built (F5-3, and a process name ``<package>:<process>`` raises), and a bad
name from an override of ``packages()`` fails there, not at the first lookup.
With no watched package the core runs no attribution at all.

The uid map
-----------

- A lookup is ``adb shell dumpsys package <package>`` (A.8: ``userId=`` on API
  24, ``appId=`` on 36), one command per package, one in flight at a time,
  each with its own id. At start every watched package is looked up once,
  and intake stays closed until all of them returned or ``package_hold``
  expired (F5-2: the startup backlog is attributed).
- Keys are app ids (uid modulo 100000), so a secondary user's line matches by
  app id. A line's package is known when its app id is mapped, is a system
  uid (below 10000; a uid column that is no number and no known name parses
  to ``uid=None`` and counts as one), is an isolated process's (90000 and
  above, A.14: a lookup names an app's own uid, never one of these), or is in
  the negative cache.
- Any other line comes from an unknown app uid. It asks for a re-lookup of
  every watched package that has no answer issued after the uid was first
  seen, at least `LOOKUP_INTERVAL` after that package's last lookup (X-3).
  Of the re-lookups that are due, one that choices have passed over for
  ``package_hold`` goes first, then packages without a uid (lines may wait
  for them), then the others; within each, the longest since its last
  lookup first, then by name. So lines wait for a mapped package's re-lookup
  only once it has waited ``package_hold``, and a due re-lookup waits about
  ``package_hold`` at most, plus one lookup of each other package, however
  long lookups take and however many new uids come. A lookup issued before
  a uid was first seen says nothing about it: the app may have been
  installed after the lookup ran. So a result only ever settles the uids
  seen before its request, and a result whose id is not the lookup in
  flight is ignored.
- A line waits only for a package without a uid (not installed, or installed
  during the capture). A mapped package is looked up again as well, so that a
  reinstall under a new uid is found (design 5.8, 9.4): an answer with another
  uid moves the package to it, and an answer that it is not installed takes
  its uid away. With every watched package mapped, no line is ever held
  (D-8): uids are static for an install, so that re-lookup only catches a
  reinstall, and the lines of the new uid that come before its answer go on
  without their package; the answer counts those whose verdict depended on
  it (see "Lines that went on before an answer").
- Once every watched package has answered after an app id was first seen,
  and none named it, that app id goes into the negative cache. The negative
  cache and the app ids waiting for an answer share one LRU of `UID_CACHE`
  entries with the map (5.10); the map's entries are never evicted (one per
  watched package), the others are, least recently seen first.

The ordered hold and tri-state evaluation
-----------------------------------------

- A line from an unknown app uid is held only when a package without a uid
  may own it and its ``where`` verdict depends on the package (`evaluate`):
  ``Package(a) | Tag("x")`` with tag ``x`` is true without it and goes on at
  once, with ``package=None`` and not counted as unattributed. Once a line is
  held, every later line queues behind it, in order, and is evaluated when it
  reaches the head, with what is known by then: a line from an unknown app
  uid takes its package if a lookup mapped its app id meanwhile, and one
  whose package is still unknown then is held again, as the new head, unless
  only mapped packages owed its app id an answer when it came (see "Lines
  that went on before an answer").
- The head of the hold goes on when its package becomes known (a lookup
  mapped its uid, or put it in the negative cache), when a lookup changes the
  verdict to one that no longer depends on the package, or else when its
  ``package_hold`` (counted from its arrival) expired or a lookup it waited
  for failed: then it is evaluated with ``package=None`` and counted in
  ``stats.unattributed``. A connection's end, a detached consumer and the
  run's conclusion release every held line the same way (the core then
  passes ``force``); a kill or a failed run makes the core discard and count
  them instead (`discard`).
- Held lines count against the outbox (5.10: the core reports `held` and
  `held_bytes` to `logpyt._core.mailbox.Admission`).

`evaluate` decides with three values and never calls a user condition that
plain evaluation would not call (`And` and `Or` short-circuit from left to
right, `logpyt.filters`). A `Package` leaf (or a `Where` of one) is unknown
when it names a package the line may belong to, else false. The other
built-in field and event conditions (`Tag`, `Level`, `MinLevel`,
`MessageContains`, `CrashCondition`, `AnrCondition`, and a `Where` of one) are
documented to change nothing, so they are called wherever the walk reaches
them, also where plain evaluation would not. Any other leaf (a user
subclass, `Where` of a function, a combination whose class overrides
``__call__`` or ``packages()``) is unknown without a call when its
``packages()`` names a package the line may belong to, or when an `And` or
`Or` above it went on past an unknown verdict to reach it: once the package
is known, that verdict may decide the junction before the leaf is reached.
Else it is called with the line as it is (``package=None``): by the contract
of `logpyt.filters.Condition.packages`, a condition that matches on
``entry.package`` names the packages it matches. So ``Package(a) &
Where(fn)`` holds a line from an unknown app uid without calling ``fn``, and
``fn`` runs at its release only if the line is ``a``'s.

A line that may wait for its package (it comes from an unknown app uid that
a package without a uid may own), whether it goes on at once, is held or
queues behind a held line, calls each leaf at most once, its evaluation at
its release included (each such line keeps the verdicts of the leaves it
called, and is evaluated by `evaluate` at its release too), and a subtree
shared by several parents is evaluated at most twice for it (reached past an
unknown verdict, and not). Any other line is evaluated by a plain
``where(entry)``, or by `evaluate` when it is attributed to a uid that
several watched packages share, except one that goes on without its package
while watched packages still owe its app id an answer (see "Lines that went
on before an answer"). A combination whose class overrides ``packages()`` is
one leaf, as `Condition.packages` does not look inside it either. And/Or
decide like Kleene logic and stop at the first deciding verdict. The tree is
walked without recursion (F7-4).

Lines that went on before an answer
-----------------------------------

A line from an unknown app uid that only mapped packages still owe an answer
goes on at once (D-8), with ``package=None``; if a later answer names its app
id, it went on without its package. Such lines are not held, but counted:

- A line from an unknown app uid that goes on with ``package=None``, and is
  not counted as unattributed already (an expiry, a failed lookup, ``force``),
  records the watched packages that still owe its app id an answer (none
  issued after the app id was first seen) and that its verdict depends on:
  `evaluate` with that package unknown is None. A package without a uid that
  the hold's verdict already took as unknown stays unknown in that
  evaluation, and is not recorded (the verdict was decided without it),
  unless the line called a leaf that names it. A held line's packages
  without a uid can grow: a mapped package whose re-lookup was issued before
  the app id was first seen answers that it is not installed. A leaf that
  names it, called before that while it was mapped, saw ``package=None``, and
  the hold reuses that call, which can decide the verdict. Such a package is
  asked about like a mapped one (``reopened``), and recorded if the verdict
  depends on it, so the answer that maps it to the app id counts the line.
  This covers a line that goes on at once, one queued behind a held line
  (evaluated when it reaches the head), a held line that goes on because
  the packages without a uid answered, and a line under ``force``.
- A held line that takes its package at its release (a lookup mapped its
  app id while it waited) reuses the leaves it called while it waited. One
  that names its package was called with ``package=None`` while that
  package was mapped elsewhere (a reinstall found while the line waited for
  a package without a uid), so its verdict may be wrong for the line. When
  the line's verdict depends on such a call (`evaluate` with those leaves
  unknown and nothing called anew is None), the line goes on with its
  verdict as it is and counts as unattributed then (`Verdict.unattributed`).
- A line that only mapped packages owed when it came is never held (D-8),
  also when it queues behind a held line. At the head it is evaluated with
  what is known then, like every queued line: if a lookup mapped its app id
  while it waited, it goes on with its package (a plain verdict, or with
  the owners of a shared uid) and is not counted; if its app id is in the
  negative cache now, by a plain verdict; else without its package, and it
  records what it depends on, as above. An app id that the LRU evicted
  while the line waited is seen anew at the head and owes every watched
  package an answer: the line still goes on, not held, and records what it
  depends on.
- An answer for package ``p`` settles ``p`` for the recorded lines of every
  app id. When it names the app id, the lines that depend on ``p`` were
  ``p``'s: `Answer.unattributed` counts them, and they are forgotten.
  Otherwise, when it was issued after the app id was first seen, ``p`` is
  struck from what they depend on, and a line left with nothing is forgotten:
  its verdict was right. A failed lookup settles nothing, so the next answer
  does. Each line is therefore counted once at most, also when several
  watched packages answer the same app id one after another (a
  ``sharedUserId``): a line that depends on both is counted at the first such
  answer and gone at the second, one that depends on the second only is
  counted then.
- The count changes no verdict, the hold or the lookups. Such a line's
  verdict is `_decided` with ``package=None`` and the leaves it called
  already (plain evaluation's order, each leaf called once), or the hold's
  tri-state verdict. What it depends on is then evaluated with the same
  calls, one package unknown at a time (all of them first: when that is
  decided, so is each alone). A leaf already called is reused unless it
  names the package asked about, as its call saw ``package=None``
  (``reopened``); a user leaf that the verdict did not call is unknown, not
  called (``called_only``). (The hold's walk may leave a shared leaf
  uncalled where it first reaches it and call it later; reusing that call,
  this walk can reach leaves the verdict never reached.) So no user leaf is
  called twice, and none that the line's verdict did not call, so none that
  plain evaluation would not call; the same holds for a held line that
  takes its package at its release. Only lines from an app id that still
  owes a mapped package an answer pay for these evaluations, until it
  answers, and a line that the hold's verdict decides with a stale call
  (for the package that call names). Each line that the hold's verdict
  decides, and each held line that takes its package at its release, also
  pays one pass over its calls to find the stale ones; with one, the latter
  pays one evaluation more.
- Memory (5.10): an app id keeps one count per distinct set of packages its
  lines depend on (a subset of the watched packages), while it is in the LRU
  or in the map; its counts go when the LRU evicts it or the map drops it,
  and `discard` and `close` drop them all.

Caveats (documented, design 5.8):

- Packages that share a uid (``sharedUserId``) match each other's lines: a
  `Package` (or a `Where` of one) that names any of the watched packages on
  that uid matches them. ``LogEntry.package`` names the first of them in
  alphabetical order, and that is what a condition that reads
  ``entry.package`` itself sees, a `Package` inside a combination whose
  class overrides ``packages()`` included (that combination is called as a
  whole). A line that goes on with ``package=None`` matches none of them
  (one from the negative cache or a system uid, queued behind a held line
  while a lookup gave that uid to them).
- A watched package whose uid is a system uid (below 10000) gets one WARNING:
  every line of that uid, whatever its process, is attributed to it.
- Isolated processes (app ids 90000-99999, A.14) are never attributed, and
  their lines are never held and ask for no lookup; lines of other apps'
  processes are never attributed either: their uid names no watched package.
- An app id in the negative cache asks for no lookup any more (the cache has
  no time-to-live): a watched package that an install during the capture
  gives that very app id is found only when a line from another app uid not
  yet in the cache asks for a lookup, and its lines before that go on
  without their package (also those queued behind a held line when that
  lookup answers). A reinstall under a new app id is found by the re-lookup
  its first line asks for, but with every watched package mapped nothing is
  held, so the lines before that lookup answered go on without their
  package. Those whose verdict depended on it count in ``stats.unattributed``
  when that answer comes, not when they go on (an answer that comes while
  the run stops still counts them); those still waiting for it at the run's
  conclusion (the re-lookup never answered), or whose app id the LRU evicted
  after they went on, are not counted. What a line depends on is asked one
  package at a time, so a verdict that only two packages of a shared uid
  change together (``Package(a) & Package(b)``) is not counted.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from logpyt._core.grouping import entry_bytes
from logpyt.adb import _package_uid_argv, _parse_package_uid
from logpyt.exceptions import AdbError
from logpyt.filters import (
    AnrCondition,
    Condition,
    CrashCondition,
    Level,
    MessageContains,
    MinLevel,
    Not,
    Or,
    Package,
    Tag,
    Where,
    _attributed_packages,
    _combines,
    _Junction,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from logpyt.adb import _AdbResult
    from logpyt.models import LogEntry

__all__ = [
    "LOOKUP_INTERVAL",
    "UID_CACHE",
    "Answer",
    "Attribution",
    "Lookup",
    "Verdict",
    "declared_packages",
    "evaluate",
]

# Design 5.8: a uid is user * 100000 + app id; app ids start at 10000.
_PER_USER: Final = 100_000
_FIRST_APP_ID: Final = 10_000
# A.14: isolated processes from here on (99000-99999 before API 29 is inside);
# no lookup names one, as dumpsys gives an app's own uid.
_FIRST_ISOLATED_ID: Final = 90_000
# Design 5.8 (X-3): lookups of one package start at least this far apart.
LOOKUP_INTERVAL: Final = 0.5
# Design 5.10: the uids the map and the negative cache remember, together.
UID_CACHE: Final = 4096
# `_Held.seen` of a line whose package was known when it arrived.
_KNOWN: Final = -1
_NOBODY: Final[frozenset[str]] = frozenset()
# `Attribution._question`: the app id, its seen, and the packages to ask a
# line's verdict about.
type _Question = tuple[int, int, frozenset[str]]
# The ``__call__`` of each built-in condition that `evaluate` may call where
# plain evaluation would not have: documented to change nothing
# (`logpyt.filters.Condition`). A `Package` is decided without a call (`_leaf`).
_PURE_CALLS: Final = frozenset(
    {
        Tag.__call__,
        Level.__call__,
        MinLevel.__call__,
        MessageContains.__call__,
        CrashCondition.__call__,
        AnrCondition.__call__,
    }
)


@dataclass(frozen=True, slots=True)
class Lookup:
    """A uid lookup the core runs as ``RunCommand(kind="lookup")``."""

    ident: int  # the request id; its result is taken only while it is in flight
    package: str
    argv: tuple[str, ...]
    first: bool = False  # one of the lookups at start (intake waits for them)


@dataclass(frozen=True, slots=True)
class Answer:
    """What a lookup result changed, for the core (`Attribution.lookup_done`)."""

    warnings: tuple[str, ...] = ()
    # Lines that went on without their package, which this answer named
    # (design 4.4: the core counts them in stats.unattributed).
    unattributed: int = 0


_NOTHING: Final = Answer()


@dataclass(frozen=True, slots=True)
class Verdict:
    """A line that goes on: its entry (attributed), whether ``where`` matched.

    ``unattributed``: it was evaluated with ``package=None`` although its
    verdict depended on a package that stayed unknown, or it took its
    package at its release but its verdict depended on a leaf it called
    with ``package=None`` while it waited (design 4.4, module docs).
    """

    entry: LogEntry
    matched: bool
    unattributed: bool = False


@dataclass(slots=True, eq=False)
class _Held:
    """A line in the hold."""

    entry: LogEntry
    arrived: float
    # _KNOWN, or the number of lookups issued when its app id was first seen.
    seen: int
    nbytes: int
    # The verdicts of the leaves it called; None for a line that may not wait
    # for its package (it calls none before its release, `Attribution._settled`).
    calls: dict[int, bool] | None = None
    given_up: bool = False  # a lookup it waited for failed
    # Its app id's seen when it came from an unknown app uid, also when it
    # may not wait (seen is _KNOWN then): it is resolved again at the head
    # (`Attribution.release`). Else _KNOWN.
    since: int = _KNOWN


@dataclass(slots=True, eq=False)
class _Owing:
    """The lines of one app id that went on without their package while
    watched packages still owed it an answer (module docs).
    """

    seen: int  # the number of lookups issued when the app id was first seen
    # The packages a line's verdict depends on -> how many lines.
    lines: dict[frozenset[str], int]


class Attribution:
    """The attribution state of one run (see the module docs).

    Args:
        where: The stream's condition; its ``packages()`` are watched.
        adb_path: The resolved adb executable.
        serial: The device serial, or None.
        package_hold: The longest a line waits for its package, in seconds;
            also how long intake waits for the lookups at start.

    Raises:
        ValueError: A name ``where.packages()`` returns is not a package name
            (F5-3), a process name (``<package>:<process>``) included.
        TypeError: Such a name is not a str, or a ``packages()`` override
            returns one str (or bytes) instead of a collection of names.

    """

    __slots__ = (
        "_adb_path",
        "_answered",
        "_closed",
        "_declared",
        "_first",
        "_first_left",
        "_flight",
        "_held_bytes",
        "_hold",
        "_issued",
        "_last_issue",
        "_owing",
        "_package_of",
        "_packages",
        "_passed",
        "_queue",
        "_ready",
        "_seen",
        "_serial",
        "_shared",
        "_uid_of",
        "_unmapped",
        "_until",
        "_wanted",
        "_warned_failure",
        "_warned_system",
        "_watched",
        "_where",
    )

    def __init__(
        self,
        where: Condition | None,
        *,
        adb_path: str,
        serial: str | None,
        package_hold: float,
    ) -> None:
        packages = sorted(() if where is None else _attributed_packages(where))
        self._packages = tuple(packages)
        self._where = where
        self._watched = bool(packages)
        self._adb_path = adb_path
        self._serial = serial
        self._hold = package_hold
        self._declared = (
            declared_packages(where) if packages and where is not None else {}
        )
        # Lookups: the first ones in order, then re-lookups; one in flight.
        self._first: deque[str] = deque(packages)
        self._first_left = len(packages)
        self._wanted: set[str] = set()
        # Wanted packages that were due when another one was chosen: since when.
        self._passed: dict[str, float] = {}
        self._flight: Lookup | None = None
        self._issued = 0
        self._last_issue: dict[str, float] = {}
        self._answered: dict[str, int] = {}  # package -> its last answer's id
        self._warned_failure = False
        self._warned_system: set[str] = set()
        self._ready = not packages
        self._until: float | None = None
        self._closed = False
        # The uid map, by app id; the negative cache and the app ids waiting
        # for an answer: app id -> None (no watched package) or `_Held.seen`.
        self._unmapped: set[str] = set(packages)
        self._uid_of: dict[str, int] = {}
        self._package_of: dict[int, str] = {}
        # App id -> its watched packages, for an app id that several share.
        self._shared: dict[int, frozenset[str]] = {}
        self._seen: OrderedDict[int, int | None] = OrderedDict()
        # App id -> its lines that went on before an answer (module docs).
        self._owing: dict[int, _Owing] = {}
        # The hold.
        self._queue: deque[_Held] = deque()
        self._held_bytes = 0

    # -- state -----------------------------------------------------------------------

    @property
    def watching(self) -> bool:
        """Whether any package is watched."""
        return self._watched

    @property
    def ready(self) -> bool:
        """Whether intake may open: the lookups at start returned, or their wait
        expired.
        """
        return self._ready

    @property
    def held(self) -> int:
        """Lines in the hold (the head and the lines queued behind it)."""
        return len(self._queue)

    @property
    def held_bytes(self) -> int:
        """Their size, as `logpyt._core.grouping.entry_bytes` counts it."""
        return self._held_bytes

    @property
    def uid_packages(self) -> dict[int, str]:
        """A copy of the uid map: app id -> package."""
        return dict(self._package_of)

    @property
    def remembered(self) -> int:
        """The uids the map and the negative cache hold (at most `UID_CACHE`)."""
        return len(self._package_of) + len(self._seen)

    @property
    def unsettled(self) -> int:
        """Lines that went on without their package and wait for an answer
        that settles them (module docs).
        """
        return sum(sum(owing.lines.values()) for owing in self._owing.values())

    def negative(self, uid: int) -> bool:
        """Whether ``uid``'s app id is known to name no watched package (tests).

        True when it is in the negative cache, or when every watched package
        answered after it was first seen (it moves into the cache when its
        next line comes).
        """
        state = self._seen.get(uid % _PER_USER, _KNOWN)
        return state is None or (state != _KNOWN and not self._owed(state))

    # -- the timer -------------------------------------------------------------------

    def begin(self, now: float) -> None:
        """The run starts: the wait for the lookups at start runs from ``now``."""
        if not self._ready:
            self._until = now + self._hold

    def deadline(self) -> float | None:
        """When ``hold_expired`` fires; None when nothing waits.

        The earliest of: the end of the wait for the lookups at start, the
        expiry of the line at the head of the hold, and the time a wanted
        re-lookup may start.
        """
        times: list[float] = []
        if not self._ready and self._until is not None:
            times.append(self._until)
        if self._queue:
            times.append(self._queue[0].arrived + self._hold)
        if self._flight is None and self._wanted:
            times.append(min(map(self._due, self._wanted)))
        return min(times) if times else None

    def expire(self, now: float) -> None:
        """``hold_expired`` at ``now``: intake opens once the start's wait is over.

        Held lines expire in `release`, and a due re-lookup starts in
        `next_lookup`.
        """
        if not self._ready and self._until is not None and now >= self._until:
            self._ready = True

    # -- lookups ---------------------------------------------------------------------

    def next_lookup(self, now: float) -> Lookup | None:
        """The lookup to run now, or None (one in flight; re-lookups rate limited)."""
        if self._flight is not None or self._closed:
            return None
        first = bool(self._first)
        if first:
            package = self._first.popleft()
        else:
            due = [name for name in self._wanted if self._due(name) <= now]
            if not due:
                return None
            package = min(due, key=lambda name: self._rank(name, now))
            self._wanted.discard(package)
            passed = self._passed
            passed.pop(package, None)
            for name in due:  # the others waited through this choice
                if name != package:
                    passed.setdefault(name, now)
        self._issued += 1
        argv = _package_uid_argv(self._adb_path, self._serial, package)
        lookup = Lookup(self._issued, package, argv, first=first)
        self._flight = lookup
        self._last_issue[package] = now
        return lookup

    def lookup_done(self, ident: int, result: _AdbResult | Exception) -> Answer:
        """Take the result of lookup ``ident``: the WARNINGs to log, and the
        lines that went on without the package it names.

        A result whose id is not the lookup in flight is stale and changes
        nothing (X-3). A failure releases the held lines that waited for it
        (counted as unattributed by `release`); one WARNING per run says so.
        An answer settles its package for the lines that went on while it was
        owed (module docs); a failure settles nothing.
        """
        flight = self._flight
        if flight is None or flight.ident != ident:
            return _NOTHING
        self._flight = None
        if flight.first:
            self._first_left -= 1
            if not self._first_left:
                self._ready = True
        package = flight.package
        error: Exception | None = None
        uid: int | None = None
        if isinstance(result, Exception):
            error = result
        else:
            try:
                uid = _parse_package_uid(result, package)
            except AdbError as exc:
                error = exc
        if error is not None:
            self._give_up(flight)
            if self._warned_failure:
                return _NOTHING
            self._warned_failure = True
            warning = (
                f"package lookup for {package} failed: {error}; lines whose "
                "where verdict needs it are evaluated without a package and "
                "counted in stats.unattributed"
            )
            return Answer((warning,))
        self._answered[package] = flight.ident
        counted = self._settle(package, flight.ident, uid)
        return Answer(tuple(self._map(package, uid)), counted)

    # -- lines -----------------------------------------------------------------------

    def admit(
        self, entry: LogEntry, now: float, *, force: bool = False
    ) -> Verdict | None:
        """Take a line before ``where``; return its verdict, or None if it is held.

        ``force``: never hold it (nobody waits for it: a detached consumer).

        Raises:
            Exception: What ``where`` raised; the line is not held then.

        """
        package, seen = self._resolve(entry.uid)
        if package is not None and package != entry.package:
            # The values are checked already, so no validation is lost (A.16).
            entry = entry.model_copy(update={"package": package})
        since = seen
        if seen != _KNOWN:
            self._want(seen)
            if not self._candidates(seen):
                # Only mapped packages are asked again (a reinstall): such a
                # line is never held (D-8), and goes on without its package.
                seen = _KNOWN
        if self._queue:
            calls = None if seen == _KNOWN else {}  # no leaf twice (module docs)
            nbytes = entry_bytes(entry)
            self._push(_Held(entry, now, seen, nbytes, calls, since=since))
            return None
        if seen == _KNOWN:
            return self._settled(entry, since, None)
        line = _Held(entry, now, seen, entry_bytes(entry), since=since)
        verdict = self._judge(line, now, force=force)
        if verdict is None:
            self._push(line)
        return verdict

    def release(self, now: float, *, force: bool = False) -> Verdict | None:
        """The head of the hold if it may go on now, else None (one per call).

        ``force``: it goes on whatever is known (a connection end, a detached
        consumer, the run's conclusion).

        A line from an unknown app uid is resolved again: it takes its
        package if a lookup mapped its app id while it waited. One that may
        not wait for its package when it came is never held (D-8).

        Raises:
            Exception: What ``where`` raised; that line has left the hold.

        """
        if not self._queue:
            return None
        line = self._pop()
        if line.since != _KNOWN:  # it came from an unknown app uid
            package, seen = self._resolve(line.entry.uid)
            if seen == _KNOWN or not self._candidates(seen):
                line.seen = _KNOWN
                if package is not None and package != line.entry.package:
                    line.entry = line.entry.model_copy(update={"package": package})
        verdict = self._judge(line, now, force=force)
        if verdict is None:
            self._queue.appendleft(line)
            self._held_bytes += line.nbytes
        return verdict

    def discard(self) -> int:
        """Drop the held lines and the wanted re-lookups; return how many lines.

        The lines that went on before an answer are dropped too, uncounted.
        """
        count = len(self._queue)
        self._queue.clear()
        self._held_bytes = 0
        self._wanted.clear()
        self._passed.clear()
        self._owing.clear()
        return count

    def close(self) -> None:
        """The run concluded: no lookup runs or is waited for any more (G5).

        The lines that went on before an answer that never came are dropped,
        uncounted.
        """
        self._closed = True
        self._flight = None
        self._first.clear()
        self._wanted.clear()
        self._passed.clear()
        self._owing.clear()
        self._ready = True

    # -- helpers ---------------------------------------------------------------------

    def _due(self, package: str) -> float:
        return self._last_issue.get(package, float("-inf")) + LOOKUP_INTERVAL

    def _rank(self, package: str, now: float) -> tuple[int, float, str]:
        """The order of the due re-lookups at ``now`` (`next_lookup`), lowest first.

        A package that choices have passed over for ``package_hold`` goes
        first, so none starves; then a package without a uid (lines may wait
        for it); then the others. Within each, the longest since its last
        lookup goes first (so packages without a uid take turns), then by name.
        """
        last = self._last_issue.get(package, float("-inf"))
        since = self._passed.get(package)
        if since is not None and now - since >= self._hold:
            return 0, last, package
        return (1 if package in self._unmapped else 2), last, package

    def _resolve(self, uid: int | None) -> tuple[str | None, int]:
        """``(package, seen)`` of a line's uid; ``seen`` is `_KNOWN` when known."""
        if uid is None:
            return None, _KNOWN
        app = uid % _PER_USER
        package = self._package_of.get(app)
        if package is not None:
            return package, _KNOWN
        if app < _FIRST_APP_ID or app >= _FIRST_ISOLATED_ID:
            return None, _KNOWN  # a system uid, or an isolated process
        seen = self._seen
        state = seen.get(app, _KNOWN)
        if state is None:
            seen.move_to_end(app)
            return None, _KNOWN
        if state == _KNOWN:
            state = self._issued
            seen[app] = state
            self._trim()
        else:
            seen.move_to_end(app)
        if not self._owed(state):
            # Every watched package answered after this one was first seen,
            # and none named it.
            if app in seen:
                seen[app] = None
            return None, _KNOWN
        return None, state

    def _candidates(self, seen: int) -> frozenset[str]:
        """The packages without a uid that a uid first seen at ``seen`` may
        still belong to: what a line from it waits for.
        """
        answered = self._answered
        return frozenset(
            name for name in self._unmapped if answered.get(name, 0) <= seen
        )

    def _owed(self, seen: int) -> list[str]:
        """The watched packages, mapped or not, that have no answer issued after
        a uid first seen at ``seen``: the lookups that would settle it.
        """
        answered = self._answered
        return [name for name in self._packages if answered.get(name, 0) <= seen]

    def _want(self, seen: int) -> None:
        """Ask for the lookups that would settle a uid first seen at ``seen``."""
        if self._closed:
            return
        flight = self._flight
        for name in self._owed(seen):
            if flight is not None and flight.package == name and flight.ident > seen:
                continue  # in flight, issued after the uid was seen
            if name in self._first:
                continue  # its first lookup is still to come
            self._wanted.add(name)

    def _give_up(self, flight: Lookup) -> None:
        """A lookup failed: the held lines that waited for it go on unattributed."""
        for line in self._queue:
            if (
                line.seen != _KNOWN
                and line.seen < flight.ident
                and flight.package in self._candidates(line.seen)
            ):
                line.given_up = True

    def _map(self, package: str, uid: int | None) -> list[str]:
        """``package`` has ``uid``, or is not installed (None): update the map.

        A package that had another app id moves (a reinstall); one that is
        not installed any more has no uid again. Returns the WARNINGs to log.
        """
        warnings: list[str] = []
        app = None if uid is None else uid % _PER_USER
        old = self._uid_of.pop(package, None)
        if app is None:
            self._unmapped.add(package)
        else:
            if app < _FIRST_APP_ID and package not in self._warned_system:
                self._warned_system.add(package)
                warnings.append(
                    f"package {package} has the system uid {app}: every line of "
                    f"uid {app}, whatever its process, is attributed to {package}"
                )
            self._uid_of[package] = app
            self._unmapped.discard(package)
            self._seen.pop(app, None)
        for key in {key for key in (app, old) if key is not None}:
            owners = sorted(
                name for name, value in self._uid_of.items() if value == key
            )
            if owners:
                self._package_of[key] = owners[0]
            else:
                self._package_of.pop(key, None)
                if key not in self._seen:
                    self._owing.pop(key, None)  # neither in the map nor the LRU
            if len(owners) > 1:
                self._shared[key] = frozenset(owners)
            else:
                self._shared.pop(key, None)
        self._trim()
        return warnings

    def _settle(self, package: str, ident: int, uid: int | None) -> int:
        """Answer ``ident`` gave ``package`` ``uid`` (None: not installed):
        settle it for the lines that went on while it was owed.

        Returns how many lines it names: their app id is ``uid``'s and their
        verdict depends on ``package`` (module docs).
        """
        app = None if uid is None else uid % _PER_USER
        counted = 0
        for key in list(self._owing):
            owing = self._owing[key]
            lines = owing.lines
            depending = [names for names in lines if package in names]
            if key == app:
                for names in depending:
                    counted += lines.pop(names)
            elif ident > owing.seen:  # it says the app id is not package's
                for names in depending:
                    count = lines.pop(names)
                    rest = names - {package}
                    if rest:
                        lines[rest] = lines.get(rest, 0) + count
            if not lines:
                del self._owing[key]
        return counted

    def _trim(self) -> None:
        """Keep the map and the negative cache within `UID_CACHE` uids (LRU).

        An app id evicted takes its lines that went on before an answer with
        it (5.10).
        """
        room = max(0, UID_CACHE - len(self._package_of))
        seen = self._seen
        while len(seen) > room:
            app, _ = seen.popitem(last=False)
            self._owing.pop(app, None)

    def _sharing(self, entry: LogEntry) -> frozenset[str]:
        """The watched packages that share ``entry``'s app id, if several do
        and the line is theirs: a line that goes on with ``package=None`` (it
        came from the negative cache or a system uid that a lookup gave to
        them while it was queued) matches no `Package` leaf.
        """
        uid = entry.uid
        if uid is None or entry.package is None or not self._shared:
            return _NOBODY
        return self._shared.get(uid % _PER_USER, _NOBODY)

    def _plain(self, entry: LogEntry, calls: dict[int, bool] | None) -> bool:
        """The verdict of a line whose package is settled (maybe None).

        A plain ``where(entry)``, unless the line may have waited for its
        package (``calls`` is not None: the verdicts of the leaves it called
        while its package was unknown are reused, and no leaf is called
        twice) or it is attributed to a uid that several watched packages
        share (each `Package` leaf matches any of them, `_sharing`).
        """
        where = self._where
        assert where is not None  # noqa: S101  (attribution needs a condition)
        owners = self._sharing(entry)
        if calls is None and not owners:
            return bool(where(entry))
        if calls is None:
            calls = {}
        return _decided(where, entry, calls, self._declared, owners)

    def _known(self, entry: LogEntry, calls: dict[int, bool] | None) -> Verdict:
        """The verdict of a line whose package is settled (maybe None): `_plain`.

        A held line that takes its package at its release reuses the leaves
        it called while it waited, with ``package=None``. One of them that
        names its package was called while that package was not one the line
        may belong to (mapped elsewhere: a reinstall), so its verdict may be
        wrong for the line. When the line's verdict depends on such a call
        (`evaluate` with those leaves unknown, calling nothing new, is None),
        the line is unattributed; its verdict stays, and no leaf is called
        again (module docs).
        """
        package = entry.package
        if not calls or package is None:
            return Verdict(entry, self._plain(entry, calls))
        stale = _calls_naming(calls, package, self._declared)  # before new calls
        matched = self._plain(entry, calls)
        if not stale:
            return Verdict(entry, matched)
        where = self._where
        assert where is not None  # noqa: S101  (attribution needs a condition)
        verdict = evaluate(
            where,
            entry,
            unknown=_NOBODY,
            calls={key: value for key, value in calls.items() if key not in stale},
            declared=self._declared,
            owners=self._sharing(entry),
            called_only=True,
        )
        return Verdict(entry, matched, unattributed=verdict is None)

    def _settled(
        self, entry: LogEntry, since: int, calls: dict[int, bool] | None
    ) -> Verdict:
        """The verdict of a line that does not wait for its package (any more).

        ``since`` is `_Held.since`. A line from an unknown app uid that
        watched packages still owe an answer goes on without its package: its
        verdict is `_decided` with its leaf calls (each leaf once), and what
        it depends on is recorded (`_owe`). Any other line takes `_known`.
        """
        question = self._question(entry, since, _NOBODY)
        if question is None:
            return self._known(entry, calls)
        where = self._where
        assert where is not None  # noqa: S101  (attribution needs a condition)
        if calls is None:
            calls = {}
        matched = _decided(where, entry, calls, self._declared)
        self._owe(entry, calls, question, _NOBODY)
        return Verdict(entry, matched)

    def _question(
        self, entry: LogEntry, since: int, asked: frozenset[str]
    ) -> _Question | None:
        """What to ask about a line that goes on without its package, if
        anything (module docs).

        ``since``: its `_Held.since`; ``asked``: the packages without a uid
        that its verdict took as unknown already. None for a line whose
        package is known, that came from a known uid, whose app id is in the
        negative cache now (`admit` and `release` have just resolved it), or
        that nothing is asked about.
        """
        uid = entry.uid
        if since == _KNOWN or uid is None or entry.package is not None:
            return None
        if self._closed:
            # No answer comes any more. The core never gets here: it closes
            # the attribution after its last line went on; a line admitted
            # after `close` records nothing.
            return None
        app = uid % _PER_USER
        seen = self._seen.get(app)
        if seen is None:
            return None
        names = frozenset(self._owed(seen)).difference(asked)
        return (app, seen, names) if names else None

    def _owe(
        self,
        entry: LogEntry,
        calls: dict[int, bool],
        question: _Question,
        asked: frozenset[str],
    ) -> None:
        """Record what a line that goes on without its package depends on.

        ``calls``: the verdicts of the leaves it called; ``question`` from
        `_question`, with the same ``asked``. It records the packages of the
        question that its verdict depends on, for their answers to settle
        (`_settle`).
        """
        app, seen, names = question
        depends = self._depends(entry, calls, names, asked)
        if depends:
            owing = self._owing.get(app)
            if owing is None:
                owing = self._owing[app] = _Owing(seen, {})
            owing.lines[depends] = owing.lines.get(depends, 0) + 1

    def _depends(
        self,
        entry: LogEntry,
        calls: dict[int, bool],
        names: frozenset[str],
        asked: frozenset[str],
    ) -> frozenset[str]:
        """The packages of ``names`` that ``entry``'s verdict depends on.

        Tri-state: `evaluate` with the package unknown, besides ``asked``, is
        None. It reuses ``calls`` and calls no user leaf that the verdict did
        not call: such a leaf is unknown (`evaluate`, ``reopened``). All of
        ``names`` are asked first: when that is decided, so is each one alone
        (an unknown leaf more never decides a verdict).
        """
        if not names or not self._undecided(entry, calls, names, asked):
            return _NOBODY
        if len(names) == 1:
            return names
        return frozenset(
            name
            for name in sorted(names)
            if self._undecided(entry, calls, frozenset((name,)), asked)
        )

    def _undecided(
        self,
        entry: LogEntry,
        calls: dict[int, bool],
        names: frozenset[str],
        asked: frozenset[str],
    ) -> bool:
        """Whether `evaluate` leaves ``entry``'s verdict undecided with
        ``names`` (reopened) and ``asked`` unknown.
        """
        where = self._where
        assert where is not None  # noqa: S101  (attribution needs a condition)
        verdict = evaluate(
            where,
            entry,
            unknown=asked | names,
            calls=calls,
            declared=self._declared,
            reopened=names,
        )
        return verdict is None

    def _judge(self, line: _Held, now: float, *, force: bool) -> Verdict | None:
        """The verdict of a line at the head of the hold, or None if it must wait."""
        where = self._where
        assert where is not None  # noqa: S101  (attribution needs a condition)
        entry = line.entry
        calls = line.calls
        if line.seen == _KNOWN:
            return self._settled(entry, line.since, calls)
        if calls is None:
            calls = line.calls = {}
        candidates = self._candidates(line.seen)
        verdict = evaluate(
            where, entry, unknown=candidates, calls=calls, declared=self._declared
        )
        if verdict is not None:
            # A package that a leaf it called names grew into the candidates
            # after that call (`_stale`): it is asked about, not taken as
            # unknown (module docs).
            asked = candidates - _stale(calls, candidates, self._declared)
            question = self._question(entry, line.since, asked)
            if question is not None:
                self._owe(entry, calls, question, asked)
            return Verdict(entry, verdict)
        if force or line.given_up or now >= line.arrived + self._hold:
            matched = _decided(where, entry, calls, self._declared)
            return Verdict(entry, matched, unattributed=True)
        return None

    def _push(self, line: _Held) -> None:
        self._queue.append(line)
        self._held_bytes += line.nbytes

    def _pop(self) -> _Held:
        line = self._queue.popleft()
        self._held_bytes -= line.nbytes
        return line


# -- tri-state evaluation ----------------------------------------------------------


@dataclass(slots=True, eq=False)
class _Frame:
    """A junction or a `Not` on the way down (`evaluate`)."""

    node: Condition
    children: tuple[Condition, ...] | None  # None: a Not
    # Reached past an unknown verdict of an And or Or above it.
    guarded: bool = False
    index: int = 1
    decisive: bool = False
    undecided: bool = False


def _is_combination(node: Condition) -> bool:
    """Whether `evaluate` takes ``node`` apart instead of treating it as a leaf.

    An `And`, `Or` or `Not` with its built-in ``__call__``, or a `Where` of
    one (`logpyt.filters._combines`), whose class keeps the built-in
    ``packages()``: an override says what the subtree depends on, and the walk
    cannot see why, so the subtree is one leaf.
    """
    return _combines(node) and type(node).packages is Condition.packages


def _unwrap(node: Condition) -> Condition:
    """``node`` without the `Where` wrappers whose built-in ``__call__`` passes a
    condition's verdict on: ``Where(c)`` matches like ``c``.
    """
    while (
        type(node).__call__ is Where.__call__
        and isinstance(node, Where)
        and isinstance(node.fn, Condition)
    ):
        node = node.fn
    return node


def _is_package(node: Condition) -> bool:
    """A `Package` leaf with its built-in ``__call__``: ``entry.package in values``.

    ``node`` is already `_unwrap`-ped: a `Where` of a `Package` matches like it.
    """
    return type(node).__call__ is Package.__call__ and isinstance(node, Package)


def _is_pure(node: Condition) -> bool:
    """Whether ``node`` is a built-in field or event condition, or a `Where` of
    one, with its built-in ``__call__``: calling it changes nothing, so
    `evaluate` may call it where plain evaluation would not have.
    """
    return type(_unwrap(node)).__call__ in _PURE_CALLS


def declared_packages(root: Condition) -> dict[int, frozenset[str]]:
    """The packages each leaf of ``root`` depends on, by ``id()`` of the leaf.

    For every leaf `evaluate` reaches but a `Package` (or a `Where` of one):
    what its ``packages()`` returns, as package names. Computed once per
    stream, so an override of ``packages()`` is asked once more; a shared
    subtree is walked once, without recursion.
    """
    declared: dict[int, frozenset[str]] = {}
    walked: set[int] = set()
    pending: list[Condition] = [root]
    while pending:
        node = pending.pop()
        if id(node) in walked:
            continue
        walked.add(id(node))
        if _is_combination(node):
            if isinstance(node, _Junction):
                pending.extend(node.conditions)
            elif isinstance(node, Not):
                pending.append(node.condition)
            elif isinstance(node, Where) and isinstance(node.fn, Condition):
                pending.append(node.fn)
            continue
        if not _is_package(_unwrap(node)):
            declared[id(node)] = frozenset(node.packages())
    return declared


def evaluate(
    root: Condition,
    entry: LogEntry,
    *,
    unknown: frozenset[str],
    calls: dict[int, bool],
    declared: Mapping[int, frozenset[str]],
    owners: frozenset[str] = _NOBODY,
    reopened: frozenset[str] = _NOBODY,
    called_only: bool = False,
) -> bool | None:
    """``root``'s verdict for ``entry``: True, False, or None when it depends on
    the package (see the module docs).

    Args:
        root: The condition.
        entry: The line; its ``package`` is None while ``unknown`` is not empty.
        unknown: The watched packages the line may belong to; empty when its
            package is known.
        calls: The verdicts of the leaves already called for this line, by
            ``id()``; the leaves this evaluation calls are added. A leaf in it
            is not called again.
        declared: `declared_packages` of ``root``.
        owners: The watched packages that share the line's uid, when several
            do (``unknown`` is empty then): a `Package` leaf matches any of
            them.
        reopened: Packages of ``unknown`` whose leaves are unknown even when
            ``calls`` holds their verdict: it was taken with ``package=None``
            while the line was not thought to be theirs (what a line that
            went on depends on, `Attribution._depends`). When it is not
            empty, ``called_only`` holds too.
        called_only: No leaf is called that ``calls`` does not hold, but a
            built-in condition that changes nothing: any other is unknown
            (whether a line's verdict depends on some of its calls,
            `Attribution._known`).

    Raises:
        Exception: What a leaf raised.

    """
    # This evaluation's verdicts, by id() and whether the node was reached
    # past an unknown verdict: there a leaf may be left unknown uncalled.
    done: dict[tuple[int, bool], bool | None] = {}
    frames: list[_Frame] = []
    node: Condition | None = root
    guarded = False
    value: bool | None = None
    called_only = called_only or bool(reopened)
    context = (entry, unknown, calls, declared, owners, reopened, called_only)
    while node is not None:
        value = _down(node, frames, done, context, guarded=guarded)
        node, guarded, value = _up(frames, done, value=value)
    return value


type _Context = tuple[
    LogEntry,
    frozenset[str],
    dict[int, bool],
    Mapping[int, frozenset[str]],
    frozenset[str],
    frozenset[str],
    bool,
]


def _down(
    node: Condition,
    frames: list[_Frame],
    done: dict[tuple[int, bool], bool | None],
    context: _Context,
    *,
    guarded: bool,
) -> bool | None:
    """Down the leftmost path from ``node`` to a verdict, pushing its frames.

    A verdict reached without the guard serves a guarded visit too: it needs
    no new call.
    """
    while True:
        key = id(node)
        if (key, False) in done:
            return done[key, False]
        if guarded and (key, True) in done:
            return done[key, True]
        if _is_combination(node):
            if isinstance(node, _Junction):
                children = node.conditions
                if children:
                    frames.append(
                        _Frame(node, children, guarded, 1, isinstance(node, Or))
                    )
                    node = children[0]
                    continue
                value = not isinstance(node, Or)  # Filter() matches everything
                done[key, guarded] = value
                return value
            if isinstance(node, Not):
                frames.append(_Frame(node, None, guarded))
                node = node.condition
                continue
            if isinstance(node, Where) and isinstance(node.fn, Condition):
                node = node.fn  # Where(c) passes c's verdict on
                continue
        value = _leaf(node, *context, guarded=guarded)
        done[key, guarded] = value
        return value


def _up(
    frames: list[_Frame],
    done: dict[tuple[int, bool], bool | None],
    *,
    value: bool | None,
) -> tuple[Condition | None, bool, bool | None]:
    """Up to the first junction ``value`` does not decide.

    Returns:
        The junction's next child to evaluate, whether it is reached past an
        unknown verdict, and the verdict so far; or ``(None, False, verdict)``
        when the root is decided.

    """
    while frames:
        frame = frames[-1]
        children = frame.children
        if children is None:  # a Not
            frames.pop()
            value = None if value is None else not value
        elif value is frame.decisive:
            frames.pop()
        else:
            if value is None:
                frame.undecided = True
            if frame.index < len(children):
                frame.index += 1
                guarded = frame.guarded or frame.undecided
                return children[frame.index - 1], guarded, value
            frames.pop()
            value = None if frame.undecided else not frame.decisive
        done[id(frame.node), frame.guarded] = value
    return None, False, value


def _leaf(
    node: Condition,
    entry: LogEntry,
    unknown: frozenset[str],
    calls: dict[int, bool],
    declared: Mapping[int, frozenset[str]],
    owners: frozenset[str],
    reopened: frozenset[str],
    called_only: bool,  # noqa: FBT001  (one of the evaluation's fields)
    *,
    guarded: bool,
) -> bool | None:
    """One leaf's verdict (see `evaluate`)."""
    package = _unwrap(node)  # Where(Package(...)) sees every owner too
    if _is_package(package):
        assert isinstance(package, Package)  # noqa: S101  (for the type checker)
        if unknown:
            return False if package.values.isdisjoint(unknown) else None
        if owners:
            return not package.values.isdisjoint(owners)
        return entry.package in package.values
    key = id(node)
    # A call it made for a reopened package saw package=None (`evaluate`);
    # reopened is part of unknown, so _left_unknown leaves it unknown.
    if not reopened or _names(node, declared).isdisjoint(reopened):
        called = calls.get(key)
        if called is not None:
            return called
    # With called_only (reopened sets it), nothing is called that the line's
    # verdict did not call: such a leaf is unknown as if guarded
    # (`Attribution._depends`, `Attribution._known`).
    guarded = guarded or called_only
    if (unknown or called_only) and _left_unknown(
        node, unknown, declared, guarded=guarded
    ):
        return None
    value = bool(node(entry))
    calls[key] = value
    return value


def _left_unknown(
    node: Condition,
    unknown: frozenset[str],
    declared: Mapping[int, frozenset[str]],
    *,
    guarded: bool,
) -> bool:
    """Whether a leaf other than a `Package` stays unknown without a call.

    It names a package the line may belong to; or the walk reached it past
    an unknown verdict, where plain evaluation may never reach it (that
    verdict may decide the junction once the package is known), and it is
    not a built-in condition that changes nothing.
    """
    names = _names(node, declared)
    return not names.isdisjoint(unknown) or (guarded and not _is_pure(node))


def _names(node: Condition, declared: Mapping[int, frozenset[str]]) -> frozenset[str]:
    """The packages a leaf other than a `Package` depends on (`declared_packages`)."""
    names = declared.get(id(node))
    return frozenset(node.packages()) if names is None else names


def _stale(
    calls: Mapping[int, bool],
    candidates: frozenset[str],
    declared: Mapping[int, frozenset[str]],
) -> frozenset[str]:
    """The packages of ``candidates`` that a leaf in ``calls`` names.

    `evaluate` calls no leaf that names a package the line may belong to, so
    such a call was made before that package became one: while it was mapped,
    with ``package=None``. ``declared`` holds every leaf `evaluate` calls.
    One pass over ``calls``.
    """
    stale: set[str] = set()
    for key in calls:
        names = declared.get(key, _NOBODY)
        if not names.isdisjoint(candidates):
            stale.update(names & candidates)
    return frozenset(stale)


def _calls_naming(
    calls: Mapping[int, bool], package: str, declared: Mapping[int, frozenset[str]]
) -> frozenset[int]:
    """The leaves in ``calls`` (by id) that name ``package``, for a held line
    that took ``package`` at its release.

    It waited with ``package`` not one it may belong to (`evaluate` calls no
    leaf that names one), so they were called with ``package=None`` while
    ``package`` was mapped elsewhere. One pass over ``calls``.
    """
    return frozenset(key for key in calls if package in declared.get(key, _NOBODY))


def _decided(
    root: Condition,
    entry: LogEntry,
    calls: dict[int, bool],
    declared: Mapping[int, frozenset[str]],
    owners: frozenset[str] = _NOBODY,
) -> bool:
    """The verdict once the package is settled (``entry.package``, maybe None)."""
    verdict = evaluate(
        root, entry, unknown=frozenset(), calls=calls, declared=declared, owners=owners
    )
    assert verdict is not None  # noqa: S101  (nothing is unknown any more)
    return verdict
