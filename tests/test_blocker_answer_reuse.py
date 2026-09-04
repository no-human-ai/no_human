"""Blocker answers survive attempt death (live defect 2026-08-12/13, task
b071f6c3): an operator's answer to a blocker question must be persisted on
the TASK record and injected into every later attempt that raises the same
(normalized) question — never re-asked.

Red-first: ``test_second_attempt_reuses_operator_answer_without_escalating``
fails on unfixed code (the second attempt re-parks and re-notifies) and
passes once ``_raise_blocker`` consults stored answers before routing.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.blockers import (
    Blocker,
    BlockerCategory,
    answer_record,
    find_stored_answer,
    normalize_question,
    question_hash,
    reuse_record,
)
from no_human.config import load_config
from no_human.core import plan_gate
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from tests.test_e2e_orchestrator import _config, _git, bare_repo  # noqa: F401

# `client`/`reply` reach `config.load_env_var` — see tests/conftest.py's
# `isolated_env_file` docstring: without this the suite's verdict on the
# reply-persistence test would depend on the operator's own `~/.no_human/.env`.
pytestmark = pytest.mark.usefixtures("isolated_env_file")

QUESTION = "Should halves round up or to even?"
ANSWER = "Round half to even (banker's rounding)."

AMBIGUITY_TEXT = f"""
Ran into an ambiguity.

BLOCKER_JSON_START
{{
  "category": "AMBIGUITY",
  "transient": false,
  "confidence": 0.9,
  "root_cause_hypothesis": "spec doesn't say which rounding mode to use",
  "evidence": "no test pins the behavior",
  "goal": "implement rounding",
  "question": "{QUESTION}"
}}
BLOCKER_JSON_END
"""


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.api.app import app

    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


class _RecordingNotifier(SlackNotifier):
    def __init__(self):
        super().__init__(None)
        self.sent = []

    def notify(self, kind, message):
        self.sent.append((kind, message))


def _orch(store, tmp_path, *, backend=None, notifier=None, sink=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(
        store, cfg.data, backend or _Backend(), notifier or SlackNotifier(None),
        event_sink=sink,
    )


async def _new_task(store, title="round halves"):
    t = Task.new(title, repo_path="/r")
    t.acceptance_criteria = ["keep me"]
    await store.create_task(t)
    return t


def _ambiguity_blocker(question, *, agent_authored=True):
    b = Blocker(
        category=BlockerCategory.AMBIGUITY, transient=False, confidence=0.9,
        goal="implement rounding", root_cause_hypothesis="unclear spec",
        evidence="no test pins the behavior", question=question,
    )
    b.reason_is_agent_authored = agent_authored
    return b


class _AskThenDoneBackend:
    """First call always raises the same AMBIGUITY question; every call after
    that mutates + succeeds — so a run recovers once the answer is stored."""

    def __init__(self, mutate, blocker_text=AMBIGUITY_TEXT):
        self.mutate = mutate
        self.blocker_text = blocker_text
        self.calls = 0
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return AgentResult(final_text=self.blocker_text, num_turns=1,
                               is_error=False, tokens_used=50, session_id="s",
                               stop_reason="end_turn")
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


class _AlwaysAskBackend:
    def __init__(self, blocker_text=AMBIGUITY_TEXT):
        self.blocker_text = blocker_text
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        return AgentResult(final_text=self.blocker_text, num_turns=1,
                           is_error=False, tokens_used=50, session_id="s",
                           stop_reason="end_turn")


# --------------------------------------------------------------------------- #
# Normalization / hash                                                        #
# --------------------------------------------------------------------------- #

def test_question_hash_ignores_case_whitespace_and_trailing_punctuation():
    a = question_hash("What is the answer ?")
    b = question_hash("what  is\nthe answer")
    assert a == b
    assert a != question_hash("what is a different answer")


def test_normalize_question_collapses_whitespace_and_strips_trailing_punct():
    assert normalize_question("  What Is The Answer?!  ") == "what is the answer"
    assert normalize_question("") == ""
    assert normalize_question(None) == ""


def test_question_hash_blank_is_never_a_match():
    assert question_hash("") == ""
    assert question_hash(None) == ""


def test_find_stored_answer_tolerates_bare_strings_and_hashless_dicts():
    replies = [
        "an old bare-string row",
        {"question": QUESTION, "answer": ANSWER},  # no question_hash of its own
    ]
    found = find_stored_answer(replies, QUESTION)
    assert found is not None
    assert found["answer"] == ANSWER


def test_find_stored_answer_skips_blank_answer_and_reuse_source():
    replies = [
        {"question": QUESTION, "answer": "", "question_hash": question_hash(QUESTION)},
        {"question": QUESTION, "answer": "ignored — this is a replay",
         "question_hash": question_hash(QUESTION), "source": "reuse"},
    ]
    assert find_stored_answer(replies, QUESTION) is None


# --------------------------------------------------------------------------- #
# Persisted answer fields (CLI + API reply paths)                             #
# --------------------------------------------------------------------------- #

async def test_reply_persists_answer_record_fields(client, store):
    from datetime import datetime

    t = Task.new("needs an answer", repo_path="/r")
    t.acceptance_criteria = ["x"]
    await store.create_task(t)
    t.blocker = {"question": QUESTION, "category": "AMBIGUITY", "attempt_id": "attempt-1"}
    await store.update_task(t)
    await store.set_status(t, TaskStatus.AWAITING_INPUT, validate=False)

    r = await client.post(f"/api/tasks/{t.id}/reply", json={"answer": ANSWER})
    assert r.status_code == 200, r.text

    refreshed = await store.find_task(t.id)
    reply = refreshed.context["human_replies"][-1]
    assert reply["question_hash"] == question_hash(QUESTION)
    assert reply["question"] == QUESTION
    assert reply["answer"] == ANSWER
    assert datetime.fromisoformat(reply["answered_at"])  # parses as ISO
    assert reply["source_attempt_id"] == "attempt-1"
    assert reply["source"] == "operator:api"


# --------------------------------------------------------------------------- #
# Injection instead of escalation — red-first, full multi-attempt scenario    #
# --------------------------------------------------------------------------- #

async def test_second_attempt_reuses_operator_answer_without_escalating(
    bare_repo, tmp_path, store
):
    """(1) attempt 1 raises AMBIGUITY question Q; (2) operator answers A;
    (3) that run terminates; (4) a SECOND run_task of the same task raises Q
    again on its first attempt; (5) it must NOT escalate/notify again, and
    the FOLLOWING attempt's prompt must carry A. Fails today (re-asks, parks,
    notifies a second time)."""
    cfg = _config(tmp_path)
    notifier = _RecordingNotifier()
    events: list[dict] = []

    backend1 = _AlwaysAskBackend()
    orch1 = Orchestrator(store, cfg.data, backend1, notifier, event_sink=events.append)
    t = Task.new("round halves", repo_path=str(bare_repo))
    t.acceptance_criteria = ["halves round predictably"]
    await store.create_task(t)

    outcome1 = await orch1.run_task(t)
    assert outcome1.status is TaskStatus.AWAITING_INPUT
    assert backend1.calls == 1
    assert len(notifier.sent) == 1

    t = await store.find_task(t.id)
    assert t.blocker["question"] == QUESTION
    original_attempt_id = t.blocker.get("attempt_id")
    assert original_attempt_id

    # Operator answers (the persistence shape `answer_record` produces — the
    # same one the CLI/API reply handlers now build).
    record = answer_record(
        question=QUESTION, answer=ANSWER, attempt_id=original_attempt_id,
        source="operator:test",
    )
    await store.append_context_list(t.id, "human_replies", record)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    t = await store.find_task(t.id)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def half_round(x):\n    return round(x)\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import half_round\n\n"
            "def test_half_round():\n    assert half_round(2.5) == 2\n"
        )

    backend2 = _AskThenDoneBackend(mutate)
    orch2 = Orchestrator(store, cfg.data, backend2, notifier, event_sink=events.append)

    outcome2 = await orch2.run_task(t)

    assert outcome2.status is TaskStatus.AWAITING_APPROVAL
    assert outcome2.pr_url
    # Still exactly the ONE "stuck" (blocker) notification, from the first
    # park — no second human round-trip on the SAME question. The run's own
    # "PR ready, please approve" notification is a different, legitimate
    # kind and is expected once the run recovers.
    stuck_notifications = [n for n in notifier.sent if n[0] == "stuck"]
    assert len(stuck_notifications) == 1
    # The reused answer reached the attempt AFTER the reuse.
    assert backend2.calls == 2
    assert ANSWER in backend2.prompts[1]

    reused = [e for e in events if e["kind"] == "answer_reused"]
    assert len(reused) == 1
    assert reused[0]["original_answer"] == ANSWER
    assert reused[0]["original_attempt_id"] == original_attempt_id
    assert reused[0]["original_question_hash"] == question_hash(QUESTION)
    assert reused[0]["reused_in_attempt_id"]
    assert reused[0]["reused_in_attempt_id"] != original_attempt_id


# --------------------------------------------------------------------------- #
# answer_reused event provenance (lightweight, `_raise_blocker` directly)     #
# --------------------------------------------------------------------------- #

async def test_answer_reused_event_carries_full_provenance(store, tmp_path):
    t = await _new_task(store)
    events: list[dict] = []
    orch = _orch(store, tmp_path, sink=events.append)

    outcome1 = await orch._raise_blocker(
        t, _ambiguity_blocker(QUESTION), attempt_id="attempt-A")
    assert outcome1.status is TaskStatus.AWAITING_INPUT

    record = answer_record(
        question=QUESTION, answer=ANSWER, attempt_id="attempt-A", source="operator:test")
    await store.append_context_list(t.id, "human_replies", record)
    t = await store.find_task(t.id)

    outcome2 = await orch._raise_blocker(
        t, _ambiguity_blocker(QUESTION), attempt_id="attempt-B")
    assert outcome2.status is TaskStatus.FAILED
    assert outcome2.off_ramp is False

    reused = [e for e in events if e["kind"] == "answer_reused"]
    assert len(reused) == 1
    ev = reused[0]
    assert ev["original_question_hash"] == question_hash(QUESTION)
    assert ev["reused_in_attempt_id"] == "attempt-B"
    assert ev["original_attempt_id"] == "attempt-A"
    assert ev["original_answer"] == ANSWER
    assert ev["reused_at"]


# --------------------------------------------------------------------------- #
# Type coverage: reuse applies to a non-AMBIGUITY question too                #
# --------------------------------------------------------------------------- #

async def test_reuse_applies_to_a_second_question_type(store, tmp_path, monkeypatch):
    """SCOPE_EXPLOSION is human-routed (ESCALATED) and carries a question —
    reuse must not be AMBIGUITY-only."""
    async def fake_generate(*a, **k):
        return None

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate)

    t = await _new_task(store)
    events: list[dict] = []
    orch = _orch(store, tmp_path, sink=events.append)

    q = "Split this into two PRs, or keep it as one large change?"

    def _scope_blocker(evidence):
        b = Blocker(
            category=BlockerCategory.SCOPE_EXPLOSION, confidence=0.9,
            goal="g", root_cause_hypothesis="too large", evidence=evidence,
            question=q,
        )
        b.reason_is_agent_authored = True
        return b

    outcome1 = await orch._raise_blocker(
        t, _scope_blocker("40 files"), attempt_id="attempt-A")
    assert outcome1.status is TaskStatus.ESCALATED

    record = answer_record(
        question=q, answer="Keep it as one PR.", attempt_id="attempt-A",
        source="operator:test")
    await store.append_context_list(t.id, "human_replies", record)
    t = await store.find_task(t.id)

    outcome2 = await orch._raise_blocker(
        t, _scope_blocker("41 files"), attempt_id="attempt-B")
    assert outcome2.status is TaskStatus.FAILED
    assert outcome2.off_ramp is False

    reused = [e for e in events if e["kind"] == "answer_reused"]
    assert len(reused) == 1
    assert reused[0]["original_answer"] == "Keep it as one PR."


# --------------------------------------------------------------------------- #
# Guard: harness-authored (human-only) blockers are never auto-answered       #
# --------------------------------------------------------------------------- #

async def test_plan_approval_blocker_is_never_auto_answered(store, tmp_path):
    t = await _new_task(store)
    events: list[dict] = []
    orch = _orch(store, tmp_path, sink=events.append)

    plan_text = "Do X, then Y."
    blocker = plan_gate.build_blocker(t, plan_text)
    assert blocker.reason_is_agent_authored is False  # harness literal

    # Pre-seed a stored answer to this EXACT question text.
    record = answer_record(
        question=blocker.question, answer="Approved.", attempt_id="attempt-Z",
        source="operator:test")
    await store.append_context_list(t.id, "human_replies", record)
    t = await store.find_task(t.id)

    outcome = await orch._raise_blocker(t, blocker, attempt_id="attempt-later")

    assert outcome.status is TaskStatus.AWAITING_INPUT  # parked, not reused
    assert not [e for e in events if e["kind"] == "answer_reused"]
    got = await store.get_task(t.id)
    replies = got.context.get("human_replies", [])
    assert len(replies) == 1  # no reuse entry appended
    assert not any(r.get("source") == "reuse" for r in replies if isinstance(r, dict))


# --------------------------------------------------------------------------- #
# Guard: an answer that applied an action is never silently re-applied        #
# --------------------------------------------------------------------------- #

async def test_answer_that_applied_an_action_is_not_reused(store, tmp_path):
    t = await _new_task(store)
    events: list[dict] = []
    orch = _orch(store, tmp_path, sink=events.append)

    outcome1 = await orch._raise_blocker(
        t, _ambiguity_blocker(QUESTION), attempt_id="attempt-A")
    assert outcome1.status is TaskStatus.AWAITING_INPUT

    record = answer_record(
        question=QUESTION, answer="raise the limit", attempt_id="attempt-A",
        source="operator:test")
    record["applied"] = "max_lines_changed=700"  # an option whose ACTION ran
    await store.append_context_list(t.id, "human_replies", record)
    t = await store.find_task(t.id)

    outcome2 = await orch._raise_blocker(
        t, _ambiguity_blocker(QUESTION), attempt_id="attempt-B")

    assert outcome2.status is TaskStatus.AWAITING_INPUT  # parked, not reused
    assert not [e for e in events if e["kind"] == "answer_reused"]


# --------------------------------------------------------------------------- #
# Guard: no ping-pong — reused at most once per (question, attempt)           #
# --------------------------------------------------------------------------- #

async def test_same_question_is_reused_at_most_once_per_attempt(store, tmp_path):
    t = await _new_task(store)
    events: list[dict] = []
    orch = _orch(store, tmp_path, sink=events.append)

    outcome1 = await orch._raise_blocker(
        t, _ambiguity_blocker(QUESTION), attempt_id="attempt-A")
    assert outcome1.status is TaskStatus.AWAITING_INPUT

    record = answer_record(
        question=QUESTION, answer=ANSWER, attempt_id="attempt-A", source="operator:test")
    await store.append_context_list(t.id, "human_replies", record)
    t = await store.find_task(t.id)

    outcome2 = await orch._raise_blocker(
        t, _ambiguity_blocker(QUESTION), attempt_id="attempt-X")
    assert outcome2.status is TaskStatus.FAILED
    assert outcome2.off_ramp is False
    t = await store.find_task(t.id)

    # Same question, SAME attempt_id again — must park normally, not reuse a
    # second time for the same attempt (the bounded loop, not this guard, is
    # what lets a later DIFFERENT attempt try again).
    outcome3 = await orch._raise_blocker(
        t, _ambiguity_blocker(QUESTION), attempt_id="attempt-X")
    assert outcome3.status is TaskStatus.AWAITING_INPUT
    assert outcome3.off_ramp is True

    reused = [e for e in events if e["kind"] == "answer_reused"]
    assert len(reused) == 1


# --------------------------------------------------------------------------- #
# Prompt injection                                                             #
# --------------------------------------------------------------------------- #

def test_resume_digest_lists_all_answered_questions():
    from no_human.core.prompt_blocks import build_resume_digest

    t = Task(
        id="a", source="test", title="x", status=TaskStatus.IMPLEMENTING,
        acceptance_criteria=["c"],
        context={"human_replies": [
            {"question": "Q1?", "answer": "A1.",
             "question_hash": question_hash("Q1?")},
            {"question": "Q2?", "answer": "A2.",
             "question_hash": question_hash("Q2?")},
        ]},
    )
    d = build_resume_digest(t)
    assert "ANSWERED QUESTIONS" in d
    assert "do NOT re-ask" in d
    assert "Q1?" in d and "A1." in d
    assert "Q2?" in d and "A2." in d
    # existing latest-reply paragraph unchanged
    assert "A human answered your blocking question" in d
    assert "A: A2." in d


# --------------------------------------------------------------------------- #
# Round-trip                                                                   #
# --------------------------------------------------------------------------- #

def test_blocker_roundtrips_attempt_id():
    b = Blocker(category=BlockerCategory.AMBIGUITY, question=QUESTION)
    b.attempt_id = "attempt-123"
    restored = Blocker.from_dict(b.to_dict())
    assert restored.attempt_id == "attempt-123"


def test_blocker_default_attempt_id_is_blank():
    b = Blocker(category=BlockerCategory.AMBIGUITY, question=QUESTION)
    assert b.attempt_id == ""
    restored = Blocker.from_dict(b.to_dict())
    assert restored.attempt_id == ""


def test_reuse_record_shape():
    stored = answer_record(
        question=QUESTION, answer=ANSWER, attempt_id="attempt-A", source="operator:test")
    record = reuse_record(stored, reused_in_attempt_id="attempt-B")
    assert record["question"] == QUESTION
    assert record["answer"] == ANSWER
    assert record["source"] == "reuse"
    assert record["source_attempt_id"] == "attempt-A"
    assert record["reused_in_attempt_id"] == "attempt-B"
    assert record["question_hash"] == question_hash(QUESTION)
