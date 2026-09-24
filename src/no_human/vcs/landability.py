"""Live landability probe — does this branch still merge into its CURRENT
base, asked fresh every time, never cached.

Bugfix context: `nh approve --ready` lists a task as "merge-ready" purely
from `core/merge_policy.py`'s six quality rules, stamped per HEAD sha
(`task.context["merge_policy"][<head sha>]`). That verdict correctly
invalidates when the BRANCH moves (a new head sha is a new dict key) and
never invalidates when the BASE moves — nothing in the six rules asks
whether the branch still merges into main. Every landing rewrites the
generated `RELEASE_MANIFEST.txt`, so every landing conflicts every other
open PR's branch against that file; the six-rule verdict stays "ready" while
`nh approve` would fail at the squash step. Twice in one day this sent an
operator into a failed `approve` -> send-back -> paid-attempt cycle (see the
task description this module closes).

This module answers the missing half — "will THIS branch merge into its
CURRENT base right now" — as a live, read-only, non-cached fact evaluated at
the moment `--ready` renders. It deliberately does NOT become a seventh
`merge_policy` rule: a rule's verdict is stamped per head sha and reused
until the head moves, which is exactly the caching bug that lets a landed
sibling PR go unnoticed. Landability must be asked fresh on every
`--ready`, so it lives beside the rules, not inside them.

Built entirely by reusing `vcs.derived_conflict` (`resolve_base_tip`,
`conflicting_paths`, `DERIVED_ARTEFACTS`) — no new `merge-tree` wrapper, no
signature changes there. This module owns only the base-ladder choice and
the four-state classification.

`DERIVED_ARTEFACTS` (`{"RELEASE_MANIFEST.txt"}`), not `derived_conflict.
mechanically_resolvable`'s wider eligible set, is what decides "derived":
`mechanically_resolvable` also accepts `EXPORT_CLASSIFICATION.txt`- and
`tests/test_structural_budget.py`-shaped conflicts, because that function
backs a DIFFERENT resolver (`resolve_derived_conflict`) that can actually
fix those up. `approve_merge.land_task`'s squash step (approve_merge.py
~1060) tolerates exactly one shape: `unmerged == {"RELEASE_MANIFEST.txt"}`
and nothing else — any other unmerged set, including a conflict that ALSO
touches `RELEASE_MANIFEST.txt`, refuses at `squash`. Classifying anything
wider than that singleton as "derived" here would make `--ready` render
`merge: clean` for a task `nh approve` still fails, which is the exact bug
this module exists to close — so "derived" must mirror `land_task`'s
narrower tolerance, not `mechanically_resolvable`'s broader one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .derived_conflict import (
    DERIVED_ARTEFACTS,
    conflicting_paths,
    resolve_base_tip,
)
from .git import GitError, GitRepo


@dataclass(frozen=True)
class Landability:
    """The live mergeability verdict for one branch against its current
    base, evaluated at the moment it was asked.

    state:
      "clean"    — merges into base with no conflicting paths.
      "derived"  — conflicts confined to exactly `DERIVED_ARTEFACTS`
                   (`{"RELEASE_MANIFEST.txt"}`), the ONLY shape
                   `land_task`'s squash step tolerates and regenerates at
                   land time (see
                   `docs/design/manifest-generated-file-conflicts.md`);
                   landable exactly like "clean". A conflict that also
                   touches any other path — including
                   `EXPORT_CLASSIFICATION.txt` or
                   `tests/test_structural_budget.py`, which a different
                   resolver can fix up but `land_task` cannot — is
                   "conflict", not "derived".
      "conflict" — conflicts a human must resolve (rebase) before landing.
      "unknown"  — the question could not be asked (no resolvable base, git
                   missing, timeout, unparseable output, or any unexpected
                   error). `nh approve --ready` does not count it as landable:
                   it reports it, and `--yes` skips it.
    """

    state: str
    base_ref: str
    base_sha: str
    conflicts: tuple[str, ...]
    detail: str


#: Base-name ladder, first name that RESOLVES wins (not first that is
#: merge-clean): `base_hint` (the task's own recorded `base_branch`) first,
#: then the repo's live default branch, then the two conventional names.
#: Mirrors `vcs/task_pr.py:classify_already_satisfied_landing` and the base
#: `approve_merge.land_task` actually squashes onto.
_FALLBACK_BASE_NAMES = ("main", "master")


def _default_branch_name(repo_path: str) -> str:
    """`GitRepo.default_branch(local_only=True)` — a local ref read only,
    never a network round trip (`--ready` must not block on an unreachable
    remote). Returns "" on anything that stops it (not a repo, no origin,
    detached) rather than raising; the base ladder just tries the next
    name."""
    try:
        repo = GitRepo(Path(repo_path))
    except (GitError, OSError, ValueError):
        return ""
    try:
        return repo.default_branch(local_only=True)
    except (GitError, OSError):
        return ""


async def _resolve_base(repo_path: str, base_hint: str) -> tuple[str, str]:
    """(base_ref name, resolved tip sha) for the first candidate name that
    resolves, or `("", "")` when none does."""
    candidates: list[str] = []
    if base_hint:
        candidates.append(base_hint)
    default = _default_branch_name(repo_path)
    if default:
        candidates.append(default)
    candidates.extend(_FALLBACK_BASE_NAMES)

    seen: set[str] = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        sha = await resolve_base_tip(repo_path, name)
        if sha:
            return name, sha
    return "", ""


async def check_landability(repo_path: str, branch: str, *,
                             base_hint: str = "") -> Landability:
    """Live, read-only, never-cached mergeability of `branch` into its
    current base. Never raises — any unexpected failure comes back as
    `state="unknown"`. Never mutates the repo: no ref is written, nothing is
    fetched (candidate resolution is local-only; the caller is expected to
    have already fetched, as `--ready`'s listing path already does before
    calling this).
    """
    try:
        if not repo_path or not branch:
            return Landability("unknown", "", "", (),
                                "no repo path or branch to check")

        base_ref, base_sha = await _resolve_base(repo_path, base_hint)
        if not base_sha:
            return Landability("unknown", "", "", (),
                                "no base branch could be resolved")

        paths = await conflicting_paths(repo_path, base_sha, branch)
        if paths is None:
            return Landability(
                "unknown", base_ref, base_sha, (),
                f"could not determine mergeability against {base_ref}")
        if not paths:
            return Landability("clean", base_ref, base_sha, (),
                                f"merges cleanly into {base_ref}")

        sorted_paths = tuple(sorted(paths))
        if paths <= DERIVED_ARTEFACTS:
            return Landability(
                "derived", base_ref, base_sha, sorted_paths,
                "conflict confined to derived artefact(s), regenerated at "
                "land time: " + ", ".join(sorted_paths))
        return Landability(
            "conflict", base_ref, base_sha, sorted_paths,
            f"conflicts with {base_ref} in " + ", ".join(sorted_paths))
    except Exception as exc:  # noqa: BLE001 — unknown state, see class docstring
        return Landability("unknown", "", "", (),
                            f"landability probe raised: {exc}")
