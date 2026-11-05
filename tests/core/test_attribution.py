"""Tests for package attribution in the stream core (design 5.8, D-8, 5.10; PR-11).

A fake clock drives every timer: the harness's ``now`` (`tests.core.harness`)
for the core, plain floats for `Attribution` on its own. The lookup is
``adb shell dumpsys package <pkg>`` (A.8); `tests.core.harness.lookup` scripts
its answers. The F5-2 behavior tests of `tests/behavior` flip in PR-12/PR-13,
when the drivers run this core.
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from logpyt import (
    AnrCondition,
    Condition,
    CrashCondition,
    Filter,
    GroupBy,
    Level,
    MessageContains,
    MinLevel,
    Package,
    Tag,
    Where,
)
from logpyt._core import attribution as attribution_module
from logpyt._core.attribution import (
    LOOKUP_INTERVAL,
    UID_CACHE,
    Answer,
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
from logpyt.filters import And, Not, Or
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
    from logpyt._core.attribution import Lookup
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
    """A.8: ``shell dumpsys package <pkg>``, one at a time, in name order."""
    harness = Harness.make(where=Package(OTHER) | Package(APP), serial="emulator-5554")
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


@pytest.mark.parametrize(
    "uid", [1000, 0, "root", "zzzzz", 10 * 100_000 + 1000, 10 * 100_000], ids=str
)
def test_system_uids_are_known_and_never_held(uid: int | str) -> None:
    """5.8: a system uid (< 10000, of any user: by app id) or a name that is
    no uid has no package."""
    harness = _running(Package(APP) | Tag("Sys"))

    harness.stdout(line("system", uid=uid, tag="Sys"))

    assert harness.messages() == ["first", "system"]
    assert harness.core._attribution.held == 0
    assert len(_lookups(harness)) == 1  # nothing asked for a re-lookup
    assert harness.core.next_deadline() is None


@pytest.mark.parametrize("uid", [99_001, "u0_i5", 10 * 100_000 + 90_000], ids=str)
def test_isolated_uids_are_known_and_never_held(uid: int | str) -> None:
    """A.14: an isolated process's uid (app id 90000 and above) is no app's
    own uid, so no lookup can name it: its lines go on at once, without a
    package and not unattributed, and ask for no lookup."""
    harness = _running(Package(APP) | Tag("Sys"))

    harness.stdout(line("isolated", uid=uid, tag="Sys") + line("other", uid=uid))

    assert harness.messages() == ["first", "isolated"]
    assert harness.core._attribution.held == 0
    assert harness.core.next_deadline() is None
    assert len(_lookups(harness)) == 1
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (1, 0)
    # With APP mapped, such a line asks for no re-lookup either.
    mapped = _attribution(Package(APP))
    started = mapped.next_lookup(0.0)
    assert started is not None
    mapped.lookup_done(started.ident, LOOKUP_OK)
    assert mapped.admit(_entry(uid=99_001), 1.0) is not None
    assert mapped.deadline() is None


def test_a_system_uid_package_warns_once_and_over_matches() -> None:
    """5.8 caveat: a watched package with a system uid gets one WARNING, also
    when later lookups answer that uid again, and every line of that uid is
    attributed to it."""
    harness = Harness.make(where=Package(APP))
    harness.connecting_spawned()

    harness.answer("lookup", lookup(1000))
    harness.stdout(line("system_server", uid=1000, pid=555))
    harness.stdout(line("third party", uid=10_099))  # asks for APP once more
    harness.until_deadline()
    harness.answer("lookup", lookup(1000))

    assert len(_lookups(harness)) == 2
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
    # Where(c) matches like c (filters.Where), a Where of a Package too.
    ("where-later", Where(Package(OTHER)) | (Package(APP) & Tag("never")), True),
    ("not-where", ~Where(Where(Package(OTHER))) | (Package(APP) & Tag("z")), False),
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
    new uid are attributed and the old uid names nothing. The line that went
    on before the answer was APP's: it counts as unattributed (D2)."""
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
    assert (stats.filtered, stats.unattributed) == (2, 1)  # crash 1 and old
    # The old uid is unknown now: its line waits for APP's next answer, which
    # names the new uid again, so it was not APP's and is forgotten.
    assert attribution.unsettled == 1
    harness.until_deadline()
    harness.answer("lookup", lookup(new_uid))
    assert attribution.unsettled == 0
    assert harness.core.stats().unattributed == 1


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


def test_the_deadline_is_the_earliest_due_relookup() -> None:
    """X-3: of two wanted re-lookups the timer fires for the one due first
    (APP at 100.5, not OTHER at 100.9), before the line's short hold expires."""
    harness = Harness.make(where=Package(APP) | Package(OTHER), package_hold=0.3)
    harness.connecting_spawned()  # APP's first lookup at t=100.0
    harness.send(Tick(), at=100.4)
    harness.answer("lookup", LOOKUP_ABSENT)  # OTHER's first lookup at t=100.4
    harness.answer("lookup", LOOKUP_ABSENT)
    harness.send(Tick(), at=100.42)

    harness.stdout(line("l2"))  # APP's uid: held until 100.72

    assert harness.core.next_deadline() == pytest.approx(100.5)
    harness.until_deadline()
    assert harness.request("lookup").argv[-1] == APP
    harness.answer("lookup", LOOKUP_OK)
    assert [(item.message, item.package) for item in _entries(harness)] == [("l2", APP)]
    assert harness.core.stats().unattributed == 0


def _slow_rounds(harness: Harness, moved: int | None = None) -> list[str]:
    """Six rounds of 0.6 s lookups (longer than LOOKUP_INTERVAL) from t=100.5,
    after the first two; a line from a new app uid comes during each. OTHER
    answers ``moved`` (None: "not installed"), APP "not installed". Returns
    the packages of the re-lookups.
    """
    harness.stdout(line("n0", uid=11_000, tag="Other"))
    harness.until_deadline()  # t=100.5: the first re-lookup
    for index in range(1, 7):
        harness.stdout(line(f"n{index}", uid=11_000 + index, tag="Other"))
        harness.advance(0.6)
        found = harness.request("lookup").argv[-1] == OTHER and moved is not None
        harness.answer("lookup", lookup(moved, OTHER) if found else LOOKUP_ABSENT)
    return [command.argv[-1] for command in _lookups(harness)[2:]]


def test_slow_relookups_without_a_uid_take_turns() -> None:
    """X-3 without starvation: with slow lookups and a new app uid during each,
    both packages (no uid) are due again at every answer. They take turns
    (the longest since its last lookup first), so the lines that wait for
    them do not expire."""
    harness = Harness.make(where=Package(APP) | Package(OTHER))
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_ABSENT)  # APP
    harness.answer("lookup", LOOKUP_ABSENT)  # OTHER

    asked = _slow_rounds(harness)

    assert asked == [APP, OTHER, APP, OTHER, APP, OTHER, APP]
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (5, 0)
    assert harness.core._attribution.held == 2  # n5, n6: the 7th answer is owed


def test_a_slow_mapped_relookup_waits_at_most_package_hold() -> None:
    """X-3 without starvation: as above with OTHER mapped. Lines wait for APP
    (no uid) only, so APP goes first; OTHER, passed over since t=100.5, goes
    first once that is package_hold (2 s) ago, at t=102.9: its reinstall under
    a new uid is found, and no line expires."""
    moved = 10_120
    harness = Harness.make(where=Package(APP) | Package(OTHER))
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_ABSENT)  # APP
    harness.answer("lookup", lookup(OTHER_UID, OTHER))

    asked = _slow_rounds(harness, moved)

    assert asked == [APP] * 4 + [OTHER] + [APP] * 2
    assert harness.core._attribution.uid_packages == {moved: OTHER}
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (6, 0)
    assert harness.core._attribution.held == 1  # n6: the 7th answer is owed
    harness.answer("lookup", LOOKUP_ABSENT)
    harness.stdout(line("moved", uid=moved))
    (entry,) = _entries(harness)
    assert (entry.message, entry.package) == ("moved", OTHER)


def test_an_app_installed_during_slow_relookups_keeps_its_launch_lines() -> None:
    """D-8 (a crash at app launch), X-3: a line from a new app uid (the
    installer) asks for re-lookups of all three packages; ZZZ (no uid) goes
    first, but the install comes just after it started: "not installed". The
    launch lines wait for ZZZ's next re-lookup, which goes before those of the
    mapped packages (passed over for less than package_hold), so with 0.7 s
    lookups every launch line is ZZZ's before its hold expires."""
    zzz, zzz_uid = "com.example.zzz", 10_200
    harness = Harness.make(where=Package(APP) | Package(OTHER) | Package(zzz))
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_OK)  # APP
    harness.answer("lookup", lookup(OTHER_UID, OTHER))
    harness.answer("lookup", lookup(None, zzz))
    harness.send(Tick(), at=105.0)
    harness.stdout(line("installer", uid=10_300))  # ZZZ's re-lookup starts
    for index in range(11):  # the launch: a line every 0.1 s from t=105.3
        harness.send(Tick(), at=105.3 + 0.1 * index)
        if index == 4:  # t=105.7
            harness.answer("lookup", lookup(None, zzz))  # started before the install
            assert harness.request("lookup").argv[-1] == zzz
        harness.stdout(line(f"u{index}", uid=zzz_uid))

    harness.send(Tick(), at=106.4)
    harness.answer("lookup", lookup(zzz_uid, zzz))

    entries = [(item.message, item.package) for item in _entries(harness)]
    assert entries == [(f"u{index}", zzz) for index in range(11)]
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (1, 0)  # the installer's line
    asked = [command.argv[-1] for command in _lookups(harness)[3:]]
    assert asked == [zzz, zzz, APP]  # then the mapped packages' re-lookups


def test_slow_relookups_of_three_packages_take_turns() -> None:
    """X-3: of the packages without a uid, the one longest since its last
    lookup goes first, so with three slow ones none is left out (by name,
    THIRD would wait for package_hold: APP and OTHER pass each other over in
    turn)."""
    third = "com.example.third"
    harness = Harness.make(
        where=Package(APP) | Package(OTHER) | Package(third), package_hold=5.0
    )
    harness.connecting_spawned()
    for _ in range(3):
        harness.answer("lookup", LOOKUP_ABSENT)  # the first lookups, at t=100
    harness.stdout(line("n0", uid=11_000, tag="Other"))
    harness.until_deadline()  # t=100.5: the first re-lookup

    for index in range(1, 7):
        harness.stdout(line(f"n{index}", uid=11_000 + index, tag="Other"))
        harness.advance(0.6)
        harness.answer("lookup", LOOKUP_ABSENT)

    asked = [command.argv[-1] for command in _lookups(harness)[3:]]
    assert asked == [APP, OTHER, third] * 2 + [APP]
    assert harness.core.stats().unattributed == 0


def test_a_uid_seen_before_a_first_lookup_asks_for_no_extra_one() -> None:
    """A uid first seen while OTHER's first lookup is still to come is settled
    by that lookup: it asks for APP's re-lookup only."""
    harness = Harness.make(where=Package(APP) | Package(OTHER), package_hold=1.0)
    harness.connecting_spawned()  # APP's first lookup in flight
    harness.advance(1.0)  # its wait expired: intake opens
    harness.stdout(line("x", uid=10_099))
    harness.answer("lookup", LOOKUP_ABSENT)  # APP's first lookup
    harness.answer("lookup", LOOKUP_ABSENT)  # OTHER's, issued after x came

    harness.answer("lookup", LOOKUP_ABSENT)  # APP's re-lookup, due at once

    assert [command.argv[-1] for command in _lookups(harness)] == [APP, OTHER, APP]
    assert harness.core.next_deadline() is None
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (1, 0)


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


def test_a_held_line_waits_only_for_the_packages_it_may_belong_to() -> None:
    """APP answered "not installed" after the line came: APP has no uid, but
    the line is not APP's, so its verdict no longer needs a package (OTHER's
    And is false for tag y) before OTHER's re-lookup."""
    harness = Harness.make(where=Package(APP) | (Package(OTHER) & Tag("x")))
    harness.connecting_spawned()
    harness.answer("lookup", LOOKUP_ABSENT)  # APP
    harness.answer("lookup", LOOKUP_ABSENT)  # OTHER
    harness.stdout(line("held", uid=10_071, tag="y"))
    assert harness.core._attribution.held == 1
    harness.until_deadline()
    assert harness.request("lookup").argv[-1] == APP

    harness.answer("lookup", LOOKUP_ABSENT)

    assert harness.core._attribution.held == 0
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
    assert alone.lookup_done(started.ident + 1, LOOKUP_OK) == Answer()
    assert alone.next_lookup(0.0) is None  # still in flight
    assert alone.uid_packages == {}
    assert alone.lookup_done(started.ident, LOOKUP_ABSENT) == Answer()
    assert alone.lookup_done(started.ident, LOOKUP_OK) == Answer()  # answered
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
    # CrashCondition reads the text, not the package (A.17): false here.
    ("crash-text", Package(APP) & CrashCondition(package=APP), "x", "filtered"),
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


_PURE = [
    ("tag", Tag("x")),
    ("level", Level("E")),
    ("min-level", MinLevel("W")),
    ("message", MessageContains("never")),
    ("crash", CrashCondition(package=APP)),
    ("anr", AnrCondition(package=APP)),
    ("where-tag", Where(Tag("x"))),
    ("where-where", Where(Where(Tag("x")))),
    ("where-package", Where(Package(OTHER))),
]


@pytest.mark.parametrize("leaf", [case[1] for case in _PURE], ids=[c[0] for c in _PURE])
def test_a_built_in_leaf_decides_past_an_unknown_package(leaf: Condition) -> None:
    """filters: built-in conditions change nothing, so evaluate calls them past
    an unknown verdict; false for the line, each decides Package(APP) & leaf
    at once (a Package, in a Where too, is decided without a call)."""
    entry = _entry(tag="App")  # level I, message "m"
    assert leaf(entry) is False
    condition = Package(APP) & leaf

    verdict = evaluate(
        condition,
        entry,
        unknown=frozenset({APP}),
        calls={},
        declared=declared_packages(condition),
    )

    assert verdict is False


def test_every_built_in_leaf_is_covered() -> None:
    """Each condition evaluate may call past an unknown verdict is in _PURE."""
    leaves = [attribution_module._unwrap(leaf) for _, leaf in _PURE]
    covered = {type(leaf).__call__ for leaf in leaves} - {Package.__call__}
    assert covered == attribution_module._PURE_CALLS


def test_a_held_line_runs_a_shared_leaf_once_at_its_release() -> None:
    """A leaf that plain evaluation calls twice (it appears twice) runs once
    for a held line, also when nothing ran while its package was unknown, and
    for a line from an unknown app uid queued behind it."""
    seen: list[str] = []

    def check(entry: LogEntry) -> bool:
        seen.append(entry.message)
        return True

    leaf = Where(check)
    harness = _running((Package(APP) | leaf) & leaf)
    seen.clear()

    harness.stdout(line("held", uid=10_099) + line("queued", uid=10_098))
    assert seen == []
    assert harness.core._attribution.held == 2
    harness.until_deadline()
    harness.answer("lookup", LOOKUP_ABSENT)  # not APP's: the Or needs the leaf

    assert seen == ["held", "queued"]
    assert harness.messages() == ["first", "held", "queued"]


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
    """A failure gives up only the lines that still wait for that package: APP
    has a uid, so l1 waits for ZZZ alone (ZZZ's lookup that ran when l1 came
    says nothing about it), and it stays, with l2 behind it, although APP's
    re-lookup fails; ZZZ's next answer releases both."""
    zzz = "com.example.zzz"
    harness = Harness.make(where=Package(APP) | Package(zzz))
    harness.connecting_spawned()
    harness.answer("lookup", lookup(OTHER_UID))  # APP: another uid, id 1, t=100
    harness.answer("lookup", LOOKUP_ABSENT)  # ZZZ, id 2, t=100
    harness.send(Tick(), at=100.5)
    harness.stdout(line("l0", uid=10_080))
    assert harness.request("lookup").argv[-1] == zzz  # id 3: no uid, first
    harness.stdout(line("l1", uid=10_071))  # first seen while id 3 runs
    harness.send(Tick(), at=100.6)
    harness.answer("lookup", LOOKUP_ABSENT)  # l0 is no one's; l1 waits for ZZZ
    assert harness.request("lookup").argv[-1] == APP  # id 4; ZZZ due at 101.0
    harness.stdout(line("l2", uid=10_072))

    harness.answer("lookup", LOOKUP_FAILED)

    assert harness.core._attribution.held == 2
    assert harness.core.stats().unattributed == 0
    harness.until_deadline()  # t=101.0: ZZZ's re-lookup
    assert harness.request("lookup").argv[-1] == zzz
    harness.answer("lookup", LOOKUP_ABSENT)
    assert harness.core._attribution.held == 0
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (3, 0)


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


class _ErrorsOf(Package):
    """A Package whose ``__call__`` also needs level E: a user leaf (A.17)."""

    def __call__(self, entry: LogEntry, /) -> bool:
        return entry.level == "E" and super().__call__(entry)


class _Strict(And):
    """An And whose ``__call__`` also needs the tag Strict: a user leaf (A.17)."""

    def __call__(self, entry: LogEntry, /) -> bool:
        return entry.tag == "Strict" and super().__call__(entry)


_EVALUATE = [
    ("package", Package(APP), "x", None),
    ("package-call", _ErrorsOf(APP), "x", None),
    ("and-call", _Strict(Package(APP), Tag("x")), "x", None),
    ("or-and-call", Tag("y") | _Strict(Package(APP), Tag("x")), "x", None),
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
    # A process name lives in the text conditions only (D1): decided.
    ("process-text", CrashCondition(package=f"{APP}:remote") | Tag("y"), "x", False),
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


def test_held_lines_join_their_group_before_the_connection_ends() -> None:
    """3.6 with grouping: at a connection's end the held line goes through
    grouping before the groups flush, so it ends its group, and that group
    comes before the change to RECONNECTING."""
    harness = _running(
        ~Package(APP),
        reconnect=Reconnect(delay=5.0),
        group_by=GroupBy(keys=(), gap_ms=60_000.0, idle_flush_ms=None),
    )
    harness.stdout(line("grouped", uid=1000) + line("held"))
    assert harness.core._attribution.held == 1

    harness.end_connection(1)

    records = _records(harness)
    groups = [index for index, item in enumerate(records) if isinstance(item, LogGroup)]
    (index,) = groups
    group = records[index]
    assert isinstance(group, LogGroup)
    assert [entry.message for entry in group] == ["first", "grouped", "held"]
    change = next(
        position
        for position, record in enumerate(records)
        if isinstance(record, StateChange) and record.new is S.RECONNECTING
    )
    assert index < change


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


# -- watched names are package names (D1) ----------------------------------------------


class _Names(Condition):
    """A user condition whose packages() override returns ``names`` as given."""

    def __init__(self, *names: object) -> None:
        self.names = names

    def __call__(self, entry: LogEntry, /) -> bool:
        del entry
        return True

    def packages(self) -> frozenset[str]:
        return self.names  # ty: ignore[invalid-return-type]


@pytest.mark.parametrize(
    ("name", "error", "match"),
    [
        (f"{APP}:remote", ValueError, rf"names a process.*package name is '{APP}'"),
        ("a b", ValueError, "is not a package name"),
        (42, TypeError, "must be a str, not int"),
    ],
    ids=["process", "grammar", "not-str"],
)
def test_the_core_checks_the_watched_names_at_construction(
    name: object, error: type[Exception], match: str
) -> None:
    """D1: a packages() override may return any string; the core checks each
    name when it is built (F5-3, no process name: a uid names an app), not
    at the first lookup inside handle()."""
    where = Tag("x") | _Names(OTHER, name)

    with pytest.raises(error, match=match) as raised:
        Harness.make(where=where)

    assert str(raised.value).startswith(f"where.packages() returned {name!r}: ")
    assert ":<process>" not in str(raised.value)  # D1: the grammar has none
    with pytest.raises(error, match=match):
        _attribution(where)


class _OneString(Condition):
    """A user condition whose packages() override returns one str."""

    def __call__(self, entry: LogEntry, /) -> bool:
        return entry.package == "myapp"

    def packages(self) -> frozenset[str]:
        return "myapp"  # ty: ignore[invalid-return-type]


@pytest.mark.parametrize("at", ["root", "nested"])
def test_the_core_refuses_a_packages_override_that_returns_a_str(at: str) -> None:
    """D1: a str from packages() would be read one character at a time (m, y,
    a, p watched, or '.' refused for a dotted name): it raises TypeError at
    construction, at the root of where or below it."""
    where = _OneString() if at == "root" else Tag("x") | _OneString()
    pattern = r"_OneString\.packages\(\) must return a collection of package names"

    with pytest.raises(TypeError, match=rf"{pattern}, not str"):
        Harness.make(where=where)


# -- a reinstall's lines before its answer (D2) ----------------------------------------

NEW_UID = 10_090  # APP's uid after a reinstall (APP_UID before)


def _mapped(where: Condition, *answers: _AdbResult, **fields: Any) -> Harness:  # noqa: ANN401
    """RUNNING at t=100 after the first lookups answered ``answers`` (default:
    APP at APP_UID), with one system-uid line read."""
    harness = Harness.make(where=where, **fields)
    harness.connecting_spawned()
    for answer in answers or (LOOKUP_OK,):
        harness.answer("lookup", answer)
    harness.stdout(line("first", uid=1000, tag="Sys"))
    assert harness.core.state is S.RUNNING
    return harness


def test_reinstall_lines_before_the_answer_are_counted() -> None:
    """D2 (a): APP is mapped, so the lines of its new uid before the re-lookup
    answers go on at once, filtered and not held (D-8); the answer that
    names the new uid counts each of them in unattributed, once, and later
    lines are APP's."""
    harness = _mapped(Package(APP))
    attribution = harness.core._attribution

    harness.stdout(b"".join(line(f"n{i}", uid=NEW_UID) for i in range(3)))
    harness.send(Tick(), at=100.2)
    harness.stdout(line("n3", uid=NEW_UID) + line("n4", uid=NEW_UID + 100_000))

    assert attribution.held == 0
    assert harness.messages() == []
    assert (harness.core.stats().filtered, harness.core.stats().unattributed) == (6, 0)
    assert attribution.unsettled == 5
    harness.until_deadline()  # t=100.5: APP's re-lookup
    harness.answer("lookup", lookup(NEW_UID))
    harness.stdout(line("after", uid=NEW_UID))

    stats = harness.core.stats()
    assert stats.unattributed == 5
    assert "unattributed" in stats.incomplete
    assert attribution.unsettled == 0
    assert [(item.message, item.package) for item in _entries(harness)] == [
        ("after", APP)
    ]
    assert attribution.deadline() is None  # no line asks for a lookup
    harness.stdout(line("again", uid=NEW_UID))
    assert harness.core.stats().unattributed == 5


def test_another_apps_new_uid_is_not_counted() -> None:
    """D2: the answer says APP kept its uid, so the lines of the new uid were
    another app's: their verdict was right, they are forgotten."""
    harness = _mapped(Package(APP))
    harness.stdout(line("a", uid=NEW_UID) + line("b", uid=NEW_UID))
    harness.until_deadline()

    harness.answer("lookup", LOOKUP_OK)

    assert harness.core._attribution.unsettled == 0
    assert harness.core._attribution.negative(NEW_UID)
    assert (harness.core.stats().filtered, harness.core.stats().unattributed) == (3, 0)


def test_only_lines_whose_verdict_depends_on_the_package_are_counted() -> None:
    """D2, tri-state: Package(APP) | Tag("x") is true for tag x without the
    package (delivered, package=None, never counted); a tag y line was
    filtered for want of it and is counted."""
    harness = _mapped(Package(APP) | Tag("x"))
    harness.stdout(line("x1", uid=NEW_UID, tag="x") + line("y1", uid=NEW_UID, tag="y"))
    assert harness.core._attribution.unsettled == 1
    harness.until_deadline()

    harness.answer("lookup", lookup(NEW_UID))

    assert [(item.message, item.package) for item in _entries(harness)] == [
        ("x1", None)
    ]
    assert harness.core.stats().unattributed == 1


def test_two_watched_packages_count_the_lines_of_the_one_found() -> None:
    """D2 with APP and OTHER mapped: a tag t line of the new uid depends on
    OTHER only (Package(OTHER) & Tag("t")), a tag p line on APP only. APP's
    answer names the new uid: the tag p lines count, the tag t line waits for
    OTHER's answer, which names OTHER's own uid: it is forgotten."""
    where = (Package(APP) & Tag("p")) | (Package(OTHER) & Tag("t"))
    harness = _mapped(where, LOOKUP_OK, lookup(OTHER_UID, OTHER))
    attribution = harness.core._attribution
    harness.stdout(
        line("p1", uid=NEW_UID, tag="p")
        + line("t1", uid=NEW_UID, tag="t")
        + line("p2", uid=NEW_UID, tag="p")
    )
    harness.until_deadline()
    assert harness.request("lookup").argv[-1] == APP

    harness.answer("lookup", lookup(NEW_UID))
    assert harness.core.stats().unattributed == 2
    assert attribution.unsettled == 1  # t1
    assert harness.request("lookup").argv[-1] == OTHER
    harness.answer("lookup", lookup(OTHER_UID, OTHER))

    assert harness.core.stats().unattributed == 2
    assert attribution.unsettled == 0


def test_a_shared_uid_counts_each_line_once() -> None:
    """D2 with a sharedUserId: APP and OTHER both answer the new uid. A line
    that depends on both counts at APP's answer only; one that depends on
    OTHER only counts at OTHER's answer."""
    either = Package(APP) | Package(OTHER)
    where = (
        (Package(APP) & Tag("p")) | (Package(OTHER) & Tag("t")) | (either & Tag("b"))
    )
    harness = _mapped(where, LOOKUP_OK, lookup(OTHER_UID, OTHER))
    attribution = harness.core._attribution
    harness.stdout(
        line("b1", uid=NEW_UID, tag="b")
        + line("p1", uid=NEW_UID, tag="p")
        + line("t1", uid=NEW_UID, tag="t")
    )
    harness.until_deadline()
    assert harness.request("lookup").argv[-1] == APP

    harness.answer("lookup", lookup(NEW_UID))
    assert harness.core.stats().unattributed == 2  # b1 and p1
    assert harness.request("lookup").argv[-1] == OTHER
    harness.answer("lookup", lookup(NEW_UID, OTHER))

    assert harness.core.stats().unattributed == 3  # t1
    assert attribution.unsettled == 0
    assert attribution.uid_packages == {NEW_UID: APP}


ZZZ = "com.example.zzz"  # a watched package that is not installed


def test_a_held_line_released_while_a_mapped_package_is_owed_is_counted() -> None:
    """D2: a line of APP's new uid waits for ZZZ (no uid); ZZZ's answer
    releases it while APP's re-lookup is still owed, so it goes on without
    its package and counts when APP's answer names the new uid."""
    harness = _mapped(Package(APP) | Package(ZZZ), LOOKUP_OK, lookup(None, ZZZ))
    harness.stdout(line("x1", uid=NEW_UID))
    assert harness.core._attribution.held == 1
    harness.until_deadline()
    assert harness.request("lookup").argv[-1] == ZZZ

    harness.answer("lookup", lookup(None, ZZZ))
    assert harness.core._attribution.held == 0
    assert harness.core.stats().unattributed == 0
    assert harness.request("lookup").argv[-1] == APP
    harness.answer("lookup", lookup(NEW_UID))

    assert (harness.core.stats().filtered, harness.core.stats().unattributed) == (2, 1)


def _queued_behind_a_held_line() -> Harness:
    """x1 of APP's new uid was held for ZZZ and went on (owed: APP); then y1 of
    another app is held for ZZZ, and x2 of the new uid, which now owes APP's
    answer only, queues behind it. APP's re-lookup is in flight."""
    harness = _mapped(Package(APP) | Package(ZZZ), LOOKUP_OK, lookup(None, ZZZ))
    harness.stdout(line("x1", uid=NEW_UID))
    harness.until_deadline()
    harness.answer("lookup", lookup(None, ZZZ))  # x1 goes on, owed APP
    assert harness.request("lookup").argv[-1] == APP
    harness.stdout(line("y1", uid=10_099) + line("x2", uid=NEW_UID))
    attribution = harness.core._attribution
    assert attribution.held == 2
    assert attribution._queue[1].seen == attribution_module._KNOWN  # x2: no hold
    return harness


def test_a_queued_line_is_counted_when_the_answer_comes_later() -> None:
    """D2: x2 is evaluated at the head with what is known then (APP still
    owed): it is recorded, and counts with x1 when APP's answer names the new
    uid. APP's failed re-lookup on the way keeps both for the next answer."""
    harness = _queued_behind_a_held_line()
    attribution = harness.core._attribution

    harness.answer("lookup", LOOKUP_FAILED)
    assert attribution.unsettled == 1
    harness.until_deadline()
    assert harness.request("lookup").argv[-1] == ZZZ
    harness.answer("lookup", lookup(None, ZZZ))  # y1, then x2 go on
    assert attribution.held == 0
    assert harness.core.stats().unattributed == 0
    assert attribution.unsettled == 3  # x1, x2, and y1 (it owes APP's too)
    assert harness.request("lookup").argv[-1] == APP
    harness.answer("lookup", lookup(NEW_UID))

    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (4, 2)  # x1 and x2
    assert attribution.unsettled == 0


def test_a_queued_line_whose_uid_was_named_meanwhile_gets_its_package() -> None:
    """D2: APP's answer names the new uid while x2 waits behind y1. x2 is
    evaluated at the head with what is known then: it goes on as APP's,
    matched and not counted; x1 counts at the answer."""
    harness = _queued_behind_a_held_line()

    harness.answer("lookup", lookup(NEW_UID))
    assert harness.core.stats().unattributed == 1  # x1
    assert harness.core._attribution.held == 2
    harness.until_deadline()
    harness.answer("lookup", lookup(None, ZZZ))  # y1, then x2 go on

    assert [(item.message, item.package) for item in _entries(harness)] == [("x2", APP)]
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (3, 1)
    assert harness.core._attribution.unsettled == 1  # y1 waits for APP's next
    harness.answer("lookup", lookup(NEW_UID))
    assert harness.core._attribution.unsettled == 0
    assert harness.core.stats().unattributed == 1


def test_a_queued_line_whose_uid_turned_negative_is_plain() -> None:
    """D2: APP's answer keeps APP's uid while x2 waits behind y1, so no
    watched package owns the new uid: at the head x2 is resolved again (its
    app id goes into the negative cache) and takes a plain verdict; nothing
    waits for it, and x1 is forgotten."""
    harness = _queued_behind_a_held_line()
    attribution = harness.core._attribution

    harness.answer("lookup", LOOKUP_OK)
    assert attribution.unsettled == 0  # x1: its verdict was right
    harness.until_deadline()
    harness.answer("lookup", lookup(None, ZZZ))  # y1, then x2 go on

    assert attribution.held == 0
    assert attribution._seen[NEW_UID] is None  # resolved again at the head
    assert _entries(harness) == []
    stats = harness.core.stats()
    assert (stats.filtered, stats.unattributed) == (4, 0)
    assert attribution.unsettled == 1  # y1 waits for APP's next answer


@pytest.mark.parametrize("leaf", ["package", "reads"])
def test_a_queued_line_on_a_uid_shared_meanwhile(leaf: str) -> None:
    """D2: APP and OTHER both answer the new uid (a sharedUserId) while x2 waits
    behind y1. At the head x2 takes its package, the first owner: its Package
    leaves match both owners, and a leaf that reads entry.package sees APP.
    It is matched and not counted (only x1 is)."""
    reads = _OfApp()
    first = Package(APP) if leaf == "package" else reads
    where = first | (Package(OTHER) & Tag("nomatch")) | Package(ZZZ)
    harness = _mapped(where, LOOKUP_OK, lookup(OTHER_UID, OTHER), lookup(None, ZZZ))
    harness.stdout(line("x1", uid=NEW_UID))
    harness.until_deadline()
    assert harness.request("lookup").argv[-1] == ZZZ
    harness.answer("lookup", lookup(None, ZZZ))  # x1 goes on, owed APP and OTHER
    harness.stdout(line("y1", uid=10_099) + line("x2", uid=NEW_UID))
    assert harness.core._attribution.held == 2
    assert harness.request("lookup").argv[-1] == APP

    harness.answer("lookup", lookup(NEW_UID))
    harness.answer("lookup", lookup(NEW_UID, OTHER))
    assert harness.core._attribution.uid_packages == {NEW_UID: APP}
    assert harness.core.stats().unattributed == 1  # x1
    harness.until_deadline()
    harness.answer("lookup", lookup(None, ZZZ))  # y1, then x2 go on

    assert harness.core._attribution.held == 0
    assert [(item.message, item.package) for item in _entries(harness)] == [("x2", APP)]
    assert harness.core.stats().unattributed == 1
    if leaf == "reads":
        assert ("x2", APP) in reads.calls


def test_a_line_counted_at_its_release_is_not_counted_again() -> None:
    """D2 never counts a line twice: x1 waited for ZZZ until its hold expired
    (unattributed then), so APP's later answer naming its uid adds nothing."""
    harness = _mapped(
        Package(APP) | Package(ZZZ), LOOKUP_OK, lookup(None, ZZZ), package_hold=0.4
    )
    harness.stdout(line("x1", uid=NEW_UID))
    harness.until_deadline()  # t=100.4: the hold expired
    assert harness.core.stats().unattributed == 1
    harness.until_deadline()  # t=100.5: ZZZ's re-lookup
    harness.answer("lookup", lookup(None, ZZZ))
    harness.answer("lookup", lookup(NEW_UID))

    assert harness.core.stats().unattributed == 1
    assert harness.core._attribution.unsettled == 0


def test_lines_under_force_are_counted_when_the_answer_comes() -> None:
    """D2 with a detached consumer (force): the new uid's lines go on (and are
    discarded and counted as undelivered); APP's answer still counts them."""
    harness = _mapped(Package(APP) | Tag("App"))
    harness.detach()
    assert harness.core.mode is Mode.STOP
    harness.stdout(line("x1", uid=NEW_UID, tag="y") + line("x2", uid=NEW_UID, tag="y"))
    assert harness.core._attribution.unsettled == 2
    harness.until_deadline()

    harness.answer("lookup", lookup(NEW_UID))

    assert harness.core.stats().unattributed == 2


@pytest.mark.parametrize("end", ["stop", "kill"])
def test_lines_waiting_at_the_end_are_not_counted(end: str) -> None:
    """D2 caveat: the run concludes before the re-lookup answers; the waiting
    lines are dropped uncounted (`close`, for a stop and a kill alike)."""
    harness = _mapped(Package(APP))
    harness.stdout(line("x1", uid=NEW_UID))
    assert harness.core._attribution.unsettled == 1

    harness.send(StopRequest() if end == "stop" else KillRequest())
    harness.end_connection(0)

    assert harness.core._attribution.unsettled == 0
    assert harness.core.stats().unattributed == 0
    assert harness.core.state is (S.STOPPING if end == "stop" else S.KILLED)


def _first_mapped(where: Condition, *answers: _AdbResult) -> Attribution:
    """An `Attribution` at t=0 whose first lookups answered ``answers`` in name
    order (default: APP at APP_UID)."""
    attribution = _attribution(where)
    for answer in answers or (LOOKUP_OK,):
        started = attribution.next_lookup(0.0)
        assert started is not None
        assert started.first
        attribution.lookup_done(started.ident, answer)
    assert attribution.ready
    return attribution


def _answer_next(
    attribution: Attribution, now: float, result: _AdbResult, package: str = APP
) -> Answer:
    """Run the next lookup at ``now`` (of ``package``) and take ``result``."""
    started = attribution.next_lookup(now)
    assert started is not None
    assert started.package == package
    return attribution.lookup_done(started.ident, result)


def test_an_evicted_app_id_drops_its_lines() -> None:
    """5.10: the lines of an app id go with it when the LRU evicts it; the
    answer that names it then counts nothing."""
    attribution = _first_mapped(Package(APP))
    verdict = attribution.admit(_entry(uid=NEW_UID), 1.0)
    assert verdict is not None
    assert not verdict.matched
    for uid in range(20_000, 20_000 + UID_CACHE):
        attribution.admit(_entry(uid=uid, tag="x"), 1.0)
    assert NEW_UID not in attribution._seen
    assert NEW_UID not in attribution._owing
    assert len(attribution._owing) <= UID_CACHE - len(attribution.uid_packages)

    answer = _answer_next(attribution, 1.0, lookup(NEW_UID))

    assert answer == Answer((), 0)
    assert attribution.unsettled == 0  # the others were not APP's either


@pytest.mark.parametrize("how", ["discard", "close"])
def test_discard_and_close_drop_the_lines(how: str) -> None:
    """D2: `discard` (a kill or failure) and `close` (the conclusion) drop the
    lines that wait for an answer, uncounted."""
    attribution = _first_mapped(Package(APP))
    attribution.admit(_entry(uid=NEW_UID), 1.0)
    started = attribution.next_lookup(1.0)
    assert started is not None
    assert attribution.unsettled == 1

    if how == "discard":
        assert attribution.discard() == 0
    else:
        attribution.close()

    assert attribution.unsettled == 0
    assert attribution.lookup_done(started.ident, lookup(NEW_UID)).unattributed == 0


def test_a_failed_relookup_keeps_the_lines_for_the_next_answer() -> None:
    """D2: a failure settles nothing; the next answer that names the uid
    counts the line."""
    attribution = _first_mapped(Package(APP))
    attribution.admit(_entry(uid=NEW_UID), 1.0)

    failed = _answer_next(attribution, 1.0, LOOKUP_FAILED)
    assert failed.unattributed == 0
    assert len(failed.warnings) == 1
    assert attribution.unsettled == 1
    attribution.admit(_entry(uid=NEW_UID), 1.5)  # asks for the next re-lookup
    answer = _answer_next(attribution, 2.0, lookup(NEW_UID))

    assert answer.unattributed == 2
    assert attribution.unsettled == 0


def test_an_answer_in_flight_when_the_uid_came_still_counts_it() -> None:
    """D2: an answer issued before the new uid's first line settles nothing
    about it when it names another uid, but one that names the new uid
    counts its lines whenever it was issued."""
    attribution = _first_mapped(Package(APP))
    attribution.admit(_entry(uid=10_099), 1.0)
    in_flight = attribution.next_lookup(1.0)
    assert in_flight is not None
    attribution.admit(_entry(uid=NEW_UID), 1.1)  # first seen while it runs

    answer = attribution.lookup_done(in_flight.ident, lookup(NEW_UID))

    assert answer.unattributed == 1
    assert attribution.unsettled == 0  # 10099: the answer was issued after it


def test_an_answer_in_flight_when_the_uid_came_strikes_nothing() -> None:
    """D2, X-3: an answer issued before the new uid's first line that names
    APP's old uid says nothing about the new uid: its line waits on, and the
    next answer that names the new uid counts both of its lines."""
    attribution = _first_mapped(Package(APP))
    attribution.admit(_entry("o1", uid=10_099), 1.0)
    in_flight = attribution.next_lookup(1.0)
    assert in_flight is not None
    attribution.admit(_entry("n1", uid=NEW_UID), 1.1)
    assert attribution.unsettled == 2

    answer = attribution.lookup_done(in_flight.ident, LOOKUP_OK)

    assert answer.unattributed == 0
    assert attribution.unsettled == 1  # o1 was another app's; n1 waits on
    attribution.admit(_entry("n2", uid=NEW_UID), 1.6)
    assert _answer_next(attribution, 1.6, lookup(NEW_UID)).unattributed == 2
    assert attribution.unsettled == 0


def test_an_answer_strikes_its_package_and_keeps_the_others() -> None:
    """D2: a line that depends on APP and OTHER (~Package([APP, OTHER])
    matched it). APP's answer keeps APP's uid: APP is struck, the line waits
    for OTHER, whose answer names the new uid and counts it."""
    where = ~Package([APP, OTHER])
    attribution = _first_mapped(where, LOOKUP_OK, lookup(OTHER_UID, OTHER))
    verdict = attribution.admit(_entry("b1", uid=NEW_UID), 1.0)
    assert verdict is not None
    assert verdict.matched
    assert attribution.unsettled == 1

    assert _answer_next(attribution, 1.0, LOOKUP_OK).unattributed == 0
    assert attribution.unsettled == 1
    answer = _answer_next(attribution, 1.0, lookup(NEW_UID, OTHER), OTHER)

    assert answer.unattributed == 1
    assert attribution.unsettled == 0


def test_an_app_id_the_map_lets_go_drops_its_lines() -> None:
    """D2, 5.10: APP moves to the new uid (the map takes it out of the LRU),
    then on to another one. The new uid is then in neither: its line that
    waited for OTHER is dropped, and OTHER's answer naming it counts 0."""
    where = (Package(APP) & Tag("p")) | (Package(OTHER) & Tag("t"))
    attribution = _first_mapped(where, LOOKUP_OK, lookup(OTHER_UID, OTHER))
    attribution.admit(_entry("t1", uid=NEW_UID, tag="t"), 1.0)  # owes OTHER
    assert attribution.unsettled == 1
    assert _answer_next(attribution, 1.0, lookup(NEW_UID)).unattributed == 0
    assert attribution.unsettled == 1
    assert _answer_next(attribution, 1.0, LOOKUP_FAILED, OTHER).unattributed == 0
    attribution.admit(_entry("o1", uid=10_099), 2.0)

    assert _answer_next(attribution, 2.0, lookup(10_098)).unattributed == 0

    assert NEW_UID not in attribution.uid_packages
    assert NEW_UID not in attribution._seen
    assert attribution.unsettled == 0
    attribution.admit(_entry("o2", uid=10_097), 2.6)
    assert _answer_next(attribution, 2.6, lookup(NEW_UID, OTHER), OTHER) == Answer()


def test_a_line_decided_while_a_package_has_no_uid_is_recorded() -> None:
    """D2: ZZZ has no uid, so the hold's verdict takes it as unknown; a tag y
    line is decided without it (false) and goes on at once. It still owes
    APP's answer: ZZZ's answer settles nothing for it, APP's naming the new
    uid counts it."""
    where = Package(APP) | (Package(ZZZ) & Tag("z"))
    attribution = _first_mapped(where, LOOKUP_OK, lookup(None, ZZZ))
    verdict = attribution.admit(_entry("y1", uid=NEW_UID, tag="y"), 1.0)
    assert verdict is not None
    assert (verdict.matched, verdict.unattributed) == (False, False)
    assert attribution.held == 0
    assert attribution.unsettled == 1

    assert _answer_next(attribution, 1.0, lookup(None, ZZZ), ZZZ) == Answer()
    answer = _answer_next(attribution, 1.0, lookup(NEW_UID))

    assert answer.unattributed == 1
    assert attribution.unsettled == 0


class _Fn:
    """A function for Where(fn) that records each call; true for tag x."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, entry: LogEntry) -> bool:
        self.calls.append((entry.message, entry.package))
        return entry.tag == "x"


class _OfApp(Condition):
    """A user leaf that reads entry.package, names APP, and records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, entry: LogEntry, /) -> bool:
        self.calls.append((entry.message, entry.package))
        return entry.package == APP

    def packages(self) -> frozenset[str]:
        return frozenset({APP})


_ONCE: list[tuple[str, Any, int]] = [
    # (id, condition from a Where(fn) and an APP leaf, lines x1/y1 counted)
    ("package-first", lambda a, b: Package(APP) & a, 2),  # a is left uncalled
    ("leaf-first", lambda a, b: a & Package(APP), 1),
    ("shared-leaf", lambda a, b: (Package(APP) | a) & a, 0),
    ("named", lambda a, b: b | Tag("z"), 2),
    ("named-after", lambda a, b: (a & Tag("x")) | b, 1),
    ("guarded", lambda a, b: (Package(APP) | a) & (b | a), 1),
    ("decided", lambda a, b: Tag("x") | (Package(APP) & a), 1),
]


@pytest.mark.parametrize(
    ("build", "counted"), [case[1:] for case in _ONCE], ids=[c[0] for c in _ONCE]
)
def test_lines_that_go_on_call_each_leaf_once_and_only_where_plain_would(
    build: Any,  # noqa: ANN401  (a builder of the condition)
    counted: int,
) -> None:
    """D2, leaf calls: the verdict and what it depends on together call a user
    leaf (a Where(fn), a condition naming APP) at most once per line, and only
    where a plain where(entry) with package=None calls it."""
    plain_a, plain_b = _Fn(), _OfApp()
    plain = build(Where(plain_a), plain_b)
    leaf_a, leaf_b = _Fn(), _OfApp()
    harness = _mapped(build(Where(leaf_a), leaf_b))
    leaf_a.calls.clear()
    leaf_b.calls.clear()
    messages = [("x1", "x"), ("y1", "y")]

    harness.stdout(b"".join(line(text, uid=NEW_UID, tag=tag) for text, tag in messages))
    harness.until_deadline()
    harness.answer("lookup", lookup(NEW_UID))

    assert harness.core._attribution.held == 0
    for text, tag in messages:
        plain(_entry(text, uid=NEW_UID, tag=tag))
    for leaf, reference in ((leaf_a, plain_a), (leaf_b, plain_b)):
        assert len(leaf.calls) == len(set(leaf.calls))  # once per line
        assert set(leaf.calls) <= set(reference.calls)  # only where plain would
    assert harness.core.stats().unattributed == counted


class _Raises:
    """A function for Where(fn) that records each call and raises for one line."""

    def __init__(self, message: str, *, value: bool = False) -> None:
        self.message = message
        self.value = value
        self.calls: list[str] = []

    def __call__(self, entry: LogEntry) -> bool:
        self.calls.append(entry.message)
        if entry.message == self.message:
            msg = f"raised for {entry.message}"
            raise RuntimeError(msg)
        return self.value


def test_what_a_held_line_depends_on_calls_no_leaf_its_verdict_did_not() -> None:
    """D2: the hold's walk reaches the shared F past an unknown verdict first
    (uncalled) and calls it later, unguarded. What the line depends on reuses
    F's verdict there, so its walk reaches G unguarded: G is unknown, not
    called (it raises for n1), and the line goes on as before."""
    f = _Fn()
    g = _Raises("n1")
    shared = Where(f)
    first = Or(Package(ZZZ), shared)
    where = Or(And(first, Where(g), Tag("nope")), shared, Package(APP))
    harness = _mapped(where, LOOKUP_OK, lookup(None, ZZZ))
    f.calls.clear()

    harness.stdout(line("n1", uid=NEW_UID, tag="x"))

    assert harness.core.state is S.RUNNING
    assert (f.calls, g.calls) == ([("n1", None)], [])
    assert [(item.message, item.package) for item in _entries(harness)] == [
        ("n1", None)
    ]
    assert harness.core._attribution.unsettled == 0  # true without APP


def test_a_queued_line_on_a_shared_uid_takes_its_owners() -> None:
    """D2: APP and OTHER answer x1's uid while x1 waits behind y0. At the head
    x1 takes its package (APP, the first owner), and its verdict is true by
    Package(APP), so h is not called for it."""
    h = _Raises("x1")
    where = Or(Package(ZZZ), Package(APP), Where(h), Package(OTHER))
    attribution = _first_mapped(
        where, LOOKUP_OK, lookup(OTHER_UID, OTHER), lookup(None, ZZZ)
    )
    assert attribution.admit(_entry("x0", uid=NEW_UID), 1.0) is None
    assert _answer_next(attribution, 1.0, lookup(None, ZZZ), ZZZ).unattributed == 0
    assert attribution.release(1.0) is not None  # x0, owed APP and OTHER
    assert attribution.admit(_entry("y0", uid=10_095), 1.1) is None
    assert attribution.admit(_entry("x1", uid=NEW_UID), 1.2) is None
    assert _answer_next(attribution, 1.2, lookup(NEW_UID)).unattributed == 1  # x0
    _answer_next(attribution, 1.2, lookup(NEW_UID, OTHER), OTHER)
    h.calls.clear()

    assert attribution.release(1.3, force=True) is not None  # y0
    verdict = attribution.release(1.3, force=True)

    assert verdict is not None
    assert (verdict.entry.message, verdict.entry.package) == ("x1", APP)
    assert (verdict.matched, verdict.unattributed) == (True, False)
    assert h.calls == ["y0"]
    assert attribution.unsettled == 0


def test_a_queued_line_on_a_mapped_uid_owes_no_answer() -> None:
    """D2: APP and OTHER answer x2's uid while it waits; THIRD, still owed, may
    share it too. At the head x2 takes its package and the owners known then,
    like any line of a mapped uid: its verdict is false and nothing waits for
    THIRD's answer, which names the uid and counts x1 only (it went on
    without its package and depends on THIRD)."""
    where = (
        Package(ZZZ) | (Package(THIRD) & Tag("t")) | (Package([APP, OTHER]) & Tag("n"))
    )
    answers = (LOOKUP_OK, lookup(OTHER_UID, OTHER), lookup(10_080, THIRD))
    attribution = _first_mapped(where, *answers, lookup(None, ZZZ))
    assert attribution.admit(_entry("x1", uid=NEW_UID, tag="t"), 1.0) is None
    _answer_next(attribution, 1.0, lookup(None, ZZZ), ZZZ)
    assert attribution.release(1.0) is not None  # x1 depends on THIRD
    assert attribution.admit(_entry("y0", uid=10_095), 1.1) is None
    assert attribution.admit(_entry("x2", uid=NEW_UID, tag="t"), 1.2) is None
    assert _answer_next(attribution, 1.2, lookup(NEW_UID)) == Answer()
    _answer_next(attribution, 1.2, lookup(NEW_UID, OTHER), OTHER)
    third = attribution.next_lookup(1.2)
    assert third is not None
    assert third.package == THIRD
    released = attribution.release(3.1)  # y0's hold expired
    assert released is not None
    assert released.unattributed

    verdict = attribution.release(3.1)

    assert verdict is not None
    assert (verdict.entry.message, verdict.entry.package) == ("x2", APP)
    assert (verdict.matched, verdict.unattributed) == (False, False)
    assert attribution.unsettled == 1  # x1
    answer = attribution.lookup_done(third.ident, lookup(NEW_UID, THIRD))
    assert answer.unattributed == 1


def _grown_while_held(where: Condition) -> tuple[Attribution, Lookup]:
    """x1 of the new uid waits for ZZZ (no uid); APP's re-lookup, issued before
    x1's uid was first seen, answers that APP is not installed, so APP may own
    it too (the held line's packages without a uid grow); ZZZ's next answer
    leaves APP alone. Returns the attribution at t=2.0 and the lookup of APP
    asked for next (not run yet)."""
    attribution = _first_mapped(where, LOOKUP_OK, lookup(None, ZZZ))
    assert attribution.admit(_entry("y0", uid=10_099, tag="z"), 1.0) is not None
    _answer_next(attribution, 1.0, lookup(None, ZZZ), ZZZ)
    in_flight = attribution.next_lookup(1.05)
    assert in_flight is not None
    assert in_flight.package == APP
    assert attribution.admit(_entry("x1", uid=NEW_UID, tag="x"), 1.1) is None
    attribution.lookup_done(in_flight.ident, LOOKUP_ABSENT)
    seen = attribution._seen[NEW_UID]
    assert seen is not None
    assert attribution._candidates(seen) == {APP, ZZZ}
    assert attribution.release(1.2) is None
    _answer_next(attribution, 2.0, lookup(None, ZZZ), ZZZ)
    assert attribution._candidates(seen) == {APP}
    relookup = attribution.next_lookup(2.0)
    assert relookup is not None
    assert relookup.package == APP
    return attribution, relookup


def test_a_held_lines_stale_call_records_its_package() -> None:
    """D2: x1 called the APP leaf while APP was mapped (package=None, false),
    before APP grew into its packages without a uid. That stale call decides
    x1 once ZZZ answers: it goes on as before, filtered and not counted, but
    records APP, so APP's answer that names its uid counts it. No leaf is
    called twice."""
    leaf = _OfApp()
    attribution, relookup = _grown_while_held(leaf | Package(ZZZ) | Tag("z"))
    assert leaf.calls == [("y0", None), ("x1", None)]

    verdict = attribution.release(2.0)

    assert verdict is not None
    assert (verdict.entry.message, verdict.entry.package) == ("x1", None)
    assert (verdict.matched, verdict.unattributed) == (False, False)
    assert attribution.held == 0
    assert attribution.unsettled == 1
    answer = attribution.lookup_done(relookup.ident, lookup(NEW_UID))
    assert answer.unattributed == 1
    assert leaf.calls == [("y0", None), ("x1", None)]


def test_a_held_line_without_a_stale_call_records_nothing() -> None:
    """D2: as above, but Tag("y") decides x1's And before the APP leaf, so x1
    never called it: x1 is decided with APP unknown, records nothing, and
    APP's answer that names its uid counts nothing (x1 is not APP's match
    either)."""
    leaf = _OfApp()
    where = (Tag("y") & leaf) | Package(ZZZ) | Tag("z")
    attribution, relookup = _grown_while_held(where)

    verdict = attribution.release(2.0)

    assert verdict is not None
    assert (verdict.entry.message, verdict.matched) == ("x1", False)
    assert attribution.unsettled == 0
    assert attribution.lookup_done(relookup.ident, lookup(NEW_UID)) == Answer()
    assert leaf.calls == []


def _taken_while_held(where: Condition, *, grown: bool) -> Attribution:
    """x1 of the new uid waits for ZZZ (no uid) and calls the APP leaves that
    it reaches with package=None; then APP's answer maps the new uid while
    x1 still waits. ``grown``: APP's earlier re-lookup said it is not
    installed, so x1 waited for APP too (`_grown_while_held`); else APP was
    mapped elsewhere all along (ZZZ's answer, issued before x1 came, settles
    nothing for it). Returns the attribution at t=2.0 (x1 not released)."""
    if grown:
        attribution, relookup = _grown_while_held(where)
    else:
        attribution = _first_mapped(where, LOOKUP_OK, lookup(None, ZZZ))
        assert attribution.admit(_entry("y0", uid=10_099, tag="z"), 0.6) is not None
        in_flight = attribution.next_lookup(0.6)
        assert in_flight is not None
        assert in_flight.package == ZZZ
        assert attribution.admit(_entry("x1", uid=NEW_UID, tag="x"), 0.7) is None
        attribution.lookup_done(in_flight.ident, lookup(None, ZZZ))
        assert attribution.release(0.8) is None
        relookup = attribution.next_lookup(0.8)  # ZZZ is not due again yet
        assert relookup is not None
        assert relookup.package == APP
    assert attribution.lookup_done(relookup.ident, lookup(NEW_UID)) == Answer()
    assert attribution.uid_packages == {NEW_UID: APP}
    return attribution


@pytest.mark.parametrize("app", ["mapped", "grown"])
def test_a_held_line_that_takes_its_package_counts_a_stale_call(app: str) -> None:
    """D2: x1 called the APP leaf with package=None while APP was mapped
    elsewhere (false), then APP's answer named its uid while it waited. At
    its release x1 is APP's and reuses that call, which decides its verdict:
    false, where plain where(entry) is true. It goes on with that verdict
    (no leaf is called twice) and counts as unattributed."""
    leaf = _OfApp()
    never = _Raises("none")
    where = leaf | (Package(APP) & Where(never)) | Package(ZZZ) | Tag("z")
    attribution = _taken_while_held(where, grown=app == "grown")

    verdict = attribution.release(2.0)

    assert verdict is not None
    assert (verdict.entry.message, verdict.entry.package) == ("x1", APP)
    assert (verdict.matched, verdict.unattributed) == (False, True)
    assert (leaf.calls, never.calls) == ([("y0", None), ("x1", None)], ["x1"])
    assert attribution.unsettled == 0
    assert where(verdict.entry)


@pytest.mark.parametrize("decider", ["package", "fresh"])
def test_a_held_line_that_takes_its_package_without_needing_a_stale_call(
    decider: str,
) -> None:
    """D2: as above, but the verdict at the release is true whatever the
    stale APP call says: Package(APP) decides it, or a second APP leaf that
    the hold left uncalled (past the unknown ZZZ) and the release calls with
    the package. x1 goes on matched and is not counted."""
    leaf, second = _OfApp(), _OfApp()
    last = Package(APP) if decider == "package" else second
    attribution = _taken_while_held(leaf | Package(ZZZ) | last | Tag("z"), grown=False)

    verdict = attribution.release(2.0)

    assert verdict is not None
    assert (verdict.entry.message, verdict.entry.package) == ("x1", APP)
    assert (verdict.matched, verdict.unattributed) == (True, False)
    assert leaf.calls == [("y0", None), ("x1", None)]
    assert second.calls == ([("x1", APP)] if decider == "fresh" else [])


def test_a_queued_line_from_the_negative_cache_matches_no_owner() -> None:
    """D2 caveat: x1 of an app id in the negative cache queues behind y0;
    meanwhile APP and OTHER both answer that app id (a sharedUserId). At the
    head x1 is not resolved again: it goes on with package=None, and its
    Package leaves match no owner, so its verdict is plain where(entry)'s."""
    where = Package(APP) | Package(OTHER) | (Tag("w") & Package(ZZZ))
    answers = (LOOKUP_OK, lookup(OTHER_UID, OTHER), lookup(None, ZZZ))
    attribution = _first_mapped(where, *answers)
    assert attribution.admit(_entry("x0", uid=NEW_UID, tag="q"), 1.0) is not None
    for package, answer in zip(
        (ZZZ, APP, OTHER), (answers[2], *answers[:2]), strict=True
    ):
        _answer_next(attribution, 1.0, answer, package)
    assert attribution.negative(NEW_UID)
    assert attribution.admit(_entry("y0", uid=10_099, tag="w"), 2.0) is None
    assert attribution.admit(_entry("x1", uid=NEW_UID, tag="w"), 2.0) is None
    _answer_next(attribution, 2.6, lookup(None, ZZZ), ZZZ)
    _answer_next(attribution, 2.6, lookup(NEW_UID), APP)
    _answer_next(attribution, 2.6, lookup(NEW_UID, OTHER), OTHER)
    assert attribution._shared == {NEW_UID: {APP, OTHER}}
    released = attribution.release(2.6)
    assert released is not None
    assert released.entry.message == "y0"

    verdict = attribution.release(2.6)

    assert verdict is not None
    assert (verdict.entry.message, verdict.entry.package) == ("x1", None)
    assert (verdict.matched, verdict.unattributed) == (False, False)
    assert not where(verdict.entry)


def test_a_stale_package_is_asked_about_not_taken_as_unknown() -> None:
    """D2: x waits for ZZZ; APP grows into its packages without a uid (its
    re-lookup, issued before x's uid was first seen, says APP is not
    installed) after x called the APP leaf. When ZZZ answers, that stale
    call decides x (true). APP is asked about like OTHER (mapped, owed),
    not taken as unknown: x depends on APP alone, as OTHER matters only
    while APP is unknown. So OTHER's answer that names x's uid counts
    nothing (as OTHER's, x matches too); APP's answer would count it."""
    where = _Names(APP) & (~Package(OTHER) | ~Package(APP)) & ~Package(ZZZ)
    answers = (LOOKUP_OK, lookup(OTHER_UID, OTHER), lookup(None, ZZZ))
    attribution = _first_mapped(where, *answers)
    assert attribution.admit(_entry("y", uid=10_099), 0.5) is None
    assert attribution.release(0.5, force=True) is not None
    _answer_next(attribution, 0.9, lookup(None, ZZZ), ZZZ)
    in_flight = attribution.next_lookup(1.0)
    assert in_flight is not None
    assert in_flight.package == APP
    assert attribution.admit(_entry("x", uid=NEW_UID), 1.1) is None
    attribution.lookup_done(in_flight.ident, LOOKUP_ABSENT)
    assert attribution.release(1.2) is None
    _answer_next(attribution, 2.0, lookup(None, ZZZ), ZZZ)

    verdict = attribution.release(2.0)

    assert verdict is not None
    assert (verdict.entry.package, verdict.matched, verdict.unattributed) == (
        None,
        True,
        False,
    )
    assert attribution._owing[NEW_UID].lines == {frozenset({APP}): 1}
    assert where(verdict.entry.model_copy(update={"package": OTHER}))
    assert _answer_next(attribution, 2.5, LOOKUP_FAILED).unattributed == 0
    assert _answer_next(attribution, 3.0, lookup(NEW_UID, OTHER), OTHER) == Answer()
    assert attribution.unsettled == 1  # APP's answer settles x


def test_an_e1_line_whose_app_id_was_evicted_goes_on_and_records() -> None:
    """D2, E1: x2 of APP's new uid (only APP owed it an answer when it came)
    queues behind y1; while it waits, the LRU evicts its app id. At the head
    the app id is seen anew and owes APP and ZZZ (no uid) an answer, but x2
    still goes on at once, before its hold could expire, and records both:
    APP's answer that names the uid counts it."""
    attribution = _first_mapped(
        Package(APP) | Package(ZZZ), LOOKUP_OK, lookup(None, ZZZ)
    )
    assert attribution.admit(_entry("x1", uid=NEW_UID), 1.0) is None
    _answer_next(attribution, 1.0, lookup(None, ZZZ), ZZZ)
    assert attribution.release(1.0) is not None  # x1, owed APP
    in_flight = attribution.next_lookup(1.0)
    assert in_flight is not None
    assert in_flight.package == APP
    assert attribution.admit(_entry("y1", uid=10_099), 1.1) is None
    assert attribution.admit(_entry("x2", uid=NEW_UID), 1.2) is None
    assert attribution._queue[1].seen == attribution_module._KNOWN
    for uid in range(20_000, 20_000 + UID_CACHE):
        attribution.admit(_entry("f", uid=uid, tag="f"), 1.3)
    assert NEW_UID not in attribution._seen
    attribution.lookup_done(in_flight.ident, LOOKUP_OK)  # issued before x2 came
    _answer_next(attribution, 1.6, lookup(None, ZZZ), ZZZ)
    released = attribution.release(1.6)
    assert released is not None
    assert released.entry.message == "y1"

    verdict = attribution.release(1.6)  # x2's hold would expire at 3.2

    assert verdict is not None
    assert (verdict.entry.message, verdict.entry.package) == ("x2", None)
    assert (verdict.matched, verdict.unattributed) == (False, False)
    assert attribution._owing[NEW_UID].lines == {frozenset({APP, ZZZ}): 1}
    assert _answer_next(attribution, 2.0, lookup(NEW_UID)).unattributed == 1


def test_a_line_admitted_after_close_records_nothing() -> None:
    """D2: once the run concluded (`close`) no answer comes, so a line that
    goes on without its package records nothing (the core admits no line
    after it closed the attribution)."""
    attribution = _first_mapped(Package(APP))
    attribution.close()

    verdict = attribution.admit(_entry(uid=NEW_UID), 1.0)

    assert verdict is not None
    assert (verdict.matched, verdict.unattributed) == (False, False)
    assert attribution.unsettled == 0
    assert attribution.next_lookup(1.0) is None


# -- what a line that went on depends on: a randomized property test (D2) --------------

THIRD = "com.example.third"
_WATCHED = (APP, OTHER, THIRD)
_TAGS = ("x", "y", "z")


class _TableLeaf(Condition):
    """A user leaf that keeps the packages() contract: it reads entry.package
    only to tell its own ``names`` apart. Its verdict is a random table; each
    call is logged."""

    def __init__(
        self, rng: random.Random, names: frozenset[str], log: list[int]
    ) -> None:
        self.names = names
        self.log = log
        self.table = {
            (tag, package): rng.random() < 0.5
            for tag in _TAGS
            for package in (None, *sorted(names))
        }

    def value(self, entry: LogEntry) -> bool:
        package = entry.package if entry.package in self.names else None
        return self.table[entry.tag, package]

    def __call__(self, entry: LogEntry, /) -> bool:
        self.log.append(id(self))
        return self.value(entry)

    def packages(self) -> frozenset[str]:
        return self.names


def _random_tree(rng: random.Random, log: list[int]) -> Condition:
    """A random condition over Package, Tag and user leaves, with Not, Where
    of a condition and shared subtrees."""
    made: list[Condition] = []

    def leaf() -> Condition:
        pick = rng.random()
        if pick < 0.35:
            names = rng.sample(_WATCHED, rng.randint(1, 2))
            node: Condition = Package(names)
        elif pick < 0.55:
            node = Tag(rng.choice(_TAGS))
        else:
            count = rng.choice([0, 0, 1, 1, 2])
            node = _TableLeaf(rng, frozenset(rng.sample(_WATCHED, count)), log)
        return Where(node) if rng.random() < 0.15 else node

    def tree(depth: int) -> Condition:
        if made and rng.random() < 0.15:
            return rng.choice(made)  # a shared subtree
        if depth == 0 or rng.random() < 0.3:
            node = leaf()
        elif rng.random() < 0.15:
            node = ~tree(depth - 1)
        else:
            children = [tree(depth - 1) for _ in range(rng.randint(2, 3))]
            node = And(*children) if rng.random() < 0.5 else Or(*children)
            if rng.random() < 0.1:
                node = Where(node)
        made.append(node)
        return node

    return tree(4)


def _oracle(
    node: Condition,
    entry: LogEntry,
    unknown: frozenset[str],
    reopened: frozenset[str],
    known: dict[int, bool],
    calls: set[int] | None,
    *,
    guarded: bool = False,
) -> bool | None:
    """The tri-state verdict, recursively and from the leaves' tables: a
    `Package` meeting ``unknown`` is unknown; a user leaf naming a reopened
    package is unknown; else its earlier verdict (``known``, by the id of the
    leaf as evaluate keys it: a Where of a leaf is one leaf) is used; else it
    is unknown when it names an unknown package, is reached past an unknown
    verdict, or ``calls`` is None (what a line depends on calls nothing);
    else it is called (its id goes into ``calls``, its verdict into
    ``known``)."""
    key = node
    while isinstance(node, Where) and isinstance(node.fn, Condition):
        combination = isinstance(node.fn, (And, Or, Not, Where))
        node = node.fn
        if combination:
            key = node
    context = (entry, unknown, reopened, known, calls)
    if isinstance(node, Package):
        value = None if not node.values.isdisjoint(unknown) else node(entry)
    elif isinstance(node, Tag):
        value = node(entry)
    elif isinstance(node, Not):
        inner = _oracle(node.condition, *context, guarded=guarded)
        value = None if inner is None else not inner
    elif isinstance(node, (And, Or)):
        decisive = isinstance(node, Or)
        value = not decisive
        for child in node.conditions:
            verdict = _oracle(child, *context, guarded=guarded or value is None)
            if verdict is decisive:
                value = decisive
                break
            if verdict is None:
                value = None
    else:
        assert isinstance(node, _TableLeaf)
        value = _oracle_leaf(node, id(key), *context, guarded=guarded)
    return value


def _oracle_leaf(
    leaf: _TableLeaf,
    key: int,
    entry: LogEntry,
    unknown: frozenset[str],
    reopened: frozenset[str],
    known: dict[int, bool],
    calls: set[int] | None,
    *,
    guarded: bool,
) -> bool | None:
    """A user leaf's verdict in `_oracle`."""
    if not leaf.names.isdisjoint(reopened):
        return None
    if key not in known:
        if calls is None or guarded or not leaf.names.isdisjoint(unknown):
            return None
        calls.add(id(leaf))
        known[key] = leaf.value(entry)
    return known[key]


@pytest.mark.parametrize("mode", ["at-once", "decided-held", "grown-held"])
def test_what_a_line_depends_on_matches_a_brute_force_oracle(mode: str) -> None:
    """D2: for random conditions and lines, `Attribution._depends` gives, for
    each watched package still owed, the tri-state answer of an independent
    recursive oracle (one package unknown at a time, so the shortcut of
    asking all first is checked too). It is sound: a package left out, or
    taken as unknown by the verdict, cannot change the verdict (the plain
    verdict with that package agrees). And the verdict and the dependence
    call each user leaf at most once, only where a plain where(entry) with
    package=None calls it (the dependence calls none).

    ``at-once``: the verdict is `_decided` (nothing unknown); ``decided-held``:
    it was decided with the packages without a uid (``asked``) unknown;
    ``grown-held``: as ``decided-held``, but the line was held first with
    fewer packages without a uid, and the leaves it called then may name a
    package that became one later (`_stale`: asked about, not unknown)."""
    rng = random.Random(f"d2-{mode}")  # noqa: S311  (a test, not cryptography)
    kinds = Counter(_dependence_trial(rng, mode, trial) for trial in range(2500))
    if mode == "grown-held":
        # A stale call often decides the verdict, and the line then depends
        # on the package it names.
        assert min(kinds["stale"], kinds["none"]) >= 50, kinds
    else:
        # Each outcome is common, a strict subset of several packages too.
        assert min(kinds["none"], kinds["all"], kinds["some"]) >= 100, kinds


@dataclass(slots=True)
class _Trial:
    """One line of the property test below: its condition and the verdicts of
    the leaves it called (``log`` records each call)."""

    root: Condition
    entry: LogEntry
    attribution: Attribution
    declared: dict[int, frozenset[str]]
    log: list[int]
    calls: dict[int, bool] = field(default_factory=dict)


def _dependence_trial(rng: random.Random, mode: str, number: int) -> str:
    """One trial of the property test above; returns what the line depends
    on: "none", "all" of the packages asked about, "some", "stale" (one that
    a stale call names), "held" (it waits), or "went on" (``grown-held``: it
    was decided before its packages without a uid grew)."""
    trial, first = _trial_line(rng, mode, number)
    plain_verdict = trial.root(trial.entry)  # package=None
    plain = set(trial.log)
    trial.log.clear()
    unknowns = _trial_unknowns(rng, mode, trial, first)
    if unknowns is None:
        return "went on"
    asked, names = unknowns
    verdict = _trial_verdict(mode, trial, asked, plain)
    if verdict is None:
        return "held"  # such a line waits; it does not go on
    assert verdict is plain_verdict
    return _trial_depends(trial, asked, names, verdict, grown=mode == "grown-held")


def _trial_line(
    rng: random.Random, mode: str, number: int
) -> tuple[_Trial, frozenset[str]]:
    """A trial's condition and line; for ``grown-held`` also the package
    without a uid when the line was held first, and often a user leaf it
    then calls first, naming mapped packages, whose call goes stale when one
    of them joins (it does not decide the junction alone)."""
    log: list[int] = []
    root = _random_tree(rng, log)
    entry = _entry(f"t{number}", uid=NEW_UID, tag=rng.choice(_TAGS))
    first: frozenset[str] = frozenset()
    if mode == "grown-held":
        first = frozenset(rng.sample(_WATCHED, 1))
        if rng.random() < 0.7:
            names = frozenset(_some(rng, sorted(set(_WATCHED) - first)))
            leaf = _TableLeaf(rng, names, log)
            junction = And if rng.random() < 0.5 else Or
            leaf.table[entry.tag, None] = junction is And
            root = junction(leaf, root)
    attribution = Attribution(root, adb_path=ADB, serial=None, package_hold=2.0)
    declared = attribution._declared = declared_packages(root)
    return _Trial(root, entry, attribution, declared, log), first


def _trial_unknowns(
    rng: random.Random, mode: str, trial: _Trial, first: frozenset[str]
) -> tuple[frozenset[str], frozenset[str]] | None:
    """A trial's packages without a uid that its verdict takes as unknown,
    and the mapped packages still owed (to ask about); None for a
    ``grown-held`` line decided while held first."""
    if mode != "grown-held":
        asked: frozenset[str] = frozenset()
        if mode == "decided-held":
            asked = frozenset(rng.sample(_WATCHED, rng.randint(1, 2)))
        return asked, frozenset(_some(rng, sorted(set(_WATCHED) - asked)))
    root, entry, calls, declared = trial.root, trial.entry, trial.calls, trial.declared
    if evaluate(root, entry, unknown=first, calls=calls, declared=declared) is not None:
        return None
    # Mapped packages joined, mostly ones that a leaf it called names; the
    # first one mostly answered.
    named = set().union(*(declared[key] for key in calls)) - first
    joined = named if named and rng.random() < 0.8 else set(_WATCHED) - first
    asked = frozenset(_some(rng, sorted(joined)))
    if rng.random() < 0.25:
        asked |= first
    rest = sorted(set(_WATCHED) - asked)
    return asked, frozenset(rng.sample(rest, rng.randint(0, len(rest))))


def _trial_verdict(
    mode: str, trial: _Trial, asked: frozenset[str], plain: set[int]
) -> bool | None:
    """The trial line's verdict, with ``asked`` unknown (`_decided` if none),
    and the check of its calls: each user leaf at most once, only where plain
    evaluation calls it, and in one evaluation exactly the oracle's."""
    root, entry, calls, declared = trial.root, trial.entry, trial.calls, trial.declared
    log = trial.log
    expected: set[int] = set()
    if asked:
        verdict = evaluate(root, entry, unknown=asked, calls=calls, declared=declared)
        if verdict is None:
            return None
        if mode == "decided-held":
            _oracle(root, entry, asked, frozenset(), {}, expected)
    else:
        verdict = attribution_module._decided(root, entry, calls, declared)
        expected = plain
    assert len(log) == len(set(log)), "a user leaf was called twice"
    assert set(log) <= plain
    if mode != "grown-held":  # ``grown-held`` evaluated twice
        assert set(log) == expected
    log.clear()
    return verdict


def _trial_depends(
    trial: _Trial,
    asked: frozenset[str],
    names: frozenset[str],
    verdict: bool,  # noqa: FBT001  (the verdict, not a switch)
    *,
    grown: bool,
) -> str:
    """What the trial line depends on (`Attribution._depends`), against the
    oracle, and that it is sound for every package asked about or unknown.
    The packages of ``asked`` that a stale call names are asked about, as
    `Attribution._judge` does; there are none unless ``grown``."""
    root, entry, calls = trial.root, trial.entry, trial.calls
    before = dict(calls)
    stale = attribution_module._stale(calls, asked, trial.declared)
    assert grown or not stale  # one evaluation calls no leaf naming an unknown
    asked -= stale
    names |= stale

    depends = trial.attribution._depends(entry, calls, names, asked)

    assert trial.log == [], "the dependence called a user leaf"
    assert depends == _oracle_depends(root, entry, names, asked, before), root
    for name in (names | asked) - depends:
        attributed = entry.model_copy(update={"package": name})
        assert _plain_value(root, attributed) is verdict, (root, name)
    if not depends.isdisjoint(stale):
        return "stale"
    return "none" if not depends else "all" if depends == names else "some"


def _some(rng: random.Random, population: list[str]) -> list[str]:
    """One or more of ``population``, at random."""
    return rng.sample(population, rng.randint(1, len(population)))


def _oracle_depends(
    root: Condition,
    entry: LogEntry,
    names: frozenset[str],
    asked: frozenset[str],
    known: dict[int, bool],
) -> frozenset[str]:
    """The packages of ``names`` that `_oracle` finds the verdict depends on,
    each asked alone, with no new call: a leaf not called yet is unknown."""
    found: set[str] = set()
    for name in sorted(names):
        reopened = frozenset({name})
        if _oracle(root, entry, asked | reopened, reopened, dict(known), None) is None:
            found.add(name)
    return frozenset(found)


def _plain_value(
    node: Condition, entry: LogEntry, owners: frozenset[str] = frozenset()
) -> bool:
    """``node(entry)`` from the leaves' tables, without logging a call; with
    ``owners`` (a line attributed to them), a `Package` matches any of them."""
    while isinstance(node, Where) and isinstance(node.fn, Condition):
        node = node.fn
    if isinstance(node, _TableLeaf):
        return node.value(entry)
    if isinstance(node, Not):
        return not _plain_value(node.condition, entry, owners)
    if isinstance(node, And):
        return all(_plain_value(child, entry, owners) for child in node.conditions)
    if isinstance(node, Or):
        return any(_plain_value(child, entry, owners) for child in node.conditions)
    if owners and isinstance(node, Package):
        return not node.values.isdisjoint(owners)
    return node(entry)


# -- lines that go on, through admit and release: a randomized driver (D2) -------------

# App ids that no watched package has at first, and uids that one may have.
_STRANGERS = (10_030, 10_031, 10_032, 10_033, 10_034, 10_035)
_HOMES = (10_010, 10_020)


def test_random_runs_attribute_each_line_as_it_goes_on() -> None:
    """D2, E1, through `admit` and `release`: random lines (from unknown app
    ids, often behind a held line from one that only mapped packages still
    owe an answer, from mapped uids and a system uid), lookup answers (not
    installed, the uid of a line that waits, another unknown app id, a
    mapped uid, a failure) and ticks, with random conditions (half of them
    with a user leaf first that names a mapped package, whose calls go stale
    when that package moves while a line waits). A line that only mapped
    packages owed when it came (an E1 line) is never held; each line that
    goes on is checked as `_Run.went_on` says, and each answer counts what
    the records say (`_Run._settle`).

    The machine's random runs (tests/core/test_machine.py) reach neither E1
    lines nor stale calls; these do (a held line that a stale call decides
    is rare here: the oracle's ``grown-held`` mode above covers it). The
    floors are about half of what the seeds reach today: E1 lines that a
    lookup mapped while they waited, that record what they depend on, and
    whose app id went into the negative cache; held lines that took their
    package but count, as their verdict depends on a stale call; and lines
    that recorded what they depend on."""
    reach: Counter[str] = Counter()
    for seed in range(400):
        run = _Run(seed)
        run.run(steps=150)
        reach.update(run.reach)
    floors = {"E1 attributed": 17, "E1 recorded": 26, "E1 negative": 60}
    floors |= {"stale call counted": 5, "recorded": 2000}
    assert {name: reach[name] for name in floors} == {
        name: max(reach[name], floor) for name, floor in floors.items()
    }, reach


class _Run:
    """One random run of an `Attribution` for the test above."""

    def __init__(self, seed: int) -> None:
        rng = self.rng = random.Random(f"run-{seed}")  # noqa: S311  (a test)
        self.log: list[int] = []
        root = _random_tree(rng, self.log)
        self.first = {name: rng.choice([*_HOMES, None]) for name in _WATCHED}
        if rng.random() < 0.5:
            # A user leaf first that names a mapped package and, with
            # package=None, does not decide the junction; another package
            # has no uid, so lines wait while the leaf's calls go stale.
            mapped, unmapped = rng.sample(_WATCHED, 2)
            self.first |= {mapped: rng.choice(_HOMES), unmapped: None}
            leaf = _TableLeaf(rng, frozenset({mapped}), self.log)
            junction = And if rng.random() < 0.5 else Or
            for tag in _TAGS:
                leaf.table[tag, None] = junction is And
            root = junction(leaf, root)
        # Every package is watched; Tag("never") keeps that from deciding.
        self.root = Or(root, And(Tag("never"), Package(list(_WATCHED))))
        self.attribution = _attribution(self.root)
        self.leaves, self.keys = _table_leaves(self.root)
        self.now = 0.0
        self.flight: Lookup | None = None
        self.count = 0
        self.e1: set[str] = set()  # the E1 lines that wait
        self.since: dict[str, int] = {}  # a line that waits -> `_Held.since`
        self.waited: dict[str, set[int]] = {}  # -> the leaves it called meanwhile
        # A line that recorded what it depends on -> its app id, the app id's
        # seen, and the packages; as `_settle` should keep them.
        self.records: dict[str, tuple[int, int, set[str]]] = {}
        self.reach: Counter[str] = Counter()

    def run(self, steps: int) -> None:
        self.lookup()
        while self.flight is not None and self.flight.first:
            self.answer()
        for _ in range(steps):
            pick = self.rng.random()
            if pick < 0.45:
                self.line()
            elif pick < 0.75:
                self.answer()
            elif pick < 0.97:
                self.now += self.rng.choice([0.05, 0.2, 0.6, 1.5])
                self.attribution.expire(self.now)
                self.lookup()
                self.release()
            else:
                self.release(force=True)
        self.release(force=True)

    def lookup(self) -> None:
        if self.flight is None:
            self.flight = self.attribution.next_lookup(self.now)

    def line(self) -> None:
        rng = self.rng
        attribution = self.attribution
        # Often, behind a held line, one from an app id that only mapped
        # packages still owe an answer: an E1 line.
        e1 = [
            app
            for app, seen in attribution._seen.items()
            if seen is not None
            and attribution._owed(seen)
            and not attribution._candidates(seen)
        ]
        if e1 and attribution.held and rng.random() < 0.5:
            uid = rng.choice(e1)
        elif rng.random() < 0.65:
            uid = rng.choice(_STRANGERS) + (100_000 if rng.random() < 0.1 else 0)
        else:
            uid = rng.choice([*_HOMES, 1000])
        self.count += 1
        entry = _entry(f"n{self.count}", uid=uid, tag=rng.choice(_TAGS))
        before = self._owing(uid)
        mark = len(self.log)
        verdict = attribution.admit(entry, self.now)
        called = set(self.log[mark:])
        if verdict is None:
            waiting = attribution._queue[-1]
            self.since[entry.message] = waiting.since
            self.waited[entry.message] = called
            if waiting.seen == attribution_module._KNOWN != waiting.since:
                self.e1.add(entry.message)
                self.reach["E1 lines"] += 1
            self.reach["held lines"] += 1
            return
        state = attribution._seen.get(uid % 100_000)
        since = attribution_module._KNOWN if state is None else state
        self.went_on(verdict, since, set(), called, before)

    def release(self, *, force: bool = False) -> None:
        attribution = self.attribution
        while attribution.held:
            head = attribution._queue[0].entry
            before = self._owing(head.uid)
            mark = len(self.log)
            verdict = attribution.release(self.now, force=force)
            called = set(self.log[mark:])
            if verdict is None:
                assert head.message not in self.e1, "an E1 line waits"
                self.waited[head.message] |= called
                return
            earlier = self.waited.pop(head.message)
            self.went_on(verdict, self.since.pop(head.message), earlier, called, before)

    def went_on(
        self,
        verdict: attribution_module.Verdict,
        since: int,
        earlier: set[int],
        called: set[int],
        before: Counter[frozenset[str]],
    ) -> None:
        """A line goes on. With ``package=None``, its verdict is plain where
        (also on a uid that watched packages came to share while it waited),
        and it records what the oracle finds it depends on (`_expected`). A
        line from an unknown app uid that a lookup mapped takes its package;
        an attributed line records nothing, and its verdict is plain where
        with the uid's owners, unless it counts: then a leaf that names its
        package was called while it waited (``earlier``), with
        package=None."""
        entry = verdict.entry
        assert entry.uid is not None
        app = entry.uid % 100_000
        e1 = entry.message in self.e1
        attribution = self.attribution
        record = self._owing(entry.uid) - before
        if entry.package is None:
            assert verdict.matched is _plain_value(self.root, entry)
            if since != attribution_module._KNOWN:
                assert app not in attribution.uid_packages
            depends = self._expected(verdict, since, earlier | called, e1=e1)
            assert record == Counter({depends: 1} if depends else {}), self.root
            if depends:
                self.records[entry.message] = (
                    app,
                    attribution._owing[app].seen,
                    set(depends),
                )
                self.reach["recorded"] += 1
                self.reach["E1 recorded"] += e1
            elif e1 and attribution._seen.get(app) is None:
                self.reach["E1 negative"] += 1
            return
        assert not record
        if since != attribution_module._KNOWN:
            assert entry.package == attribution.uid_packages[app]
        owners = attribution._shared.get(app, frozenset({entry.package}))
        stale = any(entry.package in self.leaves[key].names for key in earlier)
        if verdict.unattributed:
            assert stale, self.root
            self.reach["stale call counted"] += 1
        else:
            assert verdict.matched is _plain_value(self.root, entry, owners), self.root
        if e1:
            assert not verdict.unattributed
            self.reach["E1 attributed"] += 1

    def _expected(
        self,
        verdict: attribution_module.Verdict,
        since: int,
        called: set[int],
        *,
        e1: bool,
    ) -> frozenset[str]:
        """What a line that goes on with package=None should record: nothing
        when it is counted already or came from a known uid; else the owed
        packages, but those its verdict took as unknown (packages without a
        uid, for a line that may wait, but those a leaf it called names),
        that `_oracle_depends` finds it depends on, with no new call."""
        entry = verdict.entry
        attribution = self.attribution
        assert entry.uid is not None
        seen = attribution._seen.get(entry.uid % 100_000)
        if verdict.unattributed or since == attribution_module._KNOWN or seen is None:
            return frozenset()
        answered = attribution._answered
        owed = frozenset(name for name in _WATCHED if answered.get(name, 0) <= seen)
        asked: frozenset[str] = frozenset()
        if not e1:
            asked = owed - attribution._uid_of.keys()
            asked -= set().union(*(self.leaves[key].names for key in called))
        known = {self.keys[key]: self.leaves[key].value(entry) for key in called}
        return _oracle_depends(self.root, entry, owed - asked, asked, known)

    def answer(self) -> None:
        flight, self.flight = self.flight, None
        if flight is None:
            return
        pick = self.rng.random()
        uid: int | None
        if flight.first:
            uid = self.first[flight.package]
        elif pick < 0.05:
            answer = self.attribution.lookup_done(flight.ident, LOOKUP_FAILED)
            assert answer.unattributed == 0
            self.lookup()
            self.release()
            return
        elif pick < 0.35:
            uid = None
        elif pick < 0.6 and self.attribution.held:
            # The uid of a line that waits (for a mapped package mostly the
            # head's): a reinstall found meanwhile.
            queue = self.attribution._queue
            mapped = flight.package in self.attribution._uid_of
            uid = (queue[0] if mapped else self.rng.choice(queue)).entry.uid
        else:
            uid = self.rng.choice(_STRANGERS if pick < 0.8 else _HOMES)
        answer = self.attribution.lookup_done(flight.ident, lookup(uid, flight.package))
        assert answer.unattributed == self._settle(flight, uid)
        self.lookup()
        self.release()

    def _settle(self, flight: Lookup, uid: int | None) -> int:
        """Settle the records as the module docs say; return how many the
        answer counts. The records of an app id the map let go are dropped,
        and the rest must be the attribution's."""
        counted = 0
        named = None if uid is None else uid % 100_000
        for message, (app, seen, names) in list(self.records.items()):
            if flight.package not in names:
                continue
            if app == named:
                counted += 1
                del self.records[message]
            elif flight.ident > seen:
                names.discard(flight.package)
                if not names:
                    del self.records[message]
        attribution = self.attribution
        owing = attribution._owing
        for message, (app, _, _) in list(self.records.items()):
            if app not in owing:  # neither in the map nor in the LRU any more
                assert app not in attribution._seen
                assert app not in attribution.uid_packages
                del self.records[message]
        mine = Counter(
            (app, frozenset(names)) for app, _, names in self.records.values()
        )
        theirs = Counter(
            {
                (app, names): count
                for app, lines in owing.items()
                for names, count in lines.lines.items()
            }
        )
        assert mine == theirs
        return counted

    def _owing(self, uid: int | None) -> Counter[frozenset[str]]:
        owing = self.attribution._owing.get(-1 if uid is None else uid % 100_000)
        return Counter() if owing is None else Counter(owing.lines)


def _table_leaves(
    root: Condition,
) -> tuple[dict[int, _TableLeaf], dict[int, int]]:
    """The user leaves of ``root`` by id, and the id that `evaluate` keys
    each by (a Where of a leaf is one leaf)."""
    leaves: dict[int, _TableLeaf] = {}
    keys: dict[int, int] = {}
    pending: list[Condition] = [root]
    walked: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in walked:
            continue
        walked.add(id(node))
        inner = node.fn if isinstance(node, Where) else node
        if isinstance(inner, _TableLeaf):
            leaves[id(inner)] = inner
            keys[id(inner)] = id(node)
        elif isinstance(node, (And, Or)):
            pending.extend(node.conditions)
        elif isinstance(node, Not):
            pending.append(node.condition)
        elif isinstance(node, Where) and isinstance(node.fn, Condition):
            pending.append(node.fn)
    return leaves, keys
