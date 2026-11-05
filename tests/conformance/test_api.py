"""API conformance: argument checks, iteration misuse and assert_complete().

Design 2.3, 2.4, 4.2 and 4.4, for every driver (see ``conftest.py``): the
constructor refuses wrong types with TypeError and spawns nothing, iteration
refuses a push-mode stream, an unstarted one and a second iterator with
UsageError, and ``assert_complete()`` turns the end of a run into a CI verdict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from logpyt import AdbExitError, IncompleteCaptureError, StreamConfig, UsageError
from logpyt.streams._events import StreamState
from tests.conformance.conftest import TIMEOUT, Driver, config
from tests.conftest import FakeAdb, err, logcat_line, out, run, wait, wait_until

if TYPE_CHECKING:
    from logpyt import LogEntry

S = StreamState


async def _async_callback(record: object) -> None:
    del record


class _AsyncCall:
    async def __call__(self, record: object) -> None:
        del record


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("on_item", _async_callback, "on_item is an async function"),
        ("on_event", _async_callback, "on_event is an async function"),
        ("on_item", _AsyncCall(), "on_item is an async function"),
        ("on_item", "print", "on_item must be callable or None, not str"),
        ("group_by", "pid", "group_by must be a GroupBy or None, not str"),
    ],
    ids=["async_item", "async_event", "async_call", "str_item", "str_group_by"],
)
def test_constructor_refuses_wrong_types(
    driver: Driver, fake_adb: FakeAdb, keyword: str, value: object, message: str
) -> None:
    """A wrong type is a TypeError from the constructor, which spawns nothing
    (design 2.3, 4.2). An async callback is refused because LogStream never
    awaits it; AsyncLogStream takes one.
    """
    if driver.name != "sync" and "async" in message:
        pytest.skip("an async driver takes async callbacks (design 2.3)")
    kwargs: dict[str, Any] = {keyword: value}

    with pytest.raises(TypeError, match=message):
        driver.open(config(), **kwargs)

    assert fake_adb.calls() == []


def test_constructor_refuses_a_config_of_another_type(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """``config`` is a StreamConfig or None; a dict is a TypeError (design 4.2)."""
    with pytest.raises(TypeError, match="config must be a StreamConfig or None"):
        driver.open({"buffer_entries": 10})  # ty: ignore[invalid-argument-type]

    assert isinstance(driver.open(None).config, StreamConfig)
    assert fake_adb.calls() == []


def test_iteration_misuse_raises_usage_error(driver: Driver, fake_adb: FakeAdb) -> None:
    """Iterating a push-mode stream, an unstarted stream, or a stream a second
    time raises UsageError; the first iterator keeps working (design 2.4, 4.2).
    """
    fake_adb.logcat(run(out(logcat_line("a")), wait("never")))
    entries: list[LogEntry] = []
    push = driver.open(config(), on_item=entries.append)
    with pytest.raises(UsageError, match="with on_item cannot be iterated"):
        driver.iterate(push)
    pull = driver.open(config())
    with pytest.raises(UsageError, match=r"iterated before start\(\)"):
        driver.iterate(pull)

    driver.start(pull)
    first = driver.iterate(pull)
    with pytest.raises(UsageError, match="one iterator per run"):
        driver.iterate(pull)
    assert first.next().message == "a"
    driver.stop(pull)
    assert first.rest() == []
    driver.join(pull)

    assert pull.state is S.STOPPED
    assert fake_adb.survivors() == []


def test_assert_complete_verdicts(driver: Driver, fake_adb: FakeAdb) -> None:
    """``assert_complete()``: UsageError before the end; nothing for a STOPPED
    complete capture; IncompleteCaptureError with the reasons for a KILLED
    one (design 4.4).
    """
    fake_adb.logcat(
        run(out(logcat_line("a")), wait("never")),
        run(out(logcat_line("b")), wait("never")),
    )
    entries: list[LogEntry] = []
    stream = driver.open(config(), on_item=entries.append)
    driver.start(stream)
    wait_until(lambda: entries, timeout=TIMEOUT)
    with pytest.raises(UsageError, match=r"call join\(\) first"):
        stream.assert_complete()
    driver.stop(stream)
    driver.join(stream)
    assert stream.state is S.STOPPED
    stream.assert_complete()

    killed = stream.clone()
    driver.start(killed)
    wait_until(lambda: len(entries) == 2, timeout=TIMEOUT)
    driver.kill(killed)
    driver.join(killed)

    with pytest.raises(IncompleteCaptureError) as info:
        killed.assert_complete()
    assert info.value.reasons == killed.stats.incomplete
    assert "killed" in info.value.reasons
    assert isinstance(info.value, AssertionError)
    assert fake_adb.survivors() == []


def test_assert_complete_raises_the_failed_outcome(
    driver: Driver, fake_adb: FakeAdb
) -> None:
    """``assert_complete()`` of a FAILED run raises the outcome itself, before
    any IncompleteCaptureError (design 4.4).
    """
    fake_adb.logcat(run(err("boom\n"), exit_code=3))
    stream = driver.open(config())
    driver.start(stream)
    wait_until(lambda: stream.state is S.FAILED, timeout=TIMEOUT)

    with pytest.raises(AdbExitError) as info:
        stream.assert_complete()

    assert info.value is stream.outcome
    assert "failed" in stream.stats.incomplete
