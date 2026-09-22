"""C1: a hung SDK subprocess (Stream-closed transport) once wedged the whole
bench ~12min at 0% CPU because `await be.run()` never returns. These tests prove
every grill/intake/judge LLM call now has a hard wall-clock ceiling: a hang
becomes a TimeoutError the callers treat as advisory / fail-closed, never a
forever-wedge."""
import asyncio
from types import SimpleNamespace

from no_human.intake import evaluator
from no_human.eval import judge


class _HangBackend:
    def __init__(self):
        self.calls = 0

    async def run(self, *a, **k):
        self.calls += 1
        await asyncio.sleep(30)                    # longer than the patched ceiling
        return SimpleNamespace(final_text="UNREACHABLE", is_error=False)


def test_evaluator_bounded_run_times_out_to_sentinel(monkeypatch):
    monkeypatch.setattr(evaluator, "_LLM_TIMEOUT_S", 0.05)
    r = asyncio.run(evaluator._bounded_run(_HangBackend(), "prompt"))
    # sentinel, not a hang and not the backend's UNREACHABLE result
    assert r.final_text == "" and r.is_error is True


def test_judge_times_out_fail_closed(monkeypatch):
    monkeypatch.setattr(judge, "_JUDGE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(judge, "_RETRY_BACKOFF_S", 0.0)
    backend = _HangBackend()
    gj = judge.GoalJudge(backend=backend)
    v = asyncio.run(gj.judge(request="do x", criteria=[], agent_diff="",
                             outcome_status="done"))
    assert v.satisfied is False                    # fail-closed on timeout, no hang
    # Proves the TIMEOUT fired rather than the shared judge loop's retry-once
    # bound simply being large enough to still land under a wall-clock literal
    # on an idle box: the retry-once-then-fail-closed loop (`_judge_loop`)
    # calls `backend.run` at most twice (`for attempt in range(2)`) — a
    # backend that hangs forever therefore proves the per-call
    # `asyncio.wait_for(..., _JUDGE_TIMEOUT_S)` cut EACH call off (never let
    # one hang consume both attempts, and never fell through to a third,
    # unbounded retry) purely by the call count, with no dependence on how
    # fast the box actually ran those two cancellations.
    assert backend.calls == 2, (
        f"expected exactly 2 bounded attempts, backend ran {backend.calls}x")
