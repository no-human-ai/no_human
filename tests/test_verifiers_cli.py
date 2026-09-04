"""Tests for `nh verifiers list/add/check/propose` (re-home of the private
PR #811 CLI, reimplemented against the current tree — see
`.no_human/PLAN.md`).

CLI commands drive asyncio.run() internally, so integration tests must be
synchronous — see tests/test_task_config_cli.py for the established
`_seed_task`/`_make_runner` pattern this file reuses. `NO_HUMAN_HOME` is
always monkeypatched to a tmp dir so a developer's real `~/.no_human/
verifiers.yaml` never leaks into a test's merged verifier set.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import no_human.cli.verifiers_cmd as vcmd
from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task
from no_human.review.verifiers import validate_entry


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _runner(tmp_path: Path, monkeypatch, *, db_path: Path | None = None) -> CliRunner:
    monkeypatch.setattr(vcmd, "NO_HUMAN_HOME", tmp_path / "home")
    if db_path is not None:
        class _Cfg:
            data: dict = {}

            def get(self, key, default=None):
                return self.data.get(key, default)

            def __getitem__(self, key):
                return self.data[key]

        _Cfg.db_path = db_path
        monkeypatch.setattr(vcmd, "load_config", lambda: _Cfg())
    return CliRunner()


def _write_verifiers(repo: Path, text: str) -> None:
    (repo / ".no_human").mkdir(parents=True, exist_ok=True)
    (repo / ".no_human" / "verifiers.yaml").write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _git_repo_with_two_commits(tmp_path: Path, *, verifiers_yaml: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty",
         "-m", "init")
    _write_verifiers(repo, verifiers_yaml)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "readme.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "seed")
    (repo / "src" / "a.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "change a.py")
    return repo


_ONE_RULE = (
    "verifiers:\n"
    "  - id: rule-one\n"
    "    statement: First rule statement.\n"
    "    paths: src/**/*.py\n"
)


def _seed_task(db_path: Path, *, review_checklist=None, draft_review_comments=None,
               title="Test task") -> str:
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path="/tmp/repo")
            if draft_review_comments is not None:
                t.context = {"draft_review_comments": draft_review_comments}
            await s.create_task(t)
            if review_checklist is not None:
                attempt_id = await s.create_attempt(t.id, 1)
                await s.update_attempt(
                    attempt_id, review_checklist=json.dumps(review_checklist)
                )
            return t.id
    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# nh verifiers list                                                           #
# --------------------------------------------------------------------------- #

def test_list_prints_configured_verifiers(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write_verifiers(repo, _ONE_RULE)
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, ["verifiers", "list", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "rule-one" in result.output


def test_list_json_emits_machine_readable_output(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write_verifiers(repo, _ONE_RULE)
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, ["verifiers", "list", "--repo", str(repo), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["problems"] == []
    ids = [v["id"] for v in payload["verifiers"]]
    assert ids == ["rule-one"]


def test_list_with_no_config_is_a_clean_empty_state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, ["verifiers", "list", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "no verifiers configured" in result.output.lower()


def test_list_surfaces_problems_but_still_exits_0(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write_verifiers(repo, "verifiers:\n  - id: 'Not Valid!!'\n    statement: x\n    paths: a\n")
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, ["verifiers", "list", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "invalid id" in result.output.lower()


# --------------------------------------------------------------------------- #
# nh verifiers add                                                            #
# --------------------------------------------------------------------------- #

def test_add_creates_file_when_absent(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, [
        "verifiers", "add", "--repo", str(repo),
        "--id", "no-bare-except", "--statement", "No bare except clauses.",
        "--path", "src/**/*.py",
    ])

    assert result.exit_code == 0, result.output
    text = (repo / ".no_human" / "verifiers.yaml").read_text()
    assert "id: no-bare-except" in text


def test_add_appends_to_existing_file_verbatim(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    seed = "# hand-written comment\nverifiers:\n" + _ONE_RULE.split("\n", 1)[1]
    _write_verifiers(repo, seed)
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, [
        "verifiers", "add", "--repo", str(repo),
        "--id", "rule-two", "--statement", "Second rule.",
        "--path", "docs/*",
    ])

    assert result.exit_code == 0, result.output
    text = (repo / ".no_human" / "verifiers.yaml").read_text()
    assert "# hand-written comment" in text
    assert "id: rule-one" in text
    assert "id: rule-two" in text


def test_add_refuses_a_duplicate_id(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write_verifiers(repo, _ONE_RULE)
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, [
        "verifiers", "add", "--repo", str(repo),
        "--id", "rule-one", "--statement", "Duplicate.",
        "--path", "src/**/*.py",
    ])

    assert result.exit_code != 0
    assert "already defined" in result.output.lower()
    text = (repo / ".no_human" / "verifiers.yaml").read_text()
    assert text.count("id: rule-one") == 1


def test_add_refuses_an_invalid_id_and_an_oversize_statement(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _runner(tmp_path, monkeypatch)

    bad_id = runner.invoke(cli, [
        "verifiers", "add", "--repo", str(repo),
        "--id", "Not Valid!!", "--statement", "x", "--path", "src/*.py",
    ])
    assert bad_id.exit_code != 0
    assert not (repo / ".no_human" / "verifiers.yaml").exists()

    oversize = runner.invoke(cli, [
        "verifiers", "add", "--repo", str(repo),
        "--id", "ok-id", "--statement", "x" * 601, "--path", "src/*.py",
    ])
    assert oversize.exit_code != 0
    assert not (repo / ".no_human" / "verifiers.yaml").exists()


def test_add_refuses_an_unknown_severity(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, [
        "verifiers", "add", "--repo", str(repo),
        "--id", "ok-id", "--statement", "x", "--path", "src/*.py",
        "--severity", "blocker",
    ])

    assert result.exit_code == 1, result.output
    assert not (repo / ".no_human" / "verifiers.yaml").exists()


def test_add_writes_to_global_with_global_flag(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, [
        "verifiers", "add", "--repo", str(repo), "--global",
        "--id", "global-rule", "--statement", "Global rule.", "--path", "**/*.py",
    ])

    assert result.exit_code == 0, result.output
    assert not (repo / ".no_human" / "verifiers.yaml").exists()
    global_file = tmp_path / "home" / "verifiers.yaml"
    assert "id: global-rule" in global_file.read_text()


def test_add_restores_the_file_if_verification_fails(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write_verifiers(repo, _ONE_RULE)
    original = (repo / ".no_human" / "verifiers.yaml").read_text()
    runner = _runner(tmp_path, monkeypatch)

    def _broken_reload(*args, **kwargs):
        from no_human.review.verifiers import LoadReport
        return LoadReport(verifiers=[], problems=["forced failure"])

    monkeypatch.setattr(vcmd, "load_verifiers", _broken_reload)

    result = runner.invoke(cli, [
        "verifiers", "add", "--repo", str(repo),
        "--id", "rule-two", "--statement", "Second rule.", "--path", "docs/*",
    ])

    assert result.exit_code != 0
    assert (repo / ".no_human" / "verifiers.yaml").read_text() == original


def test_add_succeeds_despite_an_unrelated_pre_existing_bad_entry(tmp_path, monkeypatch):
    """A malformed entry already in the file (bad id, in this case) must not
    make write-then-verify roll back an otherwise-valid `add` to the same
    file: the loader's problem line for that entry is present both before
    and after the write, so it must not be mistaken for a new problem caused
    by the write."""
    repo = tmp_path / "repo"
    _write_verifiers(
        repo,
        "verifiers:\n"
        "  - id: Bad Id\n"
        "    statement: This entry has always been broken.\n"
        "    paths: src/**/*.py\n",
    )
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, [
        "verifiers", "add", "--repo", str(repo),
        "--id", "rule-two", "--statement", "Second rule.", "--path", "docs/*",
    ])

    assert result.exit_code == 0, result.output
    text = (repo / ".no_human" / "verifiers.yaml").read_text()
    assert "id: Bad Id" in text
    assert "id: rule-two" in text
    report = vcmd.load_verifiers(repo, home=tmp_path / "home")
    assert {v.id for v in report.verifiers} == {"rule-two"}
    assert any("Bad Id" in p for p in report.problems)


# --------------------------------------------------------------------------- #
# nh verifiers check                                                          #
# --------------------------------------------------------------------------- #

def test_check_reports_selected_and_not_selected(tmp_path, monkeypatch):
    yaml_text = (
        "verifiers:\n"
        "  - id: py-rule\n"
        "    statement: Python files rule.\n"
        "    paths: src/**/*.py\n"
        "  - id: docs-rule\n"
        "    statement: Docs rule.\n"
        "    paths: docs/*\n"
    )
    repo = _git_repo_with_two_commits(tmp_path, verifiers_yaml=yaml_text)
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, ["verifiers", "check", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "py-rule" in result.output and "selected" in result.output
    assert "docs-rule: " in result.output
    assert "not selected" in result.output


def test_check_would_fail_closed_when_no_matching_hunks(tmp_path, monkeypatch):
    # `select()` (real, driven by the actual changed-file list from git) and
    # `filter_diff` (monkeypatched) are independent steps in check_cmd's own
    # dispatch — select genuinely selects "everything-rule" because
    # `src/**/*.py` matches the real changed path `src/a.py`, and filter_diff
    # is forced to report no matching hunks for it, so this exercises
    # check_cmd's own "selected but filter_diff found nothing" branch
    # directly rather than depending on a git corner case to reproduce it.
    # filter_diff's own hunk-matching behaviour has its own coverage in
    # tests/test_verifiers.py.
    yaml_text = (
        "verifiers:\n"
        "  - id: everything-rule\n"
        "    statement: Applies to anything.\n"
        "    paths:\n"
        "      - src/**/*.py\n"
        "      - docs/*\n"
    )
    repo = _git_repo_with_two_commits(tmp_path, verifiers_yaml=yaml_text)
    runner = _runner(tmp_path, monkeypatch)
    monkeypatch.setattr(vcmd, "filter_diff", lambda diff_text, paths: ("", []))

    result = runner.invoke(cli, ["verifiers", "check", "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "everything-rule" in result.output
    assert "would fail closed as no_verdict" in result.output.lower()


def test_check_exits_nonzero_on_a_malformed_config(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write_verifiers(repo, "verifiers:\n  - id: 'bad id'\n    statement: x\n    paths: a\n")
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, ["verifiers", "check", "--repo", str(repo), "--path", "a"])

    assert result.exit_code == 1
    assert "invalid id" in result.output.lower()


def test_check_exits_nonzero_with_zero_verifiers_configured(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, ["verifiers", "check", "--repo", str(repo), "--path", "a"])

    assert result.exit_code == 1


def test_check_exits_nonzero_on_unresolvable_ref(tmp_path, monkeypatch):
    repo = _git_repo_with_two_commits(tmp_path, verifiers_yaml=_ONE_RULE)
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, [
        "verifiers", "check", "--repo", str(repo), "--against", "not-a-real-ref-xyz",
    ])

    assert result.exit_code == 1
    assert "cannot resolve ref" in result.output.lower()


def test_check_with_explicit_paths_needs_no_git(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _write_verifiers(repo, _ONE_RULE)
    runner = _runner(tmp_path, monkeypatch)

    result = runner.invoke(cli, [
        "verifiers", "check", "--repo", str(repo), "--path", "src/foo.py",
    ])

    assert result.exit_code == 0, result.output
    assert "rule-one" in result.output


def test_check_makes_no_model_call(tmp_path, monkeypatch):
    repo = _git_repo_with_two_commits(tmp_path, verifiers_yaml=_ONE_RULE)
    runner = _runner(tmp_path, monkeypatch)

    def _boom(*args, **kwargs):
        raise AssertionError("check must never build a verifier prompt")

    monkeypatch.setattr("no_human.review.verifiers.build_prompt", _boom)
    monkeypatch.setattr("no_human.review.verifiers.run_verifiers", _boom)

    result = runner.invoke(cli, ["verifiers", "check", "--repo", str(repo)])

    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------- #
# nh verifiers propose                                                        #
# --------------------------------------------------------------------------- #

def _blocking_checklist(*, label="payment code lacks a try/except", file="src/pay.py",
                        comment="Payment code lacks error handling.", severity="high"):
    return {
        "items": [
            {
                "label": label, "passed": False, "file": file, "line": 12,
                "comment": comment, "severity": severity,
            },
        ],
    }


def test_propose_renders_yaml_from_a_failed_review_checklist(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "test.db"
    task_id = _seed_task(db, review_checklist=_blocking_checklist())
    runner = _runner(tmp_path, monkeypatch, db_path=db)

    result = runner.invoke(cli, ["verifiers", "propose", task_id, "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "statement: |-" in result.output
    assert "src/pay.py" in result.output
    assert not (repo / ".no_human" / "verifiers.yaml").exists()


def test_propose_writes_nothing_without_apply(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "test.db"
    task_id = _seed_task(db, review_checklist=_blocking_checklist())
    runner = _runner(tmp_path, monkeypatch, db_path=db)

    result = runner.invoke(cli, ["verifiers", "propose", task_id, "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "--apply" in result.output
    assert not (repo / ".no_human" / "verifiers.yaml").exists()


def test_propose_apply_appends_and_is_idempotent(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "test.db"
    task_id = _seed_task(db, review_checklist=_blocking_checklist())
    runner = _runner(tmp_path, monkeypatch, db_path=db)

    first = runner.invoke(cli, ["verifiers", "propose", task_id, "--repo", str(repo), "--apply"])
    assert first.exit_code == 0, first.output
    text_after_first = (repo / ".no_human" / "verifiers.yaml").read_text()
    assert text_after_first.count("- id:") == 1

    second = runner.invoke(cli, ["verifiers", "propose", task_id, "--repo", str(repo), "--apply"])
    assert second.exit_code == 0, second.output
    assert "already defined" in second.output.lower()
    text_after_second = (repo / ".no_human" / "verifiers.yaml").read_text()
    assert text_after_second.count("- id:") == 1


def test_propose_apply_exits_nonzero_when_a_write_fails(tmp_path, monkeypatch):
    # A write failure must be detectable from the exit code alone — a caller
    # scripting `nh verifiers propose --apply` should never have to scrape
    # stdout for the red "failed" line to know a candidate was rolled back.
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "test.db"
    task_id = _seed_task(db, review_checklist=_blocking_checklist())
    runner = _runner(tmp_path, monkeypatch, db_path=db)
    monkeypatch.setattr(vcmd, "_write_then_verify", lambda *a, **k: "forced failure")

    result = runner.invoke(cli, ["verifiers", "propose", task_id, "--repo", str(repo), "--apply"])

    assert result.exit_code != 0
    assert "failed" in result.output.lower()
    assert not (repo / ".no_human" / "verifiers.yaml").exists()


def test_propose_skips_rule_labelled_findings(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "test.db"
    checklist = _blocking_checklist(label="rule:some-verifier")
    task_id = _seed_task(db, review_checklist=checklist)
    runner = _runner(tmp_path, monkeypatch, db_path=db)

    result = runner.invoke(cli, ["verifiers", "propose", task_id, "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "verifier's own verdict" in result.output.lower() or "skipped" in result.output.lower()
    assert "statement: |-" not in result.output


def test_propose_skips_findings_with_no_file_and_says_why(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "test.db"
    checklist = _blocking_checklist(file="")
    task_id = _seed_task(db, review_checklist=checklist)
    runner = _runner(tmp_path, monkeypatch, db_path=db)

    result = runner.invoke(cli, ["verifiers", "propose", task_id, "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "no file cited" in result.output.lower()
    assert "nothing to propose" in result.output.lower()


def test_propose_falls_back_to_draft_review_comments(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "test.db"
    drafts = [{
        "file": "src/pay.py", "line": 5, "comment": "Missing input validation.",
        "severity": "high", "posted": False,
    }]
    task_id = _seed_task(db, draft_review_comments=drafts)
    runner = _runner(tmp_path, monkeypatch, db_path=db)

    result = runner.invoke(cli, ["verifiers", "propose", task_id, "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "src/pay.py" in result.output
    assert "Missing input validation" in result.output


def test_propose_no_checklist_is_a_clean_message(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "test.db"
    task_id = _seed_task(db)
    runner = _runner(tmp_path, monkeypatch, db_path=db)

    result = runner.invoke(cli, ["verifiers", "propose", task_id, "--repo", str(repo)])

    assert result.exit_code == 0, result.output
    assert "no review checklist yet" in result.output.lower()


def test_propose_unknown_task_exits_nonzero(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = tmp_path / "test.db"
    _seed_task(db)
    runner = _runner(tmp_path, monkeypatch, db_path=db)

    result = runner.invoke(cli, ["verifiers", "propose", "deadbeef", "--repo", str(repo)])

    assert result.exit_code != 0
    assert "no task matching" in result.output.lower()


# --------------------------------------------------------------------------- #
# Regression guards                                                           #
# --------------------------------------------------------------------------- #

def test_verifiers_group_is_registered():
    assert "verifiers" in cli.commands
    sub = cli.commands["verifiers"]
    assert set(sub.commands) == {"list", "add", "check", "propose"}


def test_validate_entry_delegates_to_the_loader():
    entry = {"id": "ok-id", "statement": "A statement.", "paths": ["src/*.py"]}
    verifier, problem = validate_entry(entry, origin="unit-test")
    assert problem is None
    assert verifier is not None
    assert verifier.id == "ok-id"
    assert verifier.source_file == "unit-test"

    bad_verifier, bad_problem = validate_entry({"id": "Not Valid!!"}, origin="unit-test")
    assert bad_verifier is None
    assert "invalid id" in (bad_problem or "").lower()
