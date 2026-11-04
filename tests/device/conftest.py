"""Device tier: runs only against a live device or emulator (design 9.4).

Run with ``LOGPYT_DEVICE=<serial> pytest -m device tests/device``; also set
``LOGPYT_ADB`` to the adb client when it is not on PATH, and optionally
``LOGPYT_DEVICE_API`` (24 or 36) to assert which level the serial is.

Every test collected under this directory gets the ``device`` marker, and an
autouse fixture skips it unless ``LOGPYT_DEVICE`` is set, and skips it with the
reason when 127.0.0.1:5037 does not accept a TCP connection, whether or not
the test uses ``probe_ctx``: the device tier talks to an adb server reached
through a tunnel and never lets an adb client start a server of its own
(``tools/device_probe.py`` also checks the port before every adb call and fails
with ``TunnelDownError`` if the tunnel drops mid-run).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.device_facts import LEVELS
from tools import device_probe

DEVICE_ENV = "LOGPYT_DEVICE"
API_ENV = "LOGPYT_DEVICE_API"
ADB_ENV = "LOGPYT_ADB"
HERE = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test under this directory ``device`` (before ``-m`` selects)."""
    for item in items:
        if item.path.resolve().is_relative_to(HERE):
            item.add_marker(pytest.mark.device)


def _serial_or_skip() -> str:
    serial = os.environ.get(DEVICE_ENV, "").strip()
    if not serial:
        pytest.skip(f"{DEVICE_ENV} is not set (the device tier needs a live device)")
    host, port = device_probe.SERVER
    if not device_probe.server_reachable():
        pytest.skip(
            f"{host}:{port} does not accept connections: the tunnel to the adb server "
            "is down, and the device tier never starts an adb server"
        )
    return serial


@pytest.fixture(autouse=True)
def device_tier() -> str:
    """Before every test here: the serial, or a skip (no device, or no tunnel)."""
    return _serial_or_skip()


def _wanted_api() -> int | None:
    wanted = os.environ.get(API_ENV, "").strip()
    if not wanted:
        return None
    if wanted not in {str(level) for level in LEVELS}:
        pytest.fail(
            f"{API_ENV}={wanted!r}: expected one of {', '.join(map(str, LEVELS))} "
            "(the recorded levels; leave it unset for any other device)"
        )
    return int(wanted)


@pytest.fixture(scope="session")
def probe_ctx() -> device_probe.Context:
    """The probe context for the device named by ``LOGPYT_DEVICE``, or a skip."""
    serial = _serial_or_skip()
    wanted = _wanted_api()
    adb = os.environ.get(ADB_ENV, "").strip() or shutil.which("adb")
    if not adb:
        pytest.skip(f"no adb client: set {ADB_ENV} or put adb on PATH")
    try:
        ctx = device_probe.Context.connect(device_probe.Adb(adb, serial))
    except device_probe.DeviceNotReadyError as exc:
        pytest.fail(f"{DEVICE_ENV}: {exc}")
    if wanted is not None and wanted != ctx.api:
        pytest.fail(f"{serial} is API {ctx.api}, but {API_ENV}={wanted}")
    return ctx
