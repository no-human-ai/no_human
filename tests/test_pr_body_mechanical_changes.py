"""Mechanical (`_mechanical_changes_summary`) PR-body fallback tests.

When the coder's final message is REJECTED by `_is_non_report_summary` (a
status/deferral note, not a report), `_summary_section` used to render
`_NO_SUMMARY_BLOCK` verbatim — leaving the reader nothing but "read the
commits and diff yourself". These tests pin the new fallback: a MECHANICAL,
clearly-labeled summary built from `git log`/`git diff --numstat`, still
never pasting the rejected text, still refusing to invent prose.

The classifier itself (`_is_non_report_summary`) and the untouched-block text
are OUT OF SCOPE here — see `tests/test_pr_body_truthfulness.py` for those
pins, which this file does not modify.
"""

import re

import pytest

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.vcs.git import GitError, GitRepo

from .test_pr_body_truthfulness import (  # noqa: F401
    WAITING, _Commit, _Result, _git, _orch,
)


def _branch_repo(tmp_path, commits):
    """A real repo on `feat/x`, cut from `main`, with one commit per
    `(message, {path: content})` pair in `commits` — oldest first, matching
    how a coder's branch actually accumulates."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "seed.txt").write_text("seed\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "seed")
    _git(work, "checkout", "-qb", "feat/x")
    for msg, files in commits:
        for path, content in files.items():
            p = work / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", msg)
    return GitRepo(work)


def _changes_section(body: str) -> str:
    return body.split("## Changes\n", 1)[1].split("\n## ", 1)[0]


def _rejected_result() -> "_Result":
    result = _Result()
    result.final_text = WAITING
    return result


def test_a_rejected_final_message_yields_the_labeled_mechanical_summary(store, tmp_path):
    repo = _branch_repo(tmp_path, [
        ("add calc.py", {"calc.py": "def calc():\n    return 1\n"}),
        ("wire calc into main", {"calc.py": "def calc():\n    return 2\n"}),
    ])
    orch = _orch(store, tmp_path)
    body = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), _rejected_result(),
        repo=repo, base="main",
    )
    section = _changes_section(body)

    assert "Auto-generated summary" in section
    assert "add calc.py" in section
    assert "wire calc into main" in section
    assert "`calc.py`" in section
    assert re.search(r"\+\d+/-\d+", section)
    assert WAITING not in body
    assert "Nothing was written in its place" not in body


def test_generated_ledgers_are_excluded_but_a_real_file_is_present(store, tmp_path):
    repo = _branch_repo(tmp_path, [
        ("landing", {
            "RELEASE_MANIFEST.txt": "manifest\n",
            "EXPORT_CLASSIFICATION.txt": "classification\n",
            "docs/RELEASE_MANIFEST.txt": "not the real ledger\n",
            "src/real.py": "print('hi')\n",
        }),
    ])
    orch = _orch(store, tmp_path)
    body = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), _rejected_result(),
        repo=repo, base="main",
    )
    section = _changes_section(body)

    # Positive control: a real file in the same commit IS present, so the
    # absence of the ledger paths below is exclusion, not a broken numstat.
    assert "`src/real.py`" in section
    assert "RELEASE_MANIFEST.txt`" not in section
    assert "EXPORT_CLASSIFICATION.txt`" not in section
    assert "`docs/RELEASE_MANIFEST.txt`" not in section
    assert "(derived ledger files omitted)" in section


def test_a_report_shaped_summary_renders_byte_identically_with_and_without_repo(store, tmp_path):
    repo = _branch_repo(tmp_path, [("add calc.py", {"calc.py": "1\n"})])
    orch = _orch(store, tmp_path)
    result = _Result()  # report-shaped final_text, unchanged from truthfulness fixtures

    with_repo = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), result,
        repo=repo, base="main",
    )
    without_repo = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)

    # Only the ticket/evidence machinery differs by repo-path plumbing, never
    # the Changes section itself — so compare `_summary_section` directly,
    # which is the byte-identical regression pin the plan calls for.
    assert orch._summary_section(result) == orch._summary_section(
        result, repo=repo, base="main")
    assert Orchestrator._MECHANICAL_LABEL not in with_repo
    assert Orchestrator._MECHANICAL_LABEL not in without_repo


def test_more_than_ten_commits_elides_with_an_honest_tail(store, tmp_path):
    commits = [(f"commit {i}", {"f.py": f"content {i}\n"}) for i in range(12)]
    repo = _branch_repo(tmp_path, commits)
    orch = _orch(store, tmp_path)
    body = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), _rejected_result(),
        repo=repo, base="main",
    )
    section = _changes_section(body)

    for i in range(10):
        assert f"commit {i}" in section
    assert "commit 10" not in section
    assert "commit 11" not in section
    assert "… and 2 more commits" in section


def test_more_than_fifteen_files_elides_with_an_honest_tail(store, tmp_path):
    files = {f"file_{i:02d}.py": "x\n" for i in range(18)}
    repo = _branch_repo(tmp_path, [("add many files", files)])
    orch = _orch(store, tmp_path)
    body = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), _rejected_result(),
        repo=repo, base="main",
    )
    section = _changes_section(body)

    for i in range(15):
        assert f"`file_{i:02d}.py`" in section
    for i in range(15, 18):
        assert f"file_{i:02d}.py" not in section
    assert "… and 3 more files" in section


def _oversized_repo(tmp_path):
    # Each path component stays well under the 255-byte filesystem limit,
    # but nesting two of them makes the RENDERED line (unlimited by
    # `_inline_cell(path, None)`) long enough that 15 of them alone blow the
    # 6000-char body budget — while staying close enough to the budget that
    # the re-render still has room to keep SOME files, so the elision tail
    # actually has to recompute rather than dropping the whole block.
    seg = "d" * 120
    files = {f"{seg}/{seg}/{'f' * 120}_{i}.py": "x\n" for i in range(15)}
    return _branch_repo(tmp_path, [("add wide files", files)])


def test_an_oversized_mechanical_summary_truncates_through_the_body_budget(store, tmp_path):
    repo = _oversized_repo(tmp_path)
    orch = _orch(store, tmp_path)
    body = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), _rejected_result(),
        repo=repo, base="main",
    )
    section = _changes_section(body)

    assert "trimmed further to keep the PR body under its size budget" in body
    assert "Auto-generated summary" in section
    assert "No implementation summary was produced" in section

    file_lines = re.findall(r"^- `.*` \+\d+/-\d+$", section, flags=re.MULTILINE)
    m = re.search(r"… and (\d+) more files", section)
    hidden = int(m.group(1)) if m else 0
    assert len(file_lines) + hidden == 15, (
        "the elision tail must count every dropped file, including ones the "
        "budget trim dropped after the initial 15-file cap already passed"
    )
    assert hidden > 0, "an oversized summary must actually drop something"
    # No mid-word cut: every surviving line is a COMPLETE rendered line.
    for line in section.splitlines():
        if line.startswith("- `"):
            assert re.match(r"^- `.*` \+\d+/-\d+$", line), line


def test_a_direct_max_visible_pin_stays_within_budget(store, tmp_path):
    repo = _oversized_repo(tmp_path)
    orch = _orch(store, tmp_path)
    marker = "_(trimmed further to keep the PR body under its size budget)_"
    section = orch._summary_section(
        _rejected_result(), max_visible=300, trim_marker=marker,
        repo=repo, base="main",
    )
    assert len(section) <= 300 + len(marker)
    assert "Auto-generated summary" in section


def test_no_absolute_local_paths_reach_the_section(store, tmp_path):
    repo = _branch_repo(tmp_path, [
        ("fix calc.py per review, moved from /Users/someone/git/x/calc.py",
         {"calc.py": "1\n"}),
    ])
    orch = _orch(store, tmp_path)
    body = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), _rejected_result(),
        repo=repo, base="main",
    )
    section = _changes_section(body)

    assert "/Users/" not in section
    assert str(tmp_path) not in section
    assert "[path]" in section
    assert "`calc.py`" in section  # the real numstat entry, untouched


def test_without_a_repo_the_unchanged_absence_block_still_renders(store, tmp_path):
    orch = _orch(store, tmp_path)
    body = orch._pr_body(
        Task.new("t", repo_path="/r"), _Commit(), _rejected_result())
    assert Orchestrator._NO_SUMMARY_BLOCK in body


def test_an_unresolvable_base_falls_back_to_the_absence_block(store, tmp_path):
    repo = _branch_repo(tmp_path, [("add x", {"x.py": "1\n"})])
    orch = _orch(store, tmp_path)
    body = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), _rejected_result(),
        repo=repo, base="no-such-branch",
    )
    assert Orchestrator._NO_SUMMARY_BLOCK in body


def test_a_git_failure_during_the_mechanical_summary_never_raises(store, tmp_path, monkeypatch):
    repo = _branch_repo(tmp_path, [("add x", {"x.py": "1\n"})])

    def boom(*a, **k):
        raise GitError("boom")

    monkeypatch.setattr(repo, "_run", boom)
    orch = _orch(store, tmp_path)
    body = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), _rejected_result(),
        repo=repo, base="main",
    )
    assert Orchestrator._NO_SUMMARY_BLOCK in body


def test_the_budget_rerender_does_not_reshell_to_git(store, tmp_path):
    repo = _oversized_repo(tmp_path)
    orch = _orch(store, tmp_path)
    calls: list = []
    orig_run = repo._run

    def spy(*args, **kwargs):
        calls.append(args[0] if args else None)
        return orig_run(*args, **kwargs)

    repo._run = spy
    body = orch._pr_body(
        Task.new("t", repo_path=str(repo.path)), _Commit(), _rejected_result(),
        repo=repo, base="main",
    )
    assert "trimmed further" in body  # confirms the budget rerender path fired
    assert calls.count("log") == 1, (
        f"the budget rerender must reuse the already-computed summary, not "
        f"re-shell to git; log calls: {calls}"
    )
    assert calls.count("diff") == 1, (
        f"the budget rerender must reuse the already-computed summary, not "
        f"re-shell to git; diff calls: {calls}"
    )
