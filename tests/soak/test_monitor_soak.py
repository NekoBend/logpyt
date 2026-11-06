"""Hours-long monitoring soak (design 9.1), for both drivers.

``LOGPYT_SOAK_SECONDS=120 pytest -m soak tests/soak`` is the CI smoke (the
``soak-smoke`` job); ``LOGPYT_SOAK_SECONDS=7200`` the 2-hour release run
(section 8.4). The same code runs both: `Plan.of` scales the warm-up, the
comparison windows and the sample interval to the duration. Each run writes
its samples and numbers to ``soak-<driver>.json`` in the test's temporary
directory, to ``LOGPYT_SOAK_OUT`` (a directory) when that is set, and prints
the summary (``-s``). The scenario and the criteria are in ``harness.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.soak.harness import Plan, run_soak

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.conftest import FakeAdb

DURATION = float(os.environ.get("LOGPYT_SOAK_SECONDS", "120"))
# The run, the end (the tail's delivery, the stop), and the judgement.
MARGIN = 180.0


@pytest.mark.watchdog(DURATION + MARGIN)
@pytest.mark.parametrize("driver", ["sync", "async"])
def test_monitor_soak(
    driver: str,
    fake_adb: FakeAdb,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """Design 9.1: memory, thread/task/fd and child counts stay flat over the
    run, no seq is delivered twice, every missing seq lies inside a reported
    Gap, and the line-conservation invariant holds."""
    result = run_soak(fake_adb, Plan.of(DURATION), driver)

    summary = result.summary()
    text = json.dumps(summary, indent=1, default=str)
    (tmp_path / f"soak-{driver}.json").write_text(text, encoding="utf-8")
    out = os.environ.get("LOGPYT_SOAK_OUT", "").strip()
    if out:
        target = Path(out)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"soak-{driver}.json").write_text(text, encoding="utf-8")
    record_property("soak", summary)
    brief = {key: value for key, value in summary.items() if key != "samples"}
    print(f"soak ({driver}): {json.dumps(brief, default=str)}")
    assert result.failures == []
