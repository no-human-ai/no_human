"""Review continuity: the reviewer may not contradict prior rounds unknowingly.

Live oscillation on 84251cb2: round 14 "self-check is manual-only, enforce it"
→ round 15 "self-check errors every build, gate it" → rounds 16/17 "self-check
is out of scope, remove it". Fresh context each round, no memory of what it
had itself demanded.
"""

from __future__ import annotations


from no_human.core.task import Task
from no_human.review.reviewer import ChecklistItem, ReviewDecision, _build_review_prompt


def _decision(passed, blocking=(), advisory=()):
    items = [ChecklistItem(lbl, False, f"{lbl} evidence", severity="high")
             for lbl in blocking]
    items += [ChecklistItem(lbl, False, f"{lbl} evidence", severity="nit")
              for lbl in advisory]
    return ReviewDecision(passed=passed, checklist=items, raw_output="")


def _orch(store=None):
    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o._sink = lambda e: None
    if store is not None:
        o.store = store
    return o


# ── prompt side ───────────────────────────────────────────────────────────── #

def test_prompt_carries_continuity_and_the_no_relitigate_rule():
    t = Task.new("x")
    prompt = _build_review_prompt(
        t, "diff", "tests", "",
        prior_rounds="  - round 1 [FAIL]: parser drift — evidence",
    )
    assert "REVIEW CONTINUITY" in prompt
    assert "round 1 [FAIL]: parser drift" in prompt
    assert "do NOT reverse a prior round's request" in prompt.replace("Do NOT", "do NOT")
    assert "Operator answers above are binding" in prompt


def test_prompt_has_no_continuity_section_on_round_one():
    t = Task.new("x")
    prompt = _build_review_prompt(t, "diff", "tests", "")
    assert "REVIEW CONTINUITY" not in prompt


def test_prompt_requires_the_diff_to_prove_a_fix():
    t = Task.new("x")
    prompt = _build_review_prompt(
        t, "diff", "tests", "",
        prior_rounds="  - round 1 [FAIL @ abc1234]: parser drift",
    )
    assert "claim, not evidence" in prompt
    assert "ADDRESSED only when the CURRENT diff" in prompt


def test_claims_clause_absent_without_prior_rounds():
    t = Task.new("x")
    prompt = _build_review_prompt(t, "diff", "tests", "")
    assert "claim, not evidence" not in prompt


def test_existing_continuity_rules_survive_verbatim():
    t = Task.new("x")
    prompt = _build_review_prompt(
        t, "diff", "tests", "",
        prior_rounds="  - round 1 [FAIL @ abc1234]: parser drift",
    )
    assert (
        "Do NOT re-litigate a finding a prior round raised and the coder\n"
        "    addressed"
    ) in prompt
    assert (
        "do NOT reverse a prior round's request, unless you\n"
        "    cite NEW evidence (file:line)"
    ) in prompt
    assert (
        "Operator answers above are binding. A scope question they settle\n"
        "    is settled — it is not a finding of any severity."
    ) in prompt
    assert "New findings in code untouched by prior rounds are always fair." in prompt


def test_prompt_names_the_sha_this_round_judges():
    t = Task.new("x")
    prompt = _build_review_prompt(
        t, "diff", "tests", "",
        reviewed_sha="abc1234def5678", reviewed_branch="feat/x",
    )
    assert "You are reviewing abc1234 (branch feat/x)" in prompt
    assert "abc1234def5678" not in prompt
    assert "abc1234def" not in prompt
    assert prompt.index("You are reviewing abc1234") < prompt.index("Task: ")


def test_prompt_omits_the_target_line_without_a_sha():
    t = Task.new("x")
    prompt = _build_review_prompt(t, "diff", "tests", "")
    assert "You are reviewing" not in prompt


def test_prompt_target_line_tolerates_a_missing_branch():
    t = Task.new("x")
    prompt = _build_review_prompt(
        t, "diff", "tests", "",
        reviewed_sha="abc1234def", reviewed_branch="",
    )
    assert "You are reviewing abc1234" in prompt
    assert "(branch " not in prompt


# ── orchestrator side ─────────────────────────────────────────────────────── #


async def test_history_accumulates_and_feeds_the_next_round(store):
    t = Task.new("ci_gate", repo_path="/tmp/x")
    await store.create_task(t)
    o = _orch(store)

    await o._append_review_history(t, _decision(False, blocking=["image pinning"]))
    await o._append_review_history(
        t, _decision(False, blocking=["comment pagination"], advisory=["naming"]))

    text = o._review_continuity(t)
    assert "round 1 [FAIL]: image pinning" in text
    assert "round 2 [FAIL]: comment pagination" in text
    assert "(advisory: naming)" in text


async def test_continuity_renders_the_round_sha(store):
    t = Task.new("ci_gate", repo_path="/tmp/x")
    await store.create_task(t)
    o = _orch(store)

    await o._append_review_history(
        t, _decision(False, blocking=["image pinning"]), commit_sha="abc1234def5678")

    text = o._review_continuity(t)
    assert "round 1 [FAIL @ abc1234]:" in text
    assert "abc1234def5678" not in text


async def test_continuity_tolerates_a_record_without_a_sha(store):
    t = Task.new("ci_gate", repo_path="/tmp/x")
    await store.create_task(t)
    o = _orch(store)

    await o._append_review_history(t, _decision(False, blocking=["image pinning"]))
    ctx = t.context or {}
    history = list(ctx.get("review_history") or [])
    history.append({
        "round": len(history) + 1,
        "passed": False,
        "blocking": ["comment pagination"],
        "advisory": [],
    })
    ctx["review_history"] = history
    t.context = ctx

    text = o._review_continuity(t)
    assert "round 1 [FAIL]: image pinning" in text
    assert "round 2 [FAIL]: comment pagination" in text
    assert "@" not in text


async def test_operator_answers_are_injected_as_binding(store):
    t = Task.new("ci_gate", repo_path="/tmp/x")
    t.context = {"human_replies": [
        {"question": "q", "answer": "keep the self-check stage; it is in scope"},
    ]}
    await store.create_task(t)

    text = _orch(store)._review_continuity(t)
    assert "Operator answers (binding" in text
    assert "keep the self-check stage" in text


async def test_history_is_capped(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    o = _orch(store)
    for i in range(20):
        await o._append_review_history(t, _decision(False, blocking=[f"f{i}"]))

    stored = t.context["review_history"]
    assert len(stored) == o._REVIEW_HISTORY_ROUNDS * 2
    # The continuity block shows only the most recent rounds.
    text = o._review_continuity(t)
    assert "f19" in text and "f0 " not in text


async def test_round_one_continuity_is_empty(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    assert _orch(store)._review_continuity(t) == ""
