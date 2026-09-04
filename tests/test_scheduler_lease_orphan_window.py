"""Follow-up to ec907fb00 (itself a follow-up to PR #585): that fix closed the
fail-open READ path of `_claim_pool_lease` comprehensively, but deliberately
left one gap open and documented it in a comment rather than fixing it —
`_HEARTBEAT_STALE_S` (300s) is a THIRD of `_STRANDED_GRACE_S` (900s), so a
holder that is alive but quiet for 300-900s loses its LEASE while its own
mid-run ROWS are still too young to be considered orphans.

This module tracks the decision (approach (c): accept the window, document
it, pin it — see docs/design/lease-takeover-vs-orphan-grace.md) as executable
tests rather than only a comment, so the two constants and the derived
divergence between them cannot drift silently.
"""

from __future__ import annotations

import logging
import os
import platform
import time
from datetime import datetime, timedelta, timezone

import pytest

from no_human.core.scheduler import Scheduler, SiblingSchedulerRunning
from no_human.core.task import Task, TaskStatus


class _NeverRunOrch:
    async def run_task(self, task):  # pragma: no cover - never dispatched here
        raise AssertionError("dispatch must not run in these tests")


async def _stranded_task(store, *, age_seconds: float) -> Task:
    """Mirrors `tests/test_status_clobber.py::_stranded_task` — a mid-run
    (REVIEWING) row backdated to a given age, with no newer event, so the
    event branch of `_row_is_live` cannot rescue it.

    Returns a task object RE-FETCHED from the store after the backdate, not
    the in-memory object `set_status` mutated: `_recover_orphans` always
    reads fresh rows via `store.list_tasks()`, so it never sees the stale
    in-memory `updated_at`, but this module also calls `_row_is_live(t)`
    directly — that needs the persisted value, or it judges liveness on the
    pre-backdate timestamp `set_status` just wrote and never sees the
    backdate at all."""
    t = Task.new("stranded", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    old = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, t.id))
    await store.db.commit()
    return await store.get_task(t.id)


def test_the_two_lease_constants_are_pinned():
    msg = ("re-read the decision before changing either number — "
           "see docs/design/lease-takeover-vs-orphan-grace.md")
    assert Scheduler._HEARTBEAT_STALE_S == 300.0, msg
    assert Scheduler._STRANDED_GRACE_S == 900.0, msg
    assert Scheduler._STRANDED_GRACE_S == 3 * Scheduler._HEARTBEAT_STALE_S, msg


def test_divergence_constant_is_derived_not_hand_typed():
    assert (Scheduler._LEASE_ORPHAN_DIVERGENCE_S
            == Scheduler._STRANDED_GRACE_S - Scheduler._HEARTBEAT_STALE_S)
    assert Scheduler._LEASE_ORPHAN_DIVERGENCE_S == 600.0


def test_the_design_doc_for_the_accepted_window_exists():
    doc = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "docs" / "design" / "lease-takeover-vs-orphan-grace.md")
    assert doc.is_file()
    text = doc.read_text()
    assert len(text) > 500  # non-trivial: a real decision record, not a stub


async def test_below_the_stale_threshold_the_lease_is_refused(store):
    sibling_pid = os.getppid()  # alive, and provably not ours
    await store.write_scheduler_heartbeat(
        pid=sibling_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(),
        ts=time.time() - 100)

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    with pytest.raises(SiblingSchedulerRunning):
        await sched._claim_pool_lease()

    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == sibling_pid  # untouched — still the sibling's


async def test_inside_the_window_the_lease_is_taken_over_but_the_rows_are_not(
        store):
    """THE CONTRADICTION, pinned as accepted behaviour: at age 600s (inside
    the divergence window) a new process takes the lease over, yet the old
    holder's mid-run row is still treated as live and left untouched."""
    sibling_pid = os.getppid()
    await store.write_scheduler_heartbeat(
        pid=sibling_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(),
        ts=time.time() - 600)
    t = await _stranded_task(store, age_seconds=600)

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    await sched._claim_pool_lease()  # must not raise — the lease IS taken over

    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == os.getpid()

    assert await sched._row_is_live(t) is True
    await sched._recover_orphans(startup=True)
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.REVIEWING  # untouched, not requeued


async def test_past_the_stranded_grace_both_the_lease_and_the_rows_are_reclaimed(
        store):
    sibling_pid = os.getppid()
    await store.write_scheduler_heartbeat(
        pid=sibling_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(),
        ts=time.time() - 1000)
    t = await _stranded_task(store, age_seconds=1000)

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    await sched._claim_pool_lease()  # must not raise
    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == os.getpid()

    assert await sched._row_is_live(t) is False
    await sched._recover_orphans(startup=True)
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING  # reclaimed


async def test_a_takeover_inside_the_window_logs_the_accepted_window(
        store, caplog):
    sibling_pid = os.getppid()
    await store.write_scheduler_heartbeat(
        pid=sibling_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(),
        ts=time.time() - 600)

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    with caplog.at_level(logging.WARNING, logger="no_human.scheduler"):
        await sched._claim_pool_lease()

    takeover_records = [
        r for r in caplog.records
        if r.name == "no_human.scheduler" and r.levelno >= logging.WARNING
        and r.args and sibling_pid in r.args
    ]
    assert len(takeover_records) == 1, (
        f"expected exactly one takeover warning naming pid {sibling_pid}, "
        f"got: {[(r.message, r.args) for r in caplog.records]}")
    assert 600.0 in takeover_records[0].args
    # The 600s figure IS `_LEASE_ORPHAN_DIVERGENCE_S`; `_STRANDED_GRACE_S`
    # is 900. An operator reading the log learns which knob to turn from the
    # parenthetical, so the label must name the constant whose value is
    # printed (found in review of PR #818, plan §66).
    rendered = takeover_records[0].getMessage()
    assert "further 600s (_LEASE_ORPHAN_DIVERGENCE_S)" in rendered, rendered
    assert "(_STRANDED_GRACE_S)" not in rendered, rendered


async def test_a_self_refresh_does_not_log_a_takeover(store, caplog):
    """Mutation guard: the takeover log must sit strictly inside the `row is
    not None and not mine` branch — refreshing OUR OWN lease must never log a
    takeover of ourselves."""
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    await sched._claim_pool_lease()  # first claim, nothing to take over from

    with caplog.at_level(logging.WARNING, logger="no_human.scheduler"):
        await sched._claim_pool_lease()  # refresh of our own row

    takeover_records = [
        r for r in caplog.records
        if r.name == "no_human.scheduler" and "taking over" in r.message
    ]
    assert takeover_records == []
