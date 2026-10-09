"""AC5 (2026-10-09, "nh approve lands a PR as closed, not merged" fix):
the new head-branch push `land_task` now does — pushing the landed squash
sha onto the PR's OWN branch, force-with-lease, before the default-branch
push — must not itself re-wake `WakeWatcher` or start a fresh review
round. That push changes the PR's head sha (a fresh squash commit, never
previously reviewed) and, on forges that recompute `mergeable`/CI status
on every push, can momentarily reset both — exactly the kind of
"something about this PR changed" signal `_check_open_pr`'s ladder
watches for. Three guards:

  1. A DONE task is never re-evaluated at all (`_is_terminal`
     short-circuits `_evaluate` before any forge poll runs) — the
     ordinary case, since the host (`api/app.py` / CLI) flips the task to
     DONE as part of the same successful `land_task` call that did the
     push.
  2. A PR the forge now reports MERGED (the NORMAL outcome of this fix)
     completes the task via the existing "merged" rung — the fresh head
     sha is not treated as something needing a new review round, and the
     task does not fall into the CLOSED/escalation branch.
  3. With the PR still OPEN but otherwise nominal (no new comments, no
     merge conflict, CI green) — the shape right after the head push
     lands and before the forge settles on MERGED — no rung fires.
     Nothing in the ladder keys off the head sha itself, so a sha change
     with no other signal is inert.

No real git repo needed — `WakeWatcher`'s own checkers are faked, the
same idiom `tests/test_wake_pr_closed_repair.py` uses for the CLOSED
rung.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus

PR_URL = "https://github.com/o/r/pull/652"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _task_awaiting_approval(store, *, url=PR_URL):
    """AWAITING_APPROVAL with a `pr_open` event on record — the shape
    `_check_open_pr` expects, and the status a task is in exactly while
    `land_task`'s head-and-base pushes run (the host flips it to DONE only
    AFTER `land_task` returns ok)."""
    t = Task.new("land-head-push-check", repo_path="/tmp/does-not-matter")
    t.context = {"pr_watch": url, "pr_branch": "feature", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    await store.save_events(t.id, [
        {"source": "watcher", "kind": "pr_open", "text": f"opened {url}",
         "ts": time.time()},
    ])
    return t


async def test_done_task_is_not_rewoken_by_the_head_push_or_its_ci(store):
    """Once the task is DONE — the state the host sets right after
    `land_task` returns ok, in the same breath as the head+base pushes —
    nothing about the PR (a CI re-run on the freshly-pushed head, a new
    `mergeable` recompute, anything) reaches it. `_is_terminal` must stop
    `_evaluate` before any forge call, so even checkers rigged to blow up
    on any call are never actually invoked."""
    t = await _task_awaiting_approval(store)
    await store.set_status(
        t, TaskStatus.DONE, validate=False,
        event={"source": "watcher", "kind": "landed",
               "text": "landed via head+base push", "ts": time.time()},
    )
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE

    calls = {"state": 0, "mergeable": 0, "checks": 0, "comment": 0}

    async def _boom_state(url):
        calls["state"] += 1
        raise AssertionError("pr_state must not be polled for a DONE task")

    async def _boom_mergeable(url):
        calls["mergeable"] += 1
        raise AssertionError("pr_mergeable must not be polled for a DONE task")

    async def _boom_checks(url):
        calls["checks"] += 1
        raise AssertionError("pr_checks must not be polled for a DONE task")

    async def _boom_comment(pr_ref):
        calls["comment"] += 1
        raise AssertionError("pr_comment must not be polled for a DONE task")

    w = WakeWatcher(
        store, {}, pr_state=_boom_state, pr_mergeable=_boom_mergeable,
        pr_checks=_boom_checks, pr_comment=_boom_comment,
    )
    out = await w._evaluate(fresh, now=_now())
    assert out is None
    assert calls == {"state": 0, "mergeable": 0, "checks": 0, "comment": 0}


async def test_merged_state_completes_rather_than_escalating(store):
    """The NORMAL outcome of the fix: the forge reports MERGED (because the
    head-branch push made the landed sha reachable from base). The watcher
    must take the `merged` rung straight to DONE — not treat the new head
    sha as something needing a fresh review round, and not fall into the
    CLOSED/escalation branch."""
    t = await _task_awaiting_approval(store)

    async def _merged_state(url):
        return "MERGED"

    events: list[tuple[str, str]] = []
    w = WakeWatcher(store, {}, pr_state=_merged_state,
                     on_event=lambda k, txt: events.append((k, txt)))

    out = await w._check_open_pr(t)
    assert out == "merged"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert not fresh.blocker
    assert any(k == "merged" for k, _ in events)
    assert not any(k == "pr_closed" for k, _ in events)

    all_events = await store.list_events(t.id)
    assert any(e.get("kind") == "merged" for e in all_events)
    assert not any(e.get("kind") == "pr_closed" for e in all_events)


async def test_a_changed_pr_head_alone_triggers_no_review_round(store):
    """The transient shape right after the head-branch push: the PR is
    still OPEN (the forge hasn't settled on MERGED yet) and the sha the
    forge is now looking at is the freshly-pushed landed squash commit —
    but nothing ELSE about the PR changed: no new human comments, no merge
    conflict, CI green. No rung keys off the head sha by itself, so this
    must be a complete no-op: no resume, no revision round, no event
    beyond the routine PR-state observation `_check_open_pr` always does
    for a still-open PR."""
    t = await _task_awaiting_approval(store)

    async def _open_state(url):
        return "OPEN"

    async def _no_comments(pr_ref):
        return []

    async def _mergeable_clean(url):
        return {"mergeable": "MERGEABLE"}

    async def _green_checks(url):
        return [{"name": "build", "status": "pass"}]

    w = WakeWatcher(
        store, {}, pr_state=_open_state, pr_comment=_no_comments,
        pr_mergeable=_mergeable_clean, pr_checks=_green_checks,
    )

    resumed = {"count": 0}

    async def _fake_resume(task, now=None):
        resumed["count"] += 1
        return "resumed"

    w._resume = _fake_resume  # type: ignore[method-assign]

    out = await w._check_open_pr(t)
    assert out is None
    assert resumed["count"] == 0

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL

    events = await store.list_events(t.id)
    kinds = {e.get("kind") for e in events}
    assert "pr_closed" not in kinds
    assert "pr_ci_red" not in kinds
    assert "merged" not in kinds
