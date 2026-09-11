"""Subagent spend must reach the attempt ledger — exactly once.

Measured against the SDK (claude-agent-sdk, CLI 2.1.220) on 2026-07-30 with two
real subagent runs, recorded at ``testdata/subagent_usage_stream.json`` (one
subagent, one API response) and ``testdata/subagent_usage_stream_multi.json``
(one subagent, six API responses). The identifiers in both fixtures
(message/session/tool-use/task ids) were replaced with synthetic stand-ins
after recording — repeat structure and every usage number are unmodified.
Five properties were established and this module pins all five:

1. ``ResultMessage.usage`` covers ONLY the parent's own API requests. In the
   recording the parent's cache reads total 74,002 and the subagent's 8,197 —
   the ResultMessages report 74,002, so subagent spend is genuinely absent.
2. Subagent assistant messages ARE streamed, in the parent's session, tagged by
   ``parent_tool_use_id`` == the Task tool's ``tool_use_id``. ``session_id`` is
   NOT a usable discriminator: it is identical for parent and subagent.
3. The stream repeats each API response several times under one ``message_id``
   with a byte-identical usage block (7 assistant messages / 4 ids here).
   Across 42,925 sessions and 97,547 repeat groups the inflation is 2.00x
   median / 2.11x aggregate, and 97,546 of those groups were byte-identical:
   the lone revision moved UPWARD, none downward, which is why the ledger takes
   last-wins. Summing without dedup roughly doubles the bill.
4. Streamed ``output_tokens`` is an EARLY snapshot and never reaches its final
   value (parent streamed out=9 vs ResultMessage out=1,281). Cache reads,
   cache creation and input_tokens ARE final.
5. ``TaskNotificationMessage.usage.total_tokens`` is NOT a bill. It is
   ``(LAST request's input + cache_creation + cache_read) + SUM(output over
   every streamed occurrence, duplicates included)`` — a context-size gauge
   whose cache buckets are a final-message snapshot rather than a sum, and
   whose output term double-counts the stream's repeats. On a real 6-response
   subagent it reports 13,087 against 76,704 actually billed (17%); across
   2,962 real subagent transcripts it runs 7-12%. It never coincides with the
   true total, not even on a ONE-request subagent: there the output term is
   still an early snapshot summed over the duplicates. On the single-response
   recording it carries output=8 (4 streamed twice) where the subagent's own
   transcript records output=45. So the totals come from the deduped stream;
   the scalar is used ONLY as a labelled floor when nothing streamed at all.

``testdata/subagent_usage_stream_multi.json`` is a second live recording — a
6-response subagent — that exists specifically to cover the ~98% branch the
single-response recording cannot reach.

KNOWN RESIDUAL, pinned by ``test_the_known_residual_is_the_streamed_output``:
because streamed ``output_tokens`` is an early snapshot, subagent output is
under-recorded by roughly 0.8% of that subagent's bill. It is not recoverable
from the SDK stream — the parent transcript's Task ``toolUseResult`` has no
usage block — so these tests assert the residual rather than pretend it away.
"""

import json
from pathlib import Path

import pytest

# Exercises the REAL ClaudeBackend over the monkeypatched claude_backend.query
# seam, like tests/test_backend.py.
pytestmark = pytest.mark.real_backend

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import (
    AssistantMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
)

from no_human.agent import claude_backend
from no_human.agent.claude_backend import ClaudeBackend

FIXTURE = Path(__file__).resolve().parent.parent / "testdata" / "subagent_usage_stream.json"


def _load_recorded_stream() -> list:
    """Rebuild SDK message objects from the recorded live run."""
    msgs = []
    for rec in json.loads(FIXTURE.read_text(encoding="utf-8")):
        kind = rec["type"]
        if kind == "assistant":
            msgs.append(AssistantMessage(
                content=[TextBlock(text="")],
                model="claude-haiku-4-5-20251001",
                parent_tool_use_id=rec["parent_tool_use_id"],
                usage=rec["usage"],
                message_id=rec["message_id"],
                session_id=rec["session_id"],
            ))
        elif kind == "result":
            msgs.append(ResultMessage(
                subtype=rec["subtype"], duration_ms=0, duration_api_ms=0,
                is_error=rec["is_error"], num_turns=rec["num_turns"],
                session_id=rec["session_id"], usage=rec["usage"], result="ok",
            ))
        elif kind == "task_started":
            msgs.append(TaskStartedMessage(
                subtype="task_started", data=rec, task_id=rec["task_id"],
                description="d", uuid="u", session_id="s",
                tool_use_id=rec["tool_use_id"],
            ))
        elif kind == "task_notification":
            msgs.append(TaskNotificationMessage(
                subtype="task_notification", data=rec, task_id=rec["task_id"],
                status=rec["status"], output_file="/dev/null", summary="s",
                uuid="u", session_id="s", tool_use_id=rec["tool_use_id"],
                usage=rec["usage"],
            ))
    return msgs


def _replay(messages):
    async def _q(*args, **kwargs):
        for m in messages:
            yield m
    return _q


# ----------------------------------------------------------------------------
# Independently derived expectations.
#
# These are read STRAIGHT OFF the recorded artifact by sources that are not the
# code under test: the SDK's own ResultMessage.usage (for the parent) and the
# deduped subagent assistant messages (for the subagent). Nothing here is
# recomputed from the accounting logic being tested.
# ----------------------------------------------------------------------------
_RECORDS = json.loads(FIXTURE.read_text(encoding="utf-8"))
_RESULTS = [r["usage"] for r in _RECORDS if r["type"] == "result"]
PARENT_IN = sum(u["input_tokens"] for u in _RESULTS)                    # 35
PARENT_OUT = sum(u["output_tokens"] for u in _RESULTS)                  # 1281
PARENT_CACHE_READ = sum(u["cache_read_input_tokens"] for u in _RESULTS)      # 74002
PARENT_CACHE_CREATE = sum(u["cache_creation_input_tokens"] for u in _RESULTS)  # 7149
# The subagent's spend, deduplicated by message_id — the billing record. This
# recording's subagent made exactly ONE request (tool_uses: 0) that the stream
# repeats twice.
_SUB = {r["message_id"]: r["usage"] for r in _RECORDS
        if r["type"] == "assistant" and r["parent_tool_use_id"]}
SUBAGENT_TOTAL = sum(u["input_tokens"] + u["output_tokens"]
                     + u["cache_read_input_tokens"]
                     + u["cache_creation_input_tokens"]
                     for u in _SUB.values())                                 # 11063
# The CLI's gauge for the same subagent, for contrast only. It is NOT a bill
# and NOT the true total even at one request: 11,067 = (10 + 2,852 + 8,197)
# + SUM(output over both streamed occurrences) = 11,059 + 8, where the true
# output_tokens is 45. Never derive an expectation from this.
SUBAGENT_SCALAR = sum(r["usage"]["total_tokens"]
                      for r in _RECORDS if r["type"] == "task_notification")  # 11067
# The subagent's own transcript records in=10 out=45 cr=8,197 cc=2,852 for
# msg_TESTFIXTURE0000000000004. That file is NOT reachable from the parent's
# SDK stream, and it is NOT in this repo: it lives in a machine-local CLI
# transcript directory (~/.claude-personal/projects/-private-tmp-wt-tokens-probe/
# 00000000-.../subagents/agent-00000000000000001.jsonl — note ".claude-personal",
# not ".claude"). So this constant is TRANSCRIBED, not derived from the fixture,
# and a reader on another machine cannot re-derive it. Treat the residual test
# below as documentation of a measurement taken once, not as a live check.
#
# What IS checkable from the fixture alone, and enough on its own to establish
# the DIRECTION of the error: streamed output_tokens is an early snapshot the
# stream never revises downward, so the recorded 4 is a hard lower bound on the
# true output. The ledger therefore under-records; only the exact size of the
# gap depends on the transcribed 45.
SUBAGENT_TRUE_BILL = 10 + 45 + 8_197 + 2_852                                 # 11104
GRAND_TOTAL = (PARENT_IN + PARENT_OUT + PARENT_CACHE_READ
               + PARENT_CACHE_CREATE + SUBAGENT_TOTAL)                       # 93530


async def test_the_recording_really_does_hide_subagent_spend(tmp_path, monkeypatch):
    """Premise check: the artifact must actually exhibit the bug, or every
    other test here is vacuous. The parent's ResultMessages must exclude the
    subagent's cache reads, and the subagent must have spent something."""
    subagent_streamed_cache_read = sum(
        r["usage"]["cache_read_input_tokens"] for r in _RECORDS
        if r["type"] == "assistant" and r["parent_tool_use_id"]
    )
    assert subagent_streamed_cache_read > 0, "recording has no subagent spend to find"
    assert PARENT_CACHE_READ == sum(
        {r["message_id"]: r["usage"]["cache_read_input_tokens"] for r in _RECORDS
         if r["type"] == "assistant" and not r["parent_tool_use_id"]}.values()
    ), "ResultMessage.usage should equal the PARENT-only streamed cache reads"
    assert subagent_streamed_cache_read not in (0, PARENT_CACHE_READ)
    # Same session id for both — session_id cannot discriminate.
    sessions = {r["session_id"] for r in _RECORDS if r["type"] == "assistant"}
    assert len(sessions) == 1, "subagent shares the parent's session_id"


async def test_ledger_totals_match_the_independently_derived_grand_total(
    tmp_path, monkeypatch,
):
    """The headline: what lands in the attempt ledger must equal
    (sum of every ResultMessage) + (the deduped subagent stream)."""
    monkeypatch.setattr(claude_backend, "query", _replay(_load_recorded_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    ledger = (result.tokens_used + result.cache_read_tokens
              + result.cache_creation_tokens)
    assert ledger == GRAND_TOTAL == 93_530
    # And the split is exact, not merely the total.
    assert result.cache_read_tokens == PARENT_CACHE_READ + 8_197
    assert result.cache_creation_tokens == PARENT_CACHE_CREATE + 2_852
    assert result.tokens_used == PARENT_IN + PARENT_OUT + 14


async def test_subagent_spend_is_reported_separately_too(tmp_path, monkeypatch):
    """The rollup is auditable: the subagent share is exposed on its own so a
    reader can tell the ledger grew for a REASON, not from a summation slip."""
    monkeypatch.setattr(claude_backend, "query", _replay(_load_recorded_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    sub = (result.subagent_tokens_used + result.subagent_cache_read_tokens
           + result.subagent_cache_creation_tokens)
    assert sub == SUBAGENT_TOTAL == 11_063
    assert result.subagent_count == 1
    # Measured, not floored: the stream showed this subagent.
    assert result.subagent_floored_count == 0


async def test_every_result_message_counts_not_just_the_last(tmp_path, monkeypatch):
    """One `query()` emitted TWO successful ResultMessages (turns=3 then
    turns=1). The backend used to overwrite its totals on each one, so the
    first result's 53,821 cache reads were silently dropped. Sum, don't
    replace — with a single result this is identical to the old behaviour."""
    monkeypatch.setattr(claude_backend, "query", _replay(_load_recorded_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert len(_RESULTS) == 2, "fixture must contain the multi-result shape"
    last_only = _RESULTS[-1]["cache_read_input_tokens"]
    assert result.cache_read_tokens > last_only
    assert result.cache_read_tokens == PARENT_CACHE_READ + 8_197


# ----------------------------------------------------------------------------
# Synthetic stream with DELIBERATE duplicate message_ids — the dedup mutant is
# meant to die here, loudly, with a number that is exactly double.
# ----------------------------------------------------------------------------
def _synthetic_duplicates():
    """One parent response repeated 3x, one subagent response repeated 2x.

    Hand-computed expectation, independent of the implementation:
      parent  : ResultMessage says in 100 + out 900 + cr 5,000 + cc 300 = 6,300
      subagent: deduped stream says in 20 + out 2 + cr 2,000 + cc 150 = 2,172
      grand   : 8,472
    The 2,222 gauge on the notification is deliberately NOT 2,172: it is there
    so a test can prove the accounting ignores it.
    A naive sum would report the parent stream 3x and the subagent 2x.
    """
    usage_p = {"input_tokens": 100, "output_tokens": 7,
               "cache_read_input_tokens": 5_000, "cache_creation_input_tokens": 300}
    usage_s = {"input_tokens": 20, "output_tokens": 2,
               "cache_read_input_tokens": 2_000, "cache_creation_input_tokens": 150}
    msgs = []
    for _ in range(3):
        msgs.append(AssistantMessage(content=[TextBlock(text="")], model="m",
                                     parent_tool_use_id=None, usage=usage_p,
                                     message_id="msg_parent_1", session_id="sess"))
    msgs.append(TaskStartedMessage(subtype="task_started", data={}, task_id="t1",
                                   description="d", uuid="u", session_id="sess",
                                   tool_use_id="toolu_sub"))
    for _ in range(2):
        msgs.append(AssistantMessage(content=[TextBlock(text="")], model="m",
                                     parent_tool_use_id="toolu_sub", usage=usage_s,
                                     message_id="msg_sub_1", session_id="sess"))
    msgs.append(TaskNotificationMessage(
        subtype="task_notification", data={}, task_id="t1", status="completed",
        output_file="/dev/null", summary="s", uuid="u", session_id="sess",
        tool_use_id="toolu_sub",
        usage={"total_tokens": 2_222, "tool_uses": 1, "duration_ms": 5}))
    msgs.append(ResultMessage(
        subtype="success", duration_ms=0, duration_api_ms=0, is_error=False,
        num_turns=2, session_id="sess", result="done",
        usage={"input_tokens": 100, "output_tokens": 900,
               "cache_read_input_tokens": 5_000,
               "cache_creation_input_tokens": 300}))
    return msgs


async def test_duplicate_message_ids_are_counted_once(tmp_path, monkeypatch):
    """Repeated message_ids must not inflate the bill. This is the mutation
    target: delete the dedup and the subagent share doubles."""
    monkeypatch.setattr(claude_backend, "query", _replay(_synthetic_duplicates()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    total = (result.tokens_used + result.cache_read_tokens
             + result.cache_creation_tokens)
    assert total == 8_472, f"expected 6300 parent + 2172 subagent, got {total}"
    assert result.subagent_cache_read_tokens == 2_000, "counted the subagent twice"
    assert result.subagent_cache_creation_tokens == 150
    # From the deduped stream (20 + 2), NOT from the 2,222 gauge.
    assert result.subagent_tokens_used == 22


async def test_a_run_with_no_subagents_is_unchanged(tmp_path, monkeypatch):
    """No Task tool, no behaviour change — the rollup must be inert."""
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 700, "cache_creation_input_tokens": 60}
    msgs = [
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id=None, usage=usage,
                         message_id="msg_only", session_id="sess"),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage=usage),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.tokens_used == 15
    assert result.cache_read_tokens == 700
    assert result.cache_creation_tokens == 60
    assert result.subagent_count == 0
    assert result.subagent_tokens_used == 0


MULTI_FIXTURE = (Path(__file__).resolve().parent.parent / "testdata"
                 / "subagent_usage_stream_multi.json")


def _load_multi_stream() -> list:
    msgs = []
    for rec in json.loads(MULTI_FIXTURE.read_text(encoding="utf-8")):
        kind = rec["type"]
        if kind == "assistant":
            msgs.append(AssistantMessage(
                content=[TextBlock(text="")], model="claude-haiku-4-5-20251001",
                parent_tool_use_id=rec["parent_tool_use_id"], usage=rec["usage"],
                message_id=rec["message_id"], session_id="sess"))
        elif kind == "result":
            msgs.append(ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0,
                is_error=False, num_turns=rec["num_turns"], session_id="sess",
                usage=rec["usage"], result="ok"))
        elif kind == "task_started":
            msgs.append(TaskStartedMessage(
                subtype="task_started", data=rec, task_id="t", description="d",
                uuid="u", session_id="sess", tool_use_id=rec["tool_use_id"]))
        elif kind == "task_notification":
            msgs.append(TaskNotificationMessage(
                subtype="task_notification", data=rec, task_id="t",
                status="completed", output_file="/dev/null", summary="s",
                uuid="u", session_id="sess", tool_use_id=rec["tool_use_id"],
                usage=rec["usage"]))
    return msgs


_MULTI = json.loads(MULTI_FIXTURE.read_text(encoding="utf-8"))
_MULTI_SUB = {r["message_id"]: r["usage"] for r in _MULTI
              if r["type"] == "assistant" and r["parent_tool_use_id"]}
_MULTI_SCALAR = next(r["usage"]["total_tokens"] for r in _MULTI
                     if r["type"] == "task_notification")


async def test_a_multi_response_subagent_ignores_the_reported_scalar(
    tmp_path, monkeypatch,
):
    """The branch that carries ~98% of real subagents.

    ``TaskNotificationMessage.usage.total_tokens`` is a context-size gauge, not
    a bill: it is (LAST request's input + cache_creation + cache_read) +
    SUM(output). On this recording — a real 6-response subagent — it reports
    13,087 while the subagent actually billed 76,704, i.e. 17%. Anything that
    rebuilds spend FROM that scalar understates by ~6x, so the totals must come
    from the deduped stream and the scalar must be ignored here.
    """
    assert len(_MULTI_SUB) == 6, "fixture must be the multi-response shape"
    streamed_cr = sum(u["cache_read_input_tokens"] for u in _MULTI_SUB.values())
    streamed_cc = sum(u["cache_creation_input_tokens"] for u in _MULTI_SUB.values())
    streamed_io = sum(u["input_tokens"] + u["output_tokens"]
                      for u in _MULTI_SUB.values())
    # The premise that blocked the first attempt at this fix: the scalar is far
    # SMALLER than the cache figures it was assumed to contain.
    assert _MULTI_SCALAR < streamed_cr + streamed_cc
    assert _MULTI_SCALAR / (streamed_io + streamed_cr + streamed_cc) < 0.25

    monkeypatch.setattr(claude_backend, "query", _replay(_load_multi_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.subagent_cache_read_tokens == streamed_cr == 60_681
    assert result.subagent_cache_creation_tokens == streamed_cc == 15_966
    assert result.subagent_tokens_used == streamed_io == 57
    assert (result.subagent_tokens_used + result.subagent_cache_read_tokens
            + result.subagent_cache_creation_tokens) == 76_704


async def test_the_scalar_never_shrinks_a_multi_response_subagent(
    tmp_path, monkeypatch,
):
    """Kills any mutant that lets the gauge reduce a measured subagent.

    Historically the guard was a `len(msgs) == 1` restriction on a
    `total - cache_read - cache_creation` branch; that branch is gone, and
    nothing in the current code names such a restriction. The PROPERTY it was
    protecting is what this pins. On this fixture the arithmetic would be
    13,087 - 60,681 - 15,966 — deeply negative — and the old `total >= cr + cc`
    guard is what hid that, on 98.3% of real subagents. Whatever the
    arithmetic, the answer must not fall below the streamed in/out, which is a
    hard lower bound on what the subagent billed.
    """
    monkeypatch.setattr(claude_backend, "query", _replay(_load_multi_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.subagent_tokens_used >= 57
    assert result.subagent_tokens_used > 0


async def test_a_multi_response_subagent_ignores_the_scalar_even_when_it_fits(
    tmp_path, monkeypatch,
):
    """The mutant-killer the negative guard was hiding.

    On most real subagents `total - Sum(cr) - Sum(cc)` goes negative, so a
    guard rejecting negatives makes the wrong branch indistinguishable from the
    right one. That is precisely how the false premise survived review the
    first time. A high-output subagent breaks the tie: two responses, each
    in=10 out=5,000 cr=100 cc=50, so the CLI's gauge is
    (10+50+100) + 10,000 = 10,160 — comfortably ABOVE Sum(cr)+Sum(cc)=300.

    Correct answer, from the stream: 20 + 10,000 = 10,020.
    Scalar answer: 10,160 - 300 = 9,860.
    The scalar is no longer consulted for spend at all, so any reintroduction
    of that arithmetic — guarded or not, restricted to one response or not —
    dies here.
    """
    usage = {"input_tokens": 10, "output_tokens": 5_000,
             "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}
    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t1",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_sub"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_sub", usage=usage,
                         message_id="msg_a", session_id="sess"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_sub", usage=usage,
                         message_id="msg_b", session_id="sess"),
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="t1",
            status="completed", output_file="/dev/null", summary="s", uuid="u",
            session_id="sess", tool_use_id="toolu_sub",
            usage={"total_tokens": 10_160, "tool_uses": 1, "duration_ms": 5}),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage={"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 10,
                             "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.subagent_tokens_used == 10_020, "rebuilt in/out from the gauge"
    assert result.subagent_cache_read_tokens == 200
    assert result.subagent_cache_creation_tokens == 100


async def test_a_single_response_subagent_ignores_the_scalar_too(
    tmp_path, monkeypatch,
):
    """The 1.7% shape the deleted branch existed for.

    A single-request subagent was believed to be the one case where the gauge
    is exact — "last request" and "the only request" coincide. It is not. The
    output term is SUM(output) over every streamed occurrence of an EARLY
    snapshot, so here it is 4+4=8 against a true output_tokens of 45. The
    branch traded a false claim in source for 4 tokens (11,067 vs 11,063,
    0.036% of the bill), and neither number is the truth (11,104).
    """
    assert SUBAGENT_SCALAR == 11_067
    only = next(iter(_SUB.values()))
    # The gauge decomposes exactly as documented — last request's
    # in+cc+cr, plus output summed over BOTH streamed occurrences.
    snapshot = (only["input_tokens"] + only["cache_creation_input_tokens"]
                + only["cache_read_input_tokens"])
    dup_output = sum(r["usage"]["output_tokens"] for r in _RECORDS
                     if r["type"] == "assistant" and r["parent_tool_use_id"])
    assert snapshot == 11_059 and dup_output == 8
    assert SUBAGENT_SCALAR == snapshot + dup_output

    monkeypatch.setattr(claude_backend, "query", _replay(_load_recorded_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.subagent_tokens_used == 10 + 4, "rebuilt in/out from the gauge"


async def test_an_absurd_scalar_cannot_displace_a_measurement(tmp_path, monkeypatch):
    """The unbounded-from-above hole, closed on the path that had a measurement.

    The old code guarded the scalar from BELOW (reject if it would go negative)
    and not at all from above, so `total - cr - cc` became the in/out column
    whatever its size: a gauge of 10,000,000 against a subagent that streamed
    in=10 out=4 cr=8,197 cc=2,852 yielded 9,988,951 in/out, a 900x
    over-report that would trip any budget gate. Latent only because the CLI's
    formula bounds the real value to single digits.

    SCOPE, precisely: this is fixed for a subagent the stream SHOWED, which is
    where the bug was — the gauge displaced a real measurement. It is NOT a
    claim that the scalar is unread. It is still read on the streamed-nothing
    floor path (`test_a_subagent_the_stream_never_showed_still_counts`), and
    that path is still unbounded above: a gauge of 10,000,000,000 there lands
    10,000,000,000 in `subagent_tokens_used` verbatim, because a floor with no
    measurement to check it against has nothing to be bounded by. Capping it
    (say, at the model's context window) is a separate decision, deliberately
    not taken here.
    """
    usage_s = {"input_tokens": 10, "output_tokens": 4,
               "cache_read_input_tokens": 8_197, "cache_creation_input_tokens": 2_852}
    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t1",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_sub"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_sub", usage=usage_s,
                         message_id="msg_sub_1", session_id="sess"),
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="t1",
            status="completed", output_file="/dev/null", summary="s", uuid="u",
            session_id="sess", tool_use_id="toolu_sub",
            usage={"total_tokens": 10_000_000, "tool_uses": 0, "duration_ms": 5}),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage={"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 10,
                             "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.subagent_tokens_used == 14, "the gauge became the bill"
    assert result.subagent_cache_read_tokens == 8_197
    assert result.subagent_cache_creation_tokens == 2_852
    assert result.subagent_floored_count == 0


async def test_the_known_residual_is_the_streamed_output(tmp_path, monkeypatch):
    """Pin the undercount rather than pretend it away.

    Streamed ``output_tokens`` is an early snapshot the stream never revises,
    and the true value is not in the parent's stream anywhere: the Task
    ``toolUseResult`` carries {agentId, description, outputFile, prompt,
    resolvedModel, status} and no usage block, and the gauge is not a bill.
    The subagent's own transcript records out=45 where the stream showed 4, so
    the ledger under-records this subagent by 41 tokens — 0.37% of its bill.
    On the 6-response fixture the same effect is 633 of 77,337 (0.82%).

    TWO CLASSES OF ASSERTION BELOW, and the difference matters:
      - the DIRECTION of the error is checked against the fixture alone. The
        stream never revises output downward, so the recorded figure is a lower
        bound on the true bill, and the cache buckets — which ARE final — must
        come through untouched. This holds on any machine.
      - the SIZE of the error rests on `SUBAGENT_TRUE_BILL`, transcribed from a
        machine-local CLI transcript that is not in this repo (see its
        definition). That number cannot be re-derived here, so treat it as
        documentation of a measurement taken once, not as a live check.

    If a future SDK exposes the final figures, this test fails and should be
    replaced by one asserting the true bill. Until then it documents the gap.
    """
    monkeypatch.setattr(claude_backend, "query", _replay(_load_recorded_stream()))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    recorded = (result.subagent_tokens_used + result.subagent_cache_read_tokens
                + result.subagent_cache_creation_tokens)
    assert recorded == 11_063
    # -- direction, from the fixture alone --------------------------------
    streamed_out = next(iter(_SUB.values()))["output_tokens"]
    assert streamed_out == 4
    assert result.subagent_tokens_used == 10 + streamed_out
    # The gap is entirely output, never the cache buckets — those are final.
    assert result.subagent_cache_read_tokens == 8_197
    assert result.subagent_cache_creation_tokens == 2_852
    # -- size, from the transcribed constant ------------------------------
    assert SUBAGENT_TRUE_BILL == 11_104
    residual = SUBAGENT_TRUE_BILL - recorded
    assert residual == 41
    assert residual / SUBAGENT_TRUE_BILL < 0.01


async def test_a_scalar_below_the_streamed_cache_cannot_shrink_the_bill(
    tmp_path, monkeypatch,
):
    """A gauge smaller than the cache figures of the request it describes is
    incoherent; any arithmetic built on it would go negative and UNDERSTATE the
    bill. The stream is the only input, so the gauge cannot reach the total."""
    usage_s = {"input_tokens": 20, "output_tokens": 2,
               "cache_read_input_tokens": 9_000, "cache_creation_input_tokens": 500}
    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t1",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_sub"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_sub", usage=usage_s,
                         message_id="msg_sub_1", session_id="sess"),
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="t1",
            status="completed", output_file="/dev/null", summary="s", uuid="u",
            session_id="sess", tool_use_id="toolu_sub",
            usage={"total_tokens": 300, "tool_uses": 0, "duration_ms": 5}),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage={"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 10,
                             "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    # 300 - 9000 - 500 would be -9,200. The streamed in/out is the answer.
    assert result.subagent_tokens_used == 22
    assert result.subagent_cache_read_tokens == 9_000


async def test_a_revised_repeat_wins_over_the_earlier_partial(tmp_path, monkeypatch):
    """Kills the last-wins -> first-wins mutant. Across 42,925 sessions and
    97,547 repeat groups, one group carried a revision and it moved UPWARD (an
    early partial later completed). Zero moved down, so the later record is the
    fuller one and must replace the earlier."""
    early = {"input_tokens": 5, "output_tokens": 1,
             "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10}
    revised = {"input_tokens": 5, "output_tokens": 900,
               "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10}
    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t1",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_sub"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_sub", usage=early,
                         message_id="msg_sub_1", session_id="sess"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_sub", usage=early,
                         message_id="msg_sub_2", session_id="sess"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_sub", usage=revised,
                         message_id="msg_sub_1", session_id="sess"),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage={"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 10,
                             "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    # Two distinct ids: the revised msg_sub_1 (905) plus msg_sub_2 (6).
    assert result.subagent_tokens_used == 905 + 6, "first-wins kept the partial"


async def test_an_all_zero_repeat_never_erases_a_measurement(tmp_path, monkeypatch):
    """Last-wins must not let an empty-but-present usage block zero out a real
    one. Never observed in 97,547 repeat groups — guarded because the cost of
    the guard is one `any()` and the cost of the hole is silent free spend."""
    real = {"input_tokens": 5, "output_tokens": 30,
            "cache_read_input_tokens": 4_000, "cache_creation_input_tokens": 20}
    zeros = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t1",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_sub"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_sub", usage=real,
                         message_id="msg_sub_1", session_id="sess"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_sub", usage=zeros,
                         message_id="msg_sub_1", session_id="sess"),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage={"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 10,
                             "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.subagent_cache_read_tokens == 4_000
    assert result.subagent_tokens_used == 35


async def test_a_subagent_the_stream_never_showed_still_counts(tmp_path, monkeypatch):
    """The SDK warns that some tasks report only a terminal TaskUpdatedMessage
    and may emit no assistant messages we can see. When only the gauge arrives
    it is the sole signal, so bank it — but as a FLOOR, not as truth: the gauge
    runs 7-17% of real spend, so this case is still a large undercount. It is
    banked as non-cache tokens (the dearest bucket) so a gate whose job is to
    stop runaway spend errs toward flagging on a number known to be too low.

    The alternative — dropping it with the single-response branch — would
    record ZERO for such a subagent, which is the "subagents are free" bug this
    module exists to end. So it survives, and the word FLOOR is carried by a
    DATUM (`subagent_floored_count`), not by a comment: a surface reporting
    subagent spend can say "1 of 1 is a floor" instead of presenting a known
    undercount as a total.
    """
    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t9",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_ghost"),
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="t9",
            status="completed", output_file="/dev/null", summary="s", uuid="u",
            session_id="sess", tool_use_id="toolu_ghost",
            usage={"total_tokens": 4_444, "tool_uses": 1, "duration_ms": 5}),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage={"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 10,
                             "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")

    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.subagent_tokens_used == 4_444
    assert result.subagent_cache_read_tokens == 0
    assert (result.tokens_used + result.cache_read_tokens
            + result.cache_creation_tokens) == 2 + 10 + 4_444
    # The label, on a datum a surface can read.
    assert result.subagent_count == 1
    assert result.subagent_floored_count == 1


async def test_the_floor_path_is_unbounded_above_and_that_is_recorded(
    tmp_path, monkeypatch,
):
    """The honest counterpart to `test_an_absurd_scalar_cannot_displace_a_
    measurement`, kept executable so the caveat cannot rot into prose.

    The scalar IS still read for spend — on this one path — and here it has no
    upper bound: with no streamed measurement to check it against, there is
    nothing to bound it BY. A gauge of 10,000,000,000 lands verbatim in
    `subagent_tokens_used` and in the ledger. That is deliberate, not an
    oversight: the alternative on this path is recording zero.

    A sanity cap (reject a gauge above the model's context window, record zero
    plus the flag) is a real option and a SEPARATE decision. If it is taken,
    this test is what must change, and `subagent_floored_count` is already the
    channel for saying which subagents it fired on.
    """
    absurd = 10_000_000_000
    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t9",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_ghost"),
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="t9",
            status="completed", output_file="/dev/null", summary="s", uuid="u",
            session_id="sess", tool_use_id="toolu_ghost",
            usage={"total_tokens": absurd, "tool_uses": 1, "duration_ms": 5}),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage={"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 10,
                             "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")
    result = await backend.run("go", cwd=tmp_path, max_turns=10)

    assert result.subagent_tokens_used == absurd, "the floor is NOT capped"
    assert (result.tokens_used + result.cache_read_tokens
            + result.cache_creation_tokens) == absurd + 12
    # And it is labelled, which is what makes the unbounded number readable
    # rather than silently authoritative.
    assert result.subagent_floored_count == 1


async def test_the_floor_label_rides_the_result_event_not_just_the_result(
    tmp_path, monkeypatch,
):
    """`subagent_floored_count` must reach the orchestrator's event stream —
    that stream is the audit trail (no DB column reads these back), so a field
    only on AgentResult would be invisible to every surface."""
    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t9",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_ghost"),
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="t9",
            status="completed", output_file="/dev/null", summary="s", uuid="u",
            session_id="sess", tool_use_id="toolu_ghost",
            usage={"total_tokens": 4_444, "tool_uses": 1, "duration_ms": 5}),
        # A second subagent that DID stream, so the count is a ratio, not a flag.
        TaskStartedMessage(subtype="task_started", data={}, task_id="t8",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_seen"),
        AssistantMessage(content=[TextBlock(text="")], model="m",
                         parent_tool_use_id="toolu_seen",
                         usage={"input_tokens": 5, "output_tokens": 3,
                                "cache_read_input_tokens": 90,
                                "cache_creation_input_tokens": 7},
                         message_id="msg_seen", session_id="sess"),
        ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                      is_error=False, num_turns=1, session_id="sess", result="k",
                      usage={"input_tokens": 1, "output_tokens": 1,
                             "cache_read_input_tokens": 10,
                             "cache_creation_input_tokens": 0}),
    ]
    monkeypatch.setattr(claude_backend, "query", _replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")

    results = [ev async for ev in backend.stream("go", cwd=tmp_path, max_turns=10)
               if ev.kind == "result"]

    assert len(results) == 1
    meta = results[0].meta
    assert meta["subagent_count"] == 2
    assert meta["subagent_floored_count"] == 1
    assert meta["subagent_tokens_used"] == 4_444 + 8


async def test_the_floor_label_survives_a_mid_stream_failure(tmp_path, monkeypatch):
    """The ERROR result path carries the label too, and nothing else pinned it.

    `stream()` builds the result meta twice: once on ResultMessage and once in
    the `except` handler, which is the ONLY event a run that dies mid-flight
    keeps. Deleting `subagent_floored_count` from the error meta alone left the
    whole module green, so the label was mutation-survivable exactly where it
    matters most: an aborted attempt is the case where spend is least certain,
    and a floored subagent would have reported zero floors there.
    """
    def _raising_replay(messages):
        async def _q(*args, **kwargs):
            for m in messages:
                yield m
            raise RuntimeError("Stream closed unexpectedly")
        return _q

    msgs = [
        TaskStartedMessage(subtype="task_started", data={}, task_id="t9",
                           description="d", uuid="u", session_id="sess",
                           tool_use_id="toolu_ghost"),
        TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="t9",
            status="completed", output_file="/dev/null", summary="s", uuid="u",
            session_id="sess", tool_use_id="toolu_ghost",
            usage={"total_tokens": 4_444, "tool_uses": 1, "duration_ms": 5}),
    ]
    monkeypatch.setattr(claude_backend, "query", _raising_replay(msgs))
    backend = ClaudeBackend(model="claude-sonnet-5")

    results = [ev async for ev in backend.stream("go", cwd=tmp_path, max_turns=10)
               if ev.kind == "result"]

    assert len(results) == 1, "the except handler must still emit a result"
    meta = results[0].meta
    assert meta["is_error"] is True
    assert meta["subagent_count"] == 1
    assert meta["subagent_floored_count"] == 1, "error meta dropped the label"
    assert meta["subagent_tokens_used"] == 4_444

    # And it reaches AgentResult through run(), not only the raw event.
    monkeypatch.setattr(claude_backend, "query", _raising_replay(msgs))
    result = await ClaudeBackend(model="claude-sonnet-5").run(
        "go", cwd=tmp_path, max_turns=10)
    assert result.is_error is True
    assert result.subagent_floored_count == 1


async def test_the_mid_attempt_usage_events_are_deduped_too(tmp_path, monkeypatch):
    """The orchestrator's in-flight budget watch adds up every `usage` event it
    sees. The backend emitted one per assistant message, duplicates included,
    so the watch ran ~2.3x hot and could abort a healthy attempt early. Emit
    one event per message_id."""
    monkeypatch.setattr(claude_backend, "query", _replay(_synthetic_duplicates()))
    backend = ClaudeBackend(model="claude-sonnet-5")

    events = []
    async for ev in backend.stream("go", cwd=tmp_path, max_turns=10):
        if ev.kind == "usage":
            events.append(ev)

    assert len(events) == 2, f"one per unique message_id, got {len(events)}"
    watched = sum(e.meta["tokens_used"] + e.meta["cache_read_tokens"]
                  for e in events)
    assert watched == (107 + 5_000) + (22 + 2_000)
