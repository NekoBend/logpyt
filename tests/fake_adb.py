"""A scriptable stand-in for the ``adb`` executable, run as a real subprocess.

The ``fake_adb`` pytest fixture (``tests/conftest.py``) rewrites any spawn of
its sentinel adb path into::

    python -I fake_adb.py --fake-state <dir> <adb args...>

so the library's real ``subprocess`` / ``asyncio`` code paths, pipes, exit
codes, and kill semantics are exercised on every platform CI runs on.

``<dir>/config.json`` describes the behavior per adb command. Each command
maps to a list of runs; the n-th invocation of that command uses the n-th run,
and the last run repeats once the list is exhausted::

    {
      "logcat": [RUN, ...],
      "pidof": {"com.example": [RUN, ...]},
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

After the steps the process exits with ``exit``, or, when ``hang`` is true,
sleeps until it is killed. A command with no configured run exits 1 silently.

Every invocation is appended to ``<dir>/calls.jsonl`` as
``{"command", "key", "index", "args", "pid"}`` before any step runs.

Only the standard library is used: this file runs in ``python -I``.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

_LOCK_TIMEOUT = 10.0
_WAIT_TIMEOUT = 30.0
_POLL = 0.005

# adb global options that take a value and precede the command.
_GLOBAL_OPTS_WITH_VALUE = frozenset({"-s", "-t", "-H", "-P", "-L"})


def _split_command(args: list[str]) -> tuple[str, str, list[str]]:
    """Return (command, key, rest) for an adb argument vector.

    ``key`` selects the per-invocation counter: the command name, or
    ``pidof:<package>`` for ``shell pidof <package>``.
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
    return command, command, rest[1:]


@contextlib.contextmanager
def _locked(state: Path):
    lock = state / "lock"
    deadline = time.monotonic() + _LOCK_TIMEOUT
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise
            time.sleep(_POLL)
    try:
        yield
    finally:
        lock.rmdir()


def _next_index(state: Path, key: str) -> int:
    counters_path = state / "counters.json"
    with _locked(state):
        counters: dict[str, int] = {}
        if counters_path.exists():
            counters = json.loads(counters_path.read_text(encoding="utf-8"))
        index = counters.get(key, 0)
        counters[key] = index + 1
        counters_path.write_text(json.dumps(counters), encoding="utf-8")
        with (state / "calls.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "command": key.split(":", 1)[0],
                        "key": key,
                        "index": index,
                        "args": sys.argv[3:],
                        "pid": os.getpid(),
                    }
                )
                + "\n"
            )
    return index


def _select_run(
    config: dict[str, Any], command: str, key: str, index: int
) -> dict[str, Any] | None:
    if command == "pidof":
        runs = config.get("pidof", {}).get(key.split(":", 1)[1])
    else:
        runs = config.get(command, config.get("default"))
    if not runs:
        return None
    return runs[min(index, len(runs) - 1)]


def _write(stream: BinaryIO, data: bytes) -> None:
    stream.write(data)
    stream.flush()


def _wait_for_signal(state: Path, name: str) -> None:
    target = state / "signals" / name
    deadline = time.monotonic() + _WAIT_TIMEOUT
    while not target.exists():
        if time.monotonic() > deadline:
            sys.stderr.write(f"fake_adb: timed out waiting for signal {name!r}\n")
            sys.exit(98)
        time.sleep(_POLL)


def _run_steps(state: Path, steps: list[dict[str, Any]]) -> None:
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
            _wait_for_signal(state, step["wait"])
        elif "touch" in step:
            (state / "signals" / step["touch"]).touch()
        else:
            sys.stderr.write(f"fake_adb: unknown step {step!r}\n")
            sys.exit(99)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--fake-state":
        sys.stderr.write("usage: fake_adb.py --fake-state DIR [adb args...]\n")
        return 2
    state = Path(sys.argv[2])
    command, key, _rest = _split_command(sys.argv[3:])
    index = _next_index(state, key)
    config = json.loads((state / "config.json").read_text(encoding="utf-8"))
    run = _select_run(config, command, key, index)
    if run is None:
        return 1
    if run.get("ignore_sigterm") and sys.platform != "win32":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _run_steps(state, run.get("steps", []))
    if run.get("hang"):
        while True:
            time.sleep(3600)
    return int(run.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
