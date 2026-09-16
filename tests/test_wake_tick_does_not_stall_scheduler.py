"""AC3: N parked tasks each hitting a hung `gh` call must not stall
`Scheduler.tick()` beyond N x (the per-call bound) + a small fixed overhead.

This is the second half of the fix proven in
``tests/test_pr_watcher_cli_timeout.py``: that file shows `_run_cli` itself
now returns (never hangs) once `_CLI_TIMEOUT` fires. This file shows that
bound is actually enough to keep `Scheduler.tick()` -> `WakeWatcher.tick()`'s
SEQUENTIAL per-parked-task loop (`src/no_human/blockers/wake.py`, `tick()`:
``for task in await self.store.list_tasks(status): action = await
self._evaluate(task, now=now)``) bounded overall, with NO additional
scheduler-level timeout/shield mechanism — none was added (see
`core/scheduler.py`'s comment at its `await self.wake.tick(...)` call site):
N tasks x 1 forge call each x `_CLI_TIMEOUT` is the whole story.

Every hang-capable await is wrapped in an outer ``asyncio.wait_for(..., N)``
so a regression here reads as a clean timeout/failure, never a wedged test
session.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus
from no_human.vcs import pr_watcher as pw


class _HungProcess:
    """Same shape as `test_pr_watcher_cli_timeout.py`'s fake: `communicate()`
    never resolves on its own, only via the `_CLI_TIMEOUT` bound inside
    `_run_cli` racing it with `asyncio.wait_for`."""

    def __init__(self, argv: list[str]):
        self.argv = list(argv)
        self.pid = 424242
        self.returncode = None
        self._never = asyncio.Event()

    async def communicate(self):
        await self._never.wait()
        return b"", b""  # pragma: no cover — unreachable

    def kill(self):
        pass


class _NoopOrch:
    """Never actually dispatched in this file — every task here is parked in
    AWAITING_APPROVAL, and the store has no PENDING task for `tick()` to
    claim — but `Scheduler.__init__` requires an orchestrator factory."""

    def __init__(self, task):
        self.task = task

    async def run_task(self, task):  # pragma: no cover — not exercised
        return None


async def _approval_task(store, n: int):
    """An AWAITING_APPROVAL task carrying an open-PR watch, same shape as
    `tests/test_wake_conflict.py:_approval_task` — the door
    `WakeWatcher._check_open_pr` (via `_evaluate`) polls through."""
    t = Task.new(f"parked {n}", repo_path="/tmp/x")
    t.context = {
        "pr_watch": f"https://code.example.com/dev/x/pull/{n}",
        "pr_branch": f"scratch/{n}",
        "base_branch": "main",
    }
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


@pytest.fixture
def hang_cli(monkeypatch):
    """Same recipe as `test_pr_watcher_cli_timeout.py`'s `hang_cli` fixture:
    patch the subprocess layer (never `_run_cli` itself) so every `gh` call
    any parked task's rung makes actually exercises `_CLI_TIMEOUT`. Callers
    set `pw._CLI_TIMEOUT` themselves (different files want different scales
    here: a fast sub-second run and a `slow`-marked realistic-scale run)."""
    calls: list[list[str]] = []

    async def fake_create_subprocess_exec(*argv, **kwargs):
        calls.append(list(argv))
        return _HungProcess(argv)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(pw.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


async def _run_the_bound_check(store, hang_cli, monkeypatch, *, n: int,
                                per_call_timeout: float, outer_bound: float):
    monkeypatch.setattr(pw, "_CLI_TIMEOUT", per_call_timeout)
    for i in range(n):
        await _approval_task(store, i)

    wake = WakeWatcher(store, {}, pr_mergeable=pw.default_pr_mergeable)
    sched = Scheduler(store, _NoopOrch, max_workers=4, wake_watcher=wake)

    start = time.monotonic()
    started = await asyncio.wait_for(sched.tick(), outer_bound + 10)
    elapsed = time.monotonic() - start

    assert started == []  # nothing PENDING to dispatch — only parked tasks
    assert len(hang_cli) == n, (
        f"expected exactly one `gh` call per parked task ({n}), got "
        f"{len(hang_cli)} — the sequential per-task loop shape changed")
    assert elapsed <= outer_bound, (
        f"Scheduler.tick() took {elapsed:.2f}s for {n} parked tasks at "
        f"{per_call_timeout}s/call — expected <= {outer_bound}s "
        f"({n} x {per_call_timeout} + overhead)")


@pytest.mark.slow
async def test_twenty_parked_tasks_each_hanging_do_not_stall_the_tick_beyond_n_times_t(
    store, hang_cli, monkeypatch,
):
    """AC3, at the literal scale the ticket names: N=20 parked tasks, each
    mocked to delay ~1s (via `_CLI_TIMEOUT`), must let `Scheduler.tick()`
    complete within 22s (20x1s + 2s overhead). Without the per-call bound,
    each `_run_cli` call hangs forever and this test would itself hang
    (never fail cleanly) — which is why every other test in this file runs
    at a scaled-down timeout for the default (non-`slow`) test run, while
    this one preserves the ticket's exact numbers under `-m slow`."""
    await _run_the_bound_check(
        store, hang_cli, monkeypatch, n=20, per_call_timeout=1.0, outer_bound=22.0)


async def test_the_same_bound_holds_at_a_scaled_down_timeout(store, hang_cli, monkeypatch):
    """Same shape as the `slow` test above (N=20, sequential, one hang per
    task) but at a millisecond-scale `_CLI_TIMEOUT` so the default (non-slow)
    test run still exercises the exact mechanism AC3 asks for, fast."""
    await _run_the_bound_check(
        store, hang_cli, monkeypatch, n=20, per_call_timeout=0.05, outer_bound=2.0)


async def test_a_single_hanging_task_no_longer_stalls_the_tick_indefinitely(
    store, hang_cli, monkeypatch,
):
    """Minimal reproduction of the live incident narrative itself (one dead
    `gh` call stalling the whole scheduler, `tick_stalled: true`,
    `seconds_since_last_tick: 647`): N=1 must resolve near-instantly once
    bounded, not hang."""
    await _run_the_bound_check(
        store, hang_cli, monkeypatch, n=1, per_call_timeout=0.05, outer_bound=1.0)
