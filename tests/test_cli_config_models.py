"""Model picker part 2 of 3: ``nh config models`` / ``nh config models set``.

These exercise the CLI twin of `PUT /api/config/models` (tests/test_api_models.py)
— both call the exact same `core.model_settings.apply_model_changes` /
`models_payload` functions, so this file mostly re-proves the same acceptance
surface through the other front door, plus one test that proves the two
front doors are byte-identical on the wire (the "single shared code path"
requirement).
"""

from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner

import no_human.config as nh_config
from no_human.cli.commands import cli
from no_human.core import model_catalog as mc
from no_human.core.db import Store

pytestmark = pytest.mark.usefixtures("isolated_env_file")


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(nh_config, "CONFIG_PATH", path)
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    # Seed with the shipped defaults, but redirect `database.path` under
    # tmp_path too — DEFAULT_CONFIG otherwise bakes in the operator's REAL
    # ~/.no_human/no_human.db, and a `models set` write's audit event would
    # land there instead of a throwaway test db.
    import yaml as _yaml

    data = _yaml.safe_load(_yaml.safe_dump(nh_config.DEFAULT_CONFIG, sort_keys=False))
    data["database"] = {"path": str(tmp_path / "test.db")}
    path.write_text(_yaml.safe_dump(data, sort_keys=False))
    assert nh_config.load_config(path).db_path == tmp_path / "test.db"
    return path


@pytest.fixture
def runner():
    return CliRunner()


#: A hand-written config.yaml with real structure — a header comment, an
#: inline comment, a comment inside the `llm:` block, and unrelated
#: `server:`/`git:` sections — mirroring
#: tests/test_auth_profiles.py::test_set_auth_profile_preserves_comments_and_round_trips.
#: `cfg_path` above seeds via a comment-free `yaml.safe_dump`, so
#: `test_models_set_preserves_config_yaml_structure` cannot tell a
#: comment-preserving splice from a `yaml.safe_load`/`yaml.safe_dump`
#: round-trip — neither has any comments to lose. This seed does. `{db_path}`
#: is filled in per-test so the audit event still lands under `tmp_path`.
_COMMENTED_CONFIG = (
    "# no_human config.yaml — hand-edited, keep these comments.\n"
    "server:\n"
    "  host: 127.0.0.1\n"
    "  port: 8420\n"
    "llm:\n"
    "  # Billing mode: subscription bills CLAUDE_CODE_OAUTH_TOKEN.\n"
    "  auth_mode: subscription\n"
    "  primary_model: claude-sonnet-5\n"
    "  review_model: claude-opus-4-8  # reviewer tier — do not change lightly\n"
    "  planner_model: claude-opus-5\n"
    "  supervisor_model: claude-sonnet-5\n"
    "  utility_model: claude-haiku-4-5\n"
    "database:\n"
    "  path: {db_path}\n"
    "git:\n"
    "  branch_prefix: no-human/\n"
)


# --------------------------------------------------------------------------- #
# nh config models (list)                                                     #
# --------------------------------------------------------------------------- #


def test_models_list_shows_all_five_roles_and_current_values(runner, cfg_path):
    result = runner.invoke(cli, ["config", "models"], catch_exceptions=False)
    assert result.exit_code == 0
    defaults = mc.defaults()
    for role, key in mc.ROLES.items():
        assert role in result.output
        assert defaults[key] in result.output


def test_models_list_shows_every_catalog_option_id(runner, cfg_path):
    result = runner.invoke(cli, ["config", "models"], catch_exceptions=False)
    assert result.exit_code == 0
    for role in mc.ROLES:
        for opt in mc.options_for(role):
            assert opt.id in result.output


# --------------------------------------------------------------------------- #
# nh config models set <role> <id> — refusals                                 #
# --------------------------------------------------------------------------- #


def test_models_set_unknown_role_is_a_cli_error(runner, cfg_path):
    result = runner.invoke(cli, ["config", "models", "set", "bogus", "claude-opus-5"])
    assert result.exit_code != 0
    assert "unknown role" in result.output


def test_models_set_bad_usage_is_a_cli_error(runner, cfg_path):
    result = runner.invoke(cli, ["config", "models", "set", "coder"])
    assert result.exit_code != 0
    assert "usage" in result.output.lower()


def test_models_set_vendor_pin_refusal(runner, cfg_path):
    result = runner.invoke(cli, ["config", "models", "set", "reviewer", "gpt-5.4"])
    assert result.exit_code != 0
    assert "not a Claude model" in result.output


def test_models_set_unpriced_id_refusal(runner, cfg_path):
    result = runner.invoke(
        cli, ["config", "models", "set", "coder", "totally-made-up-model"])
    assert result.exit_code != 0
    assert "no published price" in result.output


def test_models_set_review_gate_collision_refusal(runner, cfg_path):
    defaults = mc.defaults()
    result = runner.invoke(
        cli, ["config", "models", "set", "coder", defaults["review_model"]])
    assert result.exit_code != 0
    assert "different model" in result.output


def test_models_set_coder_backend_refusal_for_priced_non_claude_id(runner, cfg_path):
    """gpt-5.4 is priced, so validate() passes — this must be caught by the
    NEW backend rule, not the price rule (same ordering the API pins)."""
    result = runner.invoke(cli, ["config", "models", "set", "coder", "gpt-5.4"])
    assert result.exit_code != 0
    assert "worker.backend" in result.output
    assert "llm.codex_model" in result.output
    assert "llm.local_model" in result.output
    assert "no published price" not in result.output


def test_models_set_refusal_does_not_write_to_disk(runner, cfg_path):
    before = cfg_path.read_text(encoding="utf-8")
    result = runner.invoke(cli, ["config", "models", "set", "coder", "gpt-5.4"])
    assert result.exit_code != 0
    assert cfg_path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# nh config models set <role> <id> — success                                  #
# --------------------------------------------------------------------------- #


def test_models_set_writes_to_disk(runner, cfg_path):
    result = runner.invoke(
        cli, ["config", "models", "set", "utility", "claude-opus-5"],
        catch_exceptions=False)
    assert result.exit_code == 0
    on_disk = nh_config.load_config(cfg_path)
    assert on_disk.data["llm"]["utility_model"] == "claude-opus-5"


def test_models_set_no_op_reports_no_change_and_writes_nothing(runner, cfg_path):
    defaults = mc.defaults()
    before = cfg_path.read_text(encoding="utf-8")
    result = runner.invoke(
        cli, ["config", "models", "set", "utility", defaults["utility_model"]],
        catch_exceptions=False)
    assert result.exit_code == 0
    assert "no change" in result.output
    assert cfg_path.read_text(encoding="utf-8") == before


def test_models_set_persists_a_source_human_event(runner, cfg_path):
    result = runner.invoke(
        cli, ["config", "models", "set", "utility", "claude-opus-5"],
        catch_exceptions=False)
    assert result.exit_code == 0

    on_disk = nh_config.load_config(cfg_path)

    async def _read():
        async with Store(on_disk.db_path) as store:
            return await store.list_events("__config__")

    events = asyncio.run(_read())
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "human"
    assert ev["kind"] == "human_config_models_set"
    assert ev["changes"] == {
        "utility_model": {"old": "claude-haiku-4-5", "new": "claude-opus-5"}
    }


def test_models_set_preserves_config_yaml_structure(runner, cfg_path):
    before_lines = cfg_path.read_text(encoding="utf-8").splitlines()
    result = runner.invoke(
        cli, ["config", "models", "set", "utility", "claude-opus-5"],
        catch_exceptions=False)
    assert result.exit_code == 0
    after_lines = cfg_path.read_text(encoding="utf-8").splitlines()
    assert len(after_lines) == len(before_lines)
    changed = [(b, a) for b, a in zip(before_lines, after_lines) if b != a]
    assert len(changed) == 1
    assert "utility_model" in changed[0][1]
    assert "claude-opus-5" in changed[0][1]


def test_models_set_preserves_hand_written_comments_and_unrelated_sections(
        runner, tmp_path, monkeypatch):
    """The test above cannot distinguish the real comment-preserving splice
    from a mutant that swaps it for `yaml.safe_load`+`yaml.safe_dump` — its
    seed config has zero comment lines, so both implementations produce a
    file with the same line count and exactly one changed line. Seed a
    config with REAL structure instead and assert every comment and
    unrelated section survives byte-for-byte. A round-trip mutant would
    delete all of them."""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(nh_config, "CONFIG_PATH", path)
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    seed = _COMMENTED_CONFIG.format(db_path=str(tmp_path / "test.db"))
    path.write_text(seed)
    assert nh_config.load_config(path).db_path == tmp_path / "test.db"

    before_lines = seed.splitlines()
    result = runner.invoke(
        cli, ["config", "models", "set", "utility", "claude-opus-5"],
        catch_exceptions=False)
    assert result.exit_code == 0

    after_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(after_lines) == len(before_lines)
    changed = [(b, a) for b, a in zip(before_lines, after_lines) if b != a]
    assert len(changed) == 1
    assert changed[0] == (
        "  utility_model: claude-haiku-4-5",
        "  utility_model: claude-opus-5",
    )
    assert "# no_human config.yaml — hand-edited, keep these comments." in after_lines
    assert "  # Billing mode: subscription bills CLAUDE_CODE_OAUTH_TOKEN." in after_lines
    assert (
        "  review_model: claude-opus-4-8  # reviewer tier — do not change lightly"
        in after_lines
    )
    assert "server:" in after_lines
    assert "  host: 127.0.0.1" in after_lines
    assert "git:" in after_lines
    assert "  branch_prefix: no-human/" in after_lines


def test_models_set_splices_into_a_header_with_an_inline_comment(
        runner, tmp_path, monkeypatch):
    """The header-locator used to require an exact `"llm:"` line, so a header
    carrying an inline comment (a real hand-edit naming which subscription
    pays) was treated as absent and a second `llm:` block got appended —
    PyYAML's last-wins constructor then silently dropped the operator's
    entire original section on the next load."""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(nh_config, "CONFIG_PATH", path)
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    seed = _COMMENTED_CONFIG.format(db_path=str(tmp_path / "test.db")).replace(
        "llm:\n", "llm:  # which subscription pays\n", 1
    )
    path.write_text(seed)
    assert nh_config.load_config(path).db_path == tmp_path / "test.db"

    before_lines = seed.splitlines()
    result = runner.invoke(
        cli, ["config", "models", "set", "utility", "claude-opus-5"],
        catch_exceptions=False)
    assert result.exit_code == 0

    after = path.read_text(encoding="utf-8")
    after_lines = after.splitlines()
    assert after.count("llm:") == 1
    assert "llm:  # which subscription pays" in after_lines
    assert len(after_lines) == len(before_lines)
    changed = [(b, a) for b, a in zip(before_lines, after_lines) if b != a]
    assert len(changed) == 1
    assert changed[0] == (
        "  utility_model: claude-haiku-4-5",
        "  utility_model: claude-opus-5",
    )
    assert (
        "  review_model: claude-opus-4-8  # reviewer tier — do not change lightly"
        in after_lines
    )
    assert nh_config.load_config(path).data["llm"]["auth_mode"] == "subscription"


# --------------------------------------------------------------------------- #
# CLI and API share one code path                                             #
# --------------------------------------------------------------------------- #


def test_cli_and_api_produce_byte_identical_config_yaml_for_the_same_change(
        runner, tmp_path, monkeypatch):
    """The whole point of `core.model_settings` existing as a standalone
    module is that the CLI and the PUT endpoint can never enforce different
    rules or write different bytes for the same input. Prove it directly:
    run the same change through each front door against separately-seeded,
    otherwise-identical config files, and diff the results."""
    import yaml as _yaml

    from no_human.core import model_settings

    # Redirect `database.path` under tmp_path too — otherwise the CLI side
    # of this comparison would persist its audit event into the operator's
    # REAL ~/.no_human/no_human.db (DEFAULT_CONFIG bakes in the real path).
    seed = _yaml.safe_dump(
        {**_yaml.safe_load(_yaml.safe_dump(nh_config.DEFAULT_CONFIG, sort_keys=False)),
         "database": {"path": str(tmp_path / "test.db")}},
        sort_keys=False,
    )
    cli_cfg = tmp_path / "cli_config.yaml"
    api_cfg = tmp_path / "api_config.yaml"
    cli_cfg.write_text(seed)
    api_cfg.write_text(seed)
    assert cli_cfg.read_text(encoding="utf-8") == api_cfg.read_text(encoding="utf-8")

    monkeypatch.setattr(nh_config, "CONFIG_PATH", cli_cfg)
    monkeypatch.setattr(nh_config, "ENV_PATH", tmp_path / ".env")
    result = runner.invoke(
        cli, ["config", "models", "set", "utility", "claude-opus-5"],
        catch_exceptions=False)
    assert result.exit_code == 0

    running = nh_config.load_config(api_cfg).data
    model_settings.apply_model_changes(
        {"utility_model": "claude-opus-5"},
        running_cfg_data=running,
        config_path=api_cfg,
    )

    assert cli_cfg.read_text(encoding="utf-8") == api_cfg.read_text(encoding="utf-8")
