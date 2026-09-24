"""`--mode gate` of the reviewer-recall harness (issue #436).

The diff-only mode hands the reviewer `diff_override`, which is the
single-turn, no-tools, no-evidence path — a different reviewer from the one
that gates PRs. Gate mode must build the reviewer through the production
factory (`AdversarialReviewer.from_config`), honour the configured reviewer
backend, take the refs path with tools and the evidence sections, review a
scratch repo holding only base and head, and do all of it without mutating
the process environment. No LLM is called: the reviewer's session methods are
stubbed at the point where the gate path hands them its prompt and options.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from no_human.review.reviewer import AdversarialReviewer, ChecklistItem, ReviewDecision

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "eval" / "reviewer_recall" / "runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "test_reviewer_recall_gate_runner_module", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rr = _load_runner()

EVIDENCE = ("LINT-EVIDENCE-SENTINEL", "WIRING-EVIDENCE-SENTINEL",
            "TYPE-EVIDENCE-SENTINEL")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def _big_create_diff(path: str, *, defect: bool) -> tuple[str, int]:
    """A deterministic create-only diff over 60,000 characters.

    2,400 filler functions, then (seeded variant) an inverted comparison as
    the LAST function — so the defect sits past `_DIFF_CAP`, in the part of
    the patch the gate cuts, and the file is too big for the whole-file
    `_FILES_CAP` context too. The clean control is the same file with the
    comparison right. Returns (diff, 1-based line of the final function's
    comparison) so a test can place truth.json's hunk on it.
    """
    body = [f"def helper_{i:04d}(x):\n    return x + {i}\n" for i in range(2400)]
    op = ">" if defect else "<="
    body.append(f"def is_within_limit(used, limit):\n    return used {op} limit\n")
    lines = "".join(body).splitlines()
    diff = (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
            + "".join(f"+{line}\n" for line in lines))
    return diff, len(lines)


def _case(tmp_path: Path, case_id: str, diff: str, truth: dict) -> object:
    case_dir = tmp_path / "cases" / case_id
    case_dir.mkdir(parents=True)
    return rr.CaseSpec(case_id=case_id, dir=case_dir, base_ref="synthetic",
                       diff_text=diff, truth=truth,
                       request="Add a usage-limit helper.")


def _stub_gate_session(monkeypatch, record: list, verdict=None):
    """Replace ONLY the model call and the evidence collectors; everything
    between `review()` and them (the refs diff, the cut ledger, the prompt
    builder) is the production code under test."""
    import no_human.review.reviewer as reviewer_mod

    async def fake_evidence(repo_path, before_ref, after_ref, *, with_type_evidence=True):
        record.append(("evidence", with_type_evidence))
        return EVIDENCE

    async def fake_agent_review(self, prompt, repo_path, **kw):
        record.append(("agent", self, prompt, kw, repo_path,
                       dict(os.environ), sorted(p.name for p in repo_path.iterdir())))
        if verdict is not None:
            return verdict(repo_path, kw)
        return ReviewDecision(passed=True, checklist=[ChecklistItem("ok", True, "ok")])

    async def fake_fast_review(self, prompt, repo_path, **kw):
        record.append(("fast", self, prompt, kw))
        return ReviewDecision(passed=True, checklist=[ChecklistItem("ok", True, "ok")])

    monkeypatch.setattr(reviewer_mod, "_collect_gate_evidence", fake_evidence)
    monkeypatch.setattr(AdversarialReviewer, "_agent_review", fake_agent_review)
    monkeypatch.setattr(AdversarialReviewer, "_fast_review", fake_fast_review)


def _run_one(tmp_path: Path, case, fn):
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(rr.run_case(REPO_ROOT, case, fn, workdir))


_SMALL_DIFF = ("diff --git a/app.py b/app.py\nnew file mode 100644\n--- /dev/null\n"
               "+++ b/app.py\n@@ -0,0 +1,2 @@\n+def f(x):\n+    return x\n")


def test_gate_mode_builds_via_the_factory_and_honours_the_configured_backend(
        tmp_path, monkeypatch):
    record: list = []
    _stub_gate_session(monkeypatch, record)
    factory_calls = []
    real_from_config = AdversarialReviewer.from_config.__func__

    def spy_from_config(cls, data, **kw):
        factory_calls.append(data)
        return real_from_config(cls, data, **kw)

    monkeypatch.setattr(AdversarialReviewer, "from_config", classmethod(spy_from_config))
    config = {"llm": {"role_backends": {"reviewer": {"backend": "codex",
                                                     "model": "gpt-5-codex"}}}}
    case = _case(tmp_path, "c1", _SMALL_DIFF, {"class": "control"})

    result = _run_one(tmp_path, case, rr._gate_reviewer_fn("ignored", config))

    assert result.status == "OK" and result.clean_pass is True
    assert factory_calls == [config]
    reviewer = next(r[1] for r in record if r[0] == "agent")
    assert reviewer.backend_name == "codex"
    assert type(reviewer._backend).__name__ == "CodexBackend"


def test_gate_mode_default_config_stays_on_the_claude_reviewer(tmp_path, monkeypatch):
    record: list = []
    _stub_gate_session(monkeypatch, record)
    case = _case(tmp_path, "c1", _SMALL_DIFF, {"class": "control"})
    _run_one(tmp_path, case, rr._gate_reviewer_fn("ignored", {}))
    reviewer = next(r[1] for r in record if r[0] == "agent")
    assert (reviewer.backend_name, reviewer.model) == ("claude", "claude-opus-4-8")


def test_gate_mode_gets_tools_and_evidence_sections_diff_only_does_not(
        tmp_path, monkeypatch):
    record: list = []
    _stub_gate_session(monkeypatch, record)
    case = _case(tmp_path, "c1", _SMALL_DIFF, {"class": "control"})

    _run_one(tmp_path, case, rr._gate_reviewer_fn("ignored", {}))
    agent = [r for r in record if r[0] == "agent"]
    assert len(agent) == 1 and not [r for r in record if r[0] == "fast"]
    _, _, prompt, options, *_ = agent[0]
    assert options["max_turns"] > 1  # the multi-turn tool session
    for sentinel in EVIDENCE:
        assert sentinel in prompt
    assert ("evidence", True) in record

    # Contrast: diff-only reaches the single-turn no-tools path, no evidence.
    record.clear()
    _run_one(tmp_path / "d", case, rr._default_reviewer_fn("claude-opus-4-8"))
    assert [r[0] for r in record] == ["fast"]
    assert not any(s in record[0][2] for s in EVIDENCE)


def test_gate_mode_repo_holds_only_base_and_head(tmp_path, monkeypatch):
    seen = {}

    def verdict(repo_path, _kw):
        seen["log"] = _git(repo_path, "log", "--all", "--format=%s").split()
        seen["refs"] = _git(repo_path, "for-each-ref", "--format=%(refname)").split()
        seen["remotes"] = _git(repo_path, "remote").split()
        seen["status"] = _git(repo_path, "status", "--porcelain")
        return ReviewDecision(passed=True, checklist=[ChecklistItem("ok", True, "ok")])

    _stub_gate_session(monkeypatch, [], verdict=verdict)
    case = _case(tmp_path, "c1", _SMALL_DIFF, {"class": "control"})
    _run_one(tmp_path, case, rr._gate_reviewer_fn("ignored", {}))
    assert seen["log"] == ["head", "base"]
    assert len(seen["refs"]) == 1 and seen["remotes"] == []
    assert seen["status"] == ""


def test_gate_mode_never_mutates_the_process_env_or_copies_home_files(
        tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".no_human").mkdir(parents=True)
    (fake_home / ".no_human" / "config.yaml").write_text("llm: {}\n")
    (fake_home / ".no_human" / ".env").write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n")
    monkeypatch.setenv("HOME", str(fake_home))
    before = dict(os.environ)
    record: list = []
    _stub_gate_session(monkeypatch, record)
    case = _case(tmp_path, "c1", _SMALL_DIFF, {"class": "control"})

    _run_one(tmp_path, case, rr._gate_reviewer_fn("ignored", {}))

    *_, env_during, tree = next(r for r in record if r[0] == "agent")
    assert env_during == before, "gate mode changed os.environ during the review"
    assert dict(os.environ) == before
    assert tree == [".git", "app.py"], tree


def test_large_synthetic_diffs_put_the_defect_past_the_cut_and_gate_must_inspect_it(
        tmp_path, monkeypatch):
    """Issue #436: seeded >60,000-char diffs with the defect after the cut,
    plus a large clean control — generated here, never committed."""
    path = "pkg/limits.py"
    seeded_diff, defect_line = _big_create_diff(path, defect=True)
    control_diff, _ = _big_create_diff(path, defect=False)
    defect_text = "return used > limit"
    import no_human.review.reviewer as reviewer_mod
    assert len(seeded_diff) > 60_000 and len(control_diff) > 60_000
    assert seeded_diff.index(defect_text) > reviewer_mod._DIFF_CAP
    file_len = sum(len(line) - 1 for line in seeded_diff.splitlines(True)
                   if line.startswith("+") and not line.startswith("+++"))
    assert file_len > reviewer_mod._FILES_CAP  # too big for a whole-file copy

    def verdict(repo_path, kw):
        # Stands in for a reviewer that opens every path the gate says it
        # MUST inspect — a finding is only possible from the committed tree,
        # because the prompt's diff no longer carries the defect.
        for rel in kw.get("required_inspections") or []:
            lines = (repo_path / rel).read_text(encoding="utf-8").splitlines()
            for n, line in enumerate(lines, 1):
                if defect_text in line:
                    item = ChecklistItem("inverted limit check", False,
                                         "comparison is inverted", file=rel, line=n)
                    return ReviewDecision(passed=False, checklist=[item])
        return ReviewDecision(passed=True, checklist=[ChecklistItem("ok", True, "ok")])

    record: list = []
    _stub_gate_session(monkeypatch, record, verdict=verdict)
    fn = rr._gate_reviewer_fn("ignored", {})
    seeded = _case(tmp_path, "seeded-large", seeded_diff, {
        "class": "logic", "file": path, "hunk_lines": [defect_line, defect_line],
        "keywords": ["inverted"]})
    control = _case(tmp_path, "control-large", control_diff, {"class": "control"})

    seeded_result = _run_one(tmp_path, seeded, fn)
    control_result = _run_one(tmp_path, control, fn)

    prompts = [(r[2], r[3]) for r in record if r[0] == "agent"]
    assert len(prompts) == 2
    for prompt, options in prompts:
        assert defect_text not in prompt  # cut from the diff, file omitted whole
        assert "is_within_limit" not in prompt
        assert options["required_inspections"] == [path]
    assert seeded_result.caught is True, seeded_result.reason
    assert control_result.clean_pass is True, control_result.reason


def test_run_all_gate_mode_discloses_a_non_default_reviewer_backend(tmp_path, monkeypatch):
    """`run_all(mode="gate")` wires the gate reviewer, and the report's model
    line names a non-default reviewer backend instead of the Claude tier."""
    case_dir = tmp_path / "cases" / "control-tiny"
    case_dir.mkdir(parents=True)
    (case_dir / "base.ref").write_text("synthetic\n")
    (case_dir / "change.diff").write_text(_SMALL_DIFF)
    (case_dir / "truth.json").write_text('{"class": "control"}')
    record: list = []
    _stub_gate_session(monkeypatch, record)
    config = {"llm": {"role_backends": {"reviewer": {"backend": "codex",
                                                     "model": "gpt-5-codex"}}}}

    report = asyncio.run(rr.run_all(REPO_ROOT, cases_dir=tmp_path / "cases",
                                    model="claude-opus-4-8", mode="gate",
                                    config_data=config, runs_dir=tmp_path / "runs"))

    assert [r[0] for r in record if r[0] in ("agent", "fast")] == ["agent"]
    assert report.model == "gpt-5-codex on codex (non-default reviewer)"
    assert "(gate mode)" in rr.render_report(report)


def _report(n_seeded: int, n_controls: int, false_alarms: int):
    results = [rr.CaseResult(case_id=f"s{i}", cls="logic", is_control=False,
                             outcome=rr.ReviewOutcome(status="FAIL"), caught=True)
               for i in range(n_seeded)]
    results += [rr.CaseResult(case_id=f"c{i}", cls="control", is_control=True,
                              outcome=rr.ReviewOutcome(status="PASS"),
                              clean_pass=i >= false_alarms)
                for i in range(n_controls)]
    return rr.RecallReport(results=results, model="m", run_date="2026-09-24", mode="gate")


def test_an_invalid_instrument_is_a_named_headline_refusal():
    try:
        rr.render_report(_report(4, 4, 3))
    except rr.HeadlineRefusedError as exc:
        assert "instrument invalid" in str(exc)
    else:
        raise AssertionError("render_report did not refuse")
    assert "gate mode" in rr.render_report(_report(4, 4, 2))


def test_nh_bench_report_prints_the_refusal_instead_of_a_traceback(monkeypatch):
    from no_human.cli import commands
    from no_human.cli.commands import cli

    async def fake_run_all(*_a, **kw):
        assert kw["mode"] == "gate" and kw["config_data"] == {"llm": {}}
        return _report(4, 4, 3)

    monkeypatch.setattr(rr, "run_all", fake_run_all)
    monkeypatch.setattr(commands, "_load_reviewer_recall_runner", lambda: (rr, REPO_ROOT))
    monkeypatch.setattr(commands, "_bootstrap", lambda **_: (
        SimpleNamespace(review_model="claude-opus-4-8", data={"llm": {}}), None))

    res = CliRunner().invoke(cli, ["bench", "report", "--reviewer-recall", "--mode", "gate"])

    assert isinstance(res.exception, SystemExit), res.exception
    assert res.exit_code == 1
    assert "recall headline refused" in res.output
    assert "instrument invalid" in res.output
