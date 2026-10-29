"""Close a task's SUPERSEDED PR(s) when it reaches DONE by a path other than
the merge flow — the merge path (`vcs.approve_merge.land_task`) already closes
the PR it just merged, and the abandon path (`orchestrator._abandon_draft_pr`)
already closes a draft it walks away from. Neither of those covers a
completion that never merges: `nh approve --landed` (a human asserting the
content already landed elsewhere), and the shipped/containment
landed-completion rungs (`blockers/shipped.py`) that reach the same
conclusion automatically. Both used to leave the task's PR open forever —
live incident: PRs #387, #382, #375 found dangling after exactly this.

``close_task_prs_on_completion`` is the shared hook both call, once, right
after they write ``TaskStatus.DONE``.

**The landed PR is LEFT OPEN, not closed (#658).** Every path that reaches
this hook does so because the task's *current* PR's content already landed on
the base branch — that is the whole reason the completion fired. GitHub marks
a PR merged when its head becomes reachable from the base on a push to the
base; closing it instead makes it read **Closed**, which is wrong (the code
is on main) and cannot be undone (re-pointing a closed PR's head at the
landed commit closes it again rather than merging it). So the task's resolved
PR (`vcs.task_pr.resolve_task_pr`) is left open and the reason is recorded;
it should read as merged, and leaving it open keeps that reconcilable instead
of throwing it away. Every OTHER URL the task still carries is a SUPERSEDED
earlier attempt whose code did not land — those are still closed, as before,
so nothing dangles.

**Best-effort, fail-open.** By the time this runs the task is already DONE —
that write already committed. A forge error (auth, 404, rate limit, a merged
PR the forge refuses to close) must never roll that back or raise past this
function: a completed task with a PR that failed to close is strictly better
than losing the completion over a transient API error. Every close is
attempted independently; one failure does not stop the rest.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ..vcs.task_pr import resolve_task_pr, task_pr_urls

log = logging.getLogger("no_human.wake")

PR_CLOSED_ON_COMPLETION_KIND = "pr_closed_on_completion"
#: Recorded for the task's own landed PR, which is deliberately NOT closed
#: (#658) so it can still read as merged rather than Closed.
PR_LEFT_OPEN_ON_COMPLETION_KIND = "pr_left_open_on_completion"

CloseFn = Callable[[str], dict]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def close_task_prs_on_completion(
    store: Any, task: Any, *, completion_path: str,
    close: CloseFn | None = None,
) -> list[str]:
    """Close every SUPERSEDED PR this task still has, leave its landed PR open,
    record one event per PR, and return the URLs actually closed. Never raises.

    The task's resolved PR (`resolve_task_pr`) is the one whose content landed
    — it is left open with a ``pr_left_open_on_completion`` event (#658), never
    closed. Every other URL is a superseded earlier attempt and is closed with
    a ``pr_closed_on_completion`` event.

    ``close`` defaults to ``vcs.comment_poster.close_pr`` (imported lazily —
    keeps ``blockers`` -> ``vcs`` a call-time dependency, matching every
    other caller in this package), run via ``asyncio.to_thread`` since it
    shells out. Tests inject a fake here instead of monkeypatching the forge.
    """
    try:
        urls = await task_pr_urls(store, task)
    except Exception as exc:  # noqa: BLE001 — enumeration must never block completion
        log.warning("pr enumeration failed for %s: %s", task.id[:8], exc)
        return []
    if not urls:
        return []

    # The resolved PR is the task's current one, whose content landed (that is
    # why this completion fired). It must NOT be closed — see the module
    # docstring. A resolution failure falls back to the old close-everything
    # behaviour rather than crashing a completion that is already DONE.
    try:
        landed_url = (await resolve_task_pr(store, task)).url
    except Exception as exc:  # noqa: BLE001 — never block completion on resolution
        log.warning("pr resolution failed for %s: %s", task.id[:8], exc)
        landed_url = ""

    if close is None:
        from ..vcs.comment_poster import close_pr as close

    closed: list[str] = []
    events: list[dict[str, Any]] = []
    for url in urls:
        if landed_url and url == landed_url:
            # This PR's content is on the base branch already; closing it would
            # read as Closed, not Merged, and cannot be undone (#658). Leave it
            # open and record why — it should read as merged, and a human or a
            # later reconcile can still make it so.
            events.append({
                "source": "system",
                "kind": PR_LEFT_OPEN_ON_COMPLETION_KIND,
                "task_id": task.id,
                "pr_id": url,
                "pr_url": url,
                "completion_path": completion_path,
                "recorded_timestamp": _now_iso(),
                "ts": time.time(),
                "text": (f"left {url} open on completion via {completion_path}: "
                         "its content is already on the base branch, so closing "
                         "it would read as Closed rather than Merged"),
            })
            continue
        try:
            res = await asyncio.to_thread(close, url)
        except Exception as exc:  # noqa: BLE001 — one forge failure must not stop the rest
            log.warning("could not close %s on completion (%s): %s",
                       url, completion_path, exc)
            continue
        if not res.get("ok"):
            log.warning("could not close %s on completion (%s): %s",
                       url, completion_path, res.get("error"))
            continue
        closed.append(url)
        events.append({
            "source": "system",
            "kind": PR_CLOSED_ON_COMPLETION_KIND,
            "task_id": task.id,
            "pr_id": url,
            "pr_url": url,
            "completion_path": completion_path,
            "closed_timestamp": _now_iso(),
            "ts": time.time(),
            "text": f"closed superseded {url} on completion via {completion_path}",
        })

    if events:
        try:
            await store.save_events(task.id, events)
        except Exception as exc:  # noqa: BLE001 — the PR is already closed; never undo that
            log.warning("could not record pr completion events for %s: %s",
                       task.id[:8], exc)

    return closed
