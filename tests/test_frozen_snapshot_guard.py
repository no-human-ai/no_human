"""The frozen-read-snapshot wedge of 2026-08-01, and the three guards for it.

THE INCIDENT. The server's shared SQLite connection served a read snapshot
pinned to a window three hours in the past. Rows written after the pin were
invisible to it, so `Scheduler._claimable` — which reads through that
connection — kept returning two long-escalated tasks and never saw the one real
task waiting. Dogfooding produced nothing for six hours while
`/api/worker/status` returned `{"running":true,"inflight":0,...}` throughout.

What proved it, so this file does not depend on a document to be read: the
server answered 404 for a task id that a read-only `sqlite3` session could see
in the file; the server counted 263 tasks where the file held 264; and the
three rows it was wrong about were exactly the three written after the instant
its view stopped moving. Meanwhile the WAL grew without being checkpointed,
which is what an oldest-active-reader that never advances causes.

WHY DETECTION IS THE LOAD-BEARING TEST HERE. The rollback covered below removes
one way to pin a connection, and the incident's own timeline argues it was not
the way that happened: the process was pinned three hours before its own first
read. So the guard that matters is the one that does not care about the cause —
compare the shared connection against an independent one and disbelieve the
shared one when they disagree. That is what most of this file exercises, and
every guard here is asserted with BOTH a known positive and a known negative,
because a probe that never fires and a probe that always fires both look like
"no problem found".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from no_human.core.db import SnapshotMarker, Store
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(store_factory):
    # Variant: `peer` below opens a SECOND connection to the exact same file,
    # so the filename is load-bearing and shared between fixtures.
    return await store_factory("f.db")


@pytest.fixture
async def peer(store_factory, store):
    """An independent connection to the same file — every `nh` CLI call, the
    Jira poller and the Linear poller are one of these in real life."""
    return await store_factory("f.db")


async def _mk(store: Store, title: str = "t") -> Task:
    t = Task.new(title, repo_path="/tmp/r")
    await store.create_task(t)
    return t


_PIN_GUARD_ROWS = 8


async def _pin(store: Store) -> None:
    """Freeze `store`'s read view, the way an unreset statement does.

    Fetching ONE row of MANY leaves the statement unreset, which holds this
    connection's read transaction open indefinitely — see the measured table in
    `db.py`'s read-helper comment. Two traps that made earlier drafts of this
    helper silently pin NOTHING, which would have made every "known positive"
    below vacuous:

    * **the table must hold more rows than we fetch.** CPython's `sqlite3` steps
      once inside `execute()` and once inside the fetch, so on a 1-row table the
      statement reaches SQLITE_DONE and RESETS ITSELF. `db.py` says this ("why
      it has to seed MORE rows than it fetches") and this helper still got it
      wrong first time. Hence the guard rows.
    * **the cursor must stay referenced.** Garbage-collecting it resets the
      statement and un-pins the connection mid-test.

    The pin is asserted, not assumed, before returning. The verification uses an
    UPDATE rather than an INSERT so that pinning does not itself change the row
    COUNT — the update-only test below needs the two connections to agree on the
    count and disagree only on `max(updated_at)`.
    """
    guards = [await _mk(store, f"pin-guard-{i}") for i in range(_PIN_GUARD_ROWS)]
    cur = await store.db.execute("SELECT * FROM tasks")
    await cur.fetchone()
    store._test_pin = cur          # keep alive; a GC'd cursor resets it

    victim = guards[0]
    seen_before = (await store.get_task(victim.id)).updated_at
    verifier = await Store(store.path).connect()
    try:
        await verifier.set_status(victim, TaskStatus.ESCALATED, validate=False)
        assert (await verifier.get_task(victim.id)).updated_at != seen_before, (
            "control: the verifying peer's own write is visible to itself")
        assert (await store.get_task(victim.id)).updated_at == seen_before, (
            "the pin did not take — this helper is not reproducing the wedge, "
            "so every assertion built on it would be vacuous")
    finally:
        await verifier.close()


# --------------------------------------------------------------------------- #
# 1. The probe: known positive, known negative, and the race it must not call.
# --------------------------------------------------------------------------- #

async def test_probe_is_clean_on_a_fresh_connection(store, peer):
    """KNOWN NEGATIVE. Must not fire on a healthy connection, including right
    after a peer commits — otherwise it would reconnect the pool constantly."""
    await _mk(store, "a")
    assert (await store.probe_snapshot_staleness()).stale is False

    await _mk(peer, "written by the peer")
    probe = await store.probe_snapshot_staleness()
    assert probe.stale is False, probe
    # ...and the healthy connection genuinely saw the peer's row; the negative
    # is not an artefact of the probe failing to look.
    assert probe.shared.count == probe.fresh.count == 2


async def test_probe_detects_a_pinned_connection(store, peer):
    """KNOWN POSITIVE. The reproduction of the incident, in-process."""
    await _pin(store)
    frozen_at = len(await store.list_tasks())
    await _mk(peer, "after the pin")

    # The wedge itself, asserted before the probe so a broken probe cannot pass
    # by accident: the row is real, and the pinned connection cannot see it.
    assert len(await peer.list_tasks()) == frozen_at + 1
    assert len(await store.list_tasks()) == frozen_at

    probe = await store.probe_snapshot_staleness()
    assert probe.stale is True, probe
    assert probe.reason == "frozen-snapshot"
    assert probe.shared.count == frozen_at
    assert probe.fresh.count == frozen_at + 1
    assert store.stale_detections == 1
    assert store.last_stale_at is not None


async def test_probe_detects_an_update_only_pin(store, peer):
    """A pin that changes no ROW COUNT — the shape the incident's first two
    symptoms took (two tasks escalating). A count-only probe would miss it.

    `_pin` ends on exactly this state: its verifying peer UPDATES a row rather
    than inserting one, so no row count moves and the only divergence is
    `max(updated_at)`.
    """
    await _pin(store)

    probe = await store.probe_snapshot_staleness()
    assert probe.stale is True, probe
    assert probe.shared.count == probe.fresh.count            # counts agree...
    assert probe.shared.max_updated_at < probe.fresh.max_updated_at  # ...times don't


async def test_probe_does_not_fire_on_a_concurrent_write_race(store, peer,
                                                              monkeypatch):
    """KNOWN NEGATIVE for the probe's own false-positive mode.

    The shared read and the fresh read cannot be simultaneous, so a peer
    committing between them leaves the fresh marker legitimately ahead. Acting
    on that would churn the connection under ordinary load. The discriminator is
    the confirming re-read: a healthy connection catches up immediately, a
    pinned one never can. Here the FIRST shared read is forced to look behind
    and the re-read is allowed to be truthful.
    """
    await _mk(store, "a")
    await _mk(peer, "b")

    real = store._shared_marker
    calls = {"n": 0}

    async def flaky() -> SnapshotMarker:
        calls["n"] += 1
        if calls["n"] == 1:
            return SnapshotMarker(0, None)      # as if mid-race
        return await real()

    monkeypatch.setattr(store, "_shared_marker", flaky)
    probe = await store.probe_snapshot_staleness()
    assert probe.stale is False, probe
    assert probe.reason == "concurrent-write-race"
    assert calls["n"] == 2, "the confirming re-read is what makes this safe"
    assert store.stale_detections == 0


async def test_read_file_marker_cannot_write_to_the_database(store, monkeypatch):
    """The probe opens a second connection to the database it is auditing —
    against the OPERATOR's live file — so it must not be able to modify it.

    An earlier version of this test was a tautology: it never called the
    product's code at all, it built its own connection, applied `query_only`
    itself and asserted SQLite's behaviour. Deleting the pragma from
    `read_file_marker` left it passing. So the write is attempted here on the
    REAL connection that `read_file_marker` really opened, at the moment it is
    really using it — which is the only arrangement that can fail when the
    pragma is removed.
    """
    from no_human.core.db import read_file_marker

    outcome: list[str] = []
    real_connect = sqlite3.connect

    class _Intercept:
        """Passes everything through, but when the function runs its SELECT —
        i.e. after it has applied whatever pragmas it applies — tries a write
        on that same live connection."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **k):
            if sql.strip().upper().startswith("SELECT") and not outcome:
                try:
                    self._conn.execute(
                        "INSERT INTO tasks (id, source, title, status) "
                        "VALUES ('probe-write','t','t','pending')")
                    self._conn.commit()
                    outcome.append("WROTE")
                except sqlite3.OperationalError as exc:
                    outcome.append(f"blocked: {exc}")
            return self._conn.execute(sql, *a, **k)

        def close(self):
            self._conn.close()

    from no_human.core import db as db_mod
    monkeypatch.setattr(db_mod, "_sqlite_connect",
                        lambda *a, **k: _Intercept(real_connect(*a, **k)))
    marker = read_file_marker(store.path)

    assert outcome, "control: the interceptor never saw the function's SELECT"
    assert outcome[0].startswith("blocked:"), (
        f"read_file_marker's connection accepted a WRITE ({outcome[0]}) — the "
        "probe can modify the database it audits")
    assert marker.count >= 0, "and the function still returned its marker"
    assert sqlite3.connect is real_connect, (
        "the test patched the GLOBAL sqlite3.connect — that corrupts coverage.py's "
        "own connection and cascades into every later test")

    # Known negative: with the pragma absent the same interception DOES write,
    # so the assertion above is measuring the pragma and not something else.
    plain = real_connect(str(store.path))
    try:
        plain.execute("INSERT INTO tasks (id, source, title, status) "
                      "VALUES ('control-write','t','t','pending')")
        plain.commit()
    finally:
        plain.close()


async def test_reconnect_clears_the_wedge(store, peer):
    await _pin(store)
    frozen_at = len(await store.list_tasks())
    await _mk(peer, "after")
    assert (await store.probe_snapshot_staleness()).stale is True

    await store.reconnect()

    assert len(await store.list_tasks()) == frozen_at + 1
    assert (await store.probe_snapshot_staleness()).stale is False
    assert store.reconnects == 1
    assert store.last_reconnect_at is not None
    # and the connection is writable again, which is the point
    await _mk(store, "after the reconnect")
    assert len(await peer.list_tasks()) == frozen_at + 2


# --------------------------------------------------------------------------- #
# 2. The missing rollback.
# --------------------------------------------------------------------------- #

def test_db_module_has_a_rollback_at_all():
    """`grep -n rollback src/no_human/core/db.py` returned NOTHING before this
    fix. `journal_mode` is the known positive proving the search works."""
    from no_human.core import db as db_mod
    src = __import__("pathlib").Path(db_mod.__file__).read_text()
    assert "journal_mode" in src, "control: the file was read at all"
    assert "rollback" in src


async def test_a_failed_write_does_not_pin_the_connection(store, peer):
    """THE REGRESSION. One exception between `execute()` and `commit()` used to
    leave an open transaction that nothing could ever end."""
    t = await _mk(store, "a")

    boom = RuntimeError("failure between execute() and commit()")
    real_execute = store.db.execute
    state = {"armed": True}

    async def exploding(sql, *a, **k):
        cur = await real_execute(sql, *a, **k)
        if state["armed"] and sql.strip().upper().startswith("UPDATE"):
            state["armed"] = False       # blow up AFTER the implicit BEGIN
            raise boom
        return cur

    store.db.execute = exploding
    with pytest.raises(RuntimeError):
        await store.set_status(t, TaskStatus.ESCALATED, validate=False)
    store.db.execute = real_execute

    # Before the fix both of these failed: the connection was inside an open
    # transaction forever, so its reads were frozen and its writes raised
    # `database is locked`.
    await _mk(peer, "committed by a peer after the failure")
    assert (await store.probe_snapshot_staleness()).stale is False
    assert len(await store.list_tasks()) == 2, "reads are not frozen"
    await _mk(store, "the connection can still write")
    assert len(await peer.list_tasks()) == 3


async def test_rollback_does_NOT_clear_a_read_pin(store, peer):
    """The limit of guard 2, measured — and it is why guard 1 exists.

    A first draft of `serialized_write`'s comment claimed `rollback()` resets
    every statement on the connection and so ends a read pin as well as a write
    one. It does not, on CPython 3.12 + aiosqlite. That claim would have made
    the rollback look like a complete fix and the staleness probe look like
    belt-and-braces, when the truth is the other way round: the incident's own
    timeline points at a pin this rollback cannot reach.

    Pinned here so the claim cannot quietly return. If a future runtime DOES
    reset on rollback this test fails loudly, which is the correct outcome —
    the comment would then need updating too.
    """
    await _pin(store)
    frozen_at = len(await store.list_tasks())
    await _mk(peer, "committed after the pin")
    assert len(await peer.list_tasks()) == frozen_at + 1     # control

    await store._rollback_quietly()

    assert len(await store.list_tasks()) == frozen_at, (
        "rollback() cleared a READ pin — the comment in serialized_write says "
        "it cannot, and one of the two is now wrong")
    # The probe, by contrast, does not care how the connection got pinned.
    assert (await store.probe_snapshot_staleness()).stale is True


async def test_cancellation_during_the_rollback_still_cancels_the_task(store):
    """F4. `_rollback_quietly` caught `BaseException`, and `await
    db.rollback()` is a suspension point — so a cancellation ARRIVING DURING
    the rollback was swallowed and the task finished with the original
    exception and `task.cancelled() == False`. A cancelled task that does not
    report as cancelled is a lie to every caller that awaits it.

    `serialized_write` reasons carefully about `CancelledError` in the OUTER
    handler; the inner one quietly undid it.
    """
    t = await _mk(store, "a")
    started = asyncio.Event()

    async def rollback_that_gets_cancelled():
        started.set()
        await asyncio.sleep(30)          # cancellation lands in here
    store._db.rollback = rollback_that_gets_cancelled

    real_execute = store.db.execute

    async def exploding(sql, *a, **k):
        cur = await real_execute(sql, *a, **k)
        if sql.strip().upper().startswith("UPDATE"):
            raise RuntimeError("ORIGINAL")
        return cur
    store.db.execute = exploding

    task = asyncio.create_task(
        store.set_status(t, TaskStatus.ESCALATED, validate=False))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() is True, (
        "the task was cancelled but does not report as cancelled — the "
        "CancelledError was swallowed inside _rollback_quietly")


async def test_an_ordinary_rollback_failure_is_still_swallowed(store):
    """KNOWN NEGATIVE for the fix above: only `CancelledError` propagates. A
    plain rollback failure must still not replace the original exception, or
    the caller loses the error that actually mattered."""
    t = await _mk(store, "a")

    async def rollback_that_fails():
        raise sqlite3.OperationalError("rollback failed too")
    store._db.rollback = rollback_that_fails

    real_execute = store.db.execute

    async def exploding(sql, *a, **k):
        cur = await real_execute(sql, *a, **k)
        if sql.strip().upper().startswith("UPDATE"):
            raise RuntimeError("ORIGINAL")
        return cur
    store.db.execute = exploding

    with pytest.raises(RuntimeError, match="ORIGINAL"):
        await store.set_status(t, TaskStatus.ESCALATED, validate=False)


async def test_successful_writes_stamp_last_successful_write_at(store):
    """KNOWN NEGATIVE for the liveness stamp: it must actually MOVE, or
    `last_successful_write_at` is a field that freezes at connect time and
    reads healthy forever. (`connect()` writes — it runs the migrations — so it
    is already non-None here; that is why this asserts movement, not presence.)"""
    assert store.last_successful_write_at is not None   # stamped by _migrate
    first = store.last_successful_write_at
    await asyncio.sleep(0.01)
    await _mk(store, "a")
    assert store.last_successful_write_at > first


# --------------------------------------------------------------------------- #
# 3. The scheduler: detect, recover, and stop swallowing the fallback write.
# --------------------------------------------------------------------------- #

class _Orch:
    def __init__(self, task=None, **kw):
        self.task = task
        self._sink = None

    async def run_task(self, task):
        raise RuntimeError("the task crashed")


def _sched(store, **kw) -> Scheduler:
    return Scheduler(store, _Orch, max_workers=2, **kw)


async def test_tick_detects_and_recovers_a_frozen_view(store, peer, caplog):
    """KNOWN POSITIVE, end to end: a pinned pool connection recovers itself on
    the next tick, and the newly visible task becomes claimable."""
    await _pin(store)
    # Everything created before the pin is PENDING and therefore claimable; take
    # them out of the way so the assertion below is about the HIDDEN task only.
    for t in await store.list_tasks(TaskStatus.PENDING):
        await peer.set_status(t, TaskStatus.DONE, validate=False,
                              event={"source": "test", "kind": "test_seed"})
    hidden = await _mk(peer, "invisible to the pinned pool")

    sched = _sched(store)
    stale_view = await sched._claimable()
    assert hidden.id not in {t.id for t in stale_view}, (
        "control: the starved task is invisible through the pinned connection")

    with caplog.at_level(logging.ERROR, logger="no_human.scheduler"):
        started = await sched.tick()

    assert sched.store.reconnects == 1
    assert "FROZEN" in caplog.text
    assert sched._db_view_stale is False, "recovered, so the flag comes down"
    assert started == [hidden.id], "the starved task finally dispatched"


async def test_tick_does_not_reconnect_a_healthy_view(store, peer):
    """KNOWN NEGATIVE. Peers commit constantly; reconnecting on that would
    replace the pool's connection on every tick."""
    await _mk(peer, "a peer wrote this")
    sched = _sched(store)
    for _ in range(3):
        await sched.tick()
    assert store.reconnects == 0
    assert sched._db_view_stale is False
    assert sched.health_snapshot()["db_view_stale"] is False


async def test_a_probe_failure_never_kills_the_tick(store, monkeypatch):
    async def broken():
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(store, "probe_snapshot_staleness", broken)
    sched = _sched(store)
    assert await sched.tick() == []          # no exception escapes


async def test_a_failing_probe_is_counted_and_reported(store, monkeypatch,
                                                       caplog):
    """F2. A DETECTOR THAT FAILS OPEN IS THE INCIDENT'S OWN SHAPE.

    The first version caught the probe's exception and returned. `db_view_stale`
    stayed False, `idle_reason` stayed `queue_empty`, `healthy` stayed true —
    byte-identical to health — and the only trace was a log line, back in the
    46,000 lines this change exists to make unnecessary. Surviving the failure
    was tested; REPORTING it was not.
    """
    async def broken():
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(store, "probe_snapshot_staleness", broken)
    sched = _sched(store)

    with caplog.at_level(logging.ERROR, logger="no_human.scheduler"):
        await sched.tick()
        await sched.tick()

    snap = sched.health_snapshot()
    assert snap["probe_failures"] == 2
    assert snap["consecutive_probe_failures"] == 2
    assert "disk I/O error" in snap["last_probe_error"]
    # "unknown" must not resolve to "healthy" — that is the fail-open.
    assert snap["idle_reason"] == "db_probe_failing"
    assert "probe FAILED" in caplog.text


async def test_a_recovered_probe_clears_the_failure_state(store, monkeypatch):
    """KNOWN NEGATIVE: the counter must fall again, or it alarms forever."""
    calls = {"n": 0}
    real = store.probe_snapshot_staleness

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("transient")
        return await real()

    monkeypatch.setattr(store, "probe_snapshot_staleness", flaky)
    sched = _sched(store)
    await sched.tick()
    assert sched.health_snapshot()["consecutive_probe_failures"] == 1
    await sched.tick()
    snap = sched.health_snapshot()
    assert snap["consecutive_probe_failures"] == 0
    assert snap["probe_failures"] == 1, "the lifetime total still remembers"
    assert snap["idle_reason"] != "db_probe_failing"


async def test_a_stalled_tick_loop_is_not_healthy(store):
    """F1. Every other field here is WRITTEN by a tick, so a loop that has
    stopped ticking freezes them all in their last-known-good state.

    `seconds_since_last_tick` was published for exactly this and had ZERO
    consumers — not in `healthy`, not in `idle_reason`. A field nobody reads
    surfaces nothing, so the state resolved to the pre-incident reading:
    `queue_empty` / `healthy: true`.
    """
    sched = _sched(store)
    await sched.tick()
    fresh = sched.health_snapshot()
    assert fresh["tick_stalled"] is False            # known negative
    assert fresh["idle_reason"] == "queue_empty"

    # Three hours since the last tick, with a real task waiting.
    await _mk(store, "waiting the whole time")
    sched._last_tick_at = time.time() - 3 * 3600
    snap = sched.health_snapshot()
    assert snap["tick_stalled"] is True
    assert snap["idle_reason"] == "tick_loop_stalled"
    assert snap["seconds_since_last_tick"] > 10000


async def test_a_loop_that_never_ticked_is_not_healthy_forever(store):
    """FINDING A. `since_tick` is None before the first tick, so the
    `> threshold` comparison was never evaluated and `tick_stalled` stayed
    False — permanently. The `starting` branch existed and simply was not
    carried into `healthy`.

    Reachable by this incident's own mechanism: `run_forever` awaits
    `_recover_orphans()` before its loop with no try/except, and that method
    WRITES. A connection wedged at startup fails those writes and kills the
    loop before tick 1.
    """
    await _mk(store, "waiting, and nothing will ever pick it up")
    sched = _sched(store)
    assert sched._last_tick_at is None

    # BOUNDED: a server four seconds old is starting, not broken.
    fresh = sched.health_snapshot()
    assert fresh["idle_reason"] == "starting"
    assert fresh["never_ticked"] is False
    assert fresh["tick_stalled"] is False

    # ...but one that has had longer than its own threshold has not started.
    sched._created_at = time.time() - 3 * 3600
    snap = sched.health_snapshot()
    assert snap["never_ticked"] is True
    assert snap["tick_stalled"] is True, "must reach `healthy` via this flag"
    assert snap["idle_reason"] == "never_ticked"
    assert snap["seconds_since_last_tick"] is None
    assert snap["seconds_since_start"] > 10000


async def test_never_ticked_is_bounded_by_the_configured_interval(store):
    """KNOWN NEGATIVE for the bound: it must scale with `poll_interval`, or a
    slow-starting server on a long interval alarms on every boot."""
    sched = _sched(store)
    sched._poll_interval = 300.0                 # threshold 1800s
    sched._created_at = time.time() - 600        # 2 intervals: still starting
    assert sched.health_snapshot()["idle_reason"] == "starting"
    sched._created_at = time.time() - 3600       # 12 intervals: never started
    assert sched.health_snapshot()["idle_reason"] == "never_ticked"


async def test_the_stall_threshold_follows_the_configured_poll_interval(store):
    """The threshold is derived, not a constant, so it stays right under any
    `poll_interval` — and is generous enough not to fire on a slow tick."""
    sched = _sched(store)
    await sched.tick()
    assert sched.health_snapshot()["tick_stall_threshold_s"] == 60.0

    sched._poll_interval = 300.0            # what run_forever would record
    assert sched.health_snapshot()["tick_stall_threshold_s"] == 1800.0
    sched._last_tick_at = time.time() - 600      # 2 intervals: NOT stalled
    assert sched.health_snapshot()["tick_stalled"] is False
    sched._last_tick_at = time.time() - 3600     # 12 intervals: stalled
    assert sched.health_snapshot()["tick_stalled"] is True


async def test_run_forever_records_the_interval_it_was_given(store):
    """Otherwise the threshold silently uses the default however the server is
    configured, and the derivation above is decorative."""
    sched = _sched(store)
    stop = asyncio.Event()
    stop.set()                     # one pass, then exit
    await sched.run_forever(stop=stop, poll_interval=42.0)
    assert sched._poll_interval == 42.0
    assert sched.health_snapshot()["tick_stall_threshold_s"] == 252.0


async def test_still_frozen_after_reconnect_keeps_the_alarm_up(store,
                                                               monkeypatch,
                                                               caplog):
    """If reconnecting does not help, the flag must NOT come down. Clearing it
    would recreate the false all-clear this change exists to remove."""
    from no_human.core.db import StalenessProbe
    always_stale = StalenessProbe(
        True, SnapshotMarker(1, "a"), SnapshotMarker(9, "z"), None,
        "frozen-snapshot")
    monkeypatch.setattr(store, "probe_snapshot_staleness",
                        lambda: asyncio.sleep(0, result=always_stale))
    sched = _sched(store)
    with caplog.at_level(logging.ERROR, logger="no_human.scheduler"):
        await sched.tick()
    assert sched._db_view_stale is True
    assert "STILL FROZEN" in caplog.text


async def test_a_failing_status_write_is_counted_and_logged(store, caplog):
    """`except Exception: pass` with no counter is what made the wedge silent.

    Deliberately NOT a retry cap: during the incident the scheduler was correct
    on the data it could see, so capping retries would have hidden the fault.
    """
    t = await _mk(store, "crashes")
    sched = _sched(store)

    async def refuse(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    store.set_status = refuse

    with caplog.at_level(logging.ERROR, logger="no_human.scheduler"):
        await sched._run(t)

    assert sched._status_write_failures == 1
    assert sched._consecutive_status_write_failures == 1
    assert "database is locked" in sched._last_status_write_error
    assert "WILL be re-dispatched" in caplog.text
    assert sched.health_snapshot()["consecutive_status_write_failures"] == 1


async def test_a_recovered_status_write_resets_the_consecutive_counter(store):
    """KNOWN NEGATIVE: the counter must fall again, or it only ever alarms."""
    t = await _mk(store, "crashes")
    sched = _sched(store)
    sched._consecutive_status_write_failures = 5
    await sched._run(t)                       # crashes, but set_status works
    assert sched._consecutive_status_write_failures == 0
    assert sched._status_write_failures == 0


async def test_crashes_are_counted_in_a_time_window(store):
    t = await _mk(store, "crashes")
    sched = _sched(store)
    assert sched.health_snapshot()["crashes_last_5m"] == 0    # known negative
    await sched._run(t)
    assert sched.health_snapshot()["crashes_last_5m"] == 1    # known positive


# --------------------------------------------------------------------------- #
# 4. /api/worker/status can finally tell idle from wedged.
# --------------------------------------------------------------------------- #

async def test_health_snapshot_says_queue_empty_when_it_is(store):
    sched = _sched(store)
    await sched.tick()
    snap = sched.health_snapshot()
    assert snap["idle_reason"] == "queue_empty"
    assert snap["db_view_stale"] is False
    assert snap["claimable"] == 0


async def test_health_snapshot_says_db_view_stale_not_queue_empty(store):
    """THE BIT THAT WAS MISSING. Both states have `inflight: 0`; only this
    field separates them — and `db_view_stale` must WIN over `queue_empty`,
    because the queue looks empty precisely because the view is frozen."""
    sched = _sched(store)
    sched._last_tick_at = time.time()
    sched._db_view_stale = True
    sched._last_claimable_count = 0
    snap = sched.health_snapshot()
    assert snap["idle_reason"] == "db_view_stale"
    assert snap["db_view_stale"] is True


async def test_the_stale_flag_is_only_as_fresh_as_the_last_tick(store, peer):
    """The measured limit of this design, pinned so nobody discovers it during
    the next incident.

    The probe runs per TICK, not per request. A poll landing between the wedge
    and the next tick therefore still answers healthy — which a live run over
    real HTTP confirmed. `seconds_since_last_tick` is what makes that window
    visible instead of invisible.
    """
    sched = _sched(store)
    await sched.tick()
    await _pin(store)
    await _mk(peer, "written after the pin")

    # Wedged, but not yet ticked: the flag has not moved. This is the honest
    # blind spot, and it is bounded by one poll_interval.
    assert sched.health_snapshot()["db_view_stale"] is False
    assert sched.health_snapshot()["seconds_since_last_tick"] is not None

    await sched.tick()          # ...and one tick is all it takes
    assert store.reconnects == 1


# --- FINDING B: the precedence argument, enforced instead of asserted ------ #
#
# Every ranking below was argued at length in a comment and NOTHING TESTED IT:
# moving `db_view_stale` above `tick_stalled`, or `db_probe_failing` above
# `db_view_stale`, left all 31 tests passing. A reasoning-only ordering is free
# to regress silently, so each adjacent pair now has a case that constructs BOTH
# conditions at once and asserts which one wins.
#
# The rule the order encodes: faults outrank ordinary operating states (a fault
# reported as `slots_full` or `queue_empty` is this whole change's defect), and
# among faults, the one that invalidates the others' evidence outranks them.

def _rig(store, **flags):
    """A Scheduler with an arbitrary combination of conditions switched on."""
    sched = _sched(store)
    sched._last_tick_at = time.time()
    sched._created_at = time.time()
    if flags.get("never_ticked"):
        sched._last_tick_at = None
        sched._created_at = time.time() - 3 * 3600
    if flags.get("tick_stalled"):
        sched._last_tick_at = time.time() - 3 * 3600
    if flags.get("db_view_stale"):
        sched._db_view_stale = True
    if flags.get("probe_failing"):
        sched._consecutive_probe_failures = 3
    if flags.get("slots_full"):
        sched._inflight = set(f"t{i}" for i in range(sched.max_workers))
    if flags.get("quota"):
        sched._quota_cooldown_until = (datetime.now(timezone.utc)
                                       + timedelta(hours=1))
    if flags.get("claimable"):
        sched._last_claimable_count = 5
    return sched


async def test_never_ticked_and_tick_stalled_are_mutually_exclusive(store):
    """NOT a precedence pair, and saying so is the point.

    A first draft of this file asserted `never_ticked` outranks
    `tick_loop_stalled`. It cannot: both are read from the SAME variable
    (`_last_tick_at` is either None or it is not), so no scheduler can be in
    both states and the ordering between them is unreachable. Pinning an
    ordering that cannot occur would be a test that can never fail — which is
    the tautology class this review round already caught once.

    Both must still reach `healthy` through `tick_stalled`, and that is what is
    asserted instead.
    """
    never = _rig(store, never_ticked=True).health_snapshot()
    stalled = _rig(store, tick_stalled=True).health_snapshot()
    assert never["idle_reason"] == "never_ticked"
    assert stalled["idle_reason"] == "tick_loop_stalled"
    assert never["never_ticked"] is True and stalled["never_ticked"] is False
    assert never["tick_stalled"] is stalled["tick_stalled"] is True


async def test_tick_stalled_outranks_db_view_stale(store):
    """`db_view_stale` is WRITTEN by a tick. If the loop stopped ticking, that
    flag is a frozen reading, not current evidence — so the stall is the
    honest headline and the reviewer confirmed this ranking."""
    s = _rig(store, tick_stalled=True, db_view_stale=True)
    assert s.health_snapshot()["idle_reason"] == "tick_loop_stalled"


async def test_db_view_stale_outranks_db_probe_failing(store):
    """A confirmed observation outranks an absence of information."""
    s = _rig(store, db_view_stale=True, probe_failing=True)
    assert s.health_snapshot()["idle_reason"] == "db_view_stale"


async def test_db_probe_failing_outranks_every_normal_state(store):
    """The point of the fault-first rule: while the detector is down, nothing
    may report a state that implies the queue is trustworthy."""
    for normal in ("slots_full", "quota", "claimable"):
        s = _rig(store, probe_failing=True, **{normal: True})
        assert s.health_snapshot()["idle_reason"] == "db_probe_failing", normal


async def test_db_view_stale_outranks_slots_full(store):
    """Regression for the ordering this review round changed: `slots_full`
    used to sit ABOVE the fault flags, so a wedged view on a busy pool
    reported a perfectly ordinary reason."""
    s = _rig(store, db_view_stale=True, slots_full=True)
    assert s.health_snapshot()["idle_reason"] == "db_view_stale"


async def test_normal_states_keep_their_own_order(store):
    """KNOWN NEGATIVE for the rule: with NO fault present the ordinary states
    still rank as before, so 'faults first' did not disturb them."""
    assert _rig(store, slots_full=True, quota=True, claimable=True
                ).health_snapshot()["idle_reason"] == "slots_full"
    assert _rig(store, quota=True, claimable=True
                ).health_snapshot()["idle_reason"] == "quota_cooldown"
    assert _rig(store, claimable=True
                ).health_snapshot()["idle_reason"] == "claimable_not_dispatched"
    assert _rig(store).health_snapshot()["idle_reason"] == "queue_empty"


async def test_every_fault_drops_healthy_whatever_the_reason_says(store,
                                                                  tmp_path):
    """`idle_reason` names ONE thing; `healthy` must fall for ALL of them.
    Otherwise the precedence order silently decides which faults are visible.
    """
    from httpx import ASGITransport, AsyncClient
    from no_human.api.app import app
    from no_human.config import load_config

    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    app.state.watcher_error = None
    app.state.worker_error = None
    try:
        for flag in ("never_ticked", "tick_stalled", "db_view_stale",
                     "probe_failing"):
            app.state.scheduler = _rig(store, **{flag: True})
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://localhost") as c:
                body = (await c.get("/api/worker/status")).json()
            assert body["healthy"] is False, f"{flag} still reported healthy"
    finally:
        app.state.scheduler = None


async def test_health_snapshot_reports_quota_cooldown(store):
    sched = _sched(store)
    sched._last_tick_at = time.time()
    sched._quota_cooldown_until = (datetime.now(timezone.utc)
                                   + timedelta(hours=1))
    assert sched.health_snapshot()["idle_reason"] == "quota_cooldown"


async def test_worker_status_endpoint_distinguishes_idle_from_wedged(store,
                                                                     tmp_path):
    """The endpoint itself, over the PRODUCTION handler.

    `ASGITransport`, never `TestClient(app)`: booting the real lifespan would
    connect to the OPERATOR's `~/.no_human/no_human.db` and start a live
    Scheduler ticking against their queue (`tests/test_api.py::_ws_shim`
    records that lesson). The handler is what is under test, so the state it
    reads is wired by hand.
    """
    from httpx import ASGITransport, AsyncClient
    from no_human.api.app import app
    from no_human.config import load_config

    sched = _sched(store)
    await sched.tick()
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    app.state.scheduler = sched
    app.state.watcher_error = None
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport,
                               base_url="http://localhost") as c:
            idle = (await c.get("/api/worker/status")).json()
            assert idle["inflight"] == 0
            assert idle["idle_reason"] == "queue_empty"
            assert idle["healthy"] is True
            assert idle["db_view_stale"] is False
            assert idle["db"]["reconnects"] == 0

            # The wedged state. Before this change the two responses were
            # byte-identical, which is why the incident ran for six hours.
            sched._db_view_stale = True
            wedged = (await c.get("/api/worker/status")).json()

        assert wedged["inflight"] == idle["inflight"] == 0
        assert wedged["running"] is idle["running"] is True
        assert wedged["idle_reason"] == "db_view_stale"
        assert wedged["healthy"] is False
        # The four fields the old endpoint returned still cannot tell them
        # apart — which is the point of adding the others.
        legacy = ("running", "inflight", "max_workers", "watcher_error")
        assert {k: idle[k] for k in legacy} == {k: wedged[k] for k in legacy}
    finally:
        app.state.scheduler = None


async def test_worker_status_flags_a_stalled_loop_and_a_failing_probe(store,
                                                                     tmp_path):
    """F1 + F2 at the endpoint, which is where the promise was made.

    Both states used to resolve to `healthy: true` / `idle_reason:
    queue_empty` — the exact reading the incident produced for six hours.
    """
    from httpx import ASGITransport, AsyncClient
    from no_human.api.app import app
    from no_human.config import load_config

    sched = _sched(store)
    await sched.tick()
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    app.state.scheduler = sched
    app.state.watcher_error = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost") as c:
            healthy = (await c.get("/api/worker/status")).json()
            assert healthy["healthy"] is True          # known negative

            sched._last_tick_at = time.time() - 3 * 3600
            stalled = (await c.get("/api/worker/status")).json()

            sched._last_tick_at = time.time()
            sched._consecutive_probe_failures = 4
            sched._last_probe_error = "OperationalError: disk I/O error"
            blind = (await c.get("/api/worker/status")).json()

        assert stalled["healthy"] is False
        assert stalled["idle_reason"] == "tick_loop_stalled"
        assert stalled["seconds_since_last_tick"] > 10000

        assert blind["healthy"] is False
        assert blind["idle_reason"] == "db_probe_failing"
        assert blind["consecutive_probe_failures"] == 4
        assert "disk I/O error" in blind["last_probe_error"]

        # All three are `inflight: 0`. That number was always honest; it was
        # never the answer.
        assert healthy["inflight"] == stalled["inflight"] == blind["inflight"] == 0
    finally:
        app.state.scheduler = None


class _WorkerBoot:
    """What `_boot_real_worker` hands back: the app `lifespan` wired, the
    context manager to unwind it, and the real `asyncio.Task` that
    `_worker_died` was registered on."""

    app = None
    cm = None
    task = None
    started = None


async def _boot_real_worker(monkeypatch, tmp_path, behaviour, tag):
    """Run the PRODUCTION `lifespan`, seaming ONLY `Scheduler.run_forever`.

    Everything Finding A's first half actually claims — the
    `asyncio.create_task`, the `_worker_died` closure, its `add_done_callback`
    — is the shipped code here, reached the way the server reaches it.

    WHY THIS AND NOT A LOCAL COPY. The first draft of these tests
    re-implemented `_worker_died` on a stand-in object. Review proved the
    consequences: the copy had already DRIFTED from production within the same
    commit, it exercised one of the three termination paths, and replacing the
    whole production callback body with `return` left the ENTIRE suite green.
    A duplicate is worse than no test, because it reads as coverage.

    `lifespan` is booted on a THROWAWAY `FastAPI()`, never the module-level
    `app`: that object is shared with every other test in this file, and a live
    Scheduler wired into it would answer `/api/worker/status` for them too.
    """
    import importlib

    from fastapi import FastAPI

    # `from no_human.api import app` resolves to the FastAPI OBJECT, not the
    # module — patching `load_config` needs the module.
    app_mod = importlib.import_module("no_human.api.app")
    from no_human.config import load_config
    from no_human.core import scheduler as sched_mod

    cfg = load_config(tmp_path / f"{tag}-config.yaml")
    cfg.data["database"]["path"] = str(tmp_path / f"{tag}.db")
    monkeypatch.setattr(app_mod, "load_config", lambda *a, **k: cfg)
    # `lifespan` writes this back into os.environ; registering it with
    # monkeypatch first is what gets it restored afterwards.
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS",
                       os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS", ""))

    boot = _WorkerBoot()
    boot.started = asyncio.Event()

    async def _seam(self, *, stop, poll_interval=10.0):
        boot.task = asyncio.current_task()
        boot.started.set()
        await behaviour()

    monkeypatch.setattr(sched_mod.Scheduler, "run_forever", _seam)
    boot.app = FastAPI()
    boot.cm = app_mod.lifespan(boot.app)
    await boot.cm.__aenter__()
    # `create_task` only SCHEDULES the coroutine; nothing has run at `yield`.
    await asyncio.wait_for(boot.started.wait(), timeout=5)
    return boot


async def _unwind_real_worker(boot):
    """Unwind a `_boot_real_worker`, closing the store whatever happened.

    A worker task that raised propagates out of `lifespan`'s own
    `await asyncio.wait_for(worker_task, ...)` — the shutdown path a dead loop
    really takes — and that skips its `await store.close()`. Left alone, the
    aiosqlite thread outlives the test.
    """
    with contextlib.suppress(BaseException):
        await boot.cm.__aexit__(None, None, None)
    store = getattr(boot.app.state, "store", None)
    if store is not None:
        with contextlib.suppress(Exception):
            await store.close()


async def test_lifespan_records_a_worker_loop_that_died_raising(tmp_path,
                                                                monkeypatch):
    """FINDING A, first half — termination 1 of 3, on the PRODUCTION callback.

    `asyncio.create_task` returns a task nobody awaits until shutdown, so an
    exception inside `run_forever` is never retrieved: the coroutine stops,
    `app.state.scheduler` keeps answering, and the endpoint reports
    `running: true` for the rest of the process's life.

    The reachable path is `_recover_orphans`, which `run_forever` awaits BEFORE
    its loop with no try/except and which issues unguarded WRITES — so a
    connection wedged at startup kills the loop before its first tick. That is
    the inferred first cause of the incident this branch exists for.
    """
    async def raises():
        raise sqlite3.OperationalError("database is locked")

    boot = await _boot_real_worker(monkeypatch, tmp_path, raises, "raised")
    try:
        with pytest.raises(sqlite3.OperationalError):
            await boot.task
        await asyncio.sleep(0)                # let the done-callback run
        assert boot.app.state.worker_error == (
            "OperationalError: database is locked"), (
            "the loop died with an exception nobody will ever retrieve. "
            "Recording the TYPE AND MESSAGE is the whole point — a callback "
            "that fires and stores nothing useful leaves the operator with "
            "the same six hours of `running: true` that caused this branch")
    finally:
        await _unwind_real_worker(boot)


async def test_lifespan_records_a_worker_loop_that_returned_without_raising(
        tmp_path, monkeypatch):
    """FINDING A, first half — termination 2 of 3, on the PRODUCTION callback.

    `task.exception()` is None here, so the easy reading is "it finished, fine".
    It is not fine: `run_forever` only returns when `stop` is set, and nothing
    set it. Nothing will ever tick again, and — unlike the raising path —
    asyncio does not even print a "never retrieved" line at GC time. This is
    the QUIETEST of the three, which is why it is asserted separately.
    """
    async def returns_early():
        return None

    boot = await _boot_real_worker(monkeypatch, tmp_path, returns_early,
                                   "early")
    try:
        await boot.task
        await asyncio.sleep(0)
        recorded = boot.app.state.worker_error
        assert recorded is not None, (
            "a loop that returned on its own is just as dead as one that "
            "raised, and reports nothing at all")
        assert "exited on its own" in recorded
        assert "no task will be dispatched" in recorded, (
            "the operator needs the consequence, not just the event")
    finally:
        await _unwind_real_worker(boot)


async def test_lifespan_ignores_a_cancelled_worker_but_not_a_dead_one(
        tmp_path, monkeypatch):
    """FINDING A, first half — termination 3 of 3, on the PRODUCTION callback.

    Cancellation IS the ordinary shutdown path (`stop_event` + the drain in
    `lifespan`'s exit), so it must NOT be recorded as a failure — otherwise
    every clean stop paints the board red and the flag stops meaning anything.

    The second half of this test is the KNOWN POSITIVE for the first: on its
    own, `worker_error is None` also passes against a callback that records
    nothing at all, which is precisely the mutation review found undetected.
    """
    never_set = asyncio.Event()

    async def blocks_until_cancelled():
        await never_set.wait()

    boot = await _boot_real_worker(monkeypatch, tmp_path,
                                   blocks_until_cancelled, "cancelled")
    try:
        boot.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await boot.task
        await asyncio.sleep(0)
        assert boot.app.state.worker_error is None, (
            "an ordinary shutdown was reported as a crash")
    finally:
        await _unwind_real_worker(boot)

    async def returns_early():
        return None

    ctl = await _boot_real_worker(monkeypatch, tmp_path, returns_early, "ctl")
    try:
        await ctl.task
        await asyncio.sleep(0)
        assert ctl.app.state.worker_error is not None, (
            "known positive: the same callback, same helper, one NON-cancelled "
            "death — if this is None the assertion above proves nothing")
    finally:
        await _unwind_real_worker(ctl)


async def test_a_dead_worker_loop_is_reported_not_silently_dropped(
        store, tmp_path, monkeypatch):
    """FINDING A, first half — the CONSUMING end.

    The three tests above prove `_worker_died` records; this one proves the
    endpoint acts on what it recorded, so the two halves cannot pass while the
    chain between them is broken.

    The value fed to the endpoint is not a literal: it is what the PRODUCTION
    callback actually stored. An earlier draft hand-wrote both the callback and
    the string, and the hand-written copy had already drifted.
    """
    from httpx import ASGITransport, AsyncClient
    from no_human.api.app import app
    from no_human.config import load_config

    async def dies_before_ticking():
        raise sqlite3.OperationalError("database is locked")

    boot = await _boot_real_worker(monkeypatch, tmp_path, dies_before_ticking,
                                   "consumed")
    try:
        with pytest.raises(sqlite3.OperationalError):
            await boot.task
        await asyncio.sleep(0)
        recorded = boot.app.state.worker_error
    finally:
        await _unwind_real_worker(boot)
    assert recorded and "database is locked" in recorded

    # The endpoint must turn that into a NOT-healthy answer, even though the
    # Scheduler object is still perfectly happy to describe itself.
    sched = _sched(store)
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    app.state.scheduler = sched
    app.state.watcher_error = None
    try:
        app.state.worker_error = None
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost") as c:
            alive = (await c.get("/api/worker/status")).json()
            assert alive["healthy"] is True          # known negative
            assert alive["worker_error"] is None

            app.state.worker_error = recorded
            dead = (await c.get("/api/worker/status")).json()
        assert dead["running"] is True, (
            "a Scheduler object is still wired up — which is exactly why "
            "`running` cannot be the signal")
        assert dead["healthy"] is False
        assert "database is locked" in dead["worker_error"]
    finally:
        app.state.worker_error = None
        app.state.scheduler = None


def test_the_worker_task_actually_gets_a_done_callback():
    """The wiring above is only real if `lifespan` installs it. Before this
    change `grep add_done_callback src/no_human/api/app.py` returned NOTHING,
    which is what let the loop die silently."""
    import importlib
    # `from no_human.api import app` resolves to the FastAPI OBJECT, not the
    # module — the first draft of this test tripped on exactly that.
    app_mod = importlib.import_module("no_human.api.app")
    src = __import__("pathlib").Path(app_mod.__file__).read_text()
    assert "worker_task = asyncio.create_task(" in src, (
        "control: this is the task site the review measured as unchecked")
    assert "worker_task.add_done_callback(" in src, (
        "the worker task's death is not captured, so a loop that dies leaves "
        "the server reporting running: true forever. Binding the handle is NOT "
        "the safety property — app.py already bound `worker_task` and still "
        "never retrieved the exception; a reference only prevents GC.")


async def test_a_scheduler_that_cannot_describe_itself_is_not_healthy(store,
                                                                      tmp_path):
    """Noted-but-not-blocking in review: `healthy` defaulted to true when the
    object lacked `health_snapshot`. Unreachable in production — which is
    precisely why it should not be left to luck."""
    from httpx import ASGITransport, AsyncClient
    from no_human.api.app import app
    from no_human.config import load_config

    class _Mute:
        inflight: set = set()
        max_workers = 4

    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    app.state.scheduler = _Mute()
    app.state.watcher_error = None
    app.state.worker_error = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost") as c:
            body = (await c.get("/api/worker/status")).json()
        assert body["healthy"] is False
        assert "health_error" in body
    finally:
        app.state.scheduler = None


async def test_worker_status_flags_a_wedged_crash_handler(store, tmp_path):
    """`healthy` must also fall when the crash handler cannot write, which is
    the other half of the silence: the tasks kept a claimable status."""
    from httpx import ASGITransport, AsyncClient
    from no_human.api.app import app
    from no_human.config import load_config

    sched = _sched(store)
    await sched.tick()
    sched._consecutive_status_write_failures = 3
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    app.state.scheduler = sched
    app.state.watcher_error = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost") as c:
            body = (await c.get("/api/worker/status")).json()
        assert body["healthy"] is False
        assert body["consecutive_status_write_failures"] == 3
    finally:
        app.state.scheduler = None
