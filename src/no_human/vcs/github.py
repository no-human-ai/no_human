"""Open a GitHub PR via the `gh` CLI. Never merges (§3.2)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("no_human.vcs")


def is_github_remote(url: str, extra_hosts: tuple[str, ...] | list[str] = ()) -> bool:
    """True for github.com or any configured GitHub Enterprise host.

    GHE hosts (e.g. ``code.example.com``) don't contain "github.com", so they
    must be listed explicitly via ``git.github_hosts`` in config. ``gh`` infers
    the host from the repo remote, so the same `gh pr create` path works.
    """
    if "github.com" in url:
        return True
    return any(h and h in url for h in extra_hosts)


def open_pr(
    repo_path: Path, branch: str, title: str, body: str, *, base: str = "main",
    update_existing_body: bool = False,
) -> str:
    """Create a draft PR and return its URL. Requires `gh` auth.

    Idempotent: if a PR already exists for ``branch`` (e.g. when revising after a
    human PR comment, which pushes onto the same branch), return the existing PR's
    URL instead of failing — the push has already updated it. Never merges.
    """
    proc = subprocess.run(
        [
            "gh", "pr", "create",
            "--head", branch,
            "--base", base,
            "--title", title,
            "--body", body,
            "--draft",
        ],
        cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode == 0:
        return proc.stdout.strip()

    stderr = proc.stderr.strip()
    if "already exists" in stderr.lower():
        existing = _existing_pr_url(repo_path, branch)
        if existing:
            # 🔴 APPLY THE NEW BODY. This path used to return the URL and silently drop
            # it, which was harmless while the only caller was `_finalize` — but 0a opens
            # a DRAFT before the review gate, so the first body is the pre-review one
            # (no test evidence, no review evidence) and `_finalize`'s richer body was
            # being discarded. A review caught it: the human-visible PR permanently lost
            # its consolidated "## Evidence" section (the reviewer's verdict and the test
            # run), which regresses W1.6/M-B and defeats 0a's own purpose. The comment
            # claiming "_finalize's
            # later call UPDATES the same PR" was simply false — nothing in src/ ran
            # `gh pr edit` at all.
            #
            # 🔴 OPT-IN ONLY. My first version edited unconditionally, which also hit the
            # REVISION flow (a task resuming onto an existing PR branch) and would have
            # OVERWRITTEN a description a human had edited — behaviour main never had. A
            # review caught it. Only the run that opened the draft itself may rewrite the
            # body. Two callers pass `update_existing_body=True`, both gated on ownership:
            # `_finalize` (orchestrator.py) passes `may_refresh_body` — `bool(pr_draft_created)
            # and pr_draft_branch == branch`, durable via task.context, branch-scoped — on
            # both its first open and its force-with-lease retry; `_gate_already_satisfied`
            # passes a literal True, but only inside a block guarded by that same predicate
            # plus the stricter identity check `pr_url == str(pr_draft_created).strip()`.
            # Nothing else does.
            #
            # Best-effort: a failed body update must not fail a PR that exists. The URL is
            # the delivery; the body is evidence, and losing it loudly beats escalating a
            # delivered change.
            if not update_existing_body:
                return existing
            edit = subprocess.run(
                ["gh", "pr", "edit", existing, "--body", body],
                cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if edit.returncode != 0:
                log.warning("gh pr edit failed for %s (%s); PR keeps its earlier body",
                            existing, edit.stderr.strip())
            return existing
    raise RuntimeError(f"gh pr create failed: {stderr}")


def mark_pr_ready(repo_path: Path, pr_url: str) -> str:
    """Promote a DRAFT PR to ready-for-review. Never raises.

    Additive and non-destructive: `gh pr ready` flips one PR field, unlike
    `gh pr edit --body` which replaces content. Repeated calls are
    idempotent (see the `already_ready` outcome below); `--undo` is never
    used.

    Returns a short outcome token, embedded in the caller's emitted event:
      - ``"ready"``        — the draft was promoted.
      - ``"already_ready"``  — `gh` reports the PR is already ready for
        review (idempotent re-run; not a failure).
      - ``"refused: <reason>"`` — `gh` refused (e.g. the PR is CLOSED or
        MERGED).
      - ``"unavailable: <reason>"`` — `gh` itself could not run (not
        installed, not authenticated, no network, PR not found).
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "ready", pr_url],
            cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        return f"unavailable: {exc}"

    if proc.returncode == 0:
        return "ready"

    stderr = proc.stderr.strip()
    lowered = stderr.lower()
    if "already" in lowered and "ready" in lowered:
        return "already_ready"
    return f"refused: {stderr.splitlines()[0] if stderr else 'gh pr ready failed'}"


def _existing_pr_url(repo_path: Path, branch: str) -> str | None:
    """Return the URL of the open PR for ``branch``, or None."""
    proc = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open",
         "--json", "url", "--jq", ".[0].url"],
        cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    url = proc.stdout.strip()
    return url or None
