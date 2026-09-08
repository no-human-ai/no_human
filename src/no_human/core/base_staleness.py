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


def staleness_record(
    behind: int, rebased: bool, overlap: Iterable[str],
) -> dict:
    """The `task.context['base_staleness']` payload.

    ``commits_behind`` is CURRENT staleness, so a successful rebase makes it
    0: the rebase replayed every local commit on top of the base and nothing
    remains behind it. ``was_behind`` preserves the measurement that justified
    acting, which a prior review caught being lost to that post-rebase 0.
    ``overlapping_files`` records WHY a below-threshold gap was acted on, so
    the decision is auditable after the fact instead of being inferred from a
    commit count that did not reach the threshold.
    """
    shared = sorted(overlap)
    return {
        "commits_behind": 0 if rebased else behind,
        "was_behind": behind,
        "rebased": rebased,
        "overlapping_files": shared,
    }
