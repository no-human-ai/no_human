"""The hard-kill twin of the graceful stop.

EVIDENCE (live DB 2026-08-20): 342 interrupted attempt rows, 254 with no
commit_sha, 25 with 20+ coder turns and no commit. A task killed mid-coding
(SIGKILL, OOM, crash — not `nh stop`) keeps its dirty worktree through the
startup sweep (`sweep_stale_worktrees` skips non-terminal tasks on purpose:
IMPLEMENTING is still "may run again"), but the task's NEXT run reaps it in
`Orchestrator._reap_dead_worktrees` with no salvage commit, and
`scheduler._ORPHANABLE` excludes IMPLEMENTING so no checkpoint is ever
stamped. `reset_agent_workspace` then starts the next attempt from base —
tens of coder turns discarded, every time.

`fix/graceful-stop-and-test-evidence` (tests/test_server_stop_checkpoint.py)
already pins the SIGTERM half: `request_server_stop` -> `_honor_server_stop`
commits + stamps before the process exits. This file pins the half that
branch cannot reach — the process is already dead, nothing inside it can run
a `finally` block, so the ONLY place left to salvage from is the NEXT
process's startup, before anything reaps the directory. That is
`core.worktree.salvage_dead_worktrees`, wired into `Scheduler.run_forever`
between the terminal-leftover sweep and orphan recovery.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

import pytest

from no_human.blockers import MACHINE_REQUEUE_PROVENANCE, resume_provenance
from no_human.config import load_config
from no_human.core.task import Task, TaskStatus
from no_human.vcs.git import GitRepo

# A pid that cannot possibly be alive — pinned in test_worktree_isolation.py
# (`assert pid_alive(4194303) is False`); reused here so every worktree test
# in the repo agrees on what "dead" means.
_DEAD_PID = "4194303"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _head(cwd) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _status(cwd) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=cwd,
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def cfg(tmp_path):
    c = load_config(tmp_path / "config.yaml")
    c.data["isolation"]["worktree_root"] = str(tmp_path / "wt")
    return c


def _main_repo(tmp_path, name, *, base_branch="main"):
    """A real repo with one commit, checked out on *base_branch*."""
    main = tmp_path / name
    main.mkdir()
    _git(main, "init", "-b", base_branch)
    _git(main, "config", "user.email", "u@e.com")
    _git(main, "config", "user.name", "u")
    (main / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "init")
    return main, _head(main)


def _add_worktree(main, root, task_id, pid, branch, *, base="main"):
    """A real `git worktree add` linked worktree, named the way the runtime
    names them: `<task_id>.<owner_pid>.<token>` — the shape `worktree_owner`
    parses and `is_agent_worktree` requires."""
    root.mkdir(parents=True, exist_ok=True)
    wt = root / f"{task_id}.{pid}.abcd1234"
    _git(main, "worktree", "add", "-b", branch, str(wt), base)
    return wt


def _dirty(wt):
    (wt / "calc.py").write_text("def add(a, b):\n    return a + b  # edited\n")
    (wt / "new_file.py").write_text("# uncommitted new work\n")


async def _seed_task(store, main, *, status=TaskStatus.IMPLEMENTING):
    t = Task.new("fix the thing", repo_path=str(main))
    t.status = status
    await store.create_task(t)
    return t


async def _seed_attempt(store, task, branch):
    attempt_id = await store.create_attempt(task.id, 1)
    await store.update_attempt(attempt_id, branch_name=branch)
    return attempt_id


async def _build(store, cfg, tmp_path, name, *, pid=_DEAD_PID,
                  branch="no-human/task-x", dirty=True, base_branch="main",
                  seed_attempt=True):
    """One dead-IMPLEMENTING-worktree scenario: real repo, real linked
    worktree, a task row, and (usually) an open attempt row — everything
    `salvage_dead_worktrees` needs to find and act on it."""
    main, base_sha = _main_repo(tmp_path, f"{name}-main", base_branch=base_branch)
    task = await _seed_task(store, main)
    root = tmp_path / "wt"
    wt = _add_worktree(main, root, task.id, pid, branch, base=base_branch)
    if dirty:
        _dirty(wt)
    attempt_id = None
    if seed_attempt:
        attempt_id = await _seed_attempt(store, task, branch)
    cfg.data["isolation"]["worktree_root"] = str(root)
    return {
        "main": main, "base_sha": base_sha, "task": task, "wt": wt,
        "attempt_id": attempt_id, "root": root, "branch": branch,
    }


def _open(wt, cfg):
    g = cfg.data["git"]
    return GitRepo(
        wt, identity_name=g["agent_identity_name"],
        identity_email=g["agent_identity_email"],
        never_push_to=g["never_push_to"],
    )


# --------------------------------------------------------------------------- #
# AC1 — the salvage itself                                                     #
# --------------------------------------------------------------------------- #


async def test_a_dead_implementing_worktree_is_committed_and_stamped(
        tmp_path, store, cfg):
    from no_human.core.worktree import salvage_dead_worktrees

    b = await _build(store, cfg, tmp_path, "dead")

    result = await salvage_dead_worktrees(store, cfg.data, live=set())

    assert result == (1, 0)
    assert _status(b["wt"]) == "", "the worktree must be clean after salvage"

    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=b["wt"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert subject.startswith("[WIP-PARTIAL]")

    changed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"], cwd=b["wt"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert "calc.py" in changed, "the edited tracked file must be in the commit"
    assert "new_file.py" in changed, "the new untracked file must be in the commit"

    new_head = _head(b["wt"])
    assert new_head != b["base_sha"]

    task = await store.get_task(b["task"].id)
    assert task.context["resume_from"] == {
        "sha": new_head, "branch": b["branch"], "by": "hard_kill_salvage",
    }

    attempts = await store.list_attempts(b["task"].id)
    assert len(attempts) == 1
    att = attempts[0]
    assert att["status"] == "interrupted"
    assert att["commit_sha"] == new_head
    assert att["infra_failure"] == 1


async def test_the_salvage_checkpoint_survives_a_repairable_manifest_refusal(
        tmp_path, store, cfg, monkeypatch):
    """`_salvage_one` routes its `[WIP-PARTIAL]` commit through
    `commit_with_manifest_repair` (mirroring the seam
    `Orchestrator._checkpoint_commit` uses) rather than a raw
    `repo.commit_all(...)` — the pre-commit manifest gate is live for this
    worktree too (`push_hook.py:72` mirrors it via the per-worktree
    `hooksPath`), so a stale pin at the moment of a hard kill must be
    repaired-and-retried here exactly as on the graceful-stop and
    attempt-timeout paths. Mutation proof: reverting the call site to a raw
    `repo.commit_all(...)` leaves `calls` empty."""
    from no_human.core.worktree import salvage_dead_worktrees
    from no_human.vcs import manifest_repair as manifest_repair_mod

    b = await _build(store, cfg, tmp_path, "repairable")

    calls: list[tuple] = []

    def fake(repo_arg, paths, message, on_repair=None):
        calls.append((paths, message))
        if on_repair:
            on_repair(["RELEASE_MANIFEST.txt"], "1 stale pin(s)")
        return repo_arg.commit_all(message)

    monkeypatch.setattr(manifest_repair_mod, "commit_with_manifest_repair", fake)

    result = await salvage_dead_worktrees(store, cfg.data, live=set())

    assert result == (1, 0)
    assert calls, "the seam was never invoked — reverting to a raw commit_all empties this"
    assert calls[0][1].startswith("[WIP-PARTIAL]")

    new_head = _head(b["wt"])
    task = await store.get_task(b["task"].id)
    assert task.context["resume_from"]["sha"] == new_head
    assert task.context["resume_from"]["by"] == "hard_kill_salvage"

    attempts = await store.list_attempts(b["task"].id)
    assert attempts[0]["commit_sha"] == new_head


async def test_a_live_owner_pid_is_never_salvaged(tmp_path, store, cfg):
    import os

    from no_human.core.worktree import salvage_dead_worktrees

    b = await _build(store, cfg, tmp_path, "live", pid=str(os.getpid()))
    before = _head(b["wt"])
    before_status = _status(b["wt"])

    result = await salvage_dead_worktrees(store, cfg.data, live=set())

    assert result == (0, 1)
    assert _status(b["wt"]) == before_status, "a live-owner worktree must be untouched"
    assert _head(b["wt"]) == before

    task = await store.get_task(b["task"].id)
    assert not (task.context or {}).get("resume_from")


async def test_a_clean_worktree_is_not_stamped(tmp_path, store, cfg):
    from no_human.core.worktree import salvage_dead_worktrees

    b = await _build(store, cfg, tmp_path, "clean", dirty=False)
    before = _head(b["wt"])

    result = await salvage_dead_worktrees(store, cfg.data, live=set())

    assert result == (0, 1)
    assert _head(b["wt"]) == before

    task = await store.get_task(b["task"].id)
    assert not (task.context or {}).get("resume_from")


async def test_a_protected_branch_is_never_committed_to(tmp_path, store, cfg, caplog):
    from no_human.core.worktree import salvage_dead_worktrees

    never_push_to = cfg.data["git"]["never_push_to"]
    assert any("release" in p for p in never_push_to), (
        "fixture assumption: default never_push_to includes a release/* pattern")

    b = await _build(store, cfg, tmp_path, "protected", branch="release/1.0")
    before = _head(b["wt"])
    before_status = _status(b["wt"])

    with caplog.at_level(logging.WARNING, logger="no_human.worktree"):
        result = await salvage_dead_worktrees(store, cfg.data, live=set())

    assert result == (0, 1)
    assert _status(b["wt"]) == before_status, "a protected-branch worktree stays dirty"
    assert _head(b["wt"]) == before

    task = await store.get_task(b["task"].id)
    assert not (task.context or {}).get("resume_from")
    assert any(
        "protected" in r.getMessage() and "release/1.0" in r.getMessage()
        for r in caplog.records
    ), "the skip must name the protected branch"


async def test_a_human_gated_resume_from_is_left_alone(tmp_path, store, cfg):
    from no_human.core.worktree import salvage_dead_worktrees

    b = await _build(store, cfg, tmp_path, "human")
    await store.merge_context(b["task"].id, {
        "resume_from": resume_provenance(
            {"sha": b["base_sha"], "branch": "some/other-branch"}, "human"),
    })
    before_status = _status(b["wt"])

    result = await salvage_dead_worktrees(store, cfg.data, live=set())

    assert result == (0, 1)
    assert _status(b["wt"]) == before_status, "a human-gated worktree must stay dirty"

    task = await store.get_task(b["task"].id)
    assert task.context["resume_from"] == {
        "sha": b["base_sha"], "branch": "some/other-branch", "by": "human",
    }


async def test_a_legacy_unstamped_resume_from_reads_as_human(tmp_path, store, cfg):
    """A `resume_from` with a sha and no `by` predates the provenance field —
    it must read as human-gated, not as fair game (the trap `resume_provenance`
    exists to close, applied one layer up: an ABSENT `by` is not a machine
    `by`)."""
    from no_human.core.worktree import salvage_dead_worktrees

    b = await _build(store, cfg, tmp_path, "legacy")
    await store.merge_context(b["task"].id, {
        "resume_from": {"sha": b["base_sha"], "branch": "some/other-branch"},
    })
    before_status = _status(b["wt"])

    result = await salvage_dead_worktrees(store, cfg.data, live=set())

    assert result == (0, 1)
    assert _status(b["wt"]) == before_status

    task = await store.get_task(b["task"].id)
    assert task.context["resume_from"]["sha"] == b["base_sha"]
    assert not task.context["resume_from"].get("by")


# --------------------------------------------------------------------------- #
# B2 — salvage clears a stale gate-failure record                              #
# --------------------------------------------------------------------------- #


async def test_a_salvaged_commit_does_not_inherit_an_earlier_gates_rejection(
        tmp_path, store, cfg):
    """A dead worktree salvaged into a `[WIP-PARTIAL]` commit must not inherit
    an EARLIER attempt's gate-failure record. `_salvage_one`'s
    `store.merge_context(...)` patch used to clear only `turns_used`, leaving
    `failed_gate`/`failed_gate_summary`/`own_partial` from an earlier
    `_persist_handoff(gate=...)` call to survive the RFC 7396 merge onto this
    salvaged commit — so `build_resume_digest` told the next attempt a gate
    REJECTED a commit no gate ever saw, and dropped the WIP-PARTIAL framing."""
    from no_human.core.prompt_blocks import build_resume_digest
    from no_human.core.worktree import salvage_dead_worktrees

    b = await _build(store, cfg, tmp_path, "gate-stale")
    old_sha = "e" * 40
    # Seed the prior handoff through the real writer's payload shape, mirroring
    # what `_persist_handoff(gate="tests", ...)` would have written for an
    # earlier attempt on this same task.
    await store.merge_context(b["task"].id, {
        "handoff": {
            "failed_gate": "tests",
            "failed_gate_summary": "tests failed: test_add AssertionError",
            "own_partial": True,
            "wip_sha": old_sha,
            "changed_files": ["calc.py"],
        },
    })
    premise = await store.get_task(b["task"].id)
    assert (premise.context or {}).get("handoff", {}).get("failed_gate") == "tests", (
        "fixture premise: the prior gate-failure record must be seeded before salvage")

    result = await salvage_dead_worktrees(store, cfg.data, live=set())
    assert result == (1, 0)

    new_sha = _head(b["wt"])
    assert new_sha != old_sha

    refreshed = await store.get_task(b["task"].id)
    handoff = (refreshed.context or {}).get("handoff") or {}
    assert handoff.get("wip_sha") == new_sha, handoff
    assert handoff.get("failed_gate") is None, handoff
    assert handoff.get("failed_gate_summary") is None, handoff
    assert handoff.get("own_partial") is None, handoff

    d = build_resume_digest(refreshed)
    assert d, "the digest must not be empty"
    assert "REJECTED that commit" not in d
    assert "gate REJECTED" not in d
    assert "tests gate" not in d
    assert "Fix that failure in the inherited commit" not in d
    assert f"committed as WIP-PARTIAL {new_sha[:8]}" in d


# --------------------------------------------------------------------------- #
# AC3 — the next run picks the salvage up                                      #
# --------------------------------------------------------------------------- #


async def test_the_next_run_branches_from_the_salvaged_sha(tmp_path, store, cfg):
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.worktree import salvage_dead_worktrees

    b = await _build(store, cfg, tmp_path, "next-run")
    result = await salvage_dead_worktrees(store, cfg.data, live=set())
    assert result == (1, 0)

    task = await store.get_task(b["task"].id)
    salvaged_sha = task.context["resume_from"]["sha"]
    repo = _open(b["wt"], cfg)
    orch = Orchestrator.__new__(Orchestrator)

    branch_point = orch._resume_branch_point(repo, task.context, attempt_n=1)

    assert branch_point == salvaged_sha
    assert branch_point != b["base_sha"]


async def test_the_already_satisfied_gate_is_armed_for_a_hard_kill_salvage(
        tmp_path, store, cfg):
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.worktree import salvage_dead_worktrees

    assert "hard_kill_salvage" in MACHINE_REQUEUE_PROVENANCE

    b = await _build(store, cfg, tmp_path, "gate")
    result = await salvage_dead_worktrees(store, cfg.data, live=set())
    assert result == (1, 0)

    task = await store.get_task(b["task"].id)
    repo = _open(b["wt"], cfg)
    orch = Orchestrator.__new__(Orchestrator)

    eligible, why = orch._already_satisfied_eligible(task, repo, b["base_sha"])

    assert eligible is False and why, (
        "a salvaged [WIP-PARTIAL] diff with no recorded review verdict must "
        "force a full review, not a zero-diff already-satisfied claim")


# --------------------------------------------------------------------------- #
# Boot wiring + safety                                                         #
# --------------------------------------------------------------------------- #


async def test_salvage_runs_at_boot_between_the_sweep_and_orphan_recovery(store):
    from no_human.core.scheduler import Scheduler

    sched = Scheduler(store, lambda *a, **k: object(), config={})
    order = []

    async def _sweep():
        order.append("sweep")

    async def _salvage():
        order.append("salvage")

    async def _recover(*, startup=True):
        order.append("recover")

    sched._sweep_stale_worktrees = _sweep
    sched._salvage_dead_worktrees = _salvage
    sched._recover_orphans = _recover

    stop = asyncio.Event()
    stop.set()
    await sched.run_forever(stop=stop)

    assert order == ["sweep", "salvage", "recover"], (
        "salvage must run once, after the terminal-leftover sweep (so it never "
        "races a directory the sweep is about to delete) and before orphan "
        "recovery (which can start creating new worktrees)")


async def test_a_salvage_that_throws_never_blocks_boot(store, monkeypatch, caplog):
    from no_human.core import scheduler as scheduler_mod

    sched = scheduler_mod.Scheduler(store, lambda *a, **k: object(), config={})

    async def _boom(*a, **k):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(scheduler_mod, "salvage_dead_worktrees", _boom)

    stop = asyncio.Event()
    stop.set()
    with caplog.at_level(logging.ERROR, logger="no_human.scheduler"):
        await sched.run_forever(stop=stop)

    assert any(
        "salvaging dead worktrees failed" in r.getMessage() for r in caplog.records
    ), "a salvage failure must be logged, not silently dropped, and must not " \
       "prevent orphan recovery from running"


async def test_a_second_pass_salvages_nothing(tmp_path, store, cfg):
    from no_human.core.worktree import salvage_dead_worktrees

    b = await _build(store, cfg, tmp_path, "second-pass")

    first = await salvage_dead_worktrees(store, cfg.data, live=set())
    assert first == (1, 0)
    head_after_first = _head(b["wt"])

    second = await salvage_dead_worktrees(store, cfg.data, live=set())

    assert second == (0, 1), (
        "the salvaged attempt is no longer 'in_progress', so a second pass "
        "must find nothing left to salvage")
    assert _head(b["wt"]) == head_after_first
