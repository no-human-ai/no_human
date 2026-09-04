"""Queue health: stuck flag + drain ETA (D2 #4). Zero LLM, pure timestamps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from no_human.core.health import CLAIMABLE_STATUSES, queue_health
from no_human.core.scheduler import _CLAIMABLE
from no_human.core.task import Task, TaskStatus


async def _task(store, status: TaskStatus, *, updated_min_ago: int = 0):
    t = Task.new(f"t-{status.value}-{updated_min_ago}", repo_path="/r")
    await store.create_task(t)
    event = {"source": "test", "kind": "test_seed"} if status is TaskStatus.DONE else None
    await store.set_status(t, status, validate=False, event=event)
    if updated_min_ago:
        ts = (datetime.now(timezone.utc)
              - timedelta(minutes=updated_min_ago)).isoformat()
        await store.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
                               (ts, t.id))
        await store.db.commit()
    return t


async def test_empty_queue_is_never_stuck(store):
    h = await queue_health(store)
    assert h.open_tasks == 0 and h.stuck is False and h.eta_minutes is None


async def test_open_work_with_no_completions_is_stuck(store):
    await _task(store, TaskStatus.IMPLEMENTING)
    await _task(store, TaskStatus.DONE, updated_min_ago=120)  # stale completion
    h = await queue_health(store, stuck_after_minutes=30)
    assert h.stuck is True
    assert "nothing completed and no task changed stage in 30 minutes" in h.stuck_reason


async def test_recent_completion_clears_stuck_and_gives_an_eta(store):
    for _ in range(4):
        await _task(store, TaskStatus.PENDING)
    # 2 completions in the last 30 minutes → 0.0667/min → 4 open ≈ 60 min
    await _task(store, TaskStatus.DONE, updated_min_ago=5)
    await _task(store, TaskStatus.FAILED, updated_min_ago=10)

    h = await queue_health(store, stuck_after_minutes=30, window_minutes=30)
    assert h.stuck is False
    assert h.completed_in_window == 2
    assert h.eta_minutes == pytest.approx(60.0, rel=0.01)


async def test_tasks_at_a_human_gate_are_not_open_work(store):
    """A board full of PRs awaiting approval is SUCCESS, not a stall — the
    queue owes nothing until the human acts."""
    await _task(store, TaskStatus.AWAITING_APPROVAL)
    await _task(store, TaskStatus.ESCALATED)
    h = await queue_health(store, stuck_after_minutes=1)
    assert h.open_tasks == 0
    assert h.at_gate == 2
    assert h.stuck is False


async def test_eta_is_none_not_zero_when_unknowable(store):
    await _task(store, TaskStatus.PENDING)
    h = await queue_health(store, window_minutes=30)
    assert h.eta_minutes is None, "an unknown ETA must never render as a number"


async def test_recent_stage_transition_is_not_stuck(store):
    """Live false alarm (2026-07-24): 30-60-minute tasks on a ONE-worker pool
    routinely complete nothing for 30 minutes while the pipeline is visibly
    moving (a task entered review 5 minutes earlier). A recent STATE event —
    a stage transition, which a busy-looping coder never emits — is motion,
    not a stall."""
    import time
    await _task(store, TaskStatus.IMPLEMENTING)
    await _task(store, TaskStatus.DONE, updated_min_ago=120)   # stale completion
    await store.save_events("sometask", [
        {"ts": time.time() - 300, "kind": "state", "text": "reviewing"}])
    h = await queue_health(store, stuck_after_minutes=30)
    assert h.stuck is False


async def test_stale_transitions_and_no_completions_is_still_stuck(store):
    """The anti-busy-loop property survives: state events OLDER than the
    window do not count as motion."""
    import time
    await _task(store, TaskStatus.IMPLEMENTING)
    await store.save_events("sometask", [
        {"ts": time.time() - 3600, "kind": "state", "text": "planning"}])
    h = await queue_health(store, stuck_after_minutes=30)
    assert h.stuck is True


async def _attempt(store, task_id: str, *, duration_seconds: float):
    """Insert a completed attempt row with a known wall-time, via started_at/
    completed_at (no wall_time column — SCRUM-70 derives duration from these)."""
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=duration_seconds)
    aid = await store.create_attempt(task_id, 1)
    await store.db.execute(
        "UPDATE attempts SET started_at = ?, completed_at = ?, status = 'succeeded' "
        "WHERE id = ?",
        (started.isoformat(), now.isoformat(), aid))
    await store.db.commit()
    return aid


async def test_queue_health_idle_shape(store):
    """SCRUM-70 AC: truthful when idle."""
    h = await queue_health(store, max_workers=2)
    assert h.workers_busy == 0
    assert h.max_workers == 2
    assert h.queue_depth == 0
    assert h.est_drain_seconds == 0.0
    d = h.as_dict()
    for key in ("workers_busy", "max_workers", "queue_depth", "est_drain_seconds"):
        assert key in d
    assert isinstance(d["workers_busy"], int)
    assert isinstance(d["max_workers"], int)
    assert isinstance(d["queue_depth"], int)
    assert isinstance(d["est_drain_seconds"], float)


async def test_queue_health_busy_shape(store):
    """SCRUM-70 AC: truthful under load — queue_depth excludes the inflight
    id, est_drain_seconds = median * depth / available_workers."""
    claimed = await _task(store, TaskStatus.IMPLEMENTING)
    await _task(store, TaskStatus.PENDING)
    await _task(store, TaskStatus.PENDING)

    await _attempt(store, claimed.id, duration_seconds=10)
    await _attempt(store, claimed.id, duration_seconds=20)
    await _attempt(store, claimed.id, duration_seconds=30)  # median = 20

    h = await queue_health(store, inflight_ids={claimed.id}, max_workers=2)
    assert h.workers_busy == 1
    assert h.max_workers == 2
    assert h.queue_depth == 2   # 3 claimable - 1 inflight
    # available = max_workers(2) - busy(1) = 1
    assert h.est_drain_seconds == pytest.approx(20.0 * 2 / 1, rel=0.01)


async def test_queue_health_null_estimate_when_no_attempt_history(store):
    """SCRUM-70 AC: null estimate path — depth > 0 but no completed attempts."""
    await _task(store, TaskStatus.PENDING)
    h = await queue_health(store, max_workers=2)
    assert h.queue_depth == 1
    assert h.est_drain_seconds is None


async def test_queue_health_null_estimate_when_no_free_capacity(store):
    """SCRUM-70 AC: honest null, never a fabricated/infinite number, when
    workers_busy == max_workers (available capacity is zero)."""
    claimed = await _task(store, TaskStatus.IMPLEMENTING)
    await _task(store, TaskStatus.PENDING)
    await _attempt(store, claimed.id, duration_seconds=10)

    h = await queue_health(store, inflight_ids={claimed.id}, max_workers=1)
    assert h.queue_depth == 1
    assert h.workers_busy == h.max_workers == 1
    assert h.est_drain_seconds is None


def test_claimable_statuses_match_scheduler():
    """Drift guard: health.py's CLAIMABLE_STATUSES is a hand-copied mirror of
    scheduler._CLAIMABLE (comment-only tie). If the scheduler's claim set ever
    changes without updating health.py, queue_depth silently lies on the
    board — fail loudly instead."""
    assert set(CLAIMABLE_STATUSES) == {s.value for s in _CLAIMABLE}


async def test_quota_pause_reports_paused_state_with_eta_from_reset(store):
    """2026-08-20 evidence: `/api/queue/health` returned "not stuck, 0 busy,
    7 queued, ETA 210 min" while the pool was in its quota cooldown, after
    tasks parked `paused_quota` on the personal2 session limit. Every field
    individually true, the picture false. With a live cooldown, health must
    say so — and the ETA must count from the reset, not from now."""
    for _ in range(7):
        await _task(store, TaskStatus.PENDING)
    done_task = await _task(store, TaskStatus.DONE)
    await _attempt(store, done_task.id, duration_seconds=60)

    parked = await _task(store, TaskStatus.PAUSED_QUOTA)
    parked.blocker = {"auth_profile": "personal2"}
    await store.update_task(parked)

    reset_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    h = await queue_health(store, max_workers=4, quota_cooldown_until=reset_at)

    assert h.paused is True
    assert h.paused_reason == "quota"
    assert h.paused_until == reset_at.isoformat()
    assert h.paused_profile == "personal2"
    assert h.stuck is False, "a quota wall is a deliberate pause, not a wedge"

    # available = max_workers(4) - busy(0) = 4; normal drain = 60s * 7 / 4
    expected_normal_drain = 60.0 * 7 / 4
    resume_in = (reset_at - datetime.now(timezone.utc)).total_seconds()
    assert h.est_drain_seconds == pytest.approx(
        expected_normal_drain + resume_in, abs=2)
    assert h.eta_minutes == pytest.approx(h.est_drain_seconds / 60.0, rel=0.01)


async def test_infra_breaker_cooldown_reports_paused_reason_infra_without_profile(store):
    """Independent review of PR #553 (2026-08-21): the infra breaker arms the
    SAME clock a quota park uses, so every cooldown was labelled "quota" and
    attributed to whichever `paused_quota` row happened to be newest — even
    an hours-old, unrelated park. An infra-breaker cooldown must report
    `paused_reason == "infra"` and skip profile attribution entirely."""
    for _ in range(7):
        await _task(store, TaskStatus.PENDING)
    done_task = await _task(store, TaskStatus.DONE)
    await _attempt(store, done_task.id, duration_seconds=60)

    # Stale, unrelated park — must NOT be blamed for the breaker trip.
    parked = await _task(store, TaskStatus.PAUSED_QUOTA)
    parked.blocker = {"auth_profile": "personal2"}
    await store.update_task(parked)

    reset_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    h = await queue_health(store, max_workers=4, infra_cooldown_until=reset_at)

    assert h.paused is True
    assert h.paused_reason == "infra"
    assert h.paused_profile is None
    assert h.paused_until == reset_at.isoformat()
    assert h.stuck is False

    expected_normal_drain = 60.0 * 7 / 4
    resume_in = (reset_at - datetime.now(timezone.utc)).total_seconds()
    assert h.est_drain_seconds == pytest.approx(
        expected_normal_drain + resume_in, abs=2)


async def test_infra_cooldown_wins_when_both_clocks_are_set(store):
    """Defence in depth: the scheduler guarantees only one clock is armed at
    a time, but health.py must not silently prefer quota if both are somehow
    non-None."""
    await _task(store, TaskStatus.PENDING)
    reset_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    h = await queue_health(
        store, max_workers=2,
        infra_cooldown_until=reset_at, quota_cooldown_until=reset_at)
    assert h.paused_reason == "infra"
    assert h.paused_profile is None


async def test_paused_pool_is_not_stuck_even_with_stale_tasks(store):
    """Branch-order pin: the pause check must run BEFORE the stuck check, or
    a pool idling on purpose behind a cooldown reports `stuck: true` next to
    `paused: true` — a contradictory payload."""
    for i in range(9):
        await _task(store, TaskStatus.IMPLEMENTING, updated_min_ago=60)

    cooldown_at = datetime.now(timezone.utc) + timedelta(minutes=20)
    h = await queue_health(
        store, max_workers=4, stuck_after_minutes=30,
        quota_cooldown_until=cooldown_at)
    assert h.stuck is False
    assert h.stuck_reason == ""

    h_infra = await queue_health(
        store, max_workers=4, stuck_after_minutes=30,
        infra_cooldown_until=cooldown_at)
    assert h_infra.stuck is False
    assert h_infra.stuck_reason == ""

    h_unpaused = await queue_health(store, max_workers=4, stuck_after_minutes=30)
    assert h_unpaused.stuck is True
    assert h_unpaused.stuck_reason != ""


async def test_expired_quota_cooldown_is_not_a_pause(store):
    """A cooldown timestamp already in the past must not report paused —
    the scheduler would have already resumed dispatch."""
    await _task(store, TaskStatus.PENDING)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    h = await queue_health(store, max_workers=2, quota_cooldown_until=past)
    assert h.paused is False
    assert h.paused_reason is None
    assert h.paused_until is None


async def test_no_quota_cooldown_leaves_payload_unchanged(store):
    """Default (no scheduler cooldown passed): existing callers/behaviour are
    untouched — this is the "with no cooldown the payload is unchanged from
    today" half of the acceptance criterion."""
    for _ in range(4):
        await _task(store, TaskStatus.PENDING)
    await _task(store, TaskStatus.DONE, updated_min_ago=5)

    h = await queue_health(store, max_workers=2)
    assert h.paused is False
    assert h.paused_reason is None
    assert h.paused_until is None
    assert h.paused_profile is None
    d = h.as_dict()
    assert d["paused"] is False
    assert d["paused_reason"] is None
    assert d["paused_until"] is None
    assert d["paused_profile"] is None


async def test_queue_depth_excludes_done_and_blocked(store):
    """Boundary test: queue_depth counts only CLAIMABLE_STATUSES. A DONE task
    and a BLOCKED (human_stopped) task must not inflate the count — only the
    PENDING task is claimable."""
    await _task(store, TaskStatus.DONE)

    blocked = await _task(store, TaskStatus.IMPLEMENTING)
    blocked.blocker = {"human_stopped": True}
    await store.set_status(blocked, TaskStatus.BLOCKED, validate=False)
    await store.update_task(blocked)

    await _task(store, TaskStatus.PENDING)

    h = await queue_health(store, max_workers=2)
    assert h.queue_depth == 1


async def test_paused_profile_names_the_wall_not_a_newer_infra_park(store):
    """An infra park (`blocker.infra`, a dead SDK session) never armed the
    pool clock, so it must not be the row that labels whose wall the pool is
    waiting on — even when it is the NEWEST paused_quota row. RED before the
    fix: `_quota_profile` took the newest row unconditionally."""
    await _task(store, TaskStatus.PENDING)   # something owed, or health returns early
    wall = await _task(store, TaskStatus.PAUSED_QUOTA)
    wall.blocker = {"auth_profile": "personal2"}
    await store.update_task(wall)
    dead = await _task(store, TaskStatus.PAUSED_QUOTA)
    dead.blocker = {"auth_profile": "personal3", "infra": True}
    await store.update_task(dead)
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), dead.id))
    await store.db.commit()

    reset_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    h = await queue_health(store, max_workers=4, quota_cooldown_until=reset_at)

    assert h.paused_profile == "personal2", (
        f"the infra park's profile was reported: {h.paused_profile!r}")
