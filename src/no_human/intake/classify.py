"""Task-type classification (WS-A).

One front door, many task shapes. ``classify_kind`` reads the signals already on
a normalized :class:`Task` — its source, title, description, acceptance criteria
— and tags it with a :class:`TaskKind` that selects the pipeline. The classifier
is **deterministic first** (cheap, hermetic, testable against the four reference
shapes) with an optional Agent-SDK fallback for genuinely ambiguous freeform
text.

This is a *routing* decision, not a quality gate: it emits cited evidence (which
signal fired), never a numeric confidence-score gate (that ban, constraint §3.3,
is about the review gate). When nothing matches we default to ``feature`` — the
generic implement→review→test pipeline — which is always a safe route.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class TaskKind(str, Enum):
    """The task shapes WS-A routes. Value is the stable string stored on a task."""

    FEATURE = "feature"            # implement new behaviour
    BUGFIX = "bugfix"              # fix a defect / regression
    CI_FIX = "ci_fix"             # make a red remote build green
    TRACEABILITY = "traceability"  # link/author an automated test case
    TEST_GAP = "test_gap"         # add missing test coverage
    INVESTIGATION = "investigation"  # deep debugging / root-cause analysis (wider bounds)
    CODE_REVIEW = "code_review"      # review someone else's PR (read-only)
    DESIGN_DOC = "design_doc"        # architecture/design DOCUMENT (report, not code)

    @classmethod
    def coerce(cls, value: str | "TaskKind | None") -> "TaskKind":
        """Map a raw string to a kind, defaulting to FEATURE for anything
        unrecognized (the safe generic pipeline)."""
        if isinstance(value, cls):
            return value
        key = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "bug": cls.BUGFIX, "defect": cls.BUGFIX,
            "ci": cls.CI_FIX, "build_fix": cls.CI_FIX, "red_build": cls.CI_FIX,
            "atc": cls.TRACEABILITY, "test_automation": cls.TRACEABILITY,
            "tests": cls.TEST_GAP, "test": cls.TEST_GAP, "coverage": cls.TEST_GAP,
            "investigate": cls.INVESTIGATION, "debug": cls.INVESTIGATION,
            "root_cause": cls.INVESTIGATION, "rca": cls.INVESTIGATION,
            "code_review": cls.CODE_REVIEW, "review": cls.CODE_REVIEW,
            "pr_review": cls.CODE_REVIEW, "cr": cls.CODE_REVIEW,
            "design": cls.DESIGN_DOC, "architecture": cls.DESIGN_DOC,
            "rfc": cls.DESIGN_DOC, "adr": cls.DESIGN_DOC,
            "design_document": cls.DESIGN_DOC,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError:
            return cls.FEATURE


@dataclass
class KindVerdict:
    kind: TaskKind
    reason: str          # cited signal, e.g. "title matches /atc|traceability/"
    source: str          # "override" | "rule" | "source" | "agent" | "default"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "reason": self.reason, "source": self.source}


# --------------------------------------------------------------------------- #
# Kind/criteria consistency guard (defect 204f2177)                           #
# --------------------------------------------------------------------------- #
#
# A report-only kind (design_doc/investigation) completes without ever
# producing code, a test, or a shipped artifact — that IS its success shape
# (see core/orchestrator.py's report-only DONE path). So when acceptance
# criteria demand exactly those things — a test, red-first proof, a CLI flag,
# an endpoint, something that "lands" or "ships" — a report-only kind can
# never satisfy them: it silently completes "report-only" while the demanded
# artifact never ships. Live case: task 204f2177 asked for `nh approve
# --landed` with red-first + control tests, was classified design_doc, and
# was marked DONE on a report with no code change — nothing flagged the
# mismatch. This guard names that mismatch so both intake (never accept it)
# and completion (never silently finish it) can refuse instead.
REPORT_ONLY_KINDS: frozenset[str] = frozenset(
    {TaskKind.DESIGN_DOC.value, TaskKind.INVESTIGATION.value})

# Deliberately the literal signal words the spec calls out, not a broader
# semantic guess — a false negative here is a silent miss (bad), but a false
# positive just makes intake ask a human to confirm the kind (cheap).
_TEST_BEARING_WORDS: tuple[str, ...] = (
    "test", "red-first", "test-bearing", "lands", "cli", "endpoint",
    "shipped", "artifact",
)
_TEST_BEARING_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _TEST_BEARING_WORDS) + r")\b", re.I)


def find_test_bearing_signal(acceptance_criteria: list[str] | None) -> str | None:
    """Return the first test-bearing/shipped-artifact keyword found in
    ``acceptance_criteria`` (e.g. ``"test"``, ``"CLI"``, ``"lands"``), or
    ``None`` if none of the signal words appear."""
    text = " ".join(acceptance_criteria or [])
    m = _TEST_BEARING_PATTERN.search(text)
    return m.group(0) if m else None


def kind_criteria_mismatch(
    kind: "TaskKind | str | None", acceptance_criteria: list[str] | None,
) -> str | None:
    """Return a reason string if ``kind`` is report-only (design_doc /
    investigation) but ``acceptance_criteria`` demand a tested/shipped
    artifact — the defect-204f2177 shape. ``None`` when the pairing is
    consistent (report-only kind with prose-only criteria, or any other
    kind).

    Callers use this at TWO points, per the bug: at intake (before a task is
    even created — refuse rather than silently apply the mismatched kind) and
    at completion (before a report-only completion is applied — refuse rather
    than silently mark DONE)."""
    kind_value = kind.value if isinstance(kind, TaskKind) else str(kind or "")
    if kind_value not in REPORT_ONLY_KINDS:
        return None
    hit = find_test_bearing_signal(acceptance_criteria)
    if hit is None:
        return None
    return (
        f"kind={kind_value!r} is report-only (produces a report, never code) "
        f"but acceptance criteria contain the test-bearing signal {hit!r} — "
        "a report-only completion would never ship the demanded artifact"
    )


# Ordered most-specific-first. The first pattern that matches wins, so a task
# that says "fix the failing CI build" routes to ci_fix, not bugfix. Each entry
# is (kind, compiled pattern, human label for the evidence string).
_RULES: list[tuple[TaskKind, re.Pattern[str], str]] = [
    # A design/architecture DOCUMENT request is a read-only deliverable —
    # routing it to feature returns code where the user asked for a document.
    # FIRST in the list: "Write a design doc for the failing CI pipeline" is a
    # document about CI, not a ci_fix — the explicit document noun is the most
    # specific intent signal. Precision-first: only document nouns match
    # ("design doc", "architecture proposal", "an RFC", "RFC: <topic>");
    # the verbs ("design the API", "redesign the UI") remain implement work.
    (TaskKind.DESIGN_DOC, re.compile(
        r"\b(design|architecture)\s+(doc|document|proposal|spec|review\s+doc)\b"
        r"|\ban?\s+(rfc|adr)\b"
        r"|^\s*(rfc|adr)\b", re.I),  # a title LEADING with RFC/ADR is a doc ask
        "design_doc (document request — read-only, produces a cited doc)"),
    # A plan-DOCUMENT request ("Create a plan to onboard…") is a document ask,
    # same deliverable class as design_doc. Precision: the plan noun must be the
    # terminal ask (followed by to/for/:/end) — "create a plan and implement it"
    # and "create a plan section" both fail the lookahead and stay implement work.
    # The trailing lookahead keeps "write a plan to fix X and then implement
    # it" in the implement pipeline — the plan-noun lookahead alone can't see
    # past "to" (review D5).
    (TaskKind.DESIGN_DOC, re.compile(
        r"\b(create|write|draft|make|produce)\s+(a\s+|the\s+)?(\w+\s+){0,2}plan\b"
        r"(?=\s*(to\b|for\b|:|\.|$))"
        r"(?!.{0,80}\b(and|then)\s+(then\s+)?(implement|execute|apply|build|code|ship)\b)",
        re.I),
        "design_doc (plan-document request — read-only, produces a plan)"),
    # An explicit review ASK is a read-only findings deliverable — the feature
    # pipeline either parks asking "is my review the deliverable?" or grinds at
    # implementing (v6 taxonomy, 2026-07-16). TWO destinations, because the
    # code_review pipeline hard-fails without a PR/MR reference
    # (_run_code_review → parse_pr_refs → _fail):
    #   1. a review ask that NAMES a PR/MR (URL path or "PR #123"-style token)
    #      → CODE_REVIEW (the fetch-diff-and-post-comments pipeline);
    #   2. a bare review ask ("in-depth code review of this service")
    #      → INVESTIGATION below — a cited findings report, which degrades
    #      gracefully in both directions (the agent can still fetch a ref it
    #      finds; a repo-level review never dies on a missing ref).
    # Precision: review+implement ("review and fix", "apply the code review
    # feedback") is implement work, blocked by the trailing lookahead.
    # No re.S: `.` must NOT cross the \n between the title and the criteria in
    # `_intent_text` — with it, "…PR #4821\n<criterion mentioning review>"
    # welded two zones into one false match (review D1). Proximity is a
    # same-line concept. `merge` is deliberately NOT in the block-list:
    # "review this PR before I merge it" is a pure review ask (the agent never
    # merges anyway); resolving/fixing findings is what makes it implement
    # work (review D7). classify_kind additionally downgrades a CODE_REVIEW
    # match to INVESTIGATION when parse_pr_refs finds no usable ref — the
    # pipeline hard-fails without one (review D2).
    (TaskKind.CODE_REVIEW, re.compile(
        r"(\breview\s+(of\s+)?(this|the|that)?\s*(pr|mr|pull\s+request|merge\s+request)\b"
        r"|(?:/pull/|/merge_requests/|\b(?:pr|mr)\s*[#!]?\d+).{0,160}?\breview\b"
        r"|\breview\b.{0,160}?(?:/pull/|/merge_requests/))"
        r"(?!(\s+(feedback|comments?)\b"
        r"|.{0,80}\b(and\s+|then\s+)?(fix|address|apply|implement|resolve)\b))",
        re.I),
        "code_review (PR/MR review — read-only, posts findings on the ref)"),
    (TaskKind.INVESTIGATION, re.compile(
        r"(\b(code|peer|in-?depth)\s+review\b"
        r"|\b(do|write|output|post|produce|give)\s+(an?\s+)?"
        r"(in-?depth\s+|code\s+|thorough\s+)*review\b)"
        r"(?!(\s+(feedback|comments?)\b"
        r"|.{0,80}\b(and\s+|then\s+)?(fix|address|apply|implement|resolve|merge)\b))",
        re.I),
        "investigation (repo-level review ask — read-only, produces a cited report)"),
    (TaskKind.TRACEABILITY, re.compile(
        # The tracker-jargon alternatives are deliberately PREFIX/SUFFIX-
        # agnostic (`\w*`): trackers name these columns with their own custom
        # prefixes, and hard-coding one vendor's spelling both narrowed the
        # routing and put that vendor's naming convention in shipped source.
        r"\b(atcs?|traceabilit|\w*test[_\s]automation\w*|"
        r"\w*automated[_\s]?test[_\s]?count|"
        r"link\s+(the\s+)?test|no\s+test\s+automation)\b", re.I), "traceability"),
    (TaskKind.CI_FIX, re.compile(
        r"\b(ci\s+is\s+red|red\s+build|failing\s+(ci|build|pipeline)|"
        r"build\s+is\s+(red|fail|broken)|fix\s+(the\s+)?(ci|build|pipeline)|"
        r"pipeline\s+fail|broken\s+build|jenkins\s+(build\s+)?fail|"
        r"monorepo\s+build)\b", re.I), "ci_fix"),
    (TaskKind.TEST_GAP, re.compile(
        r"\b(add\s+(a\s+|unit\s+|integration\s+|missing\s+)?tests?|"
        r"missing\s+tests?|test\s+gap|add\s+(test\s+)?coverage|"
        r"write\s+(a\s+)?tests?|no\s+(unit\s+)?tests?\b|untested|"
        r"increase\s+coverage)\b", re.I), "test_gap"),
    # A "verify / validate / audit that X does Y" task is READ-ONLY analysis that
    # produces a report — routing it to feature (implement) is how a validation
    # request gets mis-built as a code change (the CI_GATE-teardown mis-scope
    # seen live). High precision: a LEADING verify/validate/audit ("Verify the
    # namespaces are torn down") or the clausal form ("validate that/whether/every
    # …"), so the feature-NOUNS "email verification", "data validation", "audit
    # log", "validate the input" do NOT match.
    (TaskKind.INVESTIGATION, re.compile(
        # clausal: "validate that/whether/every …"
        r"\b(verify|validate|audit)\s+(that|whether|if|every|all|each)\b"
        # or leading verb + a CLAIM being checked ("Verify … namespaces ARE torn
        # down") — but NOT verb+object features ("validate the input").
        r"|^\s*(verify|validate|audit)\b[^.]*?\b(is|are|was|were|gets?|should|"
        r"remain\w*|match\w*|exist\w*|correct|present|destroyed|deleted|removed|"
        r"cleaned|torn)\b", re.I),
        "investigation (verify/validate intent — read-only, produces a report)"),
    # A QUESTION is an answer deliverable, not a code change: a leading
    # interrogative ("what are the allowed columns…") or an explicit ask-to-
    # find-out clause ("check when does…", "figure out why…"). Conditional
    # specs ("When the user clicks save, persist…") have neither a leading
    # what/why/which nor a find-out verb, so they stay feature.
    # The trailing lookahead blocks diagnose-AND-FIX asks ("figure out why X
    # 500s and fix it") — those are implement work whose success terminal must
    # not be a zero-change report (review D3). The leading-word guard excludes
    # punctuation AND apostrophe/hyphen: "What's New panel:…" and "Why-first
    # refactor…" are features, not questions (review D6).
    (TaskKind.INVESTIGATION, re.compile(
        r"(?:(?:^\s*(what|why|which)\b(?!\s*[?!.,'’-])"
        r"|\b(check|figure\s+out|find\s+out|explain|tell\s+me|understand)\s+"
        r"(why|what|when|how|where|whether|if)\b"
        r"|\bwhat\s+(should|can|do)\s+(i|we)\b"
        r"|^\s*how\s+(do|does|did|come)\b)"
        # The and/then guard blocks diagnose-AND-implement asks. It stays
        # conjunction-only here: after a bare comma/dash these verbs are as
        # often NOUNS ("update seems stuck", "patch notes are unclear") — a
        # splice guard on every question form flipped 8 master-correct
        # questions to feature (PR #102 re-review, HIGH).
        r"(?!.{0,80}\b(and|then)\s+(fix|address|apply|implement|resolve|update"
        r"|change|patch|remove|add|invalidate|correct|delete|rewrite)\b)"
        # Preposition-fronted questions ("in what envs DOES…") — tightened per
        # review (PR #102): no from/of (discourse openers: "From what I can
        # tell…" flipped a bugfix), an auxiliary-verb continuation is required
        # ("In which module add X" is implement work), and ONLY this
        # alternation also blocks comma/dash-SPLICED imperatives ("For which
        # users should we enable the flag, implement accordingly") — the
        # deliberate precision trade is confined to the new entry points.
        r"|^\s*(in|on|for|at|under)\s+(what|which)\b(?!\s*[?!.,'’-])"
        r"(?=[^,;\u2014\u2013]{0,60}\b(do|does|did|is|are|was|were|should"
        r"|can|could|will|would|has|have)\b)"
        r"(?!.{0,80}(?:\b(and|then)\s+|[,;\u2014\u2013]\s*|\s-\s)"
        r"(fix|address|apply|implement|resolve|update"
        r"|change|patch|remove|add|invalidate|correct|delete|rewrite"
        r"|refactor|enable|disable)\b))", re.I),
        "investigation (question — read-only, produces an answer)"),
    # An ask that EXPLICITLY declares itself read-only ("Do NOT make any
    # changes. Read-only.") is a report deliverable by the requester's own
    # words. The (?!\s+to\b) guard keeps scoped constraints ("do not make
    # changes to the tests") from stealing implement work; "make X read-only"
    # never matches (the phrase must stand alone between punctuation).
    # The scope-guard covers every scoping preposition, not just "to": "do not
    # make changes UNTIL you reproduce" / "no code changes OUTSIDE the deploy
    # scripts" are process constraints on implement work, not read-only
    # declarations (review D4).
    (TaskKind.INVESTIGATION, re.compile(
        r"\b(do\s+not|don'?t)\s+(make|commit|push)\s+(any\s+)?(code\s+)?"
        r"chang(es?|ing)\b(?!\s+(to|until|before|unless|outside|except|beyond|in)\b)"
        r"|\bno\s+code\s+changes\b(?!\s+(to|until|before|unless|outside|except|beyond|in)\b)"
        r"|(?:^|[.;:!]\s*)read-?only\s*(?:[.;!]|$)", re.I),
        "investigation (explicitly read-only ask — produces a report)"),
    (TaskKind.BUGFIX, re.compile(
        r"\b(bug|defect|regression|crash|broken|stack\s?trace|"
        r"throws?\b|exception|incorrect|wrong\s+(value|result|behaviou?r)|"
        r"does\s?n.t\s+work|fix\b)\b", re.I), "bugfix"),
]

# The bugfix rule above is the least specific: bare nouns like "bug", "defect"
# and "fix" carry intent in a *title* but appear incidentally in prose — a
# feature spec routinely cites prior art ("copied the repo's own listing bug")
# or negates an example ("post an error, not a raw stack trace"). Both of those
# misrouted a greenfield feature to the bugfix pipeline, whose directive
# ("reproduce the defect with a failing test first") is incoherent for it.
#
# So the description is scanned with only the phrases that cannot plausibly be
# incidental. Note this is deliberately *not* negation-detection: "bug (defect 1
# above)" is not negated, and a naive negation window would also swallow the real
# signal in "this is not working, fix the crash".
_BUGFIX_IN_PROSE = re.compile(
    r"\b(regression|crash(es|ed|ing)?|"
    r"throws?\s+(an?\s+)?\w*(exception|error)|"
    r"wrong\s+(value|result|behaviou?r)|incorrect|"
    r"does\s?n.t\s+work)\b", re.I)

# The report-shaped intent rules read the ASK, not the discussion: a question,
# review mention, or plan phrase buried in a long feature description must not
# steal the task from the implement pipeline. Same precision posture as the
# prose bugfix swap — these three simply do not scan prose at all.
_INTENT_ONLY_LABELS = {
    "design_doc (plan-document request — read-only, produces a plan)",
    "code_review (PR/MR review — read-only, posts findings on the ref)",
    "investigation (repo-level review ask — read-only, produces a cited report)",
    "investigation (question — read-only, produces an answer)",
}

# Same rules, same order — but the broad bugfix pattern is swapped for the
# high-precision one when scanning free prose, and intent-only rules are
# dropped entirely.
_PROSE_RULES: list[tuple[TaskKind, re.Pattern[str], str]] = [
    (kind, _BUGFIX_IN_PROSE if kind is TaskKind.BUGFIX else pattern, label)
    for kind, pattern, label in _RULES
    if label not in _INTENT_ONLY_LABELS
]


# --------------------------------------------------------------------------- #
# Position-aware masking: a quoted/reproduced symptom is not a statement of   #
# intent (defect: issue #428, "no tests ran" quoted from a pytest failure    #
# message got read as a test_gap signal).                                    #
# --------------------------------------------------------------------------- #
#
# Issues about testing infrastructure quote test-runner output; issues about
# missing coverage say "no tests" in their own voice. The two are hard to
# tell apart by keyword and trivial to tell apart by position: one sits
# inside a quotation, a code span, or a fenced block — text the author is
# reproducing, not asserting — the other is the author's own prose. This
# masker blanks the former (replacing every character with a space, except
# newlines, so offsets never shift and nothing downstream needs to change)
# before the kind rules ever see the text.
#
# Deliberately NOT masked (rare enough in issue prose that guessing wrong
# costs more than leaving them alone): 4-space indented code blocks (too
# easily confused with an ordinary list continuation), HTML blocks, and
# unterminated inline delimiters (a lone stray backtick masks nothing).
_FENCE_BLOCK = re.compile(
    r"^( {0,3})(`{3,}|~{3,})[^\n]*\n"   # opening fence line
    r"(?:.*\n)*?"                        # body, as few lines as possible…
    r"(?:\1\2[`~]*[ \t]*(?:\n|\Z)|\Z)",  # …until a matching closer, or EOF
    re.M)
_BLOCKQUOTE_LINE = re.compile(r"^ {0,3}>.*$", re.M)
# No re.S: an inline code span must not cross a newline. Without this, a
# single stray (unterminated) backtick anywhere in the body pairs with the
# NEXT real backtick — possibly lines later — and blanks everything between
# them, including real prose. Confining `.` to one line means an unterminated
# backtick simply fails to match anything, which is the documented intent
# below ("a lone stray backtick masks nothing").
_INLINE_CODE_SPAN = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
# Pairing rule: straight double quotes get the SAME word-boundary guard as
# _SINGLE_QUOTED below, for the same reason — a stray straight quote used as
# an inch/foot mark (24", 30') or a prime symbol sits directly against a
# digit, so a quote adjacent to alnum text can never be the start/end of a
# real quotation. Curly quotes (“ ”) are directional and unambiguous already
# — “ can only open and ” can only close — so they need no such guard.
_DOUBLE_QUOTED = re.compile(
    r'(?<![A-Za-z0-9])"[^"\n]{1,200}"(?![A-Za-z0-9])'
    r"|“[^”\n]{1,200}”")
# Word-boundary-guarded so real prose survives an apostrophe: without the
# guards, "pytest's runner has no tests ... instead of pytest'" would mask
# everything between the two apostrophes as one giant "quoted string".
_SINGLE_QUOTED = re.compile(r"(?<![A-Za-z0-9])'[^'\n]{1,200}'(?![A-Za-z0-9])")


def _blank(m: "re.Match[str]") -> str:
    """Replace every character of a match with a space, keeping ``\\n`` —
    preserves character offsets so nothing downstream has to shift."""
    return "".join(ch if ch == "\n" else " " for ch in m.group(0))


def mask_quoted_spans(text: str) -> str:
    """Blank out (space-fill, offsets preserved) fenced code blocks,
    blockquote lines, inline code spans, and quoted strings — constructs
    that *reproduce* text rather than assert it. Each pass runs on the
    already-masked string, most structural first, so a signal word that only
    ever appears inside one of these constructs cannot fire a kind rule,
    while the same word in the author's own prose still can."""
    masked = _FENCE_BLOCK.sub(_blank, text)
    masked = _BLOCKQUOTE_LINE.sub(_blank, masked)
    masked = _INLINE_CODE_SPAN.sub(_blank, masked)
    masked = _DOUBLE_QUOTED.sub(_blank, masked)
    masked = _SINGLE_QUOTED.sub(_blank, masked)
    return masked


def _intent_text(task: Any) -> str:
    """What the author asked for: title and the structured, authored fields.

    Each field is masked (see :func:`mask_quoted_spans`) *before* being
    joined into the others, so an unterminated fence opener in one
    acceptance criterion cannot swallow the next criterion too.
    """
    parts = [
        mask_quoted_spans(getattr(task, "title", "") or ""),
        " ".join(
            mask_quoted_spans(c) for c in (getattr(task, "acceptance_criteria", []) or [])),
        " ".join(
            mask_quoted_spans(r) for r in (getattr(task, "requirements", []) or [])),
    ]
    return "\n".join(parts)


def _prose_text(task: Any) -> str:
    """Free-form discussion: background, prior art, worked examples."""
    return mask_quoted_spans(getattr(task, "description", "") or "")


def _guard_code_review(task: Any, verdict: KindVerdict) -> KindVerdict:
    """Downgrade a CODE_REVIEW rule match to INVESTIGATION when the pipeline's
    own ref parser finds nothing to review (review D2): `_run_code_review`
    hard-fails without a parseable PR/MR ref, while an investigation report
    degrades gracefully ("PR 7002" with no host, "the merge request !45").
    Uses parse_pr_refs — the SAME parser the pipeline uses — so the classifier
    and the pipeline can never drift apart on what counts as a ref."""
    if verdict.kind is not TaskKind.CODE_REVIEW:
        return verdict
    from ..vcs.pr_refs import parse_pr_refs
    text = f"{getattr(task, 'title', '') or ''} {getattr(task, 'description', '') or ''}"
    if parse_pr_refs(text):
        return verdict
    return KindVerdict(
        TaskKind.INVESTIGATION,
        f"review ask without a parseable PR/MR ref ({verdict.reason}) — "
        "routed to a cited report instead of the ref-requiring review pipeline",
        "rule")


def classify_kind(task: Any, *, override: str | None = None) -> KindVerdict:
    """Deterministically classify a task into a :class:`TaskKind`.

    Precedence: explicit ``override`` → rules over the *intent* zone (title and
    the authored fields, most-specific first) → rules over the *prose* zone (the
    description, where the broad bugfix pattern is not trusted) → ``feature``.

    Intent outranks prose because a keyword buried in a 9KB description is not a
    statement of what to do. When prose is genuinely all we have and it is
    ambiguous, that is what :func:`classify`'s Agent-SDK fallback is for.
    """
    if override:
        return KindVerdict(TaskKind.coerce(override), "human override", "override")

    intent = _intent_text(task)
    for kind, pattern, label in _RULES:
        m = pattern.search(intent)
        if m:
            return _guard_code_review(
                task,
                KindVerdict(
                    kind,
                    f"matched {label} signal in title/criteria: {m.group(0)!r}",
                    "rule"))

    prose = _prose_text(task)
    for kind, pattern, label in _PROSE_RULES:
        m = pattern.search(prose)
        if m:
            return KindVerdict(
                kind, f"matched {label} signal in description: {m.group(0)!r}", "rule")

    # No specific signal: default to the generic feature pipeline.
    return KindVerdict(TaskKind.FEATURE, "no specific signal; generic pipeline", "default")


# --------------------------------------------------------------------------- #
# Agent-SDK fallback (for ambiguous freeform)                                  #
# --------------------------------------------------------------------------- #

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_AGENT_PROMPT = (
    "Classify this software task into exactly one kind. Do NOT modify anything; "
    "this is a classification only.\n\n"
    "Kinds:\n"
    "  - feature: implement new behaviour\n"
    "  - bugfix: fix a defect or regression in existing behaviour\n"
    "  - ci_fix: make a failing/red remote CI build green\n"
    "  - traceability: link or author test automation / ATC\n"
    "  - test_gap: add missing test coverage for existing code\n"
    "  - investigation: verify/validate/audit or root-cause existing (often "
    "external) code and produce a READ-ONLY report — never modify code\n\n"
    "Task title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n\n"
    "Respond with ONLY a fenced ```json block: "
    '{{"kind": "<one of the kinds>", "reason": "<short cited reason>"}}'
)


def parse_agent_verdict(text: str) -> KindVerdict | None:
    blocks = _JSON_BLOCK.findall(text or "")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not data.get("kind"):
        return None
    return KindVerdict(
        TaskKind.coerce(data.get("kind")),
        str(data.get("reason") or "agent classification"),
        "agent",
    )


async def classify(task: Any, *, override: str | None = None, backend: Any | None = None,
                   max_turns: int = 6) -> KindVerdict:
    """Classify a task: deterministic first, Agent-SDK fallback for ambiguity.

    The agent runs ONLY when (a) a backend is provided, (b) there is no override,
    and (c) the deterministic pass found no specific signal (``default``) on a
    freeform task — i.e. exactly the case the rules can't resolve. The agent runs
    read-only and cannot change the route to anything outside the enum.
    """
    deterministic = classify_kind(task, override=override)
    if (
        backend is None
        or override
        or deterministic.source != "default"
        or (getattr(task, "source", "") or "").lower() != "freeform"
    ):
        return deterministic

    criteria = "\n".join(f"  - {c}" for c in (getattr(task, "acceptance_criteria", []) or [])) \
        or "  (none stated)"
    prompt = _AGENT_PROMPT.format(
        title=getattr(task, "title", "") or "",
        description=getattr(task, "description", "") or "(none)",
        criteria=criteria,
    )
    try:
        result = await backend.run(prompt, max_turns=max_turns, effort="low")
    except Exception:  # noqa: BLE001 — a classifier failure must not block intake
        return deterministic
    verdict = parse_agent_verdict(getattr(result, "final_text", "") or "")
    return verdict or deterministic
