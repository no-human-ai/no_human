"""POST /api/tasks — `follows_id` (Task 7), over real HTTP.

Mirrors tests/test_api.py's `client`/`store` fixtures (no origin guard on this
route, same as its existing external_id/mcp-source tests just above the spot
this file extends).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.core.task import Task

pytestmark = pytest.mark.usefixtures("isolated_env_file")


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


@pytest.mark.asyncio
async def test_create_task_bogus_follows_id_is_404(client):
    r = await client.post("/api/tasks", json={
        "title": "Follow-up on nothing",
        "follows_id": "not-a-real-task-id",
    })
    assert r.status_code == 404
    assert "not-a-real-task-id" in r.json()["detail"]


@pytest.mark.asyncio
async def test_create_task_real_follows_id_is_echoed_on_create_and_detail(client, store):
    predecessor = Task.new("original task", repo_path="/tmp/repo")
    await store.create_task(predecessor)

    r = await client.post("/api/tasks", json={
        "title": "Follow-up to the original",
        "follows_id": predecessor.id,
    })
    assert r.status_code == 201
    body = r.json()
    assert body["follows_id"] == predecessor.id

    # TaskOut (the detail endpoint) carries it too, and it never gets mistaken
    # for a compound sub-task of `predecessor` (parent_id is untouched).
    detail = await client.get(f"/api/tasks/{body['id']}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["follows_id"] == predecessor.id
    assert detail_body["parent_id"] is None

    task = await store.get_task(body["id"])
    assert task.follows_id == predecessor.id
    assert task.parent_id is None
    assert await store.count_subtasks(predecessor.id) == 0
