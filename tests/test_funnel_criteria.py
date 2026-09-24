"""Funnel-regression pass/fail criteria (Phase C, task C1).

The gate is MEASURED, never model-judged: `quality` comes from a held-out test
run and nothing else (constraints #3/#4). These tests pin the three properties
that make the verdict readable when a night goes red — a named criterion, the
actual numbers, and independence between the ceilings.
"""

from no_human.eval.funnel_criteria import FunnelCriteria, evaluate

# A record that satisfies every criterion; each test spoils exactly one field,
# so a failure names the field the test spoiled and nothing else.
GREEN = {
    "task": "t2_small_fix",
    "pr_url": "file:///tmp/bare.git#nh/t2",
    "review_passed": True,
    "holdout": True,
    "weighted_tokens": 900_000,
    "wall_seconds": 700.0,
}

CRIT = FunnelCriteria(
    pr_opened=True, review_passed=True, holdout_green=True,
    max_weighted_tokens=1_500_000, max_wall_seconds=1_800,
)


def test_a_green_record_passes_with_measured_quality():
    v = evaluate(GREEN, CRIT)
    assert v.passed is True, v.failures
    assert v.failures == []
    assert v.quality == "holdout_green"
    assert v.cost == 900_000
    assert v.wall == 700.0


def test_a_record_missing_its_pr_fails_naming_pr_opened():
    v = evaluate({**GREEN, "pr_url": ""}, CRIT)
    assert v.passed is False
    assert len(v.failures) == 1, v.failures
    assert v.failures[0].startswith("pr_opened:"), v.failures


def test_the_cost_ceiling_fails_on_its_own_and_names_the_numbers():
    v = evaluate({**GREEN, "weighted_tokens": 5_000_000}, CRIT)
    assert v.passed is False
    assert len(v.failures) == 1, "the time ceiling must not fire too"
    assert v.failures[0].startswith("max_weighted_tokens:")
    assert "5,000,000" in v.failures[0], v.failures[0]
    assert "1,500,000" in v.failures[0], v.failures[0]


def test_the_time_ceiling_fails_on_its_own_and_names_the_numbers():
    v = evaluate({**GREEN, "wall_seconds": 2_400.0}, CRIT)
    assert v.passed is False
    assert len(v.failures) == 1, "the cost ceiling must not fire too"
    assert v.failures[0].startswith("max_wall_seconds:")
    assert "2,400" in v.failures[0], v.failures[0]
    assert "1,800" in v.failures[0], v.failures[0]


def test_a_red_holdout_is_a_measured_quality_and_a_failure():
    v = evaluate({**GREEN, "holdout": False}, CRIT)
    assert v.quality == "holdout_red"
    assert v.passed is False
    assert v.failures[0].startswith("holdout_green:")


def test_a_tier_with_no_holdout_reports_no_holdout_and_is_not_failed_for_it():
    """t1 (docs) has nothing to hold out. A tier that never had a held-out test
    must not be scored as if its holdout went red — that would make the docs
    tier permanently unpassable."""
    crit = FunnelCriteria(pr_opened=True, review_passed=True,
                          holdout_green=False, max_weighted_tokens=400_000,
                          max_wall_seconds=900)
    v = evaluate({**GREEN, "holdout": None, "weighted_tokens": 100_000,
                  "wall_seconds": 60.0}, crit)
    assert v.quality == "no_holdout"
    assert v.passed is True, v.failures


def test_a_holdout_demanded_but_never_run_fails_closed():
    """`holdout_green: true` with no measurement is not a pass. Fail-closed is
    the whole point of measuring instead of judging."""
    v = evaluate({**GREEN, "holdout": None}, CRIT)
    assert v.quality == "no_holdout"
    assert v.passed is False
    assert v.failures[0].startswith("holdout_green:")


def test_every_broken_criterion_is_reported_not_just_the_first():
    v = evaluate({"task": "t9", "pr_url": "", "review_passed": False,
                  "holdout": False, "weighted_tokens": 9_000_000,
                  "wall_seconds": 9_999.0}, CRIT)
    assert [f.split(":")[0] for f in v.failures] == [
        "pr_opened", "review_passed", "holdout_green",
        "max_weighted_tokens", "max_wall_seconds"]


def test_cost_is_priced_from_the_token_classes_when_no_total_is_recorded():
    """The record shape the runner writes is the ledger's own column names, so
    an unweighted record is priced with `core.pricing.weighted_tokens` — the
    same arithmetic every budget cap uses — never re-derived here."""
    v = evaluate({**GREEN, "weighted_tokens": None, "tokens_used": 1000,
                  "cache_read_tokens": 100_000, "cache_creation_tokens": 8000},
                 CRIT)
    assert v.cost == 1000 + int(0.1 * 100_000) + int(1.25 * 8000)


def test_the_fallback_prices_the_output_premium_too():
    """The token-class fallback must price the output SHARE of `tokens_used`
    at its own (higher) weight too — a record with no output split at all is
    exactly the shape issue #425's `weighted_tokens: null` records had."""
    without_output = evaluate({**GREEN, "weighted_tokens": None,
                               "tokens_used": 1000}, CRIT)
    with_output = evaluate({**GREEN, "weighted_tokens": None,
                            "tokens_used": 1000, "output_tokens": 200}, CRIT)
    assert without_output.cost == 1000
    assert with_output.cost == 1000 + 200 * 4
    assert with_output.cost - without_output.cost == 800


def test_criteria_load_from_the_tier_json():
    crit = FunnelCriteria.from_dict({
        "pr_opened": True, "review_passed": True, "holdout_green": True,
        "max_weighted_tokens": 3_000_000, "max_wall_seconds": 2700,
    })
    assert crit == FunnelCriteria(True, True, True, 3_000_000, 2700)
