"""AC5 (amended): the PR body itself must state, using a real query-derived
number, how many past attempts have hit the "diverged pushed tip" delivery
refusal failure class -- not only the escalation text
(`tests/test_diverged_delivery_escalation.py::
test_the_escalation_states_a_query_derived_historical_count` already pins
that half; this file pins the other half the amendment specifically added).

`Orchestrator._pr_body` is synchronous and `Store.count_attempts_failing_
like` is async, so `_finalize` runs the query on its OWN async path and
threads the already-computed int into `_pr_body` as `history_count` --
`_pr_body` (and its `_failure_class_history_note` helper) never touch
`self.store` themselves. A query failure degrades the note to absent, never
blocks delivery.

Two tests, matching the human's exact amendment wording:
  * `test_pr_body_states_a_given_count_verbatim` -- calls `_pr_body` directly
    with a known `history_count` and asserts the rendered body states it
    (and that leaving it out omits the note entirely, rather than e.g.
    printing "None" or a zero it never measured).
  * `test_the_production_path_threads_the_queried_count_into_the_pr_body` --
    seeds real `failure_reason` rows into the store, drives a REAL,
    successful `_finalize` delivery (`open_pr` mocked only to capture the
    body it would have opened, exactly like `tests/test_merge_policy_
    wiring.py` already does for its own PR-body assertions), and asserts
    the exact seeded count appears in the body. It then breaks `Store.
    count_attempts_failing_like` and re-runs the same delivery, proving the
    number in the body tracks this call's LIVE return value rather than a
    value baked in some other way -- this test fails if `_finalize` stops
    calling the query (the note would stay stuck at the old count, or the
    test's second assertion -- that the note vanishes once the query is
    broken -- would fail outright).
"""
from __future__ import annotations

import pytest

import no_human.core.db as db_mod
import no_human.core.orchestrator as orch_mod
from no_human.core.task import Task
from tests.test_failure_reason_history_query import ANCESTOR_PATTERN, _seed_attempt
from tests.test_merge_policy_wiring import (  # noqa: F401
    _Commit,
    _FakePR,
    _finalize_task,
    _git,
    _landed_receipt,
    _orch,
    _repo_with_a_commit,
    _Result,
    _stamp,
)


@pytest.fixture(autouse=True)
def _no_real_ci_rollup_network_calls(monkeypatch):
    """Same offline default as `test_merge_policy_wiring.py`'s fixture of the
    same name -- `_finalize`'s end-to-end delivery test here also reaches
    `_stamp_delivered_ci_status`, which otherwise shells out to `gh`."""
    async def _default(pr_url):
        return None, ()

    monkeypatch.setattr(orch_mod.ci_rollup, "fetch_ci_rollup", _default)


def test_pr_body_states_a_given_count_verbatim(store, tmp_path):
    orch = _orch(store, tmp_path)
    task = Task.new("Fix the thing", repo_path="/r")

    body = orch._pr_body(task, _Commit(), _Result(), history_count=4)
    assert "4 attempt(s)" in body, body
    assert "count_attempts_failing_like" in body, body

    # `None` (query never ran / failed) must omit the note outright -- never
    # invent a "0" the query never actually returned.
    body_none = orch._pr_body(task, _Commit(), _Result(), history_count=None)
    assert "attempt(s)" not in body_none, body_none


async def test_the_production_path_threads_the_queried_count_into_the_pr_body(
    store, tmp_path, monkeypatch,
):
    # Three genuine hits of the exact failure class, seeded the way a live
    # deployment's own attempt history would accumulate them.
    for i in range(3):
        await _seed_attempt(
            store, title=f"prior-{i}",
            failure_reason=(
                f"delivery refused: branch no-human/prior-{i} remote tip "
                f"aaa{i} (fetched) is not an ancestor of the reviewed sha "
                f"bbb{i} (human_gated_resume=False)"))
    # A wholly unrelated failure -- must not be counted.
    await _seed_attempt(
        store, title="unrelated", failure_reason="budget exhausted after 40 turns")

    assert await store.count_attempts_failing_like(ANCESTOR_PATTERN) == 3, (
        "test setup sanity check: the seeded rows must match the same "
        "pattern `_finalize` queries with"
    )

    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha)

    bodies: list[str] = []

    def fake_open_pr(repo, branch, title, body, **kw):
        bodies.append(body)
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    _orch1, _task1, _attempt_id1, _out1 = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch)

    assert bodies, "open_pr was never called"
    assert "3 attempt(s)" in bodies[0], bodies[0]

    # Break the query itself: the number in the body must track this call's
    # LIVE result, not a value cached or computed some other way. If
    # `_finalize` ever stops calling `count_attempts_failing_like` (e.g. the
    # wiring added for this fix is removed), this second delivery would
    # still show "3 attempt(s)" from before, and the assertion below fails.
    async def _boom(self, pattern):
        raise RuntimeError("query broken")

    monkeypatch.setattr(db_mod.Store, "count_attempts_failing_like", _boom)

    bodies.clear()
    _orch2, _task2, _attempt_id2, out2 = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch)

    assert bodies, "open_pr was never called"
    assert "attempt(s)" not in bodies[0], bodies[0]
    assert out2.status == orch_mod.TaskStatus.AWAITING_APPROVAL, out2.detail
