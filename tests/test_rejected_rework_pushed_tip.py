"""A rejected task's rework can diverge from its own already-pushed tip —
the bug this file pins.

Neither the revision/reject route (`ctx['pr_branch']`) nor the fresh route
(`create_branch`/`checkout -B`) in `_run_attempt` ever read the branch's LIVE
remote tip (`fetch_remote_branch_sha`) before the coder session started, so a
rework could silently be built on a line that resets below, or otherwise
diverges from, an already-pushed tip. Delivery's ancestor guard
(`Orchestrator._reconcile_remote_branch`) then correctly refuses: 'delivery
refused: branch ... remote tip ... (fetched) is not an ancestor of the
reviewed sha ...' — observed on 1cbc1c65/7606f734/af1602af.

The fix is `Orchestrator._align_branch_with_pushed_tip`, run before
`_refresh_stale_base` in `_run_attempt`: it reads the branch's live pushed
tip and, if the local head is not already a descendant of it, merges it in —
a plain fast-forward if the head is merely behind, a real `-X ours` merge
commit if the head has genuinely diverged — never a rebase, never a force
push.
"""
from __future__ import annotations

import subprocess

import pytest

from no_human.vcs.git import GitRepo

from tests.test_base_staleness_pushed_branch import (  # noqa: F401
    _attempt,
    _git,
    _Stop,
    origin,
    repo,
)


def _make_diverged_rework(repo, name):
    """A branch pushed once, then reset back to its own parent and given
    different, non-conflicting work — the exact "rework diverges from the
    pushed tip" shape from 1cbc1c65/7606f734/af1602af: neither the pushed
    tip nor the reworked head is an ancestor of the other, and each side
    carries a file the other lacks."""
    _git(repo, "checkout", "-q", "-b", name)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tip_only.txt").write_text("tip work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "PR work (pushed tip)")
    _git(repo, "push", "-q", "-u", "origin", name)
    remote_tip = _git(repo, "rev-parse", name)
    _git(repo, "reset", "-q", "--hard", base)
    (repo / "rework_only.txt").write_text("rework work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "PR work (rejected rework)")
    return remote_tip


def _make_pushed_tip_ahead_of_local(repo, name):
    """Push `name`, then reset the local branch back behind its own pushed
    tip — the shape a `checkout -B` reset produces when establishing a
    revision/reject route branch without reading the live remote tip
    first."""
    _git(repo, "checkout", "-q", "-b", name)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pr_marker.py").write_text("# a PR's committed work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "PR work")
    _git(repo, "push", "-q", "-u", "origin", name)
    remote_tip = _git(repo, "rev-parse", name)
    _git(repo, "reset", "-q", "--hard", base)
    return remote_tip


def _make_local_only_branch(repo, name):
    """A branch created locally and never pushed anywhere."""
    _git(repo, "checkout", "-q", "-b", name)
    (repo / "local_only.py").write_text("# never pushed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local only work")


# --------------------------------------------------------------------------- #
# AC1: a rejected task's rework, built on a branch that diverges from its own
# already-pushed tip, ends up on a line that is a DESCENDANT of that pushed
# tip once `_run_attempt` establishes the branch — before the coder session
# ever starts. THE FAILS-BEFORE TEST: before the fix, nothing in
# `_run_attempt` ever read the branch's live remote tip, so the diverged
# rework head reached the coder (and later delivery) untouched.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_rejected_reworks_diverged_branch_is_realigned_with_its_pushed_tip(
    repo, tmp_path, store, monkeypatch,
):
    gr = GitRepo(repo)
    remote_tip = _make_diverged_rework(repo, "no-human/t1")
    pre_head = gr.head_sha()

    # Positive control: the fixture really reproduces the reported shape —
    # neither sha is an ancestor of the other.
    assert gr.is_ancestor(remote_tip, pre_head) is False
    assert gr.is_ancestor(pre_head, remote_tip) is False

    ctx = {"pr_branch": "no-human/t1"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    _git(repo, "checkout", "-q", "no-human/t1")
    head = gr.head_sha()
    assert gr.is_ancestor(remote_tip, head) is True, (
        "the branch that reaches the coder session (and later delivery) "
        "must be a descendant of its own pushed tip"
    )
    assert (repo / "tip_only.txt").exists(), "the pushed tip's work must survive the realignment"
    assert (repo / "rework_only.txt").exists(), "the rework's own work must survive the realignment"

    realigned = [e for e in events if e.get("kind") == "pushed_tip_realigned"]
    assert len(realigned) == 1, events
    assert realigned[0]["mode"] == "merge"

    assert t.context["pushed_tip_alignment"]["mode"] == "merge"
    assert t.context["pushed_tip_alignment"]["ok"] is True
    assert t.context["pushed_tip_alignment"]["remote_tip"] == remote_tip


@pytest.mark.asyncio
async def test_the_realigned_branch_clears_the_delivery_ancestor_gate(
    repo, tmp_path, store, monkeypatch,
):
    _make_diverged_rework(repo, "no-human/t2")
    ctx = {"pr_branch": "no-human/t2"}

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    gr = GitRepo(repo)
    _git(repo, "checkout", "-q", "no-human/t2")
    target = gr.head_sha()

    # Must not raise ReviewedShaMismatch — this is exactly the refusal the
    # bug produced for a rejected rework diverged from its own pushed tip.
    orch._reconcile_remote_branch(
        gr, "no-human/t2", target, human_gated_resume=False)

    origin_dir = repo.parent / "origin.git"
    pushed = subprocess.run(
        ["git", "rev-parse", "refs/heads/no-human/t2"],
        cwd=str(origin_dir), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert pushed == target, (
        "delivery did not fast-forward the remote to the realigned head"
    )


@pytest.mark.asyncio
async def test_the_reject_revision_route_is_aligned_too(
    repo, tmp_path, store, monkeypatch,
):
    """The local ref is merely BEHIND its own pushed tip (the `checkout -B`
    reset shape) — alignment must fast-forward, producing no merge commit at
    all: the new head must equal the pushed tip exactly."""
    gr = GitRepo(repo)
    remote_tip = _make_pushed_tip_ahead_of_local(repo, "no-human/t3")
    pre_head = gr.head_sha()

    # Positive control: local head is strictly behind the pushed tip.
    assert gr.is_ancestor(pre_head, remote_tip) is True
    assert pre_head != remote_tip

    ctx = {"pr_branch": "no-human/t3"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    _git(repo, "checkout", "-q", "no-human/t3")
    head = gr.head_sha()
    assert head == remote_tip, (
        "a fast-forward must land exactly on the pushed tip, with no merge "
        "commit created"
    )

    realigned = [e for e in events if e.get("kind") == "pushed_tip_realigned"]
    assert len(realigned) == 1, events
    assert realigned[0]["mode"] == "fast_forward"


@pytest.mark.asyncio
async def test_a_branch_never_pushed_is_untouched(
    repo, tmp_path, store, monkeypatch,
):
    _make_local_only_branch(repo, "no-human/t4")
    gr = GitRepo(repo)
    pre_head = gr.head_sha()

    ctx = {"pr_branch": "no-human/t4"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    _git(repo, "checkout", "-q", "no-human/t4")
    assert gr.head_sha() == pre_head, (
        "a branch that was never pushed has no tip to align to"
    )
    assert not [
        e for e in events
        if e.get("kind") in ("pushed_tip_realigned", "pushed_tip_merge_conflict")
    ]


@pytest.mark.asyncio
async def test_a_transient_fetch_failure_leaves_the_branch_alone(
    repo, tmp_path, store, monkeypatch,
):
    """`fetch_remote_branch_sha` returning `None` means "cannot know" — never
    pushed OR the remote is momentarily unreachable — and alignment must fail
    OPEN (no-op) exactly like `_refresh_stale_base` does for the same
    signal; treating it as "diverged" would invent a merge out of a
    transient network blip."""
    _make_diverged_rework(repo, "no-human/t6")
    gr = GitRepo(repo)
    pre_head = gr.head_sha()

    monkeypatch.setattr(GitRepo, "fetch_remote_branch_sha", lambda self, *a, **k: None)

    ctx = {"pr_branch": "no-human/t6"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    _git(repo, "checkout", "-q", "no-human/t6")
    assert gr.head_sha() == pre_head, (
        "alignment must not touch HEAD when the pushed tip can't be read"
    )
    assert not [
        e for e in events
        if e.get("kind") in ("pushed_tip_realigned", "pushed_tip_merge_conflict")
    ]
    assert "pushed_tip_alignment" not in (t.context or {})


@pytest.mark.asyncio
async def test_alignment_never_rebases_and_never_pushes(
    repo, tmp_path, store, monkeypatch,
):
    """`_align_branch_with_pushed_tip` merges — it must never rebase (that is
    exactly the root cause this fix closes) and never pushes (delivery's own
    fast-forward-only push stays the only thing that ever moves the
    remote)."""
    remote_tip = _make_diverged_rework(repo, "no-human/t7")

    def _boom_rebase(self, base):
        raise AssertionError("pushed-tip alignment must never rebase")

    def _boom_push(self, *a, **k):
        raise AssertionError("pushed-tip alignment must never push")

    monkeypatch.setattr(GitRepo, "rebase_onto", _boom_rebase)
    monkeypatch.setattr(GitRepo, "push", _boom_push)

    ctx = {"pr_branch": "no-human/t7"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    origin_dir = repo.parent / "origin.git"
    pushed = subprocess.run(
        ["git", "rev-parse", "refs/heads/no-human/t7"],
        cwd=str(origin_dir), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert pushed == remote_tip, "nothing may push during alignment"
