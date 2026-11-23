"""Deny a coding-backend subprocess the launcher's ambient secrets.

A coder child (``claude_backend.ClaudeBackend`` via the Agent SDK,
``codex_backend.CodexBackend`` via its own subprocess) inherits the launcher's
whole environment, and after `config.load_env_var` that environment carries
every integration credential the server has touched (JIRA/Linear/monday
tokens, SSO password, Jenkins token, ...) plus whatever the operator's shell
exported (GITHUB_TOKEN, cloud keys, an ssh-agent socket). A prompt injection in
the child would read them out of ``env``. This module removes them by NAME
shape, keeping only the credential that pays for the child itself and the
agent-session mark.

Two entry points, one per way a backend builds its child env:

* :func:`scrub_foreign_secrets_into` — for an ADDITIVE env mapping
  (``ClaudeAgentOptions.env``, merged over ``os.environ`` by the SDK). Every
  foreign secret in the source environment is OVERRIDDEN to ``""``; nothing is
  removed because the SDK would inherit it anyway.
* :func:`drop_foreign_secrets` — for a FULL env mapping (a copy of
  ``os.environ`` handed to ``subprocess``). Foreign secrets are deleted.

Non-secret operational variables (PATH, HOME, GIT_ASKPASS, ...) are never
touched. Git authenticates through the forge CLI's credential helper, not
these variables, so the runtime's own push (which runs in the PARENT process)
is unaffected; inside the child, ``gh``/``git`` fall back to the keyring or
``hosts.yml`` login, and an ssh-agent-only key is unusable — documented in
docs/security.md.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, MutableMapping

# Env-var NAME shapes that mark a value as a credential, matched
# case-insensitively as substrings.
_SECRET_NAME_MARKERS = (
    # `PASSPHRASE` sits beside `PASSWORD`/`PASSWD`: a GPG, SSH-key or backup
    # (borg/restic/age) passphrase is a credential a developer's shell commonly
    # exports, and none of the other markers catch `*_PASSPHRASE`.
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PASSPHRASE", "APIKEY", "API_KEY",
    "ACCESS_KEY", "PRIVATE_KEY", "CREDENTIAL", "AUTH_TOKEN", "SESSION_KEY",
    "SIGNING_KEY", "ENCRYPTION_KEY", "WEBHOOK",
)
# A `*_KEY` suffix is a credential far more often than not (STRIPE_KEY,
# COOKIE_KEY, SECRET_KEY); the false positive (a key ID such as GPG_KEY) is
# harmless to lose.
_SECRET_NAME_SUFFIXES = ("_KEY",)
# Credential-bearing names the markers do not catch by shape: pointers to
# credential files, and the connection-URL names whose VALUE is the credential
# (`scheme://user:password@host`). The named ones are caught by name so they
# go even when the value is a placeholder; the rest of that class is caught by
# value in `_value_embeds_credential`.
_SECRET_NAME_EXACT = (
    "SSH_AUTH_SOCK", "GOOGLE_APPLICATION_CREDENTIALS", "DATABASE_URL",
    "REDIS_URL", "MONGODB_URI", "SENTRY_DSN", "NETRC", "PGPASSFILE",
    "KUBECONFIG", "DOCKER_AUTH_CONFIG", "DOCKER_CONFIG",
)
# Cloud-provider namespaces: the whole prefix goes, because a profile name
# (AWS_PROFILE) IS the pointer to a credential file the child could then read.
_SECRET_NAME_PREFIXES = ("AWS_", "GCP_", "AZURE_")
# Names the rules above would catch that carry no credential and that a task's
# own test suite commonly needs: a region, a pager setting, a CA bundle path,
# a LocalStack endpoint, a tokenizer thread flag.
_NOT_SECRET_EXACT = frozenset({
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PAGER", "AWS_CA_BUNDLE",
    "TOKENIZERS_PARALLELISM",
})
# AWS_ENDPOINT_URL and its per-service spellings (AWS_ENDPOINT_URL_S3, ...).
_NOT_SECRET_PREFIXES = ("AWS_ENDPOINT_URL",)
# HTTP(S)_PROXY / ALL_PROXY / NO_PROXY: the child IS the process that reaches
# its model, and on a corporate network it does so through this URL — which
# carries a password. Kept even so, same category as the child's own billing
# credential; an EMPTY HTTPS_PROXY means "connect directly", which the network
# then blocks, so blanking it would cut every session off silently.
_NOT_SECRET_SUFFIXES = ("_PROXY",)

#: Secret-shaped name prefixes the CLAUDE child keeps with their real value.
#: ANTHROPIC_*/CLAUDE_* are the model auth + config for the very CLI being
#: launched, already reduced to the one sanctioned billing path by
#: `config.scrub_metered_auth` at startup; NO_HUMAN_AGENT_SESSION* is the mark.
CLAUDE_CHILD_KEEP: tuple[str, ...] = ("ANTHROPIC_", "CLAUDE_", "NO_HUMAN_AGENT_SESSION")
#: The CODEX child keeps its own billing credential (subscription mode has
#: already popped every OpenAI key spelling before this runs) and the mark. No
#: Anthropic name is kept: `CodexBackend._child_env` pops those explicitly.
CODEX_CHILD_KEEP: tuple[str, ...] = ("OPENAI_", "NO_HUMAN_AGENT_SESSION")


def _is_operational(upper: str) -> bool:
    """A name the rules would catch that carries no credential the child may
    lose, or a credential the child cannot function without (a proxy)."""
    return (upper in _NOT_SECRET_EXACT or upper.startswith(_NOT_SECRET_PREFIXES)
            or upper.endswith(_NOT_SECRET_SUFFIXES))


def is_secret_env_name(name: str) -> bool:
    """Whether an env-var NAME looks like it carries a credential."""
    upper = name.upper()
    if _is_operational(upper):
        return False
    if any(marker in upper for marker in _SECRET_NAME_MARKERS):
        return True
    if upper in _SECRET_NAME_EXACT or upper.endswith(_SECRET_NAME_SUFFIXES):
        return True
    return any(upper.startswith(prefix) for prefix in _SECRET_NAME_PREFIXES)


_URL_WITH_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/?#@]*:[^/?#@]+@", re.I)


def _value_embeds_credential(value: str) -> bool:
    """A connection URL whose userinfo carries a password
    (`postgres://app:s3cret@db/x`), under whatever name it was exported —
    POSTGRES_URL, DATABASE_URI, CELERY_BROKER_URL, AMQP_URL, ..."""
    return bool(value) and "://" in value and _URL_WITH_PASSWORD.match(value.strip()) is not None


def is_foreign_secret(name: str, keep: Iterable[str], value: str = "") -> bool:
    """Secret — by name shape, or by a value that embeds a password — AND not
    on the child's keep-list (prefixes, matched case-insensitively like the
    shape test, so `anthropic_api_key` and `ANTHROPIC_API_KEY` are judged the
    same)."""
    upper = name.upper()
    if _is_operational(upper):
        return False
    if not (is_secret_env_name(name) or _value_embeds_credential(value)):
        return False
    return not any(upper.startswith(prefix.upper()) for prefix in keep)


def scrub_foreign_secrets_into(
    env: MutableMapping[str, str],
    source_env: Mapping[str, str] | None = None,
    *,
    keep: Iterable[str] = CLAUDE_CHILD_KEEP,
) -> list[str]:
    """Override every foreign secret in ``source_env`` (the process env by
    default) to ``""`` inside the ADDITIVE mapping ``env``.

    Keys already present in ``env`` are deliberate additions and are left
    as-is. Returns the names blanked (for tests/diagnostics)."""
    source = os.environ if source_env is None else source_env
    keep = tuple(keep)
    blanked: list[str] = []
    for name, value in source.items():
        if name in env or not is_foreign_secret(name, keep, value):
            continue
        env[name] = ""
        blanked.append(name)
    return blanked


def drop_foreign_secrets(
    env: MutableMapping[str, str],
    *,
    keep: Iterable[str] = CODEX_CHILD_KEEP,
) -> list[str]:
    """Delete every foreign secret from the FULL env mapping ``env`` in place.
    Returns the names dropped (for tests/diagnostics)."""
    keep = tuple(keep)
    dropped = [name for name, value in env.items() if is_foreign_secret(name, keep, value)]
    for name in dropped:
        del env[name]
    return dropped
