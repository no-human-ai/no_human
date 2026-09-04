"""GET/PUT /api/config/workers — the Settings worker-count row's wire contract.

Thin wrappers over `config.set_concurrency` (unit-tested in
test_config_workers.py); these cover the shapes, the 422 refusals, the
file-vs-process `restart_required` flag, and that a write does NOT reload
`app.state.config` (the pool size is bound at server start, so the change
lands on the next `nh serve`).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import no_human.config as nh_config
from no_human.api.app import app

pytestmark = pytest.mark.usefixtures("isolated_env_file")


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    assert str(nh_config.CONFIG_PATH).startswith(str(tmp_path))
    app.state.store = store
    app.state.config = nh_config.load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost",
                           headers={"Origin": "http://127.0.0.1:8420"}) as c:
        yield c


@pytest.mark.asyncio
async def test_get_returns_the_shipped_defaults(client):
    r = await client.get("/api/config/workers")
    assert r.status_code == 200
    b = r.json()
    assert b["max_workers"] == 2 and b["enabled"] is False
    assert b["max_allowed"] == 64
    assert b["restart_required"] is False  # disk matches the running process


@pytest.mark.asyncio
async def test_put_sets_both_and_flags_restart(client):
    r = await client.put("/api/config/workers", json={"max_workers": 8, "enabled": True})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["max_workers"] == 8 and b["enabled"] is True
    # app.state.config is NOT reloaded, so the running pool still reads 2/false.
    assert b["restart_required"] is True
    # And it really landed on disk.
    r2 = await client.get("/api/config/workers")
    assert r2.json()["max_workers"] == 8


@pytest.mark.asyncio
async def test_effective_is_clamped_when_concurrency_disabled(client):
    # enabled stays false; resolve_max_workers refuses >1 and says why.
    r = await client.put("/api/config/workers", json={"max_workers": 8})
    b = r.json()
    assert b["max_workers"] == 8            # what is written
    assert b["effective_max_workers"] == 1  # what would actually run
    assert b["warning"] and "concurrency.enabled" in b["warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, 65, 1000, -3])
async def test_put_out_of_range_is_422(client, bad):
    r = await client.put("/api/config/workers", json={"max_workers": bad})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_bool_max_workers_is_422(client):
    r = await client.put("/api/config/workers", json={"max_workers": True})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_empty_body_is_422(client):
    r = await client.put("/api/config/workers", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_non_object_body_is_422(client):
    r = await client.put("/api/config/workers", json=["nope"])
    assert r.status_code == 422
