"""A transient database lock must not stop dispatch permanently.

Incident this fixes: `_claim_pool_lease`'s CAS write
(`Store.cas_scheduler_heartbeat`) raised `sqlite3.OperationalError('database
is locked')` under ordinary SQLite write contention, and the unfixed code
treated ANY exception from that write as `PoolLeaseLost` immediately — zero
retry. Such a lock is expected (another writer holding the row a few
milliseconds) and clears on its own; the fix is a bounded retry
(`_LEASE_WRITE_ATTEMPTS`/`_LEASE_WRITE_BACKOFF_S`) keyed on the error itself
(`_is_transient_db_error`), not on where it was raised.

This file covers three of the task's acceptance criteria:

  * AC1 — a REAL write-lock collision (a second, genuine OS-level SQLite
    connection holding `BEGIN IMMEDIATE`) is retried and the claim still
    lands once the lock clears; a lock held past the whole retry budget
    still fails closed (bounded, not infinite).
  * AC3 — two real `Scheduler` instances against one database never both
    hold the lease, with a positive control proving the observation
    mechanism used to check that can actually detect a double claim.
  * AC4 — `_is_transient_db_error` classifies a MEASURED real lock
    collision as retryable, and rejects both a same-type/different-message
    `OperationalError` and a same-message/different-type exception —  the
    boundary in both directions.
"""

from __future__ import annotations

import os
import platform
import sqlite3
import threading
import time as _time
from datetime import datetime, timezone

import pytest

from no_human.core.scheduler import (
    PoolLeaseLost,
    Scheduler,
    SiblingSchedulerRunning,
    _is_transient_db_error,
    _TRANSIENT_DB_MESSAGES,
)
import no_human.core.scheduler as scheduler_mod

# asyncio_mode = "auto" (pyproject.toml) already runs every `async def` test
# as a coroutine test; no module-wide `pytestmark` is used here because this
# file also has plain sync tests (the `_is_transient_db_error` boundary
# cases), which the marker would otherwise warn on.


class _NeverRunOrch:
    async def run_task(self, task):  # pragma: no cover - dispatch must not run
        raise AssertionError("dispatch must not run in these tests")


def _sched(store):
    return Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)


def _hold_write_lock(path, acquired: threading.Event, release: threading.Event):
    """Runs on a background thread: opens a SECOND, genuinely independent
    sqlite3 connection to the same file and holds the WAL writer lock via
    `BEGIN IMMEDIATE` until `release` is set. This is real cross-connection
    contention, not a monkeypatched exception — SQLite file locks are
    per-connection, so two connections in one test process contend exactly
    as two separate processes would."""
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        acquired.set()
        release.wait(timeout=5)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# AC1 — a real transient lock is retried, bounded, before giving up          #
# --------------------------------------------------------------------------- #


async def test_a_real_transient_write_lock_is_retried_and_the_claim_still_lands(
    store,
):
    """THE REPRO. A second, real connection holds SQLite's write lock across
    the CAS write's busy_timeout window. On unfixed `_claim_pool_lease` the
    first `OperationalError('database is locked')` was raised straight
    through as `PoolLeaseLost` with zero retry, and the scheduler never
    claimed the lease. With the fix, the claim must survive the collision
    and land once the lock clears."""
    await store.db.execute("PRAGMA busy_timeout = 150")

    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(
        target=_hold_write_lock, args=(store.path, acquired, release),
        daemon=True)
    holder.start()
    try:
        assert acquired.wait(timeout=2), (
            "the holder thread never acquired the write lock")
        # Release well inside the second retry's window (attempt 1 fails at
        # ~150ms, backoff 150ms, attempt 2 runs ~[300ms, 450ms]) — generous
        # margins on both sides so this is not a timing coin-flip.
        threading.Timer(0.35, release.set).start()

        sched = _sched(store)
        sched._LEASE_WRITE_BACKOFF_S = 0.15

        await sched._claim_pool_lease()  # must not raise
    finally:
        release.set()
        holder.join(timeout=2)

    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == os.getpid(), (
        "the retried claim must land once the transient lock clears")


async def test_a_persistent_write_lock_still_fails_closed_after_the_budget(
    store,
):
    """The retry is BOUNDED, not infinite or unconditionally successful: if
    the lock never clears within `_LEASE_WRITE_ATTEMPTS` attempts, the claim
    must still fail closed as `PoolLeaseLost` — a claim this process cannot
    prove landed is not a claim, even under this fix."""
    await store.db.execute("PRAGMA busy_timeout = 30")

    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(
        target=_hold_write_lock, args=(store.path, acquired, release),
        daemon=True)
    holder.start()
    try:
        assert acquired.wait(timeout=2)

        sched = _sched(store)
        sched._LEASE_WRITE_BACKOFF_S = 0.02

        with pytest.raises(PoolLeaseLost) as exc_info:
            await sched._claim_pool_lease()
        assert str(sched._LEASE_WRITE_ATTEMPTS) in str(exc_info.value)
    finally:
        # Only release after the assertion above — releasing early would
        # let a later attempt land and defeat the point of this test.
        release.set()
        holder.join(timeout=2)


# --------------------------------------------------------------------------- #
# AC3 — genuine mutual exclusion between two real schedulers                  #
# --------------------------------------------------------------------------- #


async def test_two_schedulers_against_one_database_never_both_claim_the_lease(
    store, monkeypatch,
):
    """Two real `Scheduler` instances, sequential claims against the SAME
    store. Both share this test's one OS process, so the second claim's
    `os.getpid()` is monkeypatched to the parent pid — a REAL, alive,
    provably-different pid (the same convention `test_status_clobber.py`
    and `test_scheduler_lease_fail_closed.py` use), never a made-up number
    that `pid_alive` would read as dead and take over instead of refuse."""
    # Captured BEFORE any monkeypatching: `os.getpid` is patched below via
    # the SAME shared `os` module object scheduler.py imports, so a fresh
    # `os.getpid()` call after that point would itself return the patched
    # value, not this process's real pid.
    real_self_pid = os.getpid()
    real_parent_pid = os.getppid()

    sched_a = _sched(store)
    await sched_a._claim_pool_lease()
    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == real_self_pid

    monkeypatch.setattr(scheduler_mod.os, "getpid", lambda: real_parent_pid)
    sched_b = _sched(store)
    with pytest.raises(SiblingSchedulerRunning) as exc_info:
        await sched_b._claim_pool_lease()
    assert exc_info.value.pid == real_self_pid

    monkeypatch.undo()
    row_after = await store.read_scheduler_heartbeat()
    assert row_after["pid"] == real_self_pid, (
        "the second scheduler's claim must never land while the first "
        "still holds a live lease — a double claim here would mean two "
        "pools could both dispatch against the same queue")


async def test_positive_control_the_mutual_exclusion_test_can_observe_a_double_claim(
    store,
):
    """Positive control for the test above: without ANY guard (the raw
    unconditional upsert `write_scheduler_heartbeat` — no CAS, no liveness
    check), a second, different pid's claim DOES land over the first's,
    proving the read-the-heartbeat-row-back mechanism the real test relies
    on can actually detect a double claim when one happens. Without this,
    the real test's `pytest.raises(SiblingSchedulerRunning)` could be
    passing for a reason that has nothing to do with mutual exclusion."""
    await store.write_scheduler_heartbeat(
        pid=os.getpid(), host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(), ts=_time.time())
    first = await store.read_scheduler_heartbeat()
    assert first["pid"] == os.getpid()

    await store.write_scheduler_heartbeat(
        pid=os.getppid(), host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(), ts=_time.time())
    second = await store.read_scheduler_heartbeat()
    assert second["pid"] == os.getppid(), (
        "positive control failed: a second claim did not land, so this "
        "harness could not have caught one in the real test either")


# --------------------------------------------------------------------------- #
# AC4 — the retryable/permanent boundary is on the error, measured           #
# --------------------------------------------------------------------------- #


async def test_is_transient_db_error_accepts_a_measured_real_lock_collision(
    store,
):
    """Positive case, MEASURED rather than guessed: a genuine cross-
    connection write collision on this exact table raises
    `sqlite3.OperationalError` with one of `_TRANSIENT_DB_MESSAGES` — and
    that measured exception is exactly what `_is_transient_db_error`
    must accept."""
    await store.db.execute("PRAGMA busy_timeout = 30")

    acquired = threading.Event()
    release = threading.Event()
    holder = threading.Thread(
        target=_hold_write_lock, args=(store.path, acquired, release),
        daemon=True)
    holder.start()
    try:
        assert acquired.wait(timeout=2)
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            await store.write_scheduler_heartbeat(
                pid=os.getpid(), host=platform.node(),
                started_at=datetime.now(timezone.utc).isoformat(),
                ts=_time.time())
    finally:
        release.set()
        holder.join(timeout=2)

    exc = exc_info.value
    assert any(needle in str(exc).lower() for needle in _TRANSIENT_DB_MESSAGES), (
        f"measurement assumption changed — actual message: {exc!r}")
    assert _is_transient_db_error(exc) is True


def test_is_transient_db_error_rejects_a_different_operational_error():
    """Negative boundary, direction 1: same exception TYPE
    (`sqlite3.OperationalError`), a genuinely different cause (bad SQL, not
    lock contention) — must not be retried, or a real bug would be hidden
    behind silent retries."""
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            conn.execute("SELECT * FROM no_such_table")
    finally:
        conn.close()

    exc = exc_info.value
    assert "no such table" in str(exc).lower()
    assert _is_transient_db_error(exc) is False


def test_is_transient_db_error_rejects_a_different_exception_type():
    """Negative boundary, direction 2: the same MESSAGE text but the wrong
    exception TYPE must not be classified as transient — the check is
    typed, not a bare string match that any exception could spoof."""
    assert _is_transient_db_error(RuntimeError("database is locked")) is False
    assert _is_transient_db_error(
        sqlite3.IntegrityError("database is locked")) is False
