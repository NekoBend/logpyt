"""Resource conformance: what a connection holds ends with it (X-5, design 5.10).

For every driver (see ``conftest.py``): each reader closes its pipe when it
ends, so a retired connection leaves no pipe open, and the process's fd count
returns to its baseline. The pipes are checked on the very `subprocess.Popen`
objects the stream spawned (the fake adb registry holds them), on every
platform; the fd count where the OS lists the open fds (Linux, macOS).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from logpyt import Reconnect
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, config
from tests.conftest import FakeAdb, logcat_line, out, run, wait, wait_until

if TYPE_CHECKING:
    from logpyt import LogEntry
    from tests.conftest import SpawnedChild

RECONNECTS = 3


def _open_fds() -> int | None:
    """The number of fds this process has open; None where it cannot be listed."""
    if sys.platform == "win32":
        return None  # no fd table to list; the pipe checks still run
    for table in (Path("/proc/self/fd"), Path("/dev/fd")):
        if table.is_dir():
            return sum(1 for _ in table.iterdir())
    return None


def _fds_at_most(limit: int) -> bool:
    """Whether this process has at most ``limit`` fds open."""
    count = _open_fds()
    return count is not None and count <= limit


def _pipes_closed(child: SpawnedChild) -> bool:
    """Whether the stream closed both pipes of ``child`` (its own Popen's)."""
    popen = child.popen
    return all(pipe is None or pipe.closed for pipe in (popen.stdout, popen.stderr))


def test_fds_return_to_baseline_after_reconnects(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """After three reconnects only the live connection's two pipes are open,
    and after the end none is: the fd count returns to its baseline (X-5).
    """
    fake_adb.logcat(
        *(run(out(logcat_line(f"c{n}")), exit_code=1) for n in range(RECONNECTS)),
        run(out(logcat_line("live")), wait("never")),
    )
    entries: list[LogEntry] = []
    baseline = _open_fds()
    stream = driver.open(
        config(reconnect=Reconnect(delay=0.01)), on_item=entries.append
    )
    driver.start(stream)
    wait_until(lambda: len(entries) == RECONNECTS + 1, timeout=TIMEOUT)

    children = fake_adb.children("logcat")
    assert len(children) == RECONNECTS + 1
    wait_until(lambda: all(_pipes_closed(c) for c in children[:-1]), timeout=TIMEOUT)
    assert not _pipes_closed(children[-1])
    if baseline is not None:
        # The live connection's stdout and stderr; nothing of the retired ones.
        wait_until(lambda: _fds_at_most(baseline + 2), timeout=TIMEOUT)

    driver.stop(stream)
    driver.join(stream)

    wait_until(lambda: all(_pipes_closed(c) for c in children), timeout=TIMEOUT)
    if baseline is not None:
        wait_until(lambda: _fds_at_most(baseline), timeout=TIMEOUT)
    assert [entry.message for entry in entries] == ["c0", "c1", "c2", "live"]
    assert stream.stats.reconnects == RECONNECTS
    assert stream.state is StreamState.STOPPED
    assert fake_adb.survivors() == []
