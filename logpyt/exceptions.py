"""Exceptions for logpyt stream operations."""

from __future__ import annotations


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


# Alias for backward compatibility or alternative naming preference
LogStreamStdErrError = LogStreamStderrError


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
