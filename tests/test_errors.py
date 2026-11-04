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
    LogStreamError,
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
def test_new_errors_end_in_error_and_stay_apart_from_2x(error: type) -> None:
    assert error.__name__.endswith("Error")
    assert not issubclass(error, LogStreamError)
    assert not issubclass(LogStreamError, error)


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
def test_2x_errors_are_still_exported(name: str) -> None:
    assert name in logpyt.__all__
    assert issubclass(getattr(logpyt, name), LogStreamError)
