"""feature_used telemetry at the three closed-vocabulary call sites.

Pins: (1) the FEATURE_* constant set app.py's three call sites read from, (2)
that every name in it is wire-sendable under telemetry.py's UNTOUCHED
`_ALLOWED_EVENTS["feature_used"]` allowlist, (3) each site emits exactly its
name (and nothing else) on acceptance and stays silent on rejection, (4) the
payload is exactly `{name}` with no dynamic/operator-derived string ever
reaching a call site (a static AST guard), and (5) fail-open/consent-off is
inherited from `telemetry.record` unchanged (no new try/except here).
"""
from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human import telemetry
from no_human.api.app import app
from no_human.core.db import Store
from no_human.core.task import Task

app_module = importlib.import_module("no_human.api.app")

APP_PY_PATH = Path(__file__).resolve().parent.parent / "src" / "no_human" / "api" / "app.py"

# Tests here reach `config.load_env_var`, which reads the operator's real
# ``~/.no_human/.env`` BEFORE the process env. Requested by NAME through
# `usefixtures` — never an autouse marker; see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def client(store, tmp_path):
    app.state.store = store
    app.state.config = nh_config.load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


@pytest_asyncio.fixture
async def local_client(store, tmp_path):
    """Same app, but with the local Origin header the integrations write
    route requires (`_require_local_origin`) — mirrors
    tests/test_integrations_write.py's `client` fixture."""
    app.state.store = store
    app.state.config = nh_config.load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost",
                           headers={"Origin": "http://127.0.0.1:8420"}) as c:
        yield c


@pytest.fixture
def recorded(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))
    return sent


@pytest.fixture(autouse=True)
def _isolated_integration_paths(tmp_path, monkeypatch):
    """Same isolation as tests/test_integrations_write.py: the config-save
    site writes ~/.no_human/.env, so every test in this module (not just the
    integration ones) must never touch the operator's real files."""
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")


async def _seed_task(store: Store, *, title="a task") -> Task:
    t = Task.new(title, repo_path="/tmp/repo")
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    return t


# --------------------------------------------------------------------------- #
# 1. Closed vocabulary                                                        #
# --------------------------------------------------------------------------- #

def test_feature_vocabulary_is_the_documented_closed_set():
    assert app_module.FEATURE_NAMES == {
        "backlog_import", "attachment_added", "integration_saved",
    }
    assert app_module.FEATURE_BACKLOG_IMPORT == "backlog_import"
    assert app_module.FEATURE_ATTACHMENT_ADDED == "attachment_added"
    assert app_module.FEATURE_INTEGRATION_SAVED == "integration_saved"


def test_every_feature_name_is_wire_sendable():
    """Reads the wire contract (`telemetry._sendable`); never edits it — the
    contract's own pin lives in tests/test_telemetry.py, untouched here."""
    for n in app_module.FEATURE_NAMES:
        event = {"name": "feature_used", "ts": 1786889836, "props": {"name": n}}
        assert telemetry._sendable(event) is True


# --------------------------------------------------------------------------- #
# 2. Site 1 — backlog import/start acceptance (create_task)                   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_backlog_import_create_emits_backlog_import(client, recorded):
    r = await client.post("/api/tasks", json={
        "title": "Fix the thing", "source": "jira", "external_id": "NO-1",
    })
    assert r.status_code == 201, r.text
    assert recorded.count(("feature_used", {"name": "backlog_import"})) == 1


@pytest.mark.asyncio
async def test_linear_source_also_emits_backlog_import(client, recorded):
    r = await client.post("/api/tasks", json={
        "title": "Fix the other thing", "source": "linear",
    })
    assert r.status_code == 201, r.text
    assert recorded.count(("feature_used", {"name": "backlog_import"})) == 1
    # Task.source's own allowlist clamps "linear" to "board" — unchanged
    # behaviour; the feature_used emission does not widen it.
    assert r.json()["source"] == "board"


@pytest.mark.asyncio
async def test_typed_board_task_emits_no_feature_used(client, recorded):
    r = await client.post("/api/tasks", json={"title": "Typed by hand"})
    assert r.status_code == 201, r.text
    assert [e for e in recorded if e[0] == "feature_used"] == []


# --------------------------------------------------------------------------- #
# 3. Site 2 — task composer attachment acceptance (add_attachment)            #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_attachment_upload_emits_attachment_added(
    client, store, tmp_path, monkeypatch, recorded,
):
    monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", tmp_path / "nh_home")
    t = await _seed_task(store, title="bug with a screenshot")
    files = {"file": ("shot.png", b"\x89PNG-fake-bytes", "image/png")}
    r = await client.post(f"/api/tasks/{t.id}/attachments", files=files)
    assert r.status_code == 200, r.text
    assert recorded.count(("feature_used", {"name": "attachment_added"})) == 1


@pytest.mark.asyncio
async def test_oversize_attachment_emits_nothing(
    client, store, tmp_path, monkeypatch, recorded,
):
    monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", tmp_path / "nh_home")
    monkeypatch.setattr(app_module, "_ATTACHMENT_MAX_BYTES", 4)
    t = await _seed_task(store, title="bug with a huge file")
    files = {"file": ("shot.png", b"way-too-big", "image/png")}
    r = await client.post(f"/api/tasks/{t.id}/attachments", files=files)
    assert r.status_code == 413, r.text
    assert [e for e in recorded if e[0] == "feature_used"] == []


# --------------------------------------------------------------------------- #
# 4. Site 3 — integration config save                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_integration_config_save_emits_integration_saved(
    local_client, recorded,
):
    r = await local_client.put("/api/integrations/jira/config", json={"fields": {
        "site": "https://acme.atlassian.net",
        "project_key": "PROJ",
        "email": "me@x.com",
        "api_token": "tok-123",
    }})
    assert r.status_code == 200, r.text
    assert recorded.count(("feature_used", {"name": "integration_saved"})) == 1


@pytest.mark.asyncio
async def test_unknown_integration_save_emits_nothing(local_client, recorded):
    r = await local_client.put(
        "/api/integrations/mystery/config", json={"fields": {"x": "y"}})
    assert r.status_code == 404
    assert [e for e in recorded if e[0] == "feature_used"] == []


# --------------------------------------------------------------------------- #
# 5. Payload shape + no dynamic strings                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_payload_is_exactly_the_name_prop(
    client, local_client, store, tmp_path, monkeypatch, recorded,
):
    await client.post("/api/tasks", json={
        "title": "Fix it", "source": "jira", "external_id": "NO-2",
    })
    monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", tmp_path / "nh_home")
    t = await _seed_task(store, title="another bug")
    await client.post(f"/api/tasks/{t.id}/attachments",
                      files={"file": ("a.png", b"x", "image/png")})
    await local_client.put("/api/integrations/jira/config", json={"fields": {
        "site": "https://acme.atlassian.net",
        "project_key": "PROJ",
        "email": "me@x.com",
        "api_token": "tok-123",
    }})
    feature_events = [props for kind, props in recorded if kind == "feature_used"]
    assert len(feature_events) == 3
    for props in feature_events:
        assert set(props) == {"name"}
        assert props["name"] in app_module.FEATURE_NAMES


def test_call_sites_use_constants_not_literals():
    """Static guard on the NAME prop specifically (the closed-vocabulary
    value), not the fixed `"feature_used"` event kind (which, like
    `"approve_clicked"`, is legitimately a literal at every call site).

    Every `_record_feature_used(request, X)` call's `X`, and `record(...,
    name=X)`'s `name=` keyword, must be an `ast.Name` (a FEATURE_* constant
    reference) — never a string literal or an f-string, so no
    operator/request-derived value can ever be substituted in later."""
    tree = ast.parse(APP_PY_PATH.read_text())

    def _func_name(call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Attribute):
            return func.attr
        if isinstance(func, ast.Name):
            return func.id
        return None

    def _assert_is_name_ref(node: ast.AST, where: str) -> None:
        if isinstance(node, ast.Name):
            return
        if isinstance(node, ast.Constant):
            pytest.fail(f"literal {node.value!r} passed at {where}:{node.lineno}; "
                        "use a FEATURE_* constant")
        if isinstance(node, ast.JoinedStr):
            pytest.fail(f"f-string argument at {where}:{node.lineno}")
        pytest.fail(f"unexpected node type {type(node).__name__} at {where}:{node.lineno}")

    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = _func_name(node)
        if func_name == "_record_feature_used":
            # _record_feature_used(request, <name>)
            assert len(node.args) == 2, node.args
            _assert_is_name_ref(node.args[1], "_record_feature_used arg")
            checked += 1
        elif func_name == "record":
            for kw in node.keywords:
                if kw.arg == "name":
                    _assert_is_name_ref(kw.value, "record(name=...)")
                    checked += 1
    # 3 call sites (_record_feature_used) + 1 record(name=...) inside the
    # helper itself.
    assert checked == 4, checked


# --------------------------------------------------------------------------- #
# 6. Fail-open / consent-off inherited from telemetry.record                  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_default_config_queues_events_via_posthog(
        client, monkeypatch, tmp_path):
    """Real `record` (no monkeypatch): consent defaults ON, and — since a
    default install ships PostHog credentials — an unset `telemetry.endpoint`
    no longer means inert: a real API call now queues its event for
    PostHog's `/batch/` destination (creating a task via the API fires a
    `feature_used` event, not `task_created` — that one only fires once the
    orchestrator actually runs the task). Does not re-test `record`'s
    internals or the wire body — tests/test_telemetry.py owns those."""
    monkeypatch.setenv("HOME", str(tmp_path))
    r = await client.post("/api/tasks", json={
        "title": "Fix it", "source": "jira", "external_id": "NO-3",
    })
    assert r.status_code == 201, r.text
    queue = tmp_path / ".no_human" / "telemetry-queue.jsonl"
    assert queue.exists()
    events = [json.loads(ln) for ln in queue.read_text().splitlines() if ln.strip()]
    assert events


@pytest.mark.asyncio
async def test_consent_off_records_nothing_on_disk(client, monkeypatch, tmp_path):
    """Real `record` (no monkeypatch), `telemetry.enabled: false`: `record`
    no-ops regardless of destination — the one opt-out still works. Does not
    re-test `record`'s internals — tests/test_telemetry.py owns those."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app.state.config.data["telemetry"]["enabled"] = False
    r = await client.post("/api/tasks", json={
        "title": "Fix it", "source": "jira", "external_id": "NO-4",
    })
    assert r.status_code == 201, r.text
    assert not (tmp_path / ".no_human" / "telemetry-queue.jsonl").exists()
