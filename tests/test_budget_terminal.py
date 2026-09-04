"""BUDGET-TERMINAL: an exhausted lifetime budget ENDS the task.

Measured 2026-08-09: of 119 tasks parked waiting on a human, 69 were the same
question — "this task has exhausted its lifetime budget. Spend more, or stop
here?" — and the operator's answer never varied:

    THE ANSWER IS "STOP". NEVER RAISE A CAP. Budget raises have never once
    produced a merge on this project. An exhausted budget means the TICKET is
    wrong. Answer "stop", then rewrite it inline-complete and re-file.

A question whose answer is invariant standing policy is the product's problem,
not the operator's. `budget.exhaustion_terminal` (default on) mechanizes the
answer: the task parks HONESTLY — status `failed`, the full structured blocker
record kept, and a wake condition naming exactly what a human would have to do —
and asks nothing.

What this does NOT change, and what these tests hold to it:

  * the CAP is untouched (`bounds.lifetime_*`), resume spend still counts
    against it, and raising one is still a human-only act;
  * every other blocker category routes exactly as before;
  * nothing automatic may revive a budget-failed task.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from no_human.blockers import Blocker, BlockerCategory, WakeWatcher, triage
from no_human.config import DEFAULT_CONFIG, load_config
from no_human.core.db import Store
from no_human.core.scheduler import _CLAIMABLE
from no_human.core.task import TERMINAL_STATES, Task, TaskStatus
from no_human.notify.slack import SlackNotifier

ASK_THE_HUMAN = {"budget": {"exhaustion_terminal": False}}


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover - never reached here
        raise AssertionError("no backend call belongs in these tests")


class _RecordingNotifier(SlackNotifier):
    def __init__(self):
        super().__init__(None)
        self.sent: list[tuple] = []

    def notify(self, kind, text):  # noqa: D102
        self.sent.append((kind, text))


def _orch(store, tmp_path, notifier=None, *, config_over=None):
    from no_human.core.orchestrator import Orchestrator

    cfg = load_config(tmp_path / "config.yaml").data
    for section, values in (config_over or {}).items():
        cfg[section] = {**cfg.get(section, {}), **values}
    return Orchestrator(store, cfg, _Backend(), notifier or SlackNotifier(None))


async def _exhausted(store, title="over budget"):
    """A task whose lifetime attempt cap is spent.

    Each row is closed with a real terminal status and nonzero tokens —
    mirroring how a real attempt loop closes a row before the next one
    starts. Left bare, `create_attempt`'s own supersede sweep would tag the
    older row `status="interrupted"` with zero recorded work, which is the
    2026-08-20 DEAD-worker shape and is now excluded from the lifetime cap
    by design (see THE BOUNDARY in `Store.lifetime_usage_by_class`); a
    helper meant to represent a genuinely spent cap must not collide with
    that exclusion by accident.
    """
    t = Task.new(title, repo_path="/tmp/r")
    t.config = {"lifetime_attempts": 2}
    await store.create_task(t)
    for n in (1, 2):
        aid = await store.create_attempt(t.id, n)
        await store.update_attempt(aid, status="failed", tokens_used=100)
    return t


# --------------------------------------------------------------------------- #
# The default: exhaustion is terminal, and says so in full.
# --------------------------------------------------------------------------- #

def test_the_default_is_on_and_is_the_operator_rule():
    """Default-ON is the whole point: 69 of 119 parked tasks were this one
    question. An install that has to opt IN to the fix keeps asking it."""
    assert DEFAULT_CONFIG["budget"]["exhaustion_terminal"] is True


async def test_exhaustion_fails_the_task_with_a_record_and_no_question(
    store, tmp_path
):
    t = await _exhausted(store)
    notifier = _RecordingNotifier()
    orch = _orch(store, tmp_path, notifier)

    blocker = await orch._check_lifetime_budget(t)
    assert blocker is not None
    outcome = await orch._raise_blocker(t, blocker)

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert outcome.status is TaskStatus.FAILED
    # It came off the blocker funnel — not a plain attempt failure the bounded
    # loop may retry (see `_drive`).
    assert outcome.off_ramp is True

    # NO QUESTION. This is the whole change.
    rec = fresh.blocker or {}
    assert rec.get("question") is None
    assert rec.get("options") == []
    assert notifier.sent == [], "a terminal budget park must not page a human"

    # ...and the record is self-explanatory without one: what it was doing, what
    # it spent against what cap, and what would legitimately revive it.
    assert rec["category"] == "BUDGET_EXHAUSTED"
    assert rec["goal"] == t.title
    assert "attempts 2/2" in rec["root_cause_hypothesis"]
    assert "lifetime spend:" in rec["evidence"] and "caps:" in rec["evidence"]
    wake = rec["wake_condition"]
    assert "nh task config" in wake and "nh task retry" in wake
    assert "refile" in wake.lower()
    assert "Nothing automatic will restart it." in wake


async def test_the_event_says_failed_not_escalated(store, tmp_path):
    """The timeline must not claim a human was asked. `_raise_blocker`'s event
    kind is derived from the ROUTE, and the map had no FAILED entry — it would
    have fallen through to the "escalated" default on the one change whose
    point is that nobody was asked."""
    t = await _exhausted(store)
    events: list = []
    orch = _orch(store, tmp_path)
    orch._sink = events.append

    await orch._raise_blocker(t, await orch._check_lifetime_budget(t))

    kinds = [e["kind"] for e in events]
    assert "failed" in kinds and "escalated" not in kinds, kinds


async def test_the_cap_itself_is_untouched(store, tmp_path):
    """Terminal is an OUTCOME change. The gate still fires at the same place on
    the same numbers, and a task under its cap is still untouched."""
    from no_human.core.bounds import Bounds

    assert Bounds().lifetime_attempts == DEFAULT_CONFIG["bounds"]["lifetime_attempts"]
    assert Bounds().lifetime_tokens == DEFAULT_CONFIG["bounds"]["lifetime_tokens"]

    t = Task.new("under budget", repo_path="/tmp/r")
    t.config = {"lifetime_attempts": 5}
    await store.create_task(t)
    await store.create_attempt(t.id, 1)
    assert await _orch(store, tmp_path)._check_lifetime_budget(t) is None


# --------------------------------------------------------------------------- #
# The off-switch: an operator who wants to be asked still is.
# --------------------------------------------------------------------------- #

async def test_the_off_switch_restores_the_question_and_the_options(
    store, tmp_path
):
    t = await _exhausted(store)
    notifier = _RecordingNotifier()
    orch = _orch(store, tmp_path, notifier, config_over=ASK_THE_HUMAN)

    outcome = await orch._raise_blocker(t, await orch._check_lifetime_budget(t))

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert outcome.status is TaskStatus.ESCALATED
    rec = fresh.blocker or {}
    assert "Spend more, or stop here?" in (rec.get("question") or "")
    labels = [o["label"] for o in rec["options"]]
    assert any("raise the budget" in lbl for lbl in labels), labels
    assert any(o["action"] == {"park": True} for o in rec["options"])
    assert rec.get("wake_condition") is None
    assert [k for k, _ in notifier.sent] == ["stuck"], notifier.sent


# --------------------------------------------------------------------------- #
# Routing: only this one category moved.
# --------------------------------------------------------------------------- #

def _b(category, **kw):
    return Blocker(category=category, confidence=1.0, **kw)


def test_only_budget_exhausted_moved():
    """The `budget_exhaustion_terminal` flag is scoped to BUDGET_EXHAUSTED alone
    — the guard that matters is that every OTHER category in the taxonomy,
    including USER_PAUSED, routes identically with the flag on and off."""
    for cat in BlockerCategory:
        on = triage(_b(cat), budget_exhaustion_terminal=True)
        off = triage(_b(cat), budget_exhaustion_terminal=False)
        if cat is BlockerCategory.BUDGET_EXHAUSTED:
            assert on.target_status is TaskStatus.FAILED
            assert off.target_status is TaskStatus.ESCALATED
        else:
            assert on == off, cat


def test_the_terminal_route_is_silent_and_unparked():
    route = triage(_b(BlockerCategory.BUDGET_EXHAUSTED))
    assert route.target_status is TaskStatus.FAILED
    # parked=False: a parked route gets a `wake_check_at` and the watcher polls
    # it. This one must never be polled.
    assert route.parked is False
    # notify_now=False: the interruption is exactly what is being removed.
    assert route.notify_now is False
    assert route.auto_retry is False


# --------------------------------------------------------------------------- #
# Nothing automatic may revive it.
# --------------------------------------------------------------------------- #

def test_the_scheduler_cannot_reclaim_a_failed_task():
    """`_CLAIMABLE` is the scheduler's whole claim vocabulary (scheduler.py:
    `for status in _CLAIMABLE`). FAILED is not in it and must not become so —
    that is the property that makes FAILED, rather than any parked state, the
    right terminus for a decision nobody is going to make."""
    assert _CLAIMABLE == (TaskStatus.IMPLEMENTING, TaskStatus.PENDING)
    assert TaskStatus.FAILED not in _CLAIMABLE
    assert TaskStatus.FAILED in TERMINAL_STATES


async def test_the_wake_watcher_never_touches_a_budget_failed_task(
    store, tmp_path
):
    """The blocker carries a `wake_condition`, deliberately — it is the field
    that answers "and now what?". A prose condition on a record the watcher DID
    sweep would be an auto-resume waiting to happen, so prove it is not swept:
    due stamp, real blocker, tick does nothing."""
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    t = await _exhausted(store)
    orch = _orch(store, tmp_path)
    await orch._raise_blocker(t, await orch._check_lifetime_budget(t))

    # Force the two things a sweep would need, so the test cannot pass merely
    # because they happen to be unset.
    fresh = await store.get_task(t.id)
    assert fresh.wake_check_at is None, "a non-parked route must not arm a wake"
    fresh.wake_check_at = (now - timedelta(hours=1)).isoformat()
    await store.update_task(fresh)

    actions = await WakeWatcher(
        store, {"blockers": {"max_park_duration": "48h"}}).tick(now=now)

    assert actions == [], actions
    assert (await store.get_task(t.id)).status is TaskStatus.FAILED


# --- review finding: restore-approval must not "repair" a cancelled task --- #

def test_restore_approval_refuses_a_cancelled_task(tmp_path):
    """Review-proven defect on this branch's first cut: a CANCELLED task
    (FAILED + cancel_reason) with an old PR passed every guard, then the
    terminal CAS silently refused the transition while the repair event and
    blocker wipe went through — a recorded transition that never happened.
    A human's explicit cancel is never the parked-PR incident this verb
    repairs. Sync test: the CLI command owns its own asyncio.run."""
    import asyncio as _asyncio
    import unittest.mock as mock

    from click.testing import CliRunner

    from no_human.cli import commands as cmd_mod

    db = tmp_path / "restore.db"

    async def _setup():
        async with Store(db) as store:
            t = Task.new("cancelled with an old PR", repo_path="/tmp/x")
            t.context = {"pr_watch": {"url": "https://example.invalid/pr/1"},
                         "cancel_reason": "Cancelled from board"}
            await store.create_task(t)
            await store.save_events(t.id, [{
                "source": "test", "kind": "pr_open", "text": "", "ts": 0.0}])
            await store.set_status(t, TaskStatus.FAILED, validate=False,
                                   human_override=True)
            return t.id

    tid = _asyncio.run(_setup())

    class _Cfg:
        db_path = db

    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=False: (_Cfg(), None)):
        result = CliRunner().invoke(
            cmd_mod.task_restore_approval, [tid, "--reason", "probe"])

    assert result.exit_code == 1, result.output
    assert "cancelled by a human" in result.output

    async def _check():
        async with Store(db) as store:
            fresh = await store.get_task(tid)
            events = await store.list_events(tid)
            return fresh.status, [e.get("kind") for e in events]

    status, kinds = _asyncio.run(_check())
    assert status is TaskStatus.FAILED, "status must not move"
    assert "state_repaired" not in kinds, (
        "no repair event may exist for a refused repair")
    assert "human_restore_approval" not in kinds, (
        "no repair event may exist for a refused repair")


# --- restore-approval must clear the blocker's wake condition (2026-08-11 --- #
# --- incident: a stale escalation blocker resumed a just-landed task).    --- #

def test_restore_approval_disarms_the_wake_condition(tmp_path):
    """A task escalated with an armed wake condition (`after:2h` +
    `wake_check_at`), then restore-approval'd back to `awaiting_approval`,
    must not leave that condition armed — or the next watcher tick can still
    resume it (the live 2026-08-11 incident). Sync test: the CLI command
    owns its own asyncio.run."""
    import asyncio as _asyncio
    import unittest.mock as mock

    from click.testing import CliRunner

    from no_human.cli import commands as cmd_mod

    db = tmp_path / "restore.db"

    async def _setup():
        async with Store(db) as store:
            t = Task.new("escalated with an open PR", repo_path="/tmp/x")
            t.context = {"pr_watch": "https://example.invalid/pr/1"}
            t.blocker = {"category": "DEPENDENCY_WAIT",
                         "wake_condition": "after:2h",
                         "raised_at": "2026-08-11T20:00:00+00:00",
                         "confidence": 0.9}
            t.wake_check_at = "2026-08-11T22:00:00+00:00"
            await store.create_task(t)
            await store.save_events(t.id, [{
                "source": "test", "kind": "pr_open", "text": "", "ts": 0.0}])
            await store.set_status(t, TaskStatus.ESCALATED, validate=False,
                                   human_override=True)
            return t.id

    tid = _asyncio.run(_setup())

    class _Cfg:
        db_path = db

    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=False: (_Cfg(), None)):
        result = CliRunner().invoke(
            cmd_mod.task_restore_approval, [tid, "--reason", "probe"])

    assert result.exit_code == 0, result.output

    async def _check():
        async with Store(db) as store:
            fresh = await store.get_task(tid)
            events = await store.list_events(tid)
            return fresh, events

    fresh, events = _asyncio.run(_check())
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert fresh.blocker is None
    assert fresh.wake_check_at is None
    repaired = [e for e in events if e.get("kind") == "human_restore_approval"]
    assert len(repaired) == 1
    assert "after:2h" in repaired[0]["text"]
    assert "2026-08-11T22:00:00+00:00" in repaired[0]["text"]
