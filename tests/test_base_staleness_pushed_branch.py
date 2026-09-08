"""A rebased task branch can never be delivered — the bug this file pins.

`_refresh_stale_base` rewrites a stale branch with `rebase_onto` unconditionally.
That is harmless for a branch nobody has fetched yet, but once the branch has
been PUSHED, a rebase rewrites every commit, making the previously-pushed
remote tip mutually unreachable with the new head. Delivery's ancestor check
(`Orchestrator._reconcile_remote_branch`, via `is_ancestor(remote_tip,
target)`) then refuses the branch forever: 'remote tip ... is not an ancestor
of the reviewed sha' — even though the push guard is correctly declining to
force a non-fast-forward push it never should attempt.

The fix is at the staleness DECISION, not the ancestor check: `staleness_mode`
picks `"merge"` instead of `"rebase"` whenever the branch's LIVE remote tip
(`fetch_remote_branch_sha`) is truthy. A merge commit's head is a DESCENDANT
of the branch's previous tip, so a remote tip that equalled that previous tip
stays an ancestor of the new head — delivery's fast-forward path still works,
no force anywhere. A branch that has never been pushed still rebases, exactly
as before.
"""
from __future__ import annotations

import subprocess
import types

import pytest

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
    """Ends the attempt immediately after `_refresh_stale_base` has run —
    same pattern as `tests/test_retry_base_staleness.py`: the coder session,
    review and tests are real subprocess work and none of it is under test
    here."""


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
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return work


def _advance_main(work, n):
    for i in range(n):
        (work / f"main-advance-{i}.py").write_text(f"m = {i}\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", f"main advances {i}")


def _make_pushed_stale_branch(work, name, n):
    """A branch pushed to `origin`, then left `n` commits behind by the time
    the retry looks at it — the exact shape that used to be un-deliverable
    after a rebase: pushed once, then rewritten."""
    _git(work, "checkout", "-q", "-b", name)
    (work / "pr_marker.py").write_text("# a PR's committed work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "PR work")
    _git(work, "push", "-q", "-u", "origin", name)
    remote_tip = _git(work, "rev-parse", name)
    _git(work, "checkout", "-q", "main")
    _advance_main(work, n)
    _git(work, "checkout", "-q", name)
    return remote_tip


def _make_pushed_conflicting_branch(work, name):
    """A 1-commit gap, pushed, where both sides rewrite the same line of
    `calc.py` — the merge genuinely conflicts."""
    _git(work, "checkout", "-q", "-b", name)
    (work / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "branch rewrites the return line")
    _git(work, "push", "-q", "-u", "origin", name)
    remote_tip = _git(work, "rev-parse", name)
    _git(work, "checkout", "-q", "main")
    (work / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "main rewrites the return line differently")
    _git(work, "checkout", "-q", name)
    return remote_tip


async def _attempt(repo, tmp_path, store, monkeypatch, ctx):
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

    t = Task.new("pushed stale base retry", repo_path=str(repo))
    t.context = ctx
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    with pytest.raises(_Stop):
        await orch._run_attempt(t, GitRepo(repo), 1, "main")
    return t, events, orch


def _staleness_events(events):
    return [e for e in events if e.get("kind") == "base_staleness"]


# --------------------------------------------------------------------------- #
# AC1: a pushed, behind branch is MERGED (not rebased), and the previously
# pushed remote tip provably stays an ancestor of the new head. THE
# FAILS-BEFORE TEST: before the fix, `_refresh_stale_base` rebased
# unconditionally, which rewrites every commit and makes `remote_tip`
# mutually unreachable with the new HEAD — this assertion is exactly the one
# `_reconcile_remote_branch` makes before allowing delivery to push.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_pushed_stale_branch_is_merged_and_stays_deliverable(
    repo, tmp_path, store, monkeypatch,
):
    remote_tip = _make_pushed_stale_branch(
        repo, "no-human/t1", BASE_STALENESS_REBASE_THRESHOLD)
    ctx = {"pr_branch": "no-human/t1"}

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1, [e.get("kind") for e in events]
    ev = evs[0]
    assert ev["commits_behind"] == BASE_STALENESS_REBASE_THRESHOLD
    assert ev["mode"] == "merge"
    assert ev["merged"] is True
    assert ev["rebased"] is False
    assert "merged" in ev["text"] and "rebased" not in ev["text"]

    gr = GitRepo(repo)
    _git(repo, "checkout", "-q", "no-human/t1")
    head = gr.head_sha()
    # The core proof: the tip that was ALREADY on the remote before this
    # attempt touched the branch is still an ancestor of the new head. A
    # rebase would have broken this — rewriting every commit makes the old
    # tip mutually unreachable with the new one.
    assert gr.is_ancestor(remote_tip, head), (
        "the previously-pushed remote tip is no longer an ancestor of HEAD "
        "after base staleness acted — this is the exact defect that made "
        "delivery refuse a rebased, already-pushed branch"
    )

    staleness = t.context["base_staleness"]
    assert staleness["was_behind"] == BASE_STALENESS_REBASE_THRESHOLD
    assert staleness["commits_behind"] == 0
    assert staleness["mode"] == "merge"
    assert staleness["merged"] is True
    assert staleness["rebased"] is False


# --------------------------------------------------------------------------- #
# AC1 continued: delivery's OWN ancestor gate (`_reconcile_remote_branch`)
# accepts the merged branch and fast-forwards the remote — untouched code,
# exercised end to end to prove the fix actually unblocks delivery.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_merged_branch_still_clears_the_delivery_ancestor_gate(
    repo, tmp_path, store, monkeypatch,
):
    _make_pushed_stale_branch(
        repo, "no-human/t2", BASE_STALENESS_REBASE_THRESHOLD)
    ctx = {"pr_branch": "no-human/t2"}

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    gr = GitRepo(repo)
    _git(repo, "checkout", "-q", "no-human/t2")
    target = gr.head_sha()

    # Must not raise ReviewedShaMismatch — that is precisely the refusal the
    # bug caused for a rebased, already-pushed branch.
    orch._reconcile_remote_branch(
        gr, "no-human/t2", target, human_gated_resume=False)

    origin_dir = repo.parent / "origin.git"
    pushed = subprocess.run(
        ["git", "rev-parse", "refs/heads/no-human/t2"],
        cwd=str(origin_dir), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert pushed == target, "delivery did not fast-forward the remote to the merged head"


# --------------------------------------------------------------------------- #
# AC2: a branch that has NEVER been pushed still rebases — mode, event text
# and behaviour are all unchanged for this case.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_never_pushed_branch_still_rebases(
    repo, tmp_path, store, monkeypatch,
):
    # `main` is pushed (so `origin` is configured and reachable), but the
    # task branch itself is never pushed — `fetch_remote_branch_sha` must
    # read that as "never pushed" (None), not "no remote configured".
    _git(repo, "checkout", "-q", "-b", "no-human/t3")
    (repo / "pr_marker.py").write_text("# never pushed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "PR work, never pushed")
    _git(repo, "checkout", "-q", "main")
    _advance_main(repo, BASE_STALENESS_REBASE_THRESHOLD)
    _git(repo, "checkout", "-q", "no-human/t3")

    ctx = {"pr_branch": "no-human/t3"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["mode"] == "rebase"
    assert ev["rebased"] is True
    assert ev["merged"] is False
    assert ev["text"] == (
        f"branch no-human/t3 is {BASE_STALENESS_REBASE_THRESHOLD} commit(s) "
        "behind main — rebased onto it"
    ), "event text for the never-pushed rebase case must be unchanged"

    staleness = t.context["base_staleness"]
    assert staleness["rebased"] is True
    assert staleness["mode"] == "rebase"
    assert staleness["commits_behind"] == 0


# --------------------------------------------------------------------------- #
# The adopted merge-conflict assumption: abort and proceed un-updated, never
# fail the attempt, never fall back to rebase (that would reintroduce the
# exact non-ancestor refusal this fix closes).
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_conflicting_merge_does_not_fail_the_attempt_and_never_falls_back_to_rebase(
    repo, tmp_path, store, monkeypatch,
):
    def _boom(self, base):
        raise AssertionError(
            "rebase_onto must never be called for a pushed branch — that "
            "would reintroduce the non-ancestor delivery refusal")
    monkeypatch.setattr(GitRepo, "rebase_onto", _boom)

    _make_pushed_conflicting_branch(repo, "no-human/t4")
    ctx = {"pr_branch": "no-human/t4"}

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["mode"] == "merge"
    assert ev["merged"] is False
    assert ev["rebased"] is False
    assert "merge skipped (conflict)" in ev["text"]

    # No merge in progress was left dangling.
    status = _git(repo, "status", "--porcelain")
    assert status == "", f"merge conflict was not cleanly aborted: {status!r}"

    staleness = t.context["base_staleness"]
    assert staleness["rebased"] is False
    assert "mode" not in staleness, (
        "a failed/no-op action keeps the record shape unchanged, per "
        "staleness_record's contract"
    )


# --------------------------------------------------------------------------- #
# AC3: no force-push anywhere in the two files this fix touched.
# --------------------------------------------------------------------------- #

def test_no_force_push_introduced_by_this_fix():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "no_human"
    for rel in ("vcs/git.py", "core/orchestrator.py"):
        text = (src / rel).read_text(encoding="utf-8")
        assert "merge_base_into_branch" in text or rel != "vcs/git.py"
        # The new merge path must not introduce --force / --force-with-lease
        # anywhere it wasn't already present.
        merge_fn_start = text.find("def merge_base_into_branch")
        if merge_fn_start != -1:
            merge_fn_end = text.find("\n    def ", merge_fn_start + 1)
            merge_fn_src = text[merge_fn_start:merge_fn_end if merge_fn_end != -1 else None]
            assert "--force" not in merge_fn_src
            assert "force-with-lease" not in merge_fn_src
        refresh_fn_start = text.find("async def _refresh_stale_base")
        if refresh_fn_start != -1:
            refresh_fn_end = text.find("\n    async def ", refresh_fn_start + 1)
            refresh_fn_src = text[refresh_fn_start:refresh_fn_end if refresh_fn_end != -1 else None]
            assert "--force" not in refresh_fn_src
            assert "force-with-lease" not in refresh_fn_src
