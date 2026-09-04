"""GET /api/integrations/linear/issues — the Linear half of the Backlog page.

These routes did not exist, and the page explained their absence with a claim
about the code that was false ("the Linear side has no issue listing yet").
``LinearAdapter.search()`` has been a working paginating GraphQL listing all
along; only the HTTP route was missing. So the contract asserted here is
deliberately the SAME one tests/test_jira_issues_endpoint.py asserts — one row
shape, one imported-chip rule, one failure convention — because the page has to
treat a row from either tracker identically.

Mocking style mirrors tests/test_intake_linear.py (httpx.post monkeypatched on
the intake.linear module) and test_jira_issues_endpoint.py's client fixture.
No task is ever created here; POST /api/tasks stays the one create path.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import no_human.config as nh_config
from no_human.api.app import app
from no_human.core.task import Task, TaskStatus


def _cfg(**over):
    lin = {"enabled": True, "team_key": "NO",
           "state_types": ["triage", "backlog", "unstarted"]}
    lin.update(over)
    return {"integrations": {"linear": lin}}


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _issue(n=1, title="Ticket number one", description="body", state="Todo"):
    return {
        "id": f"uuid-{n}",
        "identifier": f"NO-{n}",
        "title": title,
        "description": description,
        "url": f"https://linear.app/acme/issue/NO-{n}",
        "priorityLabel": "Medium",
        "createdAt": "2026-08-01T09:00:00.000Z",
        "updatedAt": "2026-08-01T10:00:00.000Z",
        "state": {"id": "s1", "name": state, "type": "unstarted"},
        "assignee": {"id": "u1", "name": "ada", "displayName": "Ada Lovelace"},
        "labels": {"nodes": []},
        "team": {"id": "t1", "key": "NO", "name": "no_human"},
    }


def _page(nodes):
    return {"data": {"issues": {"nodes": nodes,
                                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    yield tmp_path
    # load_env_var writes straight into os.environ (untracked by monkeypatch).
    os.environ.pop("LINEAR_API_KEY", None)


@pytest_asyncio.fixture
async def client(store, tmp_path):
    import yaml
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(_cfg()))
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


def _stub_post(monkeypatch, payload, captured=None, raises=None):
    def fake_post(url, headers=None, timeout=None, json=None):
        if captured is not None:
            captured.setdefault("calls", []).append(json)
            captured["headers"] = headers
        if raises is not None:
            raise raises
        return _Resp(payload) if not isinstance(payload, _Resp) else payload

    monkeypatch.setattr("no_human.intake.linear.httpx.post", fake_post)


# ── configured / unconfigured ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unconfigured_returns_503_with_clear_detail(store, tmp_path):
    """No linear block at all — the same 503 shape the Jira route gives."""
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        r = await c.get("/api/integrations/linear/issues")
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()
    assert "linear" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_unconfigured_when_key_missing(client, monkeypatch):
    """team_key present but no LINEAR_API_KEY → still unconfigured, never a
    request with an empty Authorization header."""
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    r = await client.get("/api/integrations/linear/issues")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_key_only_in_env_file_is_loaded_on_demand(client, tmp_path, monkeypatch):
    """Under `nh start` (the board) LINEAR_API_KEY is NOT in the process env —
    only `nh serve`'s poller loads it. The route must read it from
    ~/.no_human/.env itself, or a configured integration wrongly 503s."""
    (tmp_path / ".env").write_text("LINEAR_API_KEY=dotenv-secret\n")
    assert "LINEAR_API_KEY" not in os.environ
    captured: dict = {}
    _stub_post(monkeypatch, _page([_issue(1)]), captured)

    r = await client.get("/api/integrations/linear/issues")
    assert r.status_code == 200, r.text  # NOT 503
    # The RAW key, not "Bearer <key>" — the documented form for personal keys.
    assert captured["headers"]["Authorization"] == "dotenv-secret"
    assert "dotenv-secret" not in r.text


# ── the row shape ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_rows_in_the_shared_tracker_shape(client, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "SEKRET")
    _stub_post(monkeypatch, _page([_issue(1), _issue(2, title="Second")]))

    r = await client.get("/api/integrations/linear/issues")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["key"] for i in body] == ["NO-1", "NO-2"]
    first = body[0]
    assert first["tracker"] == "linear", "a row must say which tracker it came from"
    assert first["summary"] == "Ticket number one"
    assert first["status"] == "Todo"
    assert first["assignee"] == "Ada Lovelace"
    assert first["updated"] == "2026-08-01T10:00:00.000Z"
    assert first["url"].endswith("/issue/NO-1")
    assert first["imported"] is None
    assert "SEKRET" not in r.text


@pytest.mark.asyncio
async def test_list_truncates_description_and_detail_does_not(client, monkeypatch):
    """Same split as Jira: the list payload is short, the detail carries the
    whole spec so a created task is not silently cut off at 2000 chars."""
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    long_desc = "x" * 2000 + "TAIL-MARKER-AFTER-2000-CHARS"
    _stub_post(monkeypatch, _page([_issue(1, description=long_desc)]))

    listed = await client.get("/api/integrations/linear/issues")
    assert len(listed.json()[0]["description"]) == 2000

    detail = await client.get("/api/integrations/linear/issues/NO-1")
    assert detail.status_code == 200, detail.text
    assert detail.json()["description"].endswith("TAIL-MARKER-AFTER-2000-CHARS")
    assert detail.json()["tracker"] == "linear"


@pytest.mark.asyncio
async def test_detail_of_an_issue_outside_the_scope_is_404_not_an_empty_row(
        client, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    _stub_post(monkeypatch, _page([_issue(1)]))
    r = await client.get("/api/integrations/linear/issues/NO-99")
    assert r.status_code == 404
    assert "NO-99" in r.json()["detail"]


# ── the scope and the query ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scope_is_the_operator_authored_filter_never_the_query(client, monkeypatch):
    """The GraphQL filter is the poller's own scope — team + state types. The
    typed query must never be pushed into it (that is how a picker starts
    listing closed tickets, and how an unverified filter key 400s)."""
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    captured: dict = {}
    _stub_post(monkeypatch, _page([_issue(1)]), captured)

    r = await client.get("/api/integrations/linear/issues", params={"q": "number"})
    assert r.status_code == 200, r.text
    # The ISSUE search, not the intake-config check search() runs first.
    searches = [c for c in captured["calls"] if "NoHumanIssues" in c["query"]]
    assert len(searches) == 1
    flt = searches[0]["variables"]["filter"]
    assert flt["team"] == {"key": {"eq": "NO"}}
    assert flt["state"] == {"type": {"in": ["triage", "backlog", "unstarted"]}}
    assert "number" not in str(flt), "the typed query must not reach the GraphQL filter"


@pytest.mark.asyncio
async def test_query_narrows_the_listed_scope(client, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    _stub_post(monkeypatch, _page([
        _issue(1, title="Fix the retry loop"),
        _issue(2, title="Add an index", description="nothing about retries"),
        _issue(3, title="Docs", description="mentions the retry loop in passing"),
    ]))

    listed = await client.get("/api/integrations/linear/issues", params={"q": "retry"})
    assert [i["key"] for i in listed.json()] == ["NO-1", "NO-3"], \
        "the filter must match title AND description, and drop the rest"

    browsed = await client.get("/api/integrations/linear/issues", params={"q": ""})
    assert len(browsed.json()) == 3, "an empty query browses the whole scope"


@pytest.mark.asyncio
async def test_limit_is_clamped_between_1_and_50(client, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    _stub_post(monkeypatch, _page([_issue(n) for n in range(1, 61)]))

    assert len((await client.get("/api/integrations/linear/issues",
                                 params={"limit": 500})).json()) == 50
    assert len((await client.get("/api/integrations/linear/issues",
                                 params={"limit": 0})).json()) == 1
    assert len((await client.get("/api/integrations/linear/issues")).json()) == 20


# ── failures never leak the key ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_failure_returns_a_distinct_502_message(client, monkeypatch):
    """Linear answers auth failure at 401 with a GraphQL errors array; the
    adapter's exception TYPE is the classification, not the status code."""
    monkeypatch.setenv("LINEAR_API_KEY", "SEKRET")
    _stub_post(monkeypatch, _Resp(
        {"errors": [{"message": "Authentication required",
                     "extensions": {"code": "AUTHENTICATION_ERROR"}}]},
        status_code=401))

    r = await client.get("/api/integrations/linear/issues")
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "key" in detail.lower()
    assert "rotate" in detail.lower() or "verify" in detail.lower()
    assert "team key" not in detail.lower(), "must not be the generic config message"
    assert "SEKRET" not in r.text


@pytest.mark.asyncio
async def test_rate_limit_is_not_reported_as_a_broken_configuration(client, monkeypatch):
    """Linear rate-limits with HTTP 400 + extensions.code RATELIMITED. Telling
    the operator to check their team key would send them to fix nothing."""
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    _stub_post(monkeypatch, _Resp(
        {"errors": [{"message": "rate limited", "extensions": {"code": "RATELIMITED"}}]},
        status_code=400))

    r = await client.get("/api/integrations/linear/issues")
    assert r.status_code == 502
    detail = r.json()["detail"].lower()
    assert "rate" in detail
    assert "team key" not in detail


@pytest.mark.asyncio
async def test_connection_error_surfaces_as_502_and_never_logs_the_key(
        client, monkeypatch, caplog):
    import logging

    import httpx as _httpx

    monkeypatch.setenv("LINEAR_API_KEY", "SEKRET")
    _stub_post(monkeypatch, None, raises=_httpx.ConnectError("connection refused"))

    with caplog.at_level(logging.DEBUG):
        r = await client.get("/api/integrations/linear/issues")
    assert r.status_code == 502
    assert "SEKRET" not in r.text
    assert "SEKRET" not in caplog.text


@pytest.mark.asyncio
async def test_a_misconfigured_team_key_is_a_503_that_says_what_to_change(
        client, monkeypatch, caplog):
    """The Backlog page listed nothing at all for a team key that names no
    team, with a 200 and an empty array. The adapter builds this message out
    of the operator's own config and the names the API returned — never out of
    a request — so it is the one Linear failure whose text can be shown."""
    import logging

    monkeypatch.setenv("LINEAR_API_KEY", "SEKRET")

    def fake_post(url, headers=None, timeout=None, json=None):
        if "NoHumanTeams" in json["query"]:
            return _Resp({"data": {"teams": {
                "nodes": [{"id": "t1", "key": "NO"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}})
        return _page([_issue(1)])

    monkeypatch.setattr("no_human.intake.linear.httpx.post", fake_post)
    import yaml
    (nh_config.CONFIG_PATH).write_text(yaml.safe_dump(_cfg(team_key="N0")))
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)

    with caplog.at_level(logging.DEBUG):
        r = await client.get("/api/integrations/linear/issues")
    assert r.status_code == 503
    assert "integrations.linear.team_key" in r.json()["detail"]
    assert "SEKRET" not in r.text and "SEKRET" not in caplog.text


# ── the imported chip, and the cross-tracker guard ────────────────────────

@pytest.mark.asyncio
async def test_marks_a_ticket_that_already_has_a_board_task(client, store, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    task = Task.new("NO-1: Ticket number one", source="linear", external_id="NO-1")
    task.status = TaskStatus.DONE
    await store.create_task(task)
    _stub_post(monkeypatch, _page([_issue(1)]))

    r = await client.get("/api/integrations/linear/issues")
    imported = r.json()[0]["imported"]
    assert imported is not None
    assert imported["task_id"] == task.id
    assert imported["status"] == "done"
    assert imported["count"] == 1
    assert set(imported.keys()) == {"task_id", "status", "count"}, \
        "never the full Task shape"


@pytest.mark.asyncio
async def test_a_jira_task_with_the_same_key_never_claims_a_linear_ticket(
        client, store, monkeypatch):
    """Both trackers mint keys like PROJ-1, and the Backlog page lists them
    side by side. Dedupe keys on (source, external_id), so a Jira NO-1 must
    leave the Linear NO-1 importable — otherwise a ticket nobody has started
    reads as already handled and is dropped out of every bulk affordance."""
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    jira_twin = Task.new("NO-1 over on Jira", source="jira", external_id="NO-1")
    await store.create_task(jira_twin)
    _stub_post(monkeypatch, _page([_issue(1)]))

    r = await client.get("/api/integrations/linear/issues")
    assert r.json()[0]["imported"] is None


@pytest.mark.asyncio
async def test_imported_lookup_is_one_store_read_for_the_whole_list(
        client, store, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    await store.create_task(Task.new("NO-1", source="linear", external_id="NO-1"))
    calls = {"n": 0}
    orig = store.list_imported_tasks

    async def counted(*a, **kw):
        calls["n"] += 1
        return await orig(*a, **kw)

    monkeypatch.setattr(store, "list_imported_tasks", counted)
    _stub_post(monkeypatch, _page([_issue(1), _issue(2), _issue(3)]))

    r = await client.get("/api/integrations/linear/issues")
    assert len(r.json()) == 3
    assert calls["n"] == 1, "one store read for the list, never one per row"


@pytest.mark.asyncio
async def test_duplicate_external_ids_surface_as_a_count(client, store, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "t")
    t1 = Task.new("NO-1 v1", source="linear", external_id="NO-1")
    t1.created_at = "2026-08-02T00:00:00+00:00"
    await store.create_task(t1)
    t2 = Task.new("NO-1 v2", source="linear", external_id="NO-1")
    t2.created_at = "2026-08-01T00:00:00+00:00"
    await store.create_task(t2)
    _stub_post(monkeypatch, _page([_issue(1)]))

    imported = (await client.get("/api/integrations/linear/issues")).json()[0]["imported"]
    assert imported["count"] == 2
    # Newest CREATED wins — the same definition the sync uses.
    assert imported["task_id"] == t1.id
