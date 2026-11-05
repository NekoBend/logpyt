"""Behavior of package filtering and package attribution, sync and async.

A package condition keeps the lines that belong to the package. These tests
pin what a user of the public API relies on, so they must survive an internal
rewrite: no private attributes, no mocked internals, only the ``fake_adb``
fixture from ``tests/conftest.py``. Every lifecycle call and join is bounded;
a missing precondition or a hang is reported with ``pytest.fail`` (or a
``TimeoutError``) instead of an assertion.

Sync: the 3.0 ``LogStream`` (ported, design 8.3). It attributes a line by the
uid column of the live wire format (``-v uid``, ``logcat_line``): at start it
looks each watched package up with ``adb shell dumpsys package <package>``
(design 5.8, A.8), which the fixture answers per invocation
(``fake_adb.dumpsys(pkg, run(out(dumpsys_package(pkg, uid))), ...)``), and
intake stays closed until that answer (or ``package_hold``), so no line is
read before the first lookup (F5-2). A line from an app uid it does not know
asks for a re-lookup at once (rate-limited to 0.5 s per package, A.22). A
restarted app keeps its uid (D-8); a reinstall changes it. The 2.x tests
followed pids through ``adb shell pidof``; each sync test keeps the scenario
its 2.x version pinned in those terms, and says where 3.0 changes the
outcome. To know what the stream has read, a sync test watches
``stream.stats``: once every line it logged was read and judged (delivered or
filtered), the stream is stopped, the normal end of a 3.0 run (an adb that
exits by itself is the run's AdbExitError outcome, design 3.2).

Async: still the 2.x ``AsyncLogStream`` until PR-13 (design 8.1), so its tests
keep the 2.x API, pid lookups and their strict xfails. A pidof invocation
starts only after the previous one's answer has been applied (the library
polls one lookup at a time), so a ``touch`` in the SECOND pidof run is the
signal that the FIRST answer is in effect; the fake logcat waits on it before
logging lines that must already resolve. To know what the stream has read, an
async test watches a ``ThreadTimeLogParser`` subclass passed through the 2.x
``parser`` argument. Lines are parsed, filtered and queued in order, so once a
sentinel line is parsed every earlier line has been through the package
filter. The fake logcat stays open (``wait("done")``) until the lines a test
asserts on were parsed; only then does logcat exit 0 and the test join the
stream. Which fake adb children were spawned, and in what order, comes from
the fixture's registry (``fake_adb.children()``).
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import threading
from typing import TYPE_CHECKING

import pytest

from logpyt import (
    AsyncLogStream,
    Filter,
    LogStream,
    StreamConfig,
    ThreadTimeLogParser,
    read_file,
)
from logpyt.filters import Condition, Level, MessageContains, Package, Tag
from tests.conftest import (
    FakeAdb,
    async_wait_until,
    dumpsys_package,
    logcat_line,
    out,
    run,
    threadtime,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from logpyt.models import LogEntry
    from logpyt.parsers import ParseContext
    from logpyt.streams import AsyncStreamHandle
    from tests.conftest import Step

# Upper bound for every wait, join and lifecycle call. A healthy run finishes far
# sooner; the bound only exists so a regression fails the test instead of
# hanging CI.
TIMEOUT = 10.0

# How long a test waits for the first PID lookup, which starts at once or never
# (never under F5-1). Only the branch where it never happens pays it; a wrong
# guess cannot make a test pass, it only shortens the wait.
GRACE = 1.0

# How long the async backlog test waits for the stream to parse logcat's
# startup backlog once it is in the pipe and the first lookup is in flight
# (held). A 2.0.0 stream, whose readers run before the lookup is spawned,
# parses it within milliseconds. A stream that looks the package up before it
# reads (F5-2's planned fix) never does until the answer, and gives a held
# lookup up after its own limit (2.0.0's pidof timeout is 5 s; the 3.0.0
# design's ``package_hold`` is 2 s), so this must stay below both.
READ_GRACE = 1.0

# How long a test waits for the stream to look the package up again after the
# app logged under a pid (async) or a uid (sync) it does not know. A fixed
# stream does that within a fraction of a second (sync: at most 0.5 s after
# the previous lookup of the package, A.22); a 2.0.0 stream waits for the
# backed-off poll (F5-2).
REPOLL_GRACE = 2.0

POLL_INTERVAL = 0.1  # the smallest pid_poll_interval the library allows
# Where PID polling settles in the backed-off tests once a lookup finds nothing
# new: a single unchanged poll multiplies the interval up to this cap.
BACKED_OFF_INTERVAL = 600.0
BACKOFF_FACTOR = BACKED_OFF_INTERVAL / POLL_INTERVAL

PACKAGE = "com.example"
APP_PID = 1234
NEW_PID = 4321  # the same app after a restart
OTHER_PID = 5678  # some other app's process
APP_UID = 10065  # the package's uid (sync)
NEW_UID = 10123  # its uid after a reinstall
OTHER_UID = 10099  # some other app's uid

FILLER = "filler"
BACKLOG = ["backlog 1", "backlog 2", "backlog 3"]

# Every gate a fake adb may be blocked on; teardown opens all of them so no fake
# adb child outlives the test waiting for a signal.
_GATES = (
    "resolved",
    "answer",
    "restart",
    "restarted",
    "live",
    "reinstall",
    "done",
)


def _line(
    message: str, *, pid: int = APP_PID, level: str = "I", tag: str = "App"
) -> Step:
    """A 2.x ``threadtime`` line, without a uid column (the async tests)."""
    return out(threadtime(message, pid=pid, tid=pid, level=level, tag=tag))


def _filler() -> Step:
    """A line from another app; the parser watcher reports when it was parsed."""
    return _line(FILLER, pid=OTHER_PID)


def _pid(pid: int) -> Step:
    return out(f"{pid}\n")


def _uid_line(
    message: str,
    *,
    uid: int = APP_UID,
    pid: int = APP_PID,
    level: str = "I",
    tag: str = "App",
) -> Step:
    """A line of the live wire format, with its uid column (the sync tests)."""
    return out(logcat_line(message, uid=uid, pid=pid, level=level, tag=tag))


def _uid_filler() -> Step:
    """A line from another app's uid, logged last (the sync tests)."""
    return _uid_line(FILLER, uid=OTHER_UID, pid=OTHER_PID)


def _installed(uid: int | None = APP_UID) -> Step:
    """The output of ``dumpsys package PACKAGE`` for an install with ``uid``."""
    return out(dumpsys_package(PACKAGE, uid))


def _resolve_steadily(fake: FakeAdb) -> None:
    """``PACKAGE`` runs as ``APP_PID``; every pidof but the first touches "resolved"."""
    fake.pidof(PACKAGE, run(_pid(APP_PID)), run(touch("resolved"), _pid(APP_PID)))


def _restart_once(fake: FakeAdb) -> None:
    """``PACKAGE`` runs as ``APP_PID``, then as ``NEW_PID`` once "restart" is released.

    The 3rd pidof waits for "restart" and answers ``NEW_PID``; the 4th and later
    touch "restarted", which therefore means ``NEW_PID`` is in effect.
    """
    fake.pidof(
        PACKAGE,
        run(_pid(APP_PID)),
        run(touch("resolved"), _pid(APP_PID)),
        run(wait("restart"), _pid(NEW_PID)),
        run(touch("restarted"), _pid(NEW_PID)),
    )


def _logcat_until_parsed(fake: FakeAdb, *steps: Step) -> None:
    """Logcat runs ``steps``, logs the ``FILLER`` sentinel and stays open until "done".

    Pair it with a ``_ParseWatcher(FILLER)`` and ``_end_logcat_once_parsed_async``.
    """
    fake.logcat(run(*steps, _filler(), wait("done")))


def _signalled(fake: FakeAdb, name: str) -> bool:
    """True when a fake adb has already run ``touch(name)``.

    It reads ``<state>/signals/<name>`` (the format in ``tests/fake_adb.py``)
    instead of calling ``wait_for``, so it never blocks the event loop.
    """
    return (fake.state / "signals" / name).exists()


def _repolled(fake: FakeAdb) -> bool:
    """True once a 3rd pidof was spawned: a lookup after the backed-off one."""
    return len(fake.children("pidof")) >= 3


def _first_spawned(fake: FakeAdb) -> str | None:
    """Whichever of ``"pidof"`` and ``"logcat"`` was spawned first; None if neither."""
    for child in fake.children():
        if child.command in {"pidof", "logcat"}:
            return child.command
    return None


class _ParseWatcher(ThreadTimeLogParser):
    """A threadtime parser that reports when the stream has parsed ``message``.

    Lines are read and filtered in order, so once a later line is parsed every
    earlier line has been through the package filter.
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message
        self.parsed = threading.Event()

    def parse(self, line: str, ctx: ParseContext, /) -> LogEntry | None:
        entry = super().parse(line, ctx)
        if entry is not None and entry.message == self.message:
            self.parsed.set()
        return entry


class _Recorded:
    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    def messages(self) -> list[str]:
        return [entry.message for entry in list(self.entries)]

    def delivered(self) -> list[tuple[str, object]]:
        """``(message, package)`` of every entry received so far."""
        return [(e.message, e.package) for e in list(self.entries)]


class _Collector(_Recorded):
    """A sync ``on_item`` callback that records every entry it receives."""

    def __call__(self, entry: LogEntry) -> None:
        self.entries.append(entry)

    def received(self, message: str) -> bool:
        """Wait up to ``TIMEOUT`` for ``message`` to be delivered."""
        try:
            wait_until(lambda: message in self.messages(), timeout=TIMEOUT)
        except TimeoutError:
            return False
        return True


class _AsyncCollector(_Recorded):
    """An async data callback that records every entry it receives."""

    async def __call__(self, entry: LogEntry, _handle: AsyncStreamHandle) -> None:
        self.entries.append(entry)

    async def received(self, message: str) -> bool:
        """Wait up to ``TIMEOUT`` for ``message`` to be delivered."""
        try:
            await async_wait_until(lambda: message in self.messages(), timeout=TIMEOUT)
        except TimeoutError:
            return False
        return True


# -- bounded calls and preconditions ----------------------------------------------


def _bounded[T](call: Callable[[], T], what: str) -> T:
    """Run a blocking library call on a helper thread, bounded by ``TIMEOUT``.

    The call's own exception is re-raised here. A call that has not returned is
    reported with ``pytest.fail`` and left on its daemon thread; the
    ``fake_adb`` finalizer kills the fake adb children it may be waiting on.
    """
    returned: list[T] = []
    raised: list[BaseException] = []

    def target() -> None:
        try:
            returned.append(call())
        except BaseException as exc:  # noqa: BLE001 (re-raised in the test thread)
            raised.append(exc)

    thread = threading.Thread(target=target, name=f"bounded {what}", daemon=True)
    thread.start()
    thread.join(TIMEOUT)
    if thread.is_alive():
        pytest.fail(f"{what} did not return within {TIMEOUT} s")
    if raised:
        raise raised[0]
    return returned[0]


async def _bounded_async[T](awaitable: Awaitable[T], what: str) -> T:
    """Await a library call, failing the test if it takes longer than ``TIMEOUT``."""
    try:
        return await asyncio.wait_for(awaitable, TIMEOUT)
    except TimeoutError:
        pytest.fail(f"{what} did not return within {TIMEOUT} s")


async def _parsed_within(watcher: _ParseWatcher, timeout: float) -> bool:
    """Async counterpart of ``watcher.parsed.wait(timeout)``."""
    try:
        await async_wait_until(watcher.parsed.is_set, timeout=timeout)
    except TimeoutError:
        return False
    return True


def _require_delivered(collector: _Collector, message: str) -> None:
    """Fail (never xfail) unless ``message`` is delivered within ``TIMEOUT``."""
    if not collector.received(message):
        pytest.fail(f"precondition: {message!r} was not delivered within {TIMEOUT} s")


async def _require_delivered_async(collector: _AsyncCollector, message: str) -> None:
    """Async variant of ``_require_delivered``."""
    if not await collector.received(message):
        pytest.fail(f"precondition: {message!r} was not delivered within {TIMEOUT} s")


def _not_parsed(watcher: _ParseWatcher) -> str:
    return (
        f"precondition: the stream did not parse {watcher.message!r} within {TIMEOUT} s"
    )


# -- stream lifecycle ---------------------------------------------------------------


def _backoff_factor(*, backed_off: bool) -> float:
    """Keep polling every ``POLL_INTERVAL``, or back off to ``BACKED_OFF_INTERVAL``
    after the first poll that finds nothing new.
    """
    return BACKOFF_FACTOR if backed_off else 1.0


def _start_sync(
    where: Condition,
    collector: _Collector,
    *,
    serial: str | None = None,
    package_hold: float = 2.0,
) -> LogStream[LogEntry]:
    """Start a 3.0 ``LogStream`` with ``where`` and ``collector`` as ``on_item``.

    2.x ``filter_by``/``device_id`` are ``where``/``StreamConfig.serial``
    (design 2.2, 2.3); its pid poll settings have no counterpart (D-8). A
    test whose lines wait for a lookup passes a ``package_hold`` that a slow
    machine cannot exceed, so the lookup's timing never decides the outcome.
    """
    config = StreamConfig(serial=serial, package_hold=package_hold)
    stream = LogStream(config, where=where, on_item=collector)
    _bounded(stream.start, "start()")
    return stream


async def _start_async(
    filter_by: Condition,
    collector: _AsyncCollector,
    *,
    parser: ThreadTimeLogParser | None = None,
    backed_off: bool = False,
) -> AsyncLogStream:
    stream = AsyncLogStream(
        parser=parser,
        filter_by=filter_by,
        stdout_callback=collector,
        pid_poll_interval=POLL_INTERVAL,
        pid_poll_backoff_factor=_backoff_factor(backed_off=backed_off),
        pid_max_poll_interval=BACKED_OFF_INTERVAL,
    )
    await _bounded_async(stream.start(), "start()")
    return stream


def _join(stream: LogStream[LogEntry]) -> None:
    """Join the stream (bounded); it must end without an error."""
    _bounded(stream.join, "join()")


async def _join_async(stream: AsyncLogStream) -> None:
    """Async variant of ``_join``."""
    await _bounded_async(stream.join(), "join()")


def _shutdown(fake: FakeAdb, stream: LogStream[LogEntry]) -> None:
    """Open every gate, then stop the stream and join it, both bounded."""
    for gate in _GATES:
        fake.release(gate)
    _bounded(stream.stop, "stop()")
    _join(stream)


async def _shutdown_async(fake: FakeAdb, stream: AsyncLogStream) -> None:
    """Async variant of ``_shutdown``."""
    for gate in _GATES:
        fake.release(gate)
    await _bounded_async(stream.stop(), "stop()")
    await _join_async(stream)


def _all_judged(stream: LogStream[LogEntry], count: int) -> None:
    """Wait until the stream has read ``count`` lines and judged each of them.

    A judged line was delivered to the consumer or filtered out, so nothing
    the test logged is still held for its package; the stop that follows
    changes no verdict. Fails (never xfails) when that takes ``TIMEOUT``.
    """
    try:
        wait_until(
            lambda: (
                (stats := stream.stats).lines == count
                and stats.delivered + stats.filtered == count
            ),
            timeout=TIMEOUT,
        )
    except TimeoutError:
        pytest.fail(
            f"precondition: the stream did not judge {count} lines: {stream.stats}"
        )


async def _end_logcat_once_parsed_async(fake: FakeAdb, watcher: _ParseWatcher) -> None:
    """Let the fake logcat exit 0 once the stream has parsed the sentinel line.

    Every line logged before the sentinel has then been read and filtered, so
    the reader's EOF defect (F1-1: it stops at process exit and drops what is
    still in the pipe) cannot drop one, and the join that follows delivers
    them all.
    """
    if not await _parsed_within(watcher, TIMEOUT):
        pytest.fail(_not_parsed(watcher))
    fake.release("done")


# -- signals the test gives the fake adb ----------------------------------------------


async def _unblock_if_never_looked_up_async(fake: FakeAdb) -> None:
    """Open the "resolved" gate when the stream never looks the package up (F5-1).

    A stream that monitors the package starts its first pidof together with
    logcat and touches "resolved" itself. One that does not (F5-1) never does;
    once logcat was spawned and ``GRACE`` passes with no lookup, the test opens
    the gate so the assertion shows what the stream delivered.
    """
    await async_wait_until(lambda: fake.children("logcat"), timeout=TIMEOUT)
    try:
        await async_wait_until(lambda: fake.children("pidof"), timeout=GRACE)
    except TimeoutError:
        fake.release("resolved")


def _after_restart_is_known(fake: FakeAdb, collector: _Recorded) -> bool:
    """The stream has applied ``NEW_PID``: polled again, or delivered a new-pid line."""
    return _signalled(fake, "restarted") or "after 1" in collector.messages()


async def _unblock_after_restart_async(
    fake: FakeAdb, collector: _AsyncCollector
) -> None:
    """Open the "restarted" gate once the stream knows ``NEW_PID``, or never will.

    A stream that looks the package up again soon after the app logged under
    an unknown pid opens the gate itself (its next poll) or has delivered that
    line. One that waits for the backed-off poll (F5-2) does neither; after
    ``REPOLL_GRACE`` without a new lookup the test opens the gate itself. A
    stream that looked up again but shows neither sign gets the gate opened
    after ``TIMEOUT``, by when that lookup's answer is long in effect.
    """
    try:
        await async_wait_until(lambda: _repolled(fake), timeout=REPOLL_GRACE)
    except TimeoutError:
        fake.release("restarted")
        return
    with contextlib.suppress(TimeoutError):
        await async_wait_until(
            lambda: _after_restart_is_known(fake, collector), timeout=TIMEOUT
        )
    fake.release("restarted")


# -- startup backlog (F5-2) ------------------------------------------------------------


def _backlog_logcat(fake: FakeAdb) -> None:
    """Logcat prints the package's backlog at once, then a live line once resolved.

    Every pidof waits for "answer", so no lookup answers before the test
    releases it; the 2nd and later touch "resolved" (the first answer is in
    effect). Logcat touches "backlog" once the backlog, ``FILLER`` last, is in
    its pipe.
    """
    fake.pidof(
        PACKAGE,
        run(wait("answer"), _pid(APP_PID)),
        run(wait("answer"), touch("resolved"), _pid(APP_PID)),
    )
    fake.logcat(
        run(
            *(_line(message) for message in BACKLOG),
            _filler(),
            touch("backlog"),
            wait("resolved"),
            _line("live"),
            wait("done"),
        )
    )


async def _answer_once_backlog_parsed_async(
    fake: FakeAdb, watcher: _ParseWatcher
) -> bool:
    """Release the held pidof answers once the stream has parsed logcat's backlog.

    Returns True when the stream parsed the backlog while every lookup was
    held, which is what F5-2 is about. A stream that looks the package up
    before it reads cannot do that; it gets the answer, and False is returned,
    at once when it spawned pidof before logcat (2.0.0 always spawns logcat
    first), or ``READ_GRACE`` after the backlog was in the pipe with the lookup
    in flight and still not parsed.
    """
    await async_wait_until(lambda: _first_spawned(fake), timeout=TIMEOUT)
    if _first_spawned(fake) == "pidof":
        fake.release("answer")
        return False
    await fake.async_wait_for("backlog", timeout=TIMEOUT)
    await async_wait_until(lambda: fake.children("pidof"), timeout=TIMEOUT)
    raced = await _parsed_within(watcher, READ_GRACE)
    fake.release("answer")
    return raced


# -- app restart (F5-2) ---------------------------------------------------------------


def _restart_logcat(fake: FakeAdb) -> None:
    """The app logs, restarts under ``NEW_PID`` on "restart"; the sentinel follows."""
    _restart_once(fake)
    _logcat_until_parsed(
        fake,
        wait("resolved"),
        _line("before"),
        wait("restart"),
        _line("after 1", pid=NEW_PID),
        wait("restarted"),
        _line("after 2", pid=NEW_PID),
    )


# D-8 (buffer lines of not-yet-resolved pids) is open for the 2.x async stream:
# a fixed stream may drop or keep "after 1", the first line logged before any
# lookup saw the restart. The sync stream settles it (A.22 D2, see its test).
_RESTART_DELIVERIES = (["before", "after 2"], ["before", "after 1", "after 2"])


# -- device-shell emulation (F5-3) ---------------------------------------------

UNSAFE_PACKAGE = "com.example;reboot"


def _shell_calls(fake: FakeAdb) -> list[list[str]]:
    """The argv of every ``adb ... shell ...`` invocation spawned so far.

    It reads the fixture's child registry, which holds every spawn, including
    one killed before it could record its call.
    """
    return [list(child.args) for child in fake.children() if "shell" in child.args]


def _device_shell_words(args: list[str]) -> list[str]:
    """How the device's ``sh`` splits the command an ``adb shell`` argv sends.

    adb hands the words after ``shell`` to the device's ``sh`` joined with
    spaces and unquoted. OBSERVED on API 24 and API 36
    (``.docs/device-probes-2026-11-04.md``, "Package / PID"): ``adb shell pidof
    'com.example.none; echo INJECTED_VIA_SEMICOLON'`` printed the marker, and
    ``pidof 'system_server zygote64'``, one argv word, returned both pids.
    Shell operators such as ``;`` come back as words of their own.
    """
    command = " ".join(args[args.index("shell") + 1 :])
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _runs_one_plain_command(words: list[str], package: str) -> bool:
    """True when the device would run one command with ``package`` as one word."""
    operator_chars = set("();<>|&")
    has_operator = any(set(word) <= operator_chars for word in words)
    return package in words and not has_operator


# -- sync ------------------------------------------------------------------------


def test_filter_package_delivers_only_that_packages_lines(fake_adb: FakeAdb) -> None:
    """Filter(package=...) delivers exactly the lines whose uid belongs to the package,
    each with its package set to it.

    2.x gated the lines on its second pid lookup; 3.0 reads nothing before the
    first lookup answered (F5-2), so the lines follow at once.
    """
    fake_adb.dumpsys(PACKAGE, run(_installed()))
    fake_adb.logcat(
        run(
            _uid_line("mine 1"),
            _uid_line("theirs", uid=OTHER_UID, pid=OTHER_PID),
            _uid_line("mine 2"),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector)
    try:
        assert collector.received("mine 2")
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("mine 1", PACKAGE), ("mine 2", PACKAGE)]


def test_advanced_filter_package_condition_delivers_that_packages_lines(
    fake_adb: FakeAdb,
) -> None:
    """A bare Package(...) condition delivers exactly the package's lines, each with
    package set, like Filter(package=...) does (F5-1; 2.x wrapped it in the removed
    AdvancedFilter).
    """
    fake_adb.dumpsys(PACKAGE, run(_installed()))
    fake_adb.logcat(
        run(
            _uid_line("mine 1"),
            _uid_line("theirs", uid=OTHER_UID, pid=OTHER_PID),
            _uid_line("mine 2"),
            _uid_filler(),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = _start_sync(Package(PACKAGE), collector)
    try:
        _all_judged(stream, 4)
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("mine 1", PACKAGE), ("mine 2", PACKAGE)]
    assert stream.stats.unattributed == 0


def test_advanced_filter_package_combination_delivers_the_matching_lines(
    fake_adb: FakeAdb,
) -> None:
    """(Package(...) & Level("E")) | MessageContains(...), the README's shape, delivers
    the package's error lines and anyone's matching message, nothing else (F5-1).
    """
    fake_adb.dumpsys(PACKAGE, run(_installed()))
    fake_adb.logcat(
        run(
            _uid_line("mine info", level="I"),
            _uid_line("mine error", level="E"),
            _uid_line("theirs error", uid=OTHER_UID, pid=OTHER_PID, level="E"),
            _uid_line("theirs CRITICAL", uid=OTHER_UID, pid=OTHER_PID, level="I"),
            _uid_filler(),
            wait("done"),
        )
    )
    collector = _Collector()
    condition = (Package(PACKAGE) & Level("E")) | MessageContains("CRITICAL")
    stream = _start_sync(condition, collector)
    try:
        _all_judged(stream, 5)
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("mine error", PACKAGE), ("theirs CRITICAL", None)]


def test_package_under_not_in_an_or_is_looked_up(fake_adb: FakeAdb) -> None:
    """A Package anywhere in the condition is looked up, here under Not inside an Or,
    so the package's lines are attributed and left out (F5-1).
    """
    fake_adb.dumpsys(PACKAGE, run(_installed()))
    fake_adb.logcat(
        run(
            _uid_line("mine"),
            _uid_line("theirs", uid=OTHER_UID, pid=OTHER_PID),
            _uid_filler(),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = _start_sync(Tag("nothing") | ~Package(PACKAGE), collector)
    try:
        _all_judged(stream, 3)
    finally:
        _shutdown(fake_adb, stream)

    assert {child.args[-1] for child in fake_adb.children("dumpsys")} == {PACKAGE}
    assert collector.delivered() == [("theirs", None), (FILLER, None)]


def test_backlog_before_the_first_pid_lookup_is_delivered(fake_adb: FakeAdb) -> None:
    """The package's lines that logcat prints at once on start (its ring-buffer backlog)
    are delivered, attributed, although no lookup had answered when adb printed
    them (F5-2).

    The lookup's answer is held until the backlog is in adb's pipe. 3.0 reads
    nothing before that answer (intake stays closed until the first lookup
    returns or ``package_hold`` expires, design 5.8), so the backlog is
    attributed; a stream that read while it looked up would have judged the
    backlog without its package. The live line follows once the backlog was
    delivered.
    """
    fake_adb.dumpsys(PACKAGE, run(wait("answer"), _installed()))
    fake_adb.logcat(
        run(
            *(_uid_line(message) for message in BACKLOG),
            _uid_filler(),
            touch("backlog"),
            wait("live"),
            _uid_line("live"),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector, package_hold=TIMEOUT)
    read_while_held = -1
    try:
        fake_adb.wait_for("backlog", timeout=TIMEOUT)
        wait_until(lambda: fake_adb.children("dumpsys"), timeout=TIMEOUT)
        read_while_held = stream.stats.lines
        fake_adb.release("answer")
        _require_delivered(collector, BACKLOG[-1])
        fake_adb.release("live")
        _require_delivered(collector, "live")
    finally:
        _shutdown(fake_adb, stream)

    assert read_while_held == 0  # intake stays closed until the first answer
    assert collector.delivered() == [(m, PACKAGE) for m in [*BACKLOG, "live"]]
    assert stream.stats.unattributed == 0


def test_app_launched_after_start_is_picked_up(fake_adb: FakeAdb) -> None:
    """An app that is not installed when the stream starts (the lookup finds nothing)
    has its lines delivered once a later lookup finds it.

    The first line from the app's uid, unknown then, is held and asks for the
    re-lookup that finds it (design 5.8), so no line of it is lost.
    """
    fake_adb.dumpsys(PACKAGE, run(_installed(None)), run(_installed()))
    fake_adb.logcat(run(_uid_line("hello"), wait("done")))
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector, package_hold=TIMEOUT)
    try:
        assert collector.received("hello")
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("hello", PACKAGE)]
    assert len(fake_adb.calls("dumpsys")) >= 2
    assert stream.stats.unattributed == 0


def test_restarted_app_is_followed_to_its_new_pid(fake_adb: FakeAdb) -> None:
    """After the app restarts under a new pid, its new-pid lines are delivered.

    Changed in 3.0: lines are attributed by uid, which a restart keeps (D-8),
    so the new pid's lines are the package's at once and no lookup follows
    the restart.
    """
    fake_adb.dumpsys(PACKAGE, run(_installed()))
    fake_adb.logcat(
        run(
            _uid_line("before"),
            wait("restart"),
            _uid_line("after", pid=NEW_PID),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector)
    try:
        assert collector.received("before")
        fake_adb.release("restart")
        assert collector.received("after")
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("before", PACKAGE), ("after", PACKAGE)]
    assert len(fake_adb.calls("dumpsys")) == 1


def test_restarted_app_is_picked_up_without_waiting_for_a_backed_off_poll(
    fake_adb: FakeAdb,
) -> None:
    """When the app comes back under a uid the stream does not know, its lines are
    delivered within seconds, without waiting for a scheduled lookup (F5-2).

    3.0 has no lookup poll to back off: the first line from an unknown app uid
    asks for a re-lookup at once (at most 0.5 s after the previous one, A.22).
    A restart keeps the uid (D-8), so the 3.0 form of this scenario is a
    reinstall under a new uid while the package is mapped. That first line
    ("after 1") is not held (D-8) and, matching nothing without its package,
    is filtered out; the answer that names its uid counts it in
    ``stats.unattributed`` (A.22 D2, which settles the D-8 question the 2.x
    test left open). The next line ("after 2") is the package's.
    """
    fake_adb.dumpsys(PACKAGE, run(_installed()), run(_installed(NEW_UID)))
    fake_adb.logcat(
        run(
            _uid_line("before"),
            wait("reinstall"),
            _uid_line("after 1", uid=NEW_UID, pid=NEW_PID),
            wait("restarted"),
            _uid_line("after 2", uid=NEW_UID, pid=NEW_PID),
            _uid_filler(),
            wait("done"),
        )
    )
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector)
    try:
        _require_delivered(collector, "before")
        fake_adb.release("reinstall")
        try:
            wait_until(lambda: stream.stats.unattributed == 1, timeout=REPOLL_GRACE)
            picked_up = True
        except TimeoutError:
            picked_up = False
        fake_adb.release("restarted")
        _all_judged(stream, 4)
    finally:
        _shutdown(fake_adb, stream)

    assert picked_up, f"no re-lookup answer within {REPOLL_GRACE} s: {stream.stats}"
    assert collector.delivered() == [("before", PACKAGE), ("after 2", PACKAGE)]
    assert stream.stats.unattributed == 1


def test_pid_lookup_queries_the_selected_device(fake_adb: FakeAdb) -> None:
    """With a serial set, every package lookup goes to that device (adb -s <serial>),
    not to whichever device adb would pick.

    2.x ``device_id`` is ``StreamConfig.serial``, and the pid lookup is the
    uid lookup (design 5.8).
    """
    serial = "emulator-5554"
    fake_adb.dumpsys(PACKAGE, run(touch("looked up"), _installed()))
    fake_adb.logcat(run(wait("done")))
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector, serial=serial)
    try:
        fake_adb.wait_for("looked up", timeout=TIMEOUT)
    finally:
        _shutdown(fake_adb, stream)

    lookups = [child.args for child in fake_adb.children("dumpsys")]
    assert lookups
    assert all(args[:2] == ("-s", serial) for args in lookups), lookups


def test_package_name_with_shell_metacharacters_is_not_run_by_the_device(
    fake_adb: FakeAdb,
) -> None:
    """A package name containing shell metacharacters is either rejected with ValueError
    or handed to the device shell as one quoted word, never as extra commands (F5-3).
    """
    fake_adb.logcat(run(wait("done")))
    collector = _Collector()
    try:
        stream = _start_sync(Filter(package=UNSAFE_PACKAGE), collector)
    except ValueError:
        return  # rejected before anything reached the device
    try:
        wait_until(lambda: _shell_calls(fake_adb), timeout=TIMEOUT)
    finally:
        _shutdown(fake_adb, stream)

    for args in _shell_calls(fake_adb):
        words = _device_shell_words(args)
        assert _runs_one_plain_command(words, UNSAFE_PACKAGE), (
            f"the device shell would split {args} into {words}"
        )


# -- async -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_filter_package_delivers_only_that_packages_lines(
    fake_adb: FakeAdb,
) -> None:
    """AsyncLogStream with Filter(package=...) delivers exactly the lines whose pid
    belongs to the package, each with its package set to it.
    """
    _resolve_steadily(fake_adb)
    fake_adb.logcat(
        run(
            wait("resolved"),
            _line("mine 1"),
            _line("theirs", pid=OTHER_PID),
            _line("mine 2"),
            wait("done"),
        )
    )
    collector = _AsyncCollector()
    stream = await _start_async(Filter(package=PACKAGE), collector)
    try:
        assert await collector.received("mine 2")
    finally:
        await _shutdown_async(fake_adb, stream)

    assert collector.delivered() == [("mine 1", PACKAGE), ("mine 2", PACKAGE)]


@pytest.mark.asyncio
async def test_async_advanced_filter_package_condition_delivers_that_packages_lines(
    fake_adb: FakeAdb,
) -> None:
    """AsyncLogStream with a bare Package(...) condition delivers exactly the
    package's lines, each with its package set, like Filter(package=...) does (F5-1;
    2.x wrapped it in the removed AdvancedFilter).
    """
    _resolve_steadily(fake_adb)
    _logcat_until_parsed(
        fake_adb,
        wait("resolved"),
        _line("mine 1"),
        _line("theirs", pid=OTHER_PID),
        _line("mine 2"),
    )
    watcher = _ParseWatcher(FILLER)
    collector = _AsyncCollector()
    stream = await _start_async(Package(PACKAGE), collector, parser=watcher)
    try:
        await _unblock_if_never_looked_up_async(fake_adb)
        await _end_logcat_once_parsed_async(fake_adb, watcher)
        await _join_async(stream)
    finally:
        await _shutdown_async(fake_adb, stream)

    assert collector.delivered() == [("mine 1", PACKAGE), ("mine 2", PACKAGE)]


@pytest.mark.asyncio
async def test_async_advanced_filter_package_combination_delivers_the_matching_lines(
    fake_adb: FakeAdb,
) -> None:
    """AsyncLogStream with (Package(...) & Level("E")) | MessageContains(...), the
    README's shape, looks the package up and delivers the package's error lines,
    attributed, and anyone's matching message, nothing else (F5-1).
    """
    _resolve_steadily(fake_adb)
    _logcat_until_parsed(
        fake_adb,
        wait("resolved"),
        _line("mine info", level="I"),
        _line("mine error", level="E"),
        _line("theirs error", pid=OTHER_PID, level="E"),
        _line("theirs CRITICAL", pid=OTHER_PID, level="I"),
    )
    watcher = _ParseWatcher(FILLER)
    collector = _AsyncCollector()
    condition = (Package(PACKAGE) & Level("E")) | MessageContains("CRITICAL")
    stream = await _start_async(condition, collector, parser=watcher)
    try:
        await _unblock_if_never_looked_up_async(fake_adb)
        await _end_logcat_once_parsed_async(fake_adb, watcher)
        await _join_async(stream)
    finally:
        await _shutdown_async(fake_adb, stream)

    assert {child.args[-1] for child in fake_adb.children("pidof")} == {PACKAGE}
    assert collector.delivered() == [("mine error", PACKAGE), ("theirs CRITICAL", None)]


@pytest.mark.asyncio
async def test_async_package_under_not_in_an_or_is_looked_up(fake_adb: FakeAdb) -> None:
    """AsyncLogStream looks up a Package anywhere in the condition, here under Not
    inside an Or, so the package's lines are attributed and left out (F5-1).
    """
    _resolve_steadily(fake_adb)
    _logcat_until_parsed(
        fake_adb,
        wait("resolved"),
        _line("mine"),
        _line("theirs", pid=OTHER_PID),
    )
    watcher = _ParseWatcher(FILLER)
    collector = _AsyncCollector()
    condition = Tag("nothing") | ~Package(PACKAGE)
    stream = await _start_async(condition, collector, parser=watcher)
    try:
        await _unblock_if_never_looked_up_async(fake_adb)
        await _end_logcat_once_parsed_async(fake_adb, watcher)
        await _join_async(stream)
    finally:
        await _shutdown_async(fake_adb, stream)

    assert {child.args[-1] for child in fake_adb.children("pidof")} == {PACKAGE}
    assert collector.delivered() == [("theirs", None), (FILLER, None)]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F5-2: lines read before the first PID lookup finishes are dropped "
    "(the startup backlog)",
)
@pytest.mark.asyncio
async def test_async_backlog_before_the_first_pid_lookup_is_delivered(
    fake_adb: FakeAdb,
) -> None:
    """AsyncLogStream delivers the package's startup backlog even though no PID lookup
    has finished when it is read (F5-2).
    """
    _backlog_logcat(fake_adb)
    watcher = _ParseWatcher(FILLER)
    collector = _AsyncCollector()
    stream = await _start_async(Filter(package=PACKAGE), collector, parser=watcher)
    raced = False
    try:
        raced = await _answer_once_backlog_parsed_async(fake_adb, watcher)
        await _require_delivered_async(collector, "live")
    finally:
        await _shutdown_async(fake_adb, stream)

    assert collector.messages() == [*BACKLOG, "live"], (
        f"backlog parsed while every lookup was held: {raced}"
    )


@pytest.mark.asyncio
async def test_async_restarted_app_is_followed_to_its_new_pid(
    fake_adb: FakeAdb,
) -> None:
    """AsyncLogStream follows a restarted app: its new-pid lines are delivered once a
    PID poll has seen the restart.
    """
    _restart_once(fake_adb)
    fake_adb.logcat(
        run(
            wait("resolved"),
            _line("before"),
            wait("restarted"),
            _line("after", pid=NEW_PID),
            wait("done"),
        )
    )
    collector = _AsyncCollector()
    stream = await _start_async(Filter(package=PACKAGE), collector)
    try:
        assert await collector.received("before")
        fake_adb.release("restart")
        assert await collector.received("after")
    finally:
        await _shutdown_async(fake_adb, stream)

    assert collector.delivered() == [("before", PACKAGE), ("after", PACKAGE)]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F5-2: once PID polling has backed off, a restarted app's lines are "
    "dropped until the next scheduled poll",
)
@pytest.mark.asyncio
async def test_async_restarted_app_is_picked_up_without_waiting_for_a_backed_off_poll(
    fake_adb: FakeAdb,
) -> None:
    """AsyncLogStream delivers a restarted app's new-pid lines within seconds after PID
    polling has backed off, not after the backed-off interval (F5-2; the first
    new-pid line is open, D-8).
    """
    _restart_logcat(fake_adb)
    watcher = _ParseWatcher(FILLER)
    collector = _AsyncCollector()
    stream = await _start_async(
        Filter(package=PACKAGE), collector, parser=watcher, backed_off=True
    )
    try:
        await _require_delivered_async(collector, "before")
        fake_adb.release("restart")
        await _unblock_after_restart_async(fake_adb, collector)
        await _end_logcat_once_parsed_async(fake_adb, watcher)
        await _join_async(stream)
    finally:
        await _shutdown_async(fake_adb, stream)

    assert collector.messages() in _RESTART_DELIVERIES


@pytest.mark.asyncio
async def test_async_package_name_with_shell_metacharacters_is_not_run_by_the_device(
    fake_adb: FakeAdb,
) -> None:
    """AsyncLogStream rejects a package name containing shell metacharacters with
    ValueError or hands it to the device shell as one quoted word (F5-3).
    """
    fake_adb.logcat(run(wait("done")))
    collector = _AsyncCollector()
    try:
        stream = await _start_async(Filter(package=UNSAFE_PACKAGE), collector)
    except ValueError:
        return  # rejected before anything reached the device
    try:
        await async_wait_until(lambda: _shell_calls(fake_adb), timeout=TIMEOUT)
    finally:
        await _shutdown_async(fake_adb, stream)

    for args in _shell_calls(fake_adb):
        words = _device_shell_words(args)
        assert _runs_one_plain_command(words, UNSAFE_PACKAGE), (
            f"the device shell would split {args} into {words}"
        )


# -- offline (files) -------------------------------------------------------------


def _write_log(path: Path) -> Path:
    path.write_bytes(
        (threadtime("mine 1", pid=APP_PID) + threadtime("mine 2", pid=APP_PID)).encode()
    )
    return path


@pytest.mark.parametrize(
    "where",
    [Filter(package=PACKAGE), Package(PACKAGE)],
    ids=["filter", "package"],
)
def test_read_file_package_condition_needs_a_uid_map(
    tmp_path: Path, where: Condition
) -> None:
    """read_file with a package condition and no uid map raises ValueError: a log
    file carries no package attribution, so the condition could match nothing
    (F5-4). The error comes before the file is read.
    """
    path = _write_log(tmp_path / "app.log")

    assert [e.message for e in read_file(path)] == ["mine 1", "mine 2"]
    with pytest.raises(ValueError, match="uid_packages"):
        read_file(path, where=where)


def test_read_file_package_condition_with_uid_map_yields_its_lines(
    tmp_path: Path,
) -> None:
    """With uid_packages, a package condition keeps the lines whose -v uid column
    the map names for that package.
    """
    path = tmp_path / "uid.log"
    path.write_text(
        "2026-11-04 05:11:16.661446 +0000 10065  1234  1234 I App: mine\n"
        "2026-11-04 05:11:16.661447 +0000 10066  5678  5678 I App: theirs\n"
        "2026-11-04 05:11:16.661448 +0000 10065  4321  4321 I App: mine again\n",
        encoding="utf-8",
    )

    entries = list(
        read_file(path, where=Filter(package=PACKAGE), uid_packages={10065: PACKAGE})
    )

    assert [(e.message, e.package) for e in entries] == [
        ("mine", PACKAGE),
        ("mine again", PACKAGE),
    ]
