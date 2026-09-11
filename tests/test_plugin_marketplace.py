"""The repo is its own Claude Code plugin marketplace, and the two manifests must agree.

`.claude-plugin/marketplace.json` is what makes

    /plugin marketplace add no-human-ai/no_human
    /plugin install no-human@no-human-ai

work against this repository directly, without waiting for a third-party
catalog. It is inert until a user adds it: Claude Code registers a marketplace
only on `/plugin marketplace add` or from an `extraKnownMarketplaces` setting,
which this repo deliberately does not ship — a repo-root file that silently
reconfigured anyone who opened the repo is the hazard that kept a root
`.mcp.json` out of this tree.

What breaks without these tests: the marketplace entry points at a plugin
directory by relative path and repeats its name, so a rename or a move makes
the catalog advertise a plugin that cannot be fetched — and the failure only
shows up in someone else's session.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = ROOT / ".claude-plugin/marketplace.json"

#: Names Anthropic reserves for official marketplaces; a third-party manifest
#: using one stops loading and reports an untrusted source.
RESERVED = {
    "claude-code-marketplace", "claude-code-plugins", "claude-plugins-official",
    "claude-plugins-community", "claude-community", "anthropic-marketplace",
    "anthropic-plugins", "agent-skills", "anthropic-agent-skills",
    "knowledge-work-plugins", "life-sciences", "claude-for-legal",
    "claude-for-financial-services", "financial-services-plugins",
    "first-party-plugins", "healthcare",
}


def _market() -> dict:
    return json.loads(MARKETPLACE.read_text(encoding="utf-8"))


def test_the_marketplace_has_the_three_required_fields():
    m = _market()
    assert m["name"] == "no-human-ai"
    assert m["owner"]["name"], "owner.name is required"
    assert isinstance(m["plugins"], list) and m["plugins"], "plugins[] must not be empty"


def test_the_marketplace_name_is_not_one_anthropic_reserves():
    assert _market()["name"] not in RESERVED


def test_every_listed_plugin_resolves_to_a_real_plugin_in_this_repo():
    for entry in _market()["plugins"]:
        source = entry["source"]
        assert isinstance(source, str) and source.startswith("./"), (
            f"{entry['name']}: same-repo plugins are listed by relative path")
        plugin_dir = (ROOT / source[2:]).resolve()
        assert plugin_dir.is_dir(), f"{entry['name']}: {source} is not a directory"
        manifest = plugin_dir / ".claude-plugin/plugin.json"
        assert manifest.is_file(), f"{entry['name']}: no plugin.json at {source}"
        assert json.loads(manifest.read_text(encoding="utf-8"))["name"] == entry["name"], (
            f"{entry['name']}: the catalog name and the plugin manifest name differ, "
            "so `/plugin install` would resolve nothing")


def test_the_catalog_entry_does_not_contradict_the_plugin_manifest():
    """Fields duplicated between the two files must say the same thing."""
    for entry in _market()["plugins"]:
        manifest = json.loads(
            (ROOT / entry["source"][2:] / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        for field in ("displayName", "description", "homepage", "repository", "license"):
            if field in entry and field in manifest:
                assert entry[field] == manifest[field], (
                    f"{entry['name']}.{field} differs between marketplace.json and plugin.json")


def test_the_repo_ships_no_auto_registering_marketplace_setting():
    """The manifest must stay inert for anyone who merely opens this repo."""
    settings = ROOT / ".claude/settings.json"
    if settings.is_file():
        assert "extraKnownMarketplaces" not in settings.read_text(encoding="utf-8"), (
            "a repo-level extraKnownMarketplaces would register this marketplace for "
            "anyone who trusts the folder, including no_human's own coder worktrees")


def test_the_plugin_manifest_carries_the_release_version():
    # The plugin ships in this repo and is released with it; a manifest pinned
    # at an old version reads as abandoned on every marketplace that lists it.
    import tomllib

    release = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    manifest = json.loads((ROOT / "plugins/no-human/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == release
