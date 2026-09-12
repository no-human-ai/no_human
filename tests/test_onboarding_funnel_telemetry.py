"""The onboarding-funnel telemetry blind spot (task c4873934 REFILE).

291 external installs produced only 6 `task_created` and 1 `task_completed`
event; 276 installs emitted exactly ONE event ever, with nothing recorded
between "app started" and "task created" (or nothing at all, for an install
that never got that far). This file pins the fix — four new events
(`onboarding_step_viewed`, `onboarding_repo_selected`, `onboarding_completed`,
`task_create_refused`) — against the acceptance criteria:

  AC1 abandonment vs. never-started, and refused-first-task vs. closed-window,
      must be distinguishable signals.
  AC2 no event/prop may carry a repo name, path, task id/title, branch, url,
      hostname, prompt, or free text — every new event/prop is a closed enum.
  AC3 no new event name reaches the deployed Lambda destination.
  AC4 no telemetry path spends credential quota or mutates the running
      process's exported credential/profile.
  AC5 per-entity events are emitted at most once per install.

Plus two tests pinning the DOCUMENTED (not fixed) desktop first-launch blind
spot, per the plan's explicit "park with a structured blocker, do not add a
probe" instruction — this file proves the server-side gap is closed and
that the desktop gap is real and stated, not silently left unaddressed.
"""
from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human import telemetry
from no_human.api import app as app_module
from no_human.api.app import app, SETUP_MODE_DETAIL, _WIZARD_STEPS, _FUNNEL_ONCE_KEY
from no_human.config import Config

# `_require_credentials` (create_task's refusal path) reaches
# `subscription_credential_missing` -> `load_env_token`, which reads the
# operator's real ~/.no_human/.env before falling back to the process env.
# Requested module-wide, never autouse — see conftest.py's isolated_env_file.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


_ENABLED_LAMBDA = {
    "enabled": True,
    "endpoint": "https://ingest.invalid/collect",
    "instance_id": "11111111-2222-3333-4444-555555555555",
    "posthog_publishable": "phc_test",
}

_ENABLED_POSTHOG = {
    "enabled": True,
    "posthog_publishable": "phc_test_publishable_token",
    "posthog_host": "https://us.i.posthog.com",
    "instance_id": "22222222-3333-4444-5555-666666666666",
}

_DISABLED = {"enabled": False}


# --------------------------------------------------------------------------- #
# fixtures — mirrors tests/test_telemetry.py's temp_home/no_network/no_thread,
# tests/test_onboarding_api.py's AsyncClient+ASGITransport+CONFIG_PATH wiring,
# and tests/test_setup_mode_boot.py's app.state.setup_mode handling.
# --------------------------------------------------------------------------- #

@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def _urlopen(req, timeout=None):
        calls.append((req, timeout))
        return contextlib.nullcontext()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return calls


@pytest.fixture
def no_thread(monkeypatch):
    """Make record() deterministic: no background flush thread in tests."""
    monkeypatch.setattr(telemetry, "_spawn_flush", lambda section: None)


def _no_ambient_token(monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _seed_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _queue_lines(temp_home) -> list[dict]:
    import json
    path = temp_home / ".no_human" / "telemetry-queue.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest_asyncio.fixture
async def client(store, tmp_path, temp_home, no_thread, monkeypatch):
    """A board wired exactly like the 291-install blind spot: subscription
    mode, NO credential on file (create_task's refusal path is reachable),
    telemetry enabled and lambda-routed by default. Individual tests mutate
    `app.state.config.data["telemetry"]` to swap destinations / disable."""
    _no_ambient_token(monkeypatch)
    app.state.store = store
    app.state.config = Config(
        data={
            "llm": {"auth_mode": "subscription"},
            "telemetry": dict(_ENABLED_LAMBDA),
        },
        path=tmp_path / "config.yaml",
    )
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    app.state.setup_mode = True
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://localhost",
            headers={"Origin": "http://127.0.0.1:8420"},
        ) as c:
            yield c
    finally:
        # `app` is the process-wide FastAPI singleton every test module that
        # imports it shares — see test_setup_mode_boot.py's `client` fixture.
        del app.state.setup_mode


# =========================================================================== #
# AC1 — abandonment vs. never-started; refused-first-task vs. closed-window   #
# =========================================================================== #

@pytest.mark.asyncio
async def test_abandon_midway_leaves_started_but_not_completed(client, temp_home):
    r = await client.post("/api/onboarding/step", json={"step": "welcome"})
    assert r.status_code == 200, r.text
    r = await client.post("/api/onboarding/step", json={"step": "repos"})
    assert r.status_code == 200, r.text
    # The wizard is abandoned here -- /api/onboarding/complete is never called.

    lines = _queue_lines(temp_home)
    names = [ln["name"] for ln in lines]
    assert names.count("onboarding_step_viewed") == 2
    assert "onboarding_completed" not in names
    steps_seen = {ln["props"]["step"] for ln in lines if ln["name"] == "onboarding_step_viewed"}
    assert steps_seen == {"welcome", "repos"}


@pytest.mark.asyncio
async def test_never_started_emits_nothing(client, temp_home):
    # No onboarding endpoint touched at all -- the app-started-only install.
    assert _queue_lines(temp_home) == []


@pytest.mark.asyncio
async def test_first_task_refused_emits_refusal(client, temp_home):
    r = await client.post("/api/tasks", json={"title": "Plain task"})
    assert r.status_code == 503
    assert "CLAUDE_CODE_OAUTH_TOKEN" in r.json()["detail"]

    lines = _queue_lines(temp_home)
    refusals = [ln for ln in lines if ln["name"] == "task_create_refused"]
    assert len(refusals) == 1
    assert refusals[0]["props"]["reason"] == "setup_mode"


@pytest.mark.asyncio
async def test_closed_window_is_not_a_refusal(client, temp_home):
    """A user who walks the wizard and then just closes the tab (never
    attempting to create a task) must leave a signal distinguishable from one
    who tried and was refused: no `task_create_refused` at all."""
    await client.post("/api/onboarding/step", json={"step": "welcome"})
    await client.post("/api/onboarding/step", json={"step": "repos"})
    await client.post("/api/onboarding/step", json={"step": "projects"})
    # No POST /api/tasks -- the window just closes.

    names = [ln["name"] for ln in _queue_lines(temp_home)]
    assert "task_create_refused" not in names
    assert names.count("onboarding_step_viewed") == 3


@pytest.mark.asyncio
async def test_refusal_detail_never_reaches_telemetry(client, temp_home):
    await client.post("/api/tasks", json={"title": "Plain task"})
    lines = _queue_lines(temp_home)
    refusal = next(ln for ln in lines if ln["name"] == "task_create_refused")
    # Only the machine-readable reason ships -- never the human-facing detail
    # string, which carries a filesystem `.env` path.
    assert set(refusal["props"]) <= {"reason", "environment"}
    assert refusal["props"]["reason"] == "setup_mode"
    blob = repr(refusal)
    assert SETUP_MODE_DETAIL not in blob
    assert ".env" not in blob
    assert "no_human" not in blob or "no_human" not in SETUP_MODE_DETAIL.replace(
        "no_human", ""
    )  # sanity: guards against a vacuous substring check
    assert "~/.no_human/.env" not in blob


# =========================================================================== #
# AC2 — every new event/prop is a closed enum; nothing free-text ever ships   #
# =========================================================================== #

def test_every_new_event_and_prop_is_enumerated():
    assert telemetry._ALLOWED_EVENTS["onboarding_step_viewed"] == frozenset({"step", "environment"})
    assert telemetry._ALLOWED_EVENTS["onboarding_repo_selected"] == frozenset({"environment"})
    assert telemetry._ALLOWED_EVENTS["onboarding_completed"] == frozenset({"path", "environment"})
    assert telemetry._ALLOWED_EVENTS["task_create_refused"] == frozenset({"reason", "environment"})

    assert telemetry.ONBOARDING_STEPS == frozenset(
        {"welcome", "repos", "projects", "integrations", "summary"}
    )
    assert telemetry.ONBOARDING_PATHS == frozenset({"minimal", "full"})
    assert telemetry.TASK_REFUSAL_REASONS == frozenset({"setup_mode"})

    assert telemetry._ALLOWED_PROP_VALUES[("onboarding_step_viewed", "step")] is telemetry.ONBOARDING_STEPS
    assert telemetry._ALLOWED_PROP_VALUES[("onboarding_completed", "path")] is telemetry.ONBOARDING_PATHS
    assert telemetry._ALLOWED_PROP_VALUES[("task_create_refused", "reason")] is telemetry.TASK_REFUSAL_REASONS
    # `onboarding_repo_selected` deliberately carries NO extra prop (no repo
    # name, no per-repo count) -- a per-repo count would itself be a
    # cardinality leak the plan explicitly rules out.
    assert not any(k[0] == "onboarding_repo_selected" for k in telemetry._ALLOWED_PROP_VALUES)


@pytest.mark.parametrize("bad_value", ["bogus", "", "Welcome", "welcome ", "repo_name"])
def test_unlisted_step_value_raises(bad_value):
    with pytest.raises(ValueError, match="not allowed"):
        telemetry.record(
            "onboarding_step_viewed", config={"telemetry": _ENABLED_LAMBDA}, step=bad_value
        )


@pytest.mark.parametrize("bad_path", ["full-custom", "Minimal", "", "/etc/passwd"])
def test_unlisted_onboarding_path_value_raises(bad_path):
    with pytest.raises(ValueError, match="not allowed"):
        telemetry.record(
            "onboarding_completed", config={"telemetry": _ENABLED_LAMBDA}, path=bad_path
        )


@pytest.mark.parametrize("bad_reason", ["no_token", "quota_exceeded", "", "SETUP_MODE_DETAIL"])
def test_unlisted_refusal_reason_value_raises(bad_reason):
    with pytest.raises(ValueError, match="not allowed"):
        telemetry.record(
            "task_create_refused", config={"telemetry": _ENABLED_LAMBDA}, reason=bad_reason
        )


def test_unlisted_prop_on_a_new_event_raises():
    with pytest.raises(ValueError, match="not allowed"):
        telemetry.record(
            "onboarding_repo_selected", config={"telemetry": _ENABLED_LAMBDA},
            repo="secret-repo-name",
        )
    with pytest.raises(ValueError, match="not allowed"):
        telemetry.record(
            "onboarding_completed", config={"telemetry": _ENABLED_LAMBDA},
            path="minimal", task_title="do the thing",
        )


@pytest.mark.asyncio
async def test_endpoint_422s_an_unlisted_step(client, temp_home):
    r = await client.post("/api/onboarding/step", json={"step": "not-a-real-step"})
    assert r.status_code == 422
    # And, crucially, the rejected step never reaches telemetry either.
    assert _queue_lines(temp_home) == []


def test_step_key_matches_the_wizards_own_list():
    """The server enum, the telemetry enum, and the frontend's own literal
    list must all agree -- a drift here would either 422 a legitimate step or
    silently accept one the wizard never shows."""
    assert set(_WIZARD_STEPS) == telemetry.ONBOARDING_STEPS
    js_path = Path(__file__).resolve().parent.parent / "web" / "src" / "onboardingFunnel.js"
    js_src = js_path.read_text(encoding="utf-8")
    # Anchored at a line start with MULTILINE: the unanchored form took
    # `re.search`'s FIRST match, so a comment carrying an out-of-date
    # literal above the real export satisfied it while the real list had
    # diverged. Demonstrated with a planted decoy.
    m = re.search(r'^export const FUNNEL_STEPS\s*=\s*\[([^\]]*)\]',
                  js_src, re.MULTILINE)
    assert m, "FUNNEL_STEPS literal not found in onboardingFunnel.js"
    js_steps = [s.strip().strip('"').strip("'") for s in m.group(1).split(",") if s.strip()]
    assert set(js_steps) == set(_WIZARD_STEPS)
    assert tuple(js_steps) == _WIZARD_STEPS  # order matches too


# =========================================================================== #
# AC3 — no new event name reaches the deployed Lambda destination            #
# =========================================================================== #

@pytest.mark.asyncio
async def test_lambda_wire_carries_no_onboarding_event(client, temp_home, no_network):
    await client.post("/api/onboarding/step", json={"step": "welcome"})
    await client.post("/api/onboarding/repos/onboard", json={"repo_path": "not-checked-yet"})
    n = telemetry.flush(_ENABLED_LAMBDA)
    assert n == 0
    assert no_network == []
    # And the all-onboarding batch is DELETED, not left wedged.
    assert _queue_lines(temp_home) == []


@pytest.mark.asyncio
async def test_lambda_batch_of_only_onboarding_events_drains(client, temp_home, no_network):
    """An all-dropped batch must not poison the queue for a later, legitimate
    event -- mirrors test_telemetry.py's own round-4 wedge regression test."""
    await client.post("/api/onboarding/step", json={"step": "welcome"})
    assert telemetry.flush(_ENABLED_LAMBDA) == 0
    assert no_network == []

    telemetry.record("app_started", config={"telemetry": _ENABLED_LAMBDA})
    assert telemetry.flush(_ENABLED_LAMBDA) == 1
    assert len(no_network) == 1


@pytest.mark.asyncio
async def test_positive_control_an_allowed_name_does_reach_the_wire(temp_home, no_thread, no_network):
    """Proves the interceptor itself actually sees real traffic -- without
    this, the two tests above proving `no_network == []` would be vacuous."""
    telemetry.record("app_started", config={"telemetry": _ENABLED_LAMBDA})
    n = telemetry.flush(_ENABLED_LAMBDA)
    assert n == 1
    assert len(no_network) == 1
    req, _timeout = no_network[0]
    assert req.full_url == _ENABLED_LAMBDA["endpoint"]


@pytest.mark.asyncio
async def test_posthog_wire_does_carry_the_new_events(temp_home, no_thread, no_network):
    telemetry.record("onboarding_completed", config={"telemetry": _ENABLED_POSTHOG}, path="minimal")
    n = telemetry.flush(_ENABLED_POSTHOG)
    assert n == 1
    assert len(no_network) == 1
    req, _timeout = no_network[0]
    body = req.data.decode("utf-8")
    assert "onboarding_completed" in body


# =========================================================================== #
# AC4 — no telemetry path spends credential quota or mutates the running     #
# process's exported credential/profile                                     #
# =========================================================================== #

@pytest.mark.parametrize("telemetry_section", [dict(_ENABLED_LAMBDA), dict(_DISABLED)])
@pytest.mark.asyncio
async def test_no_probe_counter_with_telemetry_disabled(
    client, temp_home, tmp_path, monkeypatch, telemetry_section
):
    """`/api/onboarding/step`, `/repos/onboard`, and `/complete` never call
    `_require_credentials`/`load_env_token` at all -- the probe counter must
    read 0 whether telemetry is enabled or disabled."""
    calls = []
    real_load_env_token = nh_config.load_env_token

    def _counting_load_env_token(*args, **kwargs):
        calls.append((args, kwargs))
        return real_load_env_token(*args, **kwargs)

    monkeypatch.setattr(nh_config, "load_env_token", _counting_load_env_token)
    app.state.config.data["telemetry"] = telemetry_section

    _seed_repo(tmp_path / "repo")
    await client.post("/api/onboarding/step", json={"step": "welcome"})
    r = await client.post(
        "/api/onboarding/repos/onboard", json={"repo_path": str(tmp_path / "repo")}
    )
    assert r.status_code == 200, r.text
    await client.post("/api/onboarding/complete", json={
        "team": "solo", "repos": [], "docs": [], "telemetry_asked": True,
    })

    assert calls == []


@pytest.mark.asyncio
async def test_environment_and_profile_byte_identical_across_onboarding_requests(
    client, tmp_path
):
    before_token = os.environ.get(nh_config.SUBSCRIPTION_TOKEN_VAR)
    before_profile = nh_config.active_auth_profile()
    before_env = dict(os.environ)

    _seed_repo(tmp_path / "repo2")
    await client.post("/api/onboarding/step", json={"step": "welcome"})
    await client.post(
        "/api/onboarding/repos/onboard", json={"repo_path": str(tmp_path / "repo2")}
    )
    await client.post("/api/onboarding/complete", json={
        "team": "solo", "repos": [], "docs": [], "telemetry_asked": True,
    })

    assert os.environ.get(nh_config.SUBSCRIPTION_TOKEN_VAR) == before_token
    assert nh_config.active_auth_profile() == before_profile
    assert dict(os.environ) == before_env


@pytest.mark.asyncio
async def test_refusal_path_does_not_mutate_credential_or_profile(client):
    """create_task's PRE-EXISTING `_require_credentials` call (not new in
    this change) does reach `load_env_token`, but with no ambient token it
    returns None without mutating `os.environ`/`_ACTIVE_AUTH_PROFILE` --
    `load_env_token`'s mutation only happens on its truthy-token branch."""
    before_token = os.environ.get(nh_config.SUBSCRIPTION_TOKEN_VAR)
    before_profile = nh_config.active_auth_profile()

    r = await client.post("/api/tasks", json={"title": "Plain task"})
    assert r.status_code == 503

    assert os.environ.get(nh_config.SUBSCRIPTION_TOKEN_VAR) == before_token
    assert nh_config.active_auth_profile() == before_profile


@pytest.mark.asyncio
async def test_no_new_network_egress_beyond_the_telemetry_flush(client, temp_home, no_network, tmp_path):
    """Onboarding requests themselves must never touch the network -- only a
    deliberate `telemetry.flush()` call (exercised separately above) does."""
    _seed_repo(tmp_path / "repo3")
    await client.post("/api/onboarding/step", json={"step": "welcome"})
    await client.post(
        "/api/onboarding/repos/onboard", json={"repo_path": str(tmp_path / "repo3")}
    )
    await client.post("/api/onboarding/complete", json={
        "team": "solo", "repos": [], "docs": [], "telemetry_asked": True,
    })
    await client.post("/api/tasks", json={"title": "Plain task"})

    assert no_network == []


# =========================================================================== #
# AC5 — per-entity events fire at most once per install                      #
# =========================================================================== #

@pytest.mark.asyncio
async def test_repo_selected_is_emitted_once_for_seven_repos(client, temp_home, tmp_path):
    for i in range(7):
        repo = _seed_repo(tmp_path / f"r{i}")
        r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
        assert r.status_code == 200, r.text

    names = [ln["name"] for ln in _queue_lines(temp_home)]
    assert names.count("onboarding_repo_selected") == 1


@pytest.mark.asyncio
async def test_positive_control_the_counter_can_exceed_one(client, temp_home):
    """Proves the once-per-MARKER latch mechanism itself is not stuck at a
    hardcoded 1 -- distinct markers (one per wizard step) each latch and fire
    independently, so a wire count of exactly 1 for repo_selected above is a
    real "once per install" signal, not a broken counter."""
    for step in _WIZARD_STEPS:
        r = await client.post("/api/onboarding/step", json={"step": step})
        assert r.status_code == 200, r.text

    names = [ln["name"] for ln in _queue_lines(temp_home)]
    assert names.count("onboarding_step_viewed") == len(_WIZARD_STEPS) > 1


@pytest.mark.asyncio
async def test_latch_survives_restart(client, temp_home, tmp_path):
    repo = _seed_repo(tmp_path / "restart-repo")
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    assert [ln["name"] for ln in _queue_lines(temp_home)].count("onboarding_repo_selected") == 1

    # Simulate a fresh process: rebuild app.state.config from the config.yaml
    # _persist_onboarding actually wrote to disk, rather than reusing the
    # in-memory object.
    import yaml
    on_disk = yaml.safe_load(nh_config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert on_disk["onboarding"][_FUNNEL_ONCE_KEY] == ["repo_selected"]
    app.state.config = Config(
        data={**on_disk, "telemetry": dict(_ENABLED_LAMBDA)},
        path=nh_config.CONFIG_PATH,
    )

    repo2 = _seed_repo(tmp_path / "restart-repo-2")
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo2)})
    assert r.status_code == 200, r.text
    assert [ln["name"] for ln in _queue_lines(temp_home)].count("onboarding_repo_selected") == 1


@pytest.mark.asyncio
async def test_step_viewed_deduped_per_install(client, temp_home):
    # forward -> back -> forward through the same step key, like React
    # revisiting `welcome` after the user clicks Back.
    for step in ["welcome", "repos", "welcome", "repos", "welcome"]:
        r = await client.post("/api/onboarding/step", json={"step": step})
        assert r.status_code == 200, r.text

    names = [ln["name"] for ln in _queue_lines(temp_home)]
    assert names.count("onboarding_step_viewed") == 2  # welcome, repos -- each once


# =========================================================================== #
# Desktop first-launch blind spot -- documented, deliberately NOT fixed       #
# =========================================================================== #

@pytest.mark.asyncio
async def test_server_boots_and_emits_with_no_credential(client, temp_home):
    """The exact scenario behind the 291-install blind spot: subscription
    mode, no credential on file. The SERVER-side board (as opposed to the
    desktop pre-credential native screen) must still serve onboarding and
    still emit funnel telemetry -- this is the part the fix actually closes."""
    r = await client.post("/api/onboarding/step", json={"step": "welcome"})
    assert r.status_code == 200, r.text
    names = [ln["name"] for ln in _queue_lines(temp_home)]
    assert names == ["onboarding_step_viewed"]


def test_desktop_first_launch_gate_is_documented_as_uninstrumented():
    """`desktop/main.mjs`'s pre-credential native setup screen returns before
    `ensureServer(...)` ever boots a server -- so no server-side telemetry
    (not even `app_started`) is reachable on that path. This is a documented,
    accepted gap (OUT OF SCOPE: do not touch desktop/main.mjs), pinned here as
    a STATIC source check plus a docs assertion so it can't silently regress
    into "fixed" without anyone updating the doc."""
    main_mjs = Path(__file__).resolve().parent.parent / "desktop" / "main.mjs"
    src = main_mjs.read_text(encoding="utf-8")
    m = re.search(r"if\s*\(!hasCredential\(\)\)\s*\{[^}]*?\}", src, re.S)
    assert m, "the no-credential branch in _loadBoardOrError was not found"
    branch = m.group(0)
    assert "showSetup(" in branch
    assert "return" in branch
    # ensureServer must be textually AFTER this branch in the same function,
    # i.e. never reached on this path.
    assert src.index("ensureServer(") > src.index(m.group(0))

    doc = (Path(__file__).resolve().parent.parent / "docs" / "TELEMETRY.md").read_text(
        encoding="utf-8"
    )
    assert "desktop first-launch blind spot" in doc.lower()


@pytest.mark.asyncio
async def test_completing_the_wizard_emits_completed_once_with_the_full_path(
    client, temp_home, tmp_path
):
    """The POSITIVE twin of `test_abandon_midway_leaves_started_but_not_completed`.

    That test asserts `"onboarding_completed" not in names`, and nothing
    anywhere asserted the name ever APPEARS -- an absence assertion whose
    instrument had never been shown able to produce the thing. Deleting the
    emit in `onboarding_complete` left 127 tests green across every file in the
    repo that mentions the event or the endpoint.

    Abandonment-vs-completion is the ticket's whole point, so the two runs must
    differ ON THE WIRE, which is what this pins.
    """
    _seed_repo(tmp_path / "done-repo")
    await client.post("/api/onboarding/step", json={"step": "welcome"})
    r = await client.post("/api/onboarding/complete", json={
        "team": "solo", "repos": [], "docs": [], "telemetry_asked": True,
    })
    assert r.status_code == 200, r.text

    lines = _queue_lines(temp_home)
    names = [ln["name"] for ln in lines]
    assert names.count("onboarding_completed") == 1, names
    # The test's NAME says "with the full path", so it has to assert it.
    # Without this, inverting the two literals left this test green and only
    # its sibling caught the swap -- a name claiming more than the body checks.
    completed = next(ln for ln in lines if ln["name"] == "onboarding_completed")
    assert completed["props"]["path"] == "full", completed["props"]
    # Control: the step event is on the same wire, so an empty/!-matching
    # queue cannot be what makes the assertion above pass.
    assert names.count("onboarding_step_viewed") == 1, names


@pytest.mark.asyncio
async def test_the_completed_path_prop_distinguishes_minimal_from_full(
    client, temp_home, tmp_path
):
    """`path=("minimal" if body.minimal else "full")` had no coverage through
    the endpoint -- inverting the two literals left 127 tests green, because
    the only test touching `path` calls `telemetry.record(..., path="minimal")`
    directly and so pins the enum, not the mapping. Inverted, every install
    would report the wrong wizard exit, permanently, and the data would look
    entirely plausible.
    """
    repo = _seed_repo(tmp_path / "minimal-repo")
    r = await client.post("/api/onboarding/complete", json={
        "team": "solo", "repos": [], "docs": [], "telemetry_asked": True,
        "minimal": True, "repo_path": str(repo),
    })
    assert r.status_code == 200, r.text

    completed = [ln for ln in _queue_lines(temp_home)
                 if ln["name"] == "onboarding_completed"]
    assert len(completed) == 1, completed
    assert completed[0]["props"]["path"] == "minimal", completed[0]["props"]


@pytest.mark.asyncio
async def test_the_marker_is_latched_before_the_event_is_recorded(
    client, temp_home, tmp_path, monkeypatch
):
    """`_record_onboarding_once`'s docstring claims the marker is persisted
    BEFORE `telemetry.record()`, so a failing telemetry call can only
    under-count, never duplicate. Swapping the two statements left 90 tests
    green -- a claim in shipped source with no coverage.

    The discriminator: make the FIRST record raise and the second succeed.
    Persist-before latches on call 1, so call 2 emits nothing. Persist-after
    would raise before latching, and call 2 would emit -- a duplicate for one
    install, which is exactly what the docstring promises cannot happen.
    """
    # `app.py` imports telemetry function-locally (`from .. import telemetry
    # as _telemetry`), so there is no module attribute to patch -- patch the
    # telemetry module itself, which every local alias resolves to.
    calls = {"n": 0}
    real = telemetry.record

    def flaky(kind, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("telemetry backend down")
        return real(kind, **kw)

    monkeypatch.setattr(telemetry, "record", flaky)

    body = {"team": "solo", "repos": [], "docs": [], "telemetry_asked": True}
    r1 = await client.post("/api/onboarding/complete", json=body)
    assert r1.status_code == 200, r1.text        # the failure is swallowed
    r2 = await client.post("/api/onboarding/complete", json=body)
    assert r2.status_code == 200, r2.text

    assert calls["n"] == 1, (
        "the second call reached telemetry.record, so the marker was not "
        "latched before the first (failing) record -- the docstring's "
        "under-count-never-duplicate guarantee does not hold"
    )
    names = [ln["name"] for ln in _queue_lines(temp_home)]
    assert names.count("onboarding_completed") == 0, names


@pytest_asyncio.fixture
async def credentialed_client(store, tmp_path, temp_home, no_thread, monkeypatch):
    """The same board, but WITH a credential on file.

    Every other test here drives a board with no credential, so a refused
    `POST /api/tasks` and a successful one are indistinguishable to the suite.
    That let a real mutation through: moving the `task_create_refused` emit out
    of `create_task`'s `except HTTPException:` so it fires on SUCCESS left all
    151 tests green. Every install that ever created a task would then have
    reported a refusal -- data that looks entirely plausible.
    """
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-probe")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app.state.store = store
    app.state.config = Config(
        data={"llm": {"auth_mode": "subscription"}, "telemetry": dict(_ENABLED_LAMBDA)},
        path=tmp_path / "config.yaml",
    )
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    app.state.setup_mode = True
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost",
            headers={"Origin": "http://127.0.0.1:8420"},
        ) as c:
            yield c
    finally:
        del app.state.setup_mode


@pytest.mark.asyncio
async def test_a_task_that_is_actually_created_does_not_report_a_refusal(
    credentialed_client, temp_home, tmp_path
):
    """AC1's headline property: refused-first-task and closed-window must be
    distinguishable. A successful create must emit NOTHING."""
    repo = _seed_repo(tmp_path / "real-repo")
    r = await credentialed_client.post(
        "/api/tasks", json={"title": "a real task", "repo_path": str(repo)}
    )
    assert r.status_code == 201, (
        f"the credentialed fixture did not actually lift the credential gate "
        f"({r.status_code}) -- without a real 201 this test proves nothing. {r.text[:200]}"
    )
    names = [ln["name"] for ln in _queue_lines(temp_home)]
    assert "task_create_refused" not in names, (
        f"a SUCCESSFUL create reported a refusal: {names}"
    )


@pytest.mark.asyncio
async def test_the_refusal_still_fires_when_the_create_is_actually_refused(
    client, temp_home, tmp_path
):
    """The positive control for the test above: without a credential the same
    call IS refused and DOES emit. Without this pair, a mutation that deletes
    the emit entirely would satisfy the assertion above."""
    repo = _seed_repo(tmp_path / "refused-repo")
    r = await client.post(
        "/api/tasks", json={"title": "a refused task", "repo_path": str(repo)}
    )
    assert r.status_code == 503, r.text
    names = [ln["name"] for ln in _queue_lines(temp_home)]
    assert names.count("task_create_refused") == 1, names
