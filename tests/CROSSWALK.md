# Test crosswalk

`tests/legacy_inventory.txt` is the frozen list of the logpyt 2.x tests: the 13
test modules of 2.0.0 plus `tests/behavior/` and `tests/test_fake_adb.py`. A PR
that deletes or renames one of them adds a row to the table below in the same
PR, so no 2.x test disappears without a trace (`.docs/design-3.0.0.md`, section
8.3). `tests/test_crosswalk.py` fails when the table and the tree disagree.

The left cell is the old test id, exactly as the inventory lists it. The right
cell is the id of the test that replaces it, or one of these reasons, worded
exactly like this:

    API removed by decision <decision id>
    covered by <test id>
    duplicate of <test id>
    mocked private internals; behavior covered by <test id>

For example:

    | tests/test_stream.py::test_old | tests/conformance/test_x.py::test_new |
    | tests/test_stream.py::test_pause | API removed by decision D-7 |

Rules:

- A test id is a function-level pytest node id with `/` separators and no
  parametrize suffix: `tests/test_pid_monitor.py::TestPidMonitor::test_stop`.
  Every test id in the right cell must exist; a PR that deletes one of them
  updates the rows that name it.
- A decision id is a decision of the design: `D-n`, `A-n` or `U-n` (`D-7`,
  `A-12`, `U-4`). A defect id such as `X-1` or `F1-1` is not a decision.
- One row per old test. A row for a test that still exists is stale and fails.
- Ids are plain text, without backticks. Only the table below may contain lines
  that start with `|`; the examples above are indented code. Add rows directly
  below the last one: a blank line or the end of the file ends the table, and
  no `|` line may follow it.
- `tests/legacy_inventory.txt` is never regenerated or edited;
  `tests/test_crosswalk.py` pins its ids.

| old test id | replacement test id or reason |
|---|---|
