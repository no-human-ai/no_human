"""The coding-backend SEAM: what the orchestrator needs, stated once.

Until 2026-08-01 there was exactly one coding backend and no seam at all — the
constraint read "single Claude backend via the Agent SDK (no Agent-A/api-vendor/
local abstraction)". The operator REMOVED that clause to add OpenAI Codex as a
second coding backend, so a vendor abstraction is now sanctioned. This module is
that abstraction, and nothing else: it holds the two data types the orchestrator
already consumed (:class:`AgentEvent`, :class:`AgentResult` — moved here from
``claude_backend`` and re-exported from it, so no import site changed), the
protocol every backend must satisfy, an HONEST capability record per backend,
and the factory that picks one.

WHY THE INTERFACE IS SHAPED LIKE THIS. It was read off the CALLER, not off
either vendor's SDK. Concretely, ``core/orchestrator.py`` depends on:

  * ``await backend.run(prompt, cwd=…, max_turns=…, effort=…, on_event=…)``
    returning an :class:`AgentResult`, and ``backend.stream(...)`` yielding
    :class:`AgentEvent`. ``run`` is the only method the coder path calls.
  * ``on_event`` being invoked SYNCHRONOUSLY between events, and an exception
    raised inside it propagating OUT of ``run``. ``_agent_sink`` raises
    ``CancelRequested`` (``nh task pause``), ``BudgetAbort`` (the mid-attempt
    spend watch), ``StuckAbort`` (doom-loop) and ``ConvergenceAbort`` (P2:
    turn-cap convergence — varied calls, no progress) from inside the
    callback; that raise is the ONLY way those four controls stop a running
    attempt. A backend that swallowed callback exceptions would silently
    disable all four.
  * event kinds ``thinking`` / ``text`` / ``tool_use`` / ``tool_result`` /
    ``usage`` / ``result`` with the meta keys spelled out in
    :class:`AgentEvent`. ``tool_use`` drives the doom-loop detector, the
    edited-file set (which decides what gets committed) and the scope guard;
    ``usage`` drives the mid-attempt budget watch; ``result`` is the ledger row.
  * MUTABLE ``forbidden_paths`` and ``never_push_to`` attributes — the
    orchestrator rewrites both per task (``_apply_task_guard``,
    ``_protect_base_branch``) on a backend instance the worker pool REUSES.
  * a ``model`` attribute, recorded on the attempt row and used to price it.

Everything else the Claude path offers (``skills``, ``agents``, ``thinking``,
``supervisor_hook``, ``lint_hook``, ``resume``) is OPTIONAL in the protocol and
declared per backend in :class:`BackendCapabilities`. A backend that cannot do
one of them must SAY so there rather than accept the argument and ignore it —
an ignored ``supervisor_hook`` is a supervisor that never fires, which is the
"a check nothing observes is not a check" failure this codebase keeps hitting.

WHAT THIS MODULE DOES NOT DO. It does not re-implement tools (constraint §6,
untouched by the amendment): every backend delegates Read/Edit/Bash/Grep/Glob to
the vendor's own agentic harness. It does not choose credentials — that stays in
``config.py``, where the "mode may live in config, the key never does" rule and
the exactly-one-billing-path scrub live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Protocol, runtime_checkable

log = logging.getLogger("no_human.agent.backend")

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .supervisor import SupervisorHook


@dataclass
class AgentEvent:
    """A normalized streaming event for the TUI / logs.

    ``kind`` is the vendor-independent vocabulary the orchestrator switches on:
    ``thinking``, ``text``, ``tool_use``, ``tool_result``, ``usage``,
    ``result``, ``denied``, and the ``subagent_*`` trio. A backend that cannot
    produce one of them simply never emits it; it must never invent a kind
    outside this list, because ``_agent_sink`` and the web renderers key on it.
    """

    kind: str            # thinking | text | tool_use | tool_result | result | denied
    text: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # A `tool_result`'s output text, IN PROCESS ONLY. The persisting sinks
    # (`Orchestrator._agent_sink` / `_reviewer_sink`) copy kind/text/
    # tool_name/tool_input/meta by name and never this field, so the no-text
    # rule for the DB (`claude_backend`'s tool_result emit site) is unchanged.
    # Its one reader is `diff_coverage.InspectionTracker`, which credits a
    # truncated file shown in a directory listing. None when not captured.
    output: str | None = field(default=None, repr=False)


@dataclass
class AgentResult:
    """The outcome of one agent run."""

    final_text: str
    num_turns: int
    is_error: bool
    tokens_used: int
    session_id: str | None
    stop_reason: str | None
    denials: list[str] = field(default_factory=list)
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # The OUTPUT share of `tokens_used` above, which is input+output. Output
    # bills ~5x input, and `_usage_quad` has always returned the number — this
    # dataclass was where it got thrown away, so no cost surface downstream
    # could price it. `tokens_used` is unchanged and still means the total;
    # input is `tokens_used - output_tokens`.
    #
    # `None`, not 0, and that distinction is load-bearing all the way to the
    # DB column: 0 asserts "this run emitted no output tokens", which is true
    # of almost no run, whereas None says the usage block never arrived (an
    # errored result, a session that produced no ResultMessage). It is
    # persisted as SQL NULL and priced at the old input rate — an under-count
    # that is at least visible as unknown rather than asserted as free.
    output_tokens: int | None = None
    # HTTP status of the failing API call (429/529/500...). The SDK sets it on
    # the result event precisely when is_error is true and the subtype is
    # "success" — i.e. THIS incident's shape. It was captured on the event but
    # never reached here, so `_classify_error`'s 429/529 branch could not fire:
    # `getattr(result, "api_error_status", None)` was permanently None. The
    # structured twin of the free-text reason this change surfaces.
    api_error_status: int | None = None
    # The subagent (Task tool) share of the three totals above, which INCLUDE
    # it. Broken out so a reader can see the ledger grew for a reason. These
    # ride the "result" AgentEvent into the orchestrator's event stream; no
    # surface reads them back out of the DB yet, so the audit trail is the
    # event log, not a column.
    subagent_tokens_used: int = 0
    subagent_cache_read_tokens: int = 0
    subagent_cache_creation_tokens: int = 0
    subagent_count: int = 0
    # How many of `subagent_count` contributed a FLOOR rather than a
    # measurement — subagents that streamed no assistant message at all, whose
    # only signal is the CLI's context-size gauge (7-17% of real spend). A
    # datum, not a comment: any surface that reports subagent spend can say
    # "N of M are floors" instead of presenting an undercount as a total.
    subagent_floored_count: int = 0
    # The SDK ResultMessage.structured_output when the run was given an
    # output_format (a json_schema); None otherwise. Lets a caller read the
    # model's answer as a parsed dict instead of regex-scraping fenced JSON.
    structured_output: Any = None


@dataclass(frozen=True)
class BackendCapabilities:
    """What a backend can actually do, declared rather than assumed.

    Every field here corresponds to something the orchestrator PASSES or RELIES
    ON. The point of the record is that a mismatch is a readable fact at the
    seam instead of a silently-ignored keyword argument three layers down. The
    Codex backend is missing several of these and says so; see
    ``docs/BACKENDS.md`` for what each absence costs.
    """

    #: Stable identifier, matching the ``worker.backend`` config value.
    name: str
    #: A PreToolUse decision can BLOCK a tool call before it executes
    #: (``agent.guard.evaluate`` wired as a real veto). False means the guard
    #: can only be evaluated AFTER the fact — detection, not prevention.
    blocks_tool_calls: bool
    #: PostToolUse callbacks fire per tool call, so ``supervisor_hook`` and
    #: ``lint_hook`` can observe and inject a correction mid-session.
    post_tool_hooks: bool
    #: ``resume=<session_id>`` continues a previous session's context.
    session_resume: bool
    #: Named subagents (``agents=``) can be defined for the session.
    subagents: bool
    #: Agent Skills (``skills=``) can be attached to the session.
    skills: bool
    #: An explicit extended-thinking token budget can be set.
    thinking_budget: bool
    #: Usage is reported INCREMENTALLY during the run, not only at the end.
    #: The mid-attempt budget watch (``BudgetAbort``) can only bite when this
    #: is True; with False the ceiling is still enforced, but only between
    #: attempts, so one attempt can overshoot it.
    incremental_usage: bool
    #: The vendor reports cache-write tokens as a separate billed class.
    #: False is not "we failed to measure it" — for OpenAI there is no such
    #: class to measure (prompt caching is automatic and cache writes are not
    #: billed separately), so ``cache_creation_tokens`` is legitimately 0.
    cache_creation_accounting: bool
    #: ``max_turns`` is enforced by the vendor harness itself. False means this
    #: backend counts turns from its own event stream and terminates the
    #: session when the count is exceeded — the same ceiling, enforced by us.
    native_max_turns: bool


@runtime_checkable
class CodingBackend(Protocol):
    """The contract ``core.orchestrator.Orchestrator`` is written against.

    Deliberately narrow. ``run`` is the only method the coder path calls;
    ``stream`` exists because a handful of intake/eval callers consume events
    directly. The three attributes are mutated by the orchestrator between
    tasks and are therefore part of the contract, not implementation detail.
    """

    #: Recorded on the attempt row and used to price it.
    model: str
    #: Rewritten per task by ``Orchestrator._apply_task_guard``.
    forbidden_paths: list[str]
    #: Rewritten per task by ``Orchestrator._protect_base_branch``.
    never_push_to: list[str]

    @property
    def capabilities(self) -> BackendCapabilities: ...

    def stream(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = ...,
        resume: str | None = ...,
        supervisor_hook: "SupervisorHook | None" = ...,
        lint_hook: Any | None = ...,
        skills: list[str] | None = ...,
        thinking: bool = ...,
        max_thinking_tokens: int | None = ...,
        agents: dict[str, Any] | None = ...,
        on_compact: Callable[[str], None] | None = ...,
    ) -> AsyncIterator[AgentEvent]: ...

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = ...,
        resume: str | None = ...,
        on_event: Callable[[AgentEvent], None] | None = ...,
        supervisor_hook: "SupervisorHook | None" = ...,
        lint_hook: Any | None = ...,
        skills: list[str] | None = ...,
        thinking: bool = ...,
        max_thinking_tokens: int | None = ...,
        agents: dict[str, Any] | None = ...,
        on_compact: Callable[[str], None] | None = ...,
    ) -> AgentResult: ...


class BackendUnavailable(RuntimeError):
    """A backend was selected that this install cannot run."""


#: Roles that are PINNED to Claude by DEFAULT, and why.
#:
#: This project's non-negotiable constraints fix the review gate as "an
#: independent fresh-context Agent SDK reviewer (claude-opus-5)", and fix all
#: four model tiers
#: (implementer / reviewer+planner / supervisor / utility) by ID. The operator's
#: 2026-08-01 amendment sanctioned a second CODING backend; it did not move the
#: review gate, the planner, the supervisor or the utility tier off Claude, and
#: a config key that silently did so would be a constraint change wearing the
#: costume of a feature. So the switch reaches exactly one role: the coder.
#:
#: Constraint amendment §6d (operator, commit 413d76f0d) changed
#: this from an absolute pin to a DEFAULT pin set: an explicit, per-role
#: Settings choice (``llm.role_backends.<role>``, see
#: :func:`explicit_role_backend`) now overrides it for that role. Today only
#: "reviewer" is wired through that seam (`core.role_backend_settings.
#: ROLE_BACKEND_ROLES`); every other role here still resolves to Claude
#: unconditionally, same as before.
#:
#: This is a denylist of roles rather than an allowlist of one because the
#: default for an unrecognised role must be the SAFE side (stay on Claude), and
#: because `role` is a caller-supplied string — see the `!= "coder"` test below,
#: which is what actually implements that default. The tuple is documentation.
CLAUDE_PINNED_ROLES = ("reviewer", "planner", "supervisor", "utility", "intake")


#: Every name `make_backend` accepts, in the order the docs list them. The CLI
#: (`nh task add --backend`) and the API (`CreateTaskRequest.backend`) validate
#: against THIS tuple, so a backend added below is offered everywhere at once
#: and a typo is refused at intake instead of at the first attempt.
SUPPORTED_BACKENDS: tuple[str, ...] = ("claude", "codex", "local")


def explicit_role_backend(config: dict[str, Any] | None, role: str) -> dict[str, str] | None:
    """The explicit ``llm.role_backends[role]`` entry, if one is present and
    well-shaped — ``None`` otherwise (absent, blank, or malformed).

    This is the ONLY place ``make_backend``/``resolve_backend_name`` consult
    for an override of :data:`CLAUDE_PINNED_ROLES`'s default. It reads
    exactly one thing: the config mapping passed in. It never consults
    environment variables or per-task config (``task.config``) — a per-role
    backend choice is a Settings-only, config-file-only concept (constraint
    §6d's single-write-path rule; see ``config.set_role_backend`` and
    ``config._reject_invalid_role_backends``, which together mean any entry
    reaching a REAL loaded config already went through the one writer). A
    malformed entry (should not happen for a config that went through
    ``load_config``, but this function is also handed raw dicts in tests)
    fails SAFE to ``None`` — i.e. the role's default — rather than raising:
    this is a read, not the validation gate.
    """
    role_backends = ((config or {}).get("llm") or {}).get("role_backends")
    if not isinstance(role_backends, dict):
        return None
    entry = role_backends.get(role)
    if not isinstance(entry, dict):
        return None
    backend = entry.get("backend")
    model = entry.get("model")
    if not isinstance(backend, str) or not backend.strip():
        return None
    if not isinstance(model, str) or not model.strip():
        return None
    return {"backend": backend.strip().lower(), "model": model.strip()}


def resolve_backend_name(config: dict[str, Any] | None, *, role: str = "coder") -> str:
    """Which backend name *role* should use, given a config mapping.

    ``role="coder"`` consults ``worker.backend``. Every other role resolves to
    ``"claude"`` UNLESS an explicit :func:`explicit_role_backend` entry exists
    for it (§6d) — the same lookup :func:`make_backend` uses, so this stays a
    read-only preview of what ``make_backend`` would build, never a second
    opinion. See :data:`CLAUDE_PINNED_ROLES`.
    """
    if role != "coder":
        entry = explicit_role_backend(config, role)
        return entry["backend"] if entry else "claude"
    return str(((config or {}).get("worker") or {}).get("backend") or "claude")


#: The coder's `CLAUDE_CODE_AUTO_COMPACT_WINDOW` override when
#: `bounds.coder_compact_window_tokens` is absent from config. Below the
#: ~170k-token context plateau measured on real coder attempts (the CLI's own
#: default window is the full model context, ~200k, which is why compaction
#: was effectively never observed for coder sessions) and above the largest
#: single prompt the coder receives.
DEFAULT_CODER_COMPACT_WINDOW_TOKENS = 140_000


def _resolve_coder_compact_window_tokens(bounds_cfg: dict[str, Any] | None) -> int:
    """The coder's compaction-window override, failing CLOSED on a bad value.

    An absent key resolves to :data:`DEFAULT_CODER_COMPACT_WINDOW_TOKENS`. A
    present but non-positive/non-numeric value is never forwarded to the CLI
    (which would either silently ignore a garbage env var or reject the
    subprocess) — it is logged and replaced with the same default.
    """
    raw = (bounds_cfg or {}).get("coder_compact_window_tokens")
    if raw is None:
        return DEFAULT_CODER_COMPACT_WINDOW_TOKENS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning(
            "bounds.coder_compact_window_tokens=%r is not an integer; "
            "using the default %d", raw, DEFAULT_CODER_COMPACT_WINDOW_TOKENS,
        )
        return DEFAULT_CODER_COMPACT_WINDOW_TOKENS
    if value <= 0:
        log.warning(
            "bounds.coder_compact_window_tokens=%d is not positive; "
            "using the default %d", value, DEFAULT_CODER_COMPACT_WINDOW_TOKENS,
        )
        return DEFAULT_CODER_COMPACT_WINDOW_TOKENS
    return value


#: The local coding backend's honest contract. It is still the SAME harness
#: (the Agent SDK's Claude Code CLI, just pointed at a different
#: ``ANTHROPIC_BASE_URL``) so it keeps every structural capability the Claude
#: reference implementation has — tool-call blocking, post-tool hooks, session
#: resume, subagents, skills, native max-turns, incremental usage. Two fields
#: differ, both because they depend on the MODEL behind the CLI rather than the
#: CLI itself: ``thinking_budget`` is Anthropic-specific extended-thinking
#: wiring a third-party model server has no reason to understand, and
#: ``cache_creation_accounting`` assumes Anthropic's prompt-caching billing
#: shape, which a local server generally does not bill or report at all.
LOCAL_CAPABILITIES = BackendCapabilities(
    name="local",
    blocks_tool_calls=True,
    post_tool_hooks=True,
    session_resume=True,
    subagents=True,
    skills=True,
    thinking_budget=False,
    incremental_usage=True,
    cache_creation_accounting=False,
    native_max_turns=True,
)

#: Fallback child-process ``ANTHROPIC_API_KEY`` for the local backend when the
#: operator's ``~/.no_human/.env`` has no ``LOCAL_LLM_API_KEY``. Most local
#: model servers (llama.cpp, vLLM, LM Studio, ollama's OpenAI-compat shim...)
#: do not check the key at all, but the SDK's CLI still requires SOME value in
#: this env var to start — a real Anthropic key must never land here by
#: accident, so this is a literal, obviously-fake string rather than empty.
LOCAL_BACKEND_FALLBACK_API_KEY = "no-key-local-backend"


def _local_child_env(llm_cfg: dict[str, Any]) -> dict[str, str]:
    """The EXACT per-subprocess env for the local backend — nothing more.

    Three entries, injected into ``ClaudeAgentOptions.env`` (never into
    ``os.environ``) by :class:`~.claude_backend.ClaudeBackend`:

    * ``ANTHROPIC_BASE_URL`` — the local server, already validated by
      :func:`no_human.config.assert_local_backend_mode` before this is called.
    * ``ANTHROPIC_API_KEY`` — ``LOCAL_LLM_API_KEY`` from ``~/.no_human/.env``
      if the operator set one (read via
      :func:`no_human.config.read_env_var_value`, which never exports it to
      ``os.environ``), else :data:`LOCAL_BACKEND_FALLBACK_API_KEY`.
    * ``CLAUDE_CODE_OAUTH_TOKEN`` — explicitly overridden to the empty string.
      A local run must never carry the operator's real subscription/enterprise
      token to a third-party model server; the CLI checks ``ANTHROPIC_API_KEY``
      first when both are present, but an explicit empty override — rather
      than relying on that precedence — is the difference between "the token
      happens not to be used" and "the token cannot reach this subprocess".
    """
    from ..config import LOCAL_LLM_API_KEY_VAR, read_env_var_value

    return {
        "ANTHROPIC_BASE_URL": str(llm_cfg.get("local_base_url") or ""),
        "ANTHROPIC_API_KEY": (
            read_env_var_value(LOCAL_LLM_API_KEY_VAR) or LOCAL_BACKEND_FALLBACK_API_KEY
        ),
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }


def local_run_without_subscription(
    config: dict[str, Any] | None, *, role: str = "coder",
    backend_name: str | None = None,
) -> bool:
    """True iff this run's child env carries no subscription credential AND
    the resolved backend is ``local``.

    Both conditions are required (intake decision, 2026-09-01): a bare
    ``backend == "local"`` check would be sufficient on its own today, but the
    AND makes the predicate a *measurement* of the actual child env
    (:func:`_local_child_env`) rather than a restatement of the config value —
    if ``_local_child_env`` ever stops blanking ``CLAUDE_CODE_OAUTH_TOKEN``,
    this predicate stops firing and the classifier reverts to today's
    (quota) behaviour instead of silently misclassifying a real subscription
    run as infra-only.

    ``backend_name``, when given, is used INSTEAD of resolving the backend
    from ``config`` — the coder session a task actually runs on can be a
    PER-TASK override (``task.config["backend"]``, honoured by
    ``core.runtime.task_backend_override`` /
    ``Orchestrator._task_backend`` ahead of the global ``worker.backend``), so
    a caller that has a task in scope must resolve through that and pass the
    result here; passing only ``config`` would see the global backend and
    miss a `--backend local` task filed against a `claude`-global install (or
    the inverse: a `--backend claude` task filed against a `local`-global
    install must NOT take this path). Callers with no task in scope (unit
    tests of this predicate) fall back to resolving from ``config`` alone.

    Used to route a zero-token SDK death away from ``paused_quota``: in local
    mode there is no subscription to reset, so a quota pause would wait
    forever for a reset that means nothing.
    """
    name = backend_name if backend_name is not None else resolve_backend_name(config, role=role)
    if name != "local":
        return False
    llm_cfg = (config or {}).get("llm") or {}
    try:
        token = _local_child_env(llm_cfg).get("CLAUDE_CODE_OAUTH_TOKEN") or ""
    except Exception:
        # Fail BACK to today's behaviour (quota path), not forward into the
        # new one: an exception here must never fail OPEN into treating an
        # unreadable env as "definitely no subscription".
        return False
    return not token.strip()


#: YAML spellings a human plausibly writes for a boolean. Hand-written; a
#: config file is text, and `bool("false")` is True.
_FALSEY_CONFIG_STRINGS = frozenset({"false", "no", "off", "0", ""})
_TRUTHY_CONFIG_STRINGS = frozenset({"true", "yes", "on", "1"})


def _codex_network_access(cfg: dict) -> bool:
    """Resolve `llm.codex_network_access`, refusing to GUESS.

    `bool(cfg.get(key, True))` had two measured defects, both of which made the
    operator's stated intent the opposite of what ran:

      `codex_network_access: "false"`  (quoted in YAML) -> bool("false") is
          True, so the opt-out was SILENTLY IGNORED and the capability stayed
          on. That is the dangerous direction for a sandbox control.
      `codex_network_access: null`     -> bool(None) is False, so the key was
          turned OFF, inverting the `null means use the default` convention
          every sibling key documents (`codex_model: null`,
          `codex_cli_path: null`).

    Absent or null therefore means the default (on). A real bool is honoured.
    A string is parsed against the spellings above. Anything else RAISES rather
    than resolving to a guess: silently choosing either direction for a
    capability control is how an operator ends up with a sandbox they did not
    ask for, and a loud config error at startup is cheaper than discovering it
    from a coder's network trace.
    """
    raw = cfg.get("codex_network_access", None)
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _FALSEY_CONFIG_STRINGS:
            return False
        if s in _TRUTHY_CONFIG_STRINGS:
            return True
        raise ValueError(
            f"llm.codex_network_access must be a boolean, got {raw!r}. "
            f"Write `true` or `false` (unquoted).")
    if isinstance(raw, int):
        return bool(raw)
    raise ValueError(
        f"llm.codex_network_access must be a boolean, got {type(raw).__name__}")


#: Which ``llm.*`` config key each non-Claude backend reads its model id
#: from — used by :func:`make_backend`'s explicit-role-backend seam to steer
#: a chosen model into the SAME lookup the codex/local branches already
#: perform (``cfg.get("codex_model")`` / ``cfg_llm.get("local_model")``),
#: rather than teaching either branch a second way to receive a model id.
#: ``"claude"`` needs no entry — that branch reads the ``model`` parameter
#: directly.
_MODEL_KEY_BY_BACKEND = {"codex": "codex_model", "local": "local_model"}


def _with_llm_override(config: dict[str, Any] | None, key: str, value: str) -> dict[str, Any]:
    """A shallow copy of *config* with ``llm[key] = value`` — never mutates
    the caller's config (or its nested ``llm`` dict). Both layers are copied
    because ``config or {}`` may hand back a fresh empty dict already, but a
    real config's ``llm`` sub-dict is shared state the caller (and every
    other in-flight backend construction) still holds a reference to."""
    base = dict(config or {})
    base["llm"] = {**(base.get("llm") or {}), key: value}
    return base


def make_backend(
    *,
    model: str,
    config: dict[str, Any] | None = None,
    backend: str | None = None,
    role: str = "coder",
    forbidden_paths: list[str] | None = None,
    never_push_to: list[str] | None = None,
    permission_mode: str | None = None,
    readonly: bool = False,
    supervisor_hook: "SupervisorHook | None" = None,
    lint_hook: Any | None = None,
    tool_result_caps: dict[str, int] | None = None,
    codex_config: dict[str, Any] | None = None,
) -> CodingBackend:
    """Build the coding backend for *role*.

    ``backend`` overrides the config lookup (tests, and the ``nh doctor``
    probe). Everything else is passed straight through to the chosen backend's
    constructor with the SAME meaning it has on ``ClaudeBackend`` — the seam
    exists to make the caller not care, so the caller's argument list does not
    change when the backend does.

    Defaulting is the acceptance criterion for this whole change: with no
    config, or with ``worker.backend`` absent or set to ``"claude"``, this
    returns exactly the ``ClaudeBackend`` the caller used to construct
    directly, with identical arguments. An operator who changes nothing sees
    no behavioural difference.
    """
    # `None` means "read `llm.permission_mode`" (default `bypassPermissions`,
    # so an operator who sets nothing sees no change); an explicit argument
    # still wins, which is the seam the tests construct through. Resolved once
    # here rather than in each backend branch so the coder and the reviewer —
    # the only two production call sites, both of which pass `config=` — get
    # the same answer. The reviewer needs it as much as the coder does: it
    # runs the suite through Bash.
    if permission_mode is None:
        # Aliased: the parameter and the resolver share a name. Imported here
        # like every other `..config` use in this module — the package imports
        # this one back.
        from ..config import permission_mode as config_permission_mode
        permission_mode = config_permission_mode(config or {})
    entry = None
    if role != "coder":
        # The pin is the FACTORY's, not the caller's: an explicit `backend=`
        # for a reviewer/planner/supervisor/utility role is ignored — the
        # ONLY override the factory honors for a non-coder role is an
        # explicit Settings choice (§6d, `llm.role_backends`), never a
        # caller-supplied kwarg, so no call site can put the review on the
        # model that wrote the code just by passing `backend=`.
        entry = explicit_role_backend(config, role)
        backend = entry["backend"] if entry else None
    if entry:
        chosen = entry["model"]
        if entry["backend"] in ("claude", "", "claude-agent-sdk"):
            # The claude branch below reads the `model` parameter directly,
            # not `config` — so the override is a local reassignment, not a
            # config copy.
            model = chosen
        else:
            override_key = _MODEL_KEY_BY_BACKEND.get(entry["backend"])
            if override_key is not None:
                config = _with_llm_override(config, override_key, chosen)
    name = (backend or resolve_backend_name(config, role=role)).strip().lower()

    if name in ("claude", "", "claude-agent-sdk"):
        from .claude_backend import ClaudeBackend

        # Scope guard (this ticket is the CODER's in-attempt cache burn only):
        # a readonly session (reviewer/planner/utility/supervisor/distill all
        # construct with readonly=True) never gets the window, regardless of
        # the role string passed in, and only role="coder" does.
        compact_window_tokens = None
        if role == "coder" and not readonly:
            compact_window_tokens = _resolve_coder_compact_window_tokens(
                (config or {}).get("bounds"))

        return ClaudeBackend(
            model=model,
            forbidden_paths=forbidden_paths,
            never_push_to=never_push_to,
            permission_mode=permission_mode,
            readonly=readonly,
            supervisor_hook=supervisor_hook,
            lint_hook=lint_hook,
            tool_result_caps=tool_result_caps,
            compact_window_tokens=compact_window_tokens,
        )

    if name == "codex":
        from ..config import codex_auth_mode
        from .codex_backend import CodexBackend

        cfg = codex_config if codex_config is not None else (
            (config or {}).get("llm") or {})
        mode = codex_auth_mode(config or {})
        return CodexBackend(
            # The Claude tier IDs in `config.llm` are fixed by the programme's
            # model-tier constraint and
            # are meaningless to Codex, so the Codex model is its OWN key. The
            # `model=` argument is the CLAUDE tier the caller asked for; it is
            # deliberately NOT forwarded. The default itself is PER-MODE (see
            # `default_codex_model`): a ChatGPT-subscription session refuses
            # the codex-branded ids api_key mode defaults to.
            model=str(cfg.get("codex_model") or default_codex_model(mode)),
            reasoning_effort=cfg.get("codex_reasoning_effort"),
            cli_path=cfg.get("codex_cli_path"),
            auth_mode=mode,
            forbidden_paths=forbidden_paths,
            never_push_to=never_push_to,
            readonly=readonly,
            network_access=_codex_network_access(cfg),
        )

    if name == "local":
        from ..config import assert_local_backend_mode
        from .claude_backend import ClaudeBackend

        cfg_llm = (config or {}).get("llm") or {}
        # Enforces the safety boundary (localhost/RFC1918-only, no DNS name,
        # no userinfo credentials, ambient ANTHROPIC_BASE_URL scrubbed) and
        # re-scrubs the metered-auth vars. (The Codex credential check is
        # not here: it runs at startup for a global `worker.backend: codex`
        # and in `core/runtime.assert_task_backend_usable` for a per-task
        # one, so `make_backend(backend="codex")` stays constructible in
        # tests and `nh doctor` without a key.) Raises AuthError on refusal.
        assert_local_backend_mode(cfg_llm.get("local_base_url"))

        model_id = str(cfg_llm.get("local_model") or "").strip()
        if not model_id:
            role_desc = (
                "the coder backend is 'local' (worker.backend, or this task's "
                "--backend)" if role == "coder" else
                f"the {role} backend is 'local' (llm.role_backends.{role})"
            )
            raise BackendUnavailable(
                f"{role_desc} but llm.local_model is not set. "
                "Set it in config.yaml:\n"
                "  llm:\n"
                "    local_model: <the model id the local server exposes>"
            )

        # Same scope guard as the claude branch: only the coder gets the
        # compact-window override, and only when not readonly.
        compact_window_tokens = None
        if role == "coder" and not readonly:
            compact_window_tokens = _resolve_coder_compact_window_tokens(
                (config or {}).get("bounds"))

        return ClaudeBackend(
            model=model_id,
            forbidden_paths=forbidden_paths,
            never_push_to=never_push_to,
            permission_mode=permission_mode,
            readonly=readonly,
            supervisor_hook=supervisor_hook,
            lint_hook=lint_hook,
            tool_result_caps=tool_result_caps,
            compact_window_tokens=compact_window_tokens,
            extra_env=_local_child_env(cfg_llm),
            cli_path=cfg_llm.get("local_cli_path"),
            capabilities=LOCAL_CAPABILITIES,
        )

    raise BackendUnavailable(
        f"unknown coding backend {name!r}. Supported: "
        f"{', '.join(repr(n) for n in SUPPORTED_BACKENDS)} ('claude' is the "
        f"default). Set it with `worker.backend` in ~/.no_human/config.yaml, "
        f"or per task with `nh task add --backend`."
    )


#: Default Codex model id for ``llm.codex_auth_mode: "api_key"``. Chosen
#: EXPLICITLY (BUILD item 4) rather than inherited from a Claude tier, and
#: overridable via ``llm.codex_model``.
#:
#: This ticket supersedes `dc529db3` (the retired-default fix): the OLD
#: default, `gpt-5-codex`, is dead on the api_key path — the operator's own
#: same-day measurement on their machine returned `Model not found` for it.
#: Of the codex-branded ids the operator measured as ENTITLED under api_key
#: that same day (`insufficient_quota` — a valid, billable id merely short of
#: credit, as opposed to `Model not found`/`model_not_found` for a dead one),
#: `gpt-5.3-codex` is picked here as the closest continuation of "coding-tuned
#: default" intent; `gpt-5.4`/`gpt-5.5`/`gpt-5.6-*` were entitled too and are
#: reachable via `llm.codex_model` for anyone who wants a different one.
#:
#: WHAT THIS SANDBOX COULD NOT RE-VERIFY: this build ran in a separate
#: environment (no live ChatGPT session; `codex login status` here reports
#: "Not logged in"), and could not call `codex login` to create one — that is
#: the hard, explicit constraint this task imposes on no_human itself: it
#: never calls, wraps, or shells out to that command. This sandbox's own
#: ambient `OPENAI_API_KEY` also returned a plain `401 Unauthorized` rather
#: than either of the vendor codes above, meaning it belongs to neither of
#: the operator's measured accounts and could not reproduce their entitlement
#: check either. The value below is therefore taken from the operator's own
#: same-machine, same-day measurement as instructed ("measured today on this
#: machine — do not re-derive it"), not independently re-measured here; if a
#: future run finds it retired too, the vendor's own refusal (or the typed
#: `CodexModelUnsupportedError` below) names the absence loudly rather than
#: silently degrading.
DEFAULT_CODEX_MODEL = "gpt-5.3-codex"

#: Default Codex model id for ``llm.codex_auth_mode: "subscription"``.
#: Per the operator's own same-day measurement (see above): under a live
#: ChatGPT session, `gpt-5.6-terra` and `gpt-5.5` both ran; the codex-branded
#: ids (`gpt-5.3-codex`, `gpt-5.1-codex*`) were refused with "not supported
#: when using Codex with a ChatGPT account" — the exact vendor phrase
#: `_MODEL_UNSUPPORTED_MARKER` in `codex_backend.py` matches on — so they are
#: not candidates for this default. `gpt-5.6-terra` is picked as the more
#: capable of the two working ids. Same caveat as above: this sandbox has no
#: live ChatGPT session and is forbidden from creating one via `codex login`,
#: so this value is carried from the operator's measurement rather than
#: re-confirmed here.
#:
#: ENTITLED is not the same claim as PRICED: whether an account's ChatGPT
#: plan can reach this id (measured above) is independent of whether this
#: codebase has a sourced per-token rate for it. `gpt-5.6-terra` happens to be
#: both — it is entitled per the measurement above AND carries a documented,
#: cited row in `core.pricing.MODEL_PRICES_USD_PER_MTOK` (see the citation
#: block at `core/pricing.py:189` and the row itself a few lines below it) —
#: so it is priced at its own published rate, never the unrecorded-model
#: fallback. An id that is entitled but NOT in that table (or an id this
#: constant is later changed to) still runs; it is just priced at the
#: backend-aware fallback (`core.pricing.fallback_output_extra_weight`) until
#: someone adds a sourced row for it.
DEFAULT_CODEX_MODEL_SUBSCRIPTION = "gpt-5.6-terra"


def default_codex_model(mode: str) -> str:
    """The built-in Codex model id for *mode*, before ``llm.codex_model``.

    Split per-mode because the two auth paths are refused different model
    ids by the vendor (see the constants above) — there is no single default
    that works on both.
    """
    if mode == "subscription":
        return DEFAULT_CODEX_MODEL_SUBSCRIPTION
    return DEFAULT_CODEX_MODEL


__all__ = [
    "AgentEvent",
    "AgentResult",
    "BackendCapabilities",
    "BackendUnavailable",
    "CodingBackend",
    "CLAUDE_PINNED_ROLES",
    "DEFAULT_CODEX_MODEL",
    "DEFAULT_CODEX_MODEL_SUBSCRIPTION",
    "default_codex_model",
    "DEFAULT_CODER_COMPACT_WINDOW_TOKENS",
    "SUPPORTED_BACKENDS",
    "LOCAL_CAPABILITIES",
    "LOCAL_BACKEND_FALLBACK_API_KEY",
    "local_run_without_subscription",
    "make_backend",
    "resolve_backend_name",
    "explicit_role_backend",
]
