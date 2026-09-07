"""Repair a manifest pre-commit refusal by running the gate's own FIX.

This lives in its OWN module deliberately: the egress allowlist can only
charge a dynamic exec (`sys.executable` on a runtime path) to a whole file,
and parking that wildcard on ``vcs/git.py`` would blind the gate to any
future dynamic exec added there (review finding F1, 2026-08-12). Here the
wildcard covers ~40 lines that do nothing else.

2026-08-11 incident: three tasks finished their work, passed their tests,
then died as ``task_crashed`` because the manifest pre-commit gate refused
the pipeline's own commit (changed pinned files, manifest not re-approved)
and the raw ``GitError`` propagated uncaught.

2026-08-12 incident (task 27e7352b): the fix above only *reacts* to the
pre-commit gate's refusal, and that gate is deliberately narrow — it blocks
an already-PINNED file whose content changed, never a brand-new
ship-classified file with no pin yet (``scripts/precommit_manifest_gate.py``
says so in its own docstring). A new file sails straight through it and
dies later, unpinned, in TESTING (``tests/test_pr_body_layout.py`` shipped
with no approval row). So there are now two steps, in order:

1. PROACTIVE, runs before every commit attempt. ``approve_pending_pins``
   performs the guard's own ``approve --all --prune`` maintenance on the
   PRIVATE tree so any new-or-changed ship-classified file is pinned *in
   the same commit* as the file it pins. ``write_pending_manifest`` is that
   shape's PUBLIC-tree sibling (below): when *paths* names files, it
   stages them — new files included, plus a non-code sibling deliverable
   sitting in a directory the same commit is newly creating — then runs
   ``check_release_manifest.py --write``, so a brand-new file is TRACKED
   before the gate's own ``git ls-files`` scan ever looks for it; when
   *paths* is falsy (a coder that wrote everything via Bash, with nothing
   for the caller to name individually) it stages the whole tree instead
   (``stage_all``) before the same ``--write``. Neither ever raises: a
   refusal here is advisory only — the file stays unpinned and fails
   later, honestly, by name.
2. ``commit_with_manifest_repair`` — REACTIVE fallback: if the pre-commit
   gate still refuses (an already-pinned file the proactive pass could not
   or would not touch, e.g. a scan-refused approval, or a proactive
   ``--write`` that itself failed or timed out), perform the gate's own
   documented FIX for that one refusal and retry once.

Two repo shapes, one gate
--------------------------
``scripts/precommit_manifest_gate.py`` ships to both trees this codebase
runs in, and its own ``remedy_command`` already branches on which one a
commit is running in (see ``CONTRIBUTING.md`` §"Optional: the pre-commit
manifest gate"):

* the PRIVATE tree carries ``scripts/export_guard.py`` and
  ``EXPORT_CLASSIFICATION.txt``; the documented FIX is
  ``export_guard.py approve <path>`` — hash maintenance for a file already
  on the ship ledger, gated by the guard's own term scan. This is what
  both routines above do when ``export_guard.py`` exists.
* the PUBLIC tree (no ``export_guard.py``, no classification — every
  tracked file ships) has no ledger to consult, so the documented FIX is
  wholesale regeneration: ``check_release_manifest.py --write``. Both
  reactive dogfood incidents on this repo's own public tree (before this
  module existed) died on a changed-but-pinned file: the regenerated
  manifest was never staged in the same commit as the coder's edited
  ``paths``. A THIRD dogfood shape (task 2d30b000, PR #122 head 8ecb120e,
  PR for 0e1edabb head c68ee842) died differently: a coder ADDED new files
  and ran ``--write`` from the shell before ``git add``-ing them — since
  ``--write`` regenerates the manifest from ``git ls-files`` (the INDEX,
  not the working tree), the untracked new files were invisible to it and
  shipped with no manifest row at all, failing CI's "File inventory" job.
  ``write_pending_manifest`` closes that gap PROACTIVELY, above: it stages
  *paths* (new files included) before ever invoking ``--write``, falls
  back to staging the whole tree when *paths* is falsy so a Bash-only
  coder is covered too, and also stages a non-code sibling deliverable
  sitting in a directory the same commit is newly creating — the same
  "new directory, no code extension" shape ``GitRepo.commit_paths``
  already special-cases for its own untracked sweep (independent review,
  2026-09-07: both gaps were found still live on this module's first cut).
  ``commit_with_manifest_repair`` still runs ``--write`` reactively (with
  the manifest appended to ``paths``) as the fallback for whatever the
  proactive pass could not or would not fix.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .approve_merge import COUNT_DRIFT_RE, reconcile_commit_count_drift
from .git import CommitResult, GitError, GitRepo, _branch_protected

_logger = logging.getLogger(__name__)

# The manifest pre-commit gate's refusal header
# (scripts/precommit_manifest_gate.py). Matching on this exact marker keeps
# the repair path from firing on any other hook's failure.
_MANIFEST_REFUSAL_MARKER = "no_human pre-commit gate: REFUSED"

# One refused file in the gate's output: the path on its own indented line,
# the pin/staged hash pair on the next. Only the changed-pinned-file shape
# matches — an unclassified/unknown file is a ledger DECISION the pipeline
# must never make on its own (export_guard's approve refuses those anyway;
# verified against the real guard in review). Paths may contain spaces
# (`\S.*\S|\S`), though no shipped path currently does.
_REFUSED_PIN_RE = re.compile(r"^\s{2}(\S.*\S|\S)\n\s+pinned [0-9a-f]", re.MULTILINE)

# A bounded ceiling for the approve run: it re-scans each refused file
# locally (~0.24s/file measured) and dials nothing; minutes means wedged.
_APPROVE_TIMEOUT_S = 120

# `--all` re-hashes every ship-classified file that is new or changed since
# its last pin — a bigger sweep than the reactive path's named-file re-scan,
# so it gets a longer, but still bounded, ceiling.
_PREAPPROVE_TIMEOUT_S = 300

# `--write` (the PUBLIC-tree route, no export_guard.py) re-hashes every
# tracked file locally from `git ls-files` + working-tree bytes and dials
# nothing — same shape of work as the reactive approve path above, so the
# same bounded ceiling.
_MANIFEST_WRITE_TIMEOUT_S = _APPROVE_TIMEOUT_S

# scripts/export_guard.py's own output lines for a successful pin/prune
# (`_cmd_approve`: `print(f"approved  {digest[:12]}  {rel} ({state})")` and
# `print(f"pruned    {rel} (no longer ships)")`) — matched here rather than
# re-parsed from a second grammar, so this stays in lockstep with whatever
# the guard actually printed.
_APPROVED_RE = re.compile(r"^approved\s+[0-9a-f]+\s+(\S.*\S|\S) \(", re.MULTILINE)
_PRUNED_RE = re.compile(r"^pruned\s+(\S.*\S|\S) \(", re.MULTILINE)


def _stage_untracked_for_approve(repo: GitRepo, paths: list[str] | None) -> None:
    """Make brand-new files TRACKED before the guard looks at them.

    ``export_guard.py`` classifies its ship-set from ``git ls-files`` — the
    INDEX, not the working tree — and documents that an untracked file is
    invisible to it. ``approve_pending_pins`` runs before the commit that
    would normally stage a new file for the first time, so without this a
    brand-new ship-classified file sails through ``--all`` unpinned: the
    exact tests/test_pr_body_layout.py incident (task 27e7352b).

    This performs only the ADD half of what ``GitRepo.commit_paths`` /
    ``commit_all`` (git.py) already do at commit time — the same files,
    staged a little earlier so the guard can see them too. It is never a new
    decision about what gets committed: every path added here is one
    ``commit_paths``'s own sweep would have staged anyway a few lines later.
    """
    if not paths:
        repo.stage_all()
        return
    root = repo.path
    rel_paths: list[str] = []
    for p in paths:
        try:
            rel_paths.append(str(Path(p).resolve().relative_to(root)))
        except ValueError:
            continue  # outside the repo — commit_paths will skip it too
    untracked = repo._run(
        "ls-files", "--others", "--exclude-standard", check=False
    ).splitlines()
    for u in untracked:
        u = u.strip()
        if u and Path(u).suffix.lower() in GitRepo._CODE_EXTS:
            rel_paths.append(u)
    rel_paths = [r for r in dict.fromkeys(rel_paths)
                 if r and not GitRepo._is_ephemeral_path(r)]
    if rel_paths:
        repo._run("add", "--", *rel_paths, check=False)


def approve_pending_pins(
    repo: GitRepo,
    paths: list[str] | None,
    on_repair: Callable[[list[str], str], None] | None = None,
) -> None:
    """Proactively pin new/changed ship-classified files before the commit.

    Runs the guard's own documented maintenance command,
    ``export_guard.py approve --all --prune`` — never ``--acknowledge``:
    that flag is what would turn hash maintenance into leak laundering, and
    granting it is a ledger DECISION no pipeline may make on its own, the
    same doctrine as the reactive repair's unclassified refusal below.

    A no-op (no subprocess spawned) unless the target repo has the gate
    (``scripts/export_guard.py`` and ``RELEASE_MANIFEST.txt`` both exist),
    the working tree actually has changes to maintain, and the current
    branch is not protected (nothing here should touch the index on a
    branch the commit itself refuses to land on). On the PUBLIC repo shape
    (no ``export_guard.py``) this stays a no-op — ``write_pending_manifest``,
    below, is that shape's own proactive equivalent (stage *paths*, then
    ``check_release_manifest.py --write``); the reactive repair remains the
    fallback for whichever shape's proactive pass could not or would not fix
    a refusal.

    Exit-code contract (``scripts/export_guard.py:_cmd_approve``), because
    this *is* the integration surface:

    * ``0`` — pins written for exactly the files whose content diverged,
      plus any pruned rows. Parsed and reported through ``on_repair`` so the
      ledger change is never silent (review finding: a prior version wrote
      the manifest on a run that ended non-zero without ever calling back).
    * ``1`` — one or more files refused on scan hits (a real leak
      candidate). Advisory only: never raise, retry, or acknowledge. But the
      files that WERE cleanly approved in the very same run still get
      reported through ``on_repair`` — that write already happened, and the
      only way to keep it off the ledger record would be to hide it. The
      refused file stays unpinned and fails the export tests by name later;
      the gate stays closed.
    * ``2`` — refused before any pin was written (unclassified file, a
      classification count mismatch, or the advisory scan unable to run).
      Nothing changed, so there is nothing to report through ``on_repair``;
      the guard's own FIX text is logged so it stays visible. Classification
      is a ledger DECISION; no pipeline makes it — same doctrine as the
      reactive repair's unclassified refusal. **Exception:** a declared
      win-COUNT drift that this attempt's own diff fully explains is not a
      hand decision — it is mechanically reconciled by the same
      ``_try_reconcile_count_drift`` the reactive path uses, and
      ``--all --prune`` is re-run once so the pin maintenance completes in
      the same commit.
    * ``TimeoutExpired`` / ``OSError`` — logged, never fails a commit that
      would otherwise succeed.
    """
    root = Path(repo.path)
    guard = root / "scripts" / "export_guard.py"
    if not guard.exists() or not (root / "RELEASE_MANIFEST.txt").exists():
        return
    if _branch_protected(repo.current_branch(), repo.never_push_to):
        return
    if not repo.has_changes():
        return

    _stage_untracked_for_approve(repo, paths)

    def _run_approve_all() -> subprocess.CompletedProcess | None:
        # None == timeout/OSError, already logged; caller must not raise.
        try:
            return subprocess.run(
                [sys.executable, str(guard), "approve", "--all", "--prune"],
                cwd=repo.path, capture_output=True, text=True,
                timeout=_PREAPPROVE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            _logger.warning(
                "manifest pin maintenance timed out after %ss; continuing "
                "without it", _PREAPPROVE_TIMEOUT_S)
            return None
        except OSError as exc:
            _logger.warning("manifest pin maintenance failed to start: %s", exc)
            return None

    proc = _run_approve_all()
    if proc is None:
        return

    out = proc.stdout or ""
    approved = _APPROVED_RE.findall(out)
    pruned = _PRUNED_RE.findall(out)
    tail = (out.strip() + "\n" + (proc.stderr or "").strip()).strip()[:1000]

    if proc.returncode == 0:
        if (approved or pruned) and on_repair is not None:
            on_repair(approved, "pre-commit pin maintenance: " + tail[:500])
        return

    if proc.returncode == 1:
        _logger.warning(
            "manifest pin maintenance: %s file(s) refused on scan hits:\n%s",
            out.count("REFUSED"), tail)
        if (approved or pruned) and on_repair is not None:
            on_repair(
                approved,
                "pre-commit pin maintenance (partial — some file(s) refused "
                "on scan hits): " + tail[:500],
            )
        return

    # 2 (or anything else the guard might exit with): refused before any pin
    # was written. Nothing changed — UNLESS the refusal is a declared
    # win-COUNT drift that this attempt's own diff fully explains, which is
    # not a hand decision and is reconciled by the same helper the reactive
    # path (commit_with_manifest_repair, below) uses.
    refusal_text = (proc.stderr or "") + (proc.stdout or "")
    if COUNT_DRIFT_RE.search(refusal_text):
        reconciled, note = _try_reconcile_count_drift(repo, refusal_text)
        if reconciled:
            # `_try_reconcile_count_drift` already rewrote AND staged
            # EXPORT_CLASSIFICATION.txt at this point — that ledger change
            # must reach `on_repair` regardless of what the retry below
            # approves or prunes (e.g. a `drop`-only drift leaves nothing
            # for `--all` to re-pin). Gating the report on the retry's own
            # output would let the staged rewrite ride into the commit
            # unreported.
            reconciled_note = f"count drift reconciled: {note}"
            retry = _run_approve_all()
            if retry is None:
                if on_repair is not None:
                    on_repair([], reconciled_note)
                return
            r_out = retry.stdout or ""
            r_approved = _APPROVED_RE.findall(r_out)
            r_pruned = _PRUNED_RE.findall(r_out)
            r_tail = (r_out.strip() + "\n" + (retry.stderr or "").strip()).strip()[:1000]
            if retry.returncode == 0:
                if on_repair is not None:
                    on_repair(r_approved, f"{reconciled_note}; " + r_tail[:500])
                return
            if retry.returncode == 1:
                _logger.warning(
                    "manifest pin maintenance: %s file(s) refused on scan "
                    "hits after a count-drift reconciliation (%s):\n%s",
                    r_out.count("REFUSED"), note, r_tail)
                if on_repair is not None:
                    on_repair(
                        r_approved,
                        f"{reconciled_note}; pre-commit pin maintenance "
                        "(partial — some file(s) refused on scan hits): "
                        + r_tail[:500],
                    )
                return
            # rc 2/other again: log and stop — no second reconciliation
            # attempt (the retry already consumed the one drift it
            # explained) — but the ledger rewrite above already happened
            # and must still be reported.
            _logger.warning(
                "manifest pin maintenance: approve --all --prune refused "
                "again (%s) even after reconciling a count drift (%s):\n%s",
                retry.returncode, note, r_tail)
            if on_repair is not None:
                on_repair(r_approved, reconciled_note)
            return
        why_not = f"; count-drift reconciliation declined: {note}" if note else ""
        _logger.warning(
            "manifest pin maintenance: approve --all --prune refused (%s):"
            "\n%s%s", proc.returncode, tail, why_not)
        return

    _logger.warning(
        "manifest pin maintenance: approve --all --prune refused (%s):\n%s",
        proc.returncode, tail)


def _public_manifest_shape(root: Path) -> Path | None:
    """The PUBLIC tree's ``scripts/check_release_manifest.py``, or ``None``.

    PUBLIC means: no ``scripts/export_guard.py`` (that is the PRIVATE tree's
    own route, handled by ``approve_pending_pins``/the reactive repair
    above), no ``EXPORT_CLASSIFICATION.txt`` (present without the guard
    installed — ``--write`` itself refuses there, so this must not spawn
    it), and both ``scripts/check_release_manifest.py`` and
    ``RELEASE_MANIFEST.txt`` present (otherwise this repo does not use the
    gate at all). Closely mirrors ``_repair_by_manifest_write``'s own inline
    checks — kept a separate predicate rather than a shared call so that
    function's raise-on-miss contract never has to change shape for this —
    but is not quite identical: it ALSO requires ``RELEASE_MANIFEST.txt`` to
    already exist, since this proactive step has no refusal text to parse a
    repair out of, and a repo with no manifest file at all is not using the
    gate in the first place, not a candidate for regenerating one.
    """
    guard = root / "scripts" / "export_guard.py"
    if guard.exists() or (root / "EXPORT_CLASSIFICATION.txt").exists():
        return None
    script = root / "scripts" / "check_release_manifest.py"
    if not script.exists() or not (root / "RELEASE_MANIFEST.txt").exists():
        return None
    return script


def _stage_new_directory_deliverables(repo: GitRepo) -> None:
    """Stage a non-code untracked file sitting beside something this commit
    is already staging into a directory that does not exist in ``HEAD`` yet.

    Mirrors the "new directory, no code extension" deliverable predicate
    ``GitRepo.commit_paths`` uses for its own untracked sweep (git.py,
    ``commit_paths``'s comment names the measured case: a new ``eval/``
    directory holding a harness module plus the markdown report running it
    produces) — reusing ``repo._dir_absent_from_tree`` directly rather than
    re-deriving the same "is this directory new" answer a second way, so
    the two predicates cannot drift apart. Without this, a Bash-generated
    non-code sibling of an explicitly-listed new file is invisible to both
    ``_stage_untracked_for_approve`` (code extensions only) and
    ``check_release_manifest.py --write`` (the INDEX only) — the exact
    "tracked but not listed" shape this module exists to close, just for a
    second file next to the first (independent review, 2026-09-07).

    Never raises: every git call here runs with ``check=False``, matching
    ``_stage_untracked_for_approve``'s own doctrine — the caller decides
    what a staging failure means for the commit, this only stages what it
    safely can.
    """
    staged = repo._run(
        "diff", "--cached", "--name-only", check=False
    ).splitlines()
    staged_dirs = {str(Path(s.strip()).parent) for s in staged if s.strip()}
    if not staged_dirs:
        return
    untracked = repo._run(
        "ls-files", "--others", "--exclude-standard", check=False
    ).splitlines()
    extra: list[str] = []
    for u in untracked:
        u = u.strip()
        if not u or GitRepo._is_ephemeral_path(u):
            continue
        if Path(u).suffix.lower() in GitRepo._CODE_EXTS:
            continue  # already staged by _stage_untracked_for_approve
        d = str(Path(u).parent)
        if d in staged_dirs and repo._dir_absent_from_tree(d, "HEAD"):
            extra.append(u)
    if extra:
        repo._run("add", "--", *extra, check=False)


def write_pending_manifest(
    repo: GitRepo,
    paths: list[str] | None,
    on_repair: Callable[[list[str], str], None] | None = None,
) -> list[str] | None:
    """Proactively regenerate the manifest, PUBLIC-tree shape, before the
    first commit attempt — the ``approve_pending_pins`` sibling for a repo
    with no ``export_guard.py`` ledger to consult.

    The dogfood bug this closes (task 2d30b000, PR #122 head 8ecb120e, PR
    for 0e1edabb head c68ee842): ``check_release_manifest.py``'s
    ``tracked_files()`` enumerates ``git ls-files`` — the INDEX — so a
    brand-new file is invisible to ``--write`` until it is staged. The
    reactive repair (``_repair_by_manifest_write``, below) only ever runs
    ``--write`` AFTER the pre-commit gate has already refused, and that
    gate never refuses a brand-new file in the first place (only an
    already-PINNED one whose content changed) — so a coder that ran
    ``--write`` before ``git add`` shipped the new file with no manifest
    row at all, failing CI's "File inventory" job (``--strict``). Staging
    *paths* here first — new files included — before running ``--write``
    closes that gap.

    Returns the *effective* paths for the commit: on every miss or failure
    this returns *paths* unchanged, so the reactive repair still gets its
    chance exactly as before this function existed. Never raises — same
    never-raises doctrine as ``approve_pending_pins``.

    1. Not the PUBLIC shape (see ``_public_manifest_shape``) — a no-op;
       the PRIVATE tree's route above already handled it, or this repo
       does not use the gate at all.
    2. The current branch is protected — a no-op; nothing here should touch
       the index on a branch the commit itself refuses to land on
       (``commit_paths``/``commit_all`` raise ``ProtectedBranch`` moments
       later).
    3. No changes to maintain — a no-op.
    4. ``paths`` falsy (``None``/empty) — a coder that wrote its new files
       entirely via Bash, with nothing for the caller to name individually.
       ``commit_all`` would stage everything anyway a few lines later, but
       only AFTER ``_commit()`` is reached — a new file left unstaged until
       then is invisible to ``--write`` run here, same defect this module
       exists to close. So this stages the whole tree itself
       (``repo.stage_all()``) before running ``--write``, and returns
       *paths* unchanged (still falsy): ``_commit()``'s own
       ``commit_all()`` call re-sweeps (idempotent — everything relevant is
       already staged) and picks up the regenerated manifest along with
       everything else.
    5. ``paths`` truthy — stage *paths* themselves. Reuses
       ``_stage_untracked_for_approve`` verbatim: it resolves each entry
       against the repo root (dropping anything outside it or ephemeral),
       and — because a bare relative string in *paths* resolves against the
       PROCESS cwd rather than the repo root (the same caveat
       ``GitRepo.commit_paths`` documents) — it also independently stages
       every untracked *code* file the repo already knows about via
       ``git ls-files --others``, which is how a brand-new file reaches the
       index even when the explicit entry could not be resolved.
       ``_stage_new_directory_deliverables`` then stages a non-code sibling
       of whatever just got staged, when it sits in a directory this commit
       is newly creating (the ``eval/harness.py`` + ``eval/report.md``
       shape — see that function's own docstring). A failed ``git add`` in
       either step is logged and does not stop this function from still
       attempting ``--write``.
    6. Run ``check_release_manifest.py --write``, bounded by the existing
       ``_MANIFEST_WRITE_TIMEOUT_S`` ceiling (the same constant the
       reactive route uses for the identical work). ``TimeoutExpired``,
       ``OSError``, and a non-zero exit are all logged and non-fatal here —
       the reactive route gets its chance if the gate still refuses.
    7. On a clean run, compare the manifest's bytes before and after.
       Unchanged — nothing to report (``--write`` rewrites unconditionally,
       so a mtime bump is not evidence; only a byte diff is). Changed —
       call ``on_repair`` exactly once. When *paths* is truthy,
       ``RELEASE_MANIFEST.txt`` is appended (as an ABSOLUTE path —
       ``commit_paths`` resolves each entry against the process cwd, not
       the repo root) to the returned paths, so it is staged into this same
       commit rather than left as an orphaned working-tree edit; when
       *paths* is falsy it is left modified-but-unstaged, since
       ``commit_all``'s own sweep (step 4, above) picks it up.
    """
    root = Path(repo.path)
    script = _public_manifest_shape(root)
    if script is None:
        return paths
    if _branch_protected(repo.current_branch(), repo.never_push_to):
        return paths
    if not repo.has_changes():
        return paths

    if not paths:
        try:
            repo.stage_all()
        except (GitError, OSError) as exc:
            _logger.warning("proactive manifest staging failed: %s", exc)
    else:
        try:
            _stage_untracked_for_approve(repo, paths)
            _stage_new_directory_deliverables(repo)
        except (GitError, OSError) as exc:
            _logger.warning("proactive manifest staging failed: %s", exc)

    manifest = root / "RELEASE_MANIFEST.txt"
    try:
        before = manifest.read_bytes()
    except OSError:
        before = b""

    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--write"],
            cwd=repo.path, capture_output=True, text=True,
            timeout=_MANIFEST_WRITE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _logger.warning(
            "proactive manifest --write timed out after %ss; continuing "
            "without it", _MANIFEST_WRITE_TIMEOUT_S)
        return paths
    except OSError as exc:
        _logger.warning("proactive manifest --write failed to start: %s", exc)
        return paths

    if proc.returncode != 0:
        tail = (proc.stderr.strip() or proc.stdout.strip())[:500]
        _logger.warning(
            "proactive manifest --write refused (rc=%s, "
            "check_release_manifest.py --write): %s",
            proc.returncode, tail)
        return paths

    try:
        after = manifest.read_bytes()
    except OSError:
        after = b""

    if after == before:
        return paths
    if on_repair is not None:
        tail = (proc.stdout.strip() + "\n" + proc.stderr.strip()).strip()
        on_repair(
            list(paths) if paths else [],
            "manifest re-pinned proactively by check_release_manifest.py "
            "--write: " + tail[:500],
        )
    if not paths:
        return paths
    return list(dict.fromkeys([*paths, str(manifest)]))


def parse_manifest_refusal(text: str) -> list[str] | None:
    """Extract the changed-but-pinned paths from a manifest-gate refusal.

    Returns the refused paths only when *text* is the gate's
    changed-pinned-files refusal. Any other text — other hooks, other
    shapes — returns None so the caller fails honestly instead of repairing.
    """
    if _MANIFEST_REFUSAL_MARKER not in text:
        return None
    return _REFUSED_PIN_RE.findall(text) or None


# Substrings this module raises when its OWN reactive repair could not
# reconcile a refusal (`commit_with_manifest_repair`, below) — distinct from
# the pre-commit gate's own refusal header, but still the export/manifest
# gate declining a commit, not an unrelated hook failure.
_REAPPROVE_FAILURE_MARKERS = (
    "manifest re-approve failed",
    "manifest re-approve timed out",
)


def is_gate_refusal(text: str) -> bool:
    """True when *text* is the export/manifest gate declining a commit —
    either the pre-commit gate's own refusal header, or this module's
    re-approve failure raised when the repair could not reconcile it.

    A checkpoint caller uses this to decide whether an unrepairable refusal
    is safe to bypass for a `[WIP-*]` commit (the gate protects what SHIPS;
    a WIP checkpoint ships nothing). Anything else — a merge conflict, a
    permission error, an unrelated hook failure — returns False so those
    keep failing the checkpoint exactly as before.
    """
    if _MANIFEST_REFUSAL_MARKER in text:
        return True
    return any(marker in text for marker in _REAPPROVE_FAILURE_MARKERS)


def commit_with_manifest_repair(
    repo: GitRepo,
    paths: list[str] | None,
    message: str,
    on_repair: Callable[[list[str], str], None] | None = None,
) -> CommitResult:
    """Commit, and if the manifest pre-commit gate refuses because
    already-pinned files changed, perform the gate's own documented FIX for
    whichever of the two repo shapes this one is, and retry ONCE:

    * PRIVATE tree (``scripts/export_guard.py`` present) —
      ``export_guard.py approve <paths>``: hash maintenance for files that
      are ALREADY classified ship; the re-derived manifest is staged by the
      retry's modified-tracked-files sweep.
    * PUBLIC tree (no ``export_guard.py``, ``scripts/check_release_manifest.py``
      present, no ``EXPORT_CLASSIFICATION.txt``) —
      ``check_release_manifest.py --write``: wholesale regeneration from the
      tree, sanctioned there precisely because no ledger sits beside it
      (see the module docstring's "Two repo shapes"). The retry appends
      ``RELEASE_MANIFEST.txt`` to *paths* explicitly rather than relying on
      the modified-tracked-files sweep alone.

    ``on_repair(offender_paths, note)`` is called once after a successful
    repair so the caller can put the pipeline-granted ledger change on the
    task's event record — it must never change silently (review finding F3).

    Anything else propagates ``GitError`` for the caller to turn into an
    honest attempt failure: an unclassified-file refusal never parses (and
    the target repo's guard refuses it besides), a failed or timed-out
    repair raises, a second refusal raises, and a repo with neither
    ``export_guard.py`` nor ``check_release_manifest.py`` (or one that still
    carries ``EXPORT_CLASSIFICATION.txt`` without the guard) does not use
    either repair route. ``ProtectedBranch`` passes through untouched — the
    repair never runs for it (nothing to parse).
    """
    approve_pending_pins(repo, paths, on_repair=on_repair)
    paths = write_pending_manifest(repo, paths, on_repair=on_repair)

    def _commit() -> CommitResult:
        if paths:
            return repo.commit_paths(list(paths), message)
        return repo.commit_all(message)

    try:
        return _commit()
    except GitError as exc:
        pinned = parse_manifest_refusal(str(exc))
        if not pinned:
            raise
        guard = Path(repo.path) / "scripts" / "export_guard.py"
        if not guard.exists():
            return _repair_by_manifest_write(
                repo, paths, message, pinned, exc, on_repair)
        try:
            proc = subprocess.run(
                [sys.executable, str(guard), "approve", *pinned],
                cwd=repo.path, capture_output=True, text=True,
                timeout=_APPROVE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise GitError(
                f"manifest re-approve timed out after {_APPROVE_TIMEOUT_S}s"
            ) from exc
        if proc.returncode != 0:
            reconciled, reconciled_note = _try_reconcile_count_drift(repo, proc.stderr)
            if reconciled:
                try:
                    retry = subprocess.run(
                        [sys.executable, str(guard), "approve", *pinned],
                        cwd=repo.path, capture_output=True, text=True,
                        timeout=_APPROVE_TIMEOUT_S,
                    )
                except subprocess.TimeoutExpired:
                    raise GitError(
                        f"manifest re-approve timed out after {_APPROVE_TIMEOUT_S}s "
                        "(after a count-drift reconciliation)"
                    ) from exc
                if retry.returncode == 0:
                    if on_repair is not None:
                        on_repair(
                            list(pinned),
                            f"count drift reconciled: {reconciled_note}; "
                            + retry.stderr.strip(),
                        )
                    return _commit()
                raise GitError(
                    "manifest re-approve failed "
                    f"({retry.returncode}) even after reconciling the attempt's own "
                    f"count drift ({reconciled_note}): {retry.stderr.strip()[:500]}"
                ) from exc
            # The reconciler's refusal (when it ran) travels with the guard's:
            # a human reading the failed attempt must see that the safety net
            # ran and the arithmetic it declined on, not only the stale number.
            why_not = (
                f"; count-drift reconciliation declined: {reconciled_note}"
                if reconciled_note else ""
            )
            raise GitError(
                "manifest re-approve failed "
                f"({proc.returncode}): {proc.stderr.strip()[:500]}{why_not}"
            ) from exc
        if on_repair is not None:
            on_repair(list(pinned), proc.stderr.strip())
        return _commit()


def _repair_by_manifest_write(
    repo: GitRepo,
    paths: list[str] | None,
    message: str,
    pinned: list[str],
    exc: GitError,
    on_repair: Callable[[list[str], str], None] | None,
) -> CommitResult:
    """The PUBLIC-tree repair route for `commit_with_manifest_repair`.

    Only reachable when `scripts/export_guard.py` is absent — the private
    tree's ledger route (above) always wins when it exists. Two more
    preconditions gate this route, checked before spawning anything; either
    miss re-raises *exc* unrepaired, exactly today's behaviour for a repo
    that carries neither shape of the gate:

    * `scripts/check_release_manifest.py` must exist — otherwise this repo
      does not use the gate at all.
    * `EXPORT_CLASSIFICATION.txt` must be ABSENT. Its presence is the
      private tree's shape without the guard installed — `--write` itself
      refuses there (it would forge approvals for `drop`-classified paths
      with no term scan), so re-raising the original refusal is the honest
      outcome rather than spawning a call already known to fail.

    `--write` regenerates the whole manifest from `git ls-files` +
    working-tree bytes (the manifest's own row excluded, a symlink hashed
    by its target) — a bigger, unconditional sweep than the reactive
    per-file re-scan, but the one sanctioned exactly because no
    classification ledger sits beside this tree to forge approvals against.
    It is idempotent and re-pins every drifted tracked file, not only
    *pinned* — the refused paths that led here — so a tracked file whose
    working-tree bytes still won't match what gets staged (a conflicting
    concurrent edit) earns a second, honest refusal on the retry below;
    there is no third round.

    Every outcome:

    * `TimeoutExpired` / `OSError` / `returncode != 0` all raise
      `GitError` chained from *exc*, reusing the existing
      `"manifest re-approve failed"` / `"manifest re-approve timed out"`
      substrings verbatim — `is_gate_refusal` classifies these through the
      unchanged `_REAPPROVE_FAILURE_MARKERS`, so the checkpoint bypass seam
      needs no change for this new route.
    * `0` calls `on_repair(pinned, note)` exactly once, then retries the
      commit exactly once with `RELEASE_MANIFEST.txt` appended to *paths*
      (as an ABSOLUTE path: `GitRepo.commit_paths` resolves each entry with
      `Path(p).resolve()`, which resolves a bare relative name against the
      process CWD, not the repo root, and silently drops it when that does
      not land inside the repo). A second refusal from the retry propagates
      untouched.
    """
    root = Path(repo.path)
    script = root / "scripts" / "check_release_manifest.py"
    if not script.exists() or (root / "EXPORT_CLASSIFICATION.txt").exists():
        raise exc
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--write"],
            cwd=repo.path, capture_output=True, text=True,
            timeout=_MANIFEST_WRITE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise GitError(
            "manifest re-approve timed out after "
            f"{_MANIFEST_WRITE_TIMEOUT_S}s (check_release_manifest.py --write)"
        ) from exc
    except OSError as write_exc:
        raise GitError(
            "manifest re-approve failed "
            f"(--write could not start): {write_exc}"
        ) from exc
    if proc.returncode != 0:
        tail = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(
            "manifest re-approve failed "
            f"(rc={proc.returncode}, check_release_manifest.py --write): "
            f"{tail[:500]}"
        ) from exc
    if on_repair is not None:
        tail = (proc.stdout.strip() + "\n" + proc.stderr.strip()).strip()
        on_repair(
            list(pinned),
            "manifest re-pinned by check_release_manifest.py --write: "
            + tail[:500],
        )
    manifest = str(root / "RELEASE_MANIFEST.txt")
    if paths is not None:
        return repo.commit_paths(list(dict.fromkeys([*paths, manifest])), message)
    return repo.commit_all(message)


def _try_reconcile_count_drift(
        repo: GitRepo, refusal_text: str) -> tuple[bool, str | None]:
    """When `export_guard.py approve`'s reactive re-scan refuses (2) with a
    win-COUNT that drifted, and that drift is fully explained by THIS
    ATTEMPT's own diff (one added/removed file, not a hand decision),
    rewrite the declared count and return the reconciliation note.

    Reuses `approve_merge.reconcile_commit_count_drift` — the same refusal
    parser, in-place rewriter and staging step as the merge-time reconciler
    (`reconcile_merge_count_drift`, PR #511) — for the commit-time
    arithmetic: `real - declared == added - removed` in `git diff
    --name-status HEAD`, not base+branch-ancestor arithmetic. ``HEAD`` is
    this attempt's own base: this reactive path only runs while creating a
    brand-new commit for the current round, so HEAD is the tip immediately
    before the round's changes.

    Returns ``(True, note)`` when the count was rewritten and staged.
    Returns ``(False, None)`` when the refusal names no count drift at all
    (nothing attempted), and ``(False, note)`` when the reconciler ran and
    declined — an unexplained drift, a duplicated rule, a rule the diff
    never touched, or any other hand decision — with ``note`` carrying its
    arithmetic. The caller raises the guard's own refusal and appends that
    note, so the failed attempt shows WHY the safety net did not apply.
    """
    if not COUNT_DRIFT_RE.search(refusal_text):
        return False, None
    ok, note = reconcile_commit_count_drift(Path(repo.path), "HEAD", refusal_text)
    return (True, note) if ok else (False, note)
