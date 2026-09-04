"""The awaiting-approval ladder: merged → done, closed → escalate, red CI →
bounded fix loop. Born from PR #7004: the Jenkinsfile died in Jenkins' CPS
compiler (MethodTooLargeException) while every local check passed, and nothing
watched the PR's own pipeline."""

from __future__ import annotations

import time
from datetime import datetime, timezone


from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus


async def _approval_task(store, url="https://code.example.com/dev/x/pull/7004"):
    t = Task.new("ci_gate", repo_path="/tmp/x")
    t.context = {"pr_watch": url, "pr_branch": "scratch/x"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, state="OPEN", checks=None, log="", annotations="",
             events=None, comments=None, ignore_authors=None, shipped=None):
    async def pr_state(url): return state
    async def pr_checks(url): return checks or []
    async def ci_log(link): return log
    async def ci_annotations(url, name=""): return annotations
    async def pr_comment(url): return comments or []
    # The ignore list is CONFIGURED here rather than inherited from a default.
    # It used to ship naming a real CI service account, which meant a test
    # about ignoring bot chatter only passed because the product came
    # pre-loaded with one employer's bot. A user sets their own; so does this.
    cfg = {"blockers": {"ignore_comment_authors": ignore_authors}} if ignore_authors else {}
    return WakeWatcher(
        store, cfg, pr_state=pr_state, pr_checks=pr_checks, ci_log=ci_log,
        ci_annotations=ci_annotations,
        pr_comment=pr_comment,
        # `shipped` is itself the pr_shipped(repo_path, branch, base) async
        # callable, or None to leave the checker unwired — matching every
        # other checker on this fixture and letting a test simulate a
        # raising probe by passing its own callable.
        pr_shipped=shipped,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


FAIL_CHECK = {
    "name": "continuous-integration/jenkins/pr-head", "status": "fail",
    "link": "https://build.example.com/.../PR-7004/2/display/redirect",
}


async def test_merged_pr_flips_the_task_to_done(store):
    t = await _approval_task(store)
    events = []
    out = await _watcher(store, state="MERGED", events=events)._check_open_pr(t)
    assert out == "merged"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE
    assert any(k == "merged" for k, _ in events)


async def test_closed_unmerged_escalates_with_a_question(store):
    t = await _approval_task(store)
    out = await _watcher(store, state="CLOSED")._check_open_pr(t)
    assert out == "escalated_pr_closed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert "closed without merging" in (fresh.blocker or {}).get("question", "")


async def test_red_ci_injects_the_log_and_resumes_onto_the_pr_branch(store):
    t = await _approval_task(store)
    events = []
    w = _watcher(store, checks=[FAIL_CHECK],
                 log="Method too large: WorkflowScript.___cps___", events=events)
    out = await w._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    fb = fresh.context["send_back_feedback"]
    assert fb[-1]["source"] == "pr_ci"
    assert "Method too large" in fb[-1]["message"]
    assert fresh.context["pr_ci_rounds"] == 1
    assert any(k == "pr_ci_red" for k, _ in events)


async def test_the_same_failure_signature_never_burns_a_second_round(store):
    """A re-run (or re-poll) of the same red check must be free."""
    t = await _approval_task(store)
    w = _watcher(store, checks=[FAIL_CHECK])
    assert await w._check_open_pr(t) == "resumed"
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    assert await w._check_open_pr(t) is None
    assert (await store.get_task(t.id)).context["pr_ci_rounds"] == 1


# ---- the self-comment loop (2026-07-10 P0 incident) ------------------------- #
# no_human posts its CI_GATE results comment under the operator's own gh login,
# so an author check can't distinguish the product's output from real feedback.
# The comment carries AGENT_COMMENT_MARKER; _is_self_or_bot filters it. Without
# these tests, the exact bug that flipped a green, merge-ready task to escalated
# (BUDGET_EXHAUSTED) had no regression pin.

async def test_own_agent_comment_never_resumes_the_task(store):
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER, PrComment
    t = await _approval_task(store)
    own = PrComment(
        author="dev",  # the operator's OWN login — an author check is blind
        body=f"{AGENT_COMMENT_MARKER}\n🧪 **CI_GATE Integration Test Results** ✅ passed",
        created_at="2026-07-10T19:05:41Z",
    )
    events = []
    out = await _watcher(store, comments=[own], events=events)._check_approval_pr_comments(t)
    assert out is None, "the product's own comment must not resume the task"
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL
    assert not any(k == "resumed" for k, _ in events)
    assert any(k == "pr_feedback_skipped" for k, _ in events)


async def test_genuine_operator_comment_resumes_to_revise(store):
    """A real operator comment (no agent marker) IS feedback → resume."""
    from no_human.vcs.pr_watcher import PrComment
    t = await _approval_task(store)
    human = PrComment(author="dev", body="please rename the helper",
                      created_at="2026-07-10T20:00:00Z")
    events = []
    out = await _watcher(store, comments=[human], events=events)._check_approval_pr_comments(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context["send_back_feedback"][-1]["message"] == "please rename the helper"
    assert any(k == "resumed" for k, _ in events)


async def test_self_comment_advances_the_cursor(store):
    """After skipping its own comment the cursor moves past it, so a later tick
    never reconsiders it (the incident re-fired every heartbeat before this)."""
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER, PrComment
    t = await _approval_task(store)
    own = PrComment(author="dev",
                    body=f"{AGENT_COMMENT_MARKER}\nresults",
                    created_at="2026-07-10T19:05:41Z")
    await _watcher(store, comments=[own])._check_approval_pr_comments(t)
    fresh = await store.get_task(t.id)
    assert (fresh.context or {}).get("pr_comment_since") == "2026-07-10T19:05:41Z"


# ---- comment-after-landing guard (2026-08-12 incident) ---------------------- #
# The operator closed a landed PR with an explanatory comment; the comment
# rung read author+marker only (never PR state) and resumed a coder onto
# already-merged work. Both comment rungs — the AWAITING_APPROVAL poll
# (_check_approval_pr_comments, called directly here, the incident's own
# shape) and the BLOCKED wake condition (_evaluate's pr_comment_on branch,
# driven via _evaluate itself below) — must check state+shipped before ever
# treating a comment as revision feedback.

async def test_operator_comment_on_a_closed_shipped_pr_never_resumes(store):
    from no_human.vcs.pr_watcher import PrComment
    t = await _approval_task(store)
    t.context["base_branch"] = "main"
    await store.update_task(t)
    dev = PrComment(author="dev", body="closing this — it landed as a local squash",
                    created_at="2026-08-12T03:04:00Z")
    events = []

    async def shipped(repo_path, branch, base): return True

    out = await _watcher(store, state="CLOSED", shipped=shipped, comments=[dev],
                         events=events)._check_approval_pr_comments(t)
    assert out != "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert any(k == "comment_after_landing" for k, _ in events)
    assert not any(k == "resumed" for k, _ in events)
    assert not any(k == "pr_feedback" for k, _ in events)
    assert "send_back_feedback" not in (fresh.context or {})


async def test_a_landed_task_no_ops_and_still_records_the_note(store):
    from no_human.vcs.pr_watcher import PrComment
    t = await _approval_task(store)
    t.context["base_branch"] = "main"
    await store.update_task(t)
    await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    dev = PrComment(author="dev", body="closing this — it landed as a local squash",
                    created_at="2026-08-12T03:04:00Z")
    events = []

    async def shipped(repo_path, branch, base): return True

    out = await _watcher(store, state="CLOSED", shipped=shipped, comments=[dev],
                         events=events)._check_approval_pr_comments(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert any(k == "comment_after_landing" for k, _ in events)
    assert not any(k == "shipped" for k, _ in events)


async def test_operator_comment_on_a_closed_unshipped_pr_still_resumes(store):
    from no_human.vcs.pr_watcher import PrComment
    t = await _approval_task(store)
    t.context["base_branch"] = "main"
    await store.update_task(t)
    dev = PrComment(author="dev", body="please rework this before reopening",
                    created_at="2026-08-12T03:04:00Z")

    async def shipped(repo_path, branch, base): return False

    out = await _watcher(store, state="CLOSED", shipped=shipped,
                         comments=[dev])._check_approval_pr_comments(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert ("please rework this before reopening"
            in fresh.context["send_back_feedback"][-1]["message"])


async def test_unknown_pr_state_still_resumes(store):
    """The state read failed (gh missing / network down) — the guard must
    not silence a genuine reviewer comment behind that ambiguity."""
    from no_human.vcs.pr_watcher import PrComment
    t = await _approval_task(store)
    t.context["base_branch"] = "main"
    await store.update_task(t)
    dev = PrComment(author="dev", body="please rename the helper",
                    created_at="2026-08-12T03:04:00Z")

    async def shipped(repo_path, branch, base): return True

    out = await _watcher(store, state="", shipped=shipped,
                         comments=[dev])._check_approval_pr_comments(t)
    assert out == "resumed"


async def test_a_raising_shipped_probe_still_resumes(store):
    from no_human.vcs.pr_watcher import PrComment
    t = await _approval_task(store)
    t.context["base_branch"] = "main"
    await store.update_task(t)
    dev = PrComment(author="dev", body="please rename the helper",
                    created_at="2026-08-12T03:04:00Z")

    async def shipped(repo_path, branch, base): raise RuntimeError("git probe failed")

    out = await _watcher(store, state="CLOSED", shipped=shipped,
                         comments=[dev])._check_approval_pr_comments(t)
    assert out == "resumed"


async def test_the_cursor_advances_past_a_post_landing_comment(store):
    """Run the rung twice with the SAME comment: comment_after_landing must
    appear once, and the shipped probe must not re-run (the cursor already
    advanced past this comment on the first pass)."""
    from no_human.vcs.pr_watcher import PrComment
    t = await _approval_task(store)
    t.context["base_branch"] = "main"
    await store.update_task(t)
    dev = PrComment(author="dev", body="closing this — it landed as a local squash",
                    created_at="2026-08-12T03:04:00Z")
    events, calls = [], []

    async def shipped(repo_path, branch, base):
        calls.append((repo_path, branch, base))
        return True

    w = _watcher(store, state="CLOSED", shipped=shipped, comments=[dev], events=events)
    assert await w._check_approval_pr_comments(t) != "resumed"
    t2 = await store.get_task(t.id)
    assert await w._check_approval_pr_comments(t2) is None
    assert sum(1 for k, _ in events if k == "comment_after_landing") == 1
    assert len(calls) == 1
    assert t2.context.get("pr_comment_since") == "2026-08-12T03:04:00Z"


async def _blocked_task(store, *, now, ref="org/repo#42"):
    """A BLOCKED task parked on a pr_comment_on wake condition — the shape
    _evaluate's pr_comment_on branch (not _check_approval_pr_comments)
    actually reaches: the incident's own rung."""
    t = Task.new("ci_gate", repo_path="/tmp/x")
    t.context = {"pr_branch": "scratch/x", "base_branch": "main"}
    await store.create_task(t)
    t.blocker = {
        "category": "DEPENDENCY_WAIT",
        "wake_condition": f"pr_comment_on:{ref}",
        "raised_at": now.isoformat(), "confidence": 0.9,
    }
    await store.update_task(t)
    await store.set_status(t, TaskStatus.BLOCKED, validate=False)
    return t


async def test_the_evaluate_rung_never_resumes_a_closed_shipped_pr_comment(store):
    """The incident's actual shape: _evaluate's pr_comment_on branch, not
    just the AWAITING_APPROVAL poll rung."""
    from no_human.vcs.pr_watcher import PrComment
    now = datetime(2026, 8, 12, 3, 4, tzinfo=timezone.utc)
    t = await _blocked_task(store, now=now)
    comment = PrComment(author="dev", body="closing this — it landed as a local squash",
                        created_at=now.isoformat())
    events = []

    async def pr_state(ref): return "CLOSED"
    async def pr_shipped(repo_path, branch, base): return True
    async def pr_comment(ref): return [comment]

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=pr_shipped,
                    pr_comment=pr_comment,
                    on_event=lambda k, txt: events.append((k, txt)))
    out = await w._evaluate(t, now=now)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.BLOCKED
    assert any(k == "comment_after_landing" for k, _ in events)
    assert not any(k == "resumed" for k, _ in events)
    assert not any(k == "pr_feedback" for k, _ in events)


async def test_the_evaluate_rung_still_resumes_on_a_closed_unshipped_pr_comment(store):
    """Regression pin: a genuine reopen-and-rework comment must still wake
    the task even though the PR itself is closed."""
    from no_human.vcs.pr_watcher import PrComment
    now = datetime(2026, 8, 12, 3, 4, tzinfo=timezone.utc)
    t = await _blocked_task(store, now=now)
    comment = PrComment(author="dev", body="please rework this before reopening",
                        created_at=now.isoformat())

    async def pr_state(ref): return "CLOSED"
    async def pr_shipped(repo_path, branch, base): return False
    async def pr_comment(ref): return [comment]

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=pr_shipped,
                    pr_comment=pr_comment)
    out = await w._evaluate(t, now=now)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert ("please rework this before reopening"
            in fresh.context["send_back_feedback"][-1]["message"])


async def test_the_evaluate_rung_does_not_reprobe_after_confirming_shipped(store):
    """Review finding 2026-08-12: without a throttle, a parked BLOCKED task
    with landed content re-ran pr_state + pr_shipped and re-emitted
    comment_after_landing on every tick while the operator's comment sat
    there — the exact re-fire the incident showed, since condition_satisfied
    re-satisfies on the same unresolved comment every tick."""
    from no_human.vcs.pr_watcher import PrComment
    now = datetime(2026, 8, 12, 3, 4, tzinfo=timezone.utc)
    t = await _blocked_task(store, now=now)
    comment = PrComment(author="dev", body="closing this — it landed as a local squash",
                        created_at=now.isoformat())
    state_calls, shipped_calls, events = [], [], []

    async def pr_state(ref):
        state_calls.append(ref)
        return "CLOSED"

    async def pr_shipped(repo_path, branch, base):
        shipped_calls.append((repo_path, branch, base))
        return True

    async def pr_comment(ref): return [comment]

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=pr_shipped,
                    pr_comment=pr_comment,
                    on_event=lambda k, txt: events.append(k))
    assert await w._evaluate(t, now=now) is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.BLOCKED
    assert events.count("comment_after_landing") == 1
    assert len(state_calls) == 1
    assert len(shipped_calls) == 1

    assert await w._evaluate(fresh, now=now) is None
    assert (await store.get_task(t.id)).status is TaskStatus.BLOCKED
    assert events.count("comment_after_landing") == 1
    assert len(state_calls) == 1
    assert len(shipped_calls) == 1


# ---- the stuck-active watchdog --------------------------------------------- #
# A task can hang mid-attempt (a wedged Agent-SDK session emits nothing).
# Without this, it sits IMPLEMENTING forever, invisible — the exact shape that
# needed a hand-run `nh doctor` to spot. The watchdog escalates it honestly.

def _stuck_watcher(store, *, minutes=30):
    return WakeWatcher(store, {"blockers": {"stuck_active_minutes": minutes}})


async def _active_task(store, *, last_event_age_min: float | None):
    t = Task.new("wedged", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    if last_event_age_min is not None:
        await store.save_events(t.id, [{
            "source": "orchestrator", "kind": "attempt_start", "text": "",
            "ts": time.time() - last_event_age_min * 60,
        }])
    return t


async def test_a_stalled_active_task_escalates(store):
    t = await _active_task(store, last_event_age_min=60)
    now = datetime.now(timezone.utc)
    escalated = await _stuck_watcher(store, minutes=30)._escalate_if_stalled(t, now=now)
    assert escalated is True
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert (fresh.blocker or {}).get("category") == "NOVEL_UNKNOWN"
    assert "stalled" in (fresh.blocker or {}).get("question", "")


async def test_recent_activity_is_not_stalled(store):
    t = await _active_task(store, last_event_age_min=5)
    escalated = await _stuck_watcher(store, minutes=30)._escalate_if_stalled(
        t, now=datetime.now(timezone.utc))
    assert escalated is False
    assert (await store.get_task(t.id)).status is TaskStatus.IMPLEMENTING


async def test_a_task_that_never_emitted_is_left_to_the_normal_loop(store):
    t = await _active_task(store, last_event_age_min=None)
    escalated = await _stuck_watcher(store, minutes=30)._escalate_if_stalled(
        t, now=datetime.now(timezone.utc))
    assert escalated is False


async def test_the_watchdog_is_disabled_at_zero(store):
    t = await _active_task(store, last_event_age_min=600)  # 10h stale
    escalated = await _stuck_watcher(store, minutes=0)._escalate_if_stalled(
        t, now=datetime.now(timezone.utc))
    assert escalated is False, "stuck_active_minutes<=0 disables the watchdog"


async def test_a_pause_in_flight_is_not_overridden(store):
    """cancel_requested means a pause is already landing — don't race it."""
    t = await _active_task(store, last_event_age_min=120)
    t.cancel_requested = True
    escalated = await _stuck_watcher(store, minutes=30)._escalate_if_stalled(
        t, now=datetime.now(timezone.utc))
    assert escalated is False


async def test_a_new_build_failing_the_same_checks_is_a_new_round(store):
    """The fix push re-ran CI (new build number in the link) and the same
    checks failed again — that must inject fresh feedback, not read as
    "already handled". Signing on names alone deadlocked exactly here."""
    t = await _approval_task(store)
    assert await _watcher(store, checks=[FAIL_CHECK])._check_open_pr(t) == "resumed"
    rerun = {**FAIL_CHECK, "link": FAIL_CHECK["link"].replace("/2/", "/3/")}
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    assert await _watcher(store, checks=[rerun])._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_ci_rounds"] == 2
    assert len(fresh.context["send_back_feedback"]) == 2


async def test_ci_rounds_cap_escalates_with_the_named_check(store):
    t = await _approval_task(store)
    w = _watcher(store, checks=[FAIL_CHECK])
    # Distinct signatures each round: vary the failing check name.
    for n in range(1, 4):
        w2 = _watcher(store, checks=[{**FAIL_CHECK, "name": f"check-{n}"}])
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        assert await w2._check_open_pr(t) == "resumed", f"round {n} should resume"
    w4 = _watcher(store, checks=[{**FAIL_CHECK, "name": "check-4"}])
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    out = await w4._check_open_pr(t)
    assert out == "escalated_ci"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert "check-4" in (fresh.blocker or {}).get("question", "")


_BILLING_LOG = (
    "The job was not started because recent account payments have failed "
    "or your spending limit needs to be increased."
)


async def test_billing_infra_red_never_counts_a_round_or_escalates(store):
    """A platform-layer failure (billing outage) says nothing about the code:
    no fix round, no send-back, no escalation — the 2026-08-11 incident."""
    t = await _approval_task(store)
    events = []
    w = _watcher(store, checks=[FAIL_CHECK], log=_BILLING_LOG, events=events)
    assert await w._check_open_pr(t) is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert "pr_ci_rounds" not in (fresh.context or {})
    assert "send_back_feedback" not in (fresh.context or {})
    assert any(k == "pr_ci_infra" for k, _ in events)
    assert not any(k in ("escalated_ci", "pr_ci_red") for k, _ in events)


async def test_billing_infra_never_records_the_acted_on_signature(store):
    """The infra path must not write pr_ci_last_sig: a NEW build with a REAL
    failure must count round 1 (a healthy pipeline is evaluated fresh)."""
    t = await _approval_task(store)
    assert await _watcher(store, checks=[FAIL_CHECK], log=_BILLING_LOG)._check_open_pr(t) is None
    t = await store.get_task(t.id)
    new_build = {**FAIL_CHECK, "link": FAIL_CHECK["link"].replace("/2/", "/3/")}
    w2 = _watcher(store, checks=[new_build], log="AssertionError: boom")
    assert await w2._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_ci_rounds"] == 1
    assert "AssertionError" in fresh.context["send_back_feedback"][-1]["message"]


async def test_infra_repolls_are_free_no_refetch_no_new_event(store):
    """Review finding 2026-08-12: without a throttle, a parked billing-red PR
    costs a log fetch + an event row EVERY watcher tick (8640/day at the 10s
    default). The same build must be classified once, then be free."""
    t = await _approval_task(store)
    fetches, events = [], []

    async def pr_state(url): return "OPEN"
    async def pr_checks(url): return [FAIL_CHECK]
    async def ci_log(link):
        fetches.append(link)
        return _BILLING_LOG
    w = WakeWatcher(
        store, {}, pr_state=pr_state, pr_checks=pr_checks, ci_log=ci_log,
        on_event=lambda k, t2: events.append(k),
    )
    for _ in range(5):
        t = await store.get_task(t.id)
        assert await w._check_open_pr(t) is None
    assert len(fetches) == 1
    assert events.count("pr_ci_infra") == 1


async def test_the_infra_signature_is_one_full_sentence_not_a_prefix(store):
    """Review finding 2026-08-12: partial phrases appear in unrelated failures
    and in pytest echoes of this corpus; a match SUPPRESSES reporting, so the
    dangerous direction is overmatch. Only the full billing sentence counts."""
    from no_human.blockers.wake import _CI_INFRA_RE
    assert _CI_INFRA_RE.search(_BILLING_LOG)
    assert not _CI_INFRA_RE.search(
        "ERROR: The job was not started because the upstream project failed to build."
    )
    assert not _CI_INFRA_RE.search(
        "FAILED tests/test_billing.py::test_dunning - recent account payments have failed"
    )


async def test_empty_log_with_billing_annotation_is_infra_not_a_round(store):
    """A job GitHub blocks at START never produces a log at all — the billing
    text lives only in the check-run ANNOTATION (verified via `gh api
    .../check-runs/<id>/annotations` on PRs #171/#183). The empty-log-only
    classifier can never fire for exactly the failure class it exists for;
    the annotation channel must be tried and must classify it INFRA."""
    t = await _approval_task(store)
    events = []
    w = _watcher(store, checks=[FAIL_CHECK], log="", annotations=_BILLING_LOG,
                 events=events)
    assert await w._check_open_pr(t) is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert not (fresh.context or {}).get("pr_ci_rounds")
    assert "pr_ci_last_sig" not in (fresh.context or {})
    assert "send_back_feedback" not in (fresh.context or {})
    assert any(k == "pr_ci_infra" for k, _ in events)
    assert not any(k in ("escalated_ci", "pr_ci_red") for k, _ in events)


async def test_no_annotation_and_no_log_is_still_a_real_round(store):
    """Fail-closed regression pin: an empty log AND an empty annotation must
    still be treated as a real failure — infra is never assumed."""
    t = await _approval_task(store)
    events = []
    w = _watcher(store, checks=[FAIL_CHECK], log="", annotations="", events=events)
    assert await w._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_ci_rounds"] == 1
    assert fresh.context["send_back_feedback"][-1]["source"] == "pr_ci"
    assert any(k == "pr_ci_red" for k, _ in events)


async def test_empty_log_with_non_infra_annotation_is_a_real_round(store):
    """Narrowness pin (intake decision): a non-matching annotation must NOT be
    treated as infra — only the specific billing/payment wording counts."""
    t = await _approval_task(store)
    events = []
    w = _watcher(store, checks=[FAIL_CHECK], log="", annotations="ERROR: test_foo failed",
                 events=events)
    assert await w._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_ci_rounds"] == 1
    assert fresh.context["send_back_feedback"][-1]["source"] == "pr_ci"
    assert any(k == "pr_ci_red" for k, _ in events)


async def test_advisory_policy_records_the_red_but_never_acts(store):
    """Operator override while Actions quota is exhausted (2026-08-12): a red
    check must not count a round, resume the coder, or escalate — even a REAL
    failure with a readable log. Undo at go-public."""
    t = await _approval_task(store)
    events = []

    async def pr_state(url): return "OPEN"
    async def pr_checks(url): return [FAIL_CHECK]
    async def ci_log(link): return "AssertionError: boom"
    w = WakeWatcher(
        store, {"blockers": {"pr_ci_policy": "advisory"}},
        pr_state=pr_state, pr_checks=pr_checks, ci_log=ci_log,
        on_event=lambda k, t2: events.append((k, t2)),
    )
    assert await w._check_open_pr(t) is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert "pr_ci_rounds" not in (fresh.context or {})
    assert "send_back_feedback" not in (fresh.context or {})
    assert any(k == "pr_ci_advisory" for k, _ in events)
    assert not any(k in ("escalated_ci", "pr_ci_red") for k, _ in events)
    # Throttled: the same build emits once.
    t2 = await store.get_task(t.id)
    events.clear()
    assert await w._check_open_pr(t2) is None
    assert not events


async def test_advisory_signature_never_suppresses_enforce_after_the_flip(store):
    """Review finding 2026-08-12: advisory must use its OWN throttle key. A
    build recorded under advisory (same link — GitHub re-runs keep it) must
    still count a round after the operator flips back to enforce."""
    t = await _approval_task(store)

    async def pr_state(url): return "OPEN"
    async def pr_checks(url): return [FAIL_CHECK]
    async def ci_log(link): return "AssertionError: boom"
    adv = WakeWatcher(store, {"blockers": {"pr_ci_policy": "advisory"}},
                      pr_state=pr_state, pr_checks=pr_checks, ci_log=ci_log)
    assert await adv._check_open_pr(t) is None
    t = await store.get_task(t.id)
    enf = WakeWatcher(store, {}, pr_state=pr_state, pr_checks=pr_checks,
                      ci_log=ci_log)
    assert await enf._check_open_pr(t) == "resumed"
    assert (await store.get_task(t.id)).context["pr_ci_rounds"] == 1


async def test_an_unknown_policy_value_falls_back_to_enforce_loudly(store, caplog):
    """A typo must not silently disable enforcement — warn and enforce."""
    import logging
    with caplog.at_level(logging.WARNING, logger="no_human.wake"):
        w = _watcher(store, checks=[FAIL_CHECK], log="AssertionError: boom")
        w2 = WakeWatcher(store, {"blockers": {"pr_ci_policy": "Advisory "}},
                         pr_state=None, pr_checks=None, ci_log=None)
    assert w2.pr_ci_policy == "advisory"  # case/space normalized, still valid
    w3 = WakeWatcher(store, {"blockers": {"pr_ci_policy": "advisry"}},
                     pr_state=None, pr_checks=None, ci_log=None)
    assert w3.pr_ci_policy == "enforce"
    assert w.pr_ci_policy == "enforce"


async def test_the_default_policy_is_enforce_and_still_acts(store):
    """The advisory override must be opt-in: without the config key, a real
    red check still counts a round (the go-public default)."""
    t = await _approval_task(store)
    w = _watcher(store, checks=[FAIL_CHECK], log="AssertionError: boom")
    assert w.pr_ci_policy == "enforce"
    assert await w._check_open_pr(t) == "resumed"
    assert (await store.get_task(t.id)).context["pr_ci_rounds"] == 1


async def test_an_empty_or_unreadable_excerpt_is_a_real_failure_not_infra(store):
    """Fail closed: infra must be positively identified. No log = real round."""
    t = await _approval_task(store)
    w = _watcher(store, checks=[FAIL_CHECK], log="")
    assert await w._check_open_pr(t) == "resumed"
    assert (await store.get_task(t.id)).context["pr_ci_rounds"] == 1


class _Comment:
    def __init__(self, author, body, created_at):
        self.author = author
        self.body = body
        self.created_at = created_at
        self.path = self.line = self.diff_hunk = None


async def test_bot_comments_never_trigger_a_revision(store):
    """A CI service account's per-build test-results table was injected as
    operator feedback and resumed the task straight into the budget gate —
    one wasted attempt per PR. Bot chatter must advance the cursor (never
    reconsidered) without resuming.

    The account name and the build's own numbers are deliberately generic: a
    real one names an employer's CI estate, and a test about ignoring bot
    chatter needs the SHAPE of a bot comment, not a transcript of one."""
    t = await _approval_task(store)
    t.context["pr_comment_since"] = "2026-07-10T00:00:00Z"
    await store.update_task(t)
    bots = [
        _Comment("ci-results-bot", "## Unit Test Results\n42 passed", "2026-07-10T11:15:52Z"),
        _Comment("renovate[bot]", "dep dashboard", "2026-07-10T11:16:00Z"),
    ]
    events = []
    out = await _watcher(store, comments=bots, events=events, ignore_authors=["ci-results-bot"])._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert not fresh.context.get("send_back_feedback")
    assert fresh.context["pr_comment_since"] == "2026-07-10T11:16:00Z"
    assert any(k == "pr_feedback_skipped" for k, _ in events)
    # The skip is persisted as a task event even with no host callback wired —
    # the server ran with a completely silent watcher for exactly this reason.
    persisted = await store.list_events(t.id)
    assert any(e["kind"] == "pr_feedback_skipped" and e["source"] == "watcher"
               for e in persisted)


async def test_human_comment_mixed_with_bot_chatter_injects_only_the_human(store):
    t = await _approval_task(store)
    t.context["pr_comment_since"] = "2026-07-10T00:00:00Z"
    await store.update_task(t)
    mixed = [
        _Comment("ci-results-bot", "## Unit Test Results", "2026-07-10T11:15:52Z"),
        _Comment("dev", "please rename the stage", "2026-07-10T11:20:00Z"),
    ]
    out = await _watcher(store, comments=mixed,
                         ignore_authors=["ci-results-bot"])._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    fb = fresh.context["send_back_feedback"]
    assert len(fb) == 1
    assert "rename the stage" in fb[0]["message"]


async def test_pending_or_green_checks_do_nothing(store):
    t = await _approval_task(store)
    for checks in ([], [{**FAIL_CHECK, "status": "pending"}],
                   [{**FAIL_CHECK, "status": "pass"}]):
        assert await _watcher(store, checks=checks)._check_open_pr(t) is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_an_idle_tick_leaves_a_throttled_heartbeat(store):
    """A healthy parked task produces no action events, which used to be
    indistinguishable from a dead watcher. The tick now persists one
    wake_tick per task per hour — proof of life without event spam."""
    t = await _approval_task(store)
    w = _watcher(store)  # green checks, no comments: nothing to do
    await w.tick()
    await w.tick()  # immediately again — must not duplicate
    evs = [e for e in await store.list_events(t.id) if e["kind"] == "wake_tick"]
    assert len(evs) == 1
    assert evs[0]["source"] == "watcher"
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_unknown_state_never_closes_or_completes(store):
    """gh missing / network down ⇒ state "" — must fall through, not act."""
    t = await _approval_task(store)
    out = await _watcher(store, state="")._check_open_pr(t)
    assert out is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_the_products_own_comment_never_resumes_its_task(store):
    """The 2026-07-10 incident: the CI_GATE results comment — posted under the
    OPERATOR's gh login — was injected as human feedback and resumed the
    parked task straight into the budget gate. Author identity cannot catch
    this; the body marker must."""
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER
    t = await _approval_task(store)
    t.context["pr_comment_since"] = "2026-07-10T00:00:00Z"
    await store.update_task(t)
    own = _Comment(
        "dev",  # the operator's own login — NOT a bot author
        f"{AGENT_COMMENT_MARKER}\n🧪 **CI_GATE Integration Test Results** "
        "(no_human)\n\n✅ **SUCCESS** — pipeline 7008",
        "2026-07-10T18:56:51Z",
    )
    events = []
    out = await _watcher(store, comments=[own], events=events)._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert not fresh.context.get("send_back_feedback")
    # Cursor advanced so the same comment is never reconsidered.
    assert fresh.context["pr_comment_since"] == "2026-07-10T18:56:51Z"
    assert any(k == "pr_feedback_skipped" for k, _ in events)


async def test_a_real_operator_comment_still_resumes(store):
    """The marker filter must not eat genuine feedback — even feedback that
    QUOTES agent output (the rendered comment never carries the invisible
    marker)."""
    t = await _approval_task(store)
    t.context["pr_comment_since"] = "2026-07-10T00:00:00Z"
    await store.update_task(t)
    real = _Comment(
        "dev",
        "The CI_GATE results look good but please rename the namespace",
        "2026-07-10T19:00:00Z",
    )
    out = await _watcher(store, comments=[real])._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert "rename the namespace" in fresh.context["send_back_feedback"][0]["message"]


async def test_stalled_active_task_escalates_honestly(store):
    """2026-07-11: a hung Agent-SDK reviewer left a task in 'reviewing'
    forever, holding a worker slot and never failing. The watchdog escalates
    a task with no event past the threshold."""
    import time
    t = Task.new("stalled", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    # An event 50 minutes old (> the 40-min default threshold).
    await store.save_events(t.id, [{"source": "orchestrator", "kind": "state",
                                    "text": "reviewing", "ts": time.time() - 3000}])
    events = []
    w = _watcher(store, events=events)
    # The sweep judges only worker-claimed tasks — a hung session HOLDS a slot.
    actions = await w.tick(active_ids={t.id})
    assert (t.id, "escalated_stalled") in actions
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert "hung Agent-SDK" in (fresh.blocker or {}).get("root_cause_hypothesis", "")
    assert "stalled" in (fresh.blocker or {}).get("question", "")
    assert any(k == "escalated_stalled" for k, _ in events)


async def test_recently_active_task_is_not_escalated(store):
    """A task mid-run (recent events, or a long test <40m) must NOT trip the
    watchdog — no false positives on legitimately slow work."""
    import time
    t = Task.new("working", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.TESTING, validate=False)
    await store.save_events(t.id, [{"source": "agent", "kind": "tool_use",
                                    "text": "running tests", "ts": time.time() - 600}])
    actions = await _watcher(store).tick(active_ids={t.id})
    assert (t.id, "escalated_stalled") not in actions
    assert (await store.get_task(t.id)).status is TaskStatus.TESTING
