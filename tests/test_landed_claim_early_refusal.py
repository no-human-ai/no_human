"""Integration: the landed-claim guard, wired into a real `Orchestrator` over
a real temp git repo, refuses a refutable "already satisfied" claim the
moment it is asserted — using the exact same ancestry question
(`classify_already_satisfied_landing`, `git merge-base --is-ancestor`)
delivery asks, just earlier."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.test")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover — never invoked here
        raise AssertionError("backend should not run in this test")


def _orch(store, tmp_path):
    return Orchestrator(
        store, _config(tmp_path).data, _Backend(), SlackNotifier(None),
    )


async def test_guard_is_wired_into_the_attempt_and_fires_on_a_refutable_claim(
    bare_repo, tmp_path, store,
):
    # An ORDINARY commit (no [WIP-*] subject) left by a previous, review-
    # failed round — 33 of the 42 measured incidents look like this, not a
    # checkpoint.
    attempt_branch = "no-human/task-attempt-1"
    _git(bare_repo, "checkout", "-b", attempt_branch)
    (bare_repo / "fix.py").write_text("def fix():\n    return True\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "attempt at the fix, review FAILED")
    claimed_sha = GitRepo(bare_repo).head_sha()

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    await store.create_task(task)

    guard = orch._build_landed_claim_guard(
        task, GitRepo(bare_repo), base="main", branch=attempt_branch,
    )
    assert guard is not None

    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{claimed_sha}, no changes needed."
    )
    result = await guard.hook({}, None, None)

    assert result, "a commit not reachable from main must be refused"
    message = result["hookSpecificOutput"]["additionalContext"]
    assert claimed_sha in message
    assert "main" in message
    assert "is not an ancestor of" in message
    # Non-terminal: the attempt must be told to keep going.
    assert "continue_" not in result


async def test_a_commit_that_is_on_the_base_branch_is_not_blocked(
    bare_repo, tmp_path, store,
):
    main_tip = GitRepo(bare_repo).head_sha()
    attempt_branch = "no-human/task-attempt-2"
    _git(bare_repo, "checkout", "-b", attempt_branch)

    orch = _orch(store, tmp_path)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    await store.create_task(task)

    guard = orch._build_landed_claim_guard(
        task, GitRepo(bare_repo), base="main", branch=attempt_branch,
    )
    assert guard is not None

    guard.note_text(
        f"This is already implemented — the work already exists at "
        f"{main_tip}, no changes needed."
    )
    result = await guard.hook({}, None, None)

    assert result == {}, "a commit already reachable from main must not be blocked"


def test_composed_post_tool_hooks_place_the_claim_guard_after_receipts():
    r, lint, scope, claim = object(), object(), object(), object()
    assert Orchestrator._ordered_post_tool_hooks(r, lint, scope, claim_hook=claim) == [
        r, claim, lint, scope,
    ]
    # Pre-existing 3-positional-arg call sites are unaffected: no claim hook,
    # same order as before this change.
    assert Orchestrator._ordered_post_tool_hooks(r, lint, scope) == [r, lint, scope]
    assert Orchestrator._ordered_post_tool_hooks(r, None, scope, claim_hook=claim) == [
        r, claim, scope,
    ]
