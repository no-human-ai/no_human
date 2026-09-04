"""Autonomy telemetry (megaplan P0) — metric computation over tasks/attempts."""


from no_human.core.autonomy import compute_autonomy_metrics
from no_human.core.task import Task, TaskStatus


async def _mk(store, title, status, *, blocker=None):
    t = Task.new(title, repo_path="/r")
    await store.create_task(t)
    if status is not TaskStatus.PENDING:
        event = {"source": "test", "kind": "test_seed"} if status is TaskStatus.DONE else None
        await store.set_status(t, status, validate=False, event=event)
    if blocker is not None:
        t.blocker = blocker
        t.status = status
        await store.update_task(t)
    return t


async def test_empty_db_rates_are_none(store):
    rep = await compute_autonomy_metrics(store)
    assert rep.settled_tasks == 0
    assert rep.pr_reached_rate is None
    assert rep.touchpoint_rate is None


async def test_touchpoint_and_pr_reached_rates(store):
    # 2 PR-reached (awaiting_approval, done), 2 touchpoints (escalated, blocked),
    # 1 still active (implementing → excluded from settled).
    await _mk(store, "pr1", TaskStatus.AWAITING_APPROVAL)
    await _mk(store, "pr2", TaskStatus.DONE)
    await _mk(store, "esc", TaskStatus.ESCALATED,
              blocker={"category": "STAGNATION"})
    await _mk(store, "blk", TaskStatus.BLOCKED,
              blocker={"category": "MISSING_ACCESS"})
    await _mk(store, "wip", TaskStatus.IMPLEMENTING)

    rep = await compute_autonomy_metrics(store)
    assert rep.total_tasks == 5
    assert rep.settled_tasks == 4          # wip excluded
    assert rep.pr_reached == 2
    assert rep.touchpoint_tasks == 2
    assert rep.pr_reached_rate == 0.5
    assert rep.touchpoint_rate == 0.5
    assert rep.blocker_categories == {"STAGNATION": 1, "MISSING_ACCESS": 1}


async def test_turn_exhaustion_counted_from_absent_commit(store):
    # Production-shaped: neither `diff` nor `commit_sha` is passed here — this
    # mirrors the orchestrator's own max_turns/error path (orchestrator.py
    # :3724-3763) when `repo.has_changes()` was False: no WIP-PARTIAL commit
    # gets checkpointed, so `commit_sha` stays the column's NULL default. That
    # absence is the metric's real "empty" signal (see autonomy.py
    # `_is_empty_diff`'s docstring) — `diff` itself is never written in
    # production, and the orchestrator NEVER puts turn-exhaustion wording and
    # the zero-diff detail in the same `failure_reason` (they are mutually
    # exclusive branches — orchestrator.py:2702 vs. :3963), so a check for
    # that combined text is always false and would undercount every real
    # case; hence it is not used here.
    t = await _mk(store, "big", TaskStatus.FAILED)
    aid1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid1, failure_reason="agent run did not complete (max_turns)")
    aid2 = await store.create_attempt(t.id, 2)
    await store.update_attempt(aid2, failure_reason="review failed")

    rep = await compute_autonomy_metrics(store)
    assert rep.turn_exhaustion_empty == 1


async def test_turn_exhaustion_with_changes_is_not_counted(store):
    # A turn-exhaustion attempt that DID checkpoint a WIP-PARTIAL commit
    # (`commit_sha` set — mirrors orchestrator.py:3763 when
    # `repo.has_changes()` was True) is not "empty": it left real changes in
    # the tree even though the run itself failed. This is the case a
    # naive "any turn-exhaustion attempt counts" implementation would get
    # wrong.
    t = await _mk(store, "big", TaskStatus.FAILED)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, failure_reason="agent run did not complete (max_turns)",
        commit_sha="deadbeef")

    rep = await compute_autonomy_metrics(store)
    assert rep.turn_exhaustion_empty == 0


async def test_populated_diff_column_still_counts(store):
    # No production writer sets `diff` today, but the reader must still honor
    # it when a row DOES carry one (tests, a future writer): an explicitly
    # empty diff counts even when `commit_sha` IS set — a populated `diff`'s
    # own content takes precedence over the `commit_sha` fallback.
    t = await _mk(store, "big", TaskStatus.FAILED)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, failure_reason="agent run did not complete (max_turns)",
        commit_sha="deadbeef", diff="")

    rep = await compute_autonomy_metrics(store)
    assert rep.turn_exhaustion_empty == 1


async def test_days_window_filters(store):
    # created_at is only persisted on INSERT, so backdate before create_task.
    t = Task.new("old", repo_path="/r")
    t.created_at = "2000-01-01T00:00:00+00:00"
    await store.create_task(t)
    await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    rep = await compute_autonomy_metrics(store, days=1)
    assert rep.total_tasks == 0
