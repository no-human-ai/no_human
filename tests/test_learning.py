"""Tests for the human-confirmed learning queue (PLAN.md 4.5)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from no_human.core.task import Task, TaskStatus
from no_human.learning import LearningQueue, TYPE_ANTI_PATTERN, TYPE_SKILL


@pytest_asyncio.fixture
async def queue(store):
    return LearningQueue(store)


def _task():
    t = Task.new("Fix the parser", repo_path="/tmp/repo")
    return t


@pytest.mark.asyncio
async def test_success_proposes_nothing_by_default(queue):
    """Memory lifecycle C: the per-success templated skill proposal (the flood
    source — ~394 of the NULL-origin pending rows measured against a copy of
    the operator's database) is gated behind `propose_on_success`, default
    off. Every existing `LearningQueue(store)` call site — the `queue`
    fixture included — gets the safe direction without being touched."""
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.AWAITING_APPROVAL, summary="added mul()")
    assert mem_id is None
    assert await queue.pending() == []


@pytest.mark.asyncio
async def test_success_proposes_skill_when_flag_enabled(store):
    """The gate is reversible, not a deletion: an operator who opts back in
    (`propose_on_success=True`) gets exactly the old behaviour — this
    reproduces what `test_success_proposes_skill` asserted before the gate."""
    queue = LearningQueue(store, propose_on_success=True)
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.AWAITING_APPROVAL, summary="added mul()")
    assert mem_id is not None
    pending = await queue.pending()
    assert len(pending) == 1
    assert pending[0]["type"] == TYPE_SKILL
    assert pending[0]["confirmed"] == 0
    assert pending[0]["source"] == "proposed"


@pytest.mark.asyncio
async def test_failure_proposes_anti_pattern(queue):
    blocker = {
        "category": "NOVEL_UNKNOWN",
        "root_cause_hypothesis": "CI_GATE runner returns X",
        "tried": ["approach A: failed", "approach B: failed"],
    }
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED, blocker=blocker)
    assert mem_id is not None
    pending = await queue.pending()
    assert pending[0]["type"] == TYPE_ANTI_PATTERN
    assert "CI_GATE runner returns X" in pending[0]["content"]
    assert "NOVEL_UNKNOWN" in pending[0]["content"]


@pytest.mark.asyncio
async def test_proposal_not_in_active_set_until_confirmed(queue):
    # Blocker-derived, not success-derived: the per-success proposal is now
    # gated off by default (test_success_proposes_nothing_by_default), and
    # this test's actual claim — active() excludes an unconfirmed proposal —
    # has nothing to do with which producer wrote the row.
    await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "NOVEL_UNKNOWN", "root_cause_hypothesis": "x"})
    # The active set (what later tasks consult) must NOT include the proposal.
    assert await queue.active() == []
    assert len(await queue.pending()) == 1


@pytest.mark.asyncio
async def test_confirm_promotes_to_active(queue):
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "NOVEL_UNKNOWN", "root_cause_hypothesis": "x"})
    assert await queue.confirm(mem_id) is True
    active = await queue.active()
    assert len(active) == 1
    assert active[0]["confirmed"] == 1
    assert active[0]["source"] == "confirmed"
    # No longer pending.
    assert await queue.pending() == []


@pytest.mark.asyncio
async def test_active_excludes_paused_by_default_but_include_paused_surfaces_it(queue):
    """D3.2 review deferral: `active()` must keep excluding a paused row by
    default (the injection-visibility contract every existing caller
    relies on) while giving the Second-brain UI list an explicit way to ask
    for it back — a paused row stays visible there (`Paused` chip, so
    Restore is discoverable) even though it stays invisible to
    injection."""
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "NOVEL_UNKNOWN", "root_cause_hypothesis": "x"})
    assert await queue.confirm(mem_id) is True
    assert await queue.pause(mem_id) is True

    assert await queue.active() == [], "a paused row must stay out of the default (injectable) view"
    with_paused = await queue.active(include_paused=True)
    assert [r["id"] for r in with_paused] == [mem_id]
    assert with_paused[0]["paused"] == 1


@pytest.mark.asyncio
async def test_active_excludes_archived_by_default_but_include_archived_surfaces_it(queue):
    """D3.2 review-round fix: Delete only archives (`LearningQueue.delete`),
    never a real DELETE FROM — but a caller with no way to ask for the
    archived row back makes that recoverability theoretical. Mirrors
    `include_paused` above."""
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "NOVEL_UNKNOWN", "root_cause_hypothesis": "x"})
    assert await queue.confirm(mem_id) is True
    assert await queue.delete(mem_id) is True

    assert await queue.active() == [], "an archived row must stay out of the default view"
    with_archived = await queue.active(include_archived=True)
    assert [r["id"] for r in with_archived] == [mem_id]
    assert with_archived[0]["archived"] == 1


@pytest.mark.asyncio
async def test_reject_removes_proposal(queue):
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "IMPOSSIBLE", "root_cause_hypothesis": "no API"})
    assert await queue.reject(mem_id) is True
    assert await queue.pending() == []
    assert await queue.active() == []


@pytest.mark.asyncio
async def test_dedupe_same_lesson(queue):
    """The same lesson proposed twice is deduped (one queue entry)."""
    blocker = {"category": "NOVEL_UNKNOWN", "root_cause_hypothesis": "hit the cap"}
    first = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED, blocker=blocker)
    second = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED, blocker=blocker)
    assert first is not None
    assert second is None  # deduped
    assert len(await queue.pending()) == 1


@pytest.mark.asyncio
async def test_distinct_lessons_not_deduped(queue):
    a = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "NOVEL_UNKNOWN", "root_cause_hypothesis": "cap A"})
    b = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "STAGNATION", "root_cause_hypothesis": "cause B"})
    assert a and b and a != b
    assert len(await queue.pending()) == 2


@pytest.mark.asyncio
async def test_transient_and_resource_blockers_do_not_propose(queue):
    """A budget cap, infra flake, quota wall, dependency wait, or missing-access
    gap is environmental — not a reusable code lesson. Proposing a durable
    anti-pattern for each is what flooded the confirm queue to 197 pending."""
    for cat in ("BUDGET_EXHAUSTED", "TRANSIENT_INFRA", "QUOTA",
                "DEPENDENCY_WAIT", "MISSING_ACCESS"):
        mem_id = await queue.propose_from_outcome(
            _task(), status=TaskStatus.ESCALATED,
            blocker={"category": cat, "root_cause_hypothesis": f"{cat} happened"})
        assert mem_id is None, f"{cat} should not propose a durable learning"
    assert await queue.pending() == []


@pytest.mark.asyncio
async def test_transient_flag_suppresses_proposal(queue):
    """An otherwise-learnable category flagged transient=True is suppressed too."""
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "NOVEL_UNKNOWN", "transient": True,
                 "root_cause_hypothesis": "a flake"})
    assert mem_id is None


@pytest.mark.asyncio
async def test_parked_blocker_does_not_propose():
    # _build returns None when there is neither success nor a blocker.
    q = LearningQueue.__new__(LearningQueue)  # no store needed for _build
    assert q._build(_task(), status=TaskStatus.BLOCKED, blocker=None, summary="") is None


def test_mine_reply_captures_reusable_rules_only():
    """2.3: an operator reply that states a rule becomes a learning; a plain
    answer or noise does not."""
    from no_human.history.analyzer import mine_reply
    assert mine_reply("never commit the .env file")[0] == "rule"
    assert mine_reply("always run the tests before pushing")[0] == "rule"
    assert mine_reply("you seem stuck, try another approach")[0] == "anti_pattern"
    assert mine_reply("yes, proceed with that") is None
    assert mine_reply("<bash-stdout>never add x</bash-stdout>") is None
    assert mine_reply("") is None


# ── which verb a "no" uses, per producer (ARCHIVE_ON_REJECT) ──────────────── #
#
# `reject()` dispatches on `origin`: ARCHIVE keeps the row (confirmed=0,
# archived=1) and therefore keeps its dedupe key, so the "no" sticks against a
# producer that re-reads its whole input on every run; DELETE drops row and key,
# so an event-driven producer can re-propose when the event happens again.
#
# The set had exactly ONE origin under test (supervisor→archive, review→delete
# in test_supervisor_learning.py). `history` and `curator` were added to it by
# the same change that made them rejectable at all, and nothing observed them:
# an edit that dropped either one from the frozenset changed a human's "no"
# from durable to a treadmill, and the suite stayed green.
#
# The table below is the DECISION, restated independently of the code's own
# frozenset, and `test_every_origin_declares_a_reject_verb` refuses to run on a
# module that has grown a sixth origin nobody has classified. That is the part a
# hand-written list normally gets wrong: a new producer would otherwise inherit
# `delete` by falling off the end of the set, silently.
_REJECT_VERB = {
    "review": "delete",       # a FAIL round; it returns only on new evidence
    "supervisor": "archive",  # re-reads the whole correction history each run
    "history": "archive",     # re-reads every transcript each `--analyze`
    "reply": "delete",        # the operator saying it again IS new evidence
    "escalation": "archive",    # HarvestJob re-reads every escalation each pass
    "review_fail": "archive",   # HarvestJob re-reads every reviewer FAIL each pass
    "tamper": "archive",        # HarvestJob re-reads every tamper trip each pass
    "curator": "archive",     # re-reads every surviving proposal each `--curate`
}


def test_every_origin_declares_a_reject_verb():
    """A sixth producer must choose archive or delete, not inherit one."""
    from no_human.learning import queue as queue_mod
    origins = {v for k, v in vars(queue_mod).items()
               if k.startswith("ORIGIN_") and isinstance(v, str)}
    assert origins == set(_REJECT_VERB), (
        "learning/queue.py's ORIGIN_* set no longer matches the reject-verb "
        "table. A new origin defaults to DELETE by omission from "
        "ARCHIVE_ON_REJECT — decide, then record it here."
    )


# THE CASE THE TABLE ABOVE CANNOT HOLD: `origin` IS NULL.
#
# `propose_from_outcome` never passes `origin` (learning/queue.py, the
# `add_memory` call in that method lists everything it does pass and `origin` is
# not among them), so every proposal derived from a terminal task outcome is
# written with SQL NULL. NULL is not in ARCHIVE_ON_REJECT, so `reject()` falls
# through to DELETE.
#
# That is not a corner. Measured against a `cp` of the operator's live database
# on 2026-08-07 (never the live file — a running server holds it open):
# 329 of 329 pending proposals carry `origin` NULL, spanning 2026-06-29 to
# 2026-08-03, and so do all 402 rows in the table. The five named producers
# above have written none of them yet; they are proven by these tests and by
# nothing else. So the untested case was the ONLY case that occurs.
#
# Nothing above sees it, for two structural reasons rather than an oversight:
# `test_every_origin_declares_a_reject_verb` enumerates `vars(queue_mod)` for
# `ORIGIN_*` NAMES, and NULL has no name, so no edit can make it notice one;
# and `test_reject_removes_proposal` further up does drive a NULL-origin row
# through `reject()`, but asserts only that it left `pending()` and `active()`
# — which an ARCHIVE satisfies too. Adding None to the frozenset left the whole
# file green.
#
# DELETE IS THE RIGHT VERB, by the frozenset's own criterion — "can this
# producer regenerate the proposal without new evidence?". The only production
# caller is `orchestrator._propose_learning`, fired once per terminal task
# outcome; nothing re-reads finished tasks and re-proposes from them, so there
# is no batch to treadmill. A rejected outcome lesson comes back only when
# another task actually ends the same way, which is a fresh event and worth
# re-asking about — the same argument `review` and `reply` get. The case below
# therefore pins the behaviour that was chosen, not a default nobody chose.
#
# It is NOT in `_REJECT_VERB`: that table is the ORIGIN_* completeness contract
# and adding a nameless key to it would break the test that guards it.
_REJECT_VERB_CASES = [*sorted(_REJECT_VERB.items()), (None, "delete")]


@pytest.mark.parametrize("origin,verb", _REJECT_VERB_CASES)
@pytest.mark.asyncio
async def test_reject_uses_the_right_verb_for_each_origin(queue, store, origin, verb):
    """Behavioural, not a restatement: the row is written with an origin and put
    through `reject()`, and what survives is inspected. A mutant that edits
    ARCHIVE_ON_REJECT flips the outcome for that origin and fails here — and
    `origin=None` is in the sweep because it is what every live row carries."""
    mem_id = await store.add_memory(
        mem_type="rule", title=f"rule from {origin}", content="do the thing",
        source="proposed", confirmed=False, origin=origin,
        dedupe_key=f"key:{origin}",
    )
    assert mem_id is not None
    assert [m["id"] for m in await queue.pending()] == [mem_id]

    assert await queue.reject(mem_id) is True
    assert await queue.pending() == [], "a rejected proposal must leave the queue"

    rows = await store.list_memories(include_archived=True)
    if verb == "archive":
        assert [m["id"] for m in rows] == [mem_id], (
            f"origin={origin!r} must ARCHIVE — the row is gone, so its dedupe "
            "key is gone with it and the next batch re-proposes the lesson the "
            "human just turned down"
        )
        assert rows[0]["archived"] == 1
        assert rows[0]["confirmed"] == 0
        assert "rejected" in rows[0]["content"]
        assert await store.memory_dedupe_key_exists(f"key:{origin}")
    else:
        assert rows == [], (
            f"origin={origin!r} must DELETE — archiving it keeps the dedupe key, "
            "and an event-driven producer could then never re-propose the lesson "
            "when the event genuinely recurs"
        )
        assert not await store.memory_dedupe_key_exists(f"key:{origin}")


# ── memory lifecycle C: the flood-source predicate ────────────────────────── #

@pytest.mark.asyncio
async def test_is_templated_success_proposal_predicate(store):
    """`learning.retire.is_templated_success_proposal` names the flood source
    for the one-time triage — it must match the exact shape `_build`'s
    AWAITING_APPROVAL/DONE branch writes and nothing else."""
    from no_human.learning.retire import is_templated_success_proposal

    queue = LearningQueue(store, propose_on_success=True)
    template_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.AWAITING_APPROVAL, summary="added mul()")
    template_row = await store.find_memory(template_id)
    assert is_templated_success_proposal(template_row) is True

    # A review-origin proposal must never match, even if it happened to share
    # the type — `origin` being set is disqualifying on its own.
    review_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="Approach that worked: something",
        content="Consider capturing the successful approach as a reusable "
                "skill.",
        source="proposed", confirmed=False, origin="review",
        dedupe_key="predicate:review")
    review_row = await store.find_memory(review_id)
    assert is_templated_success_proposal(review_row) is False

    # An anti-pattern (the failure-path proposal) must never match.
    anti_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "NOVEL_UNKNOWN", "root_cause_hypothesis": "x"})
    anti_row = await store.find_memory(anti_id)
    assert is_templated_success_proposal(anti_row) is False

    # A confirmed row must never match — the predicate is triage-target-only.
    confirmed_id = await store.add_memory(
        mem_type=TYPE_SKILL, title="Approach that worked: something else",
        content="Consider capturing the successful approach as a reusable "
                "skill.",
        source="proposed", confirmed=True)
    confirmed_row = await store.find_memory(confirmed_id)
    assert is_templated_success_proposal(confirmed_row) is False
