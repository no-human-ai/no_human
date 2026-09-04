"""Coder in-attempt cache burn (200-350 calls x ~170k context = 12-43M cache
per task, 2.3x the W29 median).

Three things this pins:

1. AC1 — the compaction-window config lever. `ClaudeBackend.compact_window_tokens`
   reaches the real `ClaudeAgentOptions.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]`
   (pinned against the SDK's own dataclass, not source text), `make_backend`
   resolves it from `bounds.coder_compact_window_tokens` with a fail-closed
   default, and the scope guard reaches ONLY the coder role.
2. AC2 — per-attempt cache telemetry. `_agent_sink`'s usage branch emits a
   `cache_burn` event at a cadence, carrying the running totals and the
   compaction count `_on_coder_compact` increments; `nh logs` surfaces it live
   for an attempt whose persisted cache columns are still 0/NULL.
3. The existing mid-attempt BudgetAbort watch is UNCHANGED by any of the above.
"""

from __future__ import annotations

import asyncio

import pytest

# `test_only_the_coder_role_gets_the_window` et al. build real `ClaudeBackend`
# instances through `make_backend` and read `_options()` — the real class over
# a mocked SDK client, never `.run()` — so this module is exempt from the
# hermetic ClaudeBackend stub by the same rule as `test_backend.py`.
pytestmark = pytest.mark.real_backend

from no_human.agent.backend import (
    DEFAULT_CODER_COMPACT_WINDOW_TOKENS,
    make_backend,
)
from no_human.agent.child_env import scrub_foreign_secrets_into
from no_human.agent.claude_backend import AgentEvent, ClaudeBackend
from no_human.agent.session_mark import mark_env
from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.orchestrator import CODER_ROLE, BudgetAbort, Orchestrator
from no_human.core.task import TaskStatus
from no_human.notify.slack import SlackNotifier

# Every `ClaudeBackend._options()` call now also stamps the agent-session mark
# (`session_mark.mark_env`) — additive to whatever this module's own AC1
# assertions check, so every exact `opts.env == {...}` comparison below must
# include it alongside the compaction-window key(s) it actually pins.
_MARK = mark_env("claude")


def _expected_env(**deliberate):
    """The exact `ClaudeAgentOptions.env` `_options` builds: the deliberate
    keys, the mark, and — because `agent/child_env.py` overrides every
    secret-shaped variable of the launcher's environment to "" — one empty
    entry per such name in THIS process's env (so the assertion is exact in
    any shell, not only a secret-free one)."""
    return {**deliberate, **_MARK,
            **{name: "" for name in scrub_foreign_secrets_into({})}}

from .test_cli_commands import _make_runner, _seed_attempt, _seed_task
from .test_e2e_orchestrator import _config  # noqa: F401


# --------------------------------------------------------------------------- #
# AC1 — the compaction-window config lever                                    #
# --------------------------------------------------------------------------- #

def test_compact_window_reaches_sdk_options(tmp_path):
    backend = ClaudeBackend(model="claude-sonnet-4-6", compact_window_tokens=140_000)
    opts = backend._options(tmp_path, max_turns=5)
    assert opts.env == _expected_env(CLAUDE_CODE_AUTO_COMPACT_WINDOW="140000")


def test_no_window_means_no_env_override(tmp_path):
    """Every OTHER construction site (reviewer/planner/utility/supervisor/
    distill) passes nothing — unchanged behaviour."""
    backend = ClaudeBackend(model="claude-sonnet-4-6")
    opts = backend._options(tmp_path, max_turns=5)
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in (opts.env or {})


def _window_env(backend, tmp_path):
    return backend._options(tmp_path, max_turns=5).env or {}


def test_only_the_coder_role_gets_the_window(tmp_path):
    cfg = {"bounds": {"coder_compact_window_tokens": 123456}}

    coder = make_backend(model="m", config=cfg, role="coder")
    assert _window_env(coder, tmp_path) == _expected_env(CLAUDE_CODE_AUTO_COMPACT_WINDOW="123456")

    reviewer = make_backend(model="m", config=cfg, role="reviewer")
    assert _window_env(reviewer, tmp_path) == _expected_env()

    planner = make_backend(model="m", config=cfg, role="planner")
    assert _window_env(planner, tmp_path) == _expected_env()

    readonly_coder = make_backend(model="m", config=cfg, role="coder", readonly=True)
    assert _window_env(readonly_coder, tmp_path) == _expected_env()


def test_default_window_applied_when_key_absent(tmp_path):
    backend = make_backend(model="m", config={}, role="coder")
    assert _window_env(backend, tmp_path) == _expected_env(
        CLAUDE_CODE_AUTO_COMPACT_WINDOW=str(DEFAULT_CODER_COMPACT_WINDOW_TOKENS))


@pytest.mark.parametrize("bad", ["abc", 0, -1])
def test_invalid_window_does_not_reach_the_cli(tmp_path, bad):
    cfg = {"bounds": {"coder_compact_window_tokens": bad}}
    backend = make_backend(model="m", config=cfg, role="coder")
    assert _window_env(backend, tmp_path) == _expected_env(
        CLAUDE_CODE_AUTO_COMPACT_WINDOW=str(DEFAULT_CODER_COMPACT_WINDOW_TOKENS))


# --------------------------------------------------------------------------- #
# AC2 — per-attempt cache telemetry                                           #
# --------------------------------------------------------------------------- #

class _NoBackend:
    async def run(self, *a, **k):  # pragma: no cover — must never be reached
        raise AssertionError("the coder backend should not run in a sink-level test")


def _orch(store, tmp_path, events=None):
    cfg = _config(tmp_path)
    return Orchestrator(
        store, cfg.data, _NoBackend(), SlackNotifier(None),
        event_sink=(events.append if events is not None else None),
    )


def test_sink_emits_cache_burn_with_running_totals(store, tmp_path):
    events = []
    orch = _orch(store, tmp_path, events=events)
    orch._active_task_id = "task-1"
    orch._active_attempt_number = 3
    orch._begin_attempt_accounting("task-1", remaining_tokens=10_000_000)
    ev = AgentEvent("usage", meta={
        "tokens_used": 100, "cache_read_tokens": 200, "cache_creation_tokens": 50,
    })
    for _ in range(9):
        orch._agent_sink(ev, role=CODER_ROLE)
    assert not any(e.get("kind") == "cache_burn" for e in events), \
        "the cadence is every 10th message — nothing should fire yet"

    orch._agent_sink(ev, role=CODER_ROLE)  # the 10th message

    burns = [e for e in events if e.get("kind") == "cache_burn"]
    assert len(burns) == 1
    burn = burns[0]
    assert burn["cache_read"] == 2_000
    assert burn["cache_creation"] == 500
    assert burn["tokens_used"] == 1_000
    assert burn["total"] == 3_500
    assert burn["attempt_number"] == 3
    assert burn["compactions"] == 0

    # `_attempt_usage` keeps summing every event, unchanged.
    assert orch._attempt_usage["assistant_messages"] == 10
    assert orch._attempt_usage["cache_read_tokens"] == 2_000
    assert orch._attempt_usage["cache_creation_tokens"] == 500
    assert orch._attempt_usage["tokens_used"] == 1_000


def test_budget_abort_threshold_unchanged_by_cache_burn_telemetry(store, tmp_path):
    """The mid-attempt BudgetAbort watch (B2 #2) must fire at exactly the same
    point it always has — one event past the ceiling still raises — with the
    new telemetry present."""
    events = []
    orch = _orch(store, tmp_path, events=events)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=500)
    ev = AgentEvent("usage", meta={
        "tokens_used": 300, "cache_read_tokens": 300, "cache_creation_tokens": 0,
    })
    orch._agent_sink(ev, role=CODER_ROLE)  # weighted 330 — under the ceiling
    with pytest.raises(BudgetAbort):
        orch._agent_sink(ev, role=CODER_ROLE)  # weighted 660 — over


def test_cache_burn_not_emitted_for_non_coder_roles(store, tmp_path):
    events = []
    orch = _orch(store, tmp_path, events=events)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=10_000_000)
    ev = AgentEvent("usage", meta={
        "tokens_used": 100, "cache_read_tokens": 200, "cache_creation_tokens": 50,
    })
    for _ in range(20):
        orch._agent_sink(ev, role="reviewer")
    assert not any(e.get("kind") == "cache_burn" for e in events)


def test_compaction_counter_reported(store, tmp_path):
    events = []
    orch = _orch(store, tmp_path, events=events)
    orch._active_task_id = "task-1"
    orch._active_attempt_number = 1
    orch._begin_attempt_accounting("task-1", remaining_tokens=10_000_000)

    orch._on_coder_compact("auto")
    orch._on_coder_compact("auto")

    compaction_events = [e for e in events if e.get("kind") == "compaction"]
    assert len(compaction_events) == 2
    assert compaction_events[-1]["compactions"] == 2

    ev = AgentEvent("usage", meta={
        "tokens_used": 1, "cache_read_tokens": 1, "cache_creation_tokens": 1,
    })
    for _ in range(10):
        orch._agent_sink(ev, role=CODER_ROLE)

    burns = [e for e in events if e.get("kind") == "cache_burn"]
    assert burns[-1]["compactions"] == 2


# --------------------------------------------------------------------------- #
# `nh logs` — the live fallback                                               #
# --------------------------------------------------------------------------- #

def _seed_cache_burn_event(db_path, task_id, **fields):
    async def _go():
        async with Store(db_path) as s:
            event = {
                "kind": "cache_burn", "source": CODER_ROLE, "text": "cache burn",
                "ts": 1000.0, "attempt_number": 1, "cache_read": 0,
                "cache_creation": 0, "tokens_used": 0, "total": 0,
                "compactions": 0,
            }
            event.update(fields)
            await s.save_events(task_id, [event])
    asyncio.run(_go())


def test_nh_logs_shows_live_cache_for_an_unfinished_attempt(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, title="Live attempt")
    _seed_attempt(db, task_id, status="running")  # cache columns default to 0
    _seed_cache_burn_event(
        db, task_id, cache_read=88_000, cache_creation=12_000,
        tokens_used=500, compactions=1,
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "cache (live): read 88,000 · creation 12,000" in result.output
    assert "not yet reported" not in result.output
    assert "compactions 1" in result.output


def test_nh_logs_says_not_yet_reported_with_no_cache_burn_event(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING, title="No telemetry yet")
    _seed_attempt(db, task_id, status="running")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "cache: not yet reported" in result.output
    # Never a bare 0 dressed up as a real measurement.
    assert "cache (live)" not in result.output
