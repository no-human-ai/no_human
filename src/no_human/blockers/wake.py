"""Wake-condition parsing + the parked-task watcher (PLAN.md 22.7).

A lightweight poller re-evaluates every ``blocked`` / ``paused_quota`` task: it
checks the machine-checkable wake condition (PR merged? quota back? CI green?
time elapsed?) and on satisfaction flips the task back to its prior working
state. Each parked task has a **max park duration** → escalate on timeout so
nothing is silently abandoned.

The condition grammar is deliberately tiny and machine-checkable:
  - ``after:<duration>``        e.g. ``after:2h`` — relative to when parked
  - ``quota_refreshed``         time-based; satisfied once ``wake_check_at`` passes
  - ``ci_green_on:<branch>``    delegated to an injected CI checker
  - ``pr_merged:<ref>`` / ``PR <ref> merged`` — delegated to an injected PR checker
  - ``null`` / empty            never self-wakes (waits for a human or timeout)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..core.db import Store
from ..core.task import Task, TaskStatus
from ..vcs.pr_outcome import observe_pr
from ..vcs.task_pr import resolve_task_pr
from .shipped import _TICK_ABORTED, complete_if_content_landed as _complete_landed
from .taxonomy import BlockerCategory, Blocker, resume_checkpoint, resume_provenance

log = logging.getLogger("no_human.wake")

# Async hooks the host wires in (live PR/CI lookups). Default: not satisfied.
PrMergedChecker = Callable[[str], Awaitable[bool]]
CiGreenChecker = Callable[[str], Awaitable[bool]]
# Returns (is_terminal, is_success) for a pipeline ID.
CiTerminalChecker = Callable[[str], Awaitable[tuple[bool, bool]]]
# Returns list of new PrComment objects for a PR ref.
PrCommentChecker = Callable[[str], Awaitable[list[Any]]]
# (repo_path, branch, base) -> whether branch's content already landed on
# base (a local, content-based check — see default_branch_shipped for why a
# squash merge makes ancestry the wrong test). Since 2026-08-12 the return is
# `str | bool | None`: a landed-commit SHA when the wired checker is
# `branch_landed_commit` (the anchor `_complete_if_content_landed` records),
# a bare bool from older/injected fakes. `bool(result)` is always the
# shipped/not-shipped answer; the str form is read only when present.
PrShippedChecker = Callable[[str, str, str], Awaitable[str | bool | None]]

_DURATION = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Bound on the default `ci_green` checker's `backend.trigger()` re-entry
# (A7). `trigger()` polls an already-running pipeline to TERMINAL — right for
# the orchestrator's own one-shot CI wait, wrong for a watcher tick that must
# re-evaluate every parked task and come back periodically. A timeout here
# reads as "not yet satisfied, ask again next tick", never as green.
_CI_GREEN_POLL_TIMEOUT_SECONDS = 25

# Platform-layer CI failures: the job never ran the code, so a red check
# carrying this signature is INFRA, not a coder fix round (live incident
# 2026-08-11: a GitHub Actions billing outage turned every review-PASSED
# task into escalated_ci at the finish line). The match is ONE full
# sentence, deliberately: partial phrases ("the job was not started
# because…") also appear in unrelated failures and in pytest echoes of this
# very corpus, and a match here SUPPRESSES reporting — overmatching is the
# dangerous direction. An unrecognized failure is always treated as real.
# A job GitHub blocks at START (billing wall) never runs, so the job-LOG API
# (the Jenkins consoleText fetcher) returns NOTHING for it — the failure text
# lives only in the check-run ANNOTATION (verified via `gh api
# .../check-runs/<id>/annotations` on PRs #171/#183, 2026-08-11/12 outage).
# `_check_pr_ci` below matches this regex over BOTH channels (ticket
# 8c8b36b5). blockers.pr_ci_policy=advisory remains the operator's cover for
# GitHub-hosted checks this doesn't (yet) positively classify.
_CI_INFRA_RE = re.compile(
    r"(?i)recent account payments have failed or your "
    r"spending limit needs to be increased"
)

# Sentinel returned by `_check_approval_pr_comments(resume=False)`: comments
# were injected into `send_back_feedback` and the cursor advanced, but no
# resume was performed — the caller (`_check_open_pr`) owns the single resume
# for this tick. Never leaks out of `_check_open_pr` itself.
_COMMENTS_INJECTED = "_comments_injected"


def parse_duration(text: str) -> timedelta | None:
    """Parse ``2h`` / ``30m`` / ``48h`` / ``1d`` into a timedelta, or None."""
    if not text:
        return None
    total = 0
    matched = False
    for num, unit in _DURATION.findall(text):
        total += int(num) * _UNIT_SECONDS[unit.lower()]
        matched = True
    return timedelta(seconds=total) if matched else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class WakeWatcher:
    """Polls parked tasks; resumes them when their wake condition fires, or
    escalates on max-park-duration timeout."""

    def __init__(
        self,
        store: Store,
        config: dict,
        *,
        pr_merged: PrMergedChecker | None = None,
        ci_green: CiGreenChecker | None = None,
        ci_terminal: CiTerminalChecker | None = None,
        pr_comment: PrCommentChecker | None = None,
        pr_state: Callable[[str], Awaitable[str]] | None = None,
        pr_checks: Callable[[str], Awaitable[list[dict]]] | None = None,
        pr_mergeable: Callable[[str], Awaitable[dict]] | None = None,
        ci_log: Callable[[str], Awaitable[str]] | None = None,
        ci_annotations: Callable[[str, str], Awaitable[str]] | None = None,
        pr_shipped: PrShippedChecker | None = None,
        on_event: Callable[[str, str], None] | None = None,
        ci_gate_gate: Any = None,
        derived_resolver: Callable[..., Any] | None = None,
    ):
        self.store = store
        blockers_cfg = (config or {}).get("blockers", {})
        self.max_park = parse_duration(
            str(blockers_cfg.get("max_park_duration", "48h"))
        ) or timedelta(hours=48)
        # The watcher's own check cadence — the base for the dead-machine-resume
        # backoff below. Same config key `nh wake --loop` polls at
        # (cli/commands.py:3178), so the backoff base is genuinely this
        # watcher's cadence, not an independent knob.
        self.wake_poll_interval = parse_duration(
            str(blockers_cfg.get("wake_poll_interval", "10m"))
        ) or timedelta(minutes=10)
        # Cap on autonomous PR-comment → revise cycles. A reviewer (or bot) can
        # post comments indefinitely; without this, each batch resets the full
        # attempt budget, so the agent could revise forever. After this many
        # rounds we escalate to the human instead of resuming (constraint §5,
        # bounded autonomy). Defaults to the same value as bounds.max_correction_rounds.
        self.max_revision_rounds = int(
            (config or {}).get("bounds", {}).get("max_correction_rounds", 2)
        )
        # Cap on autonomous CI-failure → fix cycles on an open PR (Jules /
        # Copilot pattern: bounded rounds, then hand the specific failure to a  # term-ok: real behavior-pattern reference
        # human). Counted per distinct failure signature, so a re-run of the
        # same red check doesn't burn a round.
        self.max_ci_fix_rounds = int(blockers_cfg.get("max_ci_fix_rounds", 3))
        # TEMPORARY OPERATOR OVERRIDE (2026-08-12): "advisory" makes rung 5
        # record a red PR check without counting a fix round, resuming the
        # coder, or escalating — the operator's instruction while the private
        # repo's GitHub Actions quota is exhausted for the month ("DO NOT FAIL
        # TASKS ON IT FOR NOW"). The default is "enforce"; the override lives
        # in ~/.no_human/config.yaml and MUST be removed at go-public (public
        # repos have unlimited Actions minutes, so CI becomes meaningful
        # again). The billing-signature classifier below stays either way —
        # it is the permanent, self-reverting handling for platform-layer
        # failures; this knob exists because a quota-blocked job may expose
        # no log at all, which the fail-closed classifier cannot see.
        self.pr_ci_policy = str(
            blockers_cfg.get("pr_ci_policy", "enforce")
        ).strip().lower()
        if self.pr_ci_policy not in ("enforce", "advisory"):
            # Fail closed, loudly: a typo ("Advisory ") silently meaning
            # "enforce" would re-escalate every task mid-outage with no signal.
            log.warning(
                "blockers.pr_ci_policy %r is not 'enforce'/'advisory' — "
                "falling back to 'enforce'", self.pr_ci_policy,
            )
            self.pr_ci_policy = "enforce"
        # Bounded PR-conflict → rebase cycles (SCRUM-41), same pattern: a PR
        # that textually conflicts with main is invisible to CI (branch checks
        # stay green through it) — only `gh pr view --json mergeable` exposes
        # it. Counted per detected CONFLICTING state; resets only once GitHub
        # confirms MERGEABLE (a later conflict is then a fresh cycle).
        self.max_pr_conflict_rounds = int(
            blockers_cfg.get("max_pr_conflict_rounds", 3)
        )
        # Stuck-active watchdog threshold (minutes). Default 40 > the 30-min
        # run_tests timeout, so a long test never trips it; a genuinely hung
        # session does. 0 disables.
        self.stuck_active_minutes = float(
            blockers_cfg.get("stuck_active_minutes", 40))
        # Bounded CI_GATE-integration-failure → fix cycles (M6), same pattern.
        self.max_ci_gate_fix_rounds = int(
            blockers_cfg.get("max_ci_gate_fix_rounds", 3)
        )
        # Comment authors whose PR comments never trigger a revision. Live
        # incident: a CI service account posts a unit-test-results table on every
        # build, which the comment rung injected as human feedback and resumed
        # the task — one wasted attempt per PR, forever. "[bot]" logins are
        # always ignored on top of this list. In-code default rather than
        # config.py DEFAULTS because a user yaml `blockers:` section replaces
        # that map wholesale (the deep-merge shadowing trap).
        self.ignore_comment_authors = {
            str(a).lower()
            for a in blockers_cfg.get("ignore_comment_authors", [])
        }
        self.config = config or {}
        self._pr_merged = pr_merged
        # Default to the real checkers when the caller doesn't inject one
        # (mirrors `ci_gate_gate` just below). Every WakeWatcher construction
        # site used to pass neither, so `ci_green_on:<branch>` / `ci_terminal_on:`
        # could NEVER fire — orchestrator.py parks human-gated CI promising "the
        # task resumes when it is green" and nothing made that true (audit A7,
        # 2026-08-11). These need `self.store`/`self.config`, unlike the
        # stateless `default_pr_merged`-style helpers, so they are bound
        # methods rather than free functions passed in from the call sites —
        # every WakeWatcher gets a working checker by construction, including
        # future construction sites that forget to ask for one.
        self._ci_green = ci_green or self._default_ci_green
        self._ci_terminal = ci_terminal or self._default_ci_terminal
        self._pr_comment = pr_comment
        self._pr_state = pr_state
        self._pr_checks = pr_checks
        self._pr_mergeable = pr_mergeable
        self._ci_log = ci_log
        self._ci_annotations = ci_annotations
        self._pr_shipped = pr_shipped
        self._on_event = on_event or (lambda kind, text: None)
        # Mechanical derived-artefact conflict resolver (SCRUM-?? rebase-round
        # bugfix). Injectable for tests, following the same idiom as every
        # other checker above; `None` means "use the real one", resolved
        # lazily inside `_check_pr_conflict` (blockers -> vcs import, as
        # `shipped.py` documents) so this module's import graph is unchanged.
        self._derived_resolver = derived_resolver
        # The post-PR CI_GATE integration gate (M6). Injectable for tests;
        # by default built here (the single wiring point for all three hosts)
        # and only when ci_gate.enabled — otherwise the rung is a no-op.
        if ci_gate_gate is None and (config or {}).get("ci_gate", {}).get("enabled"):
            ci_gate_gate = self._default_ci_gate_gate(config)
        self._ci_gate_gate = ci_gate_gate

    @property
    def pr_shipped(self):
        """Read-only access to the wired content-shipped probe (or ``None`` if
        this host never wired one). Lets a second caller — the scheduler's
        resume-dispatch gate — reuse the exact same probe this watcher's own
        rungs use, without reaching into a private attribute."""
        return self._pr_shipped

    @staticmethod
    def _default_ci_gate_gate(config: dict):
        """Build the real gate (gh/glab/kubectl-backed). Lazy import so hosts
        that never enable CI_GATE pay nothing; returns None if wiring fails —
        the watcher must keep running without the rung, not crash."""
        try:
            from ..ci_gate.gate import CiGate
            from ..vcs.pr_watcher import (
                default_pr_checks, default_pr_files, default_pr_head,
                parse_pr_url, upsert_agent_comment,
            )

            async def _post_comment(url: str, body: str) -> bool:
                parsed = parse_pr_url(url)
                if not parsed or parsed[0] != "github":
                    return False
                _, host, slug, num = parsed
                # UPDATE the one CI_GATE comment instead of posting a new one every
                # attempt (a PR once accumulated 17 near-identical comments).
                return await upsert_agent_comment(f"{host}/{slug}#{num}", body, key="ci_gate")

            return CiGate(
                config,
                pr_head=default_pr_head,
                pr_files=default_pr_files,
                pr_checks=default_pr_checks,
                post_comment=_post_comment,
            )
        except Exception:  # noqa: BLE001
            log.warning("CI_GATE gate wiring failed — rung disabled", exc_info=True)
            return None

    # ------------------------- default CI checkers -------------------------- #
    #
    # `condition_satisfied` calls `self._ci_green(branch)` /
    # `self._ci_terminal(pipeline_ref)` with only the string parsed out of the
    # condition (the same contract every injected test double in
    # tests/test_blockers.py uses) — neither carries the task or its repo. The
    # defaults below recover that context by looking up the BLOCKED task that
    # owns the exact condition string, then rebuild the CI backend from it:
    # preferably the literal conf `_park_human_gated_ci` captured at park time
    # (`task.context["human_gated_ci"]["ci_conf"]`, set from
    # `Orchestrator.ci_runner_conf` — the EXACT source that gated this run),
    # falling back to re-resolving profile-then-global for the task's repo via
    # `resolve_ci_backend_for_repo` (the same precedence
    # `Orchestrator._resolve_ci_runner` uses). Either way this fixes A7: the
    # prior (removed) wiring read only the global `ci:` block via
    # `ci_from_config(config)`, so a profile-only CI config — the common,
    # human-confirmed `nh onboard` path — could never resolve a backend here,
    # and `ci_green_on:<branch>` stayed permanently unreachable.
    #
    # Never resolving a backend, or any error along the way, reads as "not
    # satisfied yet" — never as green. A task this can't answer is freed by
    # the max-park-duration timeout escalation, not by a guess here.

    async def _find_parked_task_for_condition(self, condition: str) -> Task | None:
        """The BLOCKED task whose ``blocker.wake_condition`` is exactly
        *condition*. Branches/pipeline refs this product generates are unique
        enough in practice that one task owns a given condition; the first
        match is used."""
        for t in await self.store.list_tasks(TaskStatus.BLOCKED):
            if ((t.blocker or {}).get("wake_condition") or "") == condition:
                return t
        return None

    def _resolve_ci_backend_for_task(self, task: Task, stored_conf: dict | None):
        from ..ci import ci_from_config, resolve_ci_backend_for_repo

        if stored_conf:
            try:
                built = ci_from_config({"ci": {**stored_conf, "enabled": True}})
            except Exception:  # noqa: BLE001 — fall back to re-resolving below
                built = None
            if built is not None:
                return built
        return resolve_ci_backend_for_repo(self.config, task.repo_path)

    async def _default_ci_green(self, branch: str) -> bool:
        """Real ``ci_green_on:<branch>`` checker (A7)."""
        task = await self._find_parked_task_for_condition(f"ci_green_on:{branch}")
        if task is None:
            return False
        hg = (task.context or {}).get("human_gated_ci") or {}
        backend = self._resolve_ci_backend_for_task(task, hg.get("ci_conf"))
        if backend is None:
            return False
        from ..ci.base import HumanGatedCI

        try:
            # Bounded: on a backend whose pipeline is already running,
            # `trigger()` polls it to TERMINAL (up to `ci.timeout_minutes`,
            # default an hour) — correct for the orchestrator's own one-shot
            # CI wait, wrong here: a watcher tick re-evaluates every parked
            # task and must not stall on one of them.
            result = await asyncio.wait_for(
                backend.trigger(branch), timeout=_CI_GREEN_POLL_TIMEOUT_SECONDS)
        except HumanGatedCI:
            return False  # still nobody started it
        except asyncio.TimeoutError:
            return False  # still running, or slow to answer — retry next tick
        except Exception as exc:  # noqa: BLE001 — checker must never crash watcher
            log.warning("default ci_green checker failed for %r: %s", branch, exc)
            return False
        return bool(result.passed)

    async def _default_ci_terminal(self, pipeline_ref: str) -> tuple[bool, bool]:
        """Real ``ci_terminal_on:<pipeline_ref>`` checker (A7). A single
        non-blocking poll — always safe to call every tick."""
        task = await self._find_parked_task_for_condition(
            f"ci_terminal_on:{pipeline_ref}")
        if task is None:
            return (False, False)
        hg = (task.context or {}).get("human_gated_ci") or {}
        backend = self._resolve_ci_backend_for_task(task, hg.get("ci_conf"))
        if backend is None:
            return (False, False)
        try:
            result = await backend.check_status(pipeline_ref)
        except Exception as exc:  # noqa: BLE001 — checker must never crash watcher
            log.warning("default ci_terminal checker failed for %r: %s",
                        pipeline_ref, exc)
            return (False, False)
        return (result.status.is_terminal, result.passed)

    # ----------------------------- condition ------------------------------- #

    async def condition_satisfied(
        self, condition: str | None, *, raised_at: datetime, now: datetime,
        wake_check_at: datetime | None,
    ) -> bool:
        """Evaluate one wake condition. Unknown / null conditions never self-fire
        (the timeout path is what eventually frees them)."""
        if not condition:
            return False
        cond = condition.strip()
        low = cond.lower()

        if low.startswith("after:"):
            dur = parse_duration(cond.split(":", 1)[1])
            return dur is not None and now - raised_at >= dur

        if low in ("quota_refreshed", "quota", "quota_reset"):
            # Quota parks set wake_check_at to the expected reset time.
            return wake_check_at is not None and now >= wake_check_at

        if low.startswith("ci_green_on:"):
            branch = cond.split(":", 1)[1].strip()
            if self._ci_green is None:
                return False
            try:
                return await self._ci_green(branch)
            except Exception as exc:  # noqa: BLE001 — checker must never crash watcher
                log.warning("ci_green checker failed: %s", exc)
                return False

        if low.startswith("pr_comment_on:"):
            pr_ref = cond.split(":", 1)[1].strip()
            # Feedback the task could act on — NOT merely "a comment exists".
            # The rung used to satisfy on len(comments) > 0, so the task's own
            # marked comment (an abandoned-draft note, a verification receipt,
            # a CI_GATE table) woke the very task that posted it, which then
            # found nothing to revise — `_inject_pr_feedback` filters self and
            # bot chatter — and burned an attempt on an empty round.
            return bool(await self._human_pr_comments(pr_ref))

        if low.startswith("ci_terminal_on:"):
            pipeline_ref = cond.split(":", 1)[1].strip()
            if self._ci_terminal is None:
                return False
            try:
                is_terminal, _is_success = await self._ci_terminal(pipeline_ref)
                return is_terminal
            except Exception as exc:  # noqa: BLE001
                log.warning("ci_terminal checker failed: %s", exc)
                return False

        ref = None
        if low.startswith("pr_merged:"):
            ref = cond.split(":", 1)[1].strip()
        else:
            m = re.match(r"pr\s+(\S+)\s+merged", low)
            if m:
                ref = m.group(1)
        if ref is not None:
            if self._pr_merged is None:
                return False
            try:
                return await self._pr_merged(ref)
            except Exception as exc:  # noqa: BLE001
                log.warning("pr_merged checker failed: %s", exc)
                return False

        # Time has passed the explicit re-check stamp, with no richer condition.
        return wake_check_at is not None and now >= wake_check_at

    # ------------------------------- tick ---------------------------------- #

    async def tick(
        self, *, now: datetime | None = None,
        active_ids: set[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Re-evaluate all parked tasks once. Returns (task_id, action) tuples
        where action is 'resumed' or 'escalated_timeout'.

        ``active_ids`` is the caller's set of worker-CLAIMED task ids; the
        stuck-active sweep judges only those. A resumed task waiting in an
        active status for a free worker slot is silent because nothing is
        running it — parking it as "hung" re-created its escalation every 40
        minutes behind a deep queue (live, 2026-07-24). ``None`` means the
        caller cannot know what is claimed (standalone ``nh wake``), so the
        sweep — whose whole purpose is freeing hung worker slots — is skipped.
        """
        now = now or datetime.now(timezone.utc)
        actions: list[tuple[str, str]] = []
        for status in (TaskStatus.BLOCKED, TaskStatus.PAUSED_QUOTA,
                       TaskStatus.AWAITING_INPUT, TaskStatus.AWAITING_APPROVAL):
            for task in await self.store.list_tasks(status):
                action = await self._evaluate(task, now=now)
                if action:
                    actions.append((task.id, action))
                else:
                    await self._heartbeat(task, now=now)
        # Stuck-active watchdog: a task frozen mid-run (e.g. a hung Agent-SDK
        # session that even the reviewer's own timeout can't cancel — observed
        # 2026-07-11) would otherwise sit in an active state forever, holding a
        # worker slot and never failing honestly. Escalate one with NO event
        # for longer than the threshold (set above the 30-min test timeout, so
        # a legitimately long test run never trips it). Scope: only tasks the
        # caller actually CLAIMED — see the docstring.
        if active_ids is not None:
            for status in (TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
                           TaskStatus.TESTING, TaskStatus.PLANNING,
                           TaskStatus.CONTEXT):
                for task in await self.store.list_tasks(status):
                    if task.id not in active_ids:
                        continue
                    if await self._escalate_if_stalled(task, now=now):
                        actions.append((task.id, "escalated_stalled"))
        return actions

    async def _escalate_if_stalled(self, task: Task, *, now: datetime) -> bool:
        """Escalate a task that has emitted no event for longer than the
        stuck-active threshold. Returns True iff it escalated."""
        if self.stuck_active_minutes <= 0:
            return False  # watchdog disabled
        if getattr(task, "cancel_requested", None):
            return False  # a pause is already in flight; let it land
        last_ts = await self.store.last_event_ts(task.id)
        if last_ts is None:
            return False  # never emitted — leave to the normal loop / startup
        age_min = (now.timestamp() - last_ts) / 60.0
        if age_min < self.stuck_active_minutes:
            return False
        # Load-bearing terminal guard (SCRUM-68) — a task shipped/cancelled
        # between the caller's list fetch and this write must not be flipped
        # to ESCALATED by the stall watchdog.
        if await self._is_terminal(task):
            return False
        data = task.blocker or {}
        data["category"] = "NOVEL_UNKNOWN"
        data["question"] = (
            f"This task stalled in {task.status.value} — no activity for "
            f"{age_min:.0f} min. The agent/reviewer session likely hung. "
            "Resume to retry, or take over?")
        data["root_cause_hypothesis"] = (
            f"no event for {age_min:.0f} min while {task.status.value}; "
            "probable hung Agent-SDK session")
        task.blocker = data
        await self.store.update_task_columns(task)
        await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
        await self._emit(task, "escalated_stalled",
                         f"{task.id[:8]} stalled in {task.status.value} "
                         f"({age_min:.0f}m no activity) — escalated")
        return True

    # Throttled liveness proof. A healthy parked task produces no action
    # events (the watcher acts only on change), which is indistinguishable
    # from a dead watcher in the record — the server ran one for a full day.
    # One wake_tick per task per hour bounds the noise while making "the
    # watcher is checking this task" a queryable fact (`nh doctor` reads it).
    HEARTBEAT = timedelta(hours=1)

    # Death-blind resume loop guard (forensics 2026-08-13/08-20): a dead
    # machine-resumed attempt (0 priced tokens, <=1 turn) that keeps getting
    # re-resumed burns the task's own lifetime budget on a worker/environment
    # failure, never the task's own. Streak 1-2 backs off instead of
    # resuming; streak 3 parks honestly rather than resuming a 4th time.
    DEAD_RESUME_PARK_STREAK = 3
    DEAD_RESUME_BACKOFF_CAP = timedelta(hours=6)

    async def _heartbeat(self, task: Task, *, now: datetime) -> None:
        last = _parse_iso((task.context or {}).get("last_wake_tick"))
        if last and now - last < self.HEARTBEAT:
            return
        try:
            # Atomic merge — the heartbeat must never clobber a concurrent
            # writer's context (it did: the watcher ticks every parked task
            # while the CLI and gate write the same rows).
            task.context = await self.store.merge_context(
                task.id, {"last_wake_tick": now.isoformat()})
            await self.store.save_events(task.id, [{
                "source": "watcher", "kind": "wake_tick",
                "text": f"watcher checked ({task.status.value}): nothing to do",
                "ts": time.time(),
            }])
        except Exception:  # noqa: BLE001 — a heartbeat must never break the tick
            log.warning("wake heartbeat failed for %s", task.id[:8], exc_info=True)

    async def _is_terminal(self, task: Task) -> bool:
        """True once only an explicit human verb (never this watcher) may
        revive the task: done, or cancelled (FAILED + a cancel_reason —
        there is no separate 'cancelled' status; see api/app.py's cancel
        endpoint). Re-reads the store instead of trusting the possibly-stale
        `task` object: a concurrent POST /shipped or /cancel can land mid-tick,
        between a rung's own network poll (PR state/comments/checks/mergeable)
        and its write — live incident SCRUM-68, where a done task's PR got a
        post-merge comment and the pr_feedback rung resumed it to implementing."""
        current = await self.store.get_task(task.id)
        if current is None:
            return False
        if current.status == TaskStatus.DONE:
            return True
        return current.status == TaskStatus.FAILED and bool(
            (current.context or {}).get("cancel_reason"))

    async def _evaluate(self, task: Task, *, now: datetime) -> str | None:
        # Terminal is terminal — checked first, before any rung does any work.
        if await self._is_terminal(task):
            return None

        # Re-read the LIVE row before deciding anything else. `task` may be a
        # snapshot taken by `tick()`'s `list_tasks(BLOCKED)` fetch — a
        # concurrent `restore-approval` (or a shipped/DONE landing) can flip
        # the real row's status *between* that fetch and this call. Deciding
        # off the stale handle's `.status` is exactly the 2026-08-11 incident:
        # a task moved to `awaiting_approval` with its old wake condition still
        # armed still read as `blocked` here and fell into the wake-condition
        # rung below, resuming a task whose review-PASSED work had just landed.
        current = await self.store.get_task(task.id)
        if current is None:
            return None
        if await self._disarm_stale_wake(task, current):
            return None

        # An open PR: shepherd it. Merged → done; closed-unmerged → escalate;
        # new human comments → revise (B4); red CI on the PR head → bounded fix
        # loop (M1). It NEVER times out — a PR may wait for human approval
        # indefinitely.
        #
        # A human chose "stop — keep the work parked as-is" (SCRUM-22's
        # terminal park). Review 2026-07-25: without this skip the sweep
        # undid the stop — max_park re-escalated the task within 48h and any
        # wake_condition on the blocker resumed it. Human decisions outrank
        # every automatic branch below; only another human reply changes it.
        #
        # LOAD-BEARING ORDERING: this must run BEFORE the AWAITING_APPROVAL
        # branch just below. That branch is the door the pr_conflict rung
        # comes through — `_check_open_pr` polls the forge, can shepherd a
        # conflicting/commented PR, and resumes on its own via `_resume`
        # (which re-checks `_is_terminal` but NOT the hold). A held task
        # reaching AWAITING_APPROVAL must never enter that ladder at all: no
        # forge polls, no `pr_conflict_rounds` bump, no resume. Read off
        # `current` (the fresh row fetched above), not the possibly-stale
        # `task` snapshot — same reasoning as the AWAITING_APPROVAL branch's
        # own comment.
        if (current.blocker or {}).get("human_stopped"):
            return None

        # `current.status` (the fresh read above), not `task.status`: `task`
        # can be the stale snapshot `tick()` listed under `BLOCKED` before a
        # concurrent restore-approval flipped the live row to
        # `awaiting_approval` — deciding off the stale status here would fall
        # through to the wake-condition rung below using `task`'s own
        # (possibly already-cleared-on-the-row-but-not-on-this-handle)
        # blocker, the exact 2026-08-11 incident.
        if current.status == TaskStatus.AWAITING_APPROVAL:
            return await self._check_open_pr(task)

        blocker = Blocker.from_dict(task.blocker) if task.blocker else None
        raised_at = _parse_iso(blocker.raised_at if blocker else None) \
            or _parse_iso(task.updated_at) or now
        wake_check_at = _parse_iso(task.wake_check_at)

        # A typed PAUSE (see taxonomy.py's module docstring) resumes in one
        # step — `nh task resume` / `POST /resume` — never by a machine
        # condition. Nothing SHOULD carry a `wake_condition` on a pause, but
        # a stale one surviving on a `paused_over` record must not silently
        # resume a deliberately-parked task; it still times out below, same
        # as every other parked blocker.
        paused = blocker is not None and blocker.category is BlockerCategory.USER_PAUSED

        # AWAITING_INPUT only ever resumes on a human reply — but it still
        # times out so a forgotten question doesn't sit forever.
        condition = blocker.wake_condition if blocker else None
        if task.status != TaskStatus.AWAITING_INPUT and not paused:
            satisfied = await self.condition_satisfied(
                condition, raised_at=raised_at, now=now, wake_check_at=wake_check_at,
            )
            if satisfied:
                # condition_satisfied may have awaited a live checker (network);
                # re-verify terminal-ness right before acting on it (wake_condition
                # rung, and the pr_feedback rung it can trigger below).
                if await self._is_terminal(task):
                    return None
                # If the condition is pr_comment_on, inject the comments as feedback.
                # `rounds` stays 0 for every other condition — only None means
                # "the injection delivered nothing", and only that falls through.
                rounds: int | None = 0
                if condition and condition.strip().lower().startswith("pr_comment_on:"):
                    pr_ref = condition.split(":", 1)[1].strip()
                    # A comment on a PR whose content already shipped is a
                    # post-mortem note, not a work order (2026-08-12
                    # incident) — check state+shipped before ever injecting
                    # it as revision feedback.
                    landed = await self._comment_after_landing(
                        task, pr_ref, may_complete=False)
                    if landed is not None:
                        # Do not resume — but do not return either: treat it
                        # exactly like the "injection delivered nothing" case
                        # below and fall through to the max_park check.
                        rounds = None
                    else:
                        rounds = await self._inject_pr_feedback(task, condition)
                        # Bound the comment→revise loop: after max_revision_rounds
                        # autonomous rounds, escalate to the human rather than resume.
                        if rounds is not None and rounds > self.max_revision_rounds:
                            await self._escalate_revisions(task, rounds)
                            return "escalated_revisions"
                if rounds is not None:
                    return await self._resume(task, now=now)
                # The rung and the injection each fetch, so they can see
                # different data however well they agree on the predicate: a 502
                # between the two calls, a comment deleted or edited, or the task
                # going terminal mid-await all end here. Resuming anyway is what
                # burned an attempt on an empty round — the very failure the
                # marker was added to stop. So: do NOT resume, but FALL THROUGH
                # to the max_park check below rather than returning. An early
                # return here stranded the task forever on an ALTERNATING forge
                # (answers the rung, fails the injection — gh secondary rate
                # limits, which this design meets twice as often because it
                # fetches twice per tick): no resume, no escalation, the human's
                # review never delivered. Inside max_park the next tick still
                # re-decides on a fresh read; past it, the timeout escalates.

        # Timeout → escalate (never silently abandon). Re-verify: max_park
        # re-escalation must not revive a task a human already closed out.
        if now - raised_at >= self.max_park:
            if await self._is_terminal(task):
                return None
            await self._escalate_timeout(task, blocker)
            return "escalated_timeout"
        return None

    async def _disarm_stale_wake(self, task: Task, current: Task) -> bool:
        """Belt-and-suspenders: a task whose LIVE status is already
        ``awaiting_approval`` or ``done`` must never carry an armed wake
        condition — whichever path put it there (a restore-approval race, a
        record repaired some other way, a stale handle) — or the next tick's
        wake-condition rung can still resume it. Clears the arm, persists it,
        and emits ``wake_disarmed``. Returns True iff it disarmed something,
        in which case the caller must stop the tick for this task (no rung
        below may run against a row that just got rewritten out from under
        it).
        """
        if current.status not in (TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE):
            return False
        blocker = current.blocker or {}
        condition = blocker.get("wake_condition")
        armed = current.wake_check_at is not None or bool(condition)
        if not armed:
            return False
        prior_wake_check_at = current.wake_check_at
        current.wake_check_at = None
        if condition:
            blocker = dict(blocker)
            del blocker["wake_condition"]
            current.blocker = blocker or None
        await self.store.update_task_columns(current)
        await self._emit(
            current, "wake_disarmed",
            f"{current.id[:8]} is {current.status.value} — cleared stale "
            f"wake_condition={condition!r} wake_check_at={prior_wake_check_at!r}",
        )
        return True

    async def _resume(self, task: Task, *, now: datetime | None = None) -> str:
        """Flip a parked task back to its prior working state (IMPLEMENTING).

        Resume re-enters the loop in a fresh session seeded with the report
        (22.5) — the orchestrator picks it up from the [WIP-BLOCKED] checkpoint.

        ``now`` is keyword-only with a real-clock default so every existing
        call site keeps working unchanged; the wake-condition rung (the one
        with a tick ``now`` in scope) passes it through for deterministic
        backoff math.
        """
        # LOAD-BEARING terminal guard (SCRUM-68). The rung-level rechecks above
        # this call are cheap early-outs; THIS one is the invariant — every
        # resume path, present or future, funnels through here, and any await
        # a rung did since its own recheck reopens the race this closes.
        if await self._is_terminal(task):
            return "skipped_terminal"
        # Invariant twin of the terminal guard just above: every present and
        # future resume path (wake-condition rung, pr_conflict/comment/CI
        # rungs via `_check_open_pr`, `Scheduler._resume_quota_parks` via
        # `resume_now`) funnels through this one chokepoint, so this is the
        # one place a durable human hold can be enforced without trusting
        # every caller to re-check it. The `_evaluate` early-out above is
        # only a cheap short-circuit for the common tick path; this is the
        # backstop. Read off `task.blocker` exactly as `_evaluate` does, for
        # one spelling of the check.
        if (task.blocker or {}).get("human_stopped"):
            return "skipped_human_stopped"
        now = now or datetime.now(timezone.utc)

        # Death-blind resume loop guard. Every resume — whichever of the five
        # rungs asked for it — funnels through here, so this is the single
        # chokepoint that can see "the last machine resume died before doing
        # any work" and stop repeating it blindly.
        dead_state = (task.context or {}).get("wake_dead_resumes") or {}
        backoff_until = _parse_iso(dead_state.get("backoff_until"))
        if backoff_until is not None and backoff_until > now:
            # Still inside a previously-decided backoff window (e.g. a
            # network-checked condition like pr_merged fired early). The
            # event already recorded this decision once; re-emitting every
            # tick would be noise, and re-evaluating would double-count the
            # same window.
            return "wake_backoff_pending"

        verdict, streak, dead_ids, retry_reason = await self._dead_resume_verdict(
            task, now=now)
        if verdict == "backoff":
            await self._backoff_dead_resume(
                task, streak, dead_ids,
                anchor=dead_state.get("last_resume_at"), now=now,
            )
            return "wake_backoff"
        if verdict == "park":
            await self._park_dead_resumes(task, streak, dead_ids)
            return "parked_dead_resumes"
        # verdict is "proceed" (streak 0 — nothing dead, or a human/real-work
        # resume) or "retry" (a backoff rung's window expired: DISPATCH now,
        # carrying the ladder, rather than counting this tick as a second
        # dead attempt — see `_dead_resume_verdict`).

        patch = {
            "resumed_at": now_iso(),
            "resume_reason": "wake_condition_satisfied",
        }
        # Same contract as `nh reply` / `nh task resume`: continue from the
        # checkpoint the blocker recorded, or the next attempt branches from a
        # stale sha and discards the parked attempt's committed work.
        checkpoint = resume_checkpoint(task.blocker)
        # Stamp the provenance UNCONDITIONALLY: the zero-diff honesty gate must
        # be able to tell a MACHINE resume (a timer, a CI rung, an auto-rebase)
        # from a human answering a blocker, and crediting a machine resume opens
        # a PR on work no attempt produced.
        #
        # 🔴 This is deliberately NOT inside `if checkpoint:`. Gating the stamp
        # on the checkpoint is what made `by` a ONE-WAY LATCH through five review
        # rounds: `resume_from` is merged with RFC 7396, so when this resume had
        # no checkpoint of its own the write was skipped entirely and a `by`
        # written by the PREVIOUS actor survived to describe THIS one. Whichever
        # order the reader then used, it was wrong in one direction — a stale
        # "human" credited a timer's re-entry, a stale "wake" failed a human's
        # answer as fabrication. `by` must always describe the resume that is
        # actually happening, so it is written every time, checkpoint or not.
        # `resume_reason` beside it says the same thing and is kept for rows
        # written before provenance existed.
        patch["resume_from"] = resume_provenance(checkpoint, "wake")
        if verdict == "retry":
            # A backoff rung's window expired: this IS the re-dispatch that
            # rung was always supposed to end in — carry the ladder (streak,
            # dead_ids) forward but re-anchor the window at THIS resume, so
            # only attempts started at or after it are judged next time; the
            # already-counted dead row falls out of scope instead of being
            # re-counted on the next tick.
            patch["wake_dead_resumes"] = {
                "streak": streak, "attempt_ids": dead_ids,
                "last_resume_at": patch["resumed_at"], "backoff_until": None,
            }
        else:
            # verdict == "proceed": a real dispatch (human, a fresh/no-anchor
            # task, a window with nothing dead on the table, or a window
            # whose attributed rows did real priced work before they were
            # attributed) resets the dead-resume streak and re-anchors the
            # window at THIS resume: only attempts started at or after it
            # count toward the next streak evaluation. An attributed-only
            # window that is ALSO dead-shaped (a quota wall, a dead SDK
            # session — 0 priced tokens, <=1 turn) is NOT evidence of health
            # and must not land here while a ladder exists to protect:
            # `_dead_resume_verdict` routes that case to "retry" instead, so
            # this reset only ever fires when there is nothing to lose —
            # either no rows at all, an attributed-only window with no prior
            # streak, or an attributed row that did real work despite being
            # attributed (attribution says WHY an attempt ended, not whether
            # it did anything first — a wall landing after a genuine session
            # is exactly as healthy as any other real dispatch). Resetting
            # on a dead-shaped attributed row WOULD be evidence-free: it
            # would launder the ladder away and let an environment
            # alternating a death-blind dead dispatch with an attributed
            # wall run forever without ever reaching the park.
            patch["wake_dead_resumes"] = {
                "streak": 0, "attempt_ids": [],
                "last_resume_at": patch["resumed_at"], "backoff_until": None,
            }
        task.context = await self.store.merge_context(task.id, patch)
        task.wake_check_at = None
        await self.store.update_task_columns(task)
        await self.store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
        event_text = f"{task.id[:8]} wake condition satisfied"
        if verdict == "retry":
            if retry_reason == "attributed_wall":
                # No backoff window was ever armed for this rung — an
                # attributed wall (quota/session, not death-blind) carried an
                # existing dead-resume ladder forward. Saying "after
                # dead-resume backoff" here would claim a timer fired when
                # none did; MEDIUM finding from the 2026-08-22 review.
                event_text += (
                    f" — re-dispatch after an attributed wall carried "
                    f"dead-resume streak #{streak}/"
                    f"{self.DEAD_RESUME_PARK_STREAK} forward (no backoff "
                    f"window involved)"
                )
            else:
                event_text += (
                    f" — re-dispatch after dead-resume backoff #{streak}/"
                    f"{self.DEAD_RESUME_PARK_STREAK}"
                )
        await self._emit(task, "resumed", event_text)
        return "resumed"

    async def resume_now(self, task: Task, *, now: datetime | None = None) -> str:
        """Public entry to the one `_resume` chokepoint, for callers outside
        the wake rungs (the scheduler's quota-park sweep) that need to force
        a resume decision without re-implementing checkpoint/provenance/
        dead-resume semantics. Same return contract as `_resume`."""
        return await self._resume(task, now=now)

    async def _dead_resume_verdict(
        self, task: Task, *, now: datetime,
    ) -> tuple[str, int, list[str], str | None]:
        """Whether the machine resume due right now would repeat a dead
        pattern. Returns ``(verdict, streak, dead_attempt_ids, retry_reason)``
        where ``verdict`` is ``"proceed"`` (streak 0, resume normally),
        ``"retry"`` (either: the window's dead rows are ALL already counted
        toward the ladder, so nothing NEW was tried since it was last
        evaluated — note `prior_ids` only becomes non-empty via
        `_backoff_dead_resume`, which always arms a window, so by the time
        this arm is reachable in the real loop one has been set — or the
        window since the last resume
        contains rows, EVERY one of them is attributed, and every one is
        also dead-shaped (0 priced tokens AND <= 1 turn — `_is_dead`, not
        "did no priced work": an attributed row with 0 tokens but several
        turns RESETS the ladder), with an existing ladder to protect;
        dispatch now, carrying the ladder forward, instead of counting this
        tick), ``"backoff"`` (streak 1-2, defer instead of
        resuming) or ``"park"`` (streak >= DEAD_RESUME_PARK_STREAK, escalate
        instead of resuming). ``retry_reason`` is only meaningful when
        ``verdict == "retry"``: ``"expired_backoff"`` (the window's dead
        rows were all already counted) or ``"attributed_wall"``
        (an attributed dead-shaped row
        carried the ladder forward) — `_resume` uses it to phrase the
        "resumed" event honestly instead of always claiming a backoff timer
        fired. It is ``None`` for every other verdict.

        🔴 THE STREAK USED TO ADVANCE ON EVERY CONSECUTIVE EVALUATION that
        reached this point with only dead evidence on the table — not only
        when a NEW dead attempt appeared. The prior version of this docstring
        argued that was fine because "streak 1/2 are deliberately never
        dispatched, so no new attempt row can appear between one evaluation
        and the next" — and treated an evaluation gated behind an
        already-expired backoff window as itself the "would resume into the
        same failure again" signal. That argument was the defect: a rung that
        never re-dispatches is not evidence of anything repeating — nothing
        was RE-TRIED. It produced exactly one real dispatch (the single dead
        attempt) plus two bare timer ticks, and escalated with "3 consecutive
        dead machine resumes" naming that one attempt id three times — false,
        four times on 2026-08-21 (2c8f23ff, 0986460c, f8de9cdf, e037008e).
        STREAK NOW COUNTS ATTEMPTS, NEVER TICKS: it is structurally
        ``len(dead_attempt_ids)`` (see the ``merged_ids`` derivation below),
        so the escalation text is true by construction. A backoff window
        expiring with no new attempt since it was armed returns ``"retry"``
        — `_resume` dispatches on it, carrying the ladder but re-anchoring
        `last_resume_at` at the new dispatch, so the NEXT evaluation only
        sees rows started after it. Only a genuinely new dead attempt row
        advances the streak.

        🔴 SAME RE-ANCHORING ALSO CREATES A SECOND WAY TO LOSE THE LADDER: an
        attributed-only window (see the ``attributed``/``relevant`` split
        below) also returns ``"retry"`` when there is an existing ladder to
        protect, rather than ``"proceed"``. Re-anchoring `last_resume_at` at
        every retry dispatch means a previously-counted dead row falls out
        of scope the moment the loop re-dispatches into it; if that next
        window then contains only an attributed wall, treating it as
        `"proceed"` would silently reset the streak to 0 even though nothing
        healthy happened — an environment alternating a death-blind dead
        dispatch with an attributed wall would then never reach `"park"`.
        An attributed row is evidence the environment blocked, not evidence
        the worker is healthy, so it is neutral BY DEFAULT: it carries the
        ladder forward instead of resetting it. That neutrality holds only
        while the attributed row is ALSO dead-shaped (0 priced tokens, <=1
        turn) — a wall that lands AFTER real priced work (a long session cut
        off mid-run by a quota) is evidence the worker IS healthy despite
        being attributed, and still resets the streak like any other real
        dispatch; attribution alone must never become a shield for an
        unrelated ladder. This holds per ROW, not per window: a window that
        mixes an unattributed dead-blind row with an attributed row that did
        real work is exactly as healthy as a window with only the real-work
        row in it — the dead-blind row didn't do anything either way, and a
        streak can never be advanced by evidence that is contradicted by
        something else in the very same window. So the check is ANY row
        (attributed or not) did real work resets, never "ALL rows must be
        dead to avoid resetting" — the latter would let a real-work row
        sitting beside an unrelated dead-blind row be outvoted.

        🔴 THE LADDER'S ONLY "STAYS FLAT" STATE IS BOUNDED BY DISPATCH, NOT BY
        TIME: an attributed-only window never leaves the loop idle — `retry`
        makes `_resume` dispatch immediately, so every tick that carries the
        ladder forward also produces a fresh attempt. That attempt is either
        a new dead row (streak advances toward `park`), real work (streak
        resets to 0), or another attributed wall (carries again). No
        attributed-only carry leaves the streak unresolved without an attempt
        being made — a `wake_backoff` window does, but that window is bounded
        by its own timer (`wake_poll_interval * 2**(streak-1)`, capped at
        `DEAD_RESUME_BACKOFF_CAP`), not by this ladder. So a long attributed
        run is not the loop stalling — it is the loop working, one fresh
        attempt per tick, against an environment that keeps walling. That is
        why no separate time-based expiry is added.

        Do NOT justify this with `max_park`: it does not bound this run. Two
        earlier drafts of this paragraph said things about it that execution
        refuted, so state only what is true, and only as far as it is true:
        **the timeout is reached only by a tick that DECLINES to resume**, and
        a ladder carried by `retry` resumes on every tick, so it never gets
        there however old the blocker is. ("Carried" means the `retry` verdict
        specifically. Inside a `wake_backoff` window the ladder is preserved
        too and the tick does not resume — and that tick CAN reach the
        timeout, exactly as the rule above says a non-resuming tick may: for
        a `quota_refreshed` park it does, because arming the backoff pushes
        `wake_check_at`, which is the very thing that condition tests, so the
        window unsatisfies the condition and control falls through to
        `max_park`. Measured. Do not read this paragraph as covering that
        state.)

        Nothing stronger. In particular NOT "a satisfied condition always
        returns before the timeout" — that was the second wrong draft. The
        `pr_comment_on` rung above deliberately does the opposite: when the
        injection delivers nothing it does not resume and FALLS THROUGH to the
        `max_park` check (its own comment says so), which is a satisfied
        condition reaching the timeout. The predicate is resume-or-not, never
        satisfied-or-not.

        (A second, independent reason `max_park` cannot bound a carried
        ladder — NOT exercised by the fixture below, so do not cite the
        fixture for it: the real quota park rebuilds the blocker dict with a
        fresh `raised_at` every time it parks, so the 48h measures one
        continuous park, never a park→resume→park run.)

        `test_blockers.py` pins that OUTCOME, and its control, in one fixture named
        `..._long_attributed_run_is_bounded_by_dispatch_not_time`: 60
        simulated days of walls never escalate and every tick dispatches, and
        then the SAME `raised_at` — 61 days old by then, and already past the
        48h `max_park` from day 2 onward — escalates immediately once its
        condition stops being satisfied. Only the resume-and-return kept it
        alive. The bound is dispatch, not the calendar.

        Fails OPEN on any error (DB blip, malformed state): a healthy task
        must never be backed off or parked by a bug in this bookkeeping.
        """
        try:
            ctx = task.context or {}
            if (ctx.get("resume_from") or {}).get("by") != "wake":
                # No prior resume, or the last one was a human's — the human
                # reset case (Intake Q3: with no human action ever, there is
                # no `resume_from` either, so a fresh task also proceeds).
                return "proceed", 0, [], None
            state = ctx.get("wake_dead_resumes") or {}
            prior_ids = list(state.get("attempt_ids") or [])
            last_resume_at = _parse_iso(state.get("last_resume_at"))
            if last_resume_at is None:
                # Nothing to judge against yet — fail open rather than guess.
                return "proceed", 0, [], None
            rows = await self.store.list_attempts(task.id)
            # `started_at` is SQLite `datetime('now')` — second-resolution, no
            # fractional part (db.py). `last_resume_at` is `now_iso()` —
            # microsecond-resolution. A dispatch's own attempt row routinely
            # lands in the SAME wall-clock second as the resume that created
            # it (a dead attempt fails almost immediately, by definition), and
            # truncation can put its zeroed-microsecond `started_at` BEFORE a
            # `last_resume_at` stamped later within that same second — which
            # would wrongly drop the very row this comparison exists to catch.
            # Floor `last_resume_at` to the second, matching `started_at`'s own
            # resolution, so a same-second attempt is never spuriously excluded.
            last_resume_floor = last_resume_at.replace(microsecond=0)
            relevant = [
                row for row in rows
                if (started := _parse_iso(row.get("started_at"))) is not None
                and started >= last_resume_floor
            ]
            # An attempt the loop already ATTRIBUTED — a quota wall, a dead
            # SDK session, any `infra_failure = 1` row — is not death-blind
            # evidence: its cause is known, its park carries a wake, and the
            # resume that follows is the loop working. INCIDENT (2026-08-21,
            # task f8efad06): 2c8f23ff, 0986460c, f8de9cdf and e037008e each
            # had exactly ONE such row (the weekly/session wall) and were
            # escalated "after 3 consecutive dead machine resumes" anyway.
            #
            # An attributed-only window is NOT the same case as an EMPTY
            # window and the two must not collapse into the same verdict.
            # `_resume` re-anchors `last_resume_at` at every re-dispatch (the
            # retry rung's own fix), so a previously-counted dead row falls
            # out of scope the instant the loop re-dispatches into it. If the
            # very next window then contains nothing but an attributed wall
            # — no new dead attempt, but also no healthy one — returning
            # `proceed` here would LAUNDER the ladder away: an environment
            # alternating a death-blind dead dispatch with an attributed wall
            # would never accumulate a streak, and the breaker — whose whole
            # purpose is to stop blindly repeating a dead pattern — would be
            # inert exactly where it is needed most (2026-08-21 was walled
            # all day). A wall is evidence the ENVIRONMENT blocked, not
            # evidence the WORKER is healthy, so it is NEUTRAL BY DEFAULT: it
            # carries any existing ladder forward (`retry`) instead of
            # resetting it. But "attributed" only means the loop knows WHY
            # the attempt ended — it says nothing about whether the attempt
            # did work before it hit that wall. A wall that lands AFTER real
            # priced work (a long session that then hits a mid-run quota
            # cutoff) is still evidence the worker is healthy, so it must
            # still reset like any other real dispatch — attribution alone
            # must never become a shield for an unrelated backoff/park
            # ladder. That check is over EVERY row in the window, attributed
            # or not — a window can never be "outvoted" into counting as
            # dead evidence by an unrelated dead-blind row sitting beside a
            # row that did real work. Only a truly empty window (no rows at
            # all since the resume — a dispatch gap, not evidence of
            # anything) or a window containing ANY row — attributed or not —
            # that did real priced work still resets the streak to 0.
            attributed = [row for row in relevant if row.get("infra_failure")]
            relevant = [row for row in relevant if not row.get("infra_failure")]
            usage_cols = self.store._usage_columns()

            def _is_dead(row: dict) -> bool:
                priced = sum(int(row.get(c) or 0) for c in usage_cols)
                turns = int(row.get("turns_used") or 0)
                return priced == 0 and turns <= 1

            # Real work ANYWHERE in the window — attributed or not — resets
            # the streak. Checked across BOTH lists before branching on
            # `relevant`'s emptiness: an unattributed dead-blind row must
            # never suppress the reset a real-work attributed row (a wall
            # landing after genuine work) earns on its own, and a real-work
            # unattributed row must never be shadowed by an unrelated
            # attributed wall in the same window either.
            if any(not _is_dead(row) for row in relevant) or any(
                not _is_dead(row) for row in attributed
            ):
                return "proceed", 0, [], None

            if not relevant:
                if attributed and prior_ids:
                    # The window had rows, but every one was attributed AND
                    # dead-shaped (0 priced tokens, <=1 turn, checked above)
                    # — an ordinary quota/session wall with no work behind
                    # it — and there is an existing ladder to protect: carry
                    # it forward as `retry` (see the comment above) instead
                    # of resetting it — `_resume` will re-dispatch on
                    # `retry`, which re-anchors the window without
                    # laundering streak or attempt_ids.
                    return "retry", len(prior_ids), prior_ids, "attributed_wall"
                # Either no rows at all since the resume (a dispatch gap —
                # the attempt row simply hasn't been written yet, or this is
                # a human/no-anchor resume) or an attributed-only window with
                # no prior ladder to protect — nothing to carry, so this
                # behaves like an ordinary proceed.
                return "proceed", 0, [], None
            dead_ids = sorted({str(row["id"]) for row in relevant})
            new_ids = [i for i in dead_ids if i not in prior_ids]
            if not new_ids:
                # The backoff window expired, but every dead row on the table
                # is one already counted toward `prior_ids` — nothing new was
                # TRIED since the last evaluation (no dispatch happened in
                # between: streak 1/2 verdicts never resume). Dispatch now
                # instead of re-counting the same row as a second death;
                # `streak`/`prior_ids` are carried unchanged, structurally
                # `len(prior_ids)` — the invariant `_park_dead_resumes`'s
                # message text relies on.
                return "retry", len(prior_ids), prior_ids, "expired_backoff"
            merged_ids = prior_ids + new_ids
            streak = len(merged_ids)
            verdict = "park" if streak >= self.DEAD_RESUME_PARK_STREAK else "backoff"
            return verdict, streak, merged_ids, None
        except Exception:  # noqa: BLE001 — fail open, never park on a bug
            log.warning(
                "dead-resume verdict failed for %s", task.id[:8], exc_info=True)
            return "proceed", 0, [], None

    async def _backoff_dead_resume(
        self, task: Task, streak: int, dead_ids: list[str], *,
        anchor: str | None, now: datetime,
    ) -> None:
        """Defer instead of resuming: push wake_check_at out by a doubling
        interval (base = the watcher's own poll cadence), capped at 6h."""
        delay = min(
            self.wake_poll_interval * (2 ** (streak - 1)),
            self.DEAD_RESUME_BACKOFF_CAP,
        )
        wake_at = now + delay
        task.wake_check_at = wake_at.isoformat()
        await self.store.update_task_columns(task)
        task.context = await self.store.merge_context(task.id, {
            "wake_dead_resumes": {
                "streak": streak,
                "attempt_ids": dead_ids,
                "last_resume_at": anchor,
                "backoff_until": wake_at.isoformat(),
            },
        })
        ids_str = ", ".join(dead_ids) if dead_ids else "(none)"
        await self._emit(
            task, "wake_backoff",
            f"{task.id[:8]} dead-resume streak #{streak}/"
            f"{self.DEAD_RESUME_PARK_STREAK} — the last machine-resumed "
            f"attempt died before doing work (0 priced tokens, <=1 turn); "
            f"deferring {delay} instead of resuming (dead attempts: {ids_str})",
        )

    async def _park_dead_resumes(
        self, task: Task, streak: int, dead_ids: list[str],
    ) -> None:
        """Stop resuming: park with an honest blocker naming the dead-resume
        pattern and the dead attempt ids, instead of resuming a 4th time."""
        # `streak == len(dead_ids)` is an invariant maintained by
        # _dead_resume_verdict (streak is derived as len(merged_ids), never
        # incremented independently) — the "{streak} consecutive dead machine
        # resumes (attempts: …)" text below is true by construction, not by
        # coincidence.
        # Load-bearing terminal guard (SCRUM-68) — see _resume.
        if await self._is_terminal(task):
            return
        ids_str = ", ".join(dead_ids) if dead_ids else "(none recorded)"
        data = dict(task.blocker or {})
        data["category"] = "NOVEL_UNKNOWN"
        data["question"] = (
            f"The wake watcher would be machine-resuming this task for the "
            f"{streak}th consecutive time since the last human action, but "
            f"every machine-resumed attempt in that window died before "
            f"doing work (0 priced tokens, <=1 turn) — environment/worker "
            f"failure, not task failure. Dead attempts: {ids_str}. Fix the "
            f"worker/environment, then resume, or cancel."
        )
        data["root_cause_hypothesis"] = (
            f"{streak} consecutive machine resumes since the last human "
            f"action died before doing any work (0 priced tokens, <=1 turn "
            f"each) — worker/environment failure, not a task failure"
        )
        data["dead_resume_streak"] = streak
        data["dead_resume_attempt_ids"] = dead_ids
        # Leave blocker["wake_condition"] in place: never silently drop it —
        # the human who fixes the environment must still be able to resume
        # into it, not lose it because this park cleared the whole blocker.
        task.blocker = data
        task.wake_check_at = None
        await self.store.update_task_columns(task)
        await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
        await self._emit(
            task, "escalated_dead_resumes",
            f"{task.id[:8]} parked after {streak} consecutive dead machine "
            f"resumes (attempts: {ids_str})",
        )

    async def _inject_pr_feedback(self, task: Task, condition: str) -> int | None:
        """Fetch PR comments and thread them into send_back_feedback.

        Returns the task's running revision-round count after this batch (so the
        caller can enforce the cap), or None if there were no new comments.
        """
        pr_ref = condition.split(":", 1)[1].strip()
        comments = await self._human_pr_comments(pr_ref)
        if not comments:
            return None
        # The comment fetch above is a network await — a POST /shipped landing
        # during it must not have its own merge-notice comment injected as
        # feedback into a finished task (the SCRUM-68 incident, one await
        # deeper than the rung's own recheck).
        if await self._is_terminal(task):
            return None
        rounds = await self._append_comments_as_feedback(
            task, comments, pr_ref=pr_ref)
        if not (task.context or {}).get("pr_comment_ref"):
            task.context = await self.store.merge_context(
                task.id, {"pr_comment_ref": pr_ref})
        await self._emit(task, "pr_feedback", f"{task.id[:8]} got {len(comments)} PR comment(s)")
        return rounds

    async def _emit(self, task: Task, kind: str, text: str, *,
                     extra: dict | None = None) -> None:
        """Persist a watcher action as a task event and mirror it to the host.

        Persistence is unconditional: the board and the DB record must show
        what the watcher did even when the host wires no callback — the server
        ran with a silent watcher for exactly that reason. `extra`, when
        truthy, is merged into the persisted event dict (e.g. `error`) —
        `_on_event`'s 2-arg host callback signature is unchanged; the extra
        fields are for the persisted record only.
        """
        event = {"source": "watcher", "kind": kind, "text": text, "ts": time.time()}
        if extra:
            event.update(extra)
        try:
            await self.store.save_events(task.id, [event])
        except Exception:  # noqa: BLE001 — visibility must never break the action
            log.warning("failed to persist watcher event %r", kind, exc_info=True)
        self._on_event(kind, text)

    def _is_bot_author(self, author: str) -> bool:
        """Comments from bots (CI result tables, status dashboards) are not
        operator feedback and must never trigger a revision attempt."""
        a = (author or "").lower()
        return a.endswith("[bot]") or a in self.ignore_comment_authors

    async def _human_pr_comments(self, pr_ref: str) -> list:
        """Comments on *pr_ref* that are actual feedback: no bot chatter, and
        none of no_human's own marked output.

        The one place the two rungs that ask "is there feedback on this PR?"
        route through — the wake condition and the injection that follows it.
        Filtering at the FETCH is what keeps them from disagreeing: they used
        to, and the rung's unfiltered `len(comments) > 0` woke tasks on their
        own comments that the injection then discarded.

        A missing checker or a fetch error yields no feedback (never an
        exception): a forge blip must not crash the watcher, and "we could not
        look" is not "a human replied".

        (The `_check_approval_pr_comments` poll rung deliberately does NOT use
        this: it needs the unfiltered list to advance its `pr_comment_since`
        cursor past bot comments, or it re-reads them forever. It applies the
        same `_is_self_or_bot` predicate after that.)
        """
        if self._pr_comment is None:
            return []
        try:
            comments = await self._pr_comment(pr_ref)
        except Exception as exc:  # noqa: BLE001 — checker must never crash watcher
            log.warning("pr_comment checker failed for %s: %s", pr_ref, exc)
            return []
        return [c for c in comments if not self._is_self_or_bot(c)]

    def _is_self_or_bot(self, comment) -> bool:
        """A comment that must never trigger a revision: bot chatter OR
        no_human's own output. Author identity can't catch the latter — the
        product posts under the operator's gh login (the 2026-07-10 incident:
        the CI_GATE results comment resumed its own task) — so bodies carry
        AGENT_COMMENT_MARKER and are filtered here."""
        from ..vcs.pr_watcher import is_agent_comment
        return (self._is_bot_author(getattr(comment, "author", ""))
                or is_agent_comment(getattr(comment, "body", None)))

    async def _append_comments_as_feedback(
        self, task: Task, comments: list, *, pr_ref: str = "",
    ) -> int:
        """Append PR comments to send_back_feedback; bump revision_rounds.

        Each entry lands via an atomic list append (concurrent writers both
        survive); the rounds counter is read-then-merge (worst case under two
        watchers: an off-by-one round count, never lost feedback). Refreshes
        ``task.context`` from the store. Returns the new round count.

        Also records the newest comment as a pending send-back
        (``blockers.send_back``) — this is the ONE chokepoint both human
        PR-comment callers (``_inject_pr_feedback``,
        ``_check_approval_pr_comments``) route through, so it is the single
        place to mark "a human send-back is pending and has not yet started
        a round" for that path. Cleared at the next `attempt_start`, or
        named in the blocker if a loop-head gate refuses to start a round at
        all (`orchestrator._refuse_round`).
        """
        entries = []
        for c in comments:
            # Support both PrComment objects and plain dicts/strings.
            if hasattr(c, "body"):
                msg = c.body
                author = getattr(c, "author", "reviewer")
                path = getattr(c, "path", None)
                line = getattr(c, "line", None)
                diff_hunk = getattr(c, "diff_hunk", None)
                created = getattr(c, "created_at", "") or now_iso()
            else:
                msg = str(c)
                author = "reviewer"
                path = line = diff_hunk = None
                created = now_iso()
            if path:
                loc = f"{path}" + (f":{line}" if line else "")
                msg = f"[{loc}] {msg}"
            if diff_hunk:
                msg += f"\n\nContext:\n```\n{str(diff_hunk)[:500]}\n```"
            entries.append({
                "at": created, "message": msg, "author": author,
                "source": "pr_comment",
            })
        for entry in entries:
            await self.store.append_context_list(
                task.id, "send_back_feedback", entry)
        rounds = int((task.context or {}).get("revision_rounds", 0)) + 1
        task.context = await self.store.merge_context(
            task.id, {"revision_rounds": rounds})
        if entries:
            from .send_back import record_pending_send_back
            newest = entries[-1]
            await record_pending_send_back(
                self.store, task, source="pr_comment",
                message=newest["message"], actor=newest.get("author", ""),
                at=newest.get("at"), pr_ref=pr_ref)
        return rounds

    async def _complete_if_content_landed(
        self, task: Task, url: str, *, forge_state: str, action: str,
        situation: str, branch: str | None = None,
    ) -> str | None:
        """THE completion path for "this branch's content is already on base".

        ONE path, two callers here — the CLOSED rung (which has always had
        it, inline) and the CONFLICTING rung (which used to start a rebase
        round without ever asking) — plus a third caller in the scheduler
        (the resume/restart dispatch gate). Returns *action* once it has
        recorded the outcome and written DONE; ``_TICK_ABORTED`` if the task
        went terminal while the probe ran — on ANY answer the probe gave, see
        the guard in ``shipped.complete_if_content_landed``, and every caller
        must abandon the tick on it; ``None`` for every "no" — hook not
        wired, no branch or no base recorded, probe error, or content
        genuinely absent — all of which mean the caller keeps its existing
        behaviour unchanged.

        The question is CONTENT, not ancestry, and that is not a preference:
        this repo lands every PR as an identity-normalized LOCAL squash, so the
        landing commit has no lineage back to the branch and
        ``git merge-base --is-ancestor`` is False for every PR we ever merged.
        See ``default_branch_shipped``.

        WHAT A ``True`` HERE MEANS, precisely — it is stronger than "some of
        this shipped". ``default_branch_shipped`` merges the branch into the
        base tip (both directions) and demands the result be EXACTLY the tip's
        tree, i.e. the branch has nothing left to contribute. A PARTIALLY
        landed branch — half a rename, one of two files, a follow-up commit
        still outstanding — writes a different tree and reads False, so it can
        never complete here. That is pinned by test (the half-landed rename).

        A ``False`` is deliberately overloaded ("absent" and "could not run"
        collapse into it, per that function's contract), which is why it is
        only ever read as "keep going": the caller's existing path — escalate
        to a human, run the rebase round, or dispatch a fresh attempt — is
        the safe side of the ambiguity in every caller.

        REVIEW PRECONDITION (this class's callers only). Neither of this
        class's callers may complete a task whose review never passed, and
        neither needs its own check for that: both are reached only through
        ``_check_open_pr``, which ``_evaluate`` calls only for
        ``AWAITING_APPROVAL`` — the status a task reaches only after its
        review passed and its PR was opened. That, plus a recorded
        ``pr_branch``, is the whole precondition set of the CLOSED rung this
        was extracted from, and it is preserved exactly. Pinned by the test
        that a BLOCKED task with landed content is still never completed.

        LANDED-COMMIT ANCHORING (2026-08-12). ``pr_shipped`` may now return a
        commit SHA instead of a bare ``True`` — ``branch_landed_commit``'s
        contract, wired in by every host (``api/app.py``, ``cli/commands.py``)
        — recording exactly where the content landed rather than merely that
        it did. Two things follow, both required by the incident this fixes
        (two stacked squash trains sharing a file; the earlier one's task
        re-escalated the moment the later one landed, because the old
        tip-only check re-asked the question at a tip that had moved on):

        - A previously recorded ``ctx["landed_sha"]`` is checked FIRST, via
          ``commit_is_ancestor`` — no merge-tree at all — before the probe
          ever runs. Once a landing is known, re-confirming it costs one
          cheap ancestry check forever, not a full content scan every tick.
        - A fresh string result is written to ``ctx["landed_sha"]`` before
          the DONE status is written, so the anchor survives a restart and
          the next tick (or the next incident) does not have to re-derive it.
        ``bool(result)`` keeps every existing caller that injects a plain
        ``True``/``False`` fake working unchanged.

        The actual body (including the anchoring above) now lives in
        ``blockers.shipped.complete_if_content_landed`` — this method is a
        thin delegate so the scheduler's resume-dispatch gate shares the
        exact same check instead of a copy.
        """
        return await _complete_landed(
            self.store, task, url, pr_shipped=self._pr_shipped,
            is_terminal=self._is_terminal, on_event=self._on_event,
            forge_state=forge_state, action=action, situation=situation,
            branch=branch,
        )

    async def _comment_after_landing(
        self, task: Task, pr_ref: str, *, may_complete: bool,
    ) -> str | None:
        """Guard the two comment rungs against resuming a PR whose content
        already shipped (2026-08-12 incident: the operator closed a landed
        PR with an explanatory comment and the comment rung read it as
        revision feedback, resuming a coder onto already-merged work).

        Returns an action string when the caller must NOT resume; ``None``
        when the caller keeps its existing behaviour unchanged.

        - ``self._pr_state is None`` -> ``None`` (rung not wired).
        - The state read is best-effort: on an exception, or on anything
          other than a definite ``"CLOSED"`` (``""``/``"OPEN"``/``"MERGED"``),
          returns ``None``. This deliberately keeps ``default_pr_state``'s
          documented contract ("callers must treat unknown as no action,
          never as closed") — failing the OTHER way here (treating an
          unreadable state as closed) would permanently swallow a genuine
          operator revision request behind a forge blip.
        - Only on a definite ``CLOSED``:

          ``may_complete=True`` (the AWAITING_APPROVAL rung,
          ``_check_approval_pr_comments``): delegate to the existing
          ``_complete_if_content_landed``, which owns the
          ``default_branch_shipped`` probe (ledger-exclusions included), the
          post-await terminal recheck, ``observe_pr``, and the DONE write —
          nothing here re-implements any of it.
            * ``_TICK_ABORTED`` (the task was already terminal): still
              record the note, return ``None`` so the caller abandons the
              tick exactly like every other caller does.
            * a truthy action: record the note, return that action.
            * ``None`` (not shipped, or the probe could not run — that
              ``False`` is documented as overloaded): return ``None`` — a
              closed but UNSHIPPED PR comment still resumes. Regression pin.

          ``may_complete=False`` (the ``_evaluate`` / BLOCKED rung): never
          writes DONE — a BLOCKED task with landed content is still never
          auto-completed. Runs the shipped probe read-only
          (``self._pr_shipped``), guarded exactly like
          ``_complete_if_content_landed`` (no ``base_branch`` -> ``None``,
          never default to "main"; a probe exception -> not shipped). Once
          confirmed shipped for *pr_ref*, the confirmation is cached on the
          task (``comment_after_landing_confirmed``): a still-parked task
          does not re-probe the forge (``pr_state`` + ``pr_shipped``) or
          re-emit the event on every subsequent tick while the same
          operator comment sits there — this is the throttle the incident's
          original fix was missing: without it, ``condition_satisfied``
          re-satisfies on the SAME unresolved comment every tick forever
          (the ladder never transitions the task off BLOCKED here), so the
          confirmation must be cached rather than re-derived each time.
        """
        if not may_complete:
            ctx = task.context or {}
            if ctx.get("comment_after_landing_confirmed") == pr_ref:
                return "no_resume_comment_after_landing"
        if self._pr_state is None:
            return None
        try:
            state = (await self._pr_state(pr_ref)) or ""
        except Exception as exc:  # noqa: BLE001 — a poll error must not crash the watcher
            log.warning("pr_state checker failed for %s: %s", pr_ref, exc)
            return None
        if state != "CLOSED":
            return None

        if may_complete:
            landed = await self._complete_if_content_landed(
                task, pr_ref, forge_state="CLOSED",
                action="shipped_comment_after_landing",
                situation="PR closed with a new comment",
            )
            if landed == _TICK_ABORTED:
                await self._emit(
                    task, "comment_after_landing",
                    f"{task.id[:8]} got a comment on closed PR {pr_ref} "
                    "after the task had already finished",
                )
                return None
            if landed:
                await self._emit(
                    task, "comment_after_landing",
                    f"{task.id[:8]} got a comment on closed PR {pr_ref} "
                    "but its content already shipped — not resuming",
                )
                return landed
            return None

        # may_complete=False: never write DONE from here, even when the
        # content has landed — only record the fact and decline to resume.
        if self._pr_shipped is None:
            return None
        ctx = task.context or {}
        branch = ctx.get("pr_branch")
        base = ctx.get("base_branch")
        if not task.repo_path or not branch or not base:
            return None
        try:
            shipped = await self._pr_shipped(task.repo_path, branch, base)
        except Exception as exc:  # noqa: BLE001 — a probe error must not crash the watcher
            log.warning("pr_shipped check failed for %s: %s", task.id[:8], exc)
            return None
        if not shipped:
            return None
        task.context = await self.store.merge_context(
            task.id, {"comment_after_landing_confirmed": pr_ref})
        await self._emit(
            task, "comment_after_landing",
            f"{task.id[:8]} got a comment on closed PR {pr_ref} but its "
            "content already shipped — not resuming",
        )
        return "no_resume_comment_after_landing"

    #: Event kinds that answer the `pr_closed` rung's question on their own —
    #: a human explicitly repaired the escalation, or approved/merged the PR
    #: by hand. `merged`/`shipped*` are not included: those are the WATCHER's
    #: own completion writes, already handled by `_complete_if_content_landed`
    #: falling through with a `landed` action before this guard ever runs.
    #: `restore-approval`'s repair event moved to the shared `human_event`
    #: shape (`kind="human_restore_approval"`) so its write lands in the same
    #: transaction as the status change; `state_repaired` is retained because
    #: rows already written to live DBs before that change still carry it.
    _PR_CLOSED_ANSWER_KINDS = frozenset(
        {"state_repaired", "human_restore_approval", "human_merged"})

    async def _pr_closed_answered(self, task: Task, url: str) -> str | None:
        """Whether a human has already answered the `pr_closed` rung's
        question — "closed without merging: abandon, or rework?" — for this
        exact PR URL, so the rung must hold rather than re-escalate.

        THE LOOP THIS CLOSES (live 2026-08-12): `restore-approval` moves a
        spuriously-escalated task back to `awaiting_approval`, but
        `ESCALATED` was never in `_is_terminal`'s set, so the very next tick
        re-polls the same still-CLOSED PR, falls through the same content
        check, and re-escalates — the repair the human just made is
        overwritten within one poll interval, forever. A recorded repair or
        approval answers the rung's own question directly; re-asking it after
        that answer is the defect, not a stricter check.

        TWO WAYS TO BE ANSWERED, checked cheapest first:

        1. ``ctx["pr_closed_repaired_url"] == url`` — the fast path
           `restore-approval` stamps (`cli/commands.py`) so the guard never
           depends on scanning the event log or on log text alone.
        2. Failing that, the event log: the LATEST `pr_closed` event whose
           text names this URL is the question being asked; the LATEST of
           `_PR_CLOSED_ANSWER_KINDS` or a parsed `ctx["approved_at"]` is a
           candidate answer. Answered only when the answer is AT LEAST AS
           RECENT as the question (`repair_ts >= closed_ts`) — a repair that
           predates the very escalation it supposedly answers is not an
           answer to it, and a `pr_closed` event that was never asked
           (`closed_ts is None`) has nothing here to answer, so this returns
           unanswered and the caller's normal escalate-once path decides.
           Pinned by test: a repair recorded BEFORE the escalation must not
           suppress it — the guard is ORDERED, not blanket.

        Returns ``"pr_closed_repair_honored"`` once (throttled by
        `ctx["pr_closed_repair_honored"]`, the same cache-the-confirmation
        pattern `_comment_after_landing` uses) — never a fresh event per
        tick — or ``None`` when unanswered, in which case the caller's own
        escalate-once guard decides.
        """
        ctx = task.context or {}
        answered = ctx.get("pr_closed_repaired_url") == url
        if not answered:
            events = await self.store.list_events(task.id)
            closed_ts: float | None = None
            repair_ts: float | None = None
            for e in events:
                ts = e.get("ts")
                if ts is None:
                    continue
                kind = e.get("kind")
                if kind == "pr_closed" and url in (e.get("text") or ""):
                    if closed_ts is None or ts > closed_ts:
                        closed_ts = ts
                elif kind in self._PR_CLOSED_ANSWER_KINDS:
                    if repair_ts is None or ts > repair_ts:
                        repair_ts = ts
            approved_at = _parse_iso(ctx.get("approved_at"))
            if approved_at is not None:
                approved_ts = approved_at.timestamp()
                if repair_ts is None or approved_ts > repair_ts:
                    repair_ts = approved_ts
            answered = (
                closed_ts is not None and repair_ts is not None
                and repair_ts >= closed_ts
            )
        if not answered:
            return None
        if ctx.get("pr_closed_repair_honored") == url:
            return "pr_closed_repair_honored"
        await observe_pr(self.store, task.id, url, forge_state="CLOSED",
                         shipped=None)
        task.context = await self.store.merge_context(
            task.id, {"pr_closed_repair_honored": url})
        await self._emit(
            task, "pr_closed_repair_honored",
            f"{task.id[:8]} pr_closed rung held: a repair/approval is on "
            f"record for {url} — not re-escalating",
        )
        return "pr_closed_repair_honored"

    async def _check_open_pr(self, task: Task) -> str | None:
        """The awaiting-approval priority ladder, one rung per tick.

        1. **Merged** → DONE. The agent only ever *observes* merged-ness —
           the never-merge constraint is untouched. (Before this ladder, a
           merged PR left its task parked as awaiting_approval forever.)
        2. **Closed unmerged** → ESCALATED with a question, but escalates at
           most ONCE per PR URL, and never again once a repair or approval is
           on record for it (`_pr_closed_answered`) — `ESCALATED` is not a
           terminal status here, so without both guards a still-CLOSED PR
           re-fires this rung every tick, defeating a human's own repair.
        3. **New human comments** (existing B4 path) → injected into
           `send_back_feedback` FIRST, in inject-only mode (no resume of its
           own this tick) — see the rung-ordering comment below for why this
           runs ahead of rung 4 instead of swapping places with it outright.
        4. **Textual conflict with main** (SCRUM-41) → bounded rebase loop:
           CI stays green through a conflict (it only runs the PR's own
           branch), so this is the one rung that polls `mergeable` directly.
           UNKNOWN is GitHub still computing (notably right after the rebase
           push this rung itself asks for) — never acted on. Definite
           CONFLICTING sends the rebase instruction back, bounded like the
           CI-fix rounds; past the cap the conflict is handed to the human.
           Live occurrence: PR #26 conflicted with #25 and sat invisible
           until a human tried to merge it. This rung (or the fallback below,
           if it does not act) issues the tick's single resume, carrying any
           comments rung 3 injected along with its own findings.
        5. **Red CI on the PR head** → bounded fix loop: fetch the failing
           check's log, feed it back, resume onto the PR branch. Rounds are
           counted per distinct failure *signature* — a re-run of the same red
           check never burns a round — and past the cap the specific failing
           check is handed to the human. This is the gap a real run exposed:
           the CI pipeline definition failed to compile on the server while
           every local check passed, and nothing was watching.
        """
        pr = await resolve_task_pr(self.store, task)
        url = pr.url
        if not url:
            return None
        if pr.source != "pr_watch":
            log.info("task %s: PR resolved from %s: %s",
                     task.id[:8], pr.source, url)

        state = ""
        if self._pr_state is not None:
            try:
                state = (await self._pr_state(url)) or ""
            except Exception as exc:  # noqa: BLE001 — a poll error must not crash the watcher
                log.warning("failed to poll PR state for %s: %s", task.id[:8], exc)
        # The poll above just awaited a network call; re-verify terminal-ness
        # before acting on MERGED/CLOSED (state rung, SCRUM-68) — a concurrent
        # POST /cancel landing mid-poll must not still write DONE/ESCALATED.
        if await self._is_terminal(task):
            return None
        if state == "MERGED":
            await observe_pr(self.store, task.id, url, forge_state=state)
            merged_text = f"{task.id[:8]} PR merged by a human: {url}"
            await self.store.set_status(
                task, TaskStatus.DONE, validate=False,
                event={"source": "watcher", "kind": "merged", "text": merged_text,
                       "ts": time.time()},
            )
            self._on_event("merged", merged_text)
            return "merged"
        if state == "CLOSED":
            # GitHub's merged flag is never true for our PRs: the operator's
            # hard rule is a LOCAL, identity-normalized squash merge (never
            # `gh pr merge`), so a squash commit lands on base with a fresh
            # SHA that has no commit-graph lineage back to the branch — every
            # shipped PR still reports CLOSED here. Trusting that flag alone
            # escalated every successful task (SCRUM-68 follow-up). Before
            # escalating, ask git (not GitHub) whether the branch's content is
            # actually present on its base — that's true regardless of how
            # the commit graph got there.
            # FOR THE PR-OUTCOME RECORD ONLY — the escalation behaviour below is
            # deliberately unchanged.
            #
            # The `shipped` this rung RECORDS is True or None, NEVER False,
            # and that is not an oversight (the helper records the True itself;
            # the fall-through below records the None).
            # `default_branch_shipped` is documented to return
            # False for BOTH "the content is not on base" and "the check could
            # not run" (missing repo, deleted branch, unrelated histories) —
            # "callers must treat False as 'can't tell'". That collapse is
            # correct for the escalation decision, which only needs to never
            # see a false "shipped" and has a human on the other end of it.
            #
            # It is wrong for a RECORD. `closed_unmerged` is a SETTLED outcome,
            # so it is never re-polled; writing one from an ambiguous False
            # would permanently file a PR as "closed without merging" on the
            # strength of a git command that failed. The likeliest cause of
            # that failure is the branch being gone — which is what happens
            # AFTER a successful squash merge, and after the task's temporary
            # worktree is cleaned up. The mistake would therefore land hardest
            # on exactly the PRs that did merge, i.e. it would invert the
            # number this table exists to produce.
            #
            # So the watcher records only what it is certain of. A False leaves
            # the row `unknown`, which is UNSETTLED, so `nh pr-outcomes refresh`
            # re-polls it later with a probe that can tell the two cases apart
            # (`pr_outcome.probe_shipped`). Nothing is lost, and nothing is
            # asserted that was not observed.
            landed = await self._complete_if_content_landed(
                task, url, forge_state=state, action="shipped_pr_closed",
                situation="PR closed", branch=pr.branch)
            if landed == _TICK_ABORTED:
                return None
            if landed:
                return landed
            # Reaching here is exactly the `True`-or-`None` rule above: the
            # helper writes DONE (and returns an action) on its ONLY `True`,
            # so every path that falls through is one of the ambiguous cases —
            # no probe wired, no branch recorded, the probe raised, or it said
            # False, which is itself "absent OR could not run". None of those
            # is evidence of absence, so the RECORD gets `None` and stays
            # `unknown` (unsettled, re-polled) while the ESCALATION below —
            # deliberately unchanged — proceeds and puts a human on it.
            #
            # REPAIR-IS-TERMINAL (2026-08-12). A human who restored this task
            # to AWAITING_APPROVAL has already answered the question this rung
            # asks ("closed without merging: abandon, or rework?"); `ESCALATED`
            # is not in `_is_terminal`'s set, so with no memory of that answer
            # the very next tick re-polls the same still-CLOSED PR and
            # re-escalates — the repair-defeating loop the 2026-08-12 incident
            # was. `_pr_closed_answered` is that memory and must run before
            # any write below.
            answered = await self._pr_closed_answered(task, url)
            if answered:
                return answered
            ctx = task.context or {}
            if ctx.get("pr_closed_escalated_url") == url:
                # ESCALATE-ONCE. Same non-terminal-ESCALATED gap as above, the
                # other side of it: an unrepaired escalation must not grow a
                # fresh `pr_closed` event and outcome write on every later
                # tick just because the task is still parked there. The
                # context flag alone could theoretically survive a row this
                # task never actually escalated on (hand-edited context); the
                # event-log check keeps the guard honest rather than trusting
                # a single signal.
                events = await self.store.list_events(task.id)
                if any(e.get("kind") == "pr_closed" and url in (e.get("text") or "")
                       for e in events):
                    return None
            await observe_pr(self.store, task.id, url, forge_state=state,
                             shipped=None)
            data = task.blocker or {}
            data["category"] = "AMBIGUITY"
            data["question"] = (
                "The PR was closed without merging. Abandon the task, or rework "
                "and reopen?"
            )
            data["root_cause_hypothesis"] = f"PR closed unmerged: {url}"
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            task.context = await self.store.merge_context(
                task.id, {"pr_closed_escalated_url": url})
            await self._emit(task, "pr_closed", f"{task.id[:8]} PR closed unmerged: {url}")
            return "escalated_pr_closed"

        # Neither merged nor closed: the PR is OPEN, or its state could not be
        # read at all. Record that too — an `open` that is genuinely open and an
        # `unknown` the poll could not resolve are different facts, and the
        # whole point of this table is that the second one never counts as the
        # first. `checks=None`: this rung did not fetch CI, so whatever a
        # previous refresh measured stays.
        await observe_pr(self.store, task.id, url, forge_state=state)

        # RUNG PRECEDENCE (bugfix: a human comment was skipped for the whole
        # tick whenever the conflict rung acted first — task 1e5583dc / PR
        # #593, measured 2026-08-21: the human's 3.5k-char findings landed at
        # 20:47:10Z, the conflict rung resumed at 20:47:41Z with ONLY its own
        # 208-char rebase notice in send_back_feedback, and the comment rung
        # — the sole place that advances `pr_comment_since` — never ran).
        #
        # CHOSEN: run the comment rung FIRST but in INJECT-ONLY mode
        # (resume=False), then let the conflict rung's own resume (or the
        # fallback resume just below it, when the conflict rung does not act
        # at all) carry BOTH payloads in one coder round. This is preferred
        # over a bare order swap, which would make the conflict rung lose its
        # precedence outright: the conflict rung is the only one that can
        # terminate the tick correctly (mechanical derived-artefact
        # resolution, shipped-first completion, the round bound and its
        # escalation), and a comment-triggered resume ahead of it would open
        # a coder round against a PR that still cannot merge — silently
        # skipping that whole ladder for a tick.
        #
        # WHAT THE COMMENT RUNG GIVES UP: it no longer owns its own resume
        # once a conflict is live — whether (and how) the tick resumes is
        # decided below, so the coder sees findings + rebase notice in ONE
        # attempt instead of two, burning one fewer round than today.
        # WHAT THE CONFLICT RUNG GIVES UP: nothing about its own decisions —
        # only that its send_back_feedback entry is no longer guaranteed to
        # be the tick's only one.
        pending = await self._check_approval_pr_comments(task, pr=pr, resume=False)
        if pending is not None and pending != _COMMENTS_INJECTED:
            return pending
        injected = pending == _COMMENTS_INJECTED

        acted = await self._check_pr_conflict(task, url, state, branch=pr.branch)
        if acted:
            if injected and acted != "resumed":
                # The conflict rung ended the tick WITHOUT resuming (it took
                # a terminal or backoff path of its own — escalation,
                # mechanical resolution, shipped-first completion, dead-
                # resume backoff/park), so the findings injected above were
                # not carried into any coder round this tick. Whether they
                # are still reachable later depends on what the conflict rung
                # just did to the task, not on which string it returned: a
                # DONE task (ship-first completion) will never resume again,
                # so its context — and the findings sitting in it — are gone
                # for good, while every OTHER outcome here leaves the task
                # non-DONE, so the same context (and cursor) is still there
                # for whatever resumes it next.
                if task.status is TaskStatus.DONE:
                    defer_text = (
                        f"{task.id[:8]} human PR findings were injected into "
                        f"send_back_feedback, but the PR shipped before any "
                        f"coder round consumed them (outcome {acted!r}); the "
                        f"task is DONE and will never resume again, so the "
                        f"findings are unconsumed for good — a human should "
                        f"review them directly on the PR"
                    )
                else:
                    defer_text = (
                        f"{task.id[:8]} human PR findings were injected into "
                        f"send_back_feedback but no coder round consumed "
                        f"them this tick: the conflict rung ended the tick "
                        f"with {acted!r} — the findings stay queued for the "
                        f"next resume"
                    )
                await self._emit(task, "pr_feedback_deferred", defer_text)
            return acted
        if injected:
            # No conflict this tick (MERGEABLE/UNKNOWN): the conflict rung
            # did not act at all, so the comment rung's own resume — deferred
            # above — happens here instead.
            return await self._resume(task)

        acted = await self._check_pr_ci(task, url)
        if acted:
            return acted
        # 6. CI_GATE integration gate (M6): PR CI is green (or unknown, which
        #    the gate re-checks explicitly) — run the integration validation
        #    once per PR head, bounded send-back on failure.
        return await self._check_ci_gate_integration(task, url)

    async def _check_pr_conflict(self, task: Task, url: str,
                                 forge_state: str = "", *,
                                 branch: str | None = None) -> str | None:
        """Rung 3 (SCRUM-41): a textual conflict with main is invisible to CI
        (branch checks only run the PR's own branch) — this rung is the only
        one that polls `gh pr view --json mergeable,mergeStateStatus` directly.

        GitHub computes ``mergeable`` asynchronously after every push,
        including the rebase push this rung itself asks for — so "UNKNOWN" is
        the normal state for a few seconds after every round, not a real
        signal. It must never be treated as resolved (would leave a real
        conflict unhandled) NOR reset the round counter (would let a
        CONFLICTING → UNKNOWN → CONFLICTING cycle — the normal shape of a
        rebase-and-repoll — reset every round and never reach the bound). The
        counter only resets on a *definite* MERGEABLE: that is a genuinely new
        failure cycle, not a continuation.

        SHIPPED-FIRST (2026-08-11). Before starting OR continuing a round, ask
        git whether the branch's content is already on the base; if it is, the
        round has nothing to do and the task completes through the shared
        ``_complete_if_content_landed`` path instead. Measured live twice that
        day: a round is an ENTIRE coder attempt (session + tests + review +
        delivery — millions of tokens, ~1h wall), and task 5ef97879's attempt 9
        was moot at birth because PR #183's content was mid-landing through a
        supervised local squash. It had to be paused by hand.

        WHY THE TWO SIGNALS CAN HONESTLY DISAGREE, since "CONFLICTING yet
        already contained" reads like a contradiction: they are computed at
        different TIMES against different BASE TIPS. GitHub's ``mergeable`` is
        asynchronous and cached — it reports the verdict it last computed,
        against the base tip it last saw — while the content check runs now,
        against the tip in the local checkout (and its upstream, per
        ``_base_tips``). A squash landing pushed straight to ``origin/main``
        resolves the conflict without touching the PR, so the stale
        CONFLICTING survives it. That is precisely the live shape, and it is
        why ancestry cannot be used instead: the squash has no lineage back to
        the branch.

        TRUST THE LOCAL MERGE (2026-09-01). Measured live on four consecutive
        review-passed deliveries in one day (99e67f5e, a9c4f0f4, a5753a8a,
        11d5ff46): every landing rewrites RELEASE_MANIFEST.txt and
        EXPORT_CLASSIFICATION.txt, which flips GitHub's cached ``mergeable``
        to CONFLICTING for every OTHER open PR, while a fresh local
        three-way merge of the branch against a freshly fetched base finds no
        conflicting path at all. That used to defer, then escalate at a
        bound ("… 4 consecutive checks disagree. Advise, or take over?"),
        forcing a human to take over work that was already mergeable. The
        local check is authoritative here: it runs a real merge against refs
        fetched THIS tick, whereas the forge's verdict is async and cached
        against whatever base tip it last saw. A *definite* empty
        conflicting-path set (enumeration succeeded, found nothing, even
        after the fetch-and-retry above) can never be a real conflict a coder
        round could act on, so it is treated exactly like a definite
        MERGEABLE: reset the round bookkeeping and fall through with no
        escalation, ever, on this class. This cannot mask a genuine
        conflict — a *non-empty* or unresolvable (``None``) local result
        still opens a coder round or escalates below, unchanged — and the
        approve path (``land_task``) re-runs its own squash merge at land
        time and refuses any unmerged path outside the tolerated ledger
        files, so a wrong "clean" verdict here would still be caught before
        anything reaches ``main``.
        """
        if self._pr_mergeable is None:
            return None
        try:
            info = await self._pr_mergeable(url)
        except Exception as exc:  # noqa: BLE001 — a poll error must not crash the watcher
            log.warning("failed to poll PR mergeability for %s: %s", task.id[:8], exc)
            return None
        # The poll above just awaited a network call; re-verify terminal-ness
        # before writing anything (conflict rung, SCRUM-68).
        if await self._is_terminal(task):
            return None
        mergeable = str((info or {}).get("mergeable") or "").upper()

        if mergeable == "MERGEABLE":
            ctx = task.context or {}
            if ctx.get("pr_conflict_rounds") or ctx.get("pr_conflict_stale_flags"):
                task.context = await self.store.merge_context(
                    task.id, {"pr_conflict_rounds": 0,
                              "pr_conflict_stale_flags": 0})
            return None
        if mergeable != "CONFLICTING":
            # UNKNOWN, "", or anything else GitHub hasn't settled yet: no-op,
            # no state change — see the docstring above.
            return None

        # A definite CONFLICTING, and the ONLY place the content check is paid
        # for. Cost, stated because it is a local subprocess burst on a poll
        # loop: a handful of `git rev-parse` / `merge-tree` calls, gated behind
        # a verdict that (a) GitHub reports for a small minority of ticks —
        # every other tick returns above without touching git — and (b) is
        # about to authorise an entire coder attempt if it stands. Seconds of
        # local git against millions of tokens is not a trade that needs a
        # cache; a *negative* answer is paid at most once per round, since the
        # round that follows it changes the task's status and the rung does not
        # re-run until the coder is done.
        landed = await self._complete_if_content_landed(
            task, url, forge_state=forge_state, action="shipped_pr_conflict",
            situation="PR CONFLICTING (no rebase round needed)", branch=branch)
        if landed == _TICK_ABORTED:
            return None
        if landed:
            return landed
        # Inconclusive or negative — including every host that never wired the
        # checker — falls through to exactly the behaviour that shipped before
        # this guard existed.

        merge_state = str((info or {}).get("mergeStateStatus") or "").upper()
        ctx = task.context or {}
        rounds = int(ctx.get("pr_conflict_rounds") or 0) + 1
        task.context = await self.store.merge_context(
            task.id, {"pr_conflict_rounds": rounds})

        # SCRUM-?? (rebase-round-cannot-fix-a-generated-artefact bugfix):
        # enumerate exactly what is conflicting before opening a coder round.
        # A coder round can never fix RELEASE_MANIFEST.txt — it is
        # regenerated from the tree, not authored — so when EVERY conflicting
        # path is that file (see derived_conflict.DERIVED_ARTEFACTS), resolve
        # mechanically instead of burning an attempt. EXPORT_CLASSIFICATION.txt
        # is hand-maintained in general (its win-counts are not regenerated by
        # any command), so a conflict touching it still opens a coder round
        # below by default — UNLESS `mechanically_resolvable` proves every
        # conflicting hunk in it differs ONLY by a rule's win-count (both
        # sides independently bumping the same rule for files each
        # independently added): that shape is arithmetic, not a hand
        # decision, and gets repaired the same way a clean-merge count drift
        # already does (`approve_merge.reconcile_merge_count_drift`).
        # `tests/test_structural_budget.py`'s FROZEN_* ratchet entries get
        # the same treatment: a conflict confined to hunks that differ only
        # by the frozen numeric value (both sides growing the same frozen
        # function/file and honestly re-measuring it) is not a hand decision
        # either — the merged tree's OWN measurement is the correct value,
        # never either side's number (`derived_conflict.
        # budget_conflict_hunks_numeric_only` / `budget_conflict.measure`).
        # Import lazily (blockers -> vcs at call time, as shipped.py
        # documents) to keep this module's import graph unchanged.
        from ..vcs.derived_conflict import (
            BUDGET_TEST_PATH,
            CLASSIFICATION_NAME,
            DERIVED_ARTEFACTS,
            conflicting_paths,
            fetch_conflict_refs,
            mechanically_resolvable,
            resolve_base_tip,
            resolve_derived_conflict,
        )

        branch_name = branch or ctx.get("pr_branch")
        base_branch = ctx.get("base_branch")
        conflict_paths: set[str] | None = None
        # enumerate_error non-empty => enumeration is STILL unresolved after a
        # git-fetch retry (whether the underlying call raised or simply
        # returned None for an unresolvable ref) — the caller must escalate,
        # never open a coder round on an unknown. recovered_error non-empty
        # => the first attempt failed but the retry succeeded, so the round
        # proceeds normally with the reason recorded for visibility.
        enumerate_error = ""
        recovered_error = ""
        if task.repo_path and branch_name and base_branch:
            first_reason = ""
            try:
                conflict_paths = await conflicting_paths(
                    task.repo_path, base_branch, branch_name)
            except Exception as exc:  # noqa: BLE001 — a probe error must not crash the watcher
                first_reason = f"{exc.__class__.__name__}: {exc}"
                log.warning(
                    "failed to enumerate conflicting paths for %s: %s",
                    task.id[:8], exc)
                conflict_paths = None
            if conflict_paths is None:
                if not first_reason:
                    first_reason = (
                        "conflicting_paths() returned no result "
                        "(unresolvable ref?)")
                try:
                    fetched = await fetch_conflict_refs(
                        task.repo_path, base_branch, branch_name)
                except Exception as fexc:  # noqa: BLE001 — best-effort precondition
                    fetched = False
                    log.warning(
                        "ref fetch before the enumeration retry failed "
                        "for %s: %s", task.id[:8], fexc)
                retry_reason = ""
                try:
                    conflict_paths = await conflicting_paths(
                        task.repo_path, base_branch, branch_name)
                except Exception as exc2:  # noqa: BLE001
                    conflict_paths = None
                    retry_reason = f"{exc2.__class__.__name__}: {exc2}"
                else:
                    if conflict_paths is None:
                        retry_reason = (
                            "conflicting_paths() returned no result "
                            "(unresolvable ref?)")
                if conflict_paths is not None:
                    recovered_error = (
                        f"{first_reason} (recovered after git fetch, "
                        f"fetch_ok={fetched})")
                else:
                    enumerate_error = (
                        f"{first_reason}; retry after git fetch "
                        f"(fetch_ok={fetched}) also failed: {retry_reason}")

        # GitHub says CONFLICTING but the local three-way merge reports NO
        # conflicting path: a contradiction, not a conflict. The usual cause
        # is a stale side — the watcher's refs predate the push that made
        # the forge flip (every landing moves RELEASE_MANIFEST.txt under
        # every open PR), or the forge's asynchronous `mergeable` predates a
        # fix already pushed. Fetch, ask again; a *definite* empty result
        # (enumeration succeeded and found nothing, even after the fetch
        # retry) is trusted outright — see TRUST THE LOCAL MERGE in the
        # docstring above. This used to defer and then escalate at a bound
        # (task 855f1263: 2026-08-21; the 2026-09-01 measurement of 4/4
        # review-passed deliveries needing a human takeover in one day) — it
        # does neither anymore. A non-empty or still-unresolvable (``None``)
        # retry result is a real signal and falls through unchanged below.
        if (conflict_paths is not None and not conflict_paths
                and not enumerate_error):
            try:
                fetched = await fetch_conflict_refs(
                    task.repo_path, base_branch, branch_name)
            except Exception as fexc:  # noqa: BLE001 — best-effort precondition
                fetched = False
                log.warning(
                    "ref fetch before the empty-enumeration retry failed "
                    "for %s: %s", task.id[:8], fexc)
            try:
                again = await conflicting_paths(
                    task.repo_path, base_branch, branch_name)
            except Exception as exc3:  # noqa: BLE001
                again = None
                enumerate_error = (
                    "enumeration found no conflicting path although the forge "
                    f"reports CONFLICTING; retry after git fetch (fetch_ok="
                    f"{fetched}) failed: {exc3.__class__.__name__}: {exc3}")
            else:
                if again is None:
                    enumerate_error = (
                        "enumeration found no conflicting path although the "
                        "forge reports CONFLICTING; retry after git fetch "
                        f"(fetch_ok={fetched}) returned no result")
            if again:
                conflict_paths = again
                recovered_error = (
                    "first enumeration found no conflicting path; after git "
                    f"fetch (fetch_ok={fetched}) it names "
                    f"{len(again)} path(s)")
            elif again is not None:
                # A definite empty set after a fresh fetch: trust it. Give
                # back the round increment (nothing was dispatched) and zero
                # the stale-flags counter — there is no more bound to feed.
                # `pr_conflict_local_clean_checks` is observability-only: it
                # is incremented for visibility but never compared against
                # anything, so it can never itself become an escalation
                # trigger. Returning `None` here is the same value the
                # MERGEABLE branch returns above, so the tick falls through
                # to the comment-resume / CI / integration rungs exactly as
                # if the forge had reported MERGEABLE — the task stays
                # AWAITING_APPROVAL and reachable by `nh approve`.
                clean_checks = int(
                    ctx.get("pr_conflict_local_clean_checks") or 0) + 1
                task.context = await self.store.merge_context(
                    task.id, {"pr_conflict_stale_flags": 0,
                              "pr_conflict_rounds": rounds - 1,
                              "pr_conflict_local_clean_checks": clean_checks})
                await self._emit(
                    task, "pr_conflict_local_clean",
                    f"{task.id[:8]} PR {url} CONFLICTING per the forge "
                    f"(mergeStateStatus={merge_state or 'UNKNOWN'}) but a "
                    f"local merge after git fetch (fetch_ok={fetched}) finds "
                    "no conflicting path — trusting the local merge, no "
                    f"escalation, no coder round opened ({clean_checks} "
                    "check(s) so far)",
                )
                return None
        if conflict_paths and ctx.get("pr_conflict_stale_flags"):
            # A real conflict set ends the disagreement streak.
            task.context = await self.store.merge_context(
                task.id, {"pr_conflict_stale_flags": 0})

        if enumerate_error or recovered_error:
            task.context = await self.store.merge_context(
                task.id,
                {"pr_conflict_enumerate_error": enumerate_error or recovered_error})

        if enumerate_error:
            # A None return after the fetch retry is an UNKNOWN, not a
            # confirmed source conflict — it must never fall through to the
            # coder round below (the exact regression this gate closes).
            if await self._is_terminal(task):
                return None
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            data["question"] = (
                f"PR {url} is CONFLICTING but the conflicting paths could "
                f"not be enumerated even after fetching {base_branch} and "
                f"{branch_name}. Advise, or take over?"
            )
            data["root_cause_hypothesis"] = (
                f"conflicting-path enumeration failed: {url}"
            )
            data["evidence"] = enumerate_error
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(
                task, "escalated_pr_conflict",
                f"{task.id[:8]} PR {url} CONFLICTING — enumeration failed: "
                f"{enumerate_error}; no coder round opened",
                extra={"error": enumerate_error},
            )
            return "escalated_pr_conflict"

        conflict_desc = (
            ", ".join(sorted(conflict_paths))
            if conflict_paths else "could not enumerate"
        )

        base_tip_sha = None
        eligible = None
        if conflict_paths:
            base_tip_sha = await resolve_base_tip(task.repo_path, base_branch)
            if base_tip_sha:
                eligible = await mechanically_resolvable(
                    task.repo_path, conflict_paths, base_tip_sha, branch_name)
            elif conflict_paths <= DERIVED_ARTEFACTS | {CLASSIFICATION_NAME, BUDGET_TEST_PATH}:
                # The conflict has a mechanical SHAPE but the base tip could
                # not be resolved, so eligibility cannot be confirmed. A coder
                # cannot fix these files either — the honest outcome is the
                # same "could not resolve the base tip" escalation the
                # manifest-only path always had (review finding on PR #568:
                # this sub-case fell through to a paid coder round).
                eligible = DERIVED_ARTEFACTS | {CLASSIFICATION_NAME, BUDGET_TEST_PATH}

        if eligible:
            resolver = self._derived_resolver or resolve_derived_conflict
            result = None
            if base_tip_sha:
                result = await asyncio.to_thread(
                    resolver, task.repo_path, branch_name, base_tip_sha,
                    eligible=eligible)
            # A thread hop just ran; a terminal write from elsewhere may have
            # landed while we were off the loop (SCRUM-68 rule, every rung
            # here follows it).
            if await self._is_terminal(task):
                return None
            if result is not None and result.ok:
                unpinned_note = (
                    f"; unpinned (drop-classified): {', '.join(result.unpinned)}"
                    if result.unpinned else ""
                )
                reconciled = getattr(result, "reconciled", "")
                reconciled_note = (
                    f"; EXPORT_CLASSIFICATION.txt count reconciled by merge "
                    f"arithmetic: {reconciled}" if reconciled else ""
                )
                pruned = getattr(result, "pruned", None)
                pruned_note = (
                    f"; pruned stale pin(s): {', '.join(pruned)}" if pruned else ""
                )
                budget = getattr(result, "budget", "")
                budget_note = (
                    f"; structural budget re-anchored: {budget}" if budget else ""
                )
                await self._emit(
                    task, "pr_conflict_resolved",
                    f"{task.id[:8]} PR CONFLICTING — resolved mechanically "
                    f"(mechanically resolvable path(s) only: {conflict_desc}), "
                    f"pushed {result.pushed_sha[:8]}{unpinned_note}"
                    f"{reconciled_note}{pruned_note}{budget_note}",
                )
                return "resolved_pr_conflict"

            # Mechanical resolution was eligible (`base_tip_sha` resolved and
            # `mechanically_resolvable` confirmed the shape) but `result.ok`
            # is False — never fall through to a coder round here: a coder
            # can't fix these files either, and never push a tree that failed
            # `verify`. Escalate honestly (intake answer).
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            failing_step = result.step if result is not None else "worktree"
            detail = result.detail if result is not None else (
                "could not resolve the base tip to a commit")
            data["question"] = (
                f"PR {url} conflicts only in derived artefact(s) "
                f"({conflict_desc}) but mechanical resolution failed at "
                f"step {failing_step!r}. Advise, or take over?"
            )
            data["root_cause_hypothesis"] = (
                f"mechanical derived-artefact conflict resolution failed: "
                f"{url} step={failing_step}"
            )
            data["evidence"] = detail
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(
                task, "escalated_pr_conflict",
                f"{task.id[:8]} PR {url} CONFLICTING — mechanical "
                f"resolution failed at step {failing_step!r} "
                f"({conflict_desc})",
            )
            return "escalated_pr_conflict"

        if rounds > self.max_pr_conflict_rounds:
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            data["question"] = (
                f"PR {url} is still CONFLICTING with main after {rounds - 1} "
                f"autonomous rebase round(s) (mergeStateStatus="
                f"{merge_state or 'UNKNOWN'}). Advise, or take over?"
            )
            data["root_cause_hypothesis"] = (
                f"PR conflicts with main: {url} "
                f"(mergeable=CONFLICTING, mergeStateStatus={merge_state or 'UNKNOWN'})"
            )
            data["evidence"] = (
                f"gh pr view --json mergeable,mergeStateStatus -> "
                f"CONFLICTING / {merge_state or 'UNKNOWN'} on "
                f"{rounds - 1} consecutive detection(s) after send-back rounds"
            )
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(
                task, "escalated_pr_conflict",
                f"{task.id[:8]} PR {url} CONFLICTING past "
                f"{self.max_pr_conflict_rounds} rounds "
                f"(mergeStateStatus={merge_state or 'UNKNOWN'})",
            )
            return "escalated_pr_conflict"

        message = (
            "The PR has a textual conflict with main (mergeable=CONFLICTING"
            + (f", mergeStateStatus={merge_state}" if merge_state else "")
            + ").\nRebase onto origin/main, resolve conflicts, push — the PR "
              "updates itself."
            + f"\nConflicting paths: {conflict_desc}."
        )
        await self.store.append_context_list(task.id, "send_back_feedback", {
            "at": now_iso(), "message": message, "author": "pr_conflict",
            "source": "pr_conflict",
        })
        task.context = await self.store.merge_context(task.id, {})
        await self._emit(
            task, "pr_conflict",
            f"{task.id[:8]} PR CONFLICTING — rebase round "
            f"{rounds}/{self.max_pr_conflict_rounds} — "
            f"conflicting paths: {conflict_desc}",
            extra={"error": recovered_error} if recovered_error else None,
        )
        return await self._resume(task)

    async def _check_pr_ci(self, task: Task, url: str) -> str | None:
        """Rung 5: react to a red check on the open PR's head, bounded."""
        if self._pr_checks is None:
            return None
        try:
            checks = await self._pr_checks(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to poll PR checks for %s: %s", task.id[:8], exc)
            return None
        # The poll above just awaited a network call; re-verify terminal-ness
        # before writing anything (CI-rounds rung, SCRUM-68).
        if await self._is_terminal(task):
            return None
        failing = [c for c in checks if c.get("status") == "fail"]
        if not failing:
            return None
        # A distinct-failure signature. The link carries the build number, so
        # polling the same red build repeatedly while parked is free, but a NEW
        # build failing the same checks (the coder's fix didn't take) is a new
        # round. Names alone deadlocked here: after one fix push, the same
        # failing names read as "already handled" and the watcher went silent.
        signature = hashlib.sha256(
            "|".join(sorted(f"{c.get('name', '')}@{c.get('link', '')}" for c in failing)).encode()
        ).hexdigest()[:16]
        ctx = task.context or {}
        if ctx.get("pr_ci_last_sig") == signature:
            return None  # already acted on this exact run; wait for a new build
        if self.pr_ci_policy == "advisory":
            # Operator override (Actions quota exhausted): record the red so
            # the board can show it, but never count a round, resume, or
            # escalate. Throttled once per build via its OWN key —
            # pr_ci_last_sig must stay untouched, or a build recorded under
            # advisory would read as "already acted on" forever after the
            # operator flips back to enforce (re-run failed jobs keeps the
            # same link, so only a new push would clear it).
            if ctx.get("pr_ci_advisory_sig") == signature:
                return None
            task.context = await self.store.merge_context(
                task.id, {"pr_ci_advisory_sig": signature})
            await self._emit(
                task, "pr_ci_advisory",
                f"{task.id[:8]} PR CI red — pr_ci_policy=advisory (Actions "
                "quota exhausted): recorded, not acted on",
            )
            return None
        if ctx.get("pr_ci_infra_sig") == signature:
            # This exact build was already classified platform-layer INFRA:
            # polling it again while parked must be free (no log refetch, no
            # event row) — the same invariant the pr_ci_last_sig dedup states
            # above, kept on a separate key so infra classification never
            # suppresses a later REAL failure's round-counting.
            return None
        excerpt = ""
        if self._ci_log is not None and failing[0].get("link"):
            try:
                excerpt = await self._ci_log(failing[0]["link"])
            except Exception:  # noqa: BLE001 — the log is a bonus, not a dependency
                excerpt = ""
        annotation = ""
        if not excerpt and self._ci_annotations is not None:
            # A job blocked at START never produces a log at all (see
            # `_CI_INFRA_RE` above), so the annotation channel is only worth
            # trying when the log came back empty — a log that DID come back
            # already carries whatever text there is to classify.
            try:
                annotation = await self._ci_annotations(
                    url, failing[0].get("name", ""))
            except Exception:  # noqa: BLE001 — evidence is a bonus, not a dependency
                annotation = ""
        # Both fetches above are network awaits — re-verify terminal-ness
        # before ANY write in this rung (SCRUM-68; the round counter, the
        # escalation, and the resume below all mutate the task).
        if await self._is_terminal(task):
            return None
        if _CI_INFRA_RE.search(excerpt) or _CI_INFRA_RE.search(annotation):
            # The run failed at the platform layer (billing / runner
            # provisioning), so it says nothing about the code: no fix round,
            # no send-back, no escalation. pr_ci_last_sig stays unset so a
            # later healthy build is evaluated fresh; pr_ci_infra_sig (its own
            # key, checked above) makes re-polling this build free. An empty
            # log AND a missing/non-matching annotation falls through to the
            # real-failure path — infra must be positively identified on
            # EITHER channel, never assumed (fail closed).
            task.context = await self.store.merge_context(
                task.id, {"pr_ci_infra_sig": signature})
            await self._emit(
                task, "pr_ci_infra",
                f"{task.id[:8]} PR CI red is billing/provisioning INFRA — "
                "no fix round counted, not escalating",
            )
            return None
        names = ", ".join(c.get("name", "?") for c in failing)
        rounds = int(ctx.get("pr_ci_rounds") or 0) + 1
        task.context = await self.store.merge_context(
            task.id, {"pr_ci_rounds": rounds, "pr_ci_last_sig": signature})

        if rounds > self.max_ci_fix_rounds:
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            data["question"] = (
                f"CI on the PR is still red after {rounds - 1} autonomous fix "
                f"round(s). Failing: {names}. Advise, or take over?"
            )
            data["root_cause_hypothesis"] = f"PR CI failing: {names}"
            data["evidence"] = (excerpt or annotation or failing[0].get("link", ""))[:1500]
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(
                task, "escalated_ci",
                f"{task.id[:8]} PR CI red past {self.max_ci_fix_rounds} rounds: {names}",
            )
            return "escalated_ci"

        message = (
            f"The PR's CI is failing. Check(s): {names}.\n"
            f"Link: {failing[0].get('link', '')}\n"
            + (f"Log excerpt:\n```\n{excerpt}\n```\n" if excerpt
               else f"Annotation:\n```\n{annotation}\n```\n" if annotation else "")
            + "Fix the cause on the same branch; the push updates the PR and "
              "re-runs the checks."
        )
        await self.store.append_context_list(task.id, "send_back_feedback", {
            "at": now_iso(), "message": message, "author": "ci", "source": "pr_ci",
        })
        task.context = await self.store.merge_context(task.id, {})
        await self._emit(
            task, "pr_ci_red",
            f"{task.id[:8]} CI failing ({names}) — fix round {rounds}/{self.max_ci_fix_rounds}",
        )
        return await self._resume(task)

    async def _check_ci_gate_integration(self, task: Task, url: str) -> str | None:
        """Rung 6 (M6): run the CI_GATE integration validation post-PR, gated.

        The gate object owns eligibility, the once-per-head + in-flight +
        namespace duplicate guards, triggering, polling one status call per
        tick, and posting the PR results comment. This method owns what the
        verdict DOES to the task: pass → stays awaiting_approval (a human
        still merges); fail → bounded send-back to the coder, then escalate;
        refused (code PR needing a PR-built image) → honest escalation.
        """
        _outcome, action = await self._ci_gate_step(task, url)
        return action

    async def _ci_gate_step(self, task: Task, url: str) -> tuple[Any, str | None]:
        """One CI_GATE gate step + its task-level consequence. Returns
        (gate outcome | None, watcher action | None) — `nh ci_gate run` drives
        this directly so the manual path IS the watcher path."""
        if self._ci_gate_gate is None:
            return None, None
        try:
            outcome = await self._ci_gate_gate.step(task, url)
        except Exception as exc:  # noqa: BLE001 — the gate must never kill the watcher
            log.warning("CI_GATE gate step failed for %s: %s", task.id[:8], exc)
            return None, None
        # gate.step just triggered pipelines / posted PR comments over the
        # network; re-verify terminal-ness before acting on the outcome
        # (CI_GATE rung 6, SCRUM-68) — same race window as the other rungs.
        if await self._is_terminal(task):
            return None, None
        # The gate mutates task.context["ci_gate"] in memory (its state
        # machine) — persist that subtree atomically. RFC 7396: an empty dict
        # merges nothing, so a cleared state ({}) must become None (delete).
        state = (task.context or {}).get("ci_gate")
        task.context = await self.store.merge_context(
            task.id, {"ci_gate": state if state else None})

        if outcome.action == "skip":
            return outcome, None
        if outcome.action == "blocked":
            await self._emit(task, "ci_gate_blocked",
                             f"{task.id[:8]} CI_GATE: {outcome.reason}")
            return outcome, None
        if outcome.action == "triggered":
            await self._emit(task, "ci_gate_trigger",
                             f"{task.id[:8]} CI_GATE: {outcome.reason}")
            return outcome, "ci_gate_triggered"
        if outcome.action == "waiting":
            await self._emit(task, "ci_gate_poll",
                             f"{task.id[:8]} CI_GATE: {outcome.reason}")
            return outcome, None
        if outcome.action == "passed":
            await self._emit(
                task, "ci_gate_pass",
                f"{task.id[:8]} CI_GATE integration PASSED: {outcome.web_url}"
                + (" (PR comment posted)" if outcome.comment_posted else ""),
            )
            return outcome, "ci_gate_passed"
        if outcome.action == "refused":
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            data["question"] = (
                "CI_GATE validation is required but cannot run honestly: "
                f"{outcome.reason} Proceed without it, or wire the PR-image build?"
            )
            data["root_cause_hypothesis"] = outcome.reason
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(task, "ci_gate_refused",
                             f"{task.id[:8]} CI_GATE cannot run: {outcome.reason}")
            return outcome, "escalated_ci_gate_refused"

        # failed — bounded send-back, counted per pipeline run (a new run only
        # ever starts on a new PR head, so each failure is a distinct signature).
        names = ", ".join(outcome.failing_jobs) or "pipeline"
        rounds = int((task.context or {}).get("ci_gate_fix_rounds") or 0) + 1
        task.context = await self.store.merge_context(
            task.id, {"ci_gate_fix_rounds": rounds})
        if rounds > self.max_ci_gate_fix_rounds:
            data = task.blocker or {}
            data["category"] = "NOVEL_UNKNOWN"
            data["question"] = (
                f"CI_GATE integration still failing after {rounds - 1} autonomous "
                f"fix round(s). Failing: {names}. Advise, or take over?"
            )
            data["root_cause_hypothesis"] = f"CI_GATE integration failing: {names}"
            data["evidence"] = (outcome.log_excerpt or outcome.web_url)[:1500]
            task.blocker = data
            await self.store.update_task_columns(task)
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
            await self._emit(
                task, "ci_gate_fail",
                f"{task.id[:8]} CI_GATE red past {self.max_ci_gate_fix_rounds} "
                f"rounds: {names} — escalated",
            )
            return outcome, "escalated_ci_gate"

        message = (
            f"The CI_GATE integration validation failed. Job(s): {names}.\n"
            f"Pipeline: {outcome.web_url}\n"
            + (f"Log tail:\n```\n{outcome.log_excerpt}\n```\n"
               if outcome.log_excerpt else "")
            + "Fix the cause on the same branch; the push updates the PR and "
              "the validation re-runs on the new head."
        )
        await self.store.append_context_list(task.id, "send_back_feedback", {
            "at": now_iso(), "message": message, "author": "ci_gate",
            "source": "ci_gate",
        })
        task.context = await self.store.merge_context(task.id, {})
        await self._emit(
            task, "ci_gate_fail",
            f"{task.id[:8]} CI_GATE failing ({names}) — fix round "
            f"{rounds}/{self.max_ci_gate_fix_rounds}",
        )
        return outcome, await self._resume(task)

    async def _check_approval_pr_comments(
        self, task: Task, *, pr: Any = None, resume: bool = True,
    ) -> str | None:
        """Poll an awaiting-approval PR for NEW human comments (B4).

        Uses a per-task ``pr_comment_since`` cursor so the same comment never
        triggers a second revision. On new comments: inject them, advance the
        cursor, and either resume the task to revise or — past the revision cap —
        escalate to the human. Never times out.

        ``pr``, when given, is the caller's already-resolved
        ``task_pr.resolve_task_pr`` result (``_check_open_pr`` computes one
        per tick and passes it down so every rung agrees on one URL). When
        omitted — direct calls, including existing tests — this resolves its
        own, which degrades to the old ``ctx["pr_watch"]``-only behaviour for
        any task that has one.

        ``resume`` (keyword-only, default ``True`` — every existing direct
        call keeps today's behaviour byte-for-byte): when ``False``, a fresh
        human comment is still injected into ``send_back_feedback`` and the
        cursor still advances, but no resume is performed — the method
        returns ``_COMMENTS_INJECTED`` instead, and the CALLER
        (``_check_open_pr``) owns the tick's single resume. See the rung
        -ordering comment in ``_check_open_pr`` for why.
        """
        ctx = task.context or {}
        if pr is None:
            pr = await resolve_task_pr(self.store, task)
        url = pr.url
        if not url or self._pr_comment is None:
            return None
        try:
            comments = await self._pr_comment(url)
        except Exception as exc:  # noqa: BLE001 — a poll error must not crash the watcher
            log.warning("failed to poll PR comments for %s: %s", task.id[:8], exc)
            return None

        since = ctx.get("pr_comment_since")
        fresh = [c for c in comments
                 if not since or (getattr(c, "created_at", "") or "") > since]
        if not fresh:
            return None

        # Advance the cursor past everything we've now seen (newest wins).
        newest = max((getattr(c, "created_at", "") or "") for c in comments)
        human = [c for c in fresh if not self._is_self_or_bot(c)]

        # Closed+shipped guard runs BEFORE the terminal early-bail below: a
        # genuine operator comment on a PR whose content already landed is a
        # post-mortem note worth recording even when the task raced to DONE
        # (or was already DONE) before/during this poll — the guard's own
        # completion path (_complete_if_content_landed) re-checks
        # terminal-ness after its own probe and never double-writes, so
        # running it ahead of the check below is safe (SCRUM-68 invariant,
        # preserved one level deeper).
        if human:
            landed = await self._comment_after_landing(task, url, may_complete=True)
            if landed is not None:
                if newest:
                    task.context = await self.store.merge_context(
                        task.id, {"pr_comment_since": newest})
                return landed

        # The poll above (and the guard's own probes) just awaited network
        # calls; re-verify terminal-ness before writing anything further
        # (pr_feedback rung — the exact live incident, SCRUM-68: a done
        # task's PR got a post-merge comment and this rung counted it as new
        # human feedback and resumed the task).
        if await self._is_terminal(task):
            return None

        if not human:
            # Bot chatter only (CI result tables etc.): move the cursor so the
            # same comments are never reconsidered, but do not burn an attempt.
            if newest:
                task.context = await self.store.merge_context(
                    task.id, {"pr_comment_since": newest})
            await self._emit(
                task, "pr_feedback_skipped",
                f"{task.id[:8]} ignored {len(fresh)} bot comment(s) "
                f"({', '.join(sorted({getattr(c, 'author', '?') for c in fresh}))})",
            )
            return None
        rounds = await self._append_comments_as_feedback(task, human, pr_ref=url)
        if newest:
            task.context = await self.store.merge_context(
                task.id, {"pr_comment_since": newest})
        await self._emit(task, "pr_feedback", f"{task.id[:8]} got {len(human)} new PR comment(s)")

        if rounds > self.max_revision_rounds:
            # Precedence kept even in inject-only mode: the revision cap is
            # this rung's OWN terminal decision, so it escalates regardless of
            # `resume` — the caller must never resume a task this rung just
            # decided to hand to a human.
            await self._escalate_revisions(task, rounds)
            return "escalated_revisions"
        if not resume:
            # Inject-only mode (see the precedence comment in
            # _check_open_pr): the findings are in send_back_feedback and the
            # cursor has moved; the CALLER owns the single resume this tick.
            return _COMMENTS_INJECTED
        return await self._resume(task)

    async def _escalate_revisions(self, task: Task, rounds: int) -> None:
        """Stop the comment→revise loop after the cap and hand back to a human."""
        # Load-bearing terminal guard (SCRUM-68) — see _resume.
        if await self._is_terminal(task):
            return
        data = task.blocker or {}
        data["category"] = "AMBIGUITY"
        data["root_cause_hypothesis"] = (
            f"PR feedback revised {rounds} time(s), exceeding "
            f"max_revision_rounds={self.max_revision_rounds}; escalating so a "
            "human can decide rather than revising indefinitely."
        )
        task.blocker = data
        await self.store.update_task_columns(task)
        if task.status != TaskStatus.ESCALATED:
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
        await self._emit(
            task, "escalated_revisions",
            f"{task.id[:8]} exceeded {self.max_revision_rounds} PR-revision rounds",
        )

    async def _escalate_timeout(self, task: Task, blocker: Blocker | None) -> None:
        # Load-bearing terminal guard (SCRUM-68) — see _resume.
        if await self._is_terminal(task):
            return
        data = task.blocker or {}
        data["timed_out"] = True
        data["category"] = "NOVEL_UNKNOWN" if blocker is None else data.get("category")
        paused = blocker is not None and blocker.category is BlockerCategory.USER_PAUSED
        if paused:
            # Honest reason: this was a deliberate human pause, not a stuck
            # diagnosis — say who paused it and for how long, not the generic
            # "parked past max duration" a real blocker's timeout gets.
            data["root_cause_hypothesis"] = (
                f"paused by {data.get('paused_by') or 'a human'} and not "
                f"resumed within {self.max_park}; "
                + data.get("root_cause_hypothesis", "")
            ).strip()
        else:
            data["root_cause_hypothesis"] = (
                f"parked past max duration ({self.max_park}); "
                + data.get("root_cause_hypothesis", "")
            ).strip()
        task.blocker = data
        await self.store.update_task_columns(task)
        if task.status != TaskStatus.ESCALATED:
            await self.store.set_status(task, TaskStatus.ESCALATED, validate=False)
        await self._emit(task, "escalated_timeout", f"{task.id[:8]} parked past max duration")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
