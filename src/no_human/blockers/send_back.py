"""The "a human send-back onto an exhausted budget silently FAILS the task"
defect (2026-08-22): a send-back delivered while a loop-head gate (lifetime
budget, attempt startup floor, or any future gate that refuses BEFORE an
attempt row exists) refuses to start a round used to look identical to
organic exhaustion — the reviewer's feedback was recorded
(``send_back_feedback``) and the task moved, but nothing ever said the
send-back itself was refused.

This module owns the fix's one piece of state: a "a send-back is pending and
has not yet started a round" marker on ``task.context["pending_send_back"]``,
written by every human send-back producer (the ``POST /send-back`` API route,
``nh reject``, and the PR-comment path in ``blockers.wake``) and consumed by
``core.orchestrator._refuse_round`` / cleared at ``attempt_start``.

No forge-posting lives here (see ``.no_human/PLAN.md`` APPROACH) — this is
pure bookkeeping plus two thin store writers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: The one context key this module owns.
PENDING_KEY = "pending_send_back"

#: The two fields `mark_send_back_refused` owns, nested under `PENDING_KEY`.
#: Stamped once a refusal has already been reported for the CURRENT pending
#: send-back, so `_refuse_round` emits `send_back_refused` at most once per
#: marker instead of once per resume.
REFUSED_AT_KEY = "refused_at"
REFUSED_GATE_KEY = "refused_gate"

#: `refusal_note`'s excerpt is capped so a huge send-back doesn't bloat the
#: blocker's `root_cause_hypothesis` / `evidence` text.
_EXCERPT_CHARS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _excerpt(message: str) -> str:
    collapsed = " ".join((message or "").split())
    if len(collapsed) <= _EXCERPT_CHARS:
        return collapsed
    return collapsed[:_EXCERPT_CHARS] + "…"


async def record_pending_send_back(
    store: Any,
    task: Any,
    *,
    source: str,
    message: str,
    actor: str = "",
    at: str | None = None,
    pr_ref: str | None = None,
) -> dict[str, Any] | None:
    """Record that a human send-back is pending and has not yet started a
    round. Called by every producer (API `/send-back`, `nh reject`, the
    PR-comment path) right after they append to `send_back_feedback` —
    same message, same timestamp.

    Never raises into the caller's path: a marker must never break the
    send-back it is describing. Returns the written entry, or ``None`` if
    the write failed (logged, swallowed).
    """
    entry = {
        "at": at or _now(),
        "source": source,
        "actor": actor or "",
        "chars": len(message or ""),
        "excerpt": _excerpt(message or ""),
        "pr_comment_ref": pr_ref or "",
    }
    try:
        # RFC 7396 recursive-merge hazard: `merge_context` merges nested
        # dicts key-by-key, so a bare `{PENDING_KEY: entry}` patch over an
        # ALREADY-refused marker would preserve its stale `refused_at`/
        # `refused_gate` — this new send-back would then be silently
        # suppressed by `_refuse_round`'s "already stamped" check forever.
        # Explicitly deleting (RFC 7396: `None` value) both fields in the
        # same patch makes a fresh send-back always eligible to refuse-and-
        # emit again.
        patch = {
            **entry,
            REFUSED_AT_KEY: None,
            REFUSED_GATE_KEY: None,
        }
        merged = await store.merge_context(task.id, {PENDING_KEY: patch})
        if merged is not None:
            task.context = merged
        return entry
    except Exception:
        logger.exception(
            "record_pending_send_back: failed to persist marker for task %s "
            "(source=%s) — the send-back itself was still recorded by the "
            "caller", getattr(task, "id", "?"), source,
        )
        return None


async def clear_pending_send_back(store: Any, task: Any) -> None:
    """Clear the pending marker — a round DID start, so the send-back (if
    any) is no longer pending. RFC 7396 delete via ``db.py``'s
    `merge_context`: a ``None`` value removes the key outright, so a task
    with no pending send-back is a no-op cleared to the same "absent" state.
    """
    try:
        merged = await store.merge_context(task.id, {PENDING_KEY: None})
        if merged is not None:
            task.context = merged
    except Exception:
        logger.exception(
            "clear_pending_send_back: failed to clear marker for task %s",
            getattr(task, "id", "?"),
        )


async def mark_send_back_refused(
    store: Any, task: Any, *, gate: str, at: str | None = None,
) -> dict[str, Any] | None:
    """Stamp the CURRENT pending marker as already-refused, so a later
    resume with no new human action does not emit `send_back_refused`
    again. The single writer of `REFUSED_AT_KEY` / `REFUSED_GATE_KEY`.

    Merges `{PENDING_KEY: {REFUSED_AT_KEY: ..., REFUSED_GATE_KEY: ...}}`
    through `merge_context` (RFC 7396: nested dicts merge recursively), so
    the existing `at`/`source`/`actor`/`excerpt`/`pr_comment_ref` fields on
    the marker are left byte-identical — this is a stamp, not a rewrite.

    Never raises: identical fail-open posture to `record_pending_send_back`.
    On failure the marker stays unstamped and the worst case is one more
    duplicate `send_back_refused` event next wake — never worse than the
    pre-fix behaviour.
    """
    patch = {REFUSED_AT_KEY: at or _now(), REFUSED_GATE_KEY: gate}
    try:
        merged = await store.merge_context(task.id, {PENDING_KEY: patch})
        if merged is not None:
            task.context = merged
        return patch
    except Exception:
        logger.exception(
            "mark_send_back_refused: failed to stamp marker for task %s "
            "(gate=%s)", getattr(task, "id", "?"), gate,
        )
        return None


def refusal_note(pending: dict[str, Any], *, gate: str, remedy: str) -> str:
    """The exact sentence the ACs demand: names the send-back, the refusing
    gate, and the printed remedy — never invented, always the blocker's own
    text (``remedy_text``)."""
    actor = pending.get("actor") or "the reviewer"
    source = pending.get("source", "send-back")
    at = pending.get("at", "")
    chars = pending.get("chars", 0)
    delivered = (
        f"send-back from {actor} via {source} at {at} ({chars} chars) was "
        "delivered and recorded, but no round started."
    )
    refused = (
        f"your send-back could not start a round: {gate} refused, "
        f"remedy: {remedy}"
    )
    return f"{delivered} {refused}"


def refusal_event(
    task: Any,
    pending: dict[str, Any],
    blocker: Any,
    *,
    gate: str,
    remedy: str,
    pr_url: str = "",
) -> dict[str, Any]:
    """The structured, PR-consumable payload — everything a future PR-side
    consumer needs to surface this without re-deriving it from prose."""
    category = blocker.category
    category_value = getattr(category, "value", category)
    return {
        "task_id": getattr(task, "id", ""),
        "gate": gate,
        "category": category_value,
        "source": pending.get("source", ""),
        "actor": pending.get("actor", ""),
        "feedback_at": pending.get("at", ""),
        "feedback_chars": pending.get("chars", 0),
        "excerpt": pending.get("excerpt", ""),
        "remedy": remedy,
        "pr_url": pr_url,
        "pr_comment_ref": pending.get("pr_comment_ref", ""),
        "refused_at": _now(),
        "send_back_refused": True,
    }


def remedy_text(blocker: Any) -> str:
    """The remedy the blocker *already* prints — `wake_condition` (terminal
    mode; contains the literal `nh task config ... ` line from
    `_budget_revival_condition`) or the first `set_task_config` option's
    label (non-terminal mode). Never invents a remedy; returns "" if the
    blocker has neither (should not happen for a loop-head refusal, but this
    stays a lookup, not a generator)."""
    if blocker.wake_condition:
        # `wake_condition` is a noun-phrase fragment written to complete the
        # UI drawer's "Was waiting for" label (see
        # `_budget_revival_condition`'s docstring) — prefixed here so the
        # refusal sentence ("remedy: ...") scans as a sentence too, without
        # changing the command text the fragment carries.
        return f"waiting on {blocker.wake_condition}"
    for option in blocker.options or []:
        action = getattr(option, "action", None) or {}
        if "set_task_config" in action:
            return option.label
    return ""
