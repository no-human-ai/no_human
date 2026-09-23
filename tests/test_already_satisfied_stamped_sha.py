"""An already-satisfied PASS must stamp the commit it reviewed onto the
ATTEMPT ROW, not just onto `review_history`.

Root cause (measured live, task f5ee04d2): `_gate_already_satisfied`'s PASS
branch calls `_conclude_review_round`/`_append_review_history` with the
resolved `reviewed_sha` (the f27f3b73 fix), but the very next call —
`update_attempt(..., review_passed=1, status="succeeded")` — omits
`commit_sha` entirely. The attempt row is left with `review_passed=1,
commit_sha=NULL`: gate-ready on the board, but every consumer keyed on the
attempts table (`_send_back_resume_round`/`_land_no_changes_needed`,
`nh approve`/the API via `latest_attempt_branch()["commit_sha"]` ->
`land_task(tested_commit_sha=...)`) sees nothing, and the PR keeps pointing
at a pre-review branch head.

Fixture block (bare_repo, _git, _config, AlreadySatisfiedBackend,
FakeReviewer, _PASSING) is copied — not imported — from
tests/test_already_satisfied_evidence.py:64-193, this suite's convention for
already-satisfied fixtures. `_ALREADY_SATISFIED_CLAIM` and the direct-gate
driving pattern are copied from
tests/test_e2e_orchestrator.py:4442-4460 (test_review_only_recovery_round_stamps_the_reviewed_sha).
"""

from __future__ import annotations

import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.cli.commands import _review_pass_evidence
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewDecision
from no_human.review.selfcheck import ChecklistItem
from no_human.vcs import GitRepo


# --------------------------------------------------------------------------- #
# git/orchestrator fixtures — copied from tests/test_already_satisfied_evidence.py #
# --------------------------------------------------------------------------- #

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


class AlreadySatisfiedBackend:
    """Zero edits + a fully-cited ALREADY-SATISFIED per-criterion claim.
    Copied from tests/test_e2e_orchestrator.py's AlreadySatisfiedBackend."""

    STATEMENT = (
        "Verified every criterion against the existing code.\n"
        "ALREADY-SATISFIED\n"
        "CRITERION: mul(a,b) returns product — MET — evidence: calc.py:4\n"
    )

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(final_text=self.STATEMENT, num_turns=3, is_error=False,
                           tokens_used=120, session_id="s", stop_reason="end_turn")


class FakeBackend:
    """Stands in for ClaudeBackend: applies a scripted file mutation.
    Copied from tests/test_e2e_orchestrator.py:65-78."""

    def __init__(self, mutate):
        self.mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


class FakeReviewer:
    """Injects a scripted ReviewDecision without running the LLM. Copied
    from tests/test_e2e_orchestrator.py's FakeReviewer."""

    def __init__(self, decision: ReviewDecision):
        self._decision = decision
        self.calls: list[dict] = []

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                     before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        self.calls.append({"task_id": task.id, "mode": kwargs.get("mode"),
                           "claim_report": kwargs.get("claim_report")})
        return self._decision


_PASSING = ReviewDecision(passed=True, checklist=[
    ChecklistItem("mul(a,b) returns product", True,
                  "calc.py:4 defines mul returning a*b")])


_ALREADY_SATISFIED_CLAIM = (
    "Verified every criterion against the existing code.\n"
    "ALREADY-SATISFIED\n"
    "CRITERION: mul(a,b) returns product — MET — evidence: calc.py:4\n"
)


def _commit_real_work(work_dir):
    """Simulate attempt 1: real, reviewable work already on the branch.
    Copied from tests/test_e2e_orchestrator.py:4429-4439."""
    (work_dir / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def mul(a, b):\n    return a * b\n"
    )
    (work_dir / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


# --------------------------------------------------------------------------- #
# AC1 — a PASS stamps the reviewed head onto the attempt row.                 #
# --------------------------------------------------------------------------- #

async def test_already_satisfied_pass_stamps_the_reviewed_sha_on_the_attempt_row(
    bare_repo, tmp_path, store
):
    branch = "no-human/stamp-1"
    _git(bare_repo, "checkout", "-b", branch)
    _commit_real_work(bare_repo)
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "add mul()")
    _git(bare_repo, "push", "-u", "origin", branch)

    repo = GitRepo(bare_repo)
    head = repo.head_sha()

    cfg = _config(tmp_path)
    reviewer = FakeReviewer(_PASSING)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer,
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"},
                "resume_from": {"by": "wake"}}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 2)

    outcome = await orch._gate_already_satisfied(
        t, repo, attempt_id, _ALREADY_SATISFIED_CLAIM, branch=branch,
        attempt_n=2,
    )
    await store.save_events(t.id, events)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    row = next(a for a in await store.list_attempts(t.id) if a["id"] == attempt_id)
    assert row["commit_sha"] == head, row
    assert int(row["review_passed"]) == 1, row
    assert row["status"] == "succeeded", row

    # Cross-check: the row cannot drift from the round record it belongs to.
    refreshed = await store.find_task(t.id)
    history = (refreshed.context or {}).get("review_history") or []
    assert history and history[-1]["sha"] == head, history

    already = [e for e in events if e.get("kind") == "already_satisfied"]
    assert already and already[-1].get("reviewed_sha") == head, already


# --------------------------------------------------------------------------- #
# AC2a — an unresolvable head stamps nothing and stays unmergeable.          #
# --------------------------------------------------------------------------- #

async def test_an_unresolvable_head_stamps_nothing_and_stays_unmergeable(
    bare_repo, tmp_path, store, monkeypatch
):
    branch = "no-human/stamp-2a"
    _git(bare_repo, "checkout", "-b", branch)
    _commit_real_work(bare_repo)
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "add mul()")

    repo = GitRepo(bare_repo)
    monkeypatch.setattr(
        GitRepo, "head_sha",
        lambda self: (_ for _ in ()).throw(RuntimeError("git rev-parse failed")),
    )

    cfg = _config(tmp_path)
    reviewer = FakeReviewer(_PASSING)
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"},
                "resume_from": {"by": "wake"}}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 2)

    outcome = await orch._gate_already_satisfied(
        t, repo, attempt_id, _ALREADY_SATISFIED_CLAIM, branch=branch,
        attempt_n=2,
    )

    assert outcome.status is TaskStatus.FAILED
    row = next(a for a in await store.list_attempts(t.id) if a["id"] == attempt_id)
    assert row["commit_sha"] is None, row
    assert int(row["review_passed"] or 0) == 0, row

    monkeypatch.undo()
    refreshed = await store.find_task(t.id)
    real_head = repo.head_sha()
    passed, evidence = _review_pass_evidence(refreshed.context or {}, real_head, repo)
    assert not passed, evidence
    assert "no review round is stamped with a commit reachable from the branch head" \
        in evidence, evidence


# --------------------------------------------------------------------------- #
# AC2b — a PASS whose subject resolves no sha stamps nothing (never falls     #
# back to repo.head_sha()).                                                   #
# --------------------------------------------------------------------------- #

async def test_a_pass_whose_subject_resolves_no_sha_stamps_nothing(
    bare_repo, tmp_path, store, monkeypatch
):
    branch = "no-human/stamp-2b"
    _git(bare_repo, "checkout", "-b", branch)
    _commit_real_work(bare_repo)
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "add mul()")
    _git(bare_repo, "push", "-u", "origin", branch)

    repo = GitRepo(bare_repo)
    real_head = repo.head_sha()

    async def _fake_subject(self, task, repo, *, base, branch):
        return (True, "", "subject", "", False, "origin/main")

    monkeypatch.setattr(Orchestrator, "_already_satisfied_subject", _fake_subject)

    cfg = _config(tmp_path)
    reviewer = FakeReviewer(_PASSING)
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"},
                "resume_from": {"by": "wake"}}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 2)

    outcome = await orch._gate_already_satisfied(
        t, repo, attempt_id, _ALREADY_SATISFIED_CLAIM, branch=branch,
        attempt_n=2,
    )

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    row = next(a for a in await store.list_attempts(t.id) if a["id"] == attempt_id)
    assert int(row["review_passed"]) == 1, row
    assert row["commit_sha"] is None, row
    assert row["commit_sha"] != real_head


# --------------------------------------------------------------------------- #
# AC3 — a normal commit-then-review round keeps its OWN commit_sha.          #
# --------------------------------------------------------------------------- #

async def test_a_normal_commit_then_review_round_keeps_its_own_commit_sha(
    bare_repo, tmp_path, store
):
    pre_run_head = GitRepo(bare_repo).head_sha()

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    reviewer = FakeReviewer(_PASSING)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    attempts = await store.list_attempts(t.id)
    delivering = attempts[-1]
    assert delivering["status"] == "succeeded", delivering
    assert delivering["commit_sha"], delivering
    assert delivering["commit_sha"] != pre_run_head, delivering
    bare = tmp_path / "remote.git"
    branch_tip = subprocess.run(
        ["git", "rev-parse", delivering["branch_name"]],
        cwd=bare, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert delivering["commit_sha"] == branch_tip, (delivering, branch_tip)
