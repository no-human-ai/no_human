"""2026-08-12 incident: the `pr_closed` rung defeats its own repair in a
loop. `ESCALATED` is not a terminal status (`WakeWatcher._is_terminal` only
recognises DONE / cancelled-FAILED), so once a task escalates via the
CLOSED-PR rung, `restore-approval` moving it back to `awaiting_approval`
lasted only until the next poll: the same still-CLOSED PR fell through the
same content check and re-escalated within one poll interval, overwriting
the human's own repair — three repair attempts in a row lost live.

These tests pin the two guards that close the loop:

  1. `WakeWatcher._pr_closed_answered` — a recorded repair/approval for the
     exact PR URL makes the rung hold rather than re-fire.
  2. The escalate-once guard inline in `_check_open_pr`'s CLOSED branch — an
     UNREPAIRED escalation must not grow a fresh event on every later tick
     either, since re-polling a still-CLOSED PR is the normal case, not an
     anomaly.

No real git repo is needed here: `pr_shipped` is faked to always report
"content not landed" (`None`), so every tick falls through to the rung
itself — the git-backed anchoring (`branch_landed_commit`) is covered in
`tests/test_pr_shipped.py`.
"""

from __future__ import annotations

import time


from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

PR_URL = "https://github.com/o/r/pull/293"


async def _never_shipped(repo_path, branch, base):
    """Fake `pr_shipped`: nothing ever landed — the rung must always fall
    through to the CLOSED branch's own escalate/repair logic."""
    return None


def _closed_watcher(store, *, pr_shipped=_never_shipped, on_event=None):
    async def pr_state(url):
        return "CLOSED"

    return WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=pr_shipped,
                       on_event=on_event)


async def _escalatable_task(store, *, url=PR_URL):
    """AWAITING_APPROVAL with a `pr_open` event on record — the exact
    precondition `restore-approval` itself requires, and the shape
    `_check_open_pr` produces before its first CLOSED-rung tick."""
    t = Task.new("shipped-check", repo_path="/tmp/does-not-matter")
    t.context = {"pr_watch": url, "pr_branch": "feature", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    await store.save_events(t.id, [
        {"source": "watcher", "kind": "pr_open", "text": f"opened {url}",
         "ts": time.time()},
    ])
    return t


async def _escalate_once(store, t, w):
    """Drive one real escalation through the rung, exactly as the watcher's
    first CLOSED tick would."""
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    return fresh


async def test_a_state_repaired_event_makes_the_pr_closed_rung_terminal(store):
    """Restore via the EVENT-LOG path (no context stamp): a `state_repaired`
    event recorded after the escalation is itself the answer. 5 ticks must
    all hold at awaiting_approval with zero further `pr_closed` events."""
    t = await _escalatable_task(store)
    w = _closed_watcher(store)
    escalated = await _escalate_once(store, t, w)

    # Simulate `restore-approval`'s ESCALATED -> AWAITING_APPROVAL repair,
    # via the store directly (the CLI verb's own DB calls), deliberately
    # WITHOUT stamping `pr_closed_repaired_url` — this test is for the
    # event-log fallback specifically.
    moved = await store.set_status(
        escalated, TaskStatus.AWAITING_APPROVAL, validate=False,
        human_override=True)
    assert moved is not None
    await store.save_events(escalated.id, [{
        "source": "human", "kind": "state_repaired",
        "text": f"escalated → awaiting_approval: spurious escalation "
                f"reversed; PR: {PR_URL}",
        "ts": time.time(),
    }])
    repaired = await store.get_task(escalated.id)
    assert repaired.status is TaskStatus.AWAITING_APPROVAL

    for i in range(5):
        out = await w._check_open_pr(repaired)
        assert out == "pr_closed_repair_honored", f"tick {i}: {out!r}"
        assert repaired.status is TaskStatus.AWAITING_APPROVAL, \
            f"tick {i}: rung re-escalated the repaired task"

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    events = await store.list_events(t.id)
    closed_events = [e for e in events if e.get("kind") == "pr_closed"]
    assert len(closed_events) == 1, \
        f"expected exactly the ORIGINAL escalation, got {len(closed_events)}"


async def test_a_human_restore_approval_event_makes_the_pr_closed_rung_terminal(store):
    """Same as the `state_repaired` event-log fallback above, but for the
    kind `restore-approval` now actually writes (`human_event`'s shared
    shape, in the same transaction as the status write). 5 ticks must all
    hold at awaiting_approval with zero further `pr_closed` events — proves
    `_PR_CLOSED_ANSWER_KINDS` was extended, not just renamed."""
    t = await _escalatable_task(store)
    w = _closed_watcher(store)
    escalated = await _escalate_once(store, t, w)

    moved = await store.set_status(
        escalated, TaskStatus.AWAITING_APPROVAL, validate=False,
        human_override=True)
    assert moved is not None
    await store.save_events(escalated.id, [{
        "source": "human", "kind": "human_restore_approval",
        "text": f"escalated → awaiting_approval: spurious escalation "
                f"reversed; PR: {PR_URL}",
        "prior_status": "escalated",
        "ts": time.time(),
    }])
    repaired = await store.get_task(escalated.id)
    assert repaired.status is TaskStatus.AWAITING_APPROVAL

    for i in range(5):
        out = await w._check_open_pr(repaired)
        assert out == "pr_closed_repair_honored", f"tick {i}: {out!r}"
        assert repaired.status is TaskStatus.AWAITING_APPROVAL, \
            f"tick {i}: rung re-escalated the repaired task"

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    events = await store.list_events(t.id)
    closed_events = [e for e in events if e.get("kind") == "pr_closed"]
    assert len(closed_events) == 1, \
        f"expected exactly the ORIGINAL escalation, got {len(closed_events)}"


async def test_the_repair_hold_is_emitted_once_not_per_tick(store):
    """The CONTEXT-STAMP path (what `restore-approval` actually writes):
    over 5 ticks, exactly one `pr_closed_repair_honored` event — not one per
    tick, which would just be a quieter version of the same spam."""
    t = await _escalatable_task(store)
    w = _closed_watcher(store)
    escalated = await _escalate_once(store, t, w)

    moved = await store.set_status(
        escalated, TaskStatus.AWAITING_APPROVAL, validate=False,
        human_override=True)
    assert moved is not None
    escalated.context = await store.merge_context(
        escalated.id, {"pr_closed_repaired_url": PR_URL})

    for _ in range(5):
        out = await w._check_open_pr(escalated)
        assert out == "pr_closed_repair_honored"

    events = await store.list_events(t.id)
    honored = [e for e in events if e.get("kind") == "pr_closed_repair_honored"]
    assert len(honored) == 1, \
        f"expected the hold to be emitted once, got {len(honored)}"


async def test_a_genuine_closed_unmerged_pr_escalates_exactly_once(store):
    """No repair anywhere: the FIRST tick escalates (a human must see this
    at least once), and every later tick — even after the task sits
    ESCALATED (not a terminal status) for the rest of the poll's lifetime —
    must be silent: no new `pr_closed` event, no repeated ESCALATED write."""
    t = await _escalatable_task(store)
    events = []
    w = _closed_watcher(
        store, on_event=lambda k, txt: events.append((k, txt)))

    outcomes = []
    for _ in range(6):
        outcomes.append(await w._check_open_pr(t))

    assert outcomes[0] == "escalated_pr_closed"
    assert all(o is None for o in outcomes[1:]), outcomes

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert fresh.context.get("pr_closed_escalated_url") == PR_URL

    all_events = await store.list_events(t.id)
    closed_events = [e for e in all_events if e.get("kind") == "pr_closed"]
    assert len(closed_events) == 1, \
        f"expected exactly one escalation, got {len(closed_events)}"
    assert sum(1 for k, _ in events if k == "pr_closed") == 1


async def test_a_repair_recorded_before_the_escalation_does_not_suppress_it(store):
    """Ordering, not blanket trust: a `state_repaired` event that PREDATES
    the `pr_closed` escalation it happens to share a task with is not an
    answer to that escalation — it answered something earlier (or nothing
    at all). The guard must still let the escalation through."""
    t = await _escalatable_task(store)
    now = time.time()
    # A stale repair, timestamped well BEFORE the escalation this tick is
    # about to record.
    await store.save_events(t.id, [{
        "source": "human", "kind": "state_repaired",
        "text": f"an earlier, unrelated repair; PR: {PR_URL}",
        "ts": now - 3600,
    }])
    w = _closed_watcher(store)

    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed", \
        "a repair that predates the escalation must not suppress it"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "pr_closed" for e in events)


def _seed_pr_closed_escalation_cli(db_path) -> str:
    """A task escalated via the CLOSED-PR rung — `pr_watch` in context and a
    `pr_open` event on record, `restore-approval`'s own precondition — same
    shape `_escalatable_task` builds, but through a plain-sync helper so
    `asyncio.run()` inside the CLI command's own click callback never
    collides with a live event loop (the pattern `test_false_done_completion
    .py::_seed_cli_task` establishes)."""
    async def _go():
        async with Store(db_path) as s:
            t = Task.new("shipped-check", repo_path="/tmp/does-not-matter")
            t.context = {"pr_watch": PR_URL, "pr_branch": "feature",
                        "base_branch": "main"}
            await s.create_task(t)
            await s.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            await s.save_events(t.id, [{
                "source": "watcher", "kind": "pr_open",
                "text": f"opened {PR_URL}", "ts": time.time(),
            }])
            data = {"category": "AMBIGUITY",
                    "question": "The PR was closed without merging."}
            t.blocker = data
            await s.update_task_columns(t)
            await s.set_status(t, TaskStatus.ESCALATED, validate=False)
            await s.save_events(t.id, [{
                "source": "watcher", "kind": "pr_closed",
                "text": f"PR closed unmerged: {PR_URL}", "ts": time.time(),
            }])
            return t.id
    import asyncio
    return asyncio.run(_go())


def test_restore_approval_stamps_the_repaired_url(tmp_path, monkeypatch):
    """The CLI path end to end: `nh task restore-approval` on a task
    escalated via the pr_closed rung must stamp
    `context['pr_closed_repaired_url']` with the PR URL — the fast path
    `_pr_closed_answered` checks first — and still record
    `human_restore_approval`.
    """
    import asyncio

    from click.testing import CliRunner

    from no_human.cli.commands import task_restore_approval

    db = tmp_path / "test.db"
    task_id = _seed_pr_closed_escalation_cli(db)

    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(
        cmd_mod, "_probe_pool",
        lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED))
    runner = CliRunner()

    result = runner.invoke(task_restore_approval, [task_id[:8]])
    assert result.exit_code == 0, result.output

    async def _check():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            events = await s.list_events(task_id)
            return t, events
    fresh, events = asyncio.run(_check())
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert fresh.context.get("pr_closed_repaired_url") == PR_URL
    assert any(e.get("kind") == "human_restore_approval" for e in events)
