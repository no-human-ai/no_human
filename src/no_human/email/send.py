"""The welcome-email seam: template selection + transport.

DELIVERY IS NOT WIRED. As of 2026-09-12 the sending AWS SES account is still
in sandbox mode (``ProductionAccessEnabled: false``) and its most recent
production-access request was denied (``ReviewDetails.Status: DENIED``, case
178688493800757) — in sandbox, SES can only deliver to a handful of
individually verified addresses, not to an arbitrary registrant. Choosing and
wiring a real transport (SES, SMTP, a third-party API) is explicitly out of
scope for this change: no boto3/SMTP client, no new dependency, no AWS/DNS
change. What exists here is the seam a future change plugs a transport into.

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

import logging
import sys
from dataclasses import dataclass
from typing import Protocol

from . import base

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


def _default_transport() -> Transport:
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
