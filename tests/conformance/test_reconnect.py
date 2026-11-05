"""Reconnect conformance: the core's reconnect decision, carried out by the driver.

Design 3.2, 5.6, 5.7 and the F4-2 row of the coverage matrix, for every driver
(see ``conftest.py``). Until PR-14 a reconnect replays from the first
position (design A.20); these fakes give each connection its own lines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from logpyt import AdbExitError, LogEntry, ReadTimeoutError, Reconnect, StateChange
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, config
from tests.conftest import (
    FakeAdb,
    logcat_line,
    not_found,
    out,
    run,
    wait,
    wait_until,
    waiting_for_device,
)

if TYPE_CHECKING:
    from logpyt import StreamEvent

S = StreamState
SERIAL = "emulator-9999"


def _changes(events: list[StreamEvent]) -> list[StateChange]:
    return [event for event in events if isinstance(event, StateChange)]


def test_reconnect_after_a_nonzero_exit(driver: Driver, fake_adb: FakeAdb) -> None:
    """An unrequested end with reconnect configured is an event, not the outcome
    (D-5): RECONNECTING with the cause, a new clock read and a new adb.
    """
    fake_adb.logcat(
        run(out(logcat_line("a")), exit_code=1),
        run(out(logcat_line("b")), wait("never")),
    )
    entries: list[LogEntry] = []
    events: list[StreamEvent] = []
    stream = driver.open(
        config(reconnect=Reconnect(delay=0.01)),
        on_item=entries.append,
        on_event=events.append,
    )
    driver.start(stream)
    wait_until(lambda: len(entries) == 2, timeout=TIMEOUT)
    driver.stop(stream)
    driver.join(stream)

    assert [entry.message for entry in entries] == ["a", "b"]
    changes = _changes(events)
    assert [(c.old, c.new, c.connection) for c in changes] == [
        (S.IDLE, S.CONNECTING, 1),
        (S.CONNECTING, S.RUNNING, 1),
        (S.RUNNING, S.RECONNECTING, 1),
        (S.RECONNECTING, S.CONNECTING, 2),
        (S.CONNECTING, S.RUNNING, 2),
        (S.RUNNING, S.STOPPING, 2),
        (S.STOPPING, S.STOPPED, 2),
    ]
    cause = changes[2].error
    assert isinstance(cause, AdbExitError)
    assert cause.returncode == 1
    assert stream.outcome is None
    assert (stream.stats.connections, stream.stats.reconnects) == (2, 1)
    commands = [call["command"] for call in fake_adb.calls()]
    assert commands == ["date", "logcat", "date", "logcat"]
    assert fake_adb.survivors() == []


def test_read_timeout_without_reconnect_fails(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """A RUNNING adb silent for read_timeout is killed; without reconnect the
    outcome is ReadTimeoutError, which is no TimeoutError (D-3, design 5.7).
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    stream = driver.open(config(read_timeout=0.2))
    driver.start(stream)

    with pytest.raises(ReadTimeoutError) as info:
        driver.join(stream)

    assert not isinstance(info.value, TimeoutError)
    assert stream.state is S.FAILED
    assert fake_adb.survivors() == []


def test_timeout_kill_reconnects(driver: Driver, fake_adb: FakeAdb) -> None:
    """The kill of a read timeout is an unrequested end like any other: with
    reconnect it reconnects (F4-2).
    """
    fake_adb.logcat(
        run(out(logcat_line("a")), wait("never")),
        run(out(logcat_line("b")), wait("never")),
    )
    entries: list[LogEntry] = []
    events: list[StreamEvent] = []
    stream = driver.open(
        config(read_timeout=0.2, reconnect=Reconnect(delay=0.01)),
        on_item=entries.append,
        on_event=events.append,
    )
    driver.start(stream)
    wait_until(lambda: len(entries) >= 2, timeout=TIMEOUT)
    driver.stop(stream)
    driver.join(stream)

    assert [entry.message for entry in entries[:2]] == ["a", "b"]
    reconnecting = [c for c in _changes(events) if c.new is S.RECONNECTING]
    assert isinstance(reconnecting[0].error, ReadTimeoutError)
    assert reconnecting[0].connection == 1
    assert stream.state is S.STOPPED
    assert fake_adb.survivors() == []


def test_adb_waiting_without_output_ends_as_device_not_available(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """adb waits for an absent device instead of exiting; after connect_timeout
    the state check ends the attempt, which never was RUNNING (F4-2, design
    5.6, A-20).
    """
    fake_adb.logcat(waiting_for_device())
    fake_adb.get_state(not_found(SERIAL))
    entries: list[LogEntry] = []
    events: list[StreamEvent] = []
    stream = driver.open(
        config(serial=SERIAL, connect_timeout=0.2),
        on_item=entries.append,
        on_event=events.append,
    )
    driver.start(stream)

    with pytest.raises(AdbExitError, match="device not available: not found") as info:
        driver.join(stream)

    assert "- waiting for device -" in info.value.stderr
    assert entries == []
    assert [(c.old, c.new) for c in _changes(events)] == [
        (S.IDLE, S.CONNECTING),
        (S.CONNECTING, S.FAILED),
    ]
    commands = [call["command"] for call in fake_adb.calls()]
    assert commands == ["date", "logcat", "get-state"]
    assert all(SERIAL in call["args"] for call in fake_adb.calls())
    assert fake_adb.survivors() == []


def test_absent_device_fails_its_clock_read_on_every_attempt(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """With the device gone, the clock read before each spawn fails; max_attempts
    ends the run, and no adb logcat ever starts (design 5.6).
    """
    fake_adb.device_absent(SERIAL)
    stream = driver.open(
        config(serial=SERIAL, reconnect=Reconnect(delay=0.01, max_attempts=2))
    )
    driver.start(stream)

    with pytest.raises(AdbExitError, match="device not available") as info:
        driver.join(stream)

    assert "not found" in info.value.stderr
    assert "after 2 attempts" in info.value.__notes__
    assert [call["command"] for call in fake_adb.calls()] == ["date"] * 3
    assert stream.stats.connections == 3
