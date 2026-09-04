"""Regression: a leaked ``Store`` must not outlive the loop that owned it.

``tests/_store_scope.py`` documents the mechanism this guards against —
``aiosqlite.connect()``'s non-daemon worker thread outliving a test's event
loop, and a later ``Connection.__del__`` -> ``stop()`` tripping
``RuntimeError: Event loop is closed`` on whatever unrelated test is running
on the same xdist worker at that moment. This file asserts the fix directly,
with ``threading.enumerate()`` deltas rather than sleeps: ``store_scope``
reaps its worker thread before returning control to the caller, a store
opened and torn down inside one ``asyncio.Runner()`` loop leaves nothing for
a second loop to be poisoned by, and the two repaired modules no longer
construct a ``Store`` outside that scope at all.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from no_human.core.task import Task

from tests._store_scope import store_scope

REPO_ROOT = Path(__file__).resolve().parents[1]


def _aiosqlite_threads() -> set[int]:
    return {
        t.ident
        for t in threading.enumerate()
        if t.is_alive() and "_connection_worker_thread" in t.name
    }


async def test_store_scope_teardown_reaps_the_aiosqlite_worker_thread(tmp_path):
    before = _aiosqlite_threads()

    async with store_scope(tmp_path) as open_store:
        s = await open_store()
        await s.create_task(Task.new("x", repo_path="/tmp/r"))

        live = _aiosqlite_threads() - before
        assert live, (
            "expected a live _connection_worker_thread while the store is "
            "open — the probe has no teeth if none is ever seen"
        )

    leaked = _aiosqlite_threads() - before
    assert not leaked, (
        f"store_scope teardown left a live aiosqlite worker thread behind: "
        f"{leaked!r}"
    )


def test_a_store_teardown_does_not_outlive_the_loop_that_owned_it(tmp_path):
    before = _aiosqlite_threads()

    async def scenario():
        async with store_scope(tmp_path) as open_store:
            s = await open_store()
            await s.create_task(Task.new("x", repo_path="/tmp/r"))

    with asyncio.Runner() as r1:
        r1.run(scenario())

    leaked = _aiosqlite_threads() - before
    assert not leaked, (
        f"a worker thread survived the loop that owned it: {leaked!r} — "
        "this is exactly the state that later poisons an unrelated test's "
        "loop via Connection.__del__ -> stop() -> call_soon_threadsafe"
    )

    escaped: list[object] = []
    old_hook = threading.excepthook
    threading.excepthook = escaped.append
    try:
        async def trivial() -> int:
            await asyncio.sleep(0)
            return 1

        with asyncio.Runner() as r2:
            assert r2.run(trivial()) == 1
    finally:
        threading.excepthook = old_hook

    assert not escaped, (
        f"an exception escaped a background thread while an unrelated loop "
        f"ran: {escaped!r}"
    )


def test_the_repaired_modules_open_no_unmanaged_store():
    for name in ("test_slack_intake.py", "test_repro_base_ref_resume.py"):
        source = (REPO_ROOT / "tests" / name).read_text()
        assert "Store(" not in source, (
            f"{name} constructs a Store directly instead of going through "
            "the store_factory fixture / store_scope — that reintroduces the "
            "leaking-thread teardown bug this file guards against"
        )
