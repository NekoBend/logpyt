"""The thread driver at interpreter exit (design 3.4, 9.2).

A `LogStream` still referenced when the script ends is finalized by
``weakref.finalize`` at exit, while its daemon threads may never run again:
every adb child it started, a one-shot command's included, is killed then,
and an outcome nobody saw is logged with that cause. Each test runs a short
script in a child interpreter, with ``logpyt.adb._spawn_adb`` patched there
so that each adb command runs a small Python program instead (the
``fake_adb`` fixture patches only this process). A program that must be
seen to die holds a socket to the test: the test reads EOF on it once the
program is gone, on every platform.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import logpyt

if TYPE_CHECKING:
    from collections.abc import Iterator

TIMEOUT = 10.0

# The interpreter of the adb programs: the base one, so that killing the
# child kills the program itself (a venv's sys.executable on Windows is a
# launcher that starts python.exe as a second process).
_ADB_PYTHON = getattr(sys, "_base_executable", "") or sys.executable

# The child interpreter's prelude: `spawn(argv)` maps an adb argv to the
# program that plays it; the script after it defines PROGRAMS.
_PRELUDE = """\
import logging, subprocess, sys, time
from logpyt import LogStream, StreamConfig, adb
from logpyt.streams._events import StreamState

logging.basicConfig(
    level=logging.ERROR, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
)
_real = adb._spawn_adb
PYTHON, PORT = sys.argv[1], sys.argv[2]


def spawn(argv):
    for word, program in PROGRAMS.items():
        if word in argv:
            return _real([PYTHON, "-I", "-c", program, PORT])
    raise OSError("no program for " + " ".join(argv))


adb._spawn_adb = spawn
"""

# An adb that connects to the test, then waits until the test closes the
# connection (or until it is killed): it never ends by itself.
_HOLD = """\
import socket, sys
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
s.sendall(b"up")
s.recv(1)
"""


def _run_script(body: str, port: int) -> subprocess.Popen[bytes]:
    """Start the child interpreter with the prelude and ``body``."""
    env = dict(os.environ)
    root = str(Path(logpyt.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([root, *filter(None, [env.get("PYTHONPATH")])])
    script = _PRELUDE + textwrap.dedent(body)
    return subprocess.Popen(
        [sys.executable, "-c", script, _ADB_PYTHON, str(port)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _finish(proc: subprocess.Popen[bytes]) -> tuple[int, str]:
    """Let the script end (it waits for a line on stdin); its status and stderr."""
    try:
        _, stderr = proc.communicate(b"go\n", timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
    return proc.returncode, stderr.decode("utf-8", "replace")


def _closed(conn: socket.socket) -> bool:
    """Whether the peer of ``conn`` is gone: EOF (or a reset) within TIMEOUT."""
    conn.settimeout(TIMEOUT)
    try:
        return conn.recv(1) == b""
    except ConnectionError:  # a reset or an abort: the peer is gone too
        return True
    except TimeoutError:
        return False


@contextlib.contextmanager
def _server() -> Iterator[tuple[socket.socket, int]]:
    """A listening socket on the loopback and its port."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(TIMEOUT)
        yield server, server.getsockname()[1]


def test_exit_kills_a_pending_clock_read() -> None:
    """The script ends while the clock read before the first connection is
    still running (adb wedged): that adb is killed at exit, not left running
    once the script is gone (design 9.2). Its own timeout cannot end it then:
    the thread that enforces it dies with the interpreter.
    """
    body = """\
    PROGRAMS = {"date": HOLD}
    stream = LogStream(StreamConfig(adb_path="adb", stall_timeout=None))
    stream.start()
    sys.stdin.readline()  # the test saw the clock read start
    # The script ends with the stream referenced and the clock read running.
    """
    with _server() as (server, port):
        proc = _run_script(f"HOLD = {_HOLD!r}\n" + textwrap.dedent(body), port)
        try:
            conn, _ = server.accept()
        except TimeoutError:
            status, stderr = _finish(proc)
            raise AssertionError((status, stderr)) from None
        with conn:
            conn.settimeout(TIMEOUT)
            assert conn.recv(2) == b"up"
            status, stderr = _finish(proc)
            assert status == 0, stderr
            assert _closed(conn), "the clock read's adb outlived the script"


def test_exit_logs_an_unseen_failed_outcome() -> None:
    """A run that ended FAILED and is still referenced when the script ends,
    with nobody having seen its outcome, logs it at ERROR at exit, naming
    that cause (design 3.4): the stream was not garbage-collected.
    """
    body = """\
    PROGRAMS = {
        "date": "import time; print(int(time.time()))",
        "logcat": "import sys; sys.exit(1)",
    }
    stream = LogStream(StreamConfig(adb_path="adb", stall_timeout=None))
    stream.start()
    deadline = time.monotonic() + 10
    while stream.state is not StreamState.FAILED and time.monotonic() < deadline:
        time.sleep(0.01)
    print(stream.state, flush=True)
    sys.stdin.readline()
    """
    with _server() as (_, port):
        proc = _run_script(textwrap.dedent(body), port)
        status, stderr = _finish(proc)
        assert status == 0, stderr
        assert (
            "ERROR logpyt.stream: a LogStream was still referenced at interpreter "
            "exit and its run ended FAILED without anyone seeing it: AdbExitError: "
        ) in stderr, stderr


def test_exit_kills_an_adb_spawned_during_it() -> None:
    """The script ends while a worker is still inside the spawn of the clock
    read's adb: the child exists, but the worker has not handed it over yet.
    The exit lets that spawn finish (daemon threads still run while atexit
    handlers do) and kills the child, instead of leaving it running once the
    worker is frozen there by the interpreter's finalization (design 9.2).
    The spawn returns only after the main thread has ended.
    """
    body = """\
    import threading
    PROGRAMS = {"date": HOLD}
    entered = threading.Event()
    _plain = adb._spawn_adb


    def slow_spawn(argv):
        proc = _plain(argv)
        entered.set()
        while threading.main_thread().is_alive():
            time.sleep(0.005)
        time.sleep(0.05)  # the exit's finalizers are running meanwhile
        return proc


    adb._spawn_adb = slow_spawn
    stream = LogStream(StreamConfig(adb_path="adb", stall_timeout=None))
    stream.start()
    entered.wait(10)
    sys.stdin.readline()  # the test saw the clock read's adb start
    """
    with _server() as (server, port):
        proc = _run_script(f"HOLD = {_HOLD!r}\n" + textwrap.dedent(body), port)
        try:
            conn, _ = server.accept()
        except TimeoutError:
            status, stderr = _finish(proc)
            raise AssertionError((status, stderr)) from None
        with conn:
            conn.settimeout(TIMEOUT)
            assert conn.recv(2) == b"up"
            status, stderr = _finish(proc)
            assert status == 0, stderr
            assert _closed(conn), "the adb spawned during the exit outlived it"
