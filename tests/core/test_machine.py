"""Tests for the stream core's state machine (design 3.2, 5.2; PR-10).

One test per row of the transition table, named after the row
(``test_<state>_<event>``); the state x event product test; the tests the
design names (section 7: F1-5, F2-1, F2-2, F2-3, F2-5, F2-8, F4-3); and seeded
random event sequences for the global rules G1-G6 and line conservation. No
process runs: `tests.core.harness.Harness` scripts every event and the clock.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from logpyt import GroupBy, Package, Tag, Where
from logpyt._core.machine import Mode, StreamCore
from logpyt._core.mailbox import ControlInbox
from logpyt._core.protocol import (
    CallbackFailed,
    CommandDone,
    ConsumerDetached,
    ConsumerProgress,
    Data,
    Deliver,
    Diagnostic,
    Eof,
    Exited,
    Finished,
    KillRequest,
    RunCommand,
    Spawn,
    Spawned,
    StopRequest,
    Terminate,
    Tick,
)
from logpyt.adb import _MARKER_TAG
from logpyt.config import Reconnect
from logpyt.exceptions import AdbError, AdbExitError, CallbackError, ReadTimeoutError
from logpyt.models import LogEntry, LogGroup
from logpyt.streams._events import StateChange, StreamState
from tests.core.harness import (
    APP,
    APP_UID,
    CLOCK,
    CLOCK_FAILED,
    CLOCK_OK,
    LOOKUP_FAILED,
    LOOKUP_OK,
    MARKER_FAILED,
    MARKER_OK,
    STATE_DEVICE,
    STATE_OFFLINE,
    Harness,
    config,
    kinds,
    line,
    lines,
    result,
)
from tests.core.scenarios import (
    CONNECTION,
    CONSUMER,
    CONTROL,
    IMPOSSIBLE,
    KINDS,
    RESULTS,
    ROWS,
    STATES,
    TIMERS,
    UNREACHABLE,
    VARIANTS,
    RandomDriver,
    state_key,
    stimulus,
)

S = StreamState


def _stop(flush: float | None) -> StopRequest:
    return StopRequest(flush=flush)


def _failed(stage: str = "on_item") -> CallbackFailed:
    return CallbackFailed(stage, ValueError("boom"))  # ty: ignore[invalid-argument-type]


def _connecting_without_child() -> Harness:
    harness = Harness.make()
    harness.start()
    return harness


def _connecting_with_child() -> Harness:
    return Harness.make().connecting_spawned()


def _reconnecting(**fields: Any) -> Harness:  # noqa: ANN401
    """RECONNECTING after a connection that ended with status 1, backoff armed."""
    harness = Harness.make(reconnect=Reconnect(), **fields).running()
    harness.end_connection(1)
    assert harness.core.state is S.RECONNECTING
    return harness


def _flushing(**fields: Any) -> Harness:  # noqa: ANN401
    harness = Harness.make(**fields).running()
    harness.send(StopRequest(flush=5.0))
    assert harness.core.mode is Mode.FLUSH
    return harness


def _flush_marker(harness: Harness) -> bytes:
    token = harness.core._flush_token
    assert token is not None
    return line(token, tag=_MARKER_TAG)


def _stopping(mode: Mode, **fields: Any) -> Harness:  # noqa: ANN401
    """RUNNING, then STOPPING in ``mode`` with a live child."""
    harness = Harness.make(**fields).running()
    match mode:
        case Mode.STOP:
            harness.send(StopRequest())
        case Mode.FLUSH:
            harness.send(StopRequest(flush=5.0))
        case Mode.KILL:
            harness.send(KillRequest())
        case Mode.FAIL:
            harness.send(_failed())
    assert harness.core.mode is mode
    return harness


# -- IDLE ------------------------------------------------------------------------------


@pytest.mark.parametrize("watched", [False, True], ids=["no-packages", "packages"])
def test_idle_start(watched: bool) -> None:  # noqa: FBT001
    """IDLE start: CONNECTING, StateChange, the clock read, the uid lookup."""
    harness = Harness.make(where=Package(APP) if watched else None)

    out = harness.start()

    assert harness.core.state is S.CONNECTING
    assert harness.states() == [(S.IDLE, S.CONNECTING)]
    runs = [cmd.kind for cmd in out if isinstance(cmd, RunCommand)]
    assert runs == (["clock", "lookup"] if watched else ["clock"])
    # Intake stays closed until the lookup returns (or package_hold expires).
    assert harness.core.intake_open is not watched


@pytest.mark.parametrize(
    "event",
    [StopRequest(), StopRequest(flush=1.0), KillRequest()],
    ids=["stop", "flush", "kill"],
)
def test_idle_stop_kill_is_noop(event: StopRequest | KillRequest) -> None:
    """IDLE stop, stop(flush), kill: nothing; the stream still starts (D-6)."""
    harness = Harness.make()

    assert harness.send(event) == []
    assert harness.core.state is S.IDLE
    harness.start()
    assert harness.core.state is S.CONNECTING


def test_kill_in_idle_is_noop() -> None:
    """F2-8: kill() from IDLE does not poison the stream (sync.py:617-618 did)."""
    harness = Harness.make()

    assert harness.send(KillRequest()) == []
    harness.running()
    harness.send(StopRequest())
    harness.end_connection(0)

    assert harness.core.state is S.STOPPED
    assert harness.finished == [Finished(None)]


# -- CONNECTING ----------------------------------------------------------------------


@pytest.mark.parametrize("since", ["backlog", "now", "datetime"])
def test_connecting_clock_done(since: str) -> None:
    """CONNECTING clock done: the start marker (since="now") or the spawn."""
    start = datetime(2026, 11, 4, 5, 0, 0, 250, tzinfo=UTC)
    harness = Harness.make(since=start if since == "datetime" else since)
    harness.start()

    out = harness.answer("clock", CLOCK_OK)

    assert harness.core.state is S.CONNECTING
    if since == "now":
        (marker,) = out
        assert isinstance(marker, RunCommand)
        assert marker.kind == "start_marker"
        assert marker.argv[-4:] == ("log", "-t", "logpyt", f"start-{1:032x}")
        return
    (spawn,) = out
    assert isinstance(spawn, Spawn)
    if since == "backlog":
        assert "-T" not in spawn.argv
    else:
        position = spawn.argv[spawn.argv.index("-T") + 1]
        assert position == f"{int(start.timestamp())}.000250"


def test_connecting_start_marker_done() -> None:
    """CONNECTING start marker done: Spawn(c1, argv with -T <clock - 1 s>)."""
    harness = Harness.make(since="now")
    harness.start()
    harness.answer("clock", CLOCK_OK)

    (spawn,) = harness.answer("start_marker", MARKER_OK)

    assert isinstance(spawn, Spawn)
    assert spawn.connection == 1
    assert spawn.argv[spawn.argv.index("-T") + 1] == f"{CLOCK - 1}.000000"


@pytest.mark.parametrize("reconnect", [True, False], ids=["reconnect", "no-reconnect"])
@pytest.mark.parametrize("which", ["clock", "start_marker"])
def test_connecting_clock_or_marker_failed(which: str, reconnect: bool) -> None:  # noqa: FBT001
    """CONNECTING clock or start marker failed: RECONNECTING, or FAILED."""
    harness = Harness.make(since="now", reconnect=Reconnect() if reconnect else None)
    harness.start()
    if which == "start_marker":
        harness.answer("clock", CLOCK_OK)

    harness.answer(which, CLOCK_FAILED if which == "clock" else MARKER_FAILED)  # ty: ignore[invalid-argument-type]

    (change,) = harness.changes()[-1:]
    assert isinstance(change.error, AdbExitError)
    assert str(change.error) == "device not available"
    assert change.error.returncode == 1
    assert change.error.stderr == "error: device offline\n"
    assert isinstance(change.error.__cause__, AdbError)
    if reconnect:
        assert harness.core.state is S.RECONNECTING
        assert harness.core.armed_timers() == {"backoff"}
        assert harness.finished == []
    else:
        assert harness.core.state is S.FAILED
        assert harness.finished == [Finished(change.error)]


def test_connecting_clock_could_not_run_adb_is_terminal() -> None:
    """A one-shot adb that cannot be started is not retried, like a spawn (A-12)."""
    harness = Harness.make(reconnect=Reconnect())
    harness.start()
    message = "could not start adb shell date +%s: [Errno 2] No such file"
    error = AdbError(message, argv=("adb",), returncode=None)

    harness.answer("clock", error)

    assert harness.core.state is S.FAILED
    outcome = harness.core.outcome
    assert isinstance(outcome, AdbExitError)
    # The message says what failed, as the spawn path does, not "device not
    # available"; no "after N attempts" note, since nothing was retried.
    assert str(outcome) == message
    assert outcome.returncode is None
    assert outcome.__cause__ is error
    assert getattr(outcome, "__notes__", []) == []


@pytest.mark.parametrize("which", ["clock", "start_marker"])
def test_connecting_clock_or_marker_timed_out(
    which: Literal["clock", "start_marker"],
) -> None:
    """A one-shot command that timed out says so in the cause."""
    harness = Harness.make(since="now")
    harness.start()
    if which == "start_marker":
        harness.answer("clock", CLOCK_OK)
    command = harness.request(which)
    timeout = TimeoutError("adb ... did not finish within 5.0 s")

    harness.answer(which, timeout)

    outcome = harness.core.outcome
    assert isinstance(outcome, AdbExitError)
    shown = " ".join(["adb", *command.argv[1:]])
    assert str(outcome) == f"device not available: {shown} timed out after 5 s"
    assert outcome.__cause__ is timeout


def test_connecting_spawned() -> None:
    """CONNECTING spawned(c): arm connect_timeout."""
    harness = Harness.make(connect_timeout=7.0)
    harness.start()
    harness.answer("clock", CLOCK_OK)

    assert harness.spawned() == []

    assert harness.core.state is S.CONNECTING
    assert harness.core.armed_timers() == {"connect"}
    assert harness.core.next_deadline() == pytest.approx(harness.now + 7.0)


def test_connecting_spawn_failed() -> None:
    """CONNECTING spawn_failed(c): FAILED with AdbExitError(returncode=None)."""
    harness = Harness.make()
    harness.start()
    harness.answer("clock", CLOCK_OK)
    error = PermissionError("adb")

    harness.spawn_failed(error)

    assert harness.core.state is S.FAILED
    outcome = harness.core.outcome
    assert isinstance(outcome, AdbExitError)
    assert outcome.returncode is None
    assert outcome.__cause__ is error
    assert harness.finished == [Finished(outcome)]


def test_spawn_failure_is_terminal() -> None:
    """F2-3: a spawn failure ends the run even with reconnect; never retried (A-12)."""
    harness = Harness.make(reconnect=Reconnect(max_attempts=None))
    harness.start()
    harness.answer("clock", CLOCK_OK)

    harness.spawn_failed()

    assert harness.core.state is S.FAILED
    assert harness.core.next_deadline() is None
    assert harness.advance(3600.0) == []
    assert [cmd for cmd in harness.commands if isinstance(cmd, Spawn)] == [
        harness.spawns[1]
    ]
    assert harness.send(StopRequest()) == []
    assert harness.core.stats().connections == 1


def test_connecting_stderr_data() -> None:
    """CONNECTING data(c, stderr): log the lines and keep the stderr tail."""
    harness = Harness.make(stderr_tail=2).connecting_spawned()

    out = harness.stderr(b"- waiting for device -\nsecond\nthird\n")

    assert harness.core.state is S.CONNECTING
    assert [cmd.message for cmd in out if isinstance(cmd, Diagnostic)] == [
        "- waiting for device -",
        "second",
        "third",
    ]
    assert all(
        cmd.logger == "logpyt.adb" and cmd.level == logging.WARNING
        for cmd in out
        if isinstance(cmd, Diagnostic)
    )
    conn = harness.core._conn
    assert conn is not None
    assert list(conn.stderr_tail) == ["second", "third"]
    assert harness.core.stats().stderr_lines == 3


@pytest.mark.parametrize("since", ["backlog", "now"])
def test_connecting_stdout_data(since: str) -> None:
    """CONNECTING data(c, stdout): RUNNING; since="now" discards up to the marker."""
    harness = Harness.make(since=since, reconnect=Reconnect(), read_timeout=3.0)
    harness.connecting_spawned()
    conn = harness.core._conn
    assert conn is not None
    marker = conn.marker
    chunk = lines(2, "old")
    if marker is not None:
        chunk += line(marker, tag=_MARKER_TAG)
    chunk += lines(2, "new")

    harness.stdout(chunk)

    assert harness.core.state is S.RUNNING
    assert harness.states()[-1] == (S.CONNECTING, S.RUNNING)
    assert harness.core.armed_timers() == {"read", "stable"}
    if since == "now":
        assert harness.messages() == ["new0", "new1"]
        assert harness.core.stats().before_start == 3
    else:
        assert harness.messages() == ["old0", "old1", "new0", "new1"]
    # The state change comes before the entries of the same chunk (G3).
    (deliver,) = [cmd for cmd in harness.commands if isinstance(cmd, Deliver)][-1:]
    assert isinstance(deliver.records[0], StateChange)


def test_connecting_stdout_before_marker_stays_connecting() -> None:
    """since="now": output before the start marker keeps the attempt CONNECTING."""
    harness = Harness.make(since="now").connecting_spawned()

    harness.stdout(lines(3, "old"))

    assert harness.core.state is S.CONNECTING
    assert harness.delivered() == []
    assert harness.core.stats().before_start == 3


def test_connecting_connect_timeout() -> None:
    """CONNECTING connect_timeout: RunCommand(state check: adb get-state)."""
    harness = _connecting_with_child()

    out = harness.until_deadline()

    (check,) = out
    assert isinstance(check, RunCommand)
    assert check.kind == "state"
    assert check.argv[-1] == "get-state"
    assert harness.core.armed_timers() == frozenset()


@pytest.mark.parametrize("since", ["backlog", "now"])
def test_connecting_state_check_device(since: str) -> None:
    """CONNECTING state check = device: RUNNING; before the marker, re-arm."""
    harness = Harness.make(since=since, reconnect=Reconnect(stable_after=10.0))
    harness.connecting_spawned()
    harness.until_deadline()

    harness.answer("state", STATE_DEVICE)

    if since == "now":
        assert harness.core.state is S.CONNECTING
        assert harness.core.armed_timers() == {"connect"}
    else:
        assert harness.core.state is S.RUNNING
        # No output yet: stable_after counts from the first output (5.6), so
        # a present but silent device does not reset the reconnect budget.
        assert harness.core.armed_timers() == frozenset()
        harness.advance(3.0)
        assert harness.core.armed_timers() == frozenset()
        harness.stdout(line("late"))
        assert harness.core.armed_timers() == {"stable"}
        assert harness.core.next_deadline() == pytest.approx(harness.now + 10.0)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (STATE_OFFLINE, "offline"),
        (result(1, stderr="error: device 'x' not found\n"), "not found"),
        (TimeoutError("get-state"), "adb get-state failed: get-state"),
    ],
    ids=["offline", "not-found", "timeout"],
)
@pytest.mark.parametrize("reconnect", [True, False], ids=["reconnect", "no-reconnect"])
def test_connecting_state_check_other_or_failed(
    answer: object,
    reason: str,
    reconnect: bool,  # noqa: FBT001
) -> None:
    """CONNECTING state check other or failed: Kill(c); the connection-ended row."""
    harness = Harness.make(reconnect=Reconnect() if reconnect else None, stderr_tail=5)
    harness.connecting_spawned()
    harness.stderr(b"- waiting for device -\n")
    harness.until_deadline()

    out = harness.answer("state", answer)  # ty: ignore[invalid-argument-type]

    assert kinds(out) == ["Kill"]
    assert harness.core.state is S.CONNECTING
    harness.end_connection(-9)
    error = harness.changes()[-1].error
    assert isinstance(error, AdbExitError)
    assert str(error) == f"device not available: {reason}"
    assert error.stderr == "- waiting for device -"
    assert error.returncode == -9
    assert harness.core.state is (S.RECONNECTING if reconnect else S.FAILED)


@pytest.mark.parametrize("reconnect", [True, False], ids=["reconnect", "no-reconnect"])
def test_connecting_connection_ended(reconnect: bool) -> None:  # noqa: FBT001
    """CONNECTING exited and eof: as the RUNNING connection-ended row."""
    harness = Harness.make(reconnect=Reconnect() if reconnect else None)
    harness.connecting_spawned()

    harness.end_connection(255)

    error = harness.changes()[-1].error
    assert isinstance(error, AdbExitError)
    assert error.returncode == 255
    assert str(error) == "adb logcat exited with status 255"
    assert harness.core.state is (S.RECONNECTING if reconnect else S.FAILED)


@pytest.mark.parametrize("reconnect", [True, False], ids=["reconnect", "no-reconnect"])
def test_connecting_exit_drain(reconnect: bool) -> None:  # noqa: FBT001
    """CONNECTING exited, exit_drain expired: as the RUNNING connection-ended row.

    adb exited, but a process it started holds the pipes (design 5.1, 5.10):
    the attempt still ends.
    """
    harness = Harness.make(
        reconnect=Reconnect(delay=3.0) if reconnect else None, exit_drain=2.0
    )
    harness.connecting_spawned()

    harness.exited(1)
    assert harness.core.state is S.CONNECTING
    assert "exit_drain" in harness.core.armed_timers()
    harness.advance(2.0)

    error = harness.changes()[-1].error
    assert isinstance(error, AdbExitError)
    assert error.returncode == 1
    assert harness.core.connection is None
    assert harness.core.state is (S.RECONNECTING if reconnect else S.FAILED)
    (warning,) = [cmd for cmd in harness.diagnostics if "exit_drain" in cmd.message]
    assert warning.level == logging.WARNING
    assert warning.message.startswith(
        "logcat connection 1: adb exited, but its stdout and stderr pipes stayed "
        "open for exit_drain (2 s)"
    )


@pytest.mark.parametrize("order", ["late-data", "exited", "exit-check"])
def test_connecting_ending_attempt_is_not_running(order: str) -> None:
    """3.1: an attempt the core gave up on, or whose adb exited, is not RUNNING.

    late-data: output after the Kill of a failed state check; exited: adb
    exited with its pipes held open, so exit_drain ends the attempt and no
    state check runs; exit-check: a state check answers "device" after adb
    exited. The connection-ended row decides each time.
    """
    harness = Harness.make(reconnect=Reconnect(), exit_drain=5.0, connect_timeout=10.0)
    harness.connecting_spawned()
    if order == "late-data":
        harness.until_deadline()
        assert kinds(harness.answer("state", STATE_OFFLINE)) == ["Kill"]
        harness.stdout(line("late"))
        assert harness.core.state is S.CONNECTING
        harness.end_connection(-9)
        assert harness.messages() == ["late"]  # still delivered, not lost
        reason = "device not available: offline"
    elif order == "exited":
        harness.advance(8.0)
        harness.exited(1)
        assert harness.core.armed_timers() == {"exit_drain"}
        harness.advance(5.0)
        runs = [cmd.kind for cmd in harness.commands if isinstance(cmd, RunCommand)]
        assert runs == ["clock"]  # no state check
        reason = "adb logcat exited with status 1"
    else:
        harness.until_deadline()  # the state check runs
        harness.exited(1)
        assert harness.answer("state", STATE_DEVICE) == []
        assert harness.core.state is S.CONNECTING
        harness.eof()
        reason = "adb logcat exited with status 1"

    assert harness.states() == [
        (S.IDLE, S.CONNECTING),
        (S.CONNECTING, S.RECONNECTING),
    ]
    assert str(harness.changes()[-1].error) == reason


@pytest.mark.parametrize("reconnect", [True, False], ids=["reconnect", "no-reconnect"])
def test_connecting_state_check_cannot_start(reconnect: bool) -> None:  # noqa: FBT001
    """A state check whose adb cannot start takes the 3.2 row: Kill(c), then the
    connection-ended row, retried like any failed check (A.20 covers the clock
    read and the start marker, which end FAILED)."""
    harness = Harness.make(reconnect=Reconnect() if reconnect else None)
    harness.connecting_spawned()
    harness.until_deadline()
    error = AdbError(
        "could not start adb get-state: [Errno 24] Too many open files",
        argv=("adb", "get-state"),
        returncode=None,
    )

    assert kinds(harness.answer("state", error)) == ["Kill"]
    harness.end_connection(-9)

    cause = harness.changes()[-1].error
    assert isinstance(cause, AdbExitError)
    assert str(cause) == f"device not available: adb get-state failed: {error}"
    assert cause.returncode == -9  # the logcat's
    assert harness.core.state is (S.RECONNECTING if reconnect else S.FAILED)


@pytest.mark.parametrize("how", ["lookup", "hold"])
def test_connecting_lookup_done_or_hold_expired(how: str) -> None:
    """CONNECTING lookup done or hold_expired: update the uid map; open intake."""
    harness = Harness.make(where=Package(APP), package_hold=2.0)
    harness.connecting_spawned()
    assert not harness.core.intake_open

    if how == "lookup":
        harness.answer("lookup", LOOKUP_OK)
        assert harness.core._attribution.uid_packages == {APP_UID: APP}
    else:
        assert harness.core.armed_timers() == {"connect", "hold"}
        harness.advance(2.0)

    assert harness.core.state is S.CONNECTING
    assert harness.core.intake_open


def test_connecting_intake_waits_for_every_first_lookup() -> None:
    """Intake opens when the last of the first lookups returns, not the first."""
    harness = Harness.make(where=Package(APP) | Package("com.example.other"))
    harness.connecting_spawned()

    harness.answer("lookup", LOOKUP_OK)
    assert not harness.core.intake_open
    harness.answer("lookup", LOOKUP_OK)

    assert harness.core.intake_open


def test_lines_after_a_failed_lookup_are_unattributed() -> None:
    """A failed lookup opens intake; app lines then count as unattributed (4.4)."""
    harness = Harness.make(where=Package(APP))
    harness.connecting_spawned()

    harness.answer("lookup", LOOKUP_FAILED)
    harness.stdout(line("x"))

    stats = harness.core.stats()
    assert stats.unattributed == 1
    assert "unattributed" in stats.incomplete


@pytest.mark.parametrize("flush", [None, 1.0], ids=["stop", "flush"])
@pytest.mark.parametrize("child", [True, False], ids=["child", "no-child"])
def test_connecting_stop(child: bool, flush: float | None) -> None:  # noqa: FBT001
    """CONNECTING stop or stop(flush): STOPPING(stop) with a child, else STOPPED."""
    harness = _connecting_with_child() if child else _connecting_without_child()

    out = harness.send(_stop(flush))

    if child:
        assert kinds(out) == ["Terminate", "Deliver"]
        assert harness.core.mode is Mode.STOP
        assert harness.core.armed_timers() == {"stop_grace"}
        harness.end_connection(0)
    else:
        assert kinds(out) == ["Deliver", "Finished"]
        # The clock read is abandoned (G5): its late result changes nothing.
        assert harness.answer("clock", CLOCK_OK) == []
    assert harness.core.state is S.STOPPED
    # Design A.20: no connection reads a marker here, so a flush cannot run.
    stats = harness.core.stats()
    assert stats.flush_failed is (flush is not None)
    assert stats.complete is (flush is None)


@pytest.mark.parametrize("child", [True, False], ids=["child", "no-child"])
def test_connecting_kill(child: bool) -> None:  # noqa: FBT001
    """CONNECTING kill: DiscardUndelivered; Kill(c) with a child, else KILLED."""
    harness = _connecting_with_child() if child else _connecting_without_child()

    out = harness.send(KillRequest())

    if child:
        assert kinds(out) == ["Kill", "Deliver", "DiscardUndelivered"]
        assert harness.core.mode is Mode.KILL
    else:
        assert kinds(out) == ["DiscardUndelivered", "Deliver", "Finished"]
        assert harness.core.state is S.KILLED


@pytest.mark.parametrize("child", [True, False], ids=["child", "no-child"])
def test_connecting_callback_failed(child: bool) -> None:  # noqa: FBT001
    """CONNECTING callback_failed: latch CallbackError; Kill(c) or FAILED."""
    harness = _connecting_with_child() if child else _connecting_without_child()

    out = harness.send(_failed("on_event"))

    assert isinstance(harness.core.outcome, CallbackError)
    assert "DiscardUndelivered" in kinds(out)
    if child:
        assert "Kill" in kinds(out)
        assert harness.core.mode is Mode.FAIL
    else:
        assert harness.core.state is S.FAILED
        assert harness.finished == [Finished(harness.core.outcome)]


@pytest.mark.parametrize("child", [True, False], ids=["child", "no-child"])
def test_connecting_consumer_detached(child: bool) -> None:  # noqa: FBT001
    """CONNECTING consumer_detached: as stop; the outbox discards from now on."""
    harness = _connecting_with_child() if child else _connecting_without_child()

    harness.detach()

    if child:
        assert harness.core.mode is Mode.STOP
        assert isinstance(harness.signals[-1], Terminate)
        harness.stdout(lines(2))
        assert harness.delivered() == []
        assert harness.core.stats().undelivered == 2
    else:
        assert harness.core.state is S.STOPPED


def test_on_event_raising_in_connecting_fails_cleanly() -> None:
    """F2-5: on_event raising on IDLE -> CONNECTING fails the run cleanly."""
    harness = Harness.make(reconnect=Reconnect())
    harness.start()
    change = harness.consume()[0]
    assert isinstance(change, StateChange)
    assert change.new is S.CONNECTING

    harness.send(_failed("on_event"))

    assert harness.core.state is S.FAILED
    outcome = harness.core.outcome
    assert isinstance(outcome, CallbackError)
    assert outcome.stage == "on_event"
    assert isinstance(outcome.__cause__, ValueError)
    assert harness.finished == [Finished(outcome)]
    assert harness.core.next_deadline() is None
    # The abandoned clock read is ignored.
    assert harness.answer("clock", CLOCK_OK) == []


def test_stop_from_on_event_connecting_wins() -> None:
    """F2-2: stop() from on_event(IDLE -> CONNECTING) is not erased by start()."""
    harness = Harness.make()
    harness.start()
    harness.consume()

    harness.send(StopRequest())  # from the on_event hook, through the control inbox

    assert harness.core.state is S.STOPPED
    assert harness.answer("clock", CLOCK_OK) == []
    assert harness.spawns == {}


def test_kill_during_spawn_ends_killed() -> None:
    """F2-1: a kill between Spawn and Spawned ends KILLED; the spawned row kills."""
    harness = Harness.make()
    harness.start()
    harness.answer("clock", CLOCK_OK)  # Spawn issued, Spawned not yet seen

    out = harness.send(KillRequest())
    assert harness.core.mode is Mode.KILL
    assert "Kill" not in kinds(out)  # nothing to kill yet

    assert kinds(harness.spawned()) == ["Kill"]
    harness.stdout(lines(3))  # adb's output before the kill: discarded
    harness.end_connection(-9)

    assert harness.core.state is S.KILLED
    assert S.RUNNING not in {new for _, new in harness.states()}
    assert harness.delivered() == []
    assert harness.core.stats().undelivered == 3
    assert harness.core.stats().killed
    assert harness.finished == [Finished(None)]


# -- RUNNING ---------------------------------------------------------------------------


def test_running_data() -> None:
    """RUNNING data(c): frame, pipeline, Deliver; either pipe refreshes the deadline."""
    harness = Harness.make(read_timeout=5.0).running()
    harness.advance(4.0)

    harness.stdout(lines(2) + b"partial")
    assert harness.core.next_deadline() == pytest.approx(harness.now + 5.0)
    harness.advance(4.0)
    harness.stderr(b"note\n")
    assert harness.core.next_deadline() == pytest.approx(harness.now + 5.0)

    assert harness.messages() == ["first", "m0", "m1"]


def test_running_eof() -> None:
    """RUNNING eof(c, pipe): emit that framer's partial last line."""
    harness = Harness.make().running()
    harness.stdout(line("whole") + line("cut")[:-1])

    harness.eof("stdout")

    assert harness.messages() == ["first", "whole", "cut"]
    assert harness.core.state is S.RUNNING


def test_running_exited() -> None:
    """RUNNING exited(c, rc): arm exit_drain; it counts only while intake is open."""
    harness = Harness.make(exit_drain=2.0, buffer_entries=2).running()

    harness.exited(0)
    assert harness.core.armed_timers() == {"exit_drain"}
    assert harness.core.next_deadline() == pytest.approx(harness.now + 2.0)
    harness.advance(1.0)
    harness.stdout(lines(1))  # the outbox is full: intake closes
    assert not harness.core.intake_open
    assert "exit_drain" not in harness.core.armed_timers()
    harness.advance(30.0)
    assert harness.core.state is S.RUNNING
    harness.consume()  # low-water mark: the drain counts again with 1 s left
    assert harness.core.next_deadline() == pytest.approx(harness.now + 1.0)


@pytest.mark.parametrize("end", ["eof", "exit_drain"])
@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize("reconnect", [True, False], ids=["reconnect", "no-reconnect"])
def test_running_connection_ended(end: str, returncode: int, reconnect: bool) -> None:  # noqa: FBT001
    """RUNNING connection ended: retire c, flush groups, RECONNECTING or FAILED."""
    harness = Harness.make(
        reconnect=Reconnect(delay=3.0) if reconnect else None,
        group_by=GroupBy(keys=("pid",), gap_ms=60_000.0, idle_flush_ms=None),
    ).running()
    harness.exited(returncode)
    if end == "eof":
        harness.eof()
    else:
        harness.advance(2.0)  # exit_drain expired; the pipes never reached EOF

    error = harness.changes()[-1].error
    assert isinstance(error, AdbExitError)
    assert error.returncode == returncode  # 0 included: follow mode never ends
    (group,) = harness.delivered()
    assert isinstance(group, LogGroup)
    assert group.reason == "end"
    assert harness.core.connection is None
    if reconnect:
        assert harness.core.state is S.RECONNECTING
        assert harness.core.next_deadline() == pytest.approx(harness.now + 3.0)
        # A recovered error is logged once at WARNING (design 4.2).
        (warning,) = [
            cmd for cmd in harness.diagnostics if "reconnecting" in cmd.message
        ]
        assert warning.level == logging.WARNING
        assert warning.message == (
            f"logcat connection 1 ended, reconnecting in 3 s: {error}"
        )
    else:
        assert harness.core.state is S.FAILED
        assert harness.core.outcome is error
    drained = [cmd for cmd in harness.diagnostics if "exit_drain" in cmd.message]
    assert len(drained) == (1 if end == "exit_drain" else 0)
    # G1: the retired connection's late events change nothing.
    assert harness.send(Data(1, "stdout", line("late"))) == []


def test_running_connection_ended_after_attempts_exhausted() -> None:
    """max_attempts exhausted: the last error, noted "after N attempts"."""
    harness = Harness.make(reconnect=Reconnect(max_attempts=2, delay=1.0)).running()
    for _ in range(2):
        harness.end_connection(1)
        harness.until_deadline()
        harness.answer("clock", CLOCK_OK)
        harness.spawned()

    harness.end_connection(1)

    assert harness.core.state is S.FAILED
    outcome = harness.core.outcome
    assert isinstance(outcome, AdbExitError)
    assert outcome.__notes__ == ["after 2 attempts"]
    assert harness.core.stats().connections == 3
    assert harness.core.stats().reconnects == 2


def test_attempts_note_is_singular_for_one() -> None:
    """4.1's note reads "after 1 attempt" for a single failed attempt."""
    harness = Harness.make(reconnect=Reconnect(max_attempts=1, delay=1.0)).running()
    harness.end_connection(1)
    harness.until_deadline()
    harness.answer("clock", CLOCK_OK)
    harness.spawned()

    harness.end_connection(1)

    outcome = harness.core.outcome
    assert isinstance(outcome, AdbExitError)
    assert outcome.__notes__ == ["after 1 attempt"]


def test_backoff_is_capped_at_max_delay() -> None:
    """RECONNECTING backoff: delay := min(max_delay, delay * backoff)."""
    harness = Harness.make(
        reconnect=Reconnect(delay=1.0, backoff=10.0, max_delay=5.0)
    ).running()
    waits: list[float] = []
    for _ in range(3):
        harness.end_connection(1)
        deadline = harness.core.next_deadline()
        assert deadline is not None
        waits.append(deadline - harness.now)
        harness.until_deadline()
        harness.answer("clock", CLOCK_OK)
        harness.spawned()

    assert waits == pytest.approx([1.0, 5.0, 5.0])


def test_running_read_timeout() -> None:
    """RUNNING read_timeout: Kill(c); cause ReadTimeoutError; then the end row."""
    harness = Harness.make(read_timeout=5.0, stderr_tail=3).running()
    harness.stderr(b"adb: something\n")

    out = harness.advance(5.0)

    assert kinds(out) == ["Kill"]
    assert harness.core.armed_timers() == frozenset()
    harness.end_connection(-9)
    assert harness.core.state is S.FAILED
    outcome = harness.core.outcome
    assert isinstance(outcome, ReadTimeoutError)
    assert str(outcome) == "no output on stdout or stderr for 5 s"
    assert outcome.__notes__ == ["adb stderr:\nadb: something"]


def test_read_timeout_below_one_second() -> None:
    """F4-3: a sub-second read_timeout fires on the injected monotonic clock."""
    harness = Harness.make(read_timeout=0.3, reconnect=Reconnect()).running()

    assert harness.core.next_deadline() == pytest.approx(harness.now + 0.3)
    assert harness.advance(0.29) == []
    out = harness.advance(0.01)

    assert kinds(out) == ["Kill"]
    harness.end_connection(-9)
    assert harness.core.state is S.RECONNECTING
    assert isinstance(harness.changes()[-1].error, ReadTimeoutError)


def test_running_stall() -> None:
    """RUNNING stall: Kill(c), mark c stalled, stall_resumes += 1; c is still read."""
    harness = Harness.make(buffer_entries=4, stall_timeout=1.0).running()
    harness.stdout(lines(3))  # 4 entries: full
    assert not harness.core.intake_open
    assert harness.core.armed_timers() == {"stall"}

    out = harness.advance(1.0)

    assert kinds(out) == ["Kill"]
    assert harness.core.state is S.RUNNING
    assert harness.core.stats().stall_resumes == 1
    assert harness.core.armed_timers() == frozenset()
    assert harness.core.stats().backpressure_seconds == pytest.approx(1.0)


def test_running_stable_after() -> None:
    """RUNNING stable_after: attempts := 0, delay := Reconnect.delay."""
    harness = Harness.make(
        reconnect=Reconnect(delay=1.0, backoff=2.0, stable_after=10.0)
    ).running()
    harness.end_connection(1)
    harness.until_deadline()  # attempt 1
    harness.answer("clock", CLOCK_OK)
    harness.spawned()
    harness.stdout(line("x"))
    assert harness.core._attempts == 1
    assert harness.core._delay == pytest.approx(2.0)
    assert harness.core.armed_timers() == {"stable"}

    harness.advance(10.0)

    assert harness.core._attempts == 0
    assert harness.core._delay == pytest.approx(1.0)
    assert harness.core.armed_timers() == frozenset()


@pytest.mark.parametrize("end", ["exited", "killed"])
def test_stable_after_skips_an_ending_connection(end: str) -> None:
    """5.6: a connection whose adb exited or was killed resets no budget.

    exited: adb exited under backpressure, so its end waits for the consumer
    past stable_after; killed: a read-timeout Kill, then an exit whose pipe
    stays open past stable_after. Either way the attempt still counts.
    """
    harness = Harness.make(
        reconnect=Reconnect(delay=1.0, max_attempts=1, stable_after=10.0),
        read_timeout=9.0 if end == "killed" else None,
        buffer_entries=4,
        stall_timeout=None,
        exit_drain=5.0,
    ).running()
    harness.end_connection(1)
    harness.until_deadline()  # attempt 1 of 1
    harness.answer("clock", CLOCK_OK)
    harness.spawned()
    harness.stdout(line("x"))  # stable_after armed: 10 s
    if end == "exited":
        harness.stdout(lines(3))  # full: intake closed
        harness.advance(1.0)
        harness.exited(1)  # exit_drain paused while intake is closed
    else:
        harness.advance(9.0)  # read timeout: Kill(c)
        harness.exited(-9)
        harness.eof("stderr")  # stdout stays open

    assert "stable" not in harness.core.armed_timers()
    harness.advance(10.0 if end == "exited" else 1.5)  # past stable_after
    assert harness.core.state is S.RUNNING
    assert harness.core._attempts == 1
    if end == "exited":
        harness.consume()  # intake reopens; exit_drain runs out
    harness.advance(5.0)

    assert harness.core.state is S.FAILED
    assert str(harness.core.outcome).startswith(
        "adb logcat exited" if end == "exited" else "no output"
    )


def test_stable_after_is_disarmed_on_leaving_running() -> None:
    """G5: an earlier connection's stable_after deadline does not fire later.

    Connection 1 prints and ends before stable_after; attempt 2 is RUNNING as
    a quiet device (no output), so no stable_after runs for it.
    """
    harness = Harness.make(
        reconnect=Reconnect(delay=1.0, max_attempts=1, stable_after=10.0),
        connect_timeout=2.0,
    ).running()  # stable_after due at +10 s
    harness.advance(5.0)
    harness.end_connection(1)
    harness.until_deadline()  # attempt 1 of 1, at +6 s
    harness.answer("clock", CLOCK_OK)
    harness.spawned()
    harness.until_deadline()  # connect_timeout: the state check, at +8 s
    harness.answer("state", STATE_DEVICE)
    assert harness.core.state is S.RUNNING
    assert harness.core.armed_timers() == frozenset()

    harness.advance(5.0)  # past connection 1's deadline

    assert harness.core._attempts == 1
    harness.end_connection(1)
    assert harness.core.state is S.FAILED


def test_running_consumer_progress() -> None:
    """RUNNING consumer_progress at low water: no stall; read deadline re-armed."""
    harness = Harness.make(
        buffer_entries=4, stall_timeout=5.0, read_timeout=3.0, exit_drain=2.0
    ).running()
    harness.stdout(lines(3))
    assert harness.core.armed_timers() == {"stall"}  # the read deadline is suspended
    harness.advance(4.0)

    harness.consume(1)  # 3 queued: above half, still closed
    assert not harness.core.intake_open
    harness.consume(1)  # 2 queued: at half

    assert harness.core.intake_open
    assert harness.core.armed_timers() == {"read"}
    # Re-armed in full.
    assert harness.core.next_deadline() == pytest.approx(harness.now + 3.0)
    assert harness.core.stats().backpressure_seconds == pytest.approx(4.0)


def test_running_consumer_progress_resumes_exit_drain() -> None:
    """exit_drain is paused while intake is closed and resumes at the low-water mark."""
    harness = Harness.make(buffer_entries=4, exit_drain=2.0).running()
    harness.exited(0)
    harness.advance(0.5)
    harness.stdout(lines(3))  # full: the drain pauses with 1.5 s left
    assert harness.core.armed_timers() == frozenset()  # no child: no stall either
    harness.advance(10.0)

    harness.consume()

    assert harness.core.armed_timers() == {"exit_drain"}
    assert harness.core.next_deadline() == pytest.approx(harness.now + 1.5)


def test_running_stop() -> None:
    """RUNNING stop: STOPPING(stop); Terminate(c); arm stop_grace."""
    harness = Harness.make(stop_grace=2.0).running()

    out = harness.send(StopRequest())

    assert kinds(out) == ["Terminate", "Deliver"]
    assert harness.core.mode is Mode.STOP
    assert harness.core.armed_timers() == {"stop_grace"}


def test_running_stop_flush() -> None:
    """RUNNING stop(flush=f): STOPPING(flush); RunCommand(flush marker); flush timer."""
    harness = Harness.make().running()

    out = harness.send(StopRequest(flush=4.0))

    (run,) = [cmd for cmd in out if isinstance(cmd, RunCommand)]
    assert run.kind == "flush_marker"
    assert run.argv[-3:] == ("-t", "logpyt", f"flush-{1:032x}")
    assert harness.core.mode is Mode.FLUSH
    assert harness.core.armed_timers() == {"flush"}
    assert harness.core.next_deadline() == pytest.approx(harness.now + 4.0)
    assert harness.signals == []  # keep reading


def test_running_kill() -> None:
    """RUNNING kill: STOPPING(kill); Kill(c); DiscardUndelivered."""
    harness = Harness.make().running()
    harness.stdout(lines(2))

    out = harness.send(KillRequest())

    assert kinds(out) == ["Kill", "Deliver", "DiscardUndelivered"]
    assert harness.core.mode is Mode.KILL
    assert len(harness.outbox) == 3  # the state changes stay
    assert harness.outbox.entries == 0
    # The driver reported what the discard removed: no consumer step needed.
    assert harness.core.stats().undelivered == 3


@pytest.mark.parametrize("stage", ["on_item", "on_event", "where", "group_by"])
def test_running_callback_failed(stage: str) -> None:
    """RUNNING callback_failed: STOPPING(fail), Kill(c); F3-1 keeps accepted items."""
    calls = 0

    def where(entry: LogEntry) -> bool:
        nonlocal calls
        calls += 1
        if stage == "where" and entry.message == "bad":
            raise ValueError(entry.message)
        return True

    harness = Harness.make(where=Where(where)).running()
    if stage == "group_by":
        harness.core._grouper = _RaisingGrouper()  # ty: ignore[invalid-assignment]
    if stage in {"where", "group_by"}:
        out = harness.stdout(line("ok") + line("bad") + line("after"))
    else:
        harness.stdout(line("ok"))
        out = harness.send(_failed(stage))

    outcome = harness.core.outcome
    assert isinstance(outcome, CallbackError)
    assert outcome.stage == stage
    assert harness.core.mode is Mode.FAIL
    assert "Kill" in kinds(out)
    if stage in {"where", "group_by"}:
        # The items accepted before the failure are still delivered (F3-1);
        # "bad" and the line after it are discarded and counted.
        assert "DiscardUndelivered" not in kinds(out)
        expected = ["first", "ok"] if stage == "where" else ["first"]
        assert harness.messages() == expected
        assert harness.core.stats().undelivered == 2
        harness.end_connection(-9)
        # The open group ("ok") is discarded at the end: STOPPING(fail).
        assert harness.core.stats().undelivered == (2 if stage == "where" else 3)
    else:
        assert "DiscardUndelivered" in kinds(out)
    assert harness.core.unaccounted_lines() == 0


class _RaisingGrouper:
    """A grouper that holds every entry in one group and raises on "bad"."""

    def __init__(self) -> None:
        self.entries: list[LogEntry] = []

    @property
    def open_groups(self) -> int:
        return 1 if self.entries else 0

    @property
    def open_entries(self) -> int:
        return len(self.entries)

    def add(self, entry: LogEntry, now: float) -> list[LogGroup]:
        del now
        if entry.message == "bad":
            raise RuntimeError("grouping")
        self.entries.append(entry)
        return []

    def idle_deadline(self) -> None:
        return None

    def flush(self) -> list[LogGroup]:
        entries, self.entries = self.entries, []
        return (
            [LogGroup(key=(), entries=tuple(entries), reason="end")] if entries else []
        )


@pytest.mark.parametrize("stage", ["where", "group_by"])
def test_stage_failure_warns_once(stage: str) -> None:
    """F1-5, A.20: the line where/group_by raised on is a discard with its WARNING,
    also when it is the last line read."""

    def where(entry: LogEntry) -> bool:
        if stage == "where" and entry.message == "bad":
            raise ValueError(entry.message)
        return True

    harness = Harness.make(where=Where(where)).running()
    if stage == "group_by":
        harness.core._grouper = _RaisingGrouper()  # ty: ignore[invalid-assignment]

    harness.stdout(line("ok") + line("bad"))
    harness.end_connection(-9)

    assert harness.core.state is S.FAILED
    warnings = [
        cmd.message for cmd in harness.diagnostics if "discarding" in cmd.message
    ]
    assert warnings == [
        f"discarding undelivered items ({stage} raised); they are counted in "
        "stats.undelivered"
    ]
    # "bad"; with group_by also "ok", held in the group discarded at the end.
    assert harness.core.stats().undelivered == (1 if stage == "where" else 2)


def test_running_consumer_detached() -> None:
    """RUNNING consumer_detached: STOPPING(stop); Terminate(c); the rest discarded."""
    harness = Harness.make().running()

    out = harness.detach()
    harness.stdout(lines(2))

    assert kinds(out) == ["Terminate", "Deliver"]
    assert harness.core.mode is Mode.STOP
    # One entry discarded by the detach (in its counts), two by the core.
    assert harness.core.stats().undelivered == 3


def test_running_lookup_hold_idle() -> None:
    """RUNNING lookup done, hold_expired, idle_flush: uid map, idle groups out."""
    harness = Harness.make(
        where=Package(APP) | Tag("App"),
        group_by=GroupBy(keys=("tag",), gap_ms=1000.0, idle_flush_ms=500.0),
        package_hold=1.0,
    )
    harness.connecting_spawned()
    harness.advance(1.0)  # hold_expired: intake opens without the uid map
    assert harness.core.intake_open
    harness.stdout(line("before"))
    assert harness.core.state is S.RUNNING
    assert harness.core.stats().unattributed == 1

    harness.answer("lookup", LOOKUP_OK)
    harness.stdout(line("after"))
    assert harness.core.stats().unattributed == 1
    assert harness.core.armed_timers() == {"idle"}
    harness.advance(0.5)

    (group,) = harness.delivered()
    assert isinstance(group, LogGroup)
    assert group.reason == "idle"
    assert [entry.package for entry in group] == [None, APP]


def test_running_hold_expired() -> None:
    """RUNNING hold_expired: a quiet device's intake opens without the uid map."""
    harness = Harness.make(where=Package(APP) | Tag("App"), package_hold=30.0)
    harness.connecting_spawned()
    harness.until_deadline()  # connect_timeout: a quiet device
    harness.answer("state", STATE_DEVICE)
    assert harness.core.state is S.RUNNING
    assert not harness.core.intake_open
    assert harness.core.armed_timers() == {"hold"}

    harness.until_deadline()

    assert harness.core.state is S.RUNNING
    assert harness.core.intake_open
    assert harness.core.armed_timers() == frozenset()
    harness.stdout(line("x"))
    assert harness.messages() == ["x"]
    assert harness.core.stats().unattributed == 1
    # The lookup still runs; its answer fills the map for the later lines.
    harness.answer("lookup", LOOKUP_OK)
    harness.stdout(line("y"))
    delivered = harness.delivered()
    packages = [item.package for item in delivered if isinstance(item, LogEntry)]
    assert packages == [None, APP]


# -- RECONNECTING ----------------------------------------------------------------------


def test_reconnecting_backoff_due() -> None:
    """RECONNECTING backoff_due: CONNECTING; RunCommand(clock); the budget grows."""
    harness = _reconnecting()

    out = harness.until_deadline()

    assert harness.core.state is S.CONNECTING
    assert [cmd.kind for cmd in out if isinstance(cmd, RunCommand)] == ["clock"]
    assert harness.core.connection == 2
    assert harness.core._attempts == 1
    assert harness.core._delay == pytest.approx(2.0)
    assert harness.changes()[-1] == StateChange(S.RECONNECTING, S.CONNECTING, 2, None)


def test_reconnecting_intake_reopened_after_stall() -> None:
    """RECONNECTING after a stall: intake reopened -> CONNECTING, no attempt."""
    harness = Harness.make(buffer_entries=4, stall_timeout=1.0).running()
    harness.stdout(lines(3))
    harness.advance(1.0)  # stall: Kill(c)
    harness.end_connection(-9)
    assert harness.core.state is S.RECONNECTING
    assert harness.changes()[-1].error is None  # a stall resume is no error
    assert harness.core.armed_timers() == frozenset()  # no backoff: wait for intake

    harness.consume()

    assert harness.core.state is S.CONNECTING
    assert harness.core._attempts == 0
    assert harness.core.stats().reconnects == 1
    assert harness.core.stats().stall_resumes == 1


@pytest.mark.parametrize("flush", [None, 2.0], ids=["stop", "flush"])
def test_reconnecting_stop(flush: float | None) -> None:
    """RECONNECTING stop or stop(flush): STOPPED; cancel backoff; Finished."""
    harness = _reconnecting()

    out = harness.send(_stop(flush))

    assert kinds(out) == ["Deliver", "Finished"]
    assert harness.core.state is S.STOPPED
    assert harness.core.next_deadline() is None
    # Design A.20: no connection reads a marker here, so a flush cannot run.
    assert harness.core.stats().flush_failed is (flush is not None)


def test_stop_during_backoff_is_stopped() -> None:
    """F2-3: stop() while waiting to reconnect ends STOPPED and nothing respawns."""
    harness = Harness.make(reconnect=Reconnect()).running()
    harness.end_connection(1)

    harness.send(StopRequest())
    harness.advance(3600.0)

    assert harness.core.state is S.STOPPED
    assert harness.core.outcome is None
    assert list(harness.spawns) == [1]
    assert harness.finished == [Finished(None)]


def test_reconnecting_kill() -> None:
    """RECONNECTING kill: KILLED; DiscardUndelivered; Finished."""
    harness = _reconnecting()

    out = harness.send(KillRequest())

    assert kinds(out) == ["DiscardUndelivered", "Deliver", "Finished"]
    assert harness.core.state is S.KILLED


@pytest.mark.parametrize("stage", ["on_item", "on_event"])
def test_reconnecting_callback_failed(stage: str) -> None:
    """RECONNECTING callback_failed: FAILED; latch CallbackError; Finished."""
    harness = _reconnecting()

    out = harness.send(_failed(stage))

    assert harness.core.state is S.FAILED
    assert isinstance(harness.core.outcome, CallbackError)
    assert "DiscardUndelivered" in kinds(out)
    assert kinds(out)[-1] == "Finished"


def test_reconnecting_consumer_detached() -> None:
    """RECONNECTING consumer_detached: STOPPED into the discarding outbox; Finished."""
    harness = _reconnecting()

    harness.detach()

    assert harness.core.state is S.STOPPED


@pytest.mark.parametrize("how", ["lookup", "hold"])
def test_reconnecting_lookup_hold_idle(how: str) -> None:
    """RECONNECTING lookup done, hold_expired: as in RUNNING; no group is open."""
    harness = Harness.make(
        where=Package(APP), reconnect=Reconnect(delay=30.0), package_hold=5.0
    )
    harness.connecting_spawned()
    harness.end_connection(1)
    assert harness.core.state is S.RECONNECTING
    assert harness.core.armed_timers() == {"backoff", "hold"}
    assert not harness.core.intake_open

    if how == "lookup":
        harness.answer("lookup", LOOKUP_FAILED)
        warnings = [cmd for cmd in harness.diagnostics if "lookup" in cmd.message]
        assert len(warnings) == 1
    else:
        harness.until_deadline()  # the hold expires before the backoff

    assert harness.core.state is S.RECONNECTING
    assert harness.core.intake_open
    assert harness.core.armed_timers() == {"backoff"}


# -- STOPPING --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [Mode.STOP, Mode.FLUSH])
def test_stopping_stop_or_flush_data(mode: Mode) -> None:
    """STOPPING(stop or flush) data: pipeline and deliver adb's last output."""
    harness = _stopping(mode)

    harness.stdout(lines(2))

    assert harness.messages() == ["first", "m0", "m1"]


def _stopping_with_lookup(mode: Mode) -> Harness:
    """STOPPING(stop or flush) with a live child while the first lookup runs."""
    harness = Harness.make(
        where=Package(APP) | Tag("App"), package_hold=30.0, stop_grace=60.0
    )
    harness.connecting_spawned()
    if mode is Mode.STOP:
        harness.send(StopRequest())
    else:
        harness.until_deadline()  # connect_timeout: a quiet device
        harness.answer("state", STATE_DEVICE)
        assert harness.core.state is S.RUNNING
        harness.send(StopRequest(flush=5.0))
    assert harness.core.mode is mode
    assert not harness.core.intake_open
    assert "hold" in harness.core.armed_timers()
    return harness


@pytest.mark.parametrize("how", ["lookup", "hold"])
@pytest.mark.parametrize("mode", [Mode.STOP, Mode.FLUSH])
def test_stopping_stop_or_flush_lookup_or_hold(mode: Mode, how: str) -> None:
    """STOPPING(stop or flush) lookup done, hold_expired: intake opens; lines go on."""
    harness = _stopping_with_lookup(mode)

    if how == "lookup":
        harness.answer("lookup", LOOKUP_OK)
    else:
        harness.send(Tick(), at=harness.core._attribution.deadline())

    assert harness.core.mode is mode
    assert harness.core.intake_open
    assert "hold" not in harness.core.armed_timers()
    if mode is Mode.FLUSH:
        # The flush timer counts from here, with intake open (design 2.3).
        assert harness.core.next_deadline() == pytest.approx(harness.now + 5.0)
    harness.stdout(line("last"))
    (entry,) = harness.delivered()
    assert isinstance(entry, LogEntry)
    assert entry.package == (APP if how == "lookup" else None)


@pytest.mark.parametrize("mode", [Mode.STOP, Mode.FLUSH])
def test_stopping_stop_or_flush_idle_flush(mode: Mode) -> None:
    """STOPPING(stop or flush) idle_flush: the idle group is delivered."""
    harness = Harness.make(
        group_by=GroupBy(keys=(), gap_ms=10_000.0, idle_flush_ms=100.0)
    ).running()
    harness.send(StopRequest(flush=5.0 if mode is Mode.FLUSH else None))

    harness.advance(0.1)

    (group,) = harness.delivered()
    assert isinstance(group, LogGroup)
    assert group.reason == "idle"


def test_stopping_flush_marker_read() -> None:
    """STOPPING(flush) marker read: STOPPING(stop); Terminate(c); marker filtered."""
    harness = _flushing()
    harness.answer("flush_marker", MARKER_OK)

    out = harness.stdout(line("last") + _flush_marker(harness) + line("after"))

    assert kinds(out) == ["Terminate", "Deliver"]
    assert harness.core.mode is Mode.STOP
    assert harness.messages() == ["first", "last", "after"]
    assert harness.core.stats().filtered == 1
    assert harness.core.armed_timers() == {"stop_grace"}
    harness.end_connection(0)
    assert harness.core.state is S.STOPPED
    assert harness.core.stats().complete


def test_stopping_flush_timer() -> None:
    """STOPPING(flush) flush timer: STOPPING(stop); Terminate(c); flush_timeout."""
    harness = _flushing()

    out = harness.advance(5.0)

    assert kinds(out) == ["Terminate"]
    assert harness.core.mode is Mode.STOP
    assert harness.core.stats().flush_timeout
    # A late marker is still filtered.
    harness.stdout(_flush_marker(harness))
    assert harness.messages() == ["first"]


def test_stopping_flush_marker_failed() -> None:
    """STOPPING(flush) marker failed: STOPPING(stop); Terminate(c); flush_failed."""
    harness = _flushing()

    out = harness.answer("flush_marker", MARKER_FAILED)

    assert kinds(out) == ["Terminate"]
    assert harness.core.mode is Mode.STOP
    assert harness.core.stats().flush_failed


def test_stopping_flush_stall() -> None:
    """STOPPING(flush) stall: STOPPING(stop); Terminate(c); flush_failed; no resume."""
    harness = _flushing(buffer_entries=3, stall_timeout=1.0)
    harness.stdout(lines(2))
    # Intake is closed: the flush timer pauses, the stall timer runs.
    assert harness.core.armed_timers() == {"stall"}

    out = harness.advance(1.0)

    assert kinds(out) == ["Terminate"]
    assert harness.core.mode is Mode.STOP
    assert harness.core.stats().flush_failed
    assert harness.core.stats().stall_resumes == 0


def test_stopping_flush_consumer_detached() -> None:
    """STOPPING(flush) consumer_detached: STOPPING(stop); Terminate(c)."""
    harness = _flushing()

    out = harness.detach()

    assert kinds(out) == ["Terminate"]
    assert harness.core.mode is Mode.STOP


def test_stopping_stop_consumer_detached() -> None:
    """STOPPING(stop) consumer_detached: same state; the outbox discards from now on."""
    harness = _stopping(Mode.STOP)

    assert kinds(harness.detach()) == []
    harness.stdout(lines(2))

    assert harness.core.mode is Mode.STOP
    # "first", waiting when the consumer left, and the two lines read after.
    assert harness.core.stats().undelivered == 3


@pytest.mark.parametrize("mode", [Mode.KILL, Mode.FAIL])
def test_stopping_kill_or_fail_data(mode: Mode) -> None:
    """STOPPING(kill or fail) data: dropped, counted in undelivered."""
    harness = _stopping(mode)

    harness.stdout(lines(3))

    assert harness.messages() == ["first"]
    # "first", discarded from the outbox by the request, and the three dropped.
    assert harness.core.stats().undelivered == 4
    assert harness.core.stats().lines == 4
    warnings = [cmd for cmd in harness.diagnostics if "discarding" in cmd.message]
    assert len(warnings) == 1


@pytest.mark.parametrize("how", ["kill", "on_item", "where"])
def test_stopping_kill_or_fail_intake_open(how: str) -> None:
    """A.20 CORRECTION: intake stays open in STOPPING(kill or fail).

    With a full outbox and no report from the consumer, a driver that hands
    over Data and Eof only while intake is open still sees the killed adb end,
    so the run ends instead of waiting forever.
    """

    def where(entry: LogEntry) -> bool:
        if entry.message == "bad":
            raise ValueError(entry.message)
        return True

    harness = Harness.make(buffer_entries=3, stall_timeout=None, where=Where(where))
    harness.running()
    if how == "where":
        harness.stdout(lines(2) + line("bad"))  # full, then where raises
    else:
        harness.stdout(lines(2))  # full
        assert not harness.core.intake_open
        event = KillRequest() if how == "kill" else _failed("on_item")
        out = harness.core.handle(event, harness.now)
        # Before the driver carried out DiscardUndelivered or reported anything.
        assert harness.core.intake_open
        harness.run(out)
    assert harness.core.mode is (Mode.KILL if how == "kill" else Mode.FAIL)
    assert harness.core.intake_open

    harness.exited(-9)
    assert harness.core.intake_open
    harness.stdout(lines(2, "late"))
    harness.eof()

    assert harness.core.state is (S.KILLED if how == "kill" else S.FAILED)
    assert harness.finished == [Finished(harness.core.outcome)]


@pytest.mark.parametrize("event", ["lookup", "hold", "idle", "detached"])
@pytest.mark.parametrize("mode", [Mode.KILL, Mode.FAIL])
def test_stopping_kill_or_fail_lookup_hold_idle_detached(
    mode: Mode, event: str
) -> None:
    """STOPPING(kill or fail): held lines and open groups are discarded and counted."""
    harness = Harness.make(
        where=Package(APP) | Tag("App"),
        group_by=GroupBy(
            keys=(), gap_ms=10_000.0, idle_flush_ms=100.0 if event == "idle" else None
        ),
        package_hold=1.0 if event == "lookup" else 60.0,
    )
    harness.connecting_spawned()
    if event == "lookup":
        harness.advance(1.0)  # the hold expires; the lookup is still running
    elif event != "hold":
        harness.answer("lookup", LOOKUP_OK)
    # For "hold" the lines arrive while intake is closed; a driver would wait,
    # but the core's accounting must hold either way.
    harness.stdout(lines(2))  # two entries in one open group
    harness.send(KillRequest() if mode is Mode.KILL else _failed())
    grouper = harness.core._grouper
    assert grouper is not None
    assert grouper.open_entries == 2

    match event:
        case "lookup":
            harness.answer("lookup", LOOKUP_OK)
        case "hold":
            assert harness.core.armed_timers() == {"hold"}
            harness.until_deadline()
        case "idle":
            harness.advance(0.1)
        case _:
            harness.detach()

    assert grouper.open_entries == 0
    assert harness.delivered() == []
    assert harness.core.stats().undelivered == 2
    harness.end_connection(-9)
    assert harness.core.state is (S.KILLED if mode is Mode.KILL else S.FAILED)


@pytest.mark.parametrize("mode", list(Mode))
def test_stopping_exited_eof(mode: Mode) -> None:
    """STOPPING(any) exited, eof: as in RUNNING (exit_drain)."""
    harness = _stopping(mode)

    harness.exited(0)

    assert "exit_drain" in harness.core.armed_timers()
    harness.eof()
    assert harness.core.state in {S.STOPPED, S.KILLED, S.FAILED}


@pytest.mark.parametrize("mode", list(Mode))
def test_stopping_exit_drain(mode: Mode) -> None:
    """STOPPING(any) exited, exit_drain expired: the run ends (G4)."""
    harness = _stopping(mode, exit_drain=0.5, stop_grace=60.0)
    harness.exited(0)
    harness.eof("stderr")  # stdout stays open: a process adb started holds it
    assert harness.core.state is S.STOPPING

    harness.advance(0.5)

    expected = {
        Mode.STOP: S.STOPPED,
        Mode.FLUSH: S.STOPPED,
        Mode.KILL: S.KILLED,
        Mode.FAIL: S.FAILED,
    }[mode]
    assert harness.core.state is expected
    assert harness.finished == [Finished(harness.core.outcome)]
    # STOPPING(flush) before its marker: flush_failed.
    assert harness.core.stats().flush_failed is (mode is Mode.FLUSH)
    (warning,) = [cmd for cmd in harness.diagnostics if "exit_drain" in cmd.message]
    assert "its stdout pipe stayed open" in warning.message


@pytest.mark.parametrize("mode", list(Mode))
def test_stopping_stop_grace(mode: Mode) -> None:
    """STOPPING(any) stop_grace: Kill(c) (F2-10, both drivers)."""
    harness = Harness.make(stop_grace=2.0).running()
    harness.send(StopRequest())  # Terminate arms stop_grace
    if mode is Mode.KILL:
        harness.send(KillRequest())
    elif mode is Mode.FAIL:
        harness.send(_failed())
    elif mode is Mode.FLUSH:
        harness = Harness.make(stop_grace=2.0).running()
        harness.send(StopRequest(flush=1.0))
        harness.advance(1.0)  # flush timeout: Terminate, stop_grace armed

    out = harness.advance(2.0)

    assert kinds(out) == ["Kill"]
    assert "stop_grace" not in harness.core.armed_timers()


@pytest.mark.parametrize("mode", list(Mode))
def test_stopping_kill(mode: Mode) -> None:
    """STOPPING(any) kill: STOPPING(kill); Kill(c); DiscardUndelivered; latch kept."""
    harness = _stopping(mode)
    latched = harness.core.outcome

    out = harness.send(KillRequest())

    assert kinds(out) == ["Kill", "DiscardUndelivered"]
    assert harness.core.mode is Mode.KILL
    assert harness.core.outcome is latched
    harness.end_connection(-9)
    assert harness.core.state is (S.FAILED if mode is Mode.FAIL else S.KILLED)


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("flush", [None, 1.0], ids=["stop", "flush"])
def test_stopping_stop(mode: Mode, flush: float | None) -> None:
    """STOPPING(any) stop, stop(flush): none."""
    harness = _stopping(mode)

    assert harness.send(_stop(flush)) == []
    assert harness.core.mode is mode


@pytest.mark.parametrize("mode", list(Mode))
def test_stopping_callback_failed(mode: Mode) -> None:
    """STOPPING(any) callback_failed: STOPPING(fail); latch if none yet; Kill."""
    harness = _stopping(mode)
    latched = harness.core.outcome

    out = harness.send(_failed("on_event"))

    assert harness.core.mode is Mode.FAIL
    assert "Kill" in kinds(out)
    if latched is None:
        assert isinstance(harness.core.outcome, CallbackError)
    else:
        assert harness.core.outcome is latched
    harness.end_connection(-9)
    assert harness.core.state is S.FAILED


@pytest.mark.parametrize("request_", ["stop", "kill", "fail"])
def test_stopping_spawned(request_: str) -> None:
    """STOPPING(any) spawned(c): Terminate (stop, flush) or Kill (kill, fail)."""
    harness = Harness.make()
    harness.start()
    harness.answer("clock", CLOCK_OK)  # Spawn issued
    event = {"stop": StopRequest(), "kill": KillRequest(), "fail": _failed()}[request_]
    harness.send(event)
    assert harness.core.state is S.STOPPING

    out = harness.spawned()

    assert kinds(out) == (["Terminate"] if request_ == "stop" else ["Kill"])


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("end", ["ended", "spawn_failed"])
def test_stopping_connection_ended(mode: Mode, end: str) -> None:
    """STOPPING(any) connection ended or spawn_failed: the terminal state of G4."""
    harness = Harness.make(group_by=GroupBy(keys=(), gap_ms=60_000.0))
    if end == "spawn_failed":
        harness.start()
        harness.answer("clock", CLOCK_OK)
        harness.send(
            {
                Mode.STOP: StopRequest(),
                Mode.FLUSH: StopRequest(),  # stop(flush) in CONNECTING is a stop
                Mode.KILL: KillRequest(),
                Mode.FAIL: _failed(),
            }[mode]
        )
        harness.spawn_failed()
        flush_failed = False
    else:
        harness.running()
        harness.send(
            {
                Mode.STOP: StopRequest(),
                Mode.FLUSH: StopRequest(flush=5.0),
                Mode.KILL: KillRequest(),
                Mode.FAIL: _failed(),
            }[mode]
        )
        harness.end_connection(0)
        flush_failed = mode is Mode.FLUSH  # the connection ended before the marker

    expected = {
        Mode.STOP: S.STOPPED,
        Mode.FLUSH: S.STOPPED,
        Mode.KILL: S.KILLED,
        Mode.FAIL: S.FAILED,
    }[mode]
    assert harness.core.state is expected
    assert harness.core.stats().flush_failed is flush_failed
    assert harness.finished == [Finished(harness.core.outcome)]
    groups = [item for item in harness.delivered() if isinstance(item, LogGroup)]
    if end == "ended" and mode in {Mode.STOP, Mode.FLUSH}:
        assert [group.reason for group in groups] == ["end"]
    else:
        assert groups == []


# -- terminal --------------------------------------------------------------------------


def test_terminal_consumer_accounting() -> None:
    """terminal consumer_progress, consumer_detached: outbox accounting only (G2)."""
    harness = Harness.make().running()
    harness.stdout(lines(2))
    harness.send(StopRequest())
    harness.end_connection(0)
    assert harness.core.stats().delivered == 0

    assert harness.send(ConsumerProgress(harness.outbox.counts())) == []
    harness.consume()
    assert harness.detach() == []

    assert harness.core.stats().delivered == 3
    assert harness.core.state is S.STOPPED


@pytest.mark.parametrize(
    "event",
    [
        StopRequest(),
        KillRequest(),
        Tick(),
        Data(1, "stdout", b"x\n"),
        Eof(1, "stdout"),
        Exited(1, 0),
        Spawned(1, 7),
        CommandDone(1, CLOCK_OK),
        CallbackFailed("on_item", ValueError("late")),
    ],
    ids=["stop", "kill", "tick", "data", "eof", "exited", "spawned", "done", "failed"],
)
def test_terminal_other_events_ignored(event: object) -> None:
    """terminal any other event: unchanged, no commands (G2)."""
    harness = Harness.make().running()
    harness.send(KillRequest())
    harness.end_connection(-9)
    before = harness.core.stats()

    assert harness.send(event) == []  # ty: ignore[invalid-argument-type]
    assert harness.core.state is S.KILLED
    assert harness.core.stats() == before


# -- the state x event product (design 3.2: the table is total) ----------------

_CELLS = [(state, kind) for state in STATES for kind in KINDS]


def _classify(state: str, kind: str) -> str:
    if (state, kind) in IMPOSSIBLE:
        return "impossible"
    return "row" if kind in ROWS[state] else "ignored"


def _snapshot(harness: Harness) -> tuple[object, ...]:
    core = harness.core
    return (
        core.state,
        core.mode,
        core.stats(),
        core.armed_timers(),
        core.next_deadline(),
        core.intake_open,
        core.connection,
    )


@pytest.mark.parametrize(
    ("state", "kind"), _CELLS, ids=[f"{state}-{kind}" for state, kind in _CELLS]
)
def test_state_event_product(state: str, kind: str) -> None:
    """Every (state, event kind) is a row, an ignored cell, or an impossible one.

    The variants of a state cover the sub-contexts the events need. A row
    applies in some variant and is handled; an ignored event applies in none
    (a timer is never armed, a result has no pending request, an event names a
    retired connection) and changes nothing; an impossible one fails an
    assertion in the core.
    """
    expected = _classify(state, kind)
    applied = False
    for build in VARIANTS[state]:
        harness = build()
        assert state_key(harness) == state
        event, applies, at = stimulus(harness, kind)
        if expected == "impossible":
            if kind != "start" and harness.core.connection is None:
                continue
            with pytest.raises(AssertionError, match=r"cannot occur|start"):
                harness.core.handle(event, harness.now)
            applied = True
        elif expected == "row":
            if applies:
                harness.send(event, at=at)
                applied = True
        else:
            if kind not in CONTROL and kind not in CONSUMER:
                assert not applies, f"{kind} applies in {state}: it should be a row"
            before = _snapshot(harness)
            assert harness.send(event) == []
            assert _snapshot(harness) == before
            applied = True
    if (state, kind) in UNREACHABLE:
        assert not applied, f"{kind} became reachable in {state}"
    else:
        assert applied, f"no variant of {state} exercises {kind}"


def test_product_covers_the_ignored_lists() -> None:
    """The ignored cells include design 3.2's second table, cell for cell."""
    listed = {
        "CONNECTING": {
            "read_timeout", "stall", "stable_after", "flush_timer", "stop_grace",
            "backoff_due", "idle_flush", "flush_marker_done", "flush_marker_failed",
        },
        "RUNNING": {
            "clock_done", "clock_failed", "marker_done", "marker_failed",
            "state_device", "state_other", "connect_timeout", "backoff_due",
            "flush_timer", "stop_grace",
        },
        "RECONNECTING": {
            *CONNECTION, "read_timeout", "exit_drain", "stall", "stable_after",
            "connect_timeout", "flush_timer", "stop_grace", "clock_done",
            "clock_failed", "marker_done", "marker_failed", "state_device",
            "state_other",
        },
        **{
            f"STOPPING({mode})": {
                "clock_done", "clock_failed", "marker_done", "marker_failed",
                "state_device", "state_other", "connect_timeout", "backoff_due",
                "stable_after", "read_timeout",
            }
            | ({"stall"} if mode != "flush" else set())
            for mode in ("stop", "flush", "kill", "fail")
        },
    }  # fmt: skip
    listed["IDLE"] = {*CONNECTION, *RESULTS, *CONSUMER, *TIMERS}
    for state in ("STOPPED", "KILLED", "FAILED"):
        listed[state] = set(KINDS) - {"consumer_progress", "consumer_detached"}
    for state, kinds_ in listed.items():
        assert {kind for kind in kinds_ if _classify(state, kind) != "ignored"} == set()


# -- global rules and the named invariant tests -----------------------------------


def test_retired_connection_events_are_ignored() -> None:
    """G1: every event of a retired connection is ignored, in every later state."""
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running()
    harness.end_connection(1)
    harness.until_deadline()
    harness.answer("clock", CLOCK_OK)
    harness.spawned(5)
    before = harness.core.stats()

    for event in (
        Data(1, "stdout", lines(3)),
        Eof(1, "stdout"),
        Exited(1, 0),
        Spawned(1, 9),
    ):
        assert harness.send(event) == []

    assert harness.core.stats() == before
    assert harness.core.connection == 2


def test_state_changes_are_ordered_with_items() -> None:
    """G3, design 3.6: RECONNECTING comes after the dead connection's last entry."""
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running()
    harness.stdout(line("last of 1"))
    harness.end_connection(1)
    harness.until_deadline()
    harness.answer("clock", CLOCK_OK)
    harness.spawned()
    harness.stdout(line("first of 2"))

    harness.consume()

    sequence = [
        record.message if isinstance(record, LogEntry) else record.new
        for record in harness.taken
        if isinstance(record, LogEntry | StateChange)
    ]
    assert sequence == [
        S.CONNECTING,
        S.RUNNING,
        "first",
        "last of 1",
        S.RECONNECTING,
        S.CONNECTING,
        S.RUNNING,
        "first of 2",
    ]


def test_stop_then_callback_failure_ends_failed() -> None:
    """G4: a stop request is intent; a later callback failure still ends FAILED."""
    harness = Harness.make().running()
    harness.send(StopRequest())

    harness.send(_failed("on_item"))
    harness.send(_failed("on_event"))  # a second failure does not replace the first
    harness.end_connection(0)

    outcome = harness.core.outcome
    assert isinstance(outcome, CallbackError)
    assert outcome.stage == "on_item"
    assert harness.core.state is S.FAILED
    assert harness.changes()[-1].error is outcome
    assert outcome.stats == harness.core.stats()


def test_kill_after_stop_ends_killed_and_counts() -> None:
    """4.3: a kill after a stop yields KILLED, and the discards are counted."""
    harness = Harness.make().running()
    harness.stdout(lines(4))
    harness.send(StopRequest())

    harness.send(KillRequest())
    harness.end_connection(-9)

    stats = harness.core.stats()
    assert harness.core.state is S.KILLED
    assert stats.undelivered == 5
    assert set(stats.incomplete) == {"undelivered", "killed"}


def _discard_warnings(harness: Harness) -> list[str]:
    return [cmd.message for cmd in harness.diagnostics if "discarding" in cmd.message]


@pytest.mark.parametrize("request_", ["kill", "fail", "detach"])
def test_no_discard_warning_after_the_consumer_caught_up(request_: str) -> None:
    """A.20: one WARNING only when an item is actually discarded.

    The consumer took every item; its progress report waits in the control
    inbox behind the request (posted after it). The inbox hands the progress
    over first, and a detach carries the outbox counts, so the core sees that
    nothing waits.
    """
    harness = Harness.make().running()
    harness.stdout(lines(3))
    while harness.outbox.pop() is not None:
        pass
    inbox = ControlInbox()
    if request_ == "detach":
        harness.outbox.detach()
        inbox.post(ConsumerDetached(harness.outbox.counts()))
    else:
        inbox.post(KillRequest() if request_ == "kill" else _failed("on_item"))
    inbox.post(ConsumerProgress(harness.outbox.counts()))
    harness.reported = harness.outbox.counts()

    while (event := inbox.take()) is not None:
        harness.send(event)
    harness.end_connection(-9)

    assert _discard_warnings(harness) == []
    stats = harness.core.stats()
    assert (stats.delivered, stats.undelivered) == (4, 0)


@pytest.mark.parametrize("when", ["live", "after-finished"])
def test_detach_counts_reach_stats(when: str) -> None:
    """3.4 break: the entries a detach discards are counted from its own counts.

    live: no later report is needed, and the WARNING follows what the detach
    discarded. after-finished: the run ended STOPPED with entries queued; G2
    allows no command then, so they are counted without a WARNING.
    """
    harness = Harness.make().running()
    harness.stdout(lines(4))
    if when == "after-finished":
        harness.send(StopRequest())
        harness.end_connection(0)
        assert harness.core.stats().complete

    out = harness.detach()

    stats = harness.core.stats()
    assert stats.undelivered == 5
    assert "undelivered" in stats.incomplete
    if when == "live":
        assert _discard_warnings(harness) == [
            "discarding undelivered items (the consumer detached); they are "
            "counted in stats.undelivered"
        ]
    else:
        assert out == []
        assert harness.core.state is S.STOPPED
        assert _discard_warnings(harness) == []


def _reconnecting_with_six_queued() -> Harness:
    harness = Harness.make(reconnect=Reconnect(delay=5.0), stall_timeout=None)
    harness.running()
    harness.stdout(lines(5))
    harness.end_connection(1)
    assert harness.core.state is S.RECONNECTING
    assert harness.outbox.entries == 6
    return harness


@pytest.mark.parametrize("request_", ["kill", "on_item"])
def test_end_stats_count_last_discard(request_: str) -> None:
    """A discard in the batch that ends the run is counted when Finished is.

    The driver reports the outbox counts before it carries out Finished, so a
    ``join()`` that returns then already sees the discarded entries, in
    ``stats`` and in the outcome's stats.
    """
    harness = _reconnecting_with_six_queued()

    harness.send(KillRequest() if request_ == "kill" else _failed("on_item"))

    assert harness.at_finished is not None
    stats, outcome_stats = harness.at_finished
    assert stats.undelivered == 6
    assert "undelivered" in stats.incomplete
    assert outcome_stats == (None if request_ == "kill" else stats)
    assert stats == harness.core.stats()


@pytest.mark.parametrize("flush", [None, 1.0])
def test_end_stats_count_waiting_detach(flush: float | None) -> None:
    """A stop and a detach wait together in RECONNECTING; the stop ends the run.

    The inbox hands the detach's counts over first, so what it discarded is
    counted, with its WARNING, before Finished; the detach itself comes after
    the stop, so a raced ``stop(flush=...)`` still sets ``flush_failed``.
    """
    harness = _reconnecting_with_six_queued()
    inbox = ControlInbox()
    inbox.post(StopRequest(flush=flush))
    harness.outbox.detach()
    inbox.post(ConsumerDetached(harness.outbox.counts()))
    harness.reported = harness.outbox.counts()

    while (event := inbox.take()) is not None:
        harness.send(event)

    assert harness.core.state is S.STOPPED
    assert harness.at_finished is not None
    stats = harness.at_finished[0]
    assert stats.undelivered == 6
    reasons = {"undelivered", "flush_failed"} if flush else {"undelivered"}
    assert set(stats.incomplete) == reasons
    assert _discard_warnings(harness) == [
        "discarding undelivered items (the consumer detached); they are "
        "counted in stats.undelivered"
    ]


def test_late_command_result_is_ignored() -> None:
    """G5: a state check that returns after the first output changes nothing."""
    harness = _connecting_with_child()
    harness.until_deadline()  # connect_timeout: the state check runs
    harness.stdout(line("output"))  # RUNNING before the check returned
    before = harness.core.stats()

    assert harness.answer("state", STATE_OFFLINE) == []

    assert harness.core.state is S.RUNNING
    assert harness.core.stats() == before


def test_intake_closes_when_full_and_reopens_at_half() -> None:
    """G6: admission by entries and by bytes; low-water mark at half of both."""
    harness = Harness.make(buffer_entries=10, buffer_bytes=10_000_000).running()
    harness.stdout(lines(8))  # 9 entries
    assert harness.core.intake_open
    harness.stdout(lines(1))  # 10: full
    assert not harness.core.intake_open

    harness.consume(4)  # 6 left
    assert not harness.core.intake_open
    harness.consume(1)  # 5 left: half
    assert harness.core.intake_open

    entry_size = len(line("m0"))
    by_bytes = Harness.make(buffer_bytes=3 * entry_size).running(line("m0"))
    by_bytes.stdout(lines(2))
    assert not by_bytes.core.intake_open
    by_bytes.consume(2)
    assert by_bytes.core.intake_open


def test_backpressure_holds_through_stopping() -> None:
    """3.5: while a consumer is attached, STOPPING(stop) keeps every item."""
    harness = Harness.make(buffer_entries=2, exit_drain=1.0).running()
    harness.stdout(lines(1))
    harness.send(StopRequest())
    harness.exited(0)
    harness.advance(60.0)  # exit_drain paused: intake is closed
    assert harness.core.state is S.STOPPING

    harness.consume()
    harness.stdout(lines(3, "tail"))
    harness.eof()

    assert harness.core.state is S.STOPPED
    harness.consume()
    assert harness.messages() == ["first", "m0", "tail0", "tail1", "tail2"]
    assert harness.core.stats().undelivered == 0


def test_where_rejection_and_dividers_are_filtered() -> None:
    """``where`` runs in the core; rejected lines and dividers count as filtered."""
    harness = Harness.make(where=Tag("App")).running()

    harness.stdout(
        b"--------- beginning of main\n" + line("kept") + line("other", tag="Other")
    )

    assert harness.messages() == ["first", "kept"]
    assert harness.core.stats().filtered == 2


def test_unparsed_and_truncated_lines_are_counted() -> None:
    """An unreadable line becomes a fallback entry; a long line is cut."""
    harness = Harness.make(max_line_bytes=4096).running()

    harness.stdout(b"garbage\n" + line("y" * 5000))

    delivered = harness.delivered()
    assert [entry.parsed for entry in delivered if isinstance(entry, LogEntry)] == [
        True,
        False,
        True,
    ]
    stats = harness.core.stats()
    assert (stats.unparsed, stats.truncated) == (1, 1)


def test_stderr_is_rate_limited() -> None:
    """5.9: at most 10 stderr lines per second are logged; the rest is counted."""
    harness = Harness.make().running()

    out = harness.stderr(b"e\n" * 25)
    later = harness.advance(1.0) + harness.stderr(b"f\n")

    assert len([cmd for cmd in out if isinstance(cmd, Diagnostic)]) == 10
    messages = [cmd.message for cmd in later if isinstance(cmd, Diagnostic)]
    assert messages == ["15 more adb stderr lines were not logged (rate limit)", "f"]
    assert harness.core.stats().stderr_lines == 26


def test_stderr_count_is_logged_when_its_window_ends() -> None:
    """5.9: the suppressed count is logged one second after the burst began,
    without waiting for another stderr line or the end of the run."""
    harness = Harness.make().running()
    harness.stderr(b"e\n" * 50)
    assert harness.core.next_deadline() == pytest.approx(harness.now + 1.0)

    out = harness.until_deadline()

    messages = [cmd.message for cmd in out if isinstance(cmd, Diagnostic)]
    assert messages == ["40 more adb stderr lines were not logged (rate limit)"]
    assert harness.core.next_deadline() is None
    harness.send(StopRequest())
    assert not [cmd for cmd in harness.end_connection(0) if isinstance(cmd, Diagnostic)]


def test_group_by_larger_than_the_outbox_is_rejected() -> None:
    """2.8: max_group_size must fit buffer_entries, checked at construction."""
    with pytest.raises(ValueError, match="max_group_size"):
        StreamCore(
            config(buffer_entries=10),
            adb_path="adb",
            group_by=GroupBy(keys=(), gap_ms=1.0, max_group_size=11),
        )


_SEEDS = range(200)


@pytest.mark.parametrize("seed", _SEEDS, ids=[f"seed{seed}" for seed in _SEEDS])
def test_line_conservation_invariant(seed: int) -> None:
    """F1-5: seeded random event sequences keep every rule of the table.

    Only table transitions, in one unbroken chain; exactly one Finished, last;
    no command for a retired connection; the outcome latched once; nothing after
    the terminal state; and every stdout line delivered, filtered, discarded
    with a counted reason or held (checked after every event by the harness).
    At the end the counters balance from the stats alone.
    """
    driver = RandomDriver(seed)

    driver.run(steps=250)

    harness = driver.harness
    assert harness.core.state in {S.STOPPED, S.KILLED, S.FAILED}
    assert len(harness.finished) == 1
    assert driver.changes[-1].new is harness.core.state
    stats = harness.core.stats()
    assert stats.lines == (
        stats.delivered
        + stats.undelivered
        + stats.filtered
        + stats.before_start
        + stats.deduplicated
        + stats.below_cursor
    )
    assert harness.core.next_deadline() is None
    assert harness.send(Tick(), at=harness.now + 10.0) == []


def test_random_runs_reach_the_main_paths() -> None:
    """The seeds above do not mostly end early: they reach the main paths.

    The bounds are about half of what the seeds reach today, so a change of
    the core that shifts the trajectories a little does not fail this test,
    but one that makes most runs end at once does.
    """
    reach: Counter[str] = Counter()
    for seed in _SEEDS:
        driver = RandomDriver(seed)
        driver.run(steps=250)
        core = driver.harness.core
        stats = core.stats()
        commands = driver.harness.commands
        reach["read lines"] += stats.lines > 0
        reach["reconnected"] += stats.reconnects > 0
        reach["stall resumed"] += stats.stall_resumes > 0
        reach["flushed"] += any(
            isinstance(cmd, RunCommand) and cmd.kind == "flush_marker"
            for cmd in commands
        )
        reach[core.state.value] += 1

    assert reach["read lines"] >= 0.8 * len(_SEEDS)
    floors = {"reconnected": 20, "stall resumed": 10, "flushed": 10}
    floors |= {"STOPPED": 20, "KILLED": 15, "FAILED": 40}
    assert {name: reach[name] for name in floors} == {
        name: max(reach[name], floor) for name, floor in floors.items()
    }


def test_flush_timer_counts_only_open_intake() -> None:
    """2.3: a slow consumer causes no spurious flush_timeout."""
    harness = Harness.make(buffer_entries=2, stall_timeout=None).running()
    harness.stdout(lines(1))  # full
    harness.send(StopRequest(flush=2.0))
    harness.answer("flush_marker", MARKER_OK)

    harness.advance(30.0)  # intake closed all along: the flush timer is paused
    assert harness.core.mode is Mode.FLUSH
    harness.consume()
    harness.advance(1.5)
    harness.stdout(_flush_marker(harness))

    assert harness.core.mode is Mode.STOP
    assert not harness.core.stats().flush_timeout


def test_since_now_writes_a_new_marker_until_one_is_read() -> None:
    """since="now": an attempt that ends before its marker was read starts over."""
    harness = Harness.make(since="now", reconnect=Reconnect(delay=1.0))
    harness.connecting_spawned()
    first = harness.core._conn
    assert first is not None
    assert first.marker == f"start-{1:032x}"
    harness.end_connection(1)
    harness.until_deadline()

    harness.answer("clock", CLOCK_OK)
    harness.answer("start_marker", MARKER_OK)
    harness.spawned()
    second = harness.core._conn
    assert second is not None
    assert second.marker == f"start-{2:032x}"
    harness.stdout(line(f"start-{1:032x}", tag=_MARKER_TAG) + line("between"))
    assert harness.core.state is S.CONNECTING
    harness.stdout(line(f"start-{2:032x}", tag=_MARKER_TAG) + line("live"))

    assert harness.core.state is S.RUNNING
    assert harness.messages() == ["live"]
    assert harness.core.stats().before_start == 3


def test_read_deadline_is_suspended_during_the_first_lookup() -> None:
    """5.7: while intake is closed for the first lookup, no read timeout runs."""
    harness = Harness.make(where=Package(APP), read_timeout=1.0, package_hold=30.0)
    harness.connecting_spawned()
    harness.until_deadline()  # connect_timeout: a quiet device
    harness.answer("state", STATE_DEVICE)
    assert harness.core.state is S.RUNNING
    assert harness.core.armed_timers() == {"hold"}
    harness.advance(10.0)

    harness.answer("lookup", LOOKUP_OK)

    assert harness.core.intake_open
    assert harness.core.armed_timers() == {"read"}
    assert harness.core.next_deadline() == pytest.approx(harness.now + 1.0)


def test_since_now_reconnect_after_the_marker_writes_no_marker() -> None:
    """since="now": once the start marker was read, a reconnect writes none."""
    harness = Harness.make(since="now", reconnect=Reconnect(delay=1.0))
    harness.running()  # the marker, then "first"
    harness.end_connection(1)
    harness.until_deadline()

    out = harness.answer("clock", CLOCK_OK)

    (spawn,) = out
    assert isinstance(spawn, Spawn)
    assert spawn.argv[spawn.argv.index("-T") + 1] == f"{CLOCK - 1}.000000"
    harness.spawned()
    harness.stdout(line("again"))
    assert harness.core.state is S.RUNNING
    assert harness.messages() == ["first", "again"]
    assert harness.core.stats().before_start == 1


def test_since_now_reconnect_after_only_the_marker_keeps_the_start() -> None:
    """since="now": a connection that read only the marker still fixes the start.

    The next attempt must not start at its own, later clock: the lines logged
    between the marker and the reconnect would be lost without a Gap.
    """
    harness = Harness.make(since="now", reconnect=Reconnect(delay=1.0))
    harness.connecting_spawned()
    conn = harness.core._conn
    assert conn is not None
    assert conn.marker is not None
    harness.stdout(line(conn.marker, tag=_MARKER_TAG))
    assert harness.core.state is S.RUNNING
    harness.end_connection(1)
    harness.until_deadline()

    (spawn,) = harness.answer("clock", result(stdout=f"{CLOCK + 60}\n"))

    assert isinstance(spawn, Spawn)
    assert spawn.argv[spawn.argv.index("-T") + 1] == f"{CLOCK - 1}.000000"


def test_lines_before_the_start_marker_count_only_in_before_start() -> None:
    """2.5: before_start is every line up to and including the start marker.

    A divider, an unparsed or a cut line before the marker is not part of the
    capture, so it is not counted in filtered, unparsed or truncated, and a cut
    one does not make the capture incomplete.
    """
    harness = Harness.make(since="now", max_line_bytes=4096)
    harness.connecting_spawned()
    conn = harness.core._conn
    assert conn is not None
    assert conn.marker is not None

    harness.stdout(
        b"--------- beginning of main\n"
        + line("y" * 5000)
        + b"garbage\n"
        + line(conn.marker, tag=_MARKER_TAG)
        + line("live")
    )
    harness.send(StopRequest())
    harness.end_connection(0)

    assert harness.messages() == ["live"]
    stats = harness.core.stats()
    assert stats.before_start == 4
    assert (stats.filtered, stats.unparsed, stats.truncated) == (0, 0, 0)
    assert stats.incomplete == ()


@pytest.mark.parametrize("mode", [Mode.KILL, Mode.FAIL])
def test_before_start_while_kill_or_fail(mode: Mode) -> None:
    """A.20: lines before the start marker count only in before_start, also
    while a kill or a failure discards the rest: they are not undelivered, a
    cut one is not truncated, and no discard WARNING is logged for them."""
    harness = Harness.make(since="now", max_line_bytes=4096)
    harness.connecting_spawned()
    conn = harness.core._conn
    assert conn is not None
    assert conn.marker is not None
    harness.send(KillRequest() if mode is Mode.KILL else _failed())

    harness.stdout(
        b"--------- beginning of main\n" + line("old") + line("y" * 5000) + b"garbage\n"
    )
    harness.end_connection(-9)

    stats = harness.core.stats()
    assert stats.before_start == 4
    assert (stats.undelivered, stats.truncated, stats.filtered) == (0, 0, 0)
    assert stats.incomplete == (("killed",) if mode is Mode.KILL else ("failed",))
    assert not [cmd for cmd in harness.diagnostics if "discarding" in cmd.message]


def test_timers_fire_on_any_event_not_only_tick() -> None:
    """A busy stream that never sends Tick still fires its timers (G5)."""
    harness = _flushing()  # flush timer at +5 s

    harness.send(Data(1, "stdout", line("late")), at=harness.now + 6.0)

    assert harness.core.mode is Mode.STOP
    assert harness.core.stats().flush_timeout
    assert harness.messages() == ["first", "late"]


def test_due_timers_fire_earliest_first() -> None:
    """Timers due at the same handle() fire in deadline order.

    The flush timer (due at +1 s) fires before exit_drain (+2 s): the flush
    timed out, then the connection ended while stopping, so the reason is
    flush_timeout and not flush_failed.
    """
    harness = Harness.make(exit_drain=2.0).running()
    harness.send(StopRequest(flush=1.0))
    harness.exited(0)
    assert harness.core.armed_timers() == {"flush", "exit_drain"}

    harness.advance(3.0)

    assert harness.core.state is S.STOPPED
    stats = harness.core.stats()
    assert stats.flush_timeout
    assert not stats.flush_failed


@pytest.mark.parametrize("flush", [float("nan"), -1.0], ids=["nan", "negative"])
def test_stop_flush_without_a_valid_bound_times_out_at_once(flush: float) -> None:
    """stop(flush=nan or < 0) waits for nothing; next_deadline stays a number."""
    harness = Harness.make().running()

    harness.send(StopRequest(flush=flush))

    assert harness.core.mode is Mode.STOP
    assert harness.core.stats().flush_timeout
    deadline = harness.core.next_deadline()
    assert deadline == pytest.approx(harness.now + 2.0)  # stop_grace only


def test_stop_flush_infinite_waits_without_a_bound() -> None:
    """stop(flush=inf) arms no flush timer; the marker still ends the flush."""
    harness = Harness.make().running()

    harness.send(StopRequest(flush=float("inf")))

    assert harness.core.mode is Mode.FLUSH
    assert harness.core.armed_timers() == frozenset()
    assert harness.core.next_deadline() is None
    harness.answer("flush_marker", MARKER_OK)
    harness.stdout(_flush_marker(harness))
    assert harness.core.mode is Mode.STOP
    assert not harness.core.stats().flush_timeout


def test_finish_reports_the_suppressed_stderr_count() -> None:
    """5.9: the count of stderr lines over the limit is logged at the end too."""
    harness = Harness.make().running()
    harness.stderr(b"e\n" * 15)

    harness.send(StopRequest())
    out = harness.end_connection(0)

    messages = [cmd.message for cmd in out if isinstance(cmd, Diagnostic)]
    assert "5 more adb stderr lines were not logged (rate limit)" in messages
    assert harness.core.state is S.STOPPED


def test_outcome_stats_follow_the_consumer_after_the_end() -> None:
    """G2: terminal ConsumerProgress refreshes the latched outcome's stats."""

    def where(entry: LogEntry) -> bool:
        if entry.message == "bad":
            raise ValueError(entry.message)
        return True

    harness = Harness.make(where=Where(where)).running()
    harness.stdout(line("ok") + line("bad"))  # F3-1: "first" and "ok" are kept
    harness.end_connection(-9)
    outcome = harness.core.outcome
    assert isinstance(outcome, CallbackError)
    assert outcome.stats.delivered == 0

    harness.consume()

    assert outcome.stats.delivered == 2
    assert outcome.stats == harness.core.stats()


@pytest.mark.parametrize("order", ["stop-first", "end-first"])
def test_flush_and_connection_end_race_gives_one_verdict(order: str) -> None:
    """stop(flush) and a connection end: flush_failed in either order (A.20)."""
    harness = Harness.make(reconnect=Reconnect(delay=1.0)).running()

    if order == "stop-first":
        harness.send(StopRequest(flush=5.0))
        harness.end_connection(1)
    else:
        harness.end_connection(1)
        assert harness.core.state is S.RECONNECTING
        harness.send(StopRequest(flush=5.0))

    assert harness.core.state is S.STOPPED
    assert harness.core.stats().incomplete == ("flush_failed",)


@pytest.mark.parametrize("order", ["stall-first", "stop-first"])
def test_flush_and_stall_race_gives_one_verdict(order: str) -> None:
    """stop(flush) and a stall kill: flush_failed in either order (A.20)."""
    harness = Harness.make(buffer_entries=3, stall_timeout=1.0).running()
    harness.stdout(lines(2))  # full
    if order == "stop-first":
        harness.send(StopRequest(flush=5.0))
        harness.advance(1.0)  # stall while flushing
        harness.end_connection(-15)
    else:
        harness.advance(1.0)  # stall: Kill(c)
        harness.end_connection(-9)
        assert harness.core.state is S.RECONNECTING
        harness.send(StopRequest(flush=5.0))

    assert harness.core.state is S.STOPPED
    assert "flush_failed" in harness.core.stats().incomplete
