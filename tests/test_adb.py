"""Tests for logpyt.adb: resolving adb, the device helpers, the runners, the argv
builders and the output parsers.

The helpers and runners run against the real-subprocess fake adb
(``tests/conftest.py``). The parsers also read the device recordings of PR-02
(``tests/fixtures/device``), so these run in normal CI without a device.
"""

from __future__ import annotations

import _thread
import asyncio
import contextlib
import gc
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from logpyt import adb
from logpyt.adb import DeviceInfo, _AdbResult
from logpyt.exceptions import AdbError
from logpyt.streams import AsyncLogStream, LogStream
from tests import device_facts as facts
from tests.conftest import _FAKE_PYTHON, err, lines, out, raw, run, touch
from tests.device_facts import LEVELS, body, level_dir, load, step

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.conftest import FakeAdb

# The real resolver, taken before any fake_adb fixture patches the module
# attribute (conftest.RESOLVE_ADB_TARGETS).
REAL_RESOLVE_ADB = adb.resolve_adb

ADB_NAME = "adb.exe" if sys.platform == "win32" else "adb"
HEADER = "List of devices attached"
ADDRESS = "10.0.0.2:5555"
PACKAGE = "com.example.app"
TIMEOUT = 10.0
# Short enough to keep the suite fast, long enough for the fake to start.
SHORT = 0.5

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _result(
    *,
    argv: tuple[str, ...] = ("adb",),
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> _AdbResult:
    return _AdbResult(argv=argv, returncode=returncode, stdout=stdout, stderr=stderr)


def _recorded(recorded_step: dict[str, Any]) -> _AdbResult:
    """The `_AdbResult` of a recorded step (its lines joined with LF again)."""

    def text(recorded: list[str]) -> str:
        return "".join(f"{line}\n" for line in recorded)

    return _AdbResult(
        argv=("adb", *recorded_step["argv"]),
        returncode=recorded_step["rc"],
        stdout=text(recorded_step["stdout"]),
        stderr=text(recorded_step["stderr"]),
    )


def _shell_command(argv: tuple[str, ...]) -> str:
    """The command line ``adb shell`` hands to the device shell (words joined)."""
    return " ".join(argv[argv.index("shell") + 1 :])


def _assert_reaped(fake: FakeAdb) -> None:
    """Every fake child has exited and the library has collected its status.

    Checked right after the call returned, without waiting: a status is there
    only if the library waited for the child itself.
    """
    children = fake.children()
    assert children
    for child in children:
        handle = child.popen if child.process is None else child.process
        assert handle.returncode is not None, f"{child!r} was not reaped"


def _make_executable(directory: Path, name: str = ADB_NAME) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def no_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        monkeypatch.delenv(var, raising=False)


# Names logpyt.adb binds by importing them, besides modules.
_ADB_IMPORTS = {
    "AdbError",
    "Literal",
    "Path",
    "TYPE_CHECKING",
    "annotations",
    "dataclass",
}


def test_public_surface() -> None:
    """``__all__`` is design 2.10's list; every other name the module defines is
    private, apart from its logger and the `DeviceKind` type alias.
    """
    assert sorted(adb.__all__) == [
        "DeviceInfo",
        "adb_connect",
        "async_adb_connect",
        "async_wait_for_device",
        "list_devices",
        "resolve_adb",
        "wait_for_device",
    ]
    public = {
        name
        for name, value in vars(adb).items()
        if not name.startswith("_") and not isinstance(value, type(adb))
    }
    assert public - _ADB_IMPORTS == {*adb.__all__, "DeviceKind", "logger"}


# -- resolve_adb (F9-3) ------------------------------------------------------------


@pytest.mark.usefixtures("no_sdk")
def test_resolve_adb_returns_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative PATH entry yields an absolute path that survives a chdir (F9-3)."""
    expected = _make_executable(tmp_path / "tools")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "tools")

    found = adb.resolve_adb()

    assert Path(found).is_absolute()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert Path(found).samefile(expected)


@pytest.mark.usefixtures("no_sdk")
def test_windows_cwd_adb_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adb in the working directory is used only when PATH names the directory.

    A Windows command search would run ``.\\adb.exe`` first (F9-3); the rule is
    the same on every platform, so it is checked everywhere.
    """
    planted = _make_executable(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(tmp_path)

    # An empty PATH entry means the working directory to a POSIX shell.
    for path in (str(empty), os.pathsep + str(empty), str(empty) + os.pathsep):
        monkeypatch.setenv("PATH", path)
        with pytest.raises(FileNotFoundError):
            adb.resolve_adb()

    monkeypatch.setenv("PATH", os.pathsep.join([str(empty), "."]))
    found = adb.resolve_adb()
    assert Path(found).is_absolute()
    assert planted.samefile(found)


@windows_only
@pytest.mark.usefixtures("no_sdk")
def test_resolve_adb_reads_a_quoted_path_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows allows a quoted PATH entry, as for a directory with a space."""
    expected = _make_executable(tmp_path / "Program Files" / "platform-tools")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(empty), f' "{expected.parent}" ']))

    assert expected.samefile(adb.resolve_adb())


def test_resolve_adb_falls_back_to_the_sdk_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    in_root = _make_executable(tmp_path / "root" / "platform-tools")
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("ANDROID_HOME", str(home))
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(tmp_path / "root"))

    assert in_root.samefile(adb.resolve_adb())

    in_home = _make_executable(home / "platform-tools")
    assert in_home.samefile(adb.resolve_adb())


def test_resolve_adb_prefers_path_over_the_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_path = _make_executable(tmp_path / "bin")
    _make_executable(tmp_path / "sdk" / "platform-tools")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "sdk"))

    assert on_path.samefile(adb.resolve_adb())


@pytest.mark.usefixtures("no_sdk")
def test_resolve_adb_raises_when_no_adb_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="adb_path="):
        adb.resolve_adb()


@posix_only
@pytest.mark.usefixtures("no_sdk")
def test_resolve_adb_skips_a_file_that_is_not_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain file and a directory named adb are no candidates."""
    plain = _make_executable(tmp_path / "plain")
    plain.chmod(0o644)
    (tmp_path / "dir" / ADB_NAME).mkdir(parents=True)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(plain.parent), str(tmp_path / "dir")])
    )

    with pytest.raises(FileNotFoundError):
        adb.resolve_adb()


@pytest.mark.usefixtures("no_sdk")
def test_resolve_adb_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _make_executable(tmp_path / "a")
    second = _make_executable(tmp_path / "b")
    monkeypatch.setenv("PATH", str(tmp_path / "a"))
    assert first.samefile(adb.resolve_adb())
    monkeypatch.setenv("PATH", str(tmp_path / "b"))
    assert second.samefile(adb.resolve_adb())


def test_only_the_fake_adb_is_spawned_with_path_emptied(
    fake_adb: FakeAdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no adb to find, every helper and both streams still reach the fake.

    PATH is empty and no SDK variable is set, so the real resolver finds
    nothing: a spawn that bypassed ``logpyt.adb.resolve_adb``, the single patch
    target, would have no adb to run and fail.
    """
    monkeypatch.setenv("PATH", "")
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(FileNotFoundError):
        REAL_RESOLVE_ADB()
    fake_adb.command("devices", run(out(f"{HEADER}\nemulator-5554\tdevice\n\n")))
    fake_adb.command("wait-for-device", run())
    fake_adb.command("connect", run(out(f"connected to {ADDRESS}\n")))

    assert [info.serial for info in adb.list_devices()] == ["emulator-5554"]
    adb.wait_for_device(timeout=TIMEOUT)
    adb.adb_connect(ADDRESS, timeout=TIMEOUT)

    async def use_the_async_helpers() -> str:
        await adb.async_wait_for_device(timeout=TIMEOUT)
        await adb.async_adb_connect(ADDRESS, timeout=TIMEOUT)
        return AsyncLogStream()._adb_path

    assert asyncio.run(use_the_async_helpers()) == fake_adb.path
    assert LogStream()._adb_path == fake_adb.path
    commands = ["devices", "wait-for-device", "connect", "wait-for-device", "connect"]
    assert [call["command"] for call in fake_adb.calls()] == commands
    assert [child.command for child in fake_adb.children()] == commands


# -- list_devices (F9-1) --------------------------------------------------------------

LISTING = (
    f"{HEADER}\n"
    "emulator-5554          device product:sdk_gphone64_x86_64 "
    "model:sdk_gphone64_x86_64 device:emu64xa transport_id:1\n"
    "R58M12ABCDE            device usb:1-1.2 product:a52qxx model:SM_A525F "
    "device:a52q transport_id:2\n"
    "192.168.1.5:5555       offline transport_id:3\n"
    "\n"
)


def test_list_devices_parses_the_long_listing(fake_adb: FakeAdb) -> None:
    fake_adb.command("devices", run(out(LISTING)))

    devices = adb.list_devices()

    assert devices == [
        DeviceInfo(
            serial="emulator-5554",
            state="device",
            kind="emulator",
            product="sdk_gphone64_x86_64",
            model="sdk_gphone64_x86_64",
            device="emu64xa",
            transport_id=1,
        ),
        DeviceInfo(
            serial="R58M12ABCDE",
            state="device",
            kind="usb",
            product="a52qxx",
            model="SM_A525F",
            device="a52q",
            transport_id=2,
            usb="1-1.2",
        ),
        DeviceInfo(
            serial="192.168.1.5:5555", state="offline", kind="tcp", transport_id=3
        ),
    ]
    assert fake_adb.calls("devices")[0]["args"] == ["devices", "-l"]


def test_list_devices_keeps_a_property_value_with_a_space(fake_adb: FakeAdb) -> None:
    """A value ends where the next property begins, not at a space."""
    fake_adb.command(
        "devices",
        run(
            out(
                f"{HEADER}\n1234567890abc device product:my_phone model:Pixel 5 "
                "device:pixel5 transport_id:3\n"
            )
        ),
    )

    (info,) = adb.list_devices()

    assert (info.product, info.model, info.device, info.transport_id) == (
        "my_phone",
        "Pixel 5",
        "pixel5",
        3,
    )


def test_devices_with_daemon_preamble_and_no_permissions_state(
    fake_adb: FakeAdb,
) -> None:
    """Server start lines are no devices, and a state of several words stays whole
    (F9-1).
    """
    state = (
        "no permissions (missing udev rules? user is in the plugdev group); "
        "see [http://developer.android.com/tools/device.html]"
    )
    fake_adb.command(
        "devices",
        run(
            out(
                "* daemon not running; starting now at tcp:5037\n"
                "* daemon started successfully\n"
                f"{HEADER}\n"
                f"0123456789ABCDEF       {state} usb:1-4 transport_id:7\n"
                f"emulator-5554\t{state}\n"
                "\n"
            )
        ),
    )

    assert adb.list_devices() == [
        DeviceInfo(
            serial="0123456789ABCDEF",
            state=state,
            kind="usb",
            transport_id=7,
            usb="1-4",
        ),
        DeviceInfo(serial="emulator-5554", state=state, kind="emulator"),
    ]


@pytest.mark.parametrize(
    ("serial", "usb", "kind"),
    [
        ("emulator-5554", None, "emulator"),
        ("192.168.1.5:5555", None, "tcp"),
        ("localhost:5555", None, "tcp"),
        ("[::1]:5555", None, "tcp"),
        ("adb-R5CR10ABCDE-AbCdEf._adb-tls-connect._tcp", None, "tcp"),
        ("adb-R5CR10ABCDE-AbCdEf._adb-tls-connect._tcp.", None, "tcp"),
        ("R58M12ABCDE", "1-1", "usb"),
        ("R58M12ABCDE", None, "usb"),  # adb on Windows prints no USB path
        ("(no serial number)", "1-2", "usb"),
        ("(no serial number)", None, "unknown"),
        ("vsock:3:5555", None, "unknown"),
    ],
)
def test_device_kind_follows_the_serial(
    serial: str, usb: str | None, kind: str
) -> None:
    usb_property = f" usb:{usb}" if usb else ""
    listing = f"{HEADER}\n{serial}   device{usb_property} transport_id:9\n"

    (info,) = adb._parse_devices(_result(stdout=listing))

    assert (info.serial, info.kind, info.usb, info.transport_id) == (
        serial,
        kind,
        usb,
        9,
    )


def test_list_devices_filters_by_kind(fake_adb: FakeAdb) -> None:
    fake_adb.command(
        "devices",
        run(
            out(
                f"{HEADER}\nemulator-5554\tdevice\nR58M12ABCDE\tdevice\n"
                "10.0.0.2:5555\tdevice\nvsock:3:5555\tdevice\n"
            )
        ),
    )

    assert [d.serial for d in adb.list_devices("emulator")] == ["emulator-5554"]
    assert [d.serial for d in adb.list_devices("usb")] == ["R58M12ABCDE"]
    assert [d.serial for d in adb.list_devices("tcp")] == ["10.0.0.2:5555"]
    assert [d.serial for d in adb.list_devices("unknown")] == ["vsock:3:5555"]
    assert len(adb.list_devices()) == 4


@pytest.mark.parametrize("device_type", ["device", "USB", "", "phone"])
def test_list_devices_rejects_an_unknown_device_type(
    fake_adb: FakeAdb, device_type
) -> None:
    """2.x accepted any value, and ``"device"`` filtered by state (F9-1)."""
    with pytest.raises(ValueError, match="device_type"):
        adb.list_devices(device_type)
    assert fake_adb.calls() == []


def test_list_devices_raises_adb_error_on_failure(fake_adb: FakeAdb) -> None:
    fake_adb.command(
        "devices",
        run(err("adb: cannot connect to daemon at tcp:5037\n"), exit_code=1),
    )

    with pytest.raises(AdbError, match="cannot connect to daemon") as caught:
        adb.list_devices()

    assert caught.value.argv == (fake_adb.path, "devices", "-l")
    assert caught.value.returncode == 1
    assert "cannot connect to daemon" in caught.value.stderr


def test_list_devices_without_header_raises_adb_error(fake_adb: FakeAdb) -> None:
    fake_adb.command("devices", run(out("emulator-5554\tdevice\n")))

    with pytest.raises(AdbError, match=HEADER):
        adb.list_devices()


def test_list_devices_timeout_kills_the_child(fake_adb: FakeAdb) -> None:
    fake_adb.command("devices", run(touch("listing"), hang=True))

    with pytest.raises(TimeoutError, match="adb devices -l did not finish"):
        adb.list_devices(timeout=SHORT)

    _assert_reaped(fake_adb)


# -- wait_for_device (F3-5) ------------------------------------------------------------


def test_wait_for_device_passes_the_serial(fake_adb: FakeAdb) -> None:
    fake_adb.command("wait-for-device", run())

    adb.wait_for_device("emulator-5554", timeout=TIMEOUT)

    calls = fake_adb.calls("wait-for-device")
    assert [call["args"] for call in calls] == [
        ["-s", "emulator-5554", "wait-for-device"]
    ]


def test_wait_for_device_raises_adb_error(fake_adb: FakeAdb) -> None:
    fake_adb.command(
        "wait-for-device",
        run(err("error: device 'emulator-9999' not found\n"), exit_code=1),
    )

    with pytest.raises(AdbError, match="not found") as caught:
        adb.wait_for_device("emulator-9999")

    assert caught.value.returncode == 1


def test_wait_for_device_timeout_kills_the_child(fake_adb: FakeAdb) -> None:
    fake_adb.command("wait-for-device", run(err("- waiting for device -\n"), hang=True))

    with pytest.raises(TimeoutError, match="wait-for-device did not finish"):
        adb.wait_for_device(timeout=SHORT)

    _assert_reaped(fake_adb)


_LARGE_OUTPUT = (
    lines(4000, "o" * 70 + " {i}\n"),
    lines(4000, "e" * 70 + " {i}\n", stream="err"),
)


def test_wait_for_device_drains_a_large_output(fake_adb: FakeAdb) -> None:
    """Output beyond a pipe buffer cannot stall the wait (2.x never read it: F3-5)."""
    fake_adb.command("wait-for-device", run(*_LARGE_OUTPUT))

    adb.wait_for_device(timeout=TIMEOUT)


@pytest.mark.asyncio
async def test_async_wait_for_device_passes_the_serial(fake_adb: FakeAdb) -> None:
    fake_adb.command("wait-for-device", run())

    await adb.async_wait_for_device("emulator-5554", timeout=TIMEOUT)

    calls = fake_adb.calls("wait-for-device")
    assert [call["args"] for call in calls] == [
        ["-s", "emulator-5554", "wait-for-device"]
    ]


@pytest.mark.asyncio
async def test_async_wait_for_device_raises_adb_error(fake_adb: FakeAdb) -> None:
    fake_adb.command(
        "wait-for-device", run(err("error: device offline\n"), exit_code=1)
    )

    with pytest.raises(AdbError, match="device offline"):
        await adb.async_wait_for_device(timeout=TIMEOUT)


@pytest.mark.asyncio
async def test_async_wait_for_device_timeout_kills_the_child(fake_adb: FakeAdb) -> None:
    fake_adb.command("wait-for-device", run(hang=True))

    with pytest.raises(TimeoutError, match="wait-for-device did not finish"):
        await adb.async_wait_for_device(timeout=SHORT)

    _assert_reaped(fake_adb)


@pytest.mark.asyncio
async def test_async_wait_for_device_drains_a_large_output(fake_adb: FakeAdb) -> None:
    fake_adb.command("wait-for-device", run(*_LARGE_OUTPUT))

    await adb.async_wait_for_device(timeout=TIMEOUT)


@pytest.mark.asyncio
async def test_cancel_wait_for_device_reaps_child(fake_adb: FakeAdb) -> None:
    """Cancelling the wait kills adb and collects its exit status first (F3-5)."""
    fake_adb.command("wait-for-device", run(touch("waiting"), hang=True))
    task = asyncio.create_task(adb.async_wait_for_device())
    await fake_adb.async_wait_for("waiting", timeout=TIMEOUT)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_reaped(fake_adb)


# -- adb_connect (F9-2, F3-5) ----------------------------------------------------------


@pytest.mark.parametrize(
    "stdout",
    [
        f"connected to {ADDRESS}\n",
        f"already connected to {ADDRESS}\n",
        f"* daemon started successfully\nconnected to {ADDRESS}\n",
    ],
)
def test_adb_connect_succeeds(fake_adb: FakeAdb, stdout: str) -> None:
    fake_adb.command("connect", run(out(stdout)))

    adb.adb_connect(ADDRESS, timeout=TIMEOUT)

    assert fake_adb.calls("connect")[0]["args"] == ["connect", ADDRESS]


def test_connect_host_named_unable_succeeds(fake_adb: FakeAdb) -> None:
    """2.x took any output containing "unable" or "failed" for a failure (F9-2)."""
    host = "unable-failed.example:5555"
    fake_adb.command("connect", run(out(f"connected to {host}\n")))

    adb.adb_connect(host, timeout=TIMEOUT)


_CONNECT_FAILURES = [
    (f"unable to connect to {ADDRESS}: Connection refused\n", 0),
    (f"failed to connect to '{ADDRESS}': Connection refused\n", 1),
    (f"failed to authenticate to {ADDRESS}\n", 1),
    ("", 1),
    (f"connected to {ADDRESS}\n", 1),
]


@pytest.mark.parametrize(("stdout", "exit_code"), _CONNECT_FAILURES)
def test_adb_connect_failure_raises_adb_error(
    fake_adb: FakeAdb, stdout: str, exit_code: int
) -> None:
    fake_adb.command("connect", run(out(stdout), exit_code=exit_code))

    with pytest.raises(AdbError, match="adb connect failed") as caught:
        adb.adb_connect(ADDRESS, timeout=TIMEOUT)

    assert caught.value.returncode == exit_code
    assert stdout.strip() in str(caught.value)


def test_adb_connect_timeout_kills_the_child(fake_adb: FakeAdb) -> None:
    fake_adb.command("connect", run(hang=True))

    with pytest.raises(TimeoutError, match="connect"):
        adb.adb_connect(ADDRESS, timeout=SHORT)

    _assert_reaped(fake_adb)


@pytest.mark.asyncio
async def test_async_adb_connect_succeeds(fake_adb: FakeAdb) -> None:
    fake_adb.command("connect", run(out(f"already connected to {ADDRESS}\n")))

    await adb.async_adb_connect(ADDRESS, timeout=TIMEOUT)


@pytest.mark.asyncio
@pytest.mark.parametrize(("stdout", "exit_code"), _CONNECT_FAILURES)
async def test_async_adb_connect_failure_raises_adb_error(
    fake_adb: FakeAdb, stdout: str, exit_code: int
) -> None:
    fake_adb.command("connect", run(out(stdout), exit_code=exit_code))

    with pytest.raises(AdbError, match="adb connect failed"):
        await adb.async_adb_connect(ADDRESS, timeout=TIMEOUT)


@pytest.mark.asyncio
async def test_async_adb_connect_timeout_kills_the_child(fake_adb: FakeAdb) -> None:
    fake_adb.command("connect", run(hang=True))

    with pytest.raises(TimeoutError, match="connect"):
        await adb.async_adb_connect(ADDRESS, timeout=SHORT)

    _assert_reaped(fake_adb)


@pytest.mark.asyncio
async def test_cancel_adb_connect_reaps_child(fake_adb: FakeAdb) -> None:
    fake_adb.command("connect", run(touch("connecting"), hang=True))
    task = asyncio.create_task(adb.async_adb_connect(ADDRESS))
    await fake_adb.async_wait_for("connecting", timeout=TIMEOUT)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_reaped(fake_adb)


# -- argument checks -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeout", "error"),
    [
        (0, ValueError),
        (-1.0, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (True, TypeError),
        ("5", TypeError),
    ],
)
def test_helpers_reject_an_invalid_timeout(
    fake_adb: FakeAdb, timeout, error: type[Exception]
) -> None:
    with pytest.raises(error, match="timeout"):
        adb.list_devices(timeout=timeout)
    with pytest.raises(error, match="timeout"):
        adb.wait_for_device(timeout=timeout)
    with pytest.raises(error, match="timeout"):
        adb.adb_connect(ADDRESS, timeout=timeout)
    with pytest.raises(error, match="timeout"):
        asyncio.run(adb.async_wait_for_device(timeout=timeout))
    with pytest.raises(error, match="timeout"):
        asyncio.run(adb.async_adb_connect(ADDRESS, timeout=timeout))
    assert fake_adb.calls() == []


@pytest.mark.parametrize(
    ("serial", "error"),
    [("", ValueError), ("emulator\0", ValueError), (5554, TypeError)],
)
def test_helpers_reject_an_invalid_serial(
    fake_adb: FakeAdb, serial, error: type[Exception]
) -> None:
    with pytest.raises(error, match="serial"):
        adb.wait_for_device(serial)
    with pytest.raises(error, match="serial"):
        asyncio.run(adb.async_wait_for_device(serial))
    with pytest.raises(error, match="serial"):
        adb._get_state_argv("adb", serial)
    assert fake_adb.calls() == []


@pytest.mark.parametrize("address", ["", "-H", f" {ADDRESS}", f"{ADDRESS}\n", "a\0b"])
def test_adb_connect_rejects_an_invalid_address(
    fake_adb: FakeAdb, address: str
) -> None:
    """An address that adb would read as an option, or that is not one word."""
    with pytest.raises(ValueError, match="address"):
        adb.adb_connect(address)
    assert fake_adb.calls() == []


def test_explicit_adb_path_is_used_without_resolving(
    fake_adb: FakeAdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_resolution() -> str:
        pytest.fail("resolve_adb ran although adb_path was given")

    monkeypatch.setattr(adb, "resolve_adb", no_resolution)
    fake_adb.command("wait-for-device", run())

    adb.wait_for_device(adb_path=fake_adb.path, timeout=TIMEOUT)

    with pytest.raises(ValueError, match="adb_path"):
        adb.wait_for_device(adb_path="")


# -- the one-shot runners (F3-4) -------------------------------------------------------


@pytest.mark.asyncio
async def test_hanging_lookup_child_is_reaped_on_timeout(fake_adb: FakeAdb) -> None:
    """A uid lookup that never answers is killed and reaped at its timeout (F3-4)."""
    fake_adb.dumpsys(PACKAGE, run(touch("looking-up"), hang=True))
    argv = adb._package_uid_argv(fake_adb.path, "emulator-5554", PACKAGE)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match=f"dumpsys package {PACKAGE}"):
        await adb._async_run_adb(argv, timeout=SHORT)

    _assert_reaped(fake_adb)
    assert time.monotonic() - started < TIMEOUT


@pytest.mark.asyncio
async def test_cancel_reaps_child(fake_adb: FakeAdb) -> None:
    """Cancelling a lookup kills and reaps its child before the task ends (F3-4)."""
    fake_adb.dumpsys(PACKAGE, run(touch("looking-up"), hang=True))
    argv = adb._package_uid_argv(fake_adb.path, None, PACKAGE)
    task = asyncio.create_task(adb._async_run_adb(argv, timeout=None))
    await fake_adb.async_wait_for("looking-up", timeout=TIMEOUT)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_reaped(fake_adb)


def test_sync_runner_reaps_child_on_timeout(fake_adb: FakeAdb) -> None:
    fake_adb.pidof(PACKAGE, run(touch("resolving"), hang=True))
    argv = adb._pidof_argv(fake_adb.path, None, PACKAGE)

    with pytest.raises(TimeoutError, match=f"pidof {PACKAGE} did not finish"):
        adb._run_adb(argv, timeout=SHORT)

    _assert_reaped(fake_adb)


# A wait cut into slices notices Ctrl+C within one slice; an unsliced wait only
# once adb ends, which the test forces after TIMEOUT.
INTERRUPT_LIMIT = 2.0


def test_sync_wait_ends_on_ctrl_c_and_reaps_the_child(fake_adb: FakeAdb) -> None:
    """Ctrl+C ends an untimed `wait_for_device` and kills and reaps adb (design 9.2).

    ``_thread.interrupt_main()`` sets the flag Ctrl+C sets, without waking a
    blocked wait on any platform, just as Ctrl+C does not wake a thread join
    on Windows before CPython 3.14. Only a wait cut into slices notices it.
    """
    fake_adb.command("wait-for-device", run(touch("waiting"), hang=True))
    returned = threading.Event()
    interrupted_at: list[float] = []

    def interrupt() -> None:
        try:
            fake_adb.wait_for("waiting", timeout=TIMEOUT)
            interrupted_at.append(time.monotonic())
            _thread.interrupt_main()
        finally:
            if not returned.wait(TIMEOUT):
                # The wait never noticed: end adb, so that the test fails on
                # the elapsed time instead of hanging.
                for child in fake_adb.live_children():
                    child.popen.kill()

    helper = threading.Thread(target=interrupt, daemon=True)
    helper.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            adb.wait_for_device()
        elapsed = time.monotonic() - interrupted_at[0]
    finally:
        returned.set()
        helper.join(TIMEOUT)

    assert elapsed < INTERRUPT_LIMIT
    _assert_reaped(fake_adb)


def test_runner_returns_the_exit_status_and_output(fake_adb: FakeAdb) -> None:
    """The runner reports a failure status; the parsers decide what it means."""
    fake_adb.date(run(out("o\n"), err("e\n"), exit_code=3))
    argv = adb._device_clock_argv(fake_adb.path, None)

    result = adb._run_adb(argv, timeout=TIMEOUT)

    assert result == _AdbResult(argv=argv, returncode=3, stdout="o\n", stderr="e\n")
    _assert_reaped(fake_adb)


def test_runner_decodes_invalid_utf8_with_replacement(fake_adb: FakeAdb) -> None:
    fake_adb.date(run(raw(b"\xff\xfe ok\r\n")))

    result = adb._run_adb(adb._device_clock_argv(fake_adb.path, None), timeout=TIMEOUT)

    assert result.stdout == "�� ok\r\n"


@pytest.mark.asyncio
async def test_async_runner_decodes_invalid_utf8_with_replacement(
    fake_adb: FakeAdb,
) -> None:
    fake_adb.date(run(raw(b"\xff\xfe ok\n"), err("e\n"), exit_code=1))
    argv = adb._device_clock_argv(fake_adb.path, None)

    result = await adb._async_run_adb(argv, timeout=TIMEOUT)

    assert result == _AdbResult(argv=argv, returncode=1, stdout="�� ok\n", stderr="e\n")


def test_runner_reports_a_spawn_failure_as_adb_error(tmp_path: Path) -> None:
    argv = (str(tmp_path / "no-such-adb"), "devices")

    with pytest.raises(AdbError, match="could not start adb devices") as caught:
        adb._run_adb(argv, timeout=TIMEOUT)

    assert caught.value.returncode is None
    assert caught.value.argv == argv
    assert isinstance(caught.value.__cause__, OSError)


@pytest.mark.asyncio
async def test_async_runner_reports_a_spawn_failure_as_adb_error(
    tmp_path: Path,
) -> None:
    argv = (str(tmp_path / "no-such-adb"), "devices")

    with pytest.raises(AdbError, match="could not start adb devices") as caught:
        await adb._async_run_adb(argv, timeout=TIMEOUT)

    assert caught.value.returncode is None
    assert isinstance(caught.value.__cause__, OSError)


_PRINT_ENV = (
    "import os; print(os.environ.get('ANDROID_LOG_TAGS', '<unset>'), "
    "os.environ.get('LOGPYT_TEST_KEPT', '<unset>'))"
)


def test_child_environment_drops_android_log_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host ANDROID_LOG_TAGS would filter the device output (FINDINGS check 12)."""
    monkeypatch.setenv("ANDROID_LOG_TAGS", "*:S")
    monkeypatch.setenv("LOGPYT_TEST_KEPT", "kept")

    result = adb._run_adb([sys.executable, "-c", _PRINT_ENV], timeout=TIMEOUT)

    assert result.stdout.split() == ["<unset>", "kept"]
    assert "ANDROID_LOG_TAGS" not in adb._child_env()


@pytest.mark.asyncio
async def test_async_child_environment_drops_android_log_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANDROID_LOG_TAGS", "*:S")
    monkeypatch.setenv("LOGPYT_TEST_KEPT", "kept")

    result = await adb._async_run_adb(
        [sys.executable, "-c", _PRINT_ENV], timeout=TIMEOUT
    )

    assert result.stdout.split() == ["<unset>", "kept"]


_SESSION_LEADER = "import os; print(os.getsid(0) == os.getpid())"


@posix_only
def test_child_runs_in_its_own_session() -> None:
    """A terminal Ctrl+C reaches the foreground process group, not adb (5.11)."""
    result = adb._run_adb([sys.executable, "-c", _SESSION_LEADER], timeout=TIMEOUT)

    assert result.stdout.strip() == "True"


@posix_only
@pytest.mark.asyncio
async def test_async_child_runs_in_its_own_session() -> None:
    result = await adb._async_run_adb(
        [sys.executable, "-c", _SESSION_LEADER], timeout=TIMEOUT
    )

    assert result.stdout.strip() == "True"


_STDIN_IS_NULL = "import os; print(os.path.samestat(os.fstat(0), os.stat(os.devnull)))"


@contextlib.contextmanager
def _stdin_from_a_pipe() -> Iterator[None]:
    """Point fd 0 at an open pipe, which a child that inherited it would see.

    pytest's output capture already points fd 0 at the null device.
    """
    read_end, write_end = os.pipe()
    saved = os.dup(0)
    try:
        os.dup2(read_end, 0)
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read_end, write_end):
            os.close(fd)


@posix_only
def test_child_stdin_is_the_null_device() -> None:
    """adb never reads the user's terminal (design 5.11)."""
    with _stdin_from_a_pipe():
        result = adb._run_adb([sys.executable, "-c", _STDIN_IS_NULL], timeout=TIMEOUT)

    assert result.stdout.strip() == "True"


@posix_only
@pytest.mark.asyncio
async def test_async_child_stdin_is_the_null_device() -> None:
    with _stdin_from_a_pipe():
        result = await adb._async_run_adb(
            [sys.executable, "-c", _STDIN_IS_NULL], timeout=TIMEOUT
        )

    assert result.stdout.strip() == "True"


@windows_only
def test_windows_child_has_no_window_and_its_own_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adb opens no console window, and a terminal Ctrl+C does not reach it (5.11)."""
    if sys.platform != "win32":  # the marker skips it; this narrows for ty
        return
    expected = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    seen: list[int] = []
    real_popen = subprocess.Popen
    real_exec = asyncio.create_subprocess_exec

    class RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs) -> None:
            seen.append(kwargs.get("creationflags", 0))
            super().__init__(*args, **kwargs)

    async def recording_exec(*args, **kwargs):
        seen.append(kwargs.get("creationflags", 0))
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording_exec)
    argv = [sys.executable, "-c", "pass"]

    adb._run_adb(argv, timeout=TIMEOUT)
    asyncio.run(adb._async_run_adb(argv, timeout=TIMEOUT))

    assert len(seen) == 2
    assert all(flags & expected == expected for flags in seen), seen


# -- a process adb started holds its pipes (F1-1 for the one-shot runners) ------------

# An adb that leaves a process holding its stdout and stderr: it starts a holder
# with both handles passed explicitly (inheritance alone is not reliable on
# Windows, design 9.2), writes the holder's pid to argv[1], prints one line, and
# then exits (argv[2] "exit") or hangs until killed ("hang").
_LEAVE_A_HOLDER = """\
import subprocess, sys, time
holder = subprocess.Popen(
    [sys.executable, "-I", "-c", "import time; time.sleep(60)"], stdout=1, stderr=2
)
with open(sys.argv[1], "w", encoding="utf-8") as pid_file:
    pid_file.write(str(holder.pid))
print("printed before exiting", flush=True)
if sys.argv[2] == "hang":
    time.sleep(60)
"""
# _REAP_TIMEOUT for these tests, and a bound on each call: far below the
# holder's 60 s, which an unbounded wait for EOF would take.
REAP = 0.2
HELD_LIMIT = 5.0
# Long enough for the adb above to start its holder before the kill.
HOLDER_START = 2.0


@pytest.fixture
def quick_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adb, "_REAP_TIMEOUT", REAP)


@contextlib.contextmanager
def _adb_leaving_a_holder(tmp_path: Path, then: str) -> Iterator[list[str]]:
    """The argv of that adb; its holder is killed afterwards."""
    pid_file = tmp_path / "holder.pid"
    # The base interpreter, so that the holder's pid is the process to kill
    # (conftest._FAKE_PYTHON: a Windows venv python.exe is a launcher).
    argv = [_FAKE_PYTHON, "-I", "-c", _LEAVE_A_HOLDER, str(pid_file), then]
    try:
        yield argv
    finally:
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every child the runners spawn, as their spawn returned it."""
    children: list[Any] = []
    spawn, async_spawn = adb._spawn_adb, adb._async_spawn_adb

    def recording_spawn(argv):
        children.append(proc := spawn(argv))
        return proc

    async def recording_async_spawn(argv):
        children.append(proc := await async_spawn(argv))
        return proc

    monkeypatch.setattr(adb, "_spawn_adb", recording_spawn)
    monkeypatch.setattr(adb, "_async_spawn_adb", recording_async_spawn)
    return children


def _assert_reaped_and_closed(proc: subprocess.Popen[bytes]) -> None:
    """adb was reaped, and on POSIX the pipes given up on were closed.

    On Windows communicate()'s reader threads keep them until the holder exits.
    """
    assert proc.returncode is not None
    if sys.platform != "win32":
        for pipe in (proc.stdout, proc.stderr):
            assert pipe is not None
            assert pipe.closed


def _assert_async_reaped_and_closed(proc: asyncio.subprocess.Process) -> None:
    """adb was reaped, and its transport and pipe transports were closed."""
    assert proc.returncode is not None
    transport = getattr(proc, "_transport", None)
    assert isinstance(transport, asyncio.SubprocessTransport)
    assert transport.is_closing()
    for fd in (1, 2):
        pipe = transport.get_pipe_transport(fd)
        assert pipe is not None
        assert pipe.is_closing()


# Enough loop iterations for cancelled readers (and a gather of them) to end
# before the exception handler is checked.
LOOP_SETTLE = 0.1


@contextlib.contextmanager
def _no_loop_errors() -> Iterator[list[dict[str, Any]]]:
    """Collect what the running loop's exception handler gets, and expect nothing.

    An exception of a future nobody retrieved reaches it when the future is
    collected, so the garbage collector runs before the check.
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        yield errors
        gc.collect()
    finally:
        loop.set_exception_handler(previous)
    assert errors == []


@pytest.mark.usefixtures("quick_reap")
@pytest.mark.parametrize("timeout", [None, TIMEOUT])
def test_sync_runner_returns_when_adb_exits_while_its_pipes_are_held(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    spawned: list[Any],
    timeout: float | None,
) -> None:
    """adb's exit ends the wait, not EOF: the pipes get _REAP_TIMEOUT more.

    POSIX returns what adb printed; on Windows communicate() hands over nothing
    before EOF, so the runner reports that instead.
    """
    with (
        _adb_leaving_a_holder(tmp_path, "exit") as argv,
        caplog.at_level(logging.WARNING, logger="logpyt.adb"),
    ):
        started = time.monotonic()
        if sys.platform == "win32":
            with pytest.raises(AdbError, match="output could not be read") as caught:
                adb._run_adb(argv, timeout=timeout)
            assert caught.value.returncode == 0
        else:
            result = adb._run_adb(argv, timeout=timeout)
            assert result.returncode == 0
            assert result.stdout.splitlines() == ["printed before exiting"]
        elapsed = time.monotonic() - started

    assert elapsed < HELD_LIMIT
    assert "exited with status 0, but its output pipes were still open" in caplog.text
    (proc,) = spawned
    _assert_reaped_and_closed(proc)


@pytest.mark.asyncio
@pytest.mark.usefixtures("quick_reap")
@pytest.mark.parametrize("timeout", [None, TIMEOUT])
async def test_async_runner_returns_when_adb_exits_while_its_pipes_are_held(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    spawned: list[Any],
    timeout: float | None,
) -> None:
    """The exit is told by polling returncode (A-14), and the output read is kept.

    The readers given up on end without an unretrieved exception.
    """
    with (
        _adb_leaving_a_holder(tmp_path, "exit") as argv,
        caplog.at_level(logging.WARNING, logger="logpyt.adb"),
        _no_loop_errors(),
    ):
        started = time.monotonic()
        result = await adb._async_run_adb(argv, timeout=timeout)
        elapsed = time.monotonic() - started
        await asyncio.sleep(LOOP_SETTLE)

    assert elapsed < HELD_LIMIT
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["printed before exiting"]
    assert "exited with status 0, but its output pipes were still open" in caplog.text
    (proc,) = spawned
    _assert_async_reaped_and_closed(proc)


@pytest.mark.usefixtures("quick_reap")
def test_sync_runner_timeout_is_bounded_while_its_pipes_are_held(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, spawned: list[Any]
) -> None:
    """The reap of a killed adb waits _REAP_TIMEOUT for the pipes, not longer."""
    with (
        _adb_leaving_a_holder(tmp_path, "hang") as argv,
        caplog.at_level(logging.WARNING, logger="logpyt.adb"),
    ):
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="the adb process was killed"):
            adb._run_adb(argv, timeout=HOLDER_START)
        elapsed = time.monotonic() - started

    assert elapsed < HOLDER_START + HELD_LIMIT
    assert "was killed, but its output pipes were still open" in caplog.text
    (proc,) = spawned
    _assert_reaped_and_closed(proc)


@pytest.mark.asyncio
@pytest.mark.usefixtures("quick_reap")
async def test_async_runner_timeout_is_bounded_while_its_pipes_are_held(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, spawned: list[Any]
) -> None:
    with (
        _adb_leaving_a_holder(tmp_path, "hang") as argv,
        caplog.at_level(logging.WARNING, logger="logpyt.adb"),
        _no_loop_errors(),
    ):
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="the adb process was killed"):
            await adb._async_run_adb(argv, timeout=HOLDER_START)
        elapsed = time.monotonic() - started
        await asyncio.sleep(LOOP_SETTLE)

    assert elapsed < HOLDER_START + HELD_LIMIT
    assert "was killed, but its output pipes were still open" in caplog.text
    (proc,) = spawned
    _assert_async_reaped_and_closed(proc)


def _pending_readers() -> list[asyncio.Task[Any]]:
    """The runner's reader tasks that have not ended."""
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and "_read_into" in repr(task.get_coro())
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("quick_reap")
@pytest.mark.parametrize("then", ["exit", "hang"])
async def test_async_read_failure_closes_the_pipes_while_they_are_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawned: list[Any],
    then: str,
) -> None:
    """A failed read propagates only after adb was reaped and both readers ended.

    The stdout reader fails after its first chunk, while the stderr reader
    still waits for a holder that keeps the pipes open, before ("hang") or
    after ("exit") adb exited.
    """
    read_into = adb._read_into

    async def failing_stdout(stream, chunks):
        if stream is not spawned[0].stdout:
            return await read_into(stream, chunks)
        await stream.read(adb._READ_CHUNK)
        msg = "injected read failure"
        raise OSError(msg)

    monkeypatch.setattr(adb, "_read_into", failing_stdout)
    with _adb_leaving_a_holder(tmp_path, then) as argv, _no_loop_errors():
        started = time.monotonic()
        with pytest.raises(OSError, match="injected read failure"):
            await adb._async_run_adb(argv, timeout=TIMEOUT)
        elapsed = time.monotonic() - started
        await asyncio.sleep(LOOP_SETTLE)
        assert _pending_readers() == []

    assert elapsed < HELD_LIMIT
    (proc,) = spawned
    _assert_async_reaped_and_closed(proc)


# _REAP_TIMEOUT for the interrupted-cleanup tests: long enough that the
# interrupt lands while the reap still waits for the held pipes.
INTERRUPTED_REAP = 3.0


def test_sync_ctrl_c_during_the_reap_still_closes_the_held_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned: list[Any]
) -> None:
    """A second Ctrl+C while a killed adb is reaped ends the wait, and the pipes
    a holder keeps open are still closed (POSIX) and adb reaped.
    """
    monkeypatch.setattr(adb, "_REAP_TIMEOUT", INTERRUPTED_REAP)
    drain = adb._drain
    draining = threading.Event()

    def recording_drain(proc):
        draining.set()
        return drain(proc)

    def interrupt() -> None:
        if draining.wait(HOLDER_START + TIMEOUT):
            _thread.interrupt_main()

    monkeypatch.setattr(adb, "_drain", recording_drain)
    helper = threading.Thread(target=interrupt, daemon=True)
    with _adb_leaving_a_holder(tmp_path, "hang") as argv:
        helper.start()
        started = time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt):
                adb._run_adb(argv, timeout=HOLDER_START)
        finally:
            draining.set()
            helper.join(TIMEOUT)
        elapsed = time.monotonic() - started

    assert elapsed < HOLDER_START + INTERRUPTED_REAP
    (proc,) = spawned
    _assert_reaped_and_closed(proc)


@pytest.mark.asyncio
async def test_async_cancel_during_the_reap_waits_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned: list[Any]
) -> None:
    """A cancellation while a killed adb is reaped propagates only once adb was
    reaped and its held pipes closed.
    """
    monkeypatch.setattr(adb, "_REAP_TIMEOUT", REAP * 5)
    settle = adb._async_settle
    settling = asyncio.Event()

    async def recording_settle(proc, readers):
        settling.set()
        return await settle(proc, readers)

    monkeypatch.setattr(adb, "_async_settle", recording_settle)
    with _adb_leaving_a_holder(tmp_path, "hang") as argv, _no_loop_errors():
        task = asyncio.ensure_future(adb._async_run_adb(argv, timeout=HOLDER_START))
        await asyncio.wait_for(settling.wait(), HOLDER_START + TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        (proc,) = spawned
        _assert_async_reaped_and_closed(proc)
        await asyncio.sleep(LOOP_SETTLE)
        assert _pending_readers() == []


# -- argv builders -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "com.example;reboot",
        "com.example reboot",
        "com.example\n",
        "com.example$(id)",
        "com.example`id`",
        "com.example|sh",
        "com.example&",
        "'com.example'",
        "-e",
        "",
        "1com.example",
        "com..example",
        "com.example.",
        "com.éxample",
        "com.example:",
        "com.example:remote:x",
    ],
)
def test_package_name_check_rejects_what_the_device_shell_would_parse(
    name: str,
) -> None:
    """``adb shell`` hands its words to the device shell (R3, F5-3)."""
    with pytest.raises(ValueError, match="not a package name"):
        adb._pidof_argv("adb", None, name)
    with pytest.raises(ValueError, match="not a package name"):
        adb._package_uid_argv("adb", None, name)


@pytest.mark.parametrize(
    "name",
    ["android", "com.example.app", "com.Example_2.app", "com.example.app:remote"],
)
def test_package_name_check_accepts_package_and_process_names(name: str) -> None:
    assert adb._pidof_argv("adb", "emulator-5554", name) == (
        "adb",
        "-s",
        "emulator-5554",
        "shell",
        "pidof",
        name,
    )


@pytest.mark.parametrize("name", ["android", "com.example.app", "A_b.c9"])
def test_plain_package_check_accepts_package_names(name: str) -> None:
    assert adb._check_package(name) == name


def test_plain_package_check_rejects_a_process_name() -> None:
    """D1: one check for every name attribution takes (Package, the watched
    names, uid_packages, the uid lookup): the grammar, and no process."""
    with pytest.raises(ValueError, match=r"names a process.*'com\.example\.app'$"):
        adb._check_package("com.example.app:remote")
    with pytest.raises(ValueError, match=r"names a process.*'com\.example'; take"):
        adb._check_package("com.example:remote.sub_1", hint="; take")
    with pytest.raises(ValueError, match="not a package name") as raised:
        adb._check_package("com.example.app:")
    assert "process" not in str(raised.value)  # its grammar has no ':<process>'
    with pytest.raises(TypeError, match="must be a str"):
        adb._check_package(42)  # ty: ignore[invalid-argument-type]


def test_package_uid_argv_rejects_a_process_name() -> None:
    with pytest.raises(ValueError, match=r"'com\.example\.app'"):
        adb._package_uid_argv("adb", None, "com.example.app:remote")
    with pytest.raises(TypeError):
        adb._package_uid_argv("adb", None, 42)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("level", LEVELS)
def test_builders_build_the_argv_the_recordings_ran(level: int) -> None:
    """Each builder sends the device the command that was recorded on it."""
    directory = level_dir(level)
    lookup = step(load(directory, "C03"), "dumpsys_package")
    serial = lookup["argv"][1]
    package = lookup["argv"][-1].split()[-1]
    built = adb._package_uid_argv("adb", serial, package)
    assert built[1:3] == ("-s", serial)
    assert _shell_command(built) == lookup["argv"][-1]

    pidof = step(load(directory, "R3"), "unknown")
    assert adb._pidof_argv("adb", pidof["argv"][1], pidof["argv"][-1])[1:] == tuple(
        pidof["argv"]
    )

    state = step(load(directory, "R4"), "get_state")
    assert adb._get_state_argv("adb", state["argv"][1])[1:] == tuple(state["argv"])

    clock = step(load(directory, "C10"), "date_s")
    assert _shell_command(adb._device_clock_argv("adb", serial)) == clock["argv"][-1]

    log = step(load(directory, "C11"), "log_t")
    marker = log["argv"][-1].split()[-1]
    assert (
        _shell_command(adb._log_marker_argv("adb", serial, marker)) == log["argv"][-1]
    )


@pytest.mark.parametrize("marker", ["", "-x", "a b", "a;b", "flush-é", "a\nb", "a_b"])
def test_log_marker_argv_rejects_an_unsafe_marker(marker: str) -> None:
    with pytest.raises(ValueError, match="marker"):
        adb._log_marker_argv("adb", None, marker)


def test_new_marker_is_one_shell_safe_token() -> None:
    start = adb._new_marker("start")
    flush = adb._new_marker("flush")
    assert start.startswith("start-")
    assert flush.startswith("flush-")
    assert len(start.removeprefix("start-")) == 32
    assert start != adb._new_marker("start")
    assert adb._log_marker_argv("adb", None, flush)[-3:] == ("-t", "logpyt", flush)
    with pytest.raises(ValueError, match="kind"):
        adb._new_marker("stop")  # ty: ignore[invalid-argument-type]


# -- output parsers, read against the device recordings --------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_pidof_parser_reads_the_recorded_outputs(level: int) -> None:
    directory = level_dir(level)
    r3 = load(directory, "R3")
    two = step(r3, "two_names_one_arg")
    pids = adb._parse_pidof(_recorded(two))
    assert len(pids) == 2
    assert pids == tuple(int(token) for token in two["stdout"][0].split())
    assert adb._parse_pidof(_recorded(step(r3, "unknown"))) == ()
    app = step(load(directory, "C07"), "anr_pidof")
    assert adb._parse_pidof(_recorded(app)) == (int(app["stdout"][0]),)


@pytest.mark.parametrize("level", LEVELS)
def test_pidof_parser_rejects_the_recorded_injection_output(level: int) -> None:
    """What the injected ``echo`` printed is no pid (R3)."""
    injected = step(load(level_dir(level), "R3"), "metachar")

    with pytest.raises(AdbError, match="INJECTED_VIA_SEMICOLON"):
        adb._parse_pidof(_recorded(injected))


def _server_start_banner() -> str:
    """What a client that started the adb server printed on stderr (C09)."""
    rec = load(facts.FIXTURES / "host", "C09")
    banner = rec["facts"]["result"]["runs"][0]["client_stderr"]
    assert banner
    assert all(line.startswith("* daemon ") for line in banner)
    return "".join(f"{line}\n" for line in banner)


def test_parsers_ignore_the_recorded_server_start_on_stderr() -> None:
    """A client that had to start the server still succeeded (FINDINGS check 9)."""
    banner = _server_start_banner()

    assert adb._parse_pidof(_result(returncode=1, stderr=banner)) == ()
    assert adb._parse_pidof(_result(stdout="42\n", stderr=banner)) == (42,)
    adb._parse_log_marker(_result(stderr=banner))
    with pytest.raises(AdbError, match="adb shell log failed"):
        adb._parse_log_marker(_result(stderr=f"{banner}log: unexpected\n"))
    with pytest.raises(AdbError, match="cannot connect to daemon"):
        adb._parse_pidof(
            _result(
                returncode=1,
                stderr="* daemon still not running\nerror: cannot connect to daemon\n",
            )
        )


def test_pidof_parser_raises_on_an_adb_failure() -> None:
    with pytest.raises(AdbError, match="device offline"):
        adb._parse_pidof(_result(returncode=1, stderr="adb: device offline\n"))


@pytest.mark.parametrize("level", LEVELS)
def test_package_uid_parser_reads_the_recorded_dumpsys_output(level: int) -> None:
    """``userId=`` on API 24 and ``appId=`` on 36 (FINDINGS check 3, A.8)."""
    rec = load(level_dir(level), "C03")
    lookup = step(rec, "dumpsys_package")
    package = lookup["argv"][-1].split()[-1]
    assert adb._parse_package_uid(_recorded(lookup), package) == 1000

    # Every app uid seen in the log maps to its packages in the full listing.
    listing = _recorded(step(rec, "dumpsys_package_packages"))
    mapping = rec["facts"]["app_uid_tokens_to_packages"]
    assert mapping
    for uid, names in mapping.items():
        for name in names:
            assert adb._parse_package_uid(listing, name) == int(uid), name


def test_package_uid_parser_matches_the_name_exactly() -> None:
    """Unlike ``pm list packages -U``, a longer name with that prefix is no match."""
    stdout = (
        "Packages:\n"
        "  Package [com.android.settings.auto_generated_rro_product__] (abc):\n"
        "    appId=10010\n"
        "  Package [com.android.settings] (def):\n"
        "    versionCode=36 minSdk=36 targetSdk=36\n"
        "    appId=1000\n"
    )
    assert (
        adb._parse_package_uid(_result(stdout=stdout), "com.android.settings") == 1000
    )


def test_package_uid_parser_reads_only_the_packages_own_record() -> None:
    first_without_uid = (
        "  Package [com.example.app] (a):\n"
        "    versionCode=1\n"
        "  Package [com.example.app] (b):\n"
        "    userId=10123 gids=[3003]\n"
    )
    assert adb._parse_package_uid(_result(stdout=first_without_uid), PACKAGE) == 10123
    other_section = (
        "  Package [com.example.app] (a):\n"
        "    versionCode=1\n"
        "Shared users:\n"
        "  SharedUser [android.uid.system] (c):\n"
        "    userId=1000\n"
    )
    assert adb._parse_package_uid(_result(stdout=other_section), PACKAGE) is None


def test_package_uid_parser_returns_none_for_an_absent_package() -> None:
    assert adb._parse_package_uid(_result(stdout=""), PACKAGE) is None
    other = "  Package [com.other] (a):\n    userId=10001\n"
    assert adb._parse_package_uid(_result(stdout=other), PACKAGE) is None


def test_package_uid_parser_raises_on_a_dumpsys_failure() -> None:
    with pytest.raises(AdbError, match="device offline"):
        adb._parse_package_uid(
            _result(returncode=1, stderr="adb: device offline\n"), PACKAGE
        )


@pytest.mark.parametrize("level", LEVELS)
def test_get_state_parser_reads_the_recorded_states(level: int) -> None:
    """An absent serial and an unplugged device (FINDINGS check 4, R4)."""
    absent = step(load(level_dir(level), "R4"), "get_state")
    assert adb._parse_get_state(_recorded(absent)) is None
    unplugged = step(load(level_dir(level), "C04"), "get_state_after_unplug")
    assert adb._parse_get_state(_recorded(unplugged)) == "offline"


@pytest.mark.parametrize(
    ("result", "state"),
    [
        (_result(stdout="device\n"), "device"),
        (_result(stdout="recovery\r\n"), "recovery"),
        (
            _result(
                returncode=1,
                stderr="error: device unauthorized.\n"
                "This adb server's $ADB_VENDOR_KEYS is not set\n",
            ),
            "unauthorized",
        ),
        (
            _result(returncode=1, stderr="error: device still authorizing\n"),
            "authorizing",
        ),
        (_result(returncode=1, stderr="error: no devices/emulators found\n"), None),
    ],
)
def test_get_state_parser_names_the_state(
    result: _AdbResult, state: str | None
) -> None:
    assert adb._parse_get_state(result) == state


@pytest.mark.parametrize(
    "result",
    [
        _result(returncode=1, stderr="error: more than one device/emulator\n"),
        _result(returncode=1, stderr="adb: cannot connect to daemon at tcp:5037\n"),
        _result(stdout=""),
        _result(stdout="device offline\n"),
    ],
)
def test_get_state_parser_raises_on_an_unknown_answer(result: _AdbResult) -> None:
    with pytest.raises(AdbError, match="get-state"):
        adb._parse_get_state(result)


@pytest.mark.parametrize("level", LEVELS)
def test_device_clock_parser_reads_the_recorded_clock(level: int) -> None:
    rec = load(level_dir(level), "C10")
    seconds = adb._parse_device_clock(_recorded(step(rec, "date_s")))
    assert seconds == int(rec["facts"]["date_s"])


@pytest.mark.parametrize("level", LEVELS)
def test_device_clock_parser_rejects_fractions_and_errors(level: int) -> None:
    """``+%s.%N`` prints ``<s>.N`` on API 24, so the builder asks for whole seconds."""
    directory = level_dir(level)
    with pytest.raises(AdbError, match="epoch seconds"):
        adb._parse_device_clock(_recorded(step(load(directory, "C10"), "date_s_N")))
    unplugged = step(load(directory, "C04"), "shell_date_after_unplug")
    with pytest.raises(AdbError, match="device offline"):
        adb._parse_device_clock(_recorded(unplugged))
    absent = step(load(directory, "R4"), "shell_date")
    with pytest.raises(AdbError, match="not found"):
        adb._parse_device_clock(_recorded(absent))


@pytest.mark.parametrize("level", LEVELS)
def test_marker_matches_the_recorded_log_t_line(level: int) -> None:
    """The echo of ``log -t logpyt <marker>`` matches after trailing spaces go
    (API 24 appends one: FINDINGS check 11, A.7).
    """
    rec = load(level_dir(level), "C11")
    marker = step(rec, "log_t")["argv"][-1].split()[-1]
    main = [facts.u1a(line) for line in body(step(rec, "buffer_main")["stdout"])]

    echoed = [m for m in main if adb._is_marker(m["tag"], m["msg"], marker)]

    assert len(echoed) == 1
    assert echoed[0]["msg"].endswith(" ") is (level == 24)
    assert not adb._is_marker("logpyt", f"{marker}0", marker)
    assert not adb._is_marker("sh", marker, marker)


def test_log_marker_parser() -> None:
    adb._parse_log_marker(_result())
    with pytest.raises(AdbError, match="not found"):
        adb._parse_log_marker(
            _result(returncode=127, stderr="/system/bin/sh: log: not found\n")
        )
    with pytest.raises(AdbError, match="adb shell log failed"):
        adb._parse_log_marker(_result(stdout="usage: log [-p priorityChar] ...\n"))
