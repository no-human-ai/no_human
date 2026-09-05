"""[re-home] Board rejects an unrunnable LOCAL-backend config before submit.

Re-home of private PR #747 against current public main. That branch patched
`web/src/TaskComposer.jsx`'s `localBackendUnconfigured` — a JS-side rule that
no longer exists on this tree (grep confirms zero `local_model`/
`local_base_url` hits in TaskComposer.jsx). The composer now gates submit
purely on the server's `coder_backend_availability`
(`core.backend_settings.describe_backend` -> `core.runtime.
assert_task_backend_usable`), so the ONE hole left to close is server-side:
that preflight only validated `llm.local_base_url` and let a `local` config
missing `llm.local_model` through as "available" — the exact config
`agent.backend.make_backend`'s own `local` branch refuses at construction
(BackendUnavailable naming `llm.local_model`). This file proves the runtime
preflight now agrees with `make_backend` BEFORE the first coder turn, that
`GET /api/config` (what greys out the board's picker) carries it, and that
`POST /api/tasks` refuses to create a task doomed to die on attempt 1 even
from a client that is not the board.

RED-first: `test_availability_marks_local_unusable_when_only_the_model_is_
missing` fails on the pre-fix tree (`describe_backend` returns
`available: True`) and passes after `core/runtime.py`'s `assert_task_
backend_usable` is fixed.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.agent.backend import BackendUnavailable
from no_human.api.app import app
from no_human.config import Config
from no_human.core.backend_settings import describe_backend
from no_human.core.runtime import build_orchestrator

pytestmark = pytest.mark.usefixtures("isolated_env_file")


class _Task:
    def __init__(self, config):
        self.config = config


def _cfg_data(*, local_base_url=None, local_model=None):
    llm = {}
    if local_base_url is not None:
        llm["local_base_url"] = local_base_url
    if local_model is not None:
        llm["local_model"] = local_model
    return {"llm": llm} if llm else {}


@pytest_asyncio.fixture
async def client(store, tmp_path):
    app.state.store = store
    app.state.config = Config(
        data=_cfg_data(local_base_url="http://localhost:8000"),
        path=tmp_path / "config.yaml",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


# --------------------------------------------------------------------------- #
# describe_backend — the RED-first case.                                      #
# --------------------------------------------------------------------------- #


def test_availability_marks_local_unusable_when_only_the_model_is_missing():
    """RED on the pre-fix tree: `describe_backend` returned `available: True`
    for a `local` config with a base_url but no `local_model`, because
    `assert_task_backend_usable`'s `local` branch never checked the model
    key — the exact hole `make_backend`'s own `local` branch already closes
    at construction time (too late: after planning tokens were spent)."""
    cfg = _cfg_data(local_base_url="http://localhost:8000")
    info = describe_backend("local", cfg)
    assert info["available"] is False
    assert "local_model" in info["reason"]


def test_the_reason_names_only_the_key_that_is_actually_missing():
    # base_url missing, model set -> names local_base_url, not local_model.
    info = describe_backend("local", _cfg_data(local_model="my-model"))
    assert info["available"] is False
    assert "local_base_url" in info["reason"]
    assert "local_model" not in info["reason"]

    # base_url set, model missing -> the inverse.
    info = describe_backend(
        "local", _cfg_data(local_base_url="http://localhost:8000"))
    assert info["available"] is False
    assert "local_model" in info["reason"]
    assert "local_base_url" not in info["reason"]

    # both missing -> refused (on the base_url check, which runs first).
    info = describe_backend("local", _cfg_data())
    assert info["available"] is False

    # both set -> available.
    info = describe_backend(
        "local",
        _cfg_data(local_base_url="http://localhost:8000", local_model="my-model"),
    )
    assert info["available"] is True
    assert info["reason"] == ""


# --------------------------------------------------------------------------- #
# GET /api/config — what greys out the board's picker.                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_api_config_reports_local_unavailable_and_names_the_key(client):
    r = await client.get("/api/config")
    assert r.status_code == 200
    options = {o["id"]: o for o in r.json()["coder_backend_availability"]}
    local = options["local"]
    assert local["available"] is False
    assert "llm.local_model" in local["reason"]


# --------------------------------------------------------------------------- #
# POST /api/tasks — the server is not merely trusting the UI.                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_post_api_tasks_refuses_a_local_task_with_no_local_model(client, store):
    r = await client.post(
        "/api/tasks", json={"title": "Local task", "backend": "local"})
    assert r.status_code == 422, r.text
    assert "local_model" in r.json()["detail"]

    listed = await client.get("/api/tasks")
    assert listed.json() == []


@pytest.mark.asyncio
async def test_post_api_tasks_still_creates_a_local_task_when_both_keys_are_set(
        store, tmp_path):
    app.state.store = store
    app.state.config = Config(
        data=_cfg_data(
            local_base_url="http://localhost:8000", local_model="my-model"),
        path=tmp_path / "config.yaml",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        r = await c.post(
            "/api/tasks", json={"title": "Local task", "backend": "local"})
        assert r.status_code == 201, r.text
        task = await store.get_task(r.json()["id"])
        assert task.config["backend"] == "local"


# --------------------------------------------------------------------------- #
# The runtime remains the last line of defence; make_backend is untouched.    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_build_orchestrator_still_refuses_and_make_backend_is_untouched(
        tmp_path):
    from no_human.config import load_config
    from no_human.core.db import Store

    (tmp_path / "config.yaml").write_text(
        "llm:\n  local_base_url: 'http://localhost:8000'\n")
    cfg = load_config(tmp_path / "config.yaml")
    store = await Store(tmp_path / "t.db").connect()
    try:
        with pytest.raises(BackendUnavailable) as exc:
            build_orchestrator(cfg, store, task=_Task({"backend": "local"}))
        assert "llm.local_model" in str(exc.value)
    finally:
        await store.close()
