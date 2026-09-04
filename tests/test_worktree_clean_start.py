"""One orchestrator RUN shares ONE git worktree across all its attempts.

`_run_attempt` went straight into branch selection — `repo.checkout(branch)`
(the `pr_branch` revision path) or `repo.create_branch(branch, base=...)`
(`git checkout -B`, the fresh path) — without ever resetting the tree first.
An earlier attempt's uncommitted leftovers (a stuck coder session, a crash
before the commit) then crash the NEXT attempt's checkout with
``GitError: Your local changes would be overwritten by checkout``.

Evidence: task 7afb9346 attempt-2 crashed on a dirty
`EXPORT_CLASSIFICATION.txt` left over from a DIFFERENT task (cross-task
contamination — that file is normally touched only by task 5b2246c1);
attempt-4 crashed on a dirty `web/src/Onboarding.jsx`.

The fix: `core/worktree.reset_agent_workspace` — a fail-closed ownership
guard (`is_agent_worktree`) wrapping `GitRepo.reset_workspace` (`reset
--hard` + `clean -fd`, never `-x`) — called in `_run_attempt` immediately
before the branch decision, so both routes into an attempt start clean.

Fixtures modelled on `tests/test_worktree_teardown.py` (`_git`, `store`) and
the direct `_run_attempt(...)` call pattern in `tests/test_infra_not_work.py`
/ the `_Stop`-via-monkeypatched-`_build_implement_prompt` idiom in
`tests/test_resume_checkpoint_lost.py`.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.core.worktree import is_agent_worktree, reset_agent_workspace
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitError, GitRepo


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def main_repo(tmp_path):
    """A real repo with a committed `EXPORT_CLASSIFICATION.txt` (the exact
    evidence file) and a `.gitignore` for the ignored-artifacts test."""
    work = tmp_path / "main"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "EXPORT_CLASSIFICATION.txt").write_text("A\n")
    (work / ".gitignore").write_text(".venv/\nnode_modules/\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    return GitRepo(work)


def _cfg(tmp_path, root):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("isolation", {})["worktree_root"] = str(root)
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    return cfg


def _shaped(root, task_id, token="aaaaaaaa"):
    """The `<task_id>.<owner_pid>.<token>` shape `worktree_owner` parses,
    rooted directly under the configured worktree root — the per-run
    directory naming this fix must never touch (OUT OF SCOPE)."""
    return root / f"{task_id}.{os.getpid()}.{token}"


# --------------------------------------------------------------------------- #
# 1/2 — the two evidence scenarios                                            #
# --------------------------------------------------------------------------- #


def test_dirty_tracked_file_does_not_crash_checkout_b(tmp_path, main_repo):
    """Evidence scenario 1: `EXPORT_CLASSIFICATION.txt`, modified by a
    previous attempt and left uncommitted, must not crash the next
    attempt's `checkout -B` — RED (raises GitError) before the reset,
    GREEN after it."""
    root = tmp_path / "wt"
    cfg = _cfg(tmp_path, root)
    wt_path = _shaped(root, "a" * 32)
    wt = main_repo.add_worktree(wt_path, base="main", detach=True)

    # Main advances past the worktree's detached HEAD with a conflicting
    # change to the SAME tracked path.
    (main_repo.path / "EXPORT_CLASSIFICATION.txt").write_text("B\n")
    main_repo._run("commit", "-am", "advance main")

    # The previous attempt's uncommitted leftover.
    (wt.path / "EXPORT_CLASSIFICATION.txt").write_text("dirty-leftover\n")

    # RED: pin the exact evidence failure before the fix is applied.
    with pytest.raises(GitError, match="would be overwritten"):
        wt.create_branch("no-human/red", base="main")

    discarded = reset_agent_workspace(wt, cfg.data, task_id="a" * 32)
    assert discarded == ["EXPORT_CLASSIFICATION.txt"]

    # GREEN: the same operation that just crashed now succeeds.
    wt.create_branch("no-human/green", base="main")
    assert (wt.path / "EXPORT_CLASSIFICATION.txt").read_text() == "B\n"


def test_dirty_untracked_file_is_removed_before_branching(tmp_path, main_repo):
    """Evidence scenario 2: an untracked leftover (`web/src/Onboarding.jsx`)
    that collides with a file main added later must be removed before
    branching, not merely reset."""
    root = tmp_path / "wt"
    cfg = _cfg(tmp_path, root)
    wt_path = _shaped(root, "b" * 32)
    wt = main_repo.add_worktree(wt_path, base="main", detach=True)

    onboarding = main_repo.path / "web" / "src" / "Onboarding.jsx"
    onboarding.parent.mkdir(parents=True)
    onboarding.write_text("export default function Onboarding() {}\n")
    main_repo._run("add", "-A")
    main_repo._run("commit", "-m", "add onboarding")

    wt_onboarding = wt.path / "web" / "src" / "Onboarding.jsx"
    wt_onboarding.parent.mkdir(parents=True)
    wt_onboarding.write_text("// previous attempt's untracked leftover\n")

    with pytest.raises(GitError, match="untracked working tree files"):
        wt.create_branch("no-human/red", base="main")

    discarded = reset_agent_workspace(wt, cfg.data, task_id="b" * 32)
    # `web/` is wholly untracked, so `git status --porcelain` reports the
    # directory itself as one entry, not the file inside it — this is
    # correct/standard porcelain behaviour, not a parsing defect.
    assert discarded == ["web/"]
    assert not wt_onboarding.exists()  # removed by clean; re-created by checkout below

    wt.create_branch("no-human/green", base="main")
    assert wt_onboarding.read_text() == "export default function Onboarding() {}\n"


# --------------------------------------------------------------------------- #
# 3 — a real multi-attempt sequence through `_run_attempt`                    #
# --------------------------------------------------------------------------- #


class _Stop(Exception):
    """Ends the attempt immediately after the branch decision — everything
    that follows (the coder session, the commit) never runs, so whatever the
    attempt wrote to disk stays exactly as a crash would have left it."""


class _FakeBackend:
    """A plain `object()` can't take `self.backend.never_push_to = ...`
    (`_protect_base_branch` sets it every attempt) — this stands in with no
    behaviour of its own; `.run` is never reached because
    `_build_implement_prompt` raises `_Stop` first."""

    never_push_to: list[str] | None = None


async def test_leftovers_never_cross_the_attempt_boundary(tmp_path, main_repo, store, monkeypatch):
    root = tmp_path / "wt"
    cfg = _cfg(tmp_path, root)

    task = Task.new("fix a thing", repo_path=str(main_repo.path))
    wt_path = _shaped(root, task.id, "c0ffee01")
    wt = main_repo.add_worktree(wt_path, base="main", detach=True)

    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)

    orch = Orchestrator(store, cfg.data, _FakeBackend(), SlackNotifier(None), event_sink=[].append)
    monkeypatch.setattr(
        Orchestrator, "_build_implement_prompt",
        lambda self, *a, **k: (_ for _ in ()).throw(_Stop()))

    # Attempt 1: reaches the branch decision on a clean tree, then stops.
    with pytest.raises(_Stop):
        await orch._run_attempt(task, wt, 1, "main")

    # Simulate what a crash before the commit leaves behind: a modified
    # tracked file and a new untracked one.
    (wt.path / "calc.py").write_text("def add(a, b):\n    return a + b  # WIP\n")
    (wt.path / "leftover.py").write_text("# orphaned scratch file\n")
    assert wt.has_changes()

    # `_run_attempt` transitioned the task to IMPLEMENTING; walk it back to
    # PLANNING (bypassing `assert_transition`, exactly as the harness does
    # between real attempts) so attempt 2 is legal to start.
    await store.set_status(task, TaskStatus.PLANNING, validate=False)

    # Attempt 2: if the reset were missing, THIS is where the reused
    # worktree's leftover would crash `checkout -B` with a GitError instead
    # of reaching `_Stop`.
    with pytest.raises(_Stop):
        await orch._run_attempt(task, wt, 2, "main")

    assert not wt.has_changes(), (
        "attempt 2 must start from a clean tree — a leftover survived the "
        "reset and/or the (aborted) attempt itself left new dirt")


# --------------------------------------------------------------------------- #
# 4 — cross-task leakage                                                      #
# --------------------------------------------------------------------------- #


def test_one_tasks_leftovers_cannot_appear_in_another_tasks_workspace(tmp_path, main_repo):
    root = tmp_path / "wt"
    cfg = _cfg(tmp_path, root)

    wt_a = main_repo.add_worktree(_shaped(root, "a" * 32, "taska001"), base="main", detach=True)
    wt_b = main_repo.add_worktree(_shaped(root, "b" * 32, "taskb001"), base="main", detach=True)
    assert wt_a.path != wt_b.path

    (wt_a.path / "EXPORT_CLASSIFICATION.txt").write_text("task-a-leftover\n")
    (wt_b.path / "EXPORT_CLASSIFICATION.txt").write_text("task-b-leftover\n")
    assert wt_a.has_changes() and wt_b.has_changes()

    discarded = reset_agent_workspace(wt_a, cfg.data, task_id="a" * 32)
    assert discarded == ["EXPORT_CLASSIFICATION.txt"]

    assert not wt_a.has_changes(), "task A's own leftover must be gone"
    assert wt_b.has_changes(), "resetting task A must never touch task B's workspace"
    assert (wt_b.path / "EXPORT_CLASSIFICATION.txt").read_text() == "task-b-leftover\n"


# --------------------------------------------------------------------------- #
# 5 — scope guard: never a human checkout, never outside the root             #
# --------------------------------------------------------------------------- #


def test_primary_checkout_is_never_reset(tmp_path, main_repo):
    """`main_repo` itself is a primary checkout (`.git` is a directory, not
    a linked worktree's file) — the guard must decline even though nothing
    else distinguishes it from an agent worktree at this path shape."""
    root = tmp_path / "wt"
    cfg = _cfg(tmp_path, root)

    (main_repo.path / "EXPORT_CLASSIFICATION.txt").write_text("human-in-progress\n")
    before = (main_repo.path / "EXPORT_CLASSIFICATION.txt").read_bytes()

    assert is_agent_worktree(main_repo.path, cfg.data) is False
    result = reset_agent_workspace(main_repo, cfg.data, task_id="deadbeef" * 4)
    assert result is None
    assert (main_repo.path / "EXPORT_CLASSIFICATION.txt").read_bytes() == before


def test_path_outside_worktree_root_is_declined(tmp_path, main_repo):
    root = tmp_path / "wt"
    cfg = _cfg(tmp_path, root)

    elsewhere = tmp_path / "elsewhere"
    wt = main_repo.add_worktree(
        _shaped(elsewhere, "e" * 32, "outroot1"), base="main", detach=True)

    (wt.path / "EXPORT_CLASSIFICATION.txt").write_text("outside-root-dirty\n")
    before = (wt.path / "EXPORT_CLASSIFICATION.txt").read_bytes()

    assert is_agent_worktree(wt.path, cfg.data) is False
    result = reset_agent_workspace(wt, cfg.data, task_id="e" * 32)
    assert result is None
    assert (wt.path / "EXPORT_CLASSIFICATION.txt").read_bytes() == before


# --------------------------------------------------------------------------- #
# 6 — ignored build artifacts survive                                         #
# --------------------------------------------------------------------------- #


def test_reset_workspace_keeps_ignored_build_artifacts(tmp_path, main_repo):
    root = tmp_path / "wt"
    cfg = _cfg(tmp_path, root)
    wt = main_repo.add_worktree(_shaped(root, "f" * 32, "artifacts"), base="main", detach=True)

    venv_marker = wt.path / ".venv" / "marker"
    venv_marker.parent.mkdir(parents=True)
    venv_marker.write_text("built\n")

    node_modules = wt.path / "node_modules" / "x"
    node_modules.parent.mkdir(parents=True)
    node_modules.write_text("built\n")

    plan = wt.path / ".no_human" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("agent scratch\n")

    scratch = wt.path / "scratch.txt"
    scratch.write_text("genuinely uncommitted, not ignored\n")

    (wt.path / "calc.py").write_text("def add(a, b):\n    return a + b  # dirty\n")

    discarded = reset_agent_workspace(wt, cfg.data, task_id="f" * 32)

    assert venv_marker.exists(), ".venv is gitignored — clean -fd must never take -x"
    assert node_modules.exists(), "node_modules is gitignored — must survive"
    assert plan.exists(), ".no_human is excluded via `clean -fd -e .no_human`"
    assert not scratch.exists(), "a genuinely uncommitted, non-ignored file must be discarded"
    assert (wt.path / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert "scratch.txt" in discarded
    assert "calc.py" in discarded


# --------------------------------------------------------------------------- #
# 7 — idempotence                                                             #
# --------------------------------------------------------------------------- #


def test_reset_workspace_on_a_clean_tree_is_a_noop(tmp_path, main_repo):
    root = tmp_path / "wt"
    cfg = _cfg(tmp_path, root)
    wt = main_repo.add_worktree(_shaped(root, "9" * 32, "cleanwt1"), base="main", detach=True)

    sha_before = wt.head_sha()
    assert not wt.has_changes()

    discarded = reset_agent_workspace(wt, cfg.data, task_id="9" * 32)

    assert discarded == []
    assert wt.head_sha() == sha_before
    assert not wt.has_changes()
