"""Best-effort draft-PR closeout for human cancel paths.

When a task is moved directly to a terminal cancelled/failed state outside the
orchestrator blocker funnel, we still need the same draft PR off-ramp semantics
as `Orchestrator._abandon_draft_pr`:
- never touch a PR that was already delivered for review,
- retitle + close an outstanding draft PR best-effort,
- retire the draft slot bookkeeping regardless of forge outcome,
- record one `pr_draft_abandoned` event naming the reason.

This helper is store+task shaped so CLI/API cancel paths can use it without an
orchestrator instance.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("no_human.wake")

_ABANDONED_TITLE_PREFIX = "[ABANDONED — attempt failed review] "


async def close_draft_pr_on_cancel(
    store: Any,
    task: Any,
    *,
    reason: str,
    reason_from_agent: bool = False,
) -> str:
    """Best-effort closeout for a cancel-time outstanding draft PR.

    Returns the abandoned URL, or "" when there is nothing to abandon (or the
    PR is already considered delivered for review and therefore protected).

    Never raises. Cancel paths are already terminal once they call this helper;
    forge/store failures must not change that.
    """
    ctx = task.context or {}
    url = str(ctx.get("pr_draft_created") or "").strip()
    if not url:
        return ""

    delivered_explicit = str(ctx.get("pr_delivered_url") or "").strip()
    if url and url == delivered_explicit:
        log.info(
            "not abandoning %s for task %s: delivered-for-review explicit URL",
            url,
            task.id[:8],
        )
        return ""

    delivered_url = str(ctx.get("pr_watch") or "").strip()
    delivered_branch = str(ctx.get("pr_branch") or "").strip()
    draft_branch = str(ctx.get("pr_draft_branch") or "").strip()
    if (url and url == delivered_url) or (
        draft_branch and draft_branch == delivered_branch
    ):
        log.info(
            "not abandoning %s for task %s: delivered-for-review URL/branch",
            url,
            task.id[:8],
        )
        return ""

    if url.startswith("http"):
        title = _ABANDONED_TITLE_PREFIX + str(task.title or "")
        try:
            from ..vcs.comment_poster import close_pr, set_pr_title

            res = await asyncio.to_thread(set_pr_title, url, title)
            if not res.get("ok"):
                log.warning(
                    "could not retitle abandoned draft %s on cancel: %s",
                    url,
                    res.get("error"),
                )
            res = await asyncio.to_thread(close_pr, url)
            if not res.get("ok"):
                log.warning(
                    "could not close abandoned draft %s on cancel: %s",
                    url,
                    res.get("error"),
                )
        except Exception as exc:  # noqa: BLE001 — must fail-open on cancel
            log.warning("abandoning draft %s on cancel failed: %s", url, exc)

    prior = [u for u in (ctx.get("abandoned_pr_urls") or []) if u]
    if url not in prior:
        prior.append(url)
    ctx["abandoned_pr_urls"] = prior[-6:]
    ctx.pop("pr_draft_created", None)
    ctx.pop("pr_draft_branch", None)
    task.context = ctx

    try:
        await store.update_task(task)
    except Exception as exc:  # noqa: BLE001 — bookkeeping failure is non-fatal here
        log.warning("could not persist cancel draft closeout for %s: %s", task.id[:8], exc)

    event = {
        "source": "system",
        "kind": "pr_draft_abandoned",
        "task_id": task.id,
        "pr_id": url,
        "pr_url": url,
        "reason": reason,
        "reason_from_agent": bool(reason_from_agent),
        "text": f"{url} — {reason}",
    }
    try:
        await store.save_events(task.id, [event])
    except Exception as exc:  # noqa: BLE001 — eventing must not break cancel
        log.warning("could not record pr_draft_abandoned for %s: %s", task.id[:8], exc)

    return url
