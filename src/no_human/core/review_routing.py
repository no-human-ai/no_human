"""Review depth scales with diff size (feature, 2026-08-14).

A gate review of a small, benign diff pays for the same multi-turn
exploration budget as a 2,000-line rewrite even though the whole diff, the
full text of every changed file, lint evidence and wiring evidence are
already assembled into the prompt (`review/reviewer.py::_build_review_prompt`)
— the turns exist so the reviewer can go read files it was not shown, and a
small diff has nothing left worth reading that is not already there.

This module is the PRE-PROCESSING FILTER `Orchestrator._run_reviewer` calls
after the trivial-tier escalation checkpoint and before `reviewer.review(...)`
— a pure predicate over what ``git`` reports changed, never a judgment the
model makes about its own diff. `route()` returns ``Route.SINGLE_TURN`` only
when every one of these holds:

  - routing is enabled (``pipeline.review_routing.enabled``)
  - the diff is non-empty and readable
  - total changed (added + deleted) lines <= ``max_diff_lines``
  - `risk_reason()` finds nothing

Any risk-flagged pattern — a guard/scrub function touched, a test file
deleted or renamed away, or a security-sensitive path changed — routes FULL
regardless of size: the cheap lane must never become a bypass for the classes
the reviewer exists to catch. Every negative branch returns FULL, and every
external failure (no git, unreadable diff, binary diff) fails closed to FULL.

Guard/scrub detection is by PATH *and* by diff CONTENT. A path-only check
misses a guard function living in an otherwise-generic file — the never-push
guard (`vcs/push_hook.py::install_pre_push_guard`), the secret redactor
(`agent/verification_receipts.py::_redact`), and the commit-message sanitizer
(`vcs/git.py::_sanitize_commit_message`) all sit in files whose PATHS carry no
guard token at all. `risk_reason`, when given repo/before/after, also scans
the diff's added/removed hunk lines for a guard-family token.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .complexity import review_must_be_unbounded

#: Added + deleted lines, at or under this, is eligible for the single-turn
#: gate. Mirrored by
#: ``DEFAULT_CONFIG["pipeline"]["review_routing"]["max_diff_lines"]``
#: (config.py) — pinned equal by
#: ``tests/test_review_depth_routing.py::test_threshold_default_is_200_and_is_the_only_source``.
MAX_SINGLE_TURN_LINES = 200


class Route(str, Enum):
    SINGLE_TURN = "single_turn"
    FULL_REVIEW = "full_review"


@dataclass(frozen=True)
class Entry:
    """One side of one `git diff --name-status -M` line.

    ``status`` is the raw git status code (``M``, ``A``, ``D``, ``R100``,
    ``C75``, ...). A rename/copy yields TWO entries — one per path — so a
    rename-away-from-``tests/`` and a rename-into-a-guard-sounding-name are
    both visible to `risk_reason` without it having to special-case renames.
    """

    status: str
    path: str


def _normalize(path: str) -> str:
    """Mirrors `complexity._normalize` (not imported: that name is private to
    its own module) — strip quoting/markup a git or task-authored path token
    might carry, and make it repo-root-relative POSIX."""
    return path.strip().strip("`'\"").removeprefix("./").lstrip("/")


_TEST_ROOT_PREFIXES = ("tests/", "test/", "e2e/")


def _is_test_path(path: str) -> bool:
    p = path.casefold()
    base = p.rsplit("/", 1)[-1]
    if p.startswith(_TEST_ROOT_PREFIXES) or any(
        f"/{root}" in p for root in _TEST_ROOT_PREFIXES
    ):
        return True
    if base == "conftest.py":
        return True
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if base.endswith("_test.py"):
        return True
    if base.endswith(".test.ts") or base.endswith(".test.tsx"):
        return True
    if base.endswith(".spec.ts") or base.endswith(".spec.tsx"):
        return True
    return False


#: Guard/scrub family — matched on PATH (basename) and, via
#: `_content_guard_reason`, on diff CONTENT. Kept as one list so a token added
#: here strengthens both checks at once.
_GUARD_TOKENS = ("guard", "scrub", "sanitiz", "redact", "never_push", "tamper")
_GUARD_ROOTS = ("src/no_human/review/", "src/no_human/ci_gate/")


def _is_guard_scrub_path(path: str) -> bool:
    p = path.casefold()
    base = p.rsplit("/", 1)[-1]
    if any(tok in base for tok in _GUARD_TOKENS):
        return True
    return p.startswith(_GUARD_ROOTS)


_SECURITY_PATH_TOKENS = ("auth", "crypt", "secret", "credential", "token", "key", ".env")
_SECURITY_BASENAMES = ("config.yaml", "security.md")
_SECURITY_EXACT_PATHS = ("src/no_human/config.py",)
_SECURITY_PATH_PREFIXES = (".github/workflows/", ".githooks/")


def _is_security_path(path: str) -> bool:
    p = path.casefold()
    base = p.rsplit("/", 1)[-1]
    if p in _SECURITY_EXACT_PATHS:
        return True
    if base in _SECURITY_BASENAMES:
        return True
    if p.startswith(_SECURITY_PATH_PREFIXES):
        return True
    return any(tok in p for tok in _SECURITY_PATH_TOKENS)


def changed_entries(repo_path: Path, before: str, after: str) -> list[Entry]:
    """`git diff --name-status -M` as a list of `Entry`. Raises on a git
    failure — callers must catch (the module contract is: any exception here
    routes FULL, never SINGLE_TURN)."""
    proc = subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{before}..{after}"],
        cwd=repo_path, capture_output=True, text=True, errors="replace", encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git diff --name-status failed: {(proc.stderr or '').strip()}")
    entries: list[Entry] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status[:1] in ("R", "C") and len(parts) > 2:
            entries.append(Entry(status, _normalize(parts[1])))
            entries.append(Entry(status, _normalize(parts[-1])))
        else:
            entries.append(Entry(status, _normalize(parts[-1])))
    return entries


def diff_line_count(repo_path: Path, before: str, after: str) -> int | None:
    """Added + deleted lines from `git diff --numstat -M`, aggregated over the
    whole diff. `None` when it cannot be measured this way — a binary file
    reports `-\\t-`, or git itself fails — which the caller must treat as
    FULL_REVIEW (fail-closed, same rule as an unreadable diff)."""
    proc = subprocess.run(
        ["git", "diff", "--numstat", "-M", f"{before}..{after}"],
        cwd=repo_path, capture_output=True, text=True, errors="replace", encoding="utf-8",
    )
    if proc.returncode != 0:
        return None
    total = 0
    saw_line = False
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        added, deleted = parts[0], parts[1]
        if added == "-" or deleted == "-":
            return None  # binary — cannot be judged by line count
        try:
            total += int(added) + int(deleted)
        except ValueError:
            return None
        saw_line = True
    return total if saw_line else 0


def _diff_text(repo_path: Path, before: str, after: str) -> str | None:
    # `--no-ext-diff --no-textconv` + a secret-scrubbed env are a SECURITY
    # boundary: this produces a FULL PATCH on the default round-1 review path,
    # so a coder-planted `diff.external`/`diff.<x>.textconv` in the reviewed
    # repo's shared .git/config would otherwise run a program here — inside the
    # orchestrator process, no OS sandbox — with the launcher's secrets in
    # reach. Same hardening as `reviewer._git_diff` and `vcs/git.GitRepo`.
    from ..vcs.git import _git_subprocess_env

    proc = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "diff", "-M",
         "--no-ext-diff", "--no-textconv", f"{before}..{after}"],
        cwd=repo_path, capture_output=True, text=True, errors="replace",
        env=_git_subprocess_env("diff"), encoding="utf-8",
    )
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def _content_guard_reason(diff_text: str) -> str | None:
    """Guard/scrub functions modified by CONTENT, not just path.

    Scans every added/removed hunk line (never a ``+++``/``---`` file header)
    for a guard-family token, casefolded. This is what catches the
    never-push guard, the secret redactor and the commit-message sanitizer —
    none of their FILE paths carry a guard token, only their function names
    do (review finding, task 37b27a3d: a path-only check never saw them).
    """
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if not (line.startswith("+") or line.startswith("-")):
            continue
        body = line[1:].casefold()
        for tok in _GUARD_TOKENS:
            if tok in body:
                return f"guard/scrub reference in diff content ({tok!r})"
    return None


def risk_reason(
    entries: list[Entry],
    repo_path: Path | None = None,
    before: str = "",
    after: str = "",
) -> str | None:
    """First reason this diff must get the full review, or `None` (benign).

    Path-based checks run unconditionally. When `repo_path` is given, the
    diff's HUNK CONTENT is also scanned for guard/scrub function references a
    path-only check misses (`_content_guard_reason`) — callers computing a
    real routing decision (not just inspecting a known path list) should
    always pass it.
    """
    paths = [e.path for e in entries]
    for e in entries:
        if e.status[:1] == "D" and _is_test_path(e.path):
            return f"test file deleted ({e.path})"
        if e.status[:1] in ("R", "C") and _is_test_path(e.path):
            return f"test file renamed ({e.path})"
        if _is_guard_scrub_path(e.path):
            return f"guard/scrub path touched ({e.path})"
        if _is_security_path(e.path):
            return f"security-sensitive path touched ({e.path})"
    if review_must_be_unbounded(paths):
        return "agent instructions or gate control data touched"
    if repo_path is not None:
        diff_text = _diff_text(repo_path, before, after)
        if diff_text is None:
            return "diff content unreadable"
        content_reason = _content_guard_reason(diff_text)
        if content_reason is not None:
            return content_reason
    return None


def routing_config(config: dict | None) -> tuple[bool, int]:
    """(enabled, max_diff_lines) from `pipeline.review_routing`, tolerating
    the `pipeline: None` deep-merge shape (mirrors `complexity.trivial_enabled`).
    A non-positive or non-int `max_diff_lines` disables routing (fail-closed):
    a misconfigured threshold must never silently widen what qualifies."""
    section = (config or {}).get("pipeline") or {}
    routing = section.get("review_routing") or {}
    enabled = bool(routing.get("enabled", True))
    raw_max = routing.get("max_diff_lines", MAX_SINGLE_TURN_LINES)
    try:
        max_lines = int(raw_max)
    except (TypeError, ValueError):
        return False, MAX_SINGLE_TURN_LINES
    if max_lines <= 0:
        return False, MAX_SINGLE_TURN_LINES
    return enabled, max_lines


def route(
    *,
    entries: list[Entry],
    line_count: int | None,
    enabled: bool,
    max_lines: int,
    repo_path: Path | None = None,
    before: str = "",
    after: str = "",
) -> tuple[Route, str]:
    """The predicate. FULL unless every condition holds; every negative
    branch names why. Empty diff => FULL (same rule as `trivial_paths`:
    unknown blast radius)."""
    if not enabled:
        return Route.FULL_REVIEW, "review routing disabled"
    if not entries:
        return Route.FULL_REVIEW, "no readable changed paths"
    if line_count is None:
        return Route.FULL_REVIEW, "diff line count unreadable (binary or unmeasurable)"
    if line_count > max_lines:
        return (
            Route.FULL_REVIEW,
            f"diff is {line_count} lines, over the {max_lines}-line threshold",
        )
    reason = risk_reason(entries, repo_path, before, after)
    if reason is not None:
        return Route.FULL_REVIEW, reason
    return (
        Route.SINGLE_TURN,
        f"diff is {line_count} lines (<= {max_lines}), no risk flags",
    )
