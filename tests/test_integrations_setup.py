"""The onboarding "Connect your tools" setup surface.

Two things were broken and are covered here:

1. ``teams`` had no ``DEFAULT_CONFIG["integrations"]`` block at all, so no
   config-driven UI could offer it and the only way to reach it was to
   hand-edit YAML.
2. The wizard step configured NOTHING — it listed statuses read-only.

The gate this file exists to hold is the CREDENTIAL one: onboarding writes
``config.yaml`` and only ``config.yaml``, so a field that would put a token in
that world-readable file must be refused. `test_no_discovered_field_is_a
_credential` and `test_apply_setup_refuses_every_credential_shaped_field`
DISCOVER the fields from the defaults rather than listing them, so a NEW
integration block is covered the day it is added — the case a hand-written
list can never catch.

CRITICAL SAFETY: every test monkeypatches no_human.config.CONFIG_PATH and
ENV_PATH onto tmp_path — the real ~/.no_human is never read or written.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

import no_human.config as nh_config
import no_human.integrations as reg
from no_human.api.app import app
from no_human.notify import build_notifier

# Field names that are unambiguously credentials. Used as ATTACK input, never
# as the guard's own definition — the guard derives its answer from FIELD_SPECS
# plus a name pattern, so these are an independent oracle.
CREDENTIAL_ATTEMPTS = [
    "api_token", "api_key", "token", "secret", "password", "webhook_url",
    "access_key", "private_key", "oauth_token", "bot_token", "app_token",
    "signature", "session_cookie", "credentials", "pat",
]


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    for var in ("JIRA_API_TOKEN", "LINEAR_API_KEY", "CIRCLECI_TOKEN",
                "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# --------------------------------------------------------------------------- #
# Gap 1: Teams exists in the default config                                    #
# --------------------------------------------------------------------------- #

def test_teams_has_a_default_config_block():
    blocks = nh_config.DEFAULT_CONFIG["integrations"]
    assert "teams" in blocks, (
        "notify/teams.py is wired into the dispatcher and tested, but with no "
        "config block nothing can offer it to a user"
    )
    assert blocks["teams"]["enabled"] is True, (
        "default must be ON: a False default would silently stop delivery for "
        "every install that already has notifications.teams_webhook_url set"
    )


def test_teams_block_does_not_duplicate_the_webhook_url():
    """One source of truth per setting. The URL stays where build_notifier
    already reads it, and it is a secret besides."""
    assert "webhook_url" not in nh_config.DEFAULT_CONFIG["integrations"]["teams"]
    assert "teams_webhook_url" in nh_config.DEFAULT_CONFIG["notifications"]


def test_every_registry_notification_integration_now_has_a_block():
    """slack and teams are both notify-side registry entries; before this
    change only slack had a config block."""
    blocks = nh_config.DEFAULT_CONFIG["integrations"]
    assert {"jira", "linear", "slack", "teams"} <= set(blocks)


def test_the_ci_backends_have_no_integrations_block_so_the_wizard_cannot_lie():
    """github / gitlab / jenkins / circleci are views over the single `ci.*`
    section, so none of them may own an `integrations.<name>` block.

    `setup_specs` discovers the wizard's forms from these blocks and
    `enable_field` derives an on/off toggle from them. CircleCI had one holding
    `enabled` + `org_slug` + `project` that nothing read: the wizard rendered a
    form and a switch that changed nothing, while the Settings panel told the
    operator CircleCI was their active CI backend and no gate ran. A block here
    for any of the four is that defect coming back.
    """
    from no_human.integrations import _CI_BACKEND_BY_NAME, enable_field

    blocks = set(nh_config.DEFAULT_CONFIG["integrations"])
    assert _CI_BACKEND_BY_NAME, "non-vacuity: the CI backend map is empty"
    assert not (set(_CI_BACKEND_BY_NAME) & blocks), (
        f"CI backends own an integrations block: "
        f"{sorted(set(_CI_BACKEND_BY_NAME) & blocks)}")
    for name in _CI_BACKEND_BY_NAME:
        assert enable_field(name) is None, (
            f"{name} renders an on/off switch that governs nothing — "
            "`ci.enabled` is the only switch a CI backend has")


@pytest.mark.parametrize("enabled,expect", [(True, True), (False, False), (None, True)])
def test_teams_enabled_is_a_mute_switch_honoured_by_the_dispatcher(enabled, expect):
    """`integrations.teams.enabled` is observed, not decoration. None = the
    key is absent entirely (a config that predates it), which must behave
    exactly as before."""
    cfg = {"notifications": {"teams_webhook_url":
                             "https://x.logic.azure.com/workflows/a?sig=s"}}
    if enabled is not None:
        cfg["integrations"] = {"teams": {"enabled": enabled}}
    teams = dict(build_notifier(cfg).channels)["teams"]
    assert teams.enabled is expect


def test_teams_mute_switch_survives_a_null_integrations_section():
    """Deep-merge shadowing trap: `integrations:` set to null must not crash
    the notifier and must not mute the channel."""
    n = build_notifier({"integrations": None,
                        "notifications": {"teams_webhook_url": "https://x/y?sig=s"}})
    assert dict(n.channels)["teams"].enabled is True
    # `teams:` present but null, and `enabled:` present but null, are the same
    # trap one level down — neither may silently mute a configured channel.
    for shadowed in ({"teams": None}, {"teams": {"enabled": None}}):
        n = build_notifier({"integrations": shadowed,
                            "notifications": {"teams_webhook_url": "https://x/y?sig=s"}})
        assert dict(n.channels)["teams"].enabled is True, shadowed


# --------------------------------------------------------------------------- #
# Discovery: the wizard is generated from the defaults, not from a name list   #
# --------------------------------------------------------------------------- #

def test_setup_specs_cover_exactly_the_default_config_blocks():
    names = [s["name"] for s in reg.setup_specs({})]
    assert names == list(nh_config.DEFAULT_CONFIG["integrations"])


def test_a_new_integration_block_appears_with_no_ui_change(monkeypatch):
    """The requirement in one test: a SIXTH integration must show up in the
    wizard without anyone editing the UI or this module."""
    blocks = dict(nh_config.DEFAULT_CONFIG["integrations"])
    blocks["pagerduty"] = {"enabled": False, "service_key_id": "", "routing": ""}
    merged = dict(nh_config.DEFAULT_CONFIG)
    merged["integrations"] = blocks
    monkeypatch.setattr(nh_config, "DEFAULT_CONFIG", merged)

    specs = {s["name"]: s for s in reg.setup_specs({})}
    assert "pagerduty" in specs
    new = specs["pagerduty"]
    assert new["enable_field"] == "enabled"
    assert [f["name"] for f in new["fields"]] == ["enabled", "service_key_id", "routing"]
    assert [f["label"] for f in new["fields"]] == ["Enabled", "Service key id", "Routing"]


def test_labels_agree_with_the_settings_panel_where_both_describe_a_field():
    """The wizard and Settings → Integrations must not name the same setting
    two different ways ("Site URL" vs "Site"). FIELD_SPECS' label wins when it
    has one; a generated label is the fallback, not the default."""
    jira = {s["name"]: s for s in reg.setup_specs({})}["jira"]
    labels = {f["name"]: f["label"] for f in jira["fields"]}
    assert labels["site"] == "Site URL"
    assert labels["jql"] == "JQL filter"
    assert labels["project_key"] == "Project key"
    # No FieldSpec describes `write_back`, so it falls back to generated.
    assert labels["write_back"] == "Write back"


def test_setup_spec_names_the_env_var_but_never_a_value(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_SUPERSECRET")
    (tmp_path / ".env").write_text("LINEAR_API_KEY=lin_api_SUPERSECRET\n")
    specs = {s["name"]: s for s in reg.setup_specs({})}
    linear = specs["linear"]
    assert [s["env_var"] for s in linear["secrets"]] == ["LINEAR_API_KEY"]
    assert linear["secrets"][0]["set"] is True
    assert "SUPERSECRET" not in str(specs)


def test_teams_secret_note_points_at_settings_not_at_a_config_field():
    """Teams' webhook is a secret that the code reads from config.yaml. The
    wizard must NOT collect it — it must say where it goes instead."""
    teams = {s["name"]: s for s in reg.setup_specs({})}["teams"]
    assert teams["secrets"] == []
    assert "Settings" in teams["secret_note"]
    assert [f["name"] for f in teams["fields"]] == ["enabled"]


# --------------------------------------------------------------------------- #
# The credential gate                                                          #
# --------------------------------------------------------------------------- #

def test_no_discovered_field_is_a_credential():
    """Every field the wizard offers, for every integration that exists —
    discovered, so a new block is covered the day it lands."""
    secret_paths = {
        s.config_path
        for specs in reg.FIELD_SPECS.values() for s in specs
        if s.secret and s.config_path
    }
    for spec in reg.setup_specs({}):
        for f in spec["fields"]:
            dotted = f"integrations.{spec['name']}.{f['name']}"
            assert not reg.is_credential_name(f["name"]), (
                f"{dotted} would persist a credential into config.yaml")
            assert dotted not in secret_paths, (
                f"{dotted} is declared secret by FIELD_SPECS")


@pytest.mark.parametrize("field", CREDENTIAL_ATTEMPTS)
def test_apply_setup_refuses_every_credential_shaped_field(field):
    """Cross-product: every integration that exists × every credential-shaped
    field name. Nothing may be written and config.yaml must not appear."""
    for name in nh_config.DEFAULT_CONFIG["integrations"]:
        # Refused BY THE CREDENTIAL RULE — not incidentally, by the
        # "unknown setting" check that would also reject it. See
        # assert_config_safe_field's note on why that ordering matters.
        with pytest.raises(ValueError, match="credential"):
            reg.apply_setup(name, {field: "SUPERSECRET-VALUE"})
    assert not nh_config.CONFIG_PATH.exists()


def test_apply_setup_refusal_leaves_an_existing_config_byte_identical():
    reg.apply_setup("linear", {"enabled": True, "team_key": "ENG"})
    before = nh_config.CONFIG_PATH.read_bytes()
    with pytest.raises(ValueError):
        reg.apply_setup("linear", {"team_key": "ENG2", "api_key": "lin_api_LEAK"})
    assert nh_config.CONFIG_PATH.read_bytes() == before, (
        "validation must happen before ANY write — a refused field left the "
        "file half-updated")
    assert b"LEAK" not in before


def test_setting_names_that_are_not_credentials_still_pass():
    """The guard must not be so broad it blocks real settings — `team_key`
    and `project_key` end in `_key` and are not credentials."""
    assert reg.is_credential_name("team_key") is False
    assert reg.is_credential_name("project_key") is False
    assert reg.is_credential_name("default_repo") is False
    reg.assert_config_safe_field("linear", "team_key")
    reg.assert_config_safe_field("jira", "project_key")


def test_a_new_block_that_ships_a_credential_key_is_refused_not_offered(monkeypatch):
    """The case the credential guard actually exists for, and the one a
    hand-written list of field names can never catch: someone adds a NEW
    integration block and puts a token key in it. The wizard must neither
    render it nor write it — even though it IS a legitimate key of that
    block, so the "unknown field" check does not fire."""
    blocks = dict(nh_config.DEFAULT_CONFIG["integrations"])
    blocks["pagerduty"] = {
        "enabled": False,
        "service": "",
        "api_token": "",        # the mistake
        "routing_key": "",      # ...and a subtler one
    }
    merged = dict(nh_config.DEFAULT_CONFIG)
    merged["integrations"] = blocks
    monkeypatch.setattr(nh_config, "DEFAULT_CONFIG", merged)

    offered = {f["name"] for f in
               {s["name"]: s for s in reg.setup_specs({})}["pagerduty"]["fields"]}
    assert offered == {"enabled", "service"}, (
        "a credential key of a new block must never be rendered as a field")

    for field in ("api_token", "routing_key"):
        with pytest.raises(ValueError, match="credential"):
            reg.apply_setup("pagerduty", {field: "SUPERSECRET-VALUE"})
    assert not nh_config.CONFIG_PATH.exists()

    # ...while the non-credential keys of the same block still work.
    reg.apply_setup("pagerduty", {"enabled": True, "service": "svc"})
    on_disk = nh_config.CONFIG_PATH.read_text()
    assert "svc" in on_disk
    assert "SUPERSECRET" not in on_disk


def test_the_wizard_offers_state_types_and_refuses_a_typod_one():
    """`state_types` IS offered by the wizard (it is a key of the DEFAULT
    block, so `_setup_fields` discovers it as a comma list) — what was missing
    was any check that the strings typed into it are state types. Coercion
    only ever asked "is this a list of strings?", and Linear answers a
    nonexistent state type with an empty page rather than an error, so a typo
    went to disk and polled nothing forever."""
    linear = {s["name"]: s for s in reg.setup_specs({})}["linear"]
    field = {f["name"]: f for f in linear["fields"]}["state_types"]
    assert field["kind"] == "list"
    assert field["value"] == ["triage", "backlog", "unstarted"]

    with pytest.raises(ValueError, match="state_types"):
        reg.apply_setup("linear", {"team_key": "NO", "state_types": ["Backlog"]})
    assert not nh_config.CONFIG_PATH.exists(), (
        "a refused value must leave config.yaml untouched — the whole call is "
        "validated before anything is written")


def test_the_wizard_still_accepts_every_real_state_type():
    """The control: the guard must not block the values the product ships."""
    from no_human.intake.linear import STATE_TYPES

    reg.apply_setup("linear", {"enabled": True, "team_key": "NO",
                               "state_types": list(STATE_TYPES)})
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["state_types"] == list(STATE_TYPES)


def test_a_typod_state_type_leaves_an_existing_config_byte_identical():
    reg.apply_setup("linear", {"enabled": True, "team_key": "NO"})
    before = nh_config.CONFIG_PATH.read_bytes()
    with pytest.raises(ValueError, match="state_types"):
        reg.apply_setup("linear", {"team_key": "OTHER", "state_types": ["nope"]})
    assert nh_config.CONFIG_PATH.read_bytes() == before, (
        "the team_key in the same call landed on disk while the state_types "
        "beside it was refused")


def test_apply_setup_refuses_an_unknown_field():
    with pytest.raises(ValueError):
        reg.apply_setup("linear", {"not_a_setting": "x"})


# --------------------------------------------------------------------------- #
# What the operator actually gets: an ENABLED integration and no secret        #
# --------------------------------------------------------------------------- #

def test_wizard_result_is_an_enabled_working_linear_config_with_no_secret():
    reg.apply_setup("linear", {"enabled": True, "team_key": "ENG", "label": "bug"})

    text = nh_config.CONFIG_PATH.read_text()
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    lin = cfg["integrations"]["linear"]

    # 1. It is ON, by the EXACT predicate `nh serve` gates the poller on
    #    (cli/commands.py: `linear_cfg.get("enabled")`).
    assert ((cfg.get("integrations") or {}).get("linear") or {}).get("enabled") is True
    # 2. It carries the settings the poller needs.
    assert lin["team_key"] == "ENG"
    assert lin["label"] == "bug"
    # 3. And no credential landed in the world-readable file.
    assert "LINEAR_API_KEY" not in text
    assert "api_key" not in text
    assert "lin_api" not in text


def test_wizard_result_turns_jira_on_too():
    reg.apply_setup("jira", {"enabled": True, "site": "https://acme.atlassian.net",
                             "project_key": "PROJ", "email": "me@x.com"})
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["jira"]["enabled"] is True
    assert cfg["integrations"]["jira"]["project_key"] == "PROJ"
    assert "JIRA_API_TOKEN" not in nh_config.CONFIG_PATH.read_text()


def test_apply_setup_does_not_write_the_env_file_at_all():
    reg.apply_setup("linear", {"enabled": True, "team_key": "ENG"})
    assert not nh_config.ENV_PATH.exists(), (
        "the onboarding route is config-only; it must never touch the "
        "credential store")


def test_apply_setup_preserves_unrelated_config_keys():
    nh_config.CONFIG_PATH.write_text("llm:\n  auth_mode: subscription\n")
    reg.apply_setup("linear", {"enabled": True, "team_key": "ENG"})
    import yaml
    on_disk = yaml.safe_load(nh_config.CONFIG_PATH.read_text())
    assert on_disk["llm"]["auth_mode"] == "subscription"
    assert on_disk["integrations"]["linear"]["team_key"] == "ENG"


def test_list_field_accepts_a_comma_string_and_a_list():
    reg.apply_setup("linear", {"state_types": "triage, backlog"})
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["state_types"] == ["triage", "backlog"]
    reg.apply_setup("linear", {"state_types": ["unstarted"]})
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["state_types"] == ["unstarted"]


def test_multiline_text_value_is_refused():
    with pytest.raises(ValueError):
        reg.apply_setup("linear", {"team_key": "ENG\nother: value"})
    assert not nh_config.CONFIG_PATH.exists()


# --------------------------------------------------------------------------- #
# Honest enabled/disabled state (Settings and the wizard agree with reality)   #
# --------------------------------------------------------------------------- #

def test_status_reports_configured_but_off_honestly():
    """The "I don't have Linear" report: every setting filled in, poller never
    starts, panel said "Configured". `enabled` now says so."""
    cfg = {"integrations": {"linear": {"team_key": "ENG", "enabled": False}}}
    st = {s.name: s for s in reg.list_integrations(cfg)}
    assert st["linear"].configured is True
    assert st["linear"].enabled is False


def test_status_enabled_is_none_for_integrations_with_no_switch():
    st = {s.name: s for s in reg.list_integrations({})}
    for name in ("github", "gitlab", "jenkins"):
        assert st[name].enabled is None, "these are views over ci.*, not own blocks"
    assert st["teams"].enabled is True     # default-on mute switch
    assert st["slack"].enabled is False    # `intake`, default off
    assert st["jira"].enabled is False


def test_enable_field_is_discovered_not_named():
    assert reg.enable_field("jira") == "enabled"
    assert reg.enable_field("linear") == "enabled"
    assert reg.enable_field("teams") == "enabled"
    # slack's switch is its single boolean, `intake` — found, not hardcoded.
    assert reg.enable_field("slack") == "intake"
    assert reg.enable_field("github") is None


def test_setup_specs_report_whether_the_switch_ships_on():
    """`enable_default` lets the UI tell a ships-ON mute switch (teams) from an
    opt-in one (everything else): the wizard must not present a mute switch's
    raw `True` as "already on" before the channel is configured."""
    specs = {s["name"]: s for s in reg.setup_specs({})}
    for name in ("jira", "linear", "monday", "slack"):
        assert specs[name]["enable_default"] is False, name
    assert specs["teams"]["enable_default"] is True


def test_slack_switch_is_intake_and_ships_off():
    """slack.enabled does not exist; its switch is `intake`, and it defaults
    False like every other opt-in switch. The `enabled: None` tri-state
    belongs only to the ci.* views (github/gitlab/jenkins/circleci), which
    have no switch of their own — never to slack."""
    assert reg.enable_field("slack") == "intake"
    assert reg.enable_default("slack") is False
    assert reg.enable_state({}, "slack") is False
    st = {s.name: s for s in reg.list_integrations({})}
    none_named = {name for name, s in st.items() if s.enabled is None}
    assert none_named == {"github", "gitlab", "jenkins", "circleci"}
    assert st["slack"].enabled is False


def test_upgrade_with_webhook_and_enabled_true_still_notifies():
    """Control: an existing install with a pasted webhook and the mute switch
    left on (or explicitly True) keeps notifying — the upgrade contract this
    task must not touch."""
    cfg = {
        "notifications": {
            "teams_webhook_url":
                "https://prod-27.westus.logic.azure.com:443/workflows/abc/"
                "triggers/manual/paths/invoke?sig=SECRETSIG",
        },
        "integrations": {"teams": {"enabled": True}},
    }
    teams = dict(build_notifier(cfg).channels)["teams"]
    assert teams.webhook_url == cfg["notifications"]["teams_webhook_url"]
    assert teams.enabled is True


def test_upgrade_with_webhook_and_enabled_false_is_muted():
    """Control: the same upgrade scenario with the mute switch flipped off
    suppresses delivery — the webhook is kept, not deleted, but nothing sends."""
    cfg = {
        "notifications": {
            "teams_webhook_url":
                "https://prod-27.westus.logic.azure.com:443/workflows/abc/"
                "triggers/manual/paths/invoke?sig=SECRETSIG",
        },
        "integrations": {"teams": {"enabled": False}},
    }
    teams = dict(build_notifier(cfg).channels)["teams"]
    assert teams.webhook_url is None
    assert teams.enabled is False


# --------------------------------------------------------------------------- #
# API surface                                                                  #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def client(store):
    app.state.store = store
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost",
                           headers={"Origin": "http://127.0.0.1:8420"}) as c:
        yield c


@pytest.mark.asyncio
async def test_get_setup_lists_every_default_block(client):
    r = await client.get("/api/integrations/setup")
    assert r.status_code == 200, r.text
    names = [s["name"] for s in r.json()["integrations"]]
    assert names == list(nh_config.DEFAULT_CONFIG["integrations"])
    assert "teams" in names


@pytest.mark.asyncio
async def test_put_setup_enables_linear_and_writes_no_secret(client):
    r = await client.put("/api/integrations/linear/setup",
                         json={"values": {"enabled": True, "team_key": "ENG"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "linear"
    assert body["enabled"] is True
    assert body["configured"] is True

    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["enabled"] is True
    assert "LINEAR_API_KEY" not in nh_config.CONFIG_PATH.read_text()
    # And app.state was refreshed, so the next GET agrees.
    r2 = await client.get("/api/integrations/setup")
    linear = {s["name"]: s for s in r2.json()["integrations"]}["linear"]
    assert linear["enabled"] is True
    assert {f["name"]: f["value"] for f in linear["fields"]}["team_key"] == "ENG"


@pytest.mark.asyncio
async def test_put_setup_rejects_a_credential_with_422(client):
    r = await client.put("/api/integrations/linear/setup",
                         json={"values": {"api_key": "lin_api_LEAKME"}})
    assert r.status_code == 422, r.text
    assert "LEAKME" not in r.text
    # load_config materialises a FULL default config.yaml (yaml.safe_dump of
    # DEFAULT_CONFIG), unlike reg.apply_setup which splices only touched keys.
    # A whole-file `"api_key" not in text` was true only by accident: it broke
    # the moment an unrelated default key (llm.codex_auth_mode: "api_key")
    # started existing, in a section this endpoint never writes. What this
    # test actually guards is that a refused credential-shaped field never
    # reaches the integrations.linear section — so assert on the parsed
    # structure, not the raw text of an unrelated part of the file.
    text = nh_config.CONFIG_PATH.read_text()
    assert "LEAKME" not in text
    assert "lin_api" not in text
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    linear = (cfg.get("integrations") or {}).get("linear") or {}
    assert "api_key" not in linear, linear
    assert not [k for k in linear if k in CREDENTIAL_ATTEMPTS], linear


@pytest.mark.asyncio
async def test_put_setup_unknown_integration_422(client):
    r = await client.put("/api/integrations/mystery/setup",
                         json={"values": {"enabled": True}})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_setup_requires_a_local_origin(client):
    r = await client.put("/api/integrations/linear/setup",
                         json={"values": {"enabled": True}},
                         headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403, r.text
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["enabled"] is False


@pytest.mark.asyncio
async def test_list_integrations_endpoint_reports_enabled(client):
    await client.put("/api/integrations/linear/setup",
                     json={"values": {"enabled": True, "team_key": "ENG"}})
    r = await client.get("/api/integrations")
    by_name = {s["name"]: s for s in r.json()["integrations"]}
    assert by_name["linear"]["enabled"] is True
    assert by_name["github"]["enabled"] is None


# --------------------------------------------------------------------------- #
# C2.1 — REAL, fail-closed probes for github / gitlab / jenkins.               #
#                                                                              #
# No respx in deps, so `httpx.AsyncClient` is monkeypatched to one built on    #
# `httpx.MockTransport(handler)`: the per-test handler maps a request to a     #
# response WITHOUT any real network, and can raise `httpx.ConnectError` to     #
# exercise the fail-closed path (a probe that cannot reach the network is      #
# NEVER healthy).                                                              #
# --------------------------------------------------------------------------- #

@pytest.fixture
def mock_http(monkeypatch):
    """Install a request handler that every `httpx.AsyncClient` in reg's probes
    routes through — no socket is ever opened. Returns the installer."""
    import httpx

    def install(handler):
        real = httpx.AsyncClient
        transport = httpx.MockTransport(handler)

        def factory(*args, **kwargs):
            kwargs.setdefault("transport", transport)
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    return install


_GH_CFG = {"ci": {"enabled": True, "backend": "github_actions", "project": "o/r"}}


async def test_github_probe_healthy_on_200_user_and_repo(mock_http, monkeypatch):
    import httpx
    monkeypatch.setenv("GITHUB_TOKEN", "gh-should-not-leak")

    def handler(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "amit"})
        if request.url.path == "/repos/o/r":
            return httpx.Response(200, json={"full_name": "o/r"})
        return httpx.Response(404)

    mock_http(handler)
    s = await reg.test_integration("github", _GH_CFG)
    assert s.healthy is True, s.detail
    assert "connected as amit" in s.detail
    assert "o/r" in s.detail
    assert "gh-should-not-leak" not in s.detail  # never echoed


async def test_github_probe_401_is_unhealthy(mock_http, monkeypatch):
    import httpx
    monkeypatch.setenv("GITHUB_TOKEN", "bad")

    def handler(request):
        return httpx.Response(401, json={"message": "Bad credentials"})

    mock_http(handler)
    s = await reg.test_integration("github", _GH_CFG)
    assert s.healthy is False
    assert "401" in s.detail


async def test_network_error_is_not_verified(mock_http, monkeypatch):
    # FAIL-CLOSED: a probe that cannot reach the network is NOT healthy, and
    # says so — never a green from a request that never completed.
    import httpx
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    def handler(request):
        raise httpx.ConnectError("no route to host")

    mock_http(handler)
    s = await reg.test_integration("github", _GH_CFG)
    assert s.healthy is False
    assert "not verified" in s.detail
    assert "ConnectError" in s.detail


async def test_linear_wrong_team_key_lists_available(mock_http, monkeypatch):
    # The key authenticates, but the configured team_key names no team — the
    # detail must NAME the teams this key can actually see, so the operator can
    # fix the setting instead of guessing.
    import json as _json
    import httpx
    monkeypatch.setenv("LINEAR_API_KEY", "k")

    def handler(request):
        q = _json.loads(request.content).get("query", "")
        if "teams" in q:
            return httpx.Response(200, json={"data": {"teams": {"nodes": [
                {"key": "ENG"}, {"key": "OPS"}]}}})
        return httpx.Response(200, json={"data": {"viewer": {"id": "u", "name": "Dana"}}})

    mock_http(handler)
    s = await reg.test_integration("linear", {"integrations": {"linear": {"team_key": "ABC"}}})
    assert s.healthy is False
    assert "ABC" in s.detail
    assert "ENG" in s.detail and "OPS" in s.detail


async def test_gitlab_probe_healthy_on_200_user(mock_http, monkeypatch):
    import httpx
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-x")

    def handler(request):
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json={"username": "amit"})
        return httpx.Response(404)

    mock_http(handler)
    cfg = {"ci": {"enabled": True, "backend": "gitlab", "project": "g/p",
                  "hostname": "gitlab.com"}}
    s = await reg.test_integration("gitlab", cfg)
    assert s.healthy is True, s.detail
    assert "amit" in s.detail


async def test_jenkins_probe_healthy_on_200_and_fails_closed(mock_http, monkeypatch):
    import httpx
    monkeypatch.setenv("JENKINS_USER", "amit")
    monkeypatch.setenv("JENKINS_API_TOKEN", "jk-tok")

    def handler(request):
        if request.url.path.endswith("/api/json"):
            return httpx.Response(200, json={"mode": "NORMAL"})
        return httpx.Response(404)

    mock_http(handler)
    cfg = {"ci": {"enabled": True, "backend": "jenkins", "job": "job/build",
                  "base_url": "https://ci.example.com"}}
    s = await reg.test_integration("jenkins", cfg)
    assert s.healthy is True, s.detail
    assert "jk-tok" not in s.detail

    # And fail-closed on a transport error.
    def boom(request):
        raise httpx.ConnectTimeout("slow")

    mock_http(boom)
    s2 = await reg.test_integration("jenkins", cfg)
    assert s2.healthy is False
    assert "not verified" in s2.detail


# --------------------------------------------------------------------------- #
# C2.2 — "Ready" only after a passing test: setup_specs carries a `verified`   #
# flag from integrations.<name>.last_verified_at, and mark_verified persists   #
# it on a passing /test.                                                       #
# --------------------------------------------------------------------------- #

def test_setup_specs_report_verified_from_last_verified_at():
    cfg = {"integrations": {"linear": {"team_key": "ENG",
                                       "last_verified_at": "2026-08-27T00:00:00Z"}}}
    specs = {s["name"]: s for s in reg.setup_specs(cfg)}
    assert specs["linear"]["verified"] is True
    # An integration that has never passed a test is not verified.
    assert specs["jira"]["verified"] is False


def test_mark_verified_persists_for_a_tracker_and_refuses_a_ci_view():
    ts = reg.mark_verified("linear")
    assert ts
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["last_verified_at"] == ts
    specs = {s["name"]: s for s in reg.setup_specs(cfg)}
    assert specs["linear"]["verified"] is True
    # github/gitlab/jenkins/circleci are ci.* views with no integrations block,
    # so there is nowhere to persist a verified flag and nothing reads one.
    assert reg.mark_verified("github") is None
    cfg2 = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert "github" not in (cfg2.get("integrations") or {})
    # slack/teams DO have config blocks, but their check is view-only (a webhook
    # present, never delivered), so a healthy result is "saved", not "verified":
    # mark_verified refuses them too, so they can never earn a green "Ready".
    assert reg.mark_verified("slack") is None
    assert reg.mark_verified("teams") is None
    cfg3 = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert "last_verified_at" not in (cfg3["integrations"].get("slack") or {})
    assert "last_verified_at" not in (cfg3["integrations"].get("teams") or {})


# --------------------------------------------------------------------------- #
# C3 — "Run tasks in repo": default_repo is a select over the operator's       #
# registered repo profiles, validated server-side to be one of them.           #
# --------------------------------------------------------------------------- #

def test_default_repo_is_a_repo_select_over_registered_profiles():
    repos = ["/repos/proj1", "/repos/proj2"]
    specs = {s["name"]: s for s in reg.setup_specs({"integrations": {"linear": {}}},
                                                    repos=repos)}
    fields = {f["name"]: f for f in specs["linear"]["fields"]}
    assert fields["default_repo"]["kind"] == "repo_select"
    assert fields["default_repo"]["options"] == repos
    assert fields["default_repo"]["label"] == "Run tasks in repo"


def test_apply_setup_refuses_an_unregistered_default_repo():
    with pytest.raises(reg.RepoNotRegistered):
        reg.apply_setup("linear", {"default_repo": "/not/registered"},
                        repos=["/repos/proj"])
    # A registered path is accepted, and clearing the field ("") is always fine.
    reg.apply_setup("linear", {"default_repo": "/repos/proj"}, repos=["/repos/proj"])
    reg.apply_setup("linear", {"default_repo": ""}, repos=["/repos/proj"])
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert cfg["integrations"]["linear"]["default_repo"] == ""


@pytest.mark.asyncio
async def test_put_setup_unregistered_default_repo_is_400(client):
    # No profiles registered → any non-empty default_repo is unregistered.
    r = await client.put("/api/integrations/linear/setup",
                         json={"values": {"default_repo": "/nope"}})
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_put_setup_accepts_a_registered_default_repo(client, store):
    from no_human.profile import ProjectProfile
    await store.upsert_profile(ProjectProfile(repo_path="/repos/proj", ecosystem="node"))
    r = await client.put("/api/integrations/linear/setup",
                         json={"values": {"default_repo": "/repos/proj"}})
    assert r.status_code == 200, r.text
    fields = {f["name"]: f for f in r.json()["fields"]}
    assert fields["default_repo"]["value"] == "/repos/proj"
    assert fields["default_repo"]["options"] == ["/repos/proj"]


# --------------------------------------------------------------------------- #
# Review fixes                                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_test_endpoint_refuses_a_cross_origin_call(client):
    # CSRF: POST /test loads .env secrets, fires authenticated OUTBOUND calls
    # with the operator's tokens, and writes config.yaml on a pass. The app is
    # unauthenticated, so — like every sibling write route —
    # it must refuse a cross-origin caller, or a page the operator merely visits
    # could drive a probe with their token and read back the VCS user/project.
    r = await client.post("/api/integrations/github/test",
                          headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_slack_view_check_healthy_never_earns_verified(client):
    # slack's check is view-only: it reports healthy when a webhook string is
    # present, without delivering anything. That must NOT flip it to a green
    # "Ready" — last_verified_at is never written, so setup_specs.verified stays
    # False. (The same holds for teams; slack is the representative case.)
    import yaml
    nh_config.CONFIG_PATH.write_text(yaml.safe_dump(
        {"notifications": {"slack_webhook_url": "https://hooks.slack.com/services/T/B/x"}}))
    app.state.config = nh_config.load_config(nh_config.CONFIG_PATH)

    r = await client.post("/api/integrations/slack/test")
    assert r.status_code == 200, r.text
    assert r.json()["healthy"] is True   # the view-check reports healthy...

    # ...but that is config-presence, not a delivery test — no verified flag.
    specs = {s["name"]: s
             for s in reg.setup_specs(nh_config.load_config(nh_config.CONFIG_PATH).data)}
    assert specs["slack"]["verified"] is False
    cfg = nh_config.load_config(nh_config.CONFIG_PATH).data
    assert "last_verified_at" not in (cfg["integrations"].get("slack") or {})
