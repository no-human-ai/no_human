"""Criterion 5: the stall-watchdog-ordering fix must not touch the non-active
parked statuses at all.

The fix (`effective_stuck_active_minutes` in wake.py, `abandon_open_attempt`
in db.py) is scoped entirely to the second half of `WakeWatcher.tick` — the
stuck-active sweep over IMPLEMENTING/REVIEWING/TESTING/PLANNING/CONTEXT tasks
in `active_ids`. The first half of `tick` (the loop over BLOCKED,
PAUSED_QUOTA, AWAITING_INPUT, AWAITING_APPROVAL, calling `_evaluate` and
`_heartbeat`) never reads `self.stuck_active_minutes` / `raw_stuck_active_
minutes` at all — it is driven entirely by `self.max_park` (`blockers.
max_park_duration`), a config key the fix never touches.

These tests pin that boundary: for each of the four non-active statuses, one
task with a wake condition already satisfied ("due") and one not yet due,
driven through TWO watcher configs whose `stuck_active_minutes` /
`bounds.attempt_timeout_s` differ wildly (a config pair that — pre-fix —
would have made no difference here either, and — post-fix — pushes the
*active*-status effective threshold from 31m to 3601m). The `(id, action)`
list `tick()` returns for the non-active task must be byte-for-byte identical
across both configs; `escalated_stalled` must never appear for any of them;
and the heartbeat write on the no-action branch still fires.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)

# Two configs that differ hugely on the active-status sweep (30m vs the
# 3600s/60m-attempt-timeout floor of 61m) but share the same max_park_duration
# — a non-active-status task must not care which one is in effect.
_CONFIG_A = {
    "blockers": {"max_park_duration": "48h", "stuck_active_minutes": 30},
    "bounds": {"attempt_timeout_s": 60},
}
_CONFIG_B = {
    "blockers": {"max_park_duration": "48h", "stuck_active_minutes": 5},
    "bounds": {"attempt_timeout_s": 3600},
}


async def _park(store, *, status, blocker, wake_at=None, context=None):
    t = Task.new("Parked task", repo_path="/tmp/r")
    await store.create_task(t)
    t.blocker = blocker
    t.wake_check_at = wake_at
    if context is not None:
        t.context = context
    await store.update_task(t)
    await store.set_status(t, status, validate=False)
    return t


async def _run(store, task, *, config, **watcher_kwargs):
    """Fresh watcher per call — `WakeWatcher.__init__` reads config once, and
    each call must see its own config, not a stale instance's."""
    w = WakeWatcher(store, config, **watcher_kwargs)
    actions = await w.tick(now=NOW, active_ids=set())
    row = await store.get_task(task.id)
    return actions, row


@pytest.mark.parametrize("config", [_CONFIG_A, _CONFIG_B], ids=["A", "B"])
class TestBlockedUnchanged:
    async def test_due_after_resumes(self, store, config):
        t = await _park(
            store, status=TaskStatus.BLOCKED,
            blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                     "raised_at": (NOW - timedelta(hours=3)).isoformat(),
                     "confidence": 0.9},
        )
        actions, row = await _run(store, t, config=config)
        assert actions == [(t.id, "resumed")]
        assert row.status == TaskStatus.IMPLEMENTING

    async def test_not_yet_due_no_action_but_heartbeats(self, store, config):
        t = await _park(
            store, status=TaskStatus.BLOCKED,
            blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                     "raised_at": (NOW - timedelta(minutes=30)).isoformat(),
                     "confidence": 0.9},
        )
        before = len(await store.list_events(t.id))
        actions, row = await _run(store, t, config=config)
        assert actions == []
        assert row.status == TaskStatus.BLOCKED
        # The no-action branch still calls `_heartbeat`, which writes a
        # `wake_tick` event and stamps `last_wake_tick` in context.
        after_events = await store.list_events(t.id)
        assert len(after_events) == before + 1
        assert after_events[-1]["kind"] == "wake_tick"
        refreshed = await store.get_task(t.id)
        assert (refreshed.context or {}).get("last_wake_tick")

    async def test_past_max_park_escalates_by_timeout_never_by_stall(
        self, store, config,
    ):
        t = await _park(
            store, status=TaskStatus.BLOCKED,
            blocker={"category": "DEPENDENCY_WAIT",
                     "wake_condition": "pr_merged:org/repo#7",
                     "raised_at": (NOW - timedelta(hours=49)).isoformat(),
                     "confidence": 0.9},
        )

        async def pr_merged(ref):
            return False

        actions, row = await _run(store, t, config=config, pr_merged=pr_merged)
        assert actions == [(t.id, "escalated_timeout")]
        assert row.status == TaskStatus.ESCALATED
        assert (row.blocker or {}).get("timed_out") is True


@pytest.mark.parametrize("config", [_CONFIG_A, _CONFIG_B], ids=["A", "B"])
class TestPausedQuotaUnchanged:
    async def test_due_quota_refreshed_resumes(self, store, config):
        t = await _park(
            store, status=TaskStatus.PAUSED_QUOTA,
            blocker={"category": "QUOTA", "wake_condition": "quota_refreshed",
                     "raised_at": (NOW - timedelta(hours=1)).isoformat(),
                     "confidence": 1.0},
            wake_at=(NOW - timedelta(minutes=1)).isoformat(),
        )
        actions, row = await _run(store, t, config=config)
        assert actions == [(t.id, "resumed")]
        assert row.status == TaskStatus.IMPLEMENTING

    async def test_not_yet_due_no_action(self, store, config):
        t = await _park(
            store, status=TaskStatus.PAUSED_QUOTA,
            blocker={"category": "QUOTA", "wake_condition": "quota_refreshed",
                     "raised_at": (NOW - timedelta(hours=1)).isoformat(),
                     "confidence": 1.0},
            wake_at=(NOW + timedelta(hours=1)).isoformat(),
        )
        actions, row = await _run(store, t, config=config)
        assert actions == []
        assert row.status == TaskStatus.PAUSED_QUOTA


@pytest.mark.parametrize("config", [_CONFIG_A, _CONFIG_B], ids=["A", "B"])
class TestAwaitingInputUnchanged:
    async def test_never_auto_resumes_on_time(self, store, config):
        """AWAITING_INPUT only ever resumes on a human reply — a due `after:`
        condition must not resume it, with or without the fix's floor."""
        t = await _park(
            store, status=TaskStatus.AWAITING_INPUT,
            blocker={"category": "AMBIGUITY", "wake_condition": "after:1h",
                     "raised_at": (NOW - timedelta(hours=2)).isoformat(),
                     "confidence": 0.9},
        )
        actions, row = await _run(store, t, config=config)
        assert actions == []
        assert row.status == TaskStatus.AWAITING_INPUT

    async def test_past_max_park_escalates_by_timeout(self, store, config):
        t = await _park(
            store, status=TaskStatus.AWAITING_INPUT,
            blocker={"category": "AMBIGUITY", "wake_condition": "after:1h",
                     "raised_at": (NOW - timedelta(hours=49)).isoformat(),
                     "confidence": 0.9},
        )
        actions, row = await _run(store, t, config=config)
        assert actions == [(t.id, "escalated_timeout")]
        assert row.status == TaskStatus.ESCALATED


@pytest.mark.parametrize("config", [_CONFIG_A, _CONFIG_B], ids=["A", "B"])
class TestAwaitingApprovalUnchanged:
    async def test_due_merged_pr_completes(self, store, config):
        t = await _park(
            store, status=TaskStatus.AWAITING_APPROVAL,
            blocker={"category": "AMBIGUITY", "confidence": 0.9},
            context={"pr_watch": "https://code.example.com/o/r/pull/9"},
        )

        async def pr_state(url):
            return "MERGED"

        actions, row = await _run(store, t, config=config, pr_state=pr_state)
        assert actions == [(t.id, "merged")]
        assert row.status == TaskStatus.DONE

    async def test_no_resolvable_pr_no_action_never_times_out(self, store, config):
        """AWAITING_APPROVAL never times out on `max_park_duration` — it is
        governed entirely by the PR ladder (`_check_open_pr`), which returns
        None when no PR can be resolved. Confirm this holds even with a
        raised_at far past max_park_duration, under both configs."""
        t = await _park(
            store, status=TaskStatus.AWAITING_APPROVAL,
            blocker={"category": "AMBIGUITY", "confidence": 0.9,
                     "raised_at": (NOW - timedelta(hours=49)).isoformat()},
        )
        actions, row = await _run(store, t, config=config)
        assert actions == []
        assert row.status == TaskStatus.AWAITING_APPROVAL


@pytest.mark.parametrize("config", [_CONFIG_A, _CONFIG_B], ids=["A", "B"])
async def test_no_status_ever_emits_escalated_stalled(store, config):
    """`escalated_stalled` is produced only by the active-status sweep, which
    `tick()` never runs over BLOCKED/PAUSED_QUOTA/AWAITING_INPUT/AWAITING_
    APPROVAL (they aren't in the sweep's status tuple at all — see wake.py's
    `tick`). Belt-and-suspenders across all four statuses at once, silent
    (no due condition, no timeout) so only the no-action/heartbeat path runs."""
    tasks = []
    for status, blocker in (
        (TaskStatus.BLOCKED, {"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                               "raised_at": (NOW - timedelta(minutes=1)).isoformat(),
                               "confidence": 0.9}),
        (TaskStatus.PAUSED_QUOTA, {"category": "QUOTA", "wake_condition": "quota_refreshed",
                                    "raised_at": NOW.isoformat(), "confidence": 1.0}),
        (TaskStatus.AWAITING_INPUT, {"category": "AMBIGUITY", "wake_condition": "after:1h",
                                      "raised_at": (NOW - timedelta(minutes=1)).isoformat(),
                                      "confidence": 0.9}),
        (TaskStatus.AWAITING_APPROVAL, {"category": "AMBIGUITY", "confidence": 0.9}),
    ):
        t = await _park(store, status=status, blocker=blocker,
                         wake_at=(NOW + timedelta(hours=1)).isoformat())
        tasks.append(t)

    w = WakeWatcher(store, config)
    actions = await w.tick(now=NOW, active_ids=set())
    assert all(action != "escalated_stalled" for _id, action in actions)
    for t in tasks:
        row = await store.get_task(t.id)
        assert row.status == t.status  # none flipped to ESCALATED
