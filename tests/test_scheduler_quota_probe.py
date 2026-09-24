"""issue #431: after a FALLBACK quota wall (the self-correcting retry hour a
guessed `QuotaExhausted.reset_exact=False` produces, not a parsed reset)
lapses, the pool must send exactly ONE probe task at the wall before
resuming full-width dispatch — not `max_workers` tasks at once.

INCIDENT: 371 of 372 zero-token quota-exhausted attempts over 30 days
started INSIDE a wall this process had already recorded. The fallback hour
is a guess, not evidence the wall actually reset; feeding it `max_workers`
attempts burns that many zero-token attempts the moment it is wrong.

An EXACT wall (`reset_exact=True` — the wall's own parsed reset time, not a
guess) carries no such doubt and keeps resuming at full width, exactly as
today (see `test_scheduler_quota_park_resume.py`). A wall stamped for a
DIFFERENT `auth_profile` than the one now active must never arm a probe for
THIS pool either — that is the other half of the same incident class
(a probe for one profile must never stand in for another's wall).

Modelled on `test_scheduler_quota_park_resume.py`: same `store` fixture,
same "manually set the just-lapsed cooldown state, then call `tick()`"
pattern as `test_cooldown_end_triggers_the_same_sweep`, and the same
`monkeypatch.setattr(sched_mod, "active_auth_profile", ...)` convention.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


from no_human.blockers.wake import WakeWatcher
from no_human.core.orchestrator import TaskOutcome
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus


class _RecordingOrch:
    """Clean finish every time — mirrors the other scheduler test files'
    stub. Used for the "probe succeeds" and "exact wall" scenarios."""
    started: list[str] = []

    def __init__(self, task):
        self.task = task
        self._sink = None

    async def run_task(self, task):
        _RecordingOrch.started.append(task.id)
        await asyncio.sleep(0)
        return None


class _CrashingOrch:
    """Every dispatch raises — used to prove a probe that crashes does not
    wedge the pool at one worker forever (the `finally`-based disarm)."""
    started: list[str] = []

    def __init__(self, task):
        self.task = task
        self._sink = None

    async def run_task(self, task):
        _CrashingOrch.started.append(task.id)
        await asyncio.sleep(0)
        raise RuntimeError("boom")


class _SlowOrch:
    """Never returns until released — lets a test fire a SECOND `tick()`
    while the first probe task is still inflight (neither finished nor
    re-parked). Regression for a cap that was re-derived from
    `max_workers - len(inflight)` every tick instead of gated on a probe
    already being outstanding: with 4 workers and 1 inflight, that formula
    is 3, and `min(3, 1)` still lets a second task through."""
    started: list[str] = []
    release: asyncio.Event | None = None

    def __init__(self, task):
        self.task = task
        self._sink = None

    async def run_task(self, task):
        _SlowOrch.started.append(task.id)
        assert _SlowOrch.release is not None
        await _SlowOrch.release.wait()
        return None


class _ProbeReparkOrch:
    """Re-parks on the SAME wall every dispatch, with a fresh fallback
    (`reset_exact: False`) reset a few minutes out — simulates the wall
    still being up when the probe checks it. `store` is set by the test
    before the scheduler dispatches, since the scheduler's factory only
    passes the task, not the store."""
    started: list[str] = []
    store = None

    def __init__(self, task):
        self.task = task
        self._sink = None

    async def run_task(self, task):
        _ProbeReparkOrch.started.append(task.id)
        await asyncio.sleep(0)
        now = datetime.now(timezone.utc)
        new_wake = now + timedelta(minutes=5)
        task.blocker = {
            "category": "QUOTA", "wake_condition": "quota_refreshed",
            "raised_at": now.isoformat(),
            "root_cause_hypothesis": "still hit the wall",
            "auth_profile": "personal2",
            "reset_exact": False,
        }
        task.wake_check_at = new_wake.isoformat()
        await _ProbeReparkOrch.store.update_task_columns(task)
        await _ProbeReparkOrch.store.set_status(
            task, TaskStatus.PAUSED_QUOTA, validate=False)
        return TaskOutcome(task=task, status=TaskStatus.PAUSED_QUOTA)


async def _pending(store, n=1, *, prefix="pending"):
    ids = []
    for i in range(n):
        t = Task.new(f"{prefix} {i}", repo_path="/tmp/x")
        await store.create_task(t)
        ids.append(t.id)
    return ids


async def _quota_park(store, wake_at: datetime, *, raised_at: datetime | None = None,
                       auth_profile: str | None = None, reset_exact: bool | None = None):
    t = Task.new("parked on the wall", repo_path="/tmp/x")
    await store.create_task(t)
    blocker = {"category": "QUOTA", "wake_condition": "quota_refreshed",
               "raised_at": (raised_at or wake_at - timedelta(hours=1)).isoformat(),
               "root_cause_hypothesis": "You've hit your session limit"}
    if auth_profile:
        blocker["auth_profile"] = auth_profile
    if reset_exact is not None:
        blocker["reset_exact"] = reset_exact
    t.blocker = blocker
    t.wake_check_at = wake_at.isoformat()
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.PAUSED_QUOTA, validate=False)
    return t


def _arm_just_lapsed(sched, *, now, wall_started, profile, exact):
    """Simulate "this process was holding a cooldown that just ended,
    entering this tick" — the same manual-state pattern
    `test_cooldown_end_triggers_the_same_sweep` uses, extended with the new
    `_quota_wall_exact` field this fix adds."""
    sched._quota_cooldown_until = now - timedelta(seconds=1)
    sched._quota_wall = wall_started
    sched._quota_wall_profile = profile
    sched._quota_wall_exact = exact
    sched._was_cooling = True
    sched._resume_parks_pending = False  # already swept once before this cooldown


async def test_one_probe_dispatched_after_a_fallback_wall_lapses(store, monkeypatch):
    """THE test that fails on main: on main every park's fallback status is
    indistinguishable from an exact one, so the lapse sweep dispatches
    max_workers=4 straight into a wall the pool only GUESSED had reset. With
    the fix, exactly one task probes it."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    park = await _quota_park(store, now - timedelta(minutes=1),
                              raised_at=now - timedelta(minutes=61),
                              auth_profile="personal2", reset_exact=False)
    await _pending(store, 6)
    wake = WakeWatcher(store, {})
    events = []
    sched = Scheduler(store, _RecordingOrch, max_workers=4, wake_watcher=wake,
                      on_event=lambda k, t: events.append((k, t)))
    _arm_just_lapsed(sched, now=now, wall_started=now - timedelta(minutes=1),
                      profile="personal2", exact=False)

    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert len(started) == 1, (
        "a FALLBACK wall must dispatch exactly one probe task, not "
        f"max_workers — got {len(started)}")
    assert park.id in started, "the parked task is the one probing its own wall"
    assert any(k == "quota_probe" for k, _ in events)


async def test_the_probe_persists_while_the_wall_is_still_up(store, monkeypatch):
    """The intake answer's 'persistent per probe cycle': if the probe itself
    re-parks (the wall is genuinely still up), the NEXT lapse must again
    dispatch exactly one probe, not fall back to full width because a probe
    was already tried once."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _ProbeReparkOrch.started.clear()
    _ProbeReparkOrch.store = store
    now = datetime.now(timezone.utc)
    park = await _quota_park(store, now - timedelta(minutes=1),
                              raised_at=now - timedelta(minutes=61),
                              auth_profile="personal2", reset_exact=False)
    await _pending(store, 6)
    wake = WakeWatcher(store, {})
    events = []
    sched = Scheduler(store, _ProbeReparkOrch, max_workers=4, wake_watcher=wake,
                      on_event=lambda k, t: events.append((k, t)))
    _arm_just_lapsed(sched, now=now, wall_started=now - timedelta(minutes=1),
                      profile="personal2", exact=False)

    started1 = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert len(started1) == 1
    assert park.id in started1
    assert len(_ProbeReparkOrch.started) == 1
    assert sched._quota_probe_armed, (
        "the probe re-parked — the wall is still up, so the probe flag "
        "must stay armed rather than clearing on a re-park")

    # Simulate the SECOND lapse: the fresh fallback cooldown `_run`'s
    # live-arming just set (~5 minutes out) has now also ended.
    wall2 = sched._quota_wall
    assert wall2 is not None and wall2 > now, "the re-park must have armed a fresh wall"
    sched._was_cooling = True
    now2 = wall2 + timedelta(seconds=1)

    started2 = await sched.tick(now=now2)
    await asyncio.sleep(0.05)

    assert len(started2) == 1, (
        "a SECOND fallback lapse must again probe with exactly one task")
    assert park.id in started2
    assert len(_ProbeReparkOrch.started) == 2


async def test_a_second_tick_before_the_probe_returns_dispatches_nothing(store, monkeypatch):
    """Regression: the one-probe guarantee must hold across the WHOLE
    unverified window, not just within a single tick. A tick that lands
    while the first probe task is still inflight (not yet finished or
    re-parked) must dispatch nothing — not a second task into the same
    unverified wall."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _SlowOrch.started.clear()
    _SlowOrch.release = asyncio.Event()
    now = datetime.now(timezone.utc)
    park = await _quota_park(store, now - timedelta(minutes=1),
                              raised_at=now - timedelta(minutes=61),
                              auth_profile="personal2", reset_exact=False)
    await _pending(store, 6)
    wake = WakeWatcher(store, {})
    events = []
    sched = Scheduler(store, _SlowOrch, max_workers=4, wake_watcher=wake,
                      on_event=lambda k, t: events.append((k, t)))
    _arm_just_lapsed(sched, now=now, wall_started=now - timedelta(minutes=1),
                      profile="personal2", exact=False)

    started1 = await sched.tick(now=now)
    await asyncio.sleep(0.01)  # let the probe task actually start running

    assert len(started1) == 1
    assert park.id in started1
    assert sched._quota_probe_id in started1, (
        "the probe's own task id must be tracked as inflight")
    assert len(_SlowOrch.started) == 1

    # A second tick lands while the probe is still outstanding. On the
    # buggy formula, slots = max_workers(4) - len(inflight)(1) = 3, then
    # min(3, 1) = 1 — a second task would dispatch here.
    started2 = await sched.tick(now=now)

    assert started2 == [], (
        "a tick while the probe is still inflight must dispatch nothing — "
        f"got {started2}")
    assert len(_SlowOrch.started) == 1, (
        "no second task may probe the wall while one is already in flight")

    _SlowOrch.release.set()
    await asyncio.sleep(0.05)
    assert sched._quota_probe_id is None, (
        "the probe id must clear once the inflight task actually finishes")


async def test_a_clean_probe_restores_full_width_dispatch(store, monkeypatch):
    """The probe finishes clean (no re-park) — the wall is actually open.
    The pool must not stay wedged at one worker; the very next tick
    dispatches at full width again."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    park = await _quota_park(store, now - timedelta(minutes=1),
                              raised_at=now - timedelta(minutes=61),
                              auth_profile="personal2", reset_exact=False)
    await _pending(store, 6)
    wake = WakeWatcher(store, {})
    sched = Scheduler(store, _RecordingOrch, max_workers=4, wake_watcher=wake)
    _arm_just_lapsed(sched, now=now, wall_started=now - timedelta(minutes=1),
                      profile="personal2", exact=False)

    started1 = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert len(started1) == 1
    assert park.id in started1
    assert sched._quota_probe_armed is False, (
        "a clean probe finish must disarm immediately — the wall is open")

    now2 = now + timedelta(seconds=5)
    started2 = await sched.tick(now=now2)
    await asyncio.sleep(0.05)

    assert len(started2) == 4, (
        f"full width must resume once the probe clears the wall — got {len(started2)}")


async def test_a_probe_crash_does_not_wedge_the_pool(store, monkeypatch):
    """The probe worker crashes outright (transport death, not a re-park).
    The pool must not stay capped at one worker forever — the disarm lives
    in `finally`, not the success path."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _CrashingOrch.started.clear()
    now = datetime.now(timezone.utc)
    park = await _quota_park(store, now - timedelta(minutes=1),
                              raised_at=now - timedelta(minutes=61),
                              auth_profile="personal2", reset_exact=False)
    await _pending(store, 6)
    wake = WakeWatcher(store, {})
    sched = Scheduler(store, _CrashingOrch, max_workers=4, wake_watcher=wake)
    _arm_just_lapsed(sched, now=now, wall_started=now - timedelta(minutes=1),
                      profile="personal2", exact=False)

    started1 = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert len(started1) == 1
    assert sched._quota_probe_armed is False, (
        "a crashed probe must not leave the pool wedged at one worker")
    parked = await store.get_task(park.id)
    assert parked.status == TaskStatus.FAILED

    now2 = now + timedelta(seconds=5)
    started2 = await sched.tick(now=now2)
    await asyncio.sleep(0.05)

    assert len(started2) == 4, (
        f"full width must resume after a crashed probe — got {len(started2)}")


async def test_an_exact_wall_resumes_at_full_width(store, monkeypatch):
    """Probe mode is opt-in on evidence: a wall recorded with
    `reset_exact=True` (the wall's own parsed reset time, e.g. a dated
    reset within the 8-day ceiling) is not a guess, and lapsing it must
    dispatch max_workers immediately, exactly as before this fix."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    park = await _quota_park(store, now - timedelta(minutes=1),
                              raised_at=now - timedelta(minutes=61),
                              auth_profile="personal2", reset_exact=True)
    await _pending(store, 6)
    wake = WakeWatcher(store, {})
    events = []
    sched = Scheduler(store, _RecordingOrch, max_workers=4, wake_watcher=wake,
                      on_event=lambda k, t: events.append((k, t)))
    _arm_just_lapsed(sched, now=now, wall_started=now - timedelta(minutes=1),
                      profile="personal2", exact=True)

    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert len(started) == 4, (
        f"an EXACT wall must resume at full width, not probe — got {len(started)}")
    assert park.id in started
    assert not any(k == "quota_probe" for k, _ in events)


async def test_another_profiles_wall_does_not_arm_a_probe(store, monkeypatch):
    """Pitfall 2: a fallback wall stamped for a DIFFERENT auth_profile than
    the one now active must never arm a probe for THIS pool — dispatch stays
    full-width, and the other profile's park resumes exactly as it does
    today (see test_restart_on_a_new_profile_resumes_the_old_profiles_parks_first
    in test_scheduler_quota_park_resume.py)."""
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    work_park = await _quota_park(store, now - timedelta(minutes=1),
                                   raised_at=now - timedelta(minutes=61),
                                   auth_profile="work", reset_exact=False)
    await _pending(store, 6)
    wake = WakeWatcher(store, {})
    events = []
    sched = Scheduler(store, _RecordingOrch, max_workers=4, wake_watcher=wake,
                      on_event=lambda k, t: events.append((k, t)))
    _arm_just_lapsed(sched, now=now, wall_started=now - timedelta(minutes=1),
                      profile="work", exact=False)

    started = await sched.tick(now=now)
    await asyncio.sleep(0.05)

    assert len(started) == 4, (
        f"a different profile's fallback wall must not cap dispatch — got {len(started)}")
    assert work_park.id in started, (
        "the other profile's park must resume exactly as it does today")
    assert not any(k == "quota_probe" for k, _ in events)
