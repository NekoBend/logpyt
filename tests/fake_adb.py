"""A scriptable stand-in for the ``adb`` executable, run as a real subprocess.

The ``fake_adb`` pytest fixture (``tests/conftest.py``) rewrites any spawn of
its sentinel adb path into::

    python -I fake_adb.py --fake-state <dir> --fake-index <n> --fake-seq <s> \\
        --fake-parent <pid> <adb args...>

so the library's real ``subprocess`` / ``asyncio`` code paths, pipes, exit
codes, and kill semantics are exercised on every platform CI runs on. The
parent side (spawn gate, child registry, hang watchdog) is documented in
``tests/conftest.py``.

``<dir>/config.json`` describes the behavior per adb command. Each command
maps to a list of runs; the n-th invocation of that command uses the n-th run,
and the last run repeats once the list is exhausted::

    {
      "logcat": [RUN, ...],
      "pidof": {"com.example": [RUN, ...]},
      "dumpsys": {"com.example": [RUN, ...]},   # shell dumpsys package <pkg>
      "date": [RUN, ...],            # shell date ... (the device clock)
      "log": [RUN, ...],             # shell log ... (the start and flush markers)
      "get-state": [RUN, ...],       # get-state (the stream's state check)
      "devices": [RUN, ...],
      "default": [RUN, ...],         # any other command
      "device": DEVICE               # a simulated device log (below)
    }

    RUN  = {"steps": [STEP, ...], "exit": 0, "hang": false, "ignore_sigterm": false}
    STEP = {"out": "text"}           # write UTF-8 text to stdout as-is
         | {"err": "text"}           # write UTF-8 text to stderr as-is
         | {"out_b64": "..."}        # write raw bytes to stdout
         | {"lines": {"count": N, "template": "... {i} ...\\n", "stream": "out"}}
         | {"sleep": seconds}
         | {"wait": "name"}          # block until <dir>/signals/<name> exists
         | {"touch": "name"}         # create <dir>/signals/<name>
         | {"clock": {}}             # print the device clock: date +%s, now
         | {"hold_stdout": {"until": "name", "then": "text"}}
         | {"stream": {"until": "name", "lines": N, "ready": "name"}}

``hold_stdout`` starts a grandchild that holds this process's stdout: it gets
the handle explicitly (``Popen(stdout=<this stdout>)``, so Windows passes it
too), waits for the signal ``name`` (bounded like ``wait``), writes ``then``
to that stdout and exits. The fake adb goes on at once, so once it has exited
its stdout pipe stays open until the grandchild lets go, as when adb starts a
process that inherits its output (design 5.10, 9.2).

Device (design 5.6, PR-14). ``DEVICE = {"ring": N, "overrun": "eof" | "skip",
"log_space": false}`` turns on a simulated device whose state lives in
``<dir>/device/``:

- the log, ``log.txt``: one entry per line, ``<index> <epoch microseconds>
  <the line as logcat prints it>``, in arrival order (``index`` counts from
  0). Appends take the lock file ``log.lock`` (created exclusively; one older
  than ``_STALE_LOCK`` seconds is broken), so the test process and ``shell
  log`` children can write at once. The ring is the last ``ring`` entries:
  older ones are overwritten. A reader seeks to the ring from the end of the
  file, so a spawn costs O(ring) however long the log grew (the soak, design
  9.1). ``expire_device_entries`` rewrites the log as logd's chatty pruning
  does (an ``expire N lines`` notice in place of the entries); no stream may
  be reading meanwhile;
- the clock, ``clock.json``: ``{"base": <epoch microseconds>, "at": <host
  epoch seconds>}``, the device time ``base`` at host time ``at``, running
  with the host clock (the test steps it back or forward); absent, the device
  clock is the host clock. ``shell date`` answers it in whole seconds
  (``date +%s``) unless ``date`` runs are scripted; a scripted run's
  ``clock`` step prints it as it reads when the step runs (after a ``wait``,
  so a test can change the clock while a clock read waits);
- ``shell log -t <tag> <message>`` (``-p <priority>`` too) appends an entry
  at the device clock, as uid ``shell`` with this process's pid, and exits 0
  with no output, unless ``log`` runs are scripted. ``log_space`` appends a
  space to the message, as API 24 does (FINDINGS.md, check 11);
- a ``stream`` step prints the ring as ``adb logcat`` does: from the first
  entry (in arrival order) at or above an epoch ``-T <sec>.<usec>``
  (inclusive; later entries follow in arrival order, also when their time is
  lower, as after a clock step), the last N entries for ``-T N`` (digits
  only: a count), or the whole ring without ``-T``; then every new entry as it
  is appended. It touches ``ready`` once the entries in the ring at its start
  are written, and ends when the signal ``until`` exists or after ``lines``
  entries, and the run goes on with its next step. When the ring has
  overwritten the entry a stream would write next while it still had entries
  to write (the reader fell behind: its writes block on a full pipe; a
  reader that had written everything keeps up with any append, however
  large), it behaves as API 24 and 36 did
  (FINDINGS.md, check 5; design A.6): with ``overrun`` ``"eof"`` (the default)
  it writes ``logcat: Unexpected EOF!`` to stderr and exits 1 at once, after
  the unbroken prefix it wrote; with ``"skip"`` it goes on at the oldest entry
  the ring holds, silently, as nougat's logd source predicts for a slow
  reader. A ``logcat`` with no scripted runs streams until it is killed.

After the steps the process exits with ``exit``, or, when ``hang`` is true,
sleeps until it is killed. A command with no configured run exits 1 silently;
so does ``pidof`` or ``dumpsys`` of a package with no runs (``default`` does
not apply to them). ``shell date`` is the exception: with no ``date`` runs it
prints the host's epoch seconds (``date +%s`` of a device whose clock is the
host's) and exits 0, because every stream connection reads the device clock
first (design 3.2); ``default`` does not apply to it either. A hanging
``logcat`` run whose only output is ``- waiting for device -`` on stderr is
what adb does for a serial that is not attached (FINDINGS.md, check 4).

The parent assigns ``--fake-index`` (the per-command invocation number that
selects the run; ``pidof`` and ``dumpsys`` count per package, so a package's
lookups can change their answer, as a reinstall does) and ``--fake-seq`` (the global
spawn number) under its own lock, so this process takes no lock and keeps no
counter that a kill could leave half-written. ``--fake-parent`` is the
spawning process's pid. Before any step runs it records
itself as ``<dir>/calls/<seq>.json`` (written to a temporary name, then
renamed): ``{"command", "key", "index", "args", "pid", "seq"}``.

A child never outlives the test run by much: a ``wait`` step gives up after
``_WAIT_TIMEOUT`` (exit 98), and a hanging child exits once its parent is gone
(POSIX: its parent pid is no longer ``--fake-parent``, for example after the
hang watchdog ended pytest; exit 96) or after ``_HANG_LIMIT`` (exit 97), far
beyond any per-test limit.

Only the standard library is used: this file runs in ``python -I``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_WAIT_TIMEOUT = 30.0
_HANG_LIMIT = 600.0
_ORPHAN_POLL = 0.2
_RETRY_TIMEOUT = 5.0
_POLL = 0.005

_EXIT_USAGE = 2
_EXIT_OVERRUN = 1  # logcat's status when logd ended a reader behind its ring
_EXIT_ORPHANED = 96
_EXIT_HANG_LIMIT = 97
_EXIT_WAIT_TIMEOUT = 98
_EXIT_UNKNOWN_STEP = 99

_OPTIONS = ("--fake-state", "--fake-index", "--fake-seq", "--fake-parent")
_USAGE = (
    "usage: fake_adb.py --fake-state DIR --fake-index N --fake-seq N"
    " --fake-parent PID [adb args...]\n"
)

# adb global options that take a value and precede the command.
_GLOBAL_OPTS_WITH_VALUE = frozenset({"-s", "-t", "-H", "-P", "-L"})

# The simulated device (see the module docs).
_US = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DEFAULT_RING = 10_000
_STALE_LOCK = 5.0
_CHUNK = 256  # entries per write of a stream
_SEEK_BLOCK = 64 * 1024  # bytes per backward read when seeking to the ring
_EPOCH_T = re.compile(r"(\d+)\.(\d{1,6})")
# What API 36 printed (API 24: "read: unexpected EOF!"); FINDINGS.md check 5.
_OVERRUN_NOTICE = "logcat: Unexpected EOF!\n"


def split_command(args: list[str]) -> tuple[str, str, list[str]]:
    """Return (command, key, rest) for an adb argument vector.

    ``key`` selects the per-invocation counter: the command name,
    ``pidof:<package>`` for ``shell pidof <package>``,
    ``dumpsys:<package>`` for ``shell dumpsys package <package>`` (the uid
    lookup of design 5.8, A.8), ``date`` for ``shell date ...`` (the device
    clock of design 5.6), or ``log`` for ``shell log ...`` (the markers). The
    fixture calls this too, so parent and child always agree on the key.
    """
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GLOBAL_OPTS_WITH_VALUE else 1
    rest = args[i:]
    if not rest:
        return "", "", []
    command = rest[0]
    if command == "shell" and len(rest) >= 3 and rest[1] == "pidof":
        return "pidof", f"pidof:{rest[2]}", rest[1:]
    if command == "shell" and len(rest) == 4 and rest[1:3] == ["dumpsys", "package"]:
        return "dumpsys", f"dumpsys:{rest[3]}", rest[1:]
    if command == "shell" and len(rest) >= 2 and rest[1] == "date":
        return "date", "date", rest[1:]
    if command == "shell" and len(rest) >= 2 and rest[1] == "log":
        return "log", "log", rest[1:]
    return command, command, rest[1:]


def _parse_argv(argv: list[str]) -> tuple[Path, int, int, int, list[str]] | None:
    """Split ``argv`` into (state, index, seq, parent, adb args); None if invalid."""
    options: dict[str, str] = {}
    i = 1
    while i + 1 < len(argv) and argv[i] in _OPTIONS:
        options[argv[i]] = argv[i + 1]
        i += 2
    if set(options) != set(_OPTIONS):
        return None
    try:
        index = int(options["--fake-index"])
        seq = int(options["--fake-seq"])
        parent = int(options["--fake-parent"])
    except ValueError:
        return None
    return Path(options["--fake-state"]), index, seq, parent, argv[i:]


def _read_text_retrying(path: Path) -> str:
    """Read ``path``, retrying briefly on a Windows sharing violation.

    The test may be replacing the file (``config.json``) at that moment, which
    Windows reports as PermissionError; POSIX never does.
    """
    deadline = time.monotonic() + _RETRY_TIMEOUT
    while True:
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(_POLL)


def _record_call(
    state: Path, seq: int, command: str, key: str, index: int, args: list[str]
) -> None:
    """Publish this invocation as ``calls/<seq>.json``, atomically."""
    record = {
        "command": command,
        "key": key,
        "index": index,
        "args": args,
        "pid": os.getpid(),
        "seq": seq,
    }
    calls = state / "calls"
    tmp = calls / f"{seq:06d}.json.tmp"
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(calls / f"{seq:06d}.json")


def _select_run(
    state: Path, config: dict[str, Any], command: str, key: str, index: int
) -> dict[str, Any] | None:
    device = config.get("device")
    if command in {"pidof", "dumpsys"}:
        runs = config.get(command, {}).get(key.split(":", 1)[1])
    elif command == "date":
        clock = device_now(state) if device is not None else None
        runs = config.get("date") or [_host_clock_run(clock)]
    elif command == "log":
        runs = config.get("log")
        if not runs and device is not None:
            runs = [{"steps": [{"device_log": {}}], "exit": 0}]
        elif not runs:
            runs = config.get("shell", config.get("default"))
    elif command == "logcat" and device is not None and not config.get("logcat"):
        runs = [{"steps": [{"stream": {}}], "hang": True}]
    else:
        runs = config.get(command, config.get("default"))
    if not runs:
        return None
    return runs[min(index, len(runs) - 1)]


def _host_clock_run(device_us: int | None = None) -> dict[str, Any]:
    """``date +%s``: the unscripted answer (the device clock, else the host's)."""
    seconds = int(time.time()) if device_us is None else device_us // _US
    return {"steps": [{"out": f"{seconds}\n"}], "exit": 0}


# -- the simulated device ------------------------------------------------------------


def device_dir(state: Path) -> Path:
    """Where the simulated device keeps its log and clock."""
    return state / "device"


def device_now(state: Path) -> int:
    """The device clock in epoch microseconds (the host clock unless set)."""
    try:
        clock = json.loads(_read_text_retrying(device_dir(state) / "clock.json"))
    except FileNotFoundError:
        return time.time_ns() // 1000
    return int(clock["base"]) + round((time.time() - float(clock["at"])) * _US)


def set_device_clock(state: Path, micros: int) -> None:
    """Make the device clock read ``micros`` now; it runs on with the host clock."""
    folder = device_dir(state)
    folder.mkdir(exist_ok=True)
    tmp = folder / f"clock.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"base": micros, "at": time.time()}), encoding="utf-8")
    _replace_retrying(tmp, folder / "clock.json")


def device_stamp(micros: int) -> str:
    """``-v year -v usec -v zone`` time of epoch ``micros`` (UTC, as the emulators)."""
    moment = _EPOCH + timedelta(microseconds=micros)
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f +0000")


def device_line(
    micros: int,
    message: str,
    *,
    tag: str = "Tag",
    level: str = "I",
    uid: int | str = 10065,
    pid: int = 1234,
    tid: int | None = None,
) -> str:
    """One line of the live wire format (U-1), without its newline."""
    thread = pid if tid is None else tid
    stamp = device_stamp(micros)
    return f"{stamp} {uid!s:>5} {pid:5d} {thread:5d} {level} {tag}: {message}"


def append_device_log(state: Path, entries: list[tuple[int, str]]) -> None:
    """Append ``(epoch microseconds, line)`` entries to the device log, atomically.

    Each line is one log entry (no newline inside). The lock file serializes
    writers of all processes; a reader sees whole entries (it keeps a
    partial last line until its newline arrives). A lock older than
    ``_STALE_LOCK`` seconds is taken to be a dead writer's and broken.
    """
    folder = device_dir(state)
    folder.mkdir(exist_ok=True)
    path = folder / "log.txt"
    with _locked(folder):
        first = _next_index(path)
        data = "".join(
            f"{first + n} {micros} {text}\n" for n, (micros, text) in enumerate(entries)
        ).encode("utf-8")
        with path.open("ab") as log:
            log.write(data)


def expire_device_entries(
    state: Path, first: int, count: int, notice: Callable[[int], str]
) -> str:
    """Replace ``count`` entries from arrival index ``first`` with one notice.

    logd's chatty pruning (design 5.6): the entries leave the log and an
    ``expire N lines`` notice takes their place, at the timestamp of the
    newest of them (the replay rule's assumption, FINDINGS.md check 6).
    ``notice(micros)`` formats the notice line; it is returned. The later
    entries are renumbered. The log is rewritten (a new file replaces it),
    so no stream may be reading it.
    """
    folder = device_dir(state)
    path = folder / "log.txt"
    with _locked(folder):
        log = DeviceLog(state)
        try:
            records = log.poll()
        finally:
            log.close()
        chosen = records[first : first + count]
        if len(chosen) != count or count < 1:
            msg = f"the device log has no entries {first}..{first + count - 1}"
            raise IndexError(msg)
        stamp = max(micros for _, micros, _ in chosen)
        line = notice(stamp)
        kept = [(micros, text.decode()) for _, micros, text in records[:first]]
        kept.append((stamp, line))
        kept.extend((m, t.decode()) for _, m, t in records[first + count :])
        tmp = folder / f"log.{os.getpid()}.tmp"
        tmp.write_bytes(
            "".join(f"{n} {m} {t}\n" for n, (m, t) in enumerate(kept)).encode()
        )
        _replace_retrying(tmp, path)
    return line


@contextlib.contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """Hold the device log's lock file (see `append_device_log`)."""
    lock = folder / "log.lock"
    started = time.monotonic()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            # Windows reports a lock file that is being deleted as
            # PermissionError.
            if time.monotonic() - started > _STALE_LOCK:
                _unlink_retrying(lock)
                started = time.monotonic()
            time.sleep(0.001)
            continue
        break
    try:
        yield
    finally:
        os.close(fd)
        _unlink_retrying(lock)


def _tail_offset(file: BinaryIO, records: int) -> int:
    """The offset where the last ``records`` complete records of ``file`` start.

    Reads backwards from the end in blocks, so the cost does not grow with
    the file; bytes after the last newline (a record being written) are not
    a complete record. 0 when the file holds no more than ``records``.
    """
    file.seek(0, os.SEEK_END)
    pos = file.tell()
    need = records + 1  # the newline that ends the record before the tail
    while pos > 0:
        step = min(_SEEK_BLOCK, pos)
        pos -= step
        file.seek(pos)
        data = file.read(step)
        end = len(data)
        while need:
            found = data.rfind(b"\n", 0, end)
            if found < 0:
                break
            need -= 1
            end = found
        if not need:
            return pos + end + 1
    return 0


def _next_index(path: Path) -> int:
    """The index the next appended entry gets (call it holding the lock)."""
    try:
        file = path.open("rb")
    except FileNotFoundError:
        return 0
    with file:
        file.seek(_tail_offset(file, 1))
        last = file.readline()
    if not last.endswith(b"\n"):
        return 0  # an empty log
    return int(last.split(b" ", 1)[0]) + 1


def _unlink_retrying(path: Path) -> None:
    """Remove ``path``; retry while Windows reports another handle on it."""
    deadline = time.monotonic() + _RETRY_TIMEOUT
    while True:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(_POLL)
        else:
            return


class DeviceLog:
    """Reads the device log as it grows: entries ``(index, micros, line)``."""

    def __init__(self, state: Path) -> None:
        self._path = device_dir(state) / "log.txt"
        self._file: BinaryIO | None = None
        self._rest = b""
        self._tail: int | None = None
        self.total = 0  # the index after the last entry read

    def tail(self, records: int) -> None:
        """Start the next `poll` at the last ``records`` entries (the ring)."""
        self._tail = records

    def poll(self) -> list[tuple[int, int, bytes]]:
        """The entries appended since the last call."""
        if self._file is None:
            tail, self._tail = self._tail, None  # only where the reader starts
            try:
                self._file = self._path.open("rb")
            except FileNotFoundError:
                return []  # an empty log: later entries are read from the first
            if tail is not None:
                self._file.seek(_tail_offset(self._file, tail))
        data = self._rest + self._file.read()
        end = data.rfind(b"\n") + 1
        self._rest = data[end:]
        found: list[tuple[int, int, bytes]] = []
        for record in data[:end].splitlines():
            index, micros, text = record.split(b" ", 2)
            self.total = int(index) + 1
            found.append((self.total - 1, int(micros), text))
        return found

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def _tail_request(args: list[str]) -> tuple[str, int] | None:
    """``-T`` of a logcat command: ("count", n), ("time", micros) or None.

    Raises:
        ValueError: A ``-T`` value the fake does not read.
    """
    if "-T" not in args:
        return None
    index = args.index("-T") + 1
    value = args[index] if index < len(args) else ""
    if value.isdigit():
        return "count", int(value)  # digits only: a line count (n-logcat.cpp)
    found = _EPOCH_T.fullmatch(value)
    if found is None:
        msg = f"fake_adb: unsupported -T {value!r}"
        raise ValueError(msg)
    seconds, fraction = found.groups()
    return "time", int(seconds) * _US + int(fraction.ljust(6, "0"))


class _Stream:
    """A ``stream`` step: print the ring per ``-T``, then follow (module docs)."""

    def __init__(
        self, state: Path, spec: dict[str, Any], device: dict[str, Any], parent: int
    ) -> None:
        self.state = state
        self.parent = parent
        self.capacity = int(device.get("ring", _DEFAULT_RING))
        self.skip = device.get("overrun", "eof") == "skip"
        self.until = spec.get("until")
        self.limit = spec.get("lines")
        self.ready = spec.get("ready")
        self.log = DeviceLog(state)
        self.pending: deque[tuple[int, int, bytes]] = deque()
        self.written = 0

    def start(self, request: tuple[str, int] | None) -> None:
        """Queue the ring's entries from the one ``request`` names."""
        self.log.tail(self.capacity)
        ring = deque(self.log.poll(), maxlen=self.capacity)
        first = ring[0][0] if ring else 0
        if request is None:
            start = first
        elif request[0] == "count":
            start = max(self.log.total - request[1], first)
        else:
            # Inclusive; later entries follow in arrival order.
            start = next(
                (i for i, micros, _ in ring if micros >= request[1]), self.log.total
            )
        self.pending.extend(entry for entry in ring if entry[0] >= start)

    def run(self) -> None:
        initial = self.log.total
        signalled = self.ready is None
        while not self._ended():
            behind = bool(self.pending)  # entries left before this poll
            self.pending.extend(self.log.poll())
            if behind:
                self._check_overrun()
            if not signalled and (not self.pending or self.pending[0][0] >= initial):
                (self.state / "signals" / self.ready).touch()
                signalled = True
            if not self.pending:
                if _orphaned(self.parent):
                    sys.exit(_EXIT_ORPHANED)
                time.sleep(_POLL)
                continue
            self._write_chunk()
        self.log.close()

    def _ended(self) -> bool:
        if self.limit is not None and self.written >= self.limit:
            return True
        return self.until is not None and (self.state / "signals" / self.until).exists()

    def _check_overrun(self) -> None:
        """The ring overwrote the entry we would write next (design A.6).

        Only a reader that is behind is checked: one that had written every
        entry reads an append as it happens, as logd's caught-up readers do.
        """
        oldest = self.log.total - self.capacity
        pending = self.pending
        if not pending or pending[0][0] >= oldest:
            return
        if not self.skip:
            sys.stdout.buffer.flush()
            sys.stderr.write(_OVERRUN_NOTICE)
            sys.stderr.flush()
            sys.exit(_EXIT_OVERRUN)
        while pending and pending[0][0] < oldest:
            pending.popleft()

    def _write_chunk(self) -> None:
        count = min(len(self.pending), _CHUNK)
        if self.limit is not None:
            count = min(count, self.limit - self.written)
        chunk = [self.pending.popleft()[2] for _ in range(count)]
        _write(sys.stdout.buffer, b"".join(text + b"\n" for text in chunk))
        self.written += count


def _device_step(
    state: Path,
    step: dict[str, Any],
    parent: int,
    args: list[str],
    config: dict[str, Any],
) -> None:
    """A ``stream`` or ``device_log`` step (see the module docs)."""
    device = config.get("device")
    if device is None:
        sys.stderr.write("fake_adb: a device step needs a device\n")
        sys.exit(_EXIT_UNKNOWN_STEP)
    if "device_log" in step:
        _log_entry(state, args, device)
        return
    try:
        request = _tail_request(split_command(args)[2])
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.exit(_EXIT_USAGE)
    stream = _Stream(state, step["stream"], device, parent)
    stream.start(request)
    stream.run()


def _log_entry(state: Path, args: list[str], device: dict[str, Any]) -> None:
    """``shell log [-p <prio>] [-t <tag>] <message>``: an entry at the device clock."""
    words = split_command(args)[2][1:]  # after "log"
    tag, level = "log", "I"
    while len(words) >= 2 and words[0] in {"-t", "-p"}:
        if words[0] == "-t":
            tag = words[1]
        else:
            level = words[1][:1].upper()
        words = words[2:]
    message = " ".join(words) + (" " if device.get("log_space") else "")
    pid = os.getpid() % 100_000
    now = device_now(state)  # one reading: the stamp and the stored time agree
    text = device_line(now, message, tag=tag, level=level, uid="shell", pid=pid)
    append_device_log(state, [(now, text)])


def _replace_retrying(source: Path, target: Path) -> None:
    """``source.replace(target)``, retrying while Windows reports a reader."""
    deadline = time.monotonic() + _RETRY_TIMEOUT
    while True:
        try:
            source.replace(target)
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(_POLL)
        else:
            return


def _orphaned(parent: int) -> bool:
    """True once the spawning process ``parent`` is gone.

    POSIX re-parents an orphan, so ``getppid()`` changes. Windows keeps the
    original value, so there only the time limits end an orphan.
    """
    return sys.platform != "win32" and os.getppid() != parent


def _write(stream: BinaryIO, data: bytes) -> None:
    stream.write(data)
    stream.flush()


def _wait_for_signal(state: Path, name: str, parent: int) -> None:
    target = state / "signals" / name
    deadline = time.monotonic() + _WAIT_TIMEOUT
    while not target.exists():
        if _orphaned(parent):
            sys.exit(_EXIT_ORPHANED)
        if time.monotonic() > deadline:
            sys.stderr.write(f"fake_adb: timed out waiting for signal {name!r}\n")
            sys.exit(_EXIT_WAIT_TIMEOUT)
        time.sleep(_POLL)


# The grandchild of a ``hold_stdout`` step: argv is the signal path, the time
# limit in seconds and the text to write once the signal exists (or at the limit).
_HOLDER = """\
import os, sys, time
path, limit, text = sys.argv[1], float(sys.argv[2]), sys.argv[3]
deadline = time.monotonic() + limit
while not os.path.exists(path) and time.monotonic() < deadline:
    time.sleep(0.005)
sys.stdout.buffer.write(text.encode("utf-8"))
sys.stdout.buffer.flush()
"""


def _hold_stdout(state: Path, spec: dict[str, str]) -> None:
    """Start a grandchild that holds our stdout until a signal (see the module docs)."""
    sys.stdout.buffer.flush()
    signal_path = state / "signals" / spec["until"]
    subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            _HOLDER,
            str(signal_path),
            str(_WAIT_TIMEOUT),
            spec.get("then", ""),
        ],
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout.fileno(),
        stderr=subprocess.DEVNULL,
    )


def _write_lines(target: BinaryIO, spec: dict[str, Any]) -> None:
    """A ``lines`` step: ``template.format(i=i)`` in chunks of 256."""
    template: str = spec["template"]
    chunk: list[bytes] = []
    for i in range(spec["count"]):
        chunk.append(template.format(i=i).encode("utf-8"))
        if len(chunk) >= _CHUNK:
            target.write(b"".join(chunk))
            chunk.clear()
    target.write(b"".join(chunk))
    target.flush()


def _run_steps(
    state: Path,
    steps: list[dict[str, Any]],
    parent: int,
    args: list[str],
    config: dict[str, Any],
) -> None:
    out = sys.stdout.buffer
    err = sys.stderr.buffer
    for step in steps:
        if "stream" in step or "device_log" in step:
            _device_step(state, step, parent, args, config)
            continue
        if "out" in step:
            _write(out, step["out"].encode("utf-8"))
        elif "err" in step:
            _write(err, step["err"].encode("utf-8"))
        elif "out_b64" in step:
            _write(out, base64.b64decode(step["out_b64"]))
        elif "lines" in step:
            spec = step["lines"]
            _write_lines(err if spec.get("stream") == "err" else out, spec)
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "wait" in step:
            _wait_for_signal(state, step["wait"], parent)
        elif "touch" in step:
            (state / "signals" / step["touch"]).touch()
        elif "clock" in step:
            _write(out, f"{device_now(state) // _US}\n".encode())
        elif "hold_stdout" in step:
            _hold_stdout(state, step["hold_stdout"])
        else:
            sys.stderr.write(f"fake_adb: unknown step {step!r}\n")
            sys.exit(_EXIT_UNKNOWN_STEP)


def _hang(parent: int) -> int:
    """Sleep until killed, the parent is gone, or ``_HANG_LIMIT`` passes."""
    deadline = time.monotonic() + _HANG_LIMIT
    while time.monotonic() < deadline:
        if _orphaned(parent):
            return _EXIT_ORPHANED
        time.sleep(_ORPHAN_POLL)
    sys.stderr.write("fake_adb: hang limit reached\n")
    return _EXIT_HANG_LIMIT


def main() -> int:
    parsed = _parse_argv(sys.argv)
    if parsed is None:
        sys.stderr.write(_USAGE)
        return _EXIT_USAGE
    state, index, seq, parent, adb_args = parsed
    command, key, _rest = split_command(adb_args)
    _record_call(state, seq, command, key, index, adb_args)
    config = json.loads(_read_text_retrying(state / "config.json"))
    run = _select_run(state, config, command, key, index)
    if run is None:
        return 1
    if run.get("ignore_sigterm") and sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _run_steps(state, run.get("steps", []), parent, adb_args, config)
    if run.get("hang"):
        return _hang(parent)
    return int(run.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
