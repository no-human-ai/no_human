"""Jira polling loop — mirrors the (removed) TRACKER poller's shape.

Driven from ``nh serve``'s tick alongside the scheduler (one event loop, no new
daemon — lean-stack constraint). Two best-effort halves:

  - **Poll:** run the operator's JQL, create a ``no_human`` task per NEW issue
    (deduped by ``(source="jira", external_id=<KEY>)``).
  - **Write-back (opt-in, SCRUM-21):** as a task advances, post a work-note
    comment to its issue AND move it into the workflow's matching status
    category — In Progress on first claim, Done on completion. Escalated/
    failed tasks are commented but never transitioned (state must never lie).
    The agent still never closes or merges (constraint #2).

A Jira transport error logs and is retried next tick; it never crashes the pool.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..core.db import Store
from ..core.task import Task, TaskStatus
from ..integrations.health import ensure_fresh_before_poll
from ..profile import apply_default_task_config

log = logging.getLogger("no_human.intake.jira_poll")

# nh statuses surfaced back to the issue, and the work-note wording. Written
# only on a *change*, and never a terminal/closing state — DONE means no_human
# finished its part (a human merges), not "close the issue".
_STATUS_NOTE: dict[TaskStatus, str] = {
    TaskStatus.IMPLEMENTING: "no_human started work on this issue (In Progress).",
    TaskStatus.REVIEWING: "no_human is running its independent staff-level review.",
    TaskStatus.AWAITING_APPROVAL: (
        "no_human opened a pull request and it is awaiting human approval. "
        "no_human never merges."
    ),
    TaskStatus.BLOCKED: "no_human is blocked on this issue and parked it with a wake condition.",
    TaskStatus.AWAITING_INPUT: "no_human needs human input to proceed on this issue.",
    TaskStatus.ESCALATED: "no_human escalated this issue — it needs a human to look.",
    TaskStatus.FAILED: "no_human's attempts on this issue failed — it needs a human to look.",
    TaskStatus.DONE: "no_human completed its work on this issue.",
}

# Off-ramp "needs a human" statuses: commented, never transitioned. Before
# posting one of these notes, sync_statuses() checks whether a human already
# moved the issue to Done in Jira — if so, the note would lie (SCRUM-53).
_HUMAN_ATTENTION: set[TaskStatus] = {
    TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
    TaskStatus.ESCALATED, TaskStatus.FAILED,
}

# Status -> target Jira status-CATEGORY key (never a hardcoded transition id).
# Only active-work and done statuses get a transition; off-ramps (blocked,
# awaiting_input, escalated, failed, paused_quota) are deliberately absent —
# they comment (via _STATUS_NOTE) but never transition (requirement #3).
_TARGET_CATEGORY: dict[TaskStatus, str] = {
    TaskStatus.CONTEXT: "indeterminate",
    TaskStatus.PLANNING: "indeterminate",
    TaskStatus.IMPLEMENTING: "indeterminate",   # first-attempt claim -> In Progress
    TaskStatus.REVIEWING: "indeterminate",
    TaskStatus.TESTING: "indeterminate",
    TaskStatus.AWAITING_APPROVAL: "indeterminate",
    TaskStatus.DONE: "done",
}

# Status-category key -> human label for event text only (adapter.transition
# still gets the raw key above). Falls back to the raw key if unmapped.
_CATEGORY_LABEL: dict[str, str] = {
    "new": "To Do",
    "indeterminate": "In Progress",
    "done": "Done",
}


@dataclass
class PollResult:
    created: int = 0
    skipped: int = 0          # already tracked (deduped)
    seen: int = 0             # total issues the JQL returned
    numbers: list[str] = field(default_factory=list)  # newly-created issue keys


class JiraPoller:
    def __init__(self, adapter, store: Store, *, config: dict | None = None, on_event=None):
        self.adapter = adapter
        self.store = store
        self._config = config or {}
        j = ((config or {}).get("integrations") or {}).get("jira") or {}
        self.default_repo = j.get("default_repo") or getattr(adapter, "default_repo", None)
        self.write_back = bool(j.get("write_back", False))
        self._on_event = on_event or (lambda kind, text: None)

    async def _existing_ids(self) -> set[str]:
        return {
            t.external_id
            for t in await self.store.list_tasks()
            if t.source == "jira" and t.external_id
        }

    async def poll_once(self) -> PollResult:
        result = PollResult()
        try:
            issues = await asyncio.to_thread(self.adapter.search)
        except Exception as exc:  # noqa: BLE001 — transport error retried next tick
            log.warning("Jira poll failed: %s", exc)
            self._on_event("jira_poll_error", str(exc))
            return result

        existing = await self._existing_ids()
        for issue in issues:
            key = issue.get("key")
            if not key:
                continue
            result.seen += 1
            if key in existing:
                result.skipped += 1
                continue
            try:
                task: Task = self.adapter.normalize(issue)
            except Exception as exc:  # noqa: BLE001
                log.warning("Jira normalize %s failed: %s", key, exc)
                continue
            if self.default_repo:
                task.repo_path = self.default_repo
            if task.repo_path:
                profile = await self.store.get_profile(task.repo_path)
                task.config = apply_default_task_config(profile, task.config)
            await self.store.create_task(task)
            existing.add(key)
            result.created += 1
            result.numbers.append(key)
            self._on_event("jira_task_created", f"{key}: {task.title}")
        if result.created:
            self._on_event(
                "jira_poll",
                f"created {result.created} task(s) from {result.seen} issue(s)",
            )
        return result

    async def _issue_already_done(self, key: str) -> bool:
        """True only if the issue's current Jira status category is 'done'.
        A fetch error degrades to False (proceed to comment — today's
        behavior) and leaves the marker unset so the next tick retries
        (SCRUM-53 intake Q&A)."""
        try:
            cat = await asyncio.to_thread(self.adapter.status_category, key)
        except Exception as exc:  # noqa: BLE001 — transient; retry next tick
            log.warning("Jira status check %s failed: %s", key, type(exc).__name__)
            return False
        return cat == "done"

    async def _pr_url_for(self, task: Task) -> str:
        try:
            attempts = await self.store.list_attempts(task.id)
        except Exception:  # noqa: BLE001
            return ""
        for a in reversed(attempts):
            if a.get("pr_url"):
                return a["pr_url"]
        return ""

    async def sync_statuses(self) -> int:
        """Opt-in write-back: as each jira task advances, transition its issue
        into the matching status category (SCRUM-21) and post a work-note
        comment, once per change. These are independent idempotency markers —
        a transition no-op/failure never suppresses the comment, and vice
        versa. Escalated/failed/blocked tasks are commented but never
        transitioned (state must never lie)."""
        if not self.write_back:
            return 0
        written = 0
        newest_by_key: dict[str, Task] = {}
        for task in await self.store.list_tasks():
            if task.source != "jira" or not task.external_id:
                continue
            cur = newest_by_key.get(task.external_id)
            if cur is None or (task.created_at, task.id) > (cur.created_at, cur.id):
                newest_by_key[task.external_id] = task
        for task in newest_by_key.values():
            jira = (task.context or {}).get("jira") or {}
            changed = False

            # --- transition (SCRUM-21): once per category, independent of comment ---
            done_cats = jira.get("nh_jira_transitions") or []
            target = _TARGET_CATEGORY.get(task.status)
            if target and target not in done_cats:
                try:
                    await asyncio.to_thread(
                        self.adapter.transition, task.external_id, target)
                except Exception as exc:  # noqa: BLE001 — fire-and-forget; unset -> retry next tick
                    log.warning("Jira transition %s failed: %s", task.external_id, type(exc).__name__)
                else:
                    # Marker set on True OR no-match(False) alike — both are a
                    # handled outcome; only a raised exception should retry.
                    jira["nh_jira_transitions"] = [*done_cats, target]
                    changed = True
                    label = _CATEGORY_LABEL.get(target, target)
                    self._on_event("jira_transitioned", f"{task.external_id} → {label}")

            # --- comment (existing behavior; PR link now also on DONE) ---
            note = _STATUS_NOTE.get(task.status)
            if note and jira.get("nh_synced_status") != task.status.value:
                if (task.status in _HUMAN_ATTENTION
                        and await self._issue_already_done(task.external_id)):
                    # A human already closed this issue in Jira; the local
                    # failed/escalated/blocked row is real but a "needs a
                    # human" note would lie. Skip + mark so we never re-check
                    # this state (SCRUM-53).
                    jira["nh_synced_status"] = task.status.value
                    changed = True
                    self._on_event(
                        "jira_status_synced",
                        f"{task.external_id} already Done in Jira → skip note")
                else:
                    if task.status in (TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE):
                        pr = await self._pr_url_for(task)
                        if pr:
                            note = f"{note}\nPR: {pr}"
                    try:
                        await asyncio.to_thread(
                            self.adapter.comment, task.external_id, note)
                    except Exception as exc:  # noqa: BLE001 — type-name only, no URL/auth/body leak
                        log.warning("Jira comment %s failed: %s", task.external_id, type(exc).__name__)
                    else:
                        jira["nh_synced_status"] = task.status.value
                        changed = True
                        self._on_event("jira_status_synced", f"{task.external_id} → {task.status.value}")

            if changed:
                # merge_context, never update_task: this loop holds a snapshot
                # taken before slow network calls, and a full-row write from it
                # erased context keys (pr_watch) merged in the meantime (R15,
                # 2026-08-09 incident).
                task.context = await self.store.merge_context(
                    task.id, {"jira": jira})
                written += 1
        return written

    async def tick(self) -> PollResult:
        """One poll + write-back pass; errors in one half never block the other."""
        await ensure_fresh_before_poll("jira", self._config)
        result = await self.poll_once()
        try:
            await self.sync_statuses()
        except Exception as exc:  # noqa: BLE001
            log.warning("Jira status sync pass failed: %s", exc)
        return result
