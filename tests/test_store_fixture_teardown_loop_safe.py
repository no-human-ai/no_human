"""Regression: a leaked ``Store`` must not outlive the loop that owned it.

``tests/_store_scope.py`` documents the mechanism this guards against —
``aiosqlite.connect()``'s non-daemon worker thread outliving a test's event
loop, and a later ``Connection.__del__`` -> ``stop()`` tripping
``RuntimeError: Event loop is closed`` on whatever unrelated test is running
on the same xdist worker at that moment. This file asserts the fix directly,
with ``threading.enumerate()`` deltas rather than sleeps: ``store_scope``
reaps its worker thread before returning control to the caller, a store
opened and torn down under a manually owned event loop leaves no worker
thread able to fire ``call_soon_threadsafe`` on that (now-closed) loop after
a GC pass, and the two repaired modules no longer construct a ``Store``
outside that scope at all.
"""

from __future__ import annotations

import asyncio
import gc
import threading
import warnings
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


def test_store_scope_closes_before_the_owning_loop_so_no_call_soon_threadsafe_escapes(
    tmp_path,
):
    """Reproduces the exact production mechanic, not a proxy for it.

    ``asyncio.Runner`` conveniently calls ``asyncio.set_event_loop(None)`` on
    exit, which makes ``aiosqlite``'s ``Connection.stop()`` swallow
    ``asyncio.get_event_loop()`` failing and skip the poisoned
    ``call_soon_threadsafe`` call entirely — so a Runner-based test cannot
    ever observe the real bug. A bare fixture's ``request.addfinalizer``-style
    teardown does the same thing pytest-asyncio's default loop does: the loop
    object stays the *current* one for the thread even after ``.close()``.
    This test reproduces that precisely: a manually owned loop that is
    ``.close()``-d without being unset, then a GC pass to run
    ``Connection.__del__`` while it is still "current".

    THE ABLATION (pre-fix behaviour): delete the ``await s.close()`` line
    from ``store_scope``'s ``finally`` in ``tests/_store_scope.py`` and
    re-run this test alone. The store's aiosqlite connection is then still
    open when ``scenario()`` returns; ``loop.close()`` runs while the worker
    thread is still blocked on ``tx.get()``; the ``gc.collect()`` below
    triggers ``Connection.__del__`` -> ``stop()``, which enqueues a
    stop-sentinel future bound to the now-closed (but still "current") loop;
    the worker thread wakes up, and
    ``future.get_loop().call_soon_threadsafe(...)`` raises
    ``RuntimeError: Event loop is closed`` *inside the worker thread* —
    unhandled, so it reaches ``threading.excepthook`` and this test's
    ``escaped`` assertion fails with that exact RuntimeError. Measured
    directly: ablated run -> ``escaped`` contains
    ``RuntimeError('Event loop is closed')`` from a
    ``_connection_worker_thread``; fixed run -> ``escaped`` is empty. With
    the fix, ``store_scope`` closes the store *inside* ``scenario()``, before
    ``loop.close()`` ever runs, so the worker thread has already stopped
    cleanly and there is nothing left for the closed loop to poison.
    """
    before = _aiosqlite_threads()
    escaped: list[object] = []
    old_hook = threading.excepthook
    threading.excepthook = escaped.append
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            old_loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        old_loop = None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def scenario():
        async with store_scope(tmp_path) as open_store:
            s = await open_store()
            await s.create_task(Task.new("x", repo_path="/tmp/r"))

    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()

    try:
        gc.collect()
        # Bounded join, never a sleep: if the store above was not fully
        # stopped before `loop.close()`, its worker thread is either still
        # blocked or in the middle of exploding on the closed loop. Give it
        # up to 5s to settle either way before asserting.
        for t in threading.enumerate():
            if t.ident in before:
                continue
            if "_connection_worker_thread" in t.name:
                t.join(5.0)

        leaked = _aiosqlite_threads() - before
        assert not leaked, (
            f"a worker thread survived the loop that owned it: {leaked!r} — "
            "this is exactly the state that later poisons an unrelated "
            "test's loop via Connection.__del__ -> stop() -> "
            "call_soon_threadsafe"
        )
        assert not escaped, (
            "an unclosed store's worker thread fired call_soon_threadsafe "
            f"on its already-closed owning loop: {escaped!r}"
        )
    finally:
        threading.excepthook = old_hook
        asyncio.set_event_loop(old_loop)


def test_the_repaired_modules_open_no_unmanaged_store():
    for name in ("test_slack_intake.py", "test_repro_base_ref_resume.py"):
        source = (REPO_ROOT / "tests" / name).read_text()
        assert "Store(" not in source, (
            f"{name} constructs a Store directly instead of going through "
            "the store_factory fixture / store_scope — that reintroduces the "
            "leaking-thread teardown bug this file guards against"
        )
