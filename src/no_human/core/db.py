"""Async SQLite store (WAL). Single-user, single-host — no Postgres (§3.6)."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any, AsyncIterator, Awaitable, Callable, NamedTuple, TypeVar,
)

import aiosqlite

from . import slot_wait
from .task import (
    IllegalTransition,
    Task,
    TaskStatus,
    assert_landed_reconciliation,
    assert_terminal_landed_reconciliation,
    assert_transition,
)

log = logging.getLogger("no_human.db")


class SilentCompletion(Exception):
    """Raised by `Store.set_status` when a caller tries to write DONE without
    a completion `event`.

    Live incident (task 8c8b36b5): three DONE writers called `set_status`
    with no event obligation at all, so the false-done write left no trace —
    the task's `task_events` simply stopped, with nothing recording who
    completed it or why. This is the fail-loud guard that makes a silent
    DONE impossible to write again: deliberately the opposite of
    `EventPersister`'s best-effort contract (`core/events.py`) — a DONE
    transition's event is not advisory, it is the ONLY record the
    transition happened at all.
    """


# --------------------------------------------------------------------------- #
# The role registry: WHICH NAMED ROLE each family of attempt token columns
# bills to. THE one list — every cost surface derives its column set from here
# rather than re-typing it, because this repo has already shipped the drift
# this prevents twice (the coder-only sum that rigged the north-star ratio,
# then the four-tier sums that quietly excluded planning).
#
# Keyed by COLUMN PREFIX, valued by the role's human name. The coder's prefix
# is the empty string: its columns are the unprefixed originals
# (`tokens_used`, `cache_read_tokens`, …) and renaming them would rewrite 50+
# call sites for no gain.
#
# Adding a role means: one entry here, three `*_tokens`/`*_cache_*` columns
# plus one `*_output_tokens` column in `_migrate`, and a sink that fills them.
# `test_role_token_accounting.py` fails on the first without the second, which
# is the only reason this is a registry and not six literals.
#
# Ordered coder-first because that is the order every human-facing breakdown
# prints in; nothing else depends on it.
USAGE_ROLES: dict[str, str] = {
    "": "coder",
    "review_": "reviewer",
    "plan_": "planner",
    "utility_": "utility",
    "supervisor_": "supervisor",
    "distill_": "distill",
}

#: The `pr_outcomes.outcome` values that are FINAL (migration 0010). See the
#: "PR outcomes" section of `Store` for why one constant and not two literals.
#: Mirrors `vcs.pr_outcome.MERGED` / `CLOSED_UNMERGED`; `core` may not import
#: `vcs`, so `tests/test_pr_outcome.py` pins the two spellings equal instead.
SETTLED_PR_OUTCOMES: tuple[str, ...] = ("merged", "closed_unmerged")

#: The same tuple as a SQL `IN (...)` list. Built from the tuple rather than
#: written out again, so a value added above cannot be missed below. Safe to
#: interpolate: the members are module-level literals, never caller input.
_SETTLED_OUTCOMES_SQL = "({})".format(
    ", ".join(f"'{o}'" for o in SETTLED_PR_OUTCOMES))

# The roles that are NOT the coder's own session — i.e. the ones accumulated
# out-of-band during a task and drained onto the attempt row at exit
# (`Orchestrator._pop_aux_usage`) or to the unattributed ledger when no
# attempt ever claims them (`_flush_orphaned_aux_usage`). The reviewer is
# absent on purpose: its burn is written by the review path directly onto the
# attempt row it just judged, never through the aux accumulator.
#
# DERIVED from `USAGE_ROLES` by naming the two EXCLUSIONS rather than by
# re-typing the four members. A hand-written list here would have to be
# widened by hand every time a role is registered, and the failure mode is
# silent: an undrained accumulator loses that role's burn without raising.
# Stating the exclusions instead means a new role is aux BY DEFAULT — the
# safe direction, since a drained role that had nothing to drain is a no-op
# while an undrained one is missing spend.
AUX_USAGE_TIERS: tuple[str, ...] = tuple(
    t for t in USAGE_ROLES if t not in ("", "review_"))

# Sites whose rows ARE recorded against a task: `_flush_orphaned_aux_usage`
# (orchestrator.py:2396) writes `site=f"orphaned_{tier}usage"` for every tier
# in AUX_USAGE_TIERS, always with the task's id. `unattributed_usage_totals`
# uses this prefix (not `task_id`) to split the ledger — see that method's
# docstring for why.
ORPHANED_SITE_PREFIX = "orphaned_"


def usage_columns_for(tier: str) -> tuple[str, ...]:
    """The three ADDEND token columns for one role prefix.

    Deliberately excludes ``{tier}output_tokens``: that is a SLICE of
    ``{tier}tokens_used``, already inside it, and summing it as a fourth
    addend double-counts every output token. See
    ``Store._output_columns_by_class``.
    """
    return (
        "tokens_used" if tier == "" else f"{tier}tokens_used",
        f"{tier}cache_read_tokens",
        f"{tier}cache_creation_tokens",
    )


def _resolve_migrations_dir() -> Path:
    """Locate the schema migrations across the ways this code ships.

    Mirrors `api/app.py::_resolve_web_dist`, for the same reason and with the
    same two layouts:

    1. **Repo checkout / frozen desktop bundle** — ``parents[3]/migrations``.
       In a checkout ``__file__`` is ``<repo>/src/no_human/core/db.py``, so
       parents[3] is the repo root. Under a PyInstaller onedir freeze it is
       ``<bundle>/_internal/no_human/core/db.py``, so parents[3] is the bundle
       root, which is where ``packaging/build-installer.sh`` copies them.
    2. **Wheel install** — ``<site-packages>/no_human/migrations``. parents[3]
       is meaningless there (it points at ``lib/python3.X``, outside the
       package), so the migrations are shipped INSIDE the package instead;
       ``pyproject.toml`` force-includes ``migrations`` to that name.

    Layout 2 did not exist until 2026-08-01, and layout 1 silently resolved to
    ``<venv>/lib/python3.X/migrations`` — a directory that is simply absent.
    `Path.glob` on a missing directory does not raise, it yields nothing, so
    `_migrate` ran zero migrations, created no schema, and every wheel install
    of no_human was unusable from its very first command. See `_migrate` for
    the fail-closed check that now backs this up.

    The first candidate is returned as the fallback when neither exists, so the
    error names the path a developer expects to see.
    """
    candidates = (
        Path(__file__).resolve().parents[3] / "migrations",   # checkout / frozen
        Path(__file__).resolve().parent.parent / "migrations",  # installed wheel
    )
    for candidate in candidates:
        if any(candidate.glob("*.sql")):
            return candidate
    return candidates[0]


MIGRATIONS_DIR = _resolve_migrations_dir()

_T = TypeVar("_T")

# The `(Store, owning asyncio task)` pairs whose critical section the CURRENT
# context is inside. A SET, not one Store: two Stores per process is the normal
# shape here (`nh start` runs the pool's Store and the Jira poller's), and one
# slot made an A -> B -> A call chain self-deadlock, because entering B erased
# the record that A was already held and the return into A then waited on a lock
# this very task owns.
#
# The pair carries the owning task because the context is NOT private to the
# task that set it. `asyncio` COPIES the context into each new Task, so a Task
# created INSIDE a critical section inherits this set verbatim and would take
# the reentrant fast path — running unguarded on the connection while its parent
# still holds the section. Measured on 3.12: `asyncio.create_task`,
# `asyncio.ensure_future` and `TaskGroup.create_task` all inherit;
# `asyncio.wait_for(coro, <positive timeout>)` does NOT, because since 3.12 it
# awaits the coroutine in the caller's own task under `asyncio.timeouts.timeout`
# rather than wrapping it (it did wrap on <=3.11, and still does when the
# timeout is <= 0 — but `requires-python` is >=3.12, so the live vector is
# task creation).
#
# Comparing the recorded owner against `asyncio.current_task()` is what tells
# genuine reentrancy (same task, nested call) apart from that inheritance; see
# `Store._critical`, which raises on the second. Cross-task exclusion for tasks
# created OUTSIDE a section never depended on this — those start from a context
# in which the set is empty.
_in_critical: ContextVar["frozenset[tuple[Store, Any]]"] = ContextVar(
    "_in_critical", default=frozenset())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# THE QUEUE-VISIBILITY CONTRACT, as a symbol rather than a literal repeated in
# five files. `LearningQueue.pending()`, `nh learnings`, `GET /api/learnings`
# and the transcript ingester all select `source = "proposed"`, so an
# unconfirmed memory written under ANY other `source` is invisible to the human
# gate it exists for — it is not queued, it is lost.
#
# Two docstrings already stated this invariant (the `origin` column comment
# above `add_memory`, and `learning/queue.py`'s ORIGIN_* block) and NOTHING
# enforced it. Three separate call sites then broke it — `nh history --analyze`
# (`source="history"`), `nh reply`'s mined learnings (`source="reply"`, which
# lost two real rows from the operator's own review replies) and the curator's
# consolidation pass (`source="curator"`, which archives the proposals it
# consolidates, so a broken write there DESTROYS queue entries). A comment is
# not a constraint; `add_memory` refuses the shape now. Provenance belongs in
# `origin`, which is a second column for exactly this reason.
SOURCE_PROPOSED = "proposed"


def _sqlite_connect(path: str, *, timeout: float = 5.0):
    """The ONE point of use for stdlib `sqlite3.connect` in this module, and a
    deliberate test seam.

    Tests that need to intercept the probe's connection must patch THIS name
    (`no_human.core.db._sqlite_connect`), never `sqlite3.connect`. Patching the
    stdlib attribute is process-global: coverage.py opens its own SQLite
    connections during the run and at teardown, receives the test's wrapper
    object instead of a real Connection, and dies — taking the whole coverage
    report and every test after it with it.
    """
    import sqlite3
    return sqlite3.connect(path, timeout=timeout)


def read_file_marker(path: Path) -> "SnapshotMarker":
    """Read the database file's true head through a brand-new connection.

    MODULE-LEVEL, and not a `Store` method, because it is deliberately not an
    operation on the Store's connection — it is the second opinion the Store's
    connection is checked against, and `tests/test_db_concurrency.py::
    test_no_store_read_keeps_a_raw_cursor` is right to flag a raw `fetchone()`
    inside the class. Putting it here says what it is.

    Stdlib `sqlite3` rather than another `Store`: `Store.connect()` runs the
    migrations, and migration 0009 WRITES (it drops and recreates an FTS trigger
    on every connect). A health probe that writes to the database it is auditing
    is not a health probe. `query_only` makes that structural rather than a
    promise, which matters because this runs against the operator's live file.

    Blocking on purpose — callers hand it to `asyncio.to_thread`.

    Opens its connection through `_sqlite_connect`, the module's test seam —
    see that function's docstring for why patching it, not `sqlite3.connect`,
    is required.
    """
    conn = _sqlite_connect(str(path), timeout=5.0)
    try:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            "SELECT count(*), max(updated_at) FROM tasks").fetchone()
        return SnapshotMarker(int(row[0]), row[1])
    finally:
        conn.close()


def serialized_write(
    fn: Callable[..., Awaitable[_T]],
) -> Callable[..., Awaitable[_T]]:
    """Run one Store write — every statement of it plus its COMMIT — as a
    single critical section on the shared connection.

    ONE `aiosqlite.Connection` is shared by every coroutine (the pool runs
    `concurrency.max_workers` tasks against one Store). aiosqlite serialises
    *individual* operations on its worker thread, but never a *sequence* of
    them: each `await` is a scheduling point where another coroutine's write —
    and, fatally, its `commit()` — runs in the middle of ours. Two consequences,
    both of which this decorator fixes:

    1. `commit()` ends the connection's implicit transaction, so a foreign
       commit lands halfway through any multi-statement write here
       (`create_attempt`'s UPDATE+INSERT, `_migrate`, `update_attempt`'s
       read-modify-write, `add_memory`'s dedupe-then-insert). The atomicity
       those writes assume was never real once two tasks ran at once.
    2. If the statement the foreign commit interrupts is a *writer that has
       produced a row* — `UPDATE … RETURNING`, whose VDBE stays live between
       `execute()` and the fetch — SQLite refuses the COMMIT outright:
       ``OperationalError: cannot commit transaction - SQL statements in
       progress``. That crash killed real attempts (see
       `tests/test_db_concurrency.py`).

    Reads once ran outside this lock, on the reasoning that they never COMMIT,
    so they cannot split someone else's transaction, and that a live SELECT
    cursor does not block COMMIT (SQLite only refuses on `db->nVdbeWrite > 0`).
    Every clause of that is still true, and the conclusion drawn from it was
    still wrong: a SELECT does not block a COMMIT, but while it is UNRESET it
    holds this connection's read transaction open, and a write attempted from
    inside an open read transaction fails immediately. So reads now take this
    lock too — see `Store._critical` and the read helpers below, where the
    window is described exactly.

    The remaining sentence of the original reasoning was that the lock is
    per-Store, i.e. per-connection, which is the right scope — cross-connection
    and cross-process serialisation being SQLite's own job, and unchanged.

    **That is true and was still not enough, so read it narrowly.**
    Delegating to SQLite is correct for LOCK contention between connections
    with nothing else open: the loser waits, and `busy_timeout` (5000 ms, from
    `sqlite3.connect`'s default via aiosqlite — measured, not assumed) decides
    how long. It is NOT correct once THIS connection holds a read transaction,
    because SQLite will not run its busy handler for a read-to-write upgrade —
    waiting there can only deadlock — so it returns at once. Measured on the
    parent commit under the two-Store storm below: time to failure 0.6–1.8 ms
    against a 5000 ms timeout, three orders of magnitude short of ever
    consulting it. There is normally a peer to lose to: `nh start` opens a
    single shared Store that `nh start` hands to both its intake pollers and
    the app lifespan (`cli/commands.py::start._go`, `app.state._external_store`
    — one connection since the 2026-08-03 rescue), and every `nh` CLI
    invocation opens one more in another process. This lock, being per-Store, spans none of them.

    So the boundary above is real, and crossing it safely takes two things, both
    of which this class now does. First, no read may outlive its own fetch —
    see `_fetchone`/`_fetchall`, where that is argued in full; that is what stops
    a read transaction being held INDEFINITELY. Second, reads take this same
    critical section, because closing promptly still leaves the `await` gap
    between `execute()` and the fetch, and a concurrent coroutine's write inside
    that gap fails just the same. `tests/test_db_concurrency.py` covers both.

    **Serialising reads is not free, and the earlier claim that it was is
    withdrawn.** The reasoning behind it — aiosqlite runs every operation for a
    connection on one worker thread, so there is no read/write parallelism to
    give up — is true about the THREAD and says nothing about the LOCK, which
    also makes concurrent readers wait for each other. Measured, six interleaved
    paired rounds, four concurrent readers over a copy of a real 74 MB database
    (`get_task` + `list_attempts`, 480 reads per round):

        parent commit    5,656 – 7,740 reads/s   (median 6,114)
        this commit      2,284 – 4,897 reads/s   (median 2,986)

    — a 2.07x drop at the median, spread 1.16x–2.94x across those six pairs, on
    a loaded machine.

    **DO NOT QUOTE A SINGLE FACTOR FROM THIS.** Three independent measurements
    of the same thing now exist and they do not agree: ~1.6x (12 paired rounds),
    ~2.07x (the six above), ~3x (11,722 / 9,444 vs 2,731 / 3,608). The spread
    WITHIN one six-round session, 1.16x–2.94x, is about as wide as the spread
    BETWEEN sessions — which is what says the differences are machine load, not
    the lock. An earlier draft of this paragraph shipped "2–3x"; that band
    excluded its own disclosed minimum, and only 1 of an independent reviewer's
    12 rounds fell inside it. Withdrawn.

    The honest statement, and the only one this paragraph now makes: reads are
    SLOWER under concurrent readers — roughly 1.2x–3x, load-dependent and not
    reliably characterised. Re-measure before acting on any figure here.

    What that costs in practice, measured the same way: nothing visible on the
    board. One websocket tick (`_board_tasks`) is 68.5 ms vs 63.5 ms at one
    socket and 257.5 ms vs 268.0 ms at four (medians, same six rounds) — the
    first pair moves the WRONG way for a regression, so both are noise,
    because a tick is dominated by two large queries rather than by read count.
    The regression is real, known and bounded; it is not being optimised away
    here, and a future reader should re-measure before assuming it still holds.
    """

    @functools.wraps(fn)
    async def wrapper(self: "Store", *args: Any, **kwargs: Any) -> _T:
        async with self._critical():
            # ROLLBACK ON THE ERROR PATH. Without this, one exception between
            # `execute()` and `commit()` pinned the connection FOREVER, and
            # `db.py` contained no `rollback` anywhere at all (grep it; use
            # `journal_mode` as the known positive that the grep works).
            #
            # `aiosqlite.connect()` passes no `isolation_level`, so Python's
            # legacy implicit-BEGIN applies: the first INSERT/UPDATE/DELETE
            # opens a transaction that only COMMIT or ROLLBACK can end. A write
            # that raised after that point left the transaction open, and every
            # later statement on the shared connection ran inside it — reads
            # served from a snapshot frozen at the moment of the failure, writes
            # failing `database is locked` (SQLITE_BUSY / _BUSY_SNAPSHOT).
            # Restarting the server was the only known cure.
            #
            # WHAT THIS DOES NOT FIX, measured rather than reasoned. An earlier
            # draft of this comment claimed `rollback()` also resets every
            # statement on the connection and therefore ends a READ pin left by
            # an unreset cursor. That is FALSE on this stack. Measured on
            # CPython 3.12 + aiosqlite: pin a connection with `execute("SELECT *
            # FROM tasks")` + one `fetchone()` on a 9-row table, let a peer
            # commit, call `rollback()` — the connection still sees 8 rows while
            # the file holds 9.
            #
            # So this rollback ends a WRITE transaction and nothing else, which
            # is exactly one of the ways a connection gets pinned. That is the
            # concrete reason `probe_snapshot_staleness` is the load-bearing
            # guard and this is the cheap one: detection covers the read pin,
            # the recovered-stale-WAL case, and whatever else there turns out to
            # be. `tests/test_frozen_snapshot_guard.py` holds the measurement so
            # the claim cannot quietly come back.
            #
            # It is a no-op when no transaction is open, the common case.
            #
            # `BaseException`, not `Exception`: a `CancelledError` between
            # `execute()` and `commit()` pins the connection exactly as hard as
            # a `sqlite3.Error` does, and cancellation is routine here (the pool
            # cancels workers on shutdown).
            try:
                result = await fn(self, *args, **kwargs)
            except BaseException:
                await self._rollback_quietly()
                raise
            self.last_successful_write_at = _now()
            return result

    wrapper.__nh_serialized_write__ = True  # type: ignore[attr-defined]
    return wrapper


class SnapshotMarker(NamedTuple):
    """How far through the write history one connection can see.

    `count` alone is not enough: an UPDATE-only wedge (a task escalating) moves
    `max_updated_at` without changing the row count, and that is precisely the
    shape the 2026-08-01 incident took for its first two symptoms.
    """

    count: int
    max_updated_at: str | None

    def behind(self, other: "SnapshotMarker") -> bool:
        return (self.count < other.count
                or (self.max_updated_at or "") < (other.max_updated_at or ""))


class StalenessProbe(NamedTuple):
    """One verdict from `Store.probe_snapshot_staleness`."""

    stale: bool
    shared: SnapshotMarker
    fresh: SnapshotMarker
    recheck: SnapshotMarker | None   # the confirming re-read; None if not needed
    reason: str

    def __repr__(self) -> str:  # keeps log lines and repro output readable
        return (f"StalenessProbe(stale={self.stale}, shared={tuple(self.shared)}, "
                f"fresh={tuple(self.fresh)}, reason={self.reason!r})")


class ImportedTaskRow(NamedTuple):
    """One row of the backlog picker's imported-chip projection (SCRUM-54) —
    only the four columns the chip lookup needs, never a full Task hydration."""

    external_id: str
    id: str
    status: str
    created_at: str


class Store:
    """Thin async wrapper over the tasks/attempts tables."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self._db: aiosqlite.Connection | None = None
        # Guards every write critical section on this connection — see
        # `serialized_write` for why one connection + N coroutines needs it.
        self._write_lock = asyncio.Lock()
        # Liveness counters. Read by `/api/worker/status` so that "idle" and
        # "wedged" stop reading identically (they were the same JSON for six
        # hours on 2026-08-01).
        self.last_successful_write_at: str | None = None
        self.stale_detections = 0
        self.last_stale_at: str | None = None
        self.reconnects = 0
        self.last_reconnect_at: str | None = None

    async def connect(self) -> "Store":
        # no_human.db sits beside the credential store; the directory must be
        # private even when the DB is what creates it.
        from ..config import ensure_private_dir
        ensure_private_dir(self.path.parent)
        self._db = await aiosqlite.connect(self.path)
        # connect() is ATOMIC: it either returns a usable Store or leaves no
        # trace of itself. Anything less hangs the process forever.
        #
        # `aiosqlite.connect()` starts a worker thread, and that thread is NOT
        # a daemon (`aiosqlite/core.py`: `Thread(target=_connection_worker_thread,
        # args=(self._tx,))`, no `daemon=True`). Its loop is a blocking
        # `tx.get()` that only ever ends when `close()` enqueues the stop
        # sentinel. So if any step below raises, the exception propagates to
        # the caller perfectly well — and then the interpreter reaches
        # `threading._shutdown`, joins that live non-daemon thread, and blocks
        # there for the rest of time. The user sees a traceback, if anything,
        # and a command that never returns; ^C is the only way out.
        #
        # That converted "no such table: tasks" (the wheel shipped no
        # migrations) into an unbounded silent hang on `nh status`, `nh doctor`
        # and `nh task list` for every new user. Closing here is what makes the
        # failure a normal, fast, reported error. It is not specific to that
        # bug: EVERY failure path in connect() had it, and every future one
        # would too.
        try:
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode = WAL")
            await self._db.execute("PRAGMA foreign_keys = ON")
            await self._migrate()
            # Warm the loaded-code snapshot HERE, off the event loop, because
            # this is the one place EVERY entrypoint passes through — the
            # server's lifespan, but equally `nh` commands and the eval
            # harnesses, none of which have a lifespan to pre-warm them. Left
            # cold, the first `create_attempt` pays three blocking git
            # subprocesses while holding the sqlite write transaction its own
            # UPDATE just opened. This repo has already lost days to lock
            # storms; a telemetry stamp must not be able to start another one.
            #
            # Three is `ls-files`, `rev-parse`, `status` — 220ms measured on
            # this checkout, and a 30s worst case under the 10s per-call
            # timeout. It was TWO until the tracking check closed the
            # borrowed-sha hole: this count is a measured claim, and it moves
            # when the calls do.
            from .build_info import loaded_code
            await asyncio.to_thread(loaded_code)
            # Best-effort retention groom, same "every entrypoint passes
            # through connect()" reasoning as the warm-up above. A wedged
            # connect is worse than an ungroomed ledger, so this is
            # deliberately fail-open — the totals invariant that must never
            # lie lives in compact_unattributed_usage/unattributed_usage_totals
            # themselves, not here.
            try:
                await self.compact_unattributed_usage()
            except Exception:  # noqa: BLE001 — retention must never fail a connect
                log.debug("unattributed_usage compaction failed", exc_info=True)
        except BaseException:
            db, self._db = self._db, None
            try:
                await db.close()  # stops the worker thread (its `finally` does)
            except BaseException:  # pragma: no cover - never mask the real error
                log.debug("closing the sqlite connection after a failed "
                          "connect() also failed", exc_info=True)
            raise
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _rollback_quietly(self) -> None:
        """End whatever transaction a failed write left open. Never raises.

        The caller is already handling an exception; a failure here must not
        replace it with a less informative one. It is logged rather than
        swallowed, because a rollback that itself fails means the connection is
        in the state `probe_snapshot_staleness` exists to catch.
        """
        db = self._db
        if db is None:
            return
        try:
            await db.rollback()
        except asyncio.CancelledError:
            # NOT ours to swallow. `await db.rollback()` is a suspension point,
            # so a cancellation can arrive DURING it — and catching that here
            # let the task finish with the ORIGINAL exception and
            # `task.cancelled() == False`, i.e. a cancelled task that does not
            # report as cancelled. `serialized_write` reasons carefully about
            # `CancelledError` in the OUTER handler and the first version of
            # this inner one quietly undid it.
            #
            # Re-raising replaces the original exception with the
            # `CancelledError`, which is correct: cancellation outranks it, and
            # the caller that cancelled is entitled to see cancellation. The
            # transaction is left to the connection's teardown, which is the
            # same position every other cancelled write is in.
            raise
        except BaseException:  # noqa: BLE001 — never mask the original error
            log.warning("rollback after a failed write also failed; this "
                        "connection may be serving a frozen snapshot",
                        exc_info=True)

    # --- staleness: never trust the shared connection's own answer --------- #
    #
    # WHY THIS EXISTS AND WHY IT IS THE LOAD-BEARING GUARD. On 2026-08-01 the
    # server's shared connection served a read snapshot pinned three hours in
    # the past. Rows written after the pin were invisible to it, so the
    # scheduler re-dispatched two long-finished tasks ~12x/minute and never saw
    # the one real task waiting. Every surface reported health, because every
    # surface asked THE POISONED CONNECTION.
    #
    # The rollback above removes ONE way to get pinned, and the incident's own
    # timeline argues it was not the way that happened: the process started at
    # 23:28:37 and was pinned to before 20:34:30, three hours before its own
    # first read, with the first crash at log line 220 of 46,000 — i.e. the
    # connection was stale FROM BIRTH, which no transaction this process left
    # open can explain (a stale WAL index recovered at startup can). The first
    # cause is therefore still INFERRED.
    #
    # So this check deliberately does not care what caused the pin. It asks a
    # second, independent connection what the FILE says and compares. Any
    # mechanism that freezes the shared connection — an un-rolled-back write, an
    # unreset cursor, a recovered stale `-shm`, or something not yet imagined —
    # produces the same divergence and is caught here.

    async def _shared_marker(self) -> SnapshotMarker:
        row = await self._fetchone(
            "SELECT count(*) AS n, max(updated_at) AS m FROM tasks")
        return SnapshotMarker(int(row["n"]), row["m"])

    async def probe_snapshot_staleness(self) -> StalenessProbe:
        """Is this connection's read view behind the file? Cheap; per-tick.

        THE FALSE-POSITIVE THAT WOULD MAKE THIS UNSAFE TO ACT ON. The shared
        read and the fresh read cannot be simultaneous, so a peer committing
        between them leaves the fresh marker legitimately ahead. Reconnecting on
        that would churn the connection under ordinary concurrent load — and
        peers are guaranteed here (`nh start` opens Stores for the Jira and
        Linear pollers, and every `nh` CLI command opens one in another
        process).

        The discriminator is a CONFIRMING RE-READ, and it works because the two
        states differ in exactly one observable way: a healthy connection starts
        a new read transaction per statement and so sees the peer's commit
        immediately, whereas a pinned one can never catch up by definition. Only
        a connection still behind on the SECOND read is reported stale.

        Being behind is also the only direction that matters. The shared marker
        reading AHEAD of the fresh one is the same benign race viewed from the
        other side, never a pin.
        """
        shared = await self._shared_marker()
        fresh = await asyncio.to_thread(read_file_marker, self.path)
        if not shared.behind(fresh):
            return StalenessProbe(False, shared, fresh, None, "up-to-date")
        recheck = await self._shared_marker()
        if not recheck.behind(fresh):
            # It caught up, so it was never pinned: a peer simply committed
            # between the two reads.
            return StalenessProbe(False, recheck, fresh, recheck,
                                  "concurrent-write-race")
        self.stale_detections += 1
        self.last_stale_at = _now()
        return StalenessProbe(True, recheck, fresh, recheck, "frozen-snapshot")

    async def reconnect(self) -> None:
        """Drop the connection and open a new one. Recovery for a frozen view.

        Held under the critical section so no coroutine is mid-statement on the
        connection being replaced. `connect()` re-enters that section through
        `_migrate`; `_critical` is reentrant for the same task and Store, so
        that is safe rather than a deadlock.

        The reference is dropped BEFORE the close is attempted: if closing a
        wedged connection fails, the wedged object must not survive as
        `self._db`. If the subsequent connect also fails, `self.db` raises a
        clear "not connected" error, which is a loud failure — the state this
        method exists to escape is the silent one.
        """
        async with self._critical():
            old, self._db = self._db, None
            if old is not None:
                try:
                    await old.close()
                except BaseException:  # noqa: BLE001
                    log.warning("closing the stale connection failed; "
                                "replacing it anyway", exc_info=True)
            await self.connect()
            self.reconnects += 1
            self.last_reconnect_at = _now()

    def liveness(self) -> dict[str, Any]:
        """Connection-health counters for `/api/worker/status`."""
        return {
            "last_successful_write_at": self.last_successful_write_at,
            "stale_detections": self.stale_detections,
            "last_stale_at": self.last_stale_at,
            "reconnects": self.reconnects,
            "last_reconnect_at": self.last_reconnect_at,
        }

    async def __aenter__(self) -> "Store":
        return await self.connect()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store not connected; call connect() first")
        return self._db

    # --- reads: the read transaction must not outlive the read ------------- #
    #
    # EVERY read goes through these two helpers, and the reason is not tidiness.
    #
    # THE WINDOW. A SELECT opens this connection's read transaction when it is
    # first stepped, and holds it until the STATEMENT IS RESET. Reset happens on
    # exactly three events: the statement steps to SQLITE_DONE, the cursor is
    # closed, or the cursor is garbage-collected. So the dangerous span is not
    # "for as long as the cursor is referenced" — that overstates it, and the
    # overstatement matters, because it points at the wrong call sites. The two
    # real spans are:
    #
    #   1. THE `await` GAP BETWEEN `execute()` AND THE FETCH. This one is in
    #      EVERY read, no exceptions. aiosqlite dispatches `execute` and `fetch*`
    #      to its worker thread as two separate awaitables, so there is always a
    #      scheduling point between them at which the read transaction is open
    #      and another coroutine can run. This is the window the critical
    #      section closes, and it is why closing the cursor promptly is not on
    #      its own enough.
    #   2. EVERYTHING AFTER THE FETCH, for a read that does not consume its
    #      whole result set. `fetchmany`, `async for` over more rows than
    #      aiosqlite's 64-row `iter_chunk_size`, or `fetchone` against a query
    #      that matched more than one row all leave the statement live until
    #      close. This is the span that can be held indefinitely, and it is the
    #      one `finally: await cur.close()` below closes.
    #
    # A read that DOES consume its whole result set resets itself and holds
    # nothing past the fetch — CPython's `sqlite3` steps once inside `execute()`
    # and once more inside the fetch, so `SELECT … WHERE id = ?` on a unique key
    # followed by `fetchone()` is already reset. Measured against raw aiosqlite
    # (5-row table, peer commits between the read and the write):
    #
    #     execute() only, no fetch ................. WRITE FAILS
    #     SELECT … WHERE id = ? + fetchone ......... write OK
    #     SELECT * + ONE fetchone .................. WRITE FAILS
    #     SELECT * + fetchall ...................... write OK
    #     SELECT * + fetchmany(2) .................. WRITE FAILS
    #     `async for` + break, 200-row table ....... WRITE FAILS
    #     `async for` + break, 5-row table ......... write OK  (one chunk = done)
    #
    # That table is what `test_a_pinned_snapshot_wedges_the_connection_
    # permanently` characterises, and why it has to seed MORE rows than it fetches.
    #
    # WHY THE WINDOW IS FATAL HERE. A write attempted while this connection
    # holds a read transaction has to upgrade it, and SQLite will not run the
    # busy handler for an upgrade — waiting on one can only deadlock — so it
    # fails AT ONCE instead of after `busy_timeout`. Peers are guaranteed: `nh
    # start` shares ONE Store across its pollers and the app lifespan
    # (`app.state._external_store` — the second/third connections were the 2026-08
    # lock-flood defect and are gone),
    # every `nh` CLI command opens one in another process, and `Store.connect()`
    # itself WRITES — migration 0009
    # drops and recreates the FTS trigger on every connect — so a bare
    # `connect()` + `close()` and nothing else is enough to break a pinned
    # write (measured).
    #
    # WHICH ERROR CODE, HONESTLY. Two, and the traceback does not distinguish
    # them: same file, same line, same message `database is locked`.
    #
    #   * SQLITE_BUSY_SNAPSHOT (517) once a peer has COMMITTED past the snapshot
    #     this connection is pinned to. Deterministic, and permanent until the
    #     statement is reset — every later write fails, which is why restarting
    #     the server used to be the only known cure.
    #   * SQLITE_BUSY (5) when the upgrade merely loses to a peer that holds the
    #     write lock right now.
    #
    # Both appeared, mixed, in single runs of the realistic two-Store storm on
    # the parent commit (four errors per run; 1–3 of each, varying run to run).
    # So the production code was NOT inferable from the traceback, and nothing
    # here asserts which one it was: the honest statement is that an open read
    # transaction makes the next write on that connection fail instantly with
    # SQLITE_BUSY or SQLITE_BUSY_SNAPSHOT. The fix removes both, because it
    # removes the open read transaction.
    #
    # What makes either much worse than it sounds is the message. `database is
    # locked` is a lie about the cause: the file stays writable by every other
    # connection throughout, so the usual external-writer probe (`BEGIN
    # IMMEDIATE` from the `sqlite3` CLI) returns in milliseconds and reports a
    # healthy database while the server cannot write at all.
    #
    # `tests/test_db_concurrency.py` covers this over the read surface, so a new
    # read that reintroduces a bare `self.db.execute` + a fetch fails there.

    # `await self.db.execute(...)` + an explicit close, rather than the tidier
    # `async with self.db.execute(...)`: the connection's `execute` is
    # monkeypatched by tests (`_park_after`) to park a write mid-sequence, and a
    # plain coroutine substitute supports `await` but not `async with`. The
    # guarantee is identical — `finally` runs on the exception path too, which
    # is the path that matters, since that is exactly when a pinned snapshot
    # would otherwise be left behind.

    @asynccontextmanager
    async def _critical(self) -> "AsyncIterator[None]":
        """The connection's critical section — held by reads AND writes.

        REENTRANT, per asyncio task, and it has to be: the read-modify-write
        methods (`update_attempt`, `add_memory`, `set_status`) call `_fetchone`
        from inside `serialized_write`, and `asyncio.Lock` is not reentrant, so
        a plain acquire there would deadlock the pool instead of unlocking it.

        The exemption is keyed on `(Store, owning task)`, and both halves earn
        their place:

        * **the Store**, because the set holds every section this context is
          inside, not just the last one. One slot self-deadlocked an
          ``A -> B -> A`` chain across two Stores — and two Stores in one
          process is this product's normal shape, not a corner case;
        * **the owning task**, because a `ContextVar` is NOT private to the task
          that set it. asyncio copies the context into every new Task, so a Task
          created INSIDE this section starts life holding the exemption.
          Nothing spawns a task in here today, so this is a trap laid for a
          future call site rather than a live bug — which is exactly the kind
          that ships silently. It is made loud below instead: taking the fast
          path on an INHERITED exemption would run unguarded on the connection
          while the parent still holds the lock, so it raises.

        What was always true, and still is: a task created OUTSIDE a section
        cannot see the exemption, because it copied a context in which the set
        was empty. That is the exclusion the lock is actually for.

        WHAT THE SET DOES NOT SOLVE, said here because holding a set of Stores
        invites the assumption that it does. Two DIFFERENT tasks nesting two
        Stores in opposite orders — task 1 takes A then B, task 2 takes B then A
        — deadlock classically, and this exemption cannot see it: the owners
        differ, so each task correctly reads the other's lock as foreign and
        waits for it. Serialising reads widened that surface, because reads now
        take a lock they did not before.

        Not reachable today, and the reason is structural rather than a survey:
        `_critical` and `serialized_write` are private and are entered ONLY from
        inside `Store`, and no `Store` holds a reference to another `Store`
        (`class Store` contains no `Store(` call and no `Store` attribute), so
        no call chain can hold two sections at once. If one ever needs to, the
        rule is a fixed global lock ORDER, not a cleverer exemption — an
        exemption keyed on the holder can never distinguish a cycle from
        ordinary contention.
        """
        held = _in_critical.get()
        me = asyncio.current_task()
        for store, owner in held:
            if store is not self:
                continue
            if owner is me:
                yield                      # genuine reentrancy: our own nesting
                return
            raise RuntimeError(
                "Store._critical: this asyncio task inherited another task's "
                "critical-section exemption for this Store, which means a Task "
                "was created (or asyncio.wait_for was called) inside the "
                "section. Continuing would run this statement on the shared "
                "connection with no lock held while the parent still holds it "
                "— the race the lock exists to stop. Move the Store call out "
                "of the critical section, or give the child task its own Store."
            )
        async with self._write_lock:
            token = _in_critical.set(held | {(self, me)})
            try:
                yield
            finally:
                _in_critical.reset(token)

    async def _fetchone(self, sql: str, params: Any = ()) -> Any:
        """Read one row and release the cursor before returning."""
        async with self._critical():
            cur = await self.db.execute(sql, params)
            try:
                return await cur.fetchone()
            finally:
                await cur.close()

    async def _fetchall(self, sql: str, params: Any = ()) -> list[Any]:
        """Read all rows and release the cursor before returning."""
        async with self._critical():
            cur = await self.db.execute(sql, params)
            try:
                return await cur.fetchall()
            finally:
                await cur.close()

    # The same two, for readers OUTSIDE this module. `doctor.py`,
    # `context/sessions.py` and the board's event search all query the shared
    # connection directly, and a raw `store.db.execute(...)` there is exactly as
    # dangerous as one in here — more so for `context/sessions.py`, which runs on
    # the task path while the pool is writing. Route them through the same
    # critical section and the same guaranteed close.

    async def query(self, sql: str, params: Any = ()) -> list[Any]:
        """Run a read and return every row, cursor released."""
        return await self._fetchall(sql, params)

    async def query_one(self, sql: str, params: Any = ()) -> Any:
        """Run a read and return the first row (or None), cursor released."""
        return await self._fetchone(sql, params)

    @serialized_write
    async def _migrate(self) -> None:
        # Fail CLOSED. `Path.glob` on a directory that does not exist does not
        # raise — it yields nothing — so the natural spelling of this loop is a
        # fail-open in the one place that must not have one: zero migrations
        # runs cleanly, creates no schema, and hands the caller a connection to
        # an empty database. The first symptom then surfaces two frames later
        # in `_ensure_task_columns` as `no such table: tasks`, which names
        # neither the real cause nor the path that was searched.
        sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not sql_files:
            raise RuntimeError(
                f"no_human cannot create its database schema: no *.sql "
                f"migrations found in {MIGRATIONS_DIR}. This installation is "
                f"incomplete — the migrations are part of the package and "
                f"should have been installed alongside it. Reinstall no_human "
                f"(or, in a source checkout, verify that the repo's "
                f"migrations/ directory is present)."
            )
        for sql_file in sql_files:
            await self.db.executescript(sql_file.read_text())
        await self._ensure_task_columns()
        await self.db.commit()

    async def _ensure_task_columns(self) -> None:
        """Add columns that SQLite cannot create idempotently in a .sql file
        (no ADD COLUMN IF NOT EXISTS). Safe to run on every connect."""
        existing = {row["name"]
                    for row in await self._fetchall("PRAGMA table_info(tasks)")}
        wanted = {
            "kind": "TEXT DEFAULT 'feature'",
            "linked_repos": "TEXT",  # JSON list of additional repo paths
            "parent_id": "TEXT",  # LeadAgent: compound task sub-task linkage
            "follows_id": "TEXT",  # sibling link, NOT parent_id's compound-child relation
            # Cooperative cancellation. A dedicated column, NOT task.context:
            # the CLI and the running orchestrator both hold a Task copy, and
            # `update_task` rewrites the whole mutable surface from it — so a
            # flag in `context` is clobbered by whichever writer flushes last.
            # `update_task`'s column list deliberately omits this one, leaving
            # the CLI its sole writer and the orchestrator its sole consumer.
            "cancel_requested": "TEXT",  # reason, or NULL for "keep running"
        }
        for col, decl in wanted.items():
            if col not in existing:
                await self.db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)"
        )
        # Phase 7d: cache metric columns on attempts (validates Phase 2a caching).
        att_existing = {row["name"]
                        for row in await self._fetchall(
                            "PRAGMA table_info(attempts)")}
        att_wanted = {
            "cache_read_tokens": "INTEGER DEFAULT 0",
            "cache_creation_tokens": "INTEGER DEFAULT 0",
            # The REVIEWER's burn, in its own columns. It was thrown away after the verdict,
            # so the DB held only the coder's tokens and no cost surface could price the gate
            # (59 Opus-4-8 runs over full diffs, costing nothing on the record). Separate from
            # the coder's so by_tier/by_profile keep attributing coder spend to the coder.
            "review_tokens_used": "INTEGER DEFAULT 0",
            "review_cache_read_tokens": "INTEGER DEFAULT 0",
            "review_cache_creation_tokens": "INTEGER DEFAULT 0",
            # Every verifier verdict from this round's gate (see
            # migrations/0015_attempt_verifier_results.sql for why the ALTER
            # lives here and not in a .sql file): a JSON list of
            # `VerifierResult.as_dict()`, nullable, no DEFAULT — most rows
            # predate verifiers and NULL, not "[]", is the honest record of
            # "no verifier ran for this attempt".
            "verifier_results": "TEXT",
            # PLANNING burn (single planner, MoA proposers, aggregator): ran on
            # separate readonly backends and was persisted NOWHERE — the docs
            # even claimed it lived "inside the coder's session" (ARCH_REVIEW
            # #5; ~917k cache-read priced at $0 on one measured task). Written
            # once, onto the attempt row of the attempt the plan fed.
            "plan_tokens_used": "INTEGER DEFAULT 0",
            "plan_cache_read_tokens": "INTEGER DEFAULT 0",
            "plan_cache_creation_tokens": "INTEGER DEFAULT 0",
            # UTILITY-tier burn — discarded entirely before B2 #6. It used to
            # mean "everything that is not coder/reviewer/planner", which is a
            # residual and not a role: the supervisor and the context
            # distiller billed into it alongside the stuck hypothesis, the
            # spec evaluator, the assumption pass, both grill halves and the
            # split drafter. Since A5 those first two have columns of their
            # own (below) and this column means the INTAKE/advisory utility
            # tier only. Historical rows keep whatever they were written with;
            # nothing is moved, and the grand total is unchanged either way.
            "utility_tokens_used": "INTEGER DEFAULT 0",
            "utility_cache_read_tokens": "INTEGER DEFAULT 0",
            "utility_cache_creation_tokens": "INTEGER DEFAULT 0",
            # SUPERVISOR burn: the every-`check_every`-tool-calls course
            # corrector (`agent/supervisor.py`, `llm.supervisor_model`). It
            # runs once per N tool calls for the whole length of an
            # implementation session, so it is the one aux role whose cost
            # scales with attempt LENGTH rather than with intake — exactly the
            # thing a cost optimiser needs to see on its own before it starts
            # tuning `check_every`. Folded into `utility_` it was
            # indistinguishable from a one-shot spec evaluator.
            "supervisor_tokens_used": "INTEGER DEFAULT 0",
            "supervisor_cache_read_tokens": "INTEGER DEFAULT 0",
            "supervisor_cache_creation_tokens": "INTEGER DEFAULT 0",
            # CONTEXT-DISTILLATION burn: one utility-model session per
            # oversized gathered chunk (`_distill_large_chunks`). Unbounded in
            # the number of chunks and paid BEFORE the coder writes a line, so
            # it is the other half of the old `utility_` residual that has to
            # be separable — "distillation pays for itself" is a claim about
            # this column against the coder's, and it could not be stated,
            # let alone tested, while the two aux roles shared one bucket.
            "distill_tokens_used": "INTEGER DEFAULT 0",
            "distill_cache_read_tokens": "INTEGER DEFAULT 0",
            "distill_cache_creation_tokens": "INTEGER DEFAULT 0",
            # The OUTPUT share of the `*tokens_used` column beside each one.
            # `_usage_quad` in the backend always had this number and the
            # backend summed it away (`input_tokens + output_tokens`) before
            # anything downstream could see it, so output — which bills ~5x
            # input — was priced at the input rate everywhere: the stats
            # dollars, the cost tiles, and the lifetime brake.
            #
            # A SUBSET, not a fourth addend: `tokens_used` keeps meaning
            # input+output, exactly as all 52 files that read it already
            # assume, and this says how much of that total was output. Input
            # is `tokens_used - output_tokens`. One source of truth for the
            # total, so the two can never drift into disagreeing about it.
            #
            # NO `DEFAULT 0`, unlike every column above — and that is the
            # whole point of them being separate lines. ADD COLUMN backfills
            # the declared default, so `DEFAULT 0` would stamp "this attempt
            # emitted no output tokens" onto every row in the ledger. The
            # split was discarded AT CAPTURE; there is nothing to backfill
            # from and there never will be. NULL reads "unknown" and prices at
            # the old rate; 0 reads "free" and is a lie. A 0 written for an
            # unreported field is how a per-attempt brake went inert on 27 of
            # 27 tasks once already.
            "output_tokens": "INTEGER",
            "review_output_tokens": "INTEGER",
            "plan_output_tokens": "INTEGER",
            "utility_output_tokens": "INTEGER",
            "supervisor_output_tokens": "INTEGER",
            "distill_output_tokens": "INTEGER",
            # Which model actually ran which role on this attempt. Nothing
            # recorded it, which is how a frozen config.yaml silently inverted
            # coder and reviewer for a week.
            "models": "TEXT DEFAULT '{}'",
            # Which subscription paid for this attempt (profile name, never a
            # token). NULL on attempts that predate auth profiles.
            "auth_profile": "TEXT",
            # Which team-brain version this attempt read remote rules AS OF,
            # pinned once at attempt start. NULL whenever the feature is off,
            # which is the default and every attempt before it existed.
            # Deliberately a SECOND column rather than folded into
            # auth_profile: they answer different questions — who paid, and
            # what the agent knew — and one column cannot answer both.
            "brain_watermark": "INTEGER",
            # The checkpoint this attempt was supposed to resume from and could
            # not, plus what it did instead. NULL on every attempt that resumed
            # normally or never had a checkpoint, which is almost all of them.
            # Its own column rather than `failure_reason`: the attempt is not
            # failed — it branched from base and may well open a PR — and
            # writing "why did this fail" on a succeeding attempt would put a
            # red line under it on every surface that prints that column.
            "resume_checkpoint_lost": "TEXT",
            # The checkpoint this attempt DID branch from. Its sibling above
            # records only the failure, and is NULL both when a resume worked
            # and when there was never a checkpoint to resume — NULL on all 828
            # rows of the live DB — so on its own it cannot answer "does
            # resuming work?", which is the question the crash-requeue path
            # exists to move. The pair answers two different questions and BOTH
            # can be true of one attempt: this one names the checkpoint the
            # attempt did branch from, the other names a checkpoint that was
            # recorded and could not be read. An attempt that steps over a
            # human's pruned gate onto a surviving [WIP-PARTIAL] writes both
            # (`orchestrator._run_attempt`, same `if self._commit_exists(...)`
            # branch) — it USED one and LOST another. An earlier version of
            # this comment said "exactly one of the pair is ever written per
            # attempt"; a review refuted it executably, and this repo has a
            # documented class of claims that exceed their mechanism.
            "resume_checkpoint": "TEXT",
            # Which CODE produced this attempt's verdict — the sha of what the
            # server actually has IN MEMORY, not HEAD at query time. The server
            # loads the backend once; merging a fix to main does not reload it,
            # so a verdict from superseded code was indistinguishable from a
            # verdict from the fix (task ecfe1789 escalated on a tamper-guard
            # false positive 3h18m after the commit that fixed that exact false
            # positive had merged). With this stamped on the row, such an
            # escalation can be RE-JUDGED afterwards instead of being charged
            # to the ticket — which is also what was corrupting the dogfood
            # success measurement. See core/build_info.py for the format and
            # for why this records rather than blocks.
            "loaded_code_version": "TEXT",
            # 1 = this attempt died in the SDK/transport before doing any
            # work (an auth/billing wall, or a session that streamed zero
            # tokens) — see `orchestrator._infra_sdk_failure`. NULL/0
            # everywhere else, including every pre-existing row: nothing
            # backfills it, so old rows keep counting exactly as they always
            # did, which is correct since nothing knows they were infra.
            # The row itself is NEVER skipped — it is the only durable record
            # of the incident — only `lifetime_usage_by_class`'s attempt COUNT
            # AND cost-weighted token sums exclude it (both gated by the same
            # `_lifetime_included_sql` predicate), which is the single
            # chokepoint both budget gates (`_check_lifetime_budget`,
            # `_at_lifetime_ceiling`) read, so they cannot disagree about
            # whether a dead dispatch counted on either axis.
            "infra_failure": "INTEGER",
            # 1 = this attempt was a post-PASS MECHANICAL round — a pr_conflict
            # rebase, or a re-verification tick after the change had already
            # passed independent review (`orchestrator._mechanical_round`).
            # The row is never skipped — it is the durable record — only
            # `lifetime_usage_by_class`'s attempt COUNT AND cost-weighted
            # token sums exclude it, the same `_lifetime_included_sql`
            # predicate `infra_failure` uses, so both budget gates
            # (`_check_lifetime_budget`, `_at_lifetime_ceiling`) cannot
            # disagree on either axis. NULL/0 on every pre-existing row;
            # nothing backfills it.
            # INCIDENT 2026-08-13: tasks 79183501 and 1a4b7bf7 PASSED review,
            # then each supervising-train landing under their open PRs spawned
            # a pr_conflict rebase round that burned a lifetime attempt, until
            # attempts hit cap and killed work that was already reviewed and
            # about to land.
            "mechanical_round": "INTEGER",
            # The coder's raw final_text for this attempt, in full — the surface the
            # PR body's 4000-char `_SUMMARY_TRUNCATED_MARKER` points a reader at
            # (`GET /api/tasks/{id}` -> `TaskOut.full_report`, and `nh task show`). Written
            # once, at the coder-usage chokepoint (`update_attempt` right after the
            # coder session ends), capped at `orchestrator._FULL_REPORT_MAX_CHARS` as
            # a size guard, not a redaction — filtering/scrubbing happens at RENDER
            # time (`report_surface.render_full_report`) so a later change to
            # `_SUMMARY_DROP_MARKERS` applies retroactively to already-stored rows.
            # NULLABLE, NO DEFAULT: most rows predate this column, and NULL — not ""
            # — is the honest record of "no report was ever written here". See
            # migrations/0016_attempt_full_final_text.sql for why the ALTER lives
            # here and not in a .sql file.
            "full_final_text": "TEXT",
            # NULLABLE, NO DEFAULT: audit record of the base-branch sha
            # `Orchestrator._run_attempt` pinned via `GitRepo.ls_remote_exact`
            # BEFORE the coder session started — the exclusion root the
            # attribution gate actually used (see `_base_exclusion_refs`).
            # NULL means either a row predates this column or the pin was
            # unresolvable (remote unreachable, ref absent, ambiguous) — in
            # both cases the gate ran with no exclusion window, never a
            # silently-wrong one. Same no-.sql-migration precedent as
            # `full_final_text` above: additive, generic `ALTER TABLE` loop.
            "base_pin_sha": "TEXT",
        }
        # A THIRD exclusion lives beside these two, but needs no new column:
        # `status = 'interrupted'` (the base column, written only by
        # `create_attempt`'s stale-row sweep, `close_open_attempts` and
        # `close_attempts_of_terminal_tasks` — never by the agent) AND zero
        # priced work (`_zero_priced_work_sql()`, over `_usage_columns()`) is
        # also excluded from `lifetime_usage_by_class`'s attempt COUNT AND
        # cost-weighted token sums, at the same chokepoint `infra_failure` /
        # `mechanical_round` use. The boundary: an interrupted attempt WITH
        # real priced spend still counts — only the zero-priced interrupted
        # shape does not. See
        # `lifetime_usage_by_class` for the full incident and reasoning.
        for col, decl in att_wanted.items():
            if col not in att_existing:
                await self.db.execute(f"ALTER TABLE attempts ADD COLUMN {col} {decl}")
        # D2 #3 curator: memories gain a recoverable archive flag — the
        # curator NEVER deletes (broker invariant); archived rows leave the
        # pending queue but stay queryable.
        mem_existing = {row["name"]
                        for row in await self._fetchall(
                            "PRAGMA table_info(memories)")}
        if "archived" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN archived INTEGER DEFAULT 0")
        # B2: WHICH SIGNAL produced a proposal — "review" (a reviewer FAIL
        # round's findings, B1) or "supervisor" (a recurring supervisor
        # `correct` decision). A SECOND column rather than reusing `source`,
        # for the same reason `brain_watermark` is not folded into
        # `auth_profile`: they answer different questions. `source` is the
        # queue-VISIBILITY contract — `pending()`, `nh learnings`, the API and
        # the ingester all select `source="proposed"` — so a proposal that
        # named its provenance there would be invisible to the human gate it
        # exists for.
        #
        # NO DEFAULT, deliberately. ADD COLUMN backfills the declared default,
        # and stamping "review" (or any other value) onto every pre-existing
        # row would be inventing provenance for rows that genuinely do not
        # record it. NULL reads "unknown", which is the truth.
        if "origin" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN origin TEXT")
        # B3: STRUCTURED EVIDENCE — what happened, in which task, citing the
        # correction/review event — as JSON, beside the human-prose `content`
        # that already narrates it. NO DEFAULT, same reasoning as `origin`:
        # rows written before the column genuinely did not record structured
        # evidence, and NULL says so honestly.
        if "evidence" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN evidence TEXT")
        # B4: the PROJECT SCOPE — "prj:" + sha256 of the normalized git remote
        # URL (learning/scope.py) — so the same repository cloned at two paths
        # is one project. `project` keeps the checkout path (the human-readable
        # blast-radius line in `nh learnings`); this column is the identity
        # recall matches on. NULL = legacy row or a repo with no remote; those
        # keep matching by path, and `stamp_project_scope` upgrades them the
        # next time their repo is actually seen — the only moment the
        # path→remote mapping is knowable.
        if "project_scope" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN project_scope TEXT")
        # S2: WHEN this memory was last INJECTED into a prompt — stamped by
        # `Orchestrator._load_active_memories`, the one place a task turns into
        # an active rule set. It answers the question the confirm queue cannot:
        # of the rules a human already confirmed, which have ever done anything?
        #
        # NO DEFAULT, same reasoning as `origin` and `evidence`: a row written
        # before the column genuinely has no usage history, and backfilling
        # `datetime('now')` would stamp every legacy rule as freshly used —
        # inventing the exact fact `nh learnings --stale` exists to report.
        # NULL reads "never seen used", which is the truth for a legacy row and
        # for a rule that has genuinely never triggered; `--stale` says which
        # of the two it cannot tell apart rather than guessing.
        if "last_used_at" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN last_used_at TEXT")
        # D3-M1: WHO confirmed this memory into the active set — 'human' (every
        # `nh learnings`/API confirm path, the default) or 'auto' (the
        # evidence-gated auto-confirm of a RECURRING review-origin lesson,
        # `learning.queue`). This column is the wall that keeps auto-confirm from
        # weakening gate independence: the reviewer's confirmed-rules channel
        # EXCLUDES rows that are (origin='review' AND confirmed_by='auto'), so an
        # auto-confirmed review lesson reaches the coder and NEVER the reviewer
        # that produced it.
        #
        # NO DEFAULT, same reasoning as `origin`/`evidence`/`last_used_at`: a row
        # confirmed before this column genuinely does not record who confirmed
        # it, and NULL says so honestly. NULL is treated as human/legacy — it is
        # NOT 'auto', so a review-origin rule confirmed the old way (a human
        # clicked it, before auto-confirm existed) stays visible to the reviewer,
        # which is correct: a human stood between that verdict and that rule.
        if "confirmed_by" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN confirmed_by TEXT")
        # Memory lifecycle A: how many times this memory has ever reached a
        # prompt, incremented alongside `last_used_at` at the same chokepoint
        # (`record_memory_uses`). DEFAULT 0, unlike its siblings above — this
        # is a count, not a fact a legacy row cannot honestly report; zero is
        # the true value for a row with no recorded injection.
        if "use_count" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN use_count INTEGER DEFAULT 0")
        # P1 brain hygiene: a recoverable QUARANTINE flag, separate from
        # `archived` (a different flag, a different meaning — curator
        # dedupe/rejection, not provenance). `quarantined` gates a row out of
        # every read path `list_memories` feeds (the UI, rule injection,
        # export/manifest) without deleting it; see `learning/provenance.py`
        # for what sets it. `NOT NULL DEFAULT 0` is legal for SQLite
        # `ADD COLUMN` and backfills every existing row to "not quarantined",
        # which is correct: nothing here retroactively flags a pre-existing
        # row — that is `nh memories scan --apply`'s job, run once, explicitly.
        if "quarantined" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN quarantined "
                "INTEGER NOT NULL DEFAULT 0")
        # PROVENANCE, as a JSON field — project/context/timestamp/reason —
        # stamped by `add_memory` at write time. NO DEFAULT, same reasoning as
        # `origin`/`evidence`/`last_used_at` above: a legacy row genuinely has
        # no recorded provenance, and NULL says so honestly.
        if "provenance" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN provenance TEXT")

        # Memory lifecycle C: WHICH row this one replaced — set only by
        # `supersede_memory` when a freshly-confirmed row turns out to be a
        # near-duplicate of an existing active one. Points from the OLD
        # (archived) row to the NEW (surviving) row's id, so "why is this
        # archived?" answers with "superseded by <id>" instead of a bare
        # archive reason string that would have to be parsed to find it.
        #
        # NO DEFAULT, same reasoning as `origin`/`evidence`/`last_used_at`/
        # `confirmed_by`: NULL means "not superseded", which is honestly true
        # of every row that predates this column, not a guess.
        #
        # There is no alembic in this repo (`migrations/0001_init.sql` is
        # `CREATE TABLE IF NOT EXISTS` and replays on every connect — a bare
        # `ALTER` there would fail on the second connect, and SQLite has no
        # `ADD COLUMN IF NOT EXISTS`), so this follows the exact PRAGMA-guarded
        # idiom as every other `memories` column added since the base schema.
        if "superseded_by" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN superseded_by TEXT")

        # D3 (2026-08-31 operator directive): auto-activation's `paused`/
        # `activated_at` columns and the `learning_events` audit table — a
        # separate method (not inlined here) so this already-long function
        # doesn't grow past the structural-budget ratchet
        # (`tests/test_structural_budget.py`) for three columns' worth of
        # ALTERs.
        await self._ensure_d3_learning_columns(mem_existing)

        # Phase 6a: test_layers column on projects (JSON-encoded TestPlan layers).
        proj_existing = {row["name"]
                         for row in await self._fetchall(
                             "PRAGMA table_info(projects)")}
        if "test_layers" not in proj_existing:
            await self.db.execute(
                "ALTER TABLE projects ADD COLUMN test_layers TEXT DEFAULT '[]'"
            )
        # Phase 7e: history cache table — content-signature keyed so onboarding
        # doesn't re-extract every request. "Re-scan" forces refresh.
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS history_cache (
                content_sig TEXT PRIMARY KEY,
                cascade_id TEXT NOT NULL,
                title TEXT,
                findings_json TEXT,
                ingested_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Real spend that NO ATTEMPT ROW can own. Two sources, both intake:
        #
        #  * The interactive grill (`nh task add --grill`, the board's
        #    /api/grill endpoints) runs BEFORE a task exists, so
        #    `attempts.utility_*` is not merely the wrong column — there is no
        #    row, and often no task ever (the operator can walk away mid-
        #    wizard). Those rows carry task_id NULL.
        #  * Pre-attempt intake on a task that never reached an attempt (parked
        #    at the plan gate, escalated on an unavailable input, decomposed).
        #    The task id IS known, so those rows carry it — but no attempt
        #    spent it, and inventing an attribution is how a cost surface
        #    starts lying.
        #
        # `site` says which, per row, so the residual stays diagnosable instead
        # of being one anonymous number.
        #
        # DELIBERATELY NOT summed into per-task cost (`lifetime_usage`,
        # `eval/northstar`): those answer "what did THIS task cost", and this
        # table is by construction the spend no attempt owns. It is the
        # whole-cost residual — read it for the true total, not the per-task
        # one. `nh status` prints it whenever it is non-zero.
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS unattributed_usage (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                site TEXT NOT NULL,
                model TEXT,
                task_id TEXT,
                tokens_used INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                rolled_up INTEGER DEFAULT 0
            )
        """)
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_unattributed_usage_task "
            "ON unattributed_usage(task_id)"
        )
        # `rolled_up` (compact_unattributed_usage's retention roll-up marker,
        # below) postdates the table above for installs that already have it —
        # CREATE TABLE IF NOT EXISTS is a no-op there, same idiom as
        # `confirmed_by`/`test_layers` above.
        uu_existing = {row["name"]
                       for row in await self._fetchall(
                           "PRAGMA table_info(unattributed_usage)")}
        if "rolled_up" not in uu_existing:
            await self.db.execute(
                "ALTER TABLE unattributed_usage ADD COLUMN "
                "rolled_up INTEGER DEFAULT 0"
            )

        # Single-row (id=1) leader marker for `Scheduler._claim_pool_lease`:
        # which (pid, host) currently owns this database's pool, and when it
        # last proved it was still alive. Exists so a second `nh start`/
        # `nh serve` sharing this DB can refuse to boot alongside a live
        # sibling instead of duplicate-claiming tasks and — incident
        # 6408aba0 — having its own startup orphan sweep see the sibling's
        # live mid-run row as unowned and requeue it out from under it.
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                pid INTEGER NOT NULL,
                host TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ts REAL NOT NULL
            )
        """)
        # `start_token`: an opaque per-process start-time marker (see
        # `config.process_start_token`), added for the pid-reuse fix — same
        # host + a live pid whose CURRENT token no longer matches this row's
        # is provably a NEW process that reused the pid, not the original
        # holder. NULL for any row written before this column existed (or by
        # a caller that could not determine its own token): `_claim_pool_
        # lease` treats a NULL exactly as it always has (pid_alive alone).
        sh_existing = {row["name"]
                       for row in await self._fetchall(
                           "PRAGMA table_info(scheduler_heartbeat)")}
        if "start_token" not in sh_existing:
            await self.db.execute(
                "ALTER TABLE scheduler_heartbeat ADD COLUMN start_token TEXT"
            )

    async def _ensure_d3_learning_columns(self, mem_existing: set[str]) -> None:
        """D3 (2026-08-31 operator directive): the auto-activation pipeline's
        own schema additions — `memories.paused`, `memories.activated_at`,
        and the `learning_events` audit table. Split out of
        `_ensure_task_columns` (which calls this with the `memories` PRAGMA
        it already fetched) purely to keep that function under the
        structural-size ratchet; there is no other reason this couldn't be
        inlined there like every other `memories` column above it.
        """
        # `paused=1` keeps the row exactly where it already is (curator.py's
        # never-deletes invariant) and is the wall `list_memories`'s default
        # excludes, mirroring `archived`/`quarantined` — so
        # `Orchestrator._load_active_memories` (the one prompt-injection
        # chokepoint) never sees a paused row without any caller having to
        # remember to filter it out itself. `NOT NULL DEFAULT 0` is legal for
        # SQLite `ADD COLUMN` and backfills every existing row to "not
        # paused", which is correct: nothing here retroactively pauses a
        # pre-D3 row.
        if "paused" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN paused INTEGER NOT NULL "
                "DEFAULT 0")
        # WHEN a row was auto-activated (`confirmed_by='auto'`) —
        # `LearningQueue.activate_memory_auto`'s own timestamp, distinct from
        # `created_at`/`updated_at`. This is the column the 90-day
        # auto-retirement sweep keys on: an operator-pinned/manually-added
        # row was never auto-activated, so it never has a value here, and
        # that absence IS `curator.py`'s pinned-exempt guarantee — a query
        # keyed on `activated_at IS NOT NULL` cannot select a pinned row by
        # construction, not by a second exemption list that could drift from
        # the first. NO DEFAULT, same reasoning as `origin`/`evidence`/
        # `confirmed_by` in `_ensure_task_columns` above: a pre-D3 row
        # genuinely was never auto-activated, and NULL says so honestly.
        if "activated_at" not in mem_existing:
            await self.db.execute(
                "ALTER TABLE memories ADD COLUMN activated_at TEXT")

        # The audit trail for every learning lifecycle transition (activate /
        # auto_archive / pause / delete / confirm / retire / restore /
        # inject) — one append-only table rather than one column per
        # transition, so "what happened to this row, in order" is a single
        # query instead of reading `updated_at` against five flags. `detail`
        # is JSON: for `inject` it MUST carry which tags fired (2026-09-01
        # effectiveness study) — the one thing a bare "this memory was used"
        # row cannot answer. `CREATE TABLE IF NOT EXISTS` replays safely on
        # every connect, same idiom as `scheduler_heartbeat`/
        # `unattributed_usage` in `_ensure_task_columns`.
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS learning_events (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_learning_events_memory "
            "ON learning_events(memory_id)"
        )

    # ----------------------------- tasks ---------------------------------- #

    @serialized_write
    async def create_task(self, task: Task) -> Task:
        row = task.to_row()
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        await self.db.execute(
            f"INSERT INTO tasks ({cols}) VALUES ({placeholders})", row
        )
        await self.db.commit()
        return task

    async def get_task(self, task_id: str) -> Task | None:
        row = await self._fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return Task.from_row(dict(row)) if row else None

    async def find_task(self, prefix: str) -> Task | None:
        """Resolve a task by full id or a unique id prefix (CLI convenience)."""
        rows = await self._fetchall(
            "SELECT * FROM tasks WHERE id = ? OR id LIKE ? LIMIT 2",
            (prefix, prefix + "%"),
        )
        if len(rows) == 1:
            return Task.from_row(dict(rows[0]))
        return None

    async def list_tasks(
        self,
        status: TaskStatus | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[Task]:
        """`limit`/`offset` (P5, fleet finding 6468d631) are pushed down to
        SQL, not sliced in Python after a full fetch — a page must not still
        pay the full-table row-hydration cost. `None` (every pre-P5 caller)
        is exactly the old unbounded query.

        `rowid DESC` is the tie-break (review round 1) — same reasoning as
        `list_claimable_tasks`'s `rowid ASC`, mirrored for direction:
        `created_at` is an ISO-8601 string, so rows created in the same tight
        loop (`/split`'s children) can share one value, and without a
        tie-break their relative order is not part of the SQL contract —
        nothing stops it differing between two separate `LIMIT`/`OFFSET`
        calls (a page boundary landing inside a tie), which would duplicate
        or drop a row across pages.
        """
        params: tuple = (status.value,) if status is not None else ()
        where = "WHERE status = ? " if status is not None else ""
        sql = f"SELECT * FROM tasks {where}ORDER BY created_at DESC, rowid DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = params + (limit, offset or 0)
        rows = await self._fetchall(sql, params)
        return [Task.from_row(dict(r)) for r in rows]

    async def list_claimable_tasks(self, status: TaskStatus) -> list[Task]:
        """Tasks in `status`, OLDEST FIRST — this query's own order, and the
        scheduler's claim order for IMPLEMENTING (which this module never
        re-ranks). For PENDING, `scheduler._rank_pending` re-sorts what this
        returns — by prior-work split, then `priority_rank`, then this
        oldest-first order as the final tie-break — so the queue order a
        PENDING batch actually dispatches in is FIFO only within a priority
        tier, not across the whole batch (live, 2026-08-22).

        Deliberately NOT `list_tasks`. That one is DESC because the board
        legitimately shows newest first, and the scheduler consuming it made
        dispatch LIFO: on 2026-08-12 four tickets filed a day earlier had never
        dispatched while overnight filings went out within minutes, because
        every fresh filing landed at the head of the PENDING list. This query's
        order is FIFO; display order is not; they are two different questions
        and now two different queries.

        `rowid ASC` is the tie-break, not decoration: `created_at` is an
        ISO-8601 string, so two rows created inside the same microsecond (or
        imported with a coarser stamp by an intake poller) would otherwise
        order arbitrarily and the FIFO guarantee would be luck.
        """
        rows = await self._fetchall(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC, rowid ASC",
            (status.value,),
        )
        return [Task.from_row(dict(r)) for r in rows]

    async def get_task_by_source_external_id(
        self, source: str, external_id: str
    ) -> Task | None:
        """Filtered dedupe lookup for any external-source intake (Slack, and
        usable by Jira too) — one indexed-shape query instead of hydrating
        every task via `list_tasks()` just to scan for a match."""
        row = await self._fetchone(
            "SELECT * FROM tasks WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        )
        return Task.from_row(dict(row)) if row else None

    async def list_imported_tasks(self, source: str) -> list[ImportedTaskRow]:
        """Narrow projection for the backlog picker's imported-chip lookup
        (SCRUM-54): only (external_id, id, status, created_at) for tasks from
        ONE tracker with a linked external_id, via one filtered SQL query —
        never a full `list_tasks()` hydration of every task's every column just
        to read four fields.

        `source` is a parameter, not a literal, because the picker now lists
        two trackers: dedupe keys on (source, external_id), so a Jira NO-1 must
        not make a Linear NO-1 look already-imported. Bound as a SQL parameter
        like every other value here — never interpolated."""
        rows = await self._fetchall(
            "SELECT external_id, id, status, created_at FROM tasks "
            "WHERE source = ? AND external_id IS NOT NULL",
            (source,),
        )
        return [
            ImportedTaskRow(
                external_id=r["external_id"], id=r["id"],
                status=r["status"], created_at=r["created_at"],
            )
            for r in rows
        ]

    @serialized_write
    async def set_status(
        self,
        task: Task,
        new_status: TaskStatus,
        *,
        validate: bool = True,
        human_override: bool = False,
        event: dict[str, Any] | None = None,
        reconciliation_gate: Callable[[TaskStatus], None] | None = None,
        terminal_reconcile: bool = False,
    ) -> Task | None:
        """Transition a task, enforcing the legal-transition map by default.

        CAS guard (SCRUM-73): the WHERE clause is checked against the live DB
        row inside this one statement, not the possibly-stale `task.status`
        this caller is holding — a worker coroutine can hold IMPLEMENTING
        while a human's `shipped` verb already wrote DONE, and
        IMPLEMENTING->REVIEWING passes `assert_transition` on the stale
        value. Terminal here means the row reads DONE, or reads FAILED with a
        `cancel_reason` recorded in context (an explicit human cancel, not a
        plain failure) — a plain FAILED row stays writable so `nh task retry`
        / `POST /api/tasks/{id}/retry` keep working. Once a row is terminal,
        only a write that keeps its status unchanged may land; every other
        write (including validate=False ones) is a no-op that returns None.

        `human_override=True` bypasses the guard entirely — reserved for the
        human verbs that are allowed to move a row OUT of a terminal state
        (retry, cancel, shipped). Every other call site (watcher,
        orchestrator, scheduler, pipeline) must leave it at the default so a
        stale in-process handle can never clobber a human's terminal write.

        `event` is REQUIRED whenever `new_status` is DONE (`SilentCompletion`
        otherwise) — must be a dict with a truthy `kind` and `source`. It is
        INSERTed into `task_events` in the same commit as the status write,
        and only when the CAS actually moved the row (`cur.rowcount`): a
        refused transition records nothing, same as every other field on a
        no-op write. The insert is deliberately NOT wrapped in try/except —
        an emitter failure must fail the completion loudly, the opposite of
        `EventPersister`'s best-effort contract. Non-DONE transitions are
        unaffected: `event` is optional and ignored when absent.

        Every write that moves a row OFF `awaiting_approval` to anything
        other than `done` also stamps `context.approval_superseded_at`
        (write-once — only if `approved_at` is set and no marker is already
        recorded), in the SAME UPDATE as the status write, regardless of
        which of the three branches below fires. `approved_at` itself is
        never cleared — it stays a permanent audit trail of "a human
        approved this" — but a superseded approval must stop reading as a
        live "merge pending" once the task has escalated, failed, been sent
        back to implementing, or otherwise left the approval gate; see
        `core/lanes.py::approval_pending`, the one predicate every surface
        (API payload, CLI, board, drawer) derives the chip from. `done` is
        excluded: a completed merge is the approval's success, not its
        supersession, and the chip is already suppressed there by the
        `status == awaiting_approval` half of `approval_pending` regardless
        of this marker.

        `reconciliation_gate` is a SECOND, NARROWER legality check consulted
        ONLY as a fallback: `assert_transition` — the general
        `ALLOWED_TRANSITIONS` map — still runs first and unconditionally
        whenever `validate=True`, exactly as before this parameter existed.
        Only when that raises `IllegalTransition` does a caller-supplied gate
        get one more chance to accept the SAME (src, dst) pair on its own,
        narrower terms; if it also raises (or none was given), the original
        `IllegalTransition` propagates unchanged. This is how
        `reconcile_landed_orphan` below completes an orphaned-but-landed row
        (IMPLEMENTING/TESTING -> DONE) through this one validated write path
        — passing `assert_landed_reconciliation` as the gate — WITHOUT
        widening the general map itself: the map staying authoritative is
        exactly what keeps `Orchestrator._advance_after_review`'s plain
        `set_status(task, target)` call (no `reconciliation_gate` argument,
        so no fallback is even consulted) refusing IMPLEMENTING/TESTING ->
        DONE — see
        `tests/test_post_review_transition_6408aba0.py::
        test_recovery_never_launders_an_illegal_jump`. It is not a
        validation bypass in the `validate=False`/`human_override` sense:
        every write still passes through an explicit, raising check —
        either the general map alone, or the general map followed by a
        named narrower one — never neither.

        `terminal_reconcile=True` is a THIRD, narrow CAS mode — distinct from
        both the default guard and `human_override` — for exactly one
        caller: `reconcile_landed_terminal` below, completing a TERMINAL
        failed/cancelled row (`FAILED`, optionally with a `cancel_reason`)
        whose recorded work is verifiably on the base branch. The default
        CAS guard refuses ANY write to a row that reads FAILED with a
        `cancel_reason` — a deliberate protection against clobbering a
        human's explicit cancel — which is also, precisely, the write this
        reconciliation needs to make. Passing `terminal_reconcile=True`
        swaps the CAS `WHERE` for the tight form `id = ? AND status = ?`
        (the literal `FAILED` value): the write lands ONLY if the row is
        STILL `failed` at commit time — narrower than `human_override`
        (which skips the CAS entirely and can stomp any row in any state);
        this still refuses a row a concurrent human `retry`/`shipped` call
        already moved off `FAILED`. Ignored when `human_override=True` (that
        already bypasses the CAS outright). `SilentCompletion` (DONE
        requires an `event`) still applies unchanged.
        """
        if validate:
            try:
                assert_transition(task.status, new_status)
            except IllegalTransition:
                if reconciliation_gate is None:
                    raise
                reconciliation_gate(task.status)
        return await self._write_status(
            task, new_status, human_override=human_override, event=event,
            terminal_reconcile=terminal_reconcile)

    @serialized_write
    async def _write_status(
        self,
        task: Task,
        new_status: TaskStatus,
        *,
        human_override: bool = False,
        event: dict[str, Any] | None = None,
        terminal_reconcile: bool = False,
    ) -> Task | None:
        """The CAS write + DONE-event guard + phase recording shared by every
        validated transition. The only caller is `set_status` itself, which
        has already run its own legality check (the general map, optionally
        falling back to a caller-supplied `reconciliation_gate`) before
        reaching here — see `set_status`'s docstring for how
        `reconcile_landed_orphan` (below) uses that fallback to complete an
        orphan's IMPLEMENTING/TESTING -> DONE reconciliation without widening
        the general map (and, with it, `Orchestrator._advance_after_review`'s
        plain `set_status(task, target)` call, which must keep refusing any
        post-review target outside the two post-review states — see
        tests/test_post_review_transition_6408aba0.py). This method itself
        performs no legality check: it is not a public bypass, only the
        write tail `set_status` delegates to. Carries its own
        `@serialized_write` too — not for a legality reason, but because
        `test_every_committing_store_method_is_serialized` asserts every
        `self.db.commit()`-ing coroutine on `Store` is decorated, on the
        `Store.create_wiki_job`/`update_wiki_job` precedent (see
        `git log -S"async def _write_status"`): its one caller, `set_status`,
        is already decorated, so this nests into the SAME lock —
        `_critical()` is reentrant per (Store, owning asyncio task), see its
        own docstring — never a second, competing acquisition.

        `terminal_reconcile=True` (only `reconcile_landed_terminal` passes
        it) narrows the CAS `WHERE` to `id = ? AND status = ?` — see
        `set_status`'s docstring for why that specific, tighter clause is
        needed and how it still refuses a row a concurrent human write
        already moved off `FAILED`.
        """
        # Checked AFTER the caller's own transition check on purpose: an
        # illegally-shaped transition (e.g. CONTEXT -> DONE) must still raise
        # `IllegalTransition` from the caller — that is a different,
        # pre-existing invariant, and this guard must not shadow it. Only
        # once a DONE transition is otherwise legal does "did you bring an
        # event" apply.
        if new_status is TaskStatus.DONE and not (
            event and event.get("kind") and event.get("source")
        ):
            raise SilentCompletion(
                f"set_status({task.id[:8]}, DONE) requires event={{'kind':.., "
                "'source':..}} — a DONE transition must never write silently"
            )
        now = _now()
        # Captured BEFORE the UPDATE: the idempotent-rewrite branch below
        # (`status = ?` in the CAS WHERE) matches — and reports rowcount=1 —
        # even when the row is ALREADY at `new_status`, e.g. a second
        # `set_status(DONE, event=...)` call on a row already DONE. That is
        # a real SQL match but NOT a real transition, so `rowcount` alone
        # cannot gate the event insert below: a second completion event
        # would be recorded for a status that never actually changed.
        already_there = task.status is new_status
        was_awaiting_approval = task.status is TaskStatus.AWAITING_APPROVAL
        # Approval-integrity marker: a row LEAVING awaiting_approval to
        # anything other than a genuine completion (escalated/failed/blocked/
        # awaiting_input/paused_quota, or sent back to implementing) stamps
        # `approval_superseded_at` (write-once, guarded by the IS NULL check
        # below) so a stale `approved_at` from an earlier round can never
        # again read as "pending" once the task has moved on. `approved_at`
        # itself is left untouched — it stays an audit trail, not a live
        # flag. `done` is excluded on purpose: a completed merge is the
        # approval's success, not its supersession, and
        # `core/lanes.py::approval_pending` also gates on
        # `status == awaiting_approval`, so a DONE row's chip is already
        # suppressed regardless of this marker. This CASE runs on every
        # branch below (human_override / terminal_reconcile / default CAS)
        # so the fix applies no matter which write mode a given caller uses.
        supersede_case = (
            "context = CASE "
            "WHEN status = ? AND ? NOT IN (?, ?) "
            "AND json_extract(COALESCE(context, '{}'), '$.approved_at') IS NOT NULL "
            "AND json_extract(COALESCE(context, '{}'), '$.approval_superseded_at') IS NULL "
            "THEN json_set(COALESCE(context, '{}'), '$.approval_superseded_at', ?) "
            "ELSE context END"
        )
        supersede_params = (
            TaskStatus.AWAITING_APPROVAL.value, new_status.value,
            TaskStatus.AWAITING_APPROVAL.value, TaskStatus.DONE.value,
            now,
        )
        if human_override:
            cur = await self.db.execute(
                f"UPDATE tasks SET status = ?, updated_at = ?, {supersede_case} "
                "WHERE id = ?",
                (new_status.value, now, *supersede_params, task.id),
            )
        elif terminal_reconcile:
            cur = await self.db.execute(
                f"UPDATE tasks SET status = ?, updated_at = ?, {supersede_case} "
                "WHERE id = ? AND status = ?",
                (
                    new_status.value, now, *supersede_params,
                    task.id, TaskStatus.FAILED.value,
                ),
            )
        else:
            cur = await self.db.execute(
                f"UPDATE tasks SET status = ?, updated_at = ?, {supersede_case} "
                "WHERE id = ? AND ("
                "  status = ?"
                "  OR NOT ("
                "    status = ?"
                "    OR (status = ? AND json_extract(context, '$.cancel_reason') IS NOT NULL)"
                "  )"
                ")",
                (
                    new_status.value, now, *supersede_params, task.id,
                    new_status.value,
                    TaskStatus.DONE.value, TaskStatus.FAILED.value,
                ),
            )
        if cur.rowcount and event and not already_there:
            ev = dict(event)
            ev.setdefault("ts", time.time())
            await self.db.execute(
                "INSERT INTO task_events (task_id, ts, data) VALUES (?, ?, ?)",
                (task.id, ev["ts"], json.dumps(ev)),
            )
        await self.db.commit()
        if cur.rowcount == 0:
            row = await self._fetchone(
                "SELECT status FROM tasks WHERE id = ?", (task.id,)
            )
            if row is not None:
                log.warning(
                    "set_status: blocked %s -> %s on terminal row %s",
                    row["status"], new_status.value, task.id,
                )
                task.status = TaskStatus(row["status"])
            return None
        task.status = new_status
        task.updated_at = now
        if (
            not already_there
            and was_awaiting_approval
            and new_status not in (TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE)
            and task.context
            and task.context.get("approved_at")
            and not task.context.get("approval_superseded_at")
        ):
            # Best-effort mirror of the CASE-clause write above onto the
            # in-process object, so a caller reading `task.context` right
            # after this call (without a fresh SELECT) sees the marker
            # immediately instead of only after the next reload.
            task.context = {**task.context, "approval_superseded_at": now}
        # D1.2: record the per-phase timeline the drawer's "ran" chip reads
        # (active_seconds = Σ phase durations). set_status is the ONE writer of
        # task.status — every orchestrator/watcher/scheduler transition routes
        # here — so recording the phase boundary here is the whole "orchestrator
        # writes rows" instrumentation in one place, correct on resume/park by
        # construction. Best-effort and AFTER the status commit: a telemetry
        # failure must never fail or slow a real transition. `already_there`
        # skips an idempotent rewrite (rowcount=1 but no boundary crossed).
        if not already_there:
            await self._record_phase(task, new_status)
        return task

    @serialized_write
    async def reconcile_landed_orphan(
        self,
        task: Task,
        *,
        evidence: dict[str, Any],
        event: dict[str, Any],
    ) -> Task | None:
        """Move an orphaned-but-actually-landed row straight to DONE.

        Orphan recovery (`Scheduler._recover_orphans`) used to requeue/fail a
        row purely from its status, with no check for whether the attempt's
        PR merged or its commit already landed on the base branch — a dead
        or restarted server has no memory of that, but the base branch does.
        This is the reconciliation write: called ONLY after the caller has
        independently confirmed landedness via
        `vcs.pr_watcher.orphan_landed_evidence` (a local-git-only probe; no
        network call happens here or upstream of here).

        `evidence` must be the dict `orphan_landed_evidence` returned:
        truthy `sha`, `kind` in `{"commit", "pr"}`, truthy `base`. Anything
        else is a caller bug, not an ambiguous case — raise `ValueError`
        rather than silently doing nothing, so a malformed probe result
        cannot be mistaken for "no evidence, requeue as normal".

        `assert_landed_reconciliation(task.status)` is checked here, FIRST,
        as a fast pre-flight (`IMPLEMENTING`, `REVIEWING`, `TESTING`,
        `AWAITING_APPROVAL` only) — raises `IllegalTransition` for anything
        else, e.g. `CONTEXT`, which has no attempt to have landed in the
        first place, before either the evidence check or the context write
        below runs anything.

        The evidence is stamped into the context anchor BEFORE the status
        write (same pattern as `blockers/shipped.py`'s `landed_sha`
        bookkeeping) so a restart between the two writes does not lose it:
        `merge_context` is its own atomic UPDATE and commits independently
        of the status write below.

        The actual status write below goes through `self.set_status` —
        the SAME single validated write path every other transition in this
        Store uses, never `_write_status` directly. `set_status` runs its
        own `assert_transition` against the general `ALLOWED_TRANSITIONS`
        map FIRST and unconditionally: `IMPLEMENTING -> DONE` / `TESTING ->
        DONE` are NOT, and must not become, generally legal edges there, so
        that check still raises `IllegalTransition` exactly as it does for
        any other caller. Only because this call also names
        `reconciliation_gate=assert_landed_reconciliation` does `set_status`
        get a second, narrower, still-raising-on-failure chance to accept
        that SAME edge — see `set_status`'s docstring. The pre-flight check
        above means that fallback never actually has to reject anything
        here (an illegal source status already raised before this point),
        but the write itself is validated by the general map first, not by
        skipping it: widening the general map is still never required, and
        `Orchestrator._advance_after_review`'s plain
        `self.store.set_status(task, target)` call (no `reconciliation_gate`
        keyword, so no fallback) keeps refusing IMPLEMENTING/TESTING -> DONE
        exactly as before, which is what
        `tests/test_post_review_transition_6408aba0.py::
        test_recovery_never_launders_an_illegal_jump` pins. This is still a
        fully validated write, never a bypass: the call below names only
        `event=` and `reconciliation_gate=`, leaving every other keyword of
        `set_status` — including its terminal-row escape hatch — at its
        safe default, so `set_status` and `_write_status`'s own CAS guard
        and `SilentCompletion` event check both still apply exactly as they
        do for any other DONE transition — a concurrent human cancel or a
        missing `event` still refuses the write.
        """
        assert_landed_reconciliation(task.status)
        sha = evidence.get("sha")
        kind = evidence.get("kind")
        base = evidence.get("base")
        if not sha or kind not in ("commit", "pr") or not base:
            raise ValueError(
                f"reconcile_landed_orphan: malformed evidence {evidence!r}")
        await self.merge_context(task.id, {"landed_sha": sha})
        return await self.set_status(
            task, TaskStatus.DONE, event=event,
            reconciliation_gate=assert_landed_reconciliation,
        )

    @serialized_write
    async def reconcile_landed_terminal(
        self, task: Task, *, evidence: dict[str, Any], event: dict[str, Any],
    ) -> Task | None:
        """The TERMINAL-row twin of `reconcile_landed_orphan`.

        A row that already went FAILED (with or without a `cancel_reason` —
        there is no separate CANCELLED status) but whose recorded work is
        provably reachable from the base branch is completed to DONE here,
        through `assert_terminal_landed_reconciliation` — the same shape of
        validated, evidence-gated transition as the orphan reconciler, never
        a bypass. `terminal_reconcile=True` narrows `_write_status`'s CAS
        guard to require the row still be exactly FAILED at write time, so a
        concurrent human action (or a second sweep) can't clobber or
        double-apply this.
        """
        assert_terminal_landed_reconciliation(task.status)
        sha = evidence.get("sha")
        kind = evidence.get("kind")
        base = evidence.get("base")
        if not sha or kind not in ("commit", "pr") or not base:
            raise ValueError(
                f"reconcile_landed_terminal: malformed evidence {evidence!r}")
        await self.merge_context(
            task.id, {"landed_sha": sha, "landed_reconciled_from": "failed"})
        return await self.set_status(
            task, TaskStatus.DONE, event=event,
            reconciliation_gate=assert_terminal_landed_reconciliation,
            terminal_reconcile=True,
        )

    async def landed_reconcilable_terminal_tasks(
        self, limit: int = 200) -> list[Task]:
        """Bounded candidate set for `Scheduler._reconcile_landed_terminal`:
        FAILED rows (cancelled or not) that haven't already been reconciled,
        that carry a `repo_path` to probe and a recorded `base_branch` — a
        row without one is never guessed at and is skipped untouched (see
        `context["base_branch"]` requirement)."""
        rows = await self._fetchall(
            "SELECT * FROM tasks "
            "WHERE status = ? "
            "  AND json_extract(context,'$.landed_sha') IS NULL "
            "  AND json_extract(context,'$.landed_override_sha') IS NULL "
            "  AND COALESCE(TRIM(repo_path),'') <> '' "
            "  AND COALESCE(json_extract(context,'$.base_branch'),'') <> '' "
            "ORDER BY updated_at DESC, rowid DESC "
            "LIMIT ?",
            (TaskStatus.FAILED.value, limit),
        )
        return [Task.from_row(dict(r)) for r in rows]

    @serialized_write
    async def update_task(self, task: Task) -> Task:
        """Persist the full mutable surface of a task row — EXCEPT status.

        Status has exactly one writer: ``set_status`` (which validates
        transitions and CAS-guards terminal rows, SCRUM-73). This method
        used to write the handle's status too, guarded only for terminal
        rows — so any caller holding a stale snapshot across an ``await``
        could revert a LIVE task's advance. That is the 2026-08-09 demo
        incident (R15): a tracker poller's write-back, snapshotted before
        slow network calls, reverted awaiting_approval back to reviewing;
        the row then sat in a worker-only status with no worker attached,
        invisible until the next restart re-ran the whole task and opened
        a duplicate PR. The status column is therefore never touched here;
        the handle's ``.status`` is refreshed to the row's truth below so
        callers keep reasoning about reality. Every other column still
        writes normally, so e.g. the Jira poller can keep updating context
        write-back markers on an already-DONE row.

        A human's terminal cancel marker (``context.cancel_reason``) gets the
        same stale-handle protection as status: the CASE below carries the
        LIVE row's ``$.cancel_reason`` forward over whatever the in-memory
        `task` copy had, so a still-running attempt's next `update_task(task)`
        — snapshotted before the cancel landed — cannot silently erase it
        (that race was the actual loss the cancel_session_not_found path hit:
        no live backend task to interrupt, so the attempt kept running and its
        next write-back stomped the field back to absent). The only sanctioned
        way to clear it is an explicit `merge_context({"cancel_reason": None})`
        (used by retry), which this does not affect since it targets a
        different UPDATE.
        """
        task.updated_at = _now()
        row = task.to_row()
        await self.db.execute(
            """UPDATE tasks SET
                 external_id=:external_id, source=:source, title=:title,
                 description=:description, requirements=:requirements,
                 acceptance_criteria=:acceptance_criteria, repo_path=:repo_path,
                 kind=:kind, parent_id=:parent_id, follows_id=:follows_id,
                 blocker=:blocker, wake_check_at=:wake_check_at,
                 priority=:priority,
                 context = json_patch(
                     :context,
                     CASE WHEN json_extract(COALESCE(context, '{}'), '$.cancel_reason')
                               IS NOT NULL
                          THEN json_object(
                              'cancel_reason',
                              json_extract(COALESCE(context, '{}'), '$.cancel_reason'))
                          ELSE '{}' END),
                 plan=:plan, config=:config,
                 updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        # Deliberately NOT `UPDATE … RETURNING status`. A writer that has
        # produced a row leaves its VDBE live between `execute()` and the fetch,
        # and every `await` in that gap is a scheduling point: SQLite refuses any
        # COMMIT while a write statement is in progress ("cannot commit
        # transaction - SQL statements in progress"). That was this method's
        # half of KI-1. The write lock alone would close it, but only while the
        # ONLY thing that can reach this connection is a lock-taking Store
        # method, and only because CPython's refcounting happens to finalize the
        # abandoned cursor if this frame unwinds (a cancellation, an exception) —
        # measured, not assumed, but an implementation detail no invariant should
        # rest on. A plain UPDATE parks nothing. The read-back below is inside
        # the same uncommitted transaction and the same critical section, so it
        # observes exactly what RETURNING did.
        result = await self._fetchone(
            "SELECT status FROM tasks WHERE id = ?", (task.id,)
        )
        # A second read-back, same critical section, same uncommitted
        # transaction as the one above — kept as its own statement (rather
        # than folded into the query above) so that query's exact SQL text
        # stays the one `tests/test_db_concurrency.py` parks writers on.
        ctx_result = await self._fetchone(
            "SELECT context FROM tasks WHERE id = ?", (task.id,)
        )
        await self.db.commit()
        if result is not None and result["status"] != row["status"]:
            log.info(
                "update_task: handle held stale status %s; row is %s (%s)",
                row["status"], result["status"], task.id,
            )
            task.status = TaskStatus(result["status"])
        if ctx_result is not None:
            task.context = json.loads(ctx_result["context"]) if ctx_result["context"] else {}
        return task

    @serialized_write
    async def merge_context(self, task_id: str, patch: dict) -> dict:
        """Atomically merge *patch* into the task's context (RFC 7396).

        The lost-update fix for concurrent context writers: `update_task`
        rewrites the whole context blob from a Task copy, so the watcher, the
        CLI and the orchestrator (different coroutines AND different
        processes) clobber each other — whichever flushes last wins (the
        cancel_requested column above documents the same failure). A single
        `json_patch` UPDATE is atomic under SQLite's write serialization, so
        concurrent merges of different keys both survive, across processes.

        Semantics (RFC 7396): nested dicts merge recursively; lists/scalars
        replace; a ``None`` value DELETES the key. Returns the merged context.
        """
        await self.db.execute(
            """UPDATE tasks SET
                 context = json_patch(COALESCE(context, '{}'), ?),
                 updated_at = ?
               WHERE id = ?""",
            (json.dumps(patch), _now(), task_id),
        )
        await self.db.commit()
        row = await self._fetchone(
            "SELECT context FROM tasks WHERE id = ?", (task_id,))
        return json.loads(row[0]) if row and row[0] else {}

    async def record_cancel_reason(self, task_id: str, reason: str) -> dict:
        """Write the one discriminator every reader filters cancels on.

        Every cancel path — the API handler, the CLI's own re-label branch,
        and the orchestrator's hard-cancel unwind — must call this, not
        `merge_context` directly, regardless of which status it cancels from
        or whether a live in-process session was found to interrupt. One
        write, one field (`context.cancel_reason`), one reader
        (`db.py`'s CAS predicate, `metrics.py`'s failure query,
        `api/models.py`'s `cancelled` flag): a cancel that only appends an
        event and skips this write is indistinguishable from a genuine
        failure everywhere that matters. The event-stream write (`human_cancel`,
        `cancel_stopped_session`/`cancel_session_not_found`) is additive and
        unaffected by this method — it stays exactly where it is.
        """
        return await self.merge_context(task_id, {"cancel_reason": reason})

    _MERGE_CLAIM_STALE_S = 1800  # a crashed server must not wedge the button forever

    @serialized_write
    async def claim_merge(self, task_id: str) -> bool:
        """Atomically claim the merge lock for *task_id*.

        One UPDATE, guarded by a WHERE clause that only matches when no
        claim is held (or the held claim is older than
        ``_MERGE_CLAIM_STALE_S``) — SQLite's write serialization (see
        `merge_context`) makes this a real CAS across coroutines *and*
        processes, which is what makes a second concurrent `approve` see a
        409 instead of a second land. Returns whether the claim was taken.
        """
        now = time.time()
        cur = await self.db.execute(
            """UPDATE tasks SET
                 context = json_set(COALESCE(context, '{}'),
                                    '$.merge_in_progress', ?),
                 updated_at = ?
               WHERE id = ?
                 AND COALESCE(json_extract(context, '$.merge_in_progress'), 0)
                     < ?""",
            (now, _now(), task_id, now - self._MERGE_CLAIM_STALE_S),
        )
        await self.db.commit()
        return bool(cur.rowcount)

    async def release_merge(self, task_id: str) -> None:
        """Release the merge lock claimed by `claim_merge` — deletes the
        `merge_in_progress` key (RFC 7396 `None`-deletes), via the same
        atomic merge path as every other context writer."""
        await self.merge_context(task_id, {"merge_in_progress": None})

    @serialized_write
    async def append_context_list(self, task_id: str, key: str, item: dict) -> None:
        """Atomically append *item* to the context list at *key* (created if
        absent). List appends cannot be expressed as a merge patch (RFC 7396
        replaces arrays wholesale), so this uses json_set's '[#]' append —
        one UPDATE, no read-modify-write."""
        assert "." not in key and "[" not in key, "flat keys only"
        await self.db.execute(
            f"""UPDATE tasks SET
                 context = json_set(
                   json_patch(COALESCE(context, '{{}}'),
                              CASE WHEN json_extract(COALESCE(context,'{{}}'),
                                        '$.{key}') IS NULL
                                   THEN json_object('{key}', json_array())
                                   ELSE '{{}}' END),
                   '$.{key}[#]', json(?)),
                 updated_at = ?
               WHERE id = ?""",
            (json.dumps(item), _now(), task_id),
        )
        await self.db.commit()

    @serialized_write
    async def update_task_columns(self, task: Task) -> Task:
        """Persist the task's mutable columns EXCEPT context and status.
        Multi-writer zones (watcher, CLI, gate) must write context only via
        merge_context/append_context_list — this companion writes the rest
        without clobbering concurrent context merges with a stale blob.
        Status is excluded for the same reason as in ``update_task`` (R15):
        ``set_status`` is the only status writer; a stale handle here had
        no terminal guard at all."""
        task.updated_at = _now()
        row = task.to_row()
        await self.db.execute(
            """UPDATE tasks SET
                 external_id=:external_id, source=:source, title=:title,
                 description=:description, requirements=:requirements,
                 acceptance_criteria=:acceptance_criteria, repo_path=:repo_path,
                 kind=:kind, parent_id=:parent_id, follows_id=:follows_id,
                 blocker=:blocker, wake_check_at=:wake_check_at,
                 priority=:priority, plan=:plan, config=:config,
                 updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        await self.db.commit()
        return task

    @serialized_write
    async def request_cancel(self, task_id: str, reason: str) -> None:
        """Ask a running task to stop at its next cooperative checkpoint.

        A targeted UPDATE of one column: it must not read-modify-write the task
        row, or it would race the orchestrator that owns every other column.
        """
        await self.db.execute(
            "UPDATE tasks SET cancel_requested = ? WHERE id = ?", (reason, task_id)
        )
        await self.db.commit()

    async def get_cancel_request(self, task_id: str) -> str | None:
        """The pending cancellation reason for *task_id*, or None."""
        row = await self._fetchone(
            "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
        )
        return row["cancel_requested"] if row else None

    @serialized_write
    async def clear_cancel_request(self, task_id: str) -> None:
        """Drop a pending cancellation, once honoured or withdrawn."""
        await self.db.execute(
            "UPDATE tasks SET cancel_requested = NULL WHERE id = ?", (task_id,)
        )
        await self.db.commit()

    async def list_subtasks(self, parent_id: str) -> list[Task]:
        """Return all sub-tasks of a compound parent task."""
        rows = await self._fetchall(
            "SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at",
            (parent_id,),
        )
        return [Task.from_row(dict(r)) for r in rows]

    async def count_subtasks(self, parent_id: str) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM tasks WHERE parent_id = ?", (parent_id,)
        )
        return int(row["n"]) if row else 0

    async def done_rate_by_tier(self, *, min_sample: int = 10) -> dict[str, int]:
        """This install's measured done-rate (percent, 0-100) per complexity
        tier, over terminal tasks that recorded a tier. Calibration for the
        pre-flight feasibility hint (``core/feasibility.py``): a task's shown
        rate is THIS install's own history, not a hardcoded constant. A tier
        with fewer than ``min_sample`` terminal tasks is omitted rather than
        reported on noise. Read-only, cheap (one grouped scan)."""
        rows = await self._fetchall(
            "SELECT json_extract(context, '$.complexity_tier') AS tier, "
            "       COUNT(*) AS n, "
            "       SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done "
            "FROM tasks "
            "WHERE status IN ('done', 'failed', 'escalated') "
            "  AND json_extract(context, '$.complexity_tier') IS NOT NULL "
            "GROUP BY tier"
        )
        out: dict[str, int] = {}
        for row in rows:
            n = int(row["n"] or 0)
            if n >= min_sample:
                out[str(row["tier"])] = round(100 * int(row["done"] or 0) / n)
        return out

    # ---------------------------- attempts --------------------------------- #

    @serialized_write
    async def create_attempt(self, task_id: str, attempt_number: int, *,
                              mechanical: bool = False) -> str:
        # Read BEFORE the UPDATE below opens a write transaction. `connect()`
        # has already warmed this, so it is a cached attribute read — but
        # ordering it here means even a cold cache cannot shell out to git
        # while the write lock is held.
        from .build_info import loaded_code
        code_version = loaded_code().descriptor
        # An earlier attempt of this task still 'in_progress' cannot be running:
        # attempts are serial, so a new one starting means the old process died
        # (kill -9, crash) without ever closing its row. Left alone, those rows
        # make `attempts.status` untrustworthy as a completion signal — the
        # baseline had three of them. Close them for what they are.
        await self.db.execute(
            "UPDATE attempts SET status = 'interrupted', "
            "failure_reason = COALESCE(NULLIF(TRIM(failure_reason), ''), "
            "'interrupted: superseded by a newer attempt — the prior worker "
            "process died without closing its row') "
            "WHERE task_id = ? AND status = 'in_progress' AND attempt_number < ?",
            (task_id, attempt_number),
        )
        attempt_id = uuid.uuid4().hex
        # Stamped HERE, at the single chokepoint every attempt passes through,
        # rather than at each of the orchestrator's three creation sites — a
        # site added later would otherwise silently record nothing, and an
        # attempt with no provenance is exactly the row this exists to prevent.
        # `code_version` was resolved above, outside the write transaction.
        await self.db.execute(
            "INSERT INTO attempts (id, task_id, attempt_number, "
            "loaded_code_version, mechanical_round) VALUES (?, ?, ?, ?, ?)",
            (attempt_id, task_id, attempt_number, code_version,
             1 if mechanical else 0),
        )
        await self.db.commit()
        return attempt_id

    @serialized_write
    async def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        if not fields:
            return
        # Observability backstop (C2): a failed attempt with no stated reason
        # is undiagnosable — task 6cfdb936 burned attempts on exactly that.
        # When the caller marks failed without a reason AND the row has none,
        # stamp a loud sentinel instead of leaving silence. Never clobbers a
        # reason set by an earlier update.
        if fields.get("status") == "failed" and not fields.get("failure_reason"):
            fields.pop("failure_reason", None)
            row = await self._fetchone(
                "SELECT COALESCE(failure_reason, '') FROM attempts WHERE id = ?",
                (attempt_id,))
            if row is not None and not row[0].strip():
                fields["failure_reason"] = (
                    "(no failure reason recorded — observability gap; "
                    "report which stage failed silently)")
        # JSON-encode dict/list values transparently.
        clean = {
            k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in fields.items()
        }
        assignments = ", ".join(f"{k} = :{k}" for k in clean)
        clean["id"] = attempt_id
        await self.db.execute(
            f"UPDATE attempts SET {assignments} WHERE id = :id", clean
        )
        await self.db.commit()

    # --- task_phases: the per-phase timeline (PR-D1-3 / D1.1) ----------------
    # One row per phase entry. `open_phase` starts a phase (closing any still-
    # open phase of that task as 'superseded' first, so a task has at most one
    # open phase); `close_phase` stamps the outcome/reason on that open row.
    # `active_seconds` sums (ended_at - started_at) over closed rows plus
    # (now - started_at) for the open one — parked time is never inside a row
    # because parking closes the phase (D1.2), so it is excluded by construction.
    # `set_status` writes these rows (D1.2): a real transition into an active
    # working state opens the matching phase; every other state closes the open
    # one. `_STATUS_PHASE` is that map (the CHECK constraint's phase names). Only
    # the five active states have a phase — awaiting_approval/blocked/paused/
    # escalated/done/failed close it, so human-wait and parked time never land
    # inside a row. There is deliberately NO 'pr' phase writer: PR-open work
    # happens under the code/test states, and a separate 'pr' phase would only
    # split the timeline finer than the "ran vs wall" chip needs.
    _STATUS_PHASE: dict["TaskStatus", str] = {
        TaskStatus.CONTEXT: "intake",
        TaskStatus.PLANNING: "plan",
        TaskStatus.IMPLEMENTING: "code",
        TaskStatus.REVIEWING: "review",
        TaskStatus.TESTING: "test",
    }

    async def _record_phase(self, task: "Task", new_status: "TaskStatus") -> None:
        """Best-effort phase-boundary write for a task that just transitioned.

        Called from `set_status` AFTER the status commit. Any failure here is
        swallowed — the per-phase timeline is telemetry and must never fail or
        slow a real status transition. Re-entrant on the write lock (same task,
        same Store) via `Store._critical`, so nesting inside `set_status`'s
        serialized write is safe."""
        try:
            phase = self._STATUS_PHASE.get(new_status)
            if phase is None:
                # park / terminal / awaiting-approval: close the open phase (if
                # any). A crash between transitions leaves a phase open; the next
                # transition — or the orphan sweep's FAILED write — closes it, so
                # active_seconds counts it to `now` only while genuinely live.
                await self.close_phase(task.id, new_status.value)
                return
            row = await self._fetchone(
                "SELECT COALESCE(MAX(attempt_number), 0) AS n "
                "FROM attempts WHERE task_id = ?",
                (task.id,),
            )
            attempt = int(row["n"]) if row else 0
            await self.open_phase(task.id, attempt, phase)
        except Exception:  # noqa: BLE001 — telemetry must not break a transition
            log.debug("phase recording failed: %s -> %s",
                      task.id, new_status, exc_info=True)

    @serialized_write
    async def open_phase(self, task_id: str, attempt: int, phase: str) -> int:
        now = _now()
        # At most one open phase per task: close any still-open row first.
        await self.db.execute(
            "UPDATE task_phases SET ended_at = ?, outcome = 'superseded' "
            "WHERE task_id = ? AND ended_at IS NULL",
            (now, task_id),
        )
        cur = await self.db.execute(
            "INSERT INTO task_phases (task_id, attempt, phase, started_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, attempt, phase, now),
        )
        await self.db.commit()
        return cur.lastrowid

    @serialized_write
    async def close_phase(self, task_id: str, outcome: str,
                          reason: str = "") -> None:
        # Close the currently-open phase (there is at most one). A close with no
        # open phase is a no-op — a park may already have closed it.
        await self.db.execute(
            "UPDATE task_phases SET ended_at = ?, outcome = ?, reason = ? "
            "WHERE task_id = ? AND ended_at IS NULL",
            (_now(), outcome, reason, task_id),
        )
        await self.db.commit()

    async def phases_for(self, task_id: str) -> list[dict]:
        rows = await self._fetchall(
            "SELECT * FROM task_phases WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        return [dict(r) for r in rows]

    async def active_seconds(self, task_id: str) -> float:
        rows = await self._fetchall(
            "SELECT started_at, ended_at FROM task_phases WHERE task_id = ?",
            (task_id,),
        )
        total = 0.0
        # An open row (ended_at IS NULL) is counted up to the SAME clock that
        # stamps started_at/ended_at (`_now()`), so a frozen/injected clock is
        # honored end-to-end and the "still running" phase reads consistently.
        for r in rows:
            start = datetime.fromisoformat(r["started_at"])
            end = datetime.fromisoformat(r["ended_at"] or _now())
            total += (end - start).total_seconds()
        return total

    @serialized_write
    async def add_attempt_usage(self, attempt_id: str, **fields: int | None) -> None:
        """ADD to the five usage columns on one attempt row, instead of
        `update_attempt`'s SET.

        Exists for a round that bills onto a row a PRIOR write already
        populated (the repro-gate corrective round: the coder turn's
        authoritative numbers are on the row before the gate ever runs, and
        this round's numbers are a second, later measurement of the SAME
        attempt, not a replacement for the first). Whitelisted to the usage
        columns only — this is not a general-purpose additive `update_attempt`.

        `None` values are ignored, not coerced to 0: an unreported
        `output_tokens` stays NULL (the "the split was never captured for this
        write" state `update_attempt`'s own docs describe), rather than a 0
        that would read as "this round captured a split of exactly zero".

        Deliberately NOT idempotent — a retried call double-bills. Callers
        invoke this exactly once per corrective round.
        """
        allowed = ("turns_used", "tokens_used", "output_tokens",
                   "cache_read_tokens", "cache_creation_tokens")
        clean = {k: v for k, v in fields.items()
                 if k in allowed and v is not None}
        if not clean:
            return
        assignments = ", ".join(
            f"{k} = COALESCE({k}, 0) + :{k}" for k in clean
        )
        clean["id"] = attempt_id
        await self.db.execute(
            f"UPDATE attempts SET {assignments} WHERE id = :id", clean
        )
        await self.db.commit()

    async def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_number",
            (task_id,),
        )
        return [dict(r) for r in rows]

    async def latest_open_attempt(self, task_id: str) -> dict[str, Any] | None:
        """The attempt that DIED: the most recently STARTED row still
        ``in_progress``, or None.

        Not `list_attempts()[-1]`, and not `reversed(list_attempts(...))`.
        That list is ``ORDER BY attempt_number``, which is not recency: the
        live DB holds task ``61406d02`` with rows (1, failed, 88a1ea35),
        (2, in_progress, 75c68e08) and then (1, in_progress, —) inserted
        fifteen minutes AFTER the 2 — a lower number written later — and
        ``46fe6b92`` with four rows all numbered 1. Both came from a
        `create_attempt` call site that passed a hardcoded 1 while
        `create_attempt` closes only ``attempt_number < ?``, so it closed
        nothing. Ordering by the number would have handed a caller asking
        "which attempt just died" the row from forty-five minutes earlier;
        rows TIED at one number resolve by SQLite's unspecified within-tie
        order, which is not an answer at all.

        ``started_at`` is second-resolution (``datetime('now')``), so ``rowid``
        breaks the tie — it is insertion order, and attempts are never deleted.
        """
        row = await self._fetchone(
            "SELECT * FROM attempts WHERE task_id = ? AND status = 'in_progress' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        )
        return dict(row) if row else None

    async def latest_review_attempt(self, task_id: str) -> dict[str, Any] | None:
        """The NEWEST attempt row that recorded a review verdict, or None if
        no attempt ever recorded one.

        Newest by ``(started_at, rowid)`` — NOT ``attempt_number``, for the
        exact reason `latest_open_attempt` above documents. ``rowid`` is
        exposed as ``_rowid`` (``SELECT *`` omits the rowid pseudo-column)
        because callers compare recency against `latest_failed_attempt`'s
        row, and attempts are never deleted so it is a stable insertion
        order to compare on.
        """
        row = await self._fetchone(
            "SELECT rowid AS _rowid, * FROM attempts WHERE task_id = ? "
            "AND review_passed IS NOT NULL "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1", (task_id,))
        return dict(row) if row else None

    async def latest_failed_attempt(self, task_id: str) -> dict[str, Any] | None:
        """The NEWEST attempt row with a non-empty ``failure_reason``, or
        None if no attempt ever recorded one.

        Same ``(started_at, rowid)`` recency as `latest_review_attempt`, so
        the two can be compared directly to tell whether a task's most
        recent event was a review verdict or a later, unrelated failure
        (e.g. tests failing after review already PASSed). ``NULL`` and
        ``''`` both collapse to absent, matching `latest_attempt_pr_url` /
        `latest_attempt_branch` below — a blank reason quoted verbatim would
        read as "last failure: " with nothing after it.
        """
        row = await self._fetchone(
            "SELECT rowid AS _rowid, * FROM attempts WHERE task_id = ? "
            "AND COALESCE(TRIM(failure_reason), '') <> '' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1", (task_id,))
        return dict(row) if row else None

    async def latest_review_verdict(self, task_id: str) -> int | None:
        """The NEWEST recorded review verdict for the task (1 = PASS, 0 =
        FAIL), or None if no attempt ever recorded one.

        Delegates to `latest_review_attempt` so there is ONE ordering to
        maintain — a later review FAIL must re-arm the lifetime budget cap
        after an earlier PASS, so "latest", not "any PASS ever", is what
        `orchestrator._mechanical_round` and `_check_lifetime_budget` need
        here.
        """
        row = await self.latest_review_attempt(task_id)
        return None if row is None else int(row["review_passed"])

    async def latest_attempt_pr_url(self, task_id: str) -> str:
        """The newest non-empty ``attempts.pr_url`` for *task_id*, or ``""``.

        Ordered ``started_at DESC, rowid DESC`` — NOT ``attempt_number`` — for
        the exact reason `latest_open_attempt` above documents: the live DB
        holds rows where a lower attempt_number was inserted AFTER a higher
        one, and rows tied at one number whose order SQLite does not define.
        ``attempt_number`` answers "which attempt is this", not "which
        happened most recently"; recency is what "inherit the PR from the run
        that actually opened one" needs. ``NULL`` and ``''`` are both treated
        as absent (``COALESCE(TRIM(pr_url), '')  <> ''``).
        """
        row = await self._fetchone(
            "SELECT pr_url FROM attempts WHERE task_id = ? "
            "AND COALESCE(TRIM(pr_url), '') <> '' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        )
        return str(row[0]).strip() if row else ""

    async def latest_attempt_branch(self, task_id: str) -> dict[str, str]:
        """The newest recorded ``branch_name``/``commit_sha`` pair for
        *task_id*, or ``{"branch": "", "commit_sha": ""}`` if no attempt ever
        recorded a branch — mirrors `latest_attempt_pr_url` so a task's
        content stays locatable even when ``pr_branch`` was never written
        (the pre-PR failed shape `blockers/landed_override.py` resolves).

        Ordered ``started_at DESC, rowid DESC`` — NOT ``attempt_number`` —
        for the exact reason `latest_open_attempt` documents. ``NULL`` and
        ``''`` both collapse to absent.
        """
        row = await self._fetchone(
            "SELECT branch_name, commit_sha FROM attempts WHERE task_id = ? "
            "AND COALESCE(TRIM(branch_name), '') <> '' "
            "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        )
        if row is None:
            return {"branch": "", "commit_sha": ""}
        return {
            "branch": str(row[0] or "").strip(),
            "commit_sha": str(row[1] or "").strip(),
        }

    @serialized_write
    async def close_open_attempts(
        self, task_id: str, *, reason: str | None = None,
    ) -> None:
        """Retire every ``in_progress`` row of *task_id* — the caller has just
        declared the run that owned them over.

        ``reason`` is the ``failure_reason`` written onto rows that have none.
        The default names the checkpoint-clearing case described below; a
        graceful server stop (`Orchestrator._honor_server_stop`) passes its
        own, because a row closed by a shutdown is NOT a row whose checkpoint
        was cleared — the next run resumes from it, and the reason column is
        the only place that says so.

        Called by the CHECKPOINT-CLEARING paths (`POST /tasks/{id}/retry`,
        `nh task retry`, and both "send back" twins), all of which promise "a
        fresh run branches from base". The criterion that picks those four is
        INTENT TO DISCARD, not sha-lessness: a sha-less ``resume_from``
        describes ten sites, and the other six pass a VARIABLE checkpoint
        and are CONTINUE re-entries, where
        the sweep re-deriving a sha is the designed rescue under
        ``by: "orphan_recovery"`` with the zero-diff gate armed — so retiring
        their rows would break them, not protect them.
        Clearing ``resume_from`` alone did not keep that promise:
        the orphan sweep re-derives a checkpoint from the attempt that died,
        and a row left ``in_progress`` by a crashed worker is still exactly
        that row — so the first sweep after ANY clear put the cleared
        checkpoint straight back, from a run that had already failed.

        THE DISTINCTION HAS TO BE MADE HERE, not in the sweep. A context with
        no ``resume_from`` looks identical whether a human deliberately cleared
        it or it never had one, and the sweep has nothing else to read. What
        actually changed at the clear is that the previous run's rows stopped
        being resumable, and `attempts.status` is where that already belongs —
        `create_attempt` closes stale rows for the same reason and in the same
        words. A clear therefore leaves NO open row to reach back past, while
        an attempt started AFTER it is open and is inherited normally: the
        tombstone expires by itself, with no timestamp arithmetic and no
        second source of truth.

        Safe to call when nothing is running (every clear path runs on a task
        whose worker is already gone) and idempotent.
        """
        default = ("interrupted: its checkpoint was cleared for a fresh run "
                   "from base — the worker process had already died without "
                   "closing this row")
        await self.db.execute(
            "UPDATE attempts SET status = 'interrupted', "
            "failure_reason = COALESCE(NULLIF(TRIM(failure_reason), ''), ?) "
            "WHERE task_id = ? AND status = 'in_progress'",
            (reason or default, task_id),
        )
        await self.db.commit()

    @serialized_write
    async def close_attempts_of_terminal_tasks(self) -> int:
        """Retire ``in_progress`` attempt rows whose TASK has already finished.

        `close_open_attempts` above is per-task and is called by the paths that
        DISCARD a checkpoint. Nothing was reconciling the other shape: a task
        reaches `done`/`failed` while an attempt row is still `in_progress`, and
        `Scheduler._recover_orphans` cannot see it because that sweep iterates
        `_ORPHANABLE`, which holds only NON-terminal task statuses. The rows
        therefore live forever. Measured 2026-08-11: **42** of them, the oldest
        `in_progress` for 32 days.

        They are not cosmetic. Three costs, all observed:
          * A task hid behind one for NINE DAYS (`a8ffc957`) — the board and
            `nh task cancel` both read it as still running.
          * `nh task cancel` itself creates them: it marks the task FAILED and
            leaves the row open (reproduced on 2 of 5 tasks cancelled that day).
          * They corrupt any query that windows events by attempt, because a row
            with `completed_at IS NULL` coalesces to *now* and its window
            swallows every later event. That silently inflated a measurement I
            was using to diagnose a live regression.

        STARTUP ONLY, and that is the safety argument, not a convenience: at
        startup no worker owns any row, which is exactly the precondition
        `close_open_attempts` documents ("safe to call when nothing is
        running"). Do NOT move this to the per-tick sweep — `latest_open_attempt`
        is load-bearing for orphan recovery, and closing a row out from under a
        live worker would break the resume it exists to protect.

        The status filter is the whole safety property: a task still in flight
        keeps its open row untouched. Returns the number of rows retired so the
        caller can say so out loud rather than reconciling in silence.
        """
        cur = await self.db.execute(
            "UPDATE attempts SET status = 'interrupted', "
            "failure_reason = COALESCE(NULLIF(TRIM(failure_reason), ''), "
            "'interrupted: its task had already finished while this row was "
            "left open — the worker died without closing it') "
            "WHERE status = 'in_progress' AND task_id IN ("
            "  SELECT id FROM tasks WHERE status IN ('done', 'failed'))",
        )
        await self.db.commit()
        return int(cur.rowcount or 0)

    @serialized_write
    async def add_verification_receipt(self, attempt_id: str, receipt: Any) -> None:
        """Append one verification receipt to *attempt_id*.

        DELIBERATELY AN INSERT, not a field on the attempt row. `update_attempt`
        writes `SET <col> = :<col>` — a whole-column REPLACE with no merge — so
        a JSON column of receipts would have to be read-modify-written on every
        captured tool call while `test_results`, the token counters and
        `ci_status` are being written to the same row. Appending sidesteps that
        race entirely: nothing this method writes can be clobbered by a later
        update to a different column.
        """
        await self.db.execute(
            "INSERT INTO verification_receipts (id, attempt_id, seq, kind, "
            "command, output_excerpt, output_bytes, truncated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                attempt_id,
                int(getattr(receipt, "seq", 0)),
                str(receipt.kind),
                str(receipt.command),
                str(receipt.output_excerpt),
                int(getattr(receipt, "output_bytes", 0)),
                1 if getattr(receipt, "truncated", False) else 0,
            ),
        )
        await self.db.commit()

    async def list_verification_receipts(self, attempt_id: str) -> list[dict[str, Any]]:
        """Every receipt for *attempt_id*, in execution order."""
        rows = await self._fetchall(
            "SELECT * FROM verification_receipts WHERE attempt_id = ? "
            "ORDER BY seq, created_at",
            (attempt_id,),
        )
        return [dict(r) for r in rows]

    async def attempts_by_task(self) -> dict[str, list[dict[str, Any]]]:
        """All attempts, grouped by task — ONE query.

        B2 #16: the board issued list_attempts PER TASK, every 2 seconds, per
        connected socket (an N+1 over the whole task history on every tick).
        """
        all_rows = await self._fetchall(
            "SELECT * FROM attempts ORDER BY task_id, attempt_number")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in all_rows:
            row = dict(r)
            grouped.setdefault(row["task_id"], []).append(row)
        return grouped

    async def count_attempts(self, task_id: str) -> int:
        """Raw row count — how many attempt rows exist for this task,
        INCLUDING infra-classified ones (`infra_failure = 1`), mechanical
        rounds (`mechanical_round = 1`), and dead interrupted rows that
        burned zero priced work (`status = 'interrupted'` AND
        `_zero_priced_work_sql()`).

        NOT the budget counter: none of those three shapes did real work, so
        none of them may consume a lifetime attempt — but the row is never
        skipped here, because it is the only durable record of what
        happened. See `lifetime_usage_by_class`, which is what
        `_check_lifetime_budget` / `_at_lifetime_ceiling` actually read for
        the budget-relevant count.
        """
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM attempts WHERE task_id = ?", (task_id,)
        )
        return int(row["n"]) if row else 0

    async def attempt_counts(self) -> dict[str, int]:
        """`task_id -> number of attempt rows`, in ONE grouped query.

        For the claim-ranking check ("has this pending task been tried
        before?") — avoids either an N-query loop over every pending task or
        hydrating the full `attempts_by_task()` payload just to count rows.
        """
        rows = await self._fetchall(
            "SELECT task_id, COUNT(*) AS n FROM attempts GROUP BY task_id"
        )
        return {str(r["task_id"]): int(r["n"]) for r in rows}

    # The named roles the attempts table meters, and the three token columns
    # each one carries. `eval/northstar.py` already sums exactly this set to
    # report cost; the budget gate below matches it, so the two can no longer
    # disagree about what a task spent.
    #
    # DERIVED from the module-level `USAGE_ROLES` registry, never re-typed:
    # this list was four literals in six different files, and the last time a
    # role was added (planning) four of the six kept summing three. Adding a
    # role to the registry now widens every one of them at once.
    _USAGE_TIERS = tuple(USAGE_ROLES)

    @classmethod
    def _usage_columns_by_class(cls) -> dict[str, tuple[str, ...]]:
        """The same addend columns, grouped by PRICE CLASS rather than by role.

        Every role bills at the same three rates, so the classes — not the
        roles — are what a cost-weighted budget has to keep apart
        (``core.pricing``). Keyed by the coder-tier column name so a caller can
        splat the result straight into ``pricing.weighted_tokens``.

        Three classes x ``len(USAGE_ROLES)`` roles; the count moves when a
        role is registered, which is why nothing here or downstream states it
        as a literal any more.
        """
        return {
            "tokens_used": tuple(
                "tokens_used" if tier == "" else f"{tier}tokens_used"
                for tier in cls._USAGE_TIERS
            ),
            "cache_read_tokens": tuple(
                f"{tier}cache_read_tokens" for tier in cls._USAGE_TIERS),
            "cache_creation_tokens": tuple(
                f"{tier}cache_creation_tokens" for tier in cls._USAGE_TIERS),
        }

    @classmethod
    def _usage_columns(cls) -> tuple[str, ...]:
        # Derived, never re-listed: a column added to one of these and not the
        # other would make the raw total and the weighted total disagree about
        # what the task spent.
        return tuple(
            col for cols in cls._usage_columns_by_class().values() for col in cols
        )

    @classmethod
    def _zero_priced_work_sql(cls) -> str:
        """SQL for "this attempt row burned NO priced work".

        Built from ``_usage_columns()`` — the same addend column set
        ``lifetime_usage`` sums and the same one the budget gate splats into
        ``pricing.weighted_tokens`` — so a role or class added to
        ``USAGE_ROLES`` widens this predicate at the exact moment it widens
        the caps. There is no second, independently-typed list to drift out
        of sync with it (the ``eval/northstar_card.score_did_priced_work``
        lesson: one definition, two consumers).

        Raw-zero over those addends IS priced-zero, not merely correlated
        with it: ``pricing.weighted_tokens`` is a non-negative linear
        combination of exactly these addends with every weight strictly
        positive (``FRESH_WEIGHT`` 1.0, ``CACHE_CREATION_WEIGHT`` 1.25,
        ``CACHE_READ_WEIGHT`` 0.1), so the weighted sum is 0 exactly when
        every addend is 0. This SQL is therefore the priced predicate, not a
        second rule that happens to agree with it today.

        ``output_tokens`` is deliberately excluded: `_output_columns_by_class`
        documents it as a SLICE of `tokens_used`, already inside that addend,
        not a bucket beside it — including it would re-open the double-count
        trap for no change in verdict (it cannot be non-zero while
        ``tokens_used`` is zero).

        ``COALESCE`` per column: a row whose split was never recorded stores
        SQL NULL, and NULL must read as "no recorded spend", the same
        treatment ``lifetime_usage_by_class``'s SUMs already give it.
        """
        return "(" + " + ".join(
            f"COALESCE({c}, 0)" for c in cls._usage_columns()) + ") = 0"

    @classmethod
    def _lifetime_included_sql(cls) -> str:
        """SQL boolean predicate: TRUE when an attempts row counts toward the
        lifetime BUDGET — on BOTH axes, the attempt count and the
        cost-weighted token sums.

        INCIDENT (2026-08-2x, task ae2a535c): the attempt count already
        excluded `infra_failure`/`mechanical_round`/dead-`interrupted` rows,
        but the token SUMs in ``lifetime_usage_by_class`` had NO such gating
        at all — a plain unconditional ``SUM`` over every row, infra or not.
        An infra-classified row's tokens "still summed below" (the comment
        that used to sit next to that SUM, now removed because it is no
        longer true) even though its attempt was already spared. That let a
        quota wall or a dead SDK dispatch exhaust the TOKEN cap while
        consuming zero ATTEMPTS — the environment's failure billed as the
        task's own spend.

        This is the ONE fragment both the attempt-count CASE and the
        included/excluded token-sum CASEs in ``lifetime_usage_by_class`` use,
        so the two axes cannot drift apart again the way they just did. There
        is no second, independently-typed predicate anywhere in this method.

        Excluded spend is NOT discarded: ``lifetime_usage_by_class`` returns
        it as a separate (excluded) dict so callers can still report it (`nh
        status`, the BUDGET_EXHAUSTED blocker, the drawer) as "spend the
        environment burned, not the task" — and ``lifetime_usage`` still
        reconstructs the old all-in raw total from included + excluded, so
        nothing that wanted the whole-life sum loses it.

        Deliberately scoped to a single task's own attempts, nothing more: no
        fleet-level or all-time pool ceiling is added here (a larger, related
        proposal — PR #605 — was rejected specifically for adding one; that
        stays out of scope for this fix — a fleet-level ceiling, if wanted,
        is a separate ticket and must be a ROLLING window, never an all-time
        one). What bounds a task whose every attempt is infra-classified is
        NOT `lifetime_attempts` — an infra row never increments the attempt
        count, so a single task retrying itself alone never trips it
        (`test_breaker_trips_on_three_distinct_tasks_and_not_on_one`'s "one
        task retrying itself is that task's problem, not the fleet's") — nor
        `_check_attempt_startup_floor`/`_min_viable_attempt_cost`, which is
        computed from `included` spend only and so stays near zero for an
        all-infra task. Which mechanism actually bounds it depends on the
        infra SHAPE:

        * A dead SDK dispatch (auth/billing wall, zero tokens streamed) is
          bounded by the fleet-wide `InfraBreaker` (`core.infra_breaker`):
          it trips the whole worker pool into a cooldown after 3 DISTINCT
          tasks show this shape close together — independent of any single
          task's own token or attempt tally
          (`test_all_infra_task_does_not_loop_forever_the_fleet_breaker_bounds_it`).
        * A mid-attempt quota wall WITH real tokens streamed
          (`Orchestrator._park_quota`) is bounded by NEITHER of the above:
          streamed tokens clear the `InfraBreaker`'s streak before the quota
          branch is even reached (`record_healthy`), and that branch
          deliberately does not bump the breaker — a billing wall is not a
          dead dispatch. What bounds THIS shape is the park itself: the task
          is parked with `wake_check_at = exc.resets_at` and a
          `quota_refreshed` blocker, so it cannot re-attempt before the
          account's own quota window resets — the wake cadence is the
          bound, not a token or attempt count.
        """
        return (
            "COALESCE(infra_failure, 0) = 0 "
            "AND COALESCE(mechanical_round, 0) = 0 "
            f"AND NOT (status = 'interrupted' AND {cls._zero_priced_work_sql()})"
        )

    @classmethod
    def _output_columns_by_class(cls) -> dict[str, tuple[str, ...]]:
        """The output SHARE of the four ``*tokens_used`` columns.

        Kept OUT of ``_usage_columns_by_class`` on purpose, and this is the
        one thing to understand before editing either method. Those three
        classes are ADDENDS — ``lifetime_usage`` sums them to get the raw
        token total. ``output_tokens`` is not an addend; it is a slice of the
        ``tokens_used`` addend, already inside it. Folding it in would count
        every output token twice and silently inflate the raw figure that
        ``nh``, the web surfaces and ``eval/northstar.py`` all print.

        It rides along in ``lifetime_usage_by_class`` anyway, because the
        WEIGHTED path does need it: ``pricing.weighted_tokens`` charges it
        ``OUTPUT_EXTRA_WEIGHT`` — the premium over the 1.0 that
        ``tokens_used`` already applied — so the splat keeps working and the
        total is priced once.
        """
        return {
            "output_tokens": tuple(
                "output_tokens" if tier == "" else f"{tier}output_tokens"
                for tier in cls._USAGE_TIERS
            ),
        }

    async def lifetime_usage_by_class(
        self, task_id: str
    ) -> tuple[int, dict[str, int], dict[str, int]]:
        """(attempts, included, excluded).

        ``included``/``excluded`` are both ``{tokens_used, cache_read_tokens,
        cache_creation_tokens, output_tokens}`` — the same addend columns
        ``lifetime_usage`` sums, kept in their three price classes so the
        budget gate can weight them (``core.pricing.weighted_tokens``), plus
        a FOURTH key that is not a fourth class: ``output_tokens`` is the
        output slice of ``tokens_used``, carried here so the splat into
        ``weighted_tokens`` can charge it the output premium. It is
        deliberately absent from ``lifetime_usage``'s raw total, which would
        otherwise count it twice. The classes are summed across all
        registered roles (``USAGE_ROLES``: coder, reviewer, planner,
        utility, supervisor, distill) because they all bill at the same three
        rates. For the same numbers cut by ROLE instead, see
        ``lifetime_usage_by_role`` — it partitions the identical column set,
        so the two always agree on the total.

        ``attempts`` and ``included`` are gated by the SAME predicate,
        ``_lifetime_included_sql`` — see that method for the incident this
        fixes (infra/mechanical/dead-interrupted rows used to escape the
        attempt count but NOT the token sums). ``excluded`` is the mirror: the
        spend every excluded row carried, still reported rather than
        discarded, so a caller can show "X weighted excluded as infra"
        alongside the task's own gated spend. Nothing is ever dropped —
        ``included[name] + excluded[name]`` always equals what an
        unconditional ``SUM`` over that column would have returned, which is
        exactly how ``lifetime_usage`` reconstructs the old all-in raw total.

        SCOPE, stated once because a budget gate reads this as the whole
        truth: both totals are ``FROM attempts`` ONLY. Spend this task has
        booked in ``unattributed_usage`` under ``ORPHANED_SITE_PREFIX``
        (aux-tier planner/utility/supervisor/distill spend that
        ``Orchestrator._flush_orphaned_aux_usage`` wrote there with this
        task's id because no attempt claimed it) is on NEITHER axis here —
        not ``included``, not ``excluded``. It is real spend recorded
        against this task that this method simply never reads. The
        reconciling read is ``unattributed_usage_totals(task_id,
        attributed=True)``. Measured 2026-08-22: 73 calls / 5,425,168
        weighted tokens fleet-wide in that ledger, worst single task
        744,666 (18.6% of the 4M default ``lifetime_tokens`` cap
        (``bounds.py``) — the 8M figures elsewhere in this file are
        historical prose about the pre-2026-07-31 raw cap, not the current
        default) — enough that a human raising a cap from this method's
        ``tokens_used`` alone can be raising it from a number that already
        undercounts.
        """
        # The three raw classes PLUS the output share, which is a slice of the
        # first of them rather than a fourth class — see
        # `_output_columns_by_class`. The inner `COALESCE(col, 0)` is what
        # makes a NULL split cost nothing extra instead of poisoning the SUM:
        # an attempt whose split was never recorded prices exactly as it did
        # before the column existed, which is the honest treatment of
        # "unknown" and the only one available (there is no backfill).
        wanted = {**self._usage_columns_by_class(), **self._output_columns_by_class()}
        included_sql = self._lifetime_included_sql()
        inc_selects = ", ".join(
            "COALESCE(SUM(CASE WHEN {cond} THEN {expr} ELSE 0 END), 0) "
            "AS inc_{name}".format(
                cond=included_sql,
                expr=" + ".join(f"COALESCE({c}, 0)" for c in cols),
                name=name)
            for name, cols in wanted.items()
        )
        exc_selects = ", ".join(
            "COALESCE(SUM(CASE WHEN {cond} THEN 0 ELSE {expr} END), 0) "
            "AS exc_{name}".format(
                cond=included_sql,
                expr=" + ".join(f"COALESCE({c}, 0)" for c in cols),
                name=name)
            for name, cols in wanted.items()
        )
        # The ATTEMPT count excludes rows classified `infra_failure = 1`
        # (`orchestrator._infra_sdk_failure`) — a dead SDK dispatch (auth
        # wall, zero tokens streamed) did no work, so it must not consume a
        # lifetime attempt (INCIDENT 2026-08-13: 4 tasks burned all 9 on
        # exactly this) — OR `mechanical_round = 1` (`orchestrator.
        # _mechanical_round`) — a post-PASS pr_conflict rebase or
        # re-verification tick that changes no code (INCIDENT 2026-08-13:
        # tasks 79183501 and 1a4b7bf7 PASSED review, then rebase rounds under
        # supervising-train landings burned the rest of their lifetime
        # attempts) — OR `status = 'interrupted'` AND zero priced work
        # (`_zero_priced_work_sql`) — a worker process that died without
        # closing its row (`create_attempt`'s stale-row sweep,
        # `close_open_attempts`, `close_attempts_of_terminal_tasks`) before
        # metering anything (INCIDENT 2026-08-20: a weekly-quota outage left
        # four concurrent tasks accumulating rows closed by the supersede
        # sweep — turns <= 1, zero priced tokens; task 123dea00 had 8 of its
        # 9 attempts this shape and was FAILED by attempt-cap exhaustion with
        # no real attempt having failed; 021899de, 7553b865, e2d0802d needed
        # manual `lifetime_attempts` raises to survive).
        #
        # THE BOUNDARY, stated once and load-bearing: an interrupted attempt
        # that burned REAL priced work STILL COUNTS — the work happened and
        # was lost, and that spend pressure is real and the orphan-spend
        # accounting depends on it. Only the zero-priced interrupted shape is
        # excluded. A `failed` attempt always counts, whatever its token
        # columns say — a genuine failure that recorded nothing is still a
        # datum, which is exactly why the exclusion below tests `status =
        # 'interrupted'` and not merely "zero priced work" on its own.
        #
        # This stays outside the coder's influence: the excluded shape
        # requires `status = 'interrupted'` — written by `db.py` (dead rows
        # closed at `create_attempt` / `close_open_attempts`) and by ONE
        # orchestrator path, `Orchestrator._honor_server_stop`, which only a
        # server shutdown reaches (never the agent, never
        # `update_attempt(status='failed')` from the attempt loop) — AND
        # zero recorded spend, which an agent cannot fake downward because
        # any session it runs meters tokens. The server-stop row ALSO
        # carries `infra_failure=1` (its work is checkpointed, not lost, and
        # a coder cannot trigger a shutdown), so it is excluded by the
        # infra branch whatever it spent — but its tokens are NOT dropped:
        # they land in `excluded` below, still visible to any caller that
        # wants to report environment spend. There is no way for a coder to
        # convert its own failing attempts into free ones.
        #
        # The server-stop row is one instance of a broader shape, not the only
        # one: a FULLY-PRICED coder row — real tokens streamed, real work done
        # — can also be closed whole under `infra_failure=1` by
        # `worktree.salvage_dead_worktrees` (`hard_kill_salvage`, on a
        # SIGKILL/pid-death) and by the reviewer-side wall path (a dead or
        # quota-walled review turn closes the CODER's attempt row for that
        # round with `infra_failure=1`, ~`orchestrator.py:7178`, so a
        # reviewer-session death is not left an unattributed dead row) — and
        # likewise by the coder's own quota/infra wall (`_park_quota`'s
        # `infra_failure=1` row, which can carry millions of streamed tokens)
        # and the repro corrective round. Every one of these writers is
        # harness-triggered only — never reachable from the coder's own
        # `update_attempt(status='failed')` — which is what makes
        # excluding a fully-priced row WHOLE, rather than pro-rating it,
        # sanctioned by this single-predicate design: the predicate doesn't
        # ask how much a row spent, only whether the harness (not the coder)
        # is the reason it didn't finish.
        #
        # RECONCILE DELIBERATELY, NOT BY DRIFT: queued ticket b5f7a61e decides
        # the ATTEMPT axis on "did the coder do priced work" and says the token
        # axis is tracked separately. Since this change ONE predicate governs
        # both axes and `test_one_predicate_governs_both_attempt_count_and_token_sums`
        # pins it, so that ticket cannot land as written: either the two axes
        # split again on purpose (and the test is changed with the reason in
        # the PR body) or fully-priced walled rows' tokens come back under the
        # cap — which is the exact defect this change closes. Decide, don't drift.
        #
        # The token SUMs above are now gated by the EXACT SAME
        # `_lifetime_included_sql` predicate as the attempt count (previously
        # they were unconditional — see that method's docstring for the
        # incident) — one fragment, referenced here for the attempt count,
        # the included sums, and the excluded sums, so all three can never
        # drift apart again. The row is never deleted or skipped — it is the
        # durable record; only which bucket its tokens land in changes, at
        # the single chokepoint both budget gates (`_check_lifetime_budget`,
        # `_at_lifetime_ceiling`) read, so they cannot disagree about whether
        # a dead dispatch or a dead interrupted row counted.
        row = await self._fetchone(
            f"SELECT COALESCE(SUM(CASE WHEN {included_sql} THEN 1 ELSE 0 "
            f"END), 0) AS n, {inc_selects}, {exc_selects} FROM attempts "
            "WHERE task_id = ?",
            (task_id,),
        )
        if not row:
            empty = {name: 0 for name in wanted}
            return (0, dict(empty), dict(empty))
        return (
            int(row["n"]),
            {name: int(row[f"inc_{name}"]) for name in wanted},
            {name: int(row[f"exc_{name}"]) for name in wanted},
        )

    async def lifetime_usage(self, task_id: str) -> tuple[int, int]:
        """(attempts, tokens) spent over the task's WHOLE life, resumes included.

        Tokens = everything the attempt metered: in/out, cache reads AND cache
        creation, across every registered role (``USAGE_ROLES``: coder,
        reviewer, planner, utility, supervisor, distill). Cache reads are where the bulk of the burn lives (~83%), but
        this used to sum ONLY the coder's ``tokens_used + cache_read_tokens``
        — 2 columns out of the whole grid. The gate was therefore blind to every
        reviewer, planner and utility token, and to cache creation everywhere. Measured
        over 574 real attempt rows that blind spot is 16.2% of true spend, and
        a task whose burn was mostly reviewer or utility could never trip the
        cap at all. Cache creation is billed, so a spend gate must count it.

        Interrupted/killed rows count: they spent the attempt even if their
        token columns under-report (pre-1638427 rows recorded zero).

        RAW, and deliberately still raw: this is the burn figure `nh`, the web
        surfaces and `eval/northstar.py` all report, and it must keep matching
        them token for token. The BUDGET gate no longer compares against it —
        it uses ``lifetime_usage_by_class`` and weights the classes by price
        (``core.pricing``), because a raw sum bounds conversation length, not
        spend. Computed from the same one query so the two cannot drift.

        Sums the three ADDEND classes only. ``lifetime_usage_by_class`` also
        returns ``output_tokens``, which is a slice of ``tokens_used`` and not
        a bucket beside it; `sum(by_class.values())` would double-count it and
        move a number this docstring promises will keep matching every surface
        token for token.

        ``lifetime_usage_by_class`` now splits those addends into ``included``
        (what counts toward the budget) and ``excluded`` (infra/mechanical/
        dead-interrupted spend, reported but not charged to the cap) — this
        method reconstructs the same all-in raw total as before by summing
        BOTH, so every caller that wants the whole-life figure (`nh`, the web
        surfaces, `eval/northstar.py`) keeps seeing it unchanged.
        """
        attempts, included, excluded = await self.lifetime_usage_by_class(task_id)
        addend_cols = self._usage_columns_by_class()
        return attempts, sum(included[name] for name in addend_cols) + sum(
            excluded[name] for name in addend_cols
        )

    async def lifetime_usage_by_role(
        self, task_id: str
    ) -> dict[str, dict[str, int]]:
        """``{role: {tokens_used, cache_read_tokens, cache_creation_tokens,
        output_tokens, total}}`` over the task's whole life.

        The SAME rows and the SAME columns ``lifetime_usage`` sums, cut by
        NAMED ROLE instead of by price class. That is the whole point and the
        one invariant to preserve when editing either: both partition
        ``_usage_columns()``, so

            sum(r["total"] for r in by_role.values()) == lifetime_usage()[1]

        exactly, for every task, with no residual — a role's spend can move
        between buckets but can never leave the total.
        ``test_role_token_accounting.py`` asserts both halves (structurally,
        over the column sets, and on real rows).

        What that identity does NOT catch, stated so nobody reads more safety
        into it than is there: both sides of it derive from ``USAGE_ROLES``,
        so they narrow TOGETHER. A metered column added to the `attempts`
        schema under a prefix no role registers is unclaimed by this method
        AND absent from ``_usage_columns()``, and the sum still reconciles
        while the spend is silently uncounted. The only guard that sees that
        is one anchored to the SCHEMA rather than to the registry —
        ``test_no_metered_column_in_the_schema_is_unclaimed``, which reads
        `PRAGMA table_info(attempts)`.

        ``total`` is the three ADDENDS only. ``output_tokens`` rides along as
        a fifth key because callers pricing a role need it, but it is a SLICE
        of ``tokens_used``, not a fourth addend — adding it in would
        double-count output and break the identity above.

        Roles are reported even at zero, so a caller rendering a breakdown
        gets a stable shape and an operator can see that the supervisor cost
        nothing rather than wondering whether it was measured.
        """
        cols: dict[str, tuple[str, ...]] = {}
        for tier in USAGE_ROLES:
            cols[tier] = usage_columns_for(tier) + (
                "output_tokens" if tier == "" else f"{tier}output_tokens",)
        selects = ", ".join(
            f"COALESCE(SUM(COALESCE({col}, 0)), 0) AS {col}"
            for tier_cols in cols.values() for col in tier_cols
        )
        row = await self._fetchone(
            f"SELECT {selects} FROM attempts WHERE task_id = ?", (task_id,))
        out: dict[str, dict[str, int]] = {}
        for tier, role in USAGE_ROLES.items():
            used, read, creation, output = (
                int(row[c]) if row else 0 for c in cols[tier])
            out[role] = {
                "tokens_used": used, "cache_read_tokens": read,
                "cache_creation_tokens": creation, "output_tokens": output,
                "total": used + read + creation,
            }
        return out

    # ---------------------- unattributed usage ledger ----------------------- #

    @serialized_write
    async def record_unattributed_usage(
        self, *, site: str, tokens_used: int = 0, cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0, model: str | None = None,
        task_id: str | None = None,
    ) -> str | None:
        """Book utility-tier spend that no attempt row can own.

        ``site`` names WHERE it was spent — the live values are ``"api.grill"``,
        ``"api.grill_stream"``, ``"api.grill_stream.evaluate_spec"``,
        ``"cli.task_add.grill"``, and one ``"orphaned_<tier>usage"`` per
        registered aux role (``orphaned_plan_usage``,
        ``orphaned_utility_usage``, ``orphaned_supervisor_usage``,
        ``orphaned_distill_usage``; the set is generated from
        ``AUX_USAGE_TIERS`` by ``_flush_orphaned_aux_usage``, so it widens
        with the registry) — so the residual stays diagnosable rather
        than being one anonymous number.
        Returns the row id, or None when there was nothing to record — a call
        that reports zero across all three figures writes no row, so the table
        holds spend and never padding.

        NOT YET BOOKED ANYWHERE, and this ledger is their natural home — five
        further LLM sites still record nothing, verified present as of this
        commit: the GUI transcript analyzer (`api/app.py:2925`, review tier),
        the WikiGenerator (`api/app.py:3008` + `docs_gen.py:118`,
        ``max_turns=12``), and three CLI backends (`cli/commands.py:1776`,
        `:2310`, `:3138`). Deliberately left out of this change, which is
        scoped to the six intake sites.
        """
        tokens_used = int(tokens_used or 0)
        cache_read_tokens = int(cache_read_tokens or 0)
        cache_creation_tokens = int(cache_creation_tokens or 0)
        if not (tokens_used or cache_read_tokens or cache_creation_tokens):
            return None
        row_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO unattributed_usage (id, ts, site, model, task_id, "
            "tokens_used, cache_read_tokens, cache_creation_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, _now(), site, model, task_id, tokens_used,
             cache_read_tokens, cache_creation_tokens),
        )
        await self.db.commit()
        return row_id

    async def unattributed_usage_totals(
        self, task_id: str | None = None, *, attributed: bool | None = None
    ) -> dict[str, int]:
        """Totals over the unattributed ledger: ``{calls, tokens_used,
        cache_read_tokens, cache_creation_tokens, total}``.

        ``task_id=None`` totals the WHOLE ledger (the default question — "how
        much intake spend does no task own"); pass an id to scope it.

        ``attributed`` splits that ledger into the two classes ``nh status``
        distinguishes: ``None`` (default) keeps today's whole-ledger
        behaviour byte-identical for existing callers; ``True``/``False``
        add a ``site`` filter. The split uses the ``site`` PREFIX
        (``orphaned_%``, see ``ORPHANED_SITE_PREFIX``), not the presence of
        ``task_id``, for two reasons: (a) ``compact_unattributed_usage``
        drops ``task_id`` when it rolls a group up (a group can span tasks)
        but preserves ``site``, so a ``task_id`` test would silently
        reclassify aged spend as ownerless; (b) the prefix is generated from
        ``AUX_USAGE_TIERS`` by ``_flush_orphaned_aux_usage``, so a newly
        registered aux role lands in the attributed class automatically —
        no hardcoded site list to fall outside of.

        ``calls`` is not a plain ``COUNT(*)``: retention compaction
        (``compact_unattributed_usage``) collapses aged rows into one
        roll-up row per ``(site, model)``, tagged ``rolled_up`` with the
        number of original rows it stands for. A roll-up row must still
        count as the calls it replaced, or "how many LLM calls landed here"
        would silently shrink the moment a row ages past the retention
        window — so this sums ``rolled_up`` where set, and 1 per ordinary
        (non-rolled-up) row. The ``attributed`` split preserves this: each
        class sums ``rolled_up`` within its own filtered rows, so a rolled-up
        group still counts as the calls it replaced inside whichever class
        its ``site`` belongs to.
        """
        sql = ("SELECT COALESCE(SUM(CASE WHEN rolled_up > 0 THEN rolled_up "
               "ELSE 1 END), 0) AS calls, "
               "COALESCE(SUM(tokens_used), 0) AS tokens_used, "
               "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
               "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens "
               "FROM unattributed_usage")
        predicates: list[str] = []
        args: list[Any] = []
        if task_id is not None:
            predicates.append("task_id = ?")
            args.append(task_id)
        if attributed is True:
            predicates.append("site LIKE ?")
            args.append(f"{ORPHANED_SITE_PREFIX}%")
        elif attributed is False:
            predicates.append("(site NOT LIKE ? OR site IS NULL)")
            args.append(f"{ORPHANED_SITE_PREFIX}%")
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        row = await self._fetchone(sql, tuple(args))
        out = {k: int(row[k] if row else 0) for k in (
            "calls", "tokens_used", "cache_read_tokens", "cache_creation_tokens")}
        out["total"] = (out["tokens_used"] + out["cache_read_tokens"]
                        + out["cache_creation_tokens"])
        return out

    @serialized_write
    async def compact_unattributed_usage(
        self, *, retention_days: int | None = None
    ) -> int:
        """Roll up ``unattributed_usage`` rows older than the retention
        window into one row per ``(site, model)`` group. Returns the number
        of original rows collapsed (0 if nothing aged out, or the knob is
        disabled).

        DELETE, not roll-up, would let the residual total
        (``unattributed_usage_totals``) silently shrink — that number is
        documented as the whole-cost figure to trust, and this table exists
        precisely so spend is never quietly dropped. So aged rows are
        summed and replaced by one row per group instead: ``site``/``model``
        survive (the residual stays diagnosable), ``task_id`` does not (a
        group can span many tasks), and ``rolled_up`` carries how many
        source rows the group stands for so ``calls`` in
        ``unattributed_usage_totals`` stays exact — see that method's
        docstring for why ``COUNT(*)`` alone would drift.

        Totals invariant: the three token columns are summed per group and
        re-inserted unchanged, so ``SUM(tokens_used)`` etc. over the whole
        table is exactly what it was before compaction — only the row count
        and per-row ``ts``/``task_id`` detail are lost for rows older than
        the window.

        Idempotent: the roll-up row is written with ``ts = cutoff``, which
        is never ``< cutoff`` on the same call's own cutoff, so running this
        twice back to back is a no-op the second time (the guard SELECT
        below finds nothing new to compact). On a later day, a roll-up row
        can itself age past a NEW cutoff and get folded into a fresh
        roll-up — arithmetically stable because its ``rolled_up`` count
        (not a bare 1) carries forward.

        ``retention_days=None`` reads ``usage_ledger.retention_days`` from
        config (default 90 if config load fails — see ``config.py``);
        ``retention_days<=0`` disables compaction (returns 0, no rows
        touched).
        """
        if retention_days is None:
            try:
                from ..config import load_config
                # create_if_missing=False: a plain connect() must never have
                # the side effect of materializing ~/.no_human/config.yaml —
                # read the knob if a config is already there, else fall
                # through to the documented default below.
                retention_days = int(
                    load_config(create_if_missing=False)
                    .get("usage_ledger", {}).get("retention_days", 90))
            except Exception:  # noqa: BLE001 — a bad/unreadable config must
                # not block compaction; 90 is the documented default.
                retention_days = 90
        if retention_days <= 0:
            return 0
        # Truncated to the day: two calls on the SAME UTC calendar day compute
        # the IDENTICAL cutoff, which is what makes this idempotent (the
        # roll-up row below is written with ts = cutoff, and a value cannot be
        # `< ` a cutoff it exactly equals). Full microsecond precision would
        # make every call's cutoff strictly greater than the last, sweeping the
        # previous roll-up row back up on the very next call.
        cutoff_dt = (datetime.now(timezone.utc)
                     - timedelta(days=retention_days)).replace(
                         hour=0, minute=0, second=0, microsecond=0)
        cutoff = cutoff_dt.isoformat()
        guard = await self._fetchone(
            "SELECT COUNT(*) AS c FROM unattributed_usage WHERE ts < ?",
            (cutoff,),
        )
        total = int(guard["c"] if guard else 0)
        if not total:
            return 0
        groups = await self._fetchall(
            "SELECT site, model, "
            "SUM(tokens_used) AS tokens_used, "
            "SUM(cache_read_tokens) AS cache_read_tokens, "
            "SUM(cache_creation_tokens) AS cache_creation_tokens, "
            # A group can already contain a PRIOR roll-up row (from an
            # earlier compaction) alongside ordinary rows. `COUNT(*)` would
            # count that roll-up row as 1, discarding however many original
            # calls it stood for. Sum `rolled_up` where set, 1 per ordinary
            # row instead — the same accumulator `unattributed_usage_totals`
            # uses (see that method's docstring) — so re-compaction on a
            # later day carries the count forward exactly instead of
            # resetting it to the row count.
            "SUM(CASE WHEN rolled_up > 0 THEN rolled_up ELSE 1 END) AS n "
            "FROM unattributed_usage WHERE ts < ? GROUP BY site, model",
            (cutoff,),
        )
        await self.db.execute(
            "DELETE FROM unattributed_usage WHERE ts < ?", (cutoff,))
        for g in groups:
            await self.db.execute(
                "INSERT INTO unattributed_usage (id, ts, site, model, "
                "task_id, tokens_used, cache_read_tokens, "
                "cache_creation_tokens, rolled_up) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (uuid.uuid4().hex, cutoff, g["site"], g["model"],
                 int(g["tokens_used"] or 0), int(g["cache_read_tokens"] or 0),
                 int(g["cache_creation_tokens"] or 0), int(g["n"])),
            )
        await self.db.commit()
        return total

    # --------------------------- memories ---------------------------------- #
    # The human-confirmed learning queue (PLAN.md 4.5): proposals land here
    # with confirmed=0 and never enter the active rule set until a human
    # confirms them (avoids leniency-biased lessons accumulating silently).

    @serialized_write
    async def add_memory(
        self, *, mem_type: str, title: str, content: str,
        tags: list[str] | None = None, project: str | None = None,
        source: str = "proposed", confirmed: bool = False,
        dedupe_key: str | None = None, origin: str | None = None,
        evidence: dict[str, Any] | None = None,
        project_scope: str | None = None,
        project_allowlist: list[str] | None = None,
    ) -> str | None:
        """Insert a memory. If ``dedupe_key`` matches an existing memory's
        signature (stored in file_path), skip and return None.

        ``origin`` records WHICH SIGNAL produced the proposal (``learning.queue``'s
        ``ORIGIN_REVIEW`` / ``ORIGIN_SUPERVISOR``); it is not ``source``, which is
        the queue-visibility contract. NULL where unrecorded.

        ``evidence`` is the B3 structured record (what happened, in which
        task, citing the correction/review event) — stored as JSON, NULL where
        unrecorded. ``project_scope`` is the B4 project identity
        (``learning/scope.py``); NULL keeps the row on legacy path matching.

        P1 BRAIN HYGIENE — WRITE-TIME PROVENANCE GATE. Every insert is passed
        through ``learning.provenance.quarantine_reason`` before the row is
        written. A hit sets ``quarantined = 1``; either way, ``provenance``
        records ``{project, project_scope, context, ingested_at,
        quarantine_reason}`` as JSON — a legacy row predating this column has
        none of that and stays NULL, same reasoning as ``origin``/``evidence``.
        ``project_allowlist``, when given, OVERRIDES the configured allowlist
        (an explicit ``[]`` therefore forces the project class inert for this
        one call). When left at the default (``None``, meaning "the caller
        did not pass one" — no caller in this repo does), it is resolved via
        ``learning.provenance.project_allowlist()``, which reads the
        ``NO_HUMAN_LEARNING_PROJECT_ALLOWLIST`` env var and is INERT when that
        is unset. This is what actually wires the project-allowlist needle
        class into every write, not just into ``nh memories scan``. This is
        the single write chokepoint (every caller in this repo goes through
        it), so nothing else needs to duplicate the gate.

        RAISES ``ValueError`` for an unconfirmed memory whose ``source`` is not
        ``SOURCE_PROPOSED`` — see that constant for the three call sites that
        wrote one anyway. Deliberately loud rather than silently normalised: a
        guard that quietly repairs its input is a guard nobody ever notices is
        being hit, and every `source` in this repo is a literal at the call
        site, so no runtime data can reach this branch. It is a programming
        error, and it fires before any write, so a caller that trips it leaves
        the database untouched.

        THE ROWS THAT ARE ALREADY LIKE THAT. This guard closes the door; it
        does not go back for what walked through it. See the block below.
        """
        # ── STRANDED ROWS, and what this change does NOT do to them ────────
        #
        # Measured 2026-08-07 against a `cp` of the operator's live database —
        # never the live file, which a running server holds open — so the
        # numbers below are a snapshot of one install, not a property of the
        # schema. 20 rows have `confirmed = 0` and a `source` that `pending()`
        # does not select, in two shapes:
        #
        #   18 rows  source='confirmed', confirmed=0
        #            created_at  2026-07-01 13:40:03 (all 18, identical)
        #            updated_at  2026-07-01T13:40:39.458782+00:00
        #                     …  2026-07-01T13:40:39.467533+00:00
        #            origin NULL, archived 0
        #    2 rows  source='reply', confirmed=0
        #            created 2026-07-26 23:53:41 and 2026-07-27 00:14:28
        #
        # The 2 reply rows have a known producer: `nh reply`'s mined learning
        # passed source="reply", which is the bug this guard exists for.
        #
        # The 18 do not, and the honest claim is narrower than it is tempting to
        # make. `confirm_memory` DOES write source='confirmed' — it is the only
        # writer of that literal anywhere in this repo's history (`git log --all
        # -S"source = 'confirmed'" -- src` returns exactly one commit, the one
        # that introduced the method) — and it has always set `confirmed = 1` in
        # the SAME UPDATE. So it is the COMBINATION that has no producer either
        # this session or the review before it could find. Not "no code path can
        # produce it": nothing here rules out a hand-run UPDATE, an older tree,
        # or a path we did not think to look at. One more clue, recorded rather
        # than interpreted: `created_at` on all 18 is in the column DEFAULT's
        # format (`datetime('now')` — space separator, no offset) while
        # `updated_at` is Python `_now()`'s ISO-8601 with microseconds, so the
        # rows were inserted with the default and updated 36 seconds later by
        # something in Python. Which thing, we could not establish.
        #
        # WHAT "STRANDED" MEANS HERE, precisely — the first draft of this said
        # "no code path will ever surface them again", and that is false:
        #   · NOT reachable: `LearningQueue.pending()` (source='proposed'),
        #     `active()` and `GET /api/learnings` (confirmed=1), prompt
        #     injection via `list_memories(confirmed=True, …)`, and
        #     `context/sessions.py`'s recall (`WHERE confirmed = 1`). So they
        #     can never become an active rule, and can never reach the human
        #     confirm gate — the two paths that decide anything.
        #   · STILL reachable: `learning/curator.py`'s `curate()` reads
        #     `list_memories(confirmed=False)` with no source filter, so its
        #     dedupe pass can archive one as a duplicate and its LLM pass can
        #     propose archiving or consolidating it; and `nh recall <q>
        #     --include-pending` lists them as "memory (pending)".
        #
        # THIS BRANCH RUNS NOTHING AGAINST THEM. There is no migration here and
        # no write to the operator's database. The options are the operator's:
        #   1. Leave them. Nothing injects them into any prompt. The only cost
        #      is that an ad-hoc count of "pending learnings" disagrees with the
        #      queue by 20.
        #   2. Re-queue them — `UPDATE memories SET source='proposed' WHERE
        #      confirmed=0 AND source<>'proposed'` — which is the only option
        #      that hands the decision back, at the cost of 20 more rows on a
        #      queue already holding 329.
        #   3. Archive them (`archived=1`), keeping the rows and their dedupe
        #      keys while removing them from the curator's input.
        # Deleting them is not on the list: the row carries the dedupe key, and
        # their content is environment notes about the operator's own machine
        # and workplace — not quoted here, because this file ships.
        if not confirmed and source != SOURCE_PROPOSED:
            raise ValueError(
                f"add_memory(confirmed=False, source={source!r}) would write a "
                f"proposal that `pending()` cannot see — unconfirmed memories "
                f"must use source={SOURCE_PROPOSED!r}. Record the provenance "
                f"in `origin=` instead, which is a second column for exactly "
                f"this question."
            )
        if dedupe_key is not None:
            if await self._fetchone(
                "SELECT id FROM memories WHERE file_path = ? LIMIT 1", (dedupe_key,)
            ):
                return None
        mem_id = uuid.uuid4().hex
        from ..learning.provenance import project_allowlist as _resolve_allowlist
        from ..learning.provenance import quarantine_reason
        resolved_allowlist = (
            project_allowlist if project_allowlist is not None
            else _resolve_allowlist()
        )
        reason = quarantine_reason(
            title=title, content=content, project=project, tags=tags,
            allowlist=resolved_allowlist,
        )
        row_provenance = json.dumps({
            "project": project, "project_scope": project_scope,
            "context": origin or source, "ingested_at": _now(),
            "quarantine_reason": reason,
        })
        await self.db.execute(
            """INSERT INTO memories
                 (id, type, title, content, file_path, tags, project, source,
                  confirmed, origin, evidence, project_scope, quarantined,
                  provenance)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (mem_id, mem_type, title, content, dedupe_key,
             json.dumps(tags or []), project, source, 1 if confirmed else 0,
             origin, json.dumps(evidence) if evidence is not None else None,
             project_scope, 1 if reason else 0, row_provenance),
        )
        await self.db.commit()
        return mem_id

    async def memory_dedupe_key_exists(self, dedupe_key: str) -> bool:
        """True when some memory already carries this dedupe signature (stored
        in ``file_path``).

        ``add_memory`` runs the same check — but only once it has been handed a
        finished proposal, which for a BATCH caller means after the utility
        call that built it was already paid for. B2's harvest re-reads the
        whole correction history on every run, so without this it would spend
        one distillation per already-queued cluster and write nothing.

        ARCHIVED ROWS COUNT, and that is load-bearing rather than an oversight
        in the WHERE clause: an archived proposal is how a human's "no" is
        recorded (``LearningQueue.reject`` archives supervisor-origin rows).
        Skipping archived rows here would make every rejected lesson come back
        on the next harvest, re-distilled at the utility tier.
        """
        return await self._fetchone(
            "SELECT id FROM memories WHERE file_path = ? LIMIT 1", (dedupe_key,)
        ) is not None

    async def get_memory_by_dedupe_key(
        self, dedupe_key: str,
    ) -> dict[str, Any] | None:
        """The memory carrying this dedupe signature (stored in ``file_path``),
        or None. D3-M1 uses it to reach the row a recurring review finding
        collapsed onto — to read its recurrence set and to confirm it in place."""
        row = await self._fetchone(
            "SELECT * FROM memories WHERE file_path = ? LIMIT 1", (dedupe_key,))
        return dict(row) if row is not None else None

    @serialized_write
    async def add_review_recurrence(
        self, dedupe_key: str, task_id: str,
    ) -> list[str]:
        """Record that a deduped review proposal recurred on ``task_id`` and
        return the full set of DISTINCT tasks now associated with it — the
        original (``evidence.task_id``) plus every recorded recurrence.

        WHY THIS EXISTS. Dedupe collapses every re-occurrence of a finding onto
        ONE memory row (`add_memory` returns None and writes nothing on a key
        hit), so the row is the only place that can remember which tasks its
        later occurrences came from. D3-M1 auto-confirms only when a finding
        recurred across >=2 DISTINCT tasks, so those task ids must be counted
        somewhere — here, in the ``evidence`` JSON (a ``recurrences`` list
        beside the original ``task_id``), because evidence IS the structured
        "what happened" record and a recurrence is part of what happened.

        Idempotent: a task already recorded (or the original) is not re-added, so
        replaying the same review round does not inflate the count. Returns [] if
        the key is unknown (the row was rejected-and-deleted between calls)."""
        row = await self._fetchone(
            "SELECT id, evidence FROM memories WHERE file_path = ? LIMIT 1",
            (dedupe_key,))
        if row is None:
            return []
        try:
            evidence = json.loads(row["evidence"]) if row["evidence"] else {}
        except (ValueError, TypeError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        original = evidence.get("task_id")
        recur = evidence.get("recurrences")
        recur = [t for t in recur if t] if isinstance(recur, list) else []
        if task_id and task_id != original and task_id not in recur:
            recur.append(task_id)
            evidence["recurrences"] = recur
            await self.db.execute(
                "UPDATE memories SET evidence = ? WHERE id = ?",
                (json.dumps(evidence), row["id"]))
            await self.db.commit()
        distinct: list[str] = []
        for t in [original, *recur]:
            if t and t not in distinct:
                distinct.append(t)
        return distinct

    async def list_memories(
        self, *, confirmed: bool | None = None, source: str | None = None,
        mem_type: str | None = None, project: str | None = None,
        scope: str | None = None,
        include_global: bool = True, include_archived: bool = False,
        include_quarantined: bool = False, include_paused: bool = False,
    ) -> list[dict[str, Any]]:
        """List memories, optionally scoped to a project.

        When ``project`` and/or ``scope`` is given, only rules/skills attached
        to that project are returned, plus globals unless ``include_global``
        is False. When both are None, no project filter is applied (all rows).

        ``scope`` is the B4 project identity (``learning/scope.py``: sha256 of
        the normalized remote URL) and ``project`` the checkout path. A row
        matches on EITHER — scope for rows that carry one (the same repo
        cloned at two paths is one project), path for legacy rows written
        before the column or for repos with no remote. A GLOBAL row is one
        with neither key (``project IS NULL AND project_scope IS NULL``) —
        exactly the pre-B4 rows the old ``project IS NULL`` clause matched,
        since no row had a scope before the column existed.

        ``include_quarantined`` defaults OFF, mirroring ``include_archived``
        exactly — the P1 brain-hygiene flag (``learning/provenance.py``). This
        is what makes every caller of ``list_memories`` (the Rules/Skills/
        Learnings UI, ``nh rules``/``nh skills``, rule injection via
        ``Orchestrator._load_active_memories``, the learning queue, the
        curator) honour quarantine without any of them touching this method's
        callers individually.

        ``include_paused`` defaults OFF the same way (D3, 2026-08-31): a
        paused learning stays in the store, but no caller sees it as live
        unless it explicitly asks — which is what makes
        ``_load_active_memories`` respect a pause without itself filtering
        for it.
        """
        clauses, params = [], []
        if not include_archived:
            # archived is NULL on rows that predate the column — treat as live
            clauses.append("(archived IS NULL OR archived = 0)")
        if not include_quarantined:
            # quarantined is NULL on rows that predate the column — treat as live
            clauses.append("(quarantined IS NULL OR quarantined = 0)")
        if not include_paused:
            # D3: paused is NOT NULL DEFAULT 0, but treat NULL as live too —
            # same defensive shape as its siblings above, for a row written
            # through any path that predates this filter being added here.
            clauses.append("(paused IS NULL OR paused = 0)")
        if confirmed is not None:
            clauses.append("confirmed = ?")
            params.append(1 if confirmed else 0)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if mem_type is not None:
            clauses.append("type = ?")
            params.append(mem_type)
        if project is not None or scope is not None:
            scoped = []
            if scope is not None:
                scoped.append("project_scope = ?")
                params.append(scope)
            if project is not None:
                scoped.append("project = ?")
                params.append(project)
            if include_global:
                scoped.append("(project IS NULL AND project_scope IS NULL)")
            clauses.append("(" + " OR ".join(scoped) + ")")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self._fetchall(
            f"SELECT * FROM memories{where} ORDER BY created_at DESC", params
        )
        return [dict(r) for r in rows]

    @serialized_write
    async def stamp_project_scope(self, project: str, scope: str) -> int:
        """Attach the B4 scope identity to legacy path-keyed rows (B4's online
        migration). Runs when a repo is actually SEEN — the only moment the
        path→remote mapping is knowable, since the migration itself cannot run
        git in checkouts that may no longer exist. Only rows still without a
        scope are touched; returns how many were stamped."""
        cur = await self.db.execute(
            "UPDATE memories SET project_scope = ? "
            "WHERE project = ? AND project_scope IS NULL", (scope, project))
        await self.db.commit()
        return cur.rowcount

    # ----------------------------- playbooks ------------------------------ #

    @serialized_write
    async def add_playbook(
        self, *, title: str, trigger_keywords: list[str] | None = None,
        procedure: str = "", postconditions: list[str] | None = None,
        forbidden: list[str] | None = None,
        required_from_user: list[str] | None = None,
        project: str | None = None,
    ) -> str:
        """Insert an operator-authored playbook (1.4). Returns its id."""
        pb_id = uuid.uuid4().hex
        await self.db.execute(
            """INSERT INTO playbooks
                 (id, title, trigger_keywords, procedure, postconditions,
                  forbidden, required_from_user, project)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pb_id, title, json.dumps(trigger_keywords or []), procedure,
             json.dumps(postconditions or []), json.dumps(forbidden or []),
             json.dumps(required_from_user or []), project),
        )
        await self.db.commit()
        return pb_id

    async def list_playbooks(
        self, *, project: str | None = None, include_global: bool = True,
    ) -> list[dict[str, Any]]:
        """All playbooks, optionally scoped to a project (globals included
        unless ``include_global`` is False). Mirrors ``list_memories``."""
        clauses, params = [], []
        if project is not None:
            if include_global:
                clauses.append("(project = ? OR project IS NULL)")
            else:
                clauses.append("project = ?")
            params.append(project)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self._fetchall(
            f"SELECT * FROM playbooks{where} ORDER BY created_at DESC", params
        )
        return [dict(r) for r in rows]

    @serialized_write
    async def delete_playbook(self, prefix: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM playbooks WHERE id = ? OR id LIKE ?",
            (prefix, prefix + "%"))
        await self.db.commit()
        return cur.rowcount > 0

    # --------------------------- PR merge order (2.2) --------------------- #

    @serialized_write
    async def add_pr_edge(self, *, child_pr: str, parent_pr: str,
                          project: str | None = None) -> None:
        """Record that child_pr must merge AFTER parent_pr (2.2)."""
        await self.db.execute(
            "INSERT OR IGNORE INTO pr_edges (child_pr, parent_pr, project) "
            "VALUES (?, ?, ?)", (child_pr, parent_pr, project))
        await self.db.commit()

    async def list_pr_edges(
        self, *, project: str | None = None,
    ) -> list[tuple[str, str]]:
        """All (child_pr, parent_pr) edges, optionally scoped to a project."""
        if project is not None:
            rows = await self._fetchall(
                "SELECT child_pr, parent_pr FROM pr_edges "
                "WHERE project = ? OR project IS NULL", (project,))
        else:
            rows = await self._fetchall(
                "SELECT child_pr, parent_pr FROM pr_edges")
        return [(r["child_pr"], r["parent_pr"]) for r in rows]

    @serialized_write
    async def delete_pr_edges_for(self, pr: str) -> int:
        """Remove every edge touching a PR (e.g. once it merges or closes)."""
        cur = await self.db.execute(
            "DELETE FROM pr_edges WHERE child_pr = ? OR parent_pr = ?", (pr, pr))
        await self.db.commit()
        return cur.rowcount

    # --------------------------- PR outcomes (0010) ------------------------ #
    #
    # A PR's fate is SETTLED once it merged or was closed without merging;
    # `open` and `unknown` are still in flight. Two behaviours key off this one
    # set — the no-downgrade rule in `record_pr_outcome` and the re-poll
    # selection in `list_pr_outcomes` — and they were separate string literals
    # until they were folded into this constant, which is the shape where one
    # can be widened and the other quietly left behind.
    #
    # It restates `vcs.pr_outcome.MERGED`/`CLOSED_UNMERGED` because `core` must
    # not import `vcs`. That duplication is deliberate and it is PINNED:
    # `tests/test_pr_outcome.py::test_db_and_vcs_agree_on_which_outcomes_are_settled`
    # fails if the two spellings ever diverge.

    @serialized_write
    async def record_pr_outcome(
        self, *, task_id: str, pr_url: str,
        outcome: str, outcome_evidence: str = "", ci_status: str | None = None,
        observed_source: str = "live",
        forge: str = "", forge_host: str = "", repo_slug: str = "",
        pr_number: int | None = None,
        opened_at: str | None = None, checked_at: str | None = None,
        attributes: str | None = None,
    ) -> None:
        """Upsert one PR's recorded outcome (migration 0010).

        UPSERT rather than INSERT OR REPLACE: a REPLACE deletes the old row
        first, so every column the refresh path does not pass — notably
        ``opened_at``, which only the PR-open path knows — would be silently
        reset to its default on the first refresh. The excluded-or-keep
        expressions below preserve a value already on the row whenever the
        caller passes None.

        ``ci_status=None`` means "this observation did not look at CI" and KEEPS
        whatever the row already had. That is not the same as ``"unknown"``,
        which means "we looked and could not tell": the wake watcher's state
        rung polls the PR's state without fetching its checks, and writing
        ``unknown`` from it would erase a real ``fail`` that the previous
        refresh had measured.

        THE NO-DOWNGRADE RULE, and why it is in the SQL rather than in a caller.
        A row that has reached a SETTLED outcome (``merged``/``closed_unmerged``)
        is never overwritten by an UNSETTLED one (``open``/``unknown``). A PR
        does not un-merge, so an observation that says it did is not news — it
        is an instrument failure (``gh`` uninstalled, token expired, laptop
        offline), and letting it land would delete the one fact this table
        exists to hold. The old expression was a plain ``outcome =
        excluded.outcome``: every settled row was one broken poll away from
        reverting to ``unknown``, which is the precise failure the caller-side
        ``COALESCE`` on ``ci_status`` was already written to prevent one column
        over. It lives here, in the single statement every writer goes through,
        because a rule enforced in one caller is a rule the next caller does not
        have; ``evidence``/``checked_at``/``observed_source`` move in lockstep
        with ``outcome`` so a kept verdict never ends up wearing the rejected
        observation's justification.

        Settled → settled IS allowed: that is a correction from a better probe
        (a ship check that could not resolve the branch the first time), not a
        regression.
        """
        # `_SETTLED_OUTCOMES` is duplicated from `vcs.pr_outcome` on purpose —
        # `core` must not import `vcs`. `tests/test_pr_outcome.py` pins the two
        # spellings equal, so the duplication cannot drift silently.
        keep = (
            "CASE WHEN pr_outcomes.outcome IN {s} AND excluded.outcome NOT IN {s} "
            "THEN 1 ELSE 0 END"
        ).format(s=_SETTLED_OUTCOMES_SQL)
        await self.db.execute(
            "INSERT INTO pr_outcomes (task_id, pr_url, forge, forge_host, "
            "  repo_slug, pr_number, outcome, outcome_evidence, ci_status, "
            "  observed_source, opened_at, checked_at, attributes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'unknown'), ?, ?, ?, "
            "        COALESCE(?, '{}')) "
            "ON CONFLICT(task_id, pr_url) DO UPDATE SET "
            "  forge = excluded.forge, forge_host = excluded.forge_host, "
            "  repo_slug = excluded.repo_slug, pr_number = excluded.pr_number, "
            f"  outcome = CASE WHEN {keep} = 1 THEN pr_outcomes.outcome "
            "                 ELSE excluded.outcome END, "
            f"  outcome_evidence = CASE WHEN {keep} = 1 "
            "                     THEN pr_outcomes.outcome_evidence "
            "                     ELSE excluded.outcome_evidence END, "
            "  ci_status = COALESCE(?, pr_outcomes.ci_status), "
            f"  observed_source = CASE WHEN {keep} = 1 "
            "                    THEN pr_outcomes.observed_source "
            "                    ELSE excluded.observed_source END, "
            "  opened_at = COALESCE(pr_outcomes.opened_at, excluded.opened_at), "
            f"  checked_at = CASE WHEN {keep} = 1 THEN pr_outcomes.checked_at "
            "               ELSE COALESCE(excluded.checked_at, "
            "                             pr_outcomes.checked_at) END, "
            "  attributes = COALESCE(?, pr_outcomes.attributes)",
            (task_id, pr_url, forge, forge_host, repo_slug, pr_number,
             outcome, outcome_evidence, ci_status, observed_source,
             opened_at, checked_at, attributes, ci_status, attributes))
        await self.db.commit()

    async def list_pr_outcomes(
        self, *, task_id: str | None = None, unsettled_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Recorded PR outcomes, newest-opened first.

        ``unsettled_only`` selects the rows a refresh should re-poll — ``open``
        and ``unknown``. It is spelled as a NEGATIVE (``NOT IN`` the settled
        pair) rather than as a list of the two unsettled values on purpose: a
        row carrying some fifth value written by a future build must be
        re-polled, not silently skipped as if it were settled.
        """
        where, params = [], []
        if task_id is not None:
            where.append("task_id = ?")
            params.append(task_id)
        if unsettled_only:
            where.append(f"outcome NOT IN {_SETTLED_OUTCOMES_SQL}")
        sql = "SELECT * FROM pr_outcomes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(opened_at, checked_at, '') DESC, pr_url"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in await self._fetchall(sql, tuple(params))]

    async def find_memory(self, prefix: str) -> dict[str, Any] | None:
        rows = await self._fetchall(
            "SELECT * FROM memories WHERE id = ? OR id LIKE ? LIMIT 2",
            (prefix, prefix + "%"),
        )
        return dict(rows[0]) if len(rows) == 1 else None

    @serialized_write
    async def archive_memory(self, mem_id: str, reason: str = "") -> bool:
        """Recoverable archive (curator action — never a delete). The reason
        is appended to content so recovery keeps the audit trail."""
        suffix = f"\n\n[archived: {reason}]" if reason else ""
        cur = await self.db.execute(
            "UPDATE memories SET archived = 1, content = content || ? "
            "WHERE id = ? AND archived = 0", (suffix, mem_id))
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def unarchive_memory(self, mem_id: str) -> bool:
        """Reverse of `archive_memory` — the Rules/Skills UI's Restore action
        (Memory lifecycle C part B), and equally the undo for a sweep or a
        supersede, since all three only ever set ``archived = 1``.

        ``superseded_by`` is cleared: a restored row is live again, and
        leaving the pointer would make lineage that reads it (a live row
        with a `superseded_by` set) look like it is still retired. Content —
        including the ``[archived: reason]`` suffix `archive_memory` and
        friends appended — is left alone, as the audit trail.

        Guard is ``archived = 1`` (not the wider ``IS NULL OR = 0`` some
        sibling methods use for "is live" — here we want the strict
        opposite, "was archived", which NULL/0 never satisfy)."""
        cur = await self.db.execute(
            "UPDATE memories SET archived = 0, superseded_by = NULL "
            "WHERE id = ? AND archived = 1", (mem_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def count_quarantined(self, *, mem_type: str | None = None) -> int:
        """How many rows carry ``quarantined = 1`` — the honest count the
        Rules/Skills/Learnings UI footers report (`learning/provenance.py`)."""
        sql = "SELECT COUNT(*) AS n FROM memories WHERE quarantined = 1"
        params: tuple[Any, ...] = ()
        if mem_type is not None:
            sql += " AND type = ?"
            params = (mem_type,)
        row = await self._fetchone(sql, params)
        return int(row["n"]) if row else 0

    @serialized_write
    async def set_quarantine(
        self, mem_id: str, on: bool, reason: str | None = None,
    ) -> bool:
        """Flip the P1 brain-hygiene quarantine flag. Never deletes — the same
        recoverable-row invariant `archive_memory` states above applies here:
        the row and its dedupe key stay in the database either way.

        Stamps ``provenance.quarantine_reason`` (JSON-merged onto whatever the
        row already carries) so a manually-quarantined row records why, the
        same as a write-time quarantine does."""
        row = await self._fetchone(
            "SELECT provenance FROM memories WHERE id = ?", (mem_id,))
        if row is None:
            return False
        try:
            prov = json.loads(row["provenance"]) if row["provenance"] else {}
        except (ValueError, TypeError):
            prov = {}
        if not isinstance(prov, dict):
            prov = {}
        prov["quarantine_reason"] = reason if on else None
        prov["quarantined_at"] = _now() if on else prov.get("quarantined_at")
        cur = await self.db.execute(
            "UPDATE memories SET quarantined = ?, provenance = ? WHERE id = ?",
            (1 if on else 0, json.dumps(prov), mem_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def archive_unconfirmed_older_than(
        self, *, days: int, source: str = SOURCE_PROPOSED, limit: int = 500,
        reason: str = "", dry_run: bool = False,
    ) -> list[str]:
        """Memory lifecycle C: the 45-day auto-archive sweep for unconfirmed
        proposals. Reversible — this sets ``archived = 1``, it never deletes,
        and the same `file_path` dedupe key survives (that is what makes an
        archive "stick": a re-proposed duplicate still hits it).

        This does NOT call `archive_memory` — that method's ``AND archived =
        0`` guard silently no-ops on legacy rows where `archived` is NULL
        (every row written before the column existed), which is exactly the
        shape the sweep must reach. The WHERE clause below is deliberately
        wider: ``archived IS NULL OR archived = 0``.

        Mandatory clauses, each closing a specific hole:
        - ``confirmed = 0`` (literal, not ``IS NULL OR = 0``): an ambiguous
          row is never swept. This is the AC2 wall — a confirmed row can
          never match this method's WHERE clause, full stop.
        - ``source = ?``: never touches board/user-added unconfirmed rows,
          only the queue-visibility contract's own rows.
        - ``created_at IS NOT NULL AND created_at != '' AND
          datetime(created_at) IS NOT NULL AND datetime(created_at) <=
          datetime('now', ?)``: the comparison runs IN SQL, not in Python.
          `created_at` has two live formats — the column DEFAULT
          (``datetime('now')`` → ``"2026-08-12 10:00:00"``) and `_now()`'s ISO
          (``"2026-08-12T10:00:00+00:00"``) — and ``' ' < 'T'`` lexically, so a
          Python-side ISO-string cutoff would mark every default-format row as
          expired regardless of its real age. SQLite's `datetime()` parses
          both wire formats and returns NULL on anything it cannot parse, so
          an unparseable `created_at` is skipped rather than archived — fail
          closed, an absent/garbage input is a refusal, not a sweep target.

        Select-then-update so callers get the ids back (for the sweep's log
        line and for reversal), then one chunked ``UPDATE ... WHERE id IN
        (...)`` at 400 binds (same chunking as `touch_memories_used`) rather
        than N awaited writes behind `serialized_write`'s single connection
        lock.

        ``limit`` truncates the SELECT (``ORDER BY created_at ASC`` — oldest
        first) and does not, itself, sweep everything eligible; callers that
        care must log when ``len(result) == limit`` since a truncated sweep
        looks identical to a complete one otherwise.

        ``dry_run=True`` returns the ids that WOULD be archived and writes
        nothing.

        Raises ``ValueError`` if ``days < 1`` — a zero or negative window
        would archive rows written this very second.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1, got {days}")
        rows = await self._fetchall(
            "SELECT id FROM memories WHERE confirmed = 0 AND source = ? "
            "AND (archived IS NULL OR archived = 0) "
            "AND created_at IS NOT NULL AND created_at != '' "
            "AND datetime(created_at) IS NOT NULL "
            "AND datetime(created_at) <= datetime('now', ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (source, f"-{days} days", limit),
        )
        ids = [r["id"] for r in rows]
        if not ids or dry_run:
            return ids
        suffix = f"\n\n[archived: {reason}]" if reason else ""
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ", ".join("?" for _ in chunk)
            await self.db.execute(
                f"UPDATE memories SET archived = 1, content = content || ? "
                f"WHERE id IN ({marks})",
                (suffix, *chunk),
            )
        await self.db.commit()
        return ids

    @serialized_write
    async def archive_stale_auto_activated(
        self, *, days: int, limit: int = 500,
        reason: str = "", dry_run: bool = False,
    ) -> list[str]:
        """D3 (2026-08-31 operator directive): the automatic-retirement half
        of auto-management. AC2's `retirement_candidates` stays SUGGEST-only
        for every confirmed row — this is the one exception, and it is
        scoped so narrowly it cannot become a second door onto a pinned row:

        - ``confirmed_by = 'auto' AND activated_at IS NOT NULL`` — the ONLY
          rows this can ever select are ones `LearningQueue.auto_activate`
          itself wrote. An operator-pinned or manually-added row (``nh
          rules add``, the board, ``nh reply``, a human's ``confirm``) never
          has ``confirmed_by = 'auto'``/``activated_at`` set, so it cannot
          match this WHERE clause BY CONSTRUCTION — the same "absence is the
          exemption" reasoning ``activated_at``'s own column comment states,
          not a second exemption list that could drift from the first. This
          is `curator.py`'s pinned-exempt rule surviving auto-management.
        - unused for *days*: ``activated_at`` (not merely `created_at`) is at
          least *days* old, AND ``last_used_at`` is either NULL or also at
          least *days* old — a row auto-activated yesterday cannot be
          retired today just because it has not been injected YET; it must
          have HAD the full window to prove itself.
        - ``confirmed = 1 AND (archived IS NULL OR archived = 0)`` — never
          touches an already-archived or (defensively) an unconfirmed row.

        Same shape as `archive_unconfirmed_older_than` otherwise: reversible
        (archive, never delete), `datetime()` comparisons run in SQL for the
        same two-live-format reason, select-then-chunked-update, ``dry_run``
        returns the ids without writing, and ``days < 1`` raises.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1, got {days}")
        window = f"-{days} days"
        rows = await self._fetchall(
            "SELECT id FROM memories WHERE confirmed = 1 "
            "AND confirmed_by = 'auto' AND activated_at IS NOT NULL "
            "AND (archived IS NULL OR archived = 0) "
            "AND datetime(activated_at) IS NOT NULL "
            "AND datetime(activated_at) <= datetime('now', ?) "
            "AND (last_used_at IS NULL OR ("
            "     datetime(last_used_at) IS NOT NULL "
            "     AND datetime(last_used_at) <= datetime('now', ?))) "
            "ORDER BY activated_at ASC LIMIT ?",
            (window, window, limit),
        )
        ids = [r["id"] for r in rows]
        if not ids or dry_run:
            return ids
        suffix = f"\n\n[archived: {reason}]" if reason else ""
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ", ".join("?" for _ in chunk)
            await self.db.execute(
                f"UPDATE memories SET archived = 1, content = content || ? "
                f"WHERE id IN ({marks})",
                (suffix, *chunk),
            )
        await self.db.commit()
        return ids

    @serialized_write
    async def supersede_memory(
        self, old_id: str, new_id: str, reason: str = "",
    ) -> bool:
        """Archive *old_id* with a `superseded_by` pointer to *new_id* — the
        AC3 mechanism: confirming a near-duplicate archives the old row rather
        than leaving two active copies of the same rule.

        Refuses (returns False, writes nothing) when:
        - ``old_id == new_id`` — a row cannot supersede itself.
        - either id does not exist.
        - ``old_id`` is already archived (nothing to supersede — it is
          already inert).
        - ``new_id`` is itself archived, or itself already superseded — no
          chains and no cycles: a pointer always resolves to exactly one live
          row in one hop.

        This method does NOT forbid archiving a ``confirmed = 1`` row — that
        IS the supersede case (an old confirmed rule superseded by a new one)
        — so the "never auto-archive a confirmed row" guarantee lives in the
        CALLER (`LearningQueue.confirm`, which only ever calls this on the
        row it just confirmed superseding an EXISTING near-duplicate that the
        human's own confirm click just judged redundant — never unattended).
        """
        if old_id == new_id:
            return False
        old_row = await self._fetchone(
            "SELECT id, archived FROM memories WHERE id = ?", (old_id,))
        new_row = await self._fetchone(
            "SELECT id, archived, superseded_by FROM memories WHERE id = ?",
            (new_id,))
        if old_row is None or new_row is None:
            return False
        if old_row["archived"]:
            return False
        if new_row["archived"] or new_row["superseded_by"]:
            return False
        suffix = f"\n\n[archived: {reason}]" if reason else ""
        cur = await self.db.execute(
            "UPDATE memories SET archived = 1, superseded_by = ?, "
            "content = content || ? "
            "WHERE id = ? AND (archived IS NULL OR archived = 0) "
            "AND superseded_by IS NULL",
            (new_id, suffix, old_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def touch_memories_used(self, mem_ids: list[str]) -> int:
        """Stamp ``last_used_at`` on every memory in *mem_ids*. Returns the
        number of rows updated.

        ONE statement per chunk, not one per id. This runs on the per-attempt
        hot path (every task start, every review round) with an active set that
        is 71 rows in the operator's own install, and every write here queues
        behind `serialized_write`'s single connection lock — N awaited UPDATEs
        would be N lock acquisitions on the critical path between a task being
        picked up and the coder starting.

        Chunked at 400 ids because `IN (?, ?, …)` is one bind parameter per id
        and SQLite has a variable ceiling (999 on older builds). The active set
        is far below that today; the chunking is here so a future store that is
        not stays correct rather than raising at the worst possible moment.

        ``updated_at`` is deliberately NOT touched. It records when the memory's
        CONTENT last changed — a human confirming or editing it — and injecting
        a rule changes nothing about the rule. Overloading it would erase the
        only timestamp that says when the operator last had an opinion.
        """
        ids = [i for i in (mem_ids or []) if i]
        if not ids:
            return 0
        now = _now()
        total = 0
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ", ".join("?" for _ in chunk)
            cur = await self.db.execute(
                f"UPDATE memories SET last_used_at = ? WHERE id IN ({marks})",
                (now, *chunk),
            )
            total += cur.rowcount
        await self.db.commit()
        return total

    @serialized_write
    async def record_memory_uses(
        self, mem_ids: list[str], *, task_id: str, attempt_id: str | None = None,
    ) -> int:
        """Memory lifecycle A: increment `use_count` and append one
        `memory_uses` ledger row per injected memory. Called immediately
        after `touch_memories_used`, from the same chokepoint
        (`Orchestrator._load_active_memories`) — that call stamps WHEN a
        memory was last used; this one stamps WHICH TASK used it, so a later
        terminal handler can join injection to outcome.

        `task_outcome` starts NULL on every row: it is filled by
        `fill_memory_use_outcomes`, from `run_task`'s finalizer, once the
        task actually reaches a terminal state. Never populated here — the
        outcome of a task that has just started is not knowable yet.

        Chunked at 400 like `touch_memories_used`, for the same reason
        (SQLite's `IN (?, …)` bind-parameter ceiling).
        """
        ids = [i for i in (mem_ids or []) if i]
        if not ids:
            return 0
        now = _now()
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ", ".join("?" for _ in chunk)
            await self.db.execute(
                f"UPDATE memories SET use_count = COALESCE(use_count, 0) + 1 "
                f"WHERE id IN ({marks})", chunk,
            )
        for mem_id in ids:
            await self.db.execute(
                "INSERT INTO memory_uses (id, memory_id, task_id, attempt_id, "
                "injected_at, task_outcome, created_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (uuid.uuid4().hex, mem_id, task_id, attempt_id, now, now),
            )
        await self.db.commit()
        return len(ids)

    @serialized_write
    async def fill_memory_use_outcomes(self, task_id: str, outcome: str) -> int:
        """Stamp `task_outcome` on every still-open (`task_outcome IS NULL`)
        `memory_uses` row for *task_id*.

        Called once, from `Orchestrator.run_task`'s terminal-state finalizer,
        with a coarse label — 'success' | 'failure' | 'cancelled' | 'timeout'
        — resolved from that run's `TaskOutcome`. Every OTHER status
        (`awaiting_approval`, `escalated`, `paused_quota`, `blocked` without
        an operator cancel, …) is a resumable off-ramp, not a verdict, and is
        never passed here — those rows stay NULL until a later `run_task`
        call on the same task actually resolves one of the four.

        `WHERE task_outcome IS NULL` rather than an unconditional UPDATE: a
        task can be resumed after DONE/FAILED (e.g. a follow-up `nh reply`
        that reopens it), and a resume's fresh injections must get THEIR OWN
        outcome, not have an earlier terminal label overwritten onto rows
        that have not happened yet — while rows already resolved by an
        earlier terminal call stay exactly as they were recorded.
        """
        cur = await self.db.execute(
            "UPDATE memory_uses SET task_outcome = ? "
            "WHERE task_id = ? AND task_outcome IS NULL",
            (outcome, task_id),
        )
        await self.db.commit()
        return cur.rowcount

    @serialized_write
    async def record_friction(
        self, *, sig: str, task_id: str, error_excerpt: str,
        repo_path: str | None = None,
    ) -> str | None:
        """Append one open `fix_pairs` friction row (see 0013_fix_pairs.sql).

        Deduped per (sig, task): the SAME error failing the SAME task twice is
        the StuckDetector's business, not new friction — one open row already
        says "this task is stuck on this signature". Returns the row id, or
        None when an open row for the pair already existed.
        """
        if await self._fetchone(
            "SELECT id FROM fix_pairs WHERE sig = ? AND task_id = ? "
            "AND resolution IS NULL", (sig, task_id),
        ):
            return None
        row_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO fix_pairs (id, sig, repo_path, error_excerpt, task_id, "
            "resolution, resolved_task_id, created_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL)",
            (row_id, sig, repo_path, error_excerpt[:300], task_id, _now()),
        )
        await self.db.commit()
        return row_id

    @serialized_write
    async def resolve_friction(
        self, task_id: str, *, resolution: str,
        latest_resolution: str | None = None,
    ) -> int:
        """Fill `resolution` on every still-open friction row of *task_id* —
        the task reached a genuine SUCCESS terminal, so whatever it hit along
        the way was, demonstrably, overcome. Called from the same terminal
        finalizer that stamps `memory_uses.task_outcome`, with the same
        never-load-bearing posture. Open rows only: a resumed task's earlier
        resolved friction keeps its original resolution.

        `latest_resolution` exists because the caller has exactly ONE
        diagnosis (the last `stuck_hypothesis`) and a task can have hit
        several distinct signatures. Writing that diagnosis onto all of them
        pairs error A with the diagnosis of error B and then presents it to a
        future task as "THIS EXACT ERROR SIGNATURE WAS OVERCOME BEFORE: <B>".
        So the diagnosis lands on the NEWEST open row only — the failure the
        fix actually followed — and every older row gets the plain
        *resolution*, which claims only what is true: this task overcame it.
        """
        now = _now()
        newest = None
        if latest_resolution is not None:
            row = await self._fetchone(
                "SELECT id FROM fix_pairs WHERE task_id = ? AND resolution IS NULL "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (task_id,),
            )
            newest = row["id"] if row is not None else None
        n = 0
        if newest is not None:
            cur = await self.db.execute(
                "UPDATE fix_pairs SET resolution = ?, resolved_task_id = task_id, "
                "resolved_at = ? WHERE id = ?",
                (latest_resolution[:600], now, newest),
            )
            n += cur.rowcount
        cur = await self.db.execute(
            "UPDATE fix_pairs SET resolution = ?, resolved_task_id = task_id, "
            "resolved_at = ? WHERE task_id = ? AND resolution IS NULL",
            (resolution[:600], now, task_id),
        )
        n += cur.rowcount
        await self.db.commit()
        return n

    async def find_fix_pair(
        self, sig: str, *, repo_path: str | None = None,
        exclude_task_id: str | None = None,
    ) -> dict[str, Any] | None:
        """The freshest RESOLVED fix pair for *sig* — same repo first.

        `exclude_task_id` keeps a task from being handed its own history as if
        it were independent evidence (the current task's own attempts are
        already in its attempt_log).
        """
        rows_q = (
            "SELECT sig, repo_path, error_excerpt, task_id, resolution, "
            "resolved_at FROM fix_pairs "
            "WHERE sig = ? AND resolution IS NOT NULL"
        )
        params: list[Any] = [sig]
        if exclude_task_id:
            rows_q += " AND task_id != ?"
            params.append(exclude_task_id)
        rows_q += (
            " ORDER BY (CASE WHEN repo_path = ? THEN 0 ELSE 1 END), "
            "resolved_at DESC LIMIT 1"
        )
        params.append(repo_path or "")
        row = await self._fetchone(rows_q, tuple(params))
        return dict(row) if row is not None else None

    async def memory_usage_report(self) -> list[dict[str, Any]]:
        """Per-memory usage stats for `nh learnings --usage` and the
        Learnings/Rules UI rows: use_count, last_used_at, and the outcome
        split of every ledgered injection.

        Read-only, and CORRELATIONAL, not causal — every caller that renders
        this must carry that label; nothing here proves a rule's presence
        changed a task's outcome, only that the two coincided.
        """
        rows = await self._fetchall(
            "SELECT m.id AS memory_id, m.type AS type, m.title AS title, "
            "m.last_used_at AS last_used_at, "
            "COALESCE(m.use_count, 0) AS use_count, "
            "SUM(CASE WHEN u.task_outcome = 'success' THEN 1 ELSE 0 END) "
            "  AS success_count, "
            "SUM(CASE WHEN u.task_outcome = 'failure' THEN 1 ELSE 0 END) "
            "  AS failure_count, "
            "SUM(CASE WHEN u.task_outcome = 'cancelled' THEN 1 ELSE 0 END) "
            "  AS cancelled_count, "
            "SUM(CASE WHEN u.task_outcome = 'timeout' THEN 1 ELSE 0 END) "
            "  AS timeout_count "
            "FROM memories m LEFT JOIN memory_uses u ON u.memory_id = m.id "
            "WHERE COALESCE(m.use_count, 0) > 0 "
            "GROUP BY m.id ORDER BY use_count DESC"
        )
        return [dict(r) for r in rows]

    async def memory_outcome_counts(
        self, mem_ids: list[str],
    ) -> dict[str, dict[str, int]]:
        """The same outcome split as `memory_usage_report`, keyed by memory
        id, for enriching the Rules/Skills/Learnings API responses without a
        full report scan. Ids with no ledger rows are simply absent —
        callers default to zero."""
        ids = [i for i in (mem_ids or []) if i]
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        rows = await self._fetchall(
            f"SELECT memory_id, task_outcome, COUNT(*) AS n FROM memory_uses "
            f"WHERE memory_id IN ({marks}) GROUP BY memory_id, task_outcome",
            ids,
        )
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            d = out.setdefault(
                r["memory_id"],
                {"success_count": 0, "failure_count": 0,
                 "cancelled_count": 0, "timeout_count": 0},
            )
            key = f"{r['task_outcome']}_count"
            if key in d:
                d[key] = r["n"]
        return out

    async def stale_memories(
        self, *, days: int, project: str | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """Confirmed, unarchived memories not injected into a prompt in *days*.

        A NULL ``last_used_at`` counts as stale — but it is genuinely ambiguous
        (never triggered, or written before the column existed), and the caller
        is expected to say so rather than report both as "never used". The
        cutoff and the stored stamps are both `_now()`-format ISO-8601 UTC, so
        the string comparison is a real chronological one.

        READ-ONLY. Nothing here archives or deletes: these are CONFIRMED rows,
        which `learning/curator.py` calls "the operator's — never touched", and
        an unused rule is not a wrong rule. It is a report.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = await self.list_memories(
            confirmed=True, project=project, scope=scope)
        return [r for r in rows
                if not r.get("last_used_at") or r["last_used_at"] < cutoff]

    @serialized_write
    async def confirm_memory(
        self, mem_id: str, *, confirmed_by: str = "human",
    ) -> bool:
        """Promote a proposed memory into the active set (one-click confirm).

        ``confirmed_by`` records WHO confirmed it — 'human' (the default: every
        `nh learnings`/API confirm path) or 'auto' (D3-M1's evidence-gated
        auto-confirm of a recurring review-origin lesson). It is load-bearing
        for gate independence: the reviewer's confirmed-rules channel EXCLUDES
        (origin='review' AND confirmed_by='auto') rows, so an auto-confirmed
        review lesson reaches the coder but never the reviewer that produced it.

        A HUMAN confirm always writes 'human', deliberately overwriting an
        'auto' a prior auto-confirm may have stamped: a human clicking confirm
        is a stronger signal than the auto heuristic, and it promotes the row
        into the reviewer's channel too (a human now stands behind it)."""
        cur = await self.db.execute(
            "UPDATE memories SET confirmed = 1, source = 'confirmed', "
            "confirmed_by = ?, updated_at = ? WHERE id = ?",
            (confirmed_by, _now(), mem_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def delete_memory(self, mem_id: str) -> bool:
        cur = await self.db.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def activate_memory_auto(self, mem_id: str) -> bool:
        """D3 (2026-08-31 operator directive): the auto-activation write.

        Deliberately NOT ``confirm_memory`` — that method always writes
        ``source = 'confirmed'``, the literal that (with `confirm_memory`
        as its only writer) means "a human clicked confirm". Auto-activation
        writes ``source = 'auto'`` instead, so a row's `source` alone tells
        the difference the D3 design calls for, and `confirmed_by = 'auto'`
        keeps the D3-M1 wall intact (the reviewer's confirmed-rules channel
        already excludes ``origin='review' AND confirmed_by='auto'``).

        ``activated_at`` is stamped here and NOWHERE else — it is the column
        the 90-day auto-retirement sweep keys on, and the auto-activation
        pipeline is its only writer. ``WHERE ... AND confirmed = 0`` makes
        this refuse to re-stamp (or downgrade) an already-confirmed row —
        a human's prior confirm, or a second auto-activation attempt on the
        same row, both no-op rather than overwrite.
        """
        now = _now()
        cur = await self.db.execute(
            "UPDATE memories SET confirmed = 1, source = 'auto', "
            "confirmed_by = 'auto', activated_at = ?, updated_at = ? "
            "WHERE id = ? AND confirmed = 0",
            (now, now, mem_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def count_auto_activated_since(self, *, hours: int = 24) -> int:
        """How many rows this store auto-activated in the last *hours* — the
        D3 daily-cap check (`LearningQueue.auto_activate`). A ROLLING window
        on ``activated_at``, not a calendar-day count: `HarvestJob` ticks on
        an interval that need not align to midnight, and a rolling window is
        the only shape that can never be reset early by a tick landing just
        after 00:00."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM memories WHERE confirmed_by = 'auto' "
            "AND activated_at IS NOT NULL AND activated_at >= ?", (cutoff,))
        return int(row["n"]) if row else 0

    @serialized_write
    async def set_paused(self, mem_id: str, on: bool) -> bool:
        """D3: PAUSE / un-pause a learning without archiving it. The row
        stays exactly where the confirm queue or the active set already
        found it — `archived`/`confirmed`/`source` are all untouched; only
        whether it is ever INJECTED changes (`list_memories`'s default
        excludes a paused row)."""
        cur = await self.db.execute(
            "UPDATE memories SET paused = ?, updated_at = ? WHERE id = ?",
            (1 if on else 0, _now(), mem_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    @serialized_write
    async def record_learning_event(
        self, memory_id: str, event: str, *, detail: dict[str, Any] | None = None,
    ) -> str:
        """Append-only audit row for one learning lifecycle transition —
        D3's 'every auto-activated learning records provenance ...
        `learning_events` audit trail for activate/pause/retire',
        generalised to every transition this module makes (a human's
        confirm/retire/restore included, and an `inject` event from
        `Orchestrator._load_active_memories`) so the trail lives in ONE
        place rather than being audited here and not there.

        ``detail`` is stored as JSON. For an ``inject`` event it MUST carry
        WHICH TAGS FIRED (2026-09-01 effectiveness study) — a bare "this
        memory was used" row cannot answer that, and `last_used_at`/
        `use_count` already cover the undifferentiated count. Returns the
        new event id.
        """
        event_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO learning_events (id, memory_id, event, detail, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, memory_id, event,
             json.dumps(detail) if detail is not None else None, _now()),
        )
        await self.db.commit()
        return event_id

    @serialized_write
    async def record_learning_events(
        self, rows: list[tuple[str, str, dict[str, Any] | None]],
    ) -> list[str]:
        """Batched form of `record_learning_event`: ONE `executemany` and ONE
        commit for every `(memory_id, event, detail)` triple in *rows*,
        instead of one write + commit per row.

        Same reasoning, same call site, as `record_memory_uses` and
        `touch_memories_used`: `Orchestrator._load_active_memories`'s
        injection loop wrote one `inject` audit row per selected memory via
        `record_learning_event` — a per-row serialized write + commit on the
        per-attempt hot path — while its two ledger-writing siblings right
        beside it were already batched. Per-row content (each memory's
        `trigger_reason` detail) is unchanged; only the write shape is.

        Returns the new event ids, in the same order as *rows*. Empty input
        is a no-op — mirrors `record_memory_uses`'s empty-list behaviour.
        """
        if not rows:
            return []
        now = _now()
        ids = [uuid.uuid4().hex for _ in rows]
        await self.db.executemany(
            "INSERT INTO learning_events (id, memory_id, event, detail, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (event_id, memory_id, event,
                 json.dumps(detail) if detail is not None else None, now)
                for event_id, (memory_id, event, detail) in zip(ids, rows)
            ],
        )
        await self.db.commit()
        return ids

    async def list_learning_events(
        self, *, memory_id: str | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """The audit trail, newest first — optionally scoped to one memory.
        Read-only; nothing here mutates a `memories` row."""
        if memory_id is not None:
            rows = await self._fetchall(
                "SELECT * FROM learning_events WHERE memory_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (memory_id, limit),
            )
        else:
            rows = await self._fetchall(
                "SELECT * FROM learning_events ORDER BY created_at DESC, "
                "rowid DESC LIMIT ?", (limit,),
            )
        return [dict(r) for r in rows]

    # ----------------------- task events (persisted) ----------------------- #

    @serialized_write
    async def save_events(self, task_id: str, events: list[dict[str, Any]]) -> None:
        """Persist a batch of task events so they survive a server restart."""
        if not events:
            return
        await self.db.executemany(
            "INSERT INTO task_events (task_id, ts, data) VALUES (?, ?, ?)",
            [(task_id, e.get("ts", 0), json.dumps(e)) for e in events],
        )
        await self.db.commit()

    async def list_events(self, task_id: str) -> list[dict[str, Any]]:
        """Return persisted events for a task, ordered oldest → newest."""
        rows = await self._fetchall(
            "SELECT data FROM task_events WHERE task_id = ? ORDER BY ts ASC, id ASC",
            (task_id,),
        )
        return [json.loads(r["data"]) for r in rows]

    async def tasks_waiting_for_slot(self) -> set[str]:
        """Ids whose newest ``waiting_for_slot``/``attempt_start`` event is the
        wait — read-only, mirrors `slot_wait.is_waiting_for_slot` but computed
        in SQL so `nh status` doesn't hydrate every task's full event log."""
        # Mirrors `slot_wait.is_waiting_for_slot`: the wait, or ANY event from
        # a run source (a worker acting on the task), whichever is newest —
        # and only for tasks the scheduler could still claim.
        sources = ",".join(f"'{s}'" for s in sorted(slot_wait.RUN_SOURCES))
        statuses = ",".join(f"'{s}'" for s in sorted(slot_wait.CLAIMABLE_STATUSES))
        rows = await self._fetchall(
            "SELECT e.task_id, json_extract(e.data, '$.kind') AS kind "
            "FROM task_events e JOIN tasks t ON t.id = e.task_id "
            f"WHERE t.status IN ({statuses}) AND ("
            "json_extract(e.data, '$.kind') = ? "
            f"OR json_extract(e.data, '$.source') IN ({sources})"
            ") ORDER BY e.ts ASC, e.id ASC",
            (slot_wait.KIND,),
        )
        latest: dict[str, str] = {}
        for r in rows:
            latest[r["task_id"]] = "wait" if r["kind"] == slot_wait.KIND else "run"
        return {tid for tid, k in latest.items() if k == "wait"}

    async def list_supervisor_corrections(
        self, *, project: str | None = None, limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Every persisted supervisor ``correct`` decision, oldest first (B2).

        The supervisor emits its verdict as a ``supervisor_decision`` event
        whose ``text`` is the action and whose ``message`` is the correction —
        already truncated to 200 chars by ``Orchestrator.emit``'s call site,
        which is the only form that was ever stored. The project is the task's
        ``repo_path``, joined here so the caller clusters per repo without a
        second query per task.

        ``e.task_id`` is the indexed COLUMN, not ``json_extract(data,
        '$.task_id')`` — both are populated and they agree, but only one of
        them can use ``idx_task_events_task_id``.
        """
        clauses = [
            "json_extract(e.data, '$.kind') = 'supervisor_decision'",
            "json_extract(e.data, '$.text') = 'correct'",
        ]
        params: list[Any] = []
        if project is not None:
            clauses.append("t.repo_path = ?")
            params.append(project)
        params.append(int(limit))
        rows = await self._fetchall(
            "SELECT e.task_id AS task_id, t.repo_path AS project, e.ts AS ts, "
            "       json_extract(e.data, '$.message') AS message "
            "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY e.ts ASC LIMIT ?",
            params,
        )
        return [dict(r) for r in rows]

    async def list_escalations(
        self, *, project: str | None = None, limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Every persisted ``escalated`` event, oldest first.

        The orchestrator emits this event with ``blocker=blocker.to_dict()``
        when a task escalates to a human. ``message`` is the blocker's
        ``question`` — NOT the rendered report, which embeds the task's own
        title and id and would make every escalation gist unique, clustering
        nothing. ``category`` is returned alongside it so the caller can drop
        ``NON_LEARNABLE_CATEGORIES`` (environment facts, not reusable lessons).
        """
        clauses = ["json_extract(e.data, '$.kind') = 'escalated'"]
        params: list[Any] = []
        if project is not None:
            clauses.append("t.repo_path = ?")
            params.append(project)
        params.append(int(limit))
        rows = await self._fetchall(
            "SELECT e.task_id AS task_id, t.repo_path AS project, e.ts AS ts, "
            "       json_extract(e.data, '$.blocker.question') AS message, "
            "       json_extract(e.data, '$.blocker.category') AS category "
            "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY e.ts ASC LIMIT ?",
            params,
        )
        return [dict(r) for r in rows]

    async def list_tamper_trips(
        self, *, project: str | None = None, limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Every persisted tamper-guard TRIP, oldest first.

        The orchestrator emits a ``tamper`` event on every check, pass or
        fail (``self.emit("tamper", tamper.summary, tampered=tamper.tampered)``);
        only the trips (``tampered = 1``) are a learnable signal. ``message``
        is the guard's own summary text.
        """
        clauses = [
            "json_extract(e.data, '$.kind') = 'tamper'",
            "json_extract(e.data, '$.tampered') = 1",
        ]
        params: list[Any] = []
        if project is not None:
            clauses.append("t.repo_path = ?")
            params.append(project)
        params.append(int(limit))
        rows = await self._fetchall(
            "SELECT e.task_id AS task_id, t.repo_path AS project, e.ts AS ts, "
            "       json_extract(e.data, '$.text') AS message "
            "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY e.ts ASC LIMIT ?",
            params,
        )
        return [dict(r) for r in rows]

    async def list_review_fails(
        self, *, project: str | None = None, limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Every attempt whose review FAILed and persisted a checklist, oldest
        first. Returns the raw ``review_checklist`` JSON string — parsing it
        into findings belongs one layer up, where ``findings_from_checklist``
        (``review/reviewer.py``) already lives and is already used by
        ``nh task show``.
        """
        clauses = ["a.review_passed = 0", "a.review_checklist IS NOT NULL"]
        params: list[Any] = []
        if project is not None:
            clauses.append("t.repo_path = ?")
            params.append(project)
        params.append(int(limit))
        rows = await self._fetchall(
            "SELECT a.task_id AS task_id, t.repo_path AS project, "
            "       a.review_checklist AS review_checklist, "
            "       a.started_at AS ts "
            "FROM attempts a LEFT JOIN tasks t ON t.id = a.task_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY a.started_at ASC LIMIT ?",
            params,
        )
        return [dict(r) for r in rows]

    async def last_event_ts(self, task_id: str) -> float | None:
        """Epoch seconds of the newest persisted event, or None if none. Used
        by the stuck-active-task watchdog to detect a task frozen mid-run."""
        row = await self._fetchone(
            "SELECT MAX(ts) FROM task_events WHERE task_id = ?", (task_id,))
        return float(row[0]) if row and row[0] is not None else None

    # ----------------------- scheduler heartbeat --------------------------- #
    # Single-row (id=1) leader marker read/written by `Scheduler._claim_pool_
    # lease` — see `_migrate`'s `scheduler_heartbeat` table for why it exists.

    async def read_scheduler_heartbeat(self) -> dict | None:
        """The current lease holder, or None if no process has ever claimed
        one (or `clear_scheduler_heartbeat` cleared it on a clean shutdown).
        ``start_token`` is None for any row written before that column existed
        (or by a caller that could not determine its own token) — callers
        must treat that exactly as the token-less legacy case."""
        row = await self._fetchone(
            "SELECT pid, host, started_at, ts, start_token "
            "FROM scheduler_heartbeat WHERE id = 1")
        return dict(row) if row else None

    @serialized_write
    async def write_scheduler_heartbeat(
        self, *, pid: int, host: str, started_at: str, ts: float,
        start_token: str | None = None,
    ) -> None:
        """Claim or refresh the id=1 lease row (upsert — same
        ``ON CONFLICT ... DO UPDATE`` idiom as `upsert_profile`, since this is
        genuinely a fixed-key upsert, not an append). ``start_token`` defaults
        to None so every existing caller (tests included) keeps writing the
        token-less legacy row shape unless it opts in."""
        await self.db.execute(
            """INSERT INTO scheduler_heartbeat
                 (id, pid, host, started_at, ts, start_token)
                 VALUES (1, :pid, :host, :started_at, :ts, :start_token)
               ON CONFLICT(id) DO UPDATE SET
                 pid=excluded.pid, host=excluded.host,
                 started_at=excluded.started_at, ts=excluded.ts,
                 start_token=excluded.start_token""",
            {"pid": pid, "host": host, "started_at": started_at, "ts": ts,
             "start_token": start_token},
        )
        await self.db.commit()

    @serialized_write
    async def cas_scheduler_heartbeat(
        self, *, pid: int, host: str, started_at: str, ts: float,
        expect: dict | None, start_token: str | None = None,
    ) -> bool:
        """Conditional claim/refresh: write the id=1 row ONLY if it is still
        exactly what the caller read (`expect`), or still absent
        (`expect=None`) — the CAS half of `Scheduler._claim_pool_lease`'s
        fail-closed rewrite (see that docstring). Unlike
        `write_scheduler_heartbeat`'s unconditional upsert, a caller here has
        already read a row (or its absence) and must not blindly overwrite
        whatever is there NOW if it moved since that read.

        Returns whether the write landed. `False` means the row changed
        between the caller's read and this call — the caller has LOST the
        race and must not retry blindly (re-reading and re-deciding is the
        only correct response, since the winner may now be a live sibling
        that owns the lease legitimately).
        """
        if expect is None:
            cur = await self.db.execute(
                """INSERT INTO scheduler_heartbeat
                     (id, pid, host, started_at, ts, start_token)
                     SELECT 1, :pid, :host, :started_at, :ts, :start_token
                     WHERE NOT EXISTS (
                       SELECT 1 FROM scheduler_heartbeat WHERE id = 1)""",
                {"pid": pid, "host": host, "started_at": started_at, "ts": ts,
                 "start_token": start_token},
            )
        else:
            cur = await self.db.execute(
                """UPDATE scheduler_heartbeat
                     SET pid = :pid, host = :host,
                         started_at = :started_at, ts = :ts,
                         start_token = :start_token
                   WHERE id = 1 AND pid = :e_pid AND host = :e_host
                     AND ts = :e_ts""",
                {
                    "pid": pid, "host": host, "started_at": started_at, "ts": ts,
                    "start_token": start_token,
                    "e_pid": expect["pid"], "e_host": expect["host"],
                    "e_ts": expect["ts"],
                },
            )
        await self.db.commit()
        return cur.rowcount == 1

    @serialized_write
    async def clear_scheduler_heartbeat(self, pid: int) -> None:
        """Release the lease on a clean shutdown — ownership-guarded (mirrors
        `cli/commands.py`'s `_release_pid_lock`): only deletes the row if
        *pid* is still the one holding it, so a process that lost the lease
        to a takeover (or never held it) cannot clear a sibling's claim."""
        await self.db.execute(
            "DELETE FROM scheduler_heartbeat WHERE id = 1 AND pid = ?", (pid,))
        await self.db.commit()

    # ----------------------- project profiles ----------------------------- #

    @serialized_write
    async def upsert_profile(self, profile: "ProjectProfile") -> None:
        d = profile.to_dict()
        await self.db.execute(
            """INSERT INTO project_profiles
                 (repo_path, ecosystem, install_cmd, test_cmd, lint_cmd,
                  confirmed, data, updated_at)
               VALUES (:repo_path, :ecosystem, :install_cmd, :test_cmd, :lint_cmd,
                       :confirmed, :data, :updated_at)
               ON CONFLICT(repo_path) DO UPDATE SET
                 ecosystem=excluded.ecosystem, install_cmd=excluded.install_cmd,
                 test_cmd=excluded.test_cmd, lint_cmd=excluded.lint_cmd,
                 confirmed=excluded.confirmed, data=excluded.data,
                 updated_at=excluded.updated_at""",
            {
                "repo_path": d["repo_path"], "ecosystem": d["ecosystem"],
                "install_cmd": d["install_cmd"], "test_cmd": d["test_cmd"],
                "lint_cmd": d["lint_cmd"], "confirmed": 1 if d["confirmed"] else 0,
                "data": json.dumps(d), "updated_at": _now(),
            },
        )
        await self.db.commit()

    async def get_profile(self, repo_path: str) -> "ProjectProfile | None":
        from ..profile import ProjectProfile
        row = await self._fetchone(
            "SELECT data FROM project_profiles WHERE repo_path = ?", (str(repo_path),)
        )
        return ProjectProfile.from_dict(json.loads(row["data"])) if row else None

    async def list_profiles(self) -> list[dict[str, Any]]:
        """Return all onboarded repo profiles as dicts."""
        rows = await self._fetchall(
            "SELECT repo_path, ecosystem, confirmed, data FROM project_profiles "
            "ORDER BY repo_path"
        )
        return [dict(r) for r in rows]

    # ----------------------------- projects --------------------------------- #

    @serialized_write
    async def create_project(self, project: "Project") -> "Project":
        from ..project_model import Project
        row = project.to_row()
        await self.db.execute(
            "INSERT INTO projects (id, name, repo_paths, primary_repo, test_layers) "
            "VALUES (:id, :name, :repo_paths, :primary_repo, :test_layers)",
            row,
        )
        await self.db.commit()
        return project

    async def get_project(self, project_id: str) -> "Project | None":
        from ..project_model import Project
        row = await self._fetchone(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        return Project.from_row(row) if row else None

    async def get_project_by_name(self, name: str) -> "Project | None":
        from ..project_model import Project
        row = await self._fetchone(
            "SELECT * FROM projects WHERE name = ?", (name,)
        )
        return Project.from_row(row) if row else None

    async def list_projects(self) -> list["Project"]:
        from ..project_model import Project
        rows = await self._fetchall(
            "SELECT * FROM projects ORDER BY name"
        )
        return [Project.from_row(r) for r in rows]

    async def find_project_by_repo(self, repo_path: str) -> "Project | None":
        """Find the project whose ``repo_paths`` contains *repo_path*."""
        for proj in await self.list_projects():
            if repo_path in proj.repo_paths:
                return proj
        return None

    @serialized_write
    async def update_project(self, project: "Project") -> None:
        row = project.to_row()
        await self.db.execute(
            "UPDATE projects SET name = :name, repo_paths = :repo_paths, "
            "primary_repo = :primary_repo, test_layers = :test_layers, "
            "updated_at = :updated_at WHERE id = :id",
            {**row, "updated_at": _now()},
        )
        await self.db.commit()

    @serialized_write
    async def delete_project(self, project_id: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM projects WHERE id = ?", (project_id,)
        )
        await self.db.commit()
        return cur.rowcount > 0

    # ----------------------- history cache (Phase 7e) ---------------------- #

    async def history_cache_get(self, content_sig: str) -> dict | None:
        """Return cached ingestion result for a transcript content signature."""
        row = await self._fetchone(
            "SELECT * FROM history_cache WHERE content_sig = ?", (content_sig,)
        )
        return dict(row) if row else None

    @serialized_write
    async def history_cache_put(
        self, content_sig: str, cascade_id: str, title: str, findings_json: str,
    ) -> None:
        """Cache ingestion result keyed by content signature (upsert)."""
        await self.db.execute(
            "INSERT OR REPLACE INTO history_cache "
            "(content_sig, cascade_id, title, findings_json) VALUES (?, ?, ?, ?)",
            (content_sig, cascade_id, title, findings_json),
        )
        await self.db.commit()

    @serialized_write
    async def history_cache_clear(self) -> int:
        """Clear the entire history cache (Re-scan). Returns rows deleted."""
        cur = await self.db.execute("DELETE FROM history_cache")
        await self.db.commit()
        return cur.rowcount

    async def list_history_cache(self) -> list[dict[str, Any]]:
        """All cached IDE-transcript ingestion results (title + findings),
        most recent first — for `nh recall` to search alongside tasks/memories."""
        rows = await self._fetchall(
            "SELECT * FROM history_cache ORDER BY ingested_at DESC"
        )
        return [dict(r) for r in rows]

    # ---- wiki generation jobs (migrations/0017) --------------------------- #

    @serialized_write
    async def create_wiki_job(self, repo_path: str) -> str:
        """Insert a queued wiki-generation job for *repo_path*; return its id."""
        job_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO wiki_jobs (id, repo_path, status, created_at) "
            "VALUES (?, ?, 'queued', ?)",
            (job_id, repo_path, _now()),
        )
        await self.db.commit()
        return job_id

    async def get_wiki_job(self, job_id: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT * FROM wiki_jobs WHERE id = ?", (job_id,))
        return dict(row) if row else None

    @serialized_write
    async def update_wiki_job(self, job_id: str, **fields: Any) -> None:
        """Update one job's mutable columns. Keys are whitelisted so only the
        job's own columns can be written, never arbitrary SQL."""
        allowed = {"status", "error", "files", "started_at", "finished_at"}
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        sql = "UPDATE wiki_jobs SET " + ", ".join(f"{c} = ?" for c in cols)
        sql += " WHERE id = ?"
        await self.db.execute(sql, [fields[c] for c in cols] + [job_id])
        await self.db.commit()

    async def list_wiki_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        """All wiki jobs, newest first; optionally filtered by status."""
        if status is None:
            rows = await self._fetchall(
                "SELECT * FROM wiki_jobs ORDER BY created_at DESC")
        else:
            rows = await self._fetchall(
                "SELECT * FROM wiki_jobs WHERE status = ? ORDER BY created_at DESC",
                (status,))
        return [dict(r) for r in rows]
