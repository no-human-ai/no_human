"""D2 #5: do mined playbooks actually pay? (pure SQL over existing records)"""

from __future__ import annotations


from no_human.core.metrics import playbook_outcomes
from no_human.core.task import Task, TaskStatus


async def _task_with_playbook(store, name: str, status: TaskStatus, tokens: int):
    t = Task.new(f"t-{name}-{status.value}", repo_path="/r")
    await store.create_task(t)
    await store.save_events(t.id, [{"kind": "playbook_accessed", "ts": 1,
                                   "text": f"applying playbook: {name}"}])
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, tokens_used=tokens, cache_read_tokens=0)
    event = {"source": "test", "kind": "test_seed"} if status is TaskStatus.DONE else None
    await store.set_status(t, status, validate=False, event=event)
    return t


async def test_playbook_outcomes_join_usage_to_result_and_burn(store):
    await _task_with_playbook(store, "good-pb", TaskStatus.AWAITING_APPROVAL, 100)
    await _task_with_playbook(store, "good-pb", TaskStatus.DONE, 200)
    await _task_with_playbook(store, "bad-pb", TaskStatus.ESCALATED, 5000)

    rows = {r["playbook"]: r for r in await playbook_outcomes(store)}

    good = rows["good-pb"]
    assert good["tasks"] == 2
    assert good["reached_gate"] == 2
    assert good["gate_rate"] == 1.0
    assert good["tokens_per_task"] == 150

    bad = rows["bad-pb"]
    assert bad["escalated_or_failed"] == 1
    assert bad["gate_rate"] == 0.0
    assert bad["tokens_per_task"] == 5000   # the liability is visible


async def test_an_operator_cancel_is_not_charged_to_the_playbook(store):
    """A cancel is a WITHDRAWAL, not a verdict on the playbook.

    `nh task cancel` and the board's cancel button both store `failed` plus a
    `cancel_reason` in context; this reproduces that exact write order. Counting
    it as `escalated_or_failed` blames the playbook for a human's decision to
    stop — on the author's store, 6 of one playbook's 31 recorded "failures".
    """
    await _task_with_playbook(store, "pb", TaskStatus.ESCALATED, 100)
    t = await _task_with_playbook(store, "pb", TaskStatus.PENDING, 100)
    await store.merge_context(t.id, {"cancel_reason": "operator stopped it"})
    await store.set_status(t, TaskStatus.FAILED, validate=False,
                           human_override=True)

    row = {r["playbook"]: r for r in await playbook_outcomes(store)}["pb"]
    assert row["tasks"] == 2
    assert row["escalated_or_failed"] == 1   # the escalation, and only it
    assert row["cancelled"] == 1


async def test_no_playbook_usage_yields_no_rows(store):
    t = Task.new("plain", repo_path="/r")
    await store.create_task(t)
    assert await playbook_outcomes(store) == []


async def test_metrics_endpoint_carries_by_playbook(store, tmp_path):
    from httpx import ASGITransport, AsyncClient

    from no_human.api.app import app
    from no_human.config import load_config

    await _task_with_playbook(store, "pb-x", TaskStatus.DONE, 10)
    app.state.store = store
    app.state.config = load_config(tmp_path / "c.yaml")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://localhost") as c:
        body = (await c.get("/api/metrics")).json()
    assert any(r["playbook"] == "pb-x" for r in body["by_playbook"])


async def test_metrics_surfaces_aux_planning_burn(store):
    """Review #2: /api/metrics must sum plan_*/utility_* or it under-counts by
    the whole planning slice while the bench counts it."""
    from no_human.core.metrics import compute_metrics
    from no_human.core.task import Task

    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, tokens_used=10, plan_tokens_used=500,
                               plan_cache_read_tokens=9000,
                               utility_tokens_used=50)
    m = await compute_metrics(store)
    assert m["aux_tokens_used_total"] == 550
    assert m["aux_cache_read_total"] == 9000
