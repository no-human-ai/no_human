"""Finding 9 (independent review of task 22c4ddf6 finding #3): this bugfix
added two new event kinds, `pr_base_remeasured` and `pr_base_undetermined`
(`blockers.wake._check_base_stale`), but `doctor.py`'s `MECHANISMS` list and
`migrations/0006_events_fts.sql`'s FTS trigger both hardcode which kinds they
know about — a new kind wired into neither is invisible to `nh doctor` and
unsearchable via `/api/search`, exactly the trap migration 0009's own comment
describes for `ci_gate_fail`. "Out of scope in PLAN.md" does not excuse
leaving new event kinds unwired, per the review's finding text.

This file pins two things: `migrations/0019_fts_pr_base_event_kinds.sql`
actually makes both kinds searchable (through the real HTTP surface, not by
reading the trigger's SQL text), and `doctor.py`'s `pr_watch_ladder`
mechanism actually counts both kinds (through `run_diagnostics`'s real
counting query, not by reading `MECHANISMS`'s source).
"""
from __future__ import annotations

import pytest_asyncio

from no_human.core.task import Task


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from httpx import ASGITransport, AsyncClient
    from no_human.api.app import app
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


_TS = [0]


async def _seed_event(store, task, kind, text):
    _TS[0] += 1
    await store.save_events(task.id, [{"kind": kind, "text": text, "ts": _TS[0]}])


async def test_pr_base_remeasured_is_fts_searchable(client, store):
    t = Task.new("stale-base task", repo_path="/tmp/r")
    await store.create_task(t)
    await _seed_event(
        store, t, "pr_base_remeasured",
        "PR base moved: trunk advanced deadbeef -> cafefeed while still "
        "MERGEABLE unique-token-remeasured")

    r = await client.get("/api/search", params={"q": "unique-token-remeasured"})
    assert r.status_code == 200
    hits = r.json()
    assert len(hits) >= 1, hits
    assert hits[0]["kind"] == "pr_base_remeasured"


async def test_pr_base_undetermined_is_fts_searchable(client, store):
    t = Task.new("stale-base task 2", repo_path="/tmp/r")
    await store.create_task(t)
    await _seed_event(
        store, t, "pr_base_undetermined",
        "could not measure PR base freshness unique-token-undetermined")

    r = await client.get("/api/search", params={"q": "unique-token-undetermined"})
    assert r.status_code == 200
    hits = r.json()
    assert len(hits) >= 1, hits
    assert hits[0]["kind"] == "pr_base_undetermined"


async def test_doctor_pr_watch_ladder_counts_the_two_new_kinds(store):
    from no_human import doctor as doctor_mod

    t = Task.new("stale-base task 3", repo_path="/tmp/r")
    await store.create_task(t)
    await _seed_event(store, t, "pr_base_remeasured", "remeasured once")
    await _seed_event(store, t, "pr_base_undetermined", "undetermined once")

    entry = next(e for e in doctor_mod.MECHANISMS if e[0] == "pr_watch_ladder")
    assert "pr_base_remeasured" in entry[1], entry
    assert "pr_base_undetermined" in entry[1], entry

    # Go through the real counting path (`diagnose`'s `_kind_stats` +
    # `MECHANISMS` loop), not a hand-rolled query — this is what actually
    # governs whether `nh doctor` sees these two kinds as live evidence.
    diagnosis = await doctor_mod.diagnose(store, config=None)
    ladder = next(m for m in diagnosis.mechanisms if m["name"] == "pr_watch_ladder")
    assert ladder["count"] == 2, ladder
