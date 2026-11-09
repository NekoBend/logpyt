"""The soak's app-line criterion, without a soak (design 9.1; ``tests/soak``).

The reinstall scenario judges the app lines by what a soak run would hand over;
these cases pin that judgement on small deliveries.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, cast

import pytest

from logpyt import StreamStats
from tests.soak.harness import (
    APP_TAG,
    LAST_UID_LINES,
    SEQ_TAG,
    Consumer,
    Plan,
    Result,
    Writer,
    _judge_apps,
)

if TYPE_CHECKING:
    from tests.conftest import FakeAdb

T0 = 1_793_769_076_000_000


def _result(
    fake: FakeAdb, *, written: int, delivered: set[int], unattributed: int, last: int
) -> Result:
    fake.device()
    for n in range(written):
        fake.device_log(f"app={n}", tag=APP_TAG, at=T0 + n * 1000)
    consumer = Consumer()
    consumer.apps = bytearray(int(n in delivered) for n in range(written))
    return Result(
        driver="sync",
        outcome="None",
        plan=Plan.of(120.0),
        samples=[],
        windows=[],
        unmeasured=0,
        overstays=[],
        consumer=consumer,
        actions={},
        logs={},
        stats=StreamStats(unattributed=unattributed),
        written=0,
        app_written=written,
        app_last_uid_from=last,
    )


class _Log:
    """A device log that stops the writer right after an app line."""

    def __init__(self) -> None:
        self.tags: list[str] = []
        self.last = ""
        self.stop = threading.Event()

    def device_log(self, *messages: str, tag: str, **_who: int) -> None:
        self.tags.append(tag)
        self.last = messages[-1]
        if tag == APP_TAG:
            self.stop.set()


def test_the_log_ends_with_a_seq_line() -> None:
    """The run waits for the last seq and then stops the stream: an app line behind
    it could be cut off, which the app-line criterion would report as lost."""
    log = _Log()
    writer = Writer(cast("FakeAdb", log), Plan.of(120.0), threading.Lock())
    writer.stop = log.stop

    writer._run()

    assert log.tags[-2:] == [APP_TAG, SEQ_TAG]
    assert log.last == f"seq={writer.seqs - 1}"


def test_every_app_line_delivered_passes(fake_adb: FakeAdb) -> None:
    result = _result(
        fake_adb, written=5, delivered=set(range(5)), unattributed=0, last=0
    )

    _judge_apps(result, fake_adb)

    assert result.failures == []
    assert result.app_missing == 0


def test_missing_app_lines_need_a_gap_or_the_unattributed_count(
    fake_adb: FakeAdb,
) -> None:
    delivered = {0, 1, 4, 5}
    outside = _result(fake_adb, written=6, delivered=delivered, unattributed=1, last=0)

    _judge_apps(outside, fake_adb)

    assert outside.app_outside == 2
    assert outside.failures == ["2 missing app lines outside every Gap, 1 unattributed"]
    explained = _result(
        fake_adb, written=6, delivered=delivered, unattributed=2, last=0
    )
    _judge_apps(explained, fake_adb)
    assert explained.failures == []
    in_gap = _result(fake_adb, written=6, delivered=delivered, unattributed=0, last=0)
    in_gap.consumer.gaps.append((T0 + 1000, T0 + 4000, "ring_rotated"))
    _judge_apps(in_gap, fake_adb)
    assert in_gap.app_outside == 0
    assert in_gap.failures == []


@pytest.mark.parametrize(
    ("written", "reported"),
    [(LAST_UID_LINES, True), (LAST_UID_LINES - 1, False)],
    ids=["enough_lines", "too_few_to_judge"],
)
def test_a_line_of_the_last_uid_must_arrive(
    fake_adb: FakeAdb, written: int, *, reported: bool
) -> None:
    """A lookup that never learns the new uid filters out all its lines: each of
    them counts as unattributed at most, and only this criterion sees that."""
    result = _result(
        fake_adb, written=written, delivered=set(), unattributed=written, last=0
    )

    _judge_apps(result, fake_adb)

    assert ("no app line of the last uid arrived" in result.failures) is reported
