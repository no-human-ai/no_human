"""GET /api/search — cross-task full-text search over the failure/fix record
(events_fts, C3-G4). Advisory recall surfaced to the operator; hostile input
(FTS5 operators, bad quotes) must never 500 — it returns [] on any FTS error.
"""
from __future__ import annotations

import json

import pytest_asyncio

from no_human.api.app import app
from no_human.core.task import Task


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from httpx import ASGITransport, AsyncClient
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


_TS = [0]


async def _seed_event(store, task, kind, text):
    # Only the failure/fix kinds land in events_fts (migration 0006 trigger).
    _TS[0] += 1
    await store.save_events(task.id, [{"kind": kind, "text": text, "ts": _TS[0]}])


async def test_search_finds_a_matching_failure_event(client, store):
    t = Task.new("build widget", repo_path="/tmp/r")
    await store.create_task(t)
    await _seed_event(store, t, "attempt_failed",
                      "ImportError: cannot import name clampAgentState from cost")
    await _seed_event(store, t, "review", "the migration is missing a down step")

    r = await client.get("/api/search", params={"q": "ImportError clampAgentState"})
    assert r.status_code == 200
    hits = r.json()
    assert len(hits) >= 1
    top = hits[0]
    assert top["task_id"].startswith(t.id[:8])
    assert top["task_title"] == "build widget"
    assert top["kind"] == "attempt_failed"
    assert "clampAgentState" in top["snippet"]


async def test_non_indexed_event_kinds_are_not_searchable(client, store):
    """Only the failure/review record is indexed; ordinary progress text is not."""
    t = Task.new("t", repo_path="/tmp/r")
    await store.create_task(t)
    await _seed_event(store, t, "agent_text", "quokka zebra unique-token-xyz")
    r = await client.get("/api/search", params={"q": "unique-token-xyz"})
    assert r.status_code == 200
    assert r.json() == []


async def test_hostile_fts_input_never_500s(client, store):
    t = Task.new("t", repo_path="/tmp/r")
    await store.create_task(t)
    await _seed_event(store, t, "tamper", "assertions dropped 10 -> 2")
    for q in ['"', 'NEAR(', 'a AND OR b', '*', ')(', '""""', "foo\\"]:
        r = await client.get("/api/search", params={"q": q})
        assert r.status_code == 200, f"{q!r} -> {r.status_code}"
        assert isinstance(r.json(), list)


async def test_empty_query_is_a_400_or_empty(client):
    r = await client.get("/api/search", params={"q": "   "})
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert r.json() == []


async def test_results_are_bounded(client, store):
    t = Task.new("t", repo_path="/tmp/r")
    await store.create_task(t)
    for i in range(60):
        await _seed_event(store, t, "attempt_failed", f"widget failure number {i}")
    r = await client.get("/api/search", params={"q": "widget failure"})
    assert r.status_code == 200
    assert len(r.json()) <= 30
