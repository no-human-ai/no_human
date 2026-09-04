"""Quota cost-leak fixes in the scheduler (retry-into-quota-wall storm).

Two never-shipping-compute leaks, both proven by ablation below:

FIX #3(a) — IDLE-PATH cooldown recovery. `recover_quota_cooldown` ran only at
startup (`run_forever`, before its first tick). A RUNNING process that never
hit a wall itself (`_quota_wall is None`) therefore ignored a wall the DB
already named — a park written by a worker whose `_run` has set the row
PAUSED_QUOTA but not yet unwound to arm the in-memory clock (that arming is one
tick late), or by another process — and dispatched the rest of the queue
straight into it. `tick()` now re-derives that wall before dispatch, gated on
`_quota_wall is None` so it never fights the resume path (re-arming from a
park's own raise-time-plus-an-hour clock would recreate the 2026-08-20
starvation).

FIX #3(b) — a park must never SHORTEN a wall this process is already holding
for the same profile. Two workers dispatched before the clock armed race the
same wall; the one that parks second can carry an EARLIER reset (a stale
banner, or the fallback hour under-estimating a longer real reset), and the
unconditional overwrite in `_run` resumed the pool early — straight back into
the same wall. A genuinely LATER reset still extends it.

A walled dispatch produces zero output, so declining it loses no shipped PR;
these tests only assert never-shipping compute is removed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus


class _RecordingOrch:
    started: list[str] = []

    def __init__(self, task):
        self.task = task
        self._sink = None

    async def run_task(self, task):
        _RecordingOrch.started.append(task.id)
        await asyncio.sleep(0)
        return None


class _ParkingOrch:
    """Parks its task `paused_quota` with a given reset, the way `_park_quota`
    leaves the row, and returns the outcome `_run` reads (same shape as
    test_infra_park_is_not_a_wall._ParkingOrch)."""

    def __init__(self, store, resets_at: str):
        self.store = store
        self.resets_at = resets_at
        self._sink = None

    async def run_task(self, task):
        task.wake_check_at = self.resets_at
        task.blocker = {"category": "QUOTA", "wake_condition": "quota_refreshed",
                        "raised_at": datetime.now(timezone.utc).isoformat(),
                        "confidence": 1.0, "root_cause_hypothesis": "wall",
                        "auth_profile": None, "infra": False}
        await self.store.update_task_columns(task)
        await self.store.set_status(task, TaskStatus.PAUSED_QUOTA, validate=False)
        return SimpleNamespace(status=TaskStatus.PAUSED_QUOTA, task=task)


async def _quota_park(store, wake_at: datetime, *, raised_at: datetime | None = None):
    t = Task.new("parked on the wall", repo_path="/tmp/x")
    await store.create_task(t)
    t.blocker = {"category": "QUOTA", "wake_condition": "quota_refreshed",
                 "raised_at": (raised_at or wake_at - timedelta(hours=1)).isoformat(),
                 "root_cause_hypothesis": "You've hit your session limit"}
    t.wake_check_at = wake_at.isoformat()
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.PAUSED_QUOTA, validate=False)
    return t


async def _pending(store, n=1):
    for i in range(n):
        await store.create_task(Task.new(f"pending {i}", repo_path="/tmp/x"))


# --------------------------------------------------------------------------- #
# FIX #3(a): a running process honors a wall the DB names, without a restart   #
# --------------------------------------------------------------------------- #


async def test_tick_honors_a_wall_recorded_after_startup(store):
    """A park appears in the DB while a process runs that never hit a wall
    itself (no startup `recover_quota_cooldown` call, `_quota_wall is None`).
    The very next `tick()` must pause the pool instead of dispatching the two
    pending tasks into the wall."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    await _quota_park(store, now + timedelta(minutes=40))
    await _pending(store, 2)
    sched = Scheduler(store, _RecordingOrch, max_workers=2)
    assert sched._quota_wall is None, "precondition: this process never walled"

    started = await sched.tick()
    await asyncio.sleep(0.05)

    assert started == [] and _RecordingOrch.started == [], (
        "tick dispatched into a wall the DB already named")
    assert sched._quota_cooldown_until is not None
    assert sched.health_snapshot()["idle_reason"] == "quota_cooldown"


async def test_idle_recover_is_dormant_once_this_process_holds_a_wall(store):
    """The gate must not re-derive the pool clock from a park's own clock once
    `_quota_wall` is set (the resume path owns lapse handling from there — a
    re-derive would re-arm a wall that has actually reset). A lapsed park whose
    OWN wake_check_at is still in the future must NOT re-pause the pool."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    # Park's own clock is 4 min ahead, but the real wall passed 30 min ago.
    await _quota_park(store, now + timedelta(minutes=4),
                      raised_at=now - timedelta(minutes=64))
    await _pending(store, 1)
    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    # This process already resolved (and lapsed) a wall — resume path territory.
    sched._quota_wall = now - timedelta(minutes=30)
    sched._quota_wall_profile = None

    started = await sched.tick()
    await asyncio.sleep(0.05)

    assert sched._quota_cooldown_until is None, (
        "idle recover re-armed a wall that already reset — starvation")
    assert len(started) == 1, "work was stranded behind a dead wall"


async def test_ablation_a_without_idle_recover_the_pool_storms(store, monkeypatch):
    """Ablation for FIX #3(a): neutralize the recovery call and the identical
    setup dispatches straight into the wall — the guard, not something else,
    is what stops the storm."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    await _quota_park(store, now + timedelta(minutes=40))
    await _pending(store, 2)
    sched = Scheduler(store, _RecordingOrch, max_workers=2)

    async def _noop():
        return None
    monkeypatch.setattr(sched, "recover_quota_cooldown", _noop)

    started = await sched.tick()
    await asyncio.sleep(0.05)
    assert len(started) == 2, "ablation must storm into the wall"


# --------------------------------------------------------------------------- #
# FIX #3(b): a park never shortens a live wall (early resume -> re-storm)      #
# --------------------------------------------------------------------------- #


async def test_a_park_never_shortens_a_live_wall_same_profile(store):
    """A live wall is armed to T+50m; a second worker parks carrying an EARLIER
    T+10m (the loser of a race, under-estimating). The pool clock must KEEP
    T+50m — shortening it resumes early, straight back into the same wall.

    `_run` is driven directly: a live cooldown would make `tick()` cool and
    never dispatch, so the arming block a real second-park races into is only
    reachable by running the worker itself."""
    now = datetime.now(timezone.utc)
    live = now + timedelta(minutes=50)
    earlier = (now + timedelta(minutes=10)).isoformat()
    sched = Scheduler(store, lambda task: _ParkingOrch(store, earlier),
                      max_workers=1)
    sched._quota_cooldown_until = live
    sched._quota_wall = live
    sched._quota_wall_profile = None            # matches any active profile
    t = Task.new("second park", repo_path="/tmp/x")
    await store.create_task(t)

    await sched._run(t)

    assert sched._quota_cooldown_until == live, "a park SHORTENED a live wall"
    assert sched._quota_wall == live


async def test_a_later_park_still_extends_the_wall(store):
    """Positive control / the other direction: a fresher, LATER reset must
    still push the wall out — never-shorten is not never-move."""
    now = datetime.now(timezone.utc)
    live = now + timedelta(minutes=20)
    later_dt = now + timedelta(minutes=90)
    sched = Scheduler(store, lambda task: _ParkingOrch(store, later_dt.isoformat()),
                      max_workers=1)
    sched._quota_cooldown_until = live
    sched._quota_wall = live
    sched._quota_wall_profile = None
    t = Task.new("later park", repo_path="/tmp/x")
    await store.create_task(t)

    await sched._run(t)

    assert sched._quota_cooldown_until == later_dt, "a later reset did not extend"
    assert sched._quota_wall == later_dt
