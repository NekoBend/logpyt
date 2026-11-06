"""The errors logpyt raises: the hierarchy rooted at `LogpytError` (design 4.1).

`LogStream` and `AsyncLogStream` raise these. The six 2.x ``LogStream*Error``
classes are gone (design 4.1, PR-13).
"""

from __future__ import annotations

import pickle  # noqa: S403  only round-trips objects this process holds
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from logpyt.streams._events import IncompleteReason, StreamStats

__all__ = [
    "AdbError",
    "AdbExitError",
    "CallbackError",
    "CallbackStage",
    "IncompleteCaptureError",
    "LogpytError",
    "ReadTimeoutError",
    "StreamError",
    "UsageError",
]


# The user code that raised: a callback, the `where` condition or the grouper.
type CallbackStage = Literal["on_item", "on_event", "where", "group_by"]


def _restore_error[E: LogpytError](cls: type[E], args: tuple[object, ...]) -> E:
    """Recreate an unpickled error without calling its keyword-only ``__init__``."""
    return cls.__new__(cls, *args)


def _portable(link: BaseException | None) -> BaseException | None:
    """Return ``link`` when it survives a pickle round-trip, else None.

    A chained exception can come from user code and fail to pickle or to
    unpickle (a local class, a keyword-only ``__init__``); leaving it out
    keeps the error itself picklable.
    """
    if link is None:
        return None
    try:
        pickle.loads(pickle.dumps(link))  # noqa: S301  bytes made one line above
    except Exception:  # noqa: BLE001  any failure means the link does not travel
        return None
    return link


class LogpytError(Exception):
    """Base class of every error logpyt raises on purpose.

    Instances pickle and copy with all their attributes and notes, and with
    ``__cause__``, ``__context__`` and ``__suppress_context__``, although the
    subclasses take keyword-only constructor arguments. A cause or context
    that does not survive pickling is left out; the traceback never travels.
    """

    def __reduce__(
        self,
    ) -> tuple[
        Callable[[type[LogpytError], tuple[object, ...]], LogpytError],
        tuple[type[LogpytError], tuple[object, ...]],
        dict[str, Any],
    ]:
        """Pickle as (class, args) plus the attributes and the chaining state.

        ``BaseException.__reduce__`` keeps only the attribute dict, so a copy
        would lose the exception chain; ``__init__`` is bypassed.
        """
        state: dict[str, Any] = {
            **self.__dict__,
            "__cause__": _portable(self.__cause__),
            "__context__": _portable(self.__context__),
            "__suppress_context__": self.__suppress_context__,
        }
        return (_restore_error, (type(self), self.args), state)

    def __setstate__(self, state: dict[str, Any] | None, /) -> None:
        """Restore what `__reduce__` saved, ``__suppress_context__`` last.

        Assigning ``__cause__`` sets ``__suppress_context__`` to True, so the
        saved flag is applied after it, whatever the order of ``state``.
        """
        if state is None:
            return
        fields = dict(state)
        suppress_context = fields.pop("__suppress_context__", None)
        for name, value in fields.items():
            setattr(self, name, value)
        if suppress_context is not None:
            self.__suppress_context__ = suppress_context


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
