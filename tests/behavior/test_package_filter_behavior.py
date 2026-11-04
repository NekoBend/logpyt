"""Behavior of package filtering and PID resolution, sync and async.

A package filter keeps the lines whose pid belongs to the package. The stream
learns that mapping from ``adb shell pidof <package>``, which the ``fake_adb``
fixture answers per invocation (``fake_adb.pidof(pkg, run(...), ...)``).

These tests pin what a user of the public API relies on, so they must survive
an internal rewrite: no private attributes, no mocked internals, only the
``fake_adb`` fixture from ``tests/conftest.py``. Known 2.0.x bugs are written
for the correct behavior and marked ``xfail(strict=True)`` with their id from
``.docs/review-findings-2.0.1.md``.

Synchronization. A pidof invocation starts only after the previous one's
answer has been applied (the library polls one lookup at a time), so a
``touch`` in the SECOND pidof run is the signal that the FIRST answer is in
effect; the fake logcat waits on it before logging lines that must already
resolve. Where a test must hold a pidof answer until the stream has read some
lines, it watches a ``ThreadTimeLogParser`` subclass passed through the public
``parser`` argument (``LogParser`` documents overriding ``parse_stdout``).
Where a test must show that nothing more is delivered, the fake logcat exits 0
and the test joins the stream, which delivers everything it read.
"""

from __future__ import annotations

import contextlib
import shlex
import threading
from typing import TYPE_CHECKING, Any

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
    from pathlib import Path

    from logpyt.models import LogEntry
    from logpyt.streams import AsyncStreamHandle, StreamHandle
    from tests.conftest import Step

# Upper bound for every wait and join. A healthy run finishes far sooner; the
# bound only exists so a regression fails the test instead of hanging CI.
TIMEOUT = 10.0

# How long a test waits for something that happens at once or never: the first
# PID lookup (never under F5-1), or the stream reading lines before any lookup
# has answered (never once F5-2 is fixed). Only the branch where it never
# happens pays it; a wrong guess cannot make a test pass, it only shortens the
# wait.
GRACE = 1.0

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


def _signalled(fake: FakeAdb, name: str) -> bool:
    """True when a fake adb has already run ``touch(name)``.

    It reads ``<state>/signals/<name>`` (the format in ``tests/fake_adb.py``)
    instead of calling ``wait_for``, so it never blocks the event loop.
    """
    return (fake.state / "signals" / name).exists()


def _calls(fake: FakeAdb, command: str | None = None) -> list[dict[str, Any]]:
    """``fake.calls(command)`` for polling while fake adb children may be running.

    A child appends its record while the test may be reading the file; a record
    caught half-written reads as none yet, and the next poll sees it whole.
    """
    try:
        return fake.calls(command)
    except ValueError:  # json.JSONDecodeError
        return []


def _repolled(fake: FakeAdb) -> bool:
    """True once a 3rd pidof has started: a lookup after the backed-off one."""
    return len(_calls(fake, "pidof")) >= 3


class _ParseWatcher(ThreadTimeLogParser):
    """A threadtime parser that reports when the stream has parsed ``message``.

    Lines are read and filtered in order, so once a later line is parsed every
    earlier line has been through the package filter.
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message
        self.parsed = threading.Event()

    def parse_stdout(self, line: str) -> LogEntry:
        entry = super().parse_stdout(line)
        if entry.message == self.message:
            self.parsed.set()
        return entry


class _Recorded:
    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    def messages(self) -> list[str]:
        return [entry.message for entry in list(self.entries)]

    def delivered(self) -> list[tuple[str, object]]:
        """``(message, meta["package"])`` of every entry received so far."""
        return [(e.message, e.meta.get("package")) for e in list(self.entries)]


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
    stream.start()
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
    await stream.start()
    return stream


def _shutdown(fake: FakeAdb, stream: LogStream) -> None:
    for gate in _GATES:
        fake.release(gate)
    stream.stop()
    stream.join(timeout=TIMEOUT)


async def _shutdown_async(fake: FakeAdb, stream: AsyncLogStream) -> None:
    for gate in _GATES:
        fake.release(gate)
    await stream.stop()
    await stream.join(timeout=TIMEOUT)


def _unblock_if_never_looked_up(fake: FakeAdb) -> None:
    """Open the "resolved" gate when the stream never looks the package up (F5-1).

    A stream that monitors the package starts its first pidof together with
    logcat and touches "resolved" itself. One that does not (F5-1) never does;
    once logcat runs and ``GRACE`` passes with no lookup, the test opens the
    gate so the assertion shows what the stream delivered.
    """
    wait_until(lambda: _calls(fake, "logcat"), timeout=TIMEOUT)
    try:
        wait_until(lambda: _calls(fake, "pidof"), timeout=GRACE)
    except TimeoutError:
        fake.release("resolved")


async def _unblock_if_never_looked_up_async(fake: FakeAdb) -> None:
    """Async variant of ``_unblock_if_never_looked_up``."""
    await async_wait_until(lambda: _calls(fake, "logcat"), timeout=TIMEOUT)
    try:
        await async_wait_until(lambda: _calls(fake, "pidof"), timeout=GRACE)
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


async def _wait_parsed_async(watcher: _ParseWatcher) -> None:
    """Async counterpart of ``watcher.parsed.wait(GRACE)``."""
    with contextlib.suppress(TimeoutError):
        await async_wait_until(watcher.parsed.is_set, timeout=GRACE)


def _backlog_logcat(fake: FakeAdb) -> None:
    """Logcat prints the package's backlog at once, then a live line once resolved."""
    fake.pidof(
        PACKAGE,
        run(wait("answer"), _pid(APP_PID)),
        run(touch("resolved"), _pid(APP_PID)),
    )
    fake.logcat(
        run(
            _line("backlog 1"),
            _line("backlog 2"),
            _line("backlog 3"),
            _filler(),
            wait("resolved"),
            _line("live"),
            wait("done"),
        )
    )


def _restart_logcat(fake: FakeAdb) -> None:
    """The app logs, restarts under ``NEW_PID`` on "restart", and logcat then ends."""
    _restart_once(fake)
    fake.logcat(
        run(
            wait("resolved"),
            _line("before"),
            wait("restart"),
            _line("after 1", pid=NEW_PID),
            wait("restarted"),
            _line("after 2", pid=NEW_PID),
        )
    )


# D-8 (buffer lines of not-yet-resolved pids) is open: a fixed stream may drop
# or keep "after 1", the first line logged before any lookup saw the restart.
_RESTART_DELIVERIES = (["before", "after 2"], ["before", "after 1", "after 2"])


# -- device-shell emulation (F5-3) ---------------------------------------------

UNSAFE_PACKAGE = "com.example;reboot"


def _shell_calls(fake: FakeAdb, *, polling: bool = False) -> list[list[str]]:
    """The argv of every recorded ``adb ... shell ...`` invocation.

    ``polling=True`` tolerates a record caught half-written (see ``_calls``). The
    final read after shutdown does not, so a torn read cannot hide a call.
    """
    calls = _calls(fake) if polling else fake.calls()
    return [call["args"] for call in calls if "shell" in call["args"]]


def _device_shell_words(args: list[str]) -> list[str]:
    """How the device's ``sh`` splits the command an ``adb shell`` argv sends.

    adb joins the words after ``shell`` with spaces and does not quote them
    (recalled from adb's source, not checked against a device: the plan lists it
    as uncertain, and F5-3 hardens it either way). Shell operators such as
    ``;`` come back as words of their own.
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
    each with meta["package"] set to it.
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
    reason="F5-1: PID lookup starts only for Filter; Package() in an "
    "AdvancedFilter never matches a live line",
)
def test_advanced_filter_package_condition_delivers_that_packages_lines(
    fake_adb: FakeAdb,
) -> None:
    """AdvancedFilter(Package(...)) delivers exactly the package's lines, each with
    meta["package"] set, like Filter(package=...) does (F5-1).
    """
    _resolve_steadily(fake_adb)
    fake_adb.logcat(
        run(
            wait("resolved"),
            _line("mine 1"),
            _line("theirs", pid=OTHER_PID),
            _line("mine 2"),
        )
    )
    collector = _Collector()
    stream = _start_sync(AdvancedFilter(Package(PACKAGE)), collector)
    try:
        _unblock_if_never_looked_up(fake_adb)
        stream.join(timeout=TIMEOUT)  # logcat exits after its last line
    finally:
        _shutdown(fake_adb, stream)

    assert collector.delivered() == [("mine 1", PACKAGE), ("mine 2", PACKAGE)]


@pytest.mark.xfail(
    strict=True,
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
    fake_adb.logcat(
        run(
            wait("resolved"),
            _line("mine info", level="I"),
            _line("mine error", level="E"),
            _line("theirs error", pid=OTHER_PID, level="E"),
            _line("theirs CRITICAL", pid=OTHER_PID, level="I"),
        )
    )
    collector = _Collector()
    condition = (Package(PACKAGE) & Level("E")) | MessageContains("CRITICAL")
    stream = _start_sync(AdvancedFilter(condition), collector)
    try:
        _unblock_if_never_looked_up(fake_adb)
        stream.join(timeout=TIMEOUT)  # logcat exits after its last line
    finally:
        _shutdown(fake_adb, stream)

    assert collector.messages() == ["mine error", "theirs CRITICAL"]


@pytest.mark.xfail(
    strict=True,
    reason="F5-2: lines read before the first PID lookup finishes are dropped "
    "(the startup backlog)",
)
def test_backlog_before_the_first_pid_lookup_is_delivered(fake_adb: FakeAdb) -> None:
    """The package's lines that logcat prints at once on start (its ring-buffer backlog)
    are delivered even though no PID lookup has finished yet (F5-2).

    The first pidof answer is held until the stream has parsed the backlog, or
    for ``GRACE`` when a stream resolves before it reads.
    """
    _backlog_logcat(fake_adb)
    watcher = _ParseWatcher(FILLER)
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector, parser=watcher)
    try:
        watcher.parsed.wait(GRACE)
        fake_adb.release("answer")
        assert collector.received("live")
    finally:
        _shutdown(fake_adb, stream)

    assert collector.messages() == ["backlog 1", "backlog 2", "backlog 3", "live"]


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
    collector = _Collector()
    stream = _start_sync(Filter(package=PACKAGE), collector, backed_off=True)
    try:
        # "before" follows the 2nd lookup, which found nothing new: from here
        # the next scheduled poll is BACKED_OFF_INTERVAL away.
        assert collector.received("before")
        fake_adb.release("restart")
        _unblock_after_restart(fake_adb, collector)
        stream.join(timeout=TIMEOUT)  # logcat exits after "after 2"
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

    lookups = [call["args"] for call in fake_adb.calls("pidof")]
    assert lookups
    assert all(args[:2] == ["-s", serial] for args in lookups), lookups


@pytest.mark.xfail(
    strict=True,
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
        wait_until(lambda: _shell_calls(fake_adb, polling=True), timeout=TIMEOUT)
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
    belongs to the package, each with meta["package"] set to it.
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
    reason="F5-1: PID lookup starts only for Filter; Package() in an "
    "AdvancedFilter never matches a live line",
)
@pytest.mark.asyncio
async def test_async_advanced_filter_package_condition_delivers_that_packages_lines(
    fake_adb: FakeAdb,
) -> None:
    """AsyncLogStream with AdvancedFilter(Package(...)) delivers exactly the package's
    lines, each with meta["package"] set, like Filter(package=...) does (F5-1).
    """
    _resolve_steadily(fake_adb)
    fake_adb.logcat(
        run(
            wait("resolved"),
            _line("mine 1"),
            _line("theirs", pid=OTHER_PID),
            _line("mine 2"),
        )
    )
    collector = _AsyncCollector()
    stream = await _start_async(AdvancedFilter(Package(PACKAGE)), collector)
    try:
        await _unblock_if_never_looked_up_async(fake_adb)
        await stream.join(timeout=TIMEOUT)  # logcat exits after its last line
    finally:
        await _shutdown_async(fake_adb, stream)

    assert collector.delivered() == [("mine 1", PACKAGE), ("mine 2", PACKAGE)]


@pytest.mark.xfail(
    strict=True,
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
    try:
        await _wait_parsed_async(watcher)
        fake_adb.release("answer")
        assert await collector.received("live")
    finally:
        await _shutdown_async(fake_adb, stream)

    assert collector.messages() == ["backlog 1", "backlog 2", "backlog 3", "live"]


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
    collector = _AsyncCollector()
    stream = await _start_async(Filter(package=PACKAGE), collector, backed_off=True)
    try:
        assert await collector.received("before")
        fake_adb.release("restart")
        await _unblock_after_restart_async(fake_adb, collector)
        await stream.join(timeout=TIMEOUT)  # logcat exits after "after 2"
    finally:
        await _shutdown_async(fake_adb, stream)

    assert collector.messages() in _RESTART_DELIVERIES


@pytest.mark.xfail(
    strict=True,
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
        await async_wait_until(
            lambda: _shell_calls(fake_adb, polling=True), timeout=TIMEOUT
        )
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
