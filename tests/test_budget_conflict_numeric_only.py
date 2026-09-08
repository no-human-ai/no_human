"""Predicate/repair-only unit tests for `hunks_numeric_only` / `resolve_hunks`
(`src/no_human/vcs/budget_conflict.py`) -- the conflict-SHAPE test and repair
that decide whether a real `tests/test_structural_budget.py` merge conflict
is "both sides honestly re-measured the same frozen `FROZEN_*` entry"
(arithmetic-free re-anchoring to the merged tree's OWN measurement, never
either side's declared number) versus any other edit (a hand decision, must
still open a coder round).

These tests call the parser/predicate/repair directly against hand-built
merged text -- no worktree, no scanner, no git -- so each shape is isolated
from the end-to-end mechanical-resolution tests in
`test_orchestrator_pr_conflict.py`.
"""
from __future__ import annotations

from pathlib import Path

from no_human.vcs.budget_conflict import (
    hunks_numeric_only,
    load_scanner,
    measure,
    parse_conflict_hunks,
    resolve_hunks,
)

_KEY = "core/orchestrator.py:Orchestrator._run_attempt"


def _hunk(ours_lines: str, theirs_lines: str, *, dict_name: str = "FROZEN_FUNCTION_LINES") -> str:
    return (
        f"{dict_name} = {{\n"
        "<<<<<<< HEAD\n"
        f"{ours_lines}"
        "=======\n"
        f"{theirs_lines}"
        ">>>>>>> branch\n"
        "}\n"
    )


def test_the_value_taken_is_the_measured_one_not_either_side():
    merged = _hunk(
        f'    "{_KEY}": 2167,\n',
        f'    "{_KEY}": 2170,\n',
    )
    assert hunks_numeric_only(merged) is True
    measured = {"FROZEN_FUNCTION_LINES": {_KEY: 2181}}
    resolved = resolve_hunks(merged, measured)
    assert resolved is not None
    resolved_text, notes = resolved
    assert f'"{_KEY}": 2181,' in resolved_text
    assert "2167" not in resolved_text
    assert "2170" not in resolved_text
    assert "<<<<<<<" not in resolved_text
    assert notes == [f"FROZEN_FUNCTION_LINES:{_KEY} -> 2181"]


def test_both_sides_provenance_comments_survive():
    merged = _hunk(
        f'    # ours comment: grew via branch A\n    "{_KEY}": 2170,\n',
        f'    # theirs comment: grew via branch B\n    "{_KEY}": 2175,\n',
    )
    assert hunks_numeric_only(merged) is True
    measured = {"FROZEN_FUNCTION_LINES": {_KEY: 2181}}
    resolved = resolve_hunks(merged, measured)
    assert resolved is not None
    resolved_text, _notes = resolved
    assert "# ours comment: grew via branch A" in resolved_text
    assert "# theirs comment: grew via branch B" in resolved_text
    assert f'"{_KEY}": 2181,' in resolved_text


def test_an_added_frozen_entry_is_not_numeric_only():
    other_key = "a.py:g"
    merged = _hunk(
        f'    "{_KEY}": 310,\n',
        f'    "{_KEY}": 310,\n    "{other_key}": 320,\n',
    )
    assert hunks_numeric_only(merged) is False
    assert resolve_hunks(merged, {"FROZEN_FUNCTION_LINES": {_KEY: 310, other_key: 320}}) is None


def test_a_key_rename_is_not_numeric_only():
    merged = _hunk(
        '    "a.py:f_old": 310,\n',
        '    "a.py:f_new": 310,\n',
    )
    assert hunks_numeric_only(merged) is False
    assert resolve_hunks(merged, {"FROZEN_FUNCTION_LINES": {"a.py:f_old": 310, "a.py:f_new": 310}}) is None


def test_a_non_entry_code_line_in_a_hunk_is_not_numeric_only():
    merged = _hunk(
        f'    "{_KEY}": 310,\n',
        "    some_other_code = 1\n",
    )
    assert hunks_numeric_only(merged) is False
    assert resolve_hunks(merged, {"FROZEN_FUNCTION_LINES": {_KEY: 310}}) is None


def test_a_hunk_outside_a_frozen_dict_is_refused():
    merged = (
        "some_var = 1\n"
        "<<<<<<< HEAD\n"
        "value = 1\n"
        "=======\n"
        "value = 2\n"
        ">>>>>>> branch\n"
    )
    # Well-formed markers (parse_conflict_hunks succeeds) but no enclosing
    # FROZEN_* dict -- `_walk_hunks` refuses, distinct from an unparseable
    # marker shape (covered by `test_unparseable_markers_refuse` below).
    assert parse_conflict_hunks(merged) is not None
    assert hunks_numeric_only(merged) is False
    assert resolve_hunks(merged, {}) is None


def test_an_unmeasured_key_refuses():
    merged = _hunk(
        f'    "{_KEY}": 310,\n',
        f'    "{_KEY}": 320,\n',
    )
    assert hunks_numeric_only(merged) is True
    # The scanner's own measurement doesn't know this key (renamed/deleted on
    # the merged tree, or the dict bucket is missing entirely) -- never
    # guess, refuse.
    assert resolve_hunks(merged, {"FROZEN_FUNCTION_LINES": {}}) is None
    assert resolve_hunks(merged, {}) is None


def test_unparseable_markers_refuse():
    unterminated = (
        "FROZEN_FUNCTION_LINES = {\n"
        "<<<<<<< HEAD\n"
        f'    "{_KEY}": 310,\n'
        "=======\n"
        f'    "{_KEY}": 320,\n'
        "}\n"
    )
    assert parse_conflict_hunks(unterminated) is None
    assert hunks_numeric_only(unterminated) is False
    assert resolve_hunks(unterminated, {"FROZEN_FUNCTION_LINES": {_KEY: 330}}) is None

    marker_outside_hunk = "=======\nrogue marker\n"
    assert parse_conflict_hunks(marker_outside_hunk) is None
    assert hunks_numeric_only(marker_outside_hunk) is False
    assert resolve_hunks(marker_outside_hunk, {}) is None


# --------------------------------------------------------------------------- #
# `load_scanner`/`measure` -- the loader that decides between the real
# `src/no_human/testing/structural_budget.py` module (PR #1035's planned
# extraction) and "ours"'s self-contained `tests/test_structural_budget.py`
# copy. Bugfix: the real module has existed since 31a03c9f (2026-09-04) as a
# PREFLIGHT helper (frozen_paths/touched_frozen/...) with no `scan_tree` --
# preferring it unconditionally (as `load_scanner` used to) made `measure()`
# fail closed on every worktree that has it, and the swallowed `AttributeError`
# reported nothing but "could not run the scanner" (tasks d256ae60/e9e90630,
# 2026-09-08). These tests drive `load_scanner`/`measure` directly against a
# hand-built fake worktree -- no git, no real scanner.
# --------------------------------------------------------------------------- #

_PREFLIGHT_BODY = '''\
"""Preflight helper stub -- no scan_tree, mirroring the real
src/no_human/testing/structural_budget.py shape as of 31a03c9f."""
from __future__ import annotations


def frozen_paths(root):
    return []
'''

_REAL_WITH_SCAN_TREE = '''\
SENTINEL = "real-module"


def scan_tree(root):
    return {}, {}, {}, 0, 0
'''

_MINIMAL_SCANNER = '''\
def scan_tree(root):
    return {}, {}, {}, 0, 0
'''

_RAISING_SCANNER = '''\
def scan_tree(root):
    raise RuntimeError("boom-xyz")
'''

_BROKEN_SCANNER = "def scan_tree(:\n    pass\n"  # syntax error -- fails to load


def _fake_worktree(tmp_path, real_body: str | None = None) -> Path:
    """A minimal fake worktree: `<wt>/src/no_human/` with one trivial module
    (so `Path(root) / "src" / "no_human"` exists), and -- when *real_body* is
    given -- a `src/no_human/testing/structural_budget.py` carrying it, the
    same path `load_scanner` prefers on a real worktree."""
    wt = tmp_path / "wt"
    no_human = wt / "src" / "no_human"
    no_human.mkdir(parents=True)
    (no_human / "trivial.py").write_text("x = 1\n", encoding="utf-8")
    if real_body is not None:
        testing_dir = no_human / "testing"
        testing_dir.mkdir()
        (testing_dir / "structural_budget.py").write_text(real_body, encoding="utf-8")
    return wt


def test_a_real_module_without_scan_tree_falls_back_to_the_ours_test_file(tmp_path):
    wt = _fake_worktree(tmp_path, real_body=_PREFLIGHT_BODY)
    measured, reason = measure(str(wt), _MINIMAL_SCANNER)
    assert reason == ""
    assert measured is not None
    assert set(measured) == {
        "FROZEN_FUNCTION_LINES", "FROZEN_FUNCTION_CC", "FROZEN_FILE_LINES",
    }


def test_load_scanner_prefers_the_real_module_when_it_has_scan_tree(tmp_path):
    wt = _fake_worktree(tmp_path, real_body=_REAL_WITH_SCAN_TREE)
    mod, reason = load_scanner(str(wt), _MINIMAL_SCANNER)
    assert reason == ""
    # a sentinel only the REAL module defines -- proves "ours" was not
    # silently loaded instead.
    assert getattr(mod, "SENTINEL", None) == "real-module"


def test_a_raising_scan_tree_reports_the_exception_text(tmp_path):
    wt = _fake_worktree(tmp_path)
    measured, reason = measure(str(wt), _RAISING_SCANNER)
    assert measured is None
    assert "boom-xyz" in reason


def test_no_scanner_at_all_names_both_attempts(tmp_path):
    wt = _fake_worktree(tmp_path, real_body=_PREFLIGHT_BODY)
    real_path = wt / "src" / "no_human" / "testing" / "structural_budget.py"
    mod, reason = load_scanner(str(wt), _BROKEN_SCANNER)
    assert mod is None
    assert str(real_path) in reason
    assert "scan_tree" in reason
