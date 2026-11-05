"""Callback conformance: push delivery, the consumer context, fail fast.

Design 2.4, 3.6, 4.2, A.21 and the F2-5, F2-6, F3-1 and F3-7 rows of the
coverage matrix, for every driver (see ``conftest.py``).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from logpyt import (
    CallbackError,
    GroupBy,
    LogEntry,
    Reconnect,
    StateChange,
    UsageError,
    Where,
)
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, Pull, config
from tests.conftest import (
    FakeAdb,
    lines,
    logcat_line,
    out,
    run,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from logpyt import LogGroup, StreamEvent
    from logpyt.streams import LogStream

S = StreamState


def _changes(records: Sequence[object]) -> list[tuple[StreamState, StreamState]]:
    return [(r.old, r.new) for r in records if isinstance(r, StateChange)]


def test_push_delivers_every_line_in_order(driver: Driver, fake_adb: FakeAdb) -> None:
    """on_item and on_event share one order: the state changes come between the
    items where they happened (design 2.4, 3.6).
    """
    fake_adb.logcat(run(lines(500, logcat_line("m{i}")), wait("never")))
    records: list[object] = []
    stream = driver.open(config(), on_item=records.append, on_event=records.append)
    driver.start(stream)
    wait_until(
        lambda: sum(isinstance(r, LogEntry) for r in records) == 500, timeout=TIMEOUT
    )
    driver.stop(stream)
    driver.join(stream)

    entries = [r for r in records if isinstance(r, LogEntry)]
    assert [entry.message for entry in entries] == [f"m{i}" for i in range(500)]
    assert _changes(records) == [
        (S.IDLE, S.CONNECTING),
        (S.CONNECTING, S.RUNNING),
        (S.RUNNING, S.STOPPING),
        (S.STOPPING, S.STOPPED),
    ]
    assert records.index(entries[0]) == 2  # after the change to RUNNING
    assert records.index(entries[-1]) == len(records) - 3  # before STOPPING
    assert stream.stats.delivered == 500


def test_join_returns_after_the_last_callback(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """join() waits until the consumer finished every record, the terminal
    change's on_event included (design A.21).
    """
    fake_adb.logcat(run(lines(3, logcat_line("m{i}")), wait("never")))
    order: list[str] = []
    busy = threading.Event()
    release = threading.Event()

    def on_item(entry: LogEntry) -> None:
        if entry.message == "m2":
            busy.set()
            release.wait(TIMEOUT)
        order.append(entry.message)

    def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange):
            order.append(f"{event.old}->{event.new}")

    def release_once_adb_ended() -> None:
        wait_until(lambda: not fake_adb.live_children("logcat"), timeout=TIMEOUT)
        order.append("released")
        release.set()

    stream = driver.open(config(), on_item=on_item, on_event=on_event)
    driver.start(stream)
    assert busy.wait(TIMEOUT)
    driver.stop(stream)
    releaser = threading.Thread(target=release_once_adb_ended)
    releaser.start()
    try:
        driver.join(stream)
    finally:
        release.set()
        releaser.join(TIMEOUT)
    order.append("joined")

    assert order[-5:] == [
        "released",
        "m2",
        "RUNNING->STOPPING",
        "STOPPING->STOPPED",
        "joined",
    ]


def test_on_item_failure_ends_failed(driver: Driver, fake_adb: FakeAdb) -> None:
    """A raising on_item ends the run with CallbackError; nothing more is called
    and the rest is discarded and counted (fail fast, design 2.4, A.21).
    """
    fake_adb.logcat(run(lines(10, logcat_line("m{i}")), touch("sent"), wait("never")))
    seen: list[str] = []
    events: list[StreamEvent] = []

    def on_item(entry: LogEntry) -> None:
        seen.append(entry.message)
        if entry.message == "m3":
            msg = "boom"
            raise ValueError(msg)

    stream = driver.open(config(), on_item=on_item, on_event=events.append)
    driver.start(stream)

    with pytest.raises(CallbackError) as info:
        driver.join(stream)

    assert info.value.stage == "on_item"
    assert isinstance(info.value.__cause__, ValueError)
    assert seen == ["m0", "m1", "m2", "m3"]
    assert _changes(events) == [(S.IDLE, S.CONNECTING), (S.CONNECTING, S.RUNNING)]
    assert stream.state is S.FAILED
    stats = stream.stats
    assert stats.delivered == 4
    assert stats.delivered + stats.undelivered == 10
    assert fake_adb.survivors() == []


def test_on_event_raising_on_connecting_fails_cleanly(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """on_event raising on IDLE -> CONNECTING ends the run FAILED without a
    stranded adb (F2-5).
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))

    def on_event(event: StreamEvent) -> None:
        if isinstance(event, StateChange) and event.new is S.CONNECTING:
            msg = "hook"
            raise RuntimeError(msg)

    entries: list[LogEntry] = []
    stream = driver.open(config(), on_item=entries.append, on_event=on_event)
    driver.start(stream)

    with pytest.raises(CallbackError) as info:
        driver.join(stream)

    assert info.value.stage == "on_event"
    assert entries == []
    assert stream.state is S.FAILED
    assert fake_adb.survivors() == []


_WAITS = ("join", "join_timeout", "wait_running")


def _try_waits(stream: LogStream[LogEntry], errors: list[str]) -> None:
    calls: dict[str, Callable[[], None]] = {
        "join": stream.join,
        "join_timeout": lambda: stream.join(timeout=1.0),
        "wait_running": lambda: stream.wait_running(timeout=1.0),
    }
    for name in _WAITS:
        try:
            calls[name]()
        except UsageError as exc:
            errors.append(f"{name}: {exc}")
        else:
            errors.append(f"{name}: returned")


@pytest.mark.parametrize("where", ["on_item", "on_event", "pull_on_event", "where"])
def test_join_from_callback_raises_usage_error(
    driver: Driver, fake_adb: FakeAdb, where: str
) -> None:
    """join() and wait_running() from the stream's own user code raise
    UsageError instead of deadlocking (F3-7, design 3.6).
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    errors: list[str] = []
    stream: LogStream[LogEntry]

    def callback(record: object) -> None:
        if isinstance(record, LogEntry) or (
            isinstance(record, StateChange) and record.new is S.RUNNING
        ):
            if not errors:
                _try_waits(stream, errors)
            stream.stop()

    def condition(entry: LogEntry) -> bool:
        del entry
        if not errors:
            _try_waits(stream, errors)
        return True

    if where == "on_item":
        stream = driver.open(config(), on_item=callback)
    elif where == "on_event":
        stream = driver.open(config(), on_item=lambda _: None, on_event=callback)
    elif where == "pull_on_event":
        stream = driver.open(config(), on_event=callback)
    else:
        stream = driver.open(config(), where=Where(condition), on_item=callback)
    driver.start(stream)
    if where == "pull_on_event":
        driver.iterate(stream).rest()
    driver.join(stream)

    assert len(errors) == len(_WAITS)
    for name, message in zip(_WAITS, errors, strict=True):
        assert message.startswith(f"{name}: ")
        assert "called from a stream callback" in message
    assert stream.state is S.STOPPED


def _threads_reusing(
    idents: dict[str, int], check: Callable[[str], str]
) -> dict[str, str]:
    """Run ``check(name)`` in each new thread whose ident is ``idents[name]``.

    64 threads run at once, so they take distinct idents, freed ones too
    (glibc hands a freed one out at once). Returns name -> what check said.
    """
    results: dict[str, str] = {}
    go = threading.Event()

    def attempt() -> None:
        go.wait(TIMEOUT)
        me = threading.get_ident()
        for name, ident in idents.items():
            if me == ident:
                results[name] = check(name)

    threads = [threading.Thread(target=attempt) for _ in range(64)]
    for thread in threads:
        thread.start()
    go.set()
    for thread in threads:
        thread.join(TIMEOUT)
    return results


def test_waits_from_a_thread_reusing_a_worker_ident(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """A new thread that got the ident of the stream's ended engine or
    dispatcher is not stream code: its join() and wait_running() return
    (F3-7, design 3.6).

    Thread idents are unique only among live threads, and glibc reuses them
    at once; where no new thread gets an old ident there is nothing to check.
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    idents: dict[str, int] = {}

    def condition(entry: LogEntry) -> bool:
        del entry
        idents["engine"] = threading.get_ident()
        return True

    def on_item(entry: LogEntry) -> None:
        del entry
        idents["dispatcher"] = threading.get_ident()

    stream = driver.open(config(), where=Where(condition), on_item=on_item)
    driver.start(stream)
    wait_until(lambda: len(idents) == 2, timeout=TIMEOUT)
    driver.stop(stream)
    driver.join(stream)

    def check(name: str) -> str:
        del name
        try:
            driver.join(stream)
            driver.wait_running(stream)
        except UsageError as exc:
            return f"UsageError: {exc}"
        return "returned"

    results = _threads_reusing(idents, check)

    if not results:
        pytest.skip("no new thread reused an ident of the stream's threads here")
    assert set(results.values()) == {"returned"}, results
    assert stream.state is S.STOPPED


def test_join_from_a_thread_reusing_an_owner_ident(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """The thread that created an iterator ended and left it attached; a new
    thread that got its ident does not own it: its join(timeout) waits
    (TimeoutError) instead of raising UsageError (design 3.5).
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    idents: dict[str, int] = {}
    stream = driver.open(config())
    driver.start(stream)
    kept: list[Pull] = []

    def own_an_iterator() -> None:
        idents["owner"] = threading.get_ident()
        pull = driver.iterate(stream)
        assert pull.next().message == "a"
        kept.append(pull)  # still attached once this thread has ended

    owner = threading.Thread(target=own_an_iterator)
    owner.start()
    owner.join(TIMEOUT)
    assert kept

    def check(name: str) -> str:
        del name
        try:
            driver.join(stream, timeout=0.05)
        except UsageError as exc:
            return f"UsageError: {exc}"
        except TimeoutError:
            return "waited"
        return "returned"

    results = _threads_reusing(idents, check)
    kept[0].close()
    driver.stop(stream)
    driver.join(stream)

    if not results:
        pytest.skip("no new thread reused the ident of the iterator's owner here")
    assert results == {"owner": "waited"}
    assert stream.state is S.STOPPED


def test_join_from_the_loop_body_raises_usage_error(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """join() would wait for this thread's own iterator (design 3.5)."""
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    stream = driver.open(config())
    driver.start(stream)
    pull = driver.iterate(stream)
    assert pull.next().message == "a"

    with pytest.raises(UsageError, match="own iterator"):
        driver.join(stream)
    driver.stop(stream)
    pull.close()
    driver.join(stream)

    assert stream.state is S.STOPPED


def test_one_dispatcher_across_reconnects(driver: Driver, fake_adb: FakeAdb) -> None:
    """One consumer context for the whole run: every callback runs on the same
    dispatcher, before and after a reconnect (F2-6).
    """
    fake_adb.logcat(
        run(out(logcat_line("first")), exit_code=1),
        run(out(logcat_line("second")), wait("never")),
    )
    threads: set[int] = set()
    messages: list[str] = []

    def on_item(entry: LogEntry) -> None:
        threads.add(threading.get_ident())
        messages.append(entry.message)

    def on_event(event: StreamEvent) -> None:
        del event
        threads.add(threading.get_ident())

    stream = driver.open(
        config(reconnect=Reconnect(delay=0.01)), on_item=on_item, on_event=on_event
    )
    driver.start(stream)
    wait_until(lambda: messages == ["first", "second"], timeout=TIMEOUT)
    driver.stop(stream)
    driver.join(stream)

    assert len(threads) == 1
    assert threading.get_ident() not in threads
    assert (stream.stats.connections, stream.stats.reconnects) == (2, 1)


@pytest.mark.parametrize("stage", ["on_event", "on_item", "where"])
def test_on_event_not_under_lock(driver: Driver, fake_adb: FakeAdb, stage: str) -> None:
    """User code runs outside every library lock (F2-11, design 3.6): while
    ``stage`` blocks, another thread reads ``state`` and ``stats`` and calls
    ``stop()``, and each returns at once. While a consumer callback blocks,
    the engine reads on; ``where`` runs in the engine itself (design 2.4).
    """
    fake_adb.logcat(
        run(
            out(logcat_line("first")),
            wait("more"),
            lines(50, logcat_line("m{i}")),
            touch("sent"),
            wait("never"),
        )
    )
    inside = threading.Event()
    release = threading.Event()

    def block(record: object) -> None:
        target = (
            isinstance(record, StateChange) and record.new is S.RUNNING
            if stage == "on_event"
            else isinstance(record, LogEntry) and record.message == "first"
        )
        if target and not inside.is_set():
            inside.set()
            release.wait(3 * TIMEOUT)  # longer than the caller's bound below

    def condition(entry: LogEntry) -> bool:
        block(entry)
        return True

    entries: list[LogEntry] = []

    def on_item(entry: LogEntry) -> None:
        if stage == "on_item":
            block(entry)
        entries.append(entry)

    stream = driver.open(
        config(),
        where=Where(condition) if stage == "where" else None,
        on_item=on_item,
        on_event=block if stage == "on_event" else None,
    )
    driver.start(stream)
    try:
        assert inside.wait(TIMEOUT)
        if stage != "where":
            fake_adb.release("more")
            fake_adb.wait_for("sent")
            wait_until(lambda: stream.stats.lines == 51, timeout=TIMEOUT)
        seen: list[object] = []
        caller = threading.Thread(
            target=lambda: seen.extend(
                (stream.state, stream.stats, driver.stop(stream))
            )
        )
        caller.start()
        caller.join(TIMEOUT)
        returned = not caller.is_alive()
        assert inside.is_set()
        assert not release.is_set()  # the user code was still blocked
    finally:
        release.set()
    driver.join(stream)

    assert returned, f"state, stats or stop() blocked while {stage} ran"
    assert len(seen) == 3
    more = [] if stage == "where" else [f"m{i}" for i in range(50)]
    assert [entry.message for entry in entries] == ["first", *more]
    assert stream.state is S.STOPPED
    assert fake_adb.survivors() == []


def test_group_by_delivers_groups_and_flushes_at_the_end(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """group_by runs in the engine; the open group goes out at the end."""
    fake_adb.logcat(
        run(lines(3, logcat_line("m{i}", pid=7)), touch("sent"), wait("never"))
    )
    groups: list[LogGroup] = []
    spec = GroupBy(keys=("pid",), gap_ms=60_000.0, idle_flush_ms=None)
    stream = driver.open(config(), group_by=spec, on_item=groups.append)
    driver.start(stream)
    fake_adb.wait_for("sent")
    wait_until(lambda: stream.stats.lines == 3, timeout=TIMEOUT)
    driver.stop(stream)
    driver.join(stream)

    assert [[entry.message for entry in group] for group in groups] == [
        ["m0", "m1", "m2"]
    ]
    assert groups[0].key == (7,)
    assert groups[0].reason == "end"
    assert stream.stats.delivered == 3


def test_where_failure_ends_failed_and_keeps_what_it_accepted(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """A raising where ends the run; the items accepted before it are still
    delivered (F3-1).
    """
    fake_adb.logcat(run(lines(5, logcat_line("m{i}")), wait("never")))

    def condition(entry: LogEntry) -> bool:
        if entry.message == "m2":
            raise KeyError(entry.message)
        return True

    entries: list[LogEntry] = []
    stream = driver.open(config(), where=Where(condition), on_item=entries.append)
    driver.start(stream)

    with pytest.raises(CallbackError) as info:
        driver.join(stream)

    assert info.value.stage == "where"
    assert isinstance(info.value.__cause__, KeyError)
    assert [entry.message for entry in entries] == ["m0", "m1"]
    assert fake_adb.survivors() == []
