"""A server stop is a REQUEUE — not a kill, and not a human pause.

Measured 2026-08-20 on the live DB: 342 ``interrupted`` attempt rows, 254 of
them with no commit, 25 with 20+ coder turns and no commit. `nh stop`
SIGKILLed after 30 s while `Scheduler.drain()` waited for whole attempts to
finish; the coder's uncommitted edits sat in a worktree the task's next run
reaped, and `_ORPHANABLE` never stamped a checkpoint for IMPLEMENTING. Every
restart while coding re-ran the coder from base.

Every test here pins one half of the sentence above. Fixtures (`store`,
`bare_repo`, `_git`, `_incident_result`, `_run_one_attempt`) come from
tests/test_infra_not_work.py so the orchestrator/store/repo shape is the one
the live pause path is already tested against.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from no_human.blockers import (
    MACHINE_REQUEUE_PROVENANCE,
    SERVER_STOP_REASON,
    resume_provenance,
)
from no_human.core.task import TaskStatus

from tests.test_infra_not_work import (  # noqa: F401 — fixtures re-exported on purpose
    _git,
    _incident_result,
    _run_one_attempt,
    bare_repo,
)


def _head(cwd) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


# --------------------------------------------------------------------------- #
# Task 1 — constants + the already-satisfied gate                              #
# --------------------------------------------------------------------------- #

def test_server_stop_is_a_machine_requeue_provenance():
    assert SERVER_STOP_REASON == "__server_stop__"
    assert {"orphan_recovery", "server_stop"} <= set(MACHINE_REQUEUE_PROVENANCE)
    assert "human" not in MACHINE_REQUEUE_PROVENANCE
    assert "wake" not in MACHINE_REQUEUE_PROVENANCE
    # `consumed_human` is a HUMAN-provenance value for the credit question
    # (see `is_human_provenance`), not a machine one — it must not be added
    # here. See tests/test_human_gate_consume_once.py for the gate-armed vs
    # credited distinction this constant is not part of.
    assert "consumed_human" not in MACHINE_REQUEUE_PROVENANCE


async def test_already_satisfied_gate_treats_server_stop_like_orphan_recovery(
        store, bare_repo, tmp_path):
    """A zero-diff claim on top of an UNREVIEWED WIP diff must go to a full
    review whether the interrupted run was killed (orphan_recovery) or
    stopped gracefully (server_stop). Same seam, same gate."""
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    base = repo.head_sha()
    _git(bare_repo, "checkout", "-q", "-b", "no-human/work")
    (bare_repo / "work.py").write_text("work\n")
    _git(bare_repo, "add", "work.py")
    _git(bare_repo, "commit", "-qm", "[WIP-PARTIAL] unreviewed")
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(
            {"sha": _head(bare_repo), "branch": "no-human/work"}, "server_stop")})

    eligible, why = orch._already_satisfied_eligible(task, repo, base)

    assert eligible is False and why


async def test_already_satisfied_gate_still_ignores_a_wake_resume(
        store, bare_repo, tmp_path):
    """Control: the gate is scoped to machine REQUEUES. A wake resume never
    had a review in flight to interrupt (D15) and stays eligible."""
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    base = repo.head_sha()
    _git(bare_repo, "checkout", "-q", "-b", "no-human/work")
    (bare_repo / "work.py").write_text("work\n")
    _git(bare_repo, "add", "work.py")
    _git(bare_repo, "commit", "-qm", "[WIP-PARTIAL] reviewed elsewhere")
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(
            {"sha": _head(bare_repo), "branch": "no-human/work"}, "wake")})

    eligible, _why = orch._already_satisfied_eligible(task, repo, base)

    assert eligible is True


# --------------------------------------------------------------------------- #
# Task 2 — the store records the reason a row was closed for                  #
# --------------------------------------------------------------------------- #

async def test_close_open_attempts_records_the_callers_reason(store):
    from no_human.core.task import Task
    task = Task.new("do a thing", repo_path="/tmp/x")
    await store.create_task(task)
    aid = await store.create_attempt(task.id, 1)

    await store.close_open_attempts(
        task.id, reason="interrupted: server stopping — checkpointed abc12345")

    row = next(r for r in await store.list_attempts(task.id) if r["id"] == aid)
    assert row["status"] == "interrupted"
    assert row["failure_reason"].startswith("interrupted: server stopping")


async def test_close_open_attempts_default_reason_is_unchanged(store):
    """Control: the four checkpoint-clearing callers keep their wording."""
    from no_human.core.task import Task
    task = Task.new("do a thing", repo_path="/tmp/x")
    await store.create_task(task)
    aid = await store.create_attempt(task.id, 1)

    await store.close_open_attempts(task.id)

    row = next(r for r in await store.list_attempts(task.id) if r["id"] == aid)
    assert "checkpoint was cleared for a fresh run from base" in row["failure_reason"]


# --------------------------------------------------------------------------- #
# Task 3 — the orchestrator honours a server stop as a requeue                #
# --------------------------------------------------------------------------- #

async def _implementing(store, task):
    """Walk the task to IMPLEMENTING the way `_run_attempt` would have."""
    await store.set_status(task, TaskStatus.IMPLEMENTING)
    return await store.get_task(task.id)


async def test_server_stop_checkpoints_and_requeues_instead_of_parking(
        store, bare_repo, tmp_path):
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task = await _implementing(store, task)
    base = repo.head_sha()
    _git(bare_repo, "checkout", "-q", "-b", "no-human/live")
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    aid = await store.create_attempt(task.id, 2)
    orch.request_server_stop()

    outcome = await orch._honor_cancel(
        task, repo, "no-human/live", SERVER_STOP_REASON, attempt_id=aid)

    fresh = await store.get_task(task.id)
    rf = fresh.context["resume_from"]
    assert outcome.status == TaskStatus.IMPLEMENTING
    assert fresh.status == TaskStatus.IMPLEMENTING, "a stop is a requeue, not a pause"
    assert rf["by"] == "server_stop"
    assert rf["sha"] != base and rf["branch"] == "no-human/live"
    assert _head(bare_repo) == rf["sha"], "the WIP commit is what was stamped"
    assert fresh.context["handoff"]["wip_sha"] == rf["sha"]
    row = next(r for r in await store.list_attempts(task.id) if r["id"] == aid)
    assert row["status"] == "interrupted" and row["infra_failure"] == 1
    assert "server stopping" in row["failure_reason"]
    assert row["commit_sha"] == rf["sha"]
    assert (fresh.blocker or {}).get("category") != "USER_PAUSED"
    assert fresh.wake_check_at is None
    assert await store.get_cancel_request(task.id) is None


async def test_server_stop_with_a_clean_tree_stamps_head(
        store, bare_repo, tmp_path):
    """Nothing uncommitted: HEAD on the task branch is the checkpoint."""
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task = await _implementing(store, task)
    _git(bare_repo, "checkout", "-q", "-b", "no-human/live")
    (bare_repo / "work.py").write_text("work\n")
    _git(bare_repo, "add", "work.py")
    _git(bare_repo, "commit", "-qm", "[WIP-PARTIAL] committed already")
    head = _head(bare_repo)
    aid = await store.create_attempt(task.id, 1)
    orch.request_server_stop()

    await orch._honor_cancel(task, repo, "no-human/live", SERVER_STOP_REASON,
                             attempt_id=aid)

    fresh = await store.get_task(task.id)
    assert fresh.context["resume_from"] == {
        "sha": head, "branch": "no-human/live", "by": "server_stop"}
    assert fresh.status == TaskStatus.IMPLEMENTING


async def test_server_stop_at_a_cheap_boundary_closes_every_open_row(
        store, bare_repo, tmp_path):
    """`_drive` entry / between attempts: no attempt id in hand, no session
    open, nothing uncommitted. There is no in-flight work to preserve, so
    NOTHING is stamped — the checkout may sit at base (`_drive` entry) or
    at a review-REJECTED tip (loop top), and stamping either would move the
    next run's branch point for no gain. The prior record stands untouched
    and the dead rows are closed with the stop's own reason."""
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task = await _implementing(store, task)
    _git(bare_repo, "checkout", "-q", "-b", "no-human/work")
    (bare_repo / "work.py").write_text("work\n")
    _git(bare_repo, "add", "work.py")
    _git(bare_repo, "commit", "-qm", "[WIP-PARTIAL] real work")
    work_sha = _head(bare_repo)
    _git(bare_repo, "checkout", "-q", "main")
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(
            {"sha": work_sha, "branch": "no-human/work"}, "orphan_recovery")})
    a1 = await store.create_attempt(task.id, 1)
    orch.request_server_stop()

    await orch._honor_cancel(task, repo, None, SERVER_STOP_REASON)

    fresh = await store.get_task(task.id)
    assert fresh.context["resume_from"] == {
        "sha": work_sha, "branch": "no-human/work", "by": "orphan_recovery"}, (
        "a cheap boundary must not re-stamp a record it has nothing to add to")
    row = next(r for r in await store.list_attempts(task.id) if r["id"] == a1)
    assert row["status"] == "interrupted" and "server stopping" in row["failure_reason"]
    assert "nothing to checkpoint" in row["failure_reason"]


async def test_server_stop_at_the_loop_top_does_not_stamp_a_rejected_tip(
        store, bare_repo, tmp_path):
    """Loop-top boundary after a review FAIL: the checkout sits on attempt
    N's branch at the rejected commit. In-process, attempt N+1 would not
    branch from it (a review failure writes no handoff); a stop must not
    make the next server do so either."""
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task = await _implementing(store, task)
    _git(bare_repo, "checkout", "-q", "-b", "no-human/attempt-1")
    (bare_repo / "rejected.py").write_text("rejected\n")
    _git(bare_repo, "add", "rejected.py")
    _git(bare_repo, "commit", "-qm", "attempt 1 — review FAILED")
    orch.request_server_stop()

    await orch._honor_cancel(task, repo, "no-human/attempt-1", SERVER_STOP_REASON)

    fresh = await store.get_task(task.id)
    assert "resume_from" not in (fresh.context or {})
    assert "handoff" not in (fresh.context or {})


async def test_server_stop_during_planning_leaves_a_claimable_row(
        store, bare_repo, tmp_path):
    """The first loop-top boundary is reached with the task still PLANNING
    (the planner session is not interruptible). PLANNING is not claimable;
    the stop flips it to IMPLEMENTING exactly as the orphan sweep would,
    so the next server claims it without calling it a crash."""
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    assert task.status == TaskStatus.PLANNING
    orch.request_server_stop()

    outcome = await orch._honor_cancel(task, repo, None, SERVER_STOP_REASON)

    fresh = await store.get_task(task.id)
    assert fresh.status == TaskStatus.IMPLEMENTING
    assert outcome.status == TaskStatus.IMPLEMENTING


async def test_server_stop_never_overwrites_a_humans_resume_from(
        store, bare_repo, tmp_path):
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task = await _implementing(store, task)
    human = resume_provenance({"sha": "a" * 40, "branch": "no-human/h"}, "human")
    task.context = await store.merge_context(task.id, {"resume_from": human})
    _git(bare_repo, "checkout", "-q", "-b", "no-human/live")
    (bare_repo / "calc.py").write_text("x = 1\n")
    aid = await store.create_attempt(task.id, 1)
    orch.request_server_stop()

    await orch._honor_cancel(task, repo, "no-human/live", SERVER_STOP_REASON,
                             attempt_id=aid)

    fresh = await store.get_task(task.id)
    assert fresh.context["resume_from"]["by"] == "human"
    assert fresh.context["resume_from"]["sha"] == "a" * 40
    assert not repo.has_changes(), "the work is still committed, just not stamped over a human's gate"
    # Not resumed onto — but never unfindable: the row names the commit.
    row = max(await store.list_attempts(task.id), key=lambda r: r["attempt_number"])
    assert row["commit_sha"] == _head(bare_repo)
    assert "human-gated" in row["failure_reason"]


async def test_human_pause_is_unchanged_by_the_server_stop_branch(
        store, bare_repo, tmp_path):
    """Regression pin: the operator's pause still parks USER_PAUSED, and a
    process that is stopping does not turn a human's pause into a requeue."""
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    orch.request_server_stop()

    outcome = await orch._honor_cancel(task, repo, None, "Paused from board")

    assert outcome.status == TaskStatus.BLOCKED
    assert (await store.get_task(task.id)).blocker["category"] == "USER_PAUSED"


async def test_pending_cancel_reports_a_server_stop_at_cheap_boundaries(
        store, bare_repo, tmp_path):
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    assert await orch._pending_cancel(task) is None
    orch.request_server_stop()
    assert await orch._pending_cancel(task) == SERVER_STOP_REASON
    assert await store.get_cancel_request(task.id) is None, "never written to the DB"


async def test_a_human_cancel_outranks_the_server_stop_at_cheap_boundaries(
        store, bare_repo, tmp_path):
    """The operator's pause is the richer record: it parks with their reason."""
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    await store.request_cancel(task.id, "Paused from board")
    orch.request_server_stop()
    assert await orch._pending_cancel(task) == "Paused from board"


async def test_request_server_stop_interrupts_only_the_active_session(
        store, bare_repo, tmp_path):
    from no_human.agent.backend import AgentEvent
    from no_human.core.orchestrator import CancelRequested
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    orch._active_task_id = task.id
    orch.request_server_stop()
    with pytest.raises(CancelRequested) as exc:
        orch._agent_sink(AgentEvent(kind="tool_use", text="ls", tool_name="Bash",
                                    tool_input={"command": "ls"}))
    assert str(exc.value) == SERVER_STOP_REASON


async def test_a_session_that_starts_after_the_stop_is_still_interrupted(
        store, bare_repo, tmp_path):
    """`_active_task_id` is assigned after create_attempt; a stop in that
    window primes no `_cancel_reason`. The sink therefore reads the flag
    itself, so the first coder event of a late-starting session unwinds it
    — otherwise the whole attempt, review and PR would run into the drain."""
    from no_human.agent.backend import AgentEvent
    from no_human.core.orchestrator import CancelRequested
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    orch._active_task_id = None
    orch.request_server_stop()
    assert orch._cancel_reason is None
    orch._active_task_id = task.id          # the session starts now
    with pytest.raises(CancelRequested) as exc:
        orch._agent_sink(AgentEvent(kind="text", text="hello"))
    assert str(exc.value) == SERVER_STOP_REASON


async def test_a_session_with_no_active_attempt_is_not_interrupted(
        store, bare_repo, tmp_path):
    """`_run_code_review`'s read-only diff-fetch fallback streams through the
    sink under the coder role, sets no `_active_task_id` and has no
    CancelRequested handler: raising there crashed a code_review task to
    FAILED (review round 2). It finishes under the drain grace instead."""
    from no_human.agent.backend import AgentEvent
    orch, _backend, _task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    orch._active_task_id = None
    orch.request_server_stop()
    orch._agent_sink(AgentEvent(kind="tool_use", text="gh pr diff", tool_name="Bash",
                                tool_input={"command": "gh pr diff"}))   # must not raise


def test_every_coder_sink_session_has_a_stated_stop_disposition():
    """Which `backend.run` sites stream through `_agent_sink` under the coder
    role is an enumerated set, not a grep: a new one must decide whether a
    server stop may raise inside it (it has a CancelRequested handler) or
    must be left to the drain grace (it sets no active attempt). The entries
    here ARE that decision; the AST walk keeps them honest."""
    import ast
    from pathlib import Path
    import no_human.core.orchestrator as mod
    tree = ast.parse(Path(mod.__file__).read_text())
    sites: dict[str, int] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run"
                    and ast.unparse(node.func.value) == "self.backend"
                    and any(k.arg == "on_event"
                            and ast.unparse(k.value) == "self._agent_sink"
                            for k in node.keywords)):
                sites[fn.name] = sites.get(fn.name, 0) + 1
    expected = {
        # the coder session: CancelRequested caught, _honor_cancel(attempt_id=)
        "_run_attempt": 1,
        # the zero-diff reformat nudge: same catch, same attempt_id
        "_reformat_nudge": 1,
        # the repro-gate corrective round (PR #533): CancelRequested is
        # re-raised out of the round, caught by _run_attempt's gate-step
        # handler, and routed to _honor_cancel(attempt_id=) — mid-session
        # shape, so the round's partial work is checkpointed [WIP-PARTIAL]
        "_repro_corrective_round": 1,
        # the preflight plan, inside _run_attempt after _active_task_id is set:
        # its except swallows the raise and the coder session honours the stop
        "_maybe_preflight": 1,
        # the code_review diff-fetch fallback: NO active attempt, NO handler —
        # the sink's active-attempt gate keeps the stop out; the grace covers it
        "_run_code_review": 1,
    }
    assert sites == expected, (
        "a backend session streams through the coder sink with no stated "
        f"server-stop disposition: {sites}")


async def test_a_non_coder_session_is_never_interrupted_by_the_stop(
        store, bare_repo, tmp_path):
    """Negative control: sessions that PASS a role (planner, reviewer,
    aggregator via `_sink_for`) are deliberately not interruptible — they
    are read-only and cheap to finish; the grace covers them, the docstring
    says so."""
    from no_human.agent.backend import AgentEvent
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    orch._active_task_id = task.id
    orch.request_server_stop()
    orch._agent_sink(AgentEvent(kind="text", text="plan"), role="planner")   # must not raise


# --------------------------------------------------------------------------- #
# Task 4 — the scheduler signals in-flight orchestrators, bounds the drain    #
# --------------------------------------------------------------------------- #

from types import SimpleNamespace  # noqa: E402

from no_human.core.scheduler import Scheduler  # noqa: E402
from no_human.core.task import Task  # noqa: E402


async def _pending_tasks(store, n):
    ids = []
    for i in range(n):
        t = Task.new(f"task {i}", repo_path="/tmp/x")
        await store.create_task(t)
        ids.append(t.id)
    return ids


class _StoppableOrch:
    """Blocks in run_task until request_server_stop() releases it — the shape
    of a coder session that unwinds at its next tool boundary."""

    instances: list["_StoppableOrch"] = []

    def __init__(self, task):
        self.task = task
        self._sink = None
        self.release = asyncio.Event()
        self.stopped = False
        _StoppableOrch.instances.append(self)

    def request_server_stop(self):
        self.stopped = True
        self.release.set()

    async def run_task(self, task):
        await self.release.wait()
        return SimpleNamespace(status=TaskStatus.IMPLEMENTING, task=task)


class _WedgedOrch:
    """Never returns — a session stuck inside one long tool call."""

    def __init__(self, task):
        self.task = task
        self._sink = None
        self.stop_calls = 0

    def request_server_stop(self):
        self.stop_calls += 1

    async def run_task(self, task):
        await asyncio.sleep(3600)


class _HooklessOrch:
    """A factory that returns something without the hook (a foreign runner)."""

    def __init__(self, task):
        self.task = task
        self._sink = None
        self.release = asyncio.Event()

    async def run_task(self, task):
        await self.release.wait()
        return SimpleNamespace(status=TaskStatus.IMPLEMENTING, task=task)


async def test_run_forever_stop_signals_every_inflight_orchestrator(store):
    _StoppableOrch.instances.clear()
    sched = Scheduler(store, _StoppableOrch, max_workers=2)
    ids = await _pending_tasks(store, 2)
    stop = asyncio.Event()
    runner = asyncio.create_task(sched.run_forever(stop=stop, poll_interval=0.05))
    for _ in range(100):
        if len(sched._running) == 2:
            break
        await asyncio.sleep(0.02)
    assert sorted(sched._running) == sorted(ids)

    stop.set()
    await asyncio.wait_for(runner, timeout=5)

    assert all(o.stopped for o in _StoppableOrch.instances)
    assert len(_StoppableOrch.instances) == 2
    assert sched._running == {} and sched._inflight == set()


async def test_drain_grace_is_bounded_and_reported(store, caplog):
    sched = Scheduler(store, _WedgedOrch, max_workers=1,
                      config={"concurrency": {"stop_grace_s": 0.3}})
    await _pending_tasks(store, 1)
    stop = asyncio.Event()
    runner = asyncio.create_task(sched.run_forever(stop=stop, poll_interval=0.05))
    for _ in range(100):
        if sched._running:
            break
        await asyncio.sleep(0.02)
    stop.set()

    await asyncio.wait_for(runner, timeout=5)     # returns despite the wedge

    assert any("still running after" in r.getMessage() for r in caplog.records)
    runner_task = next(iter(sched._running.values()))
    assert runner_task.stop_calls == 1
    # The wedged coroutine is still alive; cancel it so the loop closes clean.
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task() and not t.done():
            t.cancel()


async def test_drain_returns_true_when_everything_finished(store):
    sched = Scheduler(store, _StoppableOrch, max_workers=1)
    assert await sched.drain() is True


async def test_stop_grace_default_is_sixty_seconds(store):
    sched = Scheduler(store, _StoppableOrch, max_workers=1)
    assert sched._stop_grace_s == 60.0


async def test_stop_with_nothing_inflight_signals_nothing(store):
    sched = Scheduler(store, _StoppableOrch, max_workers=1)
    events = []
    sched._on_event = lambda kind, text: events.append(kind)
    assert sched.request_stop_checkpoints() == 0
    assert "server_stop" not in events


async def test_a_hookless_orchestrator_is_skipped_not_crashed(store):
    sched = Scheduler(store, _HooklessOrch, max_workers=1,
                      config={"concurrency": {"stop_grace_s": 0.3}})
    await _pending_tasks(store, 1)
    stop = asyncio.Event()
    runner = asyncio.create_task(sched.run_forever(stop=stop, poll_interval=0.05))
    for _ in range(100):
        if sched._running:
            break
        await asyncio.sleep(0.02)
    assert sched.request_stop_checkpoints() == 0
    next(iter(sched._running.values())).release.set()
    stop.set()
    await asyncio.wait_for(runner, timeout=5)


# --------------------------------------------------------------------------- #
# Task 4b — one grace number, three readers                                    #
# --------------------------------------------------------------------------- #

def test_stop_grace_is_read_from_one_place():
    from no_human.core.scheduler import (LIFESPAN_DRAIN_MARGIN_S,
                                         STOP_COMMAND_MARGIN_S, stop_grace_s)
    assert stop_grace_s({}) == 60.0
    assert stop_grace_s(None) == 60.0
    assert stop_grace_s({"concurrency": {"stop_grace_s": 12}}) == 12.0
    assert stop_grace_s({"concurrency": {"stop_grace_s": "bogus"}}) == 60.0
    assert stop_grace_s({"concurrency": {"stop_grace_s": -5}}) == 0.0
    # The outer waits must outlast the inner one, or the kill lands first.
    assert LIFESPAN_DRAIN_MARGIN_S > 0 and STOP_COMMAND_MARGIN_S > LIFESPAN_DRAIN_MARGIN_S


def test_nh_stop_default_timeout_outlasts_the_lifespan_drain():
    from no_human.cli.commands import _default_stop_timeout
    from no_human.core.scheduler import (LIFESPAN_DRAIN_MARGIN_S,
                                         STOP_COMMAND_MARGIN_S, stop_grace_s)
    cfg = {"concurrency": {"stop_grace_s": 20}}
    assert _default_stop_timeout(cfg) == stop_grace_s(cfg) + STOP_COMMAND_MARGIN_S
    assert _default_stop_timeout(cfg) > stop_grace_s(cfg) + LIFESPAN_DRAIN_MARGIN_S


# --------------------------------------------------------------------------- #
# The real seam, end to end                                                    #
# --------------------------------------------------------------------------- #

async def test_a_stop_mid_session_closes_the_row_with_its_spend_and_requeues(
        store, bare_repo, tmp_path):
    """Drives the whole path: the backend streams usage, the scheduler-side
    stop lands, the next coder event raises out of the sink, `_run_attempt`
    catches it and hands the attempt id to the honour path. A mutation that
    drops `attempt_id=` at that catch site leaves the row closed by
    `close_open_attempts` instead — no spend, no infra_failure — which is
    exactly what the assertions below refuse."""
    from no_human.agent.backend import AgentEvent
    from no_human.config import load_config
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier
    from no_human.vcs import GitRepo

    class _StoppingBackend:
        def __init__(self):
            self.calls = 0
            self.orch = None

        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            self.calls += 1
            on_event(AgentEvent(kind="usage", meta={
                "tokens_used": 1234, "cache_read_tokens": 50,
                "cache_creation_tokens": 7, "output_tokens": 11,
                "message_id": "m1"}))
            (cwd / "calc.py").write_text("def add(a, b):\n    return b + a\n")
            self.orch.request_server_stop()          # the scheduler's stop lands
            on_event(AgentEvent(kind="tool_use", text="ls", tool_name="Bash",
                                tool_input={"command": "ls"}))
            raise AssertionError("the sink must have raised before this line")

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("isolation", {})["enabled"] = False
    backend = _StoppingBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    backend.orch = orch
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    base = GitRepo(bare_repo).head_sha()

    outcome = await orch.run_task(task)

    assert backend.calls == 1, "the backend never ran — the test proves nothing"
    fresh = await store.get_task(task.id)
    rows = await store.list_attempts(task.id)
    assert len(rows) == 1
    row = rows[0]
    assert outcome.status == TaskStatus.IMPLEMENTING and fresh.status == TaskStatus.IMPLEMENTING
    assert row["status"] == "interrupted" and row["infra_failure"] == 1
    assert row["tokens_used"] == 1234, "the attempt's spend must land on its row"
    assert row["commit_sha"] and row["commit_sha"] != base
    assert fresh.context["resume_from"] == {
        "sha": row["commit_sha"], "branch": fresh.context["resume_from"]["branch"],
        "by": "server_stop"}
    assert fresh.context["resume_from"]["branch"]
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, "a stop must not consume a lifetime attempt"
    assert (fresh.blocker or {}).get("category") != "USER_PAUSED"
