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
  config.yaml mechanism. The send path renders `no_human.email.in_app`, NOT
  base.py's four templates: those serve the website flows (a download link,
  "drag no_human to Applications") and were false for a reader inside the
  running app. base.py itself is still frozen and untouched, and in_app.py
  imports its greeting, intro and footer so the voice is the same person.
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

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human import telemetry
from no_human.api.app import app
from no_human.email import base, in_app, send

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


# `test_email_and_telemetry_modules_never_reference_each_other` used to sit
# here: `inspect.getsource(telemetry)` plus `assert "email" not in src`. It was
# a false-positive generator -- one explanatory comment mentioning the word
# turned it red with zero behaviour change -- and it could not detect a real
# leak, because an address passed under any other name satisfies it. The AC2
# guarantee is carried behaviourally by
# `test_registering_an_email_never_shows_up_on_the_telemetry_wire`, whose
# search instrument is validated against a planted leak.


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


def test_render_welcome_reuses_the_in_app_template_byte_for_byte():
    """`render_welcome` passes a template's return value through untouched.

    Asserted against the whole subject and the whole body, not just the first
    line: a previous version compared only the subject and `body.splitlines()[0]`,
    which left every other line -- including the unsubscribe footer that
    `base.py`'s docstring calls non-negotiable -- free to drift.
    """
    address = "a@b.co"
    msg = send.render_welcome(address)
    assert (msg.subject, msg.body, msg.html) == in_app.in_app_welcome(
        send.UNSUBSCRIBE_URL, address
    )
    assert "Unsubscribe: " in msg.body


def test_the_platform_no_longer_selects_the_body():
    """It used to, and that was the defect.

    `base.py`'s four templates are the WEBSITE flows -- a visitor requesting a
    download, or joining a waitlist. This step runs inside the already-installed
    desktop app, so every platform branch produced copy that was false on
    arrival: a download link for software the reader is looking at, and
    "you requested this download at getnohuman.com" for something that never
    happened. One body now, and `platform` is inert.
    """
    address = "a@b.co"
    bodies = {
        send.render_welcome(address, platform=p).body
        for p in ("linux", "win32", "darwin", "some-unknown-os", None)
    }
    assert len(bodies) == 1, "platform still changes the body"
    body = bodies.pop()
    assert "getnohuman.com/download" not in body, "no download link: they have it"
    assert "DMG" not in body and "Applications" not in body, "no install advice"
    assert "requested this download" not in body, "a reason that never happened"
    # Positive control: the shared founder voice IS present, so the assertions
    # above are not passing over an empty string.
    assert "I'm Eyal, the founder of no_human." in body
    assert "Unsubscribe:" in body


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
    # The literal, not `in_app.SUBJECT`: the operator pinned this line
    # (2026-09-12), so a rename must fail here rather than follow along.
    assert msg.subject == "Welcome to no_human"
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


# `test_the_transport_seam_is_confined_to_send_py` used to sit here, asserting
# the word "Transport" never appears in app.py's source. Same class: any
# unrelated use of the word turns it red, and it establishes nothing about
# where the seam actually lives.


# ── AC6: server-side rejection mirrors an ordinary onboarding-endpoint
# failure — no new failure mode, no lockout logic to write or test here
# (guard()/noteFetchFailure's offline-vs-err classification is generic HTTP
# status handling in Onboarding.jsx and is pinned by onboardingEmailStep.
# test.mjs / onboardingOffline.test.mjs on the JS side). ────────────────────


def _shared_email_cases():
    """The ONE table both validators are checked against.

    Deliberately not inline: `web/src/onboardingEmail.test.mjs` reads the same
    JSON. Two implementations of one rule drift, and a table living in one
    stack only ever gets extended in one stack.
    """
    path = Path(__file__).resolve().parent.parent / "testdata" / "email_validation_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


_EMAIL_CASES = _shared_email_cases()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _EMAIL_CASES, ids=[c["why"][:38] for c in _EMAIL_CASES])
async def test_the_server_agrees_with_the_shared_table_in_both_directions(client, case):
    """Acceptance AND rejection, because a bound pinned only against being too
    LOOSE lets an over-strict one ship.

    Measured on this branch: tightening the client's `EMAIL_MAX_LEN` to 25 left
    all 1669 web tests green while refusing
    `dana.lee+onboarding@example.com` -- an ordinary address, on a step the
    operator made REQUIRED, which would lock a real user out of their own
    board. The accept rows exist for that failure, not for completeness.
    """
    r = await client.post("/api/onboarding/email", json={"email": case["address"]})
    if case["valid"]:
        assert r.status_code == 200, (
            f"{case['why']}: a VALID address was refused -- over-strict "
            f"validation locks a user out of a required step. {r.text[:200]}"
        )
    else:
        assert r.status_code == 422, f"{case['why']}: expected refusal, got {r.status_code}"
        # A refusal must also leave nothing behind: the address must not reach
        # config.yaml, which is what made the 200,012-character row a denial of
        # service rather than a cosmetic complaint.
        status = await client.get("/api/onboarding/status")
        assert status.status_code == 200
        if case["address"]:          # "" is a substring of everything
            assert case["address"] not in status.text


def test_the_shared_table_covers_both_directions():
    """A table of only-rejects would make the test above vacuous in the one
    direction that actually bit."""
    ok = [c for c in _EMAIL_CASES if c["valid"]]
    bad = [c for c in _EMAIL_CASES if not c["valid"]]
    assert len(ok) >= 5 and len(bad) >= 5, (len(ok), len(bad))
    longest_ok = max(ok, key=lambda c: len(c["address"]))
    assert len(longest_ok["address"]) == 254, (
        "the table must assert a 254-character address is ACCEPTED -- that is "
        "the only row an over-tightened whole-path cap cannot satisfy"
    )


# ── The HTML part (operator direction 2026-09-12: a designed welcome) ───────
#
# Every test here observes the RENDERED artifact. None of them re-derives an
# expectation by calling the code under test, and none matches the module's
# source text -- a regex over source proves only that a character sequence is
# present, which is how ~10 review rounds were lost in this repo.

import re as _re

_ADDR = "dana.lee@example.com"


_BOARD = "http://127.0.0.1:8420"
_CTA = _BOARD + "/open"


def _rendered():
    return in_app.in_app_welcome(send.UNSUBSCRIBE_URL, _ADDR, _BOARD)


def _strip_tags(html: str) -> str:
    """Visible text of the rendered document, entities resolved.

    The second split is load-bearing: `split("<body", 1)[1]` leaves the body
    TAG's own attributes in the string, so copy moved into `<body data-x=...>`
    -- where no reader ever sees it -- counted as delivered. Measured: the
    why-you-got-this line could be deleted from the footer that way with the
    suite green. Skip past the tag's closing ">" so "visible" means visible.
    """
    import html as _h
    head, _, rest = html.partition("<body")
    # Nothing a reader sees may sit BEFORE the body tag. HTML5 reparses
    # non-whitespace character data in <head> into the body -- verified with a
    # real WebKit parse, not argued -- so a scan that starts at <body> is blind
    # to it. Everything before must be markup, a doctype, or whitespace.
    visible_before = _re.sub(r"<[^>]*>", " ", head).replace("\u00a0", " ")
    visible_before = " ".join(visible_before.split())
    # <title> is the one legitimate reader-visible string up here (the tab and
    # the mail client's subject line), and it must be the DECLARED subject --
    # allowing the element wholesale would let arbitrary copy hide in it.
    assert visible_before == in_app.SUBJECT, (
        f"copy before <body> is reader-visible but unscanned: "
        f"{visible_before!r} (expected only the subject {in_app.SUBJECT!r})")
    # Reader-visible ATTRIBUTE text is the other thing tag-stripping cannot
    # see: `<body title="...">` renders as a tooltip and never reaches the
    # visible string. The document has no images and no interactive controls,
    # so none of these attributes has a legitimate use here -- measured, the
    # rendered doc carries only align/bgcolor/border/cellpadding/cellspacing/
    # charset/content/href/lang/name/role/style/width.
    for attr in ("title=", "alt=", "aria-label="):
        assert attr not in html, (
            f"{attr} carries reader-visible text that the tag-strip cannot see")
    body = rest.split(">", 1)[1]
    return _h.unescape(_re.sub(r"<[^>]+>", " ", body))


def test_every_shared_copy_constant_reaches_both_parts():
    """Each constant in SHARED_COPY appears in BOTH parts, and the HTML-only
    chrome appears in the HTML and NOT in the text.

    Checked against the VISIBLE text of the HTML, so a string that survives
    only inside an attribute does not count as delivered.
    """
    _, text, html = _rendered()
    visible = " ".join(_strip_tags(html).split())
    flat_text = " ".join(text.split())
    for name in in_app.SHARED_COPY:
        value = " ".join(getattr(in_app, name).split())
        assert value in visible, f"{name} missing from the HTML part"
        assert value in flat_text, f"{name} missing from the text part"
    for name in in_app.HTML_ONLY_COPY:
        value = " ".join(getattr(in_app, name).split())
        assert value in visible, f"{name} missing from the HTML part"
        assert value not in flat_text, (
            f"{name} is HTML-only chrome but reached the text part")
    # The founder voice from base.py reaches both.
    assert in_app._INTRO in visible and in_app._INTRO in flat_text


def test_no_copy_reaches_one_part_alone():
    """Every sentence a reader sees is a declared one, exactly once per part.

    Two ways this was weaker than its name, both measured:

    * it compared each rendered sentence against one CONCATENATED blob with
      `in`, so any FRAGMENT of a declared constant passed -- a line reading
      only "nothing merges without you." could be added to the HTML alone and
      the suite stayed green. Membership is now against a SET of declared
      sentences, split with the same regex, so a fragment is not a member.
    * it dropped the footer forms before counting, so duplicating the
      why-you-got-this line into the text part was invisible; a reader saw it
      twice. The footer forms are now DECLARED and counted like everything
      else, with no drop list at all.
    """
    _, text, html = _rendered()

    def sentences(blob):
        out = []
        for raw in _re.split(r"(?<=[.?])\s+|\n{2,}", blob):
            one = " ".join(raw.split())
            # base.py's frozen `_footer` puts its "--" signature separator on
            # its own line, so the why-you-got-this line renders as
            # "-- You're receiving..." while a duplicate of the constant
            # renders bare. Two different strings, one sentence to a reader:
            # without collapsing the marker the counter saw each once and the
            # duplication was invisible. Measured — that attack survived.
            if one.startswith("--"):
                one = one[2:].strip()
            if one:
                out.append(one)
        return out

    # Everything a reader is allowed to see, as SENTENCES: the declared
    # constants, the founder voice from base.py, the forms those get composed
    # into at render time (the HTML joins greeting+intro; the text part spells
    # the CTA as "label: url"), and base.py's frozen `_footer`, which composes
    # its two mechanical lines from its own literals rather than this module's.
    declared: set[str] = set()
    for piece in (
        *(getattr(in_app, n) for n in in_app.SHARED_COPY + in_app.HTML_ONLY_COPY),
        in_app._INTRO,
        in_app._greet(_ADDR),
        f"{in_app._greet(_ADDR)} {in_app._INTRO}",
        f"{in_app.CTA_LABEL}: {_CTA}",
        f"-- {in_app.WHY}",
        # The text part spells the unsubscribe out; the HTML part labels an
        # anchor "Unsubscribe" and puts the mailto in its href.
        f"no_human \u00b7 {in_app.SITE} Unsubscribe: {send.UNSUBSCRIBE_URL}",
        f"no_human \u00b7 {in_app.SITE} {in_app.UNSUBSCRIBE_LABEL}",
        # The wordmark stands alone at the top of the card.
        in_app.WORDMARK,
    ):
        declared.update(sentences(piece))

    from collections import Counter

    for label, blob in (("HTML", _strip_tags(html)), ("text", text)):
        seen = Counter(sentences(blob))
        undeclared = sorted(k for k in seen if k not in declared)
        assert undeclared == [], f"{label} part shows undeclared copy: {undeclared}"
        repeated = {k: n for k, n in seen.items() if n > 1}
        assert repeated == {}, f"{label} part repeats copy: {repeated}"

    # Positive control: the checker can fail. A sentence that is genuinely not
    # declared must be reported, or every assertion above is vacuous.
    assert "Upgrade to Pro for unlimited tasks." not in declared


def test_the_greeting_is_escaped_in_the_html_part():
    """`greeting_name` does not sanitise -- measured:
    greeting_name("dana<script>...@x.io") -> 'Dana<script>...'.
    Harmless while the body was text; an injection the moment it is markup.
    """
    hostile = 'dana<script>alert(1)</script>"onx@example.com'
    # Positive control: the hostile fragment really does survive the greeting,
    # so this test is exercising a live path and not an inert one.
    assert "<script>" in in_app._greet(hostile)
    _, _, html = in_app.in_app_welcome(send.UNSUBSCRIBE_URL, hostile)
    assert "<script>" not in html, "greeting injected raw markup"
    assert "&lt;script&gt;" in html, "the greeting is missing entirely"


def test_the_html_part_fetches_nothing_and_tracks_nobody():
    """No remote asset of any kind: the brand faces are bundled for the board
    and deliberately never fetched, an <img> would be a tracking pixel by
    accident, and a <style> block is stripped by Gmail so anything depending
    on one would silently lose its styling.
    """
    _, _, html = _rendered()
    assert "<img" not in html.lower()
    assert "<style" not in html.lower()
    assert "@font-face" not in html.lower()
    assert "@import" not in html.lower()
    srcs = _re.findall(r'(?:src|@import\s+url)\s*=?\s*["\']([^"\']+)', html)
    assert srcs == [], f"remote asset references: {srcs}"
    # Every href is one of ours and reachable by a human.
    hrefs = set(_re.findall(r'href="([^"]+)"', html))
    assert hrefs <= {_CTA, in_app.SITE, send.UNSUBSCRIBE_URL}, hrefs


def test_the_palette_tracks_the_board_stylesheet():
    """Email has no CSS variables, so the tokens are literals here. This is
    what stops them going stale: each one is compared to its definition in
    `web/src/styles.css` `:root`, by token NAME.
    """
    from pathlib import Path
    css = Path(__file__).resolve().parents[1] / "web" / "src" / "styles.css"
    root = css.read_text(encoding="utf-8").split(":root", 1)[1].split("\n}", 1)[0]
    # The negative lookahead is the point: without it `--accent-500:#4C9AFFCC`
    # matched on its first six characters and compared EQUAL to #4C9AFF, so a
    # token could gain an alpha channel and this test would not notice.
    defined = dict(_re.findall(
        r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})(?![0-9A-Fa-f])", root))
    # Fail closed: an empty parse must not read as "everything matches".
    assert len(defined) >= 10, f"stylesheet parse found only {len(defined)} tokens"
    for token, literal in in_app.PALETTE.items():
        assert token in defined, f"--{token} is not a board token any more"
        assert defined[token].upper() == literal.upper(), (
            f"--{token} is {defined[token]} in styles.css but {literal} here")


def _contrast(fg: str, bg: str) -> float:
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def test_every_text_colour_in_the_email_passes_AA():
    """DESIGN.md calls >=AA a hard rule, not a preference. Computed from the
    palette the document actually uses, on the surface each colour is painted
    on, so a future palette edit that breaks contrast fails here.
    """
    p = in_app.PALETTE
    pairs = [
        ("text-hi", "surface-1"), ("text", "surface-1"),
        ("text-muted", "surface-1"), ("accent-500", "surface-1"),
        ("text-hi", "base"), ("text-dim", "base"),
        ("base", "accent-500"),  # the button: dark ink on the accent fill
    ]
    for fg, bg in pairs:
        ratio = _contrast(p[fg], p[bg])
        assert ratio >= 4.5, f"--{fg} on --{bg} is {ratio:.2f}:1, below AA"
    # Positive control: the scorer can fail. Two near-identical greys must not
    # pass, or every assertion above is vacuous.
    assert _contrast("#1A1D27", "#1C1F29") < 4.5


def test_the_unsubscribe_is_reachable_rather_than_merely_present():
    """The bare https://getnohuman.com/unsubscribe that used to ship here is
    dead: the hosted page authenticates `?e=&t=HMAC(UNSUB_SECRET, address)`
    and this process has no access to that secret (nor should a desktop binary
    -- holding it would let anyone forge an unsubscribe for any address), so
    the link it sent answered HTTP 400 "This link isn't valid".
    """
    _, text, html = _rendered()
    assert send.UNSUBSCRIBE_URL.startswith("mailto:")
    # Not the dead link, in either part.
    assert "getnohuman.com/unsubscribe" not in text
    assert "getnohuman.com/unsubscribe" not in html
    # Visible in both, as base.py's header requires.
    assert send.UNSUBSCRIBE_URL in text
    assert "Unsubscribe" in _strip_tags(html)


def test_the_one_action_is_opening_this_installs_own_board():
    """The reader just set the app up; the action is to open it, not to read
    about it. The URL is THIS install's board, so a user who moved the server
    off the default port gets their own address rather than a wrong one.
    """
    _, text, html = in_app.in_app_welcome(
        send.UNSUBSCRIBE_URL, _ADDR, "http://127.0.0.1:9137")
    assert "Open no_human" in _strip_tags(html)
    # /open, not the board root: that route hands off to the `nohuman://`
    # scheme so the DESKTOP APP comes to the front. The button cannot link to
    # the scheme directly — measured against real Gmail, which strips the href
    # of any non-standard scheme and leaves a dead button.
    assert 'href="http://127.0.0.1:9137/open"' in html
    assert "http://127.0.0.1:9137/open" in text
    # The docs link the button used to carry is not the action any more.
    assert f'href="{in_app.SITE}/docs"' not in html


def test_the_board_url_resolves_from_config_and_never_raises():
    """Positive controls on every arm, including the one that matters most:
    a wildcard bind is not something a browser can open.
    """
    assert in_app.local_board_url(
        {"notifications": {"board_url": "https://board.example.com/"}}
    ) == "https://board.example.com"
    assert in_app.local_board_url(
        {"server": {"host": "0.0.0.0", "port": 9999}}
    ) == "http://127.0.0.1:9999"
    assert in_app.local_board_url(
        {"server": {"host": "127.0.0.1", "port": 8420}}
    ) == "http://127.0.0.1:8420"
    # Unreadable config degrades, never raises: this runs on the onboarding
    # path and a transport problem must not break registration.
    assert in_app.local_board_url({"server": {"port": "not-a-port"}}) == (
        in_app.CTA_URL_FALLBACK)
    assert in_app.local_board_url({}) == "http://127.0.0.1:8420"


def test_the_email_carries_no_agent_role_colours():
    """DESIGN.md's accent is a SINGLE blue. The five `--agent-*` role hues
    identify an agent on the board; in an email they identify nothing, so
    none of them belongs here.
    """
    _, _, html = _rendered()
    for hue in ("#45C8DC", "#9F8FEF", "#E8A04F", "#4ADE80", "#57C98A"):
        assert hue.lower() not in html.lower(), f"role hue {hue} is in the email"
    # Not just 6-digit hex: a role hue smuggled in as rgb(), #f0a, an 8-digit
    # value or a named colour was invisible to the narrower pattern.
    colours = {c.lower() for c in _re.findall(
        r"#[0-9A-Fa-f]{3,8}\b|rgba?\([^)]*\)", html)}
    named = _re.findall(
        r":\s*(aqua|fuchsia|lime|maroon|navy|olive|teal|purple|green|orange"
        r"|red|blue|yellow|cyan|magenta)\b", html, _re.I)
    assert named == [], f"named colours bypass the palette: {named}"
    # Positive control: the palette it DOES use is present, so the assertions
    # above are not passing over a document with no colour in it.
    assert colours == {v.lower() for v in in_app.PALETTE.values()}


def test_the_default_path_reads_config_and_creates_nothing(monkeypatch, tmp_path):
    """`local_board_url()` with no argument is the only form that ships, and
    it was the one form no test exercised — replacing its `load_config()` call
    with `{}` left the suite green.

    It also must not WRITE: `load_config`'s default is create_if_missing=True,
    so rendering an email created ~/.no_human/config.yaml and chmod'd the
    directory as a side effect of composing a string.
    """
    seen = {}

    def fake_load_config(*a, **kw):
        seen["kwargs"] = kw
        return {"server": {"host": "127.0.0.1", "port": 7331}}

    monkeypatch.setattr("no_human.config.load_config", fake_load_config)
    assert in_app.local_board_url() == "http://127.0.0.1:7331"
    assert seen["kwargs"].get("create_if_missing") is False, (
        "rendering an email must not create the user's config file")


def test_an_ipv6_wildcard_bind_becomes_loopback_not_a_bracketed_wildcard():
    """`::` means "every interface" exactly as 0.0.0.0 does, so it must become
    loopback. Bracketing it instead yields http://[::]:8420, which is
    well-formed and still not something a reader can open.
    """
    for wildcard in ("::", "[::]", "0.0.0.0", ""):
        url = in_app.local_board_url({"server": {"host": wildcard, "port": 8420}})
        assert url == "http://127.0.0.1:8420", f"host {wildcard!r} gave {url}"


def test_a_bare_ipv6_host_is_bracketed():
    """`http://::1:8420` is not an address. Only the WILDCARD ipv6 form was
    handled, so a loopback-ipv6 install got an unopenable URL.
    """
    assert in_app.local_board_url(
        {"server": {"host": "::1", "port": 8420}}) == "http://[::1]:8420"
    assert in_app.local_board_url(
        {"server": {"host": "[::1]", "port": 8420}}) == "http://[::1]:8420"


def test_an_out_of_range_port_falls_back_instead_of_shipping_a_dead_url():
    """`int(port or DEFAULT)` was wrong twice: a configured port 0 was
    silently rewritten to 8420, and -1 / 999999 went straight into the URL.
    """
    for bad in (0, -1, 999999, 65536, "not-a-port", None, True):
        url = in_app.local_board_url({"server": {"host": "127.0.0.1", "port": bad}})
        assert url == "http://127.0.0.1:8420", f"port {bad!r} produced {url}"
    # Positive control: a legal non-default port IS honoured, so the assertions
    # above are not passing because everything falls back.
    assert in_app.local_board_url(
        {"server": {"host": "127.0.0.1", "port": 9000}}) == "http://127.0.0.1:9000"


def test_the_seam_carries_a_caller_supplied_board_url():
    """`nh start --port N` is never written back to config.yaml, so config is
    not a source of truth for where the server is listening. The address the
    server actually bound is recorded on app.state and passed down; this pins
    that the seam honours it end to end.
    """
    captured = []

    class _T:
        def send(self, msg):
            captured.append(msg)

    status = send.send_welcome("a@b.co", transport=_T(),
                               board_url="http://127.0.0.1:9000")
    assert status == "sent"
    [msg] = captured
    assert 'href="http://127.0.0.1:9000/open"' in msg.html
    assert "http://127.0.0.1:9000/open" in msg.body


def test_the_open_route_hands_off_to_the_desktop_app():
    """The button's target. It exists because a mail client will not keep a
    `nohuman://` href — measured by sending a real message to Gmail and
    reading the delivered DOM: the custom-scheme anchor came back with NO
    href, while an `http://127.0.0.1` href survived untouched.

    So the email links to this server, and this page performs the handoff.
    No JavaScript: the app's CSP is `script-src 'self'`, which would block an
    inline script, and a meta refresh needs none.
    """
    import asyncio

    import httpx

    from no_human.api.app import app as fastapi_app

    async def _get():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fastapi_app),
            base_url="http://127.0.0.1:8420",
        ) as c:
            return await c.get("/open")

    r = asyncio.run(_get())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "nohuman://open" in body, "the page does not hand off to the app"
    assert 'http-equiv="refresh"' in body, "no automatic handoff"
    assert 'href="/"' in body, "no in-browser fallback for an unregistered scheme"
    # The CSP forbids inline script; a handoff that needs one would be dead.
    assert "<script" not in body.lower()


def test_the_open_route_is_not_shadowed_by_the_spa_catch_all(tmp_path):
    """The SPA mounts `@app.get("/{path:path}")`, which matches EVERYTHING.
    `/open` survives only because it is declared before that block — and the
    block is inside `if (_WEB_DIST / "index.html").is_file()`, so a run in a
    worktree with no `web/dist` has no catch-all to be shadowed BY and
    structurally cannot see this defect.

    Run in a SUBPROCESS against a throwaway copy of the tree with a dist
    planted: `_WEB_DIST` is resolved at import time, so it cannot be repointed
    after the fact, and reloading `api.app` in-process would hand every later
    test a different `app` object.
    """
    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    src = Path(no_human_email_src_root())
    tree = tmp_path / "tree"
    # `.venv` is the one that matters: measured on the real checkout, leaving
    # it out of this list copies 8128 files / 496 MB on EVERY run, in CI too
    # (the workflow does `uv sync --frozen`, so the venv is right there).
    shutil.copytree(src.parent, tree, symlinks=True,
                    ignore=shutil.ignore_patterns("node_modules", "__pycache__",
                                                  ".git", "dist", ".venv",
                                                  ".pytest_cache", "node_modules"))
    dist = tree / "web" / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>SPA INDEX</title>")

    probe = """
import asyncio, json, httpx
from no_human.api.app import app, _WEB_DIST
async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8420") as c:
        o = await c.get("/open"); r = await c.get("/")
        print(json.dumps({"dist": (_WEB_DIST / "index.html").is_file(),
                          "web_dist": str(_WEB_DIST),
                          "status": o.status_code,
                          "handoff": "nohuman://open" in o.text,
                          "open_is_spa": "SPA INDEX" in o.text,
                          "root_is_spa": "SPA INDEX" in r.text}))
asyncio.run(main())
"""
    out = subprocess.run([sys.executable, "-c", probe], cwd=str(tree),
                         env={"PYTHONPATH": str(tree / "src"), "PATH": "/usr/bin:/bin",
                              "HOME": str(tmp_path)},
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["dist"] is True, "the probe never built a dist; it proves nothing"
    # Which tree did it import? Implicit provenance is not provenance.
    assert str(tree) in got["web_dist"], (
        f"the probe imported {got['web_dist']}, not the tree under test")
    # A 500 whose body still carries the string is not a working handoff.
    assert got["status"] == 200, f"/open answered {got['status']}"
    # Positive control: the catch-all IS live in this tree.
    assert got["root_is_spa"] is True, "no SPA catch-all to be shadowed by"
    assert got["handoff"] is True, "the SPA catch-all swallowed /open"
    assert got["open_is_spa"] is False


def no_human_email_src_root():
    """The `src` directory of the tree under test (its parent is the tree)."""
    return str(Path(in_app.__file__).resolve().parents[2])


def test_a_board_url_that_is_not_an_absolute_http_url_never_reaches_the_href():
    """These three guards were added in answer to a review and had NO test:
    deleting any one of them left the whole named gate green.

    The scheme check used to be `cleaned.split(":", 1)[0]`, which accepts a
    BARE scheme -- "http" has no colon, so the prefix is "http" and the
    button's href became the relative `http/open`.
    """
    for bad in ("javascript:alert(1)", "ftp://box:8420", "http", "https",
                "http:", "//box:8420", "box:8420", "data:text/html,x",
                "http://u:p@box:8420"):
        got = in_app.local_board_url({"notifications": {"board_url": bad}})
        assert got == in_app.CTA_URL_FALLBACK, f"{bad!r} produced {got!r}"
    # Positive control: a legitimate absolute http(s) URL IS honoured, so the
    # assertions above are not passing because everything falls back.
    for good in ("https://board.example.com/", "http://box.local:8420"):
        assert in_app.local_board_url(
            {"notifications": {"board_url": good}}) == good.rstrip("/")


def test_a_host_that_cannot_appear_in_a_url_falls_back():
    """The comment above this guard promises the port's reasoning applied to
    the host: a value that cannot resolve is a broken config, and the honest
    answer is the documented default rather than a mailed dead link.

    The IPv6 arm is the subtle one: bracketing anything containing a colon
    turned `host:8080` into `http://[host:8080]:8420` -- dressing a broken
    host as a valid one. Only a real IPv6 literal is bracketed now, and a
    zoned link-local falls back because its `%` needs percent-encoding
    (RFC 6874) and it is unreachable from a mail client anyway.
    """
    for bad in ("a b", "a/b", "u@h", "a\r\nb", "a\tb", "h?x", "h#f",
                "host:8080", "1.2.3.4:99", "fe80::1%en0", "a\\b"):
        got = in_app.local_board_url({"server": {"host": bad, "port": 8420}})
        assert got == "http://127.0.0.1:8420", f"host {bad!r} produced {got!r}"
    # Positive controls: legal hosts survive, including a real IPv6 literal.
    assert in_app.local_board_url(
        {"server": {"host": "box.local", "port": 8420}}) == "http://box.local:8420"
    assert in_app.local_board_url(
        {"server": {"host": "::1", "port": 8420}}) == "http://[::1]:8420"


def test_the_open_suffix_is_joined_not_concatenated():
    """`board_url + "/open"` put the query BEFORE the path segment:
    `http://box:8420/?theme=dark` became a query of `theme=dark/open` and a
    path of `/`, i.e. the board root -- the button silently stopped being the
    handoff.
    """
    cases = {
        "http://box:8420/?theme=dark": "http://box:8420/open",
        "http://box:8420/#/tasks": "http://box:8420/open",
        "http://box:8420/open": "http://box:8420/open",
        "http://box:8420/open/": "http://box:8420/open",
        "http://box:8420": "http://box:8420/open",
        "https://proxy.example.com/nh": "https://proxy.example.com/nh/open",
    }
    for given, want in cases.items():
        _, text, html = in_app.in_app_welcome(send.UNSUBSCRIBE_URL, _ADDR, given)
        assert f'href="{want}"' in html, f"{given!r} -> expected {want!r}"
        assert want in text


def test_a_caller_supplied_board_url_is_validated_like_the_config_one():
    """The route PREFERS `app.state.board_url`, which `nh start` builds from a
    raw `--host`. Every guard used to live only in `local_board_url`, i.e. only
    on the config path, so the preferred path skipped all of them.

    Measured before the validator was shared: `--host 0.0.0.0` — the container
    image's own documented default — mailed `http://0.0.0.0:8420/open`;
    `--host ::1` mailed `http://::1:8420/open`, the exact spelling this module
    claims to fix; and `--host '['` made `send_welcome` RAISE, which its
    docstring forbids. The pre-existing seam test passes only
    `http://127.0.0.1:9000`, so it structurally could not see any of it.
    """
    for host in ("0.0.0.0", "::", "::1", "", "box:8080", "[", "1.2.3.4:99"):
        raw = f"http://{host}:8420"
        _, text, html = in_app.in_app_welcome(send.UNSUBSCRIBE_URL, _ADDR, raw)
        assert f'href="{in_app.CTA_URL_FALLBACK}/open"' in html, (
            f"--host {host!r} produced {raw!r} and it reached the href")
        assert "0.0.0.0" not in html and "://:" not in html

    # Positive controls: legitimate caller values are NOT mangled, or the
    # assertions above would pass by refusing everything.
    keep = {
        "http://127.0.0.1:9000": "http://127.0.0.1:9000/open",
        "http://box.local:8420": "http://box.local:8420/open",
        # No explicit port means the scheme's default; adding the board's 8420
        # would silently point at a different address.
        "https://proxy.example.com/nh": "https://proxy.example.com/nh/open",
    }
    for given, want in keep.items():
        _, _, html = in_app.in_app_welcome(send.UNSUBSCRIBE_URL, _ADDR, given)
        assert f'href="{want}"' in html, f"{given!r} should stay {want!r}"


def test_send_welcome_never_raises_on_a_malformed_board_url():
    """`send_welcome`'s docstring says it never raises, and the registration
    route has ALREADY persisted the address by the time it runs. `render_welcome`
    sits outside its try, so a urlsplit ValueError on a bare `[` escaped the
    route entirely.
    """
    class _T:
        def send(self, msg):
            pass
    for bad in ("http://[:8420", "http://[", "::::", "http://box:99999"):
        assert send.send_welcome("a@b.co", transport=_T(), board_url=bad) == "sent"
