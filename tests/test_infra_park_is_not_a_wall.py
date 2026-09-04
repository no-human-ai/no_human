"""A dead SDK session parks the TASK, never the POOL.

INCIDENT (2026-08-22 11:13 local, task c8d1a30d): after 65 tool calls the
SDK transport died on one stdout JSON line over its 1 MB default buffer
("JSON message exceeded maximum buffer size") — zero tokens, turn 1. The
attempt was rightly spared (`_infra_sdk_failure` -> `QuotaExhausted` ->
`_park_quota`), but the park is the SAME shape as a billing wall, so
`Scheduler._run` (step 7.4) armed a 60-minute 'quota' pause on the whole pool
with twelve tasks queued and a worker free, and `recover_quota_cooldown` /
`_corroborated_quota_wall` would have read the row as a wall on restart.

The fleet response to dead sessions already exists — the 3-strike infra
breaker — so a single infra park must be invisible to every pool clock.
Two layers: `QuotaExhausted(infra=True)` is stamped onto the blocker, and
the three pool-side readers skip it; and the backend raises the transport's
buffer so the line that killed this session fits.

Idiom: `tests/test_infra_not_work.py` (scripted backend through the real
`_run_attempt`) and `tests/test_scheduler_quota_recovery.py` (store-level
parks driven through the real scheduler).
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from no_human.agent import claude_backend
from no_human.agent.claude_backend import AgentResult, ClaudeBackend
from no_human.config import load_config
from no_human.core.bounds import QuotaExhausted
from no_human.core.infra_breaker import infra_breaker
from no_human.core.orchestrator import Orchestrator
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

_INCIDENT_TEXT = (
    "Failed to decode JSON: JSON message exceeded maximum buffer size of "
    "1048576 bytes"
)


@pytest.fixture(autouse=True)
def _clean_infra_breaker_singleton():
    infra_breaker().reset()
    yield
    infra_breaker().reset()


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
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


class _ScriptedBackend:
    def __init__(self, result: AgentResult):
        self._result = result
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        return self._result


def _dead_session() -> AgentResult:
    return AgentResult(
        final_text=_INCIDENT_TEXT, num_turns=1, is_error=True, tokens_used=0,
        session_id=None, stop_reason="error", cache_read_tokens=0,
        cache_creation_tokens=0,
    )


async def _orch_and_task(store, bare_repo, tmp_path, result):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    backend = _ScriptedBackend(result)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    return orch, backend, task, GitRepo(bare_repo)


# --------------------------------------------------------------------------- #
# producer side: the raise carries the kind, the park records it             #
# --------------------------------------------------------------------------- #


async def test_a_dead_sdk_session_raises_an_infra_flagged_park(
        store, bare_repo, tmp_path):
    """RED before the fix: `QuotaExhausted` has no `infra` attribute, so the
    park is indistinguishable from a billing wall."""
    orch, backend, task, repo = await _orch_and_task(
        store, bare_repo, tmp_path, _dead_session())

    with pytest.raises(QuotaExhausted) as raised:
        await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert raised.value.infra is True, (
        "a prose-less SDK death must raise an INFRA park, not a wall")
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, "the attempt must still be spared"

    outcome = await orch._park_quota(task, raised.value, repo=repo)
    parked = await store.get_task(task.id)
    assert outcome.status == TaskStatus.PAUSED_QUOTA
    assert parked.blocker["infra"] is True, (
        "the park must STAMP its kind — the scheduler reads the row, not "
        "the exception")


def test_a_billing_wall_is_not_infra_by_default():
    exc = QuotaExhausted("You've hit your session limit")
    assert exc.infra is False
    assert QuotaExhausted("dead", infra=True).infra is True


# --------------------------------------------------------------------------- #
# pool side: three readers of paused_quota rows                              #
# --------------------------------------------------------------------------- #


class _ParkingOrch:
    """Parks the first task `paused_quota` with the given blocker, the way
    `_park_quota` leaves the row, and returns the outcome `_run` reads."""

    def __init__(self, store, blocker, resets_at):
        self.store = store
        self.blocker = blocker
        self.resets_at = resets_at
        self._sink = None

    async def run_task(self, task):
        task.wake_check_at = self.resets_at
        task.blocker = self.blocker
        await self.store.update_task_columns(task)
        await self.store.set_status(task, TaskStatus.PAUSED_QUOTA, validate=False)
        return SimpleNamespace(status=TaskStatus.PAUSED_QUOTA, task=task)


def _park_blocker(*, infra: bool, raised_at: datetime) -> dict:
    return {
        "category": "QUOTA", "wake_condition": "quota_refreshed",
        "raised_at": raised_at.isoformat(), "confidence": 1.0,
        "root_cause_hypothesis": "x", "auth_profile": None, "infra": infra,
    }


@pytest.mark.parametrize("infra, armed", [(True, False), (False, True)])
async def test_step_7_4_arms_the_pool_clock_only_for_a_wall(store, infra, armed):
    """RED before the fix for infra=True: `_run` armed the cooldown on every
    PAUSED_QUOTA outcome. The infra=False leg pins that a real wall still
    pauses the pool — this change must not weaken the 2026-08-20 fix."""
    now = datetime.now(timezone.utc)
    resets = (now + timedelta(minutes=50)).isoformat()
    blocker = _park_blocker(infra=infra, raised_at=now)
    events: list = []
    sched = Scheduler(store, lambda task: _ParkingOrch(store, blocker, resets),
                      max_workers=1,
                      on_event=lambda k, txt: events.append((k, txt)))
    t = Task.new("first", repo_path="/tmp/x")
    await store.create_task(t)

    await sched.tick()
    await sched.wait_idle()

    assert (await store.get_task(t.id)).status == TaskStatus.PAUSED_QUOTA
    assert (sched._quota_cooldown_until is not None) is armed, (
        "an infra park armed the pool clock" if infra
        else "a billing wall no longer pauses the pool")
    assert any(k == "quota_pause" for k, _ in events) is armed


async def test_startup_recovery_ignores_an_infra_park(store):
    """RED before the fix: the row is PAUSED_QUOTA with a future wake, so a
    restarted scheduler re-armed an hour of idle from a dead session."""
    now = datetime.now(timezone.utc)
    t = Task.new("dead session", repo_path="/tmp/x")
    await store.create_task(t)
    t.blocker = _park_blocker(infra=True, raised_at=now)
    t.wake_check_at = (now + timedelta(minutes=50)).isoformat()
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.PAUSED_QUOTA, validate=False)
    sched = Scheduler(store, lambda task: None, max_workers=1)

    assert await sched.recover_quota_cooldown() is None
    assert sched._quota_cooldown_until is None


async def test_an_infra_park_corroborates_no_bare_shape_death(store):
    """RED before the fix: `_corroborated_quota_wall` returned the infra
    park as 'the same wall' and the reviewer death was re-timed to it."""
    now = datetime.now(timezone.utc)
    t = Task.new("dead session", repo_path="/tmp/x")
    await store.create_task(t)
    t.blocker = _park_blocker(infra=True, raised_at=now - timedelta(minutes=1))
    t.wake_check_at = (now + timedelta(hours=1)).isoformat()
    await store.update_task_columns(t)
    await store.set_status(t, TaskStatus.PAUSED_QUOTA, validate=False)
    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store

    assert await orch._corroborated_quota_wall() is None


# --------------------------------------------------------------------------- #
# the line that killed the session must fit                                  #
# --------------------------------------------------------------------------- #


def test_backend_raises_the_transport_buffer_above_the_sdk_default(tmp_path):
    """RED before the fix: `_options` leaves `max_buffer_size` None and the
    SDK falls back to 1 MiB — the exact cap the incident line exceeded."""
    opts = ClaudeBackend(model="claude-sonnet-5")._options(tmp_path, 40)
    cap = getattr(claude_backend, "SDK_MAX_BUFFER_BYTES", None)
    assert cap is not None and cap > 1024 * 1024
    assert opts.max_buffer_size == cap
