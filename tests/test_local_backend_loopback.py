"""End-to-end proof for the `local` coding backend: the REAL SDK-bundled
`claude` CLI, spawned as a real subprocess through `make_backend`'s `local`
branch, talking to a stdlib-only loopback HTTP stub instead of `api.anthropic.
com`. No mocking of the SDK or the transport — the only thing that is fake is
what answers `/v1/messages`.

This is deliberately heavier than `tests/test_local_backend.py`'s unit layer:
it is the one place that proves the three `extra_env` entries actually reach
the child process's environment (nothing here can be faked by a unit test
that stops at `ClaudeAgentOptions.env`), that the pre-existing PreToolUse
guard hook fires against a REAL tool call from a REAL model turn, and that
nothing leaks to `os.environ`, the request headers, or a sibling role.

Protocol notes discovered by driving the real CLI against a logging stub
before writing this file (kept here so the next person does not have to
re-discover them):
  - the CLI's own auto-title feature makes an EXTRA `/v1/messages` call per
    session (`output_config.format.schema.properties.title` in the request
    body) — completely unrelated to the conversation turn. The stub answers
    it out of a fixed response and does not let it consume a turn slot, or
    every scripted test would be off-by-one.
  - a `tool_use` content block streams as `content_block_start` (type
    "tool_use", empty input) + `content_block_delta` (`input_json_delta`,
    the JSON-encoded input as one `partial_json` chunk) + `content_block_stop`
    — the SDK does not require token-by-token JSON streaming.
  - a PreToolUse denial does not appear as an SSE-level error: the turn
    finishes normally (`is_error=False`) and the denial surfaces on
    `AgentResult.denials` (from the CLI's own `permission_denials` on its
    result message) — and, mechanically, as the absence of the file the
    denied `Write` would have created.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from no_human.agent.backend import make_backend
from no_human.agent.claude_backend import ClaudeBackend


def _bundled_cli_path() -> str | None:
    """Locate the SDK-bundled `claude` CLI the same way the real transport
    does at connect() time (`SubprocessCLITransport._find_bundled_cli`) — not
    a hand-rolled path guess — so the skip guard below is honest about what
    it is actually skipping on. Returns `None` if the bundle is absent, e.g.
    a minimal install of `claude_agent_sdk` without its `_bundled/` payload."""
    return SubprocessCLITransport("", ClaudeAgentOptions())._find_bundled_cli()


_BUNDLED_CLI_PATH = _bundled_cli_path()

# Drives the REAL ClaudeBackend / real SDK-bundled CLI over a loopback stub —
# opts out of conftest.py's autouse hermetic-SDK stub. Also skips outright
# when no bundled CLI binary is present (AC5e): this module spawns a real
# subprocess and has nothing to drive without one.
pytestmark = [
    pytest.mark.real_backend,
    pytest.mark.skipif(
        _BUNDLED_CLI_PATH is None,
        reason=(
            "SDK-bundled claude CLI not found (expected in "
            "<claude_agent_sdk install>/_bundled/, named claude.exe on "
            "Windows and claude elsewhere) — this module "
            "spawns a real CLI subprocess and cannot run without it."
        ),
    ),
]

# --------------------------------------------------------------------------- #
# The canned Anthropic /v1/messages SSE stub.                                  #
# --------------------------------------------------------------------------- #


def _sse(events: list[tuple[str, dict]]) -> bytes:
    out = b""
    for event, data in events:
        out += f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
    return out


def _is_title_request(payload: dict) -> bool:
    """The CLI's own session-auto-title side channel — see module docstring."""
    fmt = (payload.get("output_config") or {}).get("format") or {}
    props = (fmt.get("schema") or {}).get("properties") or {}
    return "title" in props


def _title_response() -> list[tuple[str, dict]]:
    return _text_turn("Stub session title", stop_reason="end_turn")


def _text_turn(text: str, *, stop_reason: str = "end_turn") -> list[tuple[str, dict]]:
    return [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_text", "type": "message", "role": "assistant",
            "model": "stub-model", "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                            "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]


def _tool_use_turn(tool_name: str, tool_input: dict,
                    tool_use_id: str = "toolu_01") -> list[tuple[str, dict]]:
    return [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_tool", "type": "message", "role": "assistant",
            "model": "stub-model", "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "tool_use", "id": tool_use_id,
                                                     "name": tool_name, "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "input_json_delta",
                                            "partial_json": json.dumps(tool_input)}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                            "usage": {"output_tokens": 20}}),
        ("message_stop", {"type": "message_stop"}),
    ]


@dataclass
class _Recorded:
    path: str
    headers: dict[str, str]
    body: str


@dataclass
class _LoopbackState:
    """Per-test mutable state the fixture hands back: set `.script` to the
    list of turns to serve in order (clamped to the last entry once
    exhausted, same as the proven `stub_cli` pattern), then read `.requests`
    and `.base_url`."""
    base_url: str = ""
    script: list[list[tuple[str, dict]]] = field(default_factory=list)
    requests: list[_Recorded] = field(default_factory=list)
    _turn_index: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _make_handler(state: _LoopbackState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence — pytest -s stays readable
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            payload = json.loads(raw)
            with state.lock:
                state.requests.append(
                    _Recorded(self.path, dict(self.headers), raw.decode("utf-8", "replace")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if _is_title_request(payload):
                self.wfile.write(_sse(_title_response()))
                self.wfile.flush()
                return
            with state.lock:
                i = state._turn_index
                state._turn_index += 1
            events = state.script[min(i, len(state.script) - 1)]
            self.wfile.write(_sse(events))
            self.wfile.flush()

    return Handler


@pytest.fixture
def loopback_server():
    """A stdlib-only `/v1/messages` stub bound to 127.0.0.1 (a literal
    loopback IP, so `config.assert_local_backend_mode` accepts it), serving
    whatever `.script` is set to. Real thread, real socket, real HTTP —
    nothing about the transport between the CLI subprocess and this server
    is mocked."""
    state = _LoopbackState()
    handler = _make_handler(state)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _local_backend(loopback_server, tmp_path, isolated_env_file, **overrides):
    """Build the `local` backend through the SAME seam production code uses —
    `make_backend`'s `local` branch — not a direct `ClaudeBackend(...)`
    construction, so this test proves the factory wiring, not just the class."""
    cfg = {
        "worker": {"backend": "local"},
        "llm": {"local_base_url": loopback_server.base_url, "local_model": "stub-model"},
    }
    backend = make_backend(model="claude-sonnet-5", config=cfg, role="coder", **overrides)
    assert isinstance(backend, ClaudeBackend)
    return backend


# --------------------------------------------------------------------------- #
# (a) a tool_use round-trips correctly.                                        #
# --------------------------------------------------------------------------- #


async def test_tool_use_round_trips(loopback_server, tmp_path, isolated_env_file):
    target = tmp_path / "hello.txt"
    loopback_server.script = [
        _tool_use_turn("Write", {"file_path": str(target), "content": "round-tripped"}),
        _text_turn("done"),
    ]

    backend = _local_backend(loopback_server, tmp_path, isolated_env_file)
    result = await backend.run("write a file", cwd=tmp_path, max_turns=4)

    assert result.is_error is False, result.final_text
    assert result.denials == []
    assert target.read_text() == "round-tripped"


# --------------------------------------------------------------------------- #
# (b) a PreToolUse deny hook actually blocks execution.                        #
# --------------------------------------------------------------------------- #


async def test_pretooluse_deny_hook_blocks_execution(
        loopback_server, tmp_path, isolated_env_file):
    forbidden = tmp_path / ".env"
    loopback_server.script = [
        _tool_use_turn("Write", {"file_path": str(forbidden), "content": "SHOULD_NOT_LAND"}),
        _text_turn("acknowledged"),
    ]

    # Default forbidden_paths (`.env`, `secrets/`, `*.key`, `*.pem`) apply —
    # this is the SAME guard hook every ClaudeBackend session runs, not a
    # test-only stand-in.
    backend = _local_backend(loopback_server, tmp_path, isolated_env_file)
    result = await backend.run("write a file", cwd=tmp_path, max_turns=4)

    assert result.is_error is False, result.final_text
    assert not forbidden.exists(), "the denied write must never touch disk"
    assert result.denials, "the guard's denial must surface on AgentResult.denials"
    assert any("Write" in d and ".env" in d for d in result.denials), result.denials


# --------------------------------------------------------------------------- #
# (c) the stub never receives an OAuth-shaped credential.                      #
# --------------------------------------------------------------------------- #


_FAKE_OAUTH_TOKEN = "sk-ant-oat01-FAKE-SHOULD-NEVER-LEAVE-THE-PARENT-PROCESS"


async def test_stub_never_receives_an_oauth_shaped_credential(
        loopback_server, tmp_path, isolated_env_file, monkeypatch):
    """Plant a real-looking OAuth token in the PARENT process env — the shape
    an operator's actual subscription credential has — and prove it never
    reaches the wire: every request's `x-api-key` is the local key, no header
    or body anywhere carries the OAuth token or an `sk-ant-oat` prefix, and
    there is no `Authorization: Bearer ...` header at all."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", _FAKE_OAUTH_TOKEN)
    loopback_server.script = [_text_turn("hi there")]

    backend = _local_backend(loopback_server, tmp_path, isolated_env_file)
    result = await backend.run("say hi", cwd=tmp_path, max_turns=1)

    assert result.is_error is False, result.final_text
    assert loopback_server.requests, "the stub must have been called at all"
    for req in loopback_server.requests:
        haystacks = list(req.headers.values()) + [req.body]
        for value in haystacks:
            assert _FAKE_OAUTH_TOKEN not in value, (req.path, req.headers)
            assert "sk-ant-oat" not in value, (req.path, req.headers)
        assert not any(k.lower() == "authorization" for k in req.headers), req.headers
        assert req.headers.get("x-api-key") == "no-key-local-backend", req.headers


# --------------------------------------------------------------------------- #
# (d) the PARENT process's os.environ is unchanged after the run.              #
# --------------------------------------------------------------------------- #


async def test_parent_os_environ_unchanged_after_run(
        loopback_server, tmp_path, isolated_env_file):
    loopback_server.script = [_text_turn("hi there")]
    before = dict(os.environ)

    backend = _local_backend(loopback_server, tmp_path, isolated_env_file)
    result = await backend.run("say hi", cwd=tmp_path, max_turns=1)

    assert result.is_error is False, result.final_text
    assert dict(os.environ) == before
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        # Explicit per-key check as well as the dict-equality above: a key
        # that was already present and got its VALUE overwritten (rather
        # than a key that got newly added) would still pass a naive "no new
        # keys" check but not this one.
        assert os.environ.get(key) == before.get(key)


# --------------------------------------------------------------------------- #
# (e) the module is honestly skippable when the CLI is unavailable.            #
# --------------------------------------------------------------------------- #


def test_bundled_cli_skip_marker_is_honest():
    """This test only ever runs when `pytestmark`'s `skipif` let the module
    through, i.e. the locator found a real bundled CLI — so reaching this
    assertion is itself proof the guard consulted the SAME locator the real
    transport uses, not a placeholder that always evaluates True/False. The
    skip `reason` above must name where the bundle is expected so a run with
    no CLI reports *why* (`-rs`) instead of just vanishing from the count."""
    assert _BUNDLED_CLI_PATH is not None, (
        "reaching this line means pytestmark's skipif did not fire, so the "
        "locator must have found something"
    )
    # Checked as directory + stem rather than a literal `_bundled/claude`,
    # because the binary's NAME is the SDK's business and it is not the same on
    # every platform: `_find_bundled_cli` looks for `claude.exe` on Windows and
    # `claude` elsewhere. Asserting the POSIX name failed every Windows run of
    # this module while the locator was working correctly. Re-deriving the
    # platform rule here would just be a second copy of it to drift.
    found = Path(_BUNDLED_CLI_PATH)
    assert found.parent.name == "_bundled", _BUNDLED_CLI_PATH
    assert found.stem == "claude", _BUNDLED_CLI_PATH
