"""Structured escalation report (PLAN.md 22.4).

An escalation is *never* "I'm stuck." It is the six-part report a human can act
on in under a minute. We also parse the agent's structured blocker emission out
of its final text (a fenced ``BLOCKER_JSON`` block), failing safe to a
NOVEL_UNKNOWN escalation when nothing parseable is present.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .taxonomy import HARNESS_ONLY_CATEGORIES, Blocker, BlockerCategory, BlockerOption

# The agent is asked to emit its blocker report between these markers.
_BLOCKER_JSON = re.compile(
    r"BLOCKER_JSON_START\s*(.*?)\s*BLOCKER_JSON_END", re.DOTALL
)


# Sources and extensions for code mechanisms
_SOURCE_EXTENSIONS = (
    "py", "js", "ts", "jsx", "tsx", "json", "yaml", "yml", "toml", "rs", "go",
    "c", "cpp", "h", "sh", "sql", "html", "css",
)


def _build_excluded_terms() -> frozenset[str]:
    """Build the set of vocabulary and terms that should not be classified
    as internal code mechanisms (general prose, protocol terms, and no_human taxonomy)."""
    terms: set[str] = {
        # General non-mechanism terms and common protocol/environment words
        "true", "false", "null", "none", "json", "http", "https", "github", "gitlab",
        "pytest", "python", "node", "npm", "api", "ci", "pr", "url", "sdk", "cli",
        "wip", "db", "ui", "id", "os", "rest", "sql", "git", "utf8", "ascii",
        "blocker", "error", "failed", "warning", "info", "debug",
        "timeout", "timed_out", "retry_after", "status_code", "status_codes",
        "error_code", "error_codes", "error_message", "permission_denied",
        "access_denied", "unauthorized", "forbidden", "bad_request", "not_found",
        "rate_limit", "rate_limited", "rate_limits", "max_attempts", "max_park",
    }

    # Derive from BlockerCategory enum and values
    for cat in BlockerCategory:
        terms.add(cat.name.lower())
        terms.add(cat.value.lower())

    # BlockerCategory aliases
    for alias in ("spec_gap", "ambiguity_spec_gap", "invalid", "impossible_invalid", "infra", "rate_limit"):
        terms.add(alias)

    # Derive from Blocker and BlockerOption fields
    for field_name in Blocker.__dataclass_fields__:
        terms.add(field_name.lower())
    for field_name in BlockerOption.__dataclass_fields__:
        terms.add(field_name.lower())

    # Derive from TaskStatus
    try:
        from ..core.task import TaskStatus

        for status in TaskStatus:
            terms.add(status.name.lower())
            terms.add(str(status.value).lower())
    except ImportError:
        pass

    return frozenset(terms)


_EXCLUDED_TERMS = _build_excluded_terms()


def extract_code_mechanisms(text: str) -> list[str]:
    """Extract code-mechanism references (symbols, dotted paths, files, functions)
    from a root-cause hypothesis."""
    if not text:
        return []

    mechanisms: list[str] = []

    # 1. Backticked expressions: `symbol` or `path/file.py` or `Class.method`
    for match in re.findall(r"`([^`]+)`", text):
        cleaned = match.strip()
        if cleaned and cleaned.lower() not in _EXCLUDED_TERMS and cleaned not in mechanisms:
            mechanisms.append(cleaned)

    # 2. File paths with source extensions (e.g. src/no_human/report.py, core/db.py:120)
    ext_pattern = "|".join(_SOURCE_EXTENSIONS)
    file_pattern = rf"\b[\w./\\-]+\.(?:{ext_pattern})\b(?::\d+(?:-\d+)?)?"
    for match in re.findall(file_pattern, text, re.IGNORECASE):
        if match not in mechanisms and match.lower() not in _EXCLUDED_TERMS:
            mechanisms.append(match)

    # 3. Dotted symbols (e.g. ClaudeBackend._options, module.function, Class.attr)
    dotted_pattern = r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
    for match in re.findall(dotted_pattern, text):
        if match not in mechanisms and match.lower() not in _EXCLUDED_TERMS:
            mechanisms.append(match)

    # 4. Explicit function calls (e.g. parse_blocker(), get_config())
    call_pattern = r"\b([A-Za-z_][A-Za-z0-9_]*)\(\)"
    for match in re.findall(call_pattern, text):
        if match not in mechanisms and match.lower() not in _EXCLUDED_TERMS:
            mechanisms.append(match)

    # 5. camelCase identifiers (e.g. bypassPermissions, permissionMode)
    camel_pattern = r"\b[a-z]+[A-Z][a-zA-Z0-9]*\b"
    for match in re.findall(camel_pattern, text):
        if match not in mechanisms and match.lower() not in _EXCLUDED_TERMS:
            mechanisms.append(match)

    # 6. Multi-word PascalCase (e.g. ClaudeBackend, BlockerOption, TaskOutcome)
    pascal_pattern = r"\b[A-Z][a-z0-9]+(?:[A-Z][a-zA-Z0-9]*)+\b"
    for match in re.findall(pascal_pattern, text):
        if match not in mechanisms and match.lower() not in _EXCLUDED_TERMS:
            mechanisms.append(match)

    # 7. Identifiers with underscores (e.g. permission_mode, _options)
    snake_pattern = r"\b_?[a-zA-Z0-9]+(?:_[a-zA-Z0-9]+)+\b"
    for match in re.findall(snake_pattern, text):
        if match not in mechanisms and match.lower() not in _EXCLUDED_TERMS:
            mechanisms.append(match)

    return mechanisms


def _is_single_mechanism_supported(mech: str, corpus_lower: str) -> bool:
    """Check if an individual code mechanism is grounded in the corpus."""
    mech_clean = mech.strip("`()")
    mech_lower = mech_clean.lower()

    if mech_lower in corpus_lower:
        return True

    if "." in mech_clean:
        parts = [p.lower() for p in mech_clean.split(".") if p]
        if parts and all(p in corpus_lower for p in parts):
            return True

    if "/" in mech_clean or "\\" in mech_clean or ":" in mech_clean:
        base_file = mech_clean.split(":")[-2] if ":" in mech_clean else mech_clean
        base_name = base_file.replace("\\", "/").split("/")[-1].lower()
        if base_name and base_name in corpus_lower:
            return True

    return False


def is_code_mechanism_supported(
    mechanisms: list[str],
    evidence: str,
    tried: list[str] | None = None,
    goal: str = "",
) -> bool:
    """Check if all code mechanisms extracted from the hypothesis are supported
    by evidence, tried attempts, or goal context."""
    if not mechanisms:
        return True

    corpus_parts = [evidence or ""]
    if tried:
        corpus_parts.extend(tried)
    if goal:
        corpus_parts.append(goal)
    corpus = "\n".join(corpus_parts)

    if not corpus.strip():
        return False

    corpus_lower = corpus.lower()

    # Security invariant: Every asserted code mechanism must be supported.
    # Adding an incidental grounded identifier must NOT whitelist an otherwise
    # unsupported code-mechanism hypothesis.
    return all(_is_single_mechanism_supported(mech, corpus_lower) for mech in mechanisms)


def parse_blocker(text: str) -> Blocker | None:
    """Extract a Blocker from an agent's final text, or None if absent/malformed.

    Returning None lets the caller decide the fallback (the orchestrator turns it
    into a NOVEL_UNKNOWN escalation), so a missing block never silently passes.
    """
    if not text:
        return None
    match = _BLOCKER_JSON.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    blocker = Blocker.from_dict(data)
    # Trust boundary. The agent proposes answers; it never gets to attach the
    # action that applies one. An agent-authored `set_task_config` would let it
    # raise the size limit that just blocked it — resolving a blocker by
    # weakening the gate.
    blocker.options = [BlockerOption(label=o.label) for o in blocker.options]
    # Same boundary for the category: BUDGET_EXHAUSTED is raised by the
    # HARNESS ledger, never the agent — the harness version carries the true
    # spend and the raise-budget option; an agent-claimed one is a dead-end
    # blocker the human cannot answer (live: a coder near its cap
    # self-declared it, question-less and option-less). What the agent is
    # actually reporting — "this task is bigger than my budget" — IS
    # SCOPE_EXPLOSION, which routes to a human with the scope story intact.
    if blocker.category is BlockerCategory.BUDGET_EXHAUSTED:
        blocker.category = BlockerCategory.SCOPE_EXPLOSION
    # Same boundary: USER_PAUSED is stamped by the harness's own pause writers
    # (`user_pause_blocker`), never something the agent diagnosed. An
    # agent-claimed one is demoted to NOVEL_UNKNOWN — unclassified, not a
    # false "a human paused this" the board would render as if a human had
    # acted.
    if blocker.category in HARNESS_ONLY_CATEGORIES:
        blocker.category = BlockerCategory.NOVEL_UNKNOWN
    # Same boundary, third field. THIS is the one function in the codebase that
    # produces a Blocker whose prose the MODEL wrote, so it is the one place
    # that may set the flag — and it sets it unconditionally, AFTER `from_dict`,
    # so an agent that emits `"reason_is_agent_authored": false` in its own JSON
    # cannot strip the attribution off its own text and have `_abandon_draft_pr`
    # publish it as no_human's own words. The flag only ever moves toward "the
    # agent wrote this" here; nothing downstream may move it back.
    blocker.reason_is_agent_authored = True

    # Trust boundary: Code-mechanism hypotheses require supporting evidence (Issue #223).
    # If the hypothesis claims a specific code mechanism (symbols, files, methods)
    # but provides no supporting evidence in `evidence` or `tried`,
    # do not trust the agent's self-reported high confidence. Demote confidence
    # below the escalation threshold (0.6) so it is treated as an unverified
    # hypothesis rather than a high-confidence root cause.
    if blocker.confidence >= 0.6:
        mechs = extract_code_mechanisms(blocker.root_cause_hypothesis)
        if mechs and not is_code_mechanism_supported(
            mechs, blocker.evidence, blocker.tried, blocker.goal
        ):
            blocker.confidence = 0.5

    return blocker


def render_report(blocker: Blocker, *, task_title: str, task_id: str) -> str:
    """Render the 22.4 six-part escalation report as markdown for the board /
    notification. Order and headings are fixed so a human scans it fast."""
    b = blocker
    lines = [
        f"# Blocker — {task_title}  ({task_id[:8]})",
        f"**Category:** {b.category.value}   "
        f"**Confidence:** {b.confidence:.0%}   "
        f"**Transient:** {'yes' if b.transient else 'no'}",
        "",
        "## 1. Goal",
        b.goal or "(not stated)",
        "",
        "## 2. What happened (evidence)",
        b.evidence.strip() or "(no evidence captured)",
        "",
        "## 3. Why blocked",
        b.root_cause_hypothesis or "(no hypothesis)",
        "",
        "## 4. What I tried",
    ]
    if b.tried:
        lines += [f"- {t}" for t in b.tried]
    else:
        lines.append("- (no alternatives attempted)")
    lines += ["", "## 5. What I need from you"]
    if b.question:
        lines.append(b.question)
        if b.options:
            lines += [f"  - [{i+1}] {opt.label}" for i, opt in enumerate(b.options)]
    else:
        lines.append("(no specific question — review the diagnosis above)")
    lines += [
        "",
        "## 6. State & resume",
        f"Branch: `{b.resume_branch or '(none)'}`  "
        f"Commit: `{(b.resume_commit or '')[:8] or '(none)'}` "
        f"[WIP-BLOCKED]",
    ]
    if b.wake_condition:
        lines.append(f"Wake condition: `{b.wake_condition}`")
    lines.append(f"Resume with: `nh reply {task_id[:8]} \"<answer>\"`")
    return "\n".join(lines)


def notification_line(blocker: Blocker, *, task_title: str, task_id: str) -> str:
    """One-line summary for Slack/email — the headline, not the full report."""
    ask = blocker.question or blocker.root_cause_hypothesis or "needs review"
    return (
        f"{task_title} [{task_id[:8]}] {blocker.category.value}: {ask}"
        f"  →  nh blocked  /  nh reply {task_id[:8]} \"...\""
    )


def fallback_blocker(detail: str, *, resume_branch: str = "",
                     resume_commit: str = "", goal: str = "") -> Blocker:
    """Construct a NOVEL_UNKNOWN blocker when the agent gave no structured report.

    Used for deterministic orchestrator-side failures (push failed, PR error,
    size limit) and for an agent that returned no parseable block."""
    return Blocker(
        category=BlockerCategory.NOVEL_UNKNOWN,
        transient=False,
        confidence=0.0,
        root_cause_hypothesis=detail,
        goal=goal,
        evidence=detail,
        question="Review the blocker and advise how to proceed.",
        resume_branch=resume_branch,
        resume_commit=resume_commit,
    )


def missing_access(
    env_key: str,
    *,
    system: str = "",
    goal: str = "",
    evidence: str = "",
    resume_branch: str = "",
    resume_commit: str = "",
) -> Blocker:
    """Build a MISSING_ACCESS blocker that names the EXACT ``~/.no_human/.env``
    key a human must set (WS-F). The point of the precision: a human resolves it
    in under a minute — set one named key, then ``nh reply`` — without guessing
    which credential is missing. Never include the value, only the key name."""
    sys_label = f" for {system}" if system else ""
    return Blocker(
        category=BlockerCategory.MISSING_ACCESS,
        transient=False,
        confidence=0.95,
        root_cause_hypothesis=(
            f"Missing credential{sys_label}: {env_key} is not set. This is an "
            "access wall, not a code failure — retrying cannot resolve it."
        ),
        goal=goal,
        evidence=evidence or f"{env_key} not found in ~/.no_human/.env or the environment",
        question=(
            f"Set {env_key} in ~/.no_human/.env (chmod 600){sys_label}, then "
            f"`nh reply` to resume. no_human never reads or logs the value."
        ),
        resume_branch=resume_branch,
        resume_commit=resume_commit,
    )


def ci_misconfigured(reason: str, *, goal: str = "") -> Blocker:
    """Build the blocker for "this run asked for a CI gate and cannot have one".

    IMPOSSIBLE, not MISSING_ACCESS: nothing is being withheld, the request
    itself cannot be satisfied as written (the taxonomy's own alias for the
    category is ``INVALID``). It routes to ESCALATED + notify — the run stops
    and a human hears about it — which is the point. The alternative, which is
    what shipped for a long time, is a PR opened with no gate while the user
    believes the advertised gate ran.

    Confidence is high on purpose: this is a static read of the config, not a
    hypothesis, so ``triage``'s low-confidence override has nothing to do.

    *reason* comes from the resolver and already names the origin (project
    profile / global config) and the exact key — do not paraphrase it here.
    """
    return Blocker(
        category=BlockerCategory.IMPOSSIBLE,
        transient=False,
        confidence=0.95,
        goal=goal,
        root_cause_hypothesis=(
            "This repo's configuration asks for a CI gate, but no CI backend "
            "can be built from it, so the task can only be finished UNGATED. "
            "Retrying cannot resolve it — the config has to change."
        ),
        evidence=reason,
        question=(
            "Fix the CI config and re-run, or set `ci.enabled: false` to accept "
            "running on the local test suite alone. no_human will not open a PR "
            "claiming a gate that did not run."
        ),
        options=[
            BlockerOption(label="I'll fix the CI config, then re-run"),
            BlockerOption(label="Proceed without CI — set ci.enabled: false"),
        ],
    )


def blocker_prompt_suffix() -> str:
    """Instruction appended to the agent prompt so it can self-report a blocker.

    The agent emits this ONLY when it genuinely cannot proceed without lowering
    the bar — never as a way to avoid finishing a doable task."""
    cats = ", ".join(
        c.value for c in BlockerCategory if c not in HARNESS_ONLY_CATEGORIES
    )
    return (
        "\n\nIf — and only if — you cannot make verifiable progress without "
        "weakening a test, expanding scope, editing the acceptance criteria, or "
        "faking completion, STOP and emit a structured blocker report. Do not "
        "lower the bar to force a pass. Emit it between markers exactly:\n"
        "BLOCKER_JSON_START\n"
        "{\n"
        f'  "category": one of [{cats}],\n'
        '  "transient": true|false,\n'
        '  "wake_condition": "machine-checkable, e.g. \'PR org/repo#12 merged\', '
        '\'quota_refreshed\', \'after:2h\', or null",\n'
        '  "root_cause_hypothesis": "...",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "tried": ["alternative 1 + result", "alternative 2 + result"],\n'
        '  "question": "the ONE decision/info you need from a human, or null",\n'
        '  "options": ["candidate answer 1", "candidate answer 2"],\n'
        '  "goal": "the step you were attempting",\n'
        '  "evidence": "the exact command + output that shows the block"\n'
        "}\n"
        "BLOCKER_JSON_END\n"
        "Confidence below 0.6 means you are unsure what is wrong — that is fine, "
        "report it and a human will help; do not thrash."
    )
