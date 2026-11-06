"""The 3.0 exception hierarchy (design section 4.1) beside the 2.x classes."""

from __future__ import annotations

import copy
import pickle  # noqa: S403  round-trips objects the test made
from typing import TYPE_CHECKING

import pytest

import logpyt
from logpyt import (
    AdbError,
    AdbExitError,
    CallbackError,
    IncompleteCaptureError,
    LogpytError,
    ReadTimeoutError,
    StreamError,
    StreamStats,
    UsageError,
)
from logpyt import config as config_module
from logpyt import exceptions as exceptions_module
from logpyt.streams import _events as events_module

if TYPE_CHECKING:
    from collections.abc import Callable

    from logpyt.exceptions import CallbackStage

NEW_ERRORS = [
    LogpytError,
    UsageError,
    AdbError,
    StreamError,
    AdbExitError,
    ReadTimeoutError,
    CallbackError,
    IncompleteCaptureError,
]
_STATS = StreamStats(lines=10, delivered=9, filtered=1)


@pytest.mark.parametrize(
    ("error", "bases"),
    [
        (LogpytError, (Exception,)),
        (UsageError, (LogpytError, RuntimeError)),
        (AdbError, (LogpytError,)),
        (StreamError, (LogpytError,)),
        (AdbExitError, (StreamError, AdbError)),
        (ReadTimeoutError, (StreamError,)),
        (CallbackError, (StreamError,)),
        (IncompleteCaptureError, (LogpytError, AssertionError)),
    ],
)
def test_hierarchy_is_the_designed_one(
    error: type[Exception], bases: tuple[type[Exception], ...]
) -> None:
    assert error.__bases__ == bases


def test_read_timeout_is_not_a_builtin_timeout() -> None:
    assert not issubclass(ReadTimeoutError, TimeoutError)


@pytest.mark.parametrize("error", NEW_ERRORS)
def test_new_errors_end_in_error(error: type) -> None:
    assert error.__name__.endswith("Error")
    assert issubclass(error, LogpytError)


def test_adb_error_keeps_the_command_and_its_result() -> None:
    error = AdbError(
        "adb shell pidof failed", argv=["adb", "shell", "pidof"], returncode=1
    )
    assert (str(error), error.args) == (
        "adb shell pidof failed",
        ("adb shell pidof failed",),
    )
    assert error.argv == ("adb", "shell", "pidof")
    assert error.returncode == 1
    assert error.stderr == ""


@pytest.mark.parametrize(
    "make",
    [
        lambda argv: AdbError("failed", argv=argv, returncode=1),
        lambda argv: AdbExitError("ended", argv=argv, returncode=1, stats=_STATS),
    ],
    ids=["AdbError", "AdbExitError"],
)
@pytest.mark.parametrize("argv", ["adb logcat", b"adb logcat", bytearray(b"adb")])
def test_adb_errors_refuse_one_string_as_argv(
    make: Callable[[object], AdbError], argv: object
) -> None:
    # tuple("adb logcat") would keep one character per argument.
    with pytest.raises(TypeError, match="argv takes a sequence of arguments"):
        make(argv)


def test_adb_exit_error_is_both_a_stream_and_an_adb_error() -> None:
    error = AdbExitError(
        "adb logcat could not start",
        argv=("adb", "logcat"),
        returncode=None,
        stderr="- waiting for device -",
        stats=_STATS,
    )
    assert isinstance(error, StreamError)
    assert isinstance(error, AdbError)
    assert str(error) == "adb logcat could not start"
    assert (error.argv, error.returncode) == (("adb", "logcat"), None)
    assert error.stderr == "- waiting for device -"
    assert error.stats is _STATS


def test_stream_errors_carry_the_stats_snapshot() -> None:
    assert ReadTimeoutError("silent for 60 s", stats=_STATS).stats is _STATS


@pytest.mark.parametrize("stage", ["on_item", "on_event", "where", "group_by"])
def test_callback_error_keeps_the_stage_and_its_cause(stage: CallbackStage) -> None:
    cause = ZeroDivisionError("boom")
    with pytest.raises(CallbackError) as caught:
        raise CallbackError(f"{stage} raised", stage=stage, stats=_STATS) from cause
    assert caught.value.stage == stage
    assert caught.value.__cause__ is cause
    assert caught.value.stats is _STATS


def test_incomplete_capture_error_keeps_the_reasons_as_a_tuple() -> None:
    error = IncompleteCaptureError("gap, truncated", reasons=["gap", "truncated"])
    assert error.reasons == ("gap", "truncated")
    assert isinstance(error, AssertionError)


_MAKERS: list[Callable[[], LogpytError]] = [
    lambda: LogpytError("base"),
    lambda: UsageError("join() called from a stream callback"),
    lambda: AdbError("failed", argv=["adb", "get-state"], returncode=1, stderr="x"),
    lambda: StreamError("failed", stats=_STATS),
    lambda: AdbExitError("ended", argv=["adb", "logcat"], returncode=0, stats=_STATS),
    lambda: ReadTimeoutError("silent", stats=_STATS),
    lambda: CallbackError("on_item raised", stage="on_item", stats=_STATS),
    lambda: IncompleteCaptureError("gap", reasons=("gap",)),
]
_CLONES: list[Callable[[LogpytError], LogpytError]] = [
    lambda error: pickle.loads(pickle.dumps(error)),  # noqa: S301  own data
    copy.copy,
    copy.deepcopy,
]


@pytest.mark.parametrize("make", _MAKERS, ids=[e.__name__ for e in NEW_ERRORS])
@pytest.mark.parametrize("clone", _CLONES, ids=["pickle", "copy", "deepcopy"])
def test_errors_survive_pickle_and_copy_with_attributes_and_notes(
    make: Callable[[], LogpytError], clone: Callable[[LogpytError], LogpytError]
) -> None:
    error = make()
    error.add_note("seen by test")
    restored = clone(error)
    assert type(restored) is type(error)
    assert restored.args == error.args
    assert vars(restored) == vars(error)
    assert restored.__notes__ == ["seen by test"]


class _KeywordOnlyError(Exception):
    """Pickles, but cannot unpickle: ``__init__`` takes no positional args."""

    def __init__(self, *, code: int) -> None:
        super().__init__(f"code {code}")


def _raised(
    error: LogpytError,
    *,
    cause: BaseException | None,
    context: Exception | None = None,
) -> LogpytError:
    """Raise ``error`` from ``cause``, while handling ``context`` if given."""
    try:
        if context is not None:
            try:
                raise context
            except type(context):
                raise error from cause
        raise error from cause
    except LogpytError as caught:
        return caught


@pytest.mark.parametrize("make", _MAKERS, ids=[e.__name__ for e in NEW_ERRORS])
@pytest.mark.parametrize("clone", _CLONES, ids=["pickle", "copy", "deepcopy"])
def test_errors_survive_pickle_and_copy_with_their_cause(
    make: Callable[[], LogpytError], clone: Callable[[LogpytError], LogpytError]
) -> None:
    error = _raised(make(), cause=ValueError("x"))
    error.add_note("seen by test")
    restored = clone(error)
    assert type(restored.__cause__) is ValueError
    assert str(restored.__cause__) == "x"
    assert restored.__context__ is None
    assert restored.__suppress_context__ is True
    assert vars(restored) == vars(error)
    assert restored.__notes__ == ["seen by test"]


@pytest.mark.parametrize("make", _MAKERS, ids=[e.__name__ for e in NEW_ERRORS])
@pytest.mark.parametrize("clone", _CLONES, ids=["pickle", "copy", "deepcopy"])
def test_errors_survive_pickle_and_copy_with_cause_and_context(
    make: Callable[[], LogpytError], clone: Callable[[LogpytError], LogpytError]
) -> None:
    restored = clone(_raised(make(), cause=ValueError("x"), context=KeyError("k")))
    assert type(restored.__cause__) is ValueError
    assert str(restored.__cause__) == "x"
    assert type(restored.__context__) is KeyError
    assert restored.__context__.args == ("k",)
    assert restored.__suppress_context__ is True


@pytest.mark.parametrize("make", _MAKERS, ids=[e.__name__ for e in NEW_ERRORS])
@pytest.mark.parametrize("clone", _CLONES, ids=["pickle", "copy", "deepcopy"])
def test_errors_survive_pickle_and_copy_raised_from_none(
    make: Callable[[], LogpytError], clone: Callable[[LogpytError], LogpytError]
) -> None:
    restored = clone(_raised(make(), cause=None, context=KeyError("k")))
    assert restored.__cause__ is None
    assert type(restored.__context__) is KeyError
    assert restored.__suppress_context__ is True


@pytest.mark.parametrize("make", _MAKERS, ids=[e.__name__ for e in NEW_ERRORS])
@pytest.mark.parametrize("clone", _CLONES, ids=["pickle", "copy", "deepcopy"])
def test_errors_survive_pickle_and_copy_with_an_implicit_context(
    make: Callable[[], LogpytError], clone: Callable[[LogpytError], LogpytError]
) -> None:
    error = make()
    try:
        try:
            raise KeyError("k")
        except KeyError:
            raise error  # noqa: B904  the implicit chain under test
    except LogpytError as caught:
        restored = clone(caught)
    assert restored.__cause__ is None
    assert type(restored.__context__) is KeyError
    assert restored.__suppress_context__ is False


@pytest.mark.parametrize("clone", _CLONES, ids=["pickle", "copy", "deepcopy"])
def test_a_cause_that_is_also_the_context_stays_one_object(
    clone: Callable[[LogpytError], LogpytError],
) -> None:
    error = CallbackError("on_item raised", stage="on_item", stats=_STATS)
    cause = ZeroDivisionError("boom")
    restored = clone(_raised(error, cause=cause, context=cause))
    assert type(restored.__cause__) is ZeroDivisionError
    assert restored.__cause__ is restored.__context__


@pytest.mark.parametrize("clone", _CLONES, ids=["pickle", "copy", "deepcopy"])
def test_a_link_that_does_not_survive_pickling_is_left_out(
    clone: Callable[[LogpytError], LogpytError],
) -> None:
    class LocalError(Exception):
        """Cannot pickle: a local class has no importable name."""

    error = _raised(
        CallbackError("on_item raised", stage="on_item", stats=_STATS),
        cause=_KeywordOnlyError(code=5),
        context=LocalError("local"),
    )
    restored = clone(error)
    assert type(restored) is CallbackError
    assert restored.__cause__ is None
    assert restored.__context__ is None
    assert restored.__suppress_context__ is True
    assert vars(restored) == vars(error)


@pytest.mark.parametrize(
    ("name", "module"),
    [
        ("LogpytError", exceptions_module),
        ("UsageError", exceptions_module),
        ("AdbError", exceptions_module),
        ("StreamError", exceptions_module),
        ("AdbExitError", exceptions_module),
        ("ReadTimeoutError", exceptions_module),
        ("CallbackError", exceptions_module),
        ("IncompleteCaptureError", exceptions_module),
        ("StreamConfig", config_module),
        ("Reconnect", config_module),
        ("StateChange", events_module),
        ("Gap", events_module),
        ("StreamEvent", events_module),
        ("StreamStats", events_module),
    ],
)
def test_new_names_are_exported_from_the_package(name: str, module: object) -> None:
    assert name in logpyt.__all__
    assert getattr(logpyt, name) is getattr(module, name)


@pytest.mark.parametrize(
    "name",
    [
        "LogStreamError",
        "LogStreamInternalError",
        "LogStreamKilledError",
        "LogStreamPidResolveError",
        "LogStreamStderrError",
        "LogStreamTimeoutError",
    ],
)
def test_2x_errors_are_removed(name: str) -> None:
    """The six 2.x stream errors are gone with the 2.x streams (design 4.1)."""
    assert name not in logpyt.__all__
    assert not hasattr(logpyt, name)
    assert not hasattr(exceptions_module, name)
