"""The core is pure (design 5.2): no module under logpyt/_core imports I/O,
threads, asyncio, subprocesses or the clock, or reads the wall clock itself.

The checks read the source with ``ast`` and import nothing. The import check
sees every import statement (at module level, inside functions, under
``if TYPE_CHECKING:``), a banned module imported by name from another module
(``from logpyt.adb import subprocess``, ``from . import os``), and a call of
``__import__`` or ``import_module``. The clock check sees ``now()``,
``utcnow()`` and ``today()`` called on ``datetime`` or ``date``.

They do not see a clock read or I/O done by a function of a module that is not
banned (``logpyt.parsers.context.host_now``, ``Path.read_bytes``, the builtin
``open``), the clock class under another name (``from datetime import datetime
as dt``), or a module reached through ``getattr``, ``exec`` or ``sys.modules``.

The pipeline takes wall-clock time from the caller's ``ParseContext.now()``,
whose default reads the host clock (F6-4). tests/core/test_pipeline.py
(``test_wall_time_comes_only_from_the_context``) pins only that the pipeline
dates lines through ``ctx.now()`` and calls no ``host_now`` when the context
fixes ``now()``; a clock read of the core's own is caught here, in the forms
above, or not at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import logpyt._core

# Design 5.2: the modules the core must not import, and the thread and I/O
# modules that reach the same things by other names.
BANNED = frozenset(
    {
        "threading",
        "asyncio",
        "subprocess",
        "time",
        "os",
        "socket",
        "_thread",
        "concurrent",
        "multiprocessing",
        "select",
        "selectors",
    }
)
# Banned names that a from-import takes from a module where they are no module.
_NOT_MODULES = frozenset({("datetime", "time")})
# Calls that import the module their first argument names.
_IMPORT_CALLS = frozenset({"__import__", "import_module"})
# datetime.now() and its kin read the wall clock.
_CLOCK_READS = frozenset({"now", "utcnow", "today"})
_CLOCK_CLASSES = frozenset({"datetime", "date"})
CORE = Path(logpyt._core.__file__).parent


def _is_banned(module: str) -> bool:
    return module.split(".")[0] in BANNED


def _name_of(node: ast.expr) -> str | None:
    """The last name of ``node``: ``x`` for ``x`` and for ``a.b.x``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _imported_by_call(call: ast.Call) -> str | None:
    """The banned module a call of ``__import__``/``import_module`` imports;
    ``"<dynamic>"`` when its name is not a string literal.
    """
    target = call.args[0] if call.args else None
    if target is None:
        target = next((kw.value for kw in call.keywords if kw.arg == "name"), None)
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return target.value if _is_banned(target.value) else None
    return "<dynamic>"


def _banned_imports(source: str) -> list[tuple[int, str]]:
    """``(line, module)`` of every import of a banned module in ``source``."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names if _is_banned(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and _is_banned(module):
                names = [module]
            else:
                # A banned module re-exported by another one, or a relative
                # import of one: from logpyt.adb import subprocess.
                names = [
                    alias.name
                    for alias in node.names
                    if alias.name in BANNED and (module, alias.name) not in _NOT_MODULES
                ]
        elif isinstance(node, ast.Call) and _name_of(node.func) in _IMPORT_CALLS:
            module = _imported_by_call(node)
            names = [] if module is None else [module]
        else:
            continue
        found.extend((node.lineno, name) for name in names)
    return found


def _clock_reads(source: str) -> list[tuple[int, str]]:
    """``(line, call)`` of every ``now()``/``utcnow()``/``today()`` called on
    ``datetime`` or ``date`` in ``source``.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = _name_of(node.func.value)
        if node.func.attr in _CLOCK_READS and owner in _CLOCK_CLASSES:
            found.append((node.lineno, f"{owner}.{node.func.attr}()"))
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


def test_core_reads_no_clock() -> None:
    """No module under logpyt/_core calls datetime.now() or its kin (design 5.2,
    A.19: the caller's ParseContext.now() is the only wall-clock source).
    """
    offences = [
        f"{path.relative_to(CORE.parent)}:{line}: calls {call}"
        for path in _core_modules()
        for line, call in _clock_reads(path.read_text(encoding="utf-8"))
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
        "from time import time",
        "from logpyt.adb import subprocess",
        "from logpyt.adb import subprocess as sp",
        "from . import os",
        "from ..readers import os",
        "__import__('time')",
        "importlib.import_module('os.path')",
        "from importlib import import_module\nimport_module('subprocess')",
        "import_module(name='socket')",
        "import_module(name)",
        "import _thread",
        "from concurrent.futures import ThreadPoolExecutor",
        "import multiprocessing",
        "import selectors",
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
        "time-time",
        "reexport",
        "reexport-as",
        "relative",
        "relative-up",
        "dunder",
        "importlib",
        "import-module",
        "keyword",
        "dynamic",
        "thread",
        "concurrent",
        "processes",
        "selectors",
    ],
)
def test_checker_finds_every_import_form(source: str) -> None:
    """The checker catches each import statement form, a banned module imported
    by name from another module, and __import__/import_module calls, once each.
    """
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
        "from datetime import time",
        "from datetime import UTC, time as clock_time",
        "importlib.import_module('json')",
        "from . import framing",
    ],
    ids=[
        "datetime",
        "from-datetime",
        "prefix",
        "relative",
        "absolute",
        "os-prefix",
        "datetime-time",
        "datetime-time-as",
        "import-json",
        "relative-module",
    ],
)
def test_checker_allows_other_modules(source: str) -> None:
    """Modules that only share a prefix with a banned one are allowed, and so is
    datetime's time class.
    """
    assert _banned_imports(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "datetime.now(UTC)",
        "datetime.datetime.now()",
        "datetime.utcnow()",
        "date.today()",
        "def f():\n    return dt.datetime.today()",
    ],
    ids=["now", "module-now", "utcnow", "today", "in-function"],
)
def test_clock_checker_finds_each_read(source: str) -> None:
    """The clock check catches now(), utcnow() and today() on datetime or date."""
    assert len(_clock_reads(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "ctx.now()",
        "self._ctx.now()",
        "datetime.fromtimestamp(0, tz=UTC)",
        "moment.astimezone()",
        "datetime(2026, 1, 1)",
    ],
    ids=["ctx", "self-ctx", "fromtimestamp", "astimezone", "constructor"],
)
def test_clock_checker_allows_other_calls(source: str) -> None:
    """The context's now() and datetime arithmetic are no clock reads of the core."""
    assert _clock_reads(source) == []
