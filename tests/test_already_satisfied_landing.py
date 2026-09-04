"""approve on an ALREADY-SATISFIED verdict must land the branch when the
satisfying commit is only on the task branch (not reachable from
origin/base) — never silently mark the task done without landing it.

Live incident this closes: a resumed coder's ALREADY-SATISFIED report was
approved and the task marked DONE, but the satisfying commit lived only on
the task branch — origin/main never got the change, and a human had to
manually squash-land it later. `land_already_satisfied_claim`
(`vcs/task_pr.py`) is the single decision point both `nh approve`
(`cli/commands.py::_land_one`) and the API's `POST /approve`
(`api/app.py::approve_task`) now call — this file pins its contract at three
layers: the pure classifier (`classify_already_satisfied_landing`), the
async helper itself, and the end-to-end `nh approve` CLI path.

Required distinction (AC3), asserted verbatim throughout this file:
  * "satisfying commit reachable from base (nothing to land)"   -> AC2
  * "satisfying commit on task branch only (landing required)"  -> AC1
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs import GitRepo
from no_human.vcs.approve_merge import LandResult
from no_human.vcs.task_pr import (
    LANDING_REQUIRED, NOTHING_TO_LAND, UNVERIFIABLE,
    classify_already_satisfied_landing, land_already_satisfied_claim,
)

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Fixtures — a real bare-remote + working-clone repo, reused from            #
# test_already_satisfied_subject_tree.py's idiom.                            #
# --------------------------------------------------------------------------- #

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    """A real bare remote (`tmp_path/remote.git`) plus a working clone
    (`tmp_path/work`, returned) with `origin` configured and `main` pushed —
    the satisfying-commit branch and its landing are exercised against real
    git, not a fake."""
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.test")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _lonely_repo(tmp_path):
    """A repo with NO `origin` remote and no branch named `main` — every
    base-ref candidate `classify_already_satisfied_landing` tries fails to
    resolve, forcing the fail-closed UNVERIFIABLE path."""
    repo = tmp_path / "lonely"
    repo.mkdir()
    _git(repo, "init", "-b", "trunk")
    _git(repo, "config", "user.email", "u@example.test")
    _git(repo, "config", "user.name", "u")
    (repo / "a.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
async def store(tmp_path):
    result = await Store(tmp_path / "nh.db").connect()
    yield result
    await result.close()


def _commit_on_new_branch(work, branch, filename, content, message):
    """Commit `content` to `filename` on a NEW branch off the current HEAD,
    then return to `main` — the branch is never merged into `main`, so its
    tip is the satisfying commit "only on the task branch"."""
    _git(work, "checkout", "-b", branch)
    (work / filename).write_text(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", message)
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "checkout", "main")
    return sha


# --------------------------------------------------------------------------- #
# classify_already_satisfied_landing — the pure classifier                   #
# --------------------------------------------------------------------------- #

def test_classify_ancestor_sha_is_nothing_to_land(bare_repo):
    sha = _git(bare_repo, "rev-parse", "main").stdout.strip()
    repo = GitRepo(bare_repo)
    verdict = classify_already_satisfied_landing(repo, sha=sha, branch="", base="main")
    assert verdict.verdict == NOTHING_TO_LAND
    assert verdict.reason == "satisfying commit reachable from base (nothing to land)"


def test_classify_branch_only_sha_is_landing_required(bare_repo):
    sha = _commit_on_new_branch(
        bare_repo, "feature-classify", "calc.py",
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
        "add mul()")
    repo = GitRepo(bare_repo)
    verdict = classify_already_satisfied_landing(
        repo, sha=sha, branch="feature-classify", base="main")
    assert verdict.verdict == LANDING_REQUIRED
    assert "satisfying commit on task branch only (landing required)" in verdict.reason


def test_classify_empty_sha_is_unverifiable(bare_repo):
    repo = GitRepo(bare_repo)
    verdict = classify_already_satisfied_landing(repo, sha="", branch="", base="main")
    assert verdict.verdict == UNVERIFIABLE


def test_classify_unresolvable_base_is_unverifiable(tmp_path):
    lonely = _lonely_repo(tmp_path)
    sha = _git(lonely, "rev-parse", "HEAD").stdout.strip()
    repo = GitRepo(lonely)
    verdict = classify_already_satisfied_landing(repo, sha=sha, branch="", base="main")
    assert verdict.verdict == UNVERIFIABLE
    assert "could not resolve a base ref" in verdict.reason


# --------------------------------------------------------------------------- #
# land_already_satisfied_claim — the async helper, called directly           #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_state_and_blocker_text_name_which_case_applied(bare_repo, store):
    """AC3, dedicated: the SAME helper both `nh approve` and the API call
    produces the two distinguishing phrasings, one per case, on the same
    repo — proving the text is genuinely conditioned on which case applied,
    not just present somewhere by coincidence."""
    base_sha = _git(bare_repo, "rev-parse", "main").stdout.strip()
    branch_sha = _commit_on_new_branch(
        bare_repo, "feature-ac3", "calc.py",
        "def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n",
        "add div()")

    task_on_base = Task.new("on base", repo_path=str(bare_repo))
    task_on_base.context = {
        "already_satisfied_report": "x",
        "already_satisfied_landing": {"sha": base_sha, "branch": "", "on_base": False},
        "base_branch": "main",
    }
    await store.create_task(task_on_base)
    await store.set_status(task_on_base, TaskStatus.AWAITING_APPROVAL, validate=False)

    task_branch_only = Task.new("branch only", repo_path=str(bare_repo))
    task_branch_only.context = {
        "already_satisfied_report": "x",
        "already_satisfied_landing": {"sha": branch_sha, "branch": "feature-ac3", "on_base": False},
        "base_branch": "main",
    }
    await store.create_task(task_branch_only)
    await store.set_status(task_branch_only, TaskStatus.AWAITING_APPROVAL, validate=False)

    step_on_base = await land_already_satisfied_claim(
        store, task_on_base, repo_path=str(bare_repo))
    step_branch_only = await land_already_satisfied_claim(
        store, task_branch_only, repo_path=str(bare_repo))

    assert step_on_base["decision"] == "done"
    assert step_on_base["reason"] == "satisfying commit reachable from base (nothing to land)"

    assert step_branch_only["decision"] == "land"
    assert "satisfying commit on task branch only (landing required)" in step_branch_only["reason"]

    # The two phrasings are genuinely distinct — not the same string reused.
    assert step_on_base["reason"] != step_branch_only["reason"]


@pytest.mark.asyncio
async def test_unverifiable_base_refuses_to_complete(tmp_path, store):
    """Fail-closed: an unresolvable base must never be treated as "nothing
    to land" — it refuses, and the task stays awaiting_approval."""
    lonely = _lonely_repo(tmp_path)
    sha = _git(lonely, "rev-parse", "HEAD").stdout.strip()

    task = Task.new("bogus base", repo_path=str(lonely))
    task.context = {
        "already_satisfied_report": "x",
        "already_satisfied_landing": {"sha": sha, "branch": "", "on_base": False},
        "base_branch": "main",
    }
    await store.create_task(task)
    await store.set_status(task, TaskStatus.AWAITING_APPROVAL, validate=False)

    step = await land_already_satisfied_claim(store, task, repo_path=str(lonely))
    assert step["decision"] == "refuse"
    assert "could not resolve a base ref" in step["reason"]

    refreshed = await store.get_task(task.id)
    assert refreshed.status == TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# nh approve (CLI end-to-end) — AC1 and AC2                                  #
# --------------------------------------------------------------------------- #

def _seed_task_repo(db_path: Path, status: TaskStatus, repo_path: str, *,
                    title="Test task", task_id: str | None = None,
                    context: dict | None = None) -> str:
    """Like test_cli_commands.py's `_seed_task`, but accepts a REAL
    `repo_path` (a bare_repo's `work` clone) instead of the hardcoded
    `/tmp/repo` — required so `land_already_satisfied_claim`'s fresh git
    re-derivation has an actual repo to fetch/classify against."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path=repo_path)
            if task_id is not None:
                t.id = task_id
            if context:
                t.context = context
            await s.create_task(t)
            await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def _get_task(db_path: Path, task_id: str) -> Task:
    async def _go():
        async with Store(db_path) as s:
            return await s.get_task(task_id)
    return asyncio.run(_go())


def _make_runner(path, monkeypatch):
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        data: dict = {}
        db_path = path

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(
        cmd_mod, "_probe_pool",
        lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED))
    return CliRunner()


def test_task_branch_only_claim_is_landed_by_approve(tmp_path, monkeypatch, bare_repo):
    """AC1: the satisfying commit lives only on the task branch (never
    reachable from origin/main) -> `nh approve` must (a) land it via the
    PR/squash path, (b) make it appear in origin/base, (c) mark the task
    done only after landing completes, and (d) name the case in its
    console output (AC3)."""
    work = bare_repo
    branch = "feature-ac1"
    sha = _commit_on_new_branch(
        work, branch, "calc.py",
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        "add sub()")

    db = tmp_path / "nh.db"
    task_id = _seed_task_repo(db, TaskStatus.AWAITING_APPROVAL, str(work), context={
        "already_satisfied_report":
            "ALREADY-SATISFIED\nCRITERION: sub — MET — evidence: calc.py:5",
        "already_satisfied_landing": {
            "sha": sha, "branch": branch, "ship_ref": "", "on_base": False},
        "base_branch": "main",
        # Precondition for the PR-merge fallthrough's `_review_pass_evidence`
        # gate: a review round stamped on (or covering) the branch head.
        "review_history": [{"sha": sha, "passed": True}],
    })

    def _fake_land_task(*, repo_path, branch, pr_url, task_id, task_title,
                        review_evidence, config, tested_commit_sha):
        # Simulate the squash-merge: fast-forward origin/main to the
        # satisfying commit — this is the ONE assertion that matters for
        # AC1(b): the commit must actually reach origin/base.
        subprocess.run(
            ["git", "-C", repo_path, "push", "origin", f"{branch}:main"],
            check=True, capture_output=True, text=True)
        landed = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", branch],
            check=True, capture_output=True, text=True).stdout.strip()
        return LandResult(ok=True, step="close_pr", landed_sha=landed,
                          pr_url=pr_url, branch=branch, message="landed onto main")

    import no_human.vcs.approve_merge as approve_merge_mod
    monkeypatch.setattr(approve_merge_mod, "land_task", _fake_land_task)

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id])
    assert result.exit_code == 0, result.output

    # AC3: the console output names which case applied.
    assert "satisfying commit on task branch only (landing required)" in result.output

    # AC1(b): the satisfying commit now appears in origin/base.
    remote = tmp_path / "remote.git"
    main_log = _git(remote, "log", "main", "--format=%H").stdout.split()
    assert sha in main_log

    # AC1(c): task marked done only after landing completed.
    task = _get_task(db, task_id)
    assert task.status is TaskStatus.DONE

    events = asyncio.run(_list_events(db, task_id))
    merged = [e for e in events if e.get("kind") == "human_merged"]
    assert merged, events
    assert merged[0].get("sha", "").startswith(sha[:12])


def test_task_is_not_done_when_landing_fails(tmp_path, monkeypatch, bare_repo):
    """AC1 corollary: if the landing step itself fails, the task must NEVER
    be marked done — it stays awaiting_approval so a human can retry."""
    work = bare_repo
    branch = "feature-ac1-fail"
    sha = _commit_on_new_branch(
        work, branch, "calc.py",
        "def add(a, b):\n    return a + b\n\n\ndef pow2(a):\n    return a * a\n",
        "add pow2()")

    db = tmp_path / "nh.db"
    task_id = _seed_task_repo(db, TaskStatus.AWAITING_APPROVAL, str(work), context={
        "already_satisfied_report":
            "ALREADY-SATISFIED\nCRITERION: pow2 — MET — evidence: calc.py:5",
        "already_satisfied_landing": {
            "sha": sha, "branch": branch, "ship_ref": "", "on_base": False},
        "base_branch": "main",
        "review_history": [{"sha": sha, "passed": True}],
    })

    def _failing_land_task(*, repo_path, branch, pr_url, task_id, task_title,
                           review_evidence, config, tested_commit_sha):
        return LandResult(ok=False, step="tests", stderr="tests failed on the squash tree")

    import no_human.vcs.approve_merge as approve_merge_mod
    monkeypatch.setattr(approve_merge_mod, "land_task", _failing_land_task)

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id])

    # A landing failure is a hard failure: non-zero exit, never marked done.
    assert result.exit_code == 1, result.output
    assert "merge FAILED" in result.output

    remote = tmp_path / "remote.git"
    main_log = _git(remote, "log", "main", "--format=%H").stdout.split()
    assert sha not in main_log

    task = _get_task(db, task_id)
    assert task.status is TaskStatus.AWAITING_APPROVAL


async def _list_events(db_path, task_id):
    async with Store(db_path) as s:
        return await s.list_events(task_id)


def test_commit_already_on_base_completes_with_nothing_landed(tmp_path, monkeypatch, bare_repo):
    """AC2: the satisfying commit is already reachable from origin/main ->
    `nh approve` must do (a) zero landing (no push/PR/land_task call), (b)
    mark the task done immediately, and (c) name the case in its console
    output (AC3) — derived FRESH from git, not from a pre-seeded
    `on_base: True` shortcut."""
    work = bare_repo
    sha = _git(work, "rev-parse", "main").stdout.strip()

    db = tmp_path / "nh.db"
    task_id = _seed_task_repo(db, TaskStatus.AWAITING_APPROVAL, str(work), context={
        "already_satisfied_report":
            "ALREADY-SATISFIED\nCRITERION: add — MET — evidence: calc.py:1",
        "already_satisfied_landing": {
            "sha": sha, "branch": "", "ship_ref": "", "on_base": False},
        "base_branch": "main",
    })

    def _unexpected_land_task(**kwargs):
        raise AssertionError("land_task must never be called — nothing to land")

    import no_human.vcs.approve_merge as approve_merge_mod
    monkeypatch.setattr(approve_merge_mod, "land_task", _unexpected_land_task)

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id])
    assert result.exit_code == 0, result.output

    # AC3: the console output names which case applied.
    assert "satisfying commit reachable from base (nothing to land)" in result.output

    # AC2(b): done immediately.
    task = _get_task(db, task_id)
    assert task.status is TaskStatus.DONE

    # The classifier's own re-derivation backfills on_base for next time.
    assert task.context["already_satisfied_landing"]["on_base"] is True

    events = asyncio.run(_list_events(db, task_id))
    assert any(e.get("kind") == "approved_already_satisfied" for e in events)
    assert not any(e.get("kind") == "human_merged" for e in events)
