"""Static checks of the 3.0 public types, run by ``ty check`` and never executed.

Every ``assert_type`` pins an inferred type. Every ``# ty: ignore[<rule>]`` line
pins a call or assignment that ty must keep rejecting: pyproject.toml makes an
unused ignore an error, so a line that stops failing breaks ``ty check``.

The stream constructor overloads of design section 2.3 are checked on
``LogStream`` itself (design section 11, risk 12: ty is pre-1.0 and may regress
on self-typed overloads).
"""

from __future__ import annotations

from collections.abc import Hashable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, assert_never, assert_type

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
    GroupBy,
    IncompleteCaptureError,
    Level,
    LogEntry,
    LogGroup,
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
    ThreadTimeLogParser,
    UsageError,
    Where,
    read_file,
)
from logpyt.adb import DeviceInfo, DeviceKind, list_devices, wait_for_device
from logpyt.exceptions import CallbackStage
from logpyt.groupers import GroupKey
from logpyt.models.group import GroupReason
from logpyt.streams._events import GapReason, IncompleteReason, StreamState

if TYPE_CHECKING:
    from collections.abc import Sequence


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
    LogStream(where=where)
    AsyncLogStream(filter_by=CrashCondition(package="com.example"))
    read_file("app.log", where=~Tag("noise"))
    read_file("app.log", where=Where(_is_error))
    LogStream(where=_is_error)  # ty: ignore[invalid-argument-type]
    read_file("app.log", where=_is_error)  # ty: ignore[invalid-argument-type]


def check_read_file(spec: GroupBy, maybe: GroupBy | None) -> None:
    # Design 2.8: entries without group_by, groups with it.
    assert_type(read_file("app.log"), Iterator[LogEntry])
    assert_type(
        read_file(
            Path("app.log"),
            parser=ThreadTimeLogParser(),
            where=Package("com.example"),
            year=2026,
            uid_packages={10065: "com.example"},
            max_line_bytes=8192,
        ),
        Iterator[LogEntry],
    )
    assert_type(read_file("app.log", group_by=spec), Iterator[LogGroup])
    assert_type(
        read_file("app.log", group_by=maybe), Iterator[LogEntry] | Iterator[LogGroup]
    )
    for entry in read_file("app.log"):
        assert_type(entry, LogEntry)
    read_file("app.log", ThreadTimeLogParser())  # ty: ignore[no-matching-overload]
    read_file("app.log", filter_by=Tag("x"))  # ty: ignore[no-matching-overload]
    read_file("app.log", uid_packages={"com.example": 10065})  # ty: ignore[invalid-argument-type]
    read_file("app.log", group_by=("pid",))  # ty: ignore[no-matching-overload]


def check_group_by(entries: list[LogEntry]) -> None:
    spec = GroupBy(keys=("pid", "tag"), gap_ms=50.0, mode="windowed")
    assert_type(spec.keys, tuple[GroupKey, ...])
    assert_type(spec.gap_ms, float)
    assert_type(spec.mode, Literal["consecutive", "windowed"])
    assert_type(spec.idle_flush_ms, float | Literal["gap"] | None)
    assert_type(spec.max_total_bytes, int)
    assert_type(spec.groups(entries), Iterator[LogGroup])
    GroupBy(keys=(), gap_ms=1, idle_flush_ms=None)
    GroupBy(keys="pid", gap_ms=50.0)  # ty: ignore[invalid-argument-type]
    GroupBy(keys=["pid"], gap_ms=50.0)  # ty: ignore[invalid-argument-type]
    GroupBy(keys=("pdi",), gap_ms=50.0)  # ty: ignore[invalid-argument-type]
    GroupBy(keys=("pid",), gap_ms=50.0, mode="windows")  # ty: ignore[invalid-argument-type]
    GroupBy(keys=("pid",), gap_ms=50.0, idle_flush_ms="never")  # ty: ignore[invalid-argument-type]
    GroupBy(keys=("pid",), gap_ms=50.0, emit_mode="group")  # ty: ignore[unknown-argument]
    GroupBy(("pid",), 50.0)  # ty: ignore[missing-argument, too-many-positional-arguments]
    spec.gap_ms = 10.0  # ty: ignore[invalid-assignment]


def check_log_group(group: LogGroup) -> None:
    assert_type(group.key, tuple[Hashable, ...])
    assert_type(group.entries, tuple[LogEntry, ...])
    assert_type(group.reason, GroupReason)
    assert_type(group[0], LogEntry)
    assert_type(group[1:], tuple[LogEntry, ...])
    assert_type(len(group), int)
    as_sequence: Sequence[LogEntry] = group
    del as_sequence
    for entry in group:
        assert_type(entry, LogEntry)
    LogGroup(key=(1,), entries=(group[0],), reason="gap")
    LogGroup(key=(1,), entries=(group[0],), reason="timeout")  # ty: ignore[invalid-argument-type]
    LogGroup(key=(1,), entries=[group[0]], reason="end")  # ty: ignore[invalid-argument-type]
    group.reason = "end"  # ty: ignore[invalid-assignment]


# -- LogStream: the constructor overloads and the methods of design section 2.3 -----


def _on_event(event: StreamEvent) -> None:
    del event


def check_stream_overloads() -> None:
    entries: list[LogEntry] = []
    groups: list[LogGroup] = []
    events: list[StreamEvent] = []
    config = StreamConfig(serial="emulator-5554", since="now")
    assert_type(LogStream(), LogStream[LogEntry])
    assert_type(
        LogStream(config, on_item=entries.append, on_event=_on_event),
        LogStream[LogEntry],
    )
    assert_type(
        LogStream(where=Tag("x"), on_event=events.append),
        LogStream[LogEntry],
    )
    spec = GroupBy(keys=("pid",), gap_ms=50.0)
    assert_type(LogStream(group_by=spec), LogStream[LogGroup])
    assert_type(
        LogStream(config, group_by=spec, on_item=groups.append),
        LogStream[LogGroup],
    )
    LogStream(group_by=spec, on_item=entries.append)  # ty: ignore[no-matching-overload]
    LogStream(on_item=groups.append)  # ty: ignore[invalid-argument-type]
    LogStream(config=config)  # ty: ignore[no-matching-overload]


def check_stream_methods(
    stream: LogStream[LogEntry], grouped: LogStream[LogGroup]
) -> None:
    assert_type(stream.config, StreamConfig)
    assert_type(stream.state, StreamState)
    assert_type(stream.stats, StreamStats)
    assert_type(stream.outcome, StreamError | None)
    assert_type(stream.clone(), LogStream[LogEntry])
    assert_type(grouped.clone(), LogStream[LogGroup])
    assert_type(iter(stream), Iterator[LogEntry])
    for entry in stream:
        assert_type(entry, LogEntry)
    for group in grouped:
        assert_type(group, LogGroup)
    with stream as entered:
        assert_type(entered, LogStream[LogEntry])
    stream.start()
    stream.stop()
    stream.stop(flush=5.0)
    stream.kill()
    stream.join()
    stream.join(timeout=1.0)
    stream.wait_running(timeout=30.0)
    stream.assert_complete()
    stream.stop(5.0)  # ty: ignore[too-many-positional-arguments]
    stream.state = StreamState.RUNNING  # ty: ignore[invalid-assignment]
