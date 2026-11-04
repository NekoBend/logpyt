"""Shared fixtures: a real-subprocess fake adb (see ``tests/fake_adb.py``)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_FAKE_ADB_SCRIPT = Path(__file__).with_name("fake_adb.py").resolve()

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


class FakeAdb:
    """Handle returned by the ``fake_adb`` fixture.

    Pass ``fake.path`` as ``adb_path`` (``resolve_adb`` is also patched to
    return it). Configure behavior with ``logcat()`` / ``pidof()`` /
    ``command()`` before the library spawns adb.
    """

    def __init__(self, state: Path) -> None:
        self.state = state
        (state / "signals").mkdir(parents=True)
        self.path = str(state / "adb")
        self._config: dict[str, Any] = {}
        self._write_config()

    # -- configuration -------------------------------------------------

    def logcat(self, *runs: Run) -> None:
        """Set the runs for successive ``adb logcat`` invocations."""
        self._config["logcat"] = list(runs)
        self._write_config()

    def pidof(self, package: str, *runs: Run) -> None:
        """Set the runs for successive ``adb shell pidof <package>`` calls."""
        self._config.setdefault("pidof", {})[package] = list(runs)
        self._write_config()

    def command(self, name: str, *runs: Run) -> None:
        """Set the runs for another adb command (``devices``, ``connect``...)."""
        self._config[name] = list(runs)
        self._write_config()

    def _write_config(self) -> None:
        tmp = self.state / "config.json.tmp"
        tmp.write_text(json.dumps(self._config), encoding="utf-8")
        tmp.replace(self.state / "config.json")

    # -- observation and synchronization ----------------------------------

    def calls(self, command: str | None = None) -> list[dict[str, Any]]:
        """Recorded invocations, optionally only those of ``command``."""
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
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
            time.sleep(0.005)

    async def async_wait_for(self, name: str, timeout: float = 10.0) -> None:
        """Async variant of ``wait_for``."""
        target = self.state / "signals" / name
        deadline = time.monotonic() + timeout
        while not target.exists():
            if time.monotonic() > deadline:
                msg = f"fake adb never signalled {name!r}"
                raise TimeoutError(msg)
            await asyncio.sleep(0.005)

    # -- spawn rewriting -------------------------------------------------

    def rewrite(self, args: Sequence[Any]) -> list[Any]:
        """Turn ``[fake.path, *adb_args]`` into a python invocation."""
        argv = list(args)
        if argv and os.fspath(argv[0]) == self.path:
            return [
                sys.executable,
                "-I",
                str(_FAKE_ADB_SCRIPT),
                "--fake-state",
                str(self.state),
                *argv[1:],
            ]
        return argv


def wait_until(
    predicate: Callable[[], object], timeout: float = 5.0, interval: float = 0.005
) -> None:
    """Poll ``predicate`` until it is truthy; raise TimeoutError otherwise."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            msg = "condition not met before the timeout"
            raise TimeoutError(msg)
        time.sleep(interval)


async def async_wait_until(
    predicate: Callable[[], object], timeout: float = 5.0, interval: float = 0.005
) -> None:
    """Async variant of ``wait_until``."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            msg = "condition not met before the timeout"
            raise TimeoutError(msg)
        await asyncio.sleep(interval)


@pytest.fixture
def fake_adb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAdb:
    """A real-subprocess fake adb; see ``tests/fake_adb.py`` for the format."""
    fake = FakeAdb(tmp_path / "fake-adb")

    real_popen = subprocess.Popen

    # Transparent pass-through wrappers: they forward whatever the library
    # passes, so their parameters are deliberately untyped (ANN401).
    class _RewritingPopen(real_popen):
        def __init__(self, args: Any, *a: Any, **kw: Any) -> None:  # noqa: ANN401
            if isinstance(args, (list, tuple)):
                args = fake.rewrite(args)
            super().__init__(args, *a, **kw)

    real_exec = asyncio.create_subprocess_exec

    async def _rewriting_exec(program: Any, *args: Any, **kw: Any) -> Any:  # noqa: ANN401
        argv = fake.rewrite([program, *args])
        return await real_exec(*argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", _RewritingPopen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _rewriting_exec)
    for target in (
        "logpyt.utils.resolve_adb",
        "logpyt.streams.sync.resolve_adb",
        "logpyt.streams.async_stream.resolve_adb",
    ):
        monkeypatch.setattr(target, lambda: fake.path)
    return fake
