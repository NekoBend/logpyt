"""Delivery conformance: the end of adb's output, a pipe adb does not close,
and backpressure.

Design 3.2 (the connection-ended row), 5.3, 5.10 and the F1-1 row of the
coverage matrix, for every driver (see ``conftest.py``): a reader ends only at
EOF, adb's exit is a separate event, ``exit_drain`` bounds the wait for EOF
after the exit, and a full outbox holds adb back instead of growing.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import pytest

from logpyt import AdbExitError
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, config
from tests.conftest import (
    FakeAdb,
    hold_stdout,
    lines,
    logcat_line,
    out,
    run,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from logpyt import LogEntry

S = StreamState
TAIL_LINES = 20_000
# One pipe read of the thread driver (design 5.3).
READ_SIZE = 65536
# How long adb must stay held back by a blocked consumer (as in X-1).
HELD_BACK_FOR = 2.0


@pytest.mark.parametrize("attempt", range(5), ids=lambda n: f"run{n}")
@pytest.mark.parametrize("buffer", [10_000, 500], ids=["roomy", "tight"])
def test_tail_delivered_when_adb_exits_first(
    driver: Driver, fake_adb: FakeAdb, buffer: int, attempt: int
) -> None:
    """adb writes 20 000 lines and exits at once: every line is delivered, in
    order, before the outcome (F1-1).

    adb's exit usually reaches the stream while most of its output is still
    in the pipe and the data inbox; ``tight`` adds backpressure, so the exit
    also comes while intake is closed and ``exit_drain`` must wait. Exit
    status 0 ends a run without reconnect FAILED (design 3.2), and the items
    of a FAILED run are still delivered (design A.21).
    """
    del attempt  # five runs: the exit races the readers differently each time
    fake_adb.logcat(run(lines(TAIL_LINES, logcat_line("m{i}")), exit_code=0))
    entries: list[LogEntry] = []
    stream = driver.open(config(buffer_entries=buffer), on_item=entries.append)
    driver.start(stream)

    with pytest.raises(AdbExitError) as info:
        driver.join(stream)

    assert info.value.returncode == 0
    messages = [entry.message for entry in entries]
    assert len(messages) == TAIL_LINES
    assert messages == [f"m{i}" for i in range(TAIL_LINES)]
    stats = stream.stats
    assert (stats.lines, stats.delivered, stats.undelivered) == (
        TAIL_LINES,
        TAIL_LINES,
        0,
    )
    assert stream.state is S.FAILED


def test_grandchild_holding_stdout_is_bounded(
    driver: Driver, fake_adb: FakeAdb, caplog: pytest.LogCaptureFixture
) -> None:
    """adb exits while a process it started still holds its stdout: the
    connection ends after ``exit_drain``, not when the pipe closes (F1-1,
    design 5.10).

    The lines adb wrote are delivered, a WARNING names the open pipe, and the
    reader still closes the pipe once the grandchild lets go (X-5). What the
    grandchild writes then belongs to a retired connection and is ignored (G1).
    """
    caplog.set_level(logging.WARNING, logger="logpyt.stream")
    fake_adb.logcat(
        run(
            out(logcat_line("a")),
            out(logcat_line("b")),
            hold_stdout("let go", then=logcat_line("late")),
            exit_code=0,
        )
    )
    entries: list[LogEntry] = []
    stream = driver.open(config(exit_drain=0.2), on_item=entries.append)
    driver.start(stream)
    try:
        with pytest.raises(AdbExitError) as info:
            driver.join(stream)
    finally:
        fake_adb.release("let go")

    assert info.value.returncode == 0
    assert [entry.message for entry in entries] == ["a", "b"]
    warned = [r.getMessage() for r in caplog.records if r.name == "logpyt.stream"]
    assert any("stdout pipe stayed open for exit_drain" in m for m in warned), warned
    (child,) = fake_adb.children("logcat")
    assert child.popen.stdout is not None
    wait_until(lambda: child.pipe_closed(child.popen.stdout), timeout=TIMEOUT)
    assert stream.stats.lines == 2
    assert stream.state is S.FAILED


def test_a_blocked_consumer_holds_adb_back(driver: Driver, fake_adb: FakeAdb) -> None:
    """A consumer stuck in on_item: once the outbox is full, intake closes and
    the stream reads no more, so adb blocks on its pipe and the stream holds
    at most the outbox plus one read's lines (design 5.3, 5.10, X-1).

    adb writes about 3 MB, more than the outbox, the data inbox (16 reads)
    and the pipe hold together, then touches ``sent``; a stream that kept
    reading would let it get there.
    """
    buffer, count = 10, 3000
    template = logcat_line("x" * 1000 + "{i}")
    line_bytes = len(template.format(i=0).encode())
    fake_adb.logcat(run(lines(count, template), touch("sent"), wait("never")))
    busy = threading.Event()
    release = threading.Event()

    def on_item(entry: LogEntry) -> None:
        del entry
        busy.set()
        release.wait(TIMEOUT)

    stream = driver.open(config(buffer_entries=buffer), on_item=on_item)
    driver.start(stream)
    held_back = False
    try:
        assert busy.wait(TIMEOUT)
        try:
            fake_adb.wait_for("sent", timeout=HELD_BACK_FOR)
        except TimeoutError:
            held_back = True
        read_while_blocked = stream.stats.lines
    finally:
        release.set()
        driver.kill(stream)
    driver.join(stream)

    assert held_back, f"adb wrote everything; the stream read {stream.stats.lines}"
    assert read_while_blocked <= buffer + READ_SIZE // line_bytes + 1
    assert stream.state is S.KILLED
    assert fake_adb.survivors() == []
