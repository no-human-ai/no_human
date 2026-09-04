"""INCIDENT 2026-08-13: tasks 79183501 and 1a4b7bf7 PASSED independent
review, then each supervising-train landing under their open PRs spawned a
pr_conflict rebase round — pure mechanical work, since the supervisor lands
a PASSed head with its own squash procedure — and each round still burned a
lifetime attempt, until attempts hit cap and killed work that was already
reviewed and about to land.

Once a task has a PASS on record, a MECHANICAL round (a pr_conflict rebase,
or a re-verification tick) must not consume a lifetime attempt, and the
lifetime-budget gate must not park a PASSed task on BUDGET_EXHAUSTED. A
review FAIL, or an operator reject, still consumes an attempt and re-arms
the cap.
"""

from __future__ import annotations

import pytest

from no_human.blockers import BlockerCategory
from no_human.blockers.wake import WakeWatcher
from no_human.core.bounds import Bounds
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.vcs import derived_conflict as dc


@pytest.fixture(autouse=True)
def _resolvable_conflicting_paths(monkeypatch):
    """`_approval_task` below uses a fake, non-existent `repo_path`
    ("/tmp/x") -- this file is testing the mechanical-round/attempt-counter
    logic, never conflicting-path enumeration (that's
    `tests/test_orchestrator_pr_conflict.py`'s job, against real git repos).
    A real `/tmp/x` cannot be enumerated, which now correctly escalates
    instead of falling through to a coder round (see
    `tests/test_wake_conflict.py`'s identical fixture). Stub a fixed,
    non-derived path so `test_pass_then_conflict_rebase_does_not_increment_attempts`
    still exercises the "enumeration succeeded" branch its assertions were
    written against."""
    async def fake_conflicting_paths(repo_path, base_tip, branch):
        return {"src/unrelated.py"}
    monkeypatch.setattr(dc, "conflicting_paths", fake_conflicting_paths)


def _orch(store):
    """A minimal orchestrator: only .store, .bounds, .config and .emit are
    exercised — copied from `tests/test_lifetime_budget.py`'s helper."""
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds()
    o.config = {}
    o._sink = lambda e: None
    return o


async def _approval_task(store, url="https://code.example.com/dev/x/pull/26"):
    """Copied from `tests/test_wake_conflict.py` — not imported across test
    modules."""
    t = Task.new("conflict", repo_path="/tmp/x")
    t.context = {"pr_watch": url, "pr_branch": "scratch/x", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, mergeable=None, merge_state=""):
    """Copied from `tests/test_wake_conflict.py` — not imported across test
    modules."""
    async def pr_mergeable(url):
        return {"mergeable": mergeable or "", "mergeStateStatus": merge_state}

    return WakeWatcher(store, {}, pr_mergeable=pr_mergeable)


async def _spend(store, task_id, attempts, *, last_verdict=None):
    """Create `attempts` ordinary (non-mechanical) attempt rows, each closed
    with a real terminal status and nonzero tokens — mirroring how a real
    attempt loop closes every row (`update_attempt(..., status=..., …)`)
    before the next one starts. Left bare, `create_attempt`'s own supersede
    sweep would tag every-but-the-newest row `status="interrupted"` with
    zero recorded work the moment the next attempt is created — the exact
    2026-08-20 DEAD-worker shape that is now excluded from the lifetime cap
    by design (see THE BOUNDARY in `Store.lifetime_usage_by_class`) — so a
    helper meant to represent N genuinely CONSUMED attempts must close each
    row itself rather than rely on that sweep by accident. If `last_verdict`
    is not None, the LAST one also records that review verdict (0 = FAIL,
    1 = PASS)."""
    aid = None
    for n in range(1, attempts + 1):
        aid = await store.create_attempt(task_id, n)
        await store.update_attempt(aid, status="succeeded", tokens_used=1_000)
    if last_verdict is not None and aid is not None:
        await store.update_attempt(aid, review_passed=last_verdict)
    return aid


async def test_pass_then_conflict_rebase_does_not_increment_attempts(store):
    """Red-first, criterion 1: PASS -> pr_conflict rebase rounds -> the
    lifetime attempt counter is unchanged and the task ends up back in
    AWAITING_APPROVAL. RED on unfixed code: `mechanical_round`/
    `_mechanical_round` do not exist, `create_attempt` has no `mechanical`
    kwarg, and the count reads 10 instead of 8.
    """
    t = await _approval_task(store)
    await _spend(store, t.id, attempts=8, last_verdict=1)

    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY")
    out = await w._check_open_pr(t)
    assert out == "resumed"

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context["send_back_feedback"][-1]["source"] == "pr_conflict"

    orch = _orch(store)

    # Two supervising-train landings, each spawning its own rebase round.
    for n in (9, 10):
        assert await orch._mechanical_round(fresh) is True
        await store.create_attempt(
            fresh.id, n, mechanical=await orch._mechanical_round(fresh))

    assert (await store.lifetime_usage_by_class(t.id))[0] == 8, (
        "mechanical rebase rounds must not consume a lifetime attempt")

    # The rebase rounds change no code; the task lands back in
    # AWAITING_APPROVAL exactly as `_finalize` would leave it, and the
    # lifetime-budget gate must not treat it as exhausted there.
    await store.set_status(fresh, TaskStatus.AWAITING_APPROVAL, validate=False)
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert await orch._check_lifetime_budget(fresh) is None


async def test_a_passed_task_never_parks_on_budget_exhausted(store):
    """Criterion 2: a task in AWAITING_APPROVAL with a PASS on record must
    never transition to failed(BUDGET_EXHAUSTED) — the PASS freezes budget
    enforcement, on both the gate and its advisory twin."""
    t = await _approval_task(store)
    await _spend(store, t.id, attempts=9, last_verdict=1)  # at cap, newest is PASS

    events = []
    o = _orch(store)
    o._sink = events.append

    assert await o._check_lifetime_budget(t) is None
    assert await o._at_lifetime_ceiling(t) is False

    ev = next(e for e in events if e["kind"] == "lifetime_budget")
    assert ev.get("frozen_by_review_pass") is True

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL


@pytest.mark.parametrize("shape", ["review_fail", "operator_reject_over_pass"])
async def test_review_fail_still_increments_attempts(store, shape):
    """Control, criterion 4: a review-FAIL or operator-reject round still
    consumes an attempt."""
    t = await _approval_task(store)

    if shape == "review_fail":
        await _spend(store, t.id, attempts=8, last_verdict=0)
    else:
        # An operator reject: the LATEST recorded verdict is still a PASS,
        # but the newest send_back_feedback entry names a non-mechanical
        # source (a PR comment) — a stale pr_conflict entry must not make a
        # corrective round free.
        await _spend(store, t.id, attempts=8, last_verdict=1)
        await store.append_context_list(t.id, "send_back_feedback", {
            "source": "pr_comment", "author": "operator",
            "message": "reject: please redo the approach",
        })

    fresh = await store.get_task(t.id)
    orch = _orch(store)
    assert await orch._mechanical_round(fresh) is False

    await store.create_attempt(
        fresh.id, 9, mechanical=await orch._mechanical_round(fresh))
    assert (await store.lifetime_usage_by_class(t.id))[0] == 9


async def test_a_non_mechanical_send_back_after_pass_still_caps_the_gate(store):
    """Regression for the review-1 finding: freezing on a bare
    `latest_review_verdict == 1` is unsound, because `_check_approval_pr_comments`
    (operator reject) / `_check_pr_ci` / `_check_ci_gate_integration` resume the
    task to IMPLEMENTING for REAL, attempt-consuming rework without themselves
    recording a new verdict — the stale PASS from the round that already
    landed would otherwise still read as "latest" and freeze the gate for
    genuine rework too. At the cap, with the newest verdict still a PASS but
    the round now IMPLEMENTING on a non-mechanical (pr_comment) send-back,
    both the gate and its advisory twin must re-enforce the cap."""
    t = await _approval_task(store)
    await _spend(store, t.id, attempts=9, last_verdict=1)  # at cap, newest is PASS
    await store.append_context_list(t.id, "send_back_feedback", {
        "source": "pr_comment", "author": "operator",
        "message": "reject: please redo the approach",
    })
    # Mirrors what `wake._resume` actually does before the loop head calls
    # this gate: flip the task to IMPLEMENTING.
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    fresh = await store.get_task(t.id)

    orch = _orch(store)
    assert await orch._mechanical_round(fresh) is False

    b = await orch._check_lifetime_budget(fresh)
    assert b is not None
    assert b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert await orch._at_lifetime_ceiling(fresh) is True


async def test_the_cap_still_fires_on_a_task_with_no_pass(store):
    """Known-negative: proves the freeze is scoped and the existing gate is
    intact for a task that never recorded a PASS."""
    t = Task.new("no-pass", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "attempts 9/9" in b.root_cause_hypothesis


async def test_a_later_review_fail_re_arms_the_cap(store):
    """Freeze bound: a PASS followed by a NEWER FAIL restores enforcement.

    Every row is closed with a real terminal status and nonzero tokens (see
    `_spend`'s docstring above for why a bare `create_attempt` loop would
    instead collide with the 2026-08-20 dead-interrupted-row exclusion)."""
    t = Task.new("pass-then-fail", repo_path="/tmp/x")
    await store.create_task(t)
    for n in range(1, 8):
        aid = await store.create_attempt(t.id, n)
        await store.update_attempt(aid, status="succeeded", tokens_used=1_000)
    aid8 = await store.create_attempt(t.id, 8)
    await store.update_attempt(
        aid8, status="succeeded", tokens_used=1_000, review_passed=1)
    aid9 = await store.create_attempt(t.id, 9)
    await store.update_attempt(
        aid9, status="failed", tokens_used=1_000, review_passed=0)

    assert await store.latest_review_verdict(t.id) == 0

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert b.category is BlockerCategory.BUDGET_EXHAUSTED


async def test_mechanical_rounds_still_count_their_tokens(store):
    """Accounting honesty: a mechanical round's real spend is still money —
    only the attempt tally is spared, and only from the GATED (included) sum:
    the unified predicate (`Store._lifetime_included_sql`) that governs the
    attempt count now governs the token sums too (no second, independently-
    typed definition), so a mechanical round's tokens move to the EXCLUDED
    bucket, not to `by_class["tokens_used"]` — the same shape as an
    infra-classified or dead-interrupted row. The all-in raw total
    (`lifetime_usage`) still reconstructs from included + excluded, so every
    other surface (`nh status`, the drawer) keeps seeing this spend; only the
    lifetime-budget CAP is spared it. Stamps `mechanical=True` directly
    (rather than through `_mechanical_round`) since this test is about what
    the chokepoint DOES with the flag, not about the predicate that sets it —
    that is covered by the other tests in this file."""
    t = await _approval_task(store)
    await _spend(store, t.id, attempts=8, last_verdict=1)

    mech_id = await store.create_attempt(t.id, 9, mechanical=True)
    await store.update_attempt(mech_id, tokens_used=500_000)

    attempts, by_class, excluded = await store.lifetime_usage_by_class(t.id)
    assert attempts == 8, "the mechanical attempt must not count toward the cap"
    assert by_class["tokens_used"] < 500_000, (
        "the mechanical attempt's tokens must not count toward the GATED sum")
    assert excluded["tokens_used"] >= 500_000, (
        "the mechanical attempt's real spend must still be reported, as excluded")

    total_attempts, total_tokens = await store.lifetime_usage(t.id)
    assert total_attempts == 8
    assert total_tokens >= 500_000, (
        "the all-in raw total must still reflect the mechanical spend")
