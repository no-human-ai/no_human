"""GET /api/coder-backend + PUT /api/config/coder-backend, over real HTTP.

AC6's literal "real-user walk" — open Settings on a running board, change
the coder backend, submit a task, observe the attempt's `models` event name
it — cannot be performed in THIS environment: launching a live `nh`
server/process is blocked in agent sessions regardless of `HOME`/`--repo`
overrides (attempted and refused — see the session record). This module is
the closest substitute available here: the actual FastAPI ROUTES a running
board's browser would call, exercised in-process over real HTTP (`httpx`
`ASGITransport`, no live process, no port bound, no operator `~/.no_human`
touched — `CONFIG_PATH`/`ENV_PATH` are monkeypatched under `tmp_path`,
mirroring `tests/test_api_models.py`'s `client` fixture).

`tests/test_coder_backend_settings.py` already proves the seam by calling
`core.backend_settings.apply_backend_change`/`core.runtime.build_orchestrator`
directly — the exact functions the routes below call. This module adds the
one layer that leaves unproven otherwise: that the ROUTES themselves (origin
guard, body parsing, status codes, `CONFIG_PATH` wiring) reach those same
functions with nothing lost or added in translation.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import no_human.config as nh_config
from no_human.agent.backend import CLAUDE_PINNED_ROLES, resolve_backend_name
from no_human.api.app import app

pytestmark = pytest.mark.usefixtures("isolated_env_file")


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    # HARD GUARD: every config read/write in these tests must land under
    # tmp_path, never the operator's real ~/.no_human/config.yaml — same
    # guard tests/test_api_models.py's `client` fixture uses, for the same
    # reason (api_get/set_coder_backend both import CONFIG_PATH fresh from
    # `no_human.config` at call time, not from `request.app.state.config`).
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
async def test_get_coder_backend_lists_every_supported_backend_with_availability(client):
    r = await client.get("/api/coder-backend")
    assert r.status_code == 200
    data = r.json()
    assert data["current"] == "claude"
    assert data["default"] == "claude"
    ids = [o["id"] for o in data["options"]]
    assert ids == ["claude", "codex", "local"]
    # No llm.local_base_url has been written yet: "local" must be greyed out
    # right here, at GET time — never deferred to a submit that then fails.
    local = next(o for o in data["options"] if o["id"] == "local")
    assert local["available"] is False
    assert "llm.local_base_url" in local["reason"]


@pytest.mark.asyncio
async def test_get_exposes_local_fields_for_the_settings_row_to_prefill(client):
    r = await client.get("/api/coder-backend")
    assert r.status_code == 200
    lf = r.json()["local_fields"]
    assert lf["backend"] == "local"
    by_key = {f["key"]: f for f in lf["fields"]}
    assert set(by_key) == {"local_model", "local_base_url"}
    # No values on disk yet -> blank, so the inputs render empty.
    assert by_key["local_model"]["value"] == ""
    assert by_key["local_base_url"]["value"] == ""
    # Each field carries a human label the row shows without inventing one.
    assert by_key["local_base_url"]["label"]


@pytest.mark.asyncio
async def test_put_backend_and_local_fields_together_bootstraps_local_over_http(client):
    r = await client.put(
        "/api/config/coder-backend",
        json={
            "backend": "local",
            "local_model": "my-local-model",
            "local_base_url": "http://127.0.0.1:1234/v1",
        },
    )
    assert r.status_code == 200
    reloaded = nh_config.load_config(nh_config.CONFIG_PATH)
    assert reloaded.data["worker"]["backend"] == "local"
    assert reloaded.data["llm"]["local_model"] == "my-local-model"
    assert reloaded.data["llm"]["local_base_url"] == "http://127.0.0.1:1234/v1"
    # The returned payload prefills the fields with the just-written values.
    by_key = {f["key"]: f["value"] for f in r.json()["local_fields"]["fields"]}
    assert by_key["local_model"] == "my-local-model"


@pytest.mark.asyncio
async def test_put_local_with_local_base_url_unset_is_refused_over_http(client):
    r = await client.put("/api/config/coder-backend", json={"backend": "local"})
    assert r.status_code == 422
    assert "llm.local_base_url" in r.json()["detail"]
    # Refused before any write: worker.backend is still the default.
    assert resolve_backend_name(nh_config.load_config(nh_config.CONFIG_PATH).data,
                                 role="coder") == "claude"


@pytest.mark.asyncio
async def test_put_local_with_local_base_url_set_writes_the_global_default_over_http(
    client, tmp_path,
):
    nh_config.CONFIG_PATH.write_text(
        "llm:\n"
        "  local_base_url: 'http://localhost:8000'\n"
        "  local_model: 'my-local-model'\n"
    )

    r = await client.put("/api/config/coder-backend", json={"backend": "local"})
    assert r.status_code == 200
    body = r.json()
    # The route returns the payload directly (see api_set_coder_backend's
    # `return payload` — no separate "changes" envelope over the wire); the
    # running process's view is untouched until a restart (same convention
    # as PUT /api/config/models) — but the write already reached disk, which
    # `restart_required` must now report.
    assert body["current"] == "claude"
    assert body["restart_required"] is True

    reloaded = nh_config.load_config(nh_config.CONFIG_PATH)
    assert reloaded.data["worker"]["backend"] == "local"
    assert resolve_backend_name(reloaded.data, role="coder") == "local"

    # AC4, at this same HTTP-produced config: every pinned role is still
    # Claude regardless of what the board just submitted for the coder role.
    for role in CLAUDE_PINNED_ROLES:
        assert resolve_backend_name(reloaded.data, role=role) == "claude", role
