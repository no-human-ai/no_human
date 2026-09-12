"""The team-brain credential — a SEPARATE secret, and the one rule that makes it
structurally different from the Claude credential rather than merely differently
filed:

    IT IS NEVER PLACED IN ``os.environ``.

``config.py`` deliberately does the opposite for the Claude token: it exports
``CLAUDE_CODE_OAUTH_TOKEN`` into the process environment, because the Agent SDK
subprocess has to inherit it. That inheritance is the whole point there, and it
is precisely why a brain token must not travel the same road — generated code
running inside the coder's own sandbox can read the environment it was launched
with. A brain token in ``~/.no_human/.env`` would be one ``os.environ.get``
fallback (``config.py``'s loader pattern) away from exactly that.

So:

  file      ``~/.no_human/brain/credentials.json``   (0700 dir, 0600 file)
  loaded by this module, and nothing else
  environ   never
  scrubber  untouched. ``METERED_AUTH_VARS`` exists so a run bills exactly ONE
            inference path. A Cognito token is not a billing path; adding it to
            that list would teach the next reader that it is one.

HOW THE CREDENTIAL IS OBTAINED. Authorization code + PKCE against the hosted
sign-in UI, into a loopback listener on port 8422 — EXACTLY 8422, because the
redirect URI is matched literally by the identity provider and the deployed
callback is ``http://localhost:8422/callback``. The browser flow means ``nh``
never sees the user's password. A username/password flow exists on the server
side and is deliberately not used here.

WHERE THE ENDPOINTS COME FROM. Not from config, and not from a constant in this
package: the sign-in URL of the deployed stack contains a region and an account
id, and putting either in the local product would break the no-AWS-surface
invariant this whole client is built to keep. The client asks the control plane
— the one URL it knows — for its own OIDC endpoints, the way an OIDC client
reads a discovery document. See ``client.auth_config``.

Stated plainly, because it is the one thing discovery costs: a control plane
that has been taken over could point the browser at an endpoint it controls and
phish a sign-in. It could not thereby forge a RULE — those are verified against
a key pinned in this build (``keys.py``) and never against anything the service
says. Trust in the endpoint is availability-shaped; trust in a rule is not.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ..config import NO_HUMAN_HOME, atomic_write_0600, ensure_private_dir

BRAIN_DIR = NO_HUMAN_HOME / "brain"
CREDENTIALS_PATH = BRAIN_DIR / "credentials.json"
QUARANTINE_DIR = BRAIN_DIR / "quarantine"

#: Fixed by the deployed redirect URI. Not configurable, because a different
#: port silently fails at the identity provider with an unhelpful error.
LOOPBACK_PORT = 8422
LOOPBACK_REDIRECT = f"http://localhost:{LOOPBACK_PORT}/callback"

#: Refresh a cached id token this many seconds before it actually expires, so a
#: sync that starts just under the wire does not fail mid-flight.
_EXPIRY_SKEW = 120


class BrainAuthError(RuntimeError):
    """No usable brain credential. Never carries token material in its text."""


@dataclass(frozen=True)
class Credential:
    refresh_token: str
    id_token: str
    id_token_expires_at: float
    team_id: str
    subject: str

    @property
    def id_token_valid(self) -> bool:
        return bool(self.id_token) and time.time() < self.id_token_expires_at - _EXPIRY_SKEW


# --- storage ----------------------------------------------------------------


def _paths() -> tuple[Path, Path]:
    """Resolved (dir, file). Read at call time, not import time, so a test that
    relocates ``$HOME`` gets the relocated path — and so importing this module
    creates nothing."""
    home = Path(os.environ.get("NO_HUMAN_HOME_OVERRIDE") or NO_HUMAN_HOME)
    return home / "brain", home / "brain" / "credentials.json"


def quarantine_dir() -> Path:
    return _paths()[0] / "quarantine"


def load() -> Credential | None:
    """The stored credential, or None. Never raises on a corrupt file."""
    _, path = _paths()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("refresh_token"):
        return None
    try:
        expires = float(raw.get("id_token_expires_at") or 0)
    except (TypeError, ValueError):
        expires = 0.0
    return Credential(
        refresh_token=str(raw["refresh_token"]),
        id_token=str(raw.get("id_token") or ""),
        id_token_expires_at=expires,
        team_id=str(raw.get("team_id") or ""),
        subject=str(raw.get("subject") or ""),
    )


def save(cred: Credential) -> Path:
    """Write the credential 0600 inside a 0700 directory.

    Reuses ``config.ensure_private_dir`` / ``atomic_write_0600`` as plain
    helpers. That is REUSE of hardening that already exists and is already
    tested — not a change to the Claude auth path, which this module does not
    import, call, or modify.
    """
    directory, path = _paths()
    ensure_private_dir(directory)
    atomic_write_0600(path, json.dumps({
        "refresh_token": cred.refresh_token,
        "id_token": cred.id_token,
        "id_token_expires_at": cred.id_token_expires_at,
        "team_id": cred.team_id,
        "subject": cred.subject,
    }, indent=2) + "\n")
    return path


def forget() -> bool:
    """Delete the stored credential. True if there was one."""
    _, path = _paths()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BrainAuthError(f"could not remove {path}: {exc}") from exc


# --- JWT claims (OUR OWN token, read locally, never a trust decision) --------


def id_token_claims(id_token: str) -> dict:
    """Decode the claims of an id token WITHOUT verifying its signature.

    Legitimate because of what the result is used for: the team id and subject
    of the credential this machine just obtained, for display and for pinning
    the routing check on manifests. It is never an authorisation decision — the
    control plane verifies the signature on every request, and a manifest is
    trusted only against a pinned key. Reading our own token is bookkeeping.
    """
    try:
        payload = id_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


# --- PKCE ---------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(auth: dict, state: str, challenge: str) -> str:
    """Build the browser URL from the DISCOVERED endpoints."""
    endpoint = str(auth.get("authorization_endpoint") or "")
    client_id = str(auth.get("client_id") or "")
    if not endpoint.startswith("https://") or not client_id:
        raise BrainAuthError(
            "the control plane did not return a usable sign-in endpoint"
        )
    scopes = auth.get("scopes") or ["openid", "email", "profile"]
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": LOOPBACK_REDIRECT,
        "scope": " ".join(str(s) for s in scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"{endpoint}?{query}"


def validate_callback(query: dict, expected_state: str) -> tuple[str, str]:
    """Decide one OAuth callback. Returns ``(code, error)``; exactly one is set.

    THE CSRF CHECK, and it is module-level and pure on purpose. It lived inside
    a handler class nested inside ``wait_for_code``, which binds port 8422 and
    serves one real browser redirect — so there was no way to reach it from a
    test that did not open a socket, and it had none. Replacing the whole
    condition with ``if False:`` left the entire suite green. A security
    decision that can only be exercised by the thing it is embedded in is a
    decision with no gate on it.

    ``state`` is the anti-forgery value this process generated and put in the
    authorize URL. A callback carrying a different one is somebody else's
    authorization code being fed to this listener, and accepting it would bind
    this machine's brain credential to an attacker's session. Compared with
    ``compare_digest`` because the comparison is against a value an attacker
    supplies and wants to match; an empty expected state never matches anything.
    """
    got_state = (query.get("state") or [""])[0]
    code = (query.get("code") or [""])[0]
    if not expected_state or not secrets.compare_digest(got_state, expected_state):
        return "", (query.get("error") or ["state mismatch"])[0]
    if not code:
        return "", (query.get("error") or ["no authorization code in callback"])[0]
    return code, ""


def wait_for_code(state: str, timeout: float = 300.0) -> str:
    """Serve ONE request on the loopback port and return the auth code.

    A plain blocking ``http.server`` in the foreground of the command the user
    typed: no thread, no daemon, no background listener. It binds, handles one
    callback, and closes — which is what keeps invariant L2 ("zero new
    processes") true of the login path as well as the sync path.
    """
    import http.server

    captured: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's interface
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code, error = validate_callback(query, state)
            if error:
                captured["error"] = error
                body = b"no_human: sign-in failed. Return to your terminal."
            else:
                captured["code"] = code
                body = b"no_human: signed in. You can close this tab."
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # noqa: D102 - silence the default stderr log
            return

    try:
        server = http.server.HTTPServer(("127.0.0.1", LOOPBACK_PORT), _Handler)
    except OSError as exc:
        raise BrainAuthError(
            f"port {LOOPBACK_PORT} is not available ({exc}). The sign-in "
            f"redirect is registered for exactly this port, so `nh brain login` "
            f"cannot use another one — free it and retry."
        ) from exc
    server.timeout = timeout
    try:
        server.handle_request()
    finally:
        server.server_close()

    if "code" not in captured:
        raise BrainAuthError(
            f"sign-in did not complete ({captured.get('error', 'no callback received')})"
        )
    return captured["code"]
