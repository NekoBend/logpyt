"""Tests for the live ``adb logcat`` command line (design 5.6, U-1)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from logpyt._core.argv import WIRE_FORMAT, epoch_position, format_position, logcat_argv
from logpyt._core.protocol import Spawn
from tests.core.harness import ADB, CLOCK, CLOCK_OK, MARKER_OK, Harness, config
from tests.core.scenarios import RandomDriver

if TYPE_CHECKING:
    from logpyt._core.protocol import Command

_ZONE_WORDS = {"zone", "UTC", "GMT"}


def test_argv_order_library_options_first_and_marker_spec_last() -> None:
    argv = logcat_argv(
        ADB, config(serial="emulator-5554", logcat_args=("-b", "main", "App:I")), (5, 7)
    )

    assert argv == (
        ADB,
        "-s",
        "emulator-5554",
        "logcat",
        *WIRE_FORMAT,
        "-T",
        "5.000007",
        "-b",
        "main",
        "App:I",
        "logpyt:V",
    )


@pytest.mark.parametrize(
    ("args", "appended"),
    [((), False), (("-b", "crash"), False), (("-s",), True), (("App:I",), True)],
    ids=["none", "buffer", "silent", "filterspec"],
)
def test_marker_filterspec_appended_only_when_filtering(
    args: tuple[str, ...],
    appended: bool,  # noqa: FBT001
) -> None:
    """logpyt:V is appended when a filterspec or -s could hide the markers."""
    argv = logcat_argv(ADB, config(logcat_args=args), None)

    assert (argv[-1] == "logpyt:V") is appended
    assert "-T" not in argv


def test_wire_format_has_exactly_one_zone_modifier() -> None:
    """U-1: -v zone is a toggle on the device, so exactly one zone word appears."""
    values = [WIRE_FORMAT[i + 1] for i in range(0, len(WIRE_FORMAT), 2)]

    assert WIRE_FORMAT[::2] == ("-v",) * len(values)
    assert values == ["threadtime", "year", "usec", "zone", "uid"]
    assert sum(value in _ZONE_WORDS for value in values) == 1


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 11, 4, 5, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 4, 5, 0, 0, 1, tzinfo=UTC),
        datetime(2026, 11, 4, 14, 0, 0, 999_999, tzinfo=timezone(timedelta(hours=9))),
        datetime(1970, 1, 1, tzinfo=UTC),
    ],
    ids=["whole", "one-us", "zoned", "epoch"],
)
def test_every_T_value_has_a_fraction(moment: datetime) -> None:  # noqa: N802
    """A digits-only -T is a line count (n-logcat.cpp:672-693): never emit one."""
    value = format_position(epoch_position(moment))

    seconds, _, micros = value.partition(".")
    assert len(micros) == 6
    expected = moment.astimezone(UTC).replace(tzinfo=None) - datetime(1970, 1, 1)  # noqa: DTZ001
    assert int(seconds) * 1_000_000 + int(micros) == expected // timedelta(
        microseconds=1
    )


def test_core_positions_have_a_fraction() -> None:
    """The -T of since="now" (whole clock seconds) still carries ".000000"."""
    harness = Harness.make(since="now")
    harness.start()
    harness.answer("clock", CLOCK_OK)
    harness.answer("start_marker", MARKER_OK)

    argv = harness.spawns[1].argv

    assert argv[argv.index("-T") + 1] == f"{CLOCK - 1}.000000"


def test_random_runs_emit_every_T_with_a_fraction() -> None:  # noqa: N802
    """Every -T the core emits, resumes and re-anchors included (design 5.6)."""
    values = [
        command.argv[command.argv.index("-T") + 1]
        for seed in range(200)
        for command in _run(seed)
        if isinstance(command, Spawn) and "-T" in command.argv
    ]

    assert len(values) > 100
    assert all(re.fullmatch(r"\d+\.\d{6}", value) for value in values), values


def _run(seed: int) -> list[Command]:
    driver = RandomDriver(seed)
    driver.run(steps=250)
    return driver.harness.commands


def test_position_checks() -> None:
    with pytest.raises(ValueError, match="aware"):
        epoch_position(datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match=r"0\.\.999999"):
        format_position((1, 1_000_000))
