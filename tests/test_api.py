"""FastAPI board endpoint tests (Phase 4 DoD)."""
from __future__ import annotations

import importlib
import json
import logging
import subprocess

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.api.app import app
from no_human.core.build_info import LoadedCode
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.profile import ProjectProfile

# Tests here reach ``config.load_env_var``, which reads the operator's real
# ``~/.no_human/.env`` BEFORE the process env. Requested by NAME through
# `usefixtures` — never an autouse marker; see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


async def _seed_task(store: Store, *, status=TaskStatus.PENDING, title="Fix thing") -> Task:
    t = Task.new(title, repo_path="/tmp/repo")
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    if status != TaskStatus.PENDING:
        if status is TaskStatus.DONE:
            await store.set_status(t, status, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
        else:
            await store.set_status(t, status, validate=False)
    return t


# --------------------------------------------------------------------------- #
# GET /api/tasks                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_list_tasks_empty(client):
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_tasks_returns_summary(client, store):
    await _seed_task(store, title="Alpha")
    await _seed_task(store, title="Beta", status=TaskStatus.IMPLEMENTING)
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    titles = {t["title"] for t in data}
    assert titles == {"Alpha", "Beta"}
    # summary shape — no attempts field
    for item in data:
        assert "id" in item
        assert "status" in item
        assert "attempts" not in item


@pytest.mark.asyncio
async def test_list_tasks_carries_pr_url_for_done_tasks(client, store):
    """Operator finding (demo walk): the board card is the way BACK to a
    finished ticket's PR — the list payload must carry pr_url (latest
    attempt's) for DONE tasks, or the card has nothing to link."""
    t = await _seed_task(store, status=TaskStatus.DONE)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, pr_url="https://github.com/o/r/pull/42")
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    (item,) = r.json()
    assert item["pr_url"] == "https://github.com/o/r/pull/42"


@pytest.mark.asyncio
async def test_list_tasks_status_values(client, store):
    await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.get("/api/tasks")
    assert r.json()[0]["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_board_tasks_are_newest_first(client, store):
    """Pins `_board_tasks` (app.py) — the board surface the acceptance
    criterion names — to newest-first, independent of the scheduler's
    (oldest-first) claim order fixed in the same change."""
    oldest = Task.new("oldest", repo_path="/tmp/repo")
    oldest.acceptance_criteria = ["Should work"]
    oldest.created_at = "2026-08-01T08:00:00+00:00"
    await store.create_task(oldest)
    middle = Task.new("middle", repo_path="/tmp/repo")
    middle.acceptance_criteria = ["Should work"]
    middle.created_at = "2026-08-05T08:00:00+00:00"
    await store.create_task(middle)
    newest = Task.new("newest", repo_path="/tmp/repo")
    newest.acceptance_criteria = ["Should work"]
    newest.created_at = "2026-08-10T08:00:00+00:00"
    await store.create_task(newest)

    r = await client.get("/api/tasks")
    assert r.status_code == 200
    ids = [t["id"] for t in r.json()]
    assert ids == [newest.id, middle.id, oldest.id]


@pytest.mark.asyncio
async def test_list_tasks_survives_naive_updated_at(client, store):
    """SCRUM-57: a single row with a naive updated_at (the 2026-07-26 incident
    shape) must not 500 the whole board — it renders with wall_seconds
    degraded to a real (naive-treated-as-UTC) value, not a crash."""
    await _seed_task(store, title="Healthy")
    corrupt = await _seed_task(store, title="Corrupt")
    await store._db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("2026-07-26 06:11:56", corrupt.id),
    )
    await store._db.commit()

    r = await client.get("/api/tasks")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert {"Healthy", "Corrupt"} <= {t["title"] for t in data}

    corrupt_card = next(t for t in data if t["title"] == "Corrupt")
    assert isinstance(corrupt_card["wall_seconds"], (int, float))
    assert corrupt_card["wall_seconds"] >= 0


# --------------------------------------------------------------------------- #
# P5: GET /api/tasks?limit/offset                                             #
# --------------------------------------------------------------------------- #

async def _seed_n_tasks(store: Store, n: int) -> list[str]:
    """Newest-first ids to match `_board_tasks`'s ORDER BY created_at DESC."""
    ids = []
    for i in range(n):
        t = Task.new(f"fleet-{i:04d}", repo_path="/tmp/repo")
        t.acceptance_criteria = ["Should work"]
        t.created_at = f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"
        await store.create_task(t)
        ids.append(t.id)
    ids.reverse()
    return ids


@pytest.mark.asyncio
async def test_list_tasks_no_params_stays_the_full_unpaginated_list(client, store):
    """Backward compatibility: every existing consumer (web `fetchTasks`, the
    CLI's `board()`/`ping()`, the desktop shell's `probe()`) calls this with
    NO query params and must keep getting every row, exactly as before."""
    ids = await _seed_n_tasks(store, 25)
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    assert [t["id"] for t in r.json()] == ids


@pytest.mark.asyncio
async def test_list_tasks_limit_offset_paginate_newest_first(client, store):
    ids = await _seed_n_tasks(store, 10)
    r = await client.get("/api/tasks", params={"limit": 3, "offset": 2})
    assert r.status_code == 200
    assert [t["id"] for t in r.json()] == ids[2:5]


@pytest.mark.asyncio
async def test_list_tasks_limit_is_pushed_down_not_sliced_in_python(client, store, monkeypatch):
    """A page must not still pay to hydrate/serialize the whole table — the
    handler has to pass limit/offset into the store query, not fetch
    everything and slice the result."""
    await _seed_n_tasks(store, 10)
    calls = []
    real_list_tasks = store.list_tasks

    async def spy(*args, **kwargs):
        calls.append(kwargs)
        return await real_list_tasks(*args, **kwargs)

    monkeypatch.setattr(store, "list_tasks", spy)
    r = await client.get("/api/tasks", params={"limit": 4})
    assert r.status_code == 200
    assert len(r.json()) == 4
    assert calls and calls[-1].get("limit") == 4


@pytest.mark.asyncio
async def test_list_tasks_rejects_invalid_pagination_params(client, store):
    await _seed_n_tasks(store, 3)
    assert (await client.get("/api/tasks", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/tasks", params={"offset": -1})).status_code == 422


@pytest.mark.asyncio
async def test_list_tasks_fleet_scale_800_rows_pagination_shrinks_the_payload(client, store):
    """Fleet finding 6468d631: 700+ rows on the production fleet. Seeds 800
    (the same order of magnitude) and MEASURES the win: a paginated page must
    be a small fraction of the full-list response's bytes, and every row
    must still be reachable through paging (no task silently dropped)."""
    ids = await _seed_n_tasks(store, 800)

    full = await client.get("/api/tasks")
    assert full.status_code == 200
    full_body = full.content
    assert len(full.json()) == 800

    page = await client.get("/api/tasks", params={"limit": 50})
    assert page.status_code == 200
    page_body = page.content
    assert [t["id"] for t in page.json()] == ids[:50]

    # The measured win: a 50-row page is roughly limit/total of the full
    # payload's bytes (~6.25% here), never anywhere close to the full size.
    ratio = len(page_body) / len(full_body)
    assert ratio < 0.15, (
        f"800-row baseline {len(full_body)} bytes; 50-row page "
        f"{len(page_body)} bytes ({ratio:.1%}) — pagination did not shrink "
        "the payload as expected"
    )

    # Paging through recovers every row exactly once — deletions/rewrites
    # aside, a client that pages the FULL range sees the same rows a full
    # fetch would, in the same order.
    paged_ids = []
    offset = 0
    while offset < 800:
        r = await client.get("/api/tasks", params={"limit": 100, "offset": offset})
        batch = r.json()
        assert len(batch) == min(100, 800 - offset)
        paged_ids.extend(t["id"] for t in batch)
        offset += 100
    assert paged_ids == ids


@pytest.mark.asyncio
async def test_merge_ready_field_reads_the_verdict_for_the_current_head(client, store):
    """`TaskSummaryOut.merge_ready` reads `task.context.merge_policy[<latest
    attempt's commit_sha>].ready` — not any older sha's verdict, and not
    `None` misread as `False`."""
    t = await _seed_task(store, title="Ready one")
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, commit_sha="c" * 40)
    t.context = await store.merge_context(t.id, {
        "merge_policy": {"c" * 40: {"ready": True, "summary": "ready — 1 of 1 rules satisfied"}}})
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    (item,) = r.json()
    assert item["merge_ready"] is True


@pytest.mark.asyncio
async def test_merge_ready_field_is_none_for_a_different_sha(client, store):
    """A verdict stamped for an OLDER commit must not read as ready for the
    sha sitting in the attempt row now."""
    t = await _seed_task(store, title="Stale verdict")
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, commit_sha="d" * 40)
    t.context = await store.merge_context(t.id, {
        "merge_policy": {"e" * 40: {"ready": True, "summary": "ready — 1 of 1 rules satisfied"}}})
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    (item,) = r.json()
    assert item["merge_ready"] is None


@pytest.mark.asyncio
async def test_merge_ready_field_is_none_when_never_evaluated(client, store):
    t = await _seed_task(store, title="No verdict yet")
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, commit_sha="f" * 40)
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    (item,) = r.json()
    assert item["merge_ready"] is None


@pytest.mark.asyncio
async def test_merge_ready_query_filter_returns_only_ready_tasks(client, store):
    """`?merge_ready=1` is a truthy-only filter: not-ready and
    never-evaluated tasks are both excluded, and the unfiltered list still
    carries all three."""
    ready = await _seed_task(store, title="Ready")
    aid_ready = await store.create_attempt(ready.id, 1)
    await store.update_attempt(aid_ready, commit_sha="1" * 40)
    ready.context = await store.merge_context(ready.id, {
        "merge_policy": {"1" * 40: {"ready": True, "summary": "ready — 1 of 1 rules satisfied"}}})

    not_ready = await _seed_task(store, title="Not ready")
    aid_not_ready = await store.create_attempt(not_ready.id, 1)
    await store.update_attempt(aid_not_ready, commit_sha="2" * 40)
    not_ready.context = await store.merge_context(not_ready.id, {
        "merge_policy": {"2" * 40: {"ready": False, "summary": "not ready — 1 of 1 rules failed: tests_ran_and_passed"}}})

    never_evaluated = await _seed_task(store, title="Never evaluated")
    aid_never = await store.create_attempt(never_evaluated.id, 1)
    await store.update_attempt(aid_never, commit_sha="3" * 40)

    r = await client.get("/api/tasks", params={"merge_ready": 1})
    assert r.status_code == 200
    titles = {t["title"] for t in r.json()}
    assert titles == {"Ready"}

    r_all = await client.get("/api/tasks")
    assert r_all.status_code == 200
    assert {t["title"] for t in r_all.json()} == {"Ready", "Not ready", "Never evaluated"}


def test_wall_seconds_naive_treated_as_utc():
    from no_human.api.models import _wall_seconds

    assert _wall_seconds("2026-07-26T06:00:00+00:00", "2026-07-26 06:11:56") == 716.0


# --------------------------------------------------------------------------- #
# GET /api/tasks/{id}                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_task_detail(client, store):
    t = await _seed_task(store)
    r = await client.get(f"/api/tasks/{t.id}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == t.id
    assert data["title"] == t.title
    assert data["acceptance_criteria"] == ["Should work"]
    assert "attempts" in data
    # D1.3 devil's-advocate: no phase rows yet (D1.2 writes them) — the detail
    # payload carries the fields, empty. This is the SHIPPED state for every
    # real task today, and the drawer must read it as "no ran chip".
    assert data["phases"] == []
    assert data["active_seconds"] in (None, 0)


@pytest.mark.asyncio
async def test_get_task_phases_populated(client, store):
    t = await _seed_task(store)
    await store.open_phase(t.id, attempt=1, phase="code")
    await store.close_phase(t.id, outcome="done", reason="")
    await store.open_phase(t.id, attempt=1, phase="review")  # still open
    r = await client.get(f"/api/tasks/{t.id}")
    assert r.status_code == 200
    data = r.json()
    phases = data["phases"]
    assert [p["phase"] for p in phases] == ["code", "review"]
    assert phases[0]["outcome"] == "done"
    assert phases[0]["ended_at"] is not None
    assert phases[1]["ended_at"] is None  # open phase still runs
    for p in phases:
        assert p["seconds"] >= 0
    assert isinstance(data["active_seconds"], (int, float))
    assert data["active_seconds"] > 0
    # NB: not asserting active <= wall here — in this synthetic seed phase
    # writes do not advance task.updated_at, so `wall_seconds` (created → last
    # activity) stays ~0 while the open review phase accrues real time. The
    # `active <= wall` invariant is a real-run property, not one this isolated
    # store exercises; asserting it here would be flaky.


@pytest.mark.asyncio
async def test_get_task_404(client):
    r = await client.get("/api/tasks/does-not-exist")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_task_prefix_lookup(client, store):
    t = await _seed_task(store)
    r = await client.get(f"/api/tasks/{t.id[:8]}")
    assert r.status_code == 200
    assert r.json()["id"] == t.id


# --------------------------------------------------------------------------- #
# P1: heavy per-attempt blobs move off the inline payload (running-task page  #
# slow-open) — review_checklist / verifier_results / test_results are multi- #
# KB JSON per attempt, observed on live tasks, and neither the list nor the   #
# detail payload should carry them inline any more.                          #
# --------------------------------------------------------------------------- #

_HEAVY_ATTEMPT_FIELDS = ("review_checklist", "verifier_results", "test_results")


async def _seed_task_with_heavy_attempt(store) -> tuple:
    t = await _seed_task(store)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid,
        review_checklist={"passed": True, "items": [{"passed": True, "label": "x"}]},
        verifier_results=[{"name": "tests", "passed": True}],
        test_results={"test_count": 5},
    )
    return t, aid


@pytest.mark.asyncio
async def test_list_payload_never_carries_the_heavy_attempt_fields(client, store):
    await _seed_task_with_heavy_attempt(store)
    r = await client.get("/api/tasks")
    assert r.status_code == 200
    body_text = r.text
    for field in _HEAVY_ATTEMPT_FIELDS:
        assert field not in body_text, (
            f"list payload leaked {field!r} — it must never appear on the "
            "board's list endpoint")


@pytest.mark.asyncio
async def test_detail_payload_drops_the_heavy_attempt_fields(client, store):
    t, _aid = await _seed_task_with_heavy_attempt(store)
    r = await client.get(f"/api/tasks/{t.id}")
    assert r.status_code == 200
    data = r.json()
    assert len(data["attempts"]) == 1
    attempt = data["attempts"][0]
    for field in _HEAVY_ATTEMPT_FIELDS:
        assert field not in attempt, (
            f"detail payload still inlines {field!r} — it must be served "
            "lazily from the per-attempt details endpoint instead")
    # The summaries the drawer still needs inline stay put.
    assert attempt["attempt_number"] == 1


@pytest.mark.asyncio
async def test_attempt_details_endpoint_returns_the_full_blobs(client, store):
    t, _aid = await _seed_task_with_heavy_attempt(store)
    r = await client.get(f"/api/tasks/{t.id}/attempts/1/details")
    assert r.status_code == 200
    data = r.json()
    assert data["review_checklist"] == {
        "passed": True, "items": [{"passed": True, "label": "x"}]}
    assert data["verifier_results"] == [{"name": "tests", "passed": True}]
    assert data["test_results"] == {"test_count": 5}


@pytest.mark.asyncio
async def test_attempt_details_404_for_unknown_attempt_number(client, store):
    t, _aid = await _seed_task_with_heavy_attempt(store)
    r = await client.get(f"/api/tasks/{t.id}/attempts/99/details")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_attempt_details_404_for_unknown_task(client):
    r = await client.get("/api/tasks/does-not-exist/attempts/1/details")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# POST /api/tasks — external_id (SCRUM-32)                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_task_jira_stores_external_id(client, store):
    r = await client.post("/api/tasks", json={
        "title": "Import from Jira",
        "source": "jira",
        "external_id": "PROJ-9",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["external_id"] == "PROJ-9"
    task = await store.get_task(body["id"])
    assert task.external_id == "PROJ-9"


@pytest.mark.asyncio
async def test_create_task_jira_external_id_trimmed_and_capped(client, store):
    r = await client.post("/api/tasks", json={
        "title": "Import from Jira",
        "source": "jira",
        "external_id": "  " + ("X" * 100),
    })
    assert r.status_code == 201
    body = r.json()
    assert body["external_id"] == "X" * 64
    task = await store.get_task(body["id"])
    assert task.external_id == "X" * 64


@pytest.mark.asyncio
async def test_create_task_mcp_source_persists(client, store):
    r = await client.post("/api/tasks", json={
        "title": "Added via MCP bridge",
        "source": "mcp",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["source"] == "mcp"
    task = await store.get_task(body["id"])
    assert task.source == "mcp"


@pytest.mark.asyncio
async def test_create_task_unknown_source_still_coerced_to_board(client, store):
    r = await client.post("/api/tasks", json={
        "title": "Arbitrary client string",
        "source": "totally-made-up",
    })
    assert r.status_code == 201
    assert r.json()["source"] == "board"


@pytest.mark.asyncio
async def test_create_task_board_ignores_external_id(client, store):
    r = await client.post("/api/tasks", json={
        "title": "Typed task",
        "source": "board",
        "external_id": "PROJ-9",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["external_id"] is None
    task = await store.get_task(body["id"])
    assert task.external_id is None


@pytest.mark.asyncio
async def test_create_task_absent_external_id_unchanged(client, store):
    r = await client.post("/api/tasks", json={"title": "Plain task"})
    assert r.status_code == 201
    body = r.json()
    assert body["external_id"] is None
    task = await store.get_task(body["id"])
    assert task.external_id is None


# --------------------------------------------------------------------------- #
# POST /api/tasks — SCRUM-26 repo profile default token budgets               #
# --------------------------------------------------------------------------- #

def _fake_git_repo(tmp_path, name="repo"):
    """create_task's repo_path branch requires a real dir with a `.git`
    entry; content doesn't matter, only that the check passes."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    return str(repo.resolve())


@pytest.mark.asyncio
async def test_create_task_copies_profile_defaults_into_config(client, store, tmp_path):
    repo = _fake_git_repo(tmp_path)
    await store.upsert_profile(ProjectProfile(
        repo_path=repo, default_attempt_tokens=6_000_000, default_lifetime_tokens=16_000_000,
    ))
    r = await client.post("/api/tasks", json={"title": "Heavy repo task", "repo_path": repo})
    assert r.status_code == 201
    task = await store.find_task(r.json()["id"])
    assert task.config["attempt_tokens"] == 6_000_000
    assert task.config["lifetime_tokens"] == 16_000_000


@pytest.mark.asyncio
async def test_create_task_no_profile_defaults_config_unchanged(client, store, tmp_path):
    repo = _fake_git_repo(tmp_path, name="plain_repo")
    r = await client.post("/api/tasks", json={"title": "Plain task", "repo_path": repo})
    assert r.status_code == 201
    task = await store.find_task(r.json()["id"])
    assert "attempt_tokens" not in task.config
    assert "lifetime_tokens" not in task.config


@pytest.mark.asyncio
async def test_create_task_explicit_config_overrides_profile_defaults(client, store, tmp_path, monkeypatch):
    repo = _fake_git_repo(tmp_path, name="override_repo")
    await store.upsert_profile(ProjectProfile(
        repo_path=repo, default_attempt_tokens=6_000_000, default_lifetime_tokens=16_000_000,
    ))
    # Simulate a task that already carries an explicit attempt_tokens override
    # by the time create_task's profile-defaults merge runs (e.g. a future
    # explicit-config field on CreateTaskRequest, or a caller-preset value) —
    # the merge must leave it alone rather than clobber it with the profile
    # default, while still filling in the untouched lifetime_tokens key.
    from no_human.core.task import Task as TaskCls
    orig_new = TaskCls.new

    def _new_with_explicit_override(*a, **kw):
        t = orig_new(*a, **kw)
        t.config["attempt_tokens"] = 999
        return t

    monkeypatch.setattr(TaskCls, "new", _new_with_explicit_override)
    r = await client.post("/api/tasks", json={"title": "Override task", "repo_path": repo})
    assert r.status_code == 201
    task = await store.find_task(r.json()["id"])
    assert task.config["attempt_tokens"] == 999            # explicit wins, not clobbered
    assert task.config["lifetime_tokens"] == 16_000_000    # untouched key still gets the default


# --------------------------------------------------------------------------- #
# GET /api/tasks/{id}/diff                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_diff_no_repo(client, store):
    t = Task.new("No repo", repo_path=None)
    await store.create_task(t)
    r = await client.get(f"/api/tasks/{t.id}/diff")
    assert r.status_code == 200
    assert r.text == ""


@pytest.mark.asyncio
async def test_get_diff_no_attempts(client, store):
    t = await _seed_task(store)
    r = await client.get(f"/api/tasks/{t.id}/diff")
    assert r.status_code == 200
    assert r.text == ""


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _init_diff_repo(repo_dir):
    """base (main) -> attempt branch: commit A touches 3 files, commit B
    touches 1 line of a 4th (pre-existing) file. Returns (sha_a, sha_b)."""
    repo_dir.mkdir()
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "u@e.com")
    _git(repo_dir, "config", "user.name", "u")
    (repo_dir / "base.txt").write_text("base\n")
    (repo_dir / "d.txt").write_text("d1\nd2\nd3\n")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "base")

    _git(repo_dir, "checkout", "-q", "-b", "attempt")
    (repo_dir / "a.txt").write_text("a\n")
    (repo_dir / "b.txt").write_text("b\n")
    (repo_dir / "c.txt").write_text("c\n")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "commit A: three files")
    sha_a = _git(repo_dir, "rev-parse", "HEAD")

    (repo_dir / "d.txt").write_text("d1\nd2 changed\nd3\n")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "commit B: one line of a fourth file")
    sha_b = _git(repo_dir, "rev-parse", "HEAD")

    _git(repo_dir, "checkout", "-q", "main")
    return sha_a, sha_b


@pytest.mark.asyncio
async def test_diff_spans_the_whole_branch_not_just_the_last_commit(client, store, tmp_path):
    """A two-commit attempt branch must render the UNION of both commits
    against its recorded base, not just the last commit (PR #213 defect)."""
    repo = tmp_path / "repo"
    sha_a, sha_b = _init_diff_repo(repo)

    t = Task.new("Multi-commit", repo_path=str(repo))
    t.context = {"base_branch": "main"}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt_id, commit_sha=sha_b)

    r = await client.get(f"/api/tasks/{t.id}/diff")
    assert r.status_code == 200
    for path in ("a.txt", "b.txt", "c.txt", "d.txt"):
        assert path in r.text, f"{path} missing from union diff: {r.text!r}"
    assert r.text.count("diff --git") == 4


@pytest.mark.asyncio
async def test_single_commit_branch_diff_is_unchanged(client, store, tmp_path):
    """A branch one commit ahead of its base must render exactly what the
    old `<sha>~1..<sha>` form produced — the three-dot form is a strict
    superset, not a behaviour change, for the single-commit case."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "u@e.com")
    _git(repo, "config", "user.name", "u")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "attempt")
    (repo / "only.txt").write_text("only\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "one commit ahead")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    t = Task.new("Single commit", repo_path=str(repo))
    t.context = {"base_branch": "main"}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt_id, commit_sha=sha)

    r = await client.get(f"/api/tasks/{t.id}/diff")
    assert r.status_code == 200

    expected = subprocess.run(
        ["git", "diff", f"{sha}~1..{sha}", "--no-color"],
        cwd=repo, capture_output=True, text=True,
    ).stdout
    assert r.text == expected
    assert r.text != ""


@pytest.mark.asyncio
async def test_missing_base_branch_falls_back_to_single_commit(client, store, tmp_path, caplog):
    """No recorded base, and a recorded-but-deleted base, must both fall
    back to today's single-commit diff (not an empty string), with the
    reason logged — never surfaced in the response body (PlainTextResponse
    has no envelope to put it in)."""
    repo = tmp_path / "repo"
    sha_a, sha_b = _init_diff_repo(repo)
    caplog.set_level(logging.INFO, logger="no_human.api")

    # Case 1: no base_branch recorded in context at all.
    t1 = Task.new("No base", repo_path=str(repo))
    t1.context = {}
    await store.create_task(t1)
    attempt1 = await store.create_attempt(t1.id, 1)
    await store.update_attempt(attempt1, commit_sha=sha_b)

    r1 = await client.get(f"/api/tasks/{t1.id}/diff")
    assert r1.status_code == 200
    assert "d.txt" in r1.text
    assert "a.txt" not in r1.text  # only commit B's file — today's behaviour
    assert "no recorded base_branch" in caplog.text

    caplog.clear()

    # Case 2: base_branch recorded but unresolvable (branch deleted/pruned).
    t2 = Task.new("Deleted base", repo_path=str(repo))
    t2.context = {"base_branch": "branch-that-was-deleted"}
    await store.create_task(t2)
    attempt2 = await store.create_attempt(t2.id, 1)
    await store.update_attempt(attempt2, commit_sha=sha_b)

    r2 = await client.get(f"/api/tasks/{t2.id}/diff")
    assert r2.status_code == 200
    assert "d.txt" in r2.text
    assert "a.txt" not in r2.text
    assert "unresolvable base_branch" in caplog.text


# --------------------------------------------------------------------------- #
# POST /api/tasks/{id}/approve                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_approve_awaiting(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    # "never merges" language must be present
    assert "never merges" in data["message"].lower() or "agent never merges" in data["message"].lower()


@pytest.mark.asyncio
async def test_approve_wrong_status_409(client, store):
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_approve_wrong_status_writes_approve_refused_event(client, store):
    """The board's approve button was failing silently (task e24cee25/PR #643:
    the operator saw nothing when the merge was refused) because no refusal
    path wrote anything the drawer could show. Every refusal must now leave
    an `approve_refused` event carrying the exact text the response returned."""
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 409
    detail = r.json()["detail"]
    events = await store.list_events(t.id)
    assert any(
        e["kind"] == "approve_refused" and detail in e["text"] for e in events
    )


@pytest.mark.asyncio
async def test_approve_merge_in_progress_writes_approve_refused_event(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    assert await store.claim_merge(t.id)  # simulate an in-flight merge claim
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 409
    assert r.json()["detail"] == "Merge already in progress"
    events = await store.list_events(t.id)
    assert any(
        e["kind"] == "approve_refused" and "Merge already in progress" in e["text"]
        for e in events
    )


@pytest.mark.asyncio
async def test_approve_land_failure_writes_approve_refused_event(client, store, monkeypatch):
    app_module = importlib.import_module("no_human.api.app")

    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, pr_url="https://github.com/o/r/pull/7")

    async def _fake_merge_task_pr(*args, **kwargs):
        return "", {"step": "push", "stderr": "boom"}

    monkeypatch.setattr(app_module, "_merge_task_pr", _fake_merge_task_pr)

    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 500
    # Response shape is frozen: still the {step, stderr} dict, not a string.
    assert r.json()["detail"] == {"step": "push", "stderr": "boom"}
    events = await store.list_events(t.id)
    assert any(
        e["kind"] == "approve_refused" and "push" in e["text"] for e in events
    )


@pytest.mark.asyncio
async def test_approve_landed_refusal_writes_approve_refused_event(client, store, monkeypatch):
    from no_human.blockers import landed_override as landed_override_module

    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    reason = "0936e40a3 is not an ancestor of fix/global-flags-defeat-the-merge-rules — refusing."

    async def _fake_approve_landed_override(*args, **kwargs):
        raise landed_override_module.OverrideRefused(reason)

    monkeypatch.setattr(
        landed_override_module, "approve_landed_override", _fake_approve_landed_override,
    )

    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": "0936e40a3", "justification": "asserting anyway"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == reason
    events = await store.list_events(t.id)
    assert any(
        e["kind"] == "approve_refused" and reason in e["text"] for e in events
    )


@pytest.mark.asyncio
async def test_successful_approve_writes_no_approve_refused_event(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200
    events = await store.list_events(t.id)
    assert not any(e["kind"] == "approve_refused" for e in events)


@pytest.mark.asyncio
async def test_approve_records_timestamp(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await client.post(f"/api/tasks/{t.id}/approve")
    refreshed = await store.find_task(t.id)
    assert refreshed.context.get("approved_at") is not None


@pytest.mark.asyncio
async def test_approve_404(client):
    r = await client.post("/api/tasks/no-such-task/approve")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_approved_then_escalated_task_stops_reporting_a_live_approval(client, store):
    """The bug this AC closes: 16 rows carried `approved_at` while sitting in
    a status other than `awaiting_approval` (failed/escalated/implementing)
    and the board kept reading them as "approved — merge pending". Once a
    task leaves `awaiting_approval` for anywhere but `done`, the SERIALIZED
    board payload (TaskSummaryOut, what GET /api/tasks actually ships) must
    carry `approval_superseded_at` and stop looking pending — not just the
    raw DB row."""
    from no_human.api.models import TaskSummaryOut

    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200
    approved = await store.find_task(t.id)
    assert approved.context.get("approved_at") is not None

    await store.set_status(approved, TaskStatus.ESCALATED, validate=False)

    escalated = await store.find_task(t.id)
    summary = TaskSummaryOut.from_task(escalated)
    assert summary.status == "escalated"
    assert summary.approved_at is not None, "audit trail must survive"
    assert summary.approval_superseded_at is not None, (
        "the payload the board actually reads must show the supersession")

    r2 = await client.get("/api/tasks")
    assert r2.status_code == 200
    payload = next(x for x in r2.json() if x["id"] == t.id)
    assert payload["status"] == "escalated"
    assert payload["approval_superseded_at"] is not None


# --------------------------------------------------------------------------- #
# POST /api/tasks/{id}/send-back                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_send_back_stores_feedback(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(
        f"/api/tasks/{t.id}/send-back",
        json={"message": "Handle the edge case with empty input."},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    refreshed = await store.find_task(t.id)
    feedback = refreshed.context.get("send_back_feedback", [])
    assert len(feedback) == 1
    assert "edge case" in feedback[0]["message"]


@pytest.mark.asyncio
async def test_send_back_resets_to_implementing(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await client.post(
        f"/api/tasks/{t.id}/send-back",
        json={"message": "Please redo."},
    )
    refreshed = await store.find_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


@pytest.mark.asyncio
async def test_send_back_accumulates_feedback(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await client.post(f"/api/tasks/{t.id}/send-back", json={"message": "First."})
    # reset back to awaiting to send again
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    await client.post(f"/api/tasks/{t.id}/send-back", json={"message": "Second."})
    refreshed = await store.find_task(t.id)
    feedback = refreshed.context.get("send_back_feedback", [])
    assert len(feedback) == 2


@pytest.mark.asyncio
async def test_send_back_404(client):
    r = await client.post("/api/tasks/ghost/send-back", json={"message": "x"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_send_back_missing_message_422(client, store):
    t = await _seed_task(store)
    r = await client.post(f"/api/tasks/{t.id}/send-back", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_send_back_done_task_is_409(client, store):
    """SCRUM-77: a done row's status write is CAS-blocked (SCRUM-73) — the
    verb must say so, not claim success."""
    t = await _seed_task(store, status=TaskStatus.DONE)
    r = await client.post(f"/api/tasks/{t.id}/send-back", json={"message": "redo"})
    assert r.status_code == 409
    refreshed = await store.find_task(t.id)
    assert refreshed.status == TaskStatus.DONE
    assert refreshed.context.get("send_back_feedback") in (None, [])


@pytest.mark.asyncio
async def test_send_back_cancelled_task_is_409(client, store):
    t = await _seed_task(store, status=TaskStatus.FAILED)
    t.context = {"cancel_reason": "Cancelled from board"}
    await store.update_task(t)
    r = await client.post(f"/api/tasks/{t.id}/send-back", json={"message": "redo"})
    assert r.status_code == 409
    refreshed = await store.find_task(t.id)
    assert refreshed.status == TaskStatus.FAILED
    assert refreshed.context.get("send_back_feedback") in (None, [])


@pytest.mark.asyncio
async def test_send_back_done_task_sends_no_broadcast(client, store, monkeypatch):
    from no_human.api.app import _mgr

    captured: list[dict] = []

    async def fake_broadcast(msg):
        captured.append(msg)

    monkeypatch.setattr(_mgr, "broadcast", fake_broadcast)
    t = await _seed_task(store, status=TaskStatus.DONE)
    r = await client.post(f"/api/tasks/{t.id}/send-back", json={"message": "redo"})
    assert r.status_code == 409
    assert captured == []


# --------------------------------------------------------------------------- #
# POST /api/tasks/{id}/reply                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_reply_stores_answer_and_resets(client, store):
    t = await _seed_task(store, status=TaskStatus.BLOCKED)
    t.blocker = {"question": "Which DB?", "category": "need_clarification"}
    await store.update_task(t)

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "SQLite only"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True

    refreshed = await store.find_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    replies = refreshed.context.get("human_replies", [])
    assert len(replies) == 1
    assert replies[0]["answer"] == "SQLite only"
    assert replies[0]["question"] == "Which DB?"


@pytest.mark.asyncio
async def test_reply_choose_applies_the_action_and_resumes_from_checkpoint(client, store):
    """D14/D15: the board's option button must apply the option's action, and the
    reply must resume from the [WIP-BLOCKED] checkpoint, not from base."""
    t = await _seed_task(store, status=TaskStatus.ESCALATED)
    t.blocker = {
        "category": "SCOPE_EXPLOSION",
        "question": "This change exceeds the safety size limits.",
        "options": [
            "split into smaller tasks",
            {"label": "raise the limit for this task",
             "action": {"set_task_config": {"max_lines_changed": 700}}},
        ],
        "resume_branch": "scratch/x/abc-2",
        "resume_commit": "75c68e08",
    }
    await store.update_task(t)

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "", "choose": 2})
    assert r.status_code == 200, r.text

    refreshed = await store.find_task(t.id)
    assert refreshed.config["max_lines_changed"] == 700
    reply = refreshed.context["human_replies"][-1]
    assert reply["answer"] == "raise the limit for this task"
    assert reply["applied"] == "max_lines_changed=700"
    assert refreshed.context["resume_from"]["sha"] == "75c68e08"


@pytest.mark.asyncio
async def test_reply_choose_out_of_range_is_400_and_changes_nothing(client, store):
    t = await _seed_task(store, status=TaskStatus.ESCALATED)
    t.blocker = {"category": "SCOPE_EXPLOSION", "options": ["only one"]}
    await store.update_task(t)

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "", "choose": 7})
    assert r.status_code == 400
    assert "between 1 and 1" in r.json()["detail"]

    refreshed = await store.find_task(t.id)
    assert refreshed.config == {}
    assert refreshed.status is TaskStatus.ESCALATED  # not resumed


@pytest.mark.asyncio
async def test_reply_all_parked_statuses_accepted(client, store):
    for status in (TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
                   TaskStatus.PAUSED_QUOTA, TaskStatus.ESCALATED):
        t = await _seed_task(store, status=status, title=f"parked-{status.value}")
        r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "go ahead"})
        assert r.status_code == 200, f"expected 200 for {status.value}, got {r.status_code}"


@pytest.mark.asyncio
async def test_reply_wrong_status_409(client, store):
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "irrelevant"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_reply_404(client):
    r = await client.post("/api/tasks/ghost/reply", json={"answer": "x"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reply_missing_answer_422(client, store):
    t = await _seed_task(store, status=TaskStatus.BLOCKED)
    r = await client.post(f"/api/tasks/{t.id}/reply", json={})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Static SPA path resolution                                                   #
# --------------------------------------------------------------------------- #

def test_web_dist_path_points_at_repo_web_dir():
    """_WEB_DIST must resolve to <repo>/web/dist — not above the repo. A wrong
    parents[] index silently breaks SPA serving (the API just 404s on /)."""
    from pathlib import Path

    from no_human.api.app import _WEB_DIST

    repo_root = Path(__file__).resolve().parents[1]  # tests/ -> repo root
    assert _WEB_DIST == repo_root / "web" / "dist"


async def test_spa_catchall_does_not_intercept_api(client):
    """The SPA catch-all must never return index.html for /api/ paths.
    If the SPA handler intercepts an API path, the frontend gets HTML
    instead of JSON → the 'Unexpected token <' error."""
    r = await client.get("/api/onboarding/status")
    assert r.status_code == 200
    assert "application/json" in r.headers.get("content-type", "")
    data = r.json()
    assert "completed" in data


def test_board_lanes_cover_every_task_status():
    """Every TaskStatus must map to a board lane — otherwise a task in that state
    silently vanishes from the UI (regression: parked states were dropped)."""
    import re
    from pathlib import Path

    from no_human.core.task import TaskStatus

    # LANES moved out of Board.jsx into boardLanes.js (the 2026-07-11 lane split
    # into Review-PR / Needs-Answer). Read wherever the statuses arrays live.
    board = (Path(__file__).resolve().parents[1] / "web" / "src" / "boardLanes.js").read_text()
    # Collect every status string listed in a `statuses: [...]` array.
    listed: set[str] = set()
    for arr in re.findall(r"statuses:\s*\[([^\]]*)\]", board):
        listed |= set(re.findall(r'"([a-z_]+)"', arr))
    # Some statuses are routed dynamically in routeTask (e.g. "blocked" splits on
    # wake_condition into Working vs Needs-Answer) rather than via a statuses
    # array — they're still covered, so count those explicit comparisons too.
    listed |= set(re.findall(r'status === "([a-z_]+)"', board))
    missing = {s.value for s in TaskStatus} - listed
    assert not missing, f"task statuses with no board lane: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# Role attribution survives the events formatter                               #
# --------------------------------------------------------------------------- #

def test_format_events_keeps_planner_and_aggregator_events():
    """The formatter used to drop every source it didn't recognise, so stamping
    planner events with their own role would have made them vanish from the
    board entirely — the opposite of the point."""
    from no_human.api.app import _format_events

    out = _format_events([
        {"ts": 1, "source": "orchestrator", "kind": "planning", "text": "start"},
        {"ts": 2, "source": "planner:test-first", "kind": "tool_use",
         "tool_name": "Read", "tool_input": {"file_path": "Jenkinsfile"}},
        {"ts": 3, "source": "planner:risk-first", "kind": "subagent_start",
         "text": "Investigate Jenkinsfile", "task_id": "sdk-1", "status": "active"},
        {"ts": 4, "source": "aggregator", "kind": "text", "text": "synthesizing"},
        {"ts": 5, "source": "agent", "kind": "tool_use",
         "tool_name": "Edit", "tool_input": {"file_path": "Jenkinsfile"}},
    ])

    by_ts = {e["ts"]: e for e in out}
    assert set(by_ts) == {1, 2, 3, 4, 5}, "no event may be dropped"
    # The raw role is preserved, not flattened back onto "agent".
    assert by_ts[2]["source"] == "planner:test-first"
    assert by_ts[3]["source"] == "planner:risk-first"
    assert by_ts[3]["task_id"] == "sdk-1"
    assert by_ts[4]["source"] == "aggregator"
    assert by_ts[4]["kind"] == "agent_text"
    assert by_ts[5]["source"] == "agent"


def test_is_agent_session_classifies_every_role():
    from no_human.core.orchestrator import is_agent_session

    assert is_agent_session("agent")
    assert is_agent_session("planner")
    assert is_agent_session("planner:minimal-first")
    assert is_agent_session("aggregator")
    assert not is_agent_session("orchestrator")
    assert not is_agent_session("reviewer")
    assert not is_agent_session("")
    assert not is_agent_session(None)


def test_format_events_carries_the_per_role_model_map():
    """The formatter keeps only ts/kind/text/source for orchestrator events, so
    the models dict would be stripped before the System view ever saw it."""
    from no_human.api.app import _format_events

    out = _format_events([
        {"ts": 1, "source": "orchestrator", "kind": "models",
         "text": "coder=claude-sonnet-5 · reviewer=claude-opus-5",
         "models": {"coder": "claude-sonnet-5", "reviewer": "claude-opus-5"}},
        {"ts": 2, "source": "orchestrator", "kind": "state", "text": "implementing"},
    ])
    assert out[0]["models"] == {"coder": "claude-sonnet-5", "reviewer": "claude-opus-5"}
    assert "models" not in out[1], "only the models event carries the map"


def test_format_events_carries_the_role_backends_disclosure():
    """§6d part 2: a non-default reviewer backend is disclosed as its own
    `role_backends` event kwarg, never appended to the (110-char-clipped)
    `text` — the formatter used to whitelist only `models`/`message`/
    `_VERDICT_META`, so this kwarg was silently dropped before it ever
    reached `web/src/summaries.js`'s `nonDefaultReviewer`."""
    from no_human.api.app import _format_events

    out = _format_events([
        {"ts": 1, "source": "orchestrator", "kind": "models",
         "text": "coder=claude-sonnet-5 · reviewer=claude-opus-4-8",
         "models": {"coder": "claude-sonnet-5", "reviewer": "claude-opus-4-8"},
         "role_backends": {"reviewer": {"backend": "codex", "model": "gpt-5-codex"}}},
        {"ts": 2, "source": "orchestrator", "kind": "state", "text": "implementing"},
    ])
    assert out[0]["role_backends"] == {
        "reviewer": {"backend": "codex", "model": "gpt-5-codex"}}
    assert "role_backends" not in out[1]


async def test_task_events_are_not_truncated_by_the_in_memory_buffer(store):
    """The scheduler buffer is a deque(maxlen=200). Serving it alone dropped the
    Planner's events from a 321-event run and the Planner vanished from the
    System view mid-run."""
    from types import SimpleNamespace
    from no_human.api.app import task_events
    from no_human.core.task import Task

    t = Task.new("long run", repo_path="/tmp/x")
    await store.create_task(t)
    persisted = [{"ts": float(i), "source": "planner", "kind": "tool_use",
                  "tool_name": f"R{i}"} for i in range(300)]
    await store.save_events(t.id, persisted)

    # The buffer holds only the tail, plus two events not yet flushed.
    class FakeSched:
        _event_log = {t.id: None}

        def task_events(self, tid):
            return persisted[-50:] + [
                {"ts": 1000.0, "source": "agent", "kind": "tool_use", "tool_name": "live1"},
                {"ts": 1001.0, "source": "agent", "kind": "tool_use", "tool_name": "live2"},
            ]

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        scheduler=FakeSched(), store=store)))
    out = await task_events(t.id, req)

    assert len(out) == 302, "every persisted event plus the unflushed tail"
    assert out[0]["source"] == "planner", "the earliest events must survive"
    assert [e["tool_name"] for e in out[-2:]] == ["live1", "live2"]
    # The overlapping window must not be duplicated.
    assert sum(1 for e in out if e["tool_name"] == "R299") == 1


def test_format_events_carries_the_supervisor_message():
    """`text` is just "continue"/"correct"; the substance lives in `message`.
    Dropping it made the Supervisor look like it only ever said one word —
    while its corrections carried real guidance (live run 84251cb2: 33
    corrections, all invisible on the board)."""
    from no_human.api.app import _format_events

    out = _format_events([
        {"ts": 1, "source": "orchestrator", "kind": "supervisor_decision",
         "text": "correct", "message": "Use the no_human_verify skill instead"},
        {"ts": 2, "source": "orchestrator", "kind": "supervisor_decision",
         "text": "continue"},
    ])
    assert out[0]["message"] == "Use the no_human_verify skill instead"
    assert "message" not in out[1]


async def test_sse_replays_from_last_event_id(store):
    """W2.3: the browser's native EventSource reconnect sends Last-Event-ID;
    the stream must resume AFTER that cursor, not replay from zero (duplicate
    events) or force the client to give up (frozen UI)."""
    from types import SimpleNamespace
    from no_human.api.app import task_events_stream
    from no_human.core.task import Task

    t = Task.new("sse replay", repo_path="/tmp/x")
    await store.create_task(t)
    events = [
        {"ts": 10.0, "kind": "state", "source": "orchestrator", "text": "implementing"},
        {"ts": 20.0, "kind": "state", "source": "orchestrator", "text": "reviewing"},
    ]

    class FakeSched:
        inflight = set()            # not running → the stream ends after idle ticks
        _event_log = {t.id: None}   # _resolve_task_id reads the keys
        _event_notify = {}
        def task_events(self, tid):
            return events

    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(scheduler=FakeSched(), store=store)),
        headers={"last-event-id": "10.0"},
    )
    resp = await task_events_stream(t.id, req)
    body = ""
    async for chunk in resp.body_iterator:
        body += chunk if isinstance(chunk, str) else chunk.decode()
        if '"done"' in body:
            break
    assert "reviewing" in body, "events after the cursor must arrive"
    assert "implementing" not in body, "events at/before the cursor must not replay"
    assert "id: 20.0" in body, "frames must carry the ts as the SSE id"


def test_format_events_passes_watcher_events_through():
    """The persona walk found the post-PR ladder invisible: 2015 events
    served, 0 from the watcher — _format_events dropped source:'watcher'
    entirely, so the Shepherding stage could never light up."""
    from no_human.api.app import _format_events

    out = _format_events([
        {"ts": 1, "source": "watcher", "kind": "ci_gate_pass",
         "text": "CI_GATE integration PASSED"},
        {"ts": 2, "source": "watcher", "kind": "pr_ci_red",
         "text": "CI failing — fix round 1/3"},
        {"ts": 3, "source": "human", "kind": "state_repaired",
         "text": "escalated → awaiting_approval"},
    ])
    assert [e["kind"] for e in out] == ["ci_gate_pass", "pr_ci_red", "state_repaired"]
    assert out[0]["source"] == "watcher"


@pytest.mark.asyncio
async def test_retry_clears_cancel_reason_atomically(client, store):
    """W1.1 API migration: retry clears cancel_reason (None-delete via
    merge_context) and stamps retried_at, without a stale-blob clobber."""
    t = await _seed_task(store, status=TaskStatus.FAILED)
    t.context = {"cancel_reason": "was cancelled", "keep_me": "yes"}
    await store.update_task(t)
    r = await client.post(f"/api/tasks/{t.id}/retry")
    assert r.status_code == 200
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.PENDING
    assert "cancel_reason" not in fresh.context   # None-deleted
    assert fresh.context.get("keep_me") == "yes"  # sibling survived
    assert "retried_at" in fresh.context


@pytest.mark.asyncio
async def test_cancel_sets_reason_and_fails(client, store):
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    r = await client.post(f"/api/tasks/{t.id}/cancel")
    assert r.status_code == 200
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.FAILED
    assert fresh.context.get("cancel_reason")


@pytest.mark.asyncio
async def test_cancel_records_the_operator_reason(client, store):
    """The board's Cancel-task modal posts a typed reason; it must be
    recorded verbatim, the task must land FAILED (never DONE — a cancel is
    never a false "done"), and the board payload's derived `cancelled` flag
    must be True so Stats keeps it out of the success-rate denominator."""
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    r = await client.post(f"/api/tasks/{t.id}/cancel", json={"reason": "duplicate of X"})
    assert r.status_code == 200
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.FAILED
    assert fresh.status != TaskStatus.DONE
    assert fresh.context["cancel_reason"] == "duplicate of X"
    from no_human.api.models import TaskSummaryOut
    assert TaskSummaryOut.from_task(fresh).cancelled is True


@pytest.mark.asyncio
async def test_cancel_without_a_body_keeps_the_default_reason(client, store):
    """Regression guard: the board's own no-reason POST (and any pre-existing
    caller) must keep 200ing with the unchanged default reason now that the
    body is optional."""
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    r = await client.post(f"/api/tasks/{t.id}/cancel")
    assert r.status_code == 200
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.FAILED
    assert fresh.context.get("cancel_reason") == "Cancelled from board"


@pytest.mark.asyncio
async def test_resume_clears_blocker_and_implements(client, store):
    t = await _seed_task(store, status=TaskStatus.BLOCKED)
    t.blocker = {"category": "AMBIGUITY", "question": "?"}
    t.context = {"survivor": 1}
    await store.update_task(t)
    r = await client.post(f"/api/tasks/{t.id}/resume")
    assert r.status_code == 200
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.IMPLEMENTING
    assert fresh.blocker is None
    assert fresh.context.get("survivor") == 1  # context untouched by column write


# --------------------------------------------------------------------------- #
# durable human hold on parked tasks (SCRUM-58)                                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    TaskStatus.PAUSED_QUOTA, TaskStatus.BLOCKED, TaskStatus.ESCALATED,
])
async def test_pause_on_parked_task_stamps_human_stopped_and_keeps_status(client, store, status):
    """SCRUM-58: a supervisor reserving the quota window used to hit a 409
    ('only active tasks can be paused') and had to fall back to a raw DB
    write of blocker.human_stopped. /pause must accept parked statuses too,
    stamping the hold without touching status or clobbering the blocker.
    ESCALATED included: it's exactly the state a task is in when it's
    asking a human to decide — the one a human most needs to be able to
    hold (api/app.py's _HOLDABLE_STATUSES)."""
    t = await _seed_task(store, status=status)
    t.blocker = {"category": "PAUSED_QUOTA", "question": "no quota", "keep_me": "yes"}
    await store.update_task_columns(t)

    r = await client.post(f"/api/tasks/{t.id}/pause")
    assert r.status_code == 200, r.text

    fresh = await store.find_task(t.id)
    assert fresh.status == status                       # untouched
    assert fresh.blocker.get("human_stopped") is True
    assert fresh.blocker.get("keep_me") == "yes"          # existing payload kept


@pytest.mark.asyncio
async def test_pause_on_escalated_task_leaves_wake_sweep_parked(client, store):
    """The one state a human most needs to hold: /pause on ESCALATED must
    stamp a hold the wake sweep actually honors, not just a flag nobody
    reads. `now` is set past max_park (48h) relative to raised_at, so the
    ONLY thing standing between this task and re-escalation is the
    human_stopped skip (blockers/wake.py:397) — proven by re-evaluating the
    SAME task/blocker with the stamp stripped and asserting the sweep fires
    'escalated_timeout' instead, so this control can actually fail if the
    skip regresses."""
    from datetime import datetime, timedelta

    from no_human.blockers.wake import WakeWatcher

    t = await _seed_task(store, status=TaskStatus.ESCALATED)
    t.blocker = {"category": "AMBIGUITY", "question": "?",
                 "raised_at": "2026-01-01T00:00:00+00:00"}
    await store.update_task_columns(t)

    r = await client.post(f"/api/tasks/{t.id}/pause")
    assert r.status_code == 200, r.text
    held = await store.find_task(t.id)
    assert held.status == TaskStatus.ESCALATED
    assert held.blocker.get("human_stopped") is True

    now = datetime.fromisoformat("2026-01-01T00:00:00+00:00") + timedelta(hours=49)
    watcher = WakeWatcher(store, {"blockers": {"max_park_duration": "48h"}})

    action = await watcher._evaluate(held, now=now)
    assert action is None, f"human-stopped ESCALATED task must stay parked, got {action!r}"
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.ESCALATED           # still parked

    # NEGATIVE CONTROL: strip the human_stopped stamp on the SAME task and
    # re-evaluate at the SAME `now`. Without the skip, max_park (48h) has
    # elapsed since raised_at (49h ago) so the sweep must fire — proving the
    # `action is None` assertion above is discriminating, not inert.
    unheld_blocker = dict(fresh.blocker)
    del unheld_blocker["human_stopped"]
    fresh.blocker = unheld_blocker
    await store.update_task_columns(fresh)

    action2 = await watcher._evaluate(fresh, now=now)
    assert action2 == "escalated_timeout", (
        f"twin without human_stopped must be re-escalated by max_park, got {action2!r}")


@pytest.mark.asyncio
async def test_pause_on_parked_task_without_blocker_initializes_one(client, store):
    """Intake Q&A: if a parked task has no blocker yet, pause initializes one
    with sensible defaults rather than erroring."""
    t = await _seed_task(store, status=TaskStatus.PAUSED_QUOTA)
    assert t.blocker is None

    r = await client.post(f"/api/tasks/{t.id}/pause")
    assert r.status_code == 200, r.text

    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.PAUSED_QUOTA
    assert fresh.blocker.get("human_stopped") is True
    assert fresh.blocker.get("category")                  # default populated


@pytest.mark.asyncio
async def test_pause_on_parked_task_is_idempotent(client, store):
    """Intake Q&A: pausing an already-held parked task twice succeeds
    idempotently — no error, human_stopped stays True."""
    t = await _seed_task(store, status=TaskStatus.PAUSED_QUOTA)
    t.blocker = {"category": "PAUSED_QUOTA", "question": "no quota"}
    await store.update_task_columns(t)

    r1 = await client.post(f"/api/tasks/{t.id}/pause")
    assert r1.status_code == 200, r1.text
    r2 = await client.post(f"/api/tasks/{t.id}/pause")
    assert r2.status_code == 200, r2.text

    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.PAUSED_QUOTA
    assert fresh.blocker.get("human_stopped") is True


@pytest.mark.asyncio
async def test_hold_and_release_round_trip_on_paused_quota_task(client, store):
    """Full hold+release round-trip: pause stamps human_stopped on a
    paused_quota task; resume clears the flag but leaves status/blocker
    payload otherwise untouched (resume no longer forces a transition)."""
    t = await _seed_task(store, status=TaskStatus.PAUSED_QUOTA)
    t.blocker = {"category": "PAUSED_QUOTA", "question": "no quota", "keep_me": "yes"}
    await store.update_task_columns(t)

    r = await client.post(f"/api/tasks/{t.id}/pause")
    assert r.status_code == 200, r.text
    held = await store.find_task(t.id)
    assert held.status == TaskStatus.PAUSED_QUOTA
    assert held.blocker.get("human_stopped") is True

    r2 = await client.post(f"/api/tasks/{t.id}/resume")
    assert r2.status_code == 200, r2.text
    released = await store.find_task(t.id)
    assert released.status == TaskStatus.PAUSED_QUOTA     # unchanged by resume
    assert "human_stopped" not in released.blocker
    assert released.blocker.get("keep_me") == "yes"        # payload preserved


@pytest.mark.asyncio
async def test_resume_without_human_stopped_keeps_existing_semantics(client, store):
    """Non-hold path is untouched: resuming a plain blocked task (no
    human_stopped) still clears the blocker fully and moves to implementing."""
    t = await _seed_task(store, status=TaskStatus.BLOCKED)
    t.blocker = {"category": "AMBIGUITY", "question": "?"}
    await store.update_task_columns(t)

    r = await client.post(f"/api/tasks/{t.id}/resume")
    assert r.status_code == 200, r.text
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.IMPLEMENTING
    assert fresh.blocker is None


@pytest.mark.asyncio
async def test_pause_on_implementing_still_transitions_to_blocked(client, store):
    """NEGATIVE CONTROL for the ESCALATED hold change: an ACTIVE task
    (IMPLEMENTING is not in _HOLDABLE_STATUSES) must still take the
    original pause branch — transition to BLOCKED with a fresh
    USER_PAUSED blocker — not the new hold-in-place branch."""
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    r = await client.post(f"/api/tasks/{t.id}/pause")
    assert r.status_code == 200, r.text
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.BLOCKED
    assert fresh.blocker.get("category") == "USER_PAUSED"
    assert "human_stopped" not in fresh.blocker


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.FAILED])
async def test_pause_still_409s_on_done_and_cancelled(client, store, status):
    t = await _seed_task(store, status=status)
    r = await client.post(f"/api/tasks/{t.id}/pause")
    assert r.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.FAILED])
async def test_resume_still_409s_on_done_and_cancelled(client, store, status):
    t = await _seed_task(store, status=status)
    r = await client.post(f"/api/tasks/{t.id}/resume")
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# POST /api/tasks/{id}/shipped (SCRUM-56)                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    TaskStatus.AWAITING_APPROVAL, TaskStatus.ESCALATED, TaskStatus.FAILED,
    TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
    TaskStatus.BLOCKED, TaskStatus.PAUSED_QUOTA,
])
async def test_shipped_transitions_from_each_allowed_status(client, store, status):
    """SCRUM-69: operator-testimony model — shipped is valid from any
    non-terminal status, including mid-run statuses a live worker holds."""
    t = await _seed_task(store, status=status)
    t.blocker = {"category": "AMBIGUITY", "question": "?"}
    await store.update_task(t)

    r = await client.post(f"/api/tasks/{t.id}/shipped",
                           json={"sha": "abc1234", "note": "merged by hand"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "done"

    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.DONE
    assert fresh.blocker is None

    events = await store.list_events(t.id)
    merged = [e for e in events if e.get("kind") == "human_merged"]
    assert len(merged) == 1, f"expected human_merged event for {status.value}"
    assert merged[0]["sha"] == "abc1234"


@pytest.mark.asyncio
async def test_shipped_records_human_merged_event_with_sha_and_note_verbatim(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(f"/api/tasks/{t.id}/shipped",
                           json={"sha": "deadbeef1234", "note": "shipped via PR #7004"})
    assert r.status_code == 200, r.text

    events = await store.list_events(t.id)
    merged = [e for e in events if e.get("kind") == "human_merged"]
    assert len(merged) == 1
    assert merged[0]["sha"] == "deadbeef1234"
    assert merged[0]["note"] == "shipped via PR #7004"


@pytest.mark.asyncio
async def test_shipped_note_is_optional(client, store):
    t = await _seed_task(store, status=TaskStatus.FAILED)
    r = await client.post(f"/api/tasks/{t.id}/shipped", json={"sha": "abc1234"})
    assert r.status_code == 200, r.text
    events = await store.list_events(t.id)
    merged = [e for e in events if e.get("kind") == "human_merged"]
    assert merged[0]["sha"] == "abc1234"
    assert merged[0]["note"] is None


@pytest.mark.asyncio
async def test_shipped_already_done_is_409(client, store):
    t = await _seed_task(store, status=TaskStatus.DONE)
    r = await client.post(f"/api/tasks/{t.id}/shipped", json={"sha": "abc1234"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_shipped_cancelled_task_is_409(client, store):
    t = await _seed_task(store, status=TaskStatus.FAILED)
    t.context = {"cancel_reason": "Cancelled from board"}
    await store.update_task(t)
    r = await client.post(f"/api/tasks/{t.id}/shipped", json={"sha": "abc1234"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_shipped_from_mid_run_status_succeeds(client, store):
    """SCRUM-69: a task a live worker is mid-run on (e.g. resurrected after a
    wrongly-resumed-then-paused watcher incident) is no longer 409'd — the
    operator's human_merged testimony is trusted regardless of run status."""
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    r = await client.post(f"/api/tasks/{t.id}/shipped", json={"sha": "abc1234"})
    assert r.status_code == 200, r.text
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_shipped_from_implementing_declaims_and_is_not_claimable(client, store):
    """De-claim + scheduler guard: shipping a mid-run task clears its blocker
    and wake_check_at, and once DONE it falls outside the scheduler's
    _CLAIMABLE statuses (IMPLEMENTING, PENDING) so a live pool cannot
    re-claim it."""
    from no_human.core.scheduler import Scheduler

    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    t.blocker = {"category": "AMBIGUITY", "question": "?"}
    t.wake_check_at = "2026-07-27T00:00:00+00:00"
    await store.update_task(t)

    r = await client.post(f"/api/tasks/{t.id}/shipped", json={"sha": "abc1234"})
    assert r.status_code == 200, r.text

    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.DONE
    assert fresh.blocker is None
    assert fresh.wake_check_at is None

    sched = Scheduler(store, lambda task=None: None, max_workers=1)
    claimable_ids = {ct.id for ct in await sched._claimable()}
    assert t.id not in claimable_ids


@pytest.mark.asyncio
async def test_shipped_empty_sha_is_400(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(f"/api/tasks/{t.id}/shipped", json={"sha": "   "})
    assert r.status_code == 400
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.AWAITING_APPROVAL  # unchanged


@pytest.mark.asyncio
async def test_shipped_404(client):
    r = await client.post("/api/tasks/ghost/shipped", json={"sha": "abc1234"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_shipped_missing_sha_422(client, store):
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(f"/api/tasks/{t.id}/shipped", json={})
    assert r.status_code == 422


def test_task_summary_marks_operator_cancelled_tasks():
    """A cancelled task ends FAILED but carries context.cancel_reason; the API
    surfaces `cancelled` so Stats can keep it out of the success-rate denominator
    (it's an operator decision, not a capability failure)."""
    from no_human.api.models import TaskSummaryOut
    from no_human.core.task import Task, TaskStatus
    cancelled = Task.new("superseded task", repo_path="/tmp/x")
    cancelled.status = TaskStatus.FAILED
    cancelled.context = {"cancel_reason": "superseded by a fresh run"}
    assert TaskSummaryOut.from_task(cancelled).cancelled is True

    genuine = Task.new("real failure", repo_path="/tmp/x")
    genuine.status = TaskStatus.FAILED
    assert TaskSummaryOut.from_task(genuine).cancelled is False


def test_task_out_and_task_summary_out_share_one_cancelled_predicate():
    """The list endpoint (TaskSummaryOut, api/models.py) and the drawer
    (TaskOut) must never disagree about whether the same task's FAILED status
    was an operator cancel. Proven by IDENTITY — both call sites are the same
    imported function object, not two hand-written `bool((task.context or
    {}).get("cancel_reason"))` expressions that happen to agree today and can
    silently diverge tomorrow.
    """
    import inspect

    from no_human.api import models as models_mod
    from no_human.api.models import TaskOut, TaskSummaryOut, _operator_cancelled

    src_task_out = inspect.getsource(TaskOut.from_task)
    src_summary_out = inspect.getsource(TaskSummaryOut.from_task)
    assert "_operator_cancelled(" in src_task_out
    assert "_operator_cancelled(" in src_summary_out

    cancelled = Task.new("superseded task", repo_path="/tmp/x")
    cancelled.status = TaskStatus.FAILED
    cancelled.context = {"cancel_reason": "superseded by a fresh run"}

    # Monkeypatch the ONE shared function and watch both models move together
    # — the behaviour only a genuinely shared predicate (not a coincidence of
    # two identical expressions) can produce.
    original = models_mod._operator_cancelled
    try:
        models_mod._operator_cancelled = lambda task: False
        assert TaskOut.from_task(cancelled, []).cancelled is False
        assert TaskSummaryOut.from_task(cancelled).cancelled is False
    finally:
        models_mod._operator_cancelled = original

    assert TaskOut.from_task(cancelled, []).cancelled is True
    assert TaskSummaryOut.from_task(cancelled).cancelled is True
    assert _operator_cancelled(cancelled) is True


@pytest.mark.asyncio
async def test_get_task_detail_cancelled_matches_the_list_endpoint(client, store):
    """AC2: GET /api/tasks/<id> must report `cancelled` for a cancel_reason-only
    task, and it must equal what GET /api/tasks (the list/summary endpoint)
    reports for the SAME task — before this fix the drawer had no `cancelled`
    field at all, so the two views of one task disagreed."""
    t = await _seed_task(store, status=TaskStatus.FAILED)
    t.context = {"cancel_reason": "Cancelled from board"}
    await store.update_task(t)

    detail = (await client.get(f"/api/tasks/{t.id}")).json()
    listing = (await client.get("/api/tasks")).json()
    summary = next(item for item in listing if item["id"] == t.id)

    assert detail["cancelled"] is True, detail
    assert summary["cancelled"] is True, summary
    assert detail["cancelled"] == summary["cancelled"]


@pytest.mark.asyncio
async def test_attachment_upload_stores_file_and_records_path(
    client, store, tmp_path, monkeypatch,
):
    """Phase 3 (user-requested): a screenshot/document uploaded to a task is
    stored on disk and its path recorded on task.context for the coder to read."""
    monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", tmp_path / "nh_home")
    t = await _seed_task(store, title="bug with a screenshot")
    files = {"file": ("shot.png", b"\x89PNG-fake-bytes", "image/png")}
    r = await client.post(f"/api/tasks/{t.id}/attachments", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "shot.png" and body["count"] == 1
    from pathlib import Path
    assert Path(body["path"]).read_bytes() == b"\x89PNG-fake-bytes"
    got = await store.get_task(t.id)
    assert got.context["attachments"][0]["name"] == "shot.png"


def test_task_out_reports_cache_read_burn():
    """The cost meter must include cache-read (90%+ of real burn) — summing
    tokens_used alone under-reported a 33M-token task as '121.5k tok'."""
    from no_human.api.models import TaskOut, TaskSummaryOut
    from no_human.core.task import Task
    t = Task.new("x", repo_path="/tmp/r")
    attempts = [
        {"id": "a1", "tokens_used": 4_000, "cache_read_tokens": 500_000,
         "attempt_number": 1, "status": "succeeded"},
        {"id": "a2", "tokens_used": 6_000, "cache_read_tokens": 700_000,
         "attempt_number": 2, "status": "failed"},
    ]
    out = TaskOut.from_task(t, attempts)
    assert out.total_tokens == 10_000
    assert out.total_cache_read == 1_200_000
    s = TaskSummaryOut.from_task(t, attempts=attempts)
    assert s.total_cache_read == 1_200_000


def test_task_out_reports_cache_creation_burn():
    """Cache-CREATION is full-price fresh work, and it was invisible to every cost surface.

    The task rollup exposed tokens_used + cache_read only, so a per-task cost priced two of
    the three buckets — and the task rows summed to LESS than the lifetime figure on the same
    page (which does include creation). One model, three buckets, everywhere.
    """
    from no_human.api.models import TaskOut, TaskSummaryOut
    from no_human.core.task import Task
    t = Task.new("x", repo_path="/tmp/r")
    attempts = [
        {"id": "a1", "tokens_used": 4_000, "cache_read_tokens": 500_000,
         "cache_creation_tokens": 30_000, "attempt_number": 1, "status": "succeeded"},
        {"id": "a2", "tokens_used": 6_000, "cache_read_tokens": 700_000,
         "cache_creation_tokens": 20_000, "attempt_number": 2, "status": "failed"},
    ]
    out = TaskOut.from_task(t, attempts)
    assert out.total_cache_creation == 50_000
    s = TaskSummaryOut.from_task(t, attempts=attempts)
    assert s.total_cache_creation == 50_000
    # A task with no creation recorded reports None, not a misleading 0.
    bare = TaskOut.from_task(t, [{"id": "a", "tokens_used": 1, "attempt_number": 1}])
    assert bare.total_cache_creation is None


def test_attempt_rows_carry_the_cache_read_share():
    """The earliest signal an attempt is heading for budget exhaustion —
    per-attempt, computed by the one function that owns this arithmetic
    (core.metrics.cache_read_share), not re-derived here."""
    from no_human.api.models import AttemptOut
    from no_human.core.metrics import cache_read_share

    row = {"id": "a1", "attempt_number": 1,
           "cache_read_tokens": 900_000, "cache_creation_tokens": 100_000}
    out = AttemptOut.from_row(row)
    assert out.cache_read_tokens == 900_000
    assert out.cache_creation_tokens == 100_000
    assert out.cache_read_share == cache_read_share(900_000, 100_000) == 0.9


def test_attempt_cache_read_share_is_null_when_unmeasured():
    """An attempt that recorded no cache tokens at all reports None, not a
    misleading 0.0 — matching cache_read_share()'s own contract."""
    from no_human.api.models import AttemptOut

    out = AttemptOut.from_row({"id": "a1", "attempt_number": 1})
    assert out.cache_read_tokens is None
    assert out.cache_creation_tokens is None
    assert out.cache_read_share is None


def test_task_out_surfaces_the_failure_reason_the_drawer_asks_for():
    """The drawer's "Why it failed" banner could never render.

    `web/src/SlideOver.jsx` gates it on `task.failure_reason`, but that is a
    column on ATTEMPTS and was never on the task payload — so the banner was
    dead for every failed task while 461 of 461 failed attempts carried a
    reason. `web/e2e/failure-reason.mjs` passes regardless: it builds its own
    task object with the field already set, so it pins the COMPONENT's contract
    and never exercises the API that has to satisfy it.
    """
    from no_human.api.models import TaskOut
    from no_human.core.task import Task, TaskStatus
    t = Task.new("x", repo_path="/tmp/r")
    t.status = TaskStatus.FAILED
    attempts = [
        {"id": "a1", "attempt_number": 1, "status": "failed",
         "failure_reason": "first stop"},
        {"id": "a2", "attempt_number": 2, "status": "failed",
         "failure_reason": "the reason that explains the final state"},
    ]
    # The LAST attempt's reason — a retry's stop is what the task ended on.
    assert TaskOut.from_task(t, attempts).failure_reason == (
        "the reason that explains the final state")

    # Out of order on the wire: ordering is by attempt_number, not list order.
    assert TaskOut.from_task(t, list(reversed(attempts))).failure_reason == (
        "the reason that explains the final state")

    # Nothing recorded -> None, not "" and not a misleading empty banner.
    assert TaskOut.from_task(t, [{"id": "a", "attempt_number": 1}]).failure_reason is None
    assert TaskOut.from_task(t, []).failure_reason is None


# --- cost_usd / cost_model: the API prices attempts server-side (core/cost.py), the ----
# board only formats what these fields already send. web/src/cost.js used to hardcode one
# flat Anthropic rate and price EVERY attempt at it, which was simply wrong for a
# Codex/OpenAI attempt once core/pricing.py gained per-model OpenAI rows. core/cost.py's
# own unit tests (tests/test_cost.py) and pricing.usd_cost's (tests/test_pricing_usd.py)
# pin the arithmetic; these pin that the API actually wires it onto every surface the
# board reads (AttemptOut, TaskOut, TaskSummaryOut).

def test_attempt_out_prices_codex_row_from_the_openai_table():
    """gpt-5.3-codex is $1.75/Mtok, not Sonnet's $3 — the exact bug
    web/src/cost.js had (one hardcoded Anthropic rate for every attempt)."""
    from no_human.api.models import AttemptOut
    row = {"id": "a1", "attempt_number": 1, "tokens_used": 1_000_000,
           "models": {"coder": "gpt-5.3-codex"}}
    out = AttemptOut.from_row(row)
    assert out.cost_usd == pytest.approx(1.75)
    assert out.cost_model == "gpt-5.3-codex"


def test_attempt_out_prices_each_role_with_its_own_model():
    """A Codex coder reviewed by Claude is real and common — each role prices
    at its own recorded model; the label is 'mixed' when priced roles
    disagree, never collapsed to just one of them."""
    from no_human.api.models import AttemptOut
    row = {
        "id": "a1", "attempt_number": 1,
        "tokens_used": 1_000_000, "review_tokens_used": 1_000_000,
        "models": {"coder": "gpt-5.3-codex", "reviewer": "claude-opus-4-8"},
    }
    out = AttemptOut.from_row(row)
    assert out.cost_usd == pytest.approx(1.75 + 5.0)
    assert out.cost_model == "mixed"


def test_attempt_out_unpriced_model_uses_the_named_fallback_and_is_visible():
    """An id with no row in MODEL_PRICES_USD_PER_MTOK must still price
    nonzero and say so via the fallback label — never a silent 0.0 and never
    the bare unpriced id, which would look like a real price was found."""
    from no_human.api.models import AttemptOut
    from no_human.core.pricing import FALLBACK_PRICE_NAME
    row = {"id": "a1", "attempt_number": 1, "tokens_used": 1_000_000,
           "models": {"coder": "gpt-5-codex"}}  # deliberately absent from the table
    out = AttemptOut.from_row(row)
    assert out.cost_usd > 0
    assert out.cost_model == FALLBACK_PRICE_NAME
    assert out.cost_model != "gpt-5-codex"


def test_attempt_out_empty_models_prices_at_fallback():
    """11 of this install's 684 attempt rows predate the `models` column —
    NULL there must still price (fallback), never crash and never show 0.0
    for real recorded spend."""
    from no_human.api.models import AttemptOut
    from no_human.core.pricing import FALLBACK_PRICE_NAME
    out = AttemptOut.from_row({"id": "a1", "attempt_number": 1, "tokens_used": 1_000_000})
    assert out.cost_usd == pytest.approx(3.0)
    assert out.cost_model == FALLBACK_PRICE_NAME


def test_task_summary_cost_is_the_sum_of_its_attempt_costs():
    from no_human.api.models import TaskSummaryOut
    from no_human.core.task import Task
    t = Task.new("x", repo_path="/tmp/r")
    attempts = [
        {"id": "a1", "attempt_number": 1, "tokens_used": 1_000_000,
         "models": {"coder": "claude-sonnet-5"}},
        {"id": "a2", "attempt_number": 2, "tokens_used": 1_000_000,
         "models": {"coder": "gpt-5.3-codex"}},
    ]
    s = TaskSummaryOut.from_task(t, attempts=attempts)
    assert s.cost_usd == pytest.approx(3.0 + 1.75)
    assert s.cost_model == "mixed"
    # No attempts yet -> None, not 0.0 — "no attempts" and "attempts that
    # spent nothing" are different facts and must not both render as 0.
    assert TaskSummaryOut.from_task(t, attempts=None).cost_usd is None
    assert TaskSummaryOut.from_task(t, attempts=None).cost_model is None


def test_task_out_cost_matches_task_summary_cost():
    """The drawer (TaskOut) and the board card (TaskSummaryOut) must never
    disagree about the same task's price — both call attempts_cost on the
    same attempt rows, never a local computation of their own."""
    from no_human.api.models import TaskOut, TaskSummaryOut
    from no_human.core.task import Task
    t = Task.new("x", repo_path="/tmp/r")
    attempts = [
        {"id": "a1", "attempt_number": 1, "tokens_used": 2_000_000,
         "cache_read_tokens": 500_000, "models": {"coder": "claude-sonnet-5"}},
    ]
    out = TaskOut.from_task(t, attempts)
    s = TaskSummaryOut.from_task(t, attempts=attempts)
    # weighted = 2,000,000 fresh (x1.0) + 500,000 cache-read (x0.1) = 2,050,000
    # dollars = 2,050,000 * $3/Mtok = 6.15
    assert out.cost_usd == s.cost_usd == pytest.approx(6.15)
    assert out.cost_model == s.cost_model == "claude-sonnet-5"


def test_task_cost_model_is_mixed_when_roles_disagree():
    from no_human.api.models import TaskOut
    from no_human.core.task import Task
    t = Task.new("x", repo_path="/tmp/r")
    attempts = [
        {"id": "a1", "attempt_number": 1, "tokens_used": 1_000_000,
         "review_tokens_used": 1_000_000,
         "models": {"coder": "gpt-5.3-codex", "reviewer": "claude-opus-4-8"}},
    ]
    out = TaskOut.from_task(t, attempts)
    assert out.cost_model == "mixed"


async def test_metrics_cost_usd_total_equals_the_sum_of_task_costs(tmp_path):
    """core.metrics.compute_metrics's cost_usd_total must price the whole
    install with the SAME function (core.cost.attempts_cost) TaskSummaryOut
    uses per task — summing every task's own cost_usd must equal the
    lifetime figure the North Star lifetime tile renders, or the board card
    and the lifetime tile disagree (the exact class of bug this rebuild
    exists to prevent)."""
    from no_human.api.models import TaskSummaryOut
    from no_human.core.db import Store
    from no_human.core.metrics import compute_metrics
    from no_human.core.task import Task

    store = await Store(tmp_path / "nh.db").connect()
    try:
        t1 = Task.new("a", repo_path="/tmp/a")
        await store.create_task(t1)
        a1 = await store.create_attempt(t1.id, attempt_number=1)
        await store.update_attempt(
            a1, tokens_used=1_000_000, models={"coder": "claude-sonnet-5"})

        t2 = Task.new("b", repo_path="/tmp/b")
        await store.create_task(t2)
        a2 = await store.create_attempt(t2.id, attempt_number=1)
        await store.update_attempt(
            a2, tokens_used=1_000_000, models={"coder": "gpt-5.3-codex"})

        m = await compute_metrics(store)

        s1 = TaskSummaryOut.from_task(t1, attempts=await store.list_attempts(t1.id))
        s2 = TaskSummaryOut.from_task(t2, attempts=await store.list_attempts(t2.id))

        assert m["cost_usd_total"] == pytest.approx(s1.cost_usd + s2.cost_usd)
        assert m["cost_usd_total"] == pytest.approx(3.0 + 1.75)
        assert m["cost_model_total"] == "mixed"
    finally:
        await store.close()


def test_task_out_withholds_a_failure_reason_that_would_be_a_lie():
    """Two cases where a plausible string is worse than nothing.

    A task that failed attempt 1 and passed attempt 2 is not failed, and an
    operator CANCEL is stored as `failed` plus a `cancel_reason` — the last
    attempt's reason there describes interrupted work, under a heading that
    claims the task failed. Same distinction `cli/commands.py` and
    `api/app.py` already make at their read sites.
    """
    from no_human.api.models import TaskOut
    from no_human.core.task import Task, TaskStatus
    attempts = [{"id": "a1", "attempt_number": 1, "status": "failed",
                 "failure_reason": "an interrupted attempt"}]

    done = Task.new("x", repo_path="/tmp/r")
    done.status = TaskStatus.DONE
    assert TaskOut.from_task(done, attempts).failure_reason is None

    cancelled = Task.new("y", repo_path="/tmp/r")
    cancelled.status = TaskStatus.FAILED
    cancelled.context = {"cancel_reason": "operator stopped it"}
    assert TaskOut.from_task(cancelled, attempts).failure_reason is None

    # Control: same attempts, genuinely failed, no cancel -> it DOES surface,
    # so the two None results above are the guards firing and not a dead path.
    failed = Task.new("z", repo_path="/tmp/r")
    failed.status = TaskStatus.FAILED
    assert TaskOut.from_task(failed, attempts).failure_reason == "an interrupted attempt"


#: What the API answers when `web/dist` is missing (``api/app.py``'s
#: ``_NO_BOARD_MESSAGE``). It is a **200**, not a 404: the API and worker are
#: fine, only the UI is absent, so the route explains itself rather than lying
#: about the resource.
_NO_BOARD_SENTINEL = b"no_human: the web board is not installed"


def _skip_without_a_built_board(r) -> None:
    """Skip — loudly — when this checkout has no built board.

    `web/dist` is a gitignored build artifact, so CI's Python job (which runs
    `uv sync` and no npm) and every fresh clone have none. These three tests
    guarded for that with ``if r.status_code == 404: return``, and the board is
    absent as a **200** carrying the explainer above, so the guard never fired:
    CI went red on all three from the commit that added the first one, while
    they passed locally where `web/dist` exists.

    A bare ``return`` is the other half of the bug — a skipped test that reports
    as a PASS. Three real assertions could have been retired and the suite would
    have looked identical. `pytest.skip` says which assertion did not run.
    """
    if r.status_code == 404 or _NO_BOARD_SENTINEL in r.content:
        pytest.skip(
            "no built board in this checkout, so the board-serving routes "
            "cannot be exercised — this assertion DID NOT RUN. Build it with "
            "`cd web && npm install && npm run build`."
        )


async def test_spa_index_is_no_cache(client):
    """index.html must carry Cache-Control: no-cache — without it Chromium's
    heuristic freshness serves a stale app shell after every deploy (found
    live: the desktop shell ran a bundle two deploys old). Hashed /assets
    stay long-cacheable; only the entry document revalidates."""
    r = await client.get("/")
    _skip_without_a_built_board(r)
    assert r.headers.get("cache-control") == "no-cache"


async def test_root_level_static_files_are_served_not_the_app_shell(client):
    """A file at the ROOT of the board directory must be served as itself.

    Vite copies `web/public/` to the root of dist, not under /assets, and only
    /assets was mounted — so a root-level file fell through to the SPA
    catch-all. The installed app answered /nh-mark-64.png with 601 bytes of
    index.html: its own favicon was broken for every user while every content
    check passed, because the file was present, built and bundled, and simply
    unreachable. Found by running the DMG, not by inspecting it.
    """
    r = await client.get("/nh-mark-64.png")
    _skip_without_a_built_board(r)
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n", (
        "served something other than the PNG: " + repr(r.content[:40]))


async def test_the_static_route_cannot_escape_the_board_directory(client):
    """Mutation guard for the test above.

    That test asserts a positive, so it would still pass if the handler served
    ANY path off disk. `path` is caller-controlled, so serving it unresolved
    reads outside the board directory. A traversal must fall through to the app
    shell, not return a file.
    """
    r = await client.get("/../../../etc/passwd")
    _skip_without_a_built_board(r)
    assert b"root:" not in r.content, "path traversal served a file outside the board"
    assert r.content.lstrip()[:9].lower() == b"<!doctype", (
        "a traversal should fall through to the app shell")


# --------------------- B2 #9/#10 board-truthfulness ------------------------ #

async def test_task_fingerprint_sees_every_card_field(store):
    """B2 #10: the old (status, updated_at, live_status) tuple missed
    subtask_progress/pr_url/attempt_count — a stale card the snapshot pushes
    could never repair. The hash must move when ANY summary field moves."""
    from types import SimpleNamespace

    from no_human.api.app import _task_fingerprint

    def T(**over):
        base = dict(id="t1", status="implementing", updated_at="x",
                    live_status="running", pr_url=None, attempt_count=1,
                    subtask_progress=None, cancelled=False)
        base.update(over)
        return SimpleNamespace(model_dump=lambda b=base: dict(b), id=base["id"])

    fp0 = _task_fingerprint([T()])
    assert _task_fingerprint([T(pr_url="https://x/pr/1")]) != fp0
    assert _task_fingerprint([T(attempt_count=2)]) != fp0
    assert _task_fingerprint([T(subtask_progress="2/3")]) != fp0
    assert _task_fingerprint([T(cancelled=True)]) != fp0
    assert _task_fingerprint([T()]) == fp0   # stable when nothing moved


async def test_connmgr_serializes_sends_and_survives_concurrency():
    """B2 #9: two writers per socket corrupted frames → silent death behind
    a green "Connected". All sends go through one per-socket lock."""
    import asyncio as aio

    from no_human.api.app import _ConnMgr

    class FakeWS:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.sent = []
        async def accept(self): ...
        async def send_text(self, text):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await aio.sleep(0.005)
            self.sent.append(text)
            self.active -= 1

    mgr = _ConnMgr()
    ws = FakeWS()
    await mgr.connect(ws)
    await aio.gather(*[mgr.send(ws, f"m{i}") for i in range(8)],
                     mgr.broadcast({"type": "sync"}))
    assert ws.max_active == 1, "sends interleaved — the lock is not held"
    assert len(ws.sent) == 9
    mgr.remove(ws)
    assert id(ws) not in mgr._locks


async def test_summary_carries_review_totals(client, store):
    """B2 #12: TaskTable priced review fields the list endpoint never sent
    (undefined→0 dropped the review gate from every row)."""
    from no_human.core.task import Task

    t = Task.new("priced", repo_path="/r")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, tokens_used=100, cache_read_tokens=1000,
                               review_tokens_used=40,
                               review_cache_read_tokens=400,
                               review_cache_creation_tokens=4)
    r = await client.get("/api/tasks")
    row = [x for x in r.json() if x["id"] == t.id][0]
    assert row["total_review_tokens"] == 40
    assert row["total_review_cache_read"] == 400
    assert row["total_review_cache_creation"] == 4


async def test_detail_carries_aux_totals_matching_list(client, store):
    """The drawer (GET /api/tasks/{id}) priced only 6 of the 9 buckets
    web/src/cost.js reads: total_aux_* were absent → undefined → 0, so the
    drawer under-reported the run vs the board card for the same task."""
    from no_human.core.task import Task

    t = Task.new("priced aux", repo_path="/r")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid,
        tokens_used=100, cache_read_tokens=1000, cache_creation_tokens=10,
        review_tokens_used=40, review_cache_read_tokens=400, review_cache_creation_tokens=4,
        plan_tokens_used=7, plan_cache_read_tokens=70, plan_cache_creation_tokens=3,
        utility_tokens_used=5, utility_cache_read_tokens=50, utility_cache_creation_tokens=2,
    )

    detail = (await client.get(f"/api/tasks/{t.id}")).json()
    row = [x for x in (await client.get("/api/tasks")).json() if x["id"] == t.id][0]

    for key, expected in (
        ("total_aux_tokens", 12),
        ("total_aux_cache_read", 120),
        ("total_aux_cache_creation", 5),
    ):
        assert key in detail
        assert detail[key] is not None
        assert isinstance(detail[key], int)
        assert detail[key] == expected
        assert detail[key] == row[key]

    # Same nine-bucket sum web/src/cost.js's taskBurn/taskCost compute, checked
    # identical between the drawer and the board card for the same task.
    def _nine_bucket_sum(payload):
        keys = (
            "total_tokens", "total_cache_creation", "total_cache_read",
            "total_review_tokens", "total_review_cache_creation", "total_review_cache_read",
            "total_aux_tokens", "total_aux_cache_creation", "total_aux_cache_read",
        )
        return sum(payload.get(k) or 0 for k in keys)

    assert _nine_bucket_sum(detail) == _nine_bucket_sum(row)


async def test_worker_status_surfaces_watcher_error(client):
    """B2 #13: a dead WakeWatcher used to be swallowed silently while parked
    tasks (notify-silent by design) depended on it entirely to wake."""
    from no_human.api.app import app as _app
    _app.state.watcher_error = "boom: config"
    r = await client.get("/api/worker/status")
    assert r.json().get("watcher_error") == "boom: config"


async def test_worker_status_surfaces_the_loaded_code(client):
    """The server never reloads, so a merged fix is not live until restart.
    The status line says WHICH code is answering — advisory only; nothing here
    gates a claim (HEAD moves constantly, so a block would halt the loop)."""
    from no_human.api.app import app as _app
    # `app` is a module-level singleton shared by every test through the
    # `client` fixture, so this must be put back — an attribute left behind
    # here is a cross-test dependency waiting for someone to add an assertion.
    sentinel = object()
    previous = getattr(_app.state, "loaded_code", sentinel)
    try:
        _app.state.loaded_code = "git:" + "a" * 40
        r = await client.get("/api/worker/status")
    finally:
        if previous is sentinel:
            _app.state._state.pop("loaded_code", None)
        else:
            _app.state.loaded_code = previous
    body = r.json()
    assert body.get("loaded_code") == "git:" + "a" * 40
    # Present even when there is nothing to report, so a reader can tell
    # "checked, current" apart from "never checked".
    assert "loaded_code_stale" in body


async def test_advancing_head_after_startup_flips_the_stale_flag(
        client, tmp_path, monkeypatch):
    """A cached current answer must not survive a checkout HEAD advance."""
    build_info = importlib.import_module("no_human.core.build_info")
    api = importlib.import_module("no_human.api.app")
    repo = tmp_path / "repo"
    package = repo / "src" / "no_human"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("x = 1\n")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    old = _git(repo, "rev-parse", "HEAD")

    monkeypatch.setattr(build_info, "_PACKAGE_ROOT", repo)
    monkeypatch.setattr(build_info, "_SNAPSHOT", LoadedCode(sha=old, dirty=False))
    monkeypatch.setattr(api, "_stale_cache", None)

    assert (await client.get("/api/worker/status")).json()["loaded_code_stale"] is None

    (repo / "newer.txt").write_text("newer\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "advance HEAD")
    new = _git(repo, "rev-parse", "HEAD")

    stale = (await client.get("/api/worker/status")).json()["loaded_code_stale"]
    assert stale is not None
    assert old[:8] in stale
    assert new[:8] in stale


async def test_queue_health_endpoint(client, store):
    from no_human.core.task import Task, TaskStatus
    t = Task.new("open one", repo_path="/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    r = await client.get("/api/queue/health")
    body = r.json()
    assert body["open_tasks"] == 1
    assert body["stuck"] is True          # nothing completed in the window
    assert body["eta_minutes"] is None    # unknowable, not a fake zero


async def test_queue_health_endpoint_includes_worker_fields(client, store):
    """SCRUM-70: queue_health merges workers_busy/max_workers/queue_depth/
    est_drain_seconds from the scheduler's in-memory state, no nesting."""
    from types import SimpleNamespace

    from no_human.api.app import app as fastapi_app
    from no_human.core.task import Task, TaskStatus

    t = Task.new("claimed one", repo_path="/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    fastapi_app.state.scheduler = SimpleNamespace(inflight={t.id}, max_workers=2)
    try:
        r = await client.get("/api/queue/health")
    finally:
        del fastapi_app.state.scheduler

    body = r.json()
    assert isinstance(body["workers_busy"], int) and body["workers_busy"] == 1
    assert isinstance(body["max_workers"], int) and body["max_workers"] == 2
    assert isinstance(body["queue_depth"], int) and body["queue_depth"] == 0
    assert body["est_drain_seconds"] is None or isinstance(
        body["est_drain_seconds"], (int, float))
    # pre-existing keys still present, untouched
    assert body["open_tasks"] == 1
    assert "eta_minutes" in body


async def test_queue_health_endpoint_reports_quota_pause(client, store):
    """2026-08-20 evidence: `/api/queue/health` reported "not stuck, 0 busy,
    7 queued, ETA 210 min" while the pool was in its quota cooldown. This
    proves the full endpoint wiring (app.py reading sched.quota_cooldown_until
    and the most recent paused_quota blocker), not just queue_health() in
    isolation — a scheduler test double that predates this field (see
    test_queue_health_endpoint_includes_worker_fields) must also keep
    working, which is why app.py must use getattr with a default."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from no_human.api.app import app as fastapi_app
    from no_human.core.task import Task, TaskStatus

    for i in range(7):
        t = Task.new(f"queued-{i}", repo_path="/r")
        await store.create_task(t)

    parked = Task.new("parked one", repo_path="/r")
    await store.create_task(parked)
    await store.set_status(parked, TaskStatus.PAUSED_QUOTA, validate=False)
    parked.blocker = {"auth_profile": "personal2"}
    await store.update_task(parked)

    reset_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    fastapi_app.state.scheduler = SimpleNamespace(
        inflight=set(), max_workers=4, quota_cooldown_until=reset_at)
    try:
        r = await client.get("/api/queue/health")
    finally:
        del fastapi_app.state.scheduler

    body = r.json()
    assert body["paused"] is True
    assert body["paused_reason"] == "quota"
    assert body["paused_until"] == reset_at.isoformat()
    assert body["paused_profile"] == "personal2"
    assert body["stuck"] is False, "a quota wall is a deliberate pause, not a wedge"


async def test_queue_health_endpoint_reports_infra_pause_without_profile(client, store):
    """Independent review of PR #553 (2026-08-21): the infra breaker (3
    consecutive zero-token/auth SDK failures) arms the same cooldown clock a
    quota park does. The endpoint must read `sched.infra_cooldown_until` and
    report `paused_reason == "infra"` with no profile attribution — even
    though a stale, unrelated `paused_quota` row exists and would otherwise
    be misattributed as the cause."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from no_human.api.app import app as fastapi_app
    from no_human.core.task import Task, TaskStatus

    for i in range(7):
        t = Task.new(f"queued-{i}", repo_path="/r")
        await store.create_task(t)

    # Stale, unrelated park — must NOT be blamed for the breaker trip.
    parked = Task.new("parked one", repo_path="/r")
    await store.create_task(parked)
    await store.set_status(parked, TaskStatus.PAUSED_QUOTA, validate=False)
    parked.blocker = {"auth_profile": "personal2"}
    await store.update_task(parked)

    reset_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    fastapi_app.state.scheduler = SimpleNamespace(
        inflight=set(), max_workers=4,
        quota_cooldown_until=None, infra_cooldown_until=reset_at)
    try:
        r = await client.get("/api/queue/health")
    finally:
        del fastapi_app.state.scheduler

    body = r.json()
    assert body["paused"] is True
    assert body["paused_reason"] == "infra"
    assert body["paused_until"] == reset_at.isoformat()
    assert body["paused_profile"] is None
    assert body["stuck"] is False, "a breaker cooldown is a deliberate pause, not a wedge"


async def test_board_query_is_not_n_plus_1(client, store, monkeypatch):
    """B2 #16: the board issued one attempts query PER TASK, every 2s, per
    socket. It must now use a single grouped query regardless of task count."""
    from no_human.core.task import Task

    for i in range(5):
        t = Task.new(f"t{i}", repo_path="/r")
        await store.create_task(t)
        await store.create_attempt(t.id, 1)

    calls = {"per_task": 0, "grouped": 0}
    real_list = store.list_attempts
    real_grouped = store.attempts_by_task

    async def counted_list(task_id):
        calls["per_task"] += 1
        return await real_list(task_id)

    async def counted_grouped():
        calls["grouped"] += 1
        return await real_grouped()

    monkeypatch.setattr(store, "list_attempts", counted_list)
    monkeypatch.setattr(store, "attempts_by_task", counted_grouped)

    r = await client.get("/api/tasks")
    assert r.status_code == 200
    assert len(r.json()) == 5
    assert calls["grouped"] == 1
    assert calls["per_task"] == 0, "the board still issues a query per task"


# --------------------------------------------------------------------------- #
# Approve completes an already-satisfied claim (no PR exists to merge)         #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_approve_completes_an_already_satisfied_task(client, store):
    """PR #101 review HIGH: with no PR, 'merge it yourself' is a dead end —
    approval IS the human confirmation the terminal promised, so it completes
    the task."""
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await store.merge_context(t.id, {"already_satisfied_report":
        "ALREADY-SATISFIED\nCRITERION: Should work — MET — evidence: x.py:1"})
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "already satisfied" in data["message"].lower()
    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.DONE
    assert refreshed.context.get("approved_at") is not None
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "approved_already_satisfied" for e in events)


@pytest.mark.asyncio
async def test_approve_with_a_pr_still_leaves_the_merge_to_the_human(client, store):
    """The PR path is unchanged: approval recorded, status stays
    awaiting_approval, the human merges."""
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200
    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_approve_with_a_stale_claim_and_a_real_pr_does_not_auto_done(client, store):
    """PR #101 round-2 MEDIUM: a stale already_satisfied_report + a later real
    PR must keep the normal merge flow, never a false DONE."""
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await store.merge_context(t.id, {"already_satisfied_report":
        "ALREADY-SATISFIED\nCRITERION: Should work — MET — evidence: x.py:1"})
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, pr_url="https://github.com/o/r/pull/7")
    r = await client.post(f"/api/tasks/{t.id}/approve")
    assert r.status_code == 200
    assert "merge the pr" in r.json()["message"].lower()
    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# Content-Security-Policy (fonts+CSP increment)                                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_responses_carry_a_strict_csp(client):
    """The board is self-contained (self-hosted fonts, data: favicon, ws
    socket) — every response declares it, so an injected external script or
    style can never load (electron-pro checklist gap #1)."""
    r = await client.get("/api/tasks")
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp and "unsafe-eval" not in csp
    assert "img-src 'self' data:" in csp
    assert "font-src 'self'" in csp
    assert "connect-src 'self' ws: wss:" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp

# The board WebSocket actually routes (PR #107 review found it dead)           #
# --------------------------------------------------------------------------- #

def _ws_shim(store):
    """A bare app exposing the PRODUCTION ws_board coroutine over the fixture
    store — NO lifespan. PR #109's reviewer proved the first draft's
    TestClient(app) booted the real lifespan: the operator's config/DB and a
    live Scheduler ticking against their queue. Never again."""
    from fastapi import FastAPI
    from no_human.api.app import ws_board
    shim = FastAPI()
    shim.state.store = store
    shim.websocket("/ws")(ws_board)
    return shim


@pytest.mark.asyncio
async def test_board_websocket_routes_and_sends_the_init_snapshot(store):
    """Since PR #75 the @app.websocket('/ws') decorator sat on the
    _task_fingerprint HELPER, so FastAPI routed the helper (rejecting every
    connection with 'missing query tlist') and ws_board never ran. On the
    production app the route must be ws_board; the shim proves the coroutine
    serves the init snapshot from the wired store."""
    from starlette.testclient import TestClient
    from no_human.api.app import app as prod_app, ws_board
    # 1. The production route table binds /ws to ws_board (the regression).
    ws_routes = [r for r in prod_app.routes if getattr(r, "path", "") == "/ws"]
    assert ws_routes and ws_routes[0].endpoint is ws_board
    # 2. The coroutine itself, over the fixture store, no lifespan.
    t = await _seed_task(store, status=TaskStatus.PENDING, title="ws smoke")
    # host: loopback — TestClient hardcodes the ws Host to "testserver", which
    # the ws_board origin/host gate refuses; a real browser sends the true host.
    with TestClient(_ws_shim(store)) as tc:
        with tc.websocket_connect("/ws", headers={"host": "localhost"}) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "init"
            assert any(x["title"] == "ws smoke" for x in msg["tasks"])


@pytest.mark.asyncio
async def test_board_websocket_disconnect_ends_the_poll_loop(store, monkeypatch):
    """PR #109 review (medium): ws_board never received, so a disconnect on an
    IDLE board (nothing to send, nothing to raise) leaked the loop polling the
    store every 2s per closed tab, forever. This drives the PRODUCTION
    coroutine directly (a TestClient portal freezes the server coroutine
    outside the ws context, which made the first draft of this test pass on
    the UNFIXED loop — vacuous): a fake socket whose receive() resolves when
    the client closes; after that, the store must not be polled again."""
    import asyncio
    from types import SimpleNamespace
    from no_human.api.app import ws_board

    polls = {"n": 0}
    real_list = store.list_tasks

    async def counting(*a, **kw):
        polls["n"] += 1
        return await real_list(*a, **kw)
    monkeypatch.setattr(store, "list_tasks", counting, raising=False)

    closed = asyncio.Event()

    class _FakeWS:
        app = SimpleNamespace(state=SimpleNamespace(store=store, scheduler=None))
        headers = {"host": "localhost"}   # loopback: pass the ws_board origin/host gate

        async def accept(self):
            return None

        async def send_text(self, _):
            return None

        async def receive(self):
            await closed.wait()
            return {"type": "websocket.disconnect", "code": 1000}

        async def close(self):
            return None

    # Speed the poll clock up 20x so the test observes multiple windows fast.
    real_wait = asyncio.wait

    async def fast_wait(fs, timeout=None, **kw):
        return await real_wait(fs, timeout=(0.1 if timeout == 2 else timeout), **kw)
    from importlib import import_module
    # `import no_human.api.app as m` binds the parent package's `app`
    # ATTRIBUTE (the FastAPI instance) — import_module gets the module.
    app_mod = import_module("no_human.api.app")
    monkeypatch.setattr(app_mod.asyncio, "wait", fast_wait)

    task = asyncio.create_task(ws_board(_FakeWS()))
    await asyncio.sleep(0.35)          # a few poll windows while connected
    assert polls["n"] >= 2, "harness sanity: the loop must be polling"
    closed.set()                        # client disconnects
    await asyncio.wait_for(task, timeout=2)  # the coroutine must END
    baseline = polls["n"]
    await asyncio.sleep(0.5)            # five would-be windows
    assert polls["n"] == baseline, (
        f"poll loop kept running after disconnect: {polls['n'] - baseline} extra")


@pytest.mark.asyncio
async def test_board_websocket_disconnect_deregisters_the_socket(store, monkeypatch):
    """PR #109 round-2 (low): the normal-disconnect return path skipped
    _mgr.remove, so closed tabs accumulated inert socket+lock entries until
    the next broadcast pruned them."""
    import asyncio
    from types import SimpleNamespace
    from importlib import import_module
    app_mod = import_module("no_human.api.app")

    closed = asyncio.Event()

    class _FakeWS:
        app = SimpleNamespace(state=SimpleNamespace(store=store, scheduler=None))
        headers = {"host": "localhost"}   # loopback: pass the ws_board origin/host gate
        async def accept(self): return None
        async def send_text(self, _): return None
        async def receive(self):
            await closed.wait()
            return {"type": "websocket.disconnect", "code": 1000}
        async def close(self): return None

    ws = _FakeWS()
    task = asyncio.create_task(app_mod.ws_board(ws))
    await asyncio.sleep(0.1)
    assert ws in app_mod._mgr._sockets   # registered while connected
    closed.set()
    await asyncio.wait_for(task, timeout=3)
    assert ws not in app_mod._mgr._sockets
    assert id(ws) not in app_mod._mgr._locks


@pytest.mark.asyncio
async def test_board_websocket_pushes_a_sync_frame_on_change(store, monkeypatch):
    """No sync-frame coverage existed anywhere (#109 round-2, info): mutate
    the store mid-connection and the loop must push a type:'sync' frame."""
    import asyncio, json as _json
    from types import SimpleNamespace
    from importlib import import_module
    app_mod = import_module("no_human.api.app")

    sent = []
    closed = asyncio.Event()

    class _FakeWS:
        app = SimpleNamespace(state=SimpleNamespace(store=store, scheduler=None))
        headers = {"host": "localhost"}   # loopback: pass the ws_board origin/host gate
        async def accept(self): return None
        async def send_text(self, text): sent.append(_json.loads(text))
        async def receive(self):
            await closed.wait()
            return {"type": "websocket.disconnect", "code": 1000}
        async def close(self): return None

    real_wait = asyncio.wait
    async def fast_wait(fs, timeout=None, **kw):
        return await real_wait(fs, timeout=(0.1 if timeout == 2 else timeout), **kw)
    monkeypatch.setattr(app_mod.asyncio, "wait", fast_wait)

    task = asyncio.create_task(app_mod.ws_board(_FakeWS()))
    await asyncio.sleep(0.15)
    assert sent and sent[0]["type"] == "init"
    await _seed_task(store, status=TaskStatus.PENDING, title="sync me")
    await asyncio.sleep(0.4)
    closed.set()
    await asyncio.wait_for(task, timeout=3)
    syncs = [m for m in sent if m["type"] == "sync"]
    assert syncs, f"no sync frame pushed; frames: {[m['type'] for m in sent]}"
    assert any(t["title"] == "sync me" for t in syncs[-1]["tasks"])


# --------------------------------------------------------------------------- #
# GET /api/integrations · POST /api/integrations/{name}/test                   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_integrations_list_endpoint(client, mock_ambient_probes):
    r = await client.get("/api/integrations")
    assert r.status_code == 200
    items = r.json()["integrations"]
    assert [i["name"] for i in items] == ["jira", "linear", "monday", "github",
                                          "gitlab", "jenkins", "circleci",
                                          "slack", "teams"]
    assert all(i["configured"] is False for i in items)   # default config
    assert all(i["healthy"] is None for i in items)       # not tested yet


@pytest.mark.asyncio
async def test_integrations_list_includes_status_field(client, mock_ambient_probes):
    # SCRUM-81: the payload carries a tri-state `status` per provider —
    # 'configured' | 'ambient' | 'unconfigured' — not just the boolean
    # `configured` flag. Force a deterministic ambient result so this doesn't
    # depend on whether `gh`/`git` happen to be authenticated on the runner.
    mock_ambient_probes._AMBIENT_PROBES["github"] = lambda: True
    r = await client.get("/api/integrations")
    assert r.status_code == 200
    items = {i["name"]: i for i in r.json()["integrations"]}
    assert items["github"]["status"] == "ambient"
    assert items["gitlab"]["status"] == "unconfigured"
    for name in ("jira", "linear", "monday", "jenkins", "circleci", "slack", "teams"):
        assert items[name]["status"] == "unconfigured"


@pytest.mark.asyncio
async def test_integrations_list_never_leaks_the_slack_webhook(client, mock_ambient_probes):
    from no_human.api.app import app
    app.state.config.data["notifications"]["slack_webhook_url"] = "https://hooks.slack.com/T/SECRETPART"
    r = await client.get("/api/integrations")
    assert "SECRETPART" not in r.text
    slack = next(i for i in r.json()["integrations"] if i["name"] == "slack")
    assert slack["configured"] is True
    assert "SECRETPART" not in slack["detail"]


@pytest.mark.asyncio
async def test_integration_test_endpoint_unconfigured_jira(client):
    r = await client.post("/api/integrations/jira/test",
                          headers={"Origin": "http://127.0.0.1:8420"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "jira"
    assert body["healthy"] is False
    assert "not configured" in body["detail"].lower()


@pytest.mark.asyncio
async def test_integration_test_endpoint_unknown_name_404(client):
    r = await client.post("/api/integrations/mystery/test",
                          headers={"Origin": "http://127.0.0.1:8420"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reply_park_option_keeps_task_parked(client, store):
    """SCRUM-22: choosing the terminal stop option must keep the task in its
    parked state — never resume it into the claim queue."""
    from no_human.core.task import Task, TaskStatus

    t = Task.new("budget-parked", repo_path="/tmp/x")
    await store.create_task(t)
    t.blocker = {
        "category": "BUDGET_EXHAUSTED",
        "question": "Spend more, or stop here?",
        "options": [
            {"label": "raise", "action": {"set_task_config": {"lifetime_tokens": 13_000_000}}},
            {"label": "stop \u2014 keep the work parked as-is", "action": {"park": True}},
        ],
    }
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.ESCALATED, validate=False)

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "", "choose": 2})
    assert r.status_code == 200, r.text
    assert r.json().get("kept_parked") is True

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED          # NOT implementing
    replies = (fresh.context or {}).get("human_replies") or []
    assert replies and "stop" in replies[-1]["answer"]
    assert fresh.config == {}                            # park mutated nothing
    # Review 2026-07-25: the stop must be durable against the wake watcher's
    # sweep — the stamp is what _evaluate checks to skip max_park
    # re-escalation and wake_condition resumes.
    assert (fresh.blocker or {}).get("human_stopped") is True


@pytest.mark.asyncio
async def test_board_tasks_carry_claimed_from_scheduler(client, store):
    """SCRUM-15: `claimed` mirrors the scheduler's in-flight set — an
    active-status task the scheduler has not picked up is queued, not running."""
    from types import SimpleNamespace

    from no_human.api.app import _board_tasks
    from no_human.core.task import Task, TaskStatus

    a = Task.new("claimed one", repo_path="/tmp/x")
    b = Task.new("waiting one", repo_path="/tmp/x")
    await store.create_task(a)
    await store.create_task(b)
    await store.set_status(a, TaskStatus.IMPLEMENTING, validate=False)
    await store.set_status(b, TaskStatus.IMPLEMENTING, validate=False)

    sched = SimpleNamespace(inflight={a.id}, get_live_status=lambda _id: None)
    out = {s.id: s for s in await _board_tasks(store, scheduler=sched)}
    assert out[a.id].claimed is True
    assert out[b.id].claimed is False

    # No scheduler (CLI/board contexts without one): claimed defaults False.
    out2 = {s.id: s for s in await _board_tasks(store)}
    assert out2[a.id].claimed is False


@pytest.mark.asyncio
async def test_mutation_broadcast_payload_carries_claimed(client, store, monkeypatch):
    """SCRUM-15 regression net: the round-2 internal review caught a mutation
    broadcast whose tasks payload silently dropped `claimed` (a callsite
    without scheduler=). Pin the key's presence in a real broadcast payload."""
    from no_human.api.app import _mgr

    captured: list[dict] = []

    async def fake_broadcast(msg):
        captured.append(msg)

    monkeypatch.setattr(_mgr, "broadcast", fake_broadcast)
    r = await client.post("/api/tasks", json={"title": "broadcast probe"})
    assert r.status_code == 201
    created_id = r.json()["id"]

    # Value-level pin (fresh-review advisory): a fake scheduler claiming the
    # task must surface claimed=True in the NEXT mutation broadcast — key
    # presence alone would still pass if a callsite dropped scheduler=.
    from types import SimpleNamespace

    from no_human.api.app import app as fastapi_app
    monkeypatch.setattr(
        fastapi_app.state, "scheduler",
        SimpleNamespace(inflight={created_id}, get_live_status=lambda _id: None),
        raising=False)
    captured.clear()
    r2 = await client.post(f"/api/tasks/{created_id}/pause", json={})
    assert r2.status_code == 200, r2.text
    tasks_payload = captured[-1].get("tasks") or []
    assert tasks_payload, "broadcast carries the board tasks"
    by_id = {t["id"]: t for t in tasks_payload}
    assert by_id[created_id]["claimed"] is True, (
        "a scheduler-claimed task must broadcast claimed=True — a dropped "
        "scheduler= callsite silently flattens it to False")


@pytest.mark.asyncio
async def test_task_detail_carries_claimed_from_scheduler(client, store):
    """SCRUM-16: the slide-over's detail payload gets the same claimed
    contract as the board summaries — live session vs queued."""
    from types import SimpleNamespace

    from no_human.api.app import app as fastapi_app
    from no_human.core.task import Task, TaskStatus

    t = Task.new("detail claimed", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    r0 = await client.get(f"/api/tasks/{t.id}")
    assert r0.json()["claimed"] is False   # no scheduler → default

    fastapi_app.state.scheduler = SimpleNamespace(
        inflight={t.id}, get_live_status=lambda _id: None)
    try:
        r1 = await client.get(f"/api/tasks/{t.id}")
        assert r1.json()["claimed"] is True
    finally:
        del fastapi_app.state.scheduler


@pytest.mark.asyncio
async def test_summary_carries_pr_conflict_rounds_from_context(client, store):
    """SCRUM-42: the card's 'resolving merge conflict' badge is driven by the
    summary's pr_conflict_rounds mirror of SCRUM-41's context key — never by
    feedback-text matching. Absent key -> 0."""
    a = Task.new("conflicting", repo_path="/tmp/x")
    a.context = {"pr_conflict_rounds": 2}
    await store.create_task(a)
    b = Task.new("clean", repo_path="/tmp/x")
    await store.create_task(b)

    r = await client.get("/api/tasks")
    assert r.status_code == 200
    by_id = {t["id"]: t for t in r.json()}
    assert by_id[a.id]["pr_conflict_rounds"] == 2
    assert by_id[b.id]["pr_conflict_rounds"] == 0


@pytest.mark.asyncio
async def test_corrupt_pr_conflict_rounds_context_never_500s_the_board(client, store):
    """Review 2026-07-25 residue: a corrupt context value ('two', {}, [1])
    must coerce to 0, not take down GET /api/tasks for every card."""
    a = Task.new("corrupt-str", repo_path="/tmp/x")
    a.context = {"pr_conflict_rounds": "two"}
    await store.create_task(a)
    b = Task.new("corrupt-dict", repo_path="/tmp/x")
    b.context = {"pr_conflict_rounds": {"nested": 1}}
    await store.create_task(b)

    r = await client.get("/api/tasks")
    assert r.status_code == 200
    by_id = {t["id"]: t for t in r.json()}
    assert by_id[a.id]["pr_conflict_rounds"] == 0
    assert by_id[b.id]["pr_conflict_rounds"] == 0


@pytest.mark.asyncio
async def test_summary_carries_configured_max_pr_conflict_rounds(client, store):
    """Review 2026-07-25 residue: the board payload must surface the
    configured bound so the badge can render 'round N/M' with real data."""
    t = Task.new("conflicting", repo_path="/tmp/x")
    t.context = {"pr_conflict_rounds": 2}
    await store.create_task(t)

    r = await client.get("/api/tasks")
    assert r.status_code == 200
    by_id = {x["id"]: x for x in r.json()}
    # default bound (blockers.max_pr_conflict_rounds) is 3
    assert by_id[t.id]["max_pr_conflict_rounds"] == 3


@pytest.mark.asyncio
async def test_integrations_list_does_not_block_the_event_loop(client, mock_ambient_probes, monkeypatch):
    """SCRUM-81 regression guard: the ambient overlay shells out to `gh`/`git`
    (subprocess.run, up to 2s per provider). If the handler calls it INLINE
    instead of offloading with asyncio.to_thread, the single-threaded loop
    freezes for the probe's whole duration — SSE, the task list and every other
    request stall with it (measured: 1.52s typical, 15.01s worst case).

    This fails if `await asyncio.to_thread(...)` is ever reverted to a direct
    call, which nothing else in the suite notices.
    """
    import asyncio as _asyncio
    import time as _time

    from no_human import integrations as _reg

    block_s = 0.30
    real = _reg.list_integrations_with_ambient

    def slow(data):
        _time.sleep(block_s)          # blocking, exactly like a real CLI probe
        return real(data)

    monkeypatch.setattr(_reg, "list_integrations_with_ambient", slow)
    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await _asyncio.sleep(0.01)
            ticks += 1

    t = _asyncio.create_task(_ticker())
    try:
        r = await client.get("/api/integrations")
    finally:
        t.cancel()
        await _asyncio.gather(t, return_exceptions=True)

    assert r.status_code == 200
    # Offloaded: the loop keeps servicing other work (~30 ticks in 0.30s).
    # Inline: the loop is frozen for the whole probe and ticks stays ~0.
    assert ticks >= 10, (
        f"event loop stalled during GET /api/integrations (ticks={ticks}); "
        "the blocking ambient probe is not offloaded via asyncio.to_thread"
    )


@pytest.mark.asyncio
async def test_integrations_save_does_not_block_the_event_loop(
    client, mock_ambient_probes, monkeypatch, tmp_path
):
    """Same guarantee as the list endpoint, for the SAVE path.

    save_integration_config overlays an ambient probe that shells out, and the
    endpoint calls it from an `async def`. Without asyncio.to_thread a settings
    save freezes the single-threaded loop for the probe's whole duration
    (measured 1.52s). Nothing else in the suite notices that revert — this is
    the only guard for it.
    """
    # This route writes ~/.no_human/.env and reads ~/.no_human/config.yaml.
    # Redirect BOTH module-level constants off the real store, exactly like
    # tests/test_integrations_write.py::_isolated_paths — without this the
    # assertions below depend on the operator's own config (they pass only on a
    # host where github happens to be unconfigured) and the route is one
    # non-empty payload away from writing the real credential store.
    from no_human import config as nh_config

    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")

    import asyncio as _asyncio
    import time as _time

    from no_human import integrations as _reg

    real = _reg.save_integration_config

    def slow(name, fields):
        _time.sleep(0.30)          # blocking, exactly like a real CLI probe
        return real(name, fields)

    monkeypatch.setattr(_reg, "save_integration_config", slow)
    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await _asyncio.sleep(0.01)
            ticks += 1

    t = _asyncio.create_task(_ticker())
    try:
        # This route is CSRF-guarded (_require_local_origin, writing=True):
        # without a local Origin it 403s BEFORE reaching the function under
        # test, which would make this guard vacuous.
        r = await client.put(
            "/api/integrations/github/config",
            json={"fields": {}},
            headers={"Origin": "http://127.0.0.1:8420"},
        )
    finally:
        t.cancel()
        await _asyncio.gather(t, return_exceptions=True)

    assert r.status_code == 200, r.text
    assert ticks >= 10, (
        f"event loop stalled during the settings save (ticks={ticks}); "
        "the blocking ambient probe is not offloaded via asyncio.to_thread"
    )


@pytest.mark.asyncio
async def test_integration_test_endpoint_reports_ambient_not_unconfigured(
    client, mock_ambient_probes, monkeypatch
):
    """The /test endpoint must AGREE with the list endpoint, not contradict it.

    _check_view previously returned a flat "not configured" for any provider
    without stored credentials, so an ambiently-authenticated github reported
    unconfigured on one surface while the list reported ambient on another —
    the exact "whichever chip you trust, one surface is lying" problem this
    ticket exists to remove.
    """
    monkeypatch.setitem(mock_ambient_probes._AMBIENT_PROBES, "github", lambda: True)
    r = await client.post("/api/integrations/github/test",
                          headers={"Origin": "http://127.0.0.1:8420"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ambient", body
    assert body["healthy"] is None, body
    assert body["detail"] != "not configured", body


@pytest.mark.asyncio
async def test_saving_config_keeps_an_ambient_provider_ambient(
    client, mock_ambient_probes, monkeypatch, tmp_path
):
    """Saving (or clearing) fields must not make an ambiently-authenticated
    provider look worse than the list endpoint already reports it. Without the
    overlay the save response says 'unconfigured', and the client merges that
    into its copy — so the chip silently regresses on save."""
    # This route writes ~/.no_human/.env and reads ~/.no_human/config.yaml.
    # Redirect BOTH module-level constants off the real store, exactly like
    # tests/test_integrations_write.py::_isolated_paths — without this the
    # assertions below depend on the operator's own config (they pass only on a
    # host where github happens to be unconfigured) and the route is one
    # non-empty payload away from writing the real credential store.
    from no_human import config as nh_config

    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")

    monkeypatch.setitem(mock_ambient_probes._AMBIENT_PROBES, "github", lambda: True)
    r = await client.put(
        "/api/integrations/github/config",
        json={"fields": {}},
        headers={"Origin": "http://127.0.0.1:8420"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ambient", r.text


async def test_reply_stamps_HUMAN_provenance_on_the_resume_checkpoint(client, store):
    """The zero-diff honesty gate credits work already ahead of base only when a
    HUMAN gated it, and it reads `resume_from.by`. This endpoint is one of the
    three human writers, so if it does not stamp itself the gate cannot tell this
    answer from a timer-driven wake resume — and, worse, a stale `by: "wake"` from
    an earlier machine resume SURVIVES a write that omits the key, because
    `merge_context` is RFC 7396 and nested dicts merge.

    That latch failed every human resume after any machine resume as fabrication:
    two burnt attempts and a human paged. A review found the whole stamp could be
    deleted from the real writers with the entire suite staying green.
    """
    t = await _seed_task(store, status=TaskStatus.ESCALATED)
    t.blocker = {
        "category": "AMBIGUITY", "question": "which store?",
        "resume_branch": "scratch/x/abc-2", "resume_commit": "75c68e08",
    }
    await store.update_task(t)
    # The residue of an earlier machine resume, which nothing ever clears.
    await store.merge_context(t.id, {
        "resume_reason": "wake_condition_satisfied",
        "resume_from": {"sha": "75c68e08", "branch": "scratch/x/abc-2",
                        "by": "wake"},
    })

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "SQLite only"})
    assert r.status_code == 200, r.text

    resume_from = (await store.find_task(t.id)).context["resume_from"]
    assert resume_from.get("by") == "human", (
        "the reply endpoint did not stamp HUMAN provenance, so a stale machine "
        f"marker survived its write: {resume_from}")


async def test_reply_stamps_provenance_even_with_NO_checkpoint(client, store):
    """The stamp must not be gated on the blocker carrying a `resume_commit`.

    Five review rounds traded one direction of a ONE-WAY LATCH for the other
    before naming the cause: every writer stamped `by` inside `if checkpoint:`,
    so a resume whose own blocker recorded no sha wrote nothing at all, and
    because `merge_context` is RFC 7396 the PREVIOUS actor's `by` survived to
    describe THIS resume. Here a human answers a blocker that has no checkpoint
    while a machine marker is on the row: the honesty gate must see "human".
    """
    t = await _seed_task(store, status=TaskStatus.ESCALATED)
    # A blocker with NO resume_commit — the shape that skipped the write.
    t.blocker = {"category": "AMBIGUITY", "question": "which store?"}
    await store.update_task(t)
    await store.merge_context(t.id, {
        "resume_reason": "wake_condition_satisfied",
        "resume_from": {"sha": "75c68e08", "branch": "scratch/x/abc-2",
                        "by": "wake"},
    })

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "SQLite only"})
    assert r.status_code == 200, r.text

    resume_from = (await store.find_task(t.id)).context["resume_from"]
    assert resume_from.get("by") == "human", (
        "a blocker without a resume_commit skipped the provenance write, so an "
        f"earlier machine resume still describes this human answer: {resume_from}")
    # A review mutated this endpoint back to the round-6 shape and the suite
    # stayed GREEN, because every test here asserted `by` and none asserted
    # `sha`. Relabelling the machine's sha is the fail-OPEN direction.
    assert resume_from.get("sha") is None, (
        f"/reply inherited a sha it never chose and relabelled it: {resume_from}")


async def test_the_drawer_RESUME_button_stamps_human_provenance(client, store):
    """`POST /api/tasks/{id}/resume` is the Resume button in the drawer, and it
    was a resume path that wrote NO provenance at all — found independently by
    a staff review and an architecture audit on the same day.

    Two consequences, both live: a stale `by: "wake"` from any earlier machine
    resume described the human's click, so the zero-diff honesty gate failed the
    next attempt as fabrication (the D15 regression, on the surface friends
    actually use); and the endpoint cleared `task.blocker` without ever reading
    its checkpoint, so the next attempt branched from a stale sha and discarded
    everything the parked attempt had committed. Its CLI twin, `nh task resume`,
    has read the checkpoint since D15.
    """
    t = await _seed_task(store, status=TaskStatus.BLOCKED)
    t.blocker = {"category": "AMBIGUITY", "question": "which store?",
                 "resume_branch": "scratch/x/abc-2", "resume_commit": "75c68e08"}
    await store.update_task(t)
    await store.merge_context(t.id, {
        "resume_reason": "wake_condition_satisfied",
        "resume_from": {"sha": "0e22fe3d", "branch": "old", "by": "wake"},
    })

    r = await client.post(f"/api/tasks/{t.id}/resume")
    assert r.status_code == 200, r.text

    resume_from = (await store.find_task(t.id)).context["resume_from"]
    assert resume_from.get("by") == "human", (
        "the drawer's Resume button wrote no provenance, so an earlier machine "
        f"resume still describes this human's click: {resume_from}")
    assert resume_from.get("sha") == "75c68e08", (
        "the endpoint cleared the blocker without reading its checkpoint, so the "
        f"next attempt branches from a stale sha and loses committed work: {resume_from}")


async def test_the_drawer_RESUME_button_with_no_checkpoint_clears_the_sha(client, store):
    """The Resume button on a blocker that recorded no sha. It must not keep the
    sha a MACHINE resume chose — a review reverted this endpoint to the round-6
    shape and every test still passed, because they all asserted `by` and never
    `sha`."""
    t = await _seed_task(store, status=TaskStatus.BLOCKED)
    t.blocker = {"category": "AMBIGUITY", "question": "which store?"}
    await store.update_task(t)
    await store.merge_context(t.id, {
        "resume_from": {"sha": "0e22fe3d", "branch": "old", "by": "wake"}})

    r = await client.post(f"/api/tasks/{t.id}/resume")
    assert r.status_code == 200, r.text

    resume_from = (await store.find_task(t.id)).context["resume_from"]
    assert resume_from.get("by") == "human", resume_from
    assert resume_from.get("sha") is None, (
        f"the Resume button inherited a sha it never chose: {resume_from}")


async def test_the_drawer_SEND_BACK_button_stamps_human_provenance(client, store):
    """Sending a PR back for revision is a human gate, and the honesty gate has
    to see it as one. This endpoint wrote no provenance either, so on a revision
    branch whose HEAD is a [WIP-PARTIAL] a stale machine marker made the correct
    "nothing more to change" verdict read as fabrication — two burnt attempts.

    No checkpoint is involved here, so the write CLEARS any recorded sha rather
    than relabelling one it never chose — see `resume_provenance`.
    """
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await store.merge_context(t.id, {
        "resume_from": {"sha": "75c68e08", "branch": "scratch/x/abc-2",
                        "by": "wake"},
    })

    r = await client.post(f"/api/tasks/{t.id}/send-back", json={"message": "redo"})
    assert r.status_code == 200, r.text

    resume_from = (await store.find_task(t.id)).context["resume_from"]
    assert resume_from.get("by") == "human", (
        f"send-back left a machine marker describing a human's decision: {resume_from}")
    # 🔴 And it must NOT inherit the sha a MACHINE resume chose. An earlier
    # version of this test asserted the opposite — that the sha survives — and
    # pinned the fail-OPEN regression as correct: `by` said "human" over a sha
    # no human picked, which disarms the zero-diff honesty gate and credits the
    # loop's own abandoned [WIP-PARTIAL]. An independent review reproduced a PR
    # being opened on work no attempt produced. Provenance is a property OF a
    # sha; a write that names no checkpoint clears it.
    assert resume_from.get("sha") is None, (
        "send-back inherited a sha it never chose and relabelled it human — "
        f"this is the fail-OPEN direction: {resume_from}")


async def test_retry_clears_the_resume_checkpoint_so_a_fresh_run_is_fresh(client, store):
    """`POST /retry` promises "a fresh run". It kept `resume_from`, so the retry
    silently branched from a checkpoint some EARLIER actor chose — and if that
    stale pair carried `by: "human"`, the zero-diff honesty gate was disarmed
    for a run no human had gated. An eleventh resume path, found by review.

    Retry means from base. A human who wants to continue from a checkpoint has
    Resume for that.
    """
    t = await _seed_task(store, status=TaskStatus.FAILED)
    await store.merge_context(t.id, {
        "resume_from": {"sha": "75c68e08", "branch": "old", "by": "human"}})

    r = await client.post(f"/api/tasks/{t.id}/retry")
    assert r.status_code == 200, r.text

    ctx = (await store.find_task(t.id)).context or {}
    assert ctx.get("resume_from") is None, (
        "retry inherited a checkpoint it never chose, so a 'fresh run' branches "
        f"from a stale sha: {ctx.get('resume_from')}")


@pytest.mark.parametrize("status", [TaskStatus.CONTEXT, TaskStatus.PLANNING])
async def test_a_retried_checkpoint_stays_cleared_when_the_fresh_run_crashes(
        client, store, status):
    """The clear has to survive the NEXT crash, and clearing the context alone
    did not.

    `Scheduler._recover_orphans` re-derives a checkpoint from the attempt row
    still `in_progress`. `run_task` reaches CONTEXT and PLANNING — both
    orphanable — before it ever calls `create_attempt`, so a fresh run that
    dies in either leaves the PRE-RETRY row as the only row there is, and the
    sweep stamped its sha straight back: the retry's "from base" promise
    silently became "from the checkpoint of the run that already failed".
    Wrong-base work, not fabrication — the stamp carries MACHINE provenance so
    the zero-diff gate stays armed — but a broken documented promise.

    Driven through the real endpoint, not a hand-written `merge_context`: what
    fixes this is `Store.close_open_attempts`, which lives at the clear, so a
    test that simulates the clear itself would be testing its own simulation.
    """
    import time
    from datetime import datetime, timedelta, timezone

    t = await _seed_task(store, status=TaskStatus.FAILED)
    dead = await store.create_attempt(t.id, 1)
    await store.update_attempt(dead, commit_sha="a" * 40)

    from no_human.core.scheduler import Scheduler
    sched = Scheduler(store, lambda task=None: None)

    async def _age(task_id, seconds=3600):
        # The startup/runtime sweep only recovers a row whose activity (row
        # stamp AND newest event) predates the liveness grace window — a
        # `set_status` moments ago, or a prior `_recover_orphans` call's own
        # `orphan_recovered` event, both read as "still live" and are
        # correctly left alone. This test is about checkpoint provenance
        # across two crashes, not liveness detection, so it back-dates both
        # before each sweep.
        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        await store.db.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task_id))
        await store.db.execute(
            "UPDATE task_events SET ts = ? WHERE task_id = ?",
            (time.time() - seconds, task_id))
        await store.db.commit()

    # The crash before the retry: the sweep correctly rescues that work.
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    await _age(t.id)
    await sched._recover_orphans()
    assert ((await store.find_task(t.id)).context or {})["resume_from"]["sha"] == "a" * 40

    await store.set_status(t, TaskStatus.FAILED, validate=False)
    assert (await client.post(f"/api/tasks/{t.id}/retry")).status_code == 200

    # …and the fresh run dies before it ever opens an attempt row of its own.
    await store.set_status(await store.find_task(t.id), status, validate=False)
    await _age(t.id)
    await sched._recover_orphans()

    rf = ((await store.find_task(t.id)).context or {}).get("resume_from") or {}
    assert not rf.get("sha"), (
        f"the cleared checkpoint was resurrected in {status.value} — the retry "
        f"branches from a run that had already failed: {rf!r}")
    assert [a["status"] for a in await store.list_attempts(t.id)] == ["interrupted"], (
        "the pre-retry row is still open, so the next sweep will reach it")


# --------------------------------------------------------------------------- #
# The review gate's verdict reaches the human (both event surfaces)            #
# --------------------------------------------------------------------------- #
#
# Second recurrence of the `watcher` filter gap. `source:"reviewer"` was in
# neither the replay formatter's narration list nor the live stream's, so the
# one event that says whether a task was blocked, and why, was dropped by both.
#
# The source is OVERLOADED — `_emit_review` narrates the gate under it and
# `_reviewer_sink` forwards the reviewer session's raw SDK traffic under the
# same name — so the fix cannot be "add the source". Replaying task
# 84251cb2 (2770 real events, 15 review rounds) through the naive version
# surfaced 164 extra reviewer rows, 139 of them BLANK, burying the verdicts it
# was supposed to reveal.

# One review round as the orchestrator really emits it: the verdict and its
# ladder from `_emit_review`, interleaved with the reviewer session's own SDK
# chatter from `_reviewer_sink`. Kinds and shapes copied from stored events.
_REVIEW_ROUND = [
    {"ts": 1, "source": "orchestrator", "kind": "state", "text": "reviewing"},
    {"ts": 2, "source": "reviewer", "kind": "review_start",
     "text": "running independent staff-level reviewer"},
    {"ts": 3, "source": "reviewer", "kind": "tool_use",
     "tool_name": "Read", "tool_input": {"file_path": "calc.py"}},
    {"ts": 4, "source": "reviewer", "kind": "thinking",
     "text": "the held-out suite was not run"},
    {"ts": 5, "source": "reviewer", "kind": "usage", "text": ""},
    {"ts": 6, "source": "reviewer", "kind": "review",
     "text": "FAIL — 1 blocking, 0 advisory finding(s)\n"
             "  · [high] add() drops the carry (calc.py:12): returns a+b "
             "without the carry bit, so test_add_large fails",
     "passed": False, "failed_count": 1, "blocking_count": 1,
     "advisory_count": 0},
]

# The substrings that ARE the operator's answer to "why was this blocked".
_VERDICT_MARKERS = ("FAIL", "1 blocking", "add() drops the carry", "calc.py:12")


def test_format_events_serves_the_review_verdict_and_not_the_chatter():
    """The verdict, its cited evidence and the ladder around it must survive the
    replay formatter — while the reviewer session's own tool calls and usage
    telemetry (same `source`, empty `text`) must not become blank rows."""
    from no_human.api.app import _format_events

    out = _format_events(_REVIEW_ROUND)
    served = {(e["source"], e["kind"]): e for e in out}

    assert ("reviewer", "review") in served, (
        "the review VERDICT was dropped — a human cannot see why the task was "
        f"blocked without opening the database. served: {sorted(served)}")
    assert ("reviewer", "review_start") in served

    verdict = served[("reviewer", "review")]["text"]
    for marker in _VERDICT_MARKERS:
        assert marker in verdict, (
            f"the verdict reached the surface but {marker!r} did not: {verdict!r}")

    # The other half of the fix: narration only, never the raw session.
    for noisy in ("tool_use", "thinking", "usage"):
        assert ("reviewer", noisy) not in served, (
            f"reviewer SDK {noisy} events leaked into the narration feed — the "
            "verdict is now buried rather than invisible")


async def test_the_live_stream_serves_the_same_verdict_as_the_replay(store):
    """DoD: fixing one surface and not the other just moves the bug. The live
    stream and the replayed log are asserted against the SAME input here, so a
    change to either one alone fails this test."""
    from types import SimpleNamespace
    from no_human.api.app import _format_events, task_events_stream
    from no_human.core.task import Task

    t = Task.new("verdict visibility", repo_path="/tmp/x")
    await store.create_task(t)

    class FakeSched:
        inflight = set()             # not running → the stream closes on idle
        _event_log = {t.id: None}
        _event_notify = {}
        def task_events(self, tid):
            return _REVIEW_ROUND

    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(scheduler=FakeSched(), store=store)),
        headers={},
    )
    resp = await task_events_stream(t.id, req)
    body = ""
    async for chunk in resp.body_iterator:
        body += chunk if isinstance(chunk, str) else chunk.decode()
        if '"done"' in body:
            break

    streamed = [json.loads(line[len("data: "):])
                for line in body.splitlines() if line.startswith("data: ")]
    kinds = {(e.get("source"), e.get("kind")) for e in streamed}
    assert ("reviewer", "review") in kinds, (
        f"the live stream dropped the verdict: {sorted(kinds)}")

    (live,) = [e for e in streamed
               if (e.get("source"), e.get("kind")) == ("reviewer", "review")]
    (replayed,) = [e for e in _format_events(_REVIEW_ROUND)
                   if (e["source"], e["kind"]) == ("reviewer", "review")]
    assert live["text"] == replayed["text"], (
        "the live stream and the replayed log disagree about the verdict — one "
        "of the two filters was fixed and the other was not")
    for marker in _VERDICT_MARKERS:
        assert marker in live["text"]

    for noisy in ("tool_use", "thinking", "usage"):
        assert ("reviewer", noisy) not in kinds, (
            f"reviewer SDK {noisy} events leaked into the live stream")


async def test_the_live_stream_also_carries_the_role_backends_disclosure(store):
    """DoD mirrors test_the_live_stream_serves_the_same_verdict_as_the_replay:
    fixing only the replay whitelist (_format_events) and not the live SSE
    whitelist (task_events_stream) leaves the disclosure invisible on a task
    a human is actually watching run."""
    from types import SimpleNamespace
    from no_human.api.app import _format_events, task_events_stream
    from no_human.core.task import Task

    t = Task.new("reviewer backend visibility", repo_path="/tmp/x")
    await store.create_task(t)

    models_event = [{
        "ts": 1, "source": "orchestrator", "kind": "models",
        "text": "coder=claude-sonnet-5 · reviewer=claude-opus-4-8",
        "models": {"coder": "claude-sonnet-5", "reviewer": "claude-opus-4-8"},
        "role_backends": {"reviewer": {"backend": "codex", "model": "gpt-5-codex"}},
    }]

    class FakeSched:
        inflight = set()             # not running -> the stream closes on idle
        _event_log = {t.id: None}
        _event_notify = {}
        def task_events(self, tid):
            return models_event

    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(scheduler=FakeSched(), store=store)),
        headers={},
    )
    resp = await task_events_stream(t.id, req)
    body = ""
    async for chunk in resp.body_iterator:
        body += chunk if isinstance(chunk, str) else chunk.decode()
        if '"done"' in body:
            break

    streamed = [json.loads(line[len("data: "):])
                for line in body.splitlines() if line.startswith("data: ")]
    (live,) = [e for e in streamed if e.get("kind") == "models"]
    assert live["role_backends"] == {
        "reviewer": {"backend": "codex", "model": "gpt-5-codex"}}

    (replayed,) = [e for e in _format_events(models_event) if e["kind"] == "models"]
    assert live["role_backends"] == replayed["role_backends"], (
        "the live stream and the replayed log disagree about the reviewer "
        "backend disclosure — one of the two whitelists was fixed and the "
        "other was not")


def test_both_surfaces_carry_the_meta_the_board_reads_the_verdict_from():
    """The board does NOT parse the verdict prose: summaries.js branches on
    `passed` and counts findings from `blocking_count`. Serving the event
    without them renders a PASSING round as "FAIL (? blocking)" — a wrong
    verdict is worse than the missing one this fix replaced."""
    from no_human.api.app import _VERDICT_META, _format_events

    passing = {"ts": 1, "source": "reviewer", "kind": "review",
               "text": "PASS — 0 blocking, 4 advisory finding(s)",
               "passed": True, "failed_count": 4, "blocking_count": 0,
               "advisory_count": 4}
    (out,) = _format_events([passing])

    assert out["passed"] is True, (
        "the board reads PASS/FAIL from `passed`; stripped, a passing review "
        f"renders as FAIL: {out}")
    assert out["blocking_count"] == 0 and out["advisory_count"] == 4
    assert set(_VERDICT_META) <= set(out), f"verdict meta stripped: {out}"

    # ...and it must not be sprayed onto events that never carried it.
    (plain,) = _format_events(
        [{"ts": 2, "source": "orchestrator", "kind": "state", "text": "x"}])
    assert not (set(_VERDICT_META) & set(plain)), plain


async def test_the_live_stream_carries_the_verdict_meta_too(store):
    """The other half of the same list — the stream had its own copy of the
    carry-through and would otherwise disagree with the replayed log."""
    from types import SimpleNamespace
    from no_human.api.app import _VERDICT_META, task_events_stream
    from no_human.core.task import Task

    t = Task.new("verdict meta", repo_path="/tmp/x")
    await store.create_task(t)
    passing = {"ts": 1, "source": "reviewer", "kind": "review",
               "text": "PASS — 0 blocking, 4 advisory finding(s)",
               "passed": True, "failed_count": 4, "blocking_count": 0,
               "advisory_count": 4}

    class FakeSched:
        inflight = set()
        _event_log = {t.id: None}
        _event_notify = {}
        def task_events(self, tid):
            return [passing]

    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(scheduler=FakeSched(), store=store)),
        headers={},
    )
    resp = await task_events_stream(t.id, req)
    body = ""
    async for chunk in resp.body_iterator:
        body += chunk if isinstance(chunk, str) else chunk.decode()
        if '"done"' in body:
            break

    (frame,) = [json.loads(l[len("data: "):]) for l in body.splitlines()
                if l.startswith("data: ") and '"review"' in l]
    assert frame["passed"] is True, f"the live stream lost the verdict: {frame}"
    assert set(_VERDICT_META) <= set(frame)


# --------------------------------------------------------------------------- #
# Memory lifecycle C: retire-candidates / retire endpoints                    #
# --------------------------------------------------------------------------- #

async def test_retire_candidates_returns_a_stale_confirmed_row(client, store):
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="stale rule", content="x", confirmed=True)
    from datetime import datetime, timedelta, timezone
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    await store.db.execute(
        "UPDATE memories SET last_used_at = ? WHERE id = ?", (stale_ts, mem_id))
    await store.db.commit()

    r = await client.get("/api/learnings/retire-candidates?days=90")
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}
    assert mem_id in ids


async def test_retire_archives_and_drops_from_active(client, store):
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="unused rule", content="x", confirmed=True)

    active_before = await client.get("/api/learnings?active=true")
    assert mem_id in {row["id"] for row in active_before.json()}

    r = await client.post(f"/api/learnings/{mem_id}/retire")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    active_after = await client.get("/api/learnings?active=true")
    assert mem_id not in {row["id"] for row in active_after.json()}


async def test_retire_second_call_is_idempotent(client, store):
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="unused rule", content="x", confirmed=True)
    r1 = await client.post(f"/api/learnings/{mem_id}/retire")
    assert r1.status_code == 200
    assert r1.json().get("already_archived") is not True

    r2 = await client.post(f"/api/learnings/{mem_id}/retire")
    assert r2.status_code == 200
    assert r2.json()["already_archived"] is True


async def test_retire_unconfirmed_row_is_409(client, store):
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="pending proposal", content="x",
        confirmed=False)
    r = await client.post(f"/api/learnings/{mem_id}/retire")
    assert r.status_code == 409


async def test_retire_unknown_id_is_404(client):
    r = await client.post("/api/learnings/does-not-exist/retire")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Memory lifecycle C part B: include_archived on /api/rules|/api/skills, and  #
# the /restore triage endpoint (Rules/Skills UI)                              #
# --------------------------------------------------------------------------- #

async def test_rules_exclude_archived_by_default_and_include_on_request(client, store):
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="soon archived", content="x", confirmed=True)
    await store.archive_memory(mem_id, reason="test")

    default = await client.get("/api/rules")
    assert mem_id not in {row["id"] for row in default.json()}

    included = await client.get("/api/rules?include_archived=1")
    rows = {row["id"]: row for row in included.json()}
    assert mem_id in rows
    assert rows[mem_id]["archived"] == 1


async def test_skills_include_archived_flag(client, store):
    from no_human.learning import TYPE_SKILL, TYPE_FACT

    skill_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="soon archived skill", content="x", confirmed=True)
    fact_id = await store.add_memory(
        mem_type=TYPE_FACT, title="soon archived fact", content="x", confirmed=True)
    await store.archive_memory(skill_id, reason="test")
    await store.archive_memory(fact_id, reason="test")

    default = await client.get("/api/skills")
    default_ids = {row["id"] for row in default.json()}
    assert skill_id not in default_ids and fact_id not in default_ids

    included = await client.get("/api/skills?include_archived=1")
    rows = {row["id"]: row for row in included.json()}
    assert rows[skill_id]["archived"] == 1
    assert rows[fact_id]["archived"] == 1


async def test_restore_unarchives_and_clears_superseded(client, store):
    from no_human.learning import TYPE_RULE

    old_id = await store.add_memory(
        mem_type=TYPE_RULE, title="old rule", content="x", confirmed=True)
    new_id = await store.add_memory(
        mem_type=TYPE_RULE, title="new rule", content="y", confirmed=True)
    assert await store.supersede_memory(old_id, new_id, reason="dup")

    r = await client.post(f"/api/learnings/{old_id}/restore")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body.get("already_active") is not True

    m = await store.find_memory(old_id)
    assert m["archived"] == 0
    assert m["superseded_by"] is None

    included = await client.get("/api/rules?include_archived=1")
    row = next(row for row in included.json() if row["id"] == old_id)
    assert row["archived"] == 0


async def test_restore_is_idempotent_on_a_live_row(client, store):
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="live rule", content="x", confirmed=True)
    r = await client.post(f"/api/learnings/{mem_id}/restore")
    assert r.status_code == 200
    assert r.json()["already_active"] is True


async def test_restore_unknown_id_is_404(client):
    r = await client.post("/api/learnings/does-not-exist/restore")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# D3 (2026-08-31 operator directive): pause / delete, and reject-aliases-pause #
# --------------------------------------------------------------------------- #

async def test_pause_holds_the_row_without_archiving_it(client, store):
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="active rule", content="x", confirmed=True)
    r = await client.post(f"/api/learnings/{mem_id}/pause")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    m = await store.find_memory(mem_id)
    assert m["paused"] == 1
    assert m["archived"] == 0  # pause is not archive
    assert m["confirmed"] == 1  # the row is not unconfirmed either

    # A paused row is invisible to the default (injectable) view...
    active = await store.list_memories(confirmed=True)
    assert mem_id not in {row["id"] for row in active}
    # ...but still exists, on request.
    with_paused = await store.list_memories(confirmed=True, include_paused=True)
    assert mem_id in {row["id"] for row in with_paused}

    events = await store.list_learning_events(memory_id=mem_id)
    assert any(e["event"] == "pause" for e in events)


async def test_delete_archives_rather_than_deletes(client, store):
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="active rule", content="x", confirmed=True)
    r = await client.post(f"/api/learnings/{mem_id}/delete")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    m = await store.find_memory(mem_id)
    assert m is not None, "delete must archive, never DELETE FROM"
    assert m["archived"] == 1

    events = await store.list_learning_events(memory_id=mem_id)
    assert any(e["event"] == "delete" for e in events)


async def test_pause_unknown_id_is_404(client):
    r = await client.post("/api/learnings/does-not-exist/pause")
    assert r.status_code == 404


async def test_delete_unknown_id_is_404(client):
    r = await client.post("/api/learnings/does-not-exist/delete")
    assert r.status_code == 404


async def test_reject_on_a_confirmed_row_pauses_it_instead_of_deleting(client, store):
    """D3: 'reject aliases pause'. The old per-origin archive/delete
    dispatch (`ARCHIVE_ON_REJECT`) stays exactly as it was for a still-
    PENDING proposal (test_learning.py pins that unchanged); a CONFIRMED
    row — the common case now that most proposals auto-activate — is
    PAUSED, never deleted or archived by this route."""
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="active rule", content="x", confirmed=True)
    r = await client.post(f"/api/learnings/{mem_id}/reject")
    assert r.status_code == 200

    m = await store.find_memory(mem_id)
    assert m is not None, "reject on a confirmed row must never delete it"
    assert m["paused"] == 1
    assert m["archived"] == 0
    assert m["confirmed"] == 1


async def test_restore_also_unpauses(client, store):
    """The Second-brain UI's one Restore button undoes BOTH archive and
    pause — a caller never needs to know which inert state a row is in."""
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="active rule", content="x", confirmed=True)
    await client.post(f"/api/learnings/{mem_id}/pause")

    r = await client.post(f"/api/learnings/{mem_id}/restore")
    assert r.status_code == 200
    assert r.json().get("already_active") is not True

    m = await store.find_memory(mem_id)
    assert m["paused"] == 0
    active = await store.list_memories(confirmed=True)
    assert mem_id in {row["id"] for row in active}


async def test_list_learnings_active_excludes_paused_by_default(client, store):
    """D3.2 review deferral: `GET /api/learnings?active=true` must keep
    excluding a paused row by default — the injection-visibility contract
    (`Store.list_memories`'s default) is not what's changing here."""
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="active rule", content="x", confirmed=True)
    await client.post(f"/api/learnings/{mem_id}/pause")

    r = await client.get("/api/learnings?active=true")
    assert r.status_code == 200
    assert mem_id not in {row["id"] for row in r.json()}


async def test_list_learnings_active_include_paused_surfaces_the_row(client, store):
    """D3.2 review deferral: a paused row must remain VISIBLE in the
    Second-brain panel list (with a `Paused` chip, so Restore is
    discoverable) even though it stays invisible to injection —
    `?active=true&include_paused=true` is the one caller allowed to ask for
    that."""
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="active rule", content="x", confirmed=True)
    await client.post(f"/api/learnings/{mem_id}/pause")

    r = await client.get("/api/learnings?active=true&include_paused=true")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()}
    assert mem_id in rows
    assert rows[mem_id]["paused"] == 1


async def test_list_learnings_include_paused_is_a_no_op_on_the_pending_branch(client, store):
    """A PENDING (unconfirmed) proposal is not what `include_paused` is
    for — `pending()` takes no such parameter, so the query string must be
    silently ignored on that branch rather than 422ing a caller that always
    sends both params regardless of which view it's requesting."""
    from no_human.learning import LearningQueue, TYPE_RULE

    queue = LearningQueue(store)
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="a proposal", content="x", confirmed=False,
        source="proposed")
    assert mem_id is not None

    r = await client.get("/api/learnings?active=false&include_paused=true")
    assert r.status_code == 200
    assert mem_id in {row["id"] for row in r.json()}


async def test_list_learnings_active_excludes_archived_by_default(client, store):
    """D3.2 review-round fix: `GET /api/learnings?active=true` must keep
    excluding a Delete-archived row by default — the same exclusion
    `Store.list_memories`'s default already gives Rules/Skills."""
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="active rule", content="x", confirmed=True)
    await client.post(f"/api/learnings/{mem_id}/delete")

    r = await client.get("/api/learnings?active=true")
    assert r.status_code == 200
    assert mem_id not in {row["id"] for row in r.json()}


async def test_list_learnings_active_include_archived_surfaces_the_deleted_row(client, store):
    """D3.2 review-round fix: Delete only archives (never a real DELETE
    FROM), but a UI that can never ask for the archived row back makes that
    recoverability theoretical. `?active=true&include_archived=true` is the
    Second-brain panel's own archived-count footer asking for it."""
    from no_human.learning import TYPE_RULE

    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="active rule", content="x", confirmed=True)
    await client.post(f"/api/learnings/{mem_id}/delete")

    r = await client.get("/api/learnings?active=true&include_archived=true")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()}
    assert mem_id in rows
    assert rows[mem_id]["archived"] == 1


@pytest.mark.asyncio
async def test_pause_on_inflight_task_requests_cancel_and_leaves_status_to_the_worker(
        client, store):
    """A task the scheduler is RUNNING is paused the way `nh task pause` does
    it: raise the cancel flag and let the worker checkpoint + park itself
    (`_honor_cancel` writes the USER_PAUSED blocker WITH `resume_commit`).
    Flipping BLOCKED from the API under a live worker recorded no checkpoint,
    so the eventual resume branched from base. Negative control for this
    branch is `test_pause_on_implementing_still_transitions_to_blocked` (no
    scheduler → nothing is running it → parked directly)."""
    from types import SimpleNamespace
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    app.state.scheduler = SimpleNamespace(inflight={t.id},
                                          get_live_status=lambda tid: None)
    try:
        r = await client.post(f"/api/tasks/{t.id}/pause")
    finally:
        app.state.scheduler = None
    assert r.status_code == 200, r.text
    assert "checkpoint" in r.json()["message"]
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.IMPLEMENTING, "status is the worker's to change"
    assert fresh.blocker is None, "the worker writes the checkpointed blocker"
    assert await store.get_cancel_request(t.id) == "Paused from board"


@pytest.mark.asyncio
async def test_pause_with_scheduler_present_but_task_not_inflight_parks_directly_and_leaves_no_flag(
        client, store):
    """Forward guard (green on the parent too — it does not demonstrate the
    change): a scheduler IS present but is not running this task → direct
    park, and the flag raised first is withdrawn again, so a later
    retry/resume does not re-park on turn zero."""
    from types import SimpleNamespace
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    app.state.scheduler = SimpleNamespace(inflight=set(),
                                          get_live_status=lambda tid: None)
    try:
        r = await client.post(f"/api/tasks/{t.id}/pause")
    finally:
        app.state.scheduler = None
    assert r.status_code == 200, r.text
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.BLOCKED
    assert fresh.blocker.get("category") == "USER_PAUSED"
    assert await store.get_cancel_request(t.id) is None


@pytest.mark.asyncio
async def test_board_retry_and_resume_withdraw_a_pending_pause(client, store):
    """Board Pause (flag set) → worker killed before honouring → Retry/Resume
    must clear `cancel_requested`, or the fresh run honours the stale pause on
    turn zero and parks straight back (the CLI twins already do this)."""
    t = await _seed_task(store, status=TaskStatus.FAILED)
    await store.request_cancel(t.id, "Paused from board")
    r = await client.post(f"/api/tasks/{t.id}/retry")
    assert r.status_code == 200, r.text
    assert await store.get_cancel_request(t.id) is None

    t2 = await _seed_task(store, status=TaskStatus.BLOCKED)
    t2.blocker = {"category": "USER_PAUSED", "question": "Paused from board"}
    await store.update_task_columns(t2)
    await store.request_cancel(t2.id, "Paused from board")
    r = await client.post(f"/api/tasks/{t2.id}/resume")
    assert r.status_code == 200, r.text
    assert await store.get_cancel_request(t2.id) is None


@pytest.mark.asyncio
async def test_board_reply_send_back_and_cancel_withdraw_a_pending_pause(client, store):
    """The other three board re-entries/exits clear `cancel_requested` too —
    reply and send-back re-enter the loop (a stale flag would park the fresh
    attempt on turn zero); cancel kills the worker, so nothing could ever
    honour it (the CLI twin clears it as well)."""
    t = await _seed_task(store, status=TaskStatus.BLOCKED)
    t.blocker = {"question": "Which DB?", "category": "need_clarification"}
    await store.update_task(t)
    await store.request_cancel(t.id, "Paused from board")
    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "SQLite only"})
    assert r.status_code == 200, r.text
    assert await store.get_cancel_request(t.id) is None

    t2 = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    await store.request_cancel(t2.id, "Paused from board")
    r = await client.post(f"/api/tasks/{t2.id}/send-back",
                          json={"message": "Handle the empty-input edge case."})
    assert r.status_code == 200, r.text
    assert await store.get_cancel_request(t2.id) is None

    t3 = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    await store.request_cancel(t3.id, "Paused from board")
    r = await client.post(f"/api/tasks/{t3.id}/cancel")
    assert r.status_code == 200, r.text
    assert await store.get_cancel_request(t3.id) is None


@pytest.mark.asyncio
async def test_send_back_withdraws_the_durable_human_stop_too(client, store):
    """Board Pause stamps `blocker.human_stopped`; Send back is a human
    re-entry and must drop that stamp along with the cancel flag, or the
    fresh run reads 'stopped by you' on the card and the wake sweep skips it."""
    t = await _seed_task(store, status=TaskStatus.AWAITING_APPROVAL)
    t.blocker = {"category": "USER_PAUSED", "question": "Paused from board",
                 "human_stopped": True}
    await store.update_task_columns(t)
    await store.request_cancel(t.id, "Paused from board")
    r = await client.post(f"/api/tasks/{t.id}/send-back", json={"message": "redo"})
    assert r.status_code == 200, r.text
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.IMPLEMENTING
    assert fresh.blocker is None
    assert await store.get_cancel_request(t.id) is None


@pytest.mark.asyncio
async def test_board_pause_direct_park_carries_the_checkpoint(client, store):
    """R1's third writer: the board's direct park (no worker owns the task)
    used to write a USER_PAUSED blocker with no checkpoint, so the next
    resume branched from base; it now carries the one the task had."""
    from no_human.blockers import resume_checkpoint
    t = await _seed_task(store, status=TaskStatus.IMPLEMENTING)
    t.blocker = {"category": "CI_GATE", "resume_commit": "b" * 40, "resume_branch": "no-human/w"}
    await store.update_task_columns(t)
    r = await client.post(f"/api/tasks/{t.id}/pause")
    assert r.status_code == 200, r.text
    fresh = await store.find_task(t.id)
    assert fresh.status == TaskStatus.BLOCKED and fresh.blocker["category"] == "USER_PAUSED"
    assert resume_checkpoint(fresh.blocker) == {"sha": "b" * 40, "branch": "no-human/w"}


@pytest.mark.asyncio
async def test_create_task_records_a_supported_backend_and_refuses_an_unknown_one(client, store):
    """Public issue #5: the board's backend field is a real per-task switch,
    validated at intake against the factory's own tuple."""
    r = await client.post("/api/tasks", json={"title": "On codex", "backend": "codex"})
    assert r.status_code == 201, r.text
    task = await store.get_task(r.json()["id"])
    assert task.config["backend"] == "codex"

    r = await client.post("/api/tasks", json={"title": "Typo", "backend": "kodex"})
    assert r.status_code == 422, r.text
    assert "codex" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# human_event task_events on the board's own endpoints — a prior review found #
# these share NO code with the CLI twins, despite comments claiming parity,   #
# and were left silently unwired.                                             #
# --------------------------------------------------------------------------- #

_API_HUMAN_VERB_BLOCKER = {"category": "AMBIGUITY", "question": "which store?"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verb,seed_status,blocker,path,payload,expected_status,expected_kind", [
        ("resume", TaskStatus.ESCALATED, _API_HUMAN_VERB_BLOCKER,
         "resume", None, TaskStatus.IMPLEMENTING, "human_resume"),
        ("pause", TaskStatus.IMPLEMENTING, None,
         "pause", None, TaskStatus.BLOCKED, "human_pause"),
        ("retry", TaskStatus.FAILED, _API_HUMAN_VERB_BLOCKER,
         "retry", None, TaskStatus.PENDING, "human_retry"),
        ("cancel", TaskStatus.IMPLEMENTING, _API_HUMAN_VERB_BLOCKER,
         "cancel", {"reason": "stop it"}, TaskStatus.FAILED, "human_cancel"),
        ("send-back", TaskStatus.AWAITING_APPROVAL, None,
         "send-back", {"message": "redo it"}, TaskStatus.IMPLEMENTING, "human_reject"),
        ("reply", TaskStatus.ESCALATED, _API_HUMAN_VERB_BLOCKER,
         "reply", {"answer": "SQLite only"}, TaskStatus.IMPLEMENTING, "human_reply"),
    ])
async def test_every_board_endpoint_emits_its_shared_human_event(
        client, store, verb, seed_status, blocker, path, payload,
        expected_status, expected_kind):
    """Table-driven, one row per board endpoint behind a human-facing button:
    each must go through the SAME `human_event()` emitter the CLI twins use
    (`blockers/taxonomy.py`), recording `source=human`, `kind=human_<verb>`,
    `prior_status`, and the full prior blocker JSON in the SAME write as the
    status change — never a differently-shaped or independently-invented
    event, and never a silently-unwired endpoint."""
    t = await _seed_task(store, status=seed_status)
    if blocker is not None:
        t.blocker = blocker
        await store.update_task(t)

    if payload is not None:
        r = await client.post(f"/api/tasks/{t.id}/{path}", json=payload)
    else:
        r = await client.post(f"/api/tasks/{t.id}/{path}")

    assert r.status_code == 200, f"{verb}: {r.text}"
    fresh = await store.find_task(t.id)
    assert fresh.status == expected_status, f"{verb}: {fresh.status}"

    events = await store.list_events(t.id)
    human = [e for e in events if e.get("source") == "human"]
    assert len(human) == 1, f"{verb}: expected exactly one human event, got: {events}"
    ev = human[0]
    assert ev["kind"] == expected_kind, f"{verb}: {ev}"
    assert ev["prior_status"] == seed_status.value, f"{verb}: {ev}"
    if blocker is not None:
        assert ev.get("prior_blocker") == blocker, f"{verb}: {ev}"


# --------------------------------------------------------------------------- #
# POST /api/tasks/{id}/split — the 1-click split's create path (feature #1)    #
# --------------------------------------------------------------------------- #

async def test_split_creates_children_with_parent_id_and_cancels_parent(client, store):
    parent = await _seed_task(store, status=TaskStatus.PENDING, title="Big thing")
    r = await client.post(f"/api/tasks/{parent.id}/split", json={"drafts": [
        {"title": "Part A", "description": "do A", "acceptance_criteria": ["A done"]},
        {"title": "Part B", "description": "do B"},
    ]})
    assert r.status_code == 201, r.text
    children = r.json()
    assert {c["title"] for c in children} == {"Part A", "Part B"}
    # Each child is independent, carries parent_id for provenance, and inherits
    # the parent's repo.
    for c in children:
        full = await store.get_task(c["id"])
        assert full.parent_id == parent.id
        assert full.repo_path == parent.repo_path
    # The original is cancelled — its scope now lives in the children.
    p = await store.get_task(parent.id)
    assert p.status == TaskStatus.FAILED
    assert "split into 2 sub-tasks" in (p.context or {}).get("cancel_reason", "")


async def test_split_refuses_a_task_that_is_not_pending(client, store):
    # A running/parked/terminal task has work in flight or done — never silently
    # replace it.
    running = await _seed_task(store, status=TaskStatus.IMPLEMENTING, title="running")
    r = await client.post(f"/api/tasks/{running.id}/split", json={
        "drafts": [{"title": "A"}, {"title": "B"}]})
    assert r.status_code == 409


async def test_split_requires_between_2_and_8_drafts(client, store):
    parent = await _seed_task(store, status=TaskStatus.PENDING)
    r1 = await client.post(f"/api/tasks/{parent.id}/split",
                           json={"drafts": [{"title": "only one"}]})
    assert r1.status_code == 422
    # A parent left un-split by a rejected request is still splittable.
    p = await store.get_task(parent.id)
    assert p.status == TaskStatus.PENDING


async def test_a_second_split_creates_no_duplicate_children(client, store):
    # The reservation (parent PENDING->FAILED via a guarded CAS) runs BEFORE any
    # child is created, so a concurrent/repeat split loses the race: the parent
    # is no longer pending, and NO second child set is created.
    parent = await _seed_task(store, status=TaskStatus.PENDING, title="Big")
    r1 = await client.post(f"/api/tasks/{parent.id}/split", json={
        "drafts": [{"title": "A"}, {"title": "B"}]})
    assert r1.status_code == 201
    first = {c["id"] for c in r1.json()}
    r2 = await client.post(f"/api/tasks/{parent.id}/split", json={
        "drafts": [{"title": "C"}, {"title": "D"}]})
    assert r2.status_code == 409
    # Exactly the first two children exist — the second split added nothing.
    subs = await store.list_subtasks(parent.id)
    assert {s.id for s in subs} == first


async def test_create_stashes_a_feasibility_hint_for_a_large_task(client, store):
    # Two pre-plan signals at create (>=5 criteria + a >=2000-char spec) → the
    # complexity tier is `complex` → the create stashes a feasibility hint that
    # offers a 1-click split, for the drawer to surface.
    r = await client.post("/api/tasks", json={
        "title": "A big multi-part task",
        "description": "x" * 2100,
        "acceptance_criteria": ["a", "b", "c", "d", "e", "f"],
    })
    assert r.status_code == 201
    full = await store.get_task(r.json()["id"])
    hint = (full.context or {}).get("feasibility_hint")
    assert hint is not None
    assert hint["tier"] == "complex"
    assert hint["offer"] == "split"
    assert "message" in hint
    # Hint-only families (feasibility hint calibration): the create handler
    # must persist the card's `signals`/`hint_reasons`, not just band/offer —
    # otherwise a fired hint-only family (e.g. multi_family) is computed for
    # nothing and never reaches anything a human looks at.
    assert hint["signals"] == ["many-criteria", "long-spec"]
    assert hint["hint_reasons"] == []


async def test_create_of_a_simple_task_stashes_no_hint(client, store):
    # Nothing large → no hint, so the drawer never nags a task that looks fine.
    r = await client.post("/api/tasks", json={
        "title": "tiny", "description": "small",
        "acceptance_criteria": ["do the thing"],
    })
    assert r.status_code == 201
    full = await store.get_task(r.json()["id"])
    assert (full.context or {}).get("feasibility_hint") is None


async def test_create_response_carries_the_feasibility_hint_immediately(client, store):
    # P3: dispatch takes ~9s and the SlideOver card only renders while
    # status == "pending", so a client that waits for a later GET rarely sees
    # the hint before the status moves on. The create-time toast must read the
    # hint straight off THIS response, not a follow-up GET — so the response
    # body itself (not just the persisted task.context) must carry it.
    r = await client.post("/api/tasks", json={
        "title": "A big multi-part task",
        "description": "x" * 2100,
        "acceptance_criteria": ["a", "b", "c", "d", "e", "f"],
    })
    assert r.status_code == 201
    hint = r.json().get("feasibility_hint")
    assert hint is not None
    assert hint["tier"] == "complex"
    assert hint["offer"] == "split"
    assert "message" in hint
    # TaskSummaryOut.feasibility_hint reads straight off the persisted dict
    # (models.py), so the create RESPONSE — not just the stored task — must
    # carry the hint-only `signals`/`hint_reasons` too.
    assert hint["signals"] == ["many-criteria", "long-spec"]
    assert hint["hint_reasons"] == []


async def test_create_response_carries_no_hint_for_a_simple_task(client, store):
    # Fail-open mirror: nothing large → the create response's feasibility_hint
    # is exactly None, not omitted or a falsy placeholder — the frontend toast
    # renders nothing rather than something empty.
    r = await client.post("/api/tasks", json={
        "title": "tiny", "description": "small",
        "acceptance_criteria": ["do the thing"],
    })
    assert r.status_code == 201
    assert r.json().get("feasibility_hint") is None


async def test_split_drafts_refuses_a_non_pending_task_without_a_paid_call(client, store):
    # The GET generates drafts via a utility-model call; a running/terminal task
    # can never be split, so it must 409 BEFORE spending that call.
    running = await _seed_task(store, status=TaskStatus.IMPLEMENTING, title="running")
    r = await client.get(f"/api/tasks/{running.id}/split-drafts")
    assert r.status_code == 409
