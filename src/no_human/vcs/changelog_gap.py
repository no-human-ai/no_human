"""The decidable half of "did this release's notes miss something."

Whether a given CHANGELOG.md bullet *covers* a given commit is not decidable
by machine: entries name behaviour, not shas, and one bullet legitimately
covers several commits. What IS decidable — and is all this module answers —
is narrower: which commits in a range changed a user-visible surface and did
not themselves touch CHANGELOG.md. That list is the right input for whoever
writes the release notes; it is not proof that a bullet is missing.

This module is self-contained: it does not import `vcs.git.GitRepo` and does
not need repo config, auth, or the task store. Every git subcommand it shells
out to (``for-each-ref``, ``rev-parse``-free, ``log``, ``show``) is classified
``LOCAL`` in ``tests/test_egress_allowlist.py`` — no allowlist entry needed.

Surface classification below translates the intake answer's categories
(shipped source / desktop app / web board / user-facing docs), which were
phrased for a TypeScript layout (`src/**/*.ts`, `electron/**`, `client/**`)
that does not exist in this repo, onto this repo's actual layout:
`src/no_human/**/*.py` (shipped source), `desktop/**` (the desktop app,
Electron + `.mjs`), `web/**` (the web board), and `docs/**/*.md` /
`README*.md` (user-facing documentation). `migrations/**` is included because
it ships inside the wheel (see pyproject's force-include).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..core.pathglob import matches, normalize_path

#: The ledger itself — a commit that only touches this is not reporting on a
#: gap, it IS a changelog entry.
CHANGELOG_PATH = "CHANGELOG.md"

#: Exclusions are checked FIRST and win over every inclusion below.
NOT_USER_VISIBLE = [
    "tests/**",                  # pytest suite
    "**/*.test.mjs", "**/*.test.js", "**/*.test.cjs",  # desktop/web unit tests
    "**/test_*.py", "**/*_test.py",  # python test modules outside tests/
    "eval/**",                   # eval corpora/harnesses, not shipped
    "testdata/**",               # test fixtures
    "**/fixtures/**",            # test fixtures anywhere
    "desktop/testing/**",        # desktop test harness helpers
    "web/tests/**", "web/e2e/**", "e2e/**",  # web/e2e test suites
    "web/node_modules/**", "desktop/node_modules/**", "web/dist/**",  # deps/build output, not source
    ".github/**", ".gitlab-ci.yml", ".githooks/**",  # CI configuration
    "scripts/**",                # dev/release tooling, not shipped product
    "docs/design/**",            # plan documents, not user-facing docs
    CHANGELOG_PATH,               # the ledger is not a surface it reports on
]
USER_VISIBLE = [
    "src/no_human/**",           # shipped source (the wheel's package root)
    "migrations/**",             # shipped in the wheel (pyproject force-include)
    "desktop/**",                # the desktop app
    "web/**",                    # the web board
    "docs/**/*.md", "README.md", "README.*.md",  # user-facing documentation
]


class ChangelogCheckError(RuntimeError):
    """A git operation needed to answer the check failed (e.g. a bad range).

    Must never be swallowed into "nothing missing" — a bad range reported as
    an empty gap list would be indistinguishable from a clean release.
    """


def is_user_visible_path(path: str) -> bool:
    """True if *path* (repo-relative) is a user-visible surface.

    Exclusions win over inclusions: a path under both an exclude and an
    include pattern (e.g. a hypothetical `src/no_human/tests/foo.py` if such
    a directory existed) is NOT user-visible.
    """
    p = normalize_path(path)
    return not matches(p, NOT_USER_VISIBLE) and matches(p, USER_VISIBLE)


def _git(repo: Path, *args: str, label: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ChangelogCheckError(f"{label} failed: {proc.stderr.strip()}")
    return proc.stdout


def newest_tag(repo: Path) -> str | None:
    """The most recently created tag, or ``None`` if there is none (or the
    repo has no tags/is not a git repo at all — fails soft, not raises, since
    "no tags yet" is a normal state, not an error)."""
    proc = subprocess.run(
        ["git", "for-each-ref", "--sort=-creatordate",
         "--format=%(refname:short)", "--count=1", "refs/tags"],
        cwd=repo, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def resolve_range(repo: Path, spec: str | None) -> tuple[str, str]:
    """Returns ``(range_expr, description)``. *spec* given -> used verbatim
    for both. *spec* is ``None`` -> since the newest tag, or the whole
    history (as plain ``HEAD``, described as such) when there is no tag."""
    if spec:
        return spec, spec
    tag = newest_tag(repo)
    if tag is None:
        return "HEAD", "whole history (no tags)"
    return f"{tag}..HEAD", f"since {tag}"


def commit_paths(repo: Path, sha: str) -> list[str]:
    """Repo-relative paths a commit touches, NUL-delimited (`-z`) so a
    non-ASCII path is not C-quoted."""
    out = _git(repo, "show", "--name-only", "-z", "--format=", "--no-renames",
               sha, label=f"git show {sha}")
    return [p for p in out.split("\0") if p]


def commit_needs_changelog(repo: Path, sha: str) -> bool:
    """True iff *sha* touches a user-visible surface and does not itself
    touch CHANGELOG.md. The single predicate both `nh changelog-check` and
    the `nh approve --landed` warning call, so they can never disagree."""
    paths = commit_paths(repo, sha)
    if CHANGELOG_PATH in paths:
        return False
    return any(is_user_visible_path(p) for p in paths)


@dataclass(frozen=True)
class GapCommit:
    sha: str
    subject: str


def missing_changelog_commits(
    repo: Path, spec: str | None = None,
) -> tuple[list[GapCommit], str]:
    """Commits in the resolved range that change a user-visible surface and
    touch no CHANGELOG.md. Merge commits are excluded (`--no-merges`): a
    merge commit's file list duplicates its parents', so including it would
    double-report the same gap under a second sha.

    Returns ``(gaps, description)`` — *description* is human-readable text
    naming the checked range (e.g. "since v0.2.3" or "whole history (no
    tags)"), for callers to report even when *gaps* is empty.
    """
    range_expr, description = resolve_range(repo, spec)
    out = _git(repo, "log", "--no-merges", "--reverse", "--format=%H%x00%s",
               range_expr, label=f"git log {range_expr}")
    gaps: list[GapCommit] = []
    for line in out.split("\n"):
        line = line.strip("\r")
        if not line:
            continue
        sha, _, subject = line.partition("\0")
        if not sha:
            continue
        if commit_needs_changelog(repo, sha):
            gaps.append(GapCommit(sha=sha, subject=subject))
    return gaps, description
