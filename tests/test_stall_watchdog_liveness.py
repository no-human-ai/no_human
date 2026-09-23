"""The stall sweep must judge liveness by UNION of two independent signals —
a live worker process for the task, and a recent task event — escalating
only when BOTH say dead.

PR #546 (`no-human/f8cae5c4-3`, head `25a6cebe`) fixed the *ordering* between
the task-level and attempt-level watchdogs (`effective_stuck_active_minutes`)
but still decides on event age ALONE. That is not enough: a review/test
phase emits no task events for as long as review takes. Measured live,
2026-09-19 09:53:11: task `f8cae5c4` held five live worker processes, had
opened its draft PR at 08:23:58 and passed its repro gate at 07:01:53, yet
sat silent for 89 minutes and was escalated anyway — 89 >= the 61-minute
effective threshold computed from stock config. A bigger number cannot fix
this: review-phase silence is unbounded. `test_a_live_worker_defeats_ninety_
minutes_of_review_silence` below reproduces exactly that shape and must FAIL
against #546's head, where no liveness check exists at all.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

from no_human.blockers.stall_watchdog import (
    effective_stuck_active_minutes,
    worker_liveness,
)
from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus


def _watcher(store, config, *, events=None):
    return WakeWatcher(
        store, config,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


async def _active_task(store, *, last_event_age_min, status=TaskStatus.IMPLEMENTING,
                        event_kind="attempt_start"):
    t = Task.new("wedged", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, status, validate=False)
    if last_event_age_min is not None:
        await store.save_events(t.id, [{
            "source": "orchestrator", "kind": event_kind, "text": "",
            "ts": time.time() - last_event_age_min * 60,
        }])
    return t


def _plant_worktree(tmp_path, task_id, *, suffix="src") -> "os.PathLike":
    """A real, on-disk worktree directory in the `<task_id>.<pid>.<token>`
    shape `worktree_owner` parses, under a real `isolation.worktree_root`.
    """
    root = tmp_path / "wt"
    root.mkdir(exist_ok=True)
    wt_dir = root / f"{task_id}.{os.getpid()}.deadbeef"
    (wt_dir / suffix if suffix else wt_dir).mkdir(parents=True, exist_ok=True)
    return root, wt_dir


# --------------------------------------------------------------------- AC2 -- #

async def test_a_live_worker_defeats_ninety_minutes_of_review_silence(store, tmp_path, monkeypatch):
    """The exact shape that defeats the threshold fix alone: a task mid-
    review, event silence LONGER than the 61-minute effective threshold
    (90 minutes, mirroring the f8cae5c4 09:53:11 measurement), with a live
    worker process still running in its worktree. Must NOT escalate.

    This test is RED against PR #546's head (25a6cebe): that code has no
    liveness check at all (`grep -nE '(psutil|pid|process|worktree|alive|
    liveness|subprocess)' src/no_human/blockers/stall_watchdog.py` there
    returns nothing), so `_escalate_if_stalled` there only ever looks at
    event age and escalates at 90 >= 61.
    """
    config = {
        "bounds": {"attempt_timeout_s": 3600},
        "blockers": {"stuck_active_minutes": 40},
        "isolation": {"worktree_root": str(tmp_path / "wt")},
    }
    assert effective_stuck_active_minutes(config) == 61.0  # pin the measured shape

    t = await _active_task(store, last_event_age_min=90, status=TaskStatus.REVIEWING)
    root, wt_dir = _plant_worktree(tmp_path, t.id, suffix=None)
    monkeypatch.setattr("no_human.blockers.stall_watchdog._process_cwds",
                         lambda: [str(wt_dir)])

    events = []
    w = _watcher(store, config, events=events)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))

    assert escalated is False
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.REVIEWING
    assert not any(k == "escalated_stalled" for k, _ in events)
    assert (fresh.blocker or {}).get("category") != "NOVEL_UNKNOWN"


# --------------------------------------------------------------------- AC1 -- #

async def test_a_recent_event_survives_zero_processes(store, tmp_path, monkeypatch):
    """The other direction of the union: a recent event alone is enough,
    even with zero worker processes present — a task between tool calls, or
    awaiting a model response, legitimately owns no worktree subprocess.
    Encodes the measured 2026-09-19 05:47 case (two live tasks, zero
    worktree processes, emitting events that same second)."""
    config = {
        "bounds": {"attempt_timeout_s": 3600},
        "blockers": {"stuck_active_minutes": 40},
        "isolation": {"worktree_root": str(tmp_path / "wt")},
    }
    t = await _active_task(store, last_event_age_min=5)
    monkeypatch.setattr("no_human.blockers.stall_watchdog._process_cwds", lambda: [])

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))

    assert escalated is False
    assert (await store.get_task(t.id)).status is TaskStatus.IMPLEMENTING


async def test_both_signals_dead_still_escalates(store, tmp_path, monkeypatch):
    """Anti-vacuity: when NEITHER signal says alive, the sweep must still
    escalate. Without this, a fix that simply never escalates would pass
    the other tests too."""
    config = {
        "bounds": {"attempt_timeout_s": 3600},
        "blockers": {"stuck_active_minutes": 40},
        "isolation": {"worktree_root": str(tmp_path / "wt")},
    }
    t = await _active_task(store, last_event_age_min=90)
    monkeypatch.setattr("no_human.blockers.stall_watchdog._process_cwds", lambda: [])

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))

    assert escalated is True
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert (fresh.blocker or {}).get("category") == "NOVEL_UNKNOWN"


async def test_a_subdirectory_cwd_counts_as_the_task(store, tmp_path, monkeypatch):
    """A process cwd'd into a SUBDIRECTORY of the task's worktree (e.g. a
    reviewer running from `<wt>/src`) is still that task's worker."""
    config = {
        "bounds": {"attempt_timeout_s": 3600},
        "blockers": {"stuck_active_minutes": 40},
        "isolation": {"worktree_root": str(tmp_path / "wt")},
    }
    t = await _active_task(store, last_event_age_min=90, status=TaskStatus.REVIEWING)
    root, wt_dir = _plant_worktree(tmp_path, t.id, suffix="src")
    sub_cwd = wt_dir / "src"
    monkeypatch.setattr("no_human.blockers.stall_watchdog._process_cwds",
                         lambda: [str(sub_cwd)])

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))

    assert escalated is False
    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_another_tasks_worktree_is_not_this_tasks_liveness(store, tmp_path, monkeypatch):
    """A live process cwd'd into a DIFFERENT task's worktree must not read
    as this task's liveness — otherwise the signal degenerates into "any nh
    process is alive anywhere", which is vacuous."""
    config = {
        "bounds": {"attempt_timeout_s": 3600},
        "blockers": {"stuck_active_minutes": 40},
        "isolation": {"worktree_root": str(tmp_path / "wt")},
    }
    t = await _active_task(store, last_event_age_min=90)
    other_task_id = "deadbeef-other-task-id"
    _, other_wt_dir = _plant_worktree(tmp_path, other_task_id, suffix=None)
    monkeypatch.setattr("no_human.blockers.stall_watchdog._process_cwds",
                         lambda: [str(other_wt_dir)])

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))

    assert escalated is True
    assert (await store.get_task(t.id)).status is TaskStatus.ESCALATED


# --------------------------------------------------------------------- AC4 -- #

async def test_an_unreadable_process_table_does_not_escalate(store, tmp_path, monkeypatch):
    """The process table being unreadable (permission denied, a sandboxed
    environment, ...) must fail CLOSED toward NOT escalating — a false
    escalation costs a whole attempt, a missed one costs one sweep
    interval."""
    config = {
        "bounds": {"attempt_timeout_s": 3600},
        "blockers": {"stuck_active_minutes": 40},
        "isolation": {"worktree_root": str(tmp_path / "wt")},
    }
    t = await _active_task(store, last_event_age_min=90, status=TaskStatus.REVIEWING)
    _plant_worktree(tmp_path, t.id, suffix=None)

    def _raise():
        raise PermissionError("process table unreadable")

    monkeypatch.setattr("no_human.blockers.stall_watchdog._process_cwds", _raise)
    assert worker_liveness(config, t.id) is None

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))

    assert escalated is False
    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_an_unreadable_worktree_root_does_not_escalate(store, tmp_path, monkeypatch):
    """The worktree root existing but being unreadable (a regular file
    where a directory is expected, or `iterdir()` raising) is also a
    fail-closed `None`, not a determinate `False` — it is a genuine read
    error, distinct from "no worktree root was ever created"."""
    root_as_file = tmp_path / "wt"
    root_as_file.write_text("not a directory")
    config = {
        "bounds": {"attempt_timeout_s": 3600},
        "blockers": {"stuck_active_minutes": 40},
        "isolation": {"worktree_root": str(root_as_file)},
    }
    t = await _active_task(store, last_event_age_min=90, status=TaskStatus.REVIEWING)

    assert worker_liveness(config, t.id) is None

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))

    assert escalated is False
    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_isolation_disabled_degrades_to_event_age_only(store, tmp_path):
    """No worktree root was ever created (isolation off / never used) is a
    DETERMINATE `False`, not `None` — the union must degrade to plain
    event-age behaviour, not silently disable the watchdog for every task
    forever. Pins the row-3-vs-row-4 distinction in `worker_liveness`."""
    config = {
        "bounds": {"attempt_timeout_s": 3600},
        "blockers": {"stuck_active_minutes": 40},
        "isolation": {"worktree_root": str(tmp_path / "never-created")},
    }
    t = await _active_task(store, last_event_age_min=90)

    assert worker_liveness(config, t.id) is False

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))

    assert escalated is True
    assert (await store.get_task(t.id)).status is TaskStatus.ESCALATED
