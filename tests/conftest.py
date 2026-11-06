"""Shared fixtures: a real-subprocess fake adb and a per-test hang watchdog.

The child side and the config format are described in ``tests/fake_adb.py``.

``fake_adb`` fixture (a :class:`FakeAdb`)
    Patches ``subprocess.Popen`` and ``asyncio.create_subprocess_exec`` so
    that a spawn of ``fake.path`` runs ``tests/fake_adb.py`` instead, and
    patches ``resolve_adb`` to return ``fake.path``. Configure behavior with
    ``logcat()`` / ``pidof()`` / ``dumpsys()`` / ``date()`` /
    ``get_state()`` / ``device_absent()`` / ``command()``, observe it with
    ``calls()``, ``wait_for()`` and ``release()``. Every stream connection
    reads the device clock first (``adb shell date +%s``), which the fake
    answers with the host clock unless ``date()`` scripts it. Outside pytest,
    ``install_fake_adb()`` sets up the same interception
    (``bench/stream_bench.py`` uses it).

Child registry
    Every fake adb child is registered, with its ``subprocess.Popen`` or
    ``asyncio.subprocess.Process``, before the spawn call returns. Use
    ``children()``, ``live_children()``, ``survivors()`` or
    ``async_survivors()`` (every platform; no pid probing) to check that the
    library ended its children. The fixture finalizer releases the spawn gate
    and then kills and reaps, with bounded waits, every child still alive, so
    a library that leaks a child cannot leave it running.

Spawn gate (deterministic "during the spawn" interleavings)
    ``hold_spawn()`` makes the next fake adb spawns block after the child
    process exists but before ``Popen()`` / ``create_subprocess_exec()``
    returns it to the library. Wait for the acknowledgement, act, release::

        gate = fake_adb.hold_spawn(command="logcat")
        stream.start()
        gate.wait_entered()       # async: await gate.async_wait_entered()
        stream.kill()             # lands while the library is inside the spawn
        gate.release()

    ``with fake_adb.hold_spawn() as gate:`` releases on exit, and the fixture
    finalizer always releases. A sync spawn blocks its calling thread, so the
    spawn must come from another thread (the library's) than the one that
    releases. An unreleased gate gives up after its ``timeout`` and fails the
    test in teardown.

Hang watchdog (autouse)
    Each test (setup, call and teardown) runs under
    ``faulthandler.dump_traceback_later(limit, exit=True)``: a test that
    deadlocks dumps every thread's traceback to the real stderr and ends
    pytest with exit status 1 instead of hanging CI. The limit is
    ``WATCHDOG_DEFAULT_LIMIT`` seconds; the ``LOGPYT_TEST_WATCHDOG`` environment
    variable overrides it, and ``0`` disables it (for a debugger session).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import errno
import faulthandler
import json
import operator
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.fake_adb import split_command

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from types import TracebackType

_FAKE_ADB_SCRIPT = Path(__file__).with_name("fake_adb.py").resolve()
# The interpreter that runs fake_adb.py. On Windows a venv's sys.executable is
# a launcher that starts the real python.exe as a second process, so the pid
# the library sees (the launcher) would differ from the fake's own pid. The
# fake uses only the standard library under -I, so the base interpreter runs
# it directly and keeps one process per spawn on every platform.
_FAKE_PYTHON = getattr(sys, "_base_executable", "") or sys.executable

# Captured before any fixture patches the name: asyncio's Windows children are
# a subclass of the real Popen, not of the fixture's rewriting subclass.
_REAL_POPEN = subprocess.Popen

# The tests construct streams and call the adb helpers without ``adb_path`` on
# purpose, so the default resolution path stays covered. Library code calls
# ``logpyt.adb.resolve_adb`` through the module, never by a name bound at import,
# so this is the single patch target (.docs/design-3.0.0.md, PR-08);
# tests/test_adb.py proves it with an empty PATH.
RESOLVE_ADB_TARGETS = ("logpyt.adb.resolve_adb",)

WATCHDOG_DEFAULT_LIMIT = 120.0
WATCHDOG_ENV = "LOGPYT_TEST_WATCHDOG"

_SPAWN_GATE_TIMEOUT = 30.0
_REAP_TIMEOUT = 10.0
_RETRY_TIMEOUT = 5.0
_POLL = 0.005

Step = dict[str, Any]
Run = dict[str, Any]


def out(text: str) -> Step:
    """Write ``text`` to stdout exactly (UTF-8, no newline added)."""
    return {"out": text}


def err(text: str) -> Step:
    """Write ``text`` to stderr exactly (UTF-8, no newline added)."""
    return {"err": text}


def raw(data: bytes) -> Step:
    """Write raw bytes to stdout."""
    return {"out_b64": base64.b64encode(data).decode("ascii")}


def lines(count: int, template: str, *, stream: str = "out") -> Step:
    """Write ``template.format(i=i)`` for i in range(count) in large chunks."""
    return {"lines": {"count": count, "template": template, "stream": stream}}


def sleep(seconds: float) -> Step:
    """Pause the fake adb."""
    return {"sleep": seconds}


def wait(name: str) -> Step:
    """Block the fake adb until the test calls ``FakeAdb.release(name)``."""
    return {"wait": name}


def touch(name: str) -> Step:
    """Signal the test; observe it with ``FakeAdb.wait_for(name)``."""
    return {"touch": name}


def hold_stdout(until: str, then: str = "") -> Step:
    """Start a grandchild that holds adb's stdout until ``release(until)``.

    The grandchild then writes ``then`` to that stdout and exits; the fake adb
    goes on with its next step at once (``tests/fake_adb.py``).
    """
    return {"hold_stdout": {"until": until, "then": then}}


def run(
    *steps: Step,
    exit_code: int = 0,
    hang: bool = False,
    ignore_sigterm: bool = False,
) -> Run:
    """One invocation's behavior: steps, then exit (or hang until killed)."""
    return {
        "steps": list(steps),
        "exit": exit_code,
        "hang": hang,
        "ignore_sigterm": ignore_sigterm,
    }


def threadtime(
    message: str,
    *,
    pid: int = 1234,
    tid: int = 1234,
    level: str = "I",
    tag: str = "Tag",
    date: str = "01-02",
    time_: str = "03:04:05.678",
) -> str:
    """Format one ``-v threadtime`` line, newline included."""
    return f"{date} {time_} {pid:5d} {tid:5d} {level} {tag}: {message}\n"


# The live wire format of design 5.6 (U-1): -v threadtime -v year -v usec -v zone
# -v uid, as API 24 and 36 print it (tests/fixtures/device/*/R6_u1a_argv.json).
LIVE_STAMP = "2026-11-04 05:11:16.661446 +0000"


def logcat_line(
    message: str,
    *,
    uid: int | str = 10065,
    pid: int = 1234,
    tid: int | None = None,
    level: str = "I",
    tag: str = "Tag",
    stamp: str = LIVE_STAMP,
) -> str:
    """Format one line of the live wire format, newline included.

    ``uid`` is the ``-v uid`` column: a number, or a short name such as
    ``root`` (design 5.8). The default uid is an app uid; script its package
    with ``FakeAdb.dumpsys(package, run(out(dumpsys_package(package, uid))))``.
    """
    thread = pid if tid is None else tid
    return f"{stamp} {uid!s:>5} {pid:5d} {thread:5d} {level} {tag}: {message}\n"


def device_date(seconds: int) -> str:
    """The stdout of ``adb shell date +%s`` on a device whose clock is ``seconds``."""
    return f"{seconds}\n"


def waiting_for_device() -> Run:
    """``adb logcat`` for a serial that is not attached: it waits, it never exits.

    adb prints ``- waiting for device -`` on stderr and nothing on stdout,
    still waiting after 10 s (FINDINGS.md, check 4; ``R4_absent_serial.json``).
    """
    return run(err("- waiting for device -\n"), hang=True)


def not_found(serial: str) -> Run:
    """``adb get-state`` for a serial adb does not know (``R4_absent_serial.json``)."""
    return run(err(f"error: device '{serial}' not found\n"), exit_code=1)


def dumpsys_package(
    package: str, uid: int | None = None, *, key: str = "userId"
) -> str:
    """The stdout of ``dumpsys package <package>`` for an install with ``uid``.

    ``key`` is ``userId`` (API 24) or ``appId`` (API 36; FINDINGS check 3,
    design A.8). With ``uid=None`` the package is not installed: the output
    has no record of it.
    """
    text = "Activity Resolver Table:\n  Non-Data Actions:\n\nPackages:\n"
    if uid is None:
        return text
    return (
        f"{text}  Package [{package}] (4c3a2e1):\n"
        f"    {key}={uid}\n"
        "    pkg=Package{4c3a2e1 " + package + "}\n"
        "    versionCode=1 minSdk=24 targetSdk=36\n"
    )


def _posix_child_running(pid: int) -> bool:
    """True while our child ``pid`` has not exited. Never reaps it.

    asyncio reaps its own children; a ``waitpid`` from here would steal the
    exit status and make ``Process.wait()`` report 255. ``WNOWAIT`` leaves the
    status in place.
    """
    if sys.platform == "win32":
        msg = "POSIX only"
        raise NotImplementedError(msg)
    waitid = getattr(os, "waitid", None)
    if waitid is None:  # no waitid (macOS): a zombie still counts as running
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True
    try:
        status = waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:  # already reaped, so it has exited
        return False
    return status is None


@dataclasses.dataclass(frozen=True, eq=False)
class SpawnedChild:
    """One fake adb child, as the harness saw it spawned.

    ``index`` is the per-command invocation number that selected its run (the
    ``index`` of its ``calls()`` record), ``args`` the adb arguments the
    library passed, ``is_async`` whether it came from
    ``asyncio.create_subprocess_exec``. ``process`` is that asyncio Process
    (else None) and ``popen`` the Popen behind the child; check liveness with
    ``alive()``, not ``popen.poll()``, which on POSIX would steal an asyncio
    child's exit status.
    """

    pid: int
    command: str
    key: str
    index: int
    args: tuple[str, ...]
    is_async: bool
    popen: subprocess.Popen[Any] = dataclasses.field(repr=False)
    process: asyncio.subprocess.Process | None = dataclasses.field(
        repr=False, default=None
    )

    @staticmethod
    def pipe_closed(pipe: object) -> bool:
        """Whether ``pipe``, one of ``popen``'s, is closed (or was never opened).

        A sync child's pipe is a file object; an asyncio child's on Windows is
        an overlapped ``PipeHandle``, whose handle is None once closed.
        """
        if pipe is None:
            return True
        closed = getattr(pipe, "closed", None)
        if closed is not None:
            return bool(closed)
        return getattr(pipe, "handle", None) is None

    def pipes_closed(self) -> bool:
        """Whether both of the child's pipes were closed (by the library)."""
        return all(
            self.pipe_closed(pipe) for pipe in (self.popen.stdout, self.popen.stderr)
        )

    def alive(self) -> bool:
        """True until the child has exited (exited-but-unreaped counts as gone).

        Safe to call at any time from any thread: it never takes an exit
        status away from the library's own ``wait()``.
        """
        if self.process is None:
            return self.popen.poll() is None
        if self.process.returncode is not None:
            return False
        if sys.platform == "win32":  # polling a handle reaps nothing on Windows
            return self.popen.poll() is None
        return _posix_child_running(self.pid)


@dataclasses.dataclass(frozen=True)
class _PendingSpawn:
    command: str
    key: str
    index: int
    seq: int
    args: tuple[str, ...]


class SpawnGate:
    """Holds fake adb spawns between child creation and the spawn's return.

    Created by ``FakeAdb.hold_spawn()``. While the gate is closed, every
    matching spawn (``command`` None matches all) registers its child, counts
    as *entered*, and blocks until ``release()``. A released gate stays open.
    """

    def __init__(self, fake: FakeAdb, command: str | None, timeout: float) -> None:
        self.command = command
        self.timeout = timeout
        self._fake = fake
        self._open = threading.Event()
        self._lock = threading.Lock()
        self._held: list[SpawnedChild] = []

    @property
    def released(self) -> bool:
        """True once ``release()`` ran."""
        return self._open.is_set()

    @property
    def entered(self) -> int:
        """How many spawns have reached the gate (held now or earlier)."""
        with self._lock:
            return len(self._held)

    @property
    def children(self) -> list[SpawnedChild]:
        """The children whose spawns reached the gate, in arrival order."""
        with self._lock:
            return list(self._held)

    def release(self) -> None:
        """Open the gate: held spawns return, later ones pass straight through."""
        self._open.set()

    def wait_entered(self, count: int = 1, timeout: float = 10.0) -> list[SpawnedChild]:
        """Block until ``count`` spawns are held; return the held children.

        Raises TimeoutError when fewer than ``count`` arrive within ``timeout``.
        """
        try:
            wait_until(lambda: self.entered >= count, timeout=timeout)
        except TimeoutError:
            raise TimeoutError(self._shortfall(count, timeout)) from None
        return self.children

    async def async_wait_entered(
        self, count: int = 1, timeout: float = 10.0
    ) -> list[SpawnedChild]:
        """Async variant of ``wait_entered``; lets the event loop run meanwhile."""
        try:
            await async_wait_until(lambda: self.entered >= count, timeout=timeout)
        except TimeoutError:
            raise TimeoutError(self._shortfall(count, timeout)) from None
        return self.children

    def _shortfall(self, count: int, timeout: float) -> str:
        return (
            f"{self.entered} of {count} fake adb spawns (command={self.command!r})"
            f" reached the gate within {timeout} s"
        )

    def __enter__(self) -> SpawnGate:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # -- used by the spawn wrappers -----------------------------------------

    def _admit(self, child: SpawnedChild) -> bool:
        if self.released or (
            self.command is not None and child.command != self.command
        ):
            return False
        with self._lock:
            self._held.append(child)
        return True

    def _timed_out(self, child: SpawnedChild) -> None:
        self._fake._errors.append(
            f"spawn gate never released within {self.timeout} s for {child!r}"
        )

    def _hold(self, child: SpawnedChild) -> None:
        if self._admit(child) and not self._open.wait(self.timeout):
            self._timed_out(child)

    async def _async_hold(self, child: SpawnedChild) -> None:
        if not self._admit(child):
            return
        deadline = time.monotonic() + self.timeout
        while not self._open.is_set():
            if time.monotonic() > deadline:
                self._timed_out(child)
                return
            await asyncio.sleep(_POLL)


class FakeAdb:
    """Handle returned by the ``fake_adb`` fixture (see the module docstring).

    Pass ``fake.path`` as ``adb_path`` (``resolve_adb`` is also patched to
    return it). Configure behavior with ``logcat()`` / ``pidof()`` /
    ``dumpsys()`` / ``command()`` before the library spawns adb.
    """

    def __init__(self, state: Path) -> None:
        self.state = state
        (state / "signals").mkdir(parents=True)
        (state / "calls").mkdir()
        self.path = str(state / "adb")
        self._config: dict[str, Any] = {}
        self._write_config()
        self._lock = threading.Lock()
        self._seq = 0
        self._counters: dict[str, int] = {}
        self._children: list[SpawnedChild] = []
        self._gate: SpawnGate | None = None
        self._errors: list[str] = []
        self._closed = False

    # -- configuration -------------------------------------------------

    def logcat(self, *runs: Run) -> None:
        """Set the runs for successive ``adb logcat`` invocations."""
        self._config["logcat"] = list(runs)
        self._write_config()

    def pidof(self, package: str, *runs: Run) -> None:
        """Set the runs for successive ``adb shell pidof <package>`` calls."""
        self._config.setdefault("pidof", {})[package] = list(runs)
        self._write_config()

    def dumpsys(self, package: str, *runs: Run) -> None:
        """Set the runs for successive ``adb shell dumpsys package <package>`` calls.

        This is the uid lookup of a live stream (design 5.8, A.8); script its
        answers with `dumpsys_package`. Each package counts its own
        invocations: ``dumpsys(p, run(out(dumpsys_package(p))), run(out(
        dumpsys_package(p, 10123))))`` answers "not installed", then the uid
        of an install.
        """
        self._config.setdefault("dumpsys", {})[package] = list(runs)
        self._write_config()

    def date(self, *runs: Run) -> None:
        """Set the runs for successive ``adb shell date ...`` calls (the device clock).

        Unscripted, the fake prints the host's epoch seconds, so a stream's
        clock read before each connection (design 3.2) succeeds. Script a
        value with ``run(out(device_date(seconds)))``, a failure with
        ``run(err("adb: device offline\\n"), exit_code=1)``.
        """
        self._config["date"] = list(runs)
        self._write_config()

    def get_state(self, *runs: Run) -> None:
        """Set the runs for successive ``adb get-state`` calls.

        A stream asks it after ``connect_timeout`` without output (design
        3.2): ``run(out("device\\n"))`` is a quiet device that is there,
        ``not_found(serial)`` one that is not.
        """
        self._config["get-state"] = list(runs)
        self._write_config()

    def device_absent(self, serial: str) -> None:
        """Behave as adb does for ``serial`` when no such device is attached.

        ``logcat`` waits without output (`waiting_for_device`), ``get-state``
        says ``not found``, and ``shell date`` fails (FINDINGS.md, check 4).
        """
        self.logcat(waiting_for_device())
        self.get_state(not_found(serial))
        self.date(run(err(f"adb: device '{serial}' not found\n"), exit_code=1))

    def command(self, name: str, *runs: Run) -> None:
        """Set the runs for another adb command (``devices``, ``connect``...)."""
        self._config[name] = list(runs)
        self._write_config()

    def _write_config(self) -> None:
        tmp = self.state / "config.json.tmp"
        tmp.write_text(json.dumps(self._config), encoding="utf-8")
        # Windows refuses the replace while a child is reading the file.
        deadline = time.monotonic() + _RETRY_TIMEOUT
        while True:
            try:
                tmp.replace(self.state / "config.json")
            except PermissionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(_POLL)
            else:
                return

    # -- observation and synchronization ----------------------------------

    def calls(self, command: str | None = None) -> list[dict[str, Any]]:
        """Invocations that started running, in spawn order.

        Each record is ``{"command", "key", "index", "args", "pid", "seq"}``,
        written by the child before its first step; optionally only those of
        ``command``.
        """
        records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.state / "calls").glob("*.json")
        ]
        records.sort(key=operator.itemgetter("seq"))
        if command is None:
            return records
        return [r for r in records if r["command"] == command]

    def release(self, name: str) -> None:
        """Unblock a fake adb waiting on ``wait(name)``."""
        (self.state / "signals" / name).touch()

    def wait_for(self, name: str, timeout: float = 10.0) -> None:
        """Block until a fake adb ran ``touch(name)``."""
        target = self.state / "signals" / name
        deadline = time.monotonic() + timeout
        while not target.exists():
            if time.monotonic() > deadline:
                msg = f"fake adb never signalled {name!r}"
                raise TimeoutError(msg)
            time.sleep(_POLL)

    async def async_wait_for(self, name: str, timeout: float = 10.0) -> None:
        """Async variant of ``wait_for``."""
        target = self.state / "signals" / name
        deadline = time.monotonic() + timeout
        while not target.exists():
            if time.monotonic() > deadline:
                msg = f"fake adb never signalled {name!r}"
                raise TimeoutError(msg)
            await asyncio.sleep(_POLL)

    # -- child registry ---------------------------------------------------

    def children(self, command: str | None = None) -> list[SpawnedChild]:
        """Every fake adb child spawned so far (optionally of ``command``)."""
        with self._lock:
            spawned = list(self._children)
        return [c for c in spawned if command is None or c.command == command]

    def live_children(self, command: str | None = None) -> list[SpawnedChild]:
        """The children (optionally of ``command``) that have not exited yet."""
        return [c for c in self.children(command) if c.alive()]

    def survivors(
        self, timeout: float = 5.0, command: str | None = None
    ) -> list[SpawnedChild]:
        """Wait up to ``timeout`` for the children to exit; return those left.

        ``[]`` means every child (optionally of ``command``) has exited.
        """
        with contextlib.suppress(TimeoutError):
            wait_until(lambda: not self.live_children(command), timeout=timeout)
        return self.live_children(command)

    async def async_survivors(
        self, timeout: float = 5.0, command: str | None = None
    ) -> list[SpawnedChild]:
        """Async variant of ``survivors``; lets the event loop run meanwhile."""
        with contextlib.suppress(TimeoutError):
            await async_wait_until(
                lambda: not self.live_children(command), timeout=timeout
            )
        return self.live_children(command)

    # -- spawn gate -------------------------------------------------------

    def hold_spawn(
        self, command: str | None = None, *, timeout: float = _SPAWN_GATE_TIMEOUT
    ) -> SpawnGate:
        """Hold the next fake adb spawns (of ``command``) until released.

        Only one gate is active; arm a new one after releasing the previous.
        ``timeout`` bounds how long a spawn stays held when the test never
        releases; that is reported as a failure at teardown.
        """
        if self._gate is not None and not self._gate.released:
            msg = "release the previous spawn gate before arming another"
            raise RuntimeError(msg)
        self._gate = SpawnGate(self, command, timeout)
        return self._gate

    # -- spawn interception (used by the fixture's wrappers) ---------------

    def _prepare(self, args: Sequence[Any]) -> tuple[list[Any], _PendingSpawn | None]:
        """Rewrite ``[fake.path, *adb_args]`` into a python invocation.

        Any other argv is returned unchanged with no pending spawn.
        """
        argv = list(args)
        if not argv or os.fspath(argv[0]) != self.path:
            return argv, None
        adb_args = tuple(os.fspath(a) for a in argv[1:])
        command, key, _rest = split_command(list(adb_args))
        with self._lock:
            if self._closed:
                raise FileNotFoundError(
                    errno.ENOENT, "the fake_adb fixture is closed", self.path
                )
            seq = self._seq
            self._seq += 1
            index = self._counters.get(key, 0)
            self._counters[key] = index + 1
        rewritten = [
            _FAKE_PYTHON,
            "-I",
            str(_FAKE_ADB_SCRIPT),
            "--fake-state",
            str(self.state),
            "--fake-index",
            str(index),
            "--fake-seq",
            str(seq),
            "--fake-parent",
            str(os.getpid()),
            *argv[1:],
        ]
        return rewritten, _PendingSpawn(command, key, index, seq, adb_args)

    def _register(
        self,
        pending: _PendingSpawn,
        popen: subprocess.Popen[Any],
        process: asyncio.subprocess.Process | None = None,
    ) -> SpawnedChild:
        child = SpawnedChild(
            pid=popen.pid,
            command=pending.command,
            key=pending.key,
            index=pending.index,
            args=pending.args,
            is_async=process is not None,
            popen=popen,
            process=process,
        )
        with self._lock:
            self._children.append(child)
            late = self._closed
        if late:
            # The spawn passed _prepare before close() and registered after
            # close() had finished: nobody else will reap this child.
            with contextlib.suppress(OSError):
                popen.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                popen.wait(timeout=_REAP_TIMEOUT)
        return child

    def _hold(self, child: SpawnedChild) -> None:
        gate = self._gate
        if gate is not None:
            gate._hold(child)

    async def _async_hold(self, child: SpawnedChild) -> None:
        gate = self._gate
        if gate is not None:
            await gate._async_hold(child)

    def close(self) -> None:
        """Release the gate, then kill and reap every live child (bounded).

        The fixture finalizer calls this; it is idempotent. Later spawns of
        ``fake.path`` fail with FileNotFoundError, as a missing adb would.
        Raises AssertionError for harness problems (a gate that timed out, a
        child that survived kill()), not for children the library leaked.

        Known limit: a live asyncio child the library leaked is reaped here
        with ``Popen.wait()``, which takes the exit status away from asyncio's
        child watcher (its ``Process.wait()`` then reports 255). This only
        happens on the leak path, where the test has already failed or the
        library is about to be fixed.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._gate is not None:
            self._gate.release()
        problems = list(self._errors)
        checked = 0
        # The registry can still grow: a spawn in flight when the fixture
        # closed registers its child only once its Popen() returns.
        while checked < len(self.children()):
            child = self.children()[checked]
            checked += 1
            if not child.alive():
                continue
            with contextlib.suppress(OSError):  # it exited meanwhile
                child.popen.kill()
            try:
                child.popen.wait(timeout=_REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                problems.append(f"{child!r} survived kill() for {_REAP_TIMEOUT} s")
        if problems:
            raise AssertionError("fake adb harness: " + "; ".join(problems))


def _popen_of(process: asyncio.subprocess.Process) -> subprocess.Popen[Any]:
    """The ``subprocess.Popen`` behind an asyncio child.

    asyncio publishes it as the transport's ``"subprocess"`` extra, but
    ``Process`` keeps its transport private.
    """
    transport = getattr(process, "_transport", None)
    popen = None if transport is None else transport.get_extra_info("subprocess")
    if not isinstance(popen, _REAL_POPEN):
        msg = f"cannot reach the Popen of asyncio child {process.pid}"
        raise TypeError(msg)
    return popen


def wait_until(
    predicate: Callable[[], object], timeout: float = 5.0, interval: float = _POLL
) -> None:
    """Poll ``predicate`` until it is truthy; raise TimeoutError otherwise."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            msg = "condition not met before the timeout"
            raise TimeoutError(msg)
        time.sleep(interval)


async def async_wait_until(
    predicate: Callable[[], object], timeout: float = 5.0, interval: float = _POLL
) -> None:
    """Async variant of ``wait_until``."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            msg = "condition not met before the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(interval)


# -- hang watchdog -----------------------------------------------------------

_watchdog_fd = pytest.StashKey[int]()
_watchdog_limit = pytest.StashKey[float]()


def _parse_watchdog_limit() -> float:
    raw_limit = os.environ.get(WATCHDOG_ENV, "").strip()
    if not raw_limit:
        return WATCHDOG_DEFAULT_LIMIT
    try:
        return float(raw_limit)
    except ValueError:
        msg = f"{WATCHDOG_ENV} must be a number of seconds, got {raw_limit!r}"
        raise pytest.UsageError(msg) from None


def pytest_configure(config: pytest.Config) -> None:
    """Read the watchdog limit and keep a copy of the real stderr for it.

    Output capture is suspended while plugins configure, so fd 2 is the real
    stderr here; during a test it is a capture file that exit=True discards.
    """
    config.stash[_watchdog_limit] = _parse_watchdog_limit()
    with contextlib.suppress(AttributeError, OSError, ValueError):
        config.stash[_watchdog_fd] = os.dup(sys.stderr.fileno())


def pytest_unconfigure(config: pytest.Config) -> None:
    """Close the stderr copy taken in ``pytest_configure``."""
    fd = config.stash.get(_watchdog_fd, None)
    if fd is not None:
        faulthandler.cancel_dump_traceback_later()
        del config.stash[_watchdog_fd]
        os.close(fd)


@pytest.fixture(autouse=True)
def _hang_watchdog(request: pytest.FixtureRequest) -> Iterator[None]:
    """Dump all tracebacks and exit if this test runs past the watchdog limit.

    Autouse fixtures are set up first and torn down last, so the limit also
    covers the other fixtures' setup and teardown (the fake_adb finalizer).
    """
    limit = request.config.stash.get(_watchdog_limit, WATCHDOG_DEFAULT_LIMIT)
    fd = request.config.stash.get(_watchdog_fd, None)
    if limit <= 0 or fd is None:
        yield
        return
    faulthandler.dump_traceback_later(limit, exit=True, file=fd)
    yield
    faulthandler.cancel_dump_traceback_later()


# -- fake adb fixture --------------------------------------------------------


def install_fake_adb(state: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAdb:
    """Create a ``FakeAdb`` in ``state`` and route every spawn of its path to it.

    The patches go through ``monkeypatch`` and end when it is undone; call
    ``FakeAdb.close()`` before that. The ``fake_adb`` fixture is the usual
    caller; code outside pytest (``bench/stream_bench.py``) passes a
    ``pytest.MonkeyPatch.context()``.
    """
    fake = FakeAdb(state)

    real_popen = subprocess.Popen

    # Transparent pass-through wrappers: they forward whatever the library
    # passes, so their parameters are deliberately untyped (ANN401).
    class _RewritingPopen(real_popen):
        def __init__(self, args: Any, *a: Any, **kw: Any) -> None:  # noqa: ANN401
            pending = None
            if isinstance(args, (list, tuple)):
                args, pending = fake._prepare(args)
            super().__init__(args, *a, **kw)
            if pending is not None:
                fake._hold(fake._register(pending, self))

    real_exec = asyncio.create_subprocess_exec

    async def _rewriting_exec(program: Any, *args: Any, **kw: Any) -> Any:  # noqa: ANN401
        argv, pending = fake._prepare([program, *args])
        process = await real_exec(*argv, **kw)
        if pending is not None:
            child = fake._register(pending, _popen_of(process), process)
            try:
                await fake._async_hold(child)
            except asyncio.CancelledError:
                # Cancelled while held: like asyncio's own cancelled spawn,
                # end the child nobody will receive (FakeAdb.close() reaps it).
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                raise
        return process

    monkeypatch.setattr(subprocess, "Popen", _RewritingPopen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _rewriting_exec)
    for target in RESOLVE_ADB_TARGETS:
        monkeypatch.setattr(target, lambda: fake.path)
    return fake


@pytest.fixture
def fake_adb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeAdb]:
    """A real-subprocess fake adb; see the module docstring for the API."""
    fake = install_fake_adb(tmp_path / "fake-adb", monkeypatch)
    yield fake
    fake.close()
