"""Reviewer worktree integrity guard (task d115e22f).

The reviewer session keeps Bash enabled (see `docs/security.md`), so a shell
redirect (``echo x > calc.py``) is not a tool call `agent/guard.py`'s
PreToolUse hook ever sees. A compromised or careless review session could
therefore write into the very worktree the gate is about to judge, and the
gate would then judge the tree the reviewer LEFT rather than the tree it was
GIVEN.

This module is the pure snapshot/diff/targeted-revert machinery
``Orchestrator._run_reviewer`` wraps around its single ``reviewer.review(...)``
call. It never touches `agent/guard.py`, the reviewer's toolset, or anything
about WHAT the reviewer is allowed to call — it only asks, mechanically,
"did the tree change under the reviewer, and if so, undo exactly that."

Module contract: every public function either returns a complete,
trustworthy result or raises :class:`WorktreeCheckFailed`. There is no
"probably fine" return value — "could not tell" must never be read as
"nothing happened," because that is the exact failure this module replaces
(the previous attempt's check failed OPEN: guard machinery that couldn't run
was treated as if the reviewer had written nothing).

Second re-scope (this revision): `git status` — the instrument `snapshot`/
`compare` were originally built on — is structurally blind to the `.git`
subtree; it only ever reports on the tracked/untracked WORKTREE. A Bash-
enabled reviewer can plant an executable hook (e.g. `post-checkout`) there
invisibly to that instrument, and this module's own `revert()` calls
(`git checkout`, `git reset`) would then EXECUTE it. `_git_dir_inventory`
closes the detection gap; the `-c core.hooksPath=`/`-c core.fsmonitor=false`
flags on every `revert()` git call close the execution gap. Neither one does
the other's job — both are required.

Third re-scope (this revision): `_git_dir_inventory` hashes the `.git`
subtree's BYTES, but the `"index"` entry in `_VOLATILE_GIT_EXACT` excludes
the index file from that byte inventory — correctly, since every read-only
`git status`/`git diff` the reviewer legitimately runs rewrites its
stat-cache bytes. That exclusion also discarded the only signal that the
index's FLAG BITS had been tampered with. `git update-index
--assume-unchanged calc.py` (or `--skip-worktree calc.py`) followed by
rewriting `calc.py` leaves `git status --porcelain` EMPTY while disk holds
the reviewer's payload — this module's own opening threat, with no `.git`
artefact involved. `_index_flags`/`compare()` close this by reading the
index's FLAG BITS via `git ls-files -v`, watched separately from the
excused bytes.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

_DEFAULT_TIMEOUT_SECONDS = 30.0


class WorktreeCheckFailed(RuntimeError):
    """Snapshot, diff or revert could not be performed with confidence.

    Callers must treat this as fail-closed: route it to the
    ``ReviewerUnavailable`` escalation channel, never fall back to trusting
    the reviewer's decision as-is.
    """


@dataclass(frozen=True)
class Snapshot:
    head: str
    entries: dict[str, str]
    git_entries: dict[str, tuple[int, int, str]]
    #: The shared HEAD's literal content at snapshot time (linked worktrees
    #: only; None on a lone checkout or when unreadable). `compare` needs the
    #: BYTES, not the inventory hash, to tell an ordinary branch switch
    #: (symref -> symref) from a detach or garbage write — the hash alone
    #: cannot say which shape either side had.
    common_head: str | None = None
    #: Per git-config FILE (keyed as in `git_entries`, e.g. "common/config"),
    #: the EFFECTIVE `key=value` set of that one file at snapshot time. Like
    #: `common_head`, `compare` needs the semantic content, not the inventory
    #: hash, to tell a byte-only re-serialization of config (whitespace,
    #: section reordering, comments — what git and concurrent shared-dir
    #: writers produce) from a real key change (`include.path`, `alias.*`,
    #: `filter.*`, `core.hooksPath`, …). Only present-and-parseable files
    #: appear; a missing key reads as "could not establish" -> fail closed.
    config_norm: dict[str, str] = field(default_factory=dict)
    #: `{path: tag}` for every tracked path whose index FLAG BITS blind
    #: `git status --porcelain` to further writes on disk -- the
    #: assume-unchanged and skip-worktree bits, from `git ls-files -v` (see
    #: `_index_flags`). Like `config_norm`, this is the semantic-content
    #: reader for the index: `compare` needs the FLAG BITS, not the index's
    #: bytes, to catch a stat-cache refresh (bytes change, flags don't) vs a
    #: blinding bit flip (flags change) -- the "index" entry in
    #: `_VOLATILE_GIT_EXACT` excuses only the former.
    index_flags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Delta:
    added: list[str]
    modified: list[str]
    deleted: list[str]
    #: `.git`-common-dir config paths EXCUSED by `compare()`'s benign-key
    #: allowlist (see `_BENIGN_CONFIG_KEY_PATTERNS`) — disclosed but NOT a
    #: verdict-discarding write. Deliberately excluded from `is_empty()`: a
    #: benign-only delta IS empty for gate purposes, same as an
    #: effective-equal config re-serialization already was before this field
    #: existed.
    benign: list[str] = field(default_factory=list)
    #: The config keys that changed within `benign` paths, for the disclosure
    #: event — never used for gating.
    benign_keys: list[str] = field(default_factory=list)
    #: The changed config keys that were NOT on the benign allowlist and so
    #: kept the file a violation — capped at `_MAX_NONBENIGN_KEYS_SHOWN`.
    #: Disclosure only; the discard already happened via `modified`.
    nonbenign_keys: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.deleted)


#: Audit trail for every ``subprocess.run`` call site in this module (task
#: reviewer-worktree-returncode-audit). Each entry names whether that call's
#: exit status is checked and, for the one that is not, why an unraised
#: failure still fails closed downstream. `tests/test_reviewer_worktree.py
#: ::test_every_subprocess_run_call_site_is_captured_in_the_audit` parses this
#: module's AST and asserts the two sets are exactly equal, so a future
#: subprocess call added here without an entry — or an entry left stale after
#: a call site is removed — goes red rather than shipping silently unaudited.
SUBPROCESS_CALL_AUDIT: dict[str, str] = {
    "_run_git": (
        "checked — `if proc.returncode != 0: raise WorktreeCheckFailed(...)` "
        "below. This is LOAD-BEARING, not defensive: a failed `git status "
        "--porcelain -z` that was allowed to return would give back an EMPTY "
        "stdout (see the `except` clauses above, which already convert "
        "timeout/OSError to the same raise); `_parse_porcelain_z(\"\")` reads "
        "that as no entries; `compare()` reads no entries as an empty "
        "`Delta`; and `_run_reviewer` takes the `delta.is_empty(): return "
        "decision` branch, handing back the reviewer's own PASS untouched. "
        "That is the exact 'could not tell, read as nothing happened' "
        "failure this module's docstring says it replaces. Ablating this "
        "check (`if False and proc.returncode != 0:`) is what "
        "`test_run_git_raises_on_a_nonzero_exit_and_never_returns_its_empty_"
        "stdout` and `test_a_failing_git_status_after_the_review_is_never_"
        "read_as_a_clean_tree` (wiring test) pin red."
    ),
    "_config_effective": (
        "deliberately UNCHECKED — returns None on a non-zero exit (or "
        "TimeoutExpired/OSError), instead of raising. Safe only because "
        "`compare()` never treats None as a match: it excuses a config "
        "file's byte-level re-serialization solely when `bn is not None and "
        "an is not None and bn == an` (see the `bn`/`an` comparison in "
        "`compare()`), so a config read that failed on either side of the "
        "diff keeps the byte-level violation and fails closed rather than "
        "silently excusing a real key change."
    ),
}


def _run_git(repo_path: Path, *args: str, timeout: float) -> str:
    # Variadic, matching `vcs/push_hook.py:_git` / `vcs/pr_watcher.py:_git_rc`
    # / `vcs/git.py:GitRepo._run` — not a `list[str]` parameter. The
    # egress-allowlist scanner (tests/test_egress_allowlist.py) resolves a
    # git subcommand only from literal words it can see AT THE CALL SITE; a
    # single `args: list[str]` parameter hides every call site's literal
    # subcommand behind one opaque list value and the whole module collapses
    # to one undifferentiated `exec:git <dynamic>` channel. `*args` lets each
    # call site spell its subcommand as a literal positional word, so
    # `_run_git(repo_path, "rev-parse", ref, timeout=t)` resolves to the
    # already-classified-LOCAL `exec:git rev-parse`, same as every other git
    # wrapper in this tree.
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeCheckFailed(
            f"git {' '.join(args)} timed out after {timeout}s") from exc
    except (FileNotFoundError, PermissionError, OSError, TypeError) as exc:
        raise WorktreeCheckFailed(
            f"git {' '.join(args)} could not run: {exc}") from exc
    # LOAD-BEARING, see `SUBPROCESS_CALL_AUDIT["_run_git"]` above: without
    # this raise, a failed `git status` returns "" (git writes nothing to
    # stdout on a hard failure), `_parse_porcelain_z("")` parses that to no
    # entries, `compare()` reads no entries as a clean `Delta`, and
    # `_run_reviewer` hands back the reviewer's own PASS as if the tree had
    # never been touched. Deleting or disabling this check (`if False and
    # proc.returncode != 0:`) is a silent mutant today — the whole suite
    # stays green — which is exactly what
    # `test_every_subprocess_run_call_site_is_captured_in_the_audit`,
    # `test_run_git_raises_on_a_nonzero_exit_and_never_returns_its_empty_stdout`
    # and the wiring test
    # `test_a_failing_git_status_after_the_review_is_never_read_as_a_clean_tree`
    # exist to pin.
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().replace("\n", " ")[:400]
        raise WorktreeCheckFailed(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {stderr}")
    return proc.stdout or ""


def _content_hash(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return "deleted"
    except (PermissionError, OSError) as exc:
        raise WorktreeCheckFailed(f"could not read {path}: {exc}") from exc
    return hashlib.sha256(data).hexdigest()


def _parse_porcelain_z(raw: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain=v1 -z` output into (path, XY) pairs,
    skipping the extra "from" token a rename/copy entry carries."""
    tokens = raw.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        out.append((path, xy))
        if xy[0] in ("R", "C") and i < len(tokens):
            i += 1  # skip the rename/copy source path
    return out


# --- .git-subtree inventory (part A) ---------------------------------------
#
# `git status` never reports on paths under `.git` itself, so the porcelain
# snapshot above is structurally blind to a planted hook. This second
# instrument walks the `.git` subtree directly.
#
# Volatile exclusion list — kept short, each entry justified, matched on the
# path *relative to the root it was found under* (so "index" excludes both
# the admin dir's and the common dir's index file, not a directory prefix):
_VOLATILE_GIT_EXACT = frozenset({
    # BYTES stay excused: rewritten by every read-only `git status`/`git
    # diff` the reviewer legitimately runs (stat-cache refresh), and the
    # bytes are not an execution surface. That is NOT the whole story — an
    # earlier revision's "not an execution surface" was true and beside the
    # point: the index is the FILTER through which `git status --porcelain`
    # sees the worktree, so excluding its bytes also removed the only
    # signal that the filter itself had been tampered with. `git
    # update-index --assume-unchanged calc.py` (or `--skip-worktree`)
    # followed by rewriting `calc.py` leaves porcelain EMPTY while disk
    # holds the reviewer's payload — this module's own opening threat, with
    # no `.git` artefact involved. WHAT THIS EXCLUSION COVERS: nothing by
    # itself now — the assume-unchanged and skip-worktree FLAG BITS are
    # watched separately, by `_index_flags`/`compare()`, via
    # `git ls-files -v` rather than by hashing this file. WHAT IT DOES NOT
    # COVER: the index's other contents (staged blobs, cache-tree,
    # extensions) remain unwatched, and, as `_git_dir_inventory` records for
    # the tree at large, a bit set and cleared again BETWEEN the two
    # snapshots is invisible.
    "index",
    # Transient lock created/removed around index writes. Never executed.
    "index.lock",
    # Bookkeeping ref written by `git fetch`. Pure data, never executed.
    "FETCH_HEAD",
    # Bookkeeping ref written by `reset`/`merge`/`rebase`. Never executed.
    "ORIG_HEAD",
    # A flat snapshot of every ref in the repo, rewritten by `git gc`/
    # `pack-refs` and by ordinary push/fetch traffic anywhere in the SHARED
    # object store — same "not this review's business" rationale as the
    # `refs/` directory prune below, just packed into one file instead of a
    # directory. Never executed; the ref this worktree actually has checked
    # out is independently covered by the `HEAD` comparison in `compare()`.
    "packed-refs",
})
# The reflog: appended on every ref update by ordinary git use. Never
# executed.
_VOLATILE_GIT_PREFIX = "logs/"

# Whole subtrees PRUNED before descent — `_iter_git_paths` never lists their
# contents at all, as opposed to `_VOLATILE_GIT_EXACT`/`_VOLATILE_GIT_PREFIX`
# above, which still walk and stat every path and only filter afterwards.
# These are shared, high-churn, high-volume subtrees where that walk itself
# is the cost:
_SKIPPED_GIT_DIR_PREFIXES = frozenset({
    # `--git-common-dir` is shared by EVERY linked worktree of this repo, and
    # itself contains `worktrees/<id>/` for each one — including this
    # worktree's own admin dir, which `_git_dir_inventory` already walks
    # separately via `--absolute-git-dir`. Walking it again unrestricted
    # would (a) re-discover this worktree's own files a second time under a
    # different relative path (`worktrees/<id>/index` vs `index`), which the
    # exclusion lists above — matched exactly, on purpose, so they stay short
    # and auditable — would not recognise, false-positiving on ordinary index
    # churn from a read-only `git status`; and (b) pick up every OTHER
    # worktree's admin dir sharing this common dir, so an unrelated
    # concurrent task's git activity would trip THIS review's guard. Neither
    # is a real execution surface for `repo_path`: each worktree's own
    # `core.hooksPath` already points only inside its own admin dir
    # (`push_hook.py`), never another worktree's.
    "worktrees/",
    # Git's content-addressed object store. An existing OBJECT's bytes are
    # immutable — it is identified BY their sha256 — but do NOT read that as
    # "nothing under this tree can be modified": an earlier revision claimed
    # the only possible write here is an ADDITION and that claim is FALSE.
    # `objects/info/alternates` is an ordinary mutable file that git READS, and
    # rewriting it makes a foreign object store resolvable through this repo
    # while `compare()` reports an empty delta (measured: `git cat-file` on a
    # foreign blob goes 128 -> 0). The counter-example and the full residual
    # are in `_is_volatile_git_path`'s docstring below; it is named here too
    # because this is where a reader auditing the exclusion list arrives, and
    # the refuted sentence used to be all they found. The store also gains new
    # objects constantly from every OTHER concurrent task's linked worktree,
    # not just this review.
    # Walking and content-hashing it anyway was both a real
    # perf cost (measured on this checkout: 150MB across 2628 files, read
    # three times per review — snapshot, compare, and revert's own internal
    # compare — and growing with repo history) and a false-positive source
    # unrelated to what THIS reviewer wrote. Whatever the reviewer's own
    # commits added here is still caught without walking a single object:
    # `compare()` tracks `HEAD` via `git rev-parse` independently of this
    # inventory, and `revert()` refuses outright the moment HEAD moved.
    "objects/",
    # Every ref in the repo, not just the one this worktree has checked
    # out — shared across every worktree, and moved by concurrent unrelated
    # pushes/fetches/branch activity on OTHER branches this review never
    # touched. The ref THIS worktree has checked out is independently
    # covered by the `HEAD` sha comparison in `compare()`, so pruning
    # `refs/` loses no coverage of what the reviewer did while removing the
    # same shared-store concurrency false-positive vector as `objects/`
    # above (`refs/stash`, previously listed as an exact exception here, is
    # now covered by this prune instead).
    "refs/",
})

# Deliberately NOT excluded — a later reader must not "tidy" these in:
# `hooks/`, `no_human-hooks/` (the direct hook execution surface this guard
# exists to catch; see `push_hook.py`'s `_MIRRORED_HOOKS` shim chain),
# `config` / `config.worktree` / `info/attributes` (a second exec-on-checkout
# surface via smudge/clean/textconv filters and `alias.*`; hardening that is
# out of scope, but detecting a write to it is not), and the `.git` pointer
# file itself (rewriting it repoints the whole admin dir for a linked
# worktree).
#
# `config`/`config.worktree` stay WATCHED here, but they are NOT byte-hash
# compared: `compare()` adjudicates them by EFFECTIVE key set (see
# `_config_effective` and the config branch in `compare`), the same
# content-shape treatment `common/HEAD` gets and for the same reason — git
# and concurrent shared-dir writers re-serialize config on ordinary
# bookkeeping and byte-hashing that discarded real verdicts. This is NOT a
# volatile-path exclusion: every added/changed key (the `include.path`/
# `alias.*`/filter surfaces above) is still caught.


def _is_volatile_git_path(rel: str, label: str) -> bool:
    """Is this `.git`-relative path one git rewrites as DATA and never runs?

    A DENYLIST, deliberately, and the inversion that replaced it in an earlier
    revision of this commit was WRONG. That revision asked "is this an
    execution surface?" and watched only `hooks/**`, `*config*` and
    `info/attributes`. An independent review broke it by EXECUTION in four
    ways — a submodule's `modules/<sub>/hooks/`, a `core.hooksPath` more than
    one level deep, a `HEAD` rewrite preserving the resolved sha, and, the one
    that settles the design, `include.path`: a WATCHED file (`config`) can
    NAME an UNWATCHED one, and rewriting the included file got `git pwn` to
    run while `compare()` reported empty. Main caught every one of them.

    So the executable set under `.git` is NOT CLOSED under any finite list of
    names, and an allowlist cannot be made safe by lengthening it. Watching
    everything and excluding as little as possible is the only shape that
    fails closed, and unknown-therefore-watched is the correct default for a
    gate whose whole job is catching what nobody predicted.

    THE EXCLUSIONS ARE NOT ALL "PROVABLY INERT", and an earlier version of
    this paragraph said they were. `objects/` is skipped on the argument that
    its contents are content-addressed and immutable, so the only possible
    write is an addition. That is false for at least one path inside it:
    `objects/info/alternates` is an ordinary mutable file that git READS, and
    rewriting it makes a foreign object store resolvable through this repo
    while `compare()` reports empty.

    STATE THE PROPERTY, NOT ONE INSTANCE OF IT. Two earlier revisions of
    this paragraph each framed the hole one size too small — first as
    needing a config `include.path` under a pruned tree, then as "anything
    written under a pruned tree". A round-10 review measured the second
    framing short too. The real property:

        ANYTHING WRITTEN OUTSIDE THE WATCHED SET DURING A REVIEW IS
        INVISIBLE TO THIS GUARD — INCLUDING AN EXECUTABLE GIT WILL RUN —
        AND THE WATCHED SET DOES NOT FOLLOW POINTERS.

    A pre-existing `core.hooksPath` (watched, and unchanged, so nothing
    trips) is enough whenever its target is not effectively watched, and
    the effectively-watched set is EXACTLY: the `.git` inventory minus its
    walk-pruned trees (`_SKIPPED_GIT_DIR_PREFIXES`: `objects/`, `refs/`,
    `worktrees/`) and minus the excused volatile names/prefix
    (`_VOLATILE_GIT_EXACT`, `logs/`), minus, under `common` only, the two
    label-scoped names `COMMIT_EDITMSG`/`info/refs` (this function's third
    excuse branch, below); with `common/HEAD` walked but adjudicated by
    CONTENT SHAPE in `compare()`, so a symref -> symref repoint is not
    reported (see the `common/HEAD` paragraph below); plus the worktree
    paths `git status --untracked-files=all --ignored=no` reports. So a target in a pruned
    `.git` tree, outside both `.git` and the worktree, or in a GITIGNORED
    worktree directory (`.venv/`, `build/`, `__pycache__/`, … — every one
    writable) all execute with an empty delta; only an untracked-and-not-
    ignored worktree target is caught (measured: that planted hook shows as
    `added`, while the same hook under a gitignored dir executed with
    `delta.is_empty() == True` — round-12 review, positive control run).
    This paragraph has now mis-drawn this boundary four times; the
    constant names above are the boundary, not a summary of it.

    That bypass is PRE-EXISTING — it reproduces identically against main, so
    nothing here introduces or widens it — but it is a live hole and it is
    recorded rather than implied. Closing it would require RESOLVING the
    pointer targets (`core.hooksPath`, `include.path`, and any future kin)
    and watching wherever they land; unpruning the `.git` trees alone would
    not reach a target outside `.git` at all.

    `label` is "admin" (this worktree's own git dir) or "common" (the SHARED
    one, which on a linked-worktree install IS the primary checkout's `.git`).
    The distinction is load-bearing: the excused names are written by
    whoever is using the primary checkout, and NOTHING here can establish
    which process wrote a common-dir file — the excuse rests on the write's
    SHAPE being ordinary, never on who made it. `admin/HEAD` is the reviewed
    worktree's own and rewriting it — to the raw sha, say — silently
    detaches the branch the pipeline pushes; it stays watched.

    `common/HEAD` is deliberately NOT in the excuse list: a path-level skip
    would also hide a reviewer-side write that repoints the PRIMARY
    checkout's HEAD (measured: such a write moved the primary checkout to a
    decoy branch), and with `refs/` pruned for cost that flip would be the
    hijack's last visible step. It is handled by CONTENT SHAPE in `compare`
    instead: symref -> symref (an ordinary branch switch) is clean, anything
    else — a raw sha, garbage — is a violation. The stated residual: a
    symref -> symref rewrite of `common/HEAD` by the reviewer itself is
    indistinguishable from the operator switching branches and is NOT
    detected here; pointing it at a NEW decoy ref, though, needs a write
    under `refs/heads/` in a packed or loose form the primary checkout would
    then act on — a channel `refs/` pruning already concedes on main too.

    The effectively-watched set (see the paragraph above) also includes,
    separately from the `.git` byte inventory it describes, the index's
    FLAG BITS via `_index_flags` — the assume-unchanged and skip-worktree
    bits that would otherwise blind `git status --porcelain` to a rewritten
    tracked file with no `.git` artefact involved at all.
    """
    if rel in _VOLATILE_GIT_EXACT or rel.startswith(_VOLATILE_GIT_PREFIX):
        return True
    # Written by ordinary work in the SHARED git dir, by whoever is using the
    # primary checkout. Each measured discarding a completed verdict:
    #   primary `git commit`   -> COMMIT_EDITMSG (git's editor buffer)
    #   primary `git gc`       -> info/refs      (a generated ref cache)
    # (`git checkout` -> common/HEAD was the third measured writer; it is
    # content-adjudicated in `compare`, not path-skipped — see above.)
    # Neither excused name is ever executed by git.
    #
    # BOTH ARE SCOPED TO "common", which is where the concurrent writer
    # is. An earlier revision left COMMIT_EDITMSG and info/refs unscoped, which
    # blinded the ADMIN side too — this worktree's OWN git dir, where no other
    # process writes — and a review measured that as a detection regression
    # against main for both. Scoping them costs nothing (the false positives
    # all arrive via "common") and is pinned in both directions below.
    if label == "common" and rel in ("COMMIT_EDITMSG", "info/refs"):
        return True
    return False


def _resolve_git_root(repo_path: Path, arg: str, *, timeout: float) -> Path:
    raw = _run_git(repo_path, "rev-parse", arg, timeout=timeout).strip()
    if not raw:
        raise WorktreeCheckFailed(
            f"git rev-parse {arg} returned nothing in {repo_path}")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repo_path / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorktreeCheckFailed(
            f"could not resolve git dir from {arg}={raw!r}: {exc}") from exc


#: The two git-config FILES `compare` adjudicates by EFFECTIVE content rather
#: than by raw byte-hash — same name under either root. `config.worktree` is
#: git's per-worktree config file (present only under `extensions.worktreeConfig`).
_CONFIG_FILENAMES = ("config", "config.worktree")


def _config_effective(path: Path, *, timeout: float) -> str | None:
    """The normalized `key\\0value` set of a SINGLE git-config file, or None if
    it cannot be read as config.

    Reads ONLY *path* (`git config --list --file`), so — critically for a file
    this guard is judging for tampering — it does NOT expand `include.path`
    (no `--includes`), never loads system/global config, and never executes a
    filter, pager or hook. It is the same file `_git_dir_inventory` hashed,
    read for its EFFECTIVE content instead of its bytes: a byte-only
    re-serialization normalizes to the same set, while any added/changed
    key — `include.path`, `alias.*`, `filter.*.process`, `core.hooksPath`, the
    exact execution surfaces the inventory watches config FOR — changes it.

    None (unreadable, syntactically broken, or git absent) is deliberately not
    an empty set: `compare` treats it as "could not establish equality" and
    keeps the byte-level violation, so garbage written over config fails closed.
    See `SUBPROCESS_CALL_AUDIT["_config_effective"]` for why this call site is
    the one deliberately-unchecked exception to `_run_git`'s pattern.
    """
    if not path.is_file():
        return None
    try:
        proc = subprocess.run(
            ["git", "config", "--list", "-z", "--file", str(path)],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return "\0".join(sorted(proc.stdout.split("\0")))


def _config_keyvals(norm: str) -> dict[str, list[str]]:
    """Parse `_config_effective`'s normalized `key\\nvalue\\0key\\nvalue...`
    string into `{key: sorted([value, ...])}`, keyed by name so a caller can
    ask "did this KEY'S value set change" rather than only "did the whole
    file change."

    `git config --list -z` emits one `key\\nvalue` record per `\\0`-terminated
    entry; a key missing its `\\n` (git can emit a bare key for a
    valueless/boolean-shorthand entry) reads as value `""`. `maxsplit=1` on
    the split is deliberate — a value may itself contain a literal newline
    (a multi-line config value), and only the FIRST newline separates key
    from value. A key set more than once (e.g. `remote.origin.fetch`) keeps
    every value, sorted, so a reordering of duplicate entries is not mistaken
    for a value change.
    """
    out: dict[str, list[str]] = {}
    for rec in norm.split("\0"):
        if not rec:
            continue
        key, _, value = rec.partition("\n")
        out.setdefault(key, []).append(value)
    return {k: sorted(v) for k, v in out.items()}


#: Evidence for this allowlist: 31 `attempts` rows in `~/.no_human/no_human.db`
#: (`failure_reason like '%reviewer wrote to the worktree%'`) all share the
#: identical shape `(0 added, 1 modified, 0 deleted): ~.git/common/config` —
#: zero tracked-path changes, the sole diff a key added/changed in the
#: worktree-shared `.git/config` by a CONCURRENT process (another worktree's
#: `git branch`/`checkout -b`/`fetch`, editor tooling) during the review
#: window, never the reviewer under test. `git config --list --file` on the
#: shared config in this install shows exactly this churn: dozens of
#: `branch.<name>.rebase|remote|merge|vscode-merge-base` keys written by other
#: concurrent worktrees, plus `maintenance.*`/`gc.*` bookkeeping git writes to
#: itself. None of these keys has an execution surface — they are read by
#: `git branch`/`git pull --rebase`/`git maintenance`, never by a hook,
#: filter, alias, or include. Matched on the NORMALIZED LOWERCASE key: git
#: itself lowercases the section and the trailing subkey name but preserves
#: the middle branch/remote NAME verbatim, so the pattern must allow arbitrary
#: (non-dot) text in that middle segment.
#:
#: Deliberately NOT here, with an execution surface each: `remote.*.url`
#: (redirects a future fetch/push), `remote.*.uploadpack`/`receivepack`
#: (arbitrary command on push/fetch), `core.*` (`core.hooksPath`,
#: `core.fsmonitor`), `alias.*` (arbitrary command), `filter.*`
#: (smudge/clean/process — arbitrary command), `include`/`includeif.*`
#: (pulls in attacker-controlled config), `extensions.*`, `credential.*`,
#: `http.*` (proxy/cert overrides), `pager.*`, `diff.*.textconv` (arbitrary
#: command), `submodule.*` (arbitrary repo URL). An unrecognised key returns
#: `False` from `_is_benign_config_key` — fail-closed default, same posture as
#: every other adjudication in this module.
_BENIGN_CONFIG_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^branch\.[^.]+\.(rebase|remote|merge|pushremote|description|"
               r"vscode-merge-base)$"),
    re.compile(r"^maintenance\..+$"),
    re.compile(r"^gc\..+$"),
)


def _is_benign_config_key(key: str) -> bool:
    """`True` only for a config key this module has evidence is written as
    git/tooling bookkeeping with no execution surface. See
    `_BENIGN_CONFIG_KEY_PATTERNS` for the evidence and the exclusion list.
    """
    normalized = key.strip().lower()
    return any(p.match(normalized) for p in _BENIGN_CONFIG_KEY_PATTERNS)


#: Cap on how many non-benign config keys `compare()` names in a discard
#: display string. Mirrors `orchestrator._INTEGRITY_PATHS_SHOWN` (5) and the
#: same "and N more" tail: this text is PERSISTED into a review verdict, so a
#: pathological diff must not blow the message up. Naming the keys does not
#: change the gate — any non-benign key still discards.
_MAX_NONBENIGN_KEYS_SHOWN = 5


def _config_norm_map(
    admin_dir: Path, common_dir: Path, *, timeout: float
) -> dict[str, str]:
    """`{git_entry_key: effective_config}` for every config file that exists,
    keyed exactly as `_git_dir_inventory` keys it (`"<label>/<name>"`), so
    `compare` can look the snapshot value up by the same path it iterates."""
    roots = [("admin", admin_dir)]
    if common_dir != admin_dir:
        roots.append(("common", common_dir))
    out: dict[str, str] = {}
    for label, root in roots:
        for name in _CONFIG_FILENAMES:
            norm = _config_effective(root / name, timeout=timeout)
            if norm is not None:
                out[f"{label}/{name}"] = norm
    return out


def _index_flags(repo_path: Path, *, timeout: float) -> dict[str, str]:
    """`{path: tag}` for every tracked path whose index FLAG BITS blind
    `git status --porcelain` to further writes on disk.

    Reads `git ls-files -v -z`, which tags every tracked path with a single
    letter. Git LOWERCASES the tag when the assume-unchanged bit is set
    (`H` -> `h`, the documented marker), and uses `S`/`s` for skip-worktree
    (`s` when both bits are set on the same path — caught by the lowercase
    check too, hence the `tag.upper() == "S"` test rather than a bare
    `== "S"`). Only these two tampering-relevant shapes are returned;
    ordinary states the same command also reports (`H` cached, `M`/`U`
    unmerged, `R` removed, `C` modified, `K`, `?`) are exactly what `git
    status` already reports honestly on its own and must NOT be flagged
    here, or an ordinary review would trip this check on every run.

    Uses `_run_git`, not a bare `subprocess.run`: a non-zero exit, timeout
    or OSError already raises `WorktreeCheckFailed` there, which is the
    fail-conservative behaviour this check needs — unparseable or
    unreadable index state must never be read as "no flags set." A
    malformed `-z` record (not `"<tag><space><path>"`) is unverifiable
    state for the same reason and also raises rather than being skipped.
    """
    raw = _run_git(repo_path, "ls-files", "-v", "-z", timeout=timeout)
    records = raw.split("\0")
    if records and records[-1] == "":
        records.pop()
    out: dict[str, str] = {}
    for rec in records:
        if not rec:
            continue
        if len(rec) < 3 or rec[1] != " ":
            raise WorktreeCheckFailed(
                f"git ls-files -v returned an unparseable record: {rec!r}")
        tag, path = rec[0], rec[2:]
        if tag.islower() or tag.upper() == "S":
            out[path] = tag
    return out


def _stat_entry(path: Path) -> tuple[int, int, str]:
    """`(mode, size, sha256)` — deliberately NOT mtime. See `_git_dir_inventory`."""
    try:
        st = path.stat()
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise WorktreeCheckFailed(f"could not stat {path}: {exc}") from exc
    return (stat.S_IMODE(st.st_mode), st.st_size, _content_hash(path))


def _symlink_entry(path: Path) -> tuple[int, int, str]:
    # A symlinked hook is executed by git exactly like a regular file at that
    # path (`ln -s /tmp/payload post-checkout`) — the earlier revision of
    # this guard skipped `path.is_symlink()` paths entirely, which made a
    # symlinked hook invisible to the inventory. We must NOT follow the link
    # (reading whatever it points at could touch an arbitrary path outside
    # the repo, hang on a FIFO/device, or simply be a dangling target that
    # raises on `.stat()`); instead the link itself — its mode and target
    # string — is the thing being inventoried, via `lstat`/`readlink`, which
    # never dereference.
    try:
        st = path.lstat()
        target = str(path.readlink())
    except OSError as exc:
        raise WorktreeCheckFailed(f"could not read symlink {path}: {exc}") from exc
    digest = hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest()
    # Mtime omitted for the same reason as `_stat_entry`: retargeting the link
    # changes `digest`, and `chmod` changes the mode. Touching it changes
    # neither, and changes nothing git will do.
    return (stat.S_IMODE(st.st_mode), len(target), digest)


def _iter_git_paths(
    root: Path, skip_dir_prefixes: frozenset[str]
) -> Iterator[tuple[Path, bool]]:
    """Depth-first walk of *root*, yielding `(path, is_symlink)` for every
    symlink and regular file underneath it.

    A symlink is always a LEAF here, never descended through — matching
    `_symlink_entry`'s never-dereference contract, and closing the
    symlinked-hook gap an earlier revision of this guard had (a symlinked
    directory under `.git` is exactly as inventoried, by link identity, as a
    symlinked file). A directory whose root-relative path (with a trailing
    "/") is in *skip_dir_prefixes* is PRUNED — its contents are never even
    `iterdir()`-ed — rather than walked and filtered afterwards; this is
    what keeps the walk cheap for a subtree the size of `objects/`. A
    special file (socket, FIFO, device) is silently skipped: it is neither a
    symlink nor `is_file()` nor `is_dir()`.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError as exc:
            raise WorktreeCheckFailed(f"could not walk {current}: {exc}") from exc
        for child in children:
            try:
                is_link = child.is_symlink()
                is_regular_file = (not is_link) and child.is_file()
                is_dir = (not is_link) and not is_regular_file and child.is_dir()
            except OSError as exc:
                raise WorktreeCheckFailed(f"could not stat {child}: {exc}") from exc
            if is_dir:
                rel_dir = f"{child.relative_to(root).as_posix()}/"
                if rel_dir in skip_dir_prefixes:
                    continue
                stack.append(child)
                continue
            if is_link or is_regular_file:
                yield child, is_link


def _walk_git_root(
    root: Path, label: str, *, skip_dir_prefixes: frozenset[str] = frozenset()
) -> dict[str, tuple[int, int, str]]:
    entries: dict[str, tuple[int, int, str]] = {}
    if not root.is_dir():
        return entries
    for path, is_link in sorted(_iter_git_paths(root, skip_dir_prefixes)):
        rel = path.relative_to(root).as_posix()
        if _is_volatile_git_path(rel, label):
            continue
        if is_link:
            # Inventoried by link identity (mode + target), never by
            # dereferencing — see `_symlink_entry`. This is what closes the
            # symlinked-hook gap: `hooks/post-checkout -> /tmp/payload` is a
            # write under `.git` like any other and must be caught here.
            entries[f"{label}/{rel}"] = _symlink_entry(path)
        else:
            entries[f"{label}/{rel}"] = _stat_entry(path)
    return entries


def _git_dir_inventory(repo_path: Path, *, timeout: float) -> dict[str, tuple[int, int, str]]:
    """Recursive inventory of the `.git` subtree: `(mode, size, sha256)` per
    path, covering both the per-worktree admin dir
    (`git rev-parse --absolute-git-dir`) and the shared common dir
    (`git rev-parse --git-common-dir`) — for a *linked* worktree these
    differ, and the hooks that actually fire live under the common dir
    (`push_hook.py:24-31`), not the admin dir alone. Mode is part of the
    tuple so a `chmod +x` on a byte-identical existing file (e.g. a
    `hooks/*.sample`) is still a detected delta.

    THE ENTRY IDENTITY IS EXACTLY WHAT DETERMINES WHAT GIT WILL DO: content
    and mode. `st_mtime_ns` was part of this tuple until 2026-08-25 and made
    a BYTE-IDENTICAL rewrite read as a modification.

    It is NOT true that mtime could never catch anything content+mode miss —
    measured, it did catch a symlink->regular-file swap and a hardlink
    replacement. The claim that survives adversarial measurement is narrower
    and sufficient: mtime is ATTACKER-WRITABLE (`utimensat`, and
    `os.utime(..., follow_symlinks=False)` restores an exact ns), so an
    adversary defeats it for free. It was detection against accidents only,
    at the price below.

    THE PRICE, and the limit of what it establishes: completed review verdicts
    were repeatedly discarded, each reported with a `.git/common/` path —
    git's own bookkeeping, never a source file — and at least one task
    exhausted `max_attempts` and escalated to a human as a result.

    NO COUNT AND NO TASK IDS ARE GIVEN. Two other places in this repo once
    carried them and disagreed: this docstring said three verdicts and named
    one task, `orchestrator.py` said six tasks, and a commit message said
    seven events across six different ones. All were true when written, none
    is checkable from the shipped tree, and a reader could only find them
    contradicting each other. The MECHANISM below is reproducible; that is
    what belongs in permanent source.

    Whether those particular writes left the content
    unchanged is NOT established — this inventory stores hashes, not bytes
    (`revert()` deliberately never touches `.git`-subtree paths, see its
    docstring), so the before/after content of those incident writes was
    never captured anywhere. What is established by reproduction is that
    an identical-bytes rewrite is SUFFICIENT to produce exactly that report.

    ATTRIBUTION — the residual, stated rather than papered over. This
    inventory covers the common dir, which on a linked-worktree installation
    is SHARED: the primary checkout, every concurrent task worktree and any
    human working in the repo all write to the same files. Nothing here can
    establish that a change found in the common dir was made by the reviewed
    session rather than by one of those other writers. What the change above
    establishes is narrower and is the whole of what it claims: a write that
    does not alter content or mode is not reported at all, so the ordinary
    case — a tool rewriting a file with the bytes it already had — no longer
    discards a verdict. A genuine concurrent CONTENT change to the shared
    git dir is still attributed to the reviewer, and still fails closed.
    That direction is the safe one, and it is rare where the identical-bytes
    rewrite was routine, but it is not fixed here and must not be described
    as if it were.

    TRANSIENT STATE — the second residual, and the one dropping mtime makes
    sharper. `snapshot` and `compare` observe two ENDPOINTS; they never see the
    trajectory between them. A payload written over an existing hook, FIRED,
    and then restored to its exact bytes and mode executes with an empty delta.
    Measured against this revision and against main: main happens to catch the
    bytes-and-mode-restored case only because its identity tuple still carried
    mtime, and it falls to the same attack the moment the attacker also covers
    mtime. So mtime was never a defence — it was an accident that tripped the
    careless version of this attack — and removing it is still right, because
    mtime is attacker-writable and its false positives were destroying real
    verdicts. But the accident IS gone, and this is where a reader would
    otherwise have to rediscover that. Closing this class needs a different
    instrument entirely (watching writes as they happen, not diffing two
    snapshots); it is not attempted here.
    """
    repo_path = Path(repo_path)
    admin_dir = _resolve_git_root(repo_path, "--absolute-git-dir", timeout=timeout)
    common_dir = _resolve_git_root(repo_path, "--git-common-dir", timeout=timeout)
    entries: dict[str, tuple[int, int, str]] = {}
    entries.update(
        _walk_git_root(admin_dir, "admin", skip_dir_prefixes=_SKIPPED_GIT_DIR_PREFIXES))
    if common_dir != admin_dir:
        entries.update(
            _walk_git_root(
                common_dir, "common", skip_dir_prefixes=_SKIPPED_GIT_DIR_PREFIXES))
    pointer = repo_path / ".git"
    try:
        is_pointer_file = pointer.is_file() and not pointer.is_symlink()
    except OSError as exc:
        raise WorktreeCheckFailed(f"could not stat {pointer}: {exc}") from exc
    if is_pointer_file:
        # Linked worktree: `.git` is a text file (`gitdir: <admin>/...`), not
        # a directory. It lives in the WORKTREE, so neither root walk above
        # reaches it, yet rewriting it repoints the whole admin dir.
        entries["pointer"] = _stat_entry(pointer)
    return entries


def _is_git_subtree_path(path: str) -> bool:
    return path.startswith(".git/")


def snapshot(repo_path: Path, *, timeout: float) -> Snapshot:
    """Capture HEAD plus a content-hashed record of every dirty/untracked
    path, plus a full `.git`-subtree inventory. Clean tracked paths are
    deliberately NOT recorded — `compare` classifies added/modified/deleted
    from git's own status codes on the AFTER snapshot, not from this module's
    memory of what the baseline tree looked like, so a file that was clean at
    snapshot time and dirtied during the review is still correctly reported
    as "modified," never "added."
    """
    try:
        repo_path = Path(repo_path)
    except TypeError as exc:
        raise WorktreeCheckFailed(f"not a usable repo path: {repo_path!r}") from exc
    head = _run_git(repo_path, "rev-parse", "HEAD", timeout=timeout).strip()
    if not head or len(head) < 7 or any(c not in "0123456789abcdef" for c in head):
        raise WorktreeCheckFailed(
            f"HEAD did not resolve to a commit sha in {repo_path}: {head!r}")
    raw = _run_git(
        repo_path,
        "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no",
        timeout=timeout,
    )
    entries: dict[str, str] = {}
    for path, xy in _parse_porcelain_z(raw):
        entries[path] = f"{xy}:{_content_hash(repo_path / path)}"
    git_entries = _git_dir_inventory(repo_path, timeout=timeout)
    common_head: str | None = None
    admin_dir = _resolve_git_root(repo_path, "--absolute-git-dir", timeout=timeout)
    common_dir = _resolve_git_root(repo_path, "--git-common-dir", timeout=timeout)
    if common_dir != admin_dir:
        try:
            common_head = (common_dir / "HEAD").read_text(errors="replace")
        except OSError:
            common_head = None  # unreadable reads as "not a symref" -> fail closed
    config_norm = _config_norm_map(admin_dir, common_dir, timeout=timeout)
    index_flags = _index_flags(repo_path, timeout=timeout)
    return Snapshot(head=head, entries=entries, git_entries=git_entries,
                    common_head=common_head, config_norm=config_norm,
                    index_flags=index_flags)


#: One branch symref line, exactly — what `git checkout <branch>` writes.
#: A raw sha (detached), an empty file, or any extra content does not match.
_BRANCH_SYMREF = re.compile(r"ref: refs/heads/\S+\n?\Z")


def _is_branch_symref(content: str | None) -> bool:
    return content is not None and _BRANCH_SYMREF.fullmatch(content) is not None


def _bucket(xy: str) -> str:
    if xy == "??":
        return "added"
    if "D" in xy:
        return "deleted"
    if "A" in xy:
        return "added"
    return "modified"


def compare(repo_path: Path, before: Snapshot, *, timeout: float) -> Delta:
    """Re-snapshot and diff structurally against *before*.

    A changed HEAD (e.g. the reviewer ran `git commit`) is reported as a
    synthetic ``HEAD:<old>-><new>`` modification entry even when it leaves
    the working tree itself perfectly clean — `revert` refuses that case
    outright rather than guessing at undoing a commit.

    Any added/removed/changed `.git`-subtree entry (see `_git_dir_inventory`)
    is reported through the SAME added/modified/deleted lists, prefixed
    `.git/`, so it rides the one `reviewer_wrote` event + verdict-discard
    path the worktree-file delta already uses — one code path, one event
    kind, one discard.
    """
    repo_path = Path(repo_path)
    after = snapshot(repo_path, timeout=timeout)
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    benign: list[str] = []
    benign_keys: list[str] = []
    nonbenign_keys: list[str] = []
    for path in sorted(set(before.entries) | set(after.entries)):
        b = before.entries.get(path)
        a = after.entries.get(path)
        if a == b:
            continue
        if a is None:
            # No longer dirty/untracked. Only interesting if the path is
            # actually gone from disk — a reviewer "un-dirtying" a
            # pre-existing dirty file leaves no residual effect to report.
            if not (repo_path / path).exists():
                deleted.append(path)
            continue
        a_status = a.split(":", 1)[0]
        bucket = _bucket(a_status)
        if bucket == "added" and b is not None:
            # Content changed under a path that was ALREADY untracked before
            # the session — not a newly-created file, so it is reported (and
            # reverted) as a modification, not an addition.
            bucket = "modified"
        {"added": added, "modified": modified, "deleted": deleted}[bucket].append(path)
    for path in sorted(set(before.git_entries) | set(after.git_entries)):
        b = before.git_entries.get(path)
        a = after.git_entries.get(path)
        nonbenign_for_this_path: list[str] = []
        if a == b:
            continue
        if path == "common/HEAD" and a is not None and b is not None:
            # SHAPE, not writer: an ordinary branch switch in the primary
            # checkout rewrites the shared HEAD as one branch symref line to
            # another, and nothing here can tell who wrote it. Only that
            # shape is excused. A raw sha (detach), garbage, or a side this
            # snapshot could not read stays a violation — see
            # `_is_volatile_git_path`'s docstring for why the path is not
            # simply skipped.
            if (_is_branch_symref(before.common_head)
                    and _is_branch_symref(after.common_head)):
                continue
        if (path.rsplit("/", 1)[-1] in _CONFIG_FILENAMES
                and a is not None and b is not None):
            # CONTENT, not bytes — the same shape of adjudication as
            # `common/HEAD` above, for the same reason. config stays WATCHED
            # (it is an exec-on-checkout surface: `include.path`, `alias.*`,
            # smudge/clean/textconv filters, `core.hooksPath`), but git and
            # concurrent writers of the SHARED common dir re-serialize it with
            # the same effective keys — different whitespace, section order,
            # comments — on ordinary bookkeeping. Byte-hashing that discarded
            # completed verdicts with a `.git/common/config` path (the
            # incident in `_git_dir_inventory`'s docstring, which the earlier
            # mtime-drop only fixed for a bit-identical rewrite). A change to
            # the EFFECTIVE set — every new/changed key, including the
            # `include.path` `git pwn` the byte-hash was watching for — is not
            # equal and still reported; an unparseable/unreadable file has no
            # snapshot value and also falls through, fail-closed.
            bn = before.config_norm.get(path)
            an = after.config_norm.get(path)
            if bn is not None and an is not None:
                if bn == an:
                    continue
                # Real key-set change. Excuse it ONLY when every changed key
                # is on the bookkeeping allowlist (see
                # `_BENIGN_CONFIG_KEY_PATTERNS`) — the exact shape of the 31
                # recorded `.git/common/config` false-discards, all zero
                # tracked-path changes and all confined to keys git/tooling
                # write to itself. A single non-allowlisted key among the
                # changed set (`include.path`, `alias.*`, ...) keeps the
                # whole file a violation, same fail-closed shape as an
                # unparseable file below.
                bkv = _config_keyvals(bn)
                akv = _config_keyvals(an)
                changed = {k for k in set(bkv) | set(akv)
                           if bkv.get(k) != akv.get(k)}
                if changed and all(_is_benign_config_key(k) for k in changed):
                    benign.append(f".git/{path}")
                    benign_keys.extend(sorted(changed))
                    continue
                nonbenign_for_this_path = sorted(
                    k for k in changed if not _is_benign_config_key(k))
        display = f".git/{path}"
        if nonbenign_for_this_path:
            shown_keys = nonbenign_for_this_path[:_MAX_NONBENIGN_KEYS_SHOWN]
            more = len(nonbenign_for_this_path) - len(shown_keys)
            display += (" (non-benign keys: " + ", ".join(shown_keys)
                        + (f" and {more} more" if more else "") + ")")
            nonbenign_keys.extend(shown_keys)
        if a is None:
            deleted.append(display)
        elif b is None:
            added.append(display)
        else:
            modified.append(display)
    # Index FLAG BITS (assume-unchanged / skip-worktree) — see
    # `_index_flags` and the "index" entry in `_VOLATILE_GIT_EXACT`.
    # Reporting ANY path flagged in `after` (not merely a before/after
    # difference) is deliberate — the only entry in this module whose
    # baseline is "no flags set," not "the same as before": a bit already
    # set at snapshot time blinds `git status --porcelain` to that path for
    # the WHOLE review, so an unchanged-since-`before` flag is still not
    # safe. A product-built reviewer worktree (`GitRepo.add_worktree`) never
    # sets these bits, so this costs no false positives against ordinary
    # use. The before-side is still consulted so a bit CLEARED during the
    # review — also a change to the filter through which `git status` sees
    # the tree — is reported too, as `[flag cleared]` rather than a tag.
    for path in sorted(set(before.index_flags) | set(after.index_flags)):
        if path in after.index_flags:
            modified.append(f".git/index:{path} [{after.index_flags[path]}]")
        else:
            modified.append(f".git/index:{path} [flag cleared]")
    if before.head != after.head:
        modified.append(f"HEAD:{before.head}->{after.head}")
    return Delta(added=sorted(added), modified=sorted(modified), deleted=sorted(deleted),
                 benign=sorted(benign), benign_keys=sorted(set(benign_keys)),
                 nonbenign_keys=sorted(set(nonbenign_keys)))


def revert(repo_path: Path, before: Snapshot, delta: Delta, *, timeout: float) -> None:
    """Targeted revert of exactly the WORKTREE paths *delta* names — never
    `git reset --hard` / `git clean -fdx` / `git stash`.

    `.git`-subtree entries in *delta* (prefixed `.git/`, see `compare`) are
    deliberately NOT touched here: there is no generic way to restore
    hook-directory content from a hash-only inventory, and the security
    consequence — a rigged verdict — is already neutralised by the caller
    discarding it once this function returns. This function's job is to
    restore the checked-out worktree without ever executing a repo-local
    hook while doing so; it must not choke on paths `git reset`/`git
    checkout` were never going to understand. (Parked follow-up, not
    implemented: physically removing a planted `.git`-subtree file.)

    Every git call below passes `-c core.hooksPath=<empty dir>` (so a
    planted hook cannot fire) and `-c core.fsmonitor=false` (same rationale
    as `repo_discovery.py:306` — fsmonitor spawns a user-configured
    process). Both are written literally at each call site, not routed
    through a shared `cmd`-taking helper, to keep the egress-allowlist
    scanner's `["git", ...]`-literal classification intact
    (`src/no_human/vcs/git.py:167-172`), and never through `GitRepo._run`,
    whose own prefix has no hooks override and must keep firing the
    load-bearing `pre-push` hook.

    Verifies itself by re-diffing against *before* once done; a residual
    difference in the WORKTREE portion raises rather than returning quietly,
    since a silent partial revert is exactly the "probably fine" outcome
    this module refuses to produce.
    """
    repo_path = Path(repo_path)
    if any(entry.startswith("HEAD:") for entry in delta.modified):
        raise WorktreeCheckFailed(
            "the reviewer session moved HEAD (e.g. committed) — that cannot "
            "be undone by a targeted revert without rewriting history")
    worktree_added = [p for p in delta.added if not _is_git_subtree_path(p)]
    worktree_modified = [p for p in delta.modified if not _is_git_subtree_path(p)]
    worktree_deleted = [p for p in delta.deleted if not _is_git_subtree_path(p)]

    empty_hooks_dir = tempfile.mkdtemp(prefix="no_human-revert-hooks-")
    try:
        for path in worktree_added:
            full = repo_path / path
            try:
                if full.is_symlink() or full.exists():
                    full.unlink()
            except OSError as exc:
                raise WorktreeCheckFailed(
                    f"could not remove reviewer-created path {path}: {exc}") from exc
            _run_git(
                repo_path,
                "-c", f"core.hooksPath={empty_hooks_dir}", "-c", "core.fsmonitor=false",
                "reset", "--", path,
                timeout=timeout)
        for path in [*worktree_modified, *worktree_deleted]:
            _run_git(
                repo_path,
                "-c", f"core.hooksPath={empty_hooks_dir}", "-c", "core.fsmonitor=false",
                "checkout", before.head, "--", path,
                timeout=timeout)
    finally:
        shutil.rmtree(empty_hooks_dir, ignore_errors=True)

    residual = compare(repo_path, before, timeout=timeout)
    residual_worktree = Delta(
        added=[p for p in residual.added if not _is_git_subtree_path(p)],
        modified=[p for p in residual.modified if not _is_git_subtree_path(p)],
        deleted=[p for p in residual.deleted if not _is_git_subtree_path(p)],
    )
    if not residual_worktree.is_empty():
        raise WorktreeCheckFailed(
            "revert did not restore the reviewed baseline; residual delta: "
            f"added={residual_worktree.added} modified={residual_worktree.modified} "
            f"deleted={residual_worktree.deleted}")


def guard_config(config: dict | None) -> float:
    """Timeout (seconds) for every git call this module makes, from
    `pipeline.reviewer_worktree_guard.timeout_seconds`.

    Mirrors `review_routing.routing_config`'s tolerance of the
    `pipeline: None` deep-merge shape and of a malformed value — a bad
    config value here must not silently widen the check into "wait forever"
    or "never wait," so both fall back to the documented default.
    """
    section = (config or {}).get("pipeline") or {}
    guard = section.get("reviewer_worktree_guard") or {}
    raw = guard.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_TIMEOUT_SECONDS
    return value
