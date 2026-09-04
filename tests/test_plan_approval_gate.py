"""GAP 1 — the optional human plan-approval gate.

A per-task opt-in (`task.config["plan_approval"]`, default OFF) that parks the
task after the plan is generated and before ANY implementation token is spent.
The human approves (resume into implementation) or replies with a correction
(re-plan once with the correction attached, then park again).

The gate reuses the existing blocker parking mechanism: an AMBIGUITY blocker
routed to AWAITING_INPUT by `blockers.triage`, so the board's "Needs Answer"
lane, the drawer's decision panel and both reply paths already render it.
"""

from __future__ import annotations

import subprocess

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch as _patch

from no_human.agent.claude_backend import AgentResult
from no_human.api.app import app
from no_human.config import load_config
from no_human.core import plan_gate
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import ALLOWED_TRANSITIONS, Task, TaskStatus
from no_human.notify.slack import SlackNotifier


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


@pytest_asyncio.fixture
async def client(store, tmp_path):
    app.state.store = store
    app.state.config = load_config(tmp_path / "cfg.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


class CountingCoder:
    """The implementer. Counts every session so a test can assert ZERO spend."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


_PLAN_A = (
    "## FILES TO CHANGE/CREATE\n- calc.py: add mul()\n\n"
    "## APPROACH\nAdd a mul function.\n\n"
    "## TEST PLAN\ntest_mul asserts mul(2,3)==6.\n\n"
    "## OUT OF SCOPE\nDo not rename existing functions.\n\n"
    "## VERIFICATION\npytest -q\n"
)

_PLAN_B = _PLAN_A.replace("Add a mul function.", "Add a mul function via operator.mul.")


class ScriptedPlanner:
    """Returns the next scripted plan and records every prompt it was given."""

    def __init__(self, *plans: str):
        self._plans = list(plans)
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.prompts.append(prompt)
        plan = self._plans[min(len(self.prompts), len(self._plans)) - 1]
        return AgentResult(final_text=plan, num_turns=3, is_error=False,
                           tokens_used=200, session_id="s", stop_reason="end_turn")

    @property
    def plan_prompts(self) -> list[str]:
        return [p for p in self.prompts
                if "You are planning an implementation task" in p]


def _cfg(tmp_path, *, planning=True):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = planning
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    return cfg


def _orch(store, cfg, coder, events=None):
    return Orchestrator(store, cfg.data, coder, SlackNotifier(None),
                        event_sink=(events.append if events is not None else None))


async def _gated_task(store, bare_repo, *, on=True):
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    if on:
        t.config["plan_approval"] = True
    await store.create_task(t)
    return t


# --------------------------------------------------------------------------- #
# 1. Where the gate sits in the transition map (no new state)                  #
# --------------------------------------------------------------------------- #

def test_gate_needs_no_new_state_planning_parks_and_replans():
    """The gate reuses AWAITING_INPUT. Both legs must already be legal:
    PLANNING -> AWAITING_INPUT (park for approval) and AWAITING_INPUT ->
    PLANNING (a correction re-enters planning)."""
    assert TaskStatus.AWAITING_INPUT in ALLOWED_TRANSITIONS[TaskStatus.PLANNING]
    assert TaskStatus.PLANNING in ALLOWED_TRANSITIONS[TaskStatus.AWAITING_INPUT]
    assert TaskStatus.IMPLEMENTING in ALLOWED_TRANSITIONS[TaskStatus.AWAITING_INPUT]


# --------------------------------------------------------------------------- #
# 2. Parks with the plan visible, and spends nothing                           #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_gate_on_parks_with_plan_and_spends_zero_implementation_tokens(
    bare_repo, tmp_path, store,
):
    coder = CountingCoder()
    planner = ScriptedPlanner(_PLAN_A)
    events: list = []
    orch = _orch(store, _cfg(tmp_path), coder, events)
    t = await _gated_task(store, bare_repo)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_INPUT
    # ZERO implementation spend — counted, not read off the code.
    assert coder.calls == 0
    assert await store.list_attempts(t.id) == []
    # No branch was pushed, so nothing was implemented anywhere.
    branches = subprocess.run(["git", "branch", "--list"], cwd=bare_repo,
                              capture_output=True, text=True).stdout
    assert "no-human/" not in branches

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_INPUT
    # The plan is persisted and readable in the drawer (context.plan + spec).
    assert fresh.context["plan"] == _PLAN_A.strip()
    assert fresh.context["spec"]["approach"] == "Add a mul function."
    # Parked through the existing blocker mechanism, so the drawer renders it.
    assert fresh.blocker["category"] == "AMBIGUITY"
    assert "approv" in (fresh.blocker["question"] or "").lower()
    assert _PLAN_A.strip() in fresh.blocker["evidence"]
    labels = [o["label"] for o in fresh.blocker["options"]]
    assert any(o["action"] == {"approve_plan": True} for o in fresh.blocker["options"]), labels
    assert any(e["kind"] == "plan_approval" for e in events)


@pytest.mark.asyncio
async def test_gate_on_parks_even_when_no_plan_was_produced(
    bare_repo, tmp_path, store,
):
    """Planning disabled (or a SKIP_PLAN verdict) must not silently bypass the
    human's gate — it parks and says plainly that no plan was produced."""
    coder = CountingCoder()
    orch = _orch(store, _cfg(tmp_path, planning=False), coder)
    t = await _gated_task(store, bare_repo)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert coder.calls == 0
    fresh = await store.get_task(t.id)
    assert "no plan" in (fresh.blocker["question"] or "").lower()


# --------------------------------------------------------------------------- #
# 3. Flag off = today's path, byte-identical                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_gate_off_runs_straight_through_to_a_pr(bare_repo, tmp_path, store):
    coder = CountingCoder()
    planner = ScriptedPlanner(_PLAN_A)
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo, on=False)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url
    assert coder.calls >= 1
    fresh = await store.get_task(t.id)
    assert "plan_approval" not in fresh.context


# --------------------------------------------------------------------------- #
# 4. Approve resumes into implementation                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_approve_option_resumes_into_implementation(
    bare_repo, tmp_path, store, client,
):
    coder = CountingCoder()
    planner = ScriptedPlanner(_PLAN_A)
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        await orch.run_task(t)
    parked = await store.get_task(t.id)
    approve_idx = 1 + next(
        i for i, o in enumerate(parked.blocker["options"])
        if o["action"] == {"approve_plan": True}
    )

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"choose": approve_idx})
    assert r.status_code == 200, r.text

    approved = await store.get_task(t.id)
    assert approved.status is TaskStatus.IMPLEMENTING
    assert approved.context["plan_approval"]["state"] == "approved"

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        outcome = await orch.run_task(approved)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url
    assert coder.calls >= 1
    # The approved plan was NOT regenerated on resume.
    assert len(planner.plan_prompts) == 1


@pytest.mark.asyncio
async def test_choosing_an_option_needs_no_separate_answer_field(client, store):
    """The board posts `{choose: n}` with no `answer` (api.js
    `chooseBlockerOption`). That was a 422, so no one-click blocker option was
    reachable from the drawer - including the gate's Approve button. An empty
    body is still rejected."""
    t = Task.new("x", repo_path="/tmp/repo")
    t.blocker = {"category": "AMBIGUITY", "question": "which?",
                 "options": [{"label": "the first one", "action": None}]}
    await store.create_task(t)
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.AWAITING_INPUT, validate=False)

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"choose": 1})
    assert r.status_code == 200, r.text
    fresh = await store.get_task(t.id)
    assert fresh.context["human_replies"][-1]["answer"] == "the first one"

    empty = await client.post(f"/api/tasks/{t.id}/reply", json={})
    assert empty.status_code == 422


# --------------------------------------------------------------------------- #
# 5. A correction re-plans once, with the correction attached, then re-parks   #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_correction_reply_replans_once_and_parks_again(
    bare_repo, tmp_path, store, client,
):
    coder = CountingCoder()
    planner = ScriptedPlanner(_PLAN_A, _PLAN_B)
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        await orch.run_task(t)

    r = await client.post(f"/api/tasks/{t.id}/reply",
                          json={"answer": "use operator.mul, do not hand-roll it"})
    assert r.status_code == 200, r.text
    corrected = await store.get_task(t.id)
    # A correction re-enters PLANNING, not IMPLEMENTING.
    assert corrected.status is TaskStatus.PLANNING
    assert corrected.context["plan_approval"]["correction"] == (
        "use operator.mul, do not hand-roll it")

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        outcome = await orch.run_task(corrected)

    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert coder.calls == 0
    assert await store.list_attempts(t.id) == []
    # Exactly ONE re-plan, and the correction reached the planner.
    assert len(planner.plan_prompts) == 2
    assert "use operator.mul, do not hand-roll it" in planner.plan_prompts[-1]
    assert "Add a mul function." in planner.plan_prompts[-1]  # the rejected plan
    fresh = await store.get_task(t.id)
    assert fresh.context["plan"] == _PLAN_B.strip()
    assert fresh.context["plan_approval"]["replans"] == 1


@pytest.mark.asyncio
async def test_second_correction_does_not_replan_again(
    bare_repo, tmp_path, store, client,
):
    """The loop is bounded: the re-plan is capped at one. A further
    correction parks again WITHOUT spending another planner session."""
    coder = CountingCoder()
    planner = ScriptedPlanner(_PLAN_A, _PLAN_B)
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        await orch.run_task(t)
        await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "first"})
        await orch.run_task(await store.get_task(t.id))
        await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "second"})
        outcome = await orch.run_task(await store.get_task(t.id))

    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert len(planner.plan_prompts) == 2      # not 3
    assert coder.calls == 0
    fresh = await store.get_task(t.id)
    assert "already been revised once" in (fresh.blocker["question"] or "")


@pytest.mark.asyncio
async def test_a_corrected_plan_is_claimable_and_is_not_mistaken_for_an_orphan(store):
    """The correction resumes into PLANNING, which `nh serve` neither claimed
    nor left alone: the startup orphan sweep flipped every PLANNING row to
    IMPLEMENTING, which would spend the run on the plan the human had just
    rejected. Both scheduler seams must know the difference."""
    from no_human.core.scheduler import Scheduler

    corrected = Task.new("corrected", repo_path="/r")
    corrected.config["plan_approval"] = True
    corrected.context = {"plan_approval": {"state": "correcting",
                                           "correction": "do it differently"}}
    await store.create_task(corrected)
    await store.set_status(corrected, TaskStatus.PLANNING, validate=False)
    orphan = Task.new("killed mid-plan", repo_path="/r")
    await store.create_task(orphan)
    await store.set_status(orphan, TaskStatus.PLANNING, validate=False)

    sched = Scheduler(store, lambda task=None: None)
    claimable = {t.id for t in await sched._claimable()}
    assert corrected.id in claimable
    assert orphan.id not in claimable

    # `orphan` must actually read as an orphan to the sweep's liveness gate
    # (row stamp AND newest event predate the grace window) — `corrected` is
    # deliberately left fresh, since the point of this test is that a
    # `correcting` PLANNING row is skipped for its OWN reason (plan_gate.
    # correcting), not because it happens to look stale.
    from datetime import datetime, timedelta, timezone
    import time as _time
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, orphan.id))
    await store.db.execute(
        "UPDATE task_events SET ts = ? WHERE task_id = ?",
        (_time.time() - 3600, orphan.id))
    await store.db.commit()

    await sched._recover_orphans()
    assert (await store.get_task(corrected.id)).status is TaskStatus.PLANNING
    assert (await store.get_task(orphan.id)).status is TaskStatus.IMPLEMENTING


# --------------------------------------------------------------------------- #
# 6. Poller-created tasks never carry the flag                                 #
# --------------------------------------------------------------------------- #

def test_jira_normalize_never_sets_the_gate():
    from no_human.intake.jira import JiraAdapter

    adapter = JiraAdapter({"integrations": {"jira": {
        "site": "https://example.atlassian.net", "project_key": "ABC",
        "email": "e@x.com", "jql": "project = ABC",
    }}})
    task = adapter.normalize({
        "key": "ABC-1",
        "fields": {"summary": "Do a thing", "description": None},
    })
    assert plan_gate.required(task) is False
    assert "plan_approval" not in (task.config or {})


@pytest.mark.asyncio
async def test_task_create_ignores_plan_approval_for_jira_source(client, store, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    body = {"title": "imported", "repo_path": str(repo), "source": "jira",
            "external_id": "ABC-2", "plan_approval": True}
    r = await client.post("/api/tasks", json=body)
    assert r.status_code == 201, r.text
    created = await store.get_task(r.json()["id"])
    assert plan_gate.required(created) is False

    body2 = {"title": "board task", "repo_path": str(repo), "source": "board",
             "plan_approval": True}
    r2 = await client.post("/api/tasks", json=body2)
    assert r2.status_code == 201, r2.text
    board_task = await store.get_task(r2.json()["id"])
    assert plan_gate.required(board_task) is True


@pytest.mark.asyncio
async def test_flag_rides_on_task_config_and_survives_a_round_trip(store):
    """It lives on `task.config` — the same carrier as every other per-task,
    human-only override (`nh task config`, the budget caps)."""
    t = Task.new("x", repo_path="/tmp/repo")
    assert plan_gate.required(t) is False       # default OFF
    t.config["plan_approval"] = True
    await store.create_task(t)
    assert plan_gate.required(await store.get_task(t.id)) is True


def test_task_add_offers_the_opt_in():
    from click.testing import CliRunner

    from no_human.cli.commands import cli

    out = CliRunner().invoke(cli, ["task", "add", "--help"]).output
    assert "--approve-plan" in out


# --------------------------------------------------------------------------- #
# 7. The approve action is a whitelisted, human-only verb                      #
# --------------------------------------------------------------------------- #

def test_approve_plan_action_is_recognised_and_shaped():
    from no_human.blockers import ActionError, apply_action, is_plan_approval_action

    t = Task.new("x")
    assert is_plan_approval_action({"approve_plan": True}) is True
    assert is_plan_approval_action({"set_task_config": {"attempt_tokens": 1}}) is False
    assert is_plan_approval_action(None) is False
    assert apply_action(t, {"approve_plan": True})
    with pytest.raises(ActionError):
        apply_action(t, {"approve_plan": True, "park": True})
    with pytest.raises(ActionError):
        apply_action(t, {"approve_plan": "yes"})


def test_agent_raised_blocker_cannot_forge_an_approval():
    """`parse_blocker` strips actions off anything the agent emits, so the
    agent can never approve its own plan."""
    import json as _json

    from no_human.blockers import parse_blocker

    raw = _json.dumps({
        "category": "AMBIGUITY", "confidence": 0.9,
        "question": "approve?",
        "options": [{"label": "approve", "action": {"approve_plan": True}}],
    })
    b = parse_blocker(f"BLOCKER_JSON_START\n{raw}\nBLOCKER_JSON_END")
    assert b is not None
    assert all(o.action is None for o in b.options)
