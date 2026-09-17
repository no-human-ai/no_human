"""Regression tests: the scoping grill must not re-ask a question the user
already answered, and must treat a decline-to-specify answer as a STOP
signal rather than an ordinary answer to keep probing.

Hermetic: every test asserts against the prompt STRING built by
_build_qa_section / grill_step, using a CapturingBackend (same pattern as
tests/test_grill.py::test_grill_step_prompt_includes_qa_history). No live
model call anywhere.
"""

import ast
from pathlib import Path

import pytest

from no_human.intake import grill
from no_human.intake.grill import (
    MAX_ROUNDS_DEFAULT,
    GrillQuestion,
    GrillResult,
    _build_qa_section,
    grill_step,
    parse_grill_response,
)


class CapturingBackend:
    """Records the prompt it was given and returns a canned 'done' response."""

    def __init__(self, response_text=None):
        self.response_text = response_text or (
            '```json\n{"type": "done", "title": "T", "description": "D", '
            '"acceptance_criteria": ["AC"]}\n```'
        )
        self.captured_prompts = []

    async def run(self, prompt, *, cwd, max_turns, effort=None):
        self.captured_prompts.append(prompt)

        class _R:
            final_text = self.response_text

        return _R()


# --------------------------------------------------------------------------- #
# Reproduction                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_round_two_prompt_forbids_reasking_the_answered_question():
    qa_history = [
        {
            "question": "Should I widen the retry window?",
            "answer": "Proceed with what we have",
        }
    ]
    backend = CapturingBackend()
    await grill_step("Fix X", "desc", None, qa_history, backend)

    prompt = backend.captured_prompts[0]
    assert "Q1: Should I widen" in prompt
    assert "A1: Proceed with what we have" in prompt
    assert "round 2 of 5" in prompt
    assert "NEVER ask a question that is already answered" in prompt
    assert "RULE VIOLATION" in prompt


# --------------------------------------------------------------------------- #
# Rule present / absent depending on history                                  #
# --------------------------------------------------------------------------- #


def test_no_repeat_rule_present_when_history_nonempty():
    section = _build_qa_section([{"question": "q?", "answer": "a"}])
    assert "Previous Q&A:" in section
    assert "NEVER ask a question that is already answered" in section
    assert "rewording or narrowed" in " ".join(section.split())
    assert "narrowed variant" in " ".join(section.split())


def test_no_repeat_rule_absent_when_history_empty():
    assert _build_qa_section([]) == ""


@pytest.mark.asyncio
async def test_no_repeat_rule_absent_from_round_one_prompt():
    backend = CapturingBackend()
    await grill_step("Fix X", "desc", None, [], backend)
    prompt = backend.captured_prompts[0]
    assert "Previous Q&A:" not in prompt
    assert "NEVER ask a question that is already answered" not in prompt


# --------------------------------------------------------------------------- #
# Decline-to-specify handling                                                 #
# --------------------------------------------------------------------------- #


def test_decline_to_specify_is_an_explicit_stop_signal():
    section = _build_qa_section([{"question": "q?", "answer": "a"}])
    assert "DECLINE" in section
    assert "STOP signal" in section
    assert "Do NOT re-ask" in section
    assert 'type: "done"' in section


@pytest.mark.parametrize(
    "answer",
    [
        "Proceed with what we have",
        "you decide",
        "whatever you think best",
        "just ship it",
        "no me importa, sigue",
    ],
)
def test_decline_rule_is_phrasing_agnostic(answer):
    section = _build_qa_section([{"question": "q?", "answer": answer}])
    assert "not by matching any particular words" in section
    # The rule text itself never varies with the answer's wording — it is
    # produced by the model reading the answer, not by Python branching on it.
    baseline = _build_qa_section([{"question": "q?", "answer": "anything else"}])
    baseline_rules = baseline.split("RULES ABOUT THE Q&A ABOVE:", 1)[1]
    this_rules = section.split("RULES ABOUT THE Q&A ABOVE:", 1)[1]
    assert baseline_rules == this_rules


def test_decline_recognition_is_not_a_hardcoded_phrase_list():
    """Guard against reintroducing a closed list of decline phrases.

    The model — not Python — recognises a decline-to-specify answer, by
    reading its meaning. grill.py must never gain a phrase table (a list/
    set/tuple of string literals containing two or more of these phrasings)
    or a module-level `_DECLINE*`/`DECLINE*` constant, either of which would
    be exactly the closed-list defect this repo has hit before.

    The existing suggestions literal
    ["A: Let me add more context", "B: Proceed with what we have"] contains
    exactly one probe member ("proceed with what we have"), so the >=2
    threshold below passes today and trips the moment a real phrase table
    appears.
    """
    probes = [
        "no preference",
        "you decide",
        "skip",
        "proceed with what we have",
        "up to you",
        "whatever you think",
        "don't care",
    ]
    source = Path(grill.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            strings = [
                elt.value
                for elt in node.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
            lowered = [s.lower() for s in strings]
            hits = sum(
                1 for probe in probes if any(probe in s for s in lowered)
            )
            assert hits < 2, (
                f"Found a collection with {hits} decline-phrase probe hits: "
                f"{strings!r} — looks like a hard-coded decline phrase list."
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.upper().lstrip(
                    "_"
                ).startswith("DECLINE"):
                    pytest.fail(
                        f"Found module-level name {target.id!r} — decline "
                        "recognition must not be a Python-side constant."
                    )


# --------------------------------------------------------------------------- #
# Parser / formats / rounds unchanged                                         #
# --------------------------------------------------------------------------- #


def test_both_response_formats_still_parse_with_history():
    qa_history = [{"question": "Scope?", "answer": "Narrow"}]

    question_text = (
        '```json\n{"type": "question", "question": "Which DB?", '
        '"suggestions": ["A: SQLite", "B: Postgres"]}\n```'
    )
    q = parse_grill_response(question_text, round_n=2, qa_history=qa_history)
    assert isinstance(q, GrillQuestion)
    assert q.question == "Which DB?"
    assert q.round == 2

    done_text = (
        '```json\n{"type": "done", "title": "T", "description": "D", '
        '"acceptance_criteria": ["AC1"]}\n```'
    )
    d = parse_grill_response(done_text, round_n=2, qa_history=qa_history)
    assert isinstance(d, GrillResult)
    assert d.title == "T"
    assert d.qa_log == qa_history


def test_max_rounds_default_unchanged():
    assert MAX_ROUNDS_DEFAULT == 5


# --------------------------------------------------------------------------- #
# Force path unaffected                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_force_clause_still_present_at_max_rounds():
    qa_history = [{"question": f"Q{i}", "answer": f"A{i}"} for i in range(4)]
    backend = CapturingBackend(
        response_text='```json\n{"type": "question", "question": "More?"}\n```'
    )
    result = await grill_step(
        "Fix X", None, None, qa_history, backend, max_rounds=5
    )
    prompt = backend.captured_prompts[0]
    assert "This is the LAST round" in prompt
    assert "NEVER ask a question that is already answered" in prompt
    assert isinstance(result, GrillResult)
