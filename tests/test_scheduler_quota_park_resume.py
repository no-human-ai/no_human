"""On startup and on quota-cooldown end, the scheduler must resume
quota-parked tasks (sunk cost, open PRs) before claiming fresh pending ones.

INCIDENT (2026-08-20): the server restarted at 14:25:32 UTC while four tasks
sat ``paused_quota`` with ``wake_check_at`` at 14:29 (their own raise-time-
plus-an-hour clock — the wall itself had already reset at 14:20). At
14:25:43 the fresh scheduler claimed four NEW pending tasks and filled
``max_workers=4``. At 14:29:31-51 the parked tasks woke (two with open PRs)
and found no slot; they sat ``implementing`` with zero events for 26-47
minutes behind four full fresh attempts.

``_resume_quota_parks`` (wired into `Scheduler.tick`, gated by the
``_was_cooling``/``_resume_parks_pending`` edge detector so it runs exactly
once at startup and once per cooldown lapse) closes this: parks behind a
passed wall are flipped back to IMPLEMENTING and claimed ahead of any
never-started PENDING task, and among PENDING tasks a prior attempt or an
open PR outranks a newcomer (`_rank_pending` / `_has_prior_work`).
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


async def _pending(store, n=1, *, prefix="pending"):
    ids = []
    for i in range(n):
        t = Task.new(f"{prefix} {i}", repo_path="/tmp/x")
        await store.create_task(t)
        ids.append(t.id)
    return ids


async def _quota_park(store, wake_at: datetime, *, raised_at: datetime | None = None,
                       auth_profile: str | None = None, pr_branch: str | None = None):
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
    if pr_branch:
        # context is a multi-writer column — update_task_columns deliberately
        # excludes it (R15); only merge_context/append_context_list may write it.
        t.context = await store.merge_context(t.id, {"pr_branch": pr_branch})
    await store.set_status(t, TaskStatus.PAUSED_QUOTA, validate=False)
    return t


async def test_first_tick_resumes_parks_before_claiming_pending(store, monkeypatch):
    """Startup: recover_quota_cooldown() (wall already passed => returns None
    but still records the wall) then the first tick() must resume both parks
    and claim them ahead of any pending task.

    p1 is the older-raised, open-PR park with a still-in-the-future OWN
    wake_check_at (a longer, stale estimate) — the pre-existing per-task wake
    rung would NOT touch it on its own clock. p2 is raised more recently with
    a shorter clock that has already elapsed, so it is the "newest park" that
    recover_quota_cooldown() resolves the session wall from. Only the new
    wall-gate (a park raised no later than a wall that HAS passed) sweeps p1
    up too — this is what must fail on current code."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    p1 = await _quota_park(store, now + timedelta(minutes=4),
                            raised_at=now - timedelta(minutes=65),
                            auth_profile="personal2", pr_branch="nh/task-p1")
    p2 = await _quota_park(store, now - timedelta(seconds=30),
                            raised_at=now - timedelta(minutes=10),
                            auth_profile="personal2")
    await _pending(store, 6)
    wake = WakeWatcher(store, {})
    events = []
    sched = Scheduler(store, _RecordingOrch, max_workers=4, wake_watcher=wake,
                      on_event=lambda k, t: events.append((k, t)))

    recovered = await sched.recover_quota_cooldown()
    assert recovered is None, "the wall has already passed — nothing to arm"
    assert sched._quota_wall is not None, "the wall must still be RECORDED"

    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert len(started) == 4  # 2 resumed parks + 2 pending fill the remaining slots
    assert set(started[:2]) == {p1.id, p2.id}, (
        "both wall-passed parks must be resumed and claimed before any "
        "never-started pending task")
    assert any(k == "quota_resume" for k, _ in events)

    t1 = await store.get_task(p1.id)
    t2 = await store.get_task(p2.id)
    assert t1.status in (TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING, TaskStatus.TESTING) \
        or t1.id in _RecordingOrch.started
    assert t2.status in (TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING, TaskStatus.TESTING) \
        or t2.id in _RecordingOrch.started


async def test_cooldown_end_triggers_the_same_sweep(store, monkeypatch):
    """A pool-wide cooldown lapsing mid-run must re-arm the sweep exactly
    once, on the FIRST tick after the wall drops — not on every idle tick."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    park = await _quota_park(store, now - timedelta(minutes=1),
                              raised_at=now - timedelta(minutes=61),
                              auth_profile="personal2")
    await _pending(store, 1)
    wake = WakeWatcher(store, {})
    sched = Scheduler(store, _RecordingOrch, max_workers=2, wake_watcher=wake)
    # Simulate a live pool-wide cooldown this process is holding, ending now.
    sched._quota_cooldown_until = now - timedelta(seconds=1)
    sched._quota_wall = now - timedelta(minutes=1)
    sched._quota_wall_profile = "personal2"
    sched._was_cooling = True
    sched._resume_parks_pending = False  # already swept once before this cooldown

    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert park.id in started, "the lapse itself must re-arm the resume sweep"


async def test_restart_on_a_new_profile_resumes_the_old_profiles_parks_first(store, monkeypatch):
    """INCIDENT (2026-08-23): three tasks (one HIGH) sat ``paused_quota``
    under profile ``personal2``'s wall. The server was stopped, the operator
    ran `nh auth use personal3`, and it restarted. ``personal3`` was never in
    cooldown — `recover_quota_cooldown` correctly refuses to arm
    ``_quota_cooldown_until`` from another profile's wall (see
    `test_a_wall_on_another_profile_is_not_recovered` in
    test_scheduler_quota_recovery.py) — yet the parked tasks sat stranded for
    ~1h while 4 fresh PENDING tasks filled the freed worker slots: this
    process's own cooldown clock was clear, but `_resume_quota_parks` was
    unconditionally skipping every row whose ``auth_profile`` differed from
    the one now active, with no fallback. Once the CURRENT profile is not in
    cooldown, the OTHER profile's wall is moot — the task will retry under
    the current (uncooled) profile — so it must resume immediately rather
    than wait out its own hour-long `wake_check_at` clock.

    Was `test_other_profile_park_is_left_to_its_own_clock`, which pinned the
    opposite (buggy) assertion; "left to its own clock" was exactly this
    incident's stranding, not a sanctioned wait — `nh auth use <other>` +
    restart is supposed to BE the way past the wall, not a way to strand
    sunk-cost work behind it.
    """
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal3")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    # A long own-clock wake (56 minutes out) proves the resume is driven by
    # "pool not in cooldown", not by the park's own wake_check_at happening
    # to already be due.
    park = await _quota_park(store, now + timedelta(minutes=56),
                              raised_at=now - timedelta(minutes=4),
                              auth_profile="personal2")
    await _pending(store, 3)
    wake = WakeWatcher(store, {})
    sched = Scheduler(store, _RecordingOrch, max_workers=1, wake_watcher=wake)

    recovered = await sched.recover_quota_cooldown()
    assert recovered is None, "another profile's wall must not arm this process's cooldown"
    assert sched._quota_cooldown_until is None

    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert started == [park.id], (
        "the OLD profile's parked task must claim the only free slot, "
        "resumed before any never-started pending task")
    parked = await store.get_task(park.id)
    assert parked.status in (TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
                              TaskStatus.TESTING) or parked.id in _RecordingOrch.started


async def _infra_park(store, wake_at: datetime, *, raised_at: datetime | None = None,
                       auth_profile: str | None = None):
    """A dead-SDK-session park: same ``category: QUOTA`` shape as a real
    quota wall, but stamped ``infra: True`` — see
    `tests/test_infra_park_is_not_a_wall.py`. It must never be swept by a
    wall-driven path; only its own `wake_check_at` may resume it."""
    t = Task.new("dead session", repo_path="/tmp/x")
    await store.create_task(t)
    blocker = {"category": "QUOTA", "wake_condition": "quota_refreshed",
               "raised_at": (raised_at or wake_at - timedelta(hours=1)).isoformat(),
               "root_cause_hypothesis": "JSON message exceeded maximum buffer size",
               "infra": True}
    if auth_profile:
        blocker["auth_profile"] = auth_profile
    t.blocker = blocker
    t.wake_check_at = wake_at.isoformat()
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.PAUSED_QUOTA, validate=False)
    return t


async def test_restart_on_a_new_profile_does_not_resume_a_cross_profile_infra_park(
        store, monkeypatch):
    """RED before the fix: the 2026-08-23 cross-profile fast path in
    `_resume_quota_parks` keyed only on `category == QUOTA` and `auth_profile
    != mine`, so it swept up INFRA parks (dead SDK sessions, `blocker.infra`)
    exactly like real quota walls — a rotation restart onto a new profile
    resumed a dead-transport park unconditionally, bypassing its own
    `wake_check_at` clock and recreating the retry-wave class
    `test_infra_park_is_not_a_wall.py` exists to prevent (an infra park must
    be "invisible to every pool clock", scheduler.py's own incident note at
    the top of `_resume_quota_parks`). A REAL quota park under the same
    other-profile wall must still resume (the 08-23 fix stays intact); the
    infra park, with an own-clock wake far in the future, must not."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal3")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    real_park = await _quota_park(store, now + timedelta(minutes=56),
                                   raised_at=now - timedelta(minutes=4),
                                   auth_profile="personal2")
    infra_park = await _infra_park(store, now + timedelta(hours=1),
                                    raised_at=now - timedelta(minutes=1),
                                    auth_profile="personal2")
    wake = WakeWatcher(store, {})
    sched = Scheduler(store, _RecordingOrch, max_workers=4, wake_watcher=wake)

    recovered = await sched.recover_quota_cooldown()
    assert recovered is None
    assert sched._quota_cooldown_until is None

    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert real_park.id in started, (
        "a genuine cross-profile quota park must still resume on restart")
    assert infra_park.id not in started, (
        "an infra park must never be swept by the cross-profile fast path — "
        "only its own wake_check_at may resume it")
    parked_infra = await store.get_task(infra_park.id)
    assert parked_infra.status == TaskStatus.PAUSED_QUOTA


async def test_park_whose_wall_has_not_passed_is_left_parked(store, monkeypatch):
    """No wall resolved for this process (no prior park recovered it) and
    the park's own wake_check_at is well beyond one poll interval away — it
    must stay parked, on its own wake clock."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    park = await _quota_park(store, now + timedelta(hours=2),
                              raised_at=now - timedelta(minutes=10),
                              auth_profile="personal2")
    await _pending(store, 1)
    wake = WakeWatcher(store, {})
    sched = Scheduler(store, _RecordingOrch, max_workers=2, wake_watcher=wake)

    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert park.id not in started
    parked = await store.get_task(park.id)
    assert parked.status == TaskStatus.PAUSED_QUOTA


async def test_pending_with_prior_attempt_or_open_pr_outranks_a_newcomer(store, monkeypatch):
    """Among claimable PENDING tasks (no parks involved here), one carrying
    sunk cost — a prior attempt row, or an open-PR context marker — must be
    claimed ahead of a never-started task, even though both are PENDING and
    the newcomer may have been created first (FIFO is the tiebreak WITHIN
    each group, not across them)."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)

    newcomer = Task.new("never started", repo_path="/tmp/x")
    await store.create_task(newcomer)

    with_pr = Task.new("has an open pr", repo_path="/tmp/x")
    await store.create_task(with_pr)
    with_pr.context = await store.merge_context(with_pr.id, {"pr_branch": "nh/task-with-pr"})

    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert started == [with_pr.id], (
        "the sunk-cost task (open PR) must be claimed before the "
        "never-started newcomer, despite being created second")
