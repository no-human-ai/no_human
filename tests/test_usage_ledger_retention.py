"""Retention/compaction for the `unattributed_usage` ledger (core/db.py).

The ledger is append-only and, before this change, unbounded — no writer ever
deleted a row and no test covered any form of pruning. `unattributed_usage_totals`
is documented as the whole-cost residual: the number `nh status` prints as the
true total. A plain age-based DELETE would make that number silently shrink, so
`compact_unattributed_usage` rolls aged rows up into one row per (site, model)
instead of deleting them. Every test here reads the number back out of SQLite
through the same methods production reads through — the totals invariant is
the anti-lying gate and must observe the ledger, not recompute independently
of it.
"""

from __future__ import annotations

import pytest_asyncio

from no_human.core.db import Store

SITES = ("site_a", "site_b")
MODELS = ("model_x", "model_y")
OLD_TS = "2020-01-01T00:00:00+00:00"


async def _backdate(store, ids, ts=OLD_TS):
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    await store.db.execute(
        f"UPDATE unattributed_usage SET ts = ? WHERE id IN ({placeholders})",
        (ts, *ids),
    )
    await store.db.commit()


async def _seed(store, *, per_group, sites=SITES, models=MODELS, ts=OLD_TS):
    """Record `per_group` rows for every (site, model) pair, then backdate
    all of them to `ts` in one raw UPDATE — writers only ever set `ts` via
    `_now()`, so backdating is the only way to put a row inside a retention
    window from outside production code."""
    ids = []
    for site in sites:
        for model in models:
            for _ in range(per_group):
                rid = await store.record_unattributed_usage(
                    site=site, model=model, tokens_used=10,
                    cache_read_tokens=2, cache_creation_tokens=3)
                ids.append(rid)
    await _backdate(store, ids, ts)
    return ids


async def test_compaction_preserves_totals_exactly(store):
    await _seed(store, per_group=3)
    before = await store.unattributed_usage_totals()

    collapsed = await store.compact_unattributed_usage(retention_days=1)
    assert collapsed == len(SITES) * len(MODELS) * 3

    after = await store.unattributed_usage_totals()
    assert after == before, (
        "compaction must not change the residual total, including `calls`")


async def test_compaction_bounds_row_count(store):
    ids = []
    for _ in range(25):
        ids.append(await store.record_unattributed_usage(
            site="site_a", model="model_x", tokens_used=1))
    for _ in range(25):
        ids.append(await store.record_unattributed_usage(
            site="site_b", model="model_y", tokens_used=1))
    await _backdate(store, ids)

    collapsed = await store.compact_unattributed_usage(retention_days=1)
    assert collapsed == 50

    cur = await store.db.execute(
        "SELECT site, rolled_up FROM unattributed_usage ORDER BY site")
    rows = [dict(r) for r in await cur.fetchall()]
    assert rows == [
        {"site": "site_a", "rolled_up": 25},
        {"site": "site_b", "rolled_up": 25},
    ]


async def test_rows_inside_window_are_untouched(store):
    rid = await store.record_unattributed_usage(
        site="site_a", model="model_x", tokens_used=5, task_id="t1")
    cur = await store.db.execute(
        "SELECT ts, task_id FROM unattributed_usage WHERE id = ?", (rid,))
    before = dict(await cur.fetchone())

    collapsed = await store.compact_unattributed_usage(retention_days=90)
    assert collapsed == 0

    cur = await store.db.execute(
        "SELECT ts, task_id FROM unattributed_usage WHERE id = ?", (rid,))
    after = dict(await cur.fetchone())
    assert after == before


async def test_retention_disabled_is_a_noop(store):
    await _seed(store, per_group=2)
    cur = await store.db.execute("SELECT COUNT(*) AS c FROM unattributed_usage")
    before_count = (await cur.fetchone())["c"]

    collapsed = await store.compact_unattributed_usage(retention_days=0)
    assert collapsed == 0

    cur = await store.db.execute("SELECT COUNT(*) AS c FROM unattributed_usage")
    after_count = (await cur.fetchone())["c"]
    assert after_count == before_count


async def test_compaction_is_idempotent(store):
    await _seed(store, per_group=4)

    first = await store.compact_unattributed_usage(retention_days=1)
    assert first > 0
    totals_after_first = await store.unattributed_usage_totals()

    second = await store.compact_unattributed_usage(retention_days=1)
    assert second == 0
    totals_after_second = await store.unattributed_usage_totals()
    assert totals_after_second == totals_after_first


async def test_recompaction_preserves_prior_rollup_count(store):
    # Regression for a re-compaction bug: a group swept up on a LATER call can
    # already contain a PRIOR roll-up row (rolled_up > 0) alongside ordinary
    # rows. Grouping with a plain `COUNT(*)` counts that roll-up row as 1,
    # discarding however many original calls it stood for. First collapse 7
    # old rows into one roll-up row with a wide retention window, then age 3
    # more rows into the same (site, model) group and force a second,
    # narrower-window compaction that sweeps the roll-up row back up
    # (its `ts` — the first cutoff — is older than the second, narrower
    # cutoff, so it qualifies again). The group must end with `rolled_up`
    # equal to the TRUE original call count (10), not the row count (4).
    for _ in range(7):
        await store.record_unattributed_usage(
            site="site_a", model="model_x", tokens_used=1)
    cur = await store.db.execute(
        "SELECT id FROM unattributed_usage WHERE site = 'site_a'")
    first_ids = [r["id"] for r in await cur.fetchall()]
    await _backdate(store, first_ids, OLD_TS)

    first = await store.compact_unattributed_usage(retention_days=100)
    assert first == 7

    for _ in range(3):
        await store.record_unattributed_usage(
            site="site_a", model="model_x", tokens_used=1)
    cur = await store.db.execute(
        "SELECT id FROM unattributed_usage WHERE site = 'site_a' "
        "AND rolled_up = 0")
    fresh_ids = [r["id"] for r in await cur.fetchall()]
    assert len(fresh_ids) == 3
    await _backdate(store, fresh_ids, OLD_TS)

    second = await store.compact_unattributed_usage(retention_days=1)
    assert second == 4  # 1 prior roll-up row + 3 fresh rows, by ROW count

    cur = await store.db.execute(
        "SELECT rolled_up FROM unattributed_usage WHERE site = 'site_a'")
    rows = [dict(r) for r in await cur.fetchall()]
    assert rows == [{"rolled_up": 10}], (
        "the group's true call count (7 + 3) must survive re-compaction, "
        "not collapse to the row count swept up in this pass")

    totals = await store.unattributed_usage_totals()
    assert totals["calls"] == 10


async def test_connect_invokes_compaction_and_never_raises(tmp_path, monkeypatch):
    # A wedged connect is worse than an ungroomed ledger: the fail-open
    # contract lives in `compact_unattributed_usage`'s own try/except AND in
    # `connect()`'s own guard around the call — assert the store still works
    # even when the config layer itself is broken.
    import no_human.config as nh_config

    def _raise(*a, **k):
        raise RuntimeError("config exploded")

    monkeypatch.setattr(nh_config, "load_config", _raise)

    s = await Store(tmp_path / "nh.db").connect()
    try:
        rid = await s.record_unattributed_usage(site="x", tokens_used=1)
        assert rid is not None
        totals = await s.unattributed_usage_totals()
        assert totals["calls"] == 1
    finally:
        await s.close()
