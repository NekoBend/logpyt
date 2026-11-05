"""adb helpers: find adb, list, connect and wait for devices, run one-shot commands.

The public surface is ``__all__`` (design section 2.10): `resolve_adb`,
`list_devices`, `wait_for_device`, `adb_connect`, their async twins and
`DeviceInfo`. Every helper takes ``adb_path`` (None resolves it with
`resolve_adb` at the call) and raises:

- `AdbError` when adb exits with a failure status or prints a failure;
- the builtin `TimeoutError` when ``timeout`` expires, after the adb process
  was killed and reaped;
- `FileNotFoundError` when ``adb_path`` is None and no adb is found;
- `ValueError` or `TypeError` for an invalid argument, before anything runs.

`DeviceKind`, the type of ``device_type`` and `DeviceInfo.kind`, can be imported
from here but is not in ``__all__``, like the stream's `GapReason`: whether such
type aliases are exported is left to the 3.0.0 documentation PR.

The private names are logpyt's own adb invocation policy (decision D-14), used
by the stream drivers and the core:

- `_spawn_adb` / `_async_spawn_adb` and the one-shot runners `_run_adb` /
  `_async_run_adb` built on them (design section 5.11): an argv list, never a
  shell; stdin from the null device; the host environment without
  ``ANDROID_LOG_TAGS`` (`_child_env`); on POSIX a new session and on Windows
  ``CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP``, so a terminal Ctrl+C does
  not reach adb; output decoded as UTF-8 with replacement; the child killed
  and reaped on timeout, on cancellation and on any other exception. adb's exit
  and EOF on its pipes are separate events: a process adb started that keeps
  the pipes open after adb exited delays the result by at most
  ``_REAP_TIMEOUT``.
- argv builders and output parsers for the stream's one-shot commands (design
  sections 5.6 and 5.8): the package-to-uid lookup (``dumpsys package``),
  ``pidof``, ``get-state``, the device clock (``date +%s``) and the start and
  flush markers (``log -t logpyt``). A builder validates every value that
  reaches the device shell; a parser reads an `_AdbResult` and raises `AdbError`
  for output it does not recognize. The output shapes come from the device
  recordings in ``tests/fixtures/device`` (API 24 and 36).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from logpyt.exceptions import AdbError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

__all__ = [
    "DeviceInfo",
    "adb_connect",
    "async_adb_connect",
    "async_wait_for_device",
    "list_devices",
    "resolve_adb",
    "wait_for_device",
]

logger = logging.getLogger(__name__)

# On Windows a child of a GUI, pythonw or frozen app opens a console window unless
# CREATE_NO_WINDOW is set, and CREATE_NEW_PROCESS_GROUP keeps a terminal Ctrl+C
# away from adb. Neither flag exists on POSIX, where the child gets a new session.
# _CREATE_NO_WINDOW alone is what the 2.x AsyncLogStream passes until PR-13.
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_CREATION_FLAGS = (
    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    if sys.platform == "win32"
    else 0
)

# Host variables an adb child must not inherit. adb hands ANDROID_LOG_TAGS to the
# device, whose logcat filters by it when the command line has no filterspec
# (tests/fixtures/device/FINDINGS.md, check 12), which would hide lines and markers.
_DROPPED_ENV = frozenset({"ANDROID_LOG_TAGS"})

# A killed adb exits at once, and an adb that exited closed its end of the pipes.
# This bounds the wait for its exit and for EOF on its pipes after it exited or
# was killed, because a descendant (an adb server it started) could keep the
# pipes open for as long as it runs.
_REAP_TIMEOUT = 5.0

# Ctrl+C raises KeyboardInterrupt only once a wait returns to the interpreter, and
# on Windows CPython 3.12 and 3.13 it does not end the join of communicate()'s
# reader threads, so the sync runner waits in slices of at most this many seconds
# (design section 9.2).
_WAIT_SLICE = 0.25

# The async runner tells adb's exit by polling returncode (A-14, design 5.4): on
# CPython 3.12 and 3.14 a Process.wait() begun before the exit is woken only once
# the pipes close (asyncio/base_subprocess.py), which a descendant can delay.
_EXIT_POLL = 0.05

# The size of one read from an async pipe (as the readers of design 5.4).
_READ_CHUNK = 65536

_SDK_VARIABLES = ("ANDROID_HOME", "ANDROID_SDK_ROOT")

# Design 2.7 (F5-3): a package name, and one with an optional ":process" suffix.
# ASCII only and matched whole, so no character the device shell interprets can
# pass.
_PLAIN_NAME = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*"
_PLAIN_PACKAGE_NAME = re.compile(_PLAIN_NAME)
_PACKAGE_NAME = re.compile(_PLAIN_NAME + r"(?::[A-Za-z0-9_.]+)?")

# The tag of the start and flush markers (design section 5.6); logpyt.config
# reserves it in logcat_args.
_MARKER_TAG = "logpyt"
# A marker is one token the device shell leaves alone; the leading character is
# not "-", which `log` would read as an option.
_MARKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")

type _MarkerKind = Literal["start", "flush"]
type DeviceKind = Literal["usb", "emulator", "tcp", "unknown"]
_DEVICE_KINDS = ("usb", "emulator", "tcp", "unknown")

_DEVICES_HEADER = "List of devices attached"
# adb prints this in place of an empty serial.
_NO_SERIAL = "(no serial number)"
# The properties of `adb devices -l`. The state can be several words ("no
# permissions (...); see [...]"), so it ends where the first property begins.
_DEVICE_PROPERTY = re.compile(r"(?:^|\s)(usb|product|model|device|transport_id):")
_EMULATOR_SERIAL = re.compile(r"emulator-[0-9]+")
_TCP_SERIAL = re.compile(r"(?:\[[^\]\s]+\]|[^\s:\[\]]+):[0-9]{1,5}")
_MDNS_SERIAL = re.compile(r"\S+\._adb(?:-tls-connect)?\._tcp\.?")
_PLAIN_SERIAL = re.compile(r"[A-Za-z0-9._-]+")

# adb prints its server start on stderr with this prefix (FINDINGS.md, check 9).
_SERVER_START_PREFIX = "* daemon "

# F9-2: adb connect succeeded only when a line starts like this.
_CONNECTED = re.compile(r"^(?:already )?connected to ", re.MULTILINE)

# `dumpsys package <pkg>`: a record starts with "  Package [<name>] (<hash>):", and
# its uid is the first indented "userId=" (API 24) or "appId=" (API 36) line
# (FINDINGS.md, check 3).
_DUMPSYS_RECORD = re.compile(r"(?P<indent> *)Package \[(?P<name>[^\]]*)\]")
_DUMPSYS_UID = re.compile(r" +(?:userId|appId)=(?P<uid>[0-9]+)\b")

# `adb get-state` errors. Recorded on API 24 and 36: "error: device offline" and
# "error: device '<serial>' not found" (FINDINGS.md, check 4). From adb's
# transport code, recalled: unauthorized, still authorizing or connecting, and
# "no devices/emulators found".
_STATE_ERROR = re.compile(
    r"^error: device (offline|unauthorized|still authorizing|still connecting)\b",
    re.MULTILINE,
)
_STATE_NAMES = {
    "offline": "offline",
    "unauthorized": "unauthorized",
    "still authorizing": "authorizing",
    "still connecting": "connecting",
}
_ABSENT_ERROR = re.compile(
    r"^error: (?:device '.*' not found|device not found|no devices/emulators found)$",
    re.MULTILINE,
)


# -- results -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AdbResult:
    """What one finished adb command returned.

    Attributes:
        argv: The command line that ran, the adb path first.
        returncode: The exit status.
        stdout: Standard output, decoded as UTF-8 with replacement characters.
        stderr: Standard error, decoded the same way.
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceInfo:
    """One device of ``adb devices -l``.

    Attributes:
        serial: The serial adb knows the device by (``-s`` takes it).
        state: The connection state as adb prints it, possibly several words:
            ``device`` when the device is ready, else ``offline``,
            ``unauthorized``, ``recovery``, ``no permissions (...)`` and so on.
        kind: ``emulator`` for a serial ``emulator-<port>``; ``tcp`` for
            ``<host>:<port>`` or an mDNS service name (wireless debugging);
            ``usb`` for a device with a USB path, or with another plain
            serial, because adb on Windows prints no USB path; else
            ``unknown``.
        product: The ``product:`` property, None when adb printed none.
        model: The ``model:`` property (adb replaces non-alphanumerics by
            ``_``), None when adb printed none.
        device: The ``device:`` property, None when adb printed none.
        transport_id: adb's transport id (``adb -t`` takes it), None when adb
            printed none.
        usb: The USB path of the ``usb:`` property (adb on Linux and macOS),
            None otherwise.
    """

    serial: str
    state: str
    kind: DeviceKind
    product: str | None = None
    model: str | None = None
    device: str | None = None
    transport_id: int | None = None
    usb: str | None = None


# -- resolving adb ---------------------------------------------------------------


def resolve_adb() -> str:
    """Return the absolute path of the adb executable.

    The search, first match wins: each directory on ``PATH`` in order, then
    ``$ANDROID_HOME/platform-tools``, then ``$ANDROID_SDK_ROOT/platform-tools``.
    The file is ``adb`` (``adb.exe`` on Windows; on WSL ``adb``, then
    ``adb.exe``), and it must be an executable file. The working directory is
    searched only when ``PATH`` names it (``.`` or its path): unlike a Windows
    command search, an ``adb.exe`` that sits in the working directory is never
    picked by itself (F9-3). The rule holds on every platform, so unlike
    `shutil.which` (the 2.x search) an empty ``PATH`` entry never means the
    working directory, and an unset ``PATH`` searches no default directories
    (POSIX ``CS_PATH``), only the SDK ones. A relative ``PATH`` entry yields an
    absolute path, which stays valid after a ``chdir``. Nothing is cached, so a
    change of ``PATH`` applies to the next call.

    Raises:
        FileNotFoundError: No adb executable was found.
    """
    names = _adb_names()
    for name in names:
        for directory in _path_directories():
            if (found := _executable(directory / name)) is not None:
                return found
    for directory in _sdk_directories():
        for name in names:
            if (found := _executable(directory / name)) is not None:
                return found
    msg = (
        f"no adb executable ({' or '.join(names)}) on PATH or in "
        f"{' or '.join(f'${var}/platform-tools' for var in _SDK_VARIABLES)}; "
        "install the Android SDK platform-tools or pass adb_path="
    )
    raise FileNotFoundError(msg)


def _adb_names() -> tuple[str, ...]:
    """The file names adb has on this platform, in search order."""
    if sys.platform == "win32":
        return ("adb.exe",)
    if _is_wsl():
        # A WSL distribution can drive the Windows adb (and its server).
        return ("adb", "adb.exe")
    return ("adb",)


def _is_wsl() -> bool:
    """Whether this Linux runs under the Windows Subsystem for Linux."""
    if sys.platform != "linux":
        return False
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "microsoft" in version.lower()


def _path_directories() -> Iterator[Path]:
    """The directories named on ``PATH``, in order; empty entries are skipped."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        # Windows allows a quoted entry ("C:\Program Files\x").
        directory = entry.strip().strip('"') if sys.platform == "win32" else entry
        if directory:
            yield Path(directory)


def _sdk_directories() -> list[Path]:
    """``platform-tools`` of each SDK root variable that is set, in order."""
    return [
        Path(root) / "platform-tools"
        for var in _SDK_VARIABLES
        if (root := os.environ.get(var))
    ]


def _executable(candidate: Path) -> str | None:
    """Return the absolute path of ``candidate`` if it is an executable file."""
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.absolute())
    except (OSError, ValueError):  # unreadable directory, NUL in PATH
        return None
    return None


# -- spawning and the one-shot runners ---------------------------------------------


def _child_env() -> dict[str, str]:
    """Return the environment of an adb child: the host's without ``ANDROID_LOG_TAGS``.

    ``ANDROID_SERIAL`` stays, so a stream or helper without a serial uses
    adb's own choice (``ANDROID_SERIAL``, then the only device).
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _DROPPED_ENV
    }


def _spawn_adb(argv: Sequence[str]) -> subprocess.Popen[bytes]:
    """Start an adb child under logpyt's process policy (module docstring).

    stdin is the null device; stdout and stderr are unbuffered binary pipes.

    Raises:
        OSError: The process could not be started.
    """
    args = list(argv)
    if sys.platform == "win32":
        return subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=_child_env(),
            creationflags=_CREATION_FLAGS,
        )
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=_child_env(),
        start_new_session=True,
    )


async def _async_spawn_adb(argv: Sequence[str]) -> asyncio.subprocess.Process:
    """Start an adb child for asyncio under logpyt's process policy.

    stdin is the null device; stdout and stderr are pipes. Cancelled during the
    spawn, asyncio kills the new child itself.

    Raises:
        OSError: The process could not be started.
    """
    args = list(argv)
    if sys.platform == "win32":
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
            creationflags=_CREATION_FLAGS,
        )
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_child_env(),
        start_new_session=True,
    )


def _run_adb(
    argv: Sequence[str],
    *,
    timeout: float | None,
    spawn: Callable[[Sequence[str]], subprocess.Popen[bytes]] | None = None,
) -> _AdbResult:
    """Run one adb command to its end and return what it printed.

    The exit status is returned, not checked. The wait runs in slices of
    ``_WAIT_SLICE``, so Ctrl+C ends it on every platform. When ``timeout``
    (seconds; None waits without a limit) expires while adb runs, or anything
    interrupts the wait (Ctrl+C included), the child is killed and reaped
    before the exception propagates.

    ``spawn`` starts the child in place of `_spawn_adb`, with the same
    arguments and errors. The stream driver passes one that also tracks the
    child in the same step, so the end of its run and interpreter exit can
    kill it early; a child killed that way ends the wait like any other exit.

    Once adb has exited, its pipes get ``_REAP_TIMEOUT`` more to reach EOF. If
    a process adb started still holds them then, a WARNING is logged and the
    result carries what was read until then: on POSIX all that adb wrote,
    because adb's own writes ended at its exit.

    Raises:
        AdbError: adb could not be started (``returncode`` None; the
            `OSError` is the cause). On Windows also when a process adb
            started still held the pipes ``_REAP_TIMEOUT`` after adb exited:
            communicate() hands over nothing before EOF there (``returncode``
            is adb's exit status).
        TimeoutError: ``timeout`` expired while adb ran; the child was killed
            and reaped.
    """
    args = tuple(argv)
    started = time.monotonic()
    try:
        proc = (_spawn_adb if spawn is None else spawn)(args)
    except OSError as exc:
        raise _spawn_failure(args, exc) from exc
    deadline = None if timeout is None else started + timeout
    try:
        output = _communicate(proc, deadline)
    except BaseException:
        _kill_and_reap(proc)
        raise
    if output is None:
        _kill_and_reap(proc)
        raise TimeoutError(_timeout_message(args, timeout))
    stdout, stderr, at_eof = output
    returncode = proc.returncode  # collected by communicate() or poll()
    if not at_eof:
        _log_unfinished(proc.pid, returncode, killed=False)
        if sys.platform == "win32":
            raise _unread_output(args, returncode)
    return _finished(args, returncode, stdout, stderr, started)


def _communicate(
    proc: subprocess.Popen[bytes], deadline: float | None
) -> tuple[bytes, bytes, bool] | None:
    """Read adb's output to EOF and wait for its exit, in slices of ``_WAIT_SLICE``.

    adb's exit and EOF on its pipes are separate events: once adb has exited,
    `_drain` bounds the wait for EOF, whatever ``deadline`` is.

    Args:
        proc: The adb child.
        deadline: The `time.monotonic` time to give up at; None for no limit.

    Returns:
        stdout, stderr and whether both pipes reached EOF; None when
        ``deadline`` passed while adb was running.
    """
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_next_slice(deadline))
        except subprocess.TimeoutExpired:
            if proc.poll() is not None:
                return _drain(proc)
            if deadline is not None and time.monotonic() >= deadline:
                return None
        else:
            return stdout, stderr, True


def _drain(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes, bool]:
    """Wait at most ``_REAP_TIMEOUT`` for EOF on adb's pipes and for its exit.

    Called once adb has exited or was killed. Returns stdout, stderr and whether
    both pipes reached EOF and adb exited. When not, a process adb started
    holds the pipes: on POSIX they are closed here and the output is what was
    read until then. On Windows communicate()'s reader threads stay blocked
    until that process lets go, hand over nothing before, and keep two threads
    and the two pipe handles until then: Python has no way to end a blocking
    read there. adb's own server holds none of the client's pipes on Linux
    (FINDINGS.md, check 9); on Windows that is not recorded (open item 5).
    """
    deadline = time.monotonic() + _REAP_TIMEOUT
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=_next_slice(deadline))
        except subprocess.TimeoutExpired as exc:
            if time.monotonic() < deadline:
                continue
            if sys.platform != "win32":
                # communicate() reads them in this thread, so no reader is left.
                _close_pipes(proc)
            return exc.output or b"", exc.stderr or b"", False
        return stdout, stderr, True


def _next_slice(deadline: float | None) -> float:
    """``_WAIT_SLICE``, or the time left until ``deadline`` when that is less."""
    if deadline is None:
        return _WAIT_SLICE
    return max(0.0, min(_WAIT_SLICE, deadline - time.monotonic()))


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` unless it has exited, then reap it; bounded by _REAP_TIMEOUT.

    Interrupted itself (a second Ctrl+C), it still closes the pipes on POSIX and
    reaps the killed child, bounded the same way, before the interrupt
    propagates.
    """
    with contextlib.suppress(OSError):
        proc.kill()
    try:
        at_eof = _drain(proc)[2]
    except BaseException:
        if sys.platform != "win32":
            _close_pipes(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_REAP_TIMEOUT)
        raise
    if not at_eof:
        _log_unfinished(proc.pid, proc.poll(), killed=True)


# The reads of an async adb child's stdout and stderr (`_read_into`), one task
# each, so that a failed read is told apart from one still waiting for EOF.
type _Readers = tuple[asyncio.Task[None], asyncio.Task[None]]


async def _async_run_adb(argv: Sequence[str], *, timeout: float | None) -> _AdbResult:
    """Async twin of `_run_adb`: also kills and reaps the child when cancelled.

    adb's exit is told by polling ``returncode`` (A-14), and once adb has
    exited its pipes get ``_REAP_TIMEOUT`` more to reach EOF, as in `_run_adb`;
    on every platform the result then carries what was read until then. A
    cancellation that arrives while a killed child is being reaped waits for
    that bounded reap before it propagates.

    Raises:
        AdbError: adb could not be started (``returncode`` None; the
            `OSError` is the cause).
        TimeoutError: ``timeout`` expired while adb ran; the child was killed
            and reaped.
        asyncio.CancelledError: The task was cancelled; the child was killed
            and reaped first.
        OSError: Reading a pipe failed; the child was killed and reaped and
            its pipes closed first.
    """
    args = tuple(argv)
    started = time.monotonic()
    try:
        proc = await _async_spawn_adb(args)
    except OSError as exc:
        raise _spawn_failure(args, exc) from exc
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    readers = (
        asyncio.ensure_future(_read_into(proc.stdout, stdout)),
        asyncio.ensure_future(_read_into(proc.stderr, stderr)),
    )
    try:
        async with asyncio.timeout(timeout):
            returncode = await _async_until_exit(proc, readers)
    except TimeoutError:
        await _async_kill_and_reap(proc, readers)
        raise TimeoutError(_timeout_message(args, timeout)) from None
    except BaseException:
        await _async_kill_and_reap(proc, readers)
        raise
    settled = await _async_settle(proc, readers)
    if not settled:
        _log_unfinished(proc.pid, returncode, killed=False)
    if (failure := _failed_read(readers)) is not None:
        raise failure
    return _finished(args, returncode, b"".join(stdout), b"".join(stderr), started)


async def _read_into(stream: asyncio.StreamReader | None, chunks: list[bytes]) -> None:
    """Append what ``stream`` delivers to ``chunks``, up to EOF."""
    if stream is None:
        return
    while chunk := await stream.read(_READ_CHUNK):
        chunks.append(chunk)


def _failed_read(readers: _Readers) -> BaseException | None:
    """The exception of the first reader that failed, or None.

    Retrieves every finished reader's exception, so that asyncio reports none
    of them as never retrieved.
    """
    failures = [
        exc
        for task in readers
        if task.done() and not task.cancelled() and (exc := task.exception())
    ]
    return failures[0] if failures else None


async def _async_until_exit(proc: asyncio.subprocess.Process, readers: _Readers) -> int:
    """Return adb's exit status once it has exited; ``readers`` run meanwhile.

    Raises:
        OSError: A read failed while adb ran.
    """
    while (returncode := proc.returncode) is None:
        if (failure := _failed_read(readers)) is not None:
            raise failure
        pending = [task for task in readers if not task.done()]
        if not pending:
            # Both pipes are at EOF, so wait() returns at the exit on every
            # CPython version.
            return await proc.wait()
        await asyncio.wait(pending, timeout=_EXIT_POLL)
    return returncode


async def _async_settle(proc: asyncio.subprocess.Process, readers: _Readers) -> bool:
    """Wait at most ``_REAP_TIMEOUT`` for adb's exit and for both readers to end.

    Called once adb has exited or was killed. A failed read counts as ended.
    Returns whether all of it came. When not, or when the wait is
    interrupted, `_async_abandon` stops the readers and closes the pipes, so
    the output is what was read until then.
    """
    deadline = time.monotonic() + _REAP_TIMEOUT
    try:
        while proc.returncode is None or not all(task.done() for task in readers):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await _async_abandon(proc, readers)
                return False
            pending = [task for task in readers if not task.done()]
            if pending:
                await asyncio.wait(pending, timeout=min(_EXIT_POLL, remaining))
            else:
                await asyncio.sleep(min(_EXIT_POLL, remaining))
    except BaseException:
        _abandon_now(proc, readers)
        raise
    return True


async def _async_abandon(proc: asyncio.subprocess.Process, readers: _Readers) -> None:
    """`_abandon_now`, then wait for the cancelled readers to end."""
    _abandon_now(proc, readers)
    await asyncio.wait(readers)
    _failed_read(readers)


def _abandon_now(proc: asyncio.subprocess.Process, readers: _Readers) -> None:
    """Cancel the readers still running and close adb's pipes.

    A process adb started may hold the pipes for as long as it runs. A
    cancelled reader ends at the next loop iteration.
    """
    for task in readers:
        if not task.done():
            task.cancel()
    _failed_read(readers)
    # asyncio has no public way to close a child's pipes. Process keeps its
    # transport as _transport (asyncio/subprocess.py, CPython 3.12 to 3.14), and
    # closing it closes the pipe transports. Left open, they are closed by the
    # garbage collector with a ResourceWarning, and on 3.12, once the loop has
    # closed, with an "Event loop is closed" error.
    transport = getattr(proc, "_transport", None)
    if isinstance(transport, asyncio.SubprocessTransport):
        transport.close()


async def _async_kill_and_reap(
    proc: asyncio.subprocess.Process, readers: _Readers
) -> None:
    """Async twin of `_kill_and_reap`.

    The bounded reap runs in a task of its own. A cancellation of the caller
    that arrives meanwhile waits for it, then propagates, so the child is
    reaped and its pipes closed before the caller's task ends.
    """
    if proc.returncode is None:
        with contextlib.suppress(OSError):  # ProcessLookupError: it has just exited
            proc.kill()
    reap = asyncio.ensure_future(_async_settle(proc, readers))
    interrupted: asyncio.CancelledError | None = None
    while not reap.done():
        try:
            await asyncio.shield(reap)
        except asyncio.CancelledError as exc:
            interrupted = exc
    if reap.cancelled():  # the loop is shutting down and cancelled it too
        _abandon_now(proc, readers)
    elif not reap.result():
        _log_unfinished(proc.pid, proc.returncode, killed=True)
    # Retrieve a failed read's exception: the one propagating already wins.
    _failed_read(readers)
    if interrupted is not None:
        raise interrupted


def _log_unfinished(pid: int, returncode: int | None, *, killed: bool) -> None:
    """Log that the wait for an adb process ended before EOF on its pipes."""
    if returncode is None:
        logger.warning(
            "adb process %d did not exit within %s s of being killed; "
            "not waiting any longer",
            pid,
            _REAP_TIMEOUT,
        )
        return
    logger.warning(
        "adb process %d %s, but its output pipes were still open %s s later "
        "(a process it started holds them); not waiting for them any longer",
        pid,
        "was killed" if killed else f"exited with status {returncode}",
        _REAP_TIMEOUT,
    )


def _finished(
    argv: tuple[str, ...], returncode: int, stdout: bytes, stderr: bytes, started: float
) -> _AdbResult:
    logger.debug(
        "%s exited with status %d after %.3f s",
        _describe(argv),
        returncode,
        time.monotonic() - started,
    )
    return _AdbResult(
        argv=argv,
        returncode=returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _spawn_failure(argv: tuple[str, ...], exc: OSError) -> AdbError:
    return AdbError(
        f"could not start {_describe(argv)}: {exc}", argv=argv, returncode=None
    )


def _unread_output(argv: tuple[str, ...], returncode: int) -> AdbError:
    return AdbError(
        f"{_describe(argv)} exited with status {returncode}, but a process it "
        "started kept its output pipes open, so its output could not be read",
        argv=argv,
        returncode=returncode,
    )


def _timeout_message(argv: tuple[str, ...], timeout: float | None) -> str:
    return (
        f"{_describe(argv)} did not finish within {timeout} s; "
        "the adb process was killed"
    )


def _describe(argv: Sequence[str]) -> str:
    """``adb <arguments>``, without the adb path."""
    return " ".join(["adb", *argv[1:]])


def _lines(text: str) -> list[str]:
    """``text`` split at LF, each line without a trailing CR."""
    return [line.removesuffix("\r") for line in text.split("\n")]


def _failure(result: _AdbResult, what: str, *, prefer_stdout: bool = False) -> AdbError:
    """An `AdbError` for ``result``, quoting the first line that explains it.

    adb prints its own errors on stderr, but some commands (``connect``, a
    device-side command) print theirs on stdout. Lines adb prints while it
    starts its server (``* daemon ...``) explain nothing and are skipped.
    """
    texts = (
        (result.stdout, result.stderr)
        if prefer_stdout
        else (result.stderr, result.stdout)
    )
    detail = next(
        (
            line.strip()
            for text in texts
            for line in _lines(text)
            if line.strip() and not line.startswith("* ")
        ),
        "",
    )
    message = f"{what} (exit status {result.returncode})"
    if detail:
        message = f"{message}: {detail}"
    return AdbError(
        message,
        argv=result.argv,
        returncode=result.returncode,
        stderr=result.stderr,
    )


def _adb_complaints(result: _AdbResult) -> list[str]:
    """The non-blank stderr lines of ``result`` other than adb's server start.

    A client that finds no server starts one and says so on stderr, with exit
    status 0 (``* daemon not running; starting now at tcp:5037``, ``* daemon
    started successfully``: FINDINGS.md, check 9).
    """
    return [
        line
        for line in _lines(result.stderr)
        if line.strip() and not line.startswith(_SERVER_START_PREFIX)
    ]


def _require_success(result: _AdbResult, what: str) -> None:
    if result.returncode != 0:
        raise _failure(result, what)


# -- argument checks -------------------------------------------------------------


def _check_text(value: object, name: str) -> str:
    """Return ``value`` if it is a non-empty str without NUL."""
    if not isinstance(value, str):
        msg = f"{name} must be a str, not {type(value).__name__}"
        raise TypeError(msg)
    if not value or "\0" in value:
        msg = f"{name} must be a non-empty string without NUL, got {value!r}"
        raise ValueError(msg)
    return value


def _check_serial(serial: str | None) -> None:
    if serial is not None:
        _check_text(serial, "serial")


def _check_timeout(timeout: float | None) -> None:
    """Accept None or a finite number of seconds above zero."""
    if timeout is None:
        return
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        msg = (
            f"timeout must be a number of seconds or None, not {type(timeout).__name__}"
        )
        raise TypeError(msg)
    if not math.isfinite(timeout) or timeout <= 0:
        msg = (
            "timeout must be a finite number of seconds above 0 or None, "
            f"got {timeout!r}"
        )
        raise ValueError(msg)


def _check_address(address: str) -> None:
    _check_text(address, "address")
    if address.startswith("-") or address != address.strip():
        msg = f"address must be <host>[:<port>], got {address!r}"
        raise ValueError(msg)


def _check_device_type(device_type: DeviceKind | None) -> None:
    if device_type is not None and device_type not in _DEVICE_KINDS:
        msg = (
            f"device_type must be one of {', '.join(map(repr, _DEVICE_KINDS))} or "
            f"None, not {device_type!r}; to keep only ready devices, filter on "
            "DeviceInfo.state == 'device'"
        )
        raise ValueError(msg)


def _resolved(adb_path: str | None) -> str:
    """``adb_path``, or `resolve_adb` (looked up at the call) when it is None."""
    if adb_path is None:
        return resolve_adb()
    return _check_text(adb_path, "adb_path")


# -- devices, connect and wait -----------------------------------------------------


def list_devices(
    device_type: DeviceKind | None = None,
    *,
    adb_path: str | None = None,
    timeout: float | None = 10.0,
) -> list[DeviceInfo]:
    """List the devices adb knows (``adb devices -l``), in adb's order.

    Args:
        device_type: Keep only devices of this `DeviceInfo.kind`; None keeps
            all.
        adb_path: The adb executable; None resolves it.
        timeout: Seconds to wait for adb; None waits without a limit.

    Raises:
        AdbError: adb failed or printed no device list.
        TimeoutError: ``timeout`` expired; adb was killed and reaped.
        FileNotFoundError: ``adb_path`` is None and no adb was found.
        ValueError: An unknown ``device_type``, or an invalid ``timeout``.
    """
    _check_device_type(device_type)
    _check_timeout(timeout)
    argv = _adb_argv(_resolved(adb_path), None, "devices", "-l")
    devices = _parse_devices(_run_adb(argv, timeout=timeout))
    if device_type is None:
        return devices
    return [info for info in devices if info.kind == device_type]


def wait_for_device(
    serial: str | None = None,
    *,
    adb_path: str | None = None,
    timeout: float | None = None,
) -> None:
    """Wait until the device is online (``adb wait-for-device``).

    Args:
        serial: The device; None leaves the choice to adb (``ANDROID_SERIAL``,
            then the only device).
        adb_path: The adb executable; None resolves it.
        timeout: Seconds to wait; None waits without a limit.

    Raises:
        AdbError: adb failed.
        TimeoutError: ``timeout`` expired; adb was killed and reaped.
        FileNotFoundError: ``adb_path`` is None and no adb was found.
        ValueError: An empty ``serial`` or an invalid ``timeout``.
    """
    _check_serial(serial)
    _check_timeout(timeout)
    argv = _adb_argv(_resolved(adb_path), serial, "wait-for-device")
    _require_success(_run_adb(argv, timeout=timeout), "adb wait-for-device failed")


async def async_wait_for_device(
    serial: str | None = None,
    *,
    adb_path: str | None = None,
    timeout: float | None = None,
) -> None:
    """Async twin of `wait_for_device`; cancelling it kills and reaps adb."""
    _check_serial(serial)
    _check_timeout(timeout)
    argv = _adb_argv(_resolved(adb_path), serial, "wait-for-device")
    result = await _async_run_adb(argv, timeout=timeout)
    _require_success(result, "adb wait-for-device failed")


def adb_connect(
    address: str,
    *,
    adb_path: str | None = None,
    timeout: float | None = None,
) -> None:
    """Connect adb to a device over TCP/IP (``adb connect <address>``).

    Success is a line starting with ``connected to`` or ``already connected
    to`` and exit status 0 (F9-2); a host name that contains "failed" or
    "unable" is no failure.

    Args:
        address: ``<host>[:<port>]``.
        adb_path: The adb executable; None resolves it.
        timeout: Seconds to wait for adb; None waits without a limit.

    Raises:
        AdbError: adb did not report a connection.
        TimeoutError: ``timeout`` expired; adb was killed and reaped.
        FileNotFoundError: ``adb_path`` is None and no adb was found.
        ValueError: An invalid ``address`` or ``timeout``.
    """
    _check_address(address)
    _check_timeout(timeout)
    argv = _adb_argv(_resolved(adb_path), None, "connect", address)
    _parse_connect(_run_adb(argv, timeout=timeout))


async def async_adb_connect(
    address: str,
    *,
    adb_path: str | None = None,
    timeout: float | None = None,
) -> None:
    """Async twin of `adb_connect`; cancelling it kills and reaps adb."""
    _check_address(address)
    _check_timeout(timeout)
    argv = _adb_argv(_resolved(adb_path), None, "connect", address)
    _parse_connect(await _async_run_adb(argv, timeout=timeout))


def _parse_devices(result: _AdbResult) -> list[DeviceInfo]:
    """Read the devices of an ``adb devices -l`` result (F9-1).

    Everything before the ``List of devices attached`` header is ignored (adb
    can print server start messages first), a state of several words is kept
    whole, and a property value ends where the next property begins.

    Raises:
        AdbError: adb failed, or printed no header.
    """
    _require_success(result, "adb devices failed")
    lines = _lines(result.stdout)
    header = next(
        (i for i, line in enumerate(lines) if line.strip() == _DEVICES_HEADER), None
    )
    if header is None:
        raise _failure(
            result,
            f"adb devices printed no {_DEVICES_HEADER!r} line",
            prefer_stdout=True,
        )
    devices = (_parse_device_line(line) for line in lines[header + 1 :])
    return [info for info in devices if info is not None]


def _parse_device_line(line: str) -> DeviceInfo | None:
    """One ``<serial> <state> [<key>:<value>...]`` line; None for a non-device line."""
    text = line.strip()
    if not text or text.startswith("*"):
        return None
    if text.startswith(_NO_SERIAL):
        serial, rest = _NO_SERIAL, text.removeprefix(_NO_SERIAL)
    else:
        parts = text.split(None, 1)
        if len(parts) < 2:
            return None
        serial, rest = parts
    found = list(_DEVICE_PROPERTY.finditer(rest))
    state = rest[: found[0].start() if found else len(rest)].strip()
    if not state:
        return None
    values: dict[str, str] = {}
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(rest)
        values[match[1]] = rest[match.end() : end].strip()
    transport = values.get("transport_id", "")
    usb = values.get("usb") or None
    return DeviceInfo(
        serial=serial,
        state=state,
        kind=_device_kind(serial, usb),
        product=values.get("product") or None,
        model=values.get("model") or None,
        device=values.get("device") or None,
        transport_id=int(transport)
        if transport.isascii() and transport.isdigit()
        else None,
        usb=usb,
    )


def _device_kind(serial: str, usb: str | None) -> DeviceKind:
    if _EMULATOR_SERIAL.fullmatch(serial):
        return "emulator"
    if _TCP_SERIAL.fullmatch(serial) or _MDNS_SERIAL.fullmatch(serial):
        return "tcp"
    if usb is not None or _PLAIN_SERIAL.fullmatch(serial):
        return "usb"
    return "unknown"


def _parse_connect(result: _AdbResult) -> None:
    """Check an ``adb connect`` result (F9-2).

    Raises:
        AdbError: The exit status is not 0, or no line starts with
            ``connected to`` or ``already connected to``.
    """
    if result.returncode != 0 or not _CONNECTED.search(result.stdout):
        raise _failure(result, "adb connect failed", prefer_stdout=True)


# -- argv builders and parsers for the stream's one-shot commands ---------------------


def _adb_argv(adb_path: str, serial: str | None, *args: str) -> tuple[str, ...]:
    """Return ``(adb_path, "-s", serial, *args)``, without ``-s`` when serial is None.

    Raises:
        ValueError: ``serial`` is empty or holds a NUL.
        TypeError: ``serial`` is not a str.
    """
    if serial is None:
        return (adb_path, *args)
    return (adb_path, "-s", _check_text(serial, "serial"), *args)


def _check_package_name(name: str) -> str:
    """Return ``name`` if it is a package name, optionally with ``:<process>``.

    The grammar of design 2.7 (F5-3): dot-separated segments of ASCII letters,
    digits and ``_``, each starting with a letter. ``adb shell`` hands its words
    to the device shell, which parses them (FINDINGS.md, R3), so nothing else
    may reach it.

    Raises:
        ValueError: ``name`` does not follow the grammar.
        TypeError: ``name`` is not a str.
    """
    if not isinstance(name, str):
        msg = f"a package name must be a str, not {type(name).__name__}"
        raise TypeError(msg)
    if not _PACKAGE_NAME.fullmatch(name):
        msg = (
            f"{name!r} is not a package name: dot-separated segments of ASCII "
            "letters, digits and '_', each starting with a letter, optionally "
            "followed by ':<process>'"
        )
        raise ValueError(msg)
    return name


def _check_package(name: str, *, hint: str = "") -> str:
    """Return ``name`` if it is a package name, without ``:<process>``.

    Package attribution maps a line's uid to its app, and an app has one
    package name, so `logpyt.filters.Package`, the names a stream attributes,
    ``read_file``'s ``uid_packages`` values and the uid lookup take package
    names only: the grammar of `_check_package_name` without the process part.

    Args:
        name: The name.
        hint: Added to the message for a process name: what the caller offers
            for one instead.

    Raises:
        ValueError: ``name`` is not a package name, or names a process.
        TypeError: ``name`` is not a str.
    """
    if not isinstance(name, str):
        msg = f"a package name must be a str, not {type(name).__name__}"
        raise TypeError(msg)
    if _PLAIN_PACKAGE_NAME.fullmatch(name):
        return name
    if _PACKAGE_NAME.fullmatch(name):
        package = name.partition(":")[0]
        msg = (
            f"{name!r} names a process, not a package: a uid maps to an app, "
            f"so the package name is {package!r}{hint}"
        )
        raise ValueError(msg)
    msg = (
        f"{name!r} is not a package name: dot-separated segments of ASCII "
        "letters, digits and '_', each starting with a letter"
    )
    raise ValueError(msg)


def _pidof_argv(adb_path: str, serial: str | None, name: str) -> tuple[str, ...]:
    """``adb shell pidof <name>``: the pids of a package or process name.

    Raises:
        ValueError: ``name`` fails `_check_package_name`, or ``serial`` is empty.
    """
    return _adb_argv(adb_path, serial, "shell", "pidof", _check_package_name(name))


def _parse_pidof(result: _AdbResult) -> tuple[int, ...]:
    """Read the pids of a ``pidof`` result; no process is ``()``.

    ``pidof`` prints the pids separated by spaces; with no process it exits 1
    and prints nothing (FINDINGS.md, R3).

    Raises:
        AdbError: adb or the device failed, or printed something other than
            pids.
    """
    tokens = result.stdout.split()
    if result.returncode in {0, 1} and not tokens and not _adb_complaints(result):
        return ()
    if result.returncode != 0:
        raise _failure(result, "adb shell pidof failed")
    if not all(token.isascii() and token.isdigit() for token in tokens):
        raise _failure(
            result,
            "adb shell pidof printed something other than pids",
            prefer_stdout=True,
        )
    return tuple(int(token) for token in tokens)


def _package_uid_argv(
    adb_path: str, serial: str | None, package: str
) -> tuple[str, ...]:
    """``adb shell dumpsys package <package>``: the lookup of a package's uid.

    The command that works on API 24 and 36 (FINDINGS.md, check 3, A.8);
    ``pm list packages -U`` fails with status 0 on API 24 and is a substring
    filter on 36. It takes the package name, not a process name.

    Raises:
        ValueError: ``package`` fails `_check_package`, or ``serial`` is empty.
        TypeError: ``package`` is not a str.
    """
    return _adb_argv(
        adb_path, serial, "shell", "dumpsys", "package", _check_package(package)
    )


def _parse_package_uid(result: _AdbResult, package: str) -> int | None:
    """Read the uid of ``package`` from a ``dumpsys package`` result.

    The uid is the first ``userId=`` (API 24) or ``appId=`` (API 36) line of
    the record whose header names exactly ``package``; on a device with
    several users it is the app id (uid modulo 100000).

    Returns:
        The uid, or None when no record of ``package`` holds one (the package is
        not installed).

    Raises:
        AdbError: adb or dumpsys failed.
    """
    _require_success(result, f"adb shell dumpsys package {package} failed")
    lines = _lines(result.stdout)
    for index, line in enumerate(lines):
        header = _DUMPSYS_RECORD.match(line)
        if header is None or header["name"] != package:
            continue
        indent = len(header["indent"])
        for record_line in lines[index + 1 :]:
            stripped = record_line.lstrip(" ")
            if stripped and len(record_line) - len(stripped) <= indent:
                break  # the record ended without a uid
            if uid := _DUMPSYS_UID.match(record_line):
                return int(uid["uid"])
    return None


def _get_state_argv(adb_path: str, serial: str | None) -> tuple[str, ...]:
    """``adb get-state``: whether the device is there (design section 5.6).

    Raises:
        ValueError: ``serial`` is empty.
    """
    return _adb_argv(adb_path, serial, "get-state")


def _parse_get_state(result: _AdbResult) -> str | None:
    """Read the device state of an ``adb get-state`` result.

    Returns:
        The state: ``device`` when the device is online; ``offline``,
        ``unauthorized``, ``authorizing``, ``connecting``, ``recovery``,
        ``bootloader`` and so on otherwise. None when adb knows no such device
        (``error: device '<serial>' not found``).

    Raises:
        AdbError: adb failed in another way (no server, several devices and
            no serial, ...).
    """
    if result.returncode == 0:
        words = result.stdout.split()
        if len(words) == 1:
            return words[0]
        raise _failure(result, "adb get-state printed no state", prefer_stdout=True)
    text = "\n".join([*_lines(result.stderr), *_lines(result.stdout)])
    if state := _STATE_ERROR.search(text):
        return _STATE_NAMES[state[1]]
    if _ABSENT_ERROR.search(text):
        return None
    raise _failure(result, "adb get-state failed")


def _device_clock_argv(adb_path: str, serial: str | None) -> tuple[str, ...]:
    """``adb shell date +%s``: the device clock in whole epoch seconds.

    Whole seconds, because ``%N`` is not supported on API 24 (FINDINGS.md,
    check 10).

    Raises:
        ValueError: ``serial`` is empty.
    """
    return _adb_argv(adb_path, serial, "shell", "date", "+%s")


def _parse_device_clock(result: _AdbResult) -> int:
    """Read the epoch seconds of a ``date +%s`` result.

    Raises:
        AdbError: adb or the device failed (``adb: device offline``), or the
            output is not a whole number of seconds.
    """
    _require_success(result, "adb shell date failed")
    text = result.stdout.strip()
    if not (text.isascii() and text.isdigit()):
        raise _failure(
            result, "adb shell date +%s printed no epoch seconds", prefer_stdout=True
        )
    return int(text)


def _new_marker(kind: _MarkerKind) -> str:
    """Return a fresh marker token, ``start-<32 hex>`` or ``flush-<32 hex>``.

    Raises:
        ValueError: ``kind`` is neither ``start`` nor ``flush``.
    """
    if kind not in {"start", "flush"}:
        msg = f"a marker kind is 'start' or 'flush', not {kind!r}"
        raise ValueError(msg)
    return f"{kind}-{uuid.uuid4().hex}"


def _log_marker_argv(adb_path: str, serial: str | None, marker: str) -> tuple[str, ...]:
    """``adb shell log -t logpyt <marker>``: write a marker to the main buffer.

    ``log -t`` writes to the main buffer on API 24 and 36 (FINDINGS.md,
    check 11).

    Raises:
        ValueError: ``marker`` is not one token of ASCII letters, digits and
            ``-`` that starts with a letter or digit, or ``serial`` is empty.
    """
    if not isinstance(marker, str) or not _MARKER.fullmatch(marker):
        msg = (
            "a marker is one token of ASCII letters, digits and '-' that starts "
            f"with a letter or digit, got {marker!r}"
        )
        raise ValueError(msg)
    return _adb_argv(adb_path, serial, "shell", "log", "-t", _MARKER_TAG, marker)


def _parse_log_marker(result: _AdbResult) -> None:
    """Check a ``log -t`` result: status 0 and no output.

    adb's lines about starting its server are no output of ``log``.

    Raises:
        AdbError: adb or ``log`` failed, or printed anything else.
    """
    if result.returncode != 0 or result.stdout.strip() or _adb_complaints(result):
        raise _failure(result, "adb shell log failed")


def _is_marker(tag: str, message: str, marker: str) -> bool:
    """Whether a log line with ``tag`` and ``message`` is the echo of ``marker``.

    Trailing spaces are ignored: on API 24 ``log`` appends one to the message
    (FINDINGS.md, check 11; design A.7).
    """
    return tag.strip() == _MARKER_TAG and message.rstrip(" ") == marker
