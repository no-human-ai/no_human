"""Whether a retry's branch should be rebased onto a base that has moved.

`BASE_STALENESS_REBASE_THRESHOLD` gates the rebase on the SIZE of the gap, and
the number was bought with real incidents: `db9da7f7` at 5 commits burned an
attempt re-fixing a test main had already deleted, and 1 was rejected because
it would rebase on nearly every retry, most gaps being an unrelated commit
landing on main mid-attempt.

That reasoning holds and nothing here lowers the threshold. What a commit
count cannot see is a gap that is SMALL and RELATED. On 2026-09-07/08 three
tasks each paid a full conflict-resolution round, re-implement plus re-review
plus re-test, on gaps of one to three commits that touched the branch's own
files (`0e1edabb` against `d071a751`, `05b71017` against `4cbf73e3`,
`82644133` against `72ca3a0b`). That is what a fleet landing several fixes in
one subsystem back to back looks like, and every follower pays a round.

So the gap is measured on a second axis: do the two sides touch the same
files. Both sides are one `git diff --name-only` from the fork point, and a
below-threshold gap whose files intersect the branch's is not noise, it is a
conflict round already waiting at the approve gate.

GENERATED_PATHS is the part that would otherwise wreck this. RELEASE_MANIFEST
pins every shipped file with its hash, so it changes on nearly every landing
AND on nearly every branch, and it is regenerated during the merge anyway. It
intersects everything, so counting it would rebase on essentially every gap
and quietly undo the threshold's whole purpose. Measured live while writing
this: a branch three commits behind main intersected on RELEASE_MANIFEST.txt
and NOTHING else.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

log = logging.getLogger("no_human.base_staleness")

#: Generated files that intersect nearly every pair of branches and are
#: rebuilt during the merge, so an intersection on them predicts no conflict.
GENERATED_PATHS = frozenset({"RELEASE_MANIFEST.txt"})


def overlapping_paths(
    branch_files: Iterable[str], base_files: Iterable[str],
) -> list[str]:
    """The files both sides changed, generated pins excluded, sorted.

    Pure set logic so the decision can be tested without a git repository."""
    shared = (set(branch_files) & set(base_files)) - GENERATED_PATHS
    return sorted(shared)


def base_gap_overlap(repo, base: str | None, behind: int) -> list[str]:
    """The overlap between this branch and the base gap, asked of git.

    ``[]`` when there is no gap to measure, and ``[]`` on ANY failure: this
    feeds a check whose contract is that it never fails an attempt, so an
    unreadable repository degrades to today's count-only behaviour rather
    than raising or rebasing on a guess.
    """
    if not base or behind <= 0:
        return []
    try:
        return overlapping_paths(
            repo.files_changed_since_fork(base),
            repo.files_changed_since_fork(base, on_base=True),
        )
    except Exception as exc:  # noqa: BLE001 — staleness must never raise
        log.debug("base gap overlap unmeasurable: %s", exc)
        return []


def should_rebase(behind: int, threshold: int, overlap: Iterable[str]) -> bool:
    """Rebase past the threshold as before, OR on any real file overlap."""
    return behind >= threshold or bool(list(overlap))


def staleness_mode(
    behind: int, threshold: int, overlap: Iterable[str], remote_tip: str | None,
    *, confirmed_never_pushed: bool = False,
) -> str | None:
    """How to bring a stale branch up to date, or ``None`` to leave it alone.

    A REBASE rewrites every commit on the branch. That is harmless for a
    branch nobody has fetched yet, but once the branch has a pushed remote
    tip, rewriting it makes that tip mutually unreachable with the new head
    — the tip can never again be an ancestor of HEAD. Delivery
    (`Orchestrator._reconcile_remote_branch`) proves exactly that ancestry
    before it will push, so a rebase of an already-pushed branch guarantees
    delivery refuses it later: 'remote tip ... is not an ancestor of the
    reviewed sha', even though the push guard is correctly refusing to force
    a non-fast-forward push it never should. That contradiction — rebase now,
    get refused at delivery — is the defect this function closes.

    A MERGE commit avoids it: the new head is a descendant of the branch's
    previous tip, so a remote tip that equalled that previous tip stays an
    ancestor of the new head, and `push_sha_fast_forward` can still land it
    fast-forward-only, no force anywhere. The merge runs base -> branch, on
    the task's own branch; it never touches `never_push_to` and never merges
    into main.

    So: no action past `should_rebase`'s gate returns ``None``. A truthy
    `remote_tip` (the branch has been pushed — see
    `GitRepo.fetch_remote_branch_sha`) returns ``"merge"``.

    Otherwise `remote_tip` is `None`, which `fetch_remote_branch_sha`
    returns for BOTH "never pushed" and "remote unreadable" (network/auth
    failure, timeout) — collapsed together by design, see that method's
    docstring. Those two cases must NOT be treated alike here: an earlier
    version of this function rebased on any falsy `remote_tip`, so a
    transient `ls-remote` timeout against an ALREADY-PUSHED branch chose
    rebase, mutually unreaching its own remote tip and reproducing the very
    non-ancestor delivery refusal this function exists to close — a fetch
    failure must fail OPEN to the safe action (merge is always safe: an
    extra merge commit on a branch that turns out to have never been pushed
    is harmless, whereas a wrongful rebase is not undoable). So `"rebase"`
    is returned ONLY when the caller can positively confirm, via
    `GitRepo.remote_branch_confirmed_absent`, that there is no such branch
    on the remote (`confirmed_never_pushed=True`); every other falsy-tip
    case — including "cannot tell" — returns ``"merge"``.
    """
    if not should_rebase(behind, threshold, overlap):
        return None
    if remote_tip:
        return "merge"
    return "rebase" if confirmed_never_pushed else "merge"


def staleness_record(
    behind: int, rebased: bool, overlap: Iterable[str],
    *, mode: str | None = None, merged: bool = False,
) -> dict:
    """The `task.context['base_staleness']` payload.

    ``commits_behind`` is CURRENT staleness, so bringing the branch up to
    date by EITHER path (a successful rebase OR a successful merge) makes it
    0: nothing remains behind `base` afterward. ``was_behind`` preserves the
    measurement that justified acting, which a prior review caught being
    lost to that post-rebase 0. ``overlapping_files`` records WHY a
    below-threshold gap was acted on, so the decision is auditable after the
    fact instead of being inferred from a commit count that did not reach
    the threshold.

    ``mode`` and ``merged`` are added ONLY when an action actually
    SUCCEEDED (``rebased or merged``). A no-op (below threshold, nothing to
    do) or a failed/conflicted attempt is already fully described by
    ``rebased=False`` — exactly as it was before this function learned about
    merging — so the record's shape for those cases is unchanged and every
    existing caller/test that pins it (positionally, or byte-for-byte) keeps
    working. Only the new, successful merge path — and the successful-rebase
    path, symmetrically — gains the extra detail.
    """
    shared = sorted(overlap)
    record = {
        "commits_behind": 0 if (rebased or merged) else behind,
        "was_behind": behind,
        "rebased": rebased,
        "overlapping_files": shared,
    }
    if rebased or merged:
        record["mode"] = mode
        record["merged"] = merged
    return record
