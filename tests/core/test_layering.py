"""The core is pure (design 5.2): no module under logpyt/_core imports I/O,
threads, asyncio, subprocesses or the clock.

The check reads the source with ``ast`` and imports nothing, so it sees every
import statement: at module level, inside functions, and under
``if TYPE_CHECKING:``.

It checks imports only, not calls. The core does call one clock indirectly: the
pipeline takes wall-clock time from the caller's ``ParseContext.now()`` (whose
default reads the host clock, F6-4). tests/core/test_pipeline.py
(``test_wall_time_comes_only_from_the_context``) pins that this is the only
wall-clock read.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import logpyt._core

# Design 5.2: the modules the core must not import.
BANNED = frozenset({"threading", "asyncio", "subprocess", "time", "os", "socket"})
CORE = Path(logpyt._core.__file__).parent


def _banned_imports(source: str) -> list[tuple[int, str]]:
    """``(line, module)`` of every import of a banned module in ``source``."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        found.extend(
            (node.lineno, name) for name in names if name.split(".")[0] in BANNED
        )
    return found


def _core_modules() -> list[Path]:
    return sorted(CORE.rglob("*.py"))


def test_core_imports_no_io_modules() -> None:
    """No module under logpyt/_core imports a banned module (design 5.2)."""
    modules = _core_modules()
    names = {path.name for path in modules}
    # An empty or misplaced scan must not pass vacuously.
    assert {"__init__.py", "framing.py", "grouping.py", "pipeline.py"} <= names

    offences = [
        f"{path.relative_to(CORE.parent)}:{line}: imports {module}"
        for path in modules
        for line, module in _banned_imports(path.read_text(encoding="utf-8"))
    ]

    assert offences == []


@pytest.mark.parametrize(
    "source",
    [
        "import os",
        "import os.path",
        "import time as clock",
        "import json, socket",
        "from os import path",
        "from os.path import join",
        "from time import monotonic",
        "from asyncio import Queue",
        "def f():\n    import threading",
        "if TYPE_CHECKING:\n    from subprocess import Popen",
        "try:\n    import socket\nexcept ImportError:\n    pass",
    ],
    ids=[
        "import",
        "dotted",
        "alias",
        "list",
        "from",
        "from-dotted",
        "from-time",
        "from-asyncio",
        "in-function",
        "type-checking",
        "in-try",
    ],
)
def test_checker_finds_every_import_form(source: str) -> None:
    """The checker catches each way a module can be imported."""
    assert len(_banned_imports(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "import datetime",
        "from datetime import timedelta",
        "import timeit_like",
        "from .framing import Framer",
        "from logpyt.models import LogEntry",
        "import ostruct",
    ],
    ids=["datetime", "from-datetime", "prefix", "relative", "absolute", "os-prefix"],
)
def test_checker_allows_other_modules(source: str) -> None:
    """Modules that only share a prefix with a banned one are allowed."""
    assert _banned_imports(source) == []
