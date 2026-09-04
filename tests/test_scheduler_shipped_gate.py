"""Resume/restart dispatch must consult the shipped check BEFORE starting an
attempt (live incident 2026-08-12): a checkpointed task whose PR had already
closed-landed still got a fresh `attempt 1/3` ~20 minutes after the PR
closed, because the restart resume/orphan machinery decided from task STATUS
alone — the shipped/ancestry check the `pr_closed` rung already had never ran
at the resume decision point.

These pin `Scheduler._shipped_before_dispatch`, which runs the SAME shared
`blockers.shipped.complete_if_content_landed` function the wake watcher's
CLOSED rung uses, called from `tick()`'s claimable loop before a task ever
reserves a slot."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus


class FakeOrch:
    """Records run_task calls; never resolves on its own (no test here needs
    the run to finish, only to observe whether it started)."""

    def __init__(self):
        self.started: list[str] = []

    async def run_task(self, task):
        self.started.append(task.id)
        return SimpleNamespace(status=TaskStatus.AWAITING_APPROVAL, task=task)


async def _checkpointed_task(store, tmp_path, *, calls=None):
    """A resumed/requeued task: IMPLEMENTING, with the context a checkpoint
    restart leaves behind — a recorded PR watch, base branch, and
    resume_from provenance."""
    t = Task.new("resumed task", repo_path=str(tmp_path))
    t.context = {
        "pr_branch": "nh/x-1",
        "base_branch": "main",
        "pr_watch": "https://github.com/o/r/pull/302",
        "resume_from": {"sha": "abc", "by": "orphan_recovery"},
    }
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    return t


def _make_wake(pr_shipped):
    async def _noop_tick(*, now=None, active_ids=None):
        return None

    return SimpleNamespace(pr_shipped=pr_shipped, tick=_noop_tick)


async def test_landed_checkpoint_completes_without_dispatch(store, tmp_path):
    calls = []

    async def probe(repo_path, branch, base):
        calls.append((repo_path, branch, base))
        return True

    task = await _checkpointed_task(store, tmp_path)
    fake = FakeOrch()
    sched = Scheduler(store, lambda task=None: fake, max_workers=2,
                       wake_watcher=_make_wake(probe))

    started = await sched.tick()

    assert started == []
    assert fake.started == []
    fresh = await store.get_task(task.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(task.id)
    assert any(e.get("kind") == "shipped" for e in events)
    assert calls == [(str(tmp_path), "nh/x-1", "main")]


async def test_unlanded_checkpoint_still_dispatches(store, tmp_path):
    async def probe(repo_path, branch, base):
        return False

    task = await _checkpointed_task(store, tmp_path)
    fake = FakeOrch()
    sched = Scheduler(store, lambda task=None: fake, max_workers=2,
                       wake_watcher=_make_wake(probe))

    started = await sched.tick()
    await asyncio.sleep(0)          # let the ensure_future'd _run start

    assert started == [task.id]
    assert fake.started == [task.id]
    fresh = await store.get_task(task.id)
    assert fresh.status is not TaskStatus.DONE


async def test_probe_exception_dispatches(store, tmp_path):
    async def probe(repo_path, branch, base):
        raise RuntimeError("git blew up")

    task = await _checkpointed_task(store, tmp_path)
    fake = FakeOrch()
    sched = Scheduler(store, lambda task=None: fake, max_workers=2,
                       wake_watcher=_make_wake(probe))

    started = await sched.tick()
    await asyncio.sleep(0)

    assert started == [task.id]
    assert fake.started == [task.id]
    fresh = await store.get_task(task.id)
    assert fresh.status is not TaskStatus.DONE


async def test_no_probe_wired_dispatches(store, tmp_path):
    task = await _checkpointed_task(store, tmp_path)
    fake = FakeOrch()
    sched = Scheduler(store, lambda task=None: fake, max_workers=2,
                       wake_watcher=None)

    started = await sched.tick()
    await asyncio.sleep(0)

    assert started == [task.id]
    assert fake.started == [task.id]


async def test_no_pr_url_dispatches(store, tmp_path):
    calls = []

    async def probe(repo_path, branch, base):
        calls.append((repo_path, branch, base))
        return True

    t = Task.new("resumed task, no PR", repo_path=str(tmp_path))
    t.context = {}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    fake = FakeOrch()
    sched = Scheduler(store, lambda task=None: fake, max_workers=2,
                       wake_watcher=_make_wake(probe))

    started = await sched.tick()
    await asyncio.sleep(0)

    assert started == [t.id]
    assert fake.started == [t.id]
    assert calls == []


async def test_terminal_during_probe_does_not_dispatch_or_rewrite(store, tmp_path):
    task = await _checkpointed_task(store, tmp_path)

    async def probe(repo_path, branch, base):
        # Simulate a concurrent human cancel landing DONE mid-probe.
        current = await store.get_task(task.id)
        await store.set_status(current, TaskStatus.DONE, validate=False,
                                event={"source": "watcher", "kind": "shipped",
                                       "text": "cancelled mid-probe"})
        return True

    fake = FakeOrch()
    sched = Scheduler(store, lambda task=None: fake, max_workers=2,
                       wake_watcher=_make_wake(probe))

    started = await sched.tick()

    assert started == []
    assert fake.started == []
    events = await store.list_events(task.id)
    # Exactly one DONE write (the probe's own), not a second one from the gate.
    shipped_events = [e for e in events if e.get("kind") == "shipped"]
    assert len(shipped_events) == 1
