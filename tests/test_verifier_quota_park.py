"""RED-first tests: a verifier-gate judge call that dies on a session/weekly
usage-limit error must be routed through the SAME quota classifier the coder
path already uses (``no_human.core.bounds.quota_signal`` /
``quota_reason``), so the attempt PARKS as ``paused_quota`` instead of being
booked as ``infra: no-verdict`` — which today burns the verifier's one bounded
retry and can escalate a ``NOVEL_UNKNOWN`` "wall-killed-verifier" blocker to a
human for something that is not a bug, just a subscription limit.

Covers acceptance criterion 1 (and its negative control): a verifier round
dying on the exact live quota string parks the attempt as ``paused_quota``
and does not consume the bounded retry or raise a reviewer-unavailable
escalation, while a genuinely unparseable/broken judge response (no quota
phrase) keeps today's behaviour untouched.
"""

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.blockers.taxonomy import BlockerCategory
from no_human.core.bounds import QuotaExhausted
from no_human.core.task import Task, TaskStatus
from no_human.review.reviewer import ReviewerUnavailable
from no_human.review.verifiers import Verifier, run_verifiers
from no_human.vcs.git import GitRepo

from .test_e2e_orchestrator import FakeBackend, _config, _git, bare_repo  # noqa: F401
from .test_verifiers_gate import (
    FakeReviewer, VERIFIER_YAML, _ok_json_for, _orch, _repo_with_a_verifier,
)

_SESSION_LIMIT_TEXT = (
    "You've hit your session limit · resets 4:20am (Asia/Jerusalem)"
)

_DIFF_TEXT = (
    "diff --git a/src.py b/src.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src.py\n"
    "+++ b/src.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
    "+    # TODO fix\n"
)

_VERIFIER = Verifier(id="v1", statement="no TODOs", paths=("**/*.py",))


# ── unit: run_verifiers / _judge_once must propagate QuotaExhausted ────── #

async def test_judge_quota_error_propagates_not_no_verdict():
    """A judge callable that raises the exact live quota-limit string must
    surface as QuotaExhausted, never get swallowed into a deterministic
    no_verdict VerifierResult (today's behaviour: any BaseException from the
    judge becomes ``no verdict: judge raised Exception``)."""

    async def judge(prompt: str):
        raise Exception(_SESSION_LIMIT_TEXT)

    with pytest.raises(QuotaExhausted):
        await run_verifiers(
            judge,
            verifiers=[_VERIFIER],
            diff_text=_DIFF_TEXT,
            read_file=lambda path: "",
            changed_paths=["src.py"],
        )


async def test_judge_non_quota_error_still_no_verdict():
    """Negative control: a genuine, non-quota judge failure keeps today's
    no_verdict behaviour — this must never regress."""

    async def judge(prompt: str):
        raise RuntimeError("transport reset by peer")

    results = await run_verifiers(
        judge,
        verifiers=[_VERIFIER],
        diff_text=_DIFF_TEXT,
        read_file=lambda path: "",
        changed_paths=["src.py"],
    )

    assert len(results) == 1
    result = results[0]
    assert result.no_verdict is True
    assert "judge raised" in result.evidence


# ── orchestrator gate: _run_review must raise QuotaExhausted, not escalate ── #

async def test_verifier_quota_wall_raises_instead_of_escalating(store, tmp_path):
    """Mirrors test_verifiers_gate.py's own
    `test_no_verdict_escalates_instead_of_failing_the_round`, but the bounded
    judge dies on the exact live session-limit string instead of a plain
    timeout. That must never reach `ReviewerUnavailable` — it must raise
    `QuotaExhausted` on the FIRST call, without spending the one bounded
    retry the genuine-infra case is entitled to."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    from no_human.vcs.git import GitRepo
    repo = GitRepo(work)
    reviewer = FakeReviewer((None, _SESSION_LIMIT_TEXT))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    with pytest.raises(QuotaExhausted):
        await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.bounded_calls == 1, (
        "a quota wall must not consume the verifier's bounded retry — the "
        "retry exists for genuine infra flakiness, not a subscription limit")
    assert reviewer.review_calls == 0, (
        "a quota-parked round must never reach the agentic reviewer")


async def test_verifier_non_quota_no_verdict_still_escalates(store, tmp_path):
    """Negative control: a genuinely unparseable/broken verifier judge (no
    quota phrase) must keep today's behaviour — no-verdict, bounded retry
    consumed, and (after exhausting the retry) `ReviewerUnavailable` —
    never silently reclassified as a quota park."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    from no_human.vcs.git import GitRepo
    repo = GitRepo(work)
    reviewer = FakeReviewer((None, "timed out"))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    with pytest.raises(ReviewerUnavailable):
        await orch._run_review(task, repo, attempt_id, base="main")

    assert reviewer.bounded_calls == 2, (
        "the bounded retry must still be spent on a genuine no-verdict")


# ── full pipeline: the whole task parks paused_quota, not FAILED/escalated ── #

async def test_verifier_session_limit_parks_the_whole_task(store, bare_repo, tmp_path):
    """Acceptance criterion 1, end-to-end: a coder attempt that produces a
    real diff, then hits the verifier gate where the judge dies on the exact
    live session-limit string, must park the WHOLE task as PAUSED_QUOTA — not
    fail the attempt, not consume the bounded retry, not raise a
    NOVEL_UNKNOWN reviewer-unavailable escalation."""
    (bare_repo / ".no_human").mkdir(parents=True, exist_ok=True)
    (bare_repo / ".no_human" / "verifiers.yaml").write_text(VERIFIER_YAML)
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-qm", "add verifiers.yaml")
    _git(bare_repo, "push", "origin", "main")

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    reviewer = FakeReviewer((None, _SESSION_LIMIT_TEXT))
    events = []
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer, event_sink=events.append)

    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)
    parked = await store.get_task(t.id)

    assert outcome.status is TaskStatus.PAUSED_QUOTA, (
        f"expected the task to park on the quota wall, got {outcome.status}: "
        f"{getattr(outcome, 'detail', None)}"
    )
    assert parked.blocker is None or parked.blocker.get("type") != "NOVEL_UNKNOWN", (
        "a session-limit quota wall must never surface as a NOVEL_UNKNOWN escalation"
    )
    used_attempts, _, _ = await store.lifetime_usage_by_class(t.id)
    assert used_attempts == 0, "a quota park must not consume the bounded retry"
    assert reviewer.bounded_calls == 1


# ── AC1 (2026-09-04 incident): a verifier round that reaches no verdict ───── #
# because the API is WALLED (quota, or the same infra outage the coder path  #
# already classifies via `_infra_sdk_failure`) must park PAUSED_QUOTA/       #
# quota_refreshed, not escalate NOVEL_UNKNOWN — 279c03c5, c5b24230, 7da7c7ce #
# each needed a human reply to say "it was the wall, resume".                #

_529_RESULT = AgentResult(
    final_text="", num_turns=0, is_error=True, tokens_used=0,
    cache_read_tokens=0, cache_creation_tokens=0, session_id=None,
    stop_reason="error", api_error_status=529,
)


async def test_a_529_error_result_parks_the_verifier_round_on_the_wall(store, tmp_path):
    """A bounded judge call that comes BACK (does not time out) but as an
    errored `AgentResult` carrying a 529 status — the 2026-09-03 shape
    `_infra_sdk_failure` already classifies on the coder path — must raise
    `QuotaExhausted(infra=True)`, not fall through to no_verdict."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)
    reviewer = FakeReviewer((_529_RESULT, ""))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    with pytest.raises(QuotaExhausted) as excinfo:
        await orch._run_review(task, repo, attempt_id, base="main")

    assert excinfo.value.infra is True, (
        "an API-wall (529) result must be flagged infra so the pool-side "
        "breaker sees it, same as the coder path")
    assert reviewer.review_calls == 0, (
        "a walled round must never reach the agentic reviewer")

    attempts = await store.list_attempts(task.id)
    assert attempts[0]["infra_failure"] == 1, (
        "the closed attempt row must be excluded from lifetime usage — a "
        "round that never got a verdict must not burn a lifetime attempt")
    assert attempts[0]["failure_reason"].startswith("infra: ")


async def test_a_529_killed_verifier_round_parks_the_whole_task_paused_quota(
        store, bare_repo, tmp_path):
    """End-to-end sibling of `test_verifier_session_limit_parks_the_whole_task`
    for the errored-`AgentResult` (529) shape: the whole task must park
    PAUSED_QUOTA/quota_refreshed with `infra: True` on the blocker, never
    escalate."""
    (bare_repo / ".no_human").mkdir(parents=True, exist_ok=True)
    (bare_repo / ".no_human" / "verifiers.yaml").write_text(VERIFIER_YAML)
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-qm", "add verifiers.yaml")
    _git(bare_repo, "push", "origin", "main")

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    reviewer = FakeReviewer((_529_RESULT, ""))
    events = []
    from no_human.core.orchestrator import Orchestrator
    from no_human.notify.slack import SlackNotifier
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer, event_sink=events.append)

    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)
    parked = await store.get_task(t.id)

    assert outcome.status is TaskStatus.PAUSED_QUOTA, (
        f"expected the task to park on the API wall, got {outcome.status}: "
        f"{getattr(outcome, 'detail', None)}"
    )
    assert parked.blocker["wake_condition"] == "quota_refreshed"
    assert parked.blocker["category"] == "QUOTA"
    assert parked.blocker["infra"] is True
    assert parked.wake_check_at, "a park with no re-check stamp never wakes"

    kinds = {e.get("kind") for e in events}
    assert "blocker" not in kinds and "escalated" not in kinds, (
        f"a walled verifier round must never escalate — event kinds: {kinds}")
    assert "NOVEL_UNKNOWN" not in str(events), (
        "a walled verifier round must never surface as a NOVEL_UNKNOWN report")


async def test_a_judge_exception_naming_an_overloaded_api_parks_not_escalates():
    """Mirrors `test_judge_quota_error_propagates_not_no_verdict`, but for the
    API-wall shape `quota_signal` cannot see: an exception naming a raw HTTP
    529 / SDK `overloaded_error`, with no billing-limit prose at all."""

    async def judge(prompt: str):
        raise Exception("HTTP 529: {'type':'overloaded_error'}")

    with pytest.raises(QuotaExhausted):
        await run_verifiers(
            judge,
            verifiers=[_VERIFIER],
            diff_text=_DIFF_TEXT,
            read_file=lambda path: "",
            changed_paths=["src.py"],
        )


async def test_a_malformed_verdict_still_escalates_novel_unknown(store, tmp_path):
    """Guard against over-widening: a judge that RAN and answered, but with
    nothing parseable as a VERIFIER_JSON block (no quota/API-wall text
    anywhere in it), must still exhaust the bounded retry and escalate
    NOVEL_UNKNOWN — exactly today's behaviour, pinned byte-for-byte against
    `test_verifiers_gate.py::test_no_verdict_escalates_instead_of_failing_the_round`."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)
    reviewer = FakeReviewer("not a VERIFIER_JSON block at all")
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    with pytest.raises(ReviewerUnavailable) as excinfo:
        await orch._run_review(task, repo, attempt_id, base="main")

    assert str(excinfo.value) == (
        "1 verifier(s) reached no verdict after a bounded retry, and none "
        "of the other verifiers this round failed: no-todo. Escalating "
        "instead of charging the coder for a defect nobody found.")
    assert reviewer.bounded_calls == 2

    detail = str(excinfo.value)
    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    escalated = await store.get_task(task.id)

    assert outcome.status == TaskStatus.ESCALATED
    assert escalated.blocker["category"] == BlockerCategory.NOVEL_UNKNOWN.value


async def test_the_wall_park_resumes_into_the_same_verifier_round_uncharged(
        store, tmp_path):
    """AC3: resuming a wall-park must re-enter the SAME verifier round — no
    second coder attempt row, no lifetime-usage charge — and once the wall
    lifts, a healthy judge call on the identical attempt/sha reaches a real
    verdict."""
    work = _repo_with_a_verifier(tmp_path, VERIFIER_YAML)
    repo = GitRepo(work)
    reviewer = FakeReviewer((_529_RESULT, ""))
    orch = _orch(store, tmp_path, reviewer)

    task = Task.new("t", repo_path=str(work))
    await store.create_task(task)
    # Walk to an ACTIVE state (main flow, as the real pipeline does before
    # ever reaching the review gate) — `_park_quota` -> PAUSED_QUOTA is only
    # a legal edge from an active working state, not from PENDING.
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    await store.set_status(task, TaskStatus.IMPLEMENTING)
    attempt_id = await store.create_attempt(task.id, 1)
    reviewed_sha = repo.head_sha()

    with pytest.raises(QuotaExhausted) as excinfo:
        await orch._run_review(task, repo, attempt_id, base="main")

    outcome = await orch._park_quota(task, excinfo.value, repo=repo)
    parked = await store.get_task(task.id)

    assert outcome.status is TaskStatus.PAUSED_QUOTA
    assert parked.blocker["resume_commit"] == reviewed_sha
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, "a wall park must not consume the bounded retry"

    # The wall lifts: swap in a healthy reviewer and resume into the SAME
    # attempt/round rather than starting a new coder attempt.
    orch.reviewer = FakeReviewer(_ok_json_for("no-todo", passed=True))

    decision = await orch._run_review(task, repo, attempt_id, base="main")

    assert decision.passed is True
    assert await store.count_attempts(task.id) == 1, (
        "resuming a wall park must not charge a second coder attempt")
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, (
        "the closed, walled attempt row must still be excluded from "
        "lifetime usage after the round completes on resume")
