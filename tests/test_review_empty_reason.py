"""A failing review must say why, even when the reviewer named nothing.

Issue #249. `failure_reason` for a failed review was composed as
`"review failed: " + "; ".join(...)` over `blocking_items or failed_items`.
When both are empty the join yields `""` and the attempt is recorded with the
literal string `"review failed: "` and nothing after it. Three attempts across
the whole history landed that way, spending 159 turns and three attempt slots
on verdicts that explained nothing; with `max_attempts = 3` one such round is
a third of a task's allowance.

The two shapes that reach it are different facts and are kept apart:

  73dc6df1 a2, e42dfb2f a1   the reviewer produced NO checklist
  7f579176 a6               every checklist item PASSED, verdict still failed

The second is the sharper one, and the only such case in 529 failed reviews:
the verdict contradicts the evidence it published, so neither a human nor a
later agent can reason about it.

Neither shape takes `REVIEW_SESSION_ERROR_MARKER`. `reviewer.py` draws that
line and these stay on the documented side of it: a DEAD session is infra and
parks, while a reviewer that ran and produced no parseable verdict still
escalates to a person.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from no_human.review.reviewer import REVIEW_SESSION_ERROR_MARKER
from no_human.review.verdict_reason import review_failure_detail


class _Item:
    def __init__(self, label: str, evidence: str):
        self.label = label
        self.evidence = evidence


def _decision(n_items: int):
    return SimpleNamespace(passed=False, checklist=[object()] * n_items)


def test_findings_read_exactly_as_they_did_before():
    """The path that already worked must be byte-identical, or this change
    rewrites every failure reason in the fleet's history going forward."""
    failed = [_Item("a", "saw x"), _Item("b", "saw y")]
    assert (review_failure_detail(_decision(2), failed)
            == "review failed: a: saw x; b: saw y")


def test_only_the_first_three_findings_ride_out():
    """Unchanged from the call site this replaced."""
    failed = [_Item(str(i), "e%d" % i) for i in range(5)]
    detail = review_failure_detail(_decision(5), failed)
    assert "3: e3" not in detail and "2: e2" in detail


@pytest.mark.parametrize("n_items", [0, 2, 9])
def test_a_failing_review_never_records_a_bare_prefix(n_items):
    """The defect itself, across both shapes. The sizes are coverage rather
    than a census: the three historical attempts are two with no checklist and
    one whose every item passed."""
    detail = review_failure_detail(_decision(n_items), [])
    assert detail.strip() != "review failed:", "recorded a reason with nothing in it"
    assert len(detail) > len("review failed: ") + 40


def test_no_checklist_says_the_reviewer_produced_nothing():
    detail = review_failure_detail(_decision(0), [])
    assert "no checklist" in detail
    assert "unparseable" in detail


def test_an_all_passing_checklist_names_the_contradiction():
    """7f579176 a6: `review_passed = 0` with every item passing. The reason
    must say the verdict disagrees with its own evidence, and must tell the
    next round not to re-implement against a defect nobody stated."""
    detail = review_failure_detail(_decision(2), [])
    assert "2 checklist item(s) PASSED" in detail
    assert "contradicts" in detail
    assert "do not re-implement" in detail


@pytest.mark.parametrize("n_items", [0, 2])
def test_neither_shape_is_marked_as_a_dead_session(n_items):
    """`reviewer.py`'s marker means infra, and parks. These ran, so they
    escalate to a person instead; taking the marker here would route a real
    verdict into an infrastructure wait with nothing for a human to decide,
    which is the incident that marker was introduced for."""
    assert REVIEW_SESSION_ERROR_MARKER not in review_failure_detail(
        _decision(n_items), [])


def test_the_orchestrator_composes_the_reason_through_this_module():
    """Pins the wiring, not the helper. Every test above would still pass if
    the orchestrator went back to building the string inline, which is where
    the bug lived."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "no_human"
    tree = ast.parse((src / "core" / "orchestrator.py").read_text(encoding="utf-8"))

    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "review_failure_detail" in called, (
        "the orchestrator no longer routes the failed-review reason through "
        "verdict_reason, so an empty finding list can record a bare "
        '"review failed: " again (issue #249)'
    )
