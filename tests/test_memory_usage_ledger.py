"""Memory lifecycle A: the injection -> outcome ledger (use_count,
memory_uses, `nh learnings --usage`, and the counts the Rules/Skills/
Learnings API rows carry).

`memories.last_used_at` already answered "was this rule ever fetched into a
prompt"; nothing joined that injection to what the task it rode along with
actually did. These tests prove the three moving pieces this change adds:
injection writes a ledger row, a terminal outcome fills it in, and the
read surfaces (CLI + API, which the web UI renders) report it accurately —
all labelled correlational, not causal.
"""

import subprocess

import pytest

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator, TaskOutcome
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.agent.claude_backend import AgentEvent, AgentResult


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


class FakeBackend:
    """Stands in for ClaudeBackend: applies a scripted file mutation."""

    def __init__(self, mutate):
        self.mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                 tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    return cfg


# ── schema ────────────────────────────────────────────────────────────────── #

async def test_schema_has_use_count_and_memory_uses_table(store):
    cols = {r["name"] for r in await store._fetchall("PRAGMA table_info(memories)")}
    assert "use_count" in cols

    mu_cols = {r["name"] for r in await store._fetchall("PRAGMA table_info(memory_uses)")}
    assert mu_cols >= {"id", "memory_id", "task_id", "attempt_id",
                       "injected_at", "task_outcome", "created_at"}

    # FK to memories.id is enforced (PRAGMA foreign_keys = ON, Store.connect).
    mem_id = await store.add_memory(mem_type="rule", title="r", content="x",
                                    confirmed=True)
    await store.db.execute(
        "INSERT INTO memory_uses (id, memory_id, task_id, injected_at) "
        "VALUES ('u1', ?, 't1', datetime('now'))", (mem_id,))
    await store.db.commit()
    with pytest.raises(Exception):
        await store.db.execute(
            "INSERT INTO memory_uses (id, memory_id, task_id, injected_at) "
            "VALUES ('u2', 'does-not-exist', 't1', datetime('now'))")
        await store.db.commit()


# ── injection writes the ledger row (Store.record_memory_uses) ─────────────── #

async def test_record_memory_uses_writes_the_ledger_and_bumps_use_count(store):
    mem_id = await store.add_memory(mem_type="rule", title="r", content="x",
                                    confirmed=True)
    n = await store.record_memory_uses([mem_id], task_id="task-1", attempt_id="att-1")
    assert n == 1

    mem = await store.find_memory(mem_id)
    assert mem["use_count"] == 1

    rows = await store._fetchall(
        "SELECT * FROM memory_uses WHERE memory_id = ?", (mem_id,))
    assert len(rows) == 1
    row = rows[0]
    assert row["memory_id"] == mem_id
    assert row["task_id"] == "task-1"
    assert row["attempt_id"] == "att-1"
    assert row["injected_at"]
    assert row["task_outcome"] is None

    # A second injection (a resumed task) appends, it does not overwrite —
    # use_count keeps counting and there are now two ledger rows.
    await store.record_memory_uses([mem_id], task_id="task-2", attempt_id=None)
    mem = await store.find_memory(mem_id)
    assert mem["use_count"] == 2
    rows = await store._fetchall(
        "SELECT * FROM memory_uses WHERE memory_id = ?", (mem_id,))
    assert len(rows) == 2
    assert rows[1]["attempt_id"] is None

    assert await store.record_memory_uses([], task_id="task-3") == 0


# ── terminal state fills task_outcome (Store.fill_memory_use_outcomes) ─────── #

async def test_fill_memory_use_outcomes_only_touches_open_rows_for_that_task(store):
    mem_a = await store.add_memory(mem_type="rule", title="a", content="x", confirmed=True)
    mem_b = await store.add_memory(mem_type="rule", title="b", content="x", confirmed=True)
    await store.record_memory_uses([mem_a, mem_b], task_id="task-1")
    await store.record_memory_uses([mem_a], task_id="task-2")

    n = await store.fill_memory_use_outcomes("task-1", "success")
    assert n == 2

    rows = {r["task_id"]: r["task_outcome"]
            for r in await store._fetchall(
                "SELECT task_id, task_outcome FROM memory_uses ORDER BY task_id")}
    # task-1's two rows are filled; task-2's is untouched (still NULL).
    outcomes = await store._fetchall(
        "SELECT task_id, task_outcome FROM memory_uses WHERE task_id = 'task-1'")
    assert all(r["task_outcome"] == "success" for r in outcomes)
    still_open = await store._fetchall(
        "SELECT task_outcome FROM memory_uses WHERE task_id = 'task-2'")
    assert still_open[0]["task_outcome"] is None

    # A second fill on the same task (a resume that reaches a new terminal
    # state) never clobbers the FIRST resolved rows — only newly-open ones.
    await store.record_memory_uses([mem_a], task_id="task-1")  # a resumed run's injection
    n2 = await store.fill_memory_use_outcomes("task-1", "failure")
    assert n2 == 1  # only the fresh row
    outcomes = await store._fetchall(
        "SELECT task_outcome FROM memory_uses WHERE task_id = 'task-1' "
        "ORDER BY created_at")
    assert [r["task_outcome"] for r in outcomes] == ["success", "success", "failure"]


# ── Orchestrator._memory_use_outcome_label: the terminal-state mapping ─────── #

class _T:
    id = "task-x"


def test_outcome_label_maps_the_four_terminal_states():
    label = Orchestrator._memory_use_outcome_label
    task = Task.new("t", repo_path="/x")

    assert label(TaskOutcome(task, status=TaskStatus.DONE, detail="")) == "success"
    # AWAITING_APPROVAL is this codebase's own definition of a successful run
    # (attempts.status="succeeded" is stamped before it is ever returned).
    assert label(TaskOutcome(task, status=TaskStatus.AWAITING_APPROVAL,
                             detail="")) == "success"
    assert label(TaskOutcome(task, status=TaskStatus.FAILED, detail="nope")) == "failure"
    assert label(TaskOutcome(
        task, status=TaskStatus.BLOCKED,
        detail="cancelled by operator: stop please")) == "cancelled"
    assert label(TaskOutcome(
        task, status=TaskStatus.FAILED,
        detail="attempt timed out after 900s — a hung backend")) == "timeout"


def test_outcome_label_is_none_for_resumable_off_ramps():
    label = Orchestrator._memory_use_outcome_label
    task = Task.new("t", repo_path="/x")
    for status in (TaskStatus.ESCALATED, TaskStatus.PAUSED_QUOTA,
                  TaskStatus.AWAITING_INPUT):
        assert label(TaskOutcome(task, status=status, detail="")) is None
    # BLOCKED that is NOT an operator cancel (a parked blocker) stays open.
    assert label(TaskOutcome(
        task, status=TaskStatus.BLOCKED, detail="parked: needs a human")) is None


# ── end-to-end: injection at the real chokepoint, outcome at the real
#    terminal handler (run_task) ────────────────────────────────────────────── #

async def test_load_active_memories_ledgers_the_injection(bare_repo, tmp_path, store):
    """`Orchestrator._load_active_memories` — the one injection chokepoint —
    writes the ledger row with the right memory_id/task_id/injected_at."""
    mem_id = await store.add_memory(
        mem_type="rule", title="always-on rule", content="be careful",
        confirmed=True)
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("some task", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch._load_active_memories(t)

    rows = await store._fetchall(
        "SELECT * FROM memory_uses WHERE memory_id = ? AND task_id = ?",
        (mem_id, t.id))
    assert len(rows) == 1
    assert rows[0]["injected_at"]
    assert rows[0]["task_outcome"] is None
    # The implement-path call happens before attempt 1 exists.
    assert rows[0]["attempt_id"] is None

    mem = await store.find_memory(mem_id)
    assert mem["use_count"] == 1


async def test_run_task_success_fills_the_outcome_end_to_end(bare_repo, tmp_path, store):
    """A full run through `run_task` (PR opens -> AWAITING_APPROVAL) fills
    this run's ledger rows with 'success' — the terminal handler this change
    adds, exercised for real rather than unit-tested in isolation."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    mem_id = await store.add_memory(
        mem_type="rule", title="always-on rule", content="be careful",
        confirmed=True)
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL

    rows = await store._fetchall(
        "SELECT * FROM memory_uses WHERE memory_id = ? AND task_id = ?",
        (mem_id, t.id))
    assert len(rows) == 1
    assert rows[0]["task_outcome"] == "success"

    report = await store.memory_usage_report()
    row = next(r for r in report if r["memory_id"] == mem_id)
    assert row["use_count"] == 1
    assert row["success_count"] == 1
    assert row["failure_count"] == 0


# ── CLI / API read surfaces ──────────────────────────────────────────────── #

async def test_memory_usage_report_aggregates_the_outcome_split(store):
    mem_id = await store.add_memory(mem_type="rule", title="r", content="x",
                                    confirmed=True)
    await store.record_memory_uses([mem_id], task_id="t1")
    await store.record_memory_uses([mem_id], task_id="t2")
    await store.record_memory_uses([mem_id], task_id="t3")
    await store.fill_memory_use_outcomes("t1", "success")
    await store.fill_memory_use_outcomes("t2", "success")
    await store.fill_memory_use_outcomes("t3", "failure")

    report = await store.memory_usage_report()
    assert len(report) == 1
    row = report[0]
    assert row["memory_id"] == mem_id
    assert row["use_count"] == 3
    assert row["success_count"] == 2
    assert row["failure_count"] == 1
    assert row["cancelled_count"] == 0
    assert row["timeout_count"] == 0

    # Untouched memories (use_count 0) are excluded from the report — nothing
    # to report on.
    await store.add_memory(mem_type="rule", title="unused", content="x", confirmed=True)
    report2 = await store.memory_usage_report()
    assert len(report2) == 1


async def test_memory_outcome_counts_keyed_by_id_for_api_enrichment(store):
    mem_a = await store.add_memory(mem_type="rule", title="a", content="x", confirmed=True)
    mem_b = await store.add_memory(mem_type="rule", title="b", content="x", confirmed=True)
    await store.record_memory_uses([mem_a], task_id="t1")
    await store.fill_memory_use_outcomes("t1", "timeout")

    counts = await store.memory_outcome_counts([mem_a, mem_b])
    assert counts[mem_a]["timeout_count"] == 1
    assert counts[mem_a]["success_count"] == 0
    assert mem_b not in counts  # no ledger rows at all
    assert await store.memory_outcome_counts([]) == {}


def test_cli_learnings_usage_prints_counts_and_the_correlational_label(
        tmp_path, monkeypatch):
    # SYNC test, deliberately (mirrors test_supervisor_learning.py's
    # `test_the_listing_shows_scope_and_flags_a_global_row`): the CLI command
    # itself calls `asyncio.run(_go())`, which cannot nest inside the running
    # event loop pytest-asyncio would give an `async def` test.
    import asyncio

    import no_human.cli.commands as cmd_mod
    from no_human.cli.commands import cli
    from click.testing import CliRunner

    db_path = tmp_path / "learn.db"

    async def _seed():
        s = await Store(db_path).connect()
        mem_id = await s.add_memory(mem_type="rule", title="my rule", content="x",
                                    confirmed=True)
        await s.record_memory_uses([mem_id], task_id="t1")
        await s.fill_memory_use_outcomes("t1", "success")
        await s.close()
        return mem_id

    mem_id = asyncio.run(_seed())

    class _Cfg:
        db_path = tmp_path / "learn.db"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())

    result = CliRunner().invoke(cli, ["learnings", "--usage"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "Correlational metrics — not causal" in result.output
    assert mem_id[:8] in result.output
    assert "used 1x" in result.output
    assert "1 success" in result.output


async def test_api_rules_endpoint_carries_usage_counts(store):
    from no_human.api.app import _with_usage_counts

    mem_id = await store.add_memory(mem_type="rule", title="r", content="x",
                                    confirmed=True)
    await store.record_memory_uses([mem_id], task_id="t1")
    await store.fill_memory_use_outcomes("t1", "success")
    await store.record_memory_uses([mem_id], task_id="t2")
    await store.fill_memory_use_outcomes("t2", "failure")

    items = await store.list_memories(confirmed=True)
    enriched = await _with_usage_counts(store, items)
    row = next(r for r in enriched if r["id"] == mem_id)
    assert row["use_count"] == 2
    assert row["success_count"] == 1
    assert row["failure_count"] == 1
    assert row["cancelled_count"] == 0
    assert row["timeout_count"] == 0
