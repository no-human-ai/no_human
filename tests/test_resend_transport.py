"""Behavioral tests for the Resend transport behind email/send.py's seam.

Every test here stubs `urllib.request.urlopen` — none reaches the real
`api.resend.com`, live or otherwise. (The static proof that this module's
only possible egress is gated on `RESEND_API_KEY` lives in
tests/test_egress_allowlist.py.) `isolated_env_file` (tests/conftest.py)
keeps a test's `.env` reads off the operator's real `~/.no_human/.env`; a
`monkeypatch.delenv` of `RESEND_API_KEY` additionally covers the case where
the *process* environment (not the file) happens to carry a real key.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from no_human.email import send

# A stand-in recipient address. Every "never logged/returned" assertion below
# greps caplog/return values/exception text for this exact string.
PII_ADDRESS = "person.under.test@example.com"


def _http_error(status: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(send.RESEND_API_URL, status, "stub", {}, io.BytesIO(body))


class _OKResponse:
    def read(self, *_a):
        return b'{"id": "abc123"}'

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _msg(to: str = PII_ADDRESS) -> send.Message:
    return send.Message(to=to, subject="subj", body="body text")


def _raising_urlopen(exc: Exception):
    """A `urllib.request.urlopen` stand-in that always raises `exc`."""

    def _fake(request, timeout=None):
        raise exc

    return _fake


# ---------------------------------------------------------------------------
# Status -> (category, retryable) mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, body, expected_category, expected_retryable",
    [
        (401, b"", "auth_error", False),
        (403, b'{"message": "invalid api key"}', "auth_error", False),
        (403, b'{"message": "you have hit your plan quota limit"}', "quota_exceeded", True),
        (402, b"", "quota_exceeded", True),
        (422, b'{"message": "invalid `to` address"}', "recipient_rejected", False),
        (429, b"", "throttled", True),
        (500, b"", "transport_error", True),
        (503, b"", "transport_error", True),
    ],
)
def test_status_mapping(monkeypatch, status, body, expected_category, expected_retryable):
    monkeypatch.setattr(
        urllib.request, "urlopen", _raising_urlopen(_http_error(status, body))
    )
    transport = send.ResendTransport("fake-key")
    with pytest.raises(send.TransportError) as excinfo:
        transport.send(_msg())
    assert excinfo.value.category == expected_category
    assert excinfo.value.retryable is expected_retryable
    assert excinfo.value.category in send.FAILURE_CATEGORIES


def test_unanticipated_status_is_transport_error_never_sent(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", _raising_urlopen(_http_error(418, b"teapot"))
    )
    transport = send.ResendTransport("fake-key")
    with pytest.raises(send.TransportError) as excinfo:
        transport.send(_msg())
    assert excinfo.value.category == "transport_error"
    assert excinfo.value.retryable is True

    # Through the public seam too: a novel status must never read as "sent".
    status = send.send_welcome(PII_ADDRESS, transport=transport)
    assert status == "not_sent:transport_error"
    assert status != "sent"


def test_timeout_is_transport_error_retryable(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport = send.ResendTransport("fake-key")
    with pytest.raises(send.TransportError) as excinfo:
        transport.send(_msg())
    assert excinfo.value.category == "transport_error"
    assert excinfo.value.retryable is True


def test_urlerror_is_transport_error_retryable(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport = send.ResendTransport("fake-key")
    with pytest.raises(send.TransportError) as excinfo:
        transport.send(_msg())
    assert excinfo.value.category == "transport_error"
    assert excinfo.value.retryable is True


# ---------------------------------------------------------------------------
# The request itself: method, URL, timeout, body, auth header
# ---------------------------------------------------------------------------


def test_success_posts_expected_request_and_does_not_raise(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        return _OKResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport = send.ResendTransport("fake-key")
    transport.send(_msg())  # must not raise

    assert captured["timeout"] == send.RESEND_TIMEOUT_SECONDS
    req = captured["request"]
    assert req.full_url == send.RESEND_API_URL
    assert req.get_method() == "POST"
    payload = json.loads(req.data.decode("utf-8"))
    assert payload == {
        "from": send.RESEND_FROM,
        "to": [PII_ADDRESS],
        "subject": "subj",
        "text": "body text",
    }


def test_authorization_header_carries_bearer_key(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        return _OKResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    send.ResendTransport("sk_super_secret_key").send(_msg())
    assert captured["request"].get_header("Authorization") == "Bearer sk_super_secret_key"


# ---------------------------------------------------------------------------
# PII: never logged, never returned. Key: never leaks anywhere.
# ---------------------------------------------------------------------------


def test_send_welcome_never_logs_recipient_address(monkeypatch, caplog):
    monkeypatch.setattr(
        urllib.request, "urlopen", _raising_urlopen(_http_error(422, b'{"message": "bad"}'))
    )
    transport = send.ResendTransport("fake-key")
    with caplog.at_level(logging.DEBUG, logger="no_human.email"):
        status = send.send_welcome(PII_ADDRESS, transport=transport)
    assert status == "not_sent:recipient_rejected"
    for record in caplog.records:
        assert PII_ADDRESS not in record.getMessage()

    # Positive control: prove caplog/getMessage() WOULD have caught the
    # address had send_welcome regressed to logging it — otherwise the
    # all-clear above could just mean the assertion never engages.
    logging.getLogger("no_human.email").warning("canary %s", PII_ADDRESS)
    assert any(PII_ADDRESS in r.getMessage() for r in caplog.records)


def test_send_welcome_never_returns_recipient_address(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", _raising_urlopen(_http_error(422, b""))
    )
    transport = send.ResendTransport("fake-key")
    status = send.send_welcome(PII_ADDRESS, transport=transport)
    assert PII_ADDRESS not in status
    assert status == "not_sent:recipient_rejected"


def test_raised_transporterror_message_never_carries_the_address(monkeypatch):
    """`send_welcome` never surfaces `str(exc)` today (it uses only
    `exc.category`), but the exception message is exactly the kind of detail
    a provider response could tempt a future caller into logging directly —
    pin it here too, not just at the two paths `send_welcome` happens to
    use."""
    monkeypatch.setattr(
        urllib.request, "urlopen", _raising_urlopen(_http_error(422, b'{"message": "bad"}'))
    )
    transport = send.ResendTransport("fake-key")
    with pytest.raises(send.TransportError) as excinfo:
        transport.send(_msg())
    assert PII_ADDRESS not in str(excinfo.value)


def test_api_key_never_appears_in_exception_or_log(monkeypatch, caplog):
    secret_key = "sk_do_not_leak_this_0123456789"
    monkeypatch.setattr(
        urllib.request, "urlopen", _raising_urlopen(_http_error(401, b""))
    )
    transport = send.ResendTransport(secret_key)

    with caplog.at_level(logging.DEBUG, logger="no_human.email"):
        with pytest.raises(send.TransportError) as excinfo:
            transport.send(_msg())
        assert secret_key not in str(excinfo.value)

        status = send.send_welcome(PII_ADDRESS, transport=transport)
        assert secret_key not in status

    for record in caplog.records:
        assert secret_key not in record.getMessage()

    # Positive control, mirroring the PII one above.
    logging.getLogger("no_human.email").warning("canary %s", secret_key)
    assert any(secret_key in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The key gate: `_default_transport` and no-key byte-identical behavior
# ---------------------------------------------------------------------------


def test_default_transport_unavailable_without_key(isolated_env_file, monkeypatch):
    monkeypatch.delenv(send.RESEND_API_KEY_VAR, raising=False)
    assert isinstance(send._default_transport(), send.UnavailableTransport)


def test_default_transport_is_resend_with_key(isolated_env_file, monkeypatch):
    monkeypatch.delenv(send.RESEND_API_KEY_VAR, raising=False)
    isolated_env_file.write_text(f"{send.RESEND_API_KEY_VAR}=re_test_key_123\n")
    transport = send._default_transport()
    assert isinstance(transport, send.ResendTransport)
    assert transport._api_key == "re_test_key_123"


def test_no_key_send_welcome_is_byte_identical_to_pre_resend_behavior(
    isolated_env_file, monkeypatch
):
    """With no key configured, `send_welcome` must behave EXACTLY as it did
    before Resend existed: `"not_sent:unconfigured"`, no exception, no
    network attempt. This is the literal no-new-dependency-on-a-key
    contract — adding ResendTransport must not perturb the unconfigured
    path by even one byte."""
    monkeypatch.delenv(send.RESEND_API_KEY_VAR, raising=False)

    def _boom_if_called(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("urlopen must not be called with no key configured")

    monkeypatch.setattr(urllib.request, "urlopen", _boom_if_called)
    status = send.send_welcome(PII_ADDRESS)
    assert status == "not_sent:unconfigured"


# ---------------------------------------------------------------------------
# stdlib-only: no `requests`, no `resend` SDK, no other HTTP dependency
# ---------------------------------------------------------------------------


def test_send_py_imports_stdlib_http_only():
    source = Path(send.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"requests", "resend", "httpx", "aiohttp", "urllib3"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    assert not (found & forbidden), f"forbidden HTTP dependency imported: {found & forbidden}"
    assert "urllib" in found
