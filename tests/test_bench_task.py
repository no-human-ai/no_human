"""Tests for BenchTask specs + the no-leak builder (north-star A2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from no_human.eval.bench_task import (
    BenchTask,
    build_bench_tasks,
    find_leaks,
    load_bench_tasks,
)
from no_human.history.extractor import Message, Transcript


def _transcript(cascade_id="cc:abc", request="Fix the login bug in auth.py",
                *, cwd="", branch="main", source="cc:personal",
                extra_user=("also update the docs",)) -> Transcript:
    msgs = [Message(role="user", content=request, step_type="user"),
            Message(role="assistant", content="Done: I changed auth.py line 42 "
                    "to use the session token instead of the cookie value",
                    step_type="assistant")]
    for m in extra_user:
        msgs.append(Message(role="user", content=m, step_type="user"))
    return Transcript(
        cascade_id=cascade_id, title=request[:80], created="2026-07-01",
        messages=msgs, usage={"input_tokens": 100, "output_tokens": 50,
                              "cache_read_input_tokens": 500,
                              "cache_creation_input_tokens": 10},
        started="2026-07-01T10:00:00.000Z", ended="2026-07-01T10:30:00.000Z",
        cwd=cwd, git_branch=branch, source=source, corrections=len(extra_user),
    )


import os


def _commit(repo: Path, msg: str, *, when: str) -> None:
    """Commit with BOTH author and committer dates pinned (rev-list --before
    reads the committer date)."""
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True,
                   capture_output=True, env=env)


def _git_repo(tmp_path: Path) -> Path:
    """A repo whose base commit PREDATES the fixture session (2026-07-01) so
    `rev-list --before=<session start>` has something to resolve."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"],
                 ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    _commit(repo, "base", when="2026-06-01T00:00:00Z")
    return repo


# ------------------------------- schema ----------------------------------- #

def test_yaml_round_trip(tmp_path):
    t = _transcript(cwd=str(_git_repo(tmp_path)))
    (path,) = build_bench_tasks([t], out_dir=tmp_path / "specs")
    loaded = load_bench_tasks(tmp_path / "specs")
    assert len(loaded) == 1
    spec = loaded[0]
    assert spec.request == "Fix the login bug in auth.py"
    assert spec.original["corrections"] == 1
    assert spec.original["wall_clock_s"] == 1800.0
    assert spec.original["tokens"]["cache_read_input_tokens"] == 500
    assert spec.runnable is True
    assert spec.repo["pin"]  # resolved to a real sha or HEAD
    assert path == spec.path


def test_subset_filter(tmp_path):
    d = tmp_path / "specs"
    d.mkdir()
    for i, subset in enumerate(["core", "full"]):
        spec = BenchTask(id=f"ns-{i}", title="t", request="r", subset=subset)
        (d / f"ns-{i}.yaml").write_text(yaml.safe_dump(spec.to_dict()))
    assert [t.id for t in load_bench_tasks(d, subset="core")] == ["ns-0"]
    assert len(load_bench_tasks(d)) == 2


# ------------------------------ no-cheating -------------------------------- #

def test_only_first_user_message_reaches_the_spec(tmp_path):
    """Structural no-leak: corrections and assistant text must never appear
    anywhere in the written YAML."""
    t = _transcript(cwd=str(_git_repo(tmp_path)),
                    extra_user=("the fix is to use the session token",))
    (path,) = build_bench_tasks([t], out_dir=tmp_path / "specs")
    raw = path.read_text(encoding="utf-8")
    assert "Fix the login bug" in raw
    assert "session token" not in raw          # correction content
    assert "auth.py line 42" not in raw        # assistant content


def test_find_leaks_flags_curated_solution_text():
    spec = BenchTask(
        id="ns-1", title="t", request="Fix the login bug",
        acceptance_criteria=["I changed auth.py line 42 to use the session "
                             "token instead of the cookie value"])
    assistant = ("Done: I changed auth.py line 42 to use the session token "
                 "instead of the cookie value")
    assert find_leaks(spec, assistant) == ["acceptance_criteria"]
    # Independent criteria are clean.
    clean = BenchTask(id="ns-2", title="t", request="r",
                      acceptance_criteria=["login succeeds with a valid session"])
    assert find_leaks(clean, assistant) == []


def test_default_build_dir_is_gitignored():
    """The raw corpus is verbatim operator conversation content — the
    builder's DEFAULT target must be the gitignored generated/ dir, and the
    repo's .gitignore must actually cover it."""
    from no_human.eval.bench_task import GENERATED_DIR, NORTHSTAR_DIR

    assert GENERATED_DIR.parent == NORTHSTAR_DIR
    assert GENERATED_DIR.name == "generated"
    repo_root = NORTHSTAR_DIR.parents[1]
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert "eval/northstar_tasks/generated/" in gitignore

    import inspect
    from no_human.eval.bench_task import build_bench_tasks
    sig = inspect.signature(build_bench_tasks)
    assert sig.parameters["out_dir"].default == GENERATED_DIR


# ------------------------------- dedupe ------------------------------------ #

def test_title_derives_from_request_never_transcript_title(tmp_path):
    """Review finding: transcript titles are auto-summaries of the WHOLE
    conversation and can encode the solution — an unaudited leak channel into
    the coder. The spec title must be user-authored (from the request)."""
    t = _transcript(cwd=str(_git_repo(tmp_path)))
    t.title = "Refactor auth to use session tokens instead of cookies"
    build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.title.startswith("Fix the login bug")
    assert "session tokens" not in spec.title


def test_resumed_sessions_dedupe_by_first_request(tmp_path):
    repo = str(_git_repo(tmp_path))
    a = _transcript(cascade_id="cc:a", cwd=repo)
    b = _transcript(cascade_id="cc:b", cwd=repo)   # same first request
    c = _transcript(cascade_id="cc:c", request="Different task", cwd=repo)
    written = build_bench_tasks([a, b, c], out_dir=tmp_path / "specs")
    assert len(written) == 2


# ---------------------------- non-runnable --------------------------------- #

def test_windsurf_workspaces_resolve_repo_path(tmp_path):
    """Windsurf transcripts carry `workspaces` (file:// URIs), not cwd —  # term-ok: real IDE names
    without the fallback the ENTIRE original 89-conversation corpus builds as
    non-runnable (found live: all Windsurf specs skipped 'repo missing')."""  # term-ok: real IDE name
    repo = _git_repo(tmp_path)
    t = _transcript(cwd="", branch="")
    t.workspaces = [f"file://{repo}/"]
    t.source = "windsurf"  # term-ok: internal source tag names the real IDE
    build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.runnable is True
    assert spec.repo["path"] == str(repo)


def test_missing_repo_marked_not_runnable(tmp_path):
    t = _transcript(cwd="/nope/definitely/missing")
    (path,) = build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.runnable is False
    assert "repo missing" in spec.skip_reason


def test_non_git_cwd_marked_not_runnable(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    t = _transcript(cwd=str(plain))
    build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.runnable is False
    assert "not a git repo" in spec.skip_reason


def test_cli_bench_build_writes_specs(tmp_path, monkeypatch):
    """`nh bench build` is offline: parses history files, writes YAMLs, no LLM."""
    from click.testing import CliRunner
    from no_human.cli.commands import cli
    from no_human.history import extractor

    def _no_ide(**_kw):
        raise extractor.IDENotRunningError("no IDE")
    monkeypatch.setattr("no_human.history.extractor.extract_transcripts", _no_ide)

    t = _transcript(cwd=str(_git_repo(tmp_path)))
    monkeypatch.setattr(
        "no_human.history.claude_code.extract_claude_code_transcripts",
        lambda **kw: [t])

    out = tmp_path / "specs"
    result = CliRunner().invoke(
        cli, ["bench", "build", "--days", "5", "--out", str(out)])

    assert result.exit_code == 0, result.output
    assert len(list(out.glob("ns-*.yaml"))) == 1



def _results_file(res_dir):
    """`bench run` records <label>-<stamp>.json and publishes nothing; the
    baseline is written only by `nh bench publish`."""
    files = sorted((f for f in res_dir.glob("*.json")
                    if not f.name.startswith("progress")
                    and f.name != "latest.json"),
                   key=lambda f: f.stat().st_mtime)
    assert files, "the run recorded no results file"
    import json as _j
    return _j.loads(files[-1].read_text(encoding="utf-8"))

def test_cli_bench_run_wiring_end_to_end(tmp_path, monkeypatch):
    """Exercise bench run PAST the no-specs exit (the live baseline launch
    crashed on a NameError this path would have caught): specs present,
    _bootstrap stubbed, runner stubbed to skip — the command must reach the
    card/report stage and exit 0."""
    import yaml as _yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    spec = BenchTask(id="ns-wire1", title="t", request="r", subset="core",
                     runnable=False, skip_reason="wiring test")
    (d / "ns-wire1.yaml").write_text(_yaml.safe_dump(spec.to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = "m"
        review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]

    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))

    class _StubRunner:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            return BenchScore(
                task_id=spec.id, title=spec.title, outcome_status="skipped",
                goal_satisfied=None, escalated_honestly=False, mergeable=None,
                nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
                orig_cache_tokens=0, orig_cache_creation_tokens=0,
                orig_wall_clock_s=0.0, orig_corrections=0,
                subset=spec.subset, notes="stub")
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _StubRunner)
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR",
                        tmp_path / "results")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NORTH_STAR_BENCH.md")

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d)])
    assert result.exit_code == 0, result.output
    assert "success" in result.output


def test_cli_bench_run_gate_exits_nonzero_on_an_unmeasured_corpus(
        tmp_path, monkeypatch):
    """The predicate being right is worthless if the command ignores it.

    Every spec here skips, so the corpus is 100% unmeasured. There is no
    baseline (RESULTS_DIR is a fresh tmp dir — the real one is gitignored, so
    this is the DEFAULT state of a clone), which is precisely the case that
    used to sail through on `previous is None`. With --gate the command must
    exit non-zero.
    """
    import yaml as _yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    # 11 that RUN + 5 that skip: `ran` clears the first-run floor, so COVERAGE
    # (5/16 = 31%) is the sole reason. The earlier fixture skipped all 12, so
    # ran==0 and the floor fired instead — deleting the coverage check left the
    # test green, i.e. it did not pin what its name claims.
    for i in range(11):
        spec = BenchTask(id=f"ns-ok{i}", title="t", request="r",
                         subset="core", runnable=True)
        (d / f"ns-ok{i}.yaml").write_text(_yaml.safe_dump(spec.to_dict()))
    for i in range(5):
        spec = BenchTask(id=f"ns-skip{i}", title="t", request="r",
                         subset="core", runnable=True)
        (d / f"ns-skip{i}.yaml").write_text(_yaml.safe_dump(spec.to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = "m"
        review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]

    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))

    class _SkippingRunner:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            skipped = spec.id.startswith("ns-skip")
            return BenchScore(
                task_id=spec.id, title=spec.title,
                outcome_status="skipped" if skipped else "awaiting_approval",
                goal_satisfied=None if skipped else True,
                escalated_honestly=False, mergeable=None,
                nh_tokens=0 if skipped else 500,
                nh_cache_tokens=0, nh_cache_creation_tokens=0,
                nh_turns=0 if skipped else 3, nh_wall_clock_s=0.0,
                orig_tokens=0 if skipped else 1000,
                orig_cache_tokens=0, orig_cache_creation_tokens=0,
                orig_wall_clock_s=0.0, orig_corrections=0,
                subset=spec.subset, notes="repo gone" if skipped else "")

    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _SkippingRunner)
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR",
                        tmp_path / "results")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NORTH_STAR_BENCH.md")
    # Point the CANONICAL dir at this same spec set. Without it
    # `corpus_available` is read from the real 55-spec corpus, the shortfall
    # rule fires too, and this test passes with the gate's coverage rule
    # DELETED — satisfied by the publish-refusal section printed above the gate
    # output. That is exactly the trap the comment below claims to avoid.
    monkeypatch.setattr("no_human.eval.bench_task.NORTHSTAR_DIR", d)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--gate"])
    assert result.exit_code == 1, result.output
    assert "gate FAILED" in result.output
    # The gate's OWN reason, in the gate's own section — not merely the exit
    # code, and not a string the publish refusals happen to print too.
    gate_section = result.output.split("gate FAILED", 1)[1]
    assert "went unmeasured" in gate_section, result.output
    assert "available spec(s)" not in gate_section, (
        "shortfall fired too — coverage is no longer the sole reason")


def test_cli_bench_run_survives_a_crashing_task(tmp_path, monkeypatch):
    """A single task's hard crash (SDK CLI dying on quota saturation killed a
    LIVE baseline at 3/10 and lost every partial result) must be recorded as
    crashed and the run must continue to the next spec."""
    import yaml as _yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    for i in range(2):
        spec = BenchTask(id=f"ns-crash{i}", title="t", request="r",
                         subset="core", runnable=True)
        (d / f"ns-crash{i}.yaml").write_text(_yaml.safe_dump(spec.to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = "m"
        review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]
    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))

    calls = []

    class _CrashThenSkip:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            calls.append(spec.id)
            if len(calls) == 1:
                raise RuntimeError("Stream closed")
            return BenchScore(
                task_id=spec.id, title=spec.title, outcome_status="skipped",
                goal_satisfied=None, escalated_honestly=False, mergeable=None,
                nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0,
                orig_cache_tokens=0, orig_cache_creation_tokens=0,
                orig_wall_clock_s=0.0, orig_corrections=0,
                subset=spec.subset, notes="stub")
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _CrashThenSkip)
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", tmp_path / "res")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NS.md")

    result = CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2, "the run must continue past the crash"
    assert "crashed" in result.output
    import json as _json
    saved = _results_file(tmp_path / "res")
    crashed = [x for x in saved["scores"] if x["outcome_status"] == "crashed"]
    assert len(crashed) == 1 and crashed[0]["goal_satisfied"] is False


def test_cli_bench_run_exits_1_without_specs(tmp_path):
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no specs found" in result.output


def test_pin_resolves_to_commit_before_session(tmp_path):
    repo = _git_repo(tmp_path)
    first_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                               capture_output=True, text=True).stdout.strip()
    # A later commit that postdates the session must NOT be the pin.
    (repo / "later.txt").write_text("y")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    _commit(repo, "later", when="2026-07-05T00:00:00Z")
    t = _transcript(cwd=str(repo))
    build_bench_tasks([t], out_dir=tmp_path / "specs")
    spec = load_bench_tasks(tmp_path / "specs")[0]
    assert spec.repo["pin"] == first_sha


def test_cli_bench_run_checkpoints_and_resumes(tmp_path, monkeypatch):
    """A mid-run death (quota 'Stream closed' killed the expanded run at 3/14)
    must not waste completed specs: each spec checkpoints, and --resume skips
    the already-scored ones."""
    import json as _json
    import yaml as _yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    for i in range(3):
        spec = BenchTask(id=f"ns-r{i}", title="t", request="r", subset="core",
                         runnable=True)
        (d / f"ns-r{i}.yaml").write_text(_yaml.safe_dump(spec.to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]
    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))
    res_dir = tmp_path / "res"
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", res_dir)
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD", tmp_path / "N.md")

    seen = []

    def _score(spec):
        return BenchScore(
            task_id=spec.id, title=spec.title, outcome_status="skipped",
            goal_satisfied=None, escalated_honestly=False, mergeable=None,
            nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
            nh_turns=0, nh_wall_clock_s=0.0, orig_tokens=0, orig_cache_tokens=0,
            orig_cache_creation_tokens=0, orig_wall_clock_s=0.0,
            orig_corrections=0, subset=spec.subset, notes="stub")

    class _DieOnThird:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            seen.append(spec.id)
            if len(seen) == 3:
                # A hook-thread error escapes `except Exception` and kills the
                # process — the exact case the checkpoint protects (the caught
                # kind completes and cleans up).
                raise KeyboardInterrupt("Stream closed (process killed)")
            return _score(spec)
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _DieOnThird)

    # First run: 2 succeed, the 3rd hard-kills the process — the checkpoint
    # must hold the 2 completed specs.
    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])
    ckpts = list(res_dir.glob("progress-*.json"))
    assert len(ckpts) == 1, f"expected one checkpoint, got {ckpts}"
    ckpt = _json.loads(ckpts[0].read_text(encoding="utf-8"))
    assert len({s["task_id"] for s in ckpt["scores"]}) >= 2

    # Resume: the 2 already-scored specs are skipped.
    seen.clear()

    class _AllSkip:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            seen.append(spec.id)
            return _score(spec)
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _AllSkip)
    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d), "--resume"])
    assert "ns-r0" not in seen and "ns-r1" not in seen, "resume re-ran done specs"

    # The checkpointed specs must survive INTO the final card, not just be
    # skipped-and-dropped — latest.json holds all 3, and the checkpoint is gone.
    final = _results_file(res_dir)
    assert {s["task_id"] for s in final["scores"]} == {"ns-r0", "ns-r1", "ns-r2"}
    assert not list(res_dir.glob("progress-*.json")), "clean run left a checkpoint"

    # A checkpoint from a DIFFERENT run must not bleed foreign specs into this
    # run's results. Note the MECHANISM, because an earlier version of this
    # comment named the wrong one: the file is DECLINED at the ownership check
    # (label "stale" != this run's label), so the resume filter downstream is
    # never reached. The filter remains as depth, not as what makes this pass.
    (res_dir / "progress.json").write_text(_json.dumps({
        "created_at": "x", "label": "stale",
        "scores": [_score(BenchTask(id="ns-foreign", title="t", request="r",
                                    subset="core", runnable=True)).as_dict()],
    }))
    seen.clear()
    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d), "--resume"])
    final2 = _results_file(res_dir)
    assert "ns-foreign" not in {s["task_id"] for s in final2["scores"]}, \
        "resume leaked a foreign spec into the baseline"


def test_a_probe_does_not_delete_another_runs_legacy_checkpoint(tmp_path, monkeypatch):
    """THE original incident, on the path the legacy fallback exists to serve.

    A `--resume` run whose own keyed checkpoint does not exist used to adopt
    `progress.json` whatever it held, filter every spec out as foreign — while
    PRINTING that it was doing so — and then unlink it on "clean completion".
    So a one-spec probe still erased a 56-spec banked checkpoint. The code can
    already tell the file is not its own; it must decline it, not inherit and
    delete it.
    """
    import json as _json
    import yaml as _yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    (d / "ns-probe.yaml").write_text(_yaml.safe_dump(
        BenchTask(id="ns-probe", title="t", request="r", subset="core",
                  runnable=True).to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]
    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))
    res_dir = tmp_path / "res"
    res_dir.mkdir()
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", res_dir)
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD", tmp_path / "N.md")

    def _sc(task_id):
        return BenchScore(
            task_id=task_id, title="t", outcome_status="done",
            goal_satisfied=True, escalated_honestly=False, mergeable=None,
            nh_tokens=1000, nh_cache_tokens=0, nh_cache_creation_tokens=0,
            nh_turns=1, nh_wall_clock_s=0.0, orig_tokens=0, orig_cache_tokens=0,
            orig_cache_creation_tokens=0, orig_wall_clock_s=0.0,
            orig_corrections=0,
        ).as_dict()

    # A long run's banked checkpoint, at the legacy path, from a DIFFERENT set.
    banked = res_dir / "progress.json"
    banked.write_text(_json.dumps({
        "created_at": "x", "label": "expanded-core-v15",
        "scores": [_sc(f"ns-long-{i}") for i in range(56)],
    }))

    class _Runner:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            return BenchScore(
                task_id=spec.id, title=spec.title, outcome_status="done",
                goal_satisfied=True, escalated_honestly=False, mergeable=None,
                nh_tokens=5, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                nh_turns=1, nh_wall_clock_s=0.0, orig_tokens=0,
                orig_cache_tokens=0, orig_cache_creation_tokens=0,
                orig_wall_clock_s=0.0, orig_corrections=0,
            )
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _Runner)

    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d), "--resume"])

    assert banked.exists(), "the probe deleted another run's banked checkpoint"
    survived = _json.loads(banked.read_text(encoding="utf-8"))
    assert len(survived["scores"]) == 56, "the banked checkpoint was rewritten"
    assert survived["label"] == "expanded-core-v15"


def _bench_env(tmp_path, monkeypatch, spec_ids):
    """A `bench run` harness: specs on disk, results dir redirected, a runner
    that scores every spec cheaply."""
    import yaml as _yaml
    from no_human.cli import commands as cmds
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / f"specs-{abs(hash(tuple(spec_ids)))}"
    d.mkdir()
    for sid in spec_ids:
        (d / f"{sid}.yaml").write_text(_yaml.safe_dump(
            BenchTask(id=sid, title="t", request="r", subset="core",
                      runnable=True).to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]
    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))

    class _Runner:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            return BenchScore(
                task_id=spec.id, title=spec.title, outcome_status="done",
                goal_satisfied=True, escalated_honestly=False, mergeable=None,
                nh_tokens=5, nh_cache_tokens=0, nh_cache_creation_tokens=0,
                nh_turns=1, nh_wall_clock_s=0.0, orig_tokens=0,
                orig_cache_tokens=0, orig_cache_creation_tokens=0,
                orig_wall_clock_s=0.0, orig_corrections=0,
            )
    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _Runner)
    return d


def test_a_run_records_but_publishes_nothing(tmp_path, monkeypatch):
    """THE invariant this whole change exists for. Publishing as a side effect
    of finishing is what let a saturated run and a one-spec probe each overwrite
    the committed report and the gate baseline. Without this test, re-adding
    either write leaves the suite green."""
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    res_dir = tmp_path / "res"
    res_dir.mkdir()
    report = tmp_path / "NORTH_STAR_BENCH.md"
    report.write_text("PUBLISHED BASELINE — MUST NOT MOVE\n")
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", res_dir)
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD", report)
    d = _bench_env(tmp_path, monkeypatch, ["ns-a", "ns-b"])

    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])

    assert not (res_dir / "latest.json").exists(), \
        "a run wrote the gate baseline — publishing must be an explicit act"
    assert report.read_text(encoding="utf-8") == "PUBLISHED BASELINE — MUST NOT MOVE\n", \
        "a run overwrote the committed report"
    assert list(res_dir.glob("run-*.json")), "the run recorded no results file"


def test_two_unlabelled_runs_with_different_specs_do_not_share_a_checkpoint(
    tmp_path, monkeypatch
):
    """The collision was at the DEFAULT label: a probe and a `--full` run both
    slug to "run", so the label alone never separated them and the probe's
    clean-completion unlink() deleted the corpus run's only resumable state.

    Asserts the checkpoint files a run actually leaves on disk. Rebuilding the
    expected filename from _slug/_spec_set_key would only prove sha256 is
    injective — it would pass with the key dropped from the path entirely.
    Each run is killed partway (KeyboardInterrupt escapes the per-spec `except
    Exception`) so its checkpoint survives, which is the state being protected.
    """
    import json as _json
    from click.testing import CliRunner
    from no_human.cli.commands import cli
    from no_human.eval.northstar import BenchScore

    res_dir = tmp_path / "res"
    res_dir.mkdir()
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", res_dir)
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD", tmp_path / "N.md")

    def _run_dying_on_last(spec_ids):
        seen = []

        class _DieOnLast:
            def __init__(self, *a, **kw): ...
            async def run_one(self, spec, *, workdir):
                seen.append(spec.id)
                if len(seen) == len(spec_ids):
                    raise KeyboardInterrupt("Stream closed (process killed)")
                return BenchScore(
                    task_id=spec.id, title=spec.title, outcome_status="done",
                    goal_satisfied=True, escalated_honestly=False,
                    mergeable=None, nh_tokens=5, nh_cache_tokens=0,
                    nh_cache_creation_tokens=0, nh_turns=1,
                    nh_wall_clock_s=0.0, orig_tokens=0, orig_cache_tokens=0,
                    orig_cache_creation_tokens=0, orig_wall_clock_s=0.0,
                    orig_corrections=0,
                )
        d = _bench_env(tmp_path, monkeypatch, spec_ids)
        monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _DieOnLast)
        CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])

    # A corpus run, then a probe — both unlabelled, as the real runs were.
    _run_dying_on_last(["ns-full-1", "ns-full-2", "ns-full-3"])
    after_corpus = sorted(res_dir.glob("progress-*.json"))
    assert len(after_corpus) == 1, f"expected one checkpoint, got {after_corpus}"
    corpus_ckpt = after_corpus[0]
    corpus_scores = {s["task_id"] for s in
                     _json.loads(corpus_ckpt.read_text(encoding="utf-8"))["scores"]}
    assert corpus_scores == {"ns-full-1", "ns-full-2"}

    _run_dying_on_last(["ns-probe-1", "ns-probe-2"])

    checkpoints = sorted(res_dir.glob("progress-*.json"))
    assert len(checkpoints) == 2, (
        f"two unlabelled runs with different spec sets shared one checkpoint "
        f"({[c.name for c in checkpoints]}) — the probe can destroy the corpus "
        f"run's resumable state again")
    assert corpus_ckpt.exists(), "the probe deleted the corpus run's checkpoint"
    assert {s["task_id"] for s in
            _json.loads(corpus_ckpt.read_text(encoding="utf-8"))["scores"]} == corpus_scores, \
        "the probe overwrote the corpus run's checkpoint"


def test_a_superset_run_does_not_consume_another_runs_legacy_checkpoint(
    tmp_path, monkeypatch
):
    """Round 2's subset test was an ownership test, and a subset relation is not
    ownership: a run whose spec set CONTAINS the legacy specs passed it, so
    `--full --resume` would swallow an unrelated core run's scores and then
    delete its checkpoint. Reachable on the real v15 file."""
    import json as _json
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    res_dir = tmp_path / "res"
    res_dir.mkdir()
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", res_dir)
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD", tmp_path / "N.md")

    banked = res_dir / "progress.json"
    banked.write_text(_json.dumps({
        "created_at": "x", "label": "expanded-core-v15",
        "scores": [{
            "task_id": "ns-a", "title": "t", "outcome_status": "escalated",
            "goal_satisfied": False, "escalated_honestly": True,
            "mergeable": None, "nh_tokens": 0, "nh_cache_tokens": 0,
            "nh_cache_creation_tokens": 0, "nh_turns": 0, "nh_wall_clock_s": 0.0,
            "orig_tokens": 0, "orig_cache_tokens": 0,
            "orig_cache_creation_tokens": 0, "orig_wall_clock_s": 0.0,
            "orig_corrections": 0,
        }],
    }))
    before = banked.read_bytes()

    # A strict SUPERSET of the banked spec set.
    d = _bench_env(tmp_path, monkeypatch, ["ns-a", "ns-b", "ns-c"])
    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d), "--resume"])

    assert banked.exists(), "a superset run deleted another run's checkpoint"
    assert banked.read_bytes() == before, "another run's checkpoint was rewritten"
    # ...and its dead spec must not have contaminated this run's card.
    results = [f for f in res_dir.glob("*.json") if not f.name.startswith("progress")]
    card = _json.loads(results[0].read_text(encoding="utf-8"))
    assert card["aggregate"]["dead_specs"] == 0, \
        "adopted a foreign run's zero-token spec into this run's card"


def test_an_owned_legacy_checkpoint_is_resumed_from_but_left_in_place(
    tmp_path, monkeypatch
):
    """The migration case the fallback exists for: a run started before per-label
    checkpoints stays resumable. It must COPY, not consume — a run may only
    unlink a checkpoint it created, which is the one rule that makes both
    destruction incidents impossible rather than merely unlikely."""
    import json as _json
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    res_dir = tmp_path / "res"
    res_dir.mkdir()
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", res_dir)
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD", tmp_path / "N.md")

    banked = res_dir / "progress.json"
    banked.write_text(_json.dumps({
        "created_at": "x", "label": "mine",
        "scores": [{
            "task_id": "ns-a", "title": "t", "outcome_status": "done",
            "goal_satisfied": True, "escalated_honestly": False,
            "mergeable": None, "nh_tokens": 999, "nh_cache_tokens": 0,
            "nh_cache_creation_tokens": 0, "nh_turns": 1,
            "nh_wall_clock_s": 0.0, "orig_tokens": 0, "orig_cache_tokens": 0,
            "orig_cache_creation_tokens": 0, "orig_wall_clock_s": 0.0,
            "orig_corrections": 0,
        }],
    }))
    before = banked.read_bytes()

    d = _bench_env(tmp_path, monkeypatch, ["ns-a", "ns-b"])
    res = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--label", "mine", "--resume"])

    assert "1 spec(s) already scored" in res.output, res.output
    assert banked.exists() and banked.read_bytes() == before, \
        "the legacy checkpoint was consumed instead of copied"
    results = [f for f in res_dir.glob("*.json") if not f.name.startswith("progress")]
    card = _json.loads(results[0].read_text(encoding="utf-8"))
    scored = {s["task_id"]: s["nh_tokens"] for s in card["scores"]}
    assert scored == {"ns-a": 999, "ns-b": 5}, \
        f"the checkpointed spec was not carried into the final card: {scored}"


def _banked(res_dir, label, task_ids, *, nh_tokens=0):
    """A checkpoint at the LEGACY path, as an older build would have left it."""
    import json as _json
    banked = res_dir / "progress.json"
    banked.write_text(_json.dumps({
        "created_at": "x", "label": label,
        "scores": [{
            "task_id": tid, "title": "t", "outcome_status": "escalated",
            "goal_satisfied": False, "escalated_honestly": True,
            "mergeable": None, "nh_tokens": nh_tokens, "nh_cache_tokens": 0,
            "nh_cache_creation_tokens": 0, "nh_turns": 0,
            "nh_wall_clock_s": 0.0, "orig_tokens": 0, "orig_cache_tokens": 0,
            "orig_cache_creation_tokens": 0, "orig_wall_clock_s": 0.0,
            "orig_corrections": 0,
        } for tid in task_ids],
    }))
    return banked


def test_an_unlabelled_legacy_checkpoint_is_declined_not_guessed_at(
    tmp_path, monkeypatch
):
    """The shape BOTH real incidents actually had. An unlabelled checkpoint
    carries no identity, so a run cannot tell whether it is its own — and the
    unlabelled default is exactly why the label alone never separated a probe
    from the corpus. It must be declined, not adopted on a subset match.

    Without this, dropping the non-empty-label clause leaves the suite green
    while `--full --resume` swallows an unrelated run's dead specs.
    """
    import json as _json
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    res_dir = tmp_path / "res"
    res_dir.mkdir()
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", res_dir)
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD", tmp_path / "N.md")

    banked = _banked(res_dir, "", ["ns-a"])          # unlabelled, zero-token
    before = banked.read_bytes()

    d = _bench_env(tmp_path, monkeypatch, ["ns-a", "ns-b"])   # unlabelled run
    CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d), "--resume"])

    assert banked.read_bytes() == before, "an unlabelled checkpoint was consumed"
    results = [f for f in res_dir.glob("*.json") if not f.name.startswith("progress")]
    card = _json.loads(results[0].read_text(encoding="utf-8"))
    assert card["aggregate"]["dead_specs"] == 0, \
        "adopted an unidentifiable checkpoint's zero-token spec into this run"
    assert {s["task_id"] for s in card["scores"]} == {"ns-a", "ns-b"}


def test_a_same_label_checkpoint_from_a_different_spec_set_is_declined(
    tmp_path, monkeypatch
):
    """Label equality alone is not ownership either: two runs can share a label
    and cover different specs. The subset clause is what closes that, and it was
    the last clause of the predicate with no test."""
    import json as _json
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    res_dir = tmp_path / "res"
    res_dir.mkdir()
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR", res_dir)
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD", tmp_path / "N.md")

    # Holds one spec this run DOES cover, plus one it does not — so the subset
    # test fails while the downstream foreign-spec filter cannot save us: it
    # would drop ns-elsewhere but happily keep the stale ns-a score.
    banked = _banked(res_dir, "mine", ["ns-a", "ns-elsewhere"], nh_tokens=999)
    before = banked.read_bytes()

    d = _bench_env(tmp_path, monkeypatch, ["ns-a", "ns-b"])
    CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(d), "--label", "mine", "--resume"])

    assert banked.read_bytes() == before
    results = [f for f in res_dir.glob("*.json") if not f.name.startswith("progress")]
    card = _json.loads(results[0].read_text(encoding="utf-8"))
    assert {s["task_id"] for s in card["scores"]} == {"ns-a", "ns-b"}, \
        "a same-label checkpoint from a different spec set leaked into this run"
    scored = {s["task_id"]: s["nh_tokens"] for s in card["scores"]}
    assert scored["ns-a"] == 5, (
        f"ns-a carried a score from a differently-scoped run of the same label "
        f"({scored['ns-a']}) instead of being re-run — the spec sets differ, so "
        f"the flags differed, so the scores are not interchangeable")


def test_cli_records_corpus_available_from_the_canonical_dir(tmp_path, monkeypatch):
    """The loaded-vs-available rule depends on the CLI producing this number,
    and the ENTIRE plumbing could be severed — CLI, as_dict, and load — with all
    2021 tests green, because every other test hand-constructs the card.

    Canonical dir has 20 specs; the run loads 11 via --specs-dir. The refusal
    must name BOTH numbers, which is only possible if the value travelled from
    the canonical dir through the card to the message.
    """
    import yaml as _yaml
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    canon = tmp_path / "canonical"
    canon.mkdir()
    for i in range(20):
        spec = BenchTask(id=f"ns-c{i}", title="t", request="r", subset="core",
                         runnable=True)
        (canon / f"ns-c{i}.yaml").write_text(_yaml.safe_dump(spec.to_dict()))
    run_dir = tmp_path / "subset"
    run_dir.mkdir()
    for i in range(11):
        spec = BenchTask(id=f"ns-c{i}", title="t", request="r", subset="core",
                         runnable=True)
        (run_dir / f"ns-c{i}.yaml").write_text(_yaml.safe_dump(spec.to_dict()))

    class _Cfg:
        data = {"llm": {}}
        primary_model = "m"
        review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]

    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))

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
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR",
                        tmp_path / "results")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NORTH_STAR_BENCH.md")
    monkeypatch.setattr("no_human.eval.bench_task.NORTHSTAR_DIR", canon)

    result = CliRunner().invoke(
        cli, ["bench", "run", "--specs-dir", str(run_dir), "--gate"])

    assert "11 of 20" in result.output, result.output
    assert result.exit_code == 1, result.output
