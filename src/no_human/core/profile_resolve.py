"""Store/orchestrator-free profile resolution.

Pulled out of `Orchestrator` (`_primary_repo_path` / `_profile_usable_under_
policy` / `_usable_profile` / `_resolve_test_cmd`) so a caller with only a
``store`` and a ``config`` — `nh approve` in `cli/commands.py`, the board's
Approve button in `api/app.py` — can resolve the repo's proven test command
without constructing a full `Orchestrator`. The orchestrator's own methods
become thin wrappers over these (see `core/orchestrator.py`); behaviour is
unchanged, including the scar docstrings below.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Callable

from ..project_config import apply_repo_config, load_repo_config

log = logging.getLogger(__name__)


def primary_repo_path(repo_path) -> str | None:
    """The PRIMARY checkout behind a git worktree, or None if repo_path
    already is one. Profiles are keyed by the primary path; a worktree
    task that looks itself up by its worktree path finds nothing — which
    is how all three tasks of the first parallel run (2026-07-11) lost
    their proven test command and burned max_attempts on the fallback."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    common = (proc.stdout or "").strip()
    if proc.returncode != 0 or not common.endswith("/.git"):
        return None
    primary = common[: -len("/.git")]
    return primary if primary != str(repo_path).rstrip("/") else None


def profile_usable_under_policy(prof: Any, config) -> bool:
    """A profile drives a task if a human confirmed it (``is_usable``), OR —
    when ``profile.auto_confirm_proven`` is opted in — if its test command
    was PROVEN to run clean (megaplan P1). Proof (the exact command exited 0
    in a real subprocess at onboarding) is the safety signal; the flag only
    removes the human click, never the proof."""
    if prof is None:
        return False
    auto = bool((config.get("profile", {}) or {}).get("auto_confirm_proven", False))
    return prof.usable_under_policy(auto_confirm_proven=auto)


async def usable_profile(
    store, config, repo_path, *,
    on_divergence: Callable[[str, Any], None] | None = None,
    repo_config: dict[str, Any] | None = None,
) -> Any | None:
    """Return the repo's ProjectProfile if it may drive a task under the
    active policy (see ``profile_usable_under_policy``); else None. Prefer
    the SQLite mirror (keyed by the PRIMARY path — worktrees resolve to
    it); fall back to the repo's ``.no_human/project.yml``.

    ``on_divergence(hit_cand, prof)`` is an optional callback so a caller
    that keeps a once-per-run latch (the orchestrator's
    ``_profile_divergence_warned``) can still warn exactly once; callers
    with no such state (CLI/API, one-shot per invocation) pass nothing —
    no warning side effects from a merge. ``repo_config`` is the already
    loaded `.no_human.yml` dict for *repo_path*; if omitted it is read
    fresh via `load_repo_config`.
    """
    from ..profile import ProjectProfile
    prof = None
    hit_cand = None
    candidates = [str(repo_path)]
    primary = primary_repo_path(repo_path)
    if primary:
        candidates.append(primary)
    for cand in candidates:
        try:
            prof = await store.get_profile(cand)
        except Exception as exc:  # noqa: BLE001
            log.warning("profile lookup failed: %s", exc)
        if prof is not None:
            hit_cand = cand
            break
    if prof is not None and on_divergence is not None:
        on_divergence(hit_cand, prof)
    if prof is None:
        for cand in candidates:
            try:
                prof = ProjectProfile.load(cand)
            except Exception:  # noqa: BLE001
                prof = None
            if prof is not None:
                break
    if not profile_usable_under_policy(prof, config):
        return None
    # The repo's own `.no_human.yml` may fill routing rules the operator's
    # profile leaves empty (never replace them) — see project_config.py.
    cfg = repo_config if repo_config is not None else load_repo_config(repo_path)
    return apply_repo_config(prof, cfg)


async def resolve_test_cmd(store, config, repo_path) -> str | None:
    """Resolve the test command: an explicit config override wins; else a
    usable profile's proven ``test_cmd``; else None so the caller falls
    back to its own default (``detect_command`` for the orchestrator's
    attempt-time path; ``python -m pytest`` for the merge gate).

    ``config`` may be a `Config` object or a plain dict — both callers
    differ — so this only ever uses ``.get``.
    """
    explicit = (config.get("tests", {}) or {}).get("command")
    if explicit:
        return explicit
    prof = await usable_profile(store, config, repo_path)
    if prof and prof.test_cmd:
        return prof.test_cmd
    return None
