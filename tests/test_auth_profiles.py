"""Auth profiles: which subscription pays for a run (M0.0).

The property under test is a billing-safety one. A named profile must resolve
its own token or fail loudly — never fall back to another subscription's token,
never leak a token value into a name-only surface.
"""

import os

import pytest
import yaml

from no_human import config
from no_human.config import (
    DEFAULT_AUTH_PROFILE,
    SUBSCRIPTION_TOKEN_VAR,
    AuthError,
    active_auth_profile,
    assert_subscription_mode,
    available_auth_profiles,
    load_config,
    load_env_token,
    profile_token_var,
    set_auth_profile,
)
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier

PERSONAL_VAR = "CLAUDE_CODE_OAUTH_TOKEN_PERSONAL"
ENTERPRISE_VAR = "CLAUDE_CODE_OAUTH_TOKEN_ENTERPRISE"


@pytest.fixture(autouse=True)
def _isolate_auth_env(monkeypatch):
    """No token variable and no recorded profile leaks between tests."""
    for var in (SUBSCRIPTION_TOKEN_VAR, PERSONAL_VAR, ENTERPRISE_VAR):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "_ACTIVE_AUTH_PROFILE", None)


def test_profile_token_var_maps_default_to_the_unsuffixed_variable():
    assert profile_token_var(DEFAULT_AUTH_PROFILE) == SUBSCRIPTION_TOKEN_VAR
    assert profile_token_var("personal") == PERSONAL_VAR
    assert profile_token_var("ENTERPRISE") == ENTERPRISE_VAR


def test_named_profile_exports_its_own_token(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        f"{SUBSCRIPTION_TOKEN_VAR}=default-tok\n{PERSONAL_VAR}=personal-tok\n"
    )
    assert load_env_token(env, profile="personal") == "personal-tok"
    # exactly one token is exported, and it is the SDK's canonical variable
    assert os.environ[SUBSCRIPTION_TOKEN_VAR] == "personal-tok"
    assert active_auth_profile() == "personal"


def test_named_profile_without_a_token_never_falls_back_to_another(tmp_path):
    """The billing-safety property: a silent fallback would bill the wrong
    subscription. Reverting to `token = ... or os.environ[SUBSCRIPTION_TOKEN_VAR]`
    makes this test fail with 'default-tok' exported under the personal name."""
    env = tmp_path / ".env"
    env.write_text(f"{SUBSCRIPTION_TOKEN_VAR}=default-tok\n")
    with pytest.raises(AuthError, match="profile 'personal' has no token"):
        load_env_token(env, profile="personal")
    assert os.environ.get(SUBSCRIPTION_TOKEN_VAR) != "personal-tok"
    assert active_auth_profile() is None


def test_default_profile_still_reads_the_unsuffixed_variable(tmp_path):
    """Backwards compatibility: the pre-profile .env keeps working untouched."""
    env = tmp_path / ".env"
    env.write_text(f'# comment\n{SUBSCRIPTION_TOKEN_VAR}="file-token"\n')
    assert load_env_token(env) == "file-token"
    assert active_auth_profile() == DEFAULT_AUTH_PROFILE


def test_default_profile_returns_none_when_no_token_anywhere(tmp_path):
    assert load_env_token(tmp_path / "absent.env") is None
    assert active_auth_profile() is None


def test_env_file_wins_over_an_inherited_profile_token(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSONAL_VAR, "inherited")
    env = tmp_path / ".env"
    env.write_text(f"{PERSONAL_VAR}=curated\n")
    assert load_env_token(env, profile="personal") == "curated"


def test_inherited_profile_token_is_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSONAL_VAR, "inherited")
    assert load_env_token(tmp_path / "absent.env", profile="personal") == "inherited"


def test_available_profiles_lists_names_never_values(tmp_path, monkeypatch):
    monkeypatch.setenv(ENTERPRISE_VAR, "enterprise-tok")
    env = tmp_path / ".env"
    env.write_text(
        f"{SUBSCRIPTION_TOKEN_VAR}=default-tok\n"
        f"{PERSONAL_VAR}=personal-tok\n"
        "SSO_PASSWORD=hunter2\n"
    )
    profiles = available_auth_profiles(env)
    assert profiles == ["default", "enterprise", "personal"]
    for secret in ("default-tok", "personal-tok", "enterprise-tok", "hunter2"):
        assert not any(secret in p for p in profiles)


def test_available_profiles_ignores_empty_tokens(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{SUBSCRIPTION_TOKEN_VAR}=\n{PERSONAL_VAR}=tok\n")
    assert available_auth_profiles(env) == ["personal"]


def test_assert_subscription_mode_honours_the_profile(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{PERSONAL_VAR}=personal-tok\n")
    report = assert_subscription_mode(env_path=env, profile="personal")
    assert report.api_key_present is False
    assert os.environ[SUBSCRIPTION_TOKEN_VAR] == "personal-tok"


def test_assert_subscription_mode_still_scrubs_the_api_key_under_a_profile(
    monkeypatch, tmp_path
):
    """The scrub semantics must survive the profile plumbing untouched."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    env = tmp_path / ".env"
    env.write_text(f"{PERSONAL_VAR}=personal-tok\n")
    with pytest.raises(AuthError, match="ANTHROPIC_API_KEY"):
        assert_subscription_mode(env_path=env, profile="personal")
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_default_config_pins_the_default_profile():
    assert config.DEFAULT_CONFIG["llm"]["auth_profile"] == DEFAULT_AUTH_PROFILE


def test_config_predating_auth_profiles_resolves_the_default(tmp_path):
    """The frozen-config trap: a config.yaml written before this key existed
    must still resolve `default`, not None."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  auth_mode: subscription\n")
    assert load_config(cfg_path).data["llm"]["auth_profile"] == DEFAULT_AUTH_PROFILE


def test_bootstrap_survives_an_llm_block_commented_out(tmp_path, monkeypatch):
    """config.yaml is hand-edited. A bare `llm:` with its body commented out
    deep-merges to None; reading it as `config["llm"].get(...)` raises
    AttributeError before the auth error the operator needs to see."""
    from no_human.cli import commands as cmd_mod

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  # auth_mode: subscription\nserver:\n  port: 8420\n")
    cfg = load_config(cfg_path)
    assert cfg.data["llm"] is None

    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    seen = {}
    monkeypatch.setattr(
        cmd_mod, "assert_subscription_mode", lambda **kw: seen.update(kw)
    )
    cmd_mod._bootstrap()
    # _bootstrap passes both the profile and the billing mode; a bare/commented
    # `llm:` block yields the default subscription mode without raising.
    assert seen == {"profile": None, "auth_mode": "subscription"}


# --------------------------- set_auth_profile ------------------------------ #


def test_set_auth_profile_preserves_comments_and_round_trips(tmp_path):
    """A safe_load/safe_dump round-trip would delete the operator's comments —
    including the one warning that pinning models here shadowed real defaults."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "server:\n"
        "  port: 8420\n"
        "llm:\n"
        "  # Model IDs are intentionally NOT pinned here — config.py owns them.\n"
        "  auth_mode: subscription\n"
        "git:\n"
        "  branch_prefix: no-human/\n"
    )
    assert set_auth_profile("Personal", cfg_path) == "personal"

    text = cfg_path.read_text()
    assert "# Model IDs are intentionally NOT pinned here" in text
    assert "branch_prefix: no-human/" in text
    assert load_config(cfg_path).data["llm"]["auth_profile"] == "personal"
    assert load_config(cfg_path).data["server"]["port"] == 8420


def test_set_auth_profile_replaces_an_existing_pin(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  auth_profile: personal\n  auth_mode: subscription\n")
    set_auth_profile("enterprise", cfg_path)
    assert load_config(cfg_path).data["llm"]["auth_profile"] == "enterprise"
    assert cfg_path.read_text().count("auth_profile:") == 1


def test_set_auth_profile_adds_the_llm_block_when_absent(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("server:\n  port: 8420\n")
    set_auth_profile("personal", cfg_path)
    assert load_config(cfg_path).data["llm"]["auth_profile"] == "personal"


def test_set_auth_profile_edits_only_the_llm_block(tmp_path):
    """A key named auth_profile under another section must not be hijacked."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "ci_gate:\n  auth_profile: untouched\nllm:\n  auth_mode: subscription\n"
    )
    set_auth_profile("personal", cfg_path)
    data = load_config(cfg_path).data
    assert data["ci_gate"]["auth_profile"] == "untouched"
    assert data["llm"]["auth_profile"] == "personal"


def test_set_auth_profile_splices_into_a_header_with_an_inline_comment(tmp_path):
    """The header-locator used to require `ln.rstrip() == "llm:"` exactly, so
    a header carrying an inline comment (a real hand-edit, e.g. "which
    subscription pays") was treated as absent: a SECOND `llm:` block got
    appended, and PyYAML's last-wins constructor silently resolved only that
    new block — dropping the operator's entire original section, including
    which subscription pays."""
    cfg_path = tmp_path / "config.yaml"
    seed = (
        "server:\n"
        "  port: 8420\n"
        "llm:  # which subscription pays\n"
        "  auth_profile: personal\n"
        "  auth_mode: subscription\n"
    )
    cfg_path.write_text(seed)

    assert set_auth_profile("enterprise", cfg_path) == "enterprise"

    text = cfg_path.read_text()
    assert text.count("llm:") == 1
    assert "llm:  # which subscription pays" in text
    assert text.count("auth_profile:") == 1
    cfg = load_config(cfg_path)
    assert cfg.data["llm"]["auth_mode"] == "subscription"
    assert cfg.data["llm"]["auth_profile"] == "enterprise"

    before_lines = seed.splitlines()
    after_lines = text.splitlines()
    assert len(before_lines) == len(after_lines)
    assert sum(a != b for a, b in zip(before_lines, after_lines)) == 1


def test_set_auth_profile_does_not_hijack_an_llm_header_with_a_value(tmp_path):
    """`llm: {}` carries a value on the header line, so `_LLM_HEADER_RE`
    deliberately does not match it (pins the conservative half of the
    regex): `_splice_llm_scalar` still appends a fresh `llm:` block exactly
    as before this fix. That now leaves two top-level `llm` keys in the
    file, which is precisely the shape the new duplicate-key guard exists to
    catch — so the write is refused and the original is restored byte-for-
    byte, which is a strictly SAFER outcome than the pre-guard behaviour
    (silent last-wins) for the exact same input."""
    cfg_path = tmp_path / "config.yaml"
    seed = "llm: {}\n"
    cfg_path.write_text(seed)

    with pytest.raises(AuthError, match="duplicate top-level key"):
        set_auth_profile("personal", cfg_path)

    assert cfg_path.read_text() == seed
    assert "llm: {}" in cfg_path.read_text().splitlines()

    # Unit-level pin, independent of the duplicate-key guard: the splicer's
    # own append-when-not-a-block behaviour is unchanged by this fix.
    lines = ["llm: {}"]
    config._splice_llm_scalar(lines, "auth_profile", "personal")
    assert lines == ["llm: {}", "llm:", "  auth_profile: personal"]


def test_the_llm_header_pattern_matches_a_comment_but_not_a_value():
    for header in ("llm:", "llm: ", "llm:  # which plan pays", "llm:\t# x"):
        assert config._LLM_HEADER_RE.match(header), header
    for not_header in ("  llm:", "llm: {}", "llmx:", "llm: personal"):
        assert config._LLM_HEADER_RE.match(not_header) is None, not_header


def test_a_splicer_bug_that_duplicates_the_llm_block_is_refused_and_the_file_restored(
    tmp_path, monkeypatch
):
    """Simulates the exact defect this ticket fixes: a splicer that decides
    (wrongly) that no `llm:` block exists and appends a second one. The
    post-write verify must catch the resulting duplicate top-level key and
    restore the file — a resolve-and-compare check alone is not enough,
    because PyYAML resolves the SECOND block and the comparison would pass
    while the operator's subscription pin was just silently dropped."""

    def _buggy_splice(lines, key, value):
        lines.extend(["llm:", f"  {key}: {value}"])

    monkeypatch.setattr(config, "_splice_llm_scalar", _buggy_splice)

    cfg_path = tmp_path / "config.yaml"
    seed = "llm:\n  auth_profile: personal\n  auth_mode: subscription\n"
    cfg_path.write_text(seed)

    with pytest.raises(AuthError, match=r"duplicate top-level key.*'llm'"):
        set_auth_profile("enterprise", cfg_path)

    assert cfg_path.read_text() == seed
    assert load_config(cfg_path).data["llm"]["auth_profile"] == "personal"


def test_set_model_ids_refuses_a_write_that_duplicates_a_top_level_key(
    tmp_path, monkeypatch
):
    def _buggy_splice(lines, key, value):
        lines.extend(["llm:", f"  {key}: {value}"])

    monkeypatch.setattr(config, "_splice_llm_scalar", _buggy_splice)

    cfg_path = tmp_path / "config.yaml"
    seed = "llm:\n  auth_profile: personal\n  auth_mode: subscription\n"
    cfg_path.write_text(seed)

    with pytest.raises(AuthError, match=r"duplicate top-level key.*'llm'"):
        config.set_model_ids({"primary_model": "claude-sonnet-5"}, cfg_path)

    assert cfg_path.read_text() == seed
    assert load_config(cfg_path).data["llm"]["auth_profile"] == "personal"


def test_duplicate_top_level_keys_sees_what_safe_load_hides():
    """`safe_load` cannot answer this question — last-wins IS its contract —
    so the check has to run on the parse tree (`yaml.compose`), not on the
    dict `safe_load` hands back. Asserted here so the check is not vacuous:
    the ordinary instrument really does hide what we are looking for."""
    dup_text = "llm:\n  a: 1\nserver:\n  port: 1\nllm:\n  b: 2\n"
    assert config._duplicate_top_level_keys(dup_text) == ["llm"]
    assert config._duplicate_top_level_keys("llm:\n  a: 1\nserver:\n  port: 1\n") == []
    # A key repeated at a NESTED level, under two distinct top-level parents,
    # is not a top-level duplicate.
    assert config._duplicate_top_level_keys("a:\n  x: 1\nb:\n  x: 2\n") == []

    # Non-vacuity: prove `safe_load` really does hide the second block.
    assert yaml.safe_load(dup_text) == {"llm": {"b": 2}, "server": {"port": 1}}


def test_the_profile_name_pattern_rejects_a_trailing_newline():
    """`$` matches before a trailing newline, `\\Z` does not.

    `validate_profile_name` strips before it matches, so today this pattern is
    never handed a value ending in a newline — the strip is the live guard and
    the anchor is the backstop behind it. Asserted at the pattern because that
    is where the claim lives: the character class says "lowercase letters,
    digits, '-' and '_' only", and a `$` anchor silently exempts one trailing
    newline from that claim. If the strip is ever moved or dropped, this is the
    line that keeps a name with a control character out of the config file.

    Same class as its sibling in tests/test_cli_shell_model.py: both are
    backstops being made to hold on their own, neither is a live exploit.
    """
    assert config._PROFILE_NAME_RE.match("personal2\n") is None


@pytest.mark.parametrize(
    "bad",
    ["personal\nserver:\n  port: 9999", "../etc", "a b", "", "-lead", "x: y"],
)
def test_set_auth_profile_rejects_names_that_could_inject_yaml(tmp_path, bad):
    """The write is a text edit, so the value must not be able to add structure."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("llm:\n  auth_mode: subscription\n")
    before = cfg_path.read_text()
    with pytest.raises(AuthError, match="invalid auth profile name"):
        set_auth_profile(bad, cfg_path)
    assert cfg_path.read_text() == before


# ------------------- attribution: every burn names its payer ---------------- #


class _StubBackend:
    model = "claude-sonnet-5"


def _orchestrator(store, tmp_path, events):
    return Orchestrator(
        store,
        load_config(tmp_path / "config.yaml").data,
        _StubBackend(),
        SlackNotifier(None),
        event_sink=events.append,
    )


async def test_models_event_names_the_paying_subscription(store, tmp_path, monkeypatch):
    """Every burn must be attributable to a subscription — by name, never token."""
    monkeypatch.setattr(config, "_ACTIVE_AUTH_PROFILE", "personal")
    events = []
    orch = _orchestrator(store, tmp_path, events)
    orch._emit_models({"coder": "claude-sonnet-5"})

    (event,) = [e for e in events if e["kind"] == "models"]
    assert event["auth_profile"] == "personal"
    assert "auth=personal" in event["text"]


async def test_models_event_omits_the_profile_when_none_was_exported(
    store, tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "_ACTIVE_AUTH_PROFILE", None)
    events = []
    orch = _orchestrator(store, tmp_path, events)
    orch._emit_models({"coder": "claude-sonnet-5"})

    (event,) = [e for e in events if e["kind"] == "models"]
    assert event["auth_profile"] is None
    assert "auth=" not in event["text"]


async def test_attempt_row_records_the_paying_subscription(store):
    """Reverting the `auth_profile=` stamp on update_attempt leaves this NULL,
    and a burn becomes unattributable after the fact."""
    task = Task.new("t", repo_path="/r")
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)
    await store.update_attempt(attempt_id, auth_profile="enterprise")

    (row,) = await store.list_attempts(task.id)
    assert row["auth_profile"] == "enterprise"


# --------------------------------------------------------------------------- #
# `nh auth set-token` — the CLI twin of the app's File → Re-enter Claude Token #
#                                                                              #
# Until this existed a mistyped token could not be fixed from the CLI at all:  #
# `nh init` short-circuits on the PRESENCE of a credential and never           #
# overwrites one, `nh auth` had only status/use, and `nh config` only          #
# show/edit/path — so the remedy was hand-editing ~/.no_human/.env while the   #
# Mac app had a menu item for it (walkthrough B14b).                           #
# --------------------------------------------------------------------------- #


def _set_token(tmp_path, monkeypatch, stdin: str, *args):
    """Run `nh auth set-token` against a tmp .env. Returns (result, env_path)."""
    from click.testing import CliRunner

    from no_human.cli import commands as cmd_mod

    env_path = tmp_path / ".env"
    cfg = load_config(tmp_path / "config.yaml")
    monkeypatch.setattr(config, "ENV_PATH", env_path)
    monkeypatch.setattr(cmd_mod, "load_config", lambda *a, **k: cfg)
    # Never probe the operator's real server from a unit test.
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: False)
    result = CliRunner().invoke(cmd_mod.auth_set_token, list(args), input=stdin,
                                catch_exceptions=False)
    return result, env_path


def test_set_token_writes_the_active_profiles_variable(tmp_path, monkeypatch):
    """A fresh install: nothing on file, one token in, the canonical variable
    out — and the command names what it wrote so the operator can check it."""
    result, env_path = _set_token(tmp_path, monkeypatch,
                                  "sk-ant-oat01-FRESHTOKEN\n")

    assert result.exit_code == 0, result.output
    assert env_path.read_text() == (
        f"{SUBSCRIPTION_TOKEN_VAR}=sk-ant-oat01-FRESHTOKEN\n")
    assert (env_path.stat().st_mode & 0o777) == 0o600
    assert SUBSCRIPTION_TOKEN_VAR in result.output
    assert "default" in result.output
    assert "FRESHTOKEN" not in result.output, "the token was echoed back"


def test_set_token_replaces_a_token_that_is_already_there(tmp_path, monkeypatch):
    """The whole point: `nh init` refuses to overwrite, so SOMETHING has to.
    The other lines in .env survive — this is the guarded upsert, not a
    rewrite of the file."""
    env_path = tmp_path / ".env"
    env_path.write_text(f"# mine\n{SUBSCRIPTION_TOKEN_VAR}=mistyped\nOTHER=keep\n")

    result, env_path = _set_token(tmp_path, monkeypatch, "corrected-token\n")

    assert result.exit_code == 0, result.output
    assert env_path.read_text() == (
        f"# mine\n{SUBSCRIPTION_TOKEN_VAR}=corrected-token\nOTHER=keep\n")


def test_set_token_writes_a_named_profiles_own_variable(tmp_path, monkeypatch):
    result, env_path = _set_token(tmp_path, monkeypatch, "personal-tok\n",
                                  "--profile", "personal")

    assert result.exit_code == 0, result.output
    assert env_path.read_text() == f"{PERSONAL_VAR}=personal-tok\n"
    assert PERSONAL_VAR in result.output


def test_set_token_refuses_an_api_key_through_the_shared_validator(
        tmp_path, monkeypatch):
    """One opinion about what a usable token is: the same refusal `nh init`,
    `PUT /api/auth/token` and the desktop app all raise."""
    result, env_path = _set_token(tmp_path, monkeypatch,
                                  "sk-ant-api03-WRONGFIELD\n")

    assert result.exit_code == 2, result.output
    assert "not an OAuth token" in result.output, result.output
    assert not env_path.exists(), "a refused token must not be written"


def test_set_token_refuses_an_empty_line(tmp_path, monkeypatch):
    result, env_path = _set_token(tmp_path, monkeypatch, "\n")

    assert result.exit_code == 2, result.output
    assert "must not be empty" in result.output, result.output
    assert not env_path.exists()


def test_set_token_rejects_a_bad_profile_before_reading_the_token(
        tmp_path, monkeypatch):
    """The name is validated FIRST, so a run that was going to fail never has
    the credential in memory at all."""
    result, env_path = _set_token(tmp_path, monkeypatch, "a-token\n",
                                  "--profile", "not a profile!")

    assert result.exit_code == 2, result.output
    assert "invalid auth profile name" in result.output, result.output
    assert not env_path.exists()
    assert "a-token" not in result.output


def test_set_token_defaults_to_the_profile_the_config_pins(tmp_path, monkeypatch):
    """`nh auth use personal` then `nh auth set-token` must write PERSONAL's
    variable — writing `default`'s would silently leave the pinned profile
    unfixed, which is the bug this command exists to remove."""
    (tmp_path / "config.yaml").write_text("llm:\n  auth_profile: personal\n")

    result, env_path = _set_token(tmp_path, monkeypatch, "personal-tok\n")

    assert result.exit_code == 0, result.output
    assert env_path.read_text() == f"{PERSONAL_VAR}=personal-tok\n"
