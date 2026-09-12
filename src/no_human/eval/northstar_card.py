"""North-star benchmark scorecard + regression gate + report (plan Task A4).

Aggregates per-task ``BenchScore``s into the headline answer the whole program
exists to give: *does no_human complete the operator's real historical tasks
unattended, and at what token cost relative to the original babysat session?*

The gate blocks a key change when the success rate drops or the median token
ratio regresses beyond a threshold — mirroring ``scorecard.ci_gate``'s shape
so both gates read the same way. Never a numeric self-score: every number here
is measured, not judged.
"""

from __future__ import annotations

from .vendor_terms import redact_for_publish
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from ..core.metrics import cache_read_share
from .northstar import BASIS_MIXED, BenchScore

RESULTS_DIR = Path(__file__).resolve().parents[3] / "eval" / "results" / "northstar"
REPORT_MD = Path(__file__).resolve().parents[3] / "docs" / "NORTH_STAR_BENCH.md"

# Two-sided normal quantile for 95%. A literal, not scipy: the lean-stack rule
# forbids adding a dependency for one number, and this one is fixed forever.
WILSON_Z_95 = 1.959963984540054


def wilson_interval(successes: int, n: int,
                    z: float = WILSON_Z_95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion, pure stdlib.

    WHY WILSON AND NOT ±1.96·sqrt(p(1-p)/n): the normal (Wald) interval is the
    one everybody writes and it is wrong exactly where this benchmark lives —
    small n and a proportion near 1.0. At 49/54 Wald gives [0.83, 0.98], an
    interval that runs off the end of the scale at 54/54 (width zero) and can
    include values above 1. Wilson gives [0.80, 0.96], stays inside [0,1] by
    construction, and never collapses to a point.

        centre = (p̂ + z²/2n) / (1 + z²/n)
        half   = z·sqrt(p̂(1-p̂)/n + z²/4n²) / (1 + z²/n)

    Returns ``None`` for n == 0 — no observations is not a 0-width interval at
    zero, and every caller must render "n/a" rather than a number it did not
    measure.

    VALIDATION RUNS FIRST (review C6). The n<=0 early return used to swallow a
    NEGATIVE n as "nothing ran", so an n computed wrong — a subtraction that
    went the wrong way, a count read from a field that was not there — returned
    the same ``None`` as an honest empty run and was rendered as "n/a". A
    negative count is not an absence of observations, it is a broken caller,
    and it now says so where it happens rather than three surfaces downstream.
    """
    if n < 0:
        raise ValueError(f"n {n} is negative — a count, not an absence")
    if successes < 0 or successes > n:
        raise ValueError(f"successes {successes} outside 0..{n}")
    if n == 0:
        return None
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    # Clamp: the algebra keeps the interval inside [0,1] for every valid input,
    # so this only bounds float error at the extremes (0/n and n/n).
    return (max(0.0, centre - half), min(1.0, centre + half))


def intracluster_correlation(per_spec: dict[str, tuple[int, int]]) -> float:
    """ρ̂ — how much of a trial's outcome is decided by WHICH SPEC it is.

    Trials of one spec are not independent observations. A spec the agent can
    do passes every trial; a spec it cannot fails every trial; and both are one
    fact repeated N times, not N facts. Pooling them and handing the total to a
    binomial interval is the "clustered data, unclustered interval" error, and
    it is not a rounding-level one: a reviewer measured the pooled Wilson
    interval covering the true corpus rate **49.6% of the time** at a nominal
    95% — a coin flip wearing a confidence level.

    ρ̂ is the ANOVA (one-way random effects) intraclass correlation, computed
    from the per-spec pass counts in pure stdlib::

        MSB = Σ nᵢ(ȳᵢ - ȳ)² / (k-1)          between specs
        MSW = Σ nᵢȳᵢ(1-ȳᵢ) / (N-k)           within specs (binary shortcut)
        m₀  = (N - Σnᵢ²/N) / (k-1)            trials per spec, size-adjusted
        ρ̂   = (MSB - MSW) / (MSB + (m₀-1)MSW)

    Clamped to [0, 1]: the estimator is unbiased and can therefore land
    negative when specs vary LESS than chance, which is a sampling artefact and
    not a reason to claim more information than there are observations.

    THE TWO DEGENERATE CASES, and why each answers as it does:

    - **fewer than two specs, or one trial each (N == k).** There is no
      within-spec variation to measure, so ρ̂ is undefined. Returns 0.0 —
      and the design effect it feeds is 1 + (m̄-1)·ρ̂, which is exactly 1
      at one trial per spec whatever ρ̂ is, so the value is not load-bearing
      there. This is what makes the single-trial reduction exact.
    - **MSW == 0: every spec's trials agree unanimously.** Undefined by the
      formula (0/0 once MSB is also 0), maximal clustering by inspection —
      every repeat of a spec reproduced the first one. Returns 1.0, the
      CONSERVATIVE answer: the trials are treated as adding no independent
      information at all, which collapses the effective n back to one per
      spec. Returning 0.0 here would hand a perfectly-repeatable corpus the
      full row count and produce the tightest interval on the least
      informative data — the exact inversion this function exists to stop.
    """
    groups = [(p, n) for p, n in per_spec.values() if n > 0]
    k = len(groups)
    total = sum(n for _, n in groups)
    if k < 2 or total <= k:
        return 0.0
    grand = sum(p for p, _ in groups) / total
    msb = sum(n * (p / n - grand) ** 2 for p, n in groups) / (k - 1)
    # Σⱼ(yᵢⱼ - ȳᵢ)² == nᵢȳᵢ(1-ȳᵢ) == pᵢ(1 - pᵢ/nᵢ) for 0/1 outcomes.
    msw = sum(p * (1 - p / n) for p, n in groups) / (total - k)
    if msw <= 0:
        return 1.0
    m0 = (total - sum(n * n for _, n in groups) / total) / (k - 1)
    denom = msb + (m0 - 1) * msw
    if denom <= 0:
        return 1.0
    return min(1.0, max(0.0, (msb - msw) / denom))


# --------------------------------------------------------------------------- #
# THE SUCCESS PREDICATE — one definition, every consumer.
#
# `NorthStarCard` counts scores it holds as `BenchScore` objects; the paired
# comparison in `bench_compare` reads the SAME rows straight out of a results
# JSON, as dicts. Two readers of one fact is exactly how a second, subtly
# different rule gets written — so both call the functions below and neither
# spells the rule out again. They accept either shape deliberately: a shared
# predicate that only one of its two callers can call is not shared.
#
# THE RULE IS `goal_satisfied`, AND THAT ALREADY COVERS ESCALATION. For a spec
# whose only correct outcome is an honest stop, the runner assigns
# `score.goal_satisfied = score.escalated_honestly` (northstar.py, the
# `spec.expect_escalation` branch) BEFORE the score is ever written. So an
# expect-escalation spec that stopped honestly arrives here already carrying
# goal_satisfied=True, and re-deriving "success = escalated_honestly if
# expected_escalation else goal_satisfied" downstream would be a SECOND rule
# that happens to agree today — including on the case where the judge was
# skipped (goal_satisfied None) and the second rule would silently disagree.
SKIPPED_STATUS = "skipped"


def _score_field(score: Any, name: str) -> Any:
    """One field off a `BenchScore` or off a raw results-file score dict."""
    if isinstance(score, dict):
        return score.get(name)
    return getattr(score, name)


def score_ran(score: Any) -> bool:
    """Did this row produce a measurement at all? A skip is not a failure —
    it never ran, so it belongs in no numerator and no denominator."""
    return _score_field(score, "outcome_status") != SKIPPED_STATUS


def score_succeeded(score: Any) -> bool:
    """Did this ONE trial of ONE spec succeed? See the block comment above."""
    return bool(_score_field(score, "goal_satisfied"))


def score_did_priced_work(score: BenchScore) -> bool:
    """Did this row spend ANYTHING, priced the way the cost median prices it?

    THE one "did no work" predicate — every consumer (``priced_scores``,
    ``dead_specs``/``dead_spec_count``, the success denominator, and
    ``publish_refusals``' saturation rule) asks this function, none re-derives
    it. It existed as TWO disagreeing rules in this file: ``dead_specs`` gated
    on raw ``nh_tokens`` while ``priced_scores`` gated on the priced quantity,
    so a cache-only row (nh_tokens == 0 with real cache-read spend — the
    normal shape on resumed/attempt-2 runs) was simultaneously REAL WORK to
    the cost median and DEAD to the death counter.

    Gates on ``nh_priced_tokens`` — the cost median's own numerator — not on
    any one token class. The weights are all positive, so a row this rejects
    burned zero of EVERY class: the SDK died before any model call. The
    baseline half of ``priced_scores`` (``cost_ratio is not None``) is
    deliberately NOT part of this predicate: a missing ORIGINAL baseline makes
    a row unpriceABLE, not dead — real work with nothing to divide by.

    ``BenchScore``-only on purpose, unlike ``score_ran``/``score_succeeded``:
    every consumer holds card scores, and the dict path (``bench_compare``)
    never asks this question — a dict branch here would be untested code
    waiting to drift.
    """
    return bool(score.nh_priced_tokens)


def cost_ratio_basis(scores: list[BenchScore]) -> str:
    """The single basis label for a POPULATION of scores' cost ratios —
    what every rendering surface (report card headline, per-project table,
    CLI, web JSON) prints so a reader can never mistake which price table a
    published median was actually computed against.

    ``BenchScore.cost_ratio_basis`` answers the question for one row; this
    is the ONE place that collapses a population of rows down to one label,
    so the report and the web panel cannot each invent their own rule for
    "what do we call it when the rows disagree". Empty input has no ratio to
    label at all ("n/a" — never a bare pass-through of an empty string).
    Uniform input reports that basis. A population straddling the part-1
    change (an old score loaded from a pre-tier-weighting ``latest.json``
    alongside a freshly-run one) is ``BASIS_MIXED`` — never silently
    reported as either pure basis.
    """
    bases = {s.cost_ratio_basis for s in scores}
    if not bases:
        return "n/a"
    if len(bases) > 1:
        return BASIS_MIXED
    return next(iter(bases))


def published_file() -> Path:
    """`latest.json` records the last `bench publish` CALL, clean or
    `--force`d over refusals. This file records only the last CLEAN one
    (SCRUM-25): `bench_publish` writes it only when the run needed no
    override, so a forced publish over a probe can clobber `latest.json`
    without erasing the real baseline the Stats panel should headline.

    A function, not a constant: derived from ``RESULTS_DIR`` at CALL time so
    it follows the same module-attribute monkeypatch tests already use for
    ``RESULTS_DIR`` itself (a constant fixed at import time would keep
    pointing at the real project directory after a test repoints
    ``RESULTS_DIR`` to a tmp_path).
    """
    return RESULTS_DIR / "published_baseline.json"


@dataclass
class NorthStarCard:
    scores: list[BenchScore] = field(default_factory=list)
    created_at: str = ""
    label: str = ""            # e.g. "baseline" or the change under test
    # How many specs the canonical corpus HAS, independent of what this run
    # loaded. Without it, coverage is a ratio over the loaded set and therefore
    # blind to filtering: pointing --specs-dir at the specs that still resolve
    # makes a broken corpus read as a perfect one. 0 = unknown (older cards).
    corpus_available: int = 0
    # Refusals a human overrode with `nh bench publish --force`. Carried into
    # the saved card and rendered at the top of the report: a forced publish is
    # allowed, but it must never be able to look like a clean one.
    override_reasons: list[str] = field(default_factory=list)
    # How many times `nh bench run` replayed EACH spec (V1). 1 = today's
    # single-run shape. 0 means UNKNOWN — set only by `load()` when the file on
    # disk records no trials at all, i.e. it was written by a build that could
    # not measure repeat variance. A card constructed in memory declares 1
    # because that is what its scores are: one observation per spec.
    trials: int = 1
    # Did the file this card came from record a confidence interval? True for
    # any card built in memory (the interval is computed from the scores on
    # demand); False for a loaded file whose aggregate has no CI fields. A
    # published number without an interval is the thing V1 exists to stop, and
    # "the file said so" is the only way to tell after a round-trip.
    ci_recorded: bool = True
    # Did the file this card came from record the unscoreable field at all?
    # True for any card built in memory. False for a loaded file predating
    # this field — WITHOUT this flag a pre-change results file would render a
    # confident "0", indistinguishable from a run that measured zero
    # unscoreable specs. Presence, not truthiness, mirrors ``ci_recorded``.
    unscoreable_recorded: bool = True
    # Did the file this card came from record the pin_rederived field at
    # all? True for any card built in memory. False for a loaded file
    # predating this field — WITHOUT this flag a pre-change results file
    # would render a confident "0 of N", indistinguishable from a run that
    # measured zero re-derived pins. Presence, not truthiness, mirrors
    # ``unscoreable_recorded``.
    pin_rederived_recorded: bool = True
    # Set by `bench_run` when a quota-saturation halt cut the run short
    # (`quota_halt.HALTED_REASON_QUOTA`); "" for a run that completed (or a
    # legacy file predating this field). A run-status flag, not an aggregate
    # statistic — it never changes what `publish_refusals` decides; a halted
    # partial is already refused by the ordinary coverage rules.
    halted_reason: str = ""

    # ------------------------------ counts --------------------------------- #

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def ran(self) -> list[BenchScore]:
        return [s for s in self.scores if score_ran(s)]

    @property
    def skipped(self) -> int:
        return self.total - len(self.ran)

    @property
    def satisfied(self) -> int:
        return sum(1 for s in self.ran if score_succeeded(s))

    @property
    def measured_scores(self) -> list[BenchScore]:
        """Ran rows that did priced work — the success population.

        Three tiers, one exclusion rule applied twice: a SKIP is a decision
        and never enters ``ran``; a DEATH ran but measured nothing (the SDK
        died before any model call), so it leaves this list on exactly the
        same reasoning. What remains is the population every success figure
        divides over — counting a death as a quality failure makes a quota
        outage read as a quality regression (run 2: 2 dead of 5 rows was the
        difference between a reported 20% and a measured 33%)."""
        return [s for s in self.ran if score_did_priced_work(s)]

    @property
    def success_rate(self) -> float:
        """Pooled pass rate over the rows that DID PRICED WORK.

        Numerator and denominator over the SAME population: a dead
        expect-escalation row can carry goal_satisfied=True (the runner scores
        a pre-model death as escalated-honestly), so counting ``satisfied``
        (which pools every ran row) over a measured-only denominator could
        exceed 1.0."""
        measured = self.measured_scores
        if not measured:
            return 0.0
        return sum(1 for s in measured if score_succeeded(s)) / len(measured)

    @property
    def success_rate_incl_dead(self) -> float:
        """The pre-P0.1 pooled rate: every ran row, deaths included. Kept in
        the aggregate under its own honest name for continuity with older
        cards — NEVER the headline, because its denominator counts rows that
        measured nothing."""
        return self.satisfied / len(self.ran) if self.ran else 0.0

    # ------------------------- trials (V1) ---------------------------------- #
    # Every count above is over SCORES — one row per (spec, trial). With
    # `--trials 3` that is three rows per spec, so a rule written against
    # `len(ran)` silently triples its own denominator. The properties below
    # count SPECS instead, and every floor/comparison that is about "how much
    # of the corpus did we look at" uses them. For a single-trial run the two
    # are identical, so nothing existing changes meaning.

    @property
    def spec_count(self) -> int:
        """Distinct specs this card covers, however many trials each ran."""
        return len({s.task_id for s in self.scores})

    @property
    def ran_spec_count(self) -> int:
        """Distinct specs with at least one trial that was not skipped."""
        return len({s.task_id for s in self.ran})

    @property
    def per_spec_passes(self) -> dict[str, tuple[int, int]]:
        """``{task_id: (passes, trials_that_measured)}`` over the rows that
        did priced work — the same population every success figure divides
        over. A trial whose SDK died before any model call is no observation
        of the spec, so it adds to neither element; a spec dead in EVERY
        trial has no entry at all and renders as "— (not measured)", which is
        what happened, rather than a measured 0/N, which is not."""
        out: dict[str, list[int]] = {}
        for s in self.measured_scores:
            row = out.setdefault(s.task_id, [0, 0])
            row[0] += 1 if score_succeeded(s) else 0
            row[1] += 1
        return {k: (v[0], v[1]) for k, v in out.items()}

    @property
    def spec_mean_success_rate(self) -> float:
        """Corpus mean of PER-SPEC means — the headline under trials.

        Differs from ``success_rate`` (which pools every trial) only when specs
        ran unequal numbers of trials, e.g. a resumed run that died partway:
        pooling then weights the specs that happened to run more often. With
        balanced trials the two are identical, which is why the single-trial
        number published so far does not move.
        """
        per = self.per_spec_passes
        if not per:
            return 0.0
        return sum(p / n for p, n in per.values()) / len(per)

    @property
    def intracluster_correlation(self) -> float:
        """ρ̂ over this card's specs — see the module function."""
        return intracluster_correlation(self.per_spec_passes)

    @property
    def effective_n(self) -> float:
        """The number of INDEPENDENT observations behind the headline.

            m̄     = rows / S            trials per spec, actual
            DEFF  = 1 + (m̄ - 1)·ρ̂       Kish's design effect
            n_eff = max(S, rows / DEFF)

        Rows, divided by how much each spec's trials duplicate one another.
        The floor at S is not a fudge: at ρ̂ = 1 the algebra already gives
        rows/m̄ = S exactly, so it only bounds float error and the ragged case
        where m̄ is an average over unequal group sizes.

        **REDUCTION AT ONE TRIAL, exactly.** With one trial per spec, rows == S,
        so m̄ == 1 and DEFF == 1 + 0·ρ̂ == 1 IDENTICALLY — no dependence on ρ̂,
        no floating-point path through it. n_eff == rows == S, the same
        denominator the pooled interval used before this change, and
        ``spec_mean_success_rate * S`` is ``satisfied`` because each per-spec
        mean is 0 or 1. Every single-trial card therefore prints the interval
        it printed before, to the bit.

        MEASURED rows and specs, not ``ran`` — the same population
        ``spec_mean_success_rate`` (this interval's centre) is computed over.
        Counting dead rows here while the point estimate excludes them would
        put the point and the interval on different populations, the exact
        two-estimator defect the docstring below records; on a card with no
        deaths the two pairs are identical.
        """
        specs = len(self.per_spec_passes)
        rows = len(self.measured_scores)
        if specs <= 0:
            return 0.0
        deff = 1.0 + (rows / specs - 1.0) * self.intracluster_correlation
        if deff <= 0:
            return float(specs)
        return max(float(specs), rows / deff)

    @property
    def success_ci(self) -> tuple[float, float] | None:
        """Wilson 95% interval on the SAME estimator the headline reports.

        Point and interval must come from one estimator or the pair is not a
        measurement — the headline is ``spec_mean_success_rate`` (the mean of
        per-spec means, which is what survives uneven trials), so the interval
        is that rate carried onto ``effective_n``, the row count discounted by
        how much the trials of a spec repeat each other.

        THIS USED TO BE THE POOLED PAIR — ``wilson_interval(satisfied, rows)``
        — beside a spec-mean point estimate. Two defects in one line, both
        measured: the interval was centred on a different number than the one
        printed in front of it (a 12-spec resumed run printed 91.7% with an
        interval of 97.5–99.9, which EXCLUDES its own point estimate), and
        treating specs×trials rows as independent gave a nominal 95% interval
        49.6% real coverage under clustering.
        """
        n = int(round(self.effective_n))
        if n <= 0:
            return None
        # Clamped against float error only: the rate is a mean of per-spec
        # means and so is already in [0, 1].
        k = min(n, max(0, int(round(self.spec_mean_success_rate * n))))
        return wilson_interval(k, n)

    @property
    def pass_k_rate(self) -> float | None:
        """Fraction of specs that passed EVERY trial — reliability, not
        capability. A corpus can score 90% mean and 60% pass^3: two thirds of
        the specs are dependable and the rest are coin flips, and only this
        number tells them apart. ``None`` when nothing ran."""
        per = self.per_spec_passes
        if not per:
            return None
        return sum(1 for p, n in per.values() if p == n) / len(per)

    @property
    def has_trials_metadata(self) -> bool:
        """Does this card state how many times each spec was run? False only
        for a file written before trials existed."""
        return self.trials >= 1

    @property
    def has_ci(self) -> bool:
        """Is there an interval to publish alongside the headline?"""
        return self.ci_recorded and self.success_ci is not None

    # ------------------------------- cost ----------------------------------- #

    @property
    def median_token_ratio(self) -> float | None:
        vals = [s.token_ratio for s in self.ran if s.token_ratio is not None]
        return median(vals) if vals else None

    @property
    def priced_scores(self) -> list[BenchScore]:
        """Ran specs with a real, non-zero priced cost — the population
        ``median_cost_ratio`` (and its published denominator) is taken over.

        Gates on ``cost_ratio`` itself, not on ``nh_tokens`` alone: cost_ratio
        is price-weighted (nh_tokens + 0.1*cache_read + 1.25*cache_creation),
        so a spec can have nh_tokens == 0 yet real cache-read spend and a real
        non-zero cost_ratio — gating on raw nh_tokens would wrongly drop that
        spec too. Two exclusions, deliberately separate because they mean
        different things: no BASELINE (``cost_ratio is None`` — real work,
        nothing to divide by) and no SPEND (``score_did_priced_work`` false —
        a non-result, not a cost win). The spend half is the shared predicate
        ``dead_specs`` counts the complement of; re-testing spend locally is
        how the two definitions drifted apart in the first place."""
        return [s for s in self.ran
                if s.cost_ratio is not None and score_did_priced_work(s)]

    @property
    def median_cost_ratio(self) -> float | None:
        """Price-weighted (cache-aware) ratio — the honest headline; the plain
        token ratio is blind to cache-read, which is ~95% of real burn."""
        vals = [s.cost_ratio for s in self.priced_scores]
        return median(vals) if vals else None

    @property
    def median_cost_ratio_basis(self) -> str:
        """The basis label for `median_cost_ratio`, over the SAME population
        (`priced_scores`) the median itself is taken over — never a
        different predicate, or the label could describe rows the number was
        not computed from."""
        return cost_ratio_basis(self.priced_scores)

    @property
    def dead_specs(self) -> int:
        """Specs that ran but burned zero tokens — the SDK died before any model
        call. A skip is a decision and is excluded; this counts deaths only.

        The SAME "did priced work" predicate ``priced_scores`` gates its spend
        exclusion on — one definition, two consumers. It briefly gated on raw
        ``nh_tokens`` while the cost median gated on the priced quantity, and
        a cache-only row (normal on resumed runs, where cache-read dominates)
        was counted as real work by the median AND as an SDK death here."""
        return sum(1 for s in self.ran if not score_did_priced_work(s))

    @property
    def dead_spec_count(self) -> int:
        """Distinct SPECS with at least one dead trial.

        ``dead_specs`` above counts ROWS, so under ``--trials 3`` one spec that
        died every time reads as three. Any surface phrasing a count "of the
        corpus" needs this one; the refusal fraction deliberately keeps using
        the row count, because there it is rows ÷ rows and asking a different
        question (how saturated was the backend, per attempt)."""
        return len({s.task_id for s in self.ran
                    if not score_did_priced_work(s)})

    @property
    def unscoreable_specs(self) -> int:
        """Rows whose GoalJudge produced no parseable verdict after the
        bounded re-ask retry — the judge broke, not the agent. Same unit as
        ``dead_specs`` (rows, not distinct specs).

        These rows REMAIN in the success denominator as not-satisfied — this
        is the conservative default, not an oversight. See
        ``render_northstar_md``'s unscoreable line for the operator-decision
        note on why they are not dropped."""
        return sum(1 for s in self.ran if _score_field(s, "unscoreable"))

    @property
    def unscoreable_spec_count(self) -> int:
        """Distinct specs with at least one unscoreable trial. See
        ``dead_spec_count`` for why rows and distinct specs are both
        needed."""
        return len({s.task_id for s in self.ran
                    if _score_field(s, "unscoreable")})

    @property
    def pin_rederived_specs(self) -> int:
        """Rows scored against a pin re-derived by date from a rewritten
        history (``bench_task.build_bench_tasks`` — never at run time)
        rather than the pin the original task actually saw. Counted over
        ``measured_scores``, not ``ran``: a re-derived pin on a row that
        never measured anything (skipped/dead) does not touch the ratio a
        reader is being warned about."""
        return sum(1 for s in self.measured_scores
                   if _score_field(s, "pin_rederived"))

    @property
    def pin_rederived_spec_count(self) -> int:
        """Distinct specs with at least one MEASURED trial run against a
        re-derived pin. See ``dead_spec_count`` for why rows and distinct
        specs are both needed."""
        return len({s.task_id for s in self.measured_scores
                    if _score_field(s, "pin_rederived")})

    @property
    def total_nh_tokens(self) -> int:
        return sum(s.nh_tokens for s in self.ran)

    @property
    def total_orig_tokens(self) -> int:
        return sum(s.orig_tokens for s in self.ran)

    # --------------------------- babysitting -------------------------------- #

    @property
    def corrections_avoided(self) -> int:
        """Follow-up user messages the original sessions needed, for tasks
        no_human completed unattended. HONEST LABEL (review F4): this is a
        PROXY — user_messages-1 counts every follow-up ("thanks", new
        sub-asks), not only true corrections. Classifying real corrections
        needs a utility-model pass (future work); every surface says
        "follow-ups (proxy)" until then."""
        return sum(s.orig_corrections for s in self.ran if s.goal_satisfied)

    @property
    def escalation_specs(self) -> int:
        """Denominator of the honest-escalation rate: specs whose only correct
        outcome is an honest stop.

        Published alongside the rate because the rate ALONE is unreadable:
        "100%" over 2 gated specs and "100%" over 14 are the same string, and
        the first is what a broken corpus produces after the other 12 fail to
        resolve and leave the denominator. A reader must be able to tell them
        apart without opening the raw card."""
        return sum(1 for s in self.ran if s.expected_escalation)

    @property
    def honest_escalations(self) -> int:
        """Numerator: gated specs that actually stopped honestly."""
        return sum(1 for s in self.ran
                   if s.expected_escalation and s.goal_satisfied)

    @property
    def corrections_avoided_delivered(self) -> int:
        """The part of `corrections_avoided` earned by DELIVERING something.

        `goal_satisfied` is also true for a gated spec that correctly REFUSED
        and handed the task back — the right outcome, but the human still has
        to do that work, so those follow-ups were not avoided. On v13, 251 of
        350 came from refused tasks and one escalated spec alone contributed
        96 (27% of the headline). Splitting it is the difference between an
        honest number and one that flatters by construction."""
        delivered = {"done", "awaiting_approval"}
        return sum(s.orig_corrections for s in self.ran
                   if s.goal_satisfied and s.outcome_status in delivered)

    @property
    def honest_escalation_rate(self) -> float:
        """Of the tasks whose only correct outcome is an honest stop
        (expect_escalation specs), how many stopped honestly (1.0 when none)."""
        if not self.escalation_specs:
            return 1.0
        return self.honest_escalations / self.escalation_specs

    # ---------------------------- persistence ------------------------------- #

    def as_dict(self) -> dict[str, Any]:
        ci = self.success_ci
        return {
            "created_at": self.created_at,
            "label": self.label,
            "aggregate": {
                "total": self.total, "skipped": self.skipped,
                "satisfied": self.satisfied,
                "success_rate": round(self.success_rate, 4),
                # The pre-P0.1 pooled rate, deaths still in the denominator —
                # kept under an honest name so old cards stay comparable, and
                # so the two rates diverging IS the saturation signal. Never
                # the headline.
                "success_rate_incl_dead": round(self.success_rate_incl_dead, 4),
                # The measured population's own pair, published so no surface
                # (the report headline, the web Stats fraction) has to
                # re-derive it from counts that include deaths. Spec-wise
                # denominator, trial-wise numerator: at one trial per spec the
                # two axes coincide and the pair IS the headline fraction; the
                # only surface printing them as a fraction guards trials == 1.
                "measured_specs": len(self.per_spec_passes),
                "measured_satisfied": sum(
                    1 for s in self.measured_scores if score_succeeded(s)),
                # --- V1: trials & intervals -------------------------------- #
                # ADDITIVE. Every field above keeps its meaning, so a card
                # written here still loads into a reader that knows nothing of
                # trials: a single-trial run has trials=1, spec_count==total
                # and spec_mean_success_rate == success_rate.
                "trials": self.trials,
                "specs": self.spec_count,
                "ran_specs": self.ran_spec_count,
                "spec_mean_success_rate": round(self.spec_mean_success_rate, 4),
                "success_ci_low": round(ci[0], 4) if ci else None,
                "success_ci_high": round(ci[1], 4) if ci else None,
                # The interval's own basis, recorded rather than left to be
                # re-derived: a reader (or the web panel) cannot tell a tight
                # interval earned by many independent specs from one asserted
                # over rows that repeat each other, and n_eff is the whole
                # difference. Equals `ran_specs` for every single-trial card.
                "success_n_eff": round(self.effective_n, 2),
                "intracluster_correlation": round(
                    self.intracluster_correlation, 4),
                "pass_k_rate": (round(self.pass_k_rate, 4)
                                if self.pass_k_rate is not None else None),
                "median_token_ratio": (round(self.median_token_ratio, 4)
                                       if self.median_token_ratio is not None
                                       else None),
                "median_cost_ratio": (round(self.median_cost_ratio, 4)
                                      if self.median_cost_ratio is not None
                                      else None),
                # Travels WITH the ratio on every wire payload (the web
                # panel's `_bench_payload` spreads this dict straight onto
                # the JSON response) so no renderer can print the number
                # without also having its basis at hand.
                "median_cost_ratio_basis": self.median_cost_ratio_basis,
                # Specs that RAN but burned zero tokens: the SDK died before any
                # model call. Published runs are under the refusal threshold by
                # construction, so this is the number that makes a *sub*-
                # threshold saturation legible instead of leaving a reader to
                # scan the per-task table for zeroes.
                "dead_specs": self.dead_specs,
                # Spec-wise companion to the row count above — what a "N of the
                # corpus went unmeasured" sentence has to be counted in.
                "dead_spec_count": self.dead_spec_count,
                # Rows whose GoalJudge produced no parseable verdict after the
                # bounded retry — the judge broke, not the agent. STILL COUNT
                # AS NOT SATISFIED in every rate above: this is visibility,
                # not a denominator change.
                "unscoreable_specs": self.unscoreable_specs,
                "unscoreable_spec_count": self.unscoreable_spec_count,
                # Measured rows/specs scored against a pin re-derived by date
                # from a rewritten history — a partially-re-derived corpus
                # must never be mistaken for a full-fidelity baseline.
                "pin_rederived_specs": self.pin_rederived_specs,
                "pin_rederived_spec_count": self.pin_rederived_spec_count,
                "total_nh_tokens": self.total_nh_tokens,
                "total_orig_tokens": self.total_orig_tokens,
                "corpus_available": self.corpus_available,
                "corrections_avoided": self.corrections_avoided,
                "corrections_avoided_delivered": self.corrections_avoided_delivered,
                "honest_escalation_rate": round(self.honest_escalation_rate, 4),
                # The rate's own numerator and denominator: "100% (2/2)" and
                # "100% (14/14)" must not render identically.
                "honest_escalations": self.honest_escalations,
                "escalation_specs": self.escalation_specs,
            },
            "override_reasons": self.override_reasons,
            # Top-level, not inside `aggregate`: `aggregate` is spread onto
            # the web `_bench_payload` and consumed field-by-field by
            # `bench_compare`, and a run-status flag is not an aggregate
            # statistic.
            "halted_reason": self.halted_reason,
            # Sorted so a saved card is deterministic regardless of execution
            # order — `--parallel` completes specs in a nondeterministic order,
            # and the tracked per-task report renders scores in list order, so
            # unsorted saves would make baseline-to-baseline diffs noisy.
            "scores": [s.as_dict()
                       for s in sorted(self.scores, key=lambda s: s.task_id)],
        }

    def save(self, path: Path) -> None:
        # Atomic write: the bench checkpoint is re-saved after every spec and
        # the process gets hard-killed on quota saturation ("Stream closed").
        # A truncate-in-place write caught mid-kill would leave a corrupt file
        # and silently discard every completed spec on the next --resume.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.as_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def load(path: Path) -> "NorthStarCard | None":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        scores = [BenchScore(
            task_id=s["task_id"], title=s["title"],
            outcome_status=s["outcome_status"],
            goal_satisfied=s["goal_satisfied"],
            escalated_honestly=s["escalated_honestly"],
            mergeable=s["mergeable"], nh_tokens=s["nh_tokens"],
            nh_cache_tokens=s["nh_cache_tokens"],
            nh_cache_creation_tokens=s.get("nh_cache_creation_tokens", 0),
            nh_turns=s["nh_turns"],
            nh_wall_clock_s=s["nh_wall_clock_s"],
            orig_tokens=s["orig_tokens"],
            orig_cache_tokens=s.get("orig_cache_tokens", 0),
            orig_cache_creation_tokens=s.get("orig_cache_creation_tokens", 0),
            orig_wall_clock_s=s["orig_wall_clock_s"],
            orig_corrections=s["orig_corrections"],
            expected_escalation=bool(s.get("expected_escalation", False)),
            subset=s.get("subset", "full"),
            project=s.get("project", ""),
            trial=int(s.get("trial") or 0),
            attempts_before_escalation=int(s.get("attempts_before_escalation") or 0),
            tokens_before_escalation=int(s.get("tokens_before_escalation") or 0),
            notes=s.get("notes", ""),
            events=s.get("events") or [],
            nh_role_tokens=s.get("nh_role_tokens") or {},
            nh_role_models=s.get("nh_role_models") or {},
            unscoreable=bool(s.get("unscoreable", False)),
            pin_rederived=bool(s.get("pin_rederived", False)),
        ) for s in data.get("scores", [])]
        agg = data.get("aggregate") or {}
        return NorthStarCard(scores=scores,
                             corpus_available=int(
                                 agg.get('corpus_available') or 0),
                             # 0 = the FILE said nothing about trials. Not
                             # defaulted to 1: "we ran each spec once" and "we
                             # have no idea how many times we ran each spec"
                             # are different claims, and only the first may be
                             # published. See `publish_refusals`.
                             trials=int(agg.get("trials") or 0),
                             # Presence, not truthiness: a legitimately null CI
                             # (nothing ran) still means the writer recorded
                             # one, and that card is refused for other reasons.
                             ci_recorded="success_ci_low" in agg,
                             unscoreable_recorded="unscoreable_specs" in agg,
                             pin_rederived_recorded="pin_rederived_specs" in agg,
                             created_at=data.get("created_at", ""),
                             label=data.get("label", ""),
                             override_reasons=list(
                                 data.get("override_reasons") or []),
                             halted_reason=data.get("halted_reason", ""))


def sample_phrase(card: NorthStarCard) -> str:
    """The ``n=`` clause — a sentence about the run that is ARITHMETICALLY TRUE.

    Three shapes, because a run has three, and the one string that used to
    cover all of them told two lies:

    - **balanced** — every ran spec got the same number of trials, and that
      number is the one the card declares: ``n=54×3=162``. The multiplication
      is checkable and it checks out.
    - **uneven** — a resumed run, where specs carry different trial counts:
      ``n=221 trials over 12 specs``. The old form printed ``n=12×20=221``,
      whose left side is arithmetically false (12×20 is 240) and whose middle
      term asserts every spec ran twenty times when one of them ran once. A
      product is not available here, so none is printed.
    - **trials unrecorded** — a file written before ``--trials`` existed:
      ``n=3 specs (trials unrecorded)``. This used to print ``n=3×1=3``,
      which states a trial count that nothing on the card measured. The one
      thing such a file does know is how many specs it covered.
    """
    # MEASURED specs and rows — the population behind the headline and its
    # interval. A trial whose SDK died before any model call is not an
    # observation, and an ``n=`` that counted it would claim more data than
    # the estimator in front of it used. The deaths are not hidden: the report
    # prints the dead count on its own line, labeled as infrastructure.
    per = card.per_spec_passes
    specs = len(per)
    rows = sum(n for _, n in per.values())
    if card.trials < 1:
        return f"n={specs} specs (trials unrecorded)"
    counts = {n for _, n in per.values()}
    if len(counts) == 1 and counts == {card.trials}:
        return f"n={specs}×{card.trials}={rows}"
    return f"n={rows} trials over {specs} specs"


def success_headline(card: NorthStarCard) -> str:
    """The success figure, NEVER as a bare percentage.

    ``90.7% (95% CI 80.1–96.0, n=54×3=162) · pass^3 63.0%``

    One function so the console line, the report and anything added later
    cannot print three different headline numbers from the same card — the
    single-run "≥90%" claim this replaces was quoted in four places and
    measured in none of them. The interval is always shown: a percentage with
    no interval is the failure mode, and 54 specs run once has an interval too
    (it is just embarrassingly wide, which is the point).

    ONE ESTIMATOR, both halves. The percentage is the mean of per-spec means
    and so is the interval's centre (``success_ci`` carries it onto the
    clustering-adjusted effective n). They were briefly different estimators —
    a spec-mean point beside a pooled-row interval — which on a resumed
    12-spec run printed ``91.7% (95% CI 97.5–99.9)``: an interval that does
    not contain the number standing in front of it. Never derive either half
    anywhere else.

    pass^k is appended only when k > 1 — with one trial per spec it is
    arithmetically identical to the mean and would read as corroboration it is
    not.
    """
    pct = card.spec_mean_success_rate
    ci = card.success_ci
    if ci is None:
        return f"{pct:.1%} (no interval — nothing ran)"
    out = (f"{pct:.1%} (95% CI {ci[0] * 100:.1f}–{ci[1] * 100:.1f}, "
           f"{sample_phrase(card)})")
    if card.trials > 1 and card.pass_k_rate is not None:
        out += f" · pass^{card.trials} {card.pass_k_rate:.1%}"
    return out


# A run must clear these before it may become the published baseline. They are
# derived from the SCORES rather than from run flags on purpose: a checkpoint
# written by an older build carries no record of its flags, and the incident
# these prevent is precisely that such a file overwrote the report.
MIN_PUBLISHABLE_SPECS = 10
MAX_DEAD_FRACTION = 0.2
# How much of the LOADED spec set may go unmeasured before a run stops being a
# verdict. "Loaded", not "the corpus": `total` counts what this run loaded after
# the subset filter, --specs-dir and --limit, so deleting specs outright makes
# coverage look perfect, and BOTH counts shrink together so the shortfall rule
# cannot see it either. Nothing in the gate catches that on a fresh clone; what
# does is that deleting corpus files is a visible git commit.
# Deliberately its own value, NOT an alias of MAX_DEAD_FRACTION: that one is a
# fraction of the specs that RAN (how saturated was the backend), this is a
# fraction of the LOADED spec set (how much of what this run picked up did we
# actually look at). Different numerator, different denominator, different question —
# aliasing them would mean retuning backend-saturation tolerance silently
# retunes corpus-coverage tolerance. 20% is chosen so a handful of
# legitimately-unrunnable specs cannot veto a run, while a corpus that has
# largely stopped resolving cannot be reported as a result.
#
# THE TWO TOLERANCES COMPOUND, and each docstring describes its rule alone.
# A run may load 80% of the corpus AND leave 20% of that unmeasured, so the
# combined floor is 0.8 x 0.8 = 64%: measured live, 45 loaded of 55 with 9
# unmeasured passes the gate with zero refusals while only 36 of 55 (65.5%)
# were actually measured. That is a deliberate tolerance, not a hole — it is
# far clear of the ~35% incident these rules exist to catch — but a reader
# will not derive it from either rule in isolation.
MAX_UNMEASURED_FRACTION = 0.2


# How much of the AVAILABLE corpus a run must actually load. Filtering is the
# documented reaction to a coverage refusal ("just run the ones that work"), and
# it defeats every ratio computed over the loaded set — 19 surviving specs of 55
# is a perfect 0/19 unmeasured and clears a 10-spec floor comfortably.
MIN_CORPUS_LOADED_FRACTION = 0.8


def corpus_shortfall(card: NorthStarCard, available_override: int = 0) -> str:
    """Why this run's spec set is too small a slice of the corpus, or ``""``.

    Compares LOADED against AVAILABLE, which is the only comparison that
    survives filtering. Returns "" when coverage is adequate — the common
    case — and DOES NOT APPLY AT ALL only when the card records no available
    count: an older card, or a checkout with no ``eval/northstar_tasks/``.

    Pointing ``--specs-dir`` at a smaller corpus is NOT an escape hatch — the
    available count is read from the canonical directory regardless, so an
    11-spec scratch run against a 55-spec corpus still refuses "11 of 55".

    ``available_override`` is the RUNTIME-ONLY tier denominator for `--quick`
    (SCRUM-43): the run-time gate grades a quick run against its own expected
    tier, but the override is never stored on the card — `publish_refusals`
    always calls this with the card's full-corpus count, which is what keeps
    a quick card structurally unpublishable as the baseline.
    """
    available = available_override or card.corpus_available
    # SPECS, not scores. `--trials 3` writes three scores per spec, so `total`
    # would report a 19-spec slice of a 55-spec corpus as 57 loaded and this
    # rule — the one that catches filtering to the specs that still resolve —
    # would wave it through. Identical to `total` for a single-trial run.
    loaded = card.spec_count
    if not available or loaded >= available * MIN_CORPUS_LOADED_FRACTION:
        return ""
    return (
        f"this run loaded {loaded} of {available} available spec(s) "
        f"({loaded / available:.0%} < {MIN_CORPUS_LOADED_FRACTION:.0%}) — "
        f"a filtered slice cannot stand for the corpus, and filtering to the "
        f"specs that still resolve is exactly how a broken corpus reads as a "
        f"perfect one")


def unmeasured_specs(card: NorthStarCard) -> tuple[int, int]:
    """``(unmeasured, total)`` — specs that yielded no measurement: *skipped*
    (never ran) plus *dead* (ran, but burned zero tokens).

    The denominator is the LOADED spec set, not ``ran``. A skipped spec leaves
    ``ran`` entirely, so a fraction measured against ``ran`` is structurally
    blind to it — which is exactly how a corpus that mostly failed to resolve
    reads as a *higher* success rate over the handful that survived.
    """
    return card.skipped + card.dead_specs, card.total


def pin_rederivation_note(card: NorthStarCard) -> str:
    """The `bench report`/`bench compare` disclosure line for
    ``pin_rederived_spec_count``: history rewrites make a spec's recorded
    pin unreachable, and `nh bench build` repairs it by re-deriving a pin
    from the spec's own recorded start date rather than guessing or
    dropping the spec (see ``bench_task.build_bench_tasks``). A run that
    measured some specs against a re-derived pin ran against TODAY's
    ancestor of that date, not the tree the original task actually saw —
    a partially-re-derived corpus must never be mistaken for a
    full-fidelity baseline, so this is surfaced unconditionally, including
    the 0 case.

    Three states, mirroring ``unscoreable_recorded``'s presence-vs-truthiness
    split (a results file predating this field records nothing — an absence
    of measurement, not a measured zero); counted over ``measured_scores``,
    the same population every success figure divides over.
    """
    if not card.pin_rederived_recorded:
        return ("- Specs replayed against a re-derived pin: **not recorded** "
                "— this results file predates the field; the 0 it would "
                "otherwise show is an absence of measurement, not a "
                "measured zero")
    k = card.pin_rederived_spec_count
    total = len(card.measured_scores)
    if k == 0:
        return (f"- Specs replayed against a re-derived pin: **0** of "
                f"{total} measured — every measured spec ran against the "
                f"pin the original task actually saw")
    return (f"- Specs replayed against a re-derived pin: **{k}** of {total} "
            f"measured  ⚠ these ran against a commit date-derived from a "
            f"rewritten history (`pin_rederived`), not the tree the "
            f"original task actually saw — read this run as PARTIALLY "
            f"re-derived, never as a full-fidelity baseline")


def publish_refusals(card: NorthStarCard,
                     previous: NorthStarCard | None = None) -> list[str]:
    """Why *card* must not become the published baseline. Empty means it may.

    Three incidents, one root cause — the runner treated every run as
    authoritative, so a probe and a quota death both overwrote the committed
    report and the gate baseline:

    - **dead specs.** A spec that burned zero tokens did not run; the SDK died
      ("Stream closed") before any model call. Those score as failed-and-
      honestly-escalated, which *inflates* honest-escalation while crushing
      success — an infrastructure failure wearing a capability result's clothes.
      Checking a FRACTION rather than "any" is deliberate: the v14 shape was one
      working spec followed by 96 dead ones, which an any-check waves through.
      Skipped specs are excluded — a skip is a decision, not a death.
    - **a slice is not the corpus.** A capped or partial run scored some specs,
      not the benchmark, and must not replace a baseline built from more.
    - **regression gates compare against this file.** Publishing a narrower run
      silently redefines what "no regression" means for every run after it.
    """
    reasons: list[str] = []
    ran = card.ran
    if not ran:
        reasons.append(
            f"nothing ran: {card.total} spec(s), all skipped — a selection "
            f"problem, not a result")
        return reasons

    # The card's own death notion (shared "did priced work" predicate), not a
    # locally re-derived zero-token test — a third copy of the rule is exactly
    # how dead_specs and priced_scores came to disagree.
    dead = [s for s in ran if not score_did_priced_work(s)]
    if len(dead) / len(ran) > MAX_DEAD_FRACTION:
        reasons.append(
            f"{len(dead)}/{len(ran)} specs burned zero tokens "
            f"({len(dead) / len(ran):.0%} > {MAX_DEAD_FRACTION:.0%}) — the "
            f"backend was saturated, so this measures the quota, not no_human")

    # Coverage, the SAME question the gate asks, through the SAME helper. The
    # dead rule above asks "was the backend saturated" (dead ÷ ran); this asks
    # "did we look at the benchmark at all" (skipped + dead ÷ loaded). Without
    # it the incident shape published clean: 36 of 55 specs pinned at a repo
    # that no longer exists are SKIPS, so zero were dead, 19 ran (over the
    # floor), and a fresh clone has no baseline to narrow against — every rule
    # passed and a 65%-unmeasured run became the committed baseline.
    shortfall = corpus_shortfall(card)
    if shortfall:
        reasons.append(shortfall)

    unmeasured, loaded = unmeasured_specs(card)
    if loaded and unmeasured / loaded > MAX_UNMEASURED_FRACTION:
        reasons.append(
            f"{unmeasured}/{loaded} specs went unmeasured "
            f"({unmeasured / loaded:.0%} > {MAX_UNMEASURED_FRACTION:.0%}) — "
            f"skipped or dead; too little of the benchmark was measured for "
            f"this run to be the baseline every later run is compared against")

    # SPECS, not scores: `--limit 3 --trials 4` measures 3 specs and would
    # otherwise clear a 10-spec floor with 12 rows drawn from a probe.
    if card.ran_spec_count < MIN_PUBLISHABLE_SPECS:
        reasons.append(
            f"only {card.ran_spec_count} spec(s) ran (minimum "
            f"{MIN_PUBLISHABLE_SPECS}) — a probe or a capped run is a slice, "
            f"not the corpus")

    # Compared on RAN, not total. Every headline — success_rate,
    # honest_escalation_rate, median_cost_ratio — is computed over ran, and
    # skipping is the documented dominant failure mode (the specs pin to local
    # repo paths). A run that loads 56 specs and skips 40 has the same `total`
    # as the baseline and would publish "100% success" measured over 16.
    # Distinct specs on BOTH sides, so raising --trials cannot buy its way past
    # a broader baseline (3 trials over 20 specs is 60 scores against a 55-spec
    # baseline's 55 — narrower, and it would have read as broader).
    if previous is not None and card.ran_spec_count < previous.ran_spec_count:
        reasons.append(
            f"this run ran {card.ran_spec_count} spec(s) but the current "
            f"baseline '{previous.label or 'unlabelled'}' ran "
            f"{previous.ran_spec_count} — publishing would narrow what every "
            f"later regression gate checks")

    # V1. A published headline with no interval is the defect this whole
    # mechanism exists to remove: "90.7%" from 54 single runs is [80%, 96%],
    # and nothing on the card said so. These two refusals are what make the
    # missing metadata visible instead of assumed.
    #
    # They fire on a card whose FILE recorded neither (written by a build that
    # predates trials). `--force` still publishes it, and — as with every other
    # refusal here — the reason is recorded in `override_reasons` and rendered
    # in the report's warning banner, so a run published without an interval
    # can never look like one that had it.
    if not card.has_trials_metadata:
        reasons.append(
            "this results file records no trial count — it predates "
            "`--trials`, so nothing here states how many times each spec was "
            "run and the headline cannot be read as a measurement; re-run it "
            "with `nh bench run --trials N`")
    if not card.has_ci:
        reasons.append(
            "this results file carries no confidence interval on the success "
            "rate — a bare percentage over a corpus this size is not a claim "
            "anyone can check; re-run it so the card records its own interval")

    return reasons


@dataclass
class NorthStarGate:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def northstar_gate(current: NorthStarCard,
                   previous: NorthStarCard | None, *,
                   max_ratio_regression: float = 0.5,
                   max_success_drop: float = 0.0,
                   tier_expected: int = 0) -> NorthStarGate:
    """Block a key change when the bench regresses vs the previous run.

    - success rate must not drop by more than ``max_success_drop`` (default:
      any drop blocks);
    - median token ratio must not grow by more than ``max_ratio_regression``
      (absolute, e.g. 0.80 → 1.31 blocks at the default 0.5).

    A first run has no baseline to compare against, so it skips the regression
    comparisons — but it must still clear COVERAGE and, because coverage cannot
    see specs that were never loaded, an absolute floor.

    BEHAVIOUR CHANGES for existing `--gate` users, stated rather than buried:

    - a run NARROWER than the baseline now blocks;
    - a run that loads well under the AVAILABLE corpus now blocks, with or
      without a baseline — this is what catches filtering to the specs that
      still resolve;
    - `--limit N --gate` blocks well before `MIN_PUBLISHABLE_SPECS`: the
      loaded-vs-available rule refuses anything under 80% of the corpus, so on
      a 55-spec corpus the real boundary is N < 44. It also blocks against any
      baseline it is narrower than;
    - the FIRST run of any corpus with fewer than `MIN_PUBLISHABLE_SPECS`
      runnable specs now blocks;
    - a baseline published from a `--full` run will red a default (core)
      invocation IF `generated/` is populated. It is empty today, so `--full`
      currently loads the same specs and changes nothing.

    Every comparison above is computed over ``ran``, so a spec that never ran
    LEAVES the denominator instead of depressing it. That makes a broken
    instrument read as an improvement: when most of the corpus fails to resolve
    (or the backend dies), the survivors are the healthy specs and the headline
    goes UP, and without the checks below a run measuring a third of the corpus
    reports a better-than-baseline number and exits 0. ``publish_refusals``
    applies the SAME coverage rule through the same helper, so the two cannot
    disagree about whether a run measured enough to mean anything — they did
    disagree until the incident shape (36 of 55 specs pinned at a repo that no
    longer exists) was rejected by the gate and published clean.

    Coverage is therefore checked BEFORE the first-run early return. It needs
    nothing from *previous*, and ``eval/results/northstar/`` is gitignored — so
    ``previous`` is None in every fresh clone and CI checkout. Gating the check
    behind a baseline would exempt exactly the configuration where a broken
    corpus is most likely to be published as one.

    Two limits worth stating rather than implying:

    - **Coverage is counted, membership is not.** A run that measures the same
      NUMBER of specs drawn from a different (say, easier) population passes
      these checks. Corpus churn is legitimate and frequent here, so a rule
      that blocked any change in spec membership would be permanently red;
      catching a laundered population needs comparing task_ids against the
      baseline, which is deliberately not attempted here.
    - **A skip counts against coverage, though ``publish_refusals`` treats a
      skip as a decision rather than a death.** The two ask different
      questions and the divergence is intentional: a spec that can never run is
      still a spec the benchmark did not measure. The consequence is that
      persistently-unrunnable specs must be REMOVED from the corpus, not left
      to be skipped forever — otherwise their fixed cost eventually crosses the
      ceiling and the gate is red for good.
    """
    reasons: list[str] = []

    # tier_expected (SCRUM-43): the runtime denominator for a --quick run —
    # the tier's own expected size, computed from the canonical corpus and
    # NEVER stored on the card. Publishing still grades the card against the
    # full corpus (publish_refusals below), so a full-tier quick run passes
    # THIS gate as iteration signal yet remains unpublishable as baseline.
    shortfall = corpus_shortfall(current, available_override=tier_expected)
    if shortfall:
        reasons.append(shortfall)

    unmeasured, total = unmeasured_specs(current)
    if total and unmeasured / total > MAX_UNMEASURED_FRACTION:
        reasons.append(
            f"{unmeasured}/{total} specs went unmeasured "
            f"({unmeasured / total:.0%} > {MAX_UNMEASURED_FRACTION:.0%}) — "
            f"skipped or dead; too little of what this run loaded was "
            f"measured for it to be a verdict on anything")

    if previous is None:
        # A floor on `ran` catches a PROBE (--limit 3). It does NOT catch
        # filtering: 19 surviving specs of 55 clears a 10-spec floor easily,
        # which is why the corpus_shortfall check above exists and this one is
        # not load-bearing for that case. Kept because a first run has no
        # baseline to be narrower than, so nothing else bounds a tiny one.
        # Distinct specs — see publish_refusals: trials multiply scores, and a
        # floor counted on scores is bought by repeating a probe.
        if current.ran_spec_count < MIN_PUBLISHABLE_SPECS:
            reasons.append(
                f"only {current.ran_spec_count} spec(s) ran and there is no baseline "
                f"to compare against (minimum {MIN_PUBLISHABLE_SPECS}) — a "
                f"slice cannot become the reference for every later run")
        if reasons:
            return NorthStarGate(False, reasons)
        return NorthStarGate(True, ["first run — baseline recorded"])

    if not previous.ran:
        # Reachable via --prev <file>, which accepts any results file. Every
        # comparison below would silently pass against an empty baseline.
        #
        # A reviewer suggested extending this to any baseline under
        # MIN_PUBLISHABLE_SPECS, which is defensible — a one-spec baseline is
        # nearly as meaningless. DECLINED here: it blocks four existing tests
        # whose small fixtures are incidental, and adjusting fixtures so a new
        # rule fits is exactly the move that neutered a test earlier in this
        # PR's history. The empty case is the unambiguous one; a wider floor
        # deserves its own change with its own fixtures.
        reasons.append(
            f"the baseline '{previous.label or 'unlabelled'}' measured no "
            f"specs — there is nothing to compare against")
    if current.ran_spec_count < previous.ran_spec_count:
        reasons.append(
            f"this run measured {current.ran_spec_count} spec(s) but the "
            f"baseline '{previous.label or 'unlabelled'}' measured "
            f"{previous.ran_spec_count} — a narrower run cannot establish "
            f"'no regression'")

    # SPEC-MEAN, not the pooled row rate — the SAME estimator `success_headline`
    # publishes, so the gate cannot pass a run whose published number dropped.
    # They differ only when trials are uneven (a resumed run), and there the
    # pooled rate weights whichever specs happened to run most often: 10 solid
    # specs replayed 10× beside 10 broken ones replayed once pools to 91% —
    # above a 90% baseline, gate green — while half the corpus is failing and
    # the number the report prints is 50%. Gating on a figure no surface
    # publishes is how a regression clears a regression gate.
    drop = previous.spec_mean_success_rate - current.spec_mean_success_rate
    if drop > max_success_drop + 1e-9:
        reasons.append(
            f"success rate dropped {previous.spec_mean_success_rate:.0%} → "
            f"{current.spec_mean_success_rate:.0%}")
    for label, prev_r, cur_r in (
        ("token", previous.median_token_ratio, current.median_token_ratio),
        ("cost", previous.median_cost_ratio, current.median_cost_ratio),
    ):
        if prev_r is not None and cur_r is None:
            # Review finding: a run that LOSES its ratio must not silently
            # skip the cost check — fail closed and make a human look.
            reasons.append(
                f"median {label} ratio unavailable on current run "
                f"(previous had {prev_r:.2f}) — cannot verify cost; blocking")
        elif prev_r is not None and cur_r is not None \
                and cur_r - prev_r > max_ratio_regression:
            reasons.append(
                f"median {label} ratio regressed {prev_r:.2f} → {cur_r:.2f} "
                f"(> +{max_ratio_regression})")
    # A basis change (e.g. the part-1 rollout of tier-weighting) makes the
    # cost-ratio comparison above unsound even when neither figure is
    # missing: the two medians were priced against DIFFERENT tables, so a
    # "regressed"/"improved" reading compares numbers that were never on the
    # same basis to begin with. Flagged here rather than left implicit,
    # per the rule that a surface must never let old and new ratios be
    # silently conflated. "n/a" (no priced scores on one side) is not a
    # basis change — there is nothing to compare.
    prev_basis, cur_basis = (previous.median_cost_ratio_basis,
                             current.median_cost_ratio_basis)
    if "n/a" not in (prev_basis, cur_basis) and prev_basis != cur_basis:
        reasons.append(
            f"cost ratio basis changed: '{prev_basis}' -> '{cur_basis}' — "
            "the baseline and this run priced no_human's spend on different "
            "tables, so the cost-ratio comparison above is not like-for-"
            "like. Republish a new baseline on the current basis (basis: "
            f"{cur_basis}) before trusting future comparisons.")
    if reasons:
        return NorthStarGate(False, reasons)
    return NorthStarGate(True, ["no regression vs previous run"])


def render_northstar_md(card: NorthStarCard,
                        history: list[dict[str, Any]] | None = None) -> str:
    """Render docs/NORTH_STAR_BENCH.md content."""
    agg = card.as_dict()["aggregate"]
    # How many ran specs actually count toward the cost median — the same
    # `priced_scores` predicate the median itself is computed over. Using a
    # different predicate here (e.g. counting every spec with a cost_ratio,
    # zero-spend ones included) would publish a denominator larger than the
    # population the median was actually taken over.
    _priced = len(card.priced_scores)
    # Specs with ANY recorded original baseline (cost_ratio is not None),
    # independent of whether no_human spent anything on them. This is a
    # DIFFERENT, larger population than `_priced`: a zero-spend spec still
    # HAS a baseline and still contributes its orig_tokens to
    # `total_orig_tokens`, even though `priced_scores` (and therefore the
    # median) excludes it for having spent nothing. Reusing `_priced` as the
    # denominator for the orig-tokens total would understate how many specs
    # that total is actually summed over.
    _baselined = sum(1 for s in card.ran if s.cost_ratio is not None)
    # Numerator/denominator behind the escalation rate. Computed here
    # rather than added to the aggregate so this does not collide with the
    # same fields arriving on the bench-endpoint branch.
    _gated = [s for s in card.ran if s.expected_escalation]
    _gated_ok = sum(1 for s in _gated if s.goal_satisfied)
    _delivered = agg['satisfied'] - _gated_ok
    ratio = agg["median_token_ratio"]
    # Three states, not two: a card can also record NO trial count (a file
    # written before `--trials` existed). Saying "replayed once" there would be
    # an assertion nothing measured — the same made-up precision this whole
    # mechanism removes.
    if card.trials > 1:
        _trials_note = (
            f"each spec was replayed {card.trials}× — the headline is the mean "
            f"of the per-spec means, and **pass^{card.trials}** is the share of "
            f"specs that passed EVERY trial (capability vs reliability); a spec "
            f"that flips between trials is not one the agent can do")
    elif card.trials == 1:
        _trials_note = (
            "each spec was replayed ONCE: the interval above is the whole of "
            "what a single pass over this corpus supports, and it cannot tell "
            "a spec the agent does reliably from one it got right this time")
    else:
        _trials_note = (
            "**this run does not record how many times each spec was replayed** "
            "— it predates `--trials`; read the headline as unverified")
    lines = [
        "# North-star benchmark — no_human vs the operator's real sessions",
        "",
        f"> Run: {card.created_at or 'n/a'}  ·  label: {card.label or 'n/a'}. "
        "Generated by `nh bench publish` — do not edit by hand.",
        "",
    ]
    if card.override_reasons:
        lines += [
            "> [!WARNING]",
            "> **This run was published with `--force` over the checks below.**",
            "> Read every number here as unverified until they are addressed:",
            *(f"> - {r}" for r in card.override_reasons),
            "",
        ]
    lines += [
        "_Project labels and repo paths in this report are pseudonymised; every "
        "number is exactly as measured. A run may predate the current corpus — "
        "check the run date above before treating it as reproducible._",
        "",
        "> **Corpus resolution is machine-local.** Most tracked specs carry "
        "vendor-neutral repo paths — the specs are committed and real project "
        "names should not be — and a vendor-neutral path resolves nowhere on "
        "its own. `eval/repo_map.yaml` translates those to the checkouts on "
        "the machine that ran the bench, and it is gitignored (it maps the "
        "neutral names to private repos, which is exactly what the scrub "
        "removed). A fresh clone, or a `git worktree`, LOADS THE SAME SPECS "
        "but RESOLVES far fewer of them: the loader globs the spec directory "
        "and only rewrites paths, it never drops a spec. Specs that do not "
        "resolve are SKIPPED, so the figure that moves is "
        "**skipped (not measured)** in the Headline below — resolution "
        "failures cannot move loaded-vs-available, so that pair will tell you "
        "nothing here. Mind the DIRECTION: skipped specs leave the success "
        "denominator, so a run with many unresolved repos reads HIGHER, not "
        "lower. See `eval/repo_map.example.yaml` before treating a large skip "
        "count as a corpus that broke.",
        "",
        "## Headline",
        "",
        # The headline is the CI string, never a bare percentage (V1). The raw
        # fraction stays in front of it — an interval is not a substitute for
        # the counts it was computed from — but it is the MEASURED pair
        # (passes over rows that did priced work), the same population the
        # headline divides over: the dead-specs line below promises deaths
        # "are EXCLUDED from the success figures above", and a pooled
        # satisfied/ran pair here made that sentence false on any
        # dead-carrying card. The ran count (deaths included) stays on the
        # line, labeled as what it is, so nothing is hidden by the move.
        #
        # The UNIT is named above one trial. That pair counts ROWS, so under
        # `--trials 20` it reads "220/221" — 99.5% — beside a headline of
        # 91.7%, and a reader with no unit on either has no way to tell that
        # they are answering different questions (how many replays passed, vs
        # how much of the corpus the agent can do). Naming it costs a word.
        f"- **Success (goal satisfied, unattended): {agg['measured_satisfied']}/"
        f"{agg['total'] - agg['skipped'] - agg['dead_specs']}"
        f"{' trials' if card.trials > 1 else ''} measured — "
        f"{success_headline(card)}**"
        f"  ·  of {agg['total'] - agg['skipped']} ran ({agg['dead_specs']} dead)"
        # "non-runnable" was true when EVERY skip came from spec.runnable=False,
        # decided at generation time. The dominant skip is now "repo missing at
        # run time" — the instrument breaking, not a spec that was never
        # runnable — and a reader of the tracked report would otherwise
        # conclude the corpus contains N unrunnable specs rather than that N
        # checkouts vanished. This label is true of both kinds.
        f"  ·  skipped (not measured): {agg['skipped']}",
        f"  - of which {_delivered} DELIVERED a change and {_gated_ok} correctly "
        f"ESCALATED — 'satisfied' counts an honest refusal as the right outcome, "
        f"which it is, but only the first group shipped anything",
        # DELIVERED IS NOT MERGED, and this corpus structurally cannot tell you
        # whether anything merged: bench specs push to a local bare repo (the
        # `local-pr://` path in vcs/__init__.py), so there is no forge to ask
        # and no merge to observe. Stated here, in the tracked report, because
        # this is the file the headline percentage is read from — and the
        # defect being guarded against is precisely a number that measured what
        # was easy to compute being read as the thing that was asked. The real
        # merge figures come from `nh pr-outcomes show` over actual runs; no
        # number from that population is copied into this file, because this
        # file must stay reproducible from `latest.json` alone.
        "  - ⚠️ **`DELIVERED` means a change was produced, NOT that it merged.** "
        "These specs run against a local bare-repo remote with no forge, so "
        "this corpus can never show a merge rate — do not read the headline as "
        "one. For what actually landed, and what that does and does not "
        "establish, run `nh pr-outcomes show` (real runs only).",
        f"  - {_trials_note}",
        f"- **Median COST ratio (price-weighted, cache-aware; "
        f"basis: {agg['median_cost_ratio_basis']}): "
        f"{agg['median_cost_ratio'] if agg['median_cost_ratio'] is not None else 'n/a'}**"
        f" — over the {_priced} of {agg['total'] - agg['skipped']} ran spec(s) "
        f"with a recorded original cost AND non-zero no_human spend; <1.0 means no_human was cheaper than "
        f"the babysat session. The nh side counts coder+reviewer, NOT yet "
        f"planner/supervisor (B2), so it UNDERSTATES no_human's real burn.",
        f"- Median token ratio (non-cache in/out only): "
        f"{ratio if ratio is not None else 'n/a'} — blind to cache burn; "
        "nh side includes coder+reviewer, NOT yet planner/supervisor (B2)",
        f"- Total non-cache tokens: nh {agg['total_nh_tokens']:,} over all "
        f"{agg['total'] - agg['skipped']} ran spec(s), vs original "
        f"{agg['total_orig_tokens']:,} over the {_baselined} that have a baseline "
        f"at all — NOT a like-for-like pair; use the median cost ratio above",
        # Always rendered, including the 0 case. A published run is under the
        # refusal threshold by construction, so the number a reader needs is
        # confirmation that saturation was checked — not its absence when clean
        # and a silent omission when not.
        f"- Specs that ran but burned zero tokens (backend died before any "
        f"model call): **{agg['dead_specs']}** of {agg['total'] - agg['skipped']}"
        + ("  ⚠ a death is an infrastructure failure, not a quality verdict, "
           "so these rows are EXCLUDED from the success figures above (the "
           "rate with them still in the denominator is recorded as "
           "`success_rate_incl_dead`) — read every figure here with that in "
           "mind. `success_rate_incl_dead` exists precisely so the two rates' "
           "divergence stays visible to a reader; nothing gates on it — a "
           "saturated run is caught by the publish refusals, not by this line."
           if agg['dead_specs'] else ""),
        # Always rendered, including the 0 case, for the same reason as the
        # dead-specs line above: a missing line and a zero line must not look
        # the same. UNLIKE the dead-specs line, these rows are NOT excluded
        # from the success figures — a judge failure is a measured, priced,
        # failed row, and dropping it from the denominator would raise the
        # published number with no agent improving. That is an operator
        # decision, not a code change, and it has not been taken here.
        f"- Specs whose judge produced no parseable verdict after the bounded "
        f"retry (**unscoreable**): **{agg['unscoreable_specs']}** of "
        f"{agg['total'] - agg['skipped']} ran"
        + ("  — these rows STILL COUNT AS NOT SATISFIED in every figure above: "
           "the denominator is unchanged and deliberately conservative. "
           "Removing them would raise the published number with no agent "
           "improving, so it is an operator decision, not a code change. The "
           "line exists so 'the judge broke' is not read as 'the agent "
           "missed the goal'."
           if card.unscoreable_recorded else
           "  ⚠ **not recorded** — this results file predates the field; the "
           "0 is an absence of measurement, not a measured zero"),
        pin_rederivation_note(card),
        f"- **Original-session follow-ups avoided on DELIVERED tasks: "
        f"{agg['corrections_avoided_delivered']}** (proxy for corrections)"
        f"  ·  a further "
        f"{agg['corrections_avoided'] - agg['corrections_avoided_delivered']} "
        f"belong to tasks no_human correctly ESCALATED — the human still has "
        f"to do those, so they are not savings",
        f"- Honest-escalation rate on gated tasks: "
        f"{agg['honest_escalation_rate']:.0%} "
        f"({_gated_ok}/{len(_gated)}) — one flip moves a small "
        f"denominator several points",
        "",
        # Every "satisfied" above is one LLM judge's verdict, and a reader
        # handed the numbers is owed the record of what that judge has been
        # checked against — including that the human calibration it needs has
        # not been done. Emitted by the generator rather than pasted into the
        # tracked report, or the next publish would silently drop it.
        "_Every `satisfied` figure above is a verdict from the goal judge, not "
        "a human label. What that judge has and has not been calibrated "
        "against — and the one open finding against it — is recorded in "
        "`eval/JUDGE_CALIBRATION.md`._",
        "",
        "## Per-task",
        "",
        "| task | outcome | satisfied | nh tokens | orig tokens | cost ratio (basis) | "
        "orig follow-ups (proxy) | attempts | tokens @ escalation | "
        + ("pass^k | " if card.trials > 1 else "")
        + "cache | notes |",
        "|---|---|---|---|---|---|---|---|---|"
        + ("---|" if card.trials > 1 else "")
        + "---|---|",
    ]
    # PRIVACY: only hand-curated (PR-reviewed) core specs get per-task rows
    # in this git-tracked report; generated specs are verbatim operator
    # conversations and appear only in the aggregates.
    core_scores = [s for s in card.scores if s.subset == "core"]
    hidden = len(card.scores) - len(core_scores)
    per_spec = card.per_spec_passes if card.trials > 1 else {}
    # Mean over rows that recorded cache tokens — a run with none excluded,
    # not counted as a 0.0, for the same reason cache_read_share itself
    # returns None rather than 0 for an unmeasured attempt.
    _cache_shares: list[float] = []
    for s in core_scores:
        # The basis rides in the SAME cell as the number — a separate column
        # would let a reader eyeball the ratio column alone and re-introduce
        # the exact conflation this field exists to prevent.
        ratio_s = (f"{s.cost_ratio:.2f} ({s.cost_ratio_basis})"
                   if s.cost_ratio else "—")
        sat = {True: "✅", False: "❌", None: "—"}[s.goal_satisfied]
        if s.unscoreable:
            sat += " (judge unparseable)"
        # One row per TRIAL under --trials, so the trial has to be in the cell
        # or the table reads as N duplicate rows of the same spec disagreeing
        # with themselves.
        task_cell = (f"{s.task_id} #{s.trial + 1}" if card.trials > 1
                     else s.task_id)
        # Escalation-latency cells: escalated rows print the real numbers,
        # non-escalated rows print "—" in both — the visual distinction the
        # report is for. No new arithmetic: same attempts/nh_tokens `_score`
        # already stamped on the row.
        if s.escalated_honestly:
            attempts_cell = f"{s.attempts_before_escalation}"
            tokens_cell = f"{s.tokens_before_escalation:,}"
        else:
            attempts_cell = "—"
            tokens_cell = "—"
        pass_k_cell = ""
        if card.trials > 1:
            passes, ran_n = per_spec.get(s.task_id, (0, 0))
            pass_k_cell = (f"{passes}/{ran_n} | " if ran_n else "— (not measured) | ")
        # The earliest signal an attempt is heading for budget exhaustion —
        # per-task, not the fleet total metrics.py's cache_economics reports.
        sh = cache_read_share(s.nh_cache_tokens, s.nh_cache_creation_tokens)
        cache_cell = f"{sh:.0%}" if sh is not None else "—"
        if sh is not None:
            _cache_shares.append(sh)
        lines.append(
            f"| {task_cell} | {s.outcome_status} | {sat} | {s.nh_tokens:,} | "
            f"{s.orig_tokens:,} | {ratio_s} | {s.orig_corrections} | "
            f"{attempts_cell} | {tokens_cell} | {pass_k_cell}"
            f"{cache_cell} | "
            f"{redact_for_publish(s.notes or '')[:80].replace('|', '/')} |")
    if hidden:
        lines.append(f"\n_{hidden} non-core task(s) included in the aggregates only (privacy: raw corpus rows never enter git)._")
    if _cache_shares:
        _mean_cache = sum(_cache_shares) / len(_cache_shares)
        lines.append(
            f"\n_Mean cache-read share over {len(_cache_shares)} core "
            f"run(s) that recorded cache tokens: {_mean_cache:.0%} — runs "
            f"with no cache tokens are excluded, not counted as 0._")

    # Per-spec pass counts — the reliability view the pass^k headline is a
    # summary OF. Core specs only, same privacy rule as the table above.
    if card.trials > 1:
        per = card.per_spec_passes
        core_ids = sorted({s.task_id for s in core_scores})
        if core_ids:
            lines += ["", f"## Per-spec reliability ({card.trials} trials)", "",
                      "A spec at 1/3 or 2/3 is not a capability, it is a coin "
                      "flip; only n/n is something the agent can be relied on "
                      "to do.", "",
                      "| task | passed | flaky |", "|---|---|---|"]
            for tid in core_ids:
                passes, ran_n = per.get(tid, (0, 0))
                # A spec skipped in every trial has no row in `per_spec_passes`
                # at all, and "0/0" renders it as a spec that ran and failed —
                # a measured zero, which is the opposite of what happened. The
                # dominant skip cause here is a repo missing at run time, so
                # this cell is common, not exotic.
                cell = f"{passes}/{ran_n}" if ran_n else "— (not measured)"
                flaky = "⚠ flips" if 0 < passes < ran_n else ""
                lines.append(f"| {tid} | {cell} | {flaky} |")

    # Per-project view (the operator's suite spans multiple real repos — show
    # cost/quality by project). Aggregate-only (repo name + counts + median
    # cost), so it's privacy-safe over ALL scores, not just core.
    from collections import defaultdict
    proj: dict = defaultdict(
        lambda: {"n": 0, "ok": 0, "esc": 0, "ratios": [], "priced": []})
    for s in card.scores:
        p = s.project or "?"
        proj[p]["n"] += 1
        proj[p]["ok"] += 1 if s.goal_satisfied else 0
        proj[p]["esc"] += 1 if s.escalated_honestly else 0
    # The median MUST be taken over exactly `card.priced_scores` — the same
    # population as the headline `median_cost_ratio`. Re-deriving the predicate
    # here is what let the two drift apart (a zero-spend spec is a non-result,
    # not a cost win, and counting it makes a project look 8x cheaper than the
    # headline). Do not reintroduce a local `if s.cost_ratio ...` test.
    for s in card.priced_scores:
        proj[s.project or "?"]["ratios"].append(s.cost_ratio)
        proj[s.project or "?"]["priced"].append(s)
    if len(proj) > 1:
        lines += ["", "## Per-project", "",
                  "| project | tasks | satisfied | honest-escalations | "
                  "median cost (priced n, basis) |",
                  "|---|---|---|---|---|"]
        for p in sorted(proj):
            a = proj[p]
            med = (f"{median(a['ratios']):.3f} ({len(a['ratios'])}, "
                   f"{cost_ratio_basis(a['priced'])})"
                   if a["ratios"] else "—")
            # Redact the DISPLAYED label; the grouping key `p` stays the real
            # name so counts and medians are unchanged. (v13's project labels
            # are the real repo names, not the neutral spec basenames.)
            lines.append(f"| {redact_for_publish(p)} | {a['n']} | "
                         f"{a['ok']}/{a['n']} | {a['esc']} | {med} |")

    if history:
        lines += ["", "## History (key changes)", "",
                  "| date | label | success | median ratio |", "|---|---|---|---|"]
        for h in history:
            lines.append(f"| {h.get('created_at','')[:10]} | {h.get('label','')} "
                         f"| {h.get('success_rate','')} | "
                         f"| {h.get('median_token_ratio','')} |".replace("| |", "|"))
    return "\n".join(lines) + "\n"
