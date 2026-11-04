"""Throughput and burst-loss benchmark for LogStream and AsyncLogStream.

Both streams run against the real-subprocess fake adb of ``tests/fake_adb.py``,
installed with ``tests.conftest.install_fake_adb``, so the library's own pipes,
threads, event loop and process handling are what is measured. pytest does not
collect this file (its name does not match ``test_*.py``) and CI does not run
it; recorded numbers live in ``bench/BASELINE-*.md``.

Cases (each row is repeated ``--repeat`` times and reports the median):

``throughput``
    adb writes a backlog of ``--lines`` lines at start and stays alive; a
    callback that only counts receives them. The stream is configured so that
    it cannot drop an entry, so the row measures reading, parsing and
    dispatch: lines per second from ``start()`` to the last delivery.
``burst``
    adb writes a burst of ``--burst-lines`` lines at start, as a real adb dumps
    its ring buffer, and the stream runs with its DEFAULT settings. The row
    reports delivered/expected; 3.0 must deliver all of it. 2.x can drop only
    a burst larger than its default queue (10,000 entries for ``LogStream``,
    1,024 per source for ``AsyncLogStream``), and only while the reader
    outruns the consumer. The async reader does not yield while lines are
    buffered (X-1 in ``.docs/review-findings-2.0.1.md``), so how much it gets
    ahead depends on how much each pipe read returns, which depends on the
    platform: on Linux it drops part of the burst (``bench/BASELINE-2.x.md``).
``slow-burst``
    ``burst`` with a consumer that spends ``--slow-us`` microseconds of CPU on
    each entry, slower than the stream reads. Whether a counting callback
    keeps up with the reader depends on the machine; this one never does, so
    a dropping stream loses entries of a burst larger than its queue on any
    machine, and a stream with backpressure delivers all of them, in about
    ``lines * slow-us``.

Run from the repository root, so that ``tests`` is importable::

    uv run python -m bench.stream_bench
    uv run python -m bench.stream_bench --cases burst --drivers async --repeat 5

Moving to the 3.0 API touches only the drivers: each builds its stream in
``_open`` and runs it in ``run``. The cases and the report stay as they are.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import math
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

import pytest

import logpyt
from logpyt import AsyncLogStream, LogStream
from tests.conftest import install_fake_adb, lines, run, threadtime, touch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from logpyt import AsyncStreamHandle, LogEntry, StreamHandle
    from tests.conftest import FakeAdb

DEFAULT_LINES = 200_000
# The burst sizes X-1 was first measured at (review-findings-2.0.1.md).
DEFAULT_BURST_LINES = (10_000, 50_000)
DEFAULT_REPEAT = 5
# Several times the 2.x per-line cost (about 5 us), and in the range of a
# callback that formats an entry and writes it to a file.
DEFAULT_SLOW_US = 20.0

# A run has settled once adb has written every line and nothing was delivered
# for this long: the stream has read the pipe dry and emptied (or dropped) its
# queue. Two deliveries are microseconds apart while data flows.
SETTLE_SECONDS = 1.0
# Fixed part of the bound for one run and for its stop/join; run_timeout()
# adds the time the run's work needs. A default row takes about a second.
RUN_TIMEOUT = 60.0
# Far below the rates recorded without a consumer cost (over 100,000 lines/s,
# bench/BASELINE-2.x.md): a run slower than this counts as hung.
MIN_LINES_PER_SECOND = 10_000.0
POLL_SECONDS = 0.005

_LINE = threadtime("m{i}")
# Fake adb signal: every line of the backlog is in the pipe (or already read).
_WRITTEN = "written"
_EXIT_INTERRUPTED = 130


class DeliveryProbe:
    """Counts the entries a stream delivers, optionally spending CPU on each.

    ``hit`` runs in the stream's single delivery context (its callback thread
    or its event loop); the waiting side only reads the two counters.

    Args:
        expected: Entries the run should deliver.
        cost: Seconds of busy CPU per entry. A busy loop, not a sleep: sleep
            granularity (about 1 ms on Windows) would dwarf a cost in
            microseconds, and real callback work holds the GIL too.
    """

    def __init__(self, expected: int, cost: float = 0.0) -> None:
        self.expected = expected
        self.cost = cost
        self.delivered = 0
        self.last_delivery = 0.0  # time.perf_counter() of the latest delivery

    def hit(self) -> None:
        """Record one delivered entry (after ``cost`` seconds of busy CPU)."""
        now = time.perf_counter()
        if self.cost > 0:
            end = now + self.cost
            while now < end:
                now = time.perf_counter()
        self.delivered += 1
        self.last_delivery = now


class Settled:
    """Done-predicate: every entry arrived, or adb wrote all and delivery went quiet.

    Never blocks, so the async driver can poll it on its event loop.
    """

    def __init__(self, fake: FakeAdb, probe: DeliveryProbe) -> None:
        # touch(name) creates <state>/signals/<name> (tests/fake_adb.py).
        self._written = fake.state / "signals" / _WRITTEN
        self._probe = probe
        self._written_at: float | None = None

    def __call__(self) -> bool:
        """Return True once the run has nothing more to deliver."""
        probe = self._probe
        if probe.delivered >= probe.expected:
            return True
        now = time.perf_counter()
        if self._written_at is None:
            if not self._written.exists():
                return False
            self._written_at = now
        return now - max(probe.last_delivery, self._written_at) >= SETTLE_SECONDS


def run_timeout(count: int, consumer_cost: float) -> float:
    """Return how long a run of ``count`` lines may take before it counts as hung.

    The bound grows with the run's work: reading at ``MIN_LINES_PER_SECOND``
    and, under backpressure, a consumer that spends ``consumer_cost`` seconds
    on every entry. So a long but healthy run (``--lines 20000000``, or
    ``--slow-us 2000`` on a 50,000-line burst) is not aborted.
    """
    return RUN_TIMEOUT + count * (1.0 / MIN_LINES_PER_SECOND + consumer_cost)


def _poll(done: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() > deadline:
            msg = f"the run did not settle within {timeout:.0f} s"
            raise TimeoutError(msg)
        time.sleep(POLL_SECONDS)


async def _async_poll(done: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not done():
        if time.monotonic() > deadline:
            msg = f"the run did not settle within {timeout:.0f} s"
            raise TimeoutError(msg)
        await asyncio.sleep(POLL_SECONDS)


# -- drivers: the only code that knows the stream API ------------------------


class Driver(Protocol):
    """Runs one stream against the fake adb until ``done()``, then stops it."""

    name: ClassVar[str]

    def run(
        self,
        adb_path: str,
        probe: DeliveryProbe,
        *,
        lossless: bool,
        done: Callable[[], bool],
        timeout: float,
    ) -> None:
        """Deliver every entry to ``probe.hit``, poll ``done``, stop and join.

        ``lossless`` asks for settings under which the stream cannot drop an
        entry; otherwise the stream runs with its defaults. ``timeout``
        (``run_timeout``) bounds the poll and, separately, the join.
        """
        ...


class SyncDriver:
    """logpyt 2.x ``LogStream``: reader threads and one callback thread."""

    name: ClassVar[str] = "sync"

    def run(
        self,
        adb_path: str,
        probe: DeliveryProbe,
        *,
        lossless: bool,
        done: Callable[[], bool],
        timeout: float,
    ) -> None:
        """See ``Driver.run``."""
        stream = self._open(adb_path, probe, lossless=lossless)
        stream.start()
        try:
            _poll(done, timeout)
        finally:
            stream.stop()
            stream.join(timeout=timeout)

    @staticmethod
    def _open(adb_path: str, probe: DeliveryProbe, *, lossless: bool) -> LogStream:
        def on_entry(_entry: LogEntry, _handle: StreamHandle) -> None:
            probe.hit()

        if lossless:
            # 2.x drops once its callback queue is full, so the queue is sized
            # to hold the whole run.
            return LogStream(
                adb_path=adb_path,
                stdout_callback=on_entry,
                max_queue_size=probe.expected,
            )
        return LogStream(adb_path=adb_path, stdout_callback=on_entry)


class AsyncDriver:
    """logpyt 2.x ``AsyncLogStream`` under ``asyncio.run`` on the main thread."""

    name: ClassVar[str] = "async"

    def run(
        self,
        adb_path: str,
        probe: DeliveryProbe,
        *,
        lossless: bool,
        done: Callable[[], bool],
        timeout: float,
    ) -> None:
        """See ``Driver.run``."""
        asyncio.run(
            self._run(adb_path, probe, lossless=lossless, done=done, timeout=timeout)
        )

    async def _run(
        self,
        adb_path: str,
        probe: DeliveryProbe,
        *,
        lossless: bool,
        done: Callable[[], bool],
        timeout: float,
    ) -> None:
        stream = self._open(adb_path, probe, lossless=lossless)
        await stream.start()
        try:
            await _async_poll(done, timeout)
        finally:
            await stream.stop()
            await stream.join(timeout=timeout)

    @staticmethod
    def _open(adb_path: str, probe: DeliveryProbe, *, lossless: bool) -> AsyncLogStream:
        # The 2.x callback type is a coroutine function; this one has nothing
        # to await.
        async def on_entry(_entry: LogEntry, _handle: AsyncStreamHandle) -> None:  # noqa: RUF029
            probe.hit()

        if lossless:
            # Same as the sync driver: 2.x drops once a dispatch queue is full.
            return AsyncLogStream(
                adb_path=adb_path,
                stdout_callback=on_entry,
                callback_queue_size=probe.expected,
            )
        return AsyncLogStream(adb_path=adb_path, stdout_callback=on_entry)


DRIVERS: dict[str, Driver] = {"sync": SyncDriver(), "async": AsyncDriver()}


# -- cases ---------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Sample:
    """One run: what arrived, and the time from ``start()`` to its last delivery."""

    delivered: int
    expected: int
    seconds: float

    @property
    def lines_per_second(self) -> float:
        """Delivered entries per second; 0.0 when nothing arrived."""
        return self.delivered / self.seconds if self.seconds > 0 else 0.0


@dataclasses.dataclass(frozen=True)
class Case:
    """A scenario: backlog sizes, lossless or default settings, consumer cost."""

    name: str
    lossless: bool
    sizes: tuple[int, ...]
    consumer_cost: float = 0.0  # seconds of busy CPU per delivered entry

    @property
    def label(self) -> str:
        """The name, plus the consumer cost when there is one."""
        if self.consumer_cost <= 0:
            return self.name
        return f"{self.name} ({self.consumer_cost * 1e6:g} us/entry)"


@contextlib.contextmanager
def fake_adb_session() -> Iterator[FakeAdb]:
    """Yield a fresh fake adb; on exit its children are reaped and its files removed."""
    with (
        tempfile.TemporaryDirectory(
            prefix="logpyt-bench-", ignore_cleanup_errors=True
        ) as tmp,
        pytest.MonkeyPatch.context() as patch,
    ):
        fake = install_fake_adb(Path(tmp) / "fake-adb", patch)
        try:
            yield fake
        finally:
            fake.close()


def measure(driver: Driver, case: Case, count: int) -> Sample:
    """Run ``driver`` once on a backlog of ``count`` lines that adb writes at start.

    adb stays alive after the backlog (``hang``), so an end-of-stream loss
    (F1-1) cannot show up as a burst or throughput loss.
    """
    probe = DeliveryProbe(count, case.consumer_cost)
    with fake_adb_session() as fake:
        fake.logcat(run(lines(count, _LINE), touch(_WRITTEN), hang=True))
        started = time.perf_counter()
        driver.run(
            fake.path,
            probe,
            lossless=case.lossless,
            done=Settled(fake, probe),
            timeout=run_timeout(count, case.consumer_cost),
        )
    seconds = probe.last_delivery - started if probe.delivered else 0.0
    return Sample(probe.delivered, count, seconds)


# -- report --------------------------------------------------------------------

_TABLE_HEAD = (
    "| case | driver | lines | delivered/expected (median; min..max) "
    "| lines/s (median; min..max) |\n"
    "|---|---|---:|---:|---:|"
)


def format_row(case: Case, driver: Driver, samples: Sequence[Sample]) -> str:
    """Summarise the repetitions of one measurement as a Markdown table row."""
    expected = samples[0].expected
    delivered = [s.delivered for s in samples]
    rates = [s.lines_per_second for s in samples]
    share = 100.0 * statistics.median_low(delivered) / expected
    delivered_cell = (
        f"{statistics.median_low(delivered):,}/{expected:,} ({share:.1f} %;"
        f" {min(delivered):,}..{max(delivered):,})"
    )
    rate_cell = (
        f"{statistics.median(rates):,.0f} ({min(rates):,.0f}..{max(rates):,.0f})"
    )
    return (
        f"| {case.label} | {driver.name} | {expected:,} | {delivered_cell}"
        f" | {rate_cell} |"
    )


def environment_line(repeat: int) -> str:
    """Describe what produced the numbers: versions, platform, CPUs, repetitions."""
    return (
        f"logpyt {logpyt.__version__}; Python {platform.python_version()}"
        f" ({platform.python_implementation()}); {platform.platform()};"
        f" {os.cpu_count()} logical CPUs; {repeat} run(s) per row"
    )


# -- entry point ---------------------------------------------------------------


_CASE_NAMES = ("throughput", "burst", "slow-burst")


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        msg = f"must be at least 1, got {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and value > 0):
        msg = f"must be a finite number greater than 0, got {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line (see the module docstring)."""
    parser = argparse.ArgumentParser(
        prog="python -m bench.stream_bench",
        description="Measure LogStream/AsyncLogStream on the fake adb.",
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=_CASE_NAMES,
        default=list(_CASE_NAMES),
        help="cases to run (default: all)",
    )
    parser.add_argument(
        "--drivers",
        nargs="+",
        choices=tuple(DRIVERS),
        default=list(DRIVERS),
        help="streams to measure (default: both)",
    )
    parser.add_argument(
        "--lines",
        type=_positive_int,
        default=DEFAULT_LINES,
        help=f"backlog of the throughput case (default: {DEFAULT_LINES})",
    )
    parser.add_argument(
        "--burst-lines",
        nargs="+",
        type=_positive_int,
        default=list(DEFAULT_BURST_LINES),
        help="burst sizes of the burst case (default: %(default)s)",
    )
    parser.add_argument(
        "--slow-us",
        type=_positive_float,
        default=DEFAULT_SLOW_US,
        help="consumer CPU per entry in the slow-burst case, in microseconds"
        f" (default: {DEFAULT_SLOW_US:g})",
    )
    parser.add_argument(
        "--repeat",
        type=_positive_int,
        default=DEFAULT_REPEAT,
        help=f"runs per row; the median is reported (default: {DEFAULT_REPEAT})",
    )
    return parser.parse_args(argv)


def run_benchmark(args: argparse.Namespace) -> None:
    """Run every selected row and print the report as it goes."""
    bursts = tuple(args.burst_lines)
    cases = {
        "throughput": Case("throughput", lossless=True, sizes=(args.lines,)),
        "burst": Case("burst", lossless=False, sizes=bursts),
        "slow-burst": Case(
            "slow-burst",
            lossless=False,
            sizes=bursts,
            consumer_cost=args.slow_us / 1e6,
        ),
    }
    print(environment_line(args.repeat))
    print()
    print(_TABLE_HEAD, flush=True)
    for case in (cases[name] for name in args.cases):
        for size in case.sizes:
            for driver in (DRIVERS[name] for name in args.drivers):
                samples = [measure(driver, case, size) for _ in range(args.repeat)]
                print(format_row(case, driver, samples), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit status."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        run_benchmark(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return _EXIT_INTERRUPTED
    return 0


if __name__ == "__main__":
    sys.exit(main())
