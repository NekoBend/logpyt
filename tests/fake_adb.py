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
      "get-state": [RUN, ...],       # get-state (the stream's state check)
      "devices": [RUN, ...],
      "default": [RUN, ...]          # any other command
    }

    RUN  = {"steps": [STEP, ...], "exit": 0, "hang": false, "ignore_sigterm": false}
    STEP = {"out": "text"}           # write UTF-8 text to stdout as-is
         | {"err": "text"}           # write UTF-8 text to stderr as-is
         | {"out_b64": "..."}        # write raw bytes to stdout
         | {"lines": {"count": N, "template": "... {i} ...\\n", "stream": "out"}}
         | {"sleep": seconds}
         | {"wait": "name"}          # block until <dir>/signals/<name> exists
         | {"touch": "name"}         # create <dir>/signals/<name>
         | {"hold_stdout": {"until": "name", "then": "text"}}

``hold_stdout`` starts a grandchild that holds this process's stdout: it gets
the handle explicitly (``Popen(stdout=<this stdout>)``, so Windows passes it
too), waits for the signal ``name`` (bounded like ``wait``), writes ``then``
to that stdout and exits. The fake adb goes on at once, so once it has exited
its stdout pipe stays open until the grandchild lets go, as when adb starts a
process that inherits its output (design 5.10, 9.2).

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
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

_WAIT_TIMEOUT = 30.0
_HANG_LIMIT = 600.0
_ORPHAN_POLL = 0.2
_RETRY_TIMEOUT = 5.0
_POLL = 0.005

_EXIT_USAGE = 2
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


def split_command(args: list[str]) -> tuple[str, str, list[str]]:
    """Return (command, key, rest) for an adb argument vector.

    ``key`` selects the per-invocation counter: the command name,
    ``pidof:<package>`` for ``shell pidof <package>``,
    ``dumpsys:<package>`` for ``shell dumpsys package <package>`` (the uid
    lookup of design 5.8, A.8), or ``date`` for ``shell date ...`` (the
    device clock of design 5.6). The fixture calls this too, so parent and
    child always agree on the key.
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
    config: dict[str, Any], command: str, key: str, index: int
) -> dict[str, Any] | None:
    if command in {"pidof", "dumpsys"}:
        runs = config.get(command, {}).get(key.split(":", 1)[1])
    elif command == "date":
        runs = config.get("date") or [_host_clock_run()]
    else:
        runs = config.get(command, config.get("default"))
    if not runs:
        return None
    return runs[min(index, len(runs) - 1)]


def _host_clock_run() -> dict[str, Any]:
    """``date +%s`` of a device whose clock is the host's: the unscripted answer."""
    return {"steps": [{"out": f"{int(time.time())}\n"}], "exit": 0}


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


def _run_steps(state: Path, steps: list[dict[str, Any]], parent: int) -> None:
    out = sys.stdout.buffer
    err = sys.stderr.buffer
    for step in steps:
        if "out" in step:
            _write(out, step["out"].encode("utf-8"))
        elif "err" in step:
            _write(err, step["err"].encode("utf-8"))
        elif "out_b64" in step:
            _write(out, base64.b64decode(step["out_b64"]))
        elif "lines" in step:
            spec = step["lines"]
            target = err if spec.get("stream") == "err" else out
            template: str = spec["template"]
            chunk: list[bytes] = []
            for i in range(spec["count"]):
                chunk.append(template.format(i=i).encode("utf-8"))
                if len(chunk) >= 256:
                    target.write(b"".join(chunk))
                    chunk.clear()
            target.write(b"".join(chunk))
            target.flush()
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "wait" in step:
            _wait_for_signal(state, step["wait"], parent)
        elif "touch" in step:
            (state / "signals" / step["touch"]).touch()
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
    run = _select_run(config, command, key, index)
    if run is None:
        return 1
    if run.get("ignore_sigterm") and sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _run_steps(state, run.get("steps", []), parent)
    if run.get("hang"):
        return _hang(parent)
    return int(run.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
