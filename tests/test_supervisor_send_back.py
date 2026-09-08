"""The supervisor must honour the LATEST human send-back over stale criteria.

Task c1a0416d: a human send-back AMENDED acceptance criterion 3, but the
supervisor's prompt/context was built only from `task.acceptance_criteria`
(the ORIGINAL text) — it never read `task.context["send_back_feedback"]` — so
it corrected the coder straight back to the superseded criterion, the coder
reverted its (correct) fix, and the whole round was wasted.

Covers:
  - `format_send_back_feedback` (fail-closed formatter)
  - the precedence block in `build_evaluation_prompt` / `build_preflight_prompt`
  - `SupervisorHook` wiring the feedback into both prompts
  - `Orchestrator._build_supervisor` reading `task.context["send_back_feedback"]`
  - a fake-model decision test showing the prompt no longer instructs
    enforcement of the superseded criterion
  - unreadable send-back feedback is reported, never silently dropped
"""

from no_human.agent.supervisor import (
    SEND_BACK_UNREADABLE,
    SupervisorHook,
    build_evaluation_prompt,
    build_preflight_prompt,
    format_send_back_feedback,
)
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task


CRITERION = "criterion 3: treat a [WIP-PARTIAL] head as eligible"
AMENDMENT = (
    "criterion 3 is AMENDED: treat a [WIP-PARTIAL] head like a "
    "[WIP-BLOCKED] head"
)


# ── AC 1: feedback + precedence, RED on main (params don't exist there) ── #

class TestPromptIncludesSendBackFeedback:
    def test_prompt_includes_send_back_feedback_and_precedence(self):
        feedback_text, unreadable = format_send_back_feedback(
            [{"at": "2026-09-08T08:06:16Z", "author": "human", "message": AMENDMENT}]
        )
        assert unreadable is False
        prompt = build_evaluation_prompt(
            task_title="t", acceptance_criteria=[CRITERION], rules="",
            profile_context="", window=[], total_calls=0,
            send_back_feedback=feedback_text,
        )
        assert AMENDMENT in prompt
        assert "supersede" in prompt.lower()
        # Precedence sentence must name BOTH the criteria and a pinning test.
        assert "acceptance criteria" in prompt.lower()
        assert "test" in prompt.lower()

    def test_entries_render_newest_last(self):
        entries = [
            {"at": "1", "message": "first amendment"},
            {"at": "2", "message": "second amendment"},
            {"at": "3", "message": "third amendment"},
        ]
        text, unreadable = format_send_back_feedback(entries)
        assert unreadable is False
        prompt = build_evaluation_prompt(
            task_title="t", acceptance_criteria=["x"], rules="",
            profile_context="", window=[], total_calls=0,
            send_back_feedback=text,
        )
        assert (
            prompt.index("third amendment")
            > prompt.index("second amendment")
            > prompt.index("first amendment")
        )

    def test_formatter_tolerates_bare_string_entries(self):
        text, unreadable = format_send_back_feedback(["just a bare string message"])
        assert unreadable is False
        assert "just a bare string message" in text

    async def test_hook_passes_send_back_feedback_into_evaluation_prompt(self):
        seen = {}

        async def capture(prompt):
            seen["prompt"] = prompt
            return "SUPERVISOR_CONTINUE"

        hook = SupervisorHook(
            task_title="t", acceptance_criteria=[CRITERION], rules="",
            llm_call=capture, check_every=1,
            send_back_feedback=[{"at": "x", "author": "human", "message": AMENDMENT}],
        )
        hook.record("Edit", {"file_path": "a.py"}, "ok")
        await hook.evaluate()
        assert AMENDMENT in seen["prompt"]
        assert "supersede" in seen["prompt"].lower()

    def test_orchestrator_build_supervisor_carries_feedback(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {}
        task = Task(
            id="t1", source="test", title="Fix wake resume", repo_path=None,
            acceptance_criteria=[CRITERION],
            context={"send_back_feedback": [
                {"at": "2026-09-08T08:06:16Z", "author": "human", "message": AMENDMENT},
            ]},
        )
        hook = orch._build_supervisor(task)
        assert hook is not None
        text, unreadable = hook._send_back_text()
        assert unreadable is False
        assert AMENDMENT in text


# ── AC 2: fake supervisor model / no enforcement / fail-closed ─────────── #

class TestNoEnforcementOfSupersededCriterion:
    async def test_fake_supervisor_can_answer_continue_on_amended_behaviour(self):
        seen = {}

        async def fake_supervisor_model(prompt):
            # No live model anywhere — this fake stands in for the LLM tier
            # and just returns the tag a healthy, well-informed supervisor
            # would return once it sees the send-back supersedes criterion 3.
            seen["prompt"] = prompt
            return "SUPERVISOR_CONTINUE"

        hook = SupervisorHook(
            task_title="Resume a blocked task", acceptance_criteria=[CRITERION],
            rules="", llm_call=fake_supervisor_model, check_every=1,
            send_back_feedback=[{"at": "x", "author": "human", "message": AMENDMENT}],
        )
        # The recorded window is a coder edit implementing NOT-X: treating a
        # [WIP-PARTIAL] head as ineligible, like [WIP-BLOCKED] — exactly what
        # the ORIGINAL criterion (X) forbids and the send-back demands.
        hook.record(
            "Edit",
            {"file_path": "src/no_human/core/orchestrator.py"},
            "treat [WIP-PARTIAL] head as ineligible, same as [WIP-BLOCKED]",
        )
        decision = await hook.evaluate()

        assert decision.action == "continue"
        prompt = seen["prompt"]
        # The criterion text appears exactly once — under the criteria
        # heading — never repeated as a separate "enforce this" instruction.
        assert prompt.count(CRITERION) == 1
        # The precedence block is present and explicitly tells the supervisor
        # to side with the send-back over the (now-superseded) criterion.
        assert "PRECEDENCE" in prompt
        assert "SUPERSEDES" in prompt
        assert "side with the SEND-BACK" in prompt

    async def test_unreadable_send_back_is_reported_not_ignored(self):
        seen = {}

        async def capture(prompt):
            seen["prompt"] = prompt
            return "SUPERVISOR_CONTINUE"

        hook = SupervisorHook(
            task_title="t", acceptance_criteria=["x"], rules="",
            llm_call=capture, check_every=1,
            send_back_feedback=SEND_BACK_UNREADABLE,
        )
        hook.record("Edit", {"file_path": "a.py"}, "ok")
        await hook.evaluate()
        prompt = seen["prompt"]
        assert "COULD NOT BE READ" in prompt
        assert "Do NOT assume there is none" in prompt
        # Must NOT silently fall back to the normal precedence-with-entries
        # block — that would imply feedback was checked and found absent.
        assert "PRECEDENCE — the latest human send-back SUPERSEDES" not in prompt

    def test_unreadable_send_back_via_raising_context_is_reported(self):
        class _RaisingContextTask:
            id = "t2"
            title = "t"
            acceptance_criteria = ["x"]
            repo_path = None

            @property
            def context(self):
                raise RuntimeError("context store unreachable")

        orch = Orchestrator.__new__(Orchestrator)
        orch.config = {}
        hook = orch._build_supervisor(_RaisingContextTask())
        assert hook is not None
        text, unreadable = hook._send_back_text()
        assert text == ""
        assert unreadable is True

    def test_no_feedback_leaves_prompt_unchanged(self):
        prompt = build_evaluation_prompt(
            task_title="t", acceptance_criteria=["x"], rules="",
            profile_context="", window=[], total_calls=0,
        )
        assert "COULD NOT BE READ" not in prompt
        assert "HUMAN SEND-BACK FEEDBACK" not in prompt

    def test_preflight_prompt_also_carries_precedence_block(self):
        # The preflight check reads from the same original-criteria text, so
        # it needs the same fail-closed precedence block as the evaluation
        # prompt (guards against the plan being steered back to X too).
        text, unreadable = format_send_back_feedback(
            [{"at": "x", "author": "human", "message": AMENDMENT}]
        )
        prompt = build_preflight_prompt(
            task_title="t", acceptance_criteria=[CRITERION], rules="",
            skills="", plan="resume onto the WIP-PARTIAL head",
            send_back_feedback=text,
        )
        assert AMENDMENT in prompt
        assert "SUPERSEDES" in prompt

        unreadable_prompt = build_preflight_prompt(
            task_title="t", acceptance_criteria=[CRITERION], rules="",
            skills="", plan="resume onto the WIP-PARTIAL head",
            send_back_unreadable=True,
        )
        assert "COULD NOT BE READ" in unreadable_prompt
