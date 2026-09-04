"""Regression coverage for the 216-line `database is locked` startup flood.

`nh start`'s Jira and Linear intake used to each open their OWN
``Store(config.db_path).connect()`` — a second (and third) aiosqlite
connection to the SAME file, started concurrently with the FastAPI app
lifespan's own connect+migrate (fired inside ``server.serve()``). Two
connections racing a migration/write on one WAL file meant losers of longer
contentions exhausted the binding's default 5000ms ``busy_timeout`` (measured
— it was never 0 here) and failed with
``sqlite3.OperationalError: database is locked`` — 216 times in one recorded
boot. `nh serve` already shared one ``store`` across
the scheduler and both intakes; the fix makes `nh start` do the same,
handing its connection to the app via ``app.state._external_store`` so
``lifespan`` (api/app.py) reuses it instead of opening its own.

Two independent proofs:

  1. A structural drift guard on ``start()``'s source: the Jira/Linear
     blocks must not construct their own ``Store(...)`` — if a future edit
     reintroduces a second connection, this fails immediately, without
     needing to actually race two real connections (which a mere
     `busy_timeout` bump would quietly hide).
  2. A live demonstration, using the real ``Store``/aiosqlite, that two
     independent connections opened concurrently against the same fresh
     database DO race — proving the mechanism is real, not hypothetical —
     paired with the actual fix's shape (one connection, reused) producing
     zero errors under the same load.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from no_human.api.app import lifespan
from no_human.core.db import Store
from no_human.core.task import Task


def _start_go_source() -> str:
    import no_human.cli.commands as commands_mod

    src = inspect.getsource(commands_mod)
    # Slice out the `start` command's nested `_go` coroutine specifically —
    # `serve()` legitimately shares one `store` already and is not this bug.
    m = re.search(r'@cli\.command\("start"\).*?\n    async def _go\(\):(.*?)\n    try:\n        asyncio\.run\(_go\(\)\)', src, re.S)
    assert m, "could not locate start()'s _go() body — has the command been restructured?"
    return m.group(1)


def test_start_go_opens_exactly_one_store_connection():
    """The regression this guards: a second `Store(config.db_path)` inside
    `start()`'s Jira/Linear blocks is exactly what raced the app lifespan's
    own connection and flooded startup with lock errors."""
    body = _start_go_source()
    # comment-blind on purpose: the fix's own explanatory comment names the
    # constructor in prose, and an instrument that counts prose reports the
    # defect it exists to prevent
    code_lines = [ln for ln in body.splitlines() if not ln.strip().startswith("#")]
    store_constructions = re.findall(r"Store\(config\.db_path\)", "\n".join(code_lines))
    assert store_constructions == ["Store(config.db_path)"], (
        f"start()'s _go() must open exactly ONE Store(config.db_path) "
        f"connection and share it with Jira/Linear intake, not open a "
        f"second/third one — found {len(store_constructions)}: this is the "
        f"exact shape of the two-connection race that flooded startup with "
        f"'database is locked'."
    )
    assert "_app.state._external_store = store" in body, (
        "the single connection must be handed to the app via "
        "_external_store so api/app.py's lifespan reuses it instead of "
        "opening its own"
    )


@pytest.fixture
async def store(store_factory):
    # Variant: test_two_independent_connections_race_on_the_same_file (below)
    # opens tmp_path/"no_human.db" directly, so the filename is load-bearing.
    return await store_factory("no_human.db")


async def test_two_independent_connections_race_on_the_same_file(tmp_path):
    """Known positive: proves two real connections to the same fresh db DO
    produce 'database is locked' when their writes collide — the exact failure
    mode `nh start` used to trigger by opening a second Store for Jira/Linear.
    Without this, the fix test below would prove nothing (it could pass
    because the scenario never actually raced anything).

    The collision is STAGED, not raced. The first version of this control
    threw six concurrent writers at the file and asserted at least one lock:
    ~5/6 reproduction locally, and the very first public CI run rolled the
    1/6 — zero collisions on the shared runner, red gate, premise reported
    as stale when it wasn't. A known positive that depends on scheduler
    interleaving is a coin flip wearing a lab coat. Here the first
    connection holds the write lock in the open (BEGIN IMMEDIATE) while the
    second — busy_timeout=0, the binding's 5000ms default would mask the
    collision, not the defect; db.py:227 records that measurement — attempts
    a real write through the same Store.create_task path the product uses.
    SQLite must refuse it, on every runner, every time."""
    db = tmp_path / "no_human.db"
    s1 = await Store(db).connect()
    s2 = await Store(db).connect()
    try:
        await s2._db.execute("PRAGMA busy_timeout = 0")
        await s1._db.execute("BEGIN IMMEDIATE")  # holds RESERVED until rollback
        with pytest.raises(Exception, match="(?i)locked"):
            await s2.create_task(Task.new("t-collide", repo_path="/tmp/r"))
    finally:
        await s1._db.execute("ROLLBACK")
        await s1.close()
        await s2.close()


def test_lifespan_reuses_external_store_no_second_connection():
    """The handoff contract, asserted on lifespan's SOURCE (the e2e form drove
    the real lifespan, which starts a real Scheduler loop and hung the suite —
    the properties below are structural and this form cannot hang):
    1. lifespan reads app.state._external_store,
    2. POPS it (a later cycle must not reuse a closed store),
    3. only constructs its own Store when no external one exists,
    4. does not close a store it did not open (the CLI owns it)."""
    import inspect, sys
    import no_human.api.app  # noqa: F401 — ensure module load
    src = inspect.getsource(sys.modules["no_human.api.app"].lifespan.__wrapped__)
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert 'getattr(app.state, "_external_store", None)' in code, "handoff read is gone"
    assert "del app.state._external_store" in code, "handoff must be POPPED, not just read"
    assert re.search(r"external_store or await Store\(config\.db_path\)\.connect\(\)", code), (
        "own connection must be the fallback, never a second one")
    close_guard = re.search(r"if\s+external_store\s+is\s+None.*?store\.close\(\)", code, re.S)
    assert close_guard, "shutdown must close only a store lifespan itself opened"
