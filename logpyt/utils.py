"""Utility functions for logpyt.

This module provides utilities for ADB interaction and logging configuration.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict


# Type definitions
class DeviceInfo(TypedDict, total=False):
    """Device information structure."""

    id: str
    state: str
    type: str  # 'emulator' or 'usb'
    product: str
    model: str
    device: str
    transport_id: str


DeviceType = Literal["usb", "emulator", "device"]

# Safeguards for pathological extract_json inputs.
_EXTRACT_JSON_MAX_SCAN_CHARS = 1_000_000
_EXTRACT_JSON_MAX_CANDIDATES = 4_096


@functools.lru_cache(maxsize=1)
def resolve_adb() -> str:
    """Resolve the path to the ADB executable.

    Searches for 'adb' or 'adb.exe' in the following order:
    1. PATH environment variable
    2. ANDROID_HOME/platform-tools
    3. ANDROID_SDK_ROOT/platform-tools

    On WSL, 'adb.exe' is also searched to support Windows ADB server connection.

    Returns:
        Path to the ADB executable.

    Raises:
        FileNotFoundError: If ADB executable cannot be found.

    """
    # Candidates to search for
    candidates = ["adb"]

    is_wsl = False
    if sys.platform == "linux":
        try:
            if "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower():
                is_wsl = True
        except OSError:
            pass

    if sys.platform == "win32" or is_wsl:
        # Windows or WSL
        candidates.append("adb.exe")

    # 1. Search in PATH
    for candidate in candidates:
        path = shutil.which(candidate)
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return path

    # 2. Search in Environment Variables
    env_vars = ["ANDROID_HOME", "ANDROID_SDK_ROOT"]
    for var in env_vars:
        root = os.environ.get(var)
        if root:
            for candidate in candidates:
                candidate_path = Path(root) / "platform-tools" / candidate
                if candidate_path.is_file() and os.access(candidate_path, os.X_OK):
                    return str(candidate_path)

    msg = "Could not find 'adb' or 'adb.exe' in PATH or Android SDK directories."
    raise FileNotFoundError(msg)


def list_devices(  # noqa: PLR0912
    device_type: DeviceType | None = None,
    timeout: float = 10.0,
) -> list[DeviceInfo]:
    """List connected ADB devices.

    Args:
        device_type: Optional filter for device type.
            - 'usb': Physical devices (not starting with 'emulator-')
            - 'emulator': Emulators (starting with 'emulator-')
            - 'device': Devices in 'device' state (ready)
        timeout: Timeout in seconds for the ADB command. Defaults to 10.0.

    Returns:
        List of device information dictionaries.

    Raises:
        RuntimeError: If ADB command fails.
        TimeoutError: If ADB command times out.

    """
    adb_path = resolve_adb()

    try:
        # Run adb devices -l
        result = subprocess.run(
            [adb_path, "devices", "-l"],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as e:
        msg = f"Failed to run adb devices: {e.stderr}"
        raise RuntimeError(msg) from e
    except subprocess.TimeoutExpired as e:
        msg = f"Timed out running adb devices after {timeout}s"
        raise TimeoutError(msg) from e

    devices: list[DeviceInfo] = []

    # Parse output
    # Example: "emulator-5554 device product:sdk_gphone_x86_64 ..."
    lines = result.stdout.strip().splitlines()

    # Skip the first line "List of devices attached"
    if not lines:
        return []

    if lines[0].startswith("List of devices attached"):
        lines = lines[1:]

    # Regex to parse the line: serial state [properties]
    # properties are key:value pairs, potentially with spaces in value
    line_pattern = re.compile(r"^(\S+)\s+(\S+)(?:\s+(.*))?$")
    # Regex to parse properties: key:value where value can contain spaces
    # It matches a key, a colon, and then characters that are NOT followed by " key:"
    prop_pattern = re.compile(r"(\w+):((?:(?!\s\w+:).)*)")

    for line in lines:
        if not line.strip():
            continue

        match = line_pattern.match(line)
        if not match:
            continue

        serial, state, props_str = match.groups()

        info: DeviceInfo = {
            "id": serial,
            "state": state,
            "type": "emulator" if serial.startswith("emulator-") else "usb",
        }

        # Parse key:value pairs if present
        if props_str:
            for key, value in prop_pattern.findall(props_str):
                if key in {"product", "model", "device", "transport_id"}:
                    info[key] = value.strip()

        # Apply filter
        if device_type:
            if device_type == "emulator" and info["type"] != "emulator":
                continue
            if device_type == "usb" and info["type"] != "usb":
                continue
            if device_type == "device" and info["state"] != "device":
                continue

        devices.append(info)

    return devices


def enable_debug(level: str | int = "INFO") -> None:
    """Enable debug logging for logpyt.

    Note: This configures the 'logpyt' logger. It does not modify the root logger,
    but if the root logger is not configured, this will add a StreamHandler to
    the 'logpyt' logger which might result in duplicate logs if the root logger
    is later configured with a handler.

    Args:
        level: Logging level (e.g., "DEBUG", "INFO", logging.DEBUG).

    """
    logger = logging.getLogger("logpyt")
    logger.setLevel(level)

    # Add handler if not present
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def wait_for_device(serial: str | None = None, timeout: float | None = None) -> None:
    """Wait for device to be ready.

    Args:
        serial: Optional device serial number.
        timeout: Timeout in seconds.

    Raises:
        TimeoutError: If timeout expires.
        RuntimeError: If ADB command fails.

    """
    adb_path = resolve_adb()
    cmd = [adb_path]
    if serial:
        cmd.extend(["-s", serial])
    cmd.append("wait-for-device")

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        msg = f"Timed out waiting for device after {timeout}s"
        raise TimeoutError(msg) from e
    except subprocess.CalledProcessError as e:
        msg = f"Failed to wait for device: {e.stderr}"
        raise RuntimeError(msg) from e


async def async_wait_for_device(
    serial: str | None = None,
    timeout: float | None = None,
) -> None:
    """Wait for device to be ready (async).

    Args:
        serial: Optional device serial number.
        timeout: Timeout in seconds.

    Raises:
        TimeoutError: If timeout expires.
        RuntimeError: If ADB command fails.

    """
    adb_path = resolve_adb()
    args = []
    if serial:
        args.extend(["-s", serial])
    args.append("wait-for-device")

    process = await asyncio.create_subprocess_exec(
        adb_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        if timeout is not None:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        else:
            await process.wait()
    except TimeoutError as e:
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        msg = f"Timed out waiting for device after {timeout}s"
        raise TimeoutError(msg) from e

    if process.returncode != 0:
        stderr_data = await process.stderr.read() if process.stderr else b""
        detail = stderr_data.decode(errors="replace").strip()
        msg = f"Failed to wait for device: {detail}"
        raise RuntimeError(msg)


def adb_connect(address: str, timeout: float | None = None) -> None:
    """Connect to a device via TCP/IP.

    Args:
        address: Device address (host:port).
        timeout: Connection timeout in seconds.

    Raises:
        RuntimeError: If connection fails.
        TimeoutError: If operation times out.

    """
    adb_path = resolve_adb()
    cmd = [adb_path, "connect", address]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        msg = f"Timed out connecting to {address} after {timeout}s"
        raise TimeoutError(msg) from e

    output = result.stdout.strip()
    error = result.stderr.strip()

    # Check for failure patterns in stdout (adb connect often prints errors to stdout)
    # "unable to connect to 192.168.1.5:5555: Connection refused"
    # "failed to connect to '192.168.1.5:5555': Connection refused"
    if (
        result.returncode != 0
        or "unable" in output.lower()
        or "failed" in output.lower()
    ):
        detail = error or output
        msg = f"Failed to connect to {address}: {detail}"
        raise RuntimeError(msg)

    # "already connected to 192.168.1.5:5555" -> Success
    # "connected to 192.168.1.5:5555" -> Success


async def async_adb_connect(address: str, timeout: float | None = None) -> None:
    """Connect to a device via TCP/IP (async).

    Args:
        address: Device address (host:port).
        timeout: Connection timeout in seconds.

    Raises:
        RuntimeError: If connection fails.
        TimeoutError: If operation times out.

    """
    adb_path = resolve_adb()

    process = await asyncio.create_subprocess_exec(
        adb_path,
        "connect",
        address,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        if timeout is not None:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        else:
            await process.wait()
    except TimeoutError as e:
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        msg = f"Timed out connecting to {address} after {timeout}s"
        raise TimeoutError(msg) from e

    stdout_data, stderr_data = await process.communicate()
    output = stdout_data.decode(errors="replace").strip()
    error = stderr_data.decode(errors="replace").strip()

    if (
        process.returncode != 0
        or "unable" in output.lower()
        or "failed" in output.lower()
    ):
        detail = error or output
        msg = f"Failed to connect to {address}: {detail}"
        raise RuntimeError(msg)


def extract_json(text: str) -> Any | None:  # noqa: ANN401
    """Extract and parse the first valid JSON object or array found in the text.

    Args:
        text: The text to search.

    Returns:
        The parsed JSON object (dict or list) if found, otherwise None.

    Notes:
        Scanning is bounded to protect against worst-case inputs with huge numbers
        of JSON-like delimiters that repeatedly fail to parse.

    """
    if not text:
        return None

    decoder = json.JSONDecoder()
    idx = 0
    attempts = 0
    scan_limit = min(len(text), _EXTRACT_JSON_MAX_SCAN_CHARS)

    while idx < scan_limit and attempts < _EXTRACT_JSON_MAX_CANDIDATES:
        # Find next potential start
        next_brace = text.find("{", idx, scan_limit)
        next_bracket = text.find("[", idx, scan_limit)

        if next_brace == -1 and next_bracket == -1:
            return None

        if next_brace == -1:
            start = next_bracket
        elif next_bracket == -1:
            start = next_brace
        else:
            start = min(next_brace, next_bracket)

        try:
            obj, _ = decoder.raw_decode(text, idx=start)
            return obj
        except json.JSONDecodeError as exc:
            # Failed to parse from this position.
            # Heuristic: if decoder reports a later error position, skip there
            # to reduce repeated scans over known-invalid spans.
            attempts += 1
            next_idx = max(start + 1, exc.pos + 1)
            idx = min(next_idx, scan_limit)

    return None
