"""Queue health: stuck detection + drain ETA (D2 #4, watchtower `health.py`).

Zero LLM cost — pure task timestamps. Two questions the unattended operator
actually has, neither of which the board could answer:

- **Is the queue stuck?** Open work exists AND nothing has completed in
  ``stuck_after_minutes``. (Watchtower's definition, adapted: no completion in
  the window, not "no activity" — a task can look busy while looping.)
- **When will it drain?** From the completion rate in a recent window, not a
  guess: ``ETA = open / (completed_in_window / window)``. Unknown when the
  window is empty (say so rather than inventing a number).
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# Statuses that mean "the queue still owes the operator work".
OPEN_STATUSES = ("pending", "implementing", "reviewing")
# Statuses that mean the task reached a HUMAN gate — the queue's job is done;
# a board full of these is success, not a stall.
GATE_STATUSES = ("awaiting_approval", "escalated", "awaiting_input", "blocked")
DONE_STATUSES = ("done", "failed")
# Statuses a worker can claim (mirrors scheduler._CLAIMABLE).
CLAIMABLE_STATUSES = ("implementing", "pending")


@dataclass
class QueueHealth:
    open_tasks: int = 0
    at_gate: int = 0
    completed_in_window: int = 0
    window_minutes: int = 30
    stuck: bool = False
    stuck_reason: str = ""
    eta_minutes: float | None = None   # None = unknowable, not "zero"
    workers_busy: int = 0
    max_workers: int = 0
    queue_depth: int = 0
    est_drain_seconds: float | None = None   # None = unknowable
    # A pool-wide quota wall (scheduler._quota_cooldown_until): distinct from
    # `stuck` — nothing is wedged, the pool is choosing not to dispatch until
    # the reset. Reporting `stuck: false, workers_busy: 0` with no other field
    # naming why is the defect this exists to close (2026-08-20 evidence).
    paused: bool = False
    paused_reason: str | None = None   # "quota" | "infra" | "lease_lost" | None
    paused_until: str | None = None    # ISO, the wall's reset time
    paused_profile: str | None = None  # which auth profile hit the wall

    def as_dict(self) -> dict[str, Any]:
        return {
            "open_tasks": self.open_tasks,
            "at_gate": self.at_gate,
            "completed_in_window": self.completed_in_window,
            "window_minutes": self.window_minutes,
            "stuck": self.stuck,
            "stuck_reason": self.stuck_reason,
            "eta_minutes": (round(self.eta_minutes, 1)
                            if self.eta_minutes is not None else None),
            "workers_busy": self.workers_busy,
            "max_workers": self.max_workers,
            "queue_depth": self.queue_depth,
            "est_drain_seconds": (round(self.est_drain_seconds, 1)
                                  if self.est_drain_seconds is not None else None),
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "paused_until": self.paused_until,
            "paused_profile": self.paused_profile,
        }


def _iso_cutoff(minutes: int, *, now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
    return (base - timedelta(minutes=minutes)).isoformat()


async def _median_attempt_seconds(store: Any, limit: int) -> float | None:
    """Median wall-time of recently completed attempts, derived from existing
    started_at/completed_at timestamps (no schema change: julianday() parses
    both SQLite datetime('now') and ISO-T forms)."""
    rows = await store.query(
        "SELECT (julianday(completed_at) - julianday(started_at)) * 86400.0 "
        "FROM attempts WHERE completed_at IS NOT NULL AND started_at IS NOT NULL "
        "ORDER BY completed_at DESC LIMIT ?", (limit,))
    secs = [float(r[0]) for r in rows if r[0] is not None and float(r[0]) > 0]
    return statistics.median(secs) if secs else None


async def _quota_profile(store: Any) -> str | None:
    """Best-effort profile attribution for the active quota wall: the newest
    ``paused_quota`` park's ``auth_profile`` stamp — the same field
    ``Scheduler.recover_quota_cooldown`` reads to attribute a remembered
    wall. No park, or one written before this field existed, reports
    ``None`` rather than guessing. Quota-only: an infra-breaker cooldown
    must never call this — a stale unrelated park would be misattributed
    as the cause of an SDK/auth failure."""
    # Newest NON-infra park: an infra park (`blocker.infra`, a dead SDK
    # session) never armed the pool clock, so it must not be the row that
    # names whose wall the pool is waiting on. Same skip as
    # `Scheduler.recover_quota_cooldown`.
    rows = await store.query(
        "SELECT blocker FROM tasks WHERE status = 'paused_quota' "
        "AND blocker IS NOT NULL ORDER BY updated_at DESC LIMIT 20", ())
    for row in rows or ():
        if not row or not row[0]:
            continue
        try:
            blocker = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        if not isinstance(blocker, dict) or blocker.get("infra"):
            continue
        profile = blocker.get("auth_profile")
        return profile if isinstance(profile, str) else None
    return None


async def queue_health(
    store: Any, *, stuck_after_minutes: int = 30, window_minutes: int = 30,
    now: datetime | None = None, inflight_ids: Any = None, max_workers: int = 0,
    attempt_sample: int = 20, quota_cooldown_until: datetime | None = None,
    infra_cooldown_until: datetime | None = None,
    lease_lost: str | None = None,
) -> QueueHealth:
    # `store.query`/`query_one`, never `store.db`. This runs on the board's
    # live store while the pool writes through the same connection, and an
    # aliased raw connection is how it opened cursors outside the critical
    # section — see `Store._fetchone` and `tests/test_db_concurrency.py`.

    async def count(sql: str, *args) -> int:
        row = await store.query_one(sql, args)
        return int(row[0] or 0) if row else 0

    open_q = ",".join("?" * len(OPEN_STATUSES))
    gate_q = ",".join("?" * len(GATE_STATUSES))
    done_q = ",".join("?" * len(DONE_STATUSES))
    claimable_q = ",".join("?" * len(CLAIMABLE_STATUSES))

    h = QueueHealth(window_minutes=window_minutes)
    h.open_tasks = await count(
        f"SELECT COUNT(*) FROM tasks WHERE status IN ({open_q})", *OPEN_STATUSES)
    h.at_gate = await count(
        f"SELECT COUNT(*) FROM tasks WHERE status IN ({gate_q})", *GATE_STATUSES)

    cutoff = _iso_cutoff(window_minutes, now=now)
    h.completed_in_window = await count(
        f"SELECT COUNT(*) FROM tasks WHERE status IN ({done_q}) "
        "AND updated_at >= ?", *DONE_STATUSES, cutoff)

    inflight = set(inflight_ids or ())
    h.workers_busy = len(inflight)
    h.max_workers = int(max_workers)

    rows = await store.query(
        f"SELECT id FROM tasks WHERE status IN ({claimable_q})", CLAIMABLE_STATUSES)
    h.queue_depth = sum(1 for (tid,) in rows if tid not in inflight)

    median_secs = await _median_attempt_seconds(store, attempt_sample)
    # Denominator is AVAILABLE workers (max - busy), not max_workers: busy
    # workers can't claim new tasks, so this is a conservative (slower, not
    # optimistic) estimate of drain time.
    available = max(0, h.max_workers - h.workers_busy)
    if h.queue_depth == 0:
        h.est_drain_seconds = 0.0            # empty queue drains in 0s — honest
    elif median_secs is None or available <= 0:
        h.est_drain_seconds = None           # no history OR no free capacity → unknowable
    else:
        h.est_drain_seconds = median_secs * h.queue_depth / available

    # A lost pool lease outranks BOTH cooldowns and the empty-queue early
    # return below: nothing dispatches again without a restart, which is
    # true whether or not the queue happens to be empty right now, and it
    # is not a "wait it out" cooldown — `paused_until` stays None because
    # nothing resumes this on its own.
    if lease_lost:
        h.paused = True
        h.paused_reason = "lease_lost"
        h.paused_until = None
        h.paused_profile = None
        h.eta_minutes = None
        return h

    if h.open_tasks == 0:
        return h  # nothing owed → never stuck, ETA 0 is meaningless

    now_dt = now or datetime.now(timezone.utc)
    # Infra wins if both are somehow non-None (defence in depth — the
    # scheduler guarantees only one clock is armed at a time).
    cooldown, reason = None, None
    if infra_cooldown_until is not None and infra_cooldown_until > now_dt:
        cooldown, reason = infra_cooldown_until, "infra"
    elif quota_cooldown_until is not None and quota_cooldown_until > now_dt:
        cooldown, reason = quota_cooldown_until, "quota"
    if cooldown is not None:
        # A pool-wide cooldown, not a wedge: workers are idle on purpose.
        # `stuck` stays false — the reason nothing moves lives in the
        # `paused_*` fields instead, and the drain estimate is measured from
        # the reset (nothing can dispatch before then) rather than from now.
        h.paused = True
        h.paused_reason = reason
        h.paused_until = cooldown.isoformat()
        # A stale unrelated `paused_quota` row must never be blamed for an
        # SDK/auth breaker trip — profile attribution is quota-only.
        h.paused_profile = await _quota_profile(store) if reason == "quota" else None
        resume_in = (cooldown - now_dt).total_seconds()
        if h.est_drain_seconds is not None:
            h.est_drain_seconds += resume_in
            h.eta_minutes = h.est_drain_seconds / 60.0
        else:
            h.eta_minutes = None
        return h

    stuck_cutoff = _iso_cutoff(stuck_after_minutes, now=now)
    recent_completion = await count(
        f"SELECT COUNT(*) FROM tasks WHERE status IN ({done_q}) "
        "AND updated_at >= ?", *DONE_STATUSES, stuck_cutoff)
    # Completions alone false-alarm on a one-worker pool whose tasks each take
    # longer than the window (live, 2026-07-24: "Queue stuck" while a task had
    # entered review minutes earlier). A recent STATE event — a pipeline stage
    # transition — is motion a busy-looping coder cannot fake: loops emit
    # usage/tool events, never state events, so the original anti-busy-loop
    # property survives. task_events.ts is epoch seconds, not ISO.
    epoch_cutoff = (
        (now or datetime.now(timezone.utc)) - timedelta(minutes=stuck_after_minutes)
    ).timestamp()
    recent_transition = await count(
        "SELECT COUNT(*) FROM (SELECT 1 FROM task_events WHERE ts >= ? "
        "AND json_extract(data, '$.kind') = 'state' LIMIT 1)", epoch_cutoff)
    if recent_completion == 0 and recent_transition == 0:
        h.stuck = True
        h.stuck_reason = (
            f"{h.open_tasks} task(s) open, nothing completed and no task "
            f"changed stage in {stuck_after_minutes} minutes")

    if h.completed_in_window > 0:
        rate_per_min = h.completed_in_window / window_minutes
        h.eta_minutes = h.open_tasks / rate_per_min
    return h
