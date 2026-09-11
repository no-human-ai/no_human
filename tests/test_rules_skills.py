"""Tests for `nh rules` and `nh skills` CLI commands."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

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
    return CliRunner()


def _list_memories(db_path: Path, **kwargs):
    async def _go():
        async with Store(db_path) as s:
            return await s.list_memories(**kwargs)
    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# nh rules                                                                     #
# --------------------------------------------------------------------------- #


def test_rules_add_and_list(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    # Add a rule.
    result = runner.invoke(
        cli, ["rules", "add", "--title", "Never auto-merge",
              "--content", "Agent must never merge PRs", "--tag", "safety"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "added" in result.output

    # List rules.
    result = runner.invoke(cli, ["rules", "list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Never auto-merge" in result.output

    # Verify in DB — confirmed.
    mems = _list_memories(db, confirmed=True, mem_type="rule")
    assert len(mems) == 1
    assert mems[0]["title"] == "Never auto-merge"
    assert mems[0]["source"] == "manual"


def test_rules_remove(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    # Add a rule.
    runner.invoke(
        cli, ["rules", "add", "--title", "Test rule", "--content", "Remove me"],
        catch_exceptions=False,
    )
    mems = _list_memories(db, confirmed=True, mem_type="rule")
    assert len(mems) == 1
    rule_id = mems[0]["id"]

    # Remove by prefix.
    result = runner.invoke(cli, ["rules", "remove", rule_id[:8]], catch_exceptions=False)
    assert result.exit_code == 0
    assert "removed" in result.output

    # Verify gone.
    mems = _list_memories(db, confirmed=True, mem_type="rule")
    assert len(mems) == 0


def test_rules_list_empty(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["rules", "list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "no confirmed rules" in result.output


# --------------------------------------------------------------------------- #
# nh skills                                                                    #
# --------------------------------------------------------------------------- #


def test_skills_add_and_list(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(
        cli, ["skills", "add", "--title", "Fix flaky tests",
              "--content", "Always add retry logic for timing-sensitive tests",
              "--tag", "testing", "--tag", "reliability"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "added" in result.output

    result = runner.invoke(cli, ["skills", "list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Fix flaky tests" in result.output


def test_skills_remove(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    runner.invoke(
        cli, ["skills", "add", "--title", "Temp skill", "--content", "Goes away"],
        catch_exceptions=False,
    )
    mems = _list_memories(db, confirmed=True, mem_type="skill")
    assert len(mems) == 1
    skill_id = mems[0]["id"]

    result = runner.invoke(cli, ["skills", "remove", skill_id[:8]], catch_exceptions=False)
    assert result.exit_code == 0
    assert "removed" in result.output


def test_skills_list_empty(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["skills", "list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "no confirmed skills" in result.output


# --------------------------------------------------------------------------- #
# B3: nh skills propose — agent-proposed, human-confirmed                     #
# --------------------------------------------------------------------------- #

def test_skills_propose_queues_unconfirmed(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(
        cli, ["skills", "propose", "--title", "Retry flaky Jenkins polls",
              "--content", "Poll status 3x with backoff before failing.",
              "--tag", "ci"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "proposed" in result.output.lower()

    # Queued, NOT confirmed — must not appear as an active/confirmed skill.
    confirmed = _list_memories(db, confirmed=True, mem_type="skill")
    assert confirmed == []
    pending = _list_memories(db, confirmed=False, source="proposed", mem_type="skill")
    assert len(pending) == 1
    assert pending[0]["title"] == "Retry flaky Jenkins polls"
    assert pending[0]["source"] == "proposed"


def test_skills_propose_does_not_appear_in_skills_list(tmp_path, monkeypatch):
    """`skills list` shows only CONFIRMED skills — a proposal is inert until
    a human runs `nh learnings --confirm`."""
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    runner.invoke(
        cli, ["skills", "propose", "--title", "Unconfirmed idea",
              "--content", "Not yet trusted."],
        catch_exceptions=False,
    )
    result = runner.invoke(cli, ["skills", "list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "no confirmed skills" in result.output
    assert "Unconfirmed idea" not in result.output


def test_skills_propose_then_confirm_promotes_it(tmp_path, monkeypatch):
    """The proposal reaches the SAME confirm gate as post-task learnings —
    `nh learnings --confirm` promotes it to an active, confirmed skill."""
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    runner.invoke(
        cli, ["skills", "propose", "--title", "Reusable approach",
              "--content", "Do X before Y."],
        catch_exceptions=False,
    )
    pending = _list_memories(db, confirmed=False, source="proposed")
    assert len(pending) == 1
    mem_id = pending[0]["id"]

    result = runner.invoke(cli, ["learnings", "--confirm", mem_id[:8]],
                           catch_exceptions=False)
    assert result.exit_code == 0
    assert "confirmed" in result.output.lower()

    confirmed = _list_memories(db, confirmed=True, mem_type="skill")
    assert len(confirmed) == 1
    assert confirmed[0]["title"] == "Reusable approach"


# --------------------------------------------------------------------------- #
# PR3: Progressive skill disclosure — discover_skills unit tests               #
# --------------------------------------------------------------------------- #

def test_discover_skills_from_project_dir(tmp_path):
    """Skills in a project .claude/skills/ dir are discovered."""
    from no_human.history.skills import discover_skills

    proj_skills = tmp_path / ".claude" / "skills" / "test-skill"
    proj_skills.mkdir(parents=True)
    (proj_skills / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\nDetails here."
    )

    found = discover_skills(extra_roots=[tmp_path / ".claude" / "skills"])
    names = [s.name for s in found]
    assert "test-skill" in names


def test_discover_skills_dedup_across_roots(tmp_path):
    """Skill with the same name in two roots is deduplicated."""
    from no_human.history.skills import discover_skills

    r1 = tmp_path / "root1" / "my-skill"
    r1.mkdir(parents=True)
    (r1 / "SKILL.md").write_text("---\nname: shared\ndescription: first\n---\n")

    r2 = tmp_path / "root2" / "my-skill"
    r2.mkdir(parents=True)
    (r2 / "SKILL.md").write_text("---\nname: shared\ndescription: second\n---\n")

    found = discover_skills(extra_roots=[tmp_path / "root1", tmp_path / "root2"])
    shared = [s for s in found if s.name == "shared"]
    assert len(shared) == 1  # deduped


# --- PR-D: skill materialization ------------------------------------------ #


def test_materialize_skills_writes_skill_md(tmp_path):
    """_materialize_skills writes SKILL.md for confirmed DB skills."""
    from no_human.core.orchestrator import Orchestrator

    # Fake orchestrator with _active_memories containing a skill.
    class FakeOrch:
        _active_memories = [
            {"type": "skill", "title": "pr-review", "content": "Always check the diff."},
            {"type": "rule", "title": "no-force-push", "content": "Never force push."},
        ]
        _discovered_skills = []

        def emit(self, *a, **kw):
            pass

    orch = FakeOrch()
    names = Orchestrator._materialize_skills(orch, tmp_path)
    assert "pr-review" in names
    skill_file = tmp_path / ".claude" / "skills" / "pr-review" / "SKILL.md"
    assert skill_file.exists()
    content = skill_file.read_text(encoding="utf-8")
    assert "Always check the diff." in content
    assert "name: pr-review" in content
    # Rules are not materialized as skills.
    assert not (tmp_path / ".claude" / "skills" / "no-force-push").exists()


def test_materialize_skills_skips_existing(tmp_path):
    """Existing skill files on disk are not overwritten."""
    from no_human.core.orchestrator import Orchestrator

    # Pre-create a skill on disk.
    skill_dir = tmp_path / ".claude" / "skills" / "existing" / "SKILL.md"
    skill_dir.parent.mkdir(parents=True)
    skill_dir.write_text("---\nname: existing\n---\nOriginal content.\n")

    class FakeOrch:
        _active_memories = [
            {"type": "skill", "title": "existing", "content": "New content."},
        ]
        _discovered_skills = []

        def emit(self, *a, **kw):
            pass

    orch = FakeOrch()
    names = Orchestrator._materialize_skills(orch, tmp_path)
    assert "existing" in names
    # Content must NOT be overwritten.
    assert "Original content." in skill_dir.read_text(encoding="utf-8")


def test_materialize_skills_sanitizes_names(tmp_path):
    """Skill names with slashes/spaces are sanitized for filesystem."""
    from no_human.core.orchestrator import Orchestrator

    class FakeOrch:
        _active_memories = [
            {"type": "skill", "title": "my cool/skill", "content": "body"},
        ]
        _discovered_skills = []

        def emit(self, *a, **kw):
            pass

    orch = FakeOrch()
    names = Orchestrator._materialize_skills(orch, tmp_path)
    assert "my-cool_skill" in names
    assert (tmp_path / ".claude" / "skills" / "my-cool_skill" / "SKILL.md").exists()


def test_materialize_practice_skills_writes_tdd_debug_done(tmp_path):
    """1.5: the coder gets concise TDD / systematic-debugging / verify-before-
    done skills on disk, each a valid SKILL.md with frontmatter."""
    from no_human.core.orchestrator import Orchestrator
    orch = object.__new__(Orchestrator)  # method uses only the class attr + log
    orch._materialize_practice_skills(tmp_path)
    skills_dir = tmp_path / ".claude" / "skills"
    for name in ("no_human_tdd", "no_human_debug", "no_human_done", "no_human_focus"):
        sk = skills_dir / name / "SKILL.md"
        assert sk.exists(), f"{name} not materialized"
        assert sk.read_text(encoding="utf-8").startswith(f"---\nname: {name}\ndescription: ")
    # systematic-debugging is the net-new lever: root-cause, don't patch-guess
    debug = (skills_dir / "no_human_debug" / "SKILL.md").read_text(encoding="utf-8").lower()
    assert "root cause" in debug and "patch-guess" in debug


def test_review_url_queues_a_code_review_task(tmp_path, monkeypatch):
    """2.1: `nh review <pr-url>` queues a standalone code_review task."""
    db = tmp_path / "t.db"
    runner = _make_runner(db, monkeypatch)
    url = "https://github.com/org/repo/pull/42"
    res = runner.invoke(cli, ["review", url, "--repo", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "queued code review" in res.output

    async def _tasks():
        async with Store(db) as s:
            return await s.list_tasks()
    tasks = asyncio.run(_tasks())
    assert len(tasks) == 1
    assert tasks[0].kind == "code_review"
    assert url in (tasks[0].description or "")


def test_review_non_url_target_is_treated_as_task_id(tmp_path, monkeypatch):
    """A non-URL target still shows a checklist / reports no match (not queued)."""
    db = tmp_path / "t.db"
    runner = _make_runner(db, monkeypatch)
    res = runner.invoke(cli, ["review", "deadbeef"])
    assert "queued code review" not in res.output
    assert "no task matching" in res.output


def test_investigate_creates_an_investigation_task(tmp_path, monkeypatch):
    """2.6: `nh investigate <q>` queues a read-only investigation task."""
    db = tmp_path / "t.db"
    runner = _make_runner(db, monkeypatch)
    res = runner.invoke(cli, ["investigate", "why does the export drop rows?",
                              "--repo", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "investigating" in res.output

    async def _tasks():
        async with Store(db) as s:
            return await s.list_tasks()
    tasks = asyncio.run(_tasks())
    assert len(tasks) == 1 and tasks[0].kind == "investigation"


def test_investigate_show_prints_the_report(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    runner = _make_runner(db, monkeypatch)

    async def _seed():
        from no_human.core.task import Task
        async with Store(db) as s:
            t = Task.new("q", repo_path="/tmp/r", kind="investigation")
            t.context = {"findings": "ROOT CAUSE: the consumer offset reset."}
            await s.create_task(t)
            return t.id
    tid = asyncio.run(_seed())
    res = runner.invoke(cli, ["investigate", "--show", tid[:8]])
    assert "ROOT CAUSE" in res.output
