"""Auth status + token endpoints.

These let Settings show which subscription pays and update its OAuth token.
Constraint #1 is OAuth-only, so a metered API key must be refused here rather
than quietly accepted and billed per request. Constraint §8 is that a secret is
never echoed, so no response may ever carry the token value.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.config import AuthError, profile_token_var, set_profile_token
from no_human.core.db import Store

TOKEN = "sk-ant-oat01-EXAMPLE-not-a-real-token"


@pytest_asyncio.fixture
async def client(store_factory, tmp_path, monkeypatch):
    from no_human.config import load_config
    store = await store_factory("test.db")
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    monkeypatch.setattr("no_human.config.ENV_PATH", tmp_path / ".env")
    # SAME HARD GUARD for config.yaml: the Codex-mode write endpoint and the
    # codex status block both splice/read the REAL ~/.no_human/config.yaml via
    # config.CONFIG_PATH. Redirect it to the tmp file app.state.config was
    # loaded from, or a test would edit the operator's live config.
    monkeypatch.setattr("no_human.config.CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # HARD GUARD. These tests write real-looking credentials. An earlier draft
    # of set_profile_token bound `env_path = ENV_PATH` as a DEFAULT ARGUMENT,
    # captured at import, so this redirect was silently ignored and the test
    # overwrote a live token in the operator's ~/.no_human/.env. Never let a
    # broken redirect fall through to the real file again.
    import no_human.config as _cfg
    assert _cfg.ENV_PATH == tmp_path / ".env"
    assert str(_cfg.ENV_PATH).startswith(str(tmp_path))
    transport = ASGITransport(app=app)
    # A real browser always sends Origin on these calls; the write route now
    # requires it, so the fixture mirrors the legitimate UI.
    async with AsyncClient(transport=transport, base_url="http://localhost",
                           headers={"Origin": "http://127.0.0.1:8420"}) as c:
        yield c


# ------------------------------ GET status -------------------------------- #

@pytest.mark.asyncio
async def test_status_reports_names_and_booleans_only(client):
    r = await client.get("/api/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["token_var"].startswith("CLAUDE_CODE_OAUTH_TOKEN")
    assert isinstance(body["token_present"], bool)
    assert isinstance(body["metered_key_present"], bool)


@pytest.mark.asyncio
async def test_status_never_carries_a_token_value(client, tmp_path):
    """The whole point of the endpoint being safe to render in Settings."""
    set_profile_token("default", TOKEN, env_path=tmp_path / ".env")
    r = await client.get("/api/auth/status")
    assert TOKEN not in r.text
    assert "oat01" not in r.text


@pytest.mark.asyncio
async def test_status_flags_a_stray_metered_key(client, monkeypatch):
    """A stray ANTHROPIC_API_KEY silently bills the metered API — a human must
    be able to see that in the UI, not only by reading their shell profile."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-whatever")
    r = await client.get("/api/auth/status")
    assert r.json()["metered_key_present"] is True


@pytest.mark.asyncio
async def test_status_does_not_scrub_the_environment(client, monkeypatch):
    """A GET must not mutate process state. The scrub belongs to run startup."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-whatever")
    await client.get("/api/auth/status")
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api03-whatever"


# ------------------------- auth_mode-aware fields --------------------------- #

@pytest.mark.asyncio
async def test_status_carries_auth_mode_and_api_key_present(client):
    """SCRUM-13: every response — any mode — carries the new contract keys."""
    r = await client.get("/api/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_mode"] == "subscription"
    assert isinstance(body["api_key_present"], bool)


@pytest.mark.asyncio
async def test_status_reports_api_key_present_without_leaking_it(client, tmp_path):
    """api_key_present detects a key sitting in .env, and the value never
    appears in the response body."""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-api03-x\n")
    r = await client.get("/api/auth/status")
    assert r.json()["api_key_present"] is True
    assert "sk-ant-api03-x" not in r.text
    assert "api03" not in r.text


@pytest.mark.asyncio
async def test_api_key_mode_no_restart_when_already_billing_key(
        client, monkeypatch):
    """The server is already running under api_key mode and already exported
    the key ($_ACTIVE_AUTH_PROFILE == "api_key") — a restart would not change
    what pays, so restart_required must be False."""
    app.state.config.data.setdefault("llm", {})["auth_mode"] = "api_key"
    monkeypatch.setattr("no_human.config._ACTIVE_AUTH_PROFILE", "api_key")
    r = await client.get("/api/auth/status")
    body = r.json()
    assert body["auth_mode"] == "api_key"
    assert body["restart_required"] is False


@pytest.mark.asyncio
async def test_api_key_mode_restart_required_when_billing_path_differs(
        client, monkeypatch):
    """Config now says api_key, but the RUNNING process is still exporting an
    OAuth profile (it started before the switch) — that IS a billing-path
    change a restart would make, so restart_required must be True."""
    app.state.config.data.setdefault("llm", {})["auth_mode"] = "api_key"
    monkeypatch.setattr("no_human.config._ACTIVE_AUTH_PROFILE", "default")
    r = await client.get("/api/auth/status")
    assert r.json()["restart_required"] is True


@pytest.mark.asyncio
async def test_api_key_mode_token_present_false_is_not_flagged(
        client, monkeypatch):
    """token_present keeps its OAuth-token meaning in api_key mode — no OAuth
    token on file is normal here, not an error, and must not 500 or flip the
    field's semantics."""
    app.state.config.data.setdefault("llm", {})["auth_mode"] = "api_key"
    monkeypatch.setattr("no_human.config._ACTIVE_AUTH_PROFILE", "api_key")
    # Defensive: token_present reflects whatever OAuth token is resolvable
    # for the configured profile, so a real CLAUDE_CODE_OAUTH_TOKEN in the
    # ambient environment (this is a Claude Code session) must not leak into
    # the isolated test .env and make the assertion below environment-
    # dependent.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    r = await client.get("/api/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["token_present"], bool)
    assert body["token_present"] is False


@pytest.mark.asyncio
async def test_subscription_payload_existing_fields_unchanged(client):
    """Pins that adding the new keys did not alter any existing field's
    value or semantics in subscription mode (the default, untouched config)."""
    r = await client.get("/api/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["restart_required"] is False
    assert body["token_var"].startswith("CLAUDE_CODE_OAUTH_TOKEN")
    assert body["configured_profile"] == "default"
    assert "active_profile" in body
    assert body["metered_key_present"] is False
    assert body["auth_mode"] == "subscription"


# ------------------------------ PUT token ---------------------------------- #

@pytest.mark.asyncio
async def test_put_token_writes_it_and_reports_presence(client, tmp_path):
    r = await client.put("/api/auth/token",
                         json={"profile": "personal", "token": TOKEN})
    assert r.status_code == 200, r.text
    env = (tmp_path / ".env").read_text()
    assert f"CLAUDE_CODE_OAUTH_TOKEN_PERSONAL={TOKEN}" in env
    assert "personal" in [p["name"] for p in r.json()["profiles"]]
    # The response must not echo what was just stored.
    assert TOKEN not in r.text


@pytest.mark.asyncio
async def test_put_token_file_is_owner_only(client, tmp_path):
    await client.put("/api/auth/token",
                     json={"profile": "personal", "token": TOKEN})
    assert oct((tmp_path / ".env").stat().st_mode)[-3:] == "600"


@pytest.mark.asyncio
async def test_put_refuses_a_metered_api_key(client, tmp_path):
    """Constraint #1: this endpoint stores OAuth tokens only. An API key pasted
    here is refused and pointed at llm.auth_mode: api_key instead."""
    r = await client.put("/api/auth/token",
                         json={"profile": "personal",
                               "token": "sk-ant-api03-real-looking-key"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "ANTHROPIC_API_KEY" in detail
    assert "not an OAuth token" in detail
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_put_refuses_a_newline_injected_token(client, tmp_path):
    """A newline would append a forged line to .env — an ANTHROPIC_API_KEY
    among them, which is the metered-billing escape hatch constraint #1 shuts."""
    r = await client.put("/api/auth/token", json={
        "profile": "personal",
        "token": f"{TOKEN}\nANTHROPIC_API_KEY=sk-ant-api03-forged"})
    assert r.status_code == 422
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_put_refuses_an_injected_profile_name(client, tmp_path):
    """The profile becomes an .env KEY; a newline there injects a line just as
    a newline in the value would."""
    r = await client.put("/api/auth/token", json={
        "profile": "x\nANTHROPIC_API_KEY=sk-ant-api03-forged",
        "token": TOKEN})
    assert r.status_code == 422
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_put_refuses_an_empty_token(client):
    r = await client.put("/api/auth/token",
                         json={"profile": "personal", "token": "   "})
    assert r.status_code == 422


# --------------------------- unit-level guards ------------------------------ #

def test_profile_token_var_rejects_a_name_that_would_inject():
    with pytest.raises(AuthError, match="invalid auth profile"):
        profile_token_var("bad\nANTHROPIC_API_KEY=x")


def test_profile_token_var_still_maps_the_normal_cases():
    assert profile_token_var("default") == "CLAUDE_CODE_OAUTH_TOKEN"
    assert profile_token_var("Personal") == "CLAUDE_CODE_OAUTH_TOKEN_PERSONAL"


def test_set_profile_token_preserves_other_env_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# a comment\nOTHER=keepme\n")
    set_profile_token("personal", TOKEN, env_path=env)
    body = env.read_text()
    assert "# a comment" in body and "OTHER=keepme" in body
    assert f"CLAUDE_CODE_OAUTH_TOKEN_PERSONAL={TOKEN}" in body


def test_set_profile_token_replaces_rather_than_appends(tmp_path):
    env = tmp_path / ".env"
    set_profile_token("personal", "old-token", env_path=env)
    set_profile_token("personal", TOKEN, env_path=env)
    body = env.read_text()
    assert body.count("CLAUDE_CODE_OAUTH_TOKEN_PERSONAL=") == 1
    assert "old-token" not in body


# ---------------------- line-injection, the real alphabet ------------------- #

@pytest.mark.parametrize("sep", [
    "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029",
])
@pytest.mark.asyncio
async def test_put_refuses_every_separator_splitlines_honours(client, tmp_path, sep):
    """Security review: the guard checked \\n and \\r, but the READER used
    str.splitlines(), which also breaks on eight more characters. One PUT with
    U+2028 replaced the operator's real token and planted a physical
    ANTHROPIC_API_KEY= line — the metered-billing escape constraint #1 exists
    to prevent. Writer and reader must agree on what a line is."""
    r = await client.put("/api/auth/token", json={
        "profile": "personal",
        "token": f"{TOKEN}{sep}ANTHROPIC_API_KEY=sk-ant-api03-forged"})
    assert r.status_code == 422, sep
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_a_forged_separator_cannot_survive_a_round_trip(client, tmp_path):
    """Belt and braces: even if something got written, the reader must not
    invent variables no writer wrote."""
    from no_human.config import _read_env_file
    env = tmp_path / ".env"
    env.write_text(
        f"CLAUDE_CODE_OAUTH_TOKEN=good\u2028ANTHROPIC_API_KEY=forged\n")
    assert "ANTHROPIC_API_KEY" not in _read_env_file(env)


@pytest.mark.asyncio
async def test_put_refuses_a_null_byte(client, tmp_path):
    """A NUL round-trips through .env and then makes os.environ[...] raise
    'embedded null byte' on EVERY subsequent start — an unauthenticated,
    persistent brick with an error naming nothing actionable."""
    r = await client.put("/api/auth/token",
                         json={"profile": "personal", "token": "x\x00y"})
    assert r.status_code == 422
    assert not (tmp_path / ".env").exists()


# --------------------- a token pasted into the profile box ------------------ #

@pytest.mark.asyncio
async def test_a_token_in_the_profile_field_is_refused_not_echoed(client, tmp_path):
    """The profile regex accepted `sk-ant-oat01-…` (lowercase, digits, hyphens),
    stored it as a NAME, and GET /api/auth/status echoed it back — in the
    endpoint that advertises 'names and booleans only'."""
    r = await client.put("/api/auth/token",
                         json={"profile": TOKEN.lower(), "token": "whatever"})
    assert r.status_code == 422
    assert TOKEN.lower() not in r.text          # never echoed in the error
    assert not (tmp_path / ".env").exists()
    status = await client.get("/api/auth/status")
    assert "oat01" not in status.text


@pytest.mark.asyncio
async def test_an_over_long_profile_is_refused(client):
    r = await client.put("/api/auth/token",
                         json={"profile": "a" * 33, "token": TOKEN})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_an_implausibly_long_token_is_refused(client, tmp_path):
    r = await client.put("/api/auth/token",
                         json={"profile": "personal", "token": "x" * 5000})
    assert r.status_code == 422
    assert not (tmp_path / ".env").exists()


# ------------------------------ drive-by CSRF ------------------------------- #

@pytest.mark.asyncio
async def test_a_cross_origin_write_is_refused(client, tmp_path):
    """The server is unauthenticated, so any page the operator visits could
    otherwise PUT the credential store."""
    r = await client.put("/api/auth/token",
                         json={"profile": "personal", "token": TOKEN},
                         headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("origin", [
    "http://localhost.evil.com",          # startswith("http://localhost") is True
    "http://localhost.attacker.io:8080",
    "http://127.0.0.1.evil.com",
    "http://localhosting.example.com",
    "null",                               # sandboxed iframe / file://
])
@pytest.mark.asyncio
async def test_a_lookalike_origin_is_refused(client, tmp_path, origin):
    """The first version compared with `startswith`, which TRUSTS
    `http://localhost.evil.com` — a domain an attacker simply registers. A
    working drive-by was demonstrated against it: preflight 200, PUT 200, token
    overwritten. The host must be parsed and matched exactly."""
    r = await client.put("/api/auth/token",
                         json={"profile": "personal", "token": TOKEN},
                         headers={"Origin": origin})
    assert r.status_code == 403, origin
    assert not (tmp_path / ".env").exists(), origin


@pytest.mark.asyncio
async def test_a_write_with_no_origin_is_refused(store_factory, tmp_path, monkeypatch):
    """A browser always sends Origin cross-site, so refusing its ABSENCE costs
    the real UI nothing and closes the one case a local malicious process or a
    rebinding proxy would otherwise walk through unchecked."""
    from no_human.config import load_config
    store = await store_factory("test.db")
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    monkeypatch.setattr("no_human.config.ENV_PATH", tmp_path / ".env")
    import no_human.config as _cfg
    assert str(_cfg.ENV_PATH).startswith(str(tmp_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        r = await c.put("/api/auth/token",
                        json={"profile": "personal", "token": TOKEN})
    assert r.status_code == 403
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_a_same_origin_write_still_works(client, tmp_path):
    r = await client.put("/api/auth/token",
                         json={"profile": "personal", "token": TOKEN},
                         headers={"Origin": "http://127.0.0.1:8420"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_status_sees_a_metered_key_sitting_in_the_env_file(client, tmp_path):
    """A key in .env is invisible to os.environ until the next start — and
    'see it without reading your shell profile' is the point of this field."""
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-api03-x\n")
    r = await client.get("/api/auth/status")
    assert r.json()["metered_key_present"] is True


# ------------- guards that previously survived mutation untested ------------ #

@pytest.mark.asyncio
async def test_a_SHORT_credential_shaped_profile_is_refused_by_shape(client):
    """The shape check was untested: the only profile it was tried with was 37
    chars, so the LENGTH cap caught it and deleting the shape check left the
    suite green. A short credential-shaped name is the input that separates
    them."""
    r = await client.put("/api/auth/token",
                         json={"profile": "sk-ant-oat01-ab", "token": TOKEN})
    assert r.status_code == 422
    assert "looks like a token" in r.json()["detail"]


@pytest.mark.asyncio
async def test_no_422_ever_echoes_the_submitted_secret(client):
    """Constraint §8 must hold on the FAILURE paths too. Two routes echoed:
    the regex branch (where a non-sk-ant enterprise token lands) printed the
    value, and pydantic returned the whole body when 'profile' was missing."""
    secret = "EnterpriseTok!9xQz-REAL-LOOKING"
    r = await client.put("/api/auth/token",
                         json={"profile": secret, "token": "v"})
    assert r.status_code == 422
    assert secret not in r.text and secret.lower() not in r.text

    r2 = await client.put("/api/auth/token", json={"token": TOKEN})
    assert r2.status_code == 422
    assert TOKEN not in r2.text


def test_upsert_env_var_guards_at_the_choke_point(tmp_path):
    """The claim was that validation lives in upsert_env_var so EVERY writer is
    covered — but every test reached it via set_profile_token, which validates
    separately, so deleting the choke-point guard left the suite green."""
    from no_human.config import AuthError, upsert_env_var
    env = tmp_path / ".env"
    with pytest.raises(AuthError):
        upsert_env_var(env, "K", "a\u2028ANTHROPIC_API_KEY=forged")
    assert not env.exists()
    with pytest.raises(AuthError):
        upsert_env_var(env, "K\u2028X", "v")
    assert not env.exists()


def test_set_auth_profile_will_not_store_a_credential(tmp_path):
    """`nh auth use <token>` was blocked only by an UNHANDLED AuthError raised
    inside an error-message f-string — an accident that produced a traceback,
    not a refusal. And the stored value is echoed by GET /api/auth/status."""
    from no_human.config import AuthError, set_auth_profile
    cfg = tmp_path / "config.yaml"
    with pytest.raises(AuthError):
        set_auth_profile("sk-ant-oat01-secret", config_path=cfg)
    with pytest.raises(AuthError):
        set_auth_profile("z" * 400, config_path=cfg)


def test_repeated_writes_do_not_accumulate_blank_lines(tmp_path):
    """split("\\n") keeps the trailing empty field, so each append grew the
    file by a blank line — a regression from the splitlines() version."""
    from no_human.config import set_profile_token
    env = tmp_path / ".env"
    for i in range(3):
        set_profile_token(f"p{i}", f"tok{i}", env_path=env)
    assert "\n\n" not in env.read_text()


# ---------------- the OTHER writer of the same credential store -------------- #

@pytest.mark.asyncio
async def test_the_integrations_route_writes_env_and_must_guard_its_origin(
        client, tmp_path):
    """`PUT /api/integrations/{name}/config` writes the SAME ~/.no_human/.env
    the auth route guards. It had no origin check, and with the CORS grant
    open to every origin at the time the preflight succeeded for any site — so a page the
    operator merely visits while `nh serve` is up could plant a credential.
    Demonstrated end to end before this guard existed: 200, value on disk.
    """
    env = tmp_path / ".env"
    r = await client.put(
        "/api/integrations/jira/config",
        json={"fields": {"api_token": "ATTACKER-PLANTED"}},
        headers={"Origin": "https://evil.example"})

    assert r.status_code == 403, r.text
    assert not env.exists() or "ATTACKER-PLANTED" not in env.read_text()


@pytest.mark.asyncio
async def test_a_refused_integration_value_leaves_NOTHING_half_written(
        client, tmp_path):
    """The values are written one key at a time, so a value refused by the
    writer's guard used to land AFTER an earlier key had already been
    committed — a partial write, surfaced as a 500. Every value is validated
    before the loop now, and the guard is the writer's own, so the two cannot
    disagree about what a line is.
    """
    env = tmp_path / ".env"
    r = await client.put(
        "/api/integrations/jenkins/config",
        json={"fields": {"user": "alice",
                         "api_token": "tok\u2028ANTHROPIC_API_KEY=sk-planted"}})

    assert r.status_code == 422, r.text          # not a 500
    body = env.read_text() if env.exists() else ""
    assert "alice" not in body, body             # the EARLIER key did not land
    assert "ANTHROPIC_API_KEY" not in body
    # The refusal names the field but never the value (constraint §8).
    assert "sk-planted" not in r.text


@pytest.mark.asyncio
async def test_the_status_READ_is_origin_guarded_too(client):
    """Mutation survived without this: every origin test was a PUT, so the
    guard on the GET could be deleted by any refactor with the suite green."""
    r = await client.get("/api/auth/status",
                         headers={"Origin": "https://evil.example"})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_a_non_http_scheme_is_refused(client):
    """`file://localhost` has hostname "localhost" and passes the host test.
    Only the scheme clause rejects it — and nothing covered that clause, so it
    too survived deletion."""
    for origin in ("file://localhost", "ftp://127.0.0.1"):
        r = await client.put("/api/auth/token",
                             json={"profile": "personal", "token": TOKEN},
                             headers={"Origin": origin})
        assert r.status_code == 403, f"{origin}: {r.text}"


def test_the_home_dir_is_made_0700_even_when_it_ALREADY_EXISTS(tmp_path):
    """.env is 0600, but config.yaml, no_human.db and cache/ live beside it in
    ~/.no_human.

    The first fix here was a NO-OP and the first test could not see it:
    `mkdir(exist_ok=True, mode=0o700)` applies the mode only when it CREATES
    the directory, and the DB, config.yaml and the repo-map cache all create
    ~/.no_human at the process umask FIRST. So in the real ordering the
    directory is already 0755 by the time a credential is written. Testing
    against a fresh directory structurally could not catch that — this one
    creates it world-readable first, exactly as the other call sites do.
    """
    from no_human.config import upsert_env_var

    home = tmp_path / "home"
    home.mkdir(mode=0o755)                       # what the OTHER writers leave
    assert (home.stat().st_mode & 0o777) == 0o755

    upsert_env_var(home / ".env", "K", "v")

    assert (home.stat().st_mode & 0o777) == 0o700
    assert ((home / ".env").stat().st_mode & 0o777) == 0o600


@pytest.mark.asyncio
async def test_a_status_read_never_echoes_a_secret_pasted_into_the_profile_field(
        client, tmp_path):
    """`llm.auth_profile` is a NAME. A hand-edited or legacy config.yaml can
    hold anything, and the endpoint returned it verbatim — in the very branch
    that had already REJECTED it, one rejection reason being "that looks like a
    token, not a profile name". It would render into the Settings UI and into
    any screenshot made from it."""
    leaked = "sk-ant-oat01-LEAKED-SECRET"
    app.state.config.data.setdefault("llm", {})["auth_profile"] = leaked

    r = await client.get("/api/auth/status")
    assert r.status_code == 200, r.text
    assert leaked not in r.text, r.text
    assert "oat01" not in r.text, r.text
    # The rejection is still REPORTED — redaction must not hide the problem.
    assert "invalid" in r.text.lower()


def test_a_TRAILING_separator_is_refused_rather_than_silently_truncated(
        tmp_path):
    """`splitlines()` DROPS a trailing break, so a length check alone accepted
    "tok\\u2028": it survived the write and the reader returned a TRUNCATED
    token — no injection, but a credential that then fails for no visible
    reason, which is the hardest kind of auth bug to diagnose."""
    from no_human.config import AuthError, _read_env_file, upsert_env_var

    env = tmp_path / ".env"
    for sep in ("\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e",
                "\x85", "\u2028", "\u2029"):
        with pytest.raises(AuthError):
            upsert_env_var(env, "K", f"tok{sep}")
    upsert_env_var(env, "K", "tok")
    assert _read_env_file(env)["K"] == "tok"


@pytest.mark.parametrize("writer", ["db", "config", "pidlock", "cache"])
def test_every_writer_leaves_the_home_dir_private(tmp_path, monkeypatch, writer):
    """~/.no_human holds the credential store, config.yaml and the DB. Only
    .env is chmod'd individually; the DIRECTORY's 0700 protects the rest.

    This drives the REAL writers. The first version called `ensure_private_dir`
    directly and was still named "every writer" — a claim the test did not
    make, and all four call sites could be reverted to a bare mkdir with the
    suite green.
    """
    import no_human.config as cfg

    home = tmp_path / ".no_human"
    monkeypatch.setattr(cfg, "NO_HUMAN_HOME", home)
    monkeypatch.setattr(cfg, "CONFIG_PATH", home / "config.yaml")

    if writer == "db":
        import asyncio

        from no_human.core.db import Store
        async def _go():
            await (await Store(home / "no_human.db").connect()).close()
        asyncio.run(_go())
    elif writer == "config":
        cfg.load_config(home / "config.yaml")
    elif writer == "pidlock":
        import no_human.cli.commands as cmd
        monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", home)
        cmd._acquire_pid_lock()
    else:
        # The PRODUCTION path: repo_map() is what creates the cache dir.
        # Calling ensure_private_dir directly here let the real call site be
        # reverted to a bare mkdir with the suite green.
        import no_human.context.repo_map as rm
        monkeypatch.setattr(rm, "_CACHE_DIR", home / "cache")
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        rm.repo_map(repo)

    assert home.exists(), f"{writer} did not create the home dir"
    assert (home.stat().st_mode & 0o777) == 0o700, (
        f"{writer} left ~/.no_human at {oct(home.stat().st_mode & 0o777)}")


def test_the_walk_NEVER_touches_anything_above_the_home_dir(tmp_path, monkeypatch):
    """THE safety bound, and the catastrophic case if it is wrong.

    The walk chmods ancestors, so an off-by-one would march to `/`. Nothing
    asserted that before: replacing the bounded walk with
    `[path, *path.resolve().parents]` passed both original tests while
    chmod'ing the pytest basetemp — and, on a real run, everything up to the
    filesystem root including $HOME.
    """
    import no_human.config as cfg

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    home = outside / ".no_human"
    monkeypatch.setattr(cfg, "NO_HUMAN_HOME", home)
    tmp_path.chmod(0o755)

    cfg.ensure_private_dir(home / "cache" / "deep")

    assert (home.stat().st_mode & 0o777) == 0o700
    assert ((home / "cache" / "deep").stat().st_mode & 0o777) == 0o700
    # Everything ABOVE the home dir is untouched.
    assert (outside.stat().st_mode & 0o777) == 0o755, "walked above NO_HUMAN_HOME"
    assert (tmp_path.stat().st_mode & 0o777) == 0o755, "walked to the root"

    # A path that LOOKS like it is under the home dir but escapes via "..".
    # `.resolve()` is what defeats this, and nothing pinned it: dropping it
    # passed the entire suite while chmod'ing both the root AND an unrelated
    # sibling. `relative_to` on the UNRESOLVED path happily succeeds, because
    # the string does start with NO_HUMAN_HOME.
    # home is outside/.no_human, so home/.. is OUTSIDE, not tmp_path.
    victim = outside / "victim"
    victim.mkdir(mode=0o755)

    cfg.ensure_private_dir(home / ".." / "victim" / "escaped")

    assert (victim.stat().st_mode & 0o777) == 0o755, "escaped via .."
    assert (tmp_path.stat().st_mode & 0o777) == 0o755, "escaped to the root"
    # The leaf itself is still secured — leaf-only is the correct fallback.
    assert ((victim / "escaped").stat().st_mode & 0o777) == 0o700


def test_a_symlink_leaf_is_NOT_chmod_through(tmp_path, monkeypatch):
    """`Path.chmod` follows symlinks, so a symlink planted inside ~/.no_human
    pointing at an OUTSIDE directory had that target narrowed to 0700 (a
    DoS-shaped hardening bug from #214's review). We only secure the real
    directories we create in our own subtree — a symlink is skipped, and the
    file it guards is 0600 regardless."""
    import no_human.config as cfg

    home = tmp_path / ".no_human"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(cfg, "NO_HUMAN_HOME", home)

    outside = tmp_path / "victim"
    outside.mkdir(mode=0o755)
    link = home / "cache"
    link.symlink_to(outside)          # attacker plants a symlink

    cfg.ensure_private_dir(link)

    assert (outside.stat().st_mode & 0o777) == 0o755, "chmod'd through the symlink"
    # the home dir itself (a real dir) is still secured
    assert (home.stat().st_mode & 0o777) == 0o700


def test_a_path_outside_the_home_dir_secures_only_the_leaf(tmp_path, monkeypatch):
    """A custom path is not ours to restructure — secure the leaf, leave the
    caller's parent alone."""
    import no_human.config as cfg

    monkeypatch.setattr(cfg, "NO_HUMAN_HOME", tmp_path / ".no_human")
    parent = tmp_path / "someone_elses"
    parent.mkdir(mode=0o755)

    cfg.ensure_private_dir(parent / "leaf")

    assert ((parent / "leaf").stat().st_mode & 0o777) == 0o700
    assert (parent.stat().st_mode & 0o777) == 0o755, "chmod'd a caller's dir"


def test_group_and_other_are_cleared_without_widening_owner_bits(tmp_path,
                                                                 monkeypatch):
    """The documented no-churn rule, which `chmod(0o700)` did not implement:
    it RESTORED owner-write on 0550 and silently dropped setgid on 02750."""
    import os

    import no_human.config as cfg

    for before, after in ((0o755, 0o700), (0o550, 0o500), (0o2750, 0o2700),
                          (0o500, 0o500), (0o700, 0o700)):
        d = tmp_path / f"d{before:o}"
        d.mkdir()
        os.chmod(d, before)
        monkeypatch.setattr(cfg, "NO_HUMAN_HOME", d)
        cfg.ensure_private_dir(d)
        got = d.stat().st_mode & 0o7777
        assert got == after, f"{oct(before)} -> {oct(got)}, expected {oct(after)}"
        assert not got & 0o077, "group/other access survived"


def test_a_tighter_lockdown_is_never_reopened_but_IS_explained(tmp_path,
                                                                monkeypatch):
    """Two properties that pull against each other, both required.

    The old `init_cmd` used `!= 0o700`, so an operator who locked the directory
    down FURTHER (0500) had it silently re-opened. Delegating to the shared
    helper fixes that — the security goal is "no group or other access", and
    owner bits are the operator's business.

    But `nh init`'s contract is "make my install work", and without owner-write
    the NEXT step died with a raw PermissionError three calls downstream. So it
    must refuse to widen AND say why, in one place, before anything fails.
    """
    import click
    import no_human.config as cfg
    from no_human.cli import init_cmd

    home = tmp_path / ".no_human"
    home.mkdir(mode=0o500)
    monkeypatch.setattr(cfg, "NO_HUMAN_HOME", home)
    monkeypatch.setattr(init_cmd, "NO_HUMAN_HOME", home)

    with pytest.raises(click.ClickException) as exc:
        init_cmd.ensure_home_dir()

    assert (home.stat().st_mode & 0o777) == 0o500, "must not widen owner bits"
    msg = str(exc.value)
    assert "0500" in msg, msg           # names the actual mode
    assert "chmod" in msg, msg          # tells them how to fix it
    assert str(home) in msg, msg        # names the directory


def test_the_diagnostic_renders_an_ODD_mode_readably(tmp_path, monkeypatch):
    """Clearing group/other can leave 0000 (e.g. from 0070), and `oct()`
    renders that as "0o0" — which reads like a bug, not a permission. Fixed
    width keeps it recognisable as a mode."""
    import click
    import no_human.config as cfg
    from no_human.cli import init_cmd

    home = tmp_path / ".no_human"
    home.mkdir(mode=0o070)
    monkeypatch.setattr(cfg, "NO_HUMAN_HOME", home)
    monkeypatch.setattr(init_cmd, "NO_HUMAN_HOME", home)

    with pytest.raises(click.ClickException) as exc:
        init_cmd.ensure_home_dir()

    assert (home.stat().st_mode & 0o777) == 0o000, "group access must be gone"
    assert "0000" in str(exc.value), str(exc.value)
    assert "0o0" not in str(exc.value), str(exc.value)
    # Restore owner access or pytest cannot remove the tmp dir, which leaks a
    # PytestWarning and an undeletable directory into the shared tmp root on
    # every run — noticed because it polluted an UNRELATED worktree's output.
    home.chmod(0o700)


def test_a_symlinked_home_logs_the_downgrade_but_a_planted_leaf_is_silent(
        tmp_path, monkeypatch, caplog):
    """When NO_HUMAN_HOME itself is a symlink, skipping it leaves the store dir
    unsecured — a real downgrade (#221 review), so WARN. A planted LEAF symlink
    is correctly skipped and must stay silent, or every attack attempt spams
    the log."""
    import logging
    import no_human.config as cfg

    # (a) planted leaf symlink under a real home -> skipped, NO warning
    real_home = tmp_path / "a" / ".no_human"
    real_home.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(cfg, "NO_HUMAN_HOME", real_home)
    outside = tmp_path / "a" / "victim"; outside.mkdir(mode=0o755)
    (real_home / "cache").symlink_to(outside)
    with caplog.at_level(logging.WARNING, logger="no_human.config"):
        cfg.ensure_private_dir(real_home / "cache")
    assert not any("symlink" in r.message for r in caplog.records), \
        "a planted leaf symlink must not warn"

    # (b) NO_HUMAN_HOME itself is a symlink -> WARN
    caplog.clear()
    store = tmp_path / "b" / "real_store"; store.mkdir(parents=True, mode=0o755)
    linked_home = tmp_path / "b" / ".no_human"; linked_home.symlink_to(store)
    monkeypatch.setattr(cfg, "NO_HUMAN_HOME", linked_home)
    with caplog.at_level(logging.WARNING, logger="no_human.config"):
        cfg.ensure_private_dir(linked_home)
    assert any("symlink" in r.message and "0600" in r.message
               for r in caplog.records), caplog.text


# =========================== Codex, first-class ============================= #
#
# Codex made first-class in Settings' Account panel alongside Claude: the auth
# MODE may live in config.yaml (constraint #6b), the OpenAI KEY only in the
# private .env, and neither the status block nor either write ever returns,
# logs or echoes a credential value (constraint §8).

@pytest.mark.asyncio
async def test_status_carries_a_codex_block(client):
    """The default install (llm.codex_auth_mode unset) reads as api_key, and
    every codex field is a name/bool/None — never a credential value."""
    r = await client.get("/api/auth/status")
    assert r.status_code == 200
    codex = r.json()["codex"]
    assert codex["auth_mode"] == "api_key"
    assert isinstance(codex["api_key_present"], bool)
    # subscription session is not a concept in api_key mode -> null, never a probe
    assert codex["subscription_session_present"] is None
    assert isinstance(codex["model"], str) and codex["model"]


@pytest.mark.asyncio
async def test_status_codex_never_leaks_the_openai_key(client, tmp_path):
    """api_key_present detects a key in .env; the value never reaches the wire."""
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-openai-SECRET-xyz\n")
    r = await client.get("/api/auth/status")
    assert r.json()["codex"]["api_key_present"] is True
    assert "sk-openai-SECRET-xyz" not in r.text
    assert "SECRET" not in r.text


@pytest.mark.asyncio
async def test_status_codex_subscription_session_null_when_cli_absent(
        client, tmp_path, monkeypatch):
    """subscription mode + no resolvable codex CLI -> null (can't run the
    existence check), and the GET must not shell out or 500."""
    (tmp_path / "config.yaml").write_text("llm:\n  codex_auth_mode: subscription\n")
    monkeypatch.setattr(
        "no_human.agent.codex_backend.find_codex_cli", lambda *a, **k: None)
    r = await client.get("/api/auth/status")
    codex = r.json()["codex"]
    assert codex["auth_mode"] == "subscription"
    assert codex["subscription_session_present"] is None


@pytest.mark.asyncio
async def test_status_codex_subscription_session_present_when_signed_in(
        client, tmp_path, monkeypatch):
    """A live ChatGPT session (via `codex login status`, existence-check only)
    surfaces as True — never `codex login`, never a credential value."""
    from no_human.agent.codex_backend import CodexSessionStatus
    (tmp_path / "config.yaml").write_text("llm:\n  codex_auth_mode: subscription\n")
    monkeypatch.setattr(
        "no_human.agent.codex_backend.find_codex_cli", lambda *a, **k: "/x/codex")
    monkeypatch.setattr(
        "no_human.agent.codex_backend.codex_login_status",
        lambda *a, **k: CodexSessionStatus(present=True, via="chatgpt"))
    r = await client.get("/api/auth/status")
    assert r.json()["codex"]["subscription_session_present"] is True


@pytest.mark.asyncio
async def test_put_codex_mode_writes_config_not_env(client, tmp_path, monkeypatch):
    """The MODE goes to config.yaml, never .env — the mirror of the KEY rule."""
    monkeypatch.setattr(
        "no_human.agent.codex_backend.find_codex_cli", lambda *a, **k: None)
    r = await client.put("/api/auth/codex-mode", json={"mode": "subscription"})
    assert r.status_code == 200, r.text
    assert "codex_auth_mode: subscription" in (tmp_path / "config.yaml").read_text()
    assert not (tmp_path / ".env").exists()       # a MODE write never touches .env
    assert r.json()["codex"]["auth_mode"] == "subscription"


@pytest.mark.asyncio
async def test_put_codex_mode_rejects_an_unknown_mode(client):
    r = await client.put("/api/auth/codex-mode", json={"mode": "gpt5"})
    assert r.status_code == 422
    assert "api_key" in r.json()["detail"]


@pytest.mark.asyncio
async def test_codex_mode_cross_origin_is_refused(client, tmp_path):
    r = await client.put("/api/auth/codex-mode", json={"mode": "subscription"},
                         headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    # nothing written under a rejected origin
    cfg = tmp_path / "config.yaml"
    assert not cfg.exists() or "codex_auth_mode: subscription" not in cfg.read_text()


@pytest.mark.asyncio
async def test_put_codex_key_writes_env_not_config_and_never_echoes(
        client, tmp_path):
    """The KEY goes to ~/.no_human/.env only; the response carries the variable
    NAME, never the value (constraint §8)."""
    r = await client.put("/api/auth/codex-key", json={"key": "sk-openai-REAL-99"})
    assert r.status_code == 200, r.text
    assert "OPENAI_API_KEY=sk-openai-REAL-99" in (tmp_path / ".env").read_text()
    cfg = tmp_path / "config.yaml"
    if cfg.exists():
        assert "sk-openai-REAL-99" not in cfg.read_text()
    assert r.json()["codex_key_var"] == "OPENAI_API_KEY"
    assert "sk-openai-REAL-99" not in r.text
    assert r.json()["codex"]["api_key_present"] is True


@pytest.mark.asyncio
async def test_put_codex_key_file_is_owner_only(client, tmp_path):
    await client.put("/api/auth/codex-key", json={"key": "sk-openai-x"})
    assert oct((tmp_path / ".env").stat().st_mode)[-3:] == "600"


@pytest.mark.asyncio
async def test_put_codex_key_refuses_an_empty_key(client, tmp_path):
    r = await client.put("/api/auth/codex-key", json={"key": "   "})
    assert r.status_code == 422
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("sep", ["\n", "\r", " ", "\x00"])
@pytest.mark.asyncio
async def test_put_codex_key_refuses_line_injection(client, tmp_path, sep):
    """A break in the key would forge a second .env line — an ANTHROPIC_API_KEY
    among them is the metered-billing escape constraint #1 shuts."""
    r = await client.put(
        "/api/auth/codex-key",
        json={"key": f"sk-openai{sep}ANTHROPIC_API_KEY=sk-ant-api03-forged"})
    assert r.status_code == 422, sep
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_put_codex_key_refusal_never_echoes_the_key(client):
    """The 422 detail names the problem, never the submitted value."""
    r = await client.put(
        "/api/auth/codex-key", json={"key": "sk-openai-LEAKME\nX=y"})
    assert r.status_code == 422
    assert "sk-openai-LEAKME" not in r.text


@pytest.mark.asyncio
async def test_codex_key_cross_origin_is_refused(client, tmp_path):
    """The same drive-by CSRF guard the token route has — this route writes the
    SAME ~/.no_human/.env."""
    r = await client.put("/api/auth/codex-key", json={"key": "sk-openai-x"},
                         headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert not (tmp_path / ".env").exists()


def test_set_codex_api_key_returns_the_var_name_only(tmp_path):
    """Unit guard: the writer returns the NAME, and .env holds the value while
    config.yaml never can (the KEY-in-.env, MODE-in-config split)."""
    from no_human.config import set_codex_api_key
    env = tmp_path / ".env"
    assert set_codex_api_key("sk-openai-unit", env_path=env) == "OPENAI_API_KEY"
    assert "OPENAI_API_KEY=sk-openai-unit" in env.read_text()


def test_set_codex_auth_mode_rejects_a_bad_value(tmp_path):
    from no_human.config import set_codex_auth_mode
    cfg = tmp_path / "config.yaml"
    with pytest.raises(ValueError):
        set_codex_auth_mode("chatgpt-please", config_path=cfg)
