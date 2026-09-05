"""Integration health probes — boot-time + scheduled.

Every ENABLED integration (the first-class blocks — jira/linear/monday — and
the `ci.*`/webhook VIEWS — github/gitlab/jenkins/circleci/slack/teams) is
live-probed once at server boot and then on a schedule (default 6h, override
via ``integrations.health_interval``). This module owns ALL of that logic —
scheduling, the probe itself, detail normalization, credential scrubbing —
so `api/app.py` and `intake/jira_poll.py` only need a few lines of wiring.

There is deliberately no new persistence layer: `_RESULTS` is this module's
own cache, and the ONLY way it reaches a caller is through `overlay()`, which
fills in `healthy`/`detail`/`checked_at` on the very `IntegrationStatus`
objects `list_integrations`/`list_integrations_with_ambient` already produce.
Nothing here writes config.yaml (`mark_verified` stays an operator-initiated,
`/test`-route-only action) and nothing here can block server start or a poll
tick: every entry point catches broadly and logs, never raises.

Reuses the existing `_check_*` probes in `integrations/__init__.py` (via
`test_integration`) rather than writing new HTTP code — the ONLY thing added
here is a shorter, forced timeout (`_PROBE_TIMEOUT`, via a monkeypatch of the
`_http_get`/`_http_post` seams those functions already call through), a
layer of detail formatting (host + an actionable hint for Jira's classic
wrong-tenant 404), a second, independent credential scrub, and one more
rule `_check_*` itself does not apply: an ENABLED-but-UNCONFIGURED target
(nothing to probe yet) is reported `healthy=None` — neutral, not the red
"Failing" chip — while a CONFIGURED target that genuinely fails still
reports `healthy=False` (see `probe()`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

import no_human.integrations as _pkg

from ..config import AuthError, load_env_var

log = logging.getLogger("no_human.integrations.health")

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

DEFAULT_INTERVAL_SECONDS = 21600  # 6h — the scheduled sweep's normal cadence.
_FAIL_RETRY_SECONDS = 300         # short backoff for a currently-FAILING target.
_PROBE_TIMEOUT = 5.0              # shorter than _check_*'s interactive 10.0s —
                                   # these fire unattended, nobody is waiting.
_MIN_INTERVAL = 60                # floor for a misconfigured health_interval.

#: Secret env vars whose VALUE must never survive into a probe `detail`
#: string. Belt-and-braces: every `_check_*` already never echoes a token —
#: this is a second, independent guard specific to this unattended path.
_SECRET_ENV_VARS = (
    "JIRA_API_TOKEN", "LINEAR_API_KEY", "MONDAY_API_TOKEN", "CIRCLECI_TOKEN",
    "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "JENKINS_API_TOKEN",
    "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
)

#: The two webhook "secrets" that live in config.yaml rather than .env (see
#: FIELD_SPECS) — scrubbed the same way, keyed by (section, key).
_SECRET_CONFIG_PATHS = (
    ("notifications", "slack_webhook_url"),
    ("notifications", "teams_webhook_url"),
)

#: Fallback host for a target with no configured URL of its own.
_DEFAULT_HOST = {
    "github": "github.com", "gitlab": "gitlab.com",
    "circleci": "circleci.com", "jenkins": "build.example.com",
}

_HTTP_RE = re.compile(r"^HTTP (\d+)$")
_ERR_RE = re.compile(r"^(connection failed|not verified): (\w+)$")
_TIMEOUT_EXC_NAMES = ("TimeoutError", "ConnectTimeout", "ReadTimeout")
_JIRA_TENANT_HINT = (
    " — site or credentials do not match this tenant "
    "(check integrations.jira.site / JIRA_API_TOKEN)"
)

#: The single writer of the `healthy`/`detail`/`checked_at` overlay —
#: `overlay()` is the only reader. No parallel store, no config.yaml churn.
_RESULTS: dict[str, "HealthResult"] = {}

#: Serializes the `_http_get`/`_http_post` monkeypatch window (see
#: `_probe_with_timeout`) so two probes racing (e.g. the scheduled loop and
#: `ensure_fresh_before_poll` firing in the same tick) never restore each
#: other's original functions out from under a still-in-flight call.
_PATCH_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class HealthResult:
    name: str
    healthy: bool | None   # None = ambient/advisory-neutral OR unconfigured,
                           # never a "failure"; False = configured-and-broken
    detail: str            # never a secret — see `_scrub`
    checked_at: str        # UTC ISO-8601, matches `mark_verified`'s format


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def health_interval(config: dict) -> float:
    """`integrations.health_interval`, defaulted + clamped. A malformed value
    (bad config.yaml edit) falls back to the default rather than crashing the
    probe loop."""
    raw = ((config or {}).get("integrations") or {}).get(
        "health_interval", DEFAULT_INTERVAL_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_SECONDS
    return max(value, _MIN_INTERVAL)


def probe_targets(config: dict) -> list[str]:
    """Every integration to probe right now: switched ON (`enable_state` is
    True), or — for the `ci.*`/webhook views with no switch of their own
    (`enable_state` is None) — configured. An integration explicitly switched
    OFF (`enable_state` is False) is never probed."""
    statuses = {s.name: s for s in _pkg.list_integrations(config)}
    out = []
    for name in _pkg._ORDER:
        state = _pkg.enable_state(config, name)
        if state is False:
            continue
        if state is True or statuses[name].configured:
            out.append(name)
    return out


async def _load_secrets(name: str) -> None:
    """Load *name*'s secret(s) from ~/.no_human/.env into the process env —
    mirrors the `/test` route (app.py) so an unattended boot probe can
    authenticate exactly like an operator-initiated one. A metered-auth var
    or a missing .env is advisory: the probe simply reports "not set", same
    as `/test` already does; this never raises."""
    for spec in _pkg.FIELD_SPECS.get(name, []):
        if spec.env_var:
            try:
                load_env_var(spec.env_var)
            except AuthError:
                pass


def _host_for(name: str, config: dict) -> str:
    """Hostname only — via `urlsplit`, never the path/query/user-info/token
    that could ride along with a full URL."""
    integ = _pkg._sect(config, "integrations").get(name) or {}
    ci = _pkg._sect(config, "ci")
    notif = _pkg._sect(config, "notifications")
    raw = (
        integ.get("site")
        or (ci.get("hostname") if name in ("github", "gitlab") else None)
        or (ci.get("base_url") if name == "jenkins" else None)
        or (notif.get(f"{name}_webhook_url") if name in ("slack", "teams") else None)
        or _DEFAULT_HOST.get(name, "")
    )
    if not raw:
        return ""
    try:
        return urlsplit(raw if "//" in str(raw) else f"//{raw}").hostname or ""
    except ValueError:
        return ""


def _with_host(detail: str, name: str, config: dict) -> str:
    """Append the probed host to a bare status/error detail — the board must
    say WHICH tenant/host failed, not just that something did. A Jira
    401/403/404 additionally gets an actionable hint (the classic
    wrong-tenant regression: valid token, wrong `site`)."""
    host = _host_for(name, config)
    if not host:
        return detail
    m = _HTTP_RE.match(detail)
    if m:
        out = f"{detail} from {host}"
        if name == "jira" and m.group(1) in ("401", "403", "404"):
            out += _JIRA_TENANT_HINT
        return out
    m = _ERR_RE.match(detail)
    if m:
        kind, exc = m.group(1), m.group(2)
        if exc in _TIMEOUT_EXC_NAMES:
            return f"timeout after {_PROBE_TIMEOUT}s ({host})"
        return f"{kind}: {exc} ({host})"
    return detail


def _scrub(text: str, config: dict | None = None) -> str:
    """Replace any non-empty value of a known secret env var, or a configured
    webhook URL, with ``***``. Independent of (and in addition to) the
    `_check_*` functions, which already never echo a credential."""
    out = text
    for var in _SECRET_ENV_VARS:
        val = os.environ.get(var)
        if val:
            out = out.replace(val, "***")
    if config:
        for section, key in _SECRET_CONFIG_PATHS:
            val = _pkg._sect(config, section).get(key)
            if val:
                out = out.replace(str(val), "***")
    return out


async def _probe_with_timeout(name: str, config: dict) -> "_pkg.IntegrationStatus":
    """Run `test_integration` with `_PROBE_TIMEOUT` forced onto every
    `_http_get`/`_http_post` call it makes, REGARDLESS of the `timeout=10.0`
    those checkers hardcode — without touching a single `_check_*` function.
    Exploits the same "monkeypatch the module attribute" seam the existing
    test suite already uses (`tests/test_integrations_registry.py`): a
    `_check_*` function looks up `_http_get`/`_http_post` as a GLOBAL at call
    time, so reassigning the attribute on the live module object is visible
    to it immediately. Originals are restored in `finally` — even on an
    exception — so this can never leave the module patched."""
    orig_get, orig_post = _pkg._http_get, _pkg._http_post

    async def timed_get(url, headers=None, auth=None, timeout=10.0):
        return await asyncio.wait_for(
            orig_get(url, headers=headers, auth=auth, timeout=_PROBE_TIMEOUT),
            timeout=_PROBE_TIMEOUT)

    async def timed_post(url, headers=None, json=None, timeout=10.0):
        return await asyncio.wait_for(
            orig_post(url, headers=headers, json=json, timeout=_PROBE_TIMEOUT),
            timeout=_PROBE_TIMEOUT)

    async with _PATCH_LOCK:
        _pkg._http_get, _pkg._http_post = timed_get, timed_post
        try:
            return await _pkg.test_integration(name, config)
        finally:
            _pkg._http_get, _pkg._http_post = orig_get, orig_post


async def probe(name: str, config: dict) -> HealthResult:
    """Live health check for *name*, hardened for the unattended path: never
    raises (a probe failure must not block boot or a poll tick), forces the
    5s timeout above, and scrubs any credential out of the detail before it
    is stored or logged anywhere.

    Unconfigured ⇒ ``healthy=None`` (neutral — nothing to probe, never a
    "failure"); configured-and-broken ⇒ ``healthy=False`` (a real failure
    still lights the red badge)."""
    await _load_secrets(name)
    try:
        status = await _probe_with_timeout(name, config)
        healthy, detail = status.healthy, status.detail
        # An integration nobody has configured yet is NOT a failure. `_check_*`
        # returns healthy=False/"not configured" because the operator-initiated
        # /test route asks "did this connect?" — for the unattended board badge
        # the honest answer is "nothing to probe". Keyed on `configured`, not on
        # the detail string, so a *configured* target that genuinely fails (bad
        # credential, HTTP 4xx/5xx, timeout, retired connector URL) still lands
        # healthy=False and still lights the red chip.
        if healthy is False and not status.configured:
            healthy = None
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        healthy, detail = False, f"probe error: {type(exc).__name__}"
    detail = _scrub(_with_host(detail, name, config), config)
    return HealthResult(name=name, healthy=healthy, detail=detail, checked_at=_now_iso())


def overlay(statuses: list) -> list:
    """`list_integrations`/`list_integrations_with_ambient` plus the cached
    probe result for every name `_RESULTS` has one for. The single overlay
    point: `IntegrationStatus.healthy` stays computed-per-call everywhere
    else, this is the only place a LIVE probe result is folded in."""
    out = []
    for s in statuses:
        r = _RESULTS.get(s.name)
        if r is None:
            out.append(s)
        else:
            out.append(replace(s, healthy=r.healthy, detail=r.detail, checked_at=r.checked_at))
    return out


async def probe_all(config: dict) -> list[HealthResult]:
    """Boot-time / scheduled sweep: probe every current target sequentially
    (unattended outbound calls — one at a time is plenty, and it keeps the
    per-call timeout math simple) and refresh `_RESULTS`. A FAILING enabled
    integration logs a warning — the advisory surface the board's badge
    mirrors."""
    results = []
    for name in probe_targets(config):
        result = await probe(name, config)
        _RESULTS[name] = result
        results.append(result)
        if result.healthy is False:
            log.warning("integration %s is FAILING: %s", name, result.detail)
    return results


async def run_health_loop(config_provider, *, sleep=asyncio.sleep, iterations=None) -> None:
    """The scheduled probe loop: a boot pass immediately, then re-probe on
    `health_interval` — or the shorter `_FAIL_RETRY_SECONDS` backoff whenever
    the last pass left anything unhealthy. `config_provider` is a zero-arg
    callable (not a captured dict) so a config reload is picked up on the
    next pass without restarting the loop.

    `sleep`/`iterations` are the test seam: a fake `sleep` records delays
    instead of blocking, and `iterations` bounds an otherwise-infinite loop.
    `asyncio.CancelledError` propagates (a clean task cancel on shutdown);
    everything else is caught and logged so one bad pass never kills the
    loop."""
    count = 0
    while True:
        try:
            results = await probe_all(config_provider())
            delay = (_FAIL_RETRY_SECONDS if any(r.healthy is False for r in results)
                     else health_interval(config_provider()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("integration health probe pass failed: %s", exc)
            delay = _FAIL_RETRY_SECONDS
        count += 1
        if iterations is not None and count >= iterations:
            break
        await sleep(delay)


def start_health_probes(app) -> "asyncio.Task | None":
    """Fire the boot + scheduled probe loop as a background task. Never
    raises: a probe subsystem that fails to start must not take the server
    down with it — the same discipline `lifespan`'s `_worker_died` already
    applies to the scheduler's own worker loop."""
    try:
        task = asyncio.create_task(run_health_loop(lambda: app.state.config.data))
    except Exception as exc:  # noqa: BLE001
        log.warning("integration health probes did not start: %s", exc)
        return None

    def _died(t: "asyncio.Task") -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.warning("integration health probe loop stopped: %s", exc)

    task.add_done_callback(_died)
    return task


async def stop_health_probes(app) -> None:
    """Cancel the probe loop on shutdown. A no-op when it never started."""
    task = getattr(app.state, "health_probes", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


async def ensure_fresh_before_poll(name: str, config: dict) -> None:
    """Lazily re-probe *name* before a poll tick — but ONLY when the cached
    result is a FAILURE that is also STALE (older than `_FAIL_RETRY_SECONDS`).
    No cache at all, a healthy cache, or a fresh failure still inside its own
    backoff are all left alone, so this never doubles up with the scheduled
    loop's own re-probe. Never raises: a poll tick must never be blocked or
    broken by a probe failure."""
    try:
        cached = _RESULTS.get(name)
        if cached is None or cached.healthy is not False:
            return
        checked = datetime.strptime(
            cached.checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        if age < _FAIL_RETRY_SECONDS:
            return
        result = await probe(name, config)
        _RESULTS[name] = result
        if result.healthy is False:
            log.warning("integration %s is FAILING: %s", name, result.detail)
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_fresh_before_poll(%s) failed: %s", name, exc)


def with_health_probes(lifespan):
    """Wrap an ASGI `lifespan` context manager so boot + scheduled health
    probes start right after the wrapped lifespan's own startup has run
    (in particular after ``app.state.config`` is set, which
    `start_health_probes` reads) and stop during its shutdown. Lets
    `api/app.py` wire this module in with a single-line change to the
    `FastAPI(lifespan=...)` call, keeping ALL probe/scheduler logic here."""

    @asynccontextmanager
    async def wrapped(app):
        async with lifespan(app) as value:
            app.state.health_probes = start_health_probes(app)
            try:
                yield value
            finally:
                await stop_health_probes(app)

    return wrapped
