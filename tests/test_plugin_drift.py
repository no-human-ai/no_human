"""Drift test tying the shipped Claude Code plugin to the live MCP bridge.

The bridge's tool set (``src/no_human/intake/mcp_bridge.py``) is the pinned
truth; the plugin's skills (``plugins/no-human/skills/file-a-task/SKILL.md``
and ``plugins/no-human/skills/review-this-branch/SKILL.md``) and README
document it in prose. Nothing enforced that these stayed in sync,
so a renamed tool or parameter could rot the packaging silently. This file
enumerates the bridge's tools the same way ``tests/test_mcp_bridge.py`` does
(``mcp_bridge.mcp.list_tools()``) and asserts every tool name and every
input-schema property name appears verbatim in both documents, and pins the
plugin manifest files the packaging depends on.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

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
GATE_SKILL_MD = (
    REPO_ROOT / "plugins" / "no-human" / "skills" / "review-this-branch" / "SKILL.md"
)


async def _bridge_tools():
    return {t.name: t for t in await mcp_bridge.mcp.list_tools()}


def _names(tools) -> list[str]:
    """Every tool name and every input-schema property name, in one flat list."""
    names = []
    for tool in tools.values():
        names.append(tool.name)
        names.extend(tool.input_schema.get("properties", {}))
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


def test_the_gate_skill_has_frontmatter_with_a_name():
    text = GATE_SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md must open with YAML frontmatter"
    frontmatter, _, _ = text[3:].partition("---")
    assert "name:" in frontmatter, "SKILL.md frontmatter must declare name"
    assert "description:" in frontmatter, "SKILL.md frontmatter must declare description"


def test_the_gate_skill_names_the_verb_and_the_credential():
    text = GATE_SKILL_MD.read_text(encoding="utf-8")
    assert "nh gate" in text, "SKILL.md must name the CLI verb it runs"
    assert "claude setup-token" in text, (
        "SKILL.md must tell the agent to use the user's own Claude credential"
    )
    assert "merge base" in text, "SKILL.md must name the comparison it uses"


def test_the_gate_skill_pins_the_exit_code_contract():
    """The exit-code table is the skill's whole safety contract — nothing
    else stops an agent from relaying a pass on exit 1 or 2. Pin the two
    load-bearing sentences verbatim so an edit that quietly loosens them
    (e.g. "exit code 2 also means ... report a pass") goes red here instead
    of shipping silently."""
    text = GATE_SKILL_MD.read_text(encoding="utf-8")
    assert "On exit `2`, never report a pass." in text
    assert "Only exit `0` is a pass" in text


def test_the_gate_skill_is_documented_in_the_plugin_readme():
    text = PLUGIN_README.read_text(encoding="utf-8")
    assert "review-this-branch" in text, (
        "the plugin README must document the review-this-branch skill"
    )
    assert "nh gate" in text


# (pattern, human-readable label) — matched case-insensitively, whitespace-
# tolerant, against every line of GATE_SKILL_MD. A prior version of this
# guard only rejected the three exact literal substrings "git commit",
# "git push", "gh pr merge" — blind to an approval verb (`nh approve`,
# `gh pr review --approve`) or a write-then-execute shell chain that never
# spells any of those three phrases at all.
_FORBIDDEN_WRITE_PATTERNS = [
    (r"git\s+commit", "a commit"),
    (r"git\s+push", "a push"),
    (r"gh\s+pr\s+merge", "a PR merge"),
    (r"gh\s+pr\s+review\s+(--approve|-a\b)", "a PR approval"),
    (r"\bnh\s+approve\b", "an approval verb"),
    (r">>?\s*\S+(\.sh|\.py)?\s*&&\s*(bash|sh|python3?|\./)", "a write-then-execute shell chain"),
]


def test_the_gate_skill_promises_no_writes():
    text = GATE_SKILL_MD.read_text(encoding="utf-8")
    assert "merge is always the human" in text.lower(), (
        "SKILL.md must state the product boundary: read and report only"
    )
    for line in text.splitlines():
        lowered = line.lower()
        for pattern, label in _FORBIDDEN_WRITE_PATTERNS:
            assert not re.search(pattern, lowered), (
                f"SKILL.md must never instruct {label}; found: {line!r}"
            )


@pytest.mark.parametrize("sample", [
    "run `git commit -am wip` first",
    "then `git push origin feature`",
    "finish with `gh pr merge --squash`",
    "approve it via `gh pr review --approve`",
    "or just run `gh pr review -a`",
    "call `nh approve <task-id>` once you're happy",
    "cat > deploy.sh && bash deploy.sh",
    "echo done >> log.txt && ./log.txt",
])
def test_the_no_writes_guard_actually_catches_every_forbidden_shape(sample):
    """Meta-test for the guard above: each of these strings is exactly the
    shape of instruction `test_the_gate_skill_promises_no_writes` exists to
    reject. If a future edit to `_FORBIDDEN_WRITE_PATTERNS` narrows it back
    to blind spots (as the pre-fix, three-literal version was), this goes
    red before SKILL.md ever needs to say something dangerous to notice."""
    lowered = sample.lower()
    assert any(re.search(pattern, lowered) for pattern, _label in _FORBIDDEN_WRITE_PATTERNS), (
        f"no forbidden-write pattern matched {sample!r}"
    )
