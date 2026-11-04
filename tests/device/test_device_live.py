"""Re-run the non-disruptive probes on a live device and re-check the facts.

The same assertions as the fixture tests (``tests/device_facts.py``) run on a
fresh recording; on the emulator build that was recorded (API 24 and 36), the
build-specific facts are also compared with the stored recording of that
level. Disruptive and slow probes (C04 to C07, C09) are re-recorded only with
``tools/device_probe.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests import device_facts as facts
from tests.device_facts import level_dir, load, step
from tools import device_probe

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.device

LIVE_KEYS = (
    "R1",
    "R2",
    "R3",
    "R4",
    "R5",
    "R6",
    "C02",
    "C03",
    "C08",
    "C10",
    "C11",
    "C12",
)
# Below the 120 s per-test watchdog of tests/conftest.py, which exits without
# running finalizers: a device that stops answering mid-probe makes every adb
# call wait out its timeout, and the probe must end (and stop its adb
# children) first. The slowest healthy probe, R4, takes about 20 s.
LIVE_TIME_LIMIT_S = 90.0


def _ring_sizes(rec: dict[str, Any]) -> list[str]:
    return [line.split(" (")[0] for line in step(rec, "ring_default")["stdout"]]


# Facts that must equal the stored recording of the same emulator build.
SAME_AS_RECORDED: dict[str, Callable[[dict[str, Any]], object]] = {
    "R4": lambda rec: {name: value["rc"] for name, value in rec["facts"].items()},
    "R5": _ring_sizes,
    "C03": lambda rec: (
        step(rec, "pm_list_packages_U")["stdout"][:1] == ["Error: Unknown option: -U"]
    ),
    "C10": lambda rec: rec["facts"]["date_N_supported"],
    "C11": lambda rec: [
        rec["facts"]["lines_in_system"],
        rec["facts"]["lines_in_crash"],
    ],
    "C12": lambda rec: rec["facts"]["tags_silent"]["lines"],
}


@pytest.mark.parametrize("key", LIVE_KEYS)
def test_live_device_still_shows_the_recorded_fact(probe_ctx, key):
    with probe_ctx.time_limit(LIVE_TIME_LIMIT_S):
        recording = device_probe.PROBES_BY_KEY[key].run(probe_ctx)
    live = json.loads(json.dumps(recording.data))
    facts.CHECKS[key](live)
    project = SAME_AS_RECORDED.get(key)
    if project is not None:
        facts.skip_unless_recorded_build(live, "the comparison with the recording")
        recorded = load(level_dir(probe_ctx.api), key)
        assert project(live) == project(recorded)
