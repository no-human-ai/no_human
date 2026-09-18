"""`Store.abandon_open_attempt`'s body, factored out of `db.py` (structural-
budget: keep the frozen `db.py` line count flat — new logic lives here
instead; see `blockers/stall_watchdog.py` for the sibling wake.py split).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .db import serialized_write

if TYPE_CHECKING:
    from .db import Store


@serialized_write
async def abandon_open_attempt(
    store: "Store", task_id: str, *, reason: str,
) -> dict[str, Any] | None:
    """Close *task_id*'s open attempt row as ``interrupted`` with its usage
    columns made non-NULL, for a caller (the stall watchdog) that is
    escalating a task while its worker may STILL be running.

    Unlike `Store.close_open_attempts` — called only by checkpoint-CLEARING
    paths on a task whose worker is already gone — this can run while the
    process that opened the row is still alive (the stall sweep never
    cancels the backend coroutine). That is safe because the row is
    selected and updated by id, same as every other `update_attempt` write;
    if the live session later finishes on its own, its own `update_attempt`
    legitimately overwrites this row with the true final numbers, same as
    any other race between two writers of one row. These numbers are
    last-observed telemetry, not a final result.

    Picks the newest OPEN row the same way `Store.latest_open_attempt` does
    — `(started_at DESC, rowid DESC)`, not `attempt_number` (see that
    method's docstring for why a lower number can be written later).
    Returns the closed row (post-update), or None if there was no open row
    — a second sweep pass over an already-escalated task is then a pure
    no-op, which is what makes `_escalate_if_stalled` idempotent.

    The five usage columns are `COALESCE(<col>, 0)`, not overwritten with a
    fixed 0 — anything the run already billed onto the row before it
    stalled survives untouched; only a still-NULL column (never billed)
    becomes an honest zero. Do NOT fold in `unattributed_usage` rows here:
    those are already counted in the task's own `cost_usd`
    (`TaskOut.from_task`), so adding them to the attempt row too would
    double-count every token.

    Decorated with `@serialized_write` itself (it duck-types against a
    `Store` argument the same way every bound `Store` method does — see
    that decorator for why it does not need genuine method-binding), so a
    caller gets the same write-serialization guarantee a `Store` method
    would give it.
    """
    row = await store._fetchone(
        "SELECT id FROM attempts WHERE task_id = ? AND status = 'in_progress' "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    )
    if row is None:
        return None
    attempt_id = row["id"]
    await store.db.execute(
        "UPDATE attempts SET status = 'interrupted', "
        "completed_at = COALESCE(completed_at, datetime('now')), "
        "failure_reason = COALESCE(NULLIF(TRIM(failure_reason), ''), ?), "
        "turns_used = COALESCE(turns_used, 0), "
        "tokens_used = COALESCE(tokens_used, 0), "
        "output_tokens = COALESCE(output_tokens, 0), "
        "cache_read_tokens = COALESCE(cache_read_tokens, 0), "
        "cache_creation_tokens = COALESCE(cache_creation_tokens, 0) "
        "WHERE id = ?",
        (reason, attempt_id),
    )
    await store.db.commit()
    closed = await store._fetchone("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
    return dict(closed) if closed else None
