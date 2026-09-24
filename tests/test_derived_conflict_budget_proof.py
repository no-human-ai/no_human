"""`resolve_derived_conflict`'s step 6.5 (structural-budget re-anchor proof,
`src/no_human/vcs/derived_conflict.py`) must distinguish "the proof could not
run" from "the proof ran and failed" in the returned `DerivedResolution.detail`.

Bugfix context: `run_budget_test` used to shell out via `sys.executable`,
which in a PyInstaller-frozen desktop build IS the frozen `nh` binary --
`[nh, "-m", "pytest", ...]` re-enters the click CLI instead of running
anything. `run_budget_test` itself (see `test_budget_conflict_numeric_only.py`)
now resolves a real interpreter through `proc.real_python` and fails closed
(returns `(False, NO_INTERPRETER_DETAIL)` without shelling out at all) when
none is available. This module covers the OTHER half: the caller must not
report a "no interpreter" result as "the proof did not pass its own test" --
that would be a false claim, since the test never ran.

Reuses the real end-to-end budget-conflict fixture from
`test_orchestrator_pr_conflict.py` (`_repo_with_budget_stub` plus a feature
and a main branch each re-anchoring the same FROZEN_FUNCTION_LINES entry to
their own -- wrong -- number) so `budget_notes` is genuinely populated and
step 6.5 actually runs, then monkeypatches `derived_conflict.run_budget_test`
to control what step 6.5 sees, rather than depending on subprocess/venv
plumbing this module does not own.
"""
from __future__ import annotations

from pathlib import Path

from no_human.vcs import derived_conflict as dc
from tests.test_orchestrator_pr_conflict import (
    _approve,
    _git,
    _push_branch,
    _repo_with_budget_stub,
    _use_stub_export_guard,
    _worktree,
)


def _budget_conflict_fixture(tmp_path: Path) -> tuple[Path, str]:
    """feature and main each grow `grow()` and re-anchor the same frozen
    entry to their own (differing, both wrong) number -- a genuine merge
    conflict confined to the manifest pin plus the frozen entry line, so
    `budget_notes` is non-empty and step 6.5 actually runs."""
    work = _repo_with_budget_stub(tmp_path)

    wt_f = tmp_path / "wt_feature"
    _worktree(work, wt_f, "feature")
    (wt_f / "src" / "no_human" / "growing.py").write_text(
        "def grow():\n"
        "    b = 0\n"
        "    a = 1\n"
        "    return a\n",
        encoding="utf-8",
    )
    budget_path_f = wt_f / "tests" / "test_structural_budget.py"
    budget_path_f.write_text(
        budget_path_f.read_text(encoding="utf-8").replace(
            '"growing.py:grow": 3,', '"growing.py:grow": 4,'
        ),
        encoding="utf-8",
    )
    _git(wt_f, "add", "-A")
    _git(wt_f, "commit", "-qm", "feature grows grow() at the top")
    _approve(wt_f, ["src/no_human/growing.py"])
    _git(wt_f, "add", "RELEASE_MANIFEST.txt")
    _git(wt_f, "commit", "-qm", "pin growing.py (feature)")
    _push_branch(work, wt_f, "feature")

    (work / "src" / "no_human" / "growing.py").write_text(
        "def grow():\n"
        "    a = 1\n"
        "    c = 2\n"
        "    d = 3\n"
        "    return a\n",
        encoding="utf-8",
    )
    budget_path_m = work / "tests" / "test_structural_budget.py"
    budget_path_m.write_text(
        budget_path_m.read_text(encoding="utf-8").replace(
            '"growing.py:grow": 3,', '"growing.py:grow": 5,'
        ),
        encoding="utf-8",
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "main grows grow() at the bottom")
    _approve(work, ["src/no_human/growing.py"])
    _git(work, "add", "RELEASE_MANIFEST.txt")
    _git(work, "commit", "-qm", "pin growing.py (main)")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")

    base_tip = _git(work, "rev-parse", "origin/main").stdout.strip()
    return work, base_tip


def _resolve(work: Path, base_tip: str) -> dc.DerivedResolution:
    return dc.resolve_derived_conflict(
        str(work), "feature", base_tip, remote="origin",
        eligible=dc.DERIVED_ARTEFACTS | {dc.BUDGET_TEST_PATH})


def test_no_interpreter_reports_the_proof_could_not_run_not_that_it_failed(
    monkeypatch, tmp_path,
):
    _use_stub_export_guard(monkeypatch)
    work, base_tip = _budget_conflict_fixture(tmp_path)

    monkeypatch.setattr(
        dc, "run_budget_test", lambda *a, **kw: (False, dc.NO_INTERPRETER_DETAIL))

    res = _resolve(work, base_tip)

    assert res.ok is False
    assert res.step == "budget"
    assert "did not pass its own test" not in res.detail
    assert "could not" in res.detail.lower()


def test_a_real_proof_failure_still_says_the_test_did_not_pass(monkeypatch, tmp_path):
    _use_stub_export_guard(monkeypatch)
    work, base_tip = _budget_conflict_fixture(tmp_path)

    monkeypatch.setattr(
        dc, "run_budget_test", lambda *a, **kw: (False, "E assert 120 <= 100"))

    res = _resolve(work, base_tip)

    assert res.ok is False
    assert res.step == "budget"
    assert "did not pass its own test" in res.detail
