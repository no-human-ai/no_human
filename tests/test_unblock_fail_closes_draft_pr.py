"""Tests for Issue #656: `nh unblock --fail` closes the task's outstanding draft PR.

The blocker funnel keeps a parked task's draft open on purpose, because the task
may resume. `nh unblock --fail` is where it stops being able to, so it must run
the same close-out `nh task cancel` does (`blockers/cancel_pr_closeout.py`):
retitle + close the draft, retire the draft slot, record `pr_draft_abandoned`,
never touch a delivered PR, and never let a forge error fail the command.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from click.testing import CliRunner

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs import comment_poster

pytestmark = pytest.mark.usefixtures("isolated_env_file")

URL = "https://github.com/org/repo/pull/656"
BRANCH = "nh/task-656"


class FakeForge:
    def __init__(self):
        self.closes: list[str] = []
        self.titles: list[tuple[str, str]] = []

    def close_pr(self, url: str) -> dict[str, Any]:
        self.closes.append(url)
        return {"ok": True, "error": ""}

    def set_pr_title(self, url: str, title: str) -> dict[str, Any]:
        self.titles.append((url, title))
        return {"ok": True, "error": ""}


class _StubCfg:
    primary_model = "claude-sonnet-4-6"
    review_model = "claude-sonnet-4-6"
    data: dict = {}
    db_path = None

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


def _seed(db_path, context, status=TaskStatus.BLOCKED) -> str:
    async def go():
        async with Store(db_path) as s:
            t = Task.new("Fix authentication bug", repo_path="/tmp/repo")
            t.external_id = "TASK-656"
            t.context = dict(context)
            await s.create_task(t)
            await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(go())


def _load(db_path, task_id):
    async def go():
        async with Store(db_path) as s:
            return await s.find_task(task_id), await s.list_events(task_id)
    return asyncio.run(go())


def _run_unblock(tmp_path, monkeypatch, args, *, close=None, set_title=None):
    import no_human.cli.commands as cmd_mod

    forge = FakeForge()
    monkeypatch.setattr(comment_poster, "close_pr", close or forge.close_pr)
    monkeypatch.setattr(comment_poster, "set_pr_title", set_title or forge.set_pr_title)
    cfg = _StubCfg()
    cfg.db_path = tmp_path / "test.db"
    monkeypatch.setattr(cmd_mod, "load_config", lambda: cfg)
    result = CliRunner().invoke(cmd_mod.cli, ["unblock", *args], catch_exceptions=False)
    return result, forge


def _abandon_events(events):
    return [e for e in events if e.get("kind") == "pr_draft_abandoned"]


def test_unblock_fail_retitles_and_closes_parked_draft(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    task_id = _seed(db_path, {"pr_draft_created": URL, "pr_draft_branch": BRANCH})

    result, forge = _run_unblock(tmp_path, monkeypatch, [task_id, "--fail"])

    assert result.exit_code == 0, result.output
    assert "failed" in result.output
    assert forge.closes == [URL]
    assert len(forge.titles) == 1
    t_url, title = forge.titles[0]
    assert t_url == URL
    assert title.startswith("[ABANDONED — not delivered] ")
    assert "TASK-656: Fix authentication bug" in title

    fresh, events = _load(db_path, task_id)
    assert fresh.status is TaskStatus.FAILED
    assert "pr_draft_created" not in (fresh.context or {})
    assert "pr_draft_branch" not in (fresh.context or {})
    assert fresh.context.get("abandoned_pr_urls") == [URL]
    abandoned = _abandon_events(events)
    assert len(abandoned) == 1
    assert abandoned[0]["pr_url"] == URL
    assert "nh unblock --fail" in abandoned[0]["reason"]
    assert abandoned[0]["retitle_ok"] is True
    assert abandoned[0]["close_ok"] is True


@pytest.mark.parametrize("delivered", [
    {"pr_watch": URL},
    {"pr_delivered_url": URL},
    {"pr_branch": BRANCH},
])
def test_unblock_fail_never_touches_a_delivered_pr(tmp_path, monkeypatch, delivered):
    db_path = tmp_path / "test.db"
    task_id = _seed(db_path, {"pr_draft_created": URL, "pr_draft_branch": BRANCH,
                              **delivered},
                    status=TaskStatus.ESCALATED)

    result, forge = _run_unblock(tmp_path, monkeypatch, [task_id, "--fail"])

    assert result.exit_code == 0, result.output
    assert forge.closes == []
    assert forge.titles == []
    fresh, events = _load(db_path, task_id)
    assert fresh.status is TaskStatus.FAILED
    assert fresh.context.get("pr_draft_created") == URL
    assert _abandon_events(events) == []


def test_unblock_fail_forge_error_still_ends_failed(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    task_id = _seed(db_path, {"pr_draft_created": URL, "pr_draft_branch": BRANCH},
                    status=TaskStatus.AWAITING_INPUT)

    def boom(*_a, **_k):
        raise RuntimeError("forge unreachable: 502 bad gateway")

    result, _ = _run_unblock(tmp_path, monkeypatch, [task_id, "--fail"],
                             close=boom, set_title=boom)

    assert result.exit_code == 0, result.output
    fresh, events = _load(db_path, task_id)
    assert fresh.status is TaskStatus.FAILED
    assert "pr_draft_created" not in (fresh.context or {})
    assert fresh.context.get("abandoned_pr_urls") == [URL]
    abandoned = _abandon_events(events)
    assert len(abandoned) == 1
    assert abandoned[0]["close_ok"] is False
    assert "502 bad gateway" in abandoned[0]["close_error"]
    assert abandoned[0]["retitle_ok"] is False


def test_unblock_resume_keeps_the_draft_open(tmp_path, monkeypatch):
    # Resuming is the case the funnel kept the draft open FOR; only --fail retires it.
    db_path = tmp_path / "test.db"
    task_id = _seed(db_path, {"pr_draft_created": URL, "pr_draft_branch": BRANCH})

    result, forge = _run_unblock(tmp_path, monkeypatch, [task_id])

    assert result.exit_code == 0, result.output
    assert forge.closes == []
    assert forge.titles == []
    fresh, events = _load(db_path, task_id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context.get("pr_draft_created") == URL
    assert _abandon_events(events) == []
