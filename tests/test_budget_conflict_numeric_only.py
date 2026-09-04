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

from no_human.vcs.budget_conflict import (
    hunks_numeric_only,
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
