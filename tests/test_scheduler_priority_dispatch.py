"""tasks.priority was stored, shown, and validated — but never read by the
scheduler's dispatch order (`_rank_pending`), so a human raising a task to
`high` changed nothing about when it ran. `_rank_pending` now stable-sorts
each of its existing prior-work/fresh groups by `priority_rank` before FIFO,
so priority reorders the PENDING queue without preempting anything already
claimed/running or any quota-parked resume.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


from no_human.blockers.wake import WakeWatcher
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


async def _pending(store, n=1, *, prefix="pending", priority=None):
    ids = []
    for i in range(n):
        t = Task.new(f"{prefix} {i}", repo_path="/tmp/x")
        if priority is not None:
            t.priority = priority
        await store.create_task(t)
        ids.append(t.id)
    return ids


async def _quota_park(store, wake_at: datetime, *, raised_at: datetime | None = None,
                       auth_profile: str | None = None):
    t = Task.new("parked on the wall", repo_path="/tmp/x")
    await store.create_task(t)
    blocker = {"category": "QUOTA", "wake_condition": "quota_refreshed",
               "raised_at": (raised_at or wake_at - timedelta(hours=1)).isoformat(),
               "root_cause_hypothesis": "You've hit your session limit"}
    if auth_profile:
        blocker["auth_profile"] = auth_profile
    t.blocker = blocker
    t.wake_check_at = wake_at.isoformat()
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.PAUSED_QUOTA, validate=False)
    return t


async def test_high_priority_pending_dispatches_before_older_medium(store):
    """RED on current main: a medium task created first, then a high one —
    today's FIFO-only `_rank_pending` returns the medium; priority must win."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)

    medium_ids = await _pending(store, 1, prefix="medium", priority="medium")
    high_ids = await _pending(store, 1, prefix="high", priority="high")

    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert started == [high_ids[0]], (
        "the high-priority task must dispatch before the older medium one")


async def test_equal_priority_preserves_fifo(store):
    """Negative control: with all priorities equal, dispatch order is
    unchanged from today (FIFO after quota resumes)."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)

    ids = await _pending(store, 5, prefix="fifo")

    sched = Scheduler(store, _RecordingOrch, max_workers=5)
    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert started == ids


async def test_null_priority_ranks_as_medium(store):
    """A NULL priority column (a row written before this fix, or hand-
    written) ranks as medium — ahead of `low`, behind `high` — never breaks
    dispatch, and does not move a task's queue position."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)

    null_ids = await _pending(store, 1, prefix="null-prio")
    await store.db.execute(
        "UPDATE tasks SET priority = NULL WHERE id = ?", (null_ids[0],))
    await store.db.commit()
    low_ids = await _pending(store, 1, prefix="low", priority="low")
    high_ids = await _pending(store, 1, prefix="high", priority="high")

    sched = Scheduler(store, _RecordingOrch, max_workers=3)
    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert started == [high_ids[0], null_ids[0], low_ids[0]], (
        "NULL priority must rank as medium: behind high, ahead of low")


async def test_quota_park_resume_outranks_a_high_priority_pending(store, monkeypatch):
    """Quota-parked resumes still precede every PENDING task regardless of
    priority — priority only reorders within PENDING, it never lets a
    PENDING task jump ahead of a resumed park (#556's guarantee unchanged)."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)

    park = await _quota_park(store, now - timedelta(minutes=1),
                              raised_at=now - timedelta(minutes=61),
                              auth_profile="personal2")
    high_ids = await _pending(store, 1, prefix="high", priority="high")

    wake = WakeWatcher(store, {})
    sched = Scheduler(store, _RecordingOrch, max_workers=1, wake_watcher=wake)
    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert started == [park.id], (
        "the resumed quota park must be claimed before the high-priority "
        f"pending task; got {started}")
    assert high_ids  # sanity: the high task exists and was passed over


async def test_prior_work_medium_outranks_fresh_high(store):
    """Priority sorts WITHIN the prior-work/fresh split, not above it — a
    medium task carrying sunk cost (an open PR) still outranks a fresh
    never-started high task (#556's split is unchanged)."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)

    with_pr = Task.new("has an open pr, medium", repo_path="/tmp/x")
    with_pr.priority = "medium"
    await store.create_task(with_pr)
    with_pr.context = await store.merge_context(
        with_pr.id, {"pr_branch": "nh/task-with-pr"})

    fresh_high = Task.new("never started, high", repo_path="/tmp/x")
    fresh_high.priority = "high"
    await store.create_task(fresh_high)

    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert started == [with_pr.id], (
        "the sunk-cost medium task must still be claimed before the fresh "
        f"high-priority task; got {started}")


async def test_prior_work_high_outranks_older_prior_work_low(store):
    """Priority also sorts WITHIN the prior-work group itself — a high task
    carrying sunk cost must outrank an older low task that also carries sunk
    cost, even though FIFO alone would put the older one first."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)

    older_low = Task.new("older prior work, low", repo_path="/tmp/x")
    older_low.priority = "low"
    await store.create_task(older_low)
    older_low.context = await store.merge_context(
        older_low.id, {"pr_branch": "nh/task-a"})

    newer_high = Task.new("newer prior work, high", repo_path="/tmp/x")
    newer_high.priority = "high"
    await store.create_task(newer_high)
    newer_high.context = await store.merge_context(
        newer_high.id, {"pr_branch": "nh/task-b"})

    sched = Scheduler(store, _RecordingOrch, max_workers=2)
    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert started == [newer_high.id, older_low.id], (
        "both tasks are prior-work, so the prior/fresh split is a no-op and "
        "only priority-within-prior-work ordering can produce this order; "
        f"got {started}")
