"""Publishing a benchmark run is an act, not a side effect of finishing one.

Three incidents, one root cause: the runner treated every completed run as
authoritative. A quota-saturated 141-spec run and a one-spec `--limit` probe
each overwrote the committed report and the gate baseline, and the probe's
"clean completion" deleted the long run's checkpoint.

The refusals are asserted against REAL RUN SHAPES rather than invented ones,
and the CLI wiring is asserted separately from the predicate — a previous
attempt at this fix had a guard whose wiring was never exercised, so deleting
the guard left the whole suite green.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from no_human.cli.commands import _slug, cli
from no_human.eval.northstar import BenchScore
from no_human.eval.northstar_card import (
    NorthStarCard,
    publish_refusals,
    render_northstar_md,
)


def _score(task_id: str, *, nh_tokens: int = 20_000,
           status: str = "done", satisfied: bool = True) -> BenchScore:
    return BenchScore(
        task_id=task_id, title=task_id, outcome_status=status,
        goal_satisfied=satisfied, escalated_honestly=False, mergeable=None,
        nh_tokens=nh_tokens, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=100_000,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=1.0, orig_corrections=0,
    )


def _card(scores, label="run") -> NorthStarCard:
    return NorthStarCard(scores=scores, created_at="2026-07-22T00:00:00+00:00",
                         label=label)


def _healthy(n=30) -> NorthStarCard:
    return _card([_score(f"ns-{i}") for i in range(n)], label="healthy")


# --------------------------- the predicate ------------------------------- #

def test_a_healthy_run_publishes():
    assert publish_refusals(_healthy()) == []


def test_the_v14_shape_is_refused():
    """THE case the previous fix missed. v14 was not dead from spec #1 — one
    spec worked and then the quota saturated, so an `any(zero-token)` check
    passed it. The fraction is what catches it."""
    scores = [_score("ns-0")] + [
        _score(f"ns-{i}", nh_tokens=0, status="escalated", satisfied=False)
        for i in range(1, 97)
    ]
    refusals = publish_refusals(_card(scores, label="v14"))
    assert any("zero tokens" in r for r in refusals), refusals


def test_a_cache_only_run_is_not_refused_as_saturated():
    """The resumed/attempt-2 shape: every row's burn is cache-read, so
    nh_tokens == 0 across the whole run while real priced spend is real work.
    `publish_refusals` must ask the shared did-priced-work predicate — its own
    inline `not s.nh_tokens` copy (the third one this fix removed) would call
    this run 100% dead and refuse it as a quota outage it wasn't."""
    scores = []
    for i in range(30):
        s = _score(f"ns-{i}", nh_tokens=0)
        s.nh_cache_tokens = 200_000
        scores.append(s)
    card = _card(scores, label="cache-only")
    assert all(not s.nh_tokens for s in card.ran), "fixture: zero fresh tokens"
    assert all(s.nh_priced_tokens for s in card.ran), \
        "fixture: every row carries real priced spend"
    assert card.dead_specs == 0
    assert publish_refusals(card) == []


def test_a_one_spec_probe_is_refused():
    """The `--limit 1` health probe that replaced the gate baseline."""
    refusals = publish_refusals(_card([_score("ns-0", nh_tokens=0)]))
    assert any("minimum" in r for r in refusals), refusals


def test_a_narrower_run_may_not_replace_a_broader_baseline():
    refusals = publish_refusals(_healthy(12), previous=_healthy(56))
    assert any("narrow" in r for r in refusals), refusals


def test_a_broader_run_may_replace_a_narrower_baseline():
    assert publish_refusals(_healthy(56), previous=_healthy(12)) == []


def test_an_all_skipped_run_is_refused():
    scores = [_score(f"ns-{i}", nh_tokens=0, status="skipped",
                     satisfied=False) for i in range(20)]
    refusals = publish_refusals(_card(scores))
    assert any("nothing ran" in r for r in refusals), refusals


def test_skips_do_not_count_as_dead_specs():
    """A skip is a decision (not a git repo, monorepo too large); a zero-token
    RUN is a death. Counting skips as deaths would refuse valid runs — the real
    v13 has three of them.

    The fixture is LOAD-BEARING IN BOTH DIRECTIONS and must stay that way:
      - 7 skipped of 37 loaded = 19%, under the coverage ceiling, so this run
        is publishable today;
      - but 7 of the 30 that RAN = 23%, so if a skip were miscounted as a death
        the dead rule fires and this test fails.
    An earlier version used 4 skips to duck the coverage rule; that made the
    mutant it exists to catch survive the entire suite. Do not lower it.
    """
    from no_human.eval.northstar_card import (
        MAX_DEAD_FRACTION,
        MAX_UNMEASURED_FRACTION,
    )
    # Pin the WINDOW, not just the count: moving either constant silently
    # defuses the mutant this test exists to catch (raising MAX_DEAD_FRACTION
    # to 0.25 made it survive the whole suite).
    assert 7 / 37 <= MAX_UNMEASURED_FRACTION, "fixture would trip coverage"
    assert 7 / 30 > MAX_DEAD_FRACTION, "fixture no longer kills the mutant"
    scores = [_score(f"ns-{i}") for i in range(30)]
    scores += [_score(f"sk-{i}", nh_tokens=0, status="skipped",
                      satisfied=False) for i in range(7)]
    assert publish_refusals(_card(scores)) == []


def test_a_mostly_unmeasured_run_is_refused_as_the_baseline():
    """The incident shape, which published CLEAN before coverage was shared.

    36 of 55 specs pinned at a repo that no longer exists are SKIPS, so zero
    were dead; 19 ran, over the floor; and a fresh clone has no baseline to
    narrow against. Every existing rule passed and a 65%-unmeasured run became
    the reference every later run was compared against.
    """
    scores = [_score(f"ns-{i}") for i in range(19)]
    scores += [_score(f"sk-{i}", nh_tokens=0, status="skipped",
                      satisfied=False) for i in range(36)]
    refusals = publish_refusals(_card(scores))
    assert any("went unmeasured" in r for r in refusals), refusals
    assert any("36/55" in r for r in refusals), refusals


def test_gate_and_publish_agree_about_coverage():
    """They ask different questions elsewhere, but 'did we measure enough of
    the benchmark' must not get two answers — that disagreement is what let the
    incident run be rejected by one and published by the other."""
    from no_human.eval.northstar_card import northstar_gate
    scores = [_score(f"ns-{i}") for i in range(19)]
    scores += [_score(f"sk-{i}", nh_tokens=0, status="skipped",
                      satisfied=False) for i in range(36)]
    card = _card(scores)
    # Assert the REASON, not merely that both refuse: otherwise this stays green
    # if some unrelated publish rule starts firing on this shape.
    assert any("went unmeasured" in r for r in publish_refusals(card))
    gate = northstar_gate(card, None)
    assert not gate.passed
    assert any("went unmeasured" in r for r in gate.reasons)


def test_gate_and_publish_agree_about_the_CORPUS_SHORTFALL_too():
    """The other half of the shared rule, which had no defender at all: deleting
    publish's corpus_shortfall check left the whole suite green. The agreement
    test above cannot cover it, because its card leaves `corpus_available` at 0
    and the shortfall rule is silent on both sides."""
    from no_human.eval.northstar_card import northstar_gate
    # 11 loaded of 55 available, all healthy — coverage is silent (0 unmeasured),
    # so ONLY the shortfall rule can fire.
    card = _card([_score(f"ns-{i}") for i in range(11)])
    card.corpus_available = 55
    pub = publish_refusals(card)
    gate = northstar_gate(card, None)
    assert any("11 of 55" in r for r in pub), pub
    assert not gate.passed
    assert any("11 of 55" in r for r in gate.reasons), gate.reasons


# ------------------------------ the wiring -------------------------------- #
# Asserted end-to-end through the CLI: the predicate being right is worthless
# if the command ignores it. Neutering publish_refusals must fail these.

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


def test_publishing_a_bad_run_exits_nonzero_and_changes_nothing(bench_env):
    results, report = bench_env
    bad = results / "v14.json"
    _card([_score("ns-0")] + [
        _score(f"ns-{i}", nh_tokens=0, status="escalated", satisfied=False)
        for i in range(1, 97)], label="v14").save(bad)

    res = CliRunner().invoke(cli, ["bench", "publish", str(bad)])

    assert res.exit_code == 1, res.output
    assert "refusing to publish" in res.output
    assert report.read_text(encoding="utf-8") == "ORIGINAL REPORT\n", "report was overwritten"
    assert not (results / "latest.json").exists(), "baseline was overwritten"


def test_publishing_a_good_run_writes_the_baseline_and_report(bench_env):
    results, report = bench_env
    good = results / "v13.json"
    _healthy(30).save(good)

    res = CliRunner().invoke(cli, ["bench", "publish", str(good)])

    assert res.exit_code == 0, res.output
    assert "published" in res.output
    assert (results / "latest.json").exists()
    assert "North-star benchmark" in report.read_text(encoding="utf-8")
    assert json.loads((results / "latest.json").read_text(encoding="utf-8"))["label"] == "healthy"


def test_force_publishes_but_records_the_refusals_it_overrode(bench_env):
    results, report = bench_env
    bad = results / "v14.json"
    _card([_score("ns-0")] + [
        _score(f"ns-{i}", nh_tokens=0, status="escalated", satisfied=False)
        for i in range(1, 97)], label="v14").save(bad)

    res = CliRunner().invoke(cli, ["bench", "publish", str(bad), "--force"])

    assert res.exit_code == 0, res.output
    saved = json.loads((results / "latest.json").read_text(encoding="utf-8"))
    assert saved["override_reasons"], "a forced publish must record what it overrode"
    assert any("zero tokens" in r for r in saved["override_reasons"])
    # ...and it must be impossible to mistake the report for a clean one.
    text = report.read_text(encoding="utf-8")
    assert "WARNING" in text and "--force" in text


def test_publishing_a_missing_file_fails_cleanly(bench_env):
    res = CliRunner().invoke(cli, ["bench", "publish", "no-such-run.json"])
    assert res.exit_code == 1
    assert "not a readable results file" in res.output


# --------------------- SCRUM-25: the published-baseline file --------------- #

def test_a_clean_publish_also_writes_the_published_baseline(bench_env):
    """The file the Stats panel headlines from must exist after any clean
    publish — this is what lets a LATER forced publish be demoted to a
    footnote instead of erasing the record of the last trustworthy run."""
    results, report = bench_env
    good = results / "v13.json"
    _healthy(30).save(good)

    res = CliRunner().invoke(cli, ["bench", "publish", str(good)])

    assert res.exit_code == 0, res.output
    assert (results / "published_baseline.json").exists()
    assert json.loads(
        (results / "published_baseline.json").read_text(encoding="utf-8"))["label"] == "healthy"


def test_a_forced_publish_does_not_overwrite_the_published_baseline(bench_env):
    """A probe force-published over refusals must not clobber the last clean
    baseline — that is the whole point of keeping the two files separate."""
    results, report = bench_env
    good = results / "v13.json"
    _healthy(30).save(good)
    assert CliRunner().invoke(cli, ["bench", "publish", str(good)]).exit_code == 0
    clean_baseline = (results / "published_baseline.json").read_text(encoding="utf-8")

    bad = results / "v14.json"
    _card([_score("ns-0")] + [
        _score(f"ns-{i}", nh_tokens=0, status="escalated", satisfied=False)
        for i in range(1, 97)], label="v14").save(bad)
    res = CliRunner().invoke(cli, ["bench", "publish", str(bad), "--force"])

    assert res.exit_code == 0, res.output
    assert json.loads((results / "latest.json").read_text(encoding="utf-8"))["label"] == "v14"
    assert (results / "published_baseline.json").read_text(encoding="utf-8") == clean_baseline, \
        "the forced probe publish overwrote the last clean baseline"


# ------------------------- checkpoint isolation --------------------------- #

@pytest.mark.parametrize("label,expected", [
    ("expanded-core-v13", "expanded-core-v13"),
    ("../../etc/passwd", "etc-passwd"),   # a label becomes a path
    ("a b/c", "a-b-c"),
    ("", "run"),
    ("...", "run"),
])
def test_a_label_cannot_escape_the_results_directory(label, expected):
    assert _slug(label) == expected


def test_the_report_names_publish_not_run_as_its_source():
    """The report said 'Generated by nh bench run' while a run no longer
    publishes — the instruction would recreate the incident."""
    text = render_northstar_md(_healthy())
    assert "nh bench publish" in text


# ------------------- narrowing by SKIPS, not by count ---------------------- #

def test_a_mass_skip_run_may_not_replace_a_broader_baseline():
    """The narrowing guard compares RAN, not total. Skipping is the documented
    dominant failure mode (specs pin to local repo paths), so a run that loads
    the whole corpus and skips most of it has the SAME total as the baseline
    while measuring a fraction of it — and every headline is computed over ran.
    Left on `total`, this publishes '100% success' measured over 16 specs."""
    baseline = _card([_score(f"ns-{i}") for i in range(56)], label="v13")
    mass_skip = _card(
        [_score(f"ns-{i}") for i in range(16)]
        + [_score(f"ns-{i}", nh_tokens=0, status="skipped", satisfied=False)
           for i in range(16, 56)],
        label="mass-skip")
    assert mass_skip.total == baseline.total, "precondition: same total"
    refusals = publish_refusals(mass_skip, previous=baseline)
    assert any("narrow" in r for r in refusals), refusals


def test_the_aggregate_reports_dead_specs():
    """A sub-threshold saturation must be legible without scanning the per-task
    table for zeroes."""
    scores = [_score(f"ns-{i}") for i in range(18)]
    scores += [_score(f"d-{i}", nh_tokens=0, status="escalated", satisfied=False)
               for i in range(2)]
    card = _card(scores)
    assert publish_refusals(card) == [], "precondition: publishable"
    assert card.as_dict()["aggregate"]["dead_specs"] == 2


def test_the_report_states_the_dead_spec_count_even_when_zero():
    """A published run is under the refusal threshold by construction, so what a
    reader needs is confirmation that saturation was checked — not a figure that
    appears only when things are bad."""
    text = render_northstar_md(_healthy())
    assert "burned zero tokens" in text
    assert "**0**" in text


def test_publish_reads_the_current_baseline_before_deciding(bench_env):
    """The narrowing refusal was asserted only at the predicate level; the CLI's
    lookup of the existing baseline was unwired-untested, so `previous = None`
    left the whole suite green and a narrower run could quietly replace a
    broader one."""
    results, report = bench_env
    _healthy(56).save(results / "latest.json")
    baseline_before = (results / "latest.json").read_text(encoding="utf-8")
    narrower = results / "narrow.json"
    _healthy(12).save(narrower)

    res = CliRunner().invoke(cli, ["bench", "publish", str(narrower)])

    assert res.exit_code == 1, res.output
    assert "narrow" in res.output
    assert (results / "latest.json").read_text(encoding="utf-8") == baseline_before, \
        "the narrower run replaced the baseline it was supposed to be refused against"


def test_the_minimum_spec_floor_counts_RAN_not_total():
    """The same operand swap the narrowing guard was caught on in round 1 —
    unfixed here until now. It matters more: `eval/results/northstar/` is
    gitignored, so a FRESH CLONE has no latest.json, the narrowing refusal
    cannot fire, and this floor is the only thing between a mostly-skipped run
    and the baseline. Skipping is the documented dominant failure mode."""
    scores = [_score(f"ns-{i}") for i in range(3)]
    scores += [_score(f"sk-{i}", nh_tokens=0, status="skipped", satisfied=False)
               for i in range(53)]
    card = _card(scores, label="mostly-skipped")
    assert card.total >= 10, "precondition: total alone would clear the floor"
    assert len(card.ran) < 10, "precondition: only 3 specs actually ran"

    refusals = publish_refusals(card, previous=None)   # the fresh-clone case

    assert any("minimum" in r for r in refusals), (
        f"a 3-ran/53-skipped run cleared the floor on `total` and would publish "
        f"'100% success' as the corpus: {refusals}")


def test_dead_specs_counts_deaths_not_skips():
    """`test_skips_do_not_count_as_dead_specs` asserts this through
    publish_refusals' own private `dead` list, so the PROPERTY the aggregate
    reports was never bound. A wrong count here is a false number in the
    published report (the real v13 would read 4 instead of 1)."""
    scores = [_score(f"ns-{i}") for i in range(20)]
    scores += [_score("dead-1", nh_tokens=0, status="escalated", satisfied=False)]
    scores += [_score(f"sk-{i}", nh_tokens=0, status="skipped", satisfied=False)
               for i in range(3)]
    assert _card(scores).dead_specs == 1, "skips were counted as deaths"


def test_a_forced_publish_survives_re_rendering(bench_env):
    """`nh bench report` re-renders from latest.json. If override_reasons does
    not round-trip through save/load, one command launders a forced publish into
    a clean-looking report — defeating the whole guarantee that a forced publish
    is allowed but never invisible."""
    results, report = bench_env
    bad = results / "v14.json"
    _card([_score("ns-0")] + [
        _score(f"ns-{i}", nh_tokens=0, status="escalated", satisfied=False)
        for i in range(1, 97)], label="v14").save(bad)

    assert CliRunner().invoke(
        cli, ["bench", "publish", str(bad), "--force"]).exit_code == 0
    assert "WARNING" in report.read_text(encoding="utf-8"), "precondition: the banner was written"

    res = CliRunner().invoke(cli, ["bench", "report"])

    assert res.exit_code == 0, res.output
    assert "WARNING" in report.read_text(encoding="utf-8"), \
        "re-rendering laundered a forced publish into a clean report"


def test_report_refuses_a_card_no_human_ever_blessed(bench_env):
    """`bench report` is the second of the two write paths into the tracked
    report (`--force` is a flag on the first, not a third writer), and the only
    one that never consulted `publish_refusals`.

    `bench publish` refuses a probe, and this file asserts that. But `bench
    report` renders straight from latest.json and writes unconditionally — so
    the same card publish rejects, report installs. This is not hypothetical:
    the real results dir holds `latest.json` labelled "v14-health-probe" with
    ONE spec, for which `publish_refusals` returns three reasons (zero-token
    fraction, unmeasured fraction, and "only 1 spec(s) ran (minimum 10)").
    Running `nh bench report` there replaced a 53-spec 47%-success report with
    a 1-spec 0% one, exit 0, printing "report rendered".

    The distinguishing signal is `override_reasons`, not the refusals alone: a
    card that was force-published carries them and MUST still re-render, or a
    forced baseline becomes impossible to regenerate (the sibling test above
    pins that). A card with refusals and no override record never passed a
    human at all, and is the one to refuse.
    """
    results, report = bench_env
    _card([_score("ns-0", nh_tokens=0)], label="v14-health-probe").save(
        results / "latest.json")
    assert publish_refusals(
        _card([_score("ns-0", nh_tokens=0)], label="v14-health-probe")), \
        "precondition: this shape must be refusable, or the test proves nothing"

    res = CliRunner().invoke(cli, ["bench", "report"])

    assert res.exit_code == 1, res.output
    # Assert the REASON, not just the code: exit 1 is also what "no results yet"
    # and the banned-term refusal return, so a bare code check would keep passing
    # if this guard were replaced by an unrelated failure.
    assert "refusing to re-render" in res.output, res.output
    assert "minimum 10" in res.output, res.output
    assert report.read_text(encoding="utf-8") == "ORIGINAL REPORT\n", \
        "`bench report` overwrote the tracked benchmark with an unblessed card"


def test_report_refusal_survives_a_hostile_label(bench_env):
    """A refusal that crashes is not a refusal.

    `card.label` is file-authored and is printed into rich markup. The shape
    `@[/Users/dev/probe]` reads to rich as a closing tag and raises MarkupError,
    turning a clean "here is why I refused" into a traceback. The write is
    guarded either way — the print precedes it — but an independent reviewer
    found this by crashing the command, and the sibling guard
    (tests/test_bench_print_escape.py) could not see it because it asserted on
    the FIRST reason line only.
    """
    results, report = bench_env
    _card([_score("ns-0", nh_tokens=0)], label="@[/Users/dev/probe]").save(
        results / "latest.json")

    res = CliRunner().invoke(cli, ["bench", "report"])

    assert res.exit_code == 1, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), \
        f"refusal raised instead of refusing: {res.exception!r}"
    assert "refusing to re-render" in res.output, res.output
    assert report.read_text(encoding="utf-8") == "ORIGINAL REPORT\n"


def test_report_still_renders_a_clean_card(bench_env):
    """The positive control for the guard above. A refusal-free baseline must
    keep re-rendering, otherwise the fix trades a silent overwrite for a
    command that can never regenerate the tracked file."""
    results, report = bench_env
    _healthy(30).save(results / "latest.json")

    res = CliRunner().invoke(cli, ["bench", "report"])

    assert res.exit_code == 0, res.output
    assert "North-star benchmark" in report.read_text(encoding="utf-8")


# --------------------- the banned-term publish guard ----------------------- #
# Same convention as the refusals above: the predicate being right is worthless
# if the command ignores it. Neutering the guard must fail these.

def _note_card(note: str):
    scores = [_score(f"ns-{i}") for i in range(12)]
    # subset="core" is LOAD-BEARING: render_northstar_md emits per-task rows
    # only for core specs, so a "full" fixture puts the note in no rendered
    # text at all and the guard has nothing to catch. The first draft of this
    # test did exactly that and passed while proving nothing.
    for sc in scores:
        sc.subset = "core"
    scores[0].notes = note
    return _card(scores, label="v13")


def test_publish_redacts_a_banned_term_in_a_note(bench_env):
    """The per-task notes are judge-authored free text quoting real repo
    contents. Refusing the whole publish left a real measured card (v13)
    unpublishable without hand-editing. Instead the renderer redacts the term,
    so the report publishes clean AND stays reproducible from the raw card —
    the card keeps the original note, only the tracked markdown is scrubbed."""
    results, report = bench_env
    src = results / "v13.json"
    # The shape that actually slipped through: the term glued to other words.
    card = _note_card("ran `ls -la ~/.claude/skills/windsurf-metrics-core-pipeline/`")  # term-ok: the fixture needs a real banned term
    # Redaction is render-only: the raw note still carries the term, proving the
    # cleaning happens on the way to the tracked file, not by mutating evidence.
    assert "windsurf" in card.scores[0].notes  # term-ok: the fixture needs a real banned term
    card.save(src)

    res = CliRunner().invoke(cli, ["bench", "publish", str(src)])

    assert res.exit_code == 0, res.output
    published = report.read_text(encoding="utf-8")
    assert "windsurf" not in published.lower(), published  # the banned term is gone  # term-ok: the fixture needs a real banned term
    assert "metrics-core-pipeline" in published, published    # its neighbour stays
    assert (results / "latest.json").exists()              # baseline promoted


def test_publish_allows_english_that_merely_contains_a_term(bench_env):
    """A fail-closed guard with no override must not refuse legitimate prose:
    the only escape would be editing a real measured note to satisfy it."""
    results, report = bench_env
    src = results / "v13.json"
    # A short banned term contained inside a longer ordinary-English word is
    # the containment under test — the note MUST keep such a word or this
    # test is vacuous (the word below carries one; see the boundary rule).
    _note_card("pattern recognition in the report generator script").save(src)

    res = CliRunner().invoke(cli, ["bench", "publish", str(src)])

    assert res.exit_code == 0, res.output
    assert (results / "latest.json").exists()


def test_bench_report_redacts_a_banned_term_too(bench_env):
    """`bench report` renders straight from latest.json — a second write path
    into the tracked doc. Redaction has to fire here too, or regenerating the
    doc from a banked card would reintroduce the very term `bench publish`
    scrubbed."""
    results, report = bench_env
    _note_card("ran `ls -la ~/.claude/skills/windsurf-metrics-core-pipeline/`").save(  # term-ok: the fixture needs a real banned term
        results / "latest.json")

    res = CliRunner().invoke(cli, ["bench", "report"])

    assert res.exit_code == 0, res.output
    published = report.read_text(encoding="utf-8")
    assert "windsurf" not in published.lower(), published  # term-ok: the fixture needs a real banned term
    assert "metrics-core-pipeline" in published, published


def test_publish_redacts_a_home_path(bench_env):
    """The report asserts "labels and repo paths are pseudonymised". A home path
    in a note is redacted to a placeholder so the published doc keeps that
    promise while still publishing."""
    results, report = bench_env
    _note_card(f"verified via {Path.home()}/git/thing/file.py").save(
        results / "v13.json")

    res = CliRunner().invoke(cli, ["bench", "publish", str(results / "v13.json")])

    assert res.exit_code == 0, res.output
    published = report.read_text(encoding="utf-8")
    assert str(Path.home()) not in published and "/Users/" not in published
    assert (results / "latest.json").exists()


def test_the_guard_still_refuses_if_redaction_regresses(bench_env, monkeypatch):
    """Redaction cleans the report; the CLI guard is the backstop. If a future
    edit lets a term slip past the redactor (a new BANNED_TERM with no matching
    branch, say), the guard must still refuse rather than publish the leak — so
    disabling redaction must bring the refusal back, proving the guard is live
    and not dead code the redactor has made unreachable."""
    import no_human.eval.northstar_card as nc
    monkeypatch.setattr(nc, "redact_for_publish", lambda s: s)  # redaction off
    results, report = bench_env
    src = results / "v13.json"
    _note_card("ran `ls -la ~/.claude/skills/windsurf-metrics-core-pipeline/`").save(src)  # term-ok: the fixture needs a real banned term

    res = CliRunner().invoke(cli, ["bench", "publish", str(src)])

    assert res.exit_code == 1, res.output
    assert "refusing to publish" in res.output
    assert report.read_text(encoding="utf-8") == "ORIGINAL REPORT\n"
    assert not (results / "latest.json").exists()
