"""The CAPTURE half of "onboarding email must reach our servers": a
successful POST /api/onboarding/email now forwards the address to a hosted
registration endpoint, on top of (never instead of) the existing local
persistence covered by tests/test_onboarding_email.py.

Fixtures (`client`, `store`) are lifted verbatim from tests/test_onboarding_
email.py rather than reinvented — same ASGITransport/tmp-config-path pattern.
"""
from __future__ import annotations

import json
import logging
import types
import urllib.error

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human.api.app import app
from no_human.email import register


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    app.state.store = store
    app.state.config = types.SimpleNamespace(data={})
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    if hasattr(app.state, "register_transport"):
        delattr(app.state, "register_transport")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c
    if hasattr(app.state, "register_transport"):
        delattr(app.state, "register_transport")


class _RecordingTransport:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post(self, url: str, payload: dict[str, str]) -> None:
        self.calls.append((url, dict(payload)))


class _RaisingTransport:
    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    def post(self, url: str, payload: dict[str, str]) -> None:
        self.calls += 1
        raise self._exc


# ── AC1: the outbound call carries the right address + platform ───────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sys_platform,expected_plan",
    [("darwin", "desktop-darwin"), ("win32", "desktop-windows")],
)
async def test_registering_forwards_address_and_platform_to_the_hosted_endpoint(
    client, monkeypatch, sys_platform, expected_plan
):
    monkeypatch.setenv("NH_ONBOARDING_REGISTER_URL", "https://register.invalid/intake")
    monkeypatch.setattr(register.sys, "platform", sys_platform)
    fake = _RecordingTransport()
    app.state.register_transport = fake

    r = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["registration"] == "ok"

    assert len(fake.calls) == 1
    url, payload = fake.calls[0]
    assert url == "https://register.invalid/intake"
    assert payload["email"] == "person@example.com"
    assert payload["plan"] == expected_plan
    assert payload["source"] == "onboarding"


@pytest.mark.asyncio
async def test_no_endpoint_configured_makes_zero_outbound_calls(client, tmp_path):
    fake = _RecordingTransport()
    app.state.register_transport = fake

    r = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["registration"] == "not_configured"
    assert fake.calls == []

    import yaml
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["onboarding"]["email"] == "person@example.com"


# ── HTTPS enforcement: a misconfigured plaintext endpoint must not ship the
#    address in cleartext, mirroring brain/client.py's `_base()` guard ──────


@pytest.mark.asyncio
async def test_non_https_endpoint_makes_zero_outbound_calls(client, monkeypatch, tmp_path):
    monkeypatch.setenv("NH_ONBOARDING_REGISTER_URL", "http://register.invalid/intake")
    fake = _RecordingTransport()
    app.state.register_transport = fake

    r = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["registration"] == "not_configured"
    assert fake.calls == [], "a plaintext http:// endpoint must never be posted to"

    import yaml
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["onboarding"]["email"] == "person@example.com"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://register.invalid/intake", True),
        ("http://register.invalid/intake", False),
        ("http://localhost:8420/intake", True),
        ("http://127.0.0.1:8420/intake", True),
        ("http://[::1]:8420/intake", True),
        ("http://localhost.evil.com/intake", False),
        ("http://127.0.0.1@evil.com/intake", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_is_https_or_loopback(url, expected):
    assert register._is_https_or_loopback(url) is expected


def test_register_email_refuses_plaintext_endpoint_via_direct_call(caplog):
    fake = _RecordingTransport()
    with caplog.at_level(logging.WARNING):
        status = register.register_email(
            "person@example.com", transport=fake, endpoint="http://register.invalid/intake"
        )
    assert status == "not_configured"
    assert fake.calls == []
    assert "person@example.com" not in caplog.text


# ── AC2: fail-open, off the critical path ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        urllib.error.URLError("no route to host"),
        urllib.error.HTTPError("https://register.invalid/intake", 500, "Server Error", {}, None),
        RuntimeError("boom"),
    ],
    ids=["TimeoutError", "URLError", "HTTPError500", "RuntimeError"],
)
async def test_forward_failure_still_returns_200_and_persists(client, monkeypatch, tmp_path, exc):
    monkeypatch.setenv("NH_ONBOARDING_REGISTER_URL", "https://register.invalid/intake")
    app.state.register_transport = _RaisingTransport(exc)

    r = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["registration"] == "stored_locally_only"

    import yaml
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["onboarding"]["email"] == "person@example.com"


# ── AC3: the address never appears in logs or the response body ───────────


@pytest.mark.asyncio
async def test_address_never_appears_in_logs_or_response_body(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("NH_ONBOARDING_REGISTER_URL", "https://register.invalid/intake")
    secret_addr = "dana.lee+register-secret@example.com"
    leaking_exc = urllib.error.HTTPError(
        "https://register.invalid/intake", 400, f"bad payload: {secret_addr}", {}, None
    )
    app.state.register_transport = _RaisingTransport(leaking_exc)

    r = await client.post("/api/onboarding/email", json={"email": secret_addr})
    assert r.status_code == 200, r.text

    assert secret_addr not in caplog.text
    assert "dana.lee" not in caplog.text
    assert secret_addr not in r.text
    assert r.json()["registration"] in register.STATUSES


# ── AC4: re-posting the same address forwards zero additional times ───────


@pytest.mark.asyncio
async def test_reposting_the_same_address_forwards_zero_additional_times(client, monkeypatch):
    monkeypatch.setenv("NH_ONBOARDING_REGISTER_URL", "https://register.invalid/intake")
    fake = _RecordingTransport()
    app.state.register_transport = fake

    r1 = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert r1.status_code == 200
    assert r1.json()["registration"] == "ok"
    assert len(fake.calls) == 1

    r2 = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert r2.status_code == 200
    assert r2.json()["registration"] == "skipped_unchanged"
    assert len(fake.calls) == 1, "an unchanged repost must not forward a second time"

    r3 = await client.post("/api/onboarding/email", json={"email": "other@example.com"})
    assert r3.status_code == 200
    assert r3.json()["registration"] == "ok"
    assert len(fake.calls) == 2, "a genuinely new address must still forward"


# ── Unit: platform normalization ───────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("darwin", "darwin"), ("linux", "linux"), ("win32", "windows"), ("freebsd13", "unknown")],
)
def test_platform_normalization(raw, expected):
    assert register._platform(raw) == expected
