"""The welcome-email seam: template selection + transport.

The operator chose Resend as the welcome email's transport (2026-09-12): AWS
SES remains sandboxed (``ProductionAccessEnabled: false``) and its most
recent production-access request was denied (``ReviewDetails.Status:
DENIED``, case 178688493800757) — in sandbox, SES could only ever deliver to
a handful of individually verified addresses, not to an arbitrary
registrant. SES is not used anywhere in this module.

``_default_transport`` picks ``ResendTransport`` only when ``RESEND_API_KEY``
is set in ``~/.no_human/.env`` (read via ``config.read_env_var_value`` — the
same mechanism the OAuth token uses; the key is never written to
config.yaml, never logged, and never included in an exception message).
With no key configured it falls back to ``UnavailableTransport``, exactly as
before this change: registering an address still always succeeds, and
nothing leaves the machine. Nothing in this module claims that a given send
actually reached an inbox — Resend's HTTP response is mapped onto the closed
``FAILURE_CATEGORIES`` vocabulary below and no more.

``render_welcome`` picks one of the four frozen templates in ``base.py`` by
platform and returns their output UNCHANGED — this module never edits the
subject or body text.

``send_welcome`` renders, then hands the message to a ``Transport``. It never
raises (a transport problem must not break onboarding) and never logs or
returns the recipient address — only a closed-vocabulary status string.

Swapping the transport is a ONE-MODULE change: implement ``Transport.send``
for the real provider and pass it as ``send_welcome(addr, transport=...)`` (or
change the default constructed in ``_default_transport``). Nothing outside
this file needs to change.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .. import config
from . import base

log = logging.getLogger("no_human.email")

DOWNLOAD_URL = "https://getnohuman.com/download"
UNSUBSCRIBE_URL = "https://getnohuman.com/unsubscribe"

# Resend (https://resend.com) — the operator-chosen transport (2026-09-12).
RESEND_API_KEY_VAR = "RESEND_API_KEY"
RESEND_API_URL = "https://api.resend.com/emails"
# ASSUMPTION (intake Q&A, reversible): sender address for the welcome email.
RESEND_FROM = "welcome@getnohuman.com"
RESEND_TIMEOUT_SECONDS = 10.0

# Closed vocabulary of transport failure categories. Every one of these is a
# THING THAT COULD BE TRUE ABOUT A TRANSPORT, never a fragment of the address
# or any other free text — send_welcome()'s return value is always one of
# "sent" or "not_sent:<category>", so nothing address-shaped ever leaves this
# module by way of a status string.
FAILURE_CATEGORIES = frozenset(
    {
        "unconfigured",  # no transport wired — the shipped default with no key
        "recipient_rejected",  # provider rejected the address (e.g. Resend 422)
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
    """The transport used when no provider is configured. Always fails.

    This is the whole delivery story when ``RESEND_API_KEY`` is not set:
    shipping a transport that claims to work without a key would be a lie.
    `send_welcome` treats this failure as ordinary and non-retryable, logs no
    address, and still returns 200 to the caller — registering an address
    always succeeds; delivering to it does not, without a key configured.
    """

    def send(self, msg: Message) -> None:
        raise TransportUnavailable()


def _resend_failure(status: int, body: str) -> tuple[str, bool]:
    """Map a Resend HTTP error response onto (category, retryable).

    `category` is always a member of FAILURE_CATEGORIES — this is the one
    place a provider-specific status is translated into that closed
    vocabulary, and it fails closed: any status this function doesn't
    recognize maps to "transport_error" (retryable), never to "sent" and
    never to a made-up category.
    """
    if status == 401:
        return "auth_error", False
    if status == 403:
        # Resend uses 403 for both an invalid/restricted API key and for
        # quota/plan-shaped rejections (e.g. a free-tier domain-verification
        # limit). Disambiguate on the response body's own words; when the
        # shape is unrecognized, fail closed to the non-retryable reading —
        # an unrecognized 403 is treated as an auth problem, not silently
        # retried forever.
        lowered = body.lower()
        if any(word in lowered for word in ("quota", "limit", "plan")):
            return "quota_exceeded", True
        return "auth_error", False
    if status == 402:
        return "quota_exceeded", True
    if status == 422:
        return "recipient_rejected", False
    if status == 429:
        return "throttled", True
    if 500 <= status < 600:
        return "transport_error", True
    # Unanticipated status: never "sent", always the retryable transport
    # bucket so a novel provider response can't be mistaken for success.
    return "transport_error", True


class ResendTransport:
    """Sends the welcome email through Resend (https://api.resend.com).

    stdlib `urllib.request` only — no `requests`, no `resend` SDK, no new
    dependency for one POST. The API key is supplied by the caller
    (`_default_transport` reads it via `config.read_env_var_value`); this
    class never reads `~/.no_human/.env` itself, never logs the key or the
    recipient address, and never puts either into an exception message —
    only a FAILURE_CATEGORIES member ever leaves `send`.
    """

    def __init__(self, api_key: str, *, timeout: float = RESEND_TIMEOUT_SECONDS) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def send(self, msg: Message) -> None:
        payload = json.dumps(
            {
                "from": RESEND_FROM,
                "to": [msg.to],
                "subject": msg.subject,
                "text": msg.body,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            RESEND_API_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — best-effort only, never fatal here
                body = ""
            category, retryable = _resend_failure(exc.code, body)
            raise TransportError(
                f"Resend responded with HTTP {exc.code}",
                category=category,
                retryable=retryable,
            ) from None
        except TimeoutError:
            raise TransportError(
                "Resend request timed out", category="transport_error", retryable=True
            ) from None
        except urllib.error.URLError:
            raise TransportError(
                "Resend request failed", category="transport_error", retryable=True
            ) from None


def _default_transport() -> Transport:
    api_key = config.read_env_var_value(RESEND_API_KEY_VAR)
    if api_key:
        return ResendTransport(api_key)
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
    """Render one of the frozen templates in `base.py`, verbatim.

    This function only ever passes `base.*`'s own return values through — it
    never edits a subject or body character.
    """
    template = _platform_template(platform)
    if template == "linux_download":
        subject, body = base.linux_download(download_url, unsubscribe_url, address)
    elif template == "windows_waitlist":
        subject, body = base.windows_waitlist(unsubscribe_url, address)
    else:
        subject, body = base.mac_download(download_url, unsubscribe_url, address)
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
