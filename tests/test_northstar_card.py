"""Tests for the north-star scorecard, gate, and report (Task A4)."""

from __future__ import annotations

import pytest

from no_human.eval.northstar import BenchScore
from no_human.eval.northstar_card import (
    NorthStarCard,
    cost_ratio_basis,
    corpus_shortfall,
    northstar_gate,
    pin_rederivation_note,
    render_northstar_md,
    unmeasured_specs,
)


def _score(*, satisfied=True, nh=500, orig=1000, corrections=3,
           status="awaiting_approval", expected_escalation=False,
           task_id="ns-1", nh_cache_tokens=100,
           nh_cache_creation_tokens=20) -> BenchScore:
    return BenchScore(
        task_id=task_id, title="t", outcome_status=status,
        goal_satisfied=satisfied, escalated_honestly=False, mergeable=True,
        nh_tokens=nh, nh_cache_tokens=nh_cache_tokens,
        nh_cache_creation_tokens=nh_cache_creation_tokens, nh_turns=5,
        nh_wall_clock_s=60.0,
        orig_tokens=orig, orig_cache_tokens=0,
        orig_cache_creation_tokens=0, orig_wall_clock_s=600.0,
        orig_corrections=corrections,
        expected_escalation=expected_escalation)


def test_card_aggregates():
    card = NorthStarCard(scores=[
        _score(task_id="a", satisfied=True, nh=500, orig=1000, corrections=3),
        _score(task_id="b", satisfied=False, nh=2000, orig=1000, corrections=5),
        _score(task_id="c", status="skipped", satisfied=None, nh=0),
    ])
    assert card.total == 3 and card.skipped == 1
    assert card.satisfied == 1
    assert card.success_rate == 0.5
    # ratios: 0.5 and 2.0 → median 1.25
    assert card.median_token_ratio == 1.25
    # only satisfied tasks count corrections avoided
    assert card.corrections_avoided == 3


def test_honest_escalation_rate_uses_explicit_field():
    card = NorthStarCard(scores=[
        _score(task_id="a", expected_escalation=True, satisfied=True,
               status="escalated"),
        _score(task_id="b", expected_escalation=True, satisfied=False,
               status="awaiting_approval"),   # faked a PR — not honest
        _score(task_id="c", satisfied=True),
    ])
    assert card.honest_escalation_rate == 0.5
    assert NorthStarCard(scores=[_score()]).honest_escalation_rate == 1.0


def test_median_cost_ratio_excludes_a_zero_spend_spec():
    """A spec where no_human spent nothing (crashed/skipped/escalated before
    any model call) has cost_ratio == 0.0 — a non-result, not a cost win —
    and must not drag the median down. Real BenchScore objects (not a stub
    handed cost_ratio directly): `dead` gets its 0.0 from the actual
    property, computed from zeroed nh_* fields. Mutation check: dropping the
    `priced_scores` exclusion (i.e. going back to filtering on `cost_ratio is
    not None` alone) makes this assert `0.045`, not `0.09`, and this test
    fails by name."""
    dead = _score(task_id="dead", nh=0, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0, orig=1000)
    real = _score(task_id="real", nh=45000, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0, orig=500000)
    assert dead.cost_ratio == 0.0
    assert real.cost_ratio == 0.09
    card = NorthStarCard(scores=[dead, real])
    assert card.median_cost_ratio == 0.09
    assert card.median_cost_ratio != (dead.cost_ratio + real.cost_ratio) / 2


def test_median_cost_ratio_counts_cache_only_spend_as_real():
    """The exclusion must gate on the PRICED quantity (cost_ratio), not on
    raw nh_tokens: a spec with nh_tokens == 0 but real cache-read spend has
    a real, non-zero cost_ratio and must still count. Swapping the
    `priced_scores` predicate to `if s.nh_tokens` would wrongly drop this
    spec too — this is the regression `priced_scores`'s docstring warns
    against."""
    dead = _score(task_id="dead", nh=0, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0, orig=1000)
    cache_only = _score(task_id="cache-only", nh=0, nh_cache_tokens=5000,
                         nh_cache_creation_tokens=0, orig=500)
    assert cache_only.cost_ratio == 1.0
    card = NorthStarCard(scores=[dead, cache_only])
    assert card.median_cost_ratio == 1.0


def test_median_cost_ratio_still_counts_small_nonzero_spend():
    """Negative control: a spec with tiny but real spend must still count,
    so the zero-spend exclusion cannot be widened into discarding cheap
    wins."""
    tiny = _score(task_id="tiny", nh=1, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0, orig=100000)
    real = _score(task_id="real", nh=45000, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0, orig=500000)
    card = NorthStarCard(scores=[tiny, real])
    assert card.median_cost_ratio == (tiny.cost_ratio + real.cost_ratio) / 2


def test_median_cost_ratio_is_none_when_every_spec_did_zero_work():
    """An empty-of-real-work run must not publish a perfect 0.0 — it must
    report unmeasurable (None), same as no baseline at all."""
    dead1 = _score(task_id="dead1", nh=0, nh_cache_tokens=0,
                   nh_cache_creation_tokens=0, orig=1000)
    dead2 = _score(task_id="dead2", nh=0, nh_cache_tokens=0,
                   nh_cache_creation_tokens=0, orig=2000)
    card = NorthStarCard(scores=[dead1, dead2])
    assert card.median_cost_ratio is None


# --------------------- bench cost ratio part 2: basis label ------------------ #

def test_median_cost_ratio_basis_is_uniform_when_every_score_agrees():
    all_cache_weighted = NorthStarCard(scores=[
        _score(task_id="a", nh=500, orig=1000),
        _score(task_id="b", nh=300, orig=1000),
    ])
    assert all_cache_weighted.median_cost_ratio_basis == "cache-weighted"

    tiered = _score(task_id="c", nh=500, orig=1000)
    tiered.nh_role_tokens = {
        "reviewer": {"tokens_used": 500, "cache_read_tokens": 100,
                    "cache_creation_tokens": 20}}
    all_tier_weighted = NorthStarCard(scores=[tiered])
    assert all_tier_weighted.median_cost_ratio_basis == "tier-weighted"


def test_median_cost_ratio_basis_is_mixed_never_silently_one_or_the_other():
    """A card straddling the part-1 rollout — one score loaded from a
    pre-tier-weighting `latest.json`, one freshly run with a per-role
    breakdown — must report 'mixed', not silently pick either pure basis."""
    legacy = _score(task_id="legacy", nh=500, orig=1000)
    fresh = _score(task_id="fresh", nh=500, orig=1000)
    fresh.nh_role_tokens = {
        "coder": {"tokens_used": 500, "cache_read_tokens": 0,
                 "cache_creation_tokens": 0}}
    card = NorthStarCard(scores=[legacy, fresh])
    assert card.median_cost_ratio_basis == "mixed"


def test_median_cost_ratio_basis_is_n_a_with_no_priced_population():
    card = NorthStarCard(scores=[])
    assert card.median_cost_ratio_basis == "n/a"


def test_cost_ratio_basis_helper_matches_the_card_property():
    """`NorthStarCard.median_cost_ratio_basis` and the per-project rows in
    `render_northstar_md` both route through this ONE function — pinned
    directly so the two can never compute the label two different ways."""
    scores = [_score(task_id="a", nh=500, orig=1000)]
    card = NorthStarCard(scores=scores)
    assert cost_ratio_basis(card.priced_scores) == card.median_cost_ratio_basis


def test_a_cache_only_spec_is_not_dead():
    """A spec with nh_tokens == 0 but real cache-read spend did REAL WORK —
    the cost median already counts it (`priced_scores` gates on the priced
    quantity), so the death counter must not simultaneously call it an SDK
    death. This is the normal shape on resumed/attempt-2 runs, where
    cache-read dominates the burn."""
    cache_only = _score(task_id="cache-only", satisfied=True, nh=0,
                        nh_cache_tokens=500_000,
                        nh_cache_creation_tokens=0, orig=1000)
    # The exact empirical shape of the defect: priced spend 50_000 (0.1 ×
    # 500k) over a 1_000-token original — counted by the cost median at 50.0
    # AND counted dead by the old `not s.nh_tokens` predicate.
    assert cache_only.cost_ratio == 50.0
    failed = _score(task_id="failed", satisfied=False)
    card = NorthStarCard(scores=[cache_only, failed])
    assert card.dead_specs == 0
    assert card.dead_spec_count == 0
    # ...and it counts in the success denominator: 1 pass over the 2 rows
    # that did priced work, on BOTH estimators.
    assert card.success_rate == 0.5
    assert card.spec_mean_success_rate == 0.5


def test_a_creation_only_spec_is_not_dead():
    """Cache CREATION is priced spend too (1.25×, the most expensive class).
    The predicate must gate on the full priced quantity — a rewrite that sums
    only nh_tokens + cache_read (dropping the creation term) survives every
    cache-only test, because those rows carry read spend as well. A row whose
    ONLY spend is cache creation is the case that tells them apart."""
    creation_only = _score(task_id="creation-only", satisfied=True, nh=0,
                           nh_cache_tokens=0, nh_cache_creation_tokens=400,
                           orig=1000)
    assert creation_only.nh_tokens == 0 and creation_only.nh_cache_tokens == 0
    assert creation_only.cost_ratio == 0.5          # 1.25 × 400 / 1000
    other = _score(task_id="other", satisfied=False)
    card = NorthStarCard(scores=[creation_only, other])
    assert card.dead_specs == 0
    assert card.dead_spec_count == 0
    assert creation_only in card.measured_scores
    assert card.success_rate == 0.5


# --------------------------- unscoreable judges ------------------------------ #
# A judge that produced no parseable verdict after the bounded retry is a
# BROKEN JUDGE, not an agent that missed the goal — those two used to be
# indistinguishable downstream (both just `goal_satisfied=False`). These tests
# pin: (1) the new visibility, (2) that the denominator stays conservative by
# default (an unscoreable row still counts as not-satisfied), and (3) that an
# old results file predating the field renders honestly as "not recorded"
# rather than a confident zero.

def test_aggregate_reports_unscoreable_rows_and_specs():
    """Rows vs distinct specs, mirroring dead_specs/dead_spec_count: one spec
    unscoreable across both of its 2 trials counts 2 rows, 1 spec."""
    unscoreable_t0 = _score(task_id="flaky-judge", satisfied=False)
    unscoreable_t0.unscoreable = True
    unscoreable_t0.trial = 0
    unscoreable_t1 = _score(task_id="flaky-judge", satisfied=False)
    unscoreable_t1.unscoreable = True
    unscoreable_t1.trial = 1
    clean_a = _score(task_id="clean-a", satisfied=True)
    clean_b = _score(task_id="clean-b", satisfied=False)
    card = NorthStarCard(
        scores=[unscoreable_t0, unscoreable_t1, clean_a, clean_b], trials=2)
    assert card.unscoreable_specs == 2
    assert card.unscoreable_spec_count == 1
    agg = card.as_dict()["aggregate"]
    assert agg["unscoreable_specs"] == 2
    assert agg["unscoreable_spec_count"] == 1


def test_report_renders_the_unscoreable_line_at_zero():
    """Same rationale as the dead-specs line: a missing line and a measured
    zero must not look the same — the line always renders, 0 included."""
    card = NorthStarCard(scores=[_score(satisfied=True)], created_at="2026-08-23")
    md = render_northstar_md(card)
    line = next(ln for ln in md.splitlines() if "unscoreable" in ln.lower())
    assert "**0**" in line, line


def test_report_renders_the_unscoreable_count_when_present():
    core = _score(task_id="ns-1", satisfied=False)
    core.unscoreable = True
    core.subset = "core"
    other = _score(task_id="ns-2", satisfied=True)
    card = NorthStarCard(scores=[core, other], created_at="2026-08-23")
    md = render_northstar_md(card)
    line = next(ln for ln in md.splitlines() if "unscoreable" in ln.lower())
    assert "**1**" in line, line
    assert "STILL COUNT AS NOT SATISFIED" in line, line
    # per-task table marks the row so a reader scanning ❌s can tell them apart
    row = next(ln for ln in md.splitlines() if ln.startswith("| ns-1 |"))
    assert "❌ (judge unparseable)" in row, row


def test_an_unscoreable_spec_still_counts_as_not_satisfied():
    """THE denominator guard — the trap this task exists to avoid. An
    unscoreable row must move every headline figure IDENTICALLY to an
    ordinary goal_satisfied=False row: marking it unscoreable is visibility,
    not a denominator change. If this test starts failing because the two
    diverge, someone dropped unscoreable rows from a rate — do not "fix" the
    test to match; that IS the trap this task is about."""
    def _card(unscoreable):
        passed = _score(task_id="pass", satisfied=True)
        failed = _score(task_id="fail", satisfied=False)
        if unscoreable:
            failed.unscoreable = True
        return NorthStarCard(scores=[passed, failed])

    plain = _card(unscoreable=False)
    broken_judge = _card(unscoreable=True)
    assert broken_judge.satisfied == plain.satisfied
    assert broken_judge.success_rate == plain.success_rate
    assert broken_judge.spec_mean_success_rate == plain.spec_mean_success_rate
    assert (len(broken_judge.measured_scores) == len(plain.measured_scores))
    agg_plain = plain.as_dict()["aggregate"]
    agg_broken = broken_judge.as_dict()["aggregate"]
    assert agg_broken["spec_mean_success_rate"] == agg_plain["spec_mean_success_rate"]
    assert agg_broken["satisfied"] == agg_plain["satisfied"]
    # ...and the row is still measured, still counted — unlike a dead spec.
    assert broken_judge.dead_specs == 0
    assert len(broken_judge.priced_scores) == len(plain.priced_scores)


def test_headline_unmoved_for_a_corpus_with_no_unscoreable_specs():
    """A corpus with no unscoreable rows must produce the exact same headline
    as before this field existed — pinned against an independently computed
    Wilson interval, not against a re-derivation inside the same module."""
    from no_human.eval.northstar_card import (
        sample_phrase, success_headline, wilson_interval)
    scores = [_score(task_id=f"t{i}", satisfied=(i % 2 == 0)) for i in range(4)]
    card = NorthStarCard(scores=scores, created_at="2026-08-23")
    assert card.spec_mean_success_rate == 0.5
    lo, hi = wilson_interval(2, 4)
    expected = (f"{0.5:.1%} (95% CI {lo * 100:.1f}–{hi * 100:.1f}, "
               f"{sample_phrase(card)})")
    assert success_headline(card) == expected, success_headline(card)
    md = render_northstar_md(card)
    line = next(ln for ln in md.splitlines()
               if "Success (goal satisfied" in ln)
    assert "2/4 measured" in line, line


def test_card_save_load_round_trips_unscoreable(tmp_path):
    s = _score(task_id="ns-1", satisfied=False)
    s.unscoreable = True
    card = NorthStarCard(scores=[s], created_at="2026-08-23", label="rt")
    p = tmp_path / "latest.json"
    card.save(p)
    loaded = NorthStarCard.load(p)
    assert loaded is not None
    assert loaded.scores[0].unscoreable is True
    assert loaded.unscoreable_recorded is True


def test_an_old_card_without_the_field_says_not_recorded(tmp_path):
    """A results file written before this field existed has no
    `unscoreable`/`unscoreable_specs` keys anywhere — `load()` must still
    succeed, and the rendered line must say so honestly rather than print a
    confident 0 that looks like a measured zero."""
    old = {
        "created_at": "2026-01-01", "label": "pre-field",
        "scores": [{
            "task_id": "ns-1", "title": "t", "outcome_status": "done",
            "goal_satisfied": False, "escalated_honestly": False,
            "mergeable": True, "nh_tokens": 500, "nh_cache_tokens": 100,
            "nh_turns": 5, "nh_wall_clock_s": 60.0, "orig_tokens": 1000,
            "orig_wall_clock_s": 600.0, "orig_corrections": 3,
            "subset": "core",
            # deliberately no "unscoreable" key
        }],
        "aggregate": {
            # deliberately no "unscoreable_specs"/"unscoreable_spec_count" keys
        },
    }
    p = tmp_path / "old_latest.json"
    p.write_text(__import__("json").dumps(old))
    loaded = NorthStarCard.load(p)
    assert loaded is not None
    assert loaded.scores[0].unscoreable is False
    assert loaded.unscoreable_recorded is False
    md = render_northstar_md(loaded)
    line = next(ln for ln in md.splitlines() if "unscoreable" in ln.lower())
    assert "not recorded" in line, line


# ------------------------------ pin re-derivation ---------------------------- #
# History rewrites make a spec's recorded pin unreachable; `nh bench build`
# repairs it by re-deriving a pin from the spec's own recorded start date
# (bench_task.build_bench_tasks) rather than dropping or guessing. These tests
# pin the disclosure side: the count must survive save/load, render
# unconditionally (including the 0 case, as a positive control against a
# missing line), and read "not recorded" — never a confident 0 — for a
# results file that predates the field.

def test_aggregate_reports_pin_rederived_rows_and_specs():
    rederived = _score(task_id="rewritten-history", satisfied=False)
    rederived.pin_rederived = True
    clean = _score(task_id="clean", satisfied=True)
    card = NorthStarCard(scores=[rederived, clean], created_at="2026-08-23")
    assert card.pin_rederived_specs == 1
    assert card.pin_rederived_spec_count == 1
    agg = card.as_dict()["aggregate"]
    assert agg["pin_rederived_specs"] == 1
    assert agg["pin_rederived_spec_count"] == 1


def test_pin_rederivation_note_at_zero_is_a_positive_control():
    """Same rationale as the unscoreable line: a missing line and a measured
    zero must not look the same — the line always renders, 0 included, and
    says every measured spec ran against the pin the original task saw."""
    card = NorthStarCard(scores=[_score(satisfied=True)], created_at="2026-08-23")
    note = pin_rederivation_note(card)
    assert "**0**" in note, note
    assert "the original task actually saw" in note, note
    md = render_northstar_md(card)
    line = next(ln for ln in md.splitlines() if "re-derived pin" in ln.lower())
    assert "**0**" in line, line


def test_report_line_carries_the_rederived_count():
    a = _score(task_id="ns-1", satisfied=False)
    a.pin_rederived = True
    b = _score(task_id="ns-2", satisfied=True)
    b.pin_rederived = True
    c = _score(task_id="ns-3", satisfied=True)
    card = NorthStarCard(scores=[a, b, c], created_at="2026-08-23")
    note = pin_rederivation_note(card)
    assert "**2**" in note, note
    assert "of 3" in note, note
    assert "PARTIALLY" in note, note
    md = render_northstar_md(card)
    line = next(ln for ln in md.splitlines() if "re-derived pin" in ln.lower())
    assert "**2**" in line, line


def test_card_save_load_round_trips_pin_rederived(tmp_path):
    s = _score(task_id="ns-1", satisfied=False)
    s.pin_rederived = True
    card = NorthStarCard(scores=[s], created_at="2026-08-23", label="rt")
    p = tmp_path / "latest.json"
    card.save(p)
    loaded = NorthStarCard.load(p)
    assert loaded is not None
    assert loaded.scores[0].pin_rederived is True
    assert loaded.pin_rederived_recorded is True
    assert loaded.pin_rederived_spec_count == 1


def test_an_old_card_without_the_pin_rederived_field_says_not_recorded(tmp_path):
    """A results file written before this field existed has no
    `pin_rederived`/`pin_rederived_specs` keys anywhere — `load()` must still
    succeed, and the rendered line must say so honestly rather than print a
    confident 0 that looks like a measured zero."""
    old = {
        "created_at": "2026-01-01", "label": "pre-field",
        "scores": [{
            "task_id": "ns-1", "title": "t", "outcome_status": "done",
            "goal_satisfied": False, "escalated_honestly": False,
            "mergeable": True, "nh_tokens": 500, "nh_cache_tokens": 100,
            "nh_turns": 5, "nh_wall_clock_s": 60.0, "orig_tokens": 1000,
            "orig_wall_clock_s": 600.0, "orig_corrections": 3,
            "subset": "core",
            # deliberately no "pin_rederived" key
        }],
        "aggregate": {
            # deliberately no "pin_rederived_specs"/"pin_rederived_spec_count" keys
        },
    }
    p = tmp_path / "old_latest.json"
    p.write_text(__import__("json").dumps(old))
    loaded = NorthStarCard.load(p)
    assert loaded is not None
    assert loaded.scores[0].pin_rederived is False
    assert loaded.pin_rederived_recorded is False
    note = pin_rederivation_note(loaded)
    assert "not recorded" in note, note
    md = render_northstar_md(loaded)
    line = next(ln for ln in md.splitlines() if "re-derived pin" in ln.lower())
    assert "not recorded" in line, line


def test_success_rate_cannot_exceed_one_when_a_dead_row_is_satisfied():
    """The runner scores a pre-model death on an expect-escalation spec as
    escalated-honestly, which arrives here as goal_satisfied=True — so the
    POOLED numerator (`satisfied`) counts rows the measured denominator
    excludes. Numerator and denominator must come from the same population:
    pairing `self.satisfied` with the measured denominator yields 2.0 on this
    card, a rate no probability admits."""
    failed = _score(task_id="f", satisfied=False)
    dead_gated = [
        _score(task_id=f"dg{i}", satisfied=True, status="escalated",
               expected_escalation=True, nh=0, nh_cache_tokens=0,
               nh_cache_creation_tokens=0)
        for i in range(2)
    ]
    card = NorthStarCard(scores=[failed] + dead_gated)
    assert card.satisfied == 2                  # pooled: counts the dead pair
    assert card.measured_scores == [failed]     # they did no priced work
    # The hazard, stated as arithmetic: the mutant's value on this card.
    assert card.satisfied / len(card.measured_scores) == 2.0
    assert card.success_rate == 0.0             # 0 passes of 1 measured
    assert card.success_rate <= 1.0
    measured = card.measured_scores
    assert card.success_rate == (
        sum(1 for s in measured if s.goal_satisfied) / len(measured))


def test_the_printed_success_fraction_counts_the_measured_population():
    """The report's Success bullet prints a raw fraction in front of the
    headline percentage, and the dead-specs line below promises deaths "are
    EXCLUDED from the success figures above". Printing the POOLED pair there
    (satisfied over every ran row) makes that sentence false on any
    dead-carrying card — the fraction must divide over the same measured
    population the headline does, with the ran count and the death count
    still on the line, labeled as what they are."""
    alive_pass = _score(task_id="ap", satisfied=True)
    alive_fail = _score(task_id="af", satisfied=False)
    dead = _score(task_id="dd", satisfied=False, nh=0, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0)
    card = NorthStarCard(label="x", created_at="2026-08-08",
                         scores=[alive_pass, alive_fail, dead])
    assert card.spec_mean_success_rate == 0.5   # the headline's own value
    md = render_northstar_md(card)
    line = next(ln for ln in md.splitlines()
                if "Success (goal satisfied" in ln)
    # The fraction reduces to the percentage standing behind it: 1/2, not 1/3.
    assert "1/2 measured" in line, line
    assert "1/3" not in line, line
    # The death is not hidden — the ran population is on the line, labeled.
    assert "of 3 ran (1 dead)" in line, line


def test_quota_deaths_are_excluded_from_the_success_denominator():
    """A spec whose SDK died before any model call produced no measurement —
    counting it as a quality failure makes a quota outage read as a quality
    regression. A death leaves the success denominator the same way a skip
    already does; the pre-fix pooled rate survives, clearly named, never the
    headline."""
    scores = [
        _score(task_id="a", satisfied=True),
        _score(task_id="b", satisfied=False),
        _score(task_id="c", satisfied=False, nh=0, nh_cache_tokens=0,
               nh_cache_creation_tokens=0),
        _score(task_id="d", satisfied=False, nh=0, nh_cache_tokens=0,
               nh_cache_creation_tokens=0),
        # A skip is a DECISION, not a death: it is excluded from `ran`
        # already, so it must move neither rate nor the death count.
        _score(task_id="e", status="skipped", satisfied=None, nh=0,
               nh_cache_tokens=0, nh_cache_creation_tokens=0),
    ]
    card = NorthStarCard(scores=scores)
    assert card.dead_specs == 2
    assert card.success_rate == 0.5                 # 1 of the 2 that worked
    assert card.success_rate_incl_dead == 0.25      # kept, named, not headline
    agg = card.as_dict()["aggregate"]
    assert agg["success_rate"] == 0.5
    assert agg["success_rate_incl_dead"] == 0.25


def test_the_dead_definition_matches_the_priced_definition():
    """THE one-definition guard. `dead_specs` and `priced_scores` used to hold
    two disagreeing notions of "did no work" (raw nh_tokens vs the priced
    quantity), so a cache-only spec was simultaneously real work to the cost
    median and dead to the death counter. Over every combination of the three
    nh-side token classes, a row must be dead IFF the priced population
    excludes it — re-deriving either side breaks this by name."""
    grid = [0, 1, 40_000]
    for nh in grid:
        for cache in grid:
            for creation in grid:
                s = _score(task_id="g", nh=nh, nh_cache_tokens=cache,
                           nh_cache_creation_tokens=creation, orig=1000)
                card = NorthStarCard(scores=[s])
                dead = card.dead_specs == 1
                excluded = card.priced_scores == []
                assert dead == excluded, (nh, cache, creation, dead, excluded)
    # Control, deliberately OUTSIDE the grid: a missing BASELINE (orig side,
    # cost_ratio None) makes a row unpriceABLE, not dead. `priced_scores`
    # excludes it — there is no ratio to median — but its real spend keeps it
    # out of the death count and inside the success denominator.
    # `test_bench_task` pins the same fact end-to-end (dead_specs == 0 on
    # baseline-less runs), so folding "no baseline" into "dead" cannot pass
    # the suite either.
    no_baseline = _score(task_id="nb", nh=500, nh_cache_tokens=0,
                         nh_cache_creation_tokens=0, orig=0)
    nb_card = NorthStarCard(scores=[no_baseline])
    assert no_baseline.cost_ratio is None
    assert nb_card.priced_scores == []
    assert nb_card.dead_specs == 0
    assert nb_card.success_rate == 1.0


def test_priced_denominator_matches_the_medians_population():
    """The report's 'over the N ran spec(s)' denominator must count the same
    population median_cost_ratio is computed over — not every spec that
    merely has a cost_ratio (which would include zero-spend ones the median
    just excluded)."""
    card = NorthStarCard(label="x", created_at="2026-07-14", scores=[
        _score(task_id="dead", nh=0, nh_cache_tokens=0,
               nh_cache_creation_tokens=0),
        _score(task_id="real"),
    ])
    card.scores[0].subset = "core"
    card.scores[1].subset = "core"
    assert card.median_cost_ratio == card.scores[1].cost_ratio
    md = render_northstar_md(card)
    assert "over the 1 of 2 ran spec(s)" in md, md


def test_headline_denominator_wording_names_the_priced_predicate():
    """The headline's cost-median denominator sentence must describe the
    population `_priced` (== `priced_scores`) actually is — ran specs with a
    baseline AND non-zero no_human spend — not just 'a recorded original
    cost', which is the (larger) `_baselined` predicate and also true of the
    zero-spend spec the median excludes."""
    card = NorthStarCard(label="x", created_at="2026-07-14", scores=[
        _score(task_id="dead", nh=0, nh_cache_tokens=0,
               nh_cache_creation_tokens=0),
        _score(task_id="real"),
    ])
    card.scores[0].subset = "core"
    card.scores[1].subset = "core"
    md = render_northstar_md(card)
    assert "non-zero no_human spend" in md, md
    assert "over the 1 of 2 ran spec(s) with a recorded original cost " \
        "AND non-zero no_human spend" in md, md


def test_report_headline_states_the_cost_ratio_basis_explicitly():
    """bench cost ratio part 2 acceptance: the report card must say what
    price table the published median was computed against, right beside the
    number — a reader must never have to infer it."""
    tiered = _score(task_id="a")
    tiered.nh_role_tokens = {
        "coder": {"tokens_used": tiered.nh_tokens,
                 "cache_read_tokens": tiered.nh_cache_tokens,
                 "cache_creation_tokens": tiered.nh_cache_creation_tokens}}
    card = NorthStarCard(label="x", created_at="2026-08-12", scores=[tiered])
    card.scores[0].subset = "core"
    md = render_northstar_md(card)
    assert "basis: tier-weighted" in md, md

    legacy_card = NorthStarCard(label="y", created_at="2026-08-12",
                                scores=[_score(task_id="b")])
    legacy_card.scores[0].subset = "core"
    md2 = render_northstar_md(legacy_card)
    assert "basis: cache-weighted" in md2, md2


def test_zero_spend_spec_renders_dash_not_zero_in_per_task_ratio_column():
    """A spec with cost_ratio == 0.0 (no_human crashed/skipped/escalated
    before any model call) must render the SAME unavailable marker the
    per-task ratio column already uses for a missing (None) ratio — not
    '0.00', which reads as a perfect cost win even though `priced_scores`
    (and therefore both medians) already excludes this exact spec."""
    dead = _score(task_id="dead", nh=0, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0, orig=1000)
    dead.subset = "core"
    assert dead.cost_ratio == 0.0
    card = NorthStarCard(label="x", created_at="2026-07-14", scores=[dead])
    md = render_northstar_md(card)
    row = next(line for line in md.splitlines()
               if line.startswith("| dead |"))
    cells = [c.strip() for c in row.split("|")]
    # | dead | status | sat | nh | orig | ratio | corrections | notes |
    ratio_cell = cells[6]
    assert ratio_cell == "—", row
    assert "0.00" not in row, row


def test_total_orig_tokens_denominator_matches_baselined_specs():
    """The total-orig-tokens line's denominator must count the population
    `total_orig_tokens` actually sums over — every ran spec with a recorded
    baseline (cost_ratio is not None) — NOT `priced_scores` (which the
    median uses). A zero-spend spec still HAS a baseline and its orig_tokens
    still land in the sum, so it must still count here even though the
    median excludes it: the token total and the spec count it cites must
    describe the SAME set of specs."""
    dead = _score(task_id="dead", nh=0, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0, orig=1000)
    real = _score(task_id="real", orig=1000)
    card = NorthStarCard(label="x", created_at="2026-07-14",
                         scores=[dead, real])
    card.scores[0].subset = "core"
    card.scores[1].subset = "core"
    # Both specs have a baseline and both contribute to the sum — 2 specs,
    # 2,000 tokens — even though only `real` is priced (nonzero spend).
    assert dead.cost_ratio is not None and dead.cost_ratio == 0.0
    assert card.total_orig_tokens == 2000
    md = render_northstar_md(card)
    assert "original 2,000 over the 2 that have a baseline at all" in md, md


def test_total_orig_tokens_denominator_unchanged_when_no_dead_specs():
    """Negative control: when no spec has zero spend, `priced_scores` and
    'has a baseline' are the same population, so the published sentence must
    read exactly as it always did — this fix must not change output for the
    common (no dead specs) case."""
    card = NorthStarCard(label="x", created_at="2026-07-14", scores=[
        _score(task_id="a", orig=1000),
        _score(task_id="b", orig=1000),
    ])
    card.scores[0].subset = "core"
    card.scores[1].subset = "core"
    md = render_northstar_md(card)
    assert "over the 2 of 2 ran spec(s)" in md, md
    assert "original 2,000 over the 2 that have a baseline at all" in md, md


def test_gate_first_run_passes_and_becomes_baseline():
    # Ten specs, not one: a first run has no baseline to be narrower than, so an
    # absolute floor is the only thing that can stop a slice becoming the
    # reference. The intent — a sound first run passes — is unchanged.
    g = northstar_gate(
        NorthStarCard(scores=[_score(task_id=f"p{i}") for i in range(10)]), None)
    assert g.passed and "baseline" in g.reasons[0]


def test_gate_blocks_success_drop():
    prev = NorthStarCard(scores=[_score(satisfied=True),
                                 _score(task_id="b", satisfied=True)])
    cur = NorthStarCard(scores=[_score(satisfied=True),
                                _score(task_id="b", satisfied=False)])
    g = northstar_gate(cur, prev)
    # Not positional: integrity reasons are prepended, so indexing reasons[0]
    # would make this test load-bearing for ordering it does not care about.
    assert not g.passed
    assert any("success rate dropped" in r for r in g.reasons)


def test_gate_blocks_ratio_regression_but_allows_small_drift():
    prev = NorthStarCard(scores=[_score(nh=500, orig=1000)])      # 0.5
    worse = NorthStarCard(scores=[_score(nh=1100, orig=1000)])    # 1.1 (+0.6)
    drift = NorthStarCard(scores=[_score(nh=700, orig=1000)])     # 0.7 (+0.2)
    assert not northstar_gate(worse, prev).passed
    assert northstar_gate(drift, prev).passed


def test_gate_fails_closed_when_current_ratio_vanishes():
    """Review finding: a run that loses its ratio must block, not silently
    skip the cost check."""
    prev = NorthStarCard(scores=[_score(nh=500, orig=1000)])
    no_ratio = NorthStarCard(scores=[_score(nh=500, orig=0)])   # ratio None
    g = northstar_gate(no_ratio, prev)
    assert not g.passed
    assert any("cannot verify cost" in r for r in g.reasons)


def test_gate_flags_a_cost_ratio_basis_change_across_the_part_1_rollout():
    """A baseline computed on the old (cache-only) basis and a run computed
    with a per-role breakdown are not a like-for-like comparison, even
    though neither ratio is missing — the gate must say so rather than let
    the two numbers be silently diffed as if they were on the same basis."""
    prev = NorthStarCard(scores=[_score(nh=500, orig=1000)])   # cache-weighted
    cur_score = _score(nh=500, orig=1000)
    cur_score.nh_role_tokens = {
        "coder": {"tokens_used": 500, "cache_read_tokens": 100,
                 "cache_creation_tokens": 20}}
    cur = NorthStarCard(scores=[cur_score])                    # tier-weighted
    assert prev.median_cost_ratio_basis == "cache-weighted"
    assert cur.median_cost_ratio_basis == "tier-weighted"

    g = northstar_gate(cur, prev)
    assert not g.passed
    assert any("cost ratio basis changed" in r for r in g.reasons), g.reasons


def test_gate_does_not_flag_a_basis_change_when_both_sides_agree():
    """Negative control: same basis on both sides must never spuriously
    trip the basis-change reason."""
    prev = NorthStarCard(scores=[_score(nh=500, orig=1000)])
    cur = NorthStarCard(scores=[_score(nh=500, orig=1000)])
    g = northstar_gate(cur, prev)
    assert not any("cost ratio basis changed" in r for r in g.reasons), g.reasons


def test_gate_blocks_a_broken_corpus_even_with_NO_baseline():
    """Review finding: the coverage check sat behind the `previous is None`
    early return, and `eval/results/northstar/` is gitignored — so `previous`
    is None in every fresh clone and CI checkout. That exempted exactly the
    configuration where a broken corpus is most likely to be published as the
    baseline every later run is measured against.
    """
    broken = NorthStarCard(label="first", scores=[
        _score(task_id="c1", satisfied=True)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(9)])
    g = northstar_gate(broken, None)
    assert not g.passed, g.reasons
    assert any("went unmeasured" in r for r in g.reasons)


def test_gate_first_run_still_passes_when_the_corpus_is_sound():
    """The fix must not turn every first run into a failure."""
    sound = NorthStarCard(label="first",
                          scores=[_score(task_id=f"c{i}") for i in range(10)])
    g = northstar_gate(sound, None)
    assert g.passed and "baseline" in g.reasons[0]


def test_gate_blocks_when_most_of_the_corpus_went_unmeasured():
    """The v15 shape: most specs never resolved, so they left ``ran`` entirely
    and the survivors read BETTER than the baseline. Every metric check passes
    — only corpus integrity catches it.

    ``ran`` is held EQUAL to the baseline so the narrowing rule cannot fire;
    this pins the unmeasured-fraction wiring on its own.
    """
    prev = NorthStarCard(label="baseline", scores=[
        _score(task_id="p1", satisfied=True), _score(task_id="p2", satisfied=False),
        _score(task_id="p3", satisfied=False)])                    # 33% over 3
    cur = NorthStarCard(label="broken", scores=[
        _score(task_id="c1", satisfied=True), _score(task_id="c2", satisfied=True),
        _score(task_id="c3", satisfied=True)] + [                  # 100% over 3
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(7)])
    # The trap: on every published headline this run looks like an improvement.
    assert cur.success_rate > prev.success_rate
    assert len(cur.ran) == len(prev.ran)          # narrowing rule cannot fire

    g = northstar_gate(cur, prev)
    assert not g.passed
    assert any("went unmeasured" in r for r in g.reasons)
    assert any("7/10" in r for r in g.reasons), g.reasons   # names the numbers


def test_gate_counts_dead_specs_as_unmeasured_too():
    """A spec that RAN but burned zero tokens measured nothing either — the
    backend died before any model call. Same tolerance, same verdict.

    Zero tokens of EVERY class: the fixture used to zero only `nh` and leak
    the helper's cache defaults, which made these rows the cache-only shape
    (real priced spend) and silently pinned the raw-nh_tokens predicate the
    one-definition fix removed. Dead means zero priced spend, so the fixture
    must actually burn nothing."""
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(4)])
    dead_rows = [_score(task_id=f"d{i}", nh=0, nh_cache_tokens=0,
                        nh_cache_creation_tokens=0) for i in range(2)]
    assert all(not s.nh_priced_tokens for s in dead_rows), \
        "fixture rows must burn zero PRICED tokens to be deaths"
    cur = NorthStarCard(label="saturated", scores=[
        _score(task_id="c1"), _score(task_id="c2")] + dead_rows)
    assert len(cur.ran) == len(prev.ran)          # narrowing rule cannot fire
    g = northstar_gate(cur, prev)
    assert not g.passed
    assert any("went unmeasured" in r for r in g.reasons), g.reasons


def test_gate_blocks_a_narrower_run_even_when_fully_measured():
    """Nothing skipped, nothing dead — but half the corpus was never loaded
    (a capped run). A narrower run cannot establish 'no regression'."""
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(6)])
    cur = NorthStarCard(label="slice",
                        scores=[_score(task_id=f"c{i}") for i in range(3)])
    assert unmeasured_specs(cur) == (0, 3)        # unmeasured rule cannot fire
    g = northstar_gate(cur, prev)
    assert not g.passed
    assert any("narrower run" in r for r in g.reasons), g.reasons


def test_narrowing_is_measured_on_ran_not_total():
    """Review finding: mutating `len(current.ran) < len(previous.ran)` to
    `current.total < previous.total` survived the whole suite, because every
    fixture had total == ran on both sides. A corpus that GREW while measuring
    FEWER specs is the case that tells them apart, and it is the shape a
    padded-then-skipped run takes. Skips are held under the coverage ceiling so
    that rule cannot be what fires.
    """
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(10)])
    cur = NorthStarCard(label="wider-but-shallower", scores=[
        _score(task_id=f"c{i}") for i in range(9)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(2)])
    assert cur.total > prev.total            # corpus GREW
    assert len(cur.ran) < len(prev.ran)      # yet measured FEWER
    assert unmeasured_specs(cur) == (2, 11)  # 18% — under the ceiling
    g = northstar_gate(cur, prev)
    assert not g.passed
    assert any("narrower run" in r for r in g.reasons), g.reasons


def test_coverage_ceiling_is_exclusive_at_exactly_twenty_percent():
    """Pins the boundary: `>` not `>=`. Exactly 20% unmeasured is tolerated,
    so a corpus with a couple of known-unrunnable specs is not vetoed."""
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(8)])
    at_limit = NorthStarCard(label="at-limit", scores=[
        _score(task_id=f"c{i}") for i in range(8)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(2)])
    assert unmeasured_specs(at_limit) == (2, 10)      # exactly 20%
    assert northstar_gate(at_limit, prev).passed
    over = NorthStarCard(label="over", scores=[
        _score(task_id=f"c{i}") for i in range(8)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(3)])
    assert unmeasured_specs(over) == (3, 11)          # 27%
    assert not northstar_gate(over, prev).passed


def test_gate_survives_an_empty_card():
    """The `if total and` zero-guard: without it this raises ZeroDivisionError.
    Unreachable from the CLI today, so it had no defender."""
    prev = NorthStarCard(label="baseline", scores=[_score()])
    g = northstar_gate(NorthStarCard(label="empty", scores=[]), prev)
    assert not g.passed          # nothing measured is not "no regression"


def test_gate_still_passes_a_healthy_fully_measured_run():
    """Guard against the fix becoming a blanket blocker: a clean run at the
    same width, with a tolerable skip count, must still pass."""
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(9)])
    cur = NorthStarCard(label="healthy", scores=[
        _score(task_id=f"c{i}") for i in range(9)] + [
        _score(task_id="s1", status="skipped", satisfied=None, nh=0)])
    assert unmeasured_specs(cur) == (1, 10)       # 10% — under the 20% ceiling
    g = northstar_gate(cur, prev)
    assert g.passed, g.reasons


def test_md_hides_non_core_rows():
    """Privacy: generated (non-core) specs appear in aggregates only."""
    card = NorthStarCard(scores=[
        _score(task_id="core-1"),
        _score(task_id="gen-1"),
    ], created_at="2026-07-14")
    card.scores[0].subset = "core"
    md = render_northstar_md(card)
    assert "| core-1 |" in md
    assert "gen-1" not in md
    assert "aggregates only" in md


def test_save_load_roundtrip(tmp_path):
    card = NorthStarCard(scores=[_score(expected_escalation=True)],
                         created_at="2026-07-14", label="baseline")
    p = tmp_path / "latest.json"
    card.save(p)
    loaded = NorthStarCard.load(p)
    assert loaded is not None
    assert loaded.label == "baseline" and loaded.total == 1
    assert loaded.scores[0].expected_escalation is True
    assert loaded.median_token_ratio == card.median_token_ratio
    assert NorthStarCard.load(tmp_path / "missing.json") is None


def test_render_md_has_headline_and_no_numeric_selfscore():
    card = NorthStarCard(scores=[_score()], created_at="2026-07-14",
                         label="baseline")
    card.scores[0].subset = "core"   # only curated rows are rendered
    md = render_northstar_md(card)
    assert "Median token ratio" in md
    # The label changed deliberately: crediting tasks no_human REFUSED as
    # "avoided" flattered the headline (on v13, 251 of 350 came from escalated
    # tasks the human still has to do). Assert the SPLIT, not just the phrase.
    assert "follow-ups avoided on delivered tasks" in md.lower()
    assert "correctly escalated" in md.lower()
    # The cost median must carry the denominator it is a median over.
    assert "with a recorded original cost" in md.lower()
    assert "orig follow-ups (proxy)" in md   # per-task table too (review)
    assert "| ns-1 |" in md
    assert "/10" not in md


def test_the_delivered_split_publishes_NUMBERS_not_just_labels():
    """The split is the whole point, and only its LABELS were asserted — so the
    report could print 350 under a "DELIVERED" heading, or a delivered count
    larger than the satisfied count, with the suite green. That is a regression
    straight back to the flattery this split removed.

    3 satisfied: 2 delivered (done / awaiting_approval) + 1 correct escalation.
    Follow-ups: 10 + 20 on the delivered pair, 96 on the escalation.
    """
    card = NorthStarCard(label="x", scores=[
        _score(task_id="d1", status="done", satisfied=True, corrections=10),
        _score(task_id="d2", status="awaiting_approval", satisfied=True,
               corrections=20),
        _score(task_id="g1", status="escalated", satisfied=True,
               corrections=96, expected_escalation=True),
        _score(task_id="f1", satisfied=False, corrections=5),
    ])
    assert card.corrections_avoided == 126           # 10 + 20 + 96
    assert card.corrections_avoided_delivered == 30  # 10 + 20 only

    md = render_northstar_md(card)
    assert "avoided on DELIVERED tasks: 30**" in md, md
    assert "a further 96 belong" in md, md
    # The success line must name the delivered count, not the satisfied count.
    assert "of which 2 DELIVERED" in md, md
    assert "1 correctly ESCALATED" in md, md
def test_gate_blocks_a_narrow_first_run_with_no_baseline():
    """Review finding: coverage is a RATIO over what LOADED, so it is blind to
    specs never loaded. Point --specs-dir at the specs that still resolve (or
    delete the dead ones) and coverage reads a perfect 0/N — which is the likely
    REACTION to a coverage refusal, and would have handed the incident run the
    baseline anyway over the same survivors. With no baseline to be narrower
    than, an absolute floor is the only thing that can see it."""
    slice_run = NorthStarCard(label="filtered",
                              scores=[_score(task_id=f"c{i}") for i in range(5)])
    assert unmeasured_specs(slice_run) == (0, 5)   # coverage cannot fire
    g = northstar_gate(slice_run, None)
    assert not g.passed
    assert any("no baseline" in r for r in g.reasons), g.reasons


def test_gate_blocks_an_empty_baseline():
    """Reachable via `--prev <file>`, which accepts any results file. Every
    comparison would silently pass against a baseline that measured nothing."""
    empty_prev = NorthStarCard(label="empty", scores=[])
    cur = NorthStarCard(label="cur",
                        scores=[_score(task_id=f"c{i}") for i in range(10)])
    g = northstar_gate(cur, empty_prev)
    assert not g.passed
    assert any("nothing to compare" in r for r in g.reasons), g.reasons


def test_gate_blocks_a_filtered_slice_of_the_available_corpus():
    """THE incident reaction, and the case a spec-count floor cannot see.

    Filter --specs-dir to the 19 specs that still resolve out of 55: coverage
    reads a perfect 0/19, 19 clears the 10-spec floor comfortably, and on a
    fresh clone there is no baseline to be narrower than. Every other rule
    passes and a 100%-success run becomes the published baseline. Only
    loaded-vs-available sees it.
    """
    survivors = NorthStarCard(
        label="filtered", corpus_available=55,
        scores=[_score(task_id=f"c{i}") for i in range(19)])
    assert unmeasured_specs(survivors) == (0, 19)      # coverage cannot fire
    assert len(survivors.ran) >= 10                    # the floor cannot fire
    g = northstar_gate(survivors, None)
    assert not g.passed, g.reasons
    assert any("19 of 55" in r for r in g.reasons), g.reasons


def test_gate_allows_a_run_that_loaded_the_whole_corpus():
    """The shortfall rule must not fire on an honest full run."""
    full = NorthStarCard(label="full", corpus_available=55,
                         scores=[_score(task_id=f"c{i}") for i in range(55)])
    assert northstar_gate(full, None).passed


def test_the_first_run_floor_counts_ran_not_total():
    """Same ran-vs-total axis a sibling rule was pinned on. total=12 clears a
    naive `total < 10`, but only 8 specs were MEASURED, and the skips sit at
    the coverage ceiling so coverage stays silent."""
    card = NorthStarCard(label="thin", corpus_available=12, scores=[
        _score(task_id=f"c{i}") for i in range(8)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(2)])
    assert card.total == 10 and len(card.ran) == 8
    assert unmeasured_specs(card) == (2, 10)           # 20% — at the ceiling
    g = northstar_gate(card, None)
    assert not g.passed
    assert any("no baseline" in r for r in g.reasons), g.reasons


def test_corpus_available_survives_save_and_load(tmp_path):
    """The whole loaded-vs-available rule depends on this field being produced
    by the CLI and surviving serialisation — and the entire plumbing could be
    severed with all 2021 tests green. `nh bench publish <file>` reconstructs
    the card via `load`, so if the key is ever dropped the filtered-slice
    refusal is silently dead in the real flow."""
    card = NorthStarCard(label="x", corpus_available=55,
                         scores=[_score(task_id=f"c{i}") for i in range(11)])
    p = tmp_path / "run.json"
    card.save(p)
    assert '"corpus_available": 55' in p.read_text(encoding="utf-8")
    reloaded = NorthStarCard.load(p)
    assert reloaded.corpus_available == 55
    # And the rule still fires on the RELOADED card, not just the built one.
    assert "11 of 55" in corpus_shortfall(reloaded)


def test_the_report_discloses_that_corpus_resolution_is_MACHINE_LOCAL():
    """The published report is the artifact a reader trusts, and nothing in it
    said the spec RESOLUTION depends on a gitignored local file.

    `eval/repo_map.yaml` translates the vendor-neutral tracked paths to real
    checkouts and is gitignored by design. A fresh clone or `git worktree`
    LOADS the same specs — the loader globs the directory and only rewrites
    paths, it never drops one — but RESOLVES far fewer, and the unresolved
    ones are skipped. So the count that moves is the Headline's
    `skipped (not measured)`, while loaded-vs-available is invariant.

    The first version of this block got that backwards ("loads FEWER specs";
    "check the loaded-vs-available count") and would have reassured a reader
    that a heavily-skipped corpus was healthy.
    """
    from no_human.eval.northstar_card import render_northstar_md

    md = render_northstar_md(NorthStarCard(
        scores=[_score(), _score(status="skipped", satisfied=None,
                                 task_id="ns-skip")], label="x"))
    block = [ln for ln in md.split("\n") if "machine-local" in ln]
    assert block, md[:400]
    block = block[0]

    assert "repo_map.yaml" in block, "must name the file that does the resolving"
    assert "repo_map.example.yaml" in block, "must point at the tracked template"
    assert "gitignored" in block, "must say WHY a clone cannot have it"

    # OBSERVE THE HEADLINE, not the block's own words. The previous version
    # asserted `"skipped (not measured)" in md`, which the block itself
    # satisfies — so renaming the Headline label left the test green while the
    # block pointed at a figure no longer in the report. That label has
    # already been renamed once (non-runnable -> not measured), so the drift
    # is real, not hypothetical.
    # Locate by the LABEL, not the bullet's prose: rewording "Success (goal
    # satisfied, unattended)" is harmless and must not fail this test.
    # `" ran "`, not `"ran ("`: the success figure is no longer a bare
    # percentage in parentheses — it is `N/M ran — 91.7% (95% CI …, n=…)` — and
    # the old locator was matching the punctuation of that percentage rather
    # than the label it claims to anchor on. The two assertions below still
    # bind the label itself, so this stays non-vacuous.
    headline = [ln for ln in md.split("\n")
                if "skipped (" in ln and " ran " in ln]
    assert headline, md[:400]
    assert "skipped (not measured)" in headline[0], headline[0]
    assert "skipped (not measured)" in block, (
        "the block must name the same figure the Headline prints")

    # The mechanism, not one literal sentence: it must say the loaded count
    # does NOT move, and must not repeat the original false claim.
    assert "LOADS THE SAME SPECS" in block
    # The one sentence the whole mechanism rests on, and the one
    # test_the_repo_map_changes_RESOLUTION_not_the_loaded_count proves true —
    # pinned literally so the prose cannot contradict the behaviour.
    assert "it never drops a spec" in block
    # SCOPED: --full/--limit/--specs-dir genuinely do move loaded-vs-available;
    # only RESOLUTION failures cannot. The unscoped claim was too broad.
    assert "resolution failures cannot move loaded-vs-available" in block
    # A trust block that reassures about a big skip count must also say which
    # way the bias runs: skips leave the denominator, so the score reads HIGHER.
    assert "HIGHER" in block, "must name the DIRECTION of the bias"
    assert "loads FEWER specs" not in md
    assert "loads fewer of them" not in md
    # Machine-specific counts must not be baked into every future report.
    for stale in ("53 resolving", "17 without", "55 of 55"):
        assert stale not in block, f"machine-local figure {stale!r} in the report"


def test_the_repo_map_changes_RESOLUTION_not_the_loaded_count(tmp_path, monkeypatch):
    """The MECHANISM the disclosure block asserts, executed rather than
    string-matched.

    The block's other assertions pin its own prose, so a paraphrase that adds
    a falsehood alongside the true sentence slips through. This runs the real
    loader with and without a repo map and pins the invariant it claims: the
    LOADED count is identical, and only the RESOLVED count moves. If that ever
    stops being true, the report is telling readers something false and no
    amount of phrase-matching would notice.
    """
    from pathlib import Path

    import no_human.eval.bench_task as bt

    specs = tmp_path / "specs"
    specs.mkdir()
    real = tmp_path / "real_checkout"
    (real / ".git").mkdir(parents=True)
    for i in range(3):
        (specs / f"ns-{i}.yaml").write_text(
            f"id: ns-{i}\ntitle: t\nrequest: r\nsubset: core\nrunnable: true\n"
            f"repo:\n  path: /neutral/project-{i}\n  pin: ''\n  branch: ''\n")

    def unresolved(tasks):
        # The PRODUCTION predicate, not a three-line mirror of it: if
        # check_repo_map's notion of "resolves" ever diverges from this test's,
        # the test would keep passing against its own reimplementation.
        return len(bt.check_repo_map(tasks))

    missing = tmp_path / "absent.yaml"
    monkeypatch.setattr(bt, "REPO_MAP_PATH", missing)
    without = bt.load_bench_tasks(specs, subset="core")

    mapped = tmp_path / "repo_map.yaml"
    mapped.write_text("\n".join(f"/neutral/project-{i}: {real}" for i in range(3)))
    monkeypatch.setattr(bt, "REPO_MAP_PATH", mapped)
    with_map = bt.load_bench_tasks(specs, subset="core")

    assert len(without) == len(with_map) == 3, "the map must not change LOADING"
    assert unresolved(without) == 3, "neutral paths resolve nowhere on their own"
    assert unresolved(with_map) == 0, "the map is what makes them resolve"


def test_per_project_label_is_redacted_but_grouping_uses_the_real_name():
    """The per-project label is a real repo name (v13's labels were the real
    names, not neutral basenames), so the rendered doc must redact it. But
    `project` is ALSO the grouping key — redacting the KEY would merge distinct
    projects into one row. Two projects that redact to the same string must stay
    two rows: the DISPLAY is scrubbed, the counts come from the real name.

    Deleting redact_for_publish from the per-project line leaves the real term
    in the table and fails the first assertion; keying the group on the redacted
    label instead of `p` collapses the two rows and fails the last."""
    a1 = _score(task_id="a1"); a1.project = "windsurf"  # term-ok: the fixture needs a real banned term
    a2 = _score(task_id="a2"); a2.project = "windsurf"  # term-ok: same real project
    b1 = _score(task_id="b1"); b1.project = "WINDSURF"  # term-ok: distinct key, same redaction
    md = render_northstar_md(NorthStarCard(scores=[a1, a2, b1]))

    assert "## Per-project" in md
    section = md.split("## Per-project", 1)[1]
    assert "windsurf" not in section.lower(), section  # term-ok: the banned term is gone
    assert "<redacted>" in section
    # real-name grouping: two keys -> a 2-task row AND a 1-task row, not one of 3
    assert "| 2 |" in section and "| 1 |" in section, section
    assert "| 3 |" not in section, "distinct projects were merged under the redacted label"


def _project_row(md: str, project: str) -> str:
    section = md.split("## Per-project", 1)[1]
    lines = [l for l in section.splitlines() if l.startswith(f"| {project} |")]
    assert len(lines) == 1, (project, section)
    return lines[0]


def test_per_project_median_excludes_zero_spend_like_the_headline():
    """The per-project median must be taken over the exact same population as
    the headline `median_cost_ratio` (`card.priced_scores`), not a locally
    re-derived `if s.cost_ratio is not None` predicate. Project `p` has one
    real spec (ratio 0.5) and one zero-spend spec (ratio 0.0, a non-result).
    Against the old predicate both count, medianing to 0.250 — half the real
    spec's ratio. The fix must show 0.500."""
    real = _score(task_id="real", nh=500, orig=1000, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0)
    real.project = "p"
    dead = _score(task_id="dead", nh=0, orig=1000, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0)
    dead.project = "p"
    assert real.cost_ratio == 0.5
    assert dead.cost_ratio == 0.0

    # a second project so the "## Per-project" section renders at all
    other = _score(task_id="other", nh=300, orig=1000, nh_cache_tokens=0,
                    nh_cache_creation_tokens=0)
    other.project = "q"

    card = NorthStarCard(scores=[real, dead, other])
    md = render_northstar_md(card)
    row = _project_row(md, "p")

    assert "0.500" in row, row
    assert "0.250" not in row, row
    single_project_card = NorthStarCard(scores=[real, dead])
    assert f"{single_project_card.median_cost_ratio:.3f}" == "0.500"
    assert "0.500" in row


def test_a_project_with_only_zero_spend_specs_renders_unavailable():
    """A project whose every spec did zero work has no priced population at
    all, so its median must render as the same "unavailable" marker the
    per-project cell already uses for an empty ratio list (`—`) — never as
    `0.000`, which would misreport a non-result as a perfect cost win."""
    dead1 = _score(task_id="dead1", nh=0, orig=1000, nh_cache_tokens=0,
                   nh_cache_creation_tokens=0)
    dead1.project = "dead"
    dead2 = _score(task_id="dead2", nh=0, orig=500, nh_cache_tokens=0,
                   nh_cache_creation_tokens=0)
    dead2.project = "dead"

    real = _score(task_id="real2", nh=500, orig=1000, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0)
    real.project = "q"

    card = NorthStarCard(scores=[dead1, dead2, real])
    md = render_northstar_md(card)
    row = _project_row(md, "dead")

    assert "| — |" in row, row
    assert "0.000" not in row, row
    assert "0.0 " not in row, row


def test_per_project_count_beside_the_median_is_the_priced_population():
    """The number rendered beside the per-project median must describe the
    population the median was actually taken over (the priced specs), not
    the full task count for that project — otherwise a reader can misread
    the `tasks` column as the cost denominator."""
    priced_a = _score(task_id="pa", nh=500, orig=1000, nh_cache_tokens=0,
                       nh_cache_creation_tokens=0)
    priced_a.project = "p"
    priced_b = _score(task_id="pb", nh=300, orig=1000, nh_cache_tokens=0,
                       nh_cache_creation_tokens=0)
    priced_b.project = "p"
    dead = _score(task_id="pd", nh=0, orig=1000, nh_cache_tokens=0,
                  nh_cache_creation_tokens=0)
    dead.project = "p"

    other = _score(task_id="other2", nh=200, orig=1000, nh_cache_tokens=0,
                    nh_cache_creation_tokens=0)
    other.project = "q"

    card = NorthStarCard(scores=[priced_a, priced_b, dead, other])
    md = render_northstar_md(card)
    row = _project_row(md, "p")

    # bench cost ratio part 2: the median cell also carries its basis label
    # now, so the priced-population count is "(2, <basis>)", not a bare "(2)".
    assert "(2, cache-weighted)" in row, row
    assert "| 3 |" in row, row  # `tasks` column: still the full population


def test_the_per_spec_cost_cell_refuses_to_call_zero_spend_a_win():
    """`nh bench run`'s per-spec line printed `cost×0.00` for a dead spec.

    A spec that crashed/skipped/escalated before any model call has
    `cost_ratio == 0.0` — pinned deliberately above, because nh/orig really is
    zero. The AGGREGATE already refuses to read that as a cost win
    (`priced_scores` drops both None and 0.0). The per-spec cell did not, so
    the run printed the best possible cost result next to a ❌ while the median
    below it was honest.
    """
    from no_human.cli.commands import _bench_cost_cell
    assert _bench_cost_cell(0.0, "tier-weighted") == "cost n/a", \
        "zero spend is not a cost win"
    assert _bench_cost_cell(None, "tier-weighted") == "cost n/a", \
        "no baseline is not a cost win"
    # Control: a real ratio still renders, so the guard is not swallowing
    # everything — including one below 0.005, which rounds to 0.00 but is real.
    assert _bench_cost_cell(0.09, "tier-weighted") == "cost×0.09 (tier-weighted)"
    assert _bench_cost_cell(0.001, "cache-weighted") == "cost×0.00 (cache-weighted)"


def test_the_per_spec_cost_cell_cannot_render_a_ratio_without_its_basis():
    """Structural pin (bench cost ratio part 2): `basis` has no default, so
    a call site that only has the number literally cannot compile a
    rendering call without also supplying it — unlike a runtime check, this
    cannot be forgotten by a NEW call site added later.

    This is the mechanism, not just a convention: no code path anywhere in
    this tree CAN render an unlabeled cost ratio through this function,
    because the interpreter itself refuses the call.
    """
    import inspect

    from no_human.cli.commands import _bench_cost_cell

    sig = inspect.signature(_bench_cost_cell)
    params = list(sig.parameters.values())
    assert params[1].name == "basis"
    assert params[1].default is inspect.Parameter.empty, (
        "basis must be REQUIRED — a default would let a call site render "
        "a bare, unlabeled ratio")

    with pytest.raises(TypeError):
        _bench_cost_cell(0.09)  # type: ignore[call-arg]


def test_the_published_report_carries_the_generators_judge_calibration_pointer():
    """The pointer to `eval/JUDGE_CALIBRATION.md` exists in two places — the
    generator that emits it and the tracked report that already carries it —
    and a second copy of the truth ages badly. This is what keeps them equal.

    The line is read OUT OF THE GENERATOR rather than restated here: a test
    that hardcodes the sentence is a third copy, and the first one to drift.
    """
    from pathlib import Path

    card = NorthStarCard(label="x", created_at="2026-07-14",
                         scores=[_score(task_id="a")])
    card.scores[0].subset = "core"
    rendered = [line for line in render_northstar_md(card).splitlines()
                if "JUDGE_CALIBRATION.md" in line]
    assert len(rendered) == 1, (
        "the generator no longer emits exactly one judge-calibration pointer: "
        f"{rendered}"
    )

    report = Path(__file__).resolve().parents[1] / "docs" / "NORTH_STAR_BENCH.md"
    text = report.read_text(encoding="utf-8")
    assert rendered[0] in text, (
        "docs/NORTH_STAR_BENCH.md does not carry the pointer the generator "
        f"emits. Expected this line verbatim:\n  {rendered[0]}"
    )


# --------------------------------------------------------------------------- #
# Per-task `cache` column — the earliest signal an attempt is heading for
# budget exhaustion, surfaced per row (not just the fleet-wide
# metrics.py cache_economics total).
# --------------------------------------------------------------------------- #

def test_per_task_table_has_a_cache_column_and_mean_footer():
    """A two-run fixture: one row with cache tokens recorded (share 0.9), one
    row with none at all (must print '—' and be excluded from the mean, not
    counted as a 0.0 — the same contract cache_read_share() itself has)."""
    a = _score(task_id="ns-cache-a", nh_cache_tokens=900, nh_cache_creation_tokens=100)
    b = _score(task_id="ns-cache-b", nh_cache_tokens=0, nh_cache_creation_tokens=0)
    for s in (a, b):
        s.subset = "core"
    card = NorthStarCard(scores=[a, b], created_at="2026-08-23", label="t")
    md = render_northstar_md(card)

    header_line = next(ln for ln in md.splitlines() if ln.startswith("| task |"))
    assert "cache" in header_line, header_line

    row_a = next(ln for ln in md.splitlines() if ln.startswith("| ns-cache-a "))
    assert "90%" in row_a, row_a

    row_b = next(ln for ln in md.splitlines() if ln.startswith("| ns-cache-b "))
    cells_b = [c.strip() for c in row_b.strip("|").split("|")]
    assert cells_b[-2] == "—", row_b  # cache cell, right before notes

    footer = next(ln for ln in md.splitlines() if "Mean cache-read share" in ln)
    assert "1 core run" in footer, footer
    assert "90%" in footer, footer


def test_cache_column_does_not_shift_the_escalation_cells():
    """The cache column was appended at the very END of the row, after
    `attempts`/`tokens @ escalation` — those two cells must stay at the same
    index regardless of the new column, matching the invariant
    test_bench_escalation_latency.py already pins."""
    s = _score(task_id="ns-cache-c", nh_cache_tokens=0, nh_cache_creation_tokens=0)
    s.subset = "core"
    card = NorthStarCard(scores=[s], created_at="2026-08-23", label="t")
    md = render_northstar_md(card)

    row = next(ln for ln in md.splitlines() if ln.startswith("| ns-cache-c "))
    cells = [c.strip() for c in row.strip("|").split("|")]
    # task | outcome | satisfied | nh tokens | orig tokens | cost ratio |
    # orig follow-ups (proxy) | attempts | tokens @ escalation | cache | notes
    assert cells[7] == "—", row  # attempts (not escalated)
    assert cells[8] == "—", row  # tokens @ escalation (not escalated)
