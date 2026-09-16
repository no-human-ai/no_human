"""When a rework's branch genuinely diverges from its own already-pushed
tip and `_align_branch_with_pushed_tip` cannot (or was not run to) resolve
it, delivery still refuses — `_reconcile_remote_branch`'s ancestor check and
the pushed-tip rewrite guard are both untouched by the fix in
`tests/test_rejected_rework_pushed_tip.py`. What changed is what happens
AFTER the refusal: `_finalize` used to hand `ReviewedShaMismatch` to the
generic, empty-options `_escalate` fallback; it now routes to
`_escalate_diverged_pushed_branch`, which names both full shas, says which
side carries more work, and offers merging the pushed tip in as a concrete,
non-automatic option (`blockers.report.diverged_pushed_branch`).

This file pins:
  * AC3 — `_reconcile_remote_branch`'s raised message is byte-for-byte
    unchanged, and the pushed-tip rewrite guard (`agent.pushed_tip_guard`)
    still denies a genuine rebase/reset/force-push on a pushed branch. A
    force push (or anything that rewrites history below a pushed tip) is
    NEVER how this bug is fixed.
  * AC4 — the escalation the human actually sees names both shas, states
    which side carries more work, and offers the merge as a real option
    with a non-empty options list — never the empty-options fallback.
"""
from __future__ import annotations

import subprocess

import pytest

from no_human.agent import guard, pushed_tip_guard
from no_human.blockers import report
from no_human.config import load_config
from no_human.core.orchestrator import (
    BASE_STALENESS_REBASE_THRESHOLD,
    Orchestrator,
    ReviewedShaMismatch,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo

from tests.test_base_staleness_pushed_branch import (  # noqa: F401
    _git,
    _make_pushed_diverged_branch,
    origin,
    repo,
)
from tests.test_failure_reason_history_query import _seed_attempt
from tests.test_rejected_rework_pushed_tip import _make_diverged_rework


def _orch(store, tmp_path, events=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(
        store, cfg.data, object(), SlackNotifier(None),
        event_sink=(events.append if events is not None else None),
    )


def _stamp(ctx, sha, *, passed=True):
    ctx = dict(ctx or {})
    ctx["review_history"] = [
        {"round": 1, "sha": sha, "passed": passed, "blocking": [], "advisory": []},
    ]
    return ctx


class _Commit:
    files_changed = 1
    insertions = 1
    deletions = 0
    sha = ""


class _Result:
    final_text = "Implemented the change."
    num_turns = 3


# --------------------------------------------------------------------------- #
# AC3: the ancestor check and the pushed-tip guard are UNCHANGED — a genuine
# force-push/rewrite attempt is still refused, verbatim message included.
# --------------------------------------------------------------------------- #

def test_reconcile_remote_branch_message_is_unchanged_and_now_carries_shas(repo):
    """Re-pins the exact string `test_base_staleness_pushed_branch.py` already
    pins (out of scope to touch), and additionally proves the NEW structured
    attributes (`remote_tip`/`reviewed_sha`/`branch`) the escalation path
    needs are populated — without the message text itself changing at all."""
    remote_tip = _make_pushed_diverged_branch(
        repo, "no-human/esc1", BASE_STALENESS_REBASE_THRESHOLD)
    gr = GitRepo(repo)
    target = gr.head_sha()
    assert gr.is_ancestor(remote_tip, target) is False

    orch = Orchestrator.__new__(Orchestrator)
    with pytest.raises(ReviewedShaMismatch) as exc_info:
        orch._reconcile_remote_branch(
            gr, "no-human/esc1", target, human_gated_resume=False)

    message = str(exc_info.value)
    assert message == (
        f"delivery refused: branch no-human/esc1 remote tip {remote_tip} "
        f"(fetched) is not an ancestor of the reviewed sha {target} "
        f"(human_gated_resume=False)"
    ), message

    exc = exc_info.value
    assert exc.remote_tip == remote_tip
    assert exc.reviewed_sha == target
    assert exc.branch == "no-human/esc1"

    origin_dir = repo.parent / "origin.git"
    origin_ref = subprocess.run(
        ["git", "rev-parse", "refs/heads/no-human/esc1"],
        cwd=str(origin_dir), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert origin_ref == remote_tip, "the fix must never move the remote here"


def test_a_genuine_force_push_attempt_is_still_refused(repo):
    """Two independent guards, both untouched by this fix, both still deny a
    real rewrite of a pushed branch: (1) `push_sha_fast_forward` itself
    refuses a non-fast-forward push at the git layer, and (2) the coder-time
    `pushed_tip_guard` denies the rebase/reset commands that would produce
    one, naming the pushed tip (per `_DENIED_FORMS` in
    `test_pushed_tip_rewrite_guard.py` — out of scope to touch, reused here
    only by calling the same module-level `denial_reason` function)."""
    remote_tip = _make_pushed_diverged_branch(
        repo, "no-human/esc2", BASE_STALENESS_REBASE_THRESHOLD)
    gr = GitRepo(repo)
    non_descendant = gr.head_sha()
    assert gr.is_ancestor(remote_tip, non_descendant) is False

    from no_human.vcs.git import GitError
    with pytest.raises(GitError):
        gr.push_sha_fast_forward(non_descendant, "no-human/esc2")

    origin_dir = repo.parent / "origin.git"
    origin_ref = subprocess.run(
        ["git", "rev-parse", "refs/heads/no-human/esc2"],
        cwd=str(origin_dir), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert origin_ref == remote_tip, "a rejected non-fast-forward must never move the remote"

    for cmd in (
        "git rebase origin/main",
        "git reset --hard HEAD~1",
        "git reset --hard origin/main",
        "git commit --amend",
    ):
        d = pushed_tip_guard.denial_reason(
            guard._git_invocations(cmd), str(repo))
        assert d is not None, f"{cmd!r} must still be denied on a pushed branch"
        assert remote_tip in d, d


# --------------------------------------------------------------------------- #
# AC4: when divergence still cannot be avoided, the escalation names both
# shas, says which side carries more work, and offers the merge as a
# concrete option rather than an empty options list.
# --------------------------------------------------------------------------- #

def test_the_escalation_names_both_shas_and_offers_the_merge(repo):
    remote_tip = _make_diverged_rework(repo, "no-human/esc3")
    gr = GitRepo(repo)
    reviewed_sha = gr.head_sha()
    assert gr.is_ancestor(remote_tip, reviewed_sha) is False
    assert gr.is_ancestor(reviewed_sha, remote_tip) is False

    stats = gr.divergence_stats(remote_tip, reviewed_sha)
    detail = (
        f"delivery refused: branch no-human/esc3 remote tip {remote_tip} "
        f"(fetched) is not an ancestor of the reviewed sha {reviewed_sha} "
        f"(human_gated_resume=False)"
    )
    blocker = report.diverged_pushed_branch(
        branch="no-human/esc3", remote_tip=remote_tip, reviewed_sha=reviewed_sha,
        stats=stats, detail=detail, goal="Fix the thing",
    )

    # Both FULL shas, not truncated prefixes, must be findable somewhere a
    # human reviewing the escalation will look.
    haystack = f"{blocker.root_cause_hypothesis}\n{blocker.evidence}\n{blocker.question}"
    assert remote_tip in haystack, haystack
    assert reviewed_sha in haystack, haystack

    # It must say which side carries more work (both sides here carry
    # exactly one commit / one file each, from `_make_diverged_rework`).
    assert "carries more work" in blocker.root_cause_hypothesis, blocker.root_cause_hypothesis

    # Never the empty-options fallback: a concrete, non-empty options list,
    # merge offered first, and no option invents a new action verb.
    assert blocker.options, "escalation must not fall back to an empty options list"
    assert len(blocker.options) >= 2
    assert "merge" in blocker.options[0].label.lower()
    assert remote_tip[:8] in blocker.options[0].label
    for opt in blocker.options:
        assert opt.action is None


@pytest.mark.asyncio
async def test_delivery_refusal_routes_to_the_structured_blocker_not_the_empty_fallback(
    repo, tmp_path, store,
):
    """Drives a REAL `_finalize` call through a genuine diverged-pushed-tip
    `ReviewedShaMismatch` (stamping `review_history` with the diverged local
    head so `_assert_delivery_sha` -> `_reconcile_remote_branch` raises it
    for real) and proves the task's persisted blocker is the structured
    escalation, not the old empty-options `_escalate` fallback."""
    remote_tip = _make_diverged_rework(repo, "no-human/esc4")
    gr = GitRepo(repo)
    reviewed_sha = gr.head_sha()
    assert gr.is_ancestor(remote_tip, reviewed_sha) is False

    events: list[dict] = []
    orch = _orch(store, tmp_path, events=events)
    task = Task.new("Fix the thing", repo_path=str(repo))
    task.context = _stamp(task.context, reviewed_sha)
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)

    commit = _Commit()
    commit.sha = reviewed_sha

    out = await orch._finalize(
        task, gr, "no-human/esc4", "main", commit, attempt_id, _Result())

    assert out.status == TaskStatus.ESCALATED, out.detail

    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert len(mismatches) == 1, events
    assert "is not an ancestor of the reviewed sha" in mismatches[0]["text"]

    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed"
    assert "is not an ancestor of the reviewed sha" in attempts[-1]["failure_reason"]

    blocker = task.blocker
    assert blocker is not None
    assert blocker["question"] != "Review the blocker and advise how to proceed.", blocker
    assert blocker["options"], "must not be the empty-options fallback"
    assert remote_tip in blocker["evidence"]
    assert reviewed_sha in blocker["evidence"]
    assert any("merge" in o["label"].lower() for o in blocker["options"])
    assert all(o["action"] is None for o in blocker["options"])


@pytest.mark.asyncio
async def test_the_escalation_states_a_query_derived_historical_count(
    repo, tmp_path, store,
):
    """AC5: the escalation must state how many attempts have hit this exact
    failure class historically, using `Store.count_attempts_failing_like`
    over recorded `failure_reason` rows (`_escalate_diverged_pushed_branch`)
    — never an invented or estimated number. Seeds three prior attempts
    with the genuine refusal wording (plus a lookalike and an unrelated
    reason, which must NOT be counted) on the SAME store `_finalize` below
    reads from, then drives a real diverged-pushed-tip refusal through it
    and asserts the blocker's evidence carries the query's actual count."""
    await _seed_attempt(
        store, title="prior-1",
        failure_reason=(
            "delivery refused: branch no-human/aaa-1 remote tip aaa111 "
            "(fetched) is not an ancestor of the reviewed sha bbb222 "
            "(human_gated_resume=False)"))
    await _seed_attempt(
        store, title="prior-2",
        failure_reason=(
            "delivery refused: branch no-human/bbb-1 remote tip ccc333 "
            "(fetched) is not an ancestor of the reviewed sha ddd444 "
            "(human_gated_resume=False)"))
    await _seed_attempt(
        store, title="prior-3",
        failure_reason=(
            "delivery refused: branch no-human/ccc-1 remote tip eee555 "
            "(fetched) is not an ancestor of the reviewed sha fff666 "
            "(human_gated_resume=True)"))
    # A lookalike and an unrelated reason — must NOT inflate the count.
    await _seed_attempt(
        store, title="prior-lookalike",
        failure_reason=(
            "delivery refused: could not fast-forward no-human/x to "
            "reviewed sha zzz999: some ancestor lookup failed"))
    await _seed_attempt(
        store, title="prior-unrelated", failure_reason="budget exhausted after 40 turns")

    remote_tip = _make_diverged_rework(repo, "no-human/esc5")
    gr = GitRepo(repo)
    reviewed_sha = gr.head_sha()
    assert gr.is_ancestor(remote_tip, reviewed_sha) is False

    orch = _orch(store, tmp_path)
    task = Task.new("Fix the thing", repo_path=str(repo))
    task.context = _stamp(task.context, reviewed_sha)
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)

    commit = _Commit()
    commit.sha = reviewed_sha

    out = await orch._finalize(
        task, gr, "no-human/esc5", "main", commit, attempt_id, _Result())
    assert out.status == TaskStatus.ESCALATED, out.detail

    blocker = task.blocker
    assert blocker is not None
    # The THIS attempt's own failure_reason is recorded by `_finalize`
    # before the escalation queries — the count therefore includes it too
    # (4 total: the 3 seeded genuine hits + this one), proving the number
    # comes from the live query, not a value computed before this call.
    assert "4 attempt(s)" in blocker["evidence"], blocker["evidence"]
