"""Validation, defaults and immutability of `StreamConfig` and `Reconnect`.

Every rule of design section 2.2 has a test here, plus the guards the design
leaves implicit (bools, inf/nan, NUL characters, the -T epoch range).
"""

from __future__ import annotations

import copy
import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    NewType,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)

import pytest
from pydantic import ValidationError

from logpyt import Reconnect, StreamConfig
from logpyt.config import _parse_logcat_args

if TYPE_CHECKING:
    from collections.abc import Callable

STREAM_DEFAULTS: dict[str, object] = {
    "adb_path": None,
    "serial": None,
    "logcat_args": (),
    "since": "backlog",
    "read_timeout": None,
    "connect_timeout": 10.0,
    "stall_timeout": 1.0,
    "reconnect": None,
    "resume_overlap": 2.0,
    "resume_window_lines": 32_768,
    "resume_window_bytes": 4 * 1024 * 1024,
    "buffer_entries": 10_000,
    "buffer_bytes": 16 * 1024 * 1024,
    "max_line_bytes": 64 * 1024,
    "stop_grace": 2.0,
    "exit_drain": 2.0,
    "close_timeout": 5.0,
    "package_hold": 2.0,
    "stderr_tail": 20,
}
RECONNECT_DEFAULTS: dict[str, object] = {
    "delay": 1.0,
    "max_delay": 30.0,
    "backoff": 2.0,
    "max_attempts": None,
    "stable_after": 10.0,
}
STREAM_FLOATS = [
    "read_timeout",
    "connect_timeout",
    "stall_timeout",
    "resume_overlap",
    "stop_grace",
    "exit_drain",
    "close_timeout",
    "package_hold",
]
RECONNECT_FLOATS = ["delay", "max_delay", "backoff", "stable_after"]
# Every float field (a duration, or Reconnect.backoff), with its model.
FLOAT_FIELDS: dict[str, type[StreamConfig] | type[Reconnect]] = {
    **dict.fromkeys(STREAM_FLOATS, StreamConfig),
    **dict.fromkeys(RECONNECT_FLOATS, Reconnect),
}
# Every int field, with its model and an int it accepts.
INT_FIELDS: dict[str, tuple[type[StreamConfig] | type[Reconnect], int]] = {
    "resume_window_lines": (StreamConfig, 8192),
    "resume_window_bytes": (StreamConfig, 8192),
    "buffer_entries": (StreamConfig, 8192),
    "buffer_bytes": (StreamConfig, 8192),
    "max_line_bytes": (StreamConfig, 8192),
    "stderr_tail": (StreamConfig, 8192),
    "max_attempts": (Reconnect, 3),
}
DEFAULT_BUFFERS = frozenset({"main", "system", "crash"})
ALL_BUFFERS = frozenset(
    {
        "main",
        "system",
        "radio",
        "events",
        "crash",
        "security",
        "kernel",
    }
)


def _rejects(
    make: Callable[[], object], match: str, error: type[Exception] = ValueError
) -> None:
    with pytest.raises(error, match=match):
        make()


def _config(**fields: object) -> StreamConfig:
    """Validate ``fields`` like the constructor does, for inputs ty would reject."""
    return StreamConfig.model_validate(fields)


def _reconnect(**fields: object) -> Reconnect:
    """Validate ``fields`` like the constructor does, for inputs ty would reject."""
    return Reconnect.model_validate(fields)


def _errors(make: Callable[[], object]) -> list[tuple[object, str]]:
    """Return the location and type of each error ``make`` raises."""
    with pytest.raises(ValidationError) as caught:
        make()
    return [(error["loc"], error["type"]) for error in caught.value.errors()]


def _leaf_types(annotation: object) -> set[object]:
    """Return the types ``annotation`` names, through aliases, Annotated and unions.

    A container is a leaf: ``tuple[PositiveFloat, ...]`` yields the alias
    itself, so the int and float guards do not find a number inside one.
    """
    if isinstance(annotation, TypeAliasType):
        return _leaf_types(annotation.__value__)
    if isinstance(annotation, NewType):
        return _leaf_types(annotation.__supertype__)
    if get_origin(annotation) is Annotated:
        return _leaf_types(get_args(annotation)[0])
    if get_origin(annotation) in {Union, UnionType}:
        return set().union(*(_leaf_types(arg) for arg in get_args(annotation)))
    return {annotation}


def _int_fields() -> list[tuple[type[StreamConfig] | type[Reconnect], str]]:
    """Return every field of both models whose annotation names int or a subclass."""
    return [
        (model, name)
        for model in (StreamConfig, Reconnect)
        for name, info in model.model_fields.items()
        if any(
            isinstance(leaf, type) and issubclass(leaf, int) and leaf is not bool
            for leaf in _leaf_types(info.annotation)
        )
    ]


def _float_fields() -> list[tuple[type[StreamConfig] | type[Reconnect], str]]:
    """Return every field of both models whose annotation names float or a subclass."""
    return [
        (model, name)
        for model in (StreamConfig, Reconnect)
        for name, info in model.model_fields.items()
        if any(
            isinstance(leaf, type) and issubclass(leaf, float)
            for leaf in _leaf_types(info.annotation)
        )
    ]


# -- defaults, immutability, unknown fields ----------------------------------------


def test_stream_config_fields_and_defaults_are_the_designed_ones() -> None:
    assert list(StreamConfig.model_fields) == list(STREAM_DEFAULTS)
    assert StreamConfig().model_dump() == STREAM_DEFAULTS


def test_reconnect_fields_and_defaults_are_the_designed_ones() -> None:
    assert list(Reconnect.model_fields) == list(RECONNECT_DEFAULTS)
    assert Reconnect().model_dump() == RECONNECT_DEFAULTS


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (StreamConfig(), "serial", "emulator-5554"),
        (StreamConfig(), "logcat_args", ("-s",)),
        (Reconnect(), "delay", 5.0),
    ],
)
def test_models_are_frozen(model: object, field: str, value: object) -> None:
    with pytest.raises(ValidationError, match="frozen"):
        setattr(model, field, value)


def test_equal_configs_are_equal_and_hash_alike() -> None:
    since = datetime(2026, 11, 4, tzinfo=UTC)
    first = StreamConfig(
        since=since, logcat_args=("-b", "crash"), reconnect=Reconnect()
    )
    second = _config(since=since, logcat_args=["-b", "crash"], reconnect=Reconnect())
    assert first == second
    assert hash(first) == hash(second)
    assert first != StreamConfig()


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (StreamConfig, "serail"),
        (StreamConfig, "max_queue_size"),
        (StreamConfig, "pid_poll_interval"),
        (StreamConfig, "queue_overflow_policy"),
        (Reconnect, "on_gap"),
    ],
)
def test_unknown_fields_are_rejected(model: Callable[..., object], field: str) -> None:
    _rejects(lambda: model(**{field: 1}), "Extra inputs are not permitted")


def test_nested_reconnect_validates_from_a_mapping() -> None:
    config = StreamConfig.model_validate(
        {
            "reconnect": {"delay": 2.0, "max_attempts": 3},
            "logcat_args": ["-b", "main"],
        }
    )
    assert config.reconnect == Reconnect(delay=2.0, max_attempts=3)
    assert config.logcat_args == ("-b", "main")


# -- model_copy and instance revalidation ----------------------------------------


@pytest.mark.parametrize(
    ("model", "update", "match"),
    [
        (StreamConfig(), {"serail": "x"}, "Extra inputs are not permitted"),
        (StreamConfig(), {"buffer_entries": 0}, "greater than 0"),
        (StreamConfig(), {"serial": ""}, "non-empty string without NUL"),
        (StreamConfig(), {"reconnect": True}, "does not take a bool"),
        (StreamConfig(), {"since": "now", "logcat_args": ("--pid=1",)}, "marker"),
        (Reconnect(), {"delay": -1}, "greater than 0"),
        (Reconnect(), {"on_gap": 1}, "Extra inputs are not permitted"),
        (Reconnect(), {"delay": 60.0}, "max_delay"),
    ],
)
def test_model_copy_validates_its_update(
    model: StreamConfig | Reconnect, update: dict[str, object], match: str
) -> None:
    _rejects(lambda: model.model_copy(update=update), match)


def test_model_copy_keeps_the_fields_and_the_fields_set() -> None:
    config = StreamConfig(serial="emulator-5554", reconnect=Reconnect(delay=2.0))
    copied = config.model_copy(update={"buffer_entries": 5, "since": "now"})
    assert copied == StreamConfig(
        serial="emulator-5554",
        reconnect=Reconnect(delay=2.0),
        buffer_entries=5,
        since="now",
    )
    assert copied.model_fields_set == {"serial", "reconnect", "buffer_entries", "since"}
    assert copied.reconnect is not None
    assert copied.reconnect.model_fields_set == {"delay"}
    assert config.model_copy() == config
    assert config.model_copy(deep=True) == config


def test_model_copy_validates_a_mapping_into_the_nested_model() -> None:
    copied = StreamConfig().model_copy(update={"reconnect": {"max_attempts": 3}})
    assert copied.reconnect == Reconnect(max_attempts=3)


def test_copy_replace_validates_like_the_constructor() -> None:
    if sys.version_info < (3, 13):
        pytest.skip("copy.replace is new in Python 3.13")
    assert copy.replace(StreamConfig(), serial="x") == StreamConfig(serial="x")
    _rejects(lambda: copy.replace(StreamConfig(), serail="x"), "Extra inputs")


def test_unvalidated_instances_are_revalidated() -> None:
    unchecked_reconnect = Reconnect.model_construct(delay=-1)
    _rejects(lambda: StreamConfig(reconnect=unchecked_reconnect), "greater than 0")
    unchecked_config = StreamConfig.model_construct(buffer_entries=0)
    _rejects(lambda: StreamConfig.model_validate(unchecked_config), "greater than 0")


# -- numeric constraints ---------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "connect_timeout",
        "read_timeout",
        "stall_timeout",
        "resume_overlap",
        "stop_grace",
        "exit_drain",
        "close_timeout",
        "resume_window_lines",
        "resume_window_bytes",
        "buffer_entries",
        "buffer_bytes",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_stream_positive_fields_reject_zero_and_negative(
    field: str, value: int
) -> None:
    _rejects(lambda: _config(**{field: value}), "greater than 0")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connect_timeout", 0.001),
        ("read_timeout", 0.001),
        ("stall_timeout", 0.001),
        ("resume_overlap", 0.001),
        ("stop_grace", 0.001),
        ("exit_drain", 0.001),
        ("close_timeout", 0.001),
        ("resume_window_lines", 1),
        ("resume_window_bytes", 1),
        ("buffer_entries", 1),
        ("buffer_bytes", 1),
        ("package_hold", 0),
        ("stderr_tail", 0),
        ("max_line_bytes", 4096),
    ],
)
def test_stream_numeric_fields_accept_their_lower_bound(
    field: str, value: float
) -> None:
    assert getattr(_config(**{field: value}), field) == value


@pytest.mark.parametrize("field", ["package_hold", "stderr_tail"])
def test_non_negative_fields_reject_negative(field: str) -> None:
    _rejects(lambda: _config(**{field: -1}), "greater than or equal to 0")


def test_max_line_bytes_has_a_floor_of_4096() -> None:
    _rejects(lambda: StreamConfig(max_line_bytes=4095), "greater than or equal to 4096")


@pytest.mark.parametrize("field", INT_FIELDS)
@pytest.mark.parametrize(
    "kind", ["float", "str", "bytes", "decimal", "fraction", "bool"]
)
def test_int_fields_take_only_an_int(field: str, kind: str) -> None:
    """Like read_file(max_line_bytes=) (design A.19): no lax coercion to an int."""
    model, value = INT_FIELDS[field]
    raw = {
        "float": float(value),
        "str": str(value),
        "bytes": str(value).encode(),
        "decimal": Decimal(value),
        "fraction": Fraction(value),
        "bool": True,
    }[kind]
    # A bool is refused by _refuse_bool, which runs before the int check.
    error = "value_error" if kind == "bool" else "int_type"
    assert _errors(lambda: model.model_validate({field: raw})) == [((field,), error)]
    assert _errors(lambda: model().model_copy(update={field: raw})) == [
        ((field,), error)
    ]
    if sys.version_info >= (3, 13):
        assert _errors(lambda: copy.replace(model(), **{field: raw})) == [
            ((field,), error)
        ]
    assert getattr(model.model_validate({field: value}), field) == value


@pytest.mark.parametrize("field", INT_FIELDS)
@pytest.mark.parametrize("kind", ["str", "float"])
def test_int_fields_take_only_a_json_int(field: str, kind: str) -> None:
    model, value = INT_FIELDS[field]
    refused = json.dumps({field: str(value) if kind == "str" else float(value)})
    assert _errors(lambda: model.model_validate_json(refused)) == [
        ((field,), "int_type")
    ]
    accepted = json.dumps({field: value})
    assert getattr(model.model_validate_json(accepted), field) == value


def test_int_fields_are_the_listed_ones() -> None:
    assert {name for _, name in _int_fields()} == set(INT_FIELDS)


@pytest.mark.parametrize(
    ("model", "field"), [pytest.param(*pair, id=pair[1]) for pair in _int_fields()]
)
@pytest.mark.parametrize("raw", [8192.0, "8192"], ids=["float", "str"])
def test_every_int_field_is_strict(
    model: type[StreamConfig] | type[Reconnect], field: str, raw: object
) -> None:
    """A guard for int fields added later: the annotation alone finds them."""
    assert _errors(lambda: model.model_validate({field: raw})) == [
        ((field,), "int_type")
    ]


@pytest.mark.parametrize(
    "field", ["adb_path", "serial", "read_timeout", "stall_timeout", "reconnect"]
)
def test_optional_fields_accept_none(field: str) -> None:
    assert getattr(_config(**{field: None}), field) is None


@pytest.mark.parametrize(
    "field", ["delay", "max_delay", "stable_after", "max_attempts"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_reconnect_positive_fields_reject_zero_and_negative(
    field: str, value: int
) -> None:
    _rejects(lambda: _reconnect(**{field: value}), "greater than 0")


def test_reconnect_backoff_is_at_least_one() -> None:
    _rejects(lambda: Reconnect(backoff=0.99), "greater than or equal to 1")
    assert Reconnect(backoff=1.0).backoff == pytest.approx(1.0)


def test_reconnect_max_attempts_accepts_one_and_none() -> None:
    assert Reconnect(max_attempts=1).max_attempts == 1
    assert Reconnect(max_attempts=None).max_attempts is None


def test_reconnect_max_delay_below_delay_is_rejected() -> None:
    _rejects(lambda: Reconnect(delay=5.0, max_delay=4.0), "max_delay")
    assert Reconnect(delay=5.0, max_delay=5.0).max_delay == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("make", "field"),
    [(StreamConfig, field) for field in STREAM_FLOATS]
    + [(Reconnect, field) for field in RECONNECT_FLOATS],
)
@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_float_fields_reject_inf_and_nan(
    make: Callable[..., object], field: str, value: float
) -> None:
    _rejects(lambda: make(**{field: value}), field)


@pytest.mark.parametrize("field", FLOAT_FIELDS)
@pytest.mark.parametrize("kind", ["str", "bytes"])
def test_float_fields_refuse_text(field: str, kind: str) -> None:
    """A duration is a number of seconds: no lax parsing of "5" or b"5" (A.23)."""
    model = FLOAT_FIELDS[field]
    raw = {"str": "5", "bytes": b"5"}[kind]
    assert _errors(lambda: model.model_validate({field: raw})) == [
        ((field,), "float_type")
    ]
    assert _errors(lambda: model().model_copy(update={field: raw})) == [
        ((field,), "float_type")
    ]
    if sys.version_info >= (3, 13):
        assert _errors(lambda: copy.replace(model(), **{field: raw})) == [
            ((field,), "float_type")
        ]


@pytest.mark.parametrize("field", FLOAT_FIELDS)
@pytest.mark.parametrize(
    "value",
    [5, 5.5, Decimal("5.5"), Fraction(11, 2)],
    ids=["int", "float", "decimal", "fraction"],
)
def test_float_fields_take_an_int_or_a_float(
    field: str, value: float | Decimal | Fraction
) -> None:
    """A Decimal or a Fraction is taken too; each is stored as a plain float."""
    model = FLOAT_FIELDS[field]
    taken = getattr(model.model_validate({field: value}), field)
    assert taken == value
    assert type(taken) is float
    assert getattr(model().model_copy(update={field: value}), field) == value


@pytest.mark.parametrize("field", FLOAT_FIELDS)
@pytest.mark.parametrize("value", [5, 5.5], ids=["int", "float"])
def test_float_fields_take_only_a_json_number(field: str, value: float) -> None:
    model = FLOAT_FIELDS[field]
    refused = json.dumps({field: str(value)})
    assert _errors(lambda: model.model_validate_json(refused)) == [
        ((field,), "float_type")
    ]
    accepted = json.dumps({field: value})
    assert getattr(model.model_validate_json(accepted), field) == value


def test_float_fields_are_the_listed_ones() -> None:
    assert {name: model for model, name in _float_fields()} == FLOAT_FIELDS


@pytest.mark.parametrize(
    ("model", "field"), [pytest.param(*pair, id=pair[1]) for pair in _float_fields()]
)
@pytest.mark.parametrize("raw", ["5", b"5"], ids=["str", "bytes"])
def test_every_float_field_is_strict(
    model: type[StreamConfig] | type[Reconnect], field: str, raw: object
) -> None:
    """A guard for float fields added later: the annotation alone finds them."""
    assert _errors(lambda: model.model_validate({field: raw})) == [
        ((field,), "float_type")
    ]


@pytest.mark.parametrize(
    ("model", "field", "raw", "taken"),
    [
        pytest.param(model, field, "5", 5.0, id=field)
        for field, model in FLOAT_FIELDS.items()
    ]
    + [
        pytest.param(model, field, str(value), value, id=field)
        for field, (model, value) in INT_FIELDS.items()
    ],
)
def test_number_fields_follow_a_callers_strict_false(
    model: type[StreamConfig] | type[Reconnect], field: str, raw: str, taken: float
) -> None:
    """A caller's explicit strict=False keeps pydantic's lax meaning (A.23)."""
    assert getattr(model.model_validate({field: raw}, strict=False), field) == taken
    lax_bytes = model.model_validate({field: raw.encode()}, strict=False)
    assert getattr(lax_bytes, field) == taken
    lax_json = model.model_validate_json(json.dumps({field: raw}), strict=False)
    assert getattr(lax_json, field) == taken


@pytest.mark.parametrize(
    ("field", "raw", "error"),
    [pytest.param(field, "5", "float_type", id=field) for field in RECONNECT_FLOATS]
    + [pytest.param("max_attempts", "3", "int_type", id="max_attempts")],
)
def test_a_nested_reconnect_mapping_is_as_strict(
    field: str, raw: str, error: str
) -> None:
    """The error names the nested field, also through model_copy and JSON."""
    nested = {"reconnect": {field: raw}}
    errors = [(("reconnect", field), error)]
    assert _errors(lambda: StreamConfig.model_validate(nested)) == errors
    assert _errors(lambda: StreamConfig().model_copy(update=nested)) == errors
    raw_json = json.dumps(nested)
    assert _errors(lambda: StreamConfig.model_validate_json(raw_json)) == errors


@pytest.mark.parametrize(
    ("make", "field"),
    [(StreamConfig, field) for field in STREAM_DEFAULTS if field != "logcat_args"]
    + [(Reconnect, field) for field in RECONNECT_DEFAULTS],
)
@pytest.mark.parametrize("value", [True, False])
def test_no_field_takes_a_bool(
    make: Callable[..., object], field: str, value: object
) -> None:
    _rejects(lambda: make(**{field: value}), "does not take a bool")


def test_a_bool_reconnect_points_to_the_reconnect_policy() -> None:
    _rejects(lambda: _config(reconnect=True), "pass Reconnect\\(\\)")


# -- adb_path and serial ---------------------------------------------------------


@pytest.mark.parametrize("field", ["adb_path", "serial"])
@pytest.mark.parametrize("value", ["", "emu\0lator"])
def test_adb_path_and_serial_reject_empty_and_nul(field: str, value: str) -> None:
    _rejects(lambda: _config(**{field: value}), "non-empty string without NUL")


def test_adb_path_and_serial_keep_their_value() -> None:
    config = StreamConfig(adb_path="/opt/android/adb", serial="emulator-5554")
    assert (config.adb_path, config.serial) == ("/opt/android/adb", "emulator-5554")


# -- logcat_args: accepted -------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("-b", "main"),
        ("-bcrash",),
        ("--buffer", "system"),
        ("--buffer=events",),
        ("-b", "radio"),
        ("-b", "security"),
        ("-b", "kernel"),
        ("-b", "default"),
        ("-b", "all"),
        ("--pid=1234",),
        ("--pid", "1234"),
        ("--pid=2147483647",),
        ("-e", "FATAL|ANR"),
        ("-eFATAL",),
        ("--regex", "x"),
        ("--regex=x",),
        ("-e", "-starts-with-a-dash"),
        ("-s",),
        ("-D",),
        ("ActivityManager:I",),
        ("*:S",),
        ("*:s",),
        ("*",),
        ("MyApp",),
        ("ActivityManager:I MyApp:D *:S",),
        ("A:I,B:D",),
        ("A:i\tB:*",),
        # Empty tokens between separators are skipped, as on the device.
        ("A:I  B:D",),
        ("A:I,B:D,",),
        (" A:I",),
        ("-s", "MyApp:V"),
        ("MyApp:V", "-b", "crash"),
        ("-b", "crash", "-b", "main", "-D", "-s", "Tag:W"),
        # Numeric priorities, as filterCharToPri maps them on the device.
        ("Tag:5",),
        ("*:1",),
        ("A:1 B:2,C:3\tD:4",),
        ("A:6,B:7 C:8\tD:9",),
        ("*:9", "Tag:7"),
    ],
)
def test_whitelisted_logcat_args_are_accepted(args: tuple[str, ...]) -> None:
    assert StreamConfig(logcat_args=args).logcat_args == args


def test_logcat_args_list_is_stored_as_a_tuple() -> None:
    assert _config(logcat_args=["-b", "crash"]).logcat_args == ("-b", "crash")


# -- logcat_args: rejected -------------------------------------------------------


@pytest.mark.parametrize(
    "arg",
    [
        *("-v", "--format", "-d", "-t", "-T", "-c", "-f", "-r", "-n", "-g", "-G"),
        *("-S", "-p", "-P", "-L", "-B"),
    ],
)
def test_options_the_design_names_are_rejected(arg: str) -> None:
    _rejects(lambda: StreamConfig(logcat_args=(arg,)), "is not allowed")


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (("-v", "brief"), "output format"),
        (("-vlong",), "output format"),
        (("--format=long",), "output format"),
        (("--format", "long"), "output format"),
        (("-d",), "since="),
        (("-t", "100"), "since="),
        (("-t100",), "since="),
        (("--tail=5",), "since="),
        (("-T", "5"), "since="),
        (("-T1793760496.667655",), "since="),
    ],
)
def test_format_and_position_options_name_the_reason(
    args: tuple[str, ...], reason: str
) -> None:
    _rejects(lambda: StreamConfig(logcat_args=args), reason)


@pytest.mark.parametrize(
    "arg",
    [
        "--dividers",
        "-sD",
        "-Ds",
        "--",
        "-",
        "--pi=1",
        "--pidx=1",
        "--buffers=main",
        "--buffer-size",
        "--grep=x",
        "-m",
        "--max-count=1",
        "--head=1",
        "--print",
        "--wrap",
        "--clear",
        "--last",
        "--binary",
        "--statistics",
        "--prune",
        "--file=/sdcard/log",
        "-Q",
    ],
)
def test_other_options_and_spellings_are_rejected(arg: str) -> None:
    _rejects(lambda: StreamConfig(logcat_args=(arg,)), "is not allowed")


@pytest.mark.parametrize("arg", ["-b", "--buffer", "-e", "--regex", "--pid"])
def test_value_options_without_a_value_are_rejected(arg: str) -> None:
    _rejects(lambda: StreamConfig(logcat_args=(arg,)), "needs a value")
    _rejects(lambda: StreamConfig(logcat_args=("-s", arg)), "needs a value")


@pytest.mark.parametrize(
    "args",
    [
        ("-b", "Main"),
        ("-b", "main,system"),
        ("-bmain,crash",),
        ("--buffer=",),
        ("-b", "-s"),
        ("-b", "logs"),
    ],
)
def test_unknown_buffer_names_are_rejected(args: tuple[str, ...]) -> None:
    _rejects(lambda: StreamConfig(logcat_args=args), "is not a buffer name")


@pytest.mark.parametrize("args", [("-e", ""), ("--regex", ""), ("--regex=",)])
def test_an_explicitly_empty_regex_is_accepted(args: tuple[str, ...]) -> None:
    # logcat hands any pattern to pcrecpp::RE, and the empty one matches every line.
    assert StreamConfig(logcat_args=args).logcat_args == args
    assert _parse_logcat_args(args).regex == ""


@pytest.mark.parametrize(
    "value",
    [
        *("0", "00", "-1", "-0", "abc", "", " 12", "12 ", "12\n", "1.5", "1e3"),
        # Spellings strtoll(..., 0) reads on the device, refused on purpose.
        *("+12", "+1234", "0x1f", "0x4d2", "0X4D2", "0123", "01234"),
        "\uff11\uff12",  # full-width digits, which int() would read
    ],
)
def test_pid_must_be_a_plain_decimal_of_at_least_one(value: str) -> None:
    match = "only a plain decimal pid"
    _rejects(lambda: StreamConfig(logcat_args=(f"--pid={value}",)), match)
    _rejects(lambda: StreamConfig(logcat_args=("--pid", value)), match)


@pytest.mark.parametrize(
    "value", ["2147483648", "4294967296", "9223372036854775808", "9" * 5000]
)
def test_pid_above_the_32_bit_pid_t_range_is_rejected(value: str) -> None:
    match = "out of range: a plain decimal pid is from 1 to 2147483647"
    _rejects(lambda: StreamConfig(logcat_args=(f"--pid={value}",)), match)
    _rejects(lambda: StreamConfig(logcat_args=("--pid", value)), match)


@pytest.mark.parametrize("args", [("--pid=2147483647",), ("--pid", "2147483647")])
def test_pid_accepts_the_largest_32_bit_pid(args: tuple[str, ...]) -> None:
    assert _parse_logcat_args(args).pid == 2**31 - 1


@pytest.mark.parametrize("arg", ["", " ", " , ", "\t,"])
def test_filterspec_without_a_rule_is_rejected(arg: str) -> None:
    _rejects(lambda: StreamConfig(logcat_args=(arg,)), "holds no rule")


@pytest.mark.parametrize(
    "arg",
    [
        *(":V", "Tag:", "Tag:X", "Tag:V:x", "A:I B:Q", "A:I,:D"),
        # Priority characters logcat refuses: "0" is ANDROID_LOG_UNKNOWN.
        *("Tag:0", "*:0", "A:I B:0", "Tag:-", "Tag:+", "Tag:\u0665"),
        # logcat ignores what follows the priority character; logpyt refuses it.
        *("Tag:VV", "Tag:10", "Tag:5x"),
    ],
)
def test_malformed_filterspec_rules_are_rejected(arg: str) -> None:
    _rejects(lambda: StreamConfig(logcat_args=(arg,)), "is not tag\\[:priority\\]")


def test_malformed_filterspec_message_lists_the_numeric_priorities() -> None:
    _rejects(lambda: StreamConfig(logcat_args=("Tag:0",)), "\\* or a digit 1-9")


@pytest.mark.parametrize(
    "arg",
    [
        *("logpyt", "logpyt:S", "logpyt:V", "A:I logpyt:V", "logpyt:s,*:S"),
        *("logpyt:1", "logpyt:5", "*:9 logpyt:8"),
    ],
)
def test_filterspec_naming_the_marker_tag_is_rejected(arg: str) -> None:
    _rejects(lambda: StreamConfig(logcat_args=(arg,)), "reserved")


def test_filterspec_tags_are_case_sensitive_like_logcat() -> None:
    assert StreamConfig(logcat_args=("LogPyt:S",)).logcat_args == ("LogPyt:S",)


@pytest.mark.parametrize("args", [("a\0b",), ("-e", "a\0b"), ("--pid=1\0",)])
def test_nul_characters_are_rejected(args: tuple[str, ...]) -> None:
    _rejects(lambda: StreamConfig(logcat_args=args), "NUL")


@pytest.mark.parametrize(
    "value",
    [
        "-b crash",
        b"-s",
        bytearray(b"-s"),
        {"-s"},
        frozenset({"-s"}),
        {"-s": 1},
        (arg for arg in ["-s"]),
        True,
    ],
)
def test_non_sequence_logcat_args_raise_type_error(value: object) -> None:
    _rejects(lambda: _config(logcat_args=value), "logcat_args takes", TypeError)


def test_non_str_logcat_args_items_are_rejected() -> None:
    _rejects(lambda: _config(logcat_args=("-b", 1)), "valid string")


# -- logcat_args: classification used by the argv builder and the markers -------


def test_no_args_reads_the_default_buffers_without_filters() -> None:
    parsed = _parse_logcat_args(())
    assert parsed.buffers == DEFAULT_BUFFERS
    assert (parsed.pid, parsed.regex, parsed.filtered) == (None, None, False)
    assert parsed.marker_hidden_by() == ()


@pytest.mark.parametrize(
    ("args", "buffers"),
    [
        (("-b", "crash"), frozenset({"crash"})),
        (("-b", "crash", "--buffer=main"), frozenset({"crash", "main"})),
        (("-b", "crash", "-b", "crash"), frozenset({"crash"})),
        (("-b", "default"), DEFAULT_BUFFERS),
        (("-b", "events", "-b", "default"), DEFAULT_BUFFERS | {"events"}),
        (("-ball",), ALL_BUFFERS),
    ],
)
def test_buffers_are_the_union_of_every_b(
    args: tuple[str, ...], buffers: frozenset[str]
) -> None:
    assert _parse_logcat_args(args).buffers == buffers


def test_the_last_pid_and_regex_win_as_on_the_device() -> None:
    parsed = _parse_logcat_args(("--pid=7", "-e", "a", "--pid", "9", "--regex=b"))
    assert (parsed.pid, parsed.regex) == (9, "b")


@pytest.mark.parametrize(
    ("args", "filtered"),
    [(("-s",), True), (("Tag:I",), True), (("-D",), False), (("-e", "-s"), False)],
)
def test_filtered_means_a_filterspec_or_s(
    args: tuple[str, ...], *, filtered: bool
) -> None:
    assert _parse_logcat_args(args).filtered is filtered


def test_a_separate_value_is_taken_verbatim_even_with_a_dash() -> None:
    assert _parse_logcat_args(("-e", "-s")).regex == "-s"


@pytest.mark.parametrize(
    ("args", "hidden_by"),
    [
        (("--pid=7",), ("--pid=",)),
        (("-e", "x"), ("-e/--regex",)),
        (("-e", ""), ("-e/--regex",)),
        (("--regex=",), ("-e/--regex",)),
        (("-b", "crash"), ("a -b set without main",)),
        (
            ("--pid=7", "--regex=x", "-b", "events"),
            ("--pid=", "-e/--regex", "a -b set without main"),
        ),
        (("-b", "crash", "-b", "main", "-s", "Tag:I", "-D"), ()),
    ],
)
def test_marker_hidden_by_names_every_blocking_argument(
    args: tuple[str, ...], hidden_by: tuple[str, ...]
) -> None:
    assert _parse_logcat_args(args).marker_hidden_by() == hidden_by


# -- since -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "since",
    [
        "backlog",
        "now",
        datetime(2026, 11, 4, 2, 48, 35, 826893, tzinfo=UTC),
        datetime(2026, 11, 4, 11, 48, tzinfo=timezone(timedelta(hours=9))),
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(2106, 2, 7, 6, 28, 15, 999999, tzinfo=UTC),
    ],
)
def test_since_accepts_the_literals_and_aware_datetimes(since: object) -> None:
    assert _config(since=since).since == since


def test_since_accepts_an_iso_string_with_an_offset() -> None:
    since = _config(since="2026-11-04T11:48:00+09:00").since
    assert since == datetime(2026, 11, 4, 2, 48, tzinfo=UTC)


@pytest.mark.parametrize(
    "since",
    [
        "Now",
        "later",
        "",
        "2026-11-04T11:48:00",
        datetime(2026, 11, 4),  # noqa: DTZ001  the naive datetime under test
    ],
)
def test_since_rejects_other_strings_and_naive_datetimes(since: object) -> None:
    _rejects(lambda: _config(since=since), "since")


@pytest.mark.parametrize(
    "since",
    [
        100,
        100.0,
        0,
        -100,
        1_793_760_496,
        Decimal(100),
        Fraction(1, 2),
        "100",
        "100.0",
        "100.",
        ".5",
        "+100",
        "-100",
        "1793760496",
        b"100",
    ],
)
def test_since_rejects_numbers_instead_of_reading_epoch_seconds(since: object) -> None:
    _rejects(lambda: _config(since=since), "not the number")
    _rejects(lambda: _config(since=since), "datetime.fromtimestamp")


@pytest.mark.parametrize("since", ["100", "1793760496"])
def test_since_rejects_a_number_from_json(since: str) -> None:
    _rejects(
        lambda: StreamConfig.model_validate_json(f'{{"since": {since}}}'), "number"
    )
    _rejects(
        lambda: StreamConfig.model_validate_json(f'{{"since": "{since}"}}'), "number"
    )


# pydantic reads the forms with a decimal point as epoch seconds ("1.0e2" is
# 1970-01-01T00:01:40Z) and refuses the others with a vaguer message.
EXPONENT_STRINGS = [
    *("1e2", "1E2", "1e+2", "1E-3", "+1e2", "-1e2"),
    *("1.0e2", "1.e2", ".1e3", "1.0E+2", "1.0e-2", "+1.0e2", "-1.0e2", "+.1E3"),
    "1.7937604966e9",
]


@pytest.mark.parametrize(
    "since", [*EXPONENT_STRINGS, *(text.encode() for text in EXPONENT_STRINGS)]
)
@pytest.mark.parametrize(
    "make", [StreamConfig, _config], ids=["constructor", "model_validate"]
)
def test_since_rejects_numbers_in_exponent_notation(
    make: Callable[..., StreamConfig], since: str | bytes
) -> None:
    _rejects(lambda: make(since=since), "not the number")


@pytest.mark.parametrize("since", EXPONENT_STRINGS)
def test_since_rejects_exponent_strings_from_json(since: str) -> None:
    _rejects(
        lambda: StreamConfig.model_validate_json(f'{{"since": "{since}"}}'),
        "not the number",
    )


@pytest.mark.parametrize("since", ["1e2", "1E-3", "1.0e2", "-1.0e2", "1.7937604966e9"])
def test_since_rejects_exponent_numbers_from_json(since: str) -> None:
    _rejects(
        lambda: StreamConfig.model_validate_json(f'{{"since": {since}}}'),
        "not the number",
    )


@pytest.mark.parametrize(
    "since",
    [
        datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(2106, 2, 7, 6, 28, 16, tzinfo=UTC),
        datetime(9999, 1, 1, tzinfo=UTC),
    ],
)
def test_since_outside_the_logcat_epoch_range_is_rejected(since: datetime) -> None:
    _rejects(lambda: _config(since=since), "outside logcat's -T range")


@pytest.mark.parametrize(
    "args",
    [
        ("--pid=1",),
        ("--pid", "1"),
        ("-e", "x"),
        ("--regex=x",),
        ("-b", "crash"),
        ("--buffer=events", "-b", "system"),
        # An empty regex counts too: the rule does not look at the pattern.
        ("-e", ""),
        ("--regex=",),
        ("--regex", ""),
    ],
)
def test_since_now_rejects_args_that_hide_the_start_marker(
    args: tuple[str, ...],
) -> None:
    _rejects(lambda: StreamConfig(since="now", logcat_args=args), "start marker")


def test_since_now_names_every_argument_that_hides_the_start_marker() -> None:
    _rejects(
        lambda: StreamConfig(since="now", logcat_args=("--pid=1", "-e", "x")),
        "--pid= and -e/--regex in logcat_args",
    )


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("-b", "main"),
        ("-b", "crash", "-b", "main"),
        ("-b", "default"),
        ("-b", "all"),
        ("-s",),
        ("Tag:I", "*:S"),
        ("-D",),
    ],
)
def test_since_now_accepts_args_that_keep_the_start_marker(
    args: tuple[str, ...],
) -> None:
    assert StreamConfig(since="now", logcat_args=args).logcat_args == args


@pytest.mark.parametrize(
    "since", ["backlog", datetime(2026, 11, 4, tzinfo=UTC)], ids=["backlog", "datetime"]
)
def test_only_since_now_depends_on_the_start_marker(since: object) -> None:
    args = ("--pid=1", "-e", "x", "-b", "crash")
    assert _config(since=since, logcat_args=args).logcat_args == args
