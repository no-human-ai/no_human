"""Sprint 1: transcript findings → human-confirmed learning queue.

Proves the ingester routes analyzer Findings into the queue as
source="proposed"/confirmed=0 (never auto-active), is idempotent across runs,
carries provenance, and supports an optional LLM pass that emits LABELS not
scores.
"""

from __future__ import annotations


from no_human.history.analyzer import (
    Finding,
    analyze_transcript,
    build_llm_prompt,
    parse_llm_findings,
)
from no_human.history.extractor import Message, Transcript
from no_human.history.ingester import TranscriptIngester, _dedupe_key
from no_human.learning import LearningQueue


def _finding(content="never push to main", cat="rule", cascade="c1", msg=2):
    return Finding(
        category=cat, title=f"{content[:40]}", content=content,
        source_transcript=cascade, source_title="A chat",
        tags=["history", cat, "user_correction"], source_message=msg,
    )


def _transcript():
    return Transcript(
        cascade_id="cascade-123", title="Widget Alignment", created="2021-03-04",
        messages=[
            Message("assistant", "I'll take a look.", "STEP"),
            Message("user", "never commit secrets to the repo", "STEP"),
            Message("user", "you are going in circles, stop guessing", "STEP"),
        ],
    )


async def test_findings_enqueued_as_unconfirmed_proposals(store):
    ing = TranscriptIngester(store)
    res = await ing.ingest_findings([_finding()])
    assert res.proposed == 1
    # It lands in the learning queue's pending() (source="proposed", confirmed=0)
    q = LearningQueue(store)
    pending = await q.pending()
    assert len(pending) == 1
    assert pending[0]["confirmed"] == 0
    # ...and is absent from the active set until a human confirms it.
    assert await q.active() == []


async def test_ingest_is_idempotent(store):
    ing = TranscriptIngester(store)
    first = await ing.ingest_findings([_finding()])
    second = await ing.ingest_findings([_finding()])
    assert first.proposed == 1
    assert second.proposed == 0
    assert second.duplicates == 1
    # Only one proposal exists, not two.
    assert len(await LearningQueue(store).pending()) == 1


async def test_provenance_carried_in_tags(store):
    ing = TranscriptIngester(store)
    await ing.ingest_findings([_finding(cascade="cascade-xyz", msg=7)])
    pending = await LearningQueue(store).pending()
    tags = pending[0]["tags"]
    assert "src:cascade-xyz" in tags
    assert "msg:7" in tags


def test_project_derived_from_workspace_uri():
    from no_human.history.analyzer import _project_from_workspaces
    assert _project_from_workspaces(
        ["file:///Users/e/git/acme/master/no_human"]) == "/Users/e/git/acme/master/no_human"
    assert _project_from_workspaces(["/plain/path/"]) == "/plain/path"  # trailing slash trimmed
    assert _project_from_workspaces(["", "file:///second/repo"]) == "/second/repo"  # skips blank
    assert _project_from_workspaces([]) == ""
    assert _project_from_workspaces(None) == ""


async def test_mined_rule_is_scoped_to_the_conversations_repo(store):
    """A rule mined from a Metrics-core conversation must NOT surface as a global rule —
    it carries the repo it came from so a no_human task doesn't see it. This is
    the driver of the 197-item confirm-queue flood (all mined rules had empty
    project and were therefore unscoped)."""
    metrics_core = Transcript(
        cascade_id="c-metrics-core", title="Metrics-core Troubleshooting", created="2026-06-17",
        messages=[Message("user", "always verify the fix by running the tests", "STEP")],
        workspaces=["file:///Users/e/git/metrics-core"],
    )
    ing = TranscriptIngester(store)
    res = await ing.ingest_transcripts([metrics_core])
    assert res.proposed >= 1
    pending = await LearningQueue(store).pending()
    assert pending[0]["project"] == "/Users/e/git/metrics-core"


async def test_a_conversation_without_a_workspace_stays_unscoped(store):
    """No workspace → project stays empty (backward compatible, not a crash)."""
    t = Transcript(
        cascade_id="c-nows", title="Ad-hoc", created="2026-06-17",
        messages=[Message("user", "never commit secrets to the repo", "STEP")],
    )
    await TranscriptIngester(store).ingest_transcripts([t])
    pending = await LearningQueue(store).pending()
    assert pending[0]["project"] in (None, "")


async def test_dedupe_key_distinguishes_content():
    a = _dedupe_key(_finding(content="rule A"))
    b = _dedupe_key(_finding(content="rule B"))
    assert a != b
    assert _dedupe_key(_finding(content="rule A")) == a


async def test_ingest_transcripts_runs_heuristic(store):
    ing = TranscriptIngester(store)
    res = await ing.ingest_transcripts([_transcript()])
    assert res.transcripts == 1
    assert res.proposed >= 1  # the heuristic catches "never commit" / "stop guessing"


def test_command_output_masquerading_as_user_is_not_mined():
    """Slash-command expansions and local/bash command output arrive as
    user-role content but are not the user typing a rule. A signal phrase
    matched inside one is a false positive — this is what flooded the confirm
    queue with junk like "(<local-command-stdout>Set model to Sonnet 4…)"."""
    noisy = Transcript(
        cascade_id="c-noise", title="Session", created="2026-06-17",
        messages=[
            # Real user rule → mined.
            Message("user", "always verify the fix by running the tests", "STEP"),
            # Command output that happens to contain "never"/"always" → skipped.
            Message("user", "<local-command-stdout>Set model to Sonnet 4. "
                            "Never mind, always on.</local-command-stdout>", "STEP"),
            Message("user", "<command-name>/model</command-name> always", "STEP"),
            Message("user", "<system-reminder>never do X</system-reminder>", "STEP"),
        ],
    )
    findings = analyze_transcript(noisy)
    assert len(findings) == 1, "only the genuine free-text rule should be mined"
    assert "verify the fix" in findings[0].content
    assert "local-command-stdout" not in findings[0].title.lower()


def test_garbage_conversation_title_is_sanitized_in_proposal_title():
    """A genuine user rule from a conversation whose TITLE field is command
    stdout (ANSI codes / <local-command-stdout>) must still be mined, but the
    proposal title must not carry the machinery — it becomes a neutral label."""
    t = Transcript(
        cascade_id="c-badtitle",
        title="<local-command-stdout>Set model to \x1b[1mSonnet 4.6\x1b[22m</local-command-stdout>",
        created="2026-06-17",
        messages=[Message("user", "always run the tests before pushing", "STEP")],
    )
    findings = analyze_transcript(t)
    assert len(findings) == 1
    assert "local-command-stdout" not in findings[0].title.lower()
    assert "\x1b" not in findings[0].title
    assert "untitled conversation" in findings[0].title


# --------------------------------------------------------------------------- #
# Optional LLM pass                                                            #
# --------------------------------------------------------------------------- #

def test_llm_prompt_asks_for_label_not_score():
    prompt = build_llm_prompt(_transcript())
    assert "importance" in prompt
    assert "low | med | high" in prompt or "low|med|high" in prompt
    # Must NOT request a numeric 1-10 score (constraint #3).
    import re
    assert not re.search(r"score\s+\d+\s*[-–]\s*10", prompt, re.IGNORECASE)
    assert "NOT a number" in prompt or "NOT a score" in prompt


def test_parse_llm_findings_valid():
    text = (
        "FINDINGS_JSON_START\n"
        '{"findings": [{"category": "anti_pattern", "rule": "do not claim you '
        'cannot access a system before checking skills", "anti_pattern": '
        '"claimed cannot access PR", "source_message": 3, "importance": "high"}]}\n'
        "FINDINGS_JSON_END\n"
    )
    findings = parse_llm_findings(text, _transcript())
    assert len(findings) == 1
    f = findings[0]
    assert f.category == "anti_pattern"
    assert f.importance == "high"
    assert f.source_message == 3
    assert "importance:high" in f.tags


def test_parse_llm_findings_rejects_garbage_importance():
    text = (
        'FINDINGS_JSON_START\n{"findings": [{"category": "rule", "rule": "x", '
        '"importance": "11/10"}]}\nFINDINGS_JSON_END'
    )
    findings = parse_llm_findings(text, _transcript())
    assert findings[0].importance == "med"  # invalid label → safe default, not a number


def test_parse_llm_findings_no_block_is_empty():
    assert parse_llm_findings("no json here", _transcript()) == []


async def test_ingest_transcripts_with_llm_pass(store):
    async def fake_llm(prompt):
        return (
            'FINDINGS_JSON_START\n{"findings": [{"category": "skill", "rule": '
            '"use the test-linking skill for test linking", "importance": '
            '"med", "source_message": 1}]}\nFINDINGS_JSON_END'
        )

    ing = TranscriptIngester(store, llm_call=fake_llm)
    res = await ing.ingest_transcripts([_transcript()], use_llm=True)
    # heuristic findings + the one LLM finding
    pending = await LearningQueue(store).pending()
    titles = [p["title"] for p in pending]
    assert any("test-linking" in t for t in titles)
    assert res.proposed >= 2


async def test_proposal_carries_the_whole_rule_text_not_a_slice(store):
    """The wizard's rules-review step renders `proposal["content"]` and nothing
    else, so whatever this dict carries IS what the human is asked to approve.

    It used to carry ``f.content[:400]``. A heuristic finding's content is the
    user's own message verbatim (analyzer.analyze_transcript passes
    ``content=msg.content``), which routinely runs past 400 characters — so the
    card ended mid-sentence, with no ellipsis, no marker, and nothing in the UI
    able to reach the rest. Reported by the first external DMG tester.

    The DB row already holds the full text; only the response was clipped.
    """
    body = (
        "never push straight to main. "
        + "always run the full suite before you open a pull request, and paste the output. "
        * 12
        + "and the last sentence must survive intact."
    )
    assert len(body) > 400, "the fixture has to be long enough to have been clipped"

    ing = TranscriptIngester(store)
    res = await ing.ingest_findings([_finding(content=body)])
    assert res.proposed == 1
    (proposal,) = res.proposals

    # The whole thing, byte for byte — not a prefix of it.
    assert proposal["content"] == body
    assert proposal["content"].endswith("and the last sentence must survive intact.")
    # And the response agrees with the row that was actually written.
    pending = await LearningQueue(store).pending()
    assert pending[0]["content"] == proposal["content"]
