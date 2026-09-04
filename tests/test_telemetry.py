"""Opt-in usage telemetry (src/no_human/telemetry.py) + the CSP variants.

Pins the privacy contract: a CLOSED event allowlist (unknown kind/prop raises),
zero network without consent, the exact ingestion body shape with consent, a
CSP that is byte-identical to the historical value while disabled and gains
exactly the two PostHog hosts when enabled, and an /api/config echo that keeps
the PUBLISHABLE PostHog client token intact (the field is deliberately named
`posthog_publishable` so the secret scrubber's name-based rules do not eat it).
"""
from __future__ import annotations

import contextlib
import importlib
import json
import re
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human import telemetry
from no_human.api.app import app
from no_human.config import DEFAULT_CONFIG, Config
from no_human.core.db import Store

app_module = importlib.import_module("no_human.api.app")

# The CSP the board has always sent — the disabled variant must stay
# BYTE-IDENTICAL to this literal (not compared via the constant, on purpose).
_CSP_TODAY = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self' ws: wss:; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)

_ENABLED = {
    "enabled": True,
    "endpoint": "https://ingest.invalid/collect",
    "instance_id": "11111111-2222-3333-4444-555555555555",
    "posthog_publishable": "phc_test",
}


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


# ------------------------- closed allowlist ------------------------------- #

def test_unknown_event_kind_raises():
    with pytest.raises(ValueError, match="unknown event kind"):
        telemetry.record("task_opened", config={"telemetry": _ENABLED})


def test_unknown_prop_raises_even_for_known_kind():
    # `title` is exactly the class of thing that must never ship.
    with pytest.raises(ValueError, match="not allowed"):
        telemetry.record("task_created", config={"telemetry": _ENABLED},
                         source="feature", title="secret title")


def test_allowlist_is_the_documented_closed_set():
    # Every event also carries "environment" (real/bench/test/ci/dev),
    # stamped by record() via environment() — see telemetry.py's docstring.
    assert telemetry._ALLOWED_EVENTS == {
        "app_started": frozenset({"environment"}),
        "task_created": frozenset({"source", "environment"}),
        "task_completed": frozenset({"status", "duration_bucket", "attempts", "environment"}),
        "task_failed": frozenset({"category", "reason_category", "environment"}),
        "approve_clicked": frozenset({"environment"}),
        "feature_used": frozenset({"name", "environment"}),
    }


def test_hook_normalizes_unknown_task_kinds_to_other(monkeypatch):
    """Task.kind is an unvalidated str at the API layer, so the orchestrator
    hook must clamp `source` to the known vocabulary — a client-invented kind
    (which could carry title-like text) leaves the machine only as "other"."""
    from no_human.core.orchestrator import Orchestrator

    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))

    class _Stub:  # only what the hook touches
        config: dict = {}
        _telemetry_hook = Orchestrator._telemetry_hook

    stub = _Stub()
    stub._telemetry_hook("kind", {"task_kind": "my secret project name"})
    stub._telemetry_hook("kind", {"task_kind": "feature"})
    stub._telemetry_hook("kind", {})
    sources = [p["source"] for k, p in sent if k == "task_created"]
    assert sources == ["other", "feature", "unknown"]


def test_failed_hook_emits_reason_category(monkeypatch):
    """Keeps the orchestrator wiring honest: `_telemetry_hook`'s "failed"
    branch resolves `reason_category` via `failure_reason_category()`, so an
    explicit valid value passes through and a missing one degrades to
    "other" rather than raising or dropping the event."""
    from no_human.core.orchestrator import Orchestrator

    sent = []
    monkeypatch.setattr(
        telemetry, "record",
        lambda kind, config=None, **props: sent.append((kind, props)))

    class _Stub:  # only what the hook touches
        config: dict = {}
        _telemetry_hook = Orchestrator._telemetry_hook

    stub1 = _Stub()
    stub1._telemetry_hook("failed", {"reason_category": "infra"})
    stub2 = _Stub()
    stub2._telemetry_hook("failed", {})
    reasons = [p["reason_category"] for k, p in sent if k == "task_failed"]
    assert reasons == ["infra", "other"]


# --- failure reason category ----------------------------------------------- #

def test_task_failed_accepts_every_reason_category(temp_home, no_thread):
    for value in telemetry.FAILURE_REASON_CATEGORIES:
        telemetry.record("task_failed", config={"telemetry": _ENABLED},
                         category="failed", reason_category=value)
    path = temp_home / ".no_human" / "telemetry-queue.jsonl"
    lines = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    sent_reasons = {ev["props"]["reason_category"] for ev in lines}
    assert sent_reasons == set(telemetry.FAILURE_REASON_CATEGORIES)


def test_out_of_enum_reason_category_raises():
    for bad in ("budget", "BUDGET_EXHAUSTED", "repo acme/private failed at step 3"):
        with pytest.raises(ValueError, match="not allowed"):
            telemetry.record("task_failed", config={"telemetry": _ENABLED},
                             category="failed", reason_category=bad)
        # Validated even when telemetry is disabled — an out-of-enum value
        # is a privacy bug regardless of consent state.
        with pytest.raises(ValueError, match="not allowed"):
            telemetry.record(
                "task_failed", config={"telemetry": {"enabled": False}},
                category="failed", reason_category=bad)


def test_reason_category_is_closed_and_carries_no_free_text():
    assert (telemetry._ALLOWED_PROP_VALUES[("task_failed", "reason_category")]
            is telemetry.FAILURE_REASON_CATEGORIES)
    assert telemetry.FAILURE_REASON_CATEGORIES == {
        "budget_exhausted", "review_failed", "max_attempts", "infra",
        "tamper_blocked", "blocker_parked", "other",
    }
    for value in telemetry.FAILURE_REASON_CATEGORIES:
        assert re.fullmatch(r"[a-z_]{1,24}", value), value


def test_failure_reason_category_maps_explicit_blocker_and_unknown():
    # explicit valid value wins outright
    assert telemetry.failure_reason_category("infra", "BUDGET_EXHAUSTED") == "infra"
    # explicit invalid + a mapped blocker category -> the mapping
    assert telemetry.failure_reason_category(
        "not-a-category", "BUDGET_EXHAUSTED") == "budget_exhausted"
    assert telemetry.failure_reason_category(None, "TRANSIENT_INFRA") == "infra"
    assert telemetry.failure_reason_category(None, "QUOTA") == "infra"
    assert telemetry.failure_reason_category(None, "AMBIGUITY") == "blocker_parked"
    # both None -> "other"
    assert telemetry.failure_reason_category(None, None) == "other"
    # garbage blocker string -> "other"
    assert telemetry.failure_reason_category(None, "NOT_A_REAL_CATEGORY") == "other"


def test_task_failed_still_carries_environment(temp_home, no_thread, monkeypatch):
    monkeypatch.setenv("NH_ENV", "ci")
    telemetry.record("task_failed", config={"telemetry": _ENABLED},
                     category="failed", reason_category="infra")
    path = temp_home / ".no_human" / "telemetry-queue.jsonl"
    [line] = [ln for ln in path.read_text().splitlines() if ln.strip()]
    props = json.loads(line)["props"]
    assert props["environment"] == "ci"
    assert props["reason_category"] == "infra"


# ------------------------- consent gate ----------------------------------- #

def test_disabled_records_nothing_and_touches_no_network(temp_home, no_network):
    telemetry.record("app_started", config={"telemetry": {"enabled": False,
                                                          "endpoint": "https://x"}})
    assert not (temp_home / ".no_human" / "telemetry-queue.jsonl").exists()
    assert telemetry.flush({"enabled": False, "endpoint": "https://x"}) == 0
    assert no_network == []


def test_enabled_without_endpoint_is_a_noop(temp_home, no_network):
    telemetry.record("app_started",
                     config={"telemetry": {"enabled": True, "endpoint": ""}})
    assert not (temp_home / ".no_human" / "telemetry-queue.jsonl").exists()
    assert no_network == []


# ------------------------- ingestion contract ----------------------------- #

def test_enabled_flush_posts_the_contract_body(temp_home, no_network, no_thread):
    telemetry.record("task_completed", config={"telemetry": _ENABLED},
                     status="done", duration_bucket="<10m", attempts=2)
    sent = telemetry.flush(_ENABLED)
    assert sent == 1
    assert len(no_network) == 1
    req, timeout = no_network[0]
    assert timeout == pytest.approx(3.0)
    assert req.full_url == _ENABLED["endpoint"]
    body = json.loads(req.data.decode())
    assert set(body) == {"instance_id", "version", "events"}
    assert body["instance_id"] == _ENABLED["instance_id"]
    from no_human import __version__
    assert body["version"] == __version__
    [event] = body["events"]
    # "name" is the WIRE contract — the ingestion Lambda validates
    # event.get("name") and 400s anything else (learned live: "kind" made
    # every batch bounce while the client logged nothing, fail-open).
    assert event["name"] == "task_completed"
    assert "kind" not in event
    assert event["props"] == {"status": "done", "duration_bucket": "<10m",
                             "attempts": 2}
    # sent events leave the queue — a second flush sends nothing
    assert telemetry.flush(_ENABLED) == 0
    assert len(no_network) == 1


def test_queue_is_bounded_drop_oldest(temp_home, no_thread):
    for i in range(telemetry.MAX_QUEUE_LINES + 25):
        telemetry.record("feature_used", config={"telemetry": _ENABLED},
                         name=f"f{i}")
    lines = (temp_home / ".no_human" / "telemetry-queue.jsonl").read_text().splitlines()
    assert len(lines) == telemetry.MAX_QUEUE_LINES
    # the OLDEST events were dropped, the newest survived
    assert json.loads(lines[-1])["props"]["name"] == f"f{telemetry.MAX_QUEUE_LINES + 24}"
    assert json.loads(lines[0])["props"]["name"] == "f25"


def test_default_endpoint_is_empty_so_no_cloud_identifier_ships():
    # The L3 brain invariant bans cloud deployment identifiers from the local
    # product's source; the ingestion URL therefore arrives via config.yaml
    # (like team_brain.control_plane_url), and the shipped default is inert.
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["telemetry"]["endpoint"] == ""
    # Usage insights default ON (opt-out) as of the 2026-08-27 operator flip.
    assert DEFAULT_CONFIG["telemetry"]["enabled"] is True


def test_duration_bucketing():
    assert telemetry.duration_bucket(0) == "<10m"
    assert telemetry.duration_bucket(9.9) == "<10m"
    assert telemetry.duration_bucket(10) == "10-30m"
    assert telemetry.duration_bucket(45) == "30-60m"
    assert telemetry.duration_bucket(61) == ">60m"


# ------------------------- CSP variants ----------------------------------- #

def test_csp_disabled_is_byte_identical_to_today():
    assert app_module._build_csp({}) == _CSP_TODAY
    assert app_module._build_csp({"telemetry": {"enabled": False}}) == _CSP_TODAY
    # enabled but unconfigured (no client token) also stays strict
    assert app_module._build_csp(
        {"telemetry": {"enabled": True, "posthog_publishable": ""}}) == _CSP_TODAY


def test_csp_enabled_adds_exactly_the_two_posthog_hosts():
    got = app_module._build_csp(
        {"telemetry": {"enabled": True, "posthog_publishable": "phc_x"}})
    assert got == (
        "default-src 'self'; "
        "script-src 'self' https://us-assets.i.posthog.com; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss: "
        "https://us.i.posthog.com https://us-assets.i.posthog.com; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    )


# ------------------------- /api/config echo ------------------------------- #

@pytest_asyncio.fixture
async def client(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    app.state.store = s
    app.state.config = Config(
        data={"telemetry": {
            "enabled": False,
            "posthog_publishable": "phc_test_publishable_token",
            "posthog_host": "https://us.i.posthog.com",
            "endpoint": "https://ingest.invalid/collect",
            "instance_id": "",
        }},
        path=tmp_path / "config.yaml",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c
    await s.close()


@pytest.mark.asyncio
async def test_config_echo_keeps_publishable_token_intact(client):
    r = await client.get("/api/config")
    assert r.status_code == 200
    tel = r.json()["telemetry"]
    # The PUBLISHABLE client token must survive the scrub — the browser needs
    # it to init the client. (The field name avoids the scrubber's name rules
    # by design; weakening the scrubber itself would be the wrong fix.)
    assert tel["posthog_publishable"] == "phc_test_publishable_token"
    assert tel["enabled"] is False
    assert tel["posthog_host"] == "https://us.i.posthog.com"
    assert tel["endpoint"] == "https://ingest.invalid/collect"


@pytest.mark.asyncio
async def test_csp_header_default_matches_today(client):
    # No lifespan ran in this fixture, so the middleware falls back to the
    # strict constant — which must be the historical byte-identical value.
    if hasattr(app.state, "csp"):
        del app.state.csp
    r = await client.get("/api/config")
    assert r.headers["Content-Security-Policy"] == _CSP_TODAY


# ------------------------- consent endpoint -------------------------------- #

@pytest.mark.asyncio
async def test_consent_write_refused_without_same_origin(client):
    # A write with no Origin (curl / local malicious process) is refused…
    r = await client.put("/api/telemetry/consent", json={"enabled": True})
    assert r.status_code == 403
    # …and so is a cross-origin browser write (drive-by page).
    r = await client.put("/api/telemetry/consent", json={"enabled": True},
                         headers={"Origin": "http://localhost.evil.com"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_consent_enable_mints_stable_id_persists_and_widens_csp(
        client, tmp_path, monkeypatch):
    import yaml

    from no_human import config as config_mod
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("telemetry:\n  enabled: false\n"
                        "  posthog_publishable: phc_test_publishable_token\n")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    origin = {"Origin": "http://127.0.0.1:8787"}
    r = await client.put("/api/telemetry/consent", json={"enabled": True},
                         headers=origin)
    assert r.status_code == 200
    assert r.json()["reload_required"] is True

    on_disk = yaml.safe_load(cfg_path.read_text())["telemetry"]
    assert on_disk["enabled"] is True
    minted = on_disk["instance_id"]
    assert len(minted) == 36  # uuid4, minted server-side on first enable

    # CSP recomputed live: the widened variant is now served…
    r = await client.get("/api/config")
    assert "us-assets.i.posthog.com" in r.headers["Content-Security-Policy"]

    # …and toggling off both restores the strict header and KEEPS the id
    # (one stable anonymous id, not a fresh "new install" per toggle).
    r = await client.put("/api/telemetry/consent", json={"enabled": False},
                         headers=origin)
    assert r.status_code == 200
    on_disk = yaml.safe_load(cfg_path.read_text())["telemetry"]
    assert on_disk["enabled"] is False
    assert on_disk["instance_id"] == minted
    r = await client.get("/api/config")
    assert "posthog" not in r.headers["Content-Security-Policy"]

    r = await client.put("/api/telemetry/consent", json={"enabled": True},
                         headers=origin)
    assert yaml.safe_load(cfg_path.read_text())["telemetry"]["instance_id"] == minted


def test_only_the_consent_endpoint_writes_telemetry_keys():
    """The onboarding consent step must reuse PUT /api/telemetry/consent
    verbatim — no new write path. Enforce it structurally: walk app.py's AST
    and confirm no function OTHER than save_telemetry_consent ever writes
    telemetry.enabled/instance_id."""
    import ast

    src = Path(app_module.__file__).read_text()
    tree = ast.parse(src)
    writers = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and (
                    "telemetry.enabled" in sub.value or "telemetry.instance_id" in sub.value
                ):
                    writers.add(node.name)
    assert writers == {"save_telemetry_consent"}


@pytest.mark.asyncio
async def test_onboarding_yes_lands_enabled_true_in_config_yaml(
        client, tmp_path, monkeypatch):
    """The wizard's Yes path is nothing but a call into the existing consent
    endpoint, followed by the existing onboarding-complete endpoint recording
    that the question was asked — both against the SAME config.yaml."""
    import yaml

    from no_human import config as config_mod
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("telemetry:\n  enabled: false\n"
                        "  posthog_publishable: phc_test_publishable_token\n")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    origin = {"Origin": "http://127.0.0.1:8787"}
    r = await client.put("/api/telemetry/consent", json={"enabled": True},
                         headers=origin)
    assert r.status_code == 200

    r = await client.post("/api/onboarding/complete",
                          json={"telemetry_asked": True})
    assert r.status_code == 200

    on_disk = yaml.safe_load(cfg_path.read_text())
    assert on_disk["telemetry"]["enabled"] is True
    assert len(on_disk["telemetry"]["instance_id"]) == 36
    assert on_disk["onboarding"]["telemetry_asked"] is True


def test_onboarding_consent_copy_matches_the_config_contract():
    """web/src/onboardingConsent.js pins a byte-identical twin of the privacy-
    posture QUESTION constant (its own header comment says so) — catch drift
    either way. The former TELEMETRY_CONSENT_SETTINGS_HINT ("Settings > Usage
    insights") was removed with the onboarding step + Settings pane it named
    (operator, 2026-08-26); config.yaml `telemetry.enabled: false` is the opt-out."""
    from no_human.config import TELEMETRY_CONSENT_QUESTION

    js_path = (Path(__file__).resolve().parent.parent
               / "web" / "src" / "onboardingConsent.js")
    js = js_path.read_text()

    def _extract(name):
        m = re.search(name + r' =\s*((?:"[^"]*"\s*\+?\s*\n?)+);', js)
        assert m, f"could not find {name} in onboardingConsent.js"
        parts = re.findall(r'"([^"]*)"', m.group(1))
        return "".join(parts)

    assert _extract("TELEMETRY_CONSENT_QUESTION") == TELEMETRY_CONSENT_QUESTION
    assert "never code, prompts, titles, paths or tokens" in TELEMETRY_CONSENT_QUESTION
    # The removed hint must not creep back in either surface.
    assert "TELEMETRY_CONSENT_SETTINGS_HINT" not in js
    assert not hasattr(__import__("no_human.config", fromlist=["config"]),
                       "TELEMETRY_CONSENT_SETTINGS_HINT")


def test_legacy_kind_queue_lines_drain_as_name(temp_home, no_network, no_thread):
    """Queue lines from the first release carried "kind"; flush must
    normalize them to the wire's "name" so old queues drain, not 400."""
    path = temp_home / ".no_human" / "telemetry-queue.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"kind":"app_started","ts":1786889836,"props":{}}\n')
    assert telemetry.flush(_ENABLED) == 1
    [(req, _)] = no_network
    [event] = json.loads(req.data.decode())["events"]
    assert event["name"] == "app_started"
    assert "kind" not in event


def test_missing_instance_id_mints_persists_and_still_sends(
        temp_home, no_network, no_thread, monkeypatch):
    """The hand-edited-config activation path can leave instance_id empty;
    the Lambda 400s the whole batch on a non-uuid4 id. Flush must mint,
    persist, and ship the batch — never wedge (review round 2)."""
    import uuid

    import yaml

    from no_human import config as config_mod
    # Pin the REAL-context mint-and-persist path explicitly: without this,
    # pytest's own PYTEST_CURRENT_TEST would classify this process as "test"
    # and ensure_instance_id would take the sentinel branch instead (never
    # persisting), breaking the on-disk assertion below by design.
    monkeypatch.setenv("NH_ENV", "real")
    cfg_path = temp_home / ".no_human" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("telemetry:\n  enabled: true\n")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    section = {"enabled": True, "endpoint": "https://ingest.invalid/collect",
               "instance_id": ""}
    telemetry.record("app_started", config={"telemetry": section})
    assert telemetry.flush(dict(section)) == 1
    [(req, _)] = no_network
    sent_id = json.loads(req.data.decode())["instance_id"]
    assert uuid.UUID(sent_id).version == 4  # a valid id shipped
    on_disk = yaml.safe_load(cfg_path.read_text())["telemetry"]
    assert on_disk["instance_id"] == sent_id  # and it persisted, stable


def test_poisoned_queue_line_is_dropped_not_wedging(temp_home, no_network,
                                                    no_thread):
    """The Lambda rejects a batch wholesale on one bad event; a poisoned
    line must be dropped like corrupt JSON, not block every later flush."""
    path = temp_home / ".no_human" / "telemetry-queue.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"name":"task_opened","ts":1786889836,"props":{}}\n'      # bad kind
        '{"kind":"x","name":"app_started","ts":1,"props":{}}\n'    # extra key
        '{"name":"app_started","ts":1786889836,"props":{"t":1}}\n' # bad prop
        '{"name":"app_started","ts":1786889836,"props":{}}\n')     # good
    assert telemetry.flush(_ENABLED) == 1
    [(req, _)] = no_network
    events = json.loads(req.data.decode())["events"]
    assert events == [{"name": "app_started", "ts": 1786889836, "props": {}}]
    assert not path.read_text().strip()  # poisoned lines gone, queue drained


def test_client_allowlist_matches_the_deployed_lambda_contract():
    """CONTRACT FIXTURE — the hosted ingestion endpoint validates batches
    WHOLESALE against its own closed allowlist, so a client-side event the
    server doesn't know silently 400s every batch (the server carries the
    mirror of this pin). This is the server's allowlist as deployed
    2026-08-16; if this test fails you are adding a client event — ship the
    server-side allowlist change FIRST, then update this fixture."""
    # "environment" is a client-side-only addition (stripped by
    # _strip_environment before the Lambda ever sees it — the server's
    # allowlist itself is unchanged); it's still listed here because this
    # fixture pins the CLIENT'S `_ALLOWED_EVENTS`, not the wire body.
    deployed_lambda_events = {
        "app_started": frozenset({"environment"}),
        "task_created": frozenset({"source", "environment"}),
        "task_completed": frozenset({"status", "duration_bucket", "attempts", "environment"}),
        "task_failed": frozenset({"category", "reason_category", "environment"}),
        "approve_clicked": frozenset({"environment"}),
        "feature_used": frozenset({"name", "environment"}),
    }
    assert telemetry._ALLOWED_EVENTS == deployed_lambda_events
    # The server also regex-validates `version` (semver-ish, MAJOR.MINOR.
    # PATCH + optional short suffix) and 400s the whole batch otherwise —
    # a release-versioning change must trip THIS test, not the fleet.
    import re
    from no_human import __version__
    assert re.match(
        r"^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}(?:[-+][0-9A-Za-z.\-]{1,16})?$",
        __version__)


def test_bad_ts_and_bad_prop_values_are_dropped_not_wedging(temp_home,
                                                            no_network,
                                                            no_thread):
    """Round 3: the server also validates ts bounds and prop VALUE shapes;
    a reset-clock ts=0 line or an oversize prop value must be dropped, not
    400 the batch forever."""
    path = temp_home / ".no_human" / "telemetry-queue.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    long = "x" * 129
    path.write_text(
        '{"name":"app_started","ts":0,"props":{}}\n'                # reset clock
        '{"name":"app_started","ts":true,"props":{}}\n'             # bool ts
        f'{{"name":"feature_used","ts":1786889836,"props":{{"name":"{long}"}}}}\n'
        '{"name":"task_completed","ts":1786889836,"props":{"attempts":4294967296}}\n'
        '{"name":"app_started","ts":1786889836,"props":{}}\n')      # good
    assert telemetry.flush(_ENABLED) == 1
    [(req, _)] = no_network
    events = json.loads(req.data.decode())["events"]
    assert events == [{"name": "app_started", "ts": 1786889836, "props": {}}]


def test_noncanonical_uuid_in_config_is_canonicalized(temp_home, no_network,
                                                      no_thread, monkeypatch):
    """uuid.UUID accepts braced/dashless forms the server's 36-char check
    rejects; the client must ship the canonical dashed form."""
    from no_human import config as config_mod
    cfg_path = temp_home / ".no_human" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("telemetry:\n  enabled: true\n")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    dashless = "11111111222243338444555555555555"  # a v4, dashless
    section = {"enabled": True, "endpoint": "https://ingest.invalid/collect",
               "instance_id": dashless}
    telemetry.record("app_started", config={"telemetry": section})
    assert telemetry.flush(dict(section)) == 1
    [(req, _)] = no_network
    sent = json.loads(req.data.decode())["instance_id"]
    assert sent == "11111111-2222-4333-8444-555555555555"
    assert len(sent) == 36


def test_all_poisoned_batch_is_deleted_without_posting(temp_home, no_network,
                                                       no_thread):
    """Round 4: an all-dropped batch must DELETE its lines and POST nothing —
    the server 400s an empty events array, and a POST-then-fail path would
    pin the poisoned head of the queue forever (organically reachable via a
    reset clock). Later good lines then flush normally."""
    path = temp_home / ".no_human" / "telemetry-queue.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"name":"app_started","ts":0,"props":{}}\n'
        '{"name":["app_started"],"ts":1786889836,"props":{}}\n'  # unhashable
        '{"name":"task_opened","ts":1786889836,"props":{}}\n')
    assert telemetry.flush(_ENABLED) == 0
    assert no_network == []                      # nothing POSTed
    assert not path.read_text().strip()          # poisoned head deleted
    # and the queue is not wedged: a good event now flushes fine
    telemetry.record("app_started", config={"telemetry": _ENABLED})
    assert telemetry.flush(_ENABLED) == 1
    assert len(no_network) == 1


# ------------------- published event-list disclosure ---------------------- #
# The browser channel (`web/src/telemetry.js`) sends one extra event kind,
# `screen_viewed`, that the server's closed allowlist deliberately never
# accepts (it would open the ingest path to it — see telemetry.py's module
# docstring). These two tests pin the DOCUMENTED list in docs/configuration.md
# against `_ALLOWED_EVENTS` in both directions, so a server event can't ship
# undocumented and a stale doc entry can't survive a removed event either.

_DOCS_PATH = Path(__file__).resolve().parent.parent / "docs" / "configuration.md"


def _configuration_doc_text() -> str:
    return _DOCS_PATH.read_text(encoding="utf-8")


def test_every_server_event_kind_is_documented():
    doc = _configuration_doc_text()
    missing_events = [name for name in telemetry._ALLOWED_EVENTS if name not in doc]
    assert missing_events == [], (
        f"event kind(s) not listed in docs/configuration.md: {missing_events}"
    )
    all_props = {prop for props in telemetry._ALLOWED_EVENTS.values() for prop in props}
    missing_props = [
        prop for prop in all_props
        if not re.search(rf"`{re.escape(prop)}`", doc)
    ]
    assert missing_props == [], (
        f"prop name(s) not listed (as `backticked`) in docs/configuration.md: {missing_props}"
    )


def test_documented_list_has_no_phantom_events():
    doc = _configuration_doc_text()
    documented = {
        m.group(1)
        for m in re.finditer(r"`([a-z_]+)`\s*\|\s*(?:server|browser)\s*\|", doc)
    }
    published = set(telemetry._ALLOWED_EVENTS) | {"screen_viewed"}
    assert documented == published, (
        "docs/configuration.md's event table disagrees with the actual "
        f"published set — documented={documented!r} published={published!r}"
    )


def test_telemetry_defaults_on_opt_out():
    # Operator flip 2026-08-27: usage insights are opt-OUT — enabled by default,
    # turned off from the onboarding step or Settings > Usage insights.
    assert DEFAULT_CONFIG["telemetry"]["enabled"] is True
