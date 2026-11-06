"""The asyncio driver when its loop or the interpreter ends with a stream running.

``asyncio.run`` cancels every task still pending once its main coroutine
returns, the stream's engine among them. The engine then kills adb at once,
starts nothing more and waits until asyncio has reaped what it killed
(design 5.4): the adb is gone, and reaped, before ``asyncio.run`` returns,
not killed later by the interpreter's exit, no adb was respawned although
reconnect is configured, and nothing is reported on stderr (no log record,
no destroyed pending task, no unclosed transport). 2.x skipped its teardown when its
connection task was cancelled in ``process.wait()``, respawned adb, and
``asyncio.run()`` never returned.

At interpreter exit (design 3.4, 9.2, A.23), a stream still referenced is
finalized by ``weakref.finalize`` while a loop that runs in a daemon thread
still runs: the run is killed through that loop and ends KILLED, a spawn in
progress there is let finish so that its child is killed too, and every adb
child is killed, a one-shot command's included. Under ``asyncio.run`` the
loop is closed by then, and an outcome nobody saw is logged at ERROR with
that cause.

Each test runs a short script in a child interpreter, as
``tests/test_sync_shutdown.py`` does (its socket helpers are reused), with
``logpyt.adb._async_spawn_adb`` patched there so that each adb command runs
a small Python program. A program that must be seen to die holds a socket to
the test. The script keeps its stream referenced, so only the loop's
teardown can end the run before the script waits for the test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import logpyt
from tests.conftest import logcat_line
from tests.test_sync_shutdown import _ADB_PYTHON, _HOLD, TIMEOUT, _closed, _server

if TYPE_CHECKING:
    import socket

# The child interpreter's prelude: `spawn(argv)` maps an adb argv to the
# program that plays it and counts the spawns; the script after it defines
# PROGRAMS from DATE, HOLD and LOGCAT.
_PRELUDE = """\
import asyncio, logging, sys
from logpyt import AsyncLogStream, Reconnect, StreamConfig, adb

logging.basicConfig(
    level=logging.WARNING,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)
_real = adb._async_spawn_adb
PYTHON, PORT = sys.argv[1], sys.argv[2]
SPAWNED = []
KEEP = []  # the streams stay referenced: only an exit finalizer ends their runs
CONFIG = StreamConfig(
    adb_path="adb", stall_timeout=None, reconnect=Reconnect(delay=0.05)
)


async def spawn(argv):
    for word, program in PROGRAMS.items():
        if word in argv:
            SPAWNED.append(word)
            return await _real([PYTHON, "-I", "-c", program, PORT])
    raise OSError("no program for " + " ".join(argv))


adb._async_spawn_adb = spawn
"""

# The clock read before a connection: the host's clock.
_DATE = "import time; print(int(time.time()))"

# An adb logcat that connects to the test, then prints one line (so the stream
# is RUNNING; the stream may kill it at once then), then waits until the test
# closes the connection or it is killed.
_LOGCAT = f"""\
import socket, sys
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
s.sendall(b"up")
sys.stdout.write({logcat_line("up")!r})
sys.stdout.flush()
s.recv(1)
"""


def _start(body: str, port: int) -> subprocess.Popen[bytes]:
    """Start the child interpreter with the prelude, the programs and ``body``.

    ResourceWarning is shown there, so an unclosed transport reaches stderr.
    """
    env = dict(os.environ)
    root = str(Path(logpyt.__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([root, *filter(None, [env.get("PYTHONPATH")])])
    programs = f"DATE = {_DATE!r}\nHOLD = {_HOLD!r}\nLOGCAT = {_LOGCAT!r}\n"
    script = _PRELUDE + programs + textwrap.dedent(body)
    return subprocess.Popen(
        [
            sys.executable,
            "-W",
            "always::ResourceWarning",
            "-c",
            script,
            _ADB_PYTHON,
            str(port),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _finish(proc: subprocess.Popen[bytes]) -> tuple[int, list[str], str]:
    """Let the script end (it waits for a line on stdin).

    Returns its exit status, its stdout lines and its stderr.
    """
    try:
        stdout, stderr = proc.communicate(b"go\n", timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    text = stdout.decode("utf-8", "replace")
    return proc.returncode, text.splitlines(), stderr.decode("utf-8", "replace")


def _accept(server: socket.socket, proc: subprocess.Popen[bytes]) -> socket.socket:
    """The connection of the script's first adb program; fail with its output."""
    try:
        conn, _ = server.accept()
    except TimeoutError:
        raise AssertionError(_finish(proc)) from None
    conn.settimeout(TIMEOUT)
    return conn


def test_teardown_kills_a_running_logcat() -> None:
    """``asyncio.run`` ends while the stream is RUNNING: its teardown kills the
    logcat adb and reaps it before ``asyncio.run`` returns (its
    ``returncode`` is set then), the script goes on, and no second logcat was
    spawned. Once the test saw adb gone, the script drops the stream and the
    logcat's Process: a transport left open by a missing reap would be
    reported on stderr then.
    """
    body = """\
    import gc
    PROGRAMS = {"date": DATE, "logcat": LOGCAT}
    LOGCATS = []  # the asyncio Process of each logcat adb


    async def recording_spawn(argv):
        proc = await spawn(argv)
        if "logcat" in argv:
            LOGCATS.append(proc)
        return proc


    adb._async_spawn_adb = recording_spawn


    async def main():
        stream = AsyncLogStream(CONFIG)
        KEEP.append(stream)
        stream.start()
        await stream.wait_running(10)
        print("running", flush=True)
        # main returns with the run going on: asyncio.run cancels its tasks


    asyncio.run(main())
    reaped = [proc.returncode is not None for proc in LOGCATS]
    print("returned", SPAWNED.count("logcat"), "reaped", reaped, flush=True)
    sys.stdin.readline()  # the test checked that adb is gone
    KEEP.clear()
    LOGCATS.clear()
    gc.collect()
    """
    with _server() as (server, port):
        proc = _start(body, port)
        with _accept(server, proc) as conn:
            assert conn.recv(2) == b"up"
            gone = _closed(conn)  # the script still waits for the test
            status, stdout, stderr = _finish(proc)

    assert status == 0, stderr
    assert stdout == ["running", "returned 1 reaped [True]"], stderr
    assert gone, "the logcat adb outlived asyncio.run"
    assert stderr == ""


def test_teardown_kills_a_pending_clock_read() -> None:
    """``asyncio.run`` ends while the clock read before the first connection is
    still running (adb wedged): its teardown cancels the one-shot command,
    whose adb is killed and reaped then, and no logcat is spawned.
    """
    body = """\
    PROGRAMS = {"date": HOLD}


    async def main():
        stream = AsyncLogStream(CONFIG)
        KEEP.append(stream)
        stream.start()
        await asyncio.to_thread(sys.stdin.readline)  # the clock read runs
        # main returns with the run going on: asyncio.run cancels its tasks


    asyncio.run(main())
    print("returned", SPAWNED.count("logcat"), flush=True)
    sys.stdin.readline()  # the test checked that adb is gone
    """
    with _server() as (server, port):
        proc = _start(body, port)
        with _accept(server, proc) as conn:
            assert conn.recv(2) == b"up"
            assert proc.stdin is not None
            proc.stdin.write(b"go\n")
            proc.stdin.flush()
            gone = _closed(conn)  # the script still waits for the test
            status, stdout, stderr = _finish(proc)

    assert status == 0, stderr
    assert stdout == ["returned 0"], stderr
    assert gone, "the clock read's adb outlived asyncio.run"
    assert stderr == ""


# A script whose loop runs in a daemon thread: `begin()` starts a stream on it
# and keeps it.
_IN_A_THREAD = """\
import threading

LOOP = asyncio.new_event_loop()
threading.Thread(target=LOOP.run_forever, daemon=True).start()


def begin():
    async def start():
        stream = AsyncLogStream(CONFIG)
        stream.start()
        return stream

    KEEP.append(asyncio.run_coroutine_threadsafe(start(), LOOP).result(10))
"""

# `report()`, a finalizer made before the stream's (so it runs after it at
# exit), waits until the run ended and prints its state; it holds the exit
# back meanwhile.
_REPORT = """\
import time, weakref
from logpyt import StreamState


def report():
    deadline = time.monotonic() + 5
    while KEEP[0].state not in {
        StreamState.STOPPED, StreamState.KILLED, StreamState.FAILED
    } and time.monotonic() < deadline:
        time.sleep(0.01)
    print("at exit", KEEP[0].state, flush=True)


weakref.finalize(LOOP, report)
"""


def test_exit_kills_a_logcat_in_a_thread() -> None:
    """The interpreter exits while a stream runs on a loop in a daemon thread:
    the exit finalizer kills the logcat adb and posts the kill to that loop,
    which still runs, so the run ends KILLED, as the thread driver's does,
    not FAILED by the end of the adb the library killed, and no ERROR says
    that nobody saw that outcome.
    """
    body = (
        _IN_A_THREAD
        + _REPORT
        + textwrap.dedent("""\
    PROGRAMS = {"date": DATE, "logcat": LOGCAT}
    begin()
    sys.stdin.readline()  # the test saw the logcat adb start
    """)
    )
    with _server() as (server, port):
        proc = _start(body, port)
        with _accept(server, proc) as conn:
            assert conn.recv(2) == b"up"
            status, stdout, stderr = _finish(proc)
            gone = _closed(conn)

    assert status == 0, stderr
    assert gone, "the logcat adb outlived the interpreter"
    assert stdout == ["at exit KILLED"], stderr
    assert "ERROR logpyt" not in stderr, stderr


def test_exit_kills_a_clock_read_in_a_thread() -> None:
    """The interpreter exits while the clock read before the first connection
    still runs (adb wedged) on a loop in a daemon thread: the exit kills the
    one-shot command's adb, which the run tracks from its spawn on.
    """
    body = _IN_A_THREAD + textwrap.dedent("""\
    PROGRAMS = {"date": HOLD}
    begin()
    sys.stdin.readline()  # the test saw the clock read start
    """)
    with _server() as (server, port):
        proc = _start(body, port)
        with _accept(server, proc) as conn:
            assert conn.recv(2) == b"up"
            status, _, stderr = _finish(proc)
            gone = _closed(conn)

    assert status == 0, stderr
    assert gone, "the clock read's adb outlived the interpreter"


def test_exit_kills_an_adb_spawned_during_it() -> None:
    """The interpreter exits while the loop's thread is inside the spawn of
    the logcat adb: the child exists, but the spawn has not returned it yet.
    The exit lets that spawn finish (the thread still runs while atexit
    handlers do) and kills the child, instead of leaving it running once
    finalization froze the thread there (design A.23). The spawn returns only
    after the main thread has ended.
    """
    body = _IN_A_THREAD + textwrap.dedent("""\
    PROGRAMS = {"date": DATE, "logcat": LOGCAT}
    entered = threading.Event()


    async def slow_spawn(argv):
        proc = await spawn(argv)
        if "logcat" in argv:
            entered.set()
            while threading.main_thread().is_alive():
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.05)  # the exit's finalizers run meanwhile
        return proc


    adb._async_spawn_adb = slow_spawn
    begin()
    entered.wait(10)
    sys.stdin.readline()  # the test saw the logcat adb start
    """)
    with _server() as (server, port):
        proc = _start(body, port)
        with _accept(server, proc) as conn:
            assert conn.recv(2) == b"up"
            status, _, stderr = _finish(proc)
            gone = _closed(conn)

    assert status == 0, stderr
    assert gone, "the adb spawned during the exit outlived it"


def test_exit_logs_an_unseen_failed_outcome() -> None:
    """A run that ended FAILED under ``asyncio.run`` and is still referenced
    when the script ends, with nobody having seen its outcome, logs it at
    ERROR at exit, naming that cause (design 3.4, A.23).
    """
    body = """\
    from logpyt import StreamState
    PROGRAMS = {"date": DATE, "logcat": "import sys; sys.exit(1)"}


    async def main():
        stream = AsyncLogStream(StreamConfig(adb_path="adb", stall_timeout=None))
        KEEP.append(stream)
        stream.start()
        for _ in range(1000):
            if stream.state is StreamState.FAILED:
                break
            await asyncio.sleep(0.01)
        print(stream.state, flush=True)


    asyncio.run(main())
    sys.stdin.readline()
    """
    with _server() as (_, port):
        proc = _start(body, port)
        status, stdout, stderr = _finish(proc)

    assert status == 0, stderr
    assert stdout == ["FAILED"], stderr
    assert (
        "ERROR logpyt.stream: an AsyncLogStream was still referenced at "
        "interpreter exit and its run ended FAILED without anyone seeing it: "
        "AdbExitError: "
    ) in stderr, stderr
