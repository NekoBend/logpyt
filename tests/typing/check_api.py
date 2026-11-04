"""Static checks of the 3.0 public types, run by ``ty check`` and never executed.

Every ``assert_type`` pins an inferred type. Every ``# ty: ignore[<rule>]`` line
pins a call or assignment that ty must keep rejecting: pyproject.toml makes an
unused ignore an error, so a line that stops failing breaks ``ty check``.

The stream constructor overloads of design section 2.3 are checked on a stand-in
class with the same signatures until ``LogStream`` itself has them (design
section 11, risk 12: ty is pre-1.0 and may regress on self-typed overloads).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, assert_never, assert_type, overload

from logpyt import (
    AdbError,
    AdbExitError,
    And,
    AnrCondition,
    AsyncLogStream,
    CallbackError,
    Condition,
    CrashCondition,
    Filter,
    Gap,
    IncompleteCaptureError,
    Level,
    LogEntry,
    LogFileReader,
    LogpytError,
    LogStream,
    MessageContains,
    MinLevel,
    Not,
    Or,
    Package,
    ReadTimeoutError,
    Reconnect,
    StateChange,
    StreamConfig,
    StreamError,
    StreamEvent,
    StreamStats,
    Tag,
    UsageError,
    Where,
    read_file,
)
from logpyt.adb import DeviceInfo, DeviceKind, list_devices, wait_for_device
from logpyt.exceptions import CallbackStage
from logpyt.streams._events import GapReason, IncompleteReason, StreamState

if TYPE_CHECKING:
    from collections.abc import Callable


def check_config() -> None:
    config = StreamConfig(
        serial="emulator-5554",
        logcat_args=("-b", "crash", "-b", "main", "ActivityManager:I"),
        since="now",
        read_timeout=60.0,
        reconnect=Reconnect(max_attempts=5),
    )
    assert_type(config.adb_path, str | None)
    assert_type(config.logcat_args, tuple[str, ...])
    assert_type(config.since, Literal["backlog", "now"] | datetime)
    assert_type(config.read_timeout, float | None)
    assert_type(config.stall_timeout, float | None)
    assert_type(config.reconnect, Reconnect | None)
    assert_type(config.buffer_entries, int)
    assert_type(config.max_line_bytes, int)
    assert_type(Reconnect().max_attempts, int | None)
    assert_type(Reconnect().backoff, float)
    StreamConfig(since=datetime(2026, 11, 4, tzinfo=UTC))
    StreamConfig(since="later")  # ty: ignore[invalid-argument-type]
    StreamConfig(logcat_args="-s")  # ty: ignore[invalid-argument-type]
    StreamConfig(serail="emulator-5554")  # ty: ignore[unknown-argument]
    StreamConfig(config)  # ty: ignore[too-many-positional-arguments]
    config.serial = "emulator-5556"  # ty: ignore[invalid-assignment]
    Reconnect().delay = 2.0  # ty: ignore[invalid-assignment]


def check_event_construction() -> None:
    StateChange(StreamState.RUNNING, StreamState.RECONNECTING, 1, None)
    StateChange(StreamState.RUNNING, StreamState.RECONNECTING, 1)  # ty: ignore[missing-argument]


def check_events(event: StreamEvent) -> str:
    match event:
        case StateChange(new=StreamState.RECONNECTING, error=None):
            return "stall resume"
        case StateChange(old=old, new=new, connection=connection, error=error):
            assert_type(old, StreamState)
            assert_type(new, StreamState)
            assert_type(connection, int)
            assert_type(error, StreamError | None)
            return f"{old} -> {new}"
        case Gap(after=after, lost=lost, reason=reason):
            assert_type(after, datetime | None)
            assert_type(lost, int | None)
            assert_type(reason, GapReason)
            return reason
        case _:
            assert_never(event)


def check_stats(stats: StreamStats) -> None:
    assert_type(stats.incomplete, tuple[IncompleteReason, ...])
    assert_type(stats.complete, bool)
    assert_type(stats.backpressure_seconds, float)
    StreamStats(lines=1, delivered=1)
    StreamStats(1)  # ty: ignore[too-many-positional-arguments]
    stats.lines = 2  # ty: ignore[invalid-assignment]
    Gap(None, None, None, "overwritten")  # ty: ignore[invalid-argument-type]


def check_errors(stats: StreamStats) -> None:
    exit_error = AdbExitError(
        "adb logcat ended", argv=["adb", "logcat"], returncode=1, stats=stats
    )
    assert_type(exit_error.argv, tuple[str, ...])
    assert_type(exit_error.returncode, int | None)
    assert_type(exit_error.stderr, str)
    assert_type(exit_error.stats, StreamStats)
    as_stream_error: StreamError = exit_error
    as_adb_error: AdbError = exit_error
    as_base: LogpytError = as_stream_error
    callback_error = CallbackError("on_item raised", stage="on_item", stats=stats)
    assert_type(callback_error.stage, CallbackStage)
    as_runtime_error: RuntimeError = UsageError("use stream.clone()")
    as_assertion: AssertionError = IncompleteCaptureError("gap", reasons=["gap"])
    assert_type(
        IncompleteCaptureError("gap", reasons=["gap"]).reasons,
        tuple[IncompleteReason, ...],
    )
    del as_adb_error, as_base, as_runtime_error, as_assertion
    CallbackError("x", stage="on_items", stats=stats)  # ty: ignore[invalid-argument-type]
    IncompleteCaptureError("x", reasons=["gaps"])  # ty: ignore[invalid-argument-type]
    ReadTimeoutError("silent")  # ty: ignore[missing-argument]
    as_timeout: TimeoutError = ReadTimeoutError("x", stats=stats)  # ty: ignore[invalid-assignment]
    del as_timeout


def check_adb_helpers() -> None:
    devices = list_devices()
    assert_type(devices, list[DeviceInfo])
    info = devices[0]
    assert_type(info.kind, DeviceKind)
    assert_type(info.transport_id, int | None)
    assert_type(info.usb, str | None)
    list_devices("emulator", adb_path="adb", timeout=None)
    list_devices("device")  # ty: ignore[invalid-argument-type]
    list_devices("usb", 5.0)  # ty: ignore[too-many-positional-arguments]
    wait_for_device("emulator-5554", 5.0)  # ty: ignore[too-many-positional-arguments]
    info.state = "offline"  # ty: ignore[invalid-assignment]


def _is_error(entry: LogEntry) -> bool:
    return entry.level == "E"


def check_conditions(entry: LogEntry) -> None:
    where = (Package("com.example") & MinLevel("warning")) | ~Tag(["a", "b"])
    assert_type(where, Or)
    assert_type(Tag("a") & Level("E"), And)
    assert_type(~MessageContains("x"), Not)
    assert_type(where(entry), bool)
    assert_type(where.packages(), frozenset[str])
    assert_type(Filter(package="com.example").conditions, tuple[Condition, ...])
    as_and: And = Filter(tag="x", min_level="E")
    as_condition: Condition = Where(_is_error)
    crash: Condition = CrashCondition(package="com.example") | AnrCondition()
    del as_and, as_condition, crash
    Filter("com.example")  # ty: ignore[too-many-positional-arguments]
    CrashCondition("AndroidRuntime")  # ty: ignore[too-many-positional-arguments]
    AnrCondition(package=["com.example"])  # ty: ignore[invalid-argument-type]
    MinLevel(["E"])  # ty: ignore[invalid-argument-type]
    Where("E")  # ty: ignore[invalid-argument-type]
    Tag("a") & _is_error  # ty: ignore[unsupported-operator]
    And(Tag("a"), _is_error)  # ty: ignore[invalid-argument-type]


class _RenamedParameter(Condition):
    # Condition.__call__ takes the entry by position: an override may rename it.
    def __call__(self, e: LogEntry) -> bool:
        return e.level == "E"


class _PositionalOnly(Condition):
    def __call__(self, entry: LogEntry, /) -> bool:
        return entry.level == "E"


def check_custom_conditions(entry: LogEntry) -> None:
    assert_type(_RenamedParameter()(entry), bool)
    assert_type(_RenamedParameter() & _PositionalOnly(), And)
    Tag("a")(entry=entry)  # ty: ignore[positional-only-parameter-as-kwarg]


def check_filter_by_takes_any_condition() -> None:
    # X-7: the 2.x entry points were annotated ``Filter | None``.
    where = (Package("com.example") & Level("E")) | MessageContains("CRITICAL")
    LogStream(filter_by=where)
    AsyncLogStream(filter_by=CrashCondition(package="com.example"))
    LogFileReader("app.log", filter_by=~Tag("noise"))
    read_file("app.log", filter_by=Where(_is_error))
    LogStream(filter_by=_is_error)  # ty: ignore[invalid-argument-type]
    read_file("app.log", filter_by=_is_error)  # ty: ignore[invalid-argument-type]


# -- the stream constructor overloads of design section 2.3, on a stand-in ----------


class _GroupBy:
    """Stands in for logpyt.GroupBy (PR-07)."""


class _LogGroup:
    """Stands in for logpyt.LogGroup (PR-07)."""


class _StreamShape[T]:
    """Has exactly the constructor overloads design section 2.3 gives LogStream."""

    @overload
    def __init__(
        self: _StreamShape[LogEntry],
        config: StreamConfig | None = None,
        /,
        *,
        where: Condition | None = None,
        group_by: None = None,
        on_item: Callable[[LogEntry], object] | None = None,
        on_event: Callable[[StreamEvent], object] | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self: _StreamShape[_LogGroup],
        config: StreamConfig | None = None,
        /,
        *,
        where: Condition | None = None,
        group_by: _GroupBy,
        on_item: Callable[[_LogGroup], object] | None = None,
        on_event: Callable[[StreamEvent], object] | None = None,
    ) -> None: ...
    def __init__(
        self,
        config: StreamConfig | None = None,
        /,
        *,
        where: Condition | None = None,
        group_by: _GroupBy | None = None,
        on_item: Callable[[LogEntry], object]
        | Callable[[_LogGroup], object]
        | None = None,
        on_event: Callable[[StreamEvent], object] | None = None,
    ) -> None:
        del config, where, group_by, on_item, on_event


def _on_event(event: StreamEvent) -> None:
    del event


def check_stream_overloads() -> None:
    entries: list[LogEntry] = []
    groups: list[_LogGroup] = []
    events: list[StreamEvent] = []
    config = StreamConfig(serial="emulator-5554", since="now")
    assert_type(_StreamShape(), _StreamShape[LogEntry])
    assert_type(
        _StreamShape(config, on_item=entries.append, on_event=_on_event),
        _StreamShape[LogEntry],
    )
    assert_type(
        _StreamShape(where=Tag("x"), on_event=events.append),
        _StreamShape[LogEntry],
    )
    assert_type(_StreamShape(group_by=_GroupBy()), _StreamShape[_LogGroup])
    assert_type(
        _StreamShape(config, group_by=_GroupBy(), on_item=groups.append),
        _StreamShape[_LogGroup],
    )
    _StreamShape(group_by=_GroupBy(), on_item=entries.append)  # ty: ignore[no-matching-overload]
    _StreamShape(on_item=groups.append)  # ty: ignore[invalid-argument-type]
    _StreamShape(config=config)  # ty: ignore[no-matching-overload]
