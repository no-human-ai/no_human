"""An interrupted review must resume as a FULL review — live incident 2026-08-11
22:10 UTC, task 8c8b36b5, PR #253 (+191/-19).

The independent review was killed mid-run by a server restart. `_recover_orphans`
requeued the task and stamped `resume_provenance({"sha": ...}, "orphan_recovery")`
— a MACHINE label. The resumed coder correctly added nothing to a branch that
already carried the whole diff, filed an ALREADY-SATISFIED claim, and the claim
path took the task straight to `awaiting_approval` via the citation-only gate —
constraint #3 (evidence-based review before merge) was bypassed by the crash+
resume seam: the diff was never independently reviewed.

Fix under test: `Orchestrator._already_satisfied_eligible` — a resumed branch
that carries a diff vs base is only allowed to use the zero-diff ALREADY-
SATISFIED terminal when `task.context["review_history"]` already holds a round
stamped with THIS EXACT head sha and `passed=True`. A `review_start` with no
verdict is not a verdict, and an ancestor match is not this exact head either
(exact-sha equality only — see `test_a_review_that_only_started_is_not_a_verdict`).
"""
import subprocess

from no_human.blockers.taxonomy import resume_provenance
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import (  # noqa: F401
    _config, _git, bare_repo, FakeReviewer, ReviewDecision, ChecklistItem,
)
from .test_resume_wiring import ScriptedBackend, _ok  # noqa: F401


def _commit_on_main(work, name: str, body: str, subject: str) -> str:
    """Commit a file onto main and return its sha."""
    (work / name).write_text(body)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", subject)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                          capture_output=True, text=True).stdout.strip()


#: The exact statement `_parse_already_satisfied` accepts: the ALREADY-SATISFIED
#: marker on its own line plus at least one grammatical CRITERION line.
_ALREADY_SATISFIED_STATEMENT = (
    "Verified every criterion against the existing code.\n"
    "ALREADY-SATISFIED\n"
    "CRITERION: feature works — MET — evidence: feature.py:1\n"
)


def _interrupted_branch(bare_repo):
    """A real change committed onto main, with a `base-behind` ref one commit
    behind it — so `commits_ahead("base-behind") > 0` for a branch resumed onto
    the real commit, exactly the shape of an interrupted-review resume."""
    sha = _commit_on_main(
        bare_repo, "feature.py", "def feature():\n    return 1\n",
        "add feature")
    _git(bare_repo, "push", "origin", "main")
    _git(bare_repo, "branch", "-f", "base-behind", "HEAD~1")
    _git(bare_repo, "push", "-u", "origin", "base-behind")
    return sha


def _interrupted_task_context(sha: str) -> dict:
    """The real crash-restart provenance `_recover_orphans` writes — a MACHINE
    label, so `_is_own_partial` reports the branch point as the loop's own and
    the resumed attempt reaches the zero-diff claim path with a real diff still
    on the branch."""
    return {
        "resume_from": resume_provenance(
            {"sha": sha, "branch": "main"}, "orphan_recovery"),
        "resume_reason": "orphan_recovery",
        "base_branch": "base-behind",
        "eval_result": {"verdict": "accept"},
    }


def _claim_backend():
    """Edits nothing and reports the fully-cited ALREADY-SATISFIED claim —
    exactly what a resumed coder correctly does when the branch already
    carries the whole diff."""
    return ScriptedBackend(lambda cwd: _ok(_ALREADY_SATISFIED_STATEMENT))


async def test_interrupted_review_resumes_as_a_full_review(bare_repo, tmp_path, store):
    """AC1: no review_history at all (a `review_start` was emitted live and the
    process died before any verdict was recorded — nothing survives that into
    `task.context`) — the already-satisfied path must be refused and a full
    review dispatched against the real diff instead."""
    sha = _interrupted_branch(bare_repo)
    cfg = _config(tmp_path)
    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("feature works", True, "feature.py:1")],
    ))
    orch = Orchestrator(store, cfg.data, _claim_backend(), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("interrupted review must resume as a full review",
                 repo_path=str(bare_repo))
    t.context = _interrupted_task_context(sha)
    await store.create_task(t)

    await orch.run_task(t)

    assert not [c for c in reviewer.calls if c["mode"] == "already_satisfied"], (
        "the claim path ran the citation-only gate on an unreviewed diff: "
        f"{reviewer.calls}")
    assert [c for c in reviewer.calls if c["mode"] is None], (
        f"no full review was dispatched: {reviewer.calls}")
    assert [e for e in events if e.get("kind") == "already_satisfied_ineligible"], (
        f"no already_satisfied_ineligible event was emitted: {events}")


async def test_an_unstamped_round_does_not_count_as_a_verdict(bare_repo, tmp_path, store):
    """AC1 variant: an unstamped legacy round (`sha: ""`, `passed: True`) must
    NOT count as a verdict for this head — fails closed on data that predates
    stamping rather than crediting it by accident."""
    sha = _interrupted_branch(bare_repo)
    cfg = _config(tmp_path)
    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("feature works", True, "feature.py:1")],
    ))
    orch = Orchestrator(store, cfg.data, _claim_backend(), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("unstamped round is not a verdict", repo_path=str(bare_repo))
    ctx = _interrupted_task_context(sha)
    ctx["review_history"] = [
        {"round": 1, "sha": "", "passed": True, "blocking": [], "advisory": []},
    ]
    t.context = ctx
    await store.create_task(t)

    await orch.run_task(t)

    assert not [c for c in reviewer.calls if c["mode"] == "already_satisfied"], (
        f"an unstamped round was credited as a verdict: {reviewer.calls}")
    assert [c for c in reviewer.calls if c["mode"] is None]
    assert [e for e in events if e.get("kind") == "already_satisfied_ineligible"]


async def test_a_review_that_only_started_is_not_a_verdict(bare_repo, tmp_path, store):
    """AC1 negative half: `review_history` carries a PASSED round, but it is
    stamped at an EARLIER commit of the same branch, not this exact head — the
    gate must key on exact sha equality, not ancestry (an ancestor match would
    let a PASS on an earlier commit cover code added after it — the same
    laundering defect one level up)."""
    earlier_sha = _commit_on_main(
        bare_repo, "earlier.py", "def earlier():\n    return 0\n",
        "earlier commit on main")
    _git(bare_repo, "push", "origin", "main")
    sha = _commit_on_main(
        bare_repo, "feature.py", "def feature():\n    return 1\n",
        "add feature")
    _git(bare_repo, "push", "origin", "main")
    _git(bare_repo, "branch", "-f", "base-behind", earlier_sha + "~1")
    _git(bare_repo, "push", "-u", "origin", "base-behind")

    cfg = _config(tmp_path)
    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("feature works", True, "feature.py:1")],
    ))
    orch = Orchestrator(store, cfg.data, _claim_backend(), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("a review that only started is not a verdict",
                 repo_path=str(bare_repo))
    ctx = _interrupted_task_context(sha)
    ctx["review_history"] = [
        {"round": 1, "sha": earlier_sha, "passed": True, "blocking": [], "advisory": []},
    ]
    t.context = ctx
    await store.create_task(t)

    await orch.run_task(t)

    assert not [c for c in reviewer.calls if c["mode"] == "already_satisfied"], (
        f"a PASS on an earlier commit was credited to a later head: {reviewer.calls}")
    assert [c for c in reviewer.calls if c["mode"] is None]
    assert [e for e in events if e.get("kind") == "already_satisfied_ineligible"]


async def test_a_head_sha_with_a_passing_verdict_keeps_the_already_satisfied_path(
    bare_repo, tmp_path, store
):
    """AC2 regression pin: a `review_history` round stamped with THIS EXACT
    head sha and `passed=True` means the diff has already been judged — the
    already-satisfied path stays available and reaches awaiting_approval."""
    sha = _interrupted_branch(bare_repo)
    cfg = _config(tmp_path)
    reviewer = FakeReviewer(ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("feature works", True, "feature.py:1")],
    ))
    orch = Orchestrator(store, cfg.data, _claim_backend(), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("head sha with a passing verdict keeps already-satisfied",
                 repo_path=str(bare_repo))
    ctx = _interrupted_task_context(sha)
    ctx["review_history"] = [
        {"round": 1, "sha": sha, "passed": True, "blocking": [], "advisory": []},
    ]
    t.context = ctx
    await store.create_task(t)

    final = await orch.run_task(t)

    assert [c for c in reviewer.calls if c["mode"] == "already_satisfied"], (
        f"a head sha with a recorded PASS verdict was routed to a full review: "
        f"{reviewer.calls}")
    assert final.status is TaskStatus.AWAITING_APPROVAL, final.detail
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "succeeded", attempts[-1]


async def test_a_true_zero_diff_claim_with_no_branch_diff_is_unaffected(
    bare_repo, tmp_path, store
):
    """Pin the untouched case: an ordinary already-satisfied claim with NO
    commits ahead of base (nothing was ever resumed) must still reach the
    already-satisfied path — the eligibility gate must not fire on every
    ordinary claim, only on a resumed branch that actually carries a diff no
    verdict covers. Equivalent coverage already exists at
    `tests/test_e2e_orchestrator.py::test_zero_diff_already_satisfied_claim_reaches_the_human_gate`
    and `tests/test_resume_wiring_round2.py:735`; both stay green, untouched."""
    cfg = _config(tmp_path)
    reviewer = FakeReviewer(ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("feature works", True, "feature.py:1")],
    ))
    orch = Orchestrator(store, cfg.data, _claim_backend(), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("true zero-diff claim, no branch diff", repo_path=str(bare_repo))
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    final = await orch.run_task(t)

    assert [c for c in reviewer.calls if c["mode"] == "already_satisfied"], (
        f"an ordinary zero-diff claim was routed to a full review: {reviewer.calls}")
    assert final.status is TaskStatus.AWAITING_APPROVAL, final.detail
