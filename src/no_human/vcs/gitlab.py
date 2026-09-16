"""Open a GitLab MR via the `glab` CLI. Never merges (§3.2)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("no_human.vcs")


def is_gitlab_remote(url: str) -> bool:
    return "gitlab" in url


def open_mr(
    repo_path: Path, branch: str, title: str, body: str, *, base: str = "main",
) -> str:
    """Create an MR and return its URL. Requires `glab` auth.

    Note: explicitly no `--merge-when-pipeline-succeeds` — the agent never
    enables auto-merge.
    """
    proc = subprocess.run(
        [
            "glab", "mr", "create",
            "--source-branch", branch,
            "--target-branch", base,
            "--title", title,
            "--description", body,
            "--no-merge",
            "--yes",
        ],
        cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode == 0:
        return proc.stdout.strip()
    stderr = proc.stderr.strip()
    raise RuntimeError(f"glab mr create failed: {stderr}")
