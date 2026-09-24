"""The escalation-quality gate (gap-close W3): one bounded challenge before a
self-reported judgment-call blocker parks a deliverable task.

main-6cec2140 (2026-08-07 bench) put 12 of 26 failures in the burn-then-quit
class — real work, then an escalation on a task whose expected outcome was
delivery. The blocker taxonomy already separates the EXTERNAL categories
(missing access, quota, infra, a dependency, a spent budget) from the agent's
own JUDGMENT CALLS (ambiguity, novel-unknown, impossible). This module
challenges only the judgment calls, exactly once per task, with a one-turn
supervisor-tier check: *is this genuinely a human's question, or is it
answerable from repo evidence as a documented, reversible assumption?*

What this gate can and cannot do — the honest-escalation invariant:

- It NEVER converts a park into "done". A ``resolvable`` verdict costs the
  attempt (recorded FAILED with the reasoning) and re-enters the BOUNDED loop
  with the assumption on record; if the agent still cannot proceed, its next
  blocker is honored WITHOUT challenge (``blocker_challenged`` is per-task).
- External categories pass through untouched — a missing credential is a
  human's problem no matter how eloquent the supervisor is.
- The check itself is advisory infrastructure: any failure (timeout, parse
  miss, backend error) honors the blocker exactly as before. Fail-open toward
  honesty, never toward grinding.
- Verdicts are categorical with cited reasoning — never a numeric score
  (constraint #3).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .taxonomy import Blocker, BlockerCategory

#: The agent's own judgment calls — the only categories worth one challenge.
#: Everything else is structurally external (constraint #5 lists them as the
#: honest park reasons) and is honored without question.
CHALLENGEABLE: frozenset[BlockerCategory] = frozenset({
    BlockerCategory.AMBIGUITY,
    BlockerCategory.NOVEL_UNKNOWN,
    BlockerCategory.IMPOSSIBLE,
})

_CHALLENGE_JSON = re.compile(
    r"CHALLENGE_JSON_START\s*(\{.*?\})\s*CHALLENGE_JSON_END", re.DOTALL)


@dataclass
class ChallengeVerdict:
    #: "external" — honor the blocker; "resolvable" — one more bounded
    #: attempt under the stated assumption.
    verdict: str
    reasoning: str = ""
    assumption: str = ""


def build_challenge_prompt(task_title: str, criteria: list[str],
                           blocker: Blocker) -> str:
    crit = "\n".join(f"  - {c}" for c in criteria) or "  (none stated)"
    return (
        "An autonomous coding agent stopped a deliverable task and reported "
        "the blocker below. You are its supervisor. Decide ONE thing: is this "
        "blocker GENUINELY EXTERNAL (only a human can supply the missing "
        "piece: an unstated requirement, contradictory acceptance criteria, "
        "a real-world decision), or is it RESOLVABLE — answerable from the "
        "repository's own evidence, conventions, or a safe default, stated "
        "as a documented reversible assumption?\n\n"
        "Calibration: an agent that stops honestly is doing the right thing — "
        "do NOT strong-arm real ambiguity into an assumption. But 'I was not "
        "told X' where X is discoverable in the repo, derivable from its "
        "conventions, or covered by an obvious default is RESOLVABLE. If "
        "resolving would require weakening a test, editing acceptance "
        "criteria, expanding scope, or doing anything irreversible, it is "
        "EXTERNAL.\n\n"
        f"Task: {task_title}\n"
        f"Acceptance criteria:\n{crit}\n\n"
        f"Blocker category: {blocker.category.value}\n"
        f"Root-cause hypothesis: {(blocker.root_cause_hypothesis or '')[:600]}\n"
        f"Question for the human: {(blocker.question or '')[:600]}\n"
        f"What was tried: {'; '.join((blocker.tried or [])[:4])[:600]}\n"
        f"Evidence: {(blocker.evidence or '')[:600]}\n\n"
        "Reply with ONLY this block:\n"
        "CHALLENGE_JSON_START\n"
        '{"verdict": "external" | "resolvable", "reasoning": "cite the '
        'evidence for the verdict", "assumption": "the documented reversible '
        'assumption to proceed under (empty when external)"}\n'
        "CHALLENGE_JSON_END"
    )


def parse_challenge(text: str) -> ChallengeVerdict | None:
    """Fail SAFE: anything unparseable returns None and the blocker is
    honored. A "resolvable" verdict without a stated assumption is treated as
    unparseable — proceeding needs something to proceed UNDER."""
    m = _CHALLENGE_JSON.search(text or "")
    if not m:
        return None
    try:
        data: dict[str, Any] = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in ("external", "resolvable"):
        return None
    assumption = str(data.get("assumption", "")).strip()
    if verdict == "resolvable" and not assumption:
        return None
    return ChallengeVerdict(
        verdict=verdict,
        reasoning=str(data.get("reasoning", "")).strip(),
        assumption=assumption,
    )

def check_challenge_eligibility(config: dict[str, Any], blocker: Blocker,
                                context: dict[str, Any] | None) -> str | None:
    """Returns the skip reason if the challenge gate is disabled, the category
    is not challengeable, or the task was already challenged. Returns None
    if eligible."""
    if not (config.get("blockers") or {}).get("challenge", True):
        return "gate disabled"
    if blocker.category not in CHALLENGEABLE:
        return f"category not challengeable: {blocker.category.name}"
    if (context or {}).get("blocker_challenged"):
        return "already challenged"
    return None
