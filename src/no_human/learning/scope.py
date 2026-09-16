"""Project identity for lessons (B4): sha256 of the normalized remote URL.

WHY THE ABSOLUTE PATH HAD TO STOP BEING THE IDENTITY. Every lesson row was
keyed on ``task.repo_path`` — a checkout path. The same repository cloned at
two paths (or worked on through a per-task git worktree, which is the
product's OWN isolation mechanism) reads as two unrelated projects: lessons
learned in one checkout never surface in the other, and B2's clustering
counts "the same correction in two checkouts" as two one-offs instead of one
recurrence. The thing a lesson is ABOUT is the repository, and the stable
name of a repository is its remote. (This module fixes STORAGE and RECALL;
`corrections.py` still clusters on the checkout path — unifying that on the
scope is deliberately left for a follow-up, so cross-checkout recurrence
counting remains split for now.)

THE IDENTITY: ``"prj:" + sha256(normalized remote URL)``. Normalization
(:func:`normalize_remote_url`): credentials (any userinfo) stripped, scheme
dropped, host lowercased, one trailing ``.git`` and trailing slashes
stripped — so ``https://user:token@GitHub.com/org/repo.git/``,
``git@github.com:org/repo`` and ``ssh://git@github.com/org/repo`` are all ONE
project. The path segment keeps its case: only the host is case-insensitive
by standard, and lowercasing paths would merge repos that some forges keep
distinct.

WHY A HASH AND NOT THE NORMALIZED URL. A remote URL can carry a credential
(``https://user:token@host/…``), and a value derived from it is persisted to
the memories table, rendered in ``nh learnings``, and shipped around in
exports. Normalization already strips the userinfo, but storing ANY URL-shaped
value keeps the question alive forever ("did every path that writes this strip
it?"). The hash closes it structurally: what is persisted is a digest of the
credential-FREE form, the raw URL is not recoverable from it, and a test can
assert the credential appears nowhere in the database file
(``tests/test_project_scope.py``).

TWO COLUMNS, NOT A REWRITE OF ONE. ``memories.project`` keeps the checkout
path: it is what a human reads in ``nh learnings``' blast-radius line (a
sha256 tells the confirming human nothing), and rewriting its values in place
would need the path→remote mapping for repos that may no longer exist on
disk. The scope hash lives in ``memories.project_scope``, and recall matches
on EITHER — scope for rows that have one, path for legacy rows that do not.
Legacy rows are stamped with their scope opportunistically
(``Store.stamp_project_scope``) the next time their repo is actually seen,
which is the only moment the path→remote mapping is knowable at all.

A repo with NO remote has no portable identity; :func:`resolve_project_scope`
returns None and such lessons stay keyed on the path, exactly as before B4 —
the conservative direction, since inventing an identity from a path would
just be the old key wearing a hash.

Everything here is stdlib-only and never raises out of ``resolve_*``; a git
failure means "no scope", never a broken proposal path. No function in this
module logs, returns, or embeds the raw remote URL.
"""

from __future__ import annotations

import hashlib
import re
import subprocess

__all__ = [
    "normalize_remote_url",
    "project_scope_id",
    "resolve_project_scope",
    "repo_root",
]

SCOPE_PREFIX = "prj:"

# scp-like remote: [user@]host:path — a colon NOT followed by "//" and no
# slash before it (which would make it a local path like /srv/git:odd).
_SCP_LIKE = re.compile(r"^(?:(?P<user>[^/@]+)@)?(?P<host>[^/:]+):(?!//)(?P<path>.+)$")

_GIT_TIMEOUT = 10  # seconds; a hung `git` must not hang a proposal path


def normalize_remote_url(url: str | None) -> str:
    """The credential-free canonical form of a git remote URL, or ``""``.

    ``https://user:token@GitHub.com/Org/Repo.git/`` → ``github.com/Org/Repo``
    ``git@github.com:Org/Repo.git``               → ``github.com/Org/Repo``
    ``ssh://git@github.com/Org/Repo``             → ``github.com/Org/Repo``

    Scheme and userinfo (credentials) are dropped, the host is lowercased, one
    trailing ``.git`` and any trailing slashes are stripped. A local-path
    remote (``/srv/git/repo.git``, ``file:///srv/git/repo``) normalizes to its
    path with the same suffix rules and no host lowering.
    """
    s = (url or "").strip()
    if not s:
        return ""
    host = ""
    rest = s
    if "://" in s:
        rest = s.split("://", 1)[1]
    else:
        m = _SCP_LIKE.match(s)
        if m:
            rest = f"{m.group('host')}/{m.group('path')}"
        elif s.startswith(("/", ".")):
            rest = s  # local path remote: no scheme, no host
    if not s.startswith(("/", ".")) or "://" in s:
        host, _, path = rest.partition("/")
        host = host.rsplit("@", 1)[-1].lower()  # strip userinfo/credentials
        rest = f"{host}/{path}" if path else host
    rest = rest.rstrip("/")
    if rest.endswith(".git"):
        rest = rest[: -len(".git")]
    return rest.rstrip("/")


def project_scope_id(url: str | None) -> str | None:
    """``prj:<sha256 hex>`` of the normalized remote URL, or None for an
    empty/unnormalizable one. The stored value: a fixed-shape digest that a
    credential can never survive into."""
    norm = normalize_remote_url(url)
    if not norm:
        return None
    return SCOPE_PREFIX + hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _git(repo_path: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def resolve_project_scope(repo_path: str | None) -> str | None:
    """The project scope for a checkout path, or None (no repo, no remote,
    git unavailable). Prefers ``origin``; otherwise the first remote listed —
    deterministic, since ``git remote`` sorts its output.

    Never raises and never returns the URL itself: the caller gets the hash
    or nothing, so no caller can accidentally persist what this function saw.
    """
    path = (repo_path or "").strip()
    if not path:
        return None
    url = _git(path, "remote", "get-url", "origin")
    if not url:
        remotes = _git(path, "remote").splitlines()
        if remotes:
            url = _git(path, "remote", "get-url", remotes[0].strip())
    return project_scope_id(url)


def repo_root(path: str | None) -> str | None:
    """The working-tree root containing *path*, or None when it is not inside
    a git checkout. `nh recall` scopes by this: lessons are keyed on the root
    the task ran in, not on whatever subdirectory the command runs from."""
    if not (path or "").strip():
        return None
    return _git(str(path), "rev-parse", "--show-toplevel") or None
