"""`nh bench run --parallel N` — bounded concurrent spec execution.

The bench loop was strictly serial: 57 specs × minutes each = hours per run,
which throttles every measurement-driven iteration on the program. Each spec
already runs in its own sandbox clone + workdir, so bounded parallelism is a
pure wall-clock win — PROVIDED the semantics stay identical: per-completion
checkpointing (a mid-run death never wastes finished specs), one spec's crash
recorded honestly without killing the others, and the default (no flag) staying
exactly today's serial behavior so existing runs/baselines are undisturbed.
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
            id=f"ns-par{i}", title=f"spec {i}", request="r", subset="core",
            runnable=True,
            repo={"path": "/definitely/not/here", "pin": "", "branch": ""})
        (d / f"ns-par{i}.yaml").write_text(yaml.safe_dump(spec.to_dict()))


def _score_for(spec):
    from no_human.eval.northstar import BenchScore
    return BenchScore(
        task_id=spec.id, title=spec.title, outcome_status="skipped",
        goal_satisfied=None, escalated_honestly=False, mergeable=None,
        nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=0.0, orig_corrections=0, subset=spec.subset)


class _ConcurrencyProbe:
    """Stub runner that records how many run_one calls overlap in time."""
    active = 0
    max_active = 0
    calls = 0

    def __init__(self, *a, **kw): ...

    async def run_one(self, spec, *, workdir):
        cls = _ConcurrencyProbe
        cls.calls += 1
        cls.active += 1
        cls.max_active = max(cls.max_active, cls.active)
        await asyncio.sleep(0.05)
        cls.active -= 1
        return _score_for(spec)

    @classmethod
    def reset(cls):
        cls.active = cls.max_active = cls.calls = 0


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


def test_parallel_flag_overlaps_specs(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 6)
    _patch_env(monkeypatch, tmp_path)
    _ConcurrencyProbe.reset()
    monkeypatch.setattr(
        "no_human.eval.northstar.NorthStarRunner", _ConcurrencyProbe)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "3"])
    assert result.exit_code == 0, result.output
    assert _ConcurrencyProbe.calls == 6
    # With 6 specs, a 3-slot pool and a 50ms body, overlap is guaranteed.
    assert _ConcurrencyProbe.max_active >= 2, (
        f"specs never overlapped (max_active={_ConcurrencyProbe.max_active})")
    assert _ConcurrencyProbe.max_active <= 3, "pool bound exceeded"


def test_default_stays_strictly_serial(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 4)
    _patch_env(monkeypatch, tmp_path)
    _ConcurrencyProbe.reset()
    monkeypatch.setattr(
        "no_human.eval.northstar.NorthStarRunner", _ConcurrencyProbe)

    result = CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])
    assert result.exit_code == 0, result.output
    assert _ConcurrencyProbe.calls == 4
    assert _ConcurrencyProbe.max_active == 1, (
        "no --parallel flag must mean serial execution")


def test_one_crash_does_not_kill_the_parallel_run(tmp_path, monkeypatch):
    """A spec crashing mid-pool is recorded as crashed while every other spec
    still completes and lands in the results card — the serial loop's
    crash-isolation contract, preserved under parallelism."""
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 4)
    _patch_env(monkeypatch, tmp_path)

    class _OneCrash(_ConcurrencyProbe):
        async def run_one(self, spec, *, workdir):
            if spec.id == "ns-par1":
                raise RuntimeError("sdk died mid-spec")
            return await super().run_one(spec, workdir=workdir)

    _ConcurrencyProbe.reset()
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _OneCrash)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "2"])
    assert result.exit_code == 0, result.output

    results_dir = tmp_path / "results"
    files = [p for p in results_dir.glob("*.json")
             if not p.name.startswith("progress")]
    assert len(files) == 1, [p.name for p in results_dir.glob("*.json")]
    card = json.loads(files[0].read_text(encoding="utf-8"))
    by_id = {s["task_id"]: s for s in card["scores"]}
    assert len(by_id) == 4
    # Saved cards are deterministic regardless of completion order — the
    # tracked report renders scores in list order.
    ids = [s["task_id"] for s in card["scores"]]
    assert ids == sorted(ids)
    assert by_id["ns-par1"]["outcome_status"] == "crashed"
    assert "sdk died mid-spec" in by_id["ns-par1"]["notes"]
    for other in ("ns-par0", "ns-par2", "ns-par3"):
        assert by_id[other]["outcome_status"] == "skipped"


def test_checkpoint_written_per_completion_under_parallelism(tmp_path, monkeypatch):
    """The mid-run checkpoint must accumulate as specs finish (quota-death
    resumability), not only once at the end."""
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    _write_specs(d, 3)
    _patch_env(monkeypatch, tmp_path)

    seen_counts: list[int] = []

    class _Watcher(_ConcurrencyProbe):
        async def run_one(self, spec, *, workdir):
            score = await super().run_one(spec, workdir=workdir)
            ckpts = list((tmp_path / "results").glob("progress-*.json"))
            if ckpts:
                seen_counts.append(
                    len(json.loads(ckpts[0].read_text(encoding="utf-8"))["scores"]))
            return score

    _ConcurrencyProbe.reset()
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _Watcher)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--parallel", "2"])
    assert result.exit_code == 0, result.output
    # By the time the LAST spec ran, earlier completions were already
    # checkpointed — the file grows during the run.
    assert seen_counts and max(seen_counts) >= 1
