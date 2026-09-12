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
  3. On a round that already FAILS for another reason (a genuine reviewer
     finding, a genuinely-failed verifier, or a reviewer crash), a synthetic
     `ChecklistItem` naming the red run's failing ids rides along into that
     round's checklist, so `_record_review_feedback` — which only fires on a
     FAIL — surfaces them to the next attempt's prompt too.

Send-back on an earlier version of this fix (03267ead / PR #198, MAJOR-1):
that version additionally forced `decision.passed = False` whenever a
`_pre_review_red_is_blocking` classifier — a partial reimplementation of
TESTING's own ownership / build-prerequisite / base-tree-diff attribution —
said the red run was "blocking". That classifier never called
`_flaky_on_rerun`, the tiebreaker TESTING's real plain-red path uses before
billing a non-owned failure, so forcing the verdict off it made the
tiebreaker UNREACHABLE for every red pre-review run: a genuinely flaky,
non-owned, base-green test billed the coder instead of being excused, and an
`env_setup` project (where the base-tree diff check fail-closes to `None`,
and `None != []` is True) force-failed EVERY red pre-review run with no
re-run evidence at all.

The fix here drops the forced verdict entirely (the "cheaper and fully
consistent" option from that send-back): a red pre-review run is VISIBILITY
ONLY at review time. The post-review TESTING step — unchanged, reached
normally whenever the review verdict is not otherwise a FAIL — remains the
SOLE place a red run is classified and billed or excused, via the SAME
helpers (ownership, prerequisite signature, base-tree diff, and the flaky
tiebreaker) it always used. A PASS verdict over a red run therefore still
cannot silently succeed: TESTING fails that round "at tests", not at review.

This file drives the real `Orchestrator._run_attempt` (same harness idiom as
`tests/test_red_run_failure_blocks.py`'s `_run_attempt_with_result`), with a
stubbed `_run_tests_once` (red) and a stubbed `reviewer` (either a FAIL on
an unrelated finding, or a PASS) — the same stub-reviewer idiom
`tests/test_review_fail_closed.py`/`tests/test_e2e_orchestrator.py`'s
`FakeReviewer` use. The flaky-tiebreaker / env_setup tests below patch the
three TESTING-only attribution helpers directly (`_owned_failing_tests`,
`_newly_failing_vs_base`, `_flaky_on_rerun`) rather than driving real
base-tree git operations, so they stay fast and deterministic while still
exercising the exact code paths TESTING runs.
"""

import json
from unittest.mock import AsyncMock, patch

from no_human.core import orchestrator as orch_mod
from no_human.core.orchestrator import (
    Orchestrator,
    _bound_failing_test_ids,
    _FAILING_TEST_ID_CAP,
    _PRE_REVIEW_RED_LABEL,
)
from no_human.learning.queue import _INFRA_FINDING_MARKERS, LearningQueue
from no_human.learning.failures import load_failure_records
from no_human.core.prompt_blocks import build_resume_digest
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import _build_review_prompt
from no_human.review.selfcheck import ChecklistItem
from no_human.review.reviewer import ReviewDecision
from no_human.testing import runner
from no_human.vcs import GitRepo

from .test_e2e_orchestrator import (  # noqa: F401
    FakeBackend,
    SequencedFakeReviewer,
    _config,
    bare_repo,
)
from .test_orchestrator_stagnation import _make_mutate_correct_add_mul


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
    reaches TESTING (the real deterministic gate) rather than being demoted
    or force-billed by the LLM verdict alone."""

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
    store, tmp_path, bare_repo, tr, reviewer, *, mutate=_mutate, configure_task=None,
):
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    task = Task.new("desktop npm test", repo_path=str(bare_repo))
    if configure_task is not None:
        configure_task(task)
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


async def test_pre_review_red_run_still_fails_round_at_testing_when_reviewer_passes(
    bare_repo, tmp_path, store,
):
    """A PASS verdict from the LLM reviewer over a red pre-review run must
    still fail the round — but via the EXISTING post-review TESTING gate
    (unchanged), never by forcing the review verdict itself.

    An earlier version of this fix forced `decision.passed = False` at
    review time off a partial reclassification of the SAME red run —
    which (per the send-back on 03267ead, MAJOR-1) made `_run_attempt`
    return before TESTING ever ran, starving TESTING's flaky tiebreaker and
    `env_setup` re-run for every red pre-review run (see the two tests
    below). This test uses an id TESTING's ownership check bills
    deterministically (this attempt's own diff added the failing test), so
    it stays a real regression guard for "PASS-over-red still fails the
    round" independent of the flaky/pre-existing machinery those tests
    cover.
    """
    tr = _red_result()
    reviewer = _PassesEverything()
    owned_id = "tests/test_calc.py::test_mul"

    with patch.object(Orchestrator, "_owned_failing_tests",
                       AsyncMock(return_value=[owned_id])):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    tests_events = [e for e in events if e["kind"] == "tests"]
    # The pre-review visibility emit AND TESTING's own emit both fired — a
    # red run discovered before the verdict is never silently swallowed just
    # because the round goes on to be billed somewhere else.
    assert len(tests_events) >= 2, events
    pre_review = [e for e in tests_events if "pre-review" in e["text"]]
    assert pre_review and pre_review[0]["ok"] is False, tests_events

    assert reviewer.calls, "reviewer never invoked"

    # The round still fails — TESTING (reached normally, since the review
    # verdict was never forced) billed the SAME red, owned test.
    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert "test_mul" in outcome.detail, outcome.detail
    assert "own test" in outcome.detail, outcome.detail


async def test_flaky_non_owned_red_run_reaches_awaiting_approval_when_review_passes(
    bare_repo, tmp_path, store,
):
    """Fails-before test (send-back on 03267ead, MAJOR-1): a non-owned red
    id that is green on the base tree (so TESTING's base-diff check calls it
    "newly failing", the ONLY way it can reach the tiebreaker at all) and
    green again on TESTING's own identical bounded re-run must be excused as
    flaky and reach AWAITING_APPROVAL — never billed to the coder.

    The previous (forced-verdict) implementation never reached this
    tiebreaker for a red pre-review run at all: it force-failed the round at
    review time off its own partial classifier, which never called
    `_flaky_on_rerun`. Patches the three TESTING-only attribution helpers
    directly (real base-tree git plumbing is exercised elsewhere) so this
    stays a fast, deterministic regression guard for "the tiebreaker is
    reachable", not a retest of the helpers themselves.
    """
    tr = _red_result()
    reviewer = _PassesEverything()
    flaky_id = "tests/test_calc.py::test_mul"

    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=[flaky_id])),
        patch.object(Orchestrator, "_flaky_on_rerun",
                     AsyncMock(return_value=[flaky_id])) as flaky_mock,
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    flaky_mock.assert_awaited()

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    flaky_events = [e for e in events if e.get("flaky_excused")]
    assert flaky_events, events
    assert flaky_events[-1]["flaky_excused"] == [flaky_id], flaky_events

    persisted = [a for a in attempts if a.get("test_results")]
    last = persisted[-1]["test_results"]
    import json
    last = json.loads(last) if isinstance(last, str) else (last or {})
    assert last.get("flaky_excused") == [flaky_id], last


async def test_flaky_non_owned_red_run_not_blamed_when_review_fails_unrelated(
    bare_repo, tmp_path, store,
):
    """Same flaky/non-owned red run as above, but the reviewer ALSO fails
    for a genuinely unrelated reason. `_run_attempt`'s FAIL branch still
    returns before TESTING runs — that part is correct behaviour (a real,
    separate review finding must fail the round on its own) and is NOT what
    MAJOR-1 was about. What matters here is that the flaky red run is not
    what causes or amplifies the failure: TESTING's tiebreaker is never even
    consulted (nothing to excuse — nothing was billed off it either), and
    the round fails for the reviewer's real finding alone.

    `_newly_failing_vs_base` IS awaited now — exactly once — because the
    NEW-vs-pre-existing split (this task) is computed pre-review, before the
    reviewer runs at all, via `Orchestrator._round_failure_attribution`, and
    handed to the reviewer as part of its prompt. That single pre-review call
    is also the one TESTING would have reused had it been reached; here it
    is not (the round fails at review), so the base-tree check still ran
    exactly once for the round, never twice.
    """
    tr = _red_result()
    reviewer = _FailsOnUnrelatedFinding()
    flaky_id = "tests/test_calc.py::test_mul"

    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=[flaky_id])) as newly_failing_mock,
        patch.object(Orchestrator, "_flaky_on_rerun",
                     AsyncMock(return_value=[flaky_id])) as flaky_mock,
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    # The pre-review attribution block computes the split once, before the
    # reviewer ever runs — so the base-tree check IS awaited, exactly once.
    # TESTING itself never runs (the review FAIL branch returned first), so
    # the flaky tiebreaker — TESTING-only — is never consulted.
    newly_failing_mock.assert_awaited_once()
    flaky_mock.assert_not_awaited()

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    # The failure is attributable to the reviewer's real finding, not the
    # (excusable, per the test above) red run being treated as blocking.
    assert "error handling" in outcome.detail, outcome.detail
    # The reviewer's own finding leads the detail; the harness row follows
    # it (criterion 3: the ids DO reach the coder on a review FAIL — the
    # excuse is TESTING's to grant on the next round, exactly as on main).
    assert outcome.detail.index("error handling") < outcome.detail.index(
        _PRE_REVIEW_RED_LABEL), outcome.detail
    fb = (task.context or {}).get("review_feedback") or []
    row = [r for r in fb if r.get("label") == _PRE_REVIEW_RED_LABEL]
    assert row and flaky_id in (row[0].get("evidence") or ""), fb


async def test_env_setup_red_pre_review_run_reaches_the_rerun(
    bare_repo, tmp_path, store,
):
    """Fails-before test (send-back on 03267ead, MAJOR-1): on an `env_setup`
    project, TESTING's base-diff check (`_newly_failing_vs_base`) fail-closes
    to `None` (inconclusive, never re-run against a base tree that itself
    needs environment setup this harness cannot reproduce) — the caller
    still routes a `None` verdict into `_failed_tests_outcome`, which reaches
    the flaky tiebreaker exactly like an empty-list "newly failing" result.

    The previous implementation's pre-review classifier mapped `None` to
    "blocking" (`newly_failing != []` is True when `newly_failing is None`)
    and force-failed the round on the spot — no re-run, no tiebreaker, for
    EVERY red pre-review run on an `env_setup` task. This asserts the
    tiebreaker is reached instead.
    """
    tr = _red_result()
    reviewer = _PassesEverything()

    def _mark_env_setup(task):
        # `task.config["env_setup"]` is a list of shell commands `_run_attempt`
        # actually executes before the agent runs (orchestrator.py ~5470) —
        # NOT a boolean flag. `_newly_failing_vs_base`'s `env_dependent = bool(
        # (task.config or {}).get("env_setup"))` only cares that it's
        # non-empty, so a harmless real command both makes the project
        # genuinely "env_setup" (exercising the real `_run_attempt` codepath
        # this test drives) and keeps the attempt itself fast and green.
        task.config = {**(task.config or {}), "env_setup": ["true"]}

    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=None)),
        patch.object(Orchestrator, "_flaky_on_rerun",
                     AsyncMock(return_value=None)) as flaky_mock,
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer, configure_task=_mark_env_setup)

    # The re-run was reached — not skipped by a force-fail at review time.
    flaky_mock.assert_awaited()
    # Not excused this time (the mock returns None) — the round is billed at
    # TESTING, same as any other unexcused red run, proving this is a real
    # gate and not a rubber stamp.
    assert outcome.status is TaskStatus.FAILED, outcome.detail


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
    assert "a PASS here cannot excuse it" in prompt

    # The section must say the harness has NOT yet attributed these ids to
    # the diff (round-1 send-back on 429b471f/03267ead rejected classifying
    # here — that starved the flaky tiebreaker), that a base-tree-red test
    # is not this change's defect, that the post-review TESTING step is
    # what decides, and that "critical severity" is conditioned on the diff
    # plausibly explaining the failure rather than unconditional. Incidents:
    # 86b5bf3d rounds 3 and 4 were both failed by the reviewer on the
    # `tests/test_guard.py` redness — those tests inherited the runner's
    # environment instead of building the one they meant to exercise, fixed
    # in 8b85472d — despite neither coder touching them, and with both
    # reviewers stating the diff had not caused the failures.
    assert "has NOT yet attributed" in prompt
    assert "base tree" in prompt
    assert "post-review testing step" in prompt
    assert "only when the diff plausibly explains it" in prompt
    assert "observation, not a blocking finding" in prompt

    # No failing ids → no fixed section at all (byte-identical to before for
    # every call site that never had a red pre-review run).
    prompt_clean = _build_review_prompt(
        task,
        diff="--- a/calc.py\n+++ b/calc.py\n@@ ...\n",
        test_output="all green",
        held_out_output="",
    )
    assert "Failing tests in this tree" not in prompt_clean


def test_bound_failing_test_ids_caps_and_reports_dropped_count():
    """Pins `_FAILING_TEST_ID_CAP` (200): the reviewer's fixed prompt section
    and the review-feedback row must never be handed an unbounded id list.
    Regression guard for a mutation that removes the cap entirely — no
    existing test caught that before this one."""
    ids = [f"tests/test_x.py::test_{i}" for i in range(_FAILING_TEST_ID_CAP + 7)]

    kept, dropped = _bound_failing_test_ids(ids)

    assert len(kept) == _FAILING_TEST_ID_CAP
    assert dropped == 7
    assert kept == ids[:_FAILING_TEST_ID_CAP]

    small = ids[:5]
    kept2, dropped2 = _bound_failing_test_ids(small)
    assert kept2 == small
    assert dropped2 == 0


async def test_pre_review_ids_are_bounded_at_the_call_site(
    bare_repo, tmp_path, store,
):
    """Wiring pin (independent review of 429b471f, MINOR-4): the bound must
    be applied where `_run_review` hands the ids on, not only inside the
    helper — a run with CAP+1 failing ids reaches the reviewer kwargs and the
    feedback row as CAP ids plus a dropped count of 1.
    """
    ids = [f"tests/test_many.py::test_{i}" for i in range(_FAILING_TEST_ID_CAP + 1)]
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=0, failed=len(ids), errors=0,
        command="pytest -q",
        output="".join(f"FAILED {i} - AssertionError\n" for i in ids),
        full_output="", failure_blocks=[], failing_tests=ids,
    )
    reviewer = _FailsOnUnrelatedFinding()

    outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
        store, tmp_path, bare_repo, tr, reviewer)

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert reviewer.calls[0]["failing_test_ids"] == ids[:_FAILING_TEST_ID_CAP]
    assert reviewer.calls[0]["failing_test_ids_dropped"] == 1
    fb = (task.context or {}).get("review_feedback") or []
    row = [r for r in fb if r.get("label") == _PRE_REVIEW_RED_LABEL]
    assert row, fb
    evidence = row[0].get("evidence") or ""
    assert ids[_FAILING_TEST_ID_CAP - 1] in evidence, evidence[-200:]
    assert ids[_FAILING_TEST_ID_CAP] not in evidence, evidence[-200:]
    assert "(+1 more)" in evidence, evidence[-200:]


class _PassesButSnapshotsAttribution(_PassesEverything):
    """Wraps `_PassesEverything` to snapshot the await_count of the three
    attribution helpers at the moment `review()` runs. Doctrine (this task):
    `_owned_failing_tests` / `_newly_failing_vs_base` now run PRE-review,
    exactly once, via `Orchestrator._round_failure_attribution`, so the
    reviewer can be handed the NEW-vs-pre-existing split as prose — the
    snapshot proves review() sees them ALREADY awaited (once), not that the
    review path itself never touches attribution. `_flaky_on_rerun` stays
    TESTING-only — its tiebreaker starved twice before (429b471f/03267ead)
    when a partial classifier ran the other two AND forced the verdict at
    review time; this pins that the fix still never calls the tiebreaker,
    and never forces `decision.passed`, before TESTING runs."""

    def __init__(self, owned_mock, newly_mock, flaky_mock):
        super().__init__()
        self._owned_mock = owned_mock
        self._newly_mock = newly_mock
        self._flaky_mock = flaky_mock
        self.snapshots: dict[str, int] = {}

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                      before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        self.snapshots = {
            "owned": self._owned_mock.await_count,
            "newly": self._newly_mock.await_count,
            "flaky": self._flaky_mock.await_count,
        }
        return await super().review(
            task, repo_path=repo_path, test_output=test_output,
            held_out_output=held_out_output, before_ref=before_ref,
            after_ref=after_ref, **kwargs)


async def test_the_review_path_does_not_attribute_the_red_run(
    bare_repo, tmp_path, store,
):
    """AC2 pin, doctrine updated for this task: the split the reviewer is
    handed (NEW vs pre-existing vs unknown) is computed exactly ONCE per
    round, PRE-review, and shared verbatim with TESTING's own billing —
    never recomputed, never disagreeing. So by the time `review()` runs,
    `_owned_failing_tests` and `_newly_failing_vs_base` have ALREADY been
    awaited once each (the pre-review attribution block); `_flaky_on_rerun`
    — TESTING's tiebreaker, never reached before review — has not.

    What must NOT come back (round-1 send-back on 429b471f/03267ead): the
    review path forcing `decision.passed` off this classification, or
    calling the flaky tiebreaker before TESTING. This test pins both:
    the verdict is not forced (the round still reaches AWAITING_APPROVAL
    off the stub's own PASS), and `flaky_mock` is 0 at review time.

    Uses a non-owned, base-green id so TESTING's own plain-red path goes on
    to call `_flaky_on_rerun` for real after the (PASSing) review returns —
    the mock being awaited by the end of the round is the positive control
    that the patch target is live. Because the split is shared, NOT
    recomputed, `owned_mock` / `newly_mock` end the round at award_count 1,
    not 2 — the ablation for "computed once" this task requires.
    """
    tr = _red_result()
    flaky_id = "tests/test_calc.py::test_mul"

    owned_mock = AsyncMock(return_value=[])
    newly_mock = AsyncMock(return_value=[flaky_id])
    flaky_mock = AsyncMock(return_value=[flaky_id])
    reviewer = _PassesButSnapshotsAttribution(owned_mock, newly_mock, flaky_mock)

    with (
        patch.object(Orchestrator, "_owned_failing_tests", owned_mock),
        patch.object(Orchestrator, "_newly_failing_vs_base", newly_mock),
        patch.object(Orchestrator, "_flaky_on_rerun", flaky_mock),
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    # At the moment review() ran: the pre-review attribution block had
    # already awaited owned/newly once each; the TESTING-only tiebreaker
    # had not run yet.
    assert reviewer.snapshots == {"owned": 1, "newly": 1, "flaky": 0}, reviewer.snapshots

    # The verdict was not forced off the split — the round reached
    # AWAITING_APPROVAL off the stub reviewer's own PASS.
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    # Positive control: TESTING went on to call the tiebreaker for real on
    # this same round (reached because the review PASSed) — the patch
    # target is live, not dead code the review path never reaches either
    # way.
    flaky_mock.assert_awaited()

    # Computed ONCE and shared: TESTING reuses the SAME pre-review result
    # instead of re-invoking the base-tree check, so by end of round the
    # award_count is still 1, not 2.
    assert owned_mock.await_count == 1, owned_mock.await_count
    assert newly_mock.await_count == 1, newly_mock.await_count


async def test_persistent_red_pre_review_run_does_not_trip_d6_stagnation(
    bare_repo, tmp_path, store,
):
    """Fails-before test (independent review of 429b471f, MAJOR-1): the
    harness row `_run_review` attaches to a FAILing decision is persisted
    in `review_checklist` like any reviewer finding, so a pre-existing red
    id that is red on EVERY round recurs across rounds by construction.
    With one real finding per round the D6 stagnation detector's majority
    rule (`_recurring_finding`) saw `{a, pre-review test run}` vs
    `{b, pre-review test run}` as 1/2 recurring and escalated after two
    rounds — main opens the PR on round 3. Same shape as
    `test_orchestrator_stagnation.py::test_d6_still_ignores_wholly_different_findings_at_the_same_rate`
    plus a red pre-review run on a non-owned, base-red id every round
    (TESTING excuses it as pre-existing: `_newly_failing_vs_base` -> []).
    The row must be present on both FAIL rounds (criterion 3) and ignored
    by `_failing_labels`.
    """
    other = "tests/test_other.py::test_flaky"
    decisions = [
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("commitSha undefined reference", False, "Jenkinsfile:883",
                          severity="high"),
        ]),
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("Image reuse broken across Jenkins agents", False,
                          "Jenkinsfile:653", severity="high"),
        ]),
        ReviewDecision(passed=True, checklist=[
            ChecklistItem("all findings addressed", True, "verified"),
        ]),
    ]
    cfg = _config(tmp_path)
    reviewer = SequencedFakeReviewer(decisions)
    orch = Orchestrator(store, cfg.data, FakeBackend(_make_mutate_correct_add_mul()),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("fix things", repo_path=str(bare_repo))
    t.acceptance_criteria = ["things are fixed"]
    await store.create_task(t)

    with (
        patch.object(orch_mod.runner, "run_tests",
                     lambda *a, **k: _red_result(other)),
        patch.object(Orchestrator, "_newly_failing_vs_base", AsyncMock(return_value=[])),
        patch.object(Orchestrator, "_flaky_on_rerun", AsyncMock(return_value=None)),
    ):
        outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert len(reviewer.calls) == 3
    assert not (t.context or {}).get("stagnation_detected"), t.context

    import json
    rows = await store.list_attempts(t.id)
    assert len(rows) == 3
    for r in rows[:2]:
        rc = r.get("review_checklist")
        rc = json.loads(rc) if isinstance(rc, str) else (rc or {})
        labels = [i.get("label") for i in rc.get("items", []) if not i.get("passed")]
        assert _PRE_REVIEW_RED_LABEL in labels, labels


def _green_result():
    return runner.TestRunResult(
        ran=True, ok=True, passed=2, failed=0, errors=0, command="pytest -q",
        output="2 passed in 0.01s\n", full_output="", failure_blocks=[],
        failing_tests=[],
    )


async def test_one_round_red_pre_review_run_does_not_equalise_d6_pass_rates(
    bare_repo, tmp_path, store,
):
    """Fails-before test (independent review of the 429b471f landing tree,
    MAJOR-1): D6 needs equal pass rates AND a recurring label. Excluding the
    harness row from the label set alone left it in the pass-rate
    denominator: a red pre-review run on round 1 only (the flaky case) turned
    a reviewer's 2/3 then 1/2 into 2/4 then 1/2 — equal — and with a
    genuinely recurring finding the task escalated after two rounds where
    main opens the PR on round 3. `_reviewer_items` drops the row before
    both halves.
    """
    other = "tests/test_other.py::test_flaky"
    decisions = [
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("tests cover the change", True, "ok"),
            ChecklistItem("docs updated", True, "ok"),
            ChecklistItem("commitSha undefined reference", False, "Jenkinsfile:883",
                          severity="high"),
        ]),
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("tests cover the change", True, "ok"),
            ChecklistItem("commitSha undefined reference", False, "Jenkinsfile:883",
                          severity="high"),
        ]),
        ReviewDecision(passed=True, checklist=[
            ChecklistItem("all findings addressed", True, "verified"),
        ]),
    ]
    cfg = _config(tmp_path)
    reviewer = SequencedFakeReviewer(decisions)
    orch = Orchestrator(store, cfg.data, FakeBackend(_make_mutate_correct_add_mul()),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("fix things", repo_path=str(bare_repo))
    t.acceptance_criteria = ["things are fixed"]
    await store.create_task(t)
    calls = {"n": 0}

    def run_tests(*a, **k):
        calls["n"] += 1
        return _red_result(other) if calls["n"] == 1 else _green_result()

    with (
        patch.object(orch_mod.runner, "run_tests", run_tests),
        patch.object(Orchestrator, "_newly_failing_vs_base", AsyncMock(return_value=[])),
        patch.object(Orchestrator, "_flaky_on_rerun", AsyncMock(return_value=None)),
    ):
        outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert len(reviewer.calls) == 3
    assert not (t.context or {}).get("stagnation_detected"), t.context


# ── single-write invariant (task 30a97d8a): the excused pre-existing/flaky
# path must write ONE artifact and emit exactly TWO `tests` events — the
# pre-review block's and TESTING's own — never a redundant second render of
# the SAME cached `test_result` at review time. ─────────────────────────── #


async def test_excused_red_run_writes_one_artifact_and_emits_two_tests_events(
    bare_repo, tmp_path, store,
):
    """AC-1: on the excused pre-existing/flaky path, `_red_test_detail` (the
    ONE seam that renders failure blocks AND writes the `tests-attempt-N.log`
    artifact) must be called exactly ONCE for the round — the pre-review
    block renders the cached red `test_result` first, and TESTING's plain
    branch reuses that render (by IDENTITY: its own `test_result` IS the
    same object) instead of calling `_red_test_detail` a second time. Two
    `tests` events still fire — the pre-review visibility emit and TESTING's
    own excuse emit — pre-review first, but only ONE artifact write backs
    them both.
    """
    tr = _red_result()
    reviewer = _PassesEverything()
    flaky_id = "tests/test_calc.py::test_mul"

    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=[flaky_id])),
        patch.object(Orchestrator, "_flaky_on_rerun",
                     AsyncMock(return_value=[flaky_id])),
        patch.object(Orchestrator, "_red_test_detail", autospec=True,
                     side_effect=Orchestrator._red_test_detail) as red_detail_mock,
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail

    # Exactly one render/write, no matter that both the pre-review block and
    # TESTING's plain branch process this same red `test_result`.
    assert red_detail_mock.call_count == 1, red_detail_mock.call_args_list

    tests_events = [e for e in events if e["kind"] == "tests"]
    assert len(tests_events) == 2, events
    assert "pre-review" in tests_events[0]["text"], tests_events
    assert tests_events[0]["ok"] is False, tests_events
    # TESTING's own emit — the flaky-excuse note, never a second red render.
    assert tests_events[1].get("flaky_excused") == [flaky_id], tests_events


async def test_the_test_runner_is_called_once_per_round_on_the_excused_path(
    bare_repo, tmp_path, store, monkeypatch,
):
    """AC-3: the excused path's cache reuse must stay a REUSE. `_run_tests_once`
    ITSELF is called twice by design — once from `_run_review`'s pre-review
    block, once from TESTING (B3's docstring on `_run_tests_once` says so
    outright) — but its own `_test_cache` (keyed on repo path/head sha/cmd/
    cwd) must make the SECOND call a cache hit, so the real runner
    (`runner.run_tests`, the actual subprocess-spawning call) fires exactly
    ONCE per round even when the pre-review run is RED and excused at
    TESTING. Same idiom as `tests/test_e2e_orchestrator.py::
    test_the_suite_runs_once_per_attempt_not_twice`'s `_count_test_runs`,
    replicated locally for the excused-red path that test doesn't cover.
    No base recheck or extra run is introduced at review time (the 03267ead
    regression this whole fix avoids repeating)."""
    tr = _red_result()
    reviewer = _PassesEverything()
    flaky_id = "tests/test_calc.py::test_mul"
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    task = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    calls: list[str] = []

    def counting_run_tests(repo_path, test_cmd=None, *a, **kw):
        calls.append(str(test_cmd))
        return tr

    monkeypatch.setattr("no_human.core.orchestrator.runner.run_tests", counting_run_tests)

    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=[flaky_id])),
        patch.object(Orchestrator, "_flaky_on_rerun",
                     AsyncMock(return_value=[flaky_id])),
    ):
        outcome = await orch._run_attempt(task, repo, 1, "main")

    assert len(calls) == 1, calls
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail


async def test_pre_review_row_is_unclassified_until_testing_overwrites_it(
    bare_repo, tmp_path, store,
):
    """AC-2: the pre-review block's `test_results` row carries an explicit
    `classified: False` marker. When the review FAILs for an unrelated
    reason and TESTING never runs, that marker persists as False — the
    board's attempt view must be able to tell "the gate never graded this"
    apart from a genuine billed failure (pinned separately by
    `web/src/slideOverSummary.test.mjs`). When TESTING DOES run (the excused
    path below), it overwrites the marker to True.
    """
    # (a) review FAILs for an unrelated reason — TESTING never runs — the
    # marker persists as False.
    tr = _red_result()
    reviewer = _FailsOnUnrelatedFinding()
    outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
        store, tmp_path, bare_repo, tr, reviewer)
    assert outcome.status is TaskStatus.FAILED, outcome.detail
    persisted = attempts[-1]["test_results"]
    persisted = json.loads(persisted) if isinstance(persisted, str) else (persisted or {})
    assert persisted.get("classified") is False, persisted

    # (b) the excused path — TESTING DOES run — overwrites the marker to True.
    tr2 = _red_result()
    reviewer2 = _PassesEverything()
    flaky_id = "tests/test_calc.py::test_mul"
    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=[flaky_id])),
        patch.object(Orchestrator, "_flaky_on_rerun",
                     AsyncMock(return_value=[flaky_id])),
    ):
        outcome2, attempts2, events2, task2, orch2 = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr2, reviewer2)
    assert outcome2.status is TaskStatus.AWAITING_APPROVAL, outcome2.detail
    final_rows = [a for a in attempts2 if a.get("test_results")]
    final = final_rows[-1]["test_results"]
    final = json.loads(final) if isinstance(final, str) else (final or {})
    assert final.get("classified") is True, final


def test_the_learning_marker_matches_the_orchestrator_label():
    """AC-4: `learning/queue.py`'s `_INFRA_FINDING_MARKERS` excludes the
    harness's own pre-review red-run row the same way D6's `_reviewer_items`
    already does, via a hardcoded literal (avoiding a `learning` <->
    `core.orchestrator` import cycle) — this pins that literal against the
    real `_PRE_REVIEW_RED_LABEL` constant so the two can never drift apart
    silently."""
    assert _PRE_REVIEW_RED_LABEL.lower() in _INFRA_FINDING_MARKERS


async def test_the_harness_row_is_not_learned_as_a_review_finding(
    bare_repo, tmp_path, store,
):
    """AC-4, proposal half: `LearningQueue._build_from_review` filters every
    finding through `_is_infra_finding` before EITHER the dedupe key or the
    proposal body is built (same idiom as `tests/test_review_learning.py`'s
    `test_reviewer_crash_sentinel_is_not_learned`, replicated locally rather
    than by editing that file — see PLAN.md). The harness's own pre-review
    red-run row must never surface in the learned content or evidence, and
    must never shift the dedupe key a bare occurrence of the real finding
    (with no harness row riding along) would produce — riding alongside a
    real finding must not even change WHICH queue entry a recurrence
    collapses onto.
    """
    real_finding = {
        "label": "error handling",
        "evidence": "calc.py:4 mul() does not validate its inputs",
        "file": "calc.py", "line": 4,
    }
    harness_finding = {
        "label": _PRE_REVIEW_RED_LABEL,
        "evidence": "the harness's own pre-review test run was RED — "
                    "failing: tests/test_calc.py::test_mul",
        "file": None, "line": None,
    }
    task = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(task)
    queue = LearningQueue(store)

    baseline = await queue._build_from_review(
        task, findings=[real_finding], attempt=1, review_round=1, distill=None)
    with_harness_row = await queue._build_from_review(
        task, findings=[harness_finding, real_finding], attempt=1,
        review_round=1, distill=None)

    assert baseline is not None and with_harness_row is not None
    # The row does not move the key: a recurrence of the SAME real finding,
    # with or without the harness row riding along, collapses onto the SAME
    # queue entry.
    assert with_harness_row.dedupe_key == baseline.dedupe_key
    assert _PRE_REVIEW_RED_LABEL not in with_harness_row.content, with_harness_row.content
    assert all(
        f["label"] != _PRE_REVIEW_RED_LABEL
        for f in with_harness_row.evidence["findings"]
    ), with_harness_row.evidence

    # Feed the harness row ALONE — nothing learnable, no proposal at all.
    alone = await queue._build_from_review(
        task, findings=[harness_finding], attempt=1, review_round=1, distill=None)
    assert alone is None


async def test_the_harness_row_never_enters_the_review_fail_ledger(
    bare_repo, tmp_path, store,
):
    """AC-4, ledger half: `learning/failures.py::load_failure_records` walks
    every FAILed attempt's persisted review checklist through the SAME
    `_is_infra_finding` filter `_build_from_review` uses. Drive a real FAIL
    round the way the incident happened — a red pre-review run, and a
    reviewer that FAILs for an unrelated reason — so `_run_review` staples
    the `_PRE_REVIEW_RED_LABEL` row onto the SAME persisted `review_checklist`
    as the reviewer's own real finding (`_pre_review_red_checklist_item`,
    `core/orchestrator.py`). The harness row must never surface as a
    `CorrectionRecord` in the review-fail ledger, while the real finding
    riding alongside it still does.
    """
    tr = _red_result()
    reviewer = _FailsOnUnrelatedFinding()

    outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
        store, tmp_path, bare_repo, tr, reviewer)
    assert outcome.status is TaskStatus.FAILED, outcome.detail

    records = await load_failure_records(store, project=str(bare_repo))
    messages = [r.message for r in records]
    assert messages, "the FAIL round's checklist never reached the ledger at all"
    assert not any(_PRE_REVIEW_RED_LABEL in m for m in messages), messages
    assert any("error handling" in m for m in messages), messages
