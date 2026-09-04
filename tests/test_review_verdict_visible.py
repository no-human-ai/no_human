"""The review verdict must say WHY, and must reach the human unaided.

The gate is the one thing standing between an unreviewed diff and a PR, and
until this fix a human could not read its ruling from the CLI at all. Two
independent defects produced that:

1.  The verdict event's `text` was the four characters "PASS"/"FAIL". The
    counts lived in event meta the API formatter strips, and the cited
    evidence lived only in `attempts.review_checklist`. Anything downstream
    therefore saw a verdict with no content — including `events_fts`, whose
    trigger indexes exactly this field, so `_recall_failures` could never
    match a past review.
2.  `nh logs` held that checklist in its hand and printed a bare
    `review: fail`, so "why was this blocked" meant opening the database.

The event-surface half of the same bug (both API filters dropping
`source:"reviewer"`) is covered in tests/test_api.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from no_human.core.orchestrator import Orchestrator, review_verdict_text
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewDecision, findings_from_checklist
from no_human.review.selfcheck import ChecklistItem

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401
from .test_cli_commands import _make_runner, _seed_attempt, _seed_task


# A real finding's shape: graded, cited, with prose evidence.
_BLOCKING = ChecklistItem(
    label="Header calls every failed item non-blocking",
    passed=False,
    evidence="slideOverSummary.js:339 filters with `!it.passed` rather than "
             "isBlockingFinding, so a high-severity item renders as advisory.",
    file="web/src/slideOverSummary.js",
    line=339,
    severity="high",
)
_ADVISORY = ChecklistItem(
    label="stale comment above the helper", passed=False,
    evidence="says 'two lenses', there are three", severity="nit",
)


# --------------------------------------------------------------------------- #
# The verdict carries its own reasoning                                        #
# --------------------------------------------------------------------------- #

def test_verdict_text_states_the_ruling_the_counts_and_the_cited_evidence():
    text = review_verdict_text(False, [_BLOCKING], [_ADVISORY])

    assert text.startswith("FAIL"), text
    assert "1 blocking" in text and "1 advisory" in text
    assert "[high]" in text, "the grade decides whether it blocks — say it"
    assert "web/src/slideOverSummary.js:339" in text, "the citation must survive"
    assert "isBlockingFinding" in text, "the reasoning must survive"
    # Advisories are counted, never quoted — `review_advisory_findings` is
    # their channel and duplicating them here buries the blocking one.
    assert "stale comment" not in text


def test_verdict_text_is_bounded_because_it_is_also_indexed_and_re_read():
    """This string feeds events_fts, which `_recall_failures` reads back into a
    later planner/coder prompt. An unbounded verdict is paid for in tokens."""
    many = [
        ChecklistItem(label=f"finding {i}", passed=False, evidence="x" * 5000,
                      file="a.py", line=i, severity="high")
        for i in range(12)
    ]
    text = review_verdict_text(False, many, [])

    assert len(text) < 1500, f"verdict grew unbounded: {len(text)} chars"
    assert "and 9 more blocking finding(s)" in text, (
        "truncation must ANNOUNCE itself — a silently clipped verdict "
        "understates how much is wrong")
    assert "nh logs" in text, "say where the rest of them are"


async def test_the_gate_emits_the_reasoning_not_just_the_word_FAIL(
    store, bare_repo, tmp_path,
):
    """Wiring, not arithmetic: drive the real `_run_review` with a stub reviewer
    and read the event the orchestrator actually put on the bus."""
    from no_human.vcs.git import GitRepo

    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    cfg.data.setdefault("tests", {})["command"] = "true"   # keep the gate cheap
    events: list[dict] = []

    class StubReviewer:
        _on_event = None
        async def review(self, task, **kwargs):
            return ReviewDecision(passed=False, checklist=[_BLOCKING, _ADVISORY])

    orch = Orchestrator(
        store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None),
        event_sink=events.append, reviewer=StubReviewer(),
    )
    task = Task.new("t", repo_path="/r")
    await store.create_task(task)
    decision = await orch._run_review(task, GitRepo(bare_repo), "attempt-1")
    assert decision.passed is False

    (verdict,) = [e for e in events
                  if e["kind"] == "review" and e["source"] == "reviewer"]
    assert verdict["text"] != "FAIL", (
        "the gate emitted a bare verdict again — every downstream surface, and "
        "the FTS index, is back to a four-character event")
    for marker in ("FAIL", "1 blocking", "[high]",
                   "web/src/slideOverSummary.js:339", "isBlockingFinding"):
        assert marker in verdict["text"], (
            f"{marker!r} never reached the event: {verdict['text']!r}")


# --------------------------------------------------------------------------- #
# `nh logs` answers "why was this blocked"                                     #
# --------------------------------------------------------------------------- #

def test_findings_from_checklist_uses_the_gates_own_severity_rule():
    """A renderer that re-derives "blocking" is a second copy of the rule that
    can disagree with the gate. low/nit are advisory, everything else blocks."""
    stored = json.dumps({"passed": False, "items": [
        {"label": "critical thing", "passed": False, "severity": "critical"},
        {"label": "ungraded thing", "passed": False, "severity": ""},
        {"label": "small thing", "passed": False, "severity": "nit"},
        {"label": "low thing", "passed": False, "severity": "low"},
        {"label": "a passing check", "passed": True, "severity": ""},
    ]})
    blocking, advisory = findings_from_checklist(stored)

    assert [i.label for i in blocking] == ["critical thing", "ungraded thing"]
    assert [i.label for i in advisory] == ["small thing", "low thing"]


@pytest.mark.parametrize("bad", [None, "", "not json", "[]", '{"items": [1, 2]}'])
def test_findings_from_checklist_never_raises_on_a_bad_row(bad):
    """A display path must not be the thing that breaks `nh logs`."""
    assert findings_from_checklist(bad) == ([], [])


def test_nh_logs_prints_the_cited_findings_behind_a_failed_review(
    tmp_path, monkeypatch,
):
    """The operator's actual complaint: `nh logs` knew the review failed and
    would not say what the reviewer objected to."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.FAILED)
    _seed_attempt(
        db, task_id,
        review_passed=0,
        review_checklist={"passed": False, "items": [
            {"label": _BLOCKING.label, "passed": False,
             "evidence": _BLOCKING.evidence, "file": _BLOCKING.file,
             "line": _BLOCKING.line, "severity": _BLOCKING.severity},
            {"label": _ADVISORY.label, "passed": False,
             "evidence": _ADVISORY.evidence, "severity": _ADVISORY.severity},
        ]},
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli_logs(), ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())   # rich wraps; compare unwrapped
    assert "review: fail" in out
    assert _BLOCKING.label in out, (
        f"the blocking finding never reached the CLI:\n{result.output}")
    assert "web/src/slideOverSummary.js:339" in out, "the citation was dropped"
    assert "isBlockingFinding" in out, "the evidence was dropped"
    assert "blocking" in out and "advisory" in out, (
        "a human must be able to tell what BLOCKED the task from what merely "
        "came with it")
    assert _ADVISORY.label in out
    # The grade is what DECIDES blocking vs advisory, so it has to print. It
    # did not: rich parsed a "[high]" tag as console markup and ate it.
    assert "blocking/high" in out and "advisory/nit" in out, (
        f"the severity grade never rendered:\n{result.output}")


def test_nh_logs_does_not_let_rich_eat_the_reviewers_words(
    tmp_path, monkeypatch,
):
    """Reviewer prose is full of `list[str]` and `items[0]`. Passed to rich
    unescaped, "[str]" parses as a style tag and the text is silently CORRUPTED
    — the reader cannot tell anything was removed."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.FAILED)
    # Every field gets a DISTINCT bracketed token. Sharing one token across
    # fields lets a still-escaped field satisfy the assertion for a field that
    # regressed — the first version of this test was blind to exactly that.
    _seed_attempt(
        db, task_id,
        review_passed=0,
        failure_reason="review failed: reason mentions tuple[int] here",
        review_checklist={"passed": False, "items": [
            {"label": "parse() mishandles list[str]", "passed": False,
             "evidence": "annotated dict[bool] but items[0] is read",
             "file": "a.py", "line": 7, "severity": "high"},
            # A SECOND item carries the bracket in `severity`. It has to be its
            # own item rather than a bracket added to the one above: severity
            # is what `_is_blocking` grades on, so bracketing "high" would
            # silently reclassify that finding as advisory and change what the
            # rest of this fixture means. Without this row the `escape()` on
            # the grade is unobservable — every other severity in the suite is
            # bracket-free — and deleting it passes the whole file, on the one
            # field whose own comment calls it "the one field that says whether
            # the finding blocked the task".
            {"label": "second finding", "passed": False,
             "evidence": "unrelated", "file": "b.py", "line": 9,
             "severity": "high[medium]"},
        ]},
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli_logs(), ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    for field, phrase in (("label", "list[str]"),
                          ("evidence", "dict[bool]"),
                          ("evidence", "items[0]"),
                          ("severity", "high[medium]"),
                          ("failure_reason", "tuple[int]")):
        assert phrase in out, (
            f"rich swallowed {phrase!r} out of the reviewer's {field}:\n"
            f"{result.output}")


def test_render_event_prints_the_verdict_grade_it_is_given(capsys):
    """`nh task add --run --verbose` streams events through render_event. It
    printed them as rich markup, so the verdict's own "[high]" grade and any
    evidence naming `list[str]` were dropped on the way to the terminal."""
    from no_human.cli.commands import render_event

    verdict = review_verdict_text(False, [ChecklistItem(
        "add() drops the carry", False,
        "returns a+b; see the list[str] branch", file="calc.py", line=12,
        severity="high")], [])
    render_event({"source": "reviewer", "kind": "review", "text": verdict})

    out = " ".join(capsys.readouterr().out.split())
    assert "[high]" in out, f"the grade was eaten as markup: {out}"
    assert "list[str]" in out, f"the evidence was corrupted: {out}"
    assert "calc.py:12" in out


def cli_logs():
    from no_human.cli.commands import cli
    return cli
