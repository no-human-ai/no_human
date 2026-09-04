"""Stale review feedback must not survive a later PASS (PLAN.md Part 22 — the
bounded loop must never re-inject an already-fixed round's findings as if
still outstanding).

`Orchestrator._record_review_feedback` writes `task.context['review_feedback']`
wholesale on every review FAIL and was never cleared or superseded on a later
PASS. Two readers built prompts directly from that raw key —
`prompt_blocks.build_resume_digest` and `prompt_blocks.build_distilled_state`
— so a FAIL round's findings, once fixed and accepted by a subsequent PASS,
kept being injected into every later attempt's prompt: false provenance
("the reviewer FAILED your previous attempt on...") and correction-stacking
on an issue that no longer exists.

The fix (approach B, chosen at intake): stamp every `review_feedback` entry
with the round it was raised on, stamp the newest PASS round too, and gate
BOTH readers through one shared predicate,
`prompt_blocks.current_review_feedback`, rather than clearing the raw key at
write time (`review_feedback` stays intact as an audit trail; only its
*rendering* is gated — approach A, a clearer, was rejected at intake).

Round numbering is NOT derived from `len(review_history)` — that list is
BOUNDED (`Orchestrator._REVIEW_HISTORY_ROUNDS * 2`) and its own `"round"`
field collides once truncation has fired once (an earlier attempt at this
fix used that derivation; an independent reviewer caught the collision).
Instead, `Orchestrator._conclude_review_round` stamps an independent,
never-truncated `ctx['review_round_seq']` counter — see its docstring in
`orchestrator.py` for the full mechanism.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from no_human.core.bounds import Bounds
from no_human.core.task import Task
from no_human.core.prompt_blocks import (
    build_distilled_state,
    build_resume_digest,
    current_review_feedback,
)
from no_human.learning import LearningQueue
from no_human.review.reviewer import ChecklistItem, ReviewDecision

REPO = "/tmp/repo-review-currency"


def _decision(passed, blocking=(), advisory=()):
    """Verbatim from tests/test_review_memory.py:17."""
    items = [ChecklistItem(lbl, False, f"{lbl} evidence", severity="high")
             for lbl in blocking]
    items += [ChecklistItem(lbl, False, f"{lbl} evidence", severity="nit")
              for lbl in advisory]
    return ReviewDecision(passed=passed, checklist=items, raw_output="")


def _items(label="image pinning"):
    """Verbatim from tests/test_review_learning.py:_items."""
    return [ChecklistItem(
        label, False, "ci/images.yaml:12 pins `latest`, not a digest",
        file="ci/images.yaml", line=12, comment="pin the digest",
        severity="high")]


def _orch(store):
    """Same shape as tests/test_review_learning.py's `_orch` — a real
    Orchestrator with only the collaborators this path touches, and the
    utility tier stubbed so no test here can make an LLM call."""
    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.events = []
    o._sink = o.events.append
    o.store = store
    o.bounds = Bounds()
    o.config = {}
    o.learning_queue = LearningQueue(store)

    async def _stub(task, prompt):
        return ""

    o._distill_review_lesson = _stub
    return o


async def _task(store, title="Pin the CI images"):
    t = Task.new(title, repo_path=REPO)
    await store.create_task(t)
    return t


async def _fail_round(o, t, *, sha, attempt_n=1):
    """Drive a FAIL round through the REAL production sequence — the same
    order both live call sites (`_run_review`, `_gate_already_satisfied`) use:
    conclude the round (stamps the never-truncated counter), append the
    compact history record, then record the feedback with the just-concluded
    round passed explicitly, exactly as both real call sites now do."""
    decision = _decision(False, blocking=["image pinning"])
    round_no = await o._conclude_review_round(t, decision, sha=sha)
    await o._append_review_history(t, decision, commit_sha=sha)
    await o._record_review_feedback(
        t, _items(), "fix the digest", attempt_n=attempt_n,
        review_round=round_no)
    return round_no


async def _pass_round(o, t, *, sha):
    decision = _decision(True)
    round_no = await o._conclude_review_round(t, decision, sha=sha)
    await o._append_review_history(t, decision, commit_sha=sha)
    return round_no


def _distilled(t):
    return build_distilled_state(t, diff_text="", changed_files=[], last_detail="")


# ── AC1 — a PASS supersedes the FAIL round's findings ─────────────────────── #

@pytest.mark.asyncio
async def test_pass_round_supersedes_the_failed_rounds_findings(store):
    o = _orch(store)
    t = await _task(store)

    await _fail_round(o, t, sha="aaa", attempt_n=1)
    await _pass_round(o, t, sha="bbb")

    digest = build_resume_digest(t)
    assert "image pinning" not in digest
    assert "FAILED your previous attempt" not in digest
    assert "fix the digest" not in digest


# ── AC2 (negative control) — a genuine FAIL still reaches the next attempt ── #

@pytest.mark.asyncio
async def test_a_failing_round_still_injects_its_findings(store):
    o = _orch(store)
    t = await _task(store)

    await _fail_round(o, t, sha="aaa", attempt_n=1)

    digest = build_resume_digest(t)
    assert "image pinning" in digest
    assert "ci/images.yaml:12" in digest
    assert "FAILED your previous attempt" in digest
    assert "fix the digest" in digest


# ── AC3 — both readers apply the SAME currency predicate ──────────────────── #

@pytest.mark.asyncio
@pytest.mark.parametrize("builder", [build_resume_digest, _distilled])
async def test_both_readers_drop_a_superseded_rounds_findings(store, builder):
    o = _orch(store)
    t = await _task(store)

    await _fail_round(o, t, sha="aaa", attempt_n=1)
    await _pass_round(o, t, sha="bbb")

    assert "image pinning" not in builder(t)


@pytest.mark.asyncio
@pytest.mark.parametrize("builder", [build_resume_digest, _distilled])
async def test_both_readers_keep_a_still_current_failure(store, builder):
    o = _orch(store)
    t = await _task(store)

    await _fail_round(o, t, sha="aaa", attempt_n=1)

    assert "image pinning" in builder(t)


# ── AC3 (structural reinforcement) — the distilled criteria section too ───── #

@pytest.mark.asyncio
async def test_stale_findings_do_not_reach_the_distilled_criteria_section(store):
    o = _orch(store)
    t = await _task(store)
    t.acceptance_criteria = ["pin the digest"]

    await _fail_round(o, t, sha="aaa", attempt_n=1)
    # Before the PASS, the criterion is correctly backed by a live, cited
    # failure — it must NOT read "[status unknown]" yet.
    before = _distilled(t)
    assert "pin the digest — [status unknown]" not in before

    await _pass_round(o, t, sha="bbb")
    after = _distilled(t)
    assert "image pinning" not in after
    assert "pin the digest — [status unknown]" in after


# ── AC4 — no net reduction: unstamped legacy feedback renders unchanged ───── #

def test_unstamped_legacy_feedback_with_no_history_is_unchanged():
    t = Task.new("legacy task", repo_path=REPO)
    t.context = {
        "review_feedback": [{
            "label": "image pinning",
            "evidence": "ci/images.yaml:12 pins `latest`, not a digest",
            "comment": "pin the digest",
            "file": "ci/images.yaml",
            "line": 12,
        }],
        "review_suggested_next": "fix the digest",
    }
    fb, omitted = current_review_feedback(t)
    assert fb == t.context["review_feedback"]
    assert omitted == 0
    digest = build_resume_digest(t)
    assert "image pinning" in digest
    assert "fix the digest" in digest


# ── write-side: `_record_review_feedback` stamps round + sha ──────────────── #

@pytest.mark.asyncio
async def test_record_review_feedback_stamps_round_and_sha(store):
    o = _orch(store)
    t = await _task(store)

    await o._append_review_history(
        t, _decision(False, blocking=["image pinning"]), commit_sha="deadbeef")
    await o._record_review_feedback(t, _items(), attempt_n=1)

    assert t.context["review_feedback"][0]["round"] == 1
    assert t.context["review_feedback"][0]["sha"] == "deadbeef"
    assert t.context["review_feedback_round"] == 1


@pytest.mark.asyncio
async def test_record_review_feedback_tolerates_string_review_history(store):
    """`review_history` can round-trip through the store as its repr'd string
    form — the same tolerance `Orchestrator._passing_review_shas` already
    has. A malformed string must degrade to keeping the findings with an
    empty sha rather than raising; a well-formed one still recovers the
    sha."""
    o = _orch(store)

    t1 = await _task(store, "legacy string history — malformed")
    t1.context = {"review_history": "not a list"}
    await o._record_review_feedback(t1, _items(), attempt_n=1)
    assert t1.context["review_feedback"][0]["label"] == "image pinning"
    assert t1.context["review_feedback"][0]["sha"] == ""

    t2 = await _task(store, "legacy string history — well-formed")
    t2.context = {
        "review_history": "[{'round': 1, 'sha': 'cafebabe', 'passed': False}]",
    }
    await o._record_review_feedback(t2, _items(), attempt_n=1)
    assert t2.context["review_feedback"][0]["sha"] == "cafebabe"
