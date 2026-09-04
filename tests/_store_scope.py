"""Shared teardown for tests that need a real aiosqlite-backed ``Store``.

``aiosqlite.connect()`` starts a non-daemon ``_connection_worker_thread``
(see ``no_human.core.db.Store.connect``'s own comment on the same mechanic).
That thread blocks on ``tx.get()`` until ``Connection.close()`` enqueues the
stop sentinel. If a test opens a ``Store`` and never closes it, the thread
outlives the test's event loop. Under ``pytest -n 4`` that loop is closed at
the end of the *next* test that happens to run on the same xdist worker, and
when the leaked connection is later garbage-collected, ``Connection.__del__``
-> ``stop()`` schedules a callback on the now-closed loop via
``future.get_loop().call_soon_threadsafe(...)``, which trips
``BaseEventLoop._check_closed`` and raises ``RuntimeError: Event loop is
closed`` — surfacing in whatever unrelated test is running on that worker at
that moment (the four ``ui_evidence`` tests, most often).

``store_scope`` is the single place that owns this close discipline: every
``Store`` it opens is closed, in reverse order, in a ``finally`` — including
when the caller's test body raises.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from no_human.core.db import Store


@asynccontextmanager
async def store_scope(tmp_path: Path):
    """Yield an ``open_store(name="t.db")`` factory; close everything it
    opened, in reverse order, before returning control to the caller —
    always before the caller's event loop can close."""
    opened: list[Store] = []

    async def open_store(name: str = "t.db") -> Store:
        s = await Store(tmp_path / name).connect()
        opened.append(s)
        return s

    try:
        yield open_store
    finally:
        for s in reversed(opened):
            await s.close()
