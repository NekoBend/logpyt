"""Common utilities and types for log streams."""

from __future__ import annotations

from enum import Enum, auto

from logpyt.adb import _check_package_name


class StreamState(Enum):
    """State of the log stream."""

    IDLE = auto()
    STARTING = auto()
    RUNNING = auto()
    PAUSED = auto()
    STOPPING = auto()
    STOPPED = auto()
    KILLED = auto()
    RECONNECTING = auto()


def build_pidof_command(
    adb_path: str, device_id: str | None, package: str
) -> list[str]:
    """Build the ADB command to get PIDs for a package.

    Args:
        adb_path: Path to ADB executable.
        device_id: Target device serial ID.
        package: Package name; it reaches the device shell, so it must
            follow the package-name grammar (F5-3).

    Returns:
        List of command arguments.

    Raises:
        ValueError: ``package`` is not a package name.
        TypeError: ``package`` is not a str.

    """
    cmd = [adb_path]
    if device_id:
        cmd.extend(["-s", device_id])
    cmd.extend(["shell", "pidof", _check_package_name(package)])
    return cmd
