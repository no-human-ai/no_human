"""Worktree isolation is a property of every run, not of parallelism.

The two ideas used to be one flag. ``concurrency.enabled`` switched BOTH
"run several tasks at once" AND "run each task in its own git worktree", and
it defaults to false - so the DEFAULT single-task run made the operator's live
checkout the agent's working tree. Anyone who filed a task against the repo
they were mid-edit in handed their uncommitted work to the agent.

These tests pin the split:

  ``isolation.enabled``    - run this task in its own throwaway git worktree.
                             Default TRUE, whether or not anything is parallel.
  ``concurrency.enabled``  - run several tasks at once. Default FALSE, unchanged.

Parallelism still REQUIRES isolation: two workers in one checkout stomp each
other's index and branch, so the pool refuses rather than downgrading.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from no_human.config import (
    load_config,
    parallelism_enabled,
    worktree_isolation_enabled,
    worktree_root,
)
from no_human.core.scheduler import resolve_max_workers, resolve_serve_pool


def _write_config(tmp_path, data: dict):
    """A config.yaml holding only what an operator actually wrote."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return load_config(path)


# --------------------------------------------------------------------------- #
# The two concepts, at the config layer                                        #
# --------------------------------------------------------------------------- #


def test_default_config_isolates_every_task(tmp_path):
    """The defect: a default install ran the agent in the live checkout."""
    cfg = load_config(tmp_path / "config.yaml")
    assert worktree_isolation_enabled(cfg.data) is True
    assert cfg.data["isolation"]["enabled"] is True


def test_default_config_still_runs_one_task_at_a_time(tmp_path):
    """Isolation moving to on-by-default must not switch parallelism on."""
    cfg = load_config(tmp_path / "config.yaml")
    assert parallelism_enabled(cfg.data) is False
    assert resolve_max_workers(cfg.data)[0] == 1


def test_isolation_can_be_opted_out_explicitly(tmp_path):
    """An operator who WANTS the agent in the checkout they are sitting in
    can still say so - but only by saying so."""
    cfg = _write_config(tmp_path, {"isolation": {"enabled": False}})
    assert worktree_isolation_enabled(cfg.data) is False


def test_a_config_written_before_the_split_gets_the_new_default(tmp_path):
    """Migration: every existing config.yaml predates the `isolation` block
    (including the full default dump `nh init` writes, which pins
    `concurrency.enabled: false`). Absent the key, isolation is ON."""
    cfg = _write_config(
        tmp_path,
        {"concurrency": {"enabled": False, "max_workers": 2,
                         "worktree_root": None}},
    )
    assert worktree_isolation_enabled(cfg.data) is True
    assert parallelism_enabled(cfg.data) is False


def test_an_existing_concurrency_enabled_config_is_unaffected(tmp_path):
    """Migration, the other direction: an operator already running the pool
    keeps the same width and the same isolation."""
    cfg = _write_config(
        tmp_path, {"concurrency": {"enabled": True, "max_workers": 3}})
    assert parallelism_enabled(cfg.data) is True
    assert worktree_isolation_enabled(cfg.data) is True
    # `cpu_count` pinned: the width also has a machine ceiling (cpu_count//3)
    # now, and this test is about the migration, not about the test machine.
    assert resolve_max_workers(cfg.data, cpu_count=12) == (3, None)


def test_the_legacy_worktree_root_key_is_still_honoured(tmp_path):
    """`concurrency.worktree_root` was the only place to relocate worktrees,
    so a config that set it must not silently move to the default path."""
    cfg = _write_config(
        tmp_path, {"concurrency": {"worktree_root": str(tmp_path / "old")}})
    assert worktree_root(cfg.data) == tmp_path / "old"


def test_the_isolation_worktree_root_wins_over_the_legacy_key(tmp_path):
    cfg = _write_config(tmp_path, {
        "concurrency": {"worktree_root": str(tmp_path / "old")},
        "isolation": {"worktree_root": str(tmp_path / "new")},
    })
    assert worktree_root(cfg.data) == tmp_path / "new"


def test_worktree_root_defaults_under_the_no_human_home(tmp_path):
    from no_human.config import NO_HUMAN_HOME

    cfg = load_config(tmp_path / "config.yaml")
    assert worktree_root(cfg.data) == NO_HUMAN_HOME / "worktrees"


# --------------------------------------------------------------------------- #
# Parallelism still requires isolation - the pool refuses, never downgrades    #
# --------------------------------------------------------------------------- #


def test_the_pool_refuses_to_widen_without_isolation():
    """Isolation is a DEFAULT for one task and a REQUIREMENT for many."""
    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 2},
         "isolation": {"enabled": False}})
    assert workers == 1, "two workers in one checkout is the thing we refuse"
    assert warning and "isolation.enabled is false" in warning


def test_serve_refuses_a_configured_pool_without_isolation():
    workers, enabled, error = resolve_serve_pool(
        {"concurrency": {"enabled": True, "max_workers": 2},
         "isolation": {"enabled": False}}, cli_workers=None)
    assert enabled is False
    assert error and "isolation.enabled is false" in error


def test_the_serve_flag_cannot_buy_parallelism_without_isolation():
    """`--max-workers N` enables the pool for one invocation; it must not
    also override an explicit isolation opt-out."""
    workers, enabled, error = resolve_serve_pool(
        {"isolation": {"enabled": False}}, cli_workers=3)
    assert enabled is False
    assert error and "isolation.enabled is false" in error


def test_a_single_worker_without_isolation_is_the_operators_choice():
    """The opt-out is honoured where it is safe: one task, one checkout."""
    assert resolve_serve_pool(
        {"concurrency": {"enabled": True, "max_workers": 1},
         "isolation": {"enabled": False}}, cli_workers=None,
    ) == (1, True, None)


# --------------------------------------------------------------------------- #
# The orchestrator actually runs somewhere else                                #
# --------------------------------------------------------------------------- #


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def live_checkout(tmp_path):
    """A repo with UNCOMMITTED work in it - the prospect's situation."""
    work = tmp_path / "live"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b  # WIP\n")
    return work


def _orchestrator(store, cfg):
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier

    return Orchestrator(store, cfg.data, object(), SlackNotifier(None))


async def _task(store, repo):
    from no_human.core.task import Task

    t = Task.new("add mul()", repo_path=str(repo))
    await store.create_task(t)
    return t


async def test_a_default_run_works_in_a_worktree_not_the_live_checkout(
    live_checkout, tmp_path, store, monkeypatch,
):
    """End to end for the defect: with NO config at all, the working tree the
    attempt loop is handed is a worktree, and the operator's uncommitted edit
    is still sitting untouched in the checkout."""
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["isolation"]["worktree_root"] = str(tmp_path / "wt")
    orch = _orchestrator(store, cfg)
    t = await _task(store, live_checkout)

    seen = {}

    async def _capture(task, repo):
        seen["path"] = repo.path
        seen["wip"] = (live_checkout / "calc.py").read_text()
        from no_human.core.orchestrator import TaskOutcome
        from no_human.core.task import TaskStatus
        return TaskOutcome(task, status=TaskStatus.DONE, detail="stub")

    monkeypatch.setattr(orch, "_drive_watched", _capture)

    await orch.run_task(t)

    assert seen["path"] != live_checkout
    # One directory per RUN, under the configured root, named for the task.
    assert seen["path"].parent == tmp_path / "wt"
    assert seen["path"].name.startswith(t.id + ".")
    assert "# WIP" in seen["wip"], "the operator's uncommitted work survived"


async def test_an_explicit_opt_out_still_runs_in_the_live_checkout(
    live_checkout, tmp_path, store, monkeypatch,
):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["isolation"]["enabled"] = False
    orch = _orchestrator(store, cfg)
    t = await _task(store, live_checkout)

    seen = {}

    async def _capture(task, repo):
        seen["path"] = repo.path
        from no_human.core.orchestrator import TaskOutcome
        from no_human.core.task import TaskStatus
        return TaskOutcome(task, status=TaskStatus.DONE, detail="stub")

    monkeypatch.setattr(orch, "_drive_watched", _capture)

    await orch.run_task(t)
    assert seen["path"] == live_checkout


async def test_a_worktree_failure_never_falls_back_to_the_live_checkout(
    live_checkout, tmp_path, store, monkeypatch,
):
    """Silent fallback IS the defect, so an unusable worktree root has to be
    a loud failure - the attempt loop must never be handed the checkout."""
    from no_human.core.task import TaskStatus

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("this is a file, so no directory can be made under it")
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["isolation"]["worktree_root"] = str(blocker / "wt")
    orch = _orchestrator(store, cfg)
    t = await _task(store, live_checkout)

    called = []
    monkeypatch.setattr(
        orch, "_drive_watched",
        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError))

    outcome = await orch.run_task(t)

    assert called == [], "fell back to the live checkout"
    assert outcome.status is TaskStatus.FAILED
    assert "worktree" in outcome.detail.lower()
    # The message has to name the path the operator must fix.
    assert str(blocker / "wt") in outcome.detail


async def test_a_repo_path_that_is_not_a_git_repo_fails_before_any_worktree(
    tmp_path, store, monkeypatch,
):
    """The other way a worktree cannot exist: there is no repo to link one to.
    Also a hard failure, and it is reported as what it is."""
    from no_human.core.task import TaskStatus

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    cfg = load_config(tmp_path / "config.yaml")
    orch = _orchestrator(store, cfg)
    t = await _task(store, plain)

    called = []
    monkeypatch.setattr(
        orch, "_drive_watched",
        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError))

    outcome = await orch.run_task(t)

    assert called == []
    assert outcome.status is TaskStatus.FAILED
    assert "not a git repo" in outcome.detail


# --------------------------------------------------------------------------- #
# Two attempts of ONE task overlap — and must not share a directory            #
# --------------------------------------------------------------------------- #
#
# The defect: `_worktree_path` keyed the checkout on the task id alone, so every
# attempt of a task landed in the SAME directory. Overlap is not hypothetical —
# the scheduler's orphan recovery requeues a task whose previous process is
# still winding down (a server restart is enough). Two processes then wrote one
# checkout, and whichever finished first `git worktree remove`d the directory
# the other was still working in:
#
#     Claude Code returned an error result: Path
#     "/Users/…/.no_human/worktrees/<task_id>" does not exist
#
# Seen on four live tasks in one day; they burned 14-20M tokens across 5-8
# attempts and delivered nothing. Serialising the attempts is NOT the fix —
# overlap is legitimate. The shared path is the defect.


def _finished(task):
    from no_human.core.orchestrator import TaskOutcome
    from no_human.core.task import TaskStatus

    return TaskOutcome(task, status=TaskStatus.DONE, detail="stub")


async def test_overlapping_attempts_of_one_task_get_separate_worktrees(
    live_checkout, tmp_path, store,
):
    """Attempt A starts, attempt B starts while A is live, A finishes (and tears
    its worktree down) — B's checkout must still be on disk and usable.

    Both halves of the bug are pinned here: B's *acquire* must not delete A's
    live checkout, and A's *teardown* must not delete B's."""
    import asyncio

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["isolation"]["worktree_root"] = str(tmp_path / "wt")
    orch_a = _orchestrator(store, cfg)
    orch_b = _orchestrator(store, cfg)
    t = await _task(store, live_checkout)

    b_acquired = asyncio.Event()
    a_torn_down = asyncio.Event()
    seen: dict = {}

    async def drive_a(task, repo):
        seen["a"] = Path(repo.path)
        # Hold A open until B has its own checkout, so the two really overlap.
        await asyncio.wait_for(b_acquired.wait(), 30)
        seen["a_alive_after_b_acquired"] = (Path(repo.path) / "calc.py").exists()
        return _finished(task)

    async def drive_b(task, repo):
        seen["b"] = Path(repo.path)
        b_acquired.set()
        await asyncio.wait_for(a_torn_down.wait(), 30)
        seen["b_alive_after_a_torn_down"] = (Path(repo.path) / "calc.py").exists()
        return _finished(task)

    orch_a._drive_watched = drive_a
    orch_b._drive_watched = drive_b

    async def _run_a():
        try:
            return await orch_a.run_task(t)
        finally:
            a_torn_down.set()

    outcome_a, outcome_b = await asyncio.gather(_run_a(), orch_b.run_task(t))

    assert seen["a"] != seen["b"], (
        "both attempts were handed the SAME directory — this is the defect")
    assert seen["a_alive_after_b_acquired"] is True, (
        "acquiring B's worktree destroyed the checkout A was working in")
    assert seen["b_alive_after_a_torn_down"] is True, (
        "A's teardown removed the checkout B was still working in")
    assert not seen["a"].exists(), "A's own worktree was not cleaned up"
    assert outcome_a.status.value == outcome_b.status.value


async def test_a_superseded_attempts_worktree_is_reclaimed(
    live_checkout, tmp_path, store,
):
    """Disk must not fill. A directory left by a DEAD owner is reclaimed at the
    next acquire; one owned by a live process is left strictly alone."""
    import os

    cfg = load_config(tmp_path / "config.yaml")
    root = tmp_path / "wt"
    cfg.data["isolation"]["worktree_root"] = str(root)
    orch = _orchestrator(store, cfg)
    t = await _task(store, live_checkout)

    root.mkdir(parents=True)
    # A crashed run of THIS task: owner pid long gone.
    dead = root / f"{t.id}.4194303.aaaaaaaa"
    (dead / "sub").mkdir(parents=True)
    # The pre-fix directory shape, from an install that ran the old code.
    legacy = root / t.id
    legacy.mkdir()
    # A live foreign owner (pid 1 is always alive and is never us).
    live_owner = root / f"{t.id}.1.bbbbbbbb"
    live_owner.mkdir()
    # Another task's leftovers are none of our business.
    other = root / "0123456789abcdef0123456789abcdef.4194303.cccccccc"
    other.mkdir()
    # This task's id, but no readable owner and not the one legacy shape:
    # unprovable, so untouchable.
    unreadable = root / f"{t.id}.notapid.dddddddd"
    unreadable.mkdir()

    seen: dict = {}

    async def drive(task, repo):
        seen["path"] = Path(repo.path)
        seen["dead_gone"] = not dead.exists()
        seen["legacy_gone"] = not legacy.exists()
        seen["live_owner_kept"] = live_owner.exists()
        seen["other_kept"] = other.exists()
        seen["unreadable_kept"] = unreadable.exists()
        return _finished(task)

    orch._drive_watched = drive
    await orch.run_task(t)

    assert seen["dead_gone"] is True, "a crashed attempt's checkout leaks disk"
    assert seen["legacy_gone"] is True, "the pre-fix directory shape leaks disk"
    assert seen["live_owner_kept"] is True, (
        "reclaimed a directory whose owner process is still alive")
    assert seen["other_kept"] is True, "reclaimed another task's worktree"
    assert seen["unreadable_kept"] is True, (
        "reclaimed a directory whose owner could not be read — the reaper may "
        "only delete what it can PROVE is dead")
    assert seen["path"].name.startswith(t.id + "."), (
        "the worktree name must carry the task id so the doctor can find it")
    assert os.getpid() == int(seen["path"].name.split(".")[1]), (
        "the owner pid must be in the name — it is what makes reclaim safe")


# --------------------------------------------------------------------------- #
# The naming grammar every OTHER reader of the path depends on                 #
# --------------------------------------------------------------------------- #


def test_a_worktree_name_is_attributable_to_its_task_and_owner():
    from no_human.config import worktree_owner

    assert worktree_owner("deadbeef1234.4321.a1b2c3d4") == ("deadbeef1234", 4321)


def test_the_pre_fix_worktree_name_still_parses():
    """Directories in the old bare-`<task_id>` shape exist under live worktree
    roots. They must stay attributable, or the doctor silently stops finding
    them — a reader that compares the whole name would have."""
    from no_human.config import worktree_owner

    assert worktree_owner("deadbeef1234") == ("deadbeef1234", None)


def test_an_unparseable_worktree_name_reports_no_owner():
    """No owner means "cannot prove this is dead", never "safe to delete"."""
    from no_human.config import worktree_owner

    assert worktree_owner("deadbeef1234.notapid.x") == ("deadbeef1234", None)


def test_pid_liveness_errs_toward_in_use():
    import os

    from no_human.config import pid_alive

    assert pid_alive(os.getpid()) is True
    assert pid_alive(1) is True, "a pid we cannot signal must read as in use"
    assert pid_alive(4194303) is False


def test_pid_liveness_delegates_to_openprocess_probe_on_windows(monkeypatch):
    """FINDING E: on Windows pid_alive must NOT go through os.kill(pid, 0) —
    signal 0 is CTRL_C_EVENT there, so os.kill is a Ctrl-C broadcast, not a
    liveness probe, and its errors can't tell a dead pid from a live non-group
    one. It delegates to the OpenProcess-based _windows_pid_alive tri-state: a
    no-such-process (False) reads DEAD, so a scheduler_heartbeat left by an
    ungracefully-killed instance stops blocking the new one; alive (True) and
    exists-but-not-ours (None) both read alive (err-toward-ALIVE)."""
    from no_human import config

    monkeypatch.setattr(config, "_IS_WINDOWS", True)
    monkeypatch.setattr(config, "_windows_pid_alive", lambda _pid: False)
    assert config.pid_alive(4321) is False, "no such process → dead"
    monkeypatch.setattr(config, "_windows_pid_alive", lambda _pid: True)
    assert config.pid_alive(4321) is True, "alive → alive"
    monkeypatch.setattr(config, "_windows_pid_alive", lambda _pid: None)
    assert config.pid_alive(4321) is True, "exists but not ours → err-toward-ALIVE"
