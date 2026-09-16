"""Acceptance criterion 5 of the recut fix: measure, don't assume, how many
live tasks are stuck in the loop this whole feature exists to end.

`core/diverged_audit.py` (`audit_diverged_tasks`) classifies every live
task's tracked branch(es) against their own remote tip; `cli/commands.py`'s
`nh diverged` prints the count. Both are read-only by construction — no
push, no fetch into a tracking ref, no write to the task store — which this
file pins directly rather than trusting the module docstring's claim.
"""
from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

import no_human.cli.commands as cmd_mod
import no_human.vcs.git as git_mod
from no_human.cli.commands import cli
from no_human.core.diverged_audit import AuditReport, AuditRow, audit_diverged_tasks
from no_human.core.task import Task, TaskStatus
from no_human.vcs.git import GitRepo


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _bare(tmp_path, name):
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                    check=True, capture_output=True, text=True)
    return bare


def _work(tmp_path, name):
    work = tmp_path / name
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    return work


async def _seed_task(store, *, repo_path: str, pr_branch: str) -> Task:
    task = Task.new("Recut audit test", repo_path=repo_path)
    task.context = {"pr_branch": pr_branch}
    await store.create_task(task)
    return task  # default status is PENDING — a LIVE_STATUSES member


# ---------------------------------------------------------------------------
# Test 8 (AC5): three live tasks, three states, counted correctly.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_counts_diverged_live_tasks(store, tmp_path):
    # up_to_date: pushed, remote == local.
    up_repo = _work(tmp_path, "up")
    up_bare = _bare(tmp_path, "up.git")
    _git(up_repo, "remote", "add", "origin", str(up_bare))
    _git(up_repo, "checkout", "-q", "-b", "b-up")
    _git(up_repo, "push", "-q", "-u", "origin", "b-up")
    up_sha = _git(up_repo, "rev-parse", "b-up")

    # diverged: pushed, then rewritten locally without re-pushing.
    div_repo = _work(tmp_path, "div")
    div_bare = _bare(tmp_path, "div.git")
    _git(div_repo, "remote", "add", "origin", str(div_bare))
    _git(div_repo, "checkout", "-q", "-b", "b-div")
    (div_repo / "pr.py").write_text("v1\n")
    _git(div_repo, "add", "-A")
    _git(div_repo, "commit", "-q", "-m", "pr work")
    _git(div_repo, "push", "-q", "-u", "origin", "b-div")
    div_remote_sha = _git(div_repo, "rev-parse", "b-div")
    (div_repo / "pr.py").write_text("v1, rewritten\n")
    _git(div_repo, "add", "-A")
    _git(div_repo, "commit", "-q", "--amend", "-m", "pr work (rewritten)")
    div_local_sha = _git(div_repo, "rev-parse", "b-div")
    assert div_local_sha != div_remote_sha

    # unknown: local branch, never pushed anywhere.
    unk_repo = _work(tmp_path, "unk")
    unk_bare = _bare(tmp_path, "unk.git")
    _git(unk_repo, "remote", "add", "origin", str(unk_bare))
    _git(unk_repo, "checkout", "-q", "-b", "b-unk")

    t_up = await _seed_task(store, repo_path=str(up_repo), pr_branch="b-up")
    t_div = await _seed_task(store, repo_path=str(div_repo), pr_branch="b-div")
    t_unk = await _seed_task(store, repo_path=str(unk_repo), pr_branch="b-unk")

    report = await audit_diverged_tasks(store, {})

    assert report.scanned == 3, report.scanned
    assert report.counts == {"up_to_date": 1, "diverged": 1, "unknown": 1}, report.counts
    assert report.diverged_count == 1

    div_rows = [r for r in report.rows if r.task_id == t_div.id]
    assert len(div_rows) == 1, div_rows
    div_row = div_rows[0]
    assert div_row.state == "diverged"
    assert div_row.local_sha == div_local_sha
    assert div_row.remote_sha == div_remote_sha

    up_rows = [r for r in report.rows if r.task_id == t_up.id]
    assert up_rows and up_rows[0].state == "up_to_date"
    assert up_rows[0].local_sha == up_sha
    assert up_rows[0].remote_sha == up_sha

    unk_rows = [r for r in report.rows if r.task_id == t_unk.id]
    assert unk_rows and unk_rows[0].state == "unknown"


# ---------------------------------------------------------------------------
# Test 9: the audit is pure observation — no push, no store write.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audit_never_writes_to_the_remote_or_the_db(store, tmp_path, monkeypatch):
    div_repo = _work(tmp_path, "div2")
    div_bare = _bare(tmp_path, "div2.git")
    _git(div_repo, "remote", "add", "origin", str(div_bare))
    _git(div_repo, "checkout", "-q", "-b", "b-div2")
    (div_repo / "pr.py").write_text("v1\n")
    _git(div_repo, "add", "-A")
    _git(div_repo, "commit", "-q", "-m", "pr work")
    _git(div_repo, "push", "-q", "-u", "origin", "b-div2")
    (div_repo / "pr.py").write_text("v1, rewritten\n")
    _git(div_repo, "add", "-A")
    _git(div_repo, "commit", "-q", "--amend", "-m", "pr work (rewritten)")

    await _seed_task(store, repo_path=str(div_repo), pr_branch="b-div2")

    real_run = subprocess.run
    calls: list[list[str]] = []

    def _spy(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(git_mod.subprocess, "run", _spy)

    def _boom_update_task(*a, **k):
        raise AssertionError("audit must never call store.update_task")

    def _boom_set_status(*a, **k):
        raise AssertionError("audit must never call store.set_status")

    monkeypatch.setattr(store, "update_task", _boom_update_task)
    monkeypatch.setattr(store, "set_status", _boom_set_status)

    report = await audit_diverged_tasks(store, {})

    assert report.diverged_count == 1, report.counts
    assert calls, "expected the audit to actually shell out to git"
    for argv in calls:
        assert "push" not in argv, f"the audit pushed: {argv}"
        assert not any(a.startswith("--force") or a == "-f" for a in argv), argv


# ---------------------------------------------------------------------------
# Test 10: `nh diverged` prints the count and exits 0.
# ---------------------------------------------------------------------------

def test_nh_diverged_prints_the_count(tmp_path, monkeypatch):
    class _Cfg:
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = tmp_path / "nh.db"

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)

    fake_report = AuditReport(
        rows=[
            AuditRow(task_id="t1", title="Some task", branch="no-human/abc12345",
                     local_sha="a" * 40, remote_sha="b" * 40, state="diverged"),
        ],
        counts={"diverged": 1, "up_to_date": 2},
        scanned=3,
    )

    async def _fake_audit(store, config, **kw):
        return fake_report

    import no_human.core.diverged_audit as audit_mod
    monkeypatch.setattr(audit_mod, "audit_diverged_tasks", _fake_audit)

    result = CliRunner().invoke(cli, ["diverged"])

    assert result.exit_code == 0, result.output
    assert "1 task(s) diverged of 3 live task(s) scanned" in result.output, result.output
    assert "no-human/abc12345" in result.output, result.output
