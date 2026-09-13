"""Task 92e48491 (refile — PR #251 closed on trunk conflicts after 7 landing
attempts, the underlying defect stayed live): `_claim_pool_lease`'s CAS
**write** leg had no retry budget at all, unlike the READ leg
(`_LEASE_READ_ATTEMPTS`, see `tests/test_scheduler_lease_fail_closed.py`). A
single transient `sqlite3.OperationalError: database is locked` on the write
raised `PoolLeaseLost` immediately, and nothing ever clears
`Scheduler._lease_lost` once set: `tick()` returns `[]` forever and
`run_forever` exits the loop, with no way back except a process restart.

The fix (`Scheduler._cas_heartbeat_with_retry`, `_is_transient_db_lock`):
bounded retry, exponential backoff, ONLY for a NARROWLY classified transient
lock — never a wider `OperationalError`/`DatabaseError` match, and never a
CAS that merely *returns* `False` (a row that moved is a real race, not a
glitch, and is never blindly retried). Every attempt forwards the exact same
`expect` row — no re-read mid-retry — so a competitor that legitimately
claims the row inside the retry window still results in
`SiblingSchedulerRunning` naming the live pid, not a clobber.

AC map:
  AC1 — a transient lock retried within budget still lands the claim, and
        (control, unfixed shape) a budget of exactly 1 attempt reproduces the
        original permanent wedge.
  AC2 — (a) a lock held through the WHOLE budget still fails closed
        (`PoolLeaseLost`); (b) any non-transient exception fails closed on
        attempt 1, no retry spent on a fault the same write will keep
        hitting.
  AC3 — a competitor that claims the row inside the retry window is never
        overwritten by the retried (stale-`expect`) write.
  (plus) a direct unit test of `_is_transient_db_lock`'s narrowness.

`tests/test_scheduler_lease_fail_closed.py` (read/write CAS races, per-tick
refresh failure) is run unedited alongside this file — nothing here
duplicates or modifies it.
"""

from __future__ import annotations

import asyncio
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
    _is_transient_db_lock,
)

# NOTE: no module-level `pytestmark = pytest.mark.asyncio` here (unlike
# `test_scheduler_lease_fail_closed.py`) — this file's asyncio_mode is
# "auto" (pyproject.toml), which detects every `async def test_*` on its
# own, and this file also has one deliberately SYNC test
# (`test_the_classifier_retries_only_a_lock_message`) that a blanket
# `pytestmark` would incorrectly tag, producing a PytestWarning.


class _NeverRunOrch:
    async def run_task(self, task):  # pragma: no cover - dispatch must not run
        raise AssertionError("dispatch must not run in these tests")


def _sched(store):
    return Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)


class _ForeignWriteLock:
    """A REAL cross-connection write lock on `path` — a second raw
    `sqlite3.connect` holding `BEGIN IMMEDIATE` in its own thread, not a
    mock. Any writer on a DIFFERENT connection (including the Store's own
    aiosqlite connection, with `busy_timeout` forced to 0 in these tests)
    raises `sqlite3.OperationalError: database is locked` immediately while
    this is held. `acquire()`/`release()` are synchronous and block until the
    lock is genuinely taken/released, so callers never race the thread that
    owns it.
    """

    def __init__(self, path):
        self._path = str(path)
        self._acquired = threading.Event()
        self._release = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        conn = sqlite3.connect(self._path, timeout=0)
        conn.execute("BEGIN IMMEDIATE")
        self._acquired.set()
        self._release.wait(timeout=5)
        conn.rollback()
        conn.close()
        self._done.set()

    def acquire(self):
        self._thread.start()
        assert self._acquired.wait(timeout=5), "failed to acquire the foreign write lock"

    def release(self):
        self._release.set()
        assert self._done.wait(timeout=5), "foreign write lock thread did not exit"


# --------------------------------------------------------------------------- #
# AC1 — a transient lock within budget still lands the claim                  #
# --------------------------------------------------------------------------- #


async def test_a_transient_lock_on_the_first_write_attempt_still_lands_the_claim(
    store, monkeypatch,
):
    """THE REPRO (fixed shape). Attempt 1's CAS write hits a real, held
    lock and raises; the lock is released during the backoff sleep, so
    attempt 2 lands the claim. Before this fix there was no attempt 2 —
    this exact scenario raised `PoolLeaseLost` on attempt 1."""
    monkeypatch.setattr(Scheduler, "_LEASE_WRITE_BACKOFF_S", 0.2)
    await store.db.execute("PRAGMA busy_timeout = 0")
    sched = _sched(store)

    calls = {"n": 0}
    # `test_scheduler.py::test_no_sleep_as_synchronisation_wait` bans a bare
    # `asyncio.sleep(...)` as a synchronisation wait in its own file (two
    # historical flakes); this file follows the same discipline even though
    # the lint does not reach here — the spy sets this the instant attempt
    # 1's real call has raised (still inside the `except` clause's `finally`,
    # before the retry loop's `await asyncio.sleep(backoff)` yields control
    # back to us), so waiting on it is exact, not a timing guess.
    attempt1_done = asyncio.Event()
    real_cas = store.cas_scheduler_heartbeat

    async def _counting_cas(**kw):
        calls["n"] += 1
        try:
            return await real_cas(**kw)
        finally:
            if calls["n"] == 1:
                attempt1_done.set()

    monkeypatch.setattr(store, "cas_scheduler_heartbeat", _counting_cas)

    lock = _ForeignWriteLock(store.path)
    lock.acquire()
    try:
        task = asyncio.ensure_future(sched._claim_pool_lease())
        await asyncio.wait_for(attempt1_done.wait(), timeout=5)
        assert calls["n"] == 1, "attempt 1 should already have hit the held lock"
    finally:
        lock.release()

    await asyncio.wait_for(task, timeout=5)  # must NOT raise

    assert calls["n"] == 2, "exactly one retry — the lock was gone well before it"
    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == os.getpid()


async def test_the_wedge_before_the_fix_one_lock_stops_every_later_tick(
    store, monkeypatch,
):
    """Control: force the retry budget down to 1 attempt (the unfixed write
    leg's actual shape — no budget at all). The SAME transient, genuinely
    momentary lock that test 1 rides out now permanently wedges: `tick()`'s
    per-tick refresh fails once and `_lease_lost` never clears, so every
    later tick is a strict no-op forever. This is what the bounded retry in
    the fixed code exists to prevent."""
    monkeypatch.setattr(Scheduler, "_LEASE_WRITE_ATTEMPTS", 1)
    await store.db.execute("PRAGMA busy_timeout = 0")
    sched = _sched(store)
    await sched._claim_pool_lease()  # startup claim, no contention yet
    assert sched._lease_lost is None

    lock = _ForeignWriteLock(store.path)
    lock.acquire()
    try:
        result = await sched.tick()  # per-tick refresh's CAS write hits the lock
    finally:
        lock.release()

    assert result == [], "tick() must not dispatch once the refresh fails"
    assert sched._lease_lost, "a lock with zero retry budget must be recorded as lost"

    # The lock is long gone, but nothing clears `_lease_lost` — the wedge
    # this whole fix exists to bound, not eliminate outright (restart is the
    # only way back, by design; see PLAN.md's explicit OUT OF SCOPE).
    result2 = await sched.tick()
    assert result2 == []


# --------------------------------------------------------------------------- #
# AC2 — exhausting the budget, or a non-transient error, fails closed         #
# --------------------------------------------------------------------------- #


async def test_a_lock_held_through_the_whole_budget_fails_closed(store, monkeypatch):
    monkeypatch.setattr(Scheduler, "_LEASE_WRITE_BACKOFF_S", 0.01)
    await store.db.execute("PRAGMA busy_timeout = 0")
    sched = _sched(store)
    await sched._claim_pool_lease()

    calls = {"n": 0}
    real_cas = store.cas_scheduler_heartbeat

    async def _counting_cas(**kw):
        calls["n"] += 1
        return await real_cas(**kw)

    monkeypatch.setattr(store, "cas_scheduler_heartbeat", _counting_cas)

    lock = _ForeignWriteLock(store.path)
    lock.acquire()
    try:
        with pytest.raises(PoolLeaseLost) as exc_info:
            await sched._claim_pool_lease()
    finally:
        lock.release()

    assert calls["n"] == Scheduler._LEASE_WRITE_ATTEMPTS, (
        "every attempt in the budget must have been spent — none skipped, "
        "none left over")
    assert "locked" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.OperationalError("no such table: scheduler_heartbeat"),
        sqlite3.OperationalError("disk I/O error"),
        ValueError("not a lock at all"),
        sqlite3.DatabaseError("database disk image is malformed"),
    ],
    ids=["wrong-table", "disk-io-error", "value-error", "database-error-not-operational"],
)
async def test_a_non_transient_write_error_fails_closed_on_the_first_attempt(
    store, monkeypatch, exc,
):
    sched = _sched(store)
    await sched._claim_pool_lease()

    calls = {"n": 0}

    async def _boom(**kw):
        calls["n"] += 1
        raise exc

    monkeypatch.setattr(store, "cas_scheduler_heartbeat", _boom)

    with pytest.raises(PoolLeaseLost):
        await sched._claim_pool_lease()

    assert calls["n"] == 1, (
        "a fault the same write will keep hitting must not spend any of the "
        "retry budget — retrying it only delays an honest failure")


def _locked(code=None):
    """A `sqlite3.OperationalError("database is locked")`, optionally with a
    manually-set `sqlite_errorcode` — real exceptions carry this attribute
    (Python 3.11+) but a synthetic one built by `sqlite3.OperationalError(...)`
    does not, so tests that want to exercise the code-based branch of
    `_is_transient_db_lock` have to set it explicitly, same as a real
    SQLITE_BUSY/SQLITE_BUSY_SNAPSHOT exception would arrive with it already
    set."""
    exc = sqlite3.OperationalError("database is locked")
    if code is not None:
        exc.sqlite_errorcode = code
    return exc


@pytest.mark.parametrize(
    "exc,expected",
    [
        (sqlite3.OperationalError("database is locked"), True),
        (sqlite3.OperationalError("database is locked (context: ...)"), True),
        (sqlite3.OperationalError("DATABASE IS LOCKED"), True),
        (sqlite3.OperationalError("no such table: scheduler_heartbeat"), False),
        (sqlite3.OperationalError("disk I/O error"), False),
        (sqlite3.DatabaseError("database is locked"), False),
        (ValueError("database is locked"), False),
        (OSError("locked"), False),
        # F4 (review round on this refile): "database is locked" alone cannot
        # tell SQLITE_BUSY (5, transient — a peer merely holds the write lock
        # right now) from SQLITE_BUSY_SNAPSHOT (517, "deterministic, and
        # permanent until the statement is reset" per core/db.py) — same
        # exception, same message. When the exception carries a real
        # `sqlite_errorcode`, only 5 is retried; 517 (and any other code)
        # fails closed even though the message names a lock.
        (_locked(code=sqlite3.SQLITE_BUSY), True),
        (_locked(code=sqlite3.SQLITE_BUSY_SNAPSHOT), False),
        (_locked(code=9999), False),
        # No error code at all (older bindings, or — as in every other case
        # in this parametrize list — a synthetic test exception): falls back
        # to the message match alone, same as before this distinction existed.
        (_locked(code=None), True),
    ],
    ids=[
        "operational-locked",
        "operational-locked-with-context",
        "operational-locked-case-insensitive",
        "operational-wrong-table",
        "operational-disk-io",
        "databaseerror-not-operationalerror",
        "valueerror-not-operationalerror",
        "oserror-not-operationalerror",
        "sqlite-busy-code-retried",
        "sqlite-busy-snapshot-code-fails-closed",
        "unrecognised-code-fails-closed",
        "no-code-falls-back-to-message-match",
    ],
)
def test_the_classifier_retries_only_a_lock_message(exc, expected):
    assert _is_transient_db_lock(exc) is expected


# --------------------------------------------------------------------------- #
# AC3 — a competitor that claims the row inside the retry window still wins   #
# --------------------------------------------------------------------------- #


async def test_a_competitor_that_claims_the_row_inside_the_retry_window_still_stops_us(
    store, monkeypatch,
):
    """The load-bearing invariant: every retried attempt forwards the SAME
    `expect` (no re-read). Attempt 1 hits a real held lock; while the retry
    backoff sleeps, a genuinely different, live process claims the
    (still-empty) row for real, through the same CAS entrypoint a real
    sibling would use. The retried attempt 2 then carries a stale `expect`
    against a row that has since moved — it must return `False`, never
    overwrite the competitor — and `_claim_pool_lease`'s existing
    re-read-once step is what turns that into `SiblingSchedulerRunning`
    naming the competitor's pid."""
    monkeypatch.setattr(Scheduler, "_LEASE_WRITE_BACKOFF_S", 0.2)
    await store.db.execute("PRAGMA busy_timeout = 0")
    sched = _sched(store)
    # No heartbeat row exists yet — this claim's `expect` will be None.

    calls = {"n": 0}
    # See the AC1 test above for why an Event, not a sleep, marks "attempt 1
    # has already raised": it is set the instant the spy's real call returns
    # control (raising), strictly before the retry loop's own
    # `await asyncio.sleep(backoff)` yields back to us.
    attempt1_done = asyncio.Event()
    real_cas = store.cas_scheduler_heartbeat

    async def _counting_cas(**kw):
        calls["n"] += 1
        try:
            return await real_cas(**kw)
        finally:
            if calls["n"] == 1:
                attempt1_done.set()

    monkeypatch.setattr(store, "cas_scheduler_heartbeat", _counting_cas)

    lock = _ForeignWriteLock(store.path)
    lock.acquire()

    # Must be a REAL, alive, foreign pid — `pid_alive` would otherwise treat
    # a made-up pid as dead and the takeover branch, not the sibling branch,
    # would fire.
    competitor_pid = os.getppid()

    task = asyncio.ensure_future(sched._claim_pool_lease())
    # attempt 1 has hit the lock and raised; it is now in its 0.2s backoff sleep
    await asyncio.wait_for(attempt1_done.wait(), timeout=5)
    lock.release()

    landed = await real_cas(
        pid=competitor_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(),
        ts=_time.time(), expect=None)
    assert landed, "the competitor's own claim, against the still-empty row, must land"

    with pytest.raises(SiblingSchedulerRunning) as exc_info:
        await asyncio.wait_for(task, timeout=5)

    assert str(competitor_pid) in str(exc_info.value)
    assert calls["n"] == 2, (
        "attempt 1 (the held lock) + retried attempt 2 (stale expect, row "
        "now stolen) — no 3rd attempt, a False return is never retried")

    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == competitor_pid, (
        "the retried write must never overwrite a competitor's legitimately "
        "claimed row — it forwards the SAME stale `expect`, so it cannot land")
