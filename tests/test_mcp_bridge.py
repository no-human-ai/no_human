"""Tests for the MCP stdio bridge (SCRUM-63): tool schemas, task_add/task_status
against a stubbed HTTP layer (httpx.MockTransport, no real server), and the
unreachable-API startup refusal. Never talks to a real network."""

import functools
import json
import logging

import httpx
import pytest

from no_human.intake import mcp_bridge


@pytest.fixture(autouse=True)
def _reset_transport():
    """Every test stubs its own transport; never leak one across tests."""
    yield
    mcp_bridge._TRANSPORT = None


@pytest.fixture(autouse=True)
def _reset_base_url_cache():
    """`_base_url` is cached for the process, so a resolved value would
    otherwise leak into whichever test runs next."""
    mcp_bridge._base_url.cache_clear()
    yield
    mcp_bridge._base_url.cache_clear()


def _set_transport(handler):
    mcp_bridge._TRANSPORT = httpx.MockTransport(handler)


def _config(data):
    """Stand in for `load_config`, returning an object with a `.data` mapping."""
    return lambda **kwargs: type("_Config", (), {"data": data})()


async def test_tool_schemas():
    tools = {t.name: t for t in await mcp_bridge.mcp.list_tools()}
    assert set(tools) == {"task_add", "task_status"}

    add_schema = tools["task_add"].inputSchema
    assert set(add_schema["required"]) == {"title", "description", "repo_path"}
    assert add_schema["properties"]["title"]["type"] == "string"
    assert add_schema["properties"]["description"]["type"] == "string"
    assert add_schema["properties"]["repo_path"]["type"] == "string"

    status_schema = tools["task_status"].inputSchema
    assert status_schema["required"] == ["task_id_or_external_id"]
    assert status_schema["properties"]["task_id_or_external_id"]["type"] == "string"


async def test_task_add_posts_source_mcp_and_returns_compact_json():
    """task_add POSTs source='mcp' (spec); create_task in api/app.py persists
    'mcp' as a first-class source (allowlisted alongside 'board'/'jira'), so
    the response — and what we return — carries 'mcp' straight through."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tasks"
        body = json.loads(request.content)
        captured.update(body)
        assert body["source"] == "mcp"
        return httpx.Response(
            201,
            json={"id": "task-abc123", "source": "mcp", "title": body["title"]},
        )

    _set_transport(handler)
    result = await mcp_bridge.mcp.call_tool(
        "task_add",
        {"title": "Fix thing", "description": "details", "repo_path": "/tmp/repo"},
    )
    text = result[0][0].text
    payload = json.loads(text)

    # compact: no whitespace padding
    assert text == '{"task_id":"task-abc123","source":"mcp"}'
    assert payload == {"task_id": "task-abc123", "source": "mcp"}
    # the request we actually sent did carry source='mcp' per spec
    assert captured["title"] == "Fix thing"
    assert captured["description"] == "details"
    assert captured["repo_path"] == "/tmp/repo"
    assert captured["source"] == "mcp"


async def test_task_status_direct_id_hit_returns_full_object():
    full_task = {
        "id": "task-abc123",
        "external_id": None,
        "source": "board",
        "title": "Fix thing",
        "description": "details",
        "status": "PENDING",
        "created_at": "2026-07-26T00:00:00Z",
        "updated_at": "2026-07-26T00:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/tasks/task-abc123"
        return httpx.Response(200, json=full_task)

    _set_transport(handler)
    result = await mcp_bridge.mcp.call_tool(
        "task_status", {"task_id_or_external_id": "task-abc123"}
    )
    payload = json.loads(result[0][0].text)
    assert payload == full_task


async def test_task_status_falls_back_to_external_id_scan():
    """GET /api/tasks/{id} only resolves by id/id-prefix (db.find_task), not
    external_id, so a direct 404 falls back to scanning the task list."""
    full_task = {"id": "real-id", "external_id": "JIRA-9", "status": "IMPLEMENTING"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tasks/JIRA-9":
            return httpx.Response(404, json={"detail": "not found"})
        if request.url.path == "/api/tasks" and request.method == "GET":
            return httpx.Response(
                200, json=[{"id": "other", "external_id": None}, full_task]
            )
        if request.url.path == "/api/tasks/real-id":
            return httpx.Response(200, json=full_task)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    _set_transport(handler)
    result = await mcp_bridge.mcp.call_tool(
        "task_status", {"task_id_or_external_id": "JIRA-9"}
    )
    payload = json.loads(result[0][0].text)
    assert payload == full_task


async def test_task_status_not_found_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tasks" and request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": "not found"})

    _set_transport(handler)
    with pytest.raises(Exception):
        await mcp_bridge.mcp.call_tool(
            "task_status", {"task_id_or_external_id": "nope"}
        )


def test_ensure_api_reachable_refuses_on_connect_error(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    _set_transport(handler)
    with caplog.at_level("ERROR"):
        with pytest.raises(SystemExit):
            mcp_bridge._ensure_api_reachable()
    assert "unreachable" in caplog.text
    assert mcp_bridge.BASE_URL in caplog.text


def test_ensure_api_reachable_ok_when_api_up():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    _set_transport(handler)
    mcp_bridge._ensure_api_reachable()  # must not raise


def test_base_url_follows_server_port_from_config(monkeypatch):
    """An operator who moves the board must not be left dialling 8420."""
    monkeypatch.setattr(
        mcp_bridge, "load_config", _config({"server": {"host": "127.0.0.1", "port": 9999}})
    )
    assert mcp_bridge._base_url() == "http://127.0.0.1:9999"


def test_base_url_falls_back_when_config_unreadable(monkeypatch, caplog):
    """A missing or broken config leaves a default install working as before,
    and says so rather than failing silently later on."""

    def _boom(**kwargs):
        raise OSError("no config here")

    monkeypatch.setattr(mcp_bridge, "load_config", _boom)
    with caplog.at_level(logging.WARNING):
        assert mcp_bridge._base_url() == "http://127.0.0.1:8420"
    assert "OSError" in caplog.text
    assert "no config here" in caplog.text


def test_base_url_falls_back_when_server_is_not_a_mapping(monkeypatch, caplog):
    """`server: 8421` parses fine but is not a mapping. Warn and use the
    default rather than raising AttributeError out of the bridge."""
    monkeypatch.setattr(mcp_bridge, "load_config", _config({"server": 8421}))
    with caplog.at_level(logging.WARNING):
        assert mcp_bridge._base_url() == "http://127.0.0.1:8420"
    assert "not a mapping" in caplog.text


def test_reachability_check_dials_the_configured_port(monkeypatch):
    """The operator-visible wiring: main() checks the port from the config,
    not the BASE_URL literal."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    monkeypatch.setattr(mcp_bridge, "load_config", _config({"server": {"port": 9999}}))
    monkeypatch.setattr(mcp_bridge.mcp, "run", lambda: None)
    _set_transport(handler)

    mcp_bridge.main()

    assert seen == ["http://127.0.0.1:9999/api/tasks"]


def test_base_url_reads_host_and_port_from_a_real_config_file(tmp_path, monkeypatch):
    """The real `load_config` path, not a stub: the file is parsed, merged onto
    the defaults, and a non-default HOST is honoured as well as the port."""
    import no_human.config as config

    path = tmp_path / "config.yaml"
    path.write_text("server:\n  host: 192.168.1.20\n  port: 9001\n")
    monkeypatch.setattr(
        mcp_bridge, "load_config", functools.partial(config.load_config, path)
    )
    assert mcp_bridge._base_url() == "http://192.168.1.20:9001"


def test_a_config_the_product_refuses_stops_the_bridge(tmp_path, monkeypatch):
    """An ANTHROPIC_API_KEY inside config.yaml is refused by every other entry
    point. The bridge must refuse too — not warn and dial the default port
    while the file says 9001."""
    import no_human.config as config

    path = tmp_path / "config.yaml"
    path.write_text(
        "server:\n  port: 9001\nllm:\n  ANTHROPIC_API_KEY: not-a-real-key\n"
    )
    monkeypatch.setattr(
        mcp_bridge, "load_config", functools.partial(config.load_config, path)
    )
    with pytest.raises(config.AuthError):
        mcp_bridge._base_url()
