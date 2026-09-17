"""A rework built after a human REJECTION can diverge from its own pushed tip.

BACKGROUND. `nh reject` sends a task back for changes without touching its
already-pushed branch or PR. The rework the agent then builds is not
guaranteed to be a descendant of that pushed tip — it is built fresh from
the base, not from the pushed commit — so the two lines diverge. At
delivery, the ancestor check in `Orchestrator._reconcile_remote_branch`
correctly refuses to push it (`ReviewedShaMismatch`) — and before this fix,
that refusal had no route forward: the rework was spent for nothing, and
the task looped through the same shape on every later attempt. This
occurred 10 times across 8 distinct tasks in production.

CHOSEN SHAPE: RECONVERGE (see `vcs/reconverge.py`'s module docstring for the
full rationale) — a sibling to `vcs/recut.py`'s RECUT, but for the opposite
situation: recut abandons the pushed tip on a fresh branch name; reconverge
keeps the SAME branch/PR by rebasing the rework onto its own pushed tip in
place, so the pushed tip becomes an ancestor of the new head and delivery
can fast-forward. `Orchestrator._recover_diverged_branch` tries reconverge
first (one-shot per branch, same bound `recut` enforces for itself) and
falls back to the pre-existing recut recovery when reconverge is not
applicable or itself fails — so a genuinely unavoidable divergence still
reaches a human, but now with a structured blocker that names both SHAs,
says which side carries more work, and offers a concrete merge option,
instead of bare prose and nothing actionable.

This file exercises Hook 1 (`Orchestrator._recover_diverged_branch`) and the
`vcs.reconverge` primitives directly. `vcs/recut.py`,
`Orchestrator._reconcile_remote_branch`, `_assert_delivery_sha`,
`_ahead_reviewed_candidate`, `_refresh_stale_base`, `GitRepo.rebase_onto`,
`GitRepo.merge_base_into_branch`, `GitRepo.push_sha_fast_forward`, and
`GitRepo.is_ancestor` are unmodified and untested here — see
`tests/test_branch_recut_after_divergence.py`,
`tests/test_recut_preserves_delivery_refusal.py`, and
`tests/test_base_staleness_pushed_branch.py` for their own coverage.
"""
from __future__ import annotations

import subprocess

import pytest

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator, ReviewedShaMismatch
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitError, GitRepo
from no_human.vcs.recut import branch_stem
from no_human.vcs.reconverge import (
    already_reconverged,
    divergence_summary,
    is_rework_after_rejection,
    reconverge,
)


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path, events=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(
        store, cfg.data, _Backend(), SlackNotifier(None),
        event_sink=(events.append if events is not None else None))


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                    check=True, capture_output=True, text=True)
    return bare


@pytest.fixture
def repo(tmp_path, origin):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return work


def _make_reworked_branch(work, name):
    """Push `name` once (the reviewed, already-pushed PR), then simulate a
    human REJECTION followed by a fresh rework built from BEFORE that
    pushed commit — `reset --hard HEAD~1` plus a brand-new commit — the
    exact "rework built on a line that is not a descendant of the pushed
    tip" shape the bug report describes. Distinct from `--amend` (which
    `tests/test_branch_recut_after_divergence.py::_make_pushed_diverged_
    branch` covers): a reset-then-recommit is what an agent starting a
    fresh rework attempt from the task's base actually produces.
    """
    _git(work, "checkout", "-q", "-b", name)
    (work / "pr_marker.py").write_text("# original PR work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "PR work")
    _git(work, "push", "-q", "-u", "origin", name)
    pushed_tip = _git(work, "rev-parse", name)
    _git(work, "reset", "-q", "--hard", "HEAD~1")
    (work / "rework_marker.py").write_text("# reworked after rejection\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "rework after rejection")
    return pushed_tip


def _make_locally_heavier_diverged_branch(work, name):
    """Like `_make_reworked_branch`, but the rework carries an extra commit
    on top, so the local side strictly outweighs the pushed side —
    exercises `DivergenceSummary.heavier == "local"`."""
    pushed_tip = _make_reworked_branch(work, name)
    (work / "extra.py").write_text("# more rework\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "more rework")
    return pushed_tip


def _make_unrelated_rework_branch(work, name):
    """Push `name` once, then replace it with an ORPHAN commit sharing no
    history with the pushed tip at all — the degenerate divergence shape
    `reconverge` cannot repair (`merge_base` is None) and must refuse
    rather than guess at."""
    _git(work, "checkout", "-q", "-b", name)
    (work / "pr_marker.py").write_text("# original PR work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "PR work")
    _git(work, "push", "-q", "-u", "origin", name)
    pushed_tip = _git(work, "rev-parse", name)
    _git(work, "checkout", "-q", "--orphan", "tmp-orphan")
    _git(work, "rm", "-r", "-q", "-f", ".")
    (work / "rework.py").write_text("# unrelated rework\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "unrelated rework")
    _git(work, "branch", "-q", "-f", name, "tmp-orphan")
    _git(work, "checkout", "-q", name)
    _git(work, "branch", "-q", "-D", "tmp-orphan")
    return pushed_tip


# ---------------------------------------------------------------------------
# Pure primitives (`vcs.reconverge`), no orchestrator involved.
# ---------------------------------------------------------------------------

def test_is_rework_after_rejection_requires_pr_branch_match_and_feedback():
    assert is_rework_after_rejection(
        {"pr_branch": "b", "send_back_feedback": [{"m": 1}]}, "b")
    assert is_rework_after_rejection(
        {"pr_branch": "b", "pending_send_back": {"source": "reject"}}, "b")
    assert not is_rework_after_rejection(
        {"pr_branch": "other", "send_back_feedback": [{"m": 1}]}, "b")
    assert not is_rework_after_rejection(
        {"pr_branch": "b", "send_back_feedback": []}, "b")
    assert not is_rework_after_rejection({"pr_branch": "b"}, "b")
    assert not is_rework_after_rejection(None, "b")


def test_already_reconverged_reads_the_reconverge_list():
    assert already_reconverged({"reconverge": [{"branch": "b"}]}, "b")
    assert not already_reconverged({"reconverge": [{"branch": "other"}]}, "b")
    assert not already_reconverged({}, "b")
    assert not already_reconverged(None, "b")


def test_reconverge_replays_local_only_commits_onto_the_pushed_tip(repo, origin):
    branch = "rework-branch"
    pushed_tip = _make_reworked_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    assert not gitrepo.is_ancestor(pushed_tip, local_tip)
    assert not gitrepo.is_ancestor(local_tip, pushed_tip)

    result = reconverge(
        gitrepo, branch=branch, local_sha=local_tip, pushed_sha=pushed_tip)

    assert result.branch == branch
    assert result.from_sha == local_tip
    assert result.pushed_sha == pushed_tip
    new_head = gitrepo.branch_sha(branch)
    assert result.to_sha == new_head
    assert gitrepo.is_ancestor(pushed_tip, new_head), (
        "the pushed tip must be an ancestor of the reconverged head")
    assert result.replayed == 1


def test_reconverge_refuses_unrelated_histories_and_leaves_the_branch_untouched(
    repo, origin,
):
    branch = "rework-branch"
    pushed_tip = _make_unrelated_rework_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    with pytest.raises(GitError, match="share no history"):
        reconverge(
            gitrepo, branch=branch, local_sha=local_tip, pushed_sha=pushed_tip)

    assert gitrepo.branch_sha(branch) == local_tip, (
        "a refused reconvergence must not move the branch")


def test_reconverge_refuses_a_stale_remote_snapshot(repo, origin):
    branch = "rework-branch"
    _make_reworked_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    with pytest.raises(GitError, match="stale snapshot"):
        reconverge(
            gitrepo, branch=branch, local_sha=local_tip, pushed_sha="0" * 40)

    assert gitrepo.branch_sha(branch) == local_tip


def test_divergence_summary_reports_the_heavier_side(repo, origin):
    branch = "rework-branch"
    pushed_tip = _make_locally_heavier_diverged_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    summary = divergence_summary(gitrepo, local_tip, pushed_tip)

    assert summary.merge_base is not None
    assert summary.local_only == 2
    assert summary.pushed_only == 1
    assert summary.heavier == "local"


# ---------------------------------------------------------------------------
# AC 1: a rework built after a rejection is reconverged onto its own pushed
# tip, in place, so delivery can fast-forward — same branch, same PR.
# ---------------------------------------------------------------------------

async def test_rework_after_rejection_is_reconverged_onto_pushed_tip(
    store, tmp_path, repo, origin,
):
    task = Task.new("Rework test", repo_path=str(repo))
    stem = branch_stem({"git": {}}, task.id)
    branch = f"{stem}-2"

    pushed_tip = _make_reworked_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    assert not gitrepo.is_ancestor(pushed_tip, local_tip)
    assert not gitrepo.is_ancestor(local_tip, pushed_tip)

    task.context = {
        "pr_branch": branch,
        "send_back_feedback": [
            {"at": "2026-01-01T00:00:00+00:00", "message": "please fix X"}],
    }
    await store.create_task(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    events: list[dict] = []
    orch = _orch(store, tmp_path, events=events)

    result_branch = await orch._recover_diverged_branch(task, gitrepo, branch)

    assert result_branch == branch, (
        "reconvergence must keep the SAME branch/PR, unlike a recut")
    new_head = gitrepo.branch_sha(branch)
    assert gitrepo.is_ancestor(pushed_tip, new_head), (
        "the pushed tip must now be an ancestor of the branch head")
    assert _git(origin, "rev-parse", f"refs/heads/{branch}") == pushed_tip, (
        "this hook only rebases locally — the actual delivery push happens "
        "later in the normal flow, not inside recovery")

    reconv_events = [e for e in events if e.get("kind") == "branch_reconverged"]
    assert reconv_events, f"no branch_reconverged event emitted: {events}"
    text = reconv_events[0]["text"]
    assert local_tip in text, text
    assert pushed_tip in text, text

    recorded = task.context.get("reconverge")
    assert recorded and recorded[-1]["branch"] == branch, recorded
    assert recorded[-1]["pushed_sha"] == pushed_tip, recorded
    assert recorded[-1]["to_sha"] == new_head, recorded


# ---------------------------------------------------------------------------
# AC 2: reconvergence is one-shot per branch — a second divergence after an
# already-reconverged branch falls back to the pre-existing recut recovery
# rather than reconverging (or looping) a second time.
# ---------------------------------------------------------------------------

async def test_reconverge_is_one_shot_then_falls_back_to_recut(
    store, tmp_path, repo, origin,
):
    task = Task.new("Rework test", repo_path=str(repo))
    stem = branch_stem({"git": {}}, task.id)
    branch = f"{stem}-2"

    pushed_tip = _make_reworked_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    task.context = {
        "pr_branch": branch,
        "send_back_feedback": [
            {"at": "2026-01-01T00:00:00+00:00", "message": "please fix X"}],
        "reconverge": [{
            "branch": branch, "from_sha": "deadbeef" * 5,
            "pushed_sha": "cafebabe" * 5, "to_sha": "deadbeef" * 5,
            "replayed": 0, "at": "2026-01-01T00:00:00+00:00",
        }],
    }
    await store.create_task(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    events: list[dict] = []
    orch = _orch(store, tmp_path, events=events)

    new_branch = await orch._recover_diverged_branch(task, gitrepo, branch)

    assert new_branch == f"{stem}-3", new_branch
    assert not [e for e in events if e.get("kind") == "branch_reconverged"], (
        "a branch already reconverged once must not be reconverged again")
    assert [e for e in events if e.get("kind") == "branch_recut"], (
        "must fall back to recut once reconvergence is already spent")

    assert len(task.context["reconverge"]) == 1, (
        "no new reconverge entry may be recorded", task.context)
    assert task.context["pr_branch"] == new_branch
    assert _git(origin, "rev-parse", f"refs/heads/{new_branch}") == local_tip
    assert pushed_tip  # sanity: fixture actually diverged the branch


# ---------------------------------------------------------------------------
# AC 3: the reconvergence recovery never forces or rewrites published
# history — every direct `subprocess.run` and `GitRepo._run` call is
# recorded and checked for forbidden flags/refspecs.
# ---------------------------------------------------------------------------

async def test_reconverge_never_forces(store, tmp_path, repo, origin, monkeypatch):
    task = Task.new("Rework test", repo_path=str(repo))
    stem = branch_stem({"git": {}}, task.id)
    branch = f"{stem}-2"

    _make_reworked_branch(repo, branch)
    gitrepo = GitRepo(repo)

    task.context = {
        "pr_branch": branch,
        "send_back_feedback": [
            {"at": "2026-01-01T00:00:00+00:00", "message": "please fix X"}],
    }
    await store.create_task(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    recorded_subprocess: list[list[str]] = []
    real_subprocess_run = subprocess.run

    def _spy_subprocess_run(argv, *a, **k):
        if isinstance(argv, list) and argv and argv[0] == "git":
            recorded_subprocess.append(list(argv))
        return real_subprocess_run(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", _spy_subprocess_run)

    recorded_grun: list[tuple] = []
    real_grun = GitRepo._run

    def _spy_grun(self, *args, **kw):
        recorded_grun.append(args)
        return real_grun(self, *args, **kw)

    monkeypatch.setattr(GitRepo, "_run", _spy_grun)

    events: list[dict] = []
    orch = _orch(store, tmp_path, events=events)
    new_branch = await orch._recover_diverged_branch(task, gitrepo, branch)
    assert new_branch == branch  # reconverged in place, not recut

    forbidden = ("--force", "-f", "--force-with-lease", "--delete")
    for argv in (*recorded_subprocess, *recorded_grun):
        for bad in forbidden:
            assert bad not in argv, f"forbidden flag {bad!r} in git call: {argv}"
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("+"):
                raise AssertionError(f"forced refspec in git call: {argv}")

    assert recorded_grun or recorded_subprocess, "no git calls were recorded at all"


# ---------------------------------------------------------------------------
# AC 4: a divergence that genuinely cannot be reconverged (unrelated
# histories) still escalates via the pre-existing recut safety net, but now
# with a structured blocker naming both SHAs and a concrete merge option —
# never bare prose, never an empty options list.
# ---------------------------------------------------------------------------

async def test_diverged_branch_that_cannot_be_reconverged_escalates_with_actionable_blocker(
    store, tmp_path, repo, origin,
):
    task = Task.new("Rework test", repo_path=str(repo))
    stem = branch_stem({"git": {}}, task.id)
    branch = f"{stem}-2"

    pushed_tip = _make_unrelated_rework_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    task.context = {
        "pr_branch": branch,
        "send_back_feedback": [
            {"at": "2026-01-01T00:00:00+00:00", "message": "please fix X"}],
        "recut": [{
            "from_branch": branch, "from_sha": "deadbeef" * 5,
            "remote_sha": "cafebabe" * 5, "to_branch": f"{stem}-3",
            "at": "2026-01-01T00:00:00+00:00",
        }],
    }
    await store.create_task(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    orch = _orch(store, tmp_path)

    with pytest.raises(ReviewedShaMismatch) as excinfo:
        await orch._recover_diverged_branch(task, gitrepo, branch)

    exc = excinfo.value
    text = str(exc)
    assert local_tip in text, text
    assert pushed_tip in text, text

    blocker = exc.blocker
    assert blocker is not None, (
        "an unavoidable divergence must carry a structured blocker, not "
        "just an exception string")
    assert local_tip in blocker.root_cause_hypothesis, blocker.root_cause_hypothesis
    assert pushed_tip in blocker.root_cause_hypothesis, blocker.root_cause_hypothesis
    assert blocker.options, "must offer a concrete option, not an empty list"
    assert any("merge" in o.label.lower() for o in blocker.options), blocker.options
    for o in blocker.options:
        assert o.action is None, (
            "a constructor-built blocker option must never carry an action")

    assert not (task.context.get("reconverge") or []), (
        "a failed reconvergence attempt must not be recorded as one")
    assert gitrepo.branch_sha(branch) == local_tip, (
        "a refused recovery must not have moved the branch")


# ---------------------------------------------------------------------------
# AC 4 (continued): the already-recut-twice escalation path (pre-existing,
# unrelated to rework-after-rejection detection) also names the heavier
# side now, not just both SHAs.
# ---------------------------------------------------------------------------

async def test_second_divergence_after_already_recut_names_the_heavier_side(
    store, tmp_path, repo, origin,
):
    task = Task.new("Recut test", repo_path=str(repo))
    stem = branch_stem({"git": {}}, task.id)
    branch = f"{stem}-2"

    pushed_tip = _make_locally_heavier_diverged_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    task.context = {
        "pr_branch": branch,
        "recut": [{
            "from_branch": branch, "from_sha": "deadbeef" * 5,
            "remote_sha": "cafebabe" * 5, "to_branch": f"{stem}-3",
            "at": "2026-01-01T00:00:00+00:00",
        }],
    }
    await store.create_task(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    orch = _orch(store, tmp_path)

    with pytest.raises(ReviewedShaMismatch) as excinfo:
        await orch._recover_diverged_branch(task, gitrepo, branch)

    blocker = excinfo.value.blocker
    assert blocker is not None
    assert local_tip in blocker.root_cause_hypothesis
    assert pushed_tip in blocker.root_cause_hypothesis
    assert "more work" in blocker.root_cause_hypothesis
    assert "rework" in blocker.root_cause_hypothesis.lower()
