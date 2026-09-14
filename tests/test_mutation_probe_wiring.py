"""Reviewer wiring for the mutation probe: `merge_mutation_findings` plus the
constructor/`from_config`/call-site contract in `AdversarialReviewer.review`.

The record-level behaviour (AC1/AC2/AC5's finding shape, and "findings never
flip a failing review to passing") is exercised directly against
`merge_mutation_findings`. The reviewer-integration behaviour (default off,
`from_config` wiring, a crash never failing the gate by itself, the probe
never touching the reviewed worktree) goes through the real `review()` path
with a `FakeBackend`, the same pattern `tests/test_reviewer.py` uses.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.core.task import Task
from no_human.review.reviewer import (
    AdversarialReviewer,
    ReviewDecision,
    merge_mutation_findings,
)
from no_human.review.selfcheck import ChecklistItem
from no_human.testing import mutation_probe

_PROBE_CONFIG = {
    "mode": "advisory",
    "max_tests": 12,
    "max_mutations_per_test": 3,
    "timeout_seconds": 300,
}


class FakeBackend:
    def __init__(self, final_text: str):
        self._final_text = final_text
        self.run_calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None):
        self.run_calls += 1
        return AgentResult(
            final_text=self._final_text,
            num_turns=3, is_error=False,
            tokens_used=200, session_id="fake", stop_reason="end_turn",
        )


def _block(passed: bool, items: list[dict]) -> str:
    data = {"passed": passed, "items": items}
    return f"REVIEW_JSON_START\n{json.dumps(data)}\nREVIEW_JSON_END\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def simple_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add calc + test")
    return repo


# --------------------------------------------------------------------------- #
# merge_mutation_findings                                                     #
# --------------------------------------------------------------------------- #


def test_a_survived_probe_blocks_a_passing_review():
    decision = ReviewDecision(passed=True, checklist=[
        ChecklistItem("everything looks fine", True, severity="low"),
    ])
    result = mutation_probe.MutationProbeResult(
        verdict="fail",
        probes=[mutation_probe.TestProbe(
            node_id="tests/test_calc.py::test_add", verdict="survived",
            target="calc.py:add", mutation="line 2: negate return",
            reason="the test stayed green under every generated mutation",
        )],
        tree_intact=True,
    )
    merged = merge_mutation_findings(decision, result, mode="advisory")

    assert merged is decision
    assert merged.passed is False
    assert any("survives mutation" in i.label for i in merged.blocking_items)


def test_a_killed_probe_is_recorded_in_the_review_record():
    decision = ReviewDecision(passed=True, checklist=[])
    result = mutation_probe.MutationProbeResult(
        verdict="pass",
        probes=[mutation_probe.TestProbe(
            node_id="tests/test_calc.py::test_add", verdict="killed",
            target="calc.py:add",
            mutation="line 2: negate return `a + b` -> `not (a + b)`",
            reason="1 failed in 0.01s\nAssertionError",
        )],
        tree_intact=True,
    )
    merged = merge_mutation_findings(decision, result, mode="advisory")

    assert merged.passed is True
    assert len(merged.checklist) == 1
    item = merged.checklist[0]
    assert item.passed is True
    assert "pins calc.py:add" in item.label
    assert "negate return" in item.evidence
    assert "AssertionError" in item.evidence

    d = merged.as_dict()
    assert d["items"][0]["evidence"] == item.evidence
    assert d["items"][0]["passed"] is True


def test_could_not_run_is_recorded_in_advisory_and_blocks_in_required():
    probe = mutation_probe.TestProbe(
        node_id="tests/test_calc.py::test_add", verdict="undetermined",
        reason="could not determine the code under test statically",
    )
    result = mutation_probe.MutationProbeResult(verdict="pass", probes=[probe])

    advisory_decision = merge_mutation_findings(
        ReviewDecision(passed=True, checklist=[]), result, mode="advisory")
    assert advisory_decision.passed is True
    assert len(advisory_decision.checklist) == 1
    assert advisory_decision.checklist[0].passed is True

    required_decision = merge_mutation_findings(
        ReviewDecision(passed=True, checklist=[]), result, mode="required")
    assert required_decision.passed is False
    assert any(not i.passed for i in required_decision.checklist)


def test_mutation_findings_never_flip_a_failing_review_to_passing():
    decision = ReviewDecision(passed=False, checklist=[
        ChecklistItem("some other real defect", False, severity="high"),
    ])
    result = mutation_probe.MutationProbeResult(
        verdict="pass",
        probes=[mutation_probe.TestProbe(
            node_id="tests/test_calc.py::test_add", verdict="killed",
            target="calc.py:add", mutation="line 2", reason="1 failed",
        )],
        tree_intact=True,
    )
    merged = merge_mutation_findings(decision, result, mode="advisory")
    assert merged.passed is False


# --------------------------------------------------------------------------- #
# Constructor / from_config                                                   #
# --------------------------------------------------------------------------- #


async def test_a_directly_constructed_reviewer_runs_no_probe(simple_repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("run_mutation_probe must not be called")

    monkeypatch.setattr(mutation_probe, "run_mutation_probe", boom)

    output = _block(True, [{"label": "ok", "passed": True, "evidence": "calc.py:1"}])
    reviewer = AdversarialReviewer(backend=FakeBackend(output))
    assert reviewer._mutation_probe is None

    t = Task.new("add calc")
    decision = await reviewer.review(t, repo_path=simple_repo)
    assert decision.passed is True


def test_from_config_turns_the_probe_on():
    reviewer = AdversarialReviewer.from_config({}, backend=FakeBackend("x"))
    assert reviewer._mutation_probe == _PROBE_CONFIG


# --------------------------------------------------------------------------- #
# review() call-site contract                                                 #
# --------------------------------------------------------------------------- #


async def test_a_crashing_probe_never_fails_the_gate_by_itself(simple_repo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(mutation_probe, "run_mutation_probe", boom)

    output = _block(True, [{"label": "ok", "passed": True, "evidence": "calc.py:1"}])
    reviewer = AdversarialReviewer(backend=FakeBackend(output), mutation_probe=_PROBE_CONFIG)

    t = Task.new("add calc")
    decision = await reviewer.review(t, repo_path=simple_repo)

    assert decision.passed is True
    assert any(
        "could not run" in i.label and "crashed" in i.label for i in decision.checklist)


async def test_the_probe_never_touches_the_reviewed_worktree(simple_repo):
    output = _block(True, [{"label": "ok", "passed": True, "evidence": "calc.py:1"}])
    reviewer = AdversarialReviewer(backend=FakeBackend(output), mutation_probe=_PROBE_CONFIG)

    t = Task.new("add calc")
    decision = await reviewer.review(t, repo_path=simple_repo)

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=simple_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert status == ""
    assert any("mutation probe" in i.label for i in decision.checklist)
