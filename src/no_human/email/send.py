"""The welcome-email seam: template selection + transport.

DELIVERY IS WIRED, through Resend (the operator's choice, 2026-09-12). AWS SES
is NOT used: it is still sandboxed and its production-access request was denied
(case 178688493800757), so in sandbox it can only reach a handful of
individually verified addresses rather than an arbitrary registrant.

What is configured, and what an unconfigured install does. `ResendTransport`
is selected by `_default_transport` ONLY when `RESEND_API_KEY` is present in
``~/.no_human/.env`` (chmod 600, gitignored — never config.yaml, the same rule
`_reject_api_key_in_config` enforces for the Anthropic key). Without it the
default is still `UnavailableTransport`: registration succeeds, `send_welcome`
returns ``"not_sent:unconfigured"``, and nothing touches the network. The
egress is declared in ``tests/test_egress_allowlist.py`` under
``email/send.py``, gated ``env:RESEND_API_KEY``.

Mail leaves from ``send.getnohuman.com``, never the root: getnohuman.com runs
Cloudflare Email Routing (MX route1/2/3.mx.cloudflare.net), and a sender on the
root would break inbound mail to support@getnohuman.com.

``render_welcome`` picks one of the four frozen templates in ``base.py`` by
platform and returns their output UNCHANGED — this module never edits the
subject or body text.

``send_welcome`` renders, then hands the message to a ``Transport``. It never
raises (a transport problem must not break onboarding) and never logs or
returns the recipient address — only a closed-vocabulary status string.

Swapping the transport is still a ONE-MODULE change: implement ``Transport.send``
and return it from ``_default_transport``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from . import base, in_app

log = logging.getLogger("no_human.email")

DOWNLOAD_URL = "https://getnohuman.com/download"
UNSUBSCRIBE_URL = "https://getnohuman.com/unsubscribe"

# Closed vocabulary of transport failure categories. Every one of these is a
# THING THAT COULD BE TRUE ABOUT A TRANSPORT, never a fragment of the address
# or any other free text — send_welcome()'s return value is always one of
# "sent" or "not_sent:<category>", so nothing address-shaped ever leaves this
# module by way of a status string.
FAILURE_CATEGORIES = frozenset(
    {
        "unconfigured",  # no transport wired — today's shipped default
        "recipient_rejected",  # SES sandbox: true for every stranger today
        "throttled",  # retryable
        "quota_exceeded",  # retryable
        "account_disabled",  # not retryable
        "auth_error",  # not retryable
        "transport_error",  # unexpected transport exception, retryable
    }
)

RETRYABLE_CATEGORIES = frozenset({"throttled", "quota_exceeded", "transport_error"})


@dataclass(frozen=True)
class Message:
    """A fully rendered email, ready for a transport. No PII beyond `to`."""

    to: str
    subject: str
    body: str


class TransportError(Exception):
    """Raised by a `Transport.send` implementation on a failed delivery.

    `category` must be one of FAILURE_CATEGORIES — that closed set is what
    `send_welcome` reports, never the exception's own message (which a real
    transport might fill with provider-specific detail, e.g. an SES response
    that itself echoes the recipient address).
    """

    def __init__(self, message: str, *, category: str, retryable: bool) -> None:
        super().__init__(message)
        if category not in FAILURE_CATEGORIES:
            raise ValueError(f"unknown transport failure category: {category!r}")
        self.category = category
        self.retryable = retryable


class TransportUnavailable(TransportError):
    """The shipped default: no real transport is configured."""

    def __init__(self) -> None:
        super().__init__(
            "no email transport is configured", category="unconfigured", retryable=False
        )


class Transport(Protocol):
    def send(self, msg: Message) -> None: ...  # pragma: no cover - protocol


class UnavailableTransport:
    """The default transport. Always fails, never sends anything anywhere.

    This is the ENTIRE delivery story today: SES is sandboxed and denied
    production access (see module docstring), so shipping a transport that
    claims to work would be a lie. `send_welcome` treats this failure as
    ordinary and non-retryable, logs no address, and still returns 200 to the
    caller — registering an address always succeeds; delivering to it does
    not, yet.
    """

    def send(self, msg: Message) -> None:
        raise TransportUnavailable()


#: The operator's chosen provider (2026-09-12). SES stays sandboxed and its
#: production-access request is DENIED, so Resend is the path, not a fallback.
RESEND_ENDPOINT = "https://api.resend.com/emails"
#: The SENDING subdomain, never the root: getnohuman.com already runs Cloudflare
#: Email Routing (MX route1/2/3.mx.cloudflare.net), and putting the sender on the
#: root would break inbound mail to support@getnohuman.com.
RESEND_SENDER = "no_human <hello@send.getnohuman.com>"
#: Read from ~/.no_human/.env (chmod 600, gitignored) exactly like every other
#: credential here. NEVER config.yaml — `_reject_api_key_in_config` is the rule
#: this follows — and never logged, never in an exception message.
RESEND_KEY_VAR = "RESEND_API_KEY"
#: Required. See the header comment in `ResendTransport.send`.
RESEND_USER_AGENT = "no_human (+https://getnohuman.com)"

#: HTTP status -> (category, retryable). Anything unlisted is a transport error:
#: if we cannot establish that the provider ACCEPTED the message, it was not
#: sent. Never map an unknown status to success.
_RESEND_STATUS: dict[int, tuple[str, bool]] = {
    401: ("auth_error", False),
    403: ("auth_error", False),
    402: ("quota_exceeded", True),
    422: ("recipient_rejected", False),
    429: ("throttled", True),
}


def _resend_api_key(env_path=None) -> str | None:
    """The key from the .env, falling back to the process environment.

    Returns None when absent, which is what keeps an unconfigured install on
    `UnavailableTransport` and behaving exactly as before.
    """
    from ..config import ENV_PATH, _read_env_file
    path = ENV_PATH if env_path is None else env_path
    try:
        from_file = _read_env_file(path).get(RESEND_KEY_VAR)
    except Exception:  # noqa: BLE001 - an unreadable .env is "no key", not a crash
        from_file = None
    return (from_file or os.environ.get(RESEND_KEY_VAR) or "").strip() or None


class ResendTransport:
    """Delivery through Resend's HTTP API, using only the standard library.

    No `resend` SDK and no `requests`: the lean-stack constraint forbids adding
    a dependency for this, and the whole call is one POST.

    Nothing here ever puts the recipient in a log line or in a raised message.
    Resend's error bodies can echo the address, so the body is read only to pick
    a CATEGORY out of the closed set above; the body itself is discarded.
    """

    def __init__(self, api_key: str, *, sender: str = RESEND_SENDER,
                 endpoint: str = RESEND_ENDPOINT, timeout: float = 10.0,
                 opener=None) -> None:
        self._key = api_key
        self._sender = sender
        self._endpoint = endpoint
        self._timeout = timeout
        # Injectable for tests so no test ever reaches the network.
        self._opener = opener or urllib.request.urlopen

    def send(self, msg: Message) -> None:
        payload = json.dumps({
            "from": self._sender,
            "to": [msg.to],
            "subject": msg.subject,
            "text": msg.body,
        }).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint, data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                # MEASURED, not decorative: urllib's default `Python-urllib/3.x`
                # is refused by the Cloudflare edge in front of Resend with
                # HTTP 403 "error code: 1010" before the request ever reaches
                # the API. Identical request with this header -> the API
                # answers. A mocked test cannot see this; only a live call can.
                "User-Agent": RESEND_USER_AGENT,
            },
        )
        try:
            with self._opener(req, timeout=self._timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
        except urllib.error.HTTPError as exc:
            category, retryable = _RESEND_STATUS.get(
                exc.code, ("transport_error", True))
            raise TransportError(
                f"resend rejected the send ({exc.code})",
                category=category, retryable=retryable) from None
        except Exception as exc:  # noqa: BLE001 - URLError, timeout, DNS, TLS
            raise TransportError(
                f"resend was unreachable ({type(exc).__name__})",
                category="transport_error", retryable=True) from None

        if not (200 <= int(status) < 300):
            # Reached the provider but it did not accept: fail closed.
            category, retryable = _RESEND_STATUS.get(
                int(status), ("transport_error", True))
            raise TransportError(
                f"resend returned {status}", category=category, retryable=retryable)


def _default_transport() -> Transport:
    """`ResendTransport` when a key is configured, else the unavailable one.

    An install with no key behaves byte-identically to before this existed:
    registration still succeeds, `send_welcome` still returns
    `"not_sent:unconfigured"`, and nothing reaches the network.
    """
    key = _resend_api_key()
    if key:
        return ResendTransport(key)
    return UnavailableTransport()


def _platform_template(platform: str | None) -> str:
    """Map a platform identifier to one of the four template names.

    Defaults to macOS when `platform` is None or unrecognized (intake Q&A).
    """
    p = (platform or sys.platform or "").lower()
    if p.startswith("linux"):
        return "linux_download"
    if p.startswith("win"):
        return "windows_waitlist"
    return "mac_download"


def render_welcome(
    address: str,
    *,
    platform: str | None = None,
    download_url: str = DOWNLOAD_URL,
    unsubscribe_url: str = UNSUBSCRIBE_URL,
) -> Message:
    """Render the in-app welcome, verbatim from `in_app.py`.

    `platform` is accepted and ignored, and `download_url` with it. Both used
    to select one of `base.py`'s four WEBSITE templates — a download link and
    "drag no_human to Applications" — which is wrong for this reader: this
    only ever runs inside the already-installed app, so the recipient has
    downloaded it, installed it and is looking at it. Kept in the signature so
    the call site and its tests do not have to change in the same commit as
    the copy; a follow-up can drop them.

    This function only ever passes a template's own return value through — it
    never edits a subject or body character.
    """
    subject, body = in_app.in_app_welcome(unsubscribe_url, address)
    return Message(to=address, subject=subject, body=body)


def send_welcome(
    address: str,
    *,
    transport: Transport | None = None,
    platform: str | None = None,
) -> str:
    """Render the welcome email and hand it to `transport`.

    Returns "sent" or "not_sent:<category>" — never raises, never logs the
    address, and never returns anything other than one of those two shapes.
    A caller (the API route) can persist this status but must not infer that
    "not_sent:..." means the registration itself failed: it did not.
    """
    msg = render_welcome(address, platform=platform)
    active_transport = transport if transport is not None else _default_transport()
    try:
        active_transport.send(msg)
    except TransportError as exc:
        log.warning("welcome email not sent: %s", exc.category)
        return f"not_sent:{exc.category}"
    except Exception as exc:  # noqa: BLE001 — a transport must never break onboarding
        log.warning("welcome email transport raised %s", type(exc).__name__)
        return "not_sent:transport_error"
    return "sent"
