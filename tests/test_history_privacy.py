"""Privacy gates on the history miner (the DMG incident, 2026-08-01).

A real user installed the shipped DMG. no_human read their personal AI
conversation history, took the home address and phone number out of a
conversation about buying a t-shirt, labelled it FACT / HIGH confidence, and
offered it back PRE-TICKED for confirmation as standing engineering guidance.

Three defects made that possible, and this file holds a test for each plus the
regressions that would have caught them:

A. no software-topic scoping — a shopping conversation was a legitimate "task"
B. no personal-data gate anywhere in history/ or learning/
C. the machinery filter was a hand-written list of 11 substrings and the
   compaction preamble was not one of them
D. every proposal arrived at the confirm UI already ticked

The tests deliberately push in BOTH directions. A privacy gate that blocks
everything is not a fix — it silently deletes the lessons the product exists to
collect — so each gate has a companion test proving legitimate engineering
content still gets through.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from no_human.core.db import SOURCE_PROPOSED, Store
from no_human.learning import ORIGIN_HISTORY, LearningQueue
from no_human.history.analyzer import (
    _is_noise_message,
    analyze_transcript,
    build_llm_prompt,
    mine_reply,
    parse_llm_findings,
)
from no_human.history.extractor import Message, Transcript
from no_human.history.ingester import TranscriptIngester
from no_human.history.machinery import is_machinery, machinery_reason
from no_human.history.topic import classify_transcript, is_software_transcript
from no_human.learning import LearningQueue
from no_human.learning.pii import find_pii

MACHINERY_FIXTURE = Path(__file__).resolve().parents[1] / "testdata" / "machinery_strings.txt"


def _fixture_lines() -> list[str]:
    raw = MACHINERY_FIXTURE.read_text(encoding="utf-8").splitlines()
    return [ln for ln in raw if ln.strip() and not ln.lstrip().startswith("#")]


# --------------------------------------------------------------------------- #
# Realistic transcripts                                                        #
# --------------------------------------------------------------------------- #

def _shopping_transcript() -> Transcript:
    """The shape of the conversation that leaked: buying a t-shirt, with a
    delivery address and a phone number handed over for the order."""
    return Transcript(
        cascade_id="c-shop", title="Buying a t-shirt", created="2026-07-20",
        cwd="/Users/op/git/some-repo",   # location is not topic
        messages=[
            Message("user", "I want to buy that navy t-shirt in medium, "
                            "can you help me place the order?", "STEP"),
            Message("assistant", "Sure — I'll need the delivery details.", "STEP"),
            Message("user", "My shipping address is Flat 1, 12 Herzl Street, "
                            "Tel Aviv, Israel. Phone 054-123-4567. "
                            "Always send it to that address, never the office.",
                    "STEP"),
            Message("assistant", "Got it, ordering now.", "STEP"),
            Message("user", "remember that I always want express delivery", "STEP"),
        ],
    )


def _software_transcript() -> Transcript:
    """A genuine engineering conversation — must STILL be mined."""
    return Transcript(
        cascade_id="c-eng", title="Flaky auth test", created="2026-07-20",
        workspaces=["file:///Users/op/git/acme"],
        messages=[
            Message("user", "the login test is flaky in CI, can you look?", "STEP"),
            Message("assistant", "Looking at the test suite now.", "STEP"),
            Message("user", "never commit secrets to the repo, use the env var", "STEP"),
            Message("user", "always run the tests before pushing", "STEP"),
        ],
    )


# --------------------------------------------------------------------------- #
# A. TOPIC SCOPING — off-topic transcripts yield ZERO memories                  #
# --------------------------------------------------------------------------- #

async def test_shopping_transcript_yields_zero_memories(store):
    """THE INCIDENT. A non-software conversation carrying an address and a phone
    number must put NOTHING in the learning queue — not the address, and not the
    'always send it to that address' / 'remember that I always want express
    delivery' pseudo-rules either. Zero, whole-transcript."""
    t = _shopping_transcript()
    assert analyze_transcript(t) == []

    res = await TranscriptIngester(store).ingest_transcripts([t])
    assert res.proposed == 0
    assert await LearningQueue(store).pending() == []


def test_shopping_transcript_is_judged_off_topic():
    verdict = classify_transcript(_shopping_transcript())
    assert verdict.is_software is False
    assert "no software-engineering evidence" in verdict.reason


@pytest.mark.parametrize("title,body", [
    ("Weekend in Rome",
     "book me a flight to Rome on the 14th and a hotel near the station, "
     "always pick a place with free cancellation"),
    ("Blood results",
     "my blood test came back, the doctor says my iron is low — remember that "
     "I always take the supplement with food"),
    ("Council tax",
     "I need to sort out the council tax direct debit, never let it lapse again"),
    ("Dinner",
     "always book the table for 8pm, never earlier, and remember I don't eat "
     "shellfish"),
    # Measured false positive at the original weak threshold of 3 — this one
    # cleared it on check + log + review. It is what set the threshold to 4.
    ("Rome trip",
     "always review the hotel and check the log book of the trip before we go, "
     "never leave it to the last day"),
    ("Discount",
     "use the discount code SAVE10 at checkout, always apply it before paying"),
])
def test_off_topic_conversations_yield_nothing(title, body):
    """Travel, medical, personal admin and everyday life. Each body contains a
    'never X' / 'always X' / 'remember that' phrase that the correction patterns
    match — so without the topic gate every one of these becomes a rule."""
    t = Transcript(cascade_id=f"c-{title}", title=title, created="2026-07-20",
                   messages=[Message("user", body, "STEP")])
    assert is_software_transcript(t) is False
    assert analyze_transcript(t) == []


async def test_genuine_software_transcript_still_produces_memories(store):
    """The guard against 'fixing' this by disabling the feature."""
    findings = analyze_transcript(_software_transcript())
    assert findings, "engineering rules must still be mined"
    contents = " ".join(f.content for f in findings)
    assert "never commit secrets" in contents
    assert "run the tests before pushing" in contents

    res = await TranscriptIngester(store).ingest_transcripts([_software_transcript()])
    assert res.proposed >= 2
    assert await LearningQueue(store).pending()


@pytest.mark.parametrize("body", [
    "never merge without a code review",
    "always squash before you open the PR",
    "the linter is failing on line 42 of auth.py",
    "you are going in circles — revert that and start over",
    "remember to bump the version in pyproject.toml",
    "do not assume the schema, query the database",
    "never push directly to main, always open a PR",
    "add a unit test for that branch of the parser",
    "always run the tests before pushing",
    "never commit secrets to the repo",
    "always check the CI pipeline before you deploy",
])
def test_ordinary_engineering_conversations_still_pass_the_topic_gate(body):
    """The over-block direction for the topic gate. A gate that answers 'no' to
    real engineering chat deletes the lessons the product exists to collect."""
    t = Transcript(cascade_id="c-eng", title="Session", created="2026-07-20",
                   messages=[Message("user", body, "STEP")])
    assert is_software_transcript(t) is True, f"over-blocked: {body!r}"


def test_llm_prompt_demands_a_justified_topic_judgement():
    prompt = build_llm_prompt(_software_transcript())
    assert "topic" in prompt
    assert "software_engineering" in prompt
    assert "topic_reason" in prompt
    # It must name the off-topic domains rather than gesture at "a task".
    for domain in ("shopping", "travel", "medical", "personal admin"):
        assert domain in prompt.lower()
    assert "EMPTY findings list" in prompt


def test_llm_findings_discarded_when_the_model_says_off_topic():
    """Even a model that dutifully lists findings anyway gets discarded.

    The finding deliberately carries NO personal data. An earlier version used a
    home address here and passed even with the topic check disabled — the PII
    gate was silently doing the work, so the test proved nothing about topic."""
    text = (
        'FINDINGS_JSON_START\n'
        '{"topic": "other", "topic_reason": "the user is buying a t-shirt",'
        ' "findings": [{"category": "fact", "rule": "the user prefers navy'
        ' t-shirts in medium", "importance": "high"}]}\n'
        'FINDINGS_JSON_END'
    )
    assert parse_llm_findings(text, _software_transcript()) == []


def test_llm_findings_discarded_when_the_topic_call_is_unjustified():
    """The prompt requires the call to be JUSTIFIED. A bare verdict is not one."""
    text = (
        'FINDINGS_JSON_START\n'
        '{"topic": "software_engineering", "topic_reason": "  ",'
        ' "findings": [{"category": "rule", "rule": "always lint", '
        '"importance": "med"}]}\n'
        'FINDINGS_JSON_END'
    )
    assert parse_llm_findings(text, _software_transcript()) == []


def test_llm_findings_kept_when_the_topic_call_is_justified():
    text = (
        'FINDINGS_JSON_START\n'
        '{"topic": "software_engineering", "topic_reason": "the user is '
        'debugging a flaky test in CI", "findings": [{"category": "rule", '
        '"rule": "always lint before pushing", "importance": "med"}]}\n'
        'FINDINGS_JSON_END'
    )
    findings = parse_llm_findings(text, _software_transcript())
    assert len(findings) == 1
    assert findings[0].content == "always lint before pushing"


def test_llm_cannot_override_the_heuristic_floor_on_an_off_topic_transcript():
    """A model that wrongly claims a shopping conversation is engineering is
    still refused — the heuristic floor runs unconditionally."""
    text = (
        'FINDINGS_JSON_START\n'
        '{"topic": "software_engineering", "topic_reason": "it mentions '
        'ordering", "findings": [{"category": "fact", "rule": "the user '
        'prefers express delivery", "importance": "high"}]}\n'
        'FINDINGS_JSON_END'
    )
    assert parse_llm_findings(text, _shopping_transcript()) == []


async def test_off_topic_transcript_is_never_sent_to_the_model():
    """Shipping a private shopping/medical conversation to an inference backend
    so it can be told to ignore it is itself a disclosure."""
    from no_human.history.analyzer import analyze_transcript_llm

    calls: list[str] = []

    async def spy(prompt: str) -> str:
        calls.append(prompt)
        return ""

    assert await analyze_transcript_llm(_shopping_transcript(), spy) == []
    assert calls == [], "an off-topic transcript must not reach the LLM"

    await analyze_transcript_llm(_software_transcript(), spy)
    assert len(calls) == 1, "a software transcript must still be analyzed"


# --------------------------------------------------------------------------- #
# B. PII GATE — drop, don't redact; and don't over-block engineering content    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind,text", [
    ("street_address", "User's shipping address is Flat 1, X, Israel"),
    ("street_address", "send the parcel to 221B Baker Street, London"),
    ("street_address", "home address: 12 Rothschild Boulevard"),
    ("street_address", "my postcode is SW1A 1AA"),
    ("phone_number", "call me on 054-123-4567 if the build breaks"),
    ("phone_number", "my phone is 0546 123 456"),
    ("phone_number", "reach me at +972 54 123 4567"),
    ("personal_email", "email me at shopper42@gmail.com when it's merged"),
    ("personal_email", "my icloud is mailbox7@icloud.com"),
    ("payment_details", "card number 4111 1111 1111 1111 expires 09/28"),
    ("payment_details", "the cvv is 123 and the card ends 4242"),
    ("payment_details", "IBAN GB29NWBK60161331926819"),
    ("government_id", "my SSN is 123-45-6789"),
    ("government_id", "passport number 987654321 for the visa form"),
    ("date_of_birth", "date of birth 4 March 1988"),
    ("date_of_birth", "I was born on 12/03/1990"),
])
def test_personal_data_is_detected(kind, text):
    found = find_pii(text)
    assert found is not None, f"missed {kind}: {text!r}"
    assert found.kind == kind


@pytest.mark.parametrize("text", [
    # Network / infrastructure facts — digits everywhere, no personal data.
    "the service binds to 192.168.1.10 and the IP address is 10.0.0.4",
    "connect to 127.0.0.1:8080, not localhost:3000",
    "the MAC address is 00:1B:44:11:3A:B7",
    "set the memory address to 0x7fff5fbff8c0",
    "expose port 5432 for postgres and 6379 for redis",
    # Identifiers that look like numbers but are not.
    "bump the version to 1.24.3 and pin sqlite 3.45.1",
    "the commit is 4f2a9c8b1e6d3a7f5c2b0e9d8a1f4c7b2e5d0a3f",
    "the request id is 550e8400-e29b-41d4-a716-446655440000",
    "the timestamp was 1723456789012345678 nanoseconds",
    "AWS account 123456789012 in eu-west-1",
    # Test fixtures and example data.
    "the fixture uses test@example.com and admin@test.local",
    "seed the DB with user@example.org",
    # Git authorship — explicitly legitimate engineering content.
    "Author: A Contributor <shopper42@gmail.com>",
    "Co-Authored-By: A Contributor <shopper42@gmail.com>",
    "git config user.email shopper42@gmail.com",
    "Signed-off-by: A Contributor <shopper42@gmail.com>",
    "5146175+eyalgolan@users.noreply.github.com",
    "noreply@gmail.com",
    # Ordinary engineering prose that brushes against the detectors.
    "always run the tests before pushing",
    "the test suite has 3 flaky cases in unit 2 of the pipeline",
    "flatten the 1D array before the reshape",
    "Caveat: this only holds on Linux, check the CI matrix",
    "the deploy address is derived from the service name",
])
def test_engineering_content_is_not_mistaken_for_personal_data(text):
    assert find_pii(text) is None, f"over-blocked legitimate content: {text!r}"


async def test_pii_finding_never_reaches_the_queue(store):
    """The gate is at the ingester too, not only the analyzer — every caller
    funnels through ingest_findings, so a Finding built any other way is still
    refused. And it is DROPPED, not redacted: nothing about the address is
    persisted, not even a '[REDACTED]' husk."""
    from no_human.history.analyzer import Finding

    dirty = Finding(
        category="fact", title="User's home address",
        content="User's shipping address is Flat 1, Tel Aviv; phone 054-123-4567",
        source_transcript="c-shop", source_title="Buying a t-shirt",
        tags=["history", "fact"], source_message=2, importance="high",
    )
    clean = Finding(
        category="rule", title="Always run the tests",
        content="always run the tests before pushing",
        source_transcript="c-eng", source_title="Flaky auth test",
        tags=["history", "rule"], source_message=3,
    )
    res = await TranscriptIngester(store).ingest_findings([dirty, clean])
    assert res.dropped_pii == 1
    assert res.proposed == 1

    pending = await LearningQueue(store).pending()
    assert len(pending) == 1
    blob = " ".join(f"{p['title']} {p['content']}" for p in pending)
    assert "address" not in blob.lower()
    assert "REDACTED" not in blob.upper()
    assert "054" not in blob


async def test_pii_inside_engineering_content_is_dropped_per_memory(store):
    """PII embedded in an otherwise-legitimate engineering conversation: the
    contaminated memory goes, the separable engineering memory stays."""
    t = Transcript(
        cascade_id="c-mixed", title="Deploy runbook", created="2026-07-20",
        messages=[
            Message("user", "never push on a Friday, the deploy pipeline is "
                            "unstable", "STEP"),
            # Must ALSO match a correction pattern ("remember that …"), or the
            # analyzer would drop it for having no signal phrase and this test
            # would pass without exercising the personal-data gate at all —
            # which is exactly what the first version of it did.
            Message("user", "remember that you should always page me on my "
                            "phone 054-123-4567 when the deploy fails", "STEP"),
        ],
    )
    findings = analyze_transcript(t)
    contents = " ".join(f.content for f in findings)
    assert "never push on a Friday" in contents, "engineering memory preserved"
    assert "054" not in contents, "the memory carrying a phone number is dropped"

    res = await TranscriptIngester(store).ingest_transcripts([t])
    assert res.proposed == 1
    pending = await LearningQueue(store).pending()
    assert all("054" not in p["content"] for p in pending)


async def test_personal_data_in_the_conversation_TITLE_is_caught(store):
    """A separate leak channel from the message bodies. A Finding's title is
    built from the CONVERSATION title, which the per-message gate never sees —
    so a conversation *named* after a delivery address carries it into the queue
    through the title alone. The gate at the queue boundary checks both fields;
    the whole finding goes, title and content together."""
    t = Transcript(
        cascade_id="c-title",
        title="Order shipped to Flat 1, 12 Herzl Street, Tel Aviv",
        created="2026-07-20",
        messages=[Message("user", "never commit secrets to the repo", "STEP")],
    )
    findings = analyze_transcript(t)
    assert findings, "the message itself is a legitimate engineering rule"
    assert "Herzl" in findings[0].title, (
        "precondition: the title channel carries the address past the "
        "per-message gate — if this ever stops being true, this test is no "
        "longer testing the boundary gate")

    res = await TranscriptIngester(store).ingest_transcripts([t])
    assert res.dropped_pii == 1
    assert res.proposed == 0
    assert await LearningQueue(store).pending() == []


def test_cli_history_analyze_drops_personal_data(tmp_path, monkeypatch):
    """`nh history --analyze` writes to the queue DIRECTLY, not through the
    ingester, so it carries its own copy of the gate. A gate on one of two doors
    is not a gate."""
    from click.testing import CliRunner

    from no_human.cli.commands import cli
    from no_human.config import load_config
    from no_human.history import extractor

    def _no_ide(**_kw):
        raise extractor.IDENotRunningError("no IDE")
    monkeypatch.setattr("no_human.history.extractor.extract_transcripts", _no_ide)

    dirty = Transcript(
        cascade_id="c-title",
        title="Order shipped to Flat 1, 12 Herzl Street, Tel Aviv",
        created="2026-07-20",
        messages=[Message("user", "never commit secrets to the repo", "STEP")],
    )
    clean = Transcript(
        cascade_id="c-clean", title="Auth refactor", created="2026-07-20",
        messages=[Message("user", "always run the tests before pushing", "STEP")],
    )
    monkeypatch.setattr(
        "no_human.history.claude_code.extract_claude_code_transcripts",
        lambda **_kw: [dirty, clean])

    # Point the CLI at a throwaway DB. Without this the command writes into the
    # operator's real ~/.no_human database — which is exactly what happened
    # while this test was being written.
    config = load_config()
    config.data["database"]["path"] = str(tmp_path / "cli.db")
    monkeypatch.setattr("no_human.cli.commands._bootstrap",
                        lambda **_kw: (config, None))

    result = CliRunner().invoke(cli, ["history", "--days", "36500", "--analyze"])
    assert result.exit_code == 0, result.output
    assert "1 dropped" in result.output
    assert "personal data" in result.output

    import sqlite3
    rows = sqlite3.connect(tmp_path / "cli.db").execute(
        "SELECT title, content FROM memories").fetchall()
    blob = " ".join(f"{t} {c}" for t, c in rows)
    assert "Herzl" not in blob
    assert "run the tests before pushing" in blob

    # THE ROW MUST BE IN THE QUEUE THE COMMAND NAMES. Everything above this
    # line reads the table with raw SQL, which is why it stayed green while
    # `nh history --analyze` wrote source="history" and `pending()` — the query
    # behind the "review with: nh learnings" the command prints two lines
    # earlier — selects source="proposed". Every proposal this command has ever
    # made was counted, printed, and invisible. A check that reads around the
    # component under test is not observing it.
    assert "1 proposals queued" in result.output
    assert "review with: nh learnings" in result.output

    async def _queued():
        async with Store(tmp_path / "cli.db") as store:
            return await LearningQueue(store).pending()

    pending = asyncio.run(_queued())
    assert [p["title"] for p in pending] and any(
        "run the tests before pushing" in (p["content"] or "") for p in pending
    ), (
        "the mined rule reached the database but not `nh learnings` — "
        f"pending() returned {len(pending)} row(s)"
    )
    # And it says WHICH producer made it, in the column that exists for that
    # question rather than in the one that decides visibility.
    assert {p["origin"] for p in pending} == {ORIGIN_HISTORY}
    assert {p["source"] for p in pending} == {SOURCE_PROPOSED}


def test_the_reply_path_lands_in_the_queue_it_promises(tmp_path):
    """`nh reply`'s mined learnings had the SAME defect (source="reply") and it
    had already cost two real rows in the operator's own database, created
    2026-07-26 and 2026-07-27 from their own review replies. The CLI prints
    "confirm with `nh learnings`"; this asserts that command can see it.

    Driven at the store boundary rather than through `nh reply`, which needs a
    parked task with a blocker to reach the mining branch. The defect was the
    add_memory kwargs, and those are what this pins.
    """
    from no_human.learning import ORIGIN_REPLY

    async def _go():
        async with Store(tmp_path / "reply.db") as store:
            mined = mine_reply("always run the tests before pushing")
            assert mined is not None, "precondition: this reply mines a rule"
            category, desc = mined
            mem_id = await store.add_memory(
                mem_type=category,
                title=f"{desc} (from a review reply)"[:120],
                content="always run the tests before pushing",
                source=SOURCE_PROPOSED, confirmed=False, origin=ORIGIN_REPLY,
                tags=["reply", category, "user_correction"],
                dedupe_key="reply:always run the tests before pushing",
            )
            assert mem_id
            return mem_id, await LearningQueue(store).pending()

    mem_id, pending = asyncio.run(_go())
    assert [p["id"] for p in pending] == [mem_id]
    assert pending[0]["origin"] == ORIGIN_REPLY


def test_an_unconfirmed_memory_cannot_be_written_outside_the_queue(tmp_path):
    """The STRUCTURAL half. Fixing the two call sites leaves the invariant
    enforced by nothing — it was already stated in two docstrings and three
    call sites broke it anyway. `Store.add_memory` refuses the shape, so a
    fourth door fails loudly instead of silently dropping the operator's
    learnings.

    The refusal is scoped: a CONFIRMED row may carry any source (the board
    writes "board", `nh rules add` writes "manual"), because a confirmed row is
    already active and never depended on `pending()` to be seen.
    """
    async def _go():
        async with Store(tmp_path / "guard.db") as store:
            with pytest.raises(ValueError, match=r"`pending\(\)` cannot see"):
                await store.add_memory(
                    mem_type="rule", title="invisible", content="x",
                    source="history", confirmed=False)
            # ... and it refused BEFORE writing anything.
            assert await store.list_memories(include_archived=True) == []
            # A confirmed row with a non-proposed source is still legal.
            assert await store.add_memory(
                mem_type="rule", title="board rule", content="x",
                source="board", confirmed=True) is not None

    asyncio.run(_go())


def test_mine_reply_refuses_personal_data():
    """The operator-reply path reaches the same queue and needs the same gate."""
    assert mine_reply("always run the tests before pushing") is not None
    assert mine_reply("always call me on 054-123-4567 first") is None


async def test_task_outcome_proposals_are_gated_too(store):
    """learning/queue.py is the other door into the queue.

    Memory lifecycle C gates the per-success templated proposal behind
    `propose_on_success` (default off), which would make a DONE-status
    proposal return None regardless of PII — masking the gate this test
    exists to prove. Opt back in so the assertion below still exercises the
    PII check on the outcome-proposal path, not the unrelated flood-control
    default.
    """
    from no_human.core.task import Task, TaskStatus

    q = LearningQueue(store, propose_on_success=True)
    task = Task.new("Ship the order to Flat 1, 12 Herzl Street, Tel Aviv",
                    repo_path="/tmp/repo")
    assert await q.propose_from_outcome(task, status=TaskStatus.DONE) is None
    assert await q.pending() == []


# --------------------------------------------------------------------------- #
# C. MACHINERY FILTER — derived by shape, enumeration asserted complete         #
# --------------------------------------------------------------------------- #

def test_the_exact_compaction_preamble_is_filtered():
    """The string the user actually saw turned into a proposed rule."""
    preamble = (
        "This session is being continued from a previous conversation that ran "
        "out of context. The summary below covers the earlier portion of the "
        "conversation."
    )
    assert is_machinery(preamble) is True
    assert _is_noise_message(preamble) is True
    assert machinery_reason(preamble) == "preamble"


def test_every_machinery_fixture_string_is_filtered():
    """Completeness check on the enumerated half of the filter. When a new
    machinery string is seen in the wild it goes in the fixture; if the filter
    does not already cover it by shape, this fails until the filter grows."""
    lines = _fixture_lines()
    assert len(lines) >= 20, "the fixture must stay a real corpus, not a stub"
    missed = [ln for ln in lines if not is_machinery(ln)]
    assert missed == [], f"machinery not recognised: {missed}"


def test_every_enumerated_phrase_is_backed_by_a_fixture_string():
    """The other direction: no phrase may be enumerated without real evidence
    for it. Keeps the list from accreting guesses that nothing exercises."""
    from no_human.history.machinery import _MACHINERY_PHRASES

    lines = [ln.lower() for ln in _fixture_lines()]
    unbacked = [p for p in _MACHINERY_PHRASES
                if not any(p in ln for ln in lines)]
    assert unbacked == [], f"phrases with no fixture evidence: {unbacked}"


def test_tag_families_are_recognised_by_shape_not_by_name():
    """The point of the shape regex: markers nobody has written down anywhere —
    not in the analyzer, not in the extractor, not in the fixture — are still
    caught the day the harness starts emitting them."""
    from no_human.history.claude_code import _NOISE_PREFIXES
    from no_human.history.machinery import _MACHINERY_PHRASES

    novel = [
        "<command-output>hello</command-output>",
        "<bash-stdin>ls</bash-stdin>",
        "<local-command-timeout>30</local-command-timeout>",
        "<system-warning>disk full</system-warning>",
        "<function_error>boom</function_error>",
        "<tool_result>ok</tool_result>",
    ]
    for text in novel:
        assert not text.lstrip().startswith(_NOISE_PREFIXES), (
            f"{text!r} is in the enumerated list — pick a genuinely novel tag")
        assert not any(p in text.lower() for p in _MACHINERY_PHRASES)
        assert is_machinery(text) is True, f"shape regex missed {text!r}"
        assert machinery_reason(text) == "harness-tag"


def test_extractor_marker_list_cannot_drift_from_the_analyzer():
    """The extractor's own noise list is the analyzer's source, not a parallel
    copy. Adding a marker in claude_code.py must fix the analyzer for free."""
    from no_human.history.claude_code import _NOISE_PREFIXES

    for marker in _NOISE_PREFIXES:
        assert is_machinery(marker + " never add x") is True, marker


def test_machinery_filter_does_not_swallow_real_rules():
    """The over-block direction: these are the lessons the product exists for."""
    for text in [
        "never commit secrets to the repo",
        "always run the tests before pushing",
        # A leading "Caveat:" IS filtered — that is the extractor's own marker
        # for Claude Code's local-command preamble and predates this change.
        # Mid-sentence it must survive, which is why the derived markers are
        # matched as PREFIXES only and never as substrings.
        "there's a caveat: this only holds on Linux — check the CI matrix",
        "don't guess at the API shape, read the schema",
        "remember that we always rebase, never merge",
        "the summary in the PR description must cite the test output",
    ]:
        assert is_machinery(text) is False, f"over-filtered: {text!r}"
        assert machinery_reason(text) == ""


def test_a_compaction_preamble_never_becomes_a_rule():
    """End to end: the preamble contains 'always' and 'do not' phrasing that the
    correction patterns match, so only the machinery filter stands between it
    and the confirm queue."""
    t = Transcript(
        cascade_id="c-compact", title="Continued session", created="2026-07-20",
        messages=[
            Message("user",
                    "This session is being continued from a previous "
                    "conversation that ran out of context. The summary below "
                    "covers the earlier portion of the conversation. The user "
                    "asked to always run the tests and never commit to main.",
                    "STEP"),
            Message("user", "ok, now fix the failing test in the auth module",
                    "STEP"),
        ],
    )
    findings = analyze_transcript(t)
    assert all("session is being continued" not in f.content for f in findings)


# --------------------------------------------------------------------------- #
# D. NOTHING PRE-TICKED                                                        #
# --------------------------------------------------------------------------- #

def test_api_marks_every_proposal_unselected():
    """Server side of the pre-tick fix: the default is stated explicitly by the
    API so no client has to decide, and none can inherit 'ticked' by omission.

    Asserted against the endpoint's source rather than a live run because the
    endpoint needs the user's real ~/.claude history and an LLM backend."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "no_human" / "api" / "app.py").read_text(encoding="utf-8")
    assert 'p["selected"] = False' in src
    assert '"default_selected": False' in src


def test_spa_does_not_pre_tick_proposals():
    """Client side. The line that shipped the defect selected every proposal id:
        setChosenRules(new Set((an.proposals || []).map((p) => p.id)))

    The AI-learnings / proposals review moved OUT of onboarding into Settings'
    LearningsPanel (2026-08-30 — onboarding trimmed 8->6 steps); the never-pre-
    tick guard moved with it. So this guards both ends: the onboarding pre-tick
    defect must never return, AND the LearningsPanel must start with NOTHING
    selected and confirm only the proposals a human explicitly ticked."""
    root = Path(__file__).resolve().parents[1] / "web" / "src"
    onboarding = (root / "Onboarding.jsx").read_text(encoding="utf-8")
    settings = (root / "Settings.jsx").read_text(encoding="utf-8")
    learning = (root / "learningCard.js").read_text(encoding="utf-8")
    # The exact pre-tick defect must never come back to onboarding.
    assert "setChosenRules(new Set((an.proposals || []).map((p) => p.id)))" not in onboarding
    # The proposals-review UI (now the Settings LearningsPanel) starts with an
    # EMPTY selection set — nothing is pre-ticked...
    assert "const [selected, setSelected] = useState(() => new Set())" in settings
    # ...and a bulk confirm acts ONLY on that explicit selection, never on all
    # visible proposals (bulkConfirmIds keeps only ids the human ticked).
    assert "bulkConfirmIds(visible, selected)" in settings
    assert "chosen.has(id)" in learning


def test_pii_bearing_messages_are_dropped_before_the_llm_prompt():
    """A user message carrying PII (phone / home address) must NOT reach the LLM
    prompt. The LLM pass runs against the vendor API, so PII would otherwise be
    transmitted off-machine before the finding is ever dropped at persistence.
    build_llm_prompt filters (drop, not redact — same policy as learning/pii.py)."""
    t = Transcript(
        cascade_id="c-pii", title="mixed", created="2026-07-20",
        workspaces=["file:///Users/op/git/acme"],
        messages=[
            Message("user", "always run the tests before pushing", "STEP"),
            Message("user", "my number is 415-555-0132, ship to 1600 Pennsylvania Ave", "STEP"),
        ],
    )
    prompt = build_llm_prompt(t)
    assert "always run the tests before pushing" in prompt          # clean message kept
    assert "415-555-0132" not in prompt                             # PII message dropped
    assert "Pennsylvania" not in prompt
