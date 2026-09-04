"""D1: Intake quality evaluation.

Lightweight LLM pass that classifies a task spec as one of:
  ACCEPT  — ready for implementation
  ENRICH  — good but could use stronger acceptance criteria (auto-enriched)
  CLARIFY — ambiguous, needs human input before proceeding
  DECOMPOSE — too large for a single agent session

Returns an EvalResult with verdict, quality dimensions, and optionally
enriched acceptance criteria. Advisory — never blocks the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.jsonparse import loads_lenient

log = logging.getLogger("no_human.evaluator")

# Hard wall-clock ceiling on every grill/intake LLM call. A hung SDK subprocess
# (Stream-closed transport) once wedged the whole bench ~12min at 0% CPU because
# the raw `be.run()` coroutine on a Stream-closed subprocess never returns and a
# hang is not an exception (so nothing catches it). wait_for turns a
# hang into a TimeoutError the callers already treat as "no result" (advisory),
# so the run proceeds instead of blocking forever. Normal grill calls finish well
# under this ceiling; it only bounds pathological hangs.
_LLM_TIMEOUT_S = 300

from types import SimpleNamespace  # noqa: E402

# Every intake call in this module is utility-tier spend that used to be
# discarded at the type boundary: the functions return verdicts, assumptions and
# Q&A, never an ``AgentResult``, so ``tokens_used``/``cache_read_tokens``/
# ``cache_creation_tokens`` had nowhere to go. Rather than widen four different
# return types (two of which are plain ``list``/``str``), each entry point takes
# a ``usage_sink`` and every backend call is handed to it. The sink is the
# orchestrator's ``_note_utility_usage`` in-process — the SAME sink the
# supervisor hook, context distillation and stuck-hypothesis calls already use,
# so intake lands in ``attempts.utility_*`` alongside them and nothing needs a
# second mechanism.
#
# The sink is called with the raw backend result, so it reads usage exactly the
# way ``claude_backend.AgentResult`` reports it (parent + subagent rollup).
UsageSink = Any  # Callable[[AgentResult], None]


def _record_usage(usage_sink: UsageSink | None, result: Any) -> None:
    """Hand one backend result to the caller's usage sink, never raising.

    Accounting must not be able to change what an advisory call DOES: a broken
    sink degrades the ledger, it may not degrade a verdict.
    """
    if usage_sink is None or result is None:
        return
    try:
        usage_sink(result)
    except Exception as exc:  # noqa: BLE001 — accounting never blocks intake
        log.warning("utility usage sink failed (spend unrecorded): %s", exc)


# The answering pass fails OFTEN and used to fail INVISIBLY: every branch below
# is wrapped by an advisory `except` (correct — the grill may never block a
# task), so the suite was equally green whether the pass succeeded 100% or 0% of
# the time. Live evidence, two independent runs on 2026-08-06: "grill answering
# emitted no block", "grill answering failed ... Expecting value: line 1 column
# 1", "all N answerable question(s) left unanswered". None of it was countable.
#
# So the pass now classifies its own outcome and hands it to an ``outcome_sink``
# — the same shape as ``usage_sink`` above, and for the same reason: the caller
# owns the recording mechanism, this module owns the classification. The
# orchestrator's sink emits a ``grill_answering`` task event (and a
# ``grill_questions`` one for the pass before it), which ``core/metrics.py``
# groups into ``grill_answering_outcomes`` / ``grill_questions_outcomes`` and
# ``doctor.MECHANISMS`` lists by kind. No new telemetry subsystem, no new
# dependency.
OutcomeSink = Any  # Callable[[str, dict[str, Any]], None]

#: Every value ``grill_spec`` can report for one answering pass. Exhaustive by
#: construction — a pass that runs reports exactly one of these.
#:
#: The comments below are load-bearing: this tuple is what the metric's readers
#: read to know what the buckets MEAN, and one of them was wrong. ``error`` was
#: documented as covering a timeout. It does not, and the mislabelled case is
#: the most expensive one there is (``max_turns=8``, spend unrecoverable): on
#: timeout ``_bounded_run`` swallows the ``TimeoutError`` and returns a
#: ``final_text=""`` sentinel, so the pass sees an empty reply, retries, and —
#: if the retry also times out — books ``no_block_after_retry``. Fixed here by
#: correcting the DOCUMENTATION rather than the classification: a timed-out
#: session genuinely emitted no block, so that bucket is the truthful one, and
#: re-routing it to ``error`` would merge a silent-model failure with an
#: infrastructure failure under a name that means neither. The timeout stays
#: separately countable through the ``timed_out`` FIELD on the event, which is
#: what ``_bounded_run``'s sentinel now carries.
GRILL_ANSWERING_OUTCOMES: tuple[str, ...] = (
    "parsed_first_try",              # block found and parsed, no retry
    "parsed_after_tool_less_retry",  # recovered by the bounded second call
    "no_block_after_retry",          # never emitted a block — INCLUDING a
                                     # timed-out session (empty sentinel); see
                                     # the `timed_out` field to tell them apart
    "unparseable_after_retry",       # emitted a block that was not JSON
    "error",                         # the pass RAISED (backend/wiring error);
                                     # a timeout does NOT reach here
)

#: The members of the tuple above that mean THE PASS PARSED A BLOCK — the
#: denominator ``metrics.grill_answering_answers`` divides by, because a pass
#: that never parsed has no answers to apply and counting its zero would blame
#: the wrong stage.
#:
#: DECLARED here rather than inferred at the reader. metrics.py used to compute
#: it as ``o.startswith("parsed")`` under the claim that it was "derived from
#: the enum, so an outcome added later cannot silently fall out of the
#: denominator". A prefix is a naming convention, not a property of the enum:
#: a future parsing outcome spelled ``recovered_*`` would have fallen out just
#: as silently, and nothing pinned the convention. This constant does not fix
#: that by itself — nothing can make a new member enrol automatically — so the
#: two ways it drifts are pinned by
#: ``test_the_parsed_subset_is_declared_and_agrees_with_the_enum`` instead: it
#: must be a SUBSET of the tuple above (a rename that breaks the link fails),
#: and it must agree EXACTLY with the ``parsed`` prefix in BOTH directions (a
#: ``parsed_*`` member added to the enum and forgotten here fails; a member
#: added here without the prefix fails, forcing the convention to be faced
#: rather than quietly abandoned).
GRILL_ANSWERING_PARSED_OUTCOMES: tuple[str, ...] = (
    "parsed_first_try",
    "parsed_after_tool_less_retry",
)

#: The same, for the QUESTIONS pass — which ran uninstrumented until 2026-08-07
#: and carried the identical unguarded-parse bug this module fixed for the
#: answers pass: a matched-but-invalid block went straight to ``loads_lenient``
#: outside any try, and ``data.get("questions")`` on a non-object raised one
#: line later. Both landed in the function's blanket handler, which returned
#: ``None``; ``grill_spec`` then returned ``None``, the orchestrator's
#: ``if not qa: return`` fired before its own advisory, and the whole grill
#: vanished WITHOUT A SINGLE EVENT. The live line "grill produced no parseable
#: GRILL_JSON block; retrying once" comes from this pass.
#:
#: ``empty_after_parse`` has no analogue in the answering tuple because the
#: failure is particular to this pass: a well-formed block whose ``questions``
#: list is absent, empty, or entirely unusable items. It parsed and produced
#: nothing, which is not the same failure as never parsing.
GRILL_QUESTIONS_OUTCOMES: tuple[str, ...] = (
    "parsed_first_try",        # block found and parsed, no retry
    "parsed_after_retry",      # recovered by the bounded second call
    "no_block_after_retry",    # never emitted a block (incl. a timed-out one)
    "unparseable_after_retry", # emitted a block that was not JSON/not an object
    "empty_after_parse",       # parsed, but yielded zero usable questions
    "error",                   # the pass RAISED (backend/wiring error)
)


def _record_outcome(
    sink: OutcomeSink | None, outcome: str, **fields: Any,
) -> None:
    """Hand one classified answering-pass outcome to the caller's sink.

    Never raises, for the same reason ``_record_usage`` never raises:
    instrumentation may degrade the record, it may not change what an advisory
    call DOES.
    """
    if sink is None:
        return
    try:
        sink(outcome, dict(fields))
    except Exception as exc:  # noqa: BLE001 — instrumentation never blocks
        log.warning("grill outcome sink failed (outcome unrecorded): %s", exc)


async def _bounded_run(be, *args, usage_sink: UsageSink | None = None, **kwargs):
    """be.run() with a hard timeout; on timeout return an empty error-result
    sentinel so `result.final_text or ""` still works and the grill proceeds.

    Recording lives HERE rather than at each call site so no branch (the parse
    retries, the tool-less answer fallback) can forget to book its own spend.

    KNOWN UNDER-REPORT — the timeout path books nothing. ``wait_for`` cancels
    the coroutine, so no ``AgentResult`` is ever returned and the sentinel below
    carries no figures; passing it to the sink would book a false zero, which is
    worse than a gap you can name. But "no usage figure exists" would be wrong:
    ``ClaudeBackend.stream`` yields an incremental ``AgentEvent("usage", ...)``
    per assistant message (claude_backend.py, the same channel ``_agent_sink``
    meters the coder with), and this function simply never passes ``on_event``.
    A lower bound IS obtainable, and providers do bill tokens generated before a
    disconnect — so a timed-out grill-answers call (``max_turns=8``, the most
    expensive intake call there is) is recorded as zero when it was not free.
    Wiring ``on_event`` through is the fix; it is deliberately not done here
    because it changes what the call does, and this change is accounting only.
    """
    try:
        result = await asyncio.wait_for(be.run(*args, **kwargs),
                                        timeout=_LLM_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("grill/intake backend.run timed out after %ss — proceeding "
                    "without it (advisory); its spend is unrecoverable",
                    _LLM_TIMEOUT_S)
        # `timed_out` so the caller's outcome record can name this case. The
        # sentinel is indistinguishable from "the model replied with nothing"
        # by its text alone, and the two have opposite fixes (raise the
        # timeout vs fix the prompt) at very different costs — this one has
        # already burned a full max_turns=8 session. It is a FIELD, not an
        # outcome: see GRILL_ANSWERING_OUTCOMES for why the bucket stays
        # `no_block_after_retry`.
        return SimpleNamespace(final_text="", is_error=True, timed_out=True)
    _record_usage(usage_sink, result)
    return result


def usage_of(result: Any) -> tuple[int, int, int]:
    """(tokens_used, cache_read, cache_creation) off any backend result."""
    return (
        int(getattr(result, "tokens_used", 0) or 0),
        int(getattr(result, "cache_read_tokens", 0) or 0),
        int(getattr(result, "cache_creation_tokens", 0) or 0),
    )


def _render(template: str, **fields: str) -> str:
    """Substitute ``{name}`` placeholders WITHOUT ``str.format``.

    These prompt templates embed a literal JSON example (``{"verdict": ...}``).
    ``str.format`` would parse those braces as replacement fields — the field
    name ends at the ``:``, so it looks up ``kwargs['"verdict"']`` and raises
    ``KeyError('"verdict"')``, silently disabling the evaluator for every task.
    A plain ``.replace`` per field leaves all other braces untouched.
    """
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    return out


class EvalVerdict(str, Enum):
    ACCEPT = "accept"
    ENRICH = "enrich"
    CLARIFY = "clarify"
    DECOMPOSE = "decompose"


@dataclass
class EvalResult:
    verdict: EvalVerdict
    dimensions: dict[str, bool] = field(default_factory=dict)
    enriched_criteria: list[str] | None = None
    rationale: str = ""
    # What this evaluation COST. The three utility-tier token figures used to
    # stop at the ``AgentResult`` inside ``evaluate_spec`` — this type had no
    # token fields at all, so the spend was dropped at the return boundary
    # before any ledger could see it. Callers with an attempt row route it
    # through ``usage_sink``; callers without one (the GUI/CLI intake, which
    # runs before a task exists) read it off the result they already hold.
    tokens_used: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "verdict": self.verdict.value,
            "dimensions": self.dimensions,
            "rationale": self.rationale,
        }
        if self.enriched_criteria:
            d["enriched_criteria"] = self.enriched_criteria
        # Only when there is spend to report: a hand-built EvalResult (tests,
        # the board's own annotations) keeps the exact shape it always had.
        if self.tokens_used or self.cache_read_tokens or self.cache_creation_tokens:
            d["usage"] = {
                "tokens_used": self.tokens_used,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_creation_tokens": self.cache_creation_tokens,
            }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvalResult":
        """Rebuild from the ``as_dict`` shape stored on ``context['eval_result']``.

        Lenient in exactly the way ``evaluate_spec`` is: the verdict is
        lower-cased before the enum lookup (models echo the UPPERCASE bullets
        from the prompt), and an unrecognised/absent verdict falls back to
        ACCEPT rather than raising — this runs on an advisory path.
        """
        try:
            verdict = EvalVerdict(str(d.get("verdict", "accept")).strip().lower())
        except ValueError:
            verdict = EvalVerdict.ACCEPT
        dimensions = d.get("dimensions")
        if not isinstance(dimensions, dict):
            dimensions = {}
        enriched_criteria = d.get("enriched_criteria")
        enriched_criteria = list(enriched_criteria) if enriched_criteria else None
        usage = d.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        return cls(
            verdict=verdict,
            dimensions=dimensions,
            enriched_criteria=enriched_criteria,
            rationale=str(d.get("rationale") or ""),
            tokens_used=int(usage.get("tokens_used") or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_tokens") or 0),
        )


_EVAL_JSON = re.compile(r"EVAL_JSON_START\s*(.*?)\s*EVAL_JSON_END", re.DOTALL)

_EVAL_PROMPT = (
    "You are a spec quality evaluator. Assess the task spec below and classify it.\n\n"
    "Quality dimensions (true/false for each):\n"
    "  - clear_objective: Is the goal unambiguous?\n"
    "  - testable_criteria: Are acceptance criteria concrete and verifiable?\n"
    "  - bounded_scope: Is the scope small enough for one agent session?\n"
    "  - no_missing_context: Does the spec provide enough context to start?\n\n"
    "Verdict rules:\n"
    "  - ACCEPT: all dimensions true\n"
    "  - ENRICH: clear_objective=true but testable_criteria=false — auto-generate"
    " stronger criteria\n"
    "  - CLARIFY: clear_objective=false — needs human clarification\n"
    "  - DECOMPOSE: bounded_scope=false — too large\n\n"
    "If verdict is ENRICH, include 'enriched_criteria' with improved acceptance"
    " criteria.\n\n"
    "Output EXACTLY:\n"
    "EVAL_JSON_START\n"
    '{"verdict": "accept|enrich|clarify|decompose",\n'
    ' "dimensions": {"clear_objective": bool, "testable_criteria": bool,\n'
    '               "bounded_scope": bool, "no_missing_context": bool},\n'
    ' "enriched_criteria": ["..."] or null,\n'
    ' "rationale": "one-sentence explanation"}\n'
    "EVAL_JSON_END\n\n"
    "Task:\n"
    "Title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n"
)


def _default_utility_model() -> str:
    """The CONFIGURED utility model, read via ``load_config`` — not a literal
    and not the shipped default. ``DEFAULT_CONFIG`` is only the fallback when
    no config file is reachable or reading it fails for any reason (advisory
    call sites must never raise for this).

    Both entry points below used to hardcode ``claude-opus-4-8``, then later
    a schema-read of ``DEFAULT_CONFIG`` — either way an operator's own
    ``llm.utility_model`` (e.g. changed via ``nh config models set utility``
    or the Settings picker) could never reach them. Callers that hold a
    resolved config should still pass ``model=`` explicitly; this is the
    floor, not the policy.
    """
    from ..config import CONFIG_PATH, DEFAULT_CONFIG, load_config
    try:
        # Pass CONFIG_PATH explicitly — load_config()'s no-arg default binds
        # at module-def time, so a test (or a future caller) that repoints
        # config.CONFIG_PATH would otherwise be silently ignored here.
        return (load_config(CONFIG_PATH).data.get("llm") or {}).get(
            "utility_model") or DEFAULT_CONFIG["llm"]["utility_model"]
    except Exception:
        return DEFAULT_CONFIG["llm"]["utility_model"]


async def evaluate_spec(
    title: str,
    description: str,
    acceptance_criteria: list[str],
    *,
    backend: Any | None = None,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
) -> EvalResult | None:
    """Run the intake quality evaluator. Returns None on failure (advisory)."""
    try:
        import tempfile
        from pathlib import Path
        from ..agent.advisory import advisory_backend
        be = backend or advisory_backend(
            model or _default_utility_model(), role="intake",
        )
        criteria_text = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none)"
        prompt = _render(
            _EVAL_PROMPT,
            title=title,
            description=description or "(none)",
            criteria=criteria_text,
        )
        result = await _bounded_run(be, prompt, max_turns=1, effort="low",
                              cwd=Path(tempfile.gettempdir()),
                              usage_sink=usage_sink)
        text = result.final_text or ""
        m = _EVAL_JSON.search(text)
        if not m:
            log.warning("evaluator produced no parseable EVAL_JSON block")
            return None
        data = loads_lenient(m.group(1))
        tok, cr, cc = usage_of(result)
        # The prompt teaches the verdicts in UPPERCASE bullets and models echo
        # that; the enum values are lowercase — live failure 2026-07-25:
        # "'DECOMPOSE' is not a valid EvalVerdict" silently skipped the eval.
        verdict = EvalVerdict(str(data.get("verdict", "accept")).strip().lower())
        return EvalResult(
            verdict=verdict,
            dimensions=data.get("dimensions", {}),
            enriched_criteria=data.get("enriched_criteria"),
            rationale=data.get("rationale", ""),
            tokens_used=tok,
            cache_read_tokens=cr,
            cache_creation_tokens=cc,
        )
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks
        log.warning("evaluator failed (proceeding without eval): %s", exc)
        return None


_ASSUMPTIONS_JSON = re.compile(
    r"ASSUMPTIONS_JSON_START\s*(.*?)\s*ASSUMPTIONS_JSON_END", re.DOTALL)

_ASSUMPTIONS_PROMPT = (
    "An autonomous coding agent must proceed on this task WITHOUT asking a human "
    "any questions. For each ambiguous or underspecified point, state the single "
    "most reasonable assumption the agent will proceed under. Be concrete, "
    "minimal, and prefer the interpretation a senior engineer would pick. These "
    "assumptions will be surfaced in the pull request for a human to catch at "
    "review time.\n\n"
    "Output EXACTLY:\n"
    "ASSUMPTIONS_JSON_START\n"
    '{"assumptions": ["...", "..."]}\n'
    "ASSUMPTIONS_JSON_END\n\n"
    "Task:\n"
    "Title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n"
)


async def resolve_assumptions(
    title: str,
    description: str,
    acceptance_criteria: list[str],
    *,
    backend: Any | None = None,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
) -> list[str] | None:
    """For an ambiguous spec, return the assumptions the agent will proceed
    under so it never has to stop and ask a human (megaplan P2 / decision #1).
    Returns None on failure (advisory — never blocks the pipeline)."""
    try:
        import tempfile
        from pathlib import Path
        from ..agent.advisory import advisory_backend
        be = backend or advisory_backend(
            model or _default_utility_model(), role="intake",
        )
        criteria_text = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none)"
        prompt = _render(
            _ASSUMPTIONS_PROMPT,
            title=title,
            description=description or "(none)",
            criteria=criteria_text,
        )
        result = await _bounded_run(be, prompt, max_turns=1, effort="low",
                              cwd=Path(tempfile.gettempdir()),
                              usage_sink=usage_sink)
        m = _ASSUMPTIONS_JSON.search(result.final_text or "")
        if not m:
            return None
        data = loads_lenient(m.group(1))
        items = data.get("assumptions")
        if isinstance(items, list) and items:
            return [str(x) for x in items][:10]
        return None
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks
        log.warning("assumption resolution failed (proceeding without): %s", exc)
        return None


# ------------------------- intake grill (every task) ----------------------- #
# Operator directive 2026-07-17: every task goes through an intake grill "like
# with a real user". Questions are generated with an EVPI-style rubric (ask
# only what changes a decision — SAGE-Agent, ACL 2026); when no human is
# present they are answered from repo evidence as documented reversible
# assumptions, and the full Q&A is surfaced at the human gate. Advisory
# end-to-end: any failure returns what it has and never blocks the pipeline.

_GRILL_JSON = re.compile(r"GRILL_JSON_START\s*(.*?)\s*GRILL_JSON_END", re.DOTALL)

_GRILL_QUESTIONS_PROMPT = (
    "You are the intake clarifier for an autonomous coding agent. A real "
    "requester filed this task and has walked away. Generate the clarifying "
    "questions a careful engineer would ask the requester BEFORE planning.\n\n"
    "Rules:\n"
    "- Ask ONLY questions whose answer changes what gets built (target file/"
    "repo, scope, deliverable artifact, acceptance ambiguity). For each "
    "question state the decision its answer changes. Output NO question whose "
    "answer would not change the work.\n"
    "- Tag carve_out: \"access\" for anything needing credentials/permissions/"
    "external accounts, \"destructive\" for anything irreversible (deletes, "
    "rotations, prod mutations), else \"none\". Carve-out questions are for a "
    "human — never answered autonomously.\n"
    "- At most 8 questions; fewer is better. A crisp spec deserves zero.\n\n"
    "Output EXACTLY:\n"
    "GRILL_JSON_START\n"
    '{"questions": [{"question": "...", "decision_it_changes": "...", '
    '"carve_out": "none|access|destructive"}]}\n'
    "GRILL_JSON_END\n\n"
    "Task:\n"
    "Title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n"
)


@dataclass
class GrillQA:
    """One intake question with its (eventual) answer."""

    question: str
    decision_it_changes: str
    answer: str = ""
    source: str = ""  # "human" | "repo-evidence" | "assumption"
    carve_out: str = "none"  # "none" | "access" | "destructive"

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "decision_it_changes": self.decision_it_changes,
            "answer": self.answer,
            "source": self.source,
            "carve_out": self.carve_out,
        }


async def generate_grill_questions(
    title: str,
    description: str,
    acceptance_criteria: list[str],
    *,
    backend: Any | None = None,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
    outcome_sink: OutcomeSink | None = None,
) -> list[GrillQA] | None:
    """Generate the intake grill's questions. None on failure (advisory).

    ``outcome_sink`` is called EXACTLY ONCE per call, with one of
    ``GRILL_QUESTIONS_OUTCOMES`` and a fields dict (``questions``, the number
    produced; ``timed_out``; ``error`` on the raise path). Same shape and same
    never-raises contract as ``grill_spec``'s. Unconditional on purpose: unlike
    the answering pass there is no "did not run" case to keep out of the
    denominator — reaching this function IS the pass running, and its silent
    failure is what left the whole grill emitting zero events.
    """
    outcome, timed_out = "error", False
    try:
        import tempfile
        from pathlib import Path
        from ..agent.advisory import advisory_backend
        be = backend or advisory_backend(
            model or _default_utility_model(), role="intake",
        )
        criteria_text = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none)"
        prompt = _render(
            _GRILL_QUESTIONS_PROMPT,
            title=title,
            description=description or "(none)",
            criteria=criteria_text,
        )
        result = await _bounded_run(be, prompt, max_turns=1, effort="low",
                              cwd=Path(tempfile.gettempdir()),
                              usage_sink=usage_sink)
        timed_out = bool(getattr(result, "timed_out", False))
        data, why = _parse_questions_block(result.final_text or "")
        if data is None:
            # Same silent single-emit failure class #125 fixed for the
            # ANSWERS pass (v10: 6/6 lethal there) — retry ONCE. The retry is
            # a SECOND billed call; _bounded_run books it too.
            #
            # The retry used to trigger on "no block" ONLY. A block that
            # matched but was not JSON, or parsed to a list, skipped it and
            # raised out of the unguarded parse below into the blanket
            # handler — the identical defect fixed for the answers pass on
            # 2026-08-06, still live in this one. Both classes now retry, and
            # the ceiling is unchanged at two calls.
            log.warning("grill produced %s; retrying once",
                        "no GRILL_JSON block" if why == "no_block"
                        else "a GRILL_JSON block that did not parse")
            result = await _bounded_run(be, prompt, max_turns=1, effort="low",
                                  cwd=Path(tempfile.gettempdir()),
                                  usage_sink=usage_sink)
            timed_out = timed_out or bool(getattr(result, "timed_out", False))
            data, why2 = _parse_questions_block(result.final_text or "")
            if data is not None:
                outcome = "parsed_after_retry"
            elif why2 == "unparseable" or why == "unparseable":
                outcome = "unparseable_after_retry"
            else:
                outcome = "no_block_after_retry"
        else:
            outcome = "parsed_first_try"
        if data is None:
            log.warning("grill produced no usable GRILL_JSON block (%s)",
                        outcome)
            _record_outcome(outcome_sink, outcome, questions=0,
                            timed_out=timed_out)
            return None
        items = data.get("questions")
        if not isinstance(items, list) or not items:
            _record_outcome(outcome_sink, "empty_after_parse", questions=0,
                            timed_out=timed_out)
            return None
        out: list[GrillQA] = []
        for item in items[:8]:
            if not isinstance(item, dict) or not item.get("question"):
                continue
            carve = str(item.get("carve_out", "none"))
            out.append(GrillQA(
                question=str(item["question"]),
                decision_it_changes=str(item.get("decision_it_changes", "")),
                carve_out=carve if carve in ("none", "access", "destructive")
                else "none",
            ))
        # A block full of unusable items parsed and produced nothing, exactly
        # like an absent `questions` key — same bucket, or the two shapes of
        # the same failure would land in different halves of the metric.
        _record_outcome(outcome_sink,
                        outcome if out else "empty_after_parse",
                        questions=len(out), timed_out=timed_out)
        return out or None
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks
        log.warning("grill question generation failed (proceeding): %s", exc)
        _record_outcome(outcome_sink, "error", questions=0,
                        timed_out=timed_out, error=type(exc).__name__)
        return None


_GRILL_ANSWERS = re.compile(
    r"GRILL_ANSWERS_START\s*(.*?)\s*GRILL_ANSWERS_END", re.DOTALL)

_GRILL_ANSWERS_PROMPT = (
    "You are answering intake questions for an autonomous coding agent, using "
    "ONLY this repository's contents and the task spec below. The requester is "
    "not available. For each question, give the single most reasonable "
    "REVERSIBLE answer a senior engineer would proceed under, citing repo "
    "evidence as path:line where you found it. If the repo holds no evidence, "
    "answer with the minimal reasonable assumption and source \"assumption\". "
    "Never invent evidence.\n"
    "You have a LIMITED number of turns: explore at most a few files, and "
    "RESERVE YOUR FINAL TURN for the output block. The block is REQUIRED — "
    "partial or assumption answers are fine, but never end without emitting "
    "it.\n"
    "DISCOVERY IS REPO-SCOPED. Your working directory IS the task repository "
    "root: {repo_root}. Find files with `find {repo_root}/ -name '<glob>'` and "
    "search with `grep -r '<pattern>' {repo_root}/` (or the Glob/Grep tools). "
    "NEVER scan the whole filesystem (`find /`, `grep -r <pattern> /`, "
    "`ls -R /`): a pre-execution guard rejects those with 'must be "
    "repo-scoped', and they exhaust your turn budget before you can emit the "
    "block.\n\n"
    "Output EXACTLY:\n"
    "GRILL_ANSWERS_START\n"
    '{"answers": [{"i": 0, "answer": "...", "source": '
    '"repo-evidence|assumption"}]}\n'
    "GRILL_ANSWERS_END\n\n"
    "Task:\n"
    "Title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n\n"
    "Questions:\n{questions}\n"
)


def _parse_answers_block(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split "did the model emit a block?" from "was the block usable?".

    Returns ``(data, why)`` — ``data`` is the parsed object on success, else
    ``None`` with ``why`` in ``{"no_block", "unparseable"}``. The two are
    separate failures with separate fixes, and conflating them is exactly how
    the unparseable case shipped with no recovery: the old code branched on
    ``if not m`` only, so a matched-but-invalid block skipped the retry and
    raised out of ``loads_lenient`` into the advisory handler.

    A block that parses to something that is not a dict counts as unparseable:
    ``data.get("answers")`` on a list would AttributeError one line later, and
    "the model emitted the wrong shape" is the same failure as "the model
    emitted invalid JSON".
    """
    return _parse_grill_block(_GRILL_ANSWERS, text, "answers")


def _parse_questions_block(text: str) -> tuple[dict[str, Any] | None, str]:
    """``_parse_answers_block`` for the QUESTIONS block.

    Its own existence is the fix: this pass used to call ``loads_lenient``
    outside any try and then ``data.get("questions")`` on whatever came back,
    so a matched-but-invalid block and a block that parsed to a list both
    raised into the function's blanket advisory handler — no retry, no event,
    no way to tell either from a backend outage.
    """
    return _parse_grill_block(_GRILL_JSON, text, "questions")


def _parse_grill_block(
    pattern: re.Pattern[str], text: str, what: str,
) -> tuple[dict[str, Any] | None, str]:
    """The shared body of the two above. One implementation, because the two
    passes had the same three failure modes and only one of them was guarded.
    """
    m = pattern.search(text or "")
    if not m:
        return None, "no_block"
    try:
        data = loads_lenient(m.group(1))
    except Exception as exc:  # noqa: BLE001 — a bad block is data, not a crash
        log.warning("grill %s block matched but did not parse: %s", what, exc)
        return None, "unparseable"
    if not isinstance(data, dict):
        log.warning("grill %s block parsed to %s, expected an object",
                    what, type(data).__name__)
        return None, "unparseable"
    return data, ""


async def grill_spec(
    title: str,
    description: str,
    acceptance_criteria: list[str],
    repo_path: Any | None,
    *,
    backend: Any | None = None,
    model: str | None = None,
    questions: list[GrillQA] | None = None,
    usage_sink: UsageSink | None = None,
    outcome_sink: OutcomeSink | None = None,
    questions_outcome_sink: OutcomeSink | None = None,
    probe: bool = True,
) -> list[GrillQA] | None:
    """The full unattended grill: generate questions, answer the answerable
    ones FROM THE REPO (the answering session's cwd is the task's repo — the
    pre-existing resolve_assumptions path is repo-blind), and hard-gate the
    carve-outs for a human. Never raises; a failed answering pass returns the
    questions unanswered rather than fabricating.

    ``outcome_sink`` is called exactly once per answering pass that RUNS, with
    one of ``GRILL_ANSWERING_OUTCOMES`` and a small fields dict
    (``answers_applied``, ``answerable``, ``timed_out``, and ``error`` on the
    error path). A pass that never runs (no questions, or every question
    carved out) reports nothing — an absent pass is not a failed one, and
    folding the two together would poison the denominator.

    ``probe=False`` skips the exploring session entirely and asks for the
    tool-less emit as the PRIMARY (and only) pass — the caller has judged the
    task's questions answerable without reading the repo (the proportionality
    rung for prose-only tasks: the 2026-08-19 funnel forensics measured the
    exploring pass at ~3-4x the whole utility lane's 08-10 baseline, and a
    docs task's questions rarely need filesystem probes). Answers still carry
    ``source: "assumption"`` and the same never-fabricate instruction.

    ``questions_outcome_sink`` is the same for the QUESTIONS pass, and is
    forwarded rather than merged: the two passes fail for different reasons at
    different prices (1 turn vs 8), and a single mixed bucket could not tell
    "we never got questions" from "we got questions and could not answer
    them". It fires whenever that pass runs — i.e. not when ``questions=`` was
    supplied by the caller, because then it did not run."""
    try:
        qs = questions or await generate_grill_questions(
            title, description, acceptance_criteria,
            backend=backend, model=model, usage_sink=usage_sink,
            outcome_sink=questions_outcome_sink,
        )
        if not qs:
            return None
        for q in qs:
            if q.carve_out != "none":
                q.answer = "HUMAN-GATED: not self-answerable"
                q.source = ""
        answerable = [(i, q) for i, q in enumerate(qs) if q.carve_out == "none"]
        if not answerable:
            return qs
        # Bound BEFORE the try — the error path below records it, and every
        # line in there (the imports, the backend ctor, _render, a
        # _bounded_run re-raising a non-timeout) can raise ahead of the call.
        timed_out = False
        try:
            import tempfile
            from pathlib import Path
            from ..agent.claude_backend import ClaudeBackend
            be = backend or ClaudeBackend(
                model=model or _default_utility_model(), readonly=True,
            )
            criteria_text = ("\n".join(f"  - {c}" for c in acceptance_criteria)
                             or "  (none)")
            q_text = "\n".join(
                f"  {i}. {q.question} (decides: {q.decision_it_changes})"
                for i, q in answerable)
            cwd = Path(repo_path) if repo_path else Path(tempfile.gettempdir())
            prompt = _render(
                _GRILL_ANSWERS_PROMPT,
                title=title,
                description=description or "(none)",
                criteria=criteria_text,
                questions=q_text,
                repo_root=str(cwd),
            )
            fallback_used = False
            outcome = "parsed_first_try"
            if probe:
                # The expensive one: it explores a real repo. The turn budget
                # scales with the question count — one answerable question has
                # never needed eight turns of probing, and the v11 failure mode
                # (exhaust ALL turns exploring, return an error result) burns
                # every turn it is given. Ceiling unchanged at 8.
                probe_turns = min(8, 2 + 2 * len(answerable))
                result = await _bounded_run(be, prompt, max_turns=probe_turns,
                                            effort="low",
                                            cwd=cwd, usage_sink=usage_sink)
            else:
                # Proportionality rung: the tool-less emit AS the primary.
                # Same never-fabricate contract as the fallback below —
                # general-knowledge answers marked source "assumption", no
                # invented file citations.
                result = await _bounded_run(
                    be,
                    prompt + "\n\nDo NOT use tools. Emit the GRILL_ANSWERS "
                    "block IMMEDIATELY in your first reply, as STRICT JSON "
                    "with no commentary inside the block; answers from "
                    "general knowledge with source \"assumption\" are fine. "
                    "Do NOT cite file paths or line numbers you have not "
                    "read; plain-language assumptions only.",
                    max_turns=2, effort="low", cwd=cwd, usage_sink=usage_sink)
            timed_out = bool(getattr(result, "timed_out", False))
            data, why = _parse_answers_block(result.final_text or "")
            if data is None:
                # TWO distinct first-pass failures reach here, and until
                # 2026-08-06 only the first had any recovery at all:
                #  * why == "no_block": v11 live root cause — in content-rich
                #    repos the 8-turn session exhausts ALL turns exploring and
                #    the SDK returns an error result. A blind resample fails
                #    deterministically (0/6 recovery in v11), so the retry
                #    changes STRATEGY: no tools, tiny budget, emit-now;
                #    assumption-grade answers beat empty ones.
                #  * why == "unparseable": the regex DID match but the block
                #    was not JSON ("Expecting value: line 1 column 1 (char 0)",
                #    live 2026-08-06). This fell straight through to the outer
                #    handler with ZERO recovery attempts. It gets the same
                #    cheap tool-less re-emit, told what was actually wrong.
                # Bounded: ONE recovery call, whichever branch triggered it —
                # the answering pass still bills at most two sessions, exactly
                # as before.
                log.warning("grill answering %s; retrying tool-less",
                            "emitted no block" if why == "no_block"
                            else "block did not parse")
                # A prompt fragment, not operator-facing text: it is spliced
                # into the fallback prompt below and read only by the model.
                prompt_lead = (
                    "your previous reply's GRILL_ANSWERS block was not "
                    "valid JSON" if why == "unparseable"
                    else "your previous session ran out of turns exploring")
                fallback = (
                    prompt
                    + f"\n\nFINAL ATTEMPT — {prompt_lead}. Do NOT use tools now. "
                    "Emit the "
                    "GRILL_ANSWERS block IMMEDIATELY in your first reply, as "
                    "STRICT JSON with no commentary inside the block; "
                    "answers from general knowledge with source "
                    '"assumption" are fine. Do NOT cite file paths or line '
                    "numbers you have not read in THIS session; "
                    "plain-language assumptions only.")
                result = await _bounded_run(be, fallback, max_turns=2, effort="low",
                                      cwd=cwd, usage_sink=usage_sink)
                fallback_used = True
                timed_out = timed_out or bool(
                    getattr(result, "timed_out", False))
                data, why2 = _parse_answers_block(result.final_text or "")
                if data is not None:
                    outcome = "parsed_after_tool_less_retry"
                elif why2 == "unparseable" or why == "unparseable":
                    outcome = "unparseable_after_retry"
                else:
                    outcome = "no_block_after_retry"
            applied = 0
            if data is not None:
                answerable_idx = {i for i, _ in answerable}
                for item in data.get("answers", []) or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        i = int(item.get("i"))
                    except (TypeError, ValueError):
                        continue
                    # The model may only answer the questions it was asked —
                    # a carve-out index in its output is ignored.
                    if i in answerable_idx and item.get("answer"):
                        qs[i].answer = str(item["answer"])[:400]
                        applied += 1
                        # r1 blocking finding: the tool-less fallback session
                        # has read NOTHING — it must never stamp
                        # "repo-evidence" (false verifiability makes the
                        # human auditor skip exactly the check the PR-body
                        # section exists to trigger). Unchanged by the
                        # unparseable-block recovery above: that recovery IS a
                        # fallback session, so it sets fallback_used too.
                        src = ("assumption" if fallback_used
                               else str(item.get("source", "assumption")))
                        qs[i].source = (src if src in
                                        ("repo-evidence", "assumption")
                                        else "assumption")
            _record_outcome(outcome_sink, outcome, answers_applied=applied,
                            answerable=len(answerable), timed_out=timed_out)
        except Exception as exc:  # noqa: BLE001 — unanswered beats broken
            log.warning("grill answering failed (questions unanswered): %s", exc)
            _record_outcome(outcome_sink, "error", answers_applied=0,
                            answerable=len(answerable),
                            timed_out=timed_out,
                            error=type(exc).__name__)
        return qs
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks
        log.warning("grill failed entirely (proceeding without): %s", exc)
        return None
