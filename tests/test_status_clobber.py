"""The 2026-08-09 demo incident (R15): a tracker poller's write-back held a
stale Task snapshot across slow network calls, then wrote the whole row back —
reverting a live task's status from awaiting_approval to reviewing. The row
then had a mid-run status with no worker attached, which nothing swept until
the next server restart: invisible for 66 minutes, and recovery re-ran the
whole task and opened a duplicate PR.

Three invariants close the class:
  1. update_task / update_task_columns never move the status column —
     set_status is the ONLY status writer.
  2. Poller write-back persists its markers via merge_context, so a stale
     snapshot cannot erase context keys written concurrently (pr_watch).
  3. The scheduler's orphan sweep also runs per-tick: a task in a mid-run
     status with no worker attached and a stale updated_at is requeued at
     runtime, not just at startup.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from no_human.core.db import Store
from no_human.core.scheduler import Scheduler, SiblingSchedulerRunning
from no_human.core.task import Task, TaskStatus
from no_human.intake.jira_poll import JiraPoller

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# 1. Store: only set_status moves status                                       #
# --------------------------------------------------------------------------- #


async def test_update_task_never_moves_status(store):
    """A stale handle's status must not overwrite a live row's status; the
    handle is refreshed to the row's truth instead."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    stale = await store.get_task(t.id)          # snapshot at PENDING

    await store.set_status(t, TaskStatus.REVIEWING, validate=False)

    stale.title = "renamed"                     # a legitimate column edit
    result = await store.update_task(stale)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.REVIEWING     # not clobbered to PENDING
    assert fresh.title == "renamed"                 # the edit still landed
    assert result.status is TaskStatus.REVIEWING    # handle refreshed
    assert stale.status is TaskStatus.REVIEWING


async def test_update_task_columns_never_moves_status(store):
    """update_task_columns had the same landmine with no guard at all."""
    t = Task.new("x", repo_path="/tmp/r")
    await store.create_task(t)
    stale = await store.get_task(t.id)

    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    stale.blocker = {"category": "AMBIGUITY", "question": "?"}
    await store.update_task_columns(stale)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert fresh.blocker["category"] == "AMBIGUITY"


# --------------------------------------------------------------------------- #
# 2. Poller write-back: stale snapshot cannot revert status or erase context   #
# --------------------------------------------------------------------------- #


def _jira_cfg():
    return {"integrations": {"jira": {
        "enabled": True, "site": "https://acme.atlassian.net",
        "project_key": "PROJ", "email": "me@x.com", "jql": "",
        "write_back": True,
    }}}


def _adapter():
    return SimpleNamespace(transition=lambda key, cat: True,
                           comment=lambda key, body: True)


async def _stale_snapshot_poller(store, advance_to: TaskStatus):
    """Build the incident's exact race deterministically: the poller's
    list_tasks snapshot predates a status advance + context merge that land
    before its write-back."""
    task = Task.new("T", source="jira", external_id="PROJ-1",
                    repo_path="/tmp/r")
    await store.create_task(task)
    await store.set_status(task, TaskStatus.REVIEWING, validate=False)
    stale = await store.get_task(task.id)       # the poller's snapshot

    # ...meanwhile the orchestrator advances the task and records its PR.
    live = await store.get_task(task.id)
    await store.set_status(live, advance_to, validate=False)
    await store.merge_context(task.id, {"pr_watch": "https://gh/pr/5"})

    poller = JiraPoller(_adapter(), store, config=_jira_cfg())
    return task, poller, stale


async def test_jira_sync_does_not_revert_a_live_status_advance(store, monkeypatch):
    task, poller, stale = await _stale_snapshot_poller(
        store, TaskStatus.AWAITING_APPROVAL)

    async def _stale_list(status=None):
        return [stale]

    monkeypatch.setattr(store, "list_tasks", _stale_list)
    await poller.sync_statuses()

    monkeypatch.undo()
    fresh = await store.get_task(task.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL, (
        "poller write-back reverted a live status advance (the 2026-08-09 "
        "demo incident: task stranded in reviewing with no worker)")


async def test_jira_sync_does_not_erase_concurrent_context_keys(store, monkeypatch):
    task, poller, stale = await _stale_snapshot_poller(
        store, TaskStatus.AWAITING_APPROVAL)

    async def _stale_list(status=None):
        return [stale]

    monkeypatch.setattr(store, "list_tasks", _stale_list)
    await poller.sync_statuses()

    monkeypatch.undo()
    fresh = await store.get_task(task.id)
    assert (fresh.context or {}).get("pr_watch") == "https://gh/pr/5", (
        "poller write-back erased a context key merged after its snapshot "
        "(pr_watch loss silently kills PR comment-watching)")
    assert "jira" in (fresh.context or {}), "write-back markers must still land"


# --------------------------------------------------------------------------- #
# 3. Scheduler: stranded mid-run rows are swept at runtime, not just startup   #
# --------------------------------------------------------------------------- #


class _NeverRunOrch:
    async def run_task(self, task):  # pragma: no cover - sweep tests never run it
        raise AssertionError("dispatch must not run in these tests")


async def _stranded_task(store, *, age_seconds: float) -> Task:
    t = Task.new("stranded", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=age_seconds)).isoformat()
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, t.id))
    await store.db.commit()
    return t


async def test_runtime_sweep_requeues_a_stranded_reviewing_task(store):
    t = await _stranded_task(store, age_seconds=3600)
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "orphan_recovered" for e in events)


async def test_runtime_sweep_skips_inflight_tasks(store):
    t = await _stranded_task(store, age_seconds=3600)
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    sched._inflight.add(t.id)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_runtime_sweep_skips_recently_updated_tasks(store):
    """A task inside the grace window is never requeued — protects any claim
    window where a worker set a mid-run status moments ago."""
    t = await _stranded_task(store, age_seconds=300)
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_startup_sweep_honors_the_grace_window(store):
    """At startup a FRESH mid-run row is not an orphan — inverted from the
    previous pinned-defect assertion here (which required this exact case to
    be requeued unconditionally).

    That old assumption — "at startup nothing is legitimately in flight, so
    any mid-run status is an orphan of a killed process" — is true for the
    first process ever to touch a database and false the moment a SECOND `nh
    start`/`nh serve` shares it: the second process's own `_inflight` is
    empty right after boot regardless of what a first, still-running process
    is doing with that row. That gap is how incident 6408aba0 lost a
    review-PASSED attempt (task 2cc879d5): a second process's startup sweep
    saw the first process's live REVIEWING row, had no liveness evidence to
    check under the old code, and requeued it out from under the worker still
    reviewing it. See `test_startup_sweep_leaves_a_row_with_fresh_events_alone`
    for the exact repro (a fresh EVENT, not just a fresh row stamp)."""
    t = await _stranded_task(store, age_seconds=0)
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans()

    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


# --------------------------------------------------------------------------- #
# 4. The cure for review finding B1: the sweep's liveness signal is durable    #
#    (persisted events), and `nh watch` never drives a second orchestrator    #
#    beside a running server.                                                  #
# --------------------------------------------------------------------------- #


async def test_sweep_trusts_recent_events_as_liveness(store):
    """A row whose updated_at is stale but whose EVENTS are fresh is a live
    out-of-process run (nh watch, another driver flushing to the same DB) —
    never requeued. _inflight is per-process; events are not."""
    import time as _time
    t = await _stranded_task(store, age_seconds=3600)
    await store.save_events(t.id, [{
        "source": "agent", "kind": "tool_use", "text": "", "ts": _time.time(),
    }])
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_sweep_requeues_when_events_are_stale_too(store):
    """Old events do not confer liveness — only recent activity does."""
    import time as _time
    t = await _stranded_task(store, age_seconds=3600)
    await store.save_events(t.id, [{
        "source": "agent", "kind": "tool_use", "text": "",
        "ts": _time.time() - 3600,
    }])
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.IMPLEMENTING


async def test_tick_invokes_the_runtime_sweep(store):
    """The sweep must actually run from tick() — not only when called
    directly by tests."""
    t = await _stranded_task(store, age_seconds=3600)

    class _Orch:
        async def run_task(self, task):
            await store.set_status(task, TaskStatus.AWAITING_APPROVAL,
                                   validate=False)
            return SimpleNamespace(status=TaskStatus.AWAITING_APPROVAL,
                                   task=task)

    sched = Scheduler(store, lambda task=None: _Orch(), max_workers=1)
    await sched.tick()

    fresh = await store.get_task(t.id)
    assert fresh.status is not TaskStatus.REVIEWING, (
        "tick() never swept the stranded row")
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "orphan_recovered" for e in events)


async def test_runtime_sweep_skips_a_plan_correction_wait(store):
    """A PLANNING row holding a human's plan correction is WAITING, not
    stranded — _claimable picks it up; the sweep must not."""
    from no_human.core import plan_gate
    t = Task.new("corrected", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.PLANNING, validate=False)
    t.context = await store.merge_context(t.id, {
        plan_gate.CONTEXT_KEY: {"state": plan_gate.STATE_CORRECTING},
    })
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00+00:00", t.id))
    await store.db.commit()
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.PLANNING


async def test_a_refused_status_write_leaves_no_checkpoint_behind(store):
    """The sweep must not leave a side-effect behind a write the CAS guard
    refused.

    `set_status` no-ops on a terminal row (SCRUM-73) and returns None, so a
    task a human marks DONE between the sweep's `list_tasks` read and its write
    keeps its status. The checkpoint stamp used to be written BEFORE that
    check, and `merge_context` is not rolled back — so a finished task quietly
    acquired an `orphan_recovery` checkpoint that nothing would ever clear, and
    a later retry would branch from it. Before the checkpoint existed a lost
    race here was free; it is not free any more, so the write goes second.
    """
    t = Task.new("finished under the sweep", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    attempt = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt, commit_sha="a" * 40)

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    stale = await store.get_task(t.id)          # the sweep's stale handle
    real_list = store.list_tasks

    async def _list_tasks(status):              # the human wins the race
        if status is TaskStatus.REVIEWING:
            await store.set_status(stale, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            return [stale]
        return await real_list(status)

    store.list_tasks = _list_tasks
    try:
        await sched._recover_orphans()
    finally:
        store.list_tasks = real_list

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE, "CAS guard held the status"
    assert not ((fresh.context or {}).get("resume_from") or {}).get("sha"), (
        "a DONE task acquired an orphan_recovery checkpoint from a status "
        f"write that never landed; got {fresh.context!r}")
    assert not [e for e in await store.list_events(t.id)
                if e.get("kind") == "orphan_recovered"], (
        "the sweep announced a requeue that did not happen")


async def test_one_tasks_checkpoint_failure_does_not_abort_the_whole_sweep(store):
    """Recovery is a sweep over every orphan, and it ran the checkpoint lookup
    unguarded inside the loop — so one raise (a DB read error, a corrupt
    context) took every REMAINING task down with it, on the one code path whose
    whole job is to rescue tasks after a crash."""
    # Aged past the grace window: this test is about checkpoint-failure
    # resilience during the sweep, not about liveness detection — a fresh
    # row here would just be correctly left alone under the fix and never
    # reach the checkpoint lookup this test is exercising.
    doomed = await _stranded_task(store, age_seconds=3600)
    other = await _stranded_task(store, age_seconds=3600)

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    real = sched._inherited_checkpoint

    async def _boom(t):
        if t.id == doomed.id:
            raise RuntimeError("attempt read blew up")
        return await real(t)

    sched._inherited_checkpoint = _boom
    await sched._recover_orphans()

    assert (await store.get_task(other.id)).status is TaskStatus.IMPLEMENTING, (
        "one task's checkpoint failure aborted recovery for every task after it")
    assert (await store.get_task(doomed.id)).status is TaskStatus.IMPLEMENTING, (
        "the task whose checkpoint lookup failed was not requeued at all — a "
        "checkpoint is an optimisation, requeueing is the rescue")


async def test_unparseable_updated_at_reads_young_never_requeued(store):
    """A corrupt timestamp must fail closed (no requeue), not fail open."""
    assert Scheduler._row_age_s("garbage") == 0.0
    assert Scheduler._row_age_s(None) == 0.0
    t = await _stranded_task(store, age_seconds=0)
    await store.db.execute(
        "UPDATE tasks SET updated_at = 'not-a-stamp' WHERE id = ?", (t.id,))
    await store.db.commit()
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans(startup=False)

    assert (await store.get_task(t.id)).status is TaskStatus.REVIEWING


async def test_follow_task_streams_events_and_returns_on_parked(store):
    """_follow_task echoes persisted events and exits when the task leaves
    the active statuses — the read-only half of `nh watch`."""
    from no_human.cli.commands import _follow_task
    t = Task.new("followed", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    await store.save_events(t.id, [
        {"source": "orchestrator", "kind": "pr_open", "text": "url", "ts": 1.0},
        {"source": "orchestrator", "kind": "state", "text": "done", "ts": 2.0},
    ])
    echoed = []
    result = await _follow_task(store, t.id[:8], echo=echoed.append)
    assert result is not None and result.status is TaskStatus.AWAITING_APPROVAL
    assert [e["kind"] for e in echoed] == ["pr_open", "state"]


async def test_watch_follows_instead_of_running_when_server_owns(monkeypatch, tmp_path):
    """With a live server owning the pool, `nh watch` must never build its
    own orchestrator (B1: two orchestrators on one checkout; the runtime
    sweep would requeue whichever copy goes silent)."""
    from unittest import mock
    from click.testing import CliRunner
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        db_path = tmp_path / "nh.db"

    followed = []

    async def _fake_follow(store, task_id, **kw):
        followed.append(task_id)
        t = Task.new("x", repo_path="/tmp/r")
        t.status = TaskStatus.AWAITING_APPROVAL
        return t

    ran_tui = []
    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=True: (_Cfg(), None)), \
         mock.patch.object(cmd_mod, "_server_owns_worker", lambda cfg: True), \
         mock.patch.object(cmd_mod, "_follow_task", _fake_follow), \
         mock.patch.dict("sys.modules"):
        import no_human.cli.tui as tui_mod
        monkeypatch.setattr(tui_mod, "run_watch",
                            lambda *a, **k: ran_tui.append(a))
        import asyncio as _aio
        result = await _aio.to_thread(
            CliRunner().invoke, cmd_mod.watch, ["deadbeef"])

    assert result.exit_code == 0, result.output
    assert followed == ["deadbeef"]
    assert ran_tui == []


async def test_follow_task_stops_when_the_row_is_deleted(store):
    """A deleted row must end the follow, not spin forever (review N2)."""
    from no_human.cli.commands import _follow_task
    t = Task.new("doomed", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)

    import asyncio

    async def _delete_soon():
        await asyncio.sleep(0.05)
        await store.db.execute("DELETE FROM tasks WHERE id = ?", (t.id,))
        await store.db.commit()

    deleter = asyncio.ensure_future(_delete_soon())
    result = await asyncio.wait_for(
        _follow_task(store, t.id, poll_s=0.02, echo=lambda e: None), timeout=5)
    await deleter
    assert result is None


async def test_tui_run_persists_its_events():
    """The `nh watch` TUI must persist events like its two sibling runners —
    the board reads task_events, and the stranded sweep reads them as
    LIVENESS: a silent TUI run is the double-orchestrator hole (review B2).
    Structural pin on the one production construction site."""
    import inspect
    import no_human.cli.tui as tui
    src = inspect.getsource(tui.WatchApp._run)
    assert "EventPersister" in src and "_persisting(" in src, (
        "WatchApp._run no longer persists its events — the stranded sweep "
        "will read a live TUI run as silence and requeue it")


# --------------------------------------------------------------------------- #
# 5. A second process's startup sweep must not clobber a live sibling's row    #
#    (incident 6408aba0: a second `nh start`/`nh serve` saw a first process's  #
#    REVIEWING row at boot, had no liveness evidence to check under the old    #
#    unconditional-at-startup rule, and requeued a review-PASSED attempt out   #
#    from under the worker still reviewing it).                                #
# --------------------------------------------------------------------------- #


async def test_startup_sweep_leaves_a_row_with_fresh_events_alone(store):
    """THE REPRO (AC1). A second process's startup sweep must judge liveness
    the same way the runtime sweep does — a fresh EVENT (not just a fresh row
    stamp) is evidence a live worker (in ANOTHER process) still owns this row,
    and must stop the sweep from requeuing it. Fails on the pre-fix code,
    which treated "found mid-run at startup" alone as proof of orphanhood."""
    import time as _time
    t = await _stranded_task(store, age_seconds=3600)
    await store.save_events(t.id, [{
        "source": "agent", "kind": "tool_use", "text": "",
        "ts": _time.time() - 30,
    }])
    # A fresh Scheduler instance with an EMPTY `_inflight` — this is what a
    # second process's own bookkeeping looks like right after boot,
    # regardless of what a first, still-running process is doing with the
    # same row in the same database.
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans()  # startup=True (the default)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.REVIEWING, (
        "the startup sweep clobbered a live sibling's row — this is incident "
        "6408aba0 (task 2cc879d5): a review-PASSED attempt lost to a second "
        "process's boot-time sweep")
    events = await store.list_events(t.id)
    assert not any(e.get("kind") == "orphan_recovered" for e in events)


async def test_startup_sweep_still_recovers_a_row_whose_activity_is_stale(store):
    """Control for the repro above: when the newest EVENT is also stale, the
    row really is an orphan and the startup sweep must still recover it —
    the fix narrows the sweep's blind spot, it does not disable it."""
    import time as _time
    t = await _stranded_task(store, age_seconds=3600)
    await store.save_events(t.id, [{
        "source": "agent", "kind": "tool_use", "text": "",
        "ts": _time.time() - 3600,
    }])
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans()

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "orphan_recovered" for e in events)


async def test_startup_sweep_recovers_a_row_with_no_events_and_a_stale_row_stamp(store):
    """A row with NO events at all (nothing ever persisted for it) and a
    stale `updated_at` is still recovered at startup — liveness evidence is
    OPTIONAL for a row to be swept, not required for it to be left alone."""
    t = await _stranded_task(store, age_seconds=3600)
    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    await sched._recover_orphans()

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "orphan_recovered" for e in events)


async def test_a_second_scheduler_refuses_while_a_sibling_heartbeat_is_live(store):
    """THE REPRO (AC2). A second scheduler's `_claim_pool_lease` must refuse
    to boot while a fresh sibling heartbeat names a different, live process —
    this is the primary defense: a process that never gets past this raise
    never reaches the orphan sweep at all."""
    import os
    import platform
    import time as _time
    from datetime import datetime, timezone

    sibling_pid = os.getppid()  # alive, and provably not ours
    now = _time.time()
    await store.write_scheduler_heartbeat(
        pid=sibling_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(), ts=now)

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    with pytest.raises(SiblingSchedulerRunning) as exc_info:
        await sched._claim_pool_lease()

    assert str(sibling_pid) in str(exc_info.value)


async def test_a_stale_sibling_heartbeat_is_taken_over(store):
    """A heartbeat older than `_HEARTBEAT_STALE_S` (300s) is presumed dead —
    a second scheduler must take the lease over rather than refuse forever
    because a sibling crashed without clearing its row."""
    import os
    import platform
    import time as _time
    from datetime import datetime, timezone

    other_pid = os.getppid()
    await store.write_scheduler_heartbeat(
        pid=other_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(),
        ts=_time.time() - 600)

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    await sched._claim_pool_lease()  # must not raise

    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == os.getpid()


async def test_a_dead_sibling_pid_on_this_host_is_taken_over(store, monkeypatch):
    """A FRESH heartbeat for a same-host pid that is provably dead (not just
    old) is also taken over immediately — no reason to wait out the staleness
    window when `pid_alive` already proves the sibling is gone."""
    import os
    import platform
    import time as _time
    from datetime import datetime, timezone

    import no_human.core.scheduler as scheduler_mod

    other_pid = os.getppid()
    await store.write_scheduler_heartbeat(
        pid=other_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(), ts=_time.time())
    monkeypatch.setattr(scheduler_mod, "pid_alive", lambda pid: False)

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    await sched._claim_pool_lease()  # must not raise — the sibling is dead

    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == os.getpid()


async def test_a_wrong_start_token_on_a_live_pid_is_taken_over_immediately(store):
    """THE REPRO for the pid-reuse false-sibling bug. A FRESH, same-host row
    naming a pid that IS alive (`pid_alive` says so) must still be taken over
    immediately when its `start_token` does not match that pid's CURRENT
    token — that mismatch is exactly what happens when the OS recycles a pid
    within `_HEARTBEAT_STALE_S` of the original holder dying: a brand-new,
    unrelated process now answers to that pid number, and only the token
    tells the two apart. Before the fix, `_claim_pool_lease` trusted
    `pid_alive` alone for this decision and raised `SiblingSchedulerRunning`
    here — this must FAIL on unfixed code."""
    import os
    import platform
    import time as _time
    from datetime import datetime, timezone

    from no_human.config import process_start_token

    other_pid = os.getppid()  # alive, and provably not ours
    await store.write_scheduler_heartbeat(
        pid=other_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(), ts=_time.time(),
        start_token=f"not-really-{process_start_token(other_pid)}")

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    await sched._claim_pool_lease()  # must not raise — the token proves a new process

    row = await store.read_scheduler_heartbeat()
    assert row["pid"] == os.getpid()


async def test_a_matching_start_token_on_a_live_pid_still_refuses(store):
    """The flip side of the repro above: a FRESH, same-host row whose
    `start_token` DOES match the named pid's current token is the genuine
    live sibling case — `SiblingSchedulerRunning` must still be raised exactly
    as before the fix, token or no token."""
    import os
    import platform
    import time as _time
    from datetime import datetime, timezone

    from no_human.config import process_start_token

    sibling_pid = os.getppid()  # alive, and provably not ours
    await store.write_scheduler_heartbeat(
        pid=sibling_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(), ts=_time.time(),
        start_token=process_start_token(sibling_pid))

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    with pytest.raises(SiblingSchedulerRunning) as exc_info:
        await sched._claim_pool_lease()

    assert str(sibling_pid) in str(exc_info.value)


async def test_a_token_less_legacy_row_still_refuses_exactly_as_today(store):
    """A row written before the `start_token` column existed (or by a caller
    that could not determine one) has `start_token IS NULL` — this must fall
    back to the exact pre-fix, `pid_alive`-only behaviour: a fresh same-host
    live pid still refuses. This is the explicit byte-identical-legacy-
    behaviour acceptance criterion, in addition to the pre-existing
    `test_a_second_scheduler_refuses_while_a_sibling_heartbeat_is_live` (which
    also writes a token-less row)."""
    import os
    import platform
    import time as _time
    from datetime import datetime, timezone

    sibling_pid = os.getppid()
    await store.write_scheduler_heartbeat(
        pid=sibling_pid, host=platform.node(),
        started_at=datetime.now(timezone.utc).isoformat(), ts=_time.time())
    row = await store.read_scheduler_heartbeat()
    assert row["start_token"] is None, "fixture row must be token-less to test the legacy path"

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)

    with pytest.raises(SiblingSchedulerRunning) as exc_info:
        await sched._claim_pool_lease()

    assert str(sibling_pid) in str(exc_info.value)


async def test_the_lease_is_claimed_before_any_recovery_write(store):
    """`_claim_pool_lease` runs BEFORE `_reconcile_terminal_task_attempts` and
    `_recover_orphans` in `run_forever` — a process that fails the lease
    claim must never reach either sweep. That ordering is the whole point of
    `SiblingSchedulerRunning`: a process that never gets past the raise never
    touches a row a live sibling still owns."""
    import asyncio

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    calls: list[str] = []

    async def _boom():
        raise SiblingSchedulerRunning(pid=999999, host="elsewhere", age_s=1.0)

    async def _track_reconcile():
        calls.append("reconcile")

    async def _track_recover(*, startup: bool = True):
        calls.append("recover")

    sched._claim_pool_lease = _boom
    sched._reconcile_terminal_task_attempts = _track_reconcile
    sched._recover_orphans = _track_recover

    with pytest.raises(SiblingSchedulerRunning):
        await sched.run_forever(stop=asyncio.Event())

    assert calls == [], (
        "a sweep ran after a failed lease claim — the whole point of "
        "claiming the lease FIRST is that neither sweep ever sees a row a "
        "live sibling still owns")


async def test_tick_refreshes_the_heartbeat(store):
    """A running pool's heartbeat must advance on every tick — otherwise a
    sibling waiting out `_HEARTBEAT_STALE_S` would take the lease over while
    this process is still alive and ticking."""
    import asyncio

    sched = Scheduler(store, lambda task=None: _NeverRunOrch(), max_workers=0)
    await sched._claim_pool_lease()
    first = await store.read_scheduler_heartbeat()

    await asyncio.sleep(0.01)
    await sched.tick()

    second = await store.read_scheduler_heartbeat()
    assert second["ts"] > first["ts"], (
        "tick() never refreshed the pool lease heartbeat")
