"""Which board lane a task belongs in - the single source of truth.

This decision lived only in ``web/src/boardLanes.js``. Anything else that wanted
a lane had to reimplement it, and this repo already shipped that failure once
(PR-007: the counts were right and the lane LABEL lied). The board endpoint now
computes the lane here and ships it on the task payload; the JS keeps the
PRESENTATION half of ``LANES`` (labels, order, colours) and prefers the served
value, falling back to its own copy only when the field is absent.

``tests/test_lane_conformance.py`` and ``web/src/laneConformance.test.mjs`` run
the same fixture file (``testdata/lane_conformance.json``) through both
implementations, so they cannot drift apart without a suite going red.

Input shape: the FLATTENED board payload - a mapping or an object with
``status`` and ``blocker_wake_condition`` attributes (``TaskSummaryOut``). A raw
``core.task.Task`` keeps its wake condition nested under ``task.blocker``, which
this module does not read.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .task import TaskStatus

# Status -> lane, in the order routing consults it. Mirrors the `statuses`
# arrays in web/src/boardLanes.js LANES; the labels/colours/order-on-screen stay
# in the JS because they are presentation.
LANE_STATUSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("answer", ("awaiting_input", "escalated")),
    (
        "working",
        (
            "pending",
            "context",
            "planning",
            "implementing",
            "reviewing",
            "testing",
            "compound_parent",
            "paused_quota",
        ),
    ),
    # partial_success: a crash stranded a real commit after it landed but
    # before a PR existed. It is terminal and it is where a human already
    # looks for "what happened to my task" — not a fourth outcome lane.
    ("failed", ("failed", "partial_success")),
    ("review", ("awaiting_approval",)),
    ("done", ("done",)),
)

LANE_KEYS: tuple[str, ...] = tuple(key for key, _ in LANE_STATUSES)

# An unrecognised status lands in Working rather than vanishing off the board.
FALLBACK_LANE = "working"

_BLOCKED = "blocked"
_PAUSED_QUOTA = "paused_quota"
_AWAITING_APPROVAL = "awaiting_approval"


def _field(task: Any, name: str) -> Any:
    if task is None:
        return None
    if isinstance(task, Mapping):
        return task.get(name)
    return getattr(task, name, None)


def _status_of(task: Any) -> str:
    raw = _field(task, "status")
    raw = getattr(raw, "value", raw)  # TaskStatus -> its string value
    return raw if isinstance(raw, str) else ""


def _wake_condition(task: Any) -> Any:
    return _field(task, "blocker_wake_condition")


def lane_for(task: Any) -> str:
    """Return the lane key for ``task``.

    ``blocked`` routes dynamically: WITH a wake condition it self-resolves and
    sits in Working (parked, see :func:`is_waiting`); WITHOUT one a human must
    act, so it goes to Needs Answer. Every other status routes off
    :data:`LANE_STATUSES`, and anything unrecognised falls back to Working.

    The wake-condition split is on TRUTHINESS, deliberately, so ``""`` routes
    like absent. Note that Python's and JS's falsy sets are not the same: ``[]``
    and ``{}`` are falsy here and TRUTHY in ``boardLanes.js``, so a wake
    condition of ``[]`` would route to Needs Answer in Python and to Working in
    the JS. This is unreachable today - the field is
    ``TaskSummaryOut.blocker_wake_condition: str | None``, and pydantic rejects
    a list or a dict before routing sees it, which is why the conformance
    fixture carries no such case. If that field ever widens beyond ``str |
    None``, the two languages diverge and the shared fixture must gain the case.
    """
    status = _status_of(task)
    if status == _BLOCKED:
        return "working" if _wake_condition(task) else "answer"
    for key, statuses in LANE_STATUSES:
        if status in statuses:
            return key
    return FALLBACK_LANE


def is_waiting(task: Any) -> bool:
    """True when the task is parked on a signal it resolves by itself.

    Distinct from the lane: these sit in Working, but they are not live work,
    so the card says so instead of looking in-flight.
    """
    status = _status_of(task)
    if status == _PAUSED_QUOTA:
        return True
    return status == _BLOCKED and bool(_wake_condition(task))


def approval_pending(task: Any) -> bool:
    """True when ``task`` carries a LIVE, unresolved approval — the one
    predicate the API payload (``TaskSummaryOut.approval_superseded_at``),
    the CLI (``shell_lanes.py``) and the board/drawer (``approvalState.js``'s
    ``approvalLive``) all derive the "approved - merge pending" chip and the
    needs-you count suppression from, so none of them can ever contradict the
    lane again.

    A bare ``approved_at`` is NOT enough: ``core/db.py::_write_status``
    stamps ``context.approval_superseded_at`` (write-once) the moment a row
    leaves ``awaiting_approval`` for anything other than ``done``, so an
    approval recorded before an escalation, a conflict-round send-back, or a
    fresh attempt no longer reads as pending once the row has moved on.
    ``approved_at`` itself is never cleared (it stays the audit trail), which
    is why this also re-checks ``status`` directly rather than trusting the
    stamp alone — belt and suspenders against the exact contradiction this
    function exists to close: an approval surviving into a status it can no
    longer speak for.
    """
    if not _field(task, "approved_at"):
        return False
    if _field(task, "approval_superseded_at"):
        return False
    return _status_of(task) == _AWAITING_APPROVAL


# `nh status`'s bucketing loop (`cli/commands.py:status`), extracted so it has
# a unit test of its own. Distinct from `LANE_STATUSES`/`lane_for` above: this
# is the CLI's six-bucket summary, not the board's lane routing, and the two
# must not be conflated — `lane_for` and its JS conformance twin stay
# untouched by this function.
_NEEDS_YOU = {TaskStatus.AWAITING_APPROVAL, TaskStatus.AWAITING_INPUT,
              TaskStatus.ESCALATED}
# PENDING is QUEUED, not working: the scheduler's `_CLAIMABLE` treats it as a
# task waiting to be picked up, and no worker is spending on it. Counting it
# as working made `working N/max_workers` print impossible ratios like
# `working 5/4` — more in-flight than there are slots to run them.
_QUEUED = {TaskStatus.PENDING}
_WORKING = {TaskStatus.CONTEXT, TaskStatus.PLANNING, TaskStatus.IMPLEMENTING,
            TaskStatus.REVIEWING, TaskStatus.TESTING}
_WAITING = {TaskStatus.PAUSED_QUOTA}


def status_buckets(tasks: Any, waiting_for_slot_ids: Any = ()) -> dict[str, int]:
    """Bucket ``tasks`` into the six `nh status` counts.

    ``waiting_for_slot_ids`` is the set of ids a resumed-but-unclaimed task's
    events say are waiting on a full pool (`Store.tasks_waiting_for_slot`) — a
    task in that set counts as ``waiting``, never ``working``, the same class
    of lie the PENDING comment above already documents: a worker-attached
    count that includes a task with no worker attached.
    """
    waiting_ids = set(waiting_for_slot_ids)
    buckets = {"needs you": 0, "queued": 0, "working": 0, "waiting": 0,
               "failed": 0, "done": 0}
    for t in tasks:
        if t.id in waiting_ids and t.status in _WORKING:
            buckets["waiting"] += 1
        elif t.status == TaskStatus.BLOCKED:
            # Match the board: a blocked task auto-resolves (→ waiting) only
            # with a wake condition; without one a human must act.
            wake = (t.blocker or {}).get("wake_condition")
            buckets["waiting" if wake else "needs you"] += 1
        elif t.status in _NEEDS_YOU:
            buckets["needs you"] += 1
        elif t.status in _QUEUED:
            buckets["queued"] += 1
        elif t.status in _WORKING:
            buckets["working"] += 1
        elif t.status in _WAITING:
            buckets["waiting"] += 1
        elif t.status in (TaskStatus.FAILED, TaskStatus.PARTIAL_SUCCESS):
            # partial_success rolls into the same coarse count as failed: the
            # distinguishing surface is the status string and the board row
            # (LANE_STATUSES above), not this six-bucket CLI summary.
            buckets["failed"] += 1
        elif t.status == TaskStatus.DONE:
            buckets["done"] += 1
    return buckets
