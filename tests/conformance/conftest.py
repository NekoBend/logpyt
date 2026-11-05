"""Conformance tests: the behavior every stream driver shares (design 7, 8.3).

A test takes the ``driver`` fixture and makes its lifecycle calls through it
(construct, start, stop, kill, join, wait_running, iterate, the with-block),
so one test body serves every driver. PR-12 has the thread driver only; PR-13
adds ``"async"`` to `DRIVERS` with a driver that makes the same calls on an
event loop. Callbacks are plain functions, which both drivers take.

Every call that waits is bounded: ``join`` and ``wait_running`` by their own
timeout, and a pull ``next()`` or a with-block exit by `_bounded`, which kills
the stream when the call has not returned within `TIMEOUT`, so a hang fails
the test instead of waiting for the hang watchdog.

Until PR-14 a reconnect and a stall resume replay from the first position
(design A.20), so `config` turns the stall resume off unless a test asks for
it. The fake adb answers the clock read that precedes every connection with
the host clock (``tests/conftest.py``).
"""

from __future__ import annotations

import contextlib
import functools
import threading
from typing import TYPE_CHECKING, Any, cast

import pytest

from logpyt import adb
from logpyt.config import StreamConfig
from logpyt.streams import LogStream, sync
from tests.conftest import wait_until

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

# The drivers every conformance test runs for (PR-13 adds "async").
DRIVERS = ("sync",)

# Upper bound for every wait; a healthy run finishes far sooner.
TIMEOUT = 10.0


def config(**fields: Any) -> StreamConfig:  # noqa: ANN401
    """A `StreamConfig` for these tests: no stall resume unless asked (A.20)."""
    fields.setdefault("stall_timeout", None)
    return StreamConfig(**fields)


def _bounded[R](stream: LogStream[Any], call: Callable[[], R], what: str) -> R:
    """Run ``call``; kill ``stream`` and fail if it has not returned in TIMEOUT."""
    fired = threading.Event()

    def expire() -> None:
        fired.set()
        stream.kill()

    timer = threading.Timer(TIMEOUT, expire)
    timer.daemon = True
    timer.start()
    message = f"{what} did not return within {TIMEOUT} s; the stream was killed"
    try:
        result = call()
    except BaseException:
        timer.cancel()
        if fired.is_set():
            pytest.fail(message)
        raise
    timer.cancel()
    if fired.is_set():
        pytest.fail(message)
    return result


class Pull:
    """A pull iterator of a stream, with bounded steps."""

    def __init__(self, stream: LogStream[Any]) -> None:
        self._stream = stream
        self._iterator: Any = iter(stream)

    def next(self) -> Any:  # noqa: ANN401
        """The next item; StopIteration at the end."""
        return _bounded(self._stream, lambda: next(self._iterator), "next()")

    def rest(self) -> list[Any]:
        """Every item up to the end of the iteration."""
        return _bounded(self._stream, lambda: list(self._iterator), "iteration")

    def close(self) -> None:
        """Detach the iterator (design 3.5)."""
        self._iterator.close()


class SyncDriver:
    """The thread driver: each call is the `LogStream` method itself."""

    name = "sync"

    def open(
        self,
        config: StreamConfig | None = None,
        /,
        **kwargs: Any,  # noqa: ANN401  (the LogStream keywords, passed on)
    ) -> LogStream[Any]:
        """Construct a stream (it spawns nothing)."""
        return LogStream(config, **kwargs)

    def start(self, stream: LogStream[Any]) -> None:
        stream.start()

    def stop(self, stream: LogStream[Any], *, flush: float | None = None) -> None:
        stream.stop(flush=flush)

    def kill(self, stream: LogStream[Any]) -> None:
        stream.kill()

    def join(self, stream: LogStream[Any], timeout: float | None = TIMEOUT) -> None:
        """``join(timeout)``; None waits without a bound (the interrupt tests)."""
        stream.join(timeout)

    def wait_running(
        self, stream: LogStream[Any], timeout: float | None = TIMEOUT
    ) -> None:
        """``wait_running(timeout)``; None waits without a bound."""
        stream.wait_running(timeout)

    def iterate(self, stream: LogStream[Any]) -> Pull:
        """The stream's pull iterator."""
        return Pull(stream)

    def first_by_break(self, stream: LogStream[Any]) -> Any:  # noqa: ANN401
        """``for item in stream: break``: the first item, by a real loop that
        drops its iterator at the ``break`` (no `Pull` keeps it).
        """

        def first() -> Any:  # noqa: ANN401
            for item in stream:
                return item
            msg = "the iteration ended before the first item"
            raise AssertionError(msg)

        return _bounded(stream, first, "the first next()")

    def kill_in_the_loop(self, stream: LogStream[Any]) -> list[Any]:
        """``for item in stream: stream.kill()``: the items the loop body got.

        Nothing that could let another thread run comes between the kill()
        and the next step of the loop (the bound is armed once, around it).
        """

        def loop() -> list[Any]:
            got: list[Any] = []
            for item in stream:
                got.append(item)
                stream.kill()
            return got

        return _bounded(stream, loop, "the iteration")

    @contextlib.contextmanager
    def session(self, stream: LogStream[Any]) -> Iterator[LogStream[Any]]:
        """``with stream:``; the exit is bounded like every other wait, also
        after the body raised (the body's exception then propagates).
        """
        stream.__enter__()
        try:
            yield stream
        except BaseException as exc:
            leave = functools.partial(
                stream.__exit__, type(exc), exc, exc.__traceback__
            )
            _bounded(stream, leave, "the with-block exit")
            raise
        _bounded(
            stream, lambda: stream.__exit__(None, None, None), "the with-block exit"
        )

    def refuse_logcat_spawn(
        self, monkeypatch: pytest.MonkeyPatch, error: OSError
    ) -> list[tuple[str, ...]]:
        """Make every ``adb logcat`` spawn raise ``error``; return the argvs tried.

        The one-shot commands (the clock read) still run.
        """
        tried: list[tuple[str, ...]] = []
        real = adb._spawn_adb

        def spawn(argv: tuple[str, ...]) -> Any:  # noqa: ANN401
            if "logcat" in argv:
                tried.append(tuple(argv))
                raise error
            return real(argv)

        monkeypatch.setattr(adb, "_spawn_adb", spawn)
        return tried

    def consumer_identity(self) -> object:
        """What identifies the consumer context the caller runs in: its thread.

        The `threading.Thread` object, not its ident, which the OS hands out
        again once a thread ended (design A.23). A caller keeps the objects,
        so two contexts never share one.
        """
        return threading.current_thread()

    def wait_concluded(self, stream: LogStream[Any]) -> None:
        """Wait until the run concluded: terminal, or waiting in the drain (A.21).

        adb's end alone does not show that the core has handled it; this
        waits for what the engine published (`StreamCore.concluded`).
        """
        wait_until(lambda: stream._run.concluded, timeout=TIMEOUT)

    def break_reader(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        """Make the stdout reader of every connection raise ``error`` (a logpyt
        bug) at its second read; the first read returns what adb wrote.
        """
        real = cast("Callable[..., None]", sync._Run._read)

        class Broken:
            def __init__(self, pipe: Any) -> None:  # noqa: ANN401
                self._pipe = pipe
                self._reads = 0

            def read(self, size: int) -> bytes:
                self._reads += 1
                if self._reads == 2:
                    raise error
                return self._pipe.read(size)

            def close(self) -> None:
                self._pipe.close()

        def read(run: Any, connection: int, name: str, pipe: Any) -> None:  # noqa: ANN401
            real(run, connection, name, Broken(pipe) if name == "stdout" else pipe)

        monkeypatch.setattr(sync._Run, "_read", read)

    def hold_after_delivery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        hold: Callable[[Sequence[object]], None],
    ) -> None:
        """Call ``hold(records)`` right after the driver handed records to its
        consumer side, before it goes on (as a preemption there would): a test
        holds the engine to check what a consumer reads meanwhile. The records
        that end the run go out with the outcome and are not held.
        """
        real = cast("Callable[..., bool]", sync._Run._deliver)

        def deliver(run: Any, records: tuple[object, ...]) -> bool:  # noqa: ANN401
            changed = real(run, records)
            hold(records)
            return changed

        monkeypatch.setattr(sync._Run, "_deliver", deliver)


type Driver = SyncDriver


@pytest.fixture(params=DRIVERS)
def driver(request: pytest.FixtureRequest) -> Driver:
    """The driver a conformance test runs for (see the module docs)."""
    drivers: dict[str, Driver] = {"sync": SyncDriver()}
    return drivers[request.param]
