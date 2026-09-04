"""Pydantic response models for the no_human board API."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, model_validator

from ..blockers.taxonomy import Blocker
from ..core.cost import attempt_cost, attempts_cost
from ..core.db import USAGE_ROLES, usage_columns_for
from ..core.metrics import cache_read_share
from ..core.pricing import FALLBACK_PRICE_NAME
from ..core.task import Task
from .title_short import title_short


def _json_field(v: Any) -> Any:
    """Parse a column that may be a JSON string or already-decoded value.

    Shared by every AttemptOut-shaped model so ``review_checklist`` /
    ``verifier_results`` / ``test_results`` are decoded identically wherever
    they are surfaced — the full detail payload (`AttemptDetailsOut`, P1)
    used to have its own inline copy, and a divergence there would decode
    the same column two different ways depending which endpoint served it.
    """
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, ValueError):
            pass
    return v


class AttemptOut(BaseModel):
    id: str
    attempt_number: int
    branch_name: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    review_passed: int | None = None
    # review_checklist / verifier_results / test_results are NOT on this
    # model (P1, running-task page slow-open): they are multi-KB JSON blobs
    # per attempt, observed on live tasks, and serializing them into every
    # `GET /api/tasks/{id}` response is exactly the heavy-payload cost this
    # endpoint was measured to carry. Fetch them lazily, per attempt, from
    # `GET /api/tasks/{id}/attempts/{attempt_number}/details`
    # (`AttemptDetailsOut` below) instead — `web/src/api.js`'s `fetchTask`
    # is the one call site that does this today, so the drawer never notices
    # the split.
    ci_pipeline_id: str | None = None
    ci_pipeline_url: str | None = None
    ci_status: str | None = None
    failure_reason: str | None = None
    # The checkpoint this attempt could not resume from (see db.py). Explicitly
    # part of the wire shape: the drawer is where a human asks why an attempt
    # started from scratch, and a field omitted here reads as "never happened".
    resume_checkpoint_lost: str | None = None
    # …and the one it DID branch from. Its sibling above records only failures,
    # so a surface carrying that alone answers "why did this start from
    # scratch?" and can never answer "did resuming work?" — which is the
    # question the crash-requeue path exists to move, and it had no reader
    # outside SQL.
    resume_checkpoint: str | None = None
    turns_used: int | None = None
    tokens_used: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    # The earliest signal an attempt is heading for budget exhaustion — a
    # per-ATTEMPT ratio, distinct from metrics.py's fleet-wide
    # cache_economics['creation_share']. Computed by the one function that
    # owns this arithmetic (core.metrics.cache_read_share); this field never
    # derives it locally.
    cache_read_share: float | None = None
    # USD, computed server-side by core.cost.attempt_cost from core.pricing's
    # per-model table — the ONE place this attempt's dollar figure is priced.
    # web/src/cost.js used to compute this itself at one hardcoded Anthropic
    # rate, which was wrong for every Codex/OpenAI attempt; the board now only
    # formats this number, never derives it. cost_model names what priced it:
    # the recorded model id, or pricing.FALLBACK_PRICE_NAME when nothing
    # priced could be resolved, or "mixed" when this attempt's roles used
    # different models.
    cost_usd: float = 0.0
    cost_model: str = FALLBACK_PRICE_NAME
    status: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AttemptOut":
        cost_usd, cost_model = attempt_cost(row)
        return cls(
            id=row.get("id", ""),
            attempt_number=row.get("attempt_number", 0),
            branch_name=row.get("branch_name"),
            commit_sha=row.get("commit_sha"),
            pr_url=row.get("pr_url"),
            review_passed=row.get("review_passed"),
            ci_pipeline_id=row.get("ci_pipeline_id"),
            ci_pipeline_url=row.get("ci_pipeline_url"),
            ci_status=row.get("ci_status"),
            failure_reason=row.get("failure_reason"),
            resume_checkpoint_lost=row.get("resume_checkpoint_lost"),
            resume_checkpoint=row.get("resume_checkpoint"),
            turns_used=row.get("turns_used"),
            tokens_used=row.get("tokens_used"),
            cache_read_tokens=row.get("cache_read_tokens"),
            cache_creation_tokens=row.get("cache_creation_tokens"),
            cache_read_share=cache_read_share(
                row.get("cache_read_tokens"), row.get("cache_creation_tokens")),
            cost_usd=cost_usd,
            cost_model=cost_model,
            status=row.get("status"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
        )


class AttemptDetailsOut(BaseModel):
    """The three heavy per-attempt blobs `AttemptOut` no longer carries
    (P1, running-task page slow-open) — served on demand from
    `GET /api/tasks/{task_id}/attempts/{attempt_number}/details` rather than
    inline on every `GET /api/tasks/{id}`. Same decode as `AttemptOut` used
    to do inline (`_json_field`), so a client that merges this back onto an
    `AttemptOut` sees byte-identical shapes to before the split.
    """
    attempt_number: int
    review_checklist: dict | None = None
    # Every deterministic verifier verdict from this attempt's gate round
    # (review/verifiers.py's VerifierResult.as_dict(), via
    # Orchestrator._run_review) — a JSON list, or None when no verifiers ran.
    verifier_results: list | None = None
    test_results: dict | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AttemptDetailsOut":
        return cls(
            attempt_number=row.get("attempt_number", 0),
            review_checklist=_json_field(row.get("review_checklist")),
            verifier_results=_json_field(row.get("verifier_results")),
            test_results=_json_field(row.get("test_results")),
        )


def _aux_totals(attempts: list[dict] | None) -> tuple[int | None, int | None, int | None]:
    """Non-coder, non-reviewer burn summed per bucket across attempts.

    Shared by TaskSummaryOut (board card) and TaskOut (drawer) so the two
    surfaces cannot disagree: the drawer omitted these entirely and
    web/src/cost.js read undefined→0, under-reporting the run by the whole
    planner+utility tier on the surface where a human approves.

    The roles come from ``db.USAGE_ROLES`` (everything but the coder, whose
    columns the caller already reports, and the reviewer, which has its own
    keys) rather than from a literal pair. Naming them here is how the board
    would have kept showing a planner+utility total on the day the supervisor
    and distill roles landed — silently short by two roles on the surface a
    human approves spend from.
    """
    if not attempts:
        return None, None, None
    aux_tiers = [t for t in USAGE_ROLES if t not in ("", "review_")]

    def _auxsum(idx: int) -> int | None:
        keys = [usage_columns_for(t)[idx] for t in aux_tiers]
        vals = [sum(a.get(k) or 0 for k in keys) for a in attempts]
        return sum(vals) if any(v > 0 for v in vals) else None

    return (_auxsum(0), _auxsum(1), _auxsum(2))


def _operator_cancelled(task: Task) -> bool:
    """True when this task's FAILED status came from an operator cancel, not a
    capability failure — boardLanes.js `isRealFailure`'s `task.cancelled`.

    Shared by TaskOut (drawer) and TaskSummaryOut (board card / list) so the
    list and detail endpoints can never disagree about the same task: before
    this helper existed only the summary computed it inline, so the drawer
    for a cancelled task had no `cancelled` field at all.
    """
    return bool((task.context or {}).get("cancel_reason"))


def merge_ready_for(task: Task, attempts: list[dict] | None) -> bool | None:
    """The merge-ready policy verdict (core/merge_policy.py) for `task`'s
    CURRENT head — `task.context.merge_policy[<latest attempt's commit_sha>]
    .ready`, keyed the same way `_finalize` persists it and `verifier_results`
    already is. None when there is no commit yet, or no verdict was ever
    computed/persisted for that exact sha (a verdict stamped for an OLDER
    commit must not read as ready for this one — the sha key is what makes a
    stale verdict read as absent, because nothing re-evaluates it).

    Extracted verbatim from `TaskSummaryOut.from_task` so `nh status`'s
    `merge-ready: N` count (cli/commands.py) and the board card (this class)
    can never disagree about which tasks are ready. ADVISORY ONLY: nothing
    reads this to merge anything — `nh approve`, the only merge path, decides
    from its own independent-reviewer PASS check, not this field.
    """
    merge_ready = None
    if attempts:
        for a in reversed(attempts):
            sha = a.get("commit_sha")
            if sha:
                mp = ((task.context or {}).get("merge_policy") or {}).get(sha)
                if isinstance(mp, dict) and "ready" in mp:
                    merge_ready = bool(mp.get("ready"))
                break
    return merge_ready


class PhaseOut(BaseModel):
    """One `task_phases` row (D1.1). `seconds` = ended_at − started_at, and for
    the still-open phase (ended_at is null) it runs to now."""
    attempt: int
    phase: str
    started_at: str
    ended_at: str | None = None
    outcome: str | None = None
    reason: str | None = None
    seconds: float

    @classmethod
    def from_row(cls, row: dict) -> "PhaseOut":
        from datetime import datetime, timezone
        start = datetime.fromisoformat(row["started_at"])
        end = (datetime.fromisoformat(row["ended_at"]) if row.get("ended_at")
               else datetime.now(timezone.utc))
        return cls(
            attempt=row["attempt"], phase=row["phase"],
            started_at=row["started_at"], ended_at=row.get("ended_at"),
            outcome=row.get("outcome"), reason=row.get("reason") or None,
            seconds=(end - start).total_seconds(),
        )


class BudgetOut(BaseModel):
    """The lifetime COST-WEIGHTED budget the BUDGET_EXHAUSTED gate kills on —
    the #1 real-failure class, made visible before it fires.

    `used`/`cap`/`remaining` are the EXACT numbers
    `Orchestrator._check_lifetime_budget` compares, not a second estimate:
    `used = pricing.weighted_tokens(**store.lifetime_usage_by_class(...)[1])`
    (the INCLUDED classes only — infra/mechanical/dead-interrupted spend is
    excluded from the cap, exactly as the gate excludes it), and `cap =
    Orchestrator._stored_token_cap(task.config, "lifetime_tokens", <bounds
    default>)` — the same per-task-overridable, unit-guarded cap. Populated by
    the detail endpoint (`from_task` has no store), like `phases`. The cap is
    config-driven (`bounds.lifetime_tokens`, default 4M) and per-task
    overridable, so it is read live, never hard-coded here.
    """
    used: int
    cap: int
    remaining: int


class TaskOut(BaseModel):
    id: str
    # SCRUM-16: the scheduler has this task actively in-flight right now —
    # same contract as TaskSummaryOut.claimed (SCRUM-15); set only by the
    # detail endpoint, which is the one path with a scheduler in reach.
    claimed: bool = False
    external_id: str | None = None
    source: str
    title: str
    description: str | None = None
    status: str
    kind: str = "feature"
    priority: str | None = None
    acceptance_criteria: list[str] = []
    repo_path: str | None = None
    blocker: dict | None = None
    context: dict | None = None
    parent_id: str | None = None
    # Sibling link (Task 7): the task this one follows up on, if any — NOT the
    # compound-child relation `parent_id` models. See core/task.py's Task.follows_id.
    follows_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    attempts: list[AttemptOut] = []
    # Per-task cost meter: the three axes, surfaced together. One number cannot
    # answer "what did this cost" — the axes below are priced differently and a
    # surface showing only one of them misreports the bill.
    total_tokens: int | None = None
    # Cache-read is 90%+ of real burn (C1); a meter showing tokens_used alone
    # under-reported a 33M-token task as "121.5k tok".
    total_cache_read: int | None = None
    # Cache-CREATION is full-price fresh work, and it was invisible to every cost surface:
    # a per-task cost priced two of the three buckets, so the task rows summed to less than
    # the lifetime figure on the same page.
    total_cache_creation: int | None = None
    # The reviewer's burn on this task — the gate is not free, and pricing only the coder
    # under-reported every task by the whole adversarial review.
    total_review_tokens: int | None = None
    total_review_cache_creation: int | None = None
    total_review_cache_read: int | None = None
    # The planner + utility tiers' burn. TaskSummaryOut has carried these since
    # B2 #5/#6; TaskOut did not, so the drawer's nine-bucket cost silently
    # dropped them (undefined→0 in web/src/cost.js) and read ~10% lower than
    # the board card for the same task.
    total_aux_tokens: int | None = None
    total_aux_cache_read: int | None = None
    total_aux_cache_creation: int | None = None
    wall_seconds: float | None = None
    attempt_count: int = 0
    #: Why this task failed, taken from its last attempt that recorded a reason.
    #:
    #: `failure_reason` is a column on ATTEMPTS, never on tasks — so the drawer's
    #: "Why it failed" banner (`web/src/SlideOver.jsx`, gated on
    #: `task.failure_reason`) could never render: the field was not in the
    #: payload. `web/e2e/failure-reason.mjs` passes anyway because it builds its
    #: own task object with the field already set, so it pins the COMPONENT's
    #: contract and never exercises the API that has to satisfy it.
    #:
    #: Not populated for an operator cancel — those are stored as `failed` plus a
    #: `cancel_reason` (`cli/commands.py`, `api/app.py`), and the last attempt's
    #: reason would explain a stop nobody asked about under a heading that says
    #: the task failed.
    failure_reason: str | None = None
    # Mirrors TaskSummaryOut.escalation_attempts/escalation_tokens — the
    # `failure_reason` comment above records exactly the bug caused by a
    # component-only field, so this is drawer parity, not new surface.
    escalation_attempts: int | None = None
    escalation_tokens: int | None = None
    #: Mirrors TaskSummaryOut.cancelled — same `_operator_cancelled` predicate,
    #: so the drawer (this model) and the list/board card can never disagree
    #: about whether the same task's FAILED status was an operator cancel.
    cancelled: bool = False
    #: The LAST attempt's coder report, in full — the surface the PR body's
    #: 4000-char `Orchestrator._SUMMARY_TRUNCATED_MARKER` points a reader at.
    #: Filtered/scrubbed by `report_surface.render_full_report` (the same
    #: drop-marker filter and outbound scrub the PR summary passes through),
    #: but NOT capped: this is where the cut tail is meant to be read. `None`
    #: when no attempt on this task has recorded a report yet (predates this
    #: column, or the coder session produced no text).
    full_report: str | None = None
    #: Per-phase timeline (D1.1 rows). Populated by the detail endpoint from
    #: `store.phases_for` — `from_task` has no store, so it leaves these empty
    #: and the endpoint fills them. Empty until the orchestrator writes rows
    #: (D1.2): every real task reads `phases: []`, `active_seconds: null` today.
    phases: list[PhaseOut] = []
    #: Σ of phase-row durations (`store.active_seconds`); the "ran" time, which
    #: parked time never enters. `null` when no phase rows exist yet.
    active_seconds: float | None = None
    #: The lifetime cost-weighted budget the BUDGET_EXHAUSTED gate enforces —
    #: how close this task is to being killed (used/cap/remaining). Populated by
    #: the detail endpoint (`from_task` has no store); `null` there. See
    #: `BudgetOut`. Frontend note: `web/src/SlideOver.jsx`'s `sys-summary` block
    #: (near the "Attempt: #N" item, ~line 1658) is the obvious place to add a
    #: "used/cap · N% of budget" chip from `task.budget`; no UI is built here.
    budget: BudgetOut | None = None
    # USD, summed across every attempt by core.cost.attempts_cost — the same
    # per-model pricing AttemptOut.cost_usd uses, never a local computation.
    # `None` means "no attempts yet" (distinct from "attempts spent $0"), the
    # same distinction every total_* field above already makes.
    cost_usd: float | None = None
    cost_model: str | None = None

    @classmethod
    def from_task(cls, task: Task, attempts: list[dict]) -> "TaskOut":
        toks = [a.get("tokens_used") or 0 for a in (attempts or [])]
        total_tokens = sum(toks) if any(t > 0 for t in toks) else None
        cread = [a.get("cache_read_tokens") or 0 for a in (attempts or [])]
        total_cache_read = sum(cread) if any(c > 0 for c in cread) else None
        ccre = [a.get("cache_creation_tokens") or 0 for a in (attempts or [])]
        total_cache_creation = sum(ccre) if any(c > 0 for c in ccre) else None
        def _sum(key: str) -> int | None:
            vals = [a.get(key) or 0 for a in (attempts or [])]
            return sum(vals) if any(v > 0 for v in vals) else None
        total_review_tokens = _sum("review_tokens_used")
        total_review_cache_creation = _sum("review_cache_creation_tokens")
        total_review_cache_read = _sum("review_cache_read_tokens")
        total_aux_tokens, total_aux_cache_read, total_aux_cache_creation = _aux_totals(attempts)
        cost_usd, cost_model = attempts_cost(attempts)
        escalation_attempts = None
        escalation_tokens = None
        if task.blocker and isinstance(task.blocker, dict):
            _lat = task.blocker.get("escalation_latency") or {}
            escalation_attempts = _lat.get("attempts_before_escalation")
            escalation_tokens = _lat.get("tokens_before_escalation")
        # Lazy import: `report_surface` pulls in `orchestrator`, and `api` is
        # loaded well before any task runs — a top-level import here would
        # make importing `api.models` drag in the whole orchestrator graph.
        from ..core.report_surface import render_full_report
        full_report = None
        for a in reversed(attempts or []):
            raw = a.get("full_final_text")
            if raw and str(raw).strip():
                full_report = render_full_report(str(raw))
                break
        return cls(
            id=task.id,
            external_id=task.external_id,
            source=task.source,
            title=task.title,
            description=task.description,
            status=task.status.value,
            kind=task.kind,
            priority=task.priority,
            acceptance_criteria=task.acceptance_criteria,
            repo_path=task.repo_path,
            blocker=task.blocker,
            context=task.context,
            parent_id=task.parent_id,
            follows_id=task.follows_id,
            created_at=task.created_at,
            updated_at=task.updated_at,
            attempts=[AttemptOut.from_row(a) for a in attempts],
            total_tokens=total_tokens,
            total_cache_read=total_cache_read,
            total_cache_creation=total_cache_creation,
            total_review_tokens=total_review_tokens,
            total_review_cache_creation=total_review_cache_creation,
            total_review_cache_read=total_review_cache_read,
            total_aux_tokens=total_aux_tokens,
            total_aux_cache_read=total_aux_cache_read,
            total_aux_cache_creation=total_aux_cache_creation,
            wall_seconds=_wall_seconds(task.created_at, _last_activity(task, attempts)),
            attempt_count=len(attempts) if attempts else 0,
            failure_reason=_failure_reason(task, attempts),
            escalation_attempts=escalation_attempts,
            escalation_tokens=escalation_tokens,
            cancelled=_operator_cancelled(task),
            full_report=full_report,
            cost_usd=cost_usd,
            cost_model=cost_model,
        )


def _failure_reason(task: Task, attempts: list[dict]) -> str | None:
    """The reason to show under "Why it failed", or None to show nothing.

    Reads the LAST attempt that recorded one, ordered by `attempt_number` and
    falling back to list order when that is absent, because a retry's reason is
    the one that explains the task's final state.

    Two cases deliberately return None rather than a plausible-looking string:

    * the task did not fail — a task that failed attempt 1 and passed attempt 2
      is not a failed task, and its old reason under that heading is a lie;
    * an operator CANCEL, which is stored as `failed` plus a `cancel_reason`
      (`cli/commands.py`, `api/app.py`). The last attempt's reason there
      describes work that was interrupted, not a failure anyone diagnosed. The
      same distinction the read sites in `cli/commands.py` and `api/app.py`
      already make, and that `core/metrics.py` was missing.
    """
    if task.status.value != "failed":
        return None
    if (task.context or {}).get("cancel_reason") is not None:
        return None
    ordered = sorted(
        (a for a in (attempts or []) if str(a.get("failure_reason") or "").strip()),
        key=lambda a: a.get("attempt_number") or 0,
    )
    return str(ordered[-1]["failure_reason"]).strip() if ordered else None


def _parse_ts(s: str | None):
    """Parse an ISO timestamp string to an aware datetime, treating a naive
    string (the realistic corruption shape, e.g. '2026-07-26 06:11:56') as
    UTC rather than raising. Returns None on any parse failure."""
    from datetime import datetime, timezone

    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _last_activity(task: "Task", attempts: list[dict] | None) -> str | None:
    """Return the most recent timestamp across task.updated_at and attempt
    timestamps.  Used for the board card's 'last activity' line."""
    candidates = [task.updated_at or ""]
    for a in (attempts or []):
        candidates.append(a.get("completed_at") or "")
        candidates.append(a.get("started_at") or "")
    parsed = [(_parse_ts(c), c) for c in candidates]
    dated = [(dt, c) for dt, c in parsed if dt is not None]
    if dated:
        return max(dated, key=lambda p: p[0])[1] or None
    return max(candidates) or None  # preserve prior behavior when nothing parses


def _wall_seconds(created_at: str | None, last_activity: str | None) -> float | None:
    """Wall-clock seconds a task has been alive (created → last activity).

    The time half of the per-task cost meter (tokens is the other half). Cost is
    best-effort telemetry: any missing or unparseable timestamp returns None
    rather than raising. Naive timestamps (a corrupt row missing tz info) are
    treated as UTC rather than erroring, so one bad row can't 500 the board.
    """
    if not created_at or not last_activity:
        return None
    a, b = _parse_ts(created_at), _parse_ts(last_activity)
    if a is None or b is None:
        return None
    try:
        return max(0.0, (b - a).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _coerce_round_count(value) -> int:
    """Context values come from JSON the watcher wrote, but corruption (or a
    future writer bug) must degrade to 0 — never 500 the whole board."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class TaskSummaryOut(BaseModel):
    id: str
    external_id: str | None = None
    source: str
    title: str
    status: str
    kind: str = "feature"
    priority: str | None = None
    pr_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Richer fields so the board card is useful without clicking through.
    repo_name: str | None = None
    description_short: str | None = None
    title_short: str = ""
    attempt_count: int = 0
    last_turns: int | None = None
    blocker_question: str | None = None
    blocker_category: str | None = None
    blocker_wake_condition: str | None = None
    # {attempts,tokens}_before_escalation from task.blocker["escalation_latency"];
    # None when unmeasured (fail-open telemetry, see orchestrator._raise_blocker)
    # — the board/CLI must render nothing, never a fabricated 0.
    escalation_attempts: int | None = None
    escalation_tokens: int | None = None
    # The human explicitly stopped this parked task (/pause on an already-parked
    # task, or /reply with terminal:true) — durable per wake.py, which already
    # skips these in the wake sweep. Silences "N need you" for the row without
    # moving it out of its lane (see isNeedsYou / actionHint / narrativeFor).
    blocker_human_stopped: bool = False
    last_activity: str | None = None
    backend: str | None = None  # the coder backend THIS task asked for, if any
    total_tokens: int | None = None
    total_cache_read: int | None = None
    total_cache_creation: int | None = None
    # B2 #12: TaskTable priced these but the list endpoint never sent them —
    # undefined→0 silently dropped the whole review gate from every row while
    # the lifetime tile (same page) included it. Rows summed to less than the
    # total, the exact surfaces-disagree class the cost model was rebuilt for.
    total_review_tokens: int | None = None
    total_review_cache_read: int | None = None
    total_review_cache_creation: int | None = None
    # B2 #5/#6 (review #2): planning + utility burn on their own columns —
    # summed here so the task row prices the WHOLE run, not just coder+review.
    total_aux_tokens: int | None = None
    total_aux_cache_read: int | None = None
    total_aux_cache_creation: int | None = None
    wall_seconds: float | None = None  # created → last activity; time half of the cost meter
    parent_id: str | None = None
    # Sibling link (Task 7): see TaskOut.follows_id above — the board card's
    # "Follow up" affordance and the drawer both need this on the summary too.
    follows_id: str | None = None
    has_spec: bool = False
    live_status: str | None = None
    subtask_progress: str | None = None
    # A task an operator cancelled ends in FAILED status but is not a capability
    # failure — set so Stats can keep it out of the success-rate denominator.
    cancelled: bool = False
    # B2 #19: approve() records approved_at but the task STAYS in
    # awaiting_approval until the merge lands, so an approved PR looked
    # identical to an un-reviewed one and kept shouting in "N need you" — a
    # second operator (or the Electron window) would re-review it.
    approved_at: str | None = None
    # Stamped (write-once) by `core/db.py::_write_status` the moment a row
    # leaves awaiting_approval for anything other than done — an escalation,
    # a conflict-round send-back, or a fresh attempt. `approved_at` above is
    # left untouched (audit trail); this is what lets `core/lanes.py::
    # approval_pending` (and its JS twin, `approvalState.js::approvalLive`)
    # tell a LIVE approval from a stale one that has already been overtaken
    # by a later round, so the "approved - merge pending" chip can never
    # again contradict the lane the task is actually sitting in.
    approval_superseded_at: str | None = None
    # SCRUM-15: the scheduler has this task actively in-flight RIGHT NOW —
    # distinct from `status` being one of the "active" values, which merely
    # means it's eligible to run. `from_task` has no scheduler access, so this
    # defaults False; `_board_tasks` (the only path with a scheduler) sets it
    # from `scheduler.inflight`.
    claimed: bool = False
    # SCRUM-42: >0 while the wake watcher is cycling a CONFLICTING PR through
    # bounded rebase send-backs (SCRUM-41's context key, mirrored verbatim).
    # The card renders "resolving merge conflict" from THIS, never by
    # string-matching feedback text. Reset to 0 by the rung on a definite
    # MERGEABLE, so the badge clears itself.
    pr_conflict_rounds: int = 0
    # The configured blockers.max_pr_conflict_rounds bound, surfaced so the
    # badge renders "round N/M" with real data. 0 = unknown (older callers),
    # which the frontend renders as a bare round number.
    max_pr_conflict_rounds: int = 0
    # Board lane, decided by core.lanes.lane_for so the web UI and any other
    # client cannot disagree about it (the JS used to own this outright).
    # `from_task` has no reason to route a summary that is not going to a board,
    # so this defaults to None and `_board_tasks` fills it; the frontend falls
    # back to its own copy of the routing when the field is absent.
    lane: str | None = None
    # The merge-ready policy verdict (core/merge_policy.py) for the task's
    # CURRENT head — `task.context.merge_policy[<latest attempt's commit_sha>]
    # .ready`, keyed the same way `_finalize` persists it and `verifier_results`
    # already is. None when there is no commit yet, or no verdict was ever
    # computed/persisted for that exact sha (a verdict stamped for an OLDER
    # commit must not read as ready for this one — the sha key is what makes
    # a stale verdict read as absent, because nothing re-evaluates it:
    # `nh approve`, the only merge path, never reads this field).
    # ADVISORY ONLY: nothing reads this field to merge anything.
    merge_ready: bool | None = None
    # USD, summed across every attempt by core.cost.attempts_cost — same
    # source and same "None means no attempts yet" contract as TaskOut.cost_usd.
    cost_usd: float | None = None
    cost_model: str | None = None
    # Feature #1's pre-flight hint (core/feasibility.py), same dict the create
    # handler stashes on task.context — {band, tier, offer, done_rate_pct,
    # message}. Read straight off THIS field, on THIS response, so a caller
    # that only sees the create response (the board's create-time toast, P3)
    # need not wait for dispatch and a follow-up GET to see it. None when the
    # create found nothing worth flagging, mirroring `estimate_feasibility`'s
    # own fail-open contract.
    feasibility_hint: dict | None = None

    @classmethod
    def from_task(
        cls,
        task: Task,
        pr_url: str | None = None,
        attempts: list[dict] | None = None,
        max_pr_conflict_rounds: int = 0,
    ) -> "TaskSummaryOut":
        repo_name = task.repo_path.rstrip("/").rsplit("/", 1)[-1] if task.repo_path else None
        desc_short = (task.description or "")[:120] or None
        attempt_count = len(attempts) if attempts else 0
        last_turns = None
        if attempts:
            for a in reversed(attempts):
                if a.get("turns_used"):
                    last_turns = a["turns_used"]
                    break
        blocker_q = None
        blocker_cat = None
        blocker_wake = None
        blocker_stopped = False
        escalation_attempts = None
        escalation_tokens = None
        if task.blocker and isinstance(task.blocker, dict):
            # Legacy rows persisted before the blocker-prose normalisation
            # (2026-09-01, e18192e04) can still hold LIST-shaped question/
            # category/wake_condition on disk — a raw `.get()` lift feeds a
            # list straight into these `str | None` fields below and
            # ValidationErrors the whole board list endpoint. Route through
            # `Blocker.from_dict`, the same coercion every new blocker gets,
            # so a legacy row renders instead of detonating the page.
            _blocker = Blocker.from_dict(task.blocker)
            blocker_q = _blocker.question
            blocker_cat = _blocker.category.value
            blocker_wake = _blocker.wake_condition
            blocker_stopped = bool(task.blocker.get("human_stopped"))
            _lat = task.blocker.get("escalation_latency") or {}
            escalation_attempts = _lat.get("attempts_before_escalation")
            escalation_tokens = _lat.get("tokens_before_escalation")
        # Per-task backend from config dict.
        task_backend = None
        if hasattr(task, "config") and isinstance(task.config, dict):
            task_backend = task.config.get("backend")
        # Sum tokens across all attempts.
        total_tokens = None
        total_cache_read = None
        total_cache_creation = None
        if attempts:
            toks = [a.get("tokens_used") or 0 for a in attempts]
            total_tokens = sum(toks) if any(t > 0 for t in toks) else None
            cread = [a.get("cache_read_tokens") or 0 for a in attempts]
            total_cache_read = sum(cread) if any(c > 0 for c in cread) else None
            ccre = [a.get("cache_creation_tokens") or 0 for a in attempts]
            total_cache_creation = sum(ccre) if any(c > 0 for c in ccre) else None
        total_review_tokens = None
        total_review_cache_read = None
        total_review_cache_creation = None
        if attempts:
            def _rsum(key: str) -> int | None:
                vals = [a.get(key) or 0 for a in attempts]
                return sum(vals) if any(v > 0 for v in vals) else None
            total_review_tokens = _rsum("review_tokens_used")
            total_review_cache_read = _rsum("review_cache_read_tokens")
            total_review_cache_creation = _rsum("review_cache_creation_tokens")
        total_aux_tokens, total_aux_cache_read, total_aux_cache_creation = _aux_totals(attempts)
        merge_ready = merge_ready_for(task, attempts)
        cost_usd, cost_model = attempts_cost(attempts)
        return cls(
            id=task.id,
            external_id=task.external_id,
            source=task.source,
            title=task.title,
            status=task.status.value,
            kind=task.kind,
            priority=task.priority,
            pr_url=pr_url,
            created_at=task.created_at,
            updated_at=task.updated_at,
            repo_name=repo_name,
            description_short=desc_short,
            title_short=title_short(task.title),
            attempt_count=attempt_count,
            last_turns=last_turns,
            blocker_question=blocker_q,
            blocker_category=blocker_cat,
            blocker_wake_condition=blocker_wake,
            blocker_human_stopped=blocker_stopped,
            escalation_attempts=escalation_attempts,
            escalation_tokens=escalation_tokens,
            last_activity=_last_activity(task, attempts),
            backend=task_backend,
            total_tokens=total_tokens,
            total_cache_read=total_cache_read,
            total_cache_creation=total_cache_creation,
            total_review_tokens=total_review_tokens,
            total_review_cache_read=total_review_cache_read,
            total_review_cache_creation=total_review_cache_creation,
            total_aux_tokens=total_aux_tokens,
            total_aux_cache_read=total_aux_cache_read,
            total_aux_cache_creation=total_aux_cache_creation,
            wall_seconds=_wall_seconds(task.created_at, _last_activity(task, attempts)),
            parent_id=task.parent_id,
            follows_id=task.follows_id,
            has_spec=bool((task.context or {}).get("spec")),
            cancelled=_operator_cancelled(task),
            approved_at=(task.context or {}).get("approved_at"),
            approval_superseded_at=(task.context or {}).get("approval_superseded_at"),
            pr_conflict_rounds=_coerce_round_count(
                (task.context or {}).get("pr_conflict_rounds")),
            max_pr_conflict_rounds=max_pr_conflict_rounds,
            merge_ready=merge_ready,
            cost_usd=cost_usd,
            cost_model=cost_model,
            feasibility_hint=(task.context or {}).get("feasibility_hint"),
        )


class CreateTaskRequest(BaseModel):
    title: str
    description: str | None = None
    repo_path: str | None = None
    project_id: str | None = None
    kind: str = "feature"
    priority: str = "medium"
    acceptance_criteria: list[str] = []
    backend: str | None = None  # claude | codex | local; None = worker.backend
    # "board" (typed) or "jira" (Import from Jira) — any other value falls back
    # to "board" server-side. Task.source already models this (intake/jira.py's
    # poller stamps "jira" too); this just lets the web create path pick it.
    source: str = "board"
    # Jira dedup key (SCRUM-32); only honored when source == "jira" — trimmed
    # and length-capped server-side in the create-task handler.
    external_id: str | None = None
    # The branch this task's work should branch FROM (PR-001).
    #
    # Until this existed, a task branched from whatever the primary checkout
    # happened to have checked out (`orchestrator.py`: `base = ctx["base_branch"]
    # or main_repo.current_branch()`), and NO API or GUI field could pin it. The
    # mismatch surfaced only as a non-blocking warning at PR time — after the run
    # was paid for. The documented workaround was "put the checkout on main
    # before filing anything", i.e. a precondition a human has to remember every
    # time, which is not a fix.
    #
    # Left as None, the base is now the PROJECT'S DEFAULT BRANCH — the profile's
    # declared `default_branch`, else the remote's `origin/HEAD`, and only for a
    # repo with neither (no origin: the bench's fixtures, most tests) the
    # checkout's branch. `orchestrator._implicit_base_branch` is the one place
    # that decides it, for the primary repo and for linked repos alike.
    #
    # 🔴 THIS IS HALF OF PR-001, AND THE HALF A HUMAN CANNOT REACH.
    # An earlier version of this comment said "the composer offers it, so a
    # person filing a ticket can see and choose it". It does not — there is no
    # `base_branch` anywhere in `web/src`, an architecture audit proved it with
    # a positive control, and PR-001 is explicit that this is "A GUI fix, not a
    # doc fix". What HAS changed (2026-08-09) is the default that field would
    # have offered: the run-book precondition "put the primary checkout on
    # `main` before filing anything" is retired — the checkout's branch is no
    # longer inherited, so a stale checkout can no longer send a run to a base
    # `gh pr create` refuses ("No commits between <stale> and no-human/<id>").
    # What remains for PR-001: the composer field itself, for the person who
    # wants a base OTHER than the default (a stacked PR onto a colleague's
    # branch), defaulting to the resolved default branch and editable.
    base_branch: str | None = None
    # GAP 1 — the optional human plan-approval gate. Default False keeps every
    # existing caller (bench, `nh repo new`, the composer) on today's
    # unattended path. Ignored for source == "jira": an imported ticket is not
    # a person choosing to sit in front of the run, and the Jira poller (which
    # never reaches this endpoint) must behave identically to an import made
    # through it.
    plan_approval: bool = False
    # Task 7: "Follow up" on a finished task. Sibling link only — the create
    # handler 404s if this doesn't resolve to a real task, mirroring the
    # project_id check just above it in the handler; never Task.parent_id
    # (the compound-child relation the orchestrator still schedules on).
    follows_id: str | None = None
    # The grill/wizard's intake-eval verdict (`EvalResult.as_dict()` shape),
    # produced by `/api/grill/stream`'s D1/D9 evaluator and, until now, only
    # ever streamed to the composer for display — never carried into the
    # created task, so the orchestrator's stored-verdict path (dispatch-time
    # `_act_on_stored_eval`) had no production task to act on. Optional and
    # untyped-through: the server never trusts client-supplied JSON as a real
    # verdict, only rebuilds it leniently via `EvalResult.from_dict`
    # (unknown/missing fields fall back rather than raising). A bare board
    # create (composer without a grill round, `nh` CLI, pollers) omits it and
    # behaves exactly as before.
    eval_result: dict[str, Any] | None = None


class ImportedInfo(BaseModel):
    """SCRUM-18 — the accidental-re-import trap. A tiny board-side lookup
    attached to a browse row when the ticket already has a board task,
    matched by (source, external_id) against the local tasks store. Never the
    full Task shape (models.py's own TaskOut/TaskSummaryOut), no secrets."""
    task_id: str        # the board task id, for binding — never the full task shape
    status: str          # board task status value (e.g. "done", "implementing")
    count: int = 1        # >1 signals duplicate external_ids (data-integrity warning)


class TrackerIssueOut(BaseModel):
    """One row in the Backlog page's ticket list — never the full Task shape
    the adapters' ``normalize`` builds, and never a secret.

    ONE shape for both trackers. The page lists Jira and Linear side by side,
    and the two rows differ in nothing a reader of this list cares about, so a
    second near-identical model would only be a way for them to drift."""
    # Which tracker this row came from: "jira" or "linear". The page needs it
    # to route the detail fetch and to stamp the created task's `source`, and
    # dedupe keys on (source, external_id) — so a Jira NO-1 and a Linear NO-1
    # are two different tickets and must not claim each other's board task.
    tracker: str = "jira"
    key: str
    summary: str
    status: str | None = None
    assignee: str | None = None
    updated: str | None = None
    url: str
    description: str = ""
    # SCRUM-18: set when a board task already exists for this issue's key.
    imported: ImportedInfo | None = None


class SendBackRequest(BaseModel):
    message: str


class ShippedRequest(BaseModel):
    # SCRUM-56: operator testimony, never verified — see the endpoint docstring.
    sha: str
    note: str | None = None


class CancelRequest(BaseModel):
    # Optional: the board's cancel button always posts this (reason may be
    # None when the operator left it blank); `nh` CLI callers and any other
    # no-body POST still work — see cancel_task's default in api/app.py.
    reason: str | None = None


class SplitDraft(BaseModel):
    # One proposed sub-task in a 1-click split. `title` is required; the human
    # may have edited any field in the split-review screen before confirming.
    # `contract` is the split proposer's interface/dependency note (what this
    # sub-task exposes to or expects from the others) — folded into the child's
    # description so the coder keeps it, per the review (it would otherwise drop).
    title: str
    description: str | None = None
    contract: str | None = None
    acceptance_criteria: list[str] = []


class SplitRequest(BaseModel):
    # The human-confirmed sub-tasks to create from an over-scope PENDING task.
    # POST /api/tasks/{id}/split creates each as an independent child (parent_id
    # set for provenance) and cancels the original. A HUMAN api action only —
    # never a blocker-option verb, so the agent can never trigger a split.
    drafts: list[SplitDraft]


class LandedOverrideRequest(BaseModel):
    # Unlike ShippedRequest, `sha` here IS verified — commit_is_ancestor
    # against the task's recorded base_branch — and `justification` is
    # required (non-empty, enforced server-side): see
    # blockers/landed_override.py's approve_landed_override.
    sha: str
    justification: str
    # Required only for a `pending_never_ran` task with no recorded
    # base_branch and no project default (the tool never guesses one — see
    # landed_override.py's module docstring / F1). When given on a task that
    # already has a recorded or default base, it NARROWS the ancestry check
    # to exactly this branch instead of trying the usual candidates.
    base: str | None = None


class SaveIntegrationConfigRequest(BaseModel):
    fields: dict[str, str] = {}


class TelemetryConsentRequest(BaseModel):
    """Body of `PUT /api/telemetry/consent` — persists `telemetry.enabled` to
    config.yaml (the config-level opt-out; the onboarding step + Settings pane
    that once drove this were removed, operator 2026-08-26). `enabled` is the
    ONLY field: the anonymous instance id is minted server-side on first enable
    — never accepted from the browser."""
    enabled: bool


class IntegrationSetupRequest(BaseModel):
    """The onboarding "Connect your tools" step's payload for ONE integration:
    its non-secret settings only (bools, text, string lists).

    `str` is deliberately absent from the value union's scalar side except as
    text — there is no field here that could carry a credential, because the
    server re-checks every key against
    ``integrations.assert_config_safe_field`` before writing. Secrets are
    never posted to this route; the wizard only NAMES the ~/.no_human/.env
    variable they belong in."""
    values: dict[str, bool | str | list[str]] = {}


class ReplyRequest(BaseModel):
    # Optional ONLY because `choose` can carry the answer instead: picking
    # option N answers with N's label, which is exactly what `nh reply
    # --choose N` does. The board's one-click option button posts `{choose}`
    # with no `answer` at all, so a required `answer` rejected every one of
    # those with a 422 — the buttons could not be clicked. An empty body is
    # still 422 (the validator below), so nothing that was rejected before for
    # being answerless is accepted now.
    answer: str | None = None
    # 1-based index into the blocker's options. When set, the chosen option's
    # action is applied — the only path by which one ever runs.
    choose: int | None = None

    @model_validator(mode="after")
    def _answer_or_choose(self) -> "ReplyRequest":
        if self.answer is None and self.choose is None:
            raise ValueError("give an answer or choose an option")
        # A BLANK answer is not an answer. `{"answer": ""}` used to be stored
        # as a real reply: at the plan gate it recorded `state=correcting,
        # correction=""`, which stranded the task in PLANNING with no worker
        # (nothing claimed it) and 409'd every further reply, and it would
        # otherwise be handed to the planner as a binding correction — burning
        # the single re-plan allowance on nothing.
        #
        # Scoped to a FREE-TEXT reply. `{"answer": "", "choose": N}` is what
        # `nh reply --choose` and older board builds post, and there the answer
        # is replaced by option N's label before anything reads it — the field
        # is inert, so rejecting it would break a working call for no gain.
        if self.choose is None and self.answer is not None and not self.answer.strip():
            raise ValueError("answer must not be blank")
        return self


class BoardPayload(BaseModel):
    tasks: list[TaskSummaryOut]


class GrillStepRequest(BaseModel):
    title: str
    description: str | None = None
    repo_path: str | None = None
    project_id: str | None = None
    qa_history: list[dict] = []


class GrillQuestionOut(BaseModel):
    type: str = "question"
    question: str
    suggestions: list[str]
    round: int


class GrillResultOut(BaseModel):
    type: str = "done"
    title: str
    description: str
    acceptance_criteria: list[str]


class ProjectOut(BaseModel):
    id: str
    name: str
    repo_paths: list[str]
    primary_repo: str | None = None
    test_layers: list[dict[str, Any]] = []
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_project(cls, p: Any) -> "ProjectOut":
        layers = []
        raw = getattr(p, "test_layers", "[]")
        if raw:
            try:
                layers = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                pass
        return cls(
            id=p.id, name=p.name, repo_paths=p.repo_paths,
            primary_repo=p.primary_repo, test_layers=layers,
            created_at=p.created_at, updated_at=p.updated_at,
        )


class CreateProjectRequest(BaseModel):
    name: str
    repo_paths: list[str] = []
    primary_repo: str | None = None


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    repo_paths: list[str] | None = None
    primary_repo: str | None = None
    test_layers: list[dict[str, Any]] | None = None
