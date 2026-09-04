"""Find the user's repositories instead of asking them to type a path.

The first thing onboarding and the task composer ask for is a filesystem path,
typed from memory. This module walks the conventional clone roots under the
user's home and returns a pickable list.

Three properties matter more than coverage:

* **Bounded.** Depth is capped at :data:`DEFAULT_MAX_DEPTH` levels below each
  root, the well-known dependency/build directories are never entered, a repo
  is a leaf (we do not descend into one), and the result count is capped. When
  the cap bites, the response says so — a silently truncated list is a list
  the user cannot trust.
* **Contained — for configured roots.** Every conventional root and every
  operator-configured ``extra_roots`` entry is resolved and must land inside
  the resolved ``home``; one that does not is refused and reported, in both
  ``roots_refused`` (strings, kept for compatibility) and ``refusals``
  (``{path, reason}``). Every directory entered below such a root is likewise
  checked, so a symlink pointing out of home is never followed. The rule is
  containment after resolution, not "no symlinks": ``~/Code ->
  ~/actual-clones`` is scanned, ``~/Code -> /Volumes/BigDisk`` is refused. A
  user-**typed** ``root`` (the "type a folder to scan it" path) is the one
  exception: it is scanned wherever it resolves, never refused for being
  outside home — the user pointed at it on purpose, and ``GET /api/fs/suggest``
  already lists directories anywhere on disk, so this is no wider a surface.
  A symlink under a typed root still may not lead outside *that* root.
* **Fast.** The walk is pure ``os.scandir`` under its own wall-clock budget
  (:data:`WALK_BUDGET_S`) — a network-mounted root cannot stall the request, it
  can only truncate the answer, and a truncated answer says so. The only
  subprocess is one ``git status`` per *returned* repo (after the cap), run on
  a small thread pool, so a wide tree costs one status per row shown and
  nothing more.

Nothing in here may raise on a filesystem it merely cannot read. A single
``chmod 000`` directory used to take the whole response down with a
``PermissionError`` — ``Path.exists()`` swallows ENOENT but not EACCES — and
this endpoint is on the first-run onboarding path, where a 500 is the user's
first impression of the product. Unreadable entries are skipped; the rest of
the list still comes back.

``dirty`` is the reason the git probe is worth its cost: pointing an agent at
a repository the user is mid-edit in is how uncommitted work gets lost, and
the picker should show that before the click, not after.
"""
from __future__ import annotations

import logging
import os
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

#: Where developers actually clone things, in the order they are offered.
CONVENTIONAL_ROOTS: tuple[str, ...] = (
    "Projects", "Code", "Development", "Dev", "repos", "git", "workspace", "src",
)

#: Levels below a root. Three, because the common layouts are ``<root>/<repo>``
#: (1), ``<root>/<owner>/<repo>`` (2) and ``<root>/<host>/<owner>/<repo>`` (3) —
#: the last is what a host-namespaced checkout looks like, and stopping at two
#: would miss every repo on such a machine. Four buys little and starts walking
#: into monorepo subprojects and unpacked archives.
DEFAULT_MAX_DEPTH = 3

#: Rows returned. Past this the list stops being pickable anyway, and the
#: response carries a note instead of quietly dropping the tail.
DEFAULT_MAX_RESULTS = 200

#: Direct children of ``home`` the home-root scan never enters. These are the
#: macOS TCC-guarded folders (Documents/Desktop/Downloads and the media dirs) —
#: stat-ing ``.git`` inside them is what raised the "wants to access" prompt
#: during setup — plus ``Library``, which holds no repos worth the walk. Home is
#: a depth-1 root precisely so a user whose repos sit directly under ``~`` is
#: found without descending into any of these.
PROTECTED_HOME_DIRS = ("Desktop", "Documents", "Downloads", "Library",
                       "Pictures", "Movies", "Music")

#: Never entered. Hidden directories are skipped by name as well, so ``.venv``
#: and ``.tox`` are covered twice over — the explicit names document intent.
EXCLUDED_DIRS = frozenset({
    "node_modules", ".venv", "venv", "vendor", "target", "build", "dist",
    "__pycache__", ".git", "Library", "Applications", ".Trash",
    ".cache", ".gradle", ".m2", "site-packages", "Pods", ".terraform",
})

#: A directory holding one of these but no ``.git`` is still a project the user
#: may want to point a task at, so it is offered with ``is_git: false``.
_MANIFESTS = (
    "package.json", "pyproject.toml", "go.mod", "pom.xml", "Cargo.toml",
    "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
)

#: Ceiling for one tracked-files probe. Cheap on a normal repository (tens of
#: ms) but NOT free on a very large one: measured on this machine, `git status
#: --untracked-files=no` still refreshes the index and took 1.6s on the biggest
#: checkout. Past this, that repo's status is reported unavailable.
GIT_TIMEOUT_S = 1.5

#: Budget for the second, expensive probe (untracked files). Scanning for
#: untracked files means walking the whole working tree, and on a large
#: checkout that is seconds - 98% of the cost of a default `git status`
#: (measured: 2272ms full vs 40ms tracked-only). The picker must not wait, so
#: this probe is capped hard and a repo that blows the cap comes back with
#: `dirty_scan: "partial"` instead of holding up the list.
UNTRACKED_TIMEOUT_S = 0.75

#: ONE budget for all git probing, both passes, not per repo. Per-probe
#: timeouts alone let total wall time grow with the number of large checkouts -
#: exactly the machine where discovery matters most. Repos the deadline never
#: reaches report `unavailable` (tracked probe skipped) or `partial` (tracked
#: clean, untracked pass skipped), so the whole scan costs at most this plus
#: one in-flight probe, no matter how many repositories there are.
#:
#: The path/name/branch half of every row costs no subprocess at all and is
#: never budgeted away - the list itself is always complete.
DIRTY_BUDGET_S = 2.0

#: Wall clock for the DIRECTORY WALK, shared across every root. Separate from
#: (and spent before) `DIRTY_BUDGET_S`, which only ever covered git probing:
#: measured on a real home the split was walk 0.002s / total 2.34s, so the walk
#: looked free and went unbudgeted. It is free on local disk. It is not free on
#: an SMB or sshfs mount, where a single `scandir` can block for seconds, and
#: nothing in the old code could stop it. A blown budget truncates the search
#: and says so (`walk_truncated` plus a note) rather than holding the request.
WALK_BUDGET_S = 1.5

_GIT_WORKERS = 8


def _exists(p: Path) -> bool:
    """``p.exists()`` that reads an unreadable parent as "no", not as a crash.

    ``Path.exists`` deliberately swallows ENOENT/ENOTDIR but re-raises
    everything else, EACCES included. One ``chmod 000`` directory anywhere
    under a scan root would otherwise abort the entire scan.
    """
    try:
        return p.exists()
    except OSError:
        return False


def _is_dir(p: Path) -> bool:
    """``p.is_dir()``, same EACCES treatment as :func:`_exists`."""
    try:
        return p.is_dir()
    except OSError:
        return False


def _scandir(d: Path) -> list[os.DirEntry[str]]:
    """One directory listing. Indirected through the module so a test can make
    it slow and pin the walk budget without patching ``os``."""
    return list(os.scandir(d))


def _quick_ecosystem(repo: Path) -> str:
    if _exists(repo / "package.json"):
        return "node"
    if _exists(repo / "uv.lock") or _exists(repo / "pyproject.toml"):
        return "python"
    if _exists(repo / "pom.xml"):
        return "maven"
    if _exists(repo / "go.mod"):
        return "go"
    return ""


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolved(p: Path) -> Path:
    """``p.resolve()`` that degrades to the unresolved path instead of raising.

    ``ValueError`` as well as ``OSError``: an embedded NUL byte makes the
    underlying ``lstat`` raise ``ValueError``, not ``OSError``, and this is the
    one probe that operator-supplied text reaches BEFORE any filesystem check
    could reject it (``discover_repos`` resolves each ``extra_roots`` entry, and
    ``home``, before ``_is_within`` gets a chance to refuse them). Catching only
    ``OSError`` therefore let a single configured root abort the whole scan with
    a traceback, which contradicts this module's stated contract that it "does
    not raise on anything it merely cannot read".

    A path this cannot resolve is returned unchanged, so it fails the
    ``_is_within`` boundary check and lands in ``roots_refused`` - reported, not
    silently dropped.

    ``_exists``/``_is_dir`` need no such widening: ``pathlib`` already converts
    the NUL ``ValueError`` into ``False`` for those two (verified on 3.12).
    """
    try:
        return p.resolve()
    except (OSError, ValueError):
        return p


def _git_dir(repo: Path) -> Path | None:
    """Resolve ``.git`` whether it is a directory or a worktree pointer file.

    One ``stat`` decides both cases. This is the hottest probe in the walk —
    it runs on every directory visited, almost all of which have no ``.git`` at
    all — so asking twice (``is_dir`` then ``is_file``) would double the
    syscalls of the common miss.
    """
    dot = repo / ".git"
    try:
        mode = dot.stat().st_mode
    except OSError:
        return None
    if stat.S_ISDIR(mode):
        return dot
    if stat.S_ISREG(mode):
        try:
            line = dot.read_text(errors="replace").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            target = Path(line.split(":", 1)[1].strip()).expanduser()
            if not target.is_absolute():
                target = _resolved(repo / target)
            return target if _is_dir(target) else None
    return None


def _is_repo(d: Path) -> bool:
    """True only for a directory that is really a git working tree.

    A *name* ``.git`` is not evidence: unpacked archives, backup folders and
    half-deleted clones all carry one. Accepting the name alone did two kinds
    of damage — it offered a directory the agent cannot work in, and, because a
    repo is a leaf, it hid every genuine repository nested beneath it. The
    cheapest real evidence is ``HEAD``, which is also the file the branch
    readout needs anyway, so this costs nothing extra.

    Absence of evidence is treated as absence, with NO note - a deliberate
    choice, so it is recorded here rather than re-litigated. A repo whose
    ``.git`` this process cannot stat (``chmod 000 .git``) reads as False and,
    measured on a synthetic home:

      * with a manifest alongside it, the directory is still OFFERED, as
        ``is_git: false`` / ``dirty_scan: "not-a-repo"``;
      * without one, it is dropped and the walk descends into it.

    So the two outcomes differ, and a note in the truncation note's style ("some
    repositories were skipped") would be FALSE for the first of them - the
    walk-truncation note is truthful precisely because the walk really did stop.
    Producing a truthful one instead needs the EACCES/ENOENT split threaded out
    of this function, through ``_walk``'s recursion and a ``ThreadPoolExecutor``
    that has no collector. That is a real change, and the case it buys is one no
    scan has hit: a ``.git`` unreadable to this uid is unusable by the agent's
    git calls too, so there is nothing to offer even once it is named.
    """
    gd = _git_dir(d)
    return gd is not None and _exists(gd / "HEAD")


def _is_bare_repo(d: Path) -> bool:
    """A bare repository: git metadata at the top level, no working tree.

    Not offered — there is no checkout to point a task at — and, more to the
    point, not descended into: walking one means enumerating ``objects/``,
    ``refs/`` and ``hooks/`` for nothing. The ``HEAD`` probe comes first so the
    ordinary directory pays one stat, not four.
    """
    return (
        _exists(d / "HEAD")
        and _is_dir(d / "objects")
        and _is_dir(d / "refs")
        and _exists(d / "config")
    )


def _head_info(repo: Path) -> tuple[str, bool]:
    """(branch-or-short-sha, detached) read straight off ``HEAD`` — no subprocess."""
    gd = _git_dir(repo)
    if gd is None:
        return "", False
    try:
        head = (gd / "HEAD").read_text(errors="replace").strip()
    except OSError:
        return "", False
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        # Strip the refs/heads/ PREFIX only. Taking the last path segment loses
        # everything before the slash in the branch names people actually use
        # ("feat/x" would read as "x").
        for prefix in ("refs/heads/", "refs/remotes/"):
            if ref.startswith(prefix):
                return ref[len(prefix):], False
        return ref, False
    if head:
        return head[:8], True
    return "", False


def _mtime(repo: Path) -> float | None:
    """Recency of a repo, read off git's own metadata — no subprocess.

    The newest mtime among ``.git/HEAD``, ``.git/index`` and ``.git/FETCH_HEAD``
    that exist: HEAD moves on commit/checkout, index on ``git add``, FETCH_HEAD
    on fetch/pull, so the max tracks "last touched" better than any one alone.
    None when the repo has no readable git dir — those rows sort last.
    """
    gd = _git_dir(repo)
    if gd is None:
        return None
    times: list[float] = []
    for name in ("HEAD", "index", "FETCH_HEAD"):
        p = gd / name
        try:
            if p.exists():
                times.append(p.stat().st_mtime)
        except OSError:
            continue
    return max(times) if times else None


def _git_status(repo: Path, untracked: str, timeout: float) -> str | None:
    """``git status --porcelain`` output, or None when it could not be read.

    None means "no answer" (timeout, broken repo, git missing) and is kept
    distinct from "" (answered: clean) - the caller reports the difference
    rather than passing a guess off as a reading. ``GIT_CEILING_DIRECTORIES``
    stops git walking upward into an unrelated parent repository when ``.git``
    here turns out to be unusable.
    """
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CEILING_DIRECTORIES"] = str(repo.parent)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "--no-optional-locks",
             "-c", "core.fsmonitor=false", "status", "--porcelain",
             f"--untracked-files={untracked}", "--ignore-submodules=all"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:  # noqa: BLE001
        log.debug("status probe (-u%s) gave no answer for %s: %s",
                  untracked, repo, type(exc).__name__)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


#: Internal only, never returned: "the cheap probe found no tracked edits, so
#: this row still needs the untracked pass".
_PENDING = "pending"


def _describe_cheap(repo: Path, deadline: float) -> dict[str, Any]:
    """Everything a row needs except the expensive untracked verdict."""
    is_git = _is_repo(repo)
    branch, detached = _head_info(repo) if is_git else ("", False)
    row: dict[str, Any] = {
        "path": str(repo),
        "name": repo.name,
        "is_git": is_git,
        "branch": branch,
        "detached": detached,
        "dirty": False,
        "dirty_scan": "not-a-repo",
        "ecosystem": _quick_ecosystem(repo),
        "mtime": _mtime(repo) if is_git else None,
    }
    if not is_git:
        return row
    if time.monotonic() >= deadline:
        row["dirty_scan"] = "unavailable"
        return row
    tracked = _git_status(repo, "no", GIT_TIMEOUT_S)
    if tracked is None:
        row["dirty_scan"] = "unavailable"
    elif tracked.strip():
        # Already proven dirty - the expensive pass would change nothing.
        row["dirty"] = True
        row["dirty_scan"] = "complete"
    else:
        row["dirty_scan"] = _PENDING
    return row


def _untracked_pass(rows: list[dict[str, Any]], deadline: float) -> None:
    """Resolve the pending rows in place, against the shared deadline."""

    def probe(row: dict[str, Any]) -> None:
        if row["dirty_scan"] != _PENDING:
            return
        if time.monotonic() >= deadline:
            row["dirty_scan"] = "partial"
            return
        out = _git_status(Path(row["path"]), "normal", UNTRACKED_TIMEOUT_S)
        if out is None:
            # Tracked files are provably clean; untracked is unknown. Saying
            # "clean" outright would overstate what was measured.
            row["dirty_scan"] = "partial"
        else:
            row["dirty"] = bool(out.strip())
            row["dirty_scan"] = "complete"

    pending = [r for r in rows if r["dirty_scan"] == _PENDING]
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=_GIT_WORKERS) as pool:
        list(pool.map(probe, pending))


def _walk(root: Path, boundary: Path, max_depth: int, ceiling: int,
          found: list[Path], deadline: float,
          skip: frozenset[str] = frozenset()) -> bool:
    """Collect candidate project directories under ``root``, depth-bounded.

    ``boundary`` is the symlink escape hatch this walk must not take — for a
    home-contained root that is ``home`` itself; for a user-typed root that
    resolves outside home it is the typed root, so a link inside it still
    cannot lead back out.

    Returns True if the wall-clock ``deadline`` cut the walk short — the caller
    reports that rather than passing a truncated list off as a complete one.
    Whatever was found before the cut is kept.

    ``skip`` names are never entered — used for the home root, whose direct
    children include the macOS TCC-guarded dirs (:data:`PROTECTED_HOME_DIRS`)
    that must not be stat-ed at all.
    """
    truncated = False

    def visit(d: Path, depth: int) -> None:
        nonlocal truncated
        if len(found) >= ceiling:
            return
        if time.monotonic() >= deadline:
            truncated = True
            return
        if _is_repo(d):
            found.append(d)
            return  # a project is a leaf - never descend into one
        if _is_bare_repo(d):
            return  # no working tree to offer, and nothing inside worth walking
        if any(_exists(d / m) for m in _MANIFESTS):
            found.append(d)
            return
        if depth >= max_depth:
            return
        try:
            entries = _scandir(d)
        except OSError:
            return
        for e in sorted(entries, key=lambda x: x.name):
            try:
                if not e.is_dir(follow_symlinks=True):
                    continue
                is_link = e.is_symlink()
            except OSError:
                continue
            if e.name.startswith(".") or e.name in EXCLUDED_DIRS or e.name in skip:
                continue
            child = Path(e.path)
            if is_link:
                try:
                    target = child.resolve()
                except OSError:
                    continue  # a cycle or a dead link: refuse, do not guess
                # A link out of the boundary is exactly the escape hatch this
                # walk must not take.
                if not _is_within(target, boundary):
                    continue
            visit(child, depth + 1)

    visit(root, 0)
    return truncated


def discover_repos(
    *,
    home: Path | str | None = None,
    extra_roots: Iterable[str] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_results: int = DEFAULT_MAX_RESULTS,
    root: str | None = None,
) -> dict[str, Any]:
    """Scan the conventional clone roots and return a pickable repository list.

    Returns a JSON-ready dict: ``repos`` (path/name/is_git/branch/dirty/
    detached/ecosystem/``mtime``), the roots actually scanned, missing and
    refused (``roots_refused`` as strings, ``refusals`` as ``{path, reason}``),
    the cap state, ``walk_truncated`` (the walk ran out of wall clock, so some
    folders were never reached), a human-readable ``note`` covering both of
    those, ``home_direct`` (repos found directly under ``home``) and
    ``elapsed_ms``. Rows sort newest-first by ``mtime`` (None last), then name.

    ``home`` itself is a depth-1 root — the common case of repos cloned straight
    under ``~`` — scanned without descending into :data:`PROTECTED_HOME_DIRS`
    (the macOS TCC-guarded folders) so setup never triggers an access prompt.

    ``root``: when given, ONLY that single folder is scanned (still at
    ``max_depth``), and the conventional/home roots are skipped. Unlike
    ``extra_roots``, a typed ``root`` is scanned wherever it resolves, even
    outside ``home`` — this is the "type a folder to scan it" path, and the
    user's own typed intent is trusted the same way ``GET /api/fs/suggest``
    already trusts it for browsing. A symlink under the typed root still
    cannot lead outside that root. A ``root`` that does not exist lands in
    ``roots_missing``, not ``roots_refused``.

    It does not raise on anything it merely cannot read. An unreadable
    directory, a dead symlink or a root that vanished mid-scan is skipped and
    the rest of the list still comes back.
    """
    t0 = time.perf_counter()
    home_path = Path(home).expanduser() if home is not None else Path.home()
    home_path = _resolved(home_path)

    scanned: list[str] = []
    missing: list[str] = []
    refused: list[str] = []
    refusal_details: list[dict[str, str]] = []

    def _expand(text: str) -> Path:
        """``~``/``~/`` mean the home this scan is bound to, not the process's
        home — anything else would let configured text reach outside the
        boundary every other line here defends."""
        text = text.strip()
        if text == "~":
            return home_path
        if text.startswith("~/"):
            return home_path / text[2:]
        return Path(text)

    def _contain(text: str, reason: str = "outside home directory") -> Path | None:
        """Resolve a caller-supplied root and refuse it if it escapes ``home``.

        Returns None (and records the refusal, both in ``refused`` and in the
        structured ``refusal_details``) for a root that resolves outside
        ``home``.
        """
        p = _resolved(_expand(text))
        if not _is_within(p, home_path):
            refused.append(str(p))
            refusal_details.append({"path": str(p), "reason": reason})
            return None
        return p

    def _typed_root(text: str) -> Path:
        """Resolve a user-**typed** ``root`` with the same ``~`` expansion as
        :func:`_contain`, but with no containment check and no refusal — the
        user pointed here on purpose (see the module docstring)."""
        return _resolved(_expand(text))

    protected = frozenset(PROTECTED_HOME_DIRS)
    # (root, depth, skip-names, report-in-roots_scanned, symlink-boundary).
    walk_specs: list[tuple[Path, int, frozenset[str], bool, Path]] = []

    if root is not None:
        # "Type a folder to scan it": ONLY that root, scanned wherever it
        # resolves (see docstring), conventional and home roots skipped. Its
        # own resolved path is the symlink boundary, not ``home_path``.
        p = _typed_root(str(root))
        walk_specs.append((p, max_depth, frozenset(), True, p))
    else:
        candidate_roots: list[Path] = []
        # Case-sensitive filesystems (Linux) keep ``~/code`` and ``~/Code``
        # apart; APFS and NTFS fold them, which is why the list above only ever
        # needed one spelling. Measured on a real Ubuntu 24.04 desktop
        # (2026-08-18): a user's ``~/code/calc`` was invisible to the scan. So
        # every on-disk case variant of a conventional name is a root, and the
        # canonical spelling stands in only when no variant exists (so
        # ``roots_missing`` still names it). On a folding filesystem the variants
        # collapse to one real directory, and ``seen`` keeps it from being
        # scanned twice.
        try:
            by_fold: dict[str, list[Path]] = {}
            for entry in home_path.iterdir():
                try:
                    if entry.is_dir():
                        by_fold.setdefault(entry.name.lower(), []).append(entry)
                except OSError:
                    continue
        except OSError:
            by_fold = {}
        seen: set[Path] = set()
        for name in CONVENTIONAL_ROOTS:
            for cr in sorted(by_fold.get(name.lower()) or [home_path / name]):
                # The conventional roots were the ONE path built without this
                # check, and `~/Code -> /Volumes/BigDisk/code` is an ordinary
                # setup - so the walk left home through the front door while
                # refusing every side entrance. Report the link, not just its
                # target: "/Volumes/BigDisk" on its own does not tell the user
                # which of their folders did it.
                target = _resolved(cr)
                if target in seen:
                    continue
                seen.add(target)
                if target != cr and not _is_within(target, home_path):
                    refused.append(f"{cr} -> {target}")
                    refusal_details.append({
                        "path": f"{cr} -> {target}",
                        "reason": "outside home directory",
                    })
                    continue
                candidate_roots.append(cr)

        for raw in extra_roots or []:
            if not str(raw).strip():
                continue
            p = _contain(str(raw))
            if p is not None and p not in candidate_roots:
                candidate_roots.append(p)

        walk_specs = [(cr, max_depth, frozenset(), True, home_path) for cr in candidate_roots]
        # Home itself is a depth-1 root — repos cloned straight under ~, the
        # case a user with dozens of them saw zero results for. Not reported in
        # ``roots_scanned`` (``home_direct`` carries the count); its protected
        # children are never entered, so setup raises no macOS access prompt.
        walk_specs.append((home_path, 1, protected, False, home_path))

    ceiling = max(max_results * 5, 1000)
    found: list[Path] = []
    walk_deadline = time.monotonic() + WALK_BUDGET_S
    walk_truncated = False
    for wroot, wdepth, wskip, wreport, wboundary in walk_specs:
        if time.monotonic() >= walk_deadline:
            walk_truncated = True
            break
        if not _is_dir(wroot):
            if wreport:
                missing.append(str(wroot))
            continue
        if wreport:
            scanned.append(str(wroot))
        if _walk(wroot, wboundary, wdepth, ceiling, found, walk_deadline, wskip):
            walk_truncated = True

    # Dedupe on the RESOLVED path: `Projects/alias -> Projects/plain` is one
    # checkout, and two rows for it is two identical tasks waiting to happen.
    # Where both spellings were found, keep the real one.
    by_real: dict[Path, Path] = {}
    for p in found:
        key = _resolved(p)
        prev = by_real.get(key)
        if prev is None or (prev != key and p == key):
            by_real[key] = p
    # Newest first: a user opens the picker to reach the repo they were just in,
    # not the alphabetically-first one. mtime is None for non-git rows (they
    # sort last); name.lower() then path break ties for a stable list.
    mtimes = {p: _mtime(p) for p in by_real.values()}
    unique = sorted(by_real.values(),
                    key=lambda p: (-(mtimes[p] or 0), p.name.lower(), str(p)))
    home_direct = sum(1 for p in unique if p.parent == home_path)
    total = len(unique)
    capped = total > max_results
    shown = unique[:max_results]

    deadline = time.monotonic() + DIRTY_BUDGET_S
    with ThreadPoolExecutor(max_workers=_GIT_WORKERS) as pool:
        rows = list(pool.map(lambda p: _describe_cheap(p, deadline), shown))
    _untracked_pass(rows, deadline)

    notes: list[str] = []
    if capped:
        notes.append(
            f"Showing the first {max_results} of {total} repositories found - "
            "narrow the scan roots in Settings, or use \"Search another folder\" "
            "to type the path, to reach the rest."
        )
    if walk_truncated:
        notes.append(
            "The search stopped early to keep this page responsive, so some "
            "folders were not reached - use \"Search another folder\" to type a "
            "repository path and use one of them."
        )

    return {
        "repos": rows,
        "roots_scanned": scanned,
        "roots_missing": missing,
        "roots_refused": refused,
        # Structured form of the same refusals, one entry per string above, so
        # a caller can render a per-root reason instead of a bare path.
        "refusals": refusal_details,
        # Short aliases the wizard reads (the sub-heading lists the roots it
        # scanned, and a refused root becomes an inline warning).
        "roots": scanned,
        "refused": refused,
        "home_direct": home_direct,
        "total_found": total,
        "limit": max_results,
        "capped": capped,
        "walk_truncated": walk_truncated,
        "note": " ".join(notes),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


