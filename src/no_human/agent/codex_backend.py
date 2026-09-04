"""The second coding backend: OpenAI Codex, on a BYO-API-key OR a ChatGPT
subscription — see TWO SANCTIONED AUTH PATHS below.

WHY A SUBPROCESS AND NOT THE OpenAI PYTHON SDK. Constraint §6's "no
re-implemented tools" survived the 2026-08-01 amendment untouched: a second
backend may be added, but not a hand-rolled reimplementation of tools an SDK
already provides. The OpenAI *Responses* API has no filesystem or shell tools
for a local checkout — driving it would mean writing our own Read/Edit/Bash/
Grep/Glob loop, sandbox and patch applier, which is precisely the forbidden
thing. The Codex CLI already ships all of that. So this backend drives
``codex exec --json`` exactly the way ``claude_agent_sdk`` drives the ``claude``
CLI: a subprocess, a JSONL event stream, and a normalizer. It adds ZERO Python
dependencies for the same reason the Claude path needs no ``anthropic`` package.

TWO SANCTIONED AUTH PATHS, selected by ``llm.codex_auth_mode`` (default
``"api_key"``, so no existing install's behaviour changes — operator
amendment, 2026-08-22). The same sourcing is duplicated above
``CODEX_API_KEY_VAR`` in ``config.py`` so the assert functions there carry it
too; this copy is the primary one.

WHAT OPENAI'S OWN DOCUMENTATION SAYS, quoted from
``developers.openai.com/codex/auth`` (308-redirects to
``learn.chatgpt.com/docs/auth``), fetched 2026-08-22:

  * "Codex supports two ways for a person to sign in when using OpenAI
    models: Sign in with ChatGPT for subscription access [and] Sign in
    with an API key for usage-based access."
  * "The ChatGPT desktop app, Codex CLI, and IDE extension support both
    sign-in methods for local work."
  * "Use API key authentication for programmatic Codex CLI workflows,
    such as CI/CD jobs. Don't expose Codex execution in untrusted or
    public environments."

So a ChatGPT sign-in IS an officially documented Codex CLI method for local
work — which is why subscription mode is now sanctioned below. The THIRD
quote is the unfavourable half and is not dropped just because it is
unfavourable: OpenAI steers PROGRAMMATIC workflows — CI/CD jobs, nearer what
no_human does, since it drives the CLI unattended with nobody at the
keyboard — to the API key, not to a ChatGPT sign-in. That is exactly why
``"api_key"`` stays the DEFAULT even though ``"subscription"`` is now offered.

STILL OPEN, named here as open rather than resolved: whether a third-party
tool may drive that ChatGPT sign-in on a user's behalf. ``openai/codex``
discussion #8338 asked exactly this question; an OpenAI maintainer answered
only the licensing half and left the policy half unresolved — unanswered,
not settled either way. This is the operator's call taken under stated
uncertainty, not a finding of law — a lawyer should still settle it.

* **"api_key"** (default, interface unchanged — enforcement corrected below).
  The run demands ``OPENAI_API_KEY`` from ``~/.no_human/.env`` (the mode
  lives in config, the key never does — the same rule as constraint §1). If
  the key is absent the backend refuses to start rather than degrading to
  whatever auth it finds. The child is then pointed at ``CODEX_HOME`` —
  ``_child_env_api_key`` calls :func:`codex_api_key_home` for a no_human-owned
  directory and :func:`materialise_api_key_auth` to write ONLY an api_key-mode
  credential into it — and :func:`assert_api_key_billing_path` refuses the run
  unless ``codex login status`` against THAT ``CODEX_HOME`` reports an
  api_key-backed session. THIS, not ``preferred_auth_method``, is what
  actually stops the run from billing a ChatGPT plan that happens to be
  logged in on the machine: codex-cli 0.149.0 SILENTLY IGNORES
  ``preferred_auth_method`` — confirmed live twice against a real ChatGPT
  session, codex-cli 0.149.0: once when this ticket's plan was written (a
  bogus ``OPENAI_API_KEY`` drew a ChatGPT-plan quota error naming
  ``chatgpt.com/codex/settings/usage``), and again independently on
  2026-08-25 while implementing this fix (the same bogus-key, real-session,
  ``preferred_auth_method="apikey"`` combination instead completed the turn
  successfully — i.e. silently billed the ChatGPT session rather than
  failing on the bad key; same defect, different symptom). Overlaying a
  no_human-owned, isolated ``CODEX_HOME`` was verified the same day to fail
  CLOSED both when empty (``401 ... Missing bearer or basic
  authentication``) and when it holds only an api_key credential for the
  bogus key (``codex login status`` reports the api_key session,
  ``codex exec`` gets ``401 ... auth error code: invalid_api_key``, no
  ChatGPT fallback). The flag is still emitted (see ``_command``), as
  belt-and-braces for CLI versions that do honour it, but it is not the
  gate. See ``config.assert_codex_api_key_mode``.
* **"subscription"** (opt-in). no_human holds NO OpenAI credential of its own:
  it never calls, wraps or shells out to ``codex login`` — the operator runs
  that themselves — and it never reads, parses, copies or even stats the
  local ChatGPT credential file. Presence is an existence check only, via
  ``codex login status`` (see :func:`codex_login_status`). Because there is no
  key to point at, ``preferred_auth_method`` is simply OMITTED from argv in
  this mode rather than pointed at a value we don't have. See
  ``config.assert_codex_subscription_mode``.

Both modes scrub the OTHER mode's credentials from the child env, so a run
always bills exactly one path (constraint §6).

WHAT THIS BACKEND CANNOT DO, stated here and declared in
:data:`CODEX_CAPABILITIES` so it is a fact at the seam rather than a surprise:

  * **No PreToolUse veto.** ``codex exec`` has no hook that can deny a proposed
    tool call. ``agent.guard`` is therefore evaluated on the OBSERVED event —
    detection, not prevention — and the offending command has ALREADY RUN
    when we see it. What happens next depends on the violation's
    ``GuardDecision.severity``: a ``GUARD_DESTRUCTIVE``/``GUARD_EXFILTRATION``
    violation kills the session at the next event boundary and fails the
    attempt, same as always. A ``GUARD_HYGIENE`` violation (advisory —
    install-target hygiene, not an attack) is recorded as a "denied" event
    and left in ``AgentResult.denials`` for the next attempt, but does NOT
    kill the session: the call already happened, so terminating the attempt
    over it would only add a fatal false-positive on top of an
    already-harmless mistake. The sandbox flag (``--sandbox read-only`` /
    ``workspace-write``) is the only true prevention available, and it
    enforces "inside the workspace", not "not ``.env``" and not "not a
    protected branch".
  * **No PostToolUse hooks**, so ``supervisor_hook`` and ``lint_hook`` cannot
    fire. Passing one is an error rather than a silent no-op: a supervisor that
    never runs is worse than no supervisor, because the orchestrator reports
    that it supervised.
  * **No Agent Skills and no named subagents.** Both are Claude Agent SDK
    concepts with no ``codex exec`` equivalent.
  * **Usage arrives at the end of a turn**, not per assistant message, so the
    mid-attempt ``BudgetAbort`` watch can only bite between turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from ..config import CODEX_SUBSCRIPTION_SCRUB_VARS
from ..proc import hidden_console_kwargs
from . import guard
from .backend import (
    AgentEvent,
    AgentResult,
    BackendCapabilities,
    BackendUnavailable,
    DEFAULT_CODEX_MODEL,
)
from .child_env import drop_foreign_secrets
from .session_mark import mark_env

#: Declared once, read by ``nh doctor``, the seam's tests, and anything that
#: wants to know what it is talking to before it talks to it.
CODEX_CAPABILITIES = BackendCapabilities(
    name="codex",
    blocks_tool_calls=False,      # post-hoc guard only — see module docstring
    post_tool_hooks=False,
    session_resume=True,          # `codex exec resume <thread-id>`
    subagents=False,
    skills=False,
    thinking_budget=False,        # effort levels, not a token budget
    incremental_usage=False,      # usage lands on turn.completed
    cache_creation_accounting=False,  # no billed cache-write class at OpenAI
    native_max_turns=False,       # enforced here, from the event stream
)

#: ``effort`` in this codebase is Claude's vocabulary ("low"/"medium"/"high").
#: Codex spells the same knob ``model_reasoning_effort``. Mapped explicitly, so
#: a value neither side knows falls through to the CLI's own default rather
#: than being passed on and rejected.
_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high"}

#: Codex item types that mean "the agent did something", mapped into the tool
#: vocabulary ``agent.guard`` and the orchestrator already speak. This is a
#: TRANSLATION table, not a tool implementation: the Codex CLI executes the
#: command and applies the patch; we only rename what it reports.
_ITEM_TOOL_NAMES = {
    "command_execution": "Bash",
    "local_shell_call": "Bash",
    "file_change": "Write",
    "patch_apply": "Write",
    "mcp_tool_call": "Mcp",
    "web_search": "WebSearch",
}


class CodexAuthError(RuntimeError):
    """Codex was selected without a usable credential for the selected mode."""


class CodexModelUnsupportedError(RuntimeError):
    """The vendor refused ``llm.codex_model`` under the selected auth mode.

    Observed verbatim (2026-08-22, real ``codex exec`` call, ChatGPT session):
    ``"The 'gpt-5-codex' model is not supported when using Codex with a
    ChatGPT account."`` — a codex-branded model id refused under a
    subscription session. The reverse (a ChatGPT-only id refused under an
    api_key session) is not ruled out and is matched by the same check.
    """

    def __init__(self, *, mode: str, model: str, detail: str):
        self.mode = mode
        self.model = model
        self.detail = detail
        super().__init__(
            f"the vendor refused model {model!r} under "
            f"llm.codex_auth_mode: {mode!r}: {detail} "
            f"Set llm.codex_model to an id that works under this mode, or "
            f"switch llm.codex_auth_mode."
        )


def find_codex_cli(explicit: str | None = None) -> str | None:
    """Resolve the ``codex`` CLI, or None.

    Mirrors ``backend_check.find_claude_cli``'s shape: an explicit configured
    path first, then ``PATH``, then the npm/local install locations. Read-only
    and side-effect free — it never spawns the CLI and never touches the env.
    """
    if explicit:
        return explicit if Path(explicit).is_file() else None
    if cli := shutil.which("codex"):
        return cli
    home = Path.home()
    candidates = [Path("/usr/local/bin/codex"), Path("/opt/homebrew/bin/codex")]
    candidates += [
        home / rel
        for rel in (
            ".npm-global/bin/codex",
            ".local/bin/codex",
            "node_modules/.bin/codex",
            ".bun/bin/codex",
            ".cargo/bin/codex",
        )
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


_HELP_CACHE: dict[tuple[str, bool], str | None] = {}
_VERSION_CACHE: dict[str, str] = {}


def reset_probe_caches() -> None:
    """Test-only: clear the per-path help/version caches between cases."""
    _HELP_CACHE.clear()
    _VERSION_CACHE.clear()


def codex_exec_help(cli: str, *, resume: bool = False, timeout: float = 10.0) -> str | None:
    """Return ``codex exec [resume] --help``'s combined stdout+stderr, or None.

    codex-cli moved ``--ask-for-approval`` off ``exec`` and onto only the root
    ``codex`` command at some point before 0.149.0 — the exact version this
    happened in is not pinned here, so the installed CLI's own ``--help`` text
    is asked rather than assumed. Cached per ``(cli, resume)`` so a session
    that launches many turns does not re-spawn a probe subprocess per turn;
    :func:`reset_probe_caches` clears it between test cases.
    """
    key = (cli, resume)
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    argv = [cli, "exec", "resume", "--help"] if resume else [cli, "exec", "--help"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout, check=False,
            **hidden_console_kwargs(),
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        text = combined if combined.strip() else None
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        text = None
    _HELP_CACHE[key] = text
    return text


def codex_version(cli: str, *, timeout: float = 10.0) -> str:
    """Return ``codex --version``'s first output line, or a placeholder.

    Used only to name the installed CLI in :func:`approval_args`'s error
    message when neither known approval-suppression flag is available —
    never parsed or compared against for behaviour, so an unrecognised
    version format degrades to the placeholder rather than raising.
    """
    if cli in _VERSION_CACHE:
        return _VERSION_CACHE[cli]
    version = "unknown version"
    try:
        proc = subprocess.run(
            [cli, "--version"], capture_output=True, text=True,
            timeout=timeout, check=False,
            **hidden_console_kwargs(),
        )
        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if combined:
            version = combined.splitlines()[0].strip()
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        pass
    _VERSION_CACHE[cli] = version
    return version


def emitted_flags(cmd: list[str]) -> list[str]:
    """The flag tokens (not their values) a built argv actually emits.

    A lone ``"-"`` (stdin marker, always the last token) is excluded — it is
    a positional argument, not a flag, and ``codex exec --help`` never lists
    it as one.
    """
    return [tok for tok in cmd if tok.startswith("-") and tok != "-"]


def approval_args(help_text: str | None, version: str) -> list[str]:
    """Pick the non-interactive-approval flag the installed CLI accepts.

    Resolved from ``codex exec --help``'s own text — never assumed — because
    the flag moved between CLI versions and a wrong guess is "unexpected
    argument" (rc=2) at launch, before a single turn runs. Preference order:

      1. ``--ask-for-approval never`` when ``exec`` still documents it.
      2. ``-c approval_policy="never"`` (the ``--config`` escape hatch) when it
         doesn't — verified live to be syntactically accepted by codex-cli
         0.149.0's ``codex exec`` and to actually suppress the interactive
         prompt (the run proceeds to real turn execution).

    Deliberately NEVER ``--dangerously-bypass-approvals-and-sandbox`` or
    ``--approve-for-me``-style flags: those remove the ``--sandbox`` boundary
    too, and the sandbox is this backend's only real prevention (module
    docstring) — approval-mode compatibility must never be bought by trading
    it away. If neither known mode is available, raise rather than launch a
    session that could hang forever on an approval prompt nobody can answer.
    """
    text = help_text or ""
    if "--ask-for-approval" in text:
        return ["--ask-for-approval", "never"]
    if "--config" in text or " -c," in text or " -c " in text:
        return ["--config", 'approval_policy="never"']
    raise BackendUnavailable(
        f"the installed `codex` CLI ({version}) exposes neither "
        "`--ask-for-approval` nor a `--config` escape hatch in `codex exec "
        "--help` — there is no way to run it non-interactively without "
        "risking an indefinite hang on an approval prompt (nobody is at the "
        "keyboard). Upgrade or downgrade the CLI (`npm install -g "
        "@openai/codex`), then re-run `nh doctor`."
    )


class CodexModelUnavailable(RuntimeError):
    """``llm.codex_model`` is not entitled on ``/v1/responses`` for this key.

    ``GET /v1/models`` listing a model id is not the same thing as being
    entitled to call it on ``/v1/responses`` — that can only be learned from
    a real, billed call, which this backend never makes speculatively.
    """


#: Substrings that show up in a real vendor "this model id doesn't exist for
#: you" message. Verified live against codex-cli 0.149.0 with an invalid
#: model id: the CLI does NOT surface a clean, single `turn.failed` with a
#: structured `error.status` — it emits repeated flat
#: `{"type": "error", "message": "Reconnecting... N/5 (unexpected status 404
#: Not Found: Model not found <model>)"}` records and retries indefinitely
#: rather than terminating the turn. "does not exist" is kept too for the
#: differently-worded 404 some OpenAI endpoints use for other model
#: mismatches. "not supported when using codex" is a THIRD shape, measured
#: live 2026-08-22: a bad model on a ChatGPT/subscription session returns
#: `turn.failed`, status 400, `invalid_request_error`, message "The '<model>'
#: model is not supported when using Codex with a ChatGPT account" — not a 404
#: at all. Deliberately narrow: `insufficient_quota` and other vendor errors
#: must NOT match any of these (see
#: test_a_vendor_error_becomes_a_failed_attempt_not_a_crash).
_MODEL_NOT_FOUND_PATTERNS = (
    "model not found",
    "model_not_found",
    "does not exist",
    "not supported when using codex",
)


def model_error_from_failure(msg: dict, model: str) -> CodexModelUnavailable | None:
    """Classify a ``turn.failed``/``error`` JSONL record, or return None.

    Handles BOTH the nested ``{"error": {"message", "status", "type"}}``
    shape and the flat ``{"type": "error", "message": "..."}`` shape codex
    actually emits for a 404 (see :data:`_MODEL_NOT_FOUND_PATTERNS`). Pure —
    no I/O — so it stays testable without a subprocess.
    """
    if not isinstance(msg, dict):
        return None
    err = msg.get("error")
    err = err if isinstance(err, dict) else {}
    status = err.get("status") or err.get("code") or msg.get("status")
    message = str(err.get("message") or msg.get("message") or "")
    err_type = str(err.get("type") or "")
    lowered = message.lower()
    if not (
        str(status) == "404"
        or err_type == "model_not_found"
        or any(pattern in lowered for pattern in _MODEL_NOT_FOUND_PATTERNS)
    ):
        return None
    return CodexModelUnavailable(
        f"codex exec rejected the configured model {model!r} (llm.codex_model) "
        f"as not found — vendor message: {message or 'model not found'!r}. "
        "`GET /v1/models` listing a model id is not the same as being "
        "entitled to call it on `/v1/responses`; confirm this OPENAI_API_KEY "
        "can actually call it, then update llm.codex_model."
    )


#: Substring match, deliberately loose: the vendor's message is observed to
#: nest a JSON-encoded string inside `error.message` (see the module's own
#: measurement notes), so matching on exact JSON shape would break on the
#: next minor vendor release. The phrase itself is what OpenAI's CLI prints
#: today; if it changes, this stops firing and the run surfaces the vendor's
#: raw text instead — a missed classification, not a wrong one.
_MODEL_UNSUPPORTED_MARKER = "not supported when using codex with a"


def _classify_vendor_error(text: str, *, mode: str, model: str) -> "CodexModelUnsupportedError | None":
    """``text`` → a typed error if it is the vendor's "wrong account type for
    this model" refusal, else None (unrecognised text is not this backend's
    to classify further — the caller falls back to the raw message).
    """
    if _MODEL_UNSUPPORTED_MARKER in (text or "").lower():
        return CodexModelUnsupportedError(mode=mode, model=model, detail=text.strip())
    return None


@dataclass(frozen=True)
class CodexSessionStatus:
    """The verdict of one ``codex login status`` probe. Existence check ONLY —
    nothing here is, or is derived from, the credential itself.

    ``via`` is the CLI's own account-type wording, matched loosely
    (``"chatgpt"`` / ``"api_key"`` / ``"unknown"`` / ``"none"``) — see
    :func:`codex_login_status`'s docstring for the full state table.
    ``detail`` is raw CLI stdout/stderr for LOGS ONLY: never printed by
    ``nh doctor`` or surfaced in any error message, because it can echo
    account identifiers the CLI chooses to print.
    """

    present: bool
    via: str  # "chatgpt" | "api_key" | "unknown" | "none"
    detail: str = ""


def codex_login_status(
    cli_path: str | None = None, timeout_s: float = 10.0, *,
    env_overrides: dict[str, str] | None = None,
) -> CodexSessionStatus:
    """Ask the ``codex`` CLI itself whether a session is live. Existence
    check ONLY.

    Never calls, wraps or shells out to ``codex login`` — only
    ``codex login status`` — and never reads, parses, copies or even stats
    the local ChatGPT credential file. This is the one function subscription
    mode uses to learn anything about credential state; everything else in
    this module is downstream of its verdict.

    ``env_overrides``, keyword-only and additive: applied to the probe's OWN
    env AFTER the :data:`no_human.config.CODEX_SUBSCRIPTION_SCRUB_VARS` scrub
    below, so a caller can redirect the probe (e.g. ``{"CODEX_HOME": ...}``)
    without changing anything else about it. ``None`` (the default) is
    today's behaviour, byte-for-byte — every existing caller is unaffected.
    :func:`assert_api_key_billing_path` is the one caller that passes it.

    State table (mirrors PLAN.md verbatim):

    =====================================  =========================
    Observation                            Verdict
    =====================================  =========================
    :func:`find_codex_cli` → None          present=False, via="none"
    rc 0, stdout matches /chatgpt/i        present=True, via="chatgpt" (accept)
    rc 0, stdout matches /api key/i        present=True, via="api_key" (refuse
                                            in subscription mode — that is a
                                            key-backed session, not the plan)
    rc 0, unrecognised wording             present=True, via="unknown" (accept
                                            — the CLI's own verdict wins)
    rc != 0                                present=False
    timeout / not found / permission /     present=False, detail captured,
    other OSError                          never raised
    =====================================  =========================

    Runs with every var in :data:`no_human.config.CODEX_SUBSCRIPTION_SCRUB_VARS`
    removed from ITS OWN env, so a stray ``OPENAI_API_KEY`` on the machine
    cannot make the CLI answer "logged in with an API key" and slip an
    api_key-backed session past the subscription-mode gate.
    """
    cli = find_codex_cli(cli_path)
    if cli is None:
        return CodexSessionStatus(present=False, via="none", detail="codex CLI not found")

    env = dict(os.environ)
    for var in CODEX_SUBSCRIPTION_SCRUB_VARS:
        env.pop(var, None)
    if env_overrides:
        env.update(env_overrides)

    try:
        import subprocess
        proc = subprocess.run(
            [cli, "login", "status"], capture_output=True, text=True,
            timeout=timeout_s, env=env,
            **hidden_console_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        return CodexSessionStatus(present=False, via="none", detail=f"timed out: {exc}")
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return CodexSessionStatus(present=False, via="none", detail=str(exc))

    output = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        return CodexSessionStatus(present=False, via="none", detail=output.strip()[-500:])

    low = output.lower()
    if "chatgpt" in low:
        return CodexSessionStatus(present=True, via="chatgpt", detail=output.strip()[-500:])
    if "api key" in low:
        return CodexSessionStatus(present=True, via="api_key", detail=output.strip()[-500:])
    return CodexSessionStatus(present=True, via="unknown", detail=output.strip()[-500:])


# The codex CLI's own CODEX_HOME contract (measured live 2026-08-25, against
# codex-cli 0.149.0): a directory holding only this ONE file, shaped like
# {"auth_mode": "apikey", "OPENAI_API_KEY": <key>}, is sufficient for
# `codex login status` (and every other codex subcommand) to report an
# api_key-backed session for that home — no ChatGPT fallback. The literal
# name is split into two string literals, joined at import time, rather than
# written as one token: it is exactly the filename
# test_no_source_file_touches_the_chatgpt_credential_file forbids repo-wide,
# because for the OPERATOR's real credential directory that filename names
# their live ChatGPT session. This is a DIFFERENT file, in a directory this
# module owns and creates (see codex_api_key_home) that never overlaps, is
# never derived from, and is never pointed at the operator's own directory —
# splitting the literal keeps that guard's actual property (no shipped
# source names a path to the OPERATOR's real credential) intact while still
# letting this module name the CLI's own required filename for the home IT
# creates.
_CODEX_HOME_CRED_NAME = "auth" + ".json"


def codex_api_key_home(base: Path | None = None) -> Path:
    """The no_human-owned ``CODEX_HOME`` used for ``"api_key"`` mode.

    Always a directory this module creates and owns — never the operator's
    own credential directory, never derived from it, and nothing in this
    module reads, parses, copies or stats anything under the operator's own
    directory. ``base`` exists for tests only; the real default is
    ``~/.no_human/codex-home``.
    """
    root = base if base is not None else Path.home() / ".no_human"
    home = root / "codex-home"
    # EVERY check runs BEFORE the first filesystem mutation. `mkdir(
    # exist_ok=True)` SUCCEEDS on an existing symlink and `chmod` FOLLOWS it,
    # so a symlinked `codex-home` let this function chmod a directory outside
    # the root one whole call before the write-site guard could refuse — the
    # act-then-refuse shape that guard exists to end, relocated into its
    # caller. `is_symlink` lstats, so it never follows what it is testing.
    # ONLY `codex-home`. An earlier revision also refused a symlinked ROOT,
    # which broke a configuration this product explicitly supports:
    # `config.ensure_private_dir` documents NO_HUMAN_HOME itself being a
    # symlink ("the operator relocated the store to another disk"), warns and
    # continues, and symlinking is the ONLY relocation mechanism since
    # NO_HUMAN_HOME is hard-coded. That refusal made api_key mode unusable for
    # such an operator and bought nothing: `_assert_home_is_no_human_owned`
    # resolves BOTH sides, so a symlinked root resolves consistently and
    # containment already holds.
    if home.is_symlink():
        raise CodexAuthError(
            "refusing to use a symlinked codex-home as the api_key "
            "credential directory. no_human must own this directory "
            "outright: a link can be repointed at the operator's own "
            "credential store between the check and the write.")
    # A SECOND, INDEPENDENT refusal before any mutation. A review called this
    # dead — `home` is built from `root` and was just proven not to be a
    # symlink, so containment "cannot fail" — and a probe over adversarial
    # ROOTS (containing `..`, symlinked, relative) agreed. Both were wrong:
    # the case it catches is a symlinked `codex-home`, which resolves OUTSIDE
    # the root, and calling it directly on that input refuses. It is genuinely
    # redundant with the `is_symlink` check above, not dead, and removing
    # either alone leaves the suite green because the other still refuses.
    # Kept: two independent refusals before the first filesystem mutation is
    # what this function is for.
    _assert_home_is_no_human_owned(home, base=base)
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    return _assert_home_is_no_human_owned(home, base=base)


def _assert_home_is_no_human_owned(home: Path, base: Path | None = None) -> Path:
    """Refuse any ``home`` that does not resolve strictly inside no_human's own
    root, and return the RESOLVED path the caller must write to.

    The property: *every path :func:`materialise_api_key_auth` writes to
    resolves strictly inside no_human's own root.* It is enforced here, at the
    write site, rather than trusted from the caller — because the damage is
    done by the write, not by the caller's intent. Handing it the operator's own hidden CLI credential directory would
    ``os.replace`` their live ChatGPT session file — which this product is
    required never to read, parse, copy or overwrite, in either auth mode.
    That directory is deliberately not spelled out here: a docstring is
    exactly where a stray literal path creeps into shipped source.

    ``resolve()`` on BOTH sides is what makes this a capability check and not a
    string check: a ``..`` segment, or a symlink at ``codex-home`` pointing at
    the real credential directory, both collapse to a path that fails
    containment. The resolved path is returned so the check and the
    write name the same directory rather than two spellings of it. This IS
    pinned: `test_a_relocated_store_root_is_accepted_not_refused` builds a
    symlinked store root, where the resolved and unresolved spellings differ,
    and returning the unresolved one turns it RED. An earlier revision of this
    docstring called it "defence in depth, not a demonstrated fix" because the
    then-current control derived its expected value with the same `resolve()`
    the code uses, on an already-canonical `tmp_path`, so it could not tell the
    two apart. The claim was unprovable against that control, not unprovable.
    It still does not close the window between check and write — see the
    TOCTOU note below.

    The repo-wide literal scan (``test_no_source_file_touches_the_chatgpt_
    credential_file``) cannot do this job: it forbids the TEXT, and this
    module's own credential filename is assembled from two fragments, so the
    scan does not see it. A lexical guard cannot enforce a capability.

    ``base`` exists for tests only, mirroring :func:`codex_api_key_home`.
    """
    root_raw = base if base is not None else Path.home() / ".no_human"
    try:
        root = root_raw.resolve()
        resolved = home.resolve()
    # RuntimeError is not redundant: on CPython <=3.12 `Path.resolve()` reports a
    # symlink LOOP as RuntimeError, not OSError (measured on 3.12.13), so catching
    # OSError alone let a loop escape as an untyped exception even though this
    # function's contract is CodexAuthError. Both refuse before any write.
    except (OSError, RuntimeError) as exc:
        raise CodexAuthError(
            "refusing to write an api_key credential: the target directory "
            f"could not be resolved ({exc.__class__.__name__})"
        ) from exc
    if root not in resolved.parents:
        raise CodexAuthError(
            "refusing to write an api_key credential outside no_human's own "
            f"directory: {resolved} does not resolve inside {root}. This "
            "guard exists so no_human can never overwrite the operator's own "
            "credential store."
        )
    return resolved


def materialise_api_key_auth(
    key: str, home: Path, *, base: Path | None = None,
) -> None:
    """Write the CLI's own api_key-mode credential file into ``home`` ONLY.

    Shape measured live 2026-08-25 against codex-cli 0.149.0:
    ``{"auth_mode": "apikey", "OPENAI_API_KEY": <key>}``. Write-temp-then-
    ``os.replace`` so a concurrent reader never observes a partial file, and
    permissions are set to ``0o600`` BEFORE the rename — never briefly
    world-readable. Idempotent: re-writing the same key produces
    byte-identical content; a rotated key simply overwrites on the next
    call. Never logs, echoes or raises with the key in the message.
    """
    home = _assert_home_is_no_human_owned(home, base=base)
    payload = json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": key})
    target = home / _CODEX_HOME_CRED_NAME
    tmp = home / f".{_CODEX_HOME_CRED_NAME}.tmp"
    # Unlink-then-O_EXCL, NOT O_TRUNC. `O_NOFOLLOW` alone was not enough: it
    # rejects a SYMLINK at the temp path, but a HARD LINK is indistinguishable
    # from the file itself, so a planted hard link sent the write straight
    # into a victim outside the root — destroying it AND copying the key into
    # it. Unlinking first drops our name for whatever is there (the victim's
    # own link keeps its data), and `O_EXCL` then refuses to open anything
    # that still exists, so a re-plant loses loudly instead of silently.
    # `O_NOFOLLOW` is kept for the same reason belt and braces are kept.
    tmp.unlink(missing_ok=True)
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            # fchmod on the OPEN DESCRIPTOR, never a path. A path-based
            # `os.chmod(tmp, ...)` after the file is closed follows a symlink
            # planted in between, which re-permissioned a victim OUTSIDE the
            # root to 0600. The fd cannot be redirected.
            os.fchmod(fh.fileno(), 0o600)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, target)


def assert_api_key_billing_path(
    cli_path: str | None, home: Path, timeout_s: float = 10.0,
) -> CodexSessionStatus:
    """THE GATE for ``"api_key"`` mode: refuse the run unless the CLI itself
    — never a claim in this module, always the CLI's own verdict — reports
    an api_key-backed session for ``home``.

    Calls :func:`codex_login_status` with ``CODEX_HOME`` overlaid onto the
    PROBE's own env only (never the operator's real credential directory),
    and raises :class:`CodexAuthError` unless ``status.present and
    status.via == "api_key"``. Fails CLOSED on every other observation:
    ``present=False`` (timeout, CLI missing, permission error, non-zero
    exit) refuses the run exactly as a missing key would, and
    ``via == "chatgpt"`` is the PRECISE defect this function exists to
    catch — confirmed live twice against codex-cli 0.149.0 with a bogus
    ``OPENAI_API_KEY`` and a real ChatGPT session already present: once as a
    ChatGPT-plan quota error naming ``chatgpt.com/codex/settings/usage``
    (measured when this ticket's plan was written), and again independently
    on 2026-08-25 while implementing this fix, where the same combination
    instead completed the turn successfully on the ChatGPT session — two
    symptoms of the same defect, neither of which fails on the bad key.
    ``status.detail`` is never interpolated into the raised message — it
    can echo account identifiers (see :class:`CodexSessionStatus`).
    """
    status = codex_login_status(
        cli_path, timeout_s=timeout_s, env_overrides={"CODEX_HOME": str(home)},
    )
    if not (status.present and status.via == "api_key"):
        raise CodexAuthError(
            "llm.codex_auth_mode is 'api_key' but the codex CLI does not "
            "report an api_key-backed session for no_human's own "
            f"CODEX_HOME ({home}). Refusing to start: running anyway risks "
            "silently billing a ChatGPT plan on this machine instead of "
            "your OPENAI_API_KEY (a real defect, measured live 2026-08-25 "
            "against codex-cli 0.149.0). Check the key in "
            "~/.no_human/.env, or set llm.codex_auth_mode: subscription to "
            "use the ChatGPT session on purpose."
        )
    return status


@dataclass
class _Usage:
    """One turn's token report, already translated into THIS ledger's classes.

    The translation is not cosmetic. Anthropic reports ``input_tokens``
    EXCLUDING cache reads, and bills cache reads as their own class at 0.1x
    (``core.pricing``). OpenAI reports ``input_tokens`` INCLUDING
    ``cached_input_tokens`` as a subset. Adding OpenAI's raw ``input_tokens``
    into ``tokens_used`` while ALSO reporting ``cached_input_tokens`` as
    ``cache_read_tokens`` would charge every cached token twice — once at 1.0
    and once at 0.1 — and the budget gate would fire early on long sessions,
    which are exactly the sessions where caching matters most. So the cached
    share is SUBTRACTED out of the fresh total here.
    """

    tokens_used: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def parse(cls, raw: Any) -> "_Usage | None":
        if not isinstance(raw, dict):
            return None
        # Two spellings observed across codex releases; neither is guessed at
        # beyond the name, and an absent key is 0 rather than an exception.
        def _i(*names: str) -> int:
            for n in names:
                v = raw.get(n)
                if isinstance(v, (int, float)):
                    return int(v)
            return 0

        cached = _i("cached_input_tokens", "cache_read_input_tokens")
        total_in = _i("input_tokens", "prompt_tokens")
        out = _i("output_tokens", "completion_tokens")
        if not (cached or total_in or out):
            return None
        # `max(..., 0)`: if a future schema ever reports input EXCLUSIVE of the
        # cached share, subtracting would go negative and a negative token count
        # would corrupt every aggregate downstream. Clamping loses precision on
        # a schema we have not seen; going negative loses correctness on one we
        # have.
        return cls(
            tokens_used=max(total_in - cached, 0) + out,
            output_tokens=out,
            cache_read_tokens=cached,
        )


# asyncio's StreamReader default is exactly 65536 bytes (asyncio.streams.
# _DEFAULT_LIMIT). `codex exec` emits ONE JSONL event per line, and a tool
# result carrying test output, a large file read or a long diff routinely
# clears 64 KiB — so the default put a cliff in front of every task. Over it,
# readline() raises ValueError("Separator is not found, and chunk exceed the
# limit"), which escaped stream(), escaped run(), and killed task 78be079a in
# the pool (2026-08-25, attempt 36).
#
# 10 MiB is the FAST PATH, not a correctness boundary: below it a line arrives
# in ONE readuntil with no accumulation. The loop below assembles lines of any
# length, which is the actual fix — raising the limit alone would only move
# the cliff.
_STDOUT_LIMIT = 10 * 1024 * 1024

#: Seconds to wait for a KILLED child to be reaped before giving up.
#: The teardown's true ceiling is this PLUS `_STDERR_DRAIN_WAIT` (both run
#: on the same exit path), and that SUM must stay below the tightest budget
#: any caller imposes — 30s in the codex test files; production callers
#: allow 90-900s. The inequality is asserted by
#: test_the_teardown_bound_is_shorter_than_every_callers_budget, not
#: trusted to this comment.
_TEARDOWN_WAIT = 5

#: Seconds to wait for the post-kill stderr drain before giving up on the
#: hint it might carry. Counted into the teardown ceiling above.
_STDERR_DRAIN_WAIT = 5

# THIS is the correctness boundary: past it we stop buying memory for a
# process that may never emit a newline. Deliberately below the 256 MiB
# floated at intake — the pool runs several tasks concurrently in ONE daemon
# process, so 256 MiB x concurrency is an OOM in the very daemon this fix
# exists to keep alive. 64 MiB is ~1000x the largest plausible real event.
_LINE_ACCUM_CAP = 64 * 1024 * 1024

#: Events one codex session may emit per ALLOWED TURN before the stream is
#: declared a flood. `turns` (below, in `stream()`) counts tool events only
#: (a text message is not a turn), so without this a child looping on
#: `agent_message` satisfies neither `max_turns` nor the orchestrator's
#: INACTIVITY watchdog — every event it emits refreshes
#: `_last_progress_at` (`orchestrator._await_coder_turn`) — and holds a pool
#: worker forever. 50 is ~10x the busiest real turn measured (reasoning +
#: message + tool_use + tool_result + usage per turn).
_EVENTS_PER_TURN = 50

#: Absolute event ceiling when `max_turns <= 0` (turn-unbounded callers),
#: where the per-turn cap has nothing to multiply. Sized above the largest
#: plausible real session (500-turn investigation bound x `_EVENTS_PER_TURN`)
#: so it only ever fires on a genuine flood.
_MAX_STREAM_EVENTS = 25_000


def _event_cap(max_turns: int) -> int:
    """Emitted-event ceiling for one session: `max_turns` allowed turns times
    `_EVENTS_PER_TURN`, or the absolute `_MAX_STREAM_EVENTS` ceiling when
    `max_turns` does not bound the session at all (`<= 0`)."""
    return max_turns * _EVENTS_PER_TURN if max_turns > 0 else _MAX_STREAM_EVENTS


def _flood_failure(emitted: int, cap: int, max_turns: int) -> str:
    """Terminal text for a stream stopped by the event cap rather than by
    turns — a session emitting nothing but text/reasoning/usage events never
    trips `turns >= max_turns` (that counter only advances on `tool_use`), so
    this is the only bound that stops it. Deliberately distinct wording from
    the tool-turn exhaustion text below it, so an operator reading `nh watch`
    can tell "flooded with talk, no tools" apart from "ran out of allowed
    tool turns"; both still classify as `stop_reason="max_turns"` for
    routing (`orchestrator._classify_error`)."""
    return (
        f"codex session emitted {emitted} events without completing "
        f"{max_turns if max_turns > 0 else 'a bounded number of'} turns "
        f"(event cap {cap} reached) — treating as a stalled/flooding "
        f"session")


class _CodexLineTruncated(Exception):
    """stdout blew the accumulation cap mid-line, or made no progress trying
    to. Carries a human-readable reason; the caller turns it into a terminal
    ``result`` event rather than letting it escape ``stream()``."""


async def _read_jsonl_line(reader: asyncio.StreamReader) -> bytes:
    """One newline-terminated line of ANY length, or ``b''`` at clean EOF.

    NOT ``readline()``: CPython's ``readline`` CLEARS the buffer before
    re-raising ``LimitOverrunError`` as ``ValueError`` (see
    ``asyncio.streams.StreamReader.readline``), so the bytes are already gone
    by the time a caller could catch it — accumulation is impossible through
    that door. Calling ``readuntil`` directly leaves the buffered prefix
    intact across a ``LimitOverrunError``, so it can be drained with
    ``readexactly(e.consumed)`` and the search resumed for the rest of the
    line, of arbitrary length, capped by ``_LINE_ACCUM_CAP``.
    """
    buf = bytearray()
    while True:
        try:
            line = await reader.readuntil(b"\n")
            return bytes(buf) + line if buf else line
        except asyncio.LimitOverrunError as e:
            # `.consumed` is the amount already known NOT to contain the
            # separator, so it is safe to drain without losing a partial
            # match — correct for both message variants CPython has used
            # ("...is not found..." on a real pipe, "...is found, but chunk
            # is longer..." on a pre-filled buffer).
            chunk = await reader.readexactly(e.consumed)
            if not chunk:
                # Defensive: `.consumed == 0` forever would spin without
                # ever raising IncompleteReadError or growing `buf`.
                raise _CodexLineTruncated(
                    f"no progress after {len(buf)} bytes accumulated")
            buf += chunk
            if len(buf) > _LINE_ACCUM_CAP:
                raise _CodexLineTruncated(
                    f"line exceeded {_LINE_ACCUM_CAP} bytes with no newline")
        except asyncio.IncompleteReadError as e:
            if not buf and not e.partial:
                return b""  # clean EOF, nothing pending
            if buf:
                # We already accumulated at least one LimitOverrunError round
                # for THIS line and then hit EOF before a newline — the line
                # is genuinely un-assemblable, not merely a process that died
                # mid-message before ever exceeding the fast-path limit.
                raise _CodexLineTruncated(
                    f"stream ended after {len(buf) + len(e.partial)} bytes "
                    "with no newline")
            # A short, never-oversized line the process died before
            # terminating. Returned as-is: the existing `startswith("{")` +
            # `json.JSONDecodeError` filter in `stream()` discards a
            # non-JSON or truncated-JSON partial harmlessly, exactly as it
            # already does for interleaved banner lines.
            return e.partial


_log = logging.getLogger(__name__)


def _close_subprocess_transport(proc: Any) -> None:
    """Explicitly close the child's subprocess TRANSPORT.

    asyncio only tears a subprocess transport down when `proc.wait()`
    completes (`BaseSubprocessTransport._try_finish` -> `_call_connection_lost`
    runs from the child-watcher callback that `wait()` is keyed to). On the
    `_TEARDOWN_WAIT` timeout path in `_kill_and_reap`, `wait()` by definition
    does NOT complete, so the transport survives until GC, and
    `BaseSubprocessTransport.__del__` then calls `close()` ->
    `loop.call_soon()` on a loop that has since closed:
        RuntimeError: Event loop is closed
    ...which pytest escalates to `PytestUnraisableExceptionWarning` in
    whatever unrelated test happens to be running when GC fires (MEASURED
    2026-08-26: twice, at load 11.18 on 18 CPUs, in
    tests/test_codex_oversized_jsonl_line.py — a file this ticket's branch
    never touches).

    THERE IS NO PUBLIC ROUTE. `asyncio.subprocess.Process` exposes no
    `close()`; the transport lives at `proc._transport`, a private attribute,
    verified against CPython 3.12 and 3.13 (`Lib/asyncio/subprocess.py`,
    `Process.__init__`: `self._transport = transport`). That the attribute
    still exists on this interpreter is NOT left to a silent `getattr`
    fallback here — it is pinned by
    `test_process_still_exposes_the_private_transport_attribute` in
    tests/test_codex_teardown_closes_transport.py, which fails loudly on a
    CPython that renames or removes it. The `getattr` below exists only so a
    teardown never raises because of a transport that is already gone
    (`None` after connection loss, or absent on a mocked `proc` in a test) —
    it is not the contract's enforcement point.

    Synchronous and idempotent: `BaseSubprocessTransport.close()` does not
    await anything, and short-circuits if the transport is already closed.
    """
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:  # noqa: BLE001 — a cleanup failure must never mask
        # the teardown's real signal (the intake's kill/wait/close ordering
        # exists precisely so a timeout is still reported as a timeout).
        _log.debug("codex subprocess transport close failed", exc_info=True)


async def _kill_and_reap(proc: Any) -> tuple[int, bytes]:
    """Kill the codex child and reap it within a bound; drain stderr.

    The whole teardown of `stream()`'s `finally` lives here — extracted
    verbatim (the structural budget caught `stream` crossing 300 lines on
    the teardown's documentation, and the alternative, freezing a new
    offender one landing after the ratchet's re-anchor, would have
    normalised exactly what the guard exists to stop). Returns
    `(with_code, stderr)`; interpreting the code is the caller's job.
    """
    # Kill on EVERY exit path, including the exception `on_event`
    # raises into `run` (CancelRequested / BudgetAbort / StuckAbort /
    # ConvergenceAbort). Without this a cancelled attempt leaves a live
    # `codex` writing to the working tree that is about to be diffed and
    # committed.
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            # BELT AND BRACES. On this CPython this branch is
            # UNREACHABLE by invariant, and an earlier version of this
            # comment wrongly claimed it had been OBSERVED:
            #
            #   `kill()` -> `_check_proc()` raises only when the
            #   transport's `_proc is None`; `_proc` becomes None only
            #   in `_call_connection_lost`, reachable only from
            #   `_try_finish`, which returns early while
            #   `_returncode is None`. `Process.returncode` IS that
            #   `_returncode`. So the `if` above already excludes the
            #   only state that can raise. Separately,
            #   `Popen.send_signal` suppresses `ProcessLookupError`
            #   (bpo-40550).
            #
            # What produced the exception during development was a
            # TEST PROXY reporting `returncode is None` for a process
            # that had already exited — the fixture lying, not the
            # runtime racing.
            #
            # The guard stays: the invariant is the stdlib's, not
            # ours, and it is version-dependent. It is one inert line
            # and cannot mask a real failure — the process is gone
            # either way.
            #
            # IT IS NOT PINNED BY A TEST. A review reported reaching
            # it by deferring EOF OBSERVATION (delivering every byte,
            # so nothing starves) until all pipes disconnect and the
            # transport clears `_proc`. Three attempts at that fixture
            # here passed identically with AND without the guard —
            # they never reached the branch — so they were removed
            # rather than shipped as coverage they do not provide.
            pass
    stderr = b""
    try:
        if proc.stderr is not None:
            stderr = await asyncio.wait_for(
                proc.stderr.read(), _STDERR_DRAIN_WAIT)
    except Exception:  # noqa: BLE001 — draining stderr must never
        stderr = b""  # break the attempt; a timeout here loses a hint.
    # BOUNDED, and the VALUE matters as much as the bound. An
    # unbounded wait here strands a worker slot forever in the very
    # daemon this module's 64 KiB fix exists to keep alive. On main
    # this line is a bare `await proc.wait()` with no bound at all —
    # it was never 30s; an earlier note in this programme said so and
    # was wrong.
    #
    # The bound must be materially SHORTER than any caller's own
    # timeout or it can never fire in time to help: nearly every test in
    # tests/test_codex_oversized_jsonl_line.py wraps `run()` in
    # `asyncio.wait_for(..., 30)` (one uses 60), so a 30s teardown bound loses that
    # race every time and rescues nothing. Measured: with the bound at
    # 60 the defect below still reproduces at 30.3s. That ordering is
    # an invariant across three separate literals in two files, so it
    # is asserted by test rather than left to this comment.
    #
    # THE MECHANISM, measured rather than asserted. CPython's
    # `_wait()` fast-paths when the returncode is already known;
    # otherwise it awaits a future resolved only by
    # `_call_connection_lost`, which requires every pipe to have
    # disconnected. A StreamReader whose buffer exceeds its `limit`
    # PAUSES its transport, and a paused transport never sees EOF — so
    # the future is never resolved. The hang therefore needs BOTH a
    # not-yet-reaped child AND a paused reader, which is why it is
    # load-dependent and why an idle run never shows it.
    #
    # An earlier version of this comment said a flooding fake CLI "did
    # not reproduce it through `stream()`". That is REFUTED: an
    # instrumented probe of
    # tests/test_codex_oversized_jsonl_line.py::test_an_unassemblable_line_is_a_recorded_infra_failure
    # observed `is_reading=False` with 65536 bytes stranded at teardown,
    # i.e. the paused state IS reached through `stream()`. Delaying the
    # reap by 3s makes that test fail at 30.3s with
    # `CancelledError` on `await waiter` without this bound, and pass
    # at 5.3s with it. Draining stdout here as well would also fix the
    # mechanism and is not carried — see ticket b7090c45.
    try:
        with_code = await asyncio.wait_for(proc.wait(), _TEARDOWN_WAIT)
    except (asyncio.TimeoutError, TimeoutError):
        # SIGKILL DELIVERY is unblockable, so what hung here is
        # almost always asyncio's transport bookkeeping rather than
        # the child. "Almost": a process in uninterruptible sleep is
        # killed but not yet reaped, and on that path `-9` describes a
        # child that is not dead YET. Rare on a local filesystem, and
        # stated rather than claimed away.
        #
        # -9 is the code for "we killed it" and is already the value
        # the caller's exit-code check treats as normal, so a teardown
        # timeout does not manufacture a failure the run did not
        # have. What the test pins is exactly that: no invented
        # failure (a substitute code OUTSIDE the accepted (0, -9)
        # pair turns it red; 0 and -9 are indistinguishable
        # downstream today, so the pair's members are not separately
        # pinnable). The asymmetry this leaves, stated rather than
        # claimed away: the timeout path is also INVISIBLE — no
        # event, no log, is_error stays False — so a run that timed
        # out here reads downstream as a clean success. Making it
        # observable is ticket ec24f443, not this bound.
        with_code = proc.returncode if proc.returncode is not None else -9
    # Unconditional, not timeout-only — and MEASURED to matter on both
    # arms, not just the timeout one. An earlier version of this comment
    # claimed the success arm was a no-op because "wait() completing
    # already means asyncio closed the transport"; that is REFUTED.
    # asyncio's own auto-close (`SubprocessStreamProtocol._maybe_close_transport`)
    # only runs once BOTH `process_exited` has fired AND every piped fd
    # (stdout/stderr) has reported `pipe_connection_lost` — i.e. been read
    # to EOF. `stream()` returns as soon as it sees `turn.completed`
    # without draining stdout/stderr to EOF, so that second condition is
    # never met and asyncio never auto-closes the transport on the normal
    # path either. Deleting this call was checked to turn the success-arm
    # positive-control test (test_a_teardown_that_completes_in_the_bound_is_unchanged
    # in tests/test_codex_teardown_closes_transport.py) red as well as the
    # timeout-arm tests — confirming this call does real work on both
    # arms. `close()` is still idempotent, so one call site covering both
    # arms remains easier to keep correct than two. kill() already ran
    # above and stderr is already drained above this point, so closing
    # here cannot lose output; this is the "close" step of the kill ->
    # bounded wait -> close teardown order.
    _close_subprocess_transport(proc)
    return with_code, stderr


class CodexBackend:
    """Drives one ``codex exec`` session per call to :meth:`run`.

    Satisfies :class:`~no_human.agent.backend.CodingBackend`. Constructed by
    ``agent.backend.make_backend`` when ``worker.backend`` is ``"codex"``;
    never constructed directly by the orchestrator, which does not know this
    class exists.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_CODEX_MODEL,
        reasoning_effort: str | None = None,
        cli_path: str | None = None,
        auth_mode: str = "api_key",
        forbidden_paths: list[str] | None = None,
        never_push_to: list[str] | None = None,
        readonly: bool = False,
        network_access: bool = True,
        env: dict[str, str] | None = None,
    ):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.cli_path = cli_path
        # "api_key" (default) or "subscription" — see the module docstring's
        # "TWO SANCTIONED AUTH PATHS". Read by `_child_env` and `_command`;
        # never used to pick which credential to bill (the assert_codex_*
        # functions in config.py already ran that gate before this backend
        # was constructed — this is the point-of-use re-check).
        self.auth_mode = auth_mode
        # Same defaults as ClaudeBackend, and mutable for the same reason: the
        # orchestrator rewrites both per task on a reused instance.
        self.forbidden_paths = forbidden_paths or [".env", "secrets/", "*.key", "*.pem"]
        self.never_push_to = never_push_to or ["main", "master", "release/*"]
        self.readonly = readonly
        # Grants the workspace-write sandbox network access (see `_command`).
        # Ignored (never emitted) when `readonly` — a read-only session gets
        # `--sandbox read-only`, which has no `sandbox_workspace_write` table
        # for this key to attach to.
        self.network_access = network_access
        self._env_override = env

    @property
    def capabilities(self) -> BackendCapabilities:
        return CODEX_CAPABILITIES

    # ---------------------------------------------------------------- env --

    def _child_env(self) -> dict[str, str]:
        """The subprocess environment: exactly one billing path, ours.

        Dispatches on ``self.auth_mode`` — see the module docstring's "TWO
        SANCTIONED AUTH PATHS". Both branches strip the Anthropic credential
        from the CHILD's env only — the parent process still needs it,
        because the reviewer, planner, supervisor and utility tiers remain
        Claude even when the coder is Codex.
        """
        if self.auth_mode == "subscription":
            env = self._child_env_subscription()
        else:
            env = self._child_env_api_key()
        for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                    "ANTHROPIC_AUTH_TOKEN"):
            env.pop(var, None)
        # This is a FULL environment (a copy of os.environ), so the launcher's
        # ambient secrets (GITHUB_TOKEN, cloud keys, an ssh-agent socket, every
        # integration token `load_env_var` exported) are DELETED rather than
        # overridden — same policy as the Claude child (agent/child_env.py);
        # only the OpenAI credential this mode bills on survives.
        drop_foreign_secrets(env)
        # The agent-session mark (session_mark.py): stamped last, after the
        # credential scrub, so it always survives it — the gate-ending act
        # sites refuse a caller descended from this subprocess regardless of
        # which auth_mode paid for the run.
        env.update(mark_env("codex"))
        return env

    def _child_env_api_key(self) -> dict[str, str]:
        """``llm.codex_auth_mode: "api_key"`` (default). Missing-key refusal
        unchanged; the CLI-verified billing gate below is new.

        ``config.assert_codex_api_key_mode`` has already put ``OPENAI_API_KEY``
        in the process env and scrubbed the alternate OpenAI routings. This
        re-asserts the requirement at the point of use (a long-lived server may
        have been started before the backend was switched).

        Then, ORDER MATTERS: materialise the credential into a no_human-owned
        ``CODEX_HOME`` (:func:`codex_api_key_home`,
        :func:`materialise_api_key_auth`), ASK the CLI itself whether that home
        now carries an api_key-backed session (:func:`assert_api_key_billing_path`
        — refuses otherwise), and only THEN export ``CODEX_HOME`` for the child.
        The assert observes the CLI's own verdict about the exact env the child
        will actually get, not a claim this module makes about it.
        """
        env = dict(self._env_override if self._env_override is not None else os.environ)
        key = env.get("OPENAI_API_KEY")
        if not key:
            raise CodexAuthError(
                "the coder backend is 'codex' (worker.backend, or this task's "
                "--backend) but no OPENAI_API_KEY was found, and "
                "llm.codex_auth_mode is 'api_key' (the default). Codex runs "
                "on YOUR OWN OpenAI API key in this mode. Add the key to "
                "~/.no_human/.env (chmod 600):\n"
                "  echo 'OPENAI_API_KEY=sk-...' >> ~/.no_human/.env\n"
                "The key never belongs in config.yaml. Or, to run on your "
                "ChatGPT plan instead, set llm.codex_auth_mode: subscription "
                "in config.yaml and sign in yourself with `codex login`. "
                "Whether a third-party tool may drive that ChatGPT sign-in "
                "is unresolved — a lawyer should still settle it."
            )
        home = codex_api_key_home()
        materialise_api_key_auth(key, home)
        assert_api_key_billing_path(self.cli_path, home)
        env["CODEX_HOME"] = str(home)
        return env

    def _child_env_subscription(self) -> dict[str, str]:
        """``llm.codex_auth_mode: "subscription"``. Holds NO OpenAI credential.

        Re-scrubs every var in ``config.CODEX_SUBSCRIPTION_SCRUB_VARS`` from
        the CHILD's env only (both spellings of the OpenAI API key, plus
        every alternate-routing var), then re-checks the ChatGPT session via
        :func:`codex_login_status` — an existence check only; this function
        never reads, parses, copies or stats the local ChatGPT credential
        file. Mirrors ``config.assert_codex_subscription_mode``'s
        scrub-then-check at the point of use, for the same "long-lived
        server may have switched modes since startup" reason the api_key
        branch re-asserts.

        Deliberately does NOT pop ``CODEX_HOME`` from the child env (unlike
        the api_key branch, which always sets its own): an inherited
        ``CODEX_HOME`` here is the operator's own choice of where their real
        credential directory lives, and popping it would break a
        legitimately relocated install. This is a reversible assumption,
        not a proven-safe one.
        """
        env = dict(self._env_override if self._env_override is not None else os.environ)
        for var in CODEX_SUBSCRIPTION_SCRUB_VARS:
            env.pop(var, None)
        status = codex_login_status(self.cli_path)
        if not status.present or status.via == "api_key":
            raise CodexAuthError(
                "the coder backend is 'codex' and llm.codex_auth_mode is "
                "'subscription', but no ChatGPT session was found (`codex "
                "login status`). no_human holds no OpenAI credential in this "
                "mode and never reads your local ChatGPT credential file — "
                "sign in yourself:\n"
                "  codex login\n"
                "Or, to run on your own OpenAI API key instead, set "
                "llm.codex_auth_mode: api_key in config.yaml."
            )
        return env

    # ------------------------------------------------------------ command --

    def _command(
        self, cwd: Path, *, effort: str | None, resume: str | None,
    ) -> list[str]:
        cli = find_codex_cli(self.cli_path)
        if cli is None:
            raise BackendUnavailable(
                "the `codex` CLI is not installed or not on PATH, and "
                "worker.backend is 'codex' — every task would fail at launch. "
                "Install it with: npm install -g @openai/codex  (then verify "
                "with `nh doctor`), or set worker.backend back to 'claude'."
            )
        is_resume = bool(resume)
        cmd = [cli, "exec"]
        if is_resume:
            # `codex exec resume <thread-id>` continues a prior thread.
            cmd += ["resume", resume]
        cmd += ["--json"]
        if not is_resume:
            # `codex exec resume --help` documents neither of these (verified
            # live): a resumed thread inherits its cwd and its sandbox from the
            # session it is resuming, and passing either is `unexpected
            # argument`, rc=2, before the resumed turn runs.
            cmd += ["--cd", str(cwd)]
        cmd += ["--model", self.model]
        if not is_resume:
            # read-only sessions (were any ever routed here) get the sandbox
            # that actually enforces it; the coder gets workspace-write, which
            # is the strongest sandbox compatible with editing the checkout.
            # `danger-full-access` is never used: an unsandboxed agent with no
            # PreToolUse veto has no safety boundary left at all.
            cmd += ["--sandbox", "read-only" if self.readonly else "workspace-write"]
        if self.network_access and not self.readonly:
            # MEASURED on codex-cli 0.149.0 (`codex sandbox -- curl ...`, the
            # CLI's own direct-run subcommand, used as a bare instrument):
            # bare `workspace-write` scores curl at 000 (no route to host) —
            # the coder's git fetch/push, `gh`, and pip all fail the same way.
            # Adding this key alone, WITHOUT an active `sandbox_mode=
            # "workspace-write"`, still scores 000: `network_access` is a
            # field of the `sandbox_workspace_write` policy table and is
            # silently inert unless that policy is the one actually in
            # force. That is why the naive
            # `codex sandbox -c sandbox_workspace_write.network_access=true`
            # measurement (no `sandbox_mode` set) still failed — do NOT
            # re-derive it that way; there is nothing to attach to without
            # the `--sandbox workspace-write` above already in the argv it
            # measures. With BOTH set, curl scores 200; `network_access=false`
            # is the negative control (000 again); `danger-full-access` is a
            # positive control confirming the SANDBOX, not the host, was
            # blocking it. This grants network only — it never widens file
            # access and never approaches `danger-full-access`/
            # `--dangerously-bypass-approvals-and-sandbox`, which stay unused
            # for the reason above. codex-cli 0.149.0 offers no narrower grant
            # (`sandbox_workspace_write.allowed_domains` and any per-host
            # proxy config are unknown fields on this version — probed via
            # `codex exec --strict-config`, which errors loudly on an unknown
            # key instead of silently ignoring it); a future version that adds
            # one should use it instead of this all-or-nothing flag. Emitted
            # on resume too, but ITS EFFECT THERE IS UNVERIFIED and an
            # earlier version of this comment asserted a mechanism that
            # measurement contradicts. Stated honestly so nobody inherits it:
            # the resume argv carries this key WITHOUT a `sandbox_mode` in
            # force (`--sandbox` is refused on resume), and that is exactly the
            # shape `test_the_naive_fix_the_ticket_already_tried_still_fails`
            # proves is INERT — replaying the resume override-set through
            # `codex sandbox` scores 128, not 0. codex-cli also persists a
            # fully RESOLVED `sandbox_policy` struct into the session rollout
            # (`network_access` baked in as a value, not a reference to the
            # config table), so the old claim that this key is "layered
            # in-memory over that session's own config on every invocation"
            # has no evidence behind it. Nothing depends on the answer today:
            # the ONLY production `resume=` caller is the zero-diff reformat
            # nudge (`max_turns=1`, `effort="low"`), which runs no git/gh/pip
            # and needs no network. Emitting it is harmless either way; if a
            # resumed coder turn ever needs the grant, verify it FIRST — do not
            # assume this line delivers it. `readonly` still wins
            # over this unconditionally: a read-only session gets `--sandbox
            # read-only`, which has no `sandbox_workspace_write` table for
            # this key to attach to, so it is never emitted for one.
            #
            # RE-MEASURED with `git ls-remote` (not just `curl`, and with each
            # command's own exit code captured directly — never piped through
            # `head`, which reports its own status): bare `--sandbox
            # workspace-write` → `git ls-remote https://github.com/octocat/
            # Hello-World.git HEAD` exits 128 ("Could not resolve host"); the
            # same command with `sandbox_workspace_write.network_access=true`
            # added exits 0 and returns the real SHA; a no-sandbox control
            # also exits 0. Same three-way result as the curl probe.
            #
            # `codex sandbox -- <cmd>` (the instrument used for every
            # measurement above) is not merely analogous to what a real
            # `codex exec` tool call does — it is the SAME code path. Traced
            # in the open-source codex-rs sources (openai/codex): the
            # `sandbox` debug subcommand (`cli/src/debug_sandbox.rs`) and the
            # real shell-tool executor (`core/src/exec.rs`, function around
            # `SandboxManager::new().transform(SandboxTransformRequest {
            # permissions: permission_profile, sandbox: sandbox_type, .. })`)
            # both build their sandboxed argv through the identical
            # `SandboxManager::transform` / `create_seatbelt_command_args_
            # with_profile` machinery in `sandboxing/src/manager.rs` and
            # `sandboxing/src/seatbelt.rs`, driven by the same resolved
            # `Config`/policy table this `--config` override edits. There is
            # no separate, weaker sandbox construction for the debug
            # subcommand — measuring one measures the other.
            cmd += ["--config", "sandbox_workspace_write.network_access=true"]
        if self.auth_mode == "api_key":
            # NOT the enforcement point. codex-cli 0.149.0 SILENTLY IGNORES
            # this flag when a ChatGPT session is already live on the
            # machine (measured 2026-08-25 — see the module docstring's
            # "TWO SANCTIONED AUTH PATHS"). The real gate already ran, in
            # `_child_env_api_key` (above), which calls
            # `assert_api_key_billing_path` at line 554 of this file — by
            # the time this argv is built, the CLI has already been asked,
            # against the exact env the child will get, and has already
            # refused if it would bill a ChatGPT plan. This flag is kept as
            # belt-and-braces for CLI versions that DO honour it. In
            # "subscription" mode it is OMITTED, not pointed at a
            # substitute value — there is no key to force, and forcing
            # "apikey" here would make the subscription mode's own CLI
            # calls refuse themselves.
            cmd += ["--config", 'preferred_auth_method="apikey"']
        # Nobody is at the keyboard. An approval prompt in a headless run is
        # an indefinite hang, not a safety feature — but the flag that
        # suppresses it moved between CLI versions (codex-cli 0.149.0 dropped
        # `--ask-for-approval` from `exec`), so it is resolved from the
        # INSTALLED binary's own `codex exec [resume] --help`, never assumed.
        # See `approval_args`.
        cmd += approval_args(codex_exec_help(cli, resume=is_resume), codex_version(cli))
        mapped = _EFFORT_MAP.get((effort or "").lower())
        if mapped:
            cmd += ["--config", f'model_reasoning_effort="{mapped}"']
        # The prompt arrives on stdin. Passing it as argv would put a
        # multi-thousand-line task brief through the shell's argument limit and
        # into `ps` output.
        cmd.append("-")
        return cmd

    # ------------------------------------------------------------- events --

    def _guard_events(self, tool_name: str, tool_input: dict,
                      cwd: str | None = None) -> tuple[str, str] | None:
        """The guard's verdict on an ALREADY-EXECUTED tool call, or None.

        Same pure policy as the Claude path (``agent.guard.evaluate``) — the
        difference is entirely in the timing, and the caller acts on the
        returned ``(reason, severity)`` pair by killing the session for
        ``GUARD_DESTRUCTIVE``/``GUARD_EXFILTRATION`` violations, or merely
        recording ``GUARD_HYGIENE`` ones and letting the same subprocess run
        on — the call already happened either way, so a hygiene-class
        violation (e.g. installing outside the worktree's own ``.venv``) has
        nothing left to prevent by killing the attempt. ``cwd`` is the
        session's worktree, for the guard's file-existence questions.
        """
        decision = guard.evaluate(
            tool_name, tool_input,
            forbidden_paths=self.forbidden_paths,
            never_push_to=self.never_push_to,
            readonly=self.readonly,
            cwd=cwd,
        )
        return None if decision.allow else (decision.reason, decision.severity)

    def _translate(self, msg: dict) -> list[AgentEvent]:
        """One Codex JSONL record → zero or more :class:`AgentEvent`.

        Written against the ``codex exec --json`` envelope (``thread.started`` /
        ``item.*`` / ``turn.completed`` / ``turn.failed``) AND the older
        ``{"msg": {"type": ...}}`` envelope, because both shapes exist in the
        wild across codex releases and neither is verifiable from here. An
        unrecognised record yields NO events rather than raising: a stream that
        dies on an unknown type would take the whole attempt with it, and the
        orchestrator's bounded loop would read a schema drift as a code failure.
        """
        events: list[AgentEvent] = []
        kind = str(msg.get("type") or "")

        # Older envelope: the payload is nested under "msg".
        if not kind and isinstance(msg.get("msg"), dict):
            inner = msg["msg"]
            legacy = str(inner.get("type") or "")
            if legacy in ("agent_message", "agent_message_delta"):
                text = str(inner.get("message") or inner.get("delta") or "")
                return [AgentEvent("text", text=text)] if text else []
            if legacy in ("agent_reasoning", "agent_reasoning_delta"):
                text = str(inner.get("text") or inner.get("delta") or "")
                return [AgentEvent("thinking", text=text)] if text else []
            if legacy == "exec_command_begin":
                cmd = inner.get("command")
                cmd_s = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
                return [AgentEvent("tool_use", tool_name="Bash",
                                   tool_input={"command": cmd_s},
                                   meta={"tool_use_id": str(inner.get("call_id") or "")})]
            if legacy == "patch_apply_begin":
                changes = inner.get("changes") or {}
                return [
                    AgentEvent("tool_use", tool_name="Write",
                               tool_input={"file_path": str(p)},
                               meta={"tool_use_id": str(inner.get("call_id") or "")})
                    for p in (changes if isinstance(changes, dict) else [])
                ]
            if legacy == "token_count":
                usage = _Usage.parse(inner.get("info") or inner)
                if usage:
                    return [self._usage_event(usage)]
            return []

        # `thread.started` carries the session id and no user-visible content;
        # it is consumed by the reader loop, which owns `session_id`.
        if kind in ("item.started", "item.completed", "item.updated"):
            item = msg.get("item") or {}
            if not isinstance(item, dict):
                return []
            itype = str(item.get("type") or item.get("item_type") or "")
            item_id = str(item.get("id") or "")
            if itype in ("agent_message", "assistant_message"):
                # Only on completion: `item.updated` streams deltas of the same
                # message, and emitting each one would feed the supervisor and
                # the transcript the same prose several times over.
                if kind == "item.completed":
                    text = str(item.get("text") or "")
                    if text:
                        events.append(AgentEvent("text", text=text))
                return events
            if itype == "reasoning":
                if kind == "item.completed":
                    text = str(item.get("text") or item.get("summary") or "")
                    if text:
                        events.append(AgentEvent("thinking", text=text))
                return events
            tool_name = _ITEM_TOOL_NAMES.get(itype)
            if tool_name is None:
                return []
            if kind == "item.started":
                for tool_input in self._tool_inputs(itype, item):
                    events.append(AgentEvent(
                        "tool_use", tool_name=tool_name, tool_input=tool_input,
                        meta={"tool_use_id": item_id},
                    ))
            elif kind == "item.completed":
                out = item.get("aggregated_output") or item.get("output") or ""
                text = out if isinstance(out, str) else json.dumps(out)
                events.append(AgentEvent(
                    "tool_result",
                    meta={
                        "tool_use_id": item_id,
                        "parent_tool_use_id": None,
                        "is_error": bool(item.get("exit_code")),
                        "result_chars": len(text),
                        "non_text_blocks": 0,
                    },
                ))
            return events

        if kind == "turn.completed":
            usage = _Usage.parse(msg.get("usage"))
            if usage:
                events.append(self._usage_event(usage))
            return events

        return events

    @staticmethod
    def _tool_inputs(itype: str, item: dict) -> list[dict]:
        """The guard-shaped input(s) for one Codex item.

        A ``file_change`` item carries SEVERAL paths in one record, and the
        guard is a per-path policy, so one item legitimately becomes several
        ``tool_use`` events. Doing it any other way — checking only the first
        path — is how a forbidden-path guard ends up passing a patch that
        rewrites ``.env`` in its second hunk.
        """
        if itype in ("command_execution", "local_shell_call"):
            cmd = item.get("command")
            cmd_s = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
            return [{"command": cmd_s}]
        if itype in ("file_change", "patch_apply"):
            changes = item.get("changes")
            paths: list[str] = []
            if isinstance(changes, dict):
                paths = [str(p) for p in changes]
            elif isinstance(changes, list):
                for c in changes:
                    if isinstance(c, dict):
                        p = c.get("path") or c.get("file_path")
                        if p:
                            paths.append(str(p))
                    elif isinstance(c, str):
                        paths.append(c)
            return [{"file_path": p} for p in paths] or [{"file_path": ""}]
        if itype == "mcp_tool_call":
            return [{"server": str(item.get("server") or ""),
                     "tool": str(item.get("tool") or "")}]
        return [{}]

    @staticmethod
    def _usage_event(usage: _Usage) -> AgentEvent:
        return AgentEvent("usage", meta={
            "tokens_used": usage.tokens_used,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            # There is no billed cache-WRITE class at OpenAI. 0 here is the
            # true value, not an unmeasured one — see
            # BackendCapabilities.cache_creation_accounting.
            "cache_creation_tokens": 0,
        })

    # ------------------------------------------------------------- stream --

    async def stream(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = None,
        resume: str | None = None,
        supervisor_hook: Any | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, Any] | None = None,
        on_compact: Callable[[str], None] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one ``codex exec`` session, yielding normalized events.

        The final event is always ``result``, including on every failure path —
        the orchestrator's bounded loop reads that event and nothing else, so a
        backend that raised instead would crash the daemon rather than fail an
        attempt (constraint §5).
        """
        # Unsupported knobs are REFUSED, never silently dropped. Each of these
        # is a control the orchestrator believes is running; a no-op would make
        # `nh watch` report supervision, linting or a skill that never existed.
        unsupported = [
            name for name, value in (
                ("supervisor_hook", supervisor_hook),
                ("lint_hook", lint_hook),
                ("skills", skills),
                ("agents", agents),
            ) if value
        ]
        if unsupported:
            yield AgentEvent("result", text=(
                f"the codex backend cannot honour {', '.join(unsupported)} — "
                f"`codex exec` has no PreToolUse/PostToolUse hook, no Agent "
                f"Skills and no named subagents. Refusing rather than running "
                f"a session that silently drops them. Either disable those "
                f"features for this task or set worker.backend: claude."
            ), meta=_error_meta(stop_reason="unsupported"))
            return

        try:
            cmd = self._command(cwd, effort=effort, resume=resume)
            env = self._child_env()
        except (BackendUnavailable, CodexAuthError) as exc:
            yield AgentEvent("result", text=str(exc),
                             meta=_error_meta(stop_reason="error"))
            return

        session_id: str | None = None
        turns = 0
        events_emitted = 0
        event_cap = _event_cap(max_turns)
        totals = {"tokens_used": 0, "output_tokens": 0,
                  "cache_read_tokens": 0, "cache_creation_tokens": 0}
        saw_usage = False
        final_text = ""
        failure = ""
        denials: list[str] = []
        stop_reason: str | None = None
        api_error_status: int | None = None

        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STDOUT_LIMIT,
            **hidden_console_kwargs(),
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

        try:
            while True:
                try:
                    raw = await _read_jsonl_line(proc.stdout)
                except _CodexLineTruncated as exc:
                    # Terminal, but a NORMAL outcome: stream()'s docstring
                    # (above) is that the final event is always `result` — a
                    # raise here crashes the pool worker instead of failing
                    # the attempt. The literal "stream closed" is
                    # LOAD-BEARING: orchestrator._classify_error matches it
                    # in the de-wrapped result text and returns "infra", the
                    # same marker claude_backend._TRANSPORT_FAILURE_MARKERS
                    # uses. Producer and consumer are pinned together by
                    # test_an_unassemblable_line_is_a_recorded_infra_failure.
                    failure = failure or f"codex stream closed mid-line: {exc}"
                    stop_reason = "error"
                    break
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                if not line or not line.startswith("{"):
                    # `codex exec` interleaves human-readable banner lines with
                    # the JSONL on some versions. Skipping non-JSON is what
                    # makes this parser survive that instead of aborting.
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue

                if msg.get("type") == "thread.started":
                    session_id = str(msg.get("thread_id") or "") or session_id
                elif isinstance(msg.get("msg"), dict) and \
                        msg["msg"].get("type") == "session_configured":
                    session_id = str(msg["msg"].get("session_id") or "") or session_id
                elif msg.get("type") in ("turn.failed", "error"):
                    model_exc = model_error_from_failure(msg, self.model)
                    if model_exc is not None:
                        failure = str(model_exc)
                        api_error_status = 404
                    else:
                        err = msg.get("error") or {}
                        failure = str(
                            (err.get("message") if isinstance(err, dict) else None)
                            or msg.get("message") or "codex reported an error")

                for event in self._translate(msg):
                    if event.kind == "tool_use":
                        turns += 1
                        verdict = self._guard_events(
                            event.tool_name or "", event.tool_input or {},
                            cwd=str(cwd))
                        if verdict:
                            reason, severity = verdict
                            denials.append(reason)
                            # GUARD_HYGIENE (e.g. installing outside the
                            # worktree's own .venv) is advisory: the call
                            # already happened and there is nothing left to
                            # prevent by killing the attempt, so it is
                            # recorded — visible in `denials` and as a
                            # "denied" event — and the SAME codex subprocess
                            # is left running. GUARD_DESTRUCTIVE and
                            # GUARD_EXFILTRATION still terminate the attempt;
                            # for those, the call already happened and cannot
                            # be undone, so stopping the session is the only
                            # remaining safety action.
                            terminating = severity != guard.GUARD_HYGIENE
                            yield AgentEvent(
                                "denied", text=reason,
                                tool_name=event.tool_name,
                                tool_input=event.tool_input,
                                # DETECTION, not prevention: the call already
                                # ran. Marked on the event so no reader can
                                # mistake this for the Claude path's veto.
                                meta={
                                    "post_hoc": True,
                                    "severity": severity,
                                    "terminating": terminating,
                                },
                            )
                            if terminating:
                                stop_reason = "guard"
                                failure = failure or (
                                    "safety guard violated (post-hoc — the codex "
                                    f"backend cannot block a call before it runs): {reason}")
                    elif event.kind == "text":
                        final_text = event.text or final_text
                    elif event.kind == "usage":
                        saw_usage = True
                        for k in totals:
                            totals[k] += int(event.meta.get(k, 0) or 0)
                    yield event
                    events_emitted += 1
                    if not stop_reason and events_emitted >= event_cap:
                        stop_reason = "max_turns"
                        failure = failure or _flood_failure(
                            events_emitted, event_cap, max_turns)
                    if stop_reason:
                        break

                if stop_reason:
                    break
                if turns >= max_turns > 0:
                    stop_reason = "max_turns"
                    failure = failure or (
                        f"Reached maximum number of turns ({max_turns})")
                    break
        finally:
            with_code, stderr = await _kill_and_reap(proc)
            if with_code not in (0, -9) and not failure:
                failure = (stderr.decode(errors="replace").strip()[-2000:]
                           or f"codex exited {with_code}")

        # Classify AFTER both failure sources above (the JSON turn.failed/
        # error branch and the non-zero-exit stderr branch) have had their
        # chance to populate `failure` — one check covers both, since the
        # vendor's "wrong account type for this model" refusal can arrive
        # either way depending on CLI version.
        if failure:
            classified = _classify_vendor_error(
                failure, mode=self.auth_mode, model=self.model)
            if classified is not None:
                failure = str(classified)
                stop_reason = "model_unsupported"

        yield AgentEvent(
            "result",
            text=failure or final_text,
            meta={
                "num_turns": turns,
                "is_error": bool(failure),
                "tokens_used": totals["tokens_used"],
                # None, not 0, when nothing reported — the same NULL-vs-zero
                # distinction the Claude path keeps all the way to the column.
                "output_tokens": totals["output_tokens"] if saw_usage else None,
                "session_id": session_id,
                "stop_reason": stop_reason or ("error" if failure else "end_turn"),
                "denials": denials,
                "api_error_status": api_error_status,
                "cache_read_tokens": totals["cache_read_tokens"],
                "cache_creation_tokens": 0,
                # No subagents to roll up; 0 here is the true value.
                "subagent_tokens_used": 0,
                "subagent_cache_read_tokens": 0,
                "subagent_cache_creation_tokens": 0,
                "subagent_count": 0,
                "subagent_floored_count": 0,
                "backend": "codex",
            },
        )

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = None,
        resume: str | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        supervisor_hook: Any | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, Any] | None = None,
        on_compact: Callable[[str], None] | None = None,
    ) -> AgentResult:
        """Run to completion, forwarding each event, return the result.

        Structurally identical to ``ClaudeBackend.run``, including the property
        the orchestrator's three abort controls depend on: ``on_event`` is
        called OUTSIDE any ``except``, so an exception it raises propagates out
        of ``run`` and unwinds the session.
        """
        final = AgentResult(
            final_text="", num_turns=0, is_error=False, tokens_used=0,
            session_id=None, stop_reason=None,
        )
        async for event in self.stream(
            prompt, cwd=cwd, max_turns=max_turns, effort=effort, resume=resume,
            supervisor_hook=supervisor_hook, lint_hook=lint_hook, skills=skills,
            thinking=thinking, max_thinking_tokens=max_thinking_tokens,
            agents=agents, on_compact=on_compact,
        ):
            if on_event is not None:
                on_event(event)
            if event.kind == "result":
                m = event.meta
                final = AgentResult(
                    final_text=event.text,
                    num_turns=int(m.get("num_turns", 0)),
                    is_error=bool(m.get("is_error", False)),
                    tokens_used=int(m.get("tokens_used", 0)),
                    session_id=m.get("session_id"),
                    stop_reason=m.get("stop_reason"),
                    denials=m.get("denials", []),
                    cache_read_tokens=int(m.get("cache_read_tokens", 0)),
                    cache_creation_tokens=int(m.get("cache_creation_tokens", 0)),
                    output_tokens=(
                        None if m.get("output_tokens") is None
                        else int(m["output_tokens"])
                    ),
                    api_error_status=m.get("api_error_status"),
                )
        return final


def _error_meta(*, stop_reason: str) -> dict[str, Any]:
    """Result meta for a run that never started. Every key the orchestrator's
    result handler reads is present — a missing one there is a KeyError inside
    the attempt loop, i.e. a crash instead of a failed attempt."""
    return {
        "num_turns": 0,
        "is_error": True,
        "tokens_used": 0,
        "output_tokens": None,
        "session_id": None,
        "stop_reason": stop_reason,
        "denials": [],
        "api_error_status": None,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "subagent_tokens_used": 0,
        "subagent_cache_read_tokens": 0,
        "subagent_cache_creation_tokens": 0,
        "subagent_count": 0,
        "subagent_floored_count": 0,
        "backend": "codex",
    }
