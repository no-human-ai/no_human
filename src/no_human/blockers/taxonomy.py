"""Blocker taxonomy and routing (PLAN.md Part 22).

Core principle (22, stated up front): *a blocker is never resolved by lowering
the bar.* When stuck, the agent makes verifiable progress, parks with a wake
condition, or escalates with a precise diagnosis — it never weakens a test,
expands scope, edits acceptance criteria, or fakes "done".

This module is pure data + routing logic (no I/O), so it is trivially testable
and the orchestrator stays the only place that touches the DB / git / SDK.

Typed-stop matrix (a PAUSE and a HOLD are mutually exclusive stop shapes):

* **PAUSE** — ``category=USER_PAUSED`` (see `user_pause_blocker`), no
  ``human_stopped``. Written by `_honor_cancel`, `nh task pause`, and
  `POST /pause`'s direct-park branch. The wake sweep's ``max_park`` timeout
  ESCALATES it like any other parked blocker (a forgotten pause should not
  block work indefinitely; 48h is ample time for an intentional one).
  Resumes in ONE step: `nh task resume` / `POST /resume`.
* **HOLD** — ``human_stopped=True`` stamped over any EXISTING blocker
  (`POST /pause`'s hold branch). Never swept by ``max_park``. Resumes by
  releasing the hold only; the task stays parked on its original blocker.
  Refused over an existing PAUSE — see `api.pause_task`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..core.task import TaskStatus

#: Actor strings are rendered through rich's markup path (`[...]` = markup),
#: and are treated as untrusted-adjacent, so only these characters survive.
#: `[`/`]` are NOT in the set, which neutralises markup injection at the source.
_ACTOR_ALLOWED = re.compile(r"[^A-Za-z0-9._:-]")


def process_actor() -> str:
    """A SERVER/PROCESS-derived actor for human landing/approval events, so a
    landing can be attributed when several agent sessions share one git
    identity (fleet task 61c219c8).

    Composed ONLY of process-side facts: this process's pid, plus the
    ``NO_HUMAN_AGENT_SESSION`` mark it was launched with when present (via
    ``current_mark()`` — the same fail-closed read the gate uses; the mark's
    *kind* is the useful discriminator, ``session:<kind>``). It NEVER folds in
    any client- or request-supplied value (no request header, no
    ``x-request-id``) — those are attacker-controllable and must not reach an
    audit actor. The result is sanitised to ``[A-Za-z0-9._:-]`` and truncated,
    so it is safe to render through any rich/markup path.
    """
    from ..agent.session_mark import current_mark

    parts = [f"pid:{os.getpid()}"]
    mark = current_mark()
    if mark is not None:
        parts.append(f"session:{mark}")
    return _ACTOR_ALLOWED.sub("_", ":".join(parts))[:96]


class BlockerCategory(str, Enum):
    """The Part 22.2 taxonomy. Value is the stable string stored on the task."""

    TRANSIENT_INFRA = "TRANSIENT_INFRA"
    QUOTA = "QUOTA"
    DEPENDENCY_WAIT = "DEPENDENCY_WAIT"
    MISSING_ACCESS = "MISSING_ACCESS"
    AMBIGUITY = "AMBIGUITY"
    SCOPE_EXPLOSION = "SCOPE_EXPLOSION"
    IMPOSSIBLE = "IMPOSSIBLE"
    NOVEL_UNKNOWN = "NOVEL_UNKNOWN"
    STAGNATION = "STAGNATION"
    # The task's lifetime budget (attempts or tokens, resumes included) is
    # spent. Raised by the harness, never the agent.
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    # A human deliberately paused the task (`_honor_cancel`, `nh task pause`,
    # `POST /pause`). Written by the harness only, never the agent — see
    # HARNESS_ONLY_CATEGORIES and `report.parse_blocker`'s demotion of an
    # agent-claimed one. The typed-stop matrix in this module's docstring
    # spells out the write/sweep/resume contract.
    USER_PAUSED = "USER_PAUSED"

    @classmethod
    def coerce(cls, value: str | "BlockerCategory") -> "BlockerCategory":
        """Map a raw string (e.g. from the agent) to a category, defaulting to
        NOVEL_UNKNOWN so an unrecognized label escalates rather than crashes."""
        if isinstance(value, cls):
            return value
        key = (value or "").strip().upper().replace("/", "_").replace(" ", "_")
        # accept a few aliases the agent might emit
        aliases = {
            "SPEC_GAP": cls.AMBIGUITY,
            "AMBIGUITY_SPEC_GAP": cls.AMBIGUITY,
            "INVALID": cls.IMPOSSIBLE,
            "IMPOSSIBLE_INVALID": cls.IMPOSSIBLE,
            "INFRA": cls.TRANSIENT_INFRA,
            "RATE_LIMIT": cls.TRANSIENT_INFRA,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError:
            return cls.NOVEL_UNKNOWN


@dataclass(frozen=True)
class Route:
    """How a category is handled: the target state, whether to notify now, and
    whether the wake-watcher should poll it (parked) vs. a human must act."""

    target_status: TaskStatus
    notify_now: bool          # 22.6: "needs you now" vs "parked, silent"
    parked: bool              # watcher polls a parked task; escalations wait on a human
    auto_retry: bool = False  # bounded auto-retry before this routing applies


# Part 22.2 taxonomy → routing, with 22.6 severity baked in.
_ROUTING: dict[BlockerCategory, Route] = {
    # Parked, no action needed — silent until resume or timeout-then-escalate.
    BlockerCategory.TRANSIENT_INFRA: Route(
        TaskStatus.BLOCKED, notify_now=False, parked=True, auto_retry=True),
    BlockerCategory.QUOTA: Route(
        TaskStatus.PAUSED_QUOTA, notify_now=False, parked=True),
    BlockerCategory.DEPENDENCY_WAIT: Route(
        TaskStatus.BLOCKED, notify_now=False, parked=True),
    # Needs you now — only a human can move these forward.
    BlockerCategory.MISSING_ACCESS: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    BlockerCategory.AMBIGUITY: Route(
        TaskStatus.AWAITING_INPUT, notify_now=True, parked=False),
    BlockerCategory.SCOPE_EXPLOSION: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    BlockerCategory.IMPOSSIBLE: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    BlockerCategory.NOVEL_UNKNOWN: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    # Spending more is a human's call, never a retry's.
    BlockerCategory.BUDGET_EXHAUSTED: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    # Stagnation is the one blocker a retry provably cannot clear: it is RAISED
    # because two consecutive attempts made no progress (orchestrator.py, review
    # pass rate flat with a recurring specific failure). Parking needs a wake
    # condition and there is none — nothing external will change — so the only
    # honest route is to escalate with the report. This entry was MISSING, and
    # `Blocker.route` is a bare `_ROUTING[category]`, so every stagnation blocker
    # raised KeyError on the one path the stuck detector exists to serve.
    BlockerCategory.STAGNATION: Route(
        TaskStatus.ESCALATED, notify_now=True, parked=False),
    # A deliberate human pause. Parked and silent like TRANSIENT_INFRA/
    # DEPENDENCY_WAIT — no auto-retry, since nothing should touch the task
    # until a human resumes it — but see `triage`'s explicit branch: the
    # writers record `confidence=0.0` (there is nothing to be confident
    # ABOUT — it isn't a diagnosis), so this entry alone is not enough; the
    # generic low-confidence-forces-escalation override must not apply here.
    BlockerCategory.USER_PAUSED: Route(
        TaskStatus.BLOCKED, notify_now=False, parked=True),
}

#: Categories the harness alone may raise — never accepted from the agent's
#: own report. `report.parse_blocker` demotes a claimed one to NOVEL_UNKNOWN
#: (unclassified, not a false "the agent asked to be paused") and
#: `blocker_prompt_suffix` never advertises them as an option to declare.
HARNESS_ONLY_CATEGORIES = frozenset({BlockerCategory.USER_PAUSED})


#: BUDGET_EXHAUSTED under `budget.exhaustion_terminal` (the default). The task
#: ENDS — no question is asked, because the answer is standing policy: "stop;
#: an exhausted budget means the ticket was wrong, refile it inline-complete
#: and smaller". FAILED rather than a parked state so nothing automatic can
#: revive it: the scheduler's `_CLAIMABLE` is (implementing, pending) and the
#: wake watcher only sweeps blocked/paused_quota/awaiting_input/
#: awaiting_approval, so FAILED is the one terminal state both ignore.
#: `notify_now=False` because a "needs you now" ping is exactly the
#: interruption this removes — the Failed lane, the plain card line and
#: `nh doctor` still surface it.
BUDGET_TERMINAL_ROUTE = Route(TaskStatus.FAILED, notify_now=False, parked=False)


def route_for(category: BlockerCategory) -> Route:
    return _ROUTING[category]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prose(value: Any, absent: str | None = "") -> str | None:
    """Agent-emitted blocker JSON sometimes carries a prose field as a LIST of
    lines (measured 2026-09-01, task 019d8175: `evidence: [...]` reached
    `render_report`'s `.strip()` and crashed the scheduler, killing the attempt
    before its blocker was ever rendered). Same contract as options below:
    agent shapes must not need a migration. Lists join to lines; None means
    absent (`absent` keeps Optional fields None so `if b.question:` branches
    keep their meaning)."""
    if value is None:
        return absent
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value)
    return str(value)


def _machine_scalar(value: Any) -> str | None:
    """`wake_condition` is NOT prose — the watcher prefix-dispatches on it and
    `parse_duration` sums every duration-shaped match in the string, so joining
    a list would FABRICATE a satisfiable condition (measured: ["after:2h",
    "and PR org/repo#12 merged"] joins into a condition that self-fires at
    +2h12m, the 12 minutes lifted out of the PR number, the merge half never
    checked). A single-element list unwraps; anything longer is not one
    machine-checkable condition, so it becomes None — which never self-fires
    and still escalates to a human via the max_park timeout."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if len(value) == 1:
            return str(value[0])
        return None
    return str(value)


def _tried(value: Any) -> list[str]:
    """Same boundary, same contract as `_prose`: `{"tried": "I tried X"}` must
    not shred into per-character bullets in the report, and `{"tried": 3}` must
    not raise out of `parse_blocker` (the call site has no guard — the exact
    failure mode of the 2026-09-01 incident)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _confidence(value: Any) -> float:
    """`{"confidence": "high"}` raised ValueError out of the same unguarded
    call site. An unparseable confidence means the agent did not supply one:
    0.0 — which routes toward escalation, the conservative direction."""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class BlockerOption:
    """One answer a human can give, optionally carrying the action that makes it
    real.

    ``SCOPE_EXPLOSION`` offered "raise the limit for this task" long before
    anything could raise a limit, so answering that way regenerated the same
    blocker. An option with an ``action`` is applied by ``nh`` or the board when
    the human picks it; an option without one is just a label, submitted as the
    free-text answer exactly as ``nh reply`` has always behaved.

    The agent may never attach an action — that would let it resolve a blocker
    by weakening its own gate. ``parse_blocker`` strips actions at that boundary.
    """

    label: str
    action: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "action": self.action}

    @classmethod
    def coerce(cls, raw: Any) -> "BlockerOption":
        """Normalise the three shapes in the wild: an option, a bare string (old
        rows in ``tasks.blocker``, and every agent-raised blocker), or a dict."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, dict):
            action = raw.get("action")
            return cls(
                label=str(raw.get("label", "")),
                action=action if isinstance(action, dict) and action else None,
            )
        return cls(label=str(raw))


def resume_checkpoint(blocker: dict[str, Any] | None) -> dict[str, str] | None:
    """The [WIP-BLOCKED] commit a resumed task should continue from, or None.

    ``_raise_blocker`` checkpoints the working tree and records the sha here
    before parking the task. Only a human-authorised resume (`nh reply`, or the
    board) copies it onto the task, where the next attempt branches from it
    instead of throwing tens of turns of work away and starting from base.
    """
    if not isinstance(blocker, dict):
        return None
    sha = blocker.get("resume_commit") or ""
    if not sha:
        return None
    return {"sha": sha, "branch": blocker.get("resume_branch") or ""}


def carried_checkpoint(task: Any) -> dict[str, str] | None:
    """The checkpoint a NEW park should carry forward — freshest write wins.

    Two records can name one: ``context.resume_from`` (re-stamped by EVERY
    re-entry — wake, orphan requeue, human) and the retained ``blocker``
    (written by the LAST park, never cleared by a machine resume). They can
    disagree in one direction that matters: a human's send-back / reject
    writes ``resume_from`` with ``sha: None`` — "branch from base, do not
    credit the abandoned partial" — and leaves the blocker's older sha in
    place. A park that then read the blocker revived exactly the checkpoint
    the human cleared, and the next resume stamped it ``by: human``, disarming
    the zero-diff honesty gate over sent-back work. So: a PRESENT
    ``resume_from`` decides — its sha if it has one, a veto if it has none —
    and only an ABSENT one falls back to the blocker. Same rule
    `_park_quota` applies; this is the one home for it.
    """
    ctx = getattr(task, "context", None) or {}
    rf = ctx.get("resume_from") if isinstance(ctx, dict) else None
    if isinstance(rf, dict):
        sha = rf.get("sha") or ""
        if not sha:
            return None
        return {"sha": sha, "branch": rf.get("branch") or ""}
    return resume_checkpoint(getattr(task, "blocker", None))


def resume_provenance(checkpoint: dict[str, str] | None, by: str) -> dict[str, Any]:
    """The COMPLETE ``resume_from`` value for one resume, by one actor.

    Every resume path writes this and nothing else, because the zero-diff
    honesty gate reads ``sha`` and ``by`` TOGETHER — it credits work already
    ahead of base only when the branch point is the checkpoint a HUMAN gated —
    and seven review rounds were spent on the two ways those halves came apart.

    ``resume_from`` is stored with RFC 7396 (``merge_context``), where nested
    dicts MERGE and a ``None`` value DELETES the key. That is the whole trap:

    * A write that omitted ``by`` inherited the PREVIOUS actor's — so a resume
      with no checkpoint of its own was described by whoever resumed last. That
      latched in whichever direction the reader preferred: a stale ``"wake"``
      failed a human's answer as fabrication, a stale ``"human"`` credited a
      timer's re-entry.
    * Writing ``by`` alone fixed that and broke the other half — the sha a
      MACHINE resume had chosen survived and was relabelled ``human``, so
      `nh unblock` on a task the wake watcher had resumed disarmed the gate and
      an attempt that edited nothing was credited with the loop's own abandoned
      [WIP-PARTIAL]. **That direction opens a PR on work no attempt produced**,
      which is the failure this gate exists to prevent; an independent review
      reproduced it end to end through `run_task`.

    So provenance is never a separate write. Every key is stated every time:
    with no checkpoint, ``sha`` and ``branch`` are explicitly ``None``, which
    DELETES any inherited value, and ``by`` can only ever describe a sha the
    same actor chose. A human re-entry that names no checkpoint therefore
    branches from BASE and the gate stays ARMED.

    🔴 Be honest about what that costs. An earlier version of this docstring
    said "or from this run's own handoff… costing one attempt asked to redo
    work". Both halves were false: `_resume_branch_point` takes
    `handoff.wip_sha` only when ``attempt_n > 1``, and a resume starts a FRESH
    bounded loop at attempt 1, so there is no handoff to fall back to on the
    path that matters. The real cost is **redo everything committed since
    base**. That is still the right side to fail on — the alternative is a PR
    opened on work nobody did — but a caller that HAS a checkpoint must pass it
    rather than None, or it pays that price for nothing.

    ``by=CONSUMED_HUMAN_PROVENANCE`` is a distinct case worth naming here: it
    is a HUMAN-provenance value for `is_human_provenance`'s credit question
    (the work is still theirs), but `human_gate_armed` reads it as NOT armed —
    the two questions are answered by two different helpers on purpose, and
    conflating them re-opens the D15 regression this docstring describes
    above.
    """
    cp = checkpoint or {}
    return {"sha": cp.get("sha") or None,
            "branch": cp.get("branch") or None,
            "by": by}


def user_pause_blocker(
    reason: str, *, checkpoint: dict[str, str] | None, paused_by: str,
) -> dict[str, Any]:
    """The ONE blocker-dict shape all three pause writers (`_honor_cancel`,
    `nh task pause`, `POST /pause`'s direct-park branch) must produce.

    Before this, each inlined its own dict — same ``category`` string, but no
    ``raised_at`` (so the wake sweep dated the park from ``task.updated_at``,
    and an unrelated column write silently restarted the 48h max_park clock)
    and no record of who paused it. ``paused_by`` lets `_escalate_timeout`
    write an honest "paused by X and not resumed" reason instead of the
    generic "parked past max duration" a timed-out diagnosis-based blocker
    gets.

    Deliberately does NOT set ``human_stopped`` — that is the HOLD shape
    (any category, never swept, resumed only by releasing the hold) and is
    mutually exclusive with a PAUSE (USER_PAUSED, swept, resumes in one
    step). Mixing them is refused at the write end, in `api.pause_task`.
    """
    cp = checkpoint or {}
    return {
        "category": BlockerCategory.USER_PAUSED.value,
        "question": reason,
        "root_cause_hypothesis": reason,
        "resume_commit": cp.get("sha", ""),
        "resume_branch": cp.get("branch", ""),
        "raised_at": _now(),
        "paused_by": paused_by,
    }


#: Sibling fields that travel WITH a durable hold when it is carried forward.
#: Only keys actually present on the prior blocker are copied — this never
#: invents a reason/actor `POST /pause`'s hold branch did not record.
_HOLD_KEYS = ("human_stopped", "hold_reason", "hold_actor", "held_at")


def carry_human_hold(
    prior: dict[str, Any] | None, new: dict[str, Any],
) -> dict[str, Any]:
    """Forward a durable HOLD (``human_stopped``, see the module docstring)
    across a blocker REPLACEMENT.

    Every machine code path that writes a brand-new blocker dict in place of
    the old one (`Orchestrator._park_quota`'s fresh QUOTA park,
    `Orchestrator._raise_blocker`'s ``blocker.to_dict()``) silently dropped a
    hold a human had stamped on the blocker it replaced — a re-park after
    `POST /pause` held the task looked, to the wake sweep, exactly like a
    never-held one (SCRUM-22 regression). Call this with the PRIOR blocker
    (read before the assignment) and the REPLACEMENT dict; it returns the
    replacement with the hold re-stamped on top when the prior had one.

    Returns ``new`` unchanged when ``prior`` is not a dict or its
    ``human_stopped`` is not truthy — no hold to carry.

    Deliberate exception: never stamps a hold onto a replacement whose
    ``category`` is ``BlockerCategory.USER_PAUSED.value``. PAUSE and HOLD are
    mutually exclusive stop shapes (see the module docstring) and
    `api.pause_task` already refuses to mix them at the write end; carrying
    a hold onto a PAUSE here would manufacture the exact shape that refusal
    exists to prevent. A PAUSE is already a human stop, so nothing is lost.
    """
    if not isinstance(prior, dict) or not prior.get("human_stopped"):
        return new
    if new.get("category") == BlockerCategory.USER_PAUSED.value:
        return new
    carried = dict(new)
    for key in _HOLD_KEYS:
        if key in prior:
            carried[key] = prior[key]
    return carried


def human_event(
    verb: str,
    *,
    prior_status: "TaskStatus | str",
    prior_blocker: dict[str, Any] | None = None,
    reason: str | None = None,
    actor: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """The ONE ``task_events`` shape every human status-changing verb emits.

    ``Store.set_status``'s ``event=`` parameter inserts a ``task_events`` row
    in the SAME transaction as the status write (see its docstring), but
    until now nothing used it for the human verbs — ``nh task resume`` /
    ``pause`` / ``retry`` / ``cancel``, ``nh unblock``, ``nh reject``,
    ``nh reply``, and their board API twins. A human action that changed a
    task's status left no record of WHO changed it or what it changed FROM.
    This is the one place that shape is built, so a verb that forgets to
    pass ``event=human_event(...)`` fails LOUDLY (an omitted event stays
    omitted) instead of silently inventing a differently-shaped one, the
    same failure mode `resume_provenance`'s docstring describes for
    ``resume_from``.

    ``prior_status`` and ``prior_blocker`` must be read off the task by the
    CALLER *before* it mutates ``task.status`` / ``task.blocker`` — this
    function does no I/O and cannot recover a value already overwritten.
    ``prior_blocker`` is the full blocker dict as it stood at that moment
    (or ``None`` when there wasn't one, e.g. resuming a plain AWAITING_INPUT
    with no blocker attached — not every parked status carries one).
    """
    ev: dict[str, Any] = {
        "source": "human",
        "kind": f"human_{verb}",
        "text": text or f"{verb} by human",
        "prior_status": (
            prior_status.value if isinstance(prior_status, TaskStatus)
            else str(prior_status)
        ),
    }
    if prior_blocker is not None:
        ev["prior_blocker"] = prior_blocker
    if reason:
        ev["reason"] = reason
    if actor:
        ev["actor"] = actor
    return ev


#: The cooperative-stop reason a SERVER SHUTDOWN hands a running attempt.
#: Never written to ``tasks.cancel_requested`` — it is signalled in-process
#: (`Orchestrator.request_server_stop`) so a SIGKILL cannot leave it behind
#: to re-fire on the next server's first cheap boundary. `_honor_cancel`
#: routes it to a REQUEUE (checkpoint, close the row, stay IMPLEMENTING)
#: instead of a USER_PAUSED park.
SERVER_STOP_REASON = "__server_stop__"

#: ``resume_from.by`` values the MACHINE writes after an interrupted run — a
#: killed process (``orphan_recovery``, the scheduler's startup sweep), a
#: graceful stop (``server_stop``, `Orchestrator._honor_server_stop`), or a
#: hard kill mid-IMPLEMENTING (``hard_kill_salvage``,
#: `core.worktree.salvage_dead_worktrees` — the startup salvage of a worktree
#: whose owner pid died un-gracefully to SIGKILL/OOM/crash; the hard-kill twin
#: of ``server_stop``). The already-satisfied gate (`Orchestrator.
#: _already_satisfied_eligible`) reads this set, but membership in it is only
#: ONE of two ways a head becomes ineligible for the zero-diff claim escape —
#: the other is the head's own checkpoint SHAPE, a ``[WIP-BLOCKED]`` OR
#: ``[WIP-PARTIAL]`` subject (`_head_is_wip_checkpoint`), which applies
#: regardless of provenance (incident 0847f2c2, 2026-09-08: a ``wake`` resume
#: onto its own ``[WIP-BLOCKED]`` checkpoint is not in this set, yet must be
#: just as ineligible, because a ``[WIP-BLOCKED]`` subject off the ship ref
#: routinely fails `_already_satisfied_subject`; the same measurement holds
#: verbatim for ``[WIP-PARTIAL]`` — `_already_satisfied_subject` refuses both
#: subjects identically off the ship ref, so both must be routed to the full
#: review). A zero-diff attempt over a diff no completed review judged must
#: route to a full review whenever EITHER condition holds — read together
#: they are the whole rule, not this set alone.
MACHINE_REQUEUE_PROVENANCE = frozenset(
    {"orphan_recovery", "server_stop", "hard_kill_salvage"}
)

#: ``resume_from.by`` once a human's gate has been EXECUTED. `nh task resume`
#: (and its API twin) still write ``"human"`` verbatim — that write is what
#: (re-)ARMS the gate — but the attempt that actually branches from the
#: human's sha rewrites it to this value (`Orchestrator._consume_human_gate`),
#: so later AUTOMATIC checkpoints (server stop, orphan requeue, hard-kill
#: salvage) may stamp over it instead of being blocked forever. sha/branch are
#: preserved untouched — only ``by`` changes — so the audit trail still shows
#: a human chose this branch point, and the zero-diff honesty gate
#: (`Orchestrator._is_own_partial`) still credits it as theirs.
CONSUMED_HUMAN_PROVENANCE = "consumed_human"

#: Every ``resume_from.by`` value that names a HUMAN's choice, armed or
#: already consumed. One frozenset instead of the ad hoc ``by == "human"``
#: check inlined three times (`_honor_server_stop`, `_inherited_checkpoint`,
#: `salvage_dead_worktrees`) before this module gave the rule one home.
HUMAN_GATE_PROVENANCE = frozenset({"human", CONSUMED_HUMAN_PROVENANCE})


def is_human_provenance(by: str | None, ctx: dict | None) -> bool:
    """Did a HUMAN choose this branch point — armed or already consumed?

    Answers a different question than `human_gate_armed`: this is the credit
    question (`_is_own_partial`'s "is the work ahead of base a human's?"),
    that is the overwrite question ("must an automatic checkpoint refuse to
    stamp?"). A ``consumed_human`` row answers YES here and NO there —
    conflating the two re-opens the D15 regression (a correct "nothing to
    add" over a human's own sha failed as fabrication).

    Legacy rows (written before provenance existed) have no ``by`` at all;
    `blockers/wake.py` has always set ``resume_reason``, so a legacy MACHINE
    resume is still identifiable and anything else is a legacy human one.
    """
    if by:
        return by in HUMAN_GATE_PROVENANCE
    return (ctx or {}).get("resume_reason") != "wake_condition_satisfied"


def human_gate_armed(ctx: dict | None) -> bool:
    """Is a human's ``resume_from`` still UNCONSUMED — i.e. must an AUTOMATIC
    checkpoint refuse to overwrite it?

    Consume-once: the immediately-next attempt after `nh task resume`
    executes the human's sha (`_resume_branch_point`, `_is_own_partial`), and
    once it has, `Orchestrator._consume_human_gate` rewrites ``by`` to
    `CONSUMED_HUMAN_PROVENANCE` — a HUMAN provenance value for credit
    purposes (`is_human_provenance`) but no longer an armed gate, so ordinary
    machine stamping (server stop, orphan requeue, hard-kill salvage) resumes.
    A fresh ``nh task resume`` writes ``"human"`` again and re-arms it.

    A legacy stamp-less row (no ``by`` at all) is armed exactly like
    ``by == "human"`` — `is_human_provenance`'s legacy fallback — so an old
    row is not silently exempted from the protection this gate exists to give.
    """
    rf = (ctx or {}).get("resume_from") or {}
    if not rf.get("sha"):
        return False
    by = rf.get("by")
    return by == "human" or (not by and is_human_provenance(by, ctx))


@dataclass
class Blocker:
    """Structured blocker report (PLAN.md 22.1). Never prose — a human acts on
    this in under a minute (the escalation-precision promise, 22.4)."""

    category: BlockerCategory
    transient: bool = False
    wake_condition: str | None = None      # machine-checkable, see watcher
    root_cause_hypothesis: str = ""
    confidence: float = 0.0                # 0-1; low confidence biases to escalate
    tried: list[str] = field(default_factory=list)
    question: str | None = None            # the ONE decision/info needed
    options: list[BlockerOption] = field(default_factory=list)
    resume_branch: str = ""
    resume_commit: str = ""
    goal: str = ""                         # the step it was attempting (22.4 #1)
    evidence: str = ""                      # exact command + output (22.4 #2)
    raised_at: str = field(default_factory=_now)
    # 🔴 PROVENANCE OF THE PROSE, DECIDED WHERE THE PROSE COMES FROM.
    # `root_cause_hypothesis` and `question` have two completely different
    # origins. `parse_blocker` lifts them verbatim out of the coder's
    # `final_text`, so they are model-authored, unverified, and untrusted.
    # Every OTHER Blocker in this codebase — the fourteen constructions in
    # `orchestrator.py`, plus `fallback_blocker`, `missing_access`,
    # `ci_misconfigured` and `plan_gate.build_blocker` — carries prose written
    # as a source literal in no_human's own repo, which no_human demonstrably
    # DID write and which is usually its own bookkeeping ("max_attempts (3)
    # reached…" is not a hypothesis, it is a counter).
    #
    # Anything that republishes this prose has to know which it is holding.
    # `_abandon_draft_pr` labels the agent-authored kind "in the coding agent's
    # own words — no_human did not write this text and has not verified it";
    # printing that over a harness literal is a FALSE provenance claim in both
    # directions at once, and it tells the reader to distrust a fact the
    # harness established. Defaulting to False is the direction that cannot
    # lie by construction: a Blocker built by a constructor call IS
    # harness-authored, and the one place that is not — `parse_blocker` — sets
    # this True explicitly as a trust boundary, exactly like `options` and
    # `category`.
    reason_is_agent_authored: bool = False
    # {"attempts_before_escalation": int, "tokens_before_escalation": int} or
    # None when unmeasured (e.g. `store.lifetime_usage` failed — telemetry is
    # fail-open, routing is not). Never invent zeros for a missing object.
    escalation_latency: dict[str, int] | None = None
    # Which DB attempt raised this blocker (`store.create_attempt`'s id), so a
    # stored `human_replies` answer can record `source_attempt_id` and the
    # answer-reuse idempotency guard can key "already reused for THIS attempt"
    # (blockers/answers.py, orchestrator._raise_blocker). "" when unknown (a
    # blocker built outside an attempt, e.g. the plan-approval gate).
    attempt_id: str = ""
    # Structured "this refusal is a refused human send-back, not organic
    # exhaustion" marker (intake Q4). Set only by
    # `orchestrator._refuse_round` when a pending send-back
    # (`blockers.send_back`) hit a loop-head gate before an attempt row
    # existed. None for every ordinary blocker.
    send_back_refused: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # `options` is list[BlockerOption] unconditionally, whoever built it:
        # from_dict off an old row of bare strings, an agent's JSON, or a caller
        # that still passes labels.
        self.options = [BlockerOption.coerce(o) for o in (self.options or [])]

    @property
    def route(self) -> Route:
        return route_for(self.category)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "transient": self.transient,
            "wake_condition": self.wake_condition,
            "root_cause_hypothesis": self.root_cause_hypothesis,
            "confidence": self.confidence,
            "tried": list(self.tried),
            "question": self.question,
            "options": [o.to_dict() for o in self.options],
            "resume_branch": self.resume_branch,
            "resume_commit": self.resume_commit,
            "goal": self.goal,
            "evidence": self.evidence,
            "raised_at": self.raised_at,
            "reason_is_agent_authored": self.reason_is_agent_authored,
            "escalation_latency": (
                dict(self.escalation_latency) if self.escalation_latency else None
            ),
            "attempt_id": self.attempt_id,
            "send_back_refused": (
                dict(self.send_back_refused) if self.send_back_refused else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Blocker":
        return cls(
            category=BlockerCategory.coerce(data.get("category", "NOVEL_UNKNOWN")),
            transient=bool(data.get("transient", False)),
            wake_condition=_machine_scalar(data.get("wake_condition")),
            root_cause_hypothesis=_prose(data.get("root_cause_hypothesis")),
            confidence=_confidence(data.get("confidence")),
            tried=_tried(data.get("tried")),
            question=_prose(data.get("question"), absent=None),
            options=[BlockerOption.coerce(o) for o in (data.get("options") or [])],
            resume_branch=data.get("resume_branch", ""),
            resume_commit=data.get("resume_commit", ""),
            goal=_prose(data.get("goal")),
            evidence=_prose(data.get("evidence")),
            raised_at=data.get("raised_at", _now()),
            # Round-trips so a blocker rehydrated from `task.blocker` keeps the
            # provenance the run established. `parse_blocker` OVERWRITES this
            # after calling here, so an agent that puts the key in its own JSON
            # cannot clear it and get its prose published as no_human's.
            reason_is_agent_authored=bool(
                data.get("reason_is_agent_authored", False)),
            escalation_latency=(
                {
                    "attempts_before_escalation": int(
                        (data.get("escalation_latency") or {}).get(
                            "attempts_before_escalation", 0) or 0),
                    "tokens_before_escalation": int(
                        (data.get("escalation_latency") or {}).get(
                            "tokens_before_escalation", 0) or 0),
                }
                if data.get("escalation_latency") else None
            ),
            attempt_id=str(data.get("attempt_id", "") or ""),
            send_back_refused=(
                dict(data["send_back_refused"])
                if data.get("send_back_refused") else None
            ),
        )


def triage(
    blocker: Blocker, *, escalate_below_confidence: float = 0.6,
    budget_exhaustion_terminal: bool = True,
) -> Route:
    """Decide routing for a blocker.

    Honours the 22.2 taxonomy, with two overrides from config:

    * ``escalate_on_low_confidence_below`` (22.8 / Part 22 config): if the agent
      is *unsure what's wrong* (low confidence) on an otherwise-parkable
      blocker, we escalate and ask rather than silently parking on a wrong
      hypothesis.
    * ``budget.exhaustion_terminal`` (the default): BUDGET_EXHAUSTED ends the
      task instead of escalating it. See ``BUDGET_TERMINAL_ROUTE``. Checked
      FIRST — it is not a confidence call, and the budget blocker's confidence
      is a hardcoded 1.0 anyway (the harness counted, it did not guess).
    """
    if (budget_exhaustion_terminal
            and blocker.category is BlockerCategory.BUDGET_EXHAUSTED):
        return BUDGET_TERMINAL_ROUTE
    if blocker.category is BlockerCategory.USER_PAUSED:
        # Explicit, ahead of the low-confidence override below: a pause's
        # confidence is always 0.0 (there is no hypothesis to be confident
        # about), which would otherwise trip "unsure → escalate" and turn a
        # deliberate pause into an immediate escalation.
        return _ROUTING[BlockerCategory.USER_PAUSED]
    route = blocker.route
    if route.parked and blocker.confidence < escalate_below_confidence:
        # Unsure → don't thrash silently; ask a human.
        return Route(TaskStatus.ESCALATED, notify_now=True, parked=False)
    return route
