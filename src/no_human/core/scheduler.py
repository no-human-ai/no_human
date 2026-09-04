"""Phase 7.3/7.4: the concurrent task scheduler.

A single-event-loop pool that drains the SQLite queue into at most
``max_workers`` concurrent ``run_task`` coroutines. Concurrency is real because
the two long phases yield: the Agent SDK session is async, and the orchestrator
offloads the blocking test subprocess to a thread. Each task runs in its own git
worktree (``isolation.enabled``, on by default), so same-repo tasks don't
collide. Isolation and parallelism are separate switches: isolation is the
default for a single task, and a hard requirement for a pool of more than one.

Two coordination rules:
  - **No double-dispatch.** A task id is reserved in ``_inflight`` synchronously
    before its coroutine is scheduled, so the next tick won't re-claim it (even
    though its DB status becomes IMPLEMENTING mid-run).
  - **Shared-quota gate (7.4).** All workers share one subscription. When any
    task parks PAUSED_QUOTA on a billing wall, the pool stops dispatching
    until the reset time — one worker hitting the limit pauses the whole
    pool, not just itself. An INFRA park (`blocker.infra`, a dead SDK
    session) parks only its own task; the 3-strike breaker is the pool's
    response to those.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from ..agent.worker_context import WorkerContext, set_worker_context
from ..blockers import human_gate_armed
from ..blockers.shipped import _TICK_ABORTED, complete_if_content_landed
from ..config import (AuthError, DEFAULT_CONFIG, active_auth_profile,
                      parallelism_enabled, pid_alive, process_start_token,
                      worktree_isolation_enabled)
from ..vcs.pr_watcher import landing_sha_candidates, orphan_landed_evidence
from ..vcs.task_pr import resolve_task_pr
from .bounds import QuotaExhausted
from .db import Store
from . import plan_gate
from . import slot_wait
from .events import EventPersister
from .infra_breaker import infra_breaker
from .task import (
    LANDED_RECONCILABLE, TERMINAL_LANDED_RECONCILABLE, TaskStatus,
    priority_rank,
)
from .worktree import salvage_dead_worktrees, sweep_stale_worktrees

log = logging.getLogger("no_human.scheduler")

# Tasks the scheduler may pick up: freshly created, or flipped back to
# IMPLEMENTING by the WakeWatcher / `nh reply` resume. IMPLEMENTING first —
# WIP-first: resumed work carries sunk cost and a waiting operator, and
# pending-first starved three budget-raised resumes behind every newly
# imported ticket on a one-worker pool (live, 2026-07-24).
#
# Within a status, `list_claimable_tasks` still hands back OLDEST first: the
# claim path used to consume `list_tasks`, which is `created_at DESC` for the
# board, so the NEWEST pending ticket dispatched first and four day-old
# tickets never dispatched at all (live, 2026-08-12) — including the
# repro-gate fix that was blocking an escalated task. WIP-first (across
# statuses) still holds unconditionally.
#
# Within PENDING specifically, `_rank_pending` (below) re-sorts that
# oldest-first batch two ways before FIFO ever applies: prior-work tasks (a
# burned attempt, or a context marker left by an open draft PR / a prior
# resume) are claimed ahead of never-started ones (`_claimable`'s pending
# split, live 2026-08-20) — a newcomer ticket and a task carrying sunk
# context/an open PR both land in PENDING between attempts, and without this
# a restart's fresh claim can fill every slot with newcomers while the
# sunk-cost task waits behind them — and *within* each of those two groups,
# `priority_rank` (high > medium > low) now orders ahead of age (live,
# 2026-08-22: a `high` ticket used to wait behind however many `medium`
# tickets happened to be older). FIFO is only the final tie-break inside a
# priority tier of a group, not the ordering of a status on its own anymore.
_CLAIMABLE = (TaskStatus.IMPLEMENTING, TaskStatus.PENDING)

# PLANNING is claimed too, but only for a plan-approval correction resumed
# into it (see `_claimable`'s `plan_gate.correcting` branch below) — never
# for a fresh/live planning run, which reaches PLANNING through dispatch,
# not through the claim loop.
_CORRECTION_CLAIMABLE = (TaskStatus.PLANNING,)

#: The runtime twin of `slot_wait.CLAIMABLE_STATUSES` — same set of statuses,
#: expressed as the scheduler's own claim tuples rather than re-typed string
#: literals, so the two can be compared as real objects (`test_scheduler.py`)
#: instead of trusted to stay in sync by eye.
CLAIM_STATUS_VALUES = frozenset(
    s.value for s in _CLAIMABLE + _CORRECTION_CLAIMABLE)

# Context keys that mark a PENDING task as carrying prior work rather than
# being a fresh ticket: the exact keys `Orchestrator` writes at PR open
# (orchestrator.py:5818-5827) and `wake._resume` writes on resume.
_PRIOR_WORK_CONTEXT_KEYS = ("pr_branch", "pr_delivered_url", "pr_watch", "resume_from")


def _has_prior_work(task, attempt_counts: dict) -> bool:
    ctx = task.context if isinstance(task.context, dict) else {}
    if any(ctx.get(key) for key in _PRIOR_WORK_CONTEXT_KEYS):
        return True
    return attempt_counts.get(task.id, 0) > 0

#: Default for `concurrency.stop_grace_s`: how long a stopping server waits
#: for in-flight attempts to checkpoint and unwind before exiting anyway.
#: Read from the config template so the number lives in one place.
DEFAULT_STOP_GRACE_S = float(DEFAULT_CONFIG["concurrency"]["stop_grace_s"])
#: The API lifespan waits for the worker loop this much LONGER than the grace,
#: so the scheduler's own bounded drain is what ends the wait, not uvicorn.
LIFESPAN_DRAIN_MARGIN_S = 5.0
#: `nh stop --timeout` defaults to the grace plus this, so SIGKILL lands only
#: after the lifespan has had its turn. Three readers, one number.
STOP_COMMAND_MARGIN_S = 15.0


def stop_grace_s(config: dict | None) -> float:
    """`concurrency.stop_grace_s` as a non-negative float, default 60.

    The single source for the three waits that stack at shutdown — the
    scheduler's drain, the lifespan's wait on the worker loop, and `nh stop`'s
    SIGKILL timer. Each used to carry its own literal (30 s in two places),
    which is how the outer kill landed before the inner drain had a chance.
    An unparseable value reads as the default, a negative one as zero.
    """
    raw = ((config or {}).get("concurrency") or {}).get("stop_grace_s",
                                                       DEFAULT_STOP_GRACE_S)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_STOP_GRACE_S


def pool_width_ceiling(cpu_count: int | None = None) -> int:
    """The widest pool this machine is allowed to run: ``cpu_count // 3``,
    never below 2.

    Why a third of the cores and not half: a worker is not one process. Each
    one drives a nested Agent-SDK subprocess (coder, then reviewer) *and* a
    test run, and ``bounded_xdist_workers`` below hands that test run
    ``cpu // max_workers`` pytest workers — so a pool's real CPU demand is
    several times its width. A third is a sanity margin against that
    arithmetic, not a measured optimum: the one measurement behind any of it
    is the incident ``bounded_xdist_workers`` was added for (3 tasks ×
    ``pytest -n auto`` on 12 cores timed every run out). Nothing here has
    been measured at cpu//2, so nothing here claims anything about it. The
    floor of 2 keeps the shipped default (2) reachable on a small machine
    rather than silently serialising it.
    """
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 4)
    return max(2, int(cpus) // 3)


def clamp_pool_width(
    requested: int, *, cpu_count: int | None = None,
) -> tuple[int, str | None]:
    """``(width, reason)`` — the width to actually run, and why it is not the
    width that was asked for.

    ``nh start --workers 64`` used to be accepted in full: both resolvers
    bounded the pool from below (``max(1, ...)``) and not from above, so the
    only CPU-aware bound in the system was on the *children*
    (``bounded_xdist_workers``, added after 3 tasks × ``pytest -n auto`` on 12
    cores timed every run out) and nothing at all bounded the *parents*. The
    ceiling is loud rather than silent for the same reason the isolation
    refusal below is: an operator who asked for a width and got a different one
    must be told which one is running, and why.

    ``cpu_count`` is injectable so the boundary is testable on any machine.
    """
    ceiling = pool_width_ceiling(cpu_count)
    if requested <= ceiling:
        return requested, None
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 4)
    return ceiling, (
        f"{requested} workers is above this machine's ceiling of {ceiling} "
        f"({cpus} cores // 3, floor 2) - running {ceiling}, not {requested}. "
        "This is a sanity ceiling against absurd widths, not a tuned optimum: "
        "every worker drives its own nested agent subprocesses AND its own "
        "pytest -n run, whose width is already cpu//workers, so a third of the "
        "cores is where the test phase starts starving itself. It is not a "
        "stability guarantee - nested-subprocess instability at even small "
        "widths is a known separate issue, tracked and fixed on its own "
        f"branch. Ask for {ceiling} or fewer, or raise the ceiling only after "
        "re-measuring on this machine."
    )


def resolve_max_workers(
    config: dict, *, override: int | None = None, cpu_count: int | None = None,
) -> tuple[int, str | None]:
    """Effective pool size, plus a warning when a request for >1 is refused.

    Two independent switches (see ``config.worktree_isolation_enabled`` /
    ``config.parallelism_enabled``):

    - ``isolation.enabled`` — each task gets its own worktree. On by default,
      so the width below is normally allowed.
    - ``concurrency.enabled`` — more than one task at a time. Off by default.

    The pool must never be wider than the isolation allows. With ``enabled:
    false, max_workers: 2`` the server once announced "2 worker(s) · concurrent"
    while two tasks ran in the SAME checkout, stomping each other's branch and
    index. The CLI and the server lifespan both resolve the width here; they
    used to compute it separately, which is how the announcement and the real
    pool were free to disagree.

    The width is bounded from ABOVE too (``clamp_pool_width``): the isolation
    and concurrency switches decide whether a pool is allowed at all, and the
    ceiling decides how wide this machine can carry it. The two are checked in
    that order, so a downgrade to 1 still reports the switch that caused it.
    """
    conc = config.get("concurrency", {}) or {}
    requested = max(1, int(override or conc.get("max_workers", 1) or 1))
    if requested > 1:
        if not worktree_isolation_enabled(config):
            return 1, (
                f"isolation.enabled is false - running 1 worker, not {requested}. "
                "Parallel tasks would share one checkout, stomping each other's "
                "branch and index. Re-enable isolation.enabled to run them in "
                "parallel."
            )
        if not parallelism_enabled(config):
            return 1, (
                f"concurrency.enabled is false - running 1 worker, not {requested}. "
                "Set concurrency.enabled: true to run them in parallel."
            )
    return clamp_pool_width(requested, cpu_count=cpu_count)


def resolve_serve_pool(
    config: dict, *, cli_workers: int | None, cpu_count: int | None = None,
) -> tuple[int, bool, str | None]:
    """``nh serve``'s decision: an explicit ``--max-workers`` enables the pool
    for THIS invocation only (the config default on disk stays whatever it was).
    Returns ``(workers, enabled, error)``; ``error is not None`` means: don't
    serve, print it and exit.

    - ``cli_workers`` given and < 1  -> ``(0, False, "<validation message>")``.
    - ``cli_workers`` given and >= 1 -> the flag buys parallelism:
      ``(cli_workers, True, None)`` even if ``concurrency.enabled`` is false in
      config. It does NOT buy isolation — that is a separate switch, and a flag
      must not override an operator who explicitly turned isolation off.
    - ``cli_workers`` is None        -> refuse when ``concurrency.enabled`` is
      false; otherwise use ``concurrency.max_workers`` (default 2, matching
      serve()'s historical default — NOT ``resolve_max_workers``'s default of 1,
      which is for the override-clamp case, not "no flag given at all").

    Isolation is a DEFAULT for one task and a REQUIREMENT for many: any pool
    wider than one worker is refused outright when ``isolation.enabled`` is
    false, rather than quietly downgraded, because N workers in one checkout
    is the exact collision the isolation exists to prevent.

    Both widths are then bounded from above by ``clamp_pool_width``. The
    clamp is a downgrade, not a refusal, so it does NOT travel in ``error``
    (which means "do not serve"): ``serve()`` re-derives the one-line reason
    from the width that was asked for and prints it, the same way ``nh start``
    prints ``resolve_max_workers``'s warning.
    """
    conc = config.get("concurrency", {}) or {}
    isolated = worktree_isolation_enabled(config)

    def _no_isolation(width: int) -> str:
        return (
            f"isolation.enabled is false - refusing to serve {width} workers. "
            "They would share one checkout and stomp each other's branch and "
            "index. Set isolation.enabled: true in ~/.no_human/config.yaml, or "
            "serve a single worker."
        )

    if cli_workers is not None:
        if cli_workers < 1:
            return 0, False, (
                f"--max-workers must be a positive integer, got {cli_workers}."
            )
        if cli_workers > 1 and not isolated:
            return 0, False, _no_isolation(cli_workers)
        return clamp_pool_width(cli_workers, cpu_count=cpu_count)[0], True, None

    if not parallelism_enabled(config):
        return 0, False, (
            "concurrency.enabled is false - set it in ~/.no_human/config.yaml "
            "to run the pool, or pass --max-workers N to enable it for this "
            "run. Refusing to serve a pool that was not asked for."
        )
    workers = int(conc.get("max_workers", 2) or 2)
    if workers > 1 and not isolated:
        # The refusal quotes the width the operator configured, not the
        # clamped one: they need to recognise the number they wrote.
        return 0, False, _no_isolation(workers)
    return clamp_pool_width(workers, cpu_count=cpu_count)[0], True, None


def bounded_xdist_workers(
    max_workers: int, cpu_count: int, existing: str | None,
) -> str | None:
    """The value to set for PYTEST_XDIST_AUTO_NUM_WORKERS, or None to leave it.

    N parallel tasks each running `pytest -n auto` spawn N×cpu workers on cpu
    cores (2026-07-11: 3×12 on 12 → every run timed out). Bound each task's
    auto count to cpu//max_workers. Serial mode (max_workers≤1) and an
    already-set value are left untouched — never lower an explicit choice."""
    if max_workers <= 1 or existing:
        return None
    return str(max(1, (cpu_count or 2) // max_workers))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _summarize_event(event: dict) -> str | None:
    """Extract a short human-readable summary from an event dict.

    Returns None for events that are not worth showing as a live status.
    """
    kind = event.get("kind", "")
    text = event.get("text", "")

    if kind == "tool_use":
        tool = event.get("tool_name") or text.split(" ", 1)[0] or "tool"
        inp = event.get("tool_input") or {}
        path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
        basename = path.rsplit("/", 1)[-1] if path else ""
        if tool in ("Read", "View"):
            return f"reading {basename}" if basename else "reading file"
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            return f"editing {basename}" if basename else "editing file"
        if tool in ("Bash", "Terminal"):
            cmd = inp.get("command", "")[:60]
            return f"running: {cmd}" if cmd else "running command"
        if tool == "Search":
            return f"searching: {inp.get('query', '')[:50]}"
        return f"{tool.lower()} {basename}".strip()

    if kind == "state":
        return text  # e.g. "implementing", "reviewing"
    if kind == "commit":
        return "committing changes"
    if kind in ("tests", "lint"):
        return f"running {kind}"
    if kind in ("review_start", "review"):
        return "reviewing code"
    if kind == "attempt_start":
        return text  # e.g. "attempt 1/3"
    if kind == slot_wait.KIND:
        return "waiting for a worker slot"
    if kind == "context_gather" or kind == "context":
        return "gathering context"
    if kind == "supervisor":
        return "supervisor check"
    if kind == "decompose":
        return "decomposing into sub-tasks"
    return None


class SiblingSchedulerRunning(RuntimeError):
    """Raised by `Scheduler._claim_pool_lease` when a live sibling scheduler
    already owns this database's pool.

    A second `nh start`/`nh serve` sharing the same DB must not boot its own
    pool alongside one that is already running it: the two would
    duplicate-claim tasks, and — the incident this whole fix responds to
    (6408aba0, task 2cc879d5) — the second process's STARTUP orphan sweep
    used to see the first process's live REVIEWING row as an orphan (its own
    `_inflight` is empty by definition right after boot) and requeue it,
    clobbering a review-PASSED attempt out from under a worker that was
    still running. Refusing to boot here is what makes it safe for
    `Scheduler._recover_orphans`'s liveness gate to stay a per-row heuristic
    instead of a perfect one: a process that never gets past this point never
    reaches the sweep at all.

    Carries the sibling's ``pid``/``host``/``age_s`` so a CLI/API caller can
    print exactly what to stop (or how long until its heartbeat goes stale).
    """

    def __init__(self, *, pid: int, host: str, age_s: float):
        self.pid = pid
        self.host = host
        self.age_s = age_s
        super().__init__(
            f"a no_human scheduler is already running against this database "
            f"(pid {pid} on {host!r}, heartbeat {age_s:.0f}s old) — stop it "
            f"first, or wait for its heartbeat to go stale")


class PoolLeaseUnreadable(RuntimeError):
    """Raised by `Scheduler._claim_pool_lease` when the heartbeat row cannot
    be read after `_LEASE_READ_ATTEMPTS` tries.

    The rule this enforces: an unreadable lease row is a FAILURE, not an
    empty lease. A read error means UNKNOWN, not "nobody holds it" — treating
    the two as the same thing is exactly the fail-open bug this exception
    exists to close (a transient DB blip used to be read as "vacant" and the
    claim wrote over whatever the true holder's row said). The fallback here
    is never "assume nobody holds it": on exhaustion this process refuses to
    claim the pool and does not boot.
    """

    def __init__(self, *, attempts: int, error: Exception):
        self.attempts = attempts
        self.error = error
        super().__init__(
            f"pool lease: could not read the heartbeat row after "
            f"{attempts} attempt(s) ({error!r}) — refusing to claim the "
            f"pool lease and not booting (scheduler.py: an unreadable row "
            f"is a failure, not an empty lease)")


class PoolLeaseLost(RuntimeError):
    """Raised by `Scheduler._claim_pool_lease` when the CAS write does not
    land — the row we read is not the row that is there now.

    Either a sibling won the race between our read and our write (in which
    case the fresh row names who), or the write itself raised. Either way, a
    claim we cannot PROVE landed is not a claim: this process does not treat
    "I tried to write" as "I own the lease" and does not boot.
    """

    def __init__(self, *, reason: str, error: Exception | None = None):
        self.reason = reason
        self.error = error
        suffix = f" ({error!r})" if error is not None else ""
        super().__init__(
            f"pool lease: claim write did not land — {reason}{suffix} — "
            f"refusing to claim the pool lease and not booting")


def _lease_sibling_is_dead(
    *, pid: int, host: str, token: str | None, my_host: str,
) -> bool:
    """`_claim_pool_lease`'s SAME-HOST "is the row's writer provably gone"
    check — the ONLY place this fix changes; `pid_alive` itself is untouched
    (its worktree-safety callers need its err-toward-ALIVE bias, which is
    wrong for this lease-side decision alone).

    `pid_alive` is asked first, exactly as before: if it says dead, the row's
    writer is dead, full stop. What's new sits ON TOP of that: a pid that IS
    alive but whose CURRENT `process_start_token` no longer matches the row's
    is not the row's writer any more — the pid was recycled to a new process
    after the original died, and the original bug (a recycled pid read as a
    live sibling for up to `_HEARTBEAT_STALE_S`) is exactly this case.

    Falls back to the pre-fix, `pid_alive`-only answer whenever a token
    comparison is not possible: a different host, a token-less row (written
    before this column existed, or by a platform `process_start_token` cannot
    read), or a live pid whose current token this host cannot read either.
    Never LESS safe than before — only ever adds a case that was previously a
    false "alive".
    """
    if host != my_host:
        return False
    if not pid_alive(pid):
        return True
    if token is None:
        return False
    current_token = process_start_token(pid)
    if current_token is None:
        return False
    return current_token != token


class Scheduler:
    def __init__(
        self,
        store: Store,
        orchestrator_factory: Callable[..., object],
        *,
        max_workers: int = 2,
        wake_watcher: object | None = None,
        on_event: Callable[[str, str], None] | None = None,
        reanalysis_job: ReanalysisJob | None = None,
        wiki_refresh_job: "WikiRefreshJob | None" = None,
        retirement_job: "RetirementSweepJob | None" = None,
        harvest_job: "HarvestJob | None" = None,
        config: dict | None = None,
        auth_check: Callable[[], None] | None = None,
    ):
        self.store = store
        self.factory = orchestrator_factory
        self.max_workers = max(1, int(max_workers))
        self.wake = wake_watcher
        self._inflight: set[str] = set()
        # Rows `unclaimable_orphans()` found on the run that just decided NOT
        # to report drained (`run_forever(until_empty=True)`) — read by the
        # CLI so it can name the row(s) without re-querying. Empty whenever
        # the last `--until-empty` run drained cleanly, or hasn't run yet.
        self.drain_blocked_by: list[dict] = []
        # Ids this process has already emitted an unended `waiting_for_slot`
        # event for — in-memory dedupe so a continuous wait produces exactly
        # one event, not one per tick. In-memory is correct and sufficient: a
        # process restart re-emits once, which is honest (a new wait period).
        self._waiting_for_slot: set[str] = set()
        # Every id this process has dispatched, kept after it finishes: the
        # drain's exit code is about what THIS run did, not about whatever
        # else is in the database.
        self._dispatched: set[str] = set()
        self._on_event = on_event or (lambda kind, text: None)
        self._quota_cooldown_until: datetime | None = None
        # True only while the CURRENT cooldown was armed by the fleet infra
        # breaker (not by an ordinary single-task quota park) — so `tick()`
        # knows to clear the breaker's streak when THIS cooldown lapses,
        # without also clearing it on every tick a park-driven cooldown ends.
        self._infra_cooldown_active: bool = False
        # The reset time of the most recent quota wall this process knows
        # about, kept AFTER it lapses — unlike `_quota_cooldown_until`, which
        # only tracks a wall while it is still armed. Without this a restarted
        # process (or one whose cooldown just ended) cannot tell "no wall
        # ever" from "the wall passed minutes ago and parks are still sitting
        # behind it" (INCIDENT 2026-08-20 — see `_resume_quota_parks`).
        self._quota_wall: datetime | None = None
        self._quota_wall_profile: str | None = None
        # True on the FIRST tick of any process (so a restart sweeps parked
        # quota tasks before claiming pending) and re-armed whenever a
        # cooldown this process was holding lapses (see `_was_cooling` below).
        self._resume_parks_pending: bool = True
        # Edge detector for "the cooldown just ended", so the resume sweep
        # above is triggered once per lapse, not on every tick the pool
        # happens to be idle.
        self._was_cooling: bool = False
        self.reanalysis = reanalysis_job
        self.wiki_refresh = wiki_refresh_job
        self.retirement = retirement_job
        self.harvest = harvest_job
        self._config = config or {}
        # Re-probed every tick (see config.assert_subscription_mode), not just
        # at startup, so an added credential resumes dispatch without a
        # restart. None = no check configured = today's zero-gate behavior.
        self._auth_check = auth_check
        # Last auth-failure message already advisory-logged, so a stuck
        # server logs ONE line per reason, not one per tick.
        self._auth_advisory: str | None = None
        # Orchestrators whose `run_task` is being awaited right now, so a
        # shutdown can ask each one to checkpoint and requeue
        # (`request_stop_checkpoints` → `Orchestrator.request_server_stop`).
        # Distinct from `_inflight` (ids reserved synchronously at dispatch):
        # this holds the OBJECT, and only for the span of the await.
        self._running: dict[str, object] = {}
        # The actual dispatched asyncio.Task per run, kept until `_run()`'s
        # `finally` block fully unwinds (including the final event-flush
        # teardown). Distinct from `_inflight`, which the finally block clears
        # ONE STEP EARLIER, before `persister.aclose()` runs — so `not
        # inflight` is not a safe "this run is truly done" signal. Test-only
        # seam (`wait_idle()` below); nothing in the running process needs to
        # await a sibling run's completion.
        self._run_tasks: dict[str, asyncio.Task] = {}
        # How long `drain()` waits after a stop before the process exits with
        # attempts still running. A stop asks every session to unwind at its
        # next tool boundary, so the drain normally returns in seconds; the
        # grace covers a session inside ONE long tool call (a full test suite).
        # Past it, that attempt is killed as before and closed by the next
        # `create_attempt` as interrupted.
        self._stop_grace_s = stop_grace_s(self._config)
        # Per-task event log: task_id -> deque of {ts, source, kind, text, ...}
        self._event_log: dict[str, deque] = {}
        self._MAX_EVENTS = 200
        # Events are flushed to SQLite while the run is live, not just at the
        # end: a crash used to lose the entire history, and the in-memory buffer
        # above is capped, so a chatty run silently dropped its earliest events.
        self._EVENT_FLUSH_INTERVAL = 2.0
        # Phase 4a: SSE — per-task notify so streaming clients wake on new events.
        self._event_notify: dict[str, asyncio.Event] = {}
        # Live status: short human-readable summary of what the agent is doing.
        self._live_status: dict[str, str] = {}
        # --- liveness (2026-08-01 incident) ------------------------------- #
        # `inflight: 0` read identically for "queue empty" and "the connection
        # is serving a three-hour-old snapshot and I am crashing 12x/minute".
        # These make the two distinguishable from outside the process.
        self._last_tick_at: float | None = None
        self._last_dispatch_at: float | None = None
        self._last_claimable_count: int | None = None
        self._crash_times: deque = deque(maxlen=500)
        # Set (to the failure reason) the moment a per-tick lease REFRESH
        # fails — never by the startup claim, which raises instead of
        # setting a flag. Once set, `tick()` stops dispatching immediately
        # (guard at its top) and `run_forever` exits the loop: a scheduler
        # that cannot prove it still holds the lease has lost the authority
        # to dispatch, and continuing would be exactly the fail-open bug
        # this module exists to close, one level up (per-tick instead of
        # at-boot).
        self._lease_lost: str | None = None
        self._db_view_stale = False
        self._db_stale_since: float | None = None
        self._status_write_failures = 0
        self._consecutive_status_write_failures = 0
        self._last_status_write_error: str | None = None
        # The probe can fail too, and a detector that fails open is worth
        # nothing: without these, "the probe raised on every tick for three
        # hours" and "the view is fine" are the same JSON. See
        # `_check_db_liveness`.
        self._probe_failures = 0
        self._consecutive_probe_failures = 0
        self._last_probe_error: str | None = None
        # `run_forever` overwrites this with the interval it was actually given.
        self._poll_interval = 10.0
        # When this Scheduler came into existence. The anchor for "has never
        # ticked, and has had long enough that it should have" — without it,
        # "never ticked" is indistinguishable from "started four seconds ago".
        self._created_at = time.time()

    @property
    def inflight(self) -> set[str]:
        return set(self._inflight)

    async def wait_idle(self, timeout: float = 5.0) -> None:
        """Await every currently-tracked dispatched run to actually finish —
        including its background event-flush teardown — not just the moment
        `_inflight` empties (which fires one tick earlier, before the final
        `EventPersister.aclose()` flush; a caller that only waited for that
        can race the store's own teardown, e.g. seeing an intermittent
        "Cannot operate on a closed database" failure). Test-only seam: no
        production caller needs this, since nothing in the running process
        blocks on a sibling task's flush."""
        tasks = [t for t in self._run_tasks.values() if not t.done()]
        if not tasks:
            return
        # `asyncio.wait`, not `wait_for(gather(...))`: a timed-out wait_for
        # cancels the gather and the gather cancels its children — i.e. the
        # scheduler's live `_run` tasks. A waiter that gives up must leave
        # the runs it was watching untouched.
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            raise TimeoutError(
                f"{len(pending)} dispatched run(s) still live after {timeout}s")

    @property
    def quota_cooldown_until(self) -> datetime | None:
        """The pool-wide cooldown's reset time when the CURRENT cooldown was
        armed by an ordinary quota park, or None if not paused or paused by
        the infra breaker instead. One clock (`_quota_cooldown_until`), two
        causes; this and `infra_cooldown_until` are never both non-None —
        the reader names the cause by which property answers. Read by
        `/api/queue/health` — the single clock, never re-derived."""
        return None if self._infra_cooldown_active else self._quota_cooldown_until

    @property
    def infra_cooldown_until(self) -> datetime | None:
        """The pool-wide cooldown's reset time when it was armed by the fleet
        infra breaker (3 consecutive zero-token/auth SDK failures), else
        None. Read by `/api/queue/health` to label the pause "infra" instead
        of misattributing it to a stale quota park."""
        return self._quota_cooldown_until if self._infra_cooldown_active else None

    def get_live_status(self, task_id: str) -> str | None:
        """Return the latest live status summary for a task, or None."""
        return self._live_status.get(task_id)

    def _in_quota_cooldown(self, now: datetime) -> bool:
        return self._quota_cooldown_until is not None and now < self._quota_cooldown_until

    async def recover_quota_cooldown(self) -> datetime | None:
        """Re-derive an open quota wall from the DB, at startup, before any
        dispatch. Returns the reset time it armed, or None.

        INCIDENT (2026-08-20, task 426fe079): the cooldown lived only in
        process memory, armed when a dispatch of THIS process died on the
        wall (`_run`). A restart forgot the wall while the tasks table still
        held five ``paused_quota`` rows naming its reset time, so the new
        process fed four pending tasks straight into it — four dead SDK
        sessions, four more parks, for a wall the DB already knew.

        Same source of truth as the live arming: the NEWEST ``paused_quota``
        park's ``wake_check_at`` (the reset time `_park_quota` computed for
        it). Newest by when it was RAISED (``blocker.raised_at``, falling back
        to the row's ``updated_at``) — that is the park the dead process armed
        last and the wall as the vendor last described it; an older park with
        a longer stamp is history, not a longer wall. A stamp in the past, an
        unparseable stamp, or no park at all arms nothing; a cooldown this
        process already holds is never shortened (the LIVE arming in `_run`
        overwrites unconditionally — that is a fresh wall, this is a
        remembered one). Resume from the world, not from what a dead process
        remembered.

        Whose wall: a park stamped ``auth_profile`` (written by `_park_quota`)
        counts only when it matches the profile THIS process exported
        (`active_auth_profile`) — ``nh auth use <other>`` + restart is the
        operator's sanctioned way past a wall and must not idle for the old
        profile's hour. A park with no stamp cannot be attributed and is
        honoured: <=1 h idle is the cheaper mistake against N dead sessions.
        """
        now = datetime.now(timezone.utc)
        mine = active_auth_profile()
        newest_raised: datetime | None = None
        newest: datetime | None = None
        newest_profile: str | None = None
        for task in await self.store.list_tasks(TaskStatus.PAUSED_QUOTA):
            resets = _parse_iso(getattr(task, "wake_check_at", None))
            if resets is None:
                continue
            blocker = task.blocker if isinstance(task.blocker, dict) else {}
            if blocker.get("infra"):
                continue                      # a dead session, not a wall
            theirs = blocker.get("auth_profile")
            if theirs and mine and theirs != mine:
                continue                      # another profile's wall
            raised = (_parse_iso(blocker.get("raised_at"))
                      or _parse_iso(getattr(task, "updated_at", None))
                      or datetime.min.replace(tzinfo=timezone.utc))
            if newest_raised is None or raised > newest_raised:
                newest_raised, newest, newest_profile = raised, resets, theirs
        if newest is not None:
            # Remembered even when it has already passed — a lapsed wall is
            # exactly what `_resume_quota_parks` needs to sweep the parks
            # behind it, and this is the only place that knows it.
            self._quota_wall = newest
            self._quota_wall_profile = newest_profile
        if newest is None or newest <= now:
            return None
        if self._quota_cooldown_until is not None and self._quota_cooldown_until >= newest:
            return newest
        self._quota_cooldown_until = newest
        # One clock, two causes: what this restores is BY DEFINITION a
        # remembered QUOTA wall (infra parks are skipped above via
        # `blocker.get("infra")`), so the label must be re-set here, not
        # inherited from whatever armed the clock before. Every writer of
        # `_quota_cooldown_until` writes `_infra_cooldown_active` beside it —
        # the invariant holds per-site, not by call ordering (a fresh
        # Scheduler happening to have the flag False).
        self._infra_cooldown_active = False
        whose = f"'{newest_profile}' profile" if newest_profile else "an unattributed park"
        self._on_event("quota_pause",
                       f"pool paused until {newest.isoformat()} (recovered from "
                       f"the DB at startup — {whose}'s wall, parked before this "
                       "process; `nh auth use <other>` + restart clears it)")
        return newest

    def _park_wall_passed(self, task, blocker: dict, *, now: datetime) -> bool:
        """Whether a PAUSED_QUOTA task's wall has passed — the resumability
        gate `_resume_quota_parks` uses.

        Primary: the wall THIS process has resolved (`_quota_wall`, kept even
        after it lapses — see `recover_quota_cooldown` and the live arming in
        `_run`). If it has passed and the park was raised no later than it,
        the park is behind THAT wall regardless of its own fixed
        `wake_check_at` — this is the 2026-08-20 incident: a park's own clock
        (raise time + a fixed hour) can still read minutes in the future
        while the pool's wall has already reset.

        Fallback, when this process has never resolved a wall: the park's
        own `wake_check_at`, due or due within one poll interval.
        Deliberately does NOT parse the free-text `root_cause_hypothesis` —
        `bounds.py`'s decision not to trust that phrasing is out of scope
        here and stays exactly as it is.
        """
        wall = self._quota_wall
        if wall is not None and wall <= now:
            raised = (_parse_iso(blocker.get("raised_at"))
                      or _parse_iso(getattr(task, "updated_at", None)))
            if raised is None or raised <= wall:
                return True
        wake = _parse_iso(getattr(task, "wake_check_at", None))
        if wake is not None and wake <= now + timedelta(seconds=self._poll_interval):
            return True
        return False

    async def _resume_quota_parks(self, *, now: datetime) -> list[str]:
        """Resume every quota-parked task whose wall has passed, before the
        next claim.

        INCIDENT (2026-08-20): the server restarted at 14:25:32 UTC while
        four tasks sat ``paused_quota`` with their own fixed
        ``wake_check_at`` at 14:29 (each park's raise-time-plus-an-hour
        clock — the wall itself had reset at 14:20). The fresh scheduler
        filled all four worker slots with brand-new PENDING tasks at
        14:25:43; the parks — two carrying open draft PRs — then sat
        ``implementing`` with zero events for 26-47 minutes behind four
        fresh attempts. Neither startup nor a cooldown lapse used to
        consult ``PAUSED_QUOTA`` at all: `_CLAIMABLE` only orders
        IMPLEMENTING/PENDING, and a park is neither until the WakeWatcher's
        own timer flips it — which was still four minutes away.

        INCIDENT (2026-08-23): a restart onto a DIFFERENT auth profile
        (`nh auth use <other>` + restart) stranded three parks (one HIGH)
        for ~1h while fresh PENDING tasks filled the freed slots. The
        profile switch left this process's OWN cooldown clock clear
        (`recover_quota_cooldown` correctly never arms it from another
        profile's wall), but this loop was unconditionally skipping every
        row whose ``auth_profile`` didn't match the one now active, with no
        fallback — so those parks were left to their own hour-long
        `wake_check_at` timer instead. Once THIS process is not in cooldown
        (checked below, before the loop even starts), a park's OTHER
        profile's wall is moot: the task will retry under the CURRENT
        profile, which is not walled, so it is resumable now rather than
        stranded behind fresh dispatch.

        Returns the ids actually resumed (flipped to IMPLEMENTING). A live
        wall (`_in_quota_cooldown`) or no wake mechanism wired both mean
        "nothing to do" and this returns ``[]`` without touching a row.
        """
        if self._in_quota_cooldown(now) or self.wake is None:
            return []
        resumed: list[str] = []
        mine = active_auth_profile()
        for task in await self.store.list_tasks(TaskStatus.PAUSED_QUOTA):
            try:
                blocker = task.blocker if isinstance(task.blocker, dict) else {}
                if (blocker.get("category") or "").upper() != "QUOTA":
                    continue
                theirs = blocker.get("auth_profile")
                # A park behind a DIFFERENT profile's wall skips the wall
                # checks below entirely: this process is already confirmed
                # not in cooldown (for the CURRENT profile) above, and the
                # other profile's own wall status is moot once the task is
                # about to retry under a profile that isn't walled.
                #
                # An INFRA park (`blocker.infra`, a dead SDK session — see
                # `test_infra_park_is_not_a_wall.py`) carries the SAME
                # "QUOTA" category and can carry an `auth_profile` stamp too,
                # but it is not a billing wall at all: nothing about a
                # profile switch makes a dead transport come back to life.
                # Routing it through the cross-profile fast path resumed it
                # unconditionally on every restart-onto-a-new-profile,
                # recreating the retry-wave class this file's other incident
                # note exists to prevent — it must fall through to the same
                # wall/own-clock gate every other park uses below.
                cross_profile = bool(theirs and mine and theirs != mine
                                      and not blocker.get("infra"))
                if not cross_profile:
                    if (theirs and self._quota_wall_profile
                            and theirs != self._quota_wall_profile):
                        continue                  # behind a DIFFERENT wall
                    if not self._park_wall_passed(task, blocker, now=now):
                        continue
                action = await self.wake.resume_now(task, now=now)
                if action == "resumed":
                    resumed.append(task.id)
            except Exception as exc:  # noqa: BLE001 — sweep must not kill the pool
                log.warning("quota-park resume failed for %s: %s", task.id[:8], exc)
        if resumed:
            self._on_event(
                "quota_resume",
                f"resumed {len(resumed)} quota-parked task(s) — wall passed")
        return resumed

    async def _claimable(self) -> list:
        out = []
        for status in _CLAIMABLE:
            rows = [t for t in await self.store.list_claimable_tasks(status)
                    if t.id not in self._inflight]
            if status is TaskStatus.PENDING and rows:
                rows = await self._rank_pending(rows)
            out.extend(rows)
        # A plan-approval correction resumes into PLANNING, not IMPLEMENTING —
        # it must be re-planned before a token is spent implementing it. That
        # status is otherwise mid-run-only, so it is claimed here on the one
        # marker a live planning worker can never carry: a correction a human
        # left on a parked task (`plan_gate.correcting` — keyed on the STATE,
        # never on the correction text: a blank answer used to write the state
        # with empty text, which this claim missed and the orphan sweep below
        # then turned into a gate bypass).
        for status in _CORRECTION_CLAIMABLE:
            for t in await self.store.list_claimable_tasks(status):
                if t.id not in self._inflight and plan_gate.correcting(t):
                    out.append(t)
        return out

    async def _rank_pending(self, rows: list) -> list:
        """Split an oldest-first PENDING batch into prior-work tasks (a
        burned attempt, or a PR/resume marker in context — `_has_prior_work`)
        ahead of never-started ones; within each group, order by priority
        (high > medium > low), FIFO preserved within equal priority.

        Three-key order: prior-work split, then priority, then FIFO. Priority
        only reorders the PENDING queue — it never preempts a task that is
        already claimed/running, and quota-parked resumes (claimed through a
        different status ahead of PENDING in `_claimable`) never reach here.

        No aging term: this is a plain priority sort re-run every tick, not a
        priority queue with fairness built in. A `low` task is starved for as
        long as the `medium`/`high` stream stays non-empty, and `medium` by a
        `high` stream, same as FIFO's own starvation of a task behind an
        unbounded queue — priority just narrows which tier that can happen
        within. Deliberate for this landing: no operator has asked for aging
        yet, and it is easy to bolt on later (e.g. a wait-time term in the
        sort key) without touching the three-key order above.

        `attempt_counts()` is one grouped query, fetched here (only when the
        pending group is non-empty) rather than N per-task lookups. Any
        failure falls back to ranking by context markers alone — the sweep
        must never break the claim path over a ranking nicety.
        """
        try:
            attempt_counts = await self.store.attempt_counts()
        except Exception as exc:  # noqa: BLE001 — ranking must never break claim
            log.warning("attempt_counts() failed, ranking pending by context "
                        "markers only: %s", exc)
            attempt_counts = {}
        prior, fresh = [], []
        for t in rows:
            (prior if _has_prior_work(t, attempt_counts) else fresh).append(t)
        prior.sort(key=lambda t: priority_rank(getattr(t, "priority", None)))
        fresh.sort(key=lambda t: priority_rank(getattr(t, "priority", None)))
        return prior + fresh

    # Mid-run statuses only a live worker can hold. A task found in one of
    # these with NO recent activity (`_row_is_live`, below) was orphaned by a
    # crash/kill of its worker's process. NOT every task found in one of
    # these AT STARTUP: that used to be the whole test (this process's
    # `_inflight` is empty right after boot, so it looked like proof) and it
    # is false the moment a second process shares this database — see
    # `_recover_orphans` for incident 6408aba0, where exactly that let a
    # second process's startup sweep clobber a first process's live,
    # review-PASSED row. `_claim_pool_lease` now refuses to let a second
    # process reach this sweep at all while a sibling's heartbeat is live;
    # `_row_is_live` is the defense-in-depth layer under that refusal.
    _ORPHANABLE = (TaskStatus.CONTEXT, TaskStatus.PLANNING,
                   TaskStatus.REVIEWING, TaskStatus.TESTING)

    # Runtime sweep only: a mid-run row whose last persisted ACTIVITY (row
    # updated_at OR newest task event — a live run keeps flushing events from
    # whatever process drives it, even one this scheduler knows nothing
    # about) is younger than this is left alone. Generous on purpose: a
    # genuinely stranded row otherwise waits forever, so fifteen minutes
    # costs little, while a live out-of-process run's longest silent stretch
    # (a full-suite pytest inside review) must fit under it.
    #
    # RECONCILIATION with `_HEARTBEAT_STALE_S` below: the two constants
    # answer different questions about the same table and are DELIBERATELY
    # different, not an oversight. This one asks "may I requeue this row" —
    # requeueing a row a live sibling still owns is destructive (incident
    # 6408aba0), so it is generous: a row younger than this may still be
    # live, so never touch it. The other asks "may I take over the lease
    # itself", where a stale value costs only a loud, safe refusal to boot —
    # so it is short. The DOCUMENTED, DELIBERATE consequence of the gap: a
    # holder that is alive but quiet (wedged, suspended, or just slow) for
    # more than `_HEARTBEAT_STALE_S` (300s) can have its LEASE taken over by
    # a new boot while this sweep would still call its mid-run rows live
    # (they stay untouched for a further 600s). Closing that gap by
    # refusing takeover for any same-host pid `pid_alive` reports alive is
    # NOT done here: it would change the PR #585 takeover behaviour that
    # `tests/test_status_clobber.py::test_a_stale_sibling_heartbeat_is_taken_over`
    # pins (a live parent pid at age 600s IS taken over there, on purpose).
    # The 600s divergence this creates is an ACCEPTED design decision, not a
    # deferral — see docs/design/lease-takeover-vs-orphan-grace.md and
    # `_LEASE_ORPHAN_DIVERGENCE_S` below; `tests/test_scheduler_lease_orphan_window.py`
    # pins it.
    _STRANDED_GRACE_S = 900.0

    # `_claim_pool_lease` only: how old a sibling's heartbeat may be before
    # this process treats it as gone rather than live. Deliberately shorter
    # than `_STRANDED_GRACE_S` above — that window is generous because
    # requeueing a live row is destructive (incident 6408aba0) and a false
    # "still stranded" costs only a delayed recovery, but a stale value HERE
    # means an operator who killed a process and immediately restarted waits
    # out the whole window before the new one will boot, and that failure
    # mode is loud (an explicit refusal to start) rather than silent, so
    # erring toward a shorter wait costs less.
    #
    # See the reconciliation note on `_STRANDED_GRACE_S` above: this value
    # is a THIRD of that one, on purpose, and the gap it opens (a live-but-
    # quiet holder can lose the LEASE well before its mid-run rows would be
    # considered orphaned) is an ACCEPTED design decision, not a deferral —
    # see docs/design/lease-takeover-vs-orphan-grace.md and
    # `_LEASE_ORPHAN_DIVERGENCE_S` below; `tests/test_scheduler_lease_orphan_window.py`
    # pins it.
    _HEARTBEAT_STALE_S = 300.0

    # The ACCEPTED divergence between the two questions above, as a first-class
    # value rather than an arithmetic accident: for this many seconds a holder
    # that is alive but quiet is "too stale to keep the lease" and simultaneously
    # "too recently active to have its rows requeued". Both halves err toward not
    # destroying live work, which is why this is accepted rather than closed.
    # Derived, never hand-typed: change either constant and the derivation moves
    # with it, and the pin in tests/test_scheduler_lease_orphan_window.py fails so
    # the design doc gets re-read instead of silently going stale.
    _LEASE_ORPHAN_DIVERGENCE_S = _STRANDED_GRACE_S - _HEARTBEAT_STALE_S

    # `_claim_pool_lease`'s read step only: how many times to retry a read
    # that raised before refusing to claim. Bounded and short — this
    # accommodates a genuinely transient DB hiccup without turning an
    # unreadable row into a long stall before the honest refusal.
    _LEASE_READ_ATTEMPTS = 3
    _LEASE_READ_BACKOFF_S = 0.05

    async def _is_terminal_row(self, task) -> bool:
        """Re-read the live row; terminal = DONE, or FAILED with a cancel
        reason (there is no separate 'cancelled' status). Mirrors
        `blockers.wake.WakeWatcher._is_terminal` — the shared shipped-check
        below requires a callable, and the scheduler must not import the
        watcher class just to reuse its terminal check."""
        current = await self.store.get_task(task.id)
        if current is None:
            return False
        if current.status == TaskStatus.DONE:
            return True
        return current.status == TaskStatus.FAILED and bool(
            (current.context or {}).get("cancel_reason"))

    async def _shipped_before_dispatch(self, task) -> bool:
        """True when the task's work is already on its base — completed here
        instead of burning a fresh attempt. False on EVERY ambiguity: no probe
        wired, no recorded base/PR, probe error, or content genuinely absent.
        Fail open to dispatching a fresh attempt, never to silent completion.

        This is the fix for the live incident: a restart's resume/orphan
        machinery used to decide from task STATUS alone, so a task whose PR
        had already closed-landed still got a fresh `attempt 1/3` ~20 minutes
        later. The check here is the SAME shared function
        (`blockers.shipped.complete_if_content_landed`) the `pr_closed` rung
        uses — one check, both callers, not a copy.
        """
        probe = getattr(self.wake, "pr_shipped", None)
        if probe is None:
            return False
        ctx = task.context or {}
        if not ctx.get("base_branch") or not task.repo_path:
            # Cheap pre-filter: never spawn git for a task with no PR history.
            return False
        try:
            pr = await resolve_task_pr(self.store, task)
            if not pr.url:
                return False
            res = await complete_if_content_landed(
                self.store, task, pr.url,
                pr_shipped=probe, is_terminal=self._is_terminal_row,
                on_event=self._on_event, forge_state="",
                action="shipped_before_resume",
                situation="a resumed attempt was about to start",
                branch=pr.branch or None,
            )
        except Exception as exc:  # noqa: BLE001 — the gate must never block dispatch
            log.warning("shipped-before-dispatch check failed for %s: %s",
                        task.id[:8], exc)
            return False
        if res is _TICK_ABORTED:
            log.info("task %s went terminal while the shipped check ran — "
                      "skipping dispatch without rewriting it", task.id[:8])
            return True
        if res is None:
            return False
        log.info("task %s: content already on %s — completed instead of "
                  "dispatching attempt 1", task.id[:8], ctx.get("base_branch"))
        return True

    async def _reconcile_landed_orphan(self, t) -> bool:
        """True when an about-to-be-requeued orphan's attempt already landed
        — its PR merged, or its commit is an ancestor of the base branch —
        in which case it is reconciled straight to DONE instead of being
        requeued (and eventually failed) on top of work that already shipped.

        Called from `_recover_orphans`, AFTER `_row_is_live` (unchanged: a
        row something is still actively working must never be touched here)
        and BEFORE the existing unvalidated-requeue `set_status(IMPLEMENTING,
        ...)` write — a `True` return means the caller should `continue` its
        loop instead of requeueing.

        Fails open on every ambiguity, exactly like `_shipped_before_dispatch`
        beside it: no repo path, no base branch, no PR/commit to check, a git
        error, or a status outside `LANDED_RECONCILABLE` all fall through to
        `False` (requeue as normal, today's behaviour, unchanged). The probe
        itself (`vcs.pr_watcher.orphan_landed_evidence`) is LOCAL-GIT-ONLY —
        no forge/network call happens on this path, sweep or otherwise.
        """
        try:
            if t.status not in LANDED_RECONCILABLE:
                return False
            ctx = t.context or {}
            base = ctx.get("base_branch")
            if not base or not t.repo_path:
                # Cheap pre-filter: never spawn git for a row with no base
                # branch recorded (same shape as `_shipped_before_dispatch`).
                return False
            pr_url = await self.store.latest_attempt_pr_url(t.id)
            branch_info = await self.store.latest_attempt_branch(t.id)
            commit_sha = (branch_info or {}).get("commit_sha") or ""
            if not commit_sha and not pr_url:
                return False
            evidence = await orphan_landed_evidence(
                t.repo_path, base, commit_sha=commit_sha, pr_url=pr_url)
            if not evidence:
                return False
            # Re-check terminality: the probe just ran arbitrary-duration git
            # subprocesses, during which a human could have cancelled or
            # completed this row out from under us (mirrors
            # `_shipped_before_dispatch`'s post-probe re-check).
            if await self._is_terminal_row(t):
                return True
            current = await self.store.get_task(t.id)
            if current is None:
                return False
            reconciled = await self.store.reconcile_landed_orphan(
                current, evidence=evidence,
                event={
                    "source": "orchestrator", "kind": "orphan_reconciled",
                    "text": (f"reconciled: work landed while orphaned "
                             f"({evidence['kind']} {evidence['sha'][:8]} "
                             f"on {base}) — {t.id[:8]} completed instead of "
                             "requeued"),
                    "ts": time.time(),
                },
            )
            if reconciled is None:
                return False
            self._on_event(
                "orphan_reconciled",
                f"{t.id[:8]} was orphaned but had already landed on {base} — "
                "reconciled to DONE instead of requeued")
            return True
        except Exception as exc:  # noqa: BLE001 — the sweep must never block on this
            log.warning("landed-orphan reconciliation failed for %s: %s",
                        t.id[:8], exc)
            return False

    async def _terminal_landed_evidence(self, t, base: str) -> dict | None:
        """Gather + probe evidence for one TERMINAL candidate row — the
        per-row body factored out of `_reconcile_landed_terminal` to keep
        that method's complexity down.

        Candidate shas come from two places: a free-text `cancel_reason`
        (via `landing_sha_candidates` — regex only, never resolved through a
        forge API) and the row's own last attempt commit. Each candidate is
        tried against `orphan_landed_evidence` alongside the recorded
        `pr_url`, first truthy result wins. If there are no sha candidates at
        all but a `pr_url` is recorded, one call is made with `commit_sha=""`
        so the PR's squash-subject scan still runs. No candidates and no
        `pr_url` — `None`, no git subprocess spawned.
        """
        ctx = t.context or {}
        pr_url = await self.store.latest_attempt_pr_url(t.id)
        branch_info = await self.store.latest_attempt_branch(t.id)
        commit_sha = (branch_info or {}).get("commit_sha") or ""
        candidates = landing_sha_candidates(str(ctx.get("cancel_reason") or ""))
        if commit_sha and commit_sha not in candidates:
            candidates = [*candidates, commit_sha]
        if candidates:
            for cand in candidates:
                evidence = await orphan_landed_evidence(
                    t.repo_path, base, commit_sha=cand, pr_url=pr_url)
                if evidence:
                    return evidence
            return None
        if pr_url:
            return await orphan_landed_evidence(
                t.repo_path, base, commit_sha="", pr_url=pr_url)
        return None

    async def _reconcile_one_landed_terminal(self, t) -> bool:
        """True when a TERMINAL (failed/cancelled) row's recorded work is
        provably reachable from its base branch, in which case it is
        reconciled to DONE via `Store.reconcile_landed_terminal` instead of
        staying failed forever. The TERMINAL-row twin of
        `_reconcile_landed_orphan` — same fail-open-on-ambiguity contract,
        same LOCAL-GIT-ONLY probe, same post-probe re-check before writing.
        """
        try:
            if t.status not in TERMINAL_LANDED_RECONCILABLE:
                return False
            ctx = t.context or {}
            base = ctx.get("base_branch")
            if not base or not t.repo_path:
                # Never guess a base branch — a row without one is skipped
                # untouched, same as `_reconcile_landed_orphan`.
                return False
            evidence = await self._terminal_landed_evidence(t, base)
            if not evidence:
                return False
            # Re-check terminality: the probe just ran arbitrary-duration git
            # subprocesses, during which a human could have restored/retried
            # this row out from under us.
            current = await self.store.get_task(t.id)
            if current is None or current.status is not TaskStatus.FAILED:
                return False
            reconciled = await self.store.reconcile_landed_terminal(
                current, evidence=evidence,
                event={
                    "source": "orchestrator", "kind": "terminal_reconciled",
                    "text": (f"reconciled: work landed while failed/cancelled "
                             f"({evidence['kind']} {evidence['sha'][:8]} "
                             f"on {base}) — {t.id[:8]} completed instead of "
                             "staying failed"),
                    "ts": time.time(),
                },
            )
            if reconciled is None:
                return False
            self._on_event(
                "terminal_reconciled",
                f"{t.id[:8]} was failed/cancelled but had already landed on "
                f"{base} — reconciled to DONE")
            return True
        except Exception as exc:  # noqa: BLE001 — the sweep must never block on this
            log.warning("landed-terminal reconciliation failed for %s: %s",
                        t.id[:8], exc)
            return False

    async def _reconcile_landed_terminal(self, *, limit: int = 200) -> int:
        """Startup-only: complete TERMINAL failed/cancelled rows whose
        recorded work already landed on their base branch, instead of
        leaving them failed forever with no path back to DONE.

        This is `TERMINAL_LANDED_RECONCILABLE` / `Store.reconcile_landed_terminal`'s
        sweep — the terminal-row twin of `_reconcile_landed_orphan`, which
        only ever runs against non-terminal (`LANDED_RECONCILABLE`) rows
        found by `_recover_orphans` and is otherwise left untouched by this
        method. Runs once at startup, not on a per-tick cadence: each
        candidate row can cost several git subprocesses
        (`_terminal_landed_evidence`), and `landed_sha IS NULL` in
        `Store.landed_reconcilable_terminal_tasks` already makes a repeat
        pass a cheap no-op, so there is no benefit to running this more
        often than once per boot. Must never block boot — every failure
        mode here is caught and logged, never raised.
        """
        try:
            candidates = await self.store.landed_reconcilable_terminal_tasks(
                limit=limit)
        except Exception:  # noqa: BLE001 — the sweep must never block boot
            log.exception("startup: fetching landed-terminal candidates failed")
            return 0
        reconciled = 0
        for t in candidates:
            if await self._reconcile_one_landed_terminal(t):
                reconciled += 1
        if reconciled:
            log.info(
                "startup: reconciled %d terminal row(s) whose recorded work "
                "had already landed", reconciled)
        return reconciled

    async def _read_heartbeat_with_retry(self) -> dict | None:
        """Read the id=1 heartbeat row, retrying a bounded number of times on
        exception before giving up. A read failure is UNKNOWN, not "no row"
        — the caller must never treat the two as the same thing (that is the
        fail-open bug `PoolLeaseUnreadable` exists to close), so this raises
        rather than returning `None` on exhaustion."""
        last_exc: Exception | None = None
        for attempt in range(1, self._LEASE_READ_ATTEMPTS + 1):
            try:
                return await self.store.read_scheduler_heartbeat()
            except Exception as exc:  # noqa: BLE001 — retried below, then raised
                last_exc = exc
                log.warning(
                    "pool lease: read attempt %d/%d failed: %s",
                    attempt, self._LEASE_READ_ATTEMPTS, exc)
                if attempt < self._LEASE_READ_ATTEMPTS:
                    await asyncio.sleep(
                        self._LEASE_READ_BACKOFF_S * 2 ** (attempt - 1))
        log.error(
            "pool lease: could not read the heartbeat row after %d "
            "attempt(s) — refusing to claim the pool lease and not booting",
            self._LEASE_READ_ATTEMPTS)
        raise PoolLeaseUnreadable(
            attempts=self._LEASE_READ_ATTEMPTS, error=last_exc)

    async def _claim_pool_lease(self) -> None:
        """Refuse to boot this pool while a sibling scheduler's heartbeat on
        this same database is live — the primary defense for incident
        6408aba0 (see `_recover_orphans`): a process that never gets past
        this raise never reaches the orphan sweep at all, so it cannot
        requeue a row a live sibling still owns.

        A single-row (`id=1`) heartbeat table is the leader marker. Reading
        it (bounded-retry: `_read_heartbeat_with_retry`):
          - no row, or a row already stamped with OUR (pid, host) — claim/
            refresh it and return; `started_at` is carried forward from the
            existing row on a refresh so it still names when THIS run
            actually started, not when it last ticked.
          - a row stamped with a DIFFERENT (pid, host) that is fresh (younger
            than `_HEARTBEAT_STALE_S`) and not provably dead — raise
            `SiblingSchedulerRunning`, naming that pid/host/age so an
            operator (or the CLI/API catching this) knows exactly what to
            stop.
          - otherwise (stale, or a same-host pid `_lease_sibling_is_dead`
            proves is gone — see there) — the sibling is presumed gone; take
            the lease over.

        `pid_alive` (via `_lease_sibling_is_dead`) is only trusted for a
        SAME-HOST pid: a pid number from a different host means nothing here
        and is never treated as dead on that evidence alone. Every claim
        this process writes also carries its own `process_start_token`
        (module-level, `config.py`) alongside the pid — so a FUTURE reader
        can tell a genuinely live same-host sibling from a new process that
        merely reused a recycled pid within `_HEARTBEAT_STALE_S`.

        FAILS CLOSED, on both ends of this call:
          - a read that never succeeds raises `PoolLeaseUnreadable` — an
            unreadable row is a failure, never "vacant", so nothing below
            this point ever writes. `nh serve`/`nh start` catch this
            alongside `SiblingSchedulerRunning` and print the reason in red,
            then exit 1 — the operator sees WHY, not a traceback and a
            silently duplicate-claimed pool.
          - the write is a CAS (`Store.cas_scheduler_heartbeat`), conditioned
            on the row still being exactly what was read. If it does not
            land — the row moved under us — this process does NOT retry the
            write blindly (the winner may be a live sibling that now
            legitimately owns the lease): it re-reads once and either raises
            `SiblingSchedulerRunning` (a live interloper won the race) or
            `PoolLeaseLost` (anything else changed). A CAS write that raises
            is likewise `PoolLeaseLost`, not a swallowed warning — a claim
            this process cannot PROVE landed is not a claim.
        """
        my_pid = os.getpid()
        my_host = platform.node()
        my_token = process_start_token(my_pid)
        now = time.time()
        row = await self._read_heartbeat_with_retry()

        mine = row is not None and row["pid"] == my_pid and row["host"] == my_host
        if row is not None and not mine:
            age = now - float(row["ts"])
            sibling_dead = _lease_sibling_is_dead(
                pid=int(row["pid"]), host=row["host"],
                token=row.get("start_token"), my_host=my_host)
            if age < self._HEARTBEAT_STALE_S and not sibling_dead:
                raise SiblingSchedulerRunning(
                    pid=int(row["pid"]), host=row["host"], age_s=age)
            log.warning(
                "pool lease: taking over the lease held by pid %s on %s "
                "(heartbeat age %.0fs >= %.0fs). If that process is alive "
                "but quiet, its mid-run rows stay protected for up to a "
                "further %.0fs (_LEASE_ORPHAN_DIVERGENCE_S) — an accepted "
                "window, see docs/design/lease-takeover-vs-orphan-grace.md",
                row["pid"], row["host"], age, self._HEARTBEAT_STALE_S,
                self._LEASE_ORPHAN_DIVERGENCE_S)

        started_at = (row["started_at"] if mine
                      else datetime.now(timezone.utc).isoformat())
        try:
            landed = await self.store.cas_scheduler_heartbeat(
                pid=my_pid, host=my_host, started_at=started_at, ts=now,
                start_token=my_token, expect=row)
        except Exception as exc:  # noqa: BLE001 — cannot prove the claim landed
            raise PoolLeaseLost(
                reason="the CAS write raised", error=exc) from exc
        if landed:
            return

        # The row was not what we read any more — re-read ONCE (never a
        # blind retry of the write) to say exactly what happened.
        current = await self._read_heartbeat_with_retry()
        if current is not None:
            current_mine = (current["pid"] == my_pid
                             and current["host"] == my_host)
            if not current_mine:
                age = now - float(current["ts"])
                sibling_dead = _lease_sibling_is_dead(
                    pid=int(current["pid"]), host=current["host"],
                    token=current.get("start_token"), my_host=my_host)
                if age < self._HEARTBEAT_STALE_S and not sibling_dead:
                    raise SiblingSchedulerRunning(
                        pid=int(current["pid"]), host=current["host"],
                        age_s=age)
        raise PoolLeaseLost(
            reason=f"expected row {row!r}, found {current!r} after the CAS "
                   f"write was rejected")

    async def _reconcile_terminal_task_attempts(self) -> None:
        """Startup-only: retire attempt rows left open on tasks that finished.

        Separate from `_recover_orphans` on purpose. That sweep iterates
        `_ORPHANABLE`, i.e. NON-terminal task statuses, so a `done`/`failed`
        task holding an `in_progress` row is invisible to it — which is why 42
        such rows had accumulated by 2026-08-11, the oldest open for 32 days,
        and why one task stayed hidden behind one for nine days. See
        `Store.close_attempts_of_terminal_tasks` for why this is startup-only.
        """
        try:
            n = await self.store.close_attempts_of_terminal_tasks()
        except Exception:  # noqa: BLE001 — reconciliation must never block boot
            log.exception("startup: reconciling terminal-task attempts failed")
            return
        if n:
            # Say it out loud. A silent reconciliation of 42 rows is
            # indistinguishable from a bug that closed rows it should not have.
            log.info("startup: retired %d attempt row(s) left open on tasks "
                     "that had already finished", n)

    async def _sweep_stale_worktrees(self) -> None:
        """Startup-only: reclaim worktree directories left by tasks that
        already reached a terminal status and will never run again.

        Mirrors `_reconcile_terminal_task_attempts` above — same
        "must never block boot" wrapping, same reason it runs once at startup
        and not on a per-tick cadence. See `core/worktree.sweep_stale_worktrees`
        for the reclaim rules (never age/mtime — only a provably dead owner or
        a provably vanished git admin dir)."""
        try:
            removed, skipped = await sweep_stale_worktrees(self.store, self._config)
        except Exception:  # noqa: BLE001 — the sweep must never block boot
            log.exception("startup: sweeping stale worktrees failed")
            return
        if removed or skipped:
            log.info(
                "startup: reclaimed %d stale worktree(s), skipped %d",
                removed, skipped)

    async def _salvage_dead_worktrees(self) -> None:
        """Startup-only: commit + checkpoint the uncommitted work of an
        IMPLEMENTING task whose worker was KILLED (the hard-kill twin of
        `Orchestrator._honor_server_stop`). Runs before the first tick,
        because the task's own next run reaps its worktree
        (`_reap_dead_worktrees`) with no salvage. Must never block boot."""
        try:
            salvaged, skipped = await salvage_dead_worktrees(
                self.store, self._config)
        except Exception:  # noqa: BLE001 — salvage must never block boot
            log.exception("startup: salvaging dead worktrees failed")
            return
        if salvaged or skipped:
            log.info(
                "startup: salvaged %d dead worktree(s), skipped %d",
                salvaged, skipped)

    async def _row_is_live(self, t) -> bool:
        """True when *t* shows evidence a worker still owns it — this
        process's own claim, or a row/event write within the grace window
        from ANY process.

        Used by BOTH `_recover_orphans` branches (startup and per-tick) —
        see that method's docstring for why "at startup" alone used to stand
        in for this check and was wrong (incident 6408aba0). Liveness is
        judged on the newest persisted activity — row `updated_at` OR the
        newest `task_event` — because `_inflight` is per-process: a run
        driven by ANOTHER process (a second `nh start`/`nh serve`, the `nh
        watch` TUI, a CLI-driven resume) is invisible to `_inflight` but its
        events land in the same DB (B1, review 2026-08-10; every in-process
        runner persists through EventPersister).
        """
        if t.id in self._inflight:
            return True
        if self._row_age_s(t.updated_at) < self._STRANDED_GRACE_S:
            return True      # row itself is young — no query needed
        ev_ts = await self.store.last_event_ts(t.id)
        return ev_ts is not None and time.time() - ev_ts < self._STRANDED_GRACE_S

    async def _activity_age_s(self, t) -> float:
        """Seconds since *t*'s newest persisted activity — row `updated_at`
        OR the newest `task_event`, whichever is more recent — the same two
        signals `_row_is_live` reads, as a number instead of a bool. Used
        only for the "how long until claimable" estimate below; never by
        `_row_is_live` or `_recover_orphans`, which stay unchanged.

        `last_event_ts` failing is treated as age `0.0` (the full grace still
        applies) — fail toward waiting, never toward "drained".
        """
        age = self._row_age_s(t.updated_at)
        try:
            ev_ts = await self.store.last_event_ts(t.id)
        except Exception:  # noqa: BLE001 — an estimate must not raise
            return 0.0
        if ev_ts is not None:
            age = min(age, max(0.0, time.time() - ev_ts))
        return age

    async def unclaimable_orphans(self) -> list[dict]:
        """Mid-run rows (`_ORPHANABLE`) neither claimable nor owned by this
        process — the gap `queue_is_drained` used to read as "nothing left".

        A row here is either a crash orphan younger than `_STRANDED_GRACE_S`
        (so `_recover_orphans` has deliberately not touched it yet — a live
        sibling's row must not be clobbered) or is owned by a THIRD process
        this scheduler has no visibility into. Under `--until-empty`,
        `_claim_pool_lease` has already refused to boot beside a live
        sibling, so either way the row is UNKNOWN, not drained. This method
        deliberately does NOT call `_row_is_live` — a young mid-run row is
        BY CONSTRUCTION indistinguishable from a live one (that is the whole
        defect this fixes), so classifying by liveness would just reintroduce
        the fail-open reading. Every non-excluded row here is counted as
        work, full stop.

        Excluded, because something else already accounts for them:
        - `plan_gate.correcting(t)` rows — a human's plan correction sits in
          PLANNING waiting to be re-planned; `_claimable` already claims it
          (mirrors `_recover_orphans`'s own first filter, so the two agree on
          what a mid-run row *is*).
        - `t.id in self._inflight` — this process owns it; `queue_is_drained`
          already counts `_inflight` separately.
        """
        out = []
        for status in self._ORPHANABLE:
            for t in await self.store.list_tasks(status):
                if plan_gate.correcting(t):
                    continue
                if t.id in self._inflight:
                    continue
                age = await self._activity_age_s(t)
                out.append({
                    "task_id": t.id,
                    "status": status.value,
                    "seconds_until_claimable": max(
                        0.0, self._STRANDED_GRACE_S - age),
                })
        return out

    async def _recover_orphans(self, *, startup: bool = True) -> None:
        """Crash/strand recovery. A task found in one of `_ORPHANABLE`'s
        mid-run statuses is recovered — flipped to IMPLEMENTING (claimable)
        so the pool re-runs it from its checkpoint — ONLY when `_row_is_live`
        says nothing is still working it.

        That liveness check used to run at RUNTIME only; AT STARTUP the sweep
        recovered every mid-run row unconditionally, on the theory (review
        2026-07-25) that "nothing is legitimately in-flight yet, so any
        mid-run status is an orphan of a killed process". True for the first
        process ever to touch a database; false the moment a second `nh
        start`/`nh serve` shares it — the second process's OWN `_inflight` is
        empty right after boot regardless of what a FIRST, still-running
        process is doing with that same row. That gap is how incident
        6408aba0 lost a review-PASSED attempt (task 2cc879d5,
        2026-0x-xx 17:29:07-17:35:58): a second process's startup sweep saw
        the first process's live REVIEWING row, had no liveness evidence to
        check, and requeued it to IMPLEMENTING out from under the worker
        still reviewing it — which can also duplicate-claim the same task.
        `Scheduler._claim_pool_lease` now refuses to let a second process
        reach this sweep at all while a sibling's heartbeat is live; the
        liveness gate below is defense in depth for what the lease alone
        does not cover (a lease taken over from a dead process must still not
        clobber a row a THIRD process — or this same process, mid-write to
        its own DB connection — is actively touching).

        Since R15 (2026-08-09 incident) the sweep also runs PER-TICK with
        ``startup=False``: a status write that is lost or reverted (a stale
        full-row write-back did exactly that) strands a row in a worker-only
        status while the process lives, and the startup-only sweep left it
        invisible until the next restart — 66 minutes, in that incident.
        ``startup`` now only selects the event text; both call sites share
        the same `_row_is_live` gate.
        """
        for status in self._ORPHANABLE:
            for t in await self.store.list_tasks(status):
                # Not an orphan: a task sitting in PLANNING with a human's
                # plan correction on it is WAITING to be re-planned, and
                # requeueing it as IMPLEMENTING would spend the run on the
                # very plan they rejected. `_claimable` picks it up instead.
                if plan_gate.correcting(t):
                    continue
                if await self._row_is_live(t):
                    continue
                if await self._reconcile_landed_orphan(t):
                    continue
                if startup:
                    text = (f"found in {status.value} at startup with no "
                            "worker attached (previous process died mid-run) "
                            "— requeued from its checkpoint")
                else:
                    text = (f"found in {status.value} with no worker attached "
                            "(status write lost, or its worker died) — "
                            "requeued from its checkpoint")
                # THE STATUS WRITE GOES FIRST, and nothing else happens until it
                # lands. `set_status` CAS-guards terminal rows (SCRUM-73) and
                # returns None when it refuses — a human can mark this task DONE
                # or cancel it between the `list_tasks` read above and here, and
                # `merge_context` has no rollback, so stamping first left a
                # FINISHED task holding an `orphan_recovery` checkpoint nothing
                # would ever clear. A lost race here used to be free because the
                # sweep wrote nothing; it is not free any more.
                if await self.store.set_status(
                        t, TaskStatus.IMPLEMENTING, validate=False) is None:
                    continue
                try:
                    sha = await self._inherited_checkpoint(t)
                except Exception:  # noqa: BLE001 — sweep must not kill the pool
                    # One task's checkpoint must never abort the sweep. This is
                    # the code path whose whole job is to rescue tasks after a
                    # crash, and an unguarded raise inside the loop took every
                    # REMAINING orphan down with it. A checkpoint is an
                    # optimisation; requeueing is the rescue — which has already
                    # happened above, so a failure here costs a cold start.
                    log.exception(
                        "orphan recovery: checkpoint lookup failed for %s — "
                        "requeued from base", t.id[:8])
                    sha = ""
                if sha:
                    text += f" {sha[:8]}"
                await self.store.save_events(t.id, [{
                    "source": "orchestrator", "kind": "orphan_recovered",
                    "text": text,
                    "ts": time.time(),
                }])
                self._on_event(
                    "orphan_recovered",
                    f"{t.id[:8]} was orphaned in {status.value} — requeued")

    async def _inherited_checkpoint(self, t) -> str:
        """The sha the requeued run must branch from, stamped if it is not
        already recorded. Returns it, or "" when there is nothing to resume.

        🔴 A REQUEUE IS NOT A RESTART, AND THE ROW ALONE DOES NOT SAY SO.
        `run_task` re-enters as a FRESH bounded loop at ``attempt_n == 1``,
        where `_resume_branch_point` ignores ``handoff.wip_sha`` (attempt 1 has
        no predecessor of its own) — so the only checkpoint that survives a
        requeue is ``resume_from``, which is not attempt-gated. The dead
        attempt's work is committed and its sha is on the attempt row, but
        nothing copied it there: measured 2026-08-10, three restarts closed 11
        attempts as 'superseded by a newer attempt' across 5 live tasks and
        every successor re-ran the coder from base. No loss was reported
        (`resume_checkpoint_lost` NULL on all of them) because no checkpoint
        was ever considered.

        Provenance is the machine's, never ``human``: the zero-diff honesty
        gate (`Orchestrator._is_own_partial`) credits work already ahead of
        base only when a human gated the branch point, and a requeue is a
        machine decision. A HUMAN's ``resume_from`` is INHERITED untouched —
        stamping over their gated sha relabels it as the machine's and fails
        their resume as fabrication, which is the bug that gate keeps
        re-learning; the human is identified by the SAME rule that gate uses
        (`human_gate_armed`), including its legacy fallback, so the two cannot
        drift apart. A sha the object store can no longer read needs no check
        here: the orchestrator already falls back to base and says so
        (`resume_checkpoint_lost`).

        An armed gate is untouched; a CONSUMED one is not — once an attempt has
        actually branched from a human's sha (`Orchestrator._consume_human_gate`
        rewrites ``by`` to ``consumed_human``), their choice has already been
        executed, and this sha is exactly the ordinary machine-checkpoint
        inheritance below was always meant to allow. Requeuing onto a stale
        `commit_sha` there would repeat the original bug in the other
        direction — a dead run's later work discarded because a long-since-
        executed human stamp still read as armed.

        🔴 TWO THINGS THIS MUST NOT DO, both found by review of the first cut.

        It must not inherit its OWN earlier stamp. The measured incident was
        THREE restarts. Bailing on any existing ``resume_from`` cannot tell a
        human's gate from the machine's last one, so restart 2 resumed onto
        restart 1's sha and discarded everything the run in between committed —
        the same loss, one restart later. Only an ARMED ``human`` gate is
        inherited untouched.

        And the candidate is the attempt that actually DIED —
        `Store.latest_open_attempt`, the most recently STARTED row still
        ``in_progress``, and only that row. "The newest attempt this task ever
        had" reaches back PAST a deliberate clear: `POST /tasks/{id}/retry`,
        `nh task retry` and both "send back" twins all drop the checkpoint
        because a fresh run must not branch from one an earlier actor chose,
        and the first sweep after any of them put it straight back, from a
        run that had already failed.
        Those paths now call `Store.close_open_attempts` FIRST, so after a
        clear there is no open row to reach back to; the reason that fix is
        there and not here is written out on that method — the context a clear
        leaves behind is indistinguishable from a task that never had a
        checkpoint, so no amount of reading it can tell them apart.

        Not a search for "the newest open row that HAS a sha", either. Falling
        past an open row with no commit is the same reach-back by another
        route: after a clear the requeued run's own row is open and empty, and
        skipping it lands on the pre-clear row. If the attempt that died
        committed nothing, there is nothing to resume, and a cold start is the
        correct answer.
        """
        ctx = t.context or {}
        resume = ctx.get("resume_from") or {}
        if human_gate_armed(ctx):
            return ""                    # a human gated it — execute, don't decide
        sha = (await self.store.latest_open_attempt(t.id) or {}).get("commit_sha") or ""
        if not sha:
            # Nothing newer to point at. Deliberately NOT a clear: an earlier
            # machine stamp is the only thing still protecting that work.
            return ""
        if sha == resume.get("sha"):
            return sha                   # already stamped — no write, no churn
        from ..blockers import resume_provenance
        t.context = await self.store.merge_context(
            t.id, {"resume_from": resume_provenance(
                {"sha": sha}, "orphan_recovery")})
        return sha

    @staticmethod
    def _row_age_s(stamp: str | None) -> float:
        """Seconds since an isoformat stamp; unparseable reads as 0 (young),
        so a corrupt timestamp can never cause a false requeue."""
        try:
            return max(0.0, time.time()
                       - datetime.fromisoformat(str(stamp)).timestamp())
        except (ValueError, TypeError):
            return 0.0

    async def _check_db_liveness(self) -> None:
        """Per-tick: is the Store's read view still the database's?

        THIS IS THE GUARD THAT SURVIVES AN UNKNOWN FIRST CAUSE. The rollback
        added in `db.py` closes one route to a pinned connection, but the
        2026-08-01 incident's timeline does not fit that route — the process was
        pinned three hours before its own first read, i.e. stale from birth,
        which points at a recovered stale WAL index rather than at anything this
        process did. Detection does not need to know: whatever pins the
        connection, the pinned connection disagrees with the file, and that
        disagreement is what is measured here.

        Never raises. A probe that can kill the pool is worse than no probe —
        and the probe touches a second connection, so it has its own failure
        modes (a full disk, a locked file) that must not become the pool's.
        """
        probe = getattr(self.store, "probe_snapshot_staleness", None)
        if probe is None:      # a Store double in a test; nothing to check
            return
        try:
            result = await probe()
        except Exception as exc:  # noqa: BLE001 — never kill the pool
            # FAILING OPEN SILENTLY IS THE INCIDENT'S OWN SHAPE. Returning here
            # without a counter left `db_view_stale: False`, `healthy: true`,
            # `idle_reason: queue_empty` — byte-identical to health — while the
            # only thing that knew better was a log line, back in the 46,000
            # lines this change exists to make unnecessary. A detector that
            # cannot report its own failure is not a detector.
            #
            # It still must not kill the pool, so the tick continues; what
            # changes is that the failure is now COUNTED and published, exactly
            # like `status_write_failures`.
            self._probe_failures += 1
            self._consecutive_probe_failures += 1
            self._last_probe_error = f"{type(exc).__name__}: {exc}"
            log.error(
                "db staleness probe FAILED (%d in a row): %s. While it is "
                "failing, nothing is checking whether the scheduler's view of "
                "the queue is real — treat the view as UNKNOWN, not healthy.",
                self._consecutive_probe_failures, exc, exc_info=True)
            self._on_event(
                "db_probe_failed",
                f"staleness probe failed ({self._consecutive_probe_failures} "
                f"in a row): {exc}")
            return
        self._consecutive_probe_failures = 0
        if not result.stale:
            if self._db_view_stale:
                log.warning("db read view recovered")
                self._on_event("db_view_recovered",
                               "database read view is live again")
            self._db_view_stale = False
            self._db_stale_since = None
            return

        if not self._db_view_stale:
            self._db_stale_since = time.time()
        self._db_view_stale = True
        # ERROR, not warning: on 2026-08-01 this state ran for six hours with
        # every surface reporting health. It is never routine.
        log.error(
            "DATABASE READ VIEW IS FROZEN: this connection sees %d task(s) up "
            "to %s, the file has %d up to %s. Every task written since is "
            "INVISIBLE to the scheduler and cannot be dispatched. Reconnecting.",
            result.shared.count, result.shared.max_updated_at,
            result.fresh.count, result.fresh.max_updated_at)
        self._on_event(
            "db_view_stale",
            f"frozen read snapshot: connection sees {result.shared.count} "
            f"task(s), file has {result.fresh.count} — reconnecting")

        reconnect = getattr(self.store, "reconnect", None)
        if reconnect is None:
            return
        try:
            await reconnect()
        except Exception as exc:  # noqa: BLE001
            log.error("reconnect after a frozen read view FAILED: %s", exc,
                      exc_info=True)
            self._on_event("db_reconnect_failed", str(exc))
            return
        # Verify the recovery rather than assume it. If the view is still
        # frozen after a fresh connection, the cause is not this connection and
        # the flag must stay up — silently clearing it would recreate exactly
        # the false all-clear this whole change exists to remove.
        try:
            after = await probe()
        except Exception as exc:  # noqa: BLE001
            log.warning("post-reconnect staleness probe failed: %s", exc)
            return
        if after.stale:
            log.error("STILL FROZEN after reconnecting — the stale view is not "
                      "this connection's doing; escalate")
            self._on_event("db_view_stale",
                           "still frozen after reconnect — needs a human")
            return
        self._db_view_stale = False
        self._db_stale_since = None
        log.warning("reconnected; read view is live again (now sees %d task(s))",
                    after.shared.count)
        self._on_event("db_view_recovered",
                       f"reconnected — now sees {after.shared.count} task(s)")

    def _crashes_since(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        return sum(1 for t in self._crash_times if t >= cutoff)

    def health_snapshot(self) -> dict:
        """What `/api/worker/status` needs to tell idle from wedged.

        `inflight: 0` is the same number in both states — it was honest
        throughout the 2026-08-01 incident, because each crash was instantaneous
        and a poll almost never landed inside a run. So the number is kept and
        the CONTEXT is added: `idle_reason` names why nothing is running, and
        the counters say whether the last tick achieved anything.
        """
        inflight = len(self._inflight)
        since_tick = (None if self._last_tick_at is None
                      else time.time() - self._last_tick_at)
        # A LOOP THAT HAS STOPPED TICKING INVALIDATES EVERY OTHER FIELD HERE,
        # which is why it is judged first. `db_view_stale`, `claimable` and the
        # crash counts are all written BY a tick, so a stalled loop freezes them
        # at their last values and they read healthy forever — the same failure this
        # change exists to remove, one level up. Publishing
        # `seconds_since_last_tick` and leaving it out of `healthy` was not
        # enough: a field nobody reads surfaces nothing.
        #
        # The threshold is derived from the interval the loop was actually
        # given, not a constant, so it stays correct under any `poll_interval`.
        # Six intervals (>=60s) is deliberately generous: a tick does real work
        # — the wake watcher, the probe, re-analysis — and a false "stalled"
        # would be its own bad alarm. A tick that genuinely takes over a minute
        # is also not dispatching, so reporting it is right either way.
        stall_after = max(60.0, self._poll_interval * 6)
        # A LOOP THAT HAS NEVER TICKED AT ALL is the same failure one step
        # earlier, and the first version of this reported it healthy FOREVER:
        # `since_tick` is None before the first tick, so `since_tick > ...` was
        # never evaluated and `tick_stalled` stayed False. The `starting` branch
        # below existed and simply was not carried into `healthy`.
        #
        # It is REACHABLE, and by this incident's own mechanism: `run_forever`
        # awaits `_recover_orphans()` before the loop with no try/except, and
        # that method issues unguarded `set_status`/`save_events` WRITES. A
        # connection wedged at startup — "stale from birth", the inferred first
        # cause of the incident this module now guards against — fails those
        # writes with `database is locked`, killing `run_forever` before tick 1.
        #
        # BOUNDED, not unconditional: `healthy: true` during the genuine first
        # poll_interval is correct, and a server that has been up for four
        # seconds must not report a fault. So the same threshold applies, timed
        # from when this Scheduler was constructed.
        never_ticked_too_long = (
            self._last_tick_at is None
            and time.time() - self._created_at > stall_after)
        tick_stalled = (
            (since_tick is not None and since_tick > stall_after)
            or never_ticked_too_long)

        # ORDERING. Faults first, then ordinary operating states. A fault
        # reported as `slots_full` or `queue_empty` is the defect this whole
        # change exists to remove, so nothing that describes NORMAL operation
        # may mask something that describes a BREAKAGE. Every adjacent pair
        # below is pinned by a test that constructs both conditions at once —
        # the argument used to live only in this comment, where it was free to
        # regress silently.
        if never_ticked_too_long:
            # Distinguished from `tick_loop_stalled`: "it never started" and "it
            # stopped" have different causes and different first questions.
            idle_reason = "never_ticked"
        elif tick_stalled:
            # Outranks everything: `db_view_stale`, `claimable` and the crash
            # counts are all written BY a tick, so a stalled loop freezes them
            # at their last values and they all read healthy. Their content is
            # not evidence any more, and saying so first is the honest report.
            idle_reason = "tick_loop_stalled"
        elif self._lease_lost:
            # A per-tick refresh failure — this scheduler is UNLEASED and
            # `tick()` has been returning `[]` ever since. Outranks
            # `db_view_stale`/`claimable`/every normal-operation state below
            # for the same reason `tick_loop_stalled` does: once the lease is
            # lost, nothing downstream of it is evidence of normal idleness.
            idle_reason = "lease_lost"
        elif self._db_view_stale:
            # A CONFIRMED observation, so it outranks the probe-failure case
            # below (an ABSENCE of information) and every normal state. The
            # queue looks empty precisely BECAUSE the view is frozen, so
            # reporting `queue_empty` here would be the original lie in a new
            # field.
            idle_reason = "db_view_stale"
        elif self._consecutive_probe_failures:
            # Not "the view is stale" — "nobody knows whether it is".
            idle_reason = "db_probe_failing"
        elif self._last_tick_at is None:
            # Genuinely still starting, inside the first threshold.
            idle_reason = "starting"
        elif inflight >= self.max_workers:
            idle_reason = "slots_full"
        elif self._quota_cooldown_until is not None and \
                datetime.now(timezone.utc) < self._quota_cooldown_until:
            idle_reason = "quota_cooldown"
        elif inflight > 0:
            idle_reason = None
        elif self._last_claimable_count:
            idle_reason = "claimable_not_dispatched"
        else:
            idle_reason = "queue_empty"

        store_liveness = {}
        getter = getattr(self.store, "liveness", None)
        if callable(getter):
            try:
                store_liveness = getter()
            except Exception:  # noqa: BLE001
                store_liveness = {}
        return {
            "idle_reason": idle_reason,
            "db_view_stale": self._db_view_stale,
            "db_stale_since": self._db_stale_since,
            "last_tick_at": self._last_tick_at,
            # HOW FRESH `db_view_stale` IS. The probe runs per TICK, not per
            # request: a status endpoint that opened a second SQLite connection
            # on every poll would put the board's polling on the database's
            # critical path. So this flag is at most one poll_interval old, and
            # a caller that needs to know that is told rather than left to
            # assume. It is also the alarm for the other silent failure — a
            # scheduler whose loop has stopped ticking at all, which no
            # per-tick check can ever report on its own.
            "seconds_since_last_tick": (
                None if since_tick is None else round(since_tick, 1)),
            "tick_stalled": tick_stalled,
            "lease_lost": self._lease_lost,
            "never_ticked": never_ticked_too_long,
            "seconds_since_start": round(time.time() - self._created_at, 1),
            "tick_stall_threshold_s": stall_after,
            "probe_failures": self._probe_failures,
            "consecutive_probe_failures": self._consecutive_probe_failures,
            "last_probe_error": self._last_probe_error,
            "last_dispatch_at": self._last_dispatch_at,
            "claimable": self._last_claimable_count,
            "crashes_last_5m": self._crashes_since(300),
            "crashes_last_1m": self._crashes_since(60),
            "status_write_failures": self._status_write_failures,
            "consecutive_status_write_failures":
                self._consecutive_status_write_failures,
            "last_status_write_error": self._last_status_write_error,
            "quota_cooldown_until": (self._quota_cooldown_until.isoformat()
                                     if self._quota_cooldown_until else None),
            "infra_cooldown": self._infra_cooldown_active,
            "db": store_liveness,
        }

    async def _note_slot_waits(self, claimable: list, started: list[str]) -> None:
        """Persist one `waiting_for_slot` event per continuous wait, for every
        claimable task this tick left behind because the pool is full.

        A resumed task (`wake._resume`) is written IMPLEMENTING before any
        worker is attached; when `max_workers` are all busy the scheduler
        deliberately leaves it unclaimed rather than mislabel it. That
        silence is by design — the task's RECORD staying silent about it is
        not. Best-effort: a bookkeeping failure here must never take down
        the pool.
        """
        try:
            waiting = [t for t in claimable
                       if t.id not in started and t.id not in self._inflight]
            waiting_ids = {t.id for t in waiting}
            if len(self._inflight) < self.max_workers:
                # The pool is not full, so nothing new is slot-blocked — but a
                # task still unclaimed (a `_shipped_before_dispatch` skip left
                # a free slot while others wait) keeps its open wait, or the
                # next full tick would emit a SECOND event for the same wait.
                self._waiting_for_slot &= waiting_ids
                return
            for t in waiting:
                if t.id in self._waiting_for_slot:
                    continue
                self._waiting_for_slot.add(t.id)
                text = slot_wait.waiting_text(
                    len(self._inflight), self.max_workers,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
                await self.store.save_events(t.id, [{
                    "source": "orchestrator", "kind": slot_wait.KIND,
                    "text": text, "ts": time.time(),
                }])
                self._on_event(slot_wait.KIND, f"{t.id[:8]} {text}")
            # An id that started, finished, or left the claimable set drops
            # out, so a LATER wait produces a second event (one per distinct
            # waiting period), while a continuous wait produces exactly one.
            self._waiting_for_slot &= waiting_ids
        except Exception as exc:  # noqa: BLE001 — must not kill the pool
            log.warning("slot-wait bookkeeping failed: %s", exc)

    async def tick(self, *, now: datetime | None = None) -> list[str]:
        """One scheduling pass: resume parked tasks, then dispatch up to the free
        slots. Returns the task ids started this tick."""
        if self._lease_lost:
            # A prior tick's refresh already lost the lease — no orphan
            # sweep, no wake tick, no dispatch this tick or ANY later one.
            # A scheduler that cannot prove it still holds the lease has no
            # authority to touch the queue at all; `run_forever` is the one
            # that actually stops the loop, but every tick between "lost"
            # and "stopped" must be a strict no-op.
            return []
        now = now or datetime.now(timezone.utc)
        self._last_tick_at = time.time()
        # BEFORE anything reads the queue. Every decision below this line is
        # made from `store`, so if the connection's view is frozen the whole
        # tick is reasoning about a database that no longer exists.
        await self._check_db_liveness()
        # Refresh, not re-claim: a sibling that outlives this loop's poll
        # interval must never see our heartbeat go stale and take the lease
        # while we are still running. `_claim_pool_lease`'s own-(pid,host)
        # branch is exactly this refresh — the startup call already got the
        # loud, propagating check; this one, on failure, marks the pool
        # UNLEASED rather than swallowing a warning and continuing to
        # dispatch: a holder that cannot refresh its lease has lost it, and
        # silently soldiering on is exactly the fail-open bug this fix
        # exists to close, one level up (per-tick instead of at-boot).
        try:
            await self._claim_pool_lease()
        except Exception as exc:  # noqa: BLE001 — reported below, not swallowed
            self._lease_lost = str(exc)
            log.error(
                "pool lease LOST (%s) — this scheduler is UNLEASED and "
                "will stop dispatching; a sibling may now claim the pool",
                exc)
            self._on_event("pool_lease_lost", str(exc))
            return []
        if self.wake is not None:
            try:
                # Pass the claimed set so the stuck-active sweep judges only
                # tasks a worker is actually running — a resumed task waiting
                # for a free slot is silent by design, not hung.
                await self.wake.tick(now=now, active_ids=set(self._inflight))
            except Exception as exc:  # noqa: BLE001 — watcher must not kill the pool
                log.warning("wake tick failed: %s", exc)

        # R15: a lost/reverted status write strands a task in a worker-only
        # status with no worker attached — sweep every tick, not only at
        # startup, or it stays invisible until the next restart.
        try:
            await self._recover_orphans(startup=False)
        except Exception as exc:  # noqa: BLE001 — sweep must not kill the pool
            log.warning("stranded-task sweep failed: %s", exc)

        # A cooldown THIS breaker armed has lapsed — start the next window's
        # streak clean, or a single infra blip years apart from another would
        # otherwise silently accumulate toward the threshold forever.
        if self._infra_cooldown_active and not self._in_quota_cooldown(now):
            infra_breaker().reset()
            self._infra_cooldown_active = False

        # 3 consecutive zero-token/auth SDK failures across distinct tasks
        # (INCIDENT 2026-08-13) means the account or transport itself is
        # down — pause the WHOLE pool via the same cooldown gate a per-task
        # quota park uses, instead of feeding the next queued task straight
        # into the same wall.
        infra_reason = infra_breaker().tripped()
        if infra_reason and not self._in_quota_cooldown(now):
            self._quota_cooldown_until = now + timedelta(
                seconds=QuotaExhausted.RETRY_AFTER_S)
            self._infra_cooldown_active = True
            self._on_event("quota_pause", f"fleet paused — {infra_reason}")

        # Re-derive an open quota wall the DB already names but this PROCESS's
        # memory does not — the same recovery `run_forever` does once at
        # startup, now also mid-run so a running fleet stops feeding the queue
        # into a wall recorded by a worker whose `_run` has set the row
        # PAUSED_QUOTA (`_park_quota`) but not yet unwound to arm the in-memory
        # clock (that arming is one tick late), or by another process.
        #
        # Gated on `self._quota_wall is None`: once THIS process has resolved
        # ANY wall (its own `_run` park or startup recovery both set it, even
        # after it lapses), the resume path owns lapse handling and re-deriving
        # the pool clock from a park's own raise-time-plus-an-hour
        # `wake_check_at` would re-arm a wall that has actually reset — the
        # 2026-08-20 starvation `_resume_quota_parks` exists to prevent. Before
        # the first wall there is no resume in flight to fight, and startup
        # recovery has already seeded `_quota_wall` from any park that predated
        # this process — so a park seen here with `_quota_wall` still None is a
        # FRESH wall whose future reset is real. `recover_quota_cooldown` never
        # shortens a live cooldown and arms nothing for a lapsed/other-profile/
        # infra park, so this is a cheap no-op on the healthy path.
        if self._quota_wall is None and not self._in_quota_cooldown(now):
            try:
                await self.recover_quota_cooldown()
            except Exception as exc:  # noqa: BLE001 — recovery must not kill the pool
                log.warning("idle quota-cooldown recovery failed: %s", exc)

        # Edge-detect "the cooldown just ended" (or "this is the very first
        # tick", since `_was_cooling`/`_resume_parks_pending` both start
        # False/True respectively) so the resume sweep below runs exactly
        # once per lapse, not on every idle tick.
        cooling = self._in_quota_cooldown(now)
        if self._was_cooling and not cooling:
            self._resume_parks_pending = True   # first tick after a cooldown ended
        self._was_cooling = cooling
        if cooling:
            # Nothing is dispatched during a pause, so no NEW wait is emitted
            # (already true) — but the in-process dedupe set must not carry a
            # pre-pause wait across the pause, or the first post-pause full
            # tick stays silent (`_note_slot_waits` sees the id already in
            # the set) and the task's newest event keeps a stale, pre-pause
            # timestamp for as long as the pause lasts. Clearing this set
            # does not touch persisted events — the record stays
            # append-only; a wait that survives the pause simply gets a
            # fresh event once dispatch resumes.
            self._waiting_for_slot.clear()
            return []  # 7.4: pool-wide pause until the subscription resets

        if self._resume_parks_pending:
            self._resume_parks_pending = False
            try:
                await self._resume_quota_parks(now=now)
            except Exception as exc:  # noqa: BLE001 — sweep must not kill the pool
                log.warning("quota-park resume sweep failed: %s", exc)

        # Checked every tick, right before dispatch (config.assert_subscription_
        # mode is the gate) — idles instead of crash-looping when no credential
        # is on file, and resumes on the next tick once one appears.
        if self._auth_check is not None:
            try:
                self._auth_check()
            except AuthError as exc:
                msg = str(exc).splitlines()[0]
                if self._auth_advisory != msg:  # emit once per distinct reason
                    self._auth_advisory = msg
                    log.warning("dispatch paused — %s", msg)
                    self._on_event("setup_required", msg)
                return []  # idle, not raise: this must never crash-loop
            else:
                if self._auth_advisory is not None:
                    self._auth_advisory = None
                    self._on_event(
                        "setup_complete",
                        "credential detected — dispatch resumed")

        slots = self.max_workers - len(self._inflight)
        started: list[str] = []
        claimable = await self._claimable()
        self._last_claimable_count = len(claimable)
        if slots > 0:
            for task in claimable[:slots]:
                if await self._shipped_before_dispatch(task):
                    continue                      # completed; no attempt starts
                self._inflight.add(task.id)      # reserve BEFORE scheduling
                self._run_tasks[task.id] = asyncio.ensure_future(self._run(task))
                started.append(task.id)
            if started:
                self._dispatched.update(started)
                self._last_dispatch_at = time.time()
                self._on_event("dispatch", f"started {len(started)} task(s); "
                               f"{len(self._inflight)}/{self.max_workers} busy")
        await self._note_slot_waits(claimable, started)
        if not started and slots <= 0:
            return []
        # PR-E: periodic re-analysis (best-effort, never blocks task dispatch).
        if self.reanalysis is not None:
            try:
                ra_result = await self.reanalysis.maybe_run()
                if ra_result and ra_result.get("proposed", 0) > 0:
                    self._on_event(
                        "reanalysis",
                        f"proposed {ra_result['proposed']} learning(s) from "
                        f"{ra_result['transcripts']} transcript(s)",
                    )
            except Exception as exc:  # noqa: BLE001 — never kill the pool
                log.warning("re-analysis failed: %s", exc)
        # M-A: periodic repo-wiki refresh (best-effort, never blocks dispatch).
        if self.wiki_refresh is not None:
            try:
                wiki_result = await self.wiki_refresh.maybe_run()
                if wiki_result:
                    self._on_event(
                        "wiki_refresh",
                        f"refreshed wiki for {len(wiki_result)} repo(s)",
                    )
            except Exception as exc:  # noqa: BLE001 — never kill the pool
                log.warning("wiki refresh failed: %s", exc)
        # Memory lifecycle C: periodic unconfirmed-proposal sweep
        # (best-effort, never blocks task dispatch — same shape as
        # reanalysis/wiki_refresh above).
        if self.retirement is not None:
            try:
                sweep_result = await self.retirement.maybe_run()
                if sweep_result and sweep_result.get("archived", 0) > 0:
                    self._on_event(
                        "memory_sweep",
                        f"archived {sweep_result['archived']} unconfirmed "
                        f"proposal(s)",
                    )
            except Exception as exc:  # noqa: BLE001 — never kill the pool
                log.warning("memory retirement sweep failed: %s", exc)
        # Learning harvest: supervisor corrections + failure signals
        # (escalations / reviewer FAIL findings / tamper trips), best-effort,
        # never blocks task dispatch — same shape as reanalysis/wiki_refresh/
        # retirement above. UNLIKE those three this fires its event on the
        # zero-result case too — see `HarvestJob`'s docstring for why.
        if self.harvest is not None:
            try:
                harvest_result = await self.harvest.maybe_run()
                if harvest_result is not None:
                    msg = (
                        f"{harvest_result['candidates']} bench candidate(s), "
                        f"{harvest_result['proposals']} learning proposal(s) "
                        f"({harvest_result['supervisor']} supervisor, "
                        f"{harvest_result['failures']} escalation/review-fail/tamper)"
                    )
                    log.info("harvest: %s", msg)
                    self._on_event("harvest", msg)
            except Exception as exc:  # noqa: BLE001 — never kill the pool
                log.warning("learning harvest failed: %s", exc)
        return started

    def task_events(self, task_id: str) -> list[dict]:
        """Return captured events for a task (most recent last)."""
        return list(self._event_log.get(task_id, []))

    async def _run(self, task) -> None:
        # Bind this worker's identity + the concurrency it was dispatched into,
        # BEFORE anything can fail. A nested Agent SDK session that dies in the
        # transport reads this to say which worker died and how many peers were
        # live (see agent/worker_context.py); with nothing bound it can only
        # say "unknown", which is how the 2026-07-11 "Stream closed" incident
        # ended up un-attributable.
        #
        # Set INSIDE the coroutine, not at the `ensure_future` call site:
        # `ensure_future` copies the current context into the new task, so a
        # set() in here is private to this task and cannot be clobbered by the
        # next worker the same tick dispatches.
        set_worker_context(WorkerContext(
            worker=task.id[:8],
            # `_inflight` already contains this task — `tick()` reserves the id
            # synchronously before scheduling — so a lone worker reads 1 of 1.
            inflight=len(self._inflight),
            max_workers=self.max_workers,
        ))
        # Set up per-task event capture.
        buf = deque(maxlen=self._MAX_EVENTS)
        self._event_log[task.id] = buf

        notify = self._event_notify.setdefault(task.id, asyncio.Event())

        # Events reach SQLite while the run is live, so a crash can't take the
        # whole history with it (and `buf` above is capped, so a chatty run used
        # to drop its earliest events even on a clean finish).
        persister = EventPersister(self.store, task.id,
                                   interval=self._EVENT_FLUSH_INTERVAL)

        def _sink(event):
            event["ts"] = time.time()
            # Don't clobber a subagent event's own task_id (the SDK's per-
            # dispatch Task-tool id, set in claude_backend.py's meta) — it's
            # a completely different concept from "which no_human task is
            # this," and overwriting it collapsed every distinct subagent
            # dispatch in a run down to one node in the System view.
            event.setdefault("task_id", task.id)
            buf.append(event)
            persister.record(event)
            notify.set()
            summary = _summarize_event(event)
            if summary:
                self._live_status[task.id] = summary

        persister.start()
        try:
            orch = self.factory(task)
            orch._sink = _sink
            self._running[task.id] = orch
            outcome = await orch.run_task(task)
            # 7.4: a billing-wall park pauses the whole pool until the reset.
            # An INFRA park (dead SDK session, `blocker.infra`) is the
            # task's to sleep off, not the pool's: the 3-strike breaker in
            # `_tick` is the fleet response to dead sessions. Arming the
            # clock here on one of them idled a free worker for an hour
            # under a 12-deep queue (2026-08-22, task c8d1a30d).
            parked_blocker = (outcome.task.blocker
                              if outcome is not None
                              and isinstance(outcome.task.blocker, dict) else {})
            if (outcome is not None and outcome.status == TaskStatus.PAUSED_QUOTA
                    and not parked_blocker.get("infra")):
                resets = _parse_iso(getattr(outcome.task, "wake_check_at", None))
                if resets is not None:
                    now = datetime.now(timezone.utc)
                    prof = active_auth_profile()
                    # Never let THIS park SHORTEN a wall this process is already
                    # holding for the same profile. Two workers dispatched
                    # before the clock armed race the same wall, and the one
                    # that parks second can carry an EARLIER reset — a stale
                    # banner, or the fallback hour under-estimating a longer
                    # real reset. Overwriting the live clock with it resumes the
                    # pool early, straight back into the same wall it just
                    # paused on. Keep the later of the two; a genuinely later
                    # reset (the fresher, more accurate one) still extends it.
                    # An UNRELATED park (a different `auth_profile`, or an
                    # unattributed one) is a wall of its own and adopts its own
                    # reset — the guard restricts the floor to same/unstamped.
                    if (self._quota_wall is not None
                            and self._quota_wall_profile in (None, prof)
                            and self._quota_wall > now
                            and self._quota_wall > resets):
                        resets = self._quota_wall
                    self._quota_cooldown_until = resets
                    self._infra_cooldown_active = False
                    self._quota_wall = resets
                    self._quota_wall_profile = prof
                    self._on_event("quota_pause",
                                   f"pool paused until {resets.isoformat()}")
        except Exception as exc:  # noqa: BLE001 — one task must not kill the pool
            # logging only — a bare print/traceback to stderr raises
            # BrokenPipeError inside THIS except when the desktop parent that
            # piped our stderr has crashed away (SCRUM-11), killing the pool
            # worker the except exists to protect. logging.handleError swallows.
            log.warning("task %s crashed in pool: %s", task.id[:8], exc,
                        exc_info=True)
            self._on_event("task_error", f"{task.id[:8]}: {exc}")
            self._crash_times.append(time.time())
            # Durable reason. `_on_event` above is the LIVE pool stream — it is
            # gone the moment nobody is watching, and `log.warning` lands in a
            # file the board never reads. Without this the task is FAILED with
            # no recorded cause anywhere a human looks: the drawer's "Why it
            # failed" reads the last attempt's failure_reason, and a crash here
            # can happen with no attempt row to carry one. Same durable channel
            # this file already uses for `orphan_recovered` above.
            #
            # Its own try: a write that fails must not cost us the set_status
            # below, and neither may kill the pool worker this except exists to
            # protect (see the stderr note above — that hazard is real, but it
            # is about writing to a broken pipe, not about the store).
            try:
                await self.store.save_events(task.id, [{
                    "source": "scheduler", "kind": "task_crashed",
                    "text": f"{type(exc).__name__}: {exc}",
                    "ts": time.time(),
                }])
            except Exception:  # noqa: BLE001
                pass
            # Mark the task as FAILED so it doesn't stay stuck.
            try:
                from .task import TaskStatus as _TS
                await self.store.set_status(task, _TS.FAILED, validate=False)
                self._consecutive_status_write_failures = 0
            except Exception as werr:  # noqa: BLE001
                # This used to be `except Exception: pass`, with no counter.
                # That is the line that made the 2026-08-01 wedge silent: when
                # the DB is the broken thing, THIS write fails too, the task
                # keeps a claimable status, and the next tick re-dispatches it —
                # ~12x/minute for three hours, with nothing logged above debug.
                #
                # It stays non-fatal (one task must not kill the pool), but a
                # crash handler whose own fallback cannot write is a DB-LEVEL
                # alarm, not a per-task detail, and is reported as one. No
                # retry cap is added here on purpose: during the incident the
                # scheduler was behaving correctly on the data it could see, so
                # capping retries would have hidden the fault instead of fixing
                # it. Visibility is the fix; `_check_db_liveness` is the cure.
                self._status_write_failures += 1
                self._consecutive_status_write_failures += 1
                self._last_status_write_error = f"{type(werr).__name__}: {werr}"
                log.error(
                    "could not mark task %s FAILED after its crash: %s. The "
                    "task keeps a claimable status and WILL be re-dispatched. "
                    "%d consecutive status-write failure(s) — if this is "
                    "climbing, the database connection is the fault, not the "
                    "task.", task.id[:8], werr,
                    self._consecutive_status_write_failures, exc_info=True)
                self._on_event(
                    "status_write_failed",
                    f"{task.id[:8]}: could not record FAILED ({werr}); "
                    f"{self._consecutive_status_write_failures} in a row")
        finally:
            self._running.pop(task.id, None)
            self._inflight.discard(task.id)
            self._live_status.pop(task.id, None)
            # Final notify so SSE clients see the task finished, then clean up.
            if task.id in self._event_notify:
                self._event_notify[task.id].set()
                # Don't delete immediately — give SSE 5s to drain.
                # The SSE endpoint checks inflight and closes after idle ticks.

            # Stop the periodic flusher, then write whatever it hasn't taken.
            # The run is untracked whether or not that flush raises.
            try:
                await persister.aclose()
            finally:
                self._run_tasks.pop(task.id, None)

    async def run_forever(
        self, *, stop: asyncio.Event, poll_interval: float = 10.0,
        until_empty: bool = False,
    ) -> None:
        """Loop until ``stop`` is set, then drain in-flight tasks.

        ``until_empty`` (``nh serve --until-empty``) sets that SAME ``stop``
        event as soon as a tick leaves the queue drained. It is deliberately
        one more way to raise the flag ctrl-c/SIGTERM already raise, not a
        second shutdown path: everything after the loop is unchanged. Parked
        work (blocked / awaiting-input / escalated / paused-quota) is not
        claimable, so it ENDS the drain rather than holding the process open
        for a human who is not there.
        """
        # Recorded so `health_snapshot` can judge "this loop has stopped
        # ticking" against the interval it is SUPPOSED to tick at, rather than
        # against a constant that would be wrong for any other configuration.
        self._poll_interval = float(poll_interval)
        # FIRST, unguarded: a live sibling's heartbeat must stop boot before
        # anything below touches the DB — that's what makes it safe for
        # every sweep after this line to assume it is the only writer this
        # process needs to worry about not racing. Letting this propagate
        # (not caught here) is deliberate: the caller (`nh serve`/`nh start`)
        # is what prints the operator-visible refusal.
        await self._claim_pool_lease()
        # BEFORE the orphan sweep: that sweep reads `latest_open_attempt` to
        # recover a checkpoint, and a row left open on a task that already
        # FINISHED is not a checkpoint to resume from — it is debris.
        await self._reconcile_terminal_task_attempts()
        # Also startup-only, and also before the orphan sweep: a TERMINAL
        # failed/cancelled row whose recorded work already landed on its
        # base branch has the same "no path back to DONE" problem as an
        # orphan whose requeue would duplicate shipped work — see
        # `_reconcile_landed_terminal`. Bounded git cost per candidate row is
        # why this runs once per boot, not on the tick's hot path.
        await self._reconcile_landed_terminal()
        await self._sweep_stale_worktrees()
        # AFTER the sweep (which only reclaims TERMINAL leftovers and never
        # touches this) and BEFORE orphan recovery: an IMPLEMENTING task's
        # dirty worktree from a hard-killed worker must be salvaged before
        # its own next run reaps it with no commit.
        await self._salvage_dead_worktrees()
        await self._recover_orphans()
        # AFTER the orphan sweep (which only moves rows between claimable
        # states) and BEFORE the first tick: a wall the previous process was
        # honouring must gate this one's first dispatch, not its second.
        try:
            await self.recover_quota_cooldown()
        except Exception as exc:  # noqa: BLE001 — recovery must not kill the pool
            log.warning("quota cooldown recovery failed: %s", exc)
        while not stop.is_set():
            await self.tick()
            if self._lease_lost:
                # `tick()` already logged/emitted the loss and has been
                # returning `[]` since — stop the loop rather than spin
                # unleased forever. `drain()` below still requeues whatever
                # was in-flight before the lease was lost.
                stop.set()
                break
            # After the tick, so a task dispatched this pass is already in
            # `_inflight` and an empty queue can never be read mid-dispatch.
            if until_empty and not self._inflight and not await self._claimable():
                stranded = await self.unclaimable_orphans()
                if stranded:
                    # Nothing claimable and nothing in-flight, but a mid-run
                    # row exists that this process does not own — UNKNOWN,
                    # not drained (see `unclaimable_orphans`'s docstring).
                    # Exit non-zero instead of looping: the intake answer for
                    # this fix is "refuse and say why", not "wait it out" —
                    # a retry only narrows the window in which UNKNOWN reads
                    # as OK, it does not change the reading.
                    self.drain_blocked_by = stranded
                    for row in stranded:
                        log.warning(
                            "not drained: task %s is %s with no worker "
                            "attached in this process — it is either a "
                            "crash orphan or owned elsewhere, and becomes "
                            "claimable in %.0fs (_STRANDED_GRACE_S=%.0f). "
                            "Refusing to report the queue drained.",
                            row["task_id"][:8], row["status"],
                            row["seconds_until_claimable"],
                            self._STRANDED_GRACE_S)
                    self._on_event(
                        "drain_blocked",
                        "; ".join(
                            f"task {row['task_id'][:8]} is {row['status']}, "
                            f"claimable in {row['seconds_until_claimable']:.0f}s"
                            for row in stranded))
                stop.set()
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
        # A stop is a REQUEUE of whatever is running, not a wait for it to
        # finish: `nh stop` SIGKILLs after its timeout, and an attempt
        # mid-coder takes minutes. Ask first, then wait — bounded.
        self.request_stop_checkpoints()
        await self.drain()
        # Best-effort: an orderly shutdown clears the lease immediately so a
        # restart doesn't wait out `_HEARTBEAT_STALE_S`. Ownership-guarded in
        # `Store.clear_scheduler_heartbeat` (only deletes a row this pid
        # wrote) so a crash between claim and this line just leaves the
        # heartbeat to go stale on its own — never clears a row that isn't
        # ours.
        try:
            await self.store.clear_scheduler_heartbeat(os.getpid())
        except Exception:  # noqa: BLE001 — shutdown must not fail on this
            log.exception("pool lease: clearing the heartbeat failed")

    async def queue_is_drained(self) -> bool:
        """Nothing running, nothing left to claim, and nothing UNKNOWN — the
        drain's exit condition.

        Also the CLI's after-the-fact check: a run that stopped on a SIGNAL
        with work still queued did NOT drain, and must not report that it did.

        A mid-run row nothing here owns (`unclaimable_orphans`) is UNKNOWN —
        it may be a live sibling's row or a crash orphan still inside its
        grace — and UNKNOWN is counted as work: this predicate never reports
        drained on evidence it does not have.
        """
        return (not self._inflight
                and not await self._claimable()
                and not await self.unclaimable_orphans())

    async def failed_dispatched(self) -> list[str]:
        """Ids this process dispatched whose task ended ``TaskStatus.FAILED``.

        `task.status` is the field, and FAILED is the only value on it that
        means failure: `core/task.py`'s TERMINAL_STATES is {DONE, FAILED}, and
        every other resting place — awaiting_approval, blocked, awaiting_input,
        escalated, paused_quota — is an honest park the loop is designed to
        produce. A pool-level crash lands here too: `_run`'s handler writes
        FAILED (and, if even that write fails, it says so loudly rather than
        silently downgrading the outcome).
        """
        out: list[str] = []
        for tid in sorted(self._dispatched):
            task = await self.store.get_task(tid)
            if task is not None and task.status == TaskStatus.FAILED:
                out.append(tid)
        return out

    def request_stop_checkpoints(self) -> int:
        """Ask every running orchestrator to checkpoint and requeue its
        attempt (`Orchestrator.request_server_stop`). Returns how many were
        asked.

        Never raises: a factory that returned an object without the hook (a
        test double, a foreign runner) is skipped, and `drain()` still bounds
        the wait. Nothing is written to the DB — the signal lives in process
        memory on purpose, so a SIGKILL during the grace cannot leave a stop
        behind to re-fire on the next server's first cheap boundary.
        """
        asked = 0
        for task_id, orch in list(self._running.items()):
            hook = getattr(orch, "request_server_stop", None)
            if hook is None:
                continue
            try:
                hook()
                asked += 1
            except Exception as exc:  # noqa: BLE001 — one task must not block the stop
                log.warning("stop checkpoint request failed for %s: %s",
                            task_id[:8], exc)
        if asked:
            self._on_event("server_stop",
                           f"asked {asked} running task(s) to checkpoint and requeue")
        return asked

    def request_task_cancel(self, task_id: str, reason: str) -> bool:
        """Hard-stop `task_id`'s live coder session right now, if this
        scheduler is the one running it (`Orchestrator.request_task_cancel`).

        The cancel-side twin of `request_stop_checkpoints`, but targeted at
        one task and terminal rather than a requeue. Returns False — never
        raises — when this scheduler has no in-flight session for `task_id`
        (wrong process, or between attempts): the caller then knows to fall
        back to a process-tree kill or report `cancel_session_not_found`.
        """
        orch = self._running.get(task_id)
        if orch is None:
            return False
        hook = getattr(orch, "request_task_cancel", None)
        if hook is None:
            return False
        try:
            return bool(hook(task_id, reason))
        except Exception as exc:  # noqa: BLE001 — a bad hook must not crash cancel
            log.warning("task cancel request failed for %s: %s", task_id[:8], exc)
            return False

    async def drain(self, *, grace_s: float | None = None) -> bool:
        """Wait for in-flight tasks to finish — bounded by ``grace_s``
        (default `concurrency.stop_grace_s`, 60 s). Returns True when every
        in-flight task finished, False when the grace ran out first.

        Used to be unbounded, and `nh stop`'s docstring promised "a task is
        an Agent SDK session finishing a turn — seconds at best" — false: it
        waited for whole ATTEMPTS, so the 30 s SIGKILL landed on the coder
        mid-work every time. `request_stop_checkpoints` is what makes the
        seconds true; the bound is what keeps a wedged tool call from
        holding the process open forever.
        """
        limit = self._stop_grace_s if grace_s is None else float(grace_s)
        deadline = time.monotonic() + limit
        while self._inflight:
            if time.monotonic() >= deadline:
                log.warning(
                    "drain: %d task(s) still running after %.0fs grace — "
                    "exiting anyway; their attempts resume from their last "
                    "commit on the next start. The store closes behind them, "
                    "so their teardown may log a closed-database error: that "
                    "is this path, not a new defect", len(self._inflight), limit)
                return False
            await asyncio.sleep(0.05)
        return True


# --------------------------------------------------------------------------- #
# PR-E: Periodic re-analysis scheduler (EVOLUTION_PLAN Phase 9)                #
# --------------------------------------------------------------------------- #


class ReanalysisJob:
    """Periodically re-runs transcript extraction/ingestion on new sessions
    and proposes only patterns not already covered.

    All proposals land in ``learning/queue.py`` with ``confirmed=0`` — nothing
    activates without ``nh learnings --confirm``.

    Dedup is handled by the ingester's ``dedupe_key`` (content-based hash) and
    the transcript cache (Phase 7e): unchanged transcripts are skipped, and
    identical findings are never re-proposed.
    """

    def __init__(
        self,
        store: Store,
        *,
        interval_seconds: float = 86400,  # default: once per day
        days: int = 30,                    # look-back window for transcripts
        max_proposals_per_run: int = 20,   # cap to prevent queue flooding
        use_llm: bool = False,             # heuristic-only by default
        llm_call=None,                     # async (prompt) -> str, when use_llm=True
    ):
        self.store = store
        self.interval = max(60, interval_seconds)
        self.days = days
        self.max_proposals = max_proposals_per_run
        self.use_llm = use_llm
        self._llm_call = llm_call
        self._last_run: float = 0.0
        self._running = False

    def due(self, now: float | None = None) -> bool:
        """True when enough time has elapsed since the last run."""
        return (now or time.time()) - self._last_run >= self.interval

    async def maybe_run(self) -> dict | None:
        """Run re-analysis if due and not already running. Returns result dict
        or None if skipped. Thread-safe via ``_running`` flag."""
        if not self.due() or self._running:
            return None
        self._running = True
        try:
            return await self._run()
        finally:
            self._running = False
            self._last_run = time.time()

    async def _run(self) -> dict:
        from ..history.ingester import TranscriptIngester

        ingester = TranscriptIngester(self.store, llm_call=self._llm_call)
        result = await ingester.ingest(days=self.days, use_llm=self.use_llm)

        # Cap: if more proposals than max, keep only the first batch.
        # The surplus are already in the DB (ingester committed them), so
        # we log a warning but don't delete — the human can triage them.
        if result.proposed > self.max_proposals:
            log.warning(
                "re-analysis proposed %d items (cap %d) — review queue may be large",
                result.proposed, self.max_proposals,
            )

        log.info(
            "re-analysis complete: %d transcripts, %d findings, "
            "%d proposed, %d duplicates",
            result.transcripts, result.findings,
            result.proposed, result.duplicates,
        )
        return {
            "transcripts": result.transcripts,
            "findings": result.findings,
            "proposed": result.proposed,
            "duplicates": result.duplicates,
        }


# --------------------------------------------------------------------------- #
# Memory lifecycle C: retirement sweep                                        #
# --------------------------------------------------------------------------- #


class RetirementSweepJob:
    """The daily 45-day auto-archive sweep for unconfirmed proposals (AC1).

    Same ``due()``/``maybe_run()`` shape as ``ReanalysisJob``. ``enabled=False``
    is honoured by the CALLER passing ``None`` instead of constructing this
    (see ``api/app.py``'s lifespan) — same pattern as ``reanalysis``/
    ``wiki_refresh`` being ``None``-able on ``Scheduler``.

    Never touches a confirmed row that was NOT auto-activated
    (`Store.archive_unconfirmed_older_than`'s ``confirmed = 0`` clause is a
    literal equality) and never raises out of `maybe_run` — a failing sweep
    must not take the dispatch loop down with it.

    D3 (2026-08-31 operator directive): also runs the 90-day AUTOMATIC
    retirement sweep for AUTO-ACTIVATED rows (`learning/
    retire.py:sweep_auto_activated`) — the one exception to "a confirmed row
    always needs a human's explicit retire", scoped so it can only ever
    select a row `LearningQueue.auto_activate` itself wrote
    (`confirmed_by = 'auto'`); an operator-pinned or manually-added row is
    excluded by construction, not by a second exemption list. Gated on
    ``auto_manage`` — the same kill switch `HarvestJob` reads — so with
    ``learning.auto_manage: false`` this job's behaviour is exactly what it
    was before D3: the 45-day unconfirmed sweep, and nothing else.
    """

    def __init__(
        self,
        store: Store,
        *,
        interval_seconds: float = 86400,  # default: once per day
        archive_after_days: int = 45,
        max_per_run: int = 500,
        auto_manage: bool = True,
        auto_retire_days: int = 90,
    ):
        self.store = store
        self.interval = max(60, interval_seconds)
        self.archive_after_days = archive_after_days
        self.max_per_run = max_per_run
        self.auto_manage = auto_manage
        self.auto_retire_days = auto_retire_days
        # 0.0 means "never run" — the first `due()` check after boot is
        # therefore always True, so the first tick after startup IS the
        # startup sweep. There is no separate startup-only code path to test.
        self._last_run: float = 0.0
        self._running = False

    def due(self, now: float | None = None) -> bool:
        return (now or time.time()) - self._last_run >= self.interval

    async def maybe_run(self) -> dict | None:
        """Run the sweep if due and not already running. Returns a result
        dict or None if skipped.

        ``_last_run`` is stamped in ``finally`` — a raising `_run` must still
        advance it, or a persistently failing sweep would retry every single
        tick instead of backing off to the next interval.
        """
        if not self.due() or self._running:
            return None
        self._running = True
        try:
            return await self._run()
        finally:
            self._running = False
            self._last_run = time.time()

    async def _run(self) -> dict:
        from ..learning.retire import sweep_auto_activated, sweep_unconfirmed

        report = await sweep_unconfirmed(
            self.store, days=self.archive_after_days,
            limit=self.max_per_run)
        log.info("memory retirement sweep: archived %d unconfirmed "
                 "proposal(s) older than %d day(s)",
                 len(report.archived_ids), self.archive_after_days)
        result = {"archived": len(report.archived_ids)}
        # D3: kill-switched with `HarvestJob`'s own `auto_manage` — with it
        # off, no row can ever carry `confirmed_by='auto'` in the first
        # place, so this branch would always be a no-op query; skipping it
        # keeps the result dict's shape identical to the pre-D3 one too.
        if self.auto_manage:
            auto_report = await sweep_auto_activated(
                self.store, days=self.auto_retire_days,
                limit=self.max_per_run)
            log.info("auto-activation retirement sweep: archived %d "
                     "auto-activated learning(s) unused for %d day(s)",
                     len(auto_report.archived_ids), self.auto_retire_days)
            result["auto_retired"] = len(auto_report.archived_ids)
        return result


# --------------------------------------------------------------------------- #
# Learning harvest scheduler                                                   #
# --------------------------------------------------------------------------- #


class HarvestJob:
    """Runs BOTH of the product's existing harvest loops on a cadence, inside
    the same ``nh serve`` loop that already drives reanalysis / wiki refresh /
    the retirement sweep. Same ``due()``/``maybe_run()`` shape as
    ``RetirementSweepJob``:

    * ``eval.harvest.harvest`` — the bench-candidate harvest. Writes one
      ``runnable: false`` YAML per harvest-worthy terminal task to
      ``~/.no_human/harvest`` (or ``out_dir``), idempotent by filename. This
      job only *calls* it; the candidate shape and location are frozen in
      ``eval/harvest.py`` and are not touched here.
    * ``LearningQueue.harvest_supervisor_corrections`` (B2, supervisor
      ``correct`` decisions) and ``LearningQueue.harvest_failure_signals``
      (escalations, reviewer FAIL findings and tamper trips) — the
      learning-proposal harvest.

    ``distill=None`` by default: the scheduled pass never calls a model. It
    proposes the verbatim-clustered lesson, exactly like an un-configured
    ``nh learnings --harvest``. Nothing here spends a token or opens a PR —
    every bench candidate lands with ``runnable: false``, inert until a
    human edits it (see ``eval/harvest.py``'s module docstring for the
    bench side, unchanged by D3).

    LEARNING PROPOSALS ARE DIFFERENT (2026-08-31 operator directive,
    overriding what this docstring said here before — see
    ``learning/curator.py``'s module docstring for the same reversal stated
    there). With ``config learning.auto_manage`` at its default (``True``),
    every proposal this tick writes that passes
    ``LearningQueue.auto_activate``'s dedupe/PII/provenance/term screens is
    promoted straight into the active set (``confirmed=1``,
    ``source="auto"``) — capped at ``learning.auto_activate_daily_cap``
    proposals per rolling 24h window, and every activation (and every
    screen-failing archive) is written to ``learning_events`` for audit.
    A proposal that FAILS a screen is archived immediately, not left
    pending — there is no human queue left in the UI to hold it.
    ``learning.auto_manage: false`` is the kill switch FOR THIS WRITE PATH
    SPECIFICALLY: it restores the pre-D3 harvest/confirm-queue behaviour
    exactly — every row lands ``source="proposed"``, ``confirmed=False``,
    inert until a human runs ``nh learnings --confirm <id>``, and this job
    never calls ``auto_activate`` at all. It is NOT a global "everything is
    as it was" switch: the 2026-09-01 word-boundary trigger-matching fix
    (``learning/triggers.py``) and ``reject()`` aliasing ``pause()`` for an
    already-confirmed row (``learning/queue.py``) are both correctness
    fixes independent of D3's auto-activation default, and neither is
    gated by ``auto_manage`` — flipping it off does not revert either.

    UNLIKE its neighbors (``ReanalysisJob``/``WikiRefreshJob``/
    ``RetirementSweepJob``), this job's caller reports the ZERO case too
    (see the ``harvest`` block in ``Scheduler.tick``). Those three suppress a
    zero-result event because "nothing changed" is uninteresting there; here
    it matters that the pass RAN — for a human to trust an unattended
    12-hour cadence at all, "0 candidates, 0 proposals" and "the job
    silently never fires" must be distinguishable in the log.
    """

    def __init__(
        self,
        store: Store,
        *,
        interval_seconds: float = 43200,  # default: once per 12 hours
        distill: "DistillFn | None" = None,
        out_dir: "Path | None" = None,
        auto_manage: bool = True,
        auto_activate_daily_cap: int = 10,
    ):
        self.store = store
        self.interval = max(60, interval_seconds)
        self._distill = distill
        self.out_dir = out_dir
        # D3 (2026-08-31 operator directive): `config learning.auto_manage`
        # and `learning.auto_activate_daily_cap`, read via constructor
        # kwargs rather than this job reaching into `config` itself — the
        # same shape `RetirementSweepJob` already uses for its own
        # `learning.*` values. Threaded through by `nh serve`'s
        # construction (`cli/commands.py`) — the live scheduling path.
        # NOTE: `api/app.py`'s lifespan (the API server's OWN embedded
        # worker) constructs `RetirementSweepJob` but never `HarvestJob` at
        # all — a pre-existing gap this change does not close (out of
        # D3.1's file list); see the task report for detail.
        self.auto_manage = auto_manage
        self.auto_activate_daily_cap = max(0, int(auto_activate_daily_cap))
        # 0.0 means "never run" — same first-tick-after-boot convention as
        # `RetirementSweepJob`.
        self._last_run: float = 0.0
        self._running = False

    def due(self, now: float | None = None) -> bool:
        return (now or time.time()) - self._last_run >= self.interval

    async def maybe_run(self) -> dict | None:
        """Run both harvest loops if due and not already running. Returns a
        result dict (never None once due — see `_run`'s docstring for why
        the zero case is still a dict, not a skip) or None if not due yet.

        ``_last_run`` is stamped in ``finally``, matching every other job
        here — a raising pass must still advance it, or a persistently
        failing harvest would retry every tick instead of backing off.
        """
        if not self.due() or self._running:
            return None
        self._running = True
        try:
            return await self._run()
        finally:
            self._running = False
            self._last_run = time.time()

    async def _run(self) -> dict:
        from ..eval.harvest import harvest
        from ..learning import LearningQueue

        notes: list[str] = []
        candidates = await harvest(self.store, out_dir=self.out_dir)
        q = LearningQueue(self.store)
        sup = await q.harvest_supervisor_corrections(
            distill=self._distill, note=notes.append)
        fail = await q.harvest_failure_signals(
            distill=self._distill, note=notes.append)
        result = {
            "candidates": len(candidates),
            "proposals": len(sup) + len(fail),
            "supervisor": len(sup),
            "failures": len(fail),
            "notes": notes[:20],
        }
        # D3 (2026-08-31 operator directive): auto-activate whatever this
        # tick's (and any earlier tick's) pending queue holds, subject to the
        # daily cap. `auto_manage=False` is the kill switch — this branch is
        # skipped entirely, so nothing here ever calls `auto_activate`, and
        # the result dict carries no D3 keys, matching the pre-D3 shape
        # byte-for-byte.
        if self.auto_manage:
            activation = await q.auto_activate(cap=self.auto_activate_daily_cap)
            result["activated"] = len(activation.activated)
            result["auto_archived"] = len(activation.archived)
            result["cap_hit"] = activation.cap_hit
            log.info(
                "auto-activation: %d activated, %d screen-failing archived, "
                "cap_hit=%s",
                len(activation.activated), len(activation.archived),
                activation.cap_hit,
            )
        log.info(
            "harvest: %d bench candidate(s), %d learning proposal(s) "
            "(%d supervisor, %d escalation/review-fail/tamper)",
            result["candidates"], result["proposals"],
            result["supervisor"], result["failures"],
        )
        return result


# --------------------------------------------------------------------------- #
# Wiki refresh scheduler                                                       #
# --------------------------------------------------------------------------- #


class WikiRefreshJob:
    """Regenerates repo wiki docs when ``git rev-parse HEAD`` differs from the
    stored ``wiki_commit`` in the project profile.

    Same ``due()``/``maybe_run()`` shape as ``ReanalysisJob``; wired into the
    scheduler loop the same way.
    """

    def __init__(
        self,
        store: Store,
        backend: Any,
        *,
        interval_seconds: float = 3600,  # default: hourly check
        max_turns: int = 12,
    ):
        self.store = store
        self.backend = backend
        self.interval = max(60, interval_seconds)
        self.max_turns = max_turns
        self._last_run: float = 0.0
        self._running = False

    def due(self, now: float | None = None) -> bool:
        return (now or time.time()) - self._last_run >= self.interval

    async def maybe_run(self) -> list[dict] | None:
        """Check all profiled repos; regenerate wiki where HEAD moved."""
        if not self.due() or self._running:
            return None
        self._running = True
        try:
            return await self._run()
        finally:
            self._running = False
            self._last_run = time.time()

    async def _run(self) -> list[dict]:
        import subprocess
        from pathlib import Path

        from ..docs_gen import WikiGenerator
        from ..profile import ProjectProfile

        gen = WikiGenerator(self.backend, max_turns=self.max_turns)
        results: list[dict] = []

        projects = await self.store.list_projects()
        for proj in projects:
            for repo_path in (proj.get("repos") or []):
                repo = Path(repo_path).expanduser()
                if not repo.is_dir():
                    continue
                profile = ProjectProfile.load(repo)
                if not profile:
                    continue
                # Check HEAD vs stored wiki_commit.
                try:
                    r = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        capture_output=True, text=True, timeout=5,
                        cwd=repo,
                    )
                    head = r.stdout.strip() if r.returncode == 0 else ""
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue
                if not head or head == profile.wiki_commit:
                    continue

                log.info("wiki refresh: %s HEAD %s (was %s)",
                         repo, head[:8], profile.wiki_commit[:8] or "none")
                result = await gen.generate(repo)
                if not result.error and result.commit_sha:
                    profile.wiki_commit = result.commit_sha
                    profile.save()
                results.append({
                    "repo": str(repo),
                    "files": result.files_written,
                    "error": result.error,
                })
        return results
