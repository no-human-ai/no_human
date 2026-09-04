"""The loopback boundary on the unauthenticated board API (`api/local_boundary.py`).

Four checks:
  * `local_boundary` middleware — Host must be loopback or the configured
    `server.host` (DNS-rebinding), and a cross-origin browser write is refused.
  * CORS `allow_origin_regex`    — a cross-origin page cannot read responses.
  * `ws_board`                   — the same Host/Origin gate on the WebSocket.

A same-user non-browser client (the `nh` CLI, the MCP bridge) sends no
`Origin`, so it is unaffected — the tests pin that too. The `nh` CLI addresses
the board by the CONFIGURED `server.host` (`cli/shell.py:base_url_from_config`),
so a non-loopback bind must still accept its own host — pinned below.
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from starlette.testclient import TestClient
from fastapi import FastAPI

from no_human.api.app import app, ws_board
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.config import load_config

EVIL = "https://evil.example"
LOCAL = "http://localhost:8420"


@pytest_asyncio.fixture
async def client(store_factory, tmp_path):
    store = await store_factory("b.db")
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://localhost") as c:
        yield c


def _rule(title):
    return {"title": title, "content": "be nice", "tags": [], "project": None}


# ------------------------------- writes ------------------------------------ #

@pytest.mark.asyncio
async def test_cross_origin_write_is_refused(client):
    r = await client.post("/api/rules", json=_rule("x1"), headers={"Origin": EVIL})
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "cross_origin_refused"


@pytest.mark.asyncio
async def test_lookalike_origin_is_refused(client):
    # exact-host match, not startswith: localhost.evil.com is an attacker domain
    r = await client.post("/api/rules", json=_rule("x2"),
                          headers={"Origin": "http://localhost.evil.com"})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_absent_origin_write_is_allowed(client):
    # the nh CLI / MCP bridge send no Origin — must still work
    r = await client.post("/api/rules", json=_rule("x3"))
    assert r.status_code != 403, r.text


@pytest.mark.asyncio
async def test_same_origin_write_is_allowed(client):
    r = await client.post("/api/rules", json=_rule("x4"), headers={"Origin": LOCAL})
    assert r.status_code != 403, r.text


# -------------------------------- Host ------------------------------------- #

@pytest.mark.asyncio
async def test_non_loopback_host_is_refused(client):
    # DNS rebinding: the request reaches us with the attacker's domain in Host
    r = await client.get("/api/tasks", headers={"Host": "attacker.example"})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "bad_host"


@pytest.mark.asyncio
async def test_loopback_host_passes(client):
    r = await client.get("/api/tasks", headers={"Host": "127.0.0.1:8420"})
    assert r.status_code == 200, r.text


# --------------------------------- read ------------------------------------ #

@pytest.mark.asyncio
async def test_cross_origin_read_gets_no_cors_grant(client):
    r = await client.get("/api/tasks", headers={"Origin": EVIL})
    # request still processes, but the browser is told nothing: no ACAO for evil
    acao = r.headers.get("access-control-allow-origin")
    assert acao != EVIL and acao != "*", f"leaked ACAO={acao!r}"


@pytest.mark.asyncio
async def test_same_origin_read_is_granted(client):
    r = await client.get("/api/tasks", headers={"Origin": LOCAL})
    assert r.headers.get("access-control-allow-origin") == LOCAL


@pytest.mark.asyncio
async def test_cross_origin_gets_no_allow_credentials_grant(client):
    # allow_credentials is never enabled (defaults to False), so no page — cross
    # or same origin — is ever told it may send credentials with a CORS request.
    for origin in (EVIL, LOCAL):
        r = await client.get("/api/tasks", headers={"Origin": origin})
        acac = r.headers.get("access-control-allow-credentials")
        assert acac in (None, "false"), f"leaked ACAC={acac!r} for {origin}"
    # An adversarial CORS preflight from a foreign origin is not granted
    # credentials either.
    pre = await client.options(
        "/api/tasks",
        headers={"Origin": EVIL, "Access-Control-Request-Method": "GET"},
    )
    assert pre.headers.get("access-control-allow-credentials") in (None, "false")


# ------------------------------ websocket ---------------------------------- #

def _ws_shim(store):
    shim = FastAPI()
    shim.state.store = store
    shim.websocket("/ws")(ws_board)
    return shim


# TestClient hardcodes the WebSocket `Host` to "testserver" regardless of
# base_url, so the tests set `host` explicitly to isolate each check. A real
# browser sends the true Host/Origin, which is what production sees.

@pytest.mark.asyncio
async def test_ws_cross_origin_is_rejected(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    store = await Store(tmp_path / "ws.db").connect()
    try:
        with TestClient(_ws_shim(store)) as tc:
            with pytest.raises(WebSocketDisconnect):
                with tc.websocket_connect(
                        "/ws", headers={"host": "localhost", "origin": EVIL}) as ws:
                    ws.receive_text()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ws_non_loopback_host_is_rejected(tmp_path):
    from starlette.websockets import WebSocketDisconnect
    store = await Store(tmp_path / "wsh.db").connect()
    try:
        with TestClient(_ws_shim(store)) as tc:
            with pytest.raises(WebSocketDisconnect):
                with tc.websocket_connect(
                        "/ws", headers={"host": "attacker.example", "origin": LOCAL}) as ws:
                    ws.receive_text()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ws_same_origin_is_accepted(tmp_path):
    store = await Store(tmp_path / "ws2.db").connect()
    try:
        t = Task.new("visible", repo_path="/tmp/r")
        t.acceptance_criteria = ["n/a"]
        await store.create_task(t)
        with TestClient(_ws_shim(store)) as tc:
            with tc.websocket_connect(
                    "/ws", headers={"host": "localhost", "origin": LOCAL}) as ws:
                msg = ws.receive_json()
                assert msg["type"] == "init"
    finally:
        await store.close()


# ------------------------------ edge cases --------------------------------- #

@pytest.mark.asyncio
async def test_null_origin_write_is_refused(client):
    # `Origin: null` is what a sandboxed iframe / file:// page sends
    r = await client.post("/api/rules", json=_rule("x5"), headers={"Origin": "null"})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_userinfo_in_host_or_origin_is_refused(client):
    r = await client.get("/api/tasks", headers={"Host": "evil.example@127.0.0.1"})
    assert r.status_code == 400, r.text
    r = await client.post("/api/rules", json=_rule("x6"),
                          headers={"Origin": "http://evil.example@localhost"})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_missing_host_is_refused(client):
    r = await client.get("/api/tasks", headers={"Host": ""})
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_ipv6_and_case_varied_loopback_pass(client):
    for host in ("[::1]:8420", "LOCALHOST", "Localhost:8420"):
        r = await client.get("/api/tasks", headers={"Host": host})
        assert r.status_code == 200, (host, r.text)
    r = await client.post("/api/rules", json=_rule("x7"), headers={"Origin": "http://[::1]:5173"})
    assert r.status_code != 403, r.text


@pytest.mark.asyncio
async def test_cross_origin_preflight_gets_no_grant_but_is_not_a_write(client):
    r = await client.options("/api/rules", headers={
        "Origin": EVIL, "Access-Control-Request-Method": "POST"})
    assert r.status_code != 403, r.text  # OPTIONS is not a mutating verb
    assert r.headers.get("access-control-allow-origin") != EVIL


# --------------------- the configured server.host ------------------------- #

@pytest_asyncio.fixture
async def wide_client(tmp_path):
    """A board configured to bind a non-loopback host, as `nh start --host` /
    `server.host` allow. The nh CLI then addresses it by that host."""
    store = await Store(tmp_path / "w.db").connect()
    app.state.store = store
    previous = getattr(app.state, "config", None)
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["server"]["host"] = "board.local"
    app.state.config = cfg
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://board.local") as c:
            yield c
    finally:
        app.state.config = previous  # the module-level app is shared
        await store.close()


@pytest.mark.asyncio
async def test_configured_server_host_is_accepted_as_host(wide_client):
    # the CLI's own channel (`http://{server.host}:{port}`), read and write
    r = await wide_client.get("/api/tasks", headers={"Host": "board.local:8420"})
    assert r.status_code == 200, r.text
    r = await wide_client.post("/api/rules", json=_rule("x8"), headers={"Host": "BOARD.LOCAL"})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_configured_server_host_is_accepted_as_origin_but_not_granted_cors(wide_client):
    # the board page served from that host is same-origin: its writes pass
    r = await wide_client.post("/api/rules", json=_rule("x9"),
                               headers={"Origin": "http://board.local:8420"})
    assert r.status_code == 201, r.text
    # ...and it needs no CORS grant, so none is given (a cross-origin page on
    # a look-alike gets nothing either)
    r = await wide_client.get("/api/tasks", headers={"Origin": "http://board.local:8420"})
    assert r.headers.get("access-control-allow-origin") is None


@pytest.mark.asyncio
async def test_other_hosts_are_still_refused_on_a_wide_bind(wide_client):
    for host in ("attacker.example", "board.local.evil.example", "evil.example@board.local"):
        r = await wide_client.get("/api/tasks", headers={"Host": host})
        assert r.status_code == 400, (host, r.text)
    r = await wide_client.post("/api/rules", json=_rule("x10"), headers={"Origin": EVIL})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_wildcard_bind_accepts_its_literal_host(tmp_path):
    # `server.host: 0.0.0.0` (the container image's bind): the CLI sends
    # Host 0.0.0.0:8420 literally, so that literal must pass
    store = await Store(tmp_path / "z.db").connect()
    app.state.store = store
    previous = getattr(app.state, "config", None)
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["server"]["host"] = "0.0.0.0"
    app.state.config = cfg
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://0.0.0.0:8420") as c:
            assert (await c.get("/api/tasks")).status_code == 200
            assert (await c.get("/api/tasks", headers={"Host": "attacker.example"})).status_code == 400
    finally:
        app.state.config = previous
        await store.close()


def test_ws_gate_accepts_the_configured_host():
    from no_human.api.local_boundary import ws_handshake_is_local
    allowed = frozenset({"127.0.0.1", "localhost", "::1", "board.local"})
    assert ws_handshake_is_local({"host": "board.local:8420"}, allowed)
    assert ws_handshake_is_local({"host": "board.local", "origin": "http://board.local:8420"}, allowed)
    assert not ws_handshake_is_local({"host": "board.local:8420"})  # default allow-list: loopback only
    assert not ws_handshake_is_local({"host": "board.local", "origin": EVIL}, allowed)
