"""Which code is actually LOADED in this process — not which code is on disk.

The server holds the backend in memory. Merging a fix to main does NOT reload
it, and nothing on the record said so, so a verdict produced by superseded code
was indistinguishable from a verdict produced by the fix. Measured case: task
``ecfe1789`` escalated NOVEL_UNKNOWN on a tamper-guard false positive at 23:49,
3h18m AFTER the commit that fixed exactly that false positive had merged. The
guard on main was correct; the process running it was three hours stale. The
cost lands twice — a real ticket gets a bogus verdict, and the dogfood success
metric attributes the failure to the ticket instead of to staleness.

This module answers "what was in memory?" so an escalation can be RE-JUDGED
afterwards. It deliberately does not answer "should this task be allowed to
run?": HEAD moves constantly during development, so a claim-time
loaded-sha-vs-HEAD block would halt the loop almost permanently, and a guard
that must be disabled to get work done is worse than no guard. Recording is
strictly safe; blocking is not. Nothing here can prevent a claim.

Three honesty requirements shape the API:

- **There may be no sha at all.** Installed from a wheel, the package is a
  directory under ``site-packages`` with no git object behind it. That is
  reported as ``dist:<version>`` (or ``unknown``) — never as a fabricated or
  borrowed sha, and never as the sha of whatever repo the process happens to
  be *cwd*'d into, which is a different thing entirely.

  "Borrowed" is not hypothetical, and being inside a git repository is NOT
  evidence that the repository knows about you. The documented PyPI install
  (``docs/DISTRIBUTION.md``) puts the package under ``.venv/`` inside the
  USER'S project, which is itself a git repo — so ``rev-parse HEAD`` there
  cheerfully returns the user's application commit, and since ``.venv/`` is
  gitignored, ``status --porcelain`` on the package is empty and the answer
  is even labelled *clean*. Attribution therefore requires that the repo
  actually TRACKS the package (``ls-files --error-unmatch``), not merely that
  a repo encloses it. Without that check the failure compounds: when the user
  commits to their own project, our loaded sha becomes a strict ancestor of
  their HEAD and we emit a confident, entirely fabricated "restart to pick up
  merged fixes". A wrong sha is strictly worse than ``unknown`` in the one
  column whose whole purpose is honest provenance.
- **A sha alone can misrepresent a dirty tree.** A checkout with uncommitted
  edits to the package is not the code that sha names, so the descriptor
  carries an explicit ``+dirty`` suffix rather than implying fidelity it does
  not have — and ``+dirty?`` when dirtiness could not be determined at all,
  because silently printing those as clean is the same lie in a quieter voice.
- **The snapshot is taken once, at startup.** ``loaded_code()`` caches, so a
  later call cannot silently answer with a newer HEAD than the one whose code
  is in memory — which would defeat the entire purpose. The HEAD it is
  compared against is re-measured on every status read.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# The directory the `no_human` package was imported FROM. Resolving through
# __file__ (not cwd, not sys.argv) is what makes this a statement about loaded
# code rather than about the shell that happened to launch it.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

_GIT_TIMEOUT = 10


def _git(cwd: Path, *args: str) -> str | None:
    """Run git, returning stripped stdout, or None on any failure.

    Fails soft on purpose: this is telemetry. A missing git binary, a
    non-repository, or a timeout must degrade the recorded value to "unknown",
    never raise into attempt creation.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


@dataclass(frozen=True)
class LoadedCode:
    """A snapshot of the code in memory.

    ``dirty`` is tri-state on purpose: True/False are measurements, and None
    means "unknowable" (there is no working tree to be dirty). Collapsing None
    into False would report an installed wheel as a verified-clean checkout.
    """

    sha: str | None = None          # 40-hex, or None when there is no checkout
    dirty: bool | None = None       # None == unknowable, not "clean"
    dist_version: str | None = None  # only consulted when there is no sha

    @property
    def descriptor(self) -> str:
        """The single string persisted beside an attempt.

        Prefixed by provenance so a reader never has to guess whether the
        payload is a commit: ``git:`` is a real sha, ``dist:`` explicitly is
        not, ``unknown`` claims nothing at all.

        The three dirty states get three distinct spellings. An earlier draft
        rendered None as bare ``git:<sha>`` — identical to verified-clean —
        which quietly undid the tri-state everywhere it actually matters: the
        dataclass is not what anyone reads, ``attempts.loaded_code_version``
        and the status line are, and both take this string. Preserving a
        distinction on an object and collapsing it on the way to the record
        preserves nothing.
        """
        if self.sha:
            suffix = {True: "+dirty", False: "", None: "+dirty?"}[self.dirty]
            return f"git:{self.sha}{suffix}"
        if self.dist_version:
            return f"dist:{self.dist_version}"
        return "unknown"


def _is_tracked(package_root: Path) -> bool:
    """Does the enclosing repo actually track this package?

    The question `rev-parse HEAD` answers is "is there a repo above me", which
    is NOT the question. A pip-installed wheel under `.venv/` sits inside the
    user's own project repo and would otherwise be attributed that project's
    commits. `ls-files --error-unmatch` exits non-zero for a path the index
    does not contain, which is the actual test.

    `__init__.py` is the probe because it is the one file every Python package
    must have; if it is untracked, so is the package. Any failure — no repo, no
    such file, no git — reads as untracked, so this fails CLOSED to `unknown`.
    """
    return _git(package_root, "ls-files", "--error-unmatch",
                str(package_root / "__init__.py")) is not None


def _detect(package_root: Path) -> LoadedCode:
    """Measure the provenance of the package tree at ``package_root``."""
    if not _is_tracked(package_root):
        return LoadedCode(dist_version=_dist_version())
    sha = _git(package_root, "rev-parse", "HEAD")
    if sha and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
        # Scope the dirty check to the package tree itself. A repo-wide
        # `git status` would flag edits to docs, tests or a sibling worktree
        # as "the loaded code is modified", which is false and would make the
        # flag meaningless in exactly the busy repo it exists to describe.
        porcelain = _git(package_root, "status", "--porcelain", "--", str(package_root))
        return LoadedCode(sha=sha, dirty=bool(porcelain) if porcelain is not None else None)
    return LoadedCode(dist_version=_dist_version())


def _dist_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("no_human")
    except Exception:  # noqa: BLE001 — telemetry must never raise
        return None


_SNAPSHOT: LoadedCode | None = None


def loaded_code() -> LoadedCode:
    """The loaded-code snapshot, taken once and cached for the process life.

    Caching is the point, not an optimization: the first call fixes the answer,
    so a call made after main has moved still reports the code in memory.
    Call it early (the server does, in `lifespan`) so the snapshot is taken at
    startup rather than at whatever moment first happens to need it.
    """
    global _SNAPSHOT
    if _SNAPSHOT is None:
        _SNAPSHOT = _detect(_PACKAGE_ROOT)
    return _SNAPSHOT


def head_sha(package_root: Path | None = None) -> str | None:
    """The checkout's current HEAD — the moving half of the staleness compare."""
    return _git(package_root or _PACKAGE_ROOT, "rev-parse", "HEAD")


def staleness_note(info: LoadedCode | None = None,
                   package_root: Path | None = None,
                   head: str | None = None) -> str | None:
    """A human sentence when the loaded sha is a strict ANCESTOR of HEAD.

    Returns None when the code is current, when the tree has diverged rather
    than fallen behind (a detached review checkout is not "stale"), or when
    provenance is unknown. Advisory only — no caller may gate on it.
    """
    info = info if info is not None else loaded_code()
    if not info.sha:
        return None
    root = package_root or _PACKAGE_ROOT
    head = head if head is not None else head_sha(root)
    if not head or head == info.sha:
        return None
    # `--is-ancestor` exits 0 for "yes". Combined with the inequality above,
    # this is strictly-behind: it stays silent for a branch that has simply
    # diverged from HEAD, which is a different situation and not a staleness.
    if _git(root, "merge-base", "--is-ancestor", info.sha, "HEAD") is None:
        return None
    return (f"loaded code {info.sha[:8]} is behind HEAD {head[:8]} — verdicts "
            f"from this process were produced by superseded code; restart to "
            f"pick up merged fixes")
