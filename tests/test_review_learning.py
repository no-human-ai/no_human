"""Reviewer findings become learning proposals (B1 / gap G2).

The gate's blocking findings were the product's biggest labelled-failure
signal and it threw them away: `_record_review_feedback` wrote them to
`task.context["review_feedback"]`, the next attempt of the SAME task read
them, and the next task re-derived "this repo requires X" from scratch —
`LearningQueue._build` had a success branch and a blocker branch, and no
reviewer branch at all.

What must stay true after the wiring:
  * one FAIL round → exactly ONE unconfirmed, evidence-bearing proposal,
  * the same finding again → no second queue entry,
  * the proposal can NEVER reach the reviewer that produced it (gate
    independence) until a human confirms it,
  * the distillation is advisory — it may crash, and the attempt survives,
  * a task at its lifetime ceiling buys no further utility call.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from no_human.core.bounds import Bounds
from no_human.core.db import Store
from no_human.core.task import Task
from no_human.learning import LearningQueue, TYPE_ANTI_PATTERN, TYPE_RULE
from no_human.learning.queue import (
    build_review_distill_prompt,
    parse_review_lesson,
)

REPO = "/tmp/repo-b1"

_LESSON = (
    "TYPE: rule\n"
    "TITLE: Pin container images by digest in ci/images.yaml\n"
    "LESSON: This repo requires every image in ci/images.yaml to carry a "
    "sha256 digest; a floating tag fails the gate.\n"
    "TAGS: images, ci, digest\n"
)


def _findings(label="image pinning"):
    return [{
        "label": label,
        "evidence": "ci/images.yaml:12 pins `latest`, not a digest",
        "comment": "pin the digest",
        "file": "ci/images.yaml",
        "line": 12,
    }]


def _items(label="image pinning"):
    from no_human.review.selfcheck import ChecklistItem
    return [ChecklistItem(
        label, False, "ci/images.yaml:12 pins `latest`, not a digest",
        file="ci/images.yaml", line=12, comment="pin the digest",
        severity="high")]


def _orch(store, *, reply=_LESSON, boom=False):
    """A real Orchestrator with only the collaborators this path touches.
    The utility tier is STUBBED — no test here may make an LLM call."""
    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.events = []
    o._sink = o.events.append
    o.store = store
    o.bounds = Bounds()
    o.config = {}
    o.learning_queue = LearningQueue(store)
    o.distill_calls = []

    async def _stub(task, prompt):
        o.distill_calls.append(prompt)
        if boom:
            raise RuntimeError("utility tier exploded")
        return reply

    o._distill_review_lesson = _stub
    return o


async def _task(store, title="Pin the CI images"):
    t = Task.new(title, repo_path=REPO)
    await store.create_task(t)
    return t


# ── (a) a failed round writes exactly one evidence-bearing proposal ───────── #

@pytest.mark.asyncio
async def test_failed_round_queues_one_unconfirmed_proposal(store):
    o = _orch(store)
    t = await _task(store)

    await o._record_review_feedback(t, _items(), "fix the digest", attempt_n=2)

    pending = await o.learning_queue.pending()
    assert len(pending) == 1, pending
    row = pending[0]
    assert row["confirmed"] == 0
    assert row["source"] == "proposed"          # the queue-visibility contract
    assert row["project"] == REPO               # repo-scoped, as today (B4 changes this)
    assert row["type"] == TYPE_RULE             # the distiller said "rule"
    # Evidence-bearing provenance: which task, which attempt, which round, and
    # the finding verbatim — the human confirming it can check the claim.
    assert f"task {t.id[:8]}" in row["content"]
    assert "attempt 2" in row["content"]
    assert "review round 1" in row["content"]
    assert "image pinning" in row["content"]
    assert "ci/images.yaml:12" in row["content"]
    assert "sha256 digest" in row["content"]
    # Exactly one utility call for the round.
    assert len(o.distill_calls) == 1
    # The old in-task channel is untouched — this is additive.
    assert (t.context or {})["review_feedback"][0]["label"] == "image pinning"


@pytest.mark.asyncio
async def test_record_review_feedback_keeps_every_blocking_finding(store):
    """Regression guard for the historical `[:6]` slice: a review round with
    MORE than 6 blocking items must not silently lose the 7th+ finding between
    the review and the next attempt's prompt."""
    from no_human.review.selfcheck import ChecklistItem
    o = _orch(store)
    t = await _task(store)
    items = [
        ChecklistItem(
            f"criterion {i}", False, f"file{i}.py:{i} evidence {i}",
            file=f"file{i}.py", line=i, comment=f"comment {i}", severity="high")
        for i in range(9)
    ]

    await o._record_review_feedback(t, items, attempt_n=1)

    stored = (t.context or {})["review_feedback"]
    labels = {row["label"] for row in stored}
    omitted = (t.context or {}).get("review_feedback_omitted", 0)
    # No item-count cap: every one of the 9 findings is kept, and the digest
    # need never render an omission count.
    assert len(stored) == 9, stored
    assert labels == {f"criterion {i}" for i in range(9)}
    assert omitted == 0


@pytest.mark.asyncio
async def test_proposal_carries_trigger_tags_from_the_finding(store):
    o = _orch(store)
    await o._record_review_feedback(await _task(store), _items(), attempt_n=1)
    tags = (await o.learning_queue.pending())[0]["tags"]
    # B3: the distiller's free TAGS line ("images, ci, digest") is reduced to
    # the reviewed vocabulary (learning/vocab.py) before it becomes stored
    # data, and the provenance tag now rides on EVERY review proposal, not
    # only the degraded ones.
    assert "review" in tags
    assert "container" in tags and "pipeline" in tags
    assert "digest" not in tags               # free tags are not stored


# ── (b) the same finding twice does not spam the queue ────────────────────── #

@pytest.mark.asyncio
async def test_identical_finding_dedupes_across_rounds_and_tasks(store):
    from no_human.review.reviewer import ReviewDecision

    o = _orch(store)
    t = await _task(store)

    async def _round(task, items, attempt_n=1):
        """The real order `_run_review` uses: history first (it is what the
        round number is read from), then the feedback record."""
        await o._append_review_history(
            task, ReviewDecision(passed=False, checklist=list(items), raw_output=""))
        await o._record_review_feedback(task, items, attempt_n=attempt_n)

    await _round(t, _items())
    # Round 2 of the same task: the reviewer raised the SAME finding again.
    await _round(t, _items(), attempt_n=2)
    assert (t.context or {})["review_history"][-1]["round"] == 2
    # A DIFFERENT task in the same repo hits the same wall.
    await _round(await _task(store, "Add a job"), _items())

    assert len(await o.learning_queue.pending()) == 1

    # A genuinely different finding still gets its own entry.
    await _round(t, _items("comment pagination"), attempt_n=3)
    assert len(await o.learning_queue.pending()) == 2


# ── (c) gate independence: the proposal cannot reach the reviewer ─────────── #

@pytest.mark.asyncio
async def test_proposal_never_reaches_confirmed_rules_or_the_review_prompt(store):
    """Drive the REAL assembly the orchestrator uses for `confirmed_rules`:
    list_memories(confirmed=True) → filter_triggered → build_memories_block →
    _build_review_prompt. A reviewer must not consume a rule derived from its
    own verdict; only a human confirm can let it through."""
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.prompt_blocks import build_memories_block
    from no_human.learning.triggers import filter_triggered
    from no_human.review.reviewer import _build_review_prompt

    o = _orch(store)
    t = await _task(store)
    await o._record_review_feedback(t, _items(), attempt_n=1)
    proposal = (await o.learning_queue.pending())[0]
    assert proposal["confirmed"] == 0

    haystack = f"{t.title} {t.description or ''} ci/images.yaml images digest"
    confirmed = await store.list_memories(confirmed=True, project=REPO)
    assert confirmed == [], "an unconfirmed proposal must not be in the active set"

    block = build_memories_block(
        filter_triggered(confirmed, haystack),
        Orchestrator._RULES_CRITICAL_CAP, Orchestrator._RULES_RELEVANT_CAP,
    )
    prompt = _build_review_prompt(t, "diff", "tests", "", confirmed_rules=block)
    assert "sha256 digest" not in prompt
    assert proposal["title"] not in prompt

    # …and the human gate is what unlocks it: confirm, and only then does the
    # same assembly carry it (proving the test above is not vacuous).
    assert await o.learning_queue.confirm(proposal["id"])
    confirmed = await store.list_memories(confirmed=True, project=REPO)
    block = build_memories_block(
        filter_triggered(confirmed, haystack),
        Orchestrator._RULES_CRITICAL_CAP, Orchestrator._RULES_RELEVANT_CAP,
    )
    assert proposal["title"] in _build_review_prompt(
        t, "diff", "tests", "", confirmed_rules=block)


# ── (d) failure-safety ────────────────────────────────────────────────────── #

@pytest.mark.asyncio
async def test_distillation_exception_does_not_fail_the_attempt(store):
    o = _orch(store, boom=True)
    t = await _task(store)

    await o._record_review_feedback(t, _items(), "fix it", attempt_n=1)

    # The in-task feedback loop still ran…
    reloaded = await store.get_task(t.id)
    assert reloaded.context["review_feedback"][0]["label"] == "image pinning"
    assert reloaded.context["review_suggested_next"] == "fix it"
    # …nothing was queued, and the degradation is VISIBLE (nh doctor counts it).
    assert await o.learning_queue.pending() == []
    advisories = [e["text"] for e in o.events if e["kind"] == "advisory"]
    assert any("review learning" in a for a in advisories), o.events


@pytest.mark.asyncio
async def test_no_learning_queue_is_a_no_op(store):
    o = _orch(store)
    o.learning_queue = None
    await o._record_review_feedback(await _task(store), _items(), attempt_n=1)
    assert o.distill_calls == []


@pytest.mark.asyncio
async def test_undistillable_reply_still_proposes_the_findings(store):
    """The utility tier is advisory: a reply in the wrong shape degrades the
    lesson, it never loses the finding."""
    o = _orch(store, reply="I could not do that.")
    await o._record_review_feedback(await _task(store), _items(), attempt_n=1)
    row = (await o.learning_queue.pending())[0]
    assert row["type"] == TYPE_ANTI_PATTERN
    assert "image pinning" in row["content"]
    assert "(not distilled)" in row["content"]


# ── budget: an exhausted task buys nothing more ───────────────────────────── #

@pytest.mark.asyncio
async def test_task_at_its_lifetime_ceiling_does_not_spend(store):
    o = _orch(store)
    t = await _task(store)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt_id, tokens_used=o.bounds.lifetime_tokens * 2)

    assert await o._at_lifetime_ceiling(t) is True
    await o._record_review_feedback(t, _items(), attempt_n=1)

    assert o.distill_calls == [], "spent utility tokens past the ceiling"
    assert await o.learning_queue.pending() == []
    # The same predicate the loop-head blocker gates on — pinned together so
    # the two cannot drift into disagreeing about what "exhausted" means.
    assert await o._check_lifetime_budget(t) is not None


@pytest.mark.asyncio
async def test_a_task_under_its_ceiling_still_learns(store):
    o = _orch(store)
    t = await _task(store)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt_id, tokens_used=1000)

    assert await o._at_lifetime_ceiling(t) is False
    await o._record_review_feedback(t, _items(), attempt_n=1)
    assert len(await o.learning_queue.pending()) == 1


# ── the one infra-shaped reviewer verdict is not learnable ────────────────── #

@pytest.mark.asyncio
async def test_reviewer_crash_sentinel_is_not_learned(store):
    """`_run_review` fails CLOSED by synthesising a blocking item when the
    reviewer itself crashes. That is an environment failure, not a lesson
    about the repo — the same reasoning as NON_LEARNABLE_CATEGORIES."""
    from no_human.review.selfcheck import ChecklistItem
    o = _orch(store)
    t = await _task(store)

    await o._record_review_feedback(t, [ChecklistItem(
        "reviewer run", False, "reviewer crashed: connection reset")],
        attempt_n=1)

    assert await o.learning_queue.pending() == []
    assert o.distill_calls == []
    # …but a real finding riding alongside one still teaches.
    await o._record_review_feedback(t, [
        ChecklistItem("reviewer run", False, "reviewer crashed: connection reset"),
        *_items(),
    ], attempt_n=1)
    assert len(await o.learning_queue.pending()) == 1


# ── pure units ────────────────────────────────────────────────────────────── #

def test_parse_review_lesson_reads_the_four_line_shape():
    parsed = parse_review_lesson(_LESSON)
    assert parsed is not None
    mem_type, title, lesson, tags = parsed
    assert mem_type == TYPE_RULE
    assert title.startswith("Pin container images")
    assert "sha256 digest" in lesson
    assert tags == ["images", "ci", "digest"]


@pytest.mark.parametrize("reply", ["", None, "no idea", "TYPE: rule\nTITLE: x"])
def test_parse_review_lesson_rejects_anything_without_a_lesson(reply):
    assert parse_review_lesson(reply) is None


def test_parse_review_lesson_defaults_to_anti_pattern():
    parsed = parse_review_lesson("TYPE: anti_pattern\nLESSON: do not do that")
    assert parsed is not None and parsed[0] == TYPE_ANTI_PATTERN


def test_distill_prompt_is_bounded_and_carries_the_evidence():
    t = Task.new("Pin the CI images", repo_path=REPO)
    prompt = build_review_distill_prompt(t, _findings())
    assert "image pinning" in prompt and "ci/images.yaml" in prompt
    assert "Pin the CI images" in prompt
    assert "under 600 characters" in prompt
    assert len(prompt) < 2000


@pytest.mark.asyncio
async def test_queue_level_api_needs_no_llm_at_all(store):
    """The queue never reaches for a backend itself: with no distiller
    injected it still proposes, from the findings alone."""
    q = LearningQueue(store)
    t = Task.new("x", repo_path=REPO)
    mem_id = await q.propose_from_review(
        t, findings=_findings(), attempt=1, review_round=3)
    assert mem_id is not None
    row = (await q.pending())[0]
    assert row["type"] == TYPE_ANTI_PATTERN
    assert "review round 3" in row["content"]


# ── the evidence quotes the repo verbatim, so it can carry personal data ──── #

def _pii_items(label="hardcoded fixture data"):
    """A finding whose EVIDENCE quotes repo content containing a person's
    address. This is not a contrived case: cited evidence is a VERBATIM quote
    of the offending line, so whatever a fixture, a seed file or a support
    thread in the repo contains arrives here inside `evidence`."""
    from no_human.review.selfcheck import ChecklistItem
    return [ChecklistItem(
        label, False,
        "tests/fixtures/order.json:4 hardcodes the shipping address "
        "12 Maple Street, Springfield IL",
        file="tests/fixtures/order.json", line=4,
        comment="build it from a factory", severity="high")]


@pytest.mark.asyncio
async def test_evidence_carrying_personal_data_is_never_proposed(store):
    """`propose_from_outcome` drops a proposal carrying personal data; this
    path writes to the SAME table and must not be the way around that gate."""
    o = _orch(store)
    t = await _task(store)

    await o._record_review_feedback(t, _pii_items(), attempt_n=1)

    assert await o.learning_queue.pending() == [], "PII reached the memories table"
    # Refusal is not silent — a dropped lesson is a thing a human may have to
    # account for later, so it goes to the advisory stream `nh doctor` counts.
    advisories = [e for e in o.events if e["kind"] == "advisory"]
    assert any("personal data" in e["text"] for e in advisories), o.events
    assert any("street_address" in e["text"] for e in advisories), o.events
    # …and the advisory names the KIND, never the value (PIIFinding exposes
    # nothing else on purpose — logging it would recreate the defect one layer
    # down). Nothing anywhere in the event stream quotes the address.
    assert not any("Maple" in str(e) for e in o.events), o.events


@pytest.mark.asyncio
async def test_the_pii_gate_is_not_swallowing_ordinary_findings(store):
    """Non-vacuity for the test above: the same shape of finding, with
    engineering evidence instead of a person's address, IS proposed. Without
    this, a gate that refused everything would pass the assertion above."""
    o = _orch(store)
    await o._record_review_feedback(await _task(store), _items(), attempt_n=1)
    assert len(await o.learning_queue.pending()) == 1


# ── a recurrence that dedupes is still news ───────────────────────────────── #

@pytest.mark.asyncio
async def test_a_deduped_recurrence_is_announced_not_swallowed(store):
    """Dedupe is the queue working — but `add_memory` returns a bare None for
    it, and "we hit this same wall again" is exactly what a human wants out of
    this queue. The second occurrence must not vanish without a trace."""
    o = _orch(store)
    t = await _task(store)
    await o._record_review_feedback(t, _items(), attempt_n=1)
    assert len(await o.learning_queue.pending()) == 1
    o.events.clear()

    await o._record_review_feedback(t, _items(), attempt_n=2)

    assert len(await o.learning_queue.pending()) == 1, "deduped, as designed"
    deduped = [e for e in o.events
               if e["kind"] == "advisory" and "deduped" in e["text"]]
    assert deduped, o.events
    assert "learn:" in deduped[0]["text"], "names the row it collapsed onto"


# ── the ceiling predicate, on the axis that had no observer ───────────────── #

@pytest.mark.asyncio
async def test_the_attempts_axis_of_the_ceiling_gates_both_callers(store):
    """The advisory guard and the BUDGET_EXHAUSTED gate must agree on BOTH
    axes. Only the tokens axis was ever exercised, so the attempts clause of
    the guard could be deleted with every test still green — the two would
    then disagree about what "exhausted" means, and the learning hook would
    buy a utility call for a task the loop head refuses to run.

    Spends NO tokens: the tokens clause cannot be what fires here.

    Each row is closed `status="failed"` (zero tokens, same as before) rather
    than left for `create_attempt`'s own supersede sweep: a row the sweep
    tags `status="interrupted"` with zero recorded work is the 2026-08-20
    DEAD-worker shape and is now excluded from the lifetime cap by design
    (see THE BOUNDARY in `Store.lifetime_usage_by_class`) — a `failed` row
    always counts whatever its token columns say, which is exactly the
    zero-token/attempts-axis case this test is pinning.
    """
    o = _orch(store)
    t = await _task(store)
    for n in range(1, o.bounds.lifetime_attempts + 1):
        aid = await store.create_attempt(t.id, n)
        await store.update_attempt(aid, status="failed")

    assert await o._at_lifetime_ceiling(t) is True
    assert await o._check_lifetime_budget(t) is not None, "the two disagree"

    await o._record_review_feedback(t, _items("attempts axis"), attempt_n=9)
    assert o.distill_calls == [], "spent a utility call past the attempt cap"
    assert await o.learning_queue.pending() == []


@pytest.mark.asyncio
async def test_one_attempt_under_the_cap_both_callers_still_allow_it(store):
    """The other half of the pin, so the assertion above is not just "the
    ceiling is always reached". One attempt below the cap, no tokens spent:
    both callers say there is room."""
    o = _orch(store)
    t = await _task(store)
    for n in range(1, o.bounds.lifetime_attempts):
        await store.create_attempt(t.id, n)

    assert await o._at_lifetime_ceiling(t) is False
    assert await o._check_lifetime_budget(t) is None, "the two disagree"

    await o._record_review_feedback(t, _items("attempts axis under"), attempt_n=8)
    assert len(await o.learning_queue.pending()) == 1


# ── the lesson is what the utility call was spent on; it must survive ─────── #

_LONG_LESSON = (
    "This repository pins every container image by sha256 digest in "
    "ci/images.yaml and verifies it in scripts/check_images.py; a floating "
    "tag such as :latest or :stable fails the gate even when the digest it "
    "currently resolves to is correct, because the check compares the "
    "written reference and not the resolved one. Add the digest to the "
    "reference itself."
)


def _wordy_findings(n=3):
    """`_MAX_FINDINGS` findings, each with evidence at the `_MAX_EVIDENCE`
    bound — the worst case the stored body has to fit."""
    from no_human.review.selfcheck import ChecklistItem
    return [ChecklistItem(
        f"unpinned image {i}", False,
        f"ci/images.yaml:{i} " + f"pins the floating tag latest for service-{i}; " * 8,
        file="ci/images.yaml", line=i, comment="pin the digest", severity="high")
        for i in range(n)]


@pytest.mark.asyncio
async def test_the_distilled_lesson_survives_the_worst_case_body(store):
    """At the maximum number of findings, each with maximum evidence, the
    lesson used to be what got cut: the body was assembled
    header→evidence→lesson and then sliced to `_MAX_CONTENT`, so ~39 of 350
    lesson characters survived. The evidence is the padding here — a reader
    can go and look the cited lines up in the repo — and the one distilled
    sentence is not."""
    from no_human.learning.queue import _MAX_CONTENT
    reply = (f"TYPE: rule\nTITLE: Pin images by digest\nLESSON: {_LONG_LESSON}\n"
             "TAGS: images, ci, digest\n")
    o = _orch(store, reply=reply)
    t = await _task(store)

    await o._record_review_feedback(t, _wordy_findings(), attempt_n=1)

    content = (await o.learning_queue.pending())[0]["content"]
    assert len(content) <= _MAX_CONTENT
    assert _LONG_LESSON in content, (
        f"the lesson was truncated: {len(content)=}\n{content}")
    # …and the evidence still gets whatever room is left, so this is a reorder
    # and not a deletion.
    assert "ci/images.yaml" in content
    assert f"task {t.id[:8]}" in content


# ── which round the provenance names ──────────────────────────────────────── #

@pytest.mark.asyncio
async def test_an_explicit_review_round_beats_the_history_count(store):
    """The already-satisfied gate reviews through `self.reviewer.review`
    directly, so nothing appends to review_history and the count would name a
    round that reviewer did not run. It passes the round instead."""
    o = _orch(store)
    t = await _task(store)
    await o._record_review_feedback(t, _items(), attempt_n=3, review_round=4)
    assert "review round 4" in (await o.learning_queue.pending())[0]["content"]
