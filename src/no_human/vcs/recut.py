"""Recover a task whose branch has diverged from its own pushed tip.

BACKGROUND. A task's local branch can be rewritten after it was already
pushed once (a rebase onto a moved base, a squash, a human's manual
`--amend`). `GitRepo.push_sha_fast_forward` then correctly refuses to push
it — the remote holds a commit the new local tip does not descend from,
and this codebase never force-pushes to recover from that (see
`GitRepo.push_sha_fast_forward`'s docstring). Before this module existed,
that refusal had no recovery path: every subsequent attempt reproduced the
same reviewed, green diff, hit the same non-fast-forward refusal at
delivery, and the task looped forever without ever reaching a human or a
merged PR.

CHOSEN SHAPE: RECUT, performed automatically, additive-only.

Two shapes were on the table at intake: (a) RECUT — cut a fresh, never-
before-pushed branch name, point it at the already-reviewed sha, and push
that (trivially a fast-forward, since the name is new) — or (b) DETECT-
AND-ESCALATE — recognise the loop and park the task for a human instead of
retrying forever. (b) terminates the loop but throws away already-
reviewed, already-green work and still needs a human for every single
occurrence. (a) also terminates the loop, but it *delivers* the work, and
it does so without asking anyone anything: a recut only ever creates a new
ref at a name the remote has never seen and fast-forward-pushes to it, so
it is exactly as safe as any other fresh branch push in this codebase —
nothing published is read-modified, let alone rewritten. The "automatic or
approval-first?" question this raises at intake is answered by the shape
itself: because a recut never mutates anything a human has already seen
(the old branch and its PR are left byte-for-byte alone), there is no
authority to ask permission from. The one-shot bound enforced by the
caller (see `orchestrator.py`'s `task.context["recut"]` bookkeeping) is
what keeps an unattended failure honest if the *recut* branch itself
somehow also diverges: that is escalated, never recut a second time.

This module is a pure, dependency-free primitive over `GitRepo` — it does
not import the orchestrator or the store, so it is unit-testable directly
against a real temp repo + a local bare "origin". It NEVER forces, NEVER
deletes a ref, and NEVER touches any ref other than the brand-new one it
creates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .git import GitError, GitRepo

__all__ = [
    "RecutResult",
    "already_recut",
    "branch_stem",
    "diverged_state",
    "next_recut_branch",
    "recut",
]


def branch_stem(config: dict, task_id: str) -> str:
    """The task's own branch family name, e.g. ``no-human/abc12345``.

    Mirrors the exact naming convention used at branch-creation time
    (`orchestrator.py`'s `_run_attempt`, ~5682) and at the sibling-branch
    scan used to satisfy an already-satisfied claim (~12571). This
    function does not change that convention — it only reads it — so
    every existing branch name (bare stem, or ``stem-N`` for attempt
    N > 1) is still exactly what it always was.
    """
    prefix = (config.get("git") or {}).get("branch_prefix") or "no-human/"
    return f"{prefix}{task_id[:8]}"


def _stem_pattern(stem: str) -> re.Pattern:
    return re.compile(rf"{re.escape(stem)}(-(\d+))?")


def next_recut_branch(
    repo: GitRepo, stem: str, *, remote: str = "origin", timeout: int = 30,
) -> str:
    """The smallest never-yet-used ``<stem>-N`` name, local or remote.

    Scans both `git for-each-ref refs/heads/<stem>*` (this worktree's own
    branches) and `git ls-remote origin refs/heads/<stem>*` (what's
    actually published) and takes ``max(suffix) + 1`` over the union — a
    bare ``stem`` (no ``-N`` suffix, i.e. the first attempt's branch)
    counts as suffix 1, matching the existing ``-2``-starts-at-attempt-2
    convention. Taking the max over *both* local and remote — not just
    local — is what guarantees the returned name has no ref anywhere the
    remote has already seen, which is exactly what makes the recut push a
    fast-forward: git has never advertised that ref before, so there is
    nothing for the new push to conflict with.

    Read-only: writes no ref, creates no branch. A remote that cannot be
    reached contributes no candidates (fails open to "assume nothing is
    published there yet") — the local scan still bounds the result to
    something this worktree has never used, and a live remote check right
    before the actual push (inside `recut`) will refuse a genuine race
    rather than silently collide.
    """
    pattern = _stem_pattern(stem)
    max_suffix = 0
    local = repo._run(
        "for-each-ref", "--format=%(refname:short)", f"refs/heads/{stem}*",
        check=False,
    )
    for name in local.splitlines():
        name = name.strip()
        if not name:
            continue
        m = pattern.fullmatch(name)
        if not m:
            continue
        max_suffix = max(max_suffix, int(m.group(2)) if m.group(2) else 1)
    remote_names = repo.list_remote_branch_names(f"{stem}*", remote=remote, timeout=timeout)
    for name in remote_names:
        m = pattern.fullmatch(name)
        if not m:
            continue
        max_suffix = max(max_suffix, int(m.group(2)) if m.group(2) else 1)
    return f"{stem}-{max_suffix + 1}"


def diverged_state(repo: GitRepo, branch: str, *, remote: str = "origin") -> str:
    """Thin wrapper on `GitRepo.remote_branch_relation` — the vocabulary a
    recovery decision needs, unchanged: ``"up_to_date"``, ``"behind"``
    (the remote holds commits this branch does not — recutting here would
    silently orphan them, so this state must never trigger a recut),
    ``"diverged"`` (neither is an ancestor of the other — the only state a
    recut is for), or ``"unknown"`` (never pushed, or the remote could not
    be read — fails open to "do nothing", exactly today's behaviour).
    """
    return repo.remote_branch_relation(branch, remote=remote)


def already_recut(ctx: dict | None, branch: str) -> bool:
    """Has `branch` already been recut once?

    Bookkeeping lives at ``task.context["recut"]``, a list of dicts (one
    per recut ever performed for this task). RFC 7396 JSON-merge-patch
    (used by `Store.merge_context`) REPLACES lists wholesale rather than
    appending — a caller adding a new entry must read this list, append,
    and write the whole thing back, never merge a single-element list.
    This function only reads; it never mutates.
    """
    for entry in (ctx or {}).get("recut") or []:
        if isinstance(entry, dict) and entry.get("from_branch") == branch:
            return True
    return False


@dataclass(frozen=True)
class RecutResult:
    to_branch: str
    reviewed_sha: str
    remote_sha: str
    from_branch: str


def recut(
    repo: GitRepo, *, stem: str, from_branch: str, reviewed_sha: str,
    remote_sha: str, remote: str = "origin",
) -> RecutResult:
    """Cut a fresh branch at `reviewed_sha` and push it, fast-forward only.

    1. Re-read `from_branch`'s live remote tip and assert it is still
       `remote_sha` — never act on a cached snapshot; if it moved again
       between the caller's read and this call, refuse rather than push
       against a state nobody has actually observed.
    2. `repo.create_branch(new, base=reviewed_sha)` — the reviewed commit
       already carries every bit of the (possibly rebased/rewritten)
       work; "replay" here is a ref creation, not a cherry-pick or a
       rebase, so there is no new commit, no new author stamp, and no
       question of which identity performed it.
    3. `repo.push_sha_fast_forward(reviewed_sha, new, set_upstream=True)`
       — see that method's docstring: "never `--force`, never
       `--force-with-lease`". This is the one and only network write
       this function performs, and it targets a branch name the remote
       has never advertised, so git accepts it as an ordinary
       fast-forward (creation) push.
    4. Return the result. Any `GitError`/`ProtectedBranch` from either
       git call propagates verbatim — the caller escalates; it never
       retries with force.

    Idempotent: a second call after a crash between steps 2 and 3 either
    lands on the same (already correct) local ref and a no-op push
    ("Everything up-to-date"), or — if the caller instead re-derives a
    fresh `next_recut_branch` — simply allocates `N+1` again with no
    corrupt state either way.
    """
    live_remote = repo.fetch_remote_branch_sha(from_branch, remote=remote)
    if live_remote != remote_sha:
        raise GitError(
            f"recut aborted: {from_branch}'s remote tip moved from "
            f"{remote_sha} to {live_remote!r} since it was last read; "
            "refusing to recut against a stale snapshot")
    if repo.resolve_commitish(reviewed_sha) is None:
        raise GitError(
            f"recut aborted: reviewed sha {reviewed_sha!r} does not "
            "resolve to a commit in this repository")
    to_branch = next_recut_branch(repo, stem, remote=remote)
    repo.create_branch(to_branch, base=reviewed_sha)
    repo.push_sha_fast_forward(
        reviewed_sha, to_branch, remote=remote, set_upstream=True)
    return RecutResult(
        to_branch=to_branch, reviewed_sha=reviewed_sha,
        remote_sha=remote_sha, from_branch=from_branch,
    )
