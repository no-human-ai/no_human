"""SCRUM-41: a PR that textually conflicts with main is invisible to CI (branch
checks only run the PR's own branch) — live occurrence: PR #26 conflicted with
#25 and sat invisible until a human tried to merge it. This rung polls
`gh pr view --json mergeable,mergeStateStatus` directly and bounds the
rebase→repoll cycle the same way the CI-fix rung bounds its own rounds."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus
from no_human.vcs import derived_conflict as dc


@pytest.fixture(autouse=True)
def _resolvable_conflicting_paths(monkeypatch):
    """This file drives `_check_open_pr`/`_check_pr_conflict` against a fake,
    non-existent `repo_path` ("/tmp/x") -- it exists to test round-counting,
    bound enforcement, and shipped-check logic, never conflicting-path
    enumeration itself (that is `test_orchestrator_pr_conflict.py`'s job,
    against real from-scratch git repos). Since a real `/tmp/x` cannot be
    enumerated, `conflicting_paths()` would return `None` for every test here
    -- which is now, correctly, an enumeration failure that escalates rather
    than falls through to a coder round (the bugfix this file's sibling
    covers). Stub it to a fixed, non-derived path so every test below
    exercises exactly the "enumeration succeeded, real source conflict"
    branch its assertions were written against, unchanged."""
    async def fake_conflicting_paths(repo_path, base_tip, branch):
        return {"src/unrelated.py"}
    monkeypatch.setattr(dc, "conflicting_paths", fake_conflicting_paths)


async def _approval_task(store, url="https://code.example.com/dev/x/pull/26"):
    t = Task.new("conflict", repo_path="/tmp/x")
    # `base_branch` is what the orchestrator persists before any attempt runs
    # (`orchestrator.py`: `if not ctx.get("base_branch")` → `_implicit_base_
    # branch`, saved to the store), so every task that can reach
    # AWAITING_APPROVAL carries one. The content check refuses to guess it.
    t.context = {"pr_watch": url, "pr_branch": "scratch/x", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, mergeable_sequence=None, mergeable=None, merge_state="",
             events=None, pr_shipped=None):
    """``mergeable_sequence`` (a list) is popped from the front on each call —
    lets a test script CONFLICTING → UNKNOWN → CONFLICTING across ticks.
    ``mergeable`` is a fixed single value used when no sequence is given."""
    seq = list(mergeable_sequence) if mergeable_sequence is not None else None

    async def pr_mergeable(url):
        nonlocal seq
        if seq is not None:
            value = seq.pop(0) if seq else (mergeable or "")
        else:
            value = mergeable or ""
        return {"mergeable": value, "mergeStateStatus": merge_state}

    return WakeWatcher(
        store, {},
        pr_mergeable=pr_mergeable,
        pr_shipped=pr_shipped,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


async def test_conflicting_sends_back_a_rebase_instruction_and_resumes(store):
    t = await _approval_task(store)
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY", events=events)
    out = await w._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    fb = fresh.context["send_back_feedback"]
    assert len(fb) == 1
    assert fb[-1]["source"] == "pr_conflict"
    assert "rebase" in fb[-1]["message"].lower()
    assert "conflict" in fb[-1]["message"].lower()
    assert fresh.context["pr_conflict_rounds"] == 1
    assert any(k == "pr_conflict" for k, _ in events)


async def test_unknown_mergeable_is_a_pure_noop(store):
    t = await _approval_task(store)
    events = []
    w = _watcher(store, mergeable="UNKNOWN", events=events)
    out = await w._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert not fresh.context.get("send_back_feedback")
    assert "pr_conflict_rounds" not in (fresh.context or {})
    assert not any(k in ("pr_conflict", "escalated_pr_conflict") for k, _ in events)


async def test_empty_mergeable_poll_miss_is_a_pure_noop(store):
    """gh missing / network error ⇒ mergeable "" — must never act."""
    t = await _approval_task(store)
    w = _watcher(store, mergeable="")
    out = await w._check_open_pr(t)
    assert out is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_mergeable_state_is_a_pure_noop_when_never_conflicted(store):
    t = await _approval_task(store)
    w = _watcher(store, mergeable="MERGEABLE")
    out = await w._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert "pr_conflict_rounds" not in (fresh.context or {})


async def test_no_pr_mergeable_hook_wired_is_a_noop(store):
    """The rung must be inert (not crash) when the host never wired the hook."""
    t = await _approval_task(store)
    w = WakeWatcher(store, {})
    out = await w._check_open_pr(t)
    assert out is None


async def test_a_repeat_conflicting_poll_is_a_second_round(store):
    t = await _approval_task(store)
    w = _watcher(store, mergeable="CONFLICTING")
    assert await w._check_open_pr(t) == "resumed"
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    assert await w._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 2
    assert len(fresh.context["send_back_feedback"]) == 2


async def test_bound_enforcement_escalates_naming_pr_and_merge_state(store):
    url = "https://code.example.com/dev/x/pull/26"
    t = await _approval_task(store, url=url)
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY")
    for n in range(1, 4):
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        assert await w._check_open_pr(t) == "resumed", f"round {n} should resume"
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_conflict"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    question = (fresh.blocker or {}).get("question", "")
    assert url in question
    assert "DIRTY" in question
    assert fresh.context["pr_conflict_rounds"] == 4


async def test_interleaved_unknown_between_conflicts_does_not_reset_the_bound(store):
    """The exact gap the reviewer flagged: CONFLICTING resumes the coder, the
    coder rebases and pushes, the very next tick sees UNKNOWN while GitHub
    recomputes mergeability, then it settles back to CONFLICTING. The round
    counter must keep climbing through the UNKNOWN tick and still escalate at
    the bound — not reset on every rebase cycle and loop forever."""
    t = await _approval_task(store)
    w = _watcher(store, mergeable_sequence=[
        "CONFLICTING",  # round 1 -> resumed
        "UNKNOWN",      # GitHub recomputing after the rebase push -> no-op
        "CONFLICTING",  # round 2 -> resumed
        "UNKNOWN",      # recomputing again -> no-op
        "CONFLICTING",  # round 3 -> resumed
        "UNKNOWN",      # recomputing again -> no-op
        "CONFLICTING",  # round 4 -> past bound -> escalate
    ])

    async def _tick():
        nonlocal t
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        return await w._check_open_pr(t)

    assert await _tick() == "resumed"          # round 1
    assert await _tick() is None                # UNKNOWN: no-op
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 1, "UNKNOWN must not reset the counter"

    assert await _tick() == "resumed"           # round 2
    assert await _tick() is None                # UNKNOWN: no-op
    assert await _tick() == "resumed"           # round 3
    assert await _tick() is None                # UNKNOWN: no-op
    out = await _tick()                          # round 4: past bound
    assert out == "escalated_pr_conflict"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert fresh.context["pr_conflict_rounds"] == 4


async def test_mergeable_after_conflict_resets_the_round_counter(store):
    """A resolved conflict followed by a NEW conflict is a distinct failure
    cycle — the round counter starts over once GitHub confirms MERGEABLE."""
    t = await _approval_task(store)
    w = _watcher(store, mergeable_sequence=["CONFLICTING", "MERGEABLE", "CONFLICTING"])

    async def _tick():
        nonlocal t
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        return await w._check_open_pr(t)

    assert await _tick() == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 1

    assert await _tick() is None  # MERGEABLE: resolved, resets
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 0

    assert await _tick() == "resumed"  # a fresh conflict cycle
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 1


async def test_merged_pr_skips_the_conflict_rung_entirely(store):
    """A merged PR must go straight to DONE without even polling mergeability."""
    t = await _approval_task(store)
    polled = []

    async def pr_state(url):
        return "MERGED"

    async def pr_mergeable(url):
        polled.append(url)
        return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_mergeable=pr_mergeable)
    out = await w._check_open_pr(t)
    assert out == "merged"
    assert not polled, "a merged PR must never poll mergeability"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_closed_pr_skips_the_conflict_rung_entirely(store):
    t = await _approval_task(store)
    polled = []

    async def pr_state(url):
        return "CLOSED"

    async def pr_mergeable(url):
        polled.append(url)
        return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_mergeable=pr_mergeable)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed"
    assert not polled, "a closed PR must never poll mergeability"
    assert (await store.get_task(t.id)).status is TaskStatus.ESCALATED


async def test_task_without_a_pr_link_is_left_untouched(store):
    t = Task.new("no-pr", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    polled = []

    async def pr_mergeable(url):
        polled.append(url)
        return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    w = WakeWatcher(store, {}, pr_mergeable=pr_mergeable)
    out = await w._check_open_pr(t)
    assert out is None
    assert not polled
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_gh_poll_error_is_a_noop_not_a_crash(store):
    t = await _approval_task(store)

    async def pr_mergeable(url):
        raise RuntimeError("network error")

    w = WakeWatcher(store, {}, pr_mergeable=pr_mergeable)
    out = await w._check_open_pr(t)
    assert out is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# The shipped-first guard (2026-08-11). Measured live TWICE that day: a
# CONFLICTING PR whose content was ALREADY on main woke its task for a full
# rebase round — an entire coder attempt (session + tests + review + delivery,
# millions of tokens, ~1h wall) that was moot at birth. GitHub's `mergeable`
# is computed asynchronously and CACHED against the base tip it last saw, so a
# CONFLICTING verdict routinely outlives the landing that resolved it; the
# local content check is computed NOW against the freshly fetched tip. When the
# two disagree in that direction the round has nothing left to do.
# --------------------------------------------------------------------------- #


def _shipped_checker(answer, *, calls=None):
    """A `pr_shipped` hook returning `answer`; records its call args.
    `answer` may be an exception instance, which is raised instead."""
    async def pr_shipped(repo_path, branch, base):
        if calls is not None:
            calls.append((repo_path, branch, base))
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return pr_shipped


async def test_conflicting_but_content_already_on_base_completes_without_a_round(store):
    """(a) The defect itself: content contained ⇒ no rebase round at all."""
    t = await _approval_task(store)
    events, calls = [], []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 events=events, pr_shipped=_shipped_checker(True, calls=calls))
    out = await w._check_open_pr(t)

    assert out == "shipped_pr_conflict"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    # The round must not have been started OR counted.
    assert "pr_conflict_rounds" not in (fresh.context or {})
    assert not (fresh.context or {}).get("send_back_feedback")
    # …and the wake must not be a coder round.
    kinds = [k for k, _ in events]
    assert "pr_conflict" not in kinds
    assert "resumed" not in kinds
    assert "shipped" in kinds
    # It asked the content question about the right refs.
    assert calls == [("/tmp/x", "scratch/x", "main")]


async def test_shipped_completion_uses_the_same_event_kind_as_the_closed_rung(store):
    """ONE completion path, not two: the conflict rung emits the same
    `shipped` event kind the CLOSED rung has always emitted, so anything
    keying on it sees a single vocabulary."""
    t = await _approval_task(store)
    events = []
    w = _watcher(store, mergeable="CONFLICTING", events=events,
                 pr_shipped=_shipped_checker(True))
    assert await w._check_open_pr(t) == "shipped_pr_conflict"
    text = next(text for kind, text in events if kind == "shipped")
    assert "already on main" in text
    assert t.id[:8] in text


async def test_conflicting_with_content_absent_starts_the_rebase_round(store):
    """(b) Negative shipped check ⇒ today's behaviour, byte-for-byte."""
    t = await _approval_task(store)
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 events=events, pr_shipped=_shipped_checker(False))
    out = await w._check_open_pr(t)

    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context["pr_conflict_rounds"] == 1
    assert len(fresh.context["send_back_feedback"]) == 1
    assert "rebase" in fresh.context["send_back_feedback"][-1]["message"].lower()
    assert any(k == "pr_conflict" for k, _ in events)


async def test_shipped_check_error_is_inconclusive_and_keeps_the_round(store):
    """(c) A raising checker is 'cannot tell', never 'shipped' — and it must
    not crash the watcher either."""
    t = await _approval_task(store)
    w = _watcher(store, mergeable="CONFLICTING",
                 pr_shipped=_shipped_checker(RuntimeError("git exploded")))
    assert await w._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context["pr_conflict_rounds"] == 1


async def test_no_pr_shipped_hook_keeps_the_round(store):
    """(c) A host that never wired the hook keeps the old behaviour."""
    t = await _approval_task(store)
    w = _watcher(store, mergeable="CONFLICTING", pr_shipped=None)
    assert await w._check_open_pr(t) == "resumed"
    assert (await store.get_task(t.id)).context["pr_conflict_rounds"] == 1


async def test_task_without_a_branch_recorded_cannot_complete(store):
    """(d) The completion path's own precondition, preserved exactly: with no
    `pr_branch` there is nothing to ask git about, so the guard must not fire
    — and must not invent an answer either."""
    t = Task.new("conflict", repo_path="/tmp/x")
    # A base IS recorded — otherwise this test passes for the wrong reason
    # (the base precondition below would short-circuit it, and a mutant that
    # deletes the branch check would survive; it did, until 2026-08-11).
    t.context = {"pr_watch": "https://code.example.com/dev/x/pull/26",
                 "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    calls = []
    w = _watcher(store, mergeable="CONFLICTING",
                 pr_shipped=_shipped_checker(True, calls=calls))
    assert await w._check_open_pr(t) == "resumed"
    assert calls == [], "no branch ⇒ the content question is unanswerable"
    assert (await store.get_task(t.id)).context["pr_conflict_rounds"] == 1


async def test_a_task_without_a_recorded_base_cannot_complete(store):
    """(d) The base is never GUESSED. `main` used to be the fallback, which is
    silently wrong for a backport: a PR targeting `release/2.3` would be asked
    about `main`, where the content usually IS present, and the task would
    complete with its work never having reached its actual base. The
    orchestrator persists the resolved base before any attempt runs, so this
    is unreachable for a real task — and if it is ever reached, the honest
    answer is 'I cannot tell', not a guess."""
    t = Task.new("conflict", repo_path="/tmp/x")
    t.context = {"pr_watch": "https://code.example.com/dev/x/pull/26",
                 "pr_branch": "scratch/x"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    calls = []
    w = _watcher(store, mergeable="CONFLICTING",
                 pr_shipped=_shipped_checker(True, calls=calls))
    assert await w._check_open_pr(t) == "resumed"
    assert calls == [], "no base ⇒ the content question must not be asked at all"
    assert (await store.get_task(t.id)).context["pr_conflict_rounds"] == 1


async def test_a_task_that_never_reached_approval_is_never_completed(store):
    """(d) The load-bearing precondition is the STATUS: `_check_open_pr` (and
    therefore this guard) is reachable only from AWAITING_APPROVAL, which a
    task reaches only after its review passed and its PR was opened. A BLOCKED
    task with a conflicting PR and fully-landed content is still never
    completed by this watcher."""
    t = Task.new("conflict", repo_path="/tmp/x")
    t.context = {"pr_watch": "https://code.example.com/dev/x/pull/26",
                 "pr_branch": "scratch/x"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.BLOCKED, validate=False)
    calls = []
    w = _watcher(store, mergeable="CONFLICTING",
                 pr_shipped=_shipped_checker(True, calls=calls))
    out = await w._evaluate(t, now=datetime.now(timezone.utc))
    assert out is None
    assert calls == []
    assert (await store.get_task(t.id)).status is TaskStatus.BLOCKED


@pytest.mark.parametrize("state", ["MERGEABLE", "UNKNOWN", ""])
async def test_the_content_check_is_bounded_to_conflicting_ticks(store, state):
    """(a) Cost bound: the shipped check runs local git subprocesses, so it is
    gated behind the same CONFLICTING verdict the rung already waits for —
    never on the ticks that make up virtually all of a parked PR's life."""
    t = await _approval_task(store)
    calls = []
    w = _watcher(store, mergeable=state,
                 pr_shipped=_shipped_checker(True, calls=calls))
    await w._check_open_pr(t)
    assert calls == [], f"mergeable={state!r} must not pay for a content check"


# --------------------------------------------------------------------------- #
# The post-probe terminal guard (review finding D). The content probe is a
# MULTI-SECOND await — several local git subprocesses — sitting between the
# rung's own terminal recheck and every write it goes on to make. A `POST
# /shipped` or `/cancel` landing in that window must abort the whole tick, on
# EVERY answer the probe can give, not just the positive one: the store's CAS
# refuses the status flip alone, so a round counter, a send-back, an event and
# an outcome row would all still land on a task that is already DONE.
# --------------------------------------------------------------------------- #


def _shipped_then_terminal(store, task, answer):
    """A `pr_shipped` hook that flips the task DONE *while it runs* — the
    concurrent-completion race, made deterministic."""
    async def pr_shipped(repo_path, branch, base):
        fresh = await store.get_task(task.id)
        await store.set_status(fresh, TaskStatus.DONE, validate=False,
                               event={"source": "test", "kind": "test_seed"})
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return pr_shipped


@pytest.mark.parametrize("answer", [False, True, RuntimeError("git exploded")],
                         ids=["absent", "landed", "probe-raised"])
async def test_going_terminal_during_the_probe_writes_nothing(store, answer):
    t = await _approval_task(store)
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 events=events,
                 pr_shipped=_shipped_then_terminal(store, t, answer))

    out = await w._check_pr_conflict(t, "https://code.example.com/dev/x/pull/26",
                                     "OPEN")

    assert out is None, "a task that went terminal mid-probe must abort the tick"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    # Not one of the rung's writes may have landed on the finished task.
    assert "pr_conflict_rounds" not in (fresh.context or {})
    assert not (fresh.context or {}).get("send_back_feedback")
    assert await store.list_pr_outcomes(task_id=t.id) == []
    kinds = {e.get("kind") for e in await store.list_events(t.id)}
    assert not (kinds & {"pr_conflict", "resumed", "shipped",
                         "escalated_pr_conflict"}), kinds
    assert events == []


async def test_the_completed_conflict_task_records_a_settled_merged_outcome(store):
    """Review finding E, end to end: the row the completion writes must carry
    the containment evidence, not a bare `open` that nothing would ever
    revisit (`refresh_outcomes` probes containment for CLOSED PRs only)."""
    from no_human.vcs import pr_outcome as po

    t = await _approval_task(store)
    w = _watcher(store, mergeable="CONFLICTING",
                 pr_shipped=_shipped_checker(True))
    assert await w._check_open_pr(t) == "shipped_pr_conflict"

    rows = await store.list_pr_outcomes(task_id=t.id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == po.MERGED
    assert rows[0]["outcome_evidence"] == po.EVIDENCE_CONTENT_ON_BASE
    assert po.is_settled(rows[0]["outcome"]), "a landing must not stay unsettled"
