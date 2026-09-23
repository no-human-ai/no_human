"""An abandoned IMPLEMENTING row (silent, unheld, not queued for a slot)
starves the whole PENDING queue: `Scheduler._CLAIMABLE` is WIP-first, so such
a row sits at the head of every tick's claimable list forever, and the old
`claimable[:slots]` dispatch slice made a DECLINED row (one the scheduler
chose not to start) silently consume the slot it never used — measured live,
two rows, 2 free slots, 22 queued PENDING tasks, zero dispatched for hours.

These pin the fix's two halves: `Scheduler.tick`'s counted (not sliced)
dispatch loop (AC1), and `core.abandoned`'s detect/recover sweep (AC1
end-to-end, AC2, AC3) — called once per tick, before `_claimable()` is built,
so a row it recovers is re-ranked alongside PENDING on the SAME tick.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from no_human.core import abandoned, slot_wait
from no_human.core.db import Store
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus


async def _age_row(store: Store, task_id: str, seconds: float) -> None:
    """Back-date a row's `updated_at` AND every one of its `task_events` rows
    to `seconds` ago — the `_age_row` idiom (`test_scheduler.py:24`), so a
    test can simulate "this row has been silent for N minutes" without a
    real sleep."""
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task_id))
    await store.db.execute(
        "UPDATE task_events SET ts = ? WHERE task_id = ?",
        (time.time() - seconds, task_id))
    await store.db.commit()


async def _abandoned_row(
    store: Store, tmp_path, *, silent_seconds: float = 180 * 60,
    waiting: bool = False,
) -> Task:
    """A task shaped exactly like the live incident: IMPLEMENTING, unheld, an
    open attempt nobody is running any more, and silent for `silent_seconds`.

    `waiting=True` builds the "merely queued" control instead (AC2): its
    newest event is `waiting_for_slot`, so `store.tasks_waiting_for_slot()`
    — and therefore `find_abandoned`'s condition 3 — reads it as the OPPOSITE
    of abandoned, however long it has been silent.
    """
    t = Task.new("resumed task", repo_path=str(tmp_path))
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    await store.create_attempt(t.id, 1)
    event = ({"kind": slot_wait.KIND, "source": "orchestrator"} if waiting
             else {"kind": "tool_call", "source": "agent"})
    await store.save_events(t.id, [event])
    await _age_row(store, t.id, seconds=silent_seconds)
    return t


async def _mk_pending(store, n: int) -> list[str]:
    ids = []
    for i in range(n):
        t = Task.new(f"pending {i}", repo_path="/tmp/x")
        await store.create_task(t)
        ids.append(t.id)
    return ids


class FakeOrch:
    """Completes a run instantly to a terminal state — enough for the
    scheduler to free the slot on the next `wait_idle()`."""

    def __init__(self, store, *, terminal=TaskStatus.AWAITING_APPROVAL):
        self.store = store
        self.terminal = terminal
        self.started: list[str] = []

    async def run_task(self, task):
        self.started.append(task.id)
        await self.store.set_status(task, self.terminal, validate=False)
        return SimpleNamespace(status=self.terminal, task=task)


class NeverStartsOrch:
    """Records calls but never resolves — used only to prove a task was NOT
    dispatched (nothing here needs a run to finish)."""

    def __init__(self):
        self.started: list[str] = []

    async def run_task(self, task):
        self.started.append(task.id)
        return SimpleNamespace(status=TaskStatus.AWAITING_APPROVAL, task=task)


async def _checkpointed_shipped_task(store, tmp_path) -> Task:
    """A resumed IMPLEMENTING row `_shipped_before_dispatch` will decline
    (its content already landed) — borrowed from
    `test_scheduler_shipped_gate.py`'s `_checkpointed_task`. No events are
    ever written for it, so `core.abandoned.find_abandoned`'s fail-closed
    "never emitted" guard leaves it alone — this test is purely about the
    dispatch loop, not the abandoned-row sweep."""
    t = Task.new("resumed task", repo_path=str(tmp_path))
    t.context = {
        "pr_branch": "nh/x-1",
        "base_branch": "main",
        "pr_watch": "https://github.com/o/r/pull/302",
        "resume_from": {"sha": "abc", "by": "orphan_recovery"},
    }
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    return t


def _make_wake(pr_shipped):
    async def _noop_tick(*, now=None, active_ids=None):
        return None
    return SimpleNamespace(pr_shipped=pr_shipped, tick=_noop_tick)


# --------------------------------------------------------------------------- #
# AC1 — a row the scheduler DECLINES to start can never hold a free slot.
# --------------------------------------------------------------------------- #

async def test_a_row_the_scheduler_declines_to_start_cannot_hold_a_free_slot(
        store, tmp_path):
    async def probe(repo_path, branch, base):
        return True   # already shipped -> _shipped_before_dispatch declines it

    shipped = await _checkpointed_shipped_task(store, tmp_path)
    pending = Task.new("pending task", repo_path=str(tmp_path))
    await store.create_task(pending)

    fake = NeverStartsOrch()
    sched = Scheduler(store, lambda task=None: fake, max_workers=1,
                       wake_watcher=_make_wake(probe))

    started = await sched.tick()
    await asyncio.sleep(0)

    assert pending.id in started, (
        f"the declined shipped-row wasted the only free slot; started={started!r}")
    assert shipped.id not in started


async def test_abandoned_head_rows_do_not_starve_pending(store, tmp_path):
    abandoned_ids = {
        (await _abandoned_row(store, tmp_path)).id,
        (await _abandoned_row(store, tmp_path)).id,
    }
    pending_ids = set(await _mk_pending(store, 3))

    fake = FakeOrch(store)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2)

    started1 = await sched.tick()
    assert len(started1) == 2, (
        "2 free slots existed but dispatch stalled on the abandoned head")
    await sched.wait_idle()

    all_started = set(started1)
    for _ in range(4):   # ceil(5 tasks / 2 workers) == 3 ticks to fully drain
        if pending_ids <= all_started:
            break
        started = await sched.tick()
        all_started.update(started)
        await sched.wait_idle()

    assert pending_ids <= all_started, (
        "originally-PENDING tasks were never dispatched — the queue is "
        f"still starved; started so far: {all_started!r}")
    # And the abandoned rows themselves were recovered, not lost.
    assert abandoned_ids <= all_started


# --------------------------------------------------------------------------- #
# AC2 — the sweep must not touch a row that is merely queued for a slot.
# --------------------------------------------------------------------------- #

async def test_sweep_ignores_a_row_that_is_merely_queued(store, tmp_path):
    threshold = 40 * 60
    queued = await _abandoned_row(store, tmp_path, silent_seconds=90 * 60,
                                   waiting=True)
    silent = await _abandoned_row(store, tmp_path, silent_seconds=90 * 60,
                                   waiting=False)

    recovered = await abandoned.recover_abandoned(
        store, inflight_ids=set(), threshold_s=threshold)

    assert silent.id in recovered
    assert queued.id not in recovered

    still_queued = await store.get_task(queued.id)
    assert still_queued.status is TaskStatus.IMPLEMENTING
    queued_attempt = await store.latest_attempt(queued.id)
    assert queued_attempt["status"] == "in_progress", (
        "a merely-queued row's open attempt must never be closed — that is "
        "the case that would destroy a task mid-rework")

    recovered_task = await store.get_task(silent.id)
    assert recovered_task.status is TaskStatus.PENDING


async def test_a_row_sent_back_23_seconds_ago_is_untouched(store, tmp_path):
    threshold = 40 * 60
    t = await _abandoned_row(store, tmp_path, silent_seconds=23)

    recovered = await abandoned.recover_abandoned(
        store, inflight_ids=set(), threshold_s=threshold)

    assert recovered == []
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING


# --------------------------------------------------------------------------- #
# AC3 — a genuinely abandoned row is recovered, not left for an operator.
# --------------------------------------------------------------------------- #

async def test_abandoned_row_is_requeued_and_its_attempt_closed(store, tmp_path):
    threshold = 40 * 60
    t = await _abandoned_row(store, tmp_path, silent_seconds=180 * 60)

    recovered = await abandoned.recover_abandoned(
        store, inflight_ids=set(), threshold_s=threshold)

    assert recovered == [t.id]
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PENDING

    attempt = await store.latest_attempt(t.id)
    assert attempt["status"] == "interrupted"
    assert attempt["failure_reason"]
    assert "abandon" in attempt["failure_reason"].lower()

    events = await store.list_events(t.id)
    assert any(e.get("kind") == "abandoned_recovered" for e in events)


async def test_recovery_is_idempotent_and_capped(store, tmp_path):
    threshold = 40 * 60
    t = await _abandoned_row(store, tmp_path, silent_seconds=180 * 60)

    for i in range(3):
        recovered = await abandoned.recover_abandoned(
            store, inflight_ids=set(), threshold_s=threshold)
        assert recovered == [t.id], f"round {i}: expected a fresh recovery"
        fresh = await store.get_task(t.id)
        assert fresh.status is TaskStatus.PENDING
        assert fresh.context.get("abandoned_recoveries") == i + 1

        # A re-sweep on the SAME (already-PENDING) state must be a no-op —
        # idempotent because the row is no longer IMPLEMENTING, not because
        # of any separate dedupe bookkeeping.
        again = await abandoned.recover_abandoned(
            store, inflight_ids=set(), threshold_s=threshold)
        assert again == []

        # Simulate the row being picked back up, running, and going silent
        # again — the shape that would repeat forever without the cap.
        await store.set_status(fresh, TaskStatus.IMPLEMENTING, validate=False)
        await store.create_attempt(fresh.id, i + 2)
        await store.save_events(
            fresh.id, [{"kind": "tool_call", "source": "agent"}])
        await _age_row(store, fresh.id, seconds=180 * 60)

    # 4th time abandoned: escalate instead of re-queueing into the same fate.
    recovered = await abandoned.recover_abandoned(
        store, inflight_ids=set(), threshold_s=threshold)
    assert recovered == []
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert (fresh.blocker or {}).get("category") == "NOVEL_UNKNOWN"

    events = await store.list_events(t.id)
    assert any(e.get("kind") == "abandoned_escalated" for e in events)
