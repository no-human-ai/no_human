"""Onboarding-funnel telemetry: the 6 new closed-vocabulary events that fill
in the blind spot between `app_started` (291 installs) and `task_created` (6
installs) — see docs/TELEMETRY.md. Pins: (1) each call site emits exactly the
event/props the plan calls for, (2) a refused attempt (`repo_invalid`,
`task_create_failed`) reads differently in the data from silence, (3) the
live auth probe reports a closed `result` (valid/rejected/inconclusive/
absent/cli_missing) rather than presence-only, and (4) a telemetry failure
never changes the HTTP outcome of the request that triggered it (fail-open,
same discipline as tests/test_feature_used_telemetry.py).
"""
from __future__ import annotations

import types

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human import telemetry
from no_human.api.app import app
from no_human.config import Config

# `_require_credentials`/`_auth_status_payload` read the OPERATOR'S real
# ~/.no_human/.env before the process env — see tests/conftest.py's
# `isolated_env_file` and tests/test_setup_mode_boot.py, which this module's
# fixtures mirror.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


def _no_ambient_token(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def recorded(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))
    return sent


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    """Subscription mode, NO credential on file, setup mode ON — mirrors
    tests/test_setup_mode_boot.py's `client`. Carries the local Origin header
    the writing=True funnel routes (`/api/onboarding/step-viewed`,
    `/api/auth/verify`) require."""
    _no_ambient_token(monkeypatch)
    app.state.store = store
    app.state.config = Config(
        data={"llm": {"auth_mode": "subscription"}}, path=tmp_path / "config.yaml",
    )
    app.state.setup_mode = True
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost",
                               headers={"Origin": "http://127.0.0.1:8420"}) as c:
            yield c
    finally:
        del app.state.setup_mode


@pytest_asyncio.fixture
async def client_with_credential(store, tmp_path, monkeypatch, isolated_env_file):
    """Same board, but a subscription token IS on file — past
    `_require_credentials`, so the repo_path/backend/etc. checks inside
    `create_task` are reachable, and `/api/auth/verify` sees a present
    credential."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    isolated_env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=has-a-token\n")
    isolated_env_file.chmod(0o600)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    app.state.store = store
    app.state.config = Config(
        data={"llm": {"auth_mode": "subscription"}}, path=tmp_path / "config.yaml",
    )
    app.state.setup_mode = False
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost",
                               headers={"Origin": "http://127.0.0.1:8420"}) as c:
            yield c
    finally:
        del app.state.setup_mode


# --------------------------------------------------------------------------- #
# 1. onboarding_step_viewed                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_step_viewed_emits_the_step(client, recorded):
    r = await client.post("/api/onboarding/step-viewed", json={"step": "repos"})
    assert r.status_code == 200, r.text
    assert recorded.count(("onboarding_step_viewed", {"step": "repos"})) == 1


@pytest.mark.asyncio
async def test_step_viewed_refuses_an_unknown_step_and_emits_nothing(client, recorded):
    r = await client.post("/api/onboarding/step-viewed", json={"step": "done"})
    assert r.status_code == 422
    assert [e for e in recorded if e[0] == "onboarding_step_viewed"] == []


# --------------------------------------------------------------------------- #
# 2. repo_selected / repo_invalid via /api/onboarding/repos/onboard           #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_onboard_repo_emits_repo_selected_on_success(client, recorded, tmp_path):
    repo = tmp_path / "svc"
    (repo / ".git").mkdir(parents=True)
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    assert recorded.count(("repo_selected", {})) == 1
    assert [e for e in recorded if e[0] == "repo_invalid"] == []


@pytest.mark.asyncio
async def test_onboard_repo_emits_repo_invalid_for_a_non_repo_directory(
    client, recorded, tmp_path,
):
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    r = await client.post(
        "/api/onboarding/repos/onboard", json={"repo_path": str(not_a_repo)})
    assert r.status_code == 422, r.text
    assert recorded.count(("repo_invalid", {"reason": "not_a_git_repo"})) == 1
    assert [e for e in recorded if e[0] == "repo_selected"] == []


@pytest.mark.asyncio
async def test_onboard_repo_emits_repo_invalid_for_a_missing_path(
    client, recorded, tmp_path,
):
    missing = tmp_path / "does" / "not" / "exist"
    r = await client.post(
        "/api/onboarding/repos/onboard", json={"repo_path": str(missing)})
    assert r.status_code == 422, r.text
    assert recorded.count(("repo_invalid", {"reason": "missing"})) == 1


# --------------------------------------------------------------------------- #
# 3. task_create_failed via /api/tasks                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_create_task_without_credentials_emits_task_create_failed(
    client, recorded,
):
    r = await client.post("/api/tasks", json={"title": "Plain task"})
    assert r.status_code == 503, r.text
    assert recorded.count(("task_create_failed", {"reason": "no_credentials"})) == 1


@pytest.mark.asyncio
async def test_create_task_with_a_non_repo_path_emits_repo_invalid_and_task_create_failed(
    client_with_credential, recorded, tmp_path,
):
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    r = await client_with_credential.post(
        "/api/tasks", json={"title": "Plain task", "repo_path": str(not_a_repo)})
    assert r.status_code == 422, r.text
    assert recorded.count(("repo_invalid", {"reason": "not_a_git_repo"})) == 1
    assert recorded.count(("task_create_failed", {"reason": "repo_invalid"})) == 1


# --------------------------------------------------------------------------- #
# 4. /api/auth/verify — whether a credential actually WORKS                  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_auth_verify_reports_valid_rejected_and_inconclusive_separately(
    client_with_credential, recorded, monkeypatch,
):
    monkeypatch.setattr(
        "no_human.agent.backend_check.find_claude_cli", lambda: "/usr/bin/claude")

    async def _ok(**kw):
        return None

    monkeypatch.setattr("no_human.agent.backend_check.verify_credential_live", _ok)
    r = await client_with_credential.post("/api/auth/verify", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "valid"}
    assert recorded.count(("auth_check_succeeded", {})) == 1

    async def _rejected(**kw):
        return ("rejected", "401 from provider")

    monkeypatch.setattr(
        "no_human.agent.backend_check.verify_credential_live", _rejected)
    r = await client_with_credential.post("/api/auth/verify", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "rejected"}
    assert "401" not in r.text  # constraint §8: the free-text reason never ships
    assert recorded.count(("auth_check_failed", {"reason": "rejected"})) == 1

    async def _inconclusive(**kw):
        return ("inconclusive", "timed out")

    monkeypatch.setattr(
        "no_human.agent.backend_check.verify_credential_live", _inconclusive)
    r = await client_with_credential.post("/api/auth/verify", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "inconclusive"}
    assert recorded.count(("auth_check_failed", {"reason": "inconclusive"})) == 1


@pytest.mark.asyncio
async def test_auth_verify_short_circuits_with_absent_credential(client, recorded):
    """`client` has no credential on file — the live probe must never even
    be attempted (no quota spent, no network hop) when there is plainly
    nothing to verify."""
    r = await client.post("/api/auth/verify", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "absent"}
    assert recorded.count(("auth_check_failed", {"reason": "absent"})) == 1
    assert [e for e in recorded if e[0] == "auth_check_succeeded"] == []


# --------------------------------------------------------------------------- #
# 5. Fail-open: a telemetry error never changes the HTTP outcome             #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_telemetry_failure_never_changes_the_http_outcome(client, monkeypatch):
    def _boom(kind, config=None, **props):
        raise RuntimeError("telemetry backend is down")

    monkeypatch.setattr(telemetry, "record", _boom)
    r = await client.post("/api/onboarding/step-viewed", json={"step": "welcome"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
