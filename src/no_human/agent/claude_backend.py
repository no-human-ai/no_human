"""The incumbent coding backend: a thin wrapper over the Claude Agent SDK.

It is no longer the ONLY one — the operator struck the "single Claude backend"
clause on 2026-08-01 to add OpenAI Codex — but it is still the DEFAULT and its
behaviour is unchanged by that work. The vendor-independent contract now lives
in ``agent/backend.py``; ``AgentEvent`` and ``AgentResult`` moved there and are
re-exported below, so every existing ``from ...claude_backend import AgentEvent``
still resolves to the same class object.

Constraints honoured here:
  - Auth is set up by config.assert_subscription_mode() before we ever run: the
    default subscription mode exports CLAUDE_CODE_OAUTH_TOKEN and scrubs every
    metered var; operator-authorized BYO-API-key mode (llm.auth_mode: "api_key")
    leaves the operator's own ANTHROPIC_API_KEY in the env and scrubs the rest.
    Either way the SDK reads exactly one credential from the environment.
  - The SDK ships Read/Edit/Bash/Grep/Glob — we do NOT re-implement tools (§3.6).
  - A PreToolUse hook enforces the safety guard (forbidden paths, protected
    branches, rm -rf, no merge).
  - We stream events so `nh watch` can render tool calls live.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookMatcher,
    ResultMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from . import guard
from .child_env import scrub_foreign_secrets_into
from .backend import AgentEvent, AgentResult, BackendCapabilities
from .session_mark import mark_env
from .supervisor import SupervisorHook
from .tool_result_cap import make_tool_result_cap_hook
from .worker_context import describe_concurrency

# Substrings that mean THE TRANSPORT DIED, not "the task failed". A session
# that ends this way produced no verdict and no diff — there is nothing to
# learn from it and nothing to blame the task for.
#
# Scoped to what has actually been OBSERVED, not to everything that could
# plausibly be transport-shaped. "Stream closed" is the CLI's own wording
# (the bundled binary's Bun runtime raises `Stream closed by consumer`, code
# 138, and the CLI surfaces it as an errored result) and is the string the
# 2026-07-11 parallel-run incident was recorded under. "Connection error" rides
# along because `orchestrator._classify_error` has always treated the two
# identically as `infra`, and splitting them here would mean this retry and
# that classifier disagreed about the same failure.
#
# DELIBERATELY ABSENT: "timed out". A hang is already handled, twice, by
# `asyncio.wait_for` in `review/reviewer.py`, and retrying a hang inside the
# backend would silently double the wall-clock a task spends wedged — the exact
# regression `_agent_review`'s halving comment describes.
_TRANSPORT_FAILURE_MARKERS = ("stream closed", "connection error")

# One retry. Not two, not "until it works". A stream closure under a saturated
# shared subscription is the failure this exists for, and the honest response
# to it recurring is to escalate as infra, not to keep spending.
_TRANSPORT_RETRIES = 1

# A pause before the retry, because the observed cause is SATURATION and an
# immediate retry re-enters the same instant it just lost. Small on purpose:
# long enough that the condition can clear, short enough that it is never a
# hidden contributor to a review timeout.
_TRANSPORT_RETRY_DELAY_S = 5.0

# The token a downstream reader matches on to route this failure as infra
# rather than as a task defect (`orchestrator._escalate_reviewer_unavailable`).
# A constant, exported and imported, so the producer and the consumer cannot
# drift apart the way a literal repeated in two files always eventually does.
TRANSPORT_DIAGNOSIS_MARKER = "[transport]"


def dewrap(text: str) -> str:
    """Every run of whitespace collapsed to a single space.

    Exported, not private, because TWO modules must agree about it: this
    backend decides whether to RETRY a transport death, and
    ``orchestrator._classify_error`` decides what to CALL it. The comment on
    ``_TRANSPORT_FAILURE_MARKERS`` above says those two must not disagree
    about the same failure — and they did, the moment one of them started
    reading ``Stream\\nclosed`` as the message it plainly is and the other
    still read it literally. One function, imported by both, is the only
    version of that agreement that cannot drift.
    """
    return " ".join(text.split())


def _opening_span(text: str) -> tuple[str, int]:
    """``(haystack, opening_len)`` — the text's opening, de-wrapped.

    The first line is where the CLI's own wording lands, but "the first line"
    as ``splitlines()[0]`` reports it is decided by whatever wrapped the text:
    a terminal, a log formatter, or an exception renderer may put the break in
    the middle of ``Stream closed``, and may double the spaces around it. So
    the opening line is whitespace-normalised, and the line that FOLLOWS it is
    appended — with a single space, exactly as if the break were never there.

    ``opening_len`` is what stops that from becoming a wider search: it is the
    length of the opening line alone, and a caller must require the marker to
    start before it. A marker that starts on the second line is therefore still
    a miss; only one that starts on the first and spills over is a hit.

    Leading blank lines are skipped so a text that begins with a newline still
    has an opening (``.strip()`` upstream covers the common case, this covers
    a caller that does not strip).
    """
    lines = text.splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines):
        return "", 0
    opening = dewrap(lines[start])
    following = dewrap(lines[start + 1]) if start + 1 < len(lines) else ""
    return f"{opening} {following}".strip(), len(opening)


def is_transport_failure(result: "AgentResult") -> bool:
    """Did this run die in the transport rather than fail as a task?

    ``is_error`` alone does not license a substring search over the whole text.
    The SDK's *errored-with-subtype-success* shape — ``is_error=True`` on a
    ``ResultMessage`` whose ``subtype`` is still ``"success"`` — carries **the
    model's own prose** in ``result``, and that prose is what lands in
    ``final_text``. In THIS codebase, which is full of stream- and
    connection-handling code, a run that errored while summarising "added
    connection error handling" would have matched, been retried at full cost,
    and then been routed to the human as an infrastructure incident. The exact
    same over-match in ``_quota_signal`` once parked healthy tasks as
    PAUSED_QUOTA; the lesson is already paid for.

    So a match has to be CORROBORATED, by one of two independent signals:

    * a structured error signal — ``api_error_status`` (the SDK sets it on the
      failing API call) or ``stop_reason == "error"`` (the terminal-exception
      path in ``_run_once``). Either means the failure is the SESSION's, not
      something the model wrote, so the marker may match anywhere in the text
      (the traceback the exception path appends is several lines down); or
    * the marker **opens** the text. ``_run_once`` leads with what the
      CLI itself said (``last_result_text`` is prepended before the traceback,
      and is only ever captured from an errored result), and the observed
      incident's text is literally ``Stream closed unexpectedly``. Model prose
      that merely mentions the phrase does not open with it.

    Neither signal alone would do. Requiring the structured one would miss the
    observed shape outright — the recorded incident arrives as ``is_error=True,
    subtype="success", stop_reason=None, api_error_status=None`` (see the stub
    in ``tests/test_stream_closure_retry.py``, which reproduces it over a real
    subprocess). Requiring only the opening would miss the exception path
    when the CLI said nothing and the traceback is all there is.

    "Opens" is read off ``_opening_span`` rather than off ``splitlines()[0]``,
    because a physical first line is an artefact of whatever wrapped the text:
    ``Stream\\nclosed unexpectedly`` and ``Stream  closed`` are the same message
    as ``Stream closed unexpectedly`` and used to dodge the match. What did NOT
    change is how much text is eligible: the marker must still START in the
    opening line. A phrase quoted further down is still not a transport death.
    """
    if not result.is_error:
        return False
    text = (result.final_text or "").strip().lower()
    if not text:
        return False
    structured = bool(getattr(result, "api_error_status", None)) or (
        (result.stop_reason or "") == "error")
    if structured:
        return any(m in text for m in _TRANSPORT_FAILURE_MARKERS)
    opening, first_len = _opening_span(text)
    return any(0 <= opening.find(m) < first_len
               for m in _TRANSPORT_FAILURE_MARKERS)


#: What this backend can do, for the seam. Every field is True except the two
#: that describe a thing Claude genuinely does not have, and there are none —
#: the Claude path is the reference implementation of the contract, which is
#: exactly why the contract was read off it.
CLAUDE_CAPABILITIES = BackendCapabilities(
    name="claude",
    blocks_tool_calls=True,
    post_tool_hooks=True,
    session_resume=True,
    subagents=True,
    skills=True,
    thinking_budget=True,
    incremental_usage=True,
    cache_creation_accounting=True,
    native_max_turns=True,
)

# Phase 7c: explicit cap for tool-result display. Silent truncation makes the
# model treat a partial as complete; the marker + retrieval hint prevent that.
_TOOL_RESULT_CAP = 2000

# §7 0z / PR-024 lever 1. PER-TOOL caps on what the MODEL sees, each at roughly its own
# tool's p90 from 4,775 real tool results (an offline study,
# LEVER1_TOOL_RESULT_DISTRIBUTION.md). Bash and Read are 99.6%
# of all tool-result text and their medians differ ~6x, so one global number is the wrong
# instrument. Tools absent here are never touched.
#
# 🖐️ These are ENABLED by default, deliberately. `_TOOL_RESULT_CAP` above is the cautionary
# tale: a cap that shipped disabled-by-unreachability and never executed once across
# 19,440 tool_use events. A cost lever that is off is not a lever.
_TOOL_RESULT_CAPS: dict[str, int] = {"Bash": 4000, "Read": 16000}


def _result_size(content: Any) -> dict[str, Any]:
    """Size of a tool result as the MODEL sees it, not as Python reprs it.

    `ToolResultBlock.content` is `str | list[dict] | None`. The first version used
    `str(content)`, so `[{'type': 'text', 'text': 'hello world'}]` recorded 41 chars
    for 11 of payload (~30 chars of fixed dict-repr overhead, and `\n` counted as two),
    and `content=None` recorded 4 ("None") rather than 0 — planting phantom mass at the
    low end of the very distribution this exists to produce. The threshold is read off
    the HIGH end where that overhead is proportionally small, so the headline survives,
    but any median or percentile taken from repr lengths is wrong.
    """
    non_text = 0
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                # `or ""` guards a malformed `{"type":"text","text":null}`: a raise
                # here does NOT just lose telemetry — the nearest handler terminates
                # the stream and fails the whole attempt as an SDK error. This file
                # already states the principle at the guard-hook: telemetry must
                # never break the session.
                if b.get("type") == "text" or "text" in b:
                    parts.append(str(b.get("text") or ""))
                else:
                    # An image block carries a large base64 payload and ZERO text.
                    # Counting it as 0 chars is the SAME defect class as the repr
                    # inflation this helper replaced, in the opposite direction and
                    # an order of magnitude larger (11->41 vs ~1.2M->0). Flagged so
                    # it can be EXCLUDED, the way is_error and parent_tool_use_id are.
                    non_text += 1
            else:
                parts.append(str(b))
        text = "".join(parts)
    else:
        text = str(content)
    return {
        "result_chars": len(text),
        "over_cap": len(text) > _TOOL_RESULT_CAP,
        # A threshold read off the GLOBAL distribution must exclude these; a
        # threshold read off the Bash slice is unaffected, since Bash is text-only.
        "non_text_blocks": non_text,
    }


#: What a failed tool result OPENS with, and how far in we look for it. The
#: window bounds the work (a multi-MB single-line result is not copied to be
#: rejected) and bounds the digits, so `int()` cannot meet a 4,300-digit string.
_EXIT_CODE_PREFIX = "Exit code "
_EXIT_CODE_WINDOW = 64


def _exit_status(content: Any, *, is_error: bool) -> dict[str, int]:
    """``{"exit_code": N}`` when a FAILED result states a status, else ``{}``.

    103 `export_guard` invocations across 8 tasks in one night, and the DB
    cannot say whether any of that was a refusal LOOP: the size above cannot
    tell a refusal from a pass. An int can, and an int is not output — the
    no-text rule this file states at the emit site is unchanged.

    🔴 TWO SIGNALS, AND NEITHER ALONE WILL DO — the first version of this
    docstring claimed the SHAPE separated the populations, and that was wrong.
    Measured over 3,733 real ToolResultBlocks: ``block.content`` is a plain
    STRING for successes too (3,282 of them, against 127 errors and 324 block
    lists), so the type says nothing about whether a command failed. The
    ``{stdout, stderr, interrupted, isImage, noOutputExpected}`` dict is real
    but it is the CLI's own ``toolUseResult`` record — the SDK does not deliver
    it as block content. What the SDK gives is:

    * ``is_error`` — which population this result is in; and
    * the FIRST LINE of a failed result, which reads exactly ``Exit code N``
      (84 of 84 such lines in that corpus, every one ``is_error=True``, and no
      success opening with it).

    On the prefix alone, a command that SUCCEEDS while printing a captured log
    whose first line is ``Exit code 7`` — a replay, a fixture, this file's own
    test data — is recorded as having exited 7. On ``is_error`` alone there is
    no number at all, and the 43 non-command failures (a blocked tool, a schema
    rejection) state none. So both, and the line is read as a PREFIX: a status
    quoted mid-line, or on the second line, is not this command's status.

    Anything unstated stays unstated — no key, because a fabricated status is a
    measurement nobody made.
    """
    if not is_error or not isinstance(content, str):
        return {}
    window = content[:_EXIT_CODE_WINDOW]
    first, newline, _ = window.partition("\n")
    if not newline and len(content) > _EXIT_CODE_WINDOW:
        # The first line runs past the window, so digits visible here may be a
        # PREFIX OF the number rather than the number. Never guess at it.
        return {}
    first = first.strip()
    if not first.startswith(_EXIT_CODE_PREFIX):
        return {}
    digits = first[len(_EXIT_CODE_PREFIX):].strip()
    if not digits.isdigit():
        return {}
    try:
        return {"exit_code": int(digits)}
    except ValueError:
        # `isdigit()` is TRUE for "²" and "①" and `int()` refuses them, so the
        # guard above is not enough on its own (nor is `isdecimal()`: it does
        # not cover a digit run long enough to trip `int_max_str_digits`).
        # A raise here does not merely lose telemetry — the handler in
        # `_run_once` turns it into a FAILED ATTEMPT. Same invariant as
        # `_result_size`: telemetry must never break the session.
        return {}


def _fold_spend(prior: AgentResult, *, into: AgentResult) -> None:
    """Add a dead session's BILL — and only its bill — to the one that replaced it.

    A retried run is two sessions and two bills, but it is one `AgentResult`,
    and that single object is what reaches `attempts.tokens_used`. Without this
    the retry would look free while the real spend doubled.

    THE LINE, drawn deliberately: the returned result *describes the surviving
    session* — its text, turns, session_id and stop_reason all come from the
    retry. Only the money is run-wide, because money is the one quantity that
    accumulates across a discarded attempt. Structural counts
    (`num_turns`, `subagent_count`, `subagent_floored_count`) are NOT folded:
    they label the session whose verdict is being returned, and a combined
    cardinality would describe a session that never existed. The discarded
    session's structure is not lost — it rides the `transport_retry` event.

    `output_tokens` keeps its None-vs-0 distinction all the way to the SQL
    column: None means "no usage block was ever seen", and adding 0 to it would
    assert a measurement nobody made.
    """
    into.tokens_used += prior.tokens_used
    into.cache_read_tokens += prior.cache_read_tokens
    into.cache_creation_tokens += prior.cache_creation_tokens
    into.subagent_tokens_used += prior.subagent_tokens_used
    into.subagent_cache_read_tokens += prior.subagent_cache_read_tokens
    into.subagent_cache_creation_tokens += prior.subagent_cache_creation_tokens
    if prior.output_tokens is not None:
        into.output_tokens = (into.output_tokens or 0) + prior.output_tokens


def _usage_quad(usage: dict[str, Any] | None) -> tuple[int, int, int, int]:
    """(input, output, cache_read, cache_creation) from an SDK usage block."""
    u = usage or {}
    return (
        int(u.get("input_tokens", 0)),
        int(u.get("output_tokens", 0)),
        int(u.get("cache_read_input_tokens", 0)),
        int(u.get("cache_creation_input_tokens", 0)),
    )


def _rollup_subagents(
    streamed: dict[str, dict[str, tuple[int, int, int, int]]],
    reported: dict[str, int],
) -> tuple[int, int, int, int, int]:
    """Total what the Task tool's subagents spent: (in+out, cache_read,
    cache_creation, subagent_count, floored_count).

    ``ResultMessage.usage`` covers only the parent's own API requests, so
    without this every subagent was free. Both inputs are keyed by the Task
    tool's ``tool_use_id``:

    * ``streamed`` — the subagent's assistant messages, deduplicated by
      ``message_id``. This is the billing record: its input, cache_read and
      cache_creation figures are final, verified byte-exact against
      ``ResultMessage.usage`` on the parent's own messages. Only
      ``output_tokens`` is unreliable — the stream carries an early snapshot
      that is never revised upward (parent: 9 streamed vs 1,281 reported).
    * ``reported`` — ``TaskNotificationMessage.usage.total_tokens``. This is
      **NOT a bill**. Decoded from the CLI, it is

          (LAST request's input + cache_creation + cache_read)
          + SUM(output over EVERY streamed occurrence, duplicates included)

      i.e. a context-size gauge: the cache buckets are a final-message
      SNAPSHOT, not a sum, and the output term double-counts the stream's
      repeats. Confirmed live on a 6-response subagent whose stream carried 11
      occurrences: last request 6+93+12,953 = 13,052 plus SUM(output)=35 gives
      exactly the reported 13,087, while the sum-of-all-requests is 76,704 —
      the scalar is 17% of the true spend. Across 2,962 real subagent
      transcripts (median 12 API responses) it runs 7-12% of what was billed.

    So the totals come from the stream, never from the scalar. The scalar is
    used in exactly ONE place: a subagent that streamed NOTHING, where it is
    the only signal there is — banked as a FLOOR and COUNTED as one, so the
    caller can label it (`subagent_floored_count`).

    Two earlier versions of this function got the scalar wrong. The first
    claimed it equalled input+output+cache_read+cache_creation and rebuilt
    in/out from it on every subagent; the guard against a negative silently
    swallowed the error on 98.3% of real subagents. The second narrowed that to
    single-response subagents and claimed the scalar then "recovers the true
    output_tokens exactly". It does not: with one request the output term is
    still SUM over the stream's duplicates of an EARLY snapshot. On
    ``testdata/subagent_usage_stream.json`` the scalar carries output=8 (4
    streamed twice) where the subagent's own transcript records output=45 —
    11,067 against a true 11,104. That branch bought 4 tokens on 1.7% of
    subagents at the price of a false claim in source, so it is gone.

    KNOWN RESIDUAL, not a defect to hunt: streamed ``output_tokens`` is an
    early snapshot, so subagent output is under-recorded by roughly 0.8% of
    that subagent's bill (measured on the 6-response fixture: streamed in+out
    57 vs a true 690, against a true bill of 77,337 — 633 tokens, 0.82%; on the
    single-response fixture 41 tokens, 0.37%). It is NOT recoverable from the
    SDK stream at all: the parent transcript's Task ``toolUseResult`` carries
    only {agentId, description, outputFile, prompt, resolvedModel, status, …}
    and no usage block, and the scalar cannot supply it either (see above).
    The final figures live only in the subagent's own transcript file, which
    the SDK never surfaces to the parent. An SDK limitation; leave it alone.
    """
    io = cache_read = cache_creation = floored = 0
    for tool_use_id in set(streamed) | set(reported):
        msgs = streamed.get(tool_use_id, {})
        total = reported.get(tool_use_id)
        if not msgs:
            # Nothing streamed: the gauge is all we have. It UNDERSTATES real
            # spend badly, so it is a floor, not a measurement. Counted as
            # non-cache tokens — the dearest bucket — because for a gate whose
            # job is to stop runaway spend, erring toward "flag it" on a number
            # already known to be far too low is the safe direction. Tallied in
            # `floored` so the undercount rides out as a datum, not a comment.
            io += int(total or 0)
            floored += 1
            continue
        cache_read += sum(q[2] for q in msgs.values())
        cache_creation += sum(q[3] for q in msgs.values())
        io += sum(q[0] + q[1] for q in msgs.values())
    return (io, cache_read, cache_creation,
            len(set(streamed) | set(reported)), floored)


def _make_guard_hook(
    forbidden_paths: list[str], never_push_to: list[str], *, readonly: bool = False,
    cwd: str | None = None,
    session_root: str | None = None,
) -> Callable[..., Awaitable[dict]]:
    """Build a PreToolUse hook callback that applies the pure guard policy.

    ``cwd`` is the session's worktree (the SDK subprocess cwd) — the guard
    resolves file-existence questions against it, never against the
    orchestrator process's own cwd.

    ``session_root`` is that same worktree's ROOT, threaded into
    ``guard.evaluate`` so the install guard's containment boundary is the
    worktree the orchestrator created, not wherever the coder has since
    `cd`'d into.
    """

    async def hook(input_data: dict, tool_use_id: str | None, context: HookContext):
        decision = guard.evaluate(
            input_data.get("tool_name", ""),
            input_data.get("tool_input", {}) or {},
            forbidden_paths=forbidden_paths,
            never_push_to=never_push_to,
            readonly=readonly,
            cwd=cwd,
            session_root=session_root,
        )
        if decision.allow:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
            }
        }

    return hook


def _make_compact_hook(on_compact: Callable[[str], None]) -> Callable[..., Awaitable[dict]]:
    """Build a PreCompact hook: pure telemetry, never blocks. Compaction had
    never been OBSERVED for coder sessions (they end ~160k tokens, under the
    CLI's auto-compact threshold) — this makes every firing visible (C1a)."""

    async def hook(input_data: dict, tool_use_id: str | None, context: HookContext):
        try:
            on_compact(str((input_data or {}).get("trigger") or "auto"))
        except Exception:  # noqa: BLE001 — telemetry must never break the session
            pass
        return {}

    return hook


# One stdout JSON line from the CLI. 32 MiB: generous against any single tool
# result or resume replay, still a bound (a runaway line cannot eat memory).
SDK_MAX_BUFFER_BYTES = 32 * 1024 * 1024

# Tools to pre-approve when the session is NOT running `bypassPermissions`.
# That mode approves everything by itself, so this list is dead weight there
# and is not sent; `acceptEdits` is the mode that needs it.
#
# MEASURED against CLI 2.1.267 on 2026-09-10, not reasoned: under `acceptEdits`
# the CLI auto-approves the edit tools and never prompts for the read-only
# ones, so `Bash` is the only class that stops an unattended run — `git init`
# comes back "Command requires approval to run `git init`" with no allowlist
# and succeeds with one.
#
# Bash ALONE is listed. What that means is established; what it means for any
# OTHER tool is not measured here and is deliberately not claimed. Under
# `bypassPermissions` the CLI approves everything, so this mode is narrower by
# construction for any tool that would otherwise prompt. Widening the list is
# not the way to close that: `docs/security.md` §7 accounts for the coder's
# Bash egress and for nothing else, and an allowlist is the wrong place to
# widen a security disclosure quietly. A tool left out is not silently
# disabled — it surfaces as a visible blocker naming the approval it wanted.
PRE_APPROVED_TOOLS = ("Bash",)


class ClaudeBackend:
    """Drives one Agent SDK session per call to :meth:`run`."""

    def __init__(
        self,
        *,
        model: str,
        forbidden_paths: list[str] | None = None,
        never_push_to: list[str] | None = None,
        permission_mode: str = "bypassPermissions",
        readonly: bool = False,
        supervisor_hook: SupervisorHook | None = None,
        lint_hook: Any | None = None,
        tool_result_caps: dict[str, int] | None = None,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        compact_window_tokens: int | None = None,
        extra_env: dict[str, str] | None = None,
        cli_path: str | None = None,
        capabilities: BackendCapabilities | None = None,
    ):
        self.model = model
        # The auto-compaction window override (coder cache-burn ticket). `None`
        # (the default for every existing construction site — reviewer,
        # planner, utility, supervisor, distiller) means "leave the CLI's own
        # default alone"; only `make_backend`'s coder path ever sets this.
        # `_options()` turns it into `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, the env
        # var the bundled CLI reads — auto-compaction is on by default but its
        # window defaults to the full model context (~200k), while the coder's
        # measured context plateaus around ~170k, so it rarely fires without
        # this override.
        self.compact_window_tokens = compact_window_tokens
        self.forbidden_paths = forbidden_paths or [".env", "secrets/", "*.key", "*.pem"]
        self.never_push_to = never_push_to or ["main", "master", "release/*"]
        # bypassPermissions: unattended autonomy. The PreToolUse guard is the
        # real safety boundary and fires even in this mode (Part 10).
        self.permission_mode = permission_mode
        self.readonly = readonly
        self.supervisor_hook = supervisor_hook
        self.lint_hook = lint_hook
        # Both None by default: the coder/reviewer/planner path is byte-for-
        # byte unchanged. The advisory seam (`agent/advisory.py`) is the only
        # caller that passes these — `tools=[]` drops the full built-in tool
        # schema from a single-turn call that never uses a tool (38.5M
        # tok/week measured across the utility+supervisor tiers), and a
        # `system_prompt` string replaces the coding harness's prompt with
        # the role's own.
        self.tools = tools
        self.system_prompt = system_prompt
        # None means "use the measured defaults"; {} means explicitly OFF.
        #
        # 🔴 READONLY BACKENDS ARE EXEMPT BY DEFAULT, AND THAT IS THE POINT OF THE
        # CAP. This lever exists to shrink what the CODER accumulates in a
        # conversation it re-reads every turn — measured on one task at 188 turns,
        # ~64k cache-read per turn, 99.98% of a 12M budget. A readonly backend does
        # not accumulate toward a diff; its whole job IS reading. Capping it does not
        # save the thing the cap was built to save, and it damages the work.
        #
        # The reviewer is the sharp case: it is told it MAY use Read/Grep/Glob on any
        # file and is REQUIRED to cite file:line evidence. At Read's own measured p90
        # (~17k) a 16,000-char cap truncates large files mid-read, so the gate that
        # decides every merge would silently see the head of a file and cite line
        # numbers from the part it got. Nothing would mark that review as degraded.
        # An audit caught this: the cap applied to reviewer, planner, distiller,
        # supervisor and aggregator with no exemption and no test.
        #
        # Gated on `readonly` rather than on a LIST of construction sites, because
        # enumeration is exactly how this class of gap keeps recurring here — three
        # separate surfaces were missed by enumerated lists in one day. Any future
        # readonly backend is exempt automatically. An explicit `tool_result_caps=`
        # from the caller still wins in both directions.
        self.tool_result_caps = (
            ({} if readonly else _TOOL_RESULT_CAPS)
            if tool_result_caps is None else tool_result_caps
        )
        # Local backend seam (part 2): extra per-subprocess env entries merged
        # into `ClaudeAgentOptions.env` on top of the compact-window override
        # below — never into `os.environ`, and never persisted beyond a single
        # `_options()` call, so nothing here can leak across a reused backend
        # instance or another role's session. `None`/`{}` (every existing
        # construction site) means "inject nothing"; `make_backend`'s local
        # branch is the only caller that passes a non-empty dict.
        self.extra_env = dict(extra_env or {})
        # Absolute path to a `claude` CLI binary. `None` (every existing
        # construction site) means "let the SDK resolve its own bundled CLI",
        # exactly as before this parameter existed.
        self.cli_path = cli_path
        # Overrides the reference `CLAUDE_CAPABILITIES` contract for a backend
        # that is still the same harness but talks to a different model
        # server (the local backend). `None` (every existing construction
        # site) preserves today's behavior exactly.
        self._capabilities = capabilities or CLAUDE_CAPABILITIES

    @property
    def capabilities(self) -> BackendCapabilities:
        """Satisfies the seam's ``CodingBackend`` protocol. See
        :data:`CLAUDE_CAPABILITIES`."""
        return self._capabilities

    def _options(
        self, cwd: Path, max_turns: int, *, effort: str | None = None,
        resume: str | None = None,
        supervisor_hook: SupervisorHook | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, AgentDefinition] | None = None,
        on_compact: Callable[[str], None] | None = None,
        output_format: dict[str, Any] | None = None,
    ) -> ClaudeAgentOptions:
        hooks: dict = {
            "PreToolUse": [
                HookMatcher(
                    matcher=None,
                    hooks=[
                        _make_guard_hook(
                            self.forbidden_paths,
                            self.never_push_to,
                            readonly=self.readonly,
                            cwd=str(cwd),
                            session_root=str(cwd),
                        )
                    ],
                )
            ]
        }
        # PostToolUse may carry two callbacks: the deterministic per-edit lint
        # hook (cheap, runs first) and the supervisor's every-N LLM check.
        sv = supervisor_hook or self.supervisor_hook
        lh = lint_hook or self.lint_hook
        post_hooks = []
        # 0z lever 1 FIRST: it rewrites the tool output the model will see, and the
        # other two only observe. Ordering matters if a later hook ever reads the
        # output — this one should be the thing that decided it.
        if self.tool_result_caps:
            post_hooks.append(make_tool_result_cap_hook(self.tool_result_caps))
        if lh is not None:
            post_hooks.append(lh.hook)
        if sv is not None:
            post_hooks.append(sv.hook)
        if post_hooks:
            hooks["PostToolUse"] = [HookMatcher(matcher=None, hooks=post_hooks)]
        if on_compact is not None:
            hooks["PreCompact"] = [
                HookMatcher(matcher=None, hooks=[_make_compact_hook(on_compact)])
            ]
        kwargs: dict[str, Any] = {}
        if thinking:
            # The SDK's `thinking` is a dict (ThinkingConfig), not a bool. Passing
            # True made subprocess_cli do True["type"] → "'bool' object is not
            # subscriptable", crashing EVERY thinking-enabled (complex) task
            # (task 6cfdb936, all attempts). Enabled with a budget, else adaptive.
            kwargs["thinking"] = (
                {"type": "enabled", "budget_tokens": max_thinking_tokens}
                if max_thinking_tokens
                else {"type": "adaptive"}
            )
        if agents:
            kwargs["agents"] = agents
        # A json_schema the CLI enforces on the final message, so the caller
        # reads ResultMessage.structured_output instead of regex-scraping
        # fenced JSON out of the prose. Only set when asked, so a run without
        # it leaves the SDK's own default (None) — asserted by test_backend.
        if output_format is not None:
            kwargs["output_format"] = output_format
        # Additive: ClaudeAgentOptions.env merges into the subprocess
        # environment rather than replacing it, and this is a fresh options
        # object per call, so nothing here can leak across a reused backend
        # instance or another role's session, and never into `os.environ`.
        env: dict[str, str] = {}
        if self.compact_window_tokens:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(int(self.compact_window_tokens))
        env.update(self.extra_env)
        # The agent-session mark (session_mark.py): every subprocess this
        # backend launches is stamped so the gate-ending act sites can refuse
        # a caller descended from it, regardless of how that caller is
        # invoked. Stamped last so it always wins over `extra_env`. Merges
        # into the SDK's `env` (additive over the subprocess environment,
        # per the comment above) — never into this process's `os.environ`.
        env.update(mark_env("claude"))
        # Deny the coder subprocess the launcher's ambient secrets. The SDK
        # inherits the whole parent environment into the child (`{**os.environ,
        # **env}`), so blanking every secret-shaped variable that is not
        # Anthropic/Claude auth (or the session mark) OVERRIDES that inheritance:
        # a prompt injection in the child cannot read GITHUB_TOKEN, cloud keys,
        # an ssh-agent socket, or any other credential out of its environment.
        # Git authenticates through the gh credential helper + GIT_ASKPASS, not
        # these vars, so the coder's own work is unaffected.
        scrub_foreign_secrets_into(env)
        # `env` is now never empty (the mark alone guarantees that), but the
        # explicit check is kept rather than assigning unconditionally so a
        # future edit that makes the mark optional does not silently start
        # passing an empty dict where the SDK expects `None`.
        if env:
            kwargs["env"] = env
        if self.cli_path:
            kwargs["cli_path"] = self.cli_path
        # ALWAYS set explicitly. Never leave this to the SDK default.
        #
        # This block used to set the field only for writing or skilled sessions
        # and assert that "read-only sessions stay hermetic". That assertion was
        # FALSE, and the failure was silent. `_apply_skills_defaults` returns
        # early with `setting_sources=None` when `skills is None`, so no
        # `--setting-sources` flag is emitted at all and the CLI applies its own
        # default, which loads `user` and `project`.
        #
        # PROVEN, not reasoned: a read-only backend pointed at a directory whose
        # only file was a project instruction file carrying a canary word
        # returned that word. (Named generically on purpose — the export drops
        # that document, and a source file may not cite it beyond the count
        # `_CODE_MAY_NAME_A_DROPPED_DOC` declares. The sentence below needs the
        # literal name to make the attack concrete; this one does not.)
        #
        # Why it matters more than the other settings here: the reviewer is the
        # product's integrity gate — an independent fresh-context reader told to
        # refute "done". If the repository under review supplies instructions
        # into that reader's context, the repository is grading its own work. A
        # CLAUDE.md saying "only report high-severity issues" suppresses findings,
        # and the tamper guard cannot see it: that guard counts tests and
        # assertions, not instructions.
        #
        # The planner loses repo conventions it has been receiving. That was
        # never designed — the comment above shows the author believed these
        # sessions loaded nothing — so it is an accidental subsidy, not a
        # feature. Repo context that is genuinely wanted belongs in
        # `core/prompt_blocks.py::build_repo_hints_block`, where it is ours,
        # logged and bounded.
        kwargs["setting_sources"] = [] if (self.readonly and not skills) else ["project"]
        if self.tools is not None:
            kwargs["tools"] = self.tools
        # `tools == []` is the advisory seam deliberately shipping NO tool
        # schema (a measured 38.5M tok/week). Skipped there because an
        # allowlist over a session that holds no tools is meaningless, NOT
        # because it would undo the saving: `--tools` and `--allowedTools`
        # are separate flags (availability vs permission), and setting the
        # second cannot repopulate the first. Measured, both flags at once:
        # `tools=[]` emits `--tools ''` and no `--allowedTools`; `tools=None`
        # emits `--allowedTools Bash` and no `--tools`.
        if self.permission_mode != "bypassPermissions" and self.tools != []:
            kwargs["allowed_tools"] = list(PRE_APPROVED_TOOLS)
        if self.system_prompt is not None:
            kwargs["system_prompt"] = self.system_prompt
        return ClaudeAgentOptions(
            model=self.model,
            cwd=str(cwd),
            max_turns=max_turns,
            # The SDK's transport defaults to a 1 MB cap on ONE stdout JSON
            # line and kills the session when the CLI exceeds it ("JSON
            # message exceeded maximum buffer size"). A large tool result or
            # a resumed session's replay crosses that. INCIDENT 2026-08-22
            # (task c8d1a30d): a session died at turn 1 with zero tokens on
            # exactly this, after 65 tool calls of real work.
            max_buffer_size=SDK_MAX_BUFFER_BYTES,
            permission_mode=self.permission_mode,
            effort=effort,
            resume=resume,
            hooks=hooks,
            skills=skills or None,
            **kwargs,
        )

    async def stream(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = None,
        resume: str | None = None,
        supervisor_hook: SupervisorHook | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, AgentDefinition] | None = None,
        on_compact: Callable[[str], None] | None = None,
        output_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent, yielding normalized events; the final event is ``result``."""
        options = self._options(cwd, max_turns, effort=effort, resume=resume,
                                supervisor_hook=supervisor_hook, lint_hook=lint_hook,
                                skills=skills, thinking=thinking,
                                max_thinking_tokens=max_thinking_tokens,
                                agents=agents, on_compact=on_compact,
                                output_format=output_format)
        # The SDK signals terminal conditions (notably hitting max_turns) by
        # *raising* a bare Exception from inside query(). It usually emits a
        # ResultMessage first ("agent done: N turns") and THEN raises, so we
        # cannot simply re-raise once a result was seen — that's exactly the
        # max_turns crash. If the raise escaped it would crash the whole
        # orchestrator and never reach the bounded-loop retry/escalate path
        # (constraint #5). So we never let it escape: we emit a corrective
        # is_error result event. run() keeps the LAST result event, so this
        # supersedes any prior (non-error) ResultMessage and the orchestrator
        # treats the attempt as failed rather than crashing or committing
        # half-finished work.
        last_turns = last_tokens = 0
        # The PARENT's output tokens, and whether any usage block was ever
        # seen. `saw_usage` is what keeps "never reported" (None -> SQL NULL)
        # distinct from "reported zero output" (0), which are different facts
        # and price differently.
        last_output = 0
        saw_usage = False
        # The CLI's OWN explanation, carried on the last result event. The
        # SDK then replaces the trailing ProcessError with
        # "Claude Code returned an error result: <subtype>" — which for a
        # quota rejection is the word "success", the least informative
        # string available — and run() keeps the LAST event, so without
        # this the real reason is overwritten and lost.
        last_result_text = ""
        last_api_error_status: int | None = None
        # The CLI always exits non-zero after an is_error ResultMessage, so
        # the terminal-exception handler below ALWAYS runs after this branch
        # and its corrective event is the one run() keeps — the derived
        # `stop_reason` above would otherwise be thrown away every time. This
        # carries the SDK's structured subtype across that seam so the
        # exception handler can fold it in, instead of relying solely on the
        # rewritten exception text (which for error_max_turns reads "Claude
        # Code returned an error result: error_max_turns" and contains no
        # "maximum number of turns" wording to match).
        last_subtype: str | None = None
        # Carried onto the corrective error event below. run() keeps the LAST
        # result event, so anything missing there is lost: an attempt that hit
        # max_turns used to record 0 cache-read tokens — the attempts that burn
        # the most reporting nothing at all.
        last_cache_read = last_cache_creation = 0
        last_session: str | None = None
        # message_ids already reported as a "usage" event, so the orchestrator's
        # running total sees each API response once. See the emit site below.
        seen_usage_mids: set[str] = set()
        # Task-subagent spend, keyed by the Task tool call's tool_use_id:
        # `sub_streamed` holds their assistant messages (deduped by message_id),
        # `sub_reported` the CLI's own scalar rollup per subagent.
        sub_streamed: dict[str, dict[str, tuple[int, int, int, int]]] = {}
        sub_reported: dict[str, int] = {}
        try:
            async for message in query(prompt=prompt, options=options):
                # Tool RESULTS arrive in a UserMessage, not an AssistantMessage:
                # an assistant message carries the ToolUseBlock (the call), and the
                # result comes back as a user turn. A `ToolResultBlock` branch used to
                # sit inside the AssistantMessage loop below, so it was UNREACHABLE —
                # 0 tool_result events across 35 attempts against 1,497 tool_use — and
                # the `_TOOL_RESULT_CAP` truncation a previous author wrote has never
                # executed once. Verified against the SDK types: UserMessage carries
                # `content: str | list[ContentBlock]` and `tool_use_result`.
                #
                # We emit the SIZE, never the text. PR-024 measured that 72% of an
                # attempt's cost is the conversation re-read every turn, and tool
                # results are the payload — but persisting that text would bloat the DB
                # by ~1,500 results per session AND risk capturing whatever a command
                # printed, including credentials. The size is what the truncation
                # threshold must be chosen from; the text is not needed for it.
                if isinstance(message, UserMessage):
                    blocks = message.content
                    if isinstance(blocks, list):
                        for block in blocks:
                            if isinstance(block, ToolResultBlock):
                                # Bound once: it labels the population below AND
                                # gates the exit code, which is meaningless on a
                                # result that did not fail.
                                is_error = bool(getattr(block, "is_error", False))
                                yield AgentEvent(
                                    "tool_result",
                                    meta={
                                        # JOIN KEY — pairs this size with its tool.
                                        "tool_use_id": block.tool_use_id,
                                        # A SUBAGENT's results are re-read in the
                                        # SUBAGENT's context, not the main conversation
                                        # whose 72% re-read cost is the target. Counting
                                        # them undifferentiated inflates the population
                                        # the threshold is chosen from.
                                        "parent_tool_use_id": message.parent_tool_use_id,
                                        # Error results are short and a different
                                        # population; they must be excludable.
                                        "is_error": is_error,
                                        **_result_size(block.content),
                                        # The command's own status when a FAILED
                                        # result states one — a refusal LOOP is
                                        # only countable if a refusal is
                                        # distinguishable from a pass.
                                        **_exit_status(block.content,
                                                       is_error=is_error),
                                    },
                                )
                    continue
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ThinkingBlock):
                            yield AgentEvent("thinking", text=block.thinking)
                        elif isinstance(block, TextBlock):
                            yield AgentEvent("text", text=block.text)
                        elif isinstance(block, ToolUseBlock):
                            # `id` is the JOIN KEY. Without it the tool_result size
                            # distribution cannot be sliced BY TOOL — and the whole
                            # point (PR-024) is that Bash is 62% of calls and is the
                            # unbounded one, so the truncation threshold must be
                            # per-tool. Index-pairing is unsound: one assistant turn
                            # can carry several ToolUseBlocks.
                            yield AgentEvent(
                                "tool_use",
                                tool_name=block.name,
                                tool_input=block.input,
                                meta={"tool_use_id": block.id},
                            )
                    # Per-message usage → a running mid-attempt total in the
                    # orchestrator's sink (B2 #2), which ADDS UP every event it
                    # receives.
                    #
                    # The stream repeats one API response across several
                    # assistant messages that share a `message_id` and carry a
                    # byte-identical usage block (97,546 of 97,547 repeat groups
                    # across 42,925 sessions were identical; the lone exception
                    # was an early partial later revised upward). Emitting one
                    # event per message therefore counted the same tokens
                    # 2-3x — median 2.00x, aggregate 2.11x — and the watch
                    # aborted healthy attempts against a bill never incurred.
                    # First occurrence only; a message without an id (never
                    # observed) is passed through rather than silently dropped.
                    #
                    # NOTE the asymmetry with `sub_streamed` below, which is
                    # last-wins: a stream cannot retract an event already
                    # emitted, so the watch necessarily keeps the FIRST value.
                    # Only one revision exists in 97,547 observed repeat groups
                    # and it moved upward, so the watch can trail the ledger by
                    # that single delta. Not worth delta-tracking for one
                    # observation in ~10^5.
                    usage = message.usage or {}
                    mid = message.message_id
                    if usage and (mid is None or mid not in seen_usage_mids):
                        if mid is not None:
                            seen_usage_mids.add(mid)
                        yield AgentEvent(
                            "usage",
                            meta={
                                "tokens_used": int(usage.get("input_tokens", 0))
                                + int(usage.get("output_tokens", 0)),
                                # The output SLICE of the total beside it, so
                                # the in-flight budget watch prices output at
                                # its real ~5x rate instead of at the input
                                # rate. Not an extra addend — see
                                # `core.pricing.OUTPUT_EXTRA_WEIGHT`.
                                "output_tokens": int(
                                    usage.get("output_tokens", 0)),
                                "cache_read_tokens": int(
                                    usage.get("cache_read_input_tokens", 0)),
                                "cache_creation_tokens": int(
                                    usage.get("cache_creation_input_tokens", 0)),
                            },
                        )
                    # Subagent spend, which `ResultMessage.usage` omits. A Task
                    # subagent's assistant messages run inside the PARENT's
                    # session_id — that field cannot tell them apart — but they
                    # carry `parent_tool_use_id` = the Task tool call's id.
                    # Keyed by message_id so a repeat overwrites instead of
                    # adding; the later of a revised pair is the fuller one.
                    if usage and message.parent_tool_use_id:
                        bucket = sub_streamed.setdefault(
                            message.parent_tool_use_id, {})
                        # An id-less message (never observed) gets a key unique
                        # WITHIN its bucket, so it is kept rather than silently
                        # overwriting a sibling.
                        key = mid or f"_anon{len(bucket)}"
                        quad = _usage_quad(usage)
                        # LAST-WINS on a repeated id. Across 42,925 sessions and
                        # 97,547 repeat groups, 97,546 were byte-identical and
                        # the single revision moved UPWARD (an early partial
                        # later completed); no revision has ever moved down. So
                        # the later record is the fuller one — except that an
                        # all-zero block would overwrite a real measurement with
                        # nothing, which is never a revision worth taking.
                        if any(quad) or key not in bucket:
                            bucket[key] = quad
                elif isinstance(message, TaskStartedMessage):
                    yield AgentEvent(
                        "subagent_start",
                        text=message.description,
                        meta={
                            "task_id": message.task_id,
                            "task_type": message.task_type,
                            "session_id": message.session_id,
                        },
                    )
                elif isinstance(message, TaskProgressMessage):
                    yield AgentEvent(
                        "subagent_progress",
                        text=message.description,
                        meta={
                            "task_id": message.task_id,
                            "last_tool_name": message.last_tool_name,
                            "session_id": message.session_id,
                        },
                    )
                elif isinstance(message, TaskNotificationMessage):
                    # The CLI's own gauge for this subagent. total_tokens is NOT
                    # a bill: it is (LAST request's input + cache_creation +
                    # cache_read) + SUM(output over every streamed occurrence,
                    # duplicates included) — see `_rollup_subagents`, which uses
                    # it ONLY as a floor for a subagent that streamed nothing.
                    # Keyed by tool_use_id (the join key to the assistant
                    # messages' parent_tool_use_id, whereas task_id joins to
                    # nothing); last notification per task wins.
                    if message.usage and message.tool_use_id:
                        sub_reported[message.tool_use_id] = int(
                            message.usage.get("total_tokens", 0) or 0)
                    yield AgentEvent(
                        "subagent_done",
                        text=message.summary,
                        meta={
                            "task_id": message.task_id,
                            "status": message.status,
                            "session_id": message.session_id,
                            "total_tokens": (message.usage or {}).get("total_tokens"),
                            "tool_uses": (message.usage or {}).get("tool_uses"),
                        },
                    )
                elif isinstance(message, ResultMessage):
                    usage = message.usage or {}
                    # `_usage_quad` has always returned all four numbers; this
                    # site used to inline two of them and add them together,
                    # which is where the input/output split was lost.
                    in_tokens, out_tokens, cache_read, cache_creation = _usage_quad(
                        usage)
                    tokens = in_tokens + out_tokens
                    if message.usage:
                        # Only a real usage block counts as having SEEN a
                        # split. Without this flag an errored result with no
                        # usage would report output=0 — indistinguishable from
                        # a run that genuinely emitted none — and that 0 is
                        # what would land in the DB column.
                        saw_usage = True
                        last_output += out_tokens
                    denials = [str(d) for d in (message.permission_denials or [])]
                    last_turns, last_tokens = message.num_turns, last_tokens + tokens
                    # ONLY from an ERRORED result. Capturing a SUCCESSFUL
                    # run's prose here and prepending it below meant a normal
                    # finish followed by a transport error ("Stream closed")
                    # inherited the agent's own summary — and a summary that
                    # happens to mention "rate limit" or "quota" (routine in
                    # THIS codebase, which is full of quota-handling code) then
                    # tripped `_quota_signal`, parking a healthy task as
                    # PAUSED_QUOTA and aborting its bounded loop. It also fed
                    # variable prose into `stuck.record`, so identical failures
                    # stopped hashing to the same signature and stuck detection
                    # broke. The SDK only synthesises the
                    # "error result: <subtype>" wrapper when the result was
                    # itself an error, so this gate costs the incident nothing.
                    if message.is_error:
                        last_result_text = (message.result or "").strip()
                        last_api_error_status = message.api_error_status
                    last_cache_read += cache_read
                    last_cache_creation += cache_creation
                    last_session = message.session_id
                    last_subtype = message.subtype
                    sub_io, sub_cr, sub_cc, sub_n, sub_floored = _rollup_subagents(
                        sub_streamed, sub_reported)
                    # The SDK reports turn exhaustion two ways. The terminal-
                    # EXCEPTION path (`_run_once`'s handler below) sets
                    # stop_reason="max_turns"; the NORMAL path ends the session
                    # with a ResultMessage whose `subtype` is "error_max_turns"
                    # and whose stop_reason is None. Both are the same event
                    # and must reach the orchestrator as the same signal, or
                    # only one of them gets the bounded retry. Keyed on the
                    # STRUCTURED subtype, never on the text: a plan that
                    # merely QUOTES "Reached maximum number of turns" must not
                    # be read as exhaustion.
                    stop_reason = message.stop_reason
                    if not stop_reason and message.subtype == "error_max_turns":
                        stop_reason = "max_turns"
                    yield AgentEvent(
                        "result",
                        text=message.result or "",
                        meta={
                            "num_turns": message.num_turns,
                            "is_error": message.is_error,
                            # The three totals are PARENT + SUBAGENTS. They feed
                            # the attempt ledger directly (orchestrator
                            # update_attempt), so folding the rollup in here is
                            # what actually stops subagents billing as free.
                            "tokens_used": last_tokens + sub_io,
                            # The PARENT's output only, and deliberately so.
                            # The parent's figure is exact (verified byte-exact
                            # against ResultMessage.usage); the subagent
                            # stream's output is a documented early snapshot,
                            # and a FLOORED subagent has no output signal at
                            # all. Summing an exact number with an unreliable
                            # one and calling the result "the output share"
                            # would hide which is which. So this is a LOWER
                            # BOUND whenever subagents ran: their output stays
                            # inside `tokens_used` and prices at the input
                            # rate, the same under-count as before this
                            # column existed, now confined to the subagent
                            # share instead of the whole run.
                            "output_tokens": last_output if saw_usage else None,
                            "session_id": message.session_id,
                            "stop_reason": stop_reason,
                            "subtype": message.subtype,
                            "denials": denials,
                            "api_error_status": message.api_error_status,
                            "cache_read_tokens": last_cache_read + sub_cr,
                            "cache_creation_tokens": last_cache_creation + sub_cc,
                            "subagent_tokens_used": sub_io,
                            "subagent_cache_read_tokens": sub_cr,
                            "subagent_cache_creation_tokens": sub_cc,
                            "subagent_count": sub_n,
                            "subagent_floored_count": sub_floored,
                            # The parsed answer when the run was given an
                            # output_format; None otherwise (older CLIs too).
                            "structured_output": getattr(
                                message, "structured_output", None),
                        },
                    )
        except Exception as exc:  # noqa: BLE001 — SDK raises bare Exception on terminal errors
            import traceback
            msg = str(exc)
            # The CLI always exits non-zero after an is_error ResultMessage, so
            # this handler runs on EVERY max_turns ResultMessage, not only the
            # cases where the SDK's rewritten exception text happens to say
            # "maximum number of turns" — for error_max_turns it instead reads
            # "Claude Code returned an error result: error_max_turns", which
            # contains no such phrase. `last_subtype` is the structured signal
            # from the ResultMessage seen just above; OR it in so this
            # corrective event doesn't clobber that already-derived exhaustion.
            is_max_turns = (
                "maximum number of turns" in msg.lower()
                or last_subtype == "error_max_turns"
            )
            # Preserve the traceback for genuine errors — a bare "'bool' object is
            # not subscriptable" with no file:line burned 3 attempts undiagnosably
            # (task 6cfdb936). max_turns is not an error, so it keeps the clean msg.
            tb = "" if is_max_turns else traceback.format_exc()
            text = msg if is_max_turns else f"{msg}\n\n{tb[-3000:]}".strip()
            # Lead with what the CLI actually said. A spend-limit rejection
            # reported only as "returned an error result: success" cost a full
            # day of debugging: every attempt showed turns=1, tokens=0 and an
            # error message that named no cause, while the CLI had already
            # said "You've hit your monthly spend limit".
            # Capped like the traceback beside it: an unbounded result is
            # persisted on the event AND fed to `error_signature`.
            if last_result_text and not is_max_turns:
                text = f"{last_result_text[:4000]}\n\n{text}"
            # A run that dies mid-flight still spent whatever its subagents
            # spent, and this corrective event is the only one run() keeps.
            sub_io, sub_cr, sub_cc, sub_n, sub_floored = _rollup_subagents(
                sub_streamed, sub_reported)
            yield AgentEvent(
                "result",
                text=text,
                meta={
                    "num_turns": last_turns or (max_turns if is_max_turns else 0),
                    "is_error": True,
                    "tokens_used": last_tokens + sub_io,
                    # A run that died mid-flight may never have seen a usage
                    # block; NULL then, not 0.
                    "output_tokens": last_output if saw_usage else None,
                    "session_id": last_session,
                    "stop_reason": "max_turns" if is_max_turns else "error",
                    "denials": [],
                    "api_error_status": last_api_error_status,
                    "cache_read_tokens": last_cache_read + sub_cr,
                    "cache_creation_tokens": last_cache_creation + sub_cc,
                    "subagent_tokens_used": sub_io,
                    "subagent_cache_read_tokens": sub_cr,
                    "subagent_cache_creation_tokens": sub_cc,
                    "subagent_count": sub_n,
                    "subagent_floored_count": sub_floored,
                    "traceback": tb[-4000:] or None,
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
        supervisor_hook: SupervisorHook | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, AgentDefinition] | None = None,
        on_compact: Callable[[str], None] | None = None,
        output_format: dict[str, Any] | None = None,
    ) -> AgentResult:
        """Run to completion, retrying ONCE if the transport died, not the task.

        The failure this guards is the 2026-07-11 parallel-run incident: three
        workers plus the operator's own outer agent session, and the REVIEWER's
        nested Agent-SDK subprocess came back "Stream closed". The pool was
        dropped to one worker and has stayed there.

        Three properties, each of which was missing:

        1. **Bounded.** Exactly one retry (`_TRANSPORT_RETRIES`), after a short
           pause, and only for `_TRANSPORT_FAILURE_MARKERS`. A task failure —
           wrong code, failing tests, a refusal — is returned untouched on the
           first pass, as before.
        2. **Never silent.** Both the retry and the give-up emit an event
           through the caller's `on_event`, so `nh watch`, the event log and
           the DB all show that a second session was spent. A retry nobody can
           see is indistinguishable from a flaky product.
        3. **Attributable.** The give-up text names the worker and the
           concurrency it was dispatched into. Nothing in the database recorded
           that before, which is precisely why the incident could only ever be
           *asserted* to be a concurrency problem.

        The first attempt's spend is folded into the returned result. It was
        really burned, and a retry that silently reset the ledger would make
        this change look free while making the bill go up.

        **THE MULTIPLIER, stated because it is not obvious from here.** This
        retry is not the only one in the review path, and the two COMPOSE.
        ``review/reviewer.py:_agent_review`` already runs its own bounded
        infra retry (``_REVIEW_INFRA_RETRIES = 1``, so 2 rounds), and every one
        of those rounds calls this method, which may spend 2 sessions. The
        worst case for ONE review gate is therefore:

            2 reviewer rounds x (1 session + 1 transport retry) = **4 sessions**

        and across a task's bounded loop (constraint #5, ``max_attempts=3``,
        one gate per attempt):

            3 attempts x 4 = **<=12 reviewer sessions per task**

        Before this change the same numbers were 2 and 6. Nothing here is
        unbounded — every factor is a named constant — but a reader pricing a
        review gate must multiply by 2, not assume it. The wall-clock half of
        the same bound is documented on ``_agent_review``.
        """
        first = await self._run_once(
            prompt, cwd=cwd, max_turns=max_turns, effort=effort, resume=resume,
            on_event=on_event, supervisor_hook=supervisor_hook,
            lint_hook=lint_hook, skills=skills, thinking=thinking,
            max_thinking_tokens=max_thinking_tokens, agents=agents,
            on_compact=on_compact, output_format=output_format,
        )
        if not is_transport_failure(first):
            return first

        where = describe_concurrency()
        # The de-wrapped opening, not `splitlines()[0]`: the shape that made
        # this retry fire may be a message a terminal broke in half, and
        # "nested Agent SDK session died in the transport (Stream)" tells the
        # operator nothing about which death it was.
        reason = _opening_span((first.final_text or "").strip())[0][:200]
        for attempt in range(_TRANSPORT_RETRIES):
            if on_event is not None:
                on_event(AgentEvent(
                    "transport_retry",
                    text=(
                        f"nested Agent SDK session died in the transport "
                        f"({reason}) — {where}; retrying once in "
                        f"{_TRANSPORT_RETRY_DELAY_S:g}s"
                    ),
                    meta={
                        "attempt": attempt + 1,
                        "of": _TRANSPORT_RETRIES,
                        "concurrency": where,
                        "reason": reason,
                        # The discarded session's own shape. `_fold_spend`
                        # deliberately does NOT merge these into the returned
                        # result (that would describe a session that never
                        # existed), so this event is where they survive.
                        "discarded_turns": first.num_turns,
                        "discarded_tokens": first.tokens_used,
                        "discarded_subagents": first.subagent_count,
                    },
                ))
            await asyncio.sleep(_TRANSPORT_RETRY_DELAY_S)
            again = await self._run_once(
                prompt, cwd=cwd, max_turns=max_turns, effort=effort,
                resume=resume, on_event=on_event,
                supervisor_hook=supervisor_hook, lint_hook=lint_hook,
                skills=skills, thinking=thinking,
                max_thinking_tokens=max_thinking_tokens, agents=agents,
                on_compact=on_compact, output_format=output_format,
            )
            _fold_spend(first, into=again)
            if not is_transport_failure(again):
                return again
            first = again

        # Out of retries. The text keeps the CLI's own wording FIRST and
        # unaltered, because `orchestrator._classify_error` reads it and must
        # still answer "infra" — the diagnosis is appended, never substituted.
        first.final_text = (
            f"{first.final_text}\n\n"
            f"{TRANSPORT_DIAGNOSIS_MARKER} this nested Agent SDK session died "
            f"in the transport "
            f"and was retried {_TRANSPORT_RETRIES} time(s); every attempt died "
            f"the same way. {where}. This is an infrastructure failure of the "
            f"session, not a defect in the task or its diff — nothing was "
            f"reviewed and nothing should be blamed on the change. If the "
            f"concurrency above is greater than 1, suspect the shared "
            f"subscription before suspecting the code: every pool worker "
            f"spends one token against one rate-limit bucket."
        )
        if on_event is not None:
            on_event(AgentEvent(
                "transport_failed",
                text=first.final_text,
                meta={"concurrency": where, "retries": _TRANSPORT_RETRIES,
                      "reason": reason},
            ))
        return first

    async def _run_once(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = None,
        resume: str | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        supervisor_hook: SupervisorHook | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, AgentDefinition] | None = None,
        on_compact: Callable[[str], None] | None = None,
        output_format: dict[str, Any] | None = None,
    ) -> AgentResult:
        """One session: consume the stream, keep the LAST result event."""
        final = AgentResult(
            final_text="", num_turns=0, is_error=False, tokens_used=0,
            session_id=None, stop_reason=None,
        )
        async for event in self.stream(
            prompt, cwd=cwd, max_turns=max_turns, effort=effort, resume=resume,
            supervisor_hook=supervisor_hook, lint_hook=lint_hook, skills=skills,
            thinking=thinking, max_thinking_tokens=max_thinking_tokens,
            agents=agents, on_compact=on_compact, output_format=output_format,
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
                    # NOT coerced through `int(... or 0)`: None must survive as
                    # None all the way to the DB column, where it is the
                    # difference between "unknown" and "emitted no output".
                    output_tokens=(
                        None if m.get("output_tokens") is None
                        else int(m["output_tokens"])
                    ),
                    api_error_status=m.get("api_error_status"),
                    subagent_tokens_used=int(m.get("subagent_tokens_used", 0)),
                    subagent_cache_read_tokens=int(
                        m.get("subagent_cache_read_tokens", 0)),
                    subagent_cache_creation_tokens=int(
                        m.get("subagent_cache_creation_tokens", 0)),
                    subagent_count=int(m.get("subagent_count", 0)),
                    subagent_floored_count=int(
                        m.get("subagent_floored_count", 0)),
                    structured_output=m.get("structured_output"),
                )
        return final


# Re-exported for the ~50 sites that import these from here. They now live in
# ``agent/backend.py`` (the vendor-independent seam) but are the same objects.
__all__ = ["AgentEvent", "AgentResult", "CLAUDE_CAPABILITIES", "ClaudeBackend"]
