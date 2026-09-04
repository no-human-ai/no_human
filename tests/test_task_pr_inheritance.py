"""A resumed attempt must inherit the task's PR: `attempts.pr_url` empty on
the final (succeeded) attempt used to strand `AWAITING_APPROVAL` forever,
because `_check_open_pr` only ever read `ctx["pr_watch"]` — written at exactly
one place (`_finalize`, after a *delivering* PR open) — and never looked at
the attempts table or a still-open draft slot. Live incident: task
`16f850ae`, PR #230 opened as a *draft* (`pr_draft_created`), attempt 4 exited
through `_gate_already_satisfied` (a zero-diff ALREADY-SATISFIED claim) which
sets AWAITING_APPROVAL without ever writing `pr_watch` — the watcher logged
'watcher checked (awaiting_approval): nothing to do' hourly for over an hour
after the PR was merged.

These tests pin `no_human.vcs.task_pr.resolve_task_pr` (the single
task-level resolver: pr_watch -> newest non-empty attempt pr_url -> live
draft slot) and its wiring into `WakeWatcher._check_open_pr` and
`Orchestrator._gate_already_satisfied`'s backfill.

Reuses `test_pr_shipped.py`'s fixtures (`_squash_merge_repo`, `store`) rather
than refactoring that file.
"""

from __future__ import annotations

import logging
import subprocess


from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus
from no_human.vcs.pr_watcher import default_branch_shipped
from no_human.vcs.task_pr import resolve_task_pr, task_has_pr_evidence


def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _squash_merge_repo(tmp_path):
    """Branch commits a real change; main gets the SAME content via a fresh
    (squash-shaped) commit — branch head is NOT an ancestor of main, but the
    touched file's content matches. Copied from test_pr_shipped.py."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "squash: change a.txt (fresh sha, no lineage)")
    return repo


def _unshipped_repo(tmp_path):
    """Branch commits a real change that never made it to main at all.
    Copied from test_pr_shipped.py."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    _git(repo, "checkout", "main")
    return repo


async def _seed_attempts(store, task_id, rows):
    """rows: list of (pr_url, status) in the order they were run."""
    for n, (pr_url, status) in enumerate(rows, start=1):
        attempt_id = await store.create_attempt(task_id, n)
        await store.update_attempt(attempt_id, status=status, pr_url=pr_url)
    return await store.list_attempts(task_id)


# --------------------------- AC1: red-first --------------------------- #

async def test_closed_pr_completes_task_when_only_an_earlier_attempt_carries_the_url(
    tmp_path, store,
):
    repo = _squash_merge_repo(tmp_path)
    t = Task.new("shipped-check", repo_path=str(repo))
    t.context = {"pr_branch": "feature", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    await _seed_attempts(store, t.id, [
        ("https://github.com/o/r/pull/86", "failed"),
        ("", "succeeded"),
    ])

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    out = await w._check_open_pr(t)
    assert out == "shipped_pr_closed"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_closed_draft_pr_completes_an_already_satisfied_task(tmp_path, store):
    """The live incident shape: task 16f850ae. No pr_watch, no attempt ever
    recorded a pr_url, only a surviving draft slot from _open_draft_pr_for_review."""
    repo = _squash_merge_repo(tmp_path)
    t = Task.new("shipped-check", repo_path=str(repo))
    t.context = {
        "pr_draft_created": "https://github.com/o/r/pull/230",
        "pr_draft_branch": "feature",
        "base_branch": "main",
    }
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    await _seed_attempts(store, t.id, [
        ("", "failed"),
        ("", "interrupted"),
        ("", "failed"),
        ("", "succeeded"),
    ])

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    out = await w._check_open_pr(t)
    assert out == "shipped_pr_closed"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


# --------------------------- AC2: resolver --------------------------- #

async def test_resolver_prefers_pr_watch_then_newest_attempt_then_draft(tmp_path, store):
    # Case 1: pr_watch wins even when attempts and a draft also exist.
    t1 = Task.new("t1")
    t1.context = {
        "pr_watch": "https://github.com/o/r/pull/1",
        "pr_branch": "b1",
        "pr_draft_created": "https://github.com/o/r/pull/9",
        "pr_draft_branch": "b9",
    }
    await store.create_task(t1)
    await _seed_attempts(store, t1.id, [("https://github.com/o/r/pull/2", "succeeded")])
    resolved = await resolve_task_pr(store, t1)
    assert resolved.url == "https://github.com/o/r/pull/1"
    assert resolved.source == "pr_watch"

    # Case 2: no pr_watch -> newest non-empty attempt wins over a draft.
    t2 = Task.new("t2")
    t2.context = {
        "pr_draft_created": "https://github.com/o/r/pull/9",
        "pr_draft_branch": "b9",
    }
    await store.create_task(t2)
    await _seed_attempts(store, t2.id, [("https://github.com/o/r/pull/3", "succeeded")])
    resolved = await resolve_task_pr(store, t2)
    assert resolved.url == "https://github.com/o/r/pull/3"
    assert resolved.source == "attempt"

    # Case 3: no pr_watch, no attempt pr_url -> draft wins.
    t3 = Task.new("t3")
    t3.context = {
        "pr_draft_created": "https://github.com/o/r/pull/9",
        "pr_draft_branch": "b9",
    }
    await store.create_task(t3)
    await _seed_attempts(store, t3.id, [("", "succeeded")])
    resolved = await resolve_task_pr(store, t3)
    assert resolved.url == "https://github.com/o/r/pull/9"
    assert resolved.source == "draft"


async def test_resolver_picks_the_newest_non_empty_attempt_url(tmp_path, store):
    t = Task.new("t")
    await store.create_task(t)
    await _seed_attempts(store, t.id, [
        ("https://github.com/o/r/pull/5", "failed"),
        ("", "interrupted"),
        ("https://github.com/o/r/pull/9", "succeeded"),
    ])
    resolved = await resolve_task_pr(store, t)
    assert resolved.url == "https://github.com/o/r/pull/9"
    assert resolved.source == "attempt"


async def test_resolution_source_is_logged(tmp_path, store, caplog):
    repo = _squash_merge_repo(tmp_path)
    t = Task.new("shipped-check", repo_path=str(repo))
    t.context = {"pr_branch": "feature", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    await _seed_attempts(store, t.id, [("https://github.com/o/r/pull/86", "succeeded")])

    async def pr_state(url):
        return "OPEN"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    with caplog.at_level(logging.INFO, logger="no_human.wake"):
        await w._check_open_pr(t)
    assert any(
        "resolved from attempt" in r.message and "pull/86" in r.message
        for r in caplog.records
    )


# --------------------------- AC3: backfill --------------------------- #

async def test_already_satisfied_attempt_backfills_inherited_pr_url(tmp_path, store):
    t = Task.new("t")
    await store.create_task(t)
    attempt1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        attempt1, status="failed", pr_url="https://github.com/o/r/pull/42")
    attempt2 = await store.create_attempt(t.id, 2)
    await store.update_attempt(attempt2, status="succeeded", pr_url="")

    fresh = await store.get_task(t.id)
    resolved = await resolve_task_pr(store, fresh)
    assert resolved.url == "https://github.com/o/r/pull/42"
    if resolved.url:
        await store.update_attempt(attempt2, pr_url=resolved.url)

    rows = await store.list_attempts(t.id)
    assert rows[-1]["pr_url"] == "https://github.com/o/r/pull/42"


async def test_backfill_is_a_noop_when_no_pr_exists(tmp_path, store):
    t = Task.new("t")
    await store.create_task(t)
    attempt1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt1, status="succeeded", pr_url="")

    fresh = await store.get_task(t.id)
    resolved = await resolve_task_pr(store, fresh)
    assert resolved.url == ""
    if resolved.url:
        await store.update_attempt(attempt1, pr_url=resolved.url)

    rows = await store.list_attempts(t.id)
    assert rows[-1]["pr_url"] == ""


# --------------------------- AC4: regression --------------------------- #

async def test_task_with_no_pr_anywhere_still_heartbeats(tmp_path, store):
    import time as _time
    from datetime import datetime, timezone

    t = Task.new("t")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    w = WakeWatcher(store, {})
    now = datetime.now(timezone.utc)
    out = await w._evaluate(t, now=now)
    assert out is None

    await w._heartbeat(t, now=now)
    events = await store.list_events(t.id)
    assert any(
        e.get("kind") == "wake_tick"
        and e.get("text") == "watcher checked (awaiting_approval): nothing to do"
        for e in events
    )
    _ = _time.time  # keep import used defensively; heartbeat stamps its own ts


async def test_null_and_empty_pr_urls_are_both_absent(tmp_path, store):
    t = Task.new("t")
    await store.create_task(t)
    attempt1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt1, status="failed", pr_url=None)
    attempt2 = await store.create_attempt(t.id, 2)
    await store.update_attempt(attempt2, status="failed", pr_url="   ")

    fresh = await store.get_task(t.id)
    resolved = await resolve_task_pr(store, fresh)
    assert (resolved.url, resolved.branch, resolved.source) == ("", "", "none")


# ------------------------- task_has_pr_evidence ------------------------- #

async def test_evidence_falls_back_to_a_pr_open_event_when_nothing_else_has_it(
    tmp_path, store,
):
    """`resolve_task_pr` is empty (no pr_watch, no attempt url, no draft) —
    the only trace is a persisted `pr_open` event, whose URL lives in
    `text`. This is the belt-and-braces rung restore-approval must now see."""
    t = Task.new("t")
    await store.create_task(t)
    await store.save_events(t.id, [{
        "source": "test", "kind": "pr_open",
        "text": "https://example.invalid/pr/480", "ts": 0.0}])

    fresh = await store.get_task(t.id)
    assert await task_has_pr_evidence(store, fresh) == "https://example.invalid/pr/480"


async def test_evidence_is_empty_when_nothing_resolves_anywhere(tmp_path, store):
    t = Task.new("t")
    await store.create_task(t)

    fresh = await store.get_task(t.id)
    assert await task_has_pr_evidence(store, fresh) == ""


async def test_evidence_excludes_a_url_listed_as_abandoned(tmp_path, store):
    """A URL present only via the event-scan rung is still subtracted if it
    is also listed in `abandoned_pr_urls`."""
    url = "https://example.invalid/pr/253"
    t = Task.new("t")
    t.context = {"abandoned_pr_urls": [url]}
    await store.create_task(t)
    await store.save_events(t.id, [{
        "source": "test", "kind": "pr_draft", "text": "", "pr_url": url,
        "ts": 0.0}])

    fresh = await store.get_task(t.id)
    assert await task_has_pr_evidence(store, fresh) == ""
