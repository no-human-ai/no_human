"""GAP 1 follow-up — the routes that reached IMPLEMENTING *around* the gate.

The gate itself (tests/test_plan_approval_gate.py) is enforced on the PENDING
walk down the planning spine. An independent review found that the check only
ran inside `if task.status == TaskStatus.PENDING:`, so every OTHER route into
the attempt loop skipped it permanently:

  * a `nh serve` restart while the task was in CONTEXT/PLANNING — the startup
    orphan sweep flips it to IMPLEMENTING and the run goes to a PR with no plan
    at all (planning is minutes of LLM latency, so this is the normal window);
  * `WakeWatcher._resume`, which sets IMPLEMENTING unconditionally;
  * `POST /api/tasks/{id}/resume` and `nh unblock`, which clear the blocker.

Two independent strands came out of the same review:

  * a blank `answer` recorded `state=correcting, correction=""` — the writer
    accepted it and the claimer (which required truthy text) did not pick it
    up, so the task stranded in PLANNING with no worker until a restart turned
    the strand into one of the bypasses above;
  * nothing ever cleared `plan_approval.state`, so a stale `awaiting` hijacked
    a later, unrelated answer back into PLANNING and threw away the WIP sha.

Every fix here derives from a LIVE artifact — the task's status at the loop
head, the correction state, the blocker actually on the task — never from a
flag someone has to remember to clear.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch as _patch

from no_human.agent.claude_backend import AgentResult
from no_human.api.app import app
from no_human.config import load_config
from no_human.core import plan_gate
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier


# --------------------------------------------------------------------------- #
# Fixtures (same shape as tests/test_plan_approval_gate.py)                     #
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


class ScriptedPlanner:
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


async def _gated_task(store, bare_repo):
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    t.config["plan_approval"] = True
    await store.create_task(t)
    return t


def _pushed_branches(bare_repo) -> str:
    return subprocess.run(["git", "branch", "--list"], cwd=bare_repo,
                          capture_output=True, text=True).stdout


def _approve_option(blocker) -> dict | None:
    for o in (blocker or {}).get("options") or []:
        if o.get("action") == {"approve_plan": True}:
            return o
    return None


async def _age_task(store, task_id, seconds=3600):
    """Back-date a row's `updated_at` and any `task_events` it already has so
    it clears `Scheduler._row_is_live`'s activity-grace window before a test
    calls `_recover_orphans` to simulate a crash worth recovering — these
    routes are about what the sweep does with a genuinely dead row, not about
    liveness detection, and a `set_status` moments earlier in the same test
    body reads as live otherwise."""
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    await store.db.execute(
        "UPDATE tasks SET updated_at = ? WHERE id = ?", (old, task_id))
    await store.db.execute(
        "UPDATE task_events SET ts = ? WHERE task_id = ?",
        (time.time() - seconds, task_id))
    await store.db.commit()


# --------------------------------------------------------------------------- #
# Fix 1 — the gate is load-bearing on EVERY route into the attempt loop         #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_serve_restart_during_context_does_not_implement_without_a_plan(
    bare_repo, tmp_path, store,
):
    """CRITICAL reproduction. `nh serve` dies while the task is in CONTEXT.
    `_recover_orphans` flips it to IMPLEMENTING, `_drive` skips the whole
    planning spine (it only walks it from PENDING) — and with it the gate — and
    the run implements with no plan at all."""
    coder = CountingCoder()
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    await store.set_status(t, TaskStatus.CONTEXT, validate=False)
    await _age_task(store, t.id)

    # The route: startup crash recovery.
    await Scheduler(store, lambda task=None: None)._recover_orphans()
    recovered = await store.get_task(t.id)
    assert recovered.status is TaskStatus.IMPLEMENTING, "the route under test"

    outcome = await orch.run_task(recovered)

    assert coder.calls == 0, "implemented without the human ever seeing a plan"
    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert "no-human/" not in _pushed_branches(bare_repo)
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_INPUT
    assert _approve_option(fresh.blocker), "parked on the gate, with an approve option"
    # No plan exists on this route, and the question says so rather than
    # pretending there is something to read.
    assert "no plan" in (fresh.blocker["question"] or "").lower()


@pytest.mark.asyncio
async def test_serve_restart_during_planning_parks_on_the_plan_it_already_had(
    bare_repo, tmp_path, store,
):
    """Same route, one step later: the plan was persisted but the process died
    before the gate parked. The re-park must show that plan."""
    coder = CountingCoder()
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    await store.merge_context(t.id, {"plan": _PLAN_A.strip()})
    await store.set_status(t, TaskStatus.PLANNING, validate=False)
    await _age_task(store, t.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()
    recovered = await store.get_task(t.id)
    assert recovered.status is TaskStatus.IMPLEMENTING

    outcome = await orch.run_task(recovered)

    assert coder.calls == 0
    assert outcome.status is TaskStatus.AWAITING_INPUT
    fresh = await store.get_task(t.id)
    assert _PLAN_A.strip() in fresh.blocker["evidence"]


@pytest.mark.asyncio
async def test_wake_resume_of_a_rejected_plan_re_parks_instead_of_implementing(
    bare_repo, tmp_path, store,
):
    """HIGH reproduction. `WakeWatcher._resume` sets IMPLEMENTING
    unconditionally. Resuming a task whose gate state is `correcting` — the
    human REJECTED this plan — used to spend the run implementing it. It must
    re-plan with their correction instead, and park again."""
    from no_human.blockers.wake import WakeWatcher

    coder = CountingCoder()
    planner = ScriptedPlanner(_PLAN_A)
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    await store.merge_context(t.id, {
        "plan": _PLAN_A.strip(),
        "plan_approval": {"state": "correcting", "correction": "use operator.mul",
                          "plan": _PLAN_A.strip()},
    })
    await store.set_status(t, TaskStatus.BLOCKED, validate=False)

    watcher = WakeWatcher(store, {})
    assert await watcher._resume(await store.get_task(t.id)) == "resumed"
    resumed = await store.get_task(t.id)
    assert resumed.status is TaskStatus.IMPLEMENTING, "the route under test"

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        outcome = await orch.run_task(resumed)

    assert coder.calls == 0, "implemented a plan the human had rejected"
    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert "no-human/" not in _pushed_branches(bare_repo)
    # The human's correction is honoured, not dropped on the floor by the
    # route it happened to come back in on.
    assert len(planner.plan_prompts) == 1
    assert "use operator.mul" in planner.plan_prompts[-1]
    assert (await store.get_task(t.id)).context["plan_approval"]["replans"] == 1


@pytest.mark.asyncio
async def test_drawer_resume_button_does_not_clear_the_gate(
    bare_repo, tmp_path, store, client,
):
    """MEDIUM reproduction. `POST /api/tasks/{id}/resume` drops the blocker and
    sets IMPLEMENTING — it cleared the gate without approving it."""
    coder = CountingCoder()
    planner = ScriptedPlanner(_PLAN_A)
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        await orch.run_task(t)
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_INPUT

    r = await client.post(f"/api/tasks/{t.id}/resume")
    assert r.status_code == 200, r.text
    resumed = await store.get_task(t.id)
    assert resumed.status is TaskStatus.IMPLEMENTING, "the route under test"
    assert resumed.blocker is None

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        outcome = await orch.run_task(resumed)

    assert coder.calls == 0
    assert outcome.status is TaskStatus.AWAITING_INPUT
    fresh = await store.get_task(t.id)
    assert _approve_option(fresh.blocker), "the gate came back, not a bypass"
    assert not plan_gate.approved(fresh), "resume must never mean approved"


@pytest.mark.asyncio
async def test_nh_unblock_does_not_clear_the_gate(bare_repo, tmp_path, store):
    """MEDIUM reproduction, CLI half: `nh unblock` is the drawer's Resume."""
    from click.testing import CliRunner

    from no_human.cli.commands import cli

    coder = CountingCoder()
    planner = ScriptedPlanner(_PLAN_A)
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        await orch.run_task(t)

    cfg = _cfg(tmp_path)
    cfg.data.setdefault("database", {})["path"] = str(store.path)

    def _invoke():
        # The command opens its own Store and calls asyncio.run, so it cannot
        # run on this test's loop.
        with _patch("no_human.cli.commands._bootstrap", return_value=(cfg, None)):
            return CliRunner().invoke(cli, ["unblock", t.id[:8]])

    res = await asyncio.to_thread(_invoke)
    assert res.exit_code == 0, res.output
    unblocked = await store.get_task(t.id)
    assert unblocked.status is TaskStatus.IMPLEMENTING, "the route under test"

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        outcome = await orch.run_task(unblocked)

    assert coder.calls == 0
    assert outcome.status is TaskStatus.AWAITING_INPUT


@pytest.mark.asyncio
async def test_run_attempt_itself_refuses_an_unapproved_plan(
    bare_repo, tmp_path, store,
):
    """The backstop. `_run_attempt` is the only place that writes code (its two
    `backend.run` sites are the coder and the preflight; the third in the file
    is a read-only PR-diff fetch on the code_review pipeline). A sixth bypass
    route must trip here rather than ship a PR."""
    from no_human.vcs import GitRepo

    coder = CountingCoder()
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    with pytest.raises(plan_gate.PlanNotApproved):
        await orch._run_attempt(await store.get_task(t.id), GitRepo(bare_repo), 1, "main")
    assert coder.calls == 0


@pytest.mark.asyncio
async def test_the_park_wake_ping_pong_settles_and_then_escalates(
    bare_repo, tmp_path, store,
):
    """A resume flips the task to IMPLEMENTING and the orchestrator re-parks it.
    That must SETTLE (the watcher exempts AWAITING_INPUT from wake-condition
    resume, so nothing bounces it again) and must still time out into a human's
    lap rather than sit forever."""
    from no_human.blockers.wake import WakeWatcher

    coder = CountingCoder()
    planner = ScriptedPlanner(_PLAN_A)
    orch = _orch(store, _cfg(tmp_path), coder)
    t = await _gated_task(store, bare_repo)
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        await orch.run_task(t)

    watcher = WakeWatcher(store, {"blockers": {"max_park_duration": "48h"}})
    # One bounce: something resumed it, the orchestrator put it back.
    await watcher._resume(await store.get_task(t.id))
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=planner):
        await orch.run_task(await store.get_task(t.id))
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_INPUT

    now = datetime.now(timezone.utc)
    assert await watcher.tick(now=now) == [], "settles — no second bounce"
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_INPUT

    later = now + timedelta(hours=49)
    assert (t.id, "escalated_timeout") in await watcher.tick(now=later)
    assert (await store.get_task(t.id)).status is TaskStatus.ESCALATED
    assert coder.calls == 0


# --------------------------------------------------------------------------- #
# Fix 2 — a blank answer can neither be written nor strand a task              #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_blank_answer_is_rejected_by_the_api(client, store):
    """`POST /reply {"answer": ""}` recorded `state=correcting, correction=""`.
    Nothing claimed it, every further reply 409'd, and the task sat in PLANNING
    with no worker until a restart converted the strand into a gate bypass."""
    t = Task.new("x", repo_path="/tmp/repo")
    t.blocker = {"category": "AMBIGUITY", "question": "which?", "options": []}
    await store.create_task(t)
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.AWAITING_INPUT, validate=False)

    assert (await client.post(f"/api/tasks/{t.id}/reply",
                              json={"answer": ""})).status_code == 422
    assert (await client.post(f"/api/tasks/{t.id}/reply",
                              json={"answer": "   \n\t "})).status_code == 422
    # Unchanged: an empty body is still 422 (test_api.test_reply_missing_answer_422)
    assert (await client.post(f"/api/tasks/{t.id}/reply", json={})).status_code == 422
    # Also unchanged, and deliberately so: `{"answer": "", "choose": N}` is what
    # `nh reply --choose` posts. The blank field is replaced by the option's
    # label before anything reads it, so it is inert — only FREE TEXT is
    # rejected. (Here the option index is out of range, hence 400 not 422:
    # what matters is that the body itself validated.)
    assert (await client.post(f"/api/tasks/{t.id}/reply",
                              json={"answer": "", "choose": 1})).status_code == 400
    # ...and a real answer still works.
    assert (await client.post(f"/api/tasks/{t.id}/reply",
                              json={"answer": "postgres"})).status_code == 200
    assert (await store.get_task(t.id)).status is TaskStatus.IMPLEMENTING


def test_blank_answer_is_rejected_by_the_cli():
    from click.testing import CliRunner

    from no_human.cli.commands import cli

    res = CliRunner().invoke(cli, ["reply", "abc12345", ""])
    assert res.exit_code == 2, res.output
    assert "blank" in res.output.lower()


@pytest.mark.asyncio
async def test_a_correcting_task_is_claimed_on_its_state_not_its_text(store):
    """The writer and the claimer must agree on what a correction IS. Keyed on
    `state == correcting`, a blank correction that somehow got written can
    never strand a task with no worker again."""
    blank = Task.new("blank correction", repo_path="/r")
    blank.config["plan_approval"] = True
    blank.context = {"plan_approval": {"state": "correcting", "correction": ""}}
    await store.create_task(blank)
    await store.set_status(blank, TaskStatus.PLANNING, validate=False)

    orphan = Task.new("killed mid-plan", repo_path="/r")
    await store.create_task(orphan)
    await store.set_status(orphan, TaskStatus.PLANNING, validate=False)

    sched = Scheduler(store, lambda task=None: None)
    assert blank.id in {t.id for t in await sched._claimable()}
    assert orphan.id not in {t.id for t in await sched._claimable()}

    # Only `orphan` needs to read as dead — `blank` stays fresh, since the
    # point of this test is that a `correcting` PLANNING row is claimed on
    # its state (plan_gate.correcting), not because it happens to look stale.
    await _age_task(store, orphan.id)

    await sched._recover_orphans()
    assert (await store.get_task(blank.id)).status is TaskStatus.PLANNING
    assert (await store.get_task(orphan.id)).status is TaskStatus.IMPLEMENTING


# --------------------------------------------------------------------------- #
# Fix 3 — a reply is a plan correction only at a LIVE gate blocker             #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_stale_gate_state_does_not_hijack_an_unrelated_reply(
    client, store,
):
    """Nothing cleared `plan_approval.state`, so a stale `awaiting` re-routed a
    later, unrelated answer into PLANNING and dropped the WIP sha with it. The
    routing must come from the blocker actually on the task."""
    t = Task.new("x", repo_path="/tmp/repo")
    await store.create_task(t)
    await store.merge_context(t.id, {
        "plan_approval": {"state": "awaiting", "plan": "old plan"},
    })
    # A DIFFERENT question, raised mid-implementation, carrying a checkpoint.
    t.blocker = {
        "category": "AMBIGUITY", "question": "which db?",
        "options": [], "resume_commit": "deadbeef" * 5,
    }
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.AWAITING_INPUT, validate=False)

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "postgres"})
    assert r.status_code == 200, r.text

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING, "not thrown back into planning"
    assert fresh.context["plan_approval"]["state"] == "awaiting", "untouched"
    assert "postgres" not in str(fresh.context["plan_approval"])
    # The committed WIP the human was answering about is still what we resume from.
    assert fresh.context["resume_from"]["sha"] == "deadbeef" * 5


@pytest.mark.asyncio
async def test_a_reply_at_a_live_gate_blocker_is_still_a_correction(client, store):
    """The other half: with the gate's own blocker live, free text IS a
    correction and re-enters PLANNING."""
    t = Task.new("x", repo_path="/tmp/repo")
    t.config["plan_approval"] = True
    await store.create_task(t)
    t.blocker = {
        "category": "AMBIGUITY", "question": "Approve this plan?",
        "options": [{"label": plan_gate.APPROVE_LABEL,
                     "action": {"approve_plan": True}}],
    }
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.AWAITING_INPUT, validate=False)

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": "use operator.mul"})
    assert r.status_code == 200, r.text
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PLANNING
    assert fresh.context["plan_approval"]["state"] == "correcting"
    assert fresh.context["plan_approval"]["correction"] == "use operator.mul"


def test_at_gate_reads_the_blocker_not_the_context_flag():
    """The unit statement of the rule: the blocker is replaced on every park,
    so it cannot go stale; a context flag can and did."""
    stale = Task.new("x")
    stale.context = {"plan_approval": {"state": "awaiting"}}
    stale.blocker = {"category": "AMBIGUITY", "question": "which db?", "options": []}
    assert plan_gate.at_gate(stale) is False

    live = Task.new("x")
    live.blocker = {"category": "AMBIGUITY", "question": "approve?",
                    "options": [{"label": "Approve", "action": {"approve_plan": True}}]}
    assert plan_gate.at_gate(live) is True

    assert plan_gate.at_gate(Task.new("x")) is False


# --------------------------------------------------------------------------- #
# Advisory — the capped question offered a way out it did not have             #
# --------------------------------------------------------------------------- #

def test_the_capped_gate_offers_the_stop_it_tells_you_to_take():
    """The capped question said "or stop the task" and shipped no option to do
    it. `{"park": true}` is the existing terminal verb."""
    t = Task.new("x")
    b = plan_gate.build_blocker(t, "some plan", capped=True)
    actions = [o.action for o in b.options]
    assert {"approve_plan": True} in actions
    assert {"park": True} in actions, b.question
