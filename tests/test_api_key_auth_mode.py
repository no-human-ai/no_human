"""BYO-API-key auth mode (llm.auth_mode: "api_key").

An operator-authorized, explicit departure from the OAuth-only default: a
friend/commercial install can pay Anthropic directly with THEIR OWN
ANTHROPIC_API_KEY instead of a Claude subscription token. The invariants that
must still hold:

* the run bills exactly ONE path — the provided key — so every OTHER metered
  redirect (ANTHROPIC_AUTH_TOKEN, Bedrock, Vertex) is still scrubbed;
* a missing key fails loudly (never a silent fall-through to something else);
* no OAuth token is exported in this mode;
* subscription mode is UNCHANGED (API key still aborts startup);
* the billing path is stamped in attribution as the profile "api_key".
"""
from __future__ import annotations

import os

import pytest

import no_human.config as config
from no_human.config import AuthError, assert_subscription_mode


@pytest.fixture(autouse=True)
def _scrub_metered_on_teardown():
    """Every test here exercises code that sets ANTHROPIC_API_KEY in os.environ
    DIRECTLY (load_api_key, _setup_api_key). When the var was already absent,
    monkeypatch.delenv(raising=False) records nothing to undo, so a direct set
    would LEAK into later tests (it broke test_auth_profiles under xdist).
    Scrub every metered var after each test regardless of how it got set."""
    yield
    for var in config.METERED_AUTH_VARS:
        os.environ.pop(var, None)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate the process env + point ENV_PATH at an empty tmp .env."""
    for var in config.METERED_AUTH_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(config.SUBSCRIPTION_TOKEN_VAR, raising=False)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_PATH", env_file)
    # Reset the module-global stamp so assertions on it are meaningful.
    monkeypatch.setattr(config, "_ACTIVE_AUTH_PROFILE", None, raising=False)
    return env_file


def test_api_key_mode_keeps_key_and_requires_no_oauth(clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-friend-key")
    # No CLAUDE_CODE_OAUTH_TOKEN anywhere — must NOT be required in this mode.
    report = assert_subscription_mode(auth_mode="api_key")
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-friend-key"
    assert config.SUBSCRIPTION_TOKEN_VAR not in os.environ
    assert report is not None


def test_api_key_mode_scrubs_other_metered_redirects(clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-friend-key")
    # These, alongside the key, would 401 or silently redirect billing.
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauthy")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "proj")
    assert_subscription_mode(auth_mode="api_key")
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-friend-key"  # kept
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ  # scrubbed
    assert "CLAUDE_CODE_USE_BEDROCK" not in os.environ  # scrubbed
    assert "ANTHROPIC_VERTEX_PROJECT_ID" not in os.environ  # scrubbed


def test_api_key_mode_scrubs_inherited_oauth_token(clean_env, monkeypatch):
    """A subscription token inherited from the shell must not reach the SDK
    subprocess alongside the key — "bills exactly one path" must hold by
    construction, not by the SDK's documented credential precedence."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-friend-key")
    monkeypatch.setenv(config.SUBSCRIPTION_TOKEN_VAR, "sk-ant-oat-inherited")
    report = assert_subscription_mode(auth_mode="api_key")
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-friend-key"  # kept
    assert config.SUBSCRIPTION_TOKEN_VAR not in os.environ  # scrubbed
    assert config.SUBSCRIPTION_TOKEN_VAR in report.removed


def test_api_key_mode_loads_key_from_env_file(clean_env, monkeypatch):
    clean_env.write_text("ANTHROPIC_API_KEY=sk-ant-from-dotenv\n")
    assert_subscription_mode(auth_mode="api_key")
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-dotenv"


def test_api_key_mode_missing_key_fails_loudly(clean_env):
    with pytest.raises(AuthError) as exc:
        assert_subscription_mode(auth_mode="api_key")
    assert "api_key" in str(exc.value).lower()


def test_api_key_mode_stamps_billing_path(clean_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-friend-key")
    assert_subscription_mode(auth_mode="api_key")
    assert config.active_auth_profile() == "api_key"


def test_subscription_mode_still_rejects_api_key(clean_env, monkeypatch):
    """Regression guard: the default mode must NOT tolerate a metered key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oops")
    monkeypatch.setenv(config.SUBSCRIPTION_TOKEN_VAR, "sk-ant-oat-token")
    with pytest.raises(AuthError):
        assert_subscription_mode(auth_mode="subscription")


def test_default_auth_mode_is_subscription(clean_env, monkeypatch):
    """Omitting auth_mode keeps the strict subscription behavior (key aborts)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oops")
    with pytest.raises(AuthError):
        assert_subscription_mode()


# --------------------------------------------------------------------------- #
# `nh init` BYO-API-key wiring                                                 #
# --------------------------------------------------------------------------- #


def test_setup_token_byo_path_writes_key_and_returns_api_key_mode(tmp_path, monkeypatch):
    """Choosing '2' + pasting a key persists it and returns ("ready", "api_key")."""
    import click

    import no_human.cli.init_cmd as init_mod

    nh_home = tmp_path / "nh_home"
    nh_home.mkdir(mode=0o700)
    env_path = nh_home / ".env"
    config_path = nh_home / "config.yaml"  # absent → mode prompt fires
    monkeypatch.setattr(init_mod, "ENV_PATH", env_path)
    monkeypatch.setattr(init_mod, "CONFIG_PATH", config_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    answers = iter(["2", "sk-ant-byo-friend"])  # mode choice, then the key
    monkeypatch.setattr(click, "prompt", lambda *a, **k: next(answers))

    ready, mode = init_mod.setup_token()
    assert (ready, mode) == (True, "api_key")
    assert "ANTHROPIC_API_KEY=sk-ant-byo-friend" in env_path.read_text(encoding="utf-8")


def test_ensure_config_persists_api_key_mode(tmp_path, monkeypatch):
    """ensure_config(auth_mode="api_key") writes llm.auth_mode into config.yaml."""
    import yaml

    import no_human.cli.init_cmd as init_mod

    nh_home = tmp_path / "nh_home"
    nh_home.mkdir(mode=0o700)
    config_path = nh_home / "config.yaml"
    config_path.write_text("llm:\n  auth_mode: subscription\n")
    monkeypatch.setattr(init_mod, "CONFIG_PATH", config_path)

    init_mod.ensure_config(auth_mode="api_key")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["llm"]["auth_mode"] == "api_key"


def test_ensure_config_persists_subscription_mode_over_api_key(tmp_path, monkeypatch):
    """ensure_config(auth_mode="subscription") must persist the switch back
    from api_key, not silently no-op it (SCRUM-7)."""
    import yaml

    import no_human.cli.init_cmd as init_mod

    nh_home = tmp_path / "nh_home"
    nh_home.mkdir(mode=0o700)
    config_path = nh_home / "config.yaml"
    config_path.write_text("llm:\n  auth_mode: api_key\n")
    monkeypatch.setattr(init_mod, "CONFIG_PATH", config_path)

    init_mod.ensure_config(auth_mode="subscription")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["llm"]["auth_mode"] == "subscription"


def test_ensure_config_preserves_other_keys_on_mode_switch(tmp_path, monkeypatch):
    """A mode switch must not clobber unrelated config keys."""
    import yaml

    import no_human.cli.init_cmd as init_mod

    nh_home = tmp_path / "nh_home"
    nh_home.mkdir(mode=0o700)
    config_path = nh_home / "config.yaml"
    config_path.write_text(
        "llm:\n"
        "  auth_mode: api_key\n"
        "  implementer_model: claude-sonnet-5\n"
        "git:\n"
        "  agent_identity_name: no_human\n"
    )
    monkeypatch.setattr(init_mod, "CONFIG_PATH", config_path)

    init_mod.ensure_config(auth_mode="subscription")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["llm"]["auth_mode"] == "subscription"
    assert data["llm"]["implementer_model"] == "claude-sonnet-5"
    assert data["git"]["agent_identity_name"] == "no_human"


def test_ensure_config_unchanged_mode_does_not_rewrite(tmp_path, monkeypatch):
    """Calling ensure_config with the already-configured mode must not touch
    the file at all."""
    import no_human.cli.init_cmd as init_mod

    nh_home = tmp_path / "nh_home"
    nh_home.mkdir(mode=0o700)
    config_path = nh_home / "config.yaml"
    config_path.write_text("llm:\n  auth_mode: api_key\n")
    monkeypatch.setattr(init_mod, "CONFIG_PATH", config_path)
    before = config_path.read_bytes()

    init_mod.ensure_config(auth_mode="api_key")
    assert config_path.read_bytes() == before


def test_setup_token_respects_configured_api_key_without_prompt(tmp_path, monkeypatch):
    """A configured api_key install with its key present must not re-prompt."""
    import no_human.cli.init_cmd as init_mod

    nh_home = tmp_path / "nh_home"
    nh_home.mkdir(mode=0o700)
    env_path = nh_home / ".env"
    env_path.write_text("ANTHROPIC_API_KEY=sk-ant-existing\n")
    config_path = nh_home / "config.yaml"
    config_path.write_text("llm:\n  auth_mode: api_key\n")
    monkeypatch.setattr(init_mod, "ENV_PATH", env_path)
    monkeypatch.setattr(init_mod, "CONFIG_PATH", config_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # No click.prompt patched — if it tried to prompt, the test would hang/err.
    ready, mode = init_mod.setup_token()
    assert (ready, mode) == (True, "api_key")
