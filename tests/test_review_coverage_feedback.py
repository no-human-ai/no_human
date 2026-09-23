"""A coverage rejection must feed the next review round.

Background: `InspectionTracker.rejection()` (diff_coverage.py) fail-closes a
verdict that never referenced a path the per-file budget cut from the diff.
`_review_once` discards that verdict and hands the rejection reason back as
`(None, reason, result)`. Before this change, `_agent_review` retried round 2
with the IDENTICAL prompt, so a reviewer that judged those paths not worth
opening judged the same way again — a deterministic repeat, billed at double
the turn budget, ending in `ReviewerUnavailable`. These tests pin that round 2
now carries the rejection (and ONLY the rejection) forward, and that the
coverage guard itself still fail-closes exactly as before.
"""

from __future__ import annotations

import asyncio

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.review.diff_coverage import InspectionTracker, coverage_rejection_paths
from no_human.review.reviewer import AdversarialReviewer, ReviewerUnavailable


def _passing_block() -> str:
    # Same literal as tests/test_diff_coverage.py::_passing_block.
    return (
        "REVIEW_JSON_START\n"
        '{"passed": true, "items": [{"label": "ok", "passed": true, '
        '"severity": "low", "evidence": "covered"}]}\n'
        "REVIEW_JSON_END\n"
    )


class _PromptRecordingBackend:
    """Records every prompt it is called with; each call's `AgentResult` (or
    the `HANG` sentinel, which sleeps past the caller's wait_for) is picked
    from `results` by call index. `inspect_paths_by_round[call_index]`, if
    given, emits one `Read` tool_use per listed path before returning."""

    model = "test"
    HANG = object()

    def __init__(self, results, inspect_paths_by_round=None):
        self.results = results
        self.inspect_paths_by_round = inspect_paths_by_round or {}
        self.prompts: list[str] = []
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kwargs):
        idx = self.calls
        self.calls += 1
        self.prompts.append(prompt)
        paths = self.inspect_paths_by_round.get(idx)
        if paths and on_event is not None:
            for path in paths:
                on_event(AgentEvent(
                    "tool_use", tool_name="Read", tool_input={"file_path": path}))
        result = self.results[idx] if idx < len(self.results) else self.results[-1]
        if result is _PromptRecordingBackend.HANG:
            await asyncio.sleep(5)
        return result


async def test_coverage_rejection_is_fed_verbatim_into_the_next_round(tmp_path):
    """AC-1: round 1 never references the required paths, so round 2's prompt
    must carry the exact unreferenced paths plus an open-the-files
    instruction, and must not be the unmodified round-1 prompt."""
    backend = _PromptRecordingBackend([
        AgentResult(final_text=_passing_block(), num_turns=1, is_error=False,
                    tokens_used=10, session_id="s1", stop_reason="end_turn"),
        AgentResult(final_text=_passing_block(), num_turns=1, is_error=False,
                    tokens_used=10, session_id="s2", stop_reason="end_turn"),
    ])
    reviewer = AdversarialReviewer(backend=backend, timeout=1)

    with pytest.raises(ReviewerUnavailable):
        await reviewer._agent_review(
            "prompt", tmp_path, max_turns=1,
            required_inspections=["tests/hidden.py", "src/deep.py"],
        )

    assert len(backend.prompts) == 2
    round1, round2 = backend.prompts
    assert round2 != round1
    assert round2.startswith(round1)
    assert "tests/hidden.py" in round2
    assert "src/deep.py" in round2
    # No backward leakage: round 1 was sent before round 1's own rejection
    # existed, so it cannot already contain the paths it will be rejected for.
    assert "tests/hidden.py" not in round1
    assert "src/deep.py" not in round1
    assert "read each path" in round2


@pytest.mark.parametrize("case", ["timed_out", "no_review_json", "errored_session"])
async def test_only_a_coverage_rejection_changes_the_prompt(tmp_path, monkeypatch, case):
    """AC-2: the other three no-verdict reasons must not touch the prompt."""
    if case == "timed_out":
        import no_human.review.reviewer as rv
        monkeypatch.setattr(rv, "_REVIEW_MIN_RETRY_TIMEOUT", 0.05)
        backend = _PromptRecordingBackend([
            _PromptRecordingBackend.HANG, _PromptRecordingBackend.HANG,
        ])
        reviewer = AdversarialReviewer(backend=backend)
        kwargs = dict(timeout=0.3)
    elif case == "no_review_json":
        result = AgentResult(final_text="no verdict here", num_turns=1,
                              is_error=False, tokens_used=10, session_id="s",
                              stop_reason="end_turn")
        backend = _PromptRecordingBackend([result, result])
        reviewer = AdversarialReviewer(backend=backend, timeout=1)
        kwargs = dict(max_turns=1)
    else:
        result = AgentResult(final_text=_passing_block(), num_turns=1,
                              is_error=True, tokens_used=10, session_id="s",
                              stop_reason="max_turns")
        backend = _PromptRecordingBackend([result, result])
        reviewer = AdversarialReviewer(backend=backend, timeout=1)
        kwargs = dict(max_turns=1)

    with pytest.raises(ReviewerUnavailable):
        await reviewer._agent_review(
            "prompt", tmp_path, required_inspections=None, **kwargs,
        )

    assert len(backend.prompts) == 2
    assert backend.prompts[1] == backend.prompts[0]


async def test_a_reviewer_that_never_opens_the_cut_paths_still_ends_unavailable(tmp_path):
    """AC-3, positive control: `_review_once` ends with
    `rejection = tracker.rejection(); if rejection: return None, rejection, result`
    — delete that `if rejection: ...` line in reviewer.py and this test starts
    returning a passing `ReviewDecision` instead of raising, because the guard
    that discards an unreferenced-path verdict is what that line enforces.
    Feedback is a remedy the reviewer may act on, never a bypass: a reviewer
    that ignores it in every round must still end in `ReviewerUnavailable`.
    """
    backend = _PromptRecordingBackend([
        AgentResult(final_text=_passing_block(), num_turns=1, is_error=False,
                    tokens_used=10, session_id="s1", stop_reason="end_turn"),
        AgentResult(final_text=_passing_block(), num_turns=1, is_error=False,
                    tokens_used=10, session_id="s2", stop_reason="end_turn"),
    ])
    reviewer = AdversarialReviewer(backend=backend, timeout=1)

    with pytest.raises(ReviewerUnavailable):
        await reviewer._agent_review(
            "prompt", tmp_path, max_turns=1,
            required_inspections=["tests/hidden.py"],
        )

    assert backend.calls == 2


async def test_a_reviewer_that_opens_the_paths_after_feedback_passes(tmp_path):
    """Feedback is a remedy, not a bypass: a reviewer that opens the cut path
    ONLY after seeing round 1's feedback reaches a real, passing verdict on
    round 2 — the guard clears once the evidence is actually there."""
    backend = _PromptRecordingBackend(
        [
            AgentResult(final_text=_passing_block(), num_turns=1, is_error=False,
                        tokens_used=10, session_id="s1", stop_reason="end_turn"),
            AgentResult(final_text=_passing_block(), num_turns=1, is_error=False,
                        tokens_used=10, session_id="s2", stop_reason="end_turn"),
        ],
        inspect_paths_by_round={1: ["tests/hidden.py"]},
    )
    reviewer = AdversarialReviewer(backend=backend, timeout=1)

    decision = await reviewer._agent_review(
        "prompt", tmp_path, max_turns=1,
        required_inspections=["tests/hidden.py"],
    )

    assert decision.passed is True
    assert len(backend.prompts) == 2


async def test_feedback_does_not_change_the_clean_pass(tmp_path):
    """A reviewer that opens the cut path on round 1 passes on round 1,
    unaffected by any of this — no rejection ever happens, so no feedback is
    ever generated."""
    backend = _PromptRecordingBackend(
        [
            AgentResult(final_text=_passing_block(), num_turns=1, is_error=False,
                        tokens_used=10, session_id="s1", stop_reason="end_turn"),
        ],
        inspect_paths_by_round={0: ["tests/hidden.py"]},
    )
    reviewer = AdversarialReviewer(backend=backend, timeout=1)

    decision = await reviewer._agent_review(
        "prompt", tmp_path, max_turns=1,
        required_inspections=["tests/hidden.py"],
    )

    assert decision.passed is True
    assert backend.calls == 1


def test_coverage_rejection_paths_ignores_other_reasons():
    assert coverage_rejection_paths("") == []
    assert coverage_rejection_paths("timed out after 5s") == []
    assert coverage_rejection_paths("no REVIEW_JSON block") == []

    tracker = InspectionTracker(["x/y.py"])
    assert coverage_rejection_paths(tracker.rejection()) == ["x/y.py"]

    # Most-recent-only policy: round 2 named only the still-missing path (it
    # already referenced the other one), so feeding round 3 must not resurrect
    # the path round 2 cleared.
    round2_tracker = InspectionTracker(["a/one.py", "b/two.py"])
    round2_tracker.note_event(AgentEvent(
        "tool_use", tool_name="Read", tool_input={"file_path": "a/one.py"}))
    assert coverage_rejection_paths(round2_tracker.rejection()) == ["b/two.py"]
