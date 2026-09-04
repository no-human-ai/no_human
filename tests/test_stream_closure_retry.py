"""A nested Agent SDK session that dies in the TRANSPORT is infra, not a defect.

THE INCIDENT these guard, in the operator's own words (`~/.no_human/config.yaml`,
2026-07-11): "3-way parallel + the outer agent session crashed the reviewer's
nested Agent-SDK subprocess ('Stream closed')". The pool was dropped to one
worker and has stayed there since.

WHAT THE EVIDENCE ACTUALLY SUPPORTS, so these tests are not read as proving more
than they do. The only contemporaneous record of the incident is a
`cancel_requested` event the operator wrote at 05:40 that day, which attributes
it to "this session's subscription saturation", and the reviewer is recorded
passing normally on the same day both before and after. Nothing in the database
records how many sessions were live at any failure, because nothing ever wrote
that down. So these tests do NOT assert a root cause. They assert the three
properties whose absence is why the root cause could not be established:

  1. a transport death is retried exactly once, never silently;
  2. the failure that survives the retry NAMES the worker and the concurrency;
  3. it routes as infra (transient, non-learnable), not as a task failure.

The first test is reproduce-shaped: real subprocesses, real SDK transport, N
concurrent sessions against a stub `claude` binary. No LLM is involved anywhere
in this file.
"""

import asyncio
import json
import os
import stat
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# The whole module drives the REAL ClaudeBackend — over a stub binary in one
# test and over a monkeypatched `query` seam in the rest.
pytestmark = pytest.mark.real_backend

from claude_agent_sdk import ResultMessage

from no_human.agent import claude_backend
from no_human.agent.claude_backend import (
    TRANSPORT_DIAGNOSIS_MARKER,
    AgentEvent,
    AgentResult,
    ClaudeBackend,
    is_transport_failure,
)
from no_human.agent.worker_context import (
    WorkerContext,
    current_worker_context,
    describe_concurrency,
    set_worker_context,
)

# A stub `claude` that speaks just enough of the SDK's stream-json stdio
# protocol: answer the initialize control request, then answer the user turn.
# Exactly ONE of the concurrent sessions reports the CLI's own "Stream closed"
# wording on an errored result — the observed shape — and every other one
# succeeds. So a backend that retries once recovers, and one that does not,
# does not.
#
# TWO SEPARATE FILES, and the split is the point. Counting sessions is an
# append of one byte (`O_APPEND` writes never interleave, so the file's FINAL
# size is an exact total). Electing the victim is `O_CREAT | O_EXCL`, which
# exactly one process can win. The first draft used the byte count for BOTH —
# "die if I am session 1" — and flaked at about 1 run in 2: appending and then
# calling `getsize` is not one atomic step, so under four concurrent stubs all
# four could append first and then all read 4, and nobody died. A test whose
# failure injection silently does not happen is worse than no test.
_STUB = '''#!/usr/bin/env python3
import json, os, sys
COUNTER = os.environ["STUB_COUNTER"]
ELECT_ONE_VICTIM = os.environ.get("STUB_ELECT_ONE_VICTIM") == "1"
FAIL_ALL = os.environ.get("STUB_FAIL_ALL") == "1"

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def count_session():
    fd = os.open(COUNTER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, b"x")
    finally:
        os.close(fd)

def win_the_victim_slot():
    try:
        os.close(os.open(COUNTER + ".victim",
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        return True
    except FileExistsError:
        return False

if "--version" in sys.argv:
    print("0.0.0-stub (Claude Code)")
    sys.exit(0)

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    if msg.get("type") == "control_request":
        emit({"type": "control_response",
              "response": {"subtype": "success",
                           "request_id": msg["request_id"],
                           "response": {"commands": [], "output_style": "default"}}})
        continue
    if msg.get("type") == "user":
        count_session()
        died = FAIL_ALL or (ELECT_ONE_VICTIM and win_the_victim_slot())
        sid = "stub-%d" % os.getpid()
        emit({"type": "assistant",
              "message": {"id": "msg_%s" % sid, "role": "assistant", "model": "stub",
                          "content": [{"type": "text", "text": sid}],
                          "usage": {"input_tokens": 10, "output_tokens": 5}},
              "session_id": sid})
        emit({"type": "result", "subtype": "success",
              "duration_ms": 1, "duration_api_ms": 1,
              "is_error": bool(died), "num_turns": 1, "session_id": sid,
              "result": ("Stream closed unexpectedly" if died
                         else "SESSION OK %s" % sid),
              "usage": {"input_tokens": 10, "output_tokens": 5}})
        sys.exit(0)
'''


@pytest.fixture
def stub_cli(tmp_path, monkeypatch):
    """Install a stub `claude` binary as the SDK's CLI, and return its counter.

    The SDK resolves a BUNDLED binary (`claude_agent_sdk/_bundled/claude`)
    BEFORE it ever consults PATH — verified live: prepending a stub directory
    to PATH ran the real 244MB bundled CLI instead. So the seam has to be
    `_find_bundled_cli`, not the environment.
    """
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    path = tmp_path / "claude"
    path.write_text(_STUB.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    counter = tmp_path / "sessions"
    monkeypatch.setenv("STUB_COUNTER", str(counter))
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    monkeypatch.setattr(
        SubprocessCLITransport, "_find_bundled_cli", lambda self: str(path))
    # The retry's real 5s pause is a production choice, not a test property.
    monkeypatch.setattr(claude_backend, "_TRANSPORT_RETRY_DELAY_S", 0.0)
    return counter


def _sessions(counter: Path) -> int:
    return counter.stat().st_size if counter.exists() else 0


# --------------------------------------------------------------------------- #
# 1. Reproduce-shaped: N real concurrent subprocesses, one stream closure.     #
# --------------------------------------------------------------------------- #


async def test_concurrent_sessions_one_stream_closure_is_retried_once(
        stub_cli, tmp_path, monkeypatch):
    """Four concurrent ClaudeBackend sessions — the incident's width (3 workers
    + the operator's own outer session). One of them gets "Stream closed"; it
    must be retried exactly once and recover, and the other three must be
    untouched.

    This is the shape the pool was switched off for, run for real: four
    `anyio.open_process` children on one event loop, four SDK transports, four
    stdio protocol handshakes. It also pins the negative result that made the
    subprocess-contention hypothesis unusable — the Python side handles this
    width cleanly, so a stream closure here is not manufactured by concurrency
    in *our* process.
    """
    monkeypatch.setenv("STUB_ELECT_ONE_VICTIM", "1")
    set_worker_context(WorkerContext(worker="w0", inflight=4, max_workers=4))

    async def one(i):
        return await ClaudeBackend(model="stub", readonly=True).run(
            "hello", cwd=tmp_path, max_turns=1)

    results = await asyncio.gather(*(one(i) for i in range(4)))

    # 4 sessions requested, 1 died, 1 retry -> 5 stub invocations. Not 4 (no
    # retry happened) and not 6+ (it retried more than once).
    assert _sessions(stub_cli) == 5, _sessions(stub_cli)
    assert all(not r.is_error for r in results), [r.final_text for r in results]
    # The retried worker's spend is BOTH sessions, not just the surviving one.
    assert sorted(r.tokens_used for r in results) == [15, 15, 15, 30]


async def test_concurrency_is_named_when_the_retry_also_dies(
        stub_cli, tmp_path, monkeypatch):
    """Two stream closures in a row: the error a human reads must say WHICH
    worker died and HOW MANY were in flight. That datum existed nowhere before
    — which is exactly why the 2026-07-11 incident could only be *asserted* to
    be about parallelism."""
    monkeypatch.setenv("STUB_FAIL_ALL", "1")
    set_worker_context(WorkerContext(worker="a1b2c3d4", inflight=3,
                                     max_workers=4))

    result = await ClaudeBackend(model="stub", readonly=True).run(
        "hello", cwd=tmp_path, max_turns=1)

    assert result.is_error is True
    assert _sessions(stub_cli) == 2          # tried once, retried once, stopped
    assert "a1b2c3d4" in result.final_text
    assert "3 of 4" in result.final_text
    assert TRANSPORT_DIAGNOSIS_MARKER in result.final_text
    # The CLI's OWN wording survives, first and unaltered — `_classify_error`
    # reads it and must still answer "infra".
    from no_human.core.orchestrator import _classify_error
    assert result.final_text.startswith("Stream closed")
    assert _classify_error(result.stop_reason, result.final_text) == "infra"


# --------------------------------------------------------------------------- #
# 2. Bounds and scope of the retry (fast seam: monkeypatched `query`).         #
# --------------------------------------------------------------------------- #


def _query_yielding(*results):
    """A fake SDK `query` that yields one canned ResultMessage per call."""
    calls = {"n": 0}

    async def _q(*args, **kwargs):
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        yield results[i]

    return _q, calls


def _rm(is_error, text, tokens=(10, 5)):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=is_error, num_turns=1, session_id="s",
        result=text,
        usage={"input_tokens": tokens[0], "output_tokens": tokens[1]},
    )


async def test_the_retry_is_never_more_than_once(tmp_path, monkeypatch):
    """Constraint #5's bounded-loop idiom applied here: a wall that is still a
    wall on the second try is escalated, not hammered. Three sessions would be
    a silent third bill on a subscription the failure may already have
    saturated."""
    q, calls = _query_yielding(_rm(True, "Stream closed unexpectedly"))
    monkeypatch.setattr(claude_backend, "query", q)
    monkeypatch.setattr(claude_backend, "_TRANSPORT_RETRY_DELAY_S", 0.0)

    result = await ClaudeBackend(model="m").run("go", cwd=tmp_path, max_turns=3)

    assert calls["n"] == 2, calls["n"]
    assert result.is_error is True


async def test_a_real_task_failure_is_never_retried(tmp_path, monkeypatch):
    """The retry is for a dead socket, not for a failing task. Retrying a
    refusal, a max_turns exhaustion or a failing test run would double the
    spend on work that is going to fail identically — and would quietly give
    the coder a fourth attempt inside a max_attempts=3 bound."""
    for text in ("tests failed: 3 passed, 2 failed",
                 "Reached the maximum number of turns",
                 "You've hit your monthly spend limit"):
        q, calls = _query_yielding(_rm(True, text))
        monkeypatch.setattr(claude_backend, "query", q)
        result = await ClaudeBackend(model="m").run(
            "go", cwd=tmp_path, max_turns=3)
        assert calls["n"] == 1, (text, calls["n"])
        assert result.final_text.startswith(text[:20])
        assert TRANSPORT_DIAGNOSIS_MARKER not in result.final_text


async def test_a_successful_run_is_returned_untouched(tmp_path, monkeypatch):
    q, calls = _query_yielding(_rm(False, "all good"))
    monkeypatch.setattr(claude_backend, "query", q)

    result = await ClaudeBackend(model="m").run("go", cwd=tmp_path, max_turns=3)

    assert calls["n"] == 1
    assert result.is_error is False
    assert result.final_text == "all good"


async def test_the_retry_is_never_silent(tmp_path, monkeypatch):
    """A retry nobody can see is indistinguishable from a flaky product. Both
    the retry and the give-up ride the caller's own event stream, which is what
    `nh watch`, the scheduler's event log and the DB all read."""
    q, _ = _query_yielding(_rm(True, "Stream closed unexpectedly"))
    monkeypatch.setattr(claude_backend, "query", q)
    monkeypatch.setattr(claude_backend, "_TRANSPORT_RETRY_DELAY_S", 0.0)
    seen = []

    await ClaudeBackend(model="m").run(
        "go", cwd=tmp_path, max_turns=3, on_event=seen.append)

    kinds = [e.kind for e in seen]
    assert "transport_retry" in kinds
    assert "transport_failed" in kinds
    retry = next(e for e in seen if e.kind == "transport_retry")
    assert retry.meta["of"] == 1
    assert "worker" in retry.meta["concurrency"] or "no worker context" in \
        retry.meta["concurrency"]


async def test_the_retry_reason_survives_a_wrapped_first_line(
        tmp_path, monkeypatch):
    """The operator-visible half of the de-wrapping fix.

    The reason was `splitlines()[0]`, so the very shape the retry now catches
    rendered as `died in the transport (Stream)` — a word, not a cause. It
    goes into the event text, the event meta, and from there into `nh watch`
    and the event log, which is where someone reconstructs an incident.
    """
    q, _ = _query_yielding(_rm(True, "Stream\nclosed unexpectedly by consumer"))
    monkeypatch.setattr(claude_backend, "query", q)
    monkeypatch.setattr(claude_backend, "_TRANSPORT_RETRY_DELAY_S", 0.0)
    seen = []

    await ClaudeBackend(model="m").run(
        "go", cwd=tmp_path, max_turns=3, on_event=seen.append)

    retry = next(e for e in seen if e.kind == "transport_retry")
    assert retry.meta["reason"] == "Stream closed unexpectedly by consumer"
    assert "Stream closed unexpectedly by consumer" in retry.text
    # The control on the control: not merely "longer than one word".
    assert retry.meta["reason"] != "Stream"


async def test_the_dead_sessions_spend_is_not_lost(tmp_path, monkeypatch):
    """A retried run is TWO bills. `run()` returns one AgentResult and that is
    what reaches `attempts.tokens_used`, so a retry that reset the ledger would
    make this change look free while the real bill went up."""
    q, _ = _query_yielding(
        _rm(True, "Stream closed unexpectedly", tokens=(100, 20)),
        _rm(False, "recovered", tokens=(7, 3)),
    )
    monkeypatch.setattr(claude_backend, "query", q)
    monkeypatch.setattr(claude_backend, "_TRANSPORT_RETRY_DELAY_S", 0.0)

    result = await ClaudeBackend(model="m").run("go", cwd=tmp_path, max_turns=3)

    assert result.is_error is False
    assert result.tokens_used == 130          # 120 burned + 10 recovered
    assert result.output_tokens == 23         # 20 + 3
    # ...but the STRUCTURE stays the surviving session's. A combined turn count
    # would describe a session that never existed, and it is what broke
    # `test_the_floor_label_survives_a_mid_stream_failure` when the first draft
    # folded cardinality too.
    assert result.num_turns == 1
    assert result.subagent_count == 0


async def test_output_tokens_none_is_not_turned_into_a_measurement(
        tmp_path, monkeypatch):
    """None means "no usage block was ever seen" and must stay distinguishable
    from a measured zero all the way to the SQL column. Folding a dead
    session's spend must not assert a number nobody measured."""
    dead = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=0, session_id="s", result="Stream closed unexpectedly",
        usage=None,
    )
    live = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=0, session_id="s", result="Stream closed unexpectedly",
        usage=None,
    )
    q, _ = _query_yielding(dead, live)
    monkeypatch.setattr(claude_backend, "query", q)
    monkeypatch.setattr(claude_backend, "_TRANSPORT_RETRY_DELAY_S", 0.0)

    result = await ClaudeBackend(model="m").run("go", cwd=tmp_path, max_turns=3)

    assert result.output_tokens is None


# --------------------------------------------------------------------------- #
# 3. The worker context: per-task, never last-writer-wins.                     #
# --------------------------------------------------------------------------- #


async def test_each_pool_worker_sees_its_own_context_not_its_neighbours(
        tmp_path):
    """The pool is N coroutines on ONE event loop, so the obvious
    implementation — a module global set at dispatch — is last-writer-wins and
    would name the WRONG worker in the one report that exists to name the right
    one. A ContextVar is copied into each task at creation; this pins that.
    """
    seen = {}

    async def worker(i):
        set_worker_context(WorkerContext(worker=f"w{i}", inflight=4,
                                         max_workers=4))
        await asyncio.sleep(0.01 * (4 - i))   # finish in reverse order
        seen[i] = describe_concurrency()

    await asyncio.gather(*(asyncio.create_task(worker(i)) for i in range(4)))

    assert [seen[i] for i in range(4)] == [
        f"worker w{i}, dispatched at 4 of 4 pool slot(s) busy" for i in range(4)
    ]


def test_an_unattributed_session_says_so_rather_than_claiming_one_worker():
    """"No context recorded" and "one worker" are different facts and only one
    of them exonerates parallelism. An empty string here would read as the
    second."""
    set_worker_context(None)
    assert current_worker_context() is None
    described = describe_concurrency()
    assert described
    assert "no worker context" in described


async def test_the_scheduler_binds_the_context_it_dispatched_into(tmp_path):
    """The producer half. `Scheduler.tick` reserves the id in `_inflight`
    BEFORE scheduling, so a worker's own dispatch is included in the count and
    a lone worker reads 1 of 1."""
    from no_human.core.scheduler import Scheduler

    captured = {}

    class _Orch:
        _sink = None

        async def run_task(self, task):
            captured["ctx"] = current_worker_context()
            return None

    class _Task:
        id = "abcdef1234567890"

    class _Store:
        async def save_events(self, *a, **k):
            return None

        async def set_status(self, *a, **k):
            return None

    sched = Scheduler(_Store(), lambda task=None: _Orch(), max_workers=3)
    sched._inflight.add(_Task.id)
    set_worker_context(None)
    await sched._run(_Task())

    assert captured["ctx"] == WorkerContext(
        worker="abcdef12", inflight=1, max_workers=3)


# --------------------------------------------------------------------------- #
# 4. It routes as infra, not as a task failure.                                #
# --------------------------------------------------------------------------- #


def test_is_transport_failure_only_fires_on_an_errored_transport_death():
    def r(is_error, text):
        return AgentResult(final_text=text, num_turns=1, is_error=is_error,
                           tokens_used=0, session_id=None, stop_reason=None)

    assert is_transport_failure(r(True, "Stream closed unexpectedly"))
    assert is_transport_failure(r(True, "API connection error"))
    # A SUCCESSFUL run whose prose merely mentions the phrase is not a
    # transport death — this codebase is full of stream-handling code, and the
    # same mistake in `_quota_signal` once parked healthy tasks as PAUSED_QUOTA.
    assert not is_transport_failure(r(False, "Added Stream closed handling"))
    assert not is_transport_failure(r(True, "3 tests failed"))
    # A hang is deliberately NOT ours: `review/reviewer.py` already bounds it
    # with asyncio.wait_for, twice, and retrying here would double the
    # wall-clock a task sits wedged.
    assert not is_transport_failure(r(True, "reviewer timed out after 600s"))


async def test_the_reviewer_carries_the_transport_reason_out_verbatim(
        tmp_path, monkeypatch):
    """`_review_once` used to flatten every errored session to "reviewer
    session error (error)" — a string that names no cause, which the
    escalation, the blocker category and the human all inherit."""
    from no_human.review.reviewer import AdversarialReviewer, ReviewerUnavailable

    class _DeadBackend:
        model = "claude-opus-5"

        async def run(self, prompt, **kwargs):
            return AgentResult(
                final_text=("Stream closed unexpectedly\n\n"
                            f"{TRANSPORT_DIAGNOSIS_MARKER} ... worker w7, "
                            "dispatched at 3 of 4 pool slot(s) busy."),
                num_turns=0, is_error=True, tokens_used=0,
                session_id=None, stop_reason="error")

    reviewer = AdversarialReviewer(backend=_DeadBackend())
    with pytest.raises(ReviewerUnavailable) as exc:
        await reviewer._agent_review("prompt", tmp_path)

    assert TRANSPORT_DIAGNOSIS_MARKER in str(exc.value)
    assert "worker w7" in str(exc.value)
    assert "3 of 4" in str(exc.value)


async def test_a_dead_reviewer_transport_escalates_as_transient_infra():
    """NOVEL_UNKNOWN is not in `learning.queue.NON_LEARNABLE_CATEGORIES`, so
    routing a dead socket there proposed it as a durable code lesson into the
    human's confirm queue, and its route does not auto-retry. TRANSIENT_INFRA
    is non-learnable and auto-retrying, which is what a dead socket deserves.
    """
    from no_human.blockers.taxonomy import BlockerCategory
    from no_human.core.orchestrator import Orchestrator
    from no_human.learning.queue import NON_LEARNABLE_CATEGORIES

    raised = {}

    class _Task:
        id = "t1"
        title = "do a thing"

    orch = Orchestrator.__new__(Orchestrator)

    async def _raise_blocker(task, blocker, **kwargs):
        raised["blocker"] = blocker
        # The real `_raise_blocker` always returns a TaskOutcome (the caller
        # reads `.status` to decide whether a quota park needs its wall clock
        # and profile stamped); a `None` here misstates that contract.
        from no_human.core.orchestrator import TaskOutcome
        from no_human.core.task import TaskStatus
        return TaskOutcome(task=task, status=TaskStatus.BLOCKED)

    orch._raise_blocker = _raise_blocker

    detail = ("the reviewer reached no verdict after 2 rounds (reviewer "
              "session transport failure — Stream closed unexpectedly\n\n"
              f"{TRANSPORT_DIAGNOSIS_MARKER} ... worker w7, dispatched at 3 of "
              "4 pool slot(s) busy.)")
    await orch._escalate_reviewer_unavailable(_Task(), detail)

    blocker = raised["blocker"]
    assert blocker.category == BlockerCategory.TRANSIENT_INFRA
    assert blocker.transient is True
    assert blocker.category.value in NON_LEARNABLE_CATEGORIES
    assert "worker w7" in blocker.evidence


async def test_the_transport_marker_survives_the_reviewers_tail_window(
        stub_cli, tmp_path, monkeypatch):
    """🔴 TWO FILES, A NUMBER IN EACH, AND 96 CHARACTERS OF MARGIN BETWEEN THEM.

    `claude_backend` APPENDS its transport diagnosis to the dead session's
    `final_text`, opening with `TRANSPORT_DIAGNOSIS_MARKER`. `reviewer.py`
    carries only the LAST `_TRANSPORT_TAIL_CHARS` of that text out as the
    escalation reason. `orchestrator._escalate_reviewer_unavailable` then
    matches the marker to route the failure as TRANSIENT_INFRA.

    So the diagnosis must stay SHORTER than the window, and nothing said so:
    no test, no comment, no shared constant. Measured here at 504 against 600.
    If the diagnosis grows past the window the marker drops out of the slice in
    silence — the escalation takes the `_escalate` -> `fallback_blocker` branch,
    the dead socket is re-classified NOVEL_UNKNOWN (learnable, no auto-retry),
    and `root_cause_hypothesis` publishes 600 characters of the reviewer
    model's own `final_text`, plain and unattributed, to a human.

    The diagnosis is built by the REAL backend path rather than re-spelled
    here, because a re-spelling is a second copy that cannot go stale in step
    with the first — which is the whole defect.
    """
    from no_human.blockers.taxonomy import BlockerCategory
    from no_human.core.orchestrator import Orchestrator
    from no_human.review.reviewer import _TRANSPORT_TAIL_CHARS

    monkeypatch.setenv("STUB_FAIL_ALL", "1")
    set_worker_context(WorkerContext(worker="a1b2c3d4", inflight=3,
                                     max_workers=4))
    result = await ClaudeBackend(model="stub", readonly=True).run(
        "hello", cwd=tmp_path, max_turns=1)

    # The producer's half, measured rather than asserted about: how far from
    # the end of the text does the marker sit?
    text = (result.final_text or "").strip()
    assert TRANSPORT_DIAGNOSIS_MARKER in text
    from_end = len(text) - text.index(TRANSPORT_DIAGNOSIS_MARKER)
    assert from_end <= _TRANSPORT_TAIL_CHARS, (
        f"the transport diagnosis is {from_end} characters, and the reviewer "
        f"carries out only the last {_TRANSPORT_TAIL_CHARS} — the marker no "
        f"longer survives the slice, so a dead socket now escalates as "
        f"NOVEL_UNKNOWN and publishes the reviewer's raw text as a root cause")

    # …and the consumer's, driven end to end through the real slice and the
    # real escalation, so the pin is on the BEHAVIOUR and not on two numbers.
    tail = text[-_TRANSPORT_TAIL_CHARS:]
    raised = {}

    class _Task:
        id = "t1"
        title = "do a thing"

    orch = Orchestrator.__new__(Orchestrator)

    async def _raise_blocker(task, blocker, **kwargs):
        raised["blocker"] = blocker
        # The real `_raise_blocker` always returns a TaskOutcome (the caller
        # reads `.status` to decide whether a quota park needs its wall clock
        # and profile stamped); a `None` here misstates that contract.
        from no_human.core.orchestrator import TaskOutcome
        from no_human.core.task import TaskStatus
        return TaskOutcome(task=task, status=TaskStatus.BLOCKED)

    orch._raise_blocker = _raise_blocker
    await orch._escalate_reviewer_unavailable(
        _Task(), f"reviewer session transport failure — {tail}")

    assert raised["blocker"].category == BlockerCategory.TRANSIENT_INFRA, (
        "the marker fell out of the reviewer's tail window, so the dead "
        "socket routed as a task failure")


async def test_a_reviewer_that_merely_reached_no_verdict_stays_novel_unknown():
    """The control. Without it the test above passes for a function that routes
    EVERYTHING to TRANSIENT_INFRA — which would make a genuinely stuck reviewer
    park-and-auto-retry forever instead of reaching a human."""
    from no_human.blockers.taxonomy import BlockerCategory
    from no_human.core.orchestrator import Orchestrator

    raised = {}

    class _Task:
        id = "t1"
        title = "do a thing"

    orch = Orchestrator.__new__(Orchestrator)

    async def _raise_blocker(task, blocker, **kwargs):
        raised["blocker"] = blocker
        # The real `_raise_blocker` always returns a TaskOutcome (the caller
        # reads `.status` to decide whether a quota park needs its wall clock
        # and profile stamped); a `None` here misstates that contract.
        from no_human.core.orchestrator import TaskOutcome
        from no_human.core.task import TaskStatus
        return TaskOutcome(task=task, status=TaskStatus.BLOCKED)

    orch._raise_blocker = _raise_blocker
    orch._escalate = lambda task, detail, **kw: _raise_blocker(
        task,
        __import__("no_human.blockers.report", fromlist=["fallback_blocker"])
        .fallback_blocker(detail, goal=task.title))

    await orch._escalate_reviewer_unavailable(
        _Task(), "the reviewer reached no verdict after 2 rounds "
                 "(no REVIEW_JSON block)")

    assert raised["blocker"].category == BlockerCategory.NOVEL_UNKNOWN


def test_the_marker_the_orchestrator_matches_is_the_one_the_backend_writes():
    """Producer and consumer share ONE constant. A literal repeated in two
    files is the shape that silently stops matching."""
    src = Path(claude_backend.__file__).read_text()
    assert f'f"{{TRANSPORT_DIAGNOSIS_MARKER}}' in src, (
        "the diagnosis must interpolate the constant, not re-spell it")
    from no_human.core import orchestrator
    assert orchestrator._TRANSPORT_BLOCKER_MARKER is TRANSPORT_DIAGNOSIS_MARKER


# --------------------------------------------------------------------------- #
# 5. `is_error` alone does not license a substring search (review F3).         #
# --------------------------------------------------------------------------- #

# The SDK's errored-with-subtype-success shape carries THE MODEL'S OWN PROSE in
# `result`, and this repo is full of stream/connection-handling code, so its
# agents write these words constantly. Multi-line with a lead-in, because that
# is what a summary actually looks like.
_PROSE_ABOUT_TRANSPORTS = (
    "Done. Summary of the change:\n\n"
    "- added connection error handling to the poller\n"
    "- covered the stream closed path with a regression test\n"
)


def test_model_prose_about_transports_is_not_a_transport_death():
    """A run can be `is_error=True` and still have the model's own summary in
    `final_text` — that is the SDK's errored-with-subtype-success shape, and it
    is the shape the stub in this very file reproduces. Matching anywhere in
    that text made an agent's SENTENCE ABOUT connection errors indistinguishable
    from a connection error. The identical over-match in `_quota_signal` once
    parked healthy tasks as PAUSED_QUOTA.

    A match now needs corroboration: a structured error signal, or the first
    line. Both controls below fail if either half is dropped.
    """
    def r(text, *, stop_reason=None, api_error_status=None):
        return AgentResult(
            final_text=text, num_turns=1, is_error=True, tokens_used=0,
            session_id=None, stop_reason=stop_reason,
            api_error_status=api_error_status)

    assert not is_transport_failure(r(_PROSE_ABOUT_TRANSPORTS))

    # CONTROL A — the CLI's own wording leads. `_run_once` prepends
    # `last_result_text`, which it only ever captures from an errored result.
    assert is_transport_failure(
        r("Stream closed unexpectedly\n" + _PROSE_ABOUT_TRANSPORTS))
    # CONTROL B — a structured error signal licenses a match further down, which
    # is where the terminal-exception path puts it (under the traceback).
    assert is_transport_failure(
        r("something blew up\n\nTraceback...\nconnection error",
          stop_reason="error"))
    assert is_transport_failure(
        r("upstream said no\n\nStream closed", api_error_status=500))
    # ...and the structured signal is not on its own a licence: 429 is quota.
    assert not is_transport_failure(
        r("You've hit your monthly spend limit", api_error_status=429))
    # THE RESIDUAL, stated rather than hidden: prose that OPENS with the phrase
    # still matches. Narrowing further would need to parse intent; this is the
    # deliberate stopping point, and the shape above is the one that occurs.


def test_a_wrapped_opening_line_is_still_a_transport_death():
    """Whoever wrapped the text does not get to decide whether we retry.

    The corroborating signal is "the marker OPENS the text", but it used to be
    read off `splitlines()[0]` — so a break or a doubled space landing inside
    `Stream closed` made the recorded incident's own message invisible. The
    message is the same message however a terminal, a log formatter or an
    exception renderer laid it out.

    The width of the search is unchanged, and the second half of this test is
    the control that says so: the marker must still START on the opening line.
    """
    def r(text):
        return AgentResult(
            final_text=text, num_turns=1, is_error=True, tokens_used=0,
            session_id=None, stop_reason=None, api_error_status=None)

    # WRAPPED — the break falls inside the phrase.
    assert is_transport_failure(r("Stream\nclosed unexpectedly by consumer"))
    assert is_transport_failure(r("API request failed with a connection\n"
                                  "error: upstream hung up\n"
                                  "  at Object.<anonymous>"))
    # RE-SPACED, and indented by whatever printed it.
    assert is_transport_failure(r("   Stream  closed unexpectedly"))
    assert is_transport_failure(r("\n\n\tStream\tclosed unexpectedly"))
    # Still the observed shape, unwrapped, with the traceback under it.
    assert is_transport_failure(
        r("Stream closed unexpectedly\n" + _PROSE_ABOUT_TRANSPORTS))

    # CONTROL — a marker that starts on the SECOND line is not an opening, and
    # de-wrapping must not turn it into one. `_PROSE_ABOUT_TRANSPORTS` is
    # exactly this shape: a lead-in line, then a bullet naming the phrase.
    assert not is_transport_failure(r(_PROSE_ABOUT_TRANSPORTS))
    assert not is_transport_failure(
        r("Done. Summary of the change:\nStream closed handling added"))
    # ...and quoting one mid-text, several lines down, is still not a match.
    assert not is_transport_failure(
        r("Fixed the retry.\n\nThe CLI reports it as\n"
          '"Stream closed unexpectedly" and we now retry once.'))
    # THE STOPPING POINT, stated rather than hidden: a wrap that pushes the
    # WHOLE phrase onto the next line reads identically to a prose lead-in
    # followed by a bullet about it, and stays a miss. Widening to cover it
    # would take `_PROSE_ABOUT_TRANSPORTS` with it, which is the defect this
    # whole section exists for.
    assert not is_transport_failure(r("API request failed with a\n"
                                      "connection error: upstream hung up"))


def test_opening_span_keeps_the_search_one_line_wide():
    """The de-wrapping helper, directly: it joins the following line so a split
    phrase reads whole, and reports the opening line's own length so a caller
    can refuse anything that starts past it. Without that second return value
    the join would silently double the eligible text.
    """
    from no_human.agent.claude_backend import _opening_span

    haystack, opening_len = _opening_span("stream\nclosed unexpectedly\nmore")
    assert haystack == "stream closed unexpectedly"   # third line excluded
    assert opening_len == len("stream")
    assert haystack.find("stream closed") < opening_len

    # A marker wholly inside the joined-on line starts at/after opening_len.
    haystack, opening_len = _opening_span("done:\nstream closed here")
    assert haystack.find("stream closed") >= opening_len

    # Whitespace-only input, and a following blank line, both stay safe.
    assert _opening_span("   \n\t\n") == ("", 0)
    assert _opening_span("stream closed\n\ntail") == ("stream closed", 13)


async def test_prose_about_transports_never_buys_a_second_session(
        tmp_path, monkeypatch):
    """The consequence, on the real `run()` path rather than on the predicate:
    a task whose summary mentions these words must not be billed for a retry it
    did not need, and must not be filed against the infrastructure."""
    q, calls = _query_yielding(_rm(True, _PROSE_ABOUT_TRANSPORTS))
    monkeypatch.setattr(claude_backend, "query", q)
    monkeypatch.setattr(claude_backend, "_TRANSPORT_RETRY_DELAY_S", 0.0)

    result = await ClaudeBackend(model="m").run("go", cwd=tmp_path, max_turns=3)

    assert calls["n"] == 1, "the model's prose bought a second session"
    assert TRANSPORT_DIAGNOSIS_MARKER not in result.final_text


# --------------------------------------------------------------------------- #
# 6. The give-up text has to fit through the reviewer's 600-char slice (F6).   #
# --------------------------------------------------------------------------- #


async def test_the_real_giveup_text_survives_the_reviewers_600_char_slice(
        tmp_path, monkeypatch):
    """`_review_once` forwards only `final_text.strip()[-600:]`, and everything
    downstream — the blocker category, the notification, whether a human hears
    about an unreviewed diff at all — hangs off the `[transport]` marker being
    inside that window.

    Nothing pinned it. The marker sits at the FRONT of the appended diagnosis,
    so the slice keeps it only while the diagnosis is short; one more sentence
    in that f-string silently pushes the marker out and the escalation
    reverts to NOVEL_UNKNOWN with no test going red. This drives the REAL text
    through the REAL `_review_once` and measures the real margin.
    """
    from no_human.review.reviewer import AdversarialReviewer

    q, _ = _query_yielding(_rm(True, "Stream closed unexpectedly"))
    monkeypatch.setattr(claude_backend, "query", q)
    monkeypatch.setattr(claude_backend, "_TRANSPORT_RETRY_DELAY_S", 0.0)
    set_worker_context(WorkerContext(worker="a1b2c3d4", inflight=3,
                                     max_workers=4))
    real = await ClaudeBackend(model="m").run("go", cwd=tmp_path, max_turns=1)

    class _Backend:
        model = "claude-opus-5"

        async def run(self, prompt, **kwargs):
            return real

    decision, reason, _round = await AdversarialReviewer(backend=_Backend())._review_once(
        "prompt", tmp_path, max_turns=1, timeout=30)

    assert decision is None
    assert TRANSPORT_DIAGNOSIS_MARKER in reason, (
        "the marker fell outside the [-600:] slice — every downstream reader "
        "routes on it, so this failure is silent in production")
    assert "a1b2c3d4" in reason and "3 of 4" in reason

    # THE HEADROOM, measured on the real string rather than assumed. The slice
    # keeps the LAST 600 characters, so what must fit is everything from the
    # marker to the end.
    text = (real.final_text or "").strip()
    from_marker = text[text.index(TRANSPORT_DIAGNOSIS_MARKER):]
    assert len(from_marker) <= 600, (
        f"the diagnosis grew to {len(from_marker)} chars from the marker on; "
        "past 600 the reviewer's slice drops the marker and the dead review "
        "gate is silently misrouted. Shorten it, or widen the slice in "
        "review/reviewer.py._review_once — do not just raise this number.")


# --------------------------------------------------------------------------- #
# 7. A review window must not swallow the retry it is waiting on (F2).         #
# --------------------------------------------------------------------------- #


_VERDICT = (
    'REVIEW_JSON_START {"passed": true, "items": '
    '[{"label": "ok", "passed": true, "evidence": "calc.py:1"}]} REVIEW_JSON_END'
)
_WHERE = "worker w7, dispatched at 3 of 4 pool slot(s) busy"


class _RetryingBackend:
    """A backend that behaves exactly as `ClaudeBackend.run` does mid-retry:
    it ANNOUNCES the transport retry on the caller's event stream, then keeps
    working past the reviewer's window before returning the folded result."""

    model = "claude-opus-5"

    def __init__(self, *, work: float, result=None, announce: bool = True):
        self._work, self._result, self._announce = work, result, announce
        self.cancelled = 0

    async def run(self, prompt, *, cwd=None, max_turns=1, effort=None,
                  on_event=None, **kw):
        if self._announce and on_event is not None:
            on_event(AgentEvent(
                "transport_retry",
                text="session died in the transport — retrying once",
                meta={"attempt": 1, "of": 1, "concurrency": _WHERE,
                      "discarded_tokens": 120},
            ))
        try:
            await asyncio.sleep(self._work)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return self._result


async def test_a_review_window_no_longer_swallows_the_retry_its_spend_and_its_diagnosis(
        tmp_path, monkeypatch):
    """THE INVERTED PROBE. `ClaudeBackend.run` does its transport retry INSIDE
    the coroutine the reviewer wraps in `asyncio.wait_for`, so the retry — a
    5s pause plus a whole second session — spends the reviewer's window and
    then gets cancelled by it. Everything the retry exists to produce died with
    it: the folded spend never reached `attempts.tokens_used`, and the
    `[transport]` diagnosis was never appended, so the escalation said "timed
    out", routed as a task problem, and blamed the diff for a dead socket.

    The retry now gets its own bounded window, and both survive.
    """
    import no_human.review.reviewer as rv

    monkeypatch.setattr(rv, "_REVIEW_MIN_RETRY_TIMEOUT", 1.0)
    recovered = AgentResult(
        final_text=_VERDICT, num_turns=1, is_error=False,
        tokens_used=130,            # 120 burned by the dead session + 10
        session_id="s2", stop_reason="end_turn", output_tokens=23)
    backend = _RetryingBackend(work=0.30, result=recovered)

    decision, reason, _round = await rv.AdversarialReviewer(backend=backend)._review_once(
        "prompt", tmp_path, max_turns=1, timeout=0.05)

    assert decision is not None, f"the retry was swallowed: {reason}"
    assert decision.passed is True
    assert decision.tokens_used == 130, "the folded spend was lost"
    assert decision.output_tokens == 23
    assert backend.cancelled == 0


async def test_a_merely_slow_reviewer_gets_no_grace(tmp_path, monkeypatch):
    """THE CONTROL, and it is the whole reason the grace is gated on the event
    rather than granted on every timeout. Without it the test above passes for
    a change that simply made every review window 1.5x longer — which is the
    regression `_agent_review`'s halving comment was written to prevent."""
    import no_human.review.reviewer as rv

    monkeypatch.setattr(rv, "_REVIEW_MIN_RETRY_TIMEOUT", 1.0)
    backend = _RetryingBackend(work=5.0, result=None, announce=False)

    start = time.monotonic()
    decision, reason, _round = await rv.AdversarialReviewer(backend=backend)._review_once(
        "prompt", tmp_path, max_turns=1, timeout=0.05)
    elapsed = time.monotonic() - start

    assert decision is None
    assert reason == "timed out after 0.05s"
    assert backend.cancelled == 1, "the abandoned session was left running"
    # It gave up on the FIRST window. A grace here would have cost 1.0s more.
    assert elapsed < 0.9, elapsed


async def test_when_even_the_grace_runs_out_the_human_still_hears_transport(
        tmp_path, monkeypatch):
    """The other half of the inversion: no `AgentResult` survives, so nothing
    can carry the marker out — the REASON has to. It must still start with
    "timed out" (that prefix is what halves `_agent_review`'s next window) and
    must still name the retry and the concurrency, or a review gate that died
    in the transport twice is filed against the diff.

    Driven end to end: `_agent_review` -> `ReviewerUnavailable` ->
    `_escalate_reviewer_unavailable` -> the blocker a human actually reads.
    """
    import no_human.review.reviewer as rv
    from no_human.blockers.taxonomy import BlockerCategory
    from no_human.core.orchestrator import Orchestrator

    monkeypatch.setattr(rv, "_REVIEW_MIN_RETRY_TIMEOUT", 0.05)
    backend = _RetryingBackend(work=5.0, result=None)

    with pytest.raises(rv.ReviewerUnavailable) as exc:
        await rv.AdversarialReviewer(backend=backend)._agent_review(
            "prompt", tmp_path, timeout=0.05)

    detail = str(exc.value)
    assert TRANSPORT_DIAGNOSIS_MARKER in detail, detail
    assert _WHERE in detail
    assert "retry" in detail
    assert backend.cancelled == 2, "one abandoned session per round"

    raised = {}

    class _Task:
        id = "t1"
        title = "do a thing"

    orch = Orchestrator.__new__(Orchestrator)

    async def _raise_blocker(task, blocker, **kwargs):
        raised.update(blocker=blocker, kwargs=kwargs)
        from no_human.core.orchestrator import TaskOutcome
        from no_human.core.task import TaskStatus
        return TaskOutcome(task=task, status=TaskStatus.BLOCKED)

    orch._raise_blocker = _raise_blocker
    await orch._escalate_reviewer_unavailable(_Task(), detail)
    assert raised["blocker"].category == BlockerCategory.TRANSIENT_INFRA


def test_the_grace_prefix_is_the_one_agent_review_halves_on():
    """A brittle-looking coupling, pinned because it is load-bearing and
    invisible: `_agent_review` shrinks the next round's window with
    `reason.startswith("timed out")`. The transport-grace reason has to satisfy
    that AND carry the marker, and a reword that puts the marker first would
    silently stop the halving — restoring the 2x600s hang this codebase already
    paid for once."""
    import inspect

    import no_human.review.reviewer as rv

    src = inspect.getsource(rv.AdversarialReviewer._run_bounded)
    assert 'f"timed out after {timeout:g}s + ' in src
    assert 'reason.startswith("timed out")' in inspect.getsource(
        rv.AdversarialReviewer._agent_review)


# --------------------------------------------------------------------------- #
# 8. A dead review gate must reach a human AND actually retry itself (F1).     #
# --------------------------------------------------------------------------- #


_DEAD_GATE_DETAIL = (
    "the reviewer reached no verdict after 2 rounds (reviewer session "
    "transport failure — Stream closed unexpectedly\n\n"
    f"{TRANSPORT_DIAGNOSIS_MARKER} ... worker w7, dispatched at 3 of 4 pool "
    "slot(s) busy.)"
)


def _orch_with(store, notes):
    from no_human.core.orchestrator import Orchestrator

    class _Notifier:
        def notify(self, kind, line):
            notes.append((kind, line))

    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store
    orch.notifier = _Notifier()
    orch.emit = lambda *a, **k: None
    orch.config = {}
    orch.learning_queue = None
    return orch


async def _parked(store, orch, coro_factory):
    from no_human.core.task import Task, TaskStatus

    task = Task.new("do a thing", repo_path="/tmp/x")
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    outcome = await coro_factory(task)
    return task, outcome, await store.get_task(task.id)


async def test_a_dead_review_gate_notifies_now_and_wakes_itself(store):
    """TRANSIENT_INFRA is the right CATEGORY and it buys less than it looks
    like. `Route.auto_retry=True` on it is read NOWHERE in `src/`, and a parked
    blocker with no `wake_condition` never self-fires — `condition_satisfied`
    returns False for a null condition before it reaches its time branch. The
    route is also `notify_now=False`. So routing the dead review gate there,
    unaided, replaced an immediate escalation with 48 SILENT hours ending in a
    park timeout: strictly worse than the NOVEL_UNKNOWN it replaced.

    Both halves are asserted through the REAL `_raise_blocker` and the REAL
    watcher, because both were previously asserted by a docstring only.
    """
    from no_human.core.task import TaskStatus
    from no_human.blockers.wake import WakeWatcher

    notes: list = []
    orch = _orch_with(store, notes)
    task, outcome, parked = await _parked(
        store, orch,
        lambda t: orch._escalate_reviewer_unavailable(t, _DEAD_GATE_DETAIL))

    assert outcome.status == TaskStatus.BLOCKED       # parked, not escalated
    # HALF ONE — the human hears about an unreviewed diff the same minute.
    assert notes, "a dead review gate parked SILENTLY for max_park (48h)"
    assert task.id[:8] in notes[0][1]
    # HALF TWO — and it genuinely retries itself, which is what the category
    # only claimed. A wake_condition is the only thing the watcher fires on.
    assert parked.blocker["wake_condition"] == "after:30m"
    assert parked.wake_check_at

    cfg = {"blockers": {"max_park_duration": "48h"}}
    due = datetime.fromisoformat(parked.wake_check_at) + timedelta(minutes=1)
    assert (task.id, "resumed") in await WakeWatcher(store, cfg).tick(now=due)
    # ...and not before it is due, or the pool thrashes on a saturated backend.
    assert (task.id, "resumed") not in await WakeWatcher(store, cfg).tick(
        now=datetime.fromisoformat(parked.wake_check_at) - timedelta(minutes=1))


async def test_a_reviewer_that_merely_reached_no_verdict_is_not_parked_at_all(
        store):
    """The control for the test above. Without it both assertions pass for a
    function that gives EVERY unavailable reviewer a wake condition and a
    notification — turning a genuinely stuck gate into a park-and-retry loop
    instead of a human decision."""
    from no_human.core.task import TaskStatus

    notes: list = []
    orch = _orch_with(store, notes)
    _task, outcome, parked = await _parked(
        store, orch,
        lambda t: orch._escalate_reviewer_unavailable(
            t, "the reviewer reached no verdict after 2 rounds "
               "(no REVIEW_JSON block)"))

    assert outcome.status == TaskStatus.ESCALATED
    assert not parked.blocker.get("wake_condition")
    assert parked.wake_check_at is None


async def test_the_coder_timeout_streak_also_actually_wakes_up(store):
    """The sibling `_escalate_timeout_streak` carried the identical false claim
    ("the route parks with auto-retry, so a transient wedge self-heals") and the
    identical missing mechanism. Judged the same on the WAKE and differently on
    the NOTIFICATION: this path has always been parked-and-silent by design
    (22.6) and nothing regressed it, so it stays silent — but it now really does
    self-heal instead of sitting out the 48h park."""
    from no_human.blockers.wake import WakeWatcher

    notes: list = []
    orch = _orch_with(store, notes)
    task, _outcome, parked = await _parked(
        store, orch, lambda t: orch._escalate_timeout_streak(t, None, None))

    assert parked.blocker["wake_condition"] == "after:30m"
    assert notes == [], "this path is parked-and-silent by design (22.6)"
    due = datetime.fromisoformat(parked.wake_check_at) + timedelta(minutes=1)
    actions = await WakeWatcher(
        store, {"blockers": {"max_park_duration": "48h"}}).tick(now=due)
    assert (task.id, "resumed") in actions


def test_null_wake_conditions_really_do_never_self_fire():
    """The premise the two tests above rest on, pinned separately so a change
    to `condition_satisfied` cannot quietly make them tautological."""
    from no_human.blockers.wake import WakeWatcher

    watcher = WakeWatcher.__new__(WakeWatcher)
    now = datetime.now(datetime.now().astimezone().tzinfo)
    for null in (None, ""):
        assert asyncio.run(watcher.condition_satisfied(
            null, raised_at=now - timedelta(days=9), now=now,
            wake_check_at=now - timedelta(days=8))) is False


def test_route_auto_retry_is_still_a_label_nothing_reads():
    """Why the wake condition above is load-bearing and not belt-and-braces:
    `Route.auto_retry=True` sets a field NOBODY EVER READS, so the "parked, but
    it auto-retries" story two blockers told was fiction.

    Asserted over the AST rather than with grep, because both fixed docstrings
    now discuss `Route.auto_retry` by name and a text search cannot tell a
    sentence about the field from a read of it — the exact failure mode of a
    guard that matches on shape instead of meaning. If someone WIRES the field
    later this goes red, which is the moment to re-read those two blockers, not
    a moment to delete this.
    """
    import ast

    src = Path(claude_backend.__file__).parents[1]
    reads = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "auto_retry":
                reads.append(f"{path.name}:{node.lineno}")
    assert reads == [], reads


# --------------------------------------------------------------------------- #
# 9. `nh watch` shows the second session it is paying for (F5).                #
# --------------------------------------------------------------------------- #


def test_watch_renders_a_transport_retry_instead_of_dropping_it():
    """The agent-session branch of `WatchApp._sink` was a closed if/elif chain
    over five kinds, so every other kind fell off the end and was DROPPED. The
    backend goes to the trouble of announcing "I am spending a second session
    because the first one died" and `nh watch` showed a gap.

    Same construction idiom as `test_watch_SINK_wires_the_burn_into_the_status`:
    the real app, the real coder-session source, only the log widget stubbed.
    """
    from no_human.cli.tui import WatchApp
    from no_human.core.orchestrator import CODER_ROLE

    written: list[str] = []
    app = WatchApp(config=object(), task_id="abcd1234")
    app.query_one = lambda *a, **k: type(
        "L", (), {"write": lambda self, line: written.append(line)})()

    app._sink({"source": CODER_ROLE, "kind": "transport_retry",
               "text": "session died in the transport — retrying once"})
    app._sink({"source": CODER_ROLE, "kind": "transport_failed",
               "text": f"{TRANSPORT_DIAGNOSIS_MARKER} died twice"})

    assert len(written) == 2, written
    assert "retrying once" in written[0]
    assert TRANSPORT_DIAGNOSIS_MARKER in written[1]
    # A catch-all, not two more names — the next AgentEvent kind is visible on
    # the day it is added rather than the day someone notices it missing.
    app._sink({"source": CODER_ROLE, "kind": "a_kind_nobody_has_added_yet",
               "text": "hello"})
    assert len(written) == 3, "unknown kinds are still being dropped"


def test_no_llm_was_involved():
    """Belt to the hermetic fixture this module opts out of: the stub CLI is a
    local python script and the rest of the file replaces `query` outright."""
    assert "anthropic.com" not in _STUB
    assert os.environ.get("NH_TESTS_LIVE_SDK") != "1"
    json.loads("{}")  # keep the json import honest
