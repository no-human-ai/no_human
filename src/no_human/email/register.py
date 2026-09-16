"""The onboarding-email REGISTRATION seam: forwarding a newly-registered
address to a hosted intake so the team can reach the person who typed it.

This is the CAPTURE half of "onboarding email must reach our servers" (a
separate task, 85524cef, owns the SEND half — actually delivering mail — and
this module must not grow into that one: no template rendering, no
`FAILURE_CATEGORIES`, no `Transport`/`UnavailableTransport` from
`no_human.email.send`. Those stay untouched).

``register_email`` never raises (a transport problem must not break
onboarding — the address is already persisted locally by the caller before
this runs) and never logs or returns the address — only a closed-vocabulary
status string, exactly like ``send_welcome``'s contract in ``send.py``.

The hosted endpoint URL and optional bearer token are OPERATOR CONFIGURATION
(env var or `config.yaml`'s `onboarding.registration_endpoint`), never a
value baked into this module: there is no production URL or credential to
hardcode here, and shipping none is the deliberate, non-blocking encoding of
that open question -- an unset endpoint makes zero network calls.

The resolved endpoint must be `https://` (loopback excepted for tests, the
same exception `brain/client.py`'s `_base()` grants its control-plane URL):
a misconfigured `http://` endpoint is refused rather than used, since sending
this request at all means putting the address on the wire.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from typing import Any, Protocol
from urllib.parse import urlsplit

log = logging.getLogger("no_human.email")

#: Closed vocabulary for this route's forward outcome -- never free text, and
#: never the address itself (an HTTPError's message can echo the submitted
#: body, so it must never be interpolated into a returned/logged string).
STATUSES = frozenset({"ok", "stored_locally_only", "not_configured"})

_HTTP_TIMEOUT = 10.0

_URL_ENV = "NH_ONBOARDING_REGISTER_URL"
_TOKEN_ENV = "NH_ONBOARDING_REGISTER_TOKEN"

#: The only hosts the plaintext-HTTP exception covers, mirroring
#: brain/client.py's `_base()` guard exactly: compared against the PARSED
#: host, never a string prefix -- `"http://localhost.evil.com"
#: .startswith("http://localhost")` is True, so the prefix form would let a
#: remote host reach this over plaintext HTTP.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_https_or_loopback(url: str) -> bool:
    """Refuse to ship the address over plaintext HTTP to a non-loopback host.

    Same invariant `brain/client.py`'s `_base()` enforces on the control-plane
    URL: an operator-configured `onboarding.registration_endpoint` (or
    `NH_ONBOARDING_REGISTER_URL`) that resolves to `http://` would forward the
    email in cleartext, so it is rejected here before any transport is
    invoked -- not just for the shipped `UrlOpenTransport`, but for the
    explicit `endpoint=` override too, since both carry the same PII.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and host in _LOOPBACK_HOSTS


class RegisterTransport(Protocol):
    def post(self, url: str, payload: dict[str, str]) -> None: ...


class UrlOpenTransport:
    """The shipped default: stdlib `urllib`, same convention as
    `telemetry.py`'s flush -- no new runtime dependency. Any auth header is
    bound at construction time (resolved once by `register_email`), so
    `.post` keeps the plain `RegisterTransport` shape."""

    def __init__(self, headers: dict[str, str] | None = None):
        self._headers = dict(headers or {})

    def post(self, url: str, payload: dict[str, str]) -> None:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **self._headers},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT):
            pass


def _platform(platform: str | None = None) -> str:
    """Normalize a platform identifier to one of "darwin"/"linux"/"windows"/
    "unknown". Derived from local OS metadata at runtime (`sys.platform` when
    `platform` is not given) -- never from a User-Agent header (unreliable
    for desktop apps) and never from the request body (redundant: the app
    already knows its own platform). Mirrors the prefix-matching convention
    in `send._platform_template`, but that function returns TEMPLATE names,
    not platform identifiers, so it is not reused here."""
    p = (platform or sys.platform or "").lower()
    if p.startswith("darwin"):
        return "darwin"
    if p.startswith("linux"):
        return "linux"
    if p.startswith("win"):
        return "windows"
    return "unknown"


def _endpoint(config: Any = None) -> tuple[str, dict[str, str]]:
    """Resolve the hosted endpoint URL and any auth header, in precedence
    order: env `NH_ONBOARDING_REGISTER_URL`, then
    `config.data["onboarding"]["registration_endpoint"]`. No hardcoded
    production URL and no embedded credential ship here -- an unset URL
    returns "" so the caller makes zero network calls."""
    url = os.environ.get(_URL_ENV, "").strip()
    if not url and config is not None:
        onboarding = (getattr(config, "data", None) or {}).get("onboarding") or {}
        url = (onboarding.get("registration_endpoint") or "").strip()
    headers: dict[str, str] = {}
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return url, headers


def register_email(
    address: str,
    *,
    transport: RegisterTransport | None = None,
    platform: str | None = None,
    endpoint: str | None = None,
    config: Any = None,
) -> str:
    """Forward `address` to the hosted registration intake. Reuses the
    existing waitlist intake's payload shape (`{email, plan, source}`, which
    already handles `desktop-*` plans) with `source="onboarding"`.

    Fail-open: any transport problem -- timeout, DNS/TLS failure, 4xx/5xx,
    or any other unexpected exception -- collapses to "stored_locally_only".
    Only the exception's TYPE NAME is logged, never its message or the
    address (an `HTTPError`'s message can echo the submitted payload back).
    """
    if endpoint is not None:
        url, headers = endpoint, {}
    else:
        url, headers = _endpoint(config)
    if not url:
        return "not_configured"
    if not _is_https_or_loopback(url):
        log.warning(
            "onboarding registration endpoint must be https:// (loopback "
            "excepted for tests); refusing to forward over plaintext HTTP"
        )
        return "not_configured"

    plat = _platform(platform)
    payload = {"email": address, "plan": f"desktop-{plat}", "source": "onboarding"}
    active_transport = transport or UrlOpenTransport(headers)
    try:
        active_transport.post(url, payload)
        return "ok"
    except Exception as exc:  # noqa: BLE001 -- fail-open by design
        log.warning("onboarding registration not forwarded: %s", type(exc).__name__)
        return "stored_locally_only"
