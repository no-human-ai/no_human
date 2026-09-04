"""A restarted scheduler must not re-dispatch into a quota wall the DB already
names.

INCIDENT (2026-08-20, task 426fe079): the personal2 session wall was hit at
16:54Z (resets 19:20Z); five tasks sat paused_quota with that banner and the
running scheduler held its cooldown. At 18:01Z the server was restarted; the
NEW process started with ``_quota_cooldown_until = None`` (armed only when a
dispatch of its OWN dies) and immediately dispatched four pending tasks, each
of which died on the still-closed wall — four dead SDK sessions and four
parks for a wall the tasks table already recorded, with its reset time.

The cooldown is re-derived from the world at startup: the newest
``paused_quota`` row's ``wake_check_at`` — the same field the live arming
reads (`Scheduler._run`) — arms the pool if it is still in the future.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path


import no_human.core.scheduler as scheduler_mod
from no_human.core.health import queue_health
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


async def _pending(store, n=1):
    ids = []
    for i in range(n):
        t = Task.new(f"pending {i}", repo_path="/tmp/x")
        await store.create_task(t)
        ids.append(t.id)
    return ids


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


async def test_startup_recovers_an_open_wall_from_the_db(store):
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    await _quota_park(store, now + timedelta(minutes=40))
    await _pending(store, 2)
    events = []
    sched = Scheduler(store, _RecordingOrch, max_workers=2,
                      on_event=lambda k, t: events.append((k, t)))

    recovered = await sched.recover_quota_cooldown()
    started = await sched.tick()

    assert recovered is not None and recovered > now
    assert sched._quota_cooldown_until == recovered
    assert started == [] and _RecordingOrch.started == [], (
        "a fresh process dispatched into a wall the DB already named")
    assert any(k == "quota_pause" and "recovered" in t for k, t in events)
    assert sched.health_snapshot()["idle_reason"] == "quota_cooldown"


async def test_a_passed_wall_arms_nothing(store):
    """Negative control: a park whose reset is behind us is history, not a wall."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    await _quota_park(store, now - timedelta(minutes=5))
    await _pending(store, 1)
    sched = Scheduler(store, _RecordingOrch, max_workers=1)

    assert await sched.recover_quota_cooldown() is None
    assert sched._quota_cooldown_until is None
    started = await sched.tick()
    await asyncio.sleep(0.05)
    assert len(started) == 1


async def test_no_park_arms_nothing(store):
    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    assert await sched.recover_quota_cooldown() is None
    assert sched._quota_cooldown_until is None


async def test_the_newest_park_wins_not_the_longest(store):
    """Two walls recorded: an older park with a later (stale, hand-edited)
    reset and the newest park. The newest row is the current wall."""
    now = datetime.now(timezone.utc)
    await _quota_park(store, now + timedelta(hours=5), raised_at=now - timedelta(hours=3))
    newest = await _quota_park(store, now + timedelta(minutes=20), raised_at=now - timedelta(minutes=10))
    sched = Scheduler(store, _RecordingOrch, max_workers=1)

    recovered = await sched.recover_quota_cooldown()

    assert recovered is not None
    assert recovered.isoformat() == newest.wake_check_at


async def test_recovery_never_shortens_a_cooldown_this_process_already_holds(store):
    now = datetime.now(timezone.utc)
    await _quota_park(store, now + timedelta(minutes=20))
    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    later = now + timedelta(hours=2)
    sched._quota_cooldown_until = later

    await sched.recover_quota_cooldown()

    assert sched._quota_cooldown_until == later


async def test_run_forever_recovers_before_its_first_tick(store):
    """The startup path itself, not just the method: nothing is dispatched
    between process start and the first tick."""
    _RecordingOrch.started.clear()
    now = datetime.now(timezone.utc)
    await _quota_park(store, now + timedelta(minutes=40))
    await _pending(store, 1)
    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    stop = asyncio.Event()
    runner = asyncio.create_task(sched.run_forever(stop=stop, poll_interval=0.05))
    await asyncio.sleep(0.4)
    stop.set()
    await asyncio.wait_for(runner, timeout=5)

    assert _RecordingOrch.started == []
    assert sched._quota_cooldown_until is not None


async def test_a_malformed_wake_stamp_is_skipped_not_fatal(store):
    now = datetime.now(timezone.utc)
    t = await _quota_park(store, now + timedelta(minutes=40))
    t.wake_check_at = "not-a-time"
    await store.update_task_columns(t)
    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    assert await sched.recover_quota_cooldown() is None


async def test_a_wall_on_another_profile_is_not_recovered(store, monkeypatch):
    """The operator's sanctioned way past a wall is `nh auth use <other>` +
    restart. A park stamped with the OLD profile must not arm the new
    process's pool (review of the first cut)."""
    import no_human.core.scheduler as sched_mod
    now = datetime.now(timezone.utc)
    t = await _quota_park(store, now + timedelta(minutes=40))
    t.blocker = {**t.blocker, "auth_profile": "personal2"}
    await store.update_task_columns(t)
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal3")
    sched = Scheduler(store, _RecordingOrch, max_workers=1)

    assert await sched.recover_quota_cooldown() is None
    assert sched._quota_cooldown_until is None


async def test_a_wall_on_the_same_profile_is_recovered_and_named(store, monkeypatch):
    import no_human.core.scheduler as sched_mod
    now = datetime.now(timezone.utc)
    t = await _quota_park(store, now + timedelta(minutes=40))
    t.blocker = {**t.blocker, "auth_profile": "personal2"}
    await store.update_task_columns(t)
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal2")
    events = []
    sched = Scheduler(store, _RecordingOrch, max_workers=1,
                      on_event=lambda k, txt: events.append((k, txt)))

    assert await sched.recover_quota_cooldown() is not None
    assert any(k == "quota_pause" and "personal2" in txt for k, txt in events)


async def test_a_park_with_no_profile_stamp_is_still_recovered(store, monkeypatch):
    """A legacy park (no auth_profile key) cannot be attributed; the wall is
    honoured rather than guessed away — the cheaper failure is ≤1 h idle,
    not N dead sessions."""
    import no_human.core.scheduler as sched_mod
    now = datetime.now(timezone.utc)
    await _quota_park(store, now + timedelta(minutes=40))
    monkeypatch.setattr(sched_mod, "active_auth_profile", lambda: "personal3")
    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    assert await sched.recover_quota_cooldown() is not None


def test_park_quota_stamps_the_profile_on_the_blocker():
    """The producer side: `_park_quota` writes `auth_profile` so recovery can
    attribute the wall — pinned by reading the source, since the park path
    needs a live QuotaExhausted to drive."""
    import inspect
    from no_human.core.orchestrator import Orchestrator
    src = inspect.getsource(Orchestrator._park_quota)
    assert '"auth_profile": profile' in src


# nh67: `recover_quota_cooldown` is the third writer of `_quota_cooldown_until`
# (alongside `tick()`'s fleet-infra breaker trip and `_run()`'s per-task quota
# park) and, pre-fix, the only one that left `_infra_cooldown_active` as it
# found it. A fresh `Scheduler` always starts with the flag `False`, so the
# bad interleaving never fires in production as the code stands today — but
# nothing enforced it, so a caller (or a future refactor) that constructs a
# `Scheduler`, runs a tick that trips the fleet breaker, and only THEN calls
# `recover_quota_cooldown` (e.g. a reload/resync path) would arm a real quota
# wall's clock while leaving the INFRA label on it — reporting the wall to
# the operator as `paused_reason: "infra"`, with no profile, instead of the
# quota wall it actually is.

async def test_recovery_relabels_a_breaker_armed_pool_as_the_quota_wall_it_restores(
        store, monkeypatch):
    """Pre-fix this FAILS: the flag survives from the simulated breaker trip,
    so `infra_cooldown_until` stays armed and `queue_health` reports
    `paused_reason == "infra"` / `paused_profile is None` for what is, by the
    DB's own record, a quota wall."""
    monkeypatch.setattr(scheduler_mod, "active_auth_profile", lambda: "personal2")
    now = datetime.now(timezone.utc)
    await _pending(store, 1)
    wall = now + timedelta(hours=3)
    t = await _quota_park(store, wall)
    t.blocker = {**t.blocker, "auth_profile": "personal2"}
    await store.update_task_columns(t)
    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    sched._infra_cooldown_active = True  # the breaker armed first

    await sched.recover_quota_cooldown()

    assert sched.quota_cooldown_until == wall
    assert sched.infra_cooldown_until is None

    h = await queue_health(store, max_workers=1,
                            quota_cooldown_until=sched.quota_cooldown_until,
                            infra_cooldown_until=sched.infra_cooldown_until)
    assert h.paused is True
    assert h.paused_reason == "quota"
    assert h.paused_profile == "personal2"
    assert h.paused_until == wall.isoformat()


async def test_recovery_that_arms_nothing_leaves_a_live_infra_label_alone(store):
    """Pins that the flag write sits on the branch that actually arms the
    clock, not at the function's entry: a remembered park whose wall has
    already passed arms nothing (existing behaviour, `test_a_passed_wall_arms_nothing`)
    and must not clear a live breaker's label either — the mutation that
    would otherwise pass the test above (moving the flag write to the top of
    `recover_quota_cooldown`) breaks this one."""
    now = datetime.now(timezone.utc)
    await _quota_park(store, now - timedelta(minutes=5))
    sched = Scheduler(store, _RecordingOrch, max_workers=1)
    sched._infra_cooldown_active = True

    recovered = await sched.recover_quota_cooldown()

    assert recovered is None
    assert sched._infra_cooldown_active is True


def test_every_quota_cooldown_writer_also_writes_the_infra_label():
    """Registry test, not an enumeration: a hardcoded list of "the three known
    sites" would not catch a FOURTH writer added later without the flag —
    which is exactly the shape of this defect (repo memory:
    `enumerations-in-a-clear-list-reproduce-the-bug`). Instead this walks
    every statement LIST in `scheduler.py` (a function body, an `if`/`for`/
    `while`/`try` block's body/orelse/finalbody, an `except` handler's body)
    and requires that any list containing an assignment to
    `self._quota_cooldown_until` also contains, as a SIBLING statement in
    that same list, an assignment to `self._infra_cooldown_active`. Sibling-
    scope (not function-scope) is the right granularity: it would still catch
    a new writer added inside a fresh `if` branch of a function that already
    sets the flag elsewhere in its body.

    Self-check: asserts at least 4 clock-assignment sites are found (`__init__`,
    `tick()`'s breaker trip, `_run()`'s per-task park, and
    `recover_quota_cooldown`'s recovery) — a scan that silently matched
    nothing would be a green no-op (repo memory: `gates-must-fail-closed`).
    """
    _CLOCK_ATTR = "_quota_cooldown_until"
    _FLAG_ATTR = "_infra_cooldown_active"

    def _self_attrs_assigned(stmt) -> list[str]:
        """Every `self.<attr>` name this statement assigns to (Assign,
        possibly chained, or AnnAssign)."""
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        else:
            return []
        attrs = []
        for t in targets:
            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == "self"):
                attrs.append(t.attr)
        return attrs

    def _iter_stmt_lists(node):
        """Yield every statement list reachable from `node`: `body`/`orelse`/
        `finalbody` on any node that has them, recursing into each statement
        found (and into `try`'s `handlers`) — so a nested block is its OWN
        list, never merged with its parent's."""
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(node, field, None)
            if isinstance(lst, list):
                yield lst
                for stmt in lst:
                    yield from _iter_stmt_lists(stmt)
        for handler in getattr(node, "handlers", None) or ():
            yield from _iter_stmt_lists(handler)

    src_path = Path(scheduler_mod.__file__)
    tree = ast.parse(src_path.read_text())

    offenders: list[tuple[int, str]] = []
    clock_sites_found = 0
    for stmts in _iter_stmt_lists(tree):
        clock_lines = [s.lineno for s in stmts if _CLOCK_ATTR in _self_attrs_assigned(s)]
        if not clock_lines:
            continue
        clock_sites_found += len(clock_lines)
        has_flag_sibling = any(_FLAG_ATTR in _self_attrs_assigned(s) for s in stmts)
        if not has_flag_sibling:
            offenders.extend((line, f"{_CLOCK_ATTR} assigned without a sibling "
                                     f"{_FLAG_ATTR} assignment") for line in clock_lines)

    assert clock_sites_found >= 4, (
        "expected to find at least the 4 known clock-assignment sites "
        f"(__init__, tick(), _run(), recover_quota_cooldown()); found "
        f"{clock_sites_found} — the scan itself may be vacuous")
    assert offenders == [], (
        f"{_CLOCK_ATTR} assigned without also assigning {_FLAG_ATTR} in the "
        f"same statement list: {offenders}")
