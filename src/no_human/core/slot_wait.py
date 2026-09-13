"""The "waiting for a worker slot" concept, in one place.

A resumed task (`wake._resume`) is written IMPLEMENTING before any worker is
attached — deliberately (see `blockers/wake.py`). When the pool is full, the
scheduler leaves it unclaimed rather than mislabel it. That silence is
by design; the task's RECORD staying silent about it is not. This module
defines the one event kind that says so, and the pure predicate that reads it
back from a task's event log.

WHAT ENDS A WAIT (review round 2 of PR #525): the first sign that a worker
has the task — ANY run-sourced event after the wait (`repo_config`,
`state: context`, `attempt_start`, a coder tool call, ...). The first version
ended a wait only on `attempt_start`, so a PENDING task that got a slot read
"waiting for a worker slot" through its whole CONTEXT/PLANNING phase — the
lie in the opposite direction. Watcher ticks and human verbs are not a
worker: they never end a wait.

A wait is also only ever OPEN on a task the scheduler could still claim
(`CLAIMABLE_STATUSES`): a task that waited, then parked at a gate or ended,
is not waiting, whatever its last events say.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

KIND = "waiting_for_slot"

#: Event sources that mean a worker/run is acting on the task. An event from
#: any of these, of any kind other than the wait itself, ends the wait.
RUN_SOURCES = frozenset({"orchestrator", "agent", "reviewer", "scheduler"})

#: The statuses the scheduler claims from: `Scheduler._CLAIMABLE`
#: (IMPLEMENTING, PENDING) plus `Scheduler._CORRECTION_CLAIMABLE` (PLANNING,
#: claimed only when `plan_gate.correcting(t)` is true — a plan-approval
#: correction resumes into PLANNING, not IMPLEMENTING). A wait can only be
#: open while the task sits in one of them. Widening this to include
#: "planning" does NOT make a genuinely dispatched planning run read as
#: waiting: `ends_wait` already closes the wait on the run's first
#: run-sourced event (`repo_config`, `state: context`, ...), regardless of
#: which status that event lands the task in.
CLAIMABLE_STATUSES = frozenset({"implementing", "pending", "planning"})


def waiting_text(busy: int, total: int, since_iso: str) -> str:
    return f"waiting for a worker slot ({busy}/{total} busy) since {since_iso}"


def waits_are_live(pause: Mapping | None, *, reachable: bool = True) -> bool:
    """False while the pool is paused — nothing is competing for a slot, so a
    recorded wait event is not a *live* slot wait (`_running_pool_stats`'s
    ``pause`` element: truthy exactly when `/api/queue/health` reports
    ``paused``).

    Also False when the pool couldn't be asked at all (``reachable=False``,
    i.e. `_running_pool_stats` returned `None` — server not running or
    unreachable): `pause is None` is what a genuinely un-paused pool ALSO
    looks like, so without this the two were indistinguishable and an
    unreachable pool's stale wait was confidently reported as a live one
    (review finding F2) — unlike `nh task show`, which already hedges with
    `STALE_POOL_NOTE` in that same case. Fail closed: an unknown pool state is
    not evidence of a live wait."""
    return reachable and not pause


def pool_paused_text(pause: Mapping) -> str:
    """Render the same pause a task is behind, using only the fields
    `_running_pool_stats` already exposes (`/api/queue/health`'s
    ``paused_*``, renamed to ``reason``/``until``/``profile`` by
    `cli/pool_probe.py`) — no second derivation of the wall clock.

    ``quota``/``infra`` are genuine cooldowns: they carry a resume time.
    ``lease_lost`` (task 92e48491) is not — a scheduler that lost its pool
    lease never resumes dispatch on its own (no clearing site; a restart is
    the only way back), so its line never says "cooldown" or "resumes".
    Anything this function has never heard of — a genuinely unrecognised
    reason string, or a missing one — renders the same honest "unrecognised"
    line for the same reason: a permanent, restart-only failure rendered as
    a self-resolving cooldown (the closed PR #251's exact regression, and
    this task's F1 review finding) is worse than saying nothing."""
    reason = pause.get("reason")
    if reason in ("quota", "infra"):
        label = "quota cooldown" if reason == "quota" else "infra cooldown"
        until = pause.get("until") or "unknown"
        text = f"pool paused — {label}, resumes {until}"
        profile = pause.get("profile")
        if profile:
            text += f" ({profile} profile)"
        return text
    if reason == "lease_lost":
        return "pool paused — pool lease lost; restart required"
    label = reason if reason else "unknown"
    return f"pool paused — reason unrecognised ({label})"


#: Printed alongside a stale wait line when the pool's live state cannot be
#: read at all (server not running / unreachable) — labelled, not silent.
STALE_POOL_NOTE = "pool not reachable — this may be stale"


def ends_wait(event: Mapping) -> bool:
    """Is this event a worker acting on the task (so any open wait is over)?"""
    return (event.get("kind") != KIND
            and str(event.get("source") or "") in RUN_SOURCES)


def is_waiting_for_slot(events: Iterable[Mapping], *,
                        status: str | None = None) -> bool:
    """True when the newest wait-relevant event is the wait itself and the
    task is still claimable. ``events`` arrives oldest -> newest
    (`Store.list_events`). ``status`` is the task's current status value;
    when given and not claimable the answer is False regardless of events.

    Empty, or a task whose event log carries neither a wait nor a run event,
    is False.
    """
    if status is not None and str(status) not in CLAIMABLE_STATUSES:
        return False
    last: str | None = None
    for event in events:
        kind = event.get("kind")
        if kind == KIND:
            last = KIND
        elif ends_wait(event):
            last = "run"
    return last == KIND
