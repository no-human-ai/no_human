"""Per-task cost must include pre-attempt ``unattributed_usage`` spend already
attributed to that task (bugfix).

``Orchestrator._flush_orphaned_aux_usage`` books plan/utility/supervisor/
distill-tier spend that ran BEFORE any attempt row existed for a task (intake
evaluator, intake grill, assumption pass, decompose proposal, plan) straight
into the ``unattributed_usage`` ledger, tagged with that task's id. Before
this fix, ``TaskOut``/``TaskSummaryOut``/``compute_metrics`` priced only
``attempts`` rows — a task whose whole run was pre-attempt spend showed
``cost_usd: None`` on the board while `nh status` printed the very same
dollars as an anonymous residual line.

``Store.OWNED_LEDGER_SQL`` (``task_id IS NOT NULL AND rolled_up = 0``) is the
fold's ownership test: a row stops being owned the moment
``compact_unattributed_usage`` rolls it up (task_id NULLed), and NEVER before
— a task_id pointing at a deleted task still counts as owned by
``Store.usage_ledger_rows_by_task``'s definition even though no live task
claims it. That is the honest-degradation contract this file pins.

Every test here reads the fold back out through the SAME functions
production uses (`core.cost.ledger_rows_as_attempts`/`attempts_cost`,
`api.models.TaskOut`/`TaskSummaryOut`, `core.metrics.compute_metrics`,
`Store.unattributed_usage_totals(owned=...)`) — never a hand-rolled
recomputation that could silently drift from what the board renders.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.api.app import app
from no_human.api.models import TaskOut, TaskSummaryOut
from no_human.core.cost import attempt_cost, attempts_cost, ledger_rows_as_attempts
from no_human.core.db import Store
from no_human.core.metrics import compute_metrics
from no_human.core.task import Task, TaskStatus

# `client` reaches `config.load_env_var`, which reads the operator's real
# `~/.no_human/.env` before the process env — see tests/conftest.py's
# `isolated_env_file` docstring. Requested by NAME everywhere in this file.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


async def _make_task(store: Store, title: str = "t") -> Task:
    t = Task.new(title, repo_path="/tmp/repo")
    await store.create_task(t)
    return t


# --------------------------------------------------------------------------- #
# AC1 — the owned ledger fold raises a task's displayed cost                  #
# --------------------------------------------------------------------------- #

async def test_owned_ledger_row_raises_the_task_cost(store):
    t = await _make_task(store)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, tokens_used=100_000, models={"coder": "claude-sonnet-5"})
    await store.record_unattributed_usage(
        site="orphaned_plan_usage", model="claude-sonnet-5",
        tokens_used=20_000, task_id=t.id)

    attempts = await store.list_attempts(t.id)
    ledger = await store.task_usage_ledger_rows(t.id)
    assert ledger, "the owned row must be visible through task_usage_ledger_rows"

    out = TaskOut.from_task(t, attempts, ledger=ledger)

    attempt_dollars, _ = attempt_cost(attempts[0])
    ledger_dollars, _ = attempts_cost(ledger_rows_as_attempts(ledger))
    assert out.cost_usd == pytest.approx(attempt_dollars + ledger_dollars)
    assert ledger_dollars > 0, "the row must actually price to something nonzero"

    # The ledger's planner-tier tokens must show up in total_aux_tokens (the
    # aggregate the board renders beside cost_usd) — the attempt alone spent
    # nothing on any aux tier, so before the fix this stayed None.
    assert out.total_aux_tokens == 20_000


async def test_plan_gate_task_with_no_attempts_reports_its_planner_spend(store):
    """A task killed/blocked before its first attempt (plan-gate rejection,
    decompose failure) can still hold real ledger spend — `cost_usd` must be
    a number, not `None`, while `attempt_count` stays 0 (the ledger must
    never leak into an attempt-shaped field)."""
    t = await _make_task(store)
    await store.record_unattributed_usage(
        site="orphaned_plan_usage", model="claude-sonnet-5",
        tokens_used=5_000, task_id=t.id)

    ledger = await store.task_usage_ledger_rows(t.id)
    out = TaskOut.from_task(t, [], ledger=ledger)

    assert out.cost_usd is not None
    assert out.cost_usd > 0
    assert out.attempt_count == 0
    assert out.attempts == []


# --------------------------------------------------------------------------- #
# AC1 — board card, detail endpoint and /api/metrics agree                    #
# --------------------------------------------------------------------------- #

async def test_board_detail_and_metrics_agree(client, store):
    t1 = await _make_task(store, "a")
    a1 = await store.create_attempt(t1.id, 1)
    await store.update_attempt(
        a1, tokens_used=200_000, models={"coder": "claude-sonnet-5"})
    await store.record_unattributed_usage(
        site="orphaned_plan_usage", model="claude-sonnet-5",
        tokens_used=30_000, task_id=t1.id)

    t2 = await _make_task(store, "b")
    await store.record_unattributed_usage(
        site="cli.task_add.grill", model="claude-sonnet-5",
        tokens_used=15_000, task_id=None)

    r_list = await client.get("/api/tasks")
    r_detail = await client.get(f"/api/tasks/{t1.id}")
    r_metrics = await client.get("/api/metrics")
    assert r_list.status_code == r_detail.status_code == r_metrics.status_code == 200

    card = next(c for c in r_list.json() if c["id"] == t1.id)
    detail = r_detail.json()
    metrics = r_metrics.json()

    assert card["cost_usd"] == pytest.approx(detail["cost_usd"])
    assert card["cost_usd"] > 0

    ownerless = await store.unattributed_usage_totals(owned=False)
    ownerless_dollars, _ = attempts_cost(ledger_rows_as_attempts([{
        "site": "cli.task_add.grill", "model": "claude-sonnet-5",
        "tokens_used": ownerless["tokens_used"],
        "cache_read_tokens": ownerless["cache_read_tokens"],
        "cache_creation_tokens": ownerless["cache_creation_tokens"],
    }]))
    card_total = sum(c["cost_usd"] or 0 for c in r_list.json())
    assert metrics["cost_usd_total"] == pytest.approx(card_total + ownerless_dollars)


# --------------------------------------------------------------------------- #
# AC3 — no double counting                                                    #
# --------------------------------------------------------------------------- #

async def test_attempt_and_ledger_sum_exactly_once(store):
    t = await _make_task(store)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, tokens_used=100_000, models={"coder": "claude-sonnet-5"})
    await store.record_unattributed_usage(
        site="orphaned_plan_usage", model="claude-sonnet-5",
        tokens_used=25_000, task_id=t.id)

    attempts = await store.list_attempts(t.id)
    ledger = await store.task_usage_ledger_rows(t.id)

    # (a) exact equality against the hand-computed sum.
    expected, _ = attempts_cost(list(attempts) + ledger_rows_as_attempts(ledger))
    out = TaskOut.from_task(t, attempts, ledger=ledger)
    assert out.cost_usd == pytest.approx(expected)

    # (b) a future fold at TWO layers (e.g. someone re-adding the ledger into
    # `attempts` upstream AND passing `ledger=` here) must be caught, not
    # silently pass as "well, cost went up, that's expected": doubling the
    # ledger input makes the reported cost strictly bigger than the correct
    # single fold above.
    doubled = TaskOut.from_task(t, attempts, ledger=ledger + ledger)
    assert doubled.cost_usd > out.cost_usd

    # (c) disjointness PROVEN at the DB level, not assumed: the attempt row's
    # own plan-tier columns must be zero — the flushed tokens landed in the
    # ledger, never also written onto the attempt row they were flushed from.
    raw = attempts[0]
    assert (raw.get("plan_tokens_used") or 0) == 0
    assert (raw.get("plan_cache_read_tokens") or 0) == 0
    assert (raw.get("plan_cache_creation_tokens") or 0) == 0


# --------------------------------------------------------------------------- #
# AC2 — ownerless rows never attach to a task                                 #
# --------------------------------------------------------------------------- #

async def test_null_task_id_row_attaches_to_no_task(store):
    t = await _make_task(store)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, tokens_used=50_000, models={"coder": "claude-sonnet-5"})
    await store.record_unattributed_usage(
        site="cli.task_add.grill", model="claude-sonnet-5",
        tokens_used=9_000, task_id=None)

    grouped = await store.usage_ledger_rows_by_task()
    assert t.id not in grouped
    assert None not in grouped

    ledger = await store.task_usage_ledger_rows(t.id)
    assert ledger == []

    attempts = await store.list_attempts(t.id)
    out = TaskOut.from_task(t, attempts, ledger=ledger)
    attempt_dollars, _ = attempt_cost(attempts[0])
    assert out.cost_usd == pytest.approx(attempt_dollars)

    ownerless = await store.unattributed_usage_totals(owned=False)
    assert ownerless["tokens_used"] == 9_000
    owned = await store.unattributed_usage_totals(owned=True)
    assert owned["tokens_used"] == 0

    m = await compute_metrics(store)
    assert m["ledger_ownerless_tokens"] == 9_000
    assert m["ledger_owned_tokens"] == 0


async def test_rolled_up_row_with_a_task_id_is_still_ownerless(store):
    """`OWNED_LEDGER_SQL` requires BOTH `task_id IS NOT NULL` AND
    `rolled_up = 0` — a row can carry a task_id yet still be excluded once
    `rolled_up` is set. Raw INSERT/UPDATE, because `compact_unattributed_usage`
    is the only production writer of `rolled_up` and it always clears
    `task_id` at the same time (see the compacted-rollup test below); this
    pins the clause itself rather than the path that normally sets it."""
    t = await _make_task(store)
    row_id = await store.record_unattributed_usage(
        site="orphaned_plan_usage", model="claude-sonnet-5",
        tokens_used=12_000, task_id=t.id)
    await store.db.execute(
        "UPDATE unattributed_usage SET rolled_up = 3 WHERE id = ?", (row_id,))
    await store.db.commit()

    grouped = await store.usage_ledger_rows_by_task()
    assert t.id not in grouped
    assert await store.task_usage_ledger_rows(t.id) == []

    owned = await store.unattributed_usage_totals(owned=True)
    assert owned["tokens_used"] == 0
    ownerless = await store.unattributed_usage_totals(owned=False)
    assert ownerless["tokens_used"] == 12_000


# --------------------------------------------------------------------------- #
# AC4 — compaction degrades without vanishing                                 #
# --------------------------------------------------------------------------- #

async def test_compacted_rollup_degrades_without_vanishing(store):
    """Seed, backdate and compact inside ONE Store connection — `connect()`
    itself runs a best-effort `compact_unattributed_usage()` (db.py), so a
    fresh connection between the backdate and the explicit compact call
    below would let that implicit pass collapse the rows first."""
    t = await _make_task(store)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, tokens_used=40_000, models={"coder": "claude-sonnet-5"})
    row_id = await store.record_unattributed_usage(
        site="orphaned_plan_usage", model="claude-sonnet-5",
        tokens_used=8_000, task_id=t.id)
    await store.db.execute(
        "UPDATE unattributed_usage SET ts = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", row_id))
    await store.db.commit()

    whole_before = await store.unattributed_usage_totals()
    metrics_before = await compute_metrics(store)

    attempts = await store.list_attempts(t.id)
    ledger_before = await store.task_usage_ledger_rows(t.id)
    task_before = TaskOut.from_task(t, attempts, ledger=ledger_before)
    assert ledger_before  # owned before compaction

    collapsed = await store.compact_unattributed_usage(retention_days=1)
    assert collapsed == 1

    # The roll-up REPLACES the source row under a new id (one row per
    # (site, model) group) — `row_id` itself no longer exists after
    # compaction, so look the survivor up by the group key instead.
    row = await store._fetchone(
        "SELECT task_id, rolled_up FROM unattributed_usage "
        "WHERE site = ? AND model = ?",
        ("orphaned_plan_usage", "claude-sonnet-5"))
    assert row is not None
    assert row["task_id"] is None
    assert row["rolled_up"] == 1

    ledger_after = await store.task_usage_ledger_rows(t.id)
    assert ledger_after == []

    attempts_after = await store.list_attempts(t.id)
    task_after = TaskOut.from_task(t, attempts_after, ledger=ledger_after)
    attempt_dollars, _ = attempt_cost(attempts_after[0])
    assert task_after.cost_usd == pytest.approx(attempt_dollars)
    assert task_after.cost_usd < task_before.cost_usd

    owned_after = await store.unattributed_usage_totals(owned=True)
    assert owned_after["tokens_used"] == 0
    ownerless_after = await store.unattributed_usage_totals(owned=False)
    assert ownerless_after["tokens_used"] == 8_000

    whole_after = await store.unattributed_usage_totals()
    assert whole_after == whole_before, (
        "compaction must not change the whole-ledger total OR call count — "
        "spend moves buckets, it does not disappear")

    metrics_after = await compute_metrics(store)
    assert metrics_after["cost_usd_total"] == pytest.approx(
        metrics_before["cost_usd_total"]), (
        "the ledger's whole-install total must be unchanged by compaction — "
        "only which task can claim which slice of it changes")


# --------------------------------------------------------------------------- #
# Other honest-degradation cases                                              #
# --------------------------------------------------------------------------- #

async def test_ledger_row_for_a_deleted_task_stays_in_the_whole_total(store):
    """A `task_id` that never resolves to a live task (the task was deleted,
    or the id was simply never a real one) must still count in the
    whole-ledger total — no per-task figure claims it, but the money is not
    lost from the install-wide number."""
    ghost_id = "deleted-task-does-not-exist"
    await store.record_unattributed_usage(
        site="orphaned_plan_usage", model="claude-sonnet-5",
        tokens_used=6_000, task_id=ghost_id)

    assert await store.get_task(ghost_id) is None

    grouped = await store.usage_ledger_rows_by_task()
    assert ghost_id in grouped, (
        "usage_ledger_rows_by_task groups by task_id regardless of whether "
        "that task still exists — the caller decides whether to look it up")

    whole = await store.unattributed_usage_totals()
    assert whole["tokens_used"] == 6_000
    m = await compute_metrics(store)
    assert m["cost_usd_total"] > 0


def test_ledger_rows_price_at_their_own_model_and_degrade():
    """`ledger_rows_as_attempts` must never raise on a bad/missing model, and
    must reuse the SAME shared-model/mixed rule `attempt_cost` applies to
    real attempts."""
    rows = [
        {"site": "orphaned_plan_usage", "model": None, "tokens_used": 1_000},
        {"site": "orphaned_utility_usage", "model": "not-a-real-model-id",
         "tokens_used": 1_000},
    ]
    priced = ledger_rows_as_attempts(rows)
    total, label = attempts_cost(priced)
    assert total is not None
    assert label is not None  # never raises, always resolves to a label

    agree = ledger_rows_as_attempts([
        {"site": "orphaned_plan_usage", "model": "claude-sonnet-5",
         "tokens_used": 1_000},
        {"site": "orphaned_utility_usage", "model": "claude-sonnet-5",
         "tokens_used": 1_000},
    ])
    _, agree_label = attempts_cost(agree)
    assert agree_label == "claude-sonnet-5"

    disagree = ledger_rows_as_attempts([
        {"site": "orphaned_plan_usage", "model": "claude-sonnet-5",
         "tokens_used": 1_000},
        {"site": "orphaned_utility_usage", "model": "gpt-5.3-codex",
         "tokens_used": 1_000},
    ])
    _, disagree_label = attempts_cost(disagree)
    assert disagree_label == "mixed"

    # A ledger model differing from the attempt's OWN model must also make
    # the combined `cost_model` read "mixed" — a task's label must not hide
    # that its ledger spend ran on a different model than its coder attempt.
    attempt_row = {"tokens_used": 1_000, "models": {"coder": "claude-sonnet-5"}}
    combined = [attempt_row] + ledger_rows_as_attempts([
        {"site": "orphaned_plan_usage", "model": "gpt-5.3-codex",
         "tokens_used": 1_000},
    ])
    _, combined_label = attempts_cost(combined)
    assert combined_label == "mixed"


# --------------------------------------------------------------------------- #
# AC5 — `nh status`'s whole total is the sum of both clauses                  #
# --------------------------------------------------------------------------- #

def test_status_whole_total_is_the_sum_of_both_clauses(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from no_human.cli.commands import cli
    from tests.test_cli_commands import _make_runner, _seed_task, _seed_unattributed

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    _seed_unattributed(db, site="orphaned_plan_usage", tokens_used=3_000,
                        task_id=task_id)
    _seed_unattributed(db, site="cli.task_add.grill", tokens_used=1_500,
                        task_id=None)

    # A rolled-up row (raw INSERT/UPDATE, see test_rolled_up_row_with_a_task_id
    # _is_still_ownerless above) still counts as its ORIGINAL call count.
    async def _seed_rollup():
        async with Store(db) as s:
            row_id = await s.record_unattributed_usage(
                site="orphaned_utility_usage", tokens_used=400, task_id=task_id)
            await s.db.execute(
                "UPDATE unattributed_usage SET task_id = NULL, rolled_up = 4 "
                "WHERE id = ?", (row_id,))
            await s.db.commit()
    asyncio.run(_seed_rollup())

    runner = _make_runner(db, monkeypatch)
    payload = runner.invoke(cli, ["status", "--json"]).output
    import json
    out = json.loads(payload)
    resid = out["unattributed_usage"]
    assert set(resid) == {
        "calls", "tokens_used", "cache_read_tokens", "cache_creation_tokens",
        "total"}

    async def _split():
        async with Store(db) as s:
            owned = await s.unattributed_usage_totals(owned=True)
            ownerless = await s.unattributed_usage_totals(owned=False)
            return owned, ownerless
    owned, ownerless = asyncio.run(_split())

    assert owned["total"] + ownerless["total"] == resid["total"]
    assert owned["calls"] + ownerless["calls"] == resid["calls"]
    # 3 real calls (1 real owned row + 1 real ownerless row + 1 rolled-up row
    # standing for 4 originals) = 6 calls total, never shrunk by the roll-up.
    assert resid["calls"] == 1 + 1 + 4

    human = runner.invoke(cli, ["status"]).output
    assert "already included in those tasks' cost" in human
    assert "no task owns it" in human
