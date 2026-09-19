"""Stall-watchdog helpers factored out of `wake.py` (structural-budget: keep
the frozen `wake.py`/`db.py` line count flat — new logic lives here instead).

Two independent watchdogs guard the same "hung backend" condition:
`orchestrator._await_coder_turn` cancels a coder turn with no progress event
for `bounds.attempt_timeout_s` seconds (an hour by default), and
`WakeWatcher._escalate_if_stalled` escalates any CLAIMED active-status task
whose last EVENT is older than `blockers.stuck_active_minutes` (40 by
default, configured independently). 40 minutes < 60 minutes, so on stock
config the coarse task-level sweep always fired FIRST — 20 minutes before the
attempt-level watchdog designed to kill a hung backend even had its chance —
and escalated a task whose backend was still legitimately inside its own
allowance, orphaning the open attempt row (its turns and usage were never
attributed to anything: this was measured live, not hypothesised).
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from .taxonomy import human_gate_armed, resume_provenance

if TYPE_CHECKING:
    from ..core.db import Store
    from ..core.task import Task

log = logging.getLogger("no_human.wake")


def effective_stuck_active_minutes(config: dict) -> float:
    """The stuck-active threshold `_escalate_if_stalled` actually uses.

    The fix is ordering, not a bigger number: the task-level threshold must
    never be able to fire before the attempt-level one has had its full
    chance, for WHATEVER the two are independently configured to. So this
    derives a floor from `bounds.attempt_timeout_s` — mirroring
    `orchestrator.py`'s own `float(bounds.get("attempt_timeout_s") or 3600)`
    exactly, including its falsy-``or`` fallback — and returns whichever of
    the configured value and that floor is larger.

    `raw <= 0` (watchdog explicitly disabled) returns 0.0 immediately and
    skips the floor entirely: a disabled watchdog must stay disabled, not
    get resurrected at ~61 minutes by a floor meant to order two ACTIVE
    watchdogs relative to each other.

    Never raises inside a tick — a non-numeric/negative `attempt_timeout_s`
    falls back to the same 3600s default `orchestrator.py` uses.
    """
    blockers_cfg = (config or {}).get("blockers", {}) or {}
    try:
        raw = float(blockers_cfg.get("stuck_active_minutes", 40))
    except (TypeError, ValueError):
        raw = 40.0
    if raw <= 0:
        return 0.0
    bounds_cfg = (config or {}).get("bounds", {}) or {}
    try:
        attempt_s = float(bounds_cfg.get("attempt_timeout_s") or 3600)
    except (TypeError, ValueError):
        attempt_s = 3600.0
    if attempt_s <= 0:
        attempt_s = 3600.0
    # Strictly greater than the attempt bound: the attempt-level watchdog
    # must fire and finish closing its own row before the task-level sweep
    # can act on the same silence.
    floor_min = math.ceil(attempt_s / 60.0) + 1
    return max(raw, float(floor_min))


async def stamp_resume_checkpoint(store: "Store", task: "Task") -> None:
    """Stamp `context.resume_from` from the open attempt's commit BEFORE the
    caller closes that attempt row. `Scheduler._resume_branch_point`
    (scheduler.py ~1806-1820) derives the checkpoint a later resume branches
    from by reading `store.latest_open_attempt(task.id).commit_sha` — closing
    that row first, with no other record of its sha, would silently throw
    away committed WIP on the next human resume. `human_gate_armed` is the
    exact predicate the scheduler itself uses for "a human's own checkpoint
    is still unconsumed", so an armed human gate is left untouched rather
    than overwritten by this machine stamp. Fail-open: a stamp failure must
    never abort the escalation itself.
    """
    try:
        open_row = await store.latest_open_attempt(task.id)
        sha = (open_row or {}).get("commit_sha") or ""
        ctx = task.context or {}
        if sha and not human_gate_armed(ctx):
            resume = ctx.get("resume_from") or {}
            if resume.get("sha") != sha:
                task.context = await store.merge_context(
                    task.id, {"resume_from": resume_provenance(
                        {"sha": sha}, "stall_escalation")})
    except Exception:  # noqa: BLE001 — bookkeeping must never abort an escalation
        log.warning("stall-escalation checkpoint stamp failed for %s",
                    task.id[:8], exc_info=True)


async def close_stalled_attempt(
    store: "Store", task: "Task", *, age_min: float, stalled_status: str,
) -> str:
    """Close the open attempt row so its turns/usage are attributed to a
    terminal row instead of orphaned `in_progress` forever (measured: 86M+
    tokens over 243 calls recorded to tasks but not to their attempt rows).
    Does NOT stop the backend coroutine that opened the row — it keeps
    running; see `Store.abandon_open_attempt`'s docstring for why closing
    the row by id is still safe. Fail-open: bookkeeping must never abort an
    escalation already made. Returns a human-readable usage suffix
    (possibly empty) for the escalation event text.
    """
    try:
        closed = await store.abandon_open_attempt(
            task.id,
            reason=(f"interrupted: the stall watchdog abandoned this "
                    f"attempt — no event for {age_min:.0f}m while "
                    f"{stalled_status}"),
        )
    except Exception:  # noqa: BLE001 — bookkeeping must never abort an escalation
        log.warning("stall-escalation attempt close failed for %s",
                    task.id[:8], exc_info=True)
        return ""
    if not closed:
        return ""
    return (f" (attempt #{closed.get('attempt_number')} closed: "
            f"{closed.get('turns_used')} turns, "
            f"{closed.get('tokens_used')} tokens)")
