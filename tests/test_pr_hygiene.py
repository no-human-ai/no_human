"""SCRUM-59: PR hygiene — the ticket prefix is never doubled in the PR/commit
title, and harness-to-coder dialogue paragraphs never leak into the PR body's
implementation summary."""

import re

import pytest

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _unfold(text: str) -> str:
    """Strip the report fold's own markup so a length check sees the coder's
    text alone (`Orchestrator._fold_report`, 2026-08-21)."""
    return re.sub(r"\n\n<details><summary>Rest of the coder's report[^<]*</summary>\n\n"
                  r"|\n\n</details>", "", text)


class _Commit:
    files_changed = 2
    insertions = 10
    deletions = 1


class _Result:
    final_text = "did the thing"
    num_turns = 5


def test_commit_message_no_double_prefix(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("SCRUM-59: Fix X", repo_path="/r", external_id="SCRUM-59")
    msg = orch._commit_message(t)
    assert msg == "SCRUM-59: Fix X"
    assert "SCRUM-59: SCRUM-59:" not in msg


def test_commit_message_prepends_when_absent(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("Fix X", repo_path="/r", external_id="SCRUM-59")
    assert orch._commit_message(t) == "SCRUM-59: Fix X"


def test_commit_message_distinct_key_not_suppressed(store, tmp_path):
    """A different (e.g. shorter, prefix-of) external_id must not falsely
    match a title that merely starts with a similar-looking key."""
    orch = _orch(store, tmp_path)
    t = Task.new("SCRUM-59: Fix X", repo_path="/r", external_id="SCRUM-5")
    assert orch._commit_message(t) == "SCRUM-5: SCRUM-59: Fix X"


def test_commit_message_no_external_id(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("Fix X", repo_path="/r")
    assert orch._commit_message(t) == "Fix X"


def test_pr_body_filters_harness_dialogue(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = (
        "That's expected — repro_tests.json is metadata for the harness "
        "and is never committed.\n\n"
        "Added the real change: dedupe the prefix."
    )
    body = orch._pr_body(t, _Commit(), result)
    assert "Added the real change" in body
    assert "repro_tests.json" not in body
    assert "harness" not in body
    assert "metadata" not in body


def test_pr_body_keeps_clean_summary(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = "Implemented the fix and added tests."
    body = orch._pr_body(t, _Commit(), result)
    assert "Implemented the fix and added tests." in body


def test_pr_body_keeps_metadata_column_paragraph(store, tmp_path):
    """Bare 'metadata' must not be a drop marker — a legitimate summary
    describing a schema change would otherwise be silently dropped."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = "Added a metadata column to the tasks table."
    body = orch._pr_body(t, _Commit(), result)
    assert "Added a metadata column to the tasks table." in body


def test_pr_body_keeps_harness_fixture_paragraph(store, tmp_path):
    """Bare 'harness' must not be a drop marker — a legitimate summary
    describing test infrastructure would otherwise be silently dropped."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = "Extended the test harness fixture to cover the new case."
    body = orch._pr_body(t, _Commit(), result)
    assert "Extended the test harness fixture to cover the new case." in body


def test_pr_body_all_filtered_says_no_summary_was_produced(store, tmp_path):
    """When every paragraph is dropped there is no summary, and the body must
    say that in words.

    UPDATED for C1. `_clean_summary` still returns its placeholder — that
    contract is asserted directly below, so the filtering itself stays pinned —
    but the placeholder was what a real PR (#104) actually shipped under
    "## Changes": a parenthetical a reader skims past. The body
    now states the absence.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = (
        "That's expected — repro_tests.json is for the harness.\n\n"
        "See system instructions for details on the metadata file."
    )
    # the helper's own contract, unchanged
    assert orch._clean_summary(result.final_text) == \
        "_(implementation summary was filtered — see commits)_"
    body = orch._pr_body(t, _Commit(), result)
    assert "**No implementation summary was produced.**" in body
    assert "was not a report of the work" in body
    assert "repro_tests.json" not in body


# ── the PR body must be able to CARRY the evidence a criterion asks for ──────────── #
# These test the ARTIFACT (`_pr_body`'s output), not the helper, because the defect was
# invisible at the helper's own call site: `_clean_summary` returned a plausible string
# every time — just not all of it.


def test_pr_body_carries_EVERY_surviving_paragraph_not_just_the_first(store, tmp_path):
    """`_clean_summary` returned inside its loop, so it kept exactly ONE paragraph.

    Documented as a filter ("drop paragraphs that address the harness"), implemented as a
    first-paragraph extractor with a 600-char cap. Every paragraph after the first was
    discarded from every PR body the product has ever opened.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = (
        "Fixed the gate.\n\n"
        "## Pre-fix failing run\n1 failed, 4 passed\n\n"
        "## Post-fix run\n5 passed"
    )
    body = orch._pr_body(t, _Commit(), result)
    assert "Fixed the gate." in body
    assert "## Pre-fix failing run" in body, (
        "paragraph 2 was dropped — this is the defect that made a PR-body evidence "
        "criterion unsatisfiable rather than merely unmet")
    assert "## Post-fix run" in body
    assert "5 passed" in body


def test_pr_body_can_satisfy_a_scoped_command_evidence_criterion(store, tmp_path):
    """The real shape from task abc7e570, criterion 8: two named headings carrying one
    exact scoped command and its output. The criterion explicitly rules out a full-suite
    run, so the harness's own test-evidence section cannot supply this — it has to come
    from the coder, through the summary. It could not, and the task escalated as
    NOVEL_UNKNOWN against an artifact the pipeline was incapable of producing.
    """
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = (
        "Added the enabled-but-unconfigured refusal to CiGate.step and a regression test.\n\n"
        "## Pre-fix failing run\n"
        ".venv/bin/python -m pytest tests/test_ci_gate.py -q\n\n"
        "    1 failed, 4 passed\n\n"
        "## Post-fix run\n"
        ".venv/bin/python -m pytest tests/test_ci_gate.py -q\n\n"
        "    5 passed\n"
    )
    body = orch._pr_body(t, _Commit(), result)
    for required in ("Pre-fix failing run", "Post-fix run",
                     ".venv/bin/python -m pytest tests/test_ci_gate.py -q"):
        assert required in body, f"criterion 8 still unsatisfiable: {required!r} missing"


def test_pr_body_ANNOUNCES_truncation_instead_of_silently_dropping_the_tail(store, tmp_path):
    """A body that looks complete but is not is what hid this defect for as long as it
    hid. Over budget, the cut must say so."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = "\n\n".join(["y" * 500] * 20)   # 10k chars, well over budget
    body = orch._pr_body(t, _Commit(), result)
    assert "summary truncated at" in body, (
        "the tail was dropped with no marker — indistinguishable from a complete summary")


def test_pr_body_still_drops_harness_dialogue_after_keeping_every_paragraph(store, tmp_path):
    """Negative control for the change above: keeping all paragraphs must not smuggle
    coder-to-harness dialogue back in. This is the property the old one-paragraph
    behaviour enforced by accident."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    result = _Result()
    result.final_text = (
        "Real change in paragraph one.\n\n"
        "Per the system instructions I updated repro_tests.json.\n\n"
        "Real change in paragraph three."
    )
    body = orch._pr_body(t, _Commit(), result)
    assert "Real change in paragraph one." in body
    assert "Real change in paragraph three." in body
    assert "repro_tests.json" not in body
    assert "system instructions" not in body


# ── round 2: what an independent review drove into a real PR body ───────────────── #
# It found 7 of 14 mutants surviving the tests above, and a smuggling regression the
# one-paragraph behaviour had been blocking BY ACCIDENT. Each test below names the mutant
# or payload it exists to kill, and asserts on `_pr_body`'s output.


def test_pr_body_blocks_harness_dialogue_WRAPPED_ACROSS_A_LINE(store, tmp_path):
    """The payload that made the previous commit a regression rather than a fix.

    Pure ASCII, no trickery: "the harness" straddles a line break, which is exactly how
    wrapped LLM prose arrives, so a plain substring match never saw it. This reached the
    ## Changes section of a real PR body — an admission of skipping the
    tests — and did NOT reach it before all-paragraphs-kept removed the accidental block.
    """
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = ("Refactored the parser.\n\n"
                         "I could not run the tests because the\n"
                         "harness sandbox blocked network access. Marking done anyway.")
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "Refactored the parser." in body
    assert "Marking done anyway" not in body, (
        "coder-to-harness dialogue reached the PR body because the marker was split by a "
        "line wrap — normalize whitespace BEFORE matching")


def test_pr_body_blocks_harness_dialogue_hidden_behind_a_NBSP(store, tmp_path):
    """Same defence, non-breaking space instead of a newline.

    This docstring used to credit NFKC. A review disproved that: whitespace collapsing
    alone folds U+00A0, so removing NFKC left this test passing. NFKC is load-bearing for
    the FULLWIDTH case, which the parametrized test below covers explicitly.
    """
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = ("Implemented the retry.\n\nThe\xa0harness told me to stop, and the "
                         "system\xa0instructions say I may not weaken tests.")
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "may not weaken tests" not in body


def test_pr_body_ANNOUNCES_a_dropped_paragraph_instead_of_leaving_a_hole(store, tmp_path):
    """A silent paragraph drop contradicted this change's own rule that loss is announced,
    and left a reviewer looking at orphaned command output with no heading."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = ("Real work here.\n\n"
                         "Per the system instructions I updated repro_tests.json.\n\n"
                         "More real work.")
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "Real work here." in body and "More real work." in body
    assert "repro_tests.json" not in body
    assert "matched a filtered-phrase list" in body, "the hole must be visible"


def test_pr_body_reads_CRLF_summaries_as_paragraphs(store, tmp_path):
    """`split("\\n\\n")` never saw CRLF paragraphs, so a whole CRLF summary was ONE
    paragraph — and a single marker in it destroyed the entire summary."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = ("Fixed the gate.\r\n\r\n## Pre-fix failing run\r\n1 failed\r\n\r\n"
                        "Per the system instructions I edited repro_tests.json.")
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "## Pre-fix failing run" in body
    assert "repro_tests.json" not in body


def test_pr_body_PRESERVES_the_indentation_of_a_captured_output_block(store, tmp_path):
    """Kills M12. An indented block is how captured command output arrives; stripping it
    turns the evidence this function exists to carry into ordinary prose."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = "Ran it.\n\n    1 failed, 4 passed"
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "\n    1 failed, 4 passed" in body, (
        "leading whitespace was stripped — the output block renders as prose")


def test_pr_body_keeps_EVERY_paragraph_of_a_long_summary_not_a_prefix(store, tmp_path):
    """Kills M10 (`kept[:8]`) and M11 (`body[:1]`), which both survived the first round and
    reintroduce the exact silent-tail-drop defect this change exists to remove."""
    orch = _orch(store, tmp_path)
    result = _Result()
    paras = [f"Paragraph number {i} describing a real change." for i in range(1, 13)]
    result.final_text = "\n\n".join(paras)
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    missing = [p for p in paras if p not in body]
    assert not missing, f"{len(missing)} of {len(paras)} paragraphs were dropped: {missing}"
    assert "summary truncated" not in body, "well under budget — nothing to truncate"


def test_pr_body_keeps_each_heading_at_the_start_of_a_line(store, tmp_path):
    """Kills M4 (`" ".join`). Joining with a space leaves the substrings present — so every
    substring assertion still passes — while GFM stops rendering them as headings. The
    motivating criterion asks for two named HEADINGS, so line position is the property.

    UPDATED for H13: the coder's own headings are demoted so they nest UNDER
    "## Changes" instead of becoming siblings of
    "## Test evidence" and "## Stats". Still headings, still at a line start —
    the mutant this test exists to kill (`" ".join`) dies exactly as before.

    UPDATED AGAIN (R7-B): the assertion pins the PROPERTY — a heading, at a line
    start, strictly below the template's own `##` level — and not the exact
    number of hashes. Demotion moved from +1 to +2 because +1 mapped the coder's
    `#` onto `##`, the precise sibling of `## Task` and `## Stats` that this
    whole mechanism exists to prevent; a test that pinned the literal `###`
    would have had to be edited for that fix, which is the wrong way round.
    """
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = ("Fixed it.\n\n## Pre-fix failing run\n1 failed\n\n"
                         "## Post-fix run\n5 passed")
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    for heading in ("Pre-fix failing run", "Post-fix run"):
        m = re.search(rf"^(#+) {re.escape(heading)}$", body, re.M)
        assert m, f"{heading!r} is not a heading at the start of a line"
        assert len(m.group(1)) >= 3, (
            f"{heading!r} renders at h{len(m.group(1))} — a sibling of the "
            f"template's own `## …` sections, not part of the summary")


def test_pr_body_keeps_paragraphs_in_their_original_order(store, tmp_path):
    """Kills M14 (`reversed(kept)`), which inverted pre-fix and post-fix evidence while
    every substring assertion still passed."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = ("Fixed it.\n\n## Pre-fix failing run\n1 failed\n\n"
                         "## Post-fix run\n5 passed")
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert body.index("## Pre-fix failing run") < body.index("## Post-fix run")


def test_pr_body_truncation_marker_states_the_REAL_budget(store, tmp_path):
    """Kills M13 (`n=0`), which made the artifact assert "truncated at 0 characters"."""
    from no_human.core.orchestrator import Orchestrator
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = "\n\n".join(["w" * 500] * 20)
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert f"truncated at {Orchestrator._SUMMARY_MAX_CHARS} characters" in body


def test_pr_body_truncation_stays_inside_its_stated_budget(store, tmp_path):
    """The first version called 4000 a cap and emitted 4080 — the marker was added after
    the cut. The budget covers the marker."""
    from no_human.core.orchestrator import Orchestrator
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = "\n\n".join(["v" * 500] * 20)
    section = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    summary = section.split("## Changes\n", 1)[1].split("\n\n## ", 1)[0]
    # The cap bounds the CODER'S text; the fold's own `<details>` wrapper
    # (2026-08-21) is template markup and is not counted against it.
    summary = _unfold(summary)
    assert len(summary) <= Orchestrator._SUMMARY_MAX_CHARS, len(summary)


def test_pr_body_never_leaves_a_code_fence_open_after_truncating(store, tmp_path):
    """An odd fence count swallows ## Test evidence, ## Review evidence and ## Stats — and
    the truncation notice itself — into a code block. Found in the artifact by a review."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = "Intro.\n\n```\n" + ("logline padding here 12345678\n" * 300) + "```"
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert body.count("```") % 2 == 0, (
        f"orphaned fence ({body.count('```')}) — everything after it renders as code")


# ── round 2 of review: 19 of 24 payloads reached a real PR body, and 9 new mutants
# survived. Each test below names the payload class or mutant it kills. ─────────────── #

import pytest


@pytest.mark.parametrize("label,payload", [
    ("zero-width space between words", "the​harness blocked it."),
    ("zero-width space after a space", "the ​harness blocked it."),
    ("BOM / ZWNBSP",                   "the﻿harness blocked it."),
    ("word joiner",                    "the⁠harness blocked it."),
    ("soft hyphen inside the word",    "the har\xadness blocked it."),
    ("non-breaking space",             "the\xa0harness blocked it."),
    ("fullwidth forms",                "ｔｈｅ　ｈａｒｎｅｓｓ blocked it."),
    ("markdown emphasis *x*",          "the *harness* blocked it."),
    ("markdown emphasis _x_",          "the _harness_ blocked it."),
    ("code span `x`",                  "the `harness` blocked it."),
    ("interposed punctuation",         "the, harness blocked it."),
    ("HTML comment between words",     "the <!-- ignore --> harness blocked it."),
    # A comment INSIDE the word is the soft-hyphen case with different syntax: it must
    # VANISH (`har<!--x-->ness` -> `harness`) where the between-words case must SEPARATE.
    # The between-words case above passed while this one smuggled through, because the
    # substitution used a single form (" ") instead of the two forms Cf characters get.
    ("HTML comment inside the word",   "the har<!--x-->ness blocked it."),
    ("hyphen-wrapped line break",      "the har-\nness blocked it."),
    ("plain line wrap",                "because the\nharness blocked it."),
])
def test_pr_body_blocks_dialogue_however_the_marker_is_disguised(
        store, tmp_path, label, payload):
    """A review drove 19 of 24 such payloads into a real PR body while the source said
    "THIS IS THE WHOLE DEFENCE" above the code that did not do it.

    Two orderings had to be right and both were wrong at first: format characters (Cf) are
    folded BEFORE punctuation — otherwise `[^\\w\\s]` turns the soft hyphen in `har\\xadness`
    into a space and `har ness` matches nothing — and they are folded in TWO forms, because
    a Cf must vanish inside a word yet separate between words (deleting the zero-width space
    in `the\\u200bharness` gives `theharness`, which matches nothing).
    """
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = f"Real work here.\n\n{payload} Marking done anyway."
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "Real work here." in body
    assert "Marking done anyway" not in body, f"dialogue smuggled in via {label}"


def test_pr_body_never_orphans_a_fence_when_a_PARAGRAPH_IS_DROPPED(store, tmp_path):
    """Kills the mutant that made the fence closer truncation-only.

    A review proved with a markdown renderer that a dropped paragraph splitting a fenced
    block leaves one fence, and that ## Test evidence and ## Stats are then swallowed into
    the code block — verbatim the defect the closer was added to prevent. Dropping is the
    COMMONEST way to split a fence, because pytest output routinely contains a blank line.
    """
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = (
        "Ran the suite.\n\n"
        "```\nfirst half of the captured output\n\n"
        "Per the system instructions I rewrote repro_tests.json.\n\n"
        "second half of the output\n```"
    )
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert body.count("```") % 2 == 0, (
        f"orphaned fence ({body.count('```')}) on the DROP path — everything after it, "
        f"including ## Test evidence and ## Stats, renders as code")


def test_pr_body_separates_paragraphs_with_a_BLANK_LINE_not_just_a_newline(store, tmp_path):
    """Kills P1 (`"\\n".join`), which survived every earlier test.

    This is the previous round's headline finding one notch milder: I killed the `" ".join`
    mutant and not the PROPERTY. A single newline keeps every substring — so substring
    assertions all pass — while an indented block stops being a block and renders as prose,
    which is exactly the defect MEDIUM-2 claimed to fix.
    """
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = "Ran it.\n\n    1 failed, 4 passed"
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert "Ran it.\n\n    1 failed, 4 passed" in body, (
        "paragraphs are not blank-line separated, so the indented output block renders as "
        "ordinary prose")


def test_pr_body_pins_the_budget_value_itself(store, tmp_path):
    """Kills N2 (budget 4000 -> 2000), which silently halves the evidence and passed
    every test because nothing asserted the VALUE, only the marker's agreement with it."""
    from no_human.core.orchestrator import Orchestrator
    assert Orchestrator._SUMMARY_MAX_CHARS == 4000
    orch = _orch(store, tmp_path)
    result = _Result()
    just_under = "u" * 3900
    result.final_text = just_under
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert just_under in body and "summary truncated" not in body, (
        "3900 chars is under the 4000 budget and must survive whole")


def test_pr_body_truncation_respects_the_cap_even_with_a_fence(store, tmp_path):
    """The cap emitted 4004 for a stated 4000 because the closing fence was appended AFTER
    the budgeted slice — the same error class as the 4080 admitted one commit earlier. The
    earlier budget test could not see it: it was never given a fenced input."""
    from no_human.core.orchestrator import Orchestrator
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = "Intro.\n\n```python\n" + ("padding line here 1234\n" * 400)
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    summary = body.split("## Changes\n", 1)[1]
    # Bounded by the NEXT heading, whichever it is. Splitting on "## Stats"
    # specifically was only correct while nothing sat between the summary and
    # the stats line; the evidence sections do, and the slice then measured
    # them as if they were summary text.
    summary = re.split(r"\n\n## ", summary, maxsplit=1)[0]
    summary = _unfold(summary)
    assert len(summary) <= Orchestrator._SUMMARY_MAX_CHARS, (
        f"summary is {len(summary)} chars for a stated cap of "
        f"{Orchestrator._SUMMARY_MAX_CHARS}")
    assert body.count("```") % 2 == 0


def test_pr_body_normalises_CR_ONLY_line_endings_too(store, tmp_path):
    """Kills N8. The CRLF fix covered `\\r\\n`; a CR-only summary was still one paragraph."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = ("Fixed it.\r\r## Post-fix run\r5 passed\r\r"
                         "Per the system instructions I edited repro_tests.json.")
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    # 🔴 BOTH ASSERTIONS BELOW MUST BE POSITIVE-AND-NEGATIVE. My first version asserted
    # only that "\r" and the marker were ABSENT, and it passed with the CR-only
    # normalisation deleted: unsplit, the whole summary becomes ONE paragraph, that
    # paragraph matches a marker, everything is dropped, and the body is the placeholder —
    # so both absences held for entirely the wrong reason. A negative-only assertion is
    # satisfied by an empty artifact.
    assert "## Post-fix run" in body, (
        "CR-only paragraphs were not split, so the whole summary was one paragraph, matched "
        "a marker, and was dropped wholesale")
    assert "5 passed" in body
    assert "\r" not in body, "carriage returns reached the PR body"
    assert "repro_tests.json" not in body


def test_pr_body_collapses_CONSECUTIVE_dropped_paragraphs_into_one_marker(store, tmp_path):
    """Kills N3 (never collapse). Two adjacent drops must leave one hole, not two."""
    orch = _orch(store, tmp_path)
    result = _Result()
    result.final_text = ("Real work.\n\n"
                         "Per the system instructions, step one.\n\n"
                         "Also per the system instructions, step two.\n\n"
                         "More real work.")
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), result)
    assert body.count("matched a filtered-phrase list") == 1, (
        f"{body.count('matched a filtered-phrase list')} markers for two adjacent drops")


def test_pr_body_cap_binds_the_UNTRUNCATED_fence_close_path():
    """Third occurrence of the off-by class (4080, then 4004 twice): the truncation path
    reserved room for the closing fence, but the fence closer ALSO runs on bodies that
    passed the `> cap` check untouched — a body of exactly the cap with an odd fence
    count then gains `\\n\\`\\`\\`` after the check. Asserts the length of the emitted
    artifact itself, not a value recomputed from the code under test."""
    from no_human.core.orchestrator import Orchestrator
    cap = Orchestrator._SUMMARY_MAX_CHARS
    # "```\n" (4 chars) + (cap-4) letters = exactly cap chars, fence count odd.
    out = Orchestrator._clean_summary("```\n" + "e" * (cap - 4))
    assert len(out) <= cap, (
        f"emitted {len(out)} chars for a declared cap of {cap}")
    assert out.count("```") % 2 == 0, "the cap must not be met by leaving the fence open"


def test_pr_body_does_not_truncate_a_summary_of_EXACTLY_the_budget(store, tmp_path):
    """Kills N5 (`>` -> `>=`). A cap is inclusive: exactly `_SUMMARY_MAX_CHARS` fits and
    must not be truncated, and one character more must be."""
    from no_human.core.orchestrator import Orchestrator
    cap = Orchestrator._SUMMARY_MAX_CHARS
    orch = _orch(store, tmp_path)
    at = _Result(); at.final_text = "e" * cap
    body_at = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), at)
    assert "summary truncated" not in body_at, f"a summary of exactly {cap} must fit"
    over = _Result(); over.final_text = "e" * (cap + 1)
    body_over = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), over)
    assert "summary truncated" in body_over, f"{cap + 1} chars must truncate"


# ═══ 2026-08-21: the coder's report is CAPPED and FOLDED, never dropped ═════ #


def test_a_long_report_is_folded_after_the_visible_cap():
    paras = [f"paragraph {i} " + "x" * 200 for i in range(20)]
    out = Orchestrator._fold_report("\n\n".join(paras))
    visible = out.split("<details>", 1)[0]
    assert len(visible) <= Orchestrator._REPORT_VISIBLE_CHARS
    assert "<details><summary>Rest of the coder's report" in out
    assert out.count("paragraph 19") == 1
    assert "paragraph 19" in out.split("<details>", 1)[1]


def test_a_short_report_is_not_folded_at_all():
    out = Orchestrator._fold_report("one\n\ntwo")
    assert out == "one\n\ntwo"


def test_a_not_met_line_is_never_folded():
    paras = ["p " + "x" * 300 for _ in range(10)]
    paras.append("CRITERION: the retry is tested — NOT-MET — evidence: none yet")
    out = Orchestrator._fold_report("\n\n".join(paras))
    assert "NOT-MET" in out.split("<details>", 1)[0]


def test_a_fence_is_never_split_by_the_fold():
    fence = "```\n" + "\n".join("line " + "y" * 80 for _ in range(30)) + "\n```"
    out = Orchestrator._fold_report("intro\n\n" + fence + "\n\nafter")
    visible = out.split("<details>", 1)[0]
    assert visible.count("```") % 2 == 0
    assert out.count("```") == 2


def test_criterion_lines_become_a_compact_list():
    src = ("**CRITERION: `_wrap_title` measures cells — MET — evidence: "
           "`src/x.py:217`; repro: `tests/t.py::test_a` fails before, passes after.**")
    out = Orchestrator._compact_criterion_lines(src)
    assert out.startswith("- **MET** — `_wrap_title` measures cells — _evidence: ")
    assert out.endswith("passes after._")
    assert "CRITERION" not in out


def test_a_not_met_criterion_line_keeps_its_verdict():
    out = Orchestrator._compact_criterion_lines(
        "CRITERION: retries capped - NOT-MET - evidence: no test yet")
    assert out == "- **NOT-MET** — retries capped — _evidence: no test yet_"


def test_the_task_heading_is_gone_and_changes_replaces_summary(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("Fix the thing", repo_path="/r")
    r = _Result()
    r.final_text = "Did the thing."
    body = orch._pr_body(t, _Commit(), r)
    assert "## Task\n" not in body
    assert "## Implementation summary" not in body
    assert "## Changes\nDid the thing.\n" in body


def test_a_tilde_fence_and_an_indented_fence_are_never_split_by_the_fold():
    """Independent review of #576: a naive backtick tracker opened the
    `<details>` tag INSIDE a `~~~` block and desynced on an indented fence.
    The fold now reads the one scanner the demoter uses."""
    tilde = "~~~\n" + "line a\n\nline b\n" * 60 + "~~~"
    out = Orchestrator._fold_report("intro\n\n" + tilde + "\n\nafter")
    visible = out.split("<details>", 1)[0]
    assert visible.count("~~~") % 2 == 0, visible
    assert "<details>" not in out.split("~~~")[1]
    indented = "    ```\n" + "\n\n".join(f"p{i} " + "z" * 200 for i in range(12))
    out = Orchestrator._fold_report(indented)
    assert "more paragraphs)" in out and "(1 more paragraph)" not in out


def test_the_mandated_bare_criterion_line_is_compacted_too(store, tmp_path):
    """The coder is TOLD to write a bare `CRITERION: … — MET — evidence: …`
    line; the demoter list-prefixes it before the compactor sees it. The
    review found the compactor inert on exactly that form."""
    orch = _orch(store, tmp_path)
    r = _Result()
    r.final_text = ("CRITERION: foo — MET — evidence: x\n"
                    "CRITERION: bar — NOT-MET — evidence: none yet")
    body = orch._pr_body(Task.new("t", repo_path="/r"), _Commit(), r)
    assert "- **MET** — foo — _evidence: x_" in body
    assert "- **NOT-MET** — bar — _evidence: none yet_" in body
    assert "CRITERION:" not in body
