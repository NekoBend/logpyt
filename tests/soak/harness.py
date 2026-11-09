"""The monitoring soak's harness (design 9.1): scenario, samples and criteria.

One run drives one stream (``sync``: `LogStream`; ``async``: `AsyncLogStream`
on an event loop in a helper thread) against the fake adb's simulated device
(``FakeAdb.device``) for ``Plan.duration`` seconds:

- a writer thread logs a steady rate of ``seq=N`` lines (tag ``Soak``, a
  system uid) and a few ``app=N`` lines (tag ``App``) from the uid of a
  watched package; the stream's ``where`` is ``Tag("Soak") | Package(PKG)``,
  so the app lines take the uid lookup;
- every adb logcat runs a scripted behaviour, in turn: exit 0, exit 1 (an
  ``Unexpected EOF`` after backpressure is A.6's resume), a stderr burst then
  exit 255, a hang after its output (``read_timeout``), a kill (-9), and an
  adb that waits without output as for an absent device (``connect_timeout``,
  then the state check fails);
- a director thread, every ``Plan.pace`` seconds, ends the live connection
  the way its run says; or steps the device clock back, in turn by less than
  ``resume_overlap`` (absorbed by the overlap) and, while a connection
  streams, by more (``Plan.clock_jump``: a step seen live, design A.25, one
  ``Gap(clock_moved_back)`` each; the old lines' later replays are dropped,
  so the "no duplicate" criterion holds without design 5.6's exception);
  changes the package's uid (a reinstall: the next lookup answers the new
  uid); or stalls the consumer: briefly (a stall resume whose ring still
  holds hw) or long enough for the ring to overwrite hw (the resume reports
  ``Gap(ring_rotated)``);
- a sampler records, every ``Plan.sample_every`` seconds: ``tracemalloc``
  (all traced memory, and the part allocated in ``logpyt``), RSS and open fds
  (Linux), threads, asyncio tasks, live fake-adb children, and the stream's
  counters; a watcher records thread, task and fd counts while the stream is
  RECONNECTING with no adb child alive (design A.20: "with no one-shot
  command outstanding"), and how long any fake adb child outlives its
  connection.

`Result.failures` lists every criterion that failed (`judge`); the numbers
behind them are in `Result.summary()`.

The harness keeps its own bookkeeping small, because ``tracemalloc`` sees it:
delivered sequence numbers in a bytearray, events as tuples, no log records
(the ``logpyt`` loggers go to a counting handler), and the fake's registry
drops exited children (``FakeAdb.forget_exited``).
"""

from __future__ import annotations

import array
import asyncio
import bisect
import contextlib
import dataclasses
import logging
import os
import sys
import threading
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import logpyt
from logpyt import (
    AsyncLogStream,
    Gap,
    LogStream,
    Package,
    Reconnect,
    StateChange,
    StreamConfig,
    StreamState,
    Tag,
)
from tests.conftest import (
    dumpsys_package,
    err,
    lines,
    out,
    run,
    stream,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from logpyt import LogEntry, StreamEvent, StreamStats
    from tests.conftest import FakeAdb, Run, SpawnedChild

SEQ_TAG = "Soak"
APP_TAG = "App"
PKG = "com.example.soak"
SYSTEM_UID = 1000
FIRST_APP_UID = 10_065
MIB = 1024 * 1024
# The runs of successive adb logcat spawns, in turn (module docs).
BEHAVIOURS = ("rc0", "rc1", "stderr", "hang", "kill9", "absent")
# The director's actions, in turn; "end" ends the live connection.
ACTIONS = (
    "end",
    "stall_short",
    "end",
    "clock_back",
    "end",
    "reinstall",
    "end",
    "stall_long",
)
# Design 5.6 COMMAND_TIMEOUT: the one-shot runner's bound.
COMMAND_TIMEOUT = 5.0
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _us(when: datetime) -> int:
    return (when - _EPOCH) // _MICROSECOND


@dataclasses.dataclass(frozen=True, slots=True)
class Plan:
    """The soak's parameters; `Plan.of` scales the time windows to a duration.

    The values are the ones PR-14 tuned (design 9.1 asked for that): the rate
    and ring give the ring 6 s of history, so a reconnect (delay up to 0.5 s)
    keeps hw in it and only the long stall and the absent device overwrite it;
    ``buffer_entries`` makes a stalled consumer close intake within a second.
    """

    duration: float
    rate: int = 200  # seq lines per second
    app_every: float = 0.2  # seconds between app lines
    ring: int = 1200
    pace: float = 3.0  # seconds between the director's actions
    short_stall: float = 2.5
    long_stall: float = 9.0
    clock_step: float = -0.5  # below resume_overlap - 1 s (the clock's resolution)
    # Above max(resume_overlap, 1 s): a step seen live. Small enough that the
    # resumes after it are past the old lines before the next long stall.
    clock_jump: float = -4.0
    sample_every: float = 60.0
    warmup: float = 600.0
    window: float = 1800.0
    growth_limit: int = 4 * MIB  # last window's max over the first window's
    ceiling: int = 64 * MIB  # over the baseline, at any sample

    @classmethod
    def of(cls, duration: float) -> Plan:
        """Design 9.1's windows, scaled down for a run shorter than 2 h:
        warm-up min(10 min, duration / 4), windows min(30 min, a third of the
        rest), a sample every min(60 s, duration / 24)."""
        warmup = min(600.0, duration / 4)
        return cls(
            duration=duration,
            sample_every=min(60.0, duration / 24),
            warmup=warmup,
            window=min(1800.0, (duration - warmup) / 3),
        )

    def config(self) -> StreamConfig:
        return StreamConfig(
            reconnect=Reconnect(delay=0.1, max_delay=0.5, stable_after=2.0),
            read_timeout=1.0,
            connect_timeout=1.0,
            buffer_entries=200,
            stop_grace=2.0,
        )


# -- the device side ---------------------------------------------------------------


def logcat_runs(count: int) -> list[Run]:
    """The scripted run of each of ``count`` adb logcat spawns (module docs)."""
    runs: list[Run] = []
    for i in range(count):
        cut = stream(until=f"c{i}")
        behaviour = BEHAVIOURS[i % len(BEHAVIOURS)]
        if behaviour == "rc0":
            runs.append(run(cut, exit_code=0))
        elif behaviour == "rc1":
            runs.append(run(cut, exit_code=1))
        elif behaviour == "stderr":
            burst = lines(200, f"adb: noise {i} {{i}}\n", stream="err")
            runs.append(run(burst, cut, exit_code=255))
        elif behaviour == "hang":
            runs.append(run(cut, hang=True))
        elif behaviour == "kill9":
            runs.append(run(stream()))
        else:  # absent: adb waits for the device, the state check fails
            runs.append(run(err("- waiting for device -\n"), hang=True))
    return runs


class Writer:
    """Logs the seq and app lines at the plan's rate (a thread)."""

    def __init__(self, fake: FakeAdb, plan: Plan, quiet: threading.Lock) -> None:
        self.fake = fake
        self.plan = plan
        self.quiet = quiet
        self.seqs = 0  # seq lines logged
        self.apps = 0
        self.app_uid = FIRST_APP_UID
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="soak: writer")

    def _run(self) -> None:
        tick = 0.01
        per_tick = max(1, round(self.plan.rate * tick))
        due = time.monotonic()
        next_app = due
        while not self.stop.is_set():
            first = self.seqs
            with self.quiet:
                self.fake.device_log(
                    *[f"seq={first + j}" for j in range(per_tick)],
                    tag=SEQ_TAG,
                    uid=SYSTEM_UID,
                    pid=2000,
                )
                self.seqs += per_tick
                if time.monotonic() >= next_app:
                    self.fake.device_log(
                        f"app={self.apps}", tag=APP_TAG, uid=self.app_uid, pid=3000
                    )
                    self.apps += 1
                    next_app += self.plan.app_every
            due += tick
            self.stop.wait(max(0.0, due - time.monotonic()))


# -- the consumer ------------------------------------------------------------------


class Consumer:
    """Takes the items and events; can be stalled for a while (`stall`)."""

    def __init__(self) -> None:
        self.seen = bytearray()  # per seq: how often delivered (capped at 255)
        self.apps = bytearray()
        self.duplicates = 0
        self.app_duplicates = 0
        self.first_duplicates: list[int] = []
        self.others = 0
        self.items = 0
        self.stall_until = 0.0
        self.stalls = 0
        # Gaps as (after us or -1, before us, reason); state changes counted.
        self.gaps: list[tuple[int, int, str]] = []
        self.changes = 0
        self.reconnecting = 0
        self.errors: dict[str, int] = {}

    def stall(self, seconds: float) -> None:
        self.stall_until = time.monotonic() + seconds
        self.stalls += 1

    @staticmethod
    def _mark(table: bytearray, n: int) -> bool:
        """Count ``n`` in ``table``; True if it was delivered before."""
        if n >= len(table):
            table.extend(bytes(max(n + 1 - len(table), 64 * 1024)))
        before = table[n]
        table[n] = min(before + 1, 255)
        return before > 0

    def take(self, entry: LogEntry) -> None:
        self.items += 1
        message = entry.message
        if entry.tag == SEQ_TAG and message.startswith("seq="):
            n = int(message[4:])
            if self._mark(self.seen, n):
                self.duplicates += 1
                if len(self.first_duplicates) < 20:
                    self.first_duplicates.append(n)
        elif entry.tag == APP_TAG and message.startswith("app="):
            self.app_duplicates += self._mark(self.apps, int(message[4:]))
        else:
            self.others += 1

    def sync_item(self, entry: LogEntry) -> None:
        while time.monotonic() < self.stall_until:
            time.sleep(0.01)
        self.take(entry)

    async def async_item(self, entry: LogEntry) -> None:
        while time.monotonic() < self.stall_until:
            await asyncio.sleep(0.01)
        self.take(entry)

    def event(self, event: StreamEvent) -> None:
        if isinstance(event, Gap):
            after = -1 if event.after is None else _us(event.after)
            before = _us(event.before) if event.before is not None else 2**62
            self.gaps.append((after, before, event.reason))
        elif isinstance(event, StateChange):
            self.changes += 1
            if event.new is StreamState.RECONNECTING:
                self.reconnecting += 1
                name = "stall" if event.error is None else type(event.error).__name__
                self.errors[name] = self.errors.get(name, 0) + 1


class LogCounter(logging.Handler):
    """Counts the ``logpyt`` log records by level, keeping none (module docs)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.counts: dict[str, int] = {}

    def emit(self, record: logging.LogRecord) -> None:
        self.counts[record.levelname] = self.counts.get(record.levelname, 0) + 1


# -- the stream, per driver -----------------------------------------------------------


class SyncRunner:
    """A `LogStream` in push mode."""

    def __init__(self, config: StreamConfig, consumer: Consumer) -> None:
        self.stream: Any = LogStream(
            config,
            where=Tag(SEQ_TAG) | Package(PKG),
            on_item=consumer.sync_item,
            on_event=consumer.event,
        )

    def start(self) -> None:
        self.stream.start()

    def finish(self, timeout: float) -> None:
        self.stream.stop()
        self.stream.join(timeout)

    def tasks(self) -> int | None:
        return None

    def close(self) -> None:
        pass


class AsyncRunner:
    """An `AsyncLogStream` on an event loop of its own thread."""

    def __init__(self, config: StreamConfig, consumer: Consumer) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="soak: event loop", daemon=True
        )
        self.thread.start()
        self.stream: Any = AsyncLogStream(
            config,
            where=Tag(SEQ_TAG) | Package(PKG),
            on_item=consumer.async_item,
            on_event=consumer.event,
        )

    def _call[R](self, make: Callable[[], Coroutine[Any, Any, R]], timeout: float) -> R:
        return asyncio.run_coroutine_threadsafe(make(), self.loop).result(timeout)

    def start(self) -> None:
        async def start() -> None:
            self.stream.start()

        self._call(start, 10.0)

    def finish(self, timeout: float) -> None:
        self.stream.stop()

        async def join() -> None:
            await self.stream.join(timeout)

        self._call(join, timeout + 10.0)

    def tasks(self) -> int | None:
        async def count() -> int:
            return len(asyncio.all_tasks()) - 1  # without this one

        return self._call(count, 10.0)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10.0)
        if not self.thread.is_alive():
            self.loop.close()


type Runner = SyncRunner | AsyncRunner


# -- measurements ------------------------------------------------------------------


def _fds() -> int | None:
    if sys.platform != "linux":
        return None
    return sum(1 for _ in Path("/proc/self/fd").iterdir())


def _rss() -> int | None:
    if sys.platform != "linux":
        return None
    resident = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
    return resident * os.sysconf("SC_PAGE_SIZE")


_PACKAGE = str(Path(logpyt.__file__).parent / "*")


def _logpyt_traced() -> int:
    """The traced memory allocated in ``logpyt``'s own files (reported only)."""
    snapshot = tracemalloc.take_snapshot().filter_traces(
        [tracemalloc.Filter(inclusive=True, filename_pattern=_PACKAGE)]
    )
    return sum(stat.size for stat in snapshot.statistics("filename"))


@dataclasses.dataclass(slots=True)
class Quiet:
    """Thread, task and fd counts in one RECONNECTING with no adb child alive."""

    at: float
    threads: int
    tasks: int | None
    fds: int | None

    def key(self) -> tuple[int, int | None, int | None]:
        return (self.threads, self.tasks, self.fds)


class Watcher:
    """Samples while RECONNECTING, and checks how long fake adb children live."""

    def __init__(
        self,
        fake: FakeAdb,
        runner: Runner,
        plan: Plan,
        quiet: threading.Lock,
        began: float,
    ) -> None:
        self.fake = fake
        self.runner = runner
        self.plan = plan
        self.quiet = quiet
        self.began = began
        self.windows: list[Quiet] = []
        self.unmeasured = 0  # RECONNECTING windows with a child alive throughout
        self.overstays: list[str] = []
        self.allowance = plan.config().stop_grace + 1.0  # design 9.1
        self.first_seen: dict[int, float] = {}  # pid -> monotonic
        self.superseded: dict[int, float] = {}  # logcat pid -> a newer one's spawn
        self.newest = -1  # the highest logcat invocation seen
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="soak: watcher")

    def _run(self) -> None:
        next_check = 0.0
        in_window = False
        best: Quiet | None = None
        while not self.stop.wait(0.005):
            now = time.monotonic()
            if now >= next_check:
                self._children(now)
                next_check = now + 0.25
            reconnecting = self.runner.stream.state is StreamState.RECONNECTING
            if reconnecting and not in_window:
                in_window, best = True, None
            if in_window and reconnecting and not self.fake.live_children():
                best = self._measure(now, best)
            if in_window and not reconnecting:
                in_window = False
                if best is None:
                    self.unmeasured += 1
                else:
                    self.windows.append(best)

    def _measure(self, now: float, best: Quiet | None) -> Quiet:
        with self.quiet:
            sample = Quiet(
                at=now - self.began,
                threads=threading.active_count(),
                tasks=self.runner.tasks(),
                fds=_fds(),
            )
        baseline = self.baseline()
        if best is None or (baseline is not None and sample.key() == baseline):
            return sample
        return best

    def baseline(self) -> tuple[int, int | None, int | None] | None:
        """The counts of the first RECONNECTING window after the warm-up."""
        return next((w.key() for w in self.windows if w.at >= self.plan.warmup), None)

    def _children(self, now: float) -> None:
        """No fake adb child outlives its connection by more than stop_grace +
        1 s (a logcat, from the spawn of the next one), and no one-shot command
        runs longer than its timeout plus that (design 9.1)."""
        allowance = self.allowance
        children = self.fake.children()
        for child in children:
            self.first_seen.setdefault(child.pid, now)
            if child.command == "logcat":
                self.newest = max(self.newest, child.index)
        for child in children:
            if child.command == "logcat" and child.index < self.newest:
                self.superseded.setdefault(child.pid, now)
        for child in children:
            if not child.alive():
                continue
            if child.command == "logcat":
                since = self.superseded.get(child.pid)
                late = since is not None and now - since > allowance
            else:
                late = now - self.first_seen[child.pid] > COMMAND_TIMEOUT + allowance
            if late and len(self.overstays) < 50:
                self.overstays.append(
                    f"{child.command} #{child.index} pid {child.pid} at "
                    f"{now - self.began:.1f} s"
                )
        if self.fake.forget_exited():
            alive = {c.pid for c in self.fake.children()}
            self.first_seen = {p: t for p, t in self.first_seen.items() if p in alive}
            self.superseded = {p: t for p, t in self.superseded.items() if p in alive}


@dataclasses.dataclass(slots=True)
class Sample:
    """One sample of design 9.1's list."""

    at: float
    traced: int
    traced_logpyt: int
    rss: int | None
    fds: int | None
    threads: int
    tasks: int | None
    children: int
    state: str
    lines: int
    reconnects: int
    stall_resumes: int
    gaps: int


def sample(runner: Runner, fake: FakeAdb, began: float) -> Sample:
    stats: StreamStats = runner.stream.stats
    return Sample(
        at=round(time.monotonic() - began, 1),
        traced=tracemalloc.get_traced_memory()[0],
        traced_logpyt=_logpyt_traced(),
        rss=_rss(),
        fds=_fds(),
        threads=threading.active_count(),
        tasks=runner.tasks(),
        children=len(fake.live_children()),
        state=str(runner.stream.state),
        lines=stats.lines,
        reconnects=stats.reconnects,
        stall_resumes=stats.stall_resumes,
        gaps=stats.gaps,
    )


# -- the scenario ------------------------------------------------------------------


class Director:
    """Ends connections, steps the clock, reinstalls, stalls (module docs)."""

    def __init__(
        self,
        fake: FakeAdb,
        plan: Plan,
        writer: Writer,
        consumer: Consumer,
        quiet: threading.Lock,
    ) -> None:
        self.fake = fake
        self.plan = plan
        self.writer = writer
        self.consumer = consumer
        self.quiet = quiet
        self.done: dict[str, int] = {}
        self.released: set[int] = set()  # the logcat runs whose cut was released
        self.jump = False  # whether the next clock step is a jump
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="soak: director")

    def _run(self) -> None:
        turn = 0
        while not self.stop.wait(self.plan.pace):
            action = ACTIONS[turn % len(ACTIONS)]
            turn += 1
            with self.quiet:
                kind = self._act(action)
            self.done[kind] = self.done.get(kind, 0) + 1
            if action.startswith("stall"):
                self.stop.wait(self.consumer.stall_until - time.monotonic())

    def _act(self, action: str) -> str:
        if action == "end":
            return self._end()
        if action == "clock_back":
            return self._clock_back()
        if action == "reinstall":
            self.writer.app_uid += 1
            answer = out(dumpsys_package(PKG, self.writer.app_uid))
            self.fake.dumpsys(PKG, run(answer))
        elif action == "stall_short":
            self.consumer.stall(self.plan.short_stall)
        else:
            self.consumer.stall(self.plan.long_stall)
        return action

    def _clock_back(self) -> str:
        """Every other time, a jump back while an adb logcat streams, so the
        stream reads the lines after it on that connection; else a small step."""
        self.jump = not self.jump
        live: list[SpawnedChild] = self.fake.live_children("logcat")
        streaming = bool(live) and (
            BEHAVIOURS[live[-1].index % len(BEHAVIOURS)] != "absent"
            and live[-1].index not in self.released
        )
        if self.jump and streaming:
            self.fake.step_clock(self.plan.clock_jump)
            return "clock_jump"
        self.fake.step_clock(self.plan.clock_step)
        return "clock_back"

    def _end(self) -> str:
        live: list[SpawnedChild] = self.fake.live_children("logcat")
        if not live:
            return "end: no adb logcat"
        child = live[-1]
        behaviour = BEHAVIOURS[child.index % len(BEHAVIOURS)]
        if behaviour == "kill9":
            with contextlib.suppress(OSError):
                child.popen.kill()
        elif behaviour != "absent":
            self.fake.release(f"c{child.index}")
        self.released.add(child.index)
        return f"end: {behaviour}"


@dataclasses.dataclass(slots=True)
class Result:
    """What one soak run found."""

    driver: str
    outcome: str
    plan: Plan
    samples: list[Sample]
    windows: list[Quiet]
    unmeasured: int
    overstays: list[str]
    consumer: Consumer
    actions: dict[str, int]
    logs: dict[str, int]
    stats: StreamStats
    written: int
    app_written: int
    missing: int = 0
    spawns: int = 0  # adb logcat spawns
    state_checks: int = 0  # adb get-state (the absent device's)
    uncovered: list[int] = dataclasses.field(default_factory=list)
    app_missing: int = 0
    failures: list[str] = dataclasses.field(default_factory=list)

    def summary(self) -> dict[str, object]:
        stats = self.stats
        reasons: dict[str, int] = {}
        for _, _, reason in self.consumer.gaps:
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "driver": self.driver,
            "duration_s": self.plan.duration,
            "outcome": self.outcome,
            "failures": self.failures,
            "written": self.written,
            "delivered_seqs": self.written - self.missing,
            "missing_seqs": self.missing,
            "missing_outside_gaps": self.uncovered[:20],
            "duplicates": self.consumer.duplicates,
            "app_lines": {
                "written": self.app_written,
                "missing": self.app_missing,
                "duplicates": self.consumer.app_duplicates,
            },
            "gaps": reasons,
            "reconnecting_causes": self.consumer.errors,
            "actions": self.actions,
            "logcat_spawns": self.spawns,
            "state_checks": self.state_checks,
            "logs": self.logs,
            "stats": {
                "connections": stats.connections,
                "reconnects": stats.reconnects,
                "stall_resumes": stats.stall_resumes,
                "backpressure_seconds": round(stats.backpressure_seconds, 1),
                "lines": stats.lines,
                "delivered": stats.delivered,
                "filtered": stats.filtered,
                "deduplicated": stats.deduplicated,
                "below_cursor": stats.below_cursor,
                "unattributed": stats.unattributed,
                "stderr_lines": stats.stderr_lines,
                "events_coalesced": stats.events_coalesced,
            },
            "reconnecting_windows": {
                "measured": len(self.windows),
                "unmeasured": self.unmeasured,
                "counts": sorted({w.key() for w in self.windows}, key=str),
            },
            "child_overstays": self.overstays,
            "samples": [dataclasses.asdict(s) for s in self.samples],
        }


class _Logs:
    """The ``logpyt`` loggers go to a counting handler, at INFO, for the run."""

    def __init__(self) -> None:
        self.counter = LogCounter()
        self.logger = logging.getLogger("logpyt")
        self.saved = (self.logger.propagate, self.logger.level)

    def __enter__(self) -> LogCounter:
        self.logger.addHandler(self.counter)
        self.logger.propagate = False  # pytest's capture would keep every record
        self.logger.setLevel(logging.INFO)
        return self.counter

    def __exit__(self, *exc: object) -> None:
        self.logger.removeHandler(self.counter)
        self.logger.propagate, level = self.saved
        self.logger.setLevel(level)


class _Soak:
    """One run: the threads, the stream and the samples (`run_soak`)."""

    def __init__(self, fake: FakeAdb, plan: Plan, driver: str) -> None:
        self.fake, self.plan, self.driver = fake, plan, driver
        self.quiet = threading.Lock()
        self.consumer = Consumer()
        self.runner: Runner = (
            SyncRunner(plan.config(), self.consumer)
            if driver == "sync"
            else AsyncRunner(plan.config(), self.consumer)
        )
        self.writer = Writer(fake, plan, self.quiet)
        self.director = Director(fake, plan, self.writer, self.consumer, self.quiet)
        self.began = time.monotonic()
        self.watcher = Watcher(fake, self.runner, plan, self.quiet, self.began)
        self.samples = [sample(self.runner, fake, self.began)]
        self.tail: str | None = None
        self.stop_failures: list[str] = []  # how the final stop went (`end`)

    def run(self) -> None:
        plan, began = self.plan, self.began
        self.runner.start()
        for worker in (self.writer, self.director, self.watcher):
            worker.thread.start()
        while (left := began + plan.duration - time.monotonic()) > 0:
            time.sleep(min(plan.sample_every, left))
            self.samples.append(sample(self.runner, self.fake, began))
        self.director.stop.set()
        self.director.thread.join(plan.long_stall + 10)
        self.writer.stop.set()
        self.writer.thread.join(10)
        last, seen = self.writer.seqs - 1, self.consumer.seen
        try:
            wait_until(lambda: last < len(seen) and seen[last] > 0, timeout=60)
        except TimeoutError:
            self.tail = (
                f"the last seq ({last}) was not delivered within 60 s of the end"
            )

    def end(self) -> None:
        """Stop the run and judge the stop (design 9.1): it ends STOPPED, and
        no adb logcat outlives it by more than stop_grace plus 1 s."""
        self.director.stop.set()
        self.writer.stop.set()
        self.consumer.stall_until = 0.0
        failures = self.stop_failures
        try:
            self.runner.finish(30.0)
        except Exception as exc:  # noqa: BLE001  (a failed stop is a criterion)
            failures.append(f"the stop failed: {type(exc).__name__}: {exc}")
        state = self.runner.stream.state
        if state is not StreamState.STOPPED:
            failures.append(f"the run was {state.name} after the stop, not STOPPED")
        grace = self.plan.config().stop_grace + 1.0
        left = self.fake.survivors(timeout=grace, command="logcat")
        if left:
            failures.append(
                f"adb logcat outlived the stop by stop_grace + 1 s: "
                f"{[child.index for child in left]}"
            )
        self.watcher.stop.set()
        if self.watcher.thread.is_alive():
            self.watcher.thread.join(10)
        self.runner.close()

    def result(self, logs: dict[str, int]) -> Result:
        stream = self.runner.stream
        result = Result(
            driver=self.driver,
            outcome=repr(stream.outcome),
            plan=self.plan,
            samples=self.samples,
            windows=self.watcher.windows,
            unmeasured=self.watcher.unmeasured,
            overstays=self.watcher.overstays,
            consumer=self.consumer,
            actions=self.director.done,
            logs=logs,
            stats=stream.stats,
            written=self.writer.seqs,
            app_written=self.writer.apps,
        )
        if self.tail is not None:
            result.failures.append(self.tail)
        result.failures.extend(self.stop_failures)
        if stream.outcome is not None:
            result.failures.append(f"the run ended {result.outcome}")
        return result


def run_soak(fake: FakeAdb, plan: Plan, driver: str) -> Result:
    """Run the soak for ``plan.duration`` seconds; judge it (`judge`)."""
    fake.device(ring=plan.ring)
    fake.dumpsys(PKG, run(out(dumpsys_package(PKG, FIRST_APP_UID))))
    fake.logcat(*logcat_runs(int(plan.duration / plan.pace * 3) + 200))
    with _Logs() as logs:
        tracemalloc.start()
        try:
            soak = _Soak(fake, plan, driver)
            try:
                soak.run()
            finally:
                soak.end()
        finally:
            tracemalloc.stop()
    result = soak.result(logs.counts)
    judge(result, fake, soak.watcher.baseline())
    return result


def _stamps(fake: FakeAdb, count: int) -> array.array[int]:
    """The device time of every seq line, by number."""
    stamps = array.array("q", bytes(8 * count))
    marker = f" {SEQ_TAG}: seq="
    for micros, line in fake.device_entries():
        at = line.find(marker)
        if at >= 0:
            n = int(line[at + len(marker) :])
            if n < count:
                stamps[n] = micros
    return stamps


def judge(
    result: Result,
    fake: FakeAdb,
    baseline: tuple[int, int | None, int | None] | None,
) -> None:
    """Design 9.1's pass criteria, with the values PR-14 tuned (`Plan`)."""
    _judge_memory(result)
    _judge_counts(result, baseline)
    _judge_sequence(result, fake)
    stats, failures = result.stats, result.failures
    accounted = (  # line conservation (design A.20)
        stats.delivered
        + stats.undelivered
        + stats.filtered
        + stats.before_start
        + stats.deduplicated
        + stats.below_cursor
    )
    if stats.lines != accounted:
        failures.append(f"line conservation: lines {stats.lines} != {accounted}")
    # The scenario ran: every behaviour, every action, and the stall resume.
    result.spawns = len(fake.calls("logcat"))
    result.state_checks = len(fake.calls("get-state"))
    if result.spawns < len(BEHAVIOURS):
        failures.append(f"{result.spawns} adb logcat spawns: not every behaviour ran")
    if not result.state_checks:
        failures.append("no state check ran (the absent device's adb logcat)")
    for needed in (
        "stall_long",
        "stall_short",
        "clock_back",
        "clock_jump",
        "reinstall",
    ):
        if not result.actions.get(needed):
            failures.append(f"the director never ran {needed}")
    # Each jump is one step seen live; a small step is no Gap (design A.25).
    jumps = result.actions.get("clock_jump", 0)
    steps = sum(reason == "clock_moved_back" for _, _, reason in result.consumer.gaps)
    if steps != jumps:
        failures.append(f"{steps} Gap(clock_moved_back) for {jumps} clock jumps")
    # Every adb ending of 9.1 (the absent device's is the state check above).
    for behaviour in BEHAVIOURS:
        if behaviour != "absent" and not result.actions.get(f"end: {behaviour}"):
            failures.append(f"the director never ended a connection by {behaviour}")
    if not stats.stall_resumes:
        failures.append("no stall resume ran")
    if not any(reason == "ring_rotated" for _, _, reason in result.consumer.gaps):
        failures.append("no Gap(ring_rotated): the long stall never overwrote hw")


def _judge_memory(result: Result) -> None:
    """After the warm-up, the last window's tracemalloc maximum is at most
    ``growth_limit`` above the first window's, and no sample is more than
    ``ceiling`` above the baseline (all traced memory)."""
    plan, samples = result.plan, result.samples
    base = samples[0].traced
    after = [s for s in samples if s.at >= plan.warmup]
    first = [s.traced for s in after if s.at <= plan.warmup + plan.window]
    last = [s.traced for s in after if s.at >= plan.duration - plan.window]
    if first and last and max(last) > max(first) + plan.growth_limit:
        result.failures.append(
            f"tracemalloc grew: last window max {max(last)} > first window max "
            f"{max(first)} + {plan.growth_limit}"
        )
    peak = max(s.traced for s in samples)
    if peak > base + plan.ceiling:
        result.failures.append(f"tracemalloc {peak} > baseline {base} + {plan.ceiling}")


def _judge_counts(
    result: Result, baseline: tuple[int, int | None, int | None] | None
) -> None:
    """Thread, task and fd counts while RECONNECTING equal the baseline after
    the warm-up; no fake adb child outlived its connection."""
    judged = [w for w in result.windows if w.at >= result.plan.warmup]
    off = [w for w in judged if w.key() != baseline]
    if baseline is None:
        result.failures.append("no RECONNECTING window was measured after the warm-up")
    elif off:
        result.failures.append(
            f"counts while RECONNECTING left the baseline {baseline}: "
            f"{[(round(w.at), w.key()) for w in off[:10]]}"
        )
    if result.overstays:
        result.failures.append(
            f"fake adb children outlived their connection: {result.overstays}"
        )


def _judge_sequence(result: Result, fake: FakeAdb) -> None:
    """No seq delivered twice; every missing seq inside a reported Gap."""
    consumer = result.consumer
    if consumer.duplicates:
        result.failures.append(
            f"{consumer.duplicates} seqs delivered twice, "
            f"first {consumer.first_duplicates}"
        )
    seen = consumer.seen
    missing = [n for n in range(result.written) if n >= len(seen) or not seen[n]]
    result.missing = len(missing)
    if missing:
        stamps = _stamps(fake, result.written)
        spans = sorted((a, b) for a, b, _ in consumer.gaps)
        starts = [a for a, _ in spans]
        for n in missing:
            ts = stamps[n]
            at = bisect.bisect_right(starts, ts)
            if not any(b >= ts for _, b in spans[:at]):
                result.uncovered.append(n)
        if result.uncovered:
            result.failures.append(
                f"{len(result.uncovered)} missing seqs outside every Gap, "
                f"first {result.uncovered[:10]}"
            )
    apps = consumer.apps
    result.app_missing = sum(
        1 for n in range(result.app_written) if n >= len(apps) or not apps[n]
    )
    if consumer.app_duplicates:
        result.failures.append(f"{consumer.app_duplicates} app lines delivered twice")
