"""USER_PAUSED as a first-class BlockerCategory (PLAN.md Part 22 typed-stop
matrix, see taxonomy.py's module docstring).

Before this, all three pause writers (`orchestrator._honor_cancel`,
`cli.commands.task_pause`, `api.app.pause_task`'s direct-park branch) wrote
``category: "USER_PAUSED"`` as a bare string, but ``USER_PAUSED`` was not a
member of ``BlockerCategory`` — so ``Blocker.from_dict`` silently coerced
every parked pause to ``NOVEL_UNKNOWN`` via ``BlockerCategory.coerce``, and
neither ``route_for``/``triage`` nor the wake sweep had any pause-specific
handling. This file pins:

* AC1 — ``USER_PAUSED`` round-trips through ``Blocker``/``BlockerCategory``,
  ``route_for``/``triage`` have an explicit entry (never falling through to
  the low-confidence-forces-escalation override the writers' ``confidence:
  0.0`` would otherwise trip), and the agent can never self-declare one.
* AC2 — the decided ``max_park`` sweep behaviour for a PAUSE (escalate — a
  forgotten pause must not lock a task out forever) is identical for all
  three writers, a stale carried ``wake_condition`` can never resume a
  pause early, resume stays a ONE-step operation, and a HOLD
  (``human_stopped=True``) stays indefinite regardless of category.
* AC3 — the negative control: a non-paused parked task still escalates at
  the exact same ``max_park`` boundary.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app as api_app
from no_human.blockers import (
    Blocker,
    BlockerCategory,
    HARNESS_ONLY_CATEGORIES,
    Route,
    WakeWatcher,
    blocker_prompt_suffix,
    parse_blocker,
    route_for,
    triage,
    user_pause_blocker,
)
from no_human.cli.commands import cli as cli_group
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

from tests.test_blockers import _cfg, _park
from tests.test_cli_commands import _get_task, _make_runner, _seed_task as _cli_seed_task
from tests.test_infra_not_work import (  # noqa: F401 — fixtures re-exported on purpose
    _git,
    _incident_result,
    _run_one_attempt,
    bare_repo,
)

# The CLI leg goes through `nh task pause`, which (like every test in
# tests/test_cli_commands.py) can reach `config.load_env_var` before this
# module's fake config exists — requested by NAME, never autouse.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


async def _seed_active_task(store: Store, title: str = "Fix thing") -> Task:
    t = Task.new(title, repo_path="/tmp/repo")
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    return t


def _cli_pause_writer_check(cli_db, monkeypatch) -> None:
    """The `nh task pause` leg, run in a worker thread: `task_pause` calls
    `asyncio.run(...)` internally, which cannot nest inside this module's
    already-running async test loop — see commands.py:1493."""
    cli_task_id = _cli_seed_task(cli_db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(cli_db, monkeypatch)
    monkeypatch.setattr(
        "no_human.cli.commands._server_owns_worker", lambda cfg: False)
    result = runner.invoke(
        cli_group, ["task", "pause", cli_task_id[:8],
                    "--reason", "user paused via CLI"])
    assert result.exit_code == 0, result.output
    cli_task = _get_task(cli_db, cli_task_id)
    assert cli_task.blocker["category"] == "USER_PAUSED"
    assert "human_stopped" not in cli_task.blocker
    assert cli_task.blocker.get("raised_at")


def _cli_pause_then_resume_check(cli_db, monkeypatch) -> None:
    """`nh task pause` then `nh task resume`, one step each — same
    asyncio.run-nesting reason as `_cli_pause_writer_check` above."""
    cli_task_id = _cli_seed_task(cli_db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(cli_db, monkeypatch)
    monkeypatch.setattr(
        "no_human.cli.commands._server_owns_worker", lambda cfg: False)
    r3 = runner.invoke(
        cli_group, ["task", "pause", cli_task_id[:8], "--reason", "hold on"])
    assert r3.exit_code == 0, r3.output
    paused = _get_task(cli_db, cli_task_id)
    assert paused.status == TaskStatus.BLOCKED
    assert paused.blocker["category"] == "USER_PAUSED"

    r4 = runner.invoke(cli_group, ["task", "resume", cli_task_id[:8]])
    assert r4.exit_code == 0, r4.output
    cli_resumed = _get_task(cli_db, cli_task_id)
    assert cli_resumed.status == TaskStatus.IMPLEMENTING
    assert cli_resumed.blocker is None


def _api_client(store: Store, tmp_path) -> AsyncClient:
    """A board API client wired to `store`, with no scheduler attached — the
    direct-park branch of `pause_task`/`resume_task` (no worker owns the
    task), matching `nh task pause` with no server running."""
    from no_human.config import load_config

    api_app.state.store = store
    api_app.state.config = load_config(tmp_path / "api_config.yaml")
    api_app.state.scheduler = None
    transport = ASGITransport(app=api_app)
    return AsyncClient(transport=transport, base_url="http://localhost")


# --------------------------------------------------------------------------- #
# AC1 — typed category, round-trip, explicit routing                          #
# --------------------------------------------------------------------------- #

def test_user_paused_is_a_first_class_category_and_round_trips():
    assert BlockerCategory.coerce("USER_PAUSED") is BlockerCategory.USER_PAUSED
    assert BlockerCategory.USER_PAUSED in HARNESS_ONLY_CATEGORIES

    b = Blocker.from_dict({
        "category": "USER_PAUSED",
        "question": "Paused by operator via web board",
        "raised_at": "2026-06-22T12:00:00+00:00",
        "paused_by": "board",
    })
    # The bug this pins: without an enum member, `coerce` defaults an
    # unrecognized string to NOVEL_UNKNOWN — every parked pause silently lost
    # its category.
    assert b.category is BlockerCategory.USER_PAUSED
    assert b.category is not BlockerCategory.NOVEL_UNKNOWN
    assert b.to_dict()["category"] == "USER_PAUSED"


def test_route_and_triage_are_explicit_for_a_pause():
    expected = Route(TaskStatus.BLOCKED, notify_now=False, parked=True)
    assert route_for(BlockerCategory.USER_PAUSED) == expected

    # The three writers record confidence=0.0 (there is no diagnosis to be
    # confident about) — the generic `route.parked and confidence < 0.6`
    # override would otherwise force this to ESCALATED. `triage` must have an
    # explicit branch ahead of it, same as BUDGET_EXHAUSTED's early return.
    b = Blocker(category=BlockerCategory.USER_PAUSED, confidence=0.0)
    assert triage(b) == expected


def test_the_agent_cannot_declare_a_user_pause():
    # Never advertised as a category the agent may self-report.
    assert "USER_PAUSED" not in blocker_prompt_suffix()

    # And a claimed one is demoted, the same trust boundary as the
    # BUDGET_EXHAUSTED -> SCOPE_EXPLOSION demotion just above it in
    # report.py — the harness alone may raise a pause.
    text = (
        "I would like to stop here.\n"
        "BLOCKER_JSON_START\n"
        "{\n"
        '  "category": "USER_PAUSED",\n'
        '  "question": "let me pause",\n'
        '  "root_cause_hypothesis": "I want to stop",\n'
        '  "confidence": 0.9\n'
        "}\n"
        "BLOCKER_JSON_END\n"
    )
    blocker = parse_blocker(text)
    assert blocker is not None
    assert blocker.category is BlockerCategory.NOVEL_UNKNOWN
    assert blocker.reason_is_agent_authored is True


# --------------------------------------------------------------------------- #
# AC2 — one decided sweep behaviour, identical for all three writers,         #
#        resume stays one step                                                #
# --------------------------------------------------------------------------- #

async def test_all_three_writers_produce_the_same_pause_record(
        store, bare_repo, tmp_path, monkeypatch):
    # Constructor shape: different reason/paused_by, same key set/category.
    shapes = [
        user_pause_blocker("stopped by operator: Paused from board",
                            checkpoint={"sha": "a" * 40, "branch": "b"},
                            paused_by="orchestrator"),
        user_pause_blocker("user paused via CLI",
                            checkpoint={"sha": "a" * 40, "branch": "b"},
                            paused_by="cli"),
        user_pause_blocker("Paused by operator via web board",
                            checkpoint={"sha": "a" * 40, "branch": "b"},
                            paused_by="board"),
    ]
    expected_keys = {
        "category", "question", "root_cause_hypothesis", "resume_commit",
        "resume_branch", "raised_at", "paused_by",
    }
    for shape in shapes:
        assert set(shape) == expected_keys
        assert shape["category"] == "USER_PAUSED"
        assert "human_stopped" not in shape
        assert shape["raised_at"]

    # Behavioural check: each of the three REAL writers, exercised end to end.

    # 1. orchestrator._honor_cancel — a running attempt stopped mid-way.
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    outcome = await orch._honor_cancel(task, repo, None, "Paused from board")
    assert outcome.status == TaskStatus.BLOCKED
    orch_task = await store.get_task(task.id)
    assert orch_task.blocker["category"] == "USER_PAUSED"
    assert "human_stopped" not in orch_task.blocker
    assert orch_task.blocker.get("raised_at")

    # 2. `nh task pause`, no server running.
    cli_db = tmp_path / "cli_writer.db"
    await asyncio.to_thread(_cli_pause_writer_check, cli_db, monkeypatch)

    # 3. `POST /pause` direct-park branch — no scheduler owns the task.
    api_task = await _seed_active_task(store, title="board pause")
    async with _api_client(store, tmp_path) as client:
        r = await client.post(f"/api/tasks/{api_task.id}/pause")
        assert r.status_code == 200, r.text
    board_task = await store.find_task(api_task.id)
    assert board_task.blocker["category"] == "USER_PAUSED"
    assert "human_stopped" not in board_task.blocker
    assert board_task.blocker.get("raised_at")


async def test_a_forgotten_pause_escalates_at_max_park(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(hours=49)).isoformat()
    blocker = user_pause_blocker(
        "Paused by operator via web board", checkpoint=None, paused_by="board")
    blocker["raised_at"] = raised
    t = await _park(store, status=TaskStatus.BLOCKED, blocker=blocker)

    watcher = WakeWatcher(store, _cfg())
    action = await watcher._evaluate(t, now=now)
    assert action == "escalated_timeout"

    fresh = await store.get_task(t.id)
    assert fresh.status == TaskStatus.ESCALATED
    assert fresh.blocker["timed_out"] is True
    # Survives `_escalate_timeout` — only a `blocker is None` fallback
    # overwrites category, and this one round-trips as a real member now.
    assert fresh.blocker["category"] == "USER_PAUSED"
    # An honest, pause-specific reason — not the generic timeout prose a
    # diagnosis-based blocker gets.
    assert "paused by board" in fresh.blocker["root_cause_hypothesis"]
    assert "parked past max duration" not in fresh.blocker["root_cause_hypothesis"]


async def test_a_pause_is_never_resumed_by_a_carried_wake_condition(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(hours=1)).isoformat()  # well inside max_park
    blocker = user_pause_blocker(
        "Paused by operator via web board", checkpoint=None, paused_by="board")
    blocker["raised_at"] = raised
    # A wake_condition carried over from the blocker the pause landed on
    # (`paused_over`) — stale, and already satisfied by elapsed time alone.
    # `after:<dur>` is the watcher's grammar (wake.py `_parse_after`); the
    # first version of this test wrote `timeout:1s`, which matches nothing,
    # so it passed on main for the wrong reason (review of PR #557).
    blocker["wake_condition"] = "after:1s"
    t = await _park(store, status=TaskStatus.BLOCKED, blocker=blocker)

    watcher = WakeWatcher(store, _cfg())
    action = await watcher._evaluate(t, now=now)
    assert action is None

    fresh = await store.get_task(t.id)
    assert fresh.status == TaskStatus.BLOCKED


async def test_resume_still_resumes_a_paused_task_in_one_step(
        store, tmp_path, monkeypatch):
    task = await _seed_active_task(store)
    async with _api_client(store, tmp_path) as client:
        r1 = await client.post(f"/api/tasks/{task.id}/pause")
        assert r1.status_code == 200, r1.text
        fresh = await store.find_task(task.id)
        assert fresh.status == TaskStatus.BLOCKED
        assert fresh.blocker["category"] == "USER_PAUSED"

        r2 = await client.post(f"/api/tasks/{task.id}/resume")
        assert r2.status_code == 200, r2.text
    resumed = await store.find_task(task.id)
    assert resumed.status == TaskStatus.IMPLEMENTING
    assert resumed.blocker is None

    # The CLI twin: `nh task pause` then `nh task resume`, same one-step shape.
    cli_db = tmp_path / "cli_resume.db"
    await asyncio.to_thread(_cli_pause_then_resume_check, cli_db, monkeypatch)


async def test_board_pause_over_an_existing_pause_does_not_become_a_two_step_resume(
        store, tmp_path):
    task = await _seed_active_task(store)
    async with _api_client(store, tmp_path) as client:
        r1 = await client.post(f"/api/tasks/{task.id}/pause")
        assert r1.status_code == 200, r1.text
        fresh = await store.find_task(task.id)
        assert fresh.status == TaskStatus.BLOCKED
        assert fresh.blocker["category"] == "USER_PAUSED"

        # Pause again on the already-parked task (the hold branch's entry
        # condition). Must stay idempotent — never stamp `human_stopped` over
        # an existing pause, which would turn one operator pause into a
        # two-step resume (release-hold, then resume).
        r2 = await client.post(f"/api/tasks/{task.id}/pause")
        assert r2.status_code == 200, r2.text
        fresh2 = await store.find_task(task.id)
        assert "human_stopped" not in (fresh2.blocker or {})
        assert fresh2.blocker["category"] == "USER_PAUSED"

        r3 = await client.post(f"/api/tasks/{task.id}/resume")
        assert r3.status_code == 200, r3.text
    fresh3 = await store.find_task(task.id)
    assert fresh3.status == TaskStatus.IMPLEMENTING
    assert fresh3.blocker is None


async def test_a_hold_over_a_real_blocker_is_still_indefinite(store):
    """Regression guard (SCRUM-22/58): a HOLD stamped over some OTHER park
    reason is never swept by max_park, whatever the underlying category."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(hours=49)).isoformat()
    t = await _park(
        store, status=TaskStatus.ESCALATED,
        blocker={"category": "AMBIGUITY", "question": "which store?",
                 "raised_at": raised, "confidence": 0.9, "human_stopped": True},
    )
    watcher = WakeWatcher(store, _cfg())
    action = await watcher._evaluate(t, now=now)
    assert action is None
    fresh = await store.get_task(t.id)
    assert fresh.status == TaskStatus.ESCALATED


# --------------------------------------------------------------------------- #
# AC3 — negative control                                                      #
# --------------------------------------------------------------------------- #

async def test_a_non_paused_parked_task_still_escalates_at_max_park(store):
    # Same clock as `test_a_forgotten_pause_escalates_at_max_park` — the two
    # cannot both pass for a clock-arithmetic reason.
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(hours=49)).isoformat()
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "AMBIGUITY", "question": "which store?",
                 "raised_at": raised, "confidence": 0.9},
    )
    watcher = WakeWatcher(store, _cfg())
    action = await watcher._evaluate(t, now=now)
    assert action == "escalated_timeout"
    fresh = await store.get_task(t.id)
    assert fresh.status == TaskStatus.ESCALATED
    assert fresh.blocker["timed_out"] is True
    assert "parked past max duration" in fresh.blocker["root_cause_hypothesis"]
