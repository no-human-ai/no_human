"""D6 stagnation escalation must name the recurring finding.

`_recurring_finding` already computes WHICH failing review labels recur
across the last two attempts (a majority word-overlap match) before it was
collapsed to a bare bool and thrown away. These tests pin the refactor:
the D6 blocker now carries `evidence`/`tried`/a `question` that names the
labels, the firing condition (majority rule, 0.4 per-pair threshold, "minor
issue" exclusion) is unchanged, and the sibling failed-restoration STAGNATION
blocker twelve lines below is untouched.
"""

import pytest

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.profile import ProjectProfile
from no_human.review.reviewer import ReviewDecision
from no_human.review.selfcheck import ChecklistItem

from .test_e2e_orchestrator import (  # noqa: F401
    FakeBackend, SequencedFakeReviewer, _config, bare_repo,
)

# Copied locally from tests/test_failed_restoration.py (per the task's
# out-of-scope list, that file is import-only, never edited).
_TREE_GATE = "sh -c 'python3 -c \"from calc import add; assert add(1,2)==3\"'"


async def _gate_profile(store, repo_path):
    prof = ProjectProfile(
        repo_path=str(repo_path), ecosystem="custom", test_cmd=_TREE_GATE,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)


def _broken_calc_same_way(cwd):
    """A real diff whose gate fails — identically, every attempt."""
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n"   # the same wrong change each time
    )


def _make_mutate_correct_add_mul():
    """Factory, not a bare function: each attempt now branches from the
    PREVIOUS attempt's own gate-failed commit (this task's fix), so a fixed,
    byte-identical rewrite on the SECOND call is a genuine zero-diff against
    the inherited tree — caught by the honesty gate as "agent produced no
    file changes" rather than reaching review again. The code fix here is
    already correct on attempt 1; what's still wrong is unrelated (the
    reviewer's scripted Jenkinsfile findings), so a realistic coder would
    still touch something each turn in response — model that with a
    per-call counter comment so every attempt is a real (if still
    insufficient) diff. A bare module-level function shared by two tests
    would leak call counts across them; each test must get its own closure."""
    calls = []

    def mutate(cwd):
        calls.append(1)
        (cwd / "calc.py").write_text(
            f"# attempt {len(calls)}\n"
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
    return mutate


async def test_d6_escalation_names_the_recurring_finding(bare_repo, tmp_path, store):
    # Each attempt now branches from the PREVIOUS attempt's own gate-failed
    # commit (that is this task's fix) instead of re-branching from base. A
    # coder that writes byte-identical content on top of a tree that already
    # has it produces a genuine zero-diff — correctly caught by the honesty
    # gate as "agent produced no file changes", not a second review-worthy
    # commit. A real coder, told the SAME finding recurs, would still write
    # SOMETHING each turn (even if it doesn't fix the root cause) — so the
    # fixture must too: tag each attempt's content with a counter to keep it a
    # real (if still broken) diff, so attempt 2 reaches review again and D6's
    # recurring-finding check (the thing this test exists to pin) actually
    # gets a chance to fire, exactly as it did before commits were inherited.
    calls = []

    def mutate(cwd):
        calls.append(1)
        (cwd / "calc.py").write_text(
            f"# attempt {len(calls)}\ndef add(a, b):\n    return 0  # broken impl\n")

    decisions = [
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("commitSha undefined reference", False, "Jenkinsfile:883"),
            ChecklistItem("PR comment pagination missing per_page param", False,
                          "Jenkinsfile:760"),
        ]),
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("commitSha undefined reference", False,
                          "Jenkinsfile:883 (still)"),
            ChecklistItem("PR comment pagination still missing per_page param", False,
                          "Jenkinsfile:760"),
        ]),
    ]
    cfg = _config(tmp_path)
    reviewer = SequencedFakeReviewer(decisions)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("fix add()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert len(await store.list_attempts(t.id)) == 2
    blocker = outcome.task.blocker
    assert blocker["category"] == "STAGNATION"
    assert "commitsha undefined reference" in blocker["evidence"].lower()
    assert "appears stuck" not in blocker["question"]
    assert "commitsha" in blocker["question"].lower()
    attempt_log = list((outcome.task.context or {}).get("attempt_log") or [])
    assert attempt_log, "attempt log must be populated for the assertion to be meaningful"
    assert blocker["tried"] == attempt_log
    assert not blocker["options"]


@pytest.mark.slow  # real subprocess work through the full pipeline, twice over
async def test_d6_still_ignores_wholly_different_findings_at_the_same_rate(
    bare_repo, tmp_path, store
):
    """Regression control: the boolean->list refactor of `_recurring_finding`
    must not widen the detector. Same shape as
    test_stagnation_not_triggered_by_different_findings_same_rate in
    test_e2e_orchestrator.py."""
    decisions = [
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("commitSha undefined reference", False, "Jenkinsfile:883"),
            ChecklistItem("PR comment pagination missing per_page param", False,
                          "Jenkinsfile:760"),
        ]),
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("Image reuse broken across Jenkins agents", False,
                          "Jenkinsfile:653"),
            ChecklistItem("Selfcheck fixture passes for the wrong reason", False,
                          "scripts/x.groovy:52"),
        ]),
        ReviewDecision(passed=True, checklist=[
            ChecklistItem("all findings addressed", True, "verified"),
        ]),
    ]
    cfg = _config(tmp_path)
    reviewer = SequencedFakeReviewer(decisions)
    orch = Orchestrator(store, cfg.data, FakeBackend(_make_mutate_correct_add_mul()),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("fix things", repo_path=str(bare_repo))
    t.acceptance_criteria = ["things are fixed"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert len(reviewer.calls) == 3


@pytest.mark.slow  # real subprocess work through the full pipeline, three times over
async def test_minor_issues_bucket_alone_does_not_fire_d6(bare_repo, tmp_path, store):
    """The "minor issue" exclusion in `_failing_labels` is load-bearing: it
    always recurs (reviewer.py's overflow bucket) regardless of whether the
    actual minor issues are the same, so on its own it must never be treated
    as evidence of a recurring specific finding."""
    decisions = [
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("Minor issues (3 more)", False, "boilerplate overflow bucket"),
            ChecklistItem("database migration script missing rollback", False,
                          "migrations/001.sql"),
        ]),
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("Minor issues (3 more)", False, "boilerplate overflow bucket"),
            ChecklistItem("frontend button color contrast fails accessibility", False,
                          "ui/Button.tsx"),
        ]),
        ReviewDecision(passed=True, checklist=[
            ChecklistItem("all findings addressed", True, "verified"),
        ]),
    ]
    cfg = _config(tmp_path)
    reviewer = SequencedFakeReviewer(decisions)
    orch = Orchestrator(store, cfg.data, FakeBackend(_make_mutate_correct_add_mul()),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("fix things", repo_path=str(bare_repo))
    t.acceptance_criteria = ["things are fixed"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert len(reviewer.calls) == 3


async def test_failed_restoration_blocker_shape_is_unchanged(bare_repo, tmp_path, store):
    """Negative control: the failed-restoration STAGNATION blocker
    (orchestrator.py:~3110-3129) is untouched by the D6 refactor."""
    cfg = _config(tmp_path)
    await _gate_profile(store, bare_repo)
    backend = FakeBackend(_broken_calc_same_way)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("make add subtract-proof", repo_path=str(bare_repo), kind="feature")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    blocker = outcome.task.blocker
    assert blocker["category"] == "STAGNATION"
    assert "same failing state" in blocker["question"]
    assert "appears stuck" not in blocker["question"]
    assert "stuck on" not in blocker["question"]
    assert blocker["evidence"]
    assert len(blocker["evidence"]) <= 500
    assert blocker["tried"]
