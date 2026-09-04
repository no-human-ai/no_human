"""Lifetime task budget: the 21M-token / attempt-17 lesson.

`max_attempts` bounds ONE loop; every resume starts a fresh one. Task 84251cb2
reached attempt 17 and 21.2M cache-read tokens without any cap firing. The
lifetime budget stops that honestly: a BUDGET_EXHAUSTED blocker whose option
raises the budget for that one task — a human decision, never a retry's.
"""

from __future__ import annotations

import inspect

import pytest

from no_human.blockers import BlockerCategory, apply_action, route_for
from no_human.core.bounds import Bounds
from no_human.core.db import Store
from no_human.core.pricing import BUDGET_UNIT_KEY, WEIGHTED_UNIT, weighted_tokens
from no_human.core.task import Task, TaskStatus


def _orch(store, cfg_extra=None, *, config=None):
    """A minimal orchestrator: only .store, .bounds, .config and .emit are
    exercised.

    ``config`` is the app config the blocker builder reads for
    ``budget.exhaustion_terminal``; ``{}`` means "the default", which is ON.
    Pass ``ASK_THE_HUMAN`` for the pre-2026-08-09 escalate-and-ask behaviour.
    """
    from no_human.core.bounds import Bounds
    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds.from_config(cfg_extra or {})
    o.config = config if config is not None else {}
    o._sink = lambda e: None
    return o


#: The off-switch: `budget.exhaustion_terminal: false` restores the old
#: ESCALATED-with-a-question behaviour for an operator who wants to be asked.
ASK_THE_HUMAN = {"budget": {"exhaustion_terminal": False}}


async def _spend(store, task_id, attempts, tokens_each):
    for n in range(1, attempts + 1):
        aid = await store.create_attempt(task_id, n)
        await store.update_attempt(aid, tokens_used=tokens_each // 2,
                                   cache_read_tokens=tokens_each - tokens_each // 2)


async def test_under_budget_returns_none_and_emits_the_running_total(store):
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=1_000_000)

    events = []
    o = _orch(store)
    o._sink = events.append
    assert await o._check_lifetime_budget(t) is None
    ev = next(e for e in events if e["kind"] == "lifetime_budget")
    assert ev["attempts_used"] == 2
    assert ev["tokens_used"] == 2_000_000


async def test_attempt_17_would_have_parked_long_before(store):
    """The real run: 17 attempts. The default cap (9) fires at attempt 10."""
    t = Task.new("ci_gate", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "attempts 9/9" in b.root_cause_hypothesis
    # Routed to a human, never auto-retried.
    r = route_for(b.category)
    assert r.target_status is TaskStatus.ESCALATED and not r.auto_retry


async def test_token_cap_fires_even_with_few_attempts(store):
    """One monster attempt (the 3.4M-cache-read shape) must count."""
    t = Task.new("burn", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=13_000_000)  # 26M > 8M cap

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert "tokens" in b.root_cause_hypothesis


async def test_default_token_cap_parks_a_9M_burn(store):
    """The 8M-raw default (measured 2026-07-13): the largest PR-producing task in
    the corpus burned 6.15M; everything else that succeeded was ≤1.71M. Only the
    parked CI_GATE/metrics-core runaways (20.8M, 61.5M) sat above 8M. A ~9M raw task
    must park under the DEFAULT — it would NOT have parked at the old 25M cap.

    The cap is COST-WEIGHTED since 2026-07-31, and RAISED to 4M on 2026-08-03
    from the honest-ledger sweep (the 1.6M conversion was calibrated on the
    pre-fix ledger whose subagent spend was under-counted; derivation on
    core.bounds.Bounds).
    This test's 9M-raw burn is 4.95M weighted and must STILL park under the
    raised default — it is the smallest runaway class the cap exists for, and
    a raise that spares it has gone too far."""
    from no_human.core.bounds import Bounds
    assert Bounds().lifetime_tokens == 4_000_000

    t = Task.new("nine-million", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=4_500_000)  # 9M > 8M, < old 25M

    b = await _orch(store)._check_lifetime_budget(t)  # default bounds, no override
    assert b is not None and "tokens" in b.root_cause_hypothesis


# --------------------------------------------------------------------------- #
# The token-exclusion fix: infra/mechanical/dead-interrupted tokens must be   #
# gated by the SAME predicate that already spares their attempt — ae2a535c    #
# killed itself at 17.3M/17M this way (see this file's module docstring's    #
# sibling ticket). PR #605's AC1 is the discriminating case (a).             #
# --------------------------------------------------------------------------- #


async def _infra_heavy_task(store):
    """3 infra-classified attempts totalling 7.9M weighted (3.0M + 3.0M +
    1.9M) + 1 genuine attempt at 0.5M weighted. Every figure uses ONLY fresh
    `tokens_used` (weight 1.0 — `core.pricing.FRESH_WEIGHT`), so weighted ==
    raw and the numbers in the ticket are exact, not approximate. The cap is
    explicitly stamped `budget_unit: weighted` so `_stored_token_cap`'s
    cutover guard reads the 8,000,000 verbatim instead of treating it as a
    pre-2026-07-31 raw override and converting it down."""
    t = Task.new("infra-heavy", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 8_000_000, BUDGET_UNIT_KEY: WEIGHTED_UNIT}
    await store.create_task(t)
    for n, tokens in enumerate((3_000_000, 3_000_000, 1_900_000), start=1):
        aid = await store.create_attempt(t.id, n)
        await store.update_attempt(aid, status="failed", infra_failure=1,
                                    tokens_used=tokens)
    aid = await store.create_attempt(t.id, 4)
    await store.update_attempt(aid, status="failed", tokens_used=500_000)
    return t


async def test_infra_heavy_task_is_not_budget_exhausted_at_8M_cap(store):
    """PR #605's AC1, the discriminating case: 3 infra-classified attempts
    totalling 7.9M weighted + 1 genuine attempt at 0.5M weighted must NOT
    trip BUDGET_EXHAUSTED at an 8M cap.

    RED on unfixed code: the infra attempts' tokens summed unconditionally
    into the cap right alongside the genuine attempt's (7.9M + 0.5M = 8.4M
    over the 8M cap), even though their ATTEMPTS were already spared —
    exactly how ae2a535c killed itself at 17.3M/17M with no genuine attempt
    anywhere near that size. `Store._lifetime_included_sql` now gates the
    token sums with the identical predicate that already gated the attempt
    count, so the fix reads this as 0.5M/8M — comfortably under."""
    t = await _infra_heavy_task(store)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is None, (
        "3 infra-classified attempts (7.9M weighted) must not count toward "
        "the 8M cap — only the genuine 0.5M attempt should")


async def test_infra_heavy_tasks_excluded_spend_is_reported(store):
    """The excluded spend must never simply vanish: it is reported on the
    emitted `lifetime_budget` event (`excluded_tokens_weighted`/
    `excluded_tokens_raw`) so `nh status`/the drawer can show "X weighted
    excluded as infra" — and it must equal exactly the 7.9M the 3 infra
    attempts burned, not a partial or double-counted figure."""
    t = await _infra_heavy_task(store)
    events = []
    o = _orch(store)
    o._sink = events.append

    assert await o._check_lifetime_budget(t) is None
    ev = next(e for e in events if e["kind"] == "lifetime_budget")
    assert ev["attempts_used"] == 1, (
        "only the genuine attempt should consume a lifetime attempt")
    assert ev["tokens_used"] == 500_000, (
        "the task's own GATED raw spend must be the genuine attempt alone")
    assert ev["excluded_tokens_weighted"] == 7_900_000
    assert ev["excluded_tokens_raw"] == 7_900_000


async def test_genuine_attempts_tokens_still_count_exactly_as_before(store):
    """Regression (c): a task with ONLY genuine attempts — no infra,
    mechanical, or dead-interrupted rows — must see its tokens counted
    exactly as before this change. The new EXCLUDED bucket must never eat
    into an ordinary attempt's contribution to the cap."""
    t = Task.new("genuine", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=1_000_000)

    used_attempts, by_class, excluded = (
        await store.lifetime_usage_by_class(t.id))
    assert used_attempts == 2
    assert (by_class["tokens_used"] + by_class["cache_read_tokens"]
            + by_class["cache_creation_tokens"]) == 2_000_000
    assert sum(excluded.values()) == 0, (
        "an all-genuine task must exclude nothing")


async def test_interrupted_attempt_with_real_spend_counts_on_both_axes(store):
    """THE BOUNDARY (incident 2026-08-20; full history in
    `tests/test_dead_interrupted_not_work.py`, which this pins again here
    since it sits outside this ticket's VERIFY command's file list): an
    `interrupted` row is excluded ONLY when it burned zero priced work. A
    worker that died mid-attempt with real spend already on the meter must
    still count — on BOTH the attempt axis and the token axis — because the
    work happened and was lost, and that spend pressure is real."""
    t = Task.new("interrupted-real-spend", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, status="interrupted", tokens_used=2_000_000)

    used_attempts, by_class, excluded = (
        await store.lifetime_usage_by_class(t.id))
    assert used_attempts == 1, (
        "an interrupted attempt with real spend must still count as an "
        "attempt")
    assert by_class["tokens_used"] == 2_000_000, (
        "an interrupted attempt with real spend must still count its "
        "tokens")
    assert sum(excluded.values()) == 0


async def test_budget_exhausted_blocker_names_both_figures(store):
    """AC3: when the task itself IS over cap, the BUDGET_EXHAUSTED blocker's
    evidence must name BOTH this task's own (gated) spend and what its
    environment burned that was excluded from the cap — never just the
    former, which would read as if the excluded spend never happened."""
    t = Task.new("exhausted-plus-infra", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 1_000_000, BUDGET_UNIT_KEY: WEIGHTED_UNIT}
    await store.create_task(t)
    # Genuine spend alone exceeds this deliberately tiny cap: 2 attempts x
    # (500K tokens_used + 500K cache_read_tokens) = 1.1M weighted (fresh at
    # 1.0, cache-read at 0.1) against a 1.0M cap.
    await _spend(store, t.id, attempts=2, tokens_each=1_000_000)
    # Plus an infra-classified attempt burning real tokens that must show up
    # as EXCLUDED, not silently absorbed into the task's own figure.
    aid = await store.create_attempt(t.id, 3)
    await store.update_attempt(aid, status="failed", infra_failure=1,
                                tokens_used=3_000_000)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "1,100,000" in b.evidence, (
        "the task's own gated weighted spend must be named")
    assert "Excluded as infra/mechanical/dead-interrupted" in b.evidence
    assert "3,000,000" in b.evidence, (
        "the excluded environment spend must be named, not dropped")


def test_one_predicate_governs_both_attempt_count_and_token_sums():
    """AC2: `Store._lifetime_included_sql` must be the ONLY predicate
    governing both axes — no second, independently-typed condition computed
    separately for the attempt count vs. the token sums (the exact drift
    this ticket's defect was: the attempt count had a CASE exclusion, the
    token SUMs had none).

    Asserted on the SOURCE, not just behaviourally: `_lifetime_included_sql`
    is called exactly once inside `lifetime_usage_by_class`, bound to one
    local (`included_sql`), and that local is what the attempt-count
    SELECT, the included-sum CASE expressions, AND the excluded-sum CASE
    expressions all reference — never a second `_lifetime_included_sql()`
    call and never a hand-rolled duplicate condition."""
    src = inspect.getsource(Store.lifetime_usage_by_class)
    assert src.count("_lifetime_included_sql()") == 1, (
        "the predicate must be computed exactly once, not re-derived per "
        "axis")
    # Counting bare "included_sql" occurrences is gameable: the method's own
    # name (`_lifetime_included_sql`) and its docstring/comment mentions
    # ALSO contain that substring, so a padded count can pass an ablation
    # that reverted the actual reuse. Assert the three SPECIFIC reuse
    # sites — the two CASE conditions and the attempt-count SELECT — by
    # their exact surrounding syntax instead.
    assert src.count("cond=included_sql") == 2, (
        "the single computed predicate must be reused, unmodified, as the "
        "CASE condition for BOTH the included and the excluded token sums")
    assert src.count("{included_sql}") == 1, (
        "the single computed predicate must be reused, unmodified, as the "
        "attempt-count SELECT's CASE condition")


async def test_the_raise_option_actually_raises_and_unblocks(store):
    """The SCOPE_EXPLOSION lesson: an option must carry the action that makes
    it true. Applying it and re-checking must clear the blocker."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)

    # The one-click raise option only exists with the off-switch on — with
    # `budget.exhaustion_terminal` (the default) there is no question and no
    # options at all. The RAISE MECHANISM itself is unchanged and is asserted
    # for the default mode too, one test down.
    o = _orch(store, config=ASK_THE_HUMAN)
    b = await o._check_lifetime_budget(t)
    raise_opt = next(opt for opt in b.options if opt.action)
    summary = apply_action(t, raise_opt.action)
    assert "lifetime_attempts" in summary

    assert await o._check_lifetime_budget(t) is None, (
        "after the human raises the budget, the same check must pass"
    )


async def test_the_human_raise_path_still_clears_the_gate_when_terminal(store):
    """The guard rail on the change of 2026-08-09: making exhaustion TERMINAL
    removed the question, not the human's ability to raise a cap. `nh task
    config` (apply_action with human_override=True — commands.py:task_config)
    is that path, and after it the same check must pass."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)

    o = _orch(store)  # default: terminal
    b = await o._check_lifetime_budget(t)
    assert b is not None and not b.options, "terminal mode offers no options"

    summary = apply_action(t, {"set_task_config": {"lifetime_attempts": 12}},
                           human_override=True)
    assert "lifetime_attempts=12" in summary
    assert await o._check_lifetime_budget(t) is None, (
        "an explicit human budget raise must still clear the gate"
    )


async def test_per_task_override_wins_over_config(store):
    t = Task.new("task", repo_path="/tmp/x")
    t.config = {"lifetime_attempts": 2}
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=100)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and "attempts 2/2" in b.root_cause_hypothesis


async def test_killed_rows_with_zero_tokens_still_count_as_attempts(store):
    """Pre-1638427 rows recorded zero tokens; they still spent the attempt.

    Closed with `status="failed"` rather than left for `create_attempt`'s own
    supersede sweep to close: a row the sweep tags `status="interrupted"` with
    zero recorded work is the 2026-08-20 DEAD shape (a worker that died before
    doing anything) and is now excluded from the cap by design — see THE
    BOUNDARY in `Store.lifetime_usage_by_class`. A pre-1638427 row is the
    opposite: real work happened and genuinely CLOSED, only its token columns
    under-reported it because of the metering gap that commit fixed. `failed`
    always counts whatever its token columns say, so stamping it here is what
    keeps this test asserting the pre-1638427 guarantee instead of colliding
    with the new one.
    """
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    for n in range(1, 10):
        aid = await store.create_attempt(t.id, n)  # no token columns at all
        await store.update_attempt(aid, status="failed")

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and "attempts 9/9" in b.root_cause_hypothesis


# --------------------------------------------------------------------------- #
# What the gate COUNTS.
#
# `lifetime_usage` summed two of the attempts table's twelve token columns:
# tokens_used + cache_read_tokens. It ignored every cache_creation_* column and
# all three of the review_*, plan_* and utility_* tiers — so the gate that
# exists to stop runaway spend could not see reviewer, planner or utility burn
# at all. eval/northstar.py already summed all four tiers, so the budget gate
# and the benchmark were reporting different numbers for the same task.
#
# Measured over 574 real attempt rows (184 tasks): the ignored columns are
# 16.2% of true spend, and utility_cache_creation_tokens alone (60.1M) exceeds
# the coder's own cache_creation_tokens (59.0M).
# --------------------------------------------------------------------------- #

_TIERS = ("", "review_", "plan_", "utility_")


async def test_every_tier_and_both_cache_columns_reach_the_gate(store):
    """One attempt, a distinct power of ten in each of the twelve columns, so
    any dropped column changes the total by a recognisable amount."""
    t = Task.new("all tiers", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)

    cols, expected = {}, 0
    for i, tier in enumerate(_TIERS):
        base = "tokens_used" if tier == "" else f"{tier}tokens_used"
        for j, suffix in enumerate((base, f"{tier}cache_read_tokens",
                                    f"{tier}cache_creation_tokens")):
            value = 10 ** (i * 3 + j + 1)
            cols[suffix] = value
            expected += value
    await store.update_attempt(aid, **cols)

    attempts, tokens = await store.lifetime_usage(t.id)
    assert attempts == 1
    assert tokens == expected, (
        f"gate saw {tokens:,} of {expected:,} actually spent; "
        f"missing {expected - tokens:,}"
    )


async def test_a_task_whose_spend_is_all_reviewer_is_not_invisible(store):
    """The pathological shape: a task that burns its whole budget in the
    reviewer tier used to read as ZERO to the gate and could never be parked."""
    t = Task.new("review burn", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, review_tokens_used=2_000_000,
        review_cache_read_tokens=16_000_000,
        review_cache_creation_tokens=1_000_000,
    )

    _, tokens = await store.lifetime_usage(t.id)
    assert tokens == 19_000_000

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and b.category is BlockerCategory.BUDGET_EXHAUSTED


async def test_the_gate_and_the_benchmark_agree(store):
    """eval/northstar.py sums all four tiers to report cost. The budget gate
    must report the same number for the same rows, or one of the two is lying."""
    t = Task.new("agreement", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    cols = {}
    for tier in _TIERS:
        cols["tokens_used" if tier == "" else f"{tier}tokens_used"] = 7
        cols[f"{tier}cache_read_tokens"] = 70
        cols[f"{tier}cache_creation_tokens"] = 700
    await store.update_attempt(aid, **cols)

    # Recomputed the way eval/northstar.py does it — over the stored ROWS, not
    # by calling the function under test.
    rows = await store.list_attempts(t.id)
    northstar_total = 0
    for r in rows:
        for tier in _TIERS:
            northstar_total += int(r[("tokens_used" if tier == ""
                                      else f"{tier}tokens_used")] or 0)
            northstar_total += int(r[f"{tier}cache_read_tokens"] or 0)
            northstar_total += int(r[f"{tier}cache_creation_tokens"] or 0)

    _, gate_total = await store.lifetime_usage(t.id)
    assert gate_total == northstar_total == 4 * (7 + 70 + 700)


# --------------------------------------------------------------------------- #
# What the gate PRICES.
#
# Counting the three token classes 1:1 does not bound spend, it bounds
# conversation LENGTH. Cache reads bill at a tenth of fresh input and cache
# writes at 1.25x, so a raw cap fires an order of magnitude before the dollar
# cost it exists to bound — and it fires at a DIFFERENT dollar cost for every
# task, because the per-task weighted/raw ratio ranges 0.122..0.697 over this
# install's ledger.
#
# The live casualty: task d6e4b72a was killed BUDGET_EXHAUSTED at
# "12,367,237/12,000,000 tokens" after 22 minutes. Two measurements of it are
# used below, and they are different snapshots of the same run — both real,
# neither reconstructed:
#
#   * D6E4B72A_ATTEMPT — the usage events of the killing attempt, summed
#     (the numbers quoted in the incident: 1,697 / 6,589,429 / 173,190).
#   * D6E4B72A_LEDGER — the task's four attempt ROWS as the DB holds them,
#     per class, read out of ~/.no_human/no_human.db on 2026-07-31. Larger
#     than the 12,367,237 it died at, because an aborted attempt banks its
#     whole spend to the row afterwards.
# --------------------------------------------------------------------------- #

from no_human.core.pricing import (  # noqa: E402
    CACHE_CREATION_WEIGHT, CACHE_READ_WEIGHT, FRESH_WEIGHT,
    class_breakdown, weighted_tokens,
)

#: The killing attempt's usage events, summed. Raw 6,764,316.
D6E4B72A_ATTEMPT = {
    "tokens_used": 1_697,
    "cache_read_tokens": 6_589_429,
    "cache_creation_tokens": 173_190,
}
#: The same task's four attempt rows, per class. Raw 16,527,553.
D6E4B72A_LEDGER = {
    "tokens_used": 65_968,
    "cache_read_tokens": 15_005_665,
    "cache_creation_tokens": 1_455_920,
}
#: The cap it actually ran under (a human raise, in task.config).
D6E4B72A_CAP = 12_000_000


def test_each_token_class_is_weighted_by_its_own_price():
    """One class at a time, so a dropped or transposed term is visible."""
    assert weighted_tokens(tokens_used=1_000_000) == 1_000_000
    assert weighted_tokens(cache_read_tokens=1_000_000) == 100_000
    assert weighted_tokens(cache_creation_tokens=1_000_000) == 1_250_000
    # Nothing is free and nothing is double-counted: the mix is the sum.
    assert weighted_tokens(tokens_used=1_000_000, cache_read_tokens=1_000_000,
                           cache_creation_tokens=1_000_000) == 2_350_000
    assert weighted_tokens() == 0
    # The classes are NOT interchangeable — 12.5x apart end to end. A
    # positional call could not have produced this asymmetry.
    assert (weighted_tokens(cache_creation_tokens=1_000)
            == 12 * weighted_tokens(cache_read_tokens=1_000) + 50)
    # The weights themselves, so a silent re-pricing is a test failure and not
    # a quiet change in what every cap means.
    assert (FRESH_WEIGHT, CACHE_CREATION_WEIGHT, CACHE_READ_WEIGHT) == (
        1.0, 1.25, 0.1)


def test_known_event_mixes_price_the_way_the_incident_did():
    """The two d6e4b72a snapshots, priced. Both are ~5x cheaper than raw."""
    assert sum(D6E4B72A_ATTEMPT.values()) == 6_764_316          # raw
    assert weighted_tokens(**D6E4B72A_ATTEMPT) == 877_127       # real
    assert sum(D6E4B72A_LEDGER.values()) == 16_527_553          # raw
    assert weighted_tokens(**D6E4B72A_LEDGER) == 3_386_434      # real
    # A purely fresh burn is priced at face value — the weighting is not a
    # blanket discount, it is a class distinction.
    assert weighted_tokens(tokens_used=6_764_316) == 6_764_316


async def test_d6e4b72a_would_not_have_been_killed_when_it_was(store):
    """The regression the fix exists for, at the moment it fired.

    The task carried an UNMARKED raw 12,000,000 raise — its real stored
    config — so the cutover guard reads that grant as 2,382,000 weighted. At
    the kill, the running attempt had spent 6,764,316 raw / 877,127
    fresh-equivalent: over the raw ceiling that stopped it, and comfortably
    under the grant it actually held. It would have kept going.
    """
    t = Task.new("Silent task failure: a pool crash records no reason",
                 repo_path="/tmp/x")
    t.config = {"lifetime_tokens": D6E4B72A_CAP}          # raw, unmarked
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_ATTEMPT)

    o = _orch(store)
    # 12,000,000 raw converts to 2,382,000 — but that is BELOW the ungranted
    # 4,000,000 default, so the raise-floor (R1) applies the default instead.
    # Either way the grant is read in its own unit and either way this task
    # survives; the floor only made the raise stop being a cut.
    assert raw_cap_as_weighted(D6E4B72A_CAP) == 2_382_000
    assert o._lifetime_limits(t)[1] == 4_000_000, "grant read in its own unit"
    _, raw = await store.lifetime_usage(t.id)
    assert raw == 6_764_316 > 2_382_000, "the raw sum is what stopped it"

    assert await o._check_lifetime_budget(t) is None, (
        "the task killed at 12,367,237/12,000,000 must survive: its real "
        f"spend was {weighted_tokens(**D6E4B72A_ATTEMPT):,} fresh-equivalent"
    )


async def test_d6e4b72a_is_not_handed_a_blank_cheque_either(store):
    """The honest other half, recorded so the fix is not oversold.

    Across all FOUR of its attempts the task spent 3,386,434 weighted. Against
    a grant of 2,382,000 it parks — later than the raw counter killed it, and
    on a true reading of what it cost. Re-pricing the counter moved WHEN the
    gate fires; it did not exempt an expensive task from it.

    MOVED BY R1, and stated rather than hidden: an UNMARKED 12,000,000 no
    longer means 2,382,000, because that reading turned a raise into a cut. It
    now means the ungranted 4,000,000, and 3,386,434 is under that, so THIS
    ledger no longer parks on the unmarked config. That is the trade the
    2026-08-03 global raise already accepted at 6.8% of tasks — it is not a
    weakening of the gate, and the marked half below proves the gate itself is
    untouched: state the same grant unambiguously and the same spend still
    parks, at the same arithmetic, to the token.
    """
    t = Task.new("d6e4b72a, all four attempts", repo_path="/tmp/x")
    # 2,382,000 — the same grant, stated in the unit the gate enforces.
    t.config = {"lifetime_tokens": raw_cap_as_weighted(D6E4B72A_CAP), **_MARKED}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_LEDGER)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "3,386,434/2,382,000" in b.root_cause_hypothesis

    # ...and the unmarked half, so the change of meaning is on the record.
    u = Task.new("d6e4b72a, unmarked", repo_path="/tmp/x")
    u.config = {"lifetime_tokens": D6E4B72A_CAP}          # raw, unmarked
    await store.create_task(u)
    uid = await store.create_attempt(u.id, 1)
    await store.update_attempt(uid, **D6E4B72A_LEDGER)
    assert await _orch(store)._check_lifetime_budget(u) is None, (
        "3,386,434 is under the floored 4,000,000 grant — R1"
    )


async def test_a_genuinely_expensive_task_still_parks_at_the_same_cap(store):
    """The other side, and the reason this is a re-pricing and not a raise.

    The IDENTICAL raw burn to the d6e4b72a ledger above — 16,527,553 tokens to
    the token — under the identical 12,000,000 cap, but spent on fresh input
    and cache writes instead of prefix re-reads. Opposite verdict, which is the
    whole point: the gate now discriminates on cost, where before these two
    were indistinguishable and it parked both.
    """
    expensive = {"tokens_used": 14_600_000, "cache_read_tokens": 1_027_553,
                 "cache_creation_tokens": 900_000}
    assert sum(expensive.values()) == sum(D6E4B72A_LEDGER.values())
    assert weighted_tokens(**expensive) == 15_827_755 > D6E4B72A_CAP

    t = Task.new("expensive", repo_path="/tmp/x")
    # Marked: this compares against 12,000,000 as a WEIGHTED cap, which is the
    # post-cutover meaning of that number.
    t.config = {"lifetime_tokens": D6E4B72A_CAP, "budget_unit": "weighted"}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **expensive)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "cost-weighted tokens 15,827,755/12,000,000" in b.root_cause_hypothesis


async def test_the_blocker_reports_the_weighted_number_and_the_raw_classes(store):
    """Honest reporting: the operator must see the number the gate acted on AND
    the raw split it came from, or they cannot reconcile the park against a
    bill or against `nh logs`."""
    t = Task.new("park me", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 1_000_000, "budget_unit": "weighted"}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_LEDGER)

    b = await _orch(store, config=ASK_THE_HUMAN)._check_lifetime_budget(t)
    assert b is not None
    assert "3,386,434" in b.evidence and "cost-weighted" in b.evidence
    # Every raw class, named and priced, not just their sum.
    for raw_value in ("65,968", "15,005,665", "1,455,920"):
        assert raw_value in b.evidence, f"{raw_value} missing from {b.evidence}"
    for label in ("fresh", "cache-write", "cache-read"):
        assert label in b.evidence
    # The raise option is in the same unit as the cap it raises.
    raise_opt = next(o for o in b.options if o.action)
    assert "cost-weighted" in raise_opt.label
    # 3,386,434 x 1.5, rounded up to the next 100,000.
    assert raise_opt.action["set_task_config"]["lifetime_tokens"] == 5_100_000

    # TERMINAL mode reports the SAME evidence and the SAME proportional raise —
    # it drops the question, not the numbers. A record that lost the figure the
    # operator needs to act on would be a worse escalation, not a better park.
    term = await _orch(store)._check_lifetime_budget(t)
    assert term is not None
    assert term.evidence == b.evidence
    assert term.question is None and term.options == []
    assert "lifetime_tokens=5100000" in (term.wake_condition or "")


async def test_the_under_budget_event_carries_both_numbers(store):
    """The passing path reports the same two quantities the blocker does — and
    keeps `tokens_used` RAW, because that name means the raw burn on every
    other surface (`nh`, the web meters, eval/northstar.py)."""
    t = Task.new("running", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_ATTEMPT)

    events: list = []
    o = _orch(store)
    o._sink = events.append
    assert await o._check_lifetime_budget(t) is None
    ev = next(e for e in events if e["kind"] == "lifetime_budget")
    assert ev["tokens_used"] == 6_764_316          # raw, unchanged meaning
    assert ev["tokens_weighted"] == 877_127        # what the cap compares
    assert ev["raw_fresh"] == 1_697
    assert ev["raw_cache_read"] == 6_589_429
    assert ev["raw_cache_creation"] == 173_190
    assert "877,127" in ev["text"] and "4,000,000" in ev["text"]
    # web/src/summaries.js clips this text to 60 chars for the Activity
    # header's "Budget" fact. Anything longer renders truncated mid-number,
    # which is why the class split lives in the fields above and not in here.
    assert len(ev["text"]) <= 60, f"{len(ev['text'])} chars: {ev['text']!r}"


async def test_the_class_split_sees_every_tier_and_column(store):
    """`lifetime_usage_by_class` must reach all twelve ADDEND columns, like
    `lifetime_usage` — a tier missed here is spend the price weighting never
    sees. Distinct powers of ten so any dropped column is identifiable.

    Plus the four `*output_tokens` columns, which are NOT addends: each is the
    output slice of the `*tokens_used` column beside it. They are set to that
    same value here — an all-output attempt, which is coherent and keeps the
    powers of ten distinct — so a dropped output column is identifiable the
    same way, while the raw total must not move by a single token."""
    t = Task.new("all tiers", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)

    cols, per_class = {}, {"tokens_used": 0, "cache_read_tokens": 0,
                           "cache_creation_tokens": 0}
    for i, tier in enumerate(_TIERS):
        base = "tokens_used" if tier == "" else f"{tier}tokens_used"
        for j, (col, cls) in enumerate((
            (base, "tokens_used"),
            (f"{tier}cache_read_tokens", "cache_read_tokens"),
            (f"{tier}cache_creation_tokens", "cache_creation_tokens"),
        )):
            value = 10 ** (i * 3 + j + 1)
            cols[col] = value
            per_class[cls] += value
        cols[f"{tier}output_tokens"] = cols[base]
    await store.update_attempt(aid, **cols)

    attempts, by_class, _excluded = await store.lifetime_usage_by_class(t.id)
    assert attempts == 1
    assert {k: v for k, v in by_class.items() if k != "output_tokens"} == per_class
    # Every output column reached, and none double-counted: an attempt that is
    # 100% output reports exactly the fresh class as its output share.
    assert by_class["output_tokens"] == per_class["tokens_used"]
    # And the raw total is still exactly the sum of the three ADDEND classes,
    # so the burn figure every other surface shows cannot drift from the priced
    # one — the output slice is inside `tokens_used`, never beside it.
    _, raw = await store.lifetime_usage(t.id)
    assert raw == sum(per_class.values())
    # The priced number, by contrast, MUST move: that is the defect this
    # column exists to close. An all-output fresh class bills 5x, not 1x.
    assert weighted_tokens(**by_class) == weighted_tokens(
        **per_class) + 4 * per_class["tokens_used"]


async def test_the_mid_attempt_watch_prices_the_same_way_the_gate_does(store):
    """The sink's running total is the second enforcement point. It must reach
    the ceiling on the same arithmetic, or the mid-attempt abort fires where
    the boundary check would have said the task was fine — which is exactly
    what killed d6e4b72a 22 minutes in."""
    from no_human.core.orchestrator import BudgetAbort, Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o._sink = lambda e: None
    o._cancel_reason = None
    o._active_task_id = "task-1"
    # The ceiling d6e4b72a's killing attempt actually ran under: what was left
    # of its 12,000,000 after 5,602,921 of earlier spend had been banked.
    o._begin_attempt_accounting("task-1", remaining_tokens=12_000_000 - 5_602_921)

    from no_human.agent.claude_backend import AgentEvent
    from no_human.core.orchestrator import CODER_ROLE
    # The killing attempt's own usage, delivered as one event. Raw 6,764,316
    # is over the 6,397,079 ceiling — that is the abort that happened.
    assert sum(D6E4B72A_ATTEMPT.values()) > 6_397_079
    o._agent_sink(AgentEvent("usage", meta=dict(D6E4B72A_ATTEMPT)),
                  role=CODER_ROLE)  # priced at 877,127 — must NOT abort

    # Same ceiling, a fresh-heavy attempt of comparable raw size: aborts.
    o._begin_attempt_accounting("task-1", remaining_tokens=12_000_000 - 5_602_921)
    with pytest.raises(BudgetAbort) as exc:
        o._agent_sink(AgentEvent("usage", meta={
            "tokens_used": 6_000_000, "cache_read_tokens": 400_000,
            "cache_creation_tokens": 400_000}), role=CODER_ROLE)
    # And it says both numbers, for the same honest-reporting reason.
    assert "6,540,000 cost-weighted" in str(exc.value)
    assert "raw 6,800,000" in str(exc.value)


def test_the_class_breakdown_names_and_prices_every_class():
    text = class_breakdown(**D6E4B72A_ATTEMPT)
    assert "raw 6,764,316" in text
    assert "1,697 fresh (x1)" in text
    assert "173,190 cache-write (x1.25)" in text
    assert "6,589,429 cache-read (x0.1)" in text


# --------------------------------------------------------------------------- #
# The CUTOVER guard.
#
# The caps changed UNIT on 2026-07-31. Measured on the live ledger that day:
# 165 tasks carry a `task.config["lifetime_tokens"]` written in RAW tokens
# (12,000,000 to 68,000,000) and 162 carry a raw `attempt_tokens` (4M or 6M).
# They are not stale history — 94 of the 165 sit in escalated / failed /
# paused_quota / implementing, i.e. exactly what a mass retry picks up, and
# across the 91 escalated+failed alone the stored ceilings total 1.388 BILLION
# where ~275M was intended.
#
# Read verbatim against a weighted gate, every one of those is ~5x the budget
# the human granted. And `blockers/actions.py` never lowers a stored cap, so
# without a unit-aware comparison the stale value is PERMANENT: the raise flow
# that exists to fix a budget would keep re-affirming it.
#
# The guard is a read-time interpretation, not a migration: nothing is written
# to anyone's database, and there is no ordering requirement between deploying
# the code and fixing the data.
# --------------------------------------------------------------------------- #

from no_human.core.pricing import (  # noqa: E402
    BUDGET_UNIT_KEY, RAW_TO_WEIGHTED_RATIO, TOKEN_CAP_KEYS, WEIGHTED_UNIT,
    config_is_weighted, override_inverted, raw_cap_as_weighted,
)

_MARKED = {BUDGET_UNIT_KEY: WEIGHTED_UNIT}


def test_an_unmarked_stored_cap_is_read_as_raw_and_converted():
    """The reviewer's red-green case: the same stored 12,000,000 means two
    different budgets, and which one it means is decided by the marker."""
    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()

    # 68,000,000 — the largest stale row, and one whose CONVERSION still lands
    # above the ungranted default, so it shows the conversion on its own with
    # no interaction from the raise-floor below.
    stale = Task.new("pre-cutover", repo_path="/tmp/x")
    stale.config = {"lifetime_tokens": 68_000_000}
    assert o._lifetime_limits(stale)[1] == 13_498_000

    fresh = Task.new("post-cutover", repo_path="/tmp/x")
    fresh.config = {"lifetime_tokens": 68_000_000, **_MARKED}
    assert o._lifetime_limits(fresh)[1] == 68_000_000

    # 12,000,000 is the SAME conversion — but its result, 2,382,000, is below
    # the ungranted 4,000,000 default, so the raise-floor takes over. R1.
    stale.config = {"lifetime_tokens": 12_000_000}
    assert raw_cap_as_weighted(12_000_000) == 2_382_000
    assert o._lifetime_limits(stale)[1] == Bounds().lifetime_tokens
    fresh.config = {"lifetime_tokens": 12_000_000, **_MARKED}
    assert o._lifetime_limits(fresh)[1] == 12_000_000

    # BOTH keys, not just the loud one: 162 rows carry a raw attempt_tokens.
    stale.config = {"attempt_tokens": 20_000_000}
    assert o._attempt_token_cap(stale) == 3_970_000
    fresh.config = {"attempt_tokens": 20_000_000, **_MARKED}
    assert o._attempt_token_cap(fresh) == 20_000_000
    # ...and the raw 4,000,000 the 162 rows actually carry converts to 794,000,
    # under the 2,000,000 default, so it floors too.
    stale.config = {"attempt_tokens": 4_000_000}
    assert raw_cap_as_weighted(4_000_000) == 794_000
    assert o._attempt_token_cap(stale) == Bounds().attempt_tokens
    fresh.config = {"attempt_tokens": 4_000_000, **_MARKED}
    assert o._attempt_token_cap(fresh) == 4_000_000
    assert TOKEN_CAP_KEYS == {"lifetime_tokens", "attempt_tokens"}


def test_the_marker_check_fails_closed_on_anything_that_is_not_the_marker():
    """Absence is the safe state, because the unsafe direction spends money."""
    assert config_is_weighted({BUDGET_UNIT_KEY: WEIGHTED_UNIT}) is True
    for hostile in (None, {}, {"budget_unit": "raw"}, {"budget_unit": True},
                    {"budget_unit": "WEIGHTED"}, {"lifetime_tokens": 1}, "weighted"):
        assert config_is_weighted(hostile) is False, hostile


def test_the_conversion_is_the_measured_corpus_ratio():
    assert RAW_TO_WEIGHTED_RATIO == 0.1985
    assert raw_cap_as_weighted(12_000_000) == 2_382_000
    assert raw_cap_as_weighted(68_000_000) == 13_498_000   # the largest stale row
    # Never rounds an override away to "no override" — that would silently
    # RAISE a deliberately tiny cap to the default instead of lowering it.
    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    tiny = Task.new("tiny", repo_path="/tmp/x")
    tiny.config = {"lifetime_tokens": 3}
    assert o._lifetime_limits(tiny)[1] == 1
    assert o._lifetime_limits(tiny)[1] < Bounds().lifetime_tokens


def test_a_stale_raw_ceiling_is_correctable_and_does_not_become_permanent():
    """`actions.py` never lowers a stored cap. Comparing a weighted request
    against an unconverted raw prior means the raw one always wins and is then
    written back stamped as weighted — freezing the 5x forever, through the
    very flow that exists to correct a budget."""
    t = Task.new("stale", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 12_000_000, "attempt_tokens": 4_000_000}

    summary = apply_action(t, {"set_task_config": {"lifetime_tokens": 5_100_000}})

    assert t.config["lifetime_tokens"] == 5_100_000, summary
    assert t.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT
    # ...and the never-lower guard is INTACT, not disabled: a request under the
    # normalised prior is still refused. The prior is 4,000,000, not the
    # converted 2,382,000 — see the raise-floor block below (R1).
    t2 = Task.new("stale2", repo_path="/tmp/x")
    t2.config = {"lifetime_tokens": 12_000_000}
    apply_action(t2, {"set_task_config": {"lifetime_tokens": 1_000_000}})
    assert t2.config["lifetime_tokens"] == 4_000_000
    assert t2.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT


def test_the_marker_is_stamped_by_the_write_path_not_asked_of_callers():
    """Every sanctioned write lands in apply_action; a caller that forgot the
    marker would shrink its own cap 5x on the next read."""
    t = Task.new("t", repo_path="/tmp/x")
    apply_action(t, {"set_task_config": {"attempt_tokens": 900_000}})
    assert t.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT
    # The human CLI path stamps it too.
    t2 = Task.new("t2", repo_path="/tmp/x")
    apply_action(t2, {"set_task_config": {"lifetime_tokens": 5}},
                 human_override=True)
    assert t2.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT
    # A non-token cap must NOT claim a token unit it does not have.
    t3 = Task.new("t3", repo_path="/tmp/x")
    apply_action(t3, {"set_task_config": {"lifetime_attempts": 12}})
    assert BUDGET_UNIT_KEY not in t3.config


def test_a_repo_profile_default_is_covered_by_the_same_guard():
    """MEDIUM-1: `profile.apply_default_task_config` copies repo-level defaults
    straight into task.config and writes no marker, so those are raw too. Unset
    on this install, but it is the same field the 165 stale rows came through —
    confirmed, not assumed."""
    from no_human.profile import ProjectProfile, apply_default_task_config
    from no_human.core.orchestrator import Orchestrator

    prof = ProjectProfile(repo_path="/r", default_attempt_tokens=6_000_000,
                          default_lifetime_tokens=16_000_000)
    cfg = apply_default_task_config(prof, {})
    assert BUDGET_UNIT_KEY not in cfg, "profile defaults carry no unit marker"

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    t = Task.new("from profile", repo_path="/tmp/x")
    t.config = cfg
    # 16M -> 3,176,000 and 6M -> 1,191,000 are the conversions; both land under
    # the ungranted defaults (4,000,000 / 2,000,000), so both floor. R1.
    assert raw_cap_as_weighted(16_000_000) == 3_176_000
    assert raw_cap_as_weighted(6_000_000) == 1_191_000
    assert o._lifetime_limits(t)[1] == Bounds().lifetime_tokens
    assert o._attempt_token_cap(t) == Bounds().attempt_tokens


# --------------------------------------------------------------------------- #
# R1 — the raise-floor.
#
# Funnel forensics, 2026-08-10: the `no_human` repo profile carried
# `default_lifetime_tokens = 12,000,000`, written BEFORE the 07-31 cutover, so
# unmarked, so read as raw and converted to 2,382,000 — BELOW the ungranted
# 4,000,000 default. An override typed as a raise was applied as a 40% CUT, and
# nothing said so. 32 of 33 August tasks ran against `/2,382,000`; the median
# August task spent 2,210,973 and died at that wall. The one August task
# without a profile override (`e68a85e0`, in another repo) ran against
# `/4,000,000` and was the only one to reach `awaiting_approval`.
#
# The invariant: an override written as a raise can never leave a task WORSE
# off than having written no override at all. That is the only thing floored —
# a value below the default in BOTH units is a deliberate lowering and is
# untouched, so no legitimate flow loses anything.
# --------------------------------------------------------------------------- #

def test_a_pre_cutover_raise_never_converts_into_a_cut(caplog):
    """THE AUGUST DEATH SHAPE. Red on the parent commit: it returns 2,382,000."""
    import logging

    from no_human.core.orchestrator import Orchestrator
    from no_human.profile import ProjectProfile, apply_default_task_config

    prof = ProjectProfile(repo_path="/repos/no_human",
                          default_lifetime_tokens=12_000_000)
    t = Task.new("august", repo_path="/repos/no_human")
    t.config = apply_default_task_config(prof, {})
    assert t.config == {"lifetime_tokens": 12_000_000}, "unmarked, as written"

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    with caplog.at_level(logging.WARNING, logger="no_human.orchestrator"):
        cap = o._lifetime_limits(t)[1]

    assert cap == 4_000_000, "the ungranted default, not the converted 2,382,000"
    assert cap != 2_382_000

    # The warning has to be actionable on its own: both numbers, the cause and
    # the remedy. Nothing warned in August, which is why it ran for nine days.
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "12,000,000" in msg
    assert "2,382,000" in msg
    assert "4,000,000" in msg
    assert "lifetime_tokens" in msg
    assert "/repos/no_human" in msg, "the remedy must be copy-pasteable"
    # BOTH surfaces: this function cannot tell a repo-profile default from a
    # per-task override — they arrive in the same dict — so naming only one
    # sends half the operators to a command that will not fix their task.
    assert "nh repo config /repos/no_human default_lifetime_tokens=" in msg
    assert f"nh task config {t.id[:8]} lifetime_tokens=" in msg
    # ...and it must NOT talk the operator into typing the default: that
    # discards the raise this guard exists to preserve.
    assert "<weighted>" in msg
    assert "nh repo config /repos/no_human default_lifetime_tokens=4000000" not in msg
    assert "COST-WEIGHTED" in msg


def test_a_genuine_post_cutover_raise_is_applied_verbatim(caplog):
    """A marked value is deliberate in the current unit — raise or lower, it
    is taken exactly as written and never floored."""
    import logging

    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()

    t = Task.new("raised", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 9_000_000, **_MARKED}
    with caplog.at_level(logging.WARNING, logger="no_human.orchestrator"):
        assert o._lifetime_limits(t)[1] == 9_000_000

    # A marked value BELOW the default is the one unambiguous way to ask for a
    # smaller budget, and the floor must not eat it.
    t.config = {"lifetime_tokens": 1_500_000, **_MARKED}
    assert o._lifetime_limits(t)[1] == 1_500_000
    assert caplog.records == [], "nothing ambiguous happened"


def test_a_deliberate_lowering_is_never_floored(caplog):
    """Below the default in BOTH units — there is no raise to preserve, so the
    conversion stands and the tiny caps every budget test uses still work."""
    import logging

    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    t = Task.new("tiny", repo_path="/tmp/x")

    with caplog.at_level(logging.WARNING, logger="no_human.orchestrator"):
        t.config = {"lifetime_tokens": 3}
        assert o._lifetime_limits(t)[1] == 1
        t.config = {"lifetime_tokens": 1_000_000}
        assert o._lifetime_limits(t)[1] == 198_500
        t.config = {"attempt_tokens": 500_000}
        assert o._attempt_token_cap(t) == 99_250

    assert caplog.records == [], "a lowering is not the ambiguous class"


def test_no_override_is_unchanged(caplog):
    import logging

    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    t = Task.new("plain", repo_path="/tmp/x")

    with caplog.at_level(logging.WARNING, logger="no_human.orchestrator"):
        for cfg in ({}, {"lifetime_tokens": 0}, {"lifetime_tokens": "nonsense"},
                    {"lifetime_tokens": None}):
            t.config = dict(cfg)
            assert o._lifetime_limits(t)[1] == Bounds().lifetime_tokens
            assert o._attempt_token_cap(t) == Bounds().attempt_tokens
    assert caplog.records == [], "no override, nothing to warn about"


def test_override_inverted_is_the_whole_predicate():
    """Pure, and it is the ONLY thing that decides the floor: raise as typed,
    cut as read. Everything else converts exactly as before."""
    assert override_inverted(12_000_000, 4_000_000) is True    # 2,382,000
    assert override_inverted(4_000_000, 2_000_000) is True     # 794,000
    # Not a raise to begin with — a deliberate lowering.
    assert override_inverted(1_000_000, 4_000_000) is False
    assert override_inverted(4_000_000, 4_000_000) is False    # not ">"
    # A raise that SURVIVES conversion needs no floor.
    assert override_inverted(68_000_000, 4_000_000) is False   # 13,498,000
    assert override_inverted(0, 4_000_000) is False
    assert override_inverted(None, 4_000_000) is False


def test_the_raise_floor_also_holds_where_a_stale_prior_is_normalised():
    """The laundering path. `actions.py` converts the same unmarked prior for
    its never-lower comparison and then STAMPS the result weighted — so an
    un-floored conversion freezes the cut permanently, out of reach of the read
    path's floor. Same helper, same answer, or the two disagree."""
    t = Task.new("stale", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 12_000_000}
    apply_action(t, {"set_task_config": {"lifetime_tokens": 1_000_000}})
    assert t.config["lifetime_tokens"] == 4_000_000, "floored prior kept, not 2,382,000"
    assert t.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT

    # The untouched SIBLING is converted-and-stamped by the same write, and
    # gets the same floor — otherwise the write itself installs a permanent cut.
    t2 = Task.new("sibling", repo_path="/tmp/x")
    t2.config = {"lifetime_tokens": 12_000_000, "attempt_tokens": 4_000_000}
    apply_action(t2, {"set_task_config": {"lifetime_tokens": 9_000_000}})
    assert t2.config["lifetime_tokens"] == 9_000_000
    assert t2.config["attempt_tokens"] == 2_000_000, "floored, not 794,000"
    assert t2.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT


# --------------------------------------------------------------------------- #
# D1: the writer's floor must use the SAME default the reader enforces.
#
# The first cut of the raise-floor floored `actions.py` against
# `Bounds()` — the dataclass LITERALS — while the reader floors against
# `Bounds.from_config(config["bounds"])`. On any install that tunes `bounds`
# the two disagree, and because this write STAMPS `budget_unit: weighted`, the
# disagreement is frozen permanently, out of reach of the read-time floor. It
# lands on the money path and it is silent both ways.
# --------------------------------------------------------------------------- #

def _writer_reader_pair(cfg_bounds: dict, stale: dict, action: dict):
    """Apply `action` to a task carrying `stale`, on an install configured with
    `cfg_bounds`; return (written config, what the reader makes of it)."""
    from no_human.core.orchestrator import Orchestrator

    bounds = Bounds.from_config(cfg_bounds)
    t = Task.new("tuned install", repo_path="/tmp/x")
    t.config = dict(stale)
    apply_action(t, {"set_task_config": action}, bounds=bounds)

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = bounds
    return t.config, (o._lifetime_limits(t)[1], o._attempt_token_cap(t))


def test_a_tuned_install_is_floored_against_its_own_default_not_the_literal():
    """Direction (a): an install that RAISED the defaults. Floored against the
    4,000,000 literal, a sibling write cut a configured 8,000,000 in half and
    stamped it, permanently."""
    written, read = _writer_reader_pair(
        {"lifetime_tokens": 8_000_000, "attempt_tokens": 4_000_000},
        {"lifetime_tokens": 12_000_000, "attempt_tokens": 6_000_000},
        {"attempt_tokens": 5_000_000},    # touches attempt; lifetime is the SIBLING
    )
    assert written["lifetime_tokens"] == 8_000_000, (
        "the untouched sibling was frozen at the dataclass literal, not this "
        f"install's default; config={written}")
    assert written[BUDGET_UNIT_KEY] == WEIGHTED_UNIT
    # The invariant, stated as the invariant: what the write freezes is what
    # the gate would have enforced. (5,000,000 clears the never-lower guard,
    # whose normalised prior here is the floored 4,000,000.)
    assert read == (8_000_000, 5_000_000)


def test_a_lowered_install_is_never_handed_the_bigger_literal():
    """Direction (b): an install that LOWERED the defaults. Floored against the
    literals, the same write hands it 4,000,000 / 2,000,000 — 4x what it
    configured — and stamps that as deliberate."""
    written, read = _writer_reader_pair(
        {"lifetime_tokens": 1_000_000, "attempt_tokens": 500_000},
        {"lifetime_tokens": 12_000_000, "attempt_tokens": 6_000_000},
        {"attempt_tokens": 1_500_000},
    )
    # Neither cap is inverted against these small defaults — 12M converts to
    # 2,382,000 which is still ABOVE 1,000,000 — so both are plain conversions
    # and NO floor applies. The literals must not appear anywhere.
    assert written["lifetime_tokens"] == 2_382_000, (
        f"handed a literal-derived cap it never configured; config={written}")
    assert 4_000_000 not in written.values()
    assert 2_000_000 not in written.values()
    assert read == (2_382_000, 1_500_000)


def test_a_write_to_one_cap_never_changes_what_the_other_ENFORCES():
    """The general statement, swept over install shapes.

    Deliberately NOT "the stamped value reads back as itself" — that is a
    tautology (a marked cap is taken verbatim by construction, so it holds
    even while the writer is using the wrong default; it passed on the defect).
    The real invariant compares across the write: what the untouched sibling
    ENFORCED before the write must be what it enforces after, on the same
    install. That is what the defect broke, and it is what pins it."""
    from no_human.core.orchestrator import Orchestrator

    stale = {"lifetime_tokens": 12_000_000, "attempt_tokens": 6_000_000}
    shapes = ({}, {"lifetime_tokens": 8_000_000, "attempt_tokens": 4_000_000},
              {"lifetime_tokens": 1_000_000, "attempt_tokens": 500_000},
              {"lifetime_tokens": 20_000_000}, {"attempt_tokens": 100_000})
    read = {"lifetime_tokens": lambda o, t: o._lifetime_limits(t)[1],
            "attempt_tokens": lambda o, t: o._attempt_token_cap(t)}
    assert set(read) == TOKEN_CAP_KEYS, "a cap key is missing from this sweep"

    for cfg_bounds in shapes:
        for written_key in TOKEN_CAP_KEYS:
            untouched, = TOKEN_CAP_KEYS - {written_key}
            o = Orchestrator.__new__(Orchestrator)
            o.bounds = Bounds.from_config(cfg_bounds)

            before = Task.new("before", repo_path="/tmp/x")
            before.config = dict(stale)
            was = read[untouched](o, before)

            after = Task.new("after", repo_path="/tmp/x")
            after.config = dict(stale)
            apply_action(after, {"set_task_config": {written_key: 3_000_000}},
                         bounds=o.bounds)

            assert read[untouched](o, after) == was, (
                f"bounds={cfg_bounds}: writing {written_key} moved the "
                f"enforced {untouched} from {was:,} to "
                f"{read[untouched](o, after):,}")


async def test_the_armed_mid_attempt_ceiling_is_in_the_weighted_unit(store):
    """MEDIUM-2: the third enforcement point — arming the sink's ceiling from
    the lifetime ledger — had no test at all, and the failure mode is severe
    and silent. `max(cap_life - used_life, 1)` with a RAW used_life against a
    weighted cap goes negative, clamps to a 1-token ceiling, and the attempt
    dies on its first usage event."""
    t = Task.new("armed", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 4_000_000, **_MARKED}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_LEDGER)   # 16,527,553 raw

    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds()
    o._sink = lambda e: None

    # Drives the PRODUCTION arming path. The first version of this test
    # recomputed `max(cap - used, 1)` itself and asserted on the result, so
    # swapping the weighted ledger read for the raw one inside the orchestrator
    # left it green — a test that recomputes the code under test proves
    # nothing, which is this repo's own recorded lesson.
    await o._arm_attempt_budget(t)

    # 4,000,000 - 3,386,434 = 613,566 of real budget left, NOT the one-token
    # clamp a raw read produces (4,000,000 - 16,527,553 < 0).
    assert o._token_ceiling == (t.id, 613_566, "the task's remaining lifetime budget")
    assert o._token_ceiling[1] > 1
    assert weighted_tokens(**(await store.lifetime_usage_by_class(t.id))[1]) == 3_386_434


async def test_a_frozen_budget_arms_the_per_attempt_cap_not_a_one_token_ceiling(store):
    """be71aad2 (2026-08-21, tasks 5bc9e2b4 / 2c8f23ff): a task PAST its
    lifetime cap whose review PASSED is resumed for a rebase round. The loop
    head says "frozen — allowed"; the mid-attempt arming computed
    `max(cap - used, 1)` regardless and handed the round a ONE-token ceiling,
    so its first usage event budget-aborted it — three dead attempts in two
    minutes, then a false escalation. RED on main: ceiling == (id, 1, ...)."""
    t = Task.new("frozen", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 4_000_000, "attempt_tokens": 2_500_000, **_MARKED}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_LEDGER)   # 16,527,553 raw — far past the cap

    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds()
    o._sink = lambda e: None

    async def _frozen(task):
        return True
    o._budget_frozen_by_pass = _frozen

    await o._arm_attempt_budget(t)

    assert o._token_ceiling == (t.id, 2_500_000, "the per-attempt cap"), o._token_ceiling


async def test_an_unfrozen_budget_still_arms_the_weighted_remainder(store):
    """Negative control for the freeze: with the gate NOT frozen, the arming
    is byte-for-byte what it was — the weighted remainder, clamped at 1."""
    t = Task.new("unfrozen", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 4_000_000, **_MARKED}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_LEDGER)

    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds()
    o._sink = lambda e: None

    async def _not_frozen(task):
        return False
    o._budget_frozen_by_pass = _not_frozen

    await o._arm_attempt_budget(t)
    assert o._token_ceiling == (t.id, 613_566, "the task's remaining lifetime budget")

    # And past the cap, unfrozen, the clamp still yields 1 — the loop head
    # is what parks that task; this arming never lies about headroom.
    t2 = Task.new("exhausted", repo_path="/tmp/x")
    t2.config = {"lifetime_tokens": 1_000_000, **_MARKED}
    await store.create_task(t2)
    aid2 = await store.create_attempt(t2.id, 1)
    await store.update_attempt(aid2, **D6E4B72A_LEDGER)
    await o._arm_attempt_budget(t2)
    assert o._token_ceiling[1] == 1


async def test_the_loop_head_gate_and_the_arming_cannot_disagree(store):
    """The two enforcement points read ONE predicate: whenever the loop head
    lets a round start under a frozen budget, the arming never hands that
    round a ceiling below its per-attempt cap — for any ledger state."""
    from no_human.core.orchestrator import Orchestrator

    for raw_used in (0, 3_000_000, 16_527_553):
        t = Task.new(f"agree-{raw_used}", repo_path="/tmp/x")
        t.config = {"lifetime_tokens": 4_000_000, "attempt_tokens": 2_000_000, **_MARKED}
        await store.create_task(t)
        aid = await store.create_attempt(t.id, 1)
        await store.update_attempt(aid, tokens_used=raw_used)
        o = Orchestrator.__new__(Orchestrator)
        o.store = store
        o.bounds = Bounds()
        o._sink = lambda e: None
        o.emit = lambda *a, **k: None

        async def _frozen(task):
            return True
        o._budget_frozen_by_pass = _frozen

        assert await o._check_lifetime_budget(t) is None      # loop head: allowed
        await o._arm_attempt_budget(t)
        assert o._token_ceiling[1] >= 2_000_000, (raw_used, o._token_ceiling)


async def test_a_partial_write_cannot_promote_the_key_it_did_not_touch(store):
    """A TWO-KEY stale config — the shape the earlier marker tests all missed
    by starting from `Task.new()`, whose config is empty, so nothing
    pre-existing could be mis-stamped.

    `budget_unit` describes the whole dict, so stamping it makes a claim about
    every token cap in it. The BUDGET_EXHAUSTED raise option writes
    `{lifetime_attempts, lifetime_tokens}` and never `attempt_tokens`, and 162
    tasks on this install carry both keys in raw units — so the partial write
    is the ORDINARY path, not a corner case.

    The invariant asserted here is the general one, not two literals: a write
    to one cap must not change what the OTHER cap means.
    """
    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    stale = {"lifetime_tokens": 12_000_000, "attempt_tokens": 6_000_000}

    # -- direction A: the blocker's raise option touches lifetime only -------
    t = Task.new("stale two-key", repo_path="/tmp/x")
    t.config = dict(stale)
    # Both convert (2,382,000 / 1,191,000) and both land under the ungranted
    # defaults, so both floor to them — R1's raise-floor, not a raw read.
    assert (o._lifetime_limits(t)[1], o._attempt_token_cap(t)) == (4_000_000, 2_000_000)

    apply_action(t, {"set_task_config": {"lifetime_attempts": 12,
                                         "lifetime_tokens": 5_100_000}})

    assert o._lifetime_limits(t)[1] == 5_100_000            # the key it wrote
    assert o._attempt_token_cap(t) == 2_000_000, (          # the key it did NOT
        "a lifetime raise silently promoted the untouched raw attempt_tokens "
        f"to {o._attempt_token_cap(t):,}; config={t.config}")
    assert t.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT
    # The consequence that actually bites: an inflated attempt cap at or above
    # the remaining lifetime budget makes the per-attempt brake inert and the
    # bounded loop degenerates to one attempt (the v6-taxonomy failure
    # `Bounds.attempt_tokens` exists to prevent).
    assert o._attempt_token_cap(t) < o._lifetime_limits(t)[1]

    # -- direction B: the human CLI touches attempt only ---------------------
    t2 = Task.new("stale two-key reverse", repo_path="/tmp/x")
    t2.config = dict(stale)
    apply_action(t2, {"set_task_config": {"attempt_tokens": 800_000}},
                 human_override=True)
    assert t2.config["attempt_tokens"] == 800_000
    assert o._lifetime_limits(t2)[1] == 4_000_000, (
        "an attempt-cap write silently promoted the untouched raw "
        f"lifetime_tokens to {o._lifetime_limits(t2)[1]:,}; config={t2.config}")

    # -- an ALREADY-marked config is never converted a second time ----------
    t3 = Task.new("already weighted", repo_path="/tmp/x")
    t3.config = {**stale, BUDGET_UNIT_KEY: WEIGHTED_UNIT}
    apply_action(t3, {"set_task_config": {"lifetime_tokens": 20_000_000}})
    assert t3.config["attempt_tokens"] == 6_000_000, "double-converted"
    assert o._attempt_token_cap(t3) == 6_000_000


def test_a_write_never_changes_what_an_untouched_cap_means():
    """The invariant above, swept over every single-key write and both marker
    states, so a future key added to TOKEN_CAP_KEYS is covered by construction
    rather than by someone remembering to add a case."""
    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    read = {"lifetime_tokens": lambda t: o._lifetime_limits(t)[1],
            "attempt_tokens": o._attempt_token_cap}
    assert set(read) == TOKEN_CAP_KEYS, "a cap key is missing from this sweep"

    base = {"lifetime_tokens": 12_000_000, "attempt_tokens": 6_000_000}
    for marked in (False, True):
        for written in TOKEN_CAP_KEYS:
            for override in (False, True):
                cfg = dict(base)
                if marked:
                    cfg[BUDGET_UNIT_KEY] = WEIGHTED_UNIT
                t = Task.new("sweep", repo_path="/tmp/x")
                t.config = cfg
                untouched = next(iter(TOKEN_CAP_KEYS - {written}))
                before = read[untouched](t)
                apply_action(t, {"set_task_config": {written: 3_000_000}},
                             human_override=override)
                after = read[untouched](t)
                assert before == after, (
                    f"writing {written} changed {untouched}: {before:,} -> "
                    f"{after:,} (marked={marked}, override={override})")


# --- Attempt-startup floor: refuse to START an attempt the remaining ------
# --- lifetime budget cannot pay for, instead of starting it and ------------
# --- budget-aborting at turn 0 (INCIDENT 2026-08-20, run 123dea00). --------
#
# `_check_attempt_startup_floor` runs at the loop head immediately AFTER
# `_check_lifetime_budget` — the task is provably UNDER both lifetime caps
# already; this asks the narrower question of whether what remains can even
# survive the cheapest attempt's fixed startup cost.


async def _burn_event(attempt_number, *, read=0, write=0, fresh=0, ts=1):
    """A persisted `cache_burn` event shaped exactly like the live emission
    at orchestrator.py's `_CACHE_BURN_EVERY` cadence — flat fields, NOT
    nested under `meta`."""
    return {
        "source": "orchestrator", "kind": "cache_burn", "text": "",
        "attempt_number": attempt_number, "ts": ts,
        "tokens_used": fresh, "cache_read": read, "cache_creation": write,
        "total": fresh + read + write, "compactions": 0,
    }


async def test_an_attempt_the_remaining_budget_cannot_pay_for_never_starts(store):
    """AC1 / repro: remaining weighted budget 150,000, prior attempt startup
    cost 160,000 -> the gate refuses, naming both numbers, and the loop-head
    contract (`_drive` emits `attempt_start` only when this returns None) is
    upheld directly: a caller that only emits on None never gets there.

    RED on unfixed code: `_check_attempt_startup_floor` does not exist.
    """
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    # 1,500,000 cache-read raw = 150,000 weighted (0.1x) already spent.
    await store.update_attempt(aid, status="succeeded", cache_read_tokens=1_500_000)
    await store.save_events(t.id, [await _burn_event(1, read=1_600_000)])  # 160,000 weighted

    events = []
    o = _orch(store, {"lifetime_tokens": 300_000})  # remaining = 300,000 - 150,000 = 150,000
    o._sink = events.append

    assert await o._check_lifetime_budget(t) is None  # under the cap — the floor is the new question
    b = await o._check_attempt_startup_floor(t)
    assert b is not None
    assert b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "150,000" in b.root_cause_hypothesis
    assert "160,000" in b.root_cause_hypothesis

    ev = next(e for e in events if e["kind"] == "lifetime_budget" and e.get("floor_refusal"))
    assert ev["tokens_remaining"] == 150_000
    assert ev["min_viable_attempt"] == 160_000

    # The loop-head half of this contract — that `_drive` never reaches
    # `attempt_start` when this returns non-None — is proven for real (a live
    # `_drive`/`run_task`, not a hand-simulated emit) by
    # test_e2e_orchestrator.py::test_the_loop_head_refuses_to_start_an_attempt_it_cannot_afford.


async def test_the_floor_blockers_suggested_raise_actually_clears_the_floor(store):
    """Review finding (attempt 2): the shared `raise_to['lifetime_tokens']`
    formula (`ceil(used_tokens * 1.5 / 100k) * 100k`) was written for the
    OVER-CAP caller, where `used_tokens >= cap_tokens` by construction, so
    1.5x always clears the cap. The floor caller is different — it fires
    while STILL under the cap — and with this exact scenario (cap=300,000,
    used=150,000, floor=160,000) the naive 1.5x formula suggests 300,000,
    i.e. NO CHANGE: a human who applies it and retries gets `remaining =
    300,000 - 150,000 = 150,000 < 160,000` again — instant re-park. The
    fix must offer a raise that, once applied, leaves `remaining >= floor`.
    """
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, status="succeeded", cache_read_tokens=1_500_000)
    await store.save_events(t.id, [await _burn_event(1, read=1_600_000)])  # 160,000 weighted

    # ASK_THE_HUMAN (non-terminal) so the raise-budget option is populated —
    # under the default terminal mode there is no option to inspect at all,
    # only `wake_condition`, which is built from the same `raise_to` dict.
    o = _orch(store, {"lifetime_tokens": 300_000}, config=ASK_THE_HUMAN)
    b = await o._check_attempt_startup_floor(t)
    assert b is not None
    assert b.options, "expected a raise-budget option (non-terminal mode)"
    raise_option = next(
        opt for opt in b.options
        if opt.action and "set_task_config" in opt.action
    )
    suggested = raise_option.action["set_task_config"]["lifetime_tokens"]
    used_tokens = 150_000
    floor = 160_000
    assert suggested > 300_000, (
        f"suggested raise {suggested:,} is a no-op against the current cap "
        f"300,000 — applying it and retrying would re-park immediately")
    assert suggested - used_tokens >= floor, (
        f"applying the suggested raise ({suggested:,}) still leaves "
        f"remaining ({suggested - used_tokens:,}) below the floor "
        f"({floor:,}) — retrying would re-park immediately")


async def test_remaining_above_the_floor_still_starts_the_attempt(store):
    """AC2a: the same history, but remaining 400,000 >= floor 160,000 ->
    unaffected, exactly as today."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, status="succeeded", cache_read_tokens=1_500_000)
    await store.save_events(t.id, [await _burn_event(1, read=1_600_000)])

    o = _orch(store, {"lifetime_tokens": 550_000})  # remaining = 550,000 - 150,000 = 400,000
    assert await o._check_attempt_startup_floor(t) is None


async def test_a_review_pass_freeze_is_exempt_from_the_startup_floor(store):
    """AC2b: a task parked in AWAITING_APPROVAL with a PASS on record is
    frozen by `_budget_frozen_by_pass` before the floor is even computed —
    same exemption `_check_lifetime_budget` gets, same reason (INCIDENT
    2026-08-13)."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, status="succeeded", tokens_used=1_000,
                               review_passed=1)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    fresh = await store.get_task(t.id)

    # A cap so tight the floor would fire on anything but a frozen task.
    o = _orch(store, {"lifetime_tokens": 1})
    assert await o._check_attempt_startup_floor(fresh) is None


async def test_a_mechanical_round_is_exempt_from_the_startup_floor(store):
    """AC2b: a mechanical round (pr_conflict rebase / re-verification tick)
    is exempt the same way `_check_lifetime_budget` is."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    o = _orch(store, {"lifetime_tokens": 1})

    async def _mechanical(_task):
        return True

    o._mechanical_round = _mechanical
    assert await o._check_attempt_startup_floor(t) is None


async def test_the_floor_uses_the_cheapest_measured_startup(store):
    """When more than one attempt reached 10 messages, the floor is the
    CHEAPEST of them — the question is "can the next attempt survive", and
    the cheapest prior attempt is the most optimistic honest answer."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        await _burn_event(1, read=3_000_000, ts=1),  # 300,000 weighted
        await _burn_event(2, read=1_000_000, ts=2),  # 100,000 weighted — cheaper
    ])

    o = _orch(store)
    floor, provenance = await o._min_viable_attempt_cost(t)
    assert floor == 100_000
    assert "attempt #2" in provenance


async def test_an_attempt_that_never_reached_ten_messages_is_excluded(store):
    """A crashed attempt (never reached message 10) emits no `cache_burn` at
    all — excluded structurally, no extra status filter, per the resolved
    intake answer."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        {"source": "orchestrator", "kind": "attempt_start", "text": "",
         "attempt_number": 1, "ts": 1},  # crashed before message 10
        await _burn_event(2, read=5_000_000, ts=2),  # 500,000 weighted
    ])

    o = _orch(store)
    floor, provenance = await o._min_viable_attempt_cost(t)
    assert floor == 500_000
    assert "attempt #2" in provenance


async def test_with_no_history_the_config_floor_applies(store):
    """No attempt ever reached 10 messages (or none exist) -> the config
    floor (`bounds.min_viable_attempt_weighted_tokens`) applies."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    o = _orch(store)
    floor, provenance = await o._min_viable_attempt_cost(t)
    assert floor == Bounds().min_viable_attempt_weighted_tokens == 250_000
    assert "config floor" in provenance


async def test_the_floor_refusal_reads_differently_from_the_over_cap_refusal(store):
    """AC3: a human reading a parked task must be able to tell the two
    BUDGET_EXHAUSTED reasons apart — the over-cap gate says "exhausted:
    ...", the floor gate says "floor: remaining ... < minimum viable
    attempt ..." — never the same string."""
    t1 = Task.new("over-cap", repo_path="/tmp/x")
    await store.create_task(t1)
    await _spend(store, t1.id, attempts=9, tokens_each=100)
    over_blocker = await _orch(store)._check_lifetime_budget(t1)
    assert over_blocker is not None
    assert "floor" not in over_blocker.root_cause_hypothesis

    t2 = Task.new("floor", repo_path="/tmp/x")
    await store.create_task(t2)
    aid = await store.create_attempt(t2.id, 1)
    await store.update_attempt(aid, status="succeeded", cache_read_tokens=1_500_000)
    await store.save_events(t2.id, [await _burn_event(1, read=1_600_000)])
    floor_blocker = await _orch(
        store, {"lifetime_tokens": 300_000}
    )._check_attempt_startup_floor(t2)
    assert floor_blocker is not None
    assert "floor" in floor_blocker.root_cause_hypothesis
    assert "minimum viable attempt" in floor_blocker.root_cause_hypothesis
    assert floor_blocker.root_cause_hypothesis != over_blocker.root_cause_hypothesis


# --------------------------------------------------------------------------- #
# The orphaned-ledger scope statement + residual: the gate's own totals are
# FROM attempts only, so a BUDGET_EXHAUSTED blocker's evidence must say so and
# name this task's spend in the unattributed ledger (site prefix
# ORPHANED_SITE_PREFIX) alongside it — never counted against the cap, never
# silently omitted either.
# --------------------------------------------------------------------------- #

async def _orphan(store, task_id, *, tier="utility_", **classes):
    """Book one orphaned aux-tier row on ``task_id`` — the shape
    `Orchestrator._flush_orphaned_aux_usage` writes when no attempt claims
    aux spend: `site="orphaned_<tier>usage"`, this task's id."""
    return await store.record_unattributed_usage(
        site=f"orphaned_{tier}usage", task_id=task_id, **classes)


async def test_budget_blocker_states_it_excludes_the_orphaned_ledger(store):
    """AC1: the scope sentence must appear even with ZERO orphaned rows — a
    sentence that only shows up when non-zero would teach a human to read
    its absence as "there is none"."""
    t = Task.new("task", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 1_000_000, BUDGET_UNIT_KEY: WEIGHTED_UNIT}
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=1_000_000)  # 1.1M weighted > 1.0M cap

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert "sum over this task's ATTEMPT rows only" in b.evidence
    assert "orphaned_" in b.evidence
    assert "unattributed ledger" in b.evidence.lower()
    assert "0 calls, 0 cost-weighted tokens" in b.evidence


async def test_budget_blocker_reports_this_tasks_orphaned_residual(store):
    """AC2: two orphaned rows on this task, one on a different task, and one
    non-orphaned INTAKE row (site "api.grill", no "orphaned_" prefix) on
    this same task — the blocker must name `2` calls and THIS task's
    orphaned-only weighted total; neither the other task's spend nor this
    task's own intake-site spend may leak in (the intake row is what
    catches a dropped `attributed=True` filter — without it, intake sites
    on the same task_id would count too)."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)
    other = Task.new("other", repo_path="/tmp/x")
    await store.create_task(other)

    await _orphan(store, t.id, tokens_used=100_000)
    await _orphan(store, t.id, cache_read_tokens=200_000)
    await _orphan(store, other.id, tokens_used=9_000_000)
    await store.record_unattributed_usage(
        site="api.grill", task_id=t.id, tokens_used=7_000_000)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    expected = weighted_tokens(
        tokens_used=100_000, cache_read_tokens=200_000,
        cache_creation_tokens=0)
    assert f"2 calls, {expected:,} cost-weighted tokens" in b.evidence
    assert "9,000,000" not in b.evidence, (
        "the other task's orphaned spend must not appear in this blocker")
    assert "7,000,000" not in b.evidence, (
        "this task's own non-orphaned intake-site spend must not appear "
        "in the orphaned-ledger residual")


async def test_orphaned_residual_is_weighted_not_raw(store):
    """AC3: a row dominated by cache-read tokens must be priced with
    `pricing.weighted_tokens`, not summed raw — raw is ~4x weighted here and
    must never be presented as the residual."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)

    await _orphan(
        store, t.id, tokens_used=100_000, cache_read_tokens=1_000_000,
        cache_creation_tokens=100_000)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    expected = weighted_tokens(
        tokens_used=100_000, cache_read_tokens=1_000_000,
        cache_creation_tokens=100_000)
    assert f"{expected:,}" in b.evidence
    assert "1,200,000" not in b.evidence, (
        "the raw sum of the three classes must never be presented as the "
        "residual")


async def test_startup_floor_blocker_also_states_scope_and_residual(store):
    """AC1/AC2 for the SECOND caller: `_check_attempt_startup_floor` shares
    `_budget_exhausted_blocker`'s body, so the same scope sentence and
    residual must appear there too — this is what makes the shared-body
    decision testable."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, status="succeeded", cache_read_tokens=1_500_000)
    await store.save_events(t.id, [await _burn_event(1, read=1_600_000)])
    await _orphan(store, t.id, tokens_used=50_000)

    o = _orch(store, {"lifetime_tokens": 300_000})
    assert await o._check_lifetime_budget(t) is None
    b = await o._check_attempt_startup_floor(t)
    assert b is not None
    assert "sum over this task's ATTEMPT rows only" in b.evidence
    assert "orphaned_" in b.evidence
    expected = weighted_tokens(tokens_used=50_000)
    assert f"1 calls, {expected:,} cost-weighted tokens" in b.evidence


async def test_orphaned_spend_does_not_move_the_cap(store):
    """AC4: no policy change. A task UNDER cap with a large orphaned
    residual (3M weighted) must still return None, and the `lifetime_budget`
    event's `tokens_used`/`tokens_weighted` must be unchanged from the
    identical no-orphan case. The raise option's `lifetime_tokens` must also
    be identical with and without the residual, for a task that IS over
    cap."""
    t_plain = Task.new("plain", repo_path="/tmp/x")
    await store.create_task(t_plain)
    await _spend(store, t_plain.id, attempts=2, tokens_each=1_000_000)

    t_orphaned = Task.new("orphaned", repo_path="/tmp/x")
    await store.create_task(t_orphaned)
    await _spend(store, t_orphaned.id, attempts=2, tokens_each=1_000_000)
    await _orphan(store, t_orphaned.id, tokens_used=3_000_000)  # 3M weighted

    events_plain, events_orphaned = [], []
    o_plain = _orch(store)
    o_plain._sink = events_plain.append
    o_orphaned = _orch(store)
    o_orphaned._sink = events_orphaned.append

    assert await o_plain._check_lifetime_budget(t_plain) is None
    assert await o_orphaned._check_lifetime_budget(t_orphaned) is None

    ev_plain = next(e for e in events_plain if e["kind"] == "lifetime_budget")
    ev_orphaned = next(
        e for e in events_orphaned if e["kind"] == "lifetime_budget")
    assert ev_plain["tokens_used"] == ev_orphaned["tokens_used"]
    assert ev_plain["tokens_weighted"] == ev_orphaned["tokens_weighted"]

    # Same check for the raise option, on a task that IS over cap.
    t_over_a = Task.new("over-a", repo_path="/tmp/x")
    await store.create_task(t_over_a)
    await _spend(store, t_over_a.id, attempts=9, tokens_each=100)
    b_no_orphan = await _orch(
        store, config=ASK_THE_HUMAN)._check_lifetime_budget(t_over_a)

    t_over_b = Task.new("over-b", repo_path="/tmp/x")
    await store.create_task(t_over_b)
    await _spend(store, t_over_b.id, attempts=9, tokens_each=100)
    await _orphan(store, t_over_b.id, tokens_used=3_000_000)
    b_with_orphan = await _orch(
        store, config=ASK_THE_HUMAN)._check_lifetime_budget(t_over_b)

    def _raise_tokens(b):
        opt = next(o for o in b.options if o.action and "set_task_config" in o.action)
        return opt.action["set_task_config"]["lifetime_tokens"]

    assert _raise_tokens(b_no_orphan) == _raise_tokens(b_with_orphan)


async def test_a_failing_ledger_read_never_blocks_the_gate(store, monkeypatch):
    """Robustness: a broken ledger read must never suppress the
    BUDGET_EXHAUSTED blocker — the accounting failure degrades the evidence,
    not the gate."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)

    async def _boom(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(store, "unattributed_usage_totals", _boom)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "unavailable" in b.evidence


async def test_an_aged_out_orphaned_row_still_reads_as_a_qualified_zero(store):
    """AGED-OUT shape: a real orphaned row is booked on this task, then rolls
    past the retention window — `compact_unattributed_usage` re-inserts the
    group with `task_id = NULL` (a group can span many tasks), so the
    task-scoped read goes from `(1, weighted)` to `(0, 0)`. The blocker must
    still say so is qualified, not print a bare zero that reads as "there was
    never any spend"."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)
    row_id = await _orphan(store, t.id, tokens_used=2_000_000)

    before = await store.unattributed_usage_totals(t.id, attributed=True)
    assert before["calls"] == 1
    assert before["tokens_used"] == 2_000_000

    await store.db.execute(
        "UPDATE unattributed_usage SET ts = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", row_id))
    await store.db.commit()
    collapsed = await store.compact_unattributed_usage(retention_days=1)
    assert collapsed == 1

    after = await store.unattributed_usage_totals(t.id, attributed=True)
    assert after == {
        "calls": 0, "tokens_used": 0, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "total": 0,
    }, "the roll-up must drop task_id, taking this row off the task-scoped read"

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert (
        "0 calls, 0 cost-weighted tokens "
        "(aged-out or not-yet-flushed spend is not shown)"
    ) in b.evidence


async def test_unflushed_aux_spend_is_not_yet_in_the_qualified_residual(store):
    """NOT-YET-FLUSHED shape: `_flush_orphaned_aux_usage` runs in
    `_drive_watched`'s `finally`, AFTER `_check_lifetime_budget` builds its
    blocker — so a task can be sitting on real, unflushed aux-tier spend (in
    the orchestrator's in-memory `_<tier>usage` accumulator, drained by
    `_pop_aux_usage`) while the ledger read still returns zero. The blocker
    must carry the same qualifier for that zero, not present it as "this task
    has no orphaned spend"."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)

    o = _orch(store)
    # Real pending spend that has not been flushed to `unattributed_usage`
    # yet — mirrors what `_note_tier_usage("utility_", ...)` leaves behind
    # mid-attempt, before `_pop_aux_usage`/`_flush_orphaned_aux_usage` drains it.
    o._utility_usage = {
        "tokens_used": 3_000_000, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "output_tokens": None,
    }

    b = await o._check_lifetime_budget(t)
    assert b is not None
    assert (
        "0 calls, 0 cost-weighted tokens "
        "(aged-out or not-yet-flushed spend is not shown)"
    ) in b.evidence


# --------------------------------------------------------------------------- #
# f22495d8: the detail payload surfaces the gate's budget (used/cap/remaining) #
# --------------------------------------------------------------------------- #
async def _detail(store, tmp_path, task_id):
    """GET /api/tasks/{id} against `store`, returning the JSON body."""
    import no_human.config as nh_config
    from httpx import ASGITransport, AsyncClient

    from no_human.api.app import app

    app.state.store = store
    app.state.config = nh_config.load_config(tmp_path / "config.yaml")
    app.state.scheduler = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        r = await c.get(f"/api/tasks/{task_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def test_detail_payload_budget_matches_the_gate_killer_metric(store, tmp_path):
    """The drawer's `budget` is the EXACT number BUDGET_EXHAUSTED kills on.

    A cache-read-heavy attempt: 1M fresh (weight 1.0) + 10M cache-read (weight
    0.1) = 2M cost-weighted, against 11M RAW. The gate compares the WEIGHTED
    figure to the cap, so the surfaced `used` must be 2M — the raw 11M the old
    meter showed would misreport how close this task is to being killed.
    """
    t = Task.new("burn", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, tokens_used=1_000_000, cache_read_tokens=10_000_000)

    # What the gate itself would compute for this task, from the same helpers.
    _, by_class, _ = await store.lifetime_usage_by_class(t.id)
    gate_used = weighted_tokens(**by_class)
    _, gate_cap = _orch(store)._lifetime_limits(t)

    body = await _detail(store, tmp_path, t.id)
    assert "budget" in body and body["budget"] is not None
    budget = body["budget"]
    assert budget["used"] == gate_used == 2_000_000
    assert budget["cap"] == gate_cap == 4_000_000  # default bounds.lifetime_tokens
    assert budget["remaining"] == gate_cap - gate_used == 2_000_000
    # It is the WEIGHTED killer metric, not the raw 11M every other meter shows.
    raw = sum(by_class[n] for n in ("tokens_used", "cache_read_tokens",
                                    "cache_creation_tokens"))
    assert raw == 11_000_000 and budget["used"] != raw


async def test_detail_payload_budget_honours_a_per_task_cap_override(store, tmp_path):
    """The surfaced cap tracks the SAME per-task override the gate honours —
    a human raise (`_stored_token_cap`, weighted-stamped) moves both together,
    so the drawer never shows a cap the gate is not using."""
    t = Task.new("burn", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 9_000_000, BUDGET_UNIT_KEY: WEIGHTED_UNIT}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, tokens_used=1_000_000)

    _, gate_cap = _orch(store)._lifetime_limits(t)
    body = await _detail(store, tmp_path, t.id)
    assert gate_cap == 9_000_000
    assert body["budget"]["cap"] == gate_cap
    assert body["budget"]["remaining"] == 9_000_000 - 1_000_000


# --------------------------------------------------------------------------- #
# The Codex fallback bug: an unrecorded model on the codex backend must price #
# at the MAX OpenAI premium, never the plain 4.0 that (before this fix) sat   #
# below several priced OpenAI rows and bought a Codex task budget headroom    #
# for simply failing to record its model.                                    #
# --------------------------------------------------------------------------- #

async def test_a_codex_task_prices_an_unrecorded_model_at_the_codex_fallback(store):
    """A task whose `task.config["backend"] == "codex"` must route its
    unrecorded-model output tokens through the raised Codex fallback — the
    same figure `_check_lifetime_budget` reports on the `lifetime_budget`
    event, since neither this gate nor the ledger it reads ever thread a
    per-role `model` into `_weighted_tokens`/`_class_breakdown` (six roles are
    summed per attempt; see `weighted_tokens`'s docstring on why `model` stays
    `None` at every one of these call sites) — every attempt here IS the
    unrecorded-model case, not a corner of it.

    RED before this fix: the gate priced this exact spend at the plain 4.0
    regardless of `task.config["backend"]`, silently under-charging every
    Codex task's unrecorded output tokens relative to what the priced OpenAI
    rows say they actually cost.
    """
    from no_human.core.pricing import OUTPUT_EXTRA_WEIGHT, fallback_output_extra_weight

    premium = fallback_output_extra_weight(backend="codex")
    assert premium > OUTPUT_EXTRA_WEIGHT, (
        "test assumption broken: the codex fallback is no longer raised")

    t = Task.new("codex task", repo_path="/tmp/x")
    t.config = {
        "backend": "codex",
        "lifetime_tokens": 1_000_000_000,
        BUDGET_UNIT_KEY: WEIGHTED_UNIT,
    }
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, tokens_used=1_000_000, output_tokens=1_000_000)

    events = []
    o = _orch(store)
    o._sink = events.append
    assert await o._check_lifetime_budget(t) is None
    ev = next(e for e in events if e["kind"] == "lifetime_budget")

    expected = int(1_000_000 * 1.0 + 1_000_000 * premium)
    assert ev["tokens_weighted"] == expected
    # And strictly more than the plain-fallback figure a pre-fix gate reported.
    assert ev["tokens_weighted"] > int(1_000_000 + 1_000_000 * OUTPUT_EXTRA_WEIGHT)


async def test_a_claude_task_is_unchanged(store):
    """A task with no backend override — the default, and an explicit
    `"claude"` — must price identically to before this change: the plain 4.0
    fallback, never the raised Codex one. Pinned as a regression: this fix
    must not move a single number for the Claude path."""
    t = Task.new("claude task (no override)", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 1_000_000_000, BUDGET_UNIT_KEY: WEIGHTED_UNIT}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, tokens_used=1_000_000, output_tokens=1_000_000)

    events = []
    o = _orch(store)
    o._sink = events.append
    assert await o._check_lifetime_budget(t) is None
    ev = next(e for e in events if e["kind"] == "lifetime_budget")
    assert ev["tokens_weighted"] == weighted_tokens(
        tokens_used=1_000_000, output_tokens=1_000_000)  # == 5,000,000, unchanged

    # Explicit "claude" reads identically to no override at all.
    t2 = Task.new("claude task (explicit)", repo_path="/tmp/x")
    t2.config = {
        "backend": "claude", "lifetime_tokens": 1_000_000_000,
        BUDGET_UNIT_KEY: WEIGHTED_UNIT,
    }
    await store.create_task(t2)
    aid2 = await store.create_attempt(t2.id, 1)
    await store.update_attempt(aid2, tokens_used=1_000_000, output_tokens=1_000_000)
    events2 = []
    o2 = _orch(store)
    o2._sink = events2.append
    assert await o2._check_lifetime_budget(t2) is None
    ev2 = next(e for e in events2 if e["kind"] == "lifetime_budget")
    assert ev2["tokens_weighted"] == ev["tokens_weighted"]


async def test_the_orchestrator_backend_reader_agrees_with_task_backend_override(store):
    """`Orchestrator._task_backend` must exactly mirror
    `core.runtime.task_backend_override` when a task carries a per-task
    override, and fall through to the worker-config default
    (`agent.backend.resolve_backend_name`, role="coder") only when it does
    not — the same two-step resolution the running attempt actually uses to
    pick a coding backend, so the gate can never price a task under a
    different backend than the one that spent the tokens."""
    from no_human.agent.backend import resolve_backend_name
    from no_human.core.runtime import task_backend_override

    o = _orch(store, config={"worker": {"backend": "local"}})

    t = Task.new("with override", repo_path="/tmp/x")
    t.config = {"backend": "codex"}
    await store.create_task(t)
    assert task_backend_override(t) == "codex"
    assert o._task_backend(t) == "codex"

    t2 = Task.new("no override", repo_path="/tmp/x")
    t2.config = {}
    await store.create_task(t2)
    assert task_backend_override(t2) is None
    assert o._task_backend(t2) == "local" == resolve_backend_name(o.config, role="coder")

    t3 = Task.new("blank override", repo_path="/tmp/x")
    t3.config = {"backend": "   "}
    await store.create_task(t3)
    assert task_backend_override(t3) is None
    assert o._task_backend(t3) == "local"


async def test_detail_payload_budget_uses_the_codex_fallback_for_a_codex_task(store, tmp_path):
    """`api/app.py`'s `get_task` must thread the SAME backend the gate resolves
    into its own `weighted_tokens` call — the surfaced `budget.used` a human
    reads in the drawer must equal what `BUDGET_EXHAUSTED` would actually kill
    on, not a cheaper number computed with the plain fallback."""
    from no_human.core.pricing import fallback_output_extra_weight

    t = Task.new("codex burn", repo_path="/tmp/x")
    t.config = {
        "backend": "codex",
        "lifetime_tokens": 1_000_000_000,
        BUDGET_UNIT_KEY: WEIGHTED_UNIT,
    }
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, tokens_used=1_000_000, output_tokens=1_000_000)

    _, by_class, _ = await store.lifetime_usage_by_class(t.id)
    gate_used = weighted_tokens(**by_class, backend="codex")

    body = await _detail(store, tmp_path, t.id)
    assert body["budget"]["used"] == gate_used
    assert gate_used == int(1_000_000 + 1_000_000 * fallback_output_extra_weight(backend="codex"))
