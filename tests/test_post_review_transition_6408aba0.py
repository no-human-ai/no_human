"""Regression test for incident 6408aba0 (2026-08-19): task 6408aba0's review
completed with "review: PASS - 0 blocking" at 17:35:57 and, one second later,
the orchestrator raised `IllegalTransition: implementing -> testing is not
allowed`, discarding a fully reviewed, passing change (it had to be landed by
hand). The review runs INSIDE the implement round (`_run_attempt`), and the
in-process `task.status` handle can regress from a reviewing-adjacent state
back to IMPLEMENTING between the review verdict and the post-review
`set_status(TESTING)` call — via `Store.update_task` refreshing the handle
from the DB row, or `WakeWatcher._resume` writing IMPLEMENTING with
`validate=False`.

This file replays that exact event order against a real `Store` and a real
`Orchestrator._advance_after_review`.
"""

import pytest

from no_human.core.db import Store
from no_human.core.task import IllegalTransition, Task, TaskStatus


def _orch(store, tmp_path):
    from no_human.config import load_config
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier

    class _Backend:
        model = "claude-sonnet-5"
        never_push_to = ["main"]

    class _Reviewer:
        model = "claude-opus-5"
        _on_event = None

    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        reviewer=_Reviewer())


async def _implementing_task_with_attempt(store, *, review_passed=None):
    task = Task.new("fix the thing", repo_path="/tmp/r")
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    await store.set_status(task, TaskStatus.IMPLEMENTING)
    attempt_id = await store.create_attempt(task.id, 1)
    if review_passed is not None:
        await store.update_attempt(attempt_id, review_passed=review_passed)
    return task, attempt_id


async def test_replay_6408aba0_review_pass_from_implementing_does_not_crash(
    store, tmp_path,
):
    """AC1 + AC2: replays 6408aba0's event order — a review PASS verdict is
    recorded while the task's own handle still reads IMPLEMENTING, and, before
    the post-review transition runs, a SECOND handle for the same row
    regresses it back to IMPLEMENTING exactly the way `WakeWatcher._resume`
    does (`validate=False`). The post-review transition must not raise, and
    the task must land on TESTING — confirmed both on the handle and by
    re-reading the row.
    """
    orch = _orch(store, tmp_path)
    task, attempt_id = await _implementing_task_with_attempt(
        store, review_passed=1)

    # a second in-process handle for the SAME row regresses it, exactly as
    # WakeWatcher._resume does: writes IMPLEMENTING with validate=False.
    second_handle = await store.find_task(task.id)
    await store.set_status(second_handle, TaskStatus.IMPLEMENTING, validate=False)

    assert task.status is TaskStatus.IMPLEMENTING  # incident precondition

    await orch._advance_after_review(
        task, TaskStatus.TESTING, attempt_id=attempt_id, branch="dev",
        base="main", commit="deadbeef", pr_url="https://example/pr/1",
        stage="post_review",
    )

    assert task.status is TaskStatus.TESTING

    reloaded = await store.get_task(task.id)
    assert reloaded.status is TaskStatus.TESTING


async def test_replay_continues_to_delivery(store, tmp_path):
    """AC2 tail: from TESTING the state machine still has a legal path onward
    to delivery — the new edge does not strand the task at TESTING."""
    orch = _orch(store, tmp_path)
    task, attempt_id = await _implementing_task_with_attempt(
        store, review_passed=1)

    await orch._advance_after_review(
        task, TaskStatus.TESTING, attempt_id=attempt_id, branch="dev",
        base="main", commit="deadbeef", pr_url="https://example/pr/1",
        stage="post_review",
    )
    assert task.status is TaskStatus.TESTING

    await store.set_status(task, TaskStatus.AWAITING_APPROVAL)
    assert task.status is TaskStatus.AWAITING_APPROVAL


async def test_post_review_transition_failure_preserves_pr_and_branch_metadata(
    store, tmp_path,
):
    """AC4: force the escape-hatch path with a target that is deliberately
    NOT a legal edge from IMPLEMENTING (AWAITING_APPROVAL) and confirm the
    reviewed work survives — branch/commit/PR on the attempt row, a
    recoverable marker in task context, and a durable event — instead of
    crashing or silently losing it."""
    orch = _orch(store, tmp_path)
    task, attempt_id = await _implementing_task_with_attempt(
        store, review_passed=1)

    await orch._advance_after_review(
        task, TaskStatus.AWAITING_APPROVAL, attempt_id=attempt_id,
        branch="dev", base="main", commit="cafef00d",
        pr_url="https://example/pr/2", stage="delivery",
    )

    # the transition still completes (via the escape hatch) rather than
    # crashing or leaving the task stuck in IMPLEMENTING.
    assert task.status is TaskStatus.AWAITING_APPROVAL
    reloaded = await store.get_task(task.id)
    assert reloaded.status is TaskStatus.AWAITING_APPROVAL

    rows = {r["id"]: r for r in await store.list_attempts(task.id)}
    row = rows[attempt_id]
    assert row["branch_name"] == "dev"
    assert row["commit_sha"] == "cafef00d"
    assert row["pr_url"] == "https://example/pr/2"

    assert await store.latest_attempt_pr_url(task.id) == "https://example/pr/2"
    branch_info = await store.latest_attempt_branch(task.id)
    assert branch_info == {"branch": "dev", "commit_sha": "cafef00d"}

    recovery = reloaded.context.get("post_review_recovery")
    assert recovery is not None, "no recoverable marker was written"
    assert recovery["from"] == "implementing"
    assert recovery["to"] == "awaiting_approval"
    assert recovery["branch"] == "dev"
    assert recovery["pr_url"] == "https://example/pr/2"

    events = await store.list_events(task.id)
    kinds = [e.get("kind") for e in events]
    assert "post_review_transition_recovered" in kinds


async def test_recovery_never_launders_an_illegal_jump(store, tmp_path):
    """AC3 companion: the escape hatch only ever completes a POST-REVIEW move
    with a confirmed PASS verdict recorded on THIS attempt, and only onto one
    of the two post-review states — it must never become a general-purpose
    way around the guard."""
    orch = _orch(store, tmp_path)

    # no confirmed review PASS on the attempt row -> must still raise, even
    # though the target is one of the two post-review states.
    task, attempt_id = await _implementing_task_with_attempt(
        store, review_passed=None)
    with pytest.raises(IllegalTransition):
        await orch._advance_after_review(
            task, TaskStatus.AWAITING_APPROVAL, attempt_id=attempt_id,
            branch="dev", base="main", commit="0000",
            pr_url="https://example/pr/3", stage="delivery",
        )

    # a confirmed PASS but a target OUTSIDE the two post-review states (e.g.
    # DONE) must still raise -- this is not a general transition bypass.
    task2, attempt_id2 = await _implementing_task_with_attempt(
        store, review_passed=1)
    with pytest.raises(IllegalTransition):
        await orch._advance_after_review(
            task2, TaskStatus.DONE, attempt_id=attempt_id2,
            branch="dev", base="main", commit="1111",
            pr_url="https://example/pr/4", stage="delivery",
        )
