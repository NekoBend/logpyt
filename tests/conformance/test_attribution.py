"""Package attribution conformance: where=Package(...) on a live stream (design 5.8).

The core's attribution policy (PR-11) with the driver's one-shot lookups, for
every driver (see ``conftest.py``). The F5-2 behavior tests are ported in
``tests/behavior/test_package_filter_behavior.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from logpyt import Package
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, config
from tests.conftest import (
    FakeAdb,
    dumpsys_package,
    logcat_line,
    out,
    run,
    touch,
    wait,
    wait_until,
)

if TYPE_CHECKING:
    from logpyt import LogEntry

APP = "com.example.app"
APP_UID = 10065


def test_package_lines_are_attributed_by_uid_lookup(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """The lookup at start maps the package to its uid before the backlog is
    read; lines of other uids and system uids are filtered out (F5-2, D-8).
    """
    fake_adb.dumpsys(APP, run(out(dumpsys_package(APP, APP_UID))))
    fake_adb.logcat(
        run(
            out(logcat_line("app", uid=APP_UID)),
            out(logcat_line("other app", uid=10099)),
            out(logcat_line("system", uid="root")),
            out(logcat_line("app again", uid=APP_UID)),
            touch("sent"),
            wait("never"),
        )
    )
    entries: list[LogEntry] = []
    stream = driver.open(config(), where=Package(APP), on_item=entries.append)
    driver.start(stream)
    fake_adb.wait_for("sent")
    wait_until(lambda: len(entries) == 2, timeout=TIMEOUT)
    driver.stop(stream)
    driver.join(stream)

    assert [(entry.message, entry.package) for entry in entries] == [
        ("app", APP),
        ("app again", APP),
    ]
    assert stream.state is StreamState.STOPPED
    stats = stream.stats
    assert (stats.lines, stats.delivered, stats.filtered) == (4, 2, 2)
    assert stats.unattributed == 0
    lookups = fake_adb.calls("dumpsys")
    assert lookups
    assert lookups[0]["args"] == ["shell", "dumpsys", "package", APP]
