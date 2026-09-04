"""Incremental event persistence, shared by the scheduler and the CLI --run path."""

from __future__ import annotations

import asyncio


from no_human.core.events import EventPersister


async def test_flushes_on_a_timer_while_the_run_is_live(store):
    async with EventPersister(store, "task-1", interval=0.01) as p:
        p.record({"ts": 1, "kind": "tool_use"})
        p.record({"ts": 2, "kind": "tool_use"})
        await asyncio.sleep(0.05)
        assert len(await store.list_events("task-1")) == 2, "must not wait for the end"
        p.record({"ts": 3, "kind": "result"})

    assert len(await store.list_events("task-1")) == 3


async def test_never_writes_an_event_twice(store):
    """save_events INSERTs. Re-sending the buffer would duplicate every row."""
    async with EventPersister(store, "task-2", interval=0.01) as p:
        for i in range(4):
            p.record({"ts": i, "kind": "tool_use", "tool_name": f"T{i}"})
            await asyncio.sleep(0.02)   # a flush happens between each record

    rows = await store.list_events("task-2")
    assert [r["tool_name"] for r in rows] == ["T0", "T1", "T2", "T3"]


async def test_a_failing_store_never_breaks_the_run(store, caplog):
    class Boom:
        async def save_events(self, *a, **k):
            raise RuntimeError("disk on fire")

    async with EventPersister(Boom(), "task-3", interval=0.01) as p:
        p.record({"ts": 1, "kind": "tool_use"})
        await asyncio.sleep(0.03)
        p.record({"ts": 2, "kind": "tool_use"})
    # Exiting the context must not raise; the batch is dropped, not retried.


async def test_a_failed_batch_is_not_retried_into_unbounded_memory(store):
    calls: list[int] = []

    class Boom:
        async def save_events(self, task_id, events):
            calls.append(len(events))
            raise RuntimeError("nope")

    p = EventPersister(Boom(), "task-4", interval=0.01)
    p.record({"ts": 1})
    await p.flush()
    await p.flush()          # nothing left to retry
    assert calls == [1]


async def test_flush_is_a_noop_when_nothing_is_pending(store):
    p = EventPersister(store, "task-5")
    await p.flush()
    assert await store.list_events("task-5") == []
