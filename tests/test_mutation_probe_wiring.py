"""Wiring: the review gate actually runs the mutation probe (see
`testing/mutation_probe.py` for the probe itself, which is unit-tested in
`tests/test_mutation_probe.py`). These tests pin the seam between the two:

* AC1 — a `"killed"` probe is recorded in the review record as evidence a
  test pins its target.
* AC2 — a `"survived"` probe is a blocking finding that flips a passing
  review to failing.
* AC3 — the probe's own tree-integrity proof is exercised end-to-end here by
  running the REAL probe against a real tmp repo and asserting the reviewed
  working tree (`repo_path`) is untouched afterward — never a `git checkout`/
  `reset` assertion, a content check.
* AC5 — a probe that could not run is recorded either way, and blocks the
  gate only in `mode="required"`; `mode="advisory"` records it without
  blocking.
* The probe is OFF by default (every existing direct `AdversarialReviewer()`
  construction must be unaffected) and turned on only by
  `AdversarialReviewer.from_config`.
* A crashing probe must never fail the gate by itself — it becomes a visible
  "could not run" finding instead of an exception escaping `review()`.
* Mutation findings can only make a decision stricter: `passed` can go
  True -> False, never False -> True.
"""

from __future__ import annotations

import subprocess

import pytest

from no_human.review.reviewer import (
    AdversarialReviewer,
    ChecklistItem,
    ReviewDecision,
    merge_mutation_findings,
)
from no_human.testing import mutation_probe


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit(repo, msg="change"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "u@e.com")
    _git(tmp_path, "config", "user.name", "u")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    return tmp_path


def _killed_probe(node_id="tests/test_calc.py::test_double"):
    return mutation_probe.TestProbe(
        node_id=node_id, verdict="killed", target="pkg/calc.py:double",
        mutation="x * 2 -> x - 2", reason="AssertionError: 2 != 4",
    )


def _survived_probe(node_id="tests/test_calc.py::test_double"):
    return mutation_probe.TestProbe(
        node_id=node_id, verdict="survived", target="pkg/calc.py:double",
        mutation="x * 2 -> x - 2",
        reason="the test stayed green under every generated mutation tried",
    )


def _undetermined_probe(node_id="tests/test_calc.py::test_double"):
    return mutation_probe.TestProbe(
        node_id=node_id, verdict="undetermined", target="pkg/calc.py:double",
        reason="the test could not be run in the probe copy: no interpreter",
    )


# --------------------------------------------------------------------------
# AC1 / AC2 / AC5 — merge_mutation_findings
# --------------------------------------------------------------------------


def test_a_killed_probe_is_recorded_in_the_review_record():
    decision = ReviewDecision(passed=True, checklist=[
        ChecklistItem("acceptance criteria met", True)])
    result = mutation_probe.MutationProbeResult(
        verdict="pass", probes=[_killed_probe()], reasons=[], tree_intact=True)

    merged = merge_mutation_findings(decision, result, mode="advisory")

    assert merged.passed is True
    new_items = [i for i in merged.checklist if i.label != "acceptance criteria met"]
    assert len(new_items) == 1
    item = new_items[0]
    assert item.passed is True
    assert "double" in item.evidence or "double" in item.label
    assert "x * 2 -> x - 2" in item.evidence
    assert "AssertionError" in item.evidence  # names the failure, not just the mutation
    # round-trips through the persisted checklist shape
    d = merged.as_dict()
    assert any("mutation probe" in i["label"] for i in d["items"])


def test_a_survived_probe_blocks_a_passing_review():
    decision = ReviewDecision(passed=True, checklist=[
        ChecklistItem("acceptance criteria met", True)])
    result = mutation_probe.MutationProbeResult(
        verdict="fail", probes=[_survived_probe()], reasons=[], tree_intact=True)

    merged = merge_mutation_findings(decision, result, mode="advisory")

    assert merged.passed is False
    assert any(not i.passed for i in merged.blocking_items)
    survived_items = [i for i in merged.blocking_items if "survives" in i.label]
    assert len(survived_items) == 1


def test_could_not_run_is_recorded_in_advisory_and_blocks_in_required():
    decision_advisory = ReviewDecision(passed=True, checklist=[
        ChecklistItem("acceptance criteria met", True)])
    result = mutation_probe.MutationProbeResult(
        verdict="fail", probes=[_undetermined_probe()], reasons=[], tree_intact=True)

    merged_advisory = merge_mutation_findings(decision_advisory, result, mode="advisory")
    assert merged_advisory.passed is True
    could_not_run = [i for i in merged_advisory.checklist if "could not run" in i.label]
    assert len(could_not_run) == 1
    assert could_not_run[0].passed is True  # present, not blocking
    assert could_not_run[0] not in merged_advisory.blocking_items

    decision_required = ReviewDecision(passed=True, checklist=[
        ChecklistItem("acceptance criteria met", True)])
    merged_required = merge_mutation_findings(decision_required, result, mode="required")
    assert merged_required.passed is False
    could_not_run_req = [i for i in merged_required.checklist if "could not run" in i.label]
    assert could_not_run_req[0] in merged_required.blocking_items


def test_mutation_findings_never_flip_a_failing_review_to_passing():
    decision = ReviewDecision(passed=False, checklist=[
        ChecklistItem("something else is wrong", False, severity="high")])
    result = mutation_probe.MutationProbeResult(
        verdict="pass", probes=[_killed_probe(), _killed_probe("tests/test_calc.py::test_other")],
        reasons=[], tree_intact=True,
    )

    merged = merge_mutation_findings(decision, result, mode="advisory")

    assert merged.passed is False  # an all-killed probe result must not undo the pre-existing fail


# --------------------------------------------------------------------------
# Default-off / from_config wiring
# --------------------------------------------------------------------------


async def test_a_directly_constructed_reviewer_runs_no_probe(monkeypatch, tmp_path):
    """Every existing direct `AdversarialReviewer()` construction (tests,
    ad-hoc callers) must be unaffected: `mutation_probe=None` is the default,
    and `None` must mean the probe is never even imported/called."""

    def boom(*a, **k):
        raise AssertionError("run_mutation_probe must never be called when the probe is off")
    monkeypatch.setattr(mutation_probe, "run_mutation_probe", boom)

    r = AdversarialReviewer(model="claude-opus-5")
    assert r._mutation_probe is None

    decision = ReviewDecision(passed=True, checklist=[ChecklistItem("ok", True)])
    result = await r._apply_mutation_probe(
        decision, tmp_path, "HEAD~1", "HEAD", None)

    assert result is decision
    assert result.passed is True
    assert [i.label for i in result.checklist] == ["ok"]


def test_from_config_turns_the_probe_on():
    """`from_config` is the ONE place production wires the probe on, via
    `config.mutation_probe_config` — not a hardcoded dict duplicated here."""
    from no_human.config import mutation_probe_config

    class _FakeBackend:
        model = "fake"

    r = AdversarialReviewer.from_config({}, backend=_FakeBackend())

    assert r._mutation_probe == mutation_probe_config({})
    assert r._mutation_probe["mode"] == "advisory"  # DEFAULT_CONFIG's safe-by-default mode


# --------------------------------------------------------------------------
# Crash containment
# --------------------------------------------------------------------------


async def test_a_crashing_probe_never_fails_the_gate_by_itself(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(mutation_probe, "run_mutation_probe", boom)

    r_advisory = AdversarialReviewer(
        model="claude-opus-5", mutation_probe={"mode": "advisory"})
    decision = ReviewDecision(passed=True, checklist=[ChecklistItem("ok", True)])
    result = await r_advisory._apply_mutation_probe(
        decision, tmp_path, "HEAD~1", "HEAD", None)
    assert result.passed is True  # the crash itself never fails an advisory gate
    crashed_items = [i for i in result.checklist if "crashed" in i.label]
    assert len(crashed_items) == 1
    assert crashed_items[0].passed is True

    r_required = AdversarialReviewer(
        model="claude-opus-5", mutation_probe={"mode": "required"})
    decision2 = ReviewDecision(passed=True, checklist=[ChecklistItem("ok", True)])
    result2 = await r_required._apply_mutation_probe(
        decision2, tmp_path, "HEAD~1", "HEAD", None)
    assert result2.passed is False  # "required" means unrunnable blocks


# --------------------------------------------------------------------------
# AC3 — the reviewed working tree is never the one that gets mutated
# --------------------------------------------------------------------------


async def test_the_probe_never_touches_the_reviewed_worktree(repo, monkeypatch):
    """Runs the REAL probe (not mocked) end-to-end through the reviewer
    wiring against a real tmp git repo, then proves two things: (1) the
    disposable copy — not `repo_path` — is what `git worktree add` targets,
    and (2) `repo_path`'s tracked content is byte-identical afterward, via
    `git status --porcelain` staying empty across the call. A `git
    checkout`/`reset`/`stash` assertion would prove nothing was reverted;
    proving nothing was ever changed is the actual guarantee this module
    promises."""
    (repo / "pkg" / "calc.py").write_text("def double(x):\n    return x * 2\n")
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import double\n\ndef test_double():\n"
        "    assert double(2) == 4\n"
    )
    _commit(repo)
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import double\n\ndef test_double():\n"
        "    assert double(2) == 4\n    assert double(3) == 6\n"
    )
    _commit(repo, "change the test")

    real_run = subprocess.run
    worktree_add_calls = []

    def spy_run(cmd, *a, **k):
        if isinstance(cmd, list) and cmd[:3] == ["git", "worktree", "add"]:
            worktree_add_calls.append(cmd)
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(mutation_probe, "subprocess", subprocess)
    monkeypatch.setattr(subprocess, "run", spy_run)

    status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        capture_output=True, text=True,
    ).stdout

    r = AdversarialReviewer(model="claude-opus-5", mutation_probe={"mode": "advisory"})
    decision = ReviewDecision(passed=True, checklist=[
        ChecklistItem("acceptance criteria met", True)])
    result = await r._apply_mutation_probe(decision, repo, "HEAD~1", "HEAD", None)

    status_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        capture_output=True, text=True,
    ).stdout

    assert status_before == "" == status_after, (
        "the reviewed working tree must stay byte-identical across the probe")
    assert worktree_add_calls, "the probe must build its disposable copy via git worktree add"
    for call in worktree_add_calls:
        target = call[4]  # ["git", "worktree", "add", "--detach", <target>, after_ref]
        assert str(repo) != target and not target.startswith(str(repo) + "/"), (
            f"git worktree add targeted the reviewed tree itself: {call}")

    # the real probe actually ran and produced a finding — not a silent no-op
    assert any("mutation probe" in i.label for i in result.checklist)
