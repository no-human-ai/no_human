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


async def _make_client_with_credential(store, tmp_path, monkeypatch, isolated_env_file,
                                       *, telemetry_enabled: bool):
    """Shared builder for `client_with_credential`/`_telemetry_disabled`
    below — same board, but a subscription token IS on file (past
    `_require_credentials`, so the repo_path/backend/etc. checks inside
    `create_task` are reachable, and `/api/auth/verify` sees a present
    credential), parameterized ONLY on whether `telemetry.enabled` resolves
    (config flag + a destination) — the exact gate `/api/auth/verify` must
    consult before spending the credential's quota (Blocker 1)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    isolated_env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=has-a-token\n")
    isolated_env_file.chmod(0o600)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    app.state.store = store
    telemetry_section = (
        {"enabled": True, "endpoint": "https://telemetry.example.test/ingest"}
        if telemetry_enabled else {"enabled": False}
    )
    app.state.config = Config(
        data={
            "llm": {"auth_mode": "subscription"},
            "telemetry": telemetry_section,
        },
        path=tmp_path / "config.yaml",
    )
    app.state.setup_mode = False
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost",
                               headers={"Origin": "http://127.0.0.1:8420"}) as c:
            yield c
    finally:
        del app.state.setup_mode


@pytest_asyncio.fixture
async def client_with_credential(store, tmp_path, monkeypatch, isolated_env_file):
    """`telemetry.enabled` resolves True (config flag + a destination) — the
    positive control for the gate `/api/auth/verify` must consult before
    spending the credential's quota (Blocker 1): the live probe DOES fire."""
    async for c in _make_client_with_credential(
        store, tmp_path, monkeypatch, isolated_env_file, telemetry_enabled=True,
    ):
        yield c


@pytest_asyncio.fixture
async def client_with_credential_telemetry_disabled(
    store, tmp_path, monkeypatch, isolated_env_file,
):
    """Same board as `client_with_credential`, but `telemetry.enabled:
    false` — the live probe's ONLY consumer. `/api/auth/verify` must never
    invoke it here (Blocker 1)."""
    async for c in _make_client_with_credential(
        store, tmp_path, monkeypatch, isolated_env_file, telemetry_enabled=False,
    ):
        yield c


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
    # Bucketed, never the exact count (this install's first repo -> "1"):
    # an unbucketed per-repo event would let an exact repo count be derived
    # from event cardinality alone — the same shape ORPHAN_COUNT_BUCKETS
    # exists to prevent for tasks_orphaned.
    assert recorded.count(("repo_selected", {"count_bucket": "1"})) == 1
    assert [e for e in recorded if e[0] == "repo_invalid"] == []


@pytest.mark.asyncio
async def test_onboard_repo_buckets_the_count_not_the_exact_number(
    client, recorded, tmp_path,
):
    """A second onboarded repo still reads "2-5", not "2" — the prop is a
    bucket, not a counter, so it never lets an install's exact fleet size
    be derived from repeated `repo_selected` events."""
    first = tmp_path / "svc-a"
    (first / ".git").mkdir(parents=True)
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(first)})
    assert r.status_code == 200, r.text

    second = tmp_path / "svc-b"
    (second / ".git").mkdir(parents=True)
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(second)})
    assert r.status_code == 200, r.text

    buckets = [p["count_bucket"] for k, p in recorded if k == "repo_selected"]
    assert buckets == ["1", "2-5"]


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


@pytest.mark.asyncio
async def test_create_task_with_missing_project_emits_task_create_failed(
    client_with_credential, recorded,
):
    r = await client_with_credential.post(
        "/api/tasks", json={"title": "Plain task", "project_id": "no-such-project"})
    assert r.status_code == 404, r.text
    assert recorded.count(("task_create_failed", {"reason": "project_missing"})) == 1


@pytest.mark.asyncio
async def test_create_task_with_missing_follows_id_emits_task_create_failed_other(
    client_with_credential, recorded,
):
    r = await client_with_credential.post(
        "/api/tasks", json={"title": "Plain task", "follows_id": "no-such-task"})
    assert r.status_code == 404, r.text
    assert recorded.count(("task_create_failed", {"reason": "other"})) == 1


@pytest.mark.asyncio
async def test_create_task_with_invalid_priority_emits_task_create_failed_validation(
    client_with_credential, recorded,
):
    r = await client_with_credential.post(
        "/api/tasks", json={"title": "Plain task", "priority": "not-a-priority"})
    assert r.status_code == 422, r.text
    assert recorded.count(("task_create_failed", {"reason": "validation"})) == 1


@pytest.mark.asyncio
async def test_create_task_with_unknown_backend_emits_task_create_failed_validation(
    client_with_credential, recorded,
):
    r = await client_with_credential.post(
        "/api/tasks", json={"title": "Plain task", "backend": "not-a-backend"})
    assert r.status_code == 422, r.text
    assert recorded.count(("task_create_failed", {"reason": "validation"})) == 1


@pytest.mark.asyncio
async def test_create_task_with_unavailable_backend_emits_task_create_failed_backend_unavailable(
    client_with_credential, recorded,
):
    # `client_with_credential`'s config carries no `llm.local_model`, so the
    # KNOWN "local" backend is a real, non-typo unavailability — the same
    # `assert_task_backend_usable` preflight the orchestrator itself runs.
    r = await client_with_credential.post(
        "/api/tasks", json={"title": "Plain task", "backend": "local"})
    assert r.status_code == 422, r.text
    assert recorded.count(("task_create_failed", {"reason": "backend_unavailable"})) == 1


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


@pytest.mark.asyncio
async def test_auth_verify_reports_cli_missing_when_credential_present_but_no_cli(
    client_with_credential, recorded, monkeypatch,
):
    """Kills a surviving mutant: flipping `if not status["backend_cli_
    present"]:` to `if False:` left every prior test in this module green,
    because nothing pinned this branch. A present credential with no
    backend CLI installed must report `cli_missing` and never reach the
    live probe."""
    monkeypatch.setattr("no_human.agent.backend_check.find_claude_cli", lambda: None)

    called = []

    async def _should_not_be_called(**kw):
        called.append(kw)
        return None

    monkeypatch.setattr(
        "no_human.agent.backend_check.verify_credential_live", _should_not_be_called)

    r = await client_with_credential.post("/api/auth/verify", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "cli_missing"}
    assert recorded.count(("auth_check_failed", {"reason": "cli_missing"})) == 1
    assert called == []


# --------------------------------------------------------------------------- #
# 4b. /api/auth/verify — reviewer BLOCKERS: gated on telemetry.enabled, and  #
#     never leaks a mutation into the running server's billing identity     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_auth_verify_never_invokes_the_live_probe_when_telemetry_is_disabled(
    client_with_credential_telemetry_disabled, recorded, monkeypatch,
):
    """Blocker 1: the live probe's ONLY consumer is the `auth_check_
    succeeded`/`auth_check_failed` telemetry it feeds — with `telemetry.
    enabled: false` there is nothing for the spend to produce, so the
    provider call must never happen at all (not merely go unrecorded). The
    call counter, not just the HTTP result, is the pin: a result cached or
    computed some other way could still read "skipped" while quota was
    spent."""
    monkeypatch.setattr(
        "no_human.agent.backend_check.find_claude_cli", lambda: "/usr/bin/claude")
    calls = []

    async def _counted(**kw):
        calls.append(kw)
        return None

    monkeypatch.setattr("no_human.agent.backend_check.verify_credential_live", _counted)

    r = await client_with_credential_telemetry_disabled.post("/api/auth/verify", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "skipped"}
    assert len(calls) == 0
    assert [e for e in recorded
            if e[0] in ("auth_check_succeeded", "auth_check_failed")] == []


@pytest.mark.asyncio
async def test_auth_verify_invokes_the_live_probe_when_telemetry_is_enabled(
    client_with_credential, recorded, monkeypatch,
):
    """Positive control for the test above: with `telemetry.enabled` true
    AND a destination configured (`client_with_credential`'s board), the
    identical request DOES invoke the live probe exactly once — proving the
    disabled case above is the gate actually working, not the probe being
    broken some other way."""
    monkeypatch.setattr(
        "no_human.agent.backend_check.find_claude_cli", lambda: "/usr/bin/claude")
    calls = []

    async def _counted(**kw):
        calls.append(kw)
        return None

    monkeypatch.setattr("no_human.agent.backend_check.verify_credential_live", _counted)

    r = await client_with_credential.post("/api/auth/verify", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "valid"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_auth_verify_probes_the_running_profile_and_restores_env_after(
    client_with_credential, recorded, monkeypatch, isolated_env_file,
):
    """Blocker 2: the endpoint runs inside the SAME long-lived process as
    the embedded worker (`board up = worker up`), which is what every
    later task attempt's billing stamp reads via `config.
    active_auth_profile()`. Pins that (a) the probe is asked about the
    profile the RUNNING process actually exported, not whatever config.
    yaml currently says, and (b) `os.environ[CLAUDE_CODE_OAUTH_TOKEN]` and
    `config.active_auth_profile()` are byte-identical before and after the
    request even though `verify_credential_live` mutates both as a side
    effect (the same side effect the real function documents) and config
    names a DIFFERENT profile than the one exported."""
    import os as _os

    from no_human import config as config_module

    monkeypatch.setattr(
        "no_human.agent.backend_check.find_claude_cli", lambda: "/usr/bin/claude")

    # Config-on-disk names "work"; the process actually exported "personal".
    # `/api/auth/status`'s `token_present` is keyed off the config-on-disk
    # profile (`available_auth_profiles()` scanning for a `..._WORK` token),
    # independent of which profile the running process exported — so "work"
    # needs its own token on file or the request never gets past the
    # presence check to reach the code path this test exercises.
    app.state.config.data["llm"]["auth_profile"] = "work"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_WORK", "work-token-on-disk")
    monkeypatch.setattr(config_module, "_ACTIVE_AUTH_PROFILE", "personal")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "running-process-token")

    seen_profiles = []

    async def _mutating_probe(*, model, profile, auth_mode, **kw):
        seen_profiles.append(profile)
        # Mirrors verify_credential_live's REAL side effect (assert_
        # subscription_mode -> load_env_token): exports a token and
        # reassigns the active-profile global.
        _os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = f"probed-token-for-{profile}"
        config_module._ACTIVE_AUTH_PROFILE = profile
        return None

    monkeypatch.setattr(
        "no_human.agent.backend_check.verify_credential_live", _mutating_probe)

    r = await client_with_credential.post("/api/auth/verify", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"result": "valid"}

    # The probe was asked about the RUNNING process's profile, not the one
    # named in config.yaml.
    assert seen_profiles == ["personal"]
    # And the server's own billing identity is back exactly where it
    # started — restored, not left pointed at whatever the probe checked.
    assert _os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "running-process-token"
    assert config_module.active_auth_profile() == "personal"


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
