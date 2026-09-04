"""Two tasks, one Store: the shared-connection write race (KI-1).

The pool runs `concurrency.max_workers` tasks against a single
`aiosqlite.Connection`. aiosqlite serialises individual operations on its
worker thread but not *sequences* of them, so before the fix a second
coroutine's `commit()` could land in the middle of another's write. Two
symptoms, both covered here:

  * the crash — `OperationalError: cannot commit transaction - SQL statements
    in progress`, raised when the interrupted statement was a writer that had
    produced a row (`UPDATE … RETURNING`);
  * the silent one — every multi-statement write in `Store` claims implicit
    atomicity, and a foreign commit split it.

The atomicity tests all observe through a SECOND connection, because the
connection doing the write sees its own uncommitted rows and would prove
nothing.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(store_factory):
    # Variant: `observer` below opens a SECOND connection to the exact same
    # file, so the filename is load-bearing and shared between fixtures.
    return await store_factory("c.db")


@pytest.fixture
async def observer(store_factory, store):
    """A second connection to the same file — sees COMMITTED state only."""
    return await store_factory("c.db")


async def _mk_task(store, title="t") -> Task:
    t = Task.new(title, repo_path="/tmp/r")
    await store.create_task(t)
    return t


def _park_after(store, marker: str):
    """Freeze the next Store write inside its statement sequence, right after
    the statement whose SQL contains *marker*. Returns (reached, release)."""
    reached, release = asyncio.Event(), asyncio.Event()
    original = store.db.execute

    async def patched(sql, *args, **kwargs):
        result = await original(sql, *args, **kwargs)
        if marker in sql:
            store.db.execute = original  # park once, not on every statement
            reached.set()
            await release.wait()
        return result

    store.db.execute = patched
    return reached, release


# --------------------------- the crash itself --------------------------- #


async def test_concurrent_writers_can_always_commit(store):
    """The reported bug: one coroutine mid-write, another commits -> boom.

    Pre-fix this raised `cannot commit transaction - SQL statements in
    progress` on every run of this loop; it is the same error that killed
    attempts in `test_two_repos_run_concurrently_in_worktrees`.
    """
    a = await _mk_task(store, "a")
    b = await _mk_task(store, "b")
    errors: list[BaseException] = []

    async def updater():
        for _ in range(200):
            try:
                await store.update_task(a)
            except BaseException as exc:  # noqa: BLE001 - the assertion is "none"
                errors.append(exc)
                return
            await asyncio.sleep(0)

    async def committer():
        for i in range(200):
            try:
                await store.set_status(
                    b,
                    TaskStatus.IMPLEMENTING if i % 2 else TaskStatus.TESTING,
                    validate=False,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return
            await asyncio.sleep(0)

    await asyncio.gather(updater(), committer())
    assert errors == []


async def test_a_cancelled_write_does_not_wedge_the_connection(store):
    """A write cancelled between its statement and its commit must leave the
    connection usable.

    Honest scope: this passes on the pre-fix code too. Dropping the last
    reference to a `sqlite3.Cursor` finalizes its statement, so unwinding the
    cancelled frame happens to reset the live `UPDATE … RETURNING` writer. It is
    a guard, not a proof of the fix: it fails the moment anything starts holding
    a cursor on the Store (a cache, a lazily-consumed iterator), which would
    make a cancellation wedge every later commit rather than lose one attempt.
    """
    a = await _mk_task(store, "a")
    reached, release = _park_after(store, "kind=:kind")   # after update_task's UPDATE
    writer = asyncio.ensure_future(store.update_task(a))
    await asyncio.wait_for(reached.wait(), 5)
    writer.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await writer

    # The connection must still be able to commit.
    await store.set_status(a, TaskStatus.IMPLEMENTING, validate=False)
    assert (await store.get_task(a.id)).status == TaskStatus.IMPLEMENTING


# ------------------------- atomicity, per write ------------------------- #


async def test_create_attempt_update_plus_insert_stays_atomic(store, observer):
    """UPDATE (close superseded attempts) + INSERT (the new one) is one unit."""
    t = await _mk_task(store)
    other = await _mk_task(store, "other")  # before parking: writes take the lock
    await store.create_attempt(t.id, 1)  # left in_progress, as a crash leaves it

    reached, release = _park_after(store, "status = 'interrupted'")
    writer = asyncio.ensure_future(store.create_attempt(t.id, 2))
    await asyncio.wait_for(reached.wait(), 5)

    # A concurrent Store write must not be able to commit the half-done unit.
    foreign = asyncio.ensure_future(
        store.set_status(other, TaskStatus.IMPLEMENTING, validate=False))
    for _ in range(20):
        await asyncio.sleep(0)

    rows = await observer.list_attempts(t.id)
    assert [r["status"] for r in rows] == ["in_progress"], (
        "a foreign commit exposed create_attempt's UPDATE without its INSERT")

    release.set()
    await writer
    await foreign
    rows = await observer.list_attempts(t.id)
    assert [(r["attempt_number"], r["status"]) for r in rows] == [
        (1, "interrupted"), (2, "in_progress")]


async def test_update_attempt_read_modify_write_stays_atomic(store, observer):
    """SELECT failure_reason -> UPDATE: the read must not be committed away."""
    t = await _mk_task(store)
    att = await store.create_attempt(t.id, 1)

    reached, release = _park_after(store, "UPDATE attempts SET")
    writer = asyncio.ensure_future(store.update_attempt(att, status="failed"))
    await asyncio.wait_for(reached.wait(), 5)

    foreign = asyncio.ensure_future(
        store.set_status(t, TaskStatus.IMPLEMENTING, validate=False))
    for _ in range(20):
        await asyncio.sleep(0)
    assert (await observer.list_attempts(t.id))[0]["status"] == "in_progress"

    release.set()
    await writer
    await foreign
    row = (await observer.list_attempts(t.id))[0]
    assert row["status"] == "failed"
    assert "no failure reason recorded" in (row["failure_reason"] or "")


async def test_add_memory_dedupe_then_insert_stays_atomic(store, observer):
    """SELECT dedupe -> INSERT is a read-modify-write: two concurrent inserts of
    the same dedupe_key must not both pass the check and both insert."""
    results = await asyncio.gather(*[
        store.add_memory(mem_type="lesson", title=f"m{i}", content="c",
                         dedupe_key="k1")
        for i in range(4)
    ])
    assert sum(r is not None for r in results) == 1, (
        "concurrent dedupe checks all ran before any insert")
    assert len(await observer.list_memories()) == 1


async def test_update_task_write_and_readback_stay_atomic(store, observer):
    """UPDATE + status read-back + COMMIT is one unit; the read-back must see
    this write's own row, and nothing may commit it early."""
    t = await _mk_task(store, "before")
    other = await _mk_task(store, "other")  # before parking: writes take the lock
    reached, release = _park_after(store, "SELECT status FROM tasks WHERE id")
    t.title = "after"
    writer = asyncio.ensure_future(store.update_task(t))
    await asyncio.wait_for(reached.wait(), 5)

    foreign = asyncio.ensure_future(
        store.set_status(other, TaskStatus.IMPLEMENTING, validate=False))
    for _ in range(20):
        await asyncio.sleep(0)
    assert (await observer.get_task(t.id)).title == "before"

    release.set()
    await writer
    await foreign
    assert (await observer.get_task(t.id)).title == "after"


async def test_merge_context_write_then_readback_stays_atomic(store, observer):
    """merge_context commits, then reads back — the read must observe its own
    merge, and concurrent merges of different keys must all survive.

    Honest scope: this passes on the pre-fix code too — the merge is a single
    atomic `json_patch` UPDATE, so it was never split. It pins that property
    against a future rewrite into a Python-side read-modify-write, which the
    write lock would then be the only thing making safe.
    """
    t = await _mk_task(store)
    await store.merge_context(t.id, {"a": 1})

    async def merge(key, val):
        return await store.merge_context(t.id, {key: val})

    results = await asyncio.gather(*[merge(f"k{i}", i) for i in range(10)])
    for i, ctx in enumerate(results):
        assert ctx[f"k{i}"] == i, "read-back missed its own merge"
    final = (await observer.get_task(t.id)).context
    assert final["a"] == 1
    assert all(final[f"k{i}"] == i for i in range(10)), "a merge was lost"


# ---------------- the critical section's own two invariants ---------------- #


async def test_two_stores_in_one_task_do_not_self_deadlock(store, observer):
    """The exemption is a SET of Stores, not one slot.

    Two (now three) Stores in one process is this product's normal shape —
    `nh start` runs the pool's Store plus the Jira poller's and the Linear
    poller's — one shared Store since 2026-08-03 (`start._go`, `app.state._external_store`)
    — so an
    ``A -> B -> A`` call chain is reachable as soon as any code path touches
    both. With a single slot, entering B overwrote the record that A was held,
    and the return into A then awaited a lock this very task already owns:
    a permanent self-deadlock, not a slow one.

    The section is private, and this test reaches for it deliberately: there is
    no public method that enters two Stores today, which is exactly why the
    trap was invisible.
    """
    async def chain():
        async with store._critical():
            async with observer._critical():
                async with store._critical():     # hung here, pre-fix
                    return "reached"

    assert await asyncio.wait_for(chain(), timeout=2) == "reached"


async def test_a_task_spawned_inside_the_section_is_refused_loudly(store):
    """A Task created INSIDE a critical section inherits the exemption.

    `asyncio` copies the context into each new Task, so the child starts life
    believing it already holds this Store's section, takes the reentrant fast
    path, and runs on the shared connection with no lock held while the parent
    is still inside. The documented invariant used to say this could not happen
    ("invisible to every other task"); context copying is precisely how it does.

    Nothing spawns a task in there today, so the guard cannot fix a live bug —
    it makes a latent one loud instead of silent. The child must raise, not
    proceed.
    """
    inner_error: list[BaseException] = []

    async def child():
        try:
            await store.get_task("nope")          # takes _critical -> fast path
        except BaseException as exc:              # noqa: BLE001
            inner_error.append(exc)

    async with store._critical():
        await asyncio.create_task(child())

    assert inner_error, (
        "a task spawned inside the critical section inherited the exemption "
        "and ran UNGUARDED on the shared connection")
    assert isinstance(inner_error[0], RuntimeError)
    assert "inherited another task's critical-section exemption" in str(inner_error[0])

    # ...and the guard must not fire on the ordinary case: a task created
    # OUTSIDE any section copies a context whose set is empty, so it simply
    # waits for the lock.
    assert await asyncio.wait_for(
        asyncio.create_task(store.get_task("nope")), timeout=5) is None


# ------------------------------ drift guard ------------------------------ #


async def test_every_committing_store_method_is_serialized():
    """Any future `Store` method that commits must take the write lock, or the
    race comes straight back. Checked against the real attribute, not source
    text: an undecorated committer fails here."""
    import no_human.core.db as db_mod

    src = inspect.getsource(Store)
    missing = []
    for name, member in vars(Store).items():
        if not inspect.iscoroutinefunction(member):
            continue
        try:
            body = inspect.getsource(member)
        except OSError:  # pragma: no cover
            continue
        if "self.db.commit()" not in body:
            continue
        if not getattr(member, "__nh_serialized_write__", False):
            missing.append(name)
    assert missing == [], f"Store methods commit without the write lock: {missing}"
    assert src and db_mod.serialized_write  # the guard is testing the real class


# ------------------ the write that fails instantly (the wedge) -------------- #
#
# A SECOND connection to the same file is normal for this product: `nh start`
# opens one for the Jira poller and a third for the Linear poller
# (`cli/commands.py::start._go`, ONE shared Store via `app.state._external_store`) beside the
# pool's own, and every `nh` CLI command opens another in its own process. `Store.connect()` itself WRITES (migration
# 0009 drops and recreates the FTS trigger on every connect), so a bare
# `connect()` + `close()` is enough — every `nh` invocation is a qualifying
# peer, verified.
#
# That is only half of it. The other half is an open read transaction on the
# pool's connection. A write attempted from inside one has to UPGRADE it, and
# SQLite will not run its busy handler for an upgrade (waiting there can only
# deadlock), so it fails at once instead of after `busy_timeout`.
#
# TWO codes, and the traceback tells them apart from nothing: same file, same
# line, same message `database is locked`.
#
#   * SQLITE_BUSY_SNAPSHOT (517) when a peer has COMMITTED past the snapshot
#     this connection is pinned to. Permanent until the statement is reset.
#   * SQLITE_BUSY (5) when the upgrade merely loses to a peer holding the write
#     lock at that moment.
#
# The realistic two-Store storm below produced BOTH, mixed, within single runs
# on the parent commit — so the production incident's code was never inferable
# from the file, line and message it was inferred from, and nothing here claims
# it. Either way the message is a lie about the cause: the file stays writable
# by every other connection throughout, so the usual external-writer probe
# (`BEGIN IMMEDIATE` from the `sqlite3` CLI) returns in milliseconds and reports
# a healthy database while the server cannot write at all.
#
# WHAT IS STILL UNEXPLAINED. The incident's uniform ~24 errors/minute cadence
# has no identified source, and nothing here explains it. The incident logs were
# not retained; the live database holds ZERO attempts with a lock-related
# `failure_reason` (of 684 attempts, 539 of which carry some failure_reason),
# and the only 8 events in it that mention `database is locked` are one agent's
# prose ABOUT this bug, not incident records. That is consistent with the storm
# blocking the very writes that would have recorded it, and equally consistent
# with the cadence having another source entirely. "This removes the whole class
# regardless" is plausible and unfalsified; it is not established, and it must
# not be written up as if the cadence had been explained.


async def _peer_commit(observer) -> None:
    """Advance the WAL from a second connection, as the Jira poller does."""
    await _mk_task(observer, "peer")


async def test_a_live_read_cursor_cannot_break_a_concurrent_write(store, observer):
    """The production failure SHAPE, deterministically.

    One coroutine is parked mid-read on the shared connection with its read
    transaction open; a peer connection commits; a SECOND coroutine then writes.
    Pre-fix that write died instantly with `database is locked`, which is the
    `db.py update_attempt` traceback seen in the live server.

    Scope, stated so this is not read as more than it is: the parked-cursor
    setup makes the failure deterministic, and determinism is bought by parking.
    It reproduces the SHAPE of the incident, not the incident — see
    `test_pool_and_peer_connection_never_produce_a_lock_storm` for the unparked
    version, which is the one whose 4-errors-to-0 result carries the weight.

    The write must now WAIT for the read instead of failing: reads and writes
    share one critical section, so this is a lock hand-off, not a race.
    """
    for i in range(5):
        await _mk_task(store, f"row{i}")
    t = await _mk_task(store, "victim")
    attempt_id = await store.create_attempt(t.id, 1)

    # Park a multi-row read with its cursor live and unexhausted.
    reached, release = _park_after(store, "SELECT * FROM tasks ORDER BY")
    reading = asyncio.create_task(store.list_tasks())
    await asyncio.wait_for(reached.wait(), timeout=5)

    await _peer_commit(observer)           # WAL moves past the pinned snapshot

    writing = asyncio.create_task(
        store.update_attempt(attempt_id, status="failed"))
    await asyncio.sleep(0.05)              # let the writer reach the connection
    release.set()

    await asyncio.wait_for(reading, timeout=5)
    await asyncio.wait_for(writing, timeout=5)      # raised, pre-fix

    rows = await store.list_attempts(t.id)
    assert rows[0]["status"] == "failed"


# THE TREE THESE SCAN. Derived from THIS FILE's location, never from
# `no_human.__file__`. A scan root taken from the imported package follows
# `sys.path`, so without `PYTHONPATH=$PWD/src` the guard walked the developer's
# main checkout: it reported 11 offenders from a tree that was not under test,
# while the tree that WAS under test went unscanned and unjudged. A list
# governing a tree has to be derived from that tree.
PKG_ROOT = Path(__file__).resolve().parents[1] / "src" / "no_human"
DB_PY = PKG_ROOT / "core" / "db.py"


def _package_sources() -> list[Path]:
    assert PKG_ROOT.is_dir(), (
        f"the guard's scan root does not exist: {PKG_ROOT}. This test derives "
        "it from its own location, so a missing directory means the checkout "
        "layout moved — it does NOT mean the tree is clean.")
    return sorted(PKG_ROOT.rglob("*.py"))


_FETCHES = {"fetchone", "fetchall", "fetchmany"}


def _unsafe_reads_in_store(src: str) -> list[str]:
    """Every read inside `Store` that steps a cursor the helpers do not own.

    AST, not a regex over `await X.fetchone()`, because the shapes that evade a
    regex are the dangerous ones: `fetchmany` and `async for` leave the
    statement live PAST the fetch (see the measured table in `db.py`), which is
    the pin-forever case, and `async with self.db.execute(...)` closes the
    cursor but still leaves the `execute`-to-fetch gap outside the lock.

    DML cursors kept for `.rowcount` are not reads and stay exempt: only `fetch*`
    calls, `async for`, and a context-managed `execute` are flagged.
    """
    cls = next(n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef) and n.name == "Store")
    offenders: list[str] = []
    for member in cls.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if member.name in ("_fetchone", "_fetchall"):
            continue                      # these two ARE the safe implementation
        for node in ast.walk(member):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _FETCHES):
                offenders.append(f"{member.name}:{node.lineno} .{node.func.attr}()")
            elif isinstance(node, ast.AsyncFor):
                offenders.append(f"{member.name}:{node.lineno} `async for` (a "
                                 "cursor iterated this way stays live between "
                                 "64-row chunks)")
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp,
                                   ast.DictComp)) and any(
                    g.is_async for g in node.generators):
                offenders.append(f"{member.name}:{node.lineno} `async for` "
                                 "inside a comprehension — same cursor, same pin")
            elif isinstance(node, ast.AsyncWith):
                for item in node.items:
                    ctx = item.context_expr
                    if (isinstance(ctx, ast.Call)
                            and isinstance(ctx.func, ast.Attribute)
                            and ctx.func.attr in ("execute", "executemany")):
                        offenders.append(
                            f"{member.name}:{node.lineno} `async with "
                            f".{ctx.func.attr}(…)`")
    return offenders


def _raw_connection_escapes(src: str) -> list[int]:
    """Every `<expr>.db` attribute access — the ONE door to the bare connection.

    Scanning the ATTRIBUTE rather than the call is the entire point. The first
    version of this guard matched the text `.db.execute(`, which sees
    `store.db.execute(...)` and is blind to `db = store.db` followed later by
    `db.execute(...)` — and to `.db.executescript(`. `core/metrics.py` (11 raw
    cursors) and `core/health.py` (3) were both doing exactly the first of
    those, on the board's live store, and the text matcher scored 0 offenders on
    both files. Ban the resource, not one spelling of the accessor.
    """
    return [n.lineno for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Attribute) and n.attr == "db"]


def _connection_holders(src: str) -> list[str]:
    """Every `Store` member that touches `self._db`, the real connection."""
    cls = next(n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef) and n.name == "Store")
    out = []
    for member in cls.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(isinstance(n, ast.Attribute) and n.attr == "_db"
               and isinstance(n.value, ast.Name) and n.value.id == "self"
               for n in ast.walk(member)):
            out.append(member.name)
    return sorted(set(out))


def _members_returning_the_connection(src: str) -> list[str]:
    """Every `Store` member that RETURNS `self._db` — the leak the pinned list
    above is really about, stated as a property instead of a name list."""
    cls = next(n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef) and n.name == "Store")
    out = []
    for member in cls.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(member):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            for v in ast.walk(node.value):
                if (isinstance(v, ast.Attribute) and v.attr == "_db"
                        and isinstance(v.value, ast.Name) and v.value.id == "self"):
                    out.append(member.name)
    return sorted(set(out))


def test_the_connection_has_exactly_one_public_accessor():
    """The chokepoint claim, enumerated instead of assumed.

    `test_nothing_outside_db_py_touches_the_raw_connection` bans the `.db`
    attribute across the tree, and that is only a chokepoint while `.db` is the
    ONLY way out of this module. `core/db.py` is exempt from that scan wholesale,
    so a second accessor added in here — `Store.connection`, a `cursor()`
    helper, anything returning `self._db` — would hand the connection out under
    a name the tree guard has never heard of, and every call site would pass.
    That is the one gap the tree guard cannot close by construction, so it is
    closed here: the set of members that may touch `self._db` is pinned.
    """
    assert _connection_holders(DB_PY.read_text()) == [
        "__init__", "_rollback_quietly", "close", "connect", "db", "reconnect"], (
        "a Store member other than the pinned set touches self._db. "
        "If it hands the connection out, the tree-wide guard is no longer a "
        "chokepoint — every caller of the new accessor is invisible to it.")

    # `_rollback_quietly` and `reconnect` (2026-08-02, the frozen-snapshot fix)
    # touch `self._db` to END a connection's life — roll its transaction back,
    # or replace it — which is the opposite of handing it out. Widening an
    # allowlist weakens it, so the property the list was standing in for is
    # asserted directly instead: `db` is still the ONLY member that returns the
    # connection. That covers every future member, named or not.
    holders_returning_db = _members_returning_the_connection(DB_PY.read_text())
    assert holders_returning_db == ["db"], (
        f"these Store members return the raw connection: {holders_returning_db}. "
        "Only the `db` property may, or `.db` stops being the chokepoint the "
        "tree-wide guard scans for.")

    # Known positive for the returns-check: it must see a leak under any name,
    # and must not be satisfied by a member that merely touches `self._db`.
    assert _members_returning_the_connection(
        "class Store:\n"
        "    def handle(self):\n        return self._db\n") == ["handle"]
    assert _members_returning_the_connection(
        "class Store:\n"
        "    async def r(self):\n        await self._db.rollback()\n") == []

    # Known positive: the guard must see a second accessor, whatever it is called.
    assert _connection_holders(
        "class Store:\n"
        "    def connection(self):\n        return self._db\n") == ["connection"]
    # ...and must not be satisfied by a member that merely mentions `db`.
    assert _connection_holders(
        "class Store:\n"
        "    async def q(self, sql):\n        return await self.db.execute(sql)\n") == []


def test_no_store_read_keeps_a_raw_cursor():
    """Drift guard, the companion to `test_every_committing_store_method_is_
    serialized`. Every read must go through `_fetchone`/`_fetchall`, which close
    the cursor and take the connection's critical section; a hand-rolled
    `cur = await self.db.execute("SELECT …")` + a fetch reintroduces the open
    read transaction, so it is banned here rather than left to review."""
    offenders = _unsafe_reads_in_store(DB_PY.read_text())
    assert offenders == [], (
        "these Store methods step a cursor instead of using "
        f"_fetchone/_fetchall: {offenders}")


def test_the_store_read_guard_fires_on_every_shape_it_claims_to_catch():
    """The guard's own known positives. A clean result from an unproven matcher
    is indistinguishable from a matcher that matches nothing, and four of these
    seven shapes (`fetchmany`, both `async for` forms, `async with`) are ones
    the previous regex-based guard passed silently — `async for` most of all,
    since that is the shape that holds the statement open past the fetch."""
    positives = {
        "fetchone": "        cur = await self.db.execute('SELECT 1')\n"
                    "        return await cur.fetchone()\n",
        "fetchall": "        cur = await self.db.execute('SELECT 1')\n"
                    "        return await cur.fetchall()\n",
        "fetchmany": "        cur = await self.db.execute('SELECT 1')\n"
                     "        return await cur.fetchmany(10)\n",
        "async for": "        cur = await self.db.execute('SELECT 1')\n"
                     "        async for row in cur:\n            return row\n",
        "async for in a comprehension":
                     "        cur = await self.db.execute('SELECT 1')\n"
                     "        return [r async for r in cur]\n",
        "async with": "        async with self.db.execute('SELECT 1') as cur:\n"
                      "            return 1\n",
        "aliased cursor": "        c = self.db\n"
                          "        cur = await c.execute('SELECT 1')\n"
                          "        return await cur.fetchone()\n",
    }
    for label, body in positives.items():
        src = f"class Store:\n    async def leaky(self):\n{body}"
        assert _unsafe_reads_in_store(src), f"the guard MISSES {label!r}"

    # ...and the negatives it must not flag, or it is unusable in review.
    negatives = {
        "the helpers themselves":
            "class Store:\n    async def _fetchone(self, sql):\n"
            "        cur = await self.db.execute(sql)\n"
            "        return await cur.fetchone()\n",
        "a DML cursor kept for rowcount":
            "class Store:\n    async def purge(self):\n"
            "        cur = await self.db.execute('DELETE FROM t')\n"
            "        return cur.rowcount\n",
        "the critical section itself":
            "class Store:\n    async def w(self):\n"
            "        async with self._critical():\n            return 1\n",
    }
    for label, src in negatives.items():
        assert _unsafe_reads_in_store(src) == [], f"the guard FLAGS {label!r}"


def test_nothing_outside_db_py_touches_the_raw_connection():
    """The same ban, for the rest of the tree.

    `Store.db` is the bare aiosqlite connection; reaching it anywhere else opens
    a cursor OUTSIDE the critical section — which is how `context/sessions.py`
    (on the task path, during context gathering), the board's event search and
    `core/metrics.py` + `core/health.py` (behind `/api/metrics` and the board's
    queue-health tile, on the live store) could hold a read transaction open
    under the running pool. They go through `Store.query`/`query_one` now; new
    ones must too.

    The last two were found BY this guard and only after it stopped matching
    the text `.db.execute(`: both aliased the connection first (`db = store.db`),
    which hid fourteen raw cursors from the previous spelling. The text matcher
    scored 0 offenders on those two files; this one scores 3 escape points.
    """
    offenders = []
    for path in _package_sources():
        if path == DB_PY:
            continue          # the one module that OWNS the connection
        offenders += [f"{path.relative_to(PKG_ROOT)}:{n}"
                      for n in _raw_connection_escapes(path.read_text())]
    assert offenders == [], (
        "these reach past Store into the raw connection; use "
        f"Store.query/query_one: {offenders}")


def test_the_raw_connection_guard_fires_on_every_evasion_it_claims_to_catch():
    """Known positives for the tree-wide guard, including the two spellings the
    `.db.execute(` text matcher passed silently."""
    positives = {
        "the plain call":        "rows = await store.db.execute('SELECT 1')\n",
        "executescript":         "await store.db.executescript('SELECT 1')\n",
        "the aliased cursor":    "conn = store.db\nawait conn.execute('SELECT 1')\n",
        "a deeper attribute path": "await app.state.store.db.execute('SELECT 1')\n",
        "a bare handout":        "return self.store.db\n",
    }
    for label, src in positives.items():
        assert _raw_connection_escapes(src), f"the guard MISSES {label!r}"
    assert _raw_connection_escapes("rows = await store.query('SELECT 1')\n") == [], (
        "the guard flags the SAFE spelling it is supposed to steer people to")


def test_the_cited_peer_store_symbols_exist():
    """The comments above cite `cli/commands.py::start._go` for the second and
    third Store, and a citation nobody checks is a citation that rots.

    This one already rotted once: an earlier revision cited `:3772` and `:3801`,
    which were exact on the branch tip and off by four on the merge result,
    because main edited `cli/commands.py` in a commit neither side could see
    alone. Line numbers move under every edit above them; a symbol moves only
    when someone renames it, and then this fails and says so.
    """
    src = (PKG_ROOT / "cli" / "commands.py").read_text()
    start = next((n for n in ast.walk(ast.parse(src))
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "start"), None)
    assert start is not None, "cli/commands.py::start is gone — fix the citations"
    go = next((n for n in ast.walk(start)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_go"), None)
    assert go is not None, "cli/commands.py::start._go is gone — fix the citations"

    opened = {t.id for n in ast.walk(go) if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)
              and isinstance(n.value, ast.Await)
              and "Store(" in ast.unparse(n.value)}
    # Since the 2026-08-03 single-store rescue, start._go opens exactly ONE
    # Store and shares it (app.state._external_store); the old jira_store/
    # linear_store peers were the lock-flood defect. The guard keeps citing by
    # hand so the docstrings and the code cannot drift apart silently.
    assert opened == {"store"}, (
        "start._go must open exactly the one shared Store named `store`; found "
        f"{sorted(opened)} — update the db.py docstrings AND this guard together")


def test_the_tree_guards_scan_the_tree_under_test():
    """D6: the scan root must come from this checkout, not from `sys.path`.

    Without `PYTHONPATH=$PWD/src`, `no_human.__file__` resolves to whatever
    other checkout is installed, so the guards above graded the wrong tree and
    reported its offenders as this branch's. Pin the derivation itself.
    """
    assert PKG_ROOT == Path(__file__).resolve().parents[1] / "src" / "no_human"
    assert DB_PY.is_file(), DB_PY
    assert (PKG_ROOT / "core" / "metrics.py").is_file()
    # The tests directory and the scanned package are siblings in ONE tree.
    assert PKG_ROOT.parents[1] == Path(__file__).resolve().parents[1]


async def test_a_pinned_snapshot_wedges_the_connection_permanently(store, observer):
    """Characterisation of ONE of the two failure modes, so the regression tests
    above are read for what they are: not a transient race that a retry rides
    out. Once a peer has committed past the snapshot an unreset statement is
    pinned to, EVERY subsequent write on that connection fails until the
    statement is reset — which is why the server had to be restarted.

    Scope. This is the SQLITE_BUSY_SNAPSHOT half, and it is deterministic, which
    is why the extended code can be asserted here. The other half — a plain
    SQLITE_BUSY when the write merely loses the upgrade to a peer holding the
    lock — carries the identical message and is NOT deterministic; see the storm
    test below, which produced both, mixed, on the parent commit. Nothing may
    infer which one a production traceback was.

    Asserted against raw aiosqlite, not `Store`, so it keeps describing SQLite's
    behaviour even after `Store` is fixed — and it reaches for `store.db` on
    purpose, which is the one thing the tree guard bans in `src/`.
    """
    import sqlite3

    # The table must have MORE rows than we fetch, or the statement steps to
    # SQLITE_DONE inside the fetch, resets itself, and pins nothing. That is the
    # measured rule in `db.py`, and it is what makes this test's setup load-
    # bearing rather than incidental.
    for i in range(3):
        await _mk_task(store, f"row{i}")

    conn = store.db
    leaked = await conn.execute("SELECT * FROM tasks")
    await leaked.fetchone()               # unexhausted -> snapshot pinned
    try:
        await _peer_commit(observer)
        for _ in range(3):
            with pytest.raises(sqlite3.OperationalError) as caught:
                await conn.execute(
                    "INSERT INTO task_events(task_id, ts, data) "
                    "VALUES ('probe', 1.0, '{}')")
                await conn.commit()
            assert "database is locked" in str(caught.value)
            # The name the message hides. `busy_timeout` is moot here not
            # because of this code specifically, but because SQLite runs no busy
            # handler for a read-to-write upgrade at all.
            assert caught.value.sqlite_errorname == "SQLITE_BUSY_SNAPSHOT"
    finally:
        await leaked.close()
        await conn.rollback()

    # Releasing the cursor is what recovers it — nothing else does.
    await store.update_task_columns(await _mk_task(store, "after"))


async def test_pool_and_peer_connection_never_produce_a_lock_storm(store, observer):
    """End-to-end shape of the live failure: four workers doing the attempt
    read-modify-write mix on the shared connection while a peer connection
    commits, as the Jira poller does beside a running pool."""
    tasks = [await _mk_task(store, f"w{i}") for i in range(4)]
    errors: list[BaseException] = []
    stop = asyncio.Event()

    async def peer():
        while not stop.is_set():
            await _peer_commit(observer)
            await asyncio.sleep(0)

    async def worker(t):
        for _ in range(25):
            try:
                attempt_id = await store.create_attempt(t.id, 1)
                await store.get_task(t.id)
                await store.list_attempts(t.id)
                await store.update_attempt(attempt_id, status="failed")
                await store.save_events(t.id, [{"kind": "tool_use", "text": "x"}])
            except BaseException as exc:  # noqa: BLE001 - the assertion is "none"
                errors.append(exc)
                return
            await asyncio.sleep(0)

    peering = asyncio.create_task(peer())
    await asyncio.gather(*(worker(t) for t in tasks))
    stop.set()
    await peering

    assert errors == [], f"lock storm returned: {errors[:3]}"
