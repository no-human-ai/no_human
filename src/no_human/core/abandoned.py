"""The abandoned-IMPLEMENTING-row concept: one predicate, a read-only finder,
and the mutating recovery sweep.

THE INCIDENT: two live task rows read `claimed=false status=implementing`
(cf65812d silent 180 min, 0d637473 silent 40 min with a pause request nothing
ever consumed). `Scheduler._CLAIMABLE = (IMPLEMENTING, PENDING)` is walked in
that order (WIP-first, scheduler.py:100, load-bearing — see that tuple's own
comment), and dispatch used to slice `claimable[:slots]`. With 2 free slots
and 2 such rows at the head, the slice was exactly those two rows on every
tick, forever — no PENDING task, however many, was ever reached, and raising
one to `priority: high` changed nothing (`_rank_pending` only reorders the
PENDING batch it is handed; it never runs on rows ahead of it).

THE PATH THIS ROW SHAPE COMES FROM (confirmed by reading `tick()`,
scheduler.py:2279-2294, not assumed): the dispatch loop calls
`_shipped_before_dispatch(task)` on each candidate and `continue`s past a
row it returns True for — "this content already landed, don't burn an
attempt" — WITHOUT ever removing the row from IMPLEMENTING or adding it to
`_inflight`. That `continue` is the only place in the claim path that both
(a) leaves a row silently exactly as it found it and (b) can fire on a
resumed IMPLEMENTING row with no worker attached. Before this task's fix,
that same `continue` sat inside `claimable[:slots]`, so it also burned the
slot the row was sitting in — the second half of the incident (`scheduler.py`
`tick()`'s counted-dispatch loop is the fix for that half; this module is the
fix for the row itself never being reclaimed). A crashed `_run` coroutine
(scheduler.py's crash handler catches `Exception`, not `CancelledError`) or a
`drain()` grace-period exit leaving a row IMPLEMENTING are the same shape by
construction — the sweep below does not care which of the three produced it;
detection is path-independent.

ABANDONED_STATUS is deliberately ONLY IMPLEMENTING. `Scheduler._ORPHANABLE`
is `(CONTEXT, PLANNING, REVIEWING, TESTING)` and `MID_RUN_STATUSES`'s comment
(scheduler.py:2773) says it omits IMPLEMENTING because "it self-recovers via
the claim path" — this module is the proof that self-recovery had a hole.
Restricting to IMPLEMENTING guarantees no overlap with `_recover_orphans`,
which moves the other four statuses INTO IMPLEMENTING: the two sweeps can
never fight over the same row.

DETECTION, all three required (get this wrong and the fix reaps healthy
tasks — see `find_abandoned`):
  1. `status is IMPLEMENTING` and not held (`id not in inflight_ids`, the
     same signal `api/app.py`'s `claimed` field reads, api/app.py:1461).
  2. Silent past `threshold_s`, judged EXACTLY the way
     `blockers.wake._escalate_if_stalled` judges the stuck-active watchdog —
     `last_event_ts` alone, not row `updated_at`: no event ever
     (`last_event_ts` is None) is NOT abandoned, the same fail-closed rule —
     a row this sweep cannot prove is silent is left alone, not reaped on a
     hunch. Row `updated_at` is deliberately NOT folded in here (unlike
     `Scheduler._activity_age_s`, which mixes the two for its own "seconds
     until claimable" ETA estimate): every known path that writes IMPLEMENTING
     (`_recover_orphans`, `wake._resume`) ALSO emits a fresh event within the
     same call — `_recover_orphans` writes its `orphan_recovered` event right
     after the status write (scheduler.py:1752), `_resume` its `resumed`
     event right after its own (wake.py:878/898) — so `last_event_ts` is
     already the freshest signal on every real transition; adding row age
     would only let a status write with NO event of its own mask a genuinely
     stale row, the opposite of fail-closed.
  3. The newest event is NOT `waiting_for_slot` — reusing
     `store.tasks_waiting_for_slot()` (the SQL twin of
     `slot_wait.is_waiting_for_slot`) rather than re-deriving the rule. A row
     the scheduler is actively queueing for a slot is the OPPOSITE of
     abandoned (measured: 7f1660bb read `claimed=false status=implementing`
     23 seconds after a human send-back and was completely healthy — its
     next event was `waiting_for_slot`).

A `cancel_requested` row IS recovered (that is 0d637473's shape — nothing is
alive to consume the flag, so waiting for an operator would wait forever);
the next worker that picks the row back up from PENDING sees the flag on its
own first tick and ends the task honestly, same as any other resume. This is
a MACHINE re-entry (`tests/test_resume_entry_registry.py`'s STOP_REGISTRY:
KEEPS) — it executes no new human decision, so a human's still-pending stop
survives it and `_drive` parks on turn zero at the next start, same as
`Scheduler._recover_orphans` and `Orchestrator._honor_server_stop`.

`resume_from` is INHERITS_ELSE_STAMPS (`test_resume_entry_registry.py`'s
REGISTRY), via `_inherit_checkpoint` below: a still-armed human gate is
inherited untouched; otherwise the dead attempt's own commit is stamped in
(provenance `"orphan_recovery"`, reused — see `_inherit_checkpoint`'s
docstring) BEFORE its open attempt row is closed, so a genuinely abandoned
run's committed work is not silently discarded the way the 2026-08-10 orphan
incident discarded it before `_recover_orphans` grew the same mirror.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from ..blockers import human_gate_armed, resume_provenance
from . import plan_gate
from .task import Task, TaskStatus

log = logging.getLogger("no_human.abandoned")

#: See the module docstring — deliberately narrower than
#: `Scheduler._ORPHANABLE`. Never widen this without re-reading why
#: `MID_RUN_STATUSES`'s comment excludes IMPLEMENTING from that tuple.
ABANDONED_STATUS = TaskStatus.IMPLEMENTING

#: A row recovered this many times and found abandoned YET AGAIN is not
#: "unlucky", it is something structurally wrong with the task or its
#: environment (dead worktree, a poison prompt, ...) — re-queueing it a 4th
#: time would thrash the pool forever. Escalate to a human instead. Mirrors
#: `blockers.wake.DEAD_RESUME_PARK_STREAK`'s "streak 3 parks honestly" shape.
_MAX_RECOVERIES = 3


async def find_abandoned(
    store: Any, *, inflight_ids: Any, threshold_s: float,
    now: datetime | None = None,
) -> list[Task]:
    """Read-only: every IMPLEMENTING row that qualifies as abandoned (see the
    module docstring for the three conditions). Never mutates anything —
    used by both `recover_abandoned` (below) and `core.health.queue_health`
    (which only needs the count/ids, not the recovery).
    """
    now_dt = now or datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()
    inflight = set(inflight_ids or ())
    try:
        waiting = await store.tasks_waiting_for_slot()
    except Exception as exc:  # noqa: BLE001 — a failed read must not crash the sweep
        log.warning("find_abandoned: tasks_waiting_for_slot() failed, "
                    "treating no row as waiting: %s", exc)
        waiting = set()

    out: list[Task] = []
    for t in await store.list_tasks(ABANDONED_STATUS):
        # Condition 1: held rows are never abandoned — the caller's
        # `inflight_ids` IS the scheduler's own claim set.
        if t.id in inflight:
            continue
        # Condition 3: a row the scheduler is actively queueing for a slot
        # is the opposite of abandoned — see the module docstring's 7f1660bb
        # measurement.
        if t.id in waiting:
            continue
        # A human's plan correction waiting to be re-planned is claimed by
        # `Scheduler._claimable`'s PLANNING branch, never by this sweep —
        # mirrors `_recover_orphans`'s own first filter so the two sweeps
        # agree on what a mid-run row IS. IMPLEMENTING rows essentially never
        # carry this marker (it is written for PLANNING), but the guard
        # costs nothing and keeps this module fail-closed the same way its
        # sibling sweep is.
        if plan_gate.correcting(t):
            continue
        try:
            ev_ts = await store.last_event_ts(t.id)
        except Exception as exc:  # noqa: BLE001 — can't prove silence, don't reap
            log.warning("find_abandoned: last_event_ts(%s) failed: %s",
                        t.id[:8], exc)
            continue
        # Condition 2, fail-closed half: never emitted a single event is NOT
        # evidence of abandonment — same rule `wake._escalate_if_stalled`
        # applies (`last_ts is None` -> leave it to the normal loop/startup).
        if ev_ts is None:
            continue
        event_age = max(0.0, now_ts - ev_ts)
        if event_age < threshold_s:
            continue
        # Re-read: the row may have gone terminal (a human's `shipped`/cancel
        # landing) between `list_tasks`'s read above and this decision —
        # the same SCRUM-68 guard `wake._is_terminal`/
        # `Scheduler._is_terminal_row` both apply before their own writes.
        current = await store.get_task(t.id)
        if current is None or current.status is not ABANDONED_STATUS:
            continue
        out.append(current)
    return out


async def _inherit_checkpoint(store: Any, t: Task) -> str:
    """Stamp the dead attempt's own commit onto ``resume_from`` BEFORE its
    open attempt row is closed — mirrors `Scheduler._inherited_checkpoint`
    (scheduler.py), written for the exact same failure mode: a requeue
    re-enters as a FRESH bounded loop at ``attempt_n == 1``, where
    `_resume_branch_point` ignores the dead attempt's ``handoff.wip_sha`` —
    so ``resume_from`` is the ONLY checkpoint that survives, and leaving it
    untouched silently discards whatever the abandoned run had already
    committed (the 2026-08-10 incident this mirror exists to not repeat: 3
    restarts, 11 attempts burned, because nothing copied a dead attempt's
    commit into the one place a fresh run actually reads).

    A HUMAN's still-armed gate (`human_gate_armed`) is inherited untouched —
    stamping over it relabels their gated sha as the machine's own and
    disarms `Orchestrator._is_own_partial`'s zero-diff honesty check, the
    exact defect ten prior review rounds kept re-introducing.

    Provenance is ``"orphan_recovery"``, reused rather than invented: this
    sweep rescues a dead run's commit the same way `Scheduler._recover_orphans`
    does, and `blockers.MACHINE_REQUEUE_PROVENANCE` already treats that label
    as machine-requeue provenance everywhere the zero-diff honesty gate reads
    it (`Orchestrator._already_satisfied_eligible`) — inventing a fourth label
    would need registering there too, for no behavioural difference.
    """
    ctx = t.context or {}
    if human_gate_armed(ctx):
        return ""                    # a human gated it — execute, don't decide
    resume = ctx.get("resume_from") or {}
    sha = (await store.latest_open_attempt(t.id) or {}).get("commit_sha") or ""
    if not sha or sha == resume.get("sha"):
        return sha                   # nothing to inherit, or already stamped
    await store.merge_context(
        t.id, {"resume_from": resume_provenance({"sha": sha}, "orphan_recovery")})
    return sha


async def recover_abandoned(
    store: Any, *, inflight_ids: Any, threshold_s: float,
    on_event: Callable[[str, str], None] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Move every row `find_abandoned` returns back to PENDING so it can be
    re-dispatched by a fresh worker, and close its dangling open attempt with
    a recorded reason. Returns the ids actually recovered.

    Called once per scheduler tick (`Scheduler.tick`, immediately before
    `_claimable()` is built) — per the intake decision to detect and recover
    within the SAME tick that would otherwise dispatch the stale head, not on
    a slower periodic schedule that would leave the pool starved in the
    interval.

    Per-row `try/except`: one bad row must never abort the sweep or take the
    whole pool down with it — same discipline every other tick-time sweep in
    `scheduler.py` follows (`_recover_orphans`, `_resume_quota_parks`, ...).
    """
    recovered: list[str] = []
    try:
        rows = await find_abandoned(
            store, inflight_ids=inflight_ids, threshold_s=threshold_s, now=now)
    except Exception as exc:  # noqa: BLE001 — sweep must not kill the pool
        log.warning("abandoned-row sweep: find_abandoned failed: %s", exc)
        return recovered

    age_min = threshold_s / 60.0
    for t in rows:
        try:
            n = int((t.context or {}).get("abandoned_recoveries") or 0)
            if n >= _MAX_RECOVERIES:
                await _escalate(store, t, n=n, age_min=age_min, on_event=on_event)
                continue
            reason = (
                f"interrupted: the worker that held this "
                f"{ABANDONED_STATUS.value} row is gone (no event for over "
                f"{age_min:.0f} min, never claimed by any worker) — "
                "recovered to pending by the abandoned-row sweep")
            # THE STATUS WRITE GOES FIRST (mirrors `_recover_orphans`'s own
            # discipline, scheduler.py:1703): `set_status` CAS-guards
            # terminal rows and returns None when it refuses one — a human
            # can mark this task DONE or cancel it between `find_abandoned`'s
            # read and here, and nothing below has a rollback, so stamping
            # first would leave a FINISHED task's attempt closed under it
            # for no reason. A lost race here costs nothing: the sweep wrote
            # nothing.
            if await store.set_status(
                t, TaskStatus.PENDING, validate=False,
                event={
                    "kind": "abandoned_recovered", "source": "scheduler",
                    "text": (
                        f"{t.id[:8]}: no worker held this "
                        f"{ABANDONED_STATUS.value} row for over "
                        f"{age_min:.0f} min — recovered to pending"),
                },
            ) is None:
                continue  # CAS refused (row went terminal) — touch nothing else
            try:
                # MUST run before `close_open_attempts` below: the dead
                # attempt's commit is only readable from `latest_open_attempt`
                # while its row is still `in_progress`.
                await _inherit_checkpoint(store, t)
            except Exception as exc:  # noqa: BLE001 — the rescue above already
                # landed; a checkpoint is an optimisation, not the rescue
                # itself, so a lookup failure must not abort the row.
                log.warning("abandoned-row recovery: checkpoint inheritance "
                            "failed for %s: %s", t.id[:8], exc)
            # The open attempt row IS the dangling work this sweep exists to
            # close out — `close_open_attempts` is idempotent and safe even
            # when nothing is actually running any more (db.py:2729).
            await store.close_open_attempts(t.id, reason=reason)
            await store.merge_context(t.id, {"abandoned_recoveries": n + 1})
            recovered.append(t.id)
            if on_event is not None:
                on_event(
                    "abandoned_recovered",
                    f"{t.id[:8]}: recovered from an abandoned "
                    f"{ABANDONED_STATUS.value} row (no event for over "
                    f"{age_min:.0f} min) — requeued to pending")
        except Exception as exc:  # noqa: BLE001 — one bad row must never abort the sweep
            log.warning("abandoned-row recovery failed for %s: %s", t.id[:8], exc)
    return recovered


async def _escalate(
    store: Any, t: Task, *, n: int, age_min: float,
    on_event: Callable[[str, str], None] | None,
) -> None:
    """Loop guard: a row abandoned `_MAX_RECOVERIES` times already is not bad
    luck — re-queueing it a 4th time would thrash the pool forever instead of
    ever reaching a human. Escalate honestly instead."""
    blocker = {
        "category": "NOVEL_UNKNOWN",
        "question": (
            f"This task was recovered from an abandoned "
            f"{ABANDONED_STATUS.value} row {n} time(s) already and went "
            "silent again each time — something about this task or its "
            "environment keeps losing the worker before it makes progress. "
            "Take over manually?"),
        "root_cause_hypothesis": (
            f"recovered {n} time(s) by the abandoned-row sweep; each "
            f"recovery silently lost its worker again (no event for over "
            f"{age_min:.0f} min each time)"),
    }
    t.blocker = blocker
    await store.update_task_columns(t)
    if await store.set_status(
        t, TaskStatus.ESCALATED, validate=False,
        event={
            "kind": "abandoned_escalated", "source": "scheduler",
            "text": (
                f"{t.id[:8]}: abandoned {n} time(s) already — escalating "
                "instead of re-queueing again"),
        },
    ) is None:
        return  # CAS refused (row went terminal) — touch nothing else
    if on_event is not None:
        on_event(
            "abandoned_escalated",
            f"{t.id[:8]}: repeated abandonment ({n}x) — escalated instead "
            "of re-queueing")
