"""Mechanical resolution for a PR conflict confined to derived artefacts.

Bugfix context: `_check_pr_conflict` (blockers/wake.py) used to open a full
coder round for *every* CONFLICTING PR, including the case where the ONLY
conflicting path is a file that is regenerated from the tree — never
authored, never touched by a coder. A coder round can't fix that: it never
edits `RELEASE_MANIFEST.txt`, so the round burns an attempt (a real task,
8e153f1e, spent ~4.5M tokens across two such rounds that pushed zero files)
and the PR stays CONFLICTING. This module answers the two questions the rung
needs — "is this conflict confined to derived artefacts?" and, if so,
"resolve it" — without a coder in the loop.

The count-only shape rule below is judged against the MERGE-BASE, and against
the hunks that actually conflict — never against main's tip wholesale. See
`classification_count_only` and `conflict_hunks_count_only` for the incident
(task 63928824 / PR #592) that forced the distinction: a rule line main gained
from an unrelated landing merges cleanly and is not the branch's decision to
make, so it must not defeat the shape.

`EXPORT_CLASSIFICATION.txt` is NOT a derived artefact by this module's
membership rule (see `DERIVED_ARTEFACTS` below) even though it sits next to
`RELEASE_MANIFEST.txt` in the export gate: its per-rule win-COUNTs are
hand-maintained, not rebuilt by any command, so a conflict touching it still
needs a coder round exactly as before this module existed — UNLESS the
conflict's SHAPE, not just its filename, proves there is no hand decision in
it at all: every conflicting hunk in the file differs ONLY in a rule's
numeric win-count, never a pattern, a verb, a comment, or the line order
(`classification_count_only`). That shape means both sides independently
bumped the SAME rule for files each independently added — two reviewed
counts meeting, not two reviewed decisions colliding — and the correct
number is base + (branch - merge-base), written under exactly that equality
by `reconcile_merge_count_drift` (reused, never reimplemented here). This is
the same repair `reconcile_merge_count_drift` already made for a CLEAN merge
with a stale count (INCIDENT 2026-08-20, task c309a6a3); `mechanically_resolvable`
extends its use to the case where the count itself is what conflicts.

`resolve_derived_conflict` is synchronous (it shells out, like
`approve_merge.py`, which it reuses); the async watcher calls it via
`asyncio.to_thread`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .approve_merge import (
    _APPROVE_TIMEOUT_S,
    _VERIFY_TIMEOUT_S,
    _RULE_LINE_RE,
    CLASSIFICATION_NAME,
    COUNT_DRIFT_RE,
    _cap,
    _cleanup_worktree,
    _sh,
    _ship_classified_paths,
    reconcile_merge_count_drift,
)
from .git import GitError, GitRepo, ProtectedBranch
from .manifest_repair import _PRUNED_RE
from .pr_watcher import (
    _base_tips,
    _git_rc,
    classification_decisions,
    merge_tree_conflicts,
    refs_resolvable,
)

#: Repo-root paths that are REGENERATED FROM THE TREE, never authored: their
#: bytes are a pure function of the other files in the commit, so a merge
#: conflict in one of them carries no human decision to resolve — the only
#: correct resolution is to take either side and re-derive. MEMBERSHIP RULE:
#: a file belongs here IFF a documented command rebuilds ALL of its content
#: from the tree, with no hand-edit ever required or permitted.
#: `RELEASE_MANIFEST.txt` qualifies — it is one `<sha256>  <path>` pin per
#: shipped file, and `export_guard.py approve` rewrites every pin from the
#: working tree.
#:
#: `EXPORT_CLASSIFICATION.txt` does NOT qualify FOR MEMBERSHIP HERE, despite
#: sitting right next to the manifest in the same export gate: its rule lines
#: carry a hand-maintained win-COUNT (`ship 293  tests/*.py`), and no command
#: in `export_guard.py` re-tallies that count — `approve` rebuilds manifest
#: pins and nothing else, `verify` only checks the count against the tree and
#: REFUSES on a mismatch, it never repairs one. Taking `--ours` on a real
#: conflict there would, IN GENERAL, silently discard a hand decision (a
#: ship/drop flip, a new pattern) that only a coder round can make correctly
#: — the exact thing this module exists to avoid doing to genuinely derived
#: content. So a conflict touching this file — alone, or mixed with the
#: manifest — falls through to a coder round BY DEFAULT.
#:
#: The one exception is not a filename rule but a SHAPE rule, decided by
#: `classification_count_only` and applied by `mechanically_resolvable`: when
#: every conflicting hunk in the classification file differs ONLY in a rule's
#: numeric win-count (never a pattern, a verb, a comment, or the line order),
#: both sides made the identical decision and independently bumped the same
#: tally for files each added — that is merge arithmetic, not a hand
#: decision, and `reconcile_merge_count_drift` (reused, not reimplemented)
#: already makes exactly this repair for a cleanly-merged file. Taking
#: `--ours` is safe only because eligibility already proved both sides carry
#: identical decisions.
#:
#: Exact repo-root paths, never a glob or basename — `docs/RELEASE_MANIFEST.txt`
#: must NOT qualify (same doctrine as pr_watcher._GENERATED_LEDGERS). Adding a
#: second derived file is a one-line change HERE and nowhere else — but see
#: the membership rule above before adding one. `DERIVED_ARTEFACTS` itself
#: stays `RELEASE_MANIFEST.txt`-only; the classification file is admitted per
#: conflict, per `mechanically_resolvable`, never unconditionally.
DERIVED_ARTEFACTS = frozenset({"RELEASE_MANIFEST.txt"})


def _export_guard_argv() -> list[str]:
    """Base argv for invoking ``export_guard.py``. A module-level seam so
    tests can monkeypatch it to ``[sys.executable, "scripts/export_guard.py"]``
    without a `uv` dependency in the test sandbox, mirroring the documented
    ``uv run python scripts/export_guard.py`` invocation everywhere else."""
    return ["uv", "run", "python", "scripts/export_guard.py"]


def _inventory_argv() -> list[str]:
    """Base argv for invoking ``check_release_manifest.py`` — the manifest
    tool of repos WITHOUT ``scripts/export_guard.py`` (the public working
    repo): ``--write`` rebuilds every pin from the tracked tree, ``--strict``
    verifies. Same monkeypatch seam as ``_export_guard_argv``. Uses the
    running interpreter, not ``uv run``: the script is stdlib-only by its own
    contract, and ``uv run`` inside a resolver worktree would sync/claim a
    venv there for nothing. In a PyInstaller-frozen build ``sys.executable``
    is the ``nh`` binary, NOT a Python (the ``repro_gate._pytest_python``
    lesson) — fall back to a PATH interpreter there; any Python serves a
    stdlib-only script."""
    if getattr(sys, "frozen", False):
        py = shutil.which("python3") or shutil.which("python") or "python3"
        return [py, "scripts/check_release_manifest.py"]
    return [sys.executable, "scripts/check_release_manifest.py"]


async def resolve_base_tip(repo_path: str, base: str) -> str | None:
    """Resolve ``base`` (a branch name, e.g. the task's recorded
    ``base_branch``) to the concrete commit sha this repo should treat as the
    tip a PR targets — preferring the upstream remote-tracking tip over a
    possibly-stale local ref (see ``pr_watcher._base_tips``'s docstring for
    why), and returning ``None`` when neither resolves. Shared by
    `conflicting_paths` (which only needs to *ask* git a question) and
    `wake.py` (which needs a concrete sha to hand `resolve_derived_conflict`,
    a synchronous function that cannot itself await a git probe)."""
    if not repo_path or not base:
        return None
    if not await refs_resolvable(repo_path, base):
        return None
    tips = await _base_tips(repo_path, base)
    ref = tips[-1] if tips else base
    rc, sha = await _git_rc(repo_path, "rev-parse", "--verify", "--quiet",
                            f"{ref}^{{commit}}")
    return sha.strip() if rc == 0 and sha.strip() else None


async def conflicting_paths(repo_path: str, base_tip: str,
                            branch: str) -> set[str] | None:
    """The set of paths `git merge-tree` reports as conflicted for merging
    ``base_tip`` into ``branch``, or ``None`` when the question could not be
    asked at all (git missing, unresolvable refs, unparseable output) — a
    thin wrapper over `pr_watcher.merge_tree_conflicts` that resolves
    ``base_tip`` through `resolve_base_tip` first."""
    if not repo_path or not branch or not base_tip:
        return None
    if not await refs_resolvable(repo_path, branch):
        return None
    resolved_base = await resolve_base_tip(repo_path, base_tip)
    if resolved_base is None:
        return None
    result = await merge_tree_conflicts(repo_path, branch, resolved_base)
    if result is None:
        return None
    return result[1]


async def fetch_conflict_refs(repo_path: str, base: str, branch: str) -> bool:
    """Best-effort ``git fetch origin <base> <branch>`` — the common cause of
    an enumeration failure (`conflicting_paths` raising, or returning
    ``None``) is a stale/missing ref in the watcher's checkout. Returns
    whether the fetch succeeded; the caller retries the enumeration EITHER
    WAY (per the intake answer: a transient fetch failure must not
    short-circuit the retry).

    ``_git_rc`` already swallows `OSError` (git absent) and enforces
    `_GIT_TIMEOUT`, returning `rc=1` for both — so this never raises for
    those; it only additionally guards empty arguments.
    """
    if not repo_path or not base or not branch:
        return False
    rc, _ = await _git_rc(repo_path, "fetch", "--quiet", "origin", base, branch)
    return rc == 0


def all_derived(paths: set[str] | None) -> bool:
    """True iff `paths` is non-empty and every path in it is a derived
    artefact — the mechanical-resolution eligibility test. `None` (could not
    enumerate) and an empty set both read as "not eligible": the caller must
    never resolve a conflict it could not confirm is derived-only."""
    return bool(paths) and paths <= DERIVED_ARTEFACTS


def _normalized_classification_lines(text: str) -> list[str]:
    """`text` split into lines with every rule line's win-count digits
    replaced by a single `#` placeholder — everything else (verb, both
    spacing runs, pattern, and every non-rule line) kept byte-for-byte. Used
    only for the strict textual half of `classification_count_only`: two
    files whose rule lines match under this normalisation differ in COUNT
    ONLY, never in alignment/spacing — a whitespace reflow of the count
    changes `sp1`/`sp2` and so still compares unequal."""
    out: list[str] = []
    for line in text.splitlines():
        m = _RULE_LINE_RE.match(line)
        if m:
            out.append(f"{m['verb']}{m['sp1']}#{m['sp2']}{m['pattern']}")
        else:
            out.append(line)
    return out


async def classification_count_only(repo_path: str, base_tip_sha: str,
                                     branch: str) -> bool:
    """True iff the BRANCH'S OWN edit to `EXPORT_CLASSIFICATION.txt` — its
    change relative to the MERGE-BASE of `base_tip_sha` and `branch`, not
    relative to the base tip — is ONLY the numeric win-count of
    otherwise-identical rule lines: never a pattern, a verb, a comment, an
    added/removed/reordered rule line, or a whitespace reflow. This is a
    conflict-SHAPE test about one side; it says nothing about whether the
    file is currently a git conflict, nor about what the conflicting hunks
    contain (the caller, `mechanically_resolvable`, combines this with the
    conflicting-paths set and with `conflict_hunks_count_only`).

    WHY THE MERGE-BASE AND NOT THE BASE TIP (bugfix, live evidence: task
    63928824 / PR #592, 2026-08-22). This used to compare the base TIP
    against the branch wholesale. Every rule line main gained from an
    unrelated landing after the branch forked — a clean, non-conflicting
    addition that `git merge` takes without asking anyone — then made both
    checks below fail, so a conflict that was PURELY the two sides' count
    bumps meeting (321 -> 322 on the branch, 321 -> 323 on main, merging to
    324) was declared "not count-only" and sent to a paid coder round, which
    cannot author that number correctly anyway. Main's own decisions are
    main's; the only question this predicate may ask is what the BRANCH
    decided, and the merge-base is the only point that answers it.

    Two independent checks must both pass, between the merge-base and the
    branch:
      1. The DECISION sequence — `pr_watcher.classification_decisions`, which
         already elides each rule's count — is identical on both sides. Reused
         as-is, never reparsed here.
      2. A strict textual check (`_normalized_classification_lines`) that
         additionally requires identical line count, identical order, and
         identical whitespace everywhere except the count digits themselves —
         closing the hole check 1's `str.split()` leaves open for a pure
         spacing reflow.

    Fails closed (`False`) on any git failure, an UNRESOLVABLE MERGE-BASE
    (unrelated histories, a missing ref), an absent file, or an empty read on
    either side — the same doctrine as `all_derived`'s `None`-is-ineligible:
    an unknown conflict shape is never treated as count-only.
    """
    rc_mb, merge_base = await _git_rc(repo_path, "merge-base",
                                      base_tip_sha, branch)
    if rc_mb != 0 or not merge_base:
        return False

    decisions_base = await classification_decisions(repo_path, merge_base)
    decisions_branch = await classification_decisions(repo_path, branch)
    if decisions_base is None or decisions_branch is None:
        return False
    if decisions_base != decisions_branch:
        return False

    rc_base, text_base = await _git_rc(repo_path, "show",
                                        f"{merge_base}:{CLASSIFICATION_NAME}")
    rc_branch, text_branch = await _git_rc(repo_path, "show",
                                            f"{branch}:{CLASSIFICATION_NAME}")
    if rc_base != 0 or rc_branch != 0 or not text_base or not text_branch:
        return False

    return (_normalized_classification_lines(text_base)
            == _normalized_classification_lines(text_branch))


#: Conflict-marker prefixes git writes into a merged blob. `|||||||` appears
#: only under `merge.conflictStyle = diff3`/`zdiff3`, so both the 2- and
#: 3-section shapes have to parse.
_HUNK_START = "<<<<<<<"
_HUNK_BASE = "|||||||"
_HUNK_SEP = "======="
_HUNK_END = ">>>>>>>"


def conflict_hunks_count_only(merged_text: str) -> bool:
    """True iff `merged_text` — a classification file as git left it, WITH
    conflict markers — contains at least one conflict hunk and EVERY hunk's
    sections are rule lines that differ only in their win-count digits.

    The companion to `classification_count_only`, and the reason that
    predicate may look at the branch's edit alone. `classification_count_only`
    proves the branch decided nothing; this proves nothing was decided INSIDE
    the conflict either. Without it, a main-side rule line that happens to
    land beside the branch's count line (git folds adjacent edits into one
    hunk) would be resolved by `git checkout --ours` — silently discarding
    main's decision. With it, that hunk's two sides have different line
    counts and the whole conflict is refused to a coder round.

    Refuses (`False`), never guesses, on: no conflict markers at all (the file
    is not conflicted — an unknown, since eligibility said it was); a marker
    that appears outside a hunk; an unterminated or nested hunk; an empty
    section (one side deleted the rule — a decision); any non-rule line inside
    a hunk (a conflicting comment or blank is a hand edit); and any pair of
    sections that are not equal under `_normalized_classification_lines`.
    """
    lines = merged_text.splitlines()
    hunks = 0
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith(_HUNK_START):
            if line.startswith((_HUNK_BASE, _HUNK_SEP, _HUNK_END)):
                return False  # a marker outside a hunk — unparseable
            i += 1
            continue
        sections: list[list[str]] = [[]]
        i += 1
        closed = False
        while i < n:
            cur = lines[i]
            if cur.startswith(_HUNK_START):
                return False  # nested start — unparseable
            i += 1
            if cur.startswith((_HUNK_BASE, _HUNK_SEP)):
                sections.append([])
                continue
            if cur.startswith(_HUNK_END):
                closed = True
                break
            sections[-1].append(cur)
        if not closed or len(sections) not in (2, 3):
            return False
        if any(not section for section in sections):
            return False
        if any(not _RULE_LINE_RE.match(ln) for section in sections for ln in section):
            return False
        normalized = [_normalized_classification_lines("\n".join(section))
                      for section in sections]
        if any(other != normalized[0] for other in normalized[1:]):
            return False
        hunks += 1
    return hunks > 0


def take_ours_in_conflict_hunks(merged_text: str) -> str | None:
    """`merged_text` with every conflict hunk collapsed to its OURS section
    (and the markers removed), or `None` when the markers do not parse or
    there is no hunk at all.

    Why this and not `git checkout --ours -- <path>`: `--ours` restores the
    whole stage-2 BLOB — the branch's file as it was, discarding everything
    `git merge` had already merged into it cleanly. For `RELEASE_MANIFEST.txt`
    that is harmless (the next step regenerates every pin from the merged
    tree), but for `EXPORT_CLASSIFICATION.txt` it silently DROPS every rule
    line main gained since the fork, leaving files main added unclassified and
    failing the export gate on the next landing. Only the conflicting hunks
    may be resolved to ours; the rest of the merged text is main's, already
    merged, and stays.

    Safe only because the caller has already proved (`conflict_hunks_count_only`)
    that every hunk differs by nothing but a win-count digit — the number that
    survives here is then repaired by `reconcile_merge_count_drift`.
    """
    out: list[str] = []
    state = "clean"  # clean | ours | other
    hunks = 0
    for line in merged_text.splitlines(keepends=True):
        if line.startswith(_HUNK_START):
            if state != "clean":
                return None
            state, hunks = "ours", hunks + 1
            continue
        if line.startswith((_HUNK_BASE, _HUNK_SEP)):
            if state == "clean":
                return None
            state = "other"
            continue
        if line.startswith(_HUNK_END):
            if state == "clean":
                return None
            state = "clean"
            continue
        if state != "other":
            out.append(line)
    if state != "clean" or not hunks:
        return None
    return "".join(out)


async def classification_conflict_hunks_count_only(
        repo_path: str, base_tip_sha: str, branch: str) -> bool:
    """`conflict_hunks_count_only` applied to the classification file as the
    REAL three-way merge of `base_tip_sha` into `branch` leaves it.

    Reuses `pr_watcher.merge_tree_conflicts` — the same `git merge-tree
    --write-tree` call that enumerated the conflicting paths in the first
    place — and reads the conflicted blob out of the tree it wrote, so no
    worktree is created and nothing is checked out to ask this question.
    Fails closed (`False`) when the merge cannot be computed, when the
    classification file is not among the conflicted paths after all, or when
    the blob cannot be read.
    """
    result = await merge_tree_conflicts(repo_path, branch, base_tip_sha)
    if result is None:
        return False
    merged_tree, conflicted = result
    if CLASSIFICATION_NAME not in conflicted:
        return False
    rc, text = await _git_rc(repo_path, "show",
                             f"{merged_tree}:{CLASSIFICATION_NAME}")
    if rc != 0 or not text:
        return False
    return conflict_hunks_count_only(text)


async def mechanically_resolvable(repo_path: str, paths: set[str] | None,
                                  base_tip_sha: str,
                                  branch: str) -> frozenset[str] | None:
    """The eligible-for-mechanical-resolution artefact set for THIS conflict,
    or `None` when no mechanical resolution applies. `DERIVED_ARTEFACTS` for
    the existing manifest-only case — decided by `all_derived` alone, no new
    git calls, exactly the same hot path as before this function existed.
    `DERIVED_ARTEFACTS | {CLASSIFICATION_NAME}` when the conflict is confined
    to the classification file (alone, or together with the manifest) AND
    BOTH count-only checks pass: `classification_count_only` (the BRANCH's
    own edit against the merge-base is nothing but digits) and
    `classification_conflict_hunks_count_only` (the hunks git actually
    conflicts on are nothing but digits either). The pair is the whole test:
    the first alone would let a main-side decision that landed inside the
    conflicting hunk be resolved by `--ours`, and the second alone would let
    a branch-side decision that merged cleanly through. `None` on anything
    else, including `paths` itself being `None`/empty (could not enumerate) —
    fail closed, the caller must never resolve a conflict it could not
    confirm is one of these two shapes."""
    if not paths:
        return None
    if all_derived(paths):
        return DERIVED_ARTEFACTS
    eligible = DERIVED_ARTEFACTS | {CLASSIFICATION_NAME}
    if paths <= eligible and CLASSIFICATION_NAME in paths:
        if (await classification_count_only(repo_path, base_tip_sha, branch)
                and await classification_conflict_hunks_count_only(
                    repo_path, base_tip_sha, branch)):
            return eligible
    return None


@dataclass
class DerivedResolution:
    """Result of `resolve_derived_conflict`. `step` names where it stopped —
    one of "worktree", "merge", "regenerate", "verify", "commit", "push", or
    "ok" — so an escalation can name the failing step, never just "it
    failed"."""

    ok: bool
    step: str
    pushed_sha: str = ""
    unpinned: list[str] = field(default_factory=list)
    detail: str = ""
    #: Non-empty when a win-count in EXPORT_CLASSIFICATION.txt — a file this
    #: module otherwise never touches — was rewritten by merge arithmetic;
    #: the human gate must see that, so the caller puts it in the event.
    reconciled: str = ""
    #: Paths `export_guard.py approve --prune` dropped a stale pin for (a
    #: path that stopped shipping on one side of the merge) — the human gate
    #: must see that too, so the caller puts it in the event.
    pruned: list[str] = field(default_factory=list)


def _run_export_guard(worktree_path: Path, subargs: list[str], *,
                      timeout: float) -> subprocess.CompletedProcess | None:
    """Run `export_guard.py` with `subargs` in `worktree_path`; `None` means
    it timed out (the caller treats that as a failure, never as success)."""
    try:
        return _sh([*_export_guard_argv(), *subargs], cwd=worktree_path,
                   timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _run_inventory(worktree_path: Path, subargs: list[str], *,
                   timeout: float) -> subprocess.CompletedProcess | None:
    """Run `check_release_manifest.py` with `subargs` in `worktree_path`;
    `None` means it timed out OR could not be spawned at all (the caller
    treats both as a failure, never as success) — `_run_export_guard`'s
    contract plus the `OSError` arm, because on a frozen build with no
    Python on PATH the fallback argv's interpreter may not exist
    (`FileNotFoundError`), and that must fail the resolution closed, not
    crash the wake watcher (same pair `approve_merge.py` catches around
    `_sh`)."""
    try:
        return _sh([*_inventory_argv(), *subargs], cwd=worktree_path,
                   timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _unmerged_paths(worktree_path: Path) -> set[str]:
    """Paths `git ls-files -u` reports as still conflicted (any stage)."""
    out = _sh(["git", "ls-files", "-u", "-z"], cwd=worktree_path).stdout
    paths: set[str] = set()
    for field_ in out.split("\0"):
        if not field_:
            continue
        _meta, tab, path = field_.partition("\t")
        if tab and path:
            paths.add(path)
    return paths


def _parse_not_ship_classified(text: str) -> list[str]:
    """Extract the offending paths from an `export_guard.py approve` refusal
    of shape ``approve: REFUSED — not ship-classified (...):\\n  path1\\n
    path2`` (see `export_guard.py::_cmd_approve`) — each listed path is
    indented by exactly two spaces on its own line, directly after the
    header line. Returns `[]` when the text doesn't match that shape (the
    caller then treats the whole batch as an unretried failure)."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "REFUSED" in line and "not ship-classified" in line:
            start = i + 1
            break
    if start is None:
        return []
    found: list[str] = []
    for line in lines[start:]:
        if line.startswith("  ") and line.strip():
            found.append(line.strip())
        else:
            break
    return found


def _approve_argv(targets: list[str]) -> list[str]:
    """`export_guard.py approve` argv for `targets`, always with `--prune` so
    a pin for a path that stopped shipping on either side of the merge (e.g.
    deleted, or reclassified as drop) is dropped from RELEASE_MANIFEST.txt
    rather than surviving into a tree that then fails `verify` with "pinned
    but not shipped" — see the module docstring. Legal with `targets == []`:
    a prune-only pass, still worth running because a stale pin can exist even
    when the branch's own diff pins nothing new."""
    return ["approve", "--prune", *targets]


def resolve_derived_conflict(repo_path: str, branch: str, base_tip_sha: str,
                             remote: str = "origin", *,
                             eligible: frozenset[str] = DERIVED_ARTEFACTS,
                             ) -> DerivedResolution:
    """Mechanically resolve a PR conflict already confirmed (by the caller,
    via `all_derived` or `mechanically_resolvable`) to be confined to
    `eligible`: merge the base tip into a detached worktree of the branch,
    take either side of the eligible files and regenerate/reconcile them from
    the merged tree, verify, and push — no coder session. See the module
    docstring for why this exists and docs/PLAN.md's step-by-step for the
    exact procedure this implements.

    `eligible` defaults to `DERIVED_ARTEFACTS` (today's manifest-only case,
    unchanged behaviour); the caller passes
    `DERIVED_ARTEFACTS | {CLASSIFICATION_NAME}` when `mechanically_resolvable`
    confirmed the conflict is also eligible by the count-only shape rule.

    Never force-pushes; never pushes a tree that fails its backend's
    verifier (`export_guard.py verify`, or `check_release_manifest.py
    --strict` on the inventory backend). A failure at any step is reported
    via `DerivedResolution.ok = False` with `step`/`detail` naming what
    happened — the caller escalates, it never retries with force or weakens
    a gate.
    """
    root = Path(repo_path)
    guard = root / "scripts" / "export_guard.py"
    inventory = root / "scripts" / "check_release_manifest.py"
    if not guard.exists() and not inventory.exists():
        return DerivedResolution(
            ok=False, step="regenerate",
            detail=f"{root} has neither scripts/export_guard.py nor "
                   "scripts/check_release_manifest.py — not a manifest-"
                   "gated repo, mechanical resolution does not apply")
    if not guard.exists() and CLASSIFICATION_NAME in eligible:
        # The inventory backend has no classification arithmetic; a repo
        # without export_guard.py should never present this shape (its
        # `--write` refuses when a classification file exists), so fail
        # closed rather than guess.
        return DerivedResolution(
            ok=False, step="regenerate",
            detail=f"{CLASSIFICATION_NAME} is in the eligible set but {root} "
                   "has no scripts/export_guard.py — the inventory backend "
                   "cannot reconcile classification counts")

    try:
        repo = GitRepo(root)
    except GitError as exc:
        return DerivedResolution(ok=False, step="worktree", detail=_cap(str(exc)))

    branch_tip_sha = repo.resolve_commitish(branch)
    if not branch_tip_sha:
        return DerivedResolution(
            ok=False, step="worktree",
            detail=f"branch {branch!r} does not resolve to a commit")

    tmp_dir = Path(tempfile.mkdtemp(prefix="nh-derived-"))
    shutil.rmtree(tmp_dir, ignore_errors=True)  # add_worktree needs the name free
    try:
        try:
            repo.add_worktree(tmp_dir, base=branch_tip_sha, detach=True)
        except (GitError, ProtectedBranch) as exc:
            return DerivedResolution(ok=False, step="worktree", detail=_cap(str(exc)))

        return _resolve_in_worktree(
            repo=repo, worktree_path=tmp_dir, remote=remote, branch=branch,
            base_tip_sha=base_tip_sha, eligible=eligible,
        )
    finally:
        _cleanup_worktree(repo, tmp_dir)


def _take_classification_hunks(worktree_path: Path) -> DerivedResolution | None:
    """Resolve the conflicted `EXPORT_CLASSIFICATION.txt` in `worktree_path`
    to its OURS side HUNK BY HUNK and stage it. `None` on success; a failed
    `DerivedResolution` (which the caller returns after aborting the merge)
    when the conflicted file cannot be read, does not parse as conflict
    markers, or — the fail-closed re-check on the REAL merge, in case the base
    moved between enumeration and now — carries a hunk that is not count-only.
    """
    path = worktree_path / CLASSIFICATION_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return DerivedResolution(
            ok=False, step="merge",
            detail=_cap(f"cannot read the conflicted {CLASSIFICATION_NAME}: {exc}"))
    if not conflict_hunks_count_only(text):
        return DerivedResolution(
            ok=False, step="merge",
            detail=_cap(f"{CLASSIFICATION_NAME} conflict is not count-only in the "
                        "worktree merge (the base moved?) — a hand decision, "
                        "not merge arithmetic"))
    resolved = take_ours_in_conflict_hunks(text)
    if resolved is None:
        return DerivedResolution(
            ok=False, step="merge",
            detail=_cap(f"could not parse the conflict markers in "
                        f"{CLASSIFICATION_NAME}"))
    path.write_text(resolved, encoding="utf-8")
    add = _sh(["git", "add", "--", CLASSIFICATION_NAME], cwd=worktree_path)
    if add.returncode != 0:
        return DerivedResolution(ok=False, step="merge", detail=_cap(add.stderr))
    return None


def _inventory_resolve_tail(*, repo: GitRepo, worktree_path: Path, remote: str,
                            branch: str, branch_tip_sha: str,
                            ) -> DerivedResolution:
    """Steps 5-8 for the inventory backend (`check_release_manifest.py`,
    repos without `scripts/export_guard.py`): `--write` rebuilds EVERY pin
    from the merged tracked tree — no per-path approve/prune/count machinery
    exists or is needed — then commit, `--strict` verify (the same mode the
    inventory CI job runs), and the same plain-push/update-ref mechanics the
    export-guard tail documents. The caller already refused a
    CLASSIFICATION_NAME-eligible conflict for this backend, and `--write`
    itself exits 2 if a classification file appears (belt and braces).
    Called with the merge already committed/staged in `worktree_path` (after
    steps 3-4), so the tree `--write` hashes is the merged, resolved one."""
    write_proc = _run_inventory(
        worktree_path, ["--write"], timeout=_APPROVE_TIMEOUT_S)
    if write_proc is None:
        return DerivedResolution(
            ok=False, step="regenerate",
            detail=f"check_release_manifest --write timed out after "
                   f"{_APPROVE_TIMEOUT_S}s")
    if write_proc.returncode != 0:
        return DerivedResolution(
            ok=False, step="regenerate",
            detail=_cap("check_release_manifest --write failed "
                        f"({write_proc.returncode}):\n"
                        + write_proc.stdout + write_proc.stderr))

    # -- step 6 (inventory): commit — the export-guard tail's mechanics. -- #
    add = _sh(["git", "add", "--", "RELEASE_MANIFEST.txt"], cwd=worktree_path)
    if add.returncode != 0:
        return DerivedResolution(ok=False, step="regenerate",
                                 detail=_cap(add.stderr))
    status = _sh(["git", "status", "--porcelain"], cwd=worktree_path).stdout
    if status.strip():
        commit_proc = _sh(["git", "commit", "--no-edit"], cwd=worktree_path)
        if commit_proc.returncode != 0:
            return DerivedResolution(
                ok=False, step="commit",
                detail=_cap(commit_proc.stdout + "\n" + commit_proc.stderr))
    # else: `git merge` already auto-committed — HEAD is the merge commit.
    merge_sha = _sh(["git", "rev-parse", "HEAD"],
                    cwd=worktree_path).stdout.strip()

    # -- step 7 (inventory): verify BEFORE any push. `--strict` so a tracked
    # file with no row fails HERE, matching what the inventory CI job runs —
    # the no-flag run only warns on that class. No classification arithmetic
    # exists on this backend, so there is no count-drift backstop. --------- #
    verify_proc = _run_inventory(
        worktree_path, ["--strict"], timeout=_VERIFY_TIMEOUT_S)
    if verify_proc is None:
        return DerivedResolution(
            ok=False, step="verify",
            detail=f"check_release_manifest --strict timed out after "
                   f"{_VERIFY_TIMEOUT_S}s")
    if verify_proc.returncode != 0:
        return DerivedResolution(
            ok=False, step="verify",
            detail=_cap(verify_proc.stdout + verify_proc.stderr))

    # -- step 8 (inventory): plain push from the MAIN repo, exactly as the
    # export-guard tail documents (shared object database; never force). -- #
    push_proc = _sh(
        ["git", "push", remote, f"{merge_sha}:refs/heads/{branch}"],
        cwd=repo.path,
    )
    if push_proc.returncode != 0:
        return DerivedResolution(
            ok=False, step="push",
            detail=_cap(push_proc.stdout + "\n" + push_proc.stderr))
    _sh(["git", "update-ref", f"refs/heads/{branch}", merge_sha,
         branch_tip_sha], cwd=repo.path)
    return DerivedResolution(
        ok=True, step="ok", pushed_sha=merge_sha,
        detail=f"regenerated RELEASE_MANIFEST.txt whole-tree "
               f"(check_release_manifest --write) from the merged tree, "
               f"pushed {merge_sha[:8]}")


def _resolve_in_worktree(*, repo: GitRepo, worktree_path: Path, remote: str,
                         branch: str, base_tip_sha: str,
                         eligible: frozenset[str] = DERIVED_ARTEFACTS,
                         ) -> DerivedResolution:
    branch_tip_sha = _sh(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    # -- step 3: merge --------------------------------------------------- #
    # rc != 0 is expected (that is the conflict this whole module exists to
    # resolve); rc 0 with nothing left conflicted is also fine (someone else
    # resolved it between enumeration and now) — continue either way. But
    # never resolve a conflict this routine did not enumerate: if anything
    # OUTSIDE `eligible` is still conflicted (the base moved in a way that
    # changed the shape of the conflict), bail — a coder round handles that,
    # not this one.
    _sh(["git", "merge", "--no-edit", base_tip_sha], cwd=worktree_path)
    unmerged = _unmerged_paths(worktree_path)
    outside = unmerged - eligible
    if outside:
        _sh(["git", "merge", "--abort"], cwd=worktree_path)
        return DerivedResolution(
            ok=False, step="merge",
            detail=_cap("merge produced conflict(s) outside the derived set "
                        f"(base moved?): {sorted(outside)}"))

    # -- step 4: take either side of the eligible files. For the manifest
    # this is always safe (it is purely regenerated below). For the
    # classification file it is safe ONLY because eligibility already proved
    # (via `classification_count_only`) that the branch decided nothing, and
    # only the CONFLICTING HUNKS may be taken — the rest of the merged text
    # carries the rule lines main gained since the fork, which `--ours` would
    # discard (see `take_ours_in_conflict_hunks`). The win-count that survives
    # gets repaired by merge arithmetic further down, never guessed here. --
    for path in sorted(unmerged & eligible):
        if path == CLASSIFICATION_NAME:
            failed = _take_classification_hunks(worktree_path)
            if failed is not None:
                _sh(["git", "merge", "--abort"], cwd=worktree_path)
                return failed
            continue
        co = _sh(["git", "checkout", "--ours", "--", path], cwd=worktree_path)
        if co.returncode != 0:
            _sh(["git", "merge", "--abort"], cwd=worktree_path)
            return DerivedResolution(ok=False, step="merge", detail=_cap(co.stderr))
        add = _sh(["git", "add", "--", path], cwd=worktree_path)
        if add.returncode != 0:
            _sh(["git", "merge", "--abort"], cwd=worktree_path)
            return DerivedResolution(ok=False, step="merge", detail=_cap(add.stderr))

    # -- steps 5-8 (inventory backend): a repo without export_guard.py
    # carries the whole-tree inventory tool instead
    # (`check_release_manifest.py`, the public working repo) — dispatched to
    # its own tail; the export-guard tail below is untouched.
    if not (worktree_path / "scripts" / "export_guard.py").exists():
        return _inventory_resolve_tail(
            repo=repo, worktree_path=worktree_path, remote=remote,
            branch=branch, branch_tip_sha=branch_tip_sha)

    # -- step 5: regenerate the pins for what the branch actually changed - #
    # No --diff-filter here: a path base added/changed that the (pre-merge)
    # branch tip never had reads as "deleted" in this diff direction and
    # would be dropped by --diff-filter=d, even though it merged in cleanly
    # from base and needs its pin. Keep every name diff reports (including
    # D) — a path genuinely absent from the merged tree is filtered out
    # below by `_ship_classified_paths`, which checks `git ls-files` on the
    # current (post-merge) worktree, so this is safe either way.
    diff_proc = _sh(
        ["git", "diff", "--name-only", f"{base_tip_sha}..{branch_tip_sha}"],
        cwd=worktree_path,
    )
    if diff_proc.returncode != 0:
        return DerivedResolution(ok=False, step="regenerate", detail=_cap(diff_proc.stderr))
    changed = sorted({
        p.strip() for p in diff_proc.stdout.splitlines()
        if p.strip() and p.strip() not in eligible
    })
    shipped_changed = _ship_classified_paths(worktree_path, changed)
    unpinned = sorted(set(changed) - set(shipped_changed))
    reconciled = ""
    pruned_paths: list[str] = []

    # Always run an approve pass — even when `shipped_changed` is empty — so
    # a pin for a path that stopped shipping on either side of the merge
    # (deleted, or reclassified as drop) is pruned via `--prune` before
    # step-7 `verify` runs. Without this, a finished PR whose ONLY conflict
    # is the manifest can still escalate on a stale pin `verify` correctly
    # refuses (see `_approve_argv`).
    if shipped_changed:
        _sh(["git", "add", "-A", "--", *shipped_changed], cwd=worktree_path)
    approve_proc = _run_export_guard(
        worktree_path, _approve_argv(shipped_changed), timeout=_APPROVE_TIMEOUT_S)
    if approve_proc is None:
        return DerivedResolution(
            ok=False, step="regenerate", unpinned=unpinned,
            detail=f"export_guard approve timed out after {_APPROVE_TIMEOUT_S}s")

    if approve_proc.returncode == 2 and COUNT_DRIFT_RE.search(
            approve_proc.stdout + approve_proc.stderr):
        ok, note = reconcile_merge_count_drift(
            worktree_path, base_tip_sha, branch_tip_sha,
            approve_proc.stdout + approve_proc.stderr)
        if not ok:
            return DerivedResolution(
                ok=False, step="regenerate", unpinned=unpinned,
                detail=_cap(f"{CLASSIFICATION_NAME} count drift is not merge "
                            f"arithmetic ({note}):\n"
                            + approve_proc.stdout + approve_proc.stderr))
        reconciled = note
        # Wherever a repo SHIPS its classification file it is pinned, and
        # the rewrite stales that pin — re-pin it or step-7 verify refuses.
        # (This repo drops the file, so here it is a no-op; the land
        # fixture ships it and covers the path.)
        retry_targets = list(dict.fromkeys(
            [*shipped_changed,
             *_ship_classified_paths(worktree_path, [CLASSIFICATION_NAME])]))
        approve_proc = _run_export_guard(
            worktree_path, _approve_argv(retry_targets), timeout=_APPROVE_TIMEOUT_S)
        if approve_proc is None:
            return DerivedResolution(
                ok=False, step="regenerate", unpinned=unpinned,
                detail=f"export_guard approve timed out after {_APPROVE_TIMEOUT_S}s "
                       f"(after count reconcile: {note})")

    if approve_proc.returncode == 2:
        combined = approve_proc.stdout + approve_proc.stderr
        refused = _parse_not_ship_classified(combined)
        if refused:
            # Belt-and-braces: a drop-classified path in the branch's own
            # diff — not a failure, just nothing to pin. Retry with those
            # paths removed — always, even if nothing is left (a prune-only
            # pass is still worth running).
            unpinned = sorted(set(unpinned) | set(refused))
            retry_targets = [p for p in shipped_changed if p not in refused]
            approve_proc = _run_export_guard(
                worktree_path, _approve_argv(retry_targets),
                timeout=_APPROVE_TIMEOUT_S)
            if approve_proc is None:
                return DerivedResolution(
                    ok=False, step="regenerate", unpinned=unpinned,
                    detail="export_guard approve timed out after "
                           f"{_APPROVE_TIMEOUT_S}s (retry)")

    if approve_proc is not None and approve_proc.returncode != 0:
        # exit 1 (scan-hit refusal) is never retried.
        why = ("scan-hit refusal" if approve_proc.returncode == 1
               else "refused before writing pins")
        return DerivedResolution(
            ok=False, step="regenerate", unpinned=unpinned,
            detail=_cap(f"export_guard approve {why} "
                        f"({approve_proc.returncode}):\n"
                        + approve_proc.stdout + approve_proc.stderr))

    if approve_proc is not None:
        pruned_paths.extend(
            _PRUNED_RE.findall(approve_proc.stdout + approve_proc.stderr))

    # -- step 6: commit --------------------------------------------------- #
    names_to_add = set(DERIVED_ARTEFACTS)
    if CLASSIFICATION_NAME in eligible:
        names_to_add.add(CLASSIFICATION_NAME)
    for name in sorted(names_to_add):
        if (worktree_path / name).exists():
            add = _sh(["git", "add", "--", name], cwd=worktree_path)
            if add.returncode != 0:
                return DerivedResolution(ok=False, step="regenerate",
                                          unpinned=unpinned, detail=_cap(add.stderr))

    status = _sh(["git", "status", "--porcelain"], cwd=worktree_path).stdout
    if status.strip():
        commit_proc = _sh(["git", "commit", "--no-edit"], cwd=worktree_path)
        if commit_proc.returncode != 0:
            return DerivedResolution(
                ok=False, step="commit", unpinned=unpinned,
                detail=_cap(commit_proc.stdout + "\n" + commit_proc.stderr))
    # else: `git merge` above already auto-committed (no conflicts, nothing
    # left to stage) — HEAD is already the merge commit.
    merge_sha = _sh(["git", "rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()

    # -- step 7: verify BEFORE any push ------------------------------------ #
    verify_proc = _run_export_guard(worktree_path, ["verify"], timeout=_VERIFY_TIMEOUT_S)
    if verify_proc is None:
        return DerivedResolution(
            ok=False, step="verify", unpinned=unpinned,
            detail=f"export_guard verify timed out after {_VERIFY_TIMEOUT_S}s")
    if verify_proc.returncode != 0:
        combined = verify_proc.stdout + verify_proc.stderr
        # Backstop reconcile hop: this fires whenever step 5 never caught a
        # still-outstanding count drift — either because the classification
        # file was ITSELF a conflicted path and step 4 took `--ours` on it
        # (declared count now stale), or because the conflict was
        # manifest-only so `shipped_changed` never included any path that
        # would have surfaced the drift there (e.g. a clean auto-merge of
        # EXPORT_CLASSIFICATION.txt that is nonetheless not merge-arithmetic
        # correct against the regenerated tree). Not gated on
        # `CLASSIFICATION_NAME in eligible` — a manifest-only conflict is
        # eligible too, and a finished PR must not escalate just because the
        # drift was only ever detectable here. `verify` is the last gate
        # before push, so it is the last place left to catch a count drift
        # that is still merge arithmetic. Bounded to exactly one pass —
        # `reconciled` guards against looping.
        if not reconciled and COUNT_DRIFT_RE.search(combined):
            ok, note = reconcile_merge_count_drift(
                worktree_path, base_tip_sha, branch_tip_sha, combined)
            if not ok:
                return DerivedResolution(
                    ok=False, step="verify", unpinned=unpinned,
                    detail=_cap(f"{CLASSIFICATION_NAME} count drift is not merge "
                                f"arithmetic ({note}):\n{combined}"))
            reconciled = note
            retry_targets = _ship_classified_paths(worktree_path, [CLASSIFICATION_NAME])
            if retry_targets:
                approve_proc = _run_export_guard(
                    worktree_path, _approve_argv(retry_targets), timeout=_APPROVE_TIMEOUT_S)
                if approve_proc is None:
                    return DerivedResolution(
                        ok=False, step="regenerate", unpinned=unpinned,
                        detail=f"export_guard approve timed out after "
                               f"{_APPROVE_TIMEOUT_S}s (after post-verify count "
                               f"reconcile: {note})")
                if approve_proc.returncode != 0:
                    return DerivedResolution(
                        ok=False, step="regenerate", unpinned=unpinned,
                        detail=_cap(f"export_guard approve refused after post-"
                                    f"verify count reconcile "
                                    f"({approve_proc.returncode}):\n"
                                    + approve_proc.stdout + approve_proc.stderr))
                pruned_paths.extend(
                    _PRUNED_RE.findall(approve_proc.stdout + approve_proc.stderr))
            add = _sh(["git", "add", "--", CLASSIFICATION_NAME], cwd=worktree_path)
            if add.returncode != 0:
                return DerivedResolution(ok=False, step="verify", unpinned=unpinned,
                                          detail=_cap(add.stderr))
            status = _sh(["git", "status", "--porcelain"], cwd=worktree_path).stdout
            if status.strip():
                amend = _sh(["git", "commit", "--amend", "--no-edit"], cwd=worktree_path)
                if amend.returncode != 0:
                    return DerivedResolution(
                        ok=False, step="commit", unpinned=unpinned,
                        detail=_cap(amend.stdout + "\n" + amend.stderr))
                merge_sha = _sh(["git", "rev-parse", "HEAD"],
                                 cwd=worktree_path).stdout.strip()
            verify_proc = _run_export_guard(worktree_path, ["verify"],
                                             timeout=_VERIFY_TIMEOUT_S)
            if verify_proc is None:
                return DerivedResolution(
                    ok=False, step="verify", unpinned=unpinned,
                    detail=f"export_guard verify timed out after "
                           f"{_VERIFY_TIMEOUT_S}s (after post-verify count "
                           f"reconcile: {note})")
            combined = verify_proc.stdout + verify_proc.stderr
        if verify_proc.returncode != 0:
            return DerivedResolution(
                ok=False, step="verify", unpinned=unpinned,
                detail=_cap(combined))

    # -- step 8: push from the MAIN repo, not the worktree ----------------- #
    # `add_worktree` installs a pre-push guard (push_hook.py) there that
    # refuses any push matching `never_push_to` — the second enforcement
    # point (approve_merge.py documents the same reasoning for `land_task`).
    # This routine only ever pushes the PR's OWN feature branch, but pushing
    # from the main repo is what makes the push land at all: both share one
    # object database, so the sha created in the worktree is already visible
    # here. Plain (non-force) push only — the merge commit is a fast-forward
    # descendant of the branch tip.
    push_proc = _sh(
        ["git", "push", remote, f"{merge_sha}:refs/heads/{branch}"],
        cwd=repo.path,
    )
    if push_proc.returncode != 0:
        return DerivedResolution(
            ok=False, step="push", unpinned=unpinned,
            detail=_cap(push_proc.stdout + "\n" + push_proc.stderr))

    # Compare-and-swap the local branch ref so it isn't left stale; best
    # effort only — a failure here doesn't undo an already-successful push.
    _sh(["git", "update-ref", f"refs/heads/{branch}", merge_sha, branch_tip_sha],
        cwd=repo.path)

    return DerivedResolution(
        ok=True, step="ok", pushed_sha=merge_sha, unpinned=unpinned,
        reconciled=reconciled, pruned=pruned_paths,
        detail=f"regenerated derived artefact(s) from the merged tree, "
               f"pushed {merge_sha[:8]}"
               + (f"; unpinned (drop-classified): {', '.join(unpinned)}"
                  if unpinned else "")
               + (f"; {CLASSIFICATION_NAME} count reconciled by merge "
                  f"arithmetic: {reconciled}" if reconciled else "")
               + (f"; pruned stale pin(s): {', '.join(pruned_paths)}"
                  if pruned_paths else ""),
    )
