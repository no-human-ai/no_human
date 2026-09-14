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

import json
import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.core.task import Task
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


async def test_diff_override_reports_could_not_run_and_blocks_only_in_required(
    monkeypatch, tmp_path,
):
    """`review()`'s gate-mode `diff_override` path (a caller-supplied diff
    string, no `before_ref`/`after_ref` guaranteed to bound it — see
    `tests/test_gate_severity.py`'s direct `review(..., diff_override=...)`
    call) reaches `_apply_mutation_probe` too. Since the probe cannot trust
    `before_ref`/`after_ref` to compute "which tests changed" against a
    diff it was not told matches that range, it must report "could not
    run" — the same AC5 contract as a crash or a missing interpreter —
    rather than silently skip reporting anything, or silently probe
    against an untrustworthy range.

    `calls` is recorded rather than raised: an earlier version of this test
    made the mock *raise* on call, but `_apply_mutation_probe`'s own
    `except Exception` handler (its crash-containment path, tested
    separately below) silently swallows that raise into a checklist item
    that *also* contains the substring "could not run" — so a mutant that
    deletes the `diff_override` short-circuit entirely (falls through and
    calls the real probe path) still produced a matching, non-blocking
    checklist item and this test stayed green. Recording the call and
    asserting on it directly, plus asserting the *exact* diff-override
    label (distinct from the crash path's "crashed: ..." wording), closes
    that gap: verified by hand-mutating `if diff_override:` to
    `if not diff_override:` in `_apply_mutation_probe` and confirming this
    test goes red, then restoring the original source.
    """
    calls = []

    def record_call(*a, **k):
        calls.append((a, k))
        raise AssertionError("should never be reached")

    monkeypatch.setattr(mutation_probe, "run_mutation_probe", record_call)

    expected_label = (
        "mutation probe could not run (no before/after refs "
        "available for a diff-override review)"
    )

    r_advisory = AdversarialReviewer(
        model="claude-opus-5", mutation_probe={"mode": "advisory"})
    decision = ReviewDecision(passed=True, checklist=[ChecklistItem("ok", True)])
    result = await r_advisory._apply_mutation_probe(
        decision, tmp_path, "HEAD~1", "HEAD", "--- a\n+++ b\n")
    assert calls == []  # the probe itself must never be invoked here
    assert result.passed is True  # advisory: could-not-run never blocks
    could_not_run = [i for i in result.checklist if i.label == expected_label]
    assert len(could_not_run) == 1
    assert could_not_run[0].passed is True
    assert could_not_run[0] not in result.blocking_items

    r_required = AdversarialReviewer(
        model="claude-opus-5", mutation_probe={"mode": "required"})
    decision2 = ReviewDecision(passed=True, checklist=[ChecklistItem("ok", True)])
    result2 = await r_required._apply_mutation_probe(
        decision2, tmp_path, "HEAD~1", "HEAD", "--- a\n+++ b\n")
    assert calls == []  # still never invoked, in required mode either
    assert result2.passed is False  # required: could-not-run blocks
    could_not_run_req = [i for i in result2.checklist if i.label == expected_label]
    assert len(could_not_run_req) == 1
    assert could_not_run_req[0] in result2.blocking_items


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

    # the real probe actually ran and correctly classified this test: it
    # asserts `double(2) == 4` AND `double(3) == 6` against unmodified code,
    # so a real mutation of `double`'s single `return` statement must kill
    # it. A vague "some checklist item mentions mutation probe" substring
    # match would also pass for a silent "could not run" no-op — asserting
    # the SPECIFIC killed ("pins") outcome, and that nothing says it
    # couldn't run, is what actually proves the probe exercised anything.
    mutation_items = [i for i in result.checklist if i.label.startswith("mutation probe")]
    assert mutation_items, "the probe must record at least one finding"
    assert any("pins" in i.label for i in mutation_items), (
        f"expected a killed ('pins') finding, got: {[i.label for i in mutation_items]}")
    assert not any("could not run" in i.label for i in mutation_items)


# --------------------------------------------------------------------------
# B3 — the gate-mode `review()` pipeline actually wires the probe in, not
# merely `_apply_mutation_probe`/`merge_mutation_findings` called directly
# (which is all every test above this one does). This is the exact seam a
# prior human review found completely untested: deleting the
# `return await self._apply_mutation_probe(...)` tail call inside
# `AdversarialReviewer.review()` would not have failed any test in this
# file before this one was added.
# --------------------------------------------------------------------------


def _fake_review_block(passed: bool, items: list[dict]) -> str:
    data = {"passed": passed, "items": items}
    return f"REVIEW_JSON_START\n{json.dumps(data)}\nREVIEW_JSON_END\n"


class _FakeReviewBackend:
    """Returns a scripted `final_text` without touching the LLM — same
    shape as `tests/test_reviewer.py`'s `FakeBackend`, redefined locally so
    this wiring test does not depend on that file's internals."""

    def __init__(self, final_text: str):
        self._final_text = final_text

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                   on_event=None, supervisor_hook=None):
        return AgentResult(
            final_text=self._final_text,
            num_turns=1, is_error=False,
            tokens_used=10, session_id="fake", stop_reason="end_turn",
        )


async def test_review_gate_mode_actually_runs_the_mutation_probe(repo):
    """Calls the FULL `AdversarialReviewer.review()` (gate mode, the
    default) against a real git repo with a genuinely killable test — not
    `_apply_mutation_probe`/`merge_mutation_findings` directly. If the tail
    call inside `review()` that dispatches to `_apply_mutation_probe` were
    deleted, this is the one test in this file that would actually go red."""
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

    output = _fake_review_block(True, [
        {"label": "double(x) implemented", "passed": True, "evidence": "pkg/calc.py:1"},
    ])
    reviewer = AdversarialReviewer(
        backend=_FakeReviewBackend(output), mutation_probe={"mode": "advisory"})
    t = Task.new("add double()")
    t.acceptance_criteria = ["double(x) returns 2x"]

    decision = await reviewer.review(t, repo_path=repo)

    mutation_items = [i for i in decision.checklist if i.label.startswith("mutation probe")]
    assert mutation_items, (
        "review() must fold mutation-probe findings into the checklist — "
        "none were found, so the gate-mode wiring to _apply_mutation_probe "
        "did not run"
    )
    assert any("pins" in i.label for i in mutation_items), (
        f"expected a killed ('pins') finding, got: {[i.label for i in mutation_items]}")
