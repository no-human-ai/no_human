"""Bugfix: a human's PR review comment was skipped for the whole tick whenever
the conflict rung acted first (task 1e5583dc / PR #593, measured 2026-08-21) —
the comment rung is the only place that advances `pr_comment_since` and
injects human PR comments into `send_back_feedback`, but the old rung ladder
ran the conflict rung first with an early return, so the comment rung never
ran that tick. This file pins the chosen fix: the comment rung always injects
FIRST in inject-only mode, then whichever rung ends the tick carries both
payloads in one resume; a deferral (when nothing consumes the injected
findings this tick) emits a named `pr_feedback_deferred` event."""

from __future__ import annotations

import inspect
import re

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.task import Task, TaskStatus
from no_human.vcs import derived_conflict as dc
from no_human.vcs.pr_watcher import PrComment


@pytest.fixture(autouse=True)
def _resolvable_conflicting_paths(monkeypatch):
    """Same stub as `test_wake_conflict.py`: this file drives the conflict rung
    against a fake, non-existent `repo_path` ("/tmp/x"), so real conflicting-
    path enumeration must be stubbed to a fixed, non-derived path or every test
    here would exercise the enumeration-failure branch instead of the "real
    source conflict" branch its assertions are written against."""
    async def fake_conflicting_paths(repo_path, base_tip, branch):
        return {"src/unrelated.py"}
    monkeypatch.setattr(dc, "conflicting_paths", fake_conflicting_paths)


async def _approval_task(store, url="https://code.example.com/dev/x/pull/593"):
    t = Task.new("conflict", repo_path="/tmp/x")
    t.context = {"pr_watch": url, "pr_branch": "scratch/x", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, mergeable_sequence=None, mergeable=None, merge_state="",
             comments=None, events=None):
    """Composed from `test_wake_conflict.py::_watcher` (the `pr_mergeable`
    wiring) and `test_pr_ci_watch.py::_watcher` (the `pr_comment` wiring) —
    this is the only rung pair this bugfix concerns, so no other checker is
    wired."""
    seq = list(mergeable_sequence) if mergeable_sequence is not None else None

    async def pr_mergeable(url):
        nonlocal seq
        if seq is not None:
            value = seq.pop(0) if seq else (mergeable or "")
        else:
            value = mergeable or ""
        return {"mergeable": value, "mergeStateStatus": merge_state}

    async def pr_comment(url):
        return comments or []

    return WakeWatcher(
        store, {},
        pr_mergeable=pr_mergeable,
        pr_comment=pr_comment,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


# --------------------------------------------------------------------------- #
# AC 1 — combined payload
# --------------------------------------------------------------------------- #

async def test_conflicting_pr_with_fresh_human_comment_resumes_with_both_payloads(store):
    t = await _approval_task(store)
    human = PrComment(author="dev", body="please rename the helper",
                       created_at="2026-08-21T20:47:10Z")
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 comments=[human])
    out = await w._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    fb = fresh.context["send_back_feedback"]
    assert len(fb) == 2, (
        "RED on current main: only the pr_conflict entry is present because "
        "the conflict rung acted first and returned before the comment rung "
        "ever ran"
    )
    sources = {e["source"] for e in fb}
    assert sources == {"pr_comment", "pr_conflict"}
    comment_entry = next(e for e in fb if e["source"] == "pr_comment")
    assert "please rename the helper" in comment_entry["message"]
    conflict_entry = next(e for e in fb if e["source"] == "pr_conflict")
    assert "rebase" in conflict_entry["message"].lower()


async def test_the_comment_cursor_advances_so_the_same_comment_is_not_reinjected(store):
    t = await _approval_task(store)
    human = PrComment(author="dev", body="please rename the helper",
                       created_at="2026-08-21T20:47:10Z")
    w = _watcher(store, mergeable_sequence=["CONFLICTING", "MERGEABLE"],
                 merge_state="DIRTY", comments=[human])

    assert await w._check_open_pr(t) == "resumed"
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    out = await w._check_open_pr(t)
    # The conflict is now MERGEABLE (resolved) and the same comment is still
    # returned by the fake `pr_comment` hook, but its `created_at` is no
    # longer newer than the advanced `pr_comment_since` cursor, so neither
    # rung has anything to act on this tick.
    assert out is None
    fresh = await store.get_task(t.id)
    fb = fresh.context["send_back_feedback"]
    comment_entries = [e for e in fb if e["source"] == "pr_comment"]
    assert len(comment_entries) == 1, "the same comment must not be reinjected"
    assert fresh.context.get("pr_comment_since") == "2026-08-21T20:47:10Z"


# --------------------------------------------------------------------------- #
# AC 2 — the chosen precedence is stated at the ordering site
# --------------------------------------------------------------------------- #

def test_the_rung_ordering_site_states_what_the_losing_rung_gives_up():
    src = inspect.getsource(WakeWatcher._check_open_pr)
    assert src.count("GIVES UP") >= 2, (
        "the ordering site must name what BOTH the comment rung and the "
        "conflict rung give up under the chosen precedence"
    )
    assert "conflict rung" in src


# --------------------------------------------------------------------------- #
# AC 3 — a deferral (if the chosen shape still defers anything) is named
# --------------------------------------------------------------------------- #

async def test_a_conflict_escalation_after_injection_emits_a_named_deferral(store):
    t = await _approval_task(store)
    events = []
    # Drive pr_conflict_rounds past max_pr_conflict_rounds (default 3) with no
    # comments in play yet -- these rounds must resume normally.
    w_warmup = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY")
    for n in range(1, 4):
        assert await w_warmup._check_open_pr(t) == "resumed", f"round {n} should resume"
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    # A fresh human comment arrives on the round that will push the conflict
    # rung past its bound: the comment rung injects it (inject-only) THIS
    # tick, but the conflict rung ends the tick by escalating instead of
    # resuming, so the injected findings are not carried into any coder round.
    human = PrComment(author="dev", body="please rename the helper",
                       created_at="2026-08-21T20:47:10Z")
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 comments=[human], events=events)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_conflict"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED

    fb = fresh.context.get("send_back_feedback") or []
    assert any(e["source"] == "pr_comment" for e in fb), (
        "the human's comment is still injected into send_back_feedback even "
        "though the task escalated instead of resuming"
    )

    assert any(k == "pr_feedback_deferred" for k, _ in events)
    persisted = await store.list_events(t.id)
    deferred = [e for e in persisted if e.get("kind") == "pr_feedback_deferred"]
    assert deferred, "the deferral must be persisted, not just callback-emitted"
    assert "escalated_pr_conflict" in deferred[-1]["text"], (
        "the message must name the outcome that consumed the tick instead of "
        "the human's findings"
    )


async def test_no_deferral_event_when_the_round_actually_started(store):
    t = await _approval_task(store)
    human = PrComment(author="dev", body="please rename the helper",
                       created_at="2026-08-21T20:47:10Z")
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 comments=[human], events=events)
    out = await w._check_open_pr(t)
    assert out == "resumed"
    assert not any(k == "pr_feedback_deferred" for k, _ in events), (
        "when the conflict rung's own resume carries both payloads, nothing "
        "was deferred and no deferral event should fire"
    )


def test_the_deferral_kind_is_counted_on_doctors_pr_watch_ladder():
    """`nh doctor` must attribute the deferral to the PR-watch ladder itself.

    This replaces a guard that took an 800-character SOURCE window from
    ``src.index("pr_watch_ladder")`` and asserted the kind appeared somewhere
    inside it. That window spans three MECHANISMS entries (pr_watch_ladder,
    pr_watch_heartbeat, ci_gate_integration) before running into a following
    comment, so the assertion
    passed with the kind registered on a NEIGHBOURING mechanism and removed
    from this one -- verified by mutation on 2026-08-22, which is the exact
    regression it claimed to prevent. MECHANISMS is a module-level structure,
    so the entry is looked up by name, the way doctor.py itself does it.
    """
    from no_human.doctor import MECHANISMS

    kinds = next(k for name, k, _ in MECHANISMS if name == "pr_watch_ladder")
    assert "pr_feedback_deferred" in kinds, (
        "nh doctor's pr_watch_ladder mechanism must count pr_feedback_deferred; "
        f"it counts {kinds}"
    )


# The board-label half of the old assertion now lives in
# web/src/eventLabels.test.mjs, which IMPORTS the mapping and asserts the kind
# resolves to a human label. The guard here read SlideOver.jsx as text and
# matched "pr_feedback_deferred:", which passed with the mapping commented out.


def test_every_pr_watch_ladder_kind_has_a_board_label():
    """Every kind the PR-watch ladder counts must render as a human label.

    This check lives in pytest rather than in the JS suite because MECHANISMS
    is the authoritative list and it is Python. A hardcoded mirror on the JS
    side drifts silently: the first version of the JS test named eight kinds
    while the ladder counts twelve, so commenting out `escalated_ci`'s label
    left a live board defect with the whole JS suite green.

    Fails CLOSED: an unreadable or unrecognisable map is a failure, not a skip.
    """
    from pathlib import Path

    from no_human.doctor import MECHANISMS

    labels = Path(__file__).resolve().parents[1] / "web" / "src" / "eventLabels.js"
    assert labels.is_file(), f"{labels} is missing — the board label map moved"
    text = labels.read_text()

    # This guard owns exactly one question — WHICH kinds must have a label —
    # because MECHANISMS is the authoritative list and it is Python.
    #
    # It deliberately does NOT judge the label's VALUE. A regex over this file
    # reads SOURCE TEXT while the board reads a decoded string, so a Python
    # value-check passes on `"\t"` / `"\u200b"` / `"\n"` (non-empty in source,
    # blank on the board) and is blind to an `Object.assign(EVENT_LABELS, ...)`
    # override after the literal — a reviewer defeated an earlier version with
    # exactly those. Judging the value in the wrong language also made a legal
    # template-literal reformat FAIL a correct tree. The value check therefore
    # lives in web/src/eventLabels.test.mjs, which imports the map and sees the
    # decoded, post-override string. Splitting it that way deletes that whole
    # family rather than patching it case by case.
    #
    # A commented-out mapping does not match, which is the point: a commented
    # mapping is an ABSENT mapping. The optional quote allows `"escalated_ci":`.
    keys = set(re.findall(
        r"^\s{2}\"?([A-Za-z_][A-Za-z0-9_]*)\"?:", text, re.M))
    assert len(keys) > 20, (
        f"only {len(keys)} label key(s) parsed out of {labels.name} — the map's "
        "shape changed and this guard can no longer see it"
    )

    kinds = next(k for name, k, _ in MECHANISMS if name == "pr_watch_ladder")
    missing = [k for k in kinds if k not in keys]
    assert not missing, (
        "pr_watch_ladder kind(s) with no board label, so the timeline renders "
        f"the raw snake_case kind: {missing}"
    )


# --------------------------------------------------------------------------- #
# AC 4 — existing rungs unchanged, bot-only comments still a no-op for the
# comment rung and never block the conflict rung
# --------------------------------------------------------------------------- #

async def test_bot_only_comments_do_not_block_the_conflict_rung(store):
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER

    t = await _approval_task(store)
    own = PrComment(
        author="dev",  # the operator's own login posts the product's own
        # comment, so an author check alone can't distinguish it -- see
        # test_pr_ci_watch.py's self-comment loop tests.
        body=f"{AGENT_COMMENT_MARKER}\nresults",
        created_at="2026-08-21T19:05:41Z",
    )
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 comments=[own], events=events)
    out = await w._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    fb = fresh.context["send_back_feedback"]
    assert len(fb) == 1, "a bot/self comment must not be injected as feedback"
    assert fb[0]["source"] == "pr_conflict"
    assert not any(k == "pr_feedback_deferred" for k, _ in events), (
        "nothing was injected, so there is nothing to defer"
    )
