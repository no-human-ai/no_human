"""RED-first tests: the supervisor hook's "unparseable LLM output, defaulting
to CONTINUE" fallback must not swallow a session/weekly-limit quota wall.
Today, both (a) an LLM response that IS the raw quota string (no
SUPERVISOR_* tag) and (b) the `evaluate()`/`preflight()` LLM call itself
raising an exception whose text carries the quota phrase are treated exactly
like any other unparseable/broken supervisor turn — logged as "defaulting to
CONTINUE" and waved through. They must instead be routed through the same
`no_human.core.bounds.quota_signal`/`quota_reason` classifier the coder path
already uses, and must abort the session (never silently continue) so the
orchestrator's bounded quota-park path takes over.

Covers acceptance criterion 2 (and its negative control): a supervisor call
that surfaces the exact live session-limit string logs a quota park and
never "defaulting to CONTINUE"; genuinely unparseable/broken non-quota
supervisor output keeps today's fail-open CONTINUE behaviour untouched.
"""

import logging

import pytest

from no_human.agent.supervisor import SupervisorHook, parse_decision
from no_human.core.bounds import QuotaExhausted

from .test_infra_not_work import _config, _run_one_attempt, bare_repo  # noqa: F401

_SESSION_LIMIT_TEXT = "You've hit your session limit · resets 4:20am (Asia/Jerusalem)"


# ── parse_decision: an unparseable tag-less quota string is not CONTINUE ── #

def test_parse_decision_session_limit_is_quota_park_not_continue(caplog):
    with caplog.at_level(logging.WARNING):
        decision = parse_decision(_SESSION_LIMIT_TEXT)

    assert decision.action == "quota_park", decision
    assert "defaulting to CONTINUE" not in caplog.text
    assert "quota" in caplog.text.lower()


def test_genuinely_unparseable_output_still_defaults_to_continue(caplog):
    """Negative control: today's behaviour for real unparseable prose (no
    quota phrase, no tag) must survive byte-for-byte."""
    with caplog.at_level(logging.WARNING):
        decision = parse_decision("Some random text with no tags at all.")

    assert decision.action == "continue"
    assert "defaulting to CONTINUE" in caplog.text


def test_parse_decision_long_prose_mentioning_quota_vocabulary_is_not_falsely_parked(
        caplog):
    """Regression for the review finding on attempt 1: `quota_signal` is a
    bare-phrase classifier ("usage limit", "rate limit exceeded", ...) that
    is only safe against text from an ERRORED result (see its docstring). A
    tag-less supervisor turn can be ordinary (if malformed) analysis that
    happens to discuss the CODER'S OWN rate-limit-handling work — that must
    not be misread as the supervisor's own call hitting a subscription wall."""
    prose = (
        "Looking at the coder's diff: the retry helper still doesn't back "
        "off correctly when the API responds with a rate limit exceeded "
        "error, and the usage limit check in the billing module needs a "
        "unit test. Otherwise the change looks reasonable, though I'd want "
        "to see it handle the out of quota case explicitly before calling "
        "this done."
    )
    with caplog.at_level(logging.WARNING):
        decision = parse_decision(prose)

    assert decision.action == "continue", decision
    assert "defaulting to CONTINUE" in caplog.text


# ── evaluate()/preflight(): an LLM exception carrying the quota phrase ──── #

async def test_evaluate_llm_raising_a_quota_error_parks():
    decisions = []

    async def llm_call(prompt):
        raise Exception(_SESSION_LIMIT_TEXT)

    hook = SupervisorHook(
        task_title="t", acceptance_criteria=["a"], rules="r",
        llm_call=llm_call, on_decision=decisions.append,
    )
    hook.record("Read", {}, "")

    decision = await hook.evaluate()

    assert decision.action == "quota_park", decision
    assert decisions and decisions[-1].action == "quota_park"


async def test_non_quota_llm_exception_still_fails_open_continue():
    """Negative control: a real LLM/transport error unrelated to quota must
    keep today's fail-open CONTINUE — the supervisor's own error is never a
    reason to block the coder. Mirrors
    tests/test_supervisor.py::test_evaluate_llm_error_defaults_continue."""
    async def broken_llm(prompt):
        raise RuntimeError("LLM down")

    hook = SupervisorHook(
        task_title="t", acceptance_criteria=[], rules="",
        llm_call=broken_llm, check_every=1,
    )
    hook.record("Read", {}, "")

    decision = await hook.evaluate()

    assert decision.action == "continue"


async def test_preflight_llm_raising_a_quota_error_parks():
    async def llm_call(prompt):
        raise Exception(_SESSION_LIMIT_TEXT)

    hook = SupervisorHook(
        task_title="t", acceptance_criteria=["x"], rules="", llm_call=llm_call,
    )

    decision = await hook.preflight("some plan")

    assert decision.action == "quota_park", decision


async def test_preflight_non_quota_error_still_fails_open():
    """Negative control mirroring
    tests/test_supervisor.py::test_preflight_llm_error_fails_open."""
    async def broken(prompt):
        raise RuntimeError("down")

    hook = SupervisorHook(
        task_title="t", acceptance_criteria=[], rules="", llm_call=broken,
    )

    decision = await hook.preflight("plan")

    assert decision.action == "continue"


# ── hook(): a quota_park decision must abort the session, never continue ── #

async def test_hook_quota_park_aborts_the_session():
    async def llm_call(prompt):
        raise Exception(_SESSION_LIMIT_TEXT)

    hook = SupervisorHook(
        task_title="t", acceptance_criteria=["a"], rules="r",
        llm_call=llm_call, check_every=1,
    )

    result = await hook.hook(
        {"tool_name": "Edit", "tool_input": {}, "tool_response": ""}, "id1", {},
    )

    assert result.get("continue_") is False, (
        f"a quota wall must abort the session, not silently continue: {result}")
    assert "quota" in result.get("stopReason", "").lower()


async def test_hook_non_quota_unparseable_still_continues():
    """Negative control mirroring
    tests/test_supervisor.py::test_hook_continue_returns_empty — a genuinely
    broken/unparseable non-quota turn must still return `{}` (continue)."""
    async def llm_call(prompt):
        return "garbled non-tagged prose, not a verdict"

    hook = SupervisorHook(
        task_title="t", acceptance_criteria=["a"], rules="r",
        llm_call=llm_call, check_every=1,
    )

    result = await hook.hook(
        {"tool_name": "Edit", "tool_input": {}, "tool_response": ""}, "id1", {},
    )

    assert result == {}


# ── orchestrator: a latched supervisor quota wall parks the attempt ─────── #

async def test_orchestrator_supervisor_quota_wall_parks_the_attempt(
        store, bare_repo, tmp_path):
    """Once the supervisor hook has observed a quota wall mid-session, the
    orchestrator must park the ATTEMPT as a quota exhaustion the next time it
    checks — never fall through to booking a normal coder failure/success as
    if the supervisor had said nothing."""
    from no_human.agent.claude_backend import AgentResult
    healthy = AgentResult(
        final_text="done", num_turns=2, is_error=False, tokens_used=100,
        session_id="s", stop_reason="end_turn",
    )
    orch, backend, task, repo = await _run_one_attempt(store, bare_repo, tmp_path, healthy)
    orch._supervisor_quota_wall = "supervisor: " + _SESSION_LIMIT_TEXT

    with pytest.raises(QuotaExhausted):
        await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls, "the backend never ran — the test proves nothing"
