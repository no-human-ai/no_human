"""Stall-watchdog-ordering fix: the task-level stuck-active watchdog
(`WakeWatcher._escalate_if_stalled`, default 40 min) and the attempt-level
inactivity bound (`Orchestrator._await_coder_turn`, `bounds.attempt_timeout_s`,
default 3600s = 60 min) guarded the same "hung backend" condition with no
relationship between their thresholds. Because 40 < 60, the coarse task-level
watchdog always fired 20 minutes before the attempt-level one that actually
detects a hung backend — escalating tasks whose backend was still legitimately
running, and orphaning the open attempt row (its turns/usage were never
attributed anywhere; measured live: 86,108,824 tokens over 243 call(s)
recorded to tasks but not in their attempt rows).

`effective_stuck_active_minutes` now floors the task-level threshold at
`ceil(attempt_timeout_s / 60) + 1` minutes — one whole minute above the
attempt bound, so the task-level sweep can never win the race — and
`_escalate_if_stalled` closes the orphaned attempt row (`interrupted`, usage
columns made non-NULL) instead of leaving it `in_progress` forever.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from no_human.blockers.wake import WakeWatcher, effective_stuck_active_minutes
from no_human.core.attempt_completion import TERMINAL_ATTEMPT_STATUSES
from no_human.core.scheduler import Scheduler
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


# --------------------------------------------------------------------- AC1 -- #

@pytest.mark.parametrize(
    "attempt_timeout_s, raw_stuck_active_minutes, expected_floor",
    [
        (3600, 10, 61),
        (7200, 40, 121),
    ],
)
async def test_inverted_config_still_puts_the_task_bound_above_the_attempt_bound(
    store, attempt_timeout_s, raw_stuck_active_minutes, expected_floor,
):
    """A config with the task-level threshold set SHORTER than the
    attempt-level bound (the exact inversion that caused the live incident)
    must not be honoured literally — the effective threshold stays above the
    attempt bound. A build that just reads the configured number back would
    compute `raw_stuck_active_minutes` here and escalate a 45-min-stale task;
    that must fail this test.
    """
    config = {
        "bounds": {"attempt_timeout_s": attempt_timeout_s},
        "blockers": {"stuck_active_minutes": raw_stuck_active_minutes},
    }
    assert effective_stuck_active_minutes(config) == expected_floor
    assert expected_floor > attempt_timeout_s / 60.0

    t = await _active_task(store, last_event_age_min=45)
    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert escalated is False, (
        "a build that reads the raw (inverted, shorter) configured minutes "
        "would escalate this 45-min-stale task; the effective threshold must "
        "not")
    assert (await store.get_task(t.id)).status is TaskStatus.IMPLEMENTING


async def test_zero_still_disables_the_watchdog(store):
    """`stuck_active_minutes: 0` means "disabled" — the floor must not
    resurrect a watchdog an operator explicitly turned off, however small or
    large `attempt_timeout_s` is."""
    config = {
        "bounds": {"attempt_timeout_s": 120},
        "blockers": {"stuck_active_minutes": 0},
    }
    assert effective_stuck_active_minutes(config) == 0.0

    t = await _active_task(store, last_event_age_min=600)  # 10h stale
    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert escalated is False


# --------------------------------------------------------------------- AC2 -- #

async def test_a_long_compaction_is_not_a_stall(store):
    """A task whose only silence is a long compaction (50 min — older than
    the raw 40-min config, younger than the 61-min effective floor) is not
    escalated. No special-casing of `kind="compaction"` is needed or added —
    this falls out of raising the effective threshold above the attempt
    bound, per the task's own instruction: fix the ordering, not compaction
    specifically."""
    config = {
        "bounds": {"attempt_timeout_s": 3600},
        "blockers": {"stuck_active_minutes": 40},
    }
    t = await _active_task(store, last_event_age_min=50, event_kind="compaction")
    events = []
    w = _watcher(store, config, events=events)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert escalated is False
    assert (await store.get_task(t.id)).status is TaskStatus.IMPLEMENTING
    assert not any(k == "escalated_stalled" for k, _ in events)


# --------------------------------------------------------------------- AC3 -- #

async def test_escalation_closes_the_open_attempt_row_with_usage(store):
    """When the sweep escalates, the open attempt row is closed terminal
    with non-null usage — never left orphaned `in_progress` forever."""
    config = {
        "bounds": {"attempt_timeout_s": 60},
        "blockers": {"stuck_active_minutes": 1},
    }
    t = await _active_task(store, last_event_age_min=90)
    attempt_id = await store.create_attempt(t.id, 1)
    # Usage already billed onto the row before it stalled must survive
    # untouched — only a still-NULL column becomes an honest zero.
    await store.update_attempt(attempt_id, tokens_used=123, commit_sha="deadbeef" * 5)

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert escalated is True
    assert (await store.get_task(t.id)).status is TaskStatus.ESCALATED

    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 1
    row = attempts[0]
    assert row["status"] in TERMINAL_ATTEMPT_STATUSES
    assert row["completed_at"] is not None
    assert row["turns_used"] is not None
    assert row["tokens_used"] == 123          # never clobbered
    assert row["output_tokens"] is not None
    assert row["cache_read_tokens"] is not None
    assert row["cache_creation_tokens"] is not None
    assert "stall" in (row["failure_reason"] or "").lower()
    assert await store.latest_open_attempt(t.id) is None


# ------------------------------------------------------- checkpoint safety -- #

async def test_escalation_preserves_the_resume_checkpoint(store):
    """The open attempt's commit is stamped onto `context.resume_from`
    before the row is closed — otherwise the next `nh task resume` would
    branch from base and throw away everything the stalled attempt had
    already committed."""
    config = {
        "bounds": {"attempt_timeout_s": 60},
        "blockers": {"stuck_active_minutes": 1},
    }
    t = await _active_task(store, last_event_age_min=90)
    sha = "c0ffee" * 6 + "12"
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt_id, commit_sha=sha)

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert escalated is True
    fresh = await store.get_task(t.id)
    assert (fresh.context or {}).get("resume_from", {}).get("sha") == sha


async def test_an_armed_human_checkpoint_is_never_overwritten(store):
    """A human's own unconsumed `resume_from` (`human_gate_armed`) must not
    be overwritten by this machine bookkeeping stamp — same predicate the
    scheduler itself uses, so the two cannot drift."""
    config = {
        "bounds": {"attempt_timeout_s": 60},
        "blockers": {"stuck_active_minutes": 1},
    }
    t = await _active_task(store, last_event_age_min=90)
    human_sha = "aaaa" * 10
    await store.merge_context(t.id, {
        "resume_from": {"sha": human_sha, "branch": None, "by": "human"},
    })
    t = await store.get_task(t.id)

    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt_id, commit_sha="bbbb" * 10)

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert escalated is True
    fresh = await store.get_task(t.id)
    assert (fresh.context or {}).get("resume_from", {}).get("sha") == human_sha
    assert (fresh.context or {}).get("resume_from", {}).get("by") == "human"


async def test_escalation_is_idempotent(store):
    """A second sweep pass over an already-escalated task finds no open
    attempt row, writes nothing new, and reports no further escalation."""
    config = {
        "bounds": {"attempt_timeout_s": 60},
        "blockers": {"stuck_active_minutes": 1},
    }
    t = await _active_task(store, last_event_age_min=90)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt_id, tokens_used=7)

    w = _watcher(store, config)
    first = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert first is True
    # `_is_terminal` treats ESCALATED as non-terminal (SCRUM-68) — so it's
    # NOT what stops a second escalation. What actually stops it: escalating
    # emits an `escalated_stalled` event via `_emit`, which is itself a fresh
    # task event, so `last_event_ts` moves to "now". The second call's
    # `age_min` is therefore ~0 and fails the `age_min < stuck_active_minutes`
    # gate before ever reaching the (already-empty) open-attempt lookup — the
    # second pass finds no open row, writes nothing, and returns False.
    second = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert second is False  # no fresh silence yet — nothing to re-escalate
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 1
    assert attempts[0]["tokens_used"] == 7  # untouched by the second (no-op) pass


# --------------------------------------------------------------------- AC4 -- #

async def test_the_sweep_does_not_stop_a_live_backend_coroutine(store):
    """The stall sweep never cancels or awaits the backend session — it only
    writes to the Store. A coroutine standing in for a live coder session
    (the exact shape `_await_coder_turn` awaits) is still running, unaware
    and untouched, after the task has been escalated.

    Empirical record (quoted in the PR): `grep -n "request_task_cancel\\|
    _active_backend_task" src/no_human/blockers/wake.py` returns no matches —
    the escalation path never references the cancel API or a backend-task
    handle. `Orchestrator._watch_for_cancel` (orchestrator.py) polls only
    `store.get_cancel_request`, and `_agent_sink`'s only two `CancelRequested`
    raise sites are keyed off that poll result and `_server_stopping` — never
    off `tasks.status`. So a coroutine has nothing to observe here.
    """
    config = {
        "bounds": {"attempt_timeout_s": 60},
        "blockers": {"stuck_active_minutes": 1},
    }
    t = await _active_task(store, last_event_age_min=90)
    await store.create_attempt(t.id, 1)

    done = asyncio.Event()

    async def _fake_backend():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            done.set()
            raise
        done.set()

    backend_task = asyncio.create_task(_fake_backend())
    await asyncio.sleep(0)  # let it start

    w = _watcher(store, config)
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert escalated is True
    assert (await store.get_task(t.id)).status is TaskStatus.ESCALATED

    await asyncio.sleep(0)
    assert not backend_task.done(), (
        "the stall sweep must not touch the live backend coroutine")
    assert not done.is_set()

    backend_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await backend_task


class _NeverStartOrch:
    """An orchestrator whose `run_task` must never be called in this test —
    constructing one at all is the failure this guards against."""

    def __init__(self, task):
        raise AssertionError(
            "a second session must never be constructed for a task whose "
            "first is still live")


async def test_a_resume_cannot_start_a_second_session_while_the_first_is_live(store):
    """After escalation, a human runs `nh task resume` — which flips the row
    straight back to IMPLEMENTING via a plain `set_status` write (cli/
    commands.py `task_resume`), with no liveness check of its own. The ONLY
    thing standing between that and a second session racing the still-live
    first one is `Scheduler._inflight`: every dispatch/resume candidate list
    (`_claimable`, scheduler.py ~943/957) filters `t.id not in self._inflight`.
    As long as the first session's task id is still reserved there, the
    scheduler's own tick must start nothing for it.
    """
    t = await _active_task(store, last_event_age_min=90)
    w = _watcher(store, {
        "bounds": {"attempt_timeout_s": 60},
        "blockers": {"stuck_active_minutes": 1},
    })
    escalated = await w._escalate_if_stalled(t, now=datetime.now(timezone.utc))
    assert escalated is True

    # Simulate `nh task resume`: flip straight back to IMPLEMENTING (its
    # actual, minimal effect on the tasks row), while the first backend
    # coroutine is still alive and its id is still reserved in `_inflight`.
    fresh = await store.get_task(t.id)
    await store.set_status(fresh, TaskStatus.IMPLEMENTING, validate=False)

    sched = Scheduler(store, _NeverStartOrch, max_workers=5)
    sched._inflight.add(t.id)  # the first session's reservation, still held

    started = await sched.tick(now=datetime.now(timezone.utc))
    assert t.id not in started, (
        "the scheduler must not dispatch a second session while the first "
        "session's id is still in _inflight")
