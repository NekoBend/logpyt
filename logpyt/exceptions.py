"""Exceptions for logpyt stream operations.

Two generations live here during the 3.0.0 migration (design section 8.1): the 2.x
``LogStream*Error`` classes, which the current streams still raise, and the 3.0
hierarchy rooted at `LogpytError` (design section 4.1), which nothing raises yet.
The driver rewrites switch to the 3.0 classes and the last one deletes the 2.x ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from logpyt.streams._events import IncompleteReason, StreamStats

__all__ = [
    "AdbError",
    "AdbExitError",
    "CallbackError",
    "CallbackStage",
    "IncompleteCaptureError",
    "LogStreamError",
    "LogStreamInternalError",
    "LogStreamKilledError",
    "LogStreamPidResolveError",
    "LogStreamStderrError",
    "LogStreamTimeoutError",
    "LogpytError",
    "ReadTimeoutError",
    "StreamError",
    "UsageError",
]


class LogStreamError(Exception):
    """Base exception for all log stream errors.

    This exception serves as the parent class for all specific exceptions
    raised by the LogStream and related components. Catching this exception
    allows handling any error originating from the log stream processing.
    """


class LogStreamStderrError(LogStreamError):
    """Raised when there is an error reading from stderr.

    This exception indicates that the underlying ADB process produced output
    to standard error, which typically signifies a problem with the command
    execution or the device connection.
    """


class LogStreamPidResolveError(LogStreamError):
    """Raised when the PID for a package cannot be resolved.

    This exception occurs when the system attempts to resolve the Process ID (PID)
    for a given package name but fails. This can happen if the package is not
    running or if the device is not responding correctly.
    """


class LogStreamInternalError(LogStreamError):
    """Raised when an internal error occurs in the log stream.

    This exception indicates an unexpected state or failure within the
    LogStream implementation itself, rather than an external error like
    device disconnection.
    """


class LogStreamKilledError(LogStreamError):
    """Raised when the log stream process is killed.

    This exception is raised when the log stream is intentionally terminated
    via the `kill()` method. It represents a specific exit state indicating
    that the stream was stopped by user request rather than an error.
    """


class LogStreamTimeoutError(LogStreamError):
    """Raised when an operation on the log stream times out.

    This exception is raised when a blocking operation, such as waiting for
    the stream to start or stop, exceeds the specified timeout duration.
    """


# -- 3.0 hierarchy (design section 4.1) ----------------------------------------

# The user code that raised: a callback, the `where` condition or the grouper.
type CallbackStage = Literal["on_item", "on_event", "where", "group_by"]


def _restore_error[E: LogpytError](cls: type[E], args: tuple[object, ...]) -> E:
    """Recreate an unpickled error without calling its keyword-only ``__init__``."""
    return cls.__new__(cls, *args)


class LogpytError(Exception):
    """Base class of every error logpyt raises on purpose.

    Instances pickle and copy with all their attributes and notes, although the
    subclasses take keyword-only constructor arguments.
    """

    def __reduce__(
        self,
    ) -> tuple[
        Callable[[type[LogpytError], tuple[object, ...]], LogpytError],
        tuple[type[LogpytError], tuple[object, ...]],
        dict[str, object],
    ]:
        """Pickle as (class, args) plus the attribute dict, bypassing ``__init__``."""
        return (_restore_error, (type(self), self.args), self.__dict__)


class UsageError(LogpytError, RuntimeError):
    """The API was used in a way it does not support; the message names the fix."""


class AdbError(LogpytError):
    """An adb process failed.

    Args:
        message: What failed, for humans.
        argv: The full adb command line that ran, one item per argument.
        returncode: The exit status, or None when the process could not start.
        stderr: What adb wrote to stderr (for a stream: the kept stderr tail).

    Raises:
        TypeError: ``argv`` is one ``str`` or ``bytes`` instead of a sequence of
            arguments; ``tuple()`` would split it into characters.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str],
        returncode: int | None,
        stderr: str = "",
    ) -> None:
        if isinstance(argv, str | bytes | bytearray):
            msg = (
                "argv takes a sequence of arguments, not one string: "
                "write ('adb', 'logcat'), not 'adb logcat'"
            )
            raise TypeError(msg)
        super().__init__(message)
        self.argv: tuple[str, ...] = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr


class StreamError(LogpytError):
    """The terminal outcome of a stream run that failed.

    Args:
        message: Why the run failed, for humans.
        stats: The run's statistics when it ended.
    """

    def __init__(self, message: str, *, stats: StreamStats) -> None:
        super().__init__(message)
        self.stats = stats


class AdbExitError(StreamError, AdbError):
    """``adb logcat`` could not start, ended or found no device; no reconnect was left.

    Args:
        message: What happened, for humans.
        argv: The logcat command line of the last connection.
        returncode: The exit status, or None when adb could not start.
        stderr: The stderr tail of the last connection.
        stats: The run's statistics when it ended.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str],
        returncode: int | None,
        stderr: str = "",
        stats: StreamStats,
    ) -> None:
        # StreamError.__init__ would hand `message` alone to AdbError (next in the
        # MRO), so initialise the AdbError side explicitly and set stats here.
        AdbError.__init__(
            self, message, argv=argv, returncode=returncode, stderr=stderr
        )
        self.stats = stats


class ReadTimeoutError(StreamError):
    """adb was silent on stdout and stderr for ``read_timeout``; no reconnect was left.

    Deliberately not a `TimeoutError`: the builtin means "your wait expired"
    (``join(timeout)``), never "the stream failed" (design D-3).
    """


class CallbackError(StreamError):
    """User code raised: ``on_item``, ``on_event``, ``where`` or ``group_by``.

    The original exception is the ``__cause__``.

    Args:
        message: What raised, for humans.
        stage: Which user code raised.
        stats: The run's statistics when it ended.
    """

    def __init__(
        self, message: str, *, stage: CallbackStage, stats: StreamStats
    ) -> None:
        super().__init__(message, stats=stats)
        self.stage: CallbackStage = stage


class IncompleteCaptureError(LogpytError, AssertionError):
    """``assert_complete()`` found that the capture may miss device lines.

    Args:
        message: The reasons, for humans.
        reasons: The `StreamStats.incomplete` reasons, in their documented order.
    """

    def __init__(self, message: str, *, reasons: Sequence[IncompleteReason]) -> None:
        super().__init__(message)
        self.reasons: tuple[IncompleteReason, ...] = tuple(reasons)
