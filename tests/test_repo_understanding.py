"""GET /api/repo — surface what no_human understands about a repo (C3-G3).

Read-only: the cached repo map + the onboarded profile + matched playbooks, for a
repo the operator ALREADY knows. The security invariant: it must never build a map
for an arbitrary caller-supplied path (that would leak any filesystem tree), only
for a repo present in the profiles table or referenced by an existing task.
"""
from __future__ import annotations

import pytest_asyncio

from no_human.api.app import app
from no_human.core.task import Task, TaskStatus
from no_human.profile import ProjectProfile


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from httpx import ASGITransport, AsyncClient
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


def _make_repo(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def main():\n    return 1\n")
    (repo / "README.md").write_text("# myrepo\n")
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


async def test_repo_understanding_for_an_onboarded_repo(client, store, tmp_path):
    repo = _make_repo(tmp_path)
    await store.upsert_profile(ProjectProfile(
        repo_path=str(repo), ecosystem="python", test_cmd="pytest -q",
        proven={"test_cmd": True}, confirmed=True))
    await store.add_playbook(
        title="myrepo release", project=str(repo),
        procedure="do the thing", postconditions=["it works"])

    r = await client.get("/api/repo", params={"path": str(repo)})
    assert r.status_code == 200
    body = r.json()
    assert body["profile"]["ecosystem"] == "python"
    assert "app.py" in body["repo_map"]          # the cached/built map
    assert body["repo_map"]                        # non-empty
    assert any(p["title"] == "myrepo release" for p in body["playbooks"])


async def test_unknown_path_is_rejected_never_maps_arbitrary_fs(client, tmp_path):
    """The endpoint must not walk /etc (or any path the operator never onboarded)."""
    r = await client.get("/api/repo", params={"path": "/etc"})
    assert r.status_code == 404
    r = await client.get("/api/repo", params={"path": str(tmp_path / "not-a-repo")})
    assert r.status_code == 404


async def test_repo_known_via_task_even_without_profile(client, store, tmp_path):
    repo = _make_repo(tmp_path)
    t = Task.new("do", repo_path=str(repo))
    await store.create_task(t)
    r = await client.get("/api/repo", params={"path": str(repo)})
    assert r.status_code == 200
    body = r.json()
    assert body["profile"] is None                 # not onboarded, but known
    assert "app.py" in body["repo_map"]
    assert body["playbooks"] == []


async def test_missing_path_param_is_a_400(client):
    r = await client.get("/api/repo")
    assert r.status_code in (400, 422)


async def test_repos_index_lists_known_repos(client, store, tmp_path):
    """A companion index so the UI can populate its repo picker."""
    repo = _make_repo(tmp_path)
    await store.upsert_profile(ProjectProfile(
        repo_path=str(repo), ecosystem="python", test_cmd="pytest -q",
        proven={"test_cmd": True}, confirmed=True))
    r = await client.get("/api/repos")
    assert r.status_code == 200
    paths = [row["repo_path"] for row in r.json()]
    assert str(repo) in paths
