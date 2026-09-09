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
        ("cancelled_hard", {}, "cancelled"),
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


def test_cooperative_pause_kind_is_not_terminal(monkeypatch):
    # `_honor_cancel`'s cooperative PAUSE emits kind="cancelled" (status set to
    # BLOCKED, resumable via `nh task resume`) -- distinct from the hard-cancel
    # kind="cancelled_hard" emitted by `_run_attempt`'s `CancelledError`
    # branch. It must never reach `task_ended`: it isn't an end state.
    sent = _recorder(monkeypatch)
    stub = _Stub()
    stub._telemetry_hook("kind", {"task_kind": "feature"})
    stub._telemetry_hook("attempt_start", {})
    stub._telemetry_hook("cancelled", {"status": "blocked"})

    terminal = [(k, p) for k, p in sent if k in ("task_ended", "task_completed", "task_failed")]
    assert terminal == []


def test_blocked_unknown_or_missing_category_is_needs_answer(monkeypatch):
    # `USER_PAUSED` is deliberately absent: it is harness-only and its only
    # writer is the pause path, which emits kind "cancelled" (not in
    # `_TASK_END_KINDS`), so it can never reach this mapper.
    sent = _recorder(monkeypatch)
    stub2 = _Stub()
    stub2._telemetry_hook("blocked", {"blocker_category": "SOME_NEW_CATEGORY"})
    stub3 = _Stub()
    stub3._telemetry_hook("blocked", {})

    outcomes = [p["outcome"] for k, p in sent if k == "task_ended"]
    assert outcomes == ["needs_answer", "needs_answer"]


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


def test_pr_open_delivery_path_emits_task_completed(monkeypatch):
    # MAJOR-3: `_open_pr`'s ordinary successful-delivery leg never emits
    # kind="state" for AWAITING_APPROVAL -- only kind="pr_open" carrying
    # status="awaiting_approval" alongside it (see `_open_pr`,
    # orchestrator.py). Before the fix, this path never produced
    # `task_completed` at all: only the orchestrator's own
    # kind="state"/status="done" emits did (`nh approve` writes DONE
    # through the store, with no Orchestrator, so it never reaches the
    # sink).
    sent = _recorder(monkeypatch)
    stub = _Stub()
    stub._telemetry_hook("kind", {"task_kind": "feature"})
    stub._telemetry_hook("attempt_start", {})
    stub._telemetry_hook("pr_open", {"pr_kind": "github", "status": "awaiting_approval"})

    terminal = [(k, p) for k, p in sent if k in ("task_ended", "task_completed", "task_failed")]
    assert len(terminal) == 1
    name, props = terminal[0]
    assert name == "task_completed"
    assert props == {"status": "awaiting_approval", "attempts": 1, "duration_bucket": "<10m"}


def test_pr_open_then_state_done_does_not_double_fire(monkeypatch):
    # The `pr_open` delivery event fires `task_completed` once; a LATER
    # kind="state"/status="done" emit on the same live orchestrator
    # instance must not fire a second one -- the once-only
    # `_tel_terminal_sent` latch already guards this for every other pair,
    # this just pins it for the new pr_open trigger specifically.
    sent = _recorder(monkeypatch)
    stub = _Stub()
    stub._telemetry_hook("kind", {"task_kind": "feature"})
    stub._telemetry_hook("attempt_start", {})
    stub._telemetry_hook("pr_open", {"pr_kind": "github", "status": "awaiting_approval"})
    stub._telemetry_hook("state", {"status": "done"})

    terminal = [(k, p) for k, p in sent if k in ("task_ended", "task_completed", "task_failed")]
    assert len(terminal) == 1
    assert terminal[0][0] == "task_completed"
    assert terminal[0][1]["status"] == "awaiting_approval"


def test_pr_open_without_awaiting_approval_status_is_not_terminal(monkeypatch):
    # A linked-repo PR (`self.emit("pr_open", ..., pr_kind=lr_pr.kind)`, no
    # `status` kwarg at all) must not be mistaken for the delivery event.
    sent = _recorder(monkeypatch)
    stub = _Stub()
    stub._telemetry_hook("kind", {"task_kind": "feature"})
    stub._telemetry_hook("attempt_start", {})
    stub._telemetry_hook("pr_open", {"pr_kind": "github"})

    terminal = [(k, p) for k, p in sent if k in ("task_ended", "task_completed", "task_failed")]
    assert terminal == []


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
async def test_interrupted_attempt_with_no_open_attempt_counts_as_dead(store):
    # `_honor_server_stop` (graceful `nh stop` / clean app quit) closes the
    # open attempt as status='interrupted' and leaves the task non-terminal
    # with NO open attempt at all -- there is no live attempt to go stale, so
    # the ordinary staleness gate never fires and the task was previously
    # under-counted ("dead=1 of 3" in the reviewer's repro). The interrupted
    # status itself is definitive: count it unconditionally, regardless of
    # how fresh task.updated_at looks (e.g. right after a restart).
    gracefully_stopped = Task.new("mid-run, gracefully interrupted", repo_path="/r")
    await store.create_task(gracefully_stopped)
    await store.set_status(gracefully_stopped, TaskStatus.IMPLEMENTING, validate=False)
    await store.create_attempt(gracefully_stopped.id, 1)
    await store.update_attempt(
        (await store.latest_open_attempt(gracefully_stopped.id))["id"],
        status="interrupted", failure_reason="server stop")
    # Fresh updated_at -- must still count, since there is no open attempt to
    # judge liveness from at all.
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), gracefully_stopped.id))
    await store.db.commit()

    # Control: an ordinary retry -- last attempt closed 'failed', not
    # 'interrupted', with a fresh second open attempt in progress. Must not
    # be double counted or miscounted as dead.
    ordinary_retry = Task.new("mid-run, between two normal attempts", repo_path="/r")
    await store.create_task(ordinary_retry)
    await store.set_status(ordinary_retry, TaskStatus.IMPLEMENTING, validate=False)
    await store.create_attempt(ordinary_retry.id, 1)
    await store.update_attempt(
        (await store.latest_open_attempt(ordinary_retry.id))["id"],
        status="failed", failure_reason="transient")
    await store.create_attempt(ordinary_retry.id, 2)

    assert await count_dead_attempt_tasks(store) == 1


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
