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

    # A LIVE sibling proving it now owns the lease is the terminal "cannot
    # refresh" case: our CAS loses and `_claim_pool_lease` raises
    # `SiblingSchedulerRunning`. (A generic `PoolLeaseLost` — the write merely
    # did not complete — is now NON-latching and retried; see
    # `test_a_transient_refresh_failure_does_not_latch_the_pool`, #222.)
    async def _boom():
        raise SiblingSchedulerRunning(
            pid=os.getppid(), host=platform.node(), age_s=1.0)

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


# --------------------------------------------------------------------------- #
# #222 — a transient refresh failure is NON-latching; only a proven sibling   #
#        takeover is terminal                                                 #
# --------------------------------------------------------------------------- #


async def test_a_transient_refresh_failure_does_not_latch_the_pool(store, monkeypatch):
    """#222: a per-tick refresh that did not COMPLETE (a transient `database is
    locked` past the CAS retry budget) used to set `_lease_lost` permanently,
    so one lock stopped the pool for the process's life. It must be
    non-latching: this tick is a no-op, and a later successful refresh recovers.
    """
    sched = _sched(store)
    await sched._claim_pool_lease()            # baseline: we hold the lease
    assert sched._lease_lost is None

    calls = {"n": 0}

    async def _transient_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise PoolLeaseLost(reason="the CAS write raised",
                                error=OSError("database is locked"))
        # later ticks refresh cleanly (fall through -> success)

    monkeypatch.setattr(sched, "_claim_pool_lease", _transient_once)

    assert await sched.tick() == []            # no dispatch this tick
    assert sched._lease_lost is None           # NOT latched — the #222 fix
    assert sched._lease_refresh_failed         # recorded as a transient state
    assert sched.health_snapshot()["idle_reason"] == "lease_refresh_failing"

    await sched.tick()                         # a later tick refreshes cleanly
    assert sched._lease_lost is None
    assert sched._lease_refresh_failed is None  # cleared on the successful refresh


async def test_a_refresh_failing_past_the_tolerance_fails_closed(store, monkeypatch):
    """#222 review (eyalgolan): a non-latching refresh failure must not retry
    FOREVER. If refresh keeps failing long enough that a sibling could take the
    lease (`_LEASE_REFRESH_TOLERANCE_S`), the tick fails closed, latches
    `_lease_lost` and `run_forever` stops — otherwise a loop that never
    dispatches yet never stops, while its in-flight workers keep going, lets a
    sibling requeue rows those workers still hold (the two-pools hazard)."""
    sched = _sched(store)
    await sched._claim_pool_lease()            # baseline: we hold the lease
    assert sched._lease_lost is None

    async def _always_transient():
        raise PoolLeaseLost(reason="the CAS write raised",
                            error=OSError("database is locked"))

    monkeypatch.setattr(sched, "_claim_pool_lease", _always_transient)
    events: list[tuple[str, str]] = []
    sched._on_event = lambda kind, text: events.append((kind, text))

    # First failure, inside the tolerance window: non-latching, retried.
    assert await sched.tick() == []
    assert sched._lease_lost is None
    assert sched._lease_refresh_failed

    # Make the last heartbeat write that landed look older than the tolerance
    # window, then fail once more: this tick must cross the bound and fail
    # closed.
    sched._lease_refreshed_at_mono = (
        _time.monotonic() - sched._LEASE_REFRESH_TOLERANCE_S - 1)
    assert await sched.tick() == []
    assert sched._lease_lost, "a refresh outage past the tolerance must latch"
    assert any(kind == "pool_lease_lost" for kind, _ in events)

    # Once latched, run_forever stops rather than spinning a dead loop. Restore
    # the real refresh first: run_forever's boot claim is deliberately
    # unguarded (it prints the operator-visible refusal), and our own lease is
    # still held, so the boot claim refreshes cleanly; the first real tick then
    # short-circuits on the latched `_lease_lost` and the loop stops.
    monkeypatch.undo()
    stop = asyncio.Event()
    await asyncio.wait_for(sched.run_forever(stop=stop), timeout=5)
    assert stop.is_set()


# --------------------------------------------------------------------------- #
# #222 — the tolerance is measured from the last heartbeat write that LANDED  #
#        (what a sibling ages), plus the poll interval to the next tick       #
# --------------------------------------------------------------------------- #


class _Clock:
    """A fake wall + monotonic clock for `no_human.core.scheduler` only, so a
    test can step whole poll intervals without sleeping. Both advance
    together, so the heartbeat `ts` written from `time.time()` ages exactly
    as `time.monotonic()` does."""

    def __init__(self):
        self.wall = _time.time()
        self.mono = _time.monotonic()

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def system_sleep(self, seconds):
        """Wall time passes, monotonic does not — what macOS's
        `time.monotonic()` (mach_absolute_time) does across a system sleep."""
        self.wall += seconds

    def wall_jump(self, seconds):
        """The wall clock is reset (e.g. NTP), monotonic does not move."""
        self.wall += seconds


def _install_clock(monkeypatch):
    import no_human.core.scheduler as sched_mod

    clock = _Clock()

    class _FakeTime:
        def __getattr__(self, name):
            return getattr(_time, name)

        def time(self):
            return clock.wall

        def monotonic(self):
            return clock.mono

    monkeypatch.setattr(sched_mod, "time", _FakeTime())
    return clock


def _cas_outage(store, monkeypatch):
    """Route the heartbeat CAS through a switch: while `state["down"]` it
    raises a non-transient error (no retry spent), so the real
    `_claim_pool_lease` fails with `PoolLeaseLost` and nothing is written."""
    real = store.cas_scheduler_heartbeat
    state = {"down": False}

    async def _cas(**kw):
        if state["down"]:
            raise RuntimeError("disk I/O error")
        return await real(**kw)

    monkeypatch.setattr(store, "cas_scheduler_heartbeat", _cas)
    return state


@pytest.mark.parametrize("poll_interval", [10.0, 140.0])
async def test_a_failing_refresh_latches_while_a_sibling_still_sees_a_fresh_heartbeat(
    store, monkeypatch, poll_interval,
):
    """#222 review: a sibling may take the lease once OUR heartbeat's `ts` is
    `_HEARTBEAT_STALE_S` old, and that `ts` is the last write that landed — not
    the first failure. So the bound must be measured from the last success
    and include the poll interval to the next tick, or a long poll interval
    lets the heartbeat go stale before this pool latches (at 140s, counting
    from the first failure latched with the heartbeat 420s old). Every tick of
    the outage, up to and including the one that latches, must happen while
    the heartbeat a sibling would read is still younger than
    `_HEARTBEAT_STALE_S`."""
    clock = _install_clock(monkeypatch)
    outage = _cas_outage(store, monkeypatch)
    sched = _sched(store)
    sched._poll_interval = poll_interval
    await sched._claim_pool_lease()            # boot claim

    for _ in range(3):                         # healthy ticks first
        clock.advance(poll_interval)
        assert await sched.tick() == []
        assert sched._lease_refresh_failed is None
    last_ts = float((await store.read_scheduler_heartbeat())["ts"])

    outage["down"] = True
    failing_ticks = 0
    while sched._lease_lost is None:
        assert failing_ticks < 100, "the outage never latched"
        clock.advance(poll_interval)
        assert await sched.tick() == []
        failing_ticks += 1
        sibling_age = clock.wall - float(
            (await store.read_scheduler_heartbeat())["ts"])
        assert sibling_age < Scheduler._HEARTBEAT_STALE_S, (
            f"tick {failing_ticks} at poll_interval={poll_interval}: the "
            f"heartbeat is {sibling_age:.0f}s old and the pool "
            f"{'has not latched' if sched._lease_lost is None else 'only latched now'}"
            " — a sibling may already have taken the lease")

    # Measured from the last write that landed and counting the poll interval,
    # the latching tick itself comes before the heartbeat reaches the
    # tolerance — the headroom to `_HEARTBEAT_STALE_S` is left for the time a
    # tick takes, not spent on the poll interval. Against half the stale
    # window, not the tolerance constant itself, so the constant is pinned too.
    assert sibling_age < Scheduler._HEARTBEAT_STALE_S / 2
    # Nothing landed during the outage: the age above is the one a sibling saw.
    assert float((await store.read_scheduler_heartbeat())["ts"]) == last_ts
    if poll_interval == 10.0:
        # A short interval still rides out a brief outage before latching
        # (non-latching for most of the tolerance window).
        assert failing_ticks > 1


async def test_a_recovered_outage_does_not_make_a_later_single_failure_latch(
    store, monkeypatch,
):
    """#222 review: every successful refresh restarts the clock. Fail, recover,
    run healthy for 200s (longer than `_LEASE_REFRESH_TOLERANCE_S`), then fail
    ONCE: that single failure is transient and must not latch. If a successful
    refresh did not record itself, the old outage's clock would still be
    running and this one failure would switch the pool off — the behaviour
    #222 removes."""
    clock = _install_clock(monkeypatch)
    outage = _cas_outage(store, monkeypatch)
    sched = _sched(store)
    sched._poll_interval = 10.0
    await sched._claim_pool_lease()            # boot claim

    outage["down"] = True                      # fail ...
    clock.advance(10)
    assert await sched.tick() == []
    assert sched._lease_lost is None
    assert sched._lease_refresh_failed

    outage["down"] = False                     # ... recover ...
    for _ in range(21):                        # ... and stay healthy 210s
        clock.advance(10)
        assert await sched.tick() == []
        assert sched._lease_refresh_failed is None
    assert sched._lease_lost is None

    outage["down"] = True                      # fail once, much later
    clock.advance(10)
    assert await sched.tick() == []
    assert sched._lease_lost is None, (
        "one failure after a recovered outage must not latch the pool")
    assert sched._lease_refresh_failed


async def test_a_failing_refresh_with_no_landed_heartbeat_fails_closed(
    store, monkeypatch,
):
    """With no heartbeat write ever proven to have landed there is nothing to
    measure a tolerance from, so a failing refresh latches instead of being
    retried. (In `run_forever` the boot claim either lands or raises, so this
    is the fail-closed answer for a state production should not reach.)"""
    sched = _sched(store)
    assert sched._lease_refreshed_at_mono is None

    async def _transient():
        raise PoolLeaseLost(reason="the CAS write raised",
                            error=OSError("database is locked"))

    monkeypatch.setattr(sched, "_claim_pool_lease", _transient)
    assert await sched.tick() == []
    assert sched._lease_lost
    assert "no heartbeat write has landed" in sched._lease_lost


async def test_a_failing_refresh_after_a_system_sleep_latches(store, monkeypatch):
    """A sibling ages our heartbeat by WALL clock, but on macOS
    `time.monotonic()` does not advance while the machine sleeps. After an
    hour asleep the heartbeat is an hour old to any sibling, so a failing
    refresh must latch at once, even though monotonic says seconds passed."""
    clock = _install_clock(monkeypatch)
    outage = _cas_outage(store, monkeypatch)
    sched = _sched(store)
    sched._poll_interval = 10.0
    await sched._claim_pool_lease()            # boot claim

    clock.system_sleep(3600)
    outage["down"] = True
    assert await sched.tick() == []
    assert sched._lease_lost, (
        "a heartbeat an hour old by wall clock must latch the pool")


async def test_a_backwards_wall_clock_jump_does_not_hide_an_outage(
    store, monkeypatch,
):
    """The other direction: if the wall clock is set back an hour, wall time
    alone would make the last heartbeat write look newer than it is. The
    monotonic reading still ages it, so an outage that has outlived the
    tolerance by monotonic time latches."""
    clock = _install_clock(monkeypatch)
    outage = _cas_outage(store, monkeypatch)
    sched = _sched(store)
    sched._poll_interval = 10.0
    await sched._claim_pool_lease()            # boot claim

    clock.wall_jump(-3600)
    outage["down"] = True
    clock.advance(sched._LEASE_REFRESH_TOLERANCE_S)
    assert await sched.tick() == []
    assert sched._lease_lost, (
        "a monotonic outage past the tolerance must latch despite the wall jump")


async def test_the_refresh_tolerance_leaves_room_for_the_stop_drain():
    """Once a failing refresh latches, `run_forever` drains for up to
    `concurrency.stop_grace_s` before the in-flight workers stop. With the
    default grace, latch + drain must still end inside a sibling's
    `_HEARTBEAT_STALE_S` window."""
    from no_human.core.scheduler import DEFAULT_STOP_GRACE_S

    assert DEFAULT_STOP_GRACE_S == 60.0
    assert (Scheduler._LEASE_REFRESH_TOLERANCE_S + DEFAULT_STOP_GRACE_S
            < Scheduler._HEARTBEAT_STALE_S)
