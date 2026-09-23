"""B2: Tests for the intake grill — response parsing and step flow."""

import pytest

from no_human.intake.grill import (
    GrillQuestion,
    GrillResult,
    parse_grill_response,
)


# --------------------------------------------------------------------------- #
# parse_grill_response                                                         #
# --------------------------------------------------------------------------- #


class TestParseGrillResponse:
    def test_question_from_json_block(self):
        text = (
            'I explored the repo and found the following.\n\n'
            '```json\n'
            '{"type": "question", "question": "Which module?", '
            '"suggestions": ["A: foo", "B: bar"]}\n'
            '```'
        )
        result = parse_grill_response(text, round_n=1, qa_history=[])
        assert isinstance(result, GrillQuestion)
        assert result.question == "Which module?"
        assert result.suggestions == ["A: foo", "B: bar"]
        assert result.round == 1

    def test_done_from_json_block(self):
        text = (
            '```json\n'
            '{"type": "done", "title": "Add caching", '
            '"description": "Add Redis caching to the API", '
            '"acceptance_criteria": ["GET /items cached for 60s", '
            '"cache invalidated on POST"]}\n'
            '```'
        )
        result = parse_grill_response(text, round_n=3, qa_history=[{"q": "x", "a": "y"}])
        assert isinstance(result, GrillResult)
        assert result.title == "Add caching"
        assert result.description == "Add Redis caching to the API"
        assert len(result.acceptance_criteria) == 2
        assert result.qa_log == [{"q": "x", "a": "y"}]

    def test_raw_json_no_fences(self):
        text = '{"type": "question", "question": "Scope?", "suggestions": ["A: narrow"]}'
        result = parse_grill_response(text, round_n=2, qa_history=[])
        assert isinstance(result, GrillQuestion)
        assert result.question == "Scope?"

    def test_fallback_on_garbage(self):
        result = parse_grill_response("random text no json", round_n=1, qa_history=[])
        assert isinstance(result, GrillQuestion)
        assert result.round == 1
        # Should produce a fallback question
        assert "detail" in result.question.lower() or "describe" in result.question.lower()

    def test_multiple_json_blocks_uses_last(self):
        text = (
            '```json\n{"type": "question", "question": "first?"}\n```\n'
            'Actually, let me reconsider.\n'
            '```json\n{"type": "done", "title": "T", "description": "D", '
            '"acceptance_criteria": ["AC1"]}\n```'
        )
        result = parse_grill_response(text, round_n=2, qa_history=[])
        assert isinstance(result, GrillResult)
        assert result.title == "T"

    def test_done_with_empty_criteria(self):
        text = '```json\n{"type": "done", "title": "T", "description": "D", "acceptance_criteria": []}\n```'
        result = parse_grill_response(text, round_n=1, qa_history=[])
        assert isinstance(result, GrillResult)
        assert result.acceptance_criteria == []

    def test_question_missing_suggestions(self):
        text = '```json\n{"type": "question", "question": "What?"}\n```'
        result = parse_grill_response(text, round_n=1, qa_history=[])
        assert isinstance(result, GrillQuestion)
        assert result.suggestions == []

    def test_null_json_fields_coerce_to_typed_defaults(self):
        # A present-but-null field is not an absent one: `dict.get(key, default)`
        # returns None for it, which violates the `str`/`list[str]` fields and
        # crashes a `for c in acceptance_criteria` consumer.
        done = parse_grill_response(
            '```json\n{"type": "done", "title": null, "description": null, '
            '"acceptance_criteria": null}\n```',
            round_n=1, qa_history=[],
        )
        assert isinstance(done, GrillResult)
        assert done.title == ""
        assert done.description == ""
        assert done.acceptance_criteria == []

        question = parse_grill_response(
            '```json\n{"type": "question", "question": null, "suggestions": null}\n```',
            round_n=1, qa_history=[],
        )
        assert isinstance(question, GrillQuestion)
        assert isinstance(question.question, str) and question.question
        assert question.suggestions == []


# --------------------------------------------------------------------------- #
# grill_step (with a fake backend)                                             #
# --------------------------------------------------------------------------- #


class FakeBackend:
    """Minimal backend that returns a canned response."""

    def __init__(self, final_text: str):
        self._text = final_text

    async def run(self, prompt, *, cwd, max_turns, effort=None):
        class _Result:
            final_text = self._text
        return _Result()


@pytest.mark.asyncio
async def test_grill_step_returns_question():
    from no_human.intake.grill import grill_step

    backend = FakeBackend(
        '```json\n{"type": "question", "question": "Which DB?", '
        '"suggestions": ["A: SQLite", "B: Postgres"]}\n```'
    )
    result = await grill_step("Add caching", None, None, [], backend)
    assert isinstance(result, GrillQuestion)
    assert result.question == "Which DB?"
    assert result.round == 1


@pytest.mark.asyncio
async def test_grill_step_returns_result():
    from no_human.intake.grill import grill_step

    backend = FakeBackend(
        '```json\n{"type": "done", "title": "Add SQLite cache", '
        '"description": "Cache API responses in SQLite", '
        '"acceptance_criteria": ["GET cached", "POST invalidates"]}\n```'
    )
    qa = [{"question": "Which DB?", "answer": "SQLite"}]
    result = await grill_step("Add caching", None, None, qa, backend)
    assert isinstance(result, GrillResult)
    assert result.title == "Add SQLite cache"
    assert len(result.acceptance_criteria) == 2


@pytest.mark.asyncio
async def test_grill_step_force_done_on_max_rounds():
    """When max_rounds is reached and agent still asks a question,
    the engine forces a GrillResult so the pipeline isn't blocked."""
    from no_human.intake.grill import grill_step

    backend = FakeBackend(
        '```json\n{"type": "question", "question": "More?"}\n```'
    )
    qa = [{"question": f"Q{i}", "answer": f"A{i}"} for i in range(4)]
    result = await grill_step("Fix X", None, None, qa, backend, max_rounds=5)
    assert isinstance(result, GrillResult)
    assert result.title == "Fix X"


@pytest.mark.asyncio
async def test_grill_step_prompt_includes_qa_history():
    """Verify the Q&A history is included in the prompt sent to the backend."""
    captured_prompts = []

    class CapturingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None):
            captured_prompts.append(prompt)
            class _R:
                final_text = '```json\n{"type": "done", "title": "T", "description": "D", "acceptance_criteria": ["AC"]}\n```'
            return _R()

    from no_human.intake.grill import grill_step

    qa = [{"question": "Scope?", "answer": "Narrow"}]
    await grill_step("Fix X", "desc", None, qa, CapturingBackend())
    assert "Q1: Scope?" in captured_prompts[0]
    assert "A1: Narrow" in captured_prompts[0]


# --------------------------------------------------------------------------- #
# API model round-trip                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_grill_step_timeout_returns_fallback_question():
    """When the backend takes too long, the grill returns a timeout question."""
    import asyncio
    from no_human.intake.grill import grill_step

    class SlowBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None):
            await asyncio.sleep(999)

    # Use a very short timeout by monkey-patching; the real timeout is 120s.
    import no_human.intake.grill as grill_mod
    orig = grill_mod.asyncio.wait_for

    async def fast_timeout(coro, *, timeout):
        return await orig(coro, timeout=0.1)

    grill_mod.asyncio.wait_for = fast_timeout
    try:
        result = await grill_step("Fix X", None, None, [], SlowBackend())
        assert isinstance(result, GrillQuestion)
        assert "too long" in result.question.lower()
    finally:
        grill_mod.asyncio.wait_for = orig


def test_summarize_tool():
    from no_human.api.app import _summarize_tool

    assert _summarize_tool("Read", {"file_path": "/a/b/c/foo.py"}) == "Read c/foo.py"
    assert _summarize_tool("Edit", {"file_path": "/a/b/bar.js"}) == "Edit b/bar.js"
    assert 'Grep' in _summarize_tool("Grep", {"query": "hello", "path": "/x"})
    assert _summarize_tool("Bash", {"command": "mvn test"}) == "Run `mvn test`"
    assert _summarize_tool("UnknownTool", {"val": "x"}) == "UnknownTool x"


def test_format_events_attaches_tool_result_preview():
    """tool_result events must not be silently dropped — the activity feed
    needs the actual output of a tool call, not just the call signature."""
    from no_human.api.app import _format_events

    events = [
        {"source": "agent", "kind": "tool_use", "tool_name": "Read",
         "tool_input": {"file_path": "/repo/Jenkinsfile"}, "ts": 1.0},
        {"source": "agent", "kind": "tool_result", "text": "stage('Build') { ... }", "ts": 1.1},
        {"source": "agent", "kind": "tool_use", "tool_name": "Bash",
         "tool_input": {"command": "wc -l Jenkinsfile"}, "ts": 2.0},
        {"source": "agent", "kind": "tool_result", "text": "812 Jenkinsfile", "ts": 2.1},
    ]
    out = _format_events(events)
    assert len(out) == 2  # tool_result events don't produce their own entries
    assert out[0]["kind"] == "tool_use"
    assert out[0]["tool_name"] == "Read"
    assert out[0]["result_preview"] == "stage('Build') { ... }"
    assert out[1]["result_preview"] == "812 Jenkinsfile"


def test_format_events_truncates_long_result_preview():
    from no_human.api.app import _format_events, _RESULT_PREVIEW_CAP

    events = [
        {"source": "agent", "kind": "tool_use", "tool_name": "Read",
         "tool_input": {"file_path": "/repo/big.py"}, "ts": 1.0},
        {"source": "agent", "kind": "tool_result", "text": "x" * 5000, "ts": 1.1},
    ]
    out = _format_events(events)
    assert len(out[0]["result_preview"]) == _RESULT_PREVIEW_CAP + 1  # + ellipsis
    assert out[0]["result_preview"].endswith("…")


def test_format_events_tool_result_with_no_pending_tool_use_is_dropped():
    from no_human.api.app import _format_events

    events = [{"source": "agent", "kind": "tool_result", "text": "orphaned", "ts": 1.0}]
    assert _format_events(events) == []


def test_format_events_surfaces_thinking_blocks():
    """Extended-thinking content used to be silently dropped — the UI needs
    it (collapsed by default) to explain *why* the agent did something."""
    from no_human.api.app import _format_events

    events = [
        {"source": "agent", "kind": "thinking", "text": "Let me check the existing stage first.", "ts": 1.0},
        {"source": "agent", "kind": "tool_use", "tool_name": "Read",
         "tool_input": {"file_path": "/repo/Jenkinsfile"}, "ts": 1.1},
    ]
    out = _format_events(events)
    assert out[0]["kind"] == "thinking"
    assert out[0]["text"] == "Let me check the existing stage first."
    assert out[1]["kind"] == "tool_use"


def test_format_events_surfaces_subagents():
    """Subagent lifecycle events must survive _format_events so the System
    view's agent tree can discover dynamically-spawned subagents on initial
    load and for finished tasks — not only via the live SSE stream."""
    from no_human.api.app import _format_events

    events = [
        {"source": "agent", "kind": "subagent_start", "text": "research auth flow",
         "task_id": "sub-1", "task_type": "no_human_researcher", "ts": 1.0},
        {"source": "agent", "kind": "subagent_progress", "text": "reading files",
         "task_id": "sub-1", "ts": 1.5},
        {"source": "agent", "kind": "subagent_done", "text": "found it",
         "task_id": "sub-1", "status": "completed", "ts": 2.0},
    ]
    out = _format_events(events)
    assert [o["kind"] for o in out] == [
        "subagent_start", "subagent_progress", "subagent_done",
    ]
    assert out[0]["task_id"] == "sub-1"
    assert out[0]["task_type"] == "no_human_researcher"
    assert out[2]["status"] == "completed"


@pytest.mark.asyncio
async def test_grill_step_on_event_passthrough():
    """When on_event is provided, grill_step forwards it to backend.run."""
    from no_human.intake.grill import grill_step

    received_kwargs = {}

    class EventCapturingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None):
            received_kwargs["on_event"] = on_event
            class _R:
                final_text = '```json\n{"type": "done", "title": "T", "description": "D", "acceptance_criteria": ["AC"]}\n```'
            return _R()

    cb = lambda event: None
    await grill_step("Fix X", None, None, [], EventCapturingBackend(), on_event=cb)
    assert received_kwargs["on_event"] is cb


@pytest.mark.asyncio
async def test_grill_step_no_on_event_by_default():
    """When on_event is not provided, backend.run is called without it."""
    from no_human.intake.grill import grill_step

    received_kwargs = {}

    class KwargsCapturingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, **kwargs):
            received_kwargs.update(kwargs)
            class _R:
                final_text = '```json\n{"type": "done", "title": "T", "description": "D", "acceptance_criteria": ["AC"]}\n```'
            return _R()

    await grill_step("Fix X", None, None, [], KwargsCapturingBackend())
    assert "on_event" not in received_kwargs


def test_api_models_round_trip():
    from no_human.api.models import GrillQuestionOut, GrillResultOut, GrillStepRequest

    req = GrillStepRequest(
        title="Fix X", description="Longer", repo_path="/tmp",
        qa_history=[{"question": "Q", "answer": "A"}],
    )
    assert req.title == "Fix X"
    assert len(req.qa_history) == 1

    q = GrillQuestionOut(question="Which?", suggestions=["A: x"], round=1)
    assert q.type == "question"

    r = GrillResultOut(title="T", description="D", acceptance_criteria=["AC"])
    assert r.type == "done"
