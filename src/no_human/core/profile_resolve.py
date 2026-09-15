"""Profile resolution shared by the Orchestrator and the merge-time gate.

The Orchestrator decides, per task, which ``ProjectProfile`` is confirmed
("usable") and what its proven test command is — that policy lives here so
it has exactly one implementation. ``Orchestrator`` keeps its own
``_primary_repo_path`` / ``_profile_usable_under_policy`` / ``_usable_profile``
/ ``_resolve_test_cmd`` methods as thin wrappers over this module (unchanged
names and behavior, for every existing caller and test).

The merge-time land gate (``vcs.approve_merge.land_task``, invoked by
``nh approve`` and the board's Approve button) runs OUTSIDE any Orchestrator
instance — there is no bound ``self`` to call. Both of its callers
(``cli/commands.py`` and ``api/app.py``) import ``resolve_repo_test_cmd``
from here instead of re-deriving the precedence rules, so the merge gate
always runs the SAME command the orchestrator already proved clean.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Callable

from ..project_config import apply_repo_config, load_repo_config

log = logging.getLogger(__name__)


def primary_repo_path(repo_path: Any) -> str | None:
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


def profile_usable_under_policy(prof: Any, config: Any) -> bool:
    """A profile drives a task if a human confirmed it (``is_usable``), OR —
    when ``profile.auto_confirm_proven`` is opted in — if its test command
    was PROVEN to run clean (megaplan P1). Proof (the exact command exited 0
    in a real subprocess at onboarding) is the safety signal; the flag only
    removes the human click, never the proof."""
    if prof is None:
        return False
    auto = bool(config.get("profile", {}).get("auto_confirm_proven", False))
    return prof.usable_under_policy(auto_confirm_proven=auto)


async def usable_profile(
    store: Any,
    config: Any,
    repo_path: Any,
    *,
    repo_config: dict[str, Any] | None = None,
    on_hit: Callable[[Any, Any], None] | None = None,
) -> Any | None:
    """Return the repo's ProjectProfile if it may drive a task under the
    active policy (see ``profile_usable_under_policy``); else None. Prefer
    the SQLite mirror (keyed by the PRIMARY path — worktrees resolve to
    it); fall back to the repo's ``.no_human/project.yml``.

    ``on_hit(candidate_path, db_profile)`` fires only on a DB hit, mirroring
    ``Orchestrator._usable_profile``'s divergence warning without this
    module depending on the Orchestrator's emit/log plumbing.
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
    if prof is not None and on_hit is not None:
        on_hit(hit_cand, prof)
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
    if repo_config is None:
        repo_config = load_repo_config(repo_path)
    return apply_repo_config(prof, repo_config)


async def resolve_repo_test_cmd(
    store: Any,
    config: Any,
    repo_path: Any,
    *,
    profile: Any | None = None,
) -> str | None:
    """Resolve the test command exactly as the Orchestrator's own
    ``_resolve_test_cmd`` does: an explicit config override wins; else a
    usable profile's proven ``test_cmd``; else None.

    Callers OUTSIDE the Orchestrator (the merge-time ``land_task`` gate,
    reached from ``nh approve`` and the board's Approve button) use this so
    the merge gate runs the SAME command the orchestrator already proved
    clean, instead of re-deriving the precedence rules a second time.

    Wrapped in try/except: a bad or unreachable profile must never block a
    land that would otherwise succeed — ``land_task`` degrades to
    ``python -m pytest`` exactly as if no profile command were passed.
    """
    try:
        explicit = config.get("tests", {}).get("command")
        if explicit:
            return explicit
        prof = profile
        if prof is None:
            prof = await usable_profile(store, config, repo_path)
        if prof and prof.test_cmd:
            return prof.test_cmd
        return None
    except Exception as exc:  # noqa: BLE001 — never block a land
        log.warning("profile test-cmd resolution failed (falling back to "
                    "python -m pytest): %s", exc)
        return None
