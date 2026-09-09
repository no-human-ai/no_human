"""A red pre-review test run must reach the coder even when the review that
follows FAILS for an unrelated reason.

Incident (task 302012e3 round 2 (a68d2235), task e068e0cf round 2 (59cf8c66)):
`_run_review` runs a test pass purely to hand the reviewer prompt evidence
(`test_output`, capped to 4,000 chars). If that pre-review run is RED and the
review that follows FAILS for a DIFFERENT reason, `_run_attempt`'s FAIL
branch returns immediately — the post-review TESTING step, previously the
ONLY place a red run became a `tests` event / `test_results` row / feedback
the coder could see, never runs. The red run surfaced only if a LATER round
happened to PASS.

Fix (`Orchestrator._run_review`):
  1. A red pre-review run now emits its own `tests` event (ok=False, bounded
     failure blocks via the existing `_red_test_detail`) and writes
     `test_results`, unconditional of the review verdict that follows.
  2. The reviewer is handed a FIXED, deterministic section listing the
     failing ids (bounded, independent of the 4,000-char `test_output`
     excerpt) via `_build_review_prompt`'s `failing_test_ids` param.
  3. After the reviewer returns, an un-demotable synthetic `ChecklistItem`
     is appended and `decision.passed` is forced False — a PASS verdict
     from the LLM reviewer cannot demote a red run the harness itself
     measured. This item flows through the SAME `_record_review_feedback`
     path any other blocking finding takes, so the next attempt's prompt
     renders it too.

This file drives the real `Orchestrator._run_attempt` (same harness idiom as
`tests/test_red_run_failure_blocks.py`'s `_run_attempt_with_result`), with a
stubbed `_run_tests_once` (red) and a stubbed `reviewer` (either a FAIL on
an unrelated finding, or a PASS) — the same stub-reviewer idiom
`tests/test_review_fail_closed.py`/`tests/test_e2e_orchestrator.py`'s
`FakeReviewer` use.
"""

from unittest.mock import patch

from no_human.core.orchestrator import Orchestrator
from no_human.core.prompt_blocks import build_resume_digest
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import _build_review_prompt
from no_human.review.selfcheck import ChecklistItem
from no_human.review.reviewer import ReviewDecision
from no_human.testing import runner
from no_human.vcs import GitRepo

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401


def _mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


def _red_result(failing_id="tests/test_calc.py::test_mul"):
    return runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=1, errors=0,
        command="pytest -q",
        output=(
            "FAILED tests/test_calc.py::test_mul - AssertionError: "
            "assert 5 == 6\n1 failed, 1 passed in 0.01s\n"
        ),
        full_output="",
        failure_blocks=[
            "FAILED tests/test_calc.py::test_mul - AssertionError: assert 5 == 6",
        ],
        failing_tests=[failing_id],
    )


class _FailsOnUnrelatedFinding:
    """Stub reviewer: always FAILs, but on a finding that has nothing to do
    with the pre-review red run — the exact "review fails for an unrelated
    reason" shape the incident hit."""

    model = "stub-reviewer"
    _on_event = None

    def __init__(self):
        self.calls: list[dict] = []

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                      before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        self.calls.append(kwargs)
        return ReviewDecision(passed=False, checklist=[
            ChecklistItem(
                "error handling", False,
                "calc.py:4 mul() does not validate its inputs",
                severity="high",
            ),
        ])


class _PassesEverything:
    """Stub reviewer: always PASSes — used to prove a red pre-review run
    cannot be demoted by an LLM verdict that never saw it as a problem."""

    model = "stub-reviewer"
    _on_event = None

    def __init__(self):
        self.calls: list[dict] = []

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                      before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        self.calls.append(kwargs)
        return ReviewDecision(passed=True, checklist=[
            ChecklistItem("looks good", True, "clean, minimal diff"),
        ])


async def _run_attempt_with_result_and_reviewer(
    store, tmp_path, bare_repo, tr, reviewer, *, mutate=_mutate,
):
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    task = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    async def fake_run_tests_once(repo, cmd, cwd=None):
        return tr, False

    with patch.object(orch, "_run_tests_once", fake_run_tests_once):
        outcome = await orch._run_attempt(task, repo, 1, "main")

    attempts = await store.list_attempts(task.id)
    return outcome, attempts, events, task, orch


async def test_pre_review_red_run_reaches_coder_when_review_fails_unrelated(
    bare_repo, tmp_path, store,
):
    tr = _red_result()
    reviewer = _FailsOnUnrelatedFinding()

    outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
        store, tmp_path, bare_repo, tr, reviewer)

    # (1) The tests event fired, ok=False, with the bounded failure blocks —
    # BEFORE the review verdict event, and independent of the FAIL below
    # being for an unrelated reason.
    kinds = [e["kind"] for e in events]
    assert "tests" in kinds, events
    tests_idx = kinds.index("tests")
    test_event = events[tests_idx]
    assert test_event["ok"] is False, test_event
    assert "test_mul" in test_event["text"], test_event
    assert "pre-review" in test_event["text"], test_event
    if "review" in kinds:
        review_idx = kinds.index("review")
        assert tests_idx < review_idx, events

    persisted = attempts[-1]["test_results"]
    import json
    persisted = json.loads(persisted) if isinstance(persisted, str) else (persisted or {})
    assert persisted.get("failing_tests") == ["tests/test_calc.py::test_mul"], persisted
    assert persisted.get("ok") is False, persisted

    # (2) The reviewer received the fixed, bounded failing-ids kwargs —
    # independent of the 4,000-char test_output excerpt.
    assert reviewer.calls, "reviewer never invoked"
    assert reviewer.calls[0]["failing_test_ids"] == ["tests/test_calc.py::test_mul"]
    assert reviewer.calls[0]["failing_test_ids_dropped"] == 0

    # The review still failed (for the unrelated reason) — outcome FAILED.
    assert outcome.status is TaskStatus.FAILED, outcome.detail

    # (3) The next round's review feedback rows include the failing ids
    # ALONGSIDE the reviewer's own (unrelated) finding.
    fb = (task.context or {}).get("review_feedback") or []
    assert any("error handling" == row.get("label") for row in fb), fb
    assert any(
        "test_mul" in (row.get("evidence") or "") for row in fb
    ), fb

    # ...and the coder's next-round prompt actually renders them.
    digest = build_resume_digest(task)
    assert "test_mul" in digest, digest
    assert "pre-review test run" in digest, digest
    assert "error handling" in digest, digest


async def test_pre_review_red_run_still_fails_round_when_reviewer_passes(
    bare_repo, tmp_path, store,
):
    tr = _red_result()
    reviewer = _PassesEverything()

    outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
        store, tmp_path, bare_repo, tr, reviewer)

    kinds = [e["kind"] for e in events]
    assert "tests" in kinds, events
    test_event = events[kinds.index("tests")]
    assert test_event["ok"] is False, test_event
    assert "test_mul" in test_event["text"], test_event

    assert reviewer.calls, "reviewer never invoked"

    # A PASS verdict from the LLM reviewer over a red run must NOT pass the
    # round — the synthetic un-demotable item forces the round to fail.
    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert "pre-review test run" in outcome.detail, outcome.detail

    fb = (task.context or {}).get("review_feedback") or []
    assert any(
        row.get("label") == "pre-review test run" and "test_mul" in (row.get("evidence") or "")
        for row in fb
    ), fb

    digest = build_resume_digest(task)
    assert "test_mul" in digest, digest
    assert "pre-review test run" in digest, digest


async def test_build_review_prompt_carries_fixed_failing_ids_section(bare_repo, tmp_path):
    """Direct unit test on `_build_review_prompt` (reviewer.py): the failing
    ids section is fixed text, present verbatim, and independent of the
    (separately-capped) `test_output` excerpt — a huge, unrelated test_output
    blob must not crowd out or truncate the ids section."""
    task = Task.new("some task", repo_path=str(bare_repo))
    task.acceptance_criteria = ["does the thing"]

    huge_unrelated_output = "some noisy passing test output line\n" * 500
    assert len(huge_unrelated_output) > 4000

    prompt = _build_review_prompt(
        task,
        diff="--- a/calc.py\n+++ b/calc.py\n@@ ...\n",
        test_output=huge_unrelated_output,
        held_out_output="",
        failing_test_ids=["tests/test_calc.py::test_mul", "tests/test_calc.py::test_div"],
        failing_test_ids_dropped=3,
    )

    assert (
        "Failing tests in this tree (from the harness's own run, not an opinion):"
        in prompt
    )
    assert "tests/test_calc.py::test_mul" in prompt
    assert "tests/test_calc.py::test_div" in prompt
    assert "(+3 more, not shown)" in prompt
    assert "PASS verdict over a red run will be rejected" in prompt

    # No failing ids → no fixed section at all (byte-identical to before for
    # every call site that never had a red pre-review run).
    prompt_clean = _build_review_prompt(
        task,
        diff="--- a/calc.py\n+++ b/calc.py\n@@ ...\n",
        test_output="all green",
        held_out_output="",
    )
    assert "Failing tests in this tree" not in prompt_clean
