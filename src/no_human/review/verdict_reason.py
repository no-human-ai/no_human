"""A failing review must say why, even when the reviewer named nothing.

Issue #249. The orchestrator composed a failed review's `failure_reason` as::

    detail = "review failed: " + "; ".join(
        f"{i.label}: {i.evidence}" for i in failed[:3])

`failed` is `blocking_items or failed_items`, and when BOTH are empty the join
produces `""` and the attempt is recorded with the literal string
`"review failed: "` and nothing after it. Three attempts across the whole
history landed that way, spending 159 turns and three attempt slots on verdicts
that explained nothing. With `max_attempts = 3`, one such round is a third of
a task's allowance.

The two shapes reach it differently and this module keeps them apart, because
they are different facts about what happened:

* the reviewer produced NO checklist at all, so there was never a finding to
  report; and
* the reviewer produced a checklist whose items ALL PASS and still returned
  `passed=False`, so the verdict contradicts the evidence it published. One
  case in 529 failed reviews, and the sharper defect: a later agent cannot
  reason about it, and the next attempt starts with no signal.

Neither is given `REVIEW_SESSION_ERROR_MARKER`. `reviewer.py` draws that line
explicitly and this module stays on the documented side of it: a DEAD session
is infra and parks, while "a reviewer that ran and produced no parseable
verdict, or ran out of turns, still escalates to a person". The reviewer ran
in both shapes here, so a person is the right destination; what was missing
was a sentence for that person to read.

This changes what a failure SAYS, never whether it fails.
"""
from __future__ import annotations

from typing import Any

#: Kept identical to the string the orchestrator composed, so a reason that
#: does carry findings reads exactly as it did before this module existed.
_PREFIX = "review failed: "

#: How many findings ride out in the reason. Unchanged from the call site.
_MAX_FINDINGS = 3


def review_failure_detail(decision: Any, failed: list) -> str:
    """The `failure_reason` for a review that did not pass.

    `failed` is the caller's already-computed `blocking_items or
    failed_items`, taken as an argument rather than re-derived so the sentence
    can never disagree with the list the round actually acted on.
    """
    if failed:
        return _PREFIX + "; ".join(
            f"{i.label}: {i.evidence}" for i in failed[:_MAX_FINDINGS])

    checklist = list(getattr(decision, "checklist", None) or [])
    if not checklist:
        return (
            _PREFIX + "the reviewer produced no checklist and no finding, so "
            "this round has nothing to act on. Treat the verdict as "
            "unparseable rather than as a rejection of the diff: the session "
            "ran, so this is not an infrastructure park."
        )
    return (
        _PREFIX + f"every one of the {len(checklist)} checklist item(s) "
        "PASSED and the verdict was still a failure, so the verdict "
        "contradicts the evidence it published. Treat it as unparseable "
        "rather than as a finding, and do not re-implement against it: there "
        "is no stated defect to fix."
    )
