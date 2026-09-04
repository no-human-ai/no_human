"""`nh start` in subscription mode with no credential on file must boot into a
restricted SETUP MODE instead of refusing outright — a new user has no way to
add the credential (`claude setup-token` + `~/.no_human/.env`) if the board
that would walk them through it never comes up.

Setup mode's contract, exercised end to end here:
  * the board still serves onboarding, Settings and ``/api/version``;
  * anything that spends tokens (task create, split, grill) is refused with a
    503 naming the missing env var, not silently degraded;
  * ``config.assert_subscription_mode`` stays the gate for RUNNING tasks — the
    scheduler idles on a missing credential instead of crash-looping;
  * a credential added later (env file edited, or a fresh boot) lifts the
    restriction — startup/per-call re-probing is sufficient, no live-reload
    infrastructure is required (INTAKE Q&A resolution);
  * every existing scrub (``scrub_metered_auth``, the strict
    ``ANTHROPIC_API_KEY``-present refusal) keeps running exactly as before —
    setup mode is entered ONLY for "no credential at all", never for a
    misconfiguration that used to be, and must stay, fatal.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import no_human
from no_human.api.app import app
from no_human.config import (
    Config,
    MissingCredentialError,
    assert_subscription_mode,
)
from no_human.core.db import Store
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus

# `config.load_env_var`/`load_env_token` read the OPERATOR'S real
# ~/.no_human/.env before the process env (see tests/conftest.py's
# `isolated_env_file`) — every test in this module reaches that path via
# `subscription_credential_missing`/`assert_subscription_mode`, so it is
# requested module-wide rather than per test.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


def _no_ambient_token(monkeypatch) -> None:
    """Strip any credential the *process* env might be carrying too — the
    isolated `.env` file alone is not enough, `load_env_token` falls back to
    `os.environ`."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    """A board with a subscription-mode config and NO credential on file."""
    _no_ambient_token(monkeypatch)
    app.state.store = store
    app.state.config = Config(
        data={"llm": {"auth_mode": "subscription"}},
        path=tmp_path / "config.yaml",
    )
    # Freshly built app.state.setup_mode is computed live by every gated
    # endpoint (`_require_credentials`) and by `show_config` — no lifespan
    # dependency — but start from a clean slate so a leftover flag from a
    # different test module's direct `app.state` mutation can't leak in.
    app.state.setup_mode = True
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c
    finally:
        # `app` is the process-wide FastAPI singleton every test file that
        # imports it shares — leaving `setup_mode` set here would leak into
        # a later test (in ANY file, under xdist) that never opts into
        # setup-mode tracking and relies on `_require_credentials`'/
        # `show_config`'s `hasattr(state, "setup_mode")` check reading as
        # "never wired" to stay ungated, e.g. tests/test_api.py's `client`.
        del app.state.setup_mode


@pytest_asyncio.fixture
async def client_with_credential(store, tmp_path, monkeypatch, isolated_env_file):
    """Same board, but a subscription token IS on file."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    isolated_env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=has-a-token\n")
    isolated_env_file.chmod(0o600)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)  # file must be enough
    app.state.store = store
    app.state.config = Config(
        data={"llm": {"auth_mode": "subscription"}},
        path=tmp_path / "config.yaml",
    )
    app.state.setup_mode = False
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as c:
            yield c
    finally:
        del app.state.setup_mode  # see `client`'s teardown for why


# --------------------------------------------------------------------------- #
# 1. The board still serves onboarding-critical endpoints                     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_app_with_no_credential_serves_version_and_flags_setup_mode(client):
    r = await client.get("/api/version")
    assert r.status_code == 200
    assert r.json()["version"] == no_human.__version__

    r = await client.get("/api/config")
    assert r.status_code == 200
    assert r.json()["setup_mode"] is True


# --------------------------------------------------------------------------- #
# 2. `nh start`'s bootstrap boots through instead of sys.exit(2)              #
# --------------------------------------------------------------------------- #

def test_start_bootstrap_does_not_exit_without_a_credential(tmp_path, monkeypatch):
    import inspect

    import no_human.cli.commands as cmd_mod

    _no_ambient_token(monkeypatch)
    cfg = Config(
        data={"llm": {"auth_mode": "subscription"}}, path=tmp_path / "config.yaml"
    )
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)

    # No SystemExit — this used to be `sys.exit(2)`.
    config, _report = cmd_mod._bootstrap(allow_setup_mode=True)
    assert config is cfg

    reason = cmd_mod._server_setup_reason(config)
    assert reason and "CLAUDE_CODE_OAUTH_TOKEN" in reason

    # Structural guard: `start()` must actually opt in with
    # `allow_setup_mode=True` — a caller that reverted to the bare
    # `_bootstrap()` call would still exit 2 and this test's own two
    # assertions above would say nothing about the real CLI path.
    src = inspect.getsource(cmd_mod.start.callback)
    assert "_bootstrap(allow_setup_mode=True)" in src, (
        "start() must call _bootstrap(allow_setup_mode=True) to boot into "
        "setup mode instead of exiting when no credential is on file"
    )


# --------------------------------------------------------------------------- #
# 3 & 4. Anything that spends tokens is refused with a clear, actionable 503  #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_task_create_in_setup_mode_returns_setup_error(client):
    r = await client.post("/api/tasks", json={"title": "Plain task"})
    assert r.status_code == 503
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/grill", {"title": "Plain task"}),
        ("/api/tasks/does-not-exist/split", {"drafts": []}),
    ],
)
async def test_grill_and_split_are_refused_in_setup_mode(client, path, body):
    r = await client.post(path, json=body)
    assert r.status_code == 503
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# 5. The scheduler idles instead of crash-looping without a credential        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_scheduler_tick_idles_instead_of_crashing_without_a_credential(store):
    t = Task.new("t-setup-mode", repo_path="/tmp/repo")
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    assert t.status == TaskStatus.PENDING

    events: list[tuple[str, str]] = []

    def _auth_check() -> None:
        raise MissingCredentialError("No subscription token found.")

    sched = Scheduler(
        store,
        lambda *a, **kw: None,  # orchestrator_factory: never reached
        on_event=lambda kind, text: events.append((kind, text)),
        auth_check=_auth_check,
    )

    started = await sched.tick()
    assert started == []
    assert sched._inflight == set()
    assert [k for k, _ in events].count("setup_required") == 1

    # A second tick with the SAME reason must not spam a second advisory.
    started2 = await sched.tick()
    assert started2 == []
    assert [k for k, _ in events].count("setup_required") == 1


# --------------------------------------------------------------------------- #
# 6. A credential added later lifts the restriction — no restart required     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_credential_added_lifts_the_restriction(client, isolated_env_file, monkeypatch):
    r = await client.post("/api/tasks", json={"title": "Plain task"})
    assert r.status_code == 503

    isolated_env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=freshly-added\n")
    isolated_env_file.chmod(0o600)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    r = await client.post("/api/tasks", json={"title": "Plain task"})
    assert r.status_code == 201

    r = await client.get("/api/config")
    assert r.json()["setup_mode"] is False


@pytest.mark.asyncio
async def test_scheduler_resumes_once_auth_check_stops_raising(store):
    t = Task.new("t-resume", repo_path="/tmp/repo")
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)

    events: list[tuple[str, str]] = []
    failing = {"value": True}

    def _auth_check() -> None:
        if failing["value"]:
            raise MissingCredentialError("No subscription token found.")

    sched = Scheduler(
        store,
        lambda *a, **kw: None,
        on_event=lambda kind, text: events.append((kind, text)),
        auth_check=_auth_check,
    )

    assert await sched.tick() == []
    assert [k for k, _ in events].count("setup_required") == 1

    failing["value"] = False
    started = await sched.tick()
    # Dispatch is no longer blocked by the gate — the task the fixture
    # seeded is claimable, so this tick reaches past the new check.
    assert started == [t.id] or t.id in sched._inflight
    assert [k for k, _ in events].count("setup_complete") == 1


# --------------------------------------------------------------------------- #
# 7. With a credential present, nothing is gated                              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_with_a_credential_present_nothing_is_gated(client_with_credential):
    r = await client_with_credential.post("/api/tasks", json={"title": "Plain task"})
    assert r.status_code == 201

    r = await client_with_credential.get("/api/config")
    assert r.status_code == 200
    assert r.json()["setup_mode"] is False


# --------------------------------------------------------------------------- #
# 8. The metered-auth scrub still runs on the missing-credential path         #
# --------------------------------------------------------------------------- #

def test_scrub_still_runs_on_the_missing_credential_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    with pytest.raises(MissingCredentialError, match="No subscription token"):
        assert_subscription_mode(env_path=tmp_path / "nope.env")

    assert "ANTHROPIC_AUTH_TOKEN" not in __import__("os").environ
