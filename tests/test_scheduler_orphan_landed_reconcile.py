"""Orphan recovery marks rows failed/requeued without checking whether the
attempt's PR/commit actually landed — landed work must reconcile to DONE
instead, via a validated transition (`Store.reconcile_landed_orphan`,
`Scheduler._reconcile_landed_orphan`, `vcs.pr_watcher.orphan_landed_evidence`).

`Scheduler._recover_orphans` used to decide purely from task STATUS: a row
found in a mid-run status with no live worker attached was unconditionally
flipped to IMPLEMENTING (requeued), even when the attempt's commit was
already an ancestor of the base branch or its PR had already squash-merged.
That requeue eventually fails or duplicates shipped work, and poisons the
board's failed-count/cost/success analysis with rows that actually landed.

These tests build a REAL git repo per test (no mocked git) so the local-only
probe (`orphan_landed_evidence` — ancestry check + squash-subject `(#N)`
scan, no network) is exercised for real, exactly as `_recover_orphans` calls
it."""

from __future__ import annotations

import inspect
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from no_human.core.db import Store
from no_human.core.scheduler import Scheduler
from no_human.core.task import IllegalTransition, Task, TaskStatus
from no_human.vcs import pr_watcher as pr_watcher_mod


def _run_git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args],
                    check=True, capture_output=True, text=True)


def _rev_parse(repo, ref="HEAD") -> str:
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-b", "main")
    _run_git(repo, "config", "user.email", "t@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("base\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", "base commit")


def _commit(repo, filename, content, subject) -> str:
    (repo / filename).write_text(content)
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", subject)
    return _rev_parse(repo)


async def _age_row(store: Store, task_id: str, seconds: float = 3600) -> None:
    """Back-date a row's `updated_at` past `Scheduler._row_is_live`'s grace
    window, so `_recover_orphans` treats it as an orphan rather than as live
    (mirrors `tests/test_scheduler.py::_age_row`)."""
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=seconds)).isoformat()
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task_id))
    await store.db.commit()


async def _orphan(store, tmp_path, *, status=TaskStatus.TESTING,
                   base="main", commit_sha="", pr_url="", age=True):
    """A checkpointed orphan row with an attempt recording *commit_sha*/
    *pr_url* — the shape `_reconcile_landed_orphan` reads via
    `latest_attempt_branch`/`latest_attempt_pr_url`."""
    repo = tmp_path / "repo"
    if not repo.exists():
        _init_repo(repo)
    t = Task.new("orphaned task", repo_path=str(repo))
    t.context = {"base_branch": base}
    await store.create_task(t)
    await store.set_status(t, status, validate=False)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        attempt_id, branch_name="nh/x-1", commit_sha=commit_sha, pr_url=pr_url)
    if age:
        await _age_row(store, t.id)
    return t, repo, attempt_id


def _sched(store, *, events=None):
    return Scheduler(store, lambda task=None: None,
                      on_event=(lambda k, txt: events.append((k, txt)))
                      if events is not None else (lambda k, txt: None))


async def test_orphan_with_reachable_commit_reconciles_to_done(store, tmp_path):
    t, repo, attempt_id = await _orphan(store, tmp_path)
    landed_sha = _commit(repo, "g.txt", "feature\n", "add feature")
    await store.update_attempt(attempt_id, commit_sha=landed_sha)

    events = []
    sched = _sched(store, events=events)
    await sched._recover_orphans()

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert fresh.context.get("landed_sha") == landed_sha
    assert any(k == "orphan_reconciled" for k, _ in events)
    recorded = await store.list_events(t.id)
    assert any(e.get("kind") == "orphan_reconciled" for e in recorded)
    # No requeue event was ALSO written for this row.
    assert not any(e.get("kind") == "orphan_recovered" for e in recorded)


async def test_orphan_with_squash_merged_pr_number_reconciles_to_done(
        store, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    # The attempt's own commit never lands verbatim (squash rewrites it).
    _run_git(repo, "checkout", "-b", "nh/x-1")
    attempt_sha = _commit(repo, "w.txt", "wip\n", "wip commit")
    _run_git(repo, "checkout", "main")
    squash_sha = _commit(repo, "g.txt", "feature\n", "Add the feature (#7)")

    t, _repo, _attempt_id = await _orphan(
        store, tmp_path, commit_sha=attempt_sha,
        pr_url="https://github.com/o/r/pull/7")

    events = []
    sched = _sched(store, events=events)
    await sched._recover_orphans()

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert fresh.context.get("landed_sha") == squash_sha
    assert any(k == "orphan_reconciled" for k, _ in events)


async def test_unlanded_orphan_still_requeues_exactly_as_today(store, tmp_path):
    t, repo, _attempt_id = await _orphan(
        store, tmp_path, commit_sha="deadbeef" * 5,
        pr_url="https://github.com/o/r/pull/99")

    events = []
    sched = _sched(store, events=events)
    await sched._recover_orphans()

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    recorded = await store.list_events(t.id)
    assert any(e.get("kind") == "orphan_recovered" for e in recorded)
    assert not any(e.get("kind") == "orphan_reconciled" for e in recorded)


async def test_liveness_gate_runs_before_reconciliation(store, tmp_path):
    """A live row (fresh `updated_at`, no worker-death evidence) must be left
    completely untouched — neither requeued NOR reconciled — even when its
    commit has already landed. `_row_is_live` stays the first check, exactly
    as before this change."""
    t, repo, attempt_id = await _orphan(store, tmp_path, age=False)
    landed_sha = _commit(repo, "g.txt", "feature\n", "add feature")
    await store.update_attempt(attempt_id, commit_sha=landed_sha)

    sched = _sched(store)
    await sched._recover_orphans()

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.TESTING  # untouched


async def test_illegal_source_status_refuses_reconciliation(store, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _rev_parse(repo)
    t = Task.new("context-stage task", repo_path=str(repo))
    await store.create_task(t)
    await store.set_status(t, TaskStatus.CONTEXT, validate=False)
    fresh = await store.get_task(t.id)

    with pytest.raises(IllegalTransition):
        await store.reconcile_landed_orphan(
            fresh, evidence={"kind": "commit", "sha": sha, "base": "main"},
            event={"source": "orchestrator", "kind": "orphan_reconciled",
                   "text": "x"})


async def test_reconcile_landed_orphan_refuses_malformed_evidence(store, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    t = Task.new("testing-stage task", repo_path=str(repo))
    await store.create_task(t)
    await store.set_status(t, TaskStatus.TESTING, validate=False)
    fresh = await store.get_task(t.id)

    with pytest.raises(ValueError):
        await store.reconcile_landed_orphan(
            fresh, evidence={}, event={"source": "orchestrator",
                                        "kind": "orphan_reconciled", "text": "x"})


def test_no_unvalidated_write_in_reconciliation_source():
    """Static guard against reintroducing the unvalidated-status-write
    anti-pattern into this new code path — a `validate=False` or
    `human_override` anywhere here would bypass the transition map this
    feature is built on."""
    for fn in (Store.reconcile_landed_orphan, Scheduler._reconcile_landed_orphan):
        src = inspect.getsource(fn)
        assert "validate=False" not in src
        assert "human_override" not in src


async def test_sweep_makes_no_network_calls_while_reconciling(
        store, tmp_path, monkeypatch):
    """`orphan_landed_evidence` is documented LOCAL-GIT-ONLY; this pins it by
    failing loudly if the sweep ever reaches the forge-CLI helper."""
    calls = []

    async def _tripwire(*args, **kwargs):
        calls.append((args, kwargs))
        return "", 1

    monkeypatch.setattr(pr_watcher_mod, "_run_cli", _tripwire)

    repo = tmp_path / "repo"
    _init_repo(repo)
    _run_git(repo, "checkout", "-b", "nh/x-1")
    attempt_sha = _commit(repo, "w.txt", "wip\n", "wip commit")
    _run_git(repo, "checkout", "main")
    _commit(repo, "g.txt", "feature\n", "Add the feature (#7)")

    t, _repo, _attempt_id = await _orphan(
        store, tmp_path, commit_sha=attempt_sha,
        pr_url="https://github.com/o/r/pull/7")

    await _sched(store)._recover_orphans()

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert calls == []


async def test_pr_number_prefix_does_not_false_match(store, tmp_path):
    """PR #4 must not be satisfied by a subject that only contains `(#42)` —
    an anchored match, not a substring one."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _run_git(repo, "checkout", "-b", "nh/x-1")
    attempt_sha = _commit(repo, "w.txt", "wip\n", "wip commit")
    _run_git(repo, "checkout", "main")
    _commit(repo, "g.txt", "unrelated\n", "Unrelated change (#42)")

    t, _repo, _attempt_id = await _orphan(
        store, tmp_path, commit_sha=attempt_sha,
        pr_url="https://github.com/o/r/pull/4")

    await _sched(store)._recover_orphans()

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING  # requeued, not reconciled
