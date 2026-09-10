"""Is the coding backend actually usable? A pre-flight readiness check.

The board can render green while every task silently fails. ``nh start`` only
requires that an OAuth *token* is on file (``assert_subscription_mode``), and
``nh doctor`` is a HISTORICAL liveness check over past events — neither verifies
that the ``claude`` CLI the Claude Agent SDK shells out to actually exists on
this machine. A fresh install with a valid token but no CLI installed passes
both checks, then every task dies at the first SDK call with
``CLINotFoundError`` — an absence, not a crash, exactly the failure mode this
project keeps getting bitten by (TESTING dead for the system's whole life, the
watcher that persisted nothing). This makes that absence loud and early.

The CLI search MIRRORS the SDK's own resolution
(``claude_agent_sdk._internal.transport.subprocess_cli._find_cli``): the bundled
binary first, then ``PATH``, then the known npm/local install locations. Kept in
lockstep so this never reports "missing" for a CLI the SDK would have found, nor
"present" for one it would not.

:func:`check_backend` is read-only and side-effect-free: it resolves paths and
reads config, it never spawns the CLI (a live auth probe would spend quota) and
never mutates the env. That is what makes it safe to call from `nh doctor`, the
board's auth payload and startup alike — and it is also its ceiling: a
valid-SHAPED but expired or revoked credential passes it and dies at the first
task. :func:`verify_credential_live` is the opt-in answer to exactly that, and
it is the one function here that DOES spend quota and DOES export a credential.
Nothing calls it implicitly.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

_IS_WINDOWS = os.name == "nt"

# Mirror of the SDK's fallback search list (subprocess_cli._find_cli). Order is
# not load-bearing for presence, but kept identical so a divergence is obvious.
# These are POSIX shapes: on Windows npm installs the CLI as a `claude.cmd`
# shim, so the names are re-derived per platform in `_fallback_candidates`.
_CLI_FALLBACK_LOCATIONS = (
    ".npm-global/bin/claude",
    ".local/bin/claude",
    "node_modules/.bin/claude",
    ".yarn/bin/claude",
    ".claude/local/claude",
)

# Extensions Windows will actually execute for an npm-installed CLI, in the
# order npm and the Node ecosystem produce them.
_WINDOWS_CLI_SUFFIXES = (".cmd", ".exe", ".bat")


def _fallback_candidates() -> list[Path]:
    """Concrete paths to probe after ``PATH``, for the current platform.

    POSIX is byte-for-byte the SDK's list. Windows is a deliberate SUPERSET and
    the divergence is stated rather than hidden: the SDK's entries are
    extensionless, which no Windows install ever produces, so applied verbatim
    they can only ever report a working install as MISSING — the worse of the
    two errors, since it tells the operator to reinstall a CLI they have. In
    the normal Windows install npm's global bin is on ``PATH`` and the
    ``shutil.which`` step above has already resolved it, so these entries are
    only reached on a machine where they are the operator's real install.
    ``/usr/local/bin`` is dropped there: on Windows it names a path on whatever
    the current drive happens to be, and nothing runnable can live there.
    """
    home = Path.home()
    if not _IS_WINDOWS:
        return [Path("/usr/local/bin/claude"),
                *(home / rel for rel in _CLI_FALLBACK_LOCATIONS)]
    dirs = [home / Path(rel).parent for rel in _CLI_FALLBACK_LOCATIONS]
    # The Windows-native npm global prefix, the analogue of `.npm-global/bin`.
    if appdata := os.environ.get("APPDATA"):
        dirs.append(Path(appdata) / "npm")
    return [d / f"claude{ext}" for d in dirs for ext in _WINDOWS_CLI_SUFFIXES]


def find_claude_cli() -> str | None:
    """Resolve the ``claude`` CLI exactly as the Claude Agent SDK does, or None.

    Bundled (inside the installed SDK package) → ``shutil.which`` → known install
    locations. Returns the resolved path string, or ``None`` when nothing the SDK
    would accept exists.
    """
    cli_name = "claude.exe" if _IS_WINDOWS else "claude"

    # 1. Bundled CLI shipped inside the SDK package. The SDK computes this as
    #    <package_root>/_bundled/<cli_name>; package_root is the dir holding
    #    claude_agent_sdk/__init__.py.
    try:
        import claude_agent_sdk

        bundled = (
            Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / cli_name
        )
        if bundled.is_file():
            return str(bundled)
    except Exception:  # noqa: BLE001 — SDK absent/unimportable ⇒ no bundled CLI
        pass

    # 2. System-wide search on PATH. Asked for the bare name on BOTH platforms,
    #    exactly as the SDK does: on Windows `shutil.which` expands PATHEXT, so
    #    the bare name is what finds `claude.cmd`/`claude.exe` — passing
    #    "claude.exe" here would NARROW the search and miss the npm shim.
    if cli := shutil.which("claude"):
        return cli

    # 3. Known install locations, per platform (see `_fallback_candidates`).
    for path in _fallback_candidates():
        if path.is_file():
            return str(path)
    return None


def _token_on_file(profile: str | None = None) -> bool:
    """Whether an OAuth token for *profile* resolves from env or ~/.no_human/.env.

    Never returns or logs the token value. A best-effort read: any failure to
    resolve the profile name is treated as "no token" rather than raising, so a
    readiness probe never crashes on a malformed config.
    """
    try:
        from ..config import load_env_var, profile_token_var

        return bool(load_env_var(profile_token_var(profile or "default")))
    except Exception:  # noqa: BLE001
        return False


@dataclass
class BackendStatus:
    """The result of a coding-backend readiness probe."""

    cli_path: str | None
    token_present: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.cli_path is not None and self.token_present


def _api_key_on_file() -> bool:
    """Whether ``ANTHROPIC_API_KEY`` resolves (env or .env), for BYO-API-key mode.

    Read-only (never exports, never echoes the value) — a doctor probe must not
    mutate the process env. Best-effort: any failure ⇒ "no key".
    """
    try:
        from ..config import API_KEY_VAR, credential_status

        return credential_status([API_KEY_VAR]).get(API_KEY_VAR, False)
    except Exception:  # noqa: BLE001
        return False


def check_backend(*, token_present: bool | None = None,
                  profile: str | None = None,
                  auth_mode: str = "subscription") -> BackendStatus:
    """Probe whether the coding backend can actually run a task.

    ``token_present`` may be supplied by a caller that already knows it (the
    board's auth payload, the startup auth assertion); when ``None`` it is read
    from config. The check is CLI-presence + credential-presence — deliberately
    not a live auth call, which would cost quota and could not run inside
    ``nh doctor``. In ``auth_mode="api_key"`` (BYO-API-key) the credential is the
    operator's ``ANTHROPIC_API_KEY`` rather than an OAuth token.
    """
    cli = find_claude_cli()
    if token_present is None:
        token_present = (
            _api_key_on_file() if auth_mode == "api_key"
            else _token_on_file(profile)
        )
    reasons: list[str] = []
    if cli is None:
        reasons.append(
            "the `claude` CLI is not installed or not on PATH — the Claude Agent "
            "SDK shells out to it for every task, so every task would fail at "
            "launch. Install it with: npm install -g @anthropic-ai/claude-code"
        )
    if not token_present:
        if auth_mode == "api_key":
            reasons.append(
                "no ANTHROPIC_API_KEY on file — auth_mode is 'api_key', so a "
                "metered key is expected in ~/.no_human/.env or the environment."
            )
        else:
            reasons.append(
                "no OAuth token on file — expected CLAUDE_CODE_OAUTH_TOKEN in "
                "~/.no_human/.env or the environment. Create one with: "
                "claude setup-token"
            )
    return BackendStatus(cli_path=cli, token_present=bool(token_present),
                         reasons=reasons)


# The cheapest authenticated call this codebase can make: the utility tier
# (haiku), one turn, low effort, readonly, and a prompt short enough that the
# answer is the only interesting thing about it. It is the same shape as the
# advisory calls in `nh learnings-curate` — deliberately, so "cheap" here means
# what it already means everywhere else in the product rather than a new claim.
_PROBE_PROMPT = "Reply with exactly: ok"

# Generous, because the point is to distinguish "this credential is rejected"
# from "this network is slow", and reporting a slow link as a dead credential
# would send the operator to regenerate a token that was fine.
_PROBE_TIMEOUT_S = 90.0


async def verify_credential_live(*, model: str, profile: str | None = None,
                                 auth_mode: str = "subscription",
                                 timeout_s: float = _PROBE_TIMEOUT_S,
                                 ) -> tuple[str, str] | None:
    """Spend ONE cheap call to prove the credential actually works.

    Returns ``None`` when the call came back, else ``(kind, why)`` where
    ``kind`` is ``"rejected"`` — the failure is ABOUT the credential/config —
    or ``"inconclusive"`` — transport never delivered an answer (timeout,
    network down), which says nothing about the credential either way. The
    split exists because an independent review demonstrated the first cut
    reporting "Network is unreachable" as CREDENTIAL DOES NOT WORK: a cron
    doctor run on a flaky network must not read as a dead credential.
    Opt-in only: `nh doctor --verify-auth` calls this from a short-lived CLI
    process where an unrestored export doesn't matter; `POST /api/auth/verify`
    is the one other caller, and because THAT process is the long-lived
    embedded worker, it gates the call on `telemetry.enabled` (so it never
    spends unasked either — see that endpoint's docstring) and snapshots/
    restores `os.environ` and the active-profile global around the call.
    Every other probe in this module stays presence-only precisely so no
    diagnostic spends quota unasked.

    It exports the credential (via :func:`config.assert_subscription_mode`,
    which is also what scrubs every other metered path) because there is no way
    to make an authenticated call without doing so — which is the second reason
    this is never implicit: it mutates the process environment. A long-lived
    caller MUST snapshot and restore `os.environ[config.SUBSCRIPTION_TOKEN_VAR]`
    and `config._ACTIVE_AUTH_PROFILE` around the call, or the export outlives
    this one probe and silently changes which profile every later action bills.

    The verdict is "did an authenticated request succeed", not "was the answer
    correct". A model that replies with something other than ``ok`` still
    proves the credential was accepted, and holding a diagnostic hostage to a
    model's phrasing would fail installs that work.
    """
    from ..config import AuthError, assert_subscription_mode

    try:
        assert_subscription_mode(profile=profile, auth_mode=auth_mode)
    except AuthError as exc:
        return ("rejected", str(exc))

    from .claude_backend import ClaudeBackend

    try:
        result = await asyncio.wait_for(
            ClaudeBackend(model=model, readonly=True).run(
                _PROBE_PROMPT, cwd=Path.cwd(), max_turns=1, effort="low"),
            timeout=timeout_s,
        )
    except (asyncio.TimeoutError, TimeoutError):
        return ("inconclusive",
                f"the check did not answer within {timeout_s:.0f}s — this says "
                f"nothing about the credential either way; try again")
    except (OSError, ConnectionError) as exc:
        # Transport never carried the request: unreachable network, DNS,
        # refused socket. The credential was never judged.
        return ("inconclusive",
                f"{type(exc).__name__}: {exc} — the request never reached the "
                f"API, so this says nothing about the credential; try again")
    except Exception as exc:  # noqa: BLE001 — every failure is a report, not a crash
        return ("rejected", f"{type(exc).__name__}: {exc}")
    if result.is_error:
        # The call went out and came back an error — an answer ABOUT the
        # attempt (401s land here), not a transport gap.
        return ("rejected",
                result.final_text.strip() or "the backend reported an error")
    return None
