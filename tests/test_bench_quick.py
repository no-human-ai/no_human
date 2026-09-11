"""`nh bench run --quick` — stratified iteration tier.

A full core run takes hours, which throttles measurement-driven iteration.
The quick tier picks ONE representative per coverage cell
(project × runnable × expect_escalation × size-bucket), deterministically, so
quick runs are comparable to each other and still exercise the corpus's whole
variety. It is an ITERATION signal only: selection is fixed (fastest original
wall-clock in the cell, id tie-break), and a quick card can never publish as
the baseline — the existing corpus-coverage machinery refuses it.

SCRUM-43: the representative must also be RESOLVABLE (its repo path — after
repo_map/local translation — is a real git checkout on THIS machine). A cell
whose every member is unresolvable is honestly unmeasurable and drops out of
the tier with a logged note, rather than picking a spec guaranteed to skip.
The gate's coverage denominator must then be the TIER's own expected set
(what select_quick_subset picks from the canonical corpus), not the full
corpus — a dozen-cell tier is 100% of itself but ~20% of a 54-spec corpus.
"""
from __future__ import annotations

import logging

from no_human.eval.bench_task import (
    BenchTask,
    is_resolvable,
    quick_cell,
    select_quick_subset,
)


def _spec(id, repo_root, project="p", runnable=True, esc=False, wall=100.0,
          resolvable=True):
    """A BenchTask whose repo path is a REAL git checkout under *repo_root*
    when *resolvable*, or a path that resolves nowhere otherwise — the same
    structural distinction ``is_resolvable`` checks in production, not a
    mirror of it."""
    if resolvable:
        d = repo_root / project
        (d / ".git").mkdir(parents=True, exist_ok=True)
        path = str(d)
    else:
        path = str(repo_root / "does-not-exist" / project)
    return BenchTask(
        id=id, title=id, request="r", subset="core", runnable=runnable,
        expect_escalation=esc,
        repo={"path": path, "pin": "", "branch": ""},
        original={"wall_clock_s": wall},
        spec_repo_path=path,
    )


def test_selects_one_per_resolvable_cell_and_covers_every_such_cell(tmp_path):
    specs = [
        _spec("ns-a1", tmp_path, project="alpha", wall=500),
        _spec("ns-a2", tmp_path, project="alpha", wall=100),          # same cell as a1
        _spec("ns-b1", tmp_path, project="beta", wall=100),
        _spec("ns-c1", tmp_path, project="alpha", esc=True, wall=100),
        _spec("ns-e1", tmp_path, project="alpha", wall=7200),         # L bucket
    ]
    picked = select_quick_subset(specs)
    assert {quick_cell(s) for s in picked} == {quick_cell(s) for s in specs}
    assert len(picked) == len({quick_cell(s) for s in specs})


def test_a_non_runnable_only_cell_is_dropped_not_selected(tmp_path):
    """A ``runnable: false`` spec is, by definition, unresolvable — selecting
    it as the tier's one representative for its cell would pin the tier to a
    guaranteed skip. Its cell must drop out instead."""
    specs = [
        _spec("ns-a1", tmp_path, project="alpha", wall=100),
        _spec("ns-d1", tmp_path, project="alpha", runnable=False, wall=0),
    ]
    picked = select_quick_subset(specs)
    assert quick_cell(specs[1]) not in {quick_cell(s) for s in picked}
    assert [s.id for s in picked] == ["ns-a1"]


def test_picks_fastest_original_wall_clock_with_id_tiebreak(tmp_path):
    specs = [
        _spec("ns-slow", tmp_path, wall=400),   # same S bucket (<600s) — same cell
        _spec("ns-fast", tmp_path, wall=50),
        _spec("ns-tie-b", tmp_path, wall=50.0),
    ]
    picked = select_quick_subset(specs)
    assert len(picked) == 1
    # 50s beats 400s; between the two 50s specs the lexically-smaller id wins.
    assert picked[0].id == "ns-fast"


def test_selection_is_deterministic_and_order_independent(tmp_path):
    specs = [
        _spec("ns-a", tmp_path, project="alpha", wall=10),
        _spec("ns-b", tmp_path, project="beta", wall=20),
        _spec("ns-c", tmp_path, project="alpha", esc=True, wall=30),
    ]
    a = [s.id for s in select_quick_subset(specs)]
    b = [s.id for s in select_quick_subset(list(reversed(specs)))]
    assert a == b


def test_prefers_a_resolvable_spec_over_a_faster_unresolvable_one(tmp_path):
    """The fastest spec in the cell is unresolvable; a slower one in the same
    cell IS resolvable. The tier must still prefer the resolvable spec —
    picking the "fastest" one that is guaranteed to skip defeats the whole
    point of the representative."""
    specs = [
        _spec("ns-fast-broken", tmp_path, project="alpha", wall=10,
              resolvable=False),
        _spec("ns-slow-ok", tmp_path, project="alpha", wall=999,
              resolvable=True),
    ]
    picked = select_quick_subset(specs)
    assert [s.id for s in picked] == ["ns-slow-ok"]
    assert is_resolvable(specs[1]) and not is_resolvable(specs[0])


def test_drops_a_cell_whose_every_spec_is_unresolvable_and_logs_why(
        tmp_path, caplog):
    resolvable_cell = [_spec("ns-ok", tmp_path, project="alpha", wall=10)]
    dead_cell = [
        _spec("ns-dead1", tmp_path, project="ghost", wall=10, resolvable=False),
        _spec("ns-dead2", tmp_path, project="ghost", wall=20, resolvable=False),
    ]
    with caplog.at_level(logging.WARNING, logger="no_human.eval.bench_task"):
        picked = select_quick_subset(resolvable_cell + dead_cell)

    assert [s.id for s in picked] == ["ns-ok"]
    ghost_cell = quick_cell(dead_cell[0])
    [record] = [r for r in caplog.records if "ghost" in r.getMessage()
                or str(ghost_cell) in r.getMessage()]
    message = record.getMessage()
    assert str(ghost_cell) in message, message
    assert "all specs in cell" in message and "are unresolvable" in message, message
    assert "2" in message, message  # count of dropped specs


def test_is_resolvable_requires_absolute_real_git_checkout(tmp_path):
    real = tmp_path / "checkout"
    (real / ".git").mkdir(parents=True)
    resolvable = _spec("ns-x", tmp_path, project="checkout", resolvable=True)
    assert is_resolvable(resolvable)

    not_runnable = _spec("ns-y", tmp_path, project="checkout", runnable=False)
    assert not is_resolvable(not_runnable)

    missing = _spec("ns-z", tmp_path, project="nope", resolvable=False)
    assert not is_resolvable(missing)

    relative = BenchTask(id="ns-rel", title="t", request="r", runnable=True,
                         repo={"path": "relative/path", "pin": "", "branch": ""})
    assert not is_resolvable(relative)

    no_dotgit = tmp_path / "plain_dir"
    no_dotgit.mkdir()
    not_a_repo = BenchTask(id="ns-nogit", title="t", request="r", runnable=True,
                           repo={"path": str(no_dotgit), "pin": "", "branch": ""})
    assert not is_resolvable(not_a_repo)


def test_cli_quick_runs_only_the_selected_subset(tmp_path, monkeypatch):
    import yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    # Two cells: 3 specs in alpha/S (one representative) + 1 in beta/S. Real
    # (empty) git checkouts so the specs are RESOLVABLE — otherwise the whole
    # tier would now (correctly) drop to nothing.
    repos = tmp_path / "repos"
    for i, (proj, wall) in enumerate(
            [("alpha", 100), ("alpha", 50), ("alpha", 200), ("beta", 100)]):
        repo_dir = repos / proj
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        s = BenchTask(id=f"ns-q{i}", title=f"t{i}", request="r", subset="core",
                      runnable=True,
                      repo={"path": str(repo_dir), "pin": "", "branch": ""},
                      original={"wall_clock_s": wall})
        (d / f"ns-q{i}.yaml").write_text(yaml.safe_dump(s.to_dict()))

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
    # The canonical corpus (used for the gate denominator) IS this same
    # 2-cell spec set here, so the quick tier's expected size is 2.
    monkeypatch.setattr("no_human.eval.bench_task.NORTHSTAR_DIR", d)

    ran: list[str] = []

    class _Probe:
        def __init__(self, *a, **kw): ...

        async def run_one(self, spec, *, workdir):
            ran.append(spec.id)
            return BenchScore(
                task_id=spec.id, title=spec.title, outcome_status="skipped",
                goal_satisfied=None, escalated_honestly=False, mergeable=None,
                nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
                orig_cache_tokens=0, orig_cache_creation_tokens=0,
                orig_wall_clock_s=0.0, orig_corrections=0, subset=spec.subset)

    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _Probe)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--quick"])
    assert result.exit_code == 0, result.output
    # alpha/S representative = fastest (ns-q1, 50s) + beta/S's only spec.
    assert sorted(ran) == ["ns-q1", "ns-q3"], ran
    assert "quick tier" in result.output
    assert "iteration signal" in result.output


def test_cli_quick_and_full_are_mutually_exclusive(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    result = CliRunner().invoke(cli, ["bench", "run", "--quick", "--full"])
    assert result.exit_code != 0
    assert "--quick" in result.output and "--full" in result.output


QUICK_GATE_PROJECTS = [f"p{i}" for i in range(15)]  # clears MIN_PUBLISHABLE_SPECS (10)
# Five cells get a SECOND (slower) member so the canonical corpus (20) is
# STRICTLY larger than the tier (15 cells): with tier == corpus the fixture
# could not distinguish the runtime override from the card value, and the
# review proved both blocker-1 reintroduction and dropping tier_expected
# passed the whole suite (mutation escapes).
QUICK_GATE_EXTRA = [f"p{i}" for i in range(5)]


def _quick_gate_setup(tmp_path, monkeypatch, *, run_specs_dir):
    """Shared plumbing for the tier-aware-denominator gate tests: a canonical
    20-spec / 15-cell corpus (all resolvable, real git checkouts; five projects
    carry a second slower same-cell spec, so the corpus is STRICTLY larger
    than the tier) is always used
    for the gate's expected-size computation; the RUN loads from
    *run_specs_dir*. 15 cells (not 3) so the first-run MIN_PUBLISHABLE_SPECS
    floor (10) never confounds the coverage-denominator assertion below."""
    import yaml
    from no_human.cli import commands as cmds
    from no_human.eval.northstar import BenchScore

    repos = tmp_path / "repos"
    canon = tmp_path / "canonical"
    canon.mkdir()
    for i, proj in enumerate(QUICK_GATE_PROJECTS):
        repo_dir = repos / proj
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        s = BenchTask(id=f"ns-{proj}", title=f"t{i}", request="r",
                      subset="core", runnable=True,
                      repo={"path": str(repo_dir), "pin": "", "branch": ""},
                      original={"wall_clock_s": 100})
        (canon / f"ns-{proj}.yaml").write_text(yaml.safe_dump(s.to_dict()))
        (run_specs_dir / f"ns-{proj}.yaml").write_text(
            yaml.safe_dump(s.to_dict()))
    for i, proj in enumerate(QUICK_GATE_EXTRA):
        repo_dir = repos / proj
        s2 = BenchTask(id=f"ns-{proj}-slow", title=f"t{i}-slow", request="r",
                       subset="core", runnable=True,
                       repo={"path": str(repo_dir), "pin": "", "branch": ""},
                       original={"wall_clock_s": 500})
        (canon / f"ns-{proj}-slow.yaml").write_text(yaml.safe_dump(s2.to_dict()))
        (run_specs_dir / f"ns-{proj}-slow.yaml").write_text(
            yaml.safe_dump(s2.to_dict()))

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
    monkeypatch.setattr("no_human.eval.bench_task.NORTHSTAR_DIR", canon)

    class _OkRunner:
        def __init__(self, *a, **kw): ...

        async def run_one(self, spec, *, workdir):
            return BenchScore(
                task_id=spec.id, title=spec.title,
                outcome_status="awaiting_approval", goal_satisfied=True,
                escalated_honestly=False, mergeable=None, nh_tokens=500,
                nh_cache_tokens=0, nh_cache_creation_tokens=0, nh_turns=3,
                nh_wall_clock_s=1.0, orig_tokens=1000, orig_cache_tokens=0,
                orig_cache_creation_tokens=0, orig_wall_clock_s=1.0,
                orig_corrections=0, subset=spec.subset)

    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _OkRunner)


def test_quick_gate_passes_with_the_whole_tier_denominator_not_corpus(
        tmp_path, monkeypatch):
    """15 cells in the canonical corpus -> quick tier expects 15 specs. All 15
    load and run here, so coverage is 15/15 = 100%: the gate must pass. Judged
    against the OLD (full-corpus) denominator this would have been 15 of a
    much larger number and could have refused outright — the live incident
    this task fixes."""
    run_dir = tmp_path / "run_specs"
    run_dir.mkdir()
    _quick_gate_setup(tmp_path, monkeypatch, run_specs_dir=run_dir)

    from click.testing import CliRunner
    from no_human.cli.commands import cli

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(run_dir), "--quick", "--gate"])
    assert result.exit_code == 0, result.output
    assert "gate FAILED" not in result.output, result.output
    flat = result.output.replace("\n", "")
    assert "not publishable as the baseline" in flat, result.output
    # The card on disk must record the FULL canonical corpus (20), never the
    # tier (15): review mutation test proved corpus_available=tier published a
    # quick card clean. And a 15-spec run against a 20-spec corpus is 75% <
    # 80%, so dropping the runtime tier_expected override fails this test too.
    import json as _json
    results = sorted((tmp_path / "results").glob("run-*.json"))
    assert results, "the run must save a results card"
    saved = _json.loads(results[-1].read_text(encoding="utf-8"))
    assert saved["aggregate"]["corpus_available"] == 20, saved["aggregate"]
    assert len(saved["scores"]) == 15


def test_quick_gate_fails_when_a_selected_subset_member_is_missing(
        tmp_path, monkeypatch):
    """The canonical corpus still has 15 cells (tier expects 15), but the
    run's --specs-dir is missing 4 of them entirely — the run can only ever
    load 11/15 of the tier (73% < 80%), even though the 11 that DID load are
    100% present among themselves. Must fail the gate."""
    run_dir = tmp_path / "run_specs"
    run_dir.mkdir()
    _quick_gate_setup(tmp_path, monkeypatch, run_specs_dir=run_dir)
    # Projects p10-p13 have NO slow duplicate — removing their only spec
    # genuinely removes 4 tier cells from what the run can load.
    for proj in QUICK_GATE_PROJECTS[10:14]:
        (run_dir / f"ns-{proj}.yaml").unlink(missing_ok=True)

    from click.testing import CliRunner
    from no_human.cli.commands import cli

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(run_dir), "--quick", "--gate"])
    assert result.exit_code == 1, result.output
    assert "gate FAILED" in result.output, result.output
    assert "11 of 15" in result.output, result.output


def test_full_tier_quick_card_passes_the_gate_but_is_REFUSED_for_publish():
    """THE invariant (review blocker, 2026-07-25): the tier denominator is
    runtime-only. A quick card covering 100% of its tier passes the gate as
    iteration signal, but the CARD records the full-corpus count, so
    publish_refusals — which reads only the card — still refuses it as a
    baseline. Writing the tier size onto the card let a fresh-clone quick run
    publish clean and poison every later full-run comparison."""
    from no_human.eval.northstar import BenchScore
    from no_human.eval.northstar_card import (
        NorthStarCard, northstar_gate, publish_refusals,
    )

    scores = [
        BenchScore(
            task_id=f"ns-{i}", title=f"t{i}", outcome_status="awaiting_approval",
            goal_satisfied=True, escalated_honestly=False, mergeable=None,
            nh_tokens=500, nh_cache_tokens=0, nh_cache_creation_tokens=0,
            nh_turns=3, nh_wall_clock_s=1.0, orig_tokens=1000,
            orig_cache_tokens=0, orig_cache_creation_tokens=0,
            orig_wall_clock_s=1.0, orig_corrections=0, subset="core")
        for i in range(15)
    ]
    card = NorthStarCard(scores=scores, created_at="2026-07-25T00:00:00Z",
                         corpus_available=54)

    gate = northstar_gate(card, None, tier_expected=15)
    assert gate.passed, f"full-tier quick run must pass the runtime gate: {gate.reasons}"

    refusals = publish_refusals(card, None)
    assert refusals, "a quick card must remain unpublishable as the baseline"
    assert any("54" in r for r in refusals), refusals

    # And WITHOUT the runtime override the same card fails the gate too —
    # the override is the only thing that admits a tier-sized run.
    assert not northstar_gate(card, None).passed
