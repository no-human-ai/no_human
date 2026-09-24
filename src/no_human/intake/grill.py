"""B2: Native intake interrogation ("grill").

A relentless one-question-at-a-time clarifier that front-loads human input
to produce a crisp, unambiguous spec + acceptance criteria before the agent
starts implementing.

Pattern adapted from mattpocock's grilling skill:
  - One question at a time with suggested answers
  - Walk the decision tree
  - Substitute codebase exploration for questions the code can answer
  - Never ask what the code already shows

Runs at the one moment the human is present (task creation), then they step
out and the autonomous pipeline takes over.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("no_human.intake.grill")


@dataclass
class GrillQuestion:
    """One clarifying question from the intake grill."""

    question: str
    suggestions: list[str]
    round: int
    # What THIS round cost. Each grill_step is one billed backend call and the
    # caller loops, so the per-step figures are the only honest unit; neither
    # return type carried them before, so the whole interactive intake billed
    # as free.
    tokens_used: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class GrillResult:
    """The final refined spec produced by the grill."""

    title: str
    description: str
    acceptance_criteria: list[str]
    qa_log: list[dict] = field(default_factory=list)
    tokens_used: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


MAX_ROUNDS_DEFAULT = 5


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


_GRILL_PROMPT = """\
You are a relentless intake interviewer for an autonomous coding agent.
Your job: take a potentially vague task and turn it into a crisp,
unambiguous specification with testable acceptance criteria — so the
implementing agent can work WITHOUT any further human interaction.

RULES:
1. Explore the codebase FIRST (use Read, Grep, Glob tools) to understand
   the existing code, patterns, and conventions. NEVER ask the user
   something the code already answers.
   DISCOVERY IS REPO-SCOPED. Your working directory IS the task repository
   root: {repo_root}. Find files with `find {repo_root}/ -name '<glob>'` and
   search with `grep -r '<pattern>' {repo_root}/` (or the Glob/Grep tools).
   NEVER scan the whole filesystem (`find /`, `grep -r <pattern> /`,
   `ls -R /`): a pre-execution guard rejects those with 'must be
   repo-scoped', and they exhaust your turn budget before you can emit the
   block.
2. Ask ONE question at a time. Provide 2-4 suggested answers (labeled
   A, B, C, D) based on what you found in the code.
3. Each question must resolve a genuine ambiguity about INTENT, SCOPE,
   or CONSTRAINTS — things only the human can decide.
4. When you have enough clarity to write unambiguous, testable acceptance
   criteria, STOP asking and emit the final spec.
5. You must emit the final spec after at most {max_rounds} questions total.
{force_clause}
You are on round {round_n} of {max_rounds}.

TASK:
Title: {title}
Description: {description}
{qa_section}
Respond with a fenced ```json block in EXACTLY one of these two formats:

To ask a question:
```json
{{"type": "question", "question": "your single question", "suggestions": ["A: first option", "B: second option"]}}
```

To emit the final spec (when you have enough clarity):
```json
{{"type": "done", "title": "refined title", "description": "clear description of what to implement", "acceptance_criteria": ["criterion 1", "criterion 2"]}}
```

Emit EXACTLY one ```json block. No other prose after it.
"""


# Appended to the Previous Q&A section, and ONLY when there is history — an
# empty history has nothing to not-re-ask, so the rule would be noise.
# Deliberately brace-free: this string is a *substituted value* for
# _GRILL_PROMPT.format(), so braces are safe today, but staying brace-free
# makes it immune to a future double-format refactor.
_ANSWERED_RULES = """\
RULES ABOUT THE Q&A ABOVE:
  - NEVER ask a question that is already answered in the Previous Q&A
    above — not in the same words, and not in any rewording or narrowed
    variant that seeks the same decision. The user has answered it. Asking
    again is a RULE VIOLATION: it wastes one of the user's limited rounds
    and reads as though their answer was ignored. Treat every answer above
    as settled, move to a DIFFERENT open ambiguity, or emit the final spec.
  - An answer may DECLINE to specify further instead of choosing: it defers
    the decision to you, states no preference, or asks you to proceed,
    continue, or skip ahead with what is already known. Judge this by what
    the answer MEANS, not by matching any particular words or phrases, in
    any phrasing and any language. Such an answer is a STOP signal, not an
    ordinary answer. Do NOT re-ask it, do NOT rephrase it, and do NOT ask
    about a different facet of the same ambiguity. Resolve what is left
    open yourself with the most conservative, reversible, repo-evidenced
    default, record that choice explicitly in the description or acceptance
    criteria so the implementing agent is not blocked, and emit the final
    spec now with type: "done".
"""


def _build_qa_section(qa_history: list[dict]) -> str:
    if not qa_history:
        return ""
    lines = ["Previous Q&A:"]
    for i, qa in enumerate(qa_history, 1):
        lines.append(f"  Q{i}: {qa['question']}")
        lines.append(f"  A{i}: {qa['answer']}")
    return "\n".join(lines) + "\n" + _ANSWERED_RULES


def parse_grill_response(
    text: str, round_n: int, qa_history: list[dict],
) -> GrillQuestion | GrillResult:
    """Parse the agent's response into a question or final result."""
    blocks = _JSON_BLOCK.findall(text or "")
    data = None
    if blocks:
        try:
            data = json.loads(blocks[-1])
        except json.JSONDecodeError:
            pass
    if data is None:
        # Try the whole text as JSON
        try:
            data = json.loads((text or "").strip())
        except (json.JSONDecodeError, TypeError):
            pass
    if data is None:
        return GrillQuestion(
            question="Could you describe what you want in more detail?",
            suggestions=["A: Let me add more context", "B: Proceed with what we have"],
            round=round_n,
        )

    # `.get(key, default)` returns the default only for an ABSENT key; a
    # present-but-null field yields None, which violates the `str`/`list[str]`
    # fields below and crashes a `for c in acceptance_criteria` consumer. `or`
    # collapses a null (or empty) field to its typed default.
    if data.get("type") == "done":
        return GrillResult(
            title=data.get("title") or "",
            description=data.get("description") or "",
            acceptance_criteria=data.get("acceptance_criteria") or [],
            qa_log=list(qa_history),
        )

    return GrillQuestion(
        question=data.get("question") or "Please clarify your requirements.",
        suggestions=data.get("suggestions") or [],
        round=round_n,
    )


async def grill_step(
    title: str,
    description: str | None,
    repo_path: str | None,
    qa_history: list[dict],
    backend: Any,
    *,
    max_rounds: int = MAX_ROUNDS_DEFAULT,
    on_event: Any | None = None,
    timeout: float = 120,
) -> GrillQuestion | GrillResult:
    """Run one step of the intake grill.

    Each call is stateless — the caller maintains the Q&A history. Returns a
    :class:`GrillQuestion` if more clarification is needed, or a
    :class:`GrillResult` when the spec is clear enough.

    The backend should be a read-only :class:`ClaudeBackend` instance so the
    agent can explore the codebase (Read/Grep/Glob) but cannot modify it.

    Exactly one billed backend call happens per step, and BOTH return types
    carry its three token figures — so unlike the evaluator (whose returns are
    a bare ``list``/``str`` and which bills up to four calls per invocation, and
    therefore needs a ``usage_sink``), the return value is a sufficient
    accounting channel here. Callers own the booking: interactive intake runs
    before any task exists, so there is no attempt row and the GUI/CLI write
    these figures to ``unattributed_usage`` instead.
    """
    round_n = len(qa_history) + 1
    force = round_n >= max_rounds

    force_clause = (
        "\nThis is the LAST round. You MUST emit the final spec now "
        '(type: "done"). Do NOT ask another question.\n'
        if force
        else ""
    )

    cwd = Path(repo_path).resolve() if repo_path else Path(tempfile.gettempdir())

    prompt = _GRILL_PROMPT.format(
        title=title,
        description=description or "(none provided)",
        qa_section=_build_qa_section(qa_history),
        round_n=round_n,
        max_rounds=max_rounds,
        force_clause=force_clause,
        repo_root=str(cwd),
    )

    extra: dict[str, Any] = {}
    if on_event is not None:
        extra["on_event"] = on_event
    try:
        result = await asyncio.wait_for(
            backend.run(prompt, cwd=cwd, max_turns=4, effort="low", **extra),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # KNOWN UNDER-REPORT: wait_for cancelled the call, so no result object
        # exists and the fields below stay 0 — the round IS billed by the
        # provider and is recorded as free. Booking an explicit zero would be a
        # lie of a different shape; the incremental AgentEvent("usage") stream
        # could give a lower bound, but reading it means passing on_event, which
        # changes what the call does. See _bounded_run in evaluator.py.
        return GrillQuestion(
            question="The codebase exploration took too long. Can you describe the scope more narrowly?",
            suggestions=[
                "A: Let me provide a more specific description",
                "B: Skip scoping and create the task as-is",
            ],
            round=round_n,
        )

    usage = {
        "tokens_used": int(getattr(result, "tokens_used", 0) or 0),
        "cache_read_tokens": int(getattr(result, "cache_read_tokens", 0) or 0),
        "cache_creation_tokens": int(
            getattr(result, "cache_creation_tokens", 0) or 0),
    }

    parsed = parse_grill_response(result.final_text, round_n, qa_history)

    # Past max rounds and still got a question — force a result with what
    # we have so the pipeline is never blocked indefinitely.
    if force and isinstance(parsed, GrillQuestion):
        return GrillResult(
            title=title,
            description=description or "",
            acceptance_criteria=[f"Implement: {title}"],
            qa_log=list(qa_history),
            **usage,
        )

    for k, v in usage.items():
        setattr(parsed, k, v)
    return parsed
