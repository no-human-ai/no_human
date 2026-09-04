"""B20: a coder turn has NO wall-clock bound (only max_turns), so a hung SDK
subprocess (auth/quota/network stall at 0% CPU) would wedge the whole attempt
forever — the exact wedge that killed a dogfood shadow run. The advisory/judge
calls were already guarded (_bounded_run); this proves the CODER turn now is
too: a hang becomes an honest FAILED attempt, never a forever-wedge.
"""
from __future__ import annotations

import asyncio
import time

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import (  # noqa: F401
    RESUMABLE, FakeBackend, _SHA_RE, _config, bare_repo,
)


#: A zero-diff completion whose report does not parse as an ALREADY-SATISFIED
#: claim now buys ONE single-turn reformat follow-up, delivered on the same
#: session (`resume=<session_id>`). That follow-up is NOT an attempt: the fakes
#: below count `run` calls to count ATTEMPTS, so they answer it separately and
#: leave the counter alone. The reply deliberately does not parse either, so
#: the zero-diff failure each test is about still stands.
_NUDGE_REPLY = AgentResult(
    final_text="nothing further to say", num_turns=1, is_error=False,
    tokens_used=10, session_id="s", stop_reason="end_turn")


class _HangBackend:
    """A coder turn that never returns in time (a hung SDK subprocess)."""

    async def run(self, *a, **k):
        await asyncio.sleep(30)  # far longer than the patched attempt ceiling


async def test_hung_coder_turn_fails_fast_instead_of_wedging(
    bare_repo, tmp_path, store  # noqa: F811
):
    cfg = _config(tmp_path)
    # Tiny ceiling so the hang trips it immediately; default is 3600s.
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    events: list = []
    orch = Orchestrator(store, cfg.data, _HangBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    t0 = time.monotonic()
    outcome = await orch.run_task(t)
    elapsed = time.monotonic() - t0

    # Proves the timeout FIRED: the backend hangs 30s, but the bounded loop of
    # short-ceiling attempts completes in well under that — never the 30s wedge.
    assert elapsed < 15, f"attempt did not time out fast (took {elapsed:.1f}s)"
    # A terminal, honest non-hang state — never AWAITING_APPROVAL (nothing
    # ran). BLOCKED joined the set with the timeout-streak escalation
    # (SCRUM-4): two consecutive timeouts now park as TRANSIENT_INFRA.
    assert outcome.status in (
        TaskStatus.FAILED, TaskStatus.ESCALATED, TaskStatus.BLOCKED), outcome
    assert outcome.pr_url is None
    # The attempt row must SAY it timed out (observability, not a silent fail).
    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt recorded"
    assert attempts[-1]["status"] == "failed"
    assert "timed out" in (attempts[-1]["failure_reason"] or "").lower(), attempts[-1]
    # And an honest timeout event was emitted.
    assert any(e.get("error_class") == "timeout" for e in events), \
        [e for e in events if e.get("kind") == "agent_error"]


async def test_normal_run_is_not_falsely_timed_out(
    bare_repo, tmp_path, store  # noqa: F811
):
    """The wall-clock bound must NOT fire for a normal fast attempt."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 30  # ample

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)
    # A healthy run reaches an approvable PR — no false timeout, no failure.
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome
    attempts = await store.list_attempts(t.id)
    assert not any(
        "timed out" in (a["failure_reason"] or "").lower() for a in attempts
    ), attempts


class _AlwaysHangBackend:
    """Every coder turn hangs — a persistently wedged backend."""

    calls = 0

    async def run(self, *a, **k):
        type(self).calls += 1
        await asyncio.sleep(30)


async def test_repeated_timeouts_escalate_after_two_not_max_attempts(
    bare_repo, tmp_path, store  # noqa: F811
):
    """SCRUM-4 / B20 follow-up: a backend that hangs on EVERY attempt must trip
    the unproductive streak after 2 consecutive timeouts and escalate with a
    TIMEOUT-specific blocker — not burn all max_attempts, and never surface the
    misleading zero-diff 'is this already implemented?' question."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    cfg.data["bounds"]["max_attempts"] = 3
    _AlwaysHangBackend.calls = 0
    orch = Orchestrator(store, cfg.data, _AlwaysHangBackend(), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # TRANSIENT_INFRA parks (BLOCKED, auto-retry) — a transient backend wedge
    # self-heals; a low-confidence triage may escalate instead. Either way the
    # loop must STOP after two timeouts, never burn the third attempt.
    assert outcome.status in (TaskStatus.BLOCKED, TaskStatus.ESCALATED), outcome
    assert _AlwaysHangBackend.calls == 2, (
        f"expected escalation after 2 timeouts, backend ran {_AlwaysHangBackend.calls}x")
    fresh = await store.get_task(t.id)
    b = fresh.blocker or {}
    blob = ((b.get("question") or "") + (b.get("root_cause_hypothesis") or "")).lower()
    assert "hung" in blob or "timed out" in blob, b
    assert "already implemented" not in blob, (
        "timeout streak must not surface the zero-diff escalation")
    assert b.get("category") == "TRANSIENT_INFRA", b


async def test_timeout_then_productive_attempt_resets_the_streak(
    bare_repo, tmp_path, store  # noqa: F811
):
    """One timeout followed by a productive attempt must reset the streak —
    no false timeout escalation, exactly like the other unproductive reasons."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 1.5

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    productive = FakeBackend(mutate)

    class _HangOnce:
        calls = 0

        async def run(self, *a, **k):
            type(self).calls += 1
            if type(self).calls == 1:
                await asyncio.sleep(30)
            return await productive.run(*a, **k)

    _HangOnce.calls = 0
    orch = Orchestrator(store, cfg.data, _HangOnce(), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # The healthy second attempt proceeds — never a timeout-streak escalation.
    fresh = await store.get_task(t.id)
    assert (fresh.blocker or {}).get("category") != "TRANSIENT_INFRA", fresh.blocker
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome


class _ZeroDiffThenHang:
    """attempt 1: edits nothing (zero-diff). attempt 2: hangs (timeout).
    A MIXED streak in that order — SCRUM-46's (zero_diff, timeout) case,
    which used to be misread as "two consecutive timeouts"."""

    capabilities = RESUMABLE
    calls = 0

    async def run(self, *a, resume=None, **k):
        if resume is not None:
            return _NUDGE_REPLY
        type(self).calls += 1
        if type(self).calls == 1:
            return AgentResult(final_text="already done, nothing to edit",
                               num_turns=2, is_error=False, tokens_used=100,
                               session_id="s", stop_reason="end_turn")
        await asyncio.sleep(30)


async def test_zero_diff_then_timeout_escalates_as_mixed_not_pure_timeout(
    bare_repo, tmp_path, store  # noqa: F811
):
    """SCRUM-46: a (zero_diff, timeout) streak must escalate as a mixed,
    heterogeneous streak — AMBIGUITY, no auto-retry — never the pure-timeout
    TRANSIENT_INFRA path (which used to fire because it only looked at the
    LAST attempt's kind)."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    cfg.data["bounds"]["max_attempts"] = 3
    _ZeroDiffThenHang.calls = 0
    orch = Orchestrator(store, cfg.data, _ZeroDiffThenHang(), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert _ZeroDiffThenHang.calls == 2, (
        f"expected escalation after 2 attempts, backend ran {_ZeroDiffThenHang.calls}x")
    assert outcome.status is TaskStatus.ESCALATED, outcome
    fresh = await store.get_task(t.id)
    b = fresh.blocker or {}
    assert b.get("category") == "AMBIGUITY", b
    assert b.get("category") != "TRANSIENT_INFRA", b
    blob = ((b.get("question") or "") + (b.get("root_cause_hypothesis") or "")
            + (b.get("evidence") or "")).lower()
    assert "mixed" in blob, b
    assert "two consecutive" not in blob, (
        "must not be mislabeled as a pure-timeout streak")


class _HangThenZeroDiff:
    """attempt 1: hangs (timeout). attempt 2: edits nothing (zero-diff).
    The reverse order — SCRUM-46's (timeout, zero_diff) case, which used to
    ask the misleading zero-diff 'already implemented?' question right after
    a hang, with the hang itself never mentioned."""

    capabilities = RESUMABLE
    calls = 0

    async def run(self, *a, resume=None, **k):
        if resume is not None:
            return _NUDGE_REPLY
        type(self).calls += 1
        if type(self).calls == 1:
            await asyncio.sleep(30)
        return AgentResult(final_text="nothing further to add", num_turns=2,
                           is_error=False, tokens_used=100, session_id="s",
                           stop_reason="end_turn")


async def test_timeout_then_zero_diff_escalates_as_mixed_not_plain_zero_diff(
    bare_repo, tmp_path, store  # noqa: F811
):
    """SCRUM-46: the reverse order — (timeout, zero_diff) — must also
    escalate as mixed, not the plain zero-diff 'is this already implemented?'
    question that discards the fact a coder turn also hung."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    cfg.data["bounds"]["max_attempts"] = 3
    _HangThenZeroDiff.calls = 0
    orch = Orchestrator(store, cfg.data, _HangThenZeroDiff(), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert _HangThenZeroDiff.calls == 2, (
        f"expected escalation after 2 attempts, backend ran {_HangThenZeroDiff.calls}x")
    assert outcome.status is TaskStatus.ESCALATED, outcome
    fresh = await store.get_task(t.id)
    b = fresh.blocker or {}
    assert b.get("category") == "AMBIGUITY", b
    assert b.get("category") != "TRANSIENT_INFRA", b
    blob = ((b.get("question") or "") + (b.get("root_cause_hypothesis") or "")
            + (b.get("evidence") or "")).lower()
    assert "mixed" in blob, b
    # The mixed question may honestly OFFER "already implemented?" as one
    # hypothesis among several — what it must not do is present ONLY the
    # zero-diff framing. The mix and the timeout half must both be named.
    assert "timeout" in blob, b


class _FailThenZeroDiffThenHang:
    """A 3-attempt sequence: attempt 1 fails for an ORDINARY reason (a
    terminal agent error, `is_error=True` — not zero-diff, not a timeout),
    which resets the unproductive streak; attempt 2 is zero-diff; attempt 3
    hangs. Proves `prev_unproductive` starts clean after a non-streak failure
    and the trailing (zero_diff, timeout) pair still correctly escalates as
    mixed — not a stale kind leaking across the reset, and not 3 turns of
    budget burned before the human hears about the mix."""

    capabilities = RESUMABLE
    calls = 0

    async def run(self, *a, resume=None, **k):
        if resume is not None:
            return _NUDGE_REPLY
        type(self).calls += 1
        if type(self).calls == 1:
            return AgentResult(final_text="unexpected SDK error", num_turns=1,
                               is_error=True, tokens_used=100, session_id="s",
                               stop_reason="error")
        if type(self).calls == 2:
            return AgentResult(final_text="nothing further to add", num_turns=2,
                               is_error=False, tokens_used=100, session_id="s",
                               stop_reason="end_turn")
        await asyncio.sleep(30)


async def test_reset_then_mixed_streak_still_escalates_as_mixed(
    bare_repo, tmp_path, store  # noqa: F811
):
    """SCRUM-46: a longer, 3-attempt run — an ordinary test-failing attempt
    (resets the streak), then a (zero_diff, timeout) pair — must still
    escalate as mixed on the trailing pair, proving the reset doesn't leak a
    stale kind into the next streak."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    cfg.data["bounds"]["max_attempts"] = 4
    _FailThenZeroDiffThenHang.calls = 0
    orch = Orchestrator(store, cfg.data, _FailThenZeroDiffThenHang(),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert _FailThenZeroDiffThenHang.calls == 3, (
        "expected 3 attempts (ordinary failure, then the mixed pair), "
        f"backend ran {_FailThenZeroDiffThenHang.calls}x")
    assert outcome.status is TaskStatus.ESCALATED, outcome
    fresh = await store.get_task(t.id)
    b = fresh.blocker or {}
    assert b.get("category") == "AMBIGUITY", b
    assert b.get("category") != "TRANSIENT_INFRA", b
    blob = ((b.get("question") or "") + (b.get("root_cause_hypothesis") or "")
            + (b.get("evidence") or "")).lower()
    assert "mixed" in blob, b


class _HangAfterEditBackend:
    """A coder turn that edits the tree (so the checkpoint has something to
    commit) and then hangs forever — the timeout fires with `repo.has_changes()`
    True, exactly the branch that checkpoints `[WIP-PARTIAL]` at
    orchestrator.py ~4209."""

    async def run(self, *a, cwd=None, **k):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b  # wip edit\n")
        await asyncio.sleep(30)


async def test_the_timeout_checkpoint_survives_a_repairable_manifest_refusal(
    bare_repo, tmp_path, store, monkeypatch  # noqa: F811
):
    """The attempt-timeout `[WIP-PARTIAL]` checkpoint (orchestrator.py ~4209)
    must route through the same manifest-repair seam as every other
    checkpoint (`_checkpoint_commit`): a stale pin at the moment a hung coder
    turn is aborted must be repaired and committed, not silently lost because
    this recovery path called `repo.commit_all` directly. Regression proof:
    reverting the timeout site to a raw `repo.commit_all` call bypasses the
    mocked seam below, `calls` stays empty, and the first assertion fails."""
    from no_human.core import orchestrator as orch_mod

    calls = []
    approved_paths = ["src/pkg/mod.py"]

    def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg, on_repair=None):
        calls.append((edited, commit_msg))
        if on_repair is not None:
            on_repair(approved_paths, "1 stale pin(s)")
        return repo_arg.commit_all(commit_msg)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    cfg.data["bounds"]["max_attempts"] = 1
    events: list = []
    orch = Orchestrator(store, cfg.data, _HangAfterEditBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert calls, ("commit_with_manifest_repair was never called — the "
                   "timeout checkpoint bypassed the repair seam")
    # The terminal status past the single timed-out attempt (FAILED vs an
    # escalation) is the bounded-loop's business, not this seam's — this test
    # only cares that the checkpoint commit itself survived the repairable
    # refusal, which the assertions below verify directly.
    assert outcome.status in (
        TaskStatus.FAILED, TaskStatus.ESCALATED, TaskStatus.BLOCKED), outcome

    kinds = [e["kind"] for e in events]
    assert "checkpoint" in kinds, kinds
    assert kinds.count("manifest_repaired") == 1, kinds
    repaired = [e for e in events if e["kind"] == "manifest_repaired"][0]
    assert repaired["paths"] == approved_paths
    assert "src/pkg/mod.py" in repaired["text"]

    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt recorded"
    last = attempts[-1]
    assert last["status"] == "failed", last
    assert last["commit_sha"], "the repaired WIP checkpoint sha must be recorded"
    assert _SHA_RE.match(last["commit_sha"]), last["commit_sha"]


class _SteadyProgressBackend:
    """Streams a progress event every ``interval`` for ``rounds`` rounds —
    total run time comfortably exceeds ``attempt_timeout_s`` — then returns a
    normal, productive result. Proves the timeout bounds INACTIVITY, not the
    session's total wall clock: the old `asyncio.wait_for(..., timeout=...)`
    measured elapsed time since the call STARTED and would have cancelled
    this (6 * 0.12s = 0.72s total, well past the 0.25s ceiling); the
    inactivity watchdog never sees a gap wider than 0.12s between events, so
    it never fires."""

    def __init__(self, interval, rounds, mutate):
        self.interval = interval
        self.rounds = rounds
        self.mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        for _ in range(self.rounds):
            await asyncio.sleep(self.interval)
            if on_event:
                on_event(AgentEvent("tool_use", tool_name="Edit",
                                    tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_steady_progress_survives_past_the_timeout_window(
    bare_repo, tmp_path, store  # noqa: F811
):
    """AC1 (first half): a coder run that keeps emitting progress events for
    LONGER than `attempt_timeout_s` must NOT be cancelled. RED under the old
    wall-clock `asyncio.wait_for` bound (0.72s total elapsed > 0.25s
    ceiling); GREEN here because every inter-event gap stays under the
    ceiling."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.25

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    backend = _SteadyProgressBackend(interval=0.12, rounds=6, mutate=mutate)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome
    attempts = await store.list_attempts(t.id)
    assert not any(
        "timed out" in (a["failure_reason"] or "").lower() for a in attempts
    ), attempts


class _UsageThenSilentBackend:
    """Streams N "usage" events (real spend), then goes silent forever — no
    further event, no return. Proves the inactivity-cancel path records the
    TRUE streamed spend, not the pre-run zero: a cancelled `backend.run`
    never delivers a final `AgentResult` for the abort handler to read."""

    def __init__(self, usage_tokens):
        self.usage_tokens = usage_tokens

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        for tokens in self.usage_tokens:
            if on_event:
                on_event(AgentEvent("usage", meta={"tokens_used": tokens}))
            await asyncio.sleep(0.01)
        await asyncio.sleep(30)  # goes silent — never returns, never emits again


async def test_streamed_usage_is_recorded_on_inactivity_cancel(
    bare_repo, tmp_path, store  # noqa: F811
):
    """AC2: spend streamed before a later cancellation is recorded on the
    attempt row as the SUM of the streamed usage events."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    usage_tokens = [50, 75, 40]
    orch = Orchestrator(store, cfg.data, _UsageThenSilentBackend(usage_tokens),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status in (
        TaskStatus.FAILED, TaskStatus.ESCALATED, TaskStatus.BLOCKED), outcome
    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt recorded"
    first = attempts[0]
    assert first["tokens_used"] == sum(usage_tokens), first


async def test_timeout_checkpoint_event_when_nothing_to_checkpoint(
    bare_repo, tmp_path, store  # noqa: F811
):
    """AC3: the timeout branch emits a `checkpoint` event even when there is
    NOTHING to commit — a silent skip must be visible, not just the
    WIP-PARTIAL branch (covered elsewhere)."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    cfg.data["bounds"]["max_attempts"] = 1
    events: list = []
    orch = Orchestrator(store, cfg.data, _HangBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    checkpoints = [e for e in events if e["kind"] == "checkpoint"]
    assert checkpoints, "no checkpoint event emitted on the no-changes branch"
    assert any("no uncommitted changes to checkpoint" in e["text"]
               for e in checkpoints), checkpoints


async def test_failed_wip_checkpoint_commit_becomes_advisory_event(
    bare_repo, tmp_path, store, monkeypatch  # noqa: F811
):
    """AC3: when the WIP-PARTIAL checkpoint commit itself fails, that failure
    must be visible as an `advisory` event, not only a `log.warning` nobody
    watching the event stream ever sees."""
    from no_human.core import orchestrator as orch_mod

    def _boom(repo_arg, edited, commit_msg, on_repair=None):
        raise RuntimeError("simulated checkpoint commit failure")

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair", _boom)

    cfg = _config(tmp_path)
    cfg.data.setdefault("bounds", {})["attempt_timeout_s"] = 0.3
    cfg.data["bounds"]["max_attempts"] = 1
    events: list = []
    orch = Orchestrator(store, cfg.data, _HangAfterEditBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    advisories = [e for e in events if e["kind"] == "advisory"]
    assert advisories, "checkpoint-commit failure never surfaced as an advisory event"
    assert any("checkpoint" in e["text"].lower() for e in advisories), advisories
