"""A durable human HOLD (`blocker.human_stopped`, taxonomy.py's module
docstring) must survive every machine write that replaces `task.blocker`
wholesale, and no watcher resume path may act on a held task.

MEASURED INCIDENT: task `6bc7e833` was held via `POST /pause`
(`blocker.human_stopped = True`, set in place). A quota re-pause later ran
`Orchestrator._park_quota`, which writes a brand-new QUOTA blocker dict —
`human_stopped` was not among its keys, so the hold silently vanished. The
PR-conflict watcher then found an ordinary (unheld-looking) AWAITING_APPROVAL
task with a CONFLICTING PR and resumed it within the hour — exactly the
behavior a human's hold exists to prevent.

Two defects in the same family:
  1. Every blocker-REPLACEMENT site (`_park_quota`'s fresh QUOTA dict,
     `_raise_blocker`'s `blocker.to_dict()`) drops `human_stopped` because
     neither shape carries it. Fixed by `blockers.taxonomy.carry_human_hold`,
     applied at both sites.
  2. Watcher resume paths did not check `human_stopped` before resuming: the
     quota wake path (`WakeWatcher._resume`/`resume_now`) had no such guard
     at all, and the pr_conflict path's door (`_evaluate`'s
     AWAITING_APPROVAL branch, routing to `_check_open_pr`) ran BEFORE the
     existing `human_stopped` check. Fixed by adding the guard to `_resume`
     (the single resume chokepoint — new additive action
     `"skipped_human_stopped"`) and hoisting the existing `_evaluate` check
     above the AWAITING_APPROVAL branch.

Tests 1-2 pin defect 1 (the hold survives the two replacement writes via the
REAL production code, not a hand-built fixture). Tests 3-4 pin defect 2 (the
two resume paths refuse a held task). Tests 5-6 are the mirror/negative
controls: an otherwise-identical UNHELD task must resume exactly as before —
this change must add a refusal, never remove a resume.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from no_human.blockers import Blocker
from no_human.blockers.taxonomy import BlockerCategory
from no_human.config import load_config
from no_human.core.bounds import QuotaExhausted
from no_human.core.orchestrator import Orchestrator
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.blockers.wake import WakeWatcher
from no_human.vcs import derived_conflict as dc

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _resolvable_conflicting_paths(monkeypatch):
    """The pr_conflict tests below drive `_check_open_pr`/`_check_pr_conflict`
    against a fake, non-existent `repo_path` ("/tmp/x") — same stub
    `tests/test_wake_conflict.py` uses for the same reason (that file's
    `_resolvable_conflicting_paths` docstring has the full rationale): a real
    `/tmp/x` cannot be enumerated, so without this stub `conflicting_paths()`
    raises and the rung escalates on an enumeration failure instead of
    reaching the resume/hold-check logic this file exists to pin."""
    async def fake_conflicting_paths(repo_path, base_tip, branch):
        return {"src/unrelated.py"}
    monkeypatch.setattr(dc, "conflicting_paths", fake_conflicting_paths)


# --------------------------------------------------------------------------- #
# Fixtures / helpers (self-contained — mirrors patterns already used in
# tests/test_blockers.py, tests/test_abandoned_draft_closed.py and
# tests/test_wake_conflict.py, not imported from them so this file has no
# cross-test-file coupling).
# --------------------------------------------------------------------------- #


class _Notifier:
    def notify(self, *a, **k):
        pass


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _light_orch(store):
    """`Orchestrator.__new__` bypasses `__init__` for a direct `_park_quota`
    call — the pattern `test_the_REAL_quota_park_produces_a_task_the_watcher_
    can_resume` (tests/test_blockers.py) already uses for the same method."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store
    orch.notifier = _Notifier()
    orch.emit = lambda *a, **k: None
    return orch


def _full_orch(store, tmp_path, events=None):
    """Full construction (needed for `_raise_blocker`, which touches more of
    the orchestrator than the light `__new__` stub covers) — the pattern
    `tests/test_abandoned_draft_closed.py`'s `_orch` already uses."""
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(
        store, cfg.data, _Backend(), SlackNotifier(None),
        event_sink=(events.append if events is not None else None))


def _blocker(**kw):
    kw.setdefault("goal", "Add the thing")
    kw.setdefault("confidence", 0.9)
    return Blocker(**kw)


def _cfg(**over):
    base = {"blockers": {"max_park_duration": "48h"}}
    base["blockers"].update(over)
    return base


async def _approval_task(store, *, held: bool, url="https://code.example.com/dev/x/pull/26"):
    """An AWAITING_APPROVAL task with an open PR watch — same shape as
    `tests/test_wake_conflict.py`'s `_approval_task`, plus an optional hold."""
    t = Task.new("conflict", repo_path="/tmp/x")
    t.context = {"pr_watch": url, "pr_branch": "scratch/x", "base_branch": "main"}
    blocker = {"category": "NOVEL_UNKNOWN", "raised_at": datetime.now(timezone.utc).isoformat()}
    if held:
        blocker["human_stopped"] = True
    t.blocker = blocker
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, mergeable=None, merge_state="", events=None):
    """Same shape as `tests/test_wake_conflict.py`'s `_watcher`, trimmed to
    only what these tests need (a single fixed mergeable state, no sequence)."""
    async def pr_mergeable(url):
        return {"mergeable": mergeable or "", "mergeStateStatus": merge_state}
    return WakeWatcher(
        store, {},
        pr_mergeable=pr_mergeable,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


async def _quota_park_row(store, wake_at: datetime, *, held: bool,
                           raised_at: datetime | None = None):
    """A PAUSED_QUOTA task shaped like the real `_park_quota` output (same
    fixture shape as `tests/test_scheduler_quota_park_resume.py`'s
    `_quota_park`), optionally carrying a hold."""
    t = Task.new("parked on the wall", repo_path="/tmp/x")
    await store.create_task(t)
    blocker = {
        "category": "QUOTA",
        "wake_condition": "quota_refreshed",
        "raised_at": (raised_at or wake_at - timedelta(hours=1)).isoformat(),
        "root_cause_hypothesis": "You've hit your session limit",
    }
    if held:
        blocker["human_stopped"] = True
    t.blocker = blocker
    t.wake_check_at = wake_at.isoformat()
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.PAUSED_QUOTA, validate=False)
    return t


# --------------------------------------------------------------------------- #
# Defect 1: `human_stopped` must survive a blocker REPLACEMENT.
# --------------------------------------------------------------------------- #


async def test_hold_survives_a_quota_repause_via_the_production_park_path(store):
    """The measured incident: a task already PAUSED_QUOTA with a human hold
    stamped on its blocker (`POST /pause`, in place — real production shape)
    hits the wall again. `_park_quota` writes a brand-new QUOTA dict; without
    `carry_human_hold` the hold vanishes here, which is exactly what let the
    pr_conflict watcher resume 6bc7e833 within the hour.

    Drives the REAL `Orchestrator._park_quota`, not a hand-built blocker —
    same discipline as `test_the_REAL_quota_park_produces_a_task_the_watcher_
    can_resume` (tests/test_blockers.py), whose docstring notes a fixture-only
    test asserts a shape the real path never writes.
    """
    orch = _light_orch(store)

    task = Task.new("t", repo_path="/tmp/x")
    task.blocker = {
        "category": "QUOTA",
        "wake_condition": "quota_refreshed",
        "raised_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "root_cause_hypothesis": "You've hit your weekly limit",
        "human_stopped": True,
        "hold_reason": "investigating a bad merge upstream",
    }
    await store.create_task(task)
    # PAUSED_QUOTA -> PAUSED_QUOTA is a same-state no-op transition
    # (`can_transition`'s `src == dst` short-circuit) — exactly the re-pause
    # shape: the task was already parked (and held) when it hit the wall
    # again.
    await store.set_status(task, TaskStatus.PAUSED_QUOTA, validate=False)

    outcome = await orch._park_quota(task, QuotaExhausted("You've hit your weekly limit"))
    assert outcome.status == TaskStatus.PAUSED_QUOTA

    parked = await store.get_task(task.id)
    assert parked.blocker["category"] == "QUOTA"
    assert parked.blocker["wake_condition"] == "quota_refreshed"
    assert "weekly limit" in parked.blocker["root_cause_hypothesis"]
    assert parked.blocker.get("human_stopped") is True, (
        "the fresh QUOTA dict dropped the hold — the exact SCRUM-22 "
        "regression this file pins")
    assert parked.blocker.get("hold_reason") == "investigating a bad merge upstream"


async def test_hold_survives_the_off_ramp_blocker_write(store, tmp_path):
    """`_raise_blocker` is the single funnel for every off-ramp (BLOCKED,
    AWAITING_INPUT, ESCALATED, FAILED) and always writes
    `task.blocker = blocker.to_dict()` — a dataclass with no `human_stopped`
    field. A task already held (e.g. parked+held, then re-raised into a NEW
    blocker category by a later attempt) must keep the hold across that
    write too."""
    orch = _full_orch(store, tmp_path)
    t = Task.new("Add the thing", repo_path="/r")
    await store.create_task(t)
    # In-memory only, exactly as `_raise_blocker` reads it (`task.blocker`
    # off the live object, not a re-fetch) — same shape
    # `tests/test_abandoned_draft_closed.py`'s tests use.
    t.blocker = {
        "category": "TRANSIENT_INFRA",
        "human_stopped": True,
        "hold_actor": "operator",
    }

    blocker = _blocker(category=BlockerCategory.NOVEL_UNKNOWN,
                       root_cause_hypothesis="max_attempts (3) reached")
    out = await orch._raise_blocker(t, blocker)
    assert out.status == TaskStatus.ESCALATED

    fresh = await store.get_task(t.id)
    assert fresh.blocker["category"] == "NOVEL_UNKNOWN"
    assert fresh.blocker.get("human_stopped") is True, (
        "Blocker.to_dict() has no human_stopped field — the replacement "
        "silently dropped the hold")
    assert fresh.blocker.get("hold_actor") == "operator"


# --------------------------------------------------------------------------- #
# Defect 2: no watcher resume path may resume a HELD task.
# --------------------------------------------------------------------------- #


async def test_quota_wake_does_not_resume_a_held_task(store):
    """The quota wake path (`resume_now` -> `_resume`) had NO human_stopped
    guard at all before this fix — every quota-parked task past its wall
    resumed unconditionally, hold or not."""
    now = datetime.now(timezone.utc)
    task = await _quota_park_row(store, now - timedelta(minutes=1), held=True)

    wake = WakeWatcher(store, _cfg())
    action = await wake.resume_now(task, now=now)
    assert action == "skipped_human_stopped", action

    fresh = await store.get_task(task.id)
    assert fresh.status is TaskStatus.PAUSED_QUOTA, (
        "a held quota park must stay parked")

    # `Scheduler._resume_quota_parks` funnels through the same `resume_now`
    # call and only records an id when the action is literally "resumed" —
    # confirm the sweep itself surfaces nothing for a held row.
    sched = Scheduler(store, lambda t: None, wake_watcher=wake)
    resumed = await sched._resume_quota_parks(now=now)
    assert resumed == [], resumed


async def test_pr_conflict_wake_does_not_resume_a_held_task(store):
    """The pr_conflict rung is reached through `_evaluate`'s
    AWAITING_APPROVAL branch, which used to run BEFORE the human_stopped
    check — a held task reaching AWAITING_APPROVAL with an open, conflicting
    PR was shepherded (forge polled, `pr_conflict_rounds` bumped, resumed)
    exactly like an unheld one."""
    t = await _approval_task(store, held=True)
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY", events=events)

    actions = await w.tick(now=datetime.now(timezone.utc))
    assert not any(action == "resumed" for _tid, action in actions), actions

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL, (
        "a held approval-gate task must not be sent back for revision")
    assert "pr_conflict_rounds" not in (fresh.context or {}), (
        "the pr_conflict rung must never have run for a held task")
    assert not any(k == "pr_conflict" for k, _t in events)


# --------------------------------------------------------------------------- #
# Mirror controls: an UNHELD twin must resume exactly as before this change.
# --------------------------------------------------------------------------- #


async def test_an_unheld_twin_still_resumes_on_the_quota_wake(store):
    now = datetime.now(timezone.utc)
    task = await _quota_park_row(store, now - timedelta(minutes=1), held=False)

    wake = WakeWatcher(store, _cfg())
    action = await wake.resume_now(task, now=now)
    assert action == "resumed", action

    fresh = await store.get_task(task.id)
    assert fresh.status is not TaskStatus.PAUSED_QUOTA

    task2 = await _quota_park_row(store, now - timedelta(minutes=1), held=False)
    sched = Scheduler(store, lambda t: None, wake_watcher=wake)
    resumed = await sched._resume_quota_parks(now=now)
    assert task2.id in resumed, resumed


async def test_an_unheld_twin_still_resumes_via_the_pr_conflict_path(store):
    t = await _approval_task(store, held=False)
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY", events=events)

    actions = await w.tick(now=datetime.now(timezone.utc))
    assert (t.id, "resumed") in actions, actions

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context.get("pr_conflict_rounds") == 1
