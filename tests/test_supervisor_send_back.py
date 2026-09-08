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

        # `decision.action == "continue"` alone would be tautological (the
        # fake always returns SUPERVISOR_CONTINUE) — the real assertions are
        # on the PROMPT the fake was handed: no instruction anywhere tells it
        # to enforce the superseded criterion.
        assert decision.action == "continue"
        assert decision.raw == "SUPERVISOR_CONTINUE"
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


# ── Review send-back on the previous round (70fcfcca) ──────────────────── #
#
# Two defects, both proven on task c1a0416d's OWN send_back_feedback rows:
#   1. BLOCKER: a per-message 600-char cap cut the incident's 3338-char human
#      send-back BEFORE its "AMENDED" clause even started.
#   2. MAJOR: the block rendered every writer, human or machine — with the
#      old 3-entry cap, two later machine notices (pr_conflict, pr_ci, ...)
#      EVICTED the human amendment entirely.

class TestSendBackRenderingRobustness:
    def test_long_human_entry_is_never_truncated(self):
        # Mirrors the incident shape: "AMENDED" at index 576, "[WIP-PARTIAL]"
        # at 795, total length 3338 — well past the old 600-char per-message
        # cap, which cut the text before either landmark.
        prefix = "x" * 576
        middle = "AMENDED" + ("y" * (795 - 576 - len("AMENDED")))
        tail = "[WIP-PARTIAL] head must be treated like a [WIP-BLOCKED] head "
        message = prefix + middle + tail
        message += "z" * (3338 - len(message))
        assert len(message) == 3338
        assert message.index("AMENDED") == 576
        assert message.index("[WIP-PARTIAL]") == 795

        text, unreadable = format_send_back_feedback(
            [{"at": "2026-09-08T08:06:16Z", "author": "human", "message": message}]
        )
        assert unreadable is False
        assert "AMENDED" in text
        assert "[WIP-PARTIAL]" in text
        # The whole tail (well past the old 600-char cap) survives.
        assert "must be treated like a [WIP-BLOCKED] head" in text

        prompt = build_evaluation_prompt(
            task_title="t", acceptance_criteria=[CRITERION], rules="",
            profile_context="", window=[], total_calls=0,
            send_back_feedback=text,
        )
        assert "AMENDED" in prompt
        assert "[WIP-PARTIAL]" in prompt

    def test_machine_entries_never_evict_the_human_amendment(self):
        # Real shapes from the writers named in the send-back: `pr_conflict`
        # (blockers/wake.py) and `pr_ci` (blockers/wake.py) both stamp a
        # `source`; the human send-back (api/app.py / cli `nh reject`) never
        # does. With the old 3-entry cap and no source filtering, these two
        # machine entries pushed the human amendment out of the window.
        entries = [
            {"at": "2026-09-08T08:06:16Z", "author": "human", "message": AMENDMENT},
            {
                "at": "2026-09-08T08:10:00Z", "author": "pr_conflict",
                "message": "The PR has a textual conflict with main.",
                "source": "pr_conflict",
            },
            {
                "at": "2026-09-08T08:12:00Z", "author": "ci",
                "message": "The PR's CI is failing.",
                "source": "pr_ci",
            },
        ]
        text, unreadable = format_send_back_feedback(entries)
        assert unreadable is False
        assert AMENDMENT in text
        assert "textual conflict with main" not in text
        assert "CI is failing" not in text

        prompt = build_evaluation_prompt(
            task_title="t", acceptance_criteria=[CRITERION], rules="",
            profile_context="", window=[], total_calls=0,
            send_back_feedback=text,
        )
        assert AMENDMENT in prompt
        assert "textual conflict with main" not in prompt

    def test_human_entries_with_no_author_are_labelled_human(self):
        # api/app.py and cli/commands.py write `{"at": ..., "message": ...}`
        # with no `author` key at all — rendering that as `[at] : msg` (empty
        # author) is a display bug even though the text itself is present.
        text, unreadable = format_send_back_feedback(
            [{"at": "2026-09-08T08:06:16Z", "message": AMENDMENT}]
        )
        assert unreadable is False
        assert "] human:" in text

    def test_every_named_machine_source_is_excluded(self):
        # Every writer named in the review send-back — repro_gate
        # (orchestrator.py), tamper_adjudication (orchestrator.py),
        # pr_conflict/pr_ci/ci_gate/pr_comment (blockers/wake.py) — is
        # excluded individually, not just "some machine entry got through".
        from no_human.agent.supervisor import _MACHINE_SEND_BACK_SOURCES

        assert _MACHINE_SEND_BACK_SOURCES == {
            "repro_gate", "tamper_adjudication", "pr_conflict", "pr_ci",
            "ci_gate", "pr_comment",
        }
        for source in _MACHINE_SEND_BACK_SOURCES:
            entries = [
                {"at": "1", "author": "human", "message": AMENDMENT},
                {
                    "at": "2", "author": "someone", "message": "machine notice",
                    "source": source,
                },
            ]
            text, unreadable = format_send_back_feedback(entries)
            assert unreadable is False, source
            assert AMENDMENT in text, source
            assert "machine notice" not in text, source

    def test_all_machine_entries_yields_no_block_not_unreadable(self):
        # A task with ONLY machine send-back rows (no human entry yet) must
        # render nothing — never the fail-closed "COULD NOT BE READ" path,
        # which is reserved for entries we could not READ at all.
        entries = [
            {
                "at": "x", "author": "pr_conflict", "message": "conflict",
                "source": "pr_conflict",
            },
        ]
        text, unreadable = format_send_back_feedback(entries)
        assert text == ""
        assert unreadable is False
