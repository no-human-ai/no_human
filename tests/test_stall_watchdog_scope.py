"""The stuck-active watchdog must only judge tasks a worker has CLAIMED.

Live incident (2026-07-24): three resumed tasks sat in `implementing` waiting
for the single worker slot behind a deep queue. Their last persisted event was
their old escalation, so after 40 quiet minutes `_escalate_if_stalled` parked
each healthy waiting task as NOVEL_UNKNOWN ("session likely hung") — a false
stall, on a 40-minute cycle, for every queued resume. The sweep's own comment
states its purpose: a hung session "holding a worker slot". A task that has no
worker slot cannot be hung in one — the scheduler now passes its in-flight set
and the sweep judges only those; a tick without that knowledge (standalone
`nh wake`) skips the sweep entirely.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus


def _cfg():
    return {"blockers": {"max_park_duration": "48h", "stuck_active_minutes": 40}}


async def _silent_implementing_task(store, *, silent_minutes=90) -> Task:
    t = Task.new("Waiting for a worker slot", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    old = time.time() - silent_minutes * 60
    await store.save_events(t.id, [
        {"ts": old, "kind": "escalated", "text": "old park", "task_id": t.id}])
    return t


@pytest.mark.asyncio
async def test_unclaimed_implementing_task_is_not_stalled(store):
    """In the worker-slot queue but not claimed → silence is normal waiting."""
    t = await _silent_implementing_task(store)
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(
        now=datetime.now(timezone.utc), active_ids=set())
    assert (t.id, "escalated_stalled") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


@pytest.mark.asyncio
async def test_claimed_silent_task_is_stalled(store):
    """Claimed by a worker AND silent past the threshold → genuinely hung."""
    t = await _silent_implementing_task(store)
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(
        now=datetime.now(timezone.utc), active_ids={t.id})
    assert (t.id, "escalated_stalled") in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.ESCALATED
    assert (refreshed.blocker or {}).get("category") == "NOVEL_UNKNOWN"


@pytest.mark.asyncio
async def test_tick_without_active_ids_skips_the_sweep(store):
    """Standalone `nh wake` cannot know any worker's claims — no sweep."""
    t = await _silent_implementing_task(store)
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=datetime.now(timezone.utc))
    assert (t.id, "escalated_stalled") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


@pytest.mark.asyncio
async def test_scheduler_tick_passes_its_inflight_set(store, tmp_path):
    """The wiring: the scheduler judges exactly the tasks it claimed."""
    from no_human.core.scheduler import Scheduler

    captured: dict = {}

    class _Wake:
        async def tick(self, *, now=None, active_ids=None):
            captured["active_ids"] = active_ids
            return []

    sched = Scheduler(store, lambda task=None: None, max_workers=1,
                      wake_watcher=_Wake())
    sched._inflight.add("abc123")
    await sched.tick()
    assert captured["active_ids"] == {"abc123"}
