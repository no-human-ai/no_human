"""D3-M1: auto-confirm a RECURRING review-origin lesson, without weakening the
gate (constraint #3).

The learning value: a review finding this repo keeps re-deriving should reach
the CODER without a human click, once it has recurred across >=2 DISTINCT tasks
that each reached HUMAN approval (a MERGED PR outcome).

The invariant that MUST survive it: an auto-confirmed review-origin lesson can
NEVER reach the REVIEWER that produced it. The channel split makes that true by
construction — `_format_active_memories` (coder) INCLUDES it, and
`_format_reviewer_memories` (reviewer) EXCLUDES exactly
(origin='review' AND confirmed_by='auto'). The mandatory test below fails if the
reviewer-side exclusion is removed.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from no_human.core.task import Task
from no_human.learning import (
    CONFIRMED_BY_AUTO,
    CONFIRMED_BY_HUMAN,
    ORIGIN_REVIEW,
    ORIGIN_SUPERVISOR,
    LearningQueue,
)
from no_human.vcs.pr_outcome import CLOSED_UNMERGED, MERGED, OPEN

REPO = "/tmp/repo-d3m1"


def _findings(label="image pinning"):
    return [{
        "label": label,
        "evidence": "ci/images.yaml:12 pins `latest`, not a digest",
        "file": "ci/images.yaml",
        "line": 12,
    }]


async def _task(store, title="Pin the CI images"):
    t = Task.new(title, repo_path=REPO)
    await store.create_task(t)
    return t


async def _approve(store, task, outcome=MERGED):
    """Record a settled PR outcome for a task (the human-approval signal)."""
    await store.record_pr_outcome(
        task_id=task.id, pr_url=f"https://forge/pr/{task.id[:6]}",
        outcome=outcome, outcome_evidence="test")


async def _confirmed_review_row(store):
    """The single confirmed review-origin memory in the store, or None."""
    rows = await store.list_memories(confirmed=True)
    review = [r for r in rows if r.get("origin") == ORIGIN_REVIEW]
    return review[0] if review else None


# ── (1) recurring + approved + flag ON → auto-confirmed 'auto' ────────────── #

@pytest.mark.asyncio
async def test_recurring_approved_two_tasks_auto_confirms(store):
    q = LearningQueue(store)
    t1, t2 = await _task(store), await _task(store, "Add a CI job")
    await _approve(store, t1)
    await _approve(store, t2)

    # First occurrence: a normal unconfirmed proposal, nothing auto-confirmed.
    mem_id = await q.propose_from_review(
        t1, findings=_findings(), auto_confirm_recurring=True)
    assert mem_id is not None
    row = await store.find_memory(mem_id)
    assert row["confirmed"] == 0

    # Second DISTINCT task hits the same finding → dedupe hit → auto-confirm.
    dup = await q.propose_from_review(
        t2, findings=_findings(), auto_confirm_recurring=True)
    assert dup is None                       # dedupe: no new proposal queued
    row = await store.find_memory(mem_id)
    assert row["confirmed"] == 1
    assert row["confirmed_by"] == CONFIRMED_BY_AUTO
    assert row["origin"] == ORIGIN_REVIEW


@pytest.mark.asyncio
async def test_auto_confirmed_lesson_reaches_the_coder(store):
    """The auto-confirmed lesson IS in the coder's confirmed_rules block."""
    o = _orch(store)
    row = await _drive_auto_confirm(store)
    await _load(o, store)
    coder = o._format_active_memories()
    assert row["title"] in coder


# ── (2) 🔴 THE INVARIANT — absent from the REVIEWER's confirmed_rules ─────── #

@pytest.mark.asyncio
async def test_invariant_auto_confirmed_review_lesson_absent_from_reviewer(store):
    """MANDATORY. An auto-confirmed review-origin lesson reaches the coder but
    NOT the reviewer. Mutation proof: delete the `not (...)` filter in
    `_format_reviewer_memories` and this assertion fails (the reviewer block
    then equals the coder block and contains the title)."""
    o = _orch(store)
    row = await _drive_auto_confirm(store)
    # NON-VACUITY CONTROL: a human-confirmed rule that MUST stay visible to the
    # reviewer, so a reviewer block that is empty for the WRONG reason cannot
    # make this test pass by accident.
    control_id = await store.add_memory(
        mem_type="rule", title="CONTROL human rule visible everywhere",
        content="A human-confirmed rule.", project=REPO,
        source="proposed", confirmed=False, dedupe_key="k-control")
    await store.confirm_memory(control_id, confirmed_by=CONFIRMED_BY_HUMAN)
    await _load(o, store)

    coder = o._format_active_memories()
    reviewer = o._format_reviewer_memories()

    assert "CONTROL human rule" in reviewer, (
        "the reviewer channel must carry ordinary confirmed rules — else the "
        "exclusion assertion below would pass vacuously")
    assert row["title"] in coder, "sanity: the coder must see the lesson"
    assert row["title"] not in reviewer, (
        "INVARIANT VIOLATED: an auto-confirmed review-origin lesson reached the "
        "reviewer that produced it")


@pytest.mark.asyncio
async def test_human_confirmed_review_lesson_DOES_reach_reviewer(store):
    """The exclusion keys on 'auto' only: a HUMAN-confirmed review lesson (a
    human stood between the verdict and the rule) DOES reach the reviewer."""
    o = _orch(store)
    q = LearningQueue(store)
    t = await _task(store)
    mem_id = await q.propose_from_review(t, findings=_findings())
    await q.confirm(mem_id)                   # human confirm → confirmed_by='human'
    row = await store.find_memory(mem_id)
    assert row["confirmed_by"] == CONFIRMED_BY_HUMAN

    await _load(o, store)
    assert row["title"] in o._format_active_memories()
    assert row["title"] in o._format_reviewer_memories()


@pytest.mark.asyncio
async def test_nonreview_origin_auto_is_not_excluded_from_reviewer(store):
    """The exclusion is narrow: only (origin='review' AND confirmed_by='auto').
    A non-review-origin row confirmed 'auto' is NOT excluded from the reviewer —
    it was never derived from the reviewer's verdict."""
    o = _orch(store)
    mem_id = await store.add_memory(
        mem_type="rule", title="Supervisor lesson XYZ",
        content="A recurring supervisor correction.", project=REPO,
        source="proposed", confirmed=False, dedupe_key="k-sup",
        origin=ORIGIN_SUPERVISOR)
    await store.confirm_memory(mem_id, confirmed_by=CONFIRMED_BY_AUTO)
    row = await store.find_memory(mem_id)
    assert row["origin"] == ORIGIN_SUPERVISOR
    assert row["confirmed_by"] == CONFIRMED_BY_AUTO

    await _load(o, store)
    assert row["title"] in o._format_reviewer_memories()


# ── (3) flag OFF / not-recurring / not-approved → NOT auto-confirmed ──────── #

@pytest.mark.asyncio
async def test_flag_off_does_not_auto_confirm(store):
    q = LearningQueue(store)
    t1, t2 = await _task(store), await _task(store, "Add a CI job")
    await _approve(store, t1)
    await _approve(store, t2)

    mem_id = await q.propose_from_review(
        t1, findings=_findings(), auto_confirm_recurring=False)
    dup = await q.propose_from_review(
        t2, findings=_findings(), auto_confirm_recurring=False)
    assert dup is None
    row = await store.find_memory(mem_id)
    assert row["confirmed"] == 0
    # OFF is inert down to the last write: no recurrence recorded either.
    import json
    assert "recurrences" not in (json.loads(row["evidence"]) or {})


@pytest.mark.asyncio
async def test_same_task_twice_is_one_distinct_task_no_auto_confirm(store):
    """Two review rounds of the SAME task are one distinct task, not two."""
    q = LearningQueue(store)
    t1 = await _task(store)
    await _approve(store, t1)

    mem_id = await q.propose_from_review(
        t1, findings=_findings(), review_round=1, auto_confirm_recurring=True)
    await q.propose_from_review(
        t1, findings=_findings(), review_round=2, auto_confirm_recurring=True)
    row = await store.find_memory(mem_id)
    assert row["confirmed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [OPEN, CLOSED_UNMERGED, None])
async def test_not_human_approved_no_auto_confirm(store, outcome):
    """Two distinct tasks, flag ON, but the recurrence tasks did NOT reach human
    approval (open / closed-unmerged / no outcome recorded) → no auto-confirm."""
    q = LearningQueue(store)
    t1, t2 = await _task(store), await _task(store, "Add a CI job")
    if outcome is not None:
        await _approve(store, t1, outcome=outcome)
        await _approve(store, t2, outcome=outcome)

    mem_id = await q.propose_from_review(
        t1, findings=_findings(), auto_confirm_recurring=True)
    await q.propose_from_review(
        t2, findings=_findings(), auto_confirm_recurring=True)
    row = await store.find_memory(mem_id)
    assert row["confirmed"] == 0


@pytest.mark.asyncio
async def test_only_one_of_two_tasks_approved_no_auto_confirm(store):
    """>=2 approved is required, not >=1: if only the recurrence task merged and
    the original did not, the finding is not yet proven twice over."""
    q = LearningQueue(store)
    t1, t2 = await _task(store), await _task(store, "Add a CI job")
    await _approve(store, t2)                 # only the second task merged
    mem_id = await q.propose_from_review(
        t1, findings=_findings(), auto_confirm_recurring=True)
    await q.propose_from_review(
        t2, findings=_findings(), auto_confirm_recurring=True)
    row = await store.find_memory(mem_id)
    assert row["confirmed"] == 0


# ── helpers that need a real Orchestrator for the channel methods ─────────── #

def _orch(store):
    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.config = {}
    return o


async def _load(o, store):
    """Install the store's confirmed, REPO-scoped rows as `_active_memories`,
    the shared source both channel methods read."""
    o._active_memories = await store.list_memories(confirmed=True, project=REPO)


async def _drive_auto_confirm(store):
    """Auto-confirm one review lesson across two approved tasks; return its row."""
    q = LearningQueue(store)
    t1, t2 = await _task(store), await _task(store, "Add a CI job")
    await _approve(store, t1)
    await _approve(store, t2)
    await q.propose_from_review(t1, findings=_findings(), auto_confirm_recurring=True)
    await q.propose_from_review(t2, findings=_findings(), auto_confirm_recurring=True)
    row = await _confirmed_review_row(store)
    assert row is not None and row["confirmed_by"] == CONFIRMED_BY_AUTO
    return row
