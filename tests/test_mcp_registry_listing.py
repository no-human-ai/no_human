"""The MCP Registry listing is three files that must agree, or the publish fails.

`server.json` names the server and points at a PyPI version; the registry
verifies ownership by finding `mcp-name: <that name>` in the PyPI README; the
PyPI README is this repository's README.md at the version pyproject.toml
declares. Drift between any two of them is discovered at publish time, after
the PyPI upload (irreversible per version) has already happened — so it is
pinned here instead.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _server() -> dict:
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def _project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_server_json_names_the_github_namespace_of_the_public_repo():
    s = _server()
    assert s["name"] == "io.github.no-human-ai/no_human"
    assert s["repository"]["url"] == "https://github.com/no-human-ai/no_human"
    assert s["repository"]["source"] == "github"


def test_server_json_respects_the_registry_schema_limits():
    """The live registry rejected a 119-char description with HTTP 422
    (`expected length <= 100`) on the first review of this file — a limit the
    field-by-field pins above cannot see. Pin the schema's hard limits here so
    the next one is caught before the irreversible PyPI upload, not after."""
    s = _server()
    assert len(s["description"]) <= 100, len(s["description"])
    assert 1 <= len(s["title"]) <= 100
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", s["name"])
    assert s["repository"].get("id"), "repository.id guards against repo resurrection"


def test_server_json_version_matches_pyproject_at_both_levels():
    s = _server()
    assert s["version"] == _project_version()
    assert [p["version"] for p in s["packages"]] == [_project_version()]


def test_the_pypi_package_entry_runs_the_bridge_the_way_clients_invoke_it():
    (pkg,) = _server()["packages"]
    assert pkg["registryType"] == "pypi"
    assert pkg["registryBaseUrl"] == "https://pypi.org"
    assert pkg["identifier"] == "no-human"
    assert pkg["runtimeHint"] == "uvx"
    assert pkg["transport"] == {"type": "stdio"}
    # `uvx no-human mcp-serve` — so the package must expose a `no-human` script
    # (uvx runs the script named after the package) and `mcp-serve` must be a
    # real subcommand of that entry point.
    assert [a["value"] for a in pkg["packageArguments"]] == ["mcp-serve"]
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["no-human"] == scripts["nh"] == "no_human.cli.commands:main"
    from no_human.cli.commands import cli
    assert "mcp-serve" in cli.commands


def test_readme_carries_the_registry_ownership_marker_with_a_boundary():
    # The registry's rule: `mcp-name: <name>` followed by whitespace, a newline,
    # an HTML tag or `-->` — never glued to trailing punctuation.
    name = _server()["name"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(rf"mcp-name: {re.escape(name)}(\s|-->|<)", readme), (
        "README.md must carry the `mcp-name:` marker the MCP Registry checks on PyPI")


def test_the_publish_workflow_is_manual_and_tokenless():
    wf = (ROOT / ".github/workflows/publish-mcp-registry.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf
    assert "on:\n  push" not in wf and "release:" not in wf
    assert "login github-oidc" in wf
    assert "id-token: write" in wf
    assert "secrets." not in wf
