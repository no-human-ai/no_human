"""A stopped pool leaves a dead attempt `in_progress` and its task stranded.

`Store.close_attempts_of_terminal_tasks` already retires an `in_progress`
attempt row left open on a task that reached `done`/`failed`. Nothing did
the non-terminal twin: a task still `IMPLEMENTING` whose attempt row is
`in_progress` because the WHOLE POOL that owned it died, not just the one
worker. `Scheduler._recover_orphans` cannot see it either — it iterates
`_ORPHANABLE`, which excludes IMPLEMENTING on purpose (incident 6408aba0: a
live worker owns most IMPLEMENTING rows, and requeueing one out from under a
live worker is destructive). On restart the new pool wastes a full attempt
cycle rediscovering already-done work before escalating, and every
reconciliation verb stays fail-closed against `implementing` status forever,
because the attempt row keeps lying about being open.

This is the reaper's test file: `core.stranded_attempts.
reap_stranded_implementing_attempts`, wired into `Scheduler.run_forever` as
`Scheduler._reap_stranded_implementing_attempts`, between
`_salvage_dead_worktrees` and `_recover_orphans`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest

from no_human.config import load_config
from no_human.core.task import (
    ALLOWED_TRANSITIONS,
    LANDED_RECONCILABLE,
    TERMINAL_LANDED_RECONCILABLE,
    IllegalTransition,
    Task,
    TaskStatus,
    assert_stranded_reap,
    can_transition,
)
from no_human.core.stranded_attempts import (
    _LEASE_STALE_S,
    reap_stranded_implementing_attempts,
)

# A pid that cannot possibly be alive — pinned in test_worktree_isolation.py
# (`assert pid_alive(4194303) is False`); reused here so this file agrees
# with every other worktree test in the repo about what "dead" means.
_DEAD_PID = 4194303


@pytest.fixture
def cfg(tmp_path):
    c = load_config(tmp_path / "config.yaml")
    c.data["isolation"]["worktree_root"] = str(tmp_path / "wt")
    return c


async def _stranded(store, *, pr=None, review_passed=None,
                     failure_reason=None):
    """An IMPLEMENTING task with one `in_progress` attempt row — the exact
    shape a dead pool leaves behind."""
    task = Task.new("fix the thing", repo_path="/tmp/r")
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    await store.set_status(task, TaskStatus.IMPLEMENTING)
    attempt_id = await store.create_attempt(task.id, 1)
    fields = {}
    if pr is not None:
        fields["pr_url"] = pr
    if review_passed is not None:
        fields["review_passed"] = review_passed
    if failure_reason is not None:
        fields["failure_reason"] = failure_reason
    if fields:
        await store.update_attempt(attempt_id, **fields)
    return task, attempt_id


def _never_live(t):
    async def _false():
        return False
    return _false()


# --------------------------------------------------------------------------- #
# AC1 — the row is reaped: retired as `interrupted`, no tokens recorded       #
# --------------------------------------------------------------------------- #


async def test_a_dead_implementing_attempt_is_retired_as_interrupted(store, cfg):
    task, attempt_id = await _stranded(store)

    reaped, skipped = await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False, row_is_live=_never_live)

    assert (reaped, skipped) == (1, 0)

    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    row = rows[attempt_id]
    assert row["status"] == "interrupted"
    assert row["completed_at"] is not None
    assert (row["failure_reason"] or "").strip() != ""
    for col in store._usage_columns():
        assert (row[col] or 0) == 0


async def test_an_existing_failure_reason_survives_the_reap(store, cfg):
    task, attempt_id = await _stranded(
        store, failure_reason="pre-existing note")

    await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False, row_is_live=_never_live)

    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["failure_reason"] == "pre-existing note"


async def test_a_failed_attempt_close_is_not_counted_as_reaped_and_self_heals(
    store, cfg, monkeypatch,
):
    """The write order is `set_status` THEN `close_stranded_attempt` —
    never the reverse — precisely so that a failure of the SECOND write can
    never be counted as a successful reap nor leave the task stuck in
    IMPLEMENTING forever. If `close_stranded_attempt` blows up after the
    task has already left IMPLEMENTING, the row must be counted `skipped`
    (not `reaped`), the task status write must still have landed (it is not
    rolled back — that is the documented, accepted trade-off), and the
    leftover `in_progress` attempt row must not be permanently invisible:
    `Store.create_attempt`'s own stale-row sweep retires it the next time
    this task starts a fresh attempt, which is the self-healing path the
    reaper's docstring relies on instead of a cross-method transaction."""
    task, attempt_id = await _stranded(store)

    real_close = store.close_stranded_attempt

    async def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "close_stranded_attempt", _boom)

    reaped, skipped = await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False, row_is_live=_never_live)

    assert (reaped, skipped) == (0, 1)

    # The status write already landed — the task is off IMPLEMENTING, so it
    # is no longer invisible to every reconciliation verb.
    reloaded = await store.get_task(task.id)
    assert reloaded.status is TaskStatus.PENDING

    # The attempt row itself is untouched by the failed second write.
    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["status"] == "in_progress"

    # Self-heal: the next attempt this task starts retires the leftover row
    # via `create_attempt`'s existing, unmodified stale-row sweep — the task
    # is not stuck forever just because the reap's second write failed once.
    monkeypatch.setattr(store, "close_stranded_attempt", real_close)
    await store.create_attempt(task.id, 2)
    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["status"] == "interrupted"


# --------------------------------------------------------------------------- #
# AC2 — the task returns to pending / awaiting_approval, validated write     #
# --------------------------------------------------------------------------- #


async def test_a_reaped_attempt_with_a_passed_review_pr_parks_at_awaiting_approval(
    store, cfg,
):
    task, attempt_id = await _stranded(
        store, pr="https://example/pr/1", review_passed=1)

    await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False, row_is_live=_never_live)

    reloaded = await store.get_task(task.id)
    assert reloaded.status is TaskStatus.AWAITING_APPROVAL
    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["status"] == "interrupted"


@pytest.mark.parametrize("pr,review_passed", [
    (None, None),
    ("https://example/pr/2", 0),
    (None, 1),
])
async def test_a_reaped_attempt_without_a_reviewed_pr_returns_to_pending(
    store, cfg, pr, review_passed,
):
    task, attempt_id = await _stranded(store, pr=pr, review_passed=review_passed)

    await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False, row_is_live=_never_live)

    reloaded = await store.get_task(task.id)
    assert reloaded.status is TaskStatus.PENDING


async def test_the_reap_goes_through_the_validated_write_path():
    assert can_transition(TaskStatus.IMPLEMENTING, TaskStatus.PENDING) is False
    assert can_transition(
        TaskStatus.IMPLEMENTING, TaskStatus.AWAITING_APPROVAL) is False

    with pytest.raises(IllegalTransition):
        assert_stranded_reap(TaskStatus.REVIEWING)


# --------------------------------------------------------------------------- #
# AC3 — fail CLOSED on any live signal: attempt and status untouched         #
# --------------------------------------------------------------------------- #


async def test_a_live_server_leaves_the_attempt_and_status_untouched(store, cfg):
    task, attempt_id = await _stranded(store)

    reaped, skipped = await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: True)

    assert (reaped, skipped) == (0, 1)
    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["status"] == "in_progress"
    reloaded = await store.get_task(task.id)
    assert reloaded.status is TaskStatus.IMPLEMENTING


async def test_a_live_pidfile_owner_leaves_the_attempt_and_status_untouched(
    store, cfg, tmp_path, monkeypatch,
):
    import no_human.config as config_mod

    monkeypatch.setattr(config_mod, "NO_HUMAN_HOME", tmp_path)
    other_pid = os.getppid()
    (tmp_path / "nh.pid").write_text(str(other_pid))

    task, attempt_id = await _stranded(store)

    reaped, skipped = await reap_stranded_implementing_attempts(
        store, cfg.data, my_pid=os.getpid() + 1, server_probe=lambda: False)

    assert (reaped, skipped) == (0, 1)
    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["status"] == "in_progress"
    reloaded = await store.get_task(task.id)
    assert reloaded.status is TaskStatus.IMPLEMENTING


async def test_an_unexpired_lease_held_by_another_pid_leaves_it_untouched(
    store, cfg,
):
    import platform

    other_pid = os.getppid()
    await store.write_scheduler_heartbeat(
        pid=other_pid, host=platform.node(),
        started_at="2026-01-01T00:00:00+00:00", ts=time.time())

    task, attempt_id = await _stranded(store)

    reaped, skipped = await reap_stranded_implementing_attempts(
        store, cfg.data, my_pid=os.getpid() + 1, server_probe=lambda: False)

    assert (reaped, skipped) == (0, 1)
    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["status"] == "in_progress"


async def test_a_stale_lease_held_by_another_pid_is_reaped(store, cfg):
    import platform

    other_pid = os.getppid()
    await store.write_scheduler_heartbeat(
        pid=other_pid, host=platform.node(),
        started_at="2026-01-01T00:00:00+00:00",
        ts=time.time() - 10 * _LEASE_STALE_S)

    task, attempt_id = await _stranded(store)

    reaped, skipped = await reap_stranded_implementing_attempts(
        store, cfg.data, my_pid=os.getpid() + 1, server_probe=lambda: False,
        row_is_live=_never_live)

    assert (reaped, skipped) == (1, 0)
    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["status"] == "interrupted"


async def test_a_live_worktree_owner_pid_is_skipped_while_a_dead_one_is_reaped(
    store, cfg, tmp_path,
):
    root = tmp_path / "wt"
    root.mkdir(parents=True, exist_ok=True)

    live_task, live_attempt = await _stranded(store)
    dead_task, dead_attempt = await _stranded(store)

    (root / f"{live_task.id}.{os.getpid()}.tok").mkdir()
    (root / f"{dead_task.id}.{_DEAD_PID}.tok").mkdir()

    reaped, skipped = await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False, row_is_live=_never_live)

    assert (reaped, skipped) == (1, 1)

    rows = {r["id"]: r for r in await store.list_attempts(live_task.id)}
    assert rows[live_attempt]["status"] == "in_progress"
    live_reloaded = await store.get_task(live_task.id)
    assert live_reloaded.status is TaskStatus.IMPLEMENTING

    rows = {r["id"]: r for r in await store.list_attempts(dead_task.id)}
    assert rows[dead_attempt]["status"] == "interrupted"
    dead_reloaded = await store.get_task(dead_task.id)
    assert dead_reloaded.status is TaskStatus.PENDING


async def test_a_row_with_recent_activity_is_skipped(store, cfg):
    task, attempt_id = await _stranded(store)

    async def _row_is_live(t):
        return True

    reaped, skipped = await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False, row_is_live=_row_is_live)

    assert (reaped, skipped) == (0, 1)
    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["status"] == "in_progress"


async def test_an_unreadable_lease_row_fails_closed(store, cfg, monkeypatch):
    task, attempt_id = await _stranded(store)

    async def _boom():
        raise OSError("simulated transient DB read failure")

    monkeypatch.setattr(store, "read_scheduler_heartbeat", _boom)

    reaped, skipped = await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False)

    assert (reaped, skipped) == (0, 1)
    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    assert rows[attempt_id]["status"] == "in_progress"


def test_the_landed_override_and_restore_approval_guards_are_unchanged():
    assert LANDED_RECONCILABLE == frozenset({
        TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
        TaskStatus.TESTING, TaskStatus.AWAITING_APPROVAL,
    })
    assert TERMINAL_LANDED_RECONCILABLE == frozenset({TaskStatus.FAILED})
    # Freeze the IMPLEMENTING edge set itself — not just the two edges the
    # reaper cares about — against being silently widened to reach this
    # feature's targets through the general map instead of the narrow gate.
    assert ALLOWED_TRANSITIONS[TaskStatus.IMPLEMENTING] == frozenset({
        TaskStatus.REVIEWING, TaskStatus.TESTING, TaskStatus.COMPOUND_PARENT,
        TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
        TaskStatus.PAUSED_QUOTA, TaskStatus.ESCALATED, TaskStatus.FAILED,
    })
    assert TaskStatus.PENDING not in ALLOWED_TRANSITIONS[TaskStatus.IMPLEMENTING]
    assert TaskStatus.AWAITING_APPROVAL not in \
        ALLOWED_TRANSITIONS[TaskStatus.IMPLEMENTING]


# --------------------------------------------------------------------------- #
# AC4 — no lifetime attempt is consumed; recorded spend survives             #
# --------------------------------------------------------------------------- #


async def test_a_reaped_zero_spend_attempt_does_not_consume_a_lifetime_attempt(
    store, cfg,
):
    # While the row is still `in_progress` it is unconditionally counted by
    # `_lifetime_included_sql` (the zero-priced exclusion only applies to
    # rows already `status = 'interrupted'`) — that pre-reap count of 1 is
    # just the ordinary cost of an open attempt, not what this test is
    # about. What AC4 requires is that closing out a dead, zero-spend
    # attempt through the reaper leaves the task with NO lifetime attempt
    # consumed going forward, i.e. the reaped row must land in the
    # `interrupted` + zero-priced excluded bucket, not stay counted.
    task, attempt_id = await _stranded(store)

    before_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert before_attempts == 1

    await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False, row_is_live=_never_live)

    after_attempts, after_included, _ = await store.lifetime_usage_by_class(
        task.id)

    assert after_attempts == 0
    assert all(v == 0 for v in after_included.values())


async def test_recorded_spend_survives_the_reap_and_the_count_does_not_move(
    store, cfg,
):
    task, attempt_id = await _stranded(store)
    await store.update_attempt(
        attempt_id, tokens_used=1234, cache_read_tokens=99)

    before_attempts, before_included, _ = await store.lifetime_usage_by_class(
        task.id)

    await reap_stranded_implementing_attempts(
        store, cfg.data, server_probe=lambda: False, row_is_live=_never_live)

    after_attempts, after_included, _ = await store.lifetime_usage_by_class(
        task.id)

    assert after_attempts == before_attempts
    assert after_included["tokens_used"] == before_included["tokens_used"] == 1234
    assert after_included["cache_read_tokens"] == \
        before_included["cache_read_tokens"] == 99

    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    row = rows[attempt_id]
    assert row["status"] == "interrupted"
    assert row["tokens_used"] == 1234
    assert row["cache_read_tokens"] == 99


# --------------------------------------------------------------------------- #
# Scheduler wiring                                                            #
# --------------------------------------------------------------------------- #


async def test_the_startup_sweep_runs_after_salvage_and_before_orphan_recovery(
    store,
):
    from no_human.core.scheduler import Scheduler

    sched = Scheduler(store, lambda *a, **k: object(), config={})
    order = []

    async def _sweep():
        order.append("sweep")

    async def _salvage():
        order.append("salvage")

    async def _reap():
        order.append("reap")

    async def _recover(*, startup=True):
        order.append("recover")

    sched._sweep_stale_worktrees = _sweep
    sched._salvage_dead_worktrees = _salvage
    sched._reap_stranded_implementing_attempts = _reap
    sched._recover_orphans = _recover

    stop = asyncio.Event()
    stop.set()
    await sched.run_forever(stop=stop)

    assert order == ["sweep", "salvage", "reap", "recover"]


async def test_a_failing_reaper_never_blocks_boot(store, monkeypatch, caplog):
    from no_human.core import scheduler as scheduler_mod

    sched = scheduler_mod.Scheduler(store, lambda *a, **k: object(), config={})

    async def _boom(*a, **k):
        raise RuntimeError("simulated reaper failure")

    monkeypatch.setattr(scheduler_mod, "reap_stranded_implementing_attempts", _boom)

    stop = asyncio.Event()
    stop.set()
    with caplog.at_level(logging.ERROR, logger="no_human.scheduler"):
        await sched.run_forever(stop=stop)

    assert any("stranded" in r.message.lower() for r in caplog.records)
