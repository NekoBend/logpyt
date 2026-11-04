"""Stream configuration shared by both drivers (design section 2.2, decision D-1).

Nothing reads it yet: the 2.x streams keep their own constructor arguments until
the driver rewrites switch over (design section 8.1).

The ``logcat_args`` whitelist mirrors how the Android 7.0 logcat parses its
arguments (``getopt_long`` with ``:cdDLt:T:gG:sQf:r:n:v:b:BSpP:m:e:``,
n-logcat.cpp:608): a value-taking option accepts its value attached (``-bmain``,
``--buffer=main``) or as the next argument, and every argument that does not
start with ``-`` is a filterspec.
"""

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from numbers import Number
from typing import Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

from logpyt.adb import _MARKER_TAG

__all__ = ["Reconnect", "StreamConfig"]

# Buffer names Android 7.0 logcat accepts for one -b (n-logcat.cpp:316-319,
# 767-860): one name per -b, no comma lists; "default" and "all" expand.
_BUFFER_NAMES = frozenset(
    ("main", "system", "radio", "events", "crash", "security", "kernel")
)
# logcat reads these when no -b is given (n-logcat.cpp:1003-1015).
_DEFAULT_BUFFERS = frozenset({"main", "system", "crash"})
_BUFFER_GROUPS = {"default": _DEFAULT_BUFFERS, "all": _BUFFER_NAMES}

# One filter rule: a tag up to the first ":", then an optional priority character
# (android_log_addFilterRule, nougat-release-logprint.c:347-366). filterCharToPri
# (logprint.c:93-126) takes the letters in either case, "*", and the digits 1-9:
# 1-7 are the android_LogPriority of that value (5 is WARN), 8 and 9 VERBOSE, while
# 0 is ANDROID_LOG_UNKNOWN, which the rule refuses. The device ignores anything
# after the priority character; logpyt rejects it as a typo.
_FILTER_RULE = re.compile(r"(?P<tag>[^:]+)(?::(?P<priority>[VDIWEFSvdiwefs*1-9]))?")
# android_log_addFilterString splits one argument on these characters.
_FILTER_SEPARATORS = re.compile(r"[ \t,]")
# --pid takes one spelling only, plain decimal (see _parse_pid), up to the
# largest 32-bit pid_t.
_PID = re.compile(r"[1-9][0-9]*")
_PID_MAX = 2**31 - 1

_ALLOWED = "-b/--buffer, --pid=, -e/--regex, -s, -D and filterspecs"
# The whitelisted spellings that take a value.
_VALUE_OPTIONS = frozenset({"-b", "--buffer", "-e", "--regex", "--pid"})
# Rejected options whose purpose logpyt owns, with the reason in the message.
_OWNED_OPTIONS = {
    "-v": "logpyt owns the output format",
    "--format": "logpyt owns the output format",
    "-d": "logpyt owns the start position and follow mode; use since=",
    "-t": "logpyt owns the start position and follow mode; use since=",
    "--tail": "logpyt owns the start position and follow mode; use since=",
    "-T": "logpyt owns the start position; use since=",
}

# logcat -T takes epoch seconds, which its log_time keeps in 32 unsigned bits.
_SINCE_MIN = datetime(1970, 1, 1, tzinfo=UTC)
_SINCE_END = _SINCE_MIN + timedelta(seconds=2**32)
# The numeric strings since= refuses: what pydantic's lax datetime parsing reads
# as epoch seconds ("100", "1.5", "1.0e2", ".1e3"), plus the exponent forms
# without a decimal point ("1e2"), which pydantic refuses with a vaguer message.
_EPOCH_STRING = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


@dataclass(frozen=True, slots=True)
class _LogcatArgs:
    """What a validated ``logcat_args`` tuple asks logcat to do.

    Attributes:
        buffers: The buffers logcat reads, with "default" and "all" expanded;
            logcat's own default set when no ``-b`` is given.
        pid: The ``--pid`` value (the last one wins, as on the device).
        regex: The ``-e``/``--regex`` value (the last one wins); an empty
            pattern is kept as ``""``, which matches every line.
        filtered: A filterspec or ``-s`` is present, so the argv builder must
            append ``logpyt:V`` for the markers to pass (section 5.6).
    """

    buffers: frozenset[str]
    pid: int | None
    regex: str | None
    filtered: bool

    def marker_hidden_by(self) -> tuple[str, ...]:
        """Name every argument that keeps logcat from printing logpyt's markers.

        A marker is written to the main buffer by a short-lived ``log``
        process (design section 5.6), so a ``--pid``, a ``--regex`` or a
        buffer set without main can hide it. Any regex counts, the empty one
        included: the rule of design section 2.2 does not look at the
        pattern. Empty when the markers are readable.
        """
        hidden_by: list[str] = []
        if self.pid is not None:
            hidden_by.append("--pid=")
        if self.regex is not None:
            hidden_by.append("-e/--regex")
        if "main" not in self.buffers:
            hidden_by.append("a -b set without main")
        return tuple(hidden_by)


def _option_name(arg: str) -> str:
    """Return the option an argument names: ``--x`` of ``--x=y``, ``-x`` of ``-xy``."""
    if arg.startswith("--"):
        return arg.partition("=")[0]
    return arg[:2]


def _check_filterspec(arg: str) -> None:
    """Validate one filterspec argument, which may hold several rules.

    Raises:
        ValueError: The argument holds no rule, a malformed rule, or a rule
            for the reserved marker tag.
    """
    rules = [rule for rule in _FILTER_SEPARATORS.split(arg) if rule]
    if not rules:
        msg = f"logcat_args: filterspec {arg!r} holds no rule; expected tag[:priority]"
        raise ValueError(msg)
    for rule in rules:
        match = _FILTER_RULE.fullmatch(rule)
        if match is None:
            msg = (
                f"logcat_args: filterspec rule {rule!r} is not tag[:priority] with "
                "priority one of V, D, I, W, E, F, S (either case), * or a digit 1-9"
            )
            raise ValueError(msg)
        if match["tag"] == _MARKER_TAG:
            msg = (
                f"logcat_args: the tag {_MARKER_TAG!r} is reserved for logpyt's start "
                f"and flush markers; remove {rule!r}"
            )
            raise ValueError(msg)


def _parse_logcat_args(args: Sequence[str]) -> _LogcatArgs:
    """Validate ``logcat_args`` against the whitelist and classify it.

    Raises:
        ValueError: An argument is outside the whitelist, malformed, or names
            the reserved marker tag (design section 2.2).
    """
    for arg in args:
        if "\0" in arg:
            msg = f"logcat_args: {arg!r} contains a NUL character"
            raise ValueError(msg)
    buffers: set[str] = set()
    pid: int | None = None
    regex: str | None = None
    filtered = False
    pending = iter(args)
    for arg in pending:
        if arg in {"-s", "-D"}:
            filtered = filtered or arg == "-s"
        elif not arg.startswith("-"):
            _check_filterspec(arg)
            filtered = True
        else:
            name, value = _option_value(arg, pending)
            if name in {"-b", "--buffer"}:
                buffers |= _parse_buffer(arg, value)
            elif name in {"-e", "--regex"}:
                # Any pattern, the empty one included: logcat hands it to
                # pcrecpp::RE unchecked (n-logcat.cpp:701-702).
                regex = value
            else:
                pid = _parse_pid(value)
    return _LogcatArgs(
        buffers=frozenset(buffers) or _DEFAULT_BUFFERS,
        pid=pid,
        regex=regex,
        filtered=filtered,
    )


def _option_value(arg: str, pending: Iterator[str]) -> tuple[str, str]:
    """Return a whitelisted option's spelling and value, taking it from ``pending``.

    Raises:
        ValueError: The option is not whitelisted, or its value is missing.
    """
    name = _option_name(arg)
    if name not in _VALUE_OPTIONS:
        reason = _OWNED_OPTIONS.get(name, "it is not on the whitelist")
        msg = f"logcat_args: {arg!r} is not allowed ({reason}); allowed: {_ALLOWED}"
        raise ValueError(msg)
    if arg != name:
        return name, arg[len(name) + 1 :] if name.startswith("--") else arg[2:]
    value = next(pending, None)
    if value is None:
        msg = f"logcat_args: {arg} needs a value"
        raise ValueError(msg)
    return name, value


def _parse_buffer(arg: str, value: str) -> frozenset[str]:
    """Return the buffers one ``-b`` value selects.

    Raises:
        ValueError: The value is not one buffer name, "default" or "all".
    """
    if value in _BUFFER_GROUPS:
        return _BUFFER_GROUPS[value]
    if value in _BUFFER_NAMES:
        return frozenset({value})
    names = ", ".join(sorted(_BUFFER_NAMES | _BUFFER_GROUPS.keys()))
    msg = (
        f"logcat_args: {arg} {value!r} is not a buffer name; expected one of "
        f"{names} (one per -b: Android 7.0 takes no comma lists)"
    )
    raise ValueError(msg)


def _parse_pid(value: str) -> int:
    """Return a ``--pid`` value: a plain decimal number from 1 to 2**31 - 1.

    Plain decimal is the only spelling on purpose. logcat reads the value with
    ``strtoll(..., 0)`` (n-logcat.cpp:397-417, 618-623), which would also take a
    sign and ``0x`` hex, and would read ``0123`` as octal 83; one unambiguous
    spelling reads the same to a human, to logpyt and to the device. The bound
    is the largest pid_t, which is 32 bits on Android: logcat itself refuses
    only what overflows ``strtoll`` and hands the value on to liblog as a pid.

    Raises:
        ValueError: The value is not plain decimal, or is outside 1 to 2**31 - 1.
    """
    if _PID.fullmatch(value) is None:
        msg = (
            f"logcat_args: --pid takes only a plain decimal pid (no sign, no "
            f"leading zero, no 0x) from 1 to {_PID_MAX}, got {value!r}"
        )
        raise ValueError(msg)
    # The length check keeps int() off strings past its digit limit.
    if len(value) > len(str(_PID_MAX)) or int(value) > _PID_MAX:
        msg = (
            f"logcat_args: --pid {value} is out of range: a plain decimal pid is "
            f"from 1 to {_PID_MAX} (pid_t is 32 bits on Android)"
        )
        raise ValueError(msg)
    return int(value)


def _validated_copy[M: BaseModel](model: M, update: Mapping[str, Any] | None) -> M:
    """Return ``model`` with ``update`` applied, validated like the constructor.

    Only the fields set on ``model`` are passed on, so the copy keeps
    ``model_fields_set`` as pydantic's own ``model_copy`` does.
    """
    fields = model.model_dump(exclude_unset=True)
    return type(model).model_validate({**fields, **(update or {})})


def _refuse_bool(value: object, info: ValidationInfo) -> object:
    """Refuse a bool for any field: none is a flag, and pydantic reads True as 1.

    Raises:
        ValueError: ``value`` is a bool.
    """
    if isinstance(value, bool):
        msg = f"{info.field_name} does not take a bool"
        if info.field_name == "reconnect":
            msg += "; pass Reconnect() to retry until stop(), or None"
        raise ValueError(msg)
    return value


class Reconnect(
    BaseModel,
    frozen=True,
    extra="forbid",
    allow_inf_nan=False,
    revalidate_instances="always",
):
    """Reconnect policy for a stream whose adb connection ends unrequested.

    The defaults retry until ``stop()``, backing off from 1 s to 30 s.
    ``model_copy(update=...)`` validates its result like the constructor.

    Attributes:
        delay: Seconds before the first retry.
        max_delay: Cap on the retry delay; at least ``delay``.
        backoff: Factor the delay grows by after each failed attempt.
        max_attempts: Consecutive failed attempts before the run fails;
            None retries until ``stop()``.
        stable_after: Seconds a connection must stay RUNNING, counted from its
            first output, before the attempts and the delay reset.
    """

    delay: PositiveFloat = 1.0
    max_delay: PositiveFloat = 30.0
    backoff: float = Field(2.0, ge=1.0)
    max_attempts: PositiveInt | None = None
    stable_after: PositiveFloat = 10.0

    @field_validator("*", mode="before")
    @classmethod
    def _reject_bool(cls, value: object, info: ValidationInfo) -> object:
        return _refuse_bool(value, info)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """Return a copy with ``update`` applied, validated like the constructor.

        pydantic's own ``model_copy`` skips validation. ``deep`` changes
        nothing here, because every field is immutable.
        """
        return _validated_copy(self, update)

    @model_validator(mode="after")
    def _check_delays(self) -> Self:
        if self.max_delay < self.delay:
            msg = (
                f"max_delay ({self.max_delay}) must be at least delay ({self.delay}), "
                "or the first retry waits longer than the cap"
            )
            raise ValueError(msg)
        return self


class StreamConfig(
    BaseModel,
    frozen=True,
    extra="forbid",
    allow_inf_nan=False,
    revalidate_instances="always",
):
    """Configuration of one stream run, shared by `LogStream` and `AsyncLogStream`.

    Durations are seconds; buffer and window sizes are ``>= 1`` (design D-15).
    Validation errors are pydantic ``ValidationError``s, which are
    ``ValueError``s; a bare ``str`` or another non-sequence for ``logcat_args``
    raises ``TypeError``. ``model_copy(update=...)`` (and ``copy.replace`` on
    Python 3.13+) validates its result like the constructor.

    Attributes:
        adb_path: The adb executable; None resolves it when the stream is
            constructed.
        serial: The device serial; None leaves the choice to adb
            (``ANDROID_SERIAL``, then the only device).
        logcat_args: Extra logcat arguments from the whitelist: ``-b``/
            ``--buffer``, ``--pid=``, ``-e``/``--regex``, ``-s``, ``-D`` and
            filterspecs. logpyt owns the output format and the start position,
            and the tag ``logpyt`` is reserved for its markers.
        since: Where the first connection starts: the device backlog, ``"now"``
            (exact, by a start marker), or an aware datetime. A number or a
            numeric string (``"100"``, ``"1.5"``, ``"1.0e2"``, also as bytes)
            is rejected, not read as epoch seconds.
        read_timeout: Seconds without bytes on stdout or stderr while RUNNING
            before the connection counts as dead; None disables the check.
        connect_timeout: Seconds a new connection may stay silent before logpyt
            checks whether the device is there.
        stall_timeout: Seconds the intake may stay closed by backpressure before
            logpyt disconnects and later resumes; None disables the stall resume.
        reconnect: The reconnect policy; None ends the run at the first
            unrequested connection end.
        resume_overlap: Seconds of overlap requested when resuming.
        resume_window_lines: Cap on the lines kept to deduplicate a resume.
        resume_window_bytes: Cap on the bytes kept to deduplicate a resume.
        buffer_entries: Outbox capacity in entries; a group counts its members.
        buffer_bytes: Outbox capacity in bytes.
        max_line_bytes: Longer lines are truncated; at least 4096.
        stop_grace: Seconds between terminating adb and killing it.
        exit_drain: Seconds to wait for pipe EOF after adb exited, counted only
            while the intake is open.
        close_timeout: Seconds the consumer may take no item during a
            with-exit after an exception before the stream is killed.
        package_hold: Seconds a line may wait for package attribution.
        stderr_tail: adb stderr lines kept for error messages.
    """

    adb_path: str | None = None
    serial: str | None = None
    logcat_args: tuple[str, ...] = ()
    since: Literal["backlog", "now"] | AwareDatetime = "backlog"
    read_timeout: PositiveFloat | None = None
    connect_timeout: PositiveFloat = 10.0
    stall_timeout: PositiveFloat | None = 1.0
    reconnect: Reconnect | None = None
    resume_overlap: PositiveFloat = 2.0
    resume_window_lines: PositiveInt = 32_768
    resume_window_bytes: PositiveInt = 4 * 1024 * 1024
    buffer_entries: PositiveInt = 10_000
    buffer_bytes: PositiveInt = 16 * 1024 * 1024
    max_line_bytes: int = Field(64 * 1024, ge=4096)
    stop_grace: PositiveFloat = 2.0
    exit_drain: PositiveFloat = 2.0
    close_timeout: PositiveFloat = 5.0
    package_hold: NonNegativeFloat = 2.0
    stderr_tail: NonNegativeInt = 20

    @field_validator("*", mode="before")
    @classmethod
    def _reject_bool(cls, value: object, info: ValidationInfo) -> object:
        return _refuse_bool(value, info)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """Return a copy with ``update`` applied, validated like the constructor.

        pydantic's own ``model_copy`` skips validation, so an unknown key or an
        invalid value would pass. ``deep`` changes nothing here, because every
        field is immutable.
        """
        return _validated_copy(self, update)

    @field_validator("since", mode="before")
    @classmethod
    def _refuse_number(cls, value: object) -> object:
        # pydantic's lax datetime parsing reads a number or a numeric string
        # (_EPOCH_STRING) as epoch seconds, so since=100 would start in 1970
        # instead of failing.
        # A bool is left to _reject_bool, which names it.
        text = value.decode("latin-1") if isinstance(value, bytes) else value
        is_number = isinstance(value, Number) and not isinstance(value, bool)
        if is_number or (
            isinstance(text, str) and _EPOCH_STRING.fullmatch(text) is not None
        ):
            msg = (
                f'since takes "backlog", "now" or an aware datetime, not the number '
                f"{value!r}: for epoch seconds pass datetime.fromtimestamp(seconds, "
                "UTC); a line count (logcat -t/-T <count>) has no since= equivalent"
            )
            raise ValueError(msg)
        return value

    @field_validator("logcat_args", mode="before")
    @classmethod
    def _require_sequence(cls, value: object) -> object:
        if isinstance(value, str | bytes | bytearray):
            msg = (
                "logcat_args takes a sequence of arguments, not one string: "
                "write ('-b', 'crash'), not '-b crash'"
            )
            raise TypeError(msg)
        if not isinstance(value, Sequence):
            msg = (
                "logcat_args takes a list or tuple of str, "
                f"not {type(value).__name__}: the order of the arguments matters"
            )
            raise TypeError(msg)
        return value

    @field_validator("logcat_args")
    @classmethod
    def _check_logcat_args(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _parse_logcat_args(value)
        return value

    @field_validator("adb_path", "serial")
    @classmethod
    def _check_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and (not value or "\0" in value):
            msg = f"{info.field_name} must be a non-empty string without NUL, or None"
            raise ValueError(msg)
        return value

    @field_validator("since")
    @classmethod
    def _check_since(
        cls, value: Literal["backlog", "now"] | datetime
    ) -> Literal["backlog", "now"] | datetime:
        if isinstance(value, datetime) and not _SINCE_MIN <= value < _SINCE_END:
            msg = (
                f"since={value.isoformat()} is outside logcat's -T range "
                f"[{_SINCE_MIN.isoformat()}, {_SINCE_END.isoformat()})"
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_start_marker(self) -> Self:
        if self.since == "now":
            hidden_by = _parse_logcat_args(self.logcat_args).marker_hidden_by()
            if hidden_by:
                msg = (
                    'since="now" needs its start marker, which '
                    f"{' and '.join(hidden_by)} in logcat_args can hide; "
                    'remove it or use since="backlog" or an aware datetime'
                )
                raise ValueError(msg)
        return self
