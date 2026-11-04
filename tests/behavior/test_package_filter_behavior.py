"""Behavior of package filtering and PID resolution, sync and async.

A package filter keeps the lines whose pid belongs to the package. The stream
learns that mapping from ``adb shell pidof <package>``, which the ``fake_adb``
fixture answers per invocation (``fake_adb.pidof(pkg, run(...), ...)``).

These tests pin what a user of the public API relies on, so they must survive
an internal rewrite: no private attributes, no mocked internals, only the
``fake_adb`` fixture from ``tests/conftest.py``. Known 2.0.x bugs are written
for the correct behavior and marked ``xfail(strict=True, raises=AssertionError)``
with their id from ``.docs/review-findings-2.0.1.md``. A missing precondition
or a hang is reported with ``pytest.fail`` (or a ``TimeoutError``) instead of an
assertion, so it fails the test rather than passing for the known bug.

Synchronization. A pidof invocation starts only after the previous one's
answer has been applied (the library polls one lookup at a time), so a
``touch`` in the SECOND pidof run is the signal that the FIRST answer is in
effect; the fake logcat waits on it before logging lines that must already
resolve. To know what the stream has read, a test watches a
``ThreadTimeLogParser`` subclass passed through the public ``parser`` argument
(the ``LogParser`` protocol's ``parse``). Lines are parsed,
filtered and queued in order, so once a sentinel line is parsed every earlier
line has been through the package filter. That sentinel gates the next step:
a held pidof answer is released only after the stream parsed the startup
backlog, and the fake logcat stays open (``wait("done")``) until the lines a
test asserts on were parsed, so the sync reader's EOF defect (F1-1) cannot
drop them. Only then does logcat exit 0 and the test join the stream, which
delivers everything it read. Which fake adb children were spawned, and in
what order, comes from the fixture's registry (``fake_adb.children()``).
Every lifecycle call and join is bounded.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import threading
from typing import TYPE_CHECKING

import pytest

from logpyt import (
    AdvancedFilter,
    AsyncLogStream,
    Filter,
    LogFileReader,
    LogStream,
    ThreadTimeLogParser,
    read_file,
)
from logpyt.filters import Level, MessageContains, Package
from tests.conftest import (
    FakeAdb,
    async_wait_until,
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
    from logpyt.streams import AsyncStreamHandle, StreamHandle
    from tests.conftest import Step

# Upper bound for every wait, join and lifecycle call. A healthy run finishes far
# sooner; the bound only exists so a regression fails the test instead of
# hanging CI.
TIMEOUT = 10.0

# How long a test waits for the first PID lookup, which starts at once or never
# (never under F5-1). Only the branch where it never happens pays it; a wrong
# guess cannot make a test pass, it only shortens the wait.
GRACE = 1.0

# How long the backlog tests wait for the stream to parse logcat's startup
# backlog once it is in the pipe and the first lookup is in flight (held). A
# 2.0.0 stream, whose readers run before the lookup is spawned, parses it
# within milliseconds. A stream that looks the package up before it reads
# (F5-2's planned fix) never does until the answer, and gives a held lookup up
# after its own limit (2.0.0's pidof timeout is 5 s; the 3.0.0 design's
# ``package_hold`` is 2 s), so this must stay below both.
READ_GRACE = 1.0

# How long a test waits for the stream to look the package up again after the
# app logged under a pid it does not know. A fixed stream does that within a
# fraction of a second; a 2.0.0 stream waits for the backed-off poll (F5-2).
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

FILLER = "filler"
BACKLOG = ["backlog 1", "backlog 2", "backlog 3"]

# Every gate a fake adb may be blocked on; teardown opens all of them so no fake
# adb child outlives the test waiting for a signal.
_GATES = ("resolved", "answer", "restart", "restarted", "launched", "done")


def _line(
    message: str, *, pid: int = APP_PID, level: str = "I", tag: str = "App"
) -> Step:
    return out(threadtime(message, pid=pid, tid=pid, level=level, tag=tag))


def _filler() -> Step:
    """A line from another app; the parser watcher reports when it was parsed."""
    return _line(FILLER, pid=OTHER_PID)


def _pid(pid: int) -> Step:
    return out(f"{pid}\n")


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

    Pair it with a ``_ParseWatcher(FILLER)`` and ``_end_logcat_once_parsed``.
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
    """A sync data callback that records every entry it receives."""

    def __call__(self, entry: LogEntry, _handle: StreamHandle) -> None:
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
    filter_by: Filter | AdvancedFilter,
    collector: _Collector,
    *,
    parser: ThreadTimeLogParser | None = None,
    device_id: str | None = None,
    backed_off: bool = False,
) -> LogStream:
    stream = LogStream(
        device_id=device_id,
        parser=parser,
        # X-7: filter_by is annotated ``Filter | None`` although the README passes
        # an AdvancedFilter, which works at runtime.
        filter_by=filter_by,  # ty: ignore[invalid-argument-type]
        stdout_callback=collector,
        pid_poll_interval=POLL_INTERVAL,
        pid_poll_backoff_factor=_backoff_factor(backed_off=backed_off),
        pid_max_poll_interval=BACKED_OFF_INTERVAL,
    )
    _bounded(stream.start, "start()")
    return stream


async def _start_async(
    filter_by: Filter | AdvancedFilter,
    collector: _AsyncCollector,
    *,
    parser: ThreadTimeLogParser | None = None,
    backed_off: bool = False,
) -> AsyncLogStream:
    stream = AsyncLogStream(
        parser=parser,
        # X-7: see _start_sync.
        filter_by=filter_by,  # ty: ignore[invalid-argument-type]
        stdout_callback=collector,
        pid_poll_interval=POLL_INTERVAL,
        pid_poll_backoff_factor=_backoff_factor(backed_off=backed_off),
        pid_max_poll_interval=BACKED_OFF_INTERVAL,
    )
    await _bounded_async(stream.start(), "start()")
    return stream


def _join(stream: LogStream) -> None:
    """Join the stream (bounded); it must end without an error."""
    _bounded(stream.join, "join()")


async def _join_async(stream: AsyncLogStream) -> None:
    """Async variant of ``_join``."""
    await _bounded_async(stream.join(), "join()")


def _shutdown(fake: FakeAdb, stream: LogStream) -> None:
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


def _end_logcat_once_parsed(fake: FakeAdb, watcher: _ParseWatcher) -> None:
    """Let the fake logcat exit 0 once the stream has parsed the sentinel line.

    Every line logged before the sentinel has then been read and filtered, so
    the sync reader's EOF defect (F1-1: it stops at process exit and drops what
    is still in the pipe) cannot drop one, and the join that follows delivers
    them all.
    """
    if not watcher.parsed.wait(TIMEOUT):
        pytest.fail(_not_parsed(watcher))
    fake.release("done")


async def _end_logcat_once_parsed_async(fake: FakeAdb, watcher: _ParseWatcher) -> None:
    """Async variant of ``_end_logcat_once_parsed``."""
    if not await _parsed_within(watcher, TIMEOUT):
        pytest.fail(_not_parsed(watcher))
    fake.release("done")


# -- signals the test gives the fake adb ----------------------------------------------


def _unblock_if_never_looked_up(fake: FakeAdb) -> None:
    """Open the "resolved" gate when the stream never looks the package up (F5-1).

    A stream that monitors the package starts its first pidof together with
    logcat and touches "resolved" itself. One that does not (F5-1) never does;
    once logcat was spawned and ``GRACE`` passes with no lookup, the test opens
    the gate so the assertion shows what the stream delivered.
    """
    wait_until(lambda: fake.children("logcat"), timeout=TIMEOUT)
    try:
        wait_until(lambda: fake.children("pidof"), timeout=GRACE)
    except TimeoutError:
        fake.release("resolved")


async def _unblock_if_never_looked_up_async(fake: FakeAdb) -> None:
    """Async variant of ``_unblock_if_never_looked_up``."""
    await async_wait_until(lambda: fake.children("logcat"), timeout=TIMEOUT)
    try:
        await async_wait_until(lambda: fake.children("pidof"), timeout=GRACE)
    except TimeoutError:
        fake.release("resolved")


def _after_restart_is_known(fake: FakeAdb, collector: _Recorded) -> bool:
    """The stream has applied ``NEW_PID``: polled again, or delivered a new-pid line."""
    return _signalled(fake, "restarted") or "after 1" in collector.messages()


def _unblock_after_restart(fake: FakeAdb, collector: _Collector) -> None:
    """Open the "restarted" gate once the stream knows ``NEW_PID``, or never will.

    A stream that looks the package up again soon after the app logged under
    an unknown pid opens the gate itself (its next poll) or has delivered that
    line. One that waits for the backed-off poll (F5-2) does neither; after
    ``REPOLL_GRACE`` without a new lookup the test opens the gate itself. A
    stream that looked up again but shows neither sign gets the gate opened
    after ``TIMEOUT``, by when that lookup's answer is long in effect.
    """
    try:
        wait_until(lambda: _repolled(fake), timeout=REPOLL_GRACE)
    except TimeoutError:
        fake.release("restarted")
        return
    with contextlib.suppress(TimeoutError):
        wait_until(lambda: _after_restart_is_known(fake, collector), timeout=TIMEOUT)
    fake.release("restarted")


async def _unblock_after_restart_async(
    fake: FakeAdb, collector: _AsyncCollector
) -> None:
    """Async variant of ``_unblock_after_restart``."""
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


def _answer_once_backlog_parsed(fake: FakeAdb, watcher: _ParseWatcher) -> bool:
    """Release the held pidof answers once the stream has parsed logcat's backlog.

    Returns True when the stream parsed the backlog while every lookup was
    held, which is what F5-2 is about. A stream that looks the package up
    before it reads cannot do that; it gets the answer, and False is returned,
    at once when it spawned pidof before logcat (2.0.0 always spawns logcat
    first), or ``READ_GRACE`` after the backlog was in the pipe with the lookup
    in flight and still not parsed.
    """
    wait_until(lambda: _first_spawned(fake), timeout=TIMEOUT)
    if _first_spawned(fake) == "pidof":
        fake.release("answer")
        return False
    fake.wait_for("backlog", timeout=TIMEOUT)
    wait_until(lambda: fake.children("pidof"), timeout=TIMEOUT)
    raced = watcher.parsed.wait(READ_GRACE)
    fake.release("answer")
    return raced


async def _answer_once_backlog_parsed_async(
    fake: FakeAdb, watcher: _ParseWatcher
) -> bool:
    """Async variant of ``_answer_once_backlog_parsed``."""
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


# D-8 (buffer lines of not-yet-resolved pids) is open: a fixed stream may drop
# or keep "after 1", the first line logged before any lookup saw the restart.
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
    """Filter(package=...) delivers exactly the lines whose pid belongs to the package,
    each with its package set to it.
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
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector)
    try:
        assert collector.received("mine 2")
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("mine 1", PACKAGE), ("mine 2", PACKAGE)]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F5-1: PID lookup starts only for Filter; Package() in an "
    "AdvancedFilter never matches a live line",
)
def test_advanced_filter_package_condition_delivers_that_packages_lines(
    fake_adb: FakeAdb,
) -> None:
    """AdvancedFilter(Package(...)) delivers exactly the package's lines, each with
    package set, like Filter(package=...) does (F5-1).
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
    collector = _Collector()
    stream = _start_sync(AdvancedFilter(Package(PACKAGE)), collector, parser=watcher)
    try:
        _unblock_if_never_looked_up(fake_adb)
        _end_logcat_once_parsed(fake_adb, watcher)
        _join(stream)
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("mine 1", PACKAGE), ("mine 2", PACKAGE)]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F5-1: PID lookup starts only for Filter; the Package() branch of "
    "an AdvancedFilter never matches a live line",
)
def test_advanced_filter_package_combination_delivers_the_matching_lines(
    fake_adb: FakeAdb,
) -> None:
    """(Package(...) & Level("E")) | MessageContains(...), the README's shape, delivers
    the package's error lines and anyone's matching message, nothing else (F5-1).
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
    collector = _Collector()
    condition = (Package(PACKAGE) & Level("E")) | MessageContains("CRITICAL")
    stream = _start_sync(AdvancedFilter(condition), collector, parser=watcher)
    try:
        _unblock_if_never_looked_up(fake_adb)
        _end_logcat_once_parsed(fake_adb, watcher)
        _join(stream)
    finally:
        _shutdown(fake_adb, stream)

    assert collector.messages() == ["mine error", "theirs CRITICAL"]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F5-2: lines read before the first PID lookup finishes are dropped "
    "(the startup backlog)",
)
def test_backlog_before_the_first_pid_lookup_is_delivered(fake_adb: FakeAdb) -> None:
    """The package's lines that logcat prints at once on start (its ring-buffer backlog)
    are delivered even though no PID lookup has finished yet (F5-2).

    Every pidof answer is held until the stream has parsed the backlog, so a
    stream that reads while it looks up always reads the backlog first; one
    that looks up before reading gets the answer (``_answer_once_backlog_parsed``).
    """
    _backlog_logcat(fake_adb)
    watcher = _ParseWatcher(FILLER)
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector, parser=watcher)
    raced = False
    try:
        raced = _answer_once_backlog_parsed(fake_adb, watcher)
        _require_delivered(collector, "live")
    finally:
        _shutdown(fake_adb, stream)

    assert collector.messages() == [*BACKLOG, "live"], (
        f"backlog parsed while every lookup was held: {raced}"
    )


def test_app_launched_after_start_is_picked_up(fake_adb: FakeAdb) -> None:
    """An app that is not running when the stream starts (pidof finds nothing) has its
    lines delivered once a later PID poll finds it.
    """
    fake_adb.pidof(
        PACKAGE,
        run(exit_code=1),  # pidof's answer while no such process exists
        run(_pid(APP_PID)),
        run(touch("launched"), _pid(APP_PID)),
    )
    fake_adb.logcat(run(wait("launched"), _line("hello"), wait("done")))
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector)
    try:
        assert collector.received("hello")
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("hello", PACKAGE)]


def test_restarted_app_is_followed_to_its_new_pid(fake_adb: FakeAdb) -> None:
    """After the app restarts under a new pid, its new-pid lines are delivered once a
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
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector)
    try:
        assert collector.received("before")
        fake_adb.release("restart")
        assert collector.received("after")
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("before", PACKAGE), ("after", PACKAGE)]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F5-2: once PID polling has backed off, a restarted app's lines are "
    "dropped until the next scheduled poll",
)
def test_restarted_app_is_picked_up_without_waiting_for_a_backed_off_poll(
    fake_adb: FakeAdb,
) -> None:
    """When the app restarts after PID polling has backed off, its new-pid lines are
    delivered within seconds, not after the backed-off poll interval (F5-2).

    Whether the first new-pid line, logged before any lookup saw the restart, is
    kept is open (D-8); only a later one is required.
    """
    _restart_logcat(fake_adb)
    watcher = _ParseWatcher(FILLER)
    collector = _Collector()
    stream = _start_sync(
        Filter(package=PACKAGE), collector, parser=watcher, backed_off=True
    )
    try:
        # "before" follows the 2nd lookup, which found nothing new: from here
        # the next scheduled poll is BACKED_OFF_INTERVAL away.
        _require_delivered(collector, "before")
        fake_adb.release("restart")
        _unblock_after_restart(fake_adb, collector)
        _end_logcat_once_parsed(fake_adb, watcher)
        _join(stream)
    finally:
        _shutdown(fake_adb, stream)

    assert collector.messages() in _RESTART_DELIVERIES


def test_pid_lookup_queries_the_selected_device(fake_adb: FakeAdb) -> None:
    """With device_id set, every PID lookup goes to that device (adb -s <serial>), not
    to whichever device adb would pick.
    """
    serial = "emulator-5554"
    fake_adb.pidof(PACKAGE, run(touch("resolved"), _pid(APP_PID)))
    fake_adb.logcat(run(wait("done")))
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector, device_id=serial)
    try:
        fake_adb.wait_for("resolved", timeout=TIMEOUT)
    finally:
        _shutdown(fake_adb, stream)

    lookups = [child.args for child in fake_adb.children("pidof")]
    assert lookups
    assert all(args[:2] == ("-s", serial) for args in lookups), lookups


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F5-3: the package name reaches the device shell unquoted in "
    "`adb shell pidof <package>`",
)
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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F5-1: PID lookup starts only for Filter; Package() in an "
    "AdvancedFilter never matches a live line",
)
@pytest.mark.asyncio
async def test_async_advanced_filter_package_condition_delivers_that_packages_lines(
    fake_adb: FakeAdb,
) -> None:
    """AsyncLogStream with AdvancedFilter(Package(...)) delivers exactly the package's
    lines, each with its package set, like Filter(package=...) does (F5-1).
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
    stream = await _start_async(
        AdvancedFilter(Package(PACKAGE)), collector, parser=watcher
    )
    try:
        await _unblock_if_never_looked_up_async(fake_adb)
        await _end_logcat_once_parsed_async(fake_adb, watcher)
        await _join_async(stream)
    finally:
        await _shutdown_async(fake_adb, stream)

    assert collector.delivered() == [("mine 1", PACKAGE), ("mine 2", PACKAGE)]


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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="F5-3: the package name reaches the device shell unquoted in "
    "`adb shell pidof <package>`",
)
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


def test_file_reader_package_filter_yields_nothing(tmp_path: Path) -> None:
    """LogFileReader with Filter(package=...) yields no entries: a log file carries no
    pid-to-package mapping, so no line can match (pinned; F5-4 is a docs item).
    """
    path = _write_log(tmp_path / "app.log")

    assert [e.message for e in LogFileReader(path)] == ["mine 1", "mine 2"]
    assert list(LogFileReader(path, filter_by=Filter(package=PACKAGE))) == []


def test_read_file_package_condition_yields_nothing(tmp_path: Path) -> None:
    """read_file with AdvancedFilter(Package(...)) yields no entries for the same reason
    as Filter(package=...) (pinned; F5-4 is a docs item).
    """
    path = _write_log(tmp_path / "app.log")
    package_filter = AdvancedFilter(Package(PACKAGE))

    assert [e.message for e in read_file(path)] == ["mine 1", "mine 2"]
    # X-7: filter_by is annotated ``Filter | None``; AdvancedFilter works at runtime.
    assert list(read_file(path, filter_by=package_filter)) == []  # ty: ignore[invalid-argument-type]
