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
            with open("/proc/version", "r") as f:
                if "microsoft" in f.read().lower():
                    is_wsl = True
        except OSError:
            pass

    if sys.platform == "win32" or is_wsl:
        # Windows or WSL
        candidates.append("adb.exe")

    # 1. Search in PATH
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path

    # 2. Search in Environment Variables
    env_vars = ["ANDROID_HOME", "ANDROID_SDK_ROOT"]
    for var in env_vars:
        root = os.environ.get(var)
        if root:
            for candidate in candidates:
                path = os.path.join(root, "platform-tools", candidate)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    return path

    raise FileNotFoundError(
        "Could not find 'adb' or 'adb.exe' in PATH or Android SDK directories."
    )


def list_devices(
    device_type: DeviceType | None = None, timeout: float = 10.0
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
        FileNotFoundError: If ADB is not found.
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
        raise RuntimeError(f"Failed to run adb devices: {e.stderr}") from e
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"Timed out running adb devices after {timeout}s") from e

    devices: list[DeviceInfo] = []

    # Parse output
    # Example: "emulator-5554 device product:sdk_gphone_x86_64 model:sdk_gphone_x86_64 device:generic_x86_64 transport_id:1"
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
                if key in ["product", "model", "device", "transport_id"]:
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
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
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
        FileNotFoundError: If ADB is not found.
    """
    adb_path = resolve_adb()
    cmd = [adb_path]
    if serial:
        cmd.extend(["-s", serial])
    cmd.append("wait-for-device")

    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"Timed out waiting for device after {timeout}s") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to wait for device: {e.stderr}") from e


async def async_wait_for_device(
    serial: str | None = None, timeout: float | None = None
) -> None:
    """Wait for device to be ready (async).

    Args:
        serial: Optional device serial number.
        timeout: Timeout in seconds.

    Raises:
        TimeoutError: If timeout expires.
        RuntimeError: If ADB command fails.
        FileNotFoundError: If ADB is not found.
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
    except asyncio.TimeoutError as e:
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        raise TimeoutError(f"Timed out waiting for device after {timeout}s") from e

    if process.returncode != 0:
        stderr_data = await process.stderr.read() if process.stderr else b""
        raise RuntimeError(f"Failed to wait for device: {stderr_data.decode().strip()}")


def adb_connect(address: str, timeout: float | None = None) -> None:
    """Connect to a device via TCP/IP.

    Args:
        address: Device address (host:port).
        timeout: Connection timeout in seconds.

    Raises:
        RuntimeError: If connection fails.
        TimeoutError: If operation times out.
        FileNotFoundError: If ADB is not found.
    """
    adb_path = resolve_adb()
    cmd = [adb_path, "connect", address]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"Timed out connecting to {address} after {timeout}s") from e

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
        msg = error if error else output
        raise RuntimeError(f"Failed to connect to {address}: {msg}")

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
        FileNotFoundError: If ADB is not found.
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
    except asyncio.TimeoutError as e:
        try:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass
        raise TimeoutError(f"Timed out connecting to {address} after {timeout}s") from e

    stdout_data, stderr_data = await process.communicate()
    output = stdout_data.decode().strip()
    error = stderr_data.decode().strip()

    if (
        process.returncode != 0
        or "unable" in output.lower()
        or "failed" in output.lower()
    ):
        msg = error if error else output
        raise RuntimeError(f"Failed to connect to {address}: {msg}")


def extract_json(text: str) -> Any | None:
    """Extract and parse the first valid JSON object or array found in the text.

    Args:
        text: The text to search.

    Returns:
        The parsed JSON object (dict or list) if found, otherwise None.
    """
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        # Find next potential start
        next_brace = text.find("{", idx)
        next_bracket = text.find("[", idx)

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
        except json.JSONDecodeError:
            # Failed to parse from this position, move past it
            idx = start + 1

    return None
