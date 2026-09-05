"""MCP stdio bridge exposing task_add + task_status (SCRUM-63).

A minimal Model Context Protocol server, run over stdio, that lets an MCP
client (e.g. an editor's agent) create and check no_human tasks through the
EXISTING local HTTP API at ``server.host``:``server.port`` (see
:data:`BASE_URL` for the default) — this module never
imports or touches the store, orchestrator, or API endpoints directly, only
calls the HTTP surface they already expose. Exactly two tools: ``task_add``
and ``task_status``. No auth (localhost-only, same trust domain as the web
board), no editor-specific config, no retry/fallback on startup.

``task_add`` POSTs ``source="mcp"`` and ``POST /api/tasks`` (``create_task``
in ``api/app.py``) persists it as first-class (alongside ``"board"`` and
``"jira"``); ``task_add`` returns whatever ``source`` the server actually
stored, which is ``"mcp"`` for tasks created through this bridge.

Run it with either ``nh mcp-serve`` or ``python -m no_human.intake.mcp_bridge``.
"""

from __future__ import annotations

import functools
import json
import logging

import httpx
import yaml
from mcp.server.mcpserver import MCPServer

from ..config import load_config

log = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8420

# Kept as a literal rather than built from the constants above: docs/security.md
# cites this line for the default host and port, and tests/test_readme_claims.py
# checks that the token really is on it.
BASE_URL = "http://127.0.0.1:8420"
_TIMEOUT = 10.0

# What load_config raises on a config it cannot read or parse, plus the shape
# errors a file that parses into something other than a mapping would raise.
# Deliberately NOT AuthError or ConfigError: those are the product refusing a
# config it read fine (an ANTHROPIC_API_KEY in the file, decomposition turned
# on). Catching them would warn and then dial the DEFAULT port while the
# config named another — so they propagate and the bridge refuses to start,
# the same as every other entry point.
_CONFIG_ERRORS = (
    OSError,
    yaml.YAMLError,
    ValueError,
    AttributeError,
    TypeError,
)

# Tests inject an httpx.MockTransport here to stub the HTTP layer without a
# real server. None (the default) means "use the real network".
_TRANSPORT: httpx.BaseTransport | None = None

mcp = MCPServer("no_human-mcp-bridge")


@functools.cache
def _base_url() -> str:
    """Base URL of the local API, from ``server.host`` and ``server.port``.

    Resolved once per process. Falls back to :data:`BASE_URL` when the config is
    missing, unreadable, or not shaped as expected (never when the product
    refuses it — see ``_CONFIG_ERRORS``), and warns when it does, so
    that an ignored config is visible instead of surfacing later as an
    unexplained "API unreachable at 8420". The warning goes to stderr, which is
    safe for a stdio MCP server; anything on stdout would corrupt the protocol.
    """
    try:
        data = load_config(create_if_missing=False).data
    except _CONFIG_ERRORS as exc:
        log.warning(
            "could not read the config (%s: %s); using %s",
            type(exc).__name__, exc, BASE_URL,
        )
        return BASE_URL

    server = data.get("server") if isinstance(data, dict) else None
    if server is not None and not isinstance(server, dict):
        log.warning(
            "config key 'server' is %s, not a mapping; using %s",
            type(server).__name__, BASE_URL,
        )
        return BASE_URL

    server = server or {}
    host = server.get("host") or _DEFAULT_HOST
    port = server.get("port") or _DEFAULT_PORT
    return f"http://{host}:{port}"


def _client() -> httpx.Client:
    return httpx.Client(base_url=_base_url(), timeout=_TIMEOUT, transport=_TRANSPORT)


@mcp.tool()
def task_add(title: str, description: str, repo_path: str) -> str:
    """Create a no_human task via POST /api/tasks (source="mcp"). Returns
    compact JSON {"task_id": str, "source": str} — source is whatever the
    server actually stored (the "mcp" source is first-class, see module
    docstring)."""
    with _client() as client:
        resp = client.post(
            "/api/tasks",
            json={
                "title": title,
                "description": description,
                "repo_path": repo_path,
                "source": "mcp",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return json.dumps(
        {"task_id": data["id"], "source": data["source"]}, separators=(",", ":")
    )


@mcp.tool()
def task_status(task_id_or_external_id: str) -> str:
    """Fetch a task's full current state via GET /api/tasks. Resolves by
    task id (or unique id prefix) first; if that 404s, falls back to matching
    external_id across the task list (GET /api/tasks does not index by
    external_id, so this is a client-side scan). Returns the complete task
    object as compact JSON."""
    with _client() as client:
        resp = client.get(f"/api/tasks/{task_id_or_external_id}")
        if resp.status_code == 404:
            listing = client.get("/api/tasks")
            listing.raise_for_status()
            match = next(
                (t for t in listing.json()
                 if t.get("external_id") == task_id_or_external_id),
                None,
            )
            if match is not None:
                resp = client.get(f"/api/tasks/{match['id']}")
        resp.raise_for_status()
        data = resp.json()
    return json.dumps(data, separators=(",", ":"))


def _ensure_api_reachable() -> None:
    """Refuse to start (no retry, no fallback) if the nh API is unreachable."""
    base_url = _base_url()
    try:
        with _client() as client:
            resp = client.get("/api/tasks")
            resp.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        log.error(
            "no_human API unreachable at %s — refusing to start (%s)",
            base_url, exc,
        )
        raise SystemExit(
            f"no_human API unreachable at {base_url}: {exc}"
        ) from exc


def main() -> None:
    _ensure_api_reachable()
    mcp.run()


if __name__ == "__main__":
    main()
