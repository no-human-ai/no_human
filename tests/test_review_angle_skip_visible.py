"""Pins the b2e6f96c fix: a complex-tier review angle that reaches no
verdict must NEVER be recorded as a passing checklist item — that is exactly
what let task b2e6f96c ship with an inert acceptance test while its `tests`
angle silently did not run and was marked green.

The fix must, simultaneously:
  * never record a no-verdict angle as `passed=True`
  * never let a no-verdict angle fail the gate by itself (the R17
    regression: a fail-closed sentinel with no severity was once read as
    BLOCKING by `merge_angle_findings`, flipping a passing gate to FAIL and
    telling the coder to fix the reviewer's own missing output)
  * surface the skip in three places: the checklist comment, the PR body,
    and the merge-ready policy report
  * leave a clean angle run (one that reaches a real verdict) completely
    unaffected — no new noise

These tests exercise each of those independently, plus the historical-data
reader (`skipped_angles_from_checklist`) that has to keep reading 77 rows
written by the pre-fix code with `passed=True`.
"""

from __future__ import annotations

from no_human.agent.claude_backend import AgentResult
from no_human.core.merge_policy import (
    GateFacts,
    _check_required_angles_ran,
    facts_from_evidence,
)
from no_human.core.orchestrator import Orchestrator
from no_human.core.pr_evidence import PrEvidence
from no_human.core.task import Task
from no_human.review.reviewer import (
    AdversarialReviewer,
    REQUIRED_ANGLES,
    REVIEW_ANGLES,
    skipped_angles_from_checklist,
)

_MAIN_PASS = (
    'REVIEW_JSON_START\n{"passed": true, "items": '
    '[{"label": "ok", "passed": true, "severity": "low",'
    ' "evidence": "d.py:1"}]}\nREVIEW_JSON_END'
)
_NO_VERDICT_TEXT = "I looked at the diff but could not reach a conclusion."


def _angle_pass_text(name: str) -> str:
    return (
        f'REVIEW_JSON_START\n{{"passed": true, "items": '
        f'[{{"label": "{name} ok", "passed": true, "severity": "low",'
        f' "evidence": "d.py:1"}}]}}\nREVIEW_JSON_END'
    )


class _StubBackend:
    """Call 1 is always the main review (pass). Every call after that is an
    angle pass (first attempt, then — only for angles that reached no
    verdict — its one bounded retry), consumed strictly in `REVIEW_ANGLES`
    order (the same order `test_angle_timeout_never_fails_the_gate`
    relies on: `asyncio.gather` schedules the coroutines in submission
    order, and this stub's `run()` increments its counter before awaiting
    anything, so the first N calls after the main review land in angle
    order).

    `angle_texts` maps a 0-based "angle call index" (0..len(REVIEW_ANGLES)-1
    for first attempts, continuing past that for retries in the same order)
    to the text to return; any index missing from the map returns
    `_NO_VERDICT_TEXT`.
    """

    model = "claude-opus-5"

    def __init__(self, angle_texts: dict[int, str] | None = None):
        self.calls = 0
        self._angle_texts = angle_texts or {}

    async def run(self, prompt, **kw):
        self.calls += 1
        if self.calls == 1:
            return AgentResult(
                final_text=_MAIN_PASS, num_turns=1, is_error=False,
                tokens_used=10, session_id="s", stop_reason="end",
            )
        idx = self.calls - 2
        text = self._angle_texts.get(idx, _NO_VERDICT_TEXT)
        return AgentResult(
            final_text=text, num_turns=1, is_error=False,
            tokens_used=5, session_id="s", stop_reason="end",
        )


def _complex_task(tmp_path):
    t = Task.new("big task", repo_path=str(tmp_path))
    t.context = {"complexity_tier": "complex"}
    return t


async def test_a_no_verdict_angle_is_recorded_not_passed_and_never_green(tmp_path):
    """Every angle (and its bounded retry) reaches no verdict. Each resulting
    'did not run' checklist item must be `passed=False` — a no-verdict angle
    must never be recorded as passing."""
    backend = _StubBackend({})  # every angle call, incl. retries, is no-verdict
    r = AdversarialReviewer(backend=backend)
    t = _complex_task(tmp_path)
    d = await r.review(t, repo_path=tmp_path, diff_override="+ x = 1\n")

    notes = [i for i in d.checklist if "did not run" in i.label]
    assert len(notes) == len(REVIEW_ANGLES)
    assert all(i.passed is False for i in notes), (
        "a no-verdict angle must NEVER be recorded as a passing checklist item"
    )
    assert all(i.severity == "low" for i in notes)
    assert any("tests angle did not run" in i.label for i in notes)


async def test_a_no_verdict_angle_is_retried_exactly_once_then_recorded(tmp_path):
    """One bounded retry per no-verdict angle — never zero (that would be the
    pre-fix bug), never more than one (that would be unbounded retry cost)."""
    backend = _StubBackend({})
    r = AdversarialReviewer(backend=backend)
    t = _complex_task(tmp_path)
    await r.review(t, repo_path=tmp_path, diff_override="+ x = 1\n")

    # 1 main review + one first attempt per angle + one retry per angle.
    assert backend.calls == 1 + 2 * len(REVIEW_ANGLES)


async def test_a_no_verdict_angle_never_fails_the_gate_and_never_reaches_the_coder(tmp_path):
    """R17 regression pin: a fail-closed angle sentinel with no severity was
    once read as BLOCKING by `merge_angle_findings`, flipping a passing gate
    to FAIL and telling the coder to fix the reviewer's own missing output.
    This must never come back — a no-verdict angle stays advisory-shaped and
    the main gate's PASS stands."""
    backend = _StubBackend({})
    r = AdversarialReviewer(backend=backend)
    t = _complex_task(tmp_path)
    d = await r.review(t, repo_path=tmp_path, diff_override="+ x = 1\n")

    assert d.passed is True, "a no-verdict angle must never fail the gate"
    assert d.blocking_items == [], (
        "a no-verdict angle's checklist item must never be blocking-shaped"
    )


async def test_an_angle_that_runs_and_passes_leaves_a_clean_gate(tmp_path):
    """Counter-case: when every angle actually reaches a verdict (and
    passes), the fix must add no new noise — no 'did not run' items, a clean
    PASS, no retries spent."""
    texts = {i: _angle_pass_text(name) for i, (name, _focus) in enumerate(REVIEW_ANGLES)}
    backend = _StubBackend(texts)
    r = AdversarialReviewer(backend=backend)
    t = _complex_task(tmp_path)
    d = await r.review(t, repo_path=tmp_path, diff_override="+ x = 1\n")

    notes = [i for i in d.checklist if "did not run" in i.label]
    assert notes == []
    assert d.passed is True
    assert d.blocking_items == []
    # No retries spent: 1 main + 1 per angle, nothing more.
    assert backend.calls == 1 + len(REVIEW_ANGLES)


async def test_the_checklist_comment_renders_the_skip_as_unfinished(tmp_path):
    """The PR's checklist comment (`_review_checklist_comment`) must render a
    skipped angle's row with the FAIL glyph, never the PASS glyph — a human
    scanning the comment must see it as unfinished, not green."""
    backend = _StubBackend({})
    r = AdversarialReviewer(backend=backend)
    t = _complex_task(tmp_path)
    d = await r.review(t, repo_path=tmp_path, diff_override="+ x = 1\n")

    comment = Orchestrator._review_checklist_comment(t, d.as_dict())
    lines = [ln for ln in comment.splitlines() if "did not run" in ln]
    assert lines, "expected at least one 'did not run' row in the checklist comment"
    for ln in lines:
        assert ln.strip().startswith("| ❌"), (
            f"a skipped angle row must render as failed/unfinished, not passing: {ln!r}"
        )
        assert not ln.strip().startswith("| ✅")


def test_the_pr_body_names_the_skipped_angle():
    """The PR body's evidence table must name a skipped angle — never fold
    it silently into the verdict row."""
    rv = {
        "rounds": 1, "verdict": "PASSED", "addressed": [],
        "advisory_count": 0,
        "angles_skipped": ["tests"],
        "angles_skipped_required": ["tests"],
    }
    evidence = PrEvidence(review_verdict=rv)

    pin = evidence.review_angles_pin()
    assert pin == "1 angle did not run — tests (REQUIRED)"
    # Registered as its own truth pin — `review_verdict` is already taken by
    # `review_verdict_pin()`.
    assert evidence.truth_pins()["review_angles"] == pin

    t = Task.new("big task", repo_path="/r")
    row = Orchestrator._review_evidence_section(t, evidence=evidence)
    assert "Review angles" in row
    assert "⚠️" in row
    assert pin in row

    # Multiple skipped angles, only some required.
    rv2 = {
        "rounds": 1, "verdict": "PASSED", "addressed": [],
        "advisory_count": 0,
        "angles_skipped": ["tests", "maintainability"],
        "angles_skipped_required": ["tests"],
    }
    evidence2 = PrEvidence(review_verdict=rv2)
    pin2 = evidence2.review_angles_pin()
    assert pin2 == "2 angles did not run — maintainability, tests (REQUIRED)"

    headline = evidence2.headline()
    assert "Review gate incomplete" in headline


def test_review_angles_pin_is_none_when_nothing_skipped():
    rv = {
        "rounds": 1, "verdict": "PASSED", "addressed": [],
        "advisory_count": 0, "angles_skipped": [], "angles_skipped_required": [],
    }
    evidence = PrEvidence(review_verdict=rv)
    assert evidence.review_angles_pin() is None
    assert "review_angles" not in evidence.truth_pins()

    t = Task.new("big task", repo_path="/r")
    row = Orchestrator._review_evidence_section(t, evidence=evidence)
    assert "Review angles" not in row


def test_skipped_angles_from_checklist_reads_historical_passed_true_rows():
    """The 77 historical rows this fix inherits were written with the bug:
    `passed=True` on a 'did not run' item. The reader must still recognize
    them as skipped, regardless of the stored `passed` value."""
    checklist = {
        "passed": True,
        "items": [
            {
                "label": "tests angle did not run (reached no verdict)",
                "passed": True,  # the bug this task fixes
                "severity": "",
            },
            {"label": "security ok", "passed": True, "severity": "low"},
            {
                "label": "maintainability angle did not run (timed out)",
                "passed": True,
                "severity": "",
            },
        ],
    }
    skipped, required = skipped_angles_from_checklist(checklist)
    assert sorted(skipped) == ["maintainability", "tests"]
    assert required == ["tests"]

    # Also tolerant of the STORED (JSON string) shape.
    import json
    skipped2, required2 = skipped_angles_from_checklist(json.dumps(checklist))
    assert sorted(skipped2) == ["maintainability", "tests"]
    assert required2 == ["tests"]

    # And of None / malformed input — never raises.
    assert skipped_angles_from_checklist(None) == ([], [])
    assert skipped_angles_from_checklist("not json") == ([], [])
    assert skipped_angles_from_checklist({"items": "not a list"}) == ([], [])


def test_merge_policy_is_not_ready_when_a_required_angle_never_ran():
    facts = GateFacts(review_passed=True, angles_skipped_required=("tests",),
                       angles_skipped=("tests",))
    ok, detail = _check_required_angles_ran(facts, None)
    assert ok is False
    assert "tests" in detail


def test_a_non_required_skipped_angle_is_advisory_not_binding():
    facts = GateFacts(review_passed=True, angles_skipped=("maintainability",))
    ok, detail = _check_required_angles_ran(facts, None)
    assert ok is True
    assert "maintainability" in detail


def test_facts_from_evidence_reads_angles_skipped_from_the_review_verdict():
    rv = {
        "rounds": 1, "verdict": "PASSED", "advisory_count": 0,
        "angles_skipped": ["tests"], "angles_skipped_required": ["tests"],
    }
    evidence = PrEvidence(review_verdict=rv)
    facts = facts_from_evidence(evidence)
    assert facts.angles_skipped == ("tests",)
    assert facts.angles_skipped_required == ("tests",)
