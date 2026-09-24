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

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from no_human.vcs import budget_conflict
from no_human.vcs.budget_conflict import (
    BUDGET_TEST_PATH,
    NO_INTERPRETER_DETAIL,
    hunks_numeric_only,
    load_scanner,
    measure,
    parse_conflict_hunks,
    resolve_hunks,
    run_budget_test,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

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


# --------------------------------------------------------------------------- #
# Bugfix regression: `scan_source`/`scan_tree` moved from
# `tests/test_structural_budget.py` into `src/no_human/testing/structural_budget.py`
# so `load_scanner`'s PREFERRED branch (the real module) actually succeeds on
# a normal worktree, instead of always falling through to exec-ing "ours"'s
# copy of the test file -- which does `import pytest` at module level and so
# requires pytest importable in the resolver's OWN process. These tests use
# the real repo files (not the hand-built fakes above) to prove the fix
# against the actual shipped scanner, not a stand-in.
# --------------------------------------------------------------------------- #

_REAL_SCANNER_TEXT = (
    REPO_ROOT / "src" / "no_human" / "testing" / "structural_budget.py"
).read_text(encoding="utf-8")
_REAL_GUARD_TEXT = (REPO_ROOT / "tests" / "test_structural_budget.py").read_text(
    encoding="utf-8"
)


@contextmanager
def _no_pytest():
    """Makes `import pytest` fail with `ModuleNotFoundError` for the
    duration of the `with` block, simulating a resolver process that has no
    pytest installed. Pops `"pytest"` out of `sys.modules` too -- without
    that, the import is already cached and the inserted finder is never
    consulted, so the test would pass vacuously regardless of the fix."""

    class _BlockPytest:
        def find_spec(self, fullname, path, target=None):
            if fullname == "pytest":
                raise ModuleNotFoundError("No module named 'pytest'")
            return None

    saved_pytest = sys.modules.pop("pytest", None)
    sys.meta_path.insert(0, _BlockPytest())
    try:
        yield
    finally:
        sys.meta_path.pop(0)
        if saved_pytest is not None:
            sys.modules["pytest"] = saved_pytest


def test_load_scanner_returns_the_production_module_on_a_main_shaped_worktree(tmp_path):
    wt = _fake_worktree(tmp_path, real_body=_REAL_SCANNER_TEXT)
    mod, reason = load_scanner(str(wt), _REAL_GUARD_TEXT)

    assert reason == ""
    # Identified positively by attributes only the PRODUCTION module carries
    # -- never "no error was raised".
    assert mod.GUARD_RELPATH == "tests/test_structural_budget.py"
    assert hasattr(mod, "frozen_paths")
    # The ours-blob copy of tests/test_structural_budget.py WOULD carry the
    # FROZEN_* ledgers; the production module never does.
    assert not hasattr(mod, "FROZEN_FUNCTION_LINES")
    # __file__ is under the worktree, not a throwaway temp .py.
    real_path = wt / "src" / "no_human" / "testing" / "structural_budget.py"
    assert Path(mod.__file__) == real_path


def test_load_scanner_does_not_need_pytest_in_its_own_process(tmp_path):
    wt_main_shaped = _fake_worktree(tmp_path / "main_shaped", real_body=_REAL_SCANNER_TEXT)
    # No `src/no_human/testing/structural_budget.py` at all -- the pre-fix
    # shape, where `load_scanner` always fell through to exec-ing "ours"'s
    # `import pytest`-laden copy of the test file.
    wt_legacy = _fake_worktree(tmp_path / "legacy")

    with _no_pytest():
        mod, reason = load_scanner(str(wt_main_shaped), _REAL_GUARD_TEXT)
        # POSITIVE CONTROL: this is the line that FAILS if the fix is
        # reverted (i.e. if `scan_tree` still lived only in the test file) --
        # proving the assertions above are not vacuously true. With no
        # production scanner to prefer, `load_scanner` falls back to the
        # ours-blob path, which requires pytest and so fails here.
        legacy_mod, legacy_reason = load_scanner(str(wt_legacy), _REAL_GUARD_TEXT)
    # All assertions run after the context manager has restored
    # sys.meta_path/sys.modules.

    assert reason == ""
    assert hasattr(mod, "scan_tree")
    assert mod.GUARD_RELPATH == "tests/test_structural_budget.py"

    assert legacy_mod is None
    assert "pytest" in legacy_reason


# --- run_budget_test: must resolve its interpreter through `proc.real_python` -
# --- instead of `sys.executable` -- in a PyInstaller-frozen desktop build ------
# --- `sys.executable` IS the frozen `nh` binary, and `[nh, "-m", "pytest", ...]`-
# --- re-enters the click CLI instead of running the proof (issue: fifth site of-
# --- the scar fixed for approve_merge.py / manifest_repair.py / runner.py). ----


def _plant_venv(venv_root: Path) -> Path:
    """A venv root with an interpreter FILE inside it (POSIX shape), mirroring
    `tests/test_proc.py::_plant` -- `real_python` only checks `is_file()`."""
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "python"
    exe.write_text("", encoding="utf-8")
    return exe


def _recording_sh(calls: list):
    def _fake_sh(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")
    return _fake_sh


def test_run_budget_test_does_not_shell_out_to_the_frozen_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    primary = tmp_path / "primary"
    venv_python = _plant_venv(primary / ".venv")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    calls: list = []
    monkeypatch.setattr(budget_conflict, "_sh", _recording_sh(calls))

    ok, _ = run_budget_test(str(worktree), repo_root=str(primary))

    assert ok is True
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == str(venv_python)
    assert argv[0] != sys.executable
    assert argv[1:4] == ["-m", "pytest", BUDGET_TEST_PATH]


def test_run_budget_test_still_uses_sys_executable_when_not_frozen(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    primary = tmp_path / "primary"
    _plant_venv(primary / ".venv")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    calls: list = []
    monkeypatch.setattr(budget_conflict, "_sh", _recording_sh(calls))

    ok, _ = run_budget_test(str(worktree), repo_root=str(primary))

    assert ok is True
    assert len(calls) == 1
    assert calls[0][0] == sys.executable


def test_run_budget_test_fails_closed_without_shelling_out_when_no_interpreter(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(budget_conflict, "real_python", lambda *a: None)

    calls: list = []

    def _forbidden_sh(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("run_budget_test must not shell out with no interpreter")

    monkeypatch.setattr(budget_conflict, "_sh", _forbidden_sh)

    worktree = tmp_path / "wt"
    worktree.mkdir()
    ok, detail = run_budget_test(str(worktree))

    assert ok is False
    assert calls == []
    assert "interpreter" in detail.lower()
    assert detail == NO_INTERPRETER_DETAIL
