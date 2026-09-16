"""The review gate was arithmetically unpassable (M2.2, forced by task 84251cb2).

`passed = reviewer.passed AND every item passed`, while the gate prompt told the
reviewer to "consolidate the least critical ones into a single 'minor issues'
item". So any diff with more than five findings manufactured a failing item and
the gate could never pass. That, not task difficulty, is why no_human had never
opened a reviewed PR.

A blocking finding is now one graded critical/high/medium — or ungraded. low and
nit are recorded for the human and never block. Every other route fails closed.
"""

import json

import pytest

from no_human.review.reviewer import (
    ADVISORY_SEVERITIES,
    ReviewDecision,
    _build_review_prompt,
    _parse_review_output,
)
from no_human.review.selfcheck import ChecklistItem
from no_human.core.task import Task


def _decision(items, *, passed=True, stages=None) -> ReviewDecision:
    payload = {"passed": passed, "items": items}
    if stages is not None:
        payload["stages"] = stages
    return _parse_review_output(
        "REVIEW_JSON_START " + json.dumps(payload) + " REVIEW_JSON_END"
    )


def _item(label, ok, sev, **kw):
    return {"label": label, "passed": ok, "severity": sev,
            "evidence": kw.get("evidence", "f.py:1"), **kw}


# ------------------------- the bug that blocked every PR -------------------- #


def test_a_nit_alone_no_longer_fails_the_gate():
    """The exact shape the reviewer emitted on attempts 15, 16 and 17 of task
    84251cb2: real work, plus one bucket item literally called 'Minor issues'.

    Reverting `_gate_verdict` to `reviewer.passed and all_pass` fails this."""
    d = _decision(
        [
            _item("criteria met", True, "medium"),
            _item("Minor issues", False, "nit"),
        ],
        passed=False,  # the reviewer's own reflex "no"
    )
    assert d.passed is True
    assert [i.label for i in d.advisory_items] == ["Minor issues"]
    assert d.blocking_items == []


@pytest.mark.parametrize("sev", ["critical", "high", "medium"])
def test_a_real_defect_still_fails_the_gate(sev):
    d = _decision([_item("zero tests reports PASSED", False, sev)], passed=False)
    assert d.passed is False
    assert len(d.blocking_items) == 1


@pytest.mark.parametrize("sev", sorted(ADVISORY_SEVERITIES))
def test_advisory_severities_are_recorded_but_do_not_block(sev):
    d = _decision([_item("ok", True, "low"), _item("style", False, sev)])
    assert d.passed is True
    assert len(d.advisory_items) == 1


# ------------------------------- fail closed -------------------------------- #


def test_an_unclassified_finding_blocks():
    """A reviewer that omits severity must not thereby wave a defect through."""
    d = _decision([_item("something is wrong", False, "")], passed=False)
    assert d.passed is False
    assert d.blocking_items and d.advisory_items == []


def test_an_unknown_severity_blocks():
    d = _decision([_item("weird", False, "trivial-ish")], passed=False)
    assert d.passed is False


def test_no_items_at_all_fails_closed():
    """Absence of evidence is not evidence of passing."""
    assert _decision([], passed=True).passed is False


def test_a_missed_acceptance_criterion_is_never_a_nit():
    """spec_compliance false blocks even if every item is graded nit."""
    d = _decision(
        [_item("Minor issues", False, "nit")],
        passed=True,
        stages={"spec_compliance": {"passed": False},
                "code_quality": {"passed": True}},
    )
    assert d.passed is False


def test_reviewer_saying_no_while_flagging_nothing_is_trusted():
    """It disagrees with its own checklist; take the 'no'."""
    d = _decision([_item("all good", True, "nit")], passed=False)
    assert d.passed is False


def test_a_clean_review_passes():
    d = _decision([_item("criteria met", True, "nit")], passed=True)
    assert d.passed is True
    assert d.advisory_items == []


def test_severity_is_case_and_space_insensitive():
    d = _decision([_item("x", False, "  NIT  ")], passed=True)
    assert d.passed is True


# ----------------------- the prompt that caused it -------------------------- #


def test_the_gate_prompt_asks_for_severity():
    """It never did — only the code_review prompt did — so every gate finding
    arrived with severity='' and nothing could be treated as a nit."""
    t = Task.new("x")
    t.acceptance_criteria = ["does the thing"]
    prompt = _build_review_prompt(t, "diff", "", "", diff_total_len=4)
    assert "severity" in prompt
    assert "critical" in prompt and "nit" in prompt


def test_the_gate_prompt_forbids_bucketing_real_defects():
    t = Task.new("x")
    prompt = _build_review_prompt(t, "diff", "", "", diff_total_len=4)
    assert "ONLY the low/nit ones" in prompt
    # the clause wraps in the prompt source; compare on normalized whitespace
    flat = " ".join(prompt.split())
    assert "NEVER put a medium-or-higher finding in that bucket" in flat


def test_the_gate_prompt_judges_scope_against_the_criteria():
    """The reviewer demanded a parser self-check on attempt 14, then called it
    out of scope on 15, 16 and 17. Scope is judged against the criteria, not
    against taste."""
    t = Task.new("x")
    prompt = _build_review_prompt(t, "diff", "", "", diff_total_len=4)
    flat = " ".join(prompt.split())
    assert "Judge SCOPE against the acceptance criteria" in flat
    assert "Do not demand work the criteria do not require." in flat


def test_severity_is_a_classification_not_a_score():
    """The gate classifies severity; it never self-scores numerically."""
    t = Task.new("x")
    prompt = _build_review_prompt(t, "diff", "", "", diff_total_len=4)
    assert "never a score" in prompt
    assert "No numeric scores" in prompt
    for n in ("1-10", "1–10", "score of"):
        assert n not in prompt


# --------------- the coder is fed what actually blocks ---------------------- #


def test_blocking_and_advisory_partition_the_failures():
    d = ReviewDecision(
        passed=False,
        checklist=[
            ChecklistItem("a", False, severity="high"),
            ChecklistItem("b", False, severity="nit"),
            ChecklistItem("c", True, severity="low"),
        ],
    )
    assert [i.label for i in d.blocking_items] == ["a"]
    assert [i.label for i in d.advisory_items] == ["b"]
    assert {i.label for i in d.failed_items} == {"a", "b"}


async def test_angle_passes_skipped_after_a_decided_fail(monkeypatch, tmp_path):
    """B2 #11: angles can never flip fail→pass — running them after a decided
    FAIL is pure reviewer-model cost. They must not run."""
    from no_human.review.reviewer import AdversarialReviewer, ReviewDecision

    r = AdversarialReviewer(model="claude-opus-5")
    calls = {"fast": 0}

    async def fake_fast(prompt, repo_path, before_ref="HEAD~1", root_mismatch=""):
        calls["fast"] += 1
        return ReviewDecision(passed=False, raw_output="FAIL")
    monkeypatch.setattr(r, "_fast_review", fake_fast)
    monkeypatch.setattr(r, "_tier_wants_angles", lambda task: True)

    class T:
        id = "t1"; title = "x"; kind = "feature"; context = {}
        acceptance_criteria = []
    d = await r.review(T(), repo_path=str(tmp_path), diff_override="--- a\n+++ b")
    assert d.passed is False
    assert calls["fast"] == 1, "angle passes ran after a decided FAIL"


def test_silent_failure_angle_is_registered_and_distinct():
    """D2 #6: the failure-hunter lens (cc10x) — the bug class reviewers miss."""
    from no_human.review.reviewer import REVIEW_ANGLES

    names = [n for n, _ in REVIEW_ANGLES]
    assert names == ["security", "tests", "maintainability", "silent-failure"]
    focus = dict(REVIEW_ANGLES)["silent-failure"]
    assert "empty catch" in focus and "swallowed" in focus
    # Each angle must stay single-purpose or they re-review each other's work.
    assert "security" not in focus.split("Ignore")[0].lower()


def test_maintainability_angle_is_registered_and_capped():
    """The dark-factory critique: tests catch bugs, not architectural decay —
    the reviewer carries an explicit maintainability-trajectory lens. It must
    stay concrete (no taste findings) and capped below blocking severity, so
    it can never become a nit-driven retry loop."""
    import re

    from no_human.review.reviewer import ADVISORY_SEVERITIES, REVIEW_ANGLES

    focus = dict(REVIEW_ANGLES)["maintainability"]
    assert "NEXT change" in focus
    assert "CONCRETE" in focus
    assert "'Could be cleaner' is not a finding" in focus
    # The cap is named as a severity and that severity must ACTUALLY be
    # advisory in this codebase. Asserting the string alone is how the lens
    # first shipped capped at "medium" — which _is_blocking() treats as
    # blocking, the exact opposite of the docstring above.
    capped = re.findall(r"Grade (?:these|every trajectory finding) '([a-z]+)'", focus)
    assert capped, f"the lens must name the severity it caps at: {focus!r}"
    assert set(capped) <= ADVISORY_SEVERITIES, (
        f"maintainability findings are capped at {capped}, which blocks the "
        f"gate; advisory severities are {sorted(ADVISORY_SEVERITIES)}")
    # single-purpose: it must not re-review the other angles' ground
    head = focus.split("Ignore")[0].lower()
    assert "security" not in head and "injection" not in head


def test_a_maintainability_finding_does_not_block_the_gate():
    """The behaviour the cap exists for, asserted through the fold that
    decides it — not through the prompt string. A trajectory finding at the
    capped severity is recorded and the gate still passes; the same finding
    graded blocking still fails, so this is not a test that passes for the
    wrong reason."""
    import re

    from no_human.review.reviewer import (
        ADVISORY_SEVERITIES,
        ChecklistItem,
        ReviewDecision,
        REVIEW_ANGLES,
        merge_angle_findings,
    )

    cap = re.findall(
        r"Grade (?:these|every trajectory finding) '([a-z]+)'",
        dict(REVIEW_ANGLES)["maintainability"],
    )[0]
    assert cap in ADVISORY_SEVERITIES

    def fold(severity):
        main = ReviewDecision(passed=True, checklist=[
            ChecklistItem("acceptance criteria met", True)])
        angle = ReviewDecision(passed=False, checklist=[
            ChecklistItem(
                "wrapper with one caller at api.py:20 forks the module's shape",
                False, severity=severity)])
        return merge_angle_findings(main, [("maintainability", angle)])

    capped = fold(cap)
    assert capped.passed is True, (
        "a maintainability finding at the capped severity must not fail the gate")
    assert any(i.label.startswith("maintainability: ") for i in capped.checklist), (
        "the finding must still be recorded for the human")
    # negative control: the fold DOES block when the reviewer grades higher.
    assert fold("high").passed is False


def test_the_gate_prompts_maintainability_cap_is_also_advisory():
    """The angle only runs on the complex tier; this STAGE 2 text runs on
    EVERY tier, so it is the higher-impact copy of the same cap — and it was
    the one no test guarded when the lens first shipped capped at 'medium'.
    Asserted on the RENDERED prompt, against the same threshold constant."""
    import re

    from no_human.review.reviewer import ADVISORY_SEVERITIES

    t = Task.new("x")
    t.acceptance_criteria = ["does the thing"]
    prompt = _build_review_prompt(t, "diff", "", "", diff_total_len=4)
    flat = " ".join(prompt.split())
    assert "MAINTAINABILITY TRAJECTORY" in flat
    capped = re.findall(r"Grade these '([a-z]+)'", flat)
    assert capped, f"the gate's trajectory lens must name the severity it caps at: {flat[:0]!r}"
    assert set(capped) <= ADVISORY_SEVERITIES, (
        f"the gate caps trajectory findings at {capped}, which blocks; "
        f"advisory severities are {sorted(ADVISORY_SEVERITIES)}")


def test_gate_and_standalone_prompts_carry_the_maintainability_lens():
    """Both prompt builders ask the trajectory question — the gate's STAGE 2
    and the standalone review's PASS 2 — so the lens exists even for tiers
    that never run the extra angle passes."""
    import inspect

    from no_human.review import reviewer as r

    src = inspect.getsource(r)
    assert src.count("MAINTAINABILITY TRAJECTORY") >= 3  # stage 2 + pass 2 + angle
    assert "make the NEXT change" in src
