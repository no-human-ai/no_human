"""Follow-up to PR #585 (task e037008e): `_claim_pool_lease` treated a read
error on the `scheduler_heartbeat` row as "no row" (UNKNOWN read as "vacant")
and then wrote the heartbeat unconditionally — a transient DB blip could
clobber a live holder's lease row, and the true holder's own per-tick refresh
then swallowed the failure and kept dispatching, UNLEASED, with nothing
reporting it. Two related fixes:

  1. A read failure is a FAILURE, not "nobody holds the lease" — bounded
     retry (`_LEASE_READ_ATTEMPTS`), then `PoolLeaseUnreadable`, never a
     write over a row this process never actually saw.
  2. The claim write is a CAS (`Store.cas_scheduler_heartbeat`), conditioned
     on the row still being exactly what was read — a race between the read
     and the write can no longer silently overwrite a sibling that claimed
     in between. A per-tick refresh that cannot prove its claim landed marks
     the pool unleased (`Scheduler._lease_lost`) and stops dispatching
     rather than swallowing a warning and continuing.

`tests/test_status_clobber.py`'s lease tests (:562-680) are the PR #585
regression pin for the ORIGINAL claim/refresh/takeover behavior and are run
unedited alongside this file — nothing here duplicates or modifies them.
"""

from __future__ import annotations

import asyncio
import os
import platform
import time as _time
from datetime import datetime, timezone

import pytest

from no_human.core.db import Store
from no_human.core.scheduler import (
    PoolLeaseLost,
    PoolLeaseUnreadable,
    Scheduler,
    SiblingSchedulerRunning,
)

pytestmark = pytest.mark.asyncio


class _NeverRunOrch:
    async def run_task(self, task):  # pragma: no cover - dispatch must not run
        raise AssertionError("dispatch must not run in these tests")


def _sched(store):
    return Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)


# --------------------------------------------------------------------------- #
# AC1 — a read failure refuses to claim, never writes over the unread row     #
# --------------------------------------------------------------------------- #


async def test_an_unreadable_lease_row_refuses_to_claim_and_never_overwrites_it(
    store, monkeypatch,
):
    """THE REPRO. A live sibling holds the lease; every read of the row then
    raises (a transient DB blip). On unfixed main this was read as `row =
    None` ("nobody holds it") and the heartbeat was written anyway, clobbering
    the sibling's row. The fix must refuse the claim and leave the row
    untouched."""
    sibling_pid = os.getppid()  # alive, and provably not ours
    sibling_ts = _time.time()
    await store.write_scheduler_heartbeat(
        pid=sibling_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(), ts=sibling_ts)

    async def _boom():
        raise OSError("simulated transient DB read failure")

    monkeypatch.setattr(store, "read_scheduler_heartbeat", _boom)

    sched = _sched(store)
    with pytest.raises(PoolLeaseUnreadable):
        await sched._claim_pool_lease()

    monkeypatch.undo()
    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == sibling_pid, (
        "a read failure must never result in this process's own heartbeat "
        "being written over a sibling's live row")
    assert row["ts"] == sibling_ts


async def test_the_read_is_retried_a_bounded_number_of_times_then_refuses(
    store, monkeypatch,
):
    calls = {"n": 0}
    real_read = store.read_scheduler_heartbeat

    async def _fail_twice_then_succeed():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError(f"transient failure #{calls['n']}")
        return await real_read()

    monkeypatch.setattr(store, "read_scheduler_heartbeat", _fail_twice_then_succeed)
    sched = _sched(store)

    await sched._claim_pool_lease()  # must not raise — 3rd attempt succeeds

    assert calls["n"] == 3
    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == os.getpid()

    calls["n"] = 0

    async def _always_fail():
        calls["n"] += 1
        raise OSError(f"transient failure #{calls['n']}")

    monkeypatch.setattr(store, "read_scheduler_heartbeat", _always_fail)
    sched2 = _sched(store)

    with pytest.raises(PoolLeaseUnreadable) as exc_info:
        await sched2._claim_pool_lease()

    assert calls["n"] == Scheduler._LEASE_READ_ATTEMPTS
    assert str(Scheduler._LEASE_READ_ATTEMPTS) in str(exc_info.value)


# --------------------------------------------------------------------------- #
# AC2 — the claim write is a CAS, proved by a row changing under it           #
# --------------------------------------------------------------------------- #


async def test_a_row_changed_between_read_and_write_is_not_overwritten(
    store, monkeypatch,
):
    """A stale sibling row means `_claim_pool_lease` intends a takeover. But
    between its read and its write, a DIFFERENT, live sibling claims the
    lease (the exact race the CAS closes). The claim must refuse — never
    overwrite the interloper — and must say so by naming it."""
    stale_pid = os.getppid()
    await store.write_scheduler_heartbeat(
        pid=stale_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(),
        ts=_time.time() - 600)  # older than _HEARTBEAT_STALE_S (300s)

    # Must be a REAL, alive, foreign pid — `pid_alive` would otherwise treat
    # an arbitrary made-up pid as dead and this process would (correctly)
    # take the lease over instead of yielding to the interloper, which is
    # not the race this test is proving.
    interloper_pid = os.getppid()
    interloper_ts = _time.time()
    real_read = store.read_scheduler_heartbeat
    planted = {"done": False}

    async def _read_then_plant_interloper():
        row = await real_read()
        if not planted["done"]:
            planted["done"] = True
            # A concurrent claim landing between our read and our write —
            # bypass the Store API (which would itself CAS) to simulate a
            # raw concurrent writer.
            await store.db.execute(
                "UPDATE scheduler_heartbeat SET pid=?, host=?, ts=? WHERE id=1",
                (interloper_pid, platform.node(), interloper_ts))
            await store.db.commit()
        return row

    monkeypatch.setattr(store, "read_scheduler_heartbeat",
                         _read_then_plant_interloper)

    sched = _sched(store)
    with pytest.raises(SiblingSchedulerRunning) as exc_info:
        await sched._claim_pool_lease()

    assert str(interloper_pid) in str(exc_info.value)

    monkeypatch.undo()
    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == interloper_pid, (
        "the CAS-rejected claim must leave the interloper's row untouched, "
        "not overwrite it with this process's own pid")


async def test_cas_scheduler_heartbeat_rejects_a_stale_expectation(store):
    now = _time.time()
    started = datetime.now(timezone.utc).isoformat()

    # No row yet: expect=None succeeds exactly once.
    landed = await store.cas_scheduler_heartbeat(
        pid=111, host="h", started_at=started, ts=now, expect=None)
    assert landed is True

    landed_again = await store.cas_scheduler_heartbeat(
        pid=222, host="h2", started_at=started, ts=now, expect=None)
    assert landed_again is False, "expect=None must only succeed while no row exists"

    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == 111  # unchanged by the rejected expect=None write

    # A matching expectation succeeds.
    landed_match = await store.cas_scheduler_heartbeat(
        pid=111, host="h", started_at=started, ts=now + 1, expect=row)
    assert landed_match is True

    # The same (now-stale) expectation is rejected the second time.
    landed_stale = await store.cas_scheduler_heartbeat(
        pid=333, host="h3", started_at=started, ts=now + 2, expect=row)
    assert landed_stale is False

    final = await store.read_scheduler_heartbeat()
    assert final["pid"] == 111
    assert final["ts"] == now + 1


# --------------------------------------------------------------------------- #
# AC3 — a holder that cannot refresh its lease stops dispatching              #
# --------------------------------------------------------------------------- #


async def test_a_holder_that_cannot_refresh_its_lease_stops_dispatching(
    store, monkeypatch,
):
    sched = _sched(store)
    await sched._claim_pool_lease()
    assert sched._lease_lost is None

    async def _boom():
        raise PoolLeaseLost(reason="simulated refresh failure")

    monkeypatch.setattr(sched, "_claim_pool_lease", _boom)

    events: list[tuple[str, str]] = []
    sched._on_event = lambda kind, text: events.append((kind, text))

    result = await sched.tick()
    assert result == []
    assert sched._lease_lost, "tick() must record the loss, not swallow it"
    assert any(kind == "pool_lease_lost" for kind, _ in events)

    # A second tick, still lease-lost, must remain a strict no-op — no orphan
    # sweep, no dispatch, no attempt to reclaim via the queue.
    events.clear()
    result2 = await sched.tick()
    assert result2 == []
    assert events == []


async def test_health_snapshot_reports_lease_lost(store):
    sched = _sched(store)
    await sched._claim_pool_lease()
    sched._lease_lost = "simulated refresh failure"

    snap = sched.health_snapshot()
    assert snap["idle_reason"] == "lease_lost"
    assert snap["lease_lost"] == "simulated refresh failure"


async def test_run_forever_exits_when_the_lease_is_lost(store, monkeypatch):
    sched = _sched(store)

    async def _lose_lease_immediately(*, now=None):
        sched._lease_lost = "simulated refresh failure"
        return []

    monkeypatch.setattr(sched, "tick", _lose_lease_immediately)

    stop = asyncio.Event()
    await asyncio.wait_for(sched.run_forever(stop=stop), timeout=5)

    assert stop.is_set()
