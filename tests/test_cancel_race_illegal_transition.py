"""Issue #423: a human cancel racing a running attempt crashes with
``IllegalTransition``.

``POST /api/tasks/{id}/cancel`` and ``nh task cancel`` both write the task
row to FAILED whenever there is no live coder session to interrupt (the
``cancel_session_not_found`` shape — see ``api/app.py:cancel_task``). That
write lands on the PERSISTED row. The orchestrator's own attempt loop only
ever checked ``_pending_cancel`` (``tasks.cancel_requested``, the pause
column) for a stop signal — but the cancel writer explicitly CLEARS that
column in the same breath it sets FAILED, so `_pending_cancel` never sees
it. The loop drives on with a stale in-memory ``task.status`` and its next
``set_status(...)`` call gets silently refused by the CAS guard in
``Store._write_status`` (rowcount == 0 because the row no longer reads what
the caller thought), which ALSO re-syncs the in-memory handle to FAILED as
a side effect. The NEXT explicit ``set_status(task, TaskStatus.IMPLEMENTING)``
then raises ``IllegalTransition: failed -> implementing`` — a crash, not a
graceful stop. This hit 10 tracked tasks in production.

Every test below is RED on unfixed main: none of ``_persisted_terminal_cancel``
/ ``_stop_for_raced_cancel`` exist there, and the three call sites in
``_drive`` / ``_run_attempt`` never re-read the persisted row, so the race
either crashes with ``IllegalTransition`` or silently starts a further
attempt after the task was already cancelled.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from no_human.blockers import human_event
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator, TaskOutcome
from no_human.core.task import IllegalTransition, Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

from tests.test_infra_not_work import _config, bare_repo  # noqa: F401 — shared fixtures

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Reproduces the API endpoint's exact write order for the                     #
# `cancel_session_not_found` shape: record the reason, CLEAR the pause        #
# column, then set FAILED with `human_override=True` (bypasses the CAS).      #
# No scheduler/`request_task_cancel` call — there is deliberately no live     #
# orchestrator session in these unit tests, which is exactly the shape that   #
# races the attempt loop instead of ever meeting a session to stop.           #
# --------------------------------------------------------------------------- #

async def _cancel_like_the_api(store: Store, task_id: str, reason: str) -> Task:
    fresh = await store.get_task(task_id)
    prior_status = fresh.status
    prior_blocker = fresh.blocker if isinstance(fresh.blocker, dict) else None
    fresh.context = await store.record_cancel_reason(task_id, reason)
    await store.clear_cancel_request(task_id)
    await store.set_status(
        fresh, TaskStatus.FAILED, validate=False, human_override=True,
        event=human_event(
            "cancel", prior_status=prior_status, prior_blocker=prior_blocker,
            reason=reason, actor="operator:api"),
    )
    assert await store.get_cancel_request(task_id) is None, (
        "the API cancel path clears the pause column — a test that leaves "
        "it set would (wrongly) let _pending_cancel see the race too")
    return fresh


# --------------------------------------------------------------------------- #
# A fake backend that never returns on its own — stands in for a live coder   #
# session. Never actually reached by any test below: every scenario stops    #
# BEFORE the backend would start, which is the whole point of the fix.        #
# --------------------------------------------------------------------------- #

class _SleepingBackend:
    def __init__(self):
        self.started = asyncio.Event()

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, on_compact=None,
                  **kwargs):
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        raise AssertionError(  # pragma: no cover
            "a coder session started after the task was already cancelled")


def _fake_usage_result(tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        tokens_used=tokens, cache_read_tokens=0, cache_creation_tokens=0,
        output_tokens=None,
    )


async def _make_orch(store, tmp_path, backend=None) -> Orchestrator:
    cfg = _config(tmp_path)
    return Orchestrator(
        store, cfg.data, backend or _SleepingBackend(), SlackNotifier(None),
        event_sink=[].append,
    )


# --------------------------------------------------------------------------- #
# (a) cancel before the attempt starts (intake/planning).                     #
# --------------------------------------------------------------------------- #

async def test_cancel_during_planning_does_not_crash_the_following_drive(
        store, bare_repo, tmp_path):
    """The task is parked at PLANNING — a healthy in-memory handle — while a
    human cancel lands on the PERSISTED row behind its back. `_drive` must
    stop cleanly instead of driving on with the stale handle and crashing on
    its own next `set_status` call."""
    orch = await _make_orch(store, tmp_path)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    await _cancel_like_the_api(store, task.id, "operator cancelled during planning")
    # `task` (the orchestrator's in-memory handle) is still PLANNING here —
    # exactly the stale-handle race this closes.
    assert task.status == TaskStatus.PLANNING

    outcome = await orch._drive(task, repo)

    assert outcome.status == TaskStatus.FAILED
    assert outcome.off_ramp is True, (
        "without off_ramp=True, _drive would immediately start a fresh "
        "attempt and silently undo the cancel")
    assert "cancelled" in outcome.detail

    persisted = await store.get_task(task.id)
    assert persisted.status == TaskStatus.FAILED
    assert (persisted.context or {}).get("cancel_reason") == (
        "operator cancelled during planning")

    attempt_count, token_sum = await store.lifetime_usage(task.id)
    assert attempt_count == 0, "a raced cancel before any attempt must not count against budget"
    assert token_sum == 0


# --------------------------------------------------------------------------- #
# (b) cancel between attempt dispatch and `set_status(IMPLEMENTING)`.         #
# --------------------------------------------------------------------------- #

async def test_cancel_between_attempt_dispatch_and_implementing_stops_without_a_session(
        store, bare_repo, tmp_path):
    """Race the cancel into the gap between `_arm_attempt_budget` (which
    runs right after `create_attempt`) and `set_status(IMPLEMENTING)` inside
    `_run_attempt` — the exact boundary the crash report traced the bug to.
    No coder session may start, the attempt row this attempt itself created
    must end `interrupted` with no usage, and the task keeps its cancelled
    status instead of raising."""
    backend = _SleepingBackend()
    orch = await _make_orch(store, tmp_path, backend)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    orig_arm = orch._arm_attempt_budget

    async def _arm_then_cancel(task_arg):
        result = await orig_arm(task_arg)
        await _cancel_like_the_api(store, task.id, "operator cancelled mid-dispatch")
        return result

    orch._arm_attempt_budget = _arm_then_cancel

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert not backend.started.is_set(), (
        "a coder session started after the task was already cancelled")
    assert outcome.status == TaskStatus.FAILED
    assert outcome.off_ramp is True
    assert "cancelled" in outcome.detail

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    row = attempts[0]
    assert row["status"] == "interrupted", (
        "a raced attempt row must end interrupted, not failed — failed "
        "counts toward the lifetime budget")
    assert not row.get("tokens_used")

    attempt_count, token_sum = await store.lifetime_usage(task.id)
    assert attempt_count == 0, "an interrupted, zero-priced row must not count toward lifetime attempts"
    assert token_sum == 0


async def test_cancel_after_a_refused_status_write_no_longer_raises_illegal_transition(
        store, bare_repo, tmp_path):
    """The literal crash from the bug report: a status write refused by the
    CAS guard silently resyncs the in-memory handle to FAILED, and the
    UNFIXED code's next explicit `set_status(IMPLEMENTING)` call then raises
    `IllegalTransition: failed -> implementing`. Drive the exact sequence and
    assert it no longer raises."""
    orch = await _make_orch(store, tmp_path)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    await _cancel_like_the_api(store, task.id, "operator cancelled")
    # Simulate a prior refused write silently resyncing the stale in-memory
    # handle to FAILED, the way `Store._write_status` does on a CAS refusal.
    task.status = TaskStatus.FAILED

    # Unfixed code: `_run_attempt` drives on and eventually calls
    # `set_status(task, TaskStatus.IMPLEMENTING)` on this FAILED handle,
    # which raises. Fixed code: `_persisted_terminal_cancel` intercepts
    # first and neither raises nor reaches that call.
    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status == TaskStatus.FAILED
    assert outcome.off_ramp is True


# --------------------------------------------------------------------------- #
# (c) cancel past a coder session that already ran, with a further attempt   #
#     due — the further attempt must never start.                            #
# --------------------------------------------------------------------------- #

async def test_cancel_after_a_failed_attempt_does_not_start_the_next_one(
        store, bare_repo, tmp_path):
    """Attempt 1's coder session genuinely ran and failed (a plain FAILED,
    non-off_ramp outcome — the ordinary retry signal). The task is cancelled
    right as attempt 1 exits, before attempt 2 is dispatched. `_drive`'s
    loop must stop at the loop-head check instead of starting attempt 2 —
    proving the raced attempt is the FURTHER one, not the one whose session
    already ran."""
    orch = await _make_orch(store, tmp_path)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    calls = []

    async def _fake_run_attempt(task_arg, repo_arg, attempt_n, base_branch):
        calls.append(attempt_n)
        if attempt_n == 1:
            # Attempt 1's coder session "ran" (nothing here stubs a real
            # session — the point of this test is the LOOP HEAD, not
            # _run_attempt's own internals, which get their own coverage
            # above) and failed normally: a plain FAILED, non-off_ramp
            # outcome, exactly the shape that makes `_drive` retry.
            await _cancel_like_the_api(store, task.id, "operator cancelled after attempt 1")
            return TaskOutcome(task_arg, status=TaskStatus.FAILED,
                                detail="attempt 1 failed normally")
        raise AssertionError(  # pragma: no cover
            f"a further attempt ({attempt_n}) started after the task was "
            "already cancelled")

    orch._run_attempt = _fake_run_attempt

    outcome = await orch._drive(task, repo)

    assert calls == [1], "the further attempt must never be dispatched"
    assert outcome.status == TaskStatus.FAILED
    assert outcome.off_ramp is True
    assert "cancelled" in outcome.detail


# --------------------------------------------------------------------------- #
# Spend before the stop lands on the unattributed ledger, not on the raced   #
# attempt row.                                                                #
# --------------------------------------------------------------------------- #

async def test_planning_spend_of_a_raced_cancel_lands_on_the_unattributed_ledger(
        store, bare_repo, tmp_path):
    """Planner spend booked before the cancel landed (`_note_plan_usage`,
    drained only on an attempt EXIT via `_pop_aux_usage`) must survive a
    raced-cancel stop that creates no attempt row to drain it onto — booked
    instead through `_drive_watched`'s `finally` -> `_flush_orphaned_aux_usage`
    to the `orphaned_plan_usage` ledger site."""
    orch = await _make_orch(store, tmp_path)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    # Planning spend that happened before the race — the accumulator
    # `_pop_aux_usage` would otherwise drain onto an attempt row.
    orch._note_plan_usage(_fake_usage_result(4321))

    await _cancel_like_the_api(store, task.id, "operator cancelled during planning")

    outcome = await orch._drive_watched(task, repo)

    assert outcome.status == TaskStatus.FAILED
    assert outcome.off_ramp is True

    # No attempt row exists to have drained the spend onto.
    attempts = await store.list_attempts(task.id)
    assert attempts == []

    totals = await store.unattributed_usage_totals(task.id, attributed=True)
    assert totals["tokens_used"] == 4321, (
        "planning spend orphaned by a raced cancel must land on the "
        "unattributed ledger, not vanish")


# --------------------------------------------------------------------------- #
# Control: an illegal transition with NO concurrent cancel still raises.      #
# --------------------------------------------------------------------------- #

async def test_illegal_transition_without_a_cancel_still_raises(
        store, bare_repo, tmp_path):
    """The fix must not blanket-suppress `IllegalTransition` — only a
    PERSISTED cancel (status FAILED) should short-circuit it. A task whose
    persisted row is healthy (TESTING) but whose in-memory handle was forced
    to a terminal DONE by something unrelated must still raise on an illegal
    `set_status` call, and `_persisted_terminal_cancel` must report no
    cancel for it."""
    orch = await _make_orch(store, tmp_path)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    await store.set_status(task, TaskStatus.IMPLEMENTING)
    await store.set_status(task, TaskStatus.REVIEWING)
    await store.set_status(task, TaskStatus.TESTING)

    # Nothing cancelled this task — the persisted row genuinely reads
    # TESTING, a healthy non-terminal status.
    terminal = await orch._persisted_terminal_cancel(task)
    assert terminal is None

    # DONE -> IMPLEMENTING is not in ALLOWED_TRANSITIONS (DONE has no
    # outgoing transitions at all): force the in-memory handle terminal by
    # some unrelated means and confirm the guard still fires.
    task.status = TaskStatus.DONE
    with pytest.raises(IllegalTransition):
        await store.set_status(task, TaskStatus.IMPLEMENTING)
