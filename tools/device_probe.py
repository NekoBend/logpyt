"""Record the Android device facts that logpyt 3.0.0 relies on (design 9.4).

Each probe writes one JSON recording, ``<KEY>_<slug>.json``, into
``tests/fixtures/device/api<sdk>/`` (``host/`` for C09, which is about the adb
server, not a device). ``tests/fixtures/device/FINDINGS.md`` interprets the
recordings, ``tests/test_device_fixtures.py`` checks their key properties in
normal CI, and ``tests/device/`` re-runs the probes against a live device.

R1-R6 re-record the facts first observed on 2026-11-04 (R6 is the U-1 (a)
argv, open check 1); C02-C12 are the open checks of design section 9.4.

The adb server is never started from here. Every adb child runs with
``ADB_SERVER_SOCKET=tcp:127.0.0.1:5037`` (a numeric address, never
``localhost``) and without ``ANDROID_LOG_TAGS`` and ``ANDROID_SERIAL``. With
that socket spec the adb client does not start a server when it finds none: it
prints ``* cannot start server on remote host`` and exits 1, while an unset
spec or ``tcp:localhost:5037`` makes it start one (observed with the 37.0.1
client, ``tests/fixtures/device/FINDINGS.md``, "The adb server is never
started"). So the guarantee holds even when the tunnel to the remote adb
server drops between a check and the spawn. Before every adb call the probe
also checks that 127.0.0.1:5037 accepts a TCP connection, so that a tunnel
that is down stops the run with :class:`TunnelDownError` before adb is
spawned, and an adb child that reports it could not reach the server raises
the same error instead of being recorded as a device fact.

Usage::

    python tools/device_probe.py --adb PATH --serial emulator-5556
        [--out DIR] [--only R6,C02] [--root] [--vm-ssh WRAPPER] [--disruptive]

``--root`` allows the steps that run ``adb root`` (C05's logd evidence, C07).
``--vm-ssh`` names an executable that runs its one argument as a shell command
on the machine that hosts the adb server and the emulators (C04's server
restart and unplug, C09); ``--vm-adb`` is the adb path there. ``--disruptive``
allows the probes that cut every adb connection of that server: C09 stops the
server twice (every emulator connection drops until it is back), and C04
reconnects the device, restarts the adb server and kills the emulator: it runs
last, and the emulator stays down afterwards. With ``--out``, C09 is written to
``<out>/host``.

Standard library only, so any Python 3.12+ runs it.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import datetime as dt
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Final, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from types import TracebackType

SERVER: Final = ("127.0.0.1", 5037)
# How long the pipes of a child that has exited (or was killed and reaped) are
# still read: a descendant that inherited them keeps them open after the child.
DRAIN_S: Final = 5.0
# Stored instead of the configured --vm-adb path, which may name a home
# directory on the server host.
VM_ADB_PLACEHOLDER: Final = "<vm-adb>"
U1A_FORMAT: Final = (
    "-v",
    "threadtime",
    "-v",
    "year",
    "-v",
    "usec",
    "-v",
    "zone",
    "-v",
    "uid",
)
YEAR_USEC: Final = ("-v", "threadtime", "-v", "year", "-v", "usec")
ABSENT_SERIAL: Final = "emulator-9999"
MAX_LINE_CHARS: Final = 400
FIXTURES: Final = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "device"
)
PROPS: Final = (
    "ro.build.version.release",
    "ro.build.version.sdk",
    "ro.build.type",
    "ro.product.cpu.abi",
    "ro.build.fingerprint",
    "persist.sys.timezone",
)
APP_PACKAGE: Final = "com.android.settings"
_CLEAN_ENV: Final = frozenset(
    {"ANDROID_LOG_TAGS", "ANDROID_SERIAL", "ADB_SERVER_SOCKET"}
)

U1A_RE: Final = re.compile(
    r"^(?P<date>\d{4}-\d\d-\d\d) (?P<time>\d\d:\d\d:\d\d)\.(?P<usec>\d{6}) "
    r"(?P<zone>[+-]\d{4}) +(?P<uid>\S+) +(?P<pid>\d+) +(?P<tid>\d+) "
    r"(?P<level>[VDIWEFA]) (?P<tag>.*?) *: (?P<msg>.*)$"
)
_YEAR_TS_RE: Final = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{6} ")
_EPOCH_TS_RE: Final = re.compile(r"^ *(\d+\.\d{3}) ")
_NOTICE_RE: Final = re.compile(r"\b(?:expire|identical) (\d+) lines?\b")
_APP_UID_RE: Final = re.compile(r"^(?:1\d{4}|u\d+_[ai]\d+)$")
_THREADTIME_TAG_RE: Final = re.compile(
    r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} +\d+ +\d+ [VDIWEFA] (.*?) *: "
)
_PM_LINE_RE: Final = re.compile(r"^package:(\S+) uid:(\d+)")
# What the adb client prints when nothing accepts its connection to the
# server (FINDINGS.md, "The adb server is never started").
_SERVER_LOST_RE: Final = re.compile(
    r"cannot connect to daemon|cannot start server on remote host"
)
_CRASH_TAGS: Final = frozenset(
    {
        "AndroidRuntime",
        "DEBUG",
        "crash_dump32",
        "crash_dump64",
        "debuggerd",
        "debuggerd64",
        "libc",
        "tombstoned",
    }
)


class TunnelDownError(RuntimeError):
    """The adb server address does not accept connections; adb was not run."""


class DeviceNotReadyError(RuntimeError):
    """The serial is not attached in state ``device`` (every probe would wait)."""


class TimeLimitError(RuntimeError):
    """The probe ran past the time limit of :meth:`Context.time_limit`."""


def server_reachable(server: tuple[str, int] = SERVER, timeout: float = 2.0) -> bool:
    """Return whether ``server`` accepts a TCP connection."""
    try:
        with socket.create_connection(server, timeout=timeout):
            return True
    except OSError:
        return False


def split_lines(data: bytes) -> list[str]:
    """Decode ``data`` as UTF-8 and split it on LF (a trailing CR is kept)."""
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return lines


def is_divider(line: str) -> bool:
    """Return whether ``line`` is a ``--------- beginning of <buffer>`` divider."""
    return line.startswith("--------- ")


def entries(lines: Sequence[str]) -> list[str]:
    """Return ``lines`` without buffer dividers."""
    return [line for line in lines if not is_divider(line)]


def epoch_arg(us: int) -> str:
    """Format epoch microseconds as a ``-T`` value: always ``<sec>.<usec:06d>``."""
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


@dataclass(frozen=True)
class Line:
    """One line of the U-1 (a) wire format, split into its columns."""

    us: int
    zone: str
    uid: str
    pid: int
    tid: int
    level: str
    tag: str
    msg: str
    raw: str


def parse_u1a(line: str) -> Line | None:
    """Parse a ``-v threadtime -v year -v usec -v zone -v uid`` line."""
    m = U1A_RE.match(line)
    if m is None:
        return None
    year, month, day = (int(part) for part in m["date"].split("-"))
    hour, minute, second = (int(part) for part in m["time"].split(":"))
    zone = m["zone"]
    offset = (int(zone[1:3]) * 60 + int(zone[3:5])) * 60 * (1 if zone[0] == "+" else -1)
    epoch = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)) - offset
    return Line(
        us=epoch * 1_000_000 + int(m["usec"]),
        zone=zone,
        uid=m["uid"],
        pid=int(m["pid"]),
        tid=int(m["tid"]),
        level=m["level"],
        tag=m["tag"],
        msg=m["msg"],
        raw=line,
    )


def keyed_u1a(lines: Sequence[str]) -> list[tuple[int, str]]:
    """Return ``(epoch_us, line)`` for every parsable U-1 (a) line."""
    return [(p.us, p.raw) for line in lines if (p := parse_u1a(line)) is not None]


def keyed_date(lines: Sequence[str], width: int) -> list[tuple[int, str]]:
    """Return ``(timestamp digits, line)`` for ``-v year`` lines cut to ``width``."""
    return [
        (int(re.sub(r"\D", "", line[:width])), line)
        for line in lines
        if _YEAR_TS_RE.match(line)
    ]


def pick_boundary(keys: Sequence[int]) -> int:
    """Pick a boundary key from the middle third, preferring one shared by two lines."""
    counts = Counter(keys)
    middle = list(keys[len(keys) // 3 : 2 * len(keys) // 3]) or list(keys)
    for key in middle:
        if counts[key] >= 2:
            return key
    return middle[len(middle) // 2]


def boundary_facts(
    reference: Sequence[tuple[int, str]], returned: Sequence[tuple[int, str]], t: int
) -> dict[str, Any]:
    """Compare the lines a ``-T t`` read returned with a reference read."""
    at_reference = Counter(line for key, line in reference if key == t)
    at_returned = Counter(line for key, line in returned if key == t)
    return {
        "boundary_lines_in_reference": sum(at_reference.values()),
        "boundary_lines_missing": sum((at_reference - at_returned).values()),
        "first_key_returned": returned[0][0] if returned else None,
        "lines_below_boundary": sum(1 for key, _ in returned if key < t),
        "lines_returned": len(returned),
    }


def missing_ranges(seen: bytearray) -> list[list[int]]:
    """Return the inclusive ``[first, last]`` ranges of unmarked indexes."""
    ranges: list[list[int]] = []
    start: int | None = None
    for index, mark in enumerate(seen):
        if not mark and start is None:
            start = index
        elif mark and start is not None:
            ranges.append([start, index - 1])
            start = None
    if start is not None:
        ranges.append([start, len(seen) - 1])
    return ranges


def adb_args(args: Sequence[str], serial: str | None) -> list[str]:
    """Return the adb arguments (without the executable) for ``serial``."""
    return [*(() if serial is None else ("-s", serial)), *args]


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _read_all(pipe: IO[bytes] | None, sink: bytearray) -> None:
    """Thread body: append ``pipe`` to ``sink`` until EOF, then close it."""
    if pipe is None:
        return
    try:
        # ValueError, OSError: _finish_readers closed the pipe under us (POSIX).
        with contextlib.suppress(OSError, ValueError):
            while chunk := pipe.read(65536):
                sink.extend(chunk)
    finally:
        pipe.close()


def _write_all(pipe: IO[bytes] | None, data: bytes) -> None:
    """Thread body: write ``data`` to the child's stdin, then close it."""
    if pipe is None:
        return
    try:
        view = memoryview(data)
        with contextlib.suppress(OSError, ValueError):  # the child is gone
            while view:
                view = view[pipe.write(view) or 0 :]
    finally:
        with contextlib.suppress(OSError):
            pipe.close()


def _finish_readers(
    readers: Sequence[tuple[threading.Thread, IO[bytes] | None]], deadline: float
) -> bool:
    """Wait until ``deadline`` for each reader to reach EOF; return whether all did.

    Called once the child has exited or was killed and reaped. A descendant
    that inherited a pipe keeps it open after the child, so the wait is
    bounded. A pipe whose reader finished is closed. A pipe still being read
    is closed too on POSIX: the blocked read goes on on the open pipe and the
    reader ends at its next read. On Windows it is left to its reader, which
    closes it at EOF: closing a descriptor while another thread reads it may
    wait for that read to return (from memory, the C runtime holds a
    per-descriptor lock during both).
    """
    drained = True
    for thread, pipe in readers:
        if thread.is_alive():
            thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            drained = False
            if os.name == "nt":
                continue
        if pipe is not None:
            pipe.close()
    return drained


def _exec(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    drain: float = DRAIN_S,
) -> tuple[int | None, bytes, bytes, float, bool]:
    """Run ``argv``; on timeout kill it and return rc ``None``.

    Returns ``(rc, stdout, stderr, elapsed_s, drained)``. Threads read both
    pipes while the child runs. Once it has exited, or was killed and reaped
    after ``timeout``, the readers get ``drain`` more seconds to reach EOF;
    ``drained`` is false when a descendant still held a pipe then, and the
    output read until then is returned (:func:`_finish_readers`).

    The child runs in its own session, so a Ctrl-C in the terminal does not
    reach it: on any exception (``KeyboardInterrupt`` included) it is killed
    and reaped, with the same bounded drain, before the exception propagates.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=None if env is None else dict(env),
        start_new_session=True,
    )
    out, err = bytearray(), bytearray()
    readers = [
        (threading.Thread(target=_read_all, args=(pipe, sink), daemon=True), pipe)
        for pipe, sink in ((proc.stdout, out), (proc.stderr, err))
    ]
    writer = (
        threading.Thread(
            target=_write_all, args=(proc.stdin, stdin or b""), daemon=True
        ),
        proc.stdin,
    )
    try:
        for thread, _ in (*readers, writer):
            thread.start()
        try:
            rc: int | None = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            rc = None
            proc.kill()
            proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        _finish_readers([*readers, writer], time.monotonic() + drain)
        raise
    deadline = time.monotonic() + drain
    drained = _finish_readers(readers, deadline)
    _finish_readers([writer], deadline)  # stdin: closed, nothing to report
    return rc, bytes(out), bytes(err), time.monotonic() - start, drained


@dataclass
class Step:
    """One command and what it printed."""

    name: str
    where: str
    argv: list[str]
    rc: int | None
    elapsed_s: float
    stdout: list[str]
    stderr: list[str]
    env: dict[str, str | None] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    # False: a descendant still held stdout or stderr when the probe stopped
    # reading (DRAIN_S after the child's exit), so the output may be cut short.
    drained: bool = True

    @property
    def timed_out(self) -> bool:
        """Whether the probe ended the command (it did not exit by itself)."""
        return self.rc is None

    def to_json(self, stdout: Sequence[str] | None = None) -> dict[str, Any]:
        """Serialize; ``stdout`` replaces the stored lines (a trimmed selection)."""
        lines = list(self.stdout if stdout is None else stdout)
        data: dict[str, Any] = {
            "name": self.name,
            "where": self.where,
            "argv": self.argv,
            "rc": self.rc,
            "timed_out": self.timed_out,
            "elapsed_s": round(self.elapsed_s, 3),
            "stdout": [line[:MAX_LINE_CHARS] for line in lines],
            "stdout_total_lines": len(self.stdout),
            "stdout_clipped_lines": sum(
                1 for line in lines if len(line) > MAX_LINE_CHARS
            ),
            "stderr": [line[:MAX_LINE_CHARS] for line in self.stderr[:40]],
        }
        if self.env:
            data["env"] = self.env
        if not self.drained:
            data["drained"] = False
        data.update(self.extra)
        return data


def _check_server_reached(step: Step) -> None:
    """Raise :class:`TunnelDownError` if adb reports that no server answered.

    The port check before the spawn passed, so the tunnel dropped in between;
    the step holds adb's error, not a device fact.
    """
    lost = [line.strip() for line in step.stderr if _SERVER_LOST_RE.search(line)]
    if lost:
        msg = (
            f"adb ({step.name}) could not reach the adb server: {lost[-1]}; the "
            "tunnel dropped after the port check (adb did not start a server)"
        )
        raise TunnelDownError(msg)


class Adb:
    """The adb client, guarded so that it can never start a server of its own.

    The guard is the socket spec every child gets, ``ADB_SERVER_SOCKET=
    tcp:<IPv4 address>:<port>`` (:meth:`env`): with a numeric host the adb
    client never starts a server, it fails with ``cannot start server on
    remote host``; it starts one for an unset spec, an empty host or
    ``localhost`` (FINDINGS.md, "The adb server is never started"). So the
    server host must be an IPv4 address, and no override may change the spec.
    The TCP check before each spawn (:meth:`ensure_server`) only turns a tunnel
    that is down into a clear :class:`TunnelDownError`; a tunnel that drops
    after it is caught from adb's error (:func:`_check_server_reached`).

    ``deadline`` (a ``time.monotonic()`` value, set by
    :meth:`Context.time_limit`) caps every command timeout and reader wait to
    the time left, and refuses to start adb once it has passed.
    """

    def __init__(
        self,
        path: str,
        serial: str | None,
        *,
        server: tuple[str, int] = SERVER,
        connect_timeout: float = 2.0,
    ) -> None:
        host, port = server
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            msg = (
                f"adb server host {host!r} is not an IPv4 address: for an empty "
                "host or 'localhost' the adb client starts a server of its own"
            )
            raise ValueError(msg) from None
        self.path = path
        self.serial = serial
        self.server = (host, port)
        self.connect_timeout = connect_timeout
        self.deadline: float | None = None

    @property
    def server_socket(self) -> str:
        """The ``ADB_SERVER_SOCKET`` value of every child: ``tcp:<IPv4>:<port>``."""
        host, port = self.server
        return f"tcp:{host}:{port}"

    def ensure_server(self) -> None:
        """Raise :class:`TunnelDownError` unless the server address accepts TCP."""
        if not server_reachable(self.server, self.connect_timeout):
            host, port = self.server
            msg = (
                f"{host}:{port} does not accept connections (is the tunnel to the adb "
                "server up?); adb was not run, so it cannot start a server of its own"
            )
            raise TunnelDownError(msg)

    def time_left(self) -> float | None:
        """Seconds left before ``deadline`` (``None``: no limit); raise once past."""
        if self.deadline is None:
            return None
        left = self.deadline - time.monotonic()
        if left <= 0:
            msg = "the probe ran past its time limit"
            raise TimeLimitError(msg)
        return left

    def budget(self, timeout: float) -> float:
        """Return ``timeout``, capped to the time left before ``deadline``."""
        left = self.time_left()
        return timeout if left is None else min(timeout, left)

    def env(self, overrides: Mapping[str, str | None] | None = None) -> dict[str, str]:
        """Return the child environment: the host's, cleaned, plus ``overrides``.

        ``ADB_SERVER_SOCKET`` is always :attr:`server_socket`; an override
        that would change or remove it raises ``ValueError``.
        """
        if overrides and any(key.upper() == "ADB_SERVER_SOCKET" for key in overrides):
            msg = (
                "ADB_SERVER_SOCKET cannot be overridden: it keeps adb from "
                "starting a server of its own"
            )
            raise ValueError(msg)
        env = {key: value for key, value in os.environ.items() if key not in _CLEAN_ENV}
        for key, value in (overrides or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        env["ADB_SERVER_SOCKET"] = self.server_socket
        return env

    def run(
        self,
        name: str,
        args: Sequence[str],
        *,
        timeout: float = 30.0,
        serial: str | None = "",
        env: Mapping[str, str | None] | None = None,
    ) -> Step:
        """Run one adb command; ``serial=""`` means this device, ``None`` none."""
        self.ensure_server()
        selected = self.serial if serial == "" else serial
        argv = adb_args(args, selected)
        rc, out, err, elapsed, drained = _exec(
            [self.path, *argv], timeout=self.budget(timeout), env=self.env(env)
        )
        step = Step(
            name=name,
            where="adb",
            argv=argv,
            rc=rc,
            elapsed_s=elapsed,
            stdout=split_lines(out),
            stderr=split_lines(err),
            env=dict(env or {}),
            drained=drained,
        )
        _check_server_reached(step)
        return step

    def shell(self, name: str, command: str, *, timeout: float = 30.0) -> Step:
        """Run ``adb shell <command>`` (the device shell parses ``command``)."""
        return self.run(name, ["shell", command], timeout=timeout)


class Vm:
    """Runs shell commands on the machine that hosts the adb server."""

    def __init__(self, ssh: str, adb: str) -> None:
        self.ssh = ssh
        self.adb = adb

    def scrub(self, text: str) -> str:
        """Replace the configured adb path with :data:`VM_ADB_PLACEHOLDER`.

        The path (``--vm-adb``) may name a home directory on the server host,
        so it is never stored; the ssh wrapper's path is not stored either.
        """
        return text.replace(self.adb, VM_ADB_PLACEHOLDER) if self.adb else text

    def run(
        self,
        name: str,
        command: str,
        *,
        timeout: float = 60.0,
        stdin: bytes | None = None,
    ) -> Step:
        """Run ``command`` through the ssh wrapper."""
        rc, out, err, elapsed, drained = _exec(
            [self.ssh, command], timeout=timeout, stdin=stdin
        )
        return Step(
            name=name,
            where="vm",
            argv=[self.scrub(command)],
            rc=rc,
            elapsed_s=elapsed,
            stdout=[self.scrub(line) for line in split_lines(out)],
            stderr=[self.scrub(line) for line in split_lines(err)],
            drained=drained,
        )


class LiveReader:
    """A long-running adb child whose stdout one thread reads, or stops reading.

    A third thread waits for the child's exit and stamps it (``exited_at``),
    whatever the caller is doing meanwhile. If anything fails after the spawn,
    construction kills and reaps the child, ends the threads it started and
    closes the pipes before the exception propagates.
    """

    def __init__(
        self,
        adb: Adb,
        name: str,
        args: Sequence[str],
        *,
        keep: int = 100_000,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        adb.ensure_server()
        adb.time_left()
        self._adb = adb
        self.name = name
        self.argv = adb_args(args, adb.serial)
        self.lines: list[str] = []
        self.line_count = 0
        self.bytes_read = 0
        self.first_byte_s: float | None = None
        self.last_byte_at: float | None = None
        self.exited_at: float | None = None
        self._keep = keep
        self._on_line = on_line
        self._partial = b""
        self._stderr = bytearray()
        self._lock = threading.Lock()
        self._reading = threading.Event()
        self._reading.set()
        self.read_rate: float | None = None
        self._step: Step | None = None
        self._stdout_pump = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr_pump = threading.Thread(target=self._pump_stderr, daemon=True)
        self._exit_watch = threading.Thread(target=self._watch_exit, daemon=True)
        env = adb.env()
        self.started_at = time.monotonic()
        self.proc = subprocess.Popen(
            [adb.path, *self.argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
            start_new_session=True,
        )
        try:
            for thread in (self._stdout_pump, self._stderr_pump, self._exit_watch):
                thread.start()
        except BaseException:
            self._abort()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc is None:
            self.stop()
            return
        # Another exception is on its way: end the child, keep that exception.
        with contextlib.suppress(TunnelDownError):
            self.stop()

    def _abort(self) -> None:
        """Undo a construction that failed after the spawn."""
        self.proc.kill()
        self.proc.wait()
        self._reading.set()
        self._end_threads(time.monotonic() + DRAIN_S)

    def _end_threads(self, deadline: float) -> bool:
        """After the child is reaped: end the threads, close the pipes; drained?"""
        drained = _finish_readers(
            [
                (self._stdout_pump, self.proc.stdout),
                (self._stderr_pump, self.proc.stderr),
            ],
            deadline,
        )
        if self._exit_watch.is_alive():
            self._exit_watch.join(max(0.0, deadline - time.monotonic()))
        return drained

    def _take(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace")
        self.line_count += 1
        if len(self.lines) < self._keep:
            self.lines.append(line)
        if self._on_line is not None:
            self._on_line(line)

    def _pump_stdout(self) -> None:
        pipe = self.proc.stdout
        if pipe is None:
            return
        # ValueError, OSError: _finish_readers closed the pipe under us (POSIX).
        with contextlib.suppress(OSError, ValueError):
            while True:
                self._reading.wait()
                chunk = pipe.read(65536)
                if not chunk:
                    break
                now = time.monotonic()
                with self._lock:
                    if self.first_byte_s is None:
                        self.first_byte_s = now - self.started_at
                    self.last_byte_at = now
                    self.bytes_read += len(chunk)
                    *complete, self._partial = (self._partial + chunk).split(b"\n")
                    for raw in complete:
                        self._take(raw)
                if self.read_rate:
                    time.sleep(len(chunk) / self.read_rate)
        with self._lock:
            if self._partial:
                self._take(self._partial)
                self._partial = b""
        pipe.close()

    def _pump_stderr(self) -> None:
        pipe = self.proc.stderr
        if pipe is None:
            return
        with contextlib.suppress(OSError, ValueError):
            while chunk := pipe.read(65536):
                with self._lock:
                    self._stderr += chunk
        pipe.close()

    def _watch_exit(self) -> None:
        self.proc.wait()
        self.exited_at = time.monotonic()

    def pause(self) -> None:
        """Stop reading stdout after the current chunk (the pipe fills up)."""
        self._reading.clear()

    def resume(self) -> None:
        """Read stdout again, at full speed."""
        self.read_rate = None
        self._reading.set()

    def throttle(self, bytes_per_s: float) -> None:
        """Keep reading, but at most ``bytes_per_s`` (a slow consumer)."""
        self.read_rate = bytes_per_s

    def wait_until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        """Poll ``predicate`` (called under the reader lock) until true or timeout."""
        deadline = time.monotonic() + self._adb.budget(timeout)
        while True:
            with self._lock:
                if predicate():
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def snapshot(self) -> tuple[int, list[str]]:
        """Return ``(bytes_read, lines)`` as of one moment (both under the lock)."""
        with self._lock:
            return self.bytes_read, list(self.lines)

    def wait_lines(self, count: int, timeout: float) -> bool:
        """Wait until ``count`` lines were read."""
        return self.wait_until(lambda: self.line_count >= count, timeout)

    def wait_exit(self, timeout: float) -> int | None:
        """Wait for the child to exit by itself; return its rc or ``None``."""
        try:
            return self.proc.wait(self._adb.budget(timeout))
        except subprocess.TimeoutExpired:
            return None

    def stop(self, *, grace: float = 3.0, drain: float = DRAIN_S) -> Step:
        """End the child (if still running), drain both pipes, return the step.

        Draining is bounded by ``drain`` seconds after the child is reaped
        (:func:`_finish_readers`). Raises :class:`TunnelDownError` (the step is
        kept for a later call) when adb reports that no server answered.
        """
        if self._step is not None:
            return self._step
        rc = self.proc.poll()
        if rc is None:
            self.proc.terminate()
            try:
                self.proc.wait(grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._reading.set()
        drained = self._end_threads(time.monotonic() + drain)
        with self._lock:
            self._step = Step(
                name=self.name,
                where="adb",
                argv=self.argv,
                rc=rc,
                elapsed_s=time.monotonic() - self.started_at,
                stdout=list(self.lines),
                stderr=split_lines(bytes(self._stderr)),
                extra={
                    "live": True,
                    "first_byte_s": None
                    if self.first_byte_s is None
                    else round(self.first_byte_s, 3),
                    "bytes_read": self.bytes_read,
                    "lines_read": self.line_count,
                },
                drained=drained,
            )
        _check_server_reached(self._step)
        return self._step


@dataclass
class Context:
    """What every probe needs: the guarded adb client and the device it targets."""

    adb: Adb
    api: int
    device: dict[str, str]
    vm: Vm | None = None
    allow_root: bool = False
    run_id: str = field(default_factory=lambda: secrets.token_hex(4))
    lines_override: int | None = None

    @classmethod
    def connect(
        cls, adb: Adb, *, vm: Vm | None = None, allow_root: bool = False
    ) -> Context:
        """Check that the serial is attached; read its properties and API level."""
        state = adb.run("get_state", ["get-state"], timeout=10)
        if state.stdout != ["device"]:
            msg = (
                f"{adb.serial!r} is not attached as a device "
                f"(adb get-state: rc {state.rc}, {state.stdout + state.stderr})"
            )
            raise DeviceNotReadyError(msg)
        script = "; ".join(f'echo "{prop}=$(getprop {prop})"' for prop in PROPS)
        step = adb.shell("props", script)
        device: dict[str, str] = {}
        for line in step.stdout:
            key, sep, value = line.strip().partition("=")
            if sep:
                device[key] = value
        device["serial"] = adb.serial or ""
        version = adb.run("version", ["version"], serial=None)
        device["adb_client"] = next(
            (line.strip() for line in version.stdout if line.startswith("Version ")), ""
        )
        api = int(device.get("ro.build.version.sdk") or "0")
        return cls(adb=adb, api=api, device=device, vm=vm, allow_root=allow_root)

    def device_epoch(self) -> int:
        """Return the device clock in whole epoch seconds (``date +%s``)."""
        step = self.adb.shell("date", "date +%s")
        return int(step.stdout[0].strip())

    @contextlib.contextmanager
    def time_limit(self, seconds: float) -> Iterator[None]:
        """Bound the body: every adb command and reader wait gets the time left.

        Once ``seconds`` have passed, the next adb call or wait raises
        :class:`TimeLimitError` (the device tier uses this to end a probe on a
        device that stopped answering before the per-test watchdog fires).
        """
        self.adb.deadline = time.monotonic() + seconds
        try:
            yield
        finally:
            self.adb.deadline = None


class Recording:
    """One recording file: its steps, the derived facts and notes."""

    def __init__(self, ctx: Context, key: str, title: str) -> None:
        self.key = key
        self.data: dict[str, Any] = {
            "id": key,
            "title": title,
            "recorded_at": _utc_now(),
            "tool": "tools/device_probe.py",
            "device": ctx.device,
            "facts": {},
            "notes": [],
            "steps": [],
        }

    def add(self, step: Step, *, stdout: Sequence[str] | None = None) -> Step:
        """Store ``step`` (with ``stdout`` as its trimmed lines) and return it."""
        self.data["steps"].append(step.to_json(stdout))
        return step

    def fact(self, key: str, value: object) -> None:
        """Record a derived fact."""
        self.data["facts"][key] = value

    def note(self, text: str) -> None:
        """Record a free-text note."""
        self.data["notes"].append(text)


def _v(words: Sequence[str]) -> list[str]:
    return [arg for word in words for arg in ("-v", word)]


def _writer(prefix: str, count: int, *, pause_every: int = 0) -> str:
    """A device shell command logging ``<prefix><i>`` for i < count via logwrapper.

    With ``pause_every``, the loop sleeps 10 ms after every ``pause_every``
    lines, which caps the rate (unthrottled, liblog drops lines at the source).
    """
    pause = (
        f" if [ $((i % {pause_every})) -eq 0 ]; then sleep 0.01; fi;"
        if pause_every
        else ""
    )
    loop = f"i=0; while [ $i -lt {count} ]; do echo {prefix}$i; i=$((i+1));{pause} done"
    return f"logwrapper sh -c '{loop}'"


def _liblog_drops(
    ctx: Context, rec: Recording, since_us: int, *, name: str = "liblog_events"
) -> dict[str, int]:
    """Record the ``liblog`` events (source-side drop counts) since ``since_us``."""
    args = ["logcat", "-d", "-b", "events", *U1A_FORMAT, "-T", epoch_arg(since_us)]
    step = ctx.adb.run(name, [*args, "-s", "liblog"])
    values = [
        int(p.msg) for line in step.stdout if (p := parse_u1a(line)) and p.msg.isdigit()
    ]
    rec.add(step, stdout=step.stdout[-10:])
    return {"events": len(values), "lines_dropped": sum(values)}


# -- R1-R6: regression recordings ---------------------------------------------

R1_VARIANTS: Final = (
    ("threadtime",),
    ("threadtime", "year"),
    ("threadtime", "year", "usec"),
    ("threadtime", "year", "nsec"),
    ("threadtime", "epoch"),
    ("threadtime", "epoch", "usec"),
    ("threadtime", "uid"),
    ("threadtime", "year", "UTC", "zone"),
    (),
)


def probe_r1(ctx: Context) -> Recording:
    """R1: output shapes of the ``-v`` modifiers."""
    rec = Recording(
        ctx, "R1", "Output shapes of -v threadtime, year, usec, nsec, epoch, uid, zone"
    )
    for words in R1_VARIANTS:
        rec.add(
            ctx.adb.run(
                "+".join(words) or "default", ["logcat", "-d", "-t", "3", *_v(words)]
            )
        )
    return rec


def _through_boundary(lines: Sequence[str], prefix: str, extra: int = 10) -> list[str]:
    """``lines`` up to the last one starting with ``prefix``, plus ``extra``."""
    last = max(
        (i for i, line in enumerate(lines) if line.startswith(prefix)), default=0
    )
    return list(lines[: max(40, last + 1 + extra)])


def probe_r2(ctx: Context) -> Recording:
    """R2: ``-T`` in date form (ms, usec), in epoch form, and as a digits-only count."""
    rec = Recording(
        ctx, "R2", "-T date form is inclusive; epoch form accepted; digits are a count"
    )
    adb = ctx.adb
    ref = rec.add(adb.run("reference", ["logcat", "-d", "-t", "60", *YEAR_USEC]))
    for label, width in (("ms", 23), ("usec", 26)):
        reference = keyed_date(entries(ref.stdout), width)
        t = pick_boundary([key for key, _ in reference])
        value = next(line[:width] for key, line in reference if key == t)
        step = adb.run(f"T_{label}", ["logcat", "-d", *YEAR_USEC, "-T", value])
        rec.add(step, stdout=_through_boundary(step.stdout, value))
        facts = boundary_facts(reference, keyed_date(entries(step.stdout), width), t)
        rec.fact(f"T_{label}", {"T": value, **facts})
    ref_epoch = rec.add(
        adb.run(
            "reference_epoch",
            ["logcat", "-d", "-t", "60", "-v", "threadtime", "-v", "epoch"],
        )
    )
    stamps = [
        m[1] for line in entries(ref_epoch.stdout) if (m := _EPOCH_TS_RE.match(line))
    ]
    value = stamps[len(stamps) // 2]
    step = adb.run(
        "T_epoch_ms", ["logcat", "-d", "-v", "threadtime", "-v", "epoch", "-T", value]
    )
    rec.add(step, stdout=step.stdout[:20])
    returned = [
        m[1] for line in entries(step.stdout) if (m := _EPOCH_TS_RE.match(line))
    ]
    rec.fact(
        "T_epoch_ms",
        {
            "T": value,
            "rc": step.rc,
            "first_returned": returned[0] if returned else None,
        },
    )
    count = adb.run("T_digits_5", ["logcat", "-d", "-T", "5"])
    rec.add(count, stdout=[*count.stdout[:3], *count.stdout[-3:]])
    rec.fact("T_digits_5_lines", len(entries(count.stdout)))
    return rec


def probe_r3(ctx: Context) -> Recording:
    """R3: ``adb shell pidof`` hands its argument to the device shell."""
    rec = Recording(
        ctx, "R3", "adb shell pidof runs shell metacharacters; no process is rc 1"
    )
    adb = ctx.adb
    rec.add(
        adb.run(
            "metachar",
            ["shell", "pidof", "com.example.none; echo INJECTED_VIA_SEMICOLON"],
        )
    )
    rec.add(adb.run("two_names_one_arg", ["shell", "pidof", "system_server zygote64"]))
    rec.add(adb.run("unknown", ["shell", "pidof", "com.example.none"]))
    return rec


def probe_r4(ctx: Context) -> Recording:
    """R4 and part of C04: adb commands for a serial that is not attached."""
    rec = Recording(
        ctx, "R4", "An absent serial: logcat waits; get-state and shell date"
    )
    adb = ctx.adb
    for name, args in (
        ("logcat_dump", ["logcat", "-d", "-t", "1"]),
        ("logcat_live", ["logcat", *U1A_FORMAT]),
        ("get_state", ["get-state"]),
        ("shell_date", ["shell", "date"]),
    ):
        step = rec.add(adb.run(name, args, serial=ABSENT_SERIAL, timeout=10))
        rec.fact(
            name,
            {"exited": not step.timed_out, "rc": step.rc, "stderr": step.stderr[:3]},
        )
    rec.note(
        "each command was given 10 s; timed_out=true means adb was still "
        f"waiting for {ABSENT_SERIAL}"
    )
    return rec


def collision_stats(lines: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    """Timestamp collision statistics of a U-1 (a) dump; its largest identical group."""
    body = entries(lines)
    parsed = [p for line in body if (p := parse_u1a(line)) is not None]
    if not parsed:
        return {"lines": len(body), "parsed": 0}, []
    per_us = Counter(p.us for p in parsed)
    per_ms = Counter(p.us // 1000 for p in parsed)
    identical = Counter((p.us, p.raw) for p in parsed)
    (us, raw), copies = identical.most_common(1)[0]
    stats = {
        "lines": len(body),
        "bytes": sum(len(line.encode()) + 1 for line in lines),
        "parsed": len(parsed),
        "usec_shared_fraction": round(
            sum(c for c in per_us.values() if c > 1) / len(parsed), 4
        ),
        "usec_largest_group": max(per_us.values()),
        "ms_shared_fraction": round(
            sum(c for c in per_ms.values() if c > 1) / len(parsed), 4
        ),
        "ms_largest_group": max(per_ms.values()),
        "identical_groups": sum(1 for c in identical.values() if c > 1),
        "identical_largest_group": copies,
        "identical_largest_group_T": epoch_arg(us),
    }
    return stats, [raw] * min(copies, 10)


def probe_r5(ctx: Context) -> Recording:
    """R5: ring sizes, the startup backlog and its timestamp collision rates."""
    rec = Recording(ctx, "R5", "Ring sizes, backlog size, timestamp collision rates")
    adb = ctx.adb
    rec.add(adb.run("ring_default", ["logcat", "-g"]))
    rec.add(adb.run("ring_all", ["logcat", "-b", "all", "-g"]))
    rec.add(adb.shell("uptime", "cat /proc/uptime"))
    dump = adb.run("backlog", ["logcat", "-d", *U1A_FORMAT], timeout=180)
    stats, example = collision_stats(dump.stdout)
    rec.add(dump, stdout=example)
    rec.fact("backlog", stats)
    rec.note(
        "the stored backlog lines are the largest group of byte-identical lines "
        "at one timestamp"
    )
    return rec


def sample_u1a(lines: Sequence[str], limit: int = 200) -> list[str]:
    """A small sample that covers every uid token and level.

    The first line of each uid token and of each level is always kept (a long
    one is clipped when stored); up to ``limit`` lines, the first three short
    lines of each are kept.
    """
    keep: list[str] = []
    per_uid: Counter[str] = Counter()
    per_level: Counter[str] = Counter()
    dividers = unmatched = 0
    for line in lines:
        if is_divider(line):
            if dividers < 3:
                keep.append(line)
            dividers += 1
            continue
        parsed = parse_u1a(line)
        if parsed is None:
            if unmatched < 10:
                keep.append(line)
            unmatched += 1
            continue
        uid_seen, level_seen = per_uid[parsed.uid], per_level[parsed.level]
        first = not uid_seen or not level_seen
        more = (
            len(keep) < limit
            and len(line) <= MAX_LINE_CHARS
            and (uid_seen < 3 or level_seen < 3)
        )
        if first or more:
            keep.append(line)
            per_uid[parsed.uid] += 1
            per_level[parsed.level] += 1
    return keep


def probe_r6(ctx: Context) -> Recording:
    """R6 (open check 1): the U-1 (a) argv prints the zone and uid columns."""
    rec = Recording(
        ctx, "R6", "U-1 (a) argv -v threadtime -v year -v usec -v zone -v uid"
    )
    adb = ctx.adb
    dump = adb.run("dump", ["logcat", "-d", *U1A_FORMAT], timeout=180)
    rec.add(dump, stdout=sample_u1a(dump.stdout))
    body = entries(dump.stdout)
    parsed = [p for line in body if (p := parse_u1a(line)) is not None]
    unmatched = [line for line in body if parse_u1a(line) is None]
    rec.fact("lines", len(body))
    rec.fact("unmatched_lines", len(unmatched))
    rec.fact("unmatched_examples", [line[:MAX_LINE_CHARS] for line in unmatched[:5]])
    rec.fact("zones", sorted({line.zone for line in parsed}))
    rec.fact("levels", dict(Counter(p.level for p in parsed).most_common()))
    rec.fact("uid_tokens", dict(Counter(p.uid for p in parsed).most_common()))
    with LiveReader(adb, "live", ["logcat", *U1A_FORMAT]) as live:
        live.wait_lines(25, timeout=15)
    step = live.stop()
    rec.add(step, stdout=step.stdout[:25])
    return rec


# -- C02-C12: open checks --------------------------------------------------------


def probe_c02(ctx: Context) -> Recording:
    """C02: epoch ``-T <sec>.<usec>``: boundary, one microsecond above, live, early."""
    rec = Recording(
        ctx,
        "C02",
        "-T <sec>.<usec>: boundary inclusivity, +1 us, live, before ring start",
    )
    adb = ctx.adb
    ref = adb.run("reference", ["logcat", "-d", "-t", "300", *U1A_FORMAT])
    reference = keyed_u1a(ref.stdout)
    t = pick_boundary([key for key, _ in reference])
    first = next(i for i, (key, _) in enumerate(reference) if key == t)
    last = max(i for i, (key, _) in enumerate(reference) if key == t)
    rec.add(ref, stdout=[line for _, line in reference[max(0, first - 10) : last + 11]])
    rec.fact("T", epoch_arg(t))
    for name, value in (("dump_from_T", t), ("dump_from_T_plus_1us", t + 1)):
        step = adb.run(name, ["logcat", "-d", *U1A_FORMAT, "-T", epoch_arg(value)])
        rec.add(step, stdout=step.stdout[:60])
        rec.fact(name, boundary_facts(reference, keyed_u1a(step.stdout), t))
    with LiveReader(
        adb, "live_from_T", ["logcat", *U1A_FORMAT, "-T", epoch_arg(t)]
    ) as live:
        live.wait_lines(60, timeout=10)
    step = live.stop()
    rec.add(step, stdout=step.stdout[:60])
    rec.fact("live_from_T", boundary_facts(reference, keyed_u1a(step.stdout), t))
    early = adb.run(
        "dump_from_2001",
        ["logcat", "-d", *U1A_FORMAT, "-T", "1000000000.000000"],
        timeout=180,
    )
    full = adb.run("dump_without_T", ["logcat", "-d", *U1A_FORMAT], timeout=180)
    rec.add(early, stdout=early.stdout[:5])
    rec.add(full, stdout=full.stdout[:5])
    early_body, full_body = entries(early.stdout), entries(full.stdout)
    rec.fact(
        "before_ring_start",
        {
            "rc": early.rc,
            "lines": len(early_body),
            "lines_without_T": len(full_body),
            "same_first_entry": bool(
                early_body and full_body and early_body[0] == full_body[0]
            ),
        },
    )
    _connect_races(ctx, rec)
    return rec


def _connect_races(ctx: Context, rec: Recording) -> None:
    """Lines logged around a live ``-T`` reader's connect.

    ``after_newest``: T is newer than every entry when the reader connects,
    and three markers are logged once T has passed. ``marker_first``: the
    ``since="now"`` order of design 5.6 (marker written, then a reader with
    ``-T <clock - 1 s>``).
    """
    adb = ctx.adb
    after_newest: list[dict[str, Any]] = []
    marker_first: list[bool] = []
    for i in range(3):
        tag = f"lpf{ctx.run_id}{i}"
        t = (ctx.device_epoch() + 1) * 1_000_000
        with LiveReader(
            adb, f"after_newest_{i}", ["logcat", *U1A_FORMAT, "-T", epoch_arg(t)]
        ) as live:
            time.sleep(2.5)
            rec.add(
                adb.shell(
                    f"markers_{i}", f"log -t {tag} m1; log -t {tag} m2; log -t {tag} m3"
                )
            )
            live.wait_until(
                lambda tag=tag, live=live: sum(tag in x for x in live.lines) >= 3, 5
            )
        step = live.stop()
        mine = [x for x in step.stdout if tag in x]
        first = next(
            (n for n, x in enumerate(step.stdout) if tag in x), len(step.stdout)
        )
        rec.add(step, stdout=step.stdout[:20])
        after_newest.append(
            {
                "markers_delivered": [
                    m
                    for m in ("m1", "m2", "m3")
                    if any(x.rstrip().endswith(m) for x in mine)
                ],
                "entries_before_first_marker": len(entries(step.stdout[:first])),
                "lines_below_T": sum(1 for key, _ in keyed_u1a(step.stdout) if key < t),
            }
        )
    for i in range(3):
        tag = f"lpm{ctx.run_id}{i}"
        t = (ctx.device_epoch() - 1) * 1_000_000
        rec.add(adb.shell(f"marker_first_{i}", f"log -t {tag} m0"))
        args = ["logcat", *U1A_FORMAT, "-T", epoch_arg(t), "-s", f"{tag}:V"]
        with LiveReader(adb, f"marker_then_reader_{i}", args) as live:
            live.wait_until(
                lambda tag=tag, live=live: any(tag in x for x in live.lines), 5
            )
        step = live.stop()
        rec.add(step)
        marker_first.append(any(tag in x for x in step.stdout))
    rec.fact("T_after_newest_entry", after_newest)
    rec.fact("marker_written_then_reader_delivered", marker_first)


def _record_pm(ctx: Context, rec: Recording) -> dict[int, list[str]]:
    """Record the package-to-uid commands; return uid -> packages (``pm -U``)."""
    adb = ctx.adb
    listing = rec.add(adb.shell("pm_list_packages_U", "pm list packages -U"))
    uids: dict[int, list[str]] = {}
    for line in listing.stdout:
        if m := _PM_LINE_RE.match(line.strip()):
            uids.setdefault(int(m[2]), []).append(m[1])
    rec.add(
        adb.shell("pm_list_packages_U_filter", f"pm list packages -U {APP_PACKAGE}")
    )
    rec.add(
        adb.shell(
            "cmd_package_list_packages_U_filter",
            f"cmd package list packages -U {APP_PACKAGE}",
        )
    )
    plain = adb.shell("pm_list_packages", "pm list packages")
    rec.add(plain, stdout=None if not uids else plain.stdout[:3])
    dump = adb.shell("dumpsys_package", f"dumpsys package {APP_PACKAGE}")
    wanted = re.compile(r"userId=|appId=|Package \[|sharedUser")
    rec.add(dump, stdout=[line for line in dump.stdout if wanted.search(line)][:12])
    return uids


def _record_dumpsys_uids(ctx: Context, rec: Recording) -> dict[int, list[str]]:
    """``dumpsys package packages``: uid -> packages (``userId=`` or ``appId=``).

    A name can have more than one record (API 36 lists three packages twice),
    so a name can appear twice under its uid.
    """
    grep = r"grep -E '^  Package \[|^    (userId|appId)='"
    step = rec.add(
        ctx.adb.shell("dumpsys_package_packages", f"dumpsys package packages | {grep}")
    )
    uids: dict[int, list[str]] = {}
    current: str | None = None
    for line in step.stdout:
        if m := re.match(r"^  Package \[([^\]]+)\]", line):
            current = m[1]
        elif current and (m := re.match(r"^    (?:userId|appId)=(\d+)", line)):
            uids.setdefault(int(m[1]), []).append(current)
            current = None
    return uids


def probe_c03(ctx: Context) -> Recording:
    """C03: uid column tokens of app lines; the package-to-uid lookup commands."""
    rec = Recording(ctx, "C03", "uid column for app uids; package-to-uid commands")
    dump = ctx.adb.run("dump", ["logcat", "-d", *U1A_FORMAT], timeout=180)
    apps: dict[str, str] = {}
    for line in entries(dump.stdout):
        parsed = parse_u1a(line)
        if (
            parsed
            and _APP_UID_RE.match(parsed.uid)
            and parsed.uid not in apps
            and len(line) <= MAX_LINE_CHARS
        ):
            apps[parsed.uid] = line
    rec.add(dump, stdout=list(apps.values())[:30])
    rec.fact("app_uid_tokens", sorted(apps))
    uids = _record_pm(ctx, rec)
    rec.fact("pm_U_packages", sum(len(names) for names in uids.values()))
    dumpsys = _record_dumpsys_uids(ctx, rec)
    # Records with a uid line, and distinct names (recordings before this
    # change hold only the record count, as dumpsys_packages).
    rec.fact("dumpsys_package_records", sum(len(names) for names in dumpsys.values()))
    rec.fact(
        "dumpsys_package_names",
        len({name for names in dumpsys.values() for name in names}),
    )
    by_name = {name: uid for uid, names in dumpsys.items() for name in names}
    rec.fact(
        "pm_U_uid_differs_from_dumpsys",
        sorted(n for u, names in uids.items() for n in names if by_name.get(n) != u),
    )
    lookup = uids or dumpsys
    rec.fact(
        "app_uid_tokens_to_packages",
        {
            token: lookup.get(int(token), [])
            for token in sorted(apps)
            if token.isdigit()
        },
    )
    return rec


def probe_c08(ctx: Context) -> Recording:
    """C08: the ``-v process`` shape (S-parsers-models-12)."""
    rec = Recording(ctx, "C08", "-v process line shape and its (tag) suffix")
    rec.add(ctx.adb.run("process", ["logcat", "-d", "-t", "6", "-v", "process"]))
    return rec


def probe_c10(ctx: Context) -> Recording:
    """C10: ``date +%s`` / ``+%s.%N`` and the time from spawn to the first byte."""
    rec = Recording(ctx, "C10", "date +%s and +%s.%N; spawn to first byte")
    adb = ctx.adb
    _clock_facts(ctx, rec)
    rtts = [rec.add(adb.shell(f"shell_true_{i}", "true")).elapsed_s for i in range(3)]
    rec.fact("shell_true_s", [round(x, 3) for x in rtts])
    firsts: list[float | None] = []
    for i in range(3):
        with LiveReader(adb, f"first_byte_{i}", ["logcat", *U1A_FORMAT]) as live:
            live.wait_lines(1, timeout=15)
        step = live.stop()
        rec.add(step, stdout=step.stdout[:1])
        firsts.append(live.first_byte_s)
    rec.fact(
        "spawn_to_first_byte_s", [None if x is None else round(x, 3) for x in firsts]
    )
    _quiet_reader(ctx, rec)
    return rec


def _clock_facts(
    ctx: Context, rec: Recording, *, clock: Callable[[], float] = time.time
) -> None:
    """``date +%s`` and ``date +%s.%N``, and the device clock against the host's.

    ``date +%s`` is the floor of the device clock, read at some host time
    between ``host_before_s`` and ``host_after_s``, so the device clock minus
    the host clock lies in ``device_minus_host_bounds_s``:
    ``[d - host_after, d + 1 - host_before]``. ``device_minus_host_s`` (``d``
    minus the host midpoint, as in the stored recordings) is a coarse
    estimate within those bounds, up to its rounding to 0.1 s.
    """
    adb = ctx.adb
    host_before = clock()
    seconds = rec.add(adb.shell("date_s", "date +%s"))
    host_after = clock()
    nanos = rec.add(adb.shell("date_s_N", "date +%s.%N"))
    value = seconds.stdout[0].strip() if seconds.stdout else ""
    rec.fact("date_s", value)
    rec.fact("host_before_s", round(host_before, 3))
    rec.fact("host_after_s", round(host_after, 3))
    if value.isdigit():
        device = int(value)
        rec.fact(
            "device_minus_host_s", round(device - (host_before + host_after) / 2, 1)
        )
        rec.fact(
            "device_minus_host_bounds_s",
            [round(device - host_after, 3), round(device + 1 - host_before, 3)],
        )
    text = nanos.stdout[0].strip() if nanos.stdout else ""
    rec.fact("date_s_N", text)
    rec.fact("date_N_supported", re.fullmatch(r"\d+\.\d{9}", text) is not None)


def _quiet_reader(ctx: Context, rec: Recording) -> None:
    """What a reader whose filter matches nothing prints before a line matches.

    The order is that of ``since="now"`` (design 5.6): read the clock, log a
    line the filter does not match, then connect with ``-T <clock - 1 s>``, so
    logd always has an entry at or after T to deliver. With T newer than every
    entry, 7.0 printed nothing and could lose the next entry (C02
    ``T_after_newest_entry``, C05 start markers); the earlier order of this
    probe (``-T <clock>``, no unmatched line) could hit that on a quiet device.
    """
    adb = ctx.adb
    tag = f"lpq{ctx.run_id}"
    since = epoch_arg((ctx.device_epoch() - 1) * 1_000_000)
    rec.add(adb.shell("log_unmatched", f"log -t lpu{ctx.run_id} unmatched"))
    arrivals: list[float] = []

    def on_line(line: str) -> None:
        if tag in line:
            arrivals.append(time.monotonic())

    args = ["logcat", *U1A_FORMAT, "-T", since, "-s", f"{tag}:V"]
    with LiveReader(adb, "quiet_filter", args, on_line=on_line) as live:
        time.sleep(4)
        bytes_before, lines_before = live.snapshot()
        written_at = time.monotonic()
        rec.add(adb.shell("log_marker", f"log -t {tag} quiet-{ctx.run_id}"))
        live.wait_until(lambda: bool(arrivals), timeout=10)
    rec.add(live.stop())
    rec.fact(
        "quiet_reader",
        {
            "bytes_before_marker": bytes_before,
            "lines_before_marker": lines_before,
            # From the reader's spawn, like the step's first_byte_s.
            "marker_written_s": round(written_at - live.started_at, 3),
            "marker_write_to_arrival_s": round(arrivals[0] - written_at, 3)
            if arrivals
            else None,
        },
    )


def probe_c11(ctx: Context) -> Recording:
    """C11: logwrapper (tags, identical bursts) and that ``log -t`` writes to main."""
    rec = Recording(
        ctx, "C11", "logwrapper on this level; log -t writes to the main buffer"
    )
    adb = ctx.adb
    run = ctx.run_id
    rec.add(adb.shell("logwrapper_echo", f"logwrapper echo lp-lw-{run}"))
    rec.add(
        adb.shell(
            "logwrapper_burst",
            f"logwrapper sh -c 'for i in 1 2 3 4 5 6 7 8; do echo lp-burst-{run}; "
            "done'",
        )
    )
    rec.add(adb.shell("log_t", f"log -t logpyt lp-mark-{run}"))
    for buffer in ("main", "system", "crash"):
        step = adb.run(
            f"buffer_{buffer}", ["logcat", "-d", "-b", buffer, *U1A_FORMAT], timeout=120
        )
        mine = [
            line
            for line in step.stdout
            if (q := parse_u1a(line))
            and q.tag in {"echo", "sh", "logpyt"}
            and run in q.msg
        ]
        rec.add(step, stdout=mine)
        rec.fact(f"lines_in_{buffer}", len(mine))
    return rec


def probe_c12(ctx: Context) -> Recording:
    """C12: a host ``ANDROID_LOG_TAGS`` filters the device output unless removed."""
    rec = Recording(ctx, "C12", "ANDROID_LOG_TAGS from the host environment")
    adb = ctx.adb
    rec.add(adb.shell("log_t", f"log -t logpyt lp-tags-{ctx.run_id}"))
    cases: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
        ("tags_silent", "*:S", ()),
        ("tags_logpyt_only", "logpyt:V *:S", ()),
        ("tags_removed", None, ()),
        ("tags_silent_with_filterspec", "*:S", ("*:V",)),
    )
    for name, value, spec in cases:
        step = adb.run(
            name,
            ["logcat", "-d", "-t", "300", "-v", "threadtime", *spec],
            env={"ANDROID_LOG_TAGS": value},
        )
        tags = Counter(
            m[1]
            for line in entries(step.stdout)
            if (m := _THREADTIME_TAG_RE.match(line))
        )
        rec.add(step, stdout=step.stdout[:5])
        rec.fact(
            name,
            {
                "lines": len(entries(step.stdout)),
                "distinct_tags": len(tags),
                "top_tags": dict(tags.most_common(5)),
            },
        )
    return rec


def probe_c06(ctx: Context) -> Recording:
    """C06: logd pruning notices (chatty) after one uid floods the main buffer."""
    rec = Recording(
        ctx, "C06", "chatty expire/identical notices: text, timestamp, backlog, resume"
    )
    adb = ctx.adb
    count = ctx.lines_override or (40_000 if ctx.api < 30 else 800_000)
    prefix = f"lp-ch-{ctx.run_id}-"
    since_us = ctx.device_epoch() * 1_000_000
    writer = rec.add(adb.shell("writer", _writer(prefix, count), timeout=900))
    rec.fact("lines_written", count)
    rec.fact("writer_lines_per_s", round(count / writer.elapsed_s))
    rec.fact("liblog_drop_events", _liblog_drops(ctx, rec, since_us))
    dump = adb.run("dump", ["logcat", "-d", *U1A_FORMAT], timeout=180)
    body = entries(dump.stdout)
    rows = [(i, p) for i, line in enumerate(body) if (p := parse_u1a(line)) is not None]
    seq_re = re.compile(re.escape(prefix) + r"(\d+)$")
    own = [(i, p, int(m[1])) for i, p in rows if (m := seq_re.search(p.msg))]
    notices = [(i, p) for i, p in rows if p.tag == "chatty" or _NOTICE_RE.search(p.msg)]
    rec.fact("own_lines_kept", len(own))
    rec.fact("own_seq_kept_range", [own[0][2], own[-1][2]] if own else None)
    rec.fact("notices", len(notices))
    rec.fact("notices_by_uid", dict(Counter(p.uid for _, p in notices).most_common()))
    details, context = _notice_details(body, rows, notices)
    rec.fact(
        "notices_between_list_neighbors",
        sum(1 for item in details if item.get("between_list_neighbors")),
    )
    rec.fact("notice_details", details)
    rec.add(dump, stdout=context)
    if notices:
        _notices_in_resume(ctx, rec, notices[0][1].us - 1_000_000)
    return rec


def _notices_in_resume(ctx: Context, rec: Recording, t: int) -> None:
    """Does a ``-T`` read from 1 s before the first notice return the notices?"""
    resume = ctx.adb.run(
        "dump_from_1s_before_first_notice",
        ["logcat", "-d", *U1A_FORMAT, "-T", epoch_arg(t)],
        timeout=180,
    )
    found = [
        line
        for line in entries(resume.stdout)
        if (p := parse_u1a(line)) and (p.tag == "chatty" or _NOTICE_RE.search(p.msg))
    ]
    rec.add(resume, stdout=[*resume.stdout[:3], *found[:10]])
    rec.fact("notices_in_resume_from_1s_before_first_notice", len(found))


def _notice_details(
    body: Sequence[str],
    rows: Sequence[tuple[int, Line]],
    notices: Sequence[tuple[int, Line]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Place each notice's timestamp between its list neighbors; return the context."""
    position = {i: n for n, (i, _) in enumerate(rows)}
    details: list[dict[str, Any]] = []
    keep: set[int] = set()
    for i, note in notices[:12]:
        keep.update(range(max(0, i - 2), min(len(body), i + 3)))
        m = _NOTICE_RE.search(note.msg)
        item: dict[str, Any] = {
            "line": note.raw[:MAX_LINE_CHARS],
            "count": int(m[1]) if m else None,
        }
        n = position[i]
        if 0 < n < len(rows) - 1:
            prev, nxt = rows[n - 1][1], rows[n + 1][1]
            item["ts_minus_prev_line_us"] = note.us - prev.us
            item["next_line_minus_ts_us"] = nxt.us - note.us
            item["between_list_neighbors"] = prev.us <= note.us <= nxt.us
        else:
            # The first or last parsed row has one neighbor: nothing to compare.
            item["between_list_neighbors"] = None
        details.append(item)
    return details, [body[i] for i in sorted(keep)]


class _SeqSink:
    """Marks the sequence numbers a reader received."""

    def __init__(self, prefix: str, count: int) -> None:
        self.pattern = re.compile(re.escape(prefix) + r"(\d+|start|end) *$")
        self.seen = bytearray(count)
        self.started = self.ended = False
        self.other: list[str] = []
        self.duplicates = 0

    def __call__(self, line: str) -> None:
        m = self.pattern.search(line)
        if m is None:
            if len(self.other) < 30:
                self.other.append(line[:MAX_LINE_CHARS])
            return
        if m[1] == "start":
            self.started = True
        elif m[1] == "end":
            self.ended = True
        elif (index := int(m[1])) < len(self.seen):
            self.duplicates += self.seen[index]
            self.seen[index] = 1

    def facts(self, lines_read: int) -> dict[str, Any]:
        """Summarize what the reader received (``lines_read``: all lines it read)."""
        ranges = missing_ranges(self.seen)
        return {
            "received": sum(self.seen),
            "missing": sum(b - a + 1 for a, b in ranges),
            "missing_ranges": ranges[:40],
            "missing_range_count": len(ranges),
            "duplicates": self.duplicates,
            "saw_start": self.started,
            "saw_end": self.ended,
            "other_lines": self.other,
            "lines_read": lines_read,
        }


@contextlib.contextmanager
def rooted(ctx: Context, rec: Recording) -> Iterator[None]:
    """Run the body with adbd as root (``adb root``), then ``adb unroot``."""
    rec.add(ctx.adb.run("adb_root", ["root"]))
    _wait_uid(ctx, rec, "0")
    try:
        yield
    finally:
        rec.add(ctx.adb.run("adb_unroot", ["unroot"]))
        _wait_uid(ctx, rec, "2000")


def _wait_uid(ctx: Context, rec: Recording, want: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ctx.adb.run("wait_for_device", ["wait-for-device"], timeout=30)
        step = ctx.adb.shell("id_u", "id -u")
        if step.stdout and step.stdout[0].strip() == want:
            rec.add(step)
            return
        time.sleep(1)
    rec.note(f"adbd did not come back with uid {want} within {timeout:.0f} s")


C05_RUNS: Final = (
    # (label, lines on API < 30, lines on API >= 30, sleep 10 ms after every N
    # lines, stalled reader: None = stops reading, else reads at most bytes/s)
    ("fast", 150_000, 400_000, 100, None),
    ("slow", 150_000, 150_000, 10, None),
    ("slow_reader", 300_000, 300_000, 20, 50_000.0),
)


def probe_c05(ctx: Context) -> Recording:
    """C05: a reader that stops reading under heavy logging, beside a control reader.

    ``fast`` writes at about 6-10k lines/s, ``slow`` at under 1k lines/s; both
    write far more than the device ring plus the adb pipeline can hold, while
    the stalled reader reads nothing. In ``slow_reader`` it keeps reading at
    50 kB/s (about 600 lines/s) while about 2k lines/s are written.
    """
    rec = Recording(
        ctx, "C05", "Stalled reader under heavy logging: skip, notice or disconnect?"
    )
    for label, small, large, pause_every, rate in C05_RUNS:
        count = ctx.lines_override or (small if ctx.api < 30 else large)
        rec.fact(label, _stall_run(ctx, rec, label, count, pause_every, rate))
    if ctx.allow_root:
        with rooted(ctx, rec):
            rec.add(
                ctx.adb.shell(
                    "dmesg_logd", "dmesg | grep -i -E 'logd|reader|prune' | tail -n 30"
                )
            )
    else:
        rec.note("logd's own log (dmesg) not read: needs --root")
    return rec


def _stall_run(
    ctx: Context,
    rec: Recording,
    label: str,
    count: int,
    pause_every: int,
    rate: float | None,
) -> dict[str, Any]:
    """One stall: both readers connect, one stops reading while the writer runs."""
    adb = ctx.adb
    prefix = f"lp-st-{ctx.run_id}{label}-"
    since = f"{ctx.device_epoch()}.000000"
    args = ["logcat", *U1A_FORMAT, "-T", since, "-s", "sh:V"]
    control_sink, stalled_sink = _SeqSink(prefix, count), _SeqSink(prefix, count)
    writer_cmd = _writer(prefix, count, pause_every=pause_every)
    with (
        LiveReader(
            adb, f"{label}_control_reader", args, keep=5, on_line=control_sink
        ) as control,
        LiveReader(
            adb, f"{label}_stalled_reader", args, keep=5, on_line=stalled_sink
        ) as stalled,
    ):
        time.sleep(2)  # both readers connected before the first marker
        for attempt in range(3):
            rec.add(
                adb.shell(f"{label}_start_marker_{attempt}", f"log -t sh {prefix}start")
            )
            if control.wait_until(
                lambda: control_sink.started, 5
            ) and stalled.wait_until(lambda: stalled_sink.started, 5):
                break
        if rate is None:
            stalled.pause()
        else:
            stalled.throttle(rate)
        paused_at, bytes_at_pause = time.monotonic(), stalled.bytes_read
        writer = rec.add(adb.shell(f"{label}_writer", writer_cmd, timeout=900))
        rec.add(adb.shell(f"{label}_end_marker", f"log -t sh {prefix}end"))
        time.sleep(3)
        facts: dict[str, Any] = {
            "lines_written": count,
            "writer_s": round(writer.elapsed_s, 1),
            "writer_lines_per_s": round(count / writer.elapsed_s),
            "stall_s": round(time.monotonic() - paused_at, 1),
            "stalled_reader_rc_during_stall": stalled.proc.poll(),
            "stalled_reader_bytes_during_stall": stalled.bytes_read - bytes_at_pause,
        }
        stalled.resume()
        resumed_at = time.monotonic()
        for reader, sink in ((control, control_sink), (stalled, stalled_sink)):
            reader.wait_until(
                lambda r=reader, s=sink: (
                    s.ended
                    or time.monotonic() - max(r.last_byte_at or 0.0, resumed_at) > 10
                ),
                300,
            )
        facts["stalled_reader_rc_after_resume"] = stalled.proc.poll()
    rec.add(control.stop())
    rec.add(stalled.stop())
    facts["control_reader"] = control_sink.facts(control.line_count)
    facts["stalled_reader"] = stalled_sink.facts(stalled.line_count)
    facts["liblog_drop_events"] = _liblog_drops(
        ctx, rec, int(since.split(".")[0]) * 1_000_000, name=f"{label}_liblog_events"
    )
    kernel = adb.run(
        f"{label}_logd_in_kernel_buffer",
        ["logcat", "-d", "-b", "kernel", *U1A_FORMAT, "-T", since],
    )
    logd_lines = [x for x in kernel.stdout if (q := parse_u1a(x)) and q.tag == "logd"]
    rec.add(kernel, stdout=[x for x in logd_lines if "logdr" not in x][-30:])
    facts["ring_after"] = adb.run("ring_after", ["logcat", "-g"]).stdout
    return facts


def _relevant(lines: Sequence[str], pid: int | None) -> list[str]:
    keep: list[str] = []
    debug = 0
    for line in lines:
        parsed = parse_u1a(line)
        if parsed is None or len(keep) >= 80:
            continue
        if parsed.tag == "DEBUG":
            debug += 1
            if debug > 30:
                continue
        if (
            parsed.pid == pid
            or parsed.tag in _CRASH_TAGS
            or APP_PACKAGE in parsed.msg
            or "ANR" in parsed.msg
        ):
            keep.append(line[:MAX_LINE_CHARS])
    return keep


def _collect(
    ctx: Context, rec: Recording, label: str, since: int, pid: int | None
) -> None:
    summary: dict[str, Any] = {"pid": pid}
    for buffer in ("main", "system", "crash"):
        step = ctx.adb.run(
            f"{label}_{buffer}",
            ["logcat", "-d", "-b", buffer, *U1A_FORMAT, "-T", f"{since}.000000"],
            timeout=120,
        )
        kept = _relevant(step.stdout, pid)
        rec.add(step, stdout=kept)
        tags = Counter(p.tag for line in kept if (p := parse_u1a(line)) is not None)
        summary[buffer] = dict(tags.most_common())
    rec.fact(label, summary)


def _app_pid(ctx: Context, rec: Recording, label: str) -> int | None:
    """Start the app fresh (no crash dialog in front) and return its pid."""
    fresh = (
        "am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS > /dev/null; "
        f"am force-stop {APP_PACKAGE}; sleep 1; am start -W -n {APP_PACKAGE}/.Settings"
    )
    rec.add(ctx.adb.shell(f"{label}_am_start", fresh))
    time.sleep(3)
    rec.add(ctx.adb.shell(f"{label}_focus", "dumpsys window | grep -m1 mCurrentFocus"))
    step = rec.add(ctx.adb.shell(f"{label}_pidof", f"pidof {APP_PACKAGE}"))
    words = step.stdout[0].split() if step.stdout else []
    return int(words[0]) if words and words[0].isdigit() else None


def _anr(ctx: Context, rec: Recording) -> None:
    """Stop the app's process (SIGSTOP) and send it input until an ANR is logged."""
    adb = ctx.adb
    pid = _app_pid(ctx, rec, "anr")
    since = ctx.device_epoch() - 1
    if pid is None:
        rec.note("ANR: the app did not start")
        return
    rec.add(adb.shell("kill_stop", f"kill -STOP {pid}"))
    rec.add(
        adb.shell(
            "input", "input keyevent 20; sleep 1; input keyevent 20; input tap 200 400"
        )
    )
    args = [
        "logcat",
        "-d",
        "-b",
        "main",
        "-b",
        "system",
        *U1A_FORMAT,
        "-T",
        f"{since}.000000",
    ]
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if any("ANR in" in line for line in adb.run("anr_poll", args).stdout):
            break
        time.sleep(3)
    rec.add(adb.shell("kill_cont", f"kill -CONT {pid}"))
    time.sleep(2)
    _collect(ctx, rec, "anr", since, pid)


def probe_c07(ctx: Context) -> Recording:
    """C07: the default buffers; ANR, native crash and Java crash lines of an app.

    Each case starts the app (``com.android.settings``) fresh; ``adb root``
    is needed for the signals. The app runs as the system uid 1000, so the
    recording cannot show the uid of crash_dump, DEBUG and tombstoned lines
    for an app with an app uid (>= 10000).
    """
    rec = Recording(
        ctx, "C07", "Default buffers; ANR, native crash and Java crash lines"
    )
    adb = ctx.adb
    rec.add(adb.run("buffers_default", ["logcat", "-g"]))
    rec.add(
        adb.shell("am_help_crash", "am help 2>&1 | grep -i -E '^ *crash' ; echo rc=$?")
    )
    with rooted(ctx, rec):
        _anr(ctx, rec)
        pid = _app_pid(ctx, rec, "native")
        since = ctx.device_epoch() - 1
        if pid is not None:
            rec.add(adb.shell("kill_segv", f"kill -SEGV {pid}"))
            time.sleep(8)
            _collect(ctx, rec, "native_crash", since, pid)
        pid = _app_pid(ctx, rec, "am_crash")
        since = ctx.device_epoch() - 1
        rec.add(adb.shell("am_crash", f"am crash {APP_PACKAGE}"))
        time.sleep(6)
        _collect(ctx, rec, "java_crash_am_crash", since, pid)
        _app_pid(ctx, rec, "bad_fragment")
        since = ctx.device_epoch() - 1
        bad = "com.example.logpyt.NoSuchFragment"
        rec.add(
            adb.shell(
                "am_start_bad_fragment",
                f"am start -W -n {APP_PACKAGE}/.Settings "
                f"--es :settings:show_fragment {bad}",
            )
        )
        time.sleep(6)
        _collect(ctx, rec, "java_crash_bad_fragment", since, None)
        close = "am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS"
        rec.add(
            adb.shell("cleanup", f"{close} > /dev/null; am force-stop {APP_PACKAGE}")
        )
    return rec


def probe_c09(ctx: Context) -> Recording:
    """C09 (on the server host): does a client-started server share its group, pipes?"""
    rec = Recording(
        ctx, "C09", "adb server started by a client: process group, session, pipes"
    )
    rec.data["device"] = {
        "adb_client": ctx.device.get("adb_client", ""),
        "host": "emuvm (server host)",
    }
    if ctx.vm is None:
        rec.note("needs --vm-ssh")
        return rec
    step = rec.add(
        ctx.vm.run(
            "process_group_probe",
            f"python3 - {ctx.vm.adb}",
            stdin=Path(__file__).with_name("adb_server_group.py").read_bytes(),
            timeout=180,
        )
    )
    if step.stdout:
        with contextlib.suppress(json.JSONDecodeError):
            rec.fact("result", json.loads(step.stdout[-1]))
    _wait_device(ctx, rec, "after_c09")
    return rec


def _wait_device(ctx: Context, rec: Recording, label: str) -> None:
    step = rec.add(
        ctx.adb.run(f"wait_for_device_{label}", ["wait-for-device"], timeout=60)
    )
    if step.timed_out and ctx.vm is not None:
        rec.add(
            ctx.vm.run(
                f"server_restart_{label}",
                f"{ctx.vm.adb} kill-server; sleep 1; {ctx.vm.adb} start-server",
            )
        )
        rec.add(
            ctx.adb.run(
                f"wait_for_device_{label}_retry", ["wait-for-device"], timeout=60
            )
        )


def _stream_case(
    ctx: Context, rec: Recording, name: str, action: Callable[[], object]
) -> None:
    """Run ``action`` against a running ``adb logcat``; record how it ended.

    ``seconds_to_exit`` runs from the start of the action to the child's exit
    as the reader's exit watcher stamped it, so an exit during a long action
    is not counted as late as the action's end. ``action_s`` is how long the
    action took. (Recordings before this change measured until ``wait_exit``
    returned, after the action had finished: an upper bound.)
    """
    with LiveReader(
        ctx.adb, f"{name}_stream", ["logcat", "-v", "threadtime"], keep=0
    ) as live:
        live.wait_lines(1, timeout=15)
        time.sleep(1)
        before = live.line_count
        acted_at = time.monotonic()
        action()
        action_s = time.monotonic() - acted_at
        rc = live.wait_exit(30)
    step = live.stop()  # also joins the exit watcher, so exited_at is final
    rec.add(step, stdout=[])
    exited_at = live.exited_at
    rec.fact(
        name,
        {
            "exited_by_itself": rc is not None,
            "rc": rc,
            "seconds_to_exit": round(exited_at - acted_at, 2)
            if rc is not None and exited_at is not None
            else None,
            "action_s": round(action_s, 3),
            "stderr": step.stderr[:5],
            "lines_before_action": before,
            "lines_read": live.line_count,
        },
    )


def probe_c04(ctx: Context) -> Recording:
    """C04: what a running ``adb logcat`` reports on each kind of disconnect."""
    rec = Recording(
        ctx,
        "C04",
        "adb logcat on: device logcat killed, reconnect, server restart, unplug",
    )
    adb, vm, serial = ctx.adb, ctx.vm, ctx.adb.serial

    def kill_device_logcat() -> None:
        pids = rec.add(adb.shell("pidof_logcat", "pidof logcat"))
        if pids.stdout:
            rec.add(adb.shell("kill_logcat", f"kill {pids.stdout[0].strip()}"))

    _stream_case(ctx, rec, "device_logcat_killed", kill_device_logcat)
    _stream_case(
        ctx, rec, "adb_reconnect", lambda: rec.add(adb.run("reconnect", ["reconnect"]))
    )
    _wait_device(ctx, rec, "after_reconnect")
    if vm is None:
        rec.note("server restart and unplug not run: needs --vm-ssh")
        return rec
    restart = f"{vm.adb} kill-server; sleep 1; {vm.adb} start-server"
    _stream_case(
        ctx, rec, "server_restart", lambda: rec.add(vm.run("server_restart", restart))
    )
    _wait_device(ctx, rec, "after_server_restart")
    _stream_case(
        ctx,
        rec,
        "unplug",
        lambda: rec.add(vm.run("emu_kill", f"{vm.adb} -s {serial} emu kill")),
    )
    for name, args in (
        ("get_state_after_unplug", ["get-state"]),
        ("shell_date_after_unplug", ["shell", "date"]),
        ("logcat_after_unplug", ["logcat", "-d", "-t", "1"]),
    ):
        step = rec.add(adb.run(name, args, timeout=10))
        rec.fact(
            name,
            {"exited": not step.timed_out, "rc": step.rc, "stderr": step.stderr[:3]},
        )
    rec.note("the emulator was killed by the unplug case and stays down")
    return rec


@dataclass(frozen=True)
class Probe:
    """One recording: its key, file slug, function and requirements."""

    key: str
    slug: str
    run: Callable[[Context], Recording]
    needs: frozenset[str] = frozenset()
    host: bool = False


PROBES: Final[tuple[Probe, ...]] = (
    Probe("R5", "ring_backlog", probe_r5),
    Probe("R1", "output_shapes", probe_r1),
    Probe("R6", "u1a_argv", probe_r6),
    Probe("C08", "process_format", probe_c08),
    Probe("R2", "tail_time", probe_r2),
    Probe("C02", "epoch_usec_boundary", probe_c02),
    Probe("R3", "pidof_shell", probe_r3),
    Probe("C03", "uid_lookup", probe_c03),
    Probe("C10", "clock_first_byte", probe_c10),
    Probe("C11", "logwrapper_log_t", probe_c11),
    Probe("C12", "android_log_tags", probe_c12),
    Probe("R4", "absent_serial", probe_r4),
    Probe("C06", "chatty", probe_c06),
    Probe("C05", "stalled_reader", probe_c05),
    Probe("C07", "crash_anr", probe_c07, needs=frozenset({"root"})),
    # C09 stops the server twice: every adb connection of the server host drops.
    Probe(
        "C09",
        "server_process_group",
        probe_c09,
        needs=frozenset({"vm", "disruptive"}),
        host=True,
    ),
    Probe("C04", "disconnects", probe_c04, needs=frozenset({"disruptive"})),
)
PROBES_BY_KEY: Final = {probe.key: probe for probe in PROBES}


def write_recording(rec: Recording, directory: Path, slug: str) -> Path:
    """Write ``rec`` as ``<directory>/<KEY>_<slug>.json`` (replacing older slugs)."""
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob(f"{rec.key}_*.json"):
        old.unlink()
    path = directory / f"{rec.key}_{slug}.json"
    text = json.dumps(rec.data, indent=1, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.partition("\n")[0] if __doc__ else None
    )
    parser.add_argument(
        "--adb", default="adb", help="adb client executable (default: adb on PATH)"
    )
    parser.add_argument(
        "--serial", required=True, help="device serial, e.g. emulator-5556"
    )
    parser.add_argument(
        "--out",
        type=Path,
        help=(
            "output directory (default: tests/fixtures/device/api<sdk>); "
            "C09 goes to <out>/host (default: tests/fixtures/device/host)"
        ),
    )
    parser.add_argument(
        "--only", default="", help="comma-separated probe keys, e.g. R6,C02"
    )
    parser.add_argument(
        "--root", action="store_true", help="allow steps that run adb root"
    )
    parser.add_argument(
        "--vm-ssh",
        help="executable that runs its argument as a shell command on the server host",
    )
    parser.add_argument(
        "--vm-adb",
        default="~/android-sdk/platform-tools/adb",
        help=f"adb path on the server host (recorded as {VM_ADB_PLACEHOLDER})",
    )
    parser.add_argument(
        "--disruptive",
        action="store_true",
        help=(
            "allow the probes that cut every connection of the adb server: "
            "C09 (stops the server twice) and C04 (reconnect, server restart, "
            "unplug: the emulator stays down)"
        ),
    )
    parser.add_argument(
        "--lines", type=int, help="override the flood size of C05 and C06"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected probes and write their recordings."""
    args = _parse_args(argv)
    adb = Adb(args.adb, args.serial)
    vm = Vm(args.vm_ssh, args.vm_adb) if args.vm_ssh else None
    only = {key.strip().upper() for key in args.only.split(",") if key.strip()}
    unknown = only - PROBES_BY_KEY.keys()
    if unknown:
        print(f"unknown probe keys: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    granted = {
        name
        for name, ok in (
            ("root", args.root),
            ("vm", vm),
            ("disruptive", args.disruptive),
        )
        if ok
    }
    try:
        ctx = Context.connect(adb, vm=vm, allow_root=args.root)
        ctx.lines_override = args.lines
        out = args.out or FIXTURES / f"api{ctx.api}"
        host_out = args.out / "host" if args.out else FIXTURES / "host"
        for probe in PROBES:
            if only and probe.key not in only:
                continue
            if missing := probe.needs - granted:
                print(f"{probe.key}: skipped (needs {', '.join(sorted(missing))})")
                continue
            started = time.monotonic()
            rec = probe.run(ctx)
            path = write_recording(rec, host_out if probe.host else out, probe.slug)
            facts = json.dumps(rec.data["facts"], ensure_ascii=False)
            took = time.monotonic() - started
            print(f"{probe.key}: {took:.0f} s -> {path.name}: {facts[:300]}")
    except (TunnelDownError, DeviceNotReadyError) as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
