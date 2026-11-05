"""Tests for package attribution in the stream core (design 5.8, D-8, 5.10; PR-11).

A fake clock drives every timer: the harness's ``now`` (`tests.core.harness`)
for the core, plain floats for `Attribution` on its own. The lookup is
``adb shell dumpsys package <pkg>`` (A.8); `tests.core.harness.lookup` scripts
its answers. The F5-2 behavior tests of `tests/behavior` flip in PR-12/PR-13,
when the drivers run this core.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest

from logpyt import (
    AnrCondition,
    Condition,
    CrashCondition,
    Filter,
    GroupBy,
    MinLevel,
    Package,
    Tag,
    Where,
)
from logpyt._core import attribution as attribution_module
from logpyt._core.attribution import (
    LOOKUP_INTERVAL,
    UID_CACHE,
    Attribution,
    declared_packages,
    evaluate,
)
from logpyt._core.machine import COMMAND_TIMEOUT, Mode
from logpyt._core.protocol import (
    CallbackFailed,
    CommandDone,
    Deliver,
    KillRequest,
    RunCommand,
    StopRequest,
    Tick,
)
from logpyt.config import Reconnect
from logpyt.exceptions import CallbackError
from logpyt.filters import And
from logpyt.models import LogEntry, LogGroup
from logpyt.parsers import ThreadTimeLogParser
from logpyt.streams._events import StateChange, StreamState
from tests.core.harness import (
    ADB,
    APP,
    APP_UID,
    LOOKUP_ABSENT,
    LOOKUP_FAILED,
    LOOKUP_OK,
    FixedContext,
    Harness,
    line,
    lines,
    lookup,
)

if TYPE_CHECKING:
    from logpyt._core.protocol import Record
    from logpyt.adb import _AdbResult

S = StreamState
OTHER = "com.example.other"
OTHER_UID = 10070


def _running(
    where: Condition,
    *,
    first: _AdbResult = LOOKUP_ABSENT,
    **fields: Any,  # noqa: ANN401
) -> Harness:
    """RUNNING at t=100 after the first lookup answered ``first`` (default: APP
    not installed), with one system-uid line read.
    """
    harness = Harness.make(where=where, **fields)
    harness.connecting_spawned()
    harness.answer("lookup", first)
    harness.stdout(line("first", uid=1000, tag="Sys"))
    assert harness.core.state is S.RUNNING
    return harness


def _lookups(harness: Harness) -> list[RunCommand]:
    return [
        command
        for command in harness.commands
        if isinstance(command, RunCommand) and command.kind == "lookup"
    ]


def _entries(harness: Harness) -> list[LogEntry]:
    """The delivered items, all entries (no group_by)."""
    items = harness.delivered()
    entries = [item for item in items if isinstance(item, LogEntry)]
    assert len(entries) == len(items)
    return entries


def _records(harness: Harness) -> list[Record]:
    return [
        record
        for command in harness.commands
        if isinstance(command, Deliver)
        for record in command.records
    ]


def _entry(message: str = "m", **fields: Any) -> LogEntry:  # noqa: ANN401
    text = line(message, **fields).decode()
    entry = ThreadTimeLogParser().parse(text, FixedContext())
    assert entry is not None
    return entry


def _attribution(where: Condition, *, hold: float = 2.0) -> Attribution:
    attribution = Attribution(where, adb_path=ADB, serial=None, package_hold=hold)
    attribution.begin(0.0)
    return attribution


# -- the lookup and the uid map --------------------------------------------------------


def test_the_lookup_is_dumpsys_package_per_watched_package() -> None:
    """A.8: ``shell dumpsys package <pkg>``, one at a time; a process name
    watches its package (a uid names an app, not a process)."""
    harness = Harness.make(
        where=Package(f"{APP}:remote") | Package(OTHER), serial="emulator-5554"
    )
    harness.start()

    (first,) = _lookups(harness)
    harness.answer("lookup", LOOKUP_OK)
    second = _lookups(harness)[1]

    serial = ("-s", "emulator-5554")
    assert first.argv == (ADB, *serial, "shell", "dumpsys", "package", APP)
    assert second.argv[-1] == OTHER
    assert first.timeout == pytest.approx(COMMAND_TIMEOUT)
    assert not harness.core.intake_open  # it waits for every first lookup


def test_first_lookup_before_reading_attributes_the_backlog() -> None:
    """F5-2 (core): intake opens once the first lookup returned; with every
    watched package mapped no line is ever held (D-8), and another app's uid
    asks for one more lookup of each package (it may be a reinstall, 5.8)."""
    harness = Harness.make(where=Package(APP) | Package(OTHER))
    harness.connecting_spawned()
    assert not harness.core.intake_open
    harness.answer("lookup", LOOKUP_OK)
    harness.answer("lookup", lookup(OTHER_UID, OTHER, key="appId"))  # API 36 form
    assert harness.core.intake_open

    harness.stdout(
        line("app")
        + line("other", uid=OTHER_UID)
        + line("third party", uid=10099)
        + line("system", uid=1000)
    )

    assert [(item.message, item.package) for item in _entries(harness)] == [
        ("app", APP),
        ("other", OTHER),
    ]
    attribution = harness.core._attribution
    assert attribution.held == 0
    assert len(_lookups(harness)) == 2
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)  # APP's second lookup, then OTHER's
    harness.answer("lookup", lookup(OTHER_UID, OTHER, key="appId"))
    assert attribution.negative(10099)
    harness.stdout(line("third party again", uid=10099))
    assert len(_lookups(harness)) == 4
    assert harness.core.next_deadline() is None
    assert harness.core.stats().unattributed == 0


def test_secondary_user_matches_by_app_id() -> None:
    """5.8: a uid of user 10 matches the app id the lookup returned."""
    harness = _running(Package(APP), first=lookup(APP_UID, key="appId"))

    harness.stdout(line("work profile", uid=10 * 100_000 + APP_UID))

    (entry,) = harness.delivered()
    assert isinstance(entry, LogEntry)
    assert (entry.uid, entry.package) == (1_010_065, APP)


@pytest.mark.parametrize("uid", [1000, 0, "root", "zzzzz"], ids=str)
def test_system_uids_are_known_and_never_held(uid: int | str) -> None:
    """5.8: a system uid (< 10000) or a name that is no uid has no package."""
    harness = _running(Package(APP) | Tag("Sys"))

    harness.stdout(line("system", uid=uid, tag="Sys"))

    assert harness.messages() == ["first", "system"]
    assert harness.core._attribution.held == 0
    assert len(_lookups(harness)) == 1  # nothing asked for a re-lookup


def test_a_system_uid_package_warns_once_and_over_matches() -> None:
    """5.8 caveat: a watched package with a system uid gets one WARNING, and
    every line of that uid is attributed to it."""
    harness = Harness.make(where=Package(APP))
    harness.connecting_spawned()

    harness.answer("lookup", lookup(1000))
    harness.stdout(line("system_server", uid=1000, pid=555))

    warnings = [d.message for d in harness.diagnostics if "system uid" in d.message]
    assert warnings == [
        f"package {APP} has the system uid 1000: every line of uid 1000, "
        f"whatever its process, is attributed to {APP}"
    ]
    (entry,) = harness.delivered()
    assert isinstance(entry, LogEntry)
    assert entry.package == APP


def test_a_shared_uid_goes_to_the_first_watched_package() -> None:
    """5.8 caveat: packages that share a uid match each other's lines; the
    lines are attributed to the first of them in alphabetical order."""
    harness = Harness.make(where=Package(OTHER) | Package(APP))
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_OK)  # com.example.app first (sorted)
    harness.answer("lookup", lookup(APP_UID, OTHER))

    harness.stdout(line("shared"))

    assert harness.core._attribution.uid_packages == {APP_UID: APP}
    (entry,) = harness.delivered()
    assert isinstance(entry, LogEntry)
    assert entry.package == APP


_SHARED = [
    ("later-and", (Package(OTHER) & Tag("x")) | (Package(APP) & Tag("y")), True),
    ("later-alone", Package(OTHER) | (Package(APP) & Tag("never")), True),
    ("not-later", ~Package(OTHER) | (Package(APP) & Tag("never")), False),
]


@pytest.mark.parametrize(
    ("where", "matched"),
    [case[1:] for case in _SHARED],
    ids=[case[0] for case in _SHARED],
)
def test_a_shared_uid_matches_every_watched_owner(
    where: Condition,
    matched: bool,  # noqa: FBT001  (a parameter)
) -> None:
    """5.8 caveat: a Package of the alphabetically later package matches the
    shared uid's lines too, although LogEntry.package names the first."""
    harness = Harness.make(where=where)
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_OK)  # com.example.app
    harness.answer("lookup", lookup(APP_UID, OTHER))

    harness.stdout(line("shared", tag="x"))

    entries = _entries(harness)
    assert [(item.message, item.package) for item in entries] == (
        [("shared", APP)] if matched else []
    )
    assert harness.core.stats().unattributed == 0


# -- re-lookups ------------------------------------------------------------------------


def test_a_reinstall_under_a_new_uid_is_looked_up_again() -> None:
    """5.8, 9.4: a mapped package reinstalled under another app id. A line from
    the new uid asks for one more lookup and is not held (D-8: every watched
    package is mapped); the answer moves the package, so later lines of the
    new uid are attributed and the old uid names nothing."""
    new_uid = 10_120
    harness = Harness.make(where=Package(APP) & MinLevel("E"))
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_OK)
    harness.stdout(line("before", level="E"))
    harness.stdout(line("crash 1", uid=new_uid, level="E"))  # before the answer

    attribution = harness.core._attribution
    assert attribution.held == 0
    harness.until_deadline()
    assert harness.request("lookup").argv[-1] == APP
    harness.answer("lookup", lookup(new_uid))
    harness.stdout(line("crash 2", uid=new_uid, level="E") + line("old", level="E"))

    assert attribution.uid_packages == {new_uid: APP}
    assert [(item.message, item.package) for item in _entries(harness)] == [
        ("before", APP),
        ("crash 2", APP),
    ]
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (2, 0)  # crash 1 and old


def test_a_mapped_package_found_uninstalled_has_no_uid_again() -> None:
    """5.8: a second lookup that finds a mapped package not installed takes its
    uid away, so a line from an unknown app uid waits for it again."""
    harness = _running(Package(APP), first=LOOKUP_OK)
    harness.stdout(line("third party", uid=10_099))  # asks for APP once more
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_ABSENT)  # uninstalled meanwhile

    attribution = harness.core._attribution
    assert attribution.uid_packages == {}
    assert attribution.negative(10_099)
    harness.stdout(line("relaunch", uid=10_120))
    assert attribution.held == 1
    harness.until_deadline()
    harness.answer("lookup", lookup(10_120))  # installed again

    assert [(item.message, item.package) for item in _entries(harness)] == [
        ("relaunch", APP)
    ]
    assert harness.core.stats().unattributed == 0


def test_an_unknown_app_uid_is_held_until_a_relookup_names_it() -> None:
    """5.8: APP was not installed at start; its first line triggers a re-lookup
    LOOKUP_INTERVAL after the first lookup, and waits for it."""
    harness = _running(Package(APP))

    harness.stdout(line("launch"))

    assert harness.core._attribution.held == 1
    assert harness.messages() == []
    assert len(_lookups(harness)) == 1
    assert harness.core.next_deadline() == pytest.approx(100.0 + LOOKUP_INTERVAL)
    harness.until_deadline()
    first, retry = _lookups(harness)
    assert retry.argv == first.argv
    harness.answer("lookup", LOOKUP_OK)
    (entry,) = harness.delivered()
    assert isinstance(entry, LogEntry)
    assert (entry.message, entry.package) == ("launch", APP)
    assert harness.core._attribution.held == 0
    assert harness.core.stats().unattributed == 0


def test_relookups_are_one_at_a_time_and_rate_limited() -> None:
    """X-3: one lookup in flight, a package's lookups LOOKUP_INTERVAL apart; a
    result settles only the uids seen before its request was issued."""
    harness = _running(Package(APP))
    harness.stdout(line("a", uid=10071))
    harness.send(Tick(), at=100.2)
    harness.stdout(line("b", uid=10072))
    assert len(_lookups(harness)) == 1
    harness.send(Tick(), at=100.5)
    assert len(_lookups(harness)) == 2  # one retry for both uids
    harness.stdout(line("c"))  # 10065, seen after the retry was issued
    assert len(_lookups(harness)) == 2  # one in flight

    harness.send(Tick(), at=100.7)
    harness.answer("lookup", LOOKUP_ABSENT)

    attribution = harness.core._attribution
    assert attribution.negative(10071)
    assert attribution.negative(10072)
    assert not attribution.negative(APP_UID)
    assert attribution.held == 1  # c still waits; a and b were filtered
    assert harness.core.stats().filtered == 3
    assert harness.core.next_deadline() == pytest.approx(100.5 + LOOKUP_INTERVAL)
    harness.until_deadline()
    assert len(_lookups(harness)) == 3
    harness.answer("lookup", LOOKUP_OK)
    assert [(item.message, item.package) for item in _entries(harness)] == [("c", APP)]


def test_each_package_waits_its_own_interval() -> None:
    """5.8: re-lookups run one at a time, each package LOOKUP_INTERVAL after its
    own last lookup, so a second package does not wait for the first's."""
    harness = Harness.make(where=Package(APP) | Package(OTHER))
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_ABSENT)  # APP at t=100
    harness.send(Tick(), at=100.3)
    harness.answer("lookup", LOOKUP_ABSENT)  # OTHER at t=100 (issued then)
    harness.stdout(line("held"))
    harness.until_deadline()
    assert harness.now == pytest.approx(100.5)
    retry = harness.request("lookup")
    assert retry.argv[-1] == APP
    harness.send(Tick(), at=100.6)
    harness.answer("lookup", LOOKUP_ABSENT)

    # OTHER's interval passed already: its re-lookup starts at once.
    assert harness.request("lookup").argv[-1] == OTHER
    harness.answer("lookup", lookup(APP_UID, OTHER))
    (entry,) = _entries(harness)
    assert entry.package == OTHER


def test_a_held_line_goes_on_once_its_verdict_no_longer_needs_the_package() -> None:
    """Tri-state after a lookup: APP got another uid, so the line is not APP's;
    its verdict then no longer depends on OTHER, whose lookup has not run."""
    harness = Harness.make(where=(Package(OTHER) & Tag("x")) | Package(APP))
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_ABSENT)  # APP
    harness.answer("lookup", LOOKUP_ABSENT)  # OTHER
    harness.stdout(line("held", tag="y"))
    assert harness.core._attribution.held == 1
    harness.until_deadline()  # APP's re-lookup, first in alphabetical order
    assert harness.request("lookup").argv[-1] == APP

    harness.answer("lookup", lookup(10_099))  # APP installed, with another uid

    attribution = harness.core._attribution
    assert attribution.held == 0
    assert not attribution.negative(APP_UID)  # it may still be OTHER's
    assert harness.request("lookup").argv[-1] == OTHER
    stats = harness.core.stats()
    assert (stats.unattributed, stats.filtered) == (0, 1)


def test_stale_lookup_result_ignored() -> None:
    """X-3, F2-11: a lookup result counts only while its request is in flight.

    A late copy of an answered lookup changes nothing in the core (G5), and the
    attribution takes a result only for the request id in flight.
    """
    harness = _running(Package(APP))
    first = _lookups(harness)[0]
    harness.stdout(line("a"))
    harness.until_deadline()  # the re-lookup starts
    retry = harness.request("lookup")

    assert harness.send(CommandDone(first.request, lookup(OTHER_UID))) == []
    harness.answer("lookup", LOOKUP_OK)
    assert harness.send(CommandDone(retry.request, lookup(OTHER_UID))) == []
    assert harness.send(CommandDone(retry.request, LOOKUP_FAILED)) == []
    harness.stdout(line("b"))

    attribution = harness.core._attribution
    assert attribution.uid_packages == {APP_UID: APP}
    assert [(item.message, item.package) for item in _entries(harness)] == [
        ("a", APP),
        ("b", APP),
    ]
    assert harness.core.stats().unattributed == 0
    assert not any("failed" in d.message for d in harness.diagnostics)

    # The attribution itself: only the id in flight is taken.
    alone = _attribution(Package(APP))
    started = alone.next_lookup(0.0)
    assert started is not None
    assert alone.lookup_done(started.ident + 1, LOOKUP_OK) == []
    assert alone.next_lookup(0.0) is None  # still in flight
    assert alone.uid_packages == {}
    assert alone.lookup_done(started.ident, LOOKUP_ABSENT) == []
    assert alone.lookup_done(started.ident, LOOKUP_OK) == []  # answered already
    assert alone.uid_packages == {}
    assert alone.ready


def test_a_negative_uid_is_not_held_again() -> None:
    """5.8: once every package without a uid answered after a uid was seen, its
    lines are known to have no package: they go on at once."""
    harness = _running(Package(APP) | Tag("x"))
    harness.stdout(line("a", uid=OTHER_UID))
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_ABSENT)
    assert harness.core._attribution.negative(OTHER_UID)

    harness.stdout(line("b", uid=OTHER_UID, tag="x") + line("c", uid=OTHER_UID))

    assert harness.messages() == ["b"]
    assert harness.core._attribution.held == 0
    assert len(_lookups(harness)) == 2
    assert harness.core.next_deadline() is None


def test_uid_map_and_negative_cache_share_4096_uids_lru() -> None:
    """5.10: map and negative cache hold UID_CACHE uids together, least
    recently seen evicted first; the map's entries are never evicted."""
    attribution = _attribution(Package(APP) | Package(OTHER) | Tag("x"))
    lookup_a = attribution.next_lookup(0.0)
    assert lookup_a is not None
    attribution.lookup_done(lookup_a.ident, lookup(10001))
    lookup_b = attribution.next_lookup(0.0)
    assert lookup_b is not None
    attribution.lookup_done(lookup_b.ident, LOOKUP_ABSENT)

    for uid in range(20_000, 20_000 + UID_CACHE - 1):
        assert attribution.admit(_entry(uid=uid, tag="x"), 1.0) is not None
    assert attribution.remembered == UID_CACHE
    attribution.admit(_entry(uid=20_000, tag="x"), 1.0)  # seen again: recent
    for uid in (30_000, 30_001):
        attribution.admit(_entry(uid=uid, tag="x"), 1.0)

    seen = attribution._seen
    assert attribution.remembered == UID_CACHE
    assert attribution.uid_packages == {10001: APP}
    assert 20_000 in seen
    assert 20_001 not in seen
    assert 20_002 not in seen
    assert 30_001 in seen
    # The re-lookup of OTHER, then APP's second lookup (a reinstall may have
    # moved it), settle every remembered uid; a package without a uid first.
    retry = attribution.next_lookup(1.0)
    assert retry is not None
    assert retry.package == OTHER
    attribution.lookup_done(retry.ident, LOOKUP_ABSENT)
    assert not attribution.negative(20_000)  # APP's answer came before it
    again = attribution.next_lookup(1.0)
    assert again is not None
    assert again.package == APP
    attribution.lookup_done(again.ident, lookup(10001))
    assert attribution.negative(20_000)
    assert attribution.negative(30_001)


def test_a_negative_cache_hit_counts_as_recent() -> None:
    """5.10: the LRU orders the negative cache by the last line seen too, so a
    busy app outside the watched packages is not evicted first."""
    attribution = _attribution(Package(APP) | Tag("x"))
    first = attribution.next_lookup(0.0)
    assert first is not None
    attribution.lookup_done(first.ident, LOOKUP_ABSENT)
    attribution.admit(_entry(uid=20_000, tag="x"), 1.0)
    retry = attribution.next_lookup(1.0)
    assert retry is not None
    attribution.lookup_done(retry.ident, LOOKUP_ABSENT)
    attribution.admit(_entry(uid=20_000, tag="x"), 1.0)
    seen = attribution._seen
    assert seen[20_000] is None  # in the negative cache

    for uid in range(20_001, 20_000 + UID_CACHE):
        attribution.admit(_entry(uid=uid, tag="x"), 1.0)
    assert attribution.remembered == UID_CACHE
    attribution.admit(_entry(uid=20_000, tag="x"), 1.0)  # a hit: recent again
    attribution.admit(_entry(uid=30_000, tag="x"), 1.0)

    assert seen.get(20_000, 0) is None
    assert 20_001 not in seen
    assert 30_000 in seen


# -- the ordered hold and tri-state evaluation -----------------------------------------

_TRI_STATE = [
    ("or-decided", Package(APP) | Tag("x"), "x", "delivered"),
    ("or-waits", Package(APP) | Tag("x"), "y", "held"),
    ("and-decided", Package(APP) & Tag("x"), "y", "filtered"),
    ("and-waits", Package(APP) & Tag("x"), "x", "held"),
    ("not-waits", ~Package(APP), "x", "held"),
    ("filter-decided", Filter(package=APP, tag="x"), "y", "filtered"),
    (
        "crash-text",
        Package(APP) | CrashCondition(package=APP) | Tag("x"),
        "x",
        "delivered",
    ),
]


@pytest.mark.parametrize(
    ("where", "tag", "fate"),
    [case[1:] for case in _TRI_STATE],
    ids=[case[0] for case in _TRI_STATE],
)
def test_only_a_verdict_that_needs_the_package_waits(
    where: Condition, tag: str, fate: str
) -> None:
    """D-8, tri-state: a line is held only if its where verdict depends on
    the unknown package; a decided one goes on at once, unattributed or not."""
    harness = _running(where | Tag("Sys"))

    harness.stdout(line("probe", tag=tag))

    held = harness.core._attribution.held
    delivered = [item.message for item in _entries(harness)]
    actual = "held" if held else ("delivered" if "probe" in delivered else "filtered")
    assert actual == fate
    assert harness.core.stats().unattributed == 0
    if fate == "delivered":
        entry = harness.delivered()[-1]
        assert isinstance(entry, LogEntry)
        assert entry.package is None  # its package was not known then
    # Every line from an unknown app uid asks for the re-lookup.
    assert harness.core.next_deadline() == pytest.approx(100.0 + LOOKUP_INTERVAL)


def test_later_lines_queue_behind_a_held_line_in_order() -> None:
    """5.8: once a line is held, later lines wait behind it, known or not."""
    harness = _running(Package(APP) | Tag("Sys"))

    harness.stdout(
        line("held")
        + line("system 1", uid=1000, tag="Sys")
        + line("app")
        + line("system 2", uid=1000, tag="Sys")
    )
    assert harness.core._attribution.held == 4
    assert harness.messages() == ["first"]
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)

    assert harness.messages() == ["first", "held", "system 1", "app", "system 2"]
    packages = [item.package for item in _entries(harness)]
    assert packages == [None, APP, None, APP, None]


def test_hold_expiry_evaluates_each_line_without_its_package() -> None:
    """4.4: a line waits at most package_hold from its arrival; then it is
    evaluated with package=None and counted in unattributed."""
    harness = _running(~Package(APP), package_hold=1.0)
    harness.stdout(line("a"))  # t=100.0, expires at 101.0
    harness.until_deadline()  # t=100.5: the re-lookup starts; never answered
    harness.stdout(line("b"))  # t=100.5, expires at 101.5

    assert harness.core.next_deadline() == pytest.approx(101.0)
    harness.until_deadline()
    assert harness.messages() == ["first", "a"]
    assert harness.core.next_deadline() == pytest.approx(101.5)
    harness.until_deadline()

    assert harness.messages() == ["first", "a", "b"]
    assert [item.package for item in _entries(harness)] == [None, None, None]
    stats = harness.core.stats()
    assert stats.unattributed == 2
    assert "unattributed" in stats.incomplete


def test_lines_after_a_slow_first_lookup_wait_their_own_hold() -> None:
    """F5-2: when the first lookup outlasts package_hold, intake opens and the
    backlog still gets its package if the lookup answers within a line's hold."""
    harness = Harness.make(where=Package(APP), package_hold=1.0)
    harness.connecting_spawned()
    harness.advance(1.0)  # the first lookup's wait expired: intake opens
    assert harness.core.intake_open

    harness.stdout(line("backlog"))
    harness.send(Tick(), at=101.5)
    harness.answer("lookup", LOOKUP_OK)  # the first lookup, late

    (entry,) = harness.delivered()
    assert isinstance(entry, LogEntry)
    assert entry.package == APP
    assert harness.core.stats().unattributed == 0


def test_package_hold_zero_never_holds() -> None:
    """package_hold=0: no wait at start, and an undecided line is evaluated at
    once without its package."""
    harness = Harness.make(where=Package(APP), package_hold=0.0)
    harness.connecting_spawned()
    assert harness.core.intake_open

    harness.stdout(line("x"))

    assert harness.core._attribution.held == 0
    assert (harness.core.stats().unattributed, harness.core.stats().filtered) == (1, 1)
    harness.answer("lookup", LOOKUP_OK)  # the first lookup still maps APP
    harness.stdout(line("y"))
    assert [item.package for item in _entries(harness)] == [APP]


def test_a_failed_lookup_releases_the_lines_that_waited_for_it() -> None:
    """5.8: the lines held for a failed lookup go on unattributed; lines that
    come later wait for the next one; one WARNING per run."""
    harness = _running(~Package(APP))
    harness.stdout(line("a"))
    harness.until_deadline()  # t=100.5: the re-lookup
    harness.answer("lookup", LOOKUP_FAILED)
    assert harness.messages() == ["first", "a"]
    assert harness.core.stats().unattributed == 1

    harness.stdout(line("b"))
    assert harness.core._attribution.held == 1
    harness.until_deadline()  # t=101.0: the next one
    harness.answer("lookup", LOOKUP_FAILED)
    harness.stdout(line("c"))
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)

    assert harness.messages() == ["first", "a", "b"]  # c is APP's: filtered
    assert harness.core.stats().unattributed == 2
    warnings = [d for d in harness.diagnostics if "lookup for" in d.message]
    assert len(warnings) == 1
    assert "stats.unattributed" in warnings[0].message


def test_a_failed_lookup_keeps_a_line_first_seen_after_it_started() -> None:
    """X-3: a lookup says nothing about a uid first seen after its request, so
    its failure releases the lines seen before only."""
    harness = _running(~Package(APP))
    harness.stdout(line("a"))
    harness.until_deadline()  # t=100.5: the re-lookup starts
    harness.stdout(line("b", uid=10_072))  # a new uid, seen while it runs

    harness.answer("lookup", LOOKUP_FAILED)

    assert harness.messages() == ["first", "a"]
    assert harness.core._attribution.held == 1
    assert harness.core.stats().unattributed == 1


def test_a_failed_lookup_keeps_a_line_that_waits_for_another_package() -> None:
    """A failure gives up only the lines that still wait for that package: l1
    waits for ZZZ alone once APP answered after it, so it stays (and l2 behind
    it), although APP's next lookup fails."""
    zzz = "com.example.zzz"
    harness = Harness.make(where=Package(APP) | Package(zzz))
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_ABSENT)  # APP, id 1, t=100
    harness.answer("lookup", LOOKUP_ABSENT)  # ZZZ, id 2, t=100
    harness.stdout(line("l1", uid=10_071))
    harness.until_deadline()  # t=100.5: APP's re-lookup, id 3
    assert harness.request("lookup").argv[-1] == APP
    harness.stdout(line("l2", uid=10_072))
    harness.send(Tick(), at=101.0)
    harness.answer("lookup", LOOKUP_ABSENT)  # APP absent: l1 waits for ZZZ only
    assert harness.request("lookup").argv[-1] == APP  # due first, alphabetically

    harness.answer("lookup", LOOKUP_FAILED)

    assert harness.core._attribution.held == 2
    assert harness.core.stats().unattributed == 0


def test_held_lines_count_against_the_outbox() -> None:
    """5.10: held lines close intake like queued items, and reopen at half."""
    harness = _running(Package(APP), buffer_entries=4, stall_timeout=None)

    harness.stdout(lines(4))

    assert harness.core._attribution.held == 4
    assert harness.core._admission.queued_entries == 0
    assert not harness.core.intake_open
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)
    assert harness.core._attribution.held == 0
    assert harness.core._admission.queued_entries == 4
    assert not harness.core.intake_open
    harness.consume(2)
    assert harness.core.intake_open


def test_a_released_line_never_counts_twice() -> None:
    """5.10: a line leaves the hold before it reaches the outbox, so 3 held
    lines released into an empty outbox of 4 keep intake open."""
    harness = _running(~Package(APP), buffer_entries=4, stall_timeout=None)
    harness.consume()  # "first" leaves the outbox

    harness.stdout(lines(3))
    assert harness.core._attribution.held == 3
    assert harness.core.intake_open
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_ABSENT)

    assert harness.core._attribution.held == 0
    assert harness.core._admission.queued_entries == 3
    assert harness.core.intake_open


@pytest.mark.parametrize("cause", ["attribution", "consumer"])
def test_a_stall_names_what_filled_the_outbox(cause: str) -> None:
    """5.10, 5.6: held lines count against the outbox, so they alone can trip
    the stall resume (stall_timeout 1 s, package_hold 2 s); the INFO says
    whether they or the consumer filled it."""
    harness = _running(Package(APP) | Tag("Sys"), buffer_entries=4)
    harness.consume()  # "first" leaves the outbox
    if cause == "attribution":
        harness.stdout(lines(4))  # APP's uid, APP unknown: held
        assert harness.core._attribution.held == 4
    else:
        harness.stdout(b"".join(line(f"s{n}", uid=1000, tag="Sys") for n in range(4)))
        assert harness.core._admission.queued_entries == 4
    assert not harness.core.intake_open
    while not harness.signals:
        harness.until_deadline()
    assert harness.now == pytest.approx(101.0)

    harness.end_connection(-9)

    assert (S.RUNNING, S.RECONNECTING) in harness.states()
    (info,) = [d.message for d in harness.diagnostics if "disconnected" in d.message]
    expected = {
        "attribution": "because lines waited for package attribution",
        "consumer": "because the consumer fell behind",
    }
    assert expected[cause] in info


# -- user conditions -------------------------------------------------------------------


def test_a_user_leaf_is_called_at_most_once_per_line() -> None:
    """A Where(fn) that plain evaluation reaches first is called once while the
    package is unknown, and its verdict is reused when the line is released."""
    seen: list[str] = []

    def tag_x(entry: LogEntry) -> bool:
        seen.append(entry.message)
        return entry.tag == "x"

    harness = _running(Where(tag_x) | Package(APP))
    seen.clear()

    harness.stdout(line("held"))
    assert seen == ["held"]
    assert harness.core._attribution.held == 1
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_ABSENT)  # not APP: Where decides, from memory

    assert seen == ["held"]
    assert harness.core.stats().filtered == 2
    assert harness.core._attribution.held == 0


def test_a_user_leaf_after_an_unknown_package_waits_for_it() -> None:
    """filters: And short-circuits from left to right, like all(). With APP
    unknown, Package(APP) & Where(fn) holds a line without calling fn, and fn
    then runs for APP's lines only: a call on another app's line would raise
    here and fail the run."""
    calls: list[tuple[str, str | None]] = []

    def app_only(entry: LogEntry) -> bool:
        calls.append((entry.message, entry.package))
        key, _, value = entry.message.partition("=")
        if not value:
            msg = f"not an APP line: {entry.message!r}"
            raise ValueError(msg)
        return key == "state"

    harness = _running(Package(APP) & Where(app_only))
    harness.stdout(line("hello", uid=10_099, tag="Other") + line("state=on"))
    assert calls == []
    assert harness.core._attribution.held == 2
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)  # APP installed during the capture

    assert calls == [("state=on", APP)]
    assert harness.messages() == ["state=on"]
    assert harness.core.state is S.RUNNING
    assert harness.core.outcome is None
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (2, 0)


def test_a_user_leaf_after_an_unknown_package_runs_where_plain_would() -> None:
    """Package(APP) | Where(fn): fn is not called while APP is unknown; at the
    release it runs for another app's line, never for APP's (the Package
    decides the Or first)."""
    seen: list[str] = []

    def tag_x(entry: LogEntry) -> bool:
        seen.append(entry.message)
        return entry.tag == "x"

    harness = _running(Package(APP) | Where(tag_x))
    seen.clear()

    harness.stdout(line("app") + line("other", uid=10_099, tag="x"))
    assert seen == []
    assert harness.core._attribution.held == 2
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)

    assert seen == ["other"]
    assert harness.messages() == ["app", "other"]
    assert harness.core.stats().unattributed == 0


class _Recorder(Condition):
    """A user condition that records its calls and matches every entry."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, entry: LogEntry, /) -> bool:
        del entry
        self.calls += 1
        return True


_GUARDED: list[tuple[str, Any, bool | None, int]] = [
    ("and-after", lambda r: Package(APP) & r, None, 0),
    ("or-after", lambda r: Package(APP) | r, None, 0),
    ("before", lambda r: r & Package(APP), None, 1),
    ("under-not", lambda r: ~(Package(APP) | (Tag("x") & r)), None, 0),
    ("not-after", lambda r: Package(APP) & ~r, None, 0),
    ("flattened", lambda r: Package(APP) | (Tag("y") | r), None, 0),
    ("decided", lambda r: (Package(APP) & Tag("y")) | r, True, 1),
    ("reached-again", lambda r: (Package(APP) & r & Tag("y")) | r, True, 1),
]


@pytest.mark.parametrize(
    ("build", "expected", "count"),
    [case[1:] for case in _GUARDED],
    ids=[case[0] for case in _GUARDED],
)
def test_evaluate_calls_a_user_leaf_only_where_plain_evaluation_would(
    build: Any,  # noqa: ANN401  (a builder of the condition)
    expected: bool | None,  # noqa: FBT001  (a parameter)
    count: int,
) -> None:
    """filters: a user leaf after an unknown verdict in an And or Or is left
    unknown uncalled, through Not too; built-in leaves are still called, so a
    junction they decide lets the walk go on to the leaf."""
    recorder = _Recorder()
    condition = build(recorder)
    entry = _entry(tag="x")

    verdict = evaluate(
        condition,
        entry,
        unknown=frozenset({APP}),
        calls={},
        declared=declared_packages(condition),
    )

    assert verdict is expected
    assert recorder.calls == count


class _AskedOnce(Condition):
    """A leaf that names APP; its packages() fails the test if asked twice."""

    def __init__(self) -> None:
        self.asked = 0

    def __call__(self, entry: LogEntry, /) -> bool:
        del entry
        return True

    def packages(self) -> frozenset[str]:
        self.asked += 1
        if self.asked > 1:
            pytest.fail("packages() asked twice")
        return frozenset({APP})


def test_a_shared_subtree_is_walked_and_evaluated_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c & x) | (c & y) nested 40 times has 2**40 paths: declared_packages
    walks each condition once, and evaluate evaluates each at most twice."""
    depth = 40
    root: Condition = _AskedOnce()
    for _ in range(depth):
        root = (root & Tag("x")) | (root & Tag("y"))
    declared = declared_packages(root)
    real = attribution_module._leaf
    count = 0

    def counted(*args, **kwargs):
        nonlocal count
        count += 1
        if count > 2 * depth + 1:
            pytest.fail("a shared subtree was evaluated again")
        return real(*args, **kwargs)

    monkeypatch.setattr(attribution_module, "_leaf", counted)
    verdict = evaluate(
        root, _entry(tag="z"), unknown=frozenset({APP}), calls={}, declared=declared
    )

    assert verdict is False


class _InApp(Condition):
    """A user condition that reads ``entry.package`` and says so."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def __call__(self, entry: LogEntry, /) -> bool:
        self.calls.append(entry.package)
        return entry.package == APP

    def packages(self) -> frozenset[str]:
        return frozenset({APP})


def test_a_condition_that_names_its_packages_is_not_called_while_unknown() -> None:
    """A packages() override marks a leaf as package-dependent: it is called
    once, with the package, after the lookup."""
    in_app = _InApp()
    harness = _running(in_app | Tag("x"))
    in_app.calls.clear()

    harness.stdout(line("held", tag="y"))
    assert in_app.calls == []
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)

    assert in_app.calls == [APP]
    assert harness.messages() == ["held"]


class _AppLines(And):
    """An And whose function child reads the package; the override says so."""

    def packages(self) -> frozenset[str]:
        return frozenset({APP})


def test_a_combination_that_overrides_packages_is_one_leaf() -> None:
    """An override of packages() is not looked inside (A.17): taken apart, the
    Where below would see package=None and filter the line at once."""
    condition = _AppLines(Where(lambda entry: entry.package == APP), Tag("App"))
    harness = _running(condition | Tag("x"))

    harness.stdout(line("held"))
    assert harness.core._attribution.held == 1
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)

    assert harness.messages() == ["held"]


_EVALUATE = [
    ("package", Package(APP), "x", None),
    ("package-ruled-out", Package(OTHER), "x", False),
    ("or-tag", Package(APP) | Tag("x"), "x", True),
    ("or-unknown", Package(APP) | Tag("x"), "y", None),
    ("and-tag", Package(APP) & Tag("x"), "y", False),
    ("not-not", ~~Package(APP), "x", None),
    ("where-of-leaf", Where(Package(APP)), "x", None),
    ("where-of-or", Where(Package(APP) | Tag("x")), "x", True),
    ("filter-empty", Filter(), "x", True),
    ("filter", Filter(package=APP, tag="x"), "y", False),
    ("anr-text", AnrCondition(package=APP) & Package(APP), "x", False),
    ("process-name", Package(f"{APP}:remote") | Tag("y"), "x", False),
]


@pytest.mark.parametrize(
    ("condition", "tag", "expected"),
    [case[1:] for case in _EVALUATE],
    ids=[case[0] for case in _EVALUATE],
)
def test_evaluate_is_kleene_over_the_tree(
    condition: Condition,
    tag: str,
    expected: bool | None,  # noqa: FBT001  (a parameter)
) -> None:
    """True, False, or None (depends on the package) with APP unknown and
    OTHER ruled out; with the package known it matches a plain call."""
    entry = _entry(tag=tag)
    declared = declared_packages(condition)

    verdict = evaluate(
        condition, entry, unknown=frozenset({APP}), calls={}, declared=declared
    )

    assert verdict is expected
    for package in (APP, None):
        known = entry.model_copy(update={"package": package})
        plain = evaluate(
            condition, known, unknown=frozenset(), calls={}, declared=declared
        )
        assert plain is condition(known)


def test_deep_conditions_with_an_unknown_package_do_not_recurse() -> None:
    """F7-4: the tri-state walk and the leaf scan have no recursion."""
    depth = sys.getrecursionlimit() * 3
    nested: Condition = Package(APP)
    for _ in range(depth):
        nested = ~nested
    mixed: Condition = Package(APP)
    for index in range(depth):
        mixed = (mixed | Tag("t")) if index % 2 else (mixed & Tag("t"))
    harness = _running(nested if depth % 2 == 0 else ~nested)

    harness.stdout(line("deep"))
    assert harness.core._attribution.held == 1
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)

    assert harness.messages() == ["deep"]
    entry = _entry(tag="t")
    declared = declared_packages(mixed)
    assert evaluate(mixed, entry, unknown=frozenset({APP}), calls={}, declared=declared)


# -- the end of a connection, a detach and the conclusion ------------------------------


def test_held_lines_go_before_the_end_of_their_connection() -> None:
    """3.6: entries of a connection come before its change to RECONNECTING;
    what is still unknown then is evaluated without a package."""
    harness = _running(~Package(APP), reconnect=Reconnect(delay=5.0))
    harness.stdout(line("held"))

    harness.end_connection(1)

    records = _records(harness)
    held = next(
        index
        for index, record in enumerate(records)
        if isinstance(record, LogEntry) and record.message == "held"
    )
    change = next(
        index
        for index, record in enumerate(records)
        if isinstance(record, StateChange) and record.new is S.RECONNECTING
    )
    assert held < change
    assert harness.core.stats().unattributed == 1
    assert harness.core._attribution.held == 0


def test_the_conclusion_releases_held_lines_before_the_groups_flush() -> None:
    """A.21: held lines go through where and grouping inside the conclusion,
    before the groups are flushed and before the drain check."""
    harness = _running(
        ~Package(APP),
        group_by=GroupBy(keys=(), gap_ms=60_000.0, idle_flush_ms=None),
        package_hold=30.0,
    )
    harness.stdout(line("grouped", uid=1000) + line("held"))
    assert harness.core._attribution.held == 1

    harness.send(StopRequest())
    harness.end_connection(0)

    (group,) = harness.delivered()
    assert isinstance(group, LogGroup)
    assert [entry.message for entry in group] == ["first", "grouped", "held"]
    assert group.reason == "end"
    assert harness.core.mode is Mode.DRAIN
    assert harness.core.stats().unattributed == 1
    harness.consume()
    assert harness.core.state is S.STOPPED


@pytest.mark.parametrize("request_", ["kill", "fail"])
def test_a_kill_or_failure_discards_held_lines(request_: str) -> None:
    """STOPPING(kill or fail): held lines and open groups are discarded and
    counted, at the conclusion at the latest."""
    harness = _running(
        ~Package(APP),
        group_by=GroupBy(keys=(), gap_ms=60_000.0, idle_flush_ms=None),
        package_hold=30.0,
    )
    harness.stdout(line("held"))
    if request_ == "kill":
        harness.send(KillRequest())
    else:
        harness.send(CallbackFailed("on_item", ValueError("boom")))
    harness.end_connection(-9)

    stats = harness.core.stats()
    assert harness.core._attribution.held == 0
    assert stats.undelivered >= 2  # the held line and the open group
    assert harness.core.state is (S.KILLED if request_ == "kill" else S.FAILED)
    assert harness.delivered() == []


def test_where_raising_on_a_released_line_fails_the_run() -> None:
    """F3-1 / fail fast: the failure ends the run; the lines held after the
    one where raised on are discarded and counted."""

    def check(entry: LogEntry) -> bool:
        if entry.message == "boom":
            raise RuntimeError("where raised")
        return False

    harness = _running(Package(APP) | Where(check), stall_timeout=None)
    harness.stdout(line("a") + line("boom", uid=1000) + line("c"))
    assert harness.core._attribution.held == 3

    harness.until_deadline()
    harness.answer("lookup", LOOKUP_OK)

    outcome = harness.core.outcome
    assert isinstance(outcome, CallbackError)
    assert outcome.stage == "where"
    assert harness.messages() == ["a"]
    assert harness.core.mode is Mode.FAIL
    assert harness.core._attribution.held == 0
    assert harness.core.stats().undelivered == 2


def test_a_detach_releases_held_lines_and_stops_holding() -> None:
    """3.5: with no consumer nobody waits for a package: held lines go on (and
    are discarded and counted), later lines are not held."""
    harness = _running(~Package(APP))
    harness.stdout(line("held"))

    harness.detach()
    harness.stdout(line("later"))

    stats = harness.core.stats()
    assert harness.core._attribution.held == 0
    assert stats.unattributed == 2
    assert stats.undelivered >= 2
    assert harness.core.mode is Mode.STOP
