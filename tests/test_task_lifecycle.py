"""Tests for `nh task pause/resume/cancel/retry` and `nh config show`.

CLI commands call asyncio.run() internally, so tests must be synchronous.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.profile import ProjectProfile


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _seed_task(db_path: Path, status: TaskStatus, *, title="Test task") -> str:
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path="/tmp/repo")
            await s.create_task(t)
            event = {"source": "test", "kind": "test_seed"} if status is TaskStatus.DONE else None
            await s.set_status(t, status, validate=False, event=event)
            return t.id
    return asyncio.run(_go())


def _get_task(db_path: Path, task_id: str) -> Task:
    async def _go():
        async with Store(db_path) as s:
            return await s.find_task(task_id)
    return asyncio.run(_go())


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

    _Cfg.db_path = path

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    # `_server_owns_worker` really does HTTP to 127.0.0.1:8420, so pause/cancel
    # would branch on whether a server happens to be running on the developer's
    # machine — green with none up, red with one. Default to "no server"; the
    # tests about the handover set it True themselves.
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: False)
    return CliRunner()


# --------------------------------------------------------------------------- #
# nh task pause                                                                #
# --------------------------------------------------------------------------- #


def test_pause_active_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "paused" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.BLOCKED
    assert t.blocker["category"] == "USER_PAUSED"


def test_pause_already_parked(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "cannot pause" in result.output


def test_pause_done_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "cannot pause" in result.output


def test_pause_unknown_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.IMPLEMENTING)  # ensure DB exists
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "pause", "nonexistent"], catch_exceptions=False)
    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# nh task resume                                                               #
# --------------------------------------------------------------------------- #


def test_resume_blocked_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "resume", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "resumed" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.IMPLEMENTING
    assert t.blocker is None


def test_resume_active_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "resume", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "only parked tasks" in result.output


# --------------------------------------------------------------------------- #
# nh task cancel                                                               #
# --------------------------------------------------------------------------- #


def test_cancel_active_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "cancel", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "cancelled" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.FAILED
    assert t.context["cancel_reason"] == "cancelled by user"


def test_cancel_with_reason(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(
        cli, ["task", "cancel", task_id, "--reason", "no longer needed"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    t = _get_task(db, task_id)
    assert t.context["cancel_reason"] == "no longer needed"


def test_cancel_already_done(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "cancel", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "already done" in result.output


# --------------------------------------------------------------------------- #
# nh task retry                                                                #
# --------------------------------------------------------------------------- #


def test_retry_failed_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.FAILED)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "retry", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "retried" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.PENDING
    assert "retried_at" in t.context


def test_retry_active_task_rejected(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "retry", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "only failed tasks" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.IMPLEMENTING  # unchanged


# --------------------------------------------------------------------------- #
# nh config show                                                               #
# --------------------------------------------------------------------------- #


def test_config_show(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"git": {"never_push_to": ["main"]}}))

    import no_human.cli.commands as cmd_mod
    import no_human.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    # The config command imports CONFIG_PATH from config module at call time

    runner = CliRunner()
    result = runner.invoke(cli, ["config", "show"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "never_push_to" in result.output


def test_config_show_key(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"git": {"never_push_to": ["main", "master"]}}))

    import no_human.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["config", "show", "--key", "git"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "never_push_to" in result.output


def test_config_path(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.touch()

    import no_human.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["config", "path"], catch_exceptions=False)
    assert result.exit_code == 0
    # Rich may wrap the long path; check the filename appears.
    assert "config.yaml" in result.output


# --------------------------------------------------------------------------- #
# nh task add — linked-repo validation (D19)                                   #
# --------------------------------------------------------------------------- #


def test_task_add_rejects_a_linked_repo_that_is_not_a_checkout(tmp_path, monkeypatch):
    """D19: click already rejects a *missing* --linked-repo (Path(exists=True)).
    A path that exists but is not a git checkout used to sail through intake and
    then be dropped by a bare `continue` mid-attempt, after the planner had
    already written a plan naming its files.

    The console width is pinned because it decides whether this test is testing
    anything. Rich falls back to 80 columns whenever stdout is not a terminal —
    a pipe, a log file, every CI runner — and its default rendering folds a
    token longer than the line, so the reported path came back with a newline
    inside it. Unpinned, tmp_path's length alone decides where that fold lands
    and therefore whether the assertion below notices: this test passed on
    macOS and failed on Linux for no reason other than that.
    """
    monkeypatch.setenv("COLUMNS", "80")
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    primary = tmp_path / "primary"
    (primary / ".git").mkdir(parents=True)
    not_a_checkout = tmp_path / "metrics-core-service"
    not_a_checkout.mkdir()
    # pytest's tmp_path is always well past 80 characters, so the rendered
    # message cannot fit on one 80-column line — the path has to survive being
    # wrapped, which is the point.
    assert len(str(not_a_checkout)) > 80

    result = runner.invoke(cli, [
        "task", "add", "--title", "multi-repo task",
        "--repo", str(primary),
        "--linked-repo", str(not_a_checkout),
        "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 1
    assert "not a git repo" in result.output
    assert "metrics-core-service" in result.output
    # The whole path, verbatim and unbroken — a path the user cannot copy out
    # of the error is not a usable error message.
    assert str(not_a_checkout) in result.output


# --------------------------------------------------------------------------- #
# nh task add — kind/criteria consistency guard (defect 204f2177)             #
# --------------------------------------------------------------------------- #


def test_task_add_refuses_design_doc_kind_with_test_bearing_criteria(tmp_path, monkeypatch):
    """Red-first repro of the live defect: `--kind design_doc` paired with
    criteria demanding a CLI flag + tests must be refused at intake, not
    silently accepted and later marked DONE on a report-only path that never
    ships the demanded artifact."""
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    result = runner.invoke(cli, [
        "task", "add", "--title", "Add nh approve --landed",
        "--repo", str(repo), "--kind", "design_doc",
        "--criteria", "a red-first test proves the flag's behaviour",
        "--criteria", "the CLI flag lands the PR",
        "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 1
    assert "intake refused" in result.output
    assert "design_doc" in result.output

    async def _go():
        async with Store(db) as s:
            return await s.list_tasks()
    tasks = asyncio.run(_go())
    assert tasks == [], "no task should have been created when intake refuses"


def test_task_add_accepts_design_doc_kind_with_prose_only_criteria(tmp_path, monkeypatch):
    """Control: a genuine design_doc ticket with prose-only criteria (no
    test-bearing signal) is accepted at intake exactly as before."""
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    result = runner.invoke(cli, [
        "task", "add", "--title", "Write a design doc for the retention pipeline",
        "--repo", str(repo), "--kind", "design_doc",
        "--criteria", "document covers options and a recommendation",
        "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    task_id = _created_task_id(result.output)

    async def _go():
        async with Store(db) as s:
            return await s.find_task(task_id)
    t = asyncio.run(_go())
    assert t is not None
    assert t.kind == "design_doc"


# --------------------------------------------------------------------------- #
# nh task add — plain-text intake (bare sentence, no --title)                  #
# --------------------------------------------------------------------------- #


def test_task_add_files_a_bare_sentence_as_a_freeform_task(tmp_path, monkeypatch):
    """A plain sentence positional SOURCE files a freeform task using the
    sentence as the title, instead of failing with 'not a recognized task
    URL/id' — the error that used to point users at Jira/issue-URL intake
    even for the plain-text case the conversational shell handles."""
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    result = runner.invoke(cli, [
        "task", "add", "Fix the flaky E2E test",
        "--repo", str(repo), "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "created task" in result.output
    assert "not a recognized task URL/id" not in result.output

    async def _go():
        async with Store(db) as s:
            return await s.list_tasks()
    tasks = asyncio.run(_go())
    assert len(tasks) == 1
    assert tasks[0].title == "Fix the flaky E2E test"


def test_task_add_still_refuses_a_bare_tracker_key(tmp_path, monkeypatch):
    """URL/id intake behaviour is unchanged: a source-shaped token that is
    not an issue URL is still refused, not filed as plain text."""
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    result = runner.invoke(cli, [
        "task", "add", "PROJ-42",
        "--repo", str(repo), "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 1
    assert "intake failed" in result.output

    async def _go():
        async with Store(db) as s:
            return await s.list_tasks()
    tasks = asyncio.run(_go())
    assert tasks == []


def test_task_add_bare_sentence_accepts_criteria(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    result = runner.invoke(cli, [
        "task", "add", "Add greet(name)",
        "--repo", str(repo), "--criteria", "returns hi, X",
        "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    task_id = _created_task_id(result.output)

    async def _go():
        async with Store(db) as s:
            return await s.find_task(task_id)
    t = asyncio.run(_go())
    assert t is not None
    assert t.acceptance_criteria == ["returns hi, X"]


def test_task_add_bare_sentence_and_title_together_is_refused(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    result = runner.invoke(cli, [
        "task", "add", "Add greet(name)", "--title", "Add greet(name)",
        "--repo", str(repo), "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 1
    assert "not both" in result.output

    async def _go():
        async with Store(db) as s:
            return await s.list_tasks()
    tasks = asyncio.run(_go())
    assert tasks == []


def test_task_add_help_documents_the_plain_text_form():
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "add", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "plain sentence" in result.output
    assert "PROJ-42" in result.output


# --------------------------------------------------------------------------- #
# nh task add — repo default budgets (SCRUM-48)                                #
# --------------------------------------------------------------------------- #

def _created_task_id(output: str) -> str:
    match = re.search(r"created task ([0-9a-f]{8})", output)
    assert match, f"no 'created task <id>' line in output:\n{output}"
    return match.group(1)


# --------------------------------------------------------------------------- #
# nh task add — --follows (issue #232)                                        #
# --------------------------------------------------------------------------- #

def test_task_add_follows_links_to_an_existing_task(tmp_path, monkeypatch):
    """The gap issue #232 names directly: `nh task add --help` had no way to
    set `follows_id`, even though the column, model, and API were all wired.
    A unique id prefix (the same convenience every other TASK_ID argument
    takes) must resolve, not just a full id."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    runner = _make_runner(db, monkeypatch)

    predecessor = _seed_task(db, TaskStatus.AWAITING_APPROVAL, title="original task")

    result = runner.invoke(cli, [
        "task", "add", "--title", "Follow-up on the original",
        "--repo", str(repo), "--follows", predecessor[:8],
        "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert f"follows:[/] {predecessor[:8]}" in result.output or predecessor[:8] in result.output
    task_id = _created_task_id(result.output)

    t = _get_task(db, task_id)
    assert t.follows_id == predecessor


def test_task_add_follows_a_bogus_id_is_refused(tmp_path, monkeypatch):
    """A dangling follows_id could never warn `nh approve` against anything —
    refuse at intake, the same way an unresolvable TASK_ID refuses elsewhere,
    and create no task at all."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "task", "add", "--title", "Follow-up on nothing",
        "--repo", str(repo), "--follows", "deadbeef",
        "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 1

    async def _go():
        async with Store(db) as s:
            return await s.list_tasks()
    assert asyncio.run(_go()) == []


def test_task_add_help_documents_follows(tmp_path, monkeypatch):
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "add", "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "--follows" in result.output


def test_task_add_applies_repo_default_budgets(tmp_path, monkeypatch):
    """SCRUM-48: `nh task add` must merge repo default budgets in, same as
    the web create path (api/app.py) and the Jira poller already do."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()

    async def _seed():
        async with Store(db) as s:
            await s.upsert_profile(ProjectProfile(
                repo_path=str(repo.resolve()),
                default_attempt_tokens=6_000_000,
                default_lifetime_tokens=16_000_000,
            ))
    asyncio.run(_seed())

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, [
        "task", "add", "--title", "Heavy repo task",
        "--repo", str(repo), "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    task_id = _created_task_id(result.output)
    task = _get_task(db, task_id)
    assert task.config["attempt_tokens"] == 6_000_000
    assert task.config["lifetime_tokens"] == 16_000_000


def test_task_add_explicit_config_overrides_repo_defaults(tmp_path, monkeypatch):
    """An explicit key already on the task's config by the time `task add`
    creates it (e.g. a future explicit-budget flag) must survive the
    repo-defaults merge untouched, while an unset key still gets filled in."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()

    async def _seed():
        async with Store(db) as s:
            await s.upsert_profile(ProjectProfile(
                repo_path=str(repo.resolve()),
                default_attempt_tokens=6_000_000,
                default_lifetime_tokens=16_000_000,
            ))
    asyncio.run(_seed())

    from no_human.core.task import Task as TaskCls
    orig_new = TaskCls.new

    def _new_with_explicit_override(*a, **kw):
        t = orig_new(*a, **kw)
        t.config["attempt_tokens"] = 999
        return t

    monkeypatch.setattr(TaskCls, "new", _new_with_explicit_override)

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, [
        "task", "add", "--title", "Override task",
        "--repo", str(repo), "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    task_id = _created_task_id(result.output)
    task = _get_task(db, task_id)
    assert task.config["attempt_tokens"] == 999            # explicit wins, not clobbered
    assert task.config["lifetime_tokens"] == 16_000_000    # untouched key still gets the default


# --------------------------------------------------------------------------- #
# Duplicate execution: the server's scheduler already claims the task          #
# --------------------------------------------------------------------------- #


def test_server_owns_worker_is_false_when_nothing_is_listening(tmp_path, monkeypatch):
    """A failed HTTP probe with no live pidfile means "no server" — a false
    positive would silently strand the task, a false negative only restores
    the old behavior. NO_HUMAN_HOME is isolated to an empty tmp dir so this
    doesn't depend on whether the operator's own `nh start`/`nh serve` happens
    to be running (the pidfile fallback the HTTP probe now falls back to)."""
    import no_human.cli.commands as cmd_mod
    import no_human.config as config_mod

    monkeypatch.setattr(config_mod, "NO_HUMAN_HOME", tmp_path / "home")

    class _Cfg(dict):
        def get(self, k, d=None):
            return {"server": {"host": "127.0.0.1", "port": 1}}.get(k, d)

    assert cmd_mod._server_owns_worker(_Cfg()) is False


def test_reply_does_not_run_the_task_when_the_server_is_up(tmp_path, monkeypatch):
    """`nh reply` sets the task to IMPLEMENTING, which scheduler._CLAIMABLE picks
    up. Running it in-process too gave task 84251cb2 two orchestrators on one git
    checkout — duplicate commit/reviewing events and a doubled escalation."""
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)

    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: True)
    ran: list[str] = []
    monkeypatch.setattr(cmd_mod, "_build_orchestrator",
                        lambda *a, **k: ran.append("built") or (_ for _ in ()).throw(
                            AssertionError("orchestrator must not run in-process")))

    result = runner.invoke(cli, ["reply", task_id, "go on"], catch_exceptions=False)

    assert result.exit_code == 0
    assert ran == [], "the CLI ran the task while the server owned it"
    assert "picked it up" in result.output


def _cancel_flag(db_path: Path, task_id: str) -> str | None:
    async def _go():
        async with Store(db_path) as s:
            return await s.get_cancel_request(task_id)
    return asyncio.run(_go())


def test_pause_defers_the_status_to_the_running_server(tmp_path, monkeypatch):
    """With a server up, the orchestrator owns the task's status. The CLI raises
    the stop flag and stops there; writing BLOCKED from here would race the
    attempt that is still running and lose its [WIP-BLOCKED] checkpoint."""
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: True)

    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    assert _cancel_flag(db, task_id) == "user paused via CLI"
    assert _get_task(db, task_id).status is TaskStatus.IMPLEMENTING


def test_pause_without_a_server_parks_the_task_itself(tmp_path, monkeypatch):
    """No server means nothing is running, so this process is the only writer:
    park the task and consume the flag in one go."""
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: False)

    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    assert _get_task(db, task_id).status is TaskStatus.BLOCKED
    assert _cancel_flag(db, task_id) is None


def test_resume_withdraws_a_pending_stop(tmp_path, monkeypatch):
    """Otherwise the next attempt honours the stale flag and parks straight back."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)

    async def _flag():
        async with Store(db) as s:
            await s.request_cancel(task_id, "user paused")
    asyncio.run(_flag())

    result = runner.invoke(cli, ["task", "resume", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    assert _cancel_flag(db, task_id) is None
    assert _get_task(db, task_id).status is TaskStatus.IMPLEMENTING


def test_resume_continues_from_the_blockers_checkpoint(tmp_path, monkeypatch):
    """`nh task resume` used to clear the blocker without reading its
    resume_commit, so the next attempt branched from a stale `resume_from` and
    silently discarded the parked attempt's committed work (task 84251cb2,
    attempt 11: a correct pagination fix at 06cd40fc)."""
    from no_human.blockers.taxonomy import Blocker, BlockerCategory

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED)

    async def _park():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            b = Blocker(category=BlockerCategory.NOVEL_UNKNOWN, transient=False,
                        confidence=0.9, goal="g", root_cause_hypothesis="exhausted")
            b.resume_commit = "06cd40fc" * 5
            b.resume_branch = "dev"
            t.blocker = b.to_dict()
            t.context = {"resume_from": {"sha": "0e22fe3d" * 5, "branch": "dev"}}
            await s.update_task(t)
    asyncio.run(_park())

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "resume", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    t = _get_task(db, task_id)
    assert t.context["resume_from"]["sha"] == "06cd40fc" * 5
    assert t.status is TaskStatus.IMPLEMENTING


# --------------------------------------------------------------------------- #
# nh task add — the two onboarding papercuts (walkthrough Q3, Q10)             #
# --------------------------------------------------------------------------- #

def test_task_add_skips_the_grill_when_there_is_no_terminal(tmp_path, monkeypatch):
    """The intake grill asks one question at a time at a `click.prompt`. Over a
    pipe there is nobody to answer, so `nh task add … --no-run` died on
    `Your answer []: Aborted!` (walkthrough B9/Q10) — the quickstart's own
    step-4 example is not scriptable.

    Bare `nh` already refuses a terminal it does not have rather than hanging;
    this is the same predicate, so there is one answer to "is anyone there".
    """
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    runner = _make_runner(db, monkeypatch)

    import no_human.cli.commands as cmd_mod

    async def _never(*a, **kw):
        raise AssertionError("the grill ran with no terminal to answer it")

    monkeypatch.setattr(cmd_mod, "_run_cli_grill", _never)
    monkeypatch.setattr(cmd_mod, "stdio_is_interactive", lambda: False)

    result = runner.invoke(cli, [
        "task", "add", "--title", "piped task", "--repo", str(repo), "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "skipping the scoping questions" in result.output, result.output
    assert "created task" in result.output, result.output


def test_task_add_still_grills_when_a_terminal_is_there(tmp_path, monkeypatch):
    """The control: the skip must be the TTY check doing it, not the grill
    having quietly stopped running for everyone."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    runner = _make_runner(db, monkeypatch)

    import no_human.cli.commands as cmd_mod
    grilled: list[str] = []

    async def _fake_grill(config, task, store=None):
        grilled.append(task.title)
        return task

    monkeypatch.setattr(cmd_mod, "_run_cli_grill", _fake_grill)
    monkeypatch.setattr(cmd_mod, "stdio_is_interactive", lambda: True)

    result = runner.invoke(cli, [
        "task", "add", "--title", "interactive task", "--repo", str(repo),
        "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert grilled == ["interactive task"], result.output
    assert "skipping the scoping questions" not in result.output, result.output


@pytest.mark.parametrize("ambient_width", [None, 80], ids=["default", "ci-80-cols"])
def test_the_unonboarded_repo_hint_prints_a_sequence_that_works(
        tmp_path, monkeypatch, ambient_width):
    """The hint named only the SECOND half of a two-step dance: following
    `nh onboard <repo> --confirm` verbatim answers `no profile to confirm —
    run nh onboard <repo> first` (walkthrough B11/Q3).

    The `ci-80-cols` case is the ubuntu runner, which kept CI red for five
    landings. Rich resolves its width in ``Console.__init__`` — a COLUMNS in
    the ambient environment is baked into ``console._width`` when
    ``no_human.cli.commands`` is imported, and a later
    ``monkeypatch.setenv("COLUMNS", ...)`` cannot move it. The runner has such
    a COLUMNS and this machine does not, so the hint folded mid-command there
    and nowhere else, and both `nh onboard` lines came back as the bare word
    `nh onboard` with the path on a line of its own.

    So the assertions below test the invariant — two DISTINCT lines, each a
    whole copy-able command — and the hint is printed ``soft_wrap=True`` so no
    console width can fold it. Pinning ``_width`` is exactly what an inherited
    COLUMNS does, so this parametrization reproduces the runner rather than
    describing it.
    """
    if ambient_width is not None:
        import no_human.cli.commands as cmd_mod
        monkeypatch.setattr(cmd_mod.console, "_width", ambient_width)
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "task", "add", "--title", "unonboarded", "--repo", str(repo),
        "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "repo profile not usable" in result.output, result.output
    steps = [ln.strip() for ln in result.output.splitlines() if "nh onboard" in ln]
    assert len(steps) == 2, f"the hint is still one step:\n{result.output}"
    # Two DISTINCT steps: a fold produced two identical 'nh onboard' lines,
    # which satisfied the count above while printing no runnable command.
    assert steps[0] != steps[1], steps
    assert "--confirm" not in steps[0], steps
    assert steps[0].startswith(f"nh onboard {repo.resolve()}"), steps
    assert steps[1].startswith(f"nh onboard {repo.resolve()} --confirm"), steps
