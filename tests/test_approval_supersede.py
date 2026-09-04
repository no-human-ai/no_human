"""A stale `approved_at` survived escalation/conflict rounds: the board kept
showing "approved - merge pending" on a task that had already moved into
Needs Answer (measured: 16 rows carried `approved_at` with
status in {failed, escalated, implementing}). `approved_at` stays a
permanent audit trail (never cleared), so the fix is a second, write-once
marker — `context.approval_superseded_at` — stamped by `core/db.py::
_write_status` the instant a row leaves `awaiting_approval` for anything
other than `done`. `core/lanes.py::approval_pending` (and its JS twin,
`web/src/approvalState.js::approvalLive`) is the ONE predicate every
surface (API payload, CLI, board, drawer) derives the chip and the
needs-you suppression from, so none of them can disagree with the lane a
task is actually sitting in again.

Repro trio (also listed in `.no_human/repro_tests.json`, run against a copy
of the tree at the merge base to prove they fail on base code and pass on
this fix):
  - test_escalation_supersedes_a_recorded_approval
  - test_send_back_to_implementing_supersedes
  - test_completed_merge_keeps_the_approval
"""

from __future__ import annotations

import pytest

from no_human.api.models import TaskSummaryOut
from no_human.core.lanes import approval_pending
from no_human.core.task import Task, TaskStatus

pytestmark = pytest.mark.asyncio


def _pending(task: Task) -> bool:
    """`approval_pending` (like the rest of core/lanes.py) reads the
    FLATTENED board payload shape (`TaskSummaryOut`), not a raw
    `core.task.Task` — `approved_at`/`approval_superseded_at` live nested
    under `task.context` on the raw row. Mirrors how the API/CLI/board
    actually call it (`TaskSummaryOut.from_task` first)."""
    return approval_pending(TaskSummaryOut.from_task(task))


async def _approved_task(store, **create_kwargs) -> Task:
    """A task sitting in `awaiting_approval` with a recorded `approved_at`,
    the exact shape `POST /api/tasks/{id}/approve` and `nh approve` leave
    behind (see `api/app.py::approve_task`, `cli/commands.py::_land_one`)."""
    t = Task.new("x", repo_path="/tmp/r", **create_kwargs)
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    ctx = await store.merge_context(t.id, {"approved_at": "2026-08-01T00:00:00+00:00"})
    t.context = ctx
    return t


# --------------------------------------------------------------------------- #
# Repro trio                                                                   #
# --------------------------------------------------------------------------- #


async def test_escalation_supersedes_a_recorded_approval(store):
    """AC1: an approval recorded on awaiting_approval stops reading as
    pending after the task escalates."""
    t = await _approved_task(store)

    await store.set_status(t, TaskStatus.ESCALATED, validate=False)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert fresh.context.get("approved_at"), "audit trail must survive"
    assert fresh.context.get("approval_superseded_at"), (
        "escalating an approved task must stamp approval_superseded_at")
    assert not _pending(fresh), (
        "an escalated task must never report a live approval again")


async def test_send_back_to_implementing_supersedes(store):
    """AC1: a conflict-round send-back (awaiting_approval -> implementing,
    a legal MAIN_FLOW off-ramp per core/task.py) also supersedes."""
    t = await _approved_task(store)

    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context.get("approved_at")
    assert fresh.context.get("approval_superseded_at")
    assert not _pending(fresh)


async def test_completed_merge_keeps_the_approval(store):
    """AC1: a completed merge (awaiting_approval -> done, the real
    `complete_if_content_landed` shape: validate=False,
    event={'kind': 'shipped', 'source': 'watcher', ...}) must NOT stamp
    approval_superseded_at — the approval's success, not its supersession."""
    t = await _approved_task(store)

    await store.set_status(
        t, TaskStatus.DONE, validate=False,
        event={"source": "watcher", "kind": "shipped", "text": "landed",
               "ts": 0.0},
    )

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert fresh.context.get("approved_at")
    assert not fresh.context.get("approval_superseded_at"), (
        "a genuine completion must not be treated as a supersession")


# --------------------------------------------------------------------------- #
# Additional coverage                                                         #
# --------------------------------------------------------------------------- #


async def test_failure_supersedes(store):
    t = await _approved_task(store)

    await store.set_status(t, TaskStatus.FAILED, validate=False)

    fresh = await store.get_task(t.id)
    assert fresh.context.get("approval_superseded_at")
    assert not _pending(fresh)


async def test_no_marker_stamped_when_no_approval_was_recorded(store):
    """The CASE guard must be a true no-op when there is nothing to
    supersede — a task that never carried approved_at leaving
    awaiting_approval must not grow a spurious marker."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    await store.set_status(t, TaskStatus.ESCALATED, validate=False)

    fresh = await store.get_task(t.id)
    assert not (fresh.context or {}).get("approval_superseded_at")


async def test_marker_is_write_once_across_repeated_transitions(store):
    """A second off-ramp transition must not move the timestamp already
    recorded by the first — the IS NULL guard in the SQL CASE."""
    t = await _approved_task(store)

    await store.set_status(t, TaskStatus.ESCALATED, validate=False)
    first = (await store.get_task(t.id)).context["approval_superseded_at"]

    await store.set_status(t, TaskStatus.AWAITING_INPUT, validate=False)
    second = (await store.get_task(t.id)).context["approval_superseded_at"]

    assert first == second, (
        "approval_superseded_at must be write-once, not re-stamped on every "
        "subsequent transition")


async def test_a_fresh_approval_after_supersession_reads_pending_again(store):
    """The approve endpoint's `merge_context({'approved_at': ..,
    'approval_superseded_at': None})` shape (api/app.py::approve_task):
    once a task cycles back through implementing/testing to a FRESH
    awaiting_approval and gets re-approved, the chip must live again."""
    t = await _approved_task(store)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    assert (await store.get_task(t.id)).context.get("approval_superseded_at")

    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    await store.set_status(t, TaskStatus.TESTING, validate=False)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    ctx = await store.merge_context(
        t.id, {"approved_at": "2026-09-01T00:00:00+00:00",
               "approval_superseded_at": None})
    t.context = ctx

    fresh = await store.get_task(t.id)
    assert _pending(fresh), (
        "a fresh re-approval after a cleared marker must read pending again")


async def test_human_override_branch_also_supersedes(store):
    """`human_override=True` (retry/cancel/shipped verbs) takes a different
    UPDATE branch in _write_status; the supersede CASE must be spliced into
    that branch too, not just the default CAS."""
    t = await _approved_task(store)

    await store.set_status(
        t, TaskStatus.PENDING, validate=False, human_override=True)

    fresh = await store.get_task(t.id)
    assert fresh.context.get("approval_superseded_at")
    assert not _pending(fresh)


async def test_terminal_reconcile_branch_is_unaffected_when_not_awaiting_approval(store):
    """terminal_reconcile is a narrow FAILED->DONE-style reconciliation CAS;
    a task that was never in awaiting_approval must not spuriously grow a
    marker just because that branch's SQL fired."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.FAILED, validate=False)

    await store.set_status(
        t, TaskStatus.DONE, validate=False, terminal_reconcile=True,
        event={"source": "watcher", "kind": "shipped", "text": "landed",
               "ts": 0.0},
    )

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert not (fresh.context or {}).get("approval_superseded_at")


async def test_in_process_task_object_mirrors_the_marker_immediately(store):
    """The best-effort in-process mirror in _write_status: a caller reading
    task.context right after set_status returns (no fresh SELECT) must
    already see the marker."""
    t = await _approved_task(store)

    returned = await store.set_status(t, TaskStatus.ESCALATED, validate=False)

    assert returned is not None
    assert returned.context.get("approval_superseded_at")
    assert t.context.get("approval_superseded_at"), (
        "the caller's own Task handle must be mirrored too")


async def test_approval_pending_false_without_approved_at(store):
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    fresh = await store.get_task(t.id)
    assert not _pending(fresh)


async def test_approval_pending_true_while_still_awaiting_approval(store):
    t = await _approved_task(store)

    fresh = await store.get_task(t.id)
    assert _pending(fresh)
