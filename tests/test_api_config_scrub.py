"""GET /api/config must never mutate the running config, and must scrub
secret-shaped keys recursively at any nesting depth."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.api.app import app
from no_human.config import Config


@pytest_asyncio.fixture
async def client(store, tmp_path):
    app.state.store = store
    app.state.config = Config(
        data={
            "notify": {
                "slack_webhook_url": "https://hooks.slack.com/services/SECRET",
                "channel": "#builds",
            },
            "llm": {
                "primary_model": "claude-sonnet-5",
            },
            "integrations": {
                "jira": {
                    "auth": {
                        "api_token": "sekrit-nested-value",
                    },
                },
            },
        },
        path=tmp_path / "config.yaml",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


@pytest.mark.asyncio
async def test_show_config_does_not_mutate_running_config(client):
    r1 = await client.get("/api/config")
    assert r1.status_code == 200
    # The in-memory config must still hold the original secret after the
    # handler ran once — the bug was a shallow dict-copy that let the scrub
    # mutate the shared nested dict.
    assert (
        app.state.config.data["notify"]["slack_webhook_url"]
        == "https://hooks.slack.com/services/SECRET"
    )
    # Calling it again must give the same scrubbed result, proving the
    # source config was never damaged by the first call.
    r2 = await client.get("/api/config")
    assert r2.json() == r1.json()
    assert app.state.config.data["notify"]["slack_webhook_url"] == (
        "https://hooks.slack.com/services/SECRET"
    )


@pytest.mark.asyncio
async def test_show_config_scrubs_top_level_secret(client):
    r = await client.get("/api/config")
    data = r.json()
    assert data["notify"]["slack_webhook_url"] == "●●● set"
    assert data["notify"]["channel"] == "#builds"


@pytest.mark.asyncio
async def test_show_config_scrubs_nested_secret_recursively(client):
    r = await client.get("/api/config")
    data = r.json()
    nested = data["integrations"]["jira"]["auth"]["api_token"]
    assert nested == "●●● set"
    # the raw value must never appear anywhere in the response
    assert "sekrit-nested-value" not in r.text


@pytest.mark.asyncio
async def test_show_config_passes_through_empty_secret_values(client):
    store_config = app.state.config
    store_config.data["notify"]["password"] = ""
    store_config.data["notify"]["token"] = None
    r = await client.get("/api/config")
    data = r.json()
    assert data["notify"]["password"] == ""
    assert data["notify"]["token"] is None


@pytest.mark.asyncio
async def test_show_config_exposes_coder_backends_from_the_one_source_of_truth(client):
    """The board's task composer must build its coder-backend picker from
    this field, not a hardcoded JS array — pin that the field exists and is
    exactly `agent.backend.SUPPORTED_BACKENDS`, so a backend added there
    shows up here (and therefore in the UI) automatically."""
    from no_human.agent.backend import SUPPORTED_BACKENDS

    r = await client.get("/api/config")
    data = r.json()
    assert data["coder_backends"] == list(SUPPORTED_BACKENDS)


@pytest.mark.asyncio
async def test_show_config_exposes_claude_pinned_roles_from_the_one_source_of_truth(client):
    """The composer's "coder only" disclaimer must be able to name the
    pinned roles from the same tuple `make_backend` enforces, never a second
    literal that could drift from it."""
    from no_human.agent.backend import CLAUDE_PINNED_ROLES

    r = await client.get("/api/config")
    data = r.json()
    assert data["claude_pinned_roles"] == list(CLAUDE_PINNED_ROLES)


@pytest.mark.asyncio
async def test_show_config_exposes_coder_backend_availability_shape(client):
    """`coder_backend_availability` must carry one `{id, available, reason}`
    entry per `coder_backends` entry, in the same order — the task
    composer's disabled/title logic indexes it by `id`, so a missing entry
    or a shape drift would silently make an option un-greyable."""
    from no_human.agent.backend import SUPPORTED_BACKENDS

    r = await client.get("/api/config")
    data = r.json()
    availability = data["coder_backend_availability"]
    assert [row["id"] for row in availability] == list(SUPPORTED_BACKENDS)
    for row in availability:
        assert set(row) == {"id", "available", "reason"}
        assert isinstance(row["available"], bool)
        assert isinstance(row["reason"], str)


@pytest.mark.asyncio
async def test_show_config_exposes_the_effective_and_default_coder_backends(client):
    """The composer must gate its disclosure caption on the EFFECTIVE coder
    backend (worker.backend, resolved), not just the picker — otherwise an
    install with `worker.backend: codex` configured and the picker left on
    default shows no disclosure at all. Pin both fields against their real
    sources of truth, never a literal, and prove `coder_backend_default`
    stays pristine (the DEFAULT_CONFIG value) even when the running config's
    effective backend has moved off it."""
    from no_human.agent.backend import resolve_backend_name
    from no_human.config import DEFAULT_CONFIG

    default_backend = DEFAULT_CONFIG["worker"]["backend"]

    r = await client.get("/api/config")
    data = r.json()
    assert data["coder_backend_effective"] == resolve_backend_name(app.state.config.data)
    assert data["coder_backend_effective"] == default_backend
    assert data["coder_backend_default"] == default_backend

    app.state.config.data.setdefault("worker", {})["backend"] = "codex"
    r2 = await client.get("/api/config")
    data2 = r2.json()
    assert data2["coder_backend_effective"] == "codex"
    assert data2["coder_backend_effective"] == resolve_backend_name(app.state.config.data)
    # the DEFAULT must not move just because this install configured something.
    assert data2["coder_backend_default"] == default_backend
    assert data2["coder_backend_default"] != data2["coder_backend_effective"]


@pytest.mark.asyncio
async def test_show_config_reports_local_unavailable_without_local_base_url(client):
    """Acceptance criterion: 'with llm.local_base_url unset, selecting
    local is refused with the reason the existing config check gives.' This
    fixture's config carries no `llm.local_base_url` key at all, so the
    reason must be the verbatim `assert_local_backend_mode` refusal, not a
    frontend-invented message."""
    r = await client.get("/api/config")
    data = r.json()
    local = next(row for row in data["coder_backend_availability"] if row["id"] == "local")
    assert local["available"] is False
    assert "llm.local_base_url is not set" in local["reason"]


@pytest.mark.asyncio
async def test_show_config_reports_local_available_once_base_url_is_set(client):
    """The other half of the same criterion: with `llm.local_base_url` set
    to a valid loopback URL, the same selection is accepted — same config
    object, only the one key changed, proving the answer tracks the actual
    config check rather than being hardcoded to 'local' = unavailable."""
    app.state.config.data.setdefault("llm", {})["local_base_url"] = "http://localhost:8000"
    r = await client.get("/api/config")
    data = r.json()
    local = next(row for row in data["coder_backend_availability"] if row["id"] == "local")
    assert local["available"] is True
    assert local["reason"] == ""
