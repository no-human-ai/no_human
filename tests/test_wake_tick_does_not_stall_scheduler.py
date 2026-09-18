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

This used to be checked with `elapsed <= outer_bound` on `time.monotonic()`
around the whole tick — a wall clock, which reads the runner's load, not the
mechanism: a shared CI box under contention can take 4s to do 1s of nominal
work and fail a test that changed nothing (PR #497, three retries on an
unchanged commit). What actually proves the bound is a set of counts, not a
duration: `src/no_human/vcs/pr_watcher.py:135` is the ONLY bound in the
chain, `src/no_human/blockers/wake.py:466-473` calls it once per parked task
SEQUENTIALLY, and `src/no_human/core/scheduler.py:2161-2177` adds no
aggregate timeout around the tick — so the worst case is arithmetic (N x
`_CLI_TIMEOUT`), not something a clock has to confirm. This file now asserts
that arithmetic directly: exactly one bounded forge call per parked task,
EVERY one of them awaited under `asyncio.wait_for(..., _CLI_TIMEOUT)`
(nothing unbounded slipped into the chain), and the timeout actually fires
(the fake process is reaped) — three counts, identical on a quiet laptop and
a saturated 4-way CI runner.

Every hang-capable await is wrapped in an outer ``asyncio.wait_for(...,
_HANG_GUARD)`` so a regression here reads as a clean timeout/failure, never a
wedged test session. `_HANG_GUARD` is a hang guard, not a performance bound:
it only tells "returned" from "wedged forever" apart, sized at ~100x the
expected cost so ordinary runner load can never reach it. The one place a
real wall-clock number still appears is the `slow`-marked twin below, an
explicit backstop excluded from the `pull_request` CI lane (so it is not on
every contributor's path) but still executed, unfiltered, on every push to
main and via `workflow_dispatch` (`.github/workflows/ci.yml:360-380`).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus
from no_human.vcs import pr_watcher as pw

#: Hang guard only — see the module docstring. ~100x the expected cost of
#: the default-lane run (n=20 at per_call_timeout=0.01s => ~0.2s nominal),
#: and still comfortably above the slow twin's real-time backstop below
#: (`elapsed <= 40.0` for a nominal 20.0s run) so that backstop can actually
#: fire as a clean `AssertionError` instead of being preempted by this guard
#: raising `TimeoutError` first.
_HANG_GUARD = 45.0


class _Calls(list):
    """The `gh` invocations `hang_cli` records, plus which of the fake
    processes it spawned were actually reaped (`.kills`) — the fact that
    proves the `_CLI_TIMEOUT` bound FIRED, not merely that it was asked
    for."""

    def __init__(self):
        super().__init__()
        self.kills: list[int] = []


class _HungProcess:
    """Same shape as `test_pr_watcher_cli_timeout.py`'s fake: `communicate()`
    never resolves on its own, only via the `_CLI_TIMEOUT` bound inside
    `_run_cli` racing it with `asyncio.wait_for`."""

    def __init__(self, argv: list[str], calls: "_Calls"):
        self.argv = list(argv)
        self.pid = 424242
        self.returncode = None
        self._never = asyncio.Event()
        self._calls = calls

    async def communicate(self):
        await self._never.wait()
        return b"", b""  # pragma: no cover — unreachable

    def kill(self):
        self._calls.kills.append(self.pid)


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
    calls = _Calls()

    async def fake_create_subprocess_exec(*argv, **kwargs):
        calls.append(list(argv))
        return _HungProcess(argv, calls)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(pw.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


class _RecordingAsyncio:
    """Forwards everything to the real `asyncio`, recording EVERY call made
    through this module reference — not just `wait_for` — because a second,
    unrelated await slipped into `pr_watcher.py`'s chain (e.g. an injected
    `asyncio.sleep(...)` sitting above the bounded `wait_for` at
    `pr_watcher.py:135`) is just as real a regression as the per-call bound
    itself going missing, and recording `wait_for` alone cannot see it: that
    extra await would simply run alongside the recorded ones, invisible.
    `_run_cli` reaches both `create_subprocess_exec` and `wait_for` through
    the module attribute (`pr_watcher.py:120,135`), so replacing `pw.asyncio`
    with this instance sees every call the tick makes through it — and
    nothing the test's own `asyncio` use does, since that still resolves to
    the real module."""

    def __init__(self):
        self.waits: list[float] = []
        #: Name of every callable attribute accessed on this proxy, in
        #: order — the shape check below asserts this is EXACTLY the
        #: expected `create_subprocess_exec`/`wait_for` pair per task, so
        #: any other call (e.g. `sleep`) shows up as an unexpected extra
        #: entry instead of passing through unnoticed.
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(asyncio, name)
        if not callable(attr):
            return attr

        def recorder(*args, **kwargs):
            self.calls.append(name)
            if name == "wait_for":
                timeout = kwargs.get("timeout")
                if timeout is None and len(args) > 1:
                    timeout = args[1]
                self.waits.append(timeout)
            return attr(*args, **kwargs)

        return recorder


async def _run_the_bound_check(store, hang_cli, monkeypatch, *, n: int,
                                per_call_timeout: float):
    monkeypatch.setattr(pw, "_CLI_TIMEOUT", per_call_timeout)
    proxy = _RecordingAsyncio()
    monkeypatch.setattr(pw, "asyncio", proxy)
    for i in range(n):
        await _approval_task(store, i)

    wake = WakeWatcher(store, {}, pr_mergeable=pw.default_pr_mergeable)
    sched = Scheduler(store, _NoopOrch, max_workers=4, wake_watcher=wake)

    # `asyncio` here is the module's own top-level import, captured before
    # `pw.asyncio` was patched above — a hang guard, not a performance
    # bound (see `_HANG_GUARD`'s definition): it only tells "returned" from
    # "wedged forever" apart, so a regression reads as a clean
    # timeout/failure, never a wedged test session.
    started = await asyncio.wait_for(sched.tick(), _HANG_GUARD)

    assert started == []  # nothing PENDING to dispatch — only parked tasks
    assert len(hang_cli) == n, (
        f"expected exactly one `gh` call per parked task ({n}), got "
        f"{len(hang_cli)} — the sequential per-task loop shape changed")
    assert proxy.waits == [per_call_timeout] * n, (
        f"expected all {n} forge calls to be awaited under exactly one "
        f"bounded `asyncio.wait_for(..., {per_call_timeout})` each — got "
        f"{proxy.waits!r}; either a second, unbounded await slipped into "
        "the chain, or the per-call bound stopped being applied")
    expected_calls = ["create_subprocess_exec", "wait_for"] * n
    assert proxy.calls == expected_calls, (
        f"expected `pr_watcher.py` to make exactly `create_subprocess_exec` "
        f"then `wait_for`, once each per parked task, in that order ({n} "
        f"of each) — got {proxy.calls!r}. `waits` alone only sees calls "
        "named `wait_for`; this checks the full sequence of every call "
        "made through `pw.asyncio`, so a THIRD, unbounded call slipped into "
        "the chain (e.g. an injected `asyncio.sleep(...)` sitting above "
        "the bound at pr_watcher.py:135) shows up here as an extra entry "
        "even though it would never touch `waits`")
    assert len(hang_cli.kills) == n, (
        f"expected all {n} timed-out processes to be reaped, got "
        f"{len(hang_cli.kills)} — the `_CLI_TIMEOUT` bound was requested "
        "but did not actually fire")


@pytest.mark.slow
async def test_twenty_parked_tasks_each_hanging_do_not_stall_the_tick_beyond_n_times_t(
    store, hang_cli, monkeypatch,
):
    """AC3, at the literal scale the ticket names: N=20 parked tasks, each
    mocked to delay ~1s (via `_CLI_TIMEOUT`). Without the per-call bound,
    each `_run_cli` call hangs forever and this test would itself hang
    (never fail cleanly) — which is why every other test in this file runs
    at a scaled-down timeout for the default (non-`slow`) test run, while
    this one preserves the ticket's exact numbers under `-m slow`.

    The mechanism assertions live in `_run_the_bound_check` (see the module
    docstring). This test ALSO keeps the one wall-clock assertion left in
    the whole file, on purpose: `elapsed <= 40.0`, true ~2x the nominal
    20x1.0=20.0s cost (not the 1.1x `22.0` the previous draft of this
    docstring claimed — that was arithmetically wrong and, measured on an
    idle machine, left only a ~6% margin: below overshoot already observed
    on shared runners). This is NOT an opt-in-only check: excluded from the
    `pull_request` lane (`-m "not slow and not nightly"`, `ci.yml:360-380`)
    so it is not on every contributor's PR, but the SAME empty-selector
    `push to main` run executes every test unfiltered (`ci.yml:363`), so
    this still runs, under `-n 4`, on every push to main — plus explicitly
    via `workflow_dispatch` (`-m "slow or nightly"`). `_HANG_GUARD` (45.0,
    module-level) is kept comfortably above this 40.0 so the backstop can
    fire as a clean `AssertionError` rather than being preempted by the
    guard's `TimeoutError`. It exists to catch a UNIFORM slowdown the
    counting assertions above cannot see, not to grade per-call speed."""
    start = time.monotonic()
    await _run_the_bound_check(
        store, hang_cli, monkeypatch, n=20, per_call_timeout=1.0)
    elapsed = time.monotonic() - start
    assert elapsed <= 40.0, (
        f"Scheduler.tick() took {elapsed:.2f}s for 20 parked tasks at "
        "1.0s/call — expected <= 40.0s (true ~2x the nominal 20.0s cost); "
        "this is a real-time backstop that runs on every push to main "
        "(ci.yml's empty push selector), not the default pull_request gate")


async def test_the_same_bound_holds_at_a_scaled_down_timeout(store, hang_cli, monkeypatch):
    """Same shape as the `slow` test above (N=20, sequential, one hang per
    task) but at a millisecond-scale `_CLI_TIMEOUT` so the default (non-slow)
    test run still exercises the exact mechanism AC3 asks for, fast.

    What `_run_the_bound_check` asserts is three counts, never a duration:
    one forge call per parked task, every one of them bounded at exactly
    `_CLI_TIMEOUT` (`pr_watcher.py:135`), and every one of the timed-out
    processes reaped — arithmetic facts about the SEQUENTIAL per-task loop
    (`wake.py:466-473`) racing a fixed per-call bound, with no aggregate
    scheduler-level timeout (`scheduler.py:2161-2177`). All three are
    identical whether this runner is idle or saturated under `-n 4` —
    unlike the `elapsed <= outer_bound` wall-clock assertion this replaces,
    which read the runner's load, not the mechanism (PR #497: three CI
    retries on one unchanged commit)."""
    await _run_the_bound_check(
        store, hang_cli, monkeypatch, n=20, per_call_timeout=0.01)


async def test_a_single_hanging_task_no_longer_stalls_the_tick_indefinitely(
    store, hang_cli, monkeypatch,
):
    """Minimal reproduction of the live incident narrative itself (one dead
    `gh` call stalling the whole scheduler, `tick_stalled: true`,
    `seconds_since_last_tick: 647`): N=1 must resolve, bounded at exactly
    one `_CLI_TIMEOUT`-scoped wait, not hang."""
    await _run_the_bound_check(
        store, hang_cli, monkeypatch, n=1, per_call_timeout=0.05)
