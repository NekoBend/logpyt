"""Package attribution of a live stream (design 5.8, D-8, 5.10, A.21).

The stream core (`logpyt._core.machine`) calls this policy at the points of the
transition table (design 3.2): the first lookups at start, ``lookup done``,
``hold_expired`` (the policy's one deadline), every stdout line before
``where``, the end of a connection and the run's conclusion. Like the rest of
the core it reads no clock and runs nothing: the core passes the monotonic
time in and runs the lookups that `Attribution.next_lookup` asks for.

Watched packages are ``where.packages()``; a ``<package>:<process>`` name
watches ``<package>``, because a uid names an app, not one of its processes.
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
  without their package.
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
  reaches the head, with what is known by then (a line whose package is
  still unknown then is held again, as the new head).
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
unknown verdict, and not). Any other line (its package is known, or only
mapped packages are asked again for its uid) is evaluated by a plain
``where(entry)``, or by `evaluate` when several watched packages share its
uid. A combination whose class overrides ``packages()`` is one leaf, as
`Condition.packages` does not look inside it either. And/Or decide like
Kleene logic and stop at the first deciding verdict. The tree is walked
without recursion (F7-4).

Caveats (documented, design 5.8):

- Packages that share a uid (``sharedUserId``) match each other's lines: a
  `Package` (or a `Where` of one) that names any of the watched packages on
  that uid matches them. ``LogEntry.package`` names the first of them in
  alphabetical order, and that is what a condition that reads
  ``entry.package`` itself sees, a `Package` inside a combination whose
  class overrides ``packages()`` included (that combination is called as a
  whole).
- A watched package whose uid is a system uid (below 10000) gets one WARNING:
  every line of that uid, whatever its process, is attributed to it.
- Isolated processes (app ids 90000-99999, A.14) are never attributed, and
  their lines are never held and ask for no lookup; lines of other apps'
  processes are never attributed either: their uid names no watched package.
- An app id in the negative cache asks for no lookup any more (the cache has
  no time-to-live): a watched package that an install during the capture
  gives that very app id is found only when a line from another app uid not
  yet in the cache asks for a lookup, and its lines before that go on
  without their package. A reinstall under a new app id is found by the
  re-lookup its first line asks for, but with every watched package mapped
  nothing is held, so the lines before that lookup answered go on without
  their package.
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
    _combines,
    _Junction,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from logpyt.adb import _AdbResult
    from logpyt.models import LogEntry

__all__ = [
    "LOOKUP_INTERVAL",
    "UID_CACHE",
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
class Verdict:
    """A line that goes on: its entry (attributed), whether ``where`` matched.

    ``unattributed``: it was evaluated with ``package=None`` although its
    verdict depended on a package that stayed unknown (design 4.4).
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
    # for its package (a plain where at its release, `Attribution._plain`).
    calls: dict[int, bool] | None = None
    given_up: bool = False  # a lookup it waited for failed


class Attribution:
    """The attribution state of one run (see the module docs).

    Args:
        where: The stream's condition; its ``packages()`` are watched.
        adb_path: The resolved adb executable.
        serial: The device serial, or None.
        package_hold: The longest a line waits for its package, in seconds;
            also how long intake waits for the lookups at start.

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
        packages = sorted(_base_names(() if where is None else where.packages()))
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

    def lookup_done(self, ident: int, result: _AdbResult | Exception) -> list[str]:
        """Take the result of lookup ``ident``; return the WARNINGs to log.

        A result whose id is not the lookup in flight is stale and changes
        nothing (X-3). A failure releases the held lines that waited for it
        (counted as unattributed by `release`); one WARNING per run says so.
        """
        flight = self._flight
        if flight is None or flight.ident != ident:
            return []
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
                return []
            self._warned_failure = True
            return [
                f"package lookup for {package} failed: {error}; lines whose "
                "where verdict needs it are evaluated without a package and "
                "counted in stats.unattributed"
            ]
        self._answered[package] = flight.ident
        return self._map(package, uid)

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
        if seen != _KNOWN:
            self._want(seen)
            if not self._candidates(seen):
                # Only mapped packages are asked again (a reinstall): such a
                # line is never held (D-8).
                seen = _KNOWN
        if self._queue:
            calls = None if seen == _KNOWN else {}  # no leaf twice (module docs)
            self._push(_Held(entry, now, seen, entry_bytes(entry), calls))
            return None
        if seen == _KNOWN:
            return Verdict(entry, self._plain(entry, None))
        line = _Held(entry, now, seen, entry_bytes(entry))
        verdict = self._judge(line, now, force=force)
        if verdict is None:
            self._push(line)
        return verdict

    def release(self, now: float, *, force: bool = False) -> Verdict | None:
        """The head of the hold if it may go on now, else None (one per call).

        ``force``: it goes on whatever is known (a connection end, a detached
        consumer, the run's conclusion).

        Raises:
            Exception: What ``where`` raised; that line has left the hold.

        """
        if not self._queue:
            return None
        line = self._pop()
        if line.seen != _KNOWN:
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
        """Drop the held lines and the wanted re-lookups; return how many lines."""
        count = len(self._queue)
        self._queue.clear()
        self._held_bytes = 0
        self._wanted.clear()
        self._passed.clear()
        return count

    def close(self) -> None:
        """The run concluded: no lookup runs or is waited for any more (G5)."""
        self._closed = True
        self._flight = None
        self._first.clear()
        self._wanted.clear()
        self._passed.clear()
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
            if len(owners) > 1:
                self._shared[key] = frozenset(owners)
            else:
                self._shared.pop(key, None)
        self._trim()
        return warnings

    def _trim(self) -> None:
        """Keep the map and the negative cache within `UID_CACHE` uids (LRU)."""
        room = max(0, UID_CACHE - len(self._package_of))
        seen = self._seen
        while len(seen) > room:
            seen.popitem(last=False)

    def _sharing(self, entry: LogEntry) -> frozenset[str]:
        """The watched packages that share ``entry``'s app id, if several do."""
        uid = entry.uid
        if uid is None or not self._shared:
            return _NOBODY
        return self._shared.get(uid % _PER_USER, _NOBODY)

    def _plain(self, entry: LogEntry, calls: dict[int, bool] | None) -> bool:
        """The verdict of a line whose package is settled (maybe None).

        A plain ``where(entry)``, unless the line may have waited for its
        package (``calls`` is not None: the verdicts of the leaves it called
        while its package was unknown are reused, and no leaf is called
        twice) or several watched packages share its uid (each `Package` leaf
        matches any of them).
        """
        where = self._where
        assert where is not None  # noqa: S101  (attribution needs a condition)
        owners = self._sharing(entry)
        if calls is None and not owners:
            return bool(where(entry))
        if calls is None:
            calls = {}
        return _decided(where, entry, calls, self._declared, owners)

    def _judge(self, line: _Held, now: float, *, force: bool) -> Verdict | None:
        """The verdict of a line at the head of the hold, or None if it must wait."""
        where = self._where
        assert where is not None  # noqa: S101  (attribution needs a condition)
        entry = line.entry
        calls = line.calls
        if line.seen == _KNOWN:
            return Verdict(entry, self._plain(entry, calls))
        if calls is None:
            calls = line.calls = {}
        verdict = evaluate(
            where,
            entry,
            unknown=self._candidates(line.seen),
            calls=calls,
            declared=self._declared,
        )
        if verdict is not None:
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


def _base_names(names: Iterable[str]) -> frozenset[str]:
    """The packages of ``names``: ``<package>:<process>`` watches ``<package>``."""
    return frozenset(name.partition(":")[0] for name in names)


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
            declared[id(node)] = _base_names(node.packages())
    return declared


def evaluate(
    root: Condition,
    entry: LogEntry,
    *,
    unknown: frozenset[str],
    calls: dict[int, bool],
    declared: Mapping[int, frozenset[str]],
    owners: frozenset[str] = _NOBODY,
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
    context = (entry, unknown, calls, declared, owners)
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
    called = calls.get(key)
    if called is not None:
        return called
    if unknown and _left_unknown(node, unknown, declared, guarded=guarded):
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
    names = declared.get(id(node))
    if names is None:
        names = _base_names(node.packages())
    return not names.isdisjoint(unknown) or (guarded and not _is_pure(node))


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
