"""The local repo map: vendor-neutral specs, valid instrument.

Tracked specs carry vendor-neutral repo paths that resolve nowhere; a spec whose
repo cannot be cloned scores as a capability FAILURE rather than a skip. The map
translates those paths to real checkouts at LOAD time, so the scrub survives in
git and the benchmark still measures something.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from no_human.eval.bench_task import (
    check_repo_map,
    load_bench_tasks,
    load_repo_map,
    remap_repo_path,
    spec_project_name,
)

MAP = {
    "/spec/neutral/service-a": "/local/real/svc-a",
    "/spec/neutral/db": "/local/real/db",
    "/spec/neutral/db/query-service": "/local/real/db/qs",
}


def _write_spec(directory, task_id: str, repo_path: str) -> None:
    (directory / f"{task_id}.yaml").write_text(yaml.safe_dump({
        "id": task_id, "title": "t", "request": "r", "subset": "core",
        "repo": {"path": repo_path, "pin": "abc123", "branch": ""},
    }, sort_keys=False))


# ------------------------------ translation ------------------------------- #

def test_exact_path_is_translated():
    assert remap_repo_path("/spec/neutral/service-a", MAP) == \
        "/local/real/svc-a"


def test_longest_prefix_wins_over_a_shorter_nested_one():
    """`/spec/neutral/db` also prefixes the query-service path. The more
    specific entry must win, independent of dict ordering."""
    assert remap_repo_path("/spec/neutral/db/query-service", MAP) == \
        "/local/real/db/qs"


def test_a_subdirectory_below_a_mapped_root_keeps_its_tail():
    assert remap_repo_path("/spec/neutral/service-a/sub/dir", MAP) == \
        "/local/real/svc-a/sub/dir"


def test_matching_respects_path_boundaries():
    """A sibling that merely shares a string prefix must NOT be rewritten —
    otherwise `/x/foo` would silently capture `/x/foobar`."""
    assert remap_repo_path("/spec/neutral/service-a-old", MAP) == \
        "/spec/neutral/service-a-old"


def test_unlisted_paths_pass_through_untouched():
    assert remap_repo_path("/somewhere/else", MAP) == "/somewhere/else"
    assert remap_repo_path("/somewhere/else", {}) == "/somewhere/else"


# -------------------------------- loading --------------------------------- #

def test_absent_map_file_is_a_no_op(tmp_path):
    assert load_repo_map(tmp_path / "nope.yaml") == {}


def test_map_is_loaded_and_trailing_slashes_normalised(tmp_path):
    p = tmp_path / "repo_map.yaml"
    p.write_text(yaml.safe_dump({"/n/a/": "/real/b/"}))
    assert load_repo_map(p) == {"/n/a": "/real/b"}


@pytest.mark.parametrize("body, expected", [
    ("/n/a: relative/target", "absolute"),
    ("relative/src: /real/b", "absolute"),
    ("- just\n- a\n- list", "mapping"),
])
def test_structural_errors_are_loud(tmp_path, body, expected):
    """Structural problems are the operator's typo and must surface at once.
    Whether a TARGET exists is deliberately not checked here — see the module
    docstring. A target that does not EXIST is reported by
    check_repo_map instead, so one stale entry cannot kill a run."""
    p = tmp_path / "repo_map.yaml"
    p.write_text(body)
    with pytest.raises(ValueError, match=expected):
        load_repo_map(p)


def test_a_target_that_does_not_exist_is_NOT_a_load_error(tmp_path):
    """Deliberate: one unavailable repo must not kill a whole bench run."""
    p = tmp_path / "repo_map.yaml"
    p.write_text(yaml.safe_dump({"/n/a": "/definitely/not/here"}))
    assert load_repo_map(p) == {"/n/a": "/definitely/not/here"}


# ------------------------- end-to-end through the loader ------------------- #

def test_load_bench_tasks_applies_the_map(tmp_path, monkeypatch):
    """The load path is what every consumer sees — the runner's repo check, the
    sandbox copy, the scorer. Observe the loaded spec, not the helper."""
    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", "/spec/neutral/service-a")
    _write_spec(specs, "ns-02", "/untouched/repo")

    map_file = tmp_path / "repo_map.yaml"
    map_file.write_text(yaml.safe_dump(MAP))
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH", map_file)

    by_id = {t.id: t for t in load_bench_tasks(specs)}
    assert by_id["ns-01"].repo["path"] == "/local/real/svc-a"
    assert by_id["ns-02"].repo["path"] == "/untouched/repo"
    # Translation must not disturb anything else on the spec.
    assert by_id["ns-01"].repo["pin"] == "abc123"


# --------- the translated path must NOT reach a git-tracked artifact -------- #

def test_reporting_uses_the_spec_path_not_the_translated_one(tmp_path, monkeypatch):
    """Review finding (Critical): `project` is rendered into
    docs/NORTH_STAR_BENCH.md, which is TRACKED. Deriving it from the translated
    path writes the real local checkout's name into git and silently undoes the
    vendor-neutral scrub — the exact outcome this map exists to avoid.
    """
    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", "/spec/neutral/service-a")
    map_file = tmp_path / "repo_map.yaml"
    map_file.write_text(yaml.safe_dump(MAP))
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH", map_file)

    (task,) = load_bench_tasks(specs)
    assert task.repo["path"] == "/local/real/svc-a"   # runner uses this
    assert spec_project_name(task) == "service-a"      # reporting uses this


def test_skipped_score_carries_the_neutral_project_name(tmp_path, monkeypatch):
    """Pins the WIRING at a real production call site, not just the helper:
    NorthStarRunner._skipped builds a BenchScore whose `project` lands in the
    published report."""
    from no_human.eval.northstar import NorthStarRunner

    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", "/spec/neutral/service-a")
    map_file = tmp_path / "repo_map.yaml"
    map_file.write_text(yaml.safe_dump(MAP))
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH", map_file)
    (task,) = load_bench_tasks(specs)

    runner = NorthStarRunner.__new__(NorthStarRunner)   # _skipped needs no deps
    score = runner._skipped(task)
    assert score.project == "service-a"
    assert "svc-a" not in (score.project or "")


def test_rendered_report_does_not_contain_the_real_checkout_name(
        tmp_path, monkeypatch):
    """End of the chain: observe the actual markdown written to the tracked doc.

    TWO distinct projects on purpose — the per-project table only renders when
    `len(proj) > 1`, so a single-spec card asserts absence from markdown that
    was never generated. (That is exactly how the first draft of this test
    passed while the leak was still live.)
    """
    from no_human.eval.northstar import NorthStarRunner
    from no_human.eval.northstar_card import NorthStarCard, render_northstar_md

    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", "/spec/neutral/service-a")
    _write_spec(specs, "ns-02", "/spec/neutral/db")
    map_file = tmp_path / "repo_map.yaml"
    map_file.write_text(yaml.safe_dump(MAP))
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH", map_file)
    tasks = load_bench_tasks(specs)
    assert len({spec_project_name(t) for t in tasks}) == 2   # table will render

    runner = NorthStarRunner.__new__(NorthStarRunner)
    md = render_northstar_md(
        NorthStarCard(label="t", scores=[runner._skipped(t) for t in tasks]))
    assert "| service-a |" in md          # the table really is in the output
    assert "svc-a" not in md
    assert "/local/real" not in md


def test_a_crash_note_does_not_carry_the_translated_path(tmp_path, monkeypatch):
    """Review finding (Critical): `project` was not the only route into the
    tracked report. A failed sandbox copy raises CalledProcessError, whose
    string embeds the whole argv — including the real local path — and that
    becomes the spec's `notes`, which IS rendered. Truncation is not a guard:
    whether an org name survives depends only on how long the operator's home
    directory happens to be.
    """
    import subprocess as _sp

    from no_human.eval.bench_task import redact_local_path
    from no_human.eval.northstar_card import NorthStarCard, render_northstar_md
    from no_human.eval.northstar import BenchScore

    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", "/spec/neutral/service-a")
    _write_spec(specs, "ns-02", "/spec/neutral/db")
    map_file = tmp_path / "repo_map.yaml"
    map_file.write_text(yaml.safe_dump(MAP))
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH", map_file)
    tasks = {t.id: t for t in load_bench_tasks(specs)}
    spec = tasks["ns-01"]

    # The real failure, not a hand-written string.
    exc = _sp.CalledProcessError(
        128, ["git", "clone", "--no-hardlinks", spec.repo["path"], "/tmp/x"])
    assert "/local/real/svc-a" in str(exc)          # the leak, before redaction

    note = redact_local_path(str(exc), spec)
    assert "/local/real" not in note
    assert "/spec/neutral/service-a" in note

    def _crashed(t, notes):
        return BenchScore(
            task_id=t.id, title=t.title, outcome_status="crashed",
            goal_satisfied=False, escalated_honestly=False, mergeable=None,
            nh_tokens=0, nh_cache_tokens=0, nh_cache_creation_tokens=0,
            nh_turns=1, nh_wall_clock_s=0.0, orig_tokens=0, orig_cache_tokens=0,
            orig_cache_creation_tokens=0, orig_wall_clock_s=0.0,
            orig_corrections=0, subset=t.subset,
            project=spec_project_name(t), notes=notes)

    md = render_northstar_md(NorthStarCard(label="t", scores=[
        _crashed(spec, f"runner crashed: {note[:300]}"),
        _crashed(tasks["ns-02"], "runner crashed: other"),
    ]))
    assert "/local/real" not in md
    assert "svc-a" not in md


# ------------------------------ pre-flight --------------------------------- #

def test_check_repo_map_reports_a_missing_repo(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", "/definitely/not/here")
    (problems,) = [check_repo_map(load_bench_tasks(specs))]
    assert problems and "does not exist here" in problems[0]


def test_check_repo_map_reports_a_directory_that_is_not_a_repo(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    _write_spec(specs, "ns-01", str(plain))
    problems = check_repo_map(load_bench_tasks(specs))
    assert problems and "not a git repo" in problems[0]


def test_check_repo_map_catches_a_target_that_lacks_the_pin(tmp_path):
    """The dangerous case: a typo'd target that IS a real checkout would
    otherwise yield confident, meaningless scores. The pin discriminates."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", str(repo))   # pin abc123 is not in this repo
    problems = check_repo_map(load_bench_tasks(specs))
    assert problems and "does not contain pin" in problems[0]


def test_check_repo_map_is_silent_when_everything_resolves(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b",
                    "-c", "user.name=a", "commit", "-qm", "c"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "ns-01.yaml").write_text(yaml.safe_dump({
        "id": "ns-01", "title": "t", "request": "r", "subset": "core",
        "repo": {"path": str(repo), "pin": sha, "branch": ""},
    }, sort_keys=False))
    assert check_repo_map(load_bench_tasks(specs)) == []


def test_load_bench_tasks_without_a_map_leaves_paths_alone(tmp_path, monkeypatch):
    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", "/spec/neutral/service-a")
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH",
                        tmp_path / "absent.yaml")

    (task,) = load_bench_tasks(specs)
    assert task.repo["path"] == "/spec/neutral/service-a"


def test_check_repo_map_skips_an_undiscriminating_pin(tmp_path):
    """`pin: HEAD` and an EMPTY pin cannot tell a wrong checkout from the right
    one, so they are not checked at all rather than falsely reassuring.

    The empty-pin case is the one that is OBSERVABLE, and the one that matters:
    2 of the 55 tracked specs carry `pin: ''`, and without the skip
    `git cat-file -e '^{commit}'` fails and invents a bogus "does not contain
    pin" problem for a perfectly good repo. (An earlier version of this test
    used `pin: HEAD` on a one-commit repo, where `HEAD^{commit}` resolves
    anyway — so it passed with the skip branch DELETED and proved nothing.)
    """
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b",
                    "-c", "user.name=a", "commit", "-qm", "c"], check=True)
    specs = tmp_path / "specs"
    specs.mkdir()
    for i, pin in enumerate(("", "HEAD")):
        (specs / f"ns-0{i}.yaml").write_text(yaml.safe_dump({
            "id": f"ns-0{i}", "title": "t", "request": "r", "subset": "core",
            "repo": {"path": str(repo), "pin": pin, "branch": ""},
        }, sort_keys=False))
    assert check_repo_map(load_bench_tasks(specs)) == []


def test_redaction_survives_a_non_normalised_map_target(tmp_path, monkeypatch):
    """Review finding: `Path()` collapses `//` and `/./` before an argv is
    built, so an unnormalised map target made redact_local_path's exact
    substring replace a silent no-op and the real path reached the tracked
    report. The guard must not depend on how the operator typed the map."""
    import subprocess as _sp

    from no_human.eval.bench_task import redact_local_path

    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", "/spec/neutral/service-a")
    map_file = tmp_path / "repo_map.yaml"
    map_file.write_text(yaml.safe_dump(
        {"/spec/neutral/service-a": "/local//real/./svc-a"}))
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH", map_file)
    (task,) = load_bench_tasks(specs)

    exc = _sp.CalledProcessError(
        128, ["git", "clone", "--no-hardlinks",
              str(Path(task.repo["path"])), "/tmp/x"])
    note = redact_local_path(str(exc), task)
    assert "/local/real" not in note, note
    assert "svc-a" not in note, note


def test_cli_bench_run_reports_unresolvable_specs_before_running(
        tmp_path, monkeypatch):
    """Pins the WIRING of the pre-flight. The map is gitignored, so it is absent
    in every git worktree; without this warning a run launched from one fails to
    resolve most specs and reads as a model regression."""
    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli
    from no_human.eval.bench_task import BenchTask
    from no_human.eval.northstar import BenchScore

    d = tmp_path / "specs"
    d.mkdir()
    for i in range(2):
        spec = BenchTask(id=f"ns-pf{i}", title="t", request="r", subset="core",
                         runnable=True,
                         repo={"path": "/definitely/not/here", "pin": "", "branch": ""})
        (d / f"ns-pf{i}.yaml").write_text(yaml.safe_dump(spec.to_dict()))

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
                orig_wall_clock_s=0.0, orig_corrections=0, subset=spec.subset)

    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _StubRunner)
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR",
                        tmp_path / "results")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NORTH_STAR_BENCH.md")
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH",
                        tmp_path / "absent_map.yaml")

    result = CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])
    assert "will not resolve" in result.output, result.output
    assert "no repo map" in result.output, result.output
    # The pre-flight is ADVISORY: it must warn and then let the run proceed,
    # not warn and blow up.
    assert result.exit_code == 0, result.output


def test_cli_redacts_the_translated_path_from_a_crash(tmp_path, monkeypatch):
    """Pins the WIRING of the redaction, not just the helper. A crashing spec
    is the COMMON case for a mis-mapped repo, and its note is rendered into the
    tracked report."""
    import subprocess as _sp

    from click.testing import CliRunner
    from no_human.cli import commands as cmds
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    d.mkdir()
    _write_spec(d, "ns-01", "/spec/neutral/service-a")
    map_file = tmp_path / "repo_map.yaml"
    map_file.write_text(yaml.safe_dump(MAP))
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH", map_file)

    class _Cfg:
        data = {"llm": {}}
        primary_model = "m"
        review_model = "m"
        def __getitem__(self, k):
            return {"safety": {"forbidden_paths": []},
                    "git": {"never_push_to": []}}[k]

    monkeypatch.setattr(cmds, "_bootstrap", lambda *a, **kw: (_Cfg(), None))

    class _CrashingRunner:
        def __init__(self, *a, **kw): ...
        async def run_one(self, spec, *, workdir):
            # The real failure shape: argv carries the TRANSLATED path.
            raise _sp.CalledProcessError(
                128, ["git", "clone", "--no-hardlinks",
                      spec.repo["path"], str(workdir)])

    monkeypatch.setattr("no_human.eval.northstar.NorthStarRunner", _CrashingRunner)
    monkeypatch.setattr("no_human.eval.northstar_card.RESULTS_DIR",
                        tmp_path / "results")
    monkeypatch.setattr("no_human.eval.northstar_card.REPORT_MD",
                        tmp_path / "NORTH_STAR_BENCH.md")

    result = CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])

    # The pre-flight DELIBERATELY prints the real local path — it is the
    # operator's own machine and they need to know which checkout is missing.
    # That is console-only. What must never carry it is the RECORDED run, which
    # is what `nh bench publish` renders into the tracked report.
    crash_line = next(ln for ln in result.output.splitlines() if "crashed" in ln)
    assert "/local/real" not in crash_line, crash_line   # console truncates at 80

    recorded = list((tmp_path / "results").glob("*.json"))
    assert recorded, "the run recorded nothing to inspect"
    for f in recorded:
        body = f.read_text(encoding="utf-8")
        assert "/local/real" not in body, f.name
        assert "svc-a" not in body, f.name
        # The note keeps the SPEC's path, so the record stays diagnosable.
        assert "/spec/neutral/service-a" in body, f.name
        # Crashed specs must group under their project, not "?".
        assert '"project": "service-a"' in body, f.name


def test_cli_renders_a_malformed_map_instead_of_a_traceback(tmp_path, monkeypatch):
    """load_repo_map's message is precise and human-readable, but unhandled it
    exited 1 with a traceback and NO console output — so the one thing that
    tells the operator what to fix never reached them."""
    from click.testing import CliRunner
    from no_human.cli.commands import cli

    d = tmp_path / "specs"
    d.mkdir()
    _write_spec(d, "ns-01", "/spec/neutral/service-a")
    bad = tmp_path / "repo_map.yaml"
    bad.write_text("/spec/neutral/service-a: relative/target")
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH", bad)

    result = CliRunner().invoke(cli, ["bench", "run", "--specs-dir", str(d)])
    assert result.exit_code == 1
    assert "absolute" in result.output, result.output
    # CliRunner stores a traceback on .exception, never in .output, so
    # asserting on the text could never fail. Assert the real thing.
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.asyncio
async def test_the_SCORED_path_also_uses_the_neutral_project_name(
        tmp_path, monkeypatch):
    """`_score` is the branch every successfully-running spec takes — and it was
    the ONE `project` call site with no test.

    Review finding: reverting it to the pre-fix leaking form passed the entire
    2030-test suite. The two existing tests cover `_skipped` and the crash path,
    i.e. the two RARE branches, while the common one was unguarded — on a leak
    that had already regressed twice in review.
    """
    from no_human.core.task import Task, TaskStatus
    from no_human.eval.northstar import NorthStarRunner

    specs = tmp_path / "specs"
    specs.mkdir()
    _write_spec(specs, "ns-01", "/spec/neutral/service-a")
    map_file = tmp_path / "repo_map.yaml"
    map_file.write_text(yaml.safe_dump(MAP))
    monkeypatch.setattr("no_human.eval.bench_task.REPO_MAP_PATH", map_file)
    (task,) = load_bench_tasks(specs)

    class _Verdict:
        satisfied = True
        # A judge that quotes the sandbox path it was given. Defence in depth:
        # not a known leak channel, but it is the last free-text field reaching
        # the tracked report, so "is redaction applied everywhere" gets ONE
        # answer instead of "everywhere except here".
        evidence = "the change under /local/real/svc-a looks correct"

    class _Judge:
        async def judge(self, **kw):
            return _Verdict()

    runner = NorthStarRunner.__new__(NorthStarRunner)
    runner.goal_judge = _Judge()

    class _Outcome:
        status = TaskStatus.AWAITING_APPROVAL
        task = Task.new("t", repo_path="/tmp/x")
        detail = ""
        report = ""

    score = await runner._score(
        task, _Outcome(), work=tmp_path, base_sha="0" * 40,
        attempts=[{"tokens_used": 100}], elapsed=1.0)

    assert score.project == "service-a"
    assert "svc-a" not in (score.project or "")
    assert "/local/real" not in (score.notes or "")


def test_a_degenerate_local_path_cannot_MANGLE_the_note():
    """`redact_local_path` builds a set of path FORMS and replaces each. A
    degenerate `local` puts "" and "." into that set, and
    `text.replace("", x)` splices x between EVERY character — a 30-char skip
    note became 765 characters of garbage.

    The direction of harm is corruption, not disclosure (the replacement is the
    neutral spec path), and the reachable trigger is narrow — a padded path
    cannot match a repo-map key, so redaction usually short-circuits. But the
    guard that produced the padded string is the same change that added the
    stripped forms, so the two shipped together.
    """
    from no_human.eval.bench_task import BenchTask, redact_local_path

    def _spec(path: str) -> BenchTask:
        t = BenchTask(id="x", title="t", request="r", subset="core",
                      runnable=True, repo={"path": path, "pin": "", "branch": ""})
        t.spec_repo_path = "/spec/neutral/service-a"
        return t

    # Whitespace-only: yields "" and "." as forms.
    note = "skipped: spec has no repo.path"
    assert redact_local_path(note, _spec("   ")) == note
    assert redact_local_path("skipped: not absolute", _spec("  .  ")) == \
        "skipped: not absolute"
    # Root would rewrite every separator in the text.
    sep_text = "a/b/c path with /separators/"
    assert redact_local_path(sep_text, _spec("/")) == sep_text

    # EVERY whitespace character Python strips, not a hand-rolled subset. An
    # earlier " .\t\n\r" omitted \v and \f, so a vertical-tab-only path still
    # rewrote the note — the same defect, one character instead of every one.
    import string
    # The probe text must CONTAIN the character under test, or replace() is a
    # no-op and the assertion passes for the wrong reason. A first version used
    # one fixed string holding only \v and \f, so every unicode case was
    # vacuous and the mutant survived.
    for ch in string.whitespace + "\xa0\u2028\u2029\u3000\x85":
        probe = f"left{ch}right{ch}end"
        assert ch in probe                     # the trap this guards against
        assert redact_local_path(probe, _spec(ch)) == probe, repr(ch)
        assert redact_local_path(probe, _spec(ch * 3)) == probe, repr(ch)

    # MIXED separator/whitespace forms. These need whitespace in the FIRST
    # strip set: one pass of `.strip("/.")` leaves " / " as "/", and the
    # trailing bare `.strip()` cannot remove it because it is no longer at an
    # end. Dropping `string.whitespace` survived every case above precisely
    # because none of them alternated.
    # Includes the UNICODE mirrors: chained strips only reach the ENDS, so
    # "\xa0/\xa0" survived them exactly as " / " survived a single pass.
    for form in (" / ", " . ", "\t/\t", " /. ", " .. ", "\n/\n",
                 "\xa0/\xa0", "\u3000.\u3000", "\x85/\x85"):
        probe = f"left{form}right"
        assert redact_local_path(probe, _spec(form)) == probe, repr(form)

    # And every REAL path still redacts — including relative ones. The rule is
    # keyed on DEGENERACY, not on os.path.isabs: absoluteness looks like the
    # same rule but also skips a relative-but-real path, leaving a local path
    # in a note bound for the tracked report. That direction is disclosure,
    # not corruption, and it is the one that hard-blocks publication.
    for local in ("/Users/me/secret-repo", "Users/me/secret-repo",
                  "./secret-repo", "/Users/me/secret-repo/"):
        out = redact_local_path(f"skipped: missing {local}", _spec(local))
        assert "/Users/me" not in out, f"{local!r} leaked: {out!r}"
        assert "secret-repo" not in out, f"{local!r} leaked: {out!r}"
        assert "/spec/neutral/service-a" in out, out
