"""`attempts.completed_at` is stamped where the status becomes terminal.

Issue #245. `completed_at` was written by only 3 of the 43 `update_attempt`
calls in `core/orchestrator.py` that set a `failure_reason`, so "how long did
this failed attempt take" was unanswerable: 377 of 382 failed rows and 44 of
44 interrupted rows carried NULL. Two measurements were silently biased by it,
both in the direction that makes the system look faster and cheaper than it
is, and neither surfaced as an error.

The obvious fix is to add the keyword at the 40 call sites that lack it. This
module exists because that fix does not hold. A call site is a place to forget
something, and this invariant had already been forgotten 40 times out of 43;
the 41st write would forget it again. `Store.update_attempt` is the one place
every one of those writes passes through, and it already carries a backstop of
exactly this shape for exactly this reason: the C2 block above the call to
this function stamps a sentinel `failure_reason` when a caller marks an
attempt failed without one.

So this is that backstop's sibling, and the rule is the same: derive the
terminal timestamp from the terminal status, and never overwrite what a caller
supplied.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

#: The attempt statuses that end an attempt. `in_progress` is the only other
#: value the schema documents (`migrations/0001_init.sql`: "in_progress|
#: succeeded|failed"), and `interrupted` is written by the startup sweep for
#: rows whose worker died without closing them.
TERMINAL_ATTEMPT_STATUSES = frozenset({"succeeded", "failed", "interrupted"})


def utc_now_iso() -> str:
    """The timestamp format every other writer in this codebase uses."""
    return datetime.now(timezone.utc).isoformat()


def stamp_completion(fields: dict[str, Any]) -> dict[str, Any]:
    """Add `completed_at` to `fields` when it marks an attempt terminal.

    Mutates and returns `fields`, matching the style of the C2 backstop it
    sits beside.

    Never clobbers a caller-supplied value, for the same reason C2 never
    clobbers a reason set by an earlier update: three call sites already pass
    their own `completed_at`, and one of them is the delivery-receipt path
    whose timestamp is the one that matters. A falsy value is treated as
    absent rather than as a deliberate choice, because a `None` reaching here
    is a caller that did not decide, not one that decided on nothing.

    A NON-terminal update is left alone entirely. `update_attempt` is also the
    progress writer (25 of its calls set no status at all), and stamping a
    completion on those would be worse than the gap this closes: it would make
    a running attempt look finished.
    """
    if fields.get("status") in TERMINAL_ATTEMPT_STATUSES and not fields.get(
            "completed_at"):
        fields["completed_at"] = utc_now_iso()
    return fields
