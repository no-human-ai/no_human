"""A red suite reaching the reviewer before review must carry the SAME
NEW-vs-pre-existing split TESTING would have computed after review, not an
undifferentiated list of failing ids and a bare `"classified": False`.

Bug (this task): `_run_review`'s pre-review red block used to hand the
reviewer only `failing_test_ids` — no attribution at all — so the LLM
reviewer had no way to tell a failure this diff introduced from one that was
already red on the base tree before the coder ever touched the repo. The fix
computes the split ONCE per round, in `Orchestrator._round_failure_attribution`
(a `_FailureAttribution`, identity-cached on the `TestRunResult` via
`self._round_attribution`), and shares it verbatim between:

  * the reviewer-facing render (`_render_failing_attribution`, folded into
    the prompt through `_build_review_prompt`'s `failing_test_attribution`
    param — see `tests/test_pre_review_red_reaches_coder.py::
    test_build_review_prompt_carries_fixed_failing_ids_section` for the
    legacy/no-attribution prompt text this replaces when attribution is
    supplied); and
  * TESTING's billing path (`_attributed_ids`, fed `attribution.newly` /
    `attribution.owned` from the very same `_FailureAttribution` — see
    `_failed_tests_outcome`).

Ownership (`_owned_failing_tests`: ids this diff itself added/modified) is
never a separate bucket — `_attribution_buckets` only ever returns
`(new_ids, pre_existing_ids, unknown_ids)` — it is an inline annotation
(`` `id` [MODIFIED BY THIS DIFF]``) `_render_failing_attribution` stamps onto
whichever of those three buckets the id already landed in. An owned id must
never be described as pre-existing or "not this change's fault", regardless
of what the base-tree check said (red on base, or inconclusive).

This file drives the real `Orchestrator._run_attempt` through the same stub-
reviewer / stubbed-`_run_tests_once` harness as
`tests/test_pre_review_red_reaches_coder.py` (imported from there rather than
reimplemented), with `_owned_failing_tests` / `_newly_failing_vs_base`
patched directly so each scenario is fast, deterministic, and pins the exact
base-tree verdict under test.
"""

from unittest.mock import AsyncMock, patch

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import TaskStatus
from no_human.review.reviewer import _build_review_prompt, ReviewDecision
from no_human.review.selfcheck import ChecklistItem
from no_human.testing import runner

from .test_pre_review_red_reaches_coder import (  # noqa: F401
    _FailsOnUnrelatedFinding,
    _PassesEverything,
    _run_attempt_with_result_and_reviewer,
    bare_repo,
)


def _red_result_multi(failing_ids: list[str]) -> runner.TestRunResult:
    """Same shape as `test_pre_review_red_reaches_coder._red_result`, but
    carrying MULTIPLE failing ids in one run — required to exercise the
    NEW-vs-pre-existing split, which is meaningless over a single id."""
    blocks = [f"FAILED {i} - AssertionError: assert 5 == 6" for i in failing_ids]
    return runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=len(failing_ids), errors=0,
        command="pytest -q",
        output="\n".join(blocks) + f"\n{len(failing_ids)} failed, 1 passed in 0.01s\n",
        full_output="",
        failure_blocks=blocks,
        failing_tests=list(failing_ids),
    )


class _CapturingReviewer:
    """Stub reviewer that records every kwarg `_run_reviewer` hands it
    (not just the ones a narrower stub would pull into named params), so the
    test can rebuild the EXACT prompt the real reviewer would have seen via
    `_build_review_prompt(**captured)` — asserting on rendered prompt TEXT,
    not merely on the kwargs the orchestrator happened to pass."""

    model = "stub-reviewer"
    _on_event = None

    def __init__(self, passed: bool = True):
        self.calls: list[dict] = []
        self._passed = passed

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                      before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        call = dict(kwargs)
        call["test_output"] = test_output
        call["held_out_output"] = held_out_output
        self.calls.append(call)
        if self._passed:
            return ReviewDecision(passed=True, checklist=[
                ChecklistItem("looks good", True, "clean, minimal diff"),
            ])
        return ReviewDecision(passed=False, checklist=[
            ChecklistItem(
                "error handling", False,
                "calc.py:4 mul() does not validate its inputs",
                severity="high",
            ),
        ])


def _rebuild_prompt(task, call: dict) -> str:
    return _build_review_prompt(
        task,
        diff="--- a/calc.py\n+++ b/calc.py\n@@ ...\n",
        test_output=call.get("test_output", ""),
        held_out_output=call.get("held_out_output", ""),
        failing_test_ids=call.get("failing_test_ids"),
        failing_test_ids_dropped=call.get("failing_test_ids_dropped", 0),
        failing_test_attribution=call.get("failing_test_attribution", ""),
    )


# ---------------------------------------------------------------------------
# AC1: a red suite reaching the reviewer before review carries the same
# NEW-vs-pre-existing split as post-review.
# ---------------------------------------------------------------------------

async def test_pre_review_red_reaches_reviewer_with_the_new_vs_preexisting_split(
    bare_repo, tmp_path, store,
):
    """Fails-before test (pins the bug this task fixes): before the fix, the
    pre-review block handed the reviewer only a flat `failing_test_ids` list
    (`failing_test_attribution` did not exist), so `new_id` and `old_id`
    would appear side by side with no split at all — this test fails against
    that code because neither the NEW nor the ALSO-RED-ON-BASE section (nor
    the split between the two ids) exists in the prompt."""
    new_id = "tests/test_calc.py::test_mul"
    old_id = "tests/test_calc.py::test_legacy"
    tr = _red_result_multi([new_id, old_id])
    reviewer = _CapturingReviewer(passed=False)

    with (
        patch.object(Orchestrator, "_owned_failing_tests", AsyncMock(return_value=[])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=[new_id])),
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    assert reviewer.calls, "reviewer.review was never called"
    prompt = _rebuild_prompt(task, reviewer.calls[0])

    assert "NEW — failing here, green on the base tree" in prompt, prompt
    assert "ALSO RED ON THE BASE TREE" in prompt, prompt

    new_section = prompt.split("NEW — failing here")[1].split(
        "ALSO RED ON THE BASE TREE")[0]
    assert new_id in new_section, new_section
    assert old_id not in new_section, new_section

    base_section = prompt.split("ALSO RED ON THE BASE TREE")[1]
    # Only the part of the prompt up to the next fixed section header matters
    # here — bound it loosely by taking the paragraph, not the whole prompt.
    base_section = base_section.split("\n\n")[0]
    assert old_id in base_section, base_section
    assert new_id not in base_section, base_section


# ---------------------------------------------------------------------------
# AC2: the split is computed ONCE per round and shared by the reviewer-render
# and the round's billing.
# ---------------------------------------------------------------------------

async def test_the_split_is_computed_once_and_shared_with_billing(
    bare_repo, tmp_path, store,
):
    """The reviewer PASSes, so the round reaches TESTING and bills for real.
    `_newly_failing_vs_base` / `_owned_failing_tests` must each be awaited
    EXACTLY once for the whole round — the pre-review render and TESTING's
    billing must reuse the SAME `_FailureAttribution` (identity-cached on the
    `TestRunResult`), not recompute it — and the failure reason billed to the
    coder must name only the NEW id, never the pre-existing one the reviewer
    was told about."""
    new_id = "tests/test_calc.py::test_mul"
    old_id = "tests/test_calc.py::test_legacy"
    tr = _red_result_multi([new_id, old_id])
    reviewer = _PassesEverything()

    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[])) as owned_mock,
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=[new_id])) as newly_mock,
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    assert owned_mock.await_count == 1, owned_mock.await_count
    assert newly_mock.await_count == 1, newly_mock.await_count

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert new_id in outcome.detail, outcome.detail
    assert old_id not in outcome.detail, outcome.detail


# ---------------------------------------------------------------------------
# AC3: a diff-modified (owned) id is never described as pre-existing / "not
# this change's fault", whether the base check says it's red on base or the
# base check could not run at all.
# ---------------------------------------------------------------------------

async def test_owned_id_is_never_excused_when_base_check_says_red_on_base(
    bare_repo, tmp_path, store,
):
    """`newly=[]` — the base check ran cleanly and says BOTH ids were already
    red on the base tree — but one of them (`owned_id`) is also owned (this
    diff modified it). The rendered prompt must keep `owned_id` in the ALSO
    RED ON THE BASE TREE section (ownership is an annotation, not a fourth
    bucket) but must mark it `[MODIFIED BY THIS DIFF]`, and no exculpatory
    wording anywhere in the prompt may attach to it."""
    owned_id = "tests/test_calc.py::test_mul"
    other_id = "tests/test_calc.py::test_legacy"
    tr = _red_result_multi([owned_id, other_id])
    reviewer = _CapturingReviewer(passed=False)

    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[owned_id])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=[])),
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    assert reviewer.calls, "reviewer.review was never called"
    prompt = _rebuild_prompt(task, reviewer.calls[0])

    assert "ALSO RED ON THE BASE TREE" in prompt, prompt
    owned_line = [ln for ln in prompt.splitlines() if f"`{owned_id}`" in ln]
    assert owned_line, prompt
    assert "[MODIFIED BY THIS DIFF]" in owned_line[0], owned_line

    _assert_no_exculpatory_wording_for(prompt, owned_id)


async def test_owned_id_is_never_excused_when_base_check_could_not_run(
    bare_repo, tmp_path, store,
):
    """`newly=None` — the base check was inconclusive (worktree-add failure,
    base run errored, whatever) — so BOTH ids land in ATTRIBUTION UNKNOWN,
    per `_attribution_buckets`. `owned_id` is still owned; the rendered
    prompt must mark it `[MODIFIED BY THIS DIFF]` inside that UNKNOWN
    section (never promoted to a "pre-existing" claim just because the base
    check happened to fail) and no exculpatory wording may attach to it."""
    owned_id = "tests/test_calc.py::test_mul"
    other_id = "tests/test_calc.py::test_legacy"
    tr = _red_result_multi([owned_id, other_id])
    reviewer = _CapturingReviewer(passed=False)

    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[owned_id])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=None)),
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    assert reviewer.calls, "reviewer.review was never called"
    prompt = _rebuild_prompt(task, reviewer.calls[0])

    assert "ATTRIBUTION UNKNOWN" in prompt, prompt
    assert "ALSO RED ON THE BASE TREE" not in prompt, prompt
    assert "NEW — failing here" not in prompt, prompt
    owned_line = [ln for ln in prompt.splitlines() if f"`{owned_id}`" in ln]
    assert owned_line, prompt
    assert "[MODIFIED BY THIS DIFF]" in owned_line[0], owned_line

    _assert_no_exculpatory_wording_for(prompt, owned_id)


def _assert_no_exculpatory_wording_for(prompt: str, owned_id: str) -> None:
    """No phrase anywhere in the prompt may describe `owned_id` as
    pre-existing / not this change's fault. The exculpatory sentence
    `_render_failing_attribution` emits is scoped to "ids above not marked
    [MODIFIED BY THIS DIFF]" — assert that scoping sentence, if present at
    all, never sits in the same section as `owned_id` without ownership."""
    forbidden_phrases = [
        "not this change's fault",
        "not this change's defect",
        "not introduced by this change",
        "the diff had not caused",
    ]
    for phrase in forbidden_phrases:
        assert phrase not in prompt, (phrase, prompt)
    # The one legitimate exculpatory sentence in the module talks about ids
    # NOT marked [MODIFIED BY THIS DIFF] — assert it never claims blanket
    # exculpation while `owned_id` sits, unmarked, in its own line. Matched
    # on the backtick-quoted form `_render_failing_attribution` emits, not a
    # bare substring match, so the raw `FAILED ...` test-output line (which
    # also contains `owned_id`) is never mistaken for the attribution line.
    owned_lines = [ln for ln in prompt.splitlines() if f"`{owned_id}`" in ln]
    assert owned_lines, prompt
    assert "[MODIFIED BY THIS DIFF]" in owned_lines[0]


async def test_an_owned_id_stays_in_its_true_base_bucket_not_a_separate_section(
    bare_repo, tmp_path, store,
):
    """Ownership must never create a FOURTH section. With one NEW id, one
    pre-existing id, and a THIRD id that is both owned and newly-failing,
    the prompt must have exactly the two non-empty sections the buckets
    imply (NEW, ALSO RED ON THE BASE TREE) — never an "OWNED" or similar
    section of its own — and the owned id must appear, annotated, inside the
    NEW section (that is where the base check actually put it)."""
    new_id = "tests/test_calc.py::test_mul"
    old_id = "tests/test_calc.py::test_legacy"
    owned_new_id = "tests/test_calc.py::test_div"
    tr = _red_result_multi([new_id, old_id, owned_new_id])
    reviewer = _CapturingReviewer(passed=False)

    with (
        patch.object(Orchestrator, "_owned_failing_tests",
                     AsyncMock(return_value=[owned_new_id])),
        patch.object(Orchestrator, "_newly_failing_vs_base",
                     AsyncMock(return_value=[new_id, owned_new_id])),
    ):
        outcome, attempts, events, task, orch = await _run_attempt_with_result_and_reviewer(
            store, tmp_path, bare_repo, tr, reviewer)

    assert reviewer.calls, "reviewer.review was never called"
    prompt = _rebuild_prompt(task, reviewer.calls[0])

    assert "NEW — failing here, green on the base tree" in prompt, prompt
    assert "ALSO RED ON THE BASE TREE" in prompt, prompt
    assert "ATTRIBUTION UNKNOWN" not in prompt, prompt
    # No fourth/"owned" section header of any kind.
    for forbidden_header in ("OWNED", "OWNERSHIP", "BILLED"):
        assert forbidden_header not in prompt, prompt

    new_section = prompt.split("NEW — failing here")[1].split(
        "ALSO RED ON THE BASE TREE")[0]
    assert owned_new_id in new_section, new_section
    owned_line = [ln for ln in new_section.splitlines() if owned_new_id in ln][0]
    assert "[MODIFIED BY THIS DIFF]" in owned_line, owned_line
