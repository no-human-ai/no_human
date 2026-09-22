"""Ground-truth delivery receipts (D2 #1; constraint #4 "trust only
verifiable signals", watchtower `receipts.py` pattern).

A side effect isn't done because the adapter returned ok — the commit_paths
incident shipped PRs missing coder-created files past every gate, found only
by human PR inspection. After `open_pr`, the receipt reads the FORGE's view
(`gh pr view --json files,headRefOid`) and compares it to what this attempt
believes it delivered:

- ``landed``       — PR files ⊇ coder-touched repo files AND head == pushed SHA
- ``lost``         — a VERIFIED mismatch (fails the attempt before review)
- ``unverifiable`` — the forge read itself failed (advisory: network trouble
                     must never fail good work; stated on the record)
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("no_human.vcs")

Runner = Callable[..., Any]


@dataclass
class Receipt:
    kind: str            # "pr_open"
    target: str          # the PR URL
    status: str          # "landed" | "lost" | "unverifiable"
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target,
                "status": self.status, "detail": self.detail}


def _default_runner(args: list[str], *, cwd: Path) -> Any:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=60)


def verify_pr_receipt(
    repo_path: Path, pr_url: str, *,
    expected_files: set[str],
    local_sha: str,
    committed_files: set[str] | None = None,
    runner: Runner | None = None,
) -> Receipt:
    """Compare the forge's view of ``pr_url`` against this attempt's belief.

    ``expected_files`` is the coder's monotonic edited set — it includes paths
    edited then REVERTED (no net change) and gitignored/scratch files, none of
    which appear in a PR and MUST NOT be treated as lost (review finding #1).
    When ``committed_files`` (the actual branch diff) is given, only files that
    genuinely changed AND were expected are required on the forge."""
    run = runner or _default_runner
    try:
        proc = run(["gh", "pr", "view", pr_url, "--json", "files,headRefOid"],
                   cwd=repo_path)
    except Exception as exc:  # noqa: BLE001 — forge read is best-effort
        return Receipt("pr_open", pr_url, "unverifiable", f"gh view raised: {exc}")
    if getattr(proc, "returncode", 1) != 0:
        return Receipt("pr_open", pr_url, "unverifiable",
                       f"gh view failed: {getattr(proc, 'stderr', '')[:200]}")
    try:
        data = json.loads(proc.stdout)
        pr_files = {f["path"] for f in data.get("files", [])}
        head = data.get("headRefOid", "")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return Receipt("pr_open", pr_url, "unverifiable",
                       f"gh view unparseable: {exc}")

    # Only repo-relative paths can appear in a PR; the coder also touches
    # scratch/absolute paths that are correctly absent.
    relevant = {f for f in expected_files if f and not f.startswith("/")}
    # A file the coder edited but that has NO net change in the branch (edited
    # then reverted) legitimately does not appear in the PR — requiring it
    # would escalate a good, review-passed PR (review finding #1). Intersect
    # with what actually changed when we know it.
    if committed_files is not None:
        relevant &= committed_files
    missing = sorted(relevant - pr_files)
    if missing:
        # gh caps the files array (~100): in a big PR, absence from the
        # first page proves nothing — never report a loss we can't verify.
        if len(pr_files) >= 100:
            return Receipt(
                "pr_open", pr_url, "unverifiable",
                f"PR file list capped at {len(pr_files)}; cannot verify "
                f"{len(missing)} file(s)")
        return Receipt(
            "pr_open", pr_url, "lost",
            "PR is missing coder-touched file(s) the attempt believes it "
            f"delivered: {', '.join(missing[:10])}")
    if local_sha and head and head != local_sha:
        return Receipt(
            "pr_open", pr_url, "lost",
            f"PR head {head[:10]} is not the pushed commit {local_sha[:10]} — "
            "the push did not land where the PR points")
    return Receipt("pr_open", pr_url, "landed",
                   f"{len(relevant)} file(s) verified on the forge")
