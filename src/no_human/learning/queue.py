"""Human-confirmed learning queue (PLAN.md 4.5).

On success → propose a skill/fact. On failure → propose an anti-pattern/rule.
On a reviewer FAIL round → propose a rule/anti-pattern distilled from the
blocking findings (B1/G2).
Proposals are queued (``confirmed=0``) and *never* enter the active rule set
until a human confirms them in ``nh learnings`` — auto-writing rules from a
leniency-biased self-assessment would let bad lessons accumulate silently.

The active rule set (confirmed memories) is what later tasks consult; proposals
are inert until promoted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.db import SOURCE_PROPOSED, Store
from ..core.task import Task, TaskStatus
from .corrections import (
    MIN_OCCURRENCES,
    CorrectionCluster,
    CorrectionRecord,
    build_correction_distill_prompt,
    cluster_corrections,
    is_project_scoped,
)
from .pii import contains_pii
from .scope import resolve_project_scope
from .vocab import sanitize_tags

log = logging.getLogger("no_human.learning")


# WHICH SIGNAL A PROPOSAL CAME FROM — the `memories.origin` column.
#
# NOT `source`. `source` is the queue-VISIBILITY contract: `pending()`, `nh
# learnings`, the API and the ingester all select `source="proposed"`, so a
# proposal that named its provenance there would be invisible to the human gate
# it exists for (B1 already had to write that comment once). `origin` is a
# second column for a second question, and NULL on every row written before it
# existed — which is honest, not a gap: those rows genuinely do not record it.
ORIGIN_REVIEW = "review"          # B1: a reviewer FAIL round's blocking findings
ORIGIN_SUPERVISOR = "supervisor"  # B2: a recurring supervisor `correct` decision
# S1: the three producers that used to name themselves in `source` and were
# therefore never queued at all. Their proposals now write source="proposed"
# like everyone else and say what they are HERE, which is where the human
# reading `nh learnings` actually sees it.
ORIGIN_HISTORY = "history"        # `nh history --analyze`, mining past transcripts
ORIGIN_REPLY = "reply"            # a rule mined from the operator's own `nh reply`
ORIGIN_CURATOR = "curator"        # curator.py's consolidation of N proposals into 1
# The failure-harvest loop (`learning/failures.py`): three more recurring
# signals clustered through the SAME machinery as ORIGIN_SUPERVISOR. Each is
# its own origin, not a shared "failure" bucket, for the same reason B2 has
# its own: a human triaging `nh learnings` needs to filter by WHICH signal
# recurred. `ORIGIN_REVIEW_FAIL` is deliberately distinct from `ORIGIN_REVIEW`
# (B1's per-round proposal from ONE FAIL round's findings) — different origin
# strings mean different dedupe keys (`correction_dedupe_key` includes
# `cluster.source`), so a recurring finding harvested here can never collide
# with, or be shadowed by, B1's own in-flight proposal for the same finding.
ORIGIN_ESCALATION = "escalation"    # an agent escalating to a human, repeated
ORIGIN_REVIEW_FAIL = "review_fail"  # a reviewer FAIL finding, repeated across rounds
ORIGIN_TAMPER = "tamper"            # a tripped tamper guard, repeated

# Origins whose "no" must ARCHIVE rather than delete — the same treadmill
# argument `reject()`'s docstring makes for ORIGIN_SUPERVISOR, applied to the
# rest of the batch-driven producers. A delete drops the row AND the dedupe key
# it carries in `file_path`, so for any producer that RE-READS its whole input
# on every run the rejected lesson comes straight back on the next pass.
#
# `history` re-reads every transcript on each `--analyze`; `curator` re-reads
# every surviving proposal on each `--curate`. Both are treadmills. `review`
# and `reply` are event-driven — a deleted one returns only when the reviewer
# raises that finding again, or the operator says the same thing again, which
# is new evidence and worth re-asking about. That asymmetry is the rule, not
# the list: the question is "can this producer regenerate the proposal without
# new evidence?", and the answer decides the verb.
#
# The three failure-harvest origins (`ORIGIN_ESCALATION`, `ORIGIN_REVIEW_FAIL`,
# `ORIGIN_TAMPER`) are batch-driven the same way `ORIGIN_SUPERVISOR` is — the
# scheduled `HarvestJob` re-reads the whole escalation/review-fail/tamper
# history on every pass — so the identical treadmill argument applies: a
# delete would let the next pass re-propose a lesson the human just rejected.
ARCHIVE_ON_REJECT = frozenset({
    ORIGIN_SUPERVISOR, ORIGIN_HISTORY, ORIGIN_CURATOR,
    ORIGIN_ESCALATION, ORIGIN_REVIEW_FAIL, ORIGIN_TAMPER,
})


# WHO confirmed a memory into the active set — the `memories.confirmed_by`
# column (D3-M1). 'human' is every `nh learnings`/API confirm; 'auto' is the
# evidence-gated auto-confirm below. The pair (ORIGIN_REVIEW, CONFIRMED_BY_AUTO)
# is the EXACT tuple the reviewer's confirmed-rules channel excludes, so an
# auto-confirmed review lesson reaches the coder and never the reviewer that
# produced it (gate independence, constraint #3). A NULL `confirmed_by` (a row
# confirmed before the column existed) is human/legacy, not 'auto'.
CONFIRMED_BY_HUMAN = "human"
CONFIRMED_BY_AUTO = "auto"

# D3-M1: how many DISTINCT human-approved tasks a review finding must recur on
# before it may auto-confirm. Two is the minimum that makes "recurring" mean
# more than one task — one occurrence is this task's own bug, two across
# distinct approved tasks is a repository requirement.
_MIN_RECURRENCE_TASKS = 2


# Memory types (matches the migrations schema CHECK-free `type` column).
TYPE_SKILL = "skill"
TYPE_FACT = "fact"
TYPE_RULE = "rule"
TYPE_ANTI_PATTERN = "anti_pattern"

# Failure categories that are transient/resource/environmental, not a reusable
# code lesson. A budget cap, an infra flake, a quota wall, a waiting-on-dep, or
# a missing credential recurs because of the environment — capturing it as a
# durable anti-pattern floods the human's confirm queue with noise (the queue
# reached 197 pending, almost all BUDGET_EXHAUSTED + env-failure artifacts).
# Genuine anti-patterns (NOVEL_UNKNOWN, STAGNATION, SCOPE_EXPLOSION, IMPOSSIBLE,
# AMBIGUITY) still propose.
NON_LEARNABLE_CATEGORIES = frozenset({
    "TRANSIENT_INFRA",
    "QUOTA",
    "DEPENDENCY_WAIT",
    "BUDGET_EXHAUSTED",
    "MISSING_ACCESS",
})


# A reviewer FAIL is QUALITY-shaped by construction: the gate judges a diff
# against the acceptance criteria, so its findings carry no failure category
# and NON_LEARNABLE_CATEGORIES above — transient infra, quota, budget, missing
# access — cannot apply to them. There are exactly TWO harness-authored rows
# this path can produce that are not reviewer findings at all:
#
#   1. The fail-closed sentinel the orchestrator writes when the REVIEWER
#      ITSELF crashes (`ChecklistItem("reviewer run", False, "reviewer
#      crashed: …")`) — an environment failure wearing a finding's clothes.
#   2. The `_PRE_REVIEW_RED_LABEL` ("pre-review test run") row `_run_review`
#      staples onto an already-FAILED decision so a red test the harness's
#      OWN pre-review run found (never graded by the reviewer, never
#      classified by TESTING — see `core/orchestrator.py`'s `_run_review`
#      pre-review block) still reaches the coder's next-round prompt.
#
# Learning either would file a harness artefact — an SDK outage, or the
# harness's own unclassified test run — in the human's confirm queue as a
# durable lesson about the repo. Both are filtered for the same reason the
# categories above are; anything the reviewer genuinely found is learnable.
_INFRA_FINDING_MARKERS = (
    "reviewer crashed", "reviewer run", "reviewerunavailable",
    "pre-review test run",
)

_MAX_FINDINGS = 3        # the blocking few; the gate already orders by severity
_MAX_EVIDENCE = 200      # chars of cited evidence kept per finding
_MAX_DISTILL = 600       # bound on the utility tier's whole reply
_MAX_CONTENT = 1000      # bound on the stored proposal body

# An async (prompt) -> text callable. Injected, exactly like intake's optional
# `backend=`, so this layer never reaches for an LLM itself and tests never
# make a real call.
DistillFn = Callable[[str], Awaitable[str]]

# A sync (text) -> None sink for "this proposal did NOT get written, and here is
# why". Injected the same way `DistillFn` is; the orchestrator passes
# `self._advisory`, so a refusal lands in the event stream and `nh doctor`'s
# count instead of vanishing into a bare `None` return that the caller cannot
# tell apart from "nothing was learnable".
NoteFn = Callable[[str], None]


def _is_infra_finding(finding: dict[str, Any]) -> bool:
    text = f"{finding.get('label', '')} {finding.get('evidence', '')}".lower()
    return any(marker in text for marker in _INFRA_FINDING_MARKERS)


def _finding_lines(findings: list[dict[str, Any]]) -> str:
    lines = []
    for f in findings:
        where = f":{f.get('line')}" if f.get("file") and f.get("line") else ""
        loc = f" ({f.get('file')}{where})" if f.get("file") else ""
        lines.append(
            f"  - {str(f.get('label') or '?')}"
            f" — {str(f.get('evidence') or '')[:_MAX_EVIDENCE]}{loc}"
        )
    return "\n".join(lines)


_STOP_TAGS = frozenset({
    "this", "that", "with", "from", "must", "does", "when", "then", "have",
    "there", "which", "into", "should", "review", "test", "tests", "code",
})


def _tags_from_findings(findings: list[dict[str, Any]]) -> list[str]:
    """Trigger keywords derived from the findings themselves (learning/
    triggers.py matches a tag as a substring of a future task's text), plus
    ``review`` so the queue can be filtered by where a lesson came from."""
    words: list[str] = []
    for f in findings:
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{3,}", str(f.get("label") or "")):
            low = word.lower()
            if low not in words and low not in _STOP_TAGS:
                words.append(low)
    return ["review", *words[:4]]


def correction_dedupe_key(cluster: CorrectionCluster) -> str:
    """The dedupe signature for a correction cluster.

    Keyed on the SOURCE, the GIST and the project — never on the distilled
    text or on the example messages. The gist is a pure function of one
    message (``corrections.normalize_gist``), so the key is stable when the
    cluster grows and when the store is re-harvested; an LLM's phrasing is not
    stable enough to dedupe on. ``source`` is included so the SAME gist from
    two different signals (e.g. a supervisor correction and an escalation that
    happen to share their four leading tokens) never collide onto one key —
    they are not the same lesson, and ``cluster_corrections`` already never
    merges them into one cluster (source is part of the bucket key too).

    STABILITY UNDER THE S2 CHANGE: ``cluster.source`` defaults to
    ``"supervisor"``, so every pre-existing cluster (built before this field
    existed) produces the BYTE-IDENTICAL key it always did —
    ``test_supervisor_dedupe_key_is_unchanged_by_the_source_field`` pins this.

    A module-level function rather than a method because BOTH the builder and
    the batch harvest need it, and the harvest needs it BEFORE the distillation
    it would otherwise pay for.
    """
    return _sig(cluster.source, cluster.gist, cluster.project or "")


def _tags_from_gist(gist: str, *, prefix: str = ORIGIN_SUPERVISOR) -> list[str]:
    """Trigger keywords for a correction cluster, from its gist. Same contract
    as `_tags_from_findings` — a leading provenance tag so the queue can be
    filtered by where a lesson came from, then keywords `learning/triggers.py`
    can match as substrings of a future task's text.

    `prefix` names which of the four harvest signals produced the cluster
    (every pre-existing call site leaves it at its default, `ORIGIN_SUPERVISOR`).
    `_build_from_corrections` already de-duplicates this against the row's
    real `origin` before storing, so `prefix` only ever matters on the
    undistilled fallback path, where nothing else supplies a source-aware tag.
    """
    words = [w for w in gist.split() if w not in _STOP_TAGS]
    return [prefix, *words[:4]]


def build_review_distill_prompt(task: Task, findings: list[dict[str, Any]]) -> str:
    """The bounded, single-turn prompt handed to the utility tier."""
    return (
        "An independent code reviewer BLOCKED an autonomous coding agent's "
        "change to this repository. Its blocking findings were:\n"
        f"{_finding_lines(findings)}\n"
        f"\nThe task was: {task.title}\n"
        "\nDistill ONE durable lesson a FUTURE task in THIS repository should "
        "carry, so the same finding is not re-derived from scratch. Describe "
        "the repository's requirement, not this task's bug.\n"
        "Reply in EXACTLY these four lines, under 600 characters in total:\n"
        "TYPE: rule|anti_pattern   (rule = a requirement this repo imposes; "
        "anti_pattern = a mistake to avoid)\n"
        "TITLE: <one line, <=80 chars>\n"
        "LESSON: <<=300 chars, concrete and checkable — name the file, API or "
        "command involved>\n"
        "TAGS: <2-4 comma-separated lowercase keywords that would appear in a "
        "future task's text>"
    )


def parse_review_lesson(
    text: str | None,
) -> tuple[str, str, str, list[str]] | None:
    """(type, title, lesson, tags) from the utility tier's reply, or None when
    it did not answer in the required shape. Advisory: an unparseable reply
    degrades to the undistilled findings, it never blocks the proposal."""
    raw = (text or "").strip()[:_MAX_DISTILL]
    if not raw:
        return None
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().upper() in ("TYPE", "TITLE", "LESSON", "TAGS"):
            fields[key.strip().upper()] = value.strip()
    lesson = fields.get("LESSON", "")
    if not lesson:
        return None  # no lesson → nothing was distilled
    mem_type = TYPE_RULE if fields.get("TYPE", "").lower().startswith(
        "rule") else TYPE_ANTI_PATTERN
    tags = [t.strip().lower() for t in fields.get("TAGS", "").split(",") if t.strip()]
    return mem_type, fields.get("TITLE", "")[:120], lesson[:400], tags[:4]


def _sig(*parts: str) -> str:
    """Stable dedupe signature so the same lesson isn't proposed twice."""
    raw = "\x1f".join(p.strip().lower() for p in parts if p)
    return "learn:" + hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass
class AutoActivateReport:
    """What one `LearningQueue.auto_activate` call did (D3, 2026-08-31)."""

    activated: list[str] = field(default_factory=list)
    # Screen-failing rows, archived rather than left pending — 'low-quality
    # or screen-failing proposals are archived, not queued' (D3 design).
    archived: list[str] = field(default_factory=list)
    # True iff at least one otherwise-eligible row stayed pending because the
    # daily cap had no budget left this tick.
    cap_hit: bool = False


@dataclass
class Proposal:
    mem_type: str
    title: str
    content: str
    dedupe_key: str
    tags: list[str]
    # Which signal produced it (ORIGIN_*), or None for the outcome-derived
    # proposals that predate the column.
    origin: str | None = None
    # B3: the structured record of what happened — which task, which
    # correction/review event — stored beside the prose `content` that
    # narrates the same facts for the confirming human. A dict so a consumer
    # reads fields instead of re-parsing prose; None only where a path has
    # nothing structured to say.
    evidence: dict[str, Any] | None = None
    # The free tags BEFORE vocabulary sanitation (learning/vocab.py), kept for
    # the PII gate and never stored. Sanitation must not double as redaction:
    # a distiller reply carrying a personal email in its TAGS line is
    # contaminated output, and the whole proposal is dropped — silently
    # laundering the tag and storing the rest would be exactly the
    # redact-instead-of-drop behaviour learning/pii.py refuses.
    raw_tags: list[str] = field(default_factory=list)


# Which `ORIGIN_*` a cluster's `source` maps to. `cluster.source` (from
# `learning/corrections.py`) is the clustering-machine's own vocabulary
# ("supervisor"/"escalation"/"review_fail"/"tamper"); ORIGIN_* is the
# `memories.origin` vocabulary. They read the same today by construction
# (`ORIGIN_ESCALATION = "escalation"` etc.), but this map is the single place
# that fact is asserted, so the two are free to diverge without hunting every
# call site.
_SOURCE_ORIGIN: dict[str, str] = {
    "supervisor": ORIGIN_SUPERVISOR,
    "escalation": ORIGIN_ESCALATION,
    "review_fail": ORIGIN_REVIEW_FAIL,
    "tamper": ORIGIN_TAMPER,
}

# Source-aware prose for `_build_from_corrections`, keyed on `cluster.source`.
# `title`/`head` are `.format()` templates (`{gist}`, `{provenance}`,
# `{lesson}`, `{count}`, `{tasks}`); `label` and `kind` are literal. Falls
# back to the `"supervisor"` entry for any unrecognised source, which is also
# the entry every pre-existing call site (all of them supervisor-sourced)
# exercises — unchanged prose, unchanged `kind` string.
_SOURCE_PROSE: dict[str, dict[str, str]] = {
    "supervisor": {
        "title": "Repeated supervisor correction: {gist}",
        "head": "Corrected repeatedly by the supervisor ({provenance}).\nLesson: {lesson}\n",
        "label": "What the supervisor kept saying:\n",
        "what": "the supervisor issued the same correction {count}x across {tasks} task(s)",
        "kind": "supervisor_correction",
    },
    "escalation": {
        "title": "Repeated escalation: {gist}",
        "head": "Escalated to a human repeatedly for the same reason ({provenance}).\nLesson: {lesson}\n",
        "label": "What it escalated about:\n",
        "what": "the agent escalated to a human for the same reason {count}x across {tasks} task(s)",
        "kind": "escalation",
    },
    "review_fail": {
        "title": "Repeated reviewer FAIL: {gist}",
        "head": "Blocked by the reviewer for the same finding repeatedly ({provenance}).\nLesson: {lesson}\n",
        "label": "What the reviewer kept blocking on:\n",
        "what": "the reviewer raised the same blocking finding {count}x across {tasks} task(s)",
        "kind": "review_finding_recurring",
    },
    "tamper": {
        "title": "Repeated tamper trip: {gist}",
        "head": "Tripped the tamper guard repeatedly for the same summary ({provenance}).\nLesson: {lesson}\n",
        "label": "What tripped the tamper guard:\n",
        "what": "the tamper guard tripped on the same summary {count}x across {tasks} task(s)",
        "kind": "tamper_trip",
    },
}


def _evidence_strings(evidence: dict[str, Any] | None) -> list[str]:
    """Every string value inside an evidence dict, flattened — so the PII gate
    reads the WHOLE row it is guarding. Evidence quotes corrections and cited
    finding text verbatim, and `content` truncates where evidence does not:
    gating title/content/tags but not this field would be a fourth door into
    the same table."""
    out: list[str] = []
    stack: list[Any] = [evidence]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return out


async def _project_scope_of(path: str | None) -> str | None:
    """The B4 scope identity for a checkout path, off-thread (it shells out to
    git), or None — a repo with no remote, a path that is gone, git failing.
    None is always safe: the row stays on legacy path matching."""
    if not (path or "").strip():
        return None
    try:
        return await asyncio.to_thread(resolve_project_scope, path)
    except Exception:  # noqa: BLE001 — scoping is never worth losing a lesson
        return None


# Memory lifecycle C: the per-success templated skill proposal (the
# AWAITING_APPROVAL/DONE branch of `_build`) is the flood source — it
# produced ~394 of the NULL-origin pending rows measured against a copy of
# the operator's database (487-488 pending vs 53 active), and it carries no
# evidence beyond "a task finished to a reviewable PR". GATED, not deleted
# (reversibility is the point of the whole lifecycle design): the code stays,
# a keyword-only flag with a default that keeps it off, so every existing
# call site (`LearningQueue(store)`, no flag) gets the safe direction without
# being touched.
PROPOSE_ON_SUCCESS_DEFAULT = False


class LearningQueue:
    """Proposes learnings from task outcomes and manages confirmation."""

    def __init__(self, store: Store, *,
                 propose_on_success: bool = PROPOSE_ON_SUCCESS_DEFAULT):
        self.store = store
        self.propose_on_success = propose_on_success

    # ----------------------------- propose --------------------------------- #

    async def propose_from_outcome(
        self, task: Task, *, status: TaskStatus, blocker: dict[str, Any] | None = None,
        summary: str = "",
    ) -> str | None:
        """Queue a proposal derived from a terminal task outcome.

        success (awaiting_approval/done) → skill; structural blocker/failure →
        anti-pattern. Returns the new memory id, or None if deduped/not applicable.
        """
        proposal = self._build(task, status=status, blocker=blocker, summary=summary)
        if proposal is None:
            return None
        # Personal data is never a durable engineering lesson. A task title or a
        # blocker's root-cause text can quote a user's data verbatim; drop the
        # whole proposal rather than redact it (see learning/pii.py). Evidence
        # is inside the gate too (B3): it quotes the same task text.
        pii = contains_pii(proposal.title, proposal.content,
                           *_evidence_strings(proposal.evidence))
        if pii is not None:
            log.info("refused a proposed learning carrying personal data (%s)",
                     pii.kind)
            return None
        return await self.store.add_memory(
            mem_type=proposal.mem_type,
            title=proposal.title,
            content=proposal.content,
            tags=proposal.tags,
            project=task.repo_path,
            source="proposed",
            confirmed=False,
            dedupe_key=proposal.dedupe_key,
            evidence=proposal.evidence,
            project_scope=await _project_scope_of(task.repo_path),
        )

    def _build(
        self, task: Task, *, status: TaskStatus, blocker: dict[str, Any] | None,
        summary: str,
    ) -> Proposal | None:
        if status in (TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE):
            # Memory lifecycle C flood control: this branch produced ~394 of
            # the NULL-origin pending rows measured against a copy of the
            # operator's live database, and carries no evidence beyond "a
            # task finished" — no reviewer finding, no correction, no
            # operator input. Gated behind `propose_on_success` (default
            # off) rather than deleted, for reversibility. See
            # `PROPOSE_ON_SUCCESS_DEFAULT` above.
            if not self.propose_on_success:
                return None
            title = f"Approach that worked: {task.title}"[:120]
            content = (
                f"Task '{task.title}' completed to a reviewable PR.\n"
                f"{('Summary: ' + summary) if summary else ''}\n"
                "Consider capturing the successful approach as a reusable skill."
            ).strip()
            return Proposal(
                TYPE_SKILL, title, content,
                _sig("skill", task.title, task.repo_path or ""),
                # Enum-derived, not free text — outside the B3 vocabulary on
                # purpose (see learning/vocab.py: it governs free tags).
                tags=["success", task.source],
                evidence={
                    "kind": "task_outcome",
                    "what": f"task completed to a reviewable PR ({status.value})",
                    "task_id": task.id,
                    "status": status.value,
                },
            )

        # Failure / structural blocker → anti-pattern (22.8).
        if blocker:
            cat = blocker.get("category", "NOVEL_UNKNOWN")
            # Transient/resource/environmental failures are not reusable code
            # lessons — don't durably propose them (they flooded the queue).
            if str(cat).upper() in NON_LEARNABLE_CATEGORIES or blocker.get("transient"):
                return None
            cause = blocker.get("root_cause_hypothesis", "")
            tried = blocker.get("tried", []) or []
            title = f"Anti-pattern [{cat}]: {task.title}"[:120]
            content = (
                f"Category: {cat}\n"
                f"Root cause hypothesis: {cause}\n"
                f"Tried: {'; '.join(tried) if tried else '(none recorded)'}\n"
                "If this surprise recurs, anticipate it: capture the fix as a "
                "rule/skill so it becomes an auto-handled case (Part 22.8)."
            )
            return Proposal(
                TYPE_ANTI_PATTERN, title, content,
                _sig("anti", cat, cause or task.title, task.repo_path or ""),
                # Enum-derived (blocker category), outside the B3 vocabulary
                # on purpose — see learning/vocab.py.
                tags=["blocker", cat],
                evidence={
                    "kind": "task_outcome",
                    "what": f"task blocked ({cat}): {str(cause)[:200]}",
                    "task_id": task.id,
                    "status": status.value,
                    "category": str(cat),
                },
            )
        return None

    # --------------------- reviewer findings (B1 / G2) --------------------- #

    async def propose_from_review(
        self, task: Task, *, findings: list[dict[str, Any]],
        attempt: int | None = None, review_round: int = 1,
        distill: DistillFn | None = None, note: NoteFn | None = None,
        auto_confirm_recurring: bool = False,
    ) -> str | None:
        """Queue ONE proposal distilled from a reviewer FAIL round's blocking
        findings. Returns the new memory id, or None (nothing learnable, or
        deduped against a finding already proposed).

        The gate's findings were the biggest labelled-failure signal the product
        threw away: they were written to ``task.context['review_feedback']``,
        read by the next attempt of the SAME task, and discarded with it. The
        next task re-derived "this repo requires X" from scratch.

        *distill* is an optional async ``(prompt) -> text`` callable (the
        utility tier, injected by the orchestrator exactly as intake injects a
        backend). Without it the lesson is the findings themselves — degraded,
        never absent, and never an LLM call this layer made on its own.

        *note* is an optional sink for the two ways this returns ``None`` with
        something a human should know: a refusal on personal data, and a dedupe
        hit. Without it both are silent, which is how the second occurrence of a
        recurring finding disappeared with no trace.

        GATE INDEPENDENCE (design principle 6): the proposal is written
        ``confirmed=0``/``source="proposed"``, and the reviewer prompt is built
        from ``list_memories(confirmed=True, …)``. A reviewer therefore cannot
        consume a rule derived from its own verdict until a HUMAN confirms it.

        *auto_confirm_recurring* (D3-M1, default False, from
        ``config learning.auto_confirm_recurring``) narrows — never removes —
        that human step for the CODER'S CHANNEL ONLY. When a review finding
        DEDUPES (it has recurred) across >=2 DISTINCT tasks in this project that
        each reached HUMAN approval (a MERGED PR outcome), the deduped row is
        auto-confirmed with ``confirmed_by='auto'``. Gate independence still
        holds because the reviewer's confirmed-rules channel EXCLUDES
        (origin='review' AND confirmed_by='auto'): the lesson reaches the coder
        (its learning value) but never the reviewer that produced it. A human
        confirm still writes ``confirmed_by='human'`` and DOES reach the
        reviewer, because a human then stands behind the rule.
        """
        proposal = await self._build_from_review(
            task, findings=findings, attempt=attempt,
            review_round=review_round, distill=distill,
        )
        if proposal is None:
            return None
        # THE SAME GATE `propose_from_outcome` APPLIES, and this path needs it
        # at least as badly: a reviewer's cited evidence quotes repo content
        # VERBATIM, so whatever a file or a diff hunk contains — a shipping
        # address in a fixture, a personal email in a test — arrives here inside
        # `evidence` and would be persisted to `memories` and shown back to the
        # user. Drop the whole proposal rather than redact it (learning/pii.py).
        #
        # TAGS ARE GATED TOO. They are not derived text: on the distilled path
        # they come verbatim from the utility model's TAGS line, and that model
        # was shown the raw findings — so a surname or a personal email can
        # arrive as a "keyword" while title and content are clean. Tags are
        # persisted to the same row, rendered in the same queue, and matched
        # against future task text by `learning/triggers.py`. Gating two of the
        # three stored fields is not a gate. The B3 evidence field quotes the
        # findings verbatim WITHOUT `content`'s truncation, so it is inside
        # the gate too (`_evidence_strings`) — as are the RAW pre-vocabulary
        # tags: sanitation must not double as redaction, so a distiller reply
        # carrying personal data in its TAGS line still drops the whole
        # proposal even though the offending tag would never be stored.
        pii = contains_pii(proposal.title, proposal.content, *proposal.tags,
                           *proposal.raw_tags,
                           *_evidence_strings(proposal.evidence))
        if pii is not None:
            log.info("refused a proposed learning carrying personal data (%s)",
                     pii.kind)
            # Not silent: a dropped lesson is a thing a human may need to
            # explain later, and `kind` names the category without ever
            # carrying the value (PIIFinding exposes nothing else on purpose).
            if note is not None:
                note("review learning refused: the finding's evidence carries "
                     f"personal data ({pii.kind})")
            return None
        mem_id = await self.store.add_memory(
            mem_type=proposal.mem_type,
            title=proposal.title,
            content=proposal.content,
            tags=proposal.tags,
            # The human-readable checkout path, kept for `nh learnings`' scope
            # line. The IDENTITY recall matches on is `project_scope` below —
            # B4's remote hash, the same for every checkout of this repo, so
            # one repo's rows are one scope even across worktrees.
            project=task.repo_path,
            project_scope=await _project_scope_of(task.repo_path),
            # NOT the provenance string. `source` is the queue-visibility
            # contract — `pending()`, `nh learnings`, the API and the ingester
            # all select `source="proposed"`, so a proposal that named its task
            # here would be invisible to the human gate it exists for. The
            # task/attempt/round provenance rides in the content, where the
            # human reading the confirm queue actually sees it — and, since
            # B3, in the structured `evidence` field beside it.
            source="proposed",
            confirmed=False,
            dedupe_key=proposal.dedupe_key,
            origin=proposal.origin,
            evidence=proposal.evidence,
        )
        # A dedupe hit is the queue working as designed — but it is ALSO the
        # only evidence that this wall was hit twice, and `add_memory` returns a
        # bare None for it. Recurrence is the signal a human most wants out of
        # this queue ("we keep tripping on the same rule"), so say it rather
        # than let the second occurrence vanish. The dedupe key is quoted so the
        # reader can find the row it collapsed onto.
        #
        # D3-M1: a dedupe hit is ALSO the trigger for evidence-gated
        # auto-confirm — the finding recurred, and this task is the fresh
        # occurrence. Only review-origin proposals are eligible; nothing else
        # takes this branch because only they carry ORIGIN_REVIEW.
        if mem_id is None:
            if proposal.origin == ORIGIN_REVIEW:
                await self._maybe_auto_confirm_recurring(
                    task, proposal.dedupe_key,
                    enabled=auto_confirm_recurring, note=note)
            if note is not None:
                note("recurring review finding, deduped to "
                     f"{proposal.dedupe_key}")
        return mem_id

    async def _maybe_auto_confirm_recurring(
        self, task: Task, dedupe_key: str, *, enabled: bool,
        note: NoteFn | None,
    ) -> bool:
        """D3-M1: evidence-gated auto-confirm of a RECURRING review-origin
        lesson, into the CODER'S CHANNEL ONLY.

        Returns True iff it confirmed the row. Every gate below is required, and
        a miss is always the SAFE direction — it withholds a lesson from the
        coder, it can never let one reach the reviewer:

          * ``enabled`` (``config learning.auto_confirm_recurring``, default
            OFF). When off this returns immediately and records NOTHING, so the
            feature is inert by default down to the last write.
          * the finding recurred on >= 2 DISTINCT tasks in this project (the
            deduped row's ``evidence.task_id`` plus its recorded recurrences —
            ``Store.add_review_recurrence`` counts them; the dedupe key already
            scopes to one repo, so "same project" holds by construction).
          * >= 2 of those distinct tasks reached HUMAN approval — a MERGED PR
            outcome (migration 0010). A merge is the human-in-the-loop signal
            this repo can actually verify.
          * the row is still UNCONFIRMED. Never overwrite a human's confirm
            (that would downgrade ``confirmed_by`` from 'human' to 'auto' and,
            worse, is pointless) and never resurrect a human's reject (a
            review-origin reject DELETES the row, so a rejected lesson is never
            reachable here — the dedupe key is gone and the next occurrence
            starts a fresh proposal instead).

        On success writes ``confirmed_by='auto'`` via ``store.confirm_memory``.
        """
        if not enabled:
            # OFF must be inert: do not even record the recurrence, so a run with
            # the flag off is byte-for-byte the pre-D3-M1 behaviour.
            return False
        distinct = await self.store.add_review_recurrence(dedupe_key, task.id)
        if len(distinct) < _MIN_RECURRENCE_TASKS:
            return False
        row = await self.store.get_memory_by_dedupe_key(dedupe_key)
        if row is None or row.get("confirmed"):
            return False  # gone, or already confirmed (human or auto)
        approved = 0
        for tid in distinct:
            if await self._task_reached_human_approval(tid):
                approved += 1
                if approved >= _MIN_RECURRENCE_TASKS:
                    break
        if approved < _MIN_RECURRENCE_TASKS:
            return False
        ok = await self.store.confirm_memory(
            row["id"], confirmed_by=CONFIRMED_BY_AUTO)
        if ok and note is not None:
            note("auto-confirmed recurring review lesson "
                 f"{str(row['id'])[:8]} — recurred across >= "
                 f"{_MIN_RECURRENCE_TASKS} human-approved tasks (coder channel "
                 "only; excluded from the reviewer)")
        return ok

    async def _task_reached_human_approval(self, task_id: str) -> bool:
        """True when a task's PR reached a MERGED outcome (migration 0010's
        ``pr_outcomes``).

        A merge is the human-approval signal this product can VERIFY: a human
        clicked merge on the forge. ``closed_unmerged``/``open``/``unknown`` are
        not approval — a closed-unmerged PR is a human's rejection, and open/
        unknown are undecided. Multi-repo tasks open several PRs; ANY merged
        outcome for the task counts it approved, matching how the finding could
        have been fixed in any one of them."""
        from ..vcs.pr_outcome import MERGED
        rows = await self.store.list_pr_outcomes(task_id=task_id)
        return any(r.get("outcome") == MERGED for r in rows)

    async def _build_from_review(
        self, task: Task, *, findings: list[dict[str, Any]],
        attempt: int | None, review_round: int, distill: DistillFn | None,
    ) -> Proposal | None:
        learnable = [f for f in (findings or []) if not _is_infra_finding(f)]
        if not learnable:
            return None
        learnable = learnable[:_MAX_FINDINGS]
        evidence = _finding_lines(learnable)

        mem_type, title, lesson, tags = TYPE_ANTI_PATTERN, "", "", []
        if distill is not None:
            parsed = parse_review_lesson(
                await distill(build_review_distill_prompt(task, learnable))
            )
            if parsed is not None:
                mem_type, title, lesson, tags = parsed
        if not title:
            title = f"Review blocked: {learnable[0].get('label') or task.title}"
        if not lesson:
            # Degraded but honest: the findings ARE the lesson, unpolished.
            lesson = "(not distilled) " + "; ".join(
                str(f.get("label") or "") for f in learnable)
        if not tags:
            tags = _tags_from_findings(learnable)
        # B3: only vocabulary tags are stored — free tags rot (learning/
        # vocab.py); the raw ones survive on the proposal for the PII gate
        # only. And the provenance tag is now UNCONDITIONAL, closing the gap
        # B2 had to note: it used to arrive only on the degraded path, so a
        # proposal the distiller answered for could not be filtered by where
        # it came from.
        raw_tags = list(tags)
        tags = [ORIGIN_REVIEW,
                *(t for t in sanitize_tags(tags) if t != ORIGIN_REVIEW)]

        provenance = "task {} · attempt {} · review round {}".format(
            task.id[:8], attempt if attempt is not None else "?", review_round)
        # ORDER IS LOAD-BEARING, and a trailing `[:_MAX_CONTENT]` over
        # header→evidence→lesson silently ate the lesson: three findings at
        # `_MAX_EVIDENCE` each fill ~700 of the 1000 characters before the
        # word "Lesson" is even reached, so the ONE distilled sentence the
        # utility call was spent on arrived truncated to a few dozen chars —
        # and the padding that displaced it is the verbatim evidence, which
        # the reader can still go and look up in the repo. So: the lesson goes
        # FIRST and whole, and the evidence gets the room that is left. Both
        # are budgeted explicitly; nothing here relies on a final slice.
        head = f"Blocked by the review gate ({provenance}).\nLesson: {lesson}\n"
        label = "Blocking findings:\n"
        room = _MAX_CONTENT - len(head) - len(label)
        content = (head + label + evidence[:room]) if room > 0 else head[:_MAX_CONTENT]
        return Proposal(
            mem_type, title[:120], content,
            # Keyed on the FINDING LABELS, not on the distilled text: the same
            # finding raised again next round (or on the next task in this
            # repo) must collapse onto one queue entry, and an LLM's phrasing
            # is not stable enough to dedupe on.
            _sig("review", *sorted(
                str(f.get("label") or "") for f in learnable
            ), task.repo_path or ""),
            tags=tags,
            raw_tags=raw_tags,
            origin=ORIGIN_REVIEW,
            # B3: the same provenance the prose narrates, as fields — what
            # happened, in which task, citing each blocking finding.
            evidence={
                "kind": "review_finding",
                "what": ("the review gate blocked the change "
                         f"({len(learnable)} finding(s))"),
                "task_id": task.id,
                "attempt": attempt,
                "review_round": review_round,
                "findings": [
                    {
                        "label": str(f.get("label") or ""),
                        "file": f.get("file"),
                        "line": f.get("line"),
                        "evidence": str(f.get("evidence") or "")[:_MAX_EVIDENCE],
                    }
                    for f in learnable
                ],
            },
        )

    # ------------- supervisor corrections (B2) ----------------------------- #

    async def propose_from_corrections(
        self, cluster: CorrectionCluster, *,
        distill: DistillFn | None = None, note: NoteFn | None = None,
    ) -> str | None:
        """Queue ONE proposal distilled from a RECURRING supervisor correction.

        *cluster* is a ``(project, gist)`` group produced by
        ``learning.corrections.cluster_corrections``, which has already applied
        the >=2-occurrence rule — a one-off correction is noise and never
        reaches here. Returns the new memory id, or None (personal data, or
        deduped against a cluster already proposed).

        Everything below this line is B1's machinery, deliberately: the same
        ``DistillFn`` injection, the same four-line reply parser, the same
        ``contains_pii`` gate, the same ``_sig`` dedupe, the same
        ``add_memory(source="proposed", confirmed=False)`` write, the same
        ``NoteFn`` sink for the two silent-None cases. The only thing B2 adds
        is what the input is and how it was aggregated.

        GATE INDEPENDENCE (design principle 6) holds for exactly the reason it
        holds for B1, and it matters MORE here: the supervisor's corrections
        are injected into the coder's own context at runtime, so a correction
        that became an active rule with no human in between would be the agent
        writing its own standing instructions from its own supervisor's
        opinions. It is written ``confirmed=0``; the rules block every agent
        reads is built from ``list_memories(confirmed=True, …)``.
        """
        proposal = await self._build_from_corrections(cluster, distill=distill)
        if proposal is None:
            return None
        # The evidence here is the supervisor's correction text VERBATIM, and a
        # correction quotes whatever the agent was looking at — a fixture, a
        # seed file, a support thread. Same door, same gate; dropped whole
        # rather than redacted (learning/pii.py). TAGS included for the same
        # reason B1 includes them: on the distilled path they are the utility
        # model's own words, written after it read the raw corrections, so they
        # are an unwatched third field into the same row.
        # The B3 evidence field carries the corrections verbatim, so it is
        # inside the same gate — as are the RAW pre-vocabulary tags, so
        # sanitation cannot double as redaction (see the review path).
        pii = contains_pii(proposal.title, proposal.content, *proposal.tags,
                           *proposal.raw_tags,
                           *_evidence_strings(proposal.evidence))
        if pii is not None:
            log.info("refused a proposed learning carrying personal data (%s)",
                     pii.kind)
            if note is not None:
                note("supervisor correction learning refused: the correction "
                     f"carries personal data ({pii.kind})")
            return None
        mem_id = await self.store.add_memory(
            mem_type=proposal.mem_type,
            title=proposal.title,
            content=proposal.content,
            tags=proposal.tags,
            project=cluster.project,
            source="proposed",
            confirmed=False,
            dedupe_key=proposal.dedupe_key,
            origin=proposal.origin,
            evidence=proposal.evidence,
            # B4: the identity recall matches on. The cluster's repo may no
            # longer exist on disk (the harvest reads history) — then this is
            # None and the row stays on path matching, which is what it would
            # have been anyway.
            project_scope=await _project_scope_of(cluster.project),
        )
        # Idempotence is the design, not an accident: the dedupe key is
        # ``(gist, project)`` and the gist is a pure function of ONE message
        # (corrections.normalize_gist), so re-harvesting the same store — or a
        # store that has grown — collapses onto the same row instead of
        # queueing the lesson twice. Say so rather than return a bare None.
        if mem_id is None and note is not None:
            note(f"recurring supervisor correction ({cluster.count}x), deduped "
                 f"to {proposal.dedupe_key}")
        return mem_id

    async def _build_from_corrections(
        self, cluster: CorrectionCluster, *, distill: DistillFn | None,
    ) -> Proposal | None:
        if cluster.count < 2 or not cluster.gist:
            # Defence in depth. `cluster_corrections` already enforces this;
            # a caller assembling a cluster by hand must not be the way round
            # the rule that makes 257 corrections into a handful of lessons.
            return None
        prose = _SOURCE_PROSE.get(cluster.source, _SOURCE_PROSE[ORIGIN_SUPERVISOR])
        origin = _SOURCE_ORIGIN.get(cluster.source, ORIGIN_SUPERVISOR)
        examples = cluster.examples()
        evidence = "\n".join(f"  - {e}" for e in examples)

        mem_type, title, lesson, tags = TYPE_ANTI_PATTERN, "", "", []
        if distill is not None:
            parsed = parse_review_lesson(
                await distill(build_correction_distill_prompt(cluster))
            )
            if parsed is not None:
                mem_type, title, lesson, tags = parsed
        if not title:
            title = prose["title"].format(gist=cluster.gist)
        if not lesson:
            # Degraded but honest, exactly as B1 degrades: the corrections ARE
            # the lesson, unpolished.
            lesson = "(not distilled) " + (examples[0] if examples else cluster.gist)
        if not tags:
            tags = _tags_from_gist(cluster.gist, prefix=origin)
        # The provenance tag is UNCONDITIONAL here (B1 matched this in B3):
        # the queue has multiple producers and a human triaging it needs
        # "show me the escalation ones" to work on every row, not on the
        # subset the utility tier happened to fail. B3: the free tags behind
        # it — an LLM's TAGS line, gist words — are reduced to the reviewed
        # vocabulary before they become stored data (learning/vocab.py); the
        # raw ones survive on the proposal for the PII gate only.
        raw_tags = list(tags)
        tags = [origin, *(t for t in sanitize_tags(tags) if t != origin)]

        tasks = cluster.task_ids
        provenance = "{}x across {} task(s): {}".format(
            cluster.count, len(tasks),
            ", ".join(t[:8] for t in tasks[:4]) or "?")
        # Same ordering rule B1 had to learn: the lesson is what the utility
        # call was spent on, so it goes FIRST and whole; the verbatim
        # corrections get the room that is left.
        head = prose["head"].format(provenance=provenance, lesson=lesson)
        label = prose["label"]
        room = _MAX_CONTENT - len(head) - len(label)
        content = (head + label + evidence[:room]) if room > 0 else head[:_MAX_CONTENT]
        return Proposal(
            mem_type, title[:120], content,
            correction_dedupe_key(cluster),
            tags=tags,
            raw_tags=raw_tags,
            origin=origin,
            # B3: what happened, in which tasks, citing the correction events
            # themselves — (task_id, ts) is exactly the key that finds each
            # source event row again (`task_events` for supervisor/escalation/
            # tamper, `attempts.review_checklist` for review_fail).
            #
            # `kind` keeps its EXACT pre-S2 value for the default supervisor
            # source (`"supervisor_correction"`, pinned by
            # `test_lesson_evidence_vocab.py` and switched on by
            # `cli/commands.py:_learning_evidence_line`); the three new
            # sources get their own `kind` rather than reusing
            # `"review_finding"` (B1's per-round evidence, a DIFFERENT shape:
            # `task_id`/`attempt`/`review_round` vs this cluster's
            # `task_ids`/`count`). `source_type` is the new, uniform field
            # that always equals `cluster.source` — the one place a reader can
            # tell which of the four signals produced a row without a
            # per-`kind` lookup table.
            evidence={
                "kind": prose["kind"],
                "source_type": cluster.source,
                "what": prose["what"].format(count=cluster.count, tasks=len(tasks)),
                "gist": cluster.gist,
                "count": cluster.count,
                "task_ids": tasks[:8],
                "corrections": [
                    {
                        "task_id": r.task_id,
                        "ts": r.ts,
                        "message": " ".join((r.message or "").split())[:_MAX_EVIDENCE],
                    }
                    for r in cluster.records[:_MAX_FINDINGS]
                ],
            },
        )

    async def harvest_supervisor_corrections(
        self, *, project: str | None = None,
        min_occurrences: int = MIN_OCCURRENCES,
        distill: DistillFn | None = None, note: NoteFn | None = None,
        limit: int = 5000,
    ) -> list[str]:
        """Read every persisted supervisor ``correct`` decision, cluster them,
        and queue one proposal per RECURRING cluster. Returns the ids written
        (deduped clusters contribute nothing, which is what makes a re-run a
        no-op).

        A batch pass rather than an orchestrator hook, and deliberately: a
        single correction cannot know whether it recurs, so there is no point
        in the task's own run at which the >=2 rule can be evaluated. It is
        driven by ``nh learnings --harvest``.

        RE-RUNNING IS A NO-OP, including after triage. A cluster already
        queued is skipped by its dedupe key before anything is spent, and a
        cluster a human REJECTED is skipped too — `reject` archives
        supervisor-origin rows rather than deleting them, so the key survives
        the "no". Confirming a proposal likewise leaves the row and its key in
        place. The one thing that does re-queue a lesson is deleting its row
        out of the database by hand.
        """
        rows = await self.store.list_supervisor_corrections(
            project=project, limit=limit)
        records = [
            CorrectionRecord(
                task_id=str(r.get("task_id") or ""),
                project=r.get("project"),
                message=str(r.get("message") or ""),
                ts=float(r.get("ts") or 0.0),
            )
            for r in rows
        ]
        # Counted, not silently capped: a correction from a task with no
        # `repo_path` cannot be scoped to a repository, and a proposal written
        # with `project=None` is a GLOBAL rule injected into every project
        # (`is_project_scoped` explains the whole case). Excluding them is the
        # conservative call, so the human is told how many went unlearned
        # rather than left to infer it from a number that does not add up.
        unscoped = sum(1 for r in records if not is_project_scoped(r))
        if unscoped and note is not None:
            note(f"{unscoped} supervisor correction(s) skipped: their task has "
                 "no repo_path, and a repo-less lesson would become a rule in "
                 "every project")
        clusters = cluster_corrections(records, min_occurrences=min_occurrences)
        written: list[str] = []
        for cluster in clusters:
            # SPEND GUARD, and the reason `correction_dedupe_key` is a
            # standalone function: this pass re-reads the whole correction
            # history every time it runs, so most clusters on any run after the
            # first are already queued. Letting `add_memory` discover that
            # would mean paying for a distillation per already-known lesson and
            # writing nothing — the same reasoning as B1's `_at_lifetime_ceiling`
            # guard, one layer out.
            key = correction_dedupe_key(cluster)
            if await self.store.memory_dedupe_key_exists(key):
                if note is not None:
                    note(f"recurring supervisor correction ({cluster.count}x), "
                         f"deduped to {key}")
                continue
            mem_id = await self.propose_from_corrections(
                cluster, distill=distill, note=note)
            if mem_id:
                written.append(mem_id)
        return written

    async def harvest_failure_signals(
        self, *, project: str | None = None,
        min_occurrences: int = MIN_OCCURRENCES,
        distill: DistillFn | None = None, note: NoteFn | None = None,
        limit: int = 5000,
    ) -> list[str]:
        """The failure-harvest loop's batch pass: read every persisted
        escalation, reviewer FAIL finding and tamper trip, cluster them, and
        queue one proposal per RECURRING cluster. Returns the ids written.

        Literally ``harvest_supervisor_corrections``'s body with
        ``learning.failures.load_failure_records`` as the input instead of
        ``store.list_supervisor_corrections`` — same clustering, same
        spend-guard dedupe check before distillation, same
        ``propose_from_corrections`` write path. The two harvests are kept as
        separate methods (not one parameterized by source) because that is
        the shape ``nh learnings --harvest`` and the scheduled ``HarvestJob``
        both already call two named things, and because a caller wanting
        "only the supervisor one" (as ``nh learnings --harvest`` historically
        was) should not have to pass a source filter to get the old behaviour.

        Imports ``learning.failures`` locally rather than at module level:
        ``failures.py`` imports ``NON_LEARNABLE_CATEGORIES`` and
        ``_is_infra_finding`` FROM this module, so a top-level import here
        would be circular. By the time this method runs both modules have
        already finished importing.
        """
        from .failures import load_failure_records

        records = await load_failure_records(
            self.store, project=project, limit=limit, note=note)
        # Same conservative exclusion as the supervisor harvest, and the same
        # reason: a record with no `repo_path` cannot be scoped to a
        # repository, and a proposal written with `project=None` is a GLOBAL
        # rule injected into every project.
        unscoped = sum(1 for r in records if not is_project_scoped(r))
        if unscoped and note is not None:
            note(f"{unscoped} failure signal(s) skipped: their task has no "
                 "repo_path, and a repo-less lesson would become a rule in "
                 "every project")
        clusters = cluster_corrections(records, min_occurrences=min_occurrences)
        written: list[str] = []
        for cluster in clusters:
            key = correction_dedupe_key(cluster)
            if await self.store.memory_dedupe_key_exists(key):
                if note is not None:
                    note(f"recurring {cluster.source} signal ({cluster.count}x), "
                         f"deduped to {key}")
                continue
            mem_id = await self.propose_from_corrections(
                cluster, distill=distill, note=note)
            if mem_id:
                written.append(mem_id)
        return written

    # --------------------------- confirm / list ---------------------------- #

    async def pending(self) -> list[dict[str, Any]]:
        """Proposals awaiting human confirmation.

        The `source` filter here IS the visibility contract, which is why it
        reads `SOURCE_PROPOSED` and not a fifth copy of the string literal —
        `Store.add_memory` refuses any other value on an unconfirmed row
        against the same symbol, so this query and that guard cannot drift.
        """
        return await self.store.list_memories(
            confirmed=False, source=SOURCE_PROPOSED)

    async def active(
        self, *, include_paused: bool = False, include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """The confirmed, active rule/skill set later tasks consult.

        ``include_paused`` defaults OFF, matching `Store.list_memories`: a
        paused row must never reach a prompt via this call — though note
        that injection itself (`orchestrator.py:_load_active_memories`)
        reads `Store.list_memories` DIRECTLY, not through this method, so
        this default protects this method's OTHER two callers instead: `nh
        learnings`'s count/stale reports (`cli/commands.py`), which must
        keep excluding a paused row from "active" the same way the prompt
        does. D3 (2026-09-01 review deferral): the Second-brain UI's list
        needs the opposite — a paused row must stay VISIBLE in the panel (a
        `Paused` chip, so Restore is discoverable) even though it is
        invisible to injection — so the one caller that renders the UI list
        (`GET /api/learnings?active=true&include_paused=true`) passes
        ``True`` here explicitly rather than this method silently deciding
        for it.

        ``include_archived`` (D3.2 fix-review, 2026-09-01): the Second-brain
        UI's Delete action archives, never really deletes
        (`LearningQueue.delete`) — a caller that never asks for archived rows
        back makes that recoverability theoretical. The same UI passes
        ``True`` here (mirroring the Rules/Skills `MemoryList` panel's own
        `fetchFn({includeArchived: true})` convention) so the archived rows
        are on hand for its own archived-count footer to reveal, same as
        `include_paused` above — nothing else calls this with either flag on."""
        return await self.store.list_memories(
            confirmed=True, include_paused=include_paused,
            include_archived=include_archived)

    async def stale(self, *, days: int) -> list[dict[str, Any]]:
        """Confirmed rules not injected into a prompt in *days* — a report, and
        only a report. See `Store.stale_memories`: nothing here archives or
        deletes a confirmed row."""
        return await self.store.stale_memories(days=days)

    async def confirm(self, mem_id: str) -> bool:
        """Promote a proposal into the active set, then supersede its oldest
        near-duplicate ACTIVE row, if any exist (AC3). Return value is
        UNCHANGED from before this feature (True/False on the confirm itself)
        so every API/CLI caller's contract holds — supersede is a side
        effect, never surfaced through this return.

        Wrapped so a supersede failure can never undo the confirm: the human
        asked for THIS row to become active, and that must land even if the
        bookkeeping for what it replaces does not.
        """
        confirmed = await self.store.confirm_memory(mem_id)
        if not confirmed:
            return confirmed
        # Audit is best-effort (`_audit`) — never undo a real confirm over a
        # failed audit write, same reasoning as the supersede-on-confirm
        # guard right below.
        await self._audit(mem_id, "confirm", detail={"confirmed_by": "human"})
        try:
            from .retire import find_superseded
            row = await self.store.find_memory(mem_id)
            if row is not None:
                dupes = await find_superseded(self.store, row)
                if dupes:
                    # Oldest match only — bounded, one supersede per confirm,
                    # not a cascade across every near-duplicate found.
                    dupes.sort(key=lambda r: r.get("created_at") or "")
                    oldest = dupes[0]
                    await self.store.supersede_memory(
                        oldest["id"], mem_id,
                        reason="superseded by a newly confirmed near-duplicate",
                    )
        except Exception:  # noqa: BLE001 — never undo a real confirm
            log.warning("supersede-on-confirm failed for %s", mem_id,
                       exc_info=True)
        return confirmed

    async def retire_candidates(
        self, *, days: int = 90,
    ) -> list[dict[str, Any]]:
        """SUGGEST-only report of stale active rules — the `retire?` surface.
        Never archives; see `learning/retire.py:retirement_candidates`."""
        from .retire import retirement_candidates
        return await retirement_candidates(self.store, days=days)

    async def _audit(
        self, mem_id: str, event: str, *, detail: dict[str, Any] | None = None,
    ) -> None:
        """The ONE place every D3 transition writes its `learning_events`
        row — best-effort, always: a failed audit write must never turn an
        already-COMPLETED transition (the row is already paused/archived/
        confirmed/activated) into something that reads as a failure to the
        caller, and must never abort a caller that is mid-loop over several
        rows (`auto_activate`, `sweep_auto_activated`). `confirm()` proved
        this pattern first; every other transition method uses THIS helper
        rather than its own copy, so "the audit write is best-effort" is one
        guarantee instead of a claim each method has to keep re-earning."""
        try:
            await self.store.record_learning_event(mem_id, event, detail=detail)
        except Exception:  # noqa: BLE001
            log.warning("learning_events write failed for %s %s", event,
                        mem_id, exc_info=True)

    async def retire(self, mem_id: str) -> bool:
        """The human's explicit yes to a retirement suggestion (AC2's only
        door). Verifies the row is confirmed (retirement is for ACTIVE
        rules — pending has `reject` for that job), then archives it exactly
        like any other archive: reversible, no `superseded_by` (this is not a
        duplicate collapsing onto a survivor, it is one rule going quiet).

        NEVER called by any scheduled job — this stays true after D3:
        `RetirementSweepJob` calls `sweep_unconfirmed` and (D3, auto-activated
        rows only) `sweep_auto_activated`, both in `learning/retire.py`,
        never this method. Dismissal of a suggestion writes nothing here or
        anywhere; it is purely a client-side "not now"."""
        row = await self.store.find_memory(mem_id)
        if row is None or not row.get("confirmed"):
            return False
        ok = await self.store.archive_memory(
            mem_id, "retired at the human gate (unused >90d)")
        if ok:
            await self._audit(mem_id, "retire")
        return ok

    async def reject(self, mem_id: str) -> bool:
        """A human's "no".

        Proposals from a BATCH-REGENERATED producer (`ARCHIVE_ON_REJECT`:
        supervisor, history, curator) are ARCHIVED, not deleted. Every other
        origin keeps the delete this method has always done — B1's semantics
        are unchanged, and the asymmetry is deliberate rather than an
        oversight, so it is written down here.

        "EVERY OTHER ORIGIN" INCLUDES NULL, AND NULL IS THE COMMON CASE.
        `propose_from_outcome` does not pass `origin`, so an outcome-derived
        proposal is stored with NULL, which is not in the frozenset and takes
        the delete branch. That is the whole of the live data, not an edge:
        measured 2026-08-07 against a copy of the operator's database, 329 of
        329 pending proposals and 402 of 402 rows overall carry NULL, and the
        five named origins have produced none. Delete is the right verb for it
        by the same criterion the list uses — the only production caller is
        `orchestrator._propose_learning`, one call per terminal task outcome,
        and nothing re-reads finished tasks to re-propose, so a deleted lesson
        returns only when another task genuinely ends the same way. It is
        pinned behaviourally in tests/test_learning.py alongside the five.

        S1 widened that set from the single supervisor origin to three. It is
        not a new rule: `history` and `curator` proposals only became
        REACHABLE by this method when S1 stopped them writing an unqueueable
        `source`, and both re-read their whole input on every run, so the
        argument below applied to them from the moment they became rejectable.

        WHY THEY DIFFER. Delete removes the row AND the dedupe key it carries
        in ``file_path``. For B1 that is harmless: `propose_from_review` is
        driven by a FAIL round, so a deleted proposal only returns if the
        reviewer raises that finding AGAIN — which is new evidence, and worth
        re-asking about. B2 is driven by a BATCH that re-reads the whole
        correction history on every run, so for it a delete is a treadmill: the
        next `nh learnings --harvest` finds the same cluster, pays a utility
        call to re-distil it, and re-queues the exact lesson the human just
        rejected. The queue's two "no" verbs would then behave oppositely —
        archive sticks, reject does not — and the idempotence this feature
        claims would be false on any queue anyone had actually triaged.

        Archiving keeps the row (``confirmed=0``, ``archived=1``), so it leaves
        `pending()` exactly as a delete did, and `memory_dedupe_key_exists`
        still sees its key. That is what makes the "no" stick.

        D3 (2026-08-31 operator directive): the table above was built for a
        PENDING proposal's queue exit, and it still governs one — but with
        auto-activation on, the common row `reject()` is called on is
        already CONFIRMED (auto- or human-activated), and neither verb in
        that table is right for one: deleting an active rule outright is not
        "the human said no to a proposal", and the per-origin archive/delete
        split was never about an active row's lifecycle at all. A confirmed
        row therefore does not reach the table below; it is PAUSED instead
        (see ``pause()``) — recoverable, never injected, exactly the "no"
        this method's callers actually want for the Second-brain UI's
        Pause/Reject action. This is "reject aliases pause" from the D3
        design, and it changes nothing for the unconfirmed case every
        existing caller and test below exercises.
        """
        row = await self.store.find_memory(mem_id)
        if row is None:
            return False
        if row.get("confirmed"):
            return await self.pause(mem_id)
        if row.get("origin") in ARCHIVE_ON_REJECT:
            return await self.store.archive_memory(
                row["id"], "rejected at the human confirm gate")
        return await self.store.delete_memory(mem_id)

    # --------------------------- D3: pause / delete ------------------------- #

    async def pause(self, mem_id: str) -> bool:
        """D3 (2026-08-31 operator directive): PAUSE a learning. The row
        stays (recoverable — the curator's never-deletes invariant extends
        here), ``paused=1``, and it is never injected again
        (`Store.list_memories`'s default excludes a paused row, so
        `Orchestrator._load_active_memories` respects it with no extra
        filtering of its own). Works on any row regardless of confirmed
        status. Records a ``learning_events`` audit row."""
        ok = await self.store.set_paused(mem_id, True)
        if ok:
            await self._audit(mem_id, "pause")
        return ok

    async def unpause(self, mem_id: str) -> bool:
        """The reverse of `pause` — the Second-brain UI's Restore action on a
        paused (not archived) row."""
        ok = await self.store.set_paused(mem_id, False)
        if ok:
            await self._audit(mem_id, "unpause")
        return ok

    async def delete(self, mem_id: str) -> bool:
        """D3: DELETE, from the Second-brain UI's perspective. Archives the
        row — never a real ``DELETE FROM`` — mirroring `curator.py`'s
        never-deletes invariant; recoverable via `restore`. Distinct from
        the legacy `reject()`'s per-origin archive/delete dispatch: this is
        the explicit, always-archive verb the new UI's Delete button uses,
        on a row of any status. Records a ``learning_events`` audit row."""
        ok = await self.store.archive_memory(
            mem_id, "deleted via the Memories UI")
        if ok:
            await self._audit(mem_id, "delete")
        return ok

    # --------------------------- D3: auto-activation ------------------------ #

    async def _auto_activation_screen(self, m: dict[str, Any]) -> str | None:
        """None when *m* passes every D3 auto-activation screen (dedupe ->
        PII -> provenance -> term, in that order); else the name of the one
        that failed, for the audit event and the archive reason.

        - **dedupe**: a near-duplicate ACTIVE row already covers this lesson
          (`curator.dedupe_key`, scoped exactly like `retire.find_superseded`)
          — auto-activating it would add a redundant active row nobody asked
          for. Includes PAUSED rows (`include_paused=True`): a paused lesson
          is still the same lesson, and excluding it here would let a
          re-harvested near-duplicate silently undo a pause.
        - **pii**: title/content/evidence carries personal data. Defense in
          depth: every proposer already gates this before writing
          (`learning/pii.py`), but a row can reach the queue by a path that
          does not (a board-added or `nh reply`-mined row).
        - **provenance**: no `origin` AND no `evidence.task_id` — nothing
          says where this proposal came from, so it cannot auto-activate
          unattended.
        - **term**: title/content carries a banned vendor term — the same
          matcher `Orchestrator._screen_memories_for_terms` uses on the
          injection side (`eval.vendor_terms.find_banned_terms`).
        """
        from .curator import dedupe_key as near_duplicate_key
        from .pii import contains_pii

        evidence = m.get("evidence")
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except (ValueError, TypeError):
                evidence = None
        if not isinstance(evidence, dict):
            evidence = None

        key = near_duplicate_key(m)
        my_scope, my_project = m.get("project_scope"), m.get("project")
        # `include_paused=True`: a PAUSED row is still an existing lesson, not
        # a gone one — `pause()` never deletes or archives it. Without this,
        # pausing a rule and then re-harvesting a near-duplicate would pass
        # dedupe (the paused row is invisible to the default query) and
        # auto-activate the exact lesson the operator just silenced — the
        # same treadmill `reject()`'s docstring names for a batch producer
        # that re-reads its whole input on every run.
        for row in await self.store.list_memories(
                confirmed=True, include_paused=True):
            if near_duplicate_key(row) != key:
                continue
            row_scope, row_project = row.get("project_scope"), row.get("project")
            if my_scope or row_scope:
                if my_scope == row_scope:
                    return "dedupe"
            elif my_project == row_project:
                return "dedupe"

        if contains_pii(str(m.get("title") or ""), str(m.get("content") or ""),
                         *_evidence_strings(evidence)) is not None:
            return "pii"

        if not m.get("origin") and not (evidence and evidence.get("task_id")):
            return "provenance"

        from ..eval.vendor_terms import find_banned_terms
        text = f"{m.get('title', '')}\n{m.get('content', '')}"
        try:
            hits = find_banned_terms(text)
        except Exception:  # noqa: BLE001 — a screen that errors must not block
            hits = []       # a proposal; fall through to activating it
        if hits:
            return "term"
        return None

    async def auto_activate(self, *, cap: int = 10) -> AutoActivateReport:
        """D3 (2026-08-31 operator directive, overriding this module's prior
        "a human confirms every learning" contract — see `learning/
        curator.py`'s module docstring for the same reversal stated there):
        promote every PENDING proposal that passes
        `_auto_activation_screen` straight to the active set
        (``confirmed=1``, ``source="auto"``), capped at *cap* proposals per
        rolling 24h window (`Store.count_auto_activated_since`) — the
        compensating control for the flipped default, so no single tick, or
        run of ticks inside one day, can flood the active rule set
        unattended.

        A row that FAILS a screen is ARCHIVED immediately, never left
        pending — "low-quality or screen-failing proposals are archived,
        not queued" (D3 design); there is no human queue left in the UI to
        hold it. Oldest-first, so the cap is applied in a stable,
        deterministic order across repeated ticks.

        Every activation and every screen-failing archive writes a
        ``learning_events`` row (`Store.record_learning_event`) — the D3
        audit trail. Callers: `core.scheduler.HarvestJob`, gated on
        ``config learning.auto_manage`` (the kill switch — see that job's
        docstring).
        """
        report = AutoActivateReport()
        pending = await self.pending()
        if not pending:
            return report
        already = await self.store.count_auto_activated_since(hours=24)
        budget = max(0, cap - already)
        ordered = sorted(pending, key=lambda m: str(m.get("created_at") or ""))
        for m in ordered:
            reason = await self._auto_activation_screen(m)
            if reason is not None:
                if await self.store.archive_memory(
                        m["id"], f"auto-activation screen failed: {reason}"):
                    report.archived.append(m["id"])
                    await self._audit(
                        m["id"], "auto_archive", detail={"reason": reason})
                continue
            if budget <= 0:
                report.cap_hit = True
                continue  # stays pending — inert until the cap has room again
            if await self.store.activate_memory_auto(m["id"]):
                report.activated.append(m["id"])
                budget -= 1
                await self._audit(
                    m["id"], "activate",
                    detail={"origin": m.get("origin"), "source": "auto"})
        return report
