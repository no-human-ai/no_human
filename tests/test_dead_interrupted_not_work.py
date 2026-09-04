"""A zero-token interrupted attempt must not consume the lifetime attempt cap.

INCIDENT (2026-08-20): a weekly-quota outage left four concurrent tasks
(123dea00, 021899de, 7553b865, e2d0802d) accumulating attempt rows closed by
`create_attempt`'s own stale-row sweep — every worker process died mid-attempt
without ever closing its row, so the NEXT attempt's sweep tombstoned it as
`'interrupted: superseded by a newer attempt — the prior worker process died
without closing its row'`, at turns <= 1 with ZERO priced tokens. `123dea00`
had 8 of its 9 attempts this exact shape and was FAILED by attempt-cap
exhaustion though no real attempt had ever failed; the three siblings needed
manual `lifetime_attempts` raises to survive.

The fix lives at the single chokepoint both budget gates read
(`Store.lifetime_usage_by_class`): a `status = 'interrupted'` row that burned
NO priced work (`Store._zero_priced_work_sql()`, the same addend columns
`lifetime_usage`/`pricing.weighted_tokens` already use — no second,
independently-typed definition of "did no work") no longer consumes a
lifetime attempt. THE BOUNDARY, load-bearing and re-asserted by these tests:
an interrupted attempt WITH real priced spend still counts, and a `failed`
attempt always counts whatever its token columns say.

Follows `tests/test_infra_not_work.py`'s idiom exactly: a `Store` on
`tmp_path`, the local `_orch(store)` builder (`Orchestrator.__new__`), and
`asyncio_mode = "auto"` (set repo-wide) so no `@pytest.mark.asyncio` marker is
needed anywhere in this file.
"""

from __future__ import annotations


from no_human.blockers import BlockerCategory
from no_human.core.bounds import Bounds
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.pricing import weighted_tokens
from no_human.core.task import Task

# Verbatim from `Store.create_attempt`'s stale-row sweep — the exact string
# every dead row from the 2026-08-20 incident carries.
_SUPERSEDED = (
    "interrupted: superseded by a newer attempt — the prior worker "
    "process died without closing its row"
)


def _orch(store):
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds.from_config({})
    o.config = {}
    o._sink = lambda e: None
    return o


async def _dead(store, task_id, n):
    """Seed one dead `interrupted`, zero-priced attempt row — the exact
    incident shape: a worker process that died without metering anything."""
    aid = await store.create_attempt(task_id, n)
    await store.update_attempt(
        aid, status="interrupted", failure_reason=_SUPERSEDED,
        tokens_used=0, cache_read_tokens=0, cache_creation_tokens=0)
    return aid


async def _real(store, task_id, n, tokens=100_000):
    """Seed one genuine, priced attempt row — a real failure, real spend."""
    aid = await store.create_attempt(task_id, n)
    await store.update_attempt(
        aid, status="failed", tokens_used=tokens,
        cache_read_tokens=0, cache_creation_tokens=0)
    return aid


# --------------------------------------------------------------------------- #
# AC1 — dead attempts are excluded at the lifetime check, reusing the         #
# existing priced-work predicate (no new/third definition).                  #
# --------------------------------------------------------------------------- #


async def test_dead_attempts_are_excluded_at_the_lifetime_check(store):
    """The observed 123dea00 shape: 8 dead + 1 real, default `lifetime_attempts:
    9`. RED before the fix: today all 9 rows are charged and this task is one
    real attempt away from BUDGET_EXHAUSTED though only one attempt ever ran."""
    task = Task.new("dead-shape", repo_path="/tmp/x")
    await store.create_task(task)
    for n in range(1, 9):
        await _dead(store, task.id, n)
    await _real(store, task.id, 9)

    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 1, (
        "8 dead zero-priced interrupted rows must not be charged")

    blocker = await _orch(store)._check_lifetime_budget(task)
    assert blocker is None, (
        "one real attempt under a cap of 9 must never park the task")

    # The rows survive — durable record, never skipped or deleted.
    assert await store.count_attempts(task.id) == 9


def test_the_predicate_is_derived_from_the_priced_column_set():
    """`_zero_priced_work_sql()` must mention every column `_usage_columns()`
    names and no column outside it — so a role newly registered in
    `USAGE_ROLES` widens the predicate at the exact moment it widens the
    caps, instead of silently falling outside this exclusion."""
    predicate = Store._zero_priced_work_sql()
    columns = Store._usage_columns()
    assert columns, "the fixture column set must not be empty"
    for col in columns:
        assert col in predicate, f"{col!r} missing from the zero-work predicate"
    # And nothing outside that set: every COALESCE(...) target in the SQL
    # must itself be one of the wanted columns.
    import re
    mentioned = set(re.findall(r"COALESCE\((\w+),\s*0\)", predicate))
    assert mentioned == set(columns), (
        "the predicate must be built from _usage_columns() exactly, "
        "not a re-typed subset or superset of it")


# --------------------------------------------------------------------------- #
# AC2 — the cap is reached on real attempts only: M, not N + M.               #
# --------------------------------------------------------------------------- #


async def test_cap_is_reached_on_real_attempts_only(store):
    task = Task.new("cap-on-real", repo_path="/tmp/x")
    task.config = {"lifetime_attempts": 3}
    await store.create_task(task)

    for n in range(1, 6):
        await _dead(store, task.id, n)
    await _real(store, task.id, 6)
    await _real(store, task.id, 7)

    assert await _orch(store)._check_lifetime_budget(task) is None, (
        "2 real attempts under a cap of 3 must not park the task, "
        "whatever the dead-row count is")

    # A third REAL attempt — now the cap trips on 3/3 real attempts, not
    # 3 real + 5 dead = 8.
    await _real(store, task.id, 8)
    blocker = await _orch(store)._check_lifetime_budget(task)
    assert blocker is not None
    assert blocker.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "attempts 3/3" in blocker.root_cause_hypothesis, blocker.root_cause_hypothesis


# --------------------------------------------------------------------------- #
# AC3 — guard shapes: THE BOUNDARY holds in both directions.                  #
# --------------------------------------------------------------------------- #


async def test_interrupted_with_real_spend_still_counts(store):
    """THE BOUNDARY: an interrupted row that burned REAL priced work still
    counts — the work happened and was lost, and that spend pressure is
    real. Only the zero-priced interrupted shape is excluded."""
    task = Task.new("interrupted-with-spend", repo_path="/tmp/x")
    task.config = {"lifetime_attempts": 3}
    await store.create_task(task)

    for n in range(1, 4):
        aid = await store.create_attempt(task.id, n)
        await store.update_attempt(
            aid, status="interrupted", failure_reason=_SUPERSEDED,
            tokens_used=50_000, cache_read_tokens=1_000, cache_creation_tokens=0)

    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 3, (
        "an interrupted row with real spend must still be charged")

    blocker = await _orch(store)._check_lifetime_budget(task)
    assert blocker is not None
    assert blocker.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "attempts 3/3" in blocker.root_cause_hypothesis


async def test_failed_attempt_always_counts_even_with_zero_tokens(store):
    """A genuine `failed` attempt that recorded nothing is still a datum — the
    exclusion tests `status = 'interrupted'` specifically, not merely "zero
    priced work" on its own, so a real failure can never be swept aside."""
    task = Task.new("failed-zero-tokens", repo_path="/tmp/x")
    task.config = {"lifetime_attempts": 3}
    await store.create_task(task)

    for n in range(1, 4):
        aid = await store.create_attempt(task.id, n)
        await store.update_attempt(
            aid, status="failed",
            tokens_used=0, cache_read_tokens=0, cache_creation_tokens=0)

    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 3, (
        "a failed attempt must always count, whatever its token columns say")

    blocker = await _orch(store)._check_lifetime_budget(task)
    assert blocker is not None
    assert blocker.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "attempts 3/3" in blocker.root_cause_hypothesis


async def test_in_progress_zero_token_row_still_counts(store):
    """The currently-running attempt is `in_progress`, not `interrupted` (the
    schema default — `migrations/0001_init.sql`) — it must keep consuming its
    slot even though it has metered nothing yet. Only a row a DEAD process
    left behind, tombstoned `interrupted` by the sweep, is excluded."""
    task = Task.new("in-progress", repo_path="/tmp/x")
    await store.create_task(task)
    aid = await store.create_attempt(task.id, 1)

    row = await store._fetchone(
        "SELECT status FROM attempts WHERE id = ?", (aid,))
    assert row["status"] == "in_progress"

    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 1, (
        "the running attempt must still consume a lifetime attempt slot")


# --------------------------------------------------------------------------- #
# AC4 — token-based lifetime caps are untouched by this fix.                  #
# --------------------------------------------------------------------------- #


async def test_weighted_token_cap_math_is_unchanged(store):
    """The fix touches only the ATTEMPT axis. The TOKEN axis
    (cost-weighted, `core.pricing.weighted_tokens`) must produce the exact
    same numbers with or without dead interrupted rows in the mix — they
    always contribute 0 on every column, so they can never move it.

    Pinned against the emitted `lifetime_budget` event (the under-cap path;
    `_check_lifetime_budget` only emits when the task is NOT parked) rather
    than against the parked `Blocker`'s prose, so the assertion is on the
    actual structured numbers (`tokens_weighted`, `tokens_used`) and not a
    formatted string.
    """
    task = Task.new("token-axis", repo_path="/tmp/x")
    await store.create_task(task)

    await _real(store, task.id, 1, tokens=40_000)
    aid2 = await store.create_attempt(task.id, 2)
    await store.update_attempt(
        aid2, status="failed", tokens_used=20_000,
        cache_read_tokens=2_000, cache_creation_tokens=500)

    events = []
    orch = _orch(store)
    orch._sink = events.append
    blocker = await orch._check_lifetime_budget(task)
    assert blocker is None, "well under both default caps — must not park"

    _, by_class, _ = await store.lifetime_usage_by_class(task.id)
    expected_weighted = weighted_tokens(**by_class)
    expected_raw = sum(by_class[n] for n in Store._usage_columns_by_class())

    budget_events = [e for e in events if e["kind"] == "lifetime_budget"]
    assert len(budget_events) == 1
    event = budget_events[0]
    assert event["tokens_weighted"] == expected_weighted
    assert event["tokens_used"] == expected_raw

    # Now bury the same task in 6 dead interrupted rows — comparable to the
    # incident shape — and prove neither number moves.
    for n in range(3, 9):
        await _dead(store, task.id, n)

    events2 = []
    orch2 = _orch(store)
    orch2._sink = events2.append
    blocker2 = await orch2._check_lifetime_budget(task)
    assert blocker2 is None

    budget_events2 = [e for e in events2 if e["kind"] == "lifetime_budget"]
    assert len(budget_events2) == 1
    event2 = budget_events2[0]
    assert event2["tokens_weighted"] == expected_weighted, (
        "dead interrupted rows must not move the cost-weighted token axis")
    assert event2["tokens_used"] == expected_raw, (
        "dead interrupted rows must not move the raw token axis")


async def test_dead_rows_do_not_change_lifetime_usage_totals(store):
    """`store.lifetime_usage()`'s raw total — the number `nh`, the web burn
    meters and `eval/northstar.py` all report — must be identical with and
    without the dead rows present."""
    task = Task.new("totals", repo_path="/tmp/x")
    await store.create_task(task)
    await _real(store, task.id, 1, tokens=50_000)
    await _real(store, task.id, 2, tokens=70_000)
    _, tokens_before = await store.lifetime_usage(task.id)

    await _dead(store, task.id, 3)
    await _dead(store, task.id, 4)
    await _dead(store, task.id, 5)
    _, tokens_after = await store.lifetime_usage(task.id)

    assert tokens_after == tokens_before, (
        "dead zero-priced interrupted rows must not move the raw total")


# --------------------------------------------------------------------------- #
# Chokepoint — the two consumers of lifetime_usage_by_class cannot disagree.  #
# --------------------------------------------------------------------------- #


async def test_ceiling_guard_agrees_with_the_gate(store):
    """`_at_lifetime_ceiling` (the advisory guard) and `_check_lifetime_budget`
    (the gate) both read `store.lifetime_usage_by_class` — on the 8-dead-1-real
    shape, the ceiling guard must be False exactly where the gate returns
    None, because there is only ever one definition of what counts."""
    task = Task.new("chokepoint", repo_path="/tmp/x")
    await store.create_task(task)
    for n in range(1, 9):
        await _dead(store, task.id, n)
    await _real(store, task.id, 9)

    orch = _orch(store)
    gate_result = await orch._check_lifetime_budget(task)
    ceiling_result = await orch._at_lifetime_ceiling(task)

    assert gate_result is None
    assert ceiling_result is False, (
        "the advisory ceiling guard must agree with the gate: "
        "8 dead rows must not tip it over either")
