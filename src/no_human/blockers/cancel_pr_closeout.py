"""Best-effort draft-PR closeout for human cancel paths and orchestrator abandon.

When a task is moved directly to a terminal cancelled/failed state outside the
orchestrator blocker funnel (or inside `Orchestrator._abandon_draft_pr`), we need
the same draft PR off-ramp semantics:
- never touch a PR that was already delivered for review,
- retitle + close an outstanding draft PR best-effort,
- retire the draft slot bookkeeping regardless of forge outcome,
- record one `pr_draft_abandoned` event carrying the outcome.

WHY RETITLE AS WELL AS CLOSE:
A closed PR keeps its title, and the title is the field a repo's PR list,
searches and notification feeds show. Retitling with `[ABANDONED — not
delivered]` tells anyone who meets the PR there that it was abandoned and never
delivered for review, which a closed state alone does not say.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

log = logging.getLogger("no_human.wake")

# 🔴 REASON-NEUTRAL, AND IT MUST STAY THAT WAY. This read "[ABANDONED —
# attempt failed review]", which `_raise_blocker` then stamped on EVERY
# escalated route regardless of why. On the CI-infra route the review has
# already PASSED (`_run_attempt`: draft -> review -> CI -> escalate, all
# before `_finalize`), so the title asserted a review failure that did not
# happen — on the one field a repo's PR list shows, in the branch whose
# whole purpose is to stop no_human claiming untrue things.
#
# WHY A CONSTANT AND NOT A PER-REASON PREFIX: the routes reaching this
# close-out are open-ended — every blocker category that ends on the
# funnel's terminal branch (via `_abandon_draft_pr`),
# `_open_draft_pr_for_review` retiring a draft when a revision moves to a
# new branch, and the human cancel and split paths — and the only fact EVERY
# caller has actually established is that this draft did not become a
# delivered PR. Anything narrower is a claim the call site cannot guarantee,
# and a wrong mapping would just reintroduce this defect one route at a time.
# The real reason is already passed in as `reason` and recorded on the
# `pr_draft_abandoned` event, which is the field with room to be precise.
_ABANDONED_TITLE_PREFIX = "[ABANDONED — not delivered] "


async def _forge_write(
    verb: str, fn: Callable[..., Any], url: str, *args: Any,
    advise: Callable[[str], None],
) -> tuple[bool, str]:
    """Run one forge write off the event loop; return (ok, error). Never raises."""
    try:
        res = await asyncio.to_thread(fn, url, *args)
    except Exception as exc:  # noqa: BLE001 — must fail-open on cancel
        advise(f"abandoning draft {url} failed: {exc}")
        return False, str(exc)
    ok = isinstance(res, dict) and bool(res.get("ok"))
    if ok:
        return True, ""
    error = (str(res.get("error") or "") if isinstance(res, dict)
             else f"unexpected {type(res).__name__} result")
    advise(f"could not {verb} abandoned draft {url}: {error}")
    return False, error


async def close_draft_pr_on_cancel(
    store: Any,
    task: Any,
    *,
    reason: str,
    reason_from_agent: bool = False,
    config: Any = None,
    close: Callable[[str], dict[str, Any]] | None = None,
    set_title: Callable[[str, str], dict[str, Any]] | None = None,
    on_advisory: Callable[[str], None] | None = None,
    emit: Callable[..., Any] | None = None,
) -> str:
    """Best-effort closeout for a cancel-time or abandon-time outstanding draft PR.

    Returns the abandoned URL, or "" when there is nothing to abandon (or the
    PR is already considered delivered for review and therefore protected).

    Forge and store errors never propagate: cancel and abandon paths are
    terminal once they call this helper, so those failures must not fail the
    off-ramp.
    """
    ctx = task.context or {}
    url = str(ctx.get("pr_draft_created") or "").strip()
    if not url:
        return ""

    def advise(msg: str) -> None:
        if on_advisory:
            on_advisory(msg)
        else:
            log.warning("%s", msg)

    # 🔴 NEVER RETITLE A DELIVERED PR. `_finalize` does not clear the draft
    # slot — it only ever WRITES `pr_watch`/`pr_branch` alongside it — so
    # after a successful delivery the draft slot and the live slot name the
    # SAME pull request. A revision on that branch (`nh reject`, a PR
    # comment) that then exhausts max_attempts walks
    # `_escalate_exhausted` -> `_raise_blocker` -> `_abandon_draft_pr` ->
    # here, and stamped "[ABANDONED — attempt failed review]" onto a
    # human-reviewed PR sitting in AWAITING_APPROVAL — telling the reader it
    # is not a delivered change while it still holds exactly the reviewed
    # code, because the failed revision pushed nothing. A human cancel of a
    # task in that state reaches here with the same slots.
    #
    # `pr_watch`/`pr_branch` are written when a PR goes out for a human:
    # by `_finalize` (after `open_pr` returned and the task moved to
    # AWAITING_APPROVAL) and by approve's already-satisfied landing path
    # (`vcs/task_pr.py`), so their presence is the durable record that a PR
    # was delivered for a human. `_recover_diverged_branch` also writes
    # `pr_branch` mid-attempt, before any delivery, so a draft opened on that
    # recut branch matches by branch and is kept open — the safe direction. Either match is enough: the URL is the
    # direct statement, and the branch survives a forge that spells the same
    # MR's URL two ways. The asymmetry decides the OR — a guard that
    # over-fires costs a dead draft its label, one that under-fires corrupts
    # a live human-reviewed PR.
    # Explicit discriminator first (criterion 3): `pr_delivered_url` is
    # written by `_finalize` at the one place delivery-for-review is
    # established, so it cannot be derived from a title or inferred from any
    # other slot.
    draft_branch = str(ctx.get("pr_draft_branch") or "").strip()
    if (url == str(ctx.get("pr_delivered_url") or "").strip()
            or url == str(ctx.get("pr_watch") or "").strip()
            or (draft_branch
                and draft_branch == str(ctx.get("pr_branch") or "").strip())):
        msg = (f"not abandoning {url}: it is the PR this task delivered for "
               f"review, not a draft an attempt walked away from")
        if on_advisory:
            on_advisory(msg)
        else:
            log.info("%s", msg)
        return ""

    retitle_ok = close_ok = False
    retitle_error = close_error = "non-http URL"
    if url.startswith("http"):
        from ..core.task import commit_subject
        from ..vcs import comment_poster

        cfg_data = getattr(config, "data", config)
        prefix = ""
        if isinstance(cfg_data, dict):
            prefix = str((cfg_data.get("git") or {}).get("commit_prefix", "") or "")
        title = _ABANDONED_TITLE_PREFIX + commit_subject(
            task.title or "", getattr(task, "external_id", None), prefix)
        retitle_ok, retitle_error = await _forge_write(
            "retitle", set_title or comment_poster.set_pr_title, url, title,
            advise=advise)
        close_ok, close_error = await _forge_write(
            "close", close or comment_poster.close_pr, url, advise=advise)

    prior = [u for u in (ctx.get("abandoned_pr_urls") or []) if u]
    if url not in prior:
        prior.append(url)
    abandoned_urls = prior[-6:]
    ctx["abandoned_pr_urls"] = abandoned_urls
    ctx.pop("pr_draft_created", None)
    ctx.pop("pr_draft_branch", None)
    task.context = ctx
    try:
        task.context = await store.merge_context(task.id, {
            "pr_draft_created": None,
            "pr_draft_branch": None,
            "abandoned_pr_urls": abandoned_urls,
        })
    except Exception as exc:  # noqa: BLE001 — bookkeeping failure is non-fatal here
        log.warning("could not persist draft closeout for %s: %s", task.id[:8], exc)

    text = f"{url} — {reason}"
    fields = {
        "task_id": task.id,
        "pr_id": url,
        "pr_url": url,
        "reason": reason,
        "reason_from_agent": bool(reason_from_agent),
        "retitle_ok": retitle_ok,
        "retitle_error": retitle_error,
        "close_ok": close_ok,
        "close_error": close_error,
    }
    try:
        if emit is not None:
            emit("pr_draft_abandoned", text, **fields)
        else:
            await store.save_events(task.id, [{
                "source": "system", "kind": "pr_draft_abandoned", "text": text,
                **fields, "ts": time.time(),
            }])
    except Exception as exc:  # noqa: BLE001 — eventing must not break cancel
        log.warning("could not record pr_draft_abandoned for %s: %s", task.id[:8], exc)

    return url
