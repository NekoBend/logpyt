"""The test-migration crosswalk rule (.docs/design-3.0.0.md, section 8.3).

``tests/legacy_inventory.txt`` lists every test function of logpyt 2.x: the 13
test modules of 2.0.0 plus ``tests/behavior/`` and ``tests/test_fake_adb.py``,
one function-level pytest node id per line (no parametrize suffix). It is a
frozen baseline. A PR that deletes or renames one of those tests adds a row to
``tests/CROSSWALK.md``::

    | old test id | replacement test id or reason |
    |---|---|
    | tests/test_stream.py::test_old | tests/conformance/test_x.py::test_new |
    | tests/test_stream.py::test_pause | API removed by decision D-7 |

The right cell is the replacement test id or one of the fixed reasons::

    API removed by decision <decision id>
    covered by <test id>
    duplicate of <test id>
    mocked private internals; behavior covered by <test id>

The checks: an inventory id that is no longer a test function needs a row;
a row names an inventory id that is no longer a test function (no stale rows)
and appears once; every test id a row names is a test function now. Decision
ids are checked for their form only: a decision of the design's ``D``, ``A``
or ``U`` family (``D-7``, ``A-12``, ``U-4``), not a defect id such as
``X-1``, because the design document is not part of the repository.

Test functions are found with ``ast``, without importing anything, following
pytest's default discovery: modules ``test_*.py`` and ``*_test.py`` under
``tests/``, functions ``test*`` (``async def`` included), and the methods of
classes ``Test*`` (nested ones too) that define no ``__init__`` or
``__new__``. Ids always use ``/``, on Windows too. For every id the rule looks
at (the inventory ids and the ids the crosswalk rows name),
``test_discovery_agrees_with_pytest_collection`` compares that with a
``pytest --collect-only`` run of ``tests/``: it reports a test that only
pytest sees (one defined inside an ``if`` block, an inherited method, an
alias) and one that pytest no longer collects (``collect_ignore``,
``__test__ = False``), which ``ast`` alone would count as present.

The inventory was generated once, at release/3.0.0 bc26785, where the 13
legacy modules are those of 2.0.0 (8810bf5) unchanged, by::

    uv run python -m tests.test_crosswalk --write-inventory tests/legacy_inventory.txt

Never regenerate or edit it: it is the 2.x baseline, and
``test_inventory_is_the_frozen_baseline`` pins its ids (``INVENTORY_COUNT``,
``INVENTORY_SHA256``). A deleted or renamed test keeps its line and gets a
row in ``tests/CROSSWALK.md``. The generator refuses to overwrite an existing
file unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import ast
import collections
import contextlib
import dataclasses
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence, Set

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "tests" / "legacy_inventory.txt"
CROSSWALK = ROOT / "tests" / "CROSSWALK.md"

# The frozen baseline: the number of ids in tests/legacy_inventory.txt at
# bc26785 and the SHA-256 of those ids joined by "\n" (see inventory_digest;
# comments, blank lines and line endings do not count). Never change them: a
# deleted or renamed legacy test keeps its inventory line and gets a row in
# tests/CROSSWALK.md.
INVENTORY_COUNT = 306
INVENTORY_SHA256 = "6f547187505e8e28534335eafaf662906a41e287d89504a3fdb7b1a99773cd8b"

# Bound for the pytest --collect-only run, below the hang watchdog's limit.
COLLECT_TIMEOUT = 60.0

# The test modules of 2.0.0 (origin/main 8810bf5).
LEGACY_MODULES = (
    "tests/test_async_stream.py",
    "tests/test_async_stream_timeout.py",
    "tests/test_exporters.py",
    "tests/test_filters.py",
    "tests/test_groupers.py",
    "tests/test_models.py",
    "tests/test_parsers.py",
    "tests/test_pid_monitor.py",
    "tests/test_readers.py",
    "tests/test_stream.py",
    "tests/test_utils.py",
    "tests/test_windowed_grouper_lazy.py",
    "tests/test_windowed_grouper_temp.py",
)
# The black-box suite and the harness tests of PR #2, written against 2.x.
HARNESS_SCOPE = ("tests/behavior/", "tests/test_fake_adb.py")

# A function-level pytest node id under tests/, with "/" separators.
TEST_ID = re.compile(
    r"tests/(?:[\w.-]+/)*[\w.-]+\.py(?:::[A-Za-z_]\w*)+", flags=re.ASCII
)
_TEST_PART = r"(?P<test>\S+)"
REASONS = (
    # The design's decision families: D-n (6.1), A-n (6.2) and U-n (1.2).
    re.compile(r"API removed by decision (?P<decision>[ADU]-[1-9][0-9]*)"),
    re.compile(rf"covered by {_TEST_PART}"),
    re.compile(rf"duplicate of {_TEST_PART}"),
    re.compile(rf"mocked private internals; behavior covered by {_TEST_PART}"),
)
REASON_FORMS = (
    "API removed by decision <decision id>",
    "covered by <test id>",
    "duplicate of <test id>",
    "mocked private internals; behavior covered by <test id>",
)
HEADER = ("old test id", "replacement test id or reason")
_SEPARATOR_CELL = re.compile(r":?-{3,}:?")


# -- discovery ---------------------------------------------------------------


def _is_test_module(name: str) -> bool:
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def _has_constructor(node: ast.ClassDef) -> bool:
    return any(
        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name in {"__init__", "__new__"}
        for item in node.body
    )


def _test_names(body: Sequence[ast.stmt], prefix: str = "") -> Iterator[str]:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                yield prefix + node.name
        elif (
            isinstance(node, ast.ClassDef)
            and node.name.startswith("Test")
            and not _has_constructor(node)
        ):
            yield from _test_names(node.body, f"{prefix}{node.name}::")


def discover_test_ids(root: Path) -> set[str]:
    """Return the function-level ids of the test functions under ``root/tests``."""
    found: set[str] = set()
    for path in sorted((root / "tests").rglob("*.py")):
        if not _is_test_module(path.name):
            continue
        module = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_bytes(), filename=str(path))
        found.update(f"{module}::{name}" for name in _test_names(tree.body))
    return found


# -- inventory ---------------------------------------------------------------


def in_inventory_scope(test_id: str) -> bool:
    """Whether ``test_id`` belongs to a module the inventory covers."""
    module = test_id.split("::", 1)[0]
    return module in LEGACY_MODULES or module.startswith(HARNESS_SCOPE)


def parse_inventory(text: str) -> tuple[list[str], list[str]]:
    """Return the inventory's ids and its format problems.

    Blank lines and ``#`` comments are skipped; ids must be well formed,
    unique and sorted.
    """
    ids: list[str] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if not TEST_ID.fullmatch(entry):
            problems.append(f"line {number}: {entry!r} is not a function-level id")
        ids.append(entry)
    duplicates = sorted(i for i, n in collections.Counter(ids).items() if n > 1)
    problems.extend(f"{test_id} is listed twice" for test_id in duplicates)
    if ids != sorted(ids):
        problems.append("the ids are not sorted")
    return ids, problems


def inventory_digest(ids: Sequence[str]) -> str:
    """Return the SHA-256 that ``INVENTORY_SHA256`` pins, for the ids in order."""
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def inventory_text(test_ids: Iterable[str]) -> str:
    """Render the inventory file for the in-scope ids among ``test_ids``."""
    ids = sorted(i for i in test_ids if in_inventory_scope(i))
    legacy = sum(i.split("::", 1)[0] in LEGACY_MODULES for i in ids)
    header = (
        "# Legacy test inventory (.docs/design-3.0.0.md, section 8.3): the test\n"
        "# functions of logpyt 2.x at release/3.0.0 bc26785, that is the 13 test\n"
        "# modules of 2.0.0 plus tests/behavior/ and tests/test_fake_adb.py.\n"
        f"# {len(ids)} ids: {legacy} in the 2.0.0 modules, {len(ids) - legacy} in"
        " tests/behavior/ and tests/test_fake_adb.py.\n"
        "# One function-level pytest node id per line (no parametrize suffix),\n"
        "# sorted. Frozen: a PR that deletes or renames one of these tests adds a\n"
        "# row to tests/CROSSWALK.md instead (checked by tests/test_crosswalk.py).\n"
        "# Generated once at bc26785 by: uv run python -m tests.test_crosswalk\n"
        "# --write-inventory tests/legacy_inventory.txt. Never regenerate or edit\n"
        "# it: tests/test_crosswalk.py pins these ids (INVENTORY_SHA256).\n"
    )
    return header + "".join(f"{test_id}\n" for test_id in ids)


# -- crosswalk ---------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Row:
    """One crosswalk row: an old test id and its replacement or reason."""

    line: int
    old: str
    target: str


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_table_line(line: str) -> bool:
    # Markdown renders a line indented by up to 3 spaces as a table row; four
    # or more make it an indented code block (an example, not a row).
    text = line.lstrip(" ")
    return text.startswith("|") and len(line) - len(text) < 4


def parse_crosswalk(text: str) -> tuple[list[Row], list[str]]:
    """Return the rows of the crosswalk table and its format problems.

    A table line is one that Markdown can render as a table row (``|`` after
    at most three spaces). The table is the run of consecutive table lines
    that starts at the first one: the header, the separator, then rows of two
    cells. A blank line or the end of the text ends it, and no table line may
    come later. Markdown renders a line right after the table as one more
    row, and a later table line (in a code fence, an HTML comment, after a
    blank line, a second table) is not a row of this table, so each is a
    problem: the rows read here are exactly the rows the rendered table shows.
    """
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if _is_table_line(line)), None
    )
    if start is None or tuple(_cells(lines[start])) != HEADER:
        return [], [
            f"the table must start with the header row | {' | '.join(HEADER)} |"
        ]
    end = start + 1
    while end < len(lines) and _is_table_line(lines[end]):
        end += 1
    problems: list[str] = []
    separator_cells = _cells(lines[start + 1]) if start + 1 < end else []
    if len(separator_cells) != len(HEADER) or not all(
        _SEPARATOR_CELL.fullmatch(cell) for cell in separator_cells
    ):
        problems.append(f"line {start + 2}: expected the separator row |---|---|")
    rows: list[Row] = []
    for index in range(start + 2, end):
        cells = _cells(lines[index])
        if len(cells) != len(HEADER) or not all(cells):
            problems.append(f"line {index + 1}: a row needs two non-empty cells")
            continue
        rows.append(Row(index + 1, cells[0], cells[1]))
    if end < len(lines) and lines[end].strip():
        problems.append(f"line {end + 1}: a blank line must end the table")
    problems.extend(
        f"line {index + 1}: a table line outside the crosswalk table"
        for index in range(end, len(lines))
        if _is_table_line(lines[index])
    )
    return rows, problems


def named_test_id(target: str) -> str | None:
    """Return the test id a right-hand cell names, or None for a removed API.

    Raises:
        ValueError: If ``target`` is neither a test id nor a fixed reason, or
            the reason names something that is not a test id.
    """
    if TEST_ID.fullmatch(target):
        return target
    for reason in REASONS:
        match = reason.fullmatch(target)
        if match is None:
            continue
        test_id = match.groupdict().get("test")
        if test_id is not None and not TEST_ID.fullmatch(test_id):
            msg = f"{test_id!r} in {target!r} is not a function-level test id"
            raise ValueError(msg)
        return test_id
    forms = "; ".join(REASON_FORMS)
    msg = f"{target!r} is neither a test id nor one of the reasons: {forms}"
    raise ValueError(msg)


def _target_problems(row: Row, existing: Set[str]) -> list[str]:
    try:
        test_id = named_test_id(row.target)
    except ValueError as exc:
        return [f"line {row.line}: {exc}"]
    if test_id is not None and test_id not in existing:
        return [f"line {row.line}: {test_id} is not a test function"]
    return []


def check_crosswalk(
    inventory: Sequence[str], rows: Sequence[Row], existing: Set[str]
) -> list[str]:
    """Return every violation of the crosswalk rule (empty when it holds).

    Args:
        inventory: The ids of ``tests/legacy_inventory.txt``.
        rows: The rows of ``tests/CROSSWALK.md``.
        existing: The ids of the test functions that exist now.
    """
    listed = set(inventory)
    first_row: dict[str, Row] = {}
    problems: list[str] = []
    for row in rows:
        if row.old in first_row:
            problems.append(
                f"line {row.line}: second row for {row.old}"
                f" (first on line {first_row[row.old].line})"
            )
            continue
        first_row[row.old] = row
        if row.old not in listed:
            problems.append(f"line {row.line}: {row.old} is not in the inventory")
        elif row.old in existing:
            problems.append(f"line {row.line}: stale row, {row.old} still exists")
        problems.extend(_target_problems(row, existing))
    problems.extend(
        f"{test_id} no longer exists and has no row in tests/CROSSWALK.md"
        for test_id in inventory
        if test_id not in existing and test_id not in first_row
    )
    return problems


# -- the rule on this repository ---------------------------------------------


def _read_inventory() -> list[str]:
    ids, problems = parse_inventory(INVENTORY.read_text(encoding="utf-8"))
    assert not problems, "\n".join(problems)
    return ids


def _read_crosswalk() -> list[Row]:
    rows, problems = parse_crosswalk(CROSSWALK.read_text(encoding="utf-8"))
    assert not problems, "\n".join(problems)
    return rows


def test_inventory_is_well_formed_and_in_scope() -> None:
    """The inventory parses cleanly and lists only modules it covers."""
    ids = _read_inventory()

    assert ids, "the inventory is empty"
    assert [i for i in ids if not in_inventory_scope(i)] == []


def test_inventory_is_the_frozen_baseline() -> None:
    """The inventory lists exactly the ids of bc26785: none dropped, renamed, added."""
    ids = _read_inventory()

    assert (len(ids), inventory_digest(ids)) == (INVENTORY_COUNT, INVENTORY_SHA256), (
        "tests/legacy_inventory.txt is frozen at bc26785: restore it instead of"
        " regenerating or editing it. A PR that deletes or renames one of its"
        " tests keeps the line and adds a row to tests/CROSSWALK.md."
    )


def test_crosswalk_table_is_well_formed() -> None:
    """tests/CROSSWALK.md holds one well-formed table."""
    _read_crosswalk()


def test_every_deleted_or_renamed_legacy_test_has_a_valid_row() -> None:
    """Each inventory id exists or has a crosswalk row whose target is valid."""
    problems = check_crosswalk(
        _read_inventory(), _read_crosswalk(), discover_test_ids(ROOT)
    )

    assert not problems, "\n".join(problems)


def _function_id(item: pytest.Function) -> str | None:
    path = Path(item.path).resolve()
    if not path.is_relative_to(ROOT):
        return None
    name = item.nodeid.split("::", 1)[1].split("[", 1)[0]
    return f"{path.relative_to(ROOT).as_posix()}::{name}"


def collected_test_ids(root: Path) -> set[str]:
    """Return the test ids a default ``pytest --collect-only`` finds in ``root``.

    A separate pytest process collects what a plain ``pytest`` run in ``root``
    would collect, so the result does not depend on what the current run
    selected (``-k``, a path, ``PYTEST_ADDOPTS``). No path is passed: an
    explicit path would override ``testpaths``, and a test that ``testpaths``
    leaves out is no longer run by CI.
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        f"--rootdir={root}",
    ]
    env = {**os.environ, "PYTEST_ADDOPTS": "", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        command,
        cwd=root,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=COLLECT_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, (
        f"pytest --collect-only exited with {result.returncode}:\n"
        f"{result.stdout}{result.stderr}"
    )
    ids: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            break  # the node ids end at the first blank line; a summary follows
        ids.add(line.split("[", 1)[0])
    return ids


def _named_test_ids(rows: Iterable[Row]) -> set[str]:
    """Return the test ids the right cells name (malformed ones are skipped)."""
    named: set[str] = set()
    for row in rows:
        with contextlib.suppress(ValueError):
            test_id = named_test_id(row.target)
            if test_id is not None:
                named.add(test_id)
    return named


def test_discovery_agrees_with_pytest_collection(
    request: pytest.FixtureRequest,
) -> None:
    """For every id the rule looks at, ast finds a test iff pytest collects it.

    Those ids are the inventory ids and the ids the crosswalk rows name. A
    test that only pytest sees would be reported as gone; one that pytest no
    longer collects (``collect_ignore``, ``__test__ = False``) would count as
    present without being run. Other tests may use any style pytest collects.
    """
    found = discover_test_ids(ROOT)
    collected = collected_test_ids(ROOT)
    node = request.node
    assert isinstance(node, pytest.Function)
    own_id = _function_id(node)
    # Both sides must at least see this test, or the comparison checks nothing.
    assert own_id in found, f"ast discovery does not find {own_id}"
    assert own_id in collected, f"pytest --collect-only does not list {own_id}"

    relevant = set(_read_inventory()) | _named_test_ids(_read_crosswalk())
    problems = [
        f"{test_id}: pytest collects it, but ast discovery does not find it"
        for test_id in sorted((collected - found) & relevant)
    ] + [
        f"{test_id}: ast discovery finds it, but pytest does not collect it"
        for test_id in sorted((found - collected) & relevant)
    ]

    assert not problems, "\n".join(problems)


# -- the checker on synthetic input ------------------------------------------


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def test_discovery_finds_module_level_sync_and_async_tests(tmp_path: Path) -> None:
    """Module-level ``def test*`` and ``async def test*`` are tests; others not."""
    source = (
        "def test_sync(): ...\n"
        "async def test_async(): ...\n"
        "def testcompact(): ...\n"
        "def helper_test(): ...\n"
        "def _test_private(): ...\n"
    )
    root = _tree(tmp_path, {"tests/test_a.py": source})

    assert discover_test_ids(root) == {
        "tests/test_a.py::test_sync",
        "tests/test_a.py::test_async",
        "tests/test_a.py::testcompact",
    }


def test_discovery_finds_methods_of_test_classes_and_nested_classes(
    tmp_path: Path,
) -> None:
    """Methods of ``Test*`` classes are tests, nested classes included."""
    source = (
        "class TestOuter:\n"
        "    def test_method(self): ...\n"
        "    async def test_async_method(self): ...\n"
        "    def helper(self): ...\n"
        "    class TestInner:\n"
        "        def test_deep(self): ...\n"
        "class Helper:\n"
        "    def test_not_collected(self): ...\n"
    )
    root = _tree(tmp_path, {"tests/test_a.py": source})

    assert discover_test_ids(root) == {
        "tests/test_a.py::TestOuter::test_method",
        "tests/test_a.py::TestOuter::test_async_method",
        "tests/test_a.py::TestOuter::TestInner::test_deep",
    }


def test_discovery_skips_test_classes_with_a_constructor(tmp_path: Path) -> None:
    """pytest does not collect a ``Test*`` class with ``__init__`` or ``__new__``."""
    source = (
        "class TestInit:\n"
        "    def __init__(self): ...\n"
        "    def test_a(self): ...\n"
        "class TestNew:\n"
        "    def __new__(cls): ...\n"
        "    def test_b(self): ...\n"
        "class TestPlain:\n"
        "    def test_c(self): ...\n"
    )
    root = _tree(tmp_path, {"tests/test_a.py": source})

    assert discover_test_ids(root) == {"tests/test_a.py::TestPlain::test_c"}


def test_discovery_reads_only_test_modules(tmp_path: Path) -> None:
    """Only ``test_*.py`` and ``*_test.py`` under tests/ are scanned."""
    test = "def test_x(): ...\n"
    root = _tree(
        tmp_path,
        {
            "tests/test_a.py": test,
            "tests/b_test.py": test,
            "tests/conftest.py": test,
            "tests/helpers.py": test,
            "tests/test_notes.txt": test,
            "bench/test_c.py": test,
        },
    )

    assert discover_test_ids(root) == {
        "tests/test_a.py::test_x",
        "tests/b_test.py::test_x",
    }


def test_discovery_ids_use_forward_slashes_in_subdirectories(tmp_path: Path) -> None:
    """A module in a subdirectory gets a ``/``-separated id on every platform."""
    root = _tree(tmp_path, {"tests/behavior/deep/test_b.py": "def test_y(): ...\n"})

    assert discover_test_ids(root) == {"tests/behavior/deep/test_b.py::test_y"}


def test_discovery_does_not_import_the_modules(tmp_path: Path) -> None:
    """A module that would fail at import is still read, not executed."""
    source = "raise SystemExit('imported')\ndef test_z(): ...\n"
    root = _tree(tmp_path, {"tests/test_a.py": source})

    assert discover_test_ids(root) == {"tests/test_a.py::test_z"}


def test_parse_inventory_skips_comments_and_blank_lines() -> None:
    """Comments and blank lines are not ids; the ids keep their order."""
    text = "# header\n\ntests/a/test_x.py::test_a\ntests/test_b.py::TestC::test_d\n"

    assert parse_inventory(text) == (
        ["tests/a/test_x.py::test_a", "tests/test_b.py::TestC::test_d"],
        [],
    )


@pytest.mark.parametrize(
    "bad_id",
    [
        "tests/test_a.py::test_x[1]",
        "tests\\test_a.py::test_x",
        "tests/test_a.py",
        "test_a.py::test_x",
        "tests/test_a.py::",
    ],
)
def test_parse_inventory_rejects_ids_that_are_not_function_level(bad_id: str) -> None:
    """A parametrize suffix, a backslash, or a missing function part is rejected."""
    _, problems = parse_inventory(f"{bad_id}\n")

    assert problems == [f"line 1: {bad_id!r} is not a function-level id"]


def test_parse_inventory_rejects_duplicates() -> None:
    """The same id twice is a problem."""
    _, problems = parse_inventory("tests/test_a.py::test_x\n" * 2)

    assert problems == ["tests/test_a.py::test_x is listed twice"]


def test_parse_inventory_rejects_unsorted_ids() -> None:
    """Ids out of order are a problem."""
    _, problems = parse_inventory("tests/test_b.py::test_x\ntests/test_a.py::test_x\n")

    assert problems == ["the ids are not sorted"]


def test_inventory_text_keeps_in_scope_ids_sorted_under_a_counted_header() -> None:
    """The generator lists only in-scope ids, sorted, and counts both groups."""
    text = inventory_text(
        [
            "tests/test_stream.py::test_b",
            "tests/test_config.py::test_new",
            "tests/behavior/test_x.py::test_a",
            "tests/test_stream.py::test_a",
        ]
    )
    ids, problems = parse_inventory(text)

    assert problems == []
    assert ids == [
        "tests/behavior/test_x.py::test_a",
        "tests/test_stream.py::test_a",
        "tests/test_stream.py::test_b",
    ]
    assert "# 3 ids: 2 in the 2.0.0 modules, 1 in" in text


def test_inventory_digest_ignores_comments_blank_lines_and_line_endings() -> None:
    """The pinned digest depends on the ids and their order, not on the layout."""
    ids = ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]
    crlf = "# header\r\n\r\n" + "\r\n".join(ids) + "\r\n"

    assert inventory_digest(parse_inventory(crlf)[0]) == inventory_digest(ids)
    assert inventory_digest(ids) != inventory_digest(ids[:1])
    assert inventory_digest(ids) != inventory_digest([*ids[:1], ids[1] + "_renamed"])


def test_main_writes_the_inventory_of_this_tree(tmp_path: Path) -> None:
    """``--write-inventory PATH`` writes the generator's text with LF newlines."""
    target = tmp_path / "inventory.txt"

    assert main(["--write-inventory", str(target)]) == 0
    assert target.read_bytes() == inventory_text(discover_test_ids(ROOT)).encode()


def test_main_refuses_to_overwrite_an_existing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``--force`` an existing file is left alone and the exit status is 2."""
    target = tmp_path / "inventory.txt"
    target.write_text("kept\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        main(["--write-inventory", str(target)])

    assert exit_info.value.code == 2
    assert target.read_text(encoding="utf-8") == "kept\n"
    assert "--force" in capsys.readouterr().err


def test_main_overwrites_an_existing_file_with_force(tmp_path: Path) -> None:
    """``--force`` replaces an existing file with the generator's text."""
    target = tmp_path / "inventory.txt"
    target.write_text("old\n", encoding="utf-8")

    assert main(["--write-inventory", str(target), "--force"]) == 0
    assert target.read_bytes() == inventory_text(discover_test_ids(ROOT)).encode()


def test_collected_test_ids_respect_testpaths(tmp_path: Path) -> None:
    """A module that ``testpaths`` leaves out is found by ast but not collected."""
    root = _tree(
        tmp_path,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests/kept\n",
            "tests/kept/test_a.py": "def test_x(): ...\n",
            "tests/dropped/test_b.py": "def test_y(): ...\n",
        },
    )

    assert collected_test_ids(root) == {"tests/kept/test_a.py::test_x"}
    assert discover_test_ids(root) == {
        "tests/kept/test_a.py::test_x",
        "tests/dropped/test_b.py::test_y",
    }


def test_collected_test_ids_leave_out_a_module_pytest_ignores(tmp_path: Path) -> None:
    """A module that ``collect_ignore`` hides is found by ast but not collected."""
    parametrized = (
        "import pytest\n@pytest.mark.parametrize('n', [1, 2])\ndef test_x(n): ...\n"
    )
    root = _tree(
        tmp_path,
        {
            "pytest.ini": "[pytest]\n",
            "tests/conftest.py": "collect_ignore = ['test_b.py']\n",
            "tests/test_a.py": parametrized,
            "tests/test_b.py": "def test_y(): ...\n",
        },
    )

    assert collected_test_ids(root) == {"tests/test_a.py::test_x"}
    assert discover_test_ids(root) == {
        "tests/test_a.py::test_x",
        "tests/test_b.py::test_y",
    }


_HEAD = "| old test id | replacement test id or reason |\n|---|---|\n"


def test_parse_crosswalk_of_an_empty_table_has_no_rows() -> None:
    """Text before the table is ignored; a header and separator alone are valid."""
    assert parse_crosswalk("# Title\n\nSome text.\n\n" + _HEAD) == ([], [])


def test_parse_crosswalk_ignores_an_indented_example_but_reads_indented_rows() -> None:
    """Four leading spaces make a code block; up to three still make a row."""
    example = "    | tests/test_a.py::test_x | covered by tests/test_a.py::test_y |\n"
    row = "   | tests/test_a.py::test_z | API removed by decision D-7 |\n"

    assert parse_crosswalk(example + _HEAD + row) == (
        [Row(4, "tests/test_a.py::test_z", "API removed by decision D-7")],
        [],
    )


def test_parse_crosswalk_returns_rows_with_line_numbers() -> None:
    """Each row keeps its line number, with or without the closing pipe."""
    text = (
        _HEAD + "| tests/test_a.py::test_x | duplicate of tests/test_a.py::test_y |\n"
    )
    text += "| tests/test_a.py::test_z | API removed by decision D-7\n"

    assert parse_crosswalk(text) == (
        [
            Row(3, "tests/test_a.py::test_x", "duplicate of tests/test_a.py::test_y"),
            Row(4, "tests/test_a.py::test_z", "API removed by decision D-7"),
        ],
        [],
    )


def test_parse_crosswalk_requires_the_header_row() -> None:
    """A table with another header is rejected."""
    rows, problems = parse_crosswalk("| old | new |\n|---|---|\n| a | b |\n")

    assert rows == []
    assert len(problems) == 1
    assert problems[0].startswith("the table must start with the header row")


def test_parse_crosswalk_requires_the_separator_row() -> None:
    """The second table line must be the separator."""
    text = "| old test id | replacement test id or reason |\n| a | b |\n"

    assert parse_crosswalk(text)[1] == ["line 2: expected the separator row |---|---|"]


def test_parse_crosswalk_requires_the_separator_right_after_the_header() -> None:
    """A separator after a blank line does not continue the table."""
    text = "| old test id | replacement test id or reason |\n\n|---|---|\n"

    assert parse_crosswalk(text) == (
        [],
        [
            "line 2: expected the separator row |---|---|",
            "line 3: a table line outside the crosswalk table",
        ],
    )


_ROW = "| tests/test_a.py::test_x | API removed by decision D-7 |\n"


@pytest.mark.parametrize(
    ("after", "lines"),
    [
        pytest.param("\nSome text.\n\n```\n" + _ROW + "```\n", [7], id="code-fence"),
        pytest.param("\n<!--\n" + _ROW + "-->\n", [5], id="html-comment"),
        pytest.param("\n" + _ROW, [4], id="after-a-blank-line"),
        pytest.param("\n| x | y |\n|---|---|\n", [4, 5], id="second-table"),
    ],
)
def test_parse_crosswalk_rejects_table_lines_after_the_table(
    after: str, lines: list[int]
) -> None:
    """A ``|`` line after the table ends is reported, not read as a row."""
    assert parse_crosswalk(_HEAD + after) == (
        [],
        [f"line {n}: a table line outside the crosswalk table" for n in lines],
    )


def test_parse_crosswalk_requires_a_blank_line_after_the_table() -> None:
    """Markdown would render a line right after the table as one more row."""
    rows, problems = parse_crosswalk(_HEAD + _ROW + "A note.\n")

    assert rows == [Row(3, "tests/test_a.py::test_x", "API removed by decision D-7")]
    assert problems == ["line 4: a blank line must end the table"]


@pytest.mark.parametrize(
    "row",
    ["| tests/test_a.py::test_x |", "| a | b | c |", "| tests/test_a.py::test_x | |"],
)
def test_parse_crosswalk_rejects_rows_without_two_cells(row: str) -> None:
    """A row with one, three, or an empty cell is rejected."""
    assert parse_crosswalk(_HEAD + row + "\n") == (
        [],
        ["line 3: a row needs two non-empty cells"],
    )


_OLD = "tests/test_old.py::test_gone"
_NEW = "tests/test_new.py::test_here"


@pytest.mark.parametrize(
    "target",
    [
        _NEW,
        f"covered by {_NEW}",
        f"duplicate of {_NEW}",
        f"mocked private internals; behavior covered by {_NEW}",
        "API removed by decision D-7",
        "API removed by decision A-12",
        "API removed by decision U-4",
    ],
)
def test_check_accepts_each_fixed_reason_form(target: str) -> None:
    """A deleted test with a replacement id or any fixed reason passes."""
    assert check_crosswalk([_OLD], [Row(3, _OLD, target)], {_NEW}) == []


def test_check_passes_when_every_inventory_test_still_exists() -> None:
    """No rows are needed while nothing was deleted."""
    assert check_crosswalk([_OLD, _NEW], [], {_OLD, _NEW}) == []


def test_check_flags_a_deleted_test_without_a_row() -> None:
    """An inventory id that is gone and has no row fails the rule."""
    assert check_crosswalk([_OLD], [], set()) == [
        f"{_OLD} no longer exists and has no row in tests/CROSSWALK.md"
    ]


def test_check_flags_a_stale_row_for_a_test_that_still_exists() -> None:
    """A row for a test that still exists is stale."""
    problems = check_crosswalk([_OLD], [Row(3, _OLD, _NEW)], {_OLD, _NEW})

    assert problems == [f"line 3: stale row, {_OLD} still exists"]


def test_check_flags_a_second_row_for_the_same_test() -> None:
    """Two rows for one old id are rejected, naming the first."""
    rows = [Row(3, _OLD, _NEW), Row(4, _OLD, f"covered by {_NEW}")]

    assert check_crosswalk([_OLD], rows, {_NEW}) == [
        f"line 4: second row for {_OLD} (first on line 3)"
    ]


def test_check_flags_a_row_for_an_id_outside_the_inventory() -> None:
    """A row whose old id is not in the inventory (a typo) is rejected."""
    problems = check_crosswalk([], [Row(3, _OLD, _NEW)], {_NEW})

    assert problems == [f"line 3: {_OLD} is not in the inventory"]


@pytest.mark.parametrize(
    "target",
    [
        _NEW,
        f"covered by {_NEW}",
        f"duplicate of {_NEW}",
        f"mocked private internals; behavior covered by {_NEW}",
    ],
)
def test_check_flags_a_named_test_that_does_not_exist(target: str) -> None:
    """Every test id a row names must be a test function now."""
    problems = check_crosswalk([_OLD], [Row(3, _OLD, target)], set())

    assert problems == [f"line 3: {_NEW} is not a test function"]


@pytest.mark.parametrize(
    "target",
    [
        "removed",
        "API removed by decision D7",
        "API removed by decision F1-1",
        "API removed by decision X-1",
        "covered by the new suite",
        f"Covered by {_NEW}",
        f"covered by {_NEW} and more",
    ],
)
def test_check_rejects_targets_outside_the_fixed_forms(target: str) -> None:
    """Free text, another wording, or a malformed or defect id is rejected."""
    problems = check_crosswalk([_OLD], [Row(3, _OLD, target)], {_NEW})

    assert len(problems) == 1
    assert problems[0].startswith(f"line 3: {target!r} is neither a test id nor")


def test_check_rejects_a_reason_naming_a_parametrized_id() -> None:
    """A reason must name a function-level id, without a parametrize suffix."""
    target = f"covered by {_NEW}[1]"

    assert check_crosswalk([_OLD], [Row(3, _OLD, target)], {_NEW}) == [
        f"line 3: '{_NEW}[1]' in {target!r} is not a function-level test id"
    ]


# -- entry point: the generator the inventory was written with once ------------


def main(argv: Sequence[str] | None = None) -> int:
    """Write the inventory of the current tree; see the module docstring."""
    parser = argparse.ArgumentParser(
        prog="python -m tests.test_crosswalk",
        description=(
            "Write the legacy test inventory of the current tree. The inventory"
            " in tests/legacy_inventory.txt was written once, at bc26785, and is"
            " frozen: a deleted or renamed test gets a row in tests/CROSSWALK.md."
        ),
    )
    parser.add_argument(
        "--write-inventory",
        type=Path,
        required=True,
        metavar="PATH",
        help="file to write; it must not exist yet unless --force is given",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite PATH if it exists",
    )
    args = parser.parse_args(argv)
    if args.write_inventory.exists() and not args.force:
        parser.error(
            f"{args.write_inventory} exists. tests/legacy_inventory.txt is frozen;"
            " a deleted or renamed test gets a row in tests/CROSSWALK.md instead."
            " Pass --force to overwrite the file anyway."
        )
    text = inventory_text(discover_test_ids(ROOT))
    args.write_inventory.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
