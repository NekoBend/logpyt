"""Check C09 of design 9.4, run ON the adb server host (Linux), never in the container.

Does an adb server that our adb client starts share the client's process group,
or hold the client's stdout/stderr pipes? ``tools/device_probe.py`` (probe C09)
sends this file to ``python3 -`` on the server host through ``--vm-ssh``; the
only argument is the adb path there. Prints one JSON object.

For each of two runs (client in our process group; client in a new session,
as logpyt will spawn it) it stops the server (``adb kill-server``), starts
``adb devices`` with piped stdout/stderr, waits for it (a server that held the
pipes would keep ``communicate()`` from returning), and then describes the
server process from ``/proc``: parent, process group, session, fds 0-2 and
whether it holds one of the client's pipes. The last run leaves a server
running. Standard library only.

The answer holds for Linux only (it reads ``/proc``). Whether an adb server
that our client starts on Windows inherits the client's pipe handles is not
measured here (``tests/fixtures/device/FINDINGS.md``, open items).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def servers() -> list[int]:
    """Return the pids of running adb servers (``adb ... fork-server server``)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv and Path(os.fsdecode(argv[0])).name == "adb" and b"fork-server" in argv:
            found.append(int(entry.name))
    return found


def relations(pid: int) -> tuple[int, int, int]:
    """Return ``(ppid, pgid, sid)`` of ``pid`` from ``/proc/<pid>/stat``."""
    stat = (Path("/proc") / str(pid) / "stat").read_text()
    fields = stat[stat.rindex(")") + 2 :].split()
    return int(fields[1]), int(fields[2]), int(fields[3])


def describe(pid: int) -> dict[str, Any]:
    """Return the process relations and the fds 0-2 of ``pid``."""
    proc = Path("/proc") / str(pid)
    ppid, pgid, sid = relations(pid)
    fds: dict[int, str] = {}
    for fd in (proc / "fd").iterdir():
        try:
            fds[int(fd.name)] = str(fd.readlink())
        except OSError:
            continue
    args = [a.decode() for a in (proc / "cmdline").read_bytes().split(b"\0") if a]
    return {
        "pid": pid,
        "ppid": ppid,
        "pgid": pgid,
        "sid": sid,
        "args": args[1:],
        "fd0": fds.get(0),
        "fd1": fds.get(1),
        "fd2": fds.get(2),
        "pipes": sorted({v for v in fds.values() if v.startswith("pipe:")}),
    }


def one_run(adb: str, *, new_session: bool) -> dict[str, Any]:
    """Stop the server, let ``adb devices`` start it, describe the new server."""
    subprocess.run([adb, "kill-server"], capture_output=True, timeout=20, check=False)
    for _ in range(50):
        if not servers():
            break
        time.sleep(0.1)
    start = time.monotonic()
    proc = subprocess.Popen(
        [adb, "devices"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=new_session,
    )
    try:
        client_pgid: int | None = relations(proc.pid)[1]
    except OSError:
        client_pgid = None
    assert proc.stdout is not None  # noqa: S101 - stdout=PIPE above
    assert proc.stderr is not None  # noqa: S101 - stderr=PIPE above
    ours = {
        f"pipe:[{os.fstat(proc.stdout.fileno()).st_ino}]",
        f"pipe:[{os.fstat(proc.stderr.fileno()).st_ino}]",
    }
    try:
        _, err = proc.communicate(timeout=20)
        hung = False
    except subprocess.TimeoutExpired:
        hung = True
        proc.kill()
        _, err = proc.communicate()
    elapsed = time.monotonic() - start
    found = []
    for pid in servers():
        info = describe(pid)
        info["parent_is_client"] = info["ppid"] == proc.pid
        info["holds_client_pipe"] = bool(ours & set(info["pipes"]))
        info["same_pgid_as_client"] = info["pgid"] == client_pgid
        info["same_sid_as_probe"] = info["sid"] == relations(os.getpid())[2]
        found.append(info)
    return {
        "client_new_session": new_session,
        "client_pid": proc.pid,
        "client_pgid": client_pgid,
        "client_rc": proc.returncode,
        "client_hung": hung,
        "client_s": round(elapsed, 3),
        "client_stderr": err.decode(errors="replace").splitlines(),
        "servers": found,
    }


def main() -> None:
    """Run both cases and print the result as one JSON line."""
    adb = str(Path(sys.argv[1]).expanduser())
    runs = [one_run(adb, new_session=False), one_run(adb, new_session=True)]
    _, pgid, sid = relations(os.getpid())
    result = {
        "probe_pid": os.getpid(),
        "probe_pgid": pgid,
        "probe_sid": sid,
        "runs": runs,
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
