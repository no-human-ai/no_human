"""Integration health probes — boot-time + scheduled (integrations/health.py).

Mirrors the monkeypatch idiom already established by
tests/test_integrations_registry.py: `no_human.integrations._http_get` /
`_http_post` are swapped for fakes, so nothing here ever touches the network.
"""
from __future__ import annotations

import copy
import logging
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human import integrations as reg
from no_human.config import DEFAULT_CONFIG
from no_human.integrations import health as h

# health.py's _load_secrets() reaches config.load_env_var, which reads the
# operator's real ~/.no_human/.env BEFORE the process env. Requested by NAME
# through `usefixtures` — never an autouse marker; see tests/conftest.py and
# tests/test_api.py, which uses the same pattern for the same reason.
pytestmark = pytest.mark.usefixtures("isolated_env_file")

_JIRA_CFG = {
    "integrations": {
        "jira": {
            "enabled": True,
            "site": "https://acme.atlassian.net",
            "project_key": "P",
            "email": "me@example.com",
        },
    },
    "notifications": {},
    "ci": {},
}


@pytest.fixture(autouse=True)
def _clear_health_results():
    h._RESULTS.clear()
    yield
    h._RESULTS.clear()


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


# --------------------------------------------------------------------------- #
# probe()                                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_probe_healthy_on_200(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    async def fake_get(url, headers=None, auth=None, timeout=None):
        return _Resp(200, {"displayName": "Dana"})

    monkeypatch.setattr(reg, "_http_get", fake_get)
    result = await h.probe("jira", _JIRA_CFG)
    assert result.healthy is True
    assert result.checked_at


@pytest.mark.asyncio
async def test_probe_unhealthy_401_detail_has_status_and_host(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    async def fake_get(url, headers=None, auth=None, timeout=None):
        return _Resp(401)

    monkeypatch.setattr(reg, "_http_get", fake_get)
    result = await h.probe("jira", _JIRA_CFG)
    assert result.healthy is False
    assert "401" in result.detail
    assert "acme.atlassian.net" in result.detail


@pytest.mark.asyncio
async def test_probe_unhealthy_404_detail_has_status_and_host(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    async def fake_get(url, headers=None, auth=None, timeout=None):
        return _Resp(404)

    monkeypatch.setattr(reg, "_http_get", fake_get)
    result = await h.probe("jira", _JIRA_CFG)
    assert result.healthy is False
    assert "404" in result.detail
    assert "acme.atlassian.net" in result.detail


@pytest.mark.asyncio
async def test_probe_dns_error_and_timeout_are_captured_not_raised(monkeypatch):
    import httpx

    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    async def fake_get_connect_error(url, headers=None, auth=None, timeout=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(reg, "_http_get", fake_get_connect_error)
    result = await h.probe("jira", _JIRA_CFG)
    assert result.healthy is False
    assert "ConnectError" in result.detail

    async def fake_get_timeout(url, headers=None, auth=None, timeout=None):
        raise TimeoutError("boom")

    monkeypatch.setattr(reg, "_http_get", fake_get_timeout)
    result2 = await h.probe("jira", _JIRA_CFG)
    assert result2.healthy is False
    assert "timeout" in result2.detail.lower()


@pytest.mark.asyncio
async def test_probe_timeout_value_is_five_seconds(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    captured = {}

    async def fake_get(url, headers=None, auth=None, timeout=None):
        captured["timeout"] = timeout
        return _Resp(200, {})

    monkeypatch.setattr(reg, "_http_get", fake_get)
    await h.probe("jira", _JIRA_CFG)
    assert captured["timeout"] == h._PROBE_TIMEOUT == 5.0


@pytest.mark.asyncio
async def test_detail_never_contains_a_credential(monkeypatch, caplog):
    token = "SENTINELTOKENVALUE123"
    monkeypatch.setenv("JIRA_API_TOKEN", token)

    # A crafted exception whose class NAME embeds the token — the one thing
    # `_check_jira` echoes into `detail` on a transport failure
    # (`type(exc).__name__`). Proves `_scrub` catches a credential even if it
    # rides through that path; nothing today deliberately puts a secret there.
    LeakyExc = type(f"Boom{token}Boom", (Exception,), {})

    async def fake_get(url, headers=None, auth=None, timeout=None):
        raise LeakyExc("boom")

    monkeypatch.setattr(reg, "_http_get", fake_get)
    with caplog.at_level(logging.WARNING):
        results = await h.probe_all(_JIRA_CFG)
    jira_result = next(r for r in results if r.name == "jira")
    assert jira_result.healthy is False
    assert token not in jira_result.detail
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_jira_wrong_tenant_404_is_actionable(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    async def fake_get(url, headers=None, auth=None, timeout=None):
        return _Resp(404)

    monkeypatch.setattr(reg, "_http_get", fake_get)
    result = await h.probe("jira", _JIRA_CFG)
    assert result.healthy is False
    assert "acme.atlassian.net" in result.detail
    assert "tenant" in result.detail.lower() or "integrations.jira.site" in result.detail


# --------------------------------------------------------------------------- #
# probe_targets() / probe_all()                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_disabled_integration_is_never_probed(monkeypatch):
    cfg = {
        "integrations": {
            "jira": {
                "enabled": False,
                "site": "https://acme.atlassian.net",
                "project_key": "P",
                "email": "me@example.com",
            },
        },
        "notifications": {},
        "ci": {},
    }

    def boom(*a, **k):
        pytest.fail("must not call the network for a disabled integration")

    monkeypatch.setattr(reg, "_http_get", boom)
    monkeypatch.setattr(reg, "_http_post", boom)

    assert "jira" not in h.probe_targets(cfg)
    results = await h.probe_all(cfg)
    assert all(r.name != "jira" for r in results)


# --------------------------------------------------------------------------- #
# enabled-but-unconfigured must never report Failing (fresh-install bug)      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_default_config_has_no_failing_integration(monkeypatch, caplog):
    """A brand-new install (DEFAULT_CONFIG, no user edits) must show NO
    integration in a red 'Failing' state — Teams is enabled by default (see
    config.py) but has no webhook, so it must probe as neutral/unconfigured,
    not FAILING."""
    async def boom_get(*a, **k):
        pytest.fail("must not call the network for the default config")

    async def boom_post(*a, **k):
        pytest.fail("must not call the network for the default config")

    monkeypatch.setattr(reg, "_http_get", boom_get)
    monkeypatch.setattr(reg, "_http_post", boom_post)

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    with caplog.at_level(logging.WARNING, logger="no_human.integrations.health"):
        results = await h.probe_all(cfg)

    assert all(r.healthy is not False for r in results)
    assert "is FAILING" not in caplog.text

    teams_result = next(r for r in results if r.name == "teams")
    assert teams_result.healthy is None
    assert "not configured" in teams_result.detail

    # UI-contract check at the data level: `healthBadge()` in
    # web/src/integrationChip.js only turns red when `healthy === false`.
    statuses = {s.name: s for s in reg.list_integrations_with_health(cfg)}
    teams_status = statuses["teams"]
    assert teams_status.healthy is None
    assert teams_status.status == "unconfigured"


@pytest.mark.asyncio
async def test_configured_teams_with_retired_connector_still_fails(monkeypatch):
    """Positive control: a Teams webhook that IS configured but points at a
    retired Office 365 connector must still probe as Failing — the
    unconfigured -> neutral fix must not blanket-suppress a real failure."""
    cfg = {
        "integrations": {"teams": {"enabled": True}},
        "notifications": {
            "teams_webhook_url": "https://acme.webhook.office.com/webhookb2/xyz",
        },
        "ci": {},
    }

    async def boom(*a, **k):
        pytest.fail("Teams has no live ping — must not touch the network")

    monkeypatch.setattr(reg, "_http_get", boom)
    monkeypatch.setattr(reg, "_http_post", boom)

    result = await h.probe("teams", cfg)
    assert result.healthy is False
    assert "retired" in result.detail.lower()


@pytest.mark.asyncio
async def test_configured_jira_http_401_still_fails(monkeypatch):
    """Positive control: a fully-configured Jira with a real credential
    failure must still probe as Failing — guards against a blanket
    suppression keyed on anything other than `configured`."""
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    async def fake_get(url, headers=None, auth=None, timeout=None):
        return _Resp(401)

    monkeypatch.setattr(reg, "_http_get", fake_get)
    result = await h.probe("jira", _JIRA_CFG)
    assert result.healthy is False


@pytest.mark.asyncio
async def test_enabled_and_configured_probes_as_before(monkeypatch):
    """An integration the user actually enabled and configured must probe
    exactly as it did before this fix — no change to the "it works" path."""
    teams_cfg = {
        "integrations": {"teams": {"enabled": True}},
        "notifications": {
            "teams_webhook_url":
                "https://prod-1.westus.logic.azure.com/workflows/abc123",
        },
        "ci": {},
    }
    teams_result = await h.probe("teams", teams_cfg)
    assert teams_result.healthy is True

    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    async def fake_get(url, headers=None, auth=None, timeout=None):
        return _Resp(200, {"displayName": "Dana"})

    monkeypatch.setattr(reg, "_http_get", fake_get)
    jira_result = await h.probe("jira", _JIRA_CFG)
    assert jira_result.healthy is True
    assert "dana" in jira_result.detail.lower()


# --------------------------------------------------------------------------- #
# run_health_loop()                                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_boot_and_interval_scheduling_via_the_seam(monkeypatch):
    calls = []

    async def fake_sleep(delay):
        calls.append(delay)

    probed = []

    async def fake_probe_all(config):
        probed.append(config)
        return [h.HealthResult("teams", None, "not configured", h._now_iso())]

    monkeypatch.setattr(h, "probe_all", fake_probe_all)

    # Custom interval honoured.
    cfg = {"integrations": {"health_interval": 999}, "notifications": {}, "ci": {}}
    await h.run_health_loop(lambda: cfg, sleep=fake_sleep, iterations=2)
    assert len(probed) == 2  # boot pass + one scheduled pass, before any sleep gates it
    assert calls == [999]

    # Default (21600s / 6h) when absent.
    calls.clear()
    probed.clear()
    default_cfg = {"integrations": {}, "notifications": {}, "ci": {}}
    await h.run_health_loop(lambda: default_cfg, sleep=fake_sleep, iterations=2)
    assert calls == [h.DEFAULT_INTERVAL_SECONDS]


@pytest.mark.asyncio
async def test_failing_integration_reprobes_on_the_short_backoff(monkeypatch):
    calls = []

    async def fake_sleep(delay):
        calls.append(delay)

    async def fake_probe_all(config):
        return [h.HealthResult("jira", False, "HTTP 401 from acme.atlassian.net", h._now_iso())]

    monkeypatch.setattr(h, "probe_all", fake_probe_all)
    cfg = {"integrations": {}, "notifications": {}, "ci": {}}
    await h.run_health_loop(lambda: cfg, sleep=fake_sleep, iterations=2)
    assert calls == [h._FAIL_RETRY_SECONDS]


# --------------------------------------------------------------------------- #
# ensure_fresh_before_poll()                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_ensure_fresh_before_poll_reprobes_only_a_stale_failure_and_never_raises(monkeypatch):
    calls = []

    async def fake_probe(name, config):
        calls.append(name)
        return h.HealthResult(name, True, "authenticated", h._now_iso())

    monkeypatch.setattr(h, "probe", fake_probe)

    # No cache at all -> no reprobe.
    await h.ensure_fresh_before_poll("jira", {})
    assert calls == []

    # Healthy cache -> no reprobe.
    h._RESULTS["jira"] = h.HealthResult("jira", True, "authenticated", h._now_iso())
    await h.ensure_fresh_before_poll("jira", {})
    assert calls == []

    # Fresh failure (well within the backoff window) -> no reprobe.
    h._RESULTS["jira"] = h.HealthResult("jira", False, "HTTP 401", h._now_iso())
    await h.ensure_fresh_before_poll("jira", {})
    assert calls == []

    # Stale failure (older than _FAIL_RETRY_SECONDS) -> reprobes.
    stale = "2000-01-01T00:00:00Z"
    h._RESULTS["jira"] = h.HealthResult("jira", False, "HTTP 401", stale)
    await h.ensure_fresh_before_poll("jira", {})
    assert calls == ["jira"]

    # Never raises, even when the underlying probe blows up.
    async def raising_probe(name, config):
        raise RuntimeError("boom")

    monkeypatch.setattr(h, "probe", raising_probe)
    h._RESULTS["jira"] = h.HealthResult("jira", False, "HTTP 401", stale)
    await h.ensure_fresh_before_poll("jira", {})  # must not raise


# --------------------------------------------------------------------------- #
# start_health_probes() / stop_health_probes()                                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_probe_failure_never_blocks_start(monkeypatch):
    def boom(coro):
        coro.close()  # avoid a "coroutine was never awaited" warning
        raise RuntimeError("boom")

    monkeypatch.setattr(h.asyncio, "create_task", boom)

    class _State:
        config = type("C", (), {"data": {}})()

    class _App:
        state = _State()

    task = h.start_health_probes(_App())
    assert task is None

    # stop_health_probes is a no-op when no task was ever set.
    class _State2:
        pass

    class _App2:
        state = _State2()

    await h.stop_health_probes(_App2())  # must not raise


@pytest.mark.asyncio
async def test_with_health_probes_starts_after_boot_and_stops_before_return(monkeypatch):
    calls = []

    def fake_start(app):
        calls.append(("start", app.state.config))
        return "task-sentinel"

    async def fake_stop(app):
        calls.append(("stop", None))

    monkeypatch.setattr(h, "start_health_probes", fake_start)
    monkeypatch.setattr(h, "stop_health_probes", fake_stop)

    @asynccontextmanager
    async def inner_lifespan(app):
        app.state.config = "configured"  # start_health_probes must see this
        calls.append(("inner-boot", None))
        yield "sentinel-value"
        calls.append(("inner-shutdown", None))

    class _State:
        pass

    class _App:
        state = _State()

    app = _App()
    wrapped = h.with_health_probes(inner_lifespan)
    async with wrapped(app) as value:
        assert value == "sentinel-value"  # the original lifespan's yielded value passes through
        assert app.state.health_probes == "task-sentinel"
        assert calls == [("inner-boot", None), ("start", "configured")]

    # stop_health_probes runs on the way out, before the wrapped lifespan's
    # own shutdown code (an independent background task, safe to tear down
    # in either order relative to the worker/store shutdown it wraps).
    assert calls[-2:] == [("stop", None), ("inner-shutdown", None)]


# --------------------------------------------------------------------------- #
# GET /api/integrations                                                       #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.api.app import app
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


@pytest.mark.asyncio
async def test_status_endpoint_exposes_health_fields(client):
    h._RESULTS["jira"] = h.HealthResult(
        "jira", False, "HTTP 404 from acme.atlassian.net", "2026-01-01T00:00:00Z")

    r = await client.get("/api/integrations")
    assert r.status_code == 200
    items = {i["name"]: i for i in r.json()["integrations"]}
    for i in items.values():
        assert "healthy" in i
        assert "detail" in i
        assert "checked_at" in i
    jira = items["jira"]
    assert jira["healthy"] is False
    assert jira["detail"] == "HTTP 404 from acme.atlassian.net"
    assert jira["checked_at"] == "2026-01-01T00:00:00Z"
