"""The board's "last 24h" banner must price ATTEMPT activity, not task
touches.

Repro: the old implementation filtered `tasks` by `updated_at` and summed
each survivor's LIFETIME `cost_usd`. Closing or cancelling an old task bumps
`updated_at` with no new spend, so its whole historical cost swept into
"last 24h" — measured ~3.5x inflation on a live board (a stale $18.68 task
closed overnight alone accounted for most of the gap). `window_spend`
(core/metrics.py) prices only attempts whose OWN `started_at`/`completed_at`
falls in the window; `tasks` is never queried by it at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from no_human.core.cost import attempt_cost
from no_human.core.db import Store
from no_human.core.metrics import compute_metrics, window_spend
from no_human.core.task import Task, TaskStatus

# A fixed reference instant so every timestamp in this file is deterministic —
# `window_spend` accepts an injected `now` (ISO string) for exactly this.
NOW = datetime(2026, 7, 17, 6, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


def sql_ts(hours_ago: float) -> str:
    """SQLite's `datetime('now')` default spelling: space-separated, no offset
    — the format `attempts.started_at` actually gets written in."""
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def iso_ts(hours_ago: float) -> str:
    """Python `db._now()`'s spelling: ISO-T with a numeric offset — the format
    `attempts.completed_at` actually gets written in."""
    return (NOW - timedelta(hours=hours_ago)).isoformat()


async def _attempt(store, task_id, number, *, started_ago, completed_ago=None,
                    status="succeeded", tokens=1000, cache_read=9000):
    aid = await store.create_attempt(task_id, attempt_number=number)
    fields = {
        "started_at": sql_ts(started_ago),
        "tokens_used": tokens,
        "cache_read_tokens": cache_read,
        "models": {"coder": "claude-sonnet-5"},
        "status": status,
    }
    if completed_ago is not None:
        fields["completed_at"] = iso_ts(completed_ago)
    await store.update_attempt(aid, **fields)
    return aid


async def test_a_row_closed_in_the_window_with_no_new_attempt_adds_zero(store):
    """Repro #1 — MUST FAIL on the unfixed (updated_at-filtered) code."""
    t = Task.new("stale", repo_path="/r")
    await store.create_task(t)
    await _attempt(store, t.id, 1, started_ago=72, completed_ago=71,
                    tokens=50_000, cache_read=200_000)
    # The task row is "touched" long after its attempt finished — closed
    # right now, with no new spend. `window_spend` never queries `tasks`.
    await store.set_status(t, TaskStatus.FAILED, validate=False)

    w = await window_spend(store, hours=24, now=NOW_ISO)
    assert w["cost_usd"] is None
    assert w["tokens"] == 0
    assert w["attempts"] == 0

    # Positive control: the money is real — just not THIS window's. A test
    # that only checked `w["cost_usd"] is None` could pass by accident on a
    # query that always returns nothing.
    m = await compute_metrics(store)
    assert m["cost_usd_total"] > 0


async def test_an_attempt_started_in_the_window_is_counted(store):
    """Repro #2 — an attempt whose own activity is recent is priced, and only
    that attempt (not summed with an old, out-of-window one)."""
    t = Task.new("mixed", repo_path="/r")
    await store.create_task(t)
    await _attempt(store, t.id, 1, started_ago=72, completed_ago=71,
                    tokens=50_000, cache_read=200_000)
    fresh_id = await _attempt(store, t.id, 2, started_ago=2, completed_ago=1,
                               tokens=300, cache_read=1500)

    w = await window_spend(store, hours=24, now=NOW_ISO)

    expected_dollars, expected_label = attempt_cost({
        "models": '{"coder": "claude-sonnet-5"}',
        "tokens_used": 300, "cache_read_tokens": 1500,
        "cache_creation_tokens": 0, "output_tokens": None,
    })
    assert w["cost_usd"] == pytest.approx(expected_dollars)
    assert w["cost_model"] == expected_label
    assert w["tokens"] == 300 + 1500
    assert w["attempts"] == 1
    assert fresh_id  # the id created above is the one and only counted row


async def test_a_long_attempt_that_ended_in_the_window_counts_and_a_finished_one_does_not(store):
    t = Task.new("long", repo_path="/r")
    await store.create_task(t)
    # A: started well before the window, finished inside it — counts.
    await _attempt(store, t.id, 1, started_ago=30, completed_ago=1,
                    tokens=400, cache_read=0)
    # B: started AND finished before the window — excluded.
    await _attempt(store, t.id, 2, started_ago=30, completed_ago=29,
                    tokens=90_000, cache_read=0)

    w = await window_spend(store, hours=24, now=NOW_ISO)
    assert w["tokens"] == 400
    assert w["attempts"] == 1


async def test_an_open_in_progress_attempt_always_counts(store):
    t = Task.new("open", repo_path="/r")
    await store.create_task(t)
    # Started well outside the window, never completed, still in_progress —
    # it is burning right now and must count regardless of when it began.
    await _attempt(store, t.id, 1, started_ago=40, completed_ago=None,
                    status="in_progress", tokens=777, cache_read=0)

    w = await window_spend(store, hours=24, now=NOW_ISO)
    assert w["tokens"] == 777
    assert w["attempts"] == 1


async def test_space_separated_and_iso_t_timestamps_compare_alike(store):
    """`started_at` (SQLite `datetime('now')` default) and `completed_at`
    (Python ISO-T) are different spellings of the same clock; both must
    compare correctly against the cutoff via `julianday()`, not a string
    `>=` (which would mis-order the two formats)."""
    t = Task.new("spellings", repo_path="/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, attempt_number=1)
    await store.update_attempt(
        a, started_at=sql_ts(2), completed_at=iso_ts(1),
        tokens_used=10, cache_read_tokens=0, status="succeeded",
        models={"coder": "claude-sonnet-5"},
    )
    b = await store.create_attempt(t.id, attempt_number=2)
    await store.update_attempt(
        b, started_at=sql_ts(2), completed_at=None,
        tokens_used=20, cache_read_tokens=0, status="in_progress",
        models={"coder": "claude-sonnet-5"},
    )

    w = await window_spend(store, hours=24, now=NOW_ISO)
    assert w["tokens"] == 30
    assert w["attempts"] == 2


async def test_api_metrics_window_serves_the_attempt_attributed_figure(store, tmp_path):
    from httpx import ASGITransport, AsyncClient

    from no_human.api.app import app
    from no_human.config import load_config

    t = Task.new("api", repo_path="/r")
    await store.create_task(t)
    await _attempt(store, t.id, 1, started_ago=1, completed_ago=None,
                    status="in_progress", tokens=42, cache_read=0)

    app.state.store = store
    app.state.config = load_config(tmp_path / "c.yaml")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://localhost") as c:
        body = (await c.get("/api/metrics/window")).json()
        bad = await c.get("/api/metrics/window", params={"hours": 0})

    assert body["tokens"] == 42
    assert body["attempts"] == 1
    assert body["cost_usd"] is not None
    assert bad.status_code == 400


async def test_api_metrics_window_empty_db_is_zeros_not_a_crash(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from no_human.api.app import app
    from no_human.config import load_config

    empty = await Store(tmp_path / "empty.db").connect()
    try:
        app.state.store = empty
        app.state.config = load_config(tmp_path / "c2.yaml")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://localhost") as c:
            body = (await c.get("/api/metrics/window")).json()
    finally:
        await empty.close()

    assert body["cost_usd"] is None
    assert body["cost_model"] is None
    assert body["tokens"] == 0
    assert body["attempts"] == 0
