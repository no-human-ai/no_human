"""Phase 7.3/7.4: the concurrent scheduler — pool cap, no double-dispatch,
shared-quota gate, wake integration. Uses a controllable fake orchestrator so the
scheduling logic is tested in isolation (the real run_task is covered elsewhere)."""

from __future__ import annotations

import ast
import asyncio
import pathlib
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from no_human.core.bounds import parse_quota_reset
from no_human.core.db import Store
from no_human.core import scheduler as scheduler_mod
from no_human.core import slot_wait
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus


async def _age_row(store: Store, task_id: str, seconds: float = 3600) -> None:
    """Back-date a row's `updated_at` so it clears `Scheduler._row_is_live`'s
    grace window before an orphan-recovery test calls `_recover_orphans`.

    These recovery tests build their row with `set_status(..., validate=False)`
    moments before sweeping it — a fresh `updated_at` that the STARTUP sweep
    used to ignore entirely (any mid-run status was an orphan at startup,
    unconditionally) but which the fix now correctly reads as "something may
    still be live" and leaves alone. None of these tests are ABOUT that grace
    window — they exercise checkpoint/provenance behavior on an orphan that
    really is one — so they age the row first rather than change what they
    assert.

    Also back-dates any `task_events` rows already on this task: a PRIOR
    `_recover_orphans()` call in the same test writes its own `orphan_
    recovered` event with a real (not backdated) timestamp, and that event is
    itself fresh activity `_row_is_live` correctly reads as liveness — a
    second, later-in-the-test call to `_recover_orphans` on the "same task,
    now further along" would otherwise see that event and (correctly, for a
    row that really were live) leave it alone, which is not what a test
    simulating a SECOND restart wants.
    """
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=seconds)).isoformat()
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task_id))
    await store.db.execute(
        "UPDATE task_events SET ts = ? WHERE task_id = ?",
        (time.time() - seconds, task_id))
    await store.db.commit()


class FakeOrch:
    """Records run_task calls; optionally blocks on a gate; sets a terminal DB
    status so finished tasks aren't re-claimed."""

    def __init__(self, store, *, hold=None, terminal=TaskStatus.AWAITING_APPROVAL,
                 quota_first=False, quota_resets=None):
        self.store = store
        self.hold = hold
        self.terminal = terminal
        self.quota_first = quota_first
        self.quota_resets = quota_resets
        self.started: list[str] = []
        self.max_concurrent = 0
        self._active = 0

    async def run_task(self, task):
        self.started.append(task.id)
        self._active += 1
        self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if self.hold is not None:
                await self.hold.wait()
            if self.quota_first and len(self.started) == 1:
                task.wake_check_at = self.quota_resets
                await self.store.set_status(task, TaskStatus.PAUSED_QUOTA,
                                            validate=False)
                return SimpleNamespace(status=TaskStatus.PAUSED_QUOTA, task=task)
            await self.store.set_status(task, self.terminal, validate=False)
            return SimpleNamespace(status=self.terminal, task=task)
        finally:
            self._active -= 1


async def _mk_tasks(store, n):
    ids = []
    for i in range(n):
        t = Task.new(f"task {i}", repo_path="/tmp/x")
        await store.create_task(t)
        ids.append(t.id)
    return ids


async def _wait_until(cond, *, timeout: float = 5.0, poll: float = 0.0):
    """Poll ``cond()`` until it is truthy, instead of guessing how long some
    scheduler-internal background step "usually" takes (the guesses are what
    flaked this file twice — 2026-08-12 and 2026-08-20).

    ``cond`` may be sync (return a bool) or async (return a coroutine that
    resolves to a bool). It is re-evaluated on every event-loop tick
    (``poll=0`` is a bare ``asyncio.sleep(0)`` yield, not a real delay), so a
    passing run resolves the instant the real condition holds, independent of
    machine speed or load. ``timeout`` is a safety net for a genuine hang,
    never the synchronisation mechanism itself — it raises, so a stall shows
    up as a clear failure instead of a flake.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = cond()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(poll)


async def _event_count_at_least(store, task_id: str, n: int) -> bool:
    return len(await store.list_events(task_id)) >= n


async def _has_event_kind(store, task_id: str, kind: str) -> bool:
    return any(e["kind"] == kind for e in await store.list_events(task_id))


def test_no_sleep_as_synchronisation_wait():
    """Acceptance gate: every ``asyncio.sleep(...)`` call site in *this* file
    is either gone (replaced by ``_wait_until`` on a real condition) or
    explicitly allow-listed below by the name of its outermost enclosing
    top-level function, with the reason recorded — not a silent exemption.
    A blind sleep guessing how long scheduler-internal work "usually" takes
    is exactly what flaked this file twice (2026-08-12, 2026-08-20)."""
    ALLOWED: dict[str, str] = {
        "_wait_until": (
            "the shared poll primitive itself: sleeps 0s between re-checks "
            "of a caller-supplied real condition (a bare event-loop tick), "
            "never a guessed completion duration — the thing every other "
            "site in this file used to do and no longer does."
        ),
    }

    source = pathlib.Path(__file__).read_text()
    tree = ast.parse(source, filename=__file__)

    violations: list[str] = []
    stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def _enter(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_FunctionDef(self, node):
            self._enter(node)

        def visit_AsyncFunctionDef(self, node):
            self._enter(node)

        def visit_Call(self, node):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "sleep"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "asyncio"):
                owner = stack[0] if stack else "<module>"
                if owner not in ALLOWED:
                    violations.append(f"{owner} (line {node.lineno})")
            self.generic_visit(node)

    Visitor().visit(tree)
    assert violations == [], (
        "asyncio.sleep used as a sleep-as-synchronisation wait outside the "
        f"allow-list: {violations}. Replace it with a deterministic "
        "_wait_until(...) condition, or add it to ALLOWED above with a "
        "reason if the wait is genuinely time-based."
    )


async def test_pool_cap_and_no_double_dispatch(store):
    hold = asyncio.Event()
    fake = FakeOrch(store, hold=hold)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2)
    await _mk_tasks(store, 3)

    started1 = await sched.tick()
    assert len(started1) == 2                 # capped at max_workers
    assert len(sched.inflight) == 2

    started2 = await sched.tick()
    assert started2 == []                      # pool full → nothing new
    assert len(sched.inflight) == 2            # no double-dispatch

    hold.set()
    await sched.wait_idle()   # let the 2 finish
    assert len(sched.inflight) == 0

    started3 = await sched.tick()
    assert len(started3) == 1                   # the third task now runs
    await sched.wait_idle()

    assert fake.max_concurrent == 2             # never exceeded the cap


async def test_wait_idle_timeout_leaves_the_live_runs_untouched(store):
    """A waiter that gives up must not cancel what it was watching: the
    first cut used wait_for(gather(...)), and a timed-out wait_for cancels
    the gather, which cancels its children — the scheduler's live runs."""
    hold = asyncio.Event()                      # never released: run stays live
    fake = FakeOrch(store, hold=hold)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)
    await _mk_tasks(store, 1)
    await sched.tick()
    (run,) = [t for t in sched._run_tasks.values()]

    with pytest.raises(TimeoutError):
        await sched.wait_idle(timeout=0.01)

    assert not run.done() and not run.cancelled()
    assert len(sched.inflight) == 1             # still running, still tracked
    hold.set()
    await sched.wait_idle()
    assert run.done() and not run.cancelled()


async def test_run_is_untracked_even_when_the_final_flush_raises(
        store, monkeypatch):
    """`_run_tasks.pop` sits after `persister.aclose()` in the finally; a
    raising flush must not leave the finished run tracked for the
    scheduler's lifetime."""
    from no_human.core import events as events_mod

    async def _boom(self):
        raise RuntimeError("flush failed")

    monkeypatch.setattr(events_mod.EventPersister, "aclose", _boom)
    fake = FakeOrch(store)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)
    await _mk_tasks(store, 1)
    await sched.tick()
    await sched.wait_idle()

    assert sched._run_tasks == {}
    assert len(sched.inflight) == 0


async def test_inflight_task_not_reclaimed(store):
    fake = FakeOrch(store, hold=asyncio.Event())  # never releases
    sched = Scheduler(store, lambda task=None: fake, max_workers=4)
    ids = await _mk_tasks(store, 2)

    await sched.tick()
    assert len(sched.inflight) == 2
    # A second tick must not re-dispatch the same still-running tasks.
    again = await sched.tick()
    assert again == []
    assert sched.inflight == set(ids)


async def test_quota_pause_gates_the_whole_pool(store):
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    resets = (now + timedelta(hours=1)).isoformat()
    fake = FakeOrch(store, quota_first=True, quota_resets=resets)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2)
    await _mk_tasks(store, 1)

    await sched.tick(now=now)
    await sched.wait_idle()

    assert sched._quota_cooldown_until is not None
    # public property (consumed by /api/queue/health) mirrors the internal
    # cooldown clock exactly — no second clock, no drift between the two.
    assert sched.quota_cooldown_until == sched._quota_cooldown_until

    # A new task arrives, but the pool is paused until the reset time.
    await _mk_tasks(store, 1)
    during = await sched.tick(now=now + timedelta(minutes=10))
    assert during == []                           # gated pool-wide

    after = await sched.tick(now=now + timedelta(hours=2))
    assert len(after) == 1                         # resumes once quota is back


async def test_quota_pause_honors_a_reset_the_message_states_past_the_fixed_hour(store):
    """RED before the parser: a park's `wake_check_at` used to be the fixed
    `RETRY_AFTER_S` hour (+60min) no matter what the CLI's own banner said.
    Here the wall states a reset 76 minutes out — past the old fixed-hour
    mark — so a wake fired at +60min would land 16 minutes before the wall
    actually lifts and re-park (the incident this task fixes). The pool must
    stay gated at +60min and only resume once the STATED reset has passed."""
    now = datetime(2026, 8, 22, 1, 4, 0, tzinfo=timezone.utc)  # 04:04 Jerusalem
    resets = parse_quota_reset(
        "You've hit your session limit · resets 5:20am (Asia/Jerusalem)",
        now=now)
    assert resets is not None
    assert resets - now == timedelta(minutes=76), resets - now

    fake = FakeOrch(store, quota_first=True, quota_resets=resets.isoformat())
    sched = Scheduler(store, lambda task=None: fake, max_workers=2)
    await _mk_tasks(store, 1)

    await sched.tick(now=now)
    await sched.wait_idle()
    assert sched._quota_cooldown_until == resets

    # A fixed-hour wake (+60min) must NOT re-dispatch — the wall is still up.
    await _mk_tasks(store, 1)
    at_sixty = await sched.tick(now=now + timedelta(minutes=60))
    assert at_sixty == [], "re-dispatched into a wall that hadn't lifted yet"

    # Past the STATED reset, the pool resumes.
    after = await sched.tick(now=now + timedelta(minutes=77))
    assert len(after) == 1


async def test_infra_breaker_cooldown_is_exposed_as_infra_not_quota(store):
    """Independent review of PR #553 (2026-08-21): the infra breaker arms the
    SAME `_quota_cooldown_until` clock a quota park uses, so the public
    `quota_cooldown_until` property labelled every cooldown "quota" even
    when it was really an SDK/auth breaker trip. `infra_cooldown_until` must
    expose the breaker case, and `quota_cooldown_until` must go quiet while
    it is the active cause — one clock, one kind flag, exactly one property
    non-None at a time."""
    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=2)
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)
    sched._quota_cooldown_until = now + timedelta(hours=1)
    sched._infra_cooldown_active = True

    assert sched.infra_cooldown_until == sched._quota_cooldown_until
    assert sched.quota_cooldown_until is None
    assert sched.health_snapshot()["infra_cooldown"] is True

    sched._infra_cooldown_active = False
    assert sched.infra_cooldown_until is None
    assert sched.quota_cooldown_until == sched._quota_cooldown_until
    assert sched.health_snapshot()["infra_cooldown"] is False


@pytest.fixture
def _infra_breaker():
    """The breaker is a process-wide singleton (`core/infra_breaker.py`) —
    reset it around the test so it cannot leak into (or be polluted by)
    any other test in the same worker process."""
    from no_human.core.infra_breaker import infra_breaker as _get

    _get().reset()
    yield _get()
    _get().reset()


async def test_infra_breaker_pauses_the_whole_pool_and_lapsing_resumes_it(
        store, _infra_breaker):
    """INCIDENT 2026-08-13: 3 consecutive zero-token/auth SDK failures across
    distinct tasks means the account or transport itself is down — the WHOLE
    pool must stop dispatching, via the same cooldown gate a single-task
    quota park already uses, and resume once it lapses (the existing quota
    watcher's job; this test proves the scheduler side only)."""
    _infra_breaker.record_infra_failure("task-a")
    _infra_breaker.record_infra_failure("task-b")
    _infra_breaker.record_infra_failure("task-c")
    assert _infra_breaker.tripped() is not None

    events = []
    fake = FakeOrch(store)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2,
                      on_event=lambda kind, text: events.append((kind, text)))
    await _mk_tasks(store, 1)
    now = datetime(2026, 8, 13, 17, 14, tzinfo=timezone.utc)

    during = await sched.tick(now=now)
    assert during == [], "the fleet breaker must gate dispatch pool-wide"
    assert sched._quota_cooldown_until is not None
    assert any(kind == "quota_pause" for kind, _ in events)

    # Lapse the cooldown (the existing quota-watcher's job in production) and
    # tick again: dispatch resumes and the breaker's streak is cleared, or a
    # single stale incident would pause the fleet forever.
    sched._quota_cooldown_until = now - timedelta(seconds=1)
    after = await sched.tick(now=now + timedelta(seconds=5))
    assert len(after) == 1
    assert _infra_breaker.tripped() is None


def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_work_repo(tmp_path, name):
    import subprocess
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / name
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


@pytest.mark.slow
async def test_two_repos_run_concurrently_in_worktrees(store, tmp_path):
    """Phase 7 DoD: two tasks in DIFFERENT repos run through the pool, each in its
    own worktree, both open a PR — with no git corruption."""
    from no_human.agent.claude_backend import AgentResult
    from no_human.config import load_config
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier
    from no_human.review.reviewer import ReviewDecision
    from no_human.review.selfcheck import ChecklistItem
    from no_human.vcs import GitRepo

    repo_a = _make_work_repo(tmp_path, "metrics-core")
    repo_b = _make_work_repo(tmp_path, "analytics-export")

    def mutate(cwd):
        from pathlib import Path
        (Path(cwd) / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (Path(cwd) / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    class Backend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            mutate(cwd)
            return AgentResult(final_text="done", num_turns=1, is_error=False,
                               tokens_used=10, session_id="s", stop_reason="end_turn")

    class Reviewer:
        async def review(self, task, *, repo_path, **kw):
            return ReviewDecision(passed=True,
                                  checklist=[ChecklistItem("ok", True, "calc.py:3")])

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["concurrency"] = {"enabled": True, "max_workers": 2,
                               "worktree_root": str(tmp_path / "wt")}

    def factory(task=None):
        return Orchestrator(store, cfg.data, Backend(), SlackNotifier(None),
                            reviewer=Reviewer())

    ta = Task.new("metrics-core story", repo_path=str(repo_a))
    tb = Task.new("analytics-export story", repo_path=str(repo_b))
    await store.create_task(ta)
    await store.create_task(tb)

    sched = Scheduler(store, factory, max_workers=2)
    started = await sched.tick()
    assert len(started) == 2
    await sched.drain()

    a2 = await store.get_task(ta.id)
    b2 = await store.get_task(tb.id)
    assert a2.status == TaskStatus.AWAITING_APPROVAL
    assert b2.status == TaskStatus.AWAITING_APPROVAL
    # Worktrees cleaned up in both repos; primary checkouts untouched.
    assert all("/wt/" not in w for w in GitRepo(repo_a).list_worktrees())
    assert all("/wt/" not in w for w in GitRepo(repo_b).list_worktrees())
    assert "mul" not in (repo_a / "calc.py").read_text()


async def test_wake_watcher_ticked_and_implementing_is_claimable(store):
    class FakeWake:
        def __init__(self):
            self.ticked = False

        async def tick(self, *, now=None, active_ids=None):
            self.ticked = True

    wake = FakeWake()
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=2, wake_watcher=wake)

    # A task already in IMPLEMENTING (e.g. just resumed) is claimable.
    t = Task.new("resumed", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    started = await sched.tick()
    assert wake.ticked
    assert t.id in started


# --------------------------------------------------------------------------- #
# PR-E: ReanalysisJob                                                          #
# --------------------------------------------------------------------------- #

from no_human.core.scheduler import ReanalysisJob


@pytest.mark.asyncio
async def test_reanalysis_due_after_interval(store):
    """ReanalysisJob is due immediately (last_run=0), then not due after running."""
    job = ReanalysisJob(store, interval_seconds=60)
    assert job.due()
    # Simulate a run completing.
    job._last_run = __import__("time").time()
    assert not job.due()


@pytest.mark.asyncio
async def test_reanalysis_maybe_run_skips_when_not_due(store):
    """maybe_run returns None when not due."""
    import time as _time
    job = ReanalysisJob(store, interval_seconds=9999)
    job._last_run = _time.time()  # just ran
    result = await job.maybe_run()
    assert result is None


def _fixed_transcripts(monkeypatch):
    """Feed the job one transcript instead of scanning the developer's machine.

    ``ReanalysisJob._run`` → ``TranscriptIngester.ingest`` → ``extract_transcripts``,
    which hunts for a live IDE process and raises ``IDENotRunningError`` when it
    finds none. That made both tests below pass or fail on whether an editor
    happened to be open, which is neither a property of the scheduler nor
    reproducible for anyone else running the suite.

    Only the machine scan is replaced. Everything the tests actually assert on —
    analysis, enqueueing, the transcript cache, the dedupe key — runs for real.
    The patch targets the name bound in ``ingester``'s namespace, which is what
    ``ingest`` resolves at call time.
    """
    from no_human.history.extractor import Message, Transcript

    transcript = Transcript(
        cascade_id="cascade-reanalysis", title="A session", created="2026-07-01",
        messages=[
            Message("assistant", "I'll take a look.", "STEP"),
            Message("user", "never commit secrets to the repo", "STEP"),
        ],
    )
    monkeypatch.setattr("no_human.history.ingester.extract_transcripts",
                        lambda **kw: [transcript])
    return transcript


@pytest.mark.asyncio
async def test_reanalysis_maybe_run_produces_result(store, monkeypatch):
    """maybe_run returns a populated result when due."""
    _fixed_transcripts(monkeypatch)
    job = ReanalysisJob(store, interval_seconds=60, days=1)
    # Due because _last_run is 0.
    result = await job.maybe_run()
    assert result is not None
    assert result["transcripts"] == 1
    # The transcript carries a user correction the heuristic analyzer flags, so
    # this asserts the pipeline actually produced something rather than merely
    # returning a dict with the right keys.
    assert result["proposed"] > 0
    assert "duplicates" in result


@pytest.mark.asyncio
async def test_reanalysis_dedup_across_runs(store, monkeypatch):
    """Re-running over the same transcript proposes nothing new — both layers.

    Dedup is two independent mechanisms, and the old assertion (``duplicates >=
    0``, true of any count) could not detect either one breaking. Layer 1 is the
    transcript cache: an unchanged transcript is never re-analyzed. Layer 2 is
    the content-based dedupe key: even when analysis IS redone, a finding
    already in the queue is counted as a duplicate rather than enqueued twice.
    """
    _fixed_transcripts(monkeypatch)
    job = ReanalysisJob(store, interval_seconds=0, days=1)

    r1 = await job.maybe_run()
    assert r1 is not None and r1["proposed"] > 0

    # Layer 1 — same transcript, still cached: nothing is re-analyzed. Asserted
    # on `findings`, not `proposed`: layer 2 would hold `proposed` at 0 even
    # with the cache broken, so `proposed` alone cannot tell the layers apart
    # and a cache regression would slip through green.
    job._running = False  # reset guard
    job._last_run = 0     # force re-run
    r2 = await job.maybe_run()
    assert r2 is not None
    assert r2["findings"] == 0, "cached transcript was re-analyzed"
    assert r2["proposed"] == 0, "cached transcript was re-proposed"

    # Layer 2 — drop the cache so the same findings are produced again. They are
    # already queued, so they must land as duplicates, not as new proposals.
    await store.history_cache_clear()
    job._running = False
    job._last_run = 0
    r3 = await job.maybe_run()
    assert r3 is not None
    assert r3["proposed"] == 0, "re-analyzed findings were proposed a second time"
    assert r3["duplicates"] > 0, "dedupe key did not recognize the repeat findings"


@pytest.mark.asyncio
async def test_reanalysis_survives_no_ide_running(store, monkeypatch, caplog):
    """No IDE running is the ORDINARY case on a clean install, not a failure.

    ``ReanalysisJob`` is due immediately on every fresh boot (``_last_run=0``),
    and ``TranscriptIngester.ingest`` was the one caller of
    ``extract_transcripts`` that did not catch ``IDENotRunningError`` the way
    ``nh history``/``nh bench build``/the onboarding scan already do — so it
    propagated out of ``maybe_run`` and, via ``Scheduler.tick``, became a
    "re-analysis failed: No Windsurf language server found. Is the IDE
    running?" WARNING on every single startup. This must degrade silently to
    zero Windsurf transcripts instead.
    """
    import logging

    from no_human.history.extractor import IDENotRunningError

    def _raise(**kw):
        raise IDENotRunningError("No Windsurf language server found. Is the IDE running?")

    monkeypatch.setattr("no_human.history.ingester.extract_transcripts", _raise)
    job = ReanalysisJob(store, interval_seconds=60, days=1)

    with caplog.at_level(logging.WARNING):
        result = job.due()
        assert result
        out = await job.maybe_run()

    assert out is not None, "a missing IDE must not raise out of maybe_run"
    assert out["transcripts"] == 0
    assert not any(
        "re-analysis failed" in r.message for r in caplog.records
    ), f"missing-IDE degraded to a boot warning: {[r.message for r in caplog.records]}"
    assert not any(
        "No Windsurf language server found" in r.message for r in caplog.records
        if r.levelno >= logging.WARNING
    )


@pytest.mark.asyncio
async def test_scheduler_tick_triggers_reanalysis(store):
    """Scheduler.tick() triggers the re-analysis job when it's due."""
    events = []
    job = ReanalysisJob(store, interval_seconds=0, days=1)
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(
        store, lambda task=None: fake, max_workers=1,
        on_event=lambda k, t: events.append((k, t)),
        reanalysis_job=job,
    )
    await sched.tick()
    # Job ran — even if no proposals, no error should have occurred.


# --------------------------------------------------------------------------- #
# Memory lifecycle C: RetirementSweepJob                                      #
# --------------------------------------------------------------------------- #

from no_human.core.scheduler import RetirementSweepJob


@pytest.mark.asyncio
async def test_retirement_sweep_due_immediately_then_not(store):
    """due() is False immediately after maybe_run() — the same shape as
    ReanalysisJob's due-after-interval test."""
    job = RetirementSweepJob(store, interval_seconds=60)
    assert job.due()  # _last_run == 0.0 at construction
    result = await job.maybe_run()
    assert result is not None
    assert not job.due()


@pytest.mark.asyncio
async def test_retirement_sweep_maybe_run_skips_when_not_due(store):
    job = RetirementSweepJob(store, interval_seconds=9999)
    job._last_run = time.time()  # just ran
    assert await job.maybe_run() is None


@pytest.mark.asyncio
async def test_retirement_sweep_archives_old_unconfirmed_proposals(store):
    from no_human.learning import TYPE_SKILL

    old_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="old", content="x", confirmed=False)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).strftime(
        "%Y-%m-%d %H:%M:%S")
    await store.db.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?", (cutoff, old_id))
    await store.db.commit()

    job = RetirementSweepJob(store, interval_seconds=60, archive_after_days=45)
    result = await job.maybe_run()
    # D3 (2026-08-31 operator directive): `auto_manage` defaults True, so
    # every tick also runs the 90-day auto-activated retirement sweep — a
    # no-op here (nothing in this test is auto-activated), but its (zero)
    # counter still rides the result dict.
    assert result == {"archived": 1, "auto_retired": 0}

    row = await store._fetchone(
        "SELECT archived FROM memories WHERE id = ?", (old_id,))
    assert row["archived"] == 1


@pytest.mark.asyncio
async def test_retirement_sweep_last_run_advances_even_if_run_raises(store, monkeypatch):
    """A raising `_run` must still advance `_last_run`, or a persistently
    failing sweep would retry every single tick instead of backing off."""
    job = RetirementSweepJob(store, interval_seconds=60)

    async def _boom():
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(job, "_run", _boom)
    assert job.due()
    with pytest.raises(RuntimeError):
        await job.maybe_run()
    assert not job.due(), "_last_run must advance even though _run raised"
    assert not job._running


@pytest.mark.asyncio
async def test_scheduler_disabled_retirement_job_never_writes(store):
    """`enabled=False` (the job simply never being constructed/passed) means
    the scheduler's tick never touches the memories table."""
    from no_human.learning import TYPE_SKILL

    old_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="old", content="x", confirmed=False)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).strftime(
        "%Y-%m-%d %H:%M:%S")
    await store.db.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?", (cutoff, old_id))
    await store.db.commit()

    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(
        store, lambda task=None: fake, max_workers=1,
        on_event=lambda k, t: None,
        retirement_job=None,  # disabled
    )
    await sched.tick()

    row = await store._fetchone(
        "SELECT archived FROM memories WHERE id = ?", (old_id,))
    assert row["archived"] == 0


@pytest.mark.asyncio
async def test_scheduler_tick_triggers_retirement_sweep(store):
    from no_human.learning import TYPE_SKILL

    old_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="old", content="x", confirmed=False)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).strftime(
        "%Y-%m-%d %H:%M:%S")
    await store.db.execute(
        "UPDATE memories SET created_at = ? WHERE id = ?", (cutoff, old_id))
    await store.db.commit()

    events = []
    job = RetirementSweepJob(store, interval_seconds=0, archive_after_days=45)
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(
        store, lambda task=None: fake, max_workers=1,
        on_event=lambda k, t: events.append((k, t)),
        retirement_job=job,
    )
    await sched.tick()

    row = await store._fetchone(
        "SELECT archived FROM memories WHERE id = ?", (old_id,))
    assert row["archived"] == 1
    assert any(k == "memory_sweep" for k, _ in events)


# --------------------------------------------------------------------------- #
# WikiRefreshJob                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wiki_refresh_job_due_timing():
    from no_human.core.scheduler import WikiRefreshJob
    # FakeBackend not needed — only testing due() logic.
    job = WikiRefreshJob(None, None, interval_seconds=60)
    assert job.due()  # first call always due
    job._last_run = time.time()
    assert not job.due()  # just ran, not due
    job._last_run = time.time() - 61
    assert job.due()  # past interval


@pytest.mark.asyncio
async def test_wiki_refresh_job_skips_matching_commit(store, tmp_path):
    """WikiRefreshJob skips repos where HEAD == wiki_commit (no-op)."""
    from no_human.core.scheduler import WikiRefreshJob

    job = WikiRefreshJob(store, None, interval_seconds=0)
    # No projects → nothing to do.
    result = await job.maybe_run()
    assert result == []


# --------------------------------------------------------------------------- #
# _summarize_event                                                             #
# --------------------------------------------------------------------------- #

from no_human.core.scheduler import _summarize_event


def test_summarize_event_tool_use_read():
    ev = {"kind": "tool_use", "tool_name": "Read", "tool_input": {"file_path": "/a/b/foo.py"}}
    assert _summarize_event(ev) == "reading foo.py"


def test_summarize_event_tool_use_edit():
    ev = {"kind": "tool_use", "tool_name": "Edit", "tool_input": {"file_path": "/x/bar.js"}}
    assert _summarize_event(ev) == "editing bar.js"


def test_summarize_event_tool_use_bash():
    ev = {"kind": "tool_use", "tool_name": "Bash", "tool_input": {"command": "pytest -x"}, "text": ""}
    assert _summarize_event(ev) == "running: pytest -x"


def test_summarize_event_state():
    ev = {"kind": "state", "text": "implementing"}
    assert _summarize_event(ev) == "implementing"


def test_summarize_event_commit():
    ev = {"kind": "commit", "text": "abc1234 fix bug"}
    assert _summarize_event(ev) == "committing changes"


def test_summarize_event_tests():
    ev = {"kind": "tests", "text": "3 passed"}
    assert _summarize_event(ev) == "running tests"


def test_summarize_event_irrelevant():
    ev = {"kind": "unknown_event", "text": "noise"}
    assert _summarize_event(ev) is None


@pytest.mark.asyncio
async def test_live_status_populated_via_sink(store):
    """_sink callback populates _live_status on the scheduler."""
    hold = asyncio.Event()

    class FakeOrchWithSink:
        async def run_task(self, task):
            # Simulate a tool_use event via the _sink callback
            self._sink({
                "kind": "tool_use",
                "tool_name": "Read",
                "tool_input": {"file_path": "/a/b/test.py"},
                "text": "Read test.py",
            })
            hold.set()
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    orch = FakeOrchWithSink()
    sched = Scheduler(store, lambda task=None: orch, max_workers=1)
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    await hold.wait()
    assert sched.get_live_status(t.id) == "reading test.py"

    await sched.wait_idle()   # let run finish
    # After task finishes, live_status should be cleared.
    assert sched.get_live_status(t.id) is None


@pytest.mark.asyncio
async def test_events_persisted_to_store_on_task_finish(store):
    """After a run finishes, its events are durably saved via store.save_events
    so they survive a server restart (Activity/System tabs)."""
    hold = asyncio.Event()

    class FakeOrchWithSink:
        async def run_task(self, task):
            self._sink({"kind": "tool_use", "tool_name": "Read",
                        "tool_input": {"file_path": "/a/b/test.py"}, "text": "Read test.py"})
            self._sink({"kind": "result", "text": "done"})
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            hold.set()
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    orch = FakeOrchWithSink()
    sched = Scheduler(store, lambda task=None: orch, max_workers=1)
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    await hold.wait()
    await sched.wait_idle()  # let the finally block's save_events land

    persisted = await store.list_events(t.id)
    # 3, not 2: `set_status(..., DONE, event=...)` now persists its own
    # completion event atomically with the status write (no silent done).
    assert len(persisted) == 3
    assert persisted[0]["kind"] == "tool_use"
    assert persisted[1]["kind"] == "result"


@pytest.mark.asyncio
async def test_sink_preserves_subagent_task_id(store):
    """A subagent event's own task_id (the SDK's per-dispatch Task-tool id,
    e.g. from claude_backend.py's subagent_start meta) must survive _sink
    untouched — it is a different concept from "which no_human task is
    this" and must not be overwritten. Regression for a bug where every
    distinct subagent dispatch collapsed to one node in the System view
    because _sink unconditionally stamped the outer task's id over it."""
    hold = asyncio.Event()

    class FakeOrchWithSubagentEvents:
        async def run_task(self, task):
            self._sink({"kind": "subagent_start", "text": "Research A",
                        "task_id": "sdk-dispatch-aaa"})
            self._sink({"kind": "subagent_start", "text": "Research B",
                        "task_id": "sdk-dispatch-bbb"})
            # An ordinary event with no task_id of its own still gets the
            # no_human task's id backfilled (existing, still-needed behavior).
            self._sink({"kind": "tool_use", "tool_name": "Read"})
            hold.set()
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    orch = FakeOrchWithSubagentEvents()
    sched = Scheduler(store, lambda task=None: orch, max_workers=1)
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    await hold.wait()

    events = sched.task_events(t.id)
    subagent_ids = {e["task_id"] for e in events if e["kind"] == "subagent_start"}
    assert subagent_ids == {"sdk-dispatch-aaa", "sdk-dispatch-bbb"}, (
        "distinct subagent dispatches must keep distinct task_ids"
    )
    tool_use = next(e for e in events if e["kind"] == "tool_use")
    assert tool_use["task_id"] == t.id  # backfilled, no id of its own


# --------------------------------------------------------------------------- #
# Events are persisted while the run is live, not only after it ends           #
# --------------------------------------------------------------------------- #

async def test_events_are_persisted_mid_run(store):
    """A crash used to lose the whole history: save_events ran once, in the
    `finally`. Mid-run the API served 133 events while task_events held 0."""
    mid_run_rows: list[int] = []

    class FakeOrchObservingItsOwnPersistence:
        _sink = staticmethod(lambda e: None)

        async def run_task(self, task):
            for i in range(5):
                self._sink({"kind": "tool_use", "tool_name": f"Read{i}"})
            # Give the flusher a chance to run while we are still "in" the
            # task — deterministic: poll the actual persisted rows instead of
            # guessing how many 0.01s flush intervals five events need.
            await _wait_until(lambda: _event_count_at_least(store, task.id, 5))
            mid_run_rows.append(len(await store.list_events(task.id)))
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    orch = FakeOrchObservingItsOwnPersistence()
    sched = Scheduler(store, lambda task=None: orch, max_workers=1)
    sched._EVENT_FLUSH_INTERVAL = 0.01
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    await sched.wait_idle()


    assert mid_run_rows == [5], "events must reach SQLite before the run ends"
    # 6, not 5: the final flush must not re-insert what was already written,
    # plus the one completion event `set_status(..., DONE, event=...)` now
    # persists atomically with the status write.
    assert len(await store.list_events(t.id)) == 6


async def test_flushed_events_are_not_duplicated_by_the_final_flush(store):
    """save_events INSERTs. Handing it the full buffer on every flush would
    write each event once per flush."""
    class SlowFakeOrch:
        _sink = staticmethod(lambda e: None)

        async def run_task(self, task):
            for i in range(3):
                self._sink({"kind": "tool_use", "tool_name": f"Read{i}"})
                # Wait for THIS event to actually land before emitting the
                # next one, so the real flush cycle (interval 0.01s) runs
                # several times across the loop instead of guessing how many
                # 0.03s sleeps that takes.
                await _wait_until(
                    lambda i=i: _event_count_at_least(store, task.id, i + 1))
            await store.set_status(task, TaskStatus.DONE, validate=False,
                                   event={"source": "test", "kind": "test_seed"})
            return SimpleNamespace(status=TaskStatus.DONE, task=task)

    sched = Scheduler(store, lambda task=None: SlowFakeOrch(), max_workers=1)
    sched._EVENT_FLUSH_INTERVAL = 0.01
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    await sched.wait_idle()


    persisted = await store.list_events(t.id)
    # 4, not 3: `set_status(..., DONE, event=...)` persists its own
    # completion event atomically with the status write.
    assert len(persisted) == 4
    tool_use = [e for e in persisted if e.get("kind") == "tool_use"]
    assert [e["tool_name"] for e in tool_use] == ["Read0", "Read1", "Read2"]


# --------------------------------------------------------------------------- #
# The pool is never wider than the worktree isolation allows                   #
# --------------------------------------------------------------------------- #

def test_pool_is_clamped_to_one_when_concurrency_is_disabled():
    """The live config was exactly this, and the server announced
    '2 worker(s) · concurrent' while parallelism was off — so two tasks would
    be dispatched into a pool the operator never asked for."""
    from no_human.core.scheduler import resolve_max_workers

    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": False, "max_workers": 2}})
    assert workers == 1
    assert warning and "concurrency.enabled is false" in warning


def test_an_explicit_worker_flag_is_clamped_too():
    from no_human.core.scheduler import resolve_max_workers

    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": False, "max_workers": 1}}, override=4)
    assert workers == 1, "a flag must not buy unisolated parallelism"
    assert warning


def test_concurrency_enabled_honours_the_configured_width():
    from no_human.core.scheduler import resolve_max_workers

    # `cpu_count` is pinned so this asserts the honouring, not the machine:
    # the width is now also bounded from above by cpu_count//3, so on a small
    # box an unpinned 3 or 4 would be clamped and this test would report a
    # machine size as a regression.
    assert resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 3}}, cpu_count=12) == (3, None)
    assert resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 1}},
        override=4, cpu_count=12) == (4, None)


def test_resolve_max_workers_defaults_are_serial_and_silent():
    from no_human.core.scheduler import resolve_max_workers

    assert resolve_max_workers({}) == (1, None)
    assert resolve_max_workers({"concurrency": {}}) == (1, None)
    # A single worker with concurrency off is the normal case: no warning.
    assert resolve_max_workers({"concurrency": {"enabled": False, "max_workers": 1}}) == (1, None)
    # Degenerate values never produce a zero-width pool.
    assert resolve_max_workers({"concurrency": {"enabled": True, "max_workers": 0}}) == (1, None)


def test_explicit_serve_flag_enables_isolated_pool():
    """SCRUM-10: `nh serve --max-workers N` must run the pool without a
    config edit — the opposite of resolve_max_workers's clamp, since the
    flag itself is what turns isolation on for this invocation."""
    from no_human.core.scheduler import resolve_serve_pool

    # cpu_count pinned: on a 4-core CI runner the sanity ceiling is
    # max(2, 4//3) == 2 and this test would fail for machine size, not
    # behavior — the exact class the clamp change pinned everywhere else.
    workers, enabled, error = resolve_serve_pool(
        {"concurrency": {"enabled": False, "max_workers": 1}}, cli_workers=3,
        cpu_count=12)
    assert (workers, enabled, error) == (3, True, None)


def test_serve_without_flag_and_disabled_refuses():
    """Absent flag = unchanged: still refuses to serve when concurrency is
    off in config, exactly like before this feature existed."""
    from no_human.core.scheduler import resolve_serve_pool

    workers, enabled, error = resolve_serve_pool(
        {"concurrency": {"enabled": False, "max_workers": 1}}, cli_workers=None)
    assert enabled is False
    assert error is not None


def test_serve_without_flag_honours_config():
    """Absent flag = unchanged: an already-enabled config drives the pool
    width exactly as before."""
    from no_human.core.scheduler import resolve_serve_pool

    assert resolve_serve_pool(
        {"concurrency": {"enabled": True, "max_workers": 2}}, cli_workers=None,
    ) == (2, True, None)


def test_serve_without_flag_defaults_to_two_when_enabled_and_unset():
    """Absent flag = unchanged: serve()'s historical default was 2 workers
    when concurrency.enabled is true but max_workers isn't set — must not
    silently drop to resolve_max_workers's 1-worker override default."""
    from no_human.core.scheduler import resolve_serve_pool

    assert resolve_serve_pool(
        {"concurrency": {"enabled": True}}, cli_workers=None,
    ) == (2, True, None)


def test_serve_flag_rejects_non_positive():
    """CLI hygiene: zero/negative --max-workers is rejected with a clear
    error rather than silently degrading to some other width."""
    from no_human.core.scheduler import resolve_serve_pool

    workers, enabled, error = resolve_serve_pool({}, cli_workers=0)
    assert error is not None
    assert workers == 0

    workers, enabled, error = resolve_serve_pool({}, cli_workers=-1)
    assert error is not None
    assert workers == 0


# --------------------------------------------------------------------------- #
# ... and never wider than the machine can carry                               #
# --------------------------------------------------------------------------- #

def test_the_ceiling_is_a_third_of_the_cores_with_a_floor_of_two():
    from no_human.core.scheduler import pool_width_ceiling

    assert pool_width_ceiling(12) == 4
    assert pool_width_ceiling(9) == 3
    # The floor keeps the shipped default (2) reachable on a small machine
    # instead of silently serialising it.
    assert pool_width_ceiling(6) == 2
    assert pool_width_ceiling(1) == 2


def test_a_width_above_the_ceiling_is_clamped_with_a_stated_reason():
    """`nh start --workers 64` was accepted in full: both resolvers bounded
    the pool from below only."""
    from no_human.core.scheduler import clamp_pool_width

    width, reason = clamp_pool_width(64, cpu_count=12)
    assert width == 4
    assert reason, "a clamp the operator cannot see is the silent accept again"
    # It says which width is running, which was asked for, and why.
    assert "64" in reason and "4" in reason
    # The why is what this ceiling actually is — a sanity bound on absurd
    # widths, derived from each worker owning a nested subprocess tree and a
    # pytest -n run. It must NOT be sold as a stability guarantee: crashes at
    # small widths are a separate, known issue and this does not fix them.
    assert "sanity ceiling" in reason
    assert "nested agent subprocesses" in reason and "pytest -n" in reason
    assert "known separate issue" in reason
    # And it cites no particular machine's saved config as its evidence.
    assert "this install" not in reason and "config records" not in reason


def test_a_width_at_or_below_the_ceiling_is_untouched_and_silent():
    from no_human.core.scheduler import clamp_pool_width

    assert clamp_pool_width(4, cpu_count=12) == (4, None)   # exactly at it
    assert clamp_pool_width(3, cpu_count=12) == (3, None)
    assert clamp_pool_width(1, cpu_count=12) == (1, None)
    assert clamp_pool_width(2, cpu_count=1) == (2, None)    # the floor


def test_resolve_max_workers_clamps_the_flag_and_the_config():
    from no_human.core.scheduler import resolve_max_workers

    # The flag path (`nh start --workers 64`).
    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 2}},
        override=64, cpu_count=12)
    assert workers == 4
    assert warning and "ceiling" in warning

    # The config path (`concurrency.max_workers: 64` on disk).
    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 64}}, cpu_count=12)
    assert workers == 4
    assert warning and "ceiling" in warning


def test_the_isolation_downgrade_still_wins_over_the_ceiling():
    """Order matters: an over-wide request with isolation off must still be
    told about ISOLATION — that is the switch it has to fix, and the operator
    only gets one warning per run."""
    from no_human.core.scheduler import resolve_max_workers

    workers, warning = resolve_max_workers(
        {"concurrency": {"enabled": True, "max_workers": 64},
         "isolation": {"enabled": False}}, cpu_count=12)
    assert workers == 1
    assert warning and "isolation.enabled is false" in warning
    assert "ceiling" not in warning
    # ...and it quotes the width the operator ASKED for. This is what pins the
    # order: clamping first and then reporting the isolation refusal leaves
    # every assertion above true while the message reads "not 4" — a number the
    # operator never typed, about a limit that is not their problem here.
    assert "not 64" in warning, warning


def test_resolve_serve_pool_clamps_the_flag_and_the_config():
    from no_human.core.scheduler import resolve_serve_pool

    assert resolve_serve_pool(
        {}, cli_workers=64, cpu_count=12) == (4, True, None)
    assert resolve_serve_pool(
        {"concurrency": {"enabled": True, "max_workers": 64}},
        cli_workers=None, cpu_count=12) == (4, True, None)
    # Below the ceiling nothing moves — the flag still buys the pool it asked
    # for, and serve's historical no-flag default of 2 is untouched.
    assert resolve_serve_pool(
        {}, cli_workers=3, cpu_count=12) == (3, True, None)
    assert resolve_serve_pool(
        {"concurrency": {"enabled": True}}, cli_workers=None,
        cpu_count=12) == (2, True, None)


def test_bounded_xdist_workers():
    """The CPU-oversubscription guard (2026-07-11): 3 tasks × pytest -n auto
    on 12 cores must not spawn 36 workers."""
    from no_human.core.scheduler import bounded_xdist_workers
    assert bounded_xdist_workers(3, 12, None) == "4"      # 12//3
    assert bounded_xdist_workers(5, 12, None) == "2"      # 12//5
    assert bounded_xdist_workers(20, 12, None) == "1"     # floor at 1
    assert bounded_xdist_workers(1, 12, None) is None     # serial: untouched
    assert bounded_xdist_workers(3, 12, "8") is None      # explicit choice kept


@pytest.mark.asyncio
async def test_resumed_work_claims_before_fresh_pending(store):
    """WIP-first: a task resumed to IMPLEMENTING (sunk cost, operator waiting)
    must claim a free slot before a newer PENDING task. Live starvation
    (2026-07-24): every newly imported ticket jumped the single slot ahead of
    three budget-raised resumes, which then false-stalled on a 40-min cycle."""
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    resumed = Task.new("resumed WIP", repo_path="/tmp/x")
    await store.create_task(resumed)
    await store.set_status(resumed, TaskStatus.IMPLEMENTING, validate=False)
    fresh = Task.new("fresh pending", repo_path="/tmp/x")
    await store.create_task(fresh)

    started = await sched.tick()
    assert started == [resumed.id], (
        f"expected the resumed task to claim the slot, got {started}")


@pytest.mark.asyncio
async def test_wip_first_beats_an_older_pending(store):
    """WIP-first must hold even when the resumed task is the NEWER row —
    the naive fix (one merged `ORDER BY created_at ASC` across statuses)
    would hand the slot to the older PENDING task instead. The existing
    `test_resumed_work_claims_before_fresh_pending` can't catch that: there
    the resumed task already happens to be older, so a global ASC sort would
    pass it by accident too."""
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    older_pending = Task.new("older pending", repo_path="/tmp/x")
    older_pending.created_at = "2026-08-01T08:00:00+00:00"
    await store.create_task(older_pending)

    resumed = Task.new("resumed WIP, newer", repo_path="/tmp/x")
    resumed.created_at = "2026-08-11T08:00:00+00:00"
    await store.create_task(resumed)
    await store.set_status(resumed, TaskStatus.IMPLEMENTING, validate=False)

    started = await sched.tick()
    assert started == [resumed.id], (
        f"expected WIP-first to beat the older pending task, got {started}")


@pytest.mark.asyncio
async def test_oldest_pending_claims_before_newer_pending(store):
    """Bug (2026-08-12): the claim path consumed `list_tasks`, which is
    `created_at DESC` for the board, so the NEWEST pending ticket dispatched
    first and day-old tickets starved behind every fresh filing. Queue order
    within a status must be FIFO (oldest first).

    `older` is created SECOND (higher rowid) so neither insertion order nor
    rowid can accidentally produce the pass — only `created_at ASC` can."""
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    newer = Task.new("filed overnight", repo_path="/tmp/x")
    newer.created_at = "2026-08-12T08:00:00+00:00"
    await store.create_task(newer)

    older = Task.new("filed a day ago", repo_path="/tmp/x")
    older.created_at = "2026-08-11T08:00:00+00:00"
    await store.create_task(older)

    started = await sched.tick()
    assert started == [older.id], (
        f"expected the older pending task to claim the slot first, got {started}")


@pytest.mark.asyncio
async def test_claim_order_is_fifo_across_a_full_slice(store):
    """Guards against a fix that only sorts the head of the claim list: five
    PENDING tasks created in shuffled `created_at` order must be claimed in
    ascending `created_at` order across the whole slice, not just the first
    pick."""
    fake = FakeOrch(store, hold=asyncio.Event())
    sched = Scheduler(store, lambda task=None: fake, max_workers=3)

    stamps = [
        "2026-08-05T08:00:00+00:00",
        "2026-08-09T08:00:00+00:00",
        "2026-08-03T08:00:00+00:00",
        "2026-08-11T08:00:00+00:00",
        "2026-08-07T08:00:00+00:00",
    ]
    tasks = []
    for i, stamp in enumerate(stamps):
        t = Task.new(f"shuffled {i}", repo_path="/tmp/x")
        t.created_at = stamp
        await store.create_task(t)
        tasks.append(t)

    expected = [t.id for t in sorted(tasks, key=lambda t: t.created_at)[:3]]
    started = await sched.tick()
    assert started == expected, (
        f"expected the three oldest ids in ascending order, got {started}")


@pytest.mark.asyncio
async def test_startup_recovers_orphaned_midrun_tasks(tmp_path):
    """Review 2026-07-25: scoping the stuck sweep to claimed tasks left a
    task orphaned in a mid-run status (process killed during review/testing)
    invisible forever — not claimable, not swept. Startup must requeue it."""
    from no_human.core.db import Store
    from no_human.core.task import Task, TaskStatus

    store = await Store(tmp_path / "t.db").connect()
    try:
        orphan = Task.new("killed mid-review", repo_path="/r")
        await store.create_task(orphan)
        await store.set_status(orphan, TaskStatus.REVIEWING, validate=False)
        healthy = Task.new("parked", repo_path="/r")
        await store.create_task(healthy)
        await store.set_status(healthy, TaskStatus.ESCALATED, validate=False)
        await _age_row(store, orphan.id)

        events = []
        sched = Scheduler(store, lambda task=None: None,
                          on_event=lambda k, t: events.append((k, t)))
        await sched._recover_orphans()

        fresh = await store.get_task(orphan.id)
        assert fresh.status is TaskStatus.IMPLEMENTING  # claimable again
        recorded = await store.list_events(orphan.id)
        assert any(e["kind"] == "orphan_recovered" for e in recorded)
        assert any(k == "orphan_recovered" for k, _ in events)
        # Parked/terminal tasks are NOT touched.
        assert (await store.get_task(healthy.id)).status is TaskStatus.ESCALATED
    finally:
        await store.close()


def _branch_point(ctx: dict) -> str:
    """What the requeued run will actually branch from, asked of the code that
    decides it. A requeue re-enters `run_task` as a FRESH bounded loop, so
    attempt_n is 1 — the number at which `handoff.wip_sha` is ignored."""
    from no_human.core.orchestrator import Orchestrator
    return Orchestrator._resume_branch_point(None, None, ctx, 1)


class _SubjectRepo:
    """Stand-in for GitRepo where only the commit SUBJECT matters."""

    def __init__(self, subject: str):
        self.subject = subject

    def _run(self, *args, **kw):
        return self.subject


def _gate_armed(ctx: dict, sha: str, subject: str) -> bool:
    """Is the zero-diff honesty gate armed for this branch point?

    Asked of `_is_own_partial` itself, not of a proxy. True means the work
    already ahead of base is the LOOP's and must NOT be credited to an attempt
    that edited nothing.
    """
    from no_human.core.orchestrator import Orchestrator
    return Orchestrator._is_own_partial(
        Orchestrator.__new__(Orchestrator), _SubjectRepo(subject), ctx, sha)


@pytest.mark.asyncio
async def test_orphan_recovery_resumes_from_the_dead_attempts_commit(store):
    """A restart must resume the in-flight attempt, not burn it.

    Measured 2026-08-10: three server restarts produced 11 attempts closed as
    'interrupted: superseded by a newer attempt' across 5 live tasks, and every
    successor restarted the coder from base even though the dead attempt's work
    was committed and its sha recorded on the attempt row. Nothing was reported
    lost (`resume_checkpoint_lost` NULL on all of them) because no checkpoint
    was ever considered.
    """
    orphan = Task.new("killed mid-review", repo_path="/r")
    await store.create_task(orphan)
    await store.set_status(orphan, TaskStatus.REVIEWING, validate=False)
    attempt_id = await store.create_attempt(orphan.id, 1)
    sha = "a" * 40
    await store.update_attempt(attempt_id, commit_sha=sha)
    await _age_row(store, orphan.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    fresh = await store.get_task(orphan.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    resume_from = (fresh.context or {}).get("resume_from") or {}
    assert resume_from.get("sha") == sha, (
        "the requeued row carries no checkpoint — the successor attempt will "
        f"redo the dead attempt's work from base; got {resume_from!r}")
    # A machine requeue is NOT a human gate. Asked of the GATE, not of a proxy:
    # `by not in (None, "human")` was the assertion here and it proved nothing —
    # `_is_own_partial` short-circuited on the commit's SHAPE before it ever
    # read `by`, and `commit_sha` on an attempt row is the attempt's ORDINARY
    # work commit (`_run_attempt`: update_attempt(commit_sha=commit.sha)), never
    # a [WIP-PARTIAL]. So the label was right and the gate was DOWN: the
    # successor attempt could edit nothing, inherit this diff and be recorded
    # `succeeded` with `unproductive_streak` never incrementing.
    assert _gate_armed(fresh.context, sha, "fix: the dead attempt's work"), (
        "the zero-diff honesty gate is DOWN on a machine requeue whose "
        "checkpoint is an ordinary work commit")
    assert _branch_point(fresh.context) == sha


def test_a_human_gated_checkpoint_still_disarms_the_gate():
    """Control for the assertion above, in the direction that has regressed
    twice (D15 / task-84251cb2): a human who gated the branch point IS credited
    with the work already on it, whatever the commit's subject looks like.
    Arming there fails a correct "nothing to add" as fabrication."""
    for subject in ("fix: work a human gated", "[WIP-PARTIAL] half a feature"):
        ctx = {"resume_from": {"sha": "a" * 40, "by": "human"}}
        assert _gate_armed(ctx, "a" * 40, subject) is False


def test_a_consumed_human_gate_still_disarms_the_gate():
    """Same control, once an attempt has actually branched from the human's
    sha (`Orchestrator._consume_human_gate` rewrites `by` to
    `consumed_human`): the work at this sha is still the human's, so credit
    must not flip the moment the gate is consumed — see
    tests/test_human_gate_consume_once.py for the full consume-once story."""
    for subject in ("fix: work a human gated", "[WIP-PARTIAL] half a feature"):
        ctx = {"resume_from": {"sha": "a" * 40, "by": "consumed_human"}}
        assert _gate_armed(ctx, "a" * 40, subject) is False


def test_the_gate_still_reads_the_subject_when_no_provenance_names_the_sha():
    """Control: on the `handoff.wip_sha` / reused-`pr_branch` paths nothing
    records who produced the branch point, so the SUBJECT remains the signal —
    provenance only outranks shape for the sha it actually names."""
    ctx = {"resume_from": {"sha": "b" * 40, "by": "human"}}
    assert _gate_armed(ctx, "a" * 40, "[WIP-PARTIAL] half a feature") is True
    assert _gate_armed(ctx, "a" * 40, "fix: a real commit") is False


@pytest.mark.asyncio
async def test_orphan_recovery_without_a_checkpoint_still_starts_cold(store):
    """Control: an attempt that died before committing anything has nothing to
    resume onto, and must requeue exactly as it always has — branching from
    base, with no fabricated provenance."""
    orphan = Task.new("killed before its first commit", repo_path="/r")
    await store.create_task(orphan)
    await store.set_status(orphan, TaskStatus.CONTEXT, validate=False)
    await store.create_attempt(orphan.id, 1)          # no commit_sha
    await _age_row(store, orphan.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    fresh = await store.get_task(orphan.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert not ((fresh.context or {}).get("resume_from") or {}).get("sha")
    assert _branch_point(fresh.context or {}) == ""


@pytest.mark.asyncio
async def test_orphan_recovery_never_overwrites_an_existing_provenance(store):
    """The requeue must still INHERIT a decision another actor already made:
    stamping over a human's `resume_from` relabels their gated sha as the
    machine's and fails their resume as fabrication (orchestrator
    `_is_own_partial`)."""
    from no_human.blockers import resume_provenance

    orphan = Task.new("resumed by a human, then killed", repo_path="/r")
    await store.create_task(orphan)
    gated = "b" * 40
    await store.merge_context(
        orphan.id, {"resume_from": resume_provenance({"sha": gated}, "human")})
    await store.set_status(orphan, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(orphan.id, 1)
    await store.update_attempt(attempt_id, commit_sha="c" * 40)
    await _age_row(store, orphan.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    resume_from = ((await store.get_task(orphan.id)).context or {})["resume_from"]
    # `branch: None` DELETES under RFC 7396, so it is absent, not stored.
    assert resume_from == {"sha": gated, "by": "human"}


@pytest.mark.asyncio
async def test_orphan_recovery_does_not_resurrect_a_deliberately_cleared_checkpoint(
        store):
    """A CLEARS path removed the checkpoint; the sweep must not put it back.

    `POST /api/tasks/{id}/retry` and its siblings write `resume_from: None` —
    "retry means from base; a fresh run must not silently branch from a
    checkpoint some EARLIER actor chose". The candidate sha must therefore
    come from the attempt that
    actually DIED (the one still `in_progress`, db.py `create_attempt` closes
    every older one), never from "the newest attempt this task ever had": the
    latter reaches straight back past the clear, into a run that already
    failed, and resurrects exactly what the human deleted.
    """
    t = Task.new("failed, then retried from base", repo_path="/r")
    await store.create_task(t)
    dead = await store.create_attempt(t.id, 1)
    await store.update_attempt(dead, commit_sha="a" * 40)

    t.context = await store.merge_context(t.id, {"resume_from": None})
    # run 2 starts (closing run 1's row) and dies before committing anything
    await store.create_attempt(t.id, 2)
    await store.set_status(t, TaskStatus.PLANNING, validate=False)
    await _age_row(store, t.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    fresh = await store.get_task(t.id)
    assert not ((fresh.context or {}).get("resume_from") or {}).get("sha"), (
        "the retry's cleared checkpoint came back from a run that already "
        f"failed; got {(fresh.context or {}).get('resume_from')!r}")


@pytest.mark.asyncio
async def test_a_second_restart_restamps_instead_of_reusing_its_own_stale_sha(store):
    """The measured incident was THREE restarts, not one.

    Bailing on ANY existing `resume_from` cannot tell a human's gate from the
    machine's own earlier stamp, so restart 2 resumed onto restart 1's sha and
    silently discarded everything the run in between committed — the same class
    of loss this branch exists to stop, one restart later. Only `by == "human"`
    is inherited; a machine stamp is re-stamped with the dead attempt's own sha.
    """
    t = Task.new("orphaned twice", repo_path="/r")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(a1, commit_sha="a" * 40)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    await _age_row(store, t.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()
    assert ((await store.get_task(t.id)).context or {})["resume_from"] == {
        "sha": "a" * 40, "by": "orphan_recovery"}

    # the requeued run gets further, commits MORE work, and dies again
    a2 = await store.create_attempt(t.id, 2)
    await store.update_attempt(a2, commit_sha="b" * 40)
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.TESTING, validate=False)
    await _age_row(store, t.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    resume_from = ((await store.get_task(t.id)).context or {})["resume_from"]
    assert resume_from == {"sha": "b" * 40, "by": "orphan_recovery"}, (
        "restart 2 resumed onto restart 1's stale checkpoint and discarded "
        f"the interim run's commit; got {resume_from!r}")


@pytest.mark.asyncio
async def test_a_machine_stamp_survives_a_restart_that_finds_nothing_newer(store):
    """Control for the re-stamp above: re-stamping is not CLEARING. A requeued
    run that dies before committing leaves the previous machine checkpoint in
    place — dropping it would burn the very work the stamp was protecting."""
    t = Task.new("orphaned twice, second run committed nothing", repo_path="/r")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(a1, commit_sha="a" * 40)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    await _age_row(store, t.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()
    await store.create_attempt(t.id, 2)               # no commit_sha
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.TESTING, validate=False)
    await _age_row(store, t.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    assert ((await store.get_task(t.id)).context or {})["resume_from"] == {
        "sha": "a" * 40, "by": "orphan_recovery"}


async def test_a_pool_crash_records_a_durable_reason(store):
    """A task killed by a pool-level exception must say WHY, somewhere durable.

    The handler logged to a file the board never reads and emitted `task_error`
    on the LIVE pool stream — gone the moment nobody is watching — then set
    FAILED. So the task ended with no recorded cause anywhere a human looks.
    That matters more since the drawer's "Why it failed" reads the last
    attempt's failure_reason: a crash here can happen with no attempt row at
    all, so the task explains itself nowhere.
    """
    class CrashingOrch:
        async def run_task(self, task):
            raise RuntimeError("boom inside the pool")

    sched = Scheduler(store, lambda task=None: CrashingOrch(), max_workers=1)
    ids = await _mk_tasks(store, 1)

    await sched.tick()
    await sched.wait_idle()


    t = await store.find_task(ids[0])
    assert t.status is TaskStatus.FAILED, "the crash must still terminate the task"

    events = await store.list_events(ids[0])
    crashed = [e for e in events if e.get("kind") == "task_crashed"]
    assert crashed, (
        "a pool crash left no durable record — only a log line and a transient "
        f"live event; got kinds: {[e.get('kind') for e in events]}")
    assert "boom inside the pool" in crashed[0]["text"], (
        "the record must carry the actual exception, not just that one happened")
    assert "RuntimeError" in crashed[0]["text"], "and its type"

    # The pool must SURVIVE the crash — the whole reason that except exists.
    assert sched.inflight == set(), "the crashed task was not released"


async def test_a_pool_crash_increments_the_worker_death_counter(store):
    """The all-time worker-death counter must be a real counter — visible on
    `health_snapshot()` (what `/api/worker/status` serves) and surviving past
    `_crash_times`'s rolling rate windows, which age entries out."""
    class CrashingOrch:
        async def run_task(self, task):
            raise RuntimeError("boom inside the pool")

    sched = Scheduler(store, lambda task=None: CrashingOrch(), max_workers=1)
    ids = await _mk_tasks(store, 2)

    assert sched.health_snapshot()["worker_deaths_total"] == 0

    await sched.tick()
    await sched.wait_idle()
    assert sched.health_snapshot()["worker_deaths_total"] == 1

    await sched.tick()
    await sched.wait_idle()
    assert sched.health_snapshot()["worker_deaths_total"] == 2, (
        "a second worker death must ADD to the counter, not replace it")


async def test_a_pool_crash_records_exit_code_and_termination_reason(store):
    """When the crashing exception carries a subprocess-style `.returncode`,
    that exit status and a clean termination reason are captured and
    persisted — not just folded into the free-text `text` field."""
    class CrashingOrch:
        async def run_task(self, task):
            exc = RuntimeError("worker subprocess died")
            exc.returncode = 137  # SIGKILL-style exit status
            raise exc

    sched = Scheduler(store, lambda task=None: CrashingOrch(), max_workers=1)
    ids = await _mk_tasks(store, 1)

    await sched.tick()
    await sched.wait_idle()

    events = await store.list_events(ids[0])
    crashed = [e for e in events if e.get("kind") == "task_crashed"]
    assert crashed, "no durable task_crashed event was recorded"
    assert crashed[0]["exit_code"] == 137, (
        "the exception's returncode must be captured as the exit status")
    assert "worker subprocess died" in crashed[0]["termination_reason"]
    assert "RuntimeError" in crashed[0]["termination_reason"]


async def test_a_pool_crash_with_no_exit_code_records_none(store):
    """An ordinary Python exception (no subprocess behind it) must not
    fabricate an exit status — `exit_code` stays None rather than 0 or some
    other made-up value, so a reader can tell "no process" from "exited 0"."""
    class CrashingOrch:
        async def run_task(self, task):
            raise RuntimeError("boom, no subprocess involved")

    sched = Scheduler(store, lambda task=None: CrashingOrch(), max_workers=1)
    ids = await _mk_tasks(store, 1)

    await sched.tick()
    await sched.wait_idle()

    events = await store.list_events(ids[0])
    crashed = [e for e in events if e.get("kind") == "task_crashed"]
    assert crashed[0]["exit_code"] is None


async def test_a_pool_crash_preserves_the_dying_attempt_stderr(store):
    """Per-attempt stderr on the crashing exception (e.g. a
    `subprocess.CalledProcessError`) is preserved on the durable event — this
    IS the "outside a restart" path: nothing above `_run`'s except retries or
    restarts the coroutine, so this is the only chance to keep that output."""
    class CrashingOrch:
        async def run_task(self, task):
            exc = RuntimeError("build step died")
            exc.stderr = "compiler error: missing semicolon\n"
            raise exc

    sched = Scheduler(store, lambda task=None: CrashingOrch(), max_workers=1)
    ids = await _mk_tasks(store, 1)

    await sched.tick()
    await sched.wait_idle()

    events = await store.list_events(ids[0])
    crashed = [e for e in events if e.get("kind") == "task_crashed"]
    assert "compiler error: missing semicolon" in crashed[0]["stderr_excerpt"]


async def test_a_pool_crash_stderr_is_capped_not_unbounded(store):
    """A runaway process's stderr must not balloon the persisted event
    forever — capped the same way `testing/runner.py`'s output excerpts are."""
    class CrashingOrch:
        async def run_task(self, task):
            exc = RuntimeError("noisy subprocess died")
            exc.stderr = "x" * 10_000
            raise exc

    sched = Scheduler(store, lambda task=None: CrashingOrch(), max_workers=1)
    ids = await _mk_tasks(store, 1)

    await sched.tick()
    await sched.wait_idle()

    events = await store.list_events(ids[0])
    crashed = [e for e in events if e.get("kind") == "task_crashed"]
    assert len(crashed[0]["stderr_excerpt"]) < 10_000, (
        "10k of stderr must be truncated, not stored verbatim")
    assert "truncated" in crashed[0]["stderr_excerpt"]


async def test_a_pool_crash_with_no_stderr_omits_the_field(store):
    """An exception with nothing on `.stderr` must not persist an empty/None
    excerpt — the field's ABSENCE is how a reader tells "nothing captured"
    from "captured and it was empty"."""
    class CrashingOrch:
        async def run_task(self, task):
            raise RuntimeError("no stderr on this one")

    sched = Scheduler(store, lambda task=None: CrashingOrch(), max_workers=1)
    ids = await _mk_tasks(store, 1)

    await sched.tick()
    await sched.wait_idle()

    events = await store.list_events(ids[0])
    crashed = [e for e in events if e.get("kind") == "task_crashed"]
    assert "stderr_excerpt" not in crashed[0]


# --------------------------------------------------------------------------- #
# `nh serve --until-empty`: drain-and-exit (KI-3 / ADOPT-17)                   #
# --------------------------------------------------------------------------- #


class TerminalOrch:
    """Ends each task in a status chosen per id — used to prove the exit
    condition tells a FAILED task apart from an honest park."""

    def __init__(self, store, statuses: dict, default=TaskStatus.AWAITING_APPROVAL):
        self.store = store
        self.statuses = statuses
        self.default = default
        self.started: list[str] = []

    async def run_task(self, task):
        self.started.append(task.id)
        status = self.statuses.get(task.id, self.default)
        await self.store.set_status(task, status, validate=False)
        return SimpleNamespace(status=status, task=task)


async def test_until_empty_exits_immediately_on_an_empty_queue(store):
    """Nothing queued → the drain is already done. The poll interval is 30s
    and the whole call must return well inside it: an empty queue must not
    cost a caller one tick of waiting."""
    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=2)
    stop = asyncio.Event()

    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=30.0, until_empty=True), 2.0)

    assert stop.is_set(), "the drain must set the SAME stop event a signal sets"
    assert await sched.queue_is_drained()
    assert await sched.failed_dispatched() == []


async def test_until_empty_drains_the_queue_then_exits(store):
    fake = FakeOrch(store)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2)
    ids = await _mk_tasks(store, 5)
    stop = asyncio.Event()

    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True), 10.0)

    assert sorted(fake.started) == sorted(ids), "every queued task must run"
    assert sched.inflight == set()
    assert await sched.queue_is_drained()
    assert await sched.failed_dispatched() == []


async def test_until_empty_reports_a_failed_task_but_not_a_park(store):
    """The exit condition is keyed on `task.status == TaskStatus.FAILED`.
    A parked task (here ESCALATED — an honest off-ramp the loop is designed
    to produce) must NOT make the batch look failed, or every real run of a
    hard task exits non-zero and the code says nothing."""
    ids = await _mk_tasks(store, 2)
    orch = TerminalOrch(store, {ids[0]: TaskStatus.FAILED,
                                ids[1]: TaskStatus.ESCALATED})
    sched = Scheduler(store, lambda task=None: orch, max_workers=2)
    stop = asyncio.Event()

    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True), 10.0)

    assert await sched.failed_dispatched() == [ids[0]]


async def test_until_empty_ignores_failed_rows_this_process_never_dispatched(store):
    """A real install's DB routinely holds OLD failed rows. The exit code is
    scoped to `self._dispatched` — tasks THIS process started — or every cron
    run of `--until-empty` exits 1 forever and non-zero means nothing. This is
    the test an independent review's mutation (count ALL failed rows) proved
    missing: with that mutation, the stale row below flips the answer."""
    stale = Task.new("failed long ago", repo_path="/tmp/x")
    await store.create_task(stale)
    await store.set_status(stale, TaskStatus.FAILED, validate=False)

    ids = await _mk_tasks(store, 1)
    fake = FakeOrch(store)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)
    stop = asyncio.Event()
    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True), 10.0)

    assert fake.started == ids, "the fresh task ran"
    assert await sched.failed_dispatched() == [], (
        "a FAILED row this process never dispatched leaked into the exit code")


async def test_until_empty_does_not_stop_while_a_task_is_still_running(store):
    """`_claimable` is empty the moment the only task is dispatched — if the
    exit condition ignored `_inflight` the drain would exit mid-task."""
    hold = asyncio.Event()
    sched = Scheduler(store, lambda task=None: FakeOrch(store, hold=hold),
                      max_workers=1)
    await _mk_tasks(store, 1)
    stop = asyncio.Event()
    run = asyncio.ensure_future(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True))

    await _wait_until(lambda: bool(sched.inflight))
    assert not run.done(), "exited while a task was still in flight"
    assert not await sched.queue_is_drained()

    hold.set()
    await asyncio.wait_for(run, 10.0)
    assert await sched.queue_is_drained()


async def test_bare_run_forever_still_runs_forever_on_an_empty_queue(store):
    """The default is untouched: no flag, no exit, empty queue or not."""
    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=1)
    stop = asyncio.Event()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            sched.run_forever(stop=stop, poll_interval=0.01), 0.4)
    assert not stop.is_set()


async def test_a_task_left_unclaimed_by_a_full_pool_says_so_once(store):
    """A resumed task (`wake._resume`) is written IMPLEMENTING before any
    worker is attached; with the pool full the scheduler deliberately leaves
    it unclaimed rather than mislabel it — but the task's own record must say
    so, once, not sit silent (dc3b72f7)."""
    hold = asyncio.Event()
    fake = FakeOrch(store, hold=hold)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    pending = Task.new("fresh pending", repo_path="/tmp/x")
    await store.create_task(pending)
    resumed = Task.new("resumed WIP", repo_path="/tmp/x")
    await store.create_task(resumed)
    await store.set_status(resumed, TaskStatus.IMPLEMENTING, validate=False)

    started = await sched.tick()
    assert len(started) == 1
    inflight_id = started[0]
    waiting_id = pending.id if inflight_id == resumed.id else resumed.id

    for _ in range(5):
        await sched.tick()

    waits = [e for e in await store.list_events(waiting_id)
              if e["kind"] == "waiting_for_slot"]
    assert len(waits) == 1, f"expected exactly one wait event, got {waits}"
    assert waits[0]["text"].startswith("waiting for a worker slot")
    assert "1/1 busy" in waits[0]["text"]

    inflight_waits = [e for e in await store.list_events(inflight_id)
                        if e["kind"] == "waiting_for_slot"]
    assert inflight_waits == [], "the inflight task got a wait event too"

    hold.set()
    await sched.wait_idle()

    resumed_started = await sched.tick()
    assert waiting_id in resumed_started

    waits_after = [e for e in await store.list_events(waiting_id)
                    if e["kind"] == "waiting_for_slot"]
    assert len(waits_after) == 1, (
        f"expected no re-emit once the slot freed, got {waits_after}")


async def test_a_second_wait_after_running_gets_its_own_event(store):
    """A distinct later waiting period gets a second event — the dedupe is
    per continuous wait, not per task lifetime (intake Q&A)."""
    hold = asyncio.Event()
    fake = FakeOrch(store, hold=hold)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    pending = Task.new("fresh pending", repo_path="/tmp/x")
    await store.create_task(pending)
    resumed = Task.new("resumed WIP", repo_path="/tmp/x")
    await store.create_task(resumed)
    await store.set_status(resumed, TaskStatus.IMPLEMENTING, validate=False)

    started = await sched.tick()
    inflight_id = started[0]
    waiting_id = pending.id if inflight_id == resumed.id else resumed.id

    await sched.tick()
    waits = [e for e in await store.list_events(waiting_id)
              if e["kind"] == "waiting_for_slot"]
    assert len(waits) == 1

    # The first task finishes and frees the slot.
    hold.set()
    await sched.wait_idle()

    freed = await sched.tick()
    assert waiting_id in freed

    # The pool fills back up immediately with a fresh hold, and the OTHER
    # task (now claimable again) is left waiting a second, distinct period.
    fake.hold = asyncio.Event()
    await store.set_status(
        await store.get_task(inflight_id), TaskStatus.IMPLEMENTING,
        validate=False)

    await sched.tick()
    waits_second = [e for e in await store.list_events(inflight_id)
                     if e["kind"] == "waiting_for_slot"]
    assert len(waits_second) == 1, (
        f"expected a fresh wait event for the second period, got {waits_second}")


async def test_a_resumed_task_waiting_behind_a_running_task_says_so(store):
    """Review round 2 of PR #525: the earlier acceptance test let whichever
    task the scheduler picked first become the inflight one — and because
    `_claimable` iterates IMPLEMENTING before PENDING, it was always the
    RESUMED task that ran and the PENDING one that waited. This pins the
    named scenario: a task already running, then a RESUMED task arrives and
    must wait — and say so."""
    hold = asyncio.Event()

    class _EmittingOrch(FakeOrch):
        """The real orchestrator's first act on a claimed task is an event
        (`repo_config`, `state: context`, ...) — that first run-sourced event
        is what ends a wait on the record. The bare FakeOrch emits nothing."""

        async def run_task(self, task):
            await self.store.save_events(task.id, [{
                "source": "orchestrator", "kind": "repo_config",
                "text": "applying the repo's .no_human.yml", "ts": time.time(),
            }])
            return await super().run_task(task)

    fake = _EmittingOrch(store, hold=hold)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    first = Task.new("already running", repo_path="/tmp/x")
    await store.create_task(first)
    started = await sched.tick()
    assert started == [first.id]

    resumed = Task.new("resumed WIP", repo_path="/tmp/x")
    await store.create_task(resumed)
    await store.set_status(resumed, TaskStatus.IMPLEMENTING, validate=False)

    for _ in range(3):
        await sched.tick()

    waits = [e for e in await store.list_events(resumed.id)
             if e["kind"] == "waiting_for_slot"]
    assert len(waits) == 1, waits
    assert "1/1 busy" in waits[0]["text"]
    assert await store.tasks_waiting_for_slot() == {resumed.id}

    hold.set()
    await sched.wait_idle()

    assert resumed.id in await sched.tick()
    # Dispatched: the worker's first event ends the wait on the record,
    # without any attempt_start. Wait for that observable, bounded.
    await _wait_until(lambda: _has_event_kind(store, resumed.id, "repo_config"))
    assert resumed.id not in await store.tasks_waiting_for_slot()


async def test_a_free_slot_does_not_forget_tasks_that_are_still_waiting(store):
    """Review round 2 of PR #525 (defect 3): the bookkeeping cleared the whole
    dedupe set whenever the pool was not full, so a tick that left a slot
    free while a task still waited (a `_shipped_before_dispatch` skip) made
    the NEXT full tick emit a second event for the same, continuous wait."""
    hold = asyncio.Event()
    fake = FakeOrch(store, hold=hold)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    running = Task.new("running", repo_path="/tmp/x")
    await store.create_task(running)
    assert await sched.tick() == [running.id]
    waiter = Task.new("still waiting", repo_path="/tmp/x")
    await store.create_task(waiter)
    await store.set_status(waiter, TaskStatus.IMPLEMENTING, validate=False)
    await sched.tick()
    assert len([e for e in await store.list_events(waiter.id)
                if e["kind"] == "waiting_for_slot"]) == 1

    # Simulate a tick that observed a free slot but did not start the waiter
    # (the skip shape): the bookkeeping must keep the waiter's open wait.
    sched._inflight.discard(running.id)
    await sched._note_slot_waits([waiter], started=[])
    sched._inflight.add(running.id)
    await sched.tick()
    waits = [e for e in await store.list_events(waiter.id)
             if e["kind"] == "waiting_for_slot"]
    assert len(waits) == 1, f"a continuous wait must stay one event, got {waits}"


async def test_the_scheduler_claim_statuses_are_the_slot_wait_set():
    """The scheduler's own claim-status set and `slot_wait`'s copy must be the
    same set of runtime VALUES, not merely typed to look alike by eye (review
    follow-up on PR #525's `CLAIMABLE_STATUSES` widening)."""
    assert scheduler_mod.CLAIM_STATUS_VALUES == slot_wait.CLAIMABLE_STATUSES


async def test_a_correcting_planning_task_gets_a_wait_event_behind_a_full_pool(store):
    """A plan-approval correction resumes into PLANNING, not IMPLEMENTING
    (`_CORRECTION_CLAIMABLE`) — behind a full pool it gets the same
    'waiting for a worker slot' treatment as a resumed IMPLEMENTING task, and
    `Store.tasks_waiting_for_slot()` counts it, while a genuinely live (non-
    correcting) PLANNING run is never claimed/waited on at all."""
    from no_human.core import plan_gate

    hold = asyncio.Event()
    fake = FakeOrch(store, hold=hold)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    running = Task.new("running", repo_path="/tmp/x")
    await store.create_task(running)
    assert await sched.tick() == [running.id]

    corrected = Task.new("plan correction", repo_path="/tmp/x")
    corrected.context = {plan_gate.CONTEXT_KEY: {
        "state": plan_gate.STATE_CORRECTING,
        "correction": "fix the config path",
        "corrected_at": "2026-08-20T00:00:00+00:00",
        "approved_at": None,
    }}
    await store.create_task(corrected)
    await store.set_status(corrected, TaskStatus.PLANNING, validate=False)
    assert plan_gate.correcting(corrected)

    live_planning = Task.new("live plan run", repo_path="/tmp/x")
    await store.create_task(live_planning)
    await store.set_status(live_planning, TaskStatus.PLANNING, validate=False)

    for _ in range(3):
        await sched.tick()

    waits = [e for e in await store.list_events(corrected.id)
             if e["kind"] == "waiting_for_slot"]
    assert len(waits) == 1, f"expected one wait event, got {waits}"
    assert corrected.id in await store.tasks_waiting_for_slot()
    assert await store.list_events(live_planning.id) == [], (
        "a live (non-correcting) PLANNING task must not be claimed/waited on")

    hold.set()
    await sched.wait_idle()

    started = await sched.tick()
    assert corrected.id in started
    assert corrected.id not in sched._waiting_for_slot


async def test_a_cooldown_does_not_carry_the_open_wait_across_the_pause(store):
    """A wait open when a quota cooldown starts must not silently persist
    across the pause: the in-process dedupe set is cleared so the first
    post-pause tick re-emits a fresh wait event, not silence (review follow-up
    on PR #525's cooldown early-return)."""
    hold = asyncio.Event()
    fake = FakeOrch(store, hold=hold)
    sched = Scheduler(store, lambda task=None: fake, max_workers=1)

    running = Task.new("running", repo_path="/tmp/x")
    await store.create_task(running)
    t0 = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert await sched.tick(now=t0) == [running.id]

    waiter = Task.new("waiter", repo_path="/tmp/x")
    await store.create_task(waiter)
    await store.set_status(waiter, TaskStatus.IMPLEMENTING, validate=False)
    await sched.tick(now=t0)
    waits = [e for e in await store.list_events(waiter.id)
             if e["kind"] == "waiting_for_slot"]
    assert len(waits) == 1
    assert waiter.id in sched._waiting_for_slot

    sched._quota_cooldown_until = t0 + timedelta(minutes=5)
    t1 = t0 + timedelta(minutes=1)
    result = await sched.tick(now=t1)
    assert result == []
    assert waiter.id not in sched._waiting_for_slot
    waits_during = [e for e in await store.list_events(waiter.id)
                    if e["kind"] == "waiting_for_slot"]
    assert len(waits_during) == 1, "no new wait event while paused"

    t2 = t0 + timedelta(minutes=6)
    await sched.tick(now=t2)
    waits_after = [e for e in await store.list_events(waiter.id)
                   if e["kind"] == "waiting_for_slot"]
    assert len(waits_after) == 2, (
        f"expected a fresh wait event once the pause lapsed, got {waits_after}")

    hold.set()
    await sched.wait_idle()


# --------------------------------------------------------------------------- #
# `--until-empty` fail-closed on a non-live orphanable row (follow-up on      #
# PR #585 / task e037008e's `_row_is_live` — MEDIUM-1)                        #
# --------------------------------------------------------------------------- #


async def test_until_empty_refuses_to_report_drained_on_a_young_mid_run_orphan(store):
    """A REVIEWING row with a fresh `updated_at` (inside the 900s grace) and
    no worker attached in THIS process is a crash orphan or a live sibling's
    row — either way `_row_is_live` correctly leaves it alone, but that must
    not read as "queue drained". `run_forever` must still RETURN (no hang):
    the fix is refuse-and-say-why, not wait-it-out."""
    stranded = Task.new("mid-run orphan", repo_path="/tmp/x")
    await store.create_task(stranded)
    await store.set_status(stranded, TaskStatus.REVIEWING, validate=False)

    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=2)
    stop = asyncio.Event()

    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True), 10.0)

    assert stop.is_set(), "run_forever must still return, not hang"
    assert await sched.queue_is_drained() is False, (
        "a non-live orphanable row must not read as a drained queue")
    assert len(sched.drain_blocked_by) == 1, (
        f"expected exactly the one stranded row, got {sched.drain_blocked_by}")
    row = sched.drain_blocked_by[0]
    assert row["task_id"] == stranded.id
    assert row["status"] == "reviewing"
    assert 0 < row["seconds_until_claimable"] <= Scheduler._STRANDED_GRACE_S, (
        f"expected a bound inside the grace window, got {row}")


async def test_the_drain_block_names_the_row_and_the_seconds_until_it_is_claimable(
        store, caplog):
    """The operator-visible signal (log + on_event) must name the specific
    row and a seconds figure — not just say 'not drained'."""
    import logging

    stranded = Task.new("mid-run orphan", repo_path="/tmp/x")
    await store.create_task(stranded)
    await store.set_status(stranded, TaskStatus.TESTING, validate=False)

    events: list[tuple[str, str]] = []
    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=2,
                       on_event=lambda k, t: events.append((k, t)))
    stop = asyncio.Event()

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(
            sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True),
            10.0)

    short_id = stranded.id[:8]
    blocked = [e for e in events if e[0] == "drain_blocked"]
    assert blocked, f"no drain_blocked event emitted; got {events}"
    text = blocked[0][1]
    assert short_id in text
    assert "testing" in text
    assert any(c.isdigit() for c in text), f"no seconds figure in: {text!r}"

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(short_id in m and "testing" in m for m in warnings), (
        f"log did not name the row: {warnings}")
    assert any(any(c.isdigit() for c in m) for m in warnings), (
        f"log did not carry a seconds figure: {warnings}")
    assert any("not drained" in m.lower() for m in warnings), (
        f"log must say the queue is NOT drained, not just 'wait': {warnings}")


async def test_until_empty_still_drains_and_exits_with_no_stranded_row(store):
    """Negative control: with no non-live orphanable row, --until-empty
    drains and exits exactly as before this fix."""
    fake = FakeOrch(store)
    sched = Scheduler(store, lambda task=None: fake, max_workers=2)
    await _mk_tasks(store, 3)
    stop = asyncio.Event()

    await asyncio.wait_for(
        sched.run_forever(stop=stop, poll_interval=0.01, until_empty=True), 10.0)

    assert len(fake.started) == 3
    assert await sched.queue_is_drained() is True
    assert sched.drain_blocked_by == []


async def test_a_mid_run_row_this_process_owns_is_not_counted_as_stranded(store):
    """A mid-run row THIS process dispatched (tracked in `_inflight`) is not
    an unknown — `queue_is_drained` already counts `_inflight` separately,
    so `unclaimable_orphans` must not double-count it."""
    owned = Task.new("owned mid-run", repo_path="/tmp/x")
    await store.create_task(owned)
    await store.set_status(owned, TaskStatus.REVIEWING, validate=False)

    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=2)
    sched._inflight.add(owned.id)

    assert await sched.unclaimable_orphans() == []


async def test_a_plan_correction_row_is_not_counted_as_stranded(store):
    """A plan-approval correction (`plan_gate.correcting`) sitting in
    PLANNING is claimable work (`_claimable` picks it up), not an unknown
    orphan — it must not be double-counted by `unclaimable_orphans`."""
    from no_human.core import plan_gate

    corrected = Task.new("plan correction", repo_path="/tmp/x")
    corrected.context = {plan_gate.CONTEXT_KEY: {
        "state": plan_gate.STATE_CORRECTING,
        "correction": "fix the config path",
        "corrected_at": "2026-08-20T00:00:00+00:00",
        "approved_at": None,
    }}
    await store.create_task(corrected)
    await store.set_status(corrected, TaskStatus.PLANNING, validate=False)
    assert plan_gate.correcting(corrected)

    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=2)
    assert await sched.unclaimable_orphans() == []
    claimable_ids = [t.id for t in await sched._claimable()]
    assert corrected.id in claimable_ids, (
        "a correction must still be claimed through `_claimable`, unchanged")


async def test_row_is_live_and_the_grace_are_unchanged_by_the_drain_gate(store):
    """AC3: this fix must not touch `_row_is_live` or `_STRANDED_GRACE_S` —
    they behave exactly as PR #585 left them."""
    assert Scheduler._STRANDED_GRACE_S == 900.0

    fresh = Task.new("fresh mid-run", repo_path="/tmp/x")
    await store.create_task(fresh)
    await store.set_status(fresh, TaskStatus.REVIEWING, validate=False)

    aged = Task.new("aged mid-run", repo_path="/tmp/x")
    await store.create_task(aged)
    await store.set_status(aged, TaskStatus.REVIEWING, validate=False)
    await _age_row(store, aged.id, seconds=Scheduler._STRANDED_GRACE_S + 60)
    aged = await store.find_task(aged.id)  # re-read: `_age_row` back-dates the
                                            # DB row, not this in-memory object

    sched = Scheduler(store, lambda task=None: FakeOrch(store), max_workers=2)
    assert await sched._row_is_live(fresh) is True
    assert await sched._row_is_live(aged) is False
