"""A leftover worktree is disk that never comes back.

`run_task`'s `finally` block already called `remove_worktree` on every
return, terminal or not — the defect was that a failing git removal (a dirty
tree, a main repo that vanished from under it) was swallowed silently
(`vcs/git.py:remove_worktree` runs with `check=False`) and the directory
stayed on disk forever. Measured 2026-08-12: 106 directories, 2.2GB, under
`~/.no_human/worktrees` — most of them orphaned bench sandboxes whose main
repo no longer exists to even attempt a `git worktree remove` against.

These tests pin the fix: `core/worktree.teardown_worktree` adds the missing
post-condition check + an `rmtree` fallback that is loud (an ERROR log line,
never swallowed) when a directory survives both; and
`core/worktree.sweep_stale_worktrees` is the startup-only janitor that
reclaims what a missed/failed teardown left behind for tasks that will never
run again — the class `_reap_dead_worktrees` structurally cannot reach, since
it is scoped to the one task about to run.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

import pytest

from no_human.config import load_config
from no_human.core.task import Task, TaskStatus

# A pid that cannot possibly be alive — pinned in test_worktree_isolation.py
# (`assert pid_alive(4194303) is False`); reused here so both files agree on
# what "dead" means.
_DEAD_PID = "4194303"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def live_checkout(tmp_path):
    """A real repo the orchestrator can open+register a worktree against."""
    work = tmp_path / "live"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    return work


def _orchestrator(store, cfg):
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier

    return Orchestrator(store, cfg.data, object(), SlackNotifier(None))


async def _task(store, repo):
    t = Task.new("add mul()", repo_path=str(repo))
    await store.create_task(t)
    return t


def _finished(task, *, status, cancel_reason=None):
    from no_human.core.orchestrator import TaskOutcome

    if cancel_reason is not None:
        ctx = task.context or {}
        ctx["cancel_reason"] = cancel_reason
        task.context = ctx
    return TaskOutcome(task, status=status, detail="stub")


# --------------------------------------------------------------------------- #
# AC1 — every terminal outcome leaves no directory and no registration        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status,cancel_reason",
    [
        (TaskStatus.DONE, None),
        (TaskStatus.FAILED, None),
        (TaskStatus.FAILED, "operator cancel"),
    ],
    ids=["done", "failed", "cancelled"],
)
async def test_each_terminal_outcome_leaves_no_worktree_dir_and_no_registration(
    live_checkout, tmp_path, store, monkeypatch, status, cancel_reason,
):
    from no_human.vcs import GitRepo

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["isolation"]["worktree_root"] = str(tmp_path / "wt")
    orch = _orchestrator(store, cfg)
    t = await _task(store, live_checkout)

    seen = {}

    async def _drive(task, repo):
        seen["path"] = Path(repo.path)
        return _finished(task, status=status, cancel_reason=cancel_reason)

    monkeypatch.setattr(orch, "_drive_watched", _drive)

    await orch.run_task(t)

    wt = seen["path"]
    assert not wt.exists(), f"{status.value} left the worktree directory behind"
    assert str(wt) not in GitRepo(live_checkout).list_worktrees(), (
        f"{status.value} left the worktree REGISTERED in the main repo")


async def test_a_teardown_git_cannot_do_still_clears_the_directory(
    live_checkout, tmp_path, store, monkeypatch, caplog,
):
    """A `git worktree remove` that silently does nothing (dirty tree, a stale
    lock — anything `check=False` swallows) must not leave the directory on
    disk: the rmtree fallback is what actually clears it."""
    from no_human.vcs import GitRepo

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["isolation"]["worktree_root"] = str(tmp_path / "wt")
    orch = _orchestrator(store, cfg)
    t = await _task(store, live_checkout)

    monkeypatch.setattr(GitRepo, "remove_worktree", lambda self, *a, **k: None)

    seen = {}

    async def _drive(task, repo):
        seen["path"] = Path(repo.path)
        return _finished(task, status=TaskStatus.DONE)

    monkeypatch.setattr(orch, "_drive_watched", _drive)

    with caplog.at_level(logging.INFO, logger="no_human.worktree"):
        await orch.run_task(t)

    wt = seen["path"]
    assert not wt.exists(), "rmtree fallback did not clear a directory git left behind"
    assert not any(r.levelno >= logging.ERROR for r in caplog.records), (
        "teardown actually succeeded via the rmtree fallback — an ERROR here "
        "would be a false alarm")


def test_a_teardown_failure_is_logged_loudly_not_swallowed(tmp_path, monkeypatch, caplog):
    """When even the rmtree fallback cannot clear the directory, that failure
    must be an ERROR log line naming the path — never silently dropped."""
    from no_human.core import worktree as worktree_mod

    wt = tmp_path / "stuck-worktree"
    wt.mkdir()
    (wt / "file.txt").write_text("x")

    class _NoOpRepo:
        def remove_worktree(self, *a, **k):
            pass  # git "succeeds" but the directory is still there

    def _always_raises(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(worktree_mod.shutil, "rmtree", _always_raises)

    with caplog.at_level(logging.ERROR, logger="no_human.worktree"):
        result = worktree_mod.teardown_worktree(
            _NoOpRepo(), wt, task_id="deadbeef1234")

    assert result is False
    assert any(
        r.levelno == logging.ERROR and str(wt) in r.getMessage()
        for r in caplog.records
    ), "no ERROR record named the path that survived teardown"


# --------------------------------------------------------------------------- #
# AC2 — the startup janitor                                                    #
# --------------------------------------------------------------------------- #


async def test_the_startup_janitor_removes_terminal_leftovers_and_keeps_live_ones(
    tmp_path, store,
):
    from no_human.core.worktree import sweep_stale_worktrees

    cfg = load_config(tmp_path / "config.yaml")
    root = tmp_path / "wt"
    cfg.data["isolation"]["worktree_root"] = str(root)
    root.mkdir(parents=True)

    async def _mk(status, *, cancel_reason=None):
        task = Task.new("x", repo_path="/tmp/does-not-exist-nh-teardown-test")
        await store.create_task(task)
        if cancel_reason is not None:
            ctx = task.context or {}
            ctx["cancel_reason"] = cancel_reason
            task.context = ctx
            await store.update_task(task)
        event = {"source": "test", "kind": "test_seed"} if status is TaskStatus.DONE else None
        await store.set_status(task, status, validate=False, event=event)
        return task

    t_done = await _mk(TaskStatus.DONE)
    t_cancelled = await _mk(TaskStatus.FAILED, cancel_reason="operator cancel")
    t_implementing = await _mk(TaskStatus.IMPLEMENTING)
    t_escalated = await _mk(TaskStatus.ESCALATED)
    t_live_owner = await _mk(TaskStatus.DONE)

    # (a) done, dead owner pid -> removed.
    a = root / f"{t_done.id}.{_DEAD_PID}.aaaaaaaa"
    a.mkdir()
    # (b) failed+cancelled, dead owner pid -> removed.
    b = root / f"{t_cancelled.id}.{_DEAD_PID}.bbbbbbbb"
    b.mkdir()
    # (c) implementing (non-terminal) -> kept.
    c = root / f"{t_implementing.id}.{_DEAD_PID}.cccccccc"
    c.mkdir()
    # (d) escalated (parked, resumable, non-terminal) -> kept.
    d = root / f"{t_escalated.id}.{_DEAD_PID}.dddddddd"
    d.mkdir()
    # (e) done, but a LIVE owner pid -> kept (pid liveness beats status).
    e = root / f"{t_live_owner.id}.1.eeeeeeee"
    e.mkdir()
    # (f) unknown task id, gitdir target still exists -> kept + warned.
    unknown_present = "0" * 32
    f = root / f"{unknown_present}.{_DEAD_PID}.ffffffff"
    f.mkdir()
    present_target = tmp_path / "still-there"
    present_target.mkdir()
    (f / ".git").write_text(f"gitdir: {present_target}\n")
    # (g) unknown task id, DANGLING gitdir -> removed (the 88-dir bench class).
    unknown_gone = "1" * 32
    g = root / f"{unknown_gone}.{_DEAD_PID}.gggggggg"
    g.mkdir()
    (g / ".git").write_text(f"gitdir: {tmp_path / 'gone-bench-sandbox'}\n")

    removed, skipped = await sweep_stale_worktrees(store, cfg.data, live=set())

    assert (removed, skipped) == (3, 4)
    assert not a.exists(), "a done task's leftover with a dead owner must be removed"
    assert not b.exists(), "a failed+cancelled task's leftover must be removed"
    assert c.exists(), "an implementing (non-terminal) task's worktree must survive"
    assert d.exists(), "an escalated (parked, resumable) task's worktree must survive"
    assert e.exists(), "a live owner pid must beat a done status"
    assert f.exists(), "an unknown task id whose gitdir target still exists must survive"
    assert not g.exists(), "an unknown task id with a dangling gitdir is reclaimable"


async def test_the_janitor_runs_at_boot_before_orphan_recovery(store):
    from no_human.core.scheduler import Scheduler

    sched = Scheduler(store, lambda *a, **k: object(), config={})
    order = []

    async def _sweep():
        order.append("sweep")

    async def _recover(*, startup=True):
        order.append("recover")

    sched._sweep_stale_worktrees = _sweep
    sched._recover_orphans = _recover

    stop = asyncio.Event()
    stop.set()
    await sched.run_forever(stop=stop)

    assert order == ["sweep", "recover"], (
        "the janitor must run exactly once, before orphan recovery — orphan "
        "recovery requeues tasks and can start creating new worktrees")


async def test_a_sweep_that_throws_never_blocks_boot(store, monkeypatch, caplog):
    from no_human.core import scheduler as scheduler_mod

    sched = scheduler_mod.Scheduler(store, lambda *a, **k: object(), config={})

    async def _boom(*a, **k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(scheduler_mod, "sweep_stale_worktrees", _boom)

    stop = asyncio.Event()
    stop.set()
    with caplog.at_level(logging.ERROR, logger="no_human.scheduler"):
        await sched.run_forever(stop=stop)

    assert any(
        "sweeping stale worktrees failed" in r.getMessage() for r in caplog.records
    ), "a sweep failure must be logged, not silently dropped"
