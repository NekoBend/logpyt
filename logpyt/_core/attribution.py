"""Package attribution of a live stream (design 5.8): the PR-10 minimal policy.

The stream core calls this interface at the points of the transition table
(design 3.2): the lookup at start, ``lookup done``, ``hold_expired``, and each
line before ``where``. PR-11 replaces the policy behind it with the full one
(uid map with a negative cache, re-lookup of an unknown app uid with request
ids and a rate limit, the ordered hold, tri-state evaluation); the core's side
of the interface stays.

What this minimal policy does:

- No watched package (``where.packages()`` is empty): no lookup, nothing held,
  intake open from the start.
- Watched packages: at start, one ``dumpsys package <pkg>`` per package, one at
  a time (a ``<pkg>:<process>`` name looks up ``<pkg>``). Intake stays closed
  until every lookup returned or ``package_hold`` expired (F5-2's initial
  resolution before reading). A line gets the package its uid maps to, through
  the pipeline's ``uid_packages`` (`logpyt._core.pipeline.Pipeline`): the exact
  uid, then the app id for a secondary user.
- A line is counted as unattributed when its package stays unknown because a
  lookup failed or had not returned: it has an app uid that no lookup mapped
  while some watched package has no answer. A package that the lookup found
  not installed has an answer.
- Nothing is held, so `release` and `discard` have nothing to return.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from logpyt.adb import _package_uid_argv, _parse_package_uid
from logpyt.exceptions import AdbError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from logpyt.adb import _AdbResult
    from logpyt.models import LogEntry

__all__ = ["Attribution"]

# Design 5.8: a uid is user * 100000 + app id; app ids start at 10000.
_PER_USER = 100_000
_FIRST_APP_ID = 10_000


def _is_app_uid(uid: int | None) -> bool:
    """Whether ``uid`` belongs to an app (not a system uid, design 5.8)."""
    return uid is not None and uid % _PER_USER >= _FIRST_APP_ID


class Attribution:
    """The attribution state of one run (see the module docs).

    Args:
        names: The watched names, ``where.packages()``.
        adb_path: The resolved adb executable.
        serial: The device serial, or None.
        package_hold: Seconds intake may stay closed for the initial lookup.

    """

    __slots__ = (
        "_adb_path",
        "_answered",
        "_failed",
        "_hold",
        "_in_flight",
        "_pending",
        "_ready",
        "_serial",
        "_until",
        "_watched",
        "uid_packages",
    )

    def __init__(
        self,
        names: Iterable[str],
        *,
        adb_path: str,
        serial: str | None,
        package_hold: float,
    ) -> None:
        packages = sorted({name.partition(":")[0] for name in names})
        self._adb_path = adb_path
        self._serial = serial
        self._hold = package_hold
        self._pending: list[str] = packages
        self._in_flight: str | None = None
        self._answered: set[str] = set()
        self._failed = False
        self._watched = bool(packages)
        self._ready = not packages
        self._until: float | None = None
        # uid -> package; the pipeline reads it for every line.
        self.uid_packages: dict[int, str] = {}

    @property
    def watching(self) -> bool:
        """Whether any package is watched."""
        return self._watched

    @property
    def ready(self) -> bool:
        """Whether intake may open: the initial lookups returned or the hold expired."""
        return self._ready

    @property
    def held(self) -> int:
        """Lines waiting for attribution (none in this policy)."""
        return 0

    def begin(self, now: float) -> None:
        """The run starts: the hold runs from ``now``."""
        if not self._ready:
            self._until = now + self._hold

    def deadline(self) -> float | None:
        """When ``hold_expired`` fires; None when nothing waits."""
        return None if self._ready else self._until

    def next_lookup(self) -> tuple[str, ...] | None:
        """The argv of the next lookup to run, or None (one runs at a time)."""
        if self._in_flight is not None or not self._pending:
            return None
        package = self._pending.pop(0)
        self._in_flight = package
        return _package_uid_argv(self._adb_path, self._serial, package)

    def lookup_done(self, result: _AdbResult | Exception) -> str | None:
        """Take a lookup result; return a warning for the first failure of the run.

        A result while no lookup runs (stale) changes nothing.
        """
        package, self._in_flight = self._in_flight, None
        if package is None:
            return None
        error: Exception | None = None
        uid: int | None = None
        if isinstance(result, Exception):
            error = result
        else:
            try:
                uid = _parse_package_uid(result, package)
            except AdbError as exc:
                error = exc
        warning: str | None = None
        if error is None:
            self._answered.add(package)
            if uid is not None:
                self.uid_packages[uid] = package
        else:
            if not self._failed:
                warning = (
                    f"package lookup for {package} failed, its lines are counted "
                    f"in stats.unattributed: {error}"
                )
            self._failed = True
        if not self._pending and self._in_flight is None:
            self._ready = True
        return warning

    def expire(self) -> None:
        """``package_hold`` expired: intake opens with the lookups unfinished."""
        self._ready = True

    def unattributed(self, entry: LogEntry) -> bool:
        """Whether ``entry`` was evaluated without its package (see the module docs)."""
        if entry.package is not None or not _is_app_uid(entry.uid):
            return False
        return self._failed or self._in_flight is not None or bool(self._pending)

    def admit(self, entry: LogEntry) -> list[LogEntry]:  # noqa: PLR6301  (PR-11 interface)
        """Take a line before ``where``; return the lines that may go on, in order.

        The full policy holds a line whose verdict depends on an unknown
        package, and the lines after it; this one holds nothing.
        """
        return [entry]

    def release(self) -> list[LogEntry]:  # noqa: PLR6301  (PR-11 interface)
        """Held lines that may go on, in order (none in this policy)."""
        return []

    def discard(self) -> int:  # noqa: PLR6301  (PR-11 interface)
        """Drop the held lines; return how many (none in this policy)."""
        return 0
