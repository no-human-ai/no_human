"""Caller-wiring for `profile_test_cmd`: BOTH `land_task` callers — `nh
approve` (cli/commands.py) and the board's Approve button
(api/app.py::_merge_task_pr) — must resolve the repo profile's own proven
test command through `core.profile_resolve.resolve_repo_test_cmd` (the SAME
resolution the Orchestrator already uses) and hand it to `land_task` as
`profile_test_cmd`, instead of silently dropping it and leaving the
merge-time gate to always fall back to a bare `python -m pytest` regardless
of what the repo's own profile says.

Deliberately a SEPARATE file from tests/test_merge_policy_wiring.py (per
explicit review send-back on a prior attempt at this task, which had instead
deleted ~19 unrelated tests from that file) — this file adds two new tests
and touches nothing else. The merge-time gate's OWN branching logic (pytest
vs non-pytest profile commands, the focused/full gate decision, fail-closed
runner-start failures, exit-code-5 handling, etc.) is covered end-to-end in
tests/test_approve_merge.py; these two tests only check that each caller
actually resolves and forwards `profile_test_cmd` — they mock `land_task`
itself so a caller-wiring regression can't hide behind a real merge's other
preconditions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs.approve_merge import LandResult

# Fixtures/helpers re-exported on purpose — see
# tests/test_approve_merge_identity_repro.py for the same pattern.
from tests.test_approve_merge import (  # noqa: F401
    _fetch_task_and_events,
    _make_cli_runner,
    _seed_land_task,
    land_env,
)


# --------------------------------------------------------------------------- #
# CLI: `nh approve`                                                            #
# --------------------------------------------------------------------------- #

def test_cli_approve_passes_the_profile_test_command_to_land_task(
        land_env, tmp_path, monkeypatch):
    """`nh approve` must resolve the repo's own proven test command via
    `core.profile_resolve.resolve_repo_test_cmd` and pass it through to
    `land_task` as `profile_test_cmd` — not drop it and let the merge gate
    silently fall back to a bare `python -m pytest`."""
    branch, head_sha = land_env.cut_branch("no-human/t-cliprofilewiring")
    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
    )

    sentinel_cmd = "uv run pytest -q -n 4 --custom-flag"

    async def _fake_resolve_repo_test_cmd(store, config, repo_path):
        return sentinel_cmd

    monkeypatch.setattr(
        "no_human.core.profile_resolve.resolve_repo_test_cmd",
        _fake_resolve_repo_test_cmd,
    )

    calls = []

    def _fake_land_task(**kwargs):
        calls.append(kwargs)
        return LandResult(
            ok=True, step="close_pr", landed_sha="cafed00d1234",
            pr_url=kwargs["pr_url"], branch=kwargs["branch"], message="landed",
        )

    monkeypatch.setattr("no_human.vcs.approve_merge.land_task", _fake_land_task)

    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1, "nh approve must call land_task exactly once"
    assert calls[0]["profile_test_cmd"] == sentinel_cmd, (
        "nh approve must forward the repo profile's resolved test command "
        "to land_task, not drop it or pass None")

    t, _events = _fetch_task_and_events(db, task_id)
    assert t.status is TaskStatus.DONE


def test_cli_approve_falls_back_to_none_when_the_repo_has_no_profile_command(
        land_env, tmp_path, monkeypatch):
    """Symmetric case: a repo with no proven test command (resolution
    returns None, e.g. no usable profile yet) must still land — `land_task`
    itself is the one that falls back to `python -m pytest` in that case,
    the caller must just pass None through rather than inventing a value."""
    branch, head_sha = land_env.cut_branch("no-human/t-clinoprofile")
    db = tmp_path / "t.db"
    task_id = _seed_land_task(
        db, TaskStatus.AWAITING_APPROVAL, repo_path=str(land_env.clone),
        branch=branch, pr_url=land_env.pr_url,
        review_history=[{"sha": head_sha, "passed": True}],
    )

    async def _fake_resolve_repo_test_cmd(store, config, repo_path):
        return None

    monkeypatch.setattr(
        "no_human.core.profile_resolve.resolve_repo_test_cmd",
        _fake_resolve_repo_test_cmd,
    )

    calls = []

    def _fake_land_task(**kwargs):
        calls.append(kwargs)
        return LandResult(
            ok=True, step="close_pr", landed_sha="cafed00d5678",
            pr_url=kwargs["pr_url"], branch=kwargs["branch"], message="landed",
        )

    monkeypatch.setattr("no_human.vcs.approve_merge.land_task", _fake_land_task)

    runner = _make_cli_runner(db, land_env.config, monkeypatch)
    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["profile_test_cmd"] is None


# --------------------------------------------------------------------------- #
# Board: the Approve button (api/app.py::_merge_task_pr)                      #
# --------------------------------------------------------------------------- #

class _FakeGitRepo:
    """Stands in for `vcs.git.GitRepo` inside `_merge_task_pr`'s
    `_resolve_head` closure — this test is about `profile_test_cmd` wiring,
    not git plumbing, so no real repository is touched."""

    def __init__(self, path, *, identity_name, identity_email, never_push_to):
        self.path = path

    def fetch(self):
        pass

    def resolve_commitish(self, branch):
        return f"refs/heads/{branch}"

    def _run(self, *args):
        return "deadbeefcafe1234567890abcdef12345678"


class _Cfg:
    """Minimal `Config`-shaped double — `.data`/`.get`/`__getitem__` — same
    shape as the real `no_human.config.Config` and as the local `_Cfg` in
    `tests/test_approve_merge.py::_make_cli_runner`."""

    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


class _FakeAppState:
    def __init__(self, config):
        self.config = config


class _FakeApp:
    def __init__(self, config):
        self.state = _FakeAppState(config)


class _FakeRequest:
    def __init__(self, config):
        self.app = _FakeApp(config)


def test_board_approve_passes_the_profile_test_command_to_land_task(
        tmp_path, monkeypatch):
    """The board's Approve button (`_merge_task_pr`) must resolve the repo's
    own proven test command the same way `nh approve` does, and forward it
    to `land_task` as `profile_test_cmd`. Every collaborator besides the two
    under test (`resolve_repo_test_cmd` and `land_task`) is faked out so a
    regression here can't hide behind an unrelated precondition failing
    first (a missing branch, a failed review-pass check, git plumbing,
    etc.)."""
    # NOT `import no_human.api.app as app_module`: `no_human/api/__init__.py`
    # does `from .app import app`, which clobbers the `app` attribute on the
    # `no_human.api` package with the FastAPI INSTANCE — an `import ... as`
    # alias then resolves to that instance, not the submodule, so
    # `monkeypatch.setattr(app_module, "_review_pass_evidence", ...)` below
    # would silently target the wrong object.  Going through `sys.modules`
    # (which import machinery itself keeps correct) gets the real submodule.
    import sys as _sys
    app_module = _sys.modules.get("no_human.api.app") or __import__(
        "no_human.api.app", fromlist=["_review_pass_evidence"])

    db_path = tmp_path / "board.db"

    async def _seed():
        async with Store(db_path) as s:
            t = Task.new("Fix the thing", repo_path=str(tmp_path / "repo"))
            t.acceptance_criteria = ["Should work"]
            await s.create_task(t)
            return t

    task = asyncio.run(_seed())

    branch = "no-human/t-boardprofilewiring"
    pr_url = "https://github.com/acme/widgets/pull/42"
    sentinel_cmd = "npm test -- --ci"

    async def _fake_resolve_task_pr(store, task):
        class _Resolved:
            pass
        r = _Resolved()
        r.branch = branch
        return r

    monkeypatch.setattr(
        "no_human.vcs.task_pr.resolve_task_pr", _fake_resolve_task_pr)

    async def _fake_complete_if_approved_and_landed(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "no_human.blockers.shipped.complete_if_approved_and_landed",
        _fake_complete_if_approved_and_landed,
    )

    monkeypatch.setattr("no_human.vcs.git.GitRepo", _FakeGitRepo)

    monkeypatch.setattr(
        app_module, "_review_pass_evidence",
        lambda context, head_sha, repo: (True, "review PASS"),
    )

    async def _fake_resolve_repo_test_cmd(store, config, repo_path):
        return sentinel_cmd

    monkeypatch.setattr(
        "no_human.core.profile_resolve.resolve_repo_test_cmd",
        _fake_resolve_repo_test_cmd,
    )

    calls = []

    def _fake_land_task(**kwargs):
        calls.append(kwargs)
        return LandResult(
            ok=True, step="close_pr", landed_sha="feedface9999",
            pr_url=kwargs["pr_url"], branch=kwargs["branch"], message="landed",
        )

    monkeypatch.setattr("no_human.vcs.approve_merge.land_task", _fake_land_task)

    config = _Cfg({
        "git": {
            "agent_identity_name": "no_human",
            "agent_identity_email": "no-human@acme.com",
            "never_push_to": ["main", "master", "release/*"],
        },
    })
    request = _FakeRequest(config)

    async def _go():
        async with Store(db_path) as store:
            return await app_module._merge_task_pr(request, store, task, pr_url)

    landed_sha, error_detail = asyncio.run(_go())

    assert error_detail is None, error_detail
    assert landed_sha == "feedface9999"
    assert len(calls) == 1, "the Approve button must call land_task exactly once"
    assert calls[0]["profile_test_cmd"] == sentinel_cmd, (
        "the board's Approve button must forward the repo profile's "
        "resolved test command to land_task, not drop it or pass None")
