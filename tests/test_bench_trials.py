"""bench v2 / V1 — trials, intervals, and the refusal that makes them stick.

WHY THIS EXISTS. The published north-star figure was 49 of 54 specs, run ONCE:
"90.7%". Its Wilson 95% interval is [80%, 96%], which is a different claim
entirely — and nothing in the card, the report or the console said so. Three
things had to be true at once for that to stop happening:

  1. the runner can replay each spec N times and RECORD each trial separately
     (a mean over three runs of the same spec is not three specs),
  2. every surface that prints the headline prints its interval with it, and
  3. a card that cannot support an interval cannot be published as the baseline
     without a human overriding it on the record.

Each is tested here against the shape that would break it, not against a happy
path: a resumed multi-trial run (double-counting is silent and inflates the
denominator it divides by), a 4-spec probe repeated 3× (which would clear a
10-spec floor counted on rows), and a results file written before `--trials`
existed (the only card that can actually reach `publish` without a CI).

SECOND ROUND (review of the above). The first version shipped an interval that
was wrong in two ways at once, and both were found by running it rather than
reading it:

  4. it POOLED specs×trials rows into a binomial interval as if the trials of
     one spec were independent observations of different specs. Measured
     coverage of the nominal 95% interval under clustering: **49.6%** — a coin
     flip. The fix is a design-effect discount (`intracluster_correlation`,
     `effective_n`), and the property that makes it safe to adopt is that it
     reduces EXACTLY to the old arithmetic at one trial per spec;
  5. the point estimate and the interval came from DIFFERENT estimators — a
     spec-mean percentage in front of a pooled-row interval. On a resumed
     12-spec run that printed `91.7% (95% CI 97.5-99.9, n=12x20=221)`: an
     interval that excludes its own point estimate, and an `n=` whose
     multiplication is false. Both are pinned below against that exact run.

And the three floors that count SPECS rather than rows — `corpus_shortfall`
plus both sites in `northstar_gate` — had no test of their own; the reviewer's
three scenarios are here as tests, each one a case where the row count would
have waved the run through.
"""

from __future__ import annotations

import json

import pytest
import yaml
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.eval.northstar import BenchScore
from no_human.eval.northstar_card import (
    MIN_CORPUS_LOADED_FRACTION,
    MIN_PUBLISHABLE_SPECS,
    NorthStarCard,
    corpus_shortfall,
    intracluster_correlation,
    northstar_gate,
    publish_refusals,
    render_northstar_md,
    success_headline,
    wilson_interval,
)


# --------------------------------------------------------------------------- #
# 1. The interval itself
# --------------------------------------------------------------------------- #

def test_wilson_matches_the_published_49_of_54():
    """THE number this mechanism exists for. 49/54 = 90.7% single-run, whose
    95% Wilson interval is [0.80, 0.96] — the spread that makes a bare "90.7%"
    unsupportable. Asserted to 2dp against the published value, not against a
    re-derivation of the same formula, which would only prove the code equals
    itself."""
    lo, hi = wilson_interval(49, 54)
    assert round(lo, 2) == 0.80, lo
    assert round(hi, 2) == 0.96, hi


@pytest.mark.parametrize("k,n,lo,hi", [
    # Textbook Wilson values, each covering a case the normal approximation
    # gets wrong: mid-range (where Wald is closest and still differs), a zero
    # numerator (Wald gives the absurd [0, 0]) and a full one (Wald: [1, 1]).
    (5, 10, 0.24, 0.76),
    (0, 20, 0.00, 0.16),
    (20, 20, 0.84, 1.00),
])
def test_wilson_known_values(k, n, lo, hi):
    got = wilson_interval(k, n)
    assert (round(got[0], 2), round(got[1], 2)) == (lo, hi), got


def test_wilson_never_leaves_the_unit_interval():
    """The property that makes Wilson the right choice here: a benchmark that
    scores near 0 or near 1 must not be handed a bound below 0% or above 100%,
    which is exactly what the normal interval produces at these edges."""
    for n in (1, 3, 7, 54, 162):
        for k in (0, n):
            lo, hi = wilson_interval(k, n)
            assert 0.0 <= lo <= hi <= 1.0, (k, n, lo, hi)


def test_wilson_has_no_interval_for_no_observations():
    """None, not (0.0, 0.0). A zero-width interval at zero would render as a
    measured 0% with perfect precision — the most confident possible statement
    made from nothing."""
    assert wilson_interval(0, 0) is None


def test_wilson_refuses_impossible_counts():
    with pytest.raises(ValueError):
        wilson_interval(5, 4)
    # n == 0 is the ONLY absence. successes > n at n == 0 is still a broken
    # caller, and the `return None` must not be reachable ahead of it.
    with pytest.raises(ValueError):
        wilson_interval(1, 0)
    with pytest.raises(ValueError):
        wilson_interval(-1, 10)


def test_wilson_refuses_a_negative_n_instead_of_calling_it_nothing(
):
    """A NEGATIVE n used to return the same ``None`` as an empty run, so an n
    computed wrong — a subtraction the wrong way round, a count read from a
    field that was not there — rendered as "n/a" three surfaces downstream
    instead of raising where it was made. "Nothing ran" and "this count is
    impossible" are different facts and only one of them is a measurement."""
    with pytest.raises(ValueError):
        wilson_interval(0, -1)


# --------------------------------------------------------------------------- #
# 2. Scoring over trials
# --------------------------------------------------------------------------- #

def _sc(task_id: str, trial: int, satisfied: bool, *, nh: int = 5_000,
        status: str = "done") -> BenchScore:
    return BenchScore(
        task_id=task_id, title=task_id, outcome_status=status,
        goal_satisfied=satisfied, escalated_honestly=False, mergeable=None,
        nh_tokens=nh, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=10_000,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=1.0, orig_corrections=0, subset="core", trial=trial)


def _trial_card(pattern: dict[str, list[bool]], **kw) -> NorthStarCard:
    """A card from ``{spec: [pass, pass, fail]}`` — one score per trial."""
    scores = [_sc(tid, t, ok)
              for tid, outcomes in pattern.items()
              for t, ok in enumerate(outcomes)]
    trials = max(len(v) for v in pattern.values())
    return NorthStarCard(scores=scores, created_at="2026-08-03T00:00:00+00:00",
                         label="trials", trials=trials, **kw)


def test_per_spec_pass_counts_and_the_two_headline_numbers():
    """Capability and reliability are different questions and this fixture
    separates them: one spec always passes, one always fails, one flips. Mean
    success is 50% (3 of 6 trials); pass^2 is 33% (1 spec of 3). A card that
    reported only the first would call a coin-flip spec half-solved."""
    card = _trial_card({
        "ns-solid": [True, True],
        "ns-flaky": [True, False],
        "ns-broken": [False, False],
    })
    assert card.per_spec_passes == {
        "ns-solid": (2, 2), "ns-flaky": (1, 2), "ns-broken": (0, 2)}
    assert card.spec_mean_success_rate == pytest.approx(0.5)
    assert card.pass_k_rate == pytest.approx(1 / 3)
    assert card.spec_count == 3 and len(card.ran) == 6


def test_the_spec_mean_is_not_the_pooled_rate_when_trials_are_uneven():
    """The reason the headline is a mean OF MEANS. A resumed run that died
    partway leaves specs with unequal trial counts, and pooling then weights
    whichever spec happened to run most — here the failing spec ran 3× and the
    passing spec once, so pooling says 25% while the corpus is really half
    solved. With balanced trials the two agree, which is why the historical
    single-run numbers do not move."""
    card = _trial_card({"ns-a": [True], "ns-b": [False, False, False]})
    assert card.success_rate == pytest.approx(0.25)        # pooled
    assert card.spec_mean_success_rate == pytest.approx(0.5)
    balanced = _trial_card({"ns-a": [True, True], "ns-b": [False, False]})
    assert balanced.success_rate == balanced.spec_mean_success_rate


def test_trials_buy_precision_only_to_the_extent_the_trials_DISAGREE():
    """n is the number of INDEPENDENT observations, which is not the row count.

    Trials of one spec are clustered: a spec the agent can do passes every
    time, and that is one fact repeated N times. Handing specs×trials rows to a
    binomial interval as if they were independent is the defect a reviewer
    measured at **49.6% real coverage** for a nominal 95% interval. The
    effective n is rows discounted by Kish's design effect, so this test pins
    the two ends of that discount rather than "more rows is tighter":

    - 54 specs replayed 3× where every spec agrees with itself (ρ̂ = 1) is
      worth exactly the 54 specs it started from. Paying 3× the tokens bought
      pass^3, not precision, and the interval must say so;
    - 54 specs replayed 3× where every spec flips the SAME way (all the
      variance is within specs, ρ̂ = 0) is worth all 162 rows, and its
      interval is strictly tighter than the same rate measured once.

    The failure this replaces is the first bullet: the pooled interval
    reported n=162 on the unanimous card and produced the tightest interval in
    the file on its least informative data."""
    single = _trial_card({f"ns-{i}": [True] for i in range(54)})
    unanimous = _trial_card({f"ns-{i}": [True, True, True] for i in range(54)})
    assert unanimous.intracluster_correlation == 1.0
    assert unanimous.effective_n == pytest.approx(54.0)
    assert unanimous.success_ci == single.success_ci, (
        "three identical replays of a spec are one observation of it")
    # ... and the printed n still tells the reader what was actually run.
    assert "n=54×3=162" in success_headline(unanimous), \
        success_headline(unanimous)

    # Same rate, measured once vs measured three times with the disagreement
    # INSIDE the specs. Now the repeats are real information.
    flaky = _trial_card({f"ns-{i}": [True, True, False] for i in range(54)})
    once = _trial_card({f"ns-{i}": [i < 36] for i in range(54)})
    assert flaky.spec_mean_success_rate == pytest.approx(
        once.spec_mean_success_rate, abs=0.01)
    assert flaky.intracluster_correlation == 0.0
    assert flaky.effective_n == pytest.approx(162.0)
    lo_f, hi_f = flaky.success_ci
    lo_1, hi_1 = once.success_ci
    assert hi_f - lo_f < hi_1 - lo_1, ((lo_f, hi_f), (lo_1, hi_1))


def test_a_single_trial_card_is_exactly_todays_card_plus_an_interval():
    """Backward compatibility, asserted rather than assumed: with one trial per
    spec every existing aggregate keeps its value, and the only difference is
    that the file now also states its trial count and its interval."""
    card = _trial_card({f"ns-{i}": [i < 9] for i in range(10)})
    agg = card.as_dict()["aggregate"]
    assert card.trials == 1
    assert agg["success_rate"] == agg["spec_mean_success_rate"] == 0.9
    assert agg["total"] == agg["specs"] == 10
    assert agg["success_ci_low"] is not None
    assert agg["pass_k_rate"] == 0.9


# --------------------------------------------------------------------------- #
# 3. The card never prints a bare percentage
# --------------------------------------------------------------------------- #

def test_the_report_headline_carries_the_interval_and_the_n():
    card = _trial_card({f"ns-{i}": [True, True, False] for i in range(12)})
    md = render_northstar_md(card)
    line = next(ln for ln in md.splitlines() if "Success (goal satisfied" in ln)
    assert "95% CI" in line, line
    assert "n=12×3=36" in line, line
    # pass^k rides with it: 0 of 12 specs passed all three trials.
    assert "pass^3 0.0%" in line, line
    # And the reliability table the pass^k number summarises.
    assert "Per-spec reliability (3 trials)" in md
    assert "| ns-0 | 2/3 | ⚠ flips |" in md, md


def test_a_single_trial_report_still_refuses_to_print_a_bare_percentage():
    """The single-run card is the one that was actually published as "90.7%",
    so it is the one that most needs its interval — trials>1 must not be the
    condition for honesty."""
    card = _trial_card({f"ns-{i}": [i < 9] for i in range(10)})
    line = next(ln for ln in render_northstar_md(card).splitlines()
                if "Success (goal satisfied" in ln)
    assert "95% CI" in line, line
    # pass^1 is arithmetically the mean; printing it would read as a second,
    # corroborating measurement that does not exist.
    assert "pass^1" not in line, line


def test_the_headline_says_so_when_the_trial_count_is_unknown():
    """A card loaded from a pre-trials file must not be described as "replayed
    once" — nothing measured that."""
    card = _trial_card({f"ns-{i}": [True] for i in range(10)})
    card.trials = 0
    md = render_northstar_md(card)
    assert "does not record how many times each spec was replayed" in md
    assert "replayed ONCE" not in md


# --------------------------------------------------------------------------- #
# 4. Publishing refuses a card with no interval / no trial count
# --------------------------------------------------------------------------- #

def _legacy_json(card: NorthStarCard) -> dict:
    """The card as a build that predates `--trials` would have written it."""
    data = card.as_dict()
    for key in ("trials", "success_ci_low", "success_ci_high",
                "pass_k_rate", "spec_mean_success_rate", "specs", "ran_specs"):
        data["aggregate"].pop(key, None)
    for s in data["scores"]:
        s.pop("trial", None)
    return data


def _healthy(n=30) -> NorthStarCard:
    return _trial_card({f"ns-{i}": [True] for i in range(n)})


def test_a_card_with_no_trials_metadata_is_refused():
    reasons = publish_refusals(_pre_trials_card(_healthy()))
    assert any("no trial count" in r for r in reasons), reasons


def test_a_card_with_no_interval_is_refused():
    reasons = publish_refusals(_pre_trials_card(_healthy()))
    assert any("no confidence interval" in r for r in reasons), reasons


def test_a_current_card_publishes_clean():
    """The positive control. Without it, a refusal that fires on EVERYTHING
    passes both tests above while making the command useless."""
    assert publish_refusals(_healthy()) == []
    assert publish_refusals(
        _trial_card({f"ns-{i}": [True, False, True] for i in range(30)})) == []


def _pre_trials_card(card: NorthStarCard) -> NorthStarCard:
    """*card* round-tripped through the pre-trials FILE shape.

    Through the file on purpose: "this card has no interval" is a property of
    what was written down, and the only way a card reaches `publish` without
    one is by being loaded from a file that predates it. Constructing the
    object directly would test a state the loader can never produce."""
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp(prefix="nh-trials-")) / "legacy.json"
    p.write_text(json.dumps(_legacy_json(card)))
    return NorthStarCard.load(p)


def test_trials_cannot_buy_a_probe_past_the_minimum_spec_floor():
    """The floor counts SPECS, not recorded rows. Four specs replayed three
    times is 12 scores — over a 10-row floor — and still a four-spec probe. A
    floor counted on rows would be defeated by the very flag that is supposed
    to make a run more trustworthy."""
    probe = _trial_card({f"ns-{i}": [True, True, True] for i in range(4)})
    assert len(probe.ran) > MIN_PUBLISHABLE_SPECS, "precondition: rows clear it"
    reasons = publish_refusals(probe)
    assert any("minimum" in r for r in reasons), reasons


def test_trials_cannot_make_a_narrower_run_look_broader():
    """20 specs × 3 trials = 60 rows against a 55-spec baseline's 55. Compared
    on rows this publishes, and silently narrows what every later regression
    gate checks — the incident the narrowing rule exists for, re-entered
    through the new flag."""
    baseline = _trial_card({f"ns-{i}": [True] for i in range(55)})
    narrower = _trial_card({f"ns-{i}": [True, True, True] for i in range(20)})
    assert len(narrower.ran) > len(baseline.ran), "precondition: more rows"
    reasons = publish_refusals(narrower, previous=baseline)
    assert any("narrow" in r for r in reasons), reasons


# ------------------------------- the wiring -------------------------------- #
# The predicate being right is worthless if `bench publish` ignores it.

@pytest.fixture()
def bench_env(tmp_path, monkeypatch):
    import no_human.eval.northstar_card as nc
    results = tmp_path / "results"
    results.mkdir()
    report = tmp_path / "docs" / "NORTH_STAR_BENCH.md"
    report.parent.mkdir()
    report.write_text("ORIGINAL REPORT\n")
    monkeypatch.setattr(nc, "RESULTS_DIR", results)
    monkeypatch.setattr(nc, "REPORT_MD", report)
    return results, report


def test_publishing_a_pre_trials_file_is_refused_and_changes_nothing(bench_env):
    results, report = bench_env
    old = results / "v13.json"
    old.write_text(json.dumps(_legacy_json(_healthy())))

    res = CliRunner().invoke(cli, ["bench", "publish", str(old)])

    assert res.exit_code == 1, res.output
    assert "refusing to publish" in res.output
    assert "no confidence interval" in res.output, res.output
    assert report.read_text(encoding="utf-8") == "ORIGINAL REPORT\n", "report was overwritten"
    assert not (results / "latest.json").exists(), "baseline was overwritten"


def test_forcing_a_pre_trials_file_records_the_override_on_the_record(bench_env):
    """Consistent with every other refusal here: a human may overrule it, and
    the report then carries the banner saying what was overruled. A published
    number with no interval is allowed to exist; it is not allowed to look like
    a measured one."""
    results, report = bench_env
    old = results / "v13.json"
    old.write_text(json.dumps(_legacy_json(_healthy())))

    res = CliRunner().invoke(cli, ["bench", "publish", str(old), "--force"])

    assert res.exit_code == 0, res.output
    saved = json.loads((results / "latest.json").read_text(encoding="utf-8"))
    assert any("no confidence interval" in r
               for r in saved["override_reasons"]), saved["override_reasons"]
    text = report.read_text(encoding="utf-8")
    assert "WARNING" in text and "no confidence interval" in text
    # A forced publish must not become the clean baseline.
    assert not (results / "published_baseline.json").exists()


def test_a_current_results_file_still_publishes_through_the_cli(bench_env):
    """Positive control for the wiring: the new refusals must not have made
    every publish impossible."""
    results, report = bench_env
    good = results / "v16.json"
    _healthy(30).save(good)

    res = CliRunner().invoke(cli, ["bench", "publish", str(good)])

    assert res.exit_code == 0, res.output
    assert "95% CI" in res.output, res.output      # the console never bare
    assert (results / "latest.json").exists()
    assert "95% CI" in report.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 5. The runner: N trials recorded, and resume-safe
# --------------------------------------------------------------------------- #

def _write_specs(d, n: int) -> None:
    from no_human.eval.bench_task import BenchTask
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        spec = BenchTask(
            id=f"ns-t{i}", title=f"spec {i}", request="r", subset="core",
            runnable=True,
            repo={"path": "/definitely/not/here", "pin": "", "branch": ""})
        (d / f"ns-t{i}.yaml").write_text(yaml.safe_dump(spec.to_dict()))


def _patch_env(monkeypatch, tmp_path):
    from no_human.cli import commands as cmds

    class _Cfg:
        data = {"llm": {}}
        primary_model = "m"
        review_model = "m"

        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]

    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR",
                        tmp_path / "results")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NORTH_STAR_BENCH.md")
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH",
                        tmp_path / "absent_map.yaml")


class _CountingRunner:
    """Stub runner: the Nth call for a spec returns the Nth outcome of that
    spec's script, so a trial's recorded result is traceable to the call that
    produced it."""
    script: dict[str, list[bool]] = {}
    calls: dict[str, int] = {}
    workdirs: list[str] = []

    def __init__(self, *a, **kw): ...

    async def run_one(self, spec, *, workdir):
        cls = _CountingRunner
        n = cls.calls.get(spec.id, 0)
        cls.calls[spec.id] = n + 1
        cls.workdirs.append(str(workdir))
        outcomes = cls.script.get(spec.id) or [True]
        return _sc(spec.id, 0, outcomes[n % len(outcomes)])

    @classmethod
    def reset(cls, script=None):
        cls.script = script or {}
        cls.calls = {}
        cls.workdirs = []


def _results_card(results_dir):
    files = [p for p in results_dir.glob("*.json")
             if not p.name.startswith("progress")]
    assert len(files) == 1, [p.name for p in results_dir.glob("*.json")]
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_trials_records_every_trial_separately(tmp_path, monkeypatch):
    d = tmp_path / "specs"
    _write_specs(d, 3)
    _patch_env(monkeypatch, tmp_path)
    _CountingRunner.reset({"ns-t0": [True, False, True]})
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner",
                        _CountingRunner)

    res = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--trials", "3"])

    assert res.exit_code == 0, res.output
    card = _results_card(tmp_path / "results")
    assert card["aggregate"]["trials"] == 3
    assert len(card["scores"]) == 9, card["scores"]
    keys = sorted((s["task_id"], s["trial"]) for s in card["scores"])
    assert keys == sorted((f"ns-t{i}", t) for i in range(3) for t in range(3))
    # The flipping spec is recorded as flipping, not averaged away at write time.
    flips = sorted((s["trial"], s["goal_satisfied"]) for s in card["scores"]
                   if s["task_id"] == "ns-t0")
    assert flips == [(0, True), (1, False), (2, True)], flips
    # Every trial got its own sandbox workdir — two trials of one spec sharing
    # a directory would share a clone and a bench.db.
    assert len(set(_CountingRunner.workdirs)) == 9


def test_default_is_one_trial_and_the_card_says_so(tmp_path, monkeypatch):
    """Backward compatibility at the CLI: no flag = today's behaviour, one call
    per spec, and a card that states it ran each spec once."""
    d = tmp_path / "specs"
    _write_specs(d, 3)
    _patch_env(monkeypatch, tmp_path)
    _CountingRunner.reset()
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner",
                        _CountingRunner)

    res = CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])

    assert res.exit_code == 0, res.output
    assert _CountingRunner.calls == {"ns-t0": 1, "ns-t1": 1, "ns-t2": 1}
    card = _results_card(tmp_path / "results")
    assert card["aggregate"]["trials"] == 1
    assert {s["trial"] for s in card["scores"]} == {0}


class _DiesPartway(_CountingRunner):
    """Killed by quota saturation after `stop_after` completions.

    KeyboardInterrupt, not Exception: the run loop books an Exception as a
    crashed spec (by design — one spec's death must not lose the run), so only
    a BaseException reproduces the process-death shape that leaves a checkpoint
    behind with work still to do."""
    stop_after = 4

    async def run_one(self, spec, *, workdir):
        if sum(_CountingRunner.calls.values()) >= type(self).stop_after:
            raise KeyboardInterrupt("quota saturated")
        return await super().run_one(spec, workdir=workdir)


def test_resume_completes_the_missing_trials_without_double_counting(
        tmp_path, monkeypatch):
    """THE resume hazard under trials. The checkpoint is keyed per completed
    unit of work, and a spec is now several units. Resuming on the spec id
    alone would drop the outstanding trials of any spec that got one in — a
    silent under-count — while re-running the finished ones would give a spec
    more passes than it has trials, so `pass^k` could exceed 1."""
    d = tmp_path / "specs"
    _write_specs(d, 3)
    _patch_env(monkeypatch, tmp_path)
    results = tmp_path / "results"
    script = {"ns-t0": [True, False, True], "ns-t1": [True, True, False]}

    _CountingRunner.reset(script)
    _DiesPartway.stop_after = 4
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _DiesPartway)
    first = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--label", "t",
              "--trials", "3"])
    # Click turns the KeyboardInterrupt into "Aborted!" / exit 1 — the shape a
    # quota death leaves behind: a partial run and a checkpoint with work in it.
    assert first.exit_code == 1 and "Aborted" in first.output, first.output
    ckpts = list(results.glob("progress-*.json"))
    assert len(ckpts) == 1, [p.name for p in results.glob("*.json")]
    banked = json.loads(ckpts[0].read_text(encoding="utf-8"))
    assert len(banked["scores"]) == 4, banked["scores"]

    # Resume with a runner that completes. Its per-spec call counter restarts,
    # so a trial that is re-run would take the FIRST outcome of the script
    # again — which is what makes the outcome assertion below able to see a
    # re-run rather than merely counting rows.
    _CountingRunner.reset(script)
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner",
                        _CountingRunner)
    res = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--label", "t",
              "--trials", "3", "--resume"])
    assert res.exit_code == 0, res.output

    card = _results_card(results)
    keys = [(s["task_id"], s["trial"]) for s in card["scores"]]
    assert len(keys) == 9, keys
    assert len(set(keys)) == 9, f"a trial was recorded twice: {keys}"
    assert sum(_CountingRunner.calls.values()) == 5, (
        f"resume re-ran completed trials: {_CountingRunner.calls}")
    # No spec can have more passes than trials — the arithmetic that breaks
    # first when a resumed trial is double-counted.
    per: dict[str, list[int]] = {}
    for s in card["scores"]:
        row = per.setdefault(s["task_id"], [0, 0])
        row[0] += 1 if s["goal_satisfied"] else 0
        row[1] += 1
    assert all(p <= n == 3 for p, n in per.values()), per
    assert not list(results.glob("progress-*.json")), \
        "a cleanly completed resume left its checkpoint behind"


def test_a_three_trial_checkpoint_is_not_imported_into_a_one_trial_run(
        tmp_path, monkeypatch):
    """A 3-trial checkpoint holds trials 1 and 2 that a `--trials 1` card has
    no denominator for. They must be dropped, not folded in — otherwise the
    card claims one trial per spec while carrying three, and every per-spec
    rate is computed against a denominator that does not exist.

    The checkpoint is keyed on the trial count too, so in practice the two runs
    do not even share a file; this asserts the filter behind that, which is the
    thing that would matter if the key ever collided."""
    d = tmp_path / "specs"
    _write_specs(d, 3)
    _patch_env(monkeypatch, tmp_path)
    results = tmp_path / "results"
    results.mkdir(parents=True, exist_ok=True)
    from no_human.cli.commands import _slug, _spec_set_key
    from no_human.eval.bench_task import load_bench_tasks

    specs = load_bench_tasks(d, subset="core")
    # A checkpoint written by a 3-trial run, planted at the path a 1-trial run
    # would look for.
    NorthStarCard(
        scores=[_sc("ns-t0", t, True) for t in range(3)],
        created_at="x", label="t", trials=3,
    ).save(results / f"progress-{_slug('t')}-{_spec_set_key(specs, 1)}.json")

    _CountingRunner.reset()
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner",
                        _CountingRunner)
    res = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--label", "t",
              "--resume"])

    assert res.exit_code == 0, res.output
    card = _results_card(results)
    assert card["aggregate"]["trials"] == 1
    assert {s["trial"] for s in card["scores"]} == {0}, card["scores"]
    assert len(card["scores"]) == 3, card["scores"]


def test_the_run_console_line_carries_the_interval(tmp_path, monkeypatch):
    """The console is where a number gets copied out of. It was the last
    surface printing a bare percentage."""
    d = tmp_path / "specs"
    _write_specs(d, 3)
    _patch_env(monkeypatch, tmp_path)
    _CountingRunner.reset()
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner",
                        _CountingRunner)

    res = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--trials", "2"])

    assert res.exit_code == 0, res.output
    assert "95% CI" in res.output, res.output
    assert "pass^2" in res.output, res.output


# --------------------------------------------------------------------------- #
# 6. One estimator, and a design effect that reduces to it (review REQUIRED 1)
# --------------------------------------------------------------------------- #

def test_the_deff_adjustment_reduces_EXACTLY_to_the_old_pooled_pair_at_one_trial():
    """THE precondition for adopting a new estimator on a live baseline.

    Every card published so far ran each spec once. If the clustering
    adjustment moved those numbers at all, republishing would silently restate
    history and nobody could tell a corrected figure from a drifted one. It
    does not move them, and the reason is algebraic rather than empirical: at
    one trial per spec, rows == S, so the mean trials-per-spec m̄ is 1 and the
    design effect is ``1 + (1-1)·ρ̂`` — identically 1, with no path through ρ̂
    at all. n_eff is therefore S, and ``spec_mean × S`` is ``satisfied``
    because each per-spec mean is 0 or 1.

    Asserted against the OLD expression (`wilson_interval(satisfied, len(ran))`)
    written out here, not against the new code re-derived — a re-derivation
    would only prove the implementation equals itself.
    """
    for satisfied, n in ((49, 54), (0, 11), (30, 30), (1, 17), (26, 57)):
        card = _trial_card({f"ns-{i}": [i < satisfied] for i in range(n)})
        assert card.trials == 1
        assert card.effective_n == float(n), card.effective_n
        assert card.success_ci == wilson_interval(satisfied, n), (
            satisfied, n, card.success_ci)
    # The published 49-of-54 card specifically: the number this whole mechanism
    # was built around must survive the fix to the mechanism.
    published = _trial_card({f"ns-{i}": [i < 49] for i in range(54)})
    lo, hi = published.success_ci
    assert (round(lo, 2), round(hi, 2)) == (0.80, 0.96), (lo, hi)


def test_the_point_estimate_is_INSIDE_its_own_interval_however_uneven_the_run():
    """The property a point/interval pair either has or is not a measurement.

    A spec-mean point beside a pooled-row interval does not have it, and the
    counter-example is not exotic — any resumed run produces one. Swept over
    ragged trial counts rather than asserted on one fixture, because the
    defect's magnitude depends on HOW uneven the run is and a single fixture
    cannot show that it is closed everywhere."""
    import random
    rng = random.Random(11)
    for _ in range(200):
        pattern = {}
        for i in range(rng.randint(2, 14)):
            n_trials = rng.randint(1, 8)
            pattern[f"ns-{i}"] = [rng.random() < 0.75 for _ in range(n_trials)]
        card = _trial_card(pattern)
        lo, hi = card.success_ci
        p = card.spec_mean_success_rate
        assert lo <= p <= hi, (p, lo, hi, card.per_spec_passes)


def test_the_resumed_12_spec_run_the_reviewer_drove_through_the_real_publish(
        bench_env):
    """THE regression, end to end through `nh bench publish`.

    Eleven specs replayed 20× and passing every time, plus one spec that got a
    single trial and failed — the shape a run that died and resumed leaves. The
    published console line was:

        success 91.7% (95% CI 97.5-99.9, n=12x20=221)

    Three separate false statements in one string: the interval EXCLUDES the
    91.7% it sits behind (it is the pooled 220/221 rate's interval, a different
    estimator); 12×20 is 240, not 221; and no spec ran twenty times except the
    eleven that did, while the twelfth ran once.

    Pinned through the CLI, not through `success_headline` alone, because the
    reviewer reached it through publish and every surface has to agree."""
    results, report = bench_env
    pattern = {f"ns-{i}": [True] * 20 for i in range(11)}
    pattern["ns-died-on-resume"] = [False]
    card = _trial_card(pattern)
    # Preconditions: the exact run shape, and it is publishable (so this test
    # fails on the STRING, never on a refusal that would mask it).
    assert len(card.ran) == 221 and card.ran_spec_count == 12
    assert card.trials == 20
    assert publish_refusals(card) == []
    # The old estimator, written out, so the assertions below are against a
    # named alternative rather than against nothing.
    old_lo, old_hi = wilson_interval(card.satisfied, len(card.ran))
    assert (round(old_lo * 100, 1), round(old_hi * 100, 1)) == (97.5, 99.9)
    assert not (old_lo <= card.spec_mean_success_rate <= old_hi), (
        "precondition: the pooled interval excluded the printed point estimate")

    out = results / "resumed.json"
    card.save(out)
    res = CliRunner().invoke(cli, ["bench", "publish", str(out)])
    assert res.exit_code == 0, res.output

    line = next(ln for ln in res.output.splitlines() if "success 91.7%" in ln)
    # 1. the arithmetic in `n=` is true: 221 rows over 12 specs, no false product
    assert "n=221 trials over 12 specs" in line, line
    assert "n=12×20=221" not in line, line
    # 2. the interval is the one the printed percentage came from
    assert "97.5–99.9" not in line, line
    lo, hi = card.success_ci
    assert f"95% CI {lo * 100:.1f}–{hi * 100:.1f}" in line, line
    # 3. and it contains its own point estimate
    assert lo <= card.spec_mean_success_rate <= hi, (lo, hi)
    # The report is the other surface that must agree with the console.
    text = report.read_text(encoding="utf-8")
    assert "n=221 trials over 12 specs" in text
    # ...and the raw pair in front of the headline names its unit, so "220/221"
    # (99.5% of TRIALS) cannot be read as the corpus figure beside it (91.7%).
    # "measured", not "ran", since P0.1: the pair divides over the rows that
    # did priced work (identical here — nothing died), and the unit is still
    # named, which is what this assertion exists to hold.
    assert "220/221 trials measured" in text, \
        next(ln for ln in text.splitlines() if "Success (goal" in ln)


def test_a_balanced_run_still_prints_the_product_it_can_actually_check():
    """The uneven phrasing must not become the phrasing for everything: when
    every spec really did run N times, `n=S×N=rows` is both true and the more
    readable statement, and it is what the existing tests grep for."""
    balanced = _trial_card({f"ns-{i}": [True, True, False] for i in range(12)})
    assert "n=12×3=36" in success_headline(balanced), success_headline(balanced)
    # One spec short of balanced is enough to lose the product.
    ragged = _trial_card({**{f"ns-{i}": [True, True, False] for i in range(11)},
                          "ns-short": [True]})
    assert "n=34 trials over 12 specs" in success_headline(ragged), \
        success_headline(ragged)
    assert "×" not in success_headline(ragged), success_headline(ragged)


def test_a_legacy_card_does_not_assert_a_trial_count_nothing_measured():
    """`n=3×1=3` on a file that predates `--trials` states a measurement that
    was never made — the same invented precision the interval exists to remove,
    reintroduced by the string that reports it. The one thing such a file DOES
    know is how many specs it covered."""
    legacy = _pre_trials_card(_trial_card({f"ns-{i}": [True] for i in range(3)}))
    assert legacy.trials == 0
    head = success_headline(legacy)
    assert "n=3 specs (trials unrecorded)" in head, head
    assert "×1" not in head and "n=3×1=3" not in head, head


@pytest.mark.parametrize("pattern,expected,why", [
    ({"a": [True, True], "b": [False, False]}, 1.0,
     "every spec unanimous: the repeats are one fact restated"),
    ({"a": [True, False], "b": [True, False]}, 0.0,
     "all the variance is INSIDE the specs: the repeats are real information"),
    ({"a": [True], "b": [False]}, 0.0,
     "one trial each — nothing within-spec to measure, and the design effect "
     "is 1 regardless"),
    ({"a": [True, False, True]}, 0.0, "a single spec is not a cluster sample"),
    ({}, 0.0, "nothing ran"),
])
def test_the_icc_estimator_on_the_cases_that_decide_the_discount(
        pattern, expected, why):
    per = {k: (sum(v), len(v)) for k, v in pattern.items()}
    assert intracluster_correlation(per) == pytest.approx(expected), why


def test_the_icc_is_clamped_into_the_unit_interval():
    """The ANOVA estimator is unbiased and therefore lands NEGATIVE when specs
    happen to vary less than chance. A negative ρ̂ would make the design effect
    < 1 and hand the interval MORE observations than there are rows — claiming
    precision out of a sampling artefact."""
    import random
    rng = random.Random(3)
    for _ in range(300):
        per = {f"ns-{i}": (0, 0) for i in range(0)}
        per = {}
        for i in range(rng.randint(2, 10)):
            n = rng.randint(1, 6)
            per[f"ns-{i}"] = (sum(rng.random() < 0.5 for _ in range(n)), n)
        assert 0.0 <= intracluster_correlation(per) <= 1.0, per


def test_the_effective_n_never_exceeds_the_rows_nor_falls_under_the_specs():
    """The two bounds that make n_eff readable as "observations": it can never
    promise more information than rows were recorded, and never less than the
    number of independent specs those rows came from."""
    import random
    rng = random.Random(5)
    for _ in range(300):
        pattern = {f"ns-{i}": [rng.random() < 0.6
                               for _ in range(rng.randint(1, 7))]
                   for i in range(rng.randint(2, 12))}
        card = _trial_card(pattern)
        assert card.ran_spec_count <= card.effective_n <= len(card.ran), (
            card.effective_n, card.ran_spec_count, len(card.ran))


# --------------------------------------------------------------------------- #
# 7. The three spec-counted floors nothing was pinning (review REQUIRED 2)
# --------------------------------------------------------------------------- #

def test_corpus_shortfall_counts_specs_so_trials_cannot_hide_a_filtered_slice():
    """Reviewer scenario (a): 19 surviving specs of a 55-spec corpus, replayed
    3×. Counted on ROWS that is 57 loaded of 55 — over the corpus, so the rule
    that exists to catch "just run the ones that still resolve" would not only
    pass it, it would report the filtered slice as MORE than the whole corpus.
    Counted on specs it is 19 of 55 and refuses."""
    slice_run = _trial_card({f"ns-{i}": [True, True, True] for i in range(19)},
                            corpus_available=55)
    rows = slice_run.total
    assert rows == 57 and rows > 55 * MIN_CORPUS_LOADED_FRACTION, (
        f"precondition: {rows} rows clear the loaded-fraction floor")
    reason = corpus_shortfall(slice_run)
    assert reason, "a 19-of-55 slice must not read as adequate coverage"
    assert "19 of 55" in reason, reason
    # Positive control: the same rule must still pass a run that IS the corpus.
    full = _trial_card({f"ns-{i}": [True, True, True] for i in range(55)},
                       corpus_available=55)
    assert corpus_shortfall(full) == ""


def test_the_gate_first_run_floor_counts_specs_so_a_probe_cannot_repeat_past_it():
    """Reviewer scenario (b): a 3-spec probe replayed 4× with no baseline. 12
    rows clears a 10-row floor; 3 specs does not clear a 10-SPEC floor. The
    first-run branch is the one place with no baseline to be narrower than, so
    this floor is the only thing standing between a probe and becoming the
    reference every later run is compared against."""
    probe = _trial_card({f"ns-{i}": [True, True, True, True] for i in range(3)})
    assert len(probe.ran) == 12 > MIN_PUBLISHABLE_SPECS, "precondition: rows clear"
    gate = northstar_gate(probe, None)
    assert not gate.passed, gate.reasons
    assert any("only 3 spec(s) ran" in r for r in gate.reasons), gate.reasons
    # Positive control: 10 specs run once IS a first run the gate accepts.
    assert northstar_gate(
        _trial_card({f"ns-{i}": [True] for i in range(10)}), None).passed


def test_the_gate_narrowing_check_counts_specs_so_trials_cannot_fake_breadth():
    """Reviewer scenario (c): 20 specs × 3 trials against a 55-spec baseline. On
    rows that is 60 vs 55 — BROADER — so the check that stops a narrower run
    redefining "no regression" would read the narrowing as an expansion and let
    it through. The gate must name the narrowing."""
    baseline = _trial_card({f"ns-{i}": [True] for i in range(55)})
    narrower = _trial_card({f"ns-{i}": [True, True, True] for i in range(20)})
    assert len(narrower.ran) == 60 > len(baseline.ran), "precondition: more rows"
    gate = northstar_gate(narrower, baseline)
    assert not gate.passed, gate.reasons
    assert any("measured 20 spec(s)" in r and "measured 55" in r
               for r in gate.reasons), gate.reasons
    # Positive control: equal spec counts with trials on top must NOT be called
    # narrowing — otherwise this rule bans --trials outright.
    same = _trial_card({f"ns-{i}": [True, True, True] for i in range(55)})
    assert northstar_gate(same, baseline).passed, northstar_gate(same, baseline).reasons


# --------------------------------------------------------------------------- #
# 8. The gate compares the estimator the card PUBLISHES (review REQUIRED 3)
# --------------------------------------------------------------------------- #

def test_the_gate_measures_the_success_drop_on_the_spec_mean_not_pooled_rows():
    """A regression gate that reads a different number than the report prints
    is not a gate on the report.

    The two diverge exactly when trials are uneven — i.e. after a resume, the
    situation the gate is most likely to meet in anger. Here ten specs are
    solid and replayed ten times each, and ten specs are broken and got one
    trial apiece before the run died. Half the corpus is failing and the card
    publishes 50%; POOLING weights the ten specs that ran most and reports 91%,
    which is ABOVE the 90% baseline. On the pooled figure this run is an
    improvement and the gate goes green on a 40-point regression."""
    baseline = _trial_card({f"ns-{i}": [i < 18] for i in range(20)})
    resumed = _trial_card({
        **{f"ns-{i}": [True] * 10 for i in range(10)},
        **{f"ns-{i}": [False] for i in range(10, 20)},
    })
    # Preconditions: the two estimators disagree, and they disagree ACROSS the
    # gate's threshold — pooled says improvement, spec-mean says collapse.
    assert resumed.success_rate > baseline.success_rate, (
        resumed.success_rate, baseline.success_rate)
    assert resumed.spec_mean_success_rate == pytest.approx(0.5)
    assert baseline.spec_mean_success_rate == pytest.approx(0.9)
    # ...and nothing ELSE about this run would block it, so a red gate here can
    # only be the success check.
    assert resumed.ran_spec_count == baseline.ran_spec_count == 20

    gate = northstar_gate(resumed, baseline)
    assert not gate.passed, gate.reasons
    assert any("success rate dropped 90% → 50%" in r for r in gate.reasons), \
        gate.reasons
    # The published headline and the gate's number are the same number.
    assert f"{resumed.spec_mean_success_rate:.1%}" in success_headline(resumed)


def test_the_gate_still_passes_a_run_whose_spec_mean_held_up(bench_env):
    """Positive control for the change above. A check that fires on every
    uneven run would be indistinguishable from the one just added, and would
    make `--resume` permanently red."""
    baseline = _trial_card({f"ns-{i}": [i < 18] for i in range(20)})
    uneven = _trial_card({
        **{f"ns-{i}": [True] * 10 for i in range(18)},
        **{f"ns-{i}": [False] for i in range(18, 20)},
    })
    assert len({n for _, n in uneven.per_spec_passes.values()}) == 2, "uneven"
    assert uneven.spec_mean_success_rate == pytest.approx(0.9)
    assert northstar_gate(uneven, baseline).passed, \
        northstar_gate(uneven, baseline).reasons


# --------------------------------------------------------------------------- #
# 9. Small truths (review CHEAP 6)
# --------------------------------------------------------------------------- #

def test_a_spec_skipped_in_every_trial_reads_as_unmeasured_not_as_zero_passes():
    """`0/3` and `0/0` are different claims and the reliability table printed
    both as a fraction. A spec that never ran has no denominator — rendering it
    as a measured zero puts a spec the instrument could not reach next to specs
    it genuinely failed, in the one table a reader scans for "what can this
    agent not do"."""
    scores = [_sc("ns-ran", t, t == 0) for t in range(3)]
    scores += [_sc("ns-never", t, False, status="skipped") for t in range(3)]
    card = NorthStarCard(scores=scores, created_at="x", label="l", trials=3)
    md = render_northstar_md(card)
    assert "| ns-never | — (not measured) |" in md, md
    assert "| ns-never | 0/0 |" not in md
    # The spec that DID run still reports its real fraction and its flip.
    assert "| ns-ran | 1/3 | ⚠ flips |" in md, md


def test_a_checkpoint_under_a_different_trials_count_says_so_before_re_running(
        tmp_path, monkeypatch):
    """`--trials` is part of the checkpoint key, so changing it points --resume
    at a filename that does not exist and the run starts from zero — while the
    banked work sits on disk one character away. Adopting it would be the
    double-count the key exists to prevent; saying nothing bills the operator
    for it twice with no way to know why. One line."""
    d = tmp_path / "specs"
    _write_specs(d, 3)
    _patch_env(monkeypatch, tmp_path)
    results = tmp_path / "results"
    results.mkdir(parents=True, exist_ok=True)
    from no_human.cli.commands import _slug, _spec_set_key
    from no_human.eval.bench_task import load_bench_tasks

    specs = load_bench_tasks(d, subset="core")
    # A 3-trial run banked 6 results, then the operator re-runs with --trials 1.
    NorthStarCard(
        scores=[_sc(f"ns-t{i}", t, True) for i in range(2) for t in range(3)],
        created_at="x", label="t", trials=3,
    ).save(results / f"progress-{_slug('t')}-{_spec_set_key(specs, 3)}.json")

    _CountingRunner.reset()
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner",
                        _CountingRunner)
    res = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--label", "t", "--resume"])

    assert res.exit_code == 0, res.output
    assert ("6 banked result(s) under a different --trials are not resumed"
            in res.output), res.output
    # Said, not adopted: the run really did start from zero.
    assert _CountingRunner.calls == {"ns-t0": 1, "ns-t1": 1, "ns-t2": 1}


def test_a_plain_resume_with_nothing_stranded_stays_silent(tmp_path, monkeypatch):
    """Positive control: the line above must not appear on every first run, or
    it is noise and gets tuned out exactly when it matters."""
    d = tmp_path / "specs"
    _write_specs(d, 3)
    _patch_env(monkeypatch, tmp_path)
    _CountingRunner.reset()
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner",
                        _CountingRunner)
    res = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--label", "t", "--resume"])
    assert res.exit_code == 0, res.output
    assert "under a different --trials" not in res.output, res.output
