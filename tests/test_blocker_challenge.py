"""The escalation-quality gate (gap-close W3, blockers/challenge.py).

main-6cec2140 put 12 of 26 bench failures in the burn-then-quit class. The
gate gives the judgment-call blocker categories ONE supervisor-checked
challenge per task; these tests pin the whole honest-escalation invariant:
resolvable costs an attempt and re-enters the bounded loop under a recorded
assumption — and the SECOND blocker is honored unchallenged; external
verdicts, external categories, and supervisor failures all park exactly as
before; a park is never converted into "done"."""

import subprocess
from types import SimpleNamespace

from no_human.blockers.challenge import (CHALLENGEABLE, build_challenge_prompt,
                                         parse_challenge)
from no_human.blockers.taxonomy import Blocker, BlockerCategory
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import BlockerBackend, _config, bare_repo  # noqa: F401

_AMBIGUITY_JSON = (
    '{"category": "AMBIGUITY", "confidence": 0.9, '
    '"root_cause_hypothesis": "no naming convention was stated", '
    '"question": "What should the helper be called?", '
    '"goal": "add helper", "evidence": "$ grep …"}'
)


def _resolvable(reasoning="repo conventions decide this",
                assumption="follow the module's existing snake_case naming"):
    return ('CHALLENGE_JSON_START\n{"verdict": "resolvable", "reasoning": "'
            + reasoning + '", "assumption": "' + assumption + '"}\n'
            'CHALLENGE_JSON_END')


class _FakeSupervisor:
    def __init__(self, reply, *, raises=False):
        self.reply, self.raises = reply, raises
        self.calls = 0

    async def run(self, prompt, *, cwd=None, max_turns=1, effort="low", **kw):
        self.calls += 1
        if self.raises:
            raise RuntimeError("supervisor infra down")
        return SimpleNamespace(final_text=self.reply, is_error=False,
                               tokens_used=10, cache_read_tokens=0,
                               cache_creation_tokens=0, output_tokens=5,
                               num_turns=1, session_id="s",
                               stop_reason="end_turn")


class _InertAdvisory:
    """Every NON-supervisor advisory role (intake eval, distill) gets this —
    an empty reply that each advisory consumer already treats as a skip."""

    async def run(self, prompt, **kw):
        return SimpleNamespace(final_text="", is_error=False, tokens_used=0,
                               cache_read_tokens=0, cache_creation_tokens=0,
                               output_tokens=0, num_turns=1, session_id="s",
                               stop_reason="end_turn")


def _patch_supervisor(monkeypatch, fake):
    import no_human.agent.advisory as adv

    def factory(model, role):
        return fake if role == "supervisor" else _InertAdvisory()

    monkeypatch.setattr(adv, "advisory_backend", factory)


def _gate_on(tmp_path):
    cfg = _config(tmp_path)
    cfg.data["blockers"]["challenge"] = True
    return cfg


# ── parse layer ─────────────────────────────────────────────────────────── #

def test_parse_challenge_is_fail_safe():
    """Anything less than a well-formed verdict WITH an assumption returns
    None — and None always honors the blocker."""
    assert parse_challenge("no block at all") is None
    assert parse_challenge(
        'CHALLENGE_JSON_START\n{"verdict": "maybe"}\nCHALLENGE_JSON_END') is None
    # resolvable WITHOUT an assumption: nothing to proceed under → honored
    assert parse_challenge(
        'CHALLENGE_JSON_START\n{"verdict": "resolvable", "reasoning": "r", '
        '"assumption": ""}\nCHALLENGE_JSON_END') is None
    v = parse_challenge(_resolvable())
    assert v.verdict == "resolvable" and "snake_case" in v.assumption


def test_only_judgment_call_categories_are_challengeable():
    """The external categories are a human's problem no matter how eloquent
    the supervisor is — the set is pinned."""
    assert CHALLENGEABLE == {BlockerCategory.AMBIGUITY,
                             BlockerCategory.NOVEL_UNKNOWN,
                             BlockerCategory.IMPOSSIBLE}


def test_challenge_prompt_carries_the_never_lower_the_bar_rule():
    b = Blocker(category=BlockerCategory.AMBIGUITY, transient=False,
                confidence=0.9, goal="g", question="q?")
    p = build_challenge_prompt("task", ["crit"], b)
    assert "weakening a test" in p and "EXTERNAL" in p
    assert "score" not in p.lower()          # categorical, never numeric


# ── the loop-level invariant ────────────────────────────────────────────── #

async def test_resolvable_costs_the_attempt_then_the_second_blocker_parks(
    bare_repo, tmp_path, store, monkeypatch
):
    """The whole invariant in one run: attempt 1's AMBIGUITY blocker is judged
    resolvable → recorded FAILED, assumption on record, loop retries; the
    agent blocks again → challenged is spent → honored → parks. Never a fake
    'done', never more than one challenge."""
    fake = _FakeSupervisor(_resolvable())
    _patch_supervisor(monkeypatch, fake)
    cfg = _gate_on(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(_AMBIGUITY_JSON),
                        SlackNotifier(None))
    t = Task.new("add helper", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_INPUT   # honest park survives
    assert fake.calls == 1, "exactly one challenge per task"
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 2, "the challenge bought exactly one more attempt"
    assert "challenged" in (attempts[0].get("failure_reason") or "")
    refreshed = await store.get_task(t.id)
    ctx = refreshed.context or {}
    assert ctx.get("blocker_challenged") is True
    assert any("snake_case" in a for a in ctx.get("assumptions") or []), (
        "the assumption is on record for the retry and the PR body")
    assert refreshed.blocker["category"] == "AMBIGUITY"


async def test_external_verdict_parks_immediately(
    bare_repo, tmp_path, store, monkeypatch
):
    fake = _FakeSupervisor(
        'CHALLENGE_JSON_START\n{"verdict": "external", "reasoning": '
        '"criteria 1 and 2 genuinely contradict", "assumption": ""}\n'
        'CHALLENGE_JSON_END')
    _patch_supervisor(monkeypatch, fake)
    cfg = _gate_on(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(_AMBIGUITY_JSON),
                        SlackNotifier(None))
    t = Task.new("add helper", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert fake.calls == 1
    assert len(await store.list_attempts(t.id)) == 1, (
        "an external verdict must not burn a retry")
    refreshed = await store.get_task(t.id)
    assert (refreshed.context or {}).get("blocker_challenged") is True


async def test_external_categories_are_never_challenged(
    bare_repo, tmp_path, store, monkeypatch
):
    fake = _FakeSupervisor(_resolvable())
    _patch_supervisor(monkeypatch, fake)
    cfg = _gate_on(tmp_path)
    bjson = ('{"category": "MISSING_ACCESS", "confidence": 0.95, '
             '"question": "Grant repo write?", '
             '"root_cause_hypothesis": "token lacks scope"}')
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson),
                        SlackNotifier(None))
    t = Task.new("push the fix", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status in (TaskStatus.ESCALATED, TaskStatus.AWAITING_INPUT,
                              TaskStatus.BLOCKED)
    assert fake.calls == 0, "a missing credential is a human's problem"


async def test_supervisor_failure_honors_the_blocker(
    bare_repo, tmp_path, store, monkeypatch
):
    """Fail open toward honesty: the gate's own infrastructure breaking must
    never stand between the agent and its honest park."""
    fake = _FakeSupervisor("", raises=True)
    _patch_supervisor(monkeypatch, fake)
    cfg = _gate_on(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(_AMBIGUITY_JSON),
                        SlackNotifier(None))
    t = Task.new("add helper", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert fake.calls == 1
    assert len(await store.list_attempts(t.id)) == 1


async def test_a_challenged_attempts_work_survives_into_the_next_attempt(
    bare_repo, tmp_path, store, monkeypatch
):
    """The gate buys ONE more attempt; that attempt must not start from a
    blank tree. `reset_agent_workspace` hard-resets before every attempt, so a
    challenged return without a checkpoint discards exactly the work this gate
    exists to save — measured as 'workspace reset discarded 1 uncommitted
    leftover(s)' before the checkpoint call existed. the bounded-loop rule requires the
    checkpoint; this asserts it, and that the file is still there afterwards."""
    from pathlib import Path

    calls = {"n": 0}
    trees: list[tuple[int, bool]] = []

    def write_work(cwd):
        # ONLY on attempt 1. The backend's mutate hook fires every attempt, so
        # a writer that always writes lets attempt 2's honored-blocker
        # checkpoint put the file in git even when attempt 1's work was
        # discarded — the test would then pass with the fix reverted, which is
        # exactly what it did before this counter existed.
        calls["n"] += 1
        # What the attempt SEES. Committing the work is not the property —
        # `_resume_branch_point` reads only `resume_from.sha` and
        # `handoff.wip_sha`, so a checkpoint whose sha nothing records leaves
        # the commit in the object store and the next attempt branching from
        # base: recoverable by a human, invisible to the agent.
        trees.append((calls["n"], Path(cwd, "attempt1_work.py").exists()))
        if calls["n"] == 1:
            Path(cwd, "attempt1_work.py").write_text("def helper():\n    return 1\n")

    events: list = []
    fake = _FakeSupervisor(_resolvable())
    _patch_supervisor(monkeypatch, fake)
    cfg = _gate_on(tmp_path)
    orch = Orchestrator(store, cfg.data,
                        BlockerBackend(_AMBIGUITY_JSON, mutate=write_work),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add helper", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    # The property, not a proxy: the file attempt 1 wrote must still exist in
    # git after the run. A later checkpoint (the SECOND blocker is honored and
    # `_raise_blocker` checkpoints too) would satisfy a mere "a checkpoint
    # happened" assertion while attempt 1's work was already discarded — that
    # is exactly what this test caught when it was written that way.
    log = subprocess.run(["git", "log", "--all", "--name-only", "--format=%s"],
                         cwd=bare_repo, capture_output=True, text=True).stdout
    assert calls["n"] >= 2, f"expected a second attempt after the challenge, got {calls['n']}"
    assert "attempt1_work.py" in log, (
        "attempt 1's work never reached git — the challenged return did not "
        "checkpoint, so the next attempt's workspace reset discarded it")
    # PARTIAL, not BLOCKED: `_is_wip_partial` recognises only this prefix, and
    # `_is_own_partial` uses it to keep the zero-diff honesty gate armed. A
    # BLOCKED label here reads as "a human gated this" and credits attempt 1's
    # commits to whatever the next attempt does.
    assert "WIP-PARTIAL" in log, log[:400]
    later = [seen for n, seen in trees if n >= 2]
    assert later and all(later), (
        "the attempt the gate bought started WITHOUT attempt 1's work — the "
        f"checkpoint was made but never recorded as a branch point: {trees}")


def test_the_shipped_default_turns_the_gate_on():
    """Every test here forces `blockers.challenge` True through `_gate_on`, so
    flipping the SHIPPED default to False would disable the whole feature with
    the suite green. Pin the default itself."""
    from no_human.config import load_config

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        cfg = load_config(Path(d) / "config.yaml")
    assert cfg.data["blockers"]["challenge"] is True


async def test_an_empty_attempt_after_a_challenge_is_not_credited(
    bare_repo, tmp_path, store, monkeypatch
):
    """The honesty gate the checkpoint's LABEL decides.

    The attempt the challenge buys inherits attempt 1's commits as its branch
    point. If that checkpoint is labelled `[WIP-BLOCKED]`, `_is_wip_partial`
    does not recognise it, `_is_own_partial` concludes a human gated the work,
    and an attempt that edits NOTHING is credited with it — measured as
    AWAITING_APPROVAL with a PR opened on abandoned half-work, which is exactly
    the "never convert a park into done" invariant this gate is written around.

    So: attempt 1 does real work and raises a challengeable blocker; every
    later attempt edits nothing. The task must NOT reach approval."""
    from pathlib import Path

    from no_human.agent.claude_backend import AgentResult

    calls = {"n": 0}

    class _BlockThenClaimDone:
        """Attempt 1: real work + a challengeable blocker. Every attempt after:
        edits nothing and says "done" — the shape that reaches the zero-diff
        credit path (a second BLOCKER would park instead, and never test it)."""

        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                Path(cwd, "helper.py").write_text("def helper():\n    return 1\n")
                text = ("I cannot proceed without lowering the bar.\n"
                        "BLOCKER_JSON_START\n" + _AMBIGUITY_JSON + "\nBLOCKER_JSON_END\n")
            else:
                text = "done"
            return AgentResult(final_text=text, num_turns=1, is_error=False,
                               tokens_used=50, session_id="s",
                               stop_reason="end_turn")

    fake = _FakeSupervisor(_resolvable())
    _patch_supervisor(monkeypatch, fake)
    cfg = _gate_on(tmp_path)
    orch = Orchestrator(store, cfg.data, _BlockThenClaimDone(), SlackNotifier(None))
    t = Task.new("add helper", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL, (
        "an attempt that edited nothing was credited with the challenged "
        "attempt's commits and opened a PR on abandoned work")
    attempts = await store.list_attempts(t.id)
    assert not any(a.get("status") == "succeeded" for a in attempts), (
        f"no attempt did deliverable work, yet one is recorded succeeded: "
        f"{[(a.get('status'), (a.get('failure_reason') or '')[:40]) for a in attempts]}")
