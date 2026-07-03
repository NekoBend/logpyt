"""Tests for utility functions."""

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

from logpyt.utils import (
    _CREATE_NO_WINDOW,
    adb_connect,
    async_adb_connect,
    async_wait_for_device,
    enable_debug,
    extract_json,
    list_devices,
    resolve_adb,
    wait_for_device,
)


def test_resolve_adb_in_path(mocker) -> None:
    """Test resolving adb when it's in PATH."""
    resolve_adb.cache_clear()
    mocker.patch("shutil.which", return_value="/usr/bin/adb")
    mocker.patch("pathlib.Path.is_file", return_value=True)
    mocker.patch("os.access", return_value=True)

    path = resolve_adb()
    assert path == "/usr/bin/adb"


def test_resolve_adb_in_android_home(mocker) -> None:
    """Test resolving adb from ANDROID_HOME."""
    resolve_adb.cache_clear()
    mocker.patch("shutil.which", return_value=None)
    mocker.patch.dict(os.environ, {"ANDROID_HOME": "/opt/android-sdk"})

    # Mock the executable-file check and access permission.
    mocker.patch("pathlib.Path.is_file", return_value=True)
    mocker.patch("os.access", return_value=True)

    path = resolve_adb()
    expected_path = str(Path("/opt/android-sdk") / "platform-tools" / "adb")
    assert path == expected_path


def test_resolve_adb_not_found(mocker) -> None:
    """Test resolving adb when not found."""
    resolve_adb.cache_clear()
    mocker.patch("shutil.which", return_value=None)
    mocker.patch.dict(os.environ, {}, clear=True)

    with pytest.raises(FileNotFoundError):
        resolve_adb()


def test_resolve_adb_path_candidate_must_be_executable_file(mocker) -> None:
    """PATH candidate that is a file but not executable must be rejected.

    The implementation validates candidates with ``Path(path).is_file()`` and
    ``os.access(path, os.X_OK)``. Here the file exists (is_file -> True) but is
    not executable (os.access -> False), so the FileNotFoundError must be driven
    specifically by the X_OK check, not by a missing file.
    """
    resolve_adb.cache_clear()
    mocker.patch("shutil.which", return_value="/fake/bin/adb")
    # File exists...
    mocker.patch("pathlib.Path.is_file", return_value=True)
    # ...but is not executable. This is the check that must reject it.
    mock_access = mocker.patch("os.access", return_value=False)
    mocker.patch.dict(os.environ, {}, clear=True)

    with pytest.raises(FileNotFoundError):
        resolve_adb()

    # Confirm the executable check was actually exercised (X_OK on the candidate).
    mock_access.assert_any_call("/fake/bin/adb", os.X_OK)


def test_enable_debug_configures_logger_without_duplicate_handlers() -> None:
    """enable_debug sets the level and attaches exactly one handler (idempotent)."""
    logger = logging.getLogger("logpyt")
    # Snapshot and clear any pre-existing state so this test is deterministic.
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    logger.handlers.clear()

    try:
        enable_debug("DEBUG")
        assert logger.level == logging.DEBUG
        assert len(logger.handlers) == 1

        # Calling again must NOT add a duplicate handler (the `if not
        # logger.handlers` guard) and must still apply the requested level.
        enable_debug("INFO")
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
    finally:
        # Restore original logger state so this does not leak into other tests.
        logger.handlers.clear()
        logger.handlers.extend(saved_handlers)
        logger.setLevel(saved_level)


def test_list_devices_success(mocker) -> None:
    """Test listing devices successfully."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.stdout = """List of devices attached
emulator-5554 device product:sdk_gphone model:sdk_gphone device:generic transport_id:1
1234567890abc device product:myphone model:Pixel_5 device:pixel5 transport_id:2
"""

    devices = list_devices()
    assert len(devices) == 2

    assert devices[0].get("id") == "emulator-5554"
    assert devices[0].get("type") == "emulator"
    assert devices[0].get("state") == "device"
    # Parsed key:value properties must be surfaced, not just id/state/type.
    assert devices[0].get("product") == "sdk_gphone"
    assert devices[0].get("model") == "sdk_gphone"
    assert devices[0].get("device") == "generic"
    assert devices[0].get("transport_id") == "1"

    assert devices[1].get("id") == "1234567890abc"
    assert devices[1].get("type") == "usb"
    assert devices[1].get("product") == "myphone"
    assert devices[1].get("model") == "Pixel_5"
    assert devices[1].get("device") == "pixel5"
    assert devices[1].get("transport_id") == "2"


def test_list_devices_property_value_with_space(mocker) -> None:
    """A property value containing a space must be captured wholly.

    The prop regex ``(\\w+):((?:(?!\\s\\w+:).)*)`` uses a negative lookahead so a
    value only terminates when the next `` key:`` pair begins. This locks that
    behavior: ``model`` here is ``Pixel 5`` (with an embedded space) and does not
    swallow the following ``device:`` pair.
    """
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.stdout = (
        "List of devices attached\n"
        "1234567890abc device product:my_phone model:Pixel 5 "
        "device:pixel5 transport_id:3\n"
    )

    devices = list_devices()
    assert len(devices) == 1

    assert devices[0].get("product") == "my_phone"
    # The space-containing value is captured whole, not truncated at the space.
    assert devices[0].get("model") == "Pixel 5"
    # And the lookahead correctly stops before the next key, so `device` is clean.
    assert devices[0].get("device") == "pixel5"
    assert devices[0].get("transport_id") == "3"


def test_list_devices_filter(mocker) -> None:
    """Test listing devices with filter."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.stdout = """List of devices attached
emulator-5554 device
1234567890abc device
"""

    # Filter for emulator
    emulators = list_devices(device_type="emulator")
    assert len(emulators) == 1
    assert emulators[0].get("id") == "emulator-5554"

    # Filter for usb
    usb_devices = list_devices(device_type="usb")
    assert len(usb_devices) == 1
    assert usb_devices[0].get("id") == "1234567890abc"


def test_list_devices_error(mocker) -> None:
    """Test error handling in list_devices."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_run = mocker.patch("subprocess.run")
    mock_run.side_effect = subprocess.CalledProcessError(1, ["adb"], stderr="error")

    with pytest.raises(RuntimeError, match="Failed to run adb devices"):
        list_devices()


def test_wait_for_device_success(mocker) -> None:
    """Test waiting for device successfully."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")
    mock_run = mocker.patch("subprocess.run")

    wait_for_device(serial="emulator-5554", timeout=10.0)

    mock_run.assert_called_once_with(
        ["adb", "-s", "emulator-5554", "wait-for-device"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10.0,
        creationflags=_CREATE_NO_WINDOW,
    )


def test_wait_for_device_timeout(mocker) -> None:
    """Test waiting for device timeout."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")
    mock_run = mocker.patch("subprocess.run")
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["adb"], timeout=5.0)

    with pytest.raises(TimeoutError, match="Timed out waiting for device"):
        wait_for_device(timeout=5.0)


def test_wait_for_device_error(mocker) -> None:
    """Test waiting for device error."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")
    mock_run = mocker.patch("subprocess.run")
    mock_run.side_effect = subprocess.CalledProcessError(1, ["adb"], stderr="error")

    with pytest.raises(RuntimeError, match="Failed to wait for device"):
        wait_for_device()


@pytest.mark.asyncio
async def test_async_wait_for_device_success(mocker) -> None:
    """Test async waiting for device successfully."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_process = mocker.AsyncMock()
    mock_process.wait.return_value = None
    mock_process.returncode = 0

    mock_create_subprocess = mocker.patch(
        "asyncio.create_subprocess_exec", return_value=mock_process
    )

    await async_wait_for_device(serial="emulator-5554", timeout=10.0)

    mock_create_subprocess.assert_called_once_with(
        "adb",
        "-s",
        "emulator-5554",
        "wait-for-device",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
    )
    # In the implementation, if timeout is provided, asyncio.wait_for is used.
    # We should verify that wait() was called (either directly or via wait_for).
    # Since we are not mocking wait_for here, it will call process.wait().
    mock_process.wait.assert_called_once()


@pytest.mark.asyncio
async def test_async_wait_for_device_timeout(mocker) -> None:
    """Test async waiting for device timeout."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_process = mocker.AsyncMock()
    mock_process.kill = mocker.Mock()
    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

    # Make process.wait() sleep longer than timeout
    async def delayed_wait():
        await asyncio.sleep(0.2)

    mock_process.wait.side_effect = delayed_wait

    with pytest.raises(TimeoutError, match="Timed out waiting for device"):
        await async_wait_for_device(timeout=0.1)

    mock_process.kill.assert_called_once()


@pytest.mark.asyncio
async def test_async_wait_for_device_error(mocker) -> None:
    """Test async waiting for device error."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_process = mocker.AsyncMock()
    mock_process.wait.return_value = None
    mock_process.returncode = 1

    # Configure stderr as a Mock with an async read method
    mock_process.stderr = mocker.Mock()
    mock_process.stderr.read = mocker.AsyncMock(return_value=b"error message")

    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

    with pytest.raises(RuntimeError, match="Failed to wait for device: error message"):
        await async_wait_for_device()


def test_adb_connect_success(mocker) -> None:
    """Test adb_connect success."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.stdout = "connected to 192.168.1.5:5555"
    mock_run.return_value.stderr = ""
    mock_run.return_value.returncode = 0

    adb_connect("192.168.1.5:5555")

    mock_run.assert_called_once_with(
        ["adb", "connect", "192.168.1.5:5555"],
        capture_output=True,
        text=True,
        timeout=None,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )


def test_adb_connect_already_connected(mocker) -> None:
    """Test adb_connect when already connected."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.stdout = "already connected to 192.168.1.5:5555"
    mock_run.return_value.stderr = ""
    mock_run.return_value.returncode = 0

    adb_connect("192.168.1.5:5555")


def test_adb_connect_failure(mocker) -> None:
    """Test adb_connect failure."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.stdout = (
        "unable to connect to 192.168.1.5:5555: Connection refused"
    )
    mock_run.return_value.stderr = ""
    mock_run.return_value.returncode = 0

    with pytest.raises(RuntimeError, match="Failed to connect"):
        adb_connect("192.168.1.5:5555")


def test_adb_connect_timeout(mocker) -> None:
    """Test adb_connect timeout."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")
    mock_run = mocker.patch("subprocess.run")
    mock_run.side_effect = subprocess.TimeoutExpired(
        cmd=["adb", "connect"], timeout=5.0
    )

    with pytest.raises(TimeoutError, match="Timed out connecting"):
        adb_connect("192.168.1.5:5555", timeout=5.0)


@pytest.mark.asyncio
async def test_async_adb_connect_success(mocker) -> None:
    """Test async_adb_connect success."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_process = mocker.AsyncMock()
    mock_process.wait.return_value = None
    mock_process.communicate.return_value = (b"connected to 192.168.1.5:5555", b"")
    mock_process.returncode = 0

    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

    await async_adb_connect("192.168.1.5:5555")


@pytest.mark.asyncio
async def test_async_adb_connect_failure(mocker) -> None:
    """Test async_adb_connect failure."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_process = mocker.AsyncMock()
    mock_process.wait.return_value = None
    mock_process.communicate.return_value = (
        b"failed to connect to '192.168.1.5:5555': Connection refused",
        b"",
    )
    mock_process.returncode = 0

    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

    with pytest.raises(RuntimeError, match="Failed to connect"):
        await async_adb_connect("192.168.1.5:5555")


@pytest.mark.asyncio
async def test_async_adb_connect_timeout(mocker) -> None:
    """Test async_adb_connect timeout."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_process = mocker.AsyncMock()
    mock_process.kill = mocker.Mock()
    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

    # Make process.wait() sleep longer than timeout
    async def delayed_wait():
        await asyncio.sleep(0.2)

    mock_process.wait.side_effect = delayed_wait

    with pytest.raises(TimeoutError, match="Timed out connecting"):
        await async_adb_connect("192.168.1.5:5555", timeout=0.1)

    mock_process.kill.assert_called_once()


@pytest.mark.asyncio
async def test_async_wait_for_device_error_decodes_invalid_utf8(mocker) -> None:
    """Non-UTF-8 stderr must surface as RuntimeError, not UnicodeDecodeError."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_process = mocker.AsyncMock()
    mock_process.wait.return_value = None
    mock_process.returncode = 1
    mock_process.stderr = mocker.Mock()
    mock_process.stderr.read = mocker.AsyncMock(return_value=b"\xff\xfe bad bytes")

    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

    with pytest.raises(RuntimeError, match="Failed to wait for device"):
        await async_wait_for_device()


@pytest.mark.asyncio
async def test_async_adb_connect_decodes_invalid_utf8(mocker) -> None:
    """Non-UTF-8 connect output must surface as RuntimeError, not UnicodeDecodeError."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")

    mock_process = mocker.AsyncMock()
    mock_process.wait.return_value = None
    mock_process.communicate.return_value = (b"\xff\xfe", b"failed \xff")
    mock_process.returncode = 1

    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_process)

    with pytest.raises(RuntimeError, match="Failed to connect"):
        await async_adb_connect("192.168.1.5:5555")


def test_extract_json_simple_object() -> None:
    """Test extracting a simple JSON object."""
    text = '{"key": "value"}'
    assert extract_json(text) == {"key": "value"}


def test_extract_json_simple_array() -> None:
    """Test extracting a simple JSON array."""
    text = "[1, 2, 3]"
    assert extract_json(text) == [1, 2, 3]


def test_extract_json_embedded() -> None:
    """Test extracting JSON embedded in text."""
    text = 'Prefix: {"key": "value"} Suffix'
    assert extract_json(text) == {"key": "value"}


def test_extract_json_embedded_array() -> None:
    """Test extracting JSON array embedded in text."""
    text = "Prefix: [1, 2, 3] Suffix"
    assert extract_json(text) == [1, 2, 3]


def test_extract_json_nested() -> None:
    """Test extracting nested JSON."""
    text = '{"a": {"b": [1, 2]}}'
    assert extract_json(text) == {"a": {"b": [1, 2]}}


def test_extract_json_multiple_candidates() -> None:
    """Test extracting when multiple candidates exist (picks first valid)."""
    # First one is valid
    text = '{"a": 1} and {"b": 2}'
    assert extract_json(text) == {"a": 1}


def test_extract_json_first_invalid_second_valid() -> None:
    """Test extracting when first candidate is invalid."""
    # {invalid} is not valid JSON, should skip and find {"valid": 1}
    text = 'Start {invalid} Middle {"valid": 1} End'
    assert extract_json(text) == {"valid": 1}


def test_extract_json_brackets_in_text() -> None:
    """Test extracting when text contains brackets but no JSON."""
    text = "This is [not json] and {neither is this}"
    assert extract_json(text) is None


def test_extract_json_no_json() -> None:
    """Test extracting from text with no JSON."""
    text = "Just plain text"
    assert extract_json(text) is None


def test_extract_json_deeply_nested_does_not_raise() -> None:
    """Pathologically nested brackets must not escape as RecursionError.

    Untrusted device output like '[' * N drives the JSON decoder past its
    recursion limit; extract_json must swallow it and return None rather than
    crashing a caller of LogEntry.json_payload.
    """
    assert extract_json("[" * 6000) is None
    assert extract_json("{" * 6000) is None


def test_extract_json_incomplete() -> None:
    """Test extracting incomplete JSON."""
    text = '{"key": "val"'
    assert extract_json(text) is None


def test_extract_json_scan_guard_caps_decode_attempts(mocker) -> None:
    """Ensure pathological candidates are bounded by internal attempt guard."""
    from logpyt import utils

    bad_text = "{" * (utils._EXTRACT_JSON_MAX_CANDIDATES + 100)

    def _always_fail_decode(doc: str, idx: int = 0):
        raise json.JSONDecodeError("invalid", doc, idx)

    mock_decode = mocker.patch(
        "logpyt.utils.json.JSONDecoder.raw_decode",
        side_effect=_always_fail_decode,
    )

    assert extract_json(bad_text) is None
    assert mock_decode.call_count == utils._EXTRACT_JSON_MAX_CANDIDATES


def test_extract_json_scan_guard_limits_search_range() -> None:
    """Ensure JSON beyond scan limit is not searched to avoid worst-case scans."""
    from logpyt import utils

    text = "x" * utils._EXTRACT_JSON_MAX_SCAN_CHARS + '{"after_limit": true}'

    assert extract_json(text) is None


def test_list_devices_timeout(mocker) -> None:
    """Test timeout in list_devices."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")
    mock_run = mocker.patch("subprocess.run")
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["adb"], timeout=10.0)

    with pytest.raises(TimeoutError, match="Timed out running adb devices"):
        list_devices()


def test_list_devices_custom_timeout(mocker) -> None:
    """Test list_devices with custom timeout."""
    mocker.patch("logpyt.utils.resolve_adb", return_value="adb")
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.stdout = ""

    list_devices(timeout=5.0)

    mock_run.assert_called_once_with(
        ["adb", "devices", "-l"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5.0,
        creationflags=_CREATE_NO_WINDOW,
    )
