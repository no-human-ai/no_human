"""Recover a REWORK-AFTER-REJECTION whose branch has diverged from its own
pushed tip.

BACKGROUND. A human rejects a task whose branch was already pushed once
(`nh reject`, which records `send_back_feedback` / `pending_send_back` on
the task's context — see `blockers.send_back`). The rework the agent then
builds is not guaranteed to be a descendant of that pushed tip: it starts
fresh from the base, not from the pushed commit, so the two lines diverge.
At delivery, `Orchestrator._reconcile_remote_branch`'s ancestor check
correctly refuses to push it (`ReviewedShaMismatch` — see that class's
docstring in `core/orchestrator.py`) — and before this module existed, that
refusal had no route forward: every subsequent attempt reproduced the same
diverged shape, hit the same refusal, and the attempt was spent for
nothing. This occurred 10 times across 8 distinct tasks in production.

CHOSEN SHAPE: RECONVERGE, performed automatically, additive-only.

This is a sibling to `vcs.recut`, not a replacement for it: `recut` responds
to a branch that rewrote its OWN previously-pushed history (a rebase, an
amend) by abandoning the pushed tip on a fresh, never-before-pushed branch
name — a new PR. That is wrong here: the pushed tip is a human-visible,
already-reviewed PR that the human just sent back for changes, and the
rework belongs on the SAME branch, replayed on top of it, not orphaned onto
a new one. Reconvergence therefore REBASES the rework onto its own pushed
tip in place — `GitRepo.rebase_branch_onto` — so the pushed tip becomes an
ancestor of the new head and the existing PR simply gains a fast-forward
push, same branch, same PR, no new ref.

This module is a pure, dependency-free primitive over `GitRepo` (it does
not import the orchestrator, the store, or `blockers/`) so it is unit-
testable directly against a real temp repo + a local bare "origin", exactly
like `vcs.recut`. It NEVER forces, NEVER deletes a ref, and NEVER rewrites
any commit the pushed tip already carries — a rebase onto that tip can only
ever make it more reachable, never less.
"""

from __future__ import annotations

from dataclasses import dataclass

from .git import GitError, GitRepo, ProtectedBranch

# Mirrors `blockers.send_back.PENDING_KEY` as a literal string copy, the same
# dependency-free stance `vcs.recut` documents for itself: this module
# imports nothing from `blockers/`, so a rework-in-flight marker is still
# detectable here without a package-boundary import.
_PENDING_SEND_BACK_KEY = "pending_send_back"

__all__ = [
    "DivergenceSummary",
    "ReconvergeResult",
    "already_reconverged",
    "divergence_summary",
    "is_rework_after_rejection",
    "reconverge",
]


def is_rework_after_rejection(ctx: dict | None, branch: str) -> bool:
    """Was *branch* rejected by a human and is now being reworked?

    Inferred, not stored as an explicit flag: `ctx["pr_branch"] == branch`
    (this task is still driving that same branch/PR) AND either a non-empty
    `ctx["send_back_feedback"]` list (`nh reject` appends to it — see
    `cli/commands.py`'s `reject` command) or a `pending_send_back` marker
    (`blockers.send_back.PENDING_KEY`) is present. Either alone is not
    enough: a branch can equal `pr_branch` on a task that was never sent
    back, and `send_back_feedback` can outlive the branch that earned it if
    a later recut changed `pr_branch`.
    """
    ctx = ctx or {}
    if ctx.get("pr_branch") != branch:
        return False
    feedback = ctx.get("send_back_feedback")
    if isinstance(feedback, list) and feedback:
        return True
    return bool(ctx.get(_PENDING_SEND_BACK_KEY))


def already_reconverged(ctx: dict | None, branch: str) -> bool:
    """Has *branch* already been reconverged once?

    Bookkeeping lives at `task.context["reconverge"]`, a list of dicts (one
    per reconvergence ever performed for this task) — same RFC 7396
    JSON-merge-patch caveat `vcs.recut.already_recut` documents: a caller
    adding a new entry must read this list, append, and write the whole
    thing back, never merge a single-element list. This function only
    reads; it never mutates. The one-shot bound this enables is what keeps
    an unattended reconvergence honest if the rebased result somehow still
    diverges from a since-moved pushed tip: that is escalated, never
    reconverged a second time.
    """
    for entry in (ctx or {}).get("reconverge") or []:
        if isinstance(entry, dict) and entry.get("branch") == branch:
            return True
    return False


@dataclass(frozen=True)
class DivergenceSummary:
    """A human-readable shape of a divergence, for the escalation message
    when reconvergence is not applicable or itself fails.

    `heavier` is `"local"` when the rework carries more unique commits than
    the pushed tip, `"pushed"` the other way, `"equal"` when the counts tie
    (including the degenerate 0-0 case for unrelated histories)."""

    merge_base: str | None
    local_only: int
    pushed_only: int
    heavier: str


def divergence_summary(
    repo: GitRepo, local_sha: str, pushed_sha: str,
) -> DivergenceSummary:
    """Summarise how *local_sha* and *pushed_sha* diverged. Never raises —
    an unmeasurable divergence still owes the human a (blank) summary, not
    a crash out of an escalation path."""
    try:
        base = repo.merge_base(pushed_sha, local_sha)
    except GitError:
        base = None
    if not base:
        return DivergenceSummary(
            merge_base=None, local_only=0, pushed_only=0, heavier="equal")
    try:
        local_only = repo.count_commits_between(pushed_sha, local_sha)
        pushed_only = repo.count_commits_between(local_sha, pushed_sha)
    except GitError:
        local_only = pushed_only = 0
    if local_only > pushed_only:
        heavier = "local"
    elif pushed_only > local_only:
        heavier = "pushed"
    else:
        heavier = "equal"
    return DivergenceSummary(
        merge_base=base, local_only=local_only, pushed_only=pushed_only,
        heavier=heavier)


@dataclass(frozen=True)
class ReconvergeResult:
    branch: str
    from_sha: str
    pushed_sha: str
    to_sha: str
    replayed: int


def reconverge(
    repo: GitRepo, *, branch: str, local_sha: str, pushed_sha: str,
    remote: str = "origin",
) -> ReconvergeResult:
    """Rebase *branch* (currently at *local_sha*) onto its own pushed tip
    *pushed_sha*, in place, so the pushed tip becomes an ancestor of the
    new head and delivery can fast-forward. Raises `GitError` (or
    `ProtectedBranch`, from `rebase_branch_onto`) on any refusal or
    failure; the caller falls back to its own recovery (recut /
    escalation) — this function never forces, never deletes a ref, and
    only ever moves `branch` itself.

    1. Re-read `branch`'s live remote tip and assert it is still
       `pushed_sha` — never act on a cached snapshot; if it moved again
       between the caller's read and this call, refuse rather than
       reconverge against a state nobody has actually observed (same
       stale-snapshot guard `vcs.recut.recut` opens with).
    2. Refuse if either sha does not resolve locally, and refuse (as a
       no-op, not a failure) if `branch` already descends from
       `pushed_sha` — there is nothing to reconverge.
    3. Refuse if the two sides share no history (`merge_base` is None) —
       there is no base to replay from.
    4. `repo.checkout(branch)` then `repo.rebase_branch_onto(pushed_sha,
       upstream=merge_base)` — replays only `merge_base..local_sha` on top
       of the pushed tip. A conflict raises `GitError`; the branch is left
       exactly where it started (`rebase_branch_onto`'s own contract).
    5. Verify the postcondition directly (`is_ancestor(pushed_sha,
       new_head)`) rather than trusting the rebase succeeded in the shape
       expected — if it somehow did not, undo back to `local_sha` and
       raise rather than hand the caller a branch that still cannot be
       delivered.
    """
    live_remote = repo.fetch_remote_branch_sha(branch, remote=remote)
    if live_remote != pushed_sha:
        raise GitError(
            f"reconverge aborted: {branch}'s remote tip moved from "
            f"{pushed_sha} to {live_remote!r} since it was last read; "
            "refusing to reconverge against a stale snapshot")
    if (repo.resolve_commitish(local_sha) is None
            or repo.resolve_commitish(pushed_sha) is None):
        raise GitError(
            f"reconverge aborted: local {local_sha!r} or pushed "
            f"{pushed_sha!r} does not resolve to a commit in this "
            "repository")
    if repo.is_ancestor(pushed_sha, local_sha):
        raise GitError(
            f"reconverge aborted: {branch} already descends from its "
            f"pushed tip {pushed_sha}; nothing to reconverge")
    base = repo.merge_base(pushed_sha, local_sha)
    if base is None:
        raise GitError(
            f"reconverge aborted: {branch}'s rework {local_sha} and its "
            f"pushed tip {pushed_sha} share no history; cannot rebase "
            "onto an unrelated line")
    repo.checkout(branch)
    moved = repo.rebase_branch_onto(pushed_sha, upstream=base)
    if not moved:
        raise GitError(
            f"reconverge aborted: rebasing {branch} ({local_sha}) onto "
            f"its pushed tip {pushed_sha} conflicted")
    new_head = repo.branch_sha(branch)
    if not repo.is_ancestor(pushed_sha, new_head):
        repo.checkout(branch)
        repo._run("reset", "--hard", local_sha, check=False)
        raise GitError(
            f"reconverge aborted: rebasing {branch} completed but its "
            f"pushed tip {pushed_sha} is still not an ancestor of the "
            f"result {new_head}; refusing an unsafe reconvergence")
    replayed = repo.count_commits_between(pushed_sha, new_head)
    return ReconvergeResult(
        branch=branch, from_sha=local_sha, pushed_sha=pushed_sha,
        to_sha=new_head, replayed=replayed)
