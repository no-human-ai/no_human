"""A retry that reuses its branch — `ctx['pr_branch']` (a revision) or a
checkpoint (`resume_from`/`handoff.wip_sha`) — meets whatever base it was cut
from, forever, unless something measures the gap. Two real incidents:

  db9da7f7 — 5 commits behind main. Attempt 2 burned 6.9M tokens re-fixing a
             test main had already deleted, and was FAILED BY THE TAMPER GUARD
             because the agent "fixed" the now-absent-on-main red test by
             adding `pytest.skip(...)`.
  e6719bd8 — 140 commits behind, 6 attempts, 37.9M tokens, ended at 99% of cap.

`Orchestrator._refresh_stale_base` (called once, after the branch decision —
covering BOTH the `pr_branch` reuse path and the checkpoint/`else` path)
measures `commits_behind(base)` and, at or past
`BASE_STALENESS_REBASE_THRESHOLD` (5 — the `db9da7f7` incident itself; 10 was
tried and rejected because it excludes that incident), rebases the branch onto
the current base. The result is persisted on `task.context["base_staleness"]`
and narrated to the coder in `_build_implement_prompt`'s preamble.

A prior attempt on this exact ticket failed review because the rebased-branch
preamble reported the POST-rebase staleness (0 — there is nothing left behind
once the rebase replayed every commit) instead of the count that justified
acting. `_refresh_stale_base` now stores both: `commits_behind` (current — 0
once rebased) and `was_behind` (the pre-rebase measurement); the preamble
reads `was_behind` for the rebased case. `test_the_staleness_reaches_the_coders_preamble`
is the direct regression test for that finding.
"""
from __future__ import annotations

import subprocess
import types

import pytest
import pytest_asyncio

from no_human.config import load_config
from no_human.core.orchestrator import (
    BASE_STALENESS_REBASE_THRESHOLD,
    Orchestrator,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class _Stop(Exception):
    """Ends the attempt immediately after `_refresh_stale_base` has run. The
    branch decision and the staleness measurement/rebase that follows it are
    under test; the coder session, review and tests below are 40s of real
    subprocess work and none of it is under test here (same pattern as
    `tests/test_resume_checkpoint_lost.py`)."""


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    return work


def _advance_main(work, n):
    """Commit N times directly on `main` — the fix(es) a stale branch missed."""
    for i in range(n):
        (work / f"main-advance-{i}.py").write_text(f"m = {i}\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", f"main advances {i}")


def _make_stale_checkpoint(work, n):
    """A [WIP-BLOCKED]-shaped checkpoint commit off `main`, then `main`
    advances `n` times without it — the exact shape `db9da7f7` and
    `e6719bd8` were in: a checkpoint whose branch, once cut, trails a moving
    base by `n` commits it never saw."""
    _git(work, "checkout", "-q", "-b", "wip")
    (work / "wip_marker.py").write_text("# tens of turns of work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-BLOCKED] work")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")
    _git(work, "branch", "-D", "wip")
    _advance_main(work, n)
    return sha


def _make_stale_pr_branch(work, name, n):
    """An existing branch (the shape a revision's `ctx['pr_branch']` reuses),
    `n` commits behind `main` by the time the retry looks at it."""
    _git(work, "checkout", "-q", "-b", name)
    (work / "pr_marker.py").write_text("# a PR's committed work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "PR work")
    _git(work, "checkout", "-q", "main")
    _advance_main(work, n)
    _git(work, "checkout", "-q", name)


async def _attempt(repo, tmp_path, store, monkeypatch, ctx, *, attempt_n=1):
    """Drive the REAL `_run_attempt` through the branch decision and
    `_refresh_stale_base`, then stop before the coder session."""
    cfg = load_config(tmp_path / "config.yaml")
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, types.SimpleNamespace(),
                        SlackNotifier(None),
                        event_sink=events.append)
    monkeypatch.setattr(
        Orchestrator, "_build_implement_prompt",
        lambda self, *a, **k: (_ for _ in ()).throw(_Stop()))

    t = Task.new("stale base retry", repo_path=str(repo))
    t.context = ctx
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    with pytest.raises(_Stop):
        await orch._run_attempt(t, GitRepo(repo), attempt_n, "main")
    return t, events, (await store.list_attempts(t.id))[-1]


def _staleness_events(events):
    return [e for e in events if e.get("kind") == "base_staleness"]


# --------------------------------------------------------------------------- #
# AC1 + AC3 + AC4: measured, reported, rebased — checkpoint/`else` path
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_stale_checkpoint_retry_is_measured_reported_and_rebased(
    repo, tmp_path, store, monkeypatch,
):
    checkpoint = _make_stale_checkpoint(repo, BASE_STALENESS_REBASE_THRESHOLD)
    ctx = {"resume_from": {"sha": checkpoint, "by": "human"}}

    t, events, attempt = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1, [e.get("kind") for e in events]
    ev = evs[0]
    assert ev["commits_behind"] == BASE_STALENESS_REBASE_THRESHOLD
    assert str(BASE_STALENESS_REBASE_THRESHOLD) in ev["text"]
    assert ev["rebased"] is True

    branch = attempt["branch_name"]
    assert branch
    # The rebase replayed the checkpoint's own work AND picked up every commit
    # main advanced past it — this is the whole point: the fix that landed on
    # main since the branch was cut is now in the tree.
    _git(repo, "checkout", "-q", branch)
    assert (repo / "wip_marker.py").exists()
    for i in range(BASE_STALENESS_REBASE_THRESHOLD):
        assert (repo / f"main-advance-{i}.py").exists()

    staleness = t.context["base_staleness"]
    assert staleness["rebased"] is True
    assert staleness["was_behind"] == BASE_STALENESS_REBASE_THRESHOLD
    assert staleness["commits_behind"] == 0, (
        "current staleness must read 0 once the rebase replayed everything "
        "onto base — the whole reason `was_behind` exists separately"
    )


# --------------------------------------------------------------------------- #
# AC4's other half: the `pr_branch` reuse path (a revision) is covered too
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_stale_pr_branch_retry_is_rebased_too(
    repo, tmp_path, store, monkeypatch,
):
    _make_stale_pr_branch(repo, "no-human/t1", BASE_STALENESS_REBASE_THRESHOLD)
    ctx = {"pr_branch": "no-human/t1"}

    t, events, attempt = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1, [e.get("kind") for e in events]
    ev = evs[0]
    assert ev["commits_behind"] == BASE_STALENESS_REBASE_THRESHOLD
    assert ev["rebased"] is True
    assert ev["branch"] == "no-human/t1"
    assert attempt["branch_name"] == "no-human/t1"

    _git(repo, "checkout", "-q", "no-human/t1")
    assert (repo / "pr_marker.py").exists()
    for i in range(BASE_STALENESS_REBASE_THRESHOLD):
        assert (repo / f"main-advance-{i}.py").exists()

    staleness = t.context["base_staleness"]
    assert staleness["rebased"] is True
    assert staleness["was_behind"] == BASE_STALENESS_REBASE_THRESHOLD
    assert staleness["commits_behind"] == 0


# --------------------------------------------------------------------------- #
# AC7 negative control #1: a branch already at base is not rebased and does
# no extra git work
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_branch_at_base_is_not_rebased_and_does_no_extra_git_work(
    repo, tmp_path, store, monkeypatch,
):
    def _boom(self, base):
        raise AssertionError("rebase_onto must not be called below threshold")
    monkeypatch.setattr(GitRepo, "rebase_onto", _boom)

    ctx = {}  # fresh attempt, no checkpoint, no pr_branch — branches from base

    t, events, attempt = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1
    assert evs[0]["commits_behind"] == 0
    assert evs[0]["rebased"] is False
    assert t.context["base_staleness"] == {
        "commits_behind": 0, "was_behind": 0, "rebased": False,
    }


# --------------------------------------------------------------------------- #
# Threshold boundary: the two rows that pin ">= 5" and would both flip if
# anyone re-raised the threshold to 10, silently excluding the db9da7f7
# incident again.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_below_threshold_staleness_is_reported_but_not_rebased(
    repo, tmp_path, store, monkeypatch,
):
    # Literal 4, not `BASE_STALENESS_REBASE_THRESHOLD - 1`: pinning the
    # constant itself makes this test self-referential — it would stay green
    # even if the threshold silently moved to 10 (constructing a 9-behind
    # branch and asserting *that* isn't rebased proves nothing about whether
    # 5 still is). The `== 5` assertion below is the actual regression catch.
    assert BASE_STALENESS_REBASE_THRESHOLD == 5
    _make_stale_pr_branch(repo, "no-human/t1", 4)
    ctx = {"pr_branch": "no-human/t1"}

    t, events, _att = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1
    assert evs[0]["commits_behind"] == 4
    assert evs[0]["rebased"] is False
    staleness = t.context["base_staleness"]
    assert staleness["commits_behind"] == 4
    assert staleness["rebased"] is False


@pytest.mark.asyncio
async def test_the_five_commit_incident_is_rebased(
    repo, tmp_path, store, monkeypatch,
):
    """db9da7f7 itself: exactly 5 commits behind. A threshold of 10 (a
    previously rejected design) would wrongly leave this one un-rebased —
    pinned with a literal 5, not the constant, so a regression to 10 flips
    this test red instead of silently re-parametrizing itself."""
    assert BASE_STALENESS_REBASE_THRESHOLD == 5
    _make_stale_pr_branch(repo, "no-human/t1", 5)
    ctx = {"pr_branch": "no-human/t1"}

    t, events, _att = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert evs[0]["rebased"] is True
    assert t.context["base_staleness"]["rebased"] is True


# --------------------------------------------------------------------------- #
# AC6, orchestrator half: a CONFLICTING rebase must not fail the attempt
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_rebase_conflict_does_not_fail_the_attempt(
    repo, tmp_path, store, monkeypatch,
):
    n = BASE_STALENESS_REBASE_THRESHOLD
    _git(repo, "checkout", "-q", "-b", "no-human/t1")
    (repo / "shared.py").write_text("branch = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "branch edits shared.py")
    _git(repo, "checkout", "-q", "main")
    (repo / "shared.py").write_text("main = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main edits shared.py too")
    _advance_main(repo, n - 1)
    _git(repo, "checkout", "-q", "no-human/t1")

    ctx = {"pr_branch": "no-human/t1"}
    t, events, attempt = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    # It proceeded — no escalation, no failure, `_Stop` was still reached.
    assert attempt["branch_name"] == "no-human/t1"
    assert attempt["failure_reason"] is None

    evs = _staleness_events(events)
    assert len(evs) == 1
    assert evs[0]["commits_behind"] == n
    assert evs[0]["rebased"] is False
    assert "conflict" in evs[0]["text"]

    staleness = t.context["base_staleness"]
    assert staleness["rebased"] is False
    assert staleness["was_behind"] == n
    assert staleness["commits_behind"] == n, (
        "the tree was never rebased, so current staleness IS the original "
        "measurement — not 0"
    )

    # And the tree is genuinely usable afterward: no rebase left in progress.
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == "", f"a conflict must not leave a rebase in progress: {status!r}"


# --------------------------------------------------------------------------- #
# AC2: the staleness reaches the coder's preamble. Direct regression test for
# the finding that failed a prior attempt's review: the rebased case must
# report the PRE-rebase count (`was_behind`), not the post-rebase 0.
# --------------------------------------------------------------------------- #

def _orch():
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    return orch


def _task(**kw):
    defaults = dict(
        id="aaa", source="test", title="Fix bug",
        status=TaskStatus.IMPLEMENTING,
        acceptance_criteria=["Bug is fixed"],
    )
    defaults.update(kw)
    return Task(**defaults)


def test_the_staleness_reaches_the_coders_preamble():
    # Rebased: must report WAS_BEHIND (5), never the post-rebase 0.
    t = _task()
    t.context = {"base_staleness": {
        "commits_behind": 0, "was_behind": 5, "rebased": True,
    }}
    prompt = _orch()._build_implement_prompt(t, "/tmp/repo")
    assert "REBASED" in prompt
    assert "5" in prompt
    assert "0 commit(s) behind" not in prompt, (
        "the exact regression a prior attempt's review caught: the rebased "
        "case must not report the post-rebase 0"
    )

    # Conflicted (still stale, not rebased): must report the real count.
    t2 = _task(id="bbb")
    t2.context = {"base_staleness": {
        "commits_behind": 5, "was_behind": 5, "rebased": False,
    }}
    prompt2 = _orch()._build_implement_prompt(t2, "/tmp/repo")
    assert "5 COMMIT(S) BEHIND" in prompt2
    assert "conflict" in prompt2.lower() or "did not complete" in prompt2.lower()

    # No staleness at all (first attempt / a branch at base): the preamble
    # must not mention either — a first attempt's prompt stays unchanged.
    t3 = _task(id="ccc")
    t3.context = {}
    prompt3 = _orch()._build_implement_prompt(t3, "/tmp/repo")
    assert "REBASED" not in prompt3
    assert "COMMIT(S) BEHIND" not in prompt3
