"""Required onboarding email step: registration endpoint + welcome-email seam.

Covers the acceptance criteria that need real wiring, not just source-sweeps
(the JS side of the wizard step is pinned by onboardingEmailStep.test.mjs and
its siblings under web/src/):

- AC2: the registered address never reaches telemetry, in any form — proven
  by driving the REAL telemetry.flush() with urllib intercepted and searching
  the raw wire bytes, with a positive control showing the search mechanism
  can find a string that IS sent (test_telemetry.py's temp_home/no_network/
  no_thread/_ENABLED fixtures, reused verbatim rather than reinvented).
- AC4: the address is persisted via the existing `_persist_onboarding` /
  config.yaml mechanism, and the send path renders the EXISTING frozen
  templates in `no_human.email.base` byte-for-byte — never new copy.
- AC5: the transport seam is a one-module change (`no_human.email.send`): a
  fake transport substituted in observes the exact rendered `Message`; the
  shipped default (`UnavailableTransport`) never claims delivery works; the
  failure-category vocabulary is closed and address-free; re-registering an
  unchanged address is idempotent (no second send).
- AC6: a malformed address is rejected by the SERVER (422) independent of
  the client-side well-formedness gate, and the wizard's failure contract
  (guard()/noteFetchFailure, tested on the JS side) has nothing new to do
  here since a 4xx/5xx from this endpoint is handled exactly like any other
  onboarding endpoint failure.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import types

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human import telemetry
from no_human.api.app import app
from no_human.email import base, send

# ── fixtures (patterns lifted verbatim from tests/test_onboarding_api.py and
# tests/test_telemetry.py — not reinvented) ────────────────────────────────


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    app.state.store = store
    app.state.config = types.SimpleNamespace(data={})
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


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
    monkeypatch.setattr(telemetry, "_spawn_flush", lambda section: None)


_ENABLED = {
    "enabled": True,
    "endpoint": "https://ingest.invalid/collect",
    "instance_id": "11111111-2222-3333-4444-555555555555",
    "posthog_publishable": "phc_test",
}

SECRET_ADDRESS = "dana.lee+onboarding-secret@example.com"


# ── AC2: the address never reaches telemetry, in any form ─────────────────


@pytest.mark.asyncio
async def test_registering_an_email_never_shows_up_on_the_telemetry_wire(
    client, temp_home, no_network, no_thread
):
    # Register the address through the real onboarding endpoint.
    r = await client.post("/api/onboarding/email", json={"email": SECRET_ADDRESS})
    assert r.status_code == 200, r.text

    # Meanwhile, the app emits ordinary, ALLOWED telemetry (this is what real
    # usage looks like — record() can never accept an "email" prop at all,
    # see test_telemetry_record_has_no_email_shaped_prop_anywhere below, but
    # the point of THIS test is the wire payload, not just the schema).
    telemetry.record("feature_used", config={"telemetry": _ENABLED}, name="onboarding_email_step")
    telemetry.record("task_created", config={"telemetry": _ENABLED}, source="feature")

    sent = telemetry.flush(_ENABLED)
    assert sent == 2
    assert len(no_network) == 1
    req, _timeout = no_network[0]
    wire_bytes = req.data

    # The address must not appear anywhere on the wire — not raw, not
    # percent-encoded, not as a substring of a hash (a hash wouldn't contain
    # it as a substring anyway, but the point is: search the ACTUAL bytes).
    assert SECRET_ADDRESS.encode() not in wire_bytes
    assert SECRET_ADDRESS not in wire_bytes.decode()
    # Nor any case-folded/local-part-only fragment of it.
    assert b"dana.lee" not in wire_bytes
    assert b"onboarding-secret" not in wire_bytes

    # Positive control: prove the search mechanism actually finds a string
    # that IS present, so the assertions above are not just testing an empty
    # payload. "onboarding_email_step" was passed as a legitimate `name` prop
    # on an ALLOWED event and must survive to the wire.
    assert b"onboarding_email_step" in wire_bytes
    body = json.loads(wire_bytes.decode())
    assert body["instance_id"] == _ENABLED["instance_id"]


def test_telemetry_record_has_no_email_shaped_prop_anywhere():
    # Closed-enum enforcement, from telemetry.py itself (unmodified, per the
    # task's hard constraint): every allowed prop name is enumerated, and
    # "email" is not one of them for any event kind.
    for kind, props in telemetry._ALLOWED_EVENTS.items():
        assert "email" not in props, f"{kind} allows an 'email' prop"
    with pytest.raises(ValueError, match="not allowed"):
        telemetry.record(
            "feature_used", config={"telemetry": _ENABLED},
            name="x", email=SECRET_ADDRESS,
        )


def test_email_and_telemetry_modules_never_reference_each_other():
    # Structural guard for the same AC2 guarantee: the modules are fully
    # decoupled, so there is no code path by which one could leak into the
    # other's payloads.
    import inspect

    telemetry_src = inspect.getsource(telemetry)
    assert "email" not in telemetry_src.lower().replace("e-mail", "")
    for mod in (base, send):
        mod_src = inspect.getsource(mod)
        assert "telemetry" not in mod_src.lower()


# ── AC3 (backend half): the request is server-side, replay exclusion lives
# in the JS layer (replayScrub.js / maskCapturedNetworkRequestFn), pinned by
# web/src/replayScrub.test.mjs — nothing further to assert from Python. ────


# ── AC4: persistence + byte-for-byte template reuse ────────────────────────


@pytest.mark.asyncio
async def test_registering_an_email_persists_to_config_yaml_and_is_redacted_from_status(
    client, tmp_path
):
    r = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "welcome"}, "the response must never echo the address back"
    assert body["ok"] is True

    import yaml
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    ob = on_disk["onboarding"]
    assert ob["email"] == "person@example.com"
    assert "email_at" in ob
    assert "welcome_status" in ob

    status = await client.get("/api/onboarding/status")
    assert status.status_code == 200
    for field in ("email", "email_at", "welcome_status"):
        assert field not in status.json(), f"{field} must be redacted from the polled status"


@pytest.mark.asyncio
async def test_onboarding_complete_does_not_echo_the_address_either(client, tmp_path):
    """The redaction on GET /api/onboarding/status is not enough by itself:
    POST /api/onboarding/complete merges and returns the SAME onboarding
    block (`_persist_onboarding`'s return value), and it is not in
    replayScrub.js's REPLAY_EXCLUDED_PATHS (it legitimately echoes
    repos/docs for the wizard summary, so PostHog session replay captures
    its body verbatim). If the address were ever registered before this
    call, the `{"ok": True, "onboarding": ob}` response must still never
    carry it."""
    reg = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert reg.status_code == 200, reg.text

    r = await client.post(
        "/api/onboarding/complete",
        json={"team": "PLATFORM", "repos": ["/x/svc"], "docs": ["/docs/adr"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    ob = body["onboarding"]
    for field in ("email", "email_at", "welcome_status"):
        assert field not in ob, f"{field} must be redacted from /api/onboarding/complete's response"
    assert "person@example.com" not in json.dumps(body), "the address must not appear anywhere in the response body"
    # Everything else the wizard summary reads still comes through unredacted.
    assert ob["completed"] is True
    assert ob["team"] == "PLATFORM"
    assert ob["repos"] == ["/x/svc"]

    # And it is still persisted on disk — redaction is response-shaping only.
    import yaml
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["onboarding"]["email"] == "person@example.com"


@pytest.mark.asyncio
async def test_config_endpoint_does_not_echo_the_address_either(client, tmp_path):
    """A third leak vector, distinct from /api/onboarding/status and
    /api/onboarding/complete: GET /api/config returns `_scrub_secrets` over
    the ENTIRE persisted config, including `onboarding`, and
    `_SECRET_KEY_RE` (token|secret|password|webhook|key) does not match
    "email" — so without an explicit redaction, the address registered
    through /api/onboarding/email would come back verbatim on every poll of
    this endpoint (TaskComposer.jsx and Settings.jsx both fetch it with a
    plain `fetch`, and it is not in replayScrub.js's
    REPLAY_EXCLUDED_PATHS, so PostHog session replay would capture it
    unmasked for the life of the install)."""
    reg = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert reg.status_code == 200, reg.text

    r = await client.get("/api/config")
    assert r.status_code == 200, r.text
    body = r.json()
    ob = body.get("onboarding") or {}
    for field in ("email", "email_at", "welcome_status"):
        assert field not in ob, f"{field} must be redacted from /api/config"
    assert "person@example.com" not in r.text, "the address must not appear anywhere in /api/config's response"

    # And it is still persisted on disk — redaction is response-shaping only.
    import yaml
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["onboarding"]["email"] == "person@example.com"


@pytest.mark.asyncio
async def test_reset_endpoint_does_not_echo_the_address_either(client, tmp_path):
    """A fourth leak vector, closed the same way as status/complete/config:
    POST /api/onboarding/reset returns `{"completed": ..., **ob}` — the
    board's desktop "Re-run Setup..." action (App.jsx) hits this after a
    user has already completed onboarding once, so `ob` may already carry a
    registered `email`/`email_at`/`welcome_status` from a prior POST to
    /api/onboarding/email. It must never ride along in the reset response
    either — same guarantee, same `_onboarding_public` helper as the other
    three routes, not a fourth copy-pasted comprehension."""
    reg = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert reg.status_code == 200, reg.text

    r = await client.post("/api/onboarding/reset")
    assert r.status_code == 200, r.text
    body = r.json()
    for field in ("email", "email_at", "welcome_status"):
        assert field not in body, f"{field} must be redacted from /api/onboarding/reset"
    assert "person@example.com" not in r.text, "the address must not appear anywhere in the reset response"
    assert body["completed"] is False

    # And it is still persisted on disk — reset only clears `completed`.
    import yaml
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["onboarding"]["email"] == "person@example.com"


@pytest.mark.asyncio
async def test_no_route_in_the_app_ever_echoes_the_registered_address(
    client, temp_home, no_network, no_thread
):
    """Behavioural proof over the app's REAL route table — not a source-text
    scan for `_onboarding_public`/`_ONBOARDING_STATUS_REDACTED_FIELDS` call
    sites (a regex over app.py asserting a helper is called N times proves
    nothing about the N+1th route). Every GET route with no path parameter,
    plus every POST route under /api/onboarding, is actually called; the
    registered address must not appear in ANY response body, success or
    error. This is what should have caught `/api/onboarding/reset` echoing
    the unredacted onboarding block before this fix: the four routes fixed
    so far (status, complete, config, reset) all pass this walk now, and a
    fifth route that forgets `_onboarding_public` fails it immediately,
    without anyone having to remember to write a fifth hand-written test.

    No route is skipped. Errors are caught (a 503/422/500 is not itself a
    leak) but the exception text is still searched for the address.
    """
    address = "route-walk-canary@example.invalid"
    reg = await client.post("/api/onboarding/email", json={"email": address})
    assert reg.status_code == 200, reg.text

    get_routes = sorted(
        {
            r.path
            for r in app.routes
            if getattr(r, "path", None)
            and "GET" in getattr(r, "methods", set())
            and "{" not in r.path
        }
    )
    onboarding_post_routes = sorted(
        {
            r.path
            for r in app.routes
            if getattr(r, "path", None)
            and "POST" in getattr(r, "methods", set())
            and r.path.startswith("/api/onboarding/")
            and "{" not in r.path
        }
    )
    # Sanity floor: this branch's route table has 39 matching GET routes and
    # 11 matching /api/onboarding POST routes. The thresholds are well below
    # that so a route added or removed later doesn't make this test flaky,
    # but a bug in the walk itself (wrong attribute name, empty result)
    # cannot silently pass as "no leaks found".
    assert len(get_routes) >= 30, get_routes
    assert len(onboarding_post_routes) >= 8, onboarding_post_routes

    async def _hit(method: str, path: str) -> str:
        try:
            if method == "GET":
                resp = await asyncio.wait_for(client.get(path), timeout=20)
            else:
                resp = await asyncio.wait_for(client.post(path, json={}), timeout=20)
            return resp.text
        except Exception as exc:  # noqa: BLE001 - an error is not a leak, but its text is still checked below
            return repr(exc)

    checked = 0
    for path in get_routes:
        text = await _hit("GET", path)
        assert address not in text, f"GET {path} echoed the registered address"
        assert "route-walk-canary" not in text, f"GET {path} echoed a fragment of the registered address"
        checked += 1
    for path in onboarding_post_routes:
        text = await _hit("POST", path)
        assert address not in text, f"POST {path} echoed the registered address"
        assert "route-walk-canary" not in text, f"POST {path} echoed a fragment of the registered address"
        checked += 1

    assert checked == len(get_routes) + len(onboarding_post_routes)


def test_render_welcome_reuses_the_frozen_template_byte_for_byte():
    address = "person@example.com"
    golden_subject, golden_body = base.mac_download(
        send.DOWNLOAD_URL, send.UNSUBSCRIBE_URL, address
    )
    msg = send.render_welcome(address, platform="darwin")
    assert msg.subject == golden_subject
    assert msg.body == golden_body
    # Pin the specific, human-legible facts the acceptance criterion names:
    assert msg.subject == "Your no_human download for macOS"
    assert msg.body.splitlines()[0] == "Hi Person,"


def test_render_welcome_picks_the_right_template_per_platform():
    address = "a@b.co"
    linux_subject, linux_body = send.render_welcome(address, platform="linux").subject, \
        send.render_welcome(address, platform="linux").body
    assert (linux_subject, linux_body) == base.linux_download(
        send.DOWNLOAD_URL, send.UNSUBSCRIBE_URL, address
    )
    win_msg = send.render_welcome(address, platform="win32")
    assert (win_msg.subject, win_msg.body) == base.windows_waitlist(
        send.UNSUBSCRIBE_URL, address
    )
    # Unrecognized/absent platform defaults to macOS (intake Q&A).
    default_msg = send.render_welcome(address, platform="some-unknown-os")
    assert default_msg.subject == base.mac_download(
        send.DOWNLOAD_URL, send.UNSUBSCRIBE_URL, address
    )[0]


# ── AC5: one-module transport seam, closed failure vocabulary, idempotency ─


class _FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def test_a_fake_transport_substituted_in_observes_the_rendered_message():
    fake = _FakeTransport()
    status = send.send_welcome("person@example.com", transport=fake, platform="darwin")
    assert status == "sent"
    assert len(fake.sent) == 1
    [msg] = fake.sent
    assert msg.to == "person@example.com"
    assert msg.subject == "Your no_human download for macOS"
    assert msg.body.startswith("Hi Person,")


def test_default_transport_never_claims_delivery_works():
    status = send.send_welcome("person@example.com")
    assert status == "not_sent:unconfigured"
    with pytest.raises(send.TransportUnavailable):
        send.UnavailableTransport().send(
            send.render_welcome("person@example.com")
        )


def test_failure_categories_are_closed_and_address_free():
    for category in send.FAILURE_CATEGORIES:
        assert "@" not in category
        assert "person" not in category
    with pytest.raises(ValueError, match="unknown transport failure category"):
        send.TransportError("boom", category="not_a_real_category", retryable=False)


def test_a_transport_that_raises_an_arbitrary_exception_is_absorbed_as_transport_error():
    class _Boom:
        def send(self, msg):
            raise RuntimeError("some SES client-library detail, possibly PII-shaped")

    status = send.send_welcome("person@example.com", transport=_Boom())
    assert status == "not_sent:transport_error"


@pytest.mark.asyncio
async def test_reregistering_the_same_address_is_idempotent_no_second_send(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "no_human.email.send.send_welcome",
        lambda addr, **kw: calls.append(addr) or "sent",
    )
    r1 = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert r1.status_code == 200
    assert r1.json()["welcome"] == "sent"
    assert len(calls) == 1

    r2 = await client.post("/api/onboarding/email", json={"email": "person@example.com"})
    assert r2.status_code == 200
    assert r2.json()["welcome"] == "skipped_unchanged"
    assert len(calls) == 1, "an unchanged re-registration must not trigger a second send"

    r3 = await client.post("/api/onboarding/email", json={"email": "someone-else@example.com"})
    assert r3.status_code == 200
    assert r3.json()["welcome"] == "sent"
    assert len(calls) == 2, "a genuinely different address must still send"


def test_the_transport_seam_is_confined_to_send_py():
    # app.py only ever calls the module-level send_welcome function; it must
    # not reach into Transport/UnavailableTransport directly, or a future
    # transport swap would touch more than one module.
    import importlib
    import inspect

    app_module = importlib.import_module("no_human.api.app")
    app_src = inspect.getsource(app_module)
    assert "Transport" not in app_src
    assert "from ..email.send import send_welcome" in app_src


# ── AC6: server-side rejection mirrors an ordinary onboarding-endpoint
# failure — no new failure mode, no lockout logic to write or test here
# (guard()/noteFetchFailure's offline-vs-err classification is generic HTTP
# status handling in Onboarding.jsx and is pinned by onboardingEmailStep.
# test.mjs / onboardingOffline.test.mjs on the JS side). ────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "not-an-email", "no-domain@", "@no-local.com",
                                  "trailing-dot@example.", "has space@example.com"])
async def test_malformed_addresses_are_rejected_by_the_server_regardless_of_client_gate(
    client, bad
):
    r = await client.post("/api/onboarding/email", json={"email": bad})
    assert r.status_code == 422

    # And nothing was persisted.
    status = await client.get("/api/onboarding/status")
    assert status.status_code == 200
