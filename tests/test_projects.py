"""Tests for the projects feature: model, Store CRUD, and API endpoints."""
from __future__ import annotations

import json
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.project_model import Project
from no_human.core.db import Store
from no_human.api.app import app


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def client(store):
    app.state.store = store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


# --------------------------------------------------------------------------- #
# Project model unit tests                                                     #
# --------------------------------------------------------------------------- #

def test_project_new_sets_primary():
    p = Project.new("metrics-core", repo_paths=["/a", "/b"])
    assert p.primary_repo == "/a"
    assert p.linked_repos == ["/b"]


def test_project_new_empty():
    p = Project.new("empty")
    assert p.repo_paths == []
    assert p.primary_repo is None
    assert p.linked_repos == []


def test_project_to_row_roundtrip():
    p = Project.new("proj", repo_paths=["/x", "/y"], primary_repo="/x")
    row = p.to_row()
    assert json.loads(row["repo_paths"]) == ["/x", "/y"]
    restored = Project.from_row({**row, "created_at": None, "updated_at": None})
    assert restored.name == "proj"
    assert restored.repo_paths == ["/x", "/y"]
    assert restored.primary_repo == "/x"


def test_linked_repos_excludes_primary():
    p = Project.new("test", repo_paths=["/a", "/b", "/c"], primary_repo="/b")
    assert p.linked_repos == ["/a", "/c"]


# --------------------------------------------------------------------------- #
# Store CRUD tests                                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_and_get_project(store):
    p = Project.new("alpha", repo_paths=["/repo1"])
    await store.create_project(p)
    got = await store.get_project(p.id)
    assert got is not None
    assert got.name == "alpha"
    assert got.repo_paths == ["/repo1"]
    assert got.primary_repo == "/repo1"


@pytest.mark.asyncio
async def test_get_project_by_name(store):
    p = Project.new("beta", repo_paths=["/r1", "/r2"])
    await store.create_project(p)
    got = await store.get_project_by_name("beta")
    assert got is not None
    assert got.id == p.id


@pytest.mark.asyncio
async def test_list_projects_order(store):
    await store.create_project(Project.new("zzz"))
    await store.create_project(Project.new("aaa"))
    projects = await store.list_projects()
    assert [p.name for p in projects] == ["aaa", "zzz"]


@pytest.mark.asyncio
async def test_update_project(store):
    p = Project.new("gamma", repo_paths=["/old"])
    await store.create_project(p)
    p.repo_paths = ["/new1", "/new2"]
    p.primary_repo = "/new1"
    await store.update_project(p)
    got = await store.get_project(p.id)
    assert got.repo_paths == ["/new1", "/new2"]


@pytest.mark.asyncio
async def test_delete_project(store):
    p = Project.new("deleteme")
    await store.create_project(p)
    assert await store.delete_project(p.id)
    assert await store.get_project(p.id) is None


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false(store):
    assert not await store.delete_project("nonexistent_id")


@pytest.mark.asyncio
async def test_duplicate_name_raises(store):
    await store.create_project(Project.new("unique"))
    with pytest.raises(Exception, match="UNIQUE"):
        await store.create_project(Project.new("unique"))


# --------------------------------------------------------------------------- #
# API endpoint tests                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_api_list_projects_empty(client):
    r = await client.get("/api/projects")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_api_create_and_list(client, tmp_path):
    # Create a fake git repo for validation.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    r = await client.post("/api/projects", json={
        "name": "testproj",
        "repo_paths": [str(repo)],
    })
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "testproj"
    assert len(data["repo_paths"]) == 1
    assert data["primary_repo"] == str(repo)

    # List should now have one project.
    r = await client.get("/api/projects")
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_api_create_project_persists_explicit_primary(client, store, tmp_path):
    """A primary_repo sent by the client must reach the projects table.

    BUG (first external DMG tester): "one repo should be the default but
    configurable". The column and the reader both existed — db.py's INSERT lists
    primary_repo, TaskComposer.jsx reads `project.primary_repo` — but onboarding
    posted {name, repo_paths} only, so the server fell back to repo_paths[0]:
    whichever repo the user happened to tick first. Onboarding now sends a chosen
    primary (web/src/onboardingProjects.js).

    Asserted where it is stored, not where it is echoed: a response body can be
    right while the INSERT drops the column. The chosen repo is deliberately NOT
    repo_paths[0], so the pre-existing fallback cannot produce this answer.
    """
    repos = []
    for name in ("first", "second"):
        r = tmp_path / name
        r.mkdir()
        (r / ".git").mkdir()
        repos.append(str(r))

    resp = await client.post("/api/projects", json={
        "name": "chosen-primary",
        "repo_paths": repos,
        "primary_repo": repos[1],
    })
    assert resp.status_code == 201, resp.text

    # 1. The store's own read path.
    proj = await store.get_project_by_name("chosen-primary")
    assert proj is not None
    assert proj.primary_repo == repos[1]
    assert proj.linked_repos == [repos[0]]

    # 2. The column itself, straight out of SQLite — so a model-level default
    #    cannot stand in for a value that was never written.
    row = await store._fetchone(
        "SELECT primary_repo FROM projects WHERE name = ?", ("chosen-primary",)
    )
    assert dict(row)["primary_repo"] == repos[1]


@pytest.mark.asyncio
async def test_api_create_project_defaults_primary_when_client_sends_none(
    client, store, tmp_path
):
    """Omitting primary_repo still stores one — the fallback stays intact."""
    repos = []
    for name in ("alpha", "beta"):
        r = tmp_path / name
        r.mkdir()
        (r / ".git").mkdir()
        repos.append(str(r))

    resp = await client.post("/api/projects", json={
        "name": "defaulted-primary", "repo_paths": repos,
    })
    assert resp.status_code == 201, resp.text
    proj = await store.get_project_by_name("defaulted-primary")
    assert proj.primary_repo == repos[0]


@pytest.mark.asyncio
async def test_api_create_duplicate_409(client, store):
    await store.create_project(Project.new("dup"))
    r = await client.post("/api/projects", json={"name": "dup"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_api_get_project(client, store):
    p = Project.new("getme", repo_paths=[])
    await store.create_project(p)
    r = await client.get(f"/api/projects/{p.id}")
    assert r.status_code == 200
    assert r.json()["name"] == "getme"


@pytest.mark.asyncio
async def test_api_get_project_404(client):
    r = await client.get("/api/projects/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_update_project(client, store, tmp_path):
    p = Project.new("updme")
    await store.create_project(p)
    repo = tmp_path / "newrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    r = await client.put(f"/api/projects/{p.id}", json={
        "name": "updated",
        "repo_paths": [str(repo)],
    })
    assert r.status_code == 200
    assert r.json()["name"] == "updated"
    assert r.json()["primary_repo"] == str(repo)


@pytest.mark.asyncio
async def test_api_delete_project(client, store):
    p = Project.new("delme")
    await store.create_project(p)
    r = await client.delete(f"/api/projects/{p.id}")
    assert r.status_code == 200
    assert r.json()["ok"]
    # Gone.
    r = await client.get(f"/api/projects/{p.id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_create_task_with_project(client, store, tmp_path):
    """Creating a task via project_id should set repo_path + linked_repos."""
    repo1 = tmp_path / "r1"
    repo1.mkdir()
    (repo1 / ".git").mkdir()
    repo2 = tmp_path / "r2"
    repo2.mkdir()
    (repo2 / ".git").mkdir()
    p = Project.new("multi", repo_paths=[str(repo1), str(repo2)])
    await store.create_project(p)

    r = await client.post("/api/tasks", json={
        "title": "Test multi-repo",
        "project_id": p.id,
    })
    assert r.status_code == 201
    task_data = r.json()
    # TaskSummaryOut exposes repo_name (basename), not repo_path.
    assert task_data["repo_name"] == "r1"

    # Verify repo_path and linked_repos on the actual task in the DB.
    tasks = await store.list_tasks()
    t = tasks[0]
    assert t.repo_path == str(repo1)
    assert t.linked_repos == [str(repo2)]


@pytest.mark.asyncio
async def test_api_create_task_project_not_found(client):
    r = await client.post("/api/tasks", json={
        "title": "Bad project",
        "project_id": "nonexistent",
    })
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# PR5: Test-plan API tests                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_api_project_out_includes_test_layers(client, store):
    """ProjectOut includes test_layers (empty by default)."""
    p = Project.new("layered")
    await store.create_project(p)
    r = await client.get(f"/api/projects/{p.id}")
    assert r.status_code == 200
    assert r.json()["test_layers"] == []


@pytest.mark.asyncio
async def test_api_update_project_test_layers(client, store):
    """PUT /api/projects/{id} with test_layers saves and returns them."""
    p = Project.new("layered2")
    await store.create_project(p)

    layers = [
        {"name": "unit", "command": "pytest -q", "gating": "blocking"},
        {"name": "e2e", "command": "make e2e", "gating": "advisory", "depends_on": ["unit"]},
    ]
    r = await client.put(f"/api/projects/{p.id}", json={"test_layers": layers})
    assert r.status_code == 200
    data = r.json()
    assert len(data["test_layers"]) == 2
    assert data["test_layers"][0]["name"] == "unit"
    assert data["test_layers"][1]["gating"] == "advisory"

    # Verify on re-fetch.
    r = await client.get(f"/api/projects/{p.id}")
    assert len(r.json()["test_layers"]) == 2


@pytest.mark.asyncio
async def test_api_update_project_test_layers_validation(client, store):
    """Invalid test layers are rejected with 422."""
    p = Project.new("badlayers")
    await store.create_project(p)

    r = await client.put(f"/api/projects/{p.id}", json={
        "test_layers": [{"name": "bad", "gating": "invalid_gating"}],
    })
    assert r.status_code == 422


