"""Quota saturation mid-run must halt-and-checkpoint, not finalize.

The 09-02 incident: a full-corpus run hit the subscription quota wall after
~64 measured specs, then recorded every remaining spec as `outcome_status=
"crashed"` with every token field 0, and *completed* — finalizing away its
own `progress.json` checkpoint. `--resume` had nothing to resume: the only
recovery was re-running the whole corpus, re-spending everything the walled
run already spent.

`QuotaHaltDetector` (`src/no_human/eval/quota_halt.py`) is the fix: three
consecutive ran-but-zero-priced-token specs stop the run, drop those rows
from the checkpoint, and leave it holding only what was actually scored —
so `--resume` re-runs exactly the unscored tail. Modelled verbatim on
`tests/test_bench_parallel.py`: a *local* copy of `_write_specs()` /
`_patch_env()` (not a cross-module import) plus a `_ScriptedRunner` that
returns a per-spec-id scripted score instead of doing real work. No LLM
call anywhere in this file.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml


def _write_specs(d: Path, n: int) -> None:
    from no_human.eval.bench_task import BenchTask
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        spec = BenchTask(
            id=f"ns-qh{i}", title=f"spec {i}", request="r", subset="core",
            runnable=True,
            repo={"path": "/definitely/not/here", "pin": "", "branch": ""})
        (d / f"ns-qh{i}.yaml").write_text(yaml.safe_dump(spec.to_dict()))


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


def _live(spec):
    from no_human.eval.northstar import BenchScore
    return BenchScore(
        task_id=spec.id, title=spec.title, outcome_status="done",
        goal_satisfied=True, escalated_honestly=False, mergeable=None,
        nh_tokens=5000, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=0,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=0.0, orig_corrections=0, subset=spec.subset)


def _dead(spec):
    """A quota-shaped crash: zero tokens AND the SDK's own transport-death
    wording (the 09-02 incident's actual signature — see
    `agent/claude_backend.py`'s `_TRANSPORT_FAILURE_MARKERS`)."""
    from no_human.eval.northstar import BenchScore
    return BenchScore(
        task_id=spec.id, title=spec.title, outcome_status="crashed",
        goal_satisfied=False, escalated_honestly=False, mergeable=None,
        nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=0.0, orig_corrections=0, subset=spec.subset,
        notes="runner crashed: Stream closed by consumer")


def _broken(spec):
    """A genuine, UNRELATED zero-token crash (a bad repo pin, a corrupted
    clone, a permissions failure) — same shape as `_dead()` (crashed, all
    tokens 0) but its note carries none of the quota wall's own wording.
    Must never be mistaken for `_dead()` by the halt detector."""
    from no_human.eval.northstar import BenchScore
    return BenchScore(
        task_id=spec.id, title=spec.title, outcome_status="crashed",
        goal_satisfied=False, escalated_honestly=False, mergeable=None,
        nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=0.0, orig_corrections=0, subset=spec.subset,
        notes="runner crashed: [Errno 2] No such file or directory: "
              "'/definitely/not/here'")


def _skip(spec):
    from no_human.eval.northstar import BenchScore
    return BenchScore(
        task_id=spec.id, title=spec.title, outcome_status="skipped",
        goal_satisfied=None, escalated_honestly=False, mergeable=None,
        nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=0.0, orig_corrections=0, subset=spec.subset)


class _ScriptedRunner:
    """Stub runner returning a per-spec-id scripted score, optionally after a
    fixed delay. The delay is what lets a --parallel test control which
    specs are still in-flight at the moment the halt trips: nothing in this
    loop yields to the event loop unless something actually sleeps, so two
    specs only race each other if they are told to."""

    calls: list[str] = []
    script: dict = {}   # spec.id -> (factory, delay_seconds)

    def __init__(self, *a, **kw): ...

    async def run_one(self, spec, *, workdir):
        cls = _ScriptedRunner
        cls.calls.append(spec.id)
        factory, delay = cls.script.get(spec.id, (_live, 0.0))
        if delay:
            await asyncio.sleep(delay)
        return factory(spec)

    @classmethod
    def reset(cls, script=None):
        cls.calls = []
        cls.script = dict(script or {})


def _uniform_script(n, factory, delay=0.0):
    return {f"ns-qh{i}": (factory, delay) for i in range(n)}


def _results_and_progress(results_dir: Path):
    files = list(results_dir.glob("*.json"))
    progress = [p for p in files if p.name.startswith("progress")]
    results = [p for p in files if not p.name.startswith("progress")]
    return results, progress


def test_three_consecutive_zero_token_specs_halt_the_run(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 10)
    _patch_env(monkeypatch, tmp_path)

    script = {}
    for i in range(10):
        script[f"ns-qh{i}"] = (_live, 0.0) if i < 4 else (_dead, 0.0)
    _ScriptedRunner.reset(script)
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _ScriptedRunner)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "1"])

    assert result.exit_code != 0, result.output
    assert len(_ScriptedRunner.calls) == 7, _ScriptedRunner.calls
    assert "--resume" in result.output

    results_dir = tmp_path / "results"
    results, progress = _results_and_progress(results_dir)
    assert len(progress) == 1, [p.name for p in results_dir.glob("*.json")]
    ckpt = json.loads(progress[0].read_text(encoding="utf-8"))
    assert {s["task_id"] for s in ckpt["scores"]} == {
        f"ns-qh{i}" for i in range(4)}

    assert len(results) == 1, [p.name for p in results_dir.glob("*.json")]
    card = json.loads(results[0].read_text(encoding="utf-8"))
    assert card["halted_reason"] == "quota_saturation"
    assert {s["task_id"] for s in card["scores"]} == {
        f"ns-qh{i}" for i in range(4)}


def test_in_flight_specs_finish_recording_under_parallel(tmp_path, monkeypatch):
    """Same script, --parallel 3: a live and a dead spec both already
    in-flight when the 3rd consecutive dead spec trips the halt still
    finish recording — the live one kept, the dead one dropped."""
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 10)
    _patch_env(monkeypatch, tmp_path)

    script = {
        "ns-qh0": (_live, 0.0), "ns-qh1": (_live, 0.0), "ns-qh2": (_live, 0.0),
        "ns-qh3": (_dead, 0.0), "ns-qh4": (_dead, 0.0),
        "ns-qh5": (_dead, 0.05),   # trips the halt on completion
        "ns-qh6": (_live, 0.10),  # in-flight when it trips; kept
        "ns-qh7": (_dead, 0.08),  # in-flight when it trips; dropped
        "ns-qh8": (_dead, 0.0),
        "ns-qh9": (_dead, 0.0),
    }
    _ScriptedRunner.reset(script)
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _ScriptedRunner)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "3"])

    assert result.exit_code != 0, result.output
    assert len(_ScriptedRunner.calls) < 10, _ScriptedRunner.calls
    assert "ns-qh8" not in _ScriptedRunner.calls
    assert "ns-qh9" not in _ScriptedRunner.calls
    # both raced specs actually ran (were in-flight, not skipped outright)
    assert "ns-qh6" in _ScriptedRunner.calls
    assert "ns-qh7" in _ScriptedRunner.calls

    results_dir = tmp_path / "results"
    results, progress = _results_and_progress(results_dir)
    assert len(progress) == 1
    ckpt_ids = {s["task_id"] for s in json.loads(progress[0].read_text(encoding="utf-8"))["scores"]}
    assert "ns-qh7" not in ckpt_ids, "post-halt dead in-flight row must be dropped"
    assert "ns-qh6" in ckpt_ids, "post-halt live in-flight row must be kept"
    assert ckpt_ids == {"ns-qh0", "ns-qh1", "ns-qh2", "ns-qh6"}


def test_a_single_isolated_zero_token_spec_does_not_halt(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 6)
    _patch_env(monkeypatch, tmp_path)

    script = _uniform_script(6, _live)
    script["ns-qh2"] = (_dead, 0.0)   # the one isolated death
    _ScriptedRunner.reset(script)
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _ScriptedRunner)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "1"])
    assert result.exit_code == 0, result.output
    assert len(_ScriptedRunner.calls) == 6

    results_dir = tmp_path / "results"
    results, progress = _results_and_progress(results_dir)
    assert not progress, "clean completion must still unlink the checkpoint"
    assert len(results) == 1
    card = json.loads(results[0].read_text(encoding="utf-8"))
    assert card["halted_reason"] == ""
    ids = {s["task_id"] for s in card["scores"]}
    assert ids == {f"ns-qh{i}" for i in range(6)}
    assert any(s["task_id"] == "ns-qh2" and s["outcome_status"] == "crashed"
               for s in card["scores"]), "the isolated dead row must stay"


def test_three_consecutive_non_quota_crashes_do_not_halt(tmp_path, monkeypatch):
    """The review-blocking finding: a corpus broken for an UNRELATED reason
    (bad repo pin, corrupted clone — `_broken()`'s shape) must not be
    mislabeled `quota_saturation`. Left mislabeled, `--resume` would just
    re-run the same broken specs into the same halt forever. Here even 10
    non-quota crashes in a row must run to completion, unhalted."""
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 10)
    _patch_env(monkeypatch, tmp_path)

    _ScriptedRunner.reset(_uniform_script(10, _broken))
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _ScriptedRunner)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "1"])
    assert result.exit_code == 0, result.output
    assert len(_ScriptedRunner.calls) == 10, _ScriptedRunner.calls

    results_dir = tmp_path / "results"
    results, progress = _results_and_progress(results_dir)
    assert not progress, "an unhalted run must still unlink the checkpoint"
    assert len(results) == 1
    card = json.loads(results[0].read_text(encoding="utf-8"))
    assert card["halted_reason"] == ""
    ids = {s["task_id"] for s in card["scores"]}
    assert ids == {f"ns-qh{i}" for i in range(10)}, (
        "every broken row must still be kept in the card, exactly like an "
        "isolated crash is today")
    assert all(s["outcome_status"] == "crashed" for s in card["scores"])


def test_a_corpus_of_skips_never_halts(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 5)
    _patch_env(monkeypatch, tmp_path)

    _ScriptedRunner.reset(_uniform_script(5, _skip))
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _ScriptedRunner)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "1"])
    assert result.exit_code == 0, result.output
    assert len(_ScriptedRunner.calls) == 5

    results_dir = tmp_path / "results"
    results, progress = _results_and_progress(results_dir)
    assert not progress
    card = json.loads(results[0].read_text(encoding="utf-8"))
    assert card["halted_reason"] == ""
    assert all(s["outcome_status"] == "skipped" for s in card["scores"])


def test_resume_after_a_quota_halt_reruns_and_merges(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 10)
    _patch_env(monkeypatch, tmp_path)

    script = {}
    for i in range(10):
        script[f"ns-qh{i}"] = (_live, 0.0) if i < 4 else (_dead, 0.0)
    _ScriptedRunner.reset(script)
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _ScriptedRunner)

    runner = CliRunner()
    result1 = runner.invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "1",
              "--label", "wall"])
    assert result1.exit_code != 0, result1.output
    assert len(_ScriptedRunner.calls) == 7

    results_dir = tmp_path / "results"
    results_before, _ = _results_and_progress(results_dir)
    assert len(results_before) == 1

    # Swap in an all-live runner and resume: only the unscored tail
    # (specs 5-10, 0-indexed 4-9) should re-run.
    _ScriptedRunner.reset(_uniform_script(10, _live))
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _ScriptedRunner)

    result2 = runner.invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "1",
              "--label", "wall", "--resume"])
    assert result2.exit_code == 0, result2.output
    assert set(_ScriptedRunner.calls) == {f"ns-qh{i}" for i in range(4, 10)}
    for i in range(4):
        assert f"ns-qh{i}" not in _ScriptedRunner.calls

    results_after, progress_after = _results_and_progress(results_dir)
    assert not progress_after, "clean completion must unlink the checkpoint"
    assert len(results_after) == 2, [p.name for p in results_after]

    newest = max(results_after, key=lambda p: p.stat().st_mtime)
    card = json.loads(newest.read_text(encoding="utf-8"))
    assert card["label"] == "wall"
    assert card["halted_reason"] == ""
    ids = [s["task_id"] for s in card["scores"]]
    assert len(ids) == 10
    assert len(set(ids)) == 10
    assert set(ids) == {f"ns-qh{i}" for i in range(10)}


def test_a_halted_partial_is_still_not_a_baseline(tmp_path, monkeypatch):
    """The gate is unchanged: a halted partial is refused by the EXISTING
    coverage rules (dead fraction / unmeasured fraction / corpus shortfall),
    never by a new halt-specific rule."""
    from no_human.eval.northstar import BenchScore
    from no_human.eval.northstar_card import (
        MAX_DEAD_FRACTION, MAX_UNMEASURED_FRACTION, NorthStarCard,
        publish_refusals)

    # Pin the thresholds this AC promises are untouched.
    assert MAX_DEAD_FRACTION == 0.2
    assert MAX_UNMEASURED_FRACTION == 0.2

    scores = [
        BenchScore(
            task_id=f"ns-qh{i}", title=f"spec {i}", outcome_status="done",
            goal_satisfied=True, escalated_honestly=False, mergeable=None,
            nh_tokens=5000, nh_cache_tokens=0, nh_cache_creation_tokens=0,
            nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=0,
            orig_cache_tokens=0, orig_cache_creation_tokens=0,
            orig_wall_clock_s=0.0, orig_corrections=0, subset="core")
        for i in range(4)
    ]
    card = NorthStarCard(scores=scores, created_at="now", label="wall",
                         corpus_available=10, trials=1,
                         halted_reason="quota_saturation")

    refusals = publish_refusals(card)
    assert refusals, "a 4/10 partial must be refused as a baseline"
    assert not any("halted" in r.lower() for r in refusals), (
        "the refusal must come from the existing coverage rules, not a new "
        f"halt-specific one: {refusals}")


# --------------------------- detector unit tests --------------------------- #

def _bs(task_id, trial=0, *, status="done", nh_tokens=0, nh_cache_tokens=0,
        nh_cache_creation_tokens=0, notes=""):
    from no_human.eval.northstar import BenchScore
    return BenchScore(
        task_id=task_id, title=task_id, outcome_status=status,
        goal_satisfied=status == "done", escalated_honestly=False,
        mergeable=None, nh_tokens=nh_tokens, nh_cache_tokens=nh_cache_tokens,
        nh_cache_creation_tokens=nh_cache_creation_tokens, nh_turns=0,
        nh_wall_clock_s=0.0, orig_tokens=0, orig_cache_tokens=0,
        orig_cache_creation_tokens=0, orig_wall_clock_s=0.0,
        orig_corrections=0, subset="core", trial=trial, notes=notes)


# A quota-shaped crash note, for unit tests exercising streak/threshold
# mechanics (not the note-matching itself — see
# `test_detector_ignores_non_quota_shaped_crashes` for that).
_QUOTA_NOTE = "runner crashed: Stream closed by consumer"


def test_detector_ignores_skipped_rows():
    from no_human.eval.quota_halt import QuotaHaltDetector
    det = QuotaHaltDetector()
    for i in range(5):
        halted = det.observe(_bs(f"s{i}", status="skipped", nh_tokens=0))
        assert not halted
    assert not det.stopped
    assert det.streak == []


def test_detector_counts_a_cache_only_row_as_work():
    from no_human.eval.quota_halt import QuotaHaltDetector
    det = QuotaHaltDetector()
    det.observe(_bs("d0", status="crashed", nh_tokens=0, notes=_QUOTA_NOTE))
    det.observe(_bs("d1", status="crashed", nh_tokens=0, notes=_QUOTA_NOTE))
    # cache-read spend only, no raw nh_tokens: real work, not a death.
    halted = det.observe(_bs("cache", status="done", nh_tokens=0,
                              nh_cache_tokens=1000))
    assert not halted
    assert not det.stopped
    assert det.streak == []


def test_detector_streak_resets_on_a_token_bearing_row():
    from no_human.eval.quota_halt import QuotaHaltDetector
    det = QuotaHaltDetector()
    det.observe(_bs("d0", status="crashed", nh_tokens=0, notes=_QUOTA_NOTE))
    det.observe(_bs("d1", status="crashed", nh_tokens=0, notes=_QUOTA_NOTE))
    assert len(det.streak) == 2
    det.observe(_bs("live", status="done", nh_tokens=100))
    assert det.streak == []
    assert not det.stopped


def test_scored_is_identity_before_the_halt():
    from no_human.eval.quota_halt import QuotaHaltDetector
    det = QuotaHaltDetector()
    scores = [_bs("d0", status="crashed", nh_tokens=0, notes=_QUOTA_NOTE),
              _bs("d1", status="crashed", nh_tokens=0, notes=_QUOTA_NOTE)]
    for s in scores:
        det.observe(s)
    assert det.scored(scores) == scores
    assert det.scored(scores) is not scores  # a fresh list, not the same object


def test_detector_freezes_after_tripping():
    from no_human.eval.quota_halt import QuotaHaltDetector
    det = QuotaHaltDetector()
    for i in range(3):
        det.observe(_bs(f"d{i}", status="crashed", nh_tokens=0,
                         notes=_QUOTA_NOTE))
    assert det.stopped
    dropped_before = set(det.dropped)
    # A live row after the trip must not resurrect the dropped rows.
    det.observe(_bs("live-after", status="done", nh_tokens=100))
    assert det.dropped == dropped_before
    assert det.stopped


def test_detector_ignores_non_quota_shaped_crashes():
    """The review-blocking finding, at the unit level: a zero-token crash
    whose note carries none of the quota wall's own wording (a setup/sandbox
    crash, not a transport death) must never extend the streak, however many
    land in a row — or a genuinely broken (non-quota) corpus would trip the
    same halt and, on --resume, crash into it again forever."""
    from no_human.eval.quota_halt import QuotaHaltDetector
    det = QuotaHaltDetector()
    for i in range(5):
        halted = det.observe(_bs(
            f"b{i}", status="crashed", nh_tokens=0,
            notes="runner crashed: [Errno 13] Permission denied"))
        assert not halted
    assert not det.stopped
    assert det.streak == []
    assert det.dropped == set()


def test_streak_constant_is_three():
    from no_human.eval.quota_halt import QUOTA_HALT_CONSECUTIVE_DEAD
    assert QUOTA_HALT_CONSECUTIVE_DEAD == 3


def test_halted_reason_survives_save_and_load(tmp_path):
    from no_human.eval.northstar_card import NorthStarCard
    card = NorthStarCard(scores=[], created_at="now", label="wall",
                         halted_reason="quota_saturation")
    p = tmp_path / "card.json"
    card.save(p)
    loaded = NorthStarCard.load(p)
    assert loaded.halted_reason == "quota_saturation"

    # A legacy file predating the field loads as "".
    raw = json.loads(p.read_text(encoding="utf-8"))
    del raw["halted_reason"]
    p.write_text(json.dumps(raw))
    legacy = NorthStarCard.load(p)
    assert legacy.halted_reason == ""
