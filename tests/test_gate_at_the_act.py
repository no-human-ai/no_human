"""The human gate moves from the verb's SPELLING to the caller's IDENTITY.

`guard.py`'s existing PreToolUse hook is a lexical/argv check: it recognises
`nh approve`, `env nh approve`, `uv run nh approve`, ... by pattern-matching
the command line. That is necessarily incomplete — there is no bound on how a
command line can be dressed up. `session_mark.py` adds an orthogonal,
ADDITIVE layer: every subprocess a coding backend (`ClaudeBackend`,
`CodexBackend`) launches is stamped with an env-var mark
(`NO_HUMAN_AGENT_SESSION` / `NO_HUMAN_AGENT_SESSION_KIND`), and the
gate-ending act sites — the `approve` and `merge-stack run` CLI commands, and
the four gate-ending HTTP routes (`/approve`, `/approve-landed`,
`/finish-review`, `/shipped`) — refuse a caller carrying that mark before any
other work happens, regardless of how the caller spelled its way there.

This file tests every enforcement point session_mark.py introduces:

  A. session_mark.py's own pure logic (falsy spellings, refuse/headers).
  B. The two backend env funnels actually stamp the mark (and never leak it
     into this process's own os.environ).
  C. The CLI act sites (`approve`, `merge_stack_run`) refuse a marked caller
     with exit code 2, before `_bootstrap`/`Store` are ever touched, and
     leave an unmarked caller's business logic untouched (control).
  D. The HTTP middleware refuses a marked caller (by header OR by the
     server's own process mark) on all four gate-ending routes, across the
     path spellings Starlette actually routes, and leaves every other route,
     and every non-POST method, alone.
  E. `_GATE_ENDING_SUFFIXES` (app.py) and `_GATE_PATH` (guard.py) cannot
     silently drift apart — the two lists are pinned against each other.
  F. `NhClient` (api_client.py) sends the mark header when the shell process
     itself is marked, and sends nothing when it is not.

Codex SUBSCRIPTION-mode mark coverage is deliberately NOT duplicated here:
`tests/test_codex_backend.py::test_subscription_mode_accepts_an_unrecognised_but_present_session`
already asserts `_child_env()` includes `mark_env("codex")` in that mode.
"""

from __future__ import annotations

import unittest.mock as mock

import httpx
import pytest
import pytest_asyncio
from click.testing import CliRunner

from no_human.agent import codex_backend as cx
from no_human.agent.session_mark import (
    AGENT_SESSION_HEADER,
    NO_HUMAN_AGENT_SESSION,
    NO_HUMAN_AGENT_SESSION_KIND,
    GateRefused,
    current_mark,
    mark_env,
    mark_headers,
    refuse_if_marked,
    request_is_marked,
)
from no_human.api.app import _GATE_ENDING_SUFFIXES, _is_gate_ending_path, app
from no_human.agent import guard as guard_mod
from no_human.cli.api_client import NhClient
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

# Tests here go through `load_config`/`_bootstrap`-adjacent paths (the `client`
# fixture below mirrors tests/test_api.py's), which can reach the operator's
# real ~/.no_human/.env — requested by NAME, never autouse. See conftest.py.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


def _stub_codex_login_status(monkeypatch):
    """Make the api_key billing gate (`assert_api_key_billing_path`) pass
    WITHOUT shelling out to the real `codex` CLI, so `_child_env()` proceeds
    to its real assertions on a bare CI runner with no api_key-backed session.
    Mirrors the kwarg-accepting form the gate calls `codex_login_status` with."""
    monkeypatch.setattr(
        cx, "codex_login_status",
        lambda cli_path=None, timeout_s=10.0, *, env_overrides=None:
            cx.CodexSessionStatus(True, "api_key", "stub"),
    )


# --------------------------------------------------------------------------- #
# A. session_mark.py — pure logic                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["", "0", "false", "None", "NO", "  no  ", "False"])
def test_falsy_spellings_are_not_marked(monkeypatch, value):
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, value)
    assert current_mark() is None


@pytest.mark.parametrize("value", ["1", "true", "yes", "claude", " 1 "])
def test_any_other_value_is_marked(monkeypatch, value):
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, value)
    assert current_mark() is not None


def test_kind_defaults_to_unknown_when_absent(monkeypatch):
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.delenv(NO_HUMAN_AGENT_SESSION_KIND, raising=False)
    assert current_mark() == "unknown"


def test_kind_is_read_when_present(monkeypatch):
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION_KIND, "codex")
    assert current_mark() == "codex"


def test_refuse_if_marked_noops_when_unmarked():
    assert refuse_if_marked("approve") is None


def test_refuse_if_marked_raises_with_act_and_kind(monkeypatch):
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION_KIND, "claude")
    with pytest.raises(GateRefused) as exc_info:
        refuse_if_marked("merge_stack_run")
    exc = exc_info.value
    assert exc.act == "merge_stack_run"
    assert "claude" in exc.reason
    assert "merge_stack_run" in exc.reason


def test_mark_headers_empty_when_unmarked():
    assert mark_headers() == {}


def test_mark_headers_carries_kind_when_marked(monkeypatch):
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION_KIND, "codex")
    assert mark_headers() == {AGENT_SESSION_HEADER: "codex"}


def test_request_is_marked_via_header_alone():
    assert request_is_marked("codex") is True
    assert request_is_marked(None) is False
    assert request_is_marked("") is False


def test_request_is_marked_via_process_env_alone(monkeypatch):
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    assert request_is_marked(None) is True


# --------------------------------------------------------------------------- #
# B. Backend env funnels                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.real_backend
def test_claude_backend_options_carry_the_mark(tmp_path):
    from no_human.agent.claude_backend import ClaudeBackend

    b = ClaudeBackend(model="claude-opus-5")
    opts = b._options(tmp_path, 40)
    assert opts.env["NO_HUMAN_AGENT_SESSION"] == "1"
    assert opts.env["NO_HUMAN_AGENT_SESSION_KIND"] == "claude"


@pytest.mark.real_backend
def test_claude_backend_mark_never_leaks_into_this_process_env(tmp_path, monkeypatch):
    import os

    from no_human.agent.claude_backend import ClaudeBackend

    monkeypatch.delenv(NO_HUMAN_AGENT_SESSION, raising=False)
    ClaudeBackend(model="claude-opus-5")._options(tmp_path, 40)
    assert NO_HUMAN_AGENT_SESSION not in os.environ


@pytest.mark.real_backend
def test_claude_backend_options_deny_the_coder_ambient_secrets(tmp_path, monkeypatch):
    """The coder subprocess inherits the launcher's whole environment
    (`{**os.environ, **options.env}`); `_options` must blank every secret-
    shaped variable that is not Anthropic/Claude auth so a prompt injection
    in the child cannot read it. Non-secret operational vars inherit untouched;
    the model-auth var and the session mark keep their real values."""
    import os

    from no_human.agent.claude_backend import ClaudeBackend

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("MY_CUSTOM_TOKEN", "vendor_secret")  # unknown provider, caught by shape
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "real-oauth")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(tmp_path))

    opts = ClaudeBackend(model="claude-opus-5")._options(tmp_path, 40)
    # options.env overrides the inherited value to empty for every secret.
    for secret in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "MY_CUSTOM_TOKEN"):
        assert opts.env[secret] == "", secret
    # Allowed vars and non-secret operational vars are not overridden — they
    # inherit their real value into the child.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in opts.env
    assert "PATH" not in opts.env
    assert "HOME" not in opts.env
    # The effective child environment: secrets gone, everything needed intact.
    child = {**os.environ, **opts.env}
    assert child["GITHUB_TOKEN"] == ""
    assert child["SSH_AUTH_SOCK"] == ""
    assert child["CLAUDE_CODE_OAUTH_TOKEN"] == "real-oauth"
    assert child["PATH"] == "/usr/bin:/bin"
    assert opts.env["NO_HUMAN_AGENT_SESSION"] == "1"


def test_codex_backend_api_key_child_env_carries_the_mark(monkeypatch):
    """No `codex_login_status` stub: this mirrors the existing, unstubbed
    `test_the_claude_credential_is_not_exported_into_the_codex_subprocess`
    (tests/test_codex_backend.py) — on this dev environment the real `codex`
    CLI is installed, and `assert_api_key_billing_path` reads it against the
    freshly-materialised, isolated `CODEX_HOME` this call itself creates
    (`codex_api_key_home()` is redirected under pytest's HOME isolation), not
    against any live network state. If that ever needs decoupling from the
    ambient CLI, stub `cx.codex_login_status` with the kwarg-accepting form
    `assert_api_key_billing_path` calls it with: ``lambda cli_path=None,
    timeout_s=10.0, *, env_overrides=None: cx.CodexSessionStatus(True,
    "api_key", "stub")``."""
    _stub_codex_login_status(monkeypatch)  # gate must not shell out to the real CLI
    env = {"OPENAI_API_KEY": "not-a-real-key", "PATH": "/usr/bin:/bin"}
    b = cx.CodexBackend(auth_mode="api_key", env=env)
    child_env = b._child_env()
    assert child_env["NO_HUMAN_AGENT_SESSION"] == "1"
    assert child_env["NO_HUMAN_AGENT_SESSION_KIND"] == "codex"


def test_codex_backend_mark_never_leaks_into_this_process_env(monkeypatch):
    import os

    _stub_codex_login_status(monkeypatch)  # gate must not shell out to the real CLI
    monkeypatch.delenv(NO_HUMAN_AGENT_SESSION, raising=False)
    env = {"OPENAI_API_KEY": "not-a-real-key", "PATH": "/usr/bin:/bin"}
    cx.CodexBackend(auth_mode="api_key", env=env)._child_env()
    assert NO_HUMAN_AGENT_SESSION not in os.environ


# --------------------------------------------------------------------------- #
# C. CLI act sites                                                            #
# --------------------------------------------------------------------------- #

class _Cfg:
    db_path = None
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)


def _cfg(db_path):
    c = _Cfg()
    c.db_path = db_path
    return c


def test_approve_refuses_a_marked_caller_before_bootstrap(monkeypatch):
    import no_human.cli.commands as cmd_mod

    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION_KIND, "claude")
    with mock.patch.object(cmd_mod, "_bootstrap") as bootstrap:
        bootstrap.side_effect = AssertionError("must not reach _bootstrap")
        result = CliRunner().invoke(cmd_mod.approve, ["some-task-id"])
    assert result.exit_code == 2
    assert "refused" in result.output.lower()
    assert "claude" in result.output
    bootstrap.assert_not_called()


def test_merge_stack_run_refuses_a_marked_caller_before_bootstrap(monkeypatch):
    import no_human.cli.commands as cmd_mod

    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION_KIND, "codex")
    with mock.patch.object(cmd_mod, "_bootstrap") as bootstrap:
        bootstrap.side_effect = AssertionError("must not reach _bootstrap")
        result = CliRunner().invoke(cmd_mod.merge_stack_run, ["--yes"])
    assert result.exit_code == 2
    assert "refused" in result.output.lower()
    assert "codex" in result.output
    bootstrap.assert_not_called()


def test_approve_unmarked_control_reaches_business_logic(tmp_path):
    """Proves the refusal check itself (and not some other failure) is what
    produces exit code 2 above: with no mark, the same CLI command reaches
    `_bootstrap`/`Store` and does ordinary business — here, a task already
    sitting in AWAITING_APPROVAL with no PR completes as an operator merge
    instruction (exit 0)."""
    import asyncio

    import no_human.cli.commands as cmd_mod

    db = tmp_path / "nh.db"

    async def _seed():
        async with Store(db) as store:
            t = Task.new("t", repo_path="/tmp/x")
            await store.create_task(t)
            await store.set_status(
                t, TaskStatus.AWAITING_APPROVAL, validate=False, human_override=True)
            return t.id

    tid = asyncio.run(_seed())

    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=False: (_cfg(db), None)):
        result = CliRunner().invoke(cmd_mod.approve, [tid])
    assert result.exit_code == 0
    assert "approved" in result.output.lower()


# --------------------------------------------------------------------------- #
# C2. The landing module itself                                              #
#                                                                             #
# `approve_merge.land_task` is the one place the product performs a real      #
# merge, so the mark check lives at the act, not only in the CLI wrapper and  #
# HTTP middleware that call in — a marked session that drives the module in-  #
# process is refused before any state mutates.                                #
# --------------------------------------------------------------------------- #

def test_land_task_refuses_a_marked_caller(monkeypatch):
    from no_human.vcs import approve_merge

    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION_KIND, "claude")
    # A resolvable repo/branch is never reached: the mark check is the first
    # statement, before any git or config work. GitRepo would raise on this
    # path if it were reached, so ok=False with the refusal text proves the
    # refusal — not an incidental failure — is what returned.
    result = approve_merge.land_task(
        repo_path="/nonexistent/repo", branch="feature", pr_url="https://example/pr/1",
        task_id="t1", task_title="t", review_evidence="e", config={},
    )
    assert result.ok is False
    assert result.step == "preconditions"
    assert "refused" in result.stderr.lower()
    assert "claude" in result.stderr


def test_land_task_unmarked_passes_the_mark_check(monkeypatch):
    """Control: with no mark, land_task proceeds past the mark check into its
    ordinary preconditions — here `pr_url=""` returns the skipped no-PR result
    (ok=True), which is only reachable AFTER the mark gate."""
    from no_human.vcs import approve_merge

    monkeypatch.delenv(NO_HUMAN_AGENT_SESSION, raising=False)
    monkeypatch.delenv(NO_HUMAN_AGENT_SESSION_KIND, raising=False)
    result = approve_merge.land_task(
        repo_path="/tmp/x", branch="feature", pr_url="",
        task_id="t1", task_title="t", review_evidence="e", config={},
    )
    assert result.ok is True
    assert result.skipped is True


# --------------------------------------------------------------------------- #
# D. HTTP middleware                                                          #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config

    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


_GATE_ROUTES = ["/approve", "/approve-landed", "/finish-review", "/shipped"]
_NON_GATE_ROUTES = ["/pause", "/resume", "/cancel", "/retry", "/send-back", "/reply"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", _GATE_ROUTES)
async def test_gate_route_refuses_a_marked_header(client, suffix):
    r = await client.post(f"/api/tasks/nonexistent{suffix}",
                          headers={AGENT_SESSION_HEADER: "claude"})
    assert r.status_code == 403
    assert r.json() == {
        "error": "gate_refused",
        "reason": mock.ANY,
    }
    assert "operator-only" in r.json()["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", _GATE_ROUTES)
async def test_gate_route_refuses_the_servers_own_process_mark(client, suffix, monkeypatch):
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION_KIND, "codex")
    r = await client.post(f"/api/tasks/nonexistent{suffix}")
    assert r.status_code == 403
    assert r.json()["error"] == "gate_refused"


@pytest.mark.asyncio
async def test_gate_route_survives_path_spelling_tricks(client):
    """Repeated slashes, a trailing slash, and one level of percent-encoding
    all normalize to the same gate-ending path — the same class of dodge
    `guard.py`'s lexical check already resists."""
    variants = [
        "/api/tasks/nonexistent//approve",
        "/api/tasks/nonexistent/approve/",
        "/api/tasks/nonexistent/appr%6fve",  # %6f == 'o'
    ]
    for path in variants:
        r = await client.post(path, headers={AGENT_SESSION_HEADER: "claude"})
        assert r.status_code == 403, path
        assert r.json()["error"] == "gate_refused", path


@pytest.mark.asyncio
async def test_unmarked_gate_route_is_untouched(client):
    r = await client.post("/api/tasks/nonexistent/approve")
    assert r.status_code == 404
    assert r.json().get("detail") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", _NON_GATE_ROUTES)
async def test_non_gate_routes_are_unaffected_even_when_marked(client, suffix):
    r = await client.post(f"/api/tasks/nonexistent{suffix}",
                          headers={AGENT_SESSION_HEADER: "claude"})
    assert not (r.status_code == 403 and r.json().get("error") == "gate_refused"), \
        f"{suffix} was refused by the gate middleware but is not a gate-ending route"


@pytest.mark.asyncio
async def test_non_post_methods_on_a_gate_path_are_not_gate_refused(client, monkeypatch):
    """The middleware refuses the METHOD that performs the act, not the path.

    Both halves matter and both are measured here on a marked server (the
    strongest form of the mark — no header needed):

      * the CORS preflight `OPTIONS /api/tasks/{id}/approve` must still get
        its CORS answer. If the POST test is dropped, it becomes a 403 and
        the board's approve button fails as an opaque CORS error in the
        browser instead of the API's own JSON refusal on the POST that
        follows;
      * a GET of the same path is not the act; it falls through to the SPA
        catch-all, and must keep answering 404 rather than a gate refusal.

    Positive control in the same fixture: the POST *is* refused, so this
    test cannot pass by the middleware being inert.
    """
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION_KIND, "claude")

    refused = await client.post("/api/tasks/nonexistent/approve")
    assert refused.status_code == 403, "positive control: the POST must be refused"
    assert refused.json()["error"] == "gate_refused"

    preflight = await client.options(
        "/api/tasks/nonexistent/approve",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 200, (
        "the CORS preflight for a gate-ending route was answered "
        f"{preflight.status_code}, not 200 — the mark check is firing on a "
        "method that cannot end the gate"
    )

    read = await client.get("/api/tasks/nonexistent/approve")
    assert read.status_code == 404, (
        f"GET of a gate-ending path answered {read.status_code}; a read is "
        "not the act and must not be gate-refused"
    )


def test_every_gate_ending_route_is_post_only():
    """What makes the POST-only check SUFFICIENT: every route in the app whose
    path is gate-ending is declared POST-only. If a GET/PUT/DELETE gate-ending
    route is ever added, the middleware would silently stop covering it — this
    fails first.
    """
    gate_routes = [
        r for r in app.routes
        if getattr(r, "path", None) and _is_gate_ending_path(r.path)
    ]
    assert len(gate_routes) == len(_GATE_ENDING_SUFFIXES), (
        "expected exactly one route per gate-ending suffix, found "
        f"{sorted((r.path, sorted(r.methods)) for r in gate_routes)}"
    )
    for route in gate_routes:
        assert set(route.methods) == {"POST"}, (
            f"{route.path} is declared with methods {sorted(route.methods)}; "
            "the gate middleware only checks POST, so a non-POST gate-ending "
            "route would not be covered"
        )


def test_is_gate_ending_path_matches_only_the_four_suffixes():
    for suffix in _GATE_ENDING_SUFFIXES:
        assert _is_gate_ending_path(f"/api/tasks/abc123{suffix}")
    for suffix in _NON_GATE_ROUTES:
        assert not _is_gate_ending_path(f"/api/tasks/abc123{suffix}")
    assert not _is_gate_ending_path("/api/tasks")
    assert not _is_gate_ending_path("/api/tasks/abc123")


@pytest.mark.asyncio
async def test_spellings_where_the_two_layers_diverge_are_not_routable(client):
    """Honest bound on the "spelling doesn't matter" claim: it holds for the
    paths Starlette ROUTES, and there are two spellings where this predicate
    and `guard.py`'s regex disagree. Both are unroutable, which is why the
    disagreement is not a dodge — pinned so that stops being true loudly.

    `//api/tasks/x/approve`: `posixpath.normpath` preserves a doubled LEADING
    slash, so the predicate says False while `_GATE_PATH` matches.
    `/api/tasks/x/APPROVE`: `_GATE_PATH` is IGNORECASE, this predicate is a
    case-sensitive suffix test.
    """
    for path in ("//api/tasks/nonexistent/approve", "/api/tasks/nonexistent/APPROVE"):
        assert not _is_gate_ending_path(path), path
        assert guard_mod._GATE_PATH.search(path), (
            f"{path} is no longer matched by guard.py's lexical layer either — "
            "the last layer covering this spelling has gone"
        )
        r = await client.post(path, headers={AGENT_SESSION_HEADER: "claude"})
        assert r.status_code == 405, (
            f"{path} answered {r.status_code}: it now reaches a routed handler, "
            "so the middleware's blind spot on this spelling is exploitable"
        )


# --------------------------------------------------------------------------- #
# E. Drift: app.py's suffix list vs. guard.py's regex                         #
# --------------------------------------------------------------------------- #

def test_gate_ending_suffixes_agree_with_guards_gate_path():
    for suffix in _GATE_ENDING_SUFFIXES:
        path = f"/api/tasks/abc123{suffix}"
        assert guard_mod._GATE_PATH.search(path), (
            f"app.py names {suffix!r} as gate-ending but guard.py's _GATE_PATH "
            "does not recognise it — the two lists have drifted apart"
        )
    for suffix in _NON_GATE_ROUTES:
        path = f"/api/tasks/abc123{suffix}"
        assert not guard_mod._GATE_PATH.search(path), (
            f"guard.py's _GATE_PATH treats {suffix!r} as gate-ending but "
            "app.py's _GATE_ENDING_SUFFIXES does not — the two lists have "
            "drifted apart"
        )


# --------------------------------------------------------------------------- #
# F. NhClient / api_client.py wiring                                          #
# --------------------------------------------------------------------------- #

def _nh_client(handler, **kw) -> NhClient:
    return NhClient(transport=httpx.MockTransport(handler), **kw)


@pytest.mark.asyncio
async def test_nh_client_sends_no_mark_header_when_unmarked():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, json=[])

    async with _nh_client(handler) as c:
        await c.board()
    assert AGENT_SESSION_HEADER.lower() not in seen["headers"]


@pytest.mark.asyncio
async def test_nh_client_sends_the_mark_header_when_marked(monkeypatch):
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION, "1")
    monkeypatch.setenv(NO_HUMAN_AGENT_SESSION_KIND, "codex")
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, json=[])

    async with _nh_client(handler) as c:
        await c.board()
    assert seen["headers"][AGENT_SESSION_HEADER.lower()] == "codex"
