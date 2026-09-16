"""AC5: the PR body for the pushed-tip-divergence fix must state how many
historical attempts hit this failure class, using a query over recorded
failure reasons — not an invented or estimated number.

`Store.count_attempts_failing_like` / `Store.failure_reason_class_counts`
(in `core/db.py`) are that query interface: `failure_reason` already carries
the exact message `_reconcile_remote_branch` raises (see
`_finalize`'s `ReviewedShaMismatch` handling), so a failure class can be
counted retroactively via SQL `LIKE`, exactly like `_recently_failed_reason`
already matches on non-empty content. This file proves the query counts
only the rows that actually belong to the class — a lookalike substring, an
unrelated reason, and NULL/empty reasons must all be excluded — against a
real sqlite-backed `Store`, the same one a live deployment would query.
"""
from __future__ import annotations

import pytest

from no_human.core.task import Task

#: The exact wording `_reconcile_remote_branch` raises (verbatim, unchanged
#: by this fix — see `tests/test_diverged_delivery_escalation.py`).
ANCESTOR_PATTERN = "%is not an ancestor of the reviewed sha%"


async def _seed_attempt(store, *, failure_reason, title="t"):
    task = Task.new(title, repo_path="/tmp/x")
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)
    if failure_reason is None:
        # Leave the row's failure_reason untouched (NULL) — a still
        # in-progress or otherwise reason-less row.
        return attempt_id
    await store.update_attempt(
        attempt_id, status="failed", failure_reason=failure_reason)
    return attempt_id


@pytest.mark.asyncio
async def test_counts_only_rows_whose_failure_reason_matches_the_class(store):
    # Three real hits — the exact refusal string, for three different
    # branches/shas, matching the reported 1cbc1c65/7606f734/af1602af shape.
    await _seed_attempt(
        store, title="t-1cbc1c65",
        failure_reason=(
            "delivery refused: branch no-human/1cbc1c65-1 remote tip aaa111 "
            "(fetched) is not an ancestor of the reviewed sha bbb222 "
            "(human_gated_resume=False)"))
    await _seed_attempt(
        store, title="t-7606f734",
        failure_reason=(
            "delivery refused: branch no-human/7606f734-2 remote tip ccc333 "
            "(fetched) is not an ancestor of the reviewed sha ddd444 "
            "(human_gated_resume=False)"))
    await _seed_attempt(
        store, title="t-af1602af",
        failure_reason=(
            "delivery refused: branch no-human/af1602af-1 remote tip eee555 "
            "(fetched) is not an ancestor of the reviewed sha fff666 "
            "(human_gated_resume=True)"))

    # A lookalike: mentions "ancestor" and "delivery refused" but is NOT the
    # same failure class (a different guard's message) — must be excluded.
    await _seed_attempt(
        store, title="t-lookalike",
        failure_reason=(
            "delivery refused: could not fast-forward no-human/x to "
            "reviewed sha zzz999: some ancestor lookup failed"))

    # Unrelated failure reasons — must be excluded.
    await _seed_attempt(store, title="t-unrelated-1", failure_reason="review failed: 2 blocking findings")
    await _seed_attempt(store, title="t-unrelated-2", failure_reason="budget exhausted after 40 turns")

    # Empty-string and NULL reasons — must be excluded, not counted as 0
    # matches that accidentally pass a loose LIKE.
    await _seed_attempt(store, title="t-empty", failure_reason="")
    await _seed_attempt(store, title="t-null", failure_reason=None)

    n = await store.count_attempts_failing_like(ANCESTOR_PATTERN)
    assert n == 3, "must count exactly the three genuine ancestor-refusal rows"

    counts = await store.failure_reason_class_counts({
        "diverged_pushed_branch": ANCESTOR_PATTERN,
        "budget_exhausted": "%budget exhausted%",
        "never_happened": "%no such failure ever recorded%",
    })
    assert counts == {
        "diverged_pushed_branch": 3,
        "budget_exhausted": 1,
        "never_happened": 0,
    }


@pytest.mark.asyncio
async def test_a_fresh_store_with_no_history_counts_zero(store):
    assert await store.count_attempts_failing_like(ANCESTOR_PATTERN) == 0
