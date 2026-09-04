"""Goal reachability is a binary, evidence-cited verdict — never a severity.

The defect this closes, reproduced twice on the recorded startup-scenario
replays: the fresh-context reviewer FOUND the fatal "feature built in the rate
engine, never wired through the sole production caller" defect, cited it,
graded it `[low] advisory`, and PASSED the task. Detection was fine; the
severity scalar is where it died. Constraint #3 (evidence-based review, never
a numeric self-scoring gate) means the fix is not "grade harder": the ticket's
outcome not happening is now a separate `goal` block in the verdict JSON that
`_gate_verdict` consumes mechanically, the same constitutional shape as the
existing `spec_compliance` stage rule.

Hallucination guard: a `reachable: false` whose entry_point citation does not
check out is demoted through the existing demoted-citations channel and never
blocks. An ABSENT goal block gates exactly as before the field existed, and
the orchestrator announces the absence (`review_goal_missing`).
"""

import json

import pytest

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import (
    ReviewDecision,
    _VERDICT_FORMAT,
    _build_review_prompt,
    _parse_review_output,
)
from no_human.review.selfcheck import ChecklistItem

from .test_e2e_orchestrator import (  # noqa: F401
    FakeBackend,
    _config,
    _git,
    bare_repo,
)


def _payload(items=None, *, passed=True, goal="OMIT", stages=None) -> str:
    data = {"passed": passed,
            "items": items if items is not None
            else [{"label": "criteria met", "passed": True, "severity": "low",
                   "evidence": "f.py:1"}]}
    if goal != "OMIT":
        data["goal"] = goal
    if stages is not None:
        data["stages"] = stages
    return "REVIEW_JSON_START " + json.dumps(data) + " REVIEW_JSON_END"


# ----------------------------- parser round-trip ---------------------------- #


def test_goal_block_round_trips_through_parse_and_as_dict():
    goal = {"reachable": True, "entry_point": "src/app.py:12",
            "evidence": "handle() -> quote() at src/app.py:12"}
    d = _parse_review_output(_payload(goal=goal))
    assert d.goal == goal
    assert d.as_dict()["goal"] == goal


def test_absent_goal_is_none_and_gates_exactly_as_today():
    d = _parse_review_output(_payload())
    assert d.goal is None
    assert "goal" not in d.as_dict()
    assert d.passed is True


def test_malformed_goal_is_treated_as_absent():
    d = _parse_review_output(_payload(goal="not reachable"))
    assert d.goal is None
    assert d.passed is True


# ------------------------- the verdict rule itself -------------------------- #


def test_reachable_false_fails_the_gate_regardless_of_severity_words():
    """The reproduced defect verbatim: every finding graded low, reviewer says
    passed — and the request's outcome never happens through the production
    caller. Severity words must not wave that through."""
    d = _parse_review_output(_payload(
        items=[{"label": "Dimensions not reachable via handle()",
                "passed": False, "severity": "low",
                "evidence": "api.py:15 forwards only weight_kg"}],
        passed=True,
        goal={"reachable": False, "entry_point": "api.py:15",
              "evidence": "handle() never forwards dimensions to quote()"},
    ))
    assert d.passed is False


def test_reachable_true_changes_nothing():
    d = _parse_review_output(_payload(
        goal={"reachable": True, "entry_point": "api.py:15",
              "evidence": "handle() -> quote()"}))
    assert d.passed is True


def test_reachable_false_with_a_bogus_citation_is_demoted_not_blocking(tmp_path):
    """The hallucination guard: an entry_point naming a file that exists
    nowhere routes through the demoted-citations channel and must not block."""
    (tmp_path / "real.py").write_text("x = 1\n")
    d = _parse_review_output(
        _payload(goal={"reachable": False,
                       "entry_point": "no/such/file.py:3",
                       "evidence": "made up"}),
        repo_path=tmp_path, before_ref="HEAD",
    )
    assert d.passed is True
    assert d.goal["demoted"] is True
    assert any(s.startswith("goal reachability:") for s in d.demoted_citations)


def test_reachable_false_with_no_entry_point_is_demoted(tmp_path):
    """A goal veto is a blocking claim; with no citation at all it must not
    block — unlike findings, where absence of a citation is not punished."""
    d = _parse_review_output(
        _payload(goal={"reachable": False, "entry_point": "",
                       "evidence": "trust me"}),
        repo_path=tmp_path, before_ref="HEAD",
    )
    assert d.passed is True
    assert any("no entry_point cited" in s for s in d.demoted_citations)


def test_reachable_false_with_a_verified_citation_blocks(tmp_path):
    (tmp_path / "api.py").write_text("def handle(req):\n    return {}\n")
    d = _parse_review_output(
        _payload(goal={"reachable": False, "entry_point": "api.py:1",
                       "evidence": "handle() never calls the new code"}),
        repo_path=tmp_path, before_ref="HEAD",
    )
    assert d.passed is False
    assert "demoted" not in d.goal


def test_without_a_repo_path_the_goal_verdict_still_applies():
    """No repo to verify against (unit callers): the veto stands as stated."""
    d = _parse_review_output(_payload(
        goal={"reachable": False, "entry_point": "api.py:1", "evidence": "e"}))
    assert d.passed is False


# ------------------------------ prompt surface ------------------------------ #


def _task(**kw):
    return Task.new(kw.pop("title", "wire dimensions"), **kw)


def test_gate_prompt_shows_the_request_when_description_is_set():
    t = _task(description="Billing must use volumetric weight via quote().")
    p = _build_review_prompt(t, "diff", "", "")
    assert "Task request (verbatim from the ticket):" in p
    assert "Billing must use volumetric weight via quote()." in p


def test_gate_prompt_skips_the_request_section_when_description_is_empty():
    p = _build_review_prompt(_task(description="   "), "diff", "", "")
    assert "Task request" not in p


def test_gate_prompt_caps_the_request_and_says_so():
    t = _task(description="x" * 2500)
    p = _build_review_prompt(t, "diff", "", "")
    assert "x" * 2000 in p
    assert "x" * 2001 not in p
    assert "(request truncated)" in p


def test_gate_prompt_carries_the_goal_reachability_instruction():
    p = _build_review_prompt(_task(), "diff", "", "")
    assert "GOAL REACHABILITY" in p
    assert '"goal"' in p  # the verdict JSON asks for the block
    assert "never\n  called by any production path is NOT reachable" in p
    # the ns-1746bea3 canary clause: a request that IS an uncalled artifact
    assert "reachable\n  means that artifact exists as requested" in p


def test_verdict_format_requires_the_goal_block():
    assert '"goal": {"reachable": true_or_false' in _VERDICT_FORMAT
    assert "entry_point" in _VERDICT_FORMAT


def test_wiring_evidence_renders_next_to_lint_evidence():
    p = _build_review_prompt(
        _task(), "diff", "", "",
        lint_evidence="Evidence: ruff (deterministic)",
        wiring_evidence="WIRING EVIDENCE (deterministic, static)",
    )
    assert "Evidence: ruff" in p
    assert "WIRING EVIDENCE" in p


# --------------------------- orchestrator events ---------------------------- #


class _GoalStub:
    """Reviewer stub returning a fixed decision, recording nothing."""

    def __init__(self, decision):
        self._decision = decision
        self._on_event = None

    async def review(self, task, **kwargs):
        return self._decision


def _orch(store, tmp_path, events, decision):
    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    return Orchestrator(
        store, cfg.data, FakeBackend(lambda cwd: (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n"
            "    return a * b\n")),
        SlackNotifier(None), reviewer=_GoalStub(decision),
        event_sink=events.append,
    )


def _passing_decision(goal):
    return ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("criteria met", True, "stub")],
        goal=goal,
    )


async def test_absent_goal_emits_review_goal_missing(bare_repo, tmp_path, store):
    events = []
    orch = _orch(store, tmp_path, events, _passing_decision(goal=None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)
    kinds = [e.get("kind") for e in events]
    assert "review_goal_missing" in kinds


async def test_present_goal_emits_no_missing_event(bare_repo, tmp_path, store):
    events = []
    orch = _orch(store, tmp_path, events, _passing_decision(
        goal={"reachable": True, "entry_point": "calc.py:1", "evidence": "e"}))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)
    kinds = [e.get("kind") for e in events]
    assert "review_goal_missing" not in kinds


async def test_unreachable_goal_is_announced(bare_repo, tmp_path, store):
    events = []
    decision = ReviewDecision(
        passed=False,
        checklist=[ChecklistItem("built but unwired", False, "stub",
                                 severity="low")],
        goal={"reachable": False, "entry_point": "calc.py:1", "evidence": "e"},
    )
    orch = _orch(store, tmp_path, events, decision)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)
    hits = [e for e in events if e.get("kind") == "review_goal_unreachable"]
    assert hits and "calc.py:1" in hits[0]["text"]
