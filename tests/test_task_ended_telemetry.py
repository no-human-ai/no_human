"""`task_ended` (every non-done/non-failed task end) + `tasks_orphaned` (the
app/server-closed-mid-run case, detected on the NEXT server start).

Drives the existing sink (`Orchestrator._telemetry_hook`) with the `_Stub`
pattern from `tests/test_telemetry.py`, and `count_dead_attempt_tasks` /
`app._record_tasks_orphaned` against the real `store` fixture — no agent, no
network.
"""
from __future__ import annotations

import importlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from no_human import telemetry
from no_human.core.orchestrator import Orchestrator
from no_human.core.scheduler import count_dead_attempt_tasks
from no_human.core.task import Task, TaskStatus

# `no_human.api`'s __init__ re-exports the FastAPI instance under the name
# `app`, shadowing the submodule — `importlib.import_module` (as
# tests/test_telemetry.py does) is the only way to reach the MODULE, which is
# what carries `_record_tasks_orphaned`.
app_module = importlib.import_module("no_human.api.app")


class _Stub:  # only what the hook touches — mirrors tests/test_telemetry.py
    config: dict = {}
    _telemetry_hook = Orchestrator._telemetry_hook


def _recorder(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))
    return sent


# --- task_ended: one per non-done/non-failed end state ---------------------- #

@pytest.mark.parametrize(
    "kind,meta,outcome",
    [
        ("escalated", {}, "escalated"),
        ("paused_quota", {}, "parked_quota"),
        ("blocked", {"blocker_category": "TRANSIENT_INFRA"}, "parked_infra"),
        ("awaiting_input", {"blocker_category": "AMBIGUITY"}, "needs_answer"),
        ("cancelled", {}, "cancelled"),
    ],
)
def test_each_end_state_emits_exactly_one_task_ended(monkeypatch, kind, meta, outcome):
    sent = _recorder(monkeypatch)
    stub = _Stub()
    stub._telemetry_hook("kind", {"task_kind": "feature"})
    stub._telemetry_hook("attempt_start", {})
    stub._telemetry_hook(kind, meta)

    terminal = [(k, p) for k, p in sent if k in ("task_ended", "task_completed", "task_failed")]
    assert len(terminal) == 1
    name, props = terminal[0]
    assert name == "task_ended"
    assert props == {"outcome": outcome, "attempts": 1, "duration_bucket": "<10m"}


def test_blocked_user_paused_is_cancelled_and_unknown_category_needs_answer(monkeypatch):
    sent = _recorder(monkeypatch)
    stub1 = _Stub()
    stub1._telemetry_hook("blocked", {"blocker_category": "USER_PAUSED"})
    stub2 = _Stub()
    stub2._telemetry_hook("blocked", {"blocker_category": "SOME_NEW_CATEGORY"})
    stub3 = _Stub()
    stub3._telemetry_hook("blocked", {})

    outcomes = [p["outcome"] for k, p in sent if k == "task_ended"]
    assert outcomes == ["cancelled", "needs_answer", "needs_answer"]


def test_only_one_terminal_event_per_task(monkeypatch):
    sent = _recorder(monkeypatch)
    stub = _Stub()
    stub._telemetry_hook("escalated", {})
    stub._telemetry_hook("failed", {})
    terminal = [(k, p) for k, p in sent if k in ("task_ended", "task_completed", "task_failed")]
    assert len(terminal) == 1
    assert terminal[0][0] == "task_ended"

    sent2 = _recorder(monkeypatch)
    stub2 = _Stub()
    stub2._telemetry_hook("blocked", {"blocker_category": "TRANSIENT_INFRA"})
    stub2._telemetry_hook("state", {"status": "done"})
    terminal2 = [(k, p) for k, p in sent2 if k in ("task_ended", "task_completed", "task_failed")]
    assert len(terminal2) == 1
    assert terminal2[0][0] == "task_ended"


def test_done_and_failed_are_unchanged(monkeypatch):
    sent = _recorder(monkeypatch)
    stub1 = _Stub()
    stub1._telemetry_hook("attempt_start", {})
    stub1._telemetry_hook("state", {"status": "done"})
    stub2 = _Stub()
    stub2._telemetry_hook("attempt_start", {})
    stub2._telemetry_hook("state", {"status": "awaiting_approval"})
    stub3 = _Stub()
    stub3._telemetry_hook("attempt_start", {})
    stub3._telemetry_hook("failed", {"reason_category": "infra"})

    names = [k for k, p in sent]
    assert "task_ended" not in names

    completed_props = [p for k, p in sent if k == "task_completed"]
    assert len(completed_props) == 2
    for props in completed_props:
        assert set(props) == {"status", "duration_bucket", "attempts"}

    failed_props = [p for k, p in sent if k == "task_failed"]
    assert len(failed_props) == 1
    assert failed_props[0] == {"category": "failed", "reason_category": "infra"}


def test_attempts_and_duration_bucket_ride_along(monkeypatch):
    sent = _recorder(monkeypatch)
    stub = _Stub()
    stub._telemetry_hook("attempt_start", {})
    stub._telemetry_hook("attempt_start", {})
    stub._telemetry_hook("attempt_start", {})
    stub._telemetry_hook("escalated", {})
    name, props = next((k, p) for k, p in sent if k == "task_ended")
    assert props["attempts"] == 3

    stub2 = _Stub()
    # No "kind" event first -> `_tel_started_at` is never set.
    stub2._telemetry_hook("escalated", {})
    name2, props2 = next((k, p) for k, p in sent if k == "task_ended" and p["attempts"] == 0)
    assert props2["duration_bucket"] == "unknown"
    assert "unknown" in telemetry.DURATION_BUCKETS


# --- tasks_orphaned: dead-attempt counting on server start ------------------ #

async def _mid_run_task_with_open_attempt(store, *, status, age_s: float | None):
    t = Task.new(f"mid-run {status.value} age={age_s}", repo_path="/r")
    await store.create_task(t)
    await store.set_status(t, status, validate=False)
    await store.create_attempt(t.id, 1)
    if age_s is not None:
        old = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
        await store.db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, t.id))
        await store.db.commit()
    return t


@pytest.mark.asyncio
async def test_two_dead_attempts_are_counted(store):
    # Two dead: mid-run, open attempt, ~2h stale, no fresh events.
    await _mid_run_task_with_open_attempt(
        store, status=TaskStatus.IMPLEMENTING, age_s=7200)
    await _mid_run_task_with_open_attempt(
        store, status=TaskStatus.IMPLEMENTING, age_s=7200)
    # One alive: fresh updated_at.
    await _mid_run_task_with_open_attempt(
        store, status=TaskStatus.IMPLEMENTING, age_s=None)
    # One terminal (done) with an open attempt -- not mid-run, must not count.
    done = Task.new("already done", repo_path="/r")
    await store.create_task(done)
    await store.set_status(
        done, TaskStatus.DONE, validate=False,
        event={"kind": "done", "source": "test"})
    await store.create_attempt(done.id, 1)
    old = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, done.id))
    await store.db.commit()

    assert await count_dead_attempt_tasks(store) == 2


@pytest.mark.asyncio
async def test_startup_emits_tasks_orphaned_with_the_bucketed_count(store, monkeypatch):
    sent = _recorder(monkeypatch)
    for _ in range(3):
        await _mid_run_task_with_open_attempt(
            store, status=TaskStatus.IMPLEMENTING, age_s=7200)

    class _Cfg:
        data = {"telemetry": {"enabled": True}}

    await app_module._record_tasks_orphaned(store, _Cfg())

    orphan_events = [(k, p) for k, p in sent if k == "tasks_orphaned"]
    assert len(orphan_events) == 1
    assert orphan_events[0][1] == {"count_bucket": "2-5"}


@pytest.mark.asyncio
async def test_tasks_orphaned_is_emitted_even_with_zero_orphans(store, monkeypatch):
    sent = _recorder(monkeypatch)

    class _Cfg:
        data = {"telemetry": {"enabled": True}}

    await app_module._record_tasks_orphaned(store, _Cfg())

    orphan_events = [(k, p) for k, p in sent if k == "tasks_orphaned"]
    assert len(orphan_events) == 1
    assert orphan_events[0][1] == {"count_bucket": "0"}


@pytest.mark.parametrize(
    "n,bucket", [(0, "0"), (1, "1"), (2, "2-5"), (5, "2-5"), (6, "6+"), (99, "6+")]
)
def test_orphan_bucket_edges(n, bucket):
    assert telemetry.orphan_bucket(n) == bucket
