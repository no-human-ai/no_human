"""Configuration loading and the subscription-auth safety boundary.

The single most important job in this module is preventing the daemon from
silently billing the metered Anthropic API. The Claude Agent SDK honours
``ANTHROPIC_API_KEY`` over ``CLAUDE_CODE_OAUTH_TOKEN`` when both are present, so
a stray key would quietly bill pay-per-token instead of the subscription. On
startup we scrub every metered-auth variable from the process environment and
assert that subscription mode is active before any task can run.
"""

from __future__ import annotations

import contextlib
import copy
import ctypes
import fnmatch
import ipaddress
import logging
import math
import os
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import yaml

# Home for the user's private token + config. Never inside the repo.
log = logging.getLogger("no_human.config")

NO_HUMAN_HOME = Path.home() / ".no_human"
ENV_PATH = NO_HUMAN_HOME / ".env"
CONFIG_PATH = NO_HUMAN_HOME / "config.yaml"
DB_PATH = NO_HUMAN_HOME / "no_human.db"

# The subscription token the SDK / `claude` CLI reads.
SUBSCRIPTION_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Auth profiles let one install hold several subscriptions' tokens side by side
# in the same chmod-600 .env: the unsuffixed SUBSCRIPTION_TOKEN_VAR is the
# "default" profile, and any other profile <p> lives in
# ``CLAUDE_CODE_OAUTH_TOKEN_<P>``. Exactly one of them is exported into
# SUBSCRIPTION_TOKEN_VAR at startup, so a task can never span two subscriptions.
DEFAULT_AUTH_PROFILE = "default"

# The profile whose token this process exported, set by :func:`load_env_token`.
# Read it through :func:`active_auth_profile` — never re-derive it from config,
# which a long-lived server may have outlived.
_ACTIVE_AUTH_PROFILE: str | None = None

# Variables that, if present, route to metered API / cloud billing instead of
# the subscription. ANTHROPIC_API_KEY is the dangerous one (wins precedence).
# The one metered var that is a SANCTIONED billing path in BYO-API-key mode
# (llm.auth_mode: "api_key"). It stays in METERED_AUTH_VARS so subscription mode
# still scrubs it; api_key mode passes it to scrub's ``keep`` and requires it.
API_KEY_VAR = "ANTHROPIC_API_KEY"

METERED_AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AWS_BEARER_TOKEN_BEDROCK",
)


# --------------------------------------------------------------------------- #
# The SECOND coding backend's credential (OpenAI Codex).                       #
# --------------------------------------------------------------------------- #
#
# TWO SANCTIONED PATHS, selected by `llm.codex_auth_mode` (default "api_key",
# so no existing install changes — operator amendment, 2026-08-22, after the
# operator ran `codex login`, asked "can't we support both options?" and
# instructed "do it" / "you have it"):
#
# * "api_key" — YOUR OWN OpenAI API key, from ~/.no_human/.env. No
#   browser-login flow, no `codex login` call from no_human. The Codex CLI
#   IS still invoked with `preferred_auth_method="apikey"`, but that flag is
#   NOT what stops a silent fallback to a ChatGPT credential that happens to
#   exist on the machine — codex-cli 0.149.0 silently IGNORES it (measured
#   live 2026-08-25: a bogus key, with a ChatGPT session already present,
#   still billed the ChatGPT plan). The actual gate is CLI-verified, not
#   claimed: `agent.codex_backend.assert_api_key_billing_path` writes the key
#   into a no_human-owned `CODEX_HOME` (`~/.no_human/codex-home`, never the
#   operator's own credential directory) and refuses the run unless
#   `codex login status` against THAT directory reports an api_key-backed
#   session. See `assert_codex_api_key_mode` below and
#   `agent/codex_backend.py`'s module docstring.
# * "subscription" — the operator's own ChatGPT sign-in, done by the
#   operator running `codex login` themselves (no_human never calls, wraps
#   or shells out to `codex login` — only `codex login status`). no_human
#   holds NO OpenAI credential in this mode: presence is an existence check
#   via `codex login status` only, and the local ChatGPT credential file is
#   never read, parsed, copied or even stat'd. `preferred_auth_method` is
#   simply omitted from argv in this mode rather than pointed at a value we
#   don't have.
#
# SOURCING for the split (recorded so a future reader can re-judge, not
# inherit it): learn.chatgpt.com/docs/auth presents subscription sign-in and
# API-key sign-in as two supported ways for a PERSON to sign in, and says the
# CLI supports both for LOCAL work — but steers PROGRAMMATIC workflows
# (nearer what no_human does) to the API key. Whether a THIRD-PARTY tool may
# drive that sign-in is unresolved (`openai/codex` discussion #8338 answered
# only the licensing half). The flat "OpenAI's terms prohibit..." sentence
# this comment used to assert was never found in OpenAI's terms and has been
# removed — do not reintroduce it, and do not assert a new prohibition
# either. This is the operator's call taken under stated uncertainty, not a
# finding of law — a lawyer should still settle it.
#
# The same discipline as the Anthropic key applies verbatim in api_key mode:
# the MODE (`worker.backend: codex`, `llm.codex_auth_mode`) may live in
# config.yaml, the KEY never does — it comes from ~/.no_human/.env, chmod 600
# (see `_reject_api_key_in_config`, which names both vendors' keys).
CODEX_API_KEY_VAR = "OPENAI_API_KEY"

# Variables that would silently REROUTE an OpenAI call to somebody else's
# endpoint or somebody else's bill. Deliberately a SEPARATE tuple from
# METERED_AUTH_VARS rather than an extension of it: that tuple is the Anthropic
# scrub list, is asserted verbatim by a test, and is applied on EVERY run —
# including runs that use no OpenAI at all. Scrubbing these is only correct when
# Codex is the selected backend, which is exactly when `assert_codex_api_key_mode`
# runs.
#
# NOT included, deliberately: OPENAI_ORG_ID / OPENAI_PROJECT. They select which
# of the key-holder's OWN org/projects is billed, which is a legitimate choice an
# operator may have made in their shell; removing them would silently move their
# invoice. The ones below point the request somewhere else entirely.
CODEX_ALTERNATE_ROUTING_VARS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_AD_TOKEN",
)

# The two legal values of `llm.codex_auth_mode`. Never fall back silently on
# an unrecognised value — see `codex_auth_mode()` below.
CODEX_AUTH_MODES = ("api_key", "subscription")

# The alternate name some Codex CLI builds read for the OpenAI API key.
# no_human never authenticates with it — it is named here only so
# subscription mode can scrub it from the child env if an operator's shell
# happens to export it (the same "no third path" reasoning as
# CODEX_ALTERNATE_ROUTING_VARS above).
CODEX_ALT_API_KEY_VAR = "CODEX_API_KEY"

# Every var that must be ABSENT from the child env for a Codex run in
# "subscription" mode to bill the ChatGPT plan and nothing else: both
# spellings of the OpenAI API key, plus every alternate-routing var — an
# OPENAI_BASE_URL redirects the bill just as effectively as a key.
CODEX_SUBSCRIPTION_SCRUB_VARS = (
    CODEX_API_KEY_VAR,
    CODEX_ALT_API_KEY_VAR,
) + CODEX_ALTERNATE_ROUTING_VARS


# If the operator's local server enforces a key, it lives in ~/.no_human/.env
# as LOCAL_LLM_API_KEY. Named here so the assert's docstring and the error
# texts have one source of truth. NOT READ in this ticket — part 2 owns the
# backend seam that would use it.
LOCAL_LLM_API_KEY_VAR = "LOCAL_LLM_API_KEY"


# Windows cannot express POSIX permission bits: `os.chmod` there only toggles
# FILE_ATTRIBUTE_READONLY, and the mode argument to `os.open` is ignored except
# for that same bit. So every `0o600` in this module is a SILENT NO-OP on
# Windows and the credential file inherits whatever ACL its directory carries.
# Read it through this constant rather than testing `os.name` inline, so the
# Windows branches are reachable (and therefore testable) from any platform.
_IS_WINDOWS = os.name == "nt"

_ICACLS_OK_TAIL = ("Successfully processed", "Failed processing")

# The Windows Trusted Computing Base: SYSTEM and the local Administrators group.
# These are the platform's root-equivalent — the exact analog of POSIX ``root``,
# which the ``os.chmod(path, 0o600)`` branch of this module leaves with full
# access and cannot exclude. Any local administrator can already read this file,
# take ownership of it, or run as SYSTEM to reach it regardless of its ACL, so
# treating them as forbidden is BOTH impossible on an admin-owned file — the
# common case, since the primary account on most personal Windows installs is a
# local admin, and files it creates carry EXPLICIT Administrators/SYSTEM ACEs
# that ``/inheritance:r`` does not strip — AND stricter than the POSIX contract
# this module mirrors. Accepting them realigns Windows with that contract; the
# readback STILL flags every OTHER principal (Users, Everyone, a specific
# non-owner account), which is the real protection for a non-admin user.
#
# Matched by the localized names an en-US readback emits AND by the well-known
# SIDs, since icacls prints a raw SID when it cannot resolve a name. A
# non-English Windows emits localized display names not listed here; those fall
# through to the throw, which names the surviving principal, so an incomplete
# allowlist is self-reporting on the next run rather than a silent weakening.
_WINDOWS_TCB_NAMES = frozenset({"nt authority\\system", "builtin\\administrators"})
_WINDOWS_TCB_SIDS = frozenset({"s-1-5-18", "s-1-5-32-544"})


def _is_windows_tcb_principal(grantee: str) -> bool:
    """True if *grantee* is SYSTEM or the local Administrators group."""
    g = (grantee or "").lstrip("*").strip().lower()
    return g in _WINDOWS_TCB_NAMES or g in _WINDOWS_TCB_SIDS


def _non_owner_grantees(grantees: set[str], principal: str) -> set[str]:
    """Grantees that are neither the owner nor the Windows TCB — i.e. some OTHER
    account can reach the credential. Case-insensitive: Windows account names
    are, and a case difference would otherwise fail closed on a secured file.
    """
    owner = principal.casefold()
    return {
        g for g in grantees
        if g.casefold() != owner and not _is_windows_tcb_principal(g)
    }


class ConfigError(RuntimeError):
    """Raised when config.yaml asks for something the product cannot do."""


class AuthError(RuntimeError):
    """Raised when the process is not provably in subscription-billing mode."""


class CredentialPermissionError(AuthError):
    """Raised when a credential file cannot be restricted to its owner.

    This is a FAIL-CLOSED signal, not a warning. The alternative — writing an
    OAuth token or an ``ANTHROPIC_API_KEY`` into a file whose permissions we
    could not verify — is the failure this class exists to make impossible.
    """


def _windows_owner_principal() -> str:
    """The account to grant the credential file to, as ``DOMAIN\\USER``.

    Derived from the process environment rather than from a Win32 call so no
    dependency is added. ``USERNAME`` is set by every interactive and service
    logon; if it is missing we cannot name a grantee and must fail closed.
    """
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        raise CredentialPermissionError(
            "cannot secure the credential file: USERNAME is not set, so there "
            "is no account to restrict it to. Set USERNAME, or move "
            "NO_HUMAN_HOME to a directory only you can read."
        )
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    return f"{domain}\\{user}" if domain else user


def _icacls_grantees(path: Path, output: str) -> set[str]:
    """Parse ``icacls <path>`` output into the set of granted principals.

    ``icacls`` prints ``<path> <PRINCIPAL>:(perms)`` on the first line and
    ``<PRINCIPAL>:(perms)`` (indented) on each subsequent one, then a summary.
    Parsing is deliberately permissive about WHAT the permissions are: any
    principal appearing at all is access we did not intend to grant.
    """
    grantees: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith(_ICACLS_OK_TAIL):
            continue
        # Strip the path prefix icacls repeats on its first line.
        if line.startswith(str(path)):
            line = line[len(str(path)):].strip()
        if ":(" not in line:
            continue
        grantees.add(line.split(":(", 1)[0].strip())
    return grantees


def _run_icacls(args: list[str]) -> tuple[int, str]:
    """Run ``icacls`` with *args*; return ``(returncode, stdout+stderr)``.

    Split out so tests can drive both the Windows success and failure paths
    from a POSIX host, where ``icacls`` does not exist.
    """
    import shutil as _shutil
    import subprocess as _subprocess

    exe = _shutil.which("icacls")
    if exe is None:
        raise CredentialPermissionError(
            "cannot secure the credential file: `icacls` was not found on "
            "PATH, so its permissions cannot be restricted to your account. "
            "Refusing to write a credential that any account on this machine "
            "could read."
        )
    proc = _subprocess.run(
        [exe, *args], capture_output=True, text=True, timeout=30,
    )
    return proc.returncode, f"{proc.stdout}\n{proc.stderr}"


def windows_restrict_to_owner(path: Path, *, directory: bool = False) -> None:
    """Replace *path*'s ACL with an owner-only one, then VERIFY the result.

    Two steps, and the second is the one that matters: a ``chmod`` that returns
    successfully having done nothing is exactly the defect this replaces, so
    the ACL is read back and every principal on it is checked. Raises
    :class:`CredentialPermissionError` if the file is still reachable by any
    account other than its owner.

    UNTESTED ON WINDOWS — no Windows host was available. The command shapes and
    the readback parser are covered by tests that drive them from POSIX.
    """
    principal = _windows_owner_principal()
    # (OI)(CI) makes a directory's ACE inheritable by its future contents; on a
    # file those flags are meaningless and icacls rejects them.
    rights = "(OI)(CI)(F)" if directory else "(R,W)"
    code, out = _run_icacls([
        str(path), "/inheritance:r", "/grant:r", f"{principal}:{rights}",
    ])
    if code != 0:
        raise CredentialPermissionError(
            f"cannot secure {path}: icacls exited {code}. {out.strip()}"
        )
    windows_assert_owner_only(path)


def windows_assert_owner_only(path: Path) -> None:
    """Raise unless *path*'s ACL grants access to its owner and nobody else."""
    principal = _windows_owner_principal()
    code, out = _run_icacls([str(path)])
    if code != 0:
        raise CredentialPermissionError(
            f"cannot verify permissions on {path}: icacls exited {code}. "
            f"{out.strip()}"
        )
    grantees = _icacls_grantees(path, out)
    if not grantees:
        raise CredentialPermissionError(
            f"cannot verify permissions on {path}: icacls listed no grantees, "
            f"so the restriction cannot be confirmed to have taken effect."
        )
    # SYSTEM and the local Administrators group are the platform TCB and are
    # accepted (see _non_owner_grantees / _WINDOWS_TCB_*); any OTHER non-owner
    # grantee is the fail-closed signal.
    extra = _non_owner_grantees(grantees, principal)
    if extra:
        raise CredentialPermissionError(
            f"refusing to write a credential to {path}: it is still readable "
            f"by {', '.join(sorted(extra))}. Move NO_HUMAN_HOME to a location "
            f"only your account can reach, or fix the ACL with: "
            f'icacls "{path}" /inheritance:r /grant:r "{principal}:(R,W)"'
        )


@dataclass
class ScrubReport:
    """What the startup scrub found and removed."""

    removed: list[str] = field(default_factory=list)
    api_key_present: bool = False


def scrub_metered_auth(
    env: dict[str, str] | os._Environ | None = None,
    *,
    keep: tuple[str, ...] = (),
) -> ScrubReport:
    """Remove every metered-auth variable from ``env`` (process env by default).

    Returns a report listing what was removed and whether the dangerous
    ``ANTHROPIC_API_KEY`` was among them. Callers decide whether its presence is
    fatal (see :func:`assert_subscription_mode`). Scrubbing is unconditional so
    that even a caller that swallows the error cannot fall through to metered
    billing.

    ``keep`` names variables to leave in place. Its only sanctioned use is
    BYO-API-key mode, where ``ANTHROPIC_API_KEY`` is the CHOSEN billing path and
    every OTHER redirect (auth token, Bedrock, Vertex) is still scrubbed so the
    run bills exactly one path. Empty by default — subscription mode scrubs all.
    """
    target = os.environ if env is None else env
    report = ScrubReport()
    for var in METERED_AUTH_VARS:
        if var in keep:
            continue
        if var in target and target[var]:
            report.removed.append(var)
            if var == "ANTHROPIC_API_KEY":
                report.api_key_present = True
            del target[var]
    return report


def _read_env_file(env_path: Path | None = None) -> dict[str, str]:
    """Parse ``~/.no_human/.env`` into ``{key: value}``, dropping blanks.

    Comments, blank lines, and keys with an empty value are skipped, and
    surrounding quotes are stripped. The returned values are secrets: callers
    must never log or return them (constraint §8).

    ``env_path`` resolves at CALL time. A ``= ENV_PATH`` default binds at
    import, so a test redirecting ``config.ENV_PATH`` would still read (and,
    for the writer, WRITE) the operator's real credential file.
    """
    env_path = ENV_PATH if env_path is None else env_path
    entries: dict[str, str] = {}
    if not env_path.exists():
        return entries
    # split("\n"), NOT splitlines(): the latter also breaks on \x0b \x0c
    # \x1c \x1d \x1e \x85 U+2028 U+2029, so a value carrying any of them
    # would be parsed as EXTRA VARIABLES that no writer ever wrote.
    # Explicit UTF-8 both here and in `atomic_write_0600`: Python's default
    # text encoding is the locale's, which is cp1252 on most Windows installs,
    # so a round trip through the default would corrupt (or raise on) any
    # non-ASCII value this file has always been able to hold.
    for raw in env_path.read_text(encoding="utf-8").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            entries[key.strip()] = value
    return entries


# `\Z`, not `$`: `$` also matches just BEFORE a trailing newline, so the
# charset above would have exempted one — "personal2\n" read as valid.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")
# A profile name is a label, not free text. The regex alone accepts
# `sk-ant-oat01-…` — a credential is lowercase letters, digits and hyphens — so
# a token pasted into the profile box was stored as a NAME and then echoed back
# by the status endpoint, which advertises "names and booleans only". Two
# adjacent text boxes in a settings form is the likeliest operator error, and
# the active profile name is stamped on every run, so it also reaches the DB and
# logs. A length cap plus a credential-shape check makes that unreachable.
MAX_PROFILE_NAME_LEN = 32
# Unauthenticated + unbounded is a bad combination: a 2MB token was accepted
# and written. Real OAuth tokens are a few hundred bytes.
MAX_TOKEN_LEN = 4096
_CREDENTIAL_SHAPED = ("sk-ant-", "sk_ant_")


def validate_profile_name(profile: str) -> str:
    """Normalise and validate an auth profile name; return the normalised form.

    NEVER echoes the input. The credential-shape and length branches were
    written not to, but the regex branch echoed `{profile!r}` — and that is
    precisely the branch an enterprise token falls into, since enterprise
    tokens are deliberately not whitelisted by shape. Putting a pasted
    credential in the error message just moves the disclosure into the 422.
    """
    profile = profile.strip().lower()
    if profile.startswith(_CREDENTIAL_SHAPED):
        raise AuthError(
            "that looks like a token, not a profile name — did you paste it "
            "into the wrong field? A profile is a short label like 'personal'.")
    if len(profile) > MAX_PROFILE_NAME_LEN:
        raise AuthError(
            f"auth profile name is too long (max {MAX_PROFILE_NAME_LEN} "
            f"chars) — a profile is a short label.")
    if not _PROFILE_NAME_RE.match(profile):
        raise AuthError(
            "invalid auth profile name — use lowercase letters, digits, "
            "'-' and '_' only.")
    return profile


def profile_token_var(profile: str) -> str:
    """The .env variable holding *profile*'s token. Names only, never values.

    The name is validated with the same rule ``set_auth_profile`` applies. This
    function builds an .env KEY, and a key carrying a newline would let a
    caller inject an arbitrary extra line into ``~/.no_human/.env`` — a forged
    ``ANTHROPIC_API_KEY=`` among them, which is exactly the metered-billing
    escape constraint #1 exists to prevent. Harmless while the only caller was
    the operator's own shell; not harmless once a profile name can arrive over
    HTTP.
    """
    profile = validate_profile_name(profile)
    if profile == DEFAULT_AUTH_PROFILE:
        return SUBSCRIPTION_TOKEN_VAR
    return f"{SUBSCRIPTION_TOKEN_VAR}_{profile.upper()}"


def available_auth_profiles(env_path: Path | None = None) -> list[str]:
    """Profile names that have a token, in ``~/.no_human/.env`` or the process
    environment. Returns names only — a token value is never returned or logged.
    """
    env_path = ENV_PATH if env_path is None else env_path
    prefix = SUBSCRIPTION_TOKEN_VAR + "_"
    found: set[str] = set()
    for source in (_read_env_file(env_path), os.environ):
        for key, value in source.items():
            if not value:
                continue
            if key == SUBSCRIPTION_TOKEN_VAR:
                found.add(DEFAULT_AUTH_PROFILE)
            elif key.startswith(prefix):
                found.add(key[len(prefix):].lower())
    return sorted(found)


def active_auth_profile() -> str | None:
    """The profile whose token this process actually exported, or None.

    This reports what :func:`load_env_token` did, not what config.yaml currently
    says. A long-lived server exports its token once at startup; if the operator
    then runs ``nh auth use other``, config on disk changes but the running
    process is still billing the old subscription. Attributing a burn to the
    config value would be a lie, so every stamp reads this instead.
    """
    return _ACTIVE_AUTH_PROFILE


def load_env_token(
    env_path: Path | None = None, *, profile: str | None = None
) -> str | None:
    """Resolve *profile*'s token and export it as ``CLAUDE_CODE_OAUTH_TOKEN``.

    The .env is the source of truth (chmod 600, gitignored, never in the repo).
    A token already in the process environment is used as a fallback and is not
    overwritten. Exactly one token is exported — the SDK reads only the
    unsuffixed variable — so a run can never span two subscriptions.

    Returns the active token, or None if the *default* profile has none. A named
    profile with no token raises :class:`AuthError` rather than falling back to
    the default: a silent fallback would bill the wrong subscription.
    """
    global _ACTIVE_AUTH_PROFILE
    profile = (profile or DEFAULT_AUTH_PROFILE).strip().lower()
    var = profile_token_var(profile)
    # .env wins over an inherited token: it is the curated source.
    env_path = ENV_PATH if env_path is None else env_path
    token = _read_env_file(env_path).get(var) or os.environ.get(var)

    if not token:
        if profile != DEFAULT_AUTH_PROFILE:
            available = ", ".join(available_auth_profiles(env_path)) or "none"
            raise AuthError(
                f"auth profile '{profile}' has no token. Expected {var} in "
                f"{env_path} (chmod 600) or the process environment.\n"
                f"Profiles with a token: {available}\n"
                f"Switch with:  nh auth use <profile>"
            )
        return None

    os.environ[SUBSCRIPTION_TOKEN_VAR] = token
    _ACTIVE_AUTH_PROFILE = profile
    return token


def load_api_key(env_path: Path | None = None) -> str | None:
    """BYO-API-key mode only: resolve ``ANTHROPIC_API_KEY`` from
    ``~/.no_human/.env`` (source of truth) or the process env, and export it.

    Mirrors :func:`load_env_token`'s discipline — .env wins, an inherited value
    is a non-overwritten fallback — but for the metered key the operator has
    explicitly chosen to bill. Returns the key or None; NEVER echoes it. Only
    :func:`assert_subscription_mode` in ``api_key`` mode may call this; every
    other path treats ``ANTHROPIC_API_KEY`` as forbidden.
    """
    env_path = ENV_PATH if env_path is None else env_path
    key = _read_env_file(env_path).get(API_KEY_VAR) or os.environ.get(API_KEY_VAR)
    if key:
        os.environ[API_KEY_VAR] = key
    return key or None


def assert_codex_api_key_mode(env_path: Path | None = None) -> ScrubReport:
    """Enforce BYO-API-key billing for the Codex coding backend.

    Called ONLY when ``worker.backend`` is ``"codex"``, and IN ADDITION to
    :func:`assert_subscription_mode` — not instead of it. That is deliberate and
    is the one place the "a run bills exactly one path" rule needed restating
    for a two-vendor world: with Codex selected, the CODER bills OpenAI and the
    reviewer, planner, supervisor and utility tiers still bill Anthropic,
    because the review gate and the four model tiers are pinned to Claude by
    constraint. So the invariant is
    per-vendor: exactly one Anthropic credential and exactly one OpenAI
    credential, each the one the operator chose, with every alternate routing
    for both scrubbed. Two vendors, two bills, no third path.

    Raises :class:`AuthError` when no ``OPENAI_API_KEY`` resolves. Never echoes
    the key.

    Kept byte-identical in signature and control flow (2026-08-22 amendment):
    only the message text below changed, to drop the now-unsourced "OpenAI's
    terms prohibit..." absolute and name the sibling mode instead.

    This function only resolves and scrubs — it does NOT verify the key
    actually bills OpenAI rather than a live ChatGPT session. That
    CLI-verified check happens later, once per run, in
    :func:`no_human.agent.codex_backend.assert_api_key_billing_path`, gating
    :meth:`CodexBackend._child_env_api_key`: it points the CLI at a
    no_human-owned ``CODEX_HOME`` holding only this key and refuses the run
    unless ``codex login status`` against THAT directory reports an
    api_key-backed session. See the module comment above (:86) and
    ``agent/codex_backend.py``'s module docstring for the full mechanism.
    """
    env_path = ENV_PATH if env_path is None else env_path
    key = _read_env_file(env_path).get(CODEX_API_KEY_VAR) or os.environ.get(
        CODEX_API_KEY_VAR)
    report = ScrubReport()
    for var in CODEX_ALTERNATE_ROUTING_VARS:
        if os.environ.get(var):
            report.removed.append(var)
            del os.environ[var]
    if not key:
        raise AuthError(
            "the coder backend is 'codex' (worker.backend, or a task's "
            "--backend) but no OPENAI_API_KEY was found, and "
            "llm.codex_auth_mode is 'api_key' (the default). The Codex "
            "backend runs on YOUR OWN OpenAI API key in this mode.\n"
            f"Add the key to {env_path} (chmod 600):\n"
            "  echo 'OPENAI_API_KEY=sk-...' >> ~/.no_human/.env\n"
            "It must never go in config.yaml. To go back to Claude, set "
            "worker.backend: claude. Or, to run on your ChatGPT plan "
            "instead, set llm.codex_auth_mode: subscription in config.yaml "
            "and sign in yourself with `codex login`. Whether a "
            "third-party tool may drive that ChatGPT sign-in is "
            "unresolved — a lawyer should still settle it."
        )
    os.environ[CODEX_API_KEY_VAR] = key
    return report


def codex_auth_mode(data: dict[str, Any]) -> str:
    """Read ``llm.codex_auth_mode`` from a config dict; default ``"api_key"``.

    Never a silent fallback on an unrecognised value — a typo here would
    otherwise quietly bill the metered API instead of the plan the operator
    thought they had selected, so an unrecognised spelling is a fail-loud
    :class:`AuthError` naming the key and both legal values, not a default.
    """
    raw = (((data or {}).get("llm") or {}).get("codex_auth_mode") or "api_key")
    value = str(raw).strip().lower()
    if value not in CODEX_AUTH_MODES:
        raise AuthError(
            f"llm.codex_auth_mode is {raw!r}; it must be one of "
            f"{CODEX_AUTH_MODES!r}."
        )
    return value


def assert_codex_subscription_mode(*, cli_path: str | None = None,
                                    session_check: Any = None) -> ScrubReport:
    """Enforce ChatGPT-subscription billing for the Codex coding backend.

    Holds no OpenAI credential of its own: scrubs every var in
    :data:`CODEX_SUBSCRIPTION_SCRUB_VARS` from the process env FIRST, so the
    session probe below sees the exact env the run will, then asks the
    ``codex`` CLI itself whether a session is live via
    ``codex login status`` (existence check only — never ``codex login``,
    never reads/parses/copies/stats the local ChatGPT credential file).

    ``session_check``, if given, replaces the call to
    :func:`agent.codex_backend.codex_login_status` — the seam tests use to
    avoid shelling out to a real ``codex`` binary.

    Raises :class:`AuthError` naming ``llm.codex_auth_mode: subscription``
    and instructing the operator to run ``codex login`` themselves when no
    accepted session is found. Never echoes a credential — there is none to
    echo.
    """
    report = ScrubReport()
    for var in CODEX_SUBSCRIPTION_SCRUB_VARS:
        if os.environ.get(var):
            report.removed.append(var)
            del os.environ[var]
    if session_check is None:
        from .agent.codex_backend import codex_login_status
        session_check = lambda: codex_login_status(cli_path)  # noqa: E731
    status = session_check()
    if not status.present or status.via == "api_key":
        raise AuthError(
            "the coder backend is 'codex' and llm.codex_auth_mode is "
            "'subscription', but no ChatGPT session was found (`codex "
            "login status`). no_human holds no OpenAI credential in this "
            "mode and never reads your local ChatGPT credential file — sign "
            "in yourself:\n"
            "  codex login\n"
            "Or, to run on your own OpenAI API key instead, set "
            "llm.codex_auth_mode: api_key in config.yaml."
        )
    return report


def assert_codex_mode(mode: str, *, cli_path: str | None = None,
                       env_path: Path | None = None,
                       session_check: Any = None) -> ScrubReport:
    """Dispatch to the credential assertion for the selected Codex auth mode.

    The single call site both preflights (`cli/commands.py`'s `_bootstrap`
    and `core/runtime.py`'s `assert_task_backend_usable`) now use, so the two
    can never drift. ``session_check`` is injectable for tests — see
    :func:`assert_codex_subscription_mode`.
    """
    if mode == "subscription":
        return assert_codex_subscription_mode(
            cli_path=cli_path, session_check=session_check)
    return assert_codex_api_key_mode(env_path)


# The literal RFC1918 IPv4 ranges — the ONLY non-loopback hosts local mode
# trusts. is_private is deliberately NOT used here: it also admits IPv4
# link-local 169.254.0.0/16 (cloud IMDS 169.254.169.254), 0.0.0.0/8, TEST-NET,
# and — for IPv6 — the fc00::/7 ULA block that carries the IPv6 IMDS endpoint
# fd00:ec2::254. Gating on membership in these three nets keeps the boundary at
# exactly what constraint #6c says ("loopback/RFC1918") and no wider.
_RFC1918_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def assert_local_backend_mode(base_url: str | None) -> ScrubReport:
    """Enforce the local-model coding backend's safety boundary.

    Called ONLY when ``worker.backend`` is ``"local"``, and IN ADDITION to
    :func:`assert_subscription_mode` — not instead of it, for the same
    per-vendor reason :func:`assert_codex_api_key_mode` states: planner,
    supervisor and utility stay on Claude; the reviewer defaults to Claude too,
    but Settings can override it, disclosed on the task detail.

    ``base_url`` must point at a server this machine trusts by construction —
    localhost or a literal loopback/RFC1918 IP address, http or https. A DNS
    name is refused even if it currently resolves to a loopback address: a
    name is resolved again at connect time, which is a rebinding surface, so
    only a literal IP is accepted. A public/routable IP is refused because
    local mode must not leave the machine. Port numbers and paths are not
    validated — ``http://localhost:8000`` and ``http://127.0.0.1:1234/v1``
    are both fine.

    An ambient ``ANTHROPIC_BASE_URL`` is never trusted as a fallback for this
    setting — it is one of the vars :func:`scrub_metered_auth` removes, and
    this function re-runs that scrub with ``keep=()`` as a belt-and-braces
    re-check, exactly like :func:`assert_codex_api_key_mode`.

    If the local server enforces a key, it lives in ``~/.no_human/.env`` as
    ``LOCAL_LLM_API_KEY`` (see :data:`LOCAL_LLM_API_KEY_VAR`) — this function
    does not read it; that is part 2's backend seam.

    Raises :class:`AuthError` on any refusal. Never echoes credentials.
    """
    url = (base_url or "").strip()
    if not url:
        raise AuthError(
            "the coder backend is 'local' (worker.backend, or a task's "
            "--backend) but llm.local_base_url is not set. An "
            "ambient ANTHROPIC_BASE_URL is scrubbed and never trusted as a "
            "fallback.\n"
            "Set it in config.yaml:\n"
            "  llm:\n"
            "    local_base_url: http://localhost:8000"
        )

    try:
        parsed = urlsplit(url)
    except ValueError:
        raise AuthError(
            f"worker.backend is 'local' but llm.local_base_url ({url!r}) "
            "could not be parsed as a URL."
        ) from None

    if parsed.username or parsed.password:
        raise AuthError(
            "llm.local_base_url must not embed userinfo credentials before "
            "the host. The mode lives in config; the key never does — if "
            "the local server enforces one, put it in ~/.no_human/.env as "
            f"{LOCAL_LLM_API_KEY_VAR}."
        )

    if parsed.scheme not in ("http", "https"):
        raise AuthError(
            f"llm.local_base_url has scheme {parsed.scheme!r}; only http and "
            "https are accepted."
        )

    host = parsed.hostname
    if not host:
        raise AuthError(
            f"llm.local_base_url ({parsed.scheme}://...) has no host."
        )

    if host != "localhost":
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise AuthError(
                f"llm.local_base_url host {host!r} is a DNS name, not a "
                "literal IP. A name is resolved again at connect time, which "
                "is a rebinding surface — use 'localhost' or a literal "
                "loopback/RFC1918 IP address instead."
            ) from None
        if not (ip.is_loopback or any(ip in net for net in _RFC1918_NETS)):
            raise AuthError(
                f"llm.local_base_url host {host!r} is a public/routable "
                "address (or a link-local metadata endpoint). Local mode must "
                "not leave the machine — use 'localhost' or a literal "
                "loopback/RFC1918 IP address."
            )

    return scrub_metered_auth(keep=())


def load_env_var(name: str, env_path: Path | None = None) -> str | None:
    """Load a single secret (e.g. ``JENKINS_API_TOKEN``) from ``~/.no_human/.env``
    into the process env, following the same discipline as the OAuth token: the
    .env (chmod 600, gitignored, never in the repo) is the source of truth, an
    inherited value is a non-overwritten fallback. Returns the active value or
    None. Used for CI/VCS credentials — these must never live in config.yaml or
    the repo, only in the private .env or the process environment.
    """
    if name in METERED_AUTH_VARS:
        # Defensive: a metered-auth var must never be loaded as a generic secret.
        raise AuthError(f"{name} is a metered-auth variable and must never be loaded.")
    env_path = ENV_PATH if env_path is None else env_path
    value = _read_env_file(env_path).get(name)
    if value:
        os.environ[name] = value
    return os.environ.get(name) or None


def read_env_var_value(name: str, env_path: Path | None = None) -> str | None:
    """Read a single secret's VALUE without exporting it to ``os.environ``.

    Same source-of-truth discipline as :func:`load_env_var` — ``~/.no_human/.env``
    (chmod 600, gitignored) wins, an inherited process-env value is a fallback —
    but this is a pure read: unlike :func:`load_env_var` it never mutates
    ``os.environ``, so a caller building a per-subprocess env dict (the local
    coding backend's ``extra_env``, for example) cannot accidentally leak the
    value into ITS OWN process environment just by looking it up. Returns the
    active value or None.
    """
    if name in METERED_AUTH_VARS:
        # Same defensive guard as `load_env_var`: a metered-auth var must
        # never be loaded through this generic-secret path either.
        raise AuthError(f"{name} is a metered-auth variable and must never be loaded.")
    env_path = ENV_PATH if env_path is None else env_path
    return _read_env_file(env_path).get(name) or os.environ.get(name) or None


def credential_status(
    keys: list[str], env_path: Path | None = None
) -> dict[str, bool]:
    """Report which of ``keys`` currently resolve to a value — from the process
    env or ``~/.no_human/.env``. Returns ``{key: present}`` and NEVER returns or
    logs the value itself (constraint §8: secrets are never echoed). Used by
    ``nh onboard`` to tell the human exactly which .env keys are still missing.
    """
    env_path = ENV_PATH if env_path is None else env_path
    present_in_env = _read_env_file(env_path)
    return {
        key: bool(os.environ.get(key)) or key in present_in_env
        for key in keys
    }


def assert_single_env_line(text: str, what: str = "value") -> None:
    """Reject anything that would not survive a round-trip as ONE .env line.

    Checking only ``\n``/``\r`` was not enough: ``str.splitlines()`` — which
    the reader used — also breaks on ``\x0b \x0c \x1c \x1d \x1e \x85``,
    U+2028 and U+2029, so eight characters slipped past a guard that claimed to
    stop line injection. A NUL is rejected for a different reason: it round-trips
    fine but ``os.environ[...] = value`` then raises ``ValueError: embedded null
    byte`` on EVERY subsequent start, which bricks the daemon persistently.

    The value is never echoed back in the error (constraint §8).
    """
    if "\x00" in text:
        raise AuthError(f"{what} must not contain a null byte")
    # `splitlines()` DROPS a trailing separator, so a length check alone
    # accepts "tok\u2028": it survived the write and the reader then returned
    # a silently TRUNCATED token — no injection, but a credential that fails
    # for no visible reason. Comparing against the round trip catches leading,
    # interior AND trailing breaks in one rule, for all ten separators.
    parts = text.splitlines()
    if len(parts) > 1 or (parts and parts[0] != text) or (not parts and text):
        raise AuthError(f"{what} must be a single line")


def secure_credential_file(path: Path) -> None:
    """Restrict an ALREADY-WRITTEN credential file to its owner, or raise.

    For writers that cannot use :func:`atomic_write_0600` because something
    else produced the file (Playwright's ``storage_state``, for one). POSIX:
    ``chmod 0600``. Windows: owner-only ACL plus readback, because ``chmod``
    there is a silent no-op. Raises on Windows when the restriction cannot be
    proven — the caller must then DELETE the file it could not secure.
    """
    if _IS_WINDOWS:
        windows_restrict_to_owner(Path(path))
    else:
        os.chmod(path, 0o600)


def ensure_private_dir(path: Path) -> Path:
    """Create *path* and make it private (0700), even if it ALREADY EXISTS.

    `mkdir(mode=0o700)` is NOT sufficient and was a silent no-op here: Python
    applies `mode` only when it CREATES the directory, and several other call
    sites (the DB, config.yaml, the repo-map cache) create ~/.no_human at the
    process umask first — so by the time a credential is written the directory
    is already 0755 and stays that way. The .env's own 0600 does not protect
    the config.yaml, no_human.db and cache/ sitting beside it.

    chmod only when the bits are actually wrong, so this never churns a
    directory the operator has already locked down further.
    """
    # `mode=` reaches only the LEAF: CPython's makedirs recurses without
    # forwarding it, so `makedirs(a/b/c, mode=0o700)` gives a=0755, b=0755,
    # c=0700 (measured). That still closes the window where it matters most —
    # the leaf is where the file is about to be written — while intermediate
    # levels are born at the umask and repaired a moment later by the walk
    # below, before this function returns and anything is written.
    #
    # An earlier version of this comment claimed `mode=` applied to every level
    # this call creates. It does not, and nothing observes the difference, so
    # dropping `mode=` was the one mutation the suite did not catch.
    os.makedirs(path, mode=0o700, exist_ok=True)
    if _IS_WINDOWS:
        # `mode=` and the chmod walk below are no-ops on Windows. Apply the
        # ACL equivalent instead. Unlike the credential FILE this is NOT fatal:
        # the directory holds config.yaml, the DB and the cache, none of them
        # credentials, and `nh init` refusing to run over a directory ACL it
        # cannot rewrite would be worse than the exposure. The .env inside is
        # secured (and fails closed) independently.
        try:
            windows_restrict_to_owner(path, directory=True)
        except (CredentialPermissionError, OSError) as exc:
            log.warning("could not secure %s (%s); the credential file inside "
                        "is still restricted to your account independently",
                        path, exc)
        return path
    # Secure the ANCESTORS too, up to and including ~/.no_human. `parents=True`
    # creates every missing level at the process umask, so
    # `ensure_private_dir(~/.no_human/cache)` on a fresh machine left
    # ~/.no_human itself at 0755 while the leaf was private — and the
    # credential store, config.yaml and the DB all live at THAT level. Bounded
    # to our own subtree: nothing above NO_HUMAN_HOME is ever touched.
    targets = [path]
    try:
        rel = path.resolve().relative_to(NO_HUMAN_HOME.resolve())
        targets = [NO_HUMAN_HOME.joinpath(*rel.parts[:i])
                   for i in range(len(rel.parts) + 1)]
    except (ValueError, OSError):
        pass  # not under ~/.no_human (a tmp dir, a custom path): leaf only
    for target in targets:
        try:
            # NEVER chmod through a symlink. `Path.chmod` follows links, so a
            # symlink planted inside ~/.no_human pointing at an outside
            # directory would have that target tightened to 0700 (measured:
            # 0755 -> 0700). We only secure the REAL directories we create in
            # our own subtree; a symlink the operator placed (e.g. cache on
            # fast storage) is theirs, and the .env inside is 0600 regardless.
            if target.is_symlink():
                # Skipping a planted leaf symlink is correct and silent. But if
                # NO_HUMAN_HOME ITSELF is a symlink (the operator relocated the
                # store to another disk), skipping it leaves the store dir — and
                # the config.yaml / no_human.db beside the 0600 .env — at the
                # process umask. That is a real downgrade, so make it visible
                # rather than silent (review of #221).
                if target == NO_HUMAN_HOME:
                    log.warning(
                        "%s is a symlink; leaving its target's mode as-is. "
                        "The .env is still 0600, but secure the store dir "
                        "yourself (chmod 700) if it is on a shared host.",
                        NO_HUMAN_HOME)
                continue
            mode = target.stat().st_mode & 0o7777
            if mode & 0o077:
                # CLEAR group/other, preserve everything else. `chmod(0o700)`
                # was not the no-churn rule this function documents: it
                # restored owner-write on a 0550 directory and silently dropped
                # setgid on 02750. The security goal is "no group or other
                # access"; every other bit is the operator's business.
                target.chmod(mode & ~0o077)
        except OSError as exc:  # not fatal — file modes still apply
            # But do NOT fail silently: the caller is about to write a
            # credential into a directory we could not secure.
            log.warning("could not secure %s (%s); its contents rely on their "
                        "own file modes", target, exc)
    return path


def atomic_write_0600(path: Path, content: str) -> None:
    """Atomically write *content* to *path*, mode 0600 from the first byte.

    Writes to a sibling temp file created with ``O_CREAT`` at 0600 (so there is
    never a window where it exists at the process umask), then ``os.replace``s
    it onto *path* — atomic on POSIX and immune to a world/group-readable
    window even on first creation.

    On WINDOWS the 0600 above is a silent no-op (see ``_IS_WINDOWS``), so the
    temp file's ACL is replaced with an owner-only one and READ BACK to confirm
    it took — both while the file is still EMPTY, so a credential byte is never
    written to a file whose permissions are unproven. If it cannot be secured,
    this raises :class:`CredentialPermissionError` and leaves *path* untouched
    rather than writing a token any account on the machine could read.
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        if _IS_WINDOWS:
            # Release the handle before handing the path to icacls, and secure
            # + verify it BEFORE any content exists in it.
            os.close(fd)
            windows_restrict_to_owner(tmp)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        # Broader than FileNotFoundError: on Windows the unlink of a leftover
        # temp can fail with PermissionError, and that must not mask the
        # CredentialPermissionError that is the reason we are here.
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def upsert_env_var(env_path: Path, key: str, value: str) -> None:
    """Upsert ``KEY=value`` into the .env file: replace the line if the key is
    already present, append if not, preserving every other line (including
    comments and blanks) verbatim. Written atomically at 0600. Never logs
    ``value``.

    Guards line injection HERE, at the choke point every writer goes through,
    rather than only in each caller: a value that Python considers multi-line
    would forge extra .env entries — a planted ``ANTHROPIC_API_KEY=`` among
    them, which is the metered-billing escape constraint #1 exists to prevent.
    """
    assert_single_env_line(key, "key")
    assert_single_env_line(value, "value")
    lines = (env_path.read_text(encoding="utf-8").split("\n")
             if env_path.exists() else [])
    if lines and lines[-1] == "":
        lines.pop()   # split("\n") keeps the trailing empty field
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            existing_key = stripped.split("=", 1)[0].strip()
            if existing_key == key:
                out.append(f"{key}={value}")
                replaced = True
                continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    ensure_private_dir(env_path.parent)
    atomic_write_0600(env_path, "\n".join(out) + "\n")


def assert_oauth_token_usable(token: str) -> str:
    """Refuse an OAuth token that cannot work; return it stripped.

    The refusal half of :func:`set_profile_token`, split out so a caller that
    only wants to JUDGE a token — `nh init` reporting on a credential it
    FOUND already on the machine — asks the same question without writing
    anything. Two copies of "what is a usable token" is how the CLI, the HTTP
    path and the desktop app ended up with three opinions (onboarding
    walkthrough 2026-08-09, B4b); this keeps it at one.

    Refuses, in this order: an empty token; an over-length one; one carrying a
    line break that would forge a second .env line; a metered ``sk-ant-api…``
    key, which silently bills the metered API (constraint #1 is OAuth-only).
    Both personal and enterprise OAuth tokens are first-class, so it rejects
    the known-bad shape rather than whitelisting a format.
    """
    token = (token or "").strip()
    if not token:
        raise AuthError("token must not be empty")
    if len(token) > MAX_TOKEN_LEN:
        raise AuthError(
            f"token is implausibly long ({len(token)} chars, max "
            f"{MAX_TOKEN_LEN}) — refusing to write it")
    assert_single_env_line(token, "token")
    if token.casefold().startswith("sk-ant-api"):
        raise AuthError(
            "that is an ANTHROPIC_API_KEY, not an OAuth token. This field "
            "takes a subscription or enterprise OAuth token "
            "(CLAUDE_CODE_OAUTH_TOKEN) — create one with: claude setup-token. "
            "To bill your own Anthropic API account with that key, set "
            "llm.auth_mode: api_key and keep the key in ~/.no_human/.env."
        )
    return token


def set_profile_token(profile: str, token: str,
                      env_path: Path | None = None) -> str:
    """Store *profile*'s OAuth token in ``~/.no_human/.env``; return the KEY.

    ``env_path`` is resolved at CALL time, not bound as a default argument. A
    ``= ENV_PATH`` default is captured at import, so a test that redirects
    ``config.ENV_PATH`` still writes to the operator's REAL credential file —
    which is not a hypothetical: it clobbered a live token during this
    function's own development.

    Returns the variable NAME only — the token is never returned, logged, or
    echoed (constraint §8). Refuses, in this order:

    - an invalid profile name (via :func:`profile_token_var`);
    - an empty token;
    - a token containing a newline/carriage return, which would inject an
      arbitrary extra line into .env;
    - a metered API key. Constraint #1 is OAuth-only: an ``sk-ant-api…``
      credential silently bills the metered API, which is precisely what this
      product refuses to do. Both personal and enterprise OAuth tokens are
      first-class, so the check rejects the one known-bad shape rather than
      whitelisting a format and locking out a valid enterprise token.

    The last four live in :func:`assert_oauth_token_usable` so a validate-only
    caller cannot drift from the writer.
    """
    env_path = ENV_PATH if env_path is None else env_path
    key = profile_token_var(profile)
    token = assert_oauth_token_usable(token)
    upsert_env_var(env_path, key, token)
    return key


def assert_codex_api_key_usable(key: str) -> str:
    """Refuse an OpenAI API key that cannot be written; return it stripped.

    The Codex twin of :func:`assert_oauth_token_usable`: reject an empty key,
    an implausibly long one, or one carrying a line break that would forge a
    second .env line (a planted ``ANTHROPIC_API_KEY=`` among them — the
    metered-billing escape constraint #1 shuts). No vendor-shape check: unlike
    the OAuth field, any non-empty single line is a plausible ``OPENAI_API_KEY``.
    Never echoes the key.
    """
    key = (key or "").strip()
    if not key:
        raise AuthError("key must not be empty")
    if len(key) > MAX_TOKEN_LEN:
        raise AuthError(
            f"key is implausibly long ({len(key)} chars, max {MAX_TOKEN_LEN}) "
            "— refusing to write it")
    assert_single_env_line(key, "key")
    return key


def set_codex_api_key(key: str, env_path: Path | None = None) -> str:
    """Store the OpenAI API key in ``~/.no_human/.env``; return the VARIABLE NAME.

    The KEY never goes in config.yaml — only the .env (chmod 600, gitignored),
    the same discipline :func:`set_profile_token` follows for the OAuth token
    (``_reject_api_key_in_config`` names both vendors' keys). Returns
    ``CODEX_API_KEY_VAR`` (``"OPENAI_API_KEY"``) only; the key is never
    returned, logged or echoed (constraint §8).

    ``env_path`` is resolved at CALL time, not bound as a default argument —
    the same trap :func:`set_profile_token` documents (a captured ``= ENV_PATH``
    default clobbers the operator's REAL credential file under a test redirect).
    """
    env_path = ENV_PATH if env_path is None else env_path
    key = assert_codex_api_key_usable(key)
    upsert_env_var(env_path, CODEX_API_KEY_VAR, key)
    return CODEX_API_KEY_VAR


def _assert_api_key_mode(env_path: Path | None) -> ScrubReport:
    """BYO-API-key billing (``llm.auth_mode: "api_key"``).

    An operator-authorized, explicit departure from the OAuth-only default,
    for friends/commercial installs that pay Anthropic directly with THEIR OWN
    ``ANTHROPIC_API_KEY``. Invariants preserved: the run still bills exactly ONE
    path, so every OTHER metered redirect (auth token, Bedrock, Vertex) is
    scrubbed; a missing key fails loudly; no OAuth token is exported; the
    billing path is stamped as the "api_key" profile for attribution.
    """
    global _ACTIVE_AUTH_PROFILE
    env_path = ENV_PATH if env_path is None else env_path
    key = load_api_key(env_path)
    # Scrub every metered redirect EXCEPT the key we intentionally bill with.
    # ANTHROPIC_AUTH_TOKEN alongside the key yields a 401; Bedrock/Vertex would
    # silently route billing to a cloud account.
    report = scrub_metered_auth(keep=(API_KEY_VAR,))
    # An inherited subscription token must not reach the SDK subprocess either:
    # "bills exactly one path" holds by construction, not by SDK precedence.
    if os.environ.get(SUBSCRIPTION_TOKEN_VAR):
        report.removed.append(SUBSCRIPTION_TOKEN_VAR)
        del os.environ[SUBSCRIPTION_TOKEN_VAR]
    if not key:
        raise AuthError(
            "llm.auth_mode is 'api_key' but no ANTHROPIC_API_KEY was found. "
            f"Add it to {env_path} (chmod 600) or the process environment:\n"
            "  echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.no_human/.env\n"
            "This bills your own Anthropic API account (metered). To pay with a "
            "Claude subscription instead, set llm.auth_mode: subscription."
        )
    _ACTIVE_AUTH_PROFILE = "api_key"
    return report


def assert_subscription_mode(
    env_path: Path | None = None,
    *,
    strict: bool = True,
    profile: str | None = None,
    auth_mode: str = "subscription",
) -> ScrubReport:
    """Enforce the configured billing mode before any task runs.

    ``auth_mode="subscription"`` (the default):
      1. Scrub all metered-auth variables from the process environment.
      2. If ``ANTHROPIC_API_KEY`` was present, refuse to start (``strict``) — the
         user must unset it; a silent scrub-and-continue would mask a real
         misconfiguration the operator should know about.
      3. Load and require *profile*'s subscription token (see
         :func:`load_env_token`), exporting exactly that one.

    ``auth_mode="api_key"`` (operator-authorized BYO-API-key): bill Anthropic
    directly with the operator's own key — see :func:`_assert_api_key_mode`.

    Returns the :class:`ScrubReport` on success. Raises :class:`AuthError`
    otherwise.
    """
    if auth_mode == "api_key":
        return _assert_api_key_mode(env_path)

    report = scrub_metered_auth()

    if report.api_key_present and strict:
        raise AuthError(
            "ANTHROPIC_API_KEY is set in the environment while llm.auth_mode "
            "is 'subscription'. The key has been scrubbed from this process so "
            "a run bills exactly one path, but startup is aborted so you can "
            "fix the source.\n"
            "Unset it before starting:  unset ANTHROPIC_API_KEY\n"
            "(To bill your own Anthropic API account with that key, set "
            "llm.auth_mode: api_key.)"
        )

    env_path = ENV_PATH if env_path is None else env_path
    token = load_env_token(env_path, profile=profile)
    if not token:
        raise AuthError(
            f"No subscription token found. Expected {SUBSCRIPTION_TOKEN_VAR} in "
            f"{env_path} (chmod 600) or the process environment.\n"
            "Create one with:  claude setup-token\n"
            "Inspect configured profiles with:  nh auth status"
        )
    return report


# --------------------------------------------------------------------------- #
# Config file                                                                  #
# --------------------------------------------------------------------------- #

#: Floor for the reviewer's session windows, in seconds. Below this a review
#: cannot finish, so both of its bounded rounds die on the wall and the task
#: escalates with an unreviewed diff — a value under the floor is a typo, not a
#: tuning choice. Clamped rather than raised: one bad number in one knob must
#: not make the whole install refuse to load.
#:
#: IT IS 120 AND NOT 60 BECAUSE OF `_REVIEW_MIN_RETRY_TIMEOUT` (raised in
#: adversarial review). `review.reviewer._agent_review` HALVES a round that died
#: on the wall — `max(_REVIEW_MIN_RETRY_TIMEOUT, window // 2)`, floored at 120 —
#: so any configured window between 60 and 119 would hand round TWO a window
#: LARGER than round one, inverting the rule that a hang must escalate sooner
#: rather than sit blocked longer. Aligning the two floors deletes that case
#: instead of documenting it. Keep them equal: `tests/test_config.py::
#: test_the_config_floor_never_inverts_the_retry_window` goes red if either
#: moves below the other.
REVIEW_TIMEOUT_FLOOR_S = 120


def _timeout_knob(data: dict[str, Any], key: str, default: int) -> int:
    """Read one ``llm.<key>`` seconds knob: numeric, at or above the floor, or
    the measured *default*.

    YAML hands back whatever was typed, so a string, a null, a list or a bool
    all have to survive here — ``asyncio.wait_for`` would otherwise reject them
    at review time, i.e. after the coder attempt has already been paid for.
    ``bool`` is excluded explicitly because it is an ``int`` in Python and
    ``review_timeout_seconds: true`` means nothing in seconds.

    ``math.isfinite`` is the same rule for the values that LOOK numeric and are
    not usable (found in adversarial review of this function): ``.inf``,
    ``-.inf`` and ``.nan`` are legal YAML floats, so they pass the isinstance
    test; ``nan < FLOOR`` is False so the clamp below never sees them; and the
    ``int()`` on the last line then raises OverflowError / ValueError out of a
    property read by every orchestrator construction. That is exactly the
    "one bad number must not make the install unloadable" invariant this
    function exists to hold, defeated by three characters of YAML.
    """
    raw = (data.get("llm") or {}).get(key, None)
    if (isinstance(raw, bool) or not isinstance(raw, (int, float))
            or not math.isfinite(raw)):
        if raw is not None:
            log.warning("llm.%s is not a number of seconds (%r) — using %ds",
                        key, raw, default)
        return default
    if raw < REVIEW_TIMEOUT_FLOOR_S:
        log.warning("llm.%s=%s is below the %ds floor — clamping; a window this "
                    "small times out every review", key, raw,
                    REVIEW_TIMEOUT_FLOOR_S)
        return REVIEW_TIMEOUT_FLOOR_S
    return int(raw)


def review_timeout_seconds(data: dict[str, Any]) -> int:
    """Wall-clock window for ONE gate review session, from raw config data."""
    return _timeout_knob(data, "review_timeout_seconds",
                         DEFAULT_CONFIG["llm"]["review_timeout_seconds"])


def code_review_timeout_seconds(data: dict[str, Any]) -> int:
    """Wall-clock window for ONE ``code_review``-mode session (bigger diff cap)."""
    return _timeout_knob(data, "code_review_timeout_seconds",
                         DEFAULT_CONFIG["llm"]["code_review_timeout_seconds"])


DEFAULT_CONFIG: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 8420},
    "worker": {
        # WHICH CODING BACKEND THE IMPLEMENTER RUNS ON. "claude" (the default)
        # is the Claude Agent SDK path and is unchanged in every respect — an
        # operator who edits nothing sees no behavioural difference from before
        # this key meant anything. "codex" routes the CODER, and only the coder,
        # to the OpenAI Codex CLI on the operator's own OPENAI_API_KEY.
        #
        # Planner, supervisor and utility stay on Claude regardless: those three
        # model tiers are pinned by ID in the project's non-negotiable constraints,
        # and the 2026-08-01 amendment that sanctioned a second backend moved none
        # of them. See `agent.backend.CLAUDE_PINNED_ROLES`. The reviewer defaults to
        # Claude too, but Settings can override it, disclosed on the task detail.
        "backend": "claude",
        # P2 (turn-cap convergence early-abort): a coder attempt can spend
        # its whole 500-turn budget (`bounds.max_turns_per_attempt`, left
        # UNCHANGED by this feature) still "doing things" — varied reads and
        # greps, never the same tool call twice — without ever converging on
        # a fix. `StuckDetector`'s doom-loop/edit-loop/ping-pong tiers never
        # fire on that shape (every call is "new" by their signature), so
        # nothing used to end it before the raw cap. This is the kill
        # switch: True (default) aborts the ATTEMPT — checkpointed, the
        # bounded loop retries with fresh context, exactly like a
        # `StuckAbort` — once past `convergence_check_after_turns` with no
        # file edit or test run in the last `convergence_window_turns`
        # turns. False reproduces today's behaviour exactly: only the raw
        # cap and the hard stuck tiers can end an attempt.
        "abort_non_converging": True,
        # See `core.bounds.ConvergenceTracker` for the defaults' full
        # justification: 80 clears normal up-front exploration, and 40 (half
        # of it) is long enough that a real edit/verify cadence never trips
        # it but short enough to catch a genuine stall tens of turns into
        # the 500-turn budget rather than at the very end of it.
        "convergence_check_after_turns": 80,
        "convergence_window_turns": 40,
    },
    "llm": {
        # Billing mode. "subscription" (the default) bills a Claude
        # subscription via CLAUDE_CODE_OAUTH_TOKEN and scrubs ANTHROPIC_API_KEY.
        # "api_key" (operator-authorized BYO-API-key, for friends/commercial
        # installs) bills the operator's own ANTHROPIC_API_KEY from .env instead;
        # every OTHER metered redirect is still scrubbed. Only the MODE lives in
        # config — the key itself stays in ~/.no_human/.env (never config.yaml,
        # enforced by _reject_api_key_in_config).
        "auth_mode": "subscription",
        # Which subscription pays for this process. "default" is the unsuffixed
        # CLAUDE_CODE_OAUTH_TOKEN in ~/.no_human/.env; any other name <p>
        # resolves CLAUDE_CODE_OAUTH_TOKEN_<P>. Read once at startup and
        # exported into the canonical variable, so one run can never span two
        # subscriptions. Change it with `nh auth use <profile>` and restart the
        # server — a live process keeps the token it started with.
        "auth_profile": DEFAULT_AUTH_PROFILE,
        "primary_model": "claude-sonnet-5",
        # 2026-08-11, OPERATOR INSTRUCTION ("revert the reviewer to 4.8"): the
        # Jul-26 move to claude-opus-5 was reverted on overdetermined evidence —
        # the operator's own same-day A/B scored 4.8 better (15/16 recall + 2/4
        # specificity vs 14/16 + 0/4), and opus-5-as-reviewer measured 3x round
        # duration (360s -> ~1078s), ~7x session cost from tool-call sprawl
        # beginning the day of the switch (2.9 -> 16.4 calls/run; still 13.2
        # after prompt bounds), and two no-verdict failure flavors (600s wall,
        # end_turn-without-verdict) that cost tasks their attempts. Confirm with
        # the recall measurement in eval/ (control set now 10). The README's
        # published catch-rate was measured on 4.8 and is consistent again.
        "review_model": "claude-opus-4-8",
        # Constraint amendment §6d (operator, commit 413d76f0d):
        # CLAUDE_PINNED_ROLES is now a DEFAULT pin set, not absolute — an
        # explicit per-role Settings choice overrides it. That choice lives at
        # `llm.role_backends.<role>: {backend, model}`, today wired for
        # "reviewer" only (see agent.backend.explicit_role_backend). The key
        # is deliberately ABSENT here — it is never a default value, only
        # something `set_role_backend` (this module, the one on-disk writer —
        # reached only through `core.role_backend_settings.
        # apply_role_backend_change`'s validation+availability-refusal layer,
        # never called directly by a route) splices in when an operator makes
        # an explicit choice in Settings. Its absence IS "use review_model
        # above, on Claude".
        # Wall-clock seconds granted to ONE reviewer session before it is cut
        # off. These are the walls, not budgets: the reviewer is bounded by
        # turns as well, and a round that dies on the wall halves the next one.
        #
        # Raised 600 -> 1500 / 1800 on 2026-08-11 because 600 sat BELOW the mean
        # review round once the reviewer tier moved to claude-opus-5 (measured:
        # ~1078s mean, 1357s worst, over 7 rounds; ~360s in the Jul 20–26
        # baseline week that 600 was sized for). Task b0a4eba1 lost both of its
        # rounds to "timed out after 600s" and escalated unreviewed. The full
        # measurement, the worst-case arithmetic and the reason a higher wall is
        # a real cost live at `review.reviewer._REVIEW_TIMEOUT` — and these two
        # numbers are pinned equal to that module's constants by
        # tests/test_config.py::test_the_review_window_defaults_have_exactly_
        # one_source_of_truth, which is what catches a drift between them.
        #
        # These do NOT choose the reviewer tier — that is an open operator
        # decision; re-measure with `nh bench report` under its reviewer recall
        # flag (not spelled literally: that string is a needle the
        # recall-corpus guard under tests/ forbids outside the CLI wiring).
        # Read them with `review_timeout_seconds(data)` /
        # `code_review_timeout_seconds(data)`, never straight off the dict: a
        # nonsense value falls back and an under-floor one clamps there.
        "review_timeout_seconds": 1500,
        # `code_review` mode reads a 120K-char PR diff — twice the gate's cap.
        "code_review_timeout_seconds": 1800,
        "planner_model": "claude-opus-5",
        # The supervisor is a sparse every-N-tool-calls course-corrector running
        # at effort="low", max_turns=1. It used to ride on review_model, so it
        # silently ran on Opus. It is a judging call on a short prompt, not a
        # reasoning-heavy one, and Sonnet 5 is enough for it — an explicit key
        # so the choice is visible instead of inherited.
        "supervisor_model": "claude-sonnet-5",
        # Utility tier: single-turn, effort="low", advisory jobs that summarize,
        # classify, or distill — never the implement/plan/review gates. Routing
        # these to Haiku frees the Opus window; a wrong answer here degrades a
        # hint, never a verdict. It is never the implementer, planner, reviewer
        # or supervisor — those four tiers are fixed above.
        "utility_model": "claude-haiku-4-5",
        # --- OpenAI Codex backend (only read when worker.backend == "codex") ---
        # Which of the two sanctioned Codex sign-ins pays for a run — see the
        # long comment above CODEX_API_KEY_VAR. Default "api_key" so no
        # existing install's behaviour changes (2026-08-22 amendment). The
        # sibling "subscription" mode holds no OpenAI credential at all and is
        # opt-in only, by an operator who has already run `codex login`.
        "codex_auth_mode": "api_key",
        # Chosen EXPLICITLY rather than derived from a Claude tier: the four
        # Claude IDs above are fixed by constraint and mean nothing to Codex,
        # so the Codex model gets its own key and its own default. Overriding
        # this is the supported way to move the Codex tier; nothing else here
        # changes when it does. None ⇒ resolve via `default_codex_model(mode)`
        # (agent/backend.py) — the default itself depends on codex_auth_mode,
        # since a ChatGPT-subscription session refuses codex-branded model
        # ids ("not supported when using Codex with a ChatGPT account") that
        # the api_key path accepts.
        "codex_model": None,
        # Codex's `model_reasoning_effort`. None ⇒ let the CLI use its own
        # default. The orchestrator's `effort=` ("low"/"medium"/"high") is
        # mapped onto this per call and takes precedence when it is set.
        "codex_reasoning_effort": None,
        # Absolute path to the `codex` binary, for installs where it is not on
        # PATH. None ⇒ resolve it the way the CLI itself is normally found.
        "codex_cli_path": None,
        # Whether the codex CODER's workspace-write sandbox is granted network
        # access (git fetch/push, gh, pip). Without it the CLI's sandbox blocks
        # the network outright — measured 000 vs a 200 control on codex-cli
        # 0.149.0. This grants NETWORK ONLY: `workspace-write` still confines
        # writes to the workspace, and an independent review re-measured the
        # file boundary as byte-for-byte identical with and without this key
        # (writes to $HOME, /private/var/tmp and /usr/local are denied either
        # way). An earlier version of this comment said the coder's file access
        # was "already unsandboxed at the FILE level"; that was false and
        # contradicted docs/BACKENDS.md in the same commit. Default True: an
        # operator who changes nothing gets the network the Claude backend
        # already has.
        # False opts back into the CLI's own network-free default — never
        # forwarded when the session is readonly (a read-only session gets
        # `--sandbox read-only`, which has no `sandbox_workspace_write`
        # table to attach this key to in the first place). This key never
        # widens the FILE sandbox and never touches `danger-full-access`.
        "codex_network_access": True,
        # --- Local model backend (only read when worker.backend == "local") ---
        "local_model": None,        # model id the local server exposes
        "local_base_url": None,     # e.g. http://localhost:8000 — REQUIRED in local mode
        "local_cli_path": None,     # None ⇒ the SDK-bundled CLI
        # MoA (Mixture-of-Agents) planning fan-out — on by default. Runs N
        # independent plan proposals from different angles, then ONE
        # aggregator call synthesizes a single plan (evidence-based synthesis,
        # never a numeric score). Reuses planner_model; no new model tier
        # introduced. Only the (cheap) planning step is affected —
        # never the implement/review loop. Set enabled=False to fall back to
        # a single planner call.
        "moa_planning": {
            "enabled": True,
            "proposers": 3,
            # Complexity gate (B2). Measured on task 61406d02, one MoA plan cost
            # 13210 + 12027 + 14493 proposer tokens + 10796 aggregator ≈ 50.5K
            # Opus tokens; the single-planner path on d9d458b5 cost 3.2K — ~16×.
            # Worse, a trivial task only discovers it is trivial after all three
            # proposers have answered SKIP_PLAN. So fan out only when the task
            # shows at least `min_signals` of the pre-plan complexity signals
            # (see orchestrator._moa_complexity_signals). Set min_signals to 0
            # for unconditional MoA, or enabled=False for none.
            "min_signals": 2,
            "criteria_threshold": 5,      # acceptance criteria ≥ this = complex
            "description_threshold": 2000,  # spec chars ≥ this = complex
        },
    },
    "database": {"path": str(DB_PATH)},
    "notifications": {
        # Write-only webhooks, alert channel only. Read context uses separate
        # read-only tokens (Phase 1). None disables the channel (logs instead);
        # with both None, notifications are logged and nothing is sent.
        "slack_webhook_url": None,
        # Microsoft Teams, via a Power Automate "Workflows" webhook. NOT the
        # classic Office 365 connector — Microsoft disabled those between
        # 2026-05-18 and 2026-05-22 and they no longer function, so
        # notify/teams.py refuses a connector URL loudly instead of posting
        # into a dead endpoint. Create one in Teams: Workflows app → "Post to a
        # channel when a webhook request is received". The URL carries its own
        # SAS credential in the query string — treat it as a secret (it is
        # scrubbed from /api/config like every other *webhook* key).
        "teams_webhook_url": None,
        # Where a human should click through to. Rendered as the Teams Adaptive
        # Card's single "Open in no_human" button (Action.OpenUrl) — the button
        # that card format was chosen for. NOT a secret and not scrubbed. Left
        # None because the board binds 127.0.0.1 by default and a localhost
        # link is dead on the phone these alerts are read on; set it only when
        # the board is actually reachable from where Teams is read.
        "board_url": None,
        "email_to": "dev@example.com",
    },
    "updates": {
        # A once-a-day check against PyPI's public JSON API that prints a single
        # line when a newer `nh` has been published. It never blocks a command
        # (the fetch runs on a daemon thread and the notice is rendered from the
        # previous run's cache) and never fails one. Set false — or export
        # NH_NO_UPDATE_CHECK=1, which also covers CI — to turn it off entirely.
        # No telemetry: this is an outbound GET for a version string, and
        # nothing about the machine or the operator is sent.
        "enabled": True,
        "interval_seconds": 86400,
    },
    "approval": {
        "require_before_merge": True,   # ALWAYS true — agent never merges
        "auto_merge_on_approval": False,  # there is no auto-merge
        "approval_timeout": "24h",
    },
    # `nh approve` merges the PR itself (operator directive 2026-08-12): a
    # local squash under git.approve_identity, the manifest merge-result
    # ledger rule, then a push to the default branch. This is the ONE human
    # merge action (constraint #2's `approve IS the human merge action`) — the
    # agent never merges on its own, still and always. `enabled: False`
    # reverts to the pre-2026-08-12 record-only behaviour (approval is
    # recorded, the human merges the PR in their git host by hand).
    "approve_merge": {
        "enabled": True,
        "test_timeout_seconds": 1800,
        # Timeout for the FULL suite, run instead of the focused (change-
        # scoped) gate when the squash result's tree diverges from (or is
        # unknown relative to) the attempt's recorded tested tree — conflict
        # rounds/a moved base mean the attempt-time full-suite evidence no
        # longer describes what's about to land (vcs/approve_merge.py
        # `_decide_gate`). Longer than `test_timeout_seconds` because it's the
        # whole suite, not a change-scoped slice.
        "full_test_timeout_seconds": 5400,
    },
    "git": {
        "branch_prefix": "no-human/",
        "commit_prefix": "",
        "never_push_to": ["main", "master", "release/*"],
        # Extra GitHub Enterprise hosts treated as GitHub (github.com is always
        # recognized). Add your GHE host (e.g. "code.example.com") to open real PRs.
        "github_hosts": ["github.com", "code.example.com"],
        "agent_identity_name": "no_human",
        "agent_identity_email": "no-human@acme.com",
        # The identity the ONE commit `nh approve` makes when it squash-lands a
        # PR is attributed to (vcs/approve_merge.py). Distinct from
        # agent_identity_* above on purpose: that commit is a human merge action
        # (constraint #2), never the agent's, so it must never carry the agent's
        # name/email. Left EMPTY on purpose: when unset, the identity is resolved
        # from git's own configuration for that repo (repo-local overriding
        # global) — the same identity a plain `git commit` there would use. Set
        # these to override per install. If neither is set nor resolvable, `nh
        # approve` refuses rather than inventing one.
        "approve_identity": {"name": "", "email": ""},
        # Flat-key aliases for the same merge identity, read by
        # `_resolve_approve_identity` with LOWER precedence than
        # `approve_identity` (which wins if both are set) and HIGHER
        # precedence than the repo-local `git config` resolution. Left EMPTY
        # on purpose, same default-from-git-config behaviour as
        # `approve_identity` above — set these instead of `approve_identity`
        # when a flat pair of keys is more convenient to template/override.
        # Deliberately NOT a fallback to `agent_identity_name`/`_email`: an
        # unresolvable identity still refuses at `preconditions` (constraint
        # #2 — the merge commit must never be attributed to the agent).
        "merge_identity_name": "",
        "merge_identity_email": "",
    },
    "safety": {
        # No size cap by default. A line/file count is a proxy for "scope
        # explosion" that cannot tell a legitimately large change (a 645-line
        # Jenkinsfile stage) from a runaway refactor, and the check runs AFTER the
        # commit — so it saves no compute, it only stops lint, tests, the reviewer
        # and the PR from ever running. The real scope guards are semantic and
        # already in place: the plan's FILES TO CHANGE list (agent/scope_guard.py),
        # the tamper guard, the evidence-based reviewer, and the human who approves
        # the PR. Set either key to a positive int to opt back in per install; a
        # task may raise its own via task.config (blockers/actions.py).
        "max_files_changed": None,
        "max_lines_changed": None,
        "forbidden_paths": [".env", "secrets/", "*.key", "*.pem"],
    },
    "pipeline": {
        # Proportionality (2026-08-09). Measured: a one-line edit to a markdown
        # file took 35+ minutes of intake grill → 9-turn Opus planning → 9
        # skills → multi-stage review, while the complexity gate had already
        # (correctly) computed "tier simple" and nothing downstream read it.
        # ON: a task whose file set is ≤2 non-executed prose files skips the
        # grill, plans on the utility model in ≤2 turns, loads no discovered
        # skills, and gets a BOUNDED (not skipped, not weakened) review; it
        # escalates back to full ceremony the moment the plan or the actual
        # diff leaves that file set. OFF: exactly the pre-2026-08-09 pipeline.
        # What this never touches: the review gate itself, the tamper guard,
        # the export gate, and the human merge.
        "trivial_tier": {"enabled": True},
        # Review depth scales with diff size (2026-08-14). A gate review of a
        # diff at or under `max_diff_lines` changed (added+deleted) lines
        # runs SINGLE-TURN, no tools: the diff, the full text of every changed
        # file, lint and wiring evidence are already in the prompt, so the
        # exploration turns buy nothing. A diff containing a risk-flagged
        # pattern — a guard/scrub function touched (by path OR by content), a
        # deleted/renamed-away test file, or a security-sensitive path —
        # ALWAYS gets the full multi-round review regardless of size; so does
        # a diff too big to measure (binary) or a re-review after a prior
        # round failed. See `core/review_routing.py`. `enabled: false`
        # restores the pre-2026-08-14 behaviour (every gate review is full).
        "review_routing": {"enabled": True, "max_diff_lines": 200},
    },
    "feasibility": {
        # Off-switch for the pre-flight card's HINT-ONLY signal families (e.g.
        # `multi_family`, `core/complexity.py:hint_signals`) — extra
        # transparency the card shows beside the tier's own signals. Never
        # feeds back into `compute_tier`'s MoA/thinking gates; false just
        # narrows the card to exactly those legacy signals.
        "hint_signals_enabled": True,
    },
    "planning": {
        # Plan-first worker (Phase 1): generate a detailed implementation plan
        # before the implement loop. Sonnet explores the codebase and writes a
        # plan the Opus worker follows. Skipped for code_review tasks.
        "enabled": True,
        "max_turns": 10,
    },
    "bounds": {
        # Must stay in step with core.bounds.Bounds' field defaults — the one
        # place the rationale for each number lives. The guard that catches drift
        # is
        #   tests/test_run_84251cb2_regressions.py
        #     ::test_bounds_defaults_have_exactly_one_source_of_truth
        # which iterates DEFAULT_CONFIG["bounds"] and asserts each key equals
        # getattr(Bounds(), key) — except max_correction_rounds, which the test
        # exempts (WAKE_ONLY) because Bounds carries no such field, and which
        # blockers/wake.py duplicates as a hardcoded fallback, so THAT number is
        # guarded by nothing and drifts silently.
        #
        # It is NOT tests/test_bounds.py, which this comment used to name and
        # which never reads DEFAULT_CONFIG at all. That mattered: changing
        # max_attempts here and running the named file gives 28 passed, so an
        # editor who does exactly what the comment says learns nothing. A pointer
        # to a guard is itself an unguarded claim — verify by breaking the value
        # and seeing which test dies, not by reading.
        "max_attempts": 3,
        "max_turns_per_attempt": 500,
        "max_correction_rounds": 2,
        # Megaplan P3: complex tasks (>4 files / large plan / decompose verdict)
        # get max_turns_per_attempt × this, so they don't exhaust turns
        # mid-implementation and fail with an empty diff (B5). 1.0 disables.
        "complex_multiplier": 1.5,
        # Lifetime caps across the task's WHOLE life, resumes included.
        # max_attempts bounds one loop, but every resume starts a fresh loop:
        # task 84251cb2 reached attempt 17 and 21.2M cache-read tokens with no
        # cap ever firing. Exceeding either cap raises a BUDGET_EXHAUSTED
        # blocker — an honest park; the human raises the budget or abandons.
        # Both are per-task overridable via task.config (the option's action).
        "lifetime_attempts": 9,
        # COST-WEIGHTED tokens, not raw ones: fresh in/out x1.0, cache write
        # x1.25, cache read x0.1 (core.pricing). 4M replaces the converted
        # 1.6M cap, which was calibrated on a ledger whose subagent spend was
        # under-counted (~17%-visible gauge, since fixed) — against honest
        # numbers 1.6M parks 117/221 real tasks (52.9%); 4M parks 6.8% and
        # sits at the knee. Full derivation and the post-baseline re-sweep
        # obligation live on core.bounds.Bounds; kept in step with it.
        "lifetime_tokens": 4_000_000,
        # Per-attempt spend cap — ends the ATTEMPT (bounded loop retries),
        # never parks the task. Raised with the lifetime cap (2:1 shape).
        # Rationale on core.bounds.Bounds.attempt_tokens.
        "attempt_tokens": 2_000_000,
        # The floor the loop-head startup gate refuses to start an attempt
        # under. Rationale on core.bounds.Bounds.min_viable_attempt_weighted_tokens.
        "min_viable_attempt_weighted_tokens": 250_000,
    },
    # A separate section from `bounds` on purpose: `bounds` is mirrored
    # key-for-key by core.bounds.Bounds and guarded by
    # tests/test_run_84251cb2_regressions.py::
    # test_bounds_defaults_have_exactly_one_source_of_truth, which asserts every
    # key there has a Bounds field. This is policy, not a bound.
    "budget": {
        # An exhausted lifetime budget ENDS the task (status `failed`) with a
        # structured record and a wake condition naming what a human would have
        # to do, instead of asking the human "spend more, or stop here?".
        #
        # Default ON because the answer never varied. Measured 2026-08-09: of
        # 119 parked tasks awaiting a human, 69 were this one question, and the
        # operator's standing rule is "the answer is STOP. NEVER RAISE A CAP.
        # Budget raises have never once produced a merge on this project. An
        # exhausted budget means the TICKET is wrong — answer stop, then rewrite
        # it inline-complete and re-file." A question whose answer is invariant
        # policy is the product's problem, not the operator's.
        #
        # This changes the OUTCOME, never the CAP: the caps stay in `bounds`,
        # resume spend still counts against them, and raising one is still a
        # human-only act (`nh task config <id> lifetime_tokens=N`).
        #
        # False restores the old behaviour exactly — ESCALATED, with the
        # question and the raise/stop options — for an operator who would
        # rather be asked.
        "exhaustion_terminal": True,
    },
    # The nightly funnel eval (Phase C). The only knob it has: a run REFUSES
    # to start when the corpus's own ceiling sum exceeds this, so an unattended
    # 03:00 job cannot be authorised to spend more than the corpus was designed
    # to cost. The default IS that sum (400k + 1.5M + 3M + 4M + 2M across the
    # five tiers), weighted exactly as `bounds.lifetime_tokens` is weighted —
    # so out of the box the guard permits the corpus and nothing more. Raise it
    # only with a corpus that justifies the raise; `tests/test_funnel_eval.py
    # ::test_the_default_budget_is_the_corpus_ceiling` is the drift guard, and
    # it recomputes the sum from the corpus rather than restating it.
    "eval": {
        "nightly_budget_tokens": 10_900_000,
    },
    "bounds_investigation": {
        "max_attempts": 8,
        "max_turns_per_attempt": 80,
        "max_correction_rounds": 4,
    },
    "repro_gate": {
        # The reproduction-test gate (M2): "off" | "advisory" | "required".
        #   off      — never runs.
        #   advisory — runs and reports for every kind, and ENFORCES for a
        #              bugfix whose edits IN THIS ATTEMPT touched .py (the
        #              agent-edit hook's Write/Edit/MultiEdit/NotebookEdit
        #              events, reset per attempt — NOT the branch diff. So a
        #              .py edit made through bash/sed/python -c is invisible to
        #              it, as is a resumed attempt that edits only JS while the
        #              shipped diff touches Python): a
        #              "fail" (a manifest exists but doesn't reproduce the bug
        #              on the unfixed code) fails that attempt and sends it
        #              back immediately. "waived" (no manifest at all) is a
        #              missing artefact, not failed code: it buys ONE bounded
        #              corrective round on the SAME branch to write the
        #              manifest (`Orchestrator._repro_corrective_round`)
        #              before the attempt fails — only a second non-pass
        #              verdict sends it back. Non-Python and non-bugfix
        #              changes stay report-only, so a JS/CSS bugfix is never
        #              asked for a pytest repro.
        #   required — enforces for every kind and every change.
        # advisory is NOT passive. It was when this default was written; the
        # bugfix carve-out (orchestrator: `enforced = ...`) made it partly
        # enforcing, and this comment still claimed otherwise until 2026-07-22.
        "mode": "advisory",
    },
    # UI evidence (testing/ui_evidence.py): the harness drives a real browser
    # at the attempt's own dev server from a coder-written walk manifest,
    # after this attempt's tests pass (D1.2, 2026-08-31). `enabled` is a
    # three-state switch, read by `ui_evidence_should_run` below — the
    # egress allowlist charges the browser channel against this key. `None`
    # (the default) means "decide per attempt from the diff": ON for a
    # change that touches `web/**`/`desktop/**` (or a repo-declared
    # `ui_paths` glob), OFF otherwise. `True`/`False` are an operator's
    # explicit override, forcing every attempt the same way regardless of
    # the diff.
    "ui_evidence": {"enabled": None},
    "tamper_adjudication": {
        # When the test-tampering guard fires, ask ONE fresh-context reviewer
        # whether the ticket REQUIRED those test changes, instead of ending the
        # task on a human's desk in the guard's own counter jargon.
        # Operator-directed, 2026-08-09.
        #
        # ON BY DEFAULT, and the reasoning is worth keeping next to the switch:
        # the guard's DETECTOR is unchanged and still absolute, and every
        # unresolved outcome still stops the run (a TAMPERING verdict costs a
        # bounded attempt, a second one parks, and any doubt at all parks). The
        # only new outcome is "the ticket asked for this, here is the criterion,
        # printed on the PR" — which is strictly more information than the
        # escalation it replaces, in front of the same human.
        #
        # false restores the pre-2026-08-09 behaviour byte for byte: every fire
        # escalates immediately with the raw findings. It exists for an operator
        # who wants no LLM in this path at all, and because a feature that
        # changes what a SAFETY gate does should be answerable with a config
        # line rather than a revert.
        "enabled": True,
    },
    "context": {
        # Repo-map seed (M3): a ~3K-token map of the repo in the coder prompt
        # to cut exploration turns. Cached per (repo, HEAD). Off = fall back to
        # pure agentic exploration.
        "repo_map_enabled": True,
        # Retry-cost class: attempt N>1 gets a distilled state doc (what was
        # tried, what failed, the diff so far, review findings, remaining
        # criteria) INSTEAD of re-accumulating the repo map and gathered-
        # context digest every attempt. False restores the pre-change prompt
        # byte-for-byte — every attempt re-accumulates, as before this switch
        # existed.
        "attempt_state_distill_enabled": True,
    },
    # C1 seed-context diet: user-level (~/.claude/skills) skills are delivered
    # to the coder only when relevant to the task (token overlap on title/
    # description/repo path). Project-level and DB-confirmed skills are always
    # delivered. False = deliver every user skill on every task.
    "filter_user_skills": True,
    "blockers": {
        # Part 22 blocker handling.
        "max_park_duration": "48h",
        "wake_poll_interval": "10m",
        "escalate_on_low_confidence_below": 0.6,
        # Escalation-quality gate (blockers/challenge.py): the judgment-call
        # categories (AMBIGUITY / NOVEL_UNKNOWN / IMPOSSIBLE) get ONE
        # supervisor-checked challenge per task before parking a deliverable
        # task; external categories and every second blocker are honored
        # untouched. Never converts a park into "done" — a resolvable verdict
        # costs the attempt and re-enters the bounded loop under a documented
        # reversible assumption.
        "challenge": True,
        # PR comments from these authors never trigger a revision ("[bot]"
        # logins are always ignored on top). A CI service account that posts a
        # test-results table on every build is the shape this exists for:
        # treated as operator feedback, it burns an attempt per PR. The
        # default names none — set yours in `blockers:`. NOTE: a user-yaml `blockers:` section
        # replaces this map wholesale, so wake.py carries the same default.
        "ignore_comment_authors": [],
        "max_ci_fix_rounds": 3,
        # "enforce" (default): a red PR check counts a fix round and can
        # escalate past max_ci_fix_rounds. "advisory": record the red, never
        # act on it — a TEMPORARY operator override for a private repo whose
        # Actions quota is exhausted (set in ~/.no_human/config.yaml
        # 2026-08-12; REMOVE at go-public, where Actions minutes are
        # unlimited and CI is meaningful again).
        "pr_ci_policy": "enforce",
        # Bounded CI_GATE-failure → fix cycles on an open PR (M6), counted per
        # distinct failure signature like max_ci_fix_rounds; past the cap the
        # failing job is escalated to the human.
        "max_ci_gate_fix_rounds": 3,
        # Stuck-active watchdog: a task emitting NO event for this many minutes
        # while in an active state (implementing/reviewing/testing/planning/
        # context) is escalated as a probable hung Agent-SDK session (the
        # 2026-07-11 reviewer hang). 40 > the 30-min run_tests timeout so a
        # long test never trips it; 0 disables. wake.py mirrors this default
        # (the deep-merge trap: a user `blockers:` block replaces this map).
        "stuck_active_minutes": 40,
    },
    "supervisor": {
        # In-flight human-replacement (EVOLUTION_PLAN Phase 1). The PostToolUse
        # hook evaluates every `check_every` calls (NOT 1 — per-call LLM ≈ 8× cost
        # and serializes every action; see §1.2). preflight runs one plan check
        # before the first edit (skipped for trivial tasks via SKIP_PLAN gate).
        "enabled": True,
        "check_every": 5,
        "preflight": True,
    },
    "reviewer": {
        # Independent staff reviewer (EVOLUTION_PLAN Phase 2). 3-pass prompt,
        # pass/fail with cited evidence only — never a numeric score (constraint
        # #3). feedback_rounds reuses the bounded attempt loop, then escalates.
        "passes": ["correctness", "architecture", "edge_cases"],
        "feedback_rounds": 3,
        # When no reviewer is wired, the gate FAILS CLOSED (the task escalates).
        # It used to return a passing decision, which made the one hard gate a
        # silent rubber stamp. Set true only for eval/replay flows that skip the
        # gate on purpose; even then the skip is announced on the board.
        "allow_advisory": False,
    },
    "review": {
        # no-human-67: the independent reviewer's checklist (verdict, rounds,
        # every finding with severity and file:line), posted ONCE as its own
        # PR comment via the same idempotent `post_to_pr_once` marker
        # discipline as the verification-receipts comment. True by default —
        # the whole point is that a PR reader sees the checklist without
        # digging into the DB. False skips posting (an event still fires).
        "post_checklist_comment": True,
    },
    "onboarding": {"completed": False},
    "profile": {
        # Megaplan P1 (full autonomy). By default a profile drives a task only
        # after a human confirms it (ProjectProfile.is_usable). These opt-in
        # flags let an unattended deployment run without that click:
        #   auto_confirm_proven — trust a profile whose test_cmd was PROVEN to
        #     run clean (exact command exited 0 in a real subprocess), even if a
        #     human never confirmed it. Proof, not a click, is the safety signal.
        #   auto_onboard — if a task's repo has no usable profile, derive+prove
        #     one inline before the first attempt (best-effort; never blocks).
        "auto_confirm_proven": False,
        "auto_onboard": False,
    },
    "isolation": {
        # WHERE one task runs. Every task gets its own throwaway git worktree,
        # so the agent's working tree is never the checkout the operator is
        # sitting in. On by default and independent of `concurrency` below —
        # the two were one flag, and because parallelism defaults off, the
        # default run used the live checkout and could overwrite uncommitted
        # work. Set false to deliberately run in the primary checkout; a run
        # then edits whatever is in it.
        "enabled": True,
        # Where the per-task worktrees live. None → ~/.no_human/worktrees.
        # `concurrency.worktree_root` is still read for configs written before
        # the split.
        "worktree_root": None,
    },
    "concurrency": {
        # HOW MANY tasks run at once. Phase 7: `nh serve` drains the queue into
        # a bounded asyncio pool. Default off → one task at a time. Parallelism
        # requires `isolation.enabled` (workers sharing one checkout would stomp
        # each other's index and branch), so the pool refuses rather than
        # downgrading when isolation is opted out.
        "enabled": False,
        "max_workers": 2,
        "poll_interval": "10s",   # how often `nh serve` checks for new pending tasks
        # Seconds a stopping server waits for running attempts to checkpoint
        # ([WIP-PARTIAL] + resume_from) and unwind before exiting anyway.
        # `nh stop --timeout` defaults to this plus 15.
        "stop_grace_s": 60,
        "worktree_root": None,    # pre-split alias for isolation.worktree_root
    },
    "decomposition": {
        # TOMBSTONE (2026-08-12, operator decision A1): the LeadAgent
        # child-task decomposition subsystem this gate re-enabled has been
        # DELETED — `no_human.core.lead_agent` no longer exists. Intake-level
        # decomposition (the split-proposal advisory) and in-session
        # delegation via SDK sub-agents replace it; a task never spawns
        # child tasks. The key stays only so `enabled: true` in an old
        # config.yaml fails loudly instead of silently doing nothing — see
        # `_reject_decomposition_enabled`, called from `load_config`.
        "enabled": False,
    },
    "ci": {
        # The install-wide FALLBACK. `Orchestrator._resolve_ci_runner` reads
        # this block when the project profile names no pipeline target; the
        # profile wins when it does, because it describes one repo and this
        # describes every repo the install touches. Read that method for the
        # precedence rules and docs/configuration.md for the user-facing
        # version. Until 2026-08-02 nothing read this at all — a user who
        # configured CI exactly as documented got no gate and no diagnostic —
        # so if you are changing the resolver, that is the regression to avoid.
        #
        # Opt-in per project. Set enabled=true and provide project path.
        "enabled": False,
        "backend": "gitlab",      # gitlab | github_actions | jenkins | circleci
        # The pipeline target, read by every backend: "namespace/repo"
        # (gitlab), "owner/repo" (github_actions / ghe_checkruns) or the
        # CircleCI API v2 project slug "<vcs>/<org>/<repo>", e.g. "gh/acme/svc".
        "project": "",
        "hostname": "gitlab.acme.net",
        "variables": {},          # extra pipeline variables (sent as the POST body's variables array)
        "timeout_minutes": 60,
        "max_infra_retries": 2,   # infra failures only: retry after 2 min, max 2
        "poll_interval": 30,
        "result_parser": "pytest",  # or "surefire" for Maven projects
        # --- Jenkins backend (build.example.com) ---
        # job: full job path to the branch/PR job, e.g.
        #   "job/acme-universe/job/acme-core-test-master/job/PR-042"
        "job": "",
        "base_url": "https://build.example.com",
        # mode: watch (DEFAULT, read-only poll of the PR-triggered build) |
        #       trigger (POST buildWithParameters — outward-facing, opt-in) |
        #       human_gated (a person must build the image first — park-and-wake)
        "mode": "watch",
        # Credentials are NEVER stored here. The backend reads JENKINS_USER /
        # JENKINS_API_TOKEN from ~/.no_human/.env (chmod 600) or the process env.
        "wake_hint": "",
        # auth: "token" (basic auth, DEFAULT) | "cookie" (form-login session
        # cookie). CloudBees build.example.com rejects API-token basic auth, so
        # it needs "cookie": a one-time Playwright form login (SSO_USERNAME /
        # SSO_PASSWORD in ~/.no_human/.env) captures a session that is reused
        # headlessly and auto-refreshed on expiry.
        "auth": "token",
        # crumb_path: CSRF crumb issuer, relative to base_url, used for POST
        # (trigger mode) under cookie auth. For CJOC controllers this is
        # "cjoc/crumbIssuer/api/json".
        "crumb_path": "crumbIssuer/api/json",
        # storage_state_path: where the Playwright session is persisted. Null =>
        # ~/.no_human/jenkins_storage_state.json.
        "storage_state_path": None,
        "cookie_auto_refresh": True,
    },
    "integrations": {
        # First-class integration config. github/gitlab/jenkins/slack are NOT
        # here — their status is a read-only VIEW over ci.* / notifications.*
        # (one source of truth per setting). Tokens live in ~/.no_human/.env,
        # never in this world-readable file.
        "jira": {
            "enabled": False, "site": "", "project_key": "", "jql": "", "email": "",
            # JIRA_API_TOKEN in ~/.no_human/.env
            "default_repo": "",       # where polled-in tasks run
            "write_back": False,      # opt-in: comment on status change (never transition/close)
            "poll_interval": "5m",    # floor 60s enforced at the serve hook
        },
        "linear": {
            # Polled issue intake, same role as `jira` above (a server-side
            # poller, not an argument to `nh task add`). LINEAR_API_KEY lives
            # in ~/.no_human/.env, never in this world-readable file.
            "enabled": False,
            "team_key": "",           # e.g. "ENG" — the prefix in ENG-123
            # WorkflowState.type values to pull in. Linear's seven documented
            # types are triage/backlog/unstarted/started/completed/canceled/
            # duplicate; the default takes only work nobody has started.
            "state_types": ["triage", "backlog", "unstarted"],
            "label": "",              # optional: only issues carrying this label
            "default_repo": "",       # where polled-in tasks run
            "write_back": False,      # opt-in: comment + type-matched state move
            "poll_interval": "5m",    # floor 60s enforced at the serve hook
        },
        "monday": {
            # Polled item intake, same role as `jira`/`linear` above.
            # MONDAY_API_TOKEN lives in ~/.no_human/.env, never in this
            # world-readable file.
            #
            # THE ONE REAL DIFFERENCE FROM JIRA AND LINEAR, and why this block
            # looks nothing like the one above it: Jira and Linear expose a
            # TYPED workflow state, so "pull the backlog" means the same thing
            # on every workspace. monday does not — a status column is a bag of
            # user-defined labels ("Ready for Dev", "Fixing", "Known Bug", ...)
            # that differs per board, and nothing in the API says which of them
            # means "not started yet". So the label→meaning mapping is stated
            # HERE, explicitly, and is never inferred from label text or colour.
            # With board_id/status_column unset the adapter RAISES rather than
            # returning nothing, because a silent empty result is
            # indistinguishable from an empty board.
            "enabled": False,
            "board_id": "",           # which board to pull from (numeric id, as a string)
            "status_column": "",      # the status column's ID, e.g. "bug_status" —
                                      # NOT its title. Discover with:
                                      #   boards { columns { id title type } }
            "todo_labels": [],        # labels meaning "not started yet", e.g. ["Ready for Dev"]
            "in_progress_label": "",  # optional: label to move to when work starts
            "done_label": "",         # optional: label to move to on completion
            "default_repo": "",       # where polled-in tasks run
            "write_back": False,      # opt-in: update (comment) + status-label move
            "poll_interval": "5m",    # floor 60s enforced at the serve hook
        },
        # No `circleci` block. It held `enabled` + `org_slug` + `project` and
        # NOTHING read any of the three: the CI layer builds CircleCICI from
        # `ci.project` (the API v2 project slug), and `ci.enabled` is the only
        # switch that turns a CI gate on. So the block rendered an onboarding
        # form and an on/off toggle that governed nothing, while the panel told
        # the operator CircleCI was their active CI backend and no gate ran.
        # CircleCI is configured in the `ci:` block above, exactly like
        # github_actions / gitlab / jenkins. An older config that still carries
        # this block loads fine (unknown keys are merged, not rejected) and is
        # reported unconfigured with a re-save nudge — see
        # `integrations._CIRCLECI_LEGACY_DETAIL` for why it is NOT auto-promoted.
        "slack": {
            # Opt-in Socket-Mode intake worker (SCRUM-60/61/62 split). Default
            # OFF: no worker starts and no import-time side effects occur.
            # SLACK_BOT_TOKEN / SLACK_APP_TOKEN live in ~/.no_human/.env only —
            # never in this world-readable file.
            #
            # This block's switch is `intake`, not `enabled` — there is no
            # `integrations.slack.enabled` key, deliberately: `enable_field()`
            # falls back to a block's single bool key when it has no `enabled`
            # key of its own, and `intake` is that key. Adding an `enabled`
            # key here would rebind the switch away from `intake` and break
            # the Socket-Mode worker toggle. The `enabled: None` tri-state
            # elsewhere in this registry belongs only to the `ci.*` views
            # (github/gitlab/jenkins/circleci), which have no switch of their
            # own — it does not apply to slack, whose switch (`intake`) is a
            # concrete bool, defaulting False.
            "intake": False,
        },
        "teams": {
            # Microsoft Teams notify-OUT (notify/teams.py), the write-only
            # sibling of notify/slack.py. Until this block existed, Teams was
            # the one integration in the registry (integrations/__init__.py
            # `_ORDER`) with NO config block at all, so nothing could offer it
            # to a user and it could only be reached by hand-editing YAML.
            #
            # The webhook URL is deliberately NOT duplicated here: it stays at
            # `notifications.teams_webhook_url`, where notify.build_notifier
            # already reads it — one source of truth per setting. That URL
            # carries its own SAS credential (`sp`/`sv`/`sig`) in the query
            # string, so it is a SECRET and is never collected by onboarding.
            #
            # `enabled` is a mute switch: it turns the channel off without
            # making the operator delete a webhook they pasted. Honoured by
            # notify.build_notifier. Default True, so an install that already
            # has a webhook keeps delivering byte-for-byte as before — a False
            # default here would silently stop existing Teams alerts.
            "enabled": True,
        },
    },
    "ci_gate": {
        # M6: post-PR CI_GATE integration validation, run as a WakeWatcher rung
        # (blockers/wake.py) once the PR's normal CI is green. Deploys the service +
        # runs the integration tests on the GitLab pipeline project in a
        # throwaway per-PR namespace — NEVER a prod environment. Trigger is a
        # subprocess to `glab` (operator's local auth), not the Agent SDK.
        # Every value below is deliberately EMPTY: this block describes one
        # deployment's private CI topology (project ids, cluster names, job
        # paths), which must never ship inside the product — a packaged build
        # serves the effective config over /api/config, so anything left here
        # is readable by whoever installs it. An operator using this gate fills
        # these in via ~/.no_human/config.yaml on their own machine.
        "enabled": False,
        "project_id": None,  # GitLab numeric project id of the pipeline project to trigger
        "hostname": "",
        "ref": "main",
        # Repos governed by this gate, matched against the PR's repo name.
        # Empty list = gate never fires even when enabled. NOTE: a user-yaml
        # `ci_gate:` block replaces this list wholesale (deep-merge trap), so
        # consumers must treat a missing/empty list as "no match", never crash.
        # gate.py guards on `not enabled or not repos or not project_id`, so the
        # empty defaults below are inert rather than a crash.
        "repos": [],
        # Throwaway namespace, one per PR. Checked for collisions pre-trigger.
        # Must contain `{pr_number}`.
        "namespace_template": "ci-gate-pr{pr_number}",
        # The pipeline variable that carries the namespace. Must match what the
        # operator's pipeline actually reads.
        "namespace_variable": "CI_GATE_NAMESPACE",
        # Static variables sent with the pipeline trigger. Deployment-specific:
        # the operator supplies whatever their pipeline requires. Namespace and
        # image variables are injected at run time.
        "variables": {},
        "poll_interval": 30,
        # Cluster used to resolve latest_dev images (ci_gate/images.py) and to
        # check namespace collisions before triggering. kubectl only — the
        # registry API 401s and the enrich job is a separate (Part A) path.
        "kubeconfig": "",
        # Part A: code PRs get an image built FROM the PR via a Jenkins enrich
        # job, triggered externally with the operator's own SSO Basic auth
        # + session-scoped crumb — NEVER the Jenkins credential store.
        # pr_build=False turns code PRs into honest escalations instead.
        "pr_build": True,
        "enrich_job_url": "",
        "jenkins_controller": "",
        "jenkins_ca_bundle": "",  # PEM the CI-log fetch verifies against; "" = system store
        "registry_prefix": "",
    },
    "hooks": {
        # Per-edit lint feedback (agent/lint_hook.py): after each Edit/Write,
        # lint the changed file and inject hard errors straight back into the
        # session. SWE-agent's single biggest ACI win (arXiv 2405.15793) —
        # a non-parsing edit costs one hook round instead of a whole failed
        # attempt. ON by default (W1.3): deterministic, no LLM cost, no-op
        # unless the repo has a confirmed lint command, fail-open on linter
        # timeout/absence (lint_hook.py: `not result.ran → {}`).
        "per_edit_lint": True,
    },
    "docs": {
        # M-A: the local, in-house repo wiki (docs_gen). Generated by the
        # existing Claude backend into <repo>/.no_human/wiki/ (commit-excluded,
        # never sent to any third party) and provided to the agent /
        # no_human_researcher as an on-demand reference.
        # `nh docs generate <repo>` is always available; these keys gate ONLY
        # the background WikiRefreshJob in `nh serve`. Default off → no
        # unattended backend cost until you opt in.
        "auto_refresh": False,
        "refresh_interval_seconds": 3600,  # HEAD-diff check cadence when serving
        "max_turns": 12,                   # bound the read-only recon session
    },
    # The team-brain client (src/no_human/brain/). THREE keys, and this is the
    # whole of its configuration surface — see that package's docstring for why
    # a fourth would be a problem rather than a feature.
    #
    # `enabled: false` is not a default, it is invariant L4: with it false the
    # package is never imported, no file is created, no socket is opened, and
    # not one byte of any prompt differs from a build with src/no_human/brain/
    # deleted. tests/test_brain_invariants.py asserts that byte-identity.
    #
    # `control_plane_url` is the ONLY thing this product knows about the hosted
    # service. No region, no account id, no table, no bucket, no ARN — the
    # service's shape is deliberately unlearnable from the client, and a grep
    # gate in the same test file fails the build if any of it appears.
    #
    # No credential lives here. The brain credential is a separate secret in a
    # separate file (~/.no_human/brain/credentials.json) read by a separate
    # loader, and it is never placed in os.environ — unlike the Claude token
    # above, which is exported on purpose because the Agent SDK subprocess must
    # inherit it. That difference is the point.
    "team_brain": {
        "enabled": False,
        "control_plane_url": "",
        # A withdrawn rule must not live forever on a laptop that stopped
        # syncing: past this many days without a VERIFIED sync, remote rules
        # stop being injected until one succeeds.
        "max_stale_days": 14,
    },
    "learning": {
        # D3-M1: auto-confirm a RECURRING review-origin lesson without a human
        # click — but ONLY into the CODER's channel, NEVER the reviewer's. The
        # channel split (core/db.py `confirmed_by` + the orchestrator's
        # reviewer-memory exclusion) preserves gate independence (constraint #3)
        # BY CONSTRUCTION: an auto-confirmed review lesson reaches the coder and
        # can never reach the reviewer that produced it.
        #
        # Modeled on `profile.auto_confirm_proven` — the same "proof, not a
        # click" shape, default OFF. Here the proof is the same review finding
        # recurring across >=2 DISTINCT tasks in one project that each reached
        # HUMAN approval (a MERGED PR outcome, migration 0010). A miss is always
        # the safe direction: it withholds a lesson, it never lets one through.
        "auto_confirm_recurring": False,
        # Memory lifecycle C (2026-08-12 research report). The per-success
        # templated skill proposal (`learning.queue._build`'s
        # AWAITING_APPROVAL/DONE branch) is the flood source — no evidence
        # beyond "a task finished", ~394 of the pending backlog measured
        # against a copy of the operator's database. Default OFF; the safe
        # direction, same as `auto_confirm_recurring` above.
        "propose_on_success": False,
        # AC1: unconfirmed proposals older than this many days are
        # auto-archived (reversible — never deleted) by the daily sweep.
        "archive_unconfirmed_days": 45,
        # AC2: confirmed rules unused for this many days surface in the
        # `retire?` SUGGEST-only section — never auto-archived.
        "retire_suggest_days": 90,
        # How often the retirement sweep job ticks. `_last_run = 0.0` at
        # construction means the first tick after boot runs immediately —
        # that IS the startup sweep, with no separate path to test.
        "sweep_interval_seconds": 86400,
        # Kill switch for the sweep job (`api/app.py`'s lifespan passes None
        # to `Scheduler` instead of constructing `RetirementSweepJob`). The
        # `sweep_unconfirmed`/`archive_unconfirmed_older_than` FUNCTIONS stay
        # reachable regardless (CLI `--triage-templated`, tests) — this only
        # turns off the unattended daily tick.
        "sweep_enabled": True,
        # D3 (2026-08-31 operator directive — recorded in `learning/
        # curator.py`'s and `core/scheduler.py` `HarvestJob`'s rewritten
        # docstrings): auto-activation of a harvested learning that passes
        # the dedupe/PII/provenance/term screens
        # (`LearningQueue.auto_activate`), REVERSING the previous "a human
        # confirms every learning" contract — deliberately, and recorded
        # here rather than silently flipped. Default True is the flipped
        # default the directive calls for. `False` is the KILL SWITCH FOR
        # THIS WRITE PATH: it must exist before the default flips, and it
        # restores the pre-D3 harvest/confirm-queue behaviour exactly —
        # `HarvestJob` calls `auto_activate` not at all, every proposal
        # stays `confirmed=False`/`source="proposed"`, inert until a human
        # runs `nh learnings --confirm <id>`. It does NOT touch the
        # 2026-09-01 word-boundary trigger-matching fix or `reject()`
        # aliasing `pause()` for an already-confirmed row — both are
        # correctness fixes, not gated by this key.
        "auto_manage": True,
        # D3: the ceiling on how many proposals `HarvestJob` may
        # auto-activate per rolling 24h window (`Store.
        # count_auto_activated_since`, checked inside
        # `LearningQueue.auto_activate`). The compensating control for
        # `auto_manage`'s flipped default — even with the human gate
        # reversed, no tick (or run of ticks inside one day) can flood the
        # active rule set unattended. An 11th otherwise-eligible proposal in
        # the same window stays pending, not activated and not archived.
        "auto_activate_daily_cap": 10,
    },
    # The scheduled learning-harvest pass (`core/scheduler.py` `HarvestJob`):
    # supervisor corrections + escalations + reviewer FAIL findings + tamper
    # trips, clustered and proposed on a cadence inside `nh serve` — the same
    # `harvest_supervisor_corrections`/`harvest_failure_signals` a human can
    # already run by hand via `nh learnings --harvest`. `distill` is never
    # configured here (always `None`): the scheduled pass makes NO LLM call,
    # ever — it proposes the verbatim-clustered lesson, same as an
    # un-configured manual harvest. It only ever PROPOSES: every row lands
    # `source="proposed"`, `confirmed=False`, and stays inert until a human
    # runs `nh learnings --confirm <id>` — see `learning/failures.py`'s
    # module docstring for why that stays reviewable entries, not a PR.
    "harvest": {
        # Both harvest loops (eval/harvest.py's bench candidates and
        # learning/queue.py's supervisor-correction + failure-signal
        # proposals) on a cadence inside the EXISTING `nh serve` tick — no
        # cron, no queue, no daemon (core/scheduler.py's HarvestJob). The
        # scheduled pass never calls a model (distill=None), which is why
        # it defaults on, and it only ever PROPOSES: nothing is applied,
        # confirmed or opened as a PR without a human running
        # `nh learnings --confirm` (or editing a harvested bench task by
        # hand).
        "enabled": True,
        # Once per 12 hours by default — a persisted signal is not
        # time-sensitive the way task dispatch is, so this rides a much
        # slower cadence than the dispatch loop itself.
        "interval_seconds": 43200,
    },
    # The `unattributed_usage` ledger (core/db.py) is append-only and never
    # DELETEd wholesale: it is the whole-cost residual `nh status` reads as
    # the true total, and a plain DELETE would make that number silently
    # shrink. Past this many days, `Store.compact_unattributed_usage` rolls
    # aged rows up into one row per (site, model) instead — token totals
    # survive exactly; per-row ts/task_id detail does not. `0` disables
    # compaction (unbounded growth, the pre-existing behavior).
    "usage_ledger": {
        "retention_days": 90,
    },
    "telemetry": {
        # Opt-OUT usage telemetry + masked session replay. CONSENT, default
        # ON — usage events and masked recordings are sent unless the user turns
        # this OFF. The onboarding step and the Settings > Usage insights pane
        # were removed (operator, 2026-08-26); the one opt-out now is config.yaml
        # `telemetry.enabled: false`. The published privacy posture: anonymous
        # usage events and masked recordings of the app's OWN interface — never
        # code, prompts, titles, paths or tokens.
        "enabled": True,
        # PostHog *publishable* client token (phc_…). Publishable by design —
        # it can only ingest events, never read data — which is why it is
        # allowed to live in config defaults at all. The field is named
        # `posthog_publishable` (not *_key / *_token) deliberately:
        # /api/config's `_scrub_secrets` masks any key whose NAME matches
        # token|secret|password|webhook|key, and this value must survive the
        # /api/config echo so the browser can init the client. Renaming the
        # field, not weakening the scrubber, is the sanctioned fix.
        "posthog_publishable": "phc_vwbcZ2PwY5hvSmxN7jJeAsK3UqjG5QhvZzTRRHqzvGkv",
        "posthog_host": "https://us.i.posthog.com",
        # First-party ingestion endpoint for server-side events. Deliberately
        # EMPTY in defaults: the L3 brain invariant (tests/test_brain_
        # invariants.py) bans cloud deployment identifiers from the local
        # product's source, so the hosted ingestion URL arrives as
        # configuration (config.yaml `telemetry.endpoint`) exactly like
        # `team_brain.control_plane_url` — never as a constant here. With no
        # endpoint configured, server-side events go to PostHog's `/batch/`
        # endpoint on `posthog_host` instead (telemetry.py:_destination); an
        # explicit `telemetry.endpoint` always wins over PostHog. Browser-side
        # PostHog telemetry is independent of this key.
        "endpoint": "",
        # Anonymous instance id: minted (uuid4) SERVER-SIDE on first enable by
        # the consent endpoint and persisted to config.yaml via the shared
        # config write path. Never minted in, or accepted from, the browser.
        "instance_id": "",
    },
}


# The canonical privacy-posture wording (web/src/onboardingConsent.js holds the
# byte-identical twin; tests/test_telemetry.py pins them together). SAME contract
# the "telemetry" block above states — never widen it here without widening the
# comment and the privacy policy. The onboarding consent step and the Settings >
# Usage insights pane that once showed this were removed (operator, 2026-08-26);
# telemetry now ships on with config.yaml `telemetry.enabled: false` the one
# opt-out, so this is kept as documentation of the posture, not a UI prompt.
TELEMETRY_CONSENT_QUESTION = (
    "Share anonymous usage events and masked screen recordings of the app's "
    "own interface — never code, prompts, titles, paths or tokens?"
)


def worktree_isolation_enabled(config: dict[str, Any]) -> bool:
    """True when a task must run in its own git worktree. Default TRUE.

    Reads ``isolation.enabled``. Every config.yaml written before isolation and
    parallelism were split lacks the block entirely — including the full default
    dump ``nh init`` writes, which pins ``concurrency.enabled: false`` — so an
    absent key has to resolve to the new default, not to the old coupled one.
    """
    return bool((config.get("isolation") or {}).get("enabled", True))


def parallelism_enabled(config: dict[str, Any]) -> bool:
    """True when more than one task may run at a time. Default FALSE."""
    return bool((config.get("concurrency") or {}).get("enabled", False))


#: Paths the harness always treats as UI work, regardless of any per-repo
#: profile — a bare install with no confirmed `ui_evidence.ui_paths` still
#: gets the walk on the two conventional UI directories this product itself
#: ships (`web/`, `desktop/`).
UI_EVIDENCE_DEFAULT_GLOBS = ("web/**", "desktop/**")


def ui_evidence_should_run(
    config: dict[str, Any], changed_paths: list[str] | None = None,
    *, extra_globs: "list[str] | tuple[str, ...]" = (),
) -> bool:
    """True when the harness should run the UI-evidence browser walk
    (`testing/ui_evidence.py`) after this attempt's tests pass (D1.2).

    ``ui_evidence.enabled`` is a three-state switch. Left at its default
    (``None`` — see ``DEFAULT_CONFIG``), the decision follows the diff: ON
    when *changed_paths* touches ``web/**`` or ``desktop/**`` (plus any
    repo-declared ``ui_evidence.ui_paths`` globs the caller threads through
    as ``extra_globs``), OFF otherwise — a change that never touches UI
    code never pays for a browser launch. An operator who sets the key
    explicitly (``True``/``False``) forces that answer for EVERY attempt
    regardless of the diff: the master kill switch this feature needed
    before it could run unattended (the egress allowlist cites this same
    key as the browser channel's gate).
    """
    raw = (config.get("ui_evidence") or {}).get("enabled")
    if raw is not None:
        return bool(raw)
    globs = tuple(UI_EVIDENCE_DEFAULT_GLOBS) + tuple(extra_globs or ())
    return any(
        fnmatch.fnmatch(p, g) for p in (changed_paths or []) for g in globs
    )


def worktree_root(config: dict[str, Any]) -> Path:
    """Directory the per-task worktrees are created under.

    ``isolation.worktree_root`` first, then the pre-split
    ``concurrency.worktree_root`` (an operator who relocated their worktrees
    must not have them silently move back), then ``~/.no_human/worktrees``.
    """
    root = ((config.get("isolation") or {}).get("worktree_root")
            or (config.get("concurrency") or {}).get("worktree_root"))
    return Path(root).expanduser() if root else (NO_HUMAN_HOME / "worktrees")


# A worktree directory is named `<task_id>.<owner_pid>.<token>`. The three parts
# each do one job: the task id makes the directory attributable (the doctor's
# orphan check reads it, and so does an operator staring at the root), the owner
# pid says which process is entitled to it, and the random token is what makes
# the name UNIQUE PER RUN — the whole point. Before this shape the path was the
# bare task id, so overlapping attempts of one task shared one checkout and the
# first to finish removed the directory the other was working in.
#
# Directories in the OLD bare-`<task_id>` shape still exist under existing
# worktree roots; both readers below accept them (the parse simply yields the
# whole name) so nothing is orphaned by the rename.
def worktree_owner(dir_name: str) -> tuple[str, int | None]:
    """Split a worktree directory NAME into ``(task_id, owner_pid)``.

    ``owner_pid`` is None for the legacy bare-``<task_id>`` shape and for any
    name that does not parse — callers must treat "no owner" as "cannot prove a
    live owner", never as "definitely dead"… except where the old code already
    took the directory (see the orchestrator's reaper, which is scoped to the
    one task it is about to run and is the same reclaim the old acquire did).
    """
    parts = dir_name.split(".")
    if len(parts) >= 3 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return parts[0], None


def _kernel32():
    """The Win32 ``kernel32`` handle. Imported lazily — ``ctypes.WinDLL`` does
    not exist off Windows — and split out so a test can substitute it."""
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_pid_alive(pid: int):
    """Whether *pid* is a live process, WITHOUT signalling it (OpenProcess, not
    ``os.kill(pid, 0)`` — signal 0 is CTRL_C_EVENT on Windows).

    UNTESTED ON WINDOWS. Returns True (alive), False (no such process) or None
    (exists but not ours to touch), matching the POSIX branch's tri-state.
    """
    import ctypes

    ERROR_ACCESS_DENIED = 5
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        # ERROR_ACCESS_DENIED means the process EXISTS but belongs to someone
        # else; every other failure (ERROR_INVALID_PARAMETER) means no such pid.
        denied = ctypes.get_last_error() == ERROR_ACCESS_DENIED
        return None if denied else False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # we hold a handle, so it exists; state unreadable
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Whether *pid* names a live process.

    Errs toward ALIVE for UNANSWERABLE questions only — a pid we are not
    allowed to signal (POSIX EPERM), a process that exists but is not ours
    (Windows access-denied). A recycled pid likewise reads as alive: that leaks
    a directory the doctor then reports, where the opposite error deletes a
    checkout somebody is working in.

    A PROVABLY-dead pid reads as dead on every platform. On Windows this must
    NOT go through ``os.kill(pid, 0)``: signal 0 is ``CTRL_C_EVENT``, so
    ``os.kill`` there is ``GenerateConsoleCtrlEvent`` (a Ctrl-C broadcast, not a
    liveness probe — see ``cli.commands._probe_pid``), and its
    ERROR_INVALID_PARAMETER fires for a *live* non-group pid too. It goes
    through ``_windows_pid_alive`` (OpenProcess) instead, so a genuinely-absent
    pid reads dead while access-denied keeps the err-toward-ALIVE bias. This is
    the scheduler-lease footgun's fix: an ungracefully-killed instance (the NSIS
    upgrade path taskkills the running app) leaves a ``scheduler_heartbeat``
    row; without a correct dead-pid read the new instance saw a live sibling for
    the whole heartbeat-stale window and silently refused to dispatch.
    """
    if _IS_WINDOWS:
        # tri-state: True alive, False no-such-process, None exists-not-ours.
        return _windows_pid_alive(pid) is not False
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _parse_linux_stat_starttime(content: str) -> str | None:
    """Extract field 22 (``starttime``) from the text of a ``/proc/<pid>/stat``
    line. Pure string parsing, split out from the file read so it can be unit
    tested against a fixture line without a real ``/proc``.

    The ``comm`` field (2nd, parenthesized) is attacker/user controlled and may
    itself contain spaces or ``)`` — the only safe split is from the LAST
    ``)`` in the line, never the first. Every field after that point is
    whitespace-delimited and fixed-position: ``state`` is 1st, ``starttime``
    is the 20th (field 22 overall, minus the 2 consumed by pid/comm).
    """
    idx = content.rfind(")")
    if idx == -1:
        return None
    fields = content[idx + 1:].split()
    if len(fields) < 20:
        return None
    return fields[19]


def _linux_start_token(pid: int) -> str | None:
    """The kernel's ``starttime`` for *pid* on Linux, opaque and comparable
    only for equality against another read of the same pid. None on any read
    failure (dead pid, permission, unexpected format) — never raises."""
    try:
        content = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return _parse_linux_stat_starttime(content)


def _macos_start_token(pid: int) -> str | None:
    """The kernel's process start time for *pid* on macOS, opaque and
    comparable only for equality against another read of the same pid.

    Reads it via ``ps -o lstart=`` rather than hand-rolled ``ctypes`` over
    ``sysctl(KERN_PROC_PID)``'s ``kinfo_proc`` struct: that struct is not a
    stable ABI across Darwin releases/architectures, and a silently
    misaligned read here would corrupt a SAFETY decision (a garbage token
    looks like a mismatch and takes over a lease that is still live). ``ps``
    reads the exact same kernel field through a stable, already-installed
    system utility — no new dependency, and no private struct layout to keep
    in sync with the OS.
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    token = proc.stdout.strip()
    return token or None


def _win_start_token_from_kernel32(kernel32, wintypes_mod, pid: int) -> str | None:
    """The guts of the Windows start-token read, taking the ``kernel32``
    handle and ``ctypes.wintypes`` module as arguments so a test can inject a
    mock ``kernel32`` (mocked ``OpenProcess``/``GetProcessTimes``/
    ``CloseHandle``) and exercise this on any platform — the real call site,
    `_windows_start_token`, only reaches here on ``win32``, where the actual
    ``ctypes.windll.kernel32`` exists."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes_mod.FILETIME()
        exit_t = wintypes_mod.FILETIME()
        kernel_t = wintypes_mod.FILETIME()
        user_t = wintypes_mod.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_t),
            ctypes.byref(kernel_t), ctypes.byref(user_t))
        if not ok:
            return None
        return f"{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
    finally:
        kernel32.CloseHandle(handle)


def _windows_start_token(pid: int) -> str | None:
    """The process creation time for *pid* on Windows via ``GetProcessTimes``
    (kernel32, through ``ctypes`` — no new dependency), opaque and comparable
    only for equality. Fails SOFT: any missing platform bits, a permission
    refusal, or an unexpected error returns None rather than raising, which
    makes the lease-side caller fall back to today's legacy (token-less,
    ``pid_alive``-only) behaviour — never less safe, never a crash, even if
    this exact path was never exercised live (only unit-tested with a mocked
    handle, see `_win_start_token_from_kernel32`)."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes
        return _win_start_token_from_kernel32(ctypes.windll.kernel32, wintypes, pid)
    except Exception:  # noqa: BLE001 — must fail soft, never crash the lease claim
        return None


def process_start_token(pid: int) -> str | None:
    """An opaque per-process start-time token for *pid* on THIS host, or None
    if it cannot be determined (dead pid, unsupported platform, any read
    failure) — never raises.

    Meaningful ONLY as an equality comparison across two reads of the same
    pid number: a match means "the same OS process instance is still there";
    a mismatch means a NEW process now holds that pid, so whatever wrote the
    old token is provably gone. Used by `core.scheduler._lease_sibling_is_dead`
    to catch a recycled pid `pid_alive` alone cannot distinguish from its
    original holder — `pid_alive` itself is untouched (its other callers need
    the err-toward-alive bias this function does not carry).
    """
    if sys.platform.startswith("linux"):
        return _linux_start_token(pid)
    if sys.platform == "darwin":
        return _macos_start_token(pid)
    if sys.platform == "win32":
        return _windows_start_token(pid)
    return None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a DEEP copy of ``base``.

    The copy has to be deep. ``dict(base)`` duplicates only the top level, so
    every section the user's config.yaml does not mention came back as
    DEFAULT_CONFIG's OWN nested dict — and a caller writing into its own
    resolved config then re-pointed the default for the whole process. Measured
    2026-08-10: the nightly eval sets ``server.port`` on its isolated instance's
    config, which moved ``DEFAULT_CONFIG["server"]["port"]`` from 8420 to 8431
    and surfaced (a suite away) as a README-claims failure. Lists are copied for
    the same reason — ``never_push_to`` and ``forbidden_paths`` are the shapes a
    caller is most likely to append to.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


@dataclass
class Config:
    """Resolved configuration: defaults overlaid with the user's config.yaml."""

    data: dict[str, Any]
    path: Path

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def primary_model(self) -> str:
        return self.data["llm"]["primary_model"]

    @property
    def review_model(self) -> str:
        return self.data["llm"]["review_model"]

    @property
    def review_timeout_seconds(self) -> int:
        return review_timeout_seconds(self.data)

    @property
    def code_review_timeout_seconds(self) -> int:
        return code_review_timeout_seconds(self.data)

    @property
    def planner_model(self) -> str:
        return self.data.get("llm", {}).get("planner_model", self.review_model)

    @property
    def utility_model(self) -> str:
        return self.data.get("llm", {}).get(
            "utility_model", DEFAULT_CONFIG["llm"]["utility_model"]
        )

    @property
    def worker_backend(self) -> str:
        return self.data.get("worker", {}).get("backend", "claude")

    @property
    def db_path(self) -> Path:
        return Path(self.data["database"]["path"]).expanduser()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (POSIX ``os.replace``).

    Writes to a sibling ``.tmp`` file first, then replaces the target in a
    single rename — so a concurrent reader of *path* will never see a
    half-written file.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def load_config(
    config_path: Path = CONFIG_PATH,
    *,
    create_if_missing: bool = True,
) -> Config:
    """Load ``~/.no_human/config.yaml``, generating a default if absent.

    Refuses to honour an ``ANTHROPIC_API_KEY`` smuggled into the config file —
    that variable must never appear in config (constraint §3.1).
    """
    # Outside the fresh-config branch: an ALREADY-INITIALISED install never
    # reached this, so its ~/.no_human stayed at whatever umask created it
    # until someone happened to write a credential.
    # Chmod ONLY our own directory: widening that to every load meant a caller
    # passing a custom config path had ITS directory forced to 0700 —
    # including, for a relative path, the CWD.
    #
    # But still CREATE whatever parent was asked for. Scoping the chmod also
    # dropped the mkdir, which silently narrowed a contract: a custom path
    # under a missing parent used to be created and started raising
    # FileNotFoundError from _atomic_write_text instead.
    if config_path.parent == NO_HUMAN_HOME:
        ensure_private_dir(NO_HUMAN_HOME)
    elif create_if_missing:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists() and create_if_missing:
        _atomic_write_text(config_path, yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False))

    user_data: dict[str, Any] = {}
    if config_path.exists():
        user_data = yaml.safe_load(config_path.read_text()) or {}

    _reject_api_key_in_config(user_data)
    _reject_invalid_role_backends(user_data)
    if "tracker" in user_data:
        warnings.warn(
            "The 'tracker' config section is deprecated and ignored — the TRACKER "
            "integration was removed. Delete it from config.yaml to silence this.",
            DeprecationWarning,
            stacklevel=2,
        )
    merged = _deep_merge(DEFAULT_CONFIG, user_data)
    merged.pop("tracker", None)  # ignore any stale block from an old config
    _reject_decomposition_enabled(merged)
    return Config(data=merged, path=config_path)


#: The top-level `llm:` header line, with an OPTIONAL trailing inline comment
#: (`llm:  # which subscription pays`). Matched conservatively: no value may
#: follow the colon except whitespace and a `#` comment, so `llm: {}` or a
#: flow mapping is deliberately NOT treated as a spliceable block header.
#: Anchored at column 0 — no leading whitespace — so a nested `llm:` under
#: another section stays out of scope.
_LLM_HEADER_RE = re.compile(r"^llm:[ \t]*(#.*)?$")


def _splice_llm_scalar(lines: list[str], key: str, value: str) -> None:
    """Splice ``key: value`` into the top-level ``llm:`` block of *lines*, in
    place, preserving every other line (including comments) verbatim.

    Extracted from ``set_auth_profile``, which used to inline exactly this
    walk for ``auth_profile`` alone: find the top-level ``llm:`` line; if
    absent, append a fresh ``llm:`` block. If present, replace an existing
    ``key:`` scalar line within the block (preserving its indent), or insert
    a new ``  key: value`` line right after ``llm:`` if the key is not yet
    present. ``set_model_ids`` reuses this once per changed key, on the same
    mutable ``lines`` list, so multiple keys land in one atomic write.

    The header line itself is never rewritten — an inline comment on it
    (``llm:  # which subscription pays``) is matched by ``_LLM_HEADER_RE``
    and preserved byte-for-byte; only ``==  "llm:"`` used to match, so a
    commented header was treated as absent and a second ``llm:`` block got
    appended, which PyYAML resolves last-wins — silently dropping the
    operator's entire original section.
    """
    try:
        start = next(i for i, ln in enumerate(lines) if _LLM_HEADER_RE.match(ln.rstrip()))
    except StopIteration:
        lines.extend(["llm:", f"  {key}: {value}"])
        return

    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line[:1].isspace():
            end = i
            break
    for i in range(start + 1, end):
        stripped = lines[i].lstrip()
        if stripped.startswith(f"{key}:"):
            indent = lines[i][: len(lines[i]) - len(stripped)]
            lines[i] = f"{indent}{key}: {value}"
            break
    else:
        lines.insert(start + 1, f"  {key}: {value}")


def _duplicate_top_level_keys(text: str) -> list[str]:
    """Top-level keys that appear more than once in *text*.

    PyYAML's *constructor* (``safe_load``) resolves duplicate mapping keys
    last-wins and silently drops the earlier block; its *composer*
    (``yaml.compose``) does not — it keeps every key node in the parse tree,
    so it is the one instrument that can see the outcome the constructor
    hides. A malformed document returns ``[]`` and is left to ``load_config``
    to reject.
    """
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return []
    if not isinstance(node, yaml.MappingNode):
        return []
    keys: list[str] = []
    for key_node, _value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode):
            keys.append(key_node.value)
    seen_once: set[str] = set()
    dupes: set[str] = set()
    for k in keys:
        if k in seen_once:
            dupes.add(k)
        else:
            seen_once.add(k)
    return sorted(dupes)


def _reject_duplicate_keys_after_write(config_path: Path, original: str, what: str) -> None:
    """Refuse a write that left two top-level blocks for the same key.

    Called right after ``_atomic_write_text`` and BEFORE the resolve check,
    because the resolve check is exactly the instrument that a duplicate-key
    bug can satisfy while the file is wrong: PyYAML resolves only the last
    block, so re-loading and checking the new value back looks fine even
    though the operator's *entire original section* (e.g. which subscription
    pays) was just silently dropped. A verify that the bug itself can pass is
    not a verify.
    """
    dupes = _duplicate_top_level_keys(config_path.read_text())
    if dupes:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to {what}: the edit left duplicate top-level key(s) "
            f"{dupes!r} in {config_path}; PyYAML would silently resolve only "
            "the last one and drop the operator's original section. The file "
            "has been restored."
        )


def set_auth_profile(profile: str, config_path: Path = CONFIG_PATH) -> str:
    """Pin ``llm.auth_profile`` in config.yaml. Returns the normalized name.

    The key is edited as text, not via a ``safe_load``/``safe_dump`` round-trip,
    because that would silently delete the operator's hand-written comments —
    among them the "model IDs are intentionally NOT pinned here" warning that
    exists precisely because a frozen dump once shadowed the real defaults.

    A text edit into YAML is only safe if the value cannot inject structure, so
    the name is validated first; the result is then verified by re-resolving the
    config, and the original file is restored on any mismatch.
    """
    profile = validate_profile_name(profile)

    load_config(config_path)  # materialize a default file if there is none
    original = config_path.read_text()
    lines = original.splitlines()
    _splice_llm_scalar(lines, "auth_profile", profile)
    _atomic_write_text(config_path, "\n".join(lines) + "\n")
    _reject_duplicate_keys_after_write(config_path, original, "set auth profile")

    resolved = load_config(config_path).data["llm"].get("auth_profile")
    if resolved != profile:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set auth profile: {config_path} resolved to {resolved!r} "
            f"after the edit, not {profile!r}. The file has been restored."
        )
    return profile


def set_codex_auth_mode(mode: str, config_path: Path = CONFIG_PATH) -> str:
    """Pin ``llm.codex_auth_mode`` in config.yaml. Returns the normalized mode.

    The MODE may live in config.yaml (constraint #6b); the OpenAI KEY never
    does — that goes to ``~/.no_human/.env`` via :func:`set_codex_api_key`.
    Same text-splice-preserving-comments discipline as :func:`set_auth_profile`
    (the value — ``api_key`` / ``subscription`` — is a bare word that cannot
    inject YAML structure): validate first, splice via ``_splice_llm_scalar``,
    verify by re-resolving, restore the original file on any mismatch.
    """
    mode = str(mode or "").strip().lower()
    if mode not in CODEX_AUTH_MODES:
        raise ValueError(
            f"codex auth mode {mode!r} is invalid; must be one of "
            f"{sorted(CODEX_AUTH_MODES)!r}"
        )

    load_config(config_path)  # materialize a default file if there is none
    original = config_path.read_text()
    lines = original.splitlines()
    _splice_llm_scalar(lines, "codex_auth_mode", mode)
    _atomic_write_text(config_path, "\n".join(lines) + "\n")
    _reject_duplicate_keys_after_write(config_path, original, "set codex auth mode")

    resolved = load_config(config_path).data.get("llm", {}).get("codex_auth_mode")
    if resolved != mode:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set codex auth mode: {config_path} resolved to "
            f"{resolved!r} after the edit, not {mode!r}. The file has been "
            "restored."
        )
    return mode


#: A bare model id, as YAML would parse it back unquoted: letters, digits,
#: '.', '_', '-' only. No quotes, no colons, no newlines — nothing that could
#: inject YAML structure via a spliced scalar. Checked BEFORE the file is
#: ever touched, same discipline as ``validate_profile_name``.
_MODEL_ID_SHAPE_RE = re.compile(r"[A-Za-z0-9._-]+")


def set_model_ids(
    updates: dict[str, str], config_path: Path = CONFIG_PATH
) -> dict[str, str]:
    """Splice ``updates`` (``llm.*`` config key -> model id) into config.yaml,
    one atomic write, preserving comments. Returns the resolved values after
    the write.

    Mirrors ``set_auth_profile``: every value is shape-validated before the
    file is ever touched, the write is verified by re-loading the config
    afterward, and the original file is restored on ANY failure — including
    ``load_config`` itself raising (e.g. ``_reject_api_key_in_config`` firing
    on a written value shaped like a credential) and a written value that
    resolved to something other than what was requested.

    This function does no role resolution and no catalog validation
    (vendor pin / price / review-gate) of its own — callers
    (``core.model_settings.apply_model_changes``, and its CLI twin) are
    responsible for calling ``model_catalog.validate`` on every value first.
    Its own job is purely the text splice and the write-time safety net; it
    still refuses an unrecognised key as a last-line guard, so a caller bug
    cannot write outside the five allowed ``llm.*_model`` keys.
    """
    from .core.model_catalog import ROLES  # local: config.py stays free of a

    # module-level import from core/ so the arrow (core -> config, via
    # model_catalog.defaults()'s own local import) stays one-directional.

    allowed = set(ROLES.values())
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(
            f"unrecognised config key(s) {sorted(unknown)!r}; must be a "
            f"subset of {sorted(allowed)!r}"
        )
    for key, value in updates.items():
        if not isinstance(value, str) or not _MODEL_ID_SHAPE_RE.fullmatch(value):
            raise ValueError(
                f"{key} value {value!r} is not a bare model id "
                "(letters, digits, '.', '_', '-' only) — refusing to splice "
                "it into config.yaml."
            )

    load_config(config_path)  # materialize a default file if there is none
    original = config_path.read_text()
    lines = original.splitlines()
    for key, value in updates.items():
        _splice_llm_scalar(lines, key, value)
    _atomic_write_text(config_path, "\n".join(lines) + "\n")
    _reject_duplicate_keys_after_write(
        config_path, original, f"set model id(s) {sorted(updates)!r}"
    )

    try:
        resolved_cfg = load_config(config_path)
    except Exception as exc:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set model id(s) {sorted(updates)!r}: {config_path} "
            f"failed to reload after the edit ({exc}). The file has been "
            "restored."
        ) from exc

    resolved = {key: resolved_cfg.data.get("llm", {}).get(key) for key in updates}
    mismatched = {k: v for k, v in resolved.items() if v != updates[k]}
    if mismatched:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set model id(s): {config_path} resolved to "
            f"{mismatched!r} after the edit, not the requested values. The "
            "file has been restored."
        )
    return resolved


#: Same shape as ``_LLM_HEADER_RE`` above, for the top-level ``worker:``
#: block — anchored at column 0 so a nested ``worker:`` under another section
#: stays out of scope.
_WORKER_HEADER_RE = re.compile(r"^worker:[ \t]*(#.*)?$")


def _splice_worker_scalar(lines: list[str], key: str, value: str) -> None:
    """``_splice_llm_scalar``'s twin for the top-level ``worker:`` block. Not
    unified with it into one generic helper on purpose: the two headers
    (``llm:`` / ``worker:``) are matched by separate compiled regexes, and
    inlining a header-selector parameter into the hot ``set_model_ids`` path
    was judged a larger blast radius than 20 duplicated lines here — see
    ``set_worker_backend``, this function's only caller.
    """
    try:
        start = next(i for i, ln in enumerate(lines) if _WORKER_HEADER_RE.match(ln.rstrip()))
    except StopIteration:
        lines.extend(["worker:", f"  {key}: {value}"])
        return

    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line[:1].isspace():
            end = i
            break
    for i in range(start + 1, end):
        stripped = lines[i].lstrip()
        if stripped.startswith(f"{key}:"):
            indent = lines[i][: len(lines[i]) - len(stripped)]
            lines[i] = f"{indent}{key}: {value}"
            break
    else:
        lines.insert(start + 1, f"  {key}: {value}")


_CONCURRENCY_HEADER_RE = re.compile(r"^concurrency:[ \t]*(#.*)?$")


def _splice_concurrency_scalar(lines: list[str], key: str, value: str) -> None:
    """``_splice_worker_scalar``'s twin for the top-level ``concurrency:``
    block. Same deliberate duplication rationale (a header-specific regex, not
    a generic selector) — see that function. Only caller: ``set_concurrency``.
    """
    try:
        start = next(
            i for i, ln in enumerate(lines)
            if _CONCURRENCY_HEADER_RE.match(ln.rstrip())
        )
    except StopIteration:
        lines.extend(["concurrency:", f"  {key}: {value}"])
        return

    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line[:1].isspace():
            end = i
            break
    for i in range(start + 1, end):
        stripped = lines[i].lstrip()
        if stripped.startswith(f"{key}:"):
            indent = lines[i][: len(lines[i]) - len(stripped)]
            lines[i] = f"{indent}{key}: {value}"
            break
    else:
        lines.insert(start + 1, f"  {key}: {value}")


#: A bare backend name, as YAML would parse it back unquoted. Reuses the
#: exact same shape as a model id (letters, digits, '.', '_', '-' only) — no
#: quotes, no colons, no newlines, nothing that could inject YAML structure.
_BACKEND_NAME_SHAPE_RE = _MODEL_ID_SHAPE_RE


def set_worker_backend(backend: str, config_path: Path = CONFIG_PATH) -> str:
    """Splice ``worker.backend`` into config.yaml, one atomic write,
    preserving comments — the Settings-pane twin of ``set_model_ids``/
    ``set_auth_profile``, for the coder-only global default backend
    (``core.backend_settings.apply_backend_change`` is the only caller; the
    catalog/availability validation happens there, BEFORE this is reached —
    this function's own job is purely the text splice, the write-time safety
    net, and refusing a name outside ``SUPPORTED_BACKENDS`` as a last-line
    guard so a caller bug can never write an arbitrary string into
    ``worker.backend``).

    Every value is shape-validated before the file is ever touched, the
    write is verified by re-loading the config afterward, and the original
    file is restored on ANY failure — same discipline as ``set_model_ids``.
    """
    from .agent.backend import SUPPORTED_BACKENDS  # local: config.py stays

    # free of a module-level import from agent/ (agent.backend already
    # imports from config.py inside functions; this keeps the arrow
    # one-directional at import time, same idiom set_model_ids uses for
    # core.model_catalog).
    backend = str(backend or "").strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"backend {backend!r} is not supported; must be one of "
            f"{sorted(SUPPORTED_BACKENDS)!r}"
        )
    if not _BACKEND_NAME_SHAPE_RE.fullmatch(backend):
        raise ValueError(
            f"backend {backend!r} is not a bare name (letters, digits, '.', "
            "'_', '-' only) — refusing to splice it into config.yaml."
        )

    load_config(config_path)  # materialize a default file if there is none
    original = config_path.read_text()
    lines = original.splitlines()
    _splice_worker_scalar(lines, "backend", backend)
    _atomic_write_text(config_path, "\n".join(lines) + "\n")
    _reject_duplicate_keys_after_write(config_path, original, "set worker backend")

    try:
        resolved_cfg = load_config(config_path)
    except Exception as exc:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set worker backend {backend!r}: {config_path} failed "
            f"to reload after the edit ({exc}). The file has been restored."
        ) from exc

    resolved = resolved_cfg.data.get("worker", {}).get("backend")
    if resolved != backend:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set worker backend: {config_path} resolved to "
            f"{resolved!r} after the edit, not {backend!r}. The file has "
            "been restored."
        )
    return backend


def _role_backends_flow_mapping(role_backends: dict[str, dict[str, str]]) -> str:
    """Serialize a ``{role: {backend, model}}`` mapping as a single-line YAML
    flow mapping, for ``_splice_llm_scalar``.

    Every token going in here has already been shape-validated (role against
    ``_ROLE_BACKENDS_ROLE_WHITELIST``, backend against ``SUPPORTED_BACKENDS``,
    model against ``_MODEL_ID_SHAPE_RE``) by ``set_role_backend``, so nothing
    here can inject YAML structure. Sorted by role for a deterministic write.
    """
    if not role_backends:
        return "{}"
    parts = [
        f"{role}: {{backend: {entry['backend']}, model: {entry['model']}}}"
        for role, entry in sorted(role_backends.items())
    ]
    return "{" + ", ".join(parts) + "}"


def set_role_backend(
    role: str,
    backend: str | None,
    model: str | None,
    config_path: Path = CONFIG_PATH,
) -> dict[str, str] | None:
    """Splice one role's entry into ``llm.role_backends``, one atomic write,
    preserving comments — the *only* writer for this key (constraint §6d;
    ``core.role_backend_settings.apply_role_backend_change`` is the only
    caller; catalog/availability validation happens there, BEFORE this is
    reached — this function's own job is purely the text splice, the
    write-time safety net, and refusing an out-of-whitelist role / unsupported
    backend / non-bare-shape model as a last-line guard).

    A falsy ``backend`` AND ``model`` (both ``None``/blank) *clears* the role
    — removes its entry from the mapping, leaving any sibling role untouched.
    Otherwise both must be given and shape-valid.

    Reads the CURRENT mapping first (so this is additive per role, never a
    second writer for the whole key), re-emits the whole flow mapping via
    ``_splice_llm_scalar``, and keeps the exact same discipline as
    ``set_worker_backend``: write -> reject-duplicate-keys -> reload ->
    compare resolved value -> restore original + ``AuthError`` on any
    mismatch. Returns the resolved entry for *role* (``None`` if cleared).
    """
    from .agent.backend import SUPPORTED_BACKENDS  # local: see set_worker_backend

    role = str(role or "").strip()
    if role not in _ROLE_BACKENDS_ROLE_WHITELIST:
        raise ValueError(
            f"role {role!r} is not supported for role_backends; must be one "
            f"of {sorted(_ROLE_BACKENDS_ROLE_WHITELIST)!r}"
        )

    clearing = not backend and not model
    if not clearing:
        backend = str(backend or "").strip().lower()
        if backend not in SUPPORTED_BACKENDS:
            raise ValueError(
                f"backend {backend!r} is not supported; must be one of "
                f"{sorted(SUPPORTED_BACKENDS)!r}"
            )
        if not _BACKEND_NAME_SHAPE_RE.fullmatch(backend):
            raise ValueError(
                f"backend {backend!r} is not a bare name (letters, digits, "
                "'.', '_', '-' only) — refusing to splice it into "
                "config.yaml."
            )
        model = str(model or "").strip()
        if not model or not _MODEL_ID_SHAPE_RE.fullmatch(model):
            raise ValueError(
                f"model {model!r} is not a bare model id (letters, digits, "
                "'.', '_', '-' only) — refusing to splice it into "
                "config.yaml."
            )

    cfg = load_config(config_path)  # materialize a default file if there is none
    current = dict((cfg.data.get("llm") or {}).get("role_backends") or {})
    if clearing:
        current.pop(role, None)
    else:
        current[role] = {"backend": backend, "model": model}

    original = config_path.read_text()
    lines = original.splitlines()
    _splice_llm_scalar(lines, "role_backends", _role_backends_flow_mapping(current))
    _atomic_write_text(config_path, "\n".join(lines) + "\n")
    _reject_duplicate_keys_after_write(config_path, original, "set role backend")

    try:
        resolved_cfg = load_config(config_path)
    except Exception as exc:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set role backend for {role!r}: {config_path} failed "
            f"to reload after the edit ({exc}). The file has been restored."
        ) from exc

    resolved = dict(resolved_cfg.data.get("llm", {}).get("role_backends") or {})
    if resolved != current:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set role backend for {role!r}: {config_path} "
            f"resolved to {resolved!r} after the edit, not {current!r}. The "
            "file has been restored."
        )
    return resolved.get(role)


#: Upper bound on ``concurrency.max_workers`` a caller may WRITE. The scheduler
#: clamps the effective pool further at runtime (``clamp_pool_width`` — CPU and
#: isolation aware), so this is only a sanity rail against an absurd config
#: value, not the real ceiling on how many workers actually run.
_MAX_WORKERS_WRITE_CEILING = 64


def set_concurrency(
    config_path: Path = CONFIG_PATH,
    *,
    max_workers: int | None = None,
    enabled: bool | None = None,
) -> dict[str, object]:
    """Splice ``concurrency.max_workers`` and/or ``concurrency.enabled`` into
    config.yaml in one atomic write, preserving comments — the Settings-pane
    twin of ``set_worker_backend``/``set_model_ids`` for the worker-count row.

    Only the keys passed non-``None`` are written; the other keeps its file
    value. Shape is validated before the file is touched, the write is verified
    by re-loading, and the original file is restored on ANY failure. Returns the
    reloaded ``{"max_workers", "enabled"}`` so the caller reports what actually
    landed, not what it asked for.

    Does NOT resize a running pool: ``resolve_max_workers`` is read once at
    server start (``api.app`` lifespan), so a change here takes effect on the
    next ``nh serve``. The API layer surfaces that as ``restart_required``.
    """
    if max_workers is None and enabled is None:
        raise ValueError("set_concurrency: nothing to set (both args are None)")

    if max_workers is not None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int):
            raise ValueError(
                f"max_workers must be an int, got {type(max_workers).__name__}"
            )
        if not 1 <= max_workers <= _MAX_WORKERS_WRITE_CEILING:
            raise ValueError(
                f"max_workers must be between 1 and {_MAX_WORKERS_WRITE_CEILING}, "
                f"got {max_workers}"
            )
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(f"enabled must be a bool, got {type(enabled).__name__}")

    load_config(config_path)  # materialize a default file if there is none
    original = config_path.read_text()
    lines = original.splitlines()
    if max_workers is not None:
        _splice_concurrency_scalar(lines, "max_workers", str(max_workers))
    if enabled is not None:
        _splice_concurrency_scalar(lines, "enabled", "true" if enabled else "false")
    _atomic_write_text(config_path, "\n".join(lines) + "\n")
    _reject_duplicate_keys_after_write(config_path, original, "set concurrency")

    try:
        resolved_cfg = load_config(config_path)
    except Exception as exc:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set concurrency: {config_path} failed to reload after "
            f"the edit ({exc}). The file has been restored."
        ) from exc

    conc = resolved_cfg.data.get("concurrency", {}) or {}
    if max_workers is not None and conc.get("max_workers") != max_workers:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set concurrency: max_workers resolved to "
            f"{conc.get('max_workers')!r} after the edit, not {max_workers!r}. "
            "The file has been restored."
        )
    if enabled is not None and conc.get("enabled") != enabled:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set concurrency: enabled resolved to "
            f"{conc.get('enabled')!r} after the edit, not {enabled!r}. "
            "The file has been restored."
        )
    return {"max_workers": conc.get("max_workers"), "enabled": conc.get("enabled")}


#: The two ``llm.*`` scalars the local coder backend needs, set together by
#: the Settings pane's coder-backend row
#: (``core.backend_settings.apply_backend_change``). NOT credentials — a model
#: id and a loopback URL — so they live in config.yaml like every other
#: ``llm.*`` value; the URL's safety boundary (loopback/RFC1918 only, no
#: userinfo) is enforced by :func:`assert_local_backend_mode` at write time
#: here and again at ``make_backend`` time.
_LOCAL_BACKEND_KEYS = ("local_model", "local_base_url")


def set_local_backend_fields(
    updates: dict[str, str], config_path: Path = CONFIG_PATH
) -> dict[str, str]:
    """Splice ``llm.local_model`` / ``llm.local_base_url`` into config.yaml,
    one atomic write, preserving comments — the ``set_model_ids`` twin for the
    local backend's two non-secret config fields.

    ``local_base_url`` is validated through :func:`assert_local_backend_mode`
    BEFORE the file is touched, so a public/DNS/userinfo URL is refused with
    nothing on disk rather than left for ``make_backend`` to catch later. Same
    write-then-reload-then-restore-on-any-failure discipline as
    ``set_model_ids``; values are YAML-quoted so a URL's ``:`` / ``/`` cannot
    break the splice.
    """
    unknown = set(updates) - set(_LOCAL_BACKEND_KEYS)
    if unknown:
        raise ValueError(
            f"unrecognised config key(s) {sorted(unknown)!r}; must be a "
            f"subset of {sorted(_LOCAL_BACKEND_KEYS)!r}"
        )
    for key, value in updates.items():
        if not isinstance(value, str):
            raise ValueError(f"{key} value {value!r} must be a string")
    base_url = updates.get("local_base_url", "").strip()
    if base_url:
        assert_local_backend_mode(base_url)  # raises AuthError on a bad URL

    load_config(config_path)  # materialize a default file if there is none
    original = config_path.read_text()
    lines = original.splitlines()
    for key, value in updates.items():
        quoted = "'" + value.replace("'", "''") + "'"
        _splice_llm_scalar(lines, key, quoted)
    _atomic_write_text(config_path, "\n".join(lines) + "\n")
    _reject_duplicate_keys_after_write(
        config_path, original, f"set local backend field(s) {sorted(updates)!r}"
    )

    try:
        resolved_cfg = load_config(config_path)
    except Exception as exc:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set local backend field(s) {sorted(updates)!r}: "
            f"{config_path} failed to reload after the edit ({exc}). The file "
            "has been restored."
        ) from exc

    llm = resolved_cfg.data.get("llm", {})
    resolved = {key: (llm.get(key) or "") for key in updates}
    mismatched = {k: v for k, v in resolved.items() if v != updates[k]}
    if mismatched:
        _atomic_write_text(config_path, original)
        raise AuthError(
            f"failed to set local backend field(s): {config_path} resolved to "
            f"{mismatched!r} after the edit, not the requested values. The "
            "file has been restored."
        )
    return resolved


#: Query-param names, matched case-insensitively as a substring, that mark a
#: URL as carrying a credential. Covers `key`, `api_key`, `apikey`,
#: `access_token`/`token`, `secret`, and `password` in one pass.
_CREDENTIAL_LOOKING_PARAM_MARKERS = ("key", "token", "secret", "password")


def _reject_credential_in_url(url: str) -> None:
    """Fail loudly if *url* embeds a credential (userinfo or a key-looking
    query param). Never echoes the value. A malformed URL is left to
    :func:`assert_local_backend_mode` to reject; this returns quietly.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return
    if parsed.username or parsed.password:
        raise AuthError(
            "llm.local_base_url must not embed userinfo credentials before "
            "the host. The mode lives in config; the key never does."
        )
    for name, _ in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = name.lower()
        if any(marker in lowered for marker in _CREDENTIAL_LOOKING_PARAM_MARKERS):
            raise AuthError(
                f"llm.local_base_url has a credential-looking query "
                f"parameter ({name!r}). The mode lives in config; the key "
                "never does."
            )


def _reject_api_key_in_config(data: dict[str, Any]) -> None:
    """Fail loudly if a metered API key was placed in config (it never should).

    Covers BOTH vendors' keys. The rule is not "Anthropic's key is special" —
    it is that config.yaml is a plain, world-readable, frequently-copied file
    and no credential belongs in one. Adding the second coding backend added a
    second key that could be put there, so it is named here in the same breath;
    a guard that enumerates one vendor is a guard that misses the next one.
    The rule now also covers a credential smuggled inside a URL, since
    ``llm.local_base_url`` is a URL and not a bare key.
    """
    banned = {API_KEY_VAR, CODEX_API_KEY_VAR}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.upper() in banned:
                    raise AuthError(
                        f"{key.upper()} must never appear in config.yaml. "
                        "The auth *mode* may live in config; the key itself "
                        "belongs only in ~/.no_human/.env (chmod 600) or the "
                        "process environment."
                    )
                if isinstance(key, str) and key.lower() == "local_base_url" \
                        and isinstance(value, str):
                    _reject_credential_in_url(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)


#: Roles that may appear as a key in `llm.role_backends`. Constraint §6d wires
#: the reviewer only; planner/supervisor/utility/intake are future entries —
#: adding one here is a deliberate whitelist edit, not something a stray
#: config value can smuggle in.
_ROLE_BACKENDS_ROLE_WHITELIST = ("reviewer",)


def _reject_invalid_role_backends(data: dict[str, Any]) -> None:
    """Fail loudly if `llm.role_backends` is not exactly what the Settings
    write path (`core.role_backend_settings.apply_role_backend_change` via
    `set_role_backend`, the ONE writer) would have produced.

    This is the load-time half of the single-write-path rule for §6d: a
    config-file-injected entry (hand-edited, or dropped in by a task/env-var/
    other code path) is rejected here with a clear, operator-facing error —
    it never silently takes effect. `set_role_backend` validates before it
    writes; this validates every time the file is read, so the two guards
    together mean the only way `role_backends` ever holds a value is the
    Settings endpoint.

    A `backend: "claude"` entry is additionally required to name a model
    `core.model_catalog.options_for(role)` actually offers that role — the
    SAME catalog-membership rule `core.role_backend_settings.
    validate_role_backend_entries` enforces at write time, checked again here
    so a config-file-injected entry can't bypass it by claiming the Claude
    backend for a model outside the catalog.
    """
    from .agent.backend import SUPPORTED_BACKENDS  # local: see set_worker_backend
    from .core.model_catalog import options_for  # local: avoids an import cycle

    role_backends = (data.get("llm") or {}).get("role_backends")
    if role_backends is None:
        return
    if not isinstance(role_backends, dict):
        raise ValueError(
            f"llm.role_backends must be a mapping of role -> "
            f"{{backend, model}}, got {type(role_backends).__name__}"
        )
    for role, entry in role_backends.items():
        if role not in _ROLE_BACKENDS_ROLE_WHITELIST:
            raise ValueError(
                f"llm.role_backends has an unknown role {role!r}; only "
                f"{sorted(_ROLE_BACKENDS_ROLE_WHITELIST)!r} may appear here"
            )
        if not isinstance(entry, dict):
            raise ValueError(
                f"llm.role_backends.{role} must be a mapping of "
                f"{{backend, model}}, got {type(entry).__name__}"
            )
        extra = set(entry) - {"backend", "model"}
        if extra:
            raise ValueError(
                f"llm.role_backends.{role} has unrecognised key(s) "
                f"{sorted(extra)!r}; only 'backend' and 'model' are allowed"
            )
        backend = entry.get("backend")
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError(
                f"llm.role_backends.{role}.backend is required and must be a "
                "non-blank string"
            )
        if backend not in SUPPORTED_BACKENDS:
            raise ValueError(
                f"llm.role_backends.{role}.backend {backend!r} is not "
                f"supported; must be one of {sorted(SUPPORTED_BACKENDS)!r}"
            )
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip() \
                or not _MODEL_ID_SHAPE_RE.fullmatch(model):
            raise ValueError(
                f"llm.role_backends.{role}.model must be a bare model id "
                "(letters, digits, '.', '_', '-' only), "
                f"got {model!r}"
            )
        if backend == "claude":
            offered = {opt.id for opt in options_for(role)}
            if model not in offered:
                raise ValueError(
                    f"llm.role_backends.{role}.model {model!r} is not an "
                    f"offered model for the {role} role; must be one of "
                    f"{sorted(offered)!r}"
                )


def _reject_decomposition_enabled(data: dict[str, Any]) -> None:
    """Fail loudly if a config asks for the removed LeadAgent child-task path.

    ``decomposition.enabled`` re-enabled the legacy ``LeadAgent`` sub-task
    orchestrator (``no_human.core.lead_agent``), deleted 2026-08-12 (operator
    decision A1: intake-level decomposition composes with every gate we have;
    runtime child-spawning fought them). The key stays in DEFAULT_CONFIG so an
    old config.yaml that turns it on gets a clear startup error instead of the
    key silently doing nothing.
    """
    if bool((data.get("decomposition") or {}).get("enabled", False)):
        raise ConfigError(
            "decomposition was removed 2026-08-12 — delegation happens "
            "in-session via SDK subagents; intake-level splitting replaces "
            "child tasks"
        )
