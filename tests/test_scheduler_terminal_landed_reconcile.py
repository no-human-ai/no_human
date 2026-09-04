"""TERMINAL failed/cancelled rows have no path back to DONE even when their
recorded work already landed on the base branch — the same "shipped work
poisons the board" problem `reconcile_landed_orphan`/`_reconcile_landed_orphan`
solved for non-terminal orphans (see `tests/test_scheduler_orphan_landed_reconcile.py`),
but for rows that already reached FAILED.

"Cancelled" is not a separate status: it is `FAILED` + a truthy
`context["cancel_reason"]` (see `core/scheduler.py`, `core/metrics.py`). So
this sweep's scope is exactly `status == FAILED`, with or without a
`cancel_reason` set.

This is the TERMINAL-row twin of the orphan reconciler:
`Store.reconcile_landed_terminal`, `Scheduler._reconcile_landed_terminal`,
and the narrow `TERMINAL_LANDED_RECONCILABLE` / `assert_terminal_landed_reconciliation`
gate in `core/task.py`. It reuses `vcs.pr_watcher.orphan_landed_evidence`
(LOCAL-GIT-ONLY, no network) verbatim, exactly like the orphan reconciler
does, and adds only a pure-regex sha extractor (`landing_sha_candidates`) so
a free-text `cancel_reason` can also be probed for a commit sha — never a PR
number resolved through any forge API.

These tests build a REAL git repo per test (no mocked git), following the
convention of the orphan-reconciler test file."""

from __future__ import annotations

import inspect
import subprocess

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


async def _failed_row(store, tmp_path, *, cancel_reason="", commit_sha="",
                       pr_url="", base="main"):
    """A FAILED row with an attempt recording *commit_sha*/*pr_url* and an
    optional `cancel_reason` — the shape `_reconcile_landed_terminal` reads
    via `latest_attempt_branch`/`latest_attempt_pr_url`/`context`."""
    repo = tmp_path / "repo"
    if not repo.exists():
        _init_repo(repo)
    t = Task.new("failed task", repo_path=str(repo))
    ctx = {"base_branch": base} if base else {}
    if cancel_reason:
        ctx["cancel_reason"] = cancel_reason
    t.context = ctx
    await store.create_task(t)
    await store.set_status(t, TaskStatus.FAILED, validate=False)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        attempt_id, branch_name="nh/x-1", commit_sha=commit_sha, pr_url=pr_url)
    return t, repo, attempt_id


def _sched(store, *, events=None):
    return Scheduler(store, lambda task=None: None,
                      on_event=(lambda k, txt: events.append((k, txt)))
                      if events is not None else (lambda k, txt: None))


@pytest.mark.parametrize("short", [False, True])
async def test_cancelled_row_naming_a_landed_sha_reconciles_to_done(
        store, tmp_path, short):
    repo = tmp_path / "repo"
    _init_repo(repo)
    landed_sha = _commit(repo, "g.txt", "feature\n", "add feature")
    sha_text = landed_sha[:9] if short else landed_sha

    t, _repo, _attempt_id = await _failed_row(
        store, tmp_path, cancel_reason=f"shipped as {sha_text} on main")

    events = []
    sched = _sched(store, events=events)
    n = await sched._reconcile_landed_terminal()

    assert n == 1
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    # `orphan_landed_evidence` echoes back whatever sha it was given — it
    # never expands an abbreviation to the full 40-hex form — so the short
    # parametrization lands the 9-hex candidate, not `landed_sha` itself.
    assert fresh.context.get("landed_sha") == sha_text
    assert fresh.context.get("landed_reconciled_from") == "failed"
    assert any(k == "terminal_reconciled" for k, _ in events)
    recorded = await store.list_events(t.id)
    assert any(e.get("kind") == "terminal_reconciled" for e in recorded)


async def test_failed_row_naming_an_unreachable_sha_stays_failed(
        store, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _run_git(repo, "checkout", "-b", "side")
    side_sha = _commit(repo, "s.txt", "side\n", "side work")
    _run_git(repo, "checkout", "main")

    t, _repo, _attempt_id = await _failed_row(
        store, tmp_path, cancel_reason=f"tried {side_sha}")

    sched = _sched(store)
    n = await sched._reconcile_landed_terminal()

    assert n == 0
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert "landed_sha" not in (fresh.context or {})


async def test_failed_row_with_no_evidence_is_untouched(
        store, tmp_path, monkeypatch):
    """No sha in the cancel reason, no attempt commit, no PR url — the row
    is left alone AND no git subprocess is spawned for it (there is nothing
    honest to probe)."""
    async def _tripwire(*args, **kwargs):
        raise AssertionError("git must not be invoked with no evidence at all")

    monkeypatch.setattr(pr_watcher_mod, "_git_rc", _tripwire)

    t, _repo, _attempt_id = await _failed_row(
        store, tmp_path, cancel_reason="no evidence here")

    sched = _sched(store)
    n = await sched._reconcile_landed_terminal()

    assert n == 0
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED


async def test_squash_merged_pr_from_the_recorded_url_reconciles(
        store, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _run_git(repo, "checkout", "-b", "nh/x-1")
    attempt_sha = _commit(repo, "w.txt", "wip\n", "wip commit")
    _run_git(repo, "checkout", "main")
    squash_sha = _commit(repo, "g.txt", "feature\n", "Add the feature (#7)")

    t, _repo, _attempt_id = await _failed_row(
        store, tmp_path, commit_sha=attempt_sha,
        pr_url="https://github.com/o/r/pull/7",
        cancel_reason="closed without landing (or so we thought)")

    events = []
    sched = _sched(store, events=events)
    n = await sched._reconcile_landed_terminal()

    assert n == 1
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert fresh.context.get("landed_sha") == squash_sha
    assert any(k == "terminal_reconciled" for k, _ in events)


async def test_row_without_recorded_base_branch_is_never_reconciled(
        store, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    landed_sha = _commit(repo, "g.txt", "feature\n", "add feature")

    t, _repo, _attempt_id = await _failed_row(
        store, tmp_path, base="", cancel_reason=f"shipped as {landed_sha}")
    assert "base_branch" not in (t.context or {})

    sched = _sched(store)
    n = await sched._reconcile_landed_terminal()

    assert n == 0
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED


async def test_reconcile_landed_terminal_refuses_malformed_evidence(
        store, tmp_path):
    t, _repo, _attempt_id = await _failed_row(store, tmp_path)
    fresh = await store.get_task(t.id)

    with pytest.raises(ValueError):
        await store.reconcile_landed_terminal(
            fresh, evidence={}, event={"source": "orchestrator",
                                        "kind": "terminal_reconciled",
                                        "text": "x"})


async def test_non_failed_status_refuses_terminal_reconciliation(
        store, tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _rev_parse(repo)
    t = Task.new("testing-stage task", repo_path=str(repo))
    await store.create_task(t)
    await store.set_status(t, TaskStatus.TESTING, validate=False)
    fresh = await store.get_task(t.id)

    with pytest.raises(IllegalTransition):
        await store.reconcile_landed_terminal(
            fresh, evidence={"kind": "commit", "sha": sha, "base": "main"},
            event={"source": "orchestrator", "kind": "terminal_reconciled",
                   "text": "x"})


def test_no_unvalidated_write_in_terminal_reconciliation_source():
    """Static guard against reintroducing the unvalidated-status-write
    anti-pattern into this new code path — a `validate=False` or
    `human_override` anywhere here would bypass the transition map this
    feature is built on (mirrors the orphan reconciler's own guard test)."""
    for fn in (Store.reconcile_landed_terminal,
               Scheduler._reconcile_landed_terminal,
               Scheduler._reconcile_one_landed_terminal,
               Scheduler._terminal_landed_evidence):
        src = inspect.getsource(fn)
        assert "validate=False" not in src
        assert "human_override" not in src


async def test_sweep_makes_no_network_calls(store, tmp_path, monkeypatch):
    """`orphan_landed_evidence` (reused verbatim) is documented
    LOCAL-GIT-ONLY; this pins it by failing loudly if the terminal sweep
    ever reaches the forge-CLI helper."""
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

    t, _repo, _attempt_id = await _failed_row(
        store, tmp_path, commit_sha=attempt_sha,
        pr_url="https://github.com/o/r/pull/7")

    n = await _sched(store)._reconcile_landed_terminal()

    fresh = await store.get_task(t.id)
    assert n == 1
    assert fresh.status is TaskStatus.DONE
    assert calls == []


async def test_second_pass_is_a_no_op(store, tmp_path):
    """The `landed_sha IS NULL` predicate in
    `landed_reconcilable_terminal_tasks` makes a repeat sweep cheap and
    idempotent: the row is `done` after the first pass and no longer
    `failed`, so it drops out of the candidate query entirely."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    landed_sha = _commit(repo, "g.txt", "feature\n", "add feature")

    t, _repo, _attempt_id = await _failed_row(
        store, tmp_path, cancel_reason=f"shipped as {landed_sha} on main")

    events = []
    sched = _sched(store, events=events)
    first = await sched._reconcile_landed_terminal()
    second = await sched._reconcile_landed_terminal()

    assert first == 1
    assert second == 0
    recorded = await store.list_events(t.id)
    assert len([e for e in recorded if e.get("kind") == "terminal_reconciled"]) == 1
