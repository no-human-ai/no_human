"""Tests for the Phase 1 human-action CLI verbs (nh approve / reject / diff / review / logs).

CLI commands call asyncio.run() internally, so tests must be synchronous.
Each helper opens its own fresh Store connection inside asyncio.run() so the
aiosqlite connection is never reused across event loops.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import signal
import socket
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import uvicorn
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.cli.pool_probe import (
    POOL_BAD_BODY, POOL_HTTP_ERROR, POOL_LIVE, POOL_NO_SCHEDULER, POOL_REFUSED,
    POOL_TIMEOUT, POOL_UNREACHABLE, PROBE_TIMEOUT_S, PoolProbe, _pool_note,
    _probe_pool,
)
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

# The `nh start` poller tests reach ``config.load_env_var``, which reads the
# operator's real ``~/.no_human/.env`` BEFORE the process env. Requested by
# NAME through `usefixtures` — never an autouse marker; see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Helpers — each opens a fresh Store connection in its own asyncio.run()      #
# --------------------------------------------------------------------------- #

def _seed_task(db_path: Path, status: TaskStatus, *, title="Test task",
               task_id: str | None = None) -> str:
    """Seed one task. `task_id` pins the id instead of taking `Task.new`'s uuid.

    Pin it whenever the test asserts on rendered output. A uuid is random data
    printed into the same frame the assertions read, and a substring assertion
    cannot tell it apart from the value under test - which is exactly how the
    agents-table test below started failing at random.
    """
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path="/tmp/repo")
            if task_id is not None:
                t.id = task_id
            await s.create_task(t)
            if status is TaskStatus.DONE:
                await s.set_status(t, status, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            else:
                await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def _seed_attempt(db_path: Path, task_id: str, **fields) -> str:
    async def _go():
        async with Store(db_path) as s:
            aid = await s.create_attempt(task_id, 1)
            if fields:
                await s.update_attempt(aid, **fields)
            return aid
    return asyncio.run(_go())


def _get_task(db_path: Path, task_id: str) -> Task:
    async def _go():
        async with Store(db_path) as s:
            return await s.find_task(task_id)
    return asyncio.run(_go())


def _list_events(db_path: Path, task_id: str) -> list[dict]:
    async def _go():
        async with Store(db_path) as s:
            return await s.list_events(task_id)
    return asyncio.run(_go())


def _table_rows(output: str) -> list[dict[str, str]]:
    """Parse a rendered `rich` Table into one {column: cell} dict per data row.

    A cell is the unit a table assertion actually means. Asserting on a
    substring of the whole frame instead is what made the agents test flaky:
    every column's text is in that one string, including the random uuid.
    """
    header: list[str] = []
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if "┃" in stripped and not header:
            header = [c.strip() for c in stripped.strip("┃").split("┃")]
        elif "│" in stripped and header:
            cells = [c.strip() for c in stripped.strip("│").split("│")]
            if len(cells) == len(header):
                rows.append(dict(zip(header, cells)))
    return rows


def _make_runner(path: Path, monkeypatch) -> CliRunner:
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = path  # assign after class def — class body can't see enclosing locals

    # Patch where the names are USED (commands.py has `from ..config import load_config`)
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    # `nh status` asks the running server for the real pool width, which is a
    # socket to 127.0.0.1:8420 — the operator's own install answers it on a dev
    # box. Default it to "no server" so these tests read a fixed number; the
    # tests that are ABOUT that width stub the HTTP call itself. Same reason
    # test_task_lifecycle stubs `_server_owns_worker`. `status` calls
    # `_probe_pool` directly (not `_running_pool_stats`), so THIS is the name
    # that must be patched or `nh status` opens a real socket on a dev box.
    monkeypatch.setattr(
        cmd_mod, "_probe_pool",
        lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED))
    return CliRunner()


# --------------------------------------------------------------------------- #
# nh approve                                                                   #
# --------------------------------------------------------------------------- #

def test_approve_awaiting_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_attempt(db, task_id, pr_url="https://example.com/pr/1")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "approved" in result.output.lower()
    assert "https://example.com/pr/1" in result.output

    refreshed = _get_task(db, task_id)
    assert refreshed.context.get("approved_at") is not None


def test_approve_completes_an_already_satisfied_task(tmp_path, monkeypatch):
    """PR #101 review HIGH: an already-satisfied claim has no PR — 'merge it
    in your git host' is a dead end. Approval IS the confirmation → DONE."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)

    async def _ctx():
        async with Store(db) as s:
            await s.merge_context(task_id, {
                "already_satisfied_report":
                    "ALREADY-SATISFIED\nCRITERION: x — MET — evidence: a.py:1",
                # Satisfying commit is reachable from origin/base — truly
                # nothing to land, so approve marks DONE with zero changes
                # (AC2). Without this the new landing classifier would
                # re-derive from git and find no repo at the seeded
                # `repo_path`, refusing to complete the task.
                "already_satisfied_landing": {
                    "on_base": True, "sha": "deadbeef", "branch": "",
                    "ship_ref": "origin/main",
                },
            })
    asyncio.run(_ctx())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "already satisfied" in result.output.lower()
    assert "merge the pr" not in result.output.lower()
    refreshed = _get_task(db, task_id)
    assert refreshed.status is TaskStatus.DONE
    assert refreshed.context.get("approved_at") is not None
    events = _list_events(db, task_id)
    assert any(e.get("kind") == "approved_already_satisfied" for e in events)


def test_approve_wrong_status(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code != 0
    output = result.output.lower()
    assert "not awaiting_approval" in output or "cannot approve" in output


def _git(repo, *args, check=True):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


def _git_out(repo, *args) -> str:
    return _git(repo, *args).stdout.strip()


def _make_landed_repo(tmp_path) -> Path:
    repo = tmp_path / "landed_repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _seed_landed_task(db_path: Path, repo_path: Path, *,
                      status=TaskStatus.AWAITING_APPROVAL,
                      attempt_branch: str | None = None,
                      attempt_commit_sha: str = "") -> str:
    """`attempt_branch` seeds an attempt row (branch_name/commit_sha) so a
    `status=TaskStatus.FAILED` task's content is locatable the same way a
    budget-exhausted pre-PR task's is — via `Store.latest_attempt_branch`,
    not `context["pr_branch"]` (left blank here on purpose)."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("landed-check", repo_path=str(repo_path))
            t.context = {"base_branch": "main", "pr_branch": ""}
            await s.create_task(t)
            if attempt_branch is not None:
                attempt_id = await s.create_attempt(t.id, 1)
                await s.update_attempt(
                    attempt_id, branch_name=attempt_branch,
                    commit_sha=attempt_commit_sha, status="failed",
                    failure_reason="BUDGET_EXHAUSTED")
            if status is not TaskStatus.PENDING:
                await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def test_approve_landed_override_completes(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = _make_landed_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    tid = _seed_landed_task(db, repo)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "approve", tid[:8], "--landed", sha,
        "--because", "supervisor squash train 15",
    ])

    assert result.exit_code == 0, result.output
    assert "override" in result.output.lower()

    refreshed = _get_task(db, tid)
    assert refreshed.status is TaskStatus.DONE
    events = _list_events(db, tid)
    assert any(e.get("kind") == "approved_landed_override" for e in events)
    ev = [e for e in events if e.get("kind") == "approved_landed_override"][0]
    assert ev["sha"] == sha
    assert ev["justification"] == "supervisor squash train 15"
    assert "residue" in ev


def test_approve_landed_requires_justification(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = _make_landed_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    tid = _seed_landed_task(db, repo)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", tid[:8], "--landed", sha])

    assert result.exit_code != 0
    refreshed = _get_task(db, tid)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL
    assert _list_events(db, tid) == []


def test_approve_landed_refuses_blank_justification(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = _make_landed_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    tid = _seed_landed_task(db, repo)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "approve", tid[:8], "--landed", sha, "--because", "   ",
    ])

    assert result.exit_code != 0
    refreshed = _get_task(db, tid)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL
    assert _list_events(db, tid) == []


def test_approve_landed_refuses_unknown_sha(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = _make_landed_repo(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "a.txt").write_text("side\n")
    _git(repo, "commit", "-am", "side: never merged")
    side_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    tid = _seed_landed_task(db, repo)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "approve", tid[:8], "--landed", side_sha,
        "--because", "asserting anyway",
    ])

    assert result.exit_code != 0
    assert "not an ancestor" in result.output.lower()
    refreshed = _get_task(db, tid)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL
    assert _list_events(db, tid) == []


def test_approve_landed_refuses_wrong_task_status(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = _make_landed_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    tid = _seed_landed_task(db, repo, status=TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "approve", tid[:8], "--landed", sha, "--because", "asserting anyway",
    ])

    assert result.exit_code != 0
    output = result.output.lower()
    assert "not awaiting_approval" in output
    refreshed = _get_task(db, tid)
    assert refreshed.status is TaskStatus.IMPLEMENTING


def test_approve_landed_completes_failed_no_pr(tmp_path, monkeypatch):
    """The 5b2246c1 shape: a task that died pre-PR (budget exhaustion) whose
    branch content a human later hand-lands must be completable via the same
    `--landed`/`--because` override — not stuck FAILED forever because
    neither `nh approve --landed` (needs awaiting_approval) nor
    `nh task restore-approval` (needs PR evidence) admits it."""
    db = tmp_path / "test.db"
    repo = _make_landed_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature: add b.txt")
    feature_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "hand-landed: add b.txt")
    landed_sha = _git_out(repo, "rev-parse", "main")

    tid = _seed_landed_task(
        db, repo, status=TaskStatus.FAILED,
        attempt_branch="feature", attempt_commit_sha=feature_sha)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "approve", tid[:8], "--landed", landed_sha,
        "--because", "hand-landed by operator",
    ])

    assert result.exit_code == 0, result.output
    assert "failed" in result.output.lower()

    refreshed = _get_task(db, tid)
    assert refreshed.status is TaskStatus.DONE
    events = _list_events(db, tid)
    ev = [e for e in events if e.get("kind") == "approved_landed_override"][0]
    assert ev["sha"] == landed_sha
    assert ev["shape"] == "failed_pre_pr"
    assert ev["prior_status"] == "failed"


def test_approve_landed_with_base_narrows_and_names_matched_branch(
    tmp_path, monkeypatch,
):
    """AC4 (CLI side): `--base` must be HONOURED — as a narrowing assertion
    — even on a task that already has a recorded base_branch (seeded here as
    "main" by `_seed_landed_task`), and the confirmation must name the
    branch that actually matched."""
    db = tmp_path / "test.db"
    repo = _make_landed_repo(tmp_path)
    _git(repo, "checkout", "-b", "release/9")
    (repo / "a.txt").write_text("release-only change\n")
    _git(repo, "commit", "-am", "release/9: hotfix")
    release_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    tid = _seed_landed_task(db, repo)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "approve", tid[:8], "--landed", release_sha, "--base", "release/9",
        "--because", "landed on the release branch, not main",
    ])

    assert result.exit_code == 0, result.output
    assert "release/9" in result.output

    refreshed = _get_task(db, tid)
    assert refreshed.status is TaskStatus.DONE
    # narrowing an already-recorded base with --base never overwrites it
    assert refreshed.context.get("base_branch") == "main"
    assert refreshed.context.get("landed_override_base") == "release/9"
    events = _list_events(db, tid)
    ev = [e for e in events if e.get("kind") == "approved_landed_override"][0]
    assert ev["matched_branch"] == "release/9"
    assert ev["base_source"] == "human_asserted"


def test_approve_landed_with_base_still_requires_because(tmp_path, monkeypatch):
    """AC4: `--base` narrows the ancestry check, but does not make
    `--because` optional — the justification requirement is unconditional."""
    db = tmp_path / "test.db"
    repo = _make_landed_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    tid = _seed_landed_task(db, repo)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "approve", tid[:8], "--landed", sha, "--base", "main",
    ])

    assert result.exit_code != 0
    refreshed = _get_task(db, tid)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL
    assert _list_events(db, tid) == []


def test_approve_without_landed_is_unchanged(tmp_path, monkeypatch):
    """Regression guard: adding --landed/--because must not disturb the plain
    `nh approve <id>` path — same assertions as test_approve_awaiting_task."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_attempt(db, task_id, pr_url="https://example.com/pr/1")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "approved" in result.output.lower()
    assert "https://example.com/pr/1" in result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.context.get("approved_at") is not None


def test_approve_unknown_id(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)  # ensure DB exists
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", "deadbeef"])

    assert result.exit_code != 0
    assert "no task" in result.output.lower()


@pytest.mark.parametrize("argv", [["approve", "deadbeef"], ["review", "deadbeef"]])
def test_unknown_id_tells_user_how_to_find_a_task_id(tmp_path, monkeypatch, argv):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)  # ensure DB exists
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, argv)

    assert result.exit_code == 1, result.output
    assert "no task matching" in result.output
    assert "nh task list" in result.output


# --------------------------------------------------------------------------- #
# nh reject                                                                    #
# --------------------------------------------------------------------------- #

def test_reject_stores_feedback_and_resets(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs better tests"])

    assert result.exit_code == 0, result.output
    assert "sent back" in result.output.lower()

    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    feedback = refreshed.context.get("send_back_feedback", [])
    assert any("better tests" in f["message"] for f in feedback)


def test_reject_unknown_id(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", "deadbeef", "--reason", "nope"])

    assert result.exit_code != 0


def test_reject_done_task_is_blocked(tmp_path, monkeypatch):
    """SCRUM-77: a done row's status write is CAS-blocked (SCRUM-73) —
    reject must exit non-zero and say so, not print 'sent back'."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs fixes"])

    assert result.exit_code == 1, result.output
    assert "sent back" not in result.output.lower()
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.DONE
    assert refreshed.context.get("send_back_feedback") in (None, [])


def test_reject_cancelled_task_is_blocked(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.FAILED)

    async def _cancel():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            t.context = {"cancel_reason": "Cancelled from board"}
            await s.update_task(t)
    asyncio.run(_cancel())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs fixes"])

    assert result.exit_code == 1, result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.FAILED
    assert refreshed.context.get("send_back_feedback") in (None, [])


# --------------------------------------------------------------------------- #
# nh unblock                                                                   #
# --------------------------------------------------------------------------- #

def test_unblock_resumes_blocked_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8]])

    assert result.exit_code == 0, result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


def test_unblock_done_task_is_blocked(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8]])

    assert result.exit_code == 1, result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.DONE


def test_unblock_cancelled_task_is_blocked(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.FAILED)

    async def _cancel():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            t.context = {"cancel_reason": "Cancelled from board"}
            await s.update_task(t)
    asyncio.run(_cancel())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8]])

    assert result.exit_code == 1, result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.FAILED


# --------------------------------------------------------------------------- #
# nh diff                                                                      #
# --------------------------------------------------------------------------- #

def test_diff_no_commit(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["diff", task_id[:8]])

    assert result.exit_code == 0
    assert "no commit" in result.output.lower()


def test_diff_git_failure_handled(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)

    # Override repo_path to a nonexistent dir after seeding
    async def _patch_repo():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            t.repo_path = str(tmp_path / "nonexistent_repo")
            await s.update_task(t)
    asyncio.run(_patch_repo())

    _seed_attempt(db, task_id, commit_sha="abc123def456")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["diff", task_id[:8]])

    # Must not crash — output contains a useful message
    assert result.exit_code == 0
    lower = result.output.lower()
    assert "abc123" in result.output or "git" in lower or "failed" in lower


# --------------------------------------------------------------------------- #
# nh review                                                                    #
# --------------------------------------------------------------------------- #

def test_review_shows_checklist(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    checklist = {
        "passed": True,
        "items": [
            {"label": "Tests pass", "passed": True, "evidence": "208 passed"},
            {"label": "No regressions", "passed": True, "evidence": "tamper guard clean"},
        ],
    }
    _seed_attempt(db, task_id, review_checklist=checklist, review_passed=1)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["review", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "Tests pass" in result.output
    assert "208 passed" in result.output
    assert "PASSED" in result.output.upper()


def test_review_no_checklist(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["review", task_id[:8]])

    assert result.exit_code == 0
    assert "no review" in result.output.lower()


def test_review_checklist_escapes_model_authored_markup(tmp_path, monkeypatch):
    # A reviewer-authored label/evidence containing ALPHABETIC bracket tags must
    # survive to the terminal literally — rich only eats alphabetic tags, so a
    # numeric payload like "high[2]" is inert and proves nothing (per the task).
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    label = "a[b]c [dim]hidden[/] end"
    evidence = "before [red]boom[/] after"
    checklist = {
        "passed": True,
        "items": [
            {"label": label, "passed": True, "evidence": evidence},
        ],
    }
    _seed_attempt(db, task_id, review_checklist=checklist, review_passed=1)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["review", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert label in result.output
    assert evidence in result.output


def test_investigate_show_escapes_model_authored_findings(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    findings = "root cause: [bold]x[/] is unguarded"

    async def _set_findings():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            t.context = {"findings": findings}
            await s.update_task(t)
    asyncio.run(_set_findings())

    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["investigate", "--show", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert findings in result.output


# --------------------------------------------------------------------------- #
# nh logs                                                                      #
# --------------------------------------------------------------------------- #

def test_logs_shows_attempts(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED, title="Hard task")
    _seed_attempt(
        db, task_id,
        turns_used=42, tokens_used=15000,
        failure_reason="max_turns exceeded",
        status="failed",
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "Hard task" in result.output
    assert "42" in result.output
    assert "max_turns" in result.output


def test_logs_names_a_resume_checkpoint_that_could_not_be_read(tmp_path, monkeypatch):
    """`nh logs` reads ATTEMPTS, not the event stream, and it is the first place
    a human asks "why did this attempt start from scratch?". A checkpoint the
    orchestrator could not resume from must answer that here — and must not be
    dressed as a failure: the attempt succeeded, it just lost prior work."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE, title="Resumed task")
    _seed_attempt(
        db, task_id, status="succeeded", turns_used=7,
        resume_checkpoint_lost=(
            "checkpoint 5013e6c9 is no longer in the repository — this attempt "
            "branched from main instead"),
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "5013e6c9" in result.output, result.output
    assert "branched from main" in result.output, result.output
    assert "reason:" not in result.output, \
        "a lost checkpoint is not a failure reason and must not print as one"


def test_logs_no_attempts(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.PENDING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0
    assert "no attempts" in result.output.lower()


def test_logs_shows_and_tails_the_verification_artifact_when_present(
    tmp_path, monkeypatch,
):
    """D1.1 review finding #3: the PR body's pointer (and docs/pr-body.md)
    tell a reader to run `nh logs <id>` for the full command log — this is
    the one place that promise must actually hold. The path is computed the
    SAME way `Orchestrator._write_verification_artifact` does, so it can
    never disagree with what a PR body pointed at."""
    from no_human.core.orchestrator import Orchestrator

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL, title="Hard task")
    _seed_attempt(db, task_id, turns_used=5, status="succeeded")

    artifact_path = Orchestrator._verification_artifact_path(task_id, 1)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        "## How I verified this\n1 command recorded - as recorded\n\n"
        "### test\n- `uv run pytest -q`\n\n```\nTHE-DISTINCTIVE-TAIL-LINE\n```\n",
        encoding="utf-8",
    )

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert Orchestrator._display_path(str(artifact_path)) in result.output
    assert "THE-DISTINCTIVE-TAIL-LINE" in result.output, result.output


def test_logs_says_so_when_no_verification_artifact_was_written(
    tmp_path, monkeypatch,
):
    """The other half: an attempt with no artifact file must say so, not
    silently omit the line — the same honesty discipline as every other
    absence this command already reports."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED, title="Hard task")
    _seed_attempt(db, task_id, turns_used=5, status="failed")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "verification log: not written for this attempt" in result.output


def test_test_cmd_help():
    """nh test --help works without any bootstrap or auth."""
    runner = CliRunner()
    result = runner.invoke(cli, ["test", "--help"])
    assert result.exit_code == 0
    assert "fast" in result.output
    assert "full" in result.output
    assert "slow" in result.output
    assert "zero llm tokens" in result.output.lower()


# --------------------------------------------------------------------------- #
# nh agents                                                                     #
# --------------------------------------------------------------------------- #

def test_agents_shows_active(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, title="doing work")
    _seed_attempt(db, task_id, turns_used=5, tokens_used=1234)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["agents"])

    assert result.exit_code == 0, result.output
    assert "doing work" in result.output
    assert "implementing" in result.output.lower()


def test_agents_empty(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.DONE, title="finished")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["agents"])

    assert result.exit_code == 0
    assert "no active" in result.output.lower()


# --------------------------------------------------------------------------- #
# nh recall (B2): agentic-grep search over tasks/attempts/memories/history      #
# --------------------------------------------------------------------------- #

def _seed_memory(db_path: Path, *, mem_type="fact", content="",
                 confirmed=True, source="human") -> str:
    async def _go():
        async with Store(db_path) as s:
            return await s.add_memory(
                mem_type=mem_type, title=content[:40], content=content,
                confirmed=confirmed, source=source,
            )
    return asyncio.run(_go())


def _seed_history_cache(db_path: Path, *, title="", findings="") -> None:
    async def _go():
        async with Store(db_path) as s:
            await s.history_cache_put(
                content_sig=f"sig-{title}", cascade_id="cascade-1",
                title=title, findings_json=findings,
            )
    asyncio.run(_go())


def test_recall_finds_matching_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.DONE, title="Add cookie auth for the build server")
    _seed_task(db, TaskStatus.DONE, title="unrelated reporting dashboard work")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "cookie"])

    assert result.exit_code == 0, result.output
    assert "cookie auth for the build server" in result.output.lower()
    assert "reporting dashboard" not in result.output.lower()


def test_recall_finds_matching_memory(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_memory(db, mem_type="anti_pattern",
                content="Never hardcode the build-server password in a script.")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "jenkins password"])

    assert result.exit_code == 0, result.output
    assert "memory" in result.output.lower()
    assert "anti_pattern" in result.output.lower()


# G15: `nh recall` is named in the coder's own instructions as a Bash command it
# may run, so an unconfirmed proposal reachable through it is the human confirm
# gate bypassed by a search box. Default = confirmed-only; the operator opts in.

def test_recall_hides_unconfirmed_memory_by_default(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_memory(db, mem_type="rule", confirmed=False, source="proposed",
                 content="Always deploy the widget pipeline straight to prod.")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "widget pipeline"])

    assert result.exit_code == 0, result.output
    assert "straight to prod" not in result.output.lower()
    assert "no matches" in result.output.lower()


def test_recall_shows_unconfirmed_memory_with_include_pending(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_memory(db, mem_type="rule", confirmed=False, source="proposed",
                 content="Always deploy the widget pipeline straight to prod.")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "widget pipeline", "--include-pending"])

    assert result.exit_code == 0, result.output
    assert "straight to prod" in result.output.lower()
    # and it is labelled as pending, so the operator can tell the two apart
    assert "pending" in result.output.lower()


def test_recall_shows_confirmed_memory_either_way(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_memory(db, mem_type="rule", confirmed=True, source="human",
                 content="The widget pipeline needs a staging soak first.")
    runner = _make_runner(db, monkeypatch)

    default = runner.invoke(cli, ["recall", "widget pipeline"])
    opted_in = runner.invoke(cli, ["recall", "widget pipeline", "--include-pending"])

    assert default.exit_code == 0, default.output
    assert opted_in.exit_code == 0, opted_in.output
    for result in (default, opted_in):
        assert "staging soak" in result.output.lower()
        assert "pending" not in result.output.lower()


def test_recall_finds_matching_history_cache(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_history_cache(db, title="Debugging the build-server auth 401 loop",
                        findings="{}")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "auth 401"])

    assert result.exit_code == 0, result.output
    assert "build-server auth 401 loop" in result.output.lower()


def test_recall_shows_attempt_outcome_and_pr(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL, title="add mul() to calc")
    _seed_attempt(db, task_id, pr_url="https://example.com/pr/9", status="succeeded")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "mul"])

    assert result.exit_code == 0, result.output
    assert "https://example.com/pr/9" in result.output


def test_recall_no_matches(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.DONE, title="totally different work")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "nonexistent-xyz-term"])

    assert result.exit_code == 0, result.output
    assert "no matches" in result.output.lower()


def test_recall_respects_limit(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    for i in range(5):
        _seed_task(db, TaskStatus.DONE, title=f"widget task number {i}")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["recall", "widget", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "3 more" in result.output.lower()


# --------------------------------------------------------------------------- #
# A CLI in-process run (`--run`) must also persist its events                  #
# --------------------------------------------------------------------------- #

def test_persisting_sink_records_stamps_and_forwards():
    from no_human.cli.commands import _persisting

    class FakePersister:
        def __init__(self):
            self.recorded = []

        def record(self, e):
            self.recorded.append(e)

    p = FakePersister()
    seen = []
    sink = _persisting(p, "task-abc", seen.append)

    sink({"kind": "tool_use", "tool_name": "Read"})
    # A subagent carries the SDK's own dispatch id — it must survive, or every
    # subagent collapses onto one node in the System view.
    sink({"kind": "subagent_start", "task_id": "sdk-dispatch-1"})

    assert len(p.recorded) == 2
    assert seen == p.recorded, "the console sink still sees every event"

    assert p.recorded[0]["task_id"] == "task-abc"
    assert p.recorded[0]["ts"] > 0
    assert p.recorded[1]["task_id"] == "sdk-dispatch-1"


def test_persisting_sink_does_not_overwrite_an_existing_ts():
    from no_human.cli.commands import _persisting

    class FakePersister:
        def __init__(self):
            self.recorded = []

        def record(self, e):
            self.recorded.append(e)

    p = FakePersister()
    sink = _persisting(p, "task-abc", lambda e: None)
    sink({"kind": "state", "ts": 123.0})
    assert p.recorded[0]["ts"] == 123.0


# --------------------------------------------------------------------------- #
# nh reply --choose  (D14: only a human applies a blocker option's action)     #
# --------------------------------------------------------------------------- #

def _seed_blocked_task(db_path: Path) -> str:
    from no_human.blockers import Blocker, BlockerCategory, BlockerOption

    async def _go():
        async with Store(db_path) as s:
            t = Task.new("scope explosion", repo_path="/tmp/repo")
            await s.create_task(t)
            t.blocker = Blocker(
                category=BlockerCategory.SCOPE_EXPLOSION,
                confidence=0.9,
                question="This change exceeds the safety size limits.",
                options=[
                    BlockerOption(label="split into smaller tasks"),
                    BlockerOption(
                        label="raise the limit for this task",
                        action={"set_task_config": {"max_lines_changed": 700}},
                    ),
                ],
                resume_branch="scratch/x/abc-2",
                resume_commit="75c68e08",
            ).to_dict()
            await s.update_task(t)
            await s.set_status(t, TaskStatus.ESCALATED, validate=False)
            return t.id
    return asyncio.run(_go())


def test_reply_choose_applies_the_options_action(tmp_path, monkeypatch):
    """'raise the limit for this task' has to actually raise the limit — before
    D14 the same blocker was regenerated on the next attempt."""
    db = tmp_path / "test.db"
    task_id = _seed_blocked_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reply", task_id[:8], "--choose", "2", "--no-run"])

    assert result.exit_code == 0, result.output
    assert "max_lines_changed=700" in result.output

    t = _get_task(db, task_id)
    assert t.config["max_lines_changed"] == 700
    assert t.status is TaskStatus.IMPLEMENTING
    reply = t.context["human_replies"][-1]
    assert reply["answer"] == "raise the limit for this task"
    assert reply["applied"] == "max_lines_changed=700"
    # And it resumes from the checkpoint rather than from base (D15).
    assert t.context["resume_from"]["sha"] == "75c68e08"


def test_reply_choose_without_an_action_is_plain_free_text(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_blocked_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reply", task_id[:8], "--choose", "1", "--no-run"])

    assert result.exit_code == 0, result.output
    t = _get_task(db, task_id)
    assert t.config == {}  # nothing applied
    assert t.context["human_replies"][-1]["answer"] == "split into smaller tasks"


def test_reply_rejects_an_out_of_range_choice(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_blocked_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reply", task_id[:8], "--choose", "9", "--no-run"])

    assert result.exit_code != 0
    assert "between 1 and 2" in result.output
    assert _get_task(db, task_id).config == {}


def test_reply_needs_exactly_one_of_answer_or_choose(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_blocked_task(db)
    runner = _make_runner(db, monkeypatch)

    both = runner.invoke(cli, ["reply", task_id[:8], "an answer", "--choose", "1"])
    neither = runner.invoke(cli, ["reply", task_id[:8]])

    assert both.exit_code != 0 and "not both" in both.output
    assert neither.exit_code != 0 and "not both" in neither.output


# --------------------------------------------------------------------------- #
# nh status --json                                                             #
# --------------------------------------------------------------------------- #

def test_status_counts_pending_as_queued_not_working(tmp_path, monkeypatch):
    """`working N/max_workers` must not exceed the number of worker slots.

    PENDING is a task waiting to be picked up — the scheduler's `_CLAIMABLE`
    treats it that way and no worker is spending on it. Counting it as working
    printed impossible ratios like `working 5/4`, and that ratio is the one
    number here an operator reads to decide whether the pool is saturated. The
    command's docstring has always promised a "queued (pending)" lane.
    """
    db = tmp_path / "test.db"
    for _ in range(5):
        _seed_task(db, TaskStatus.PENDING)
    _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)

    out = json.loads(runner.invoke(cli, ["status", "--json"]).output)
    assert out["queued"] == 5, "five pending tasks are queued, not in flight"
    assert out["working"] == 1, (
        "only the IMPLEMENTING task occupies a worker; this read 6 before, "
        "which is more than max_workers and cannot be true")

    # And the human-readable line shows the same split.
    text = runner.invoke(cli, ["status"]).output
    assert "queued" in text and "5" in text


# --------------------------------------------------------------------------- #
# nh status — the denominator is the RUNNING pool, or says it isn't            #
# --------------------------------------------------------------------------- #

def _status_runner_with_config_width(db: Path, monkeypatch, width: int) -> CliRunner:
    """A `nh status` runner whose CONFIG says `width` workers, so the printed
    denominator can be told apart from the configured one."""
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data = {"concurrency": {"enabled": True, "max_workers": width}}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    return CliRunner()


def _stub_health(monkeypatch, payload, *, status: int = 200, exc=None,
                  body=None, seen=None):
    """Stub the queue-health HTTP call at the socket boundary, so the CLI's own
    parsing of the endpoint is exercised rather than mocked away. `payload` of
    None raises, standing in for "no server listening" — by default the exact
    exception `urlopen` raises on a closed local port: `URLError` wrapping a
    real `ConnectionRefusedError` (NOT a bare string reason, which the
    outcome classifier would misread as unreachable). Pass `exc` to choose a
    DIFFERENT failure at the socket boundary (a timeout, a DNS failure, …),
    `status` to answer with a non-200, `body` to answer with raw bytes the
    JSON parser has to cope with (truncated, empty, not JSON at all), and
    `seen` (a list) to capture the keyword arguments `urlopen` was called
    with, so a test can assert what timeout the probe really waited.

    `status` models what real `urlopen` DOES, which is not the same as what a
    naive stub does. `urllib`'s `HTTPErrorProcessor` routes every non-2xx into
    the opener's error path, which RAISES `HTTPError` for anything no handler
    claims — 4xx and 5xx among them — so a 500/503/404 never reaches the
    caller as a returned object with `.status == 500`; that shape does not
    exist. The stub therefore raises for a 4xx/5xx `status` and returns only
    for 2xx, so a probe that classifies a 500 correctly here classifies it
    correctly in production. (`HTTPError` is a `URLError`, hence an `OSError`,
    so it lands in the probe's general connection-failure except tuple: unless
    the probe names it there it degrades silently to "unreachable".)

    3xx is NOT modelled: `HTTPRedirectHandler` claims 301/302/303/307/308 and
    FOLLOWS them, so the caller sees whatever the redirect resolved to, and
    neither raising nor returning a 3xx here would be faithful."""
    import urllib.error
    import urllib.request

    class _Resp:
        def __init__(self):
            self.status = status

        def read(self):
            return body if body is not None else json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(url, timeout=None):
        assert "/api/queue/health" in url, f"status must read the live pool: {url}"
        if seen is not None:
            seen.append({"url": url, "timeout": timeout})
        if exc is not None:
            raise exc
        if payload is None:
            raise urllib.error.URLError(
                ConnectionRefusedError(61, "Connection refused"))
        assert not 300 <= status < 400, (
            f"this stub does not model redirects; {status} would be FOLLOWED")
        if not 200 <= status < 300:
            raise urllib.error.HTTPError(
                url, status, f"stub status {status}", {}, None)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)


class _ProbeCfg:
    """The minimal config `_probe_pool` needs: `server.host`/`server.port`
    default via `.get`, matching what `config.get("server", {})` sees."""

    def get(self, key, default=None):
        return {} if key == "server" else default


#: The URL `_probe_pool` builds from `_ProbeCfg`'s defaults. Used only as the
#: `filename` the hand-built `HTTPError` rows carry, so they look like the ones
#: `urlopen` raises rather than carrying a placeholder.
_HEALTH_URL = "http://127.0.0.1:8420/api/queue/health"


#: One row per way the probe can fail to learn the pool's width, as
#: (id, `_stub_health` kwargs, outcome, the words the operator must see).
#: NONE of them may classify as REFUSED and none may render "server not
#: running": each row names a cause where the server may well be up, and two
#: of them (the non-200 rows, the unreadable-body rows) are causes where the
#: server demonstrably ANSWERED. The same table drives three tests — the
#: classifier, the note, and the rendered `nh status` line — so a cause
#: cannot be classified without also being given honest words.
_PROBE_FAILURE_CAUSES = [
    ("read-timeout", dict(payload=None, exc=TimeoutError("timed out")),
     POOL_TIMEOUT, "no answer in"),
    ("socket-timeout", dict(payload=None, exc=socket.timeout("timed out")),
     POOL_TIMEOUT, "no answer in"),
    ("urlerror-wrapping-timeout",
     dict(payload=None, exc=urllib.error.URLError(TimeoutError("timed out"))),
     POOL_TIMEOUT, "no answer in"),
    ("dns-failure",
     dict(payload=None,
          exc=urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))),
     POOL_UNREACHABLE, "could not get a readable answer"),
    # `http.client` errors are NOT `OSError` or `URLError` subclasses; before
    # `HTTPException` joined the except tuple they escaped the probe and
    # crashed `nh status` outright.
    ("bad-status-line",
     dict(payload=None, exc=http.client.BadStatusLine("\x16\x03\x01")),
     POOL_UNREACHABLE, "could not get a readable answer"),
    ("invalid-url",
     dict(payload=None, exc=http.client.InvalidURL("nonnumeric port: ''")),
     POOL_UNREACHABLE, "could not get a readable answer"),
    ("incomplete-read",
     dict(payload=None, exc=http.client.IncompleteRead(b"{\"max_wo", 42)),
     POOL_UNREACHABLE, "could not get a readable answer"),
    # The RAISED shape, which is the only shape a real 500/503/404 has:
    # `urllib`'s error path turns a 4xx/5xx into a raised `HTTPError`, and
    # `HTTPError` is a `URLError` is an `OSError`, so these
    # rows are the ones that go red if the probe leaves an answered non-2xx to
    # its general connection-failure handler.
    ("http-500-raised",
     dict(payload=None,
          exc=urllib.error.HTTPError(_HEALTH_URL, 500, "Internal Server Error",
                                     {}, None)),
     POOL_HTTP_ERROR, "server answered HTTP 500"),
    ("http-503-raised",
     dict(payload=None,
          exc=urllib.error.HTTPError(_HEALTH_URL, 503, "Service Unavailable",
                                     {}, None)),
     POOL_HTTP_ERROR, "server answered HTTP 503"),
    ("http-404-raised",
     dict(payload=None,
          exc=urllib.error.HTTPError(_HEALTH_URL, 404, "Not Found", {}, None)),
     POOL_HTTP_ERROR, "server answered HTTP 404"),
    # Same 500 reached through the stub's own modelling of `urlopen` (it
    # raises for a 4xx/5xx `status=`), so the fixture and the probe agree.
    ("http-500", dict(payload={"max_workers": 8}, status=500),
     POOL_HTTP_ERROR, "server answered HTTP 500"),
    # 2xx-but-not-200 is the one non-200 that really is RETURNED: the error
    # processor passes 2xx through, so this row exercises the return path.
    ("http-204-empty", dict(payload={}, status=204, body=b""),
     POOL_HTTP_ERROR, "server answered HTTP 204"),
    ("body-is-not-json", dict(payload={}, body=b"{not json"),
     POOL_BAD_BODY, "server answered but the response was unreadable"),
    ("body-truncated", dict(payload={}, body=b'{"max_workers": 8'),
     POOL_BAD_BODY, "server answered but the response was unreadable"),
    ("body-is-not-an-object", dict(payload={}, body=b'"a string"'),
     POOL_BAD_BODY, "server answered but the response was unreadable"),
    ("width-is-not-a-number", dict(payload={"max_workers": "wide"}),
     POOL_BAD_BODY, "server answered but the response was unreadable"),
]

_PROBE_CAUSE_IDS = [row[0] for row in _PROBE_FAILURE_CAUSES]


def test_probe_pool_classifies_connection_refused_as_not_running(monkeypatch):
    _stub_health(monkeypatch, None)  # faithful URLError(ConnectionRefusedError)

    assert _probe_pool(_ProbeCfg()) == PoolProbe(None, POOL_REFUSED)


@pytest.mark.parametrize("_id,kwargs,outcome,_words", _PROBE_FAILURE_CAUSES,
                          ids=_PROBE_CAUSE_IDS)
def test_probe_pool_classifies_each_failure_by_what_it_established(
        monkeypatch, _id, kwargs, outcome, _words):
    """A stall, a DNS failure, a protocol error, a non-200 and an unreadable
    body are five DIFFERENT facts. None of them is evidence the server isn't
    running, so none may classify as REFUSED — and none may be flattened into
    the others, because the note the operator reads is chosen by this
    outcome."""
    _stub_health(monkeypatch, **kwargs)

    result = _probe_pool(_ProbeCfg())

    assert result.outcome == outcome, result.outcome
    assert result.outcome != POOL_REFUSED, result.outcome
    assert result.stats is None, result.stats


def test_probe_pool_carries_the_status_code_it_saw(monkeypatch):
    """The HTTP-error note names the code, so the probe has to carry it."""
    _stub_health(monkeypatch, {"max_workers": 8}, status=503)

    result = _probe_pool(_ProbeCfg())

    assert result == PoolProbe(None, POOL_HTTP_ERROR, 503), result


@pytest.mark.parametrize("code", [500, 503, 404])
def test_probe_pool_classifies_a_raised_httperror_by_the_code_it_carries(
        monkeypatch, code):
    """A real 500 arrives as a RAISED `HTTPError`, not as a returned response
    with `.status == 500`: `urllib`'s `HTTPErrorProcessor` hands every non-2xx
    to the error path, which raises for a 4xx/5xx (nothing claims those the
    way the redirect handler claims a 3xx). And `HTTPError` is a `URLError`,
    which is an `OSError` — so it lands in the probe's connection-failure
    except tuple.
    Unless the handler names it there, it falls through to that tuple's
    `POOL_UNREACHABLE` default and every real HTTP error reads as "no readable
    answer", losing the one fact the response established: the server
    answered, with this code."""
    import urllib.error

    # The subclassing that puts an answered non-2xx in the connection-failure
    # handler in the first place, asserted rather than asserted-in-prose.
    assert issubclass(urllib.error.HTTPError, urllib.error.URLError)
    assert issubclass(urllib.error.URLError, OSError)
    _stub_health(monkeypatch, None, exc=urllib.error.HTTPError(
        _HEALTH_URL, code, "stub", {}, None))

    result = _probe_pool(_ProbeCfg())

    assert result == PoolProbe(None, POOL_HTTP_ERROR, code), result
    assert str(code) in _pool_note(result.outcome, result.http_status)


def test_probe_pool_reads_a_2xx_that_is_not_200_off_the_returned_response(
        monkeypatch):
    """The other side of the same boundary: 2xx is the range `urlopen` really
    does RETURN, so a 204 (no body to parse a pool width out of) has to be
    classified off the returned object's status, not off an exception. This
    test walks the stub itself first, to show the row reaches the probe
    through the return path and not the raise path."""
    import urllib.request

    _stub_health(monkeypatch, {}, status=204, body=b"")

    with urllib.request.urlopen(_HEALTH_URL) as resp:   # returns; no raise
        assert resp.status == 204

    assert _probe_pool(_ProbeCfg()) == PoolProbe(None, POOL_HTTP_ERROR, 204)


@pytest.mark.parametrize("_id,kwargs,outcome,words", _PROBE_FAILURE_CAUSES,
                          ids=_PROBE_CAUSE_IDS)
def test_pool_note_says_only_what_that_outcome_established(
        monkeypatch, _id, kwargs, outcome, words):
    """The outcome->note relation, driven directly (no console scraping).
    "server not running" is REFUSED's alone: every other outcome here is a
    cause the server may have survived, and the non-200 / unreadable-body
    ones are causes where it demonstrably answered."""
    _stub_health(monkeypatch, **kwargs)
    probe = _probe_pool(_ProbeCfg())

    note = _pool_note(probe.outcome, probe.http_status)

    assert words in note, note
    assert "server not running" not in note, note
    assert "(configured;" in note, note


def test_pool_note_for_refused_is_the_one_that_says_not_running():
    """The other half of the relation: connection refused IS evidence there
    is no listener, and must not be softened into "unreachable"."""
    assert _pool_note(POOL_REFUSED) == " [dim](configured; server not running)[/]"


def test_pool_note_for_no_scheduler_says_the_server_answered():
    assert _pool_note(POOL_NO_SCHEDULER) == (
        " [dim](configured; server up, no pool attached)[/]")


@pytest.mark.parametrize("exc", [
    # The server answered 200 and sent bytes; the stream ended early.
    http.client.IncompleteRead(b"{\"max_wo", 42),
    # The server sent bytes that were not a status line (a TLS listener on a
    # plaintext port sends `\x16\x03\x01`) — bytes are an answer of a kind.
    http.client.BadStatusLine("\x16\x03\x01"),
], ids=["incomplete-read-after-200", "bad-status-line"])
def test_pool_note_for_unreachable_claims_no_mechanism(monkeypatch, exc):
    """POOL_UNREACHABLE covers causes where the server DID send bytes, so the
    note may not describe HOW it failed. It used to say "the connection failed
    before any answer", which for these two is false: an `IncompleteRead`
    happens after a 200 and a `BadStatusLine` is bytes that arrived. The note
    is allowed to say only the outcome — no readable answer, pool state
    unknown, and the printed width may therefore be wrong."""
    _stub_health(monkeypatch, None, exc=exc)
    probe = _probe_pool(_ProbeCfg())
    assert probe.outcome == POOL_UNREACHABLE, probe

    note = _pool_note(probe.outcome, probe.http_status)

    assert "pool state unknown" in note, note
    assert "this width may be wrong" in note, note
    for false_mechanism in ("before any answer", "the connection failed",
                            "server not running"):
        assert false_mechanism not in note, (false_mechanism, note)


def test_pool_note_default_for_an_unknown_outcome_claims_nothing_but_unknown():
    """The `.get` default. An outcome nobody has written a note for gets the
    note that asserts least about the server: not "not running" (which only a
    refusal establishes) and not a mechanism either (nothing is known about
    the mechanism of a failure nobody enumerated) — only that the pool state
    is unknown and the width being printed may be wrong."""
    note = _pool_note("an-outcome-from-the-future")

    assert note == _pool_note(POOL_UNREACHABLE)
    assert "pool state unknown" in note, note
    assert "server not running" not in note, note
    assert "the connection failed" not in note, note


def test_pool_note_for_a_status_code_the_probe_could_not_read():
    """`http_status` is None when the response object carried no status. The
    note still has to be a sentence, and still may not claim a code."""
    note = _pool_note(POOL_HTTP_ERROR, None)

    assert "pool state unknown" in note, note
    assert "None" not in note, note


def test_pool_note_timeout_is_rendered_from_the_timeout_the_probe_waits():
    """The operator-facing number and the number `urlopen` is given are ONE
    constant. Expectation is FORMATTED from the constant, not typed as a
    literal, so moving `PROBE_TIMEOUT_S` moves both or fails here."""
    assert f"no answer in {PROBE_TIMEOUT_S}s" in _pool_note(POOL_TIMEOUT)


def test_probe_pool_waits_exactly_the_timeout_the_note_advertises(monkeypatch):
    """The other end of the same coupling: the note's number is only honest if
    it is what was actually passed to `urlopen`."""
    seen = []
    _stub_health(monkeypatch, {"max_workers": 4}, seen=seen)

    _probe_pool(_ProbeCfg())

    assert [c["timeout"] for c in seen] == [PROBE_TIMEOUT_S], seen


def test_probe_pool_reports_no_scheduler_for_a_zero_width_pool(monkeypatch):
    _stub_health(monkeypatch, {"max_workers": 0})

    assert _probe_pool(_ProbeCfg()) == PoolProbe(None, POOL_NO_SCHEDULER)


def test_probe_pool_returns_live_stats_unchanged(monkeypatch):
    """Pins the wrapper's contract for `task_show`: `_probe_pool`'s `.stats`
    is the exact `(busy, width, pause)` tuple `_running_pool_stats` returns
    today for the same payload."""
    _stub_health(monkeypatch, {
        "max_workers": 8, "workers_busy": 2,
        "paused": True, "paused_reason": "quota",
        "paused_until": "2026-08-20T17:20:00+00:00",
        "paused_profile": "personal2",
    })

    result = _probe_pool(_ProbeCfg())

    assert result.outcome == POOL_LIVE, result.outcome
    assert result.stats == (2, 8, {
        "reason": "quota",
        "until": "2026-08-20T17:20:00+00:00",
        "profile": "personal2",
    })


@pytest.mark.parametrize("busy_raw", ["many", "2.5", [1], {"a": 1}, -3],
                          ids=["word", "decimal-string", "list", "object",
                               "negative"])
def test_probe_pool_degrades_an_unreadable_busy_but_keeps_the_observed_width(
        monkeypatch, busy_raw):
    """The judgement this pins: a junk NUMERATOR does not discredit a width
    the server really did report. The server answered 200 and `max_workers`
    parsed, so the outcome is LIVE and the denominator is an observation; only
    `workers_busy` is dropped, to exactly the `None` an ABSENT `workers_busy`
    already produces (`_running_pool_stats`' documented contract: absent or
    unparseable both mean "answered, but reported no numerator", and the
    caller then counts rows for the numerator).

    Not POOL_BAD_BODY: that would throw away the observed width and print the
    configured guess instead, which is a worse number, and would claim the
    response was unreadable when all of it but one field was read.

    Not an exception either — the probe may never raise. `-3` is here for the
    other unreadable case: a numerator that parses but cannot be true, since
    `workers_busy` is `len(inflight_ids)` and a set has no negative size."""
    _stub_health(monkeypatch, {"max_workers": 8, "workers_busy": busy_raw})

    result = _probe_pool(_ProbeCfg())

    assert result == PoolProbe((None, 8, None), POOL_LIVE), result


def test_probe_pool_still_reports_no_scheduler_when_busy_is_unreadable(
        monkeypatch):
    """The width decides the outcome, and it is read first: a zero width with
    a junk numerator is still "server up, no pool attached", not a live pool
    and not a crash."""
    _stub_health(monkeypatch, {"max_workers": 0, "workers_busy": "many"})

    assert _probe_pool(_ProbeCfg()) == PoolProbe(None, POOL_NO_SCHEDULER)


def test_status_counts_rows_for_the_numerator_when_workers_busy_is_unreadable(
        tmp_path, monkeypatch):
    """What the operator actually reads for the degraded case. The width is
    the observed 8, NOT the configured 2, and it carries no note — because
    nothing about it is a guess. The numerator falls back to counting rows,
    the same fallback an absent `workers_busy` takes
    (`test_status_keeps_counting_rows_when_health_omits_workers_busy`), so the
    line makes no claim the payload failed to support: no "server not
    running", no "configured" label on an observed number, and no `None`
    leaking into the ratio."""
    db = tmp_path / "test.db"
    for _ in range(3):
        _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 8, "workers_busy": "many"})

    result = runner.invoke(cli, ["status"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "working 3/8" in out, out
    assert "configured" not in out, out
    assert "server not running" not in out, out
    assert "None" not in out, out


def test_status_prints_the_running_pool_width_not_the_configured_one(tmp_path, monkeypatch):
    """`nh start --workers 8` deliberately leaves config on disk alone, so a
    config-sourced denominator printed `working N/2` while 8 workers ran —
    wrong for exactly the override a saturation question depends on. `nh start`
    is the path this fixes: it is the one that puts the pool behind an HTTP
    server there is something to ask. `nh serve` binds no socket, so it stays
    on the labelled config fallback (known gap, see `_running_pool_stats`)."""
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 8, "queue_depth": 0})

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "working 1/8" in out, out
    assert "configured" not in out, "the width was observed, not guessed"


def test_status_falls_back_to_config_and_says_so_when_no_server(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, None)          # nothing listening

    result = runner.invoke(cli, ["status"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "working 1/2" in out, out
    assert "(configured; server not running)" in out, out


def test_status_does_not_claim_the_server_is_down_when_the_probe_timed_out(tmp_path, monkeypatch):
    """A single stall past the probe's timeout used to render as the
    definitive "server not running", because every non-refusal failure took
    the same branch as a refusal. A timeout establishes "could not reach it in
    the time it had", never "not running"."""
    db = tmp_path / "test.db"
    for _ in range(7):
        _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 4)
    _stub_health(monkeypatch, None, exc=TimeoutError("timed out"))

    result = runner.invoke(cli, ["status"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "server not running" not in out, out
    assert "unreachable" in out, out
    assert "configured" in out, out
    # The advertised number is the one the probe waits, formatted from the
    # constant rather than typed here.
    assert f"no answer in {PROBE_TIMEOUT_S}s" in out, out


@pytest.mark.parametrize("_id,kwargs,_outcome,words", _PROBE_FAILURE_CAUSES,
                          ids=_PROBE_CAUSE_IDS)
def test_status_renders_what_each_probe_failure_established(
        tmp_path, monkeypatch, _id, kwargs, _outcome, words):
    """Every cause, driven through the real render path. `nh status` must
    exit 0 (a probe that cannot classify a failure must never crash the
    command), must fall back to the labelled config width, and must print the
    words for THAT cause — never "server not running", which only a refusal
    establishes."""
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, **kwargs)

    result = runner.invoke(cli, ["status"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "working 1/2" in out, out
    assert words in out, out
    assert "server not running" not in out, out


def test_status_does_not_trust_a_zero_width_pool(tmp_path, monkeypatch):
    """A reachable server with no scheduler attached reports max_workers 0.
    Printing `working 1/0` would be an impossible ratio, and treating 0 as an
    observation would be a claim about a pool that isn't draining anything.
    The server DID answer 200, though — "server not running" is falsified by
    the probe's own evidence, so the note must say something else."""
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 0})

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "working 1/2" in out, out
    assert "server not running" not in out, out
    assert "server up, no pool attached" in out, out


def test_status_json_is_unchanged_by_the_live_width(tmp_path, monkeypatch):
    """The --json shape is a consumer contract: the honest denominator is a
    human-readable-line change only."""
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 8})

    out = json.loads(runner.invoke(cli, ["status", "--json"]).output)

    assert set(out) == {"needs you", "queued", "working", "waiting", "failed",
                        "done", "unattributed_usage"}
    assert out["working"] == 1


def test_status_json_is_unchanged_by_the_probe_outcome(tmp_path, monkeypatch):
    """The probe's classification of WHY there are no stats is a note on the
    human-readable line only — `--json` carries no note today and must not
    grow one no matter which failure mode tripped the probe."""
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.IMPLEMENTING)
    expected_keys = {"needs you", "queued", "working", "waiting", "failed",
                      "done", "unattributed_usage"}

    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, None, exc=TimeoutError("timed out"))
    timeout_out = json.loads(runner.invoke(cli, ["status", "--json"]).output)

    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, None)  # connection refused
    refused_out = json.loads(runner.invoke(cli, ["status", "--json"]).output)

    assert set(timeout_out) == expected_keys, timeout_out
    assert set(refused_out) == expected_keys, refused_out
    assert set(timeout_out) == set(refused_out)


def test_status_working_numerator_comes_from_workers_busy(tmp_path, monkeypatch):
    """The `working` numerator is the live `workers_busy` count, not a count of
    worker-owned status rows: a row can be IMPLEMENTING while stranded
    (claimable, not running) after a restart."""
    db = tmp_path / "test.db"
    for _ in range(3):
        _seed_task(db, TaskStatus.IMPLEMENTING)
    _seed_task(db, TaskStatus.REVIEWING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 4, "workers_busy": 2})

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "working 2/4" in out, out
    assert "working 4/4" not in out, out


def test_status_never_prints_an_over_capacity_ratio(tmp_path, monkeypatch):
    """Reproduces the live 2026-08-20 shape: 8 worker-owned rows (4 stranded
    IMPLEMENTING after a restart, 4 genuinely running) against a 4-wide pool.
    The line must never claim more in-flight than there are slots to run
    them."""
    db = tmp_path / "test.db"
    for _ in range(8):
        _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 4, "workers_busy": 4})

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "working 4/4" in out, out
    assert "working 8/4" not in out, out


def test_status_reports_unclaimed_implementing_rows_as_queued(tmp_path, monkeypatch):
    """The 4 stranded IMPLEMENTING rows in excess of `workers_busy` don't
    disappear from the line — they move to `queued`, matching how
    `/api/queue/health` itself counts an unclaimed IMPLEMENTING row toward
    `queue_depth`. No task vanishes: working + queued accounts for all 14."""
    db = tmp_path / "test.db"
    for _ in range(8):
        _seed_task(db, TaskStatus.IMPLEMENTING)
    for _ in range(6):
        _seed_task(db, TaskStatus.PENDING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 4, "workers_busy": 4,
                                "queue_depth": 10})

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "queued 10" in out, out
    assert "working 4/4" in out, out


def test_status_prints_the_quota_pause_line_from_the_same_fields(tmp_path, monkeypatch):
    """2026-08-20 evidence: `nh status` (like the board header) must say WHY
    nothing is moving when the pool is behind a quota wall, sourced from the
    same `paused_*` fields `/api/queue/health` already reports — not a
    second, independently-derived clock."""
    db = tmp_path / "test.db"
    for _ in range(7):
        _seed_task(db, TaskStatus.PENDING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {
        "max_workers": 4, "workers_busy": 0, "queue_depth": 7,
        "paused": True, "paused_reason": "quota",
        "paused_until": "2026-08-20T17:20:00+00:00",
        "paused_profile": "personal2",
    })

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "paused" in out, out
    assert "quota cooldown" in out, out
    assert "personal2 profile" in out, out
    # 2026-08-21 amendment: the resume time renders in the user's local
    # 24-hour HH:MM (mirroring the board's formatPausedUntil()), not the raw
    # UTC ISO string — computed here, not hardcoded, so the assertion holds
    # under any machine's local timezone.
    expected_hhmm = datetime.fromisoformat(
        "2026-08-20T17:20:00+00:00").astimezone().strftime("%H:%M")
    assert f"resumes {expected_hhmm}" in out, out
    assert "2026-08-20T17:20:00+00:00" not in out, out


def test_status_prints_infra_pause_line(tmp_path, monkeypatch):
    """Independent review of PR #553 (2026-08-21): the infra breaker (3
    consecutive zero-token/auth SDK failures) arms the same cooldown clock a
    quota park does, so `nh status` must not blame a profile that had nothing
    to do with it. `paused_reason: "infra"` must print an SDK/auth-specific
    line with no profile mention, even though the stub still carries a
    `paused_profile` (the field an infra cooldown must ignore)."""
    db = tmp_path / "test.db"
    for _ in range(7):
        _seed_task(db, TaskStatus.PENDING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {
        "max_workers": 4, "workers_busy": 0, "queue_depth": 7,
        "paused": True, "paused_reason": "infra",
        "paused_until": "2026-08-20T17:20:00+00:00",
        "paused_profile": "personal2",
    })

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "paused" in out, out
    assert "SDK/auth failures" in out, out
    expected_hhmm = datetime.fromisoformat(
        "2026-08-20T17:20:00+00:00").astimezone().strftime("%H:%M")
    assert f"resumes {expected_hhmm}" in out, out
    assert "personal2" not in out, out
    assert "quota" not in out, out


def test_status_prints_no_pause_line_when_not_paused(tmp_path, monkeypatch):
    """Negative control: an ordinary (non-cooldown) payload must not grow a
    pause line — unchanged from today, per the acceptance criterion."""
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 4, "workers_busy": 1})

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "paused" not in out, out


def test_status_still_prints_a_saturated_pool_as_saturated(tmp_path, monkeypatch):
    """Negative control: the fix must not make a genuinely busy pool look
    idle. Exactly 4 IMPLEMENTING rows against a 4-wide, fully-busy pool is
    real saturation, not a stranded-row artifact."""
    db = tmp_path / "test.db"
    for _ in range(4):
        _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 4, "workers_busy": 4})

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "working 4/4" in out, out
    assert "configured" not in out, out
    assert "working 0/4" not in out, out


def test_status_keeps_counting_rows_when_health_omits_workers_busy(tmp_path, monkeypatch):
    """An older server build (or any payload missing `workers_busy`) still
    answers with a live `max_workers`. `busy=None` must not print a false
    `working 0/N` on a busy pool — the caller falls back to counting rows for
    the numerator while still trusting the observed denominator."""
    db = tmp_path / "test.db"
    for _ in range(3):
        _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _status_runner_with_config_width(db, monkeypatch, 2)
    _stub_health(monkeypatch, {"max_workers": 8})

    out = " ".join(runner.invoke(cli, ["status"]).output.split())

    assert "working 3/8" in out, out


def test_status_json_bucket_counts(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_task(db, TaskStatus.IMPLEMENTING)
    _seed_task(db, TaskStatus.PAUSED_QUOTA)
    _seed_task(db, TaskStatus.FAILED)
    _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    # The residual ledger rides alongside the buckets (it is not summed into
    # any of them); with no intake spend seeded it reports an explicit zero
    # rather than being absent, so consumers see a stable shape.
    assert json.loads(result.output) == {
        "needs you": 2, "queued": 0, "working": 1, "waiting": 1,
        "failed": 1, "done": 1,
        "unattributed_usage": {
            "calls": 0, "tokens_used": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "total": 0,
        },
    }


def test_status_json_exit_zero(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status", "--json"])

    assert result.exit_code == 0, result.output


def test_status_default_unchanged(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_task(db, TaskStatus.IMPLEMENTING)
    _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "needs you" in result.output
    assert "working" in result.output
    assert "done" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


def test_status_excludes_operator_cancels_from_the_failed_count(tmp_path, monkeypatch):
    """A task an operator cancelled ends in FAILED status but is not a
    capability failure — `web/src/boardLanes.js` `isRealFailure` already
    excludes it from the board's Outcomes count. `nh status` never adopted
    that split: two FAILED tasks, one a cancel, printed `failed 2`, hiding
    the one real failure inside a number that also counts intentional stops.
    The --json bucket keeps summing both (a documented consumer contract,
    see `test_status_json_bucket_counts`); only the human-readable line
    changes, to `failed 1 (+1 cancelled)`.
    """
    db = tmp_path / "test.db"
    real_failure = _seed_task(db, TaskStatus.FAILED, title="Real failure")
    cancelled = _seed_task(db, TaskStatus.FAILED, title="Cancelled task")
    _seed_task(db, TaskStatus.DONE)

    async def _cancel():
        async with Store(db) as s:
            t = await s.find_task(cancelled)
            t.context = {"cancel_reason": "Cancelled from board"}
            await s.update_task(t)
    asyncio.run(_cancel())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "failed 1 (+1 cancelled)" in out, out
    assert "done 1" in out, out

    # The --json shape is untouched: it still sums both under "failed".
    json_out = json.loads(runner.invoke(cli, ["status", "--json"]).output)
    assert json_out["failed"] == 2, json_out


def test_status_json_empty(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["status", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "needs you": 0, "queued": 0, "working": 0, "waiting": 0,
        "failed": 0, "done": 0,
        "unattributed_usage": {
            "calls": 0, "tokens_used": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "total": 0,
        },
    }


# --------------------------------------------------------------------------- #
# nh status — unattributed-vs-attributed intake-spend split                   #
# --------------------------------------------------------------------------- #

def _seed_unattributed(db_path: Path, *, site: str, tokens_used: int = 100,
                        task_id: str | None = None) -> str:
    async def _go():
        async with Store(db_path) as s:
            return await s.record_unattributed_usage(
                site=site, tokens_used=tokens_used, task_id=task_id)
    return asyncio.run(_go())


def test_status_splits_owned_and_ownerless_intake_spend(tmp_path, monkeypatch):
    """AC1/AC2: the printed line separates the genuinely ownerless spend
    (`cli.*`/`api.*`, no task_id) from spend already recorded against a task
    (`orphaned_*`) — "no task owns it" attaches only to the former, and the
    latter is named as recorded-but-not-yet-in-attempt-rows, not as lost."""
    db = tmp_path / "test.db"
    _seed_unattributed(db, site="cli.task_add.grill", tokens_used=1000)
    task_id = _seed_task(db, TaskStatus.DONE)
    _seed_unattributed(db, site="orphaned_plan_usage", tokens_used=5000,
                        task_id=task_id)
    runner = _make_runner(db, monkeypatch)

    out = runner.invoke(cli, ["status"]).output

    assert "1,000 tokens over 1 call(s) — no task owns it" in out, out
    assert "5,000 tokens over 1 call(s) recorded to tasks but not in " \
           "their attempt rows" in out, out


def test_status_does_not_say_no_task_owns_it_when_every_row_is_attributed(
        tmp_path, monkeypatch):
    """Negative control for AC1: with only `orphaned_*` rows, the residual
    line still prints (there is something to say) but never claims no task
    owns the spend."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    _seed_unattributed(db, site="orphaned_plan_usage", tokens_used=5000,
                        task_id=task_id)
    runner = _make_runner(db, monkeypatch)

    out = runner.invoke(cli, ["status"]).output

    assert "recorded to tasks but not in their attempt rows" in out, out
    assert "no task owns it" not in out, out


def test_a_new_orphaned_site_classifies_as_attributed(tmp_path, monkeypatch):
    """AC3: the split is derived from the `orphaned_` site PREFIX, not a
    hardcoded list of the four known aux tiers — a brand-new aux role's site
    (never referenced anywhere in source) must still land in the attributed
    half automatically."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    _seed_unattributed(db, site="orphaned_reviewer_usage", tokens_used=777,
                        task_id=task_id)
    runner = _make_runner(db, monkeypatch)

    out = runner.invoke(cli, ["status"]).output

    assert "no task owns it" not in out, out
    assert "777 tokens over 1 call(s) recorded to tasks but not in their " \
           "attempt rows" in out, out


def test_status_json_keys_unchanged_with_both_classes_present(tmp_path, monkeypatch):
    """AC4: `--json`'s `unattributed_usage` keeps its five keys and its
    whole-ledger `total`, unaffected by the new split logic used only on the
    human-readable line."""
    db = tmp_path / "test.db"
    _seed_unattributed(db, site="cli.task_add.grill", tokens_used=1000)
    task_id = _seed_task(db, TaskStatus.DONE)
    _seed_unattributed(db, site="orphaned_plan_usage", tokens_used=5000,
                        task_id=task_id)
    runner = _make_runner(db, monkeypatch)

    out = json.loads(runner.invoke(cli, ["status", "--json"]).output)

    assert set(out["unattributed_usage"]) == {
        "calls", "tokens_used", "cache_read_tokens", "cache_creation_tokens",
        "total"}
    assert out["unattributed_usage"]["total"] == 6000
    assert out["unattributed_usage"]["calls"] == 2


def test_status_split_counts_a_rolled_up_row_as_its_original_calls(
        tmp_path, monkeypatch):
    """AC5: retention compaction must not shrink the attributed clause's call
    count — a roll-up row still counts as the calls it replaced.

    Seed, backdate and compact all inside ONE Store connection: `connect()`
    itself runs a best-effort `compact_unattributed_usage()` (db.py:518), so
    opening a fresh connection between the backdate UPDATE and the explicit
    compact call below would let that implicit, default-retention pass
    collapse the rows first and mask what this test means to exercise."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)

    async def _seed_backdate_compact():
        async with Store(db) as s:
            ids = []
            for _ in range(4):
                ids.append(await s.record_unattributed_usage(
                    site="orphaned_plan_usage", tokens_used=100,
                    task_id=task_id))
            placeholders = ",".join("?" for _ in ids)
            await s.db.execute(
                f"UPDATE unattributed_usage SET ts = ? WHERE id IN "
                f"({placeholders})",
                ("2020-01-01T00:00:00+00:00", *ids))
            await s.db.commit()
            return await s.compact_unattributed_usage(retention_days=1)
    collapsed = asyncio.run(_seed_backdate_compact())
    assert collapsed == 4
    runner = _make_runner(db, monkeypatch)

    out = runner.invoke(cli, ["status"]).output

    assert "400 tokens over 4 call(s) recorded to tasks but not in their " \
           "attempt rows" in out, out


def test_status_prints_no_residual_line_on_an_empty_ledger(tmp_path, monkeypatch):
    """AC6: with nothing in the unattributed ledger, the residual line is
    absent entirely — today's "prints only when it has something to say"."""
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    out = runner.invoke(cli, ["status"]).output

    assert "unattributed intake spend" not in out, out


def test_approve_with_a_stale_claim_and_a_real_pr_does_not_auto_done(tmp_path, monkeypatch):
    """PR #101 round-2 MEDIUM: after a claim is sent back and a later attempt
    ships a REAL PR, the stale already_satisfied_report must not hijack the
    approval into a false DONE — the human still has a PR to merge."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_attempt(db, task_id, pr_url="https://github.com/o/r/pull/7")

    async def _ctx():
        async with Store(db) as s:
            await s.merge_context(task_id, {"already_satisfied_report":
                "ALREADY-SATISFIED\nCRITERION: x — MET — evidence: a.py:1"})
    asyncio.run(_ctx())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "merge the pr" in result.output.lower()
    assert "https://github.com/o/r/pull/7" in result.output
    refreshed = _get_task(db, task_id)
    assert refreshed.status is TaskStatus.AWAITING_APPROVAL


# ------------------------- interactive grill (B2) -------------------------- #

@pytest.mark.asyncio
async def test_cli_grill_writes_shared_qa_surface(tmp_path, monkeypatch):
    """#121 reviewer gap: the GrillResult path had no test. The human-answered
    Q&A must land on the SAME audit surface the unattended grill uses
    (context['intake_qa'], source=human) and stamp grill_complete so the
    orchestrator's auto-grill never re-asks."""
    from no_human.cli import commands as cmds
    from no_human.config import load_config
    from no_human.intake.grill import GrillQuestion, GrillResult

    steps = [
        GrillQuestion(round=1, question="Which repo?", suggestions=["A", "B"]),
        GrillResult(title="refined title", description="refined desc",
                    acceptance_criteria=["AC1"]),
    ]

    async def fake_grill_step(*a, **k):
        return steps.pop(0)

    monkeypatch.setattr(cmds, "ClaudeBackend", lambda **k: object())
    monkeypatch.setattr("no_human.intake.grill.grill_step", fake_grill_step)
    monkeypatch.setattr("click.prompt", lambda *a, **k: "repo A please")

    cfg = load_config(tmp_path / "config.yaml")
    t = Task.new("raw title", repo_path="/r")
    out = await cmds._run_cli_grill(cfg, t)

    assert out.title == "refined title"
    assert out.acceptance_criteria == ["AC1"]
    ctx = out.context or {}
    assert ctx["grill_complete"] is True
    qa = ctx["intake_qa"]
    assert len(qa) == 1
    assert qa[0]["question"] == "Which repo?"
    assert qa[0]["answer"] == "repo A please"
    assert qa[0]["source"] == "human"


def test_logs_reports_SPEND_not_just_non_cache_tokens(tmp_path, monkeypatch):
    """`nh logs` under-reported a live runaway by ~5500x.

    `attempts.tokens_used` holds NON-CACHE tokens only, while the budget guard
    enforces `tokens_used + cache_read_tokens`. Cache reads dominate real burn,
    so an attempt aborted at 4,054,229 displayed as `tokens=731` — the one
    attempt that needed attention looked like the cheapest thing that ever ran.

    NOTE ON THIS TEST'S OWN HISTORY: the first version asserted the bare string
    "4,054,229", which the seeded `failure_reason` echoed back on the next
    line — so restoring the bug (`_plain + _read` -> `_plain`) left it green.
    It now asserts the RENDERED FIELD, `spend=...`, and the seeded reason
    deliberately contains no digits that could satisfy it.

    Numbers are the real ones from task fa7be197.
    """
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED, title="Runaway")
    _seed_attempt(
        db, task_id, tokens_used=731,          # turns NULL, as on a real abort
        cache_read_tokens=4_053_498, cache_creation_tokens=197_948,
        plan_tokens_used=5_046, plan_cache_read_tokens=334_396,
        plan_cache_creation_tokens=45_165,
        utility_tokens_used=2_395, utility_cache_read_tokens=136_632,
        utility_cache_creation_tokens=217_009,
        status="failed",
        failure_reason="budget-abort: crossed the per-attempt cap",
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])
    out = result.output.replace("\n", "")     # the line wraps at 80 cols

    assert result.exit_code == 0, result.output
    # THE assertion: the guard's own number, as a rendered field.
    assert "spend=4,054,229" in out, result.output
    # Cache CREATION is billed at the fresh rate and the cap ignores it, so a
    # spend-only line hides ~33% of the dollar cost. burn must show it.
    # burn is the ATTEMPT's total, not the coder session's. The plan and
    # utility sessions on this row add 740,643 tokens — 15% of the tokens and
    # 34% of the dollars. Presenting the coder's number as the total is the
    # same "partial number shown as a total" defect the coder-only display
    # had, one tier up; cost.js's header records this repo shipping it twice.
    assert "burn=4,992,820" in out, result.output
    # Components stay visible so "why" is answerable.
    assert "non-cache 731" in out and "cache-read 4,053,498" in out
    assert "cache-creation 197,948" in out
    # A NULL turns column must not print the literal "None".
    assert "turns=None" not in out, result.output


def test_logs_says_UNKNOWN_rather_than_zero_when_tokens_are_null(
        tmp_path, monkeypatch):
    """13 of 127 live attempt rows have a NULL `tokens_used`.

    The previous version of this test claimed the CACHE columns could be NULL;
    they cannot — `db.py` declares them `INTEGER DEFAULT 0`, and 0 of 127 live
    rows are NULL. So it guarded a branch that never runs. `tokens_used` NULL
    is the real case, and without it the total is genuinely unknown: printing
    "0" would be a claim, the same kind of untrue display as `turns=None`.
    """
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED, title="Unknown spend")
    _seed_attempt(db, task_id, turns_used=3, cache_read_tokens=900,
                  status="failed")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])
    out = result.output.replace("\n", "")

    assert result.exit_code == 0, result.output
    assert "spend=?" in out, result.output
    assert "burn=?" in out, result.output
    assert "spend=0" not in out, "0 is a claim; the value is unknown"
    # The component that IS known is still reported.
    assert "cache-read 900" in out


def test_agents_table_shows_BURN_not_non_cache_coder_tokens(tmp_path, monkeypatch):
    """The Agent Sessions table is what an operator watches a runaway on, so
    it was the worst place to print the smallest number: it carried the same
    5500x under-report `nh logs` did (`tokens_used` is NON-CACHE CODER tokens).

    The id below is pinned, and pinned to a COLLIDING one on purpose. This test
    used to seed a random uuid and assert `"731" not in <whole frame>`; the id
    column prints `t.id[:8]`, so it failed whenever those eight hex digits
    happened to contain "731" - which is roughly 1 run in 700, and cost this
    project three misdiagnoses in a single session when a uuid came up
    `731a952d`. That id is now the fixture. The assertions read the burn CELL,
    so the test is its own positive control: the collision is on screen every
    run, and it passes only because nothing asserts on the frame as a whole.
    """
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, title="Runaway",
                         task_id="731a952d" + "f" * 24)
    _seed_attempt(
        db, task_id, tokens_used=731,
        cache_read_tokens=4_053_498, cache_creation_tokens=197_948,
        plan_tokens_used=5_046, plan_cache_read_tokens=334_396,
        plan_cache_creation_tokens=45_165,
        utility_tokens_used=2_395, utility_cache_read_tokens=136_632,
        utility_cache_creation_tokens=217_009,
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["agents"])
    rows = _table_rows(result.output)

    assert result.exit_code == 0, result.output
    assert len(rows) == 1, result.output
    # The collision really is rendered - otherwise the guard below proves
    # nothing and this test quietly stops being a control.
    assert rows[0]["id"] == "731a952d", result.output
    assert rows[0]["burn"] == "4,992,820", result.output
    # The old value must be gone, not merely joined by the new one - anywhere
    # in the row except the id, which is an identifier and not a number.
    non_id = " ".join(v for k, v in rows[0].items() if k != "id")
    assert "731" not in non_id.replace("4,992,820", ""), result.output


def test_burn_includes_the_REVIEWER_session(tmp_path, monkeypatch):
    """The reviewer's tokens are part of the attempt's burn, and nothing saw
    them.

    The other fixtures model attempt #1 of task fa7be197, which ABORTED before
    review — so all six `review_*` columns are legitimately 0 there, and
    dropping the whole review group from `_TOKEN_GROUPS` survived the entire
    suite. This models attempt #2 of the SAME task, which completed and did
    run a reviewer; every number below is that row verbatim.

    Live impact of the blind spot: 4 attempts in the operator's DB carry review
    burn, up to 300,236 tokens.
    """
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL, title="Reviewed")
    _seed_attempt(
        db, task_id, turns_used=39,
        tokens_used=12_665, cache_read_tokens=2_146_223,
        cache_creation_tokens=49_005,
        review_tokens_used=2_701, review_cache_read_tokens=59_624,
        review_cache_creation_tokens=31_159,
        utility_tokens_used=474, utility_cache_read_tokens=141_771,
        utility_cache_creation_tokens=149_727,
        status="succeeded",
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])
    out = result.output.replace("\n", "")

    assert result.exit_code == 0, result.output
    # spend is the CODER session only — what the cap enforces.
    assert "spend=2,158,888" in out, result.output
    # burn is the whole attempt. Drop the review group and this is 2,499,865.
    assert "burn=2,593,349" in out, result.output


# --------------------------------------------------------------------------- #
# nh start — Jira poller parity with nh serve (SCRUM-21)                      #
# --------------------------------------------------------------------------- #

class _FakeUvicornServer:
    """Stands in for uvicorn.Server so `nh start` tests never bind a socket."""

    def __init__(self, config):
        self.config = config

    async def serve(self):
        return None


def _make_start_cfg(db_path: Path, *, jira_enabled: bool):
    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data = {
            "server": {"port": 8420},
            "concurrency": {},
            "integrations": {"jira": {
                "enabled": jira_enabled,
                "project_key": "SCRUM",
                "poll_interval": "5m",
            }},
        }

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db_path
    return _Cfg()


def _patch_start_scaffolding(monkeypatch, cfg):
    import no_human.cli.commands as cmd_mod

    # Never touch the real ~/.no_human/nh.pid or shell out for auth/CLI
    # checks — the test only cares about the Jira-poller wiring.
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(cmd_mod, "_assert_backend_usable", lambda: None)
    monkeypatch.setattr(cmd_mod, "_acquire_pid_lock", lambda: True)
    monkeypatch.setattr(cmd_mod, "_release_pid_lock", lambda: None)
    # `nh start` builds its own uvicorn.Server (instead of uvicorn.run) so it
    # can run the Jira poll loop in the same event loop — fake it so the test
    # never binds a real socket.
    monkeypatch.setattr(uvicorn, "Server", _FakeUvicornServer)
    return cmd_mod


def test_start_runs_jira_poller_when_enabled(tmp_path, monkeypatch):
    import no_human.intake.jira as jira_mod
    import no_human.intake.jira_poll as jira_poll_mod

    cfg = _make_start_cfg(tmp_path / "test.db", jira_enabled=True)
    cmd_mod = _patch_start_scaffolding(monkeypatch, cfg)

    mock_poller_instance = MagicMock()
    mock_poller_cls = MagicMock(return_value=mock_poller_instance)
    monkeypatch.setattr(jira_poll_mod, "JiraPoller", mock_poller_cls)
    monkeypatch.setattr(jira_mod, "JiraAdapter", MagicMock())
    # Stands in for the poller's polling loop being started — `nh serve` has
    # no `JiraPoller.start()` either; both drive `poller.tick()` through this
    # shared coroutine (verified: src/no_human/intake/jira_poll.py has no
    # `start` method).
    mock_poll_loop = AsyncMock(return_value=None)
    monkeypatch.setattr(cmd_mod, "_jira_poll_loop", mock_poll_loop)

    runner = CliRunner()
    result = runner.invoke(cli, ["start", "--no-open", "--port", "8420"])

    assert result.exit_code == 0, result.output
    mock_poller_cls.assert_called_once()
    mock_poll_loop.assert_called_once()
    assert mock_poll_loop.call_args.args[0] is mock_poller_instance
    assert "jira intake" in result.output.lower()


def test_start_skips_jira_poller_when_disabled(tmp_path, monkeypatch):
    import no_human.intake.jira as jira_mod
    import no_human.intake.jira_poll as jira_poll_mod

    cfg = _make_start_cfg(tmp_path / "test.db", jira_enabled=False)
    cmd_mod = _patch_start_scaffolding(monkeypatch, cfg)

    mock_poller_cls = MagicMock()
    monkeypatch.setattr(jira_poll_mod, "JiraPoller", mock_poller_cls)
    monkeypatch.setattr(jira_mod, "JiraAdapter", MagicMock())
    mock_poll_loop = AsyncMock(return_value=None)
    monkeypatch.setattr(cmd_mod, "_jira_poll_loop", mock_poll_loop)

    runner = CliRunner()
    result = runner.invoke(cli, ["start", "--no-open", "--port", "8420"])

    assert result.exit_code == 0, result.output
    mock_poller_cls.assert_not_called()
    mock_poll_loop.assert_not_called()
    assert "jira" not in result.output.lower()


def _make_start_cfg_linear(db_path: Path, *, linear_enabled: bool):
    """Same shape as _make_start_cfg, with Jira off so the two trackers'
    wiring can be asserted independently."""
    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data = {
            "server": {"port": 8420},
            "concurrency": {},
            "integrations": {
                "jira": {"enabled": False},
                "linear": {
                    "enabled": linear_enabled,
                    "team_key": "ENG",
                    "poll_interval": "5m",
                },
            },
        }

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db_path
    return _Cfg()


def test_start_runs_linear_poller_when_enabled(tmp_path, monkeypatch):
    import no_human.intake.linear as linear_mod
    import no_human.intake.linear_poll as linear_poll_mod

    cfg = _make_start_cfg_linear(tmp_path / "test.db", linear_enabled=True)
    cmd_mod = _patch_start_scaffolding(monkeypatch, cfg)

    mock_poller_instance = MagicMock()
    mock_poller_cls = MagicMock(return_value=mock_poller_instance)
    monkeypatch.setattr(linear_poll_mod, "LinearPoller", mock_poller_cls)
    monkeypatch.setattr(linear_mod, "LinearAdapter", MagicMock())
    # A real coroutine (not an AsyncMock) so the SHUTDOWN path is observable:
    # it records the stop event `start` handed it, and the assertion below
    # checks `start`'s finally-block actually set it. With an AsyncMock the
    # task completes instantly and a missing shutdown looks identical to a
    # working one.
    seen = {}

    async def fake_loop(poller, stop, poll_interval):
        seen["poller"] = poller
        seen["stop"] = stop
        seen["interval"] = poll_interval

    monkeypatch.setattr(cmd_mod, "_linear_poll_loop", fake_loop)

    runner = CliRunner()
    result = runner.invoke(cli, ["start", "--no-open", "--port", "8420"])

    assert result.exit_code == 0, result.output
    mock_poller_cls.assert_called_once()
    assert seen["poller"] is mock_poller_instance
    assert seen["interval"] == 300           # "5m", floored at 60s
    # `start` must stop the poll loop on the way out, or `nh start` would
    # leave a polling task running after the server exits.
    assert seen["stop"].is_set() is True
    assert "linear intake" in result.output.lower()
    # Jira is off in this config: the two trackers must be independent.
    assert "jira" not in result.output.lower()


def test_start_skips_linear_poller_when_disabled(tmp_path, monkeypatch):
    import no_human.intake.linear as linear_mod
    import no_human.intake.linear_poll as linear_poll_mod

    cfg = _make_start_cfg_linear(tmp_path / "test.db", linear_enabled=False)
    cmd_mod = _patch_start_scaffolding(monkeypatch, cfg)

    mock_poller_cls = MagicMock()
    monkeypatch.setattr(linear_poll_mod, "LinearPoller", mock_poller_cls)
    monkeypatch.setattr(linear_mod, "LinearAdapter", MagicMock())
    mock_poll_loop = AsyncMock(return_value=None)
    monkeypatch.setattr(cmd_mod, "_linear_poll_loop", mock_poll_loop)

    runner = CliRunner()
    result = runner.invoke(cli, ["start", "--no-open", "--port", "8420"])

    assert result.exit_code == 0, result.output
    mock_poller_cls.assert_not_called()
    mock_poll_loop.assert_not_called()
    assert "linear" not in result.output.lower()


# --------------------------------------------------------------------------- #
# nh start --workers N — the machine ceiling is printed, not silent            #
# --------------------------------------------------------------------------- #

def _make_start_cfg_concurrent(db_path: Path):
    """`nh start` scaffolding config with the pool switched ON, so the width
    reaches the ceiling instead of being downgraded by a switch first."""
    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data = {
            "server": {"port": 8420},
            "concurrency": {"enabled": True, "max_workers": 2},
            "integrations": {"jira": {"enabled": False},
                             "linear": {"enabled": False}},
        }

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db_path
    return _Cfg()


def test_start_prints_the_reason_when_it_clamps_the_worker_flag(tmp_path, monkeypatch):
    """`nh start --workers 64` was accepted in full and in silence. The clamp
    is only a guard if the operator is told the pool is not the width they
    asked for — otherwise the number they read back is their own flag."""
    import os as _os

    cfg = _make_start_cfg_concurrent(tmp_path / "test.db")
    _patch_start_scaffolding(monkeypatch, cfg)
    monkeypatch.setattr(_os, "cpu_count", lambda: 12)  # pin the ceiling at 4

    result = CliRunner().invoke(
        cli, ["start", "--no-open", "--port", "8420", "--workers", "64"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "ceiling" in out, out
    assert "64" in out and "sanity ceiling" in out, out
    # And the pool it announces is the clamped one, not the requested one.
    assert "4 worker(s)" in out, out


def test_start_does_not_clamp_or_warn_below_the_ceiling(tmp_path, monkeypatch):
    import os as _os

    cfg = _make_start_cfg_concurrent(tmp_path / "test.db")
    _patch_start_scaffolding(monkeypatch, cfg)
    monkeypatch.setattr(_os, "cpu_count", lambda: 12)

    result = CliRunner().invoke(
        cli, ["start", "--no-open", "--port", "8420", "--workers", "3"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "ceiling" not in out, out
    assert "3 worker(s)" in out, out


# --------------------------------------------------------------------------- #
# nh serve --max-workers N — the ceiling is printed on THIS path too           #
# --------------------------------------------------------------------------- #

def _patch_serve_scaffolding(monkeypatch, cfg):
    """`nh serve` up to the point it would start draining: config, the backend
    probe, and the event loop. `serve` builds its coroutine and hands it to
    `asyncio.run` — closing it instead runs the whole synchronous prelude (the
    resolve + the clamp print) and nothing after it."""
    import no_human.cli.commands as cmd_mod

    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(cmd_mod, "_assert_backend_usable", lambda: None)
    monkeypatch.setattr(cmd_mod, "_acquire_pid_lock", lambda: True)
    monkeypatch.setattr(cmd_mod, "_release_pid_lock", lambda: None)

    def _dont_run(coro):
        coro.close()          # no "never awaited" warning, no scheduler, no DB

    monkeypatch.setattr(asyncio, "run", _dont_run)
    return cmd_mod


def test_serve_prints_the_reason_when_it_clamps_the_worker_flag(tmp_path, monkeypatch):
    """`resolve_serve_pool` clamps `nh serve --max-workers 64` silently — the
    clamp is a downgrade, so it deliberately does NOT travel in `error` (which
    means "do not serve"), and `serve` prints the reason itself. Without an
    assertion on THAT print, `if _clamp_reason:` can be `if False:` and the
    operator is back to a 64-wide request quietly served 4 wide."""
    import os as _os

    cfg = _make_start_cfg_concurrent(tmp_path / "test.db")
    _patch_serve_scaffolding(monkeypatch, cfg)
    monkeypatch.setattr(_os, "cpu_count", lambda: 12)  # pin the ceiling at 4

    result = CliRunner().invoke(cli, ["serve", "--max-workers", "64"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "ceiling" in out, out
    assert "64" in out and "not 64" in out, out
    assert "sanity ceiling" in out, out
    # The pool serve is left holding is the clamped one, and the flag's
    # for-this-run-only override was written with THAT number, not 64.
    assert cfg.data["concurrency"]["max_workers"] == 4, cfg.data


def test_serve_does_not_warn_below_the_ceiling(tmp_path, monkeypatch):
    """The control: the reason is absent when nothing was clamped, so the
    assertion above is about the clamp and not about `serve` printing at all."""
    import os as _os

    cfg = _make_start_cfg_concurrent(tmp_path / "test.db")
    _patch_serve_scaffolding(monkeypatch, cfg)
    monkeypatch.setattr(_os, "cpu_count", lambda: 12)

    result = CliRunner().invoke(cli, ["serve", "--max-workers", "3"])
    out = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "ceiling" not in out, out
    assert cfg.data["concurrency"]["max_workers"] == 3, cfg.data


def test_serve_defaults_to_running_forever(tmp_path, monkeypatch):
    """`--until-empty` is opt-in. If its default ever flips, a plain `nh serve`
    left running overnight would exit the moment the queue emptied."""
    cfg = _make_start_cfg_concurrent(tmp_path / "test.db")
    _patch_serve_scaffolding(monkeypatch, cfg)
    params = {p.name: p for p in cli.commands["serve"].params}

    assert "until_empty" in params, "the drain-and-exit flag is missing"
    assert params["until_empty"].default is False
    assert params["until_empty"].is_flag

    # And the flagless path still exits 0 (the prelude runs; `_go` does not).
    result = CliRunner().invoke(cli, ["serve", "--max-workers", "1"])
    assert result.exit_code == 0, result.output


def test_serve_until_empty_exit_code_is_the_drains_verdict(tmp_path, monkeypatch):
    """The batch caller's only channel is the exit code, so `_go`'s return
    value must reach it. Without this, `serve` could compute a failure and
    still exit 0 — the exact silence KI-3 is about."""
    cfg = _make_start_cfg_concurrent(tmp_path / "test.db")
    _patch_serve_scaffolding(monkeypatch, cfg)

    def _run_returning_one(coro):
        coro.close()
        return 1

    monkeypatch.setattr(asyncio, "run", _run_returning_one)

    result = CliRunner().invoke(cli, ["serve", "--until-empty"])
    assert result.exit_code == 1, result.output


def test_serve_until_empty_EXITS_2_when_a_row_is_stranded(tmp_path, monkeypatch):
    """BEHAVIOURAL pin for the exit-2 operator contract, added because the
    help-text assertion below it is INERT: an independent review of #624
    deleted the whole `return 2` branch from `commands.py` and the changed
    test files still reported `224 passed`. A source-text guard cannot see a
    deleted branch — this drives the real `_go()` and reads the exit code and
    the message the operator actually gets.

    RED when the branch is removed: the run falls through to the signal check
    and exits 0 or 1, never 2, and the row id never reaches stderr."""
    import no_human.cli.commands as cmd_mod

    cfg = _make_start_cfg_concurrent(tmp_path / "test.db")
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(cmd_mod, "_assert_backend_usable", lambda: None)
    monkeypatch.setattr(cmd_mod, "_acquire_pid_lock", lambda: True)
    monkeypatch.setattr(cmd_mod, "_release_pid_lock", lambda: None)

    stranded = [{"task_id": "abcd1234ef567890", "status": "reviewing",
                 "seconds_until_claimable": 842.0}]

    class _Sched:
        drain_blocked_by = None

        def __init__(self, *a, **k):
            pass

        async def run_forever(self, *, stop=None, poll_interval=None,
                              until_empty=False):
            return None                    # the drain "completes" immediately

        async def failed_dispatched(self):
            return []                      # no FAILED task -> not exit 1

        async def unclaimable_orphans(self):
            return stranded                # ONE stranded row -> must be exit 2

        async def queue_is_drained(self):
            return False                   # as it is whenever a row is stranded

    # `serve` does `from ..core.scheduler import Scheduler` INSIDE the
    # function, so the name must be patched at its source module.
    import no_human.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "Scheduler", _Sched)
    # Store is REAL, against a tmp DB — only the scheduler is stubbed, so the
    # exit-code path under test is the shipped one.

    # click 8.3 dropped `mix_stderr`; this runner already merges the streams,
    # so `result.output` carries both the stdout claim and the stderr verdict.
    result = CliRunner().invoke(cli, ["serve", "--until-empty"])

    assert result.exit_code == 2, (result.exit_code, result.output)
    combined = result.output
    assert "abcd1234" in combined, combined       # names the row
    assert "not drained" in combined, combined    # says what is wrong
    # And it must NOT claim the queue drained on this path (MEDIUM-2 of the
    # same review: stdout printed "drained; stopped" immediately above
    # "not drained ...", re-creating the false signal in the log).
    assert "drained; stopped" not in combined, combined


def test_serve_until_empty_documents_the_not_yet_claimable_exit_code():
    """The operator contract for the stranded-row exit code is pinned in the
    flag's own --help text, not only in docs (MEDIUM-1 follow-up on #585) —
    a cron/CI operator scripting on exit codes reads `nh serve --help`, not
    the repo's markdown."""
    import re

    params = {p.name: p for p in cli.commands["serve"].params}
    help_text = params["until_empty"].help

    assert re.search(r"\b2\b", help_text), (
        f"help text never names exit code 2: {help_text!r}")
    assert "claimable" in help_text.lower(), (
        f"help text never explains what code 2 means: {help_text!r}")
    # And the existing 0/1 contract must still be documented alongside it.
    assert re.search(r"\b0\b", help_text)
    assert re.search(r"\b1\b", help_text)


# --------------------------------------------------------------------------- #
# nh stop                                                                      #
# --------------------------------------------------------------------------- #

def test_stop_waits_long_enough_for_a_real_drain():
    """3s was shorter than one Agent SDK turn, so `nh stop` SIGKILLed the very
    drain SIGTERM had just asked for. The default is now DERIVED — the
    server's `concurrency.stop_grace_s` plus a margin — so it cannot fall
    below the drain it waits for, whatever the grace is set to."""
    from no_human.cli.commands import _default_stop_timeout
    from no_human.core.scheduler import stop_grace_s
    click_default = {p.name: p for p in cli.commands["stop"].params}["timeout"].default
    assert click_default is None, "a literal default can disagree with the server's grace"
    default = _default_stop_timeout(None)
    assert default >= 30.0, f"nh stop --timeout default fell back to {default}s"
    assert default > stop_grace_s(None)
    assert _default_stop_timeout({"concurrency": {"stop_grace_s": 600}}) > 600

def _write_pidfile(home: Path, pid: int) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "nh.pid"
    path.write_text(str(pid))
    return path


def _patch_stop_home(monkeypatch, home: Path):
    import no_human.config as config_mod
    monkeypatch.setattr(config_mod, "NO_HUMAN_HOME", home)
    return CliRunner()


def test_stop_no_pidfile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    runner = _patch_stop_home(monkeypatch, home)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "not running" in result.output.lower()
    assert not (home / "nh.pid").exists()


def test_stop_stale_pid(tmp_path, monkeypatch):
    home = tmp_path / "home"
    pidfile = _write_pidfile(home, 424242)
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "stale" in result.output.lower()
    assert not pidfile.exists()


def test_stop_happy_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target_pid = 555
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    calls = []
    state = {"alive": True}

    def _fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            if not state["alive"]:
                raise ProcessLookupError()
            return
        if sig == signal.SIGTERM:
            state["alive"] = False
            return
        raise AssertionError(f"unexpected signal {sig}")

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output.lower()
    assert not pidfile.exists()
    sigterm_calls = [c for c in calls if c[1] == signal.SIGTERM]
    sigkill_calls = [c for c in calls if c[1] == signal.SIGKILL]
    assert sigterm_calls == [(target_pid, signal.SIGTERM)]
    assert sigkill_calls == []


def test_stop_wedged_escalates_to_sigkill(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target_pid = 777
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    calls = []
    state = {"killed": False}

    def _fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            if state["killed"]:
                raise ProcessLookupError()
            return  # still alive — never dies from SIGTERM alone
        if sig == signal.SIGKILL:
            state["killed"] = True
            return
        # SIGTERM: no-op, process stays wedged

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop", "--timeout", "0"])

    assert result.exit_code == 0, result.output
    assert "force-kill" in result.output.lower()
    assert not pidfile.exists()
    assert (target_pid, signal.SIGTERM) in calls
    assert (target_pid, signal.SIGKILL) in calls


def test_stop_only_targets_pidfile_pid(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target_pid = 999
    _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    calls = []
    state = {"alive": True}

    def _fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            if not state["alive"]:
                raise ProcessLookupError()
            return
        if sig == signal.SIGTERM:
            state["alive"] = False

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert calls, "expected os.kill to be called at least once"
    assert all(pid == target_pid for pid, _sig in calls)


@pytest.mark.parametrize("bad_pid", [-1, 0, 1])
def test_stop_rejects_corrupt_pid(tmp_path, monkeypatch, bad_pid):
    home = tmp_path / "home"
    pidfile = _write_pidfile(home, bad_pid)
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        raise AssertionError(f"must not signal corrupt pid {pid}")
    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "corrupt" in result.output.lower()
    assert not pidfile.exists()


def test_stop_rejects_self_pid(tmp_path, monkeypatch):
    home = tmp_path / "home"
    pidfile = _write_pidfile(home, os.getpid())
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        raise AssertionError(f"must not signal self pid {pid}")
    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "corrupt" in result.output.lower()
    assert not pidfile.exists()


def test_stop_permission_denied_keeps_pidfile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target_pid = 4242
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        raise PermissionError()
    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "another user" in result.output.lower()
    assert pidfile.exists()


def test_stop_race_exits_before_sigterm_delivered(tmp_path, monkeypatch):
    """Process exits between the liveness check and the SIGTERM call —
    os.kill(pid, SIGTERM) itself raises ProcessLookupError. Must be treated
    as a successful stop, not crash, and must not escalate to SIGKILL."""
    home = tmp_path / "home"
    target_pid = 321
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    calls = []

    def _fake_kill(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            return  # liveness check: still alive
        if sig == signal.SIGTERM:
            raise ProcessLookupError()  # gone by the time the signal lands
        raise AssertionError(f"unexpected signal {sig}")

    monkeypatch.setattr("os.kill", _fake_kill)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output.lower()
    assert not pidfile.exists()
    assert (target_pid, signal.SIGKILL) not in calls


def test_stop_sigkill_exhausted_keeps_pidfile(tmp_path, monkeypatch):
    """If the process is still alive after SIGKILL (shouldn't normally
    happen, but the wait is bounded), the pidfile must be left in place and
    the command must report failure — never claim success for a process
    that is still running."""
    home = tmp_path / "home"
    target_pid = 888
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    def _fake_kill(pid, sig):
        if sig == 0:
            return  # always alive, no matter what was sent
        # SIGTERM / SIGKILL: no-op, process never dies

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop", "--timeout", "0"])

    assert result.exit_code == 1, result.output
    assert "still running" in result.output.lower()
    assert pidfile.exists()


def test_stop_keeps_pidfile_while_process_alive(tmp_path, monkeypatch):
    """Pins that the pidfile is NOT removed until the process is confirmed
    gone — a mutation that unlinks right after sending SIGTERM (before
    confirming death) must fail this test."""
    home = tmp_path / "home"
    target_pid = 654
    pidfile = _write_pidfile(home, target_pid)
    runner = _patch_stop_home(monkeypatch, home)

    state = {"polls": 0}

    def _fake_kill(pid, sig):
        if sig == 0:
            state["polls"] += 1
            if state["polls"] == 1:
                return  # initial liveness check, before SIGTERM
            assert pidfile.exists(), "pidfile removed while process still alive"
            if state["polls"] < 3:
                return  # still alive for a couple of post-SIGTERM polls
            raise ProcessLookupError()
        elif sig == signal.SIGTERM:
            return
        else:
            raise AssertionError(f"unexpected signal {sig}")

    monkeypatch.setattr("os.kill", _fake_kill)
    monkeypatch.setattr("time.sleep", lambda s: None)

    result = runner.invoke(cli, ["stop"])

    assert result.exit_code == 0, result.output
    assert not pidfile.exists()


# --------------------------------------------------------------------------- #
# Resume provenance — every path, driven through the REAL command              #
#                                                                              #
# The zero-diff honesty gate credits work already ahead of base only when a     #
# HUMAN gated it, reading `resume_from.by`. Six review rounds kept trading one  #
# direction of a ONE-WAY LATCH for the other because the stamp was written      #
# inside `if checkpoint:`: a resume whose blocker recorded no sha wrote nothing, #
# and `merge_context` is RFC 7396, so the PREVIOUS actor's `by` survived to      #
# describe THIS resume. These drive the real commands, not the store helper —   #
# a round-5 test asserted this through `merge_context` directly and the whole    #
# suite stayed green with both CLI stamps deleted.                              #
# --------------------------------------------------------------------------- #

def _seed_parked_task(db_path: Path, status: TaskStatus, *,
                      checkpoint: bool, stale_by: str) -> str:
    """A parked task carrying a stale provenance marker from an earlier resume."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("resume provenance", repo_path="/tmp/repo")
            await s.create_task(t)
            blocker = {"category": "AMBIGUITY", "question": "which store?"}
            if checkpoint:
                blocker |= {"resume_branch": "scratch/x/abc-2",
                            "resume_commit": "75c68e08"}
            t.blocker = blocker
            await s.update_task(t)
            await s.merge_context(t.id, {
                "resume_reason": "wake_condition_satisfied",
                "resume_from": {"sha": "0e22fe3d", "branch": "old",
                                "by": stale_by},
            })
            await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def test_task_resume_stamps_provenance_with_NO_checkpoint(tmp_path, monkeypatch):
    """`nh task resume` on a blocker that recorded no sha still has to say who
    resumed it, or the stale machine marker fails the human's own resume."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=False, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "resume", task_id[:8]])

    assert result.exit_code == 0, result.output
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", (
        f"`nh task resume` skipped the stamp with no checkpoint: {resume_from}")


def test_nh_reply_stamps_provenance_with_NO_checkpoint(tmp_path, monkeypatch):
    """Same latch on `nh reply` — the path the blocker's own message promises."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=False, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reply", task_id[:8], "SQLite only", "--no-run"])

    assert result.exit_code == 0, result.output
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", (
        f"`nh reply` skipped the stamp with no checkpoint: {resume_from}")


def test_unblock_stamps_human_provenance(tmp_path, monkeypatch):
    """`nh unblock` re-enters the loop by hand and wrote no provenance at all,
    so whatever an earlier machine resume left behind described this human."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=True, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8]])

    assert result.exit_code == 0, result.output
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.IMPLEMENTING
    assert t.context["resume_from"].get("by") == "human", (
        f"`nh unblock` left a machine marker on a human's action: {t.context['resume_from']}")


def test_unblock_with_FAIL_does_not_claim_a_resume(tmp_path, monkeypatch):
    """Negative control: `--fail` abandons the task rather than resuming it, so
    it must NOT stamp a resume that never happened."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=True, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8], "--fail"])

    assert result.exit_code == 0, result.output
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.FAILED
    assert t.context["resume_from"].get("by") == "wake", (
        "--fail is not a resume and must leave provenance untouched: "
        f"{t.context['resume_from']}")


def test_reject_stamps_human_provenance(tmp_path, monkeypatch):
    """`nh reject` is the CLI twin of the drawer's Send back — a human gate."""
    db = tmp_path / "test.db"
    task_id = _seed_parked_task(db, TaskStatus.AWAITING_APPROVAL,
                                checkpoint=False, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "redo it"])

    assert result.exit_code == 0, result.output
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", (
        f"`nh reject` left a machine marker on a human's decision: {resume_from}")
    # It must NOT keep the sha the machine resume chose — see the send-back test
    # in test_api.py. Labelling another actor's sha "human" is the fail-OPEN
    # direction and opens a PR on work no attempt produced.
    assert resume_from.get("sha") is None, (
        f"`nh reject` inherited a sha it never chose: {resume_from}")


# --------------------------------------------------------------------------- #
# 🔴 THE WIRING TABLE — every resume entry point, driven for real.             #
#                                                                              #
# Eight rounds of review went past this because the enumeration of writers      #
# lived in COMMIT MESSAGES and DOCSTRINGS instead of in a test. Round 8 proved  #
# the cost: `nh reply` was named in prose as converted, was not, and shipped     #
# the fail-OPEN shape — a sha a MACHINE chose, relabelled `human`, which        #
# disarms the zero-diff honesty gate and opens a PR on work no attempt          #
# produced. Six more call sites had no guard against that regression at all:    #
# their tests asserted `by` and never `sha`, so reverting any of them to the    #
# old shape left the suite green.                                              #
#                                                                              #
# So the enumeration is now executable. Each case drives the REAL entry point   #
# on a task carrying a stale MACHINE checkpoint, and asserts the sha is not     #
# inherited. Adding a ninth resume path without adding it here is still         #
# possible — but silently reverting any of these eight is not.                 #
# --------------------------------------------------------------------------- #

def _sha_less_seed(db_path: Path, status: TaskStatus) -> str:
    """A parked task whose blocker holds NO checkpoint, carrying the residue of
    an earlier MACHINE resume that chose a sha of its own."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("wiring table", repo_path="/tmp/repo")
            await s.create_task(t)
            t.blocker = {"category": "AMBIGUITY", "question": "which store?"}
            await s.update_task(t)
            await s.merge_context(t.id, {
                "resume_reason": "wake_condition_satisfied",
                "resume_from": {"sha": "0e22fe3d", "branch": "old", "by": "wake"},
            })
            await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


@pytest.mark.parametrize("verb,args,status", [
    ("nh task resume", ["task", "resume"], TaskStatus.ESCALATED),
    ("nh reply", ["reply", "__ID__", "SQLite only", "--no-run"], TaskStatus.ESCALATED),
    ("nh unblock", ["unblock"], TaskStatus.ESCALATED),
    ("nh reject", ["reject", "__ID__", "--reason", "redo it"], TaskStatus.AWAITING_APPROVAL),
])
def test_no_cli_resume_path_inherits_a_sha_it_did_not_choose(
        verb, args, status, tmp_path, monkeypatch):
    """Whichever CLI verb re-enters the loop, it must not relabel a sha that a
    MACHINE resume chose. `nh reply` failed exactly this and shipped."""
    db = tmp_path / "test.db"
    task_id = _sha_less_seed(db, status)
    runner = _make_runner(db, monkeypatch)

    argv = [task_id[:8] if a == "__ID__" else a for a in args]
    if "__ID__" not in args:
        argv = argv + [task_id[:8]]
    result = runner.invoke(cli, argv)

    assert result.exit_code == 0, f"{verb}: {result.output}"
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", (
        f"{verb} left a machine marker describing a human's action: {resume_from}")
    assert resume_from.get("sha") is None, (
        f"{verb} INHERITED a sha it never chose and relabelled it human — this "
        f"is the fail-OPEN direction that opens a PR on work no attempt "
        f"produced: {resume_from}")


def test_unblock_REFUSES_a_live_task_and_leaves_provenance_alone(tmp_path, monkeypatch):
    """🔴 THE FAIL-OPEN HOLE `nh unblock` opened when it learned to read a
    checkpoint. It copied the drawer Resume's checkpoint read and NEITHER of the
    two guards that make it safe: the drawer refuses unless the task is parked,
    and the drawer clears the blocker.

    Without the first guard this fired on a LIVE attempt — implementing,
    reviewing, testing, awaiting_approval — re-applying a sha the WAKE WATCHER
    had chosen and stamping it `human`. An independent review reproduced the
    end state through `run_task`: an attempt that edited nothing was `succeeded`
    and the task advanced to `awaiting_approval`. A PR on work no attempt made.
    """
    for live in (TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
                 TaskStatus.TESTING, TaskStatus.AWAITING_APPROVAL):
        db = tmp_path / f"live-{live.value}.db"
        task_id = _seed_parked_task(db, live, checkpoint=True, stale_by="wake")
        runner = _make_runner(db, monkeypatch)

        result = runner.invoke(cli, ["unblock", task_id[:8]])

        assert result.exit_code == 0, result.output
        t = _get_task(db, task_id)
        assert t.status is live, (
            f"`nh unblock` re-entered a LIVE {live.value} task: now {t.status.value}")
        assert t.context["resume_from"].get("by") == "wake", (
            "`nh unblock` relabelled a machine's sha as human-gated on a live "
            f"attempt — the fail-OPEN direction: {t.context['resume_from']}")


def test_unblock_CONSUMES_the_checkpoint_so_it_cannot_be_reapplied(tmp_path, monkeypatch):
    """The second guard. A checkpoint must be consumable exactly ONCE, by the
    human who read it. Leaving the blocker in place made the same sha
    re-appliable forever, stamped `human` every time."""
    db = tmp_path / "consume.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=True, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    assert runner.invoke(cli, ["unblock", task_id[:8]]).exit_code == 0
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.IMPLEMENTING
    assert t.context["resume_from"].get("by") == "human"
    assert t.context["resume_from"].get("sha") == "75c68e08", t.context["resume_from"]
    assert t.blocker in (None, {}), (
        f"the blocker was not consumed, so its sha stays re-appliable: {t.blocker}")


@pytest.mark.parametrize("verb,args", [
    ("nh task resume", ["task", "resume"]),
    ("nh unblock", ["unblock"]),
])
def test_a_human_verb_adopts_only_the_checkpoint_IT_read(verb, args, tmp_path, monkeypatch):
    """The shape the sha-less wiring table structurally CANNOT see.

    Nine review rounds all checked the SHAPE of the write — is `by` present, is
    `sha` deleted — and none asked **who chose the sha that gets written**. The
    sha-less seed can only ever exercise the delete path. Here the blocker DOES
    carry a `resume_commit`, and `resume_from` already holds a DIFFERENT sha a
    machine picked. The human verb must adopt the one it read from the blocker,
    never the one left lying in context.
    """
    db = tmp_path / f"adopt-{args[-1]}.db"
    task_id = _seed_parked_task(db, TaskStatus.ESCALATED,
                                checkpoint=True, stale_by="wake")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [*args, task_id[:8]])

    assert result.exit_code == 0, f"{verb}: {result.output}"
    resume_from = _get_task(db, task_id).context["resume_from"]
    assert resume_from.get("by") == "human", resume_from
    assert resume_from.get("sha") == "75c68e08", (
        f"{verb} adopted a sha it never read — the seeded machine sha was "
        f"'0e22fe3d', the blocker's checkpoint is '75c68e08': {resume_from}")


# --------------------------------------------------------------------------- #
# human_event task_events — every human status-changing verb records WHO      #
# changed a task's status and what it changed FROM, in the same write.        #
# --------------------------------------------------------------------------- #

_HUMAN_VERB_BLOCKER = {"category": "AMBIGUITY", "question": "which store?"}


def _seed_human_verb_task(db_path: Path, status: TaskStatus, *,
                          blocker: dict | None) -> str:
    """Seed a task at `status`, optionally carrying `blocker`, with NO events —
    so a test can assert the verb under test wrote exactly one."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("human verb test", repo_path="/tmp/repo")
            await s.create_task(t)
            if blocker is not None:
                t.blocker = blocker
                await s.update_task(t)
            await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def test_task_resume_emits_human_resume_event_with_prior_status_and_blocker(
        tmp_path, monkeypatch):
    """The acceptance test: `nh task resume` on an ESCALATED task with a
    blocker moves it to IMPLEMENTING (as today) AND now records a
    `source=human`, `kind=human_resume` task_event carrying `prior_status`
    and the full prior blocker JSON — before this fix it wrote nothing."""
    db = tmp_path / "test.db"
    task_id = _seed_human_verb_task(
        db, TaskStatus.ESCALATED, blocker=_HUMAN_VERB_BLOCKER)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "resume", task_id[:8]])

    assert result.exit_code == 0, result.output
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.IMPLEMENTING, t.status

    events = _list_events(db, task_id)
    human = [e for e in events if e.get("source") == "human"]
    assert len(human) == 1, f"expected exactly one human event, got: {events}"
    ev = human[0]
    assert ev["kind"] == "human_resume", ev
    assert ev["prior_status"] == "escalated", ev
    assert ev["prior_blocker"] == _HUMAN_VERB_BLOCKER, ev


@pytest.mark.parametrize(
    "verb,seed_status,blocker,argv,expected_status,expected_kind,stub_no_server", [
        ("nh task resume", TaskStatus.ESCALATED, _HUMAN_VERB_BLOCKER,
         ["task", "resume", "__ID__"], TaskStatus.IMPLEMENTING, "human_resume", False),
        ("nh task pause", TaskStatus.IMPLEMENTING, None,
         ["task", "pause", "__ID__", "--reason", "hold"],
         TaskStatus.BLOCKED, "human_pause", True),
        ("nh task retry", TaskStatus.FAILED, _HUMAN_VERB_BLOCKER,
         ["task", "retry", "__ID__"], TaskStatus.PENDING, "human_retry", False),
        ("nh task cancel", TaskStatus.BLOCKED, _HUMAN_VERB_BLOCKER,
         ["task", "cancel", "__ID__", "--reason", "stop"],
         TaskStatus.FAILED, "human_cancel", False),
        ("nh unblock", TaskStatus.ESCALATED, _HUMAN_VERB_BLOCKER,
         ["unblock", "__ID__"], TaskStatus.IMPLEMENTING, "human_unblock", False),
        ("nh reject", TaskStatus.AWAITING_APPROVAL, None,
         ["reject", "__ID__", "--reason", "redo it"],
         TaskStatus.IMPLEMENTING, "human_reject", False),
        ("nh reply", TaskStatus.ESCALATED, _HUMAN_VERB_BLOCKER,
         ["reply", "__ID__", "SQLite only", "--no-run"],
         TaskStatus.IMPLEMENTING, "human_reply", False),
    ])
def test_every_human_verb_emits_its_shared_human_event(
        verb, seed_status, blocker, argv, expected_status, expected_kind,
        stub_no_server, tmp_path, monkeypatch):
    """Table-driven, one row per human status-changing verb: every one of them
    must go through the SAME `human_event()` emitter in `blockers/taxonomy.py`
    — never a differently-shaped, independently-invented event. Only the
    seed/args/expected outcome vary per row; the shape asserted at the bottom
    never does. `nh task resume` wrote no event at all before this fix; a
    prior attempt at this same ticket wired only the CLI half and left the
    board's API endpoints (which share no code with these commands) silently
    unwired despite comments claiming parity — this table pins the CLI side,
    `tests/test_api.py` pins the API twins."""
    db = tmp_path / f"verb-{expected_kind}.db"
    task_id = _seed_human_verb_task(db, seed_status, blocker=blocker)
    runner = _make_runner(db, monkeypatch)
    if stub_no_server:
        monkeypatch.setattr("no_human.cli.commands._server_owns_worker", lambda cfg: False)

    argv = [task_id[:8] if a == "__ID__" else a for a in argv]
    result = runner.invoke(cli, argv)

    assert result.exit_code == 0, f"{verb}: {result.output}"
    t = _get_task(db, task_id)
    assert t.status is expected_status, f"{verb}: {t.status}"

    events = _list_events(db, task_id)
    human = [e for e in events if e.get("source") == "human"]
    assert len(human) == 1, f"{verb}: expected exactly one human event, got: {events}"
    ev = human[0]
    assert ev["kind"] == expected_kind, f"{verb}: {ev}"
    assert ev["prior_status"] == seed_status.value, f"{verb}: {ev}"
    if blocker is not None:
        assert ev.get("prior_blocker") == blocker, f"{verb}: {ev}"


# --------------------------------------------------------------------------- #
# restore-approval: event write moved INTO set_status's one transaction       #
# (b404b872, MEDIUM-2) — matching every other human verb above. Before this   #
# fix, each repair branch wrote its `state_repaired` event via a SEPARATE     #
# `store.save_events` call made AFTER `set_status`; a process death or a      #
# concurrent write landing between the two calls left the status change on   #
# record with no trace of who/why (the same lost-write class as a4a666b0).   #
# --------------------------------------------------------------------------- #

def _seed_restore_approval_escalated(
        db_path: Path, *, pr_url="https://example.invalid/pr/42") -> str:
    """ESCALATED with `pr_watch` context + a `pr_open` event — the exact
    precondition the ESCALATED/FAILED tail branch of `restore-approval`
    requires (`task_has_pr_evidence` + a `PR_EVENT_KINDS` event on record)."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("escalated with an open PR", repo_path="/tmp/repo")
            t.context = {"pr_watch": pr_url}
            await s.create_task(t)
            await s.save_events(t.id, [{
                "source": "watcher", "kind": "pr_open", "text": pr_url,
                "ts": 0.0}])
            await s.set_status(t, TaskStatus.ESCALATED, validate=False)
            return t.id
    return asyncio.run(_go())


def _seed_restore_approval_false_done(
        db_path: Path, *, pr_url="https://example.invalid/pr/43") -> str:
    """DONE with a PR still open and no completion event on record — the
    false-done repair shape (task 8c8b36b5's incident; see
    `tests/test_false_done_completion.py::_seed_false_done_cli`), reaching
    DONE the same way a pre-fix build did: a direct SQL write that bypasses
    `set_status`'s `SilentCompletion` guard."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("false done with an open PR", repo_path="/tmp/repo")
            t.context = {"pr_watch": pr_url}
            await s.create_task(t)
            await s.save_events(t.id, [{
                "source": "watcher", "kind": "pr_open", "text": pr_url,
                "ts": 0.0}])
            await s.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            await s.db.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (TaskStatus.DONE.value, t.id))
            await s.db.commit()
            return t.id
    return asyncio.run(_go())


def _seed_restore_approval_blocked(
        db_path: Path, *, pr_url="https://example.invalid/pr/44",
        blocker: dict | None = None) -> str:
    """BLOCKED after a PASSING review with an open PR — the stranded-post-pass
    incident shape (`tests/test_approve.py::_blocked_task_with`)."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("blocked post-pass", repo_path="/tmp/repo")
            t.context = {"pr_watch": pr_url}
            await s.create_task(t)
            await s.save_events(t.id, [{
                "source": "watcher", "kind": "pr_open", "text": pr_url,
                "ts": 0.0}])
            aid = await s.create_attempt(t.id, 1)
            await s.update_attempt(aid, review_passed=1)
            t.blocker = dict(blocker or _HUMAN_VERB_BLOCKER)
            await s.update_task_columns(t)
            await s.set_status(t, TaskStatus.BLOCKED, validate=False,
                               human_override=True)
            return t.id
    return asyncio.run(_go())


def _raising_save_events(*a, **kw):
    raise AssertionError(
        "save_events must not be called — the repair event belongs in "
        "set_status's own transaction")


@pytest.mark.parametrize("branch,seed_fn", [
    ("done", _seed_restore_approval_false_done),
    ("blocked", _seed_restore_approval_blocked),
    ("escalated", _seed_restore_approval_escalated),
])
def test_restore_approval_records_its_event_when_save_events_is_dead(
        branch, seed_fn, tmp_path, monkeypatch):
    """RED on main: each repair branch recorded its event with a SEPARATE
    `store.save_events` call made AFTER `set_status` — a lost write if the
    process dies or a concurrent write lands between the two. The fix folds
    the event into `set_status(event=...)`'s own transaction, matching every
    other human verb (PR #567). With `Store.save_events` monkeypatched to
    raise, main's leftover call blows up; the fixed code never calls
    `save_events` for these branches at all, so the stub never fires and the
    event still lands via `set_status`."""
    db = tmp_path / f"restore-{branch}.db"
    task_id = seed_fn(db)
    monkeypatch.setattr(Store, "save_events", _raising_save_events)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(
        cli, ["task", "restore-approval", task_id[:8], "--reason", "repair"])

    assert result.exit_code == 0, f"{branch}: {result.output}"
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.AWAITING_APPROVAL, f"{branch}: {t.status}"

    events = _list_events(db, task_id)
    human = [e for e in events if e.get("source") == "human"]
    assert len(human) == 1, \
        f"{branch}: expected exactly one human event, got: {events}"
    assert human[0]["kind"] == "human_restore_approval", f"{branch}: {human[0]}"


def test_restore_approval_event_carries_the_prior_state(tmp_path, monkeypatch):
    """AC2: the repair event must carry `prior_status`/`prior_blocker` as
    they stood BEFORE the repair — `human_event` requires the caller to read
    them off the task before `set_status` mutates it, since an overwritten
    value cannot be recovered afterward."""
    db = tmp_path / "restore-prior-state.db"
    blocker = {"category": "AMBIGUITY", "question": "which store?",
              "wake_condition": "after:2h"}
    task_id = _seed_restore_approval_blocked(db, blocker=blocker)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "task", "restore-approval", task_id[:8],
        "--reason", "conflict round paused"])

    assert result.exit_code == 0, result.output
    events = _list_events(db, task_id)
    human = [e for e in events if e.get("source") == "human"]
    assert len(human) == 1, events
    ev = human[0]
    assert ev["kind"] == "human_restore_approval", ev
    assert ev["prior_status"] == "blocked", ev
    assert ev["prior_blocker"] == blocker, ev
    assert ev["reason"] == "conflict round paused", ev
    assert ev["actor"] == "cli", ev
    text = ev["text"]
    assert "https://example.invalid/pr/44" in text, text
    assert "PASS" in text, text
    assert "after:2h" in text, text


def test_task_retry_clears_the_checkpoint_like_its_HTTP_twin(tmp_path, monkeypatch):
    """`nh task retry` is the CLI twin of `POST /api/tasks/{id}/retry`, down to
    the docstring. The endpoint was fixed to clear `resume_from`; this was not,
    and a review reproduced the end state through `run_task` — an attempt that
    edited nothing came back `succeeded` and advanced to `awaiting_approval`,
    credited with a [WIP-PARTIAL] an EARLIER actor's resume had chosen.

    🔴 That was the FOURTH time in this branch a fix landed on one half of a
    pair: `nh reply` behind the reply endpoint, `nh unblock` behind the Resume
    endpoint's guards, and here. When a CLI verb and an HTTP endpoint share a
    docstring, they share an invariant.
    """
    db = tmp_path / "retry.db"

    async def _seed():
        async with Store(db) as s:
            t = Task.new("retry twin", repo_path="/tmp/repo")
            await s.create_task(t)
            await s.merge_context(t.id, {
                "resume_from": {"sha": "75c68e08", "branch": "old", "by": "human"}})
            await s.set_status(t, TaskStatus.FAILED, validate=False)
            return t.id
    task_id = asyncio.run(_seed())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "retry", task_id[:8]])

    assert result.exit_code == 0, result.output
    t = _get_task(db, task_id)
    assert t.status is TaskStatus.PENDING
    assert (t.context or {}).get("resume_from") is None, (
        "`nh task retry` inherited a checkpoint it never chose, so a 'fresh "
        f"run' branches from a stale sha: {(t.context or {}).get('resume_from')}")


# --------------------------------------------------------------------------- #
# print_path_error — an error that names a path must reproduce it verbatim     #
# --------------------------------------------------------------------------- #

def _render(prefix: str, detail: str, width: int) -> str:
    """Render one print_path_error call at a fixed console width."""
    from io import StringIO

    from rich.console import Console

    from no_human.cli import print_path_error

    buf = StringIO()
    print_path_error(Console(file=buf, width=width, no_color=True), prefix, detail)
    return buf.getvalue()


def test_print_path_error_never_folds_a_path_mid_token():
    """Rich's default rendering breaks a token longer than the line, so an
    80-column terminal — also Rich's fallback for any pipe, log file or CI
    runner — printed `.../metrics\\ndb-service` and the user could not copy the
    path out of the error. The path must come back whole."""
    path = "/tmp/pytest-of-runner/pytest-0/popen-gw3/test_task_add_rejects_a_linked0/metrics-core-service"
    assert len(path) > 80
    out = _render("[red]multi-repo intake:[/]", f"not a git repo: {path}", 80)
    assert path in out, out
    assert "metrics\ndb-service" not in out


def test_print_path_error_keeps_square_brackets_in_a_path():
    """A directory named `a[b]c` is a legal path. Read as console markup it
    renders as `ac`, so the error reports a path that does not exist."""
    path = "/home/dev/proj[b]/repo"
    out = _render("[red]not a git repo:[/]", path, 200)
    assert path in out, out


def test_review_comments_shows_every_severity_not_just_uppercase():
    """A model-authored severity must be visible, whatever its case.

    `nh review-comments` built the label as f" [{severity}]" — wrapping model
    output in square brackets does not decorate it, rich PARSES it as a markup
    tag. Every realistic value ("high", "medium", "blocking") was silently
    swallowed; only an uppercase one survived, by accident of not being a valid
    tag. The field a human reads first to triage a review was invisible, and
    the command looked like it was working.
    """
    from rich.console import Console
    from rich.markup import escape
    import io

    def render(sev: str) -> str:
        buf = io.StringIO()
        c = Console(file=buf, width=100, no_color=True, highlight=False)
        label = f" \\[{escape(str(sev))}]" if sev else ""
        c.print(f"  [bold]1.[/] [dim]draft[/] [cyan]app.py:12[/]{label}", emoji=False)
        return buf.getvalue()

    for sev in ("high", "medium", "blocking", "HIGH"):
        assert sev in render(sev), f"severity {sev!r} was swallowed by the renderer"


def test_review_comments_renders_model_text_literally():
    """Mutation guard for the test above.

    That test only proves the severity survives. The comment body is also model
    authored, and carries the two shapes that bite: rich markup, and a file:line
    citation that emoji substitution rewrites (`:100:` becomes an emoji), which
    would destroy the evidence the review gate is built on.
    """
    from rich.console import Console
    from rich.markup import escape
    import io

    def render(text: str) -> str:
        buf = io.StringIO()
        c = Console(file=buf, width=100, no_color=True, highlight=False)
        c.print(f"     {escape(str(text))}", emoji=False)
        return buf.getvalue()

    assert "the list[/] was empty" in render("the list[/] was empty")
    assert "commands.py:100:" in render("see commands.py:100: here")
    assert ":warning:" in render("see :warning: for details")


# --------------------------------------------------------------------------- #
# nh task show — which code produced each attempt's verdict                    #
# --------------------------------------------------------------------------- #

def test_task_show_prints_the_recorded_loaded_code(tmp_path, monkeypatch):
    """The honest CLI surface: the value the SERVER stamped on the attempt.

    Deliberately a pure DB read. `nh` is its own process, so anything this
    command measured about its own checkout would describe the CLI and not the
    server that judged the attempt — the same borrowed-provenance trap the
    `_is_tracked` fix closed, wearing a different hat.
    """
    db = tmp_path / "test.db"

    async def _seed():
        async with Store(db) as s:
            t = Task.new("a task", repo_path="/tmp/r")
            await s.create_task(t)
            attempt_id = await s.create_attempt(t.id, 1)
            await s.update_attempt(attempt_id,
                                   loaded_code_version="git:" + "b" * 40)
            return t.id

    task_id = asyncio.run(_seed())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "git:" + "b" * 40 in result.output, result.output


def test_task_show_says_nothing_when_no_code_was_recorded(tmp_path, monkeypatch):
    """Attempts predating the column are NULL. Print nothing rather than
    invent a value or render a bare `code: None` that reads as a finding."""
    db = tmp_path / "test.db"

    async def _seed():
        async with Store(db) as s:
            t = Task.new("older task", repo_path="/tmp/r")
            await s.create_task(t)
            attempt_id = await s.create_attempt(t.id, 1)
            await s.update_attempt(attempt_id, loaded_code_version=None)
            return t.id

    task_id = asyncio.run(_seed())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "code:" not in result.output, result.output


# --------------------------------------------------------------------------- #
# nh --help — the epilog that names the docs                                   #
# --------------------------------------------------------------------------- #

def test_root_help_names_the_docs_url():
    """`nh --help` is the whole manual a bundle ships, so it must name /docs.

    Pinned because this exact string has already regressed once on this
    branch: it landed as /docs.html, which only reaches the page through a
    307. Assert the canonical URL, not a substring of the host, so that
    swapping it back for a redirect fails here instead of in a user's shell.
    """
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Docs: https://getnohuman.com/docs" in result.output, result.output


def test_unblock_fail_on_a_live_task_leaves_a_pending_stop_for_its_worker(tmp_path, monkeypatch):
    """`nh unblock --fail` parks rather than re-enters, and is legal on a LIVE
    task — where the flag may be the pause its worker is about to honour.
    Clearing it there lets the coder run on; only the IMPLEMENTING branch
    withdraws it (re-entry registry: placement, not presence)."""
    import asyncio
    from no_human.core.db import Store
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)

    async def _flag():
        async with Store(db) as store:
            await store.request_cancel(task_id, "Paused from board")
    asyncio.run(_flag())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8], "--fail"])

    assert result.exit_code == 0, result.output
    async def _read():
        async with Store(db) as store:
            return await store.get_cancel_request(task_id)
    assert asyncio.run(_read()) == "Paused from board"


def test_reject_withdraws_both_human_stop_signals(tmp_path, monkeypatch):
    """A board-Paused task (flag + durable `blocker.human_stopped`) that is
    sent back must not run while still stamped 'stopped by you' nor park on
    turn zero from the stale flag."""
    import asyncio
    from no_human.core.db import Store
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)

    async def _arm():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            t.blocker = {"category": "USER_PAUSED", "question": "Paused from board",
                         "human_stopped": True}
            await store.update_task_columns(t)
            await store.request_cancel(task_id, "Paused from board")
    asyncio.run(_arm())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "redo it"])

    assert result.exit_code == 0, result.output
    async def _read():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            return t.status, t.blocker, await store.get_cancel_request(task_id)
    status, blocker, flag = asyncio.run(_read())
    assert status == TaskStatus.IMPLEMENTING
    assert blocker is None and flag is None


def test_unblock_withdraws_a_pending_stop_on_the_re_entry_branch(tmp_path, monkeypatch):
    """Positive twin of the --fail pin: plain `nh unblock` (re-entry) DOES
    withdraw the flag — the structural registry check cannot tell the two
    arms apart, so both behaviours are pinned explicitly."""
    import asyncio
    from no_human.core.db import Store
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)

    async def _flag():
        async with Store(db) as store:
            await store.request_cancel(task_id, "Paused from board")
    asyncio.run(_flag())
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["unblock", task_id[:8]])

    assert result.exit_code == 0, result.output
    async def _read():
        async with Store(db) as store:
            return (await store.get_task(task_id)).status, await store.get_cancel_request(task_id)
    status, flag = asyncio.run(_read())
    assert status == TaskStatus.IMPLEMENTING and flag is None


def test_task_pause_without_a_server_carries_the_checkpoint(tmp_path, monkeypatch):
    """R1's second writer: `nh task pause` with no server parks directly and
    used to drop the task's checkpoint; it now carries it."""
    import asyncio
    from no_human.core.db import Store
    from no_human.blockers import resume_checkpoint
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)

    async def _arm():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            t.blocker = {"category": "CI_GATE", "resume_commit": "c" * 40, "resume_branch": "no-human/v"}
            await store.update_task_columns(t)
    asyncio.run(_arm())
    runner = _make_runner(db, monkeypatch)
    monkeypatch.setattr("no_human.cli.commands._server_owns_worker", lambda cfg: False)

    result = runner.invoke(cli, ["task", "pause", task_id[:8], "--reason", "hold"])

    assert result.exit_code == 0, result.output
    assert resume_checkpoint(_get_task(db, task_id).blocker) == {"sha": "c" * 40, "branch": "no-human/v"}
