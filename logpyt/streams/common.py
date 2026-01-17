"""Common utilities and types for log streams."""

from __future__ import annotations

from enum import Enum, auto


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
        package: Package name.

    Returns:
        List of command arguments.
    """
    cmd = [adb_path]
    if device_id:
        cmd.extend(["-s", device_id])
    cmd.extend(["shell", "pidof", package])
    return cmd
