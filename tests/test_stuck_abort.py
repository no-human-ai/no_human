"""Deterministic runaway abort (docs/ARCH_REVIEW.md B2 #1 + #2).

The stuck/doom-loop detectors used to emit telemetry and let the attempt run
on — the LLM supervisor, which fails open, held the only abort authority — so
a recognized loop could burn the full 500-turn budget (live precedent: 3.4M
cache-read in 41 turns, ~12× headroom at the current cap). And the lifetime
token cap was checked only at attempt boundaries, so one attempt could blow
through the whole 8M unwatched.

These tests pin the deterministic teeth:

1. a HARD detector fire (far above the advisory thresholds) raises StuckAbort
   at the next event boundary → the attempt FAILS with its work checkpointed
   and the bounded loop retries with fresh context — the task is not parked;
2. per-turn usage events accumulate in the sink and raise BudgetAbort the
   moment the attempt crosses the task's remaining lifetime budget → the
   attempt records its true spend and the task parks behind the same
   BUDGET_EXHAUSTED blocker the boundary check raises.
"""

import subprocess

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.core.bounds import StuckDetector
from no_human.core.orchestrator import (
    CODER_ROLE,
    BudgetAbort,
    Orchestrator,
    StuckAbort,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401


def _mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )


def _orch(store, tmp_path, backend=None, events=None, bounds=None):
    cfg = _config(tmp_path)
    if bounds:
        cfg.data.setdefault("bounds", {}).update(bounds)
    return Orchestrator(
        store, cfg.data, backend or FakeBackend(_mutate), SlackNotifier(None),
        event_sink=(events.append if events is not None else None),
    )


# ------------------------------ the sink ----------------------------------- #


def test_sink_aborts_on_hard_doom_loop(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._stuck = StuckDetector()
    ev = AgentEvent("tool_use", tool_name="Bash", tool_input={"command": "pytest -x"})
    with pytest.raises(StuckAbort, match="doom-loop"):
        for _ in range(orch._stuck.doom_loop_abort):
            orch._agent_sink(ev, role=CODER_ROLE)


def test_sink_never_aborts_below_the_hard_threshold(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._stuck = StuckDetector()
    ev = AgentEvent("tool_use", tool_name="Bash", tool_input={"command": "pytest -x"})
    for _ in range(orch._stuck.doom_loop_abort - 1):
        orch._agent_sink(ev, role=CODER_ROLE)  # advisory only — must not raise


@pytest.mark.parametrize("role", ["planner", "reviewer", "aggregator"])
def test_only_the_implementer_session_stuck_aborts(store, tmp_path, role):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._stuck = StuckDetector()
    ev = AgentEvent("tool_use", tool_name="Bash", tool_input={"command": "pytest -x"})
    for _ in range(orch._stuck.doom_loop_abort + 3):
        orch._agent_sink(ev, role=role)  # must not raise


def test_sink_aborts_when_spend_crosses_the_remaining_budget(store, tmp_path):
    # The ceiling is in COST-WEIGHTED tokens (core.pricing), so each event is
    # worth 300 fresh x1.0 + 300 cache-read x0.1 = 330, not its raw 600.
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=500)
    ev = AgentEvent("usage", meta={"tokens_used": 300, "cache_read_tokens": 300,
                                   "cache_creation_tokens": 0})
    orch._agent_sink(ev, role=CODER_ROLE)  # 330 — under the ceiling
    with pytest.raises(BudgetAbort):
        orch._agent_sink(ev, role=CODER_ROLE)  # 660 — over


def test_budget_ceiling_is_scoped_to_the_running_task(store, tmp_path):
    """The worker pool reuses one Orchestrator — task B's usage must never be
    charged against task A's ceiling (same scoping rule as _cancel_reason)."""
    orch = _orch(store, tmp_path)
    orch._begin_attempt_accounting("task-1", remaining_tokens=100)
    orch._active_task_id = "task-2"
    ev = AgentEvent("usage", meta={"tokens_used": 500, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0})
    orch._agent_sink(ev, role=CODER_ROLE)  # must not raise


def test_cache_creation_counts_toward_the_cap(store, tmp_path):
    """The running total must count what the lifetime ledger counts, or the two
    gates disagree.

    This assertion was INVERTED until db.lifetime_usage was corrected: it
    required a 5,000-token cache-creation burn against a 1,000 ceiling NOT to
    abort, pinning the very blind spot that let cache creation — a billed
    bucket, 16.2% of true spend once the reviewer/planner/utility tiers are
    included — accumulate unwatched. Same intent as before, now anchored to a
    ledger that counts all twelve columns.
    """
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=1_000)
    ev = AgentEvent("usage", meta={"tokens_used": 100, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 5_000})
    with pytest.raises(BudgetAbort):
        orch._agent_sink(ev, role=CODER_ROLE)


def test_spend_below_the_ceiling_still_does_not_abort(store, tmp_path):
    """The counterpart: counting more buckets must not make the watch
    trigger-happy — under the ceiling is still under the ceiling."""
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=10_000)
    ev = AgentEvent("usage", meta={"tokens_used": 100, "cache_read_tokens": 200,
                                   "cache_creation_tokens": 300})
    orch._agent_sink(ev, role=CODER_ROLE)  # must not raise


# ------------------------------ the backend -------------------------------- #


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
# over a mocked SDK client — the hermetic stub must not replace it.
async def test_stream_yields_usage_events_per_assistant_message(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage
    from claude_agent_sdk.types import TextBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend

    msg = AssistantMessage(
        content=[TextBlock(text="working…")], model="claude-sonnet-5",
        usage={"input_tokens": 1_000, "output_tokens": 200,
               "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 7},
    )

    async def _q(*args, **kwargs):
        yield msg

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]

    usage = [e for e in events if e.kind == "usage"]
    assert len(usage) == 1
    assert usage[0].meta["tokens_used"] == 1_200
    assert usage[0].meta["cache_read_tokens"] == 50_000
    assert usage[0].meta["cache_creation_tokens"] == 7


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
# over a mocked SDK client — the hermetic stub must not replace it.
async def test_per_message_sum_equals_result_cumulative(tmp_path, monkeypatch):
    """Review F2: an ABORTED attempt records the per-message SUM while a normal
    attempt records the ResultMessage cumulative. Pin our arithmetic: on the
    same stream, the accumulated usage events must equal what the result event
    reports — if the SDK's semantics ever drift, this is the tripwire."""
    from claude_agent_sdk import AssistantMessage, ResultMessage
    from claude_agent_sdk.types import TextBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend

    msgs = [
        AssistantMessage(
            content=[TextBlock(text=f"turn {i}")], model="claude-sonnet-5",
            usage={"input_tokens": 100 * i, "output_tokens": 10 * i,
                   "cache_read_input_tokens": 1_000 * i,
                   "cache_creation_input_tokens": i},
        )
        for i in (1, 2, 3)
    ]
    total_in_out = sum(100 * i + 10 * i for i in (1, 2, 3))
    total_read = sum(1_000 * i for i in (1, 2, 3))
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=3, session_id="s", total_cost_usd=0.0,
        usage={"input_tokens": 600, "output_tokens": 60,
               "cache_read_input_tokens": 6_000,
               "cache_creation_input_tokens": 6},
        result="done",
    )

    async def _q(*args, **kwargs):
        for m in msgs:
            yield m
        yield result

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]

    usage_events = [e for e in events if e.kind == "usage"]
    summed = sum(e.meta["tokens_used"] for e in usage_events)
    summed_read = sum(e.meta["cache_read_tokens"] for e in usage_events)
    (result_ev,) = [e for e in events if e.kind == "result"]

    assert summed == total_in_out == result_ev.meta["tokens_used"]
    assert summed_read == total_read == result_ev.meta["cache_read_tokens"]


# --------------------------- end to end ------------------------------------ #


class _DoomLoopThenFixBackend:
    """Attempt 1 doom-loops with WIP on disk; attempt 2 does the real work."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n# WIP\n")
            for _ in range(500):
                if on_event:
                    on_event(AgentEvent("tool_use", tool_name="Bash",
                                        tool_input={"command": "pytest -x"}))
            raise AssertionError("hard doom-loop never aborted the attempt")
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        _mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_hard_doom_loop_fails_the_attempt_and_the_loop_retries(
    store, bare_repo, tmp_path
):
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(task)

    backend = _DoomLoopThenFixBackend()
    orch = _orch(store, tmp_path, backend=backend)
    outcome = await orch.run_task(task)

    # ended the attempt, not the task: the bounded loop got its retry
    assert backend.calls == 2
    assert outcome.status is not TaskStatus.BLOCKED

    attempts = await store.list_attempts(task.id)
    first = attempts[0]
    assert first["status"] == "failed"
    assert "doom-loop" in (first["failure_reason"] or "")

    # attempt 1's work survived as a checkpoint (on attempt 1's own branch —
    # attempt 2 branches fresh from base, so it is not in HEAD's history)
    log = subprocess.run(
        ["git", "log", "--all", "--pretty=%s"], cwd=bare_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "[WIP-PARTIAL]" in log


class _TokenGusherBackend:
    """Burns tokens forever; only the mid-attempt cap can stop it."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        _mutate(cwd)
        for _ in range(500):
            if on_event:
                on_event(AgentEvent("usage", meta={
                    "tokens_used": 30_000, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0}))
        raise AssertionError("budget cross never aborted the attempt")


async def test_mid_attempt_budget_cross_parks_behind_budget_exhausted(
    store, bare_repo, tmp_path
):
    # `budget_unit` marks this override as already being in the weighted
    # unit. Without it the cutover guard reads a stored cap as pre-2026-07-31
    # RAW tokens and converts it (core.pricing.raw_cap_as_weighted), which is
    # right for the 165 real rows written before the cutover and wrong for a
    # fixture that means "a small weighted cap this backend will cross".
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.config = {"lifetime_tokens": 50_000, "budget_unit": "weighted"}
    await store.create_task(task)

    # This test's whole point is a MID-ATTEMPT cross — a fresh task's first
    # attempt must actually be allowed to start so the sink's own ceiling
    # check is what stops it. `min_viable_attempt_weighted_tokens` (the
    # loop-head startup floor, `_check_attempt_startup_floor`) would
    # otherwise refuse to start it first: a brand-new task has no measured
    # attempt history, so the floor falls back to the config default
    # (250,000), which this fixture's tiny lifetime cap can never clear.
    # Zeroing it neutralizes a gate this test isn't exercising, restoring
    # the pre-existing behavior this test was written to pin.
    orch = _orch(store, tmp_path, backend=_TokenGusherBackend(),
                 bounds={"min_viable_attempt_weighted_tokens": 0})
    outcome = await orch.run_task(task)

    assert outcome.status is not TaskStatus.DONE
    reloaded = await store.find_task(task.id)
    assert (reloaded.blocker or {}).get("category") == "BUDGET_EXHAUSTED"

    # the attempt's true spend was recorded — an aborted attempt must not
    # report zero tokens (that's how 21.2M once slipped past every cap)
    attempts = await store.list_attempts(task.id)
    assert attempts[0]["tokens_used"] >= 50_000


# ------------------- per-attempt token cap (v6 taxonomy) -------------------- #
# Four live specs burned the ENTIRE 8M lifetime budget in attempt #1 — the
# ceiling was armed with the remaining LIFETIME budget, so the bounded loop
# never got a second attempt. The attempt cap ends the ATTEMPT (work
# checkpointed, loop retries with fresh context); only the lifetime cap parks.


def test_sink_attempt_cap_beats_a_larger_remaining_budget(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting(
        "task-1", remaining_tokens=1_000_000, attempt_cap=1_000)
    ev = AgentEvent("usage", meta={"tokens_used": 600, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0})
    orch._agent_sink(ev, role=CODER_ROLE)  # 600 — under the cap
    with pytest.raises(BudgetAbort, match="attempt cap"):
        orch._agent_sink(ev, role=CODER_ROLE)  # 1,200 — over the cap


def test_sink_lifetime_ceiling_still_wins_when_smaller(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting(
        "task-1", remaining_tokens=1_000, attempt_cap=1_000_000)
    ev = AgentEvent("usage", meta={"tokens_used": 600, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0})
    orch._agent_sink(ev, role=CODER_ROLE)
    with pytest.raises(BudgetAbort, match="lifetime"):
        orch._agent_sink(ev, role=CODER_ROLE)


class _TokenGusherThenFixBackend:
    """Attempt 1 gushes past the ATTEMPT cap (WIP on disk); attempt 2 works."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n# WIP\n")
            for _ in range(500):
                if on_event:
                    on_event(AgentEvent("usage", meta={
                        "tokens_used": 30_000, "cache_read_tokens": 0,
                        "cache_creation_tokens": 0}))
            raise AssertionError("attempt cap never aborted the attempt")
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        _mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_attempt_cap_fails_the_attempt_and_the_loop_retries(
    store, bare_repo, tmp_path
):
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["mul(a,b) returns a*b"]
    task.config = {"attempt_tokens": 50_000, "budget_unit": "weighted"}
    # lifetime cap (the 1.6M default) stays far away
    await store.create_task(task)

    backend = _TokenGusherThenFixBackend()
    orch = _orch(store, tmp_path, backend=backend)
    outcome = await orch.run_task(task)

    # ended the ATTEMPT, not the task: the bounded loop got its retry
    assert backend.calls == 2
    reloaded = await store.find_task(task.id)
    assert (reloaded.blocker or {}).get("category") != "BUDGET_EXHAUSTED"

    attempts = await store.list_attempts(task.id)
    first = attempts[0]
    assert first["status"] == "failed"
    assert "attempt cap" in (first["failure_reason"] or "")
    # the attempt's true spend was recorded, not zero
    assert first["tokens_used"] >= 50_000

    # attempt 1's work survived as a checkpoint
    log = subprocess.run(
        ["git", "log", "--all", "--pretty=%s"], cwd=bare_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "[WIP-PARTIAL]" in log


class _TokenGusherWithWipBackend:
    """Gushes past the LIFETIME cap with WIP on disk — the park must keep the
    dirty tree for _raise_blocker's [WIP-BLOCKED] checkpoint (resume_commit)."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n# WIP\n")
        for _ in range(500):
            if on_event:
                on_event(AgentEvent("usage", meta={
                    "tokens_used": 30_000, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0}))
        raise AssertionError("budget cross never aborted the attempt")


async def test_lifetime_park_still_records_a_resume_checkpoint(
    store, bare_repo, tmp_path
):
    """Regression guard for the attempt-cap change: the WIP-PARTIAL checkpoint
    must fire ONLY on the attempt-cap path — a pre-emptive commit on the
    lifetime path would clean the tree before _raise_blocker's [WIP-BLOCKED]
    checkpoint and lose the blocker's resume_commit."""
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.config = {"lifetime_tokens": 50_000, "budget_unit": "weighted"}
    await store.create_task(task)

    # See the sibling test above: a fresh task's first attempt has no
    # measured history, so the loop-head startup floor
    # (`_check_attempt_startup_floor`) would otherwise refuse to start it
    # against this fixture's tiny lifetime cap before the mid-attempt path
    # under test ever runs. Zeroed for the same reason.
    orch = _orch(store, tmp_path, backend=_TokenGusherWithWipBackend(),
                 bounds={"min_viable_attempt_weighted_tokens": 0})
    await orch.run_task(task)

    reloaded = await store.find_task(task.id)
    blocker = reloaded.blocker or {}
    assert blocker.get("category") == "BUDGET_EXHAUSTED"
    assert blocker.get("resume_commit"), (
        "lifetime park lost its [WIP-BLOCKED] resume checkpoint")
    # Mutation-proof (review D8): resume_commit alone is a tautology —
    # _checkpoint_wip returns head_sha even on a clean tree. The park's
    # checkpoint must be the [WIP-BLOCKED] one; a pre-emptive [WIP-PARTIAL]
    # on this path would clean the tree first and mislabel the park point.
    log = subprocess.run(
        ["git", "log", "--all", "--pretty=%s"], cwd=bare_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "[WIP-BLOCKED]" in log
    assert "[WIP-PARTIAL]" not in log


# ── v8: the budget nudge closure `_build_supervisor` wires into the hook ──── #


def test_supervisor_budget_status_reads_the_armed_accounting(store, tmp_path):
    """The nudge must count EXACTLY what the hard abort counts and be scoped to
    the running task (worker-pool reuse).

    It did not, on two counts, until the caps became cost-weighted: it summed
    in/out + cache reads only (silently dropping cache CREATION, which the hard
    abort has always counted), and it summed them RAW against a ceiling the
    abort compares WEIGHTED. The second one mattered most — the supervisor
    divides this spend by this ceiling and force-stops exploration at 85%, so a
    raw numerator over a weighted denominator ordered the coder to wrap up at
    roughly a fifth of the budget it actually had."""
    orch = _orch(store, tmp_path)
    t = Task.new("investigate", repo_path=str(tmp_path))
    hook = orch._build_supervisor(t, str(tmp_path))
    assert hook is not None and hook.budget_status is not None

    # Unarmed → None (never crashes).
    assert hook.budget_status() is None

    orch._begin_attempt_accounting(t.id, remaining_tokens=9_999_999,
                                   attempt_cap=800_000)
    orch._attempt_usage["tokens_used"] = 100
    orch._attempt_usage["cache_read_tokens"] = 200
    orch._attempt_usage["cache_creation_tokens"] = 7_777  # counts, at x1.25
    # 100x1.0 + 200x0.1 + 7,777x1.25 = 9,841.25, floored.
    assert hook.budget_status() == (9_841, 800_000)
    # Anchored to the ABORT, not just to a literal: the same usage fed to the
    # sink must cross a ceiling of exactly this number, or the nudge and the
    # force-stop are measuring different things again.
    orch._active_task_id = t.id
    orch._begin_attempt_accounting(t.id, remaining_tokens=9_841)
    orch._agent_sink(
        AgentEvent("usage", meta={"tokens_used": 100, "cache_read_tokens": 200,
                                  "cache_creation_tokens": 7_776}),
        role=CODER_ROLE)  # 9,840 — one under, must not abort
    with pytest.raises(BudgetAbort):
        orch._agent_sink(
            AgentEvent("usage", meta={"cache_creation_tokens": 1}),
            role=CODER_ROLE)  # 9,841.25 — crosses

    # Armed for a DIFFERENT task → None.
    orch._begin_attempt_accounting("other-task", remaining_tokens=1_000)
    assert hook.budget_status() is None


def test_supervisor_only_names_skills_the_coder_actually_has(store, tmp_path):
    """v10 drill (ns-7ef821b2): the supervisor told the coder to use skills
    that DON'T exist — skill-type memory TITLES leaked into its 'available
    skills' list while the coder's manifest carries only discovered on-disk
    skills. A falsifiable recommendation earned the coder's distrust of the
    whole [SUPERVISOR] channel. The supervisor's list must be exactly the
    delivered manifest names."""
    from types import SimpleNamespace

    orch = _orch(store, tmp_path)
    t = Task.new("investigate", repo_path=str(tmp_path))
    # Coder manifest: one real on-disk skill.
    orch._discovered_skills = ["real-deploy-skill"]
    orch._discovered_skills_info = [
        SimpleNamespace(name="real-deploy-skill", description="d")]
    # Skill-type memory: a title that is NOT an invocable skill.
    orch._active_memories = [
        {"type": "skill", "title": "how we once fixed the kafka retry"}]
    hook = orch._build_supervisor(t, str(tmp_path))
    assert hook is not None
    assert "real-deploy-skill" in hook.skills
    assert "how we once fixed the kafka retry" not in hook.skills


def test_supervisor_keeps_db_matched_on_disk_skills(store, tmp_path):
    """r1 finding: the delivered manifest is _discovered_skills_info, which
    deliberately RESURRECTS on-disk skills whose names match DB skill titles
    (the _kept union) — relevant_skill_names skips those from
    _discovered_skills. The supervisor's list must follow the manifest, so a
    db-matched on-disk skill (invocable under its sanitized on-disk name)
    stays recommendable when the two sets diverge."""
    from types import SimpleNamespace

    orch = _orch(store, tmp_path)
    t = Task.new("investigate", repo_path=str(tmp_path))
    # Diverge the sets: the db-matched skill is in the manifest info but NOT
    # in _discovered_skills (exactly what relevant_skill_names produces).
    orch._discovered_skills = ["plain-skill"]
    orch._discovered_skills_info = [
        SimpleNamespace(name="plain-skill", description="d"),
        SimpleNamespace(name="kafka-retry-helper", description="db-matched")]
    orch._active_memories = [
        {"type": "skill", "title": "kafka-retry-helper"}]
    hook = orch._build_supervisor(t, str(tmp_path))
    assert hook is not None
    assert "kafka-retry-helper" in hook.skills
    assert "plain-skill" in hook.skills


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
async def test_stream_reports_tool_result_SIZE_from_the_user_message(tmp_path, monkeypatch):
    """PR-024 lever 1's prerequisite.

    Tool RESULTS arrive in a UserMessage — an AssistantMessage carries the
    ToolUseBlock (the call). A ToolResultBlock branch used to sit inside the
    AssistantMessage loop, so it was UNREACHABLE: 0 tool_result events across 35
    attempts against 1,497 tool_use, and the `_TOOL_RESULT_CAP` truncation a
    previous author wrote never executed once.

    The SIZE is emitted, never the text: 72% of an attempt's cost is the
    conversation re-read every turn and tool results are the payload, so the size
    distribution is what a truncation threshold must be chosen from — while
    persisting the text would bloat the DB and could capture whatever a command
    printed, including credentials.
    """
    from claude_agent_sdk import UserMessage
    from claude_agent_sdk.types import ToolResultBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend, _TOOL_RESULT_CAP

    big = "x" * (_TOOL_RESULT_CAP + 500)
    small = "ok"

    async def _q(*args, **kwargs):
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=big)])
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t2", content=small)])

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]

    results = [e for e in events if e.kind == "tool_result"]
    assert len(results) == 2, f"tool results were not observed at all; got {events!r}"
    assert results[0].meta["result_chars"] == len(big)
    assert results[0].meta["over_cap"] is True
    assert results[1].meta["result_chars"] == len(small)
    assert results[1].meta["over_cap"] is False
    # JOIN KEY — without it the distribution cannot be sliced by tool, which is the
    # whole point (Bash is 62% of calls and is the unbounded one).
    assert results[0].meta["tool_use_id"] == "t1"
    assert results[1].meta["tool_use_id"] == "t2"
    # The TEXT must never be carried — DB bloat and secret capture.
    assert not results[0].text, f"tool_result must not carry the text; got {results[0].text!r}"


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
async def test_stream_records_the_EXIT_CODE_of_a_failed_tool_and_still_no_text(
    tmp_path, monkeypatch
):
    """Coder agents ran `export_guard` 103 times across 8 tasks in one night and
    the DB cannot say whether any of that was a refusal LOOP: the tool_result
    event carries the SIZE and never the text (DB bloat, and a command's stderr
    can print a credential), and a size cannot tell a refusal from a pass. An
    int can, and an int is not output.

    THE MECHANISM IS `is_error` PLUS A FIRST-LINE PREFIX, and the correction
    matters because the first version of this docstring got it wrong. Measured
    over 3,733 real ToolResultBlocks: `block.content` is a plain STRING for
    successes too (3,282 of them; 324 are block lists, 127 are the errors), so
    the type says NOTHING about whether the command failed. The
    `{stdout, stderr, interrupted, isImage, noOutputExpected}` dict does exist,
    but it is the CLI's own `toolUseResult` record — the SDK never delivers it
    as block content. What separates the two populations is `is_error`, and
    what carries the number is the FIRST LINE of a failed result: `Exit code N`
    (84 of 84 such lines, every one `is_error=True`; zero successes open with
    it in that corpus).

    So both halves are load-bearing, and the last three blocks below are each a
    way the number would otherwise be invented:

    * `printed_by_a_pass` — a command that SUCCEEDED while replaying a captured
      log whose first line is `Exit code 7`. On the prefix alone that is a
      failure with status 7. `is_error` is what refuses it.
    * `second_line` — a failure whose SECOND line opens with the prefix. Any
      scan that walks lines rather than reading the first one files this as 7.
    * `quoted` — the prefix mid-line, which no body search may pick up either.

    Unknown stays unknown: the key is simply absent, because a fabricated
    status is worse than no measurement.
    """
    from claude_agent_sdk import UserMessage
    from claude_agent_sdk.types import ToolResultBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend

    async def _q(*args, **kwargs):
        yield UserMessage(content=[
            ToolResultBlock(
                tool_use_id="refused",
                content="Exit code 1\nEXPORT_CLASSIFICATION.txt:12: `ship 4  docs/`"
                        " actually wins 3 file(s).",
                is_error=True),
            ToolResultBlock(tool_use_id="passed", content="OK", is_error=False),
            ToolResultBlock(
                tool_use_id="killed",
                content="Exit code 143\nterminated", is_error=True),
            ToolResultBlock(
                tool_use_id="quoted",
                content="ran 3 checks\nthe log said Exit code 7\n", is_error=True),
            # A command that SUCCEEDED and printed a captured log. Identical to
            # `refused` at the prefix; only is_error tells them apart.
            ToolResultBlock(
                tool_use_id="printed_by_a_pass",
                content="Exit code 7\n--- replayed from the captured log ---",
                is_error=False),
            ToolResultBlock(
                tool_use_id="second_line",
                content="checking exports\nExit code 7\nrefused", is_error=True),
        ])

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]
    meta = {e.meta["tool_use_id"]: e.meta for e in events if e.kind == "tool_result"}
    assert set(meta) == {"refused", "passed", "killed", "quoted",
                         "printed_by_a_pass", "second_line"}, meta

    assert meta["refused"]["exit_code"] == 1, (
        "a refusal's exit status is the whole point — without it 103 invocations "
        f"cannot be told from 103 refusals; got {meta['refused']!r}")
    # Multi-digit, so a one-character parse cannot pass.
    assert meta["killed"]["exit_code"] == 143, meta["killed"]
    # No status was STATED for any of these; inventing one is a false measurement.
    assert "exit_code" not in meta["passed"], meta["passed"]
    assert "exit_code" not in meta["quoted"], (
        "'Exit code 7' printed in the body is not this command's status; a body "
        f"search inflates every refusal count taken from this field: {meta['quoted']!r}")
    assert "exit_code" not in meta["printed_by_a_pass"], (
        "this command SUCCEEDED — its stdout merely opens with the words. The "
        "prefix alone cannot tell that from a failure; is_error is the other "
        f"half of the mechanism: {meta['printed_by_a_pass']!r}")
    assert "exit_code" not in meta["second_line"], (
        "the status is stated on the FIRST line or not at all; a scan that walks "
        f"line starts records a number the command never returned: {meta['second_line']!r}")

    # The no-text rule is UNCHANGED: an int, and nothing that carried it.
    assert not [e.text for e in events if e.kind == "tool_result" and e.text]
    leaked = [v for m in meta.values() for v in m.values() if isinstance(v, str)]
    assert not any("EXPORT_CLASSIFICATION" in v or "terminated" in v for v in leaked), (
        f"tool_result meta must carry no output text; got {leaked!r}")


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
async def test_stream_handles_PARALLEL_tool_results_and_measures_model_visible_text(
    tmp_path, monkeypatch
):
    """Three gaps a review found in the first version, each with its own mutation.

    * PARALLEL CALLS: the CLI batches several results into ONE UserMessage. The first
      test fed two messages of one block each, so a `break`-after-first-block mutation
      SURVIVED. This feeds one message with three blocks.
    * REPR vs TEXT: `str(content)` measured `[{'type': 'text', 'text': 'hello world'}]`
      as 41 chars for 11 of payload, and `None` as 4 rather than 0.
    * SUBAGENT SPLIT: a subagent's results are re-read in the SUBAGENT's context, not
      the main conversation, so they must be excludable from the distribution.
    """
    from claude_agent_sdk import UserMessage
    from claude_agent_sdk.types import ToolResultBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend

    async def _q(*args, **kwargs):
        yield UserMessage(content=[
            ToolResultBlock(tool_use_id="a", content="12345"),
            ToolResultBlock(tool_use_id="b", content=[{"type": "text", "text": "hello world"}]),
            ToolResultBlock(tool_use_id="c", content=None),
        ])
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="d", content="sub")],
            parent_tool_use_id="toolu_parent",
        )

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]
    r = [e for e in events if e.kind == "tool_result"]

    assert len(r) == 4, f"parallel blocks in ONE message must each be counted; got {len(r)}"
    assert [e.meta["tool_use_id"] for e in r] == ["a", "b", "c", "d"]
    assert r[0].meta["result_chars"] == 5
    # 11 chars of payload, NOT the 41-char repr of the wrapper list.
    assert r[1].meta["result_chars"] == 11, f"repr length leaked in: {r[1].meta!r}"
    # None is 0 chars, not 4 ("None").
    assert r[2].meta["result_chars"] == 0, f"None recorded as text: {r[2].meta!r}"
    # Main-thread results are distinguishable from subagent results.
    assert r[0].meta["parent_tool_use_id"] is None
    assert r[3].meta["parent_tool_use_id"] == "toolu_parent"


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
async def test_tool_use_emits_the_join_key_so_the_per_tool_slice_is_computable(
    tmp_path, monkeypatch
):
    # A review deleted `meta={"tool_use_id": block.id}` from the tool_use emit and the
    # ENTIRE suite stayed green. The commit message claimed both ends of the join key
    # were proved when only the RESULT end was. A join key with one end is not a join
    # key — and the CALL side is the half carrying `tool_name`, which is what makes the
    # per-tool slice ("Bash is 62% of calls and is the unbounded one") computable.
    from claude_agent_sdk import AssistantMessage
    from claude_agent_sdk.types import ToolUseBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend

    async def _q(*args, **kwargs):
        yield AssistantMessage(
            content=[ToolUseBlock(id="toolu_x", name="Bash", input={"command": "ls"})],
            model="claude-sonnet-5",
        )

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]
    tu = [e for e in events if e.kind == "tool_use"]
    assert len(tu) == 1
    assert tu[0].tool_name == "Bash"
    assert tu[0].meta["tool_use_id"] == "toolu_x", (
        "without the CALL-side id the size distribution cannot be sliced BY TOOL, "
        "which is the entire purpose of collecting it"
    )


def test_exit_status_is_gated_on_is_error_and_never_raises():
    """The same invariant `_result_size` carries, one helper over: telemetry
    must never break the session. A raise here is not a lost measurement — the
    handler at the bottom of `_run_once` catches it and the whole ATTEMPT fails.

    Both malformed cases below are real. `"²"` and `"①"` satisfy `str.isdigit()`
    and `int()` refuses them, so an isdigit-guarded parse still raises; and a
    long enough run of ASCII digits trips CPython's `int_max_str_digits` (4300),
    which `str.isdecimal()` does not cover either. Neither can be produced by
    the CLI today — they are produced by whatever a COMMAND printed, which is
    exactly the input this helper reads.
    """
    from no_human.agent.claude_backend import _exit_status

    assert _exit_status("Exit code 1\nrefused", is_error=True) == {"exit_code": 1}
    assert _exit_status("Exit code 143", is_error=True) == {"exit_code": 143}
    # THE GATE: identical text, and this command did not fail.
    assert _exit_status("Exit code 1\nrefused", is_error=False) == {}
    # isdigit() is True for both of these; int() still refuses them.
    assert _exit_status("Exit code ²", is_error=True) == {}
    assert _exit_status("Exit code ①", is_error=True) == {}
    # int_max_str_digits. The parse must decline it, not raise through.
    assert _exit_status("Exit code " + "1" * 4301, is_error=True) == {}
    # A single line longer than the window: the digits visible are only a
    # PREFIX OF whatever number is there, so it is never guessed at. 100 digits
    # is the case that MATTERS — it parses perfectly well, so nothing but the
    # window stops a 100-digit "exit code" being recorded as a measurement.
    assert _exit_status("Exit code " + "9" * 100, is_error=True) == {}
    assert _exit_status("Exit code " + "9" * 100_000, is_error=True) == {}
    assert _exit_status(None, is_error=True) == {}
    assert _exit_status([{"type": "text", "text": "Exit code 1"}], is_error=True) == {}


def test_result_size_flags_non_text_blocks_and_never_raises():
    # Two review findings, both in the size helper.
    # 1. An image tool result carries a large base64 payload and ZERO text, so joining
    #    only `text` recorded it as 0 chars — the SAME defect class as the repr
    #    inflation this helper replaced, opposite direction, an order of magnitude
    #    larger (11->41 vs ~1.2M->0). Now FLAGGED so it can be excluded, the way
    #    is_error and parent_tool_use_id are.
    # 2. `{"type":"text","text":null}` made it RAISE. That does not merely lose
    #    telemetry: the nearest handler terminates the stream, so the whole attempt
    #    fails as an SDK error. Telemetry must never break the session.
    from no_human.agent.claude_backend import _result_size

    assert _result_size([{"type": "text", "text": "hello world"}]) == {
        "result_chars": 11, "over_cap": False, "non_text_blocks": 0}
    img = _result_size([{"type": "image", "source": {"data": "x" * 100}}])
    assert img["result_chars"] == 0 and img["non_text_blocks"] == 1, img
    assert _result_size([{"type": "text", "text": None}])["result_chars"] == 0
    assert _result_size(None)["result_chars"] == 0
    assert _result_size("12345")["result_chars"] == 5
def test_budget_abort_records_the_SHAPE_of_the_spend_not_just_its_size(store, tmp_path):
    """S1.2. `attempts.turns_used` is NULL on every budget-aborted attempt — it comes
    from ResultMessage.num_turns and an abort has no result — so a 4M attempt was
    indistinguishable from any other. An agent spinning through hundreds of small
    turns and an agent taking a handful of enormous ones look identical in the
    ledger and need OPPOSITE fixes.

    This pins the live counter. It is deliberately NOT written to turns_used: a
    "usage" event is emitted per assistant message and only when that message
    carries a usage block, so it is a LOWER BOUND, and a lower bound in a column
    that elsewhere holds an exact count corrupts every aggregate over it.
    """
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=1_000)
    ev = AgentEvent("usage", meta={"tokens_used": 100, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0})
    for _ in range(7):
        orch._agent_sink(ev, role=CODER_ROLE)

    assert orch._attempt_usage["assistant_messages"] == 7, (
        "the sink must count assistant messages, or a budget-aborted attempt cannot "
        "be told apart from any other"
    )
    # And the counter must be scoped to the attempt, like the token totals beside it:
    # re-arming resets it, or attempt N+1 inherits attempt N's shape.
    orch._begin_attempt_accounting("task-1", remaining_tokens=1_000)
    assert orch._attempt_usage["assistant_messages"] == 0


class _BudgetBurnBackend:
    """A coder turn that spends past the ceiling, driving the REAL sink.

    The sink raises BudgetAbort out of `run`, so the orchestrator's own
    `except BudgetAbort` handler runs — which is the code under test.
    """

    def __init__(self, per_message: int, messages: int = 50):
        self.per_message, self.messages = per_message, messages

    async def run(self, *a, on_event=None, **k):
        for _ in range(self.messages):
            # PRODUCTION-SHAPED, deliberately. A review showed the first fixture used
            # tokens_used=100_000 / cache_read=0 — the INVERSE of production, where
            # cache reads are 99.98% of spend (real row: tokens_used=709,
            # cache_read=4,033,914). Under that fixture, deleting the cache-read term
            # from `spent` was undetectable, and the shipped diagnostic would have read
            # ~17 tokens/message instead of ~69,562: a wrong number read as truth, which
            # is the exact harm this change refuses to inflict on `turns_used`.
            on_event(AgentEvent("usage", meta={
                "tokens_used": self.per_message // 100,
                "cache_read_tokens": self.per_message - (self.per_message // 100),
                "cache_creation_tokens": 0,
            }))
        raise AssertionError("the sink should have aborted before this")


async def test_the_budget_abort_EVENT_carries_the_spend_shape(
    bare_repo, tmp_path, store  # noqa: F811
):
    """Observes the ARTIFACT, not a private attribute.

    A review deleted both meta keys from the emit and the full suite stayed
    green (2867 passed): the only test asserted on `orch._attempt_usage`, never
    on the emitted `agent_error` event, which is the entire deliverable. That is
    this repo's own recorded lesson — a test that recomputes the expected value
    from the code under test proves nothing; break the WIRING and it must fail.
    """
    cfg = _config(tmp_path)
    # 54,500 COST-WEIGHTED tokens = exactly 5 of this backend's messages. Each
    # is 100,000 raw in the production shape below — 1,000 fresh (x1.0) +
    # 99,000 cache-read (x0.1) = 10,900 weighted. The old literal was the same
    # boundary in the old raw unit (5 x 100,000).
    cfg.data.setdefault("bounds", {})["attempt_tokens"] = 54_500
    events: list = []
    orch = Orchestrator(store, cfg.data, _BudgetBurnBackend(per_message=100_000),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("burn the budget", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    budget = [e for e in events
              if e.get("kind") == "agent_error" and e.get("error_class") == "budget"]
    assert budget, f"no budget agent_error was emitted; kinds={sorted({e.get('kind') for e in events})}"
    ev = budget[0]
    # The COUNT must be exact, not merely truthy. A review replaced the emitted count
    # with a constant 1 — the precise opposite of the diagnostic's purpose, which is
    # telling "hundreds of small turns" from "a handful of enormous ones" — and the
    # whole suite stayed green. 5 messages are driven below the 500k ceiling at 100k
    # each, so the 5TH is what crosses it: the sink increments BEFORE the ceiling check, so spent==500_000 >= 500_000 fires on message 5. The assertion was right and this comment was wrong.
    assert ev.get("assistant_messages") == 5, (
        "the budget-abort event must carry the EXACT message count, or a constant "
        f"passes and the diagnostic is a lie; got {ev!r}"
    )
    # And the numerator must include cache reads, which are ~99.98% of real spend.
    assert ev["tokens_per_message"] == 100_000, ev


async def test_the_message_counter_is_scoped_to_the_running_task(store, tmp_path):
    """Task B's messages must never be charged to task A — the same scoping bug
    the token ceiling already had. A review moved the increment outside the
    `ceiling[0] == self._active_task_id` guard and all 2867 tests stayed green.
    """
    orch = _orch(store, tmp_path)
    orch._begin_attempt_accounting("task-A", remaining_tokens=10_000)
    orch._active_task_id = "task-B"          # a DIFFERENT task is running
    ev = AgentEvent("usage", meta={"tokens_used": 100, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0})
    for _ in range(5):
        orch._agent_sink(ev, role=CODER_ROLE)

    assert orch._attempt_usage["assistant_messages"] == 0, (
        "task B's assistant messages were charged to task A's accounting"
    )


async def test_the_parked_task_evidence_actually_SHOWS_the_spend_shape(store, tmp_path):
    """The visibility claim, made true instead of retracted.

    A review found the emit's justification — that it put the shape "where a human
    reading a parked task can see it" — was an unchecked claim about a consumer:
    `web/src` has zero references to the keys, the CLI prints kind+text only, and
    this blocker's evidence carried attempts and tokens with no SHAPE. The
    BUDGET_EXHAUSTED blocker is what a human actually reads when a task parks, so
    the shape belongs there.
    """
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=10_000_000)
    ev = AgentEvent("usage", meta={"tokens_used": 100, "cache_read_tokens": 9_900,
                                   "cache_creation_tokens": 0})
    for _ in range(4):
        orch._agent_sink(ev, role=CODER_ROLE)

    t = Task.new("x", repo_path=str(tmp_path))
    t.id = "task-1"
    # Assert on the BLOCKER a human reads, not on the helper. Testing the helper
    # directly left the WIRING unpinned: deleting `+ self._spend_shape_note(task)`
    # from the evidence kept all 23 tests green — the same "test the artifact, not
    # the function" defect this branch was already corrected for once.
    # Force the cap with a REAL attempt row: lifetime_usage sums attempts from the DB,
    # and _lifetime_limits rejects a 0 override (`value if value > 0 else default`), so
    # neither a zero cap nor an attempt-less task can produce the blocker.
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, status="failed", tokens_used=5_000_000,
                               cache_read_tokens=0, cache_creation_tokens=0)
    t.config = {"lifetime_tokens": 1_000_000, "budget_unit": "weighted"}
    blocker = await orch._check_lifetime_budget(t)
    assert blocker is not None, "the lifetime cap should have produced a blocker"
    assert "4 assistant messages" in blocker.evidence, blocker.evidence
    assert "10,000 raw tokens/message" in blocker.evidence, blocker.evidence
    # The causal clause must be EVIDENCED, not asserted. This fixture is 99%
    # cache-read (100 fresh / 9,900 cached), so the clause is true and appears —
    # and the percentage that justifies it is printed beside it.
    assert "99% cache-read" in blocker.evidence, blocker.evidence
    assert "scales with TURNS" in blocker.evidence, blocker.evidence

    # Scoped to THIS task: the pool reuses one Orchestrator, so another task's
    # counters must never describe this one.
    other = Task.new("y", repo_path=str(tmp_path))
    other.id = "task-2"
    assert orch._spend_shape_note(other) == "", (
        "task-2's evidence was built from task-1's counters"
    )


def test_the_causal_clause_is_WITHHELD_when_the_numbers_do_not_support_it(store, tmp_path):
    """A review drove a SPINNER shape — many messages, cache-read only ~3% of spend —
    and the note still asserted "cost is dominated by re-reading the conversation each
    turn". That was false for those inputs: ~97% was fresh input/output. The split was
    in hand and only the sum was printed, so a claim was ASSERTED where it could be
    EVIDENCED.

    PR-024 measured 99% cache-read over 1,896 messages, but that is ONE repo and ONE
    prompt shape, while this string ships to every install. It must describe the
    attempt in front of it.
    """
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=10_000_000)
    # Spinner: fresh dominates, cache-read is a small minority.
    ev = AgentEvent("usage", meta={"tokens_used": 2_900, "cache_read_tokens": 100,
                                   "cache_creation_tokens": 0})
    for _ in range(200):
        orch._agent_sink(ev, role=CODER_ROLE)

    t = Task.new("spinner", repo_path=str(tmp_path))
    t.id = "task-1"
    note = orch._spend_shape_note(t)

    assert "200 assistant messages" in note, note          # the numbers still ship
    assert "3% cache-read" in note, note                   # and so does the split
    assert "scales with TURNS" not in note, (
        f"the causal clause was asserted for a shape that contradicts it: {note}"
    )
