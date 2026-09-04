"""Cooperative cancellation: `nh task pause` actually stops the run (M0.2).

`nh task pause` used to flip a DB row that nothing ever read. The attempt loop
never re-read the task, so a paused task kept burning subscription tokens until
it finished on its own — the runaway-cost hazard that ended the last flagship
run. These tests pin the three properties that make the stop real:

1. the flag survives a concurrent `update_task` (dedicated control column),
2. the running agent session unwinds at its next tool boundary,
3. the work is checkpointed as [WIP-BLOCKED], never thrown away.
"""

import asyncio
import subprocess

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.core import orchestrator as orch_mod
from no_human.core.orchestrator import CODER_ROLE, CancelRequested, Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401


def _mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )


# ------------------------- the control column ------------------------------ #


async def test_cancel_flag_round_trips(store):
    task = Task.new("t", repo_path="/r")
    await store.create_task(task)

    assert await store.get_cancel_request(task.id) is None
    await store.request_cancel(task.id, "user paused")
    assert await store.get_cancel_request(task.id) == "user paused"
    await store.clear_cancel_request(task.id)
    assert await store.get_cancel_request(task.id) is None


async def test_update_task_cannot_clobber_a_pending_cancel(store):
    """The reason it is a column and not `task.context`.

    The CLI and the running orchestrator each hold a Task copy, and
    `update_task` rewrites the whole mutable surface from it. A flag stored in
    `context` is silently dropped by whichever writer flushes last — which is
    the orchestrator, i.e. exactly the process the flag is meant to stop.
    """
    task = Task.new("t", repo_path="/r")
    await store.create_task(task)
    stale = await store.find_task(task.id)  # the orchestrator's copy, pre-pause

    await store.request_cancel(task.id, "user paused")  # the CLI pauses

    stale.context = {"attempt_log": ["attempt 1"]}  # orchestrator flushes its copy
    await store.update_task(stale)

    assert await store.get_cancel_request(task.id) == "user paused"

    # And the failure mode the column exists to avoid: the very same flag, put
    # in `context` instead, is silently dropped by that identical flush.
    cli_copy = await store.find_task(task.id)
    cli_copy.context = {**(cli_copy.context or {}), "cancel_requested": "user paused"}
    await store.update_task(cli_copy)

    stale.context = {"attempt_log": ["attempt 2"]}
    await store.update_task(stale)

    survivor = await store.find_task(task.id)
    assert "cancel_requested" not in (survivor.context or {})
    assert await store.get_cancel_request(task.id) == "user paused"


# ------------------------------ the sink ----------------------------------- #


def _orch(store, tmp_path, events=None):
    cfg = _config(tmp_path)
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_mutate), SlackNotifier(None),
        event_sink=(events.append if events is not None else None),
    )
    return orch


def _tool_event():
    return AgentEvent("tool_use", tool_name="Read", tool_input={"file_path": "x.py"})


def test_agent_sink_raises_for_the_running_task(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._cancel_reason = ("task-1", "user paused")
    with pytest.raises(CancelRequested, match="user paused"):
        orch._agent_sink(_tool_event(), role=CODER_ROLE)


def test_agent_sink_ignores_a_cancel_aimed_at_another_task(store, tmp_path):
    """The worker pool reuses one Orchestrator; a stop for task A must not
    abort task B's session."""
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-2"
    orch._cancel_reason = ("task-1", "user paused")
    orch._agent_sink(_tool_event(), role=CODER_ROLE)  # must not raise


@pytest.mark.parametrize("role", ["planner", "reviewer", "aggregator"])
def test_only_the_implementer_session_is_interrupted(store, tmp_path, role):
    """Never interrupt the planner or the reviewer: they are short, read-only,
    and a half-run review is worse than no cancellation."""
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._cancel_reason = ("task-1", "user paused")
    orch._agent_sink(_tool_event(), role=role)  # must not raise


# ------------------------------ the watcher -------------------------------- #


async def test_watcher_promotes_a_db_flag_into_the_in_memory_signal(
    store, tmp_path, monkeypatch
):
    monkeypatch.setattr(orch_mod, "_CANCEL_POLL_SECONDS", 0.01)
    task = Task.new("t", repo_path="/r")
    await store.create_task(task)
    orch = _orch(store, tmp_path)

    watcher = asyncio.create_task(orch._watch_for_cancel(task.id))
    await store.request_cancel(task.id, "user paused")
    await asyncio.wait_for(watcher, timeout=5)

    assert orch._cancel_reason == (task.id, "user paused")


# --------------------------- end to end ------------------------------------ #


class _PauseMidSessionBackend:
    """An agent that does real work, then the operator hits pause mid-session."""

    def __init__(self, store, task_id):
        self.store = store
        self.task_id = task_id

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        _mutate(cwd)  # the agent's edits are on disk, uncommitted
        await self.store.request_cancel(self.task_id, "user paused")
        # Keep working. Each tool call is a cancellation checkpoint; one of them
        # must raise once the watcher has seen the flag.
        for _ in range(500):
            await asyncio.sleep(0.01)
            if on_event:
                on_event(AgentEvent("tool_use", tool_name="Read", tool_input={}))
        raise AssertionError("the session was never interrupted")


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_pause_stops_the_session_and_checkpoints_the_work(
    store, bare_repo, tmp_path, monkeypatch
):
    """The whole point: reverting the raise in `_agent_sink` makes the fake
    backend run to completion and fail with 'the session was never interrupted'.
    """
    monkeypatch.setattr(orch_mod, "_CANCEL_POLL_SECONDS", 0.01)
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(task)

    events = []
    cfg = _config(tmp_path)
    orch = Orchestrator(
        store, cfg.data, _PauseMidSessionBackend(store, task.id),
        SlackNotifier(None), event_sink=events.append,
    )
    # 60s flaked twice under xdist load (isolated runtime is 40-53s); the
    # timeout guards against a hang, not against a slow machine.
    outcome = await asyncio.wait_for(orch.run_task(task), timeout=180)

    assert outcome.status is TaskStatus.BLOCKED
    assert outcome.pr_url is None
    assert "user paused" in outcome.detail

    # the task is parked, not failed, and the flag was consumed
    reloaded = await store.find_task(task.id)
    assert reloaded.status is TaskStatus.BLOCKED
    assert reloaded.blocker["category"] == "USER_PAUSED"

    # The work survived, as a [WIP-BLOCKED] commit on the task's branch. Read
    # the BRANCH, not the checkout: the run happens in its own worktree
    # (isolation.enabled), so the operator's checkout is deliberately still on
    # main at the commit they left it on. The branch is the surviving artifact.
    resume_branch = reloaded.blocker["resume_branch"]
    assert resume_branch, "nothing recorded to resume from"
    head_msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%s", resume_branch], cwd=bare_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "[WIP-BLOCKED]" in head_msg
    committed = subprocess.run(
        ["git", "show", f"{resume_branch}:calc.py"], cwd=bare_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "def mul" in committed
    # ...and the operator's own working tree was never written to.
    assert "def mul" not in (bare_repo / "calc.py").read_text()
    assert await store.get_cancel_request(task.id) is None
    assert [e for e in events if e["kind"] == "cancelled"]


async def test_a_cancel_between_attempts_spends_no_further_tokens(
    store, bare_repo, tmp_path
):
    """A flag raised while the task is queued must stop it before the first
    session opens — the cheapest possible boundary."""
    task = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.request_cancel(task.id, "user paused")

    ran = []

    class _RecordingBackend(FakeBackend):
        async def run(self, *a, **kw):
            ran.append(1)
            return await super().run(*a, **kw)

    cfg = _config(tmp_path)
    orch = Orchestrator(
        store, cfg.data, _RecordingBackend(_mutate), SlackNotifier(None)
    )
    outcome = await orch.run_task(task)

    assert outcome.status is TaskStatus.BLOCKED
    assert ran == [], "no agent session may start once a stop is pending"
    assert await store.get_cancel_request(task.id) is None
