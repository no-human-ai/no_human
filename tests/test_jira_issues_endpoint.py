"""GET /api/integrations/jira/issues — Task 1.6's Import-from-Jira read side.

Mirrors tests/test_jira_intake.py's mocking style (httpx.get monkeypatched on
the intake.jira module) and tests/test_integrations_write.py's API-client
fixture (isolated ENV_PATH/CONFIG_PATH, ASGITransport). No task is ever
created here — POST /api/tasks stays the one create path.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human.api.app import app
from no_human.core.task import Task, TaskStatus


def _cfg(**over):
    j = {"site": "https://acme.atlassian.net", "project_key": "PROJ",
         "email": "me@x.com", "jql": ""}
    j.update(over)
    return {"integrations": {"jira": j}}


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    return tmp_path


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    """A live config the endpoint reads via request.app.state.config.data —
    write config.yaml directly (no HTTP round trip needed for these tests)."""
    import yaml
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(_cfg()))
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


@pytest.mark.asyncio
async def test_unconfigured_returns_503_with_clear_detail(store, tmp_path):
    # No config.yaml written at all this time — jira section stays empty.
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        r = await c.get("/api/integrations/jira/issues")
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_unconfigured_when_token_missing(client, monkeypatch):
    # site/project_key/email present but no JIRA_API_TOKEN → still unconfigured.
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    r = await client.get("/api/integrations/jira/issues")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_configured_returns_shaped_issues_and_clamps_description(client, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    long_desc = "x" * 3000
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(url=url, params=params, auth=auth)
        return _Resp({"issues": [{
            "key": "PROJ-9",
            "fields": {
                "summary": "Fix the thing",
                "description": long_desc,
                "status": {"name": "In Progress"},
                "assignee": {"displayName": "Ada Lovelace"},
                "updated": "2026-07-18T10:00:00.000+0000",
            },
        }]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "thing"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    item = body[0]
    assert item["key"] == "PROJ-9"
    assert item["summary"] == "Fix the thing"
    assert item["status"] == "In Progress"
    assert item["assignee"] == "Ada Lovelace"
    assert item["updated"] == "2026-07-18T10:00:00.000+0000"
    assert item["url"].endswith("/browse/PROJ-9")
    assert len(item["description"]) == 2000

    # Reused the successor endpoint + Basic auth, exactly like the poller's search().
    assert "/rest/api/3/search/jql" in captured["url"]
    assert captured["auth"] == ("me@x.com", "SEKRET")
    assert 'text ~ "thing"' in captured["params"]["jql"]
    assert "ORDER BY updated DESC" in captured["params"]["jql"]
    # Scoped to the CONFIGURED project — the picker shows the wired project, not
    # a cross-project text match (typing the project key used to find nothing).
    assert 'project = "PROJ"' in captured["params"]["jql"]

    # The token never appears anywhere in the response body.
    assert "SEKRET" not in r.text


@pytest.mark.asyncio
async def test_nonempty_query_endpoint_jql_stays_open_scoped(client, monkeypatch):
    """SCRUM-5 bug 1: typing a query into the picker must FILTER the open
    tickets, never expand into Done/closed ones."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(params=params)
        return _Resp({"issues": []})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "thing"})
    assert r.status_code == 200, r.text
    jql = captured["params"]["jql"]
    assert "statusCategory != Done" in jql, jql
    assert 'text ~ "thing"' in jql, jql


@pytest.mark.asyncio
async def test_limit_is_clamped_between_1_and_50(client, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(params=params)
        return _Resp({"issues": []})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)

    await client.get("/api/integrations/jira/issues", params={"limit": 500})
    assert captured["params"]["maxResults"] == 50

    await client.get("/api/integrations/jira/issues", params={"limit": 0})
    assert captured["params"]["maxResults"] == 1

    await client.get("/api/integrations/jira/issues")  # default
    assert captured["params"]["maxResults"] == 20


@pytest.mark.asyncio
async def test_empty_query_browses_the_projects_open_tickets(client, monkeypatch):
    """An empty box must LIST the configured project's open tickets to choose
    from — not run a text match on nothing. Typing the project key used to find
    nothing; opening the picker now browses the whole project."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(params=params)
        return _Resp({"issues": []})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)

    r = await client.get("/api/integrations/jira/issues", params={"q": ""})
    assert r.status_code == 200, r.text
    jql = captured["params"]["jql"]
    assert 'project = "PROJ"' in jql, jql
    assert "statusCategory != Done" in jql, jql
    assert "text ~" not in jql, "empty query must browse, not text-match"
    assert "ORDER BY updated DESC" in jql


@pytest.mark.asyncio
async def test_token_only_in_env_file_is_loaded_on_demand(client, tmp_path, monkeypatch):
    """Under `nh start` (the board) JIRA_API_TOKEN is NOT in the process env —
    only `nh serve`'s poller loaded it. The picker endpoint must load it on
    demand from ~/.no_human/.env (B1 pattern), else a valid, configured Jira
    integration wrongly 503s "not configured" and the picker looks broken."""
    (tmp_path / ".env").write_text("JIRA_API_TOKEN=dotenv-secret\n")
    assert "JIRA_API_TOKEN" not in os.environ  # process env is clean (nh start)
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(auth=auth)
        return _Resp({"issues": []})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    try:
        r = await client.get("/api/integrations/jira/issues", params={"q": ""})
        assert r.status_code == 200, r.text  # NOT 503 "not configured"
        # proves the token was loaded from .env into the request's adapter
        assert captured["auth"] == ("me@x.com", "dotenv-secret")
    finally:
        # load_env_var sets os.environ directly (untracked by monkeypatch); pop
        # it so the token never leaks into a later test.
        os.environ.pop("JIRA_API_TOKEN", None)


@pytest.mark.asyncio
async def test_upstream_error_surfaces_as_502_never_leaking_token(client, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"errorMessages": ["boom"]}, status_code=500)

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "x"})
    assert r.status_code == 502
    assert "SEKRET" not in r.text
    assert "Settings > Integrations" not in r.json()["detail"]


@pytest.mark.asyncio
async def test_upstream_connection_error_surfaces_as_502(client, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        import httpx
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "x"})
    assert r.status_code == 502
    assert "SEKRET" not in r.text
    assert "Settings > Integrations" not in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_credential_error_returns_distinct_message(client, monkeypatch, status_code):
    """SCRUM-5 bug 2: a token rejection (401/403) must surface a distinct,
    actionable message — not the generic 'check the site/project
    configuration' text other failures get — while staying at 502 (resolved
    intake Q&A: status code unchanged, only the body message differs)."""
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"errorMessages": ["Unauthorized"]}, status_code=status_code)

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "x"})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "Settings > Integrations" in detail
    assert "token" in detail.lower()
    assert "rotate" in detail.lower() or "verify" in detail.lower()
    # never the generic other-failures message
    assert "check the site/project configuration" not in detail
    # never leaks the token, the site URL, or the account email
    assert "SEKRET" not in r.text
    assert "acme.atlassian.net" not in r.text
    assert "me@x.com" not in r.text


@pytest.mark.asyncio
async def test_credential_error_never_logs_site_or_token(client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"errorMessages": ["Unauthorized"]}, status_code=401)

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    with caplog.at_level(logging.DEBUG):
        r = await client.get("/api/integrations/jira/issues", params={"q": "x"})
    assert r.status_code == 502
    assert "SEKRET" not in caplog.text
    assert "acme.atlassian.net" not in caplog.text


@pytest.mark.asyncio
async def test_issue_detail_returns_full_untruncated_description(client, monkeypatch):
    """SCRUM-9 repro: the browse list truncates description to 2000 chars for
    a small list payload, but the picker's "pick" action must fetch the FULL
    text so the created task doesn't silently lose everything past 2000
    chars. This is the detail GET (a single issue by key), distinct from the
    list endpoint above."""
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    long_desc = "x" * 2000 + "TAIL-MARKER-AFTER-2000-CHARS"
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(url=url, params=params, auth=auth)
        return _Resp({
            "key": "PROJ-9",
            "fields": {
                "summary": "Fix the thing",
                "description": long_desc,
                "status": {"name": "In Progress"},
                "assignee": {"displayName": "Ada Lovelace"},
                "updated": "2026-07-18T10:00:00.000+0000",
            },
        })

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues/PROJ-9")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"] == "PROJ-9"
    assert len(body["description"]) == len(long_desc)
    assert body["description"].endswith("TAIL-MARKER-AFTER-2000-CHARS")

    assert "/rest/api/3/issue/PROJ-9" in captured["url"]
    assert captured["auth"] == ("me@x.com", "SEKRET")
    assert "SEKRET" not in r.text


@pytest.mark.asyncio
async def test_issue_detail_unconfigured_returns_503(store, tmp_path):
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        r = await c.get("/api/integrations/jira/issues/PROJ-1")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_issue_detail_upstream_error_surfaces_as_502(client, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"errorMessages": ["boom"]}, status_code=500)

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues/PROJ-9")
    assert r.status_code == 502
    assert "SEKRET" not in r.text


# ── SCRUM-18: accidental re-import trap — the `imported` lookup field ──────

def _jira_issue_payload(key="SCRUM-18", status="In Progress"):
    return {
        "key": key,
        "fields": {
            "summary": "Some ticket",
            "description": "",
            "status": {"name": status},
            "assignee": None,
            "updated": "2026-07-18T10:00:00.000+0000",
        },
    }


@pytest.mark.asyncio
async def test_browse_marks_imported_ticket(client, store, monkeypatch):
    """A ticket that already has a board task (matched by external_id/key)
    must carry that task's status in the response's `imported` block."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    task = Task.new("SCRUM-18: Some ticket", source="jira", external_id="SCRUM-18")
    task.status = TaskStatus.DONE
    await store.create_task(task)

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"issues": [_jira_issue_payload()]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "ticket"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    imported = body[0]["imported"]
    assert imported is not None
    assert imported["task_id"] == task.id
    assert imported["status"] == "done"
    assert imported["count"] == 1


@pytest.mark.asyncio
async def test_no_match_leaves_imported_none(client, store, monkeypatch):
    """A ticket with no matching board task must leave `imported` unset —
    it must still be importable (no accidental "already handled" claim)."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    other = Task.new("Unrelated", source="jira", external_id="SCRUM-1")
    await store.create_task(other)
    # Same external_id as the browsed key but NOT a jira-sourced task: the
    # source filter must exclude it (cross-source id collisions never claim
    # a ticket is imported).
    collider = Task.new("Colliding id", source="freeform", external_id="SCRUM-18")
    await store.create_task(collider)

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"issues": [_jira_issue_payload(key="SCRUM-18")]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "ticket"})
    assert r.status_code == 200, r.text
    assert r.json()[0]["imported"] is None


@pytest.mark.asyncio
async def test_imported_lookup_uses_single_store_read(client, store, monkeypatch):
    """The match against the local store must be ONE read regardless of how
    many issues come back — never a per-row store or Jira call."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    task = Task.new("SCRUM-18: Some ticket", source="jira", external_id="SCRUM-18")
    await store.create_task(task)

    # SCRUM-54: the picker's local-store read is the narrow projection, not
    # list_tasks() — monkeypatch the projection method to verify the same
    # "exactly once" invariant against the new call site.
    calls = {"list_imported_tasks": 0}
    orig_projection = store.list_imported_tasks

    async def counted_projection(*a, **kw):
        calls["list_imported_tasks"] += 1
        return await orig_projection(*a, **kw)

    monkeypatch.setattr(store, "list_imported_tasks", counted_projection)

    search_calls = {"n": 0}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        search_calls["n"] += 1
        return _Resp({"issues": [
            _jira_issue_payload(key="SCRUM-18"),
            _jira_issue_payload(key="SCRUM-19"),
            _jira_issue_payload(key="SCRUM-20"),
        ]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "ticket"})
    assert r.status_code == 200, r.text
    assert len(r.json()) == 3
    assert calls["list_imported_tasks"] == 1, "the tasks store must be read exactly once, not once per issue"
    assert search_calls["n"] == 1, "the Jira adapter must be called exactly once, not once per issue"


@pytest.mark.asyncio
async def test_duplicate_external_ids_set_count(client, store, monkeypatch):
    """Two board tasks sharing the same external_id (a data-integrity bug)
    must surface as a count > 1 so the picker can warn, not silently pick
    one and hide the duplication."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    # Explicit created_at values pin the pick's SEMANTICS (newest created —
    # the sync's definition; see test_imported_chip_uses_newest_created_task_
    # like_the_sync for the updated_at-divergence regression case).
    t1 = Task.new("SCRUM-18 v1", source="jira", external_id="SCRUM-18")
    t1.created_at = "2026-07-02T00:00:00+00:00"
    await store.create_task(t1)
    t2 = Task.new("SCRUM-18 v2", source="jira", external_id="SCRUM-18")
    t2.created_at = "2026-07-01T00:00:00+00:00"
    await store.create_task(t2)

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"issues": [_jira_issue_payload()]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "ticket"})
    assert r.status_code == 200, r.text
    imported = r.json()[0]["imported"]
    assert imported["count"] == 2
    # The newest-CREATED match wins for task_id/status (the sync's definition).
    assert imported["task_id"] == t1.id


@pytest.mark.asyncio
async def test_imported_never_leaks_full_task_shape(client, store, monkeypatch):
    """The lookup is additive-only: task_id + status (+ count), never the
    full Task shape (description/requirements/context/etc)."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    task = Task.new(
        "SCRUM-18: Some ticket", source="jira", external_id="SCRUM-18",
        description="secret internal notes",
    )
    await store.create_task(task)

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"issues": [_jira_issue_payload()]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "ticket"})
    imported = r.json()[0]["imported"]
    assert set(imported.keys()) == {"task_id", "status", "count"}
    assert "secret internal notes" not in r.text


@pytest.mark.asyncio
async def test_test_connection_loads_token_from_env_not_only_serve(client, tmp_path, monkeypatch):
    """Regression (settings 'Test connection' bug): the health check must load
    JIRA_API_TOKEN from ~/.no_human/.env ITSELF, so it authenticates from a plain
    `nh start` — not only when `nh serve`'s Jira poll happened to load the token.
    Here the token lives ONLY in the .env file (never pre-set in os.environ), yet
    the endpoint must authenticate and never echo the secret back."""
    (tmp_path / ".env").write_text("JIRA_API_TOKEN=secret-shhh\n")
    assert "JIRA_API_TOKEN" not in os.environ  # proves it is not pre-loaded

    import no_human.integrations as integ

    async def _fake_get(url, headers=None, auth=None, timeout=10.0):
        assert auth == ("me@x.com", "secret-shhh")  # token was read from the .env
        return _Resp({"displayName": "Me"}, status_code=200)

    monkeypatch.setattr(integ, "_http_get", _fake_get)

    r = await client.post("/api/integrations/jira/test",
                          headers={"Origin": "http://127.0.0.1:8420"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["healthy"] is True, body
    assert "authenticated" in body["detail"].lower()
    assert "secret-shhh" not in body["detail"]  # never leak the token


@pytest.mark.asyncio
async def test_imported_chip_uses_newest_created_task_like_the_sync(
        client, store, monkeypatch):
    """Review 2026-07-25 residue: the sync and the picker chip must share ONE
    definition of "latest task per external_id" — newest (created_at, id),
    the sync's key. An older import that was merely touched later
    (updated_at) must not win the chip."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    older = Task.new("SCRUM-18: first import", source="jira",
                     external_id="SCRUM-18")
    older.status = TaskStatus.FAILED
    older.created_at = "2026-07-20T00:00:00+00:00"
    older.updated_at = "2026-07-25T09:00:00+00:00"  # touched recently
    await store.create_task(older)
    newer = Task.new("SCRUM-18: re-import", source="jira",
                     external_id="SCRUM-18")
    newer.status = TaskStatus.DONE
    newer.created_at = "2026-07-24T00:00:00+00:00"
    newer.updated_at = "2026-07-24T00:00:00+00:00"
    await store.create_task(newer)

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        return _Resp({"issues": [_jira_issue_payload()]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    r = await client.get("/api/integrations/jira/issues", params={"q": "ticket"})
    assert r.status_code == 200, r.text
    imported = r.json()[0]["imported"]
    assert imported["task_id"] == newer.id, \
        "chip must follow the sync's newest-created definition"
    assert imported["status"] == "done"
    assert imported["count"] == 2
