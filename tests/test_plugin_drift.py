"""Drift test tying the shipped Claude Code plugin to the live MCP bridge.

The bridge's tool set (``src/no_human/intake/mcp_bridge.py``) is the pinned
truth; the plugin's skill (``plugins/no-human/skills/file-a-task/SKILL.md``)
and README document it in prose. Nothing enforced those two stayed in sync,
so a renamed tool or parameter could rot the packaging silently. This file
enumerates the bridge's tools the same way ``tests/test_mcp_bridge.py`` does
(``mcp_bridge.mcp.list_tools()``) and asserts every tool name and every
input-schema property name appears verbatim in both documents, and pins the
plugin manifest files the packaging depends on.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from no_human.intake import mcp_bridge

REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        text=True,
    ).strip()
)

SKILL_MD = REPO_ROOT / "plugins" / "no-human" / "skills" / "file-a-task" / "SKILL.md"
PLUGIN_README = REPO_ROOT / "plugins" / "no-human" / "README.md"
MCP_JSON = REPO_ROOT / "plugins" / "no-human" / ".mcp.json"
PLUGIN_MANIFEST = REPO_ROOT / "plugins" / "no-human" / ".claude-plugin" / "plugin.json"


async def _bridge_tools():
    return {t.name: t for t in await mcp_bridge.mcp.list_tools()}


def _names(tools) -> list[str]:
    """Every tool name and every input-schema property name, in one flat list."""
    names = []
    for tool in tools.values():
        names.append(tool.name)
        schema = getattr(tool, "input_schema", getattr(tool, "inputSchema", None))
        if isinstance(schema, dict):
            names.extend(schema.get("properties", {}))
    return names


async def test_every_tool_and_property_name_is_documented_in_the_skill():
    tools = await _bridge_tools()
    text = SKILL_MD.read_text(encoding="utf-8")
    missing = [n for n in _names(tools) if n not in text]
    assert not missing, (
        f"{len(missing)} tool/property name(s) missing verbatim from "
        f"{SKILL_MD.relative_to(REPO_ROOT)}: {missing}"
    )


async def test_every_tool_and_property_name_is_documented_in_the_plugin_readme():
    tools = await _bridge_tools()
    text = PLUGIN_README.read_text(encoding="utf-8")
    missing = [n for n in _names(tools) if n not in text]
    assert not missing, (
        f"{len(missing)} tool/property name(s) missing verbatim from "
        f"{PLUGIN_README.relative_to(REPO_ROOT)}: {missing}"
    )


def test_the_skill_states_the_never_merge_boundary():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "merge is always the human" in text.lower(), (
        "SKILL.md must state the product boundary: no_human opens a PR and "
        "stops, merge is always the human's action"
    )
    for line in text.splitlines():
        assert "gh pr merge" not in line, (
            f"SKILL.md must never instruct merging; found: {line!r}"
        )


def test_the_skill_names_the_server_requirement():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "nh start" in text, "SKILL.md must tell the agent to start the server"
    assert "127.0.0.1:8420" in text, "SKILL.md must name the API address"


def test_the_skill_has_frontmatter_with_a_name():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md must open with YAML frontmatter"
    frontmatter, _, _ = text[3:].partition("---")
    assert "name:" in frontmatter, "SKILL.md frontmatter must declare name"
    assert "description:" in frontmatter, "SKILL.md frontmatter must declare description"


def test_mcp_json_launches_exactly_nh_mcp_serve():
    data = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    servers = data["mcpServers"]
    assert len(servers) == 1, f"expected exactly one server entry, got {list(servers)}"
    (entry,) = servers.values()
    assert entry["command"] == "nh"
    assert entry["args"] == ["mcp-serve"]


def test_plugin_manifest_parses_with_name_no_human():
    data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert data["name"] == "no-human"
