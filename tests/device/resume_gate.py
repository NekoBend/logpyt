"""The machinery of the resume device gates (design 9.4; ``test_resume_gate.py``).

What a stream delivered (`Capture`), the pass criteria as pure functions
(`check_sequence`, `notice_times`; unit-tested without a device in
``tests/test_resume_gate_checks.py``), what a gate needs of the device
(`Bench`, `DeviceBench`), and the gates themselves (`run_resume_gate`,
`run_overwrite_gate`), which take a `Bench` so that they can be dry-run
against the fake adb.

The writers run on the device, detached from adb (``nohup``), so they outlive
the disconnects a gate makes: ``logwrapper sh -c <loop>`` logs each line of
the loop with tag ``sh``, uid shell and one pid (FINDINGS.md check 11). Each
run has its own id in its lines, so lines of other runs are ignored.
"""

from __future__ import annotations

import bisect
import contextlib
import dataclasses
import os
import re
import secrets
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from logpyt import (
    Gap,
    LogEntry,
    LogStream,
    Reconnect,
    StateChange,
    StreamConfig,
    StreamError,
    StreamState,
)
from logpyt.parsers import ParseContext, ThreadTimeLogParser
from tests.conftest import wait_until
from tools import device_probe

if TYPE_CHECKING:
    from collections.abc import Sequence

    from logpyt import StreamEvent

US = 1_000_000
GATE_SECONDS = float(os.environ.get("LOGPYT_GATE_SECONDS", "600"))
STALL_SECONDS = float(os.environ.get("LOGPYT_GATE_STALL", "60"))
DISCONNECTS = 10  # design 9.4: 10 disconnects per run
BURST = 8  # K byte-identical burst lines after each seq line
PERIOD = 0.01  # the writer's pause between seq lines (10 ms)
WAIT = 60.0  # bound of every wait for the device or the stream
# Below the watchdog of the gate tests (their marker), which exits pytest
# without finalizers: the run, the stop and the analysis.
SLACK = 300.0

_LINE = re.compile(r"^(seq|burst|end)=([0-9a-f]{8})-(\d+)$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _us(when: datetime) -> int:
    return (when - _EPOCH) // _MICROSECOND


# -- what a stream delivered ---------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Seen:
    """One writer line as delivered: ``seq``, ``burst`` or ``end``, its number,
    and its device time in epoch microseconds."""

    kind: str
    n: int
    ts: int


class Capture:
    """The writer lines (of run ``rid``), Gaps and state changes a stream delivered.

    ``on_item``/``on_event`` are the stream's callbacks; ``stall`` (seconds)
    makes the consumer stop at the first writer line for that long, as the
    overwrite gate's slow consumer.
    """

    def __init__(self, rid: str, *, stall: float = 0.0) -> None:
        self.rid = rid
        self.lines: list[Seen] = []
        self.events: list[StreamEvent] = []
        self.stall = stall
        self.stalled = threading.Event()  # the consumer entered its stall
        self.resumed = threading.Event()  # and left it
        self.abort = threading.Event()  # ends a stall early (test teardown)
        self.other_lines = 0

    def on_item(self, entry: LogEntry) -> None:
        found = _LINE.match(entry.message.rstrip(" "))
        if found is None or found.group(2) != self.rid:
            self.other_lines += 1
            return
        if self.stall and not self.stalled.is_set():
            self.stalled.set()
            self.abort.wait(self.stall)
            self.resumed.set()
        kind, _, n = found.groups()
        self.lines.append(Seen(kind, int(n), _us(entry.timestamp)))

    def on_event(self, event: StreamEvent) -> None:
        self.events.append(event)

    @property
    def gaps(self) -> list[Gap]:
        return [e for e in self.events if isinstance(e, Gap)]

    @property
    def changes(self) -> list[StateChange]:
        return [e for e in self.events if isinstance(e, StateChange)]

    def count(self, kind: str) -> int:
        return sum(1 for seen in self.lines if seen.kind == kind)

    def ended(self) -> int | None:
        """The writer's line count, once its ``end`` line was delivered."""
        return next((s.n for s in self.lines if s.kind == "end"), None)


# -- the pass criteria (design 9.4) ----------------------------------------------------


@dataclasses.dataclass(slots=True)
class Report:
    """What `check_sequence` found; ``failures`` empty means the gate passed."""

    failures: list[str]
    duplicate_seqs: list[int]
    duplicate_bursts: list[int]
    missing_seqs: int
    incomplete_bursts: int
    uncovered: list[str]
    collided_bursts: int

    def summary(self) -> dict[str, object]:
        return {
            "failures": self.failures,
            "missing_seqs": self.missing_seqs,
            "incomplete_bursts": self.incomplete_bursts,
            "collided_bursts": self.collided_bursts,
        }


def check_sequence(
    lines: Sequence[Seen], gaps: Sequence[Gap], *, count: int, burst: int
) -> Report:
    """Design 9.4's criteria over a writer of ``count`` seq lines with ``burst``
    identical lines after each.

    - no seq is delivered twice, no burst more than ``burst`` times;
    - every missing seq, and every burst delivered fewer than ``burst`` times,
      lies inside a reported Gap: between its delivered neighbours (in the
      writer's order: the delivered line before it, and the first delivered
      line after it, after every delivered copy of a burst) the stream
      reported a Gap whose span meets theirs;
    - ``collided_bursts`` counts the bursts with two copies in one
      microsecond (the occurrence-count dedupe needs them; the caller
      decides whether at least one is required).
    """
    seqs = Counter(s.n for s in lines if s.kind == "seq")
    bursts = Counter(s.n for s in lines if s.kind == "burst")
    duplicate_seqs = sorted(n for n, c in seqs.items() if c > 1)
    duplicate_bursts = sorted(n for n, c in bursts.items() if c > burst)
    # The writer's order: seq n, then its burst lines.
    position = sorted(
        ((s.n, s.kind != "seq"), s.ts) for s in lines if s.kind in {"seq", "burst"}
    )
    keys = [key for key, _ in position]
    stamps = [ts for _, ts in position]
    spans = [
        (-1 if g.after is None else _us(g.after), _us(g.before))
        for g in gaps
        if g.before is not None
    ]

    def covered(key: tuple[int, bool]) -> bool:
        # A burst's lost copies may follow any of its delivered ones.
        first = bisect.bisect_left(keys, key)
        after = bisect.bisect_right(keys, key)
        low = stamps[first - 1] if first > 0 else -1
        high = stamps[after] if after < len(stamps) else 2**63
        return any(start <= high and end >= low for start, end in spans)

    uncovered: list[str] = []
    missing = [n for n in range(count) if not seqs[n]]
    incomplete = [n for n in range(count) if burst and bursts[n] < burst]
    uncovered.extend(f"seq {n}" for n in missing if not covered((n, False)))
    uncovered.extend(f"burst {n}" for n in incomplete if not covered((n, True)))
    collided = 0
    by_burst: dict[int, list[int]] = {}
    for s in lines:
        if s.kind == "burst":
            by_burst.setdefault(s.n, []).append(s.ts)
    for times in by_burst.values():
        collided += len(times) != len(set(times))
    failures: list[str] = []
    if duplicate_seqs:
        failures.append(f"seq delivered twice: {duplicate_seqs[:20]}")
    if duplicate_bursts:
        failures.append(
            f"burst delivered more than {burst} times: {duplicate_bursts[:20]}"
        )
    if uncovered:
        failures.append(f"missing outside every Gap: {uncovered[:20]}")
    return Report(
        failures,
        duplicate_seqs,
        duplicate_bursts,
        len(missing),
        len(incomplete),
        uncovered,
        collided,
    )


# -- what the gates need of the device ------------------------------------------------


class Bench(Protocol):
    """The device side of a gate (`DeviceBench`; a dry run passes a fake)."""

    adb_path: str
    serial: str | None
    kinds: tuple[str, ...]  # the disconnects it can make

    def start_writer(self, rid: str, *, count: int, burst: int, pause: bool) -> None:
        """Start a detached writer of ``count`` seq lines (see the module docs)."""
        ...

    def stop_writer(self, rid: str) -> None:
        """Make the writer stop after its current line and log its end line."""
        ...

    def disconnect(self, kind: str) -> None:
        """End the stream's adb logcat one of the ways of FINDINGS.md check 4,
        and return once the device answers again."""
        ...

    def log(self, tag: str, message: str) -> None:
        """``adb shell log -t <tag> <message>``."""
        ...

    def dump(self) -> list[str]:
        """``adb logcat -d`` in the library's format (design 5.6 argv)."""
        ...


def writer_command(rid: str, *, count: int, burst: int, pause: bool) -> str:
    """The device shell command of a detached writer (one line per echo)."""
    stop = f"/data/local/tmp/lp-stop-{rid}"
    bursts = "".join(f"echo burst={rid}-$i; " for _ in range(burst))
    sleep = f"sleep {PERIOD}; " if pause else ""
    loop = (
        f"i=0; while [ $i -lt {count} ] && [ ! -e {stop} ]; do "
        f"echo seq={rid}-$i; {bursts}{sleep}i=$((i+1)); done; echo end={rid}-$i"
    )
    return f"rm -f {stop}; nohup logwrapper sh -c '{loop}' > /dev/null 2>&1 &"


class DeviceBench:
    """`Bench` on the device of ``probe_ctx`` (the guarded adb client)."""

    def __init__(self, ctx: device_probe.Context, vm: device_probe.Vm | None) -> None:
        self.ctx = ctx
        self.vm = vm
        self.adb_path = ctx.adb.path
        self.serial = ctx.adb.serial
        kinds = ["logcat_killed", "adb_reconnect"]
        if vm is not None:
            kinds.append("server_restart")
        self.kinds = tuple(kinds)

    def _shell(self, name: str, command: str) -> device_probe.Step:
        step = self.ctx.adb.shell(name, command)
        if step.rc != 0 and name != "kill_logcat":
            msg = f"adb shell {command!r}: rc {step.rc}, {step.stdout + step.stderr}"
            raise RuntimeError(msg)
        return step

    def start_writer(self, rid: str, *, count: int, burst: int, pause: bool) -> None:
        self._shell(
            f"writer_{rid}", writer_command(rid, count=count, burst=burst, pause=pause)
        )

    def stop_writer(self, rid: str) -> None:
        self._shell(f"stop_{rid}", f"touch /data/local/tmp/lp-stop-{rid}")

    def disconnect(self, kind: str) -> None:
        if kind == "logcat_killed":  # rc 143
            self._shell("kill_logcat", "kill $(pidof logcat)")
        elif kind == "adb_reconnect":  # rc 255
            self.ctx.adb.run("reconnect", ["reconnect"])
        elif kind == "server_restart" and self.vm is not None:  # rc 255
            adb = self.vm.adb
            self.vm.run(
                "server_restart", f"{adb} kill-server; sleep 1; {adb} start-server"
            )
        else:
            msg = f"unknown disconnect {kind!r}"
            raise ValueError(msg)
        self._wait_device()

    def _wait_device(self) -> None:
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            with contextlib.suppress(device_probe.TunnelDownError):
                step = self.ctx.adb.run("get_state", ["get-state"], timeout=10)
                if step.stdout == ["device"]:
                    return
            time.sleep(0.5)
        msg = f"{self.serial} did not come back within {WAIT} s"
        raise TimeoutError(msg)

    def log(self, tag: str, message: str) -> None:
        self.ctx.adb.run("log", ["shell", "log", "-t", tag, message])

    def dump(self) -> list[str]:
        words = ("threadtime", "year", "usec", "zone", "uid")
        argv = ["logcat", "-d", *(a for w in words for a in ("-v", w))]
        return self.ctx.adb.run("dump", argv, timeout=60).stdout


# -- the gates (they take a Bench, so a dry run can use the fake adb) -----------------


def _pace(stream: LogStream[LogEntry], until: float) -> None:
    """Let the scenario run until ``until`` (monotonic); fail on a dead stream.

    The gates last minutes by design (9.4), so they pace their schedule; the
    waits that synchronize on the stream use ``wait_until``.
    """
    while time.monotonic() < until:
        if stream.state in {StreamState.FAILED, StreamState.KILLED}:
            msg = f"the stream ended {stream.state}: {stream.outcome!r}"
            raise AssertionError(msg)
        time.sleep(min(0.5, max(until - time.monotonic(), 0.0)))


def _pace_plain(until: float) -> None:
    """Let the scenario run until ``until`` (monotonic)."""
    while time.monotonic() < until:
        time.sleep(min(0.5, max(until - time.monotonic(), 0.0)))


def _end(bench: Bench, rid: str, stream: LogStream[LogEntry], timeout: float) -> None:
    """Stop the writer (again: a failed gate leaves no writer running on the
    device; its stop file stays, a few bytes) and the stream. A FAILED run's
    outcome is left to the test, which checks ``stream.outcome``."""
    with contextlib.suppress(Exception):
        bench.stop_writer(rid)
    stream.stop()
    with contextlib.suppress(StreamError):
        stream.join(timeout)


def _running_again(stream: LogStream[LogEntry], reconnects: int) -> None:
    wait_until(
        lambda: (
            stream.stats.reconnects > reconnects and stream.state is StreamState.RUNNING
        ),
        timeout=WAIT,
    )


def run_resume_gate(
    bench: Bench, seconds: float, *, disconnects: int = DISCONNECTS
) -> tuple[Capture, Report, LogStream[LogEntry], list[str]]:
    """The F1-6 resume gate: a writer of seq lines and K-line identical bursts
    every 10 ms, a ``Reconnect()`` stream, and ``disconnects`` disconnects of
    the bench's kinds spread over ``seconds`` (design 9.4)."""
    rid = secrets.token_hex(4)
    capture = Capture(rid)
    stream = LogStream(
        StreamConfig(
            adb_path=bench.adb_path,
            serial=bench.serial,
            since="now",
            reconnect=Reconnect(delay=0.5, max_delay=5.0, stable_after=5.0),
        ),
        on_item=capture.on_item,
        on_event=capture.on_event,
    )
    made: list[str] = []
    stream.start()
    try:
        stream.wait_running(WAIT)
        # Enough loops for the whole run; the stop file ends it.
        bench.start_writer(
            rid, count=int(4 * seconds / PERIOD), burst=BURST, pause=True
        )
        wait_until(lambda: capture.count("seq") > 0, timeout=WAIT)
        began = time.monotonic()
        for i in range(disconnects):
            _pace(stream, began + seconds * (i + 1) / (disconnects + 1))
            kind = bench.kinds[i % len(bench.kinds)]
            reconnects = stream.stats.reconnects
            bench.disconnect(kind)
            made.append(kind)
            _running_again(stream, reconnects)
        _pace(stream, began + seconds)
        bench.stop_writer(rid)
        wait_until(lambda: capture.ended() is not None, timeout=WAIT)
    finally:
        _end(bench, rid, stream, WAIT)
    count = capture.ended()
    assert count is not None
    report = check_sequence(capture.lines, capture.gaps, count=count, burst=BURST)
    return capture, report, stream, made


def run_overwrite_gate(
    bench: Bench, stall: float, *, stall_timeout: float | None
) -> tuple[Capture, Report, LogStream[LogEntry]]:
    """The overwrite gate: a writer fast enough to wrap the ring within 30 s,
    and a consumer that stalls for ``stall`` seconds, then drains (9.4)."""
    rid = secrets.token_hex(4)
    capture = Capture(rid, stall=stall)
    stream = LogStream(
        StreamConfig(
            adb_path=bench.adb_path,
            serial=bench.serial,
            since="now",
            stall_timeout=stall_timeout,
        ),
        on_item=capture.on_item,
        on_event=capture.on_event,
    )
    stream.start()
    try:
        stream.wait_running(WAIT)
        bench.start_writer(rid, count=5_000_000, burst=0, pause=False)
        wait_until(capture.stalled.is_set, timeout=WAIT)
        wait_until(capture.resumed.is_set, timeout=stall + WAIT)
        _pace_plain(time.monotonic() + 10)  # the writer runs on past the stall
        bench.stop_writer(rid)
        if stream.state not in {StreamState.FAILED, StreamState.STOPPED}:
            wait_until(
                lambda: (
                    capture.ended() is not None or stream.state is StreamState.FAILED
                ),
                timeout=4 * WAIT,
            )
    finally:
        capture.abort.set()
        _end(bench, rid, stream, 4 * WAIT)
    count = capture.ended()
    if count is None:  # the run ended first (stall_timeout=None): judge what came
        count = max((s.n + 1 for s in capture.lines if s.kind == "seq"), default=0)
    report = check_sequence(capture.lines, capture.gaps, count=count, burst=0)
    return capture, report, stream


def notice_times(dump: Sequence[str], rid: str, live: dict[int, int]) -> list[str]:
    """For each chatty notice among the writer's lines in ``dump``: ``newest``,
    ``oldest`` or ``other``, by its time against the times of the lines it
    replaced (``live``: seq -> time, read live before the pruning).

    A notice stands where its lines stood (FINDINGS.md check 6); the lines it
    replaced are the writer's seqs read live between its neighbours in the
    dump that the dump no longer holds. A notice without such lines (it
    replaced only lines of other processes of the uid) is left out.
    """
    parser = ThreadTimeLogParser()
    context = ParseContext()
    order: list[tuple[str, int]] = []  # ("seq", n) or ("notice", time)
    for line in dump:
        entry = parser.parse(line, context)
        if entry is None or not entry.parsed:
            continue
        found = _LINE.match(entry.message.rstrip(" "))
        if found is not None and found.group(2) == rid and found.group(1) == "seq":
            order.append(("seq", int(found.group(3))))
        elif entry.tag == "chatty" and " expire " in entry.message:
            order.append(("notice", _us(entry.timestamp)))
    dumped = {n for kind, n in order if kind == "seq"}
    verdicts: list[str] = []
    for i, (kind, ts) in enumerate(order):
        if kind != "notice":
            continue
        before = next((n for k, n in reversed(order[:i]) if k == "seq"), None)
        after = next((n for k, n in order[i + 1 :] if k == "seq"), None)
        if before is None or after is None:
            continue
        replaced = [
            live[n] for n in range(before + 1, after) if n in live and n not in dumped
        ]
        if not replaced:
            continue
        if ts >= max(replaced):
            verdicts.append("newest")
        elif ts <= min(replaced):
            verdicts.append("oldest")
        else:
            verdicts.append("other")
    return verdicts
