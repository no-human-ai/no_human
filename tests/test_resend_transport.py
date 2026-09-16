"""The Resend transport: status mapping, fail-closed, and no address anywhere.

The operator chose Resend on 2026-09-12; SES stays sandboxed with its
production-access request DENIED, so this is the path rather than a fallback.

WHAT A MOCKED TEST HERE CANNOT SEE, recorded because it already bit once. The
first live call from this code was refused by the Cloudflare edge in front of
Resend with `HTTP 403 "error code: 1010"` — before the request reached the API —
purely because urllib sends `Python-urllib/3.x` as its User-Agent. An A/B with
an explicit header got a real API answer. Every test below stubs the opener, so
none of them would have caught that; `test_the_request_carries_an_explicit_user_agent`
pins the header, and only a live send proves the rest.

SAFETY: no test in this file may construct `send.ResendTransport` without
`opener=`, and no test may call `send._default_transport()` (or anything that
reaches it) without first `monkeypatch.delenv(send.RESEND_KEY_VAR,
raising=False)` AND redirecting `no_human.config.ENV_PATH` at a path that does
not exist. Skip either half on a machine that actually has `RESEND_API_KEY` in
`~/.no_human/.env` and this suite mails a real stranger.
"""

from __future__ import annotations

import ast
import io
import logging
import urllib.error

import pytest

from no_human.email import send


def _opener(*, status=None, http_status=None, exc=None, seen=None):
    """A stand-in for `urllib.request.urlopen` — no test reaches the network."""
    def opener(req, timeout=None):
        if seen is not None:
            seen.append(req)
        if exc is not None:
            raise exc
        if http_status is not None:
            raise urllib.error.HTTPError(
                req.full_url, http_status, "err", {}, io.BytesIO(b"{}"))

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def getcode(self): return status
        r = _Resp()
        r.status = status
        return r
    return opener


MSG = send.Message(to="probe@example.com", subject="subj", body="body")


@pytest.mark.parametrize("status", [200, 201, 202])
def test_a_status_the_provider_accepts_is_a_send(status):
    seen = []
    result = send.ResendTransport(
        "k", opener=_opener(status=status, seen=seen)).send(MSG)
    assert result is None
    assert len(seen) == 1


@pytest.mark.parametrize("status", [100, 199, 999])
def test_a_raw_status_outside_2xx_is_never_a_send(status):
    """A real `urlopen` raises `HTTPError` for 4xx/5xx and follows 3xx
    redirects itself, so the explicit `200 <= status < 300` check in
    `ResendTransport.send` is defense in depth for whatever an opener hands
    back directly (the informational 1xx range, or anything malformed). That
    check must fail closed for a value like this, never read it as a send —
    this is what actually kills a mutant that widens or drops the range
    check, since every other status test here goes through the `HTTPError`
    branch instead and never touches this line."""
    with pytest.raises(send.TransportError) as ei:
        send.ResendTransport("k", opener=_opener(status=status)).send(MSG)
    assert ei.value.category == "transport_error"
    assert ei.value.retryable is True
    assert ei.value.category in send.FAILURE_CATEGORIES


@pytest.mark.parametrize("http_status,category,retryable", [
    (401, "auth_error", False),
    (403, "auth_error", False),
    (402, "quota_exceeded", True),
    (422, "recipient_rejected", False),
    (429, "throttled", True),
    (500, "transport_error", True),
    (503, "transport_error", True),
    # The one that matters: a status nobody anticipated must NOT read as sent.
    (418, "transport_error", True),
])
def test_each_status_maps_to_its_category_and_never_to_success(
    http_status, category, retryable
):
    with pytest.raises(send.TransportError) as ei:
        send.ResendTransport("k", opener=_opener(http_status=http_status)).send(MSG)
    assert ei.value.category == category
    assert ei.value.retryable is retryable
    assert ei.value.category in send.FAILURE_CATEGORIES


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("no route to host"),
    TimeoutError("timed out"),
    OSError("connection reset"),
])
def test_an_unreachable_provider_fails_closed_and_is_retryable(exc):
    """Fail CLOSED: if we cannot establish that the provider accepted the
    message, it was not sent."""
    with pytest.raises(send.TransportError) as ei:
        send.ResendTransport("k", opener=_opener(exc=exc)).send(MSG)
    assert ei.value.category == "transport_error"
    assert ei.value.retryable is True


@pytest.mark.parametrize("kw", [
    {"http_status": 422}, {"http_status": 500},
    {"exc": urllib.error.URLError("boom")},
])
def test_the_recipient_never_appears_in_a_raised_message(kw):
    """A provider error body can echo the address; the body is read for a
    CATEGORY only and then discarded."""
    with pytest.raises(send.TransportError) as ei:
        send.ResendTransport("k", opener=_opener(**kw)).send(MSG)
    assert MSG.to not in str(ei.value)
    assert MSG.to not in repr(ei.value)


def test_the_recipient_never_appears_in_a_log_line(caplog):
    """`send_welcome` (not the transport) is what logs; it must log a
    category, never the address — even on a raising transport."""
    caplog.set_level(logging.WARNING, logger="no_human.email")

    class _Raising:
        def send(self, msg):
            raise send.TransportError(
                "boom", category="recipient_rejected", retryable=False)

    status = send.send_welcome("secret-person@example.com", transport=_Raising())
    assert status == "not_sent:recipient_rejected"
    for record in caplog.records:
        assert "secret-person@example.com" not in record.getMessage()
        assert "secret-person@example.com" not in record.message


def test_the_request_carries_an_explicit_user_agent():
    """Measured: without it the Cloudflare edge in front of Resend returns
    `403 error code: 1010` and the API is never reached."""
    seen = []
    send.ResendTransport("k", opener=_opener(status=200, seen=seen)).send(MSG)
    assert len(seen) == 1
    ua = seen[0].get_header("User-agent")
    assert ua and "urllib" not in ua.lower(), ua


def test_the_request_is_the_shape_resend_parses():
    import json
    seen = []
    send.ResendTransport("k", opener=_opener(status=200, seen=seen)).send(MSG)
    req = seen[0]
    assert req.full_url == send.RESEND_ENDPOINT
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer k"
    body = json.loads(req.data)
    assert body["to"] == [MSG.to]
    assert body["subject"] == MSG.subject
    assert body["text"] == MSG.body
    assert body["reply_to"] == send.RESEND_REPLY_TO
    # The sender must sit on the SENDING subdomain: the root runs Cloudflare
    # Email Routing and a sender there would break inbound support@ mail.
    assert "send.getnohuman.com" in body["from"]


def test_the_transport_uses_only_the_standard_library():
    """Stdlib-only, statically: no `resend`/`requests` import anywhere in the
    module — the lean-stack rule forbids an SDK for one POST."""
    import inspect
    tree = ast.parse(inspect.getsource(send))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    banned = {"resend", "requests", "httpx", "aiohttp"}
    assert not (modules & banned), modules & banned


def test_without_a_key_the_default_transport_is_unchanged(monkeypatch, tmp_path):
    """An install with no key behaves byte-identically to before this existed."""
    monkeypatch.delenv(send.RESEND_KEY_VAR, raising=False)
    monkeypatch.setattr("no_human.config.ENV_PATH", tmp_path / "absent.env")
    assert isinstance(send._default_transport(), send.UnavailableTransport)


def test_with_a_key_the_default_transport_is_resend(monkeypatch, tmp_path):
    """The positive control for the test above — without it, a
    `_default_transport` hard-wired to Unavailable would satisfy it."""
    monkeypatch.setattr("no_human.config.ENV_PATH", tmp_path / "absent.env")
    monkeypatch.setenv(send.RESEND_KEY_VAR, "re_probe")
    assert isinstance(send._default_transport(), send.ResendTransport)


def test_an_unreadable_env_file_is_no_key_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.delenv(send.RESEND_KEY_VAR, raising=False)
    bad = tmp_path / "unreadable.env"
    bad.write_text("x", encoding="utf-8")
    bad.chmod(0o000)
    monkeypatch.setattr("no_human.config.ENV_PATH", bad)
    try:
        assert send._resend_api_key() is None
    finally:
        bad.chmod(0o600)


def test_the_key_is_read_from_the_env_file_too(monkeypatch, tmp_path):
    """`.env` is read directly (not only os.environ) — the primary place the
    key is documented to live."""
    monkeypatch.delenv(send.RESEND_KEY_VAR, raising=False)
    env_path = tmp_path / "present.env"
    env_path.write_text(f"{send.RESEND_KEY_VAR}=re_from_file\n", encoding="utf-8")
    env_path.chmod(0o600)
    assert send._resend_api_key(env_path) == "re_from_file"
