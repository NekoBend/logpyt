"""The monitoring soak (design 9.1): marked ``soak``, outside the default run.

Every test collected under this directory gets the ``soak`` marker (it is
registered in ``tests/conftest.py``). It runs only when the ``-m`` expression
names ``soak`` without negating it, as the CI job does::

    LOGPYT_SOAK_SECONDS=120 pytest -m soak tests/soak

and is skipped otherwise, also under ``-m "not device"``, which would select it.
``LOGPYT_SOAK_SECONDS`` sets the duration per driver (default 120 s, the CI
smoke; 7200 for the release run, section 8.4).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def selected(markexpr: str) -> bool:
    """Whether a ``-m`` expression asks for the soak: it names ``soak``, and not
    right after ``not``."""
    words = re.findall(r"[\w-]+", markexpr)
    return any(
        word == "soak" and (i == 0 or words[i - 1] != "not")
        for i, word in enumerate(words)
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Mark every test here ``soak`` (before ``-m`` selects), and skip them
    unless the run asked for the soak."""
    run = selected(config.option.markexpr or "")
    skip = pytest.mark.skip(reason="the soak runs only with -m soak (design 9.1)")
    for item in items:
        if item.path.resolve().is_relative_to(HERE):
            item.add_marker(pytest.mark.soak)
            if not run:
                item.add_marker(skip)
