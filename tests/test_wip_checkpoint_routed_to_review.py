"""A resumed attempt's `[WIP-BLOCKED]` OR `[WIP-PARTIAL]` head must reach a
reviewer, never a zero-diff terminal — three incidents, all 2026-09-08.

`_run_attempt` has two zero-diff terminals: the ALREADY-SATISFIED claim gate
(`_gate_already_satisfied`, mode="already_satisfied") and the silent
`_NO_CHANGES_DETAIL` ("agent produced no file changes") fall-through. Before
this fix, a `wake`/machine resume that branched from its OWN `[WIP-BLOCKED]`
or `[WIP-PARTIAL]` checkpoint could reach either one with an unjudged diff
still sitting at head:

* task 0847f2c2 (the claim terminal): the coder correctly added nothing and
  filed a fully-cited ALREADY-SATISFIED claim. The old
  `_already_satisfied_eligible` read `wake` as eligible on provenance alone
  (it is not in `MACHINE_REQUEUE_PROVENANCE`), so the claim reached
  `_gate_already_satisfied` — which structurally refuses a `[WIP-BLOCKED]`
  subject (`_already_satisfied_subject`) — and the attempt failed with
  "already-satisfied claim refused" instead of reviewing the work already on
  the branch.
* task d256ae60 (the silent terminal): same shape, but the coder's final
  text did not parse as a claim at all, so `claim is None`, eligibility was
  never even asked, and the attempt fell straight to `_NO_CHANGES_DETAIL`
  — twice — escalating on a false "already satisfied" hypothesis.
* the neighbouring 2026-09-08 incident, same date and shape, on the
  `[WIP-PARTIAL]` subject instead: `_already_satisfied_subject` refuses
  `[WIP-BLOCKED]` and `[WIP-PARTIAL]` identically off the ship ref (~10674),
  so a checkpoint parked mid-coder-turn by a wake/quota resume burns the
  same two terminals the same way. Round 3 widens the fix's own predicate,
  `_head_is_wip_checkpoint` (renamed from `_head_is_blocked_checkpoint`), to
  match both prefixes instead of just one.

The fix hoists one check, `_route_unjudged_head` (via `_already_satisfied_
eligible`, now keyed on the HEAD's own shape — its checkpoint subject and its
review stamp — not on provenance alone), before BOTH terminals. Ineligible
heads are routed straight to a full independent review instead. This file
reproduces all three incidents, pins the routing across every `resume_from.by`
value, and guards the escapes the fix must NOT touch: a genuinely no-diff
resume must still take the claim gate, and a head a completed review already
passed (exact sha, not an ancestor) must still be credited.
"""
from __future__ import annotations

import subprocess

import pytest

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

from .test_e2e_orchestrator import (  # noqa: F401
    ChecklistItem, FakeReviewer, ReviewDecision, _config, _git, bare_repo,
)
from .test_resume_wiring import ScriptedBackend, _ok  # noqa: F401
from .test_resume_wiring_round2 import _commit_on_main  # noqa: F401


def _blocked_checkpoint(bare_repo, *, filename="feature.py",
                        body="def feature():\n    return 1\n"):
    """The live shape (0847f2c2 / d256ae60): a `[WIP-BLOCKED]` checkpoint one
    commit ahead of the base branch, with no review verdict ever recorded
    against it — a quota/human park mid-attempt, not the loop's own
    abandoned partial.

    Deliberately does NOT push the checkpoint itself to `origin/main`: doing
    so would leave the checkpoint ON the ship ref, and `_already_satisfied_
    subject`'s `on_ship_ref` check (`repo.is_ancestor`, ~10660) accepts
    ANY subject once the head is on the ship ref, bypassing the very
    `[WIP-BLOCKED]` refusal these incidents depend on. `origin/main` is left
    at the checkpoint's PARENT (the state the `bare_repo` fixture already
    pushed), so the checkpoint sits one commit ahead of it, off the ship ref
    — the actual incident shape."""
    sha = _commit_on_main(bare_repo, filename, body,
                          "[WIP-BLOCKED] parked by the quota wall")
    _git(bare_repo, "branch", "-f", "base-behind", "HEAD~1")
    _git(bare_repo, "push", "origin", "base-behind")
    return sha


def _partial_checkpoint(bare_repo, *, filename="feature.py",
                        body="def feature():\n    return 1\n"):
    """Same shape as `_blocked_checkpoint`, on the neighbouring subject: a
    `[WIP-PARTIAL]` checkpoint marks a wake/quota park mid-coder-turn rather
    than a quota/human park, but `_already_satisfied_subject`'s refusal
    (~10674) reads both prefixes identically off the ship ref — same
    incident shape, same fix, same test shape as `_blocked_checkpoint`.
    `origin/main` is left at the checkpoint's PARENT for the identical
    reason: staying off the ship ref keeps the `[WIP-PARTIAL]` refusal
    itself in play, rather than being bypassed by the `on_ship_ref` check."""
    sha = _commit_on_main(bare_repo, filename, body,
                          "[WIP-PARTIAL] parked mid-attempt")
    _git(bare_repo, "branch", "-f", "base-behind", "HEAD~1")
    _git(bare_repo, "push", "origin", "base-behind")
    return sha


# --------------------------------------------------------------------------- #
# Incident B — the silent terminal (task d256ae60)                            #
# --------------------------------------------------------------------------- #

async def test_a_wake_resume_from_a_blocked_checkpoint_reviews_it_instead_of_no_file_changes(
        bare_repo, tmp_path, store):
    """Incident d256ae60. A wake resume branches from its own `[WIP-BLOCKED]`
    checkpoint; the coder correctly adds nothing and says so in plain prose
    with no parseable claim. Pre-fix, `claim is None` skipped eligibility
    entirely and the attempt fell straight to `_NO_CHANGES_DETAIL` on a diff
    that plainly exists. The fix must route the unjudged head to a full
    review before that fall-through is ever reached."""
    sha = _blocked_checkpoint(bare_repo)

    def says_nothing_left(cwd):
        return _ok("CI is green now, nothing left to change.")

    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("feature() returns 1", True, "feature.py:2 returns 1")]))
    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(says_nothing_left), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("resumed on a blocked checkpoint, coder is silent",
                 repo_path=str(bare_repo))
    t.acceptance_criteria = ["feature() returns 1"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)
    await store.merge_context(t.id, {
        "resume_from": {"sha": sha, "branch": "main", "by": "wake"},
        "resume_reason": "wake_condition_satisfied",
        "base_branch": "base-behind",
    })

    t = await store.get_task(t.id)
    final = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert not any(
        a["status"] == "failed"
        and a.get("failure_reason") == "agent produced no file changes"
        for a in attempts
    ), [(a["attempt_number"], a["status"], a.get("failure_reason")) for a in attempts]
    assert reviewer.calls, (
        "the unreviewed diff never reached the reviewer at all — the silent "
        "terminal fell straight through to 'no file changes'")
    assert not any(c["mode"] == "already_satisfied" for c in reviewer.calls), (
        f"a silent (non-claim) turn must never reach the claim gate: {reviewer.calls}")
    assert any(c.get("reviewed_sha") == sha for c in reviewer.calls), (
        f"the full review did not judge the checkpoint head {sha}: {reviewer.calls}")
    ineligible_events = [e for e in events if e.get("kind") == "already_satisfied_ineligible"]
    assert ineligible_events, (
        f"no already_satisfied_ineligible event was emitted: {events}")
    assert final.status is TaskStatus.AWAITING_APPROVAL, final.status


# --------------------------------------------------------------------------- #
# Incident A — the claim terminal (task 0847f2c2)                             #
# --------------------------------------------------------------------------- #

async def test_a_fully_cited_claim_over_a_blocked_checkpoint_routes_to_the_full_review(
        bare_repo, tmp_path, store):
    """Incident 0847f2c2. A wake resume branches from its own `[WIP-BLOCKED]`
    checkpoint; the coder correctly adds nothing and files a fully-cited
    ALREADY-SATISFIED claim. Pre-fix, `wake` read as eligible on provenance
    alone, so the claim reached `_gate_already_satisfied` — which
    structurally refuses a `[WIP-BLOCKED]` subject — and the attempt failed
    with "already-satisfied claim refused". The fix routes the head to a
    full review BEFORE the claim is even parsed, so `_gate_already_satisfied`
    (mode="already_satisfied") must never be reached here."""
    sha = _blocked_checkpoint(bare_repo)
    claim = ("Re-checked every criterion after the quota wall cleared.\n"
             "ALREADY-SATISFIED\n"
             "CRITERION: feature() returns 1 — MET — evidence: feature.py:2\n")

    def files_the_claim(cwd):
        return _ok(claim)

    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("feature() returns 1", True, "feature.py:2 returns 1")]))
    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(files_the_claim), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("resumed on a blocked checkpoint, coder files a claim",
                 repo_path=str(bare_repo))
    t.acceptance_criteria = ["feature() returns 1"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)
    await store.merge_context(t.id, {
        "resume_from": {"sha": sha, "branch": "main", "by": "wake"},
        "resume_reason": "wake_condition_satisfied",
        "base_branch": "base-behind",
    })

    t = await store.get_task(t.id)
    final = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert not any(
        a["status"] == "failed"
        and "already-satisfied claim refused" in (a.get("failure_reason") or "")
        for a in attempts
    ), [(a["attempt_number"], a["status"], a.get("failure_reason")) for a in attempts]
    assert not any(c["mode"] == "already_satisfied" for c in reviewer.calls), (
        "a [WIP-BLOCKED] head's claim reached the claim gate, which "
        f"structurally refuses it: {reviewer.calls}")
    assert reviewer.calls, "the claim's diff never reached a full review"
    assert any(c.get("reviewed_sha") == sha for c in reviewer.calls), (
        f"the full review did not judge the checkpoint head {sha}: {reviewer.calls}")
    ineligible_events = [e for e in events if e.get("kind") == "already_satisfied_ineligible"]
    assert ineligible_events, (
        f"no already_satisfied_ineligible event was emitted: {events}")
    assert final.status is TaskStatus.AWAITING_APPROVAL, final.status


# --------------------------------------------------------------------------- #
# The neighbouring incident, same date/shape, on [WIP-PARTIAL] — round 3      #
# --------------------------------------------------------------------------- #

async def test_a_wake_resume_from_a_partial_checkpoint_reviews_it_instead_of_no_file_changes(
        bare_repo, tmp_path, store):
    """The `[WIP-PARTIAL]` sibling of
    `test_a_wake_resume_from_a_blocked_checkpoint_reviews_it_instead_of_no_file_changes`.
    A wake resume branches from its own `[WIP-PARTIAL]` checkpoint (parked
    mid-coder-turn rather than parked by the quota wall); the coder
    correctly adds nothing and says so in plain prose with no parseable
    claim. Before round 3, `_head_is_blocked_checkpoint` matched only
    `[WIP-BLOCKED]`, so this exact shape fell straight through to
    `_NO_CHANGES_DETAIL` on a diff that plainly exists — the same defect as
    incident d256ae60, just on the neighbouring subject."""
    sha = _partial_checkpoint(bare_repo)

    def says_nothing_left(cwd):
        return _ok("CI is green now, nothing left to change.")

    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("feature() returns 1", True, "feature.py:2 returns 1")]))
    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(says_nothing_left), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("resumed on a partial checkpoint, coder is silent",
                 repo_path=str(bare_repo))
    t.acceptance_criteria = ["feature() returns 1"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)
    await store.merge_context(t.id, {
        "resume_from": {"sha": sha, "branch": "main", "by": "wake"},
        "resume_reason": "wake_condition_satisfied",
        "base_branch": "base-behind",
    })

    t = await store.get_task(t.id)
    final = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert not any(
        a["status"] == "failed"
        and a.get("failure_reason") == "agent produced no file changes"
        for a in attempts
    ), [(a["attempt_number"], a["status"], a.get("failure_reason")) for a in attempts]
    assert reviewer.calls, (
        "the unreviewed diff never reached the reviewer at all — the silent "
        "terminal fell straight through to 'no file changes'")
    assert not any(c["mode"] == "already_satisfied" for c in reviewer.calls), (
        f"a silent (non-claim) turn must never reach the claim gate: {reviewer.calls}")
    assert any(c.get("reviewed_sha") == sha for c in reviewer.calls), (
        f"the full review did not judge the checkpoint head {sha}: {reviewer.calls}")
    ineligible_events = [e for e in events if e.get("kind") == "already_satisfied_ineligible"]
    assert ineligible_events, (
        f"no already_satisfied_ineligible event was emitted: {events}")
    assert final.status is TaskStatus.AWAITING_APPROVAL, final.status


async def test_a_fully_cited_claim_over_a_partial_checkpoint_routes_to_the_full_review(
        bare_repo, tmp_path, store):
    """The `[WIP-PARTIAL]` sibling of
    `test_a_fully_cited_claim_over_a_blocked_checkpoint_routes_to_the_full_review`.
    A wake resume branches from its own `[WIP-PARTIAL]` checkpoint; the
    coder correctly adds nothing and files a fully-cited ALREADY-SATISFIED
    claim. Before round 3, `wake` read as eligible for this subject (the
    predicate matched only `[WIP-BLOCKED]`), so the claim reached
    `_gate_already_satisfied` — which structurally refuses a `[WIP-PARTIAL]`
    subject exactly like it refuses `[WIP-BLOCKED]` — and the attempt
    failed with "already-satisfied claim refused", the same defect as
    incident 0847f2c2 on the neighbouring subject."""
    sha = _partial_checkpoint(bare_repo)
    claim = ("Re-checked every criterion after the quota wall cleared.\n"
             "ALREADY-SATISFIED\n"
             "CRITERION: feature() returns 1 — MET — evidence: feature.py:2\n")

    def files_the_claim(cwd):
        return _ok(claim)

    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("feature() returns 1", True, "feature.py:2 returns 1")]))
    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(files_the_claim), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("resumed on a partial checkpoint, coder files a claim",
                 repo_path=str(bare_repo))
    t.acceptance_criteria = ["feature() returns 1"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)
    await store.merge_context(t.id, {
        "resume_from": {"sha": sha, "branch": "main", "by": "wake"},
        "resume_reason": "wake_condition_satisfied",
        "base_branch": "base-behind",
    })

    t = await store.get_task(t.id)
    final = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert not any(
        a["status"] == "failed"
        and "already-satisfied claim refused" in (a.get("failure_reason") or "")
        for a in attempts
    ), [(a["attempt_number"], a["status"], a.get("failure_reason")) for a in attempts]
    assert not any(c["mode"] == "already_satisfied" for c in reviewer.calls), (
        "a [WIP-PARTIAL] head's claim reached the claim gate, which "
        f"structurally refuses it: {reviewer.calls}")
    assert reviewer.calls, "the claim's diff never reached a full review"
    assert any(c.get("reviewed_sha") == sha for c in reviewer.calls), (
        f"the full review did not judge the checkpoint head {sha}: {reviewer.calls}")
    ineligible_events = [e for e in events if e.get("kind") == "already_satisfied_ineligible"]
    assert ineligible_events, (
        f"no already_satisfied_ineligible event was emitted: {events}")
    assert final.status is TaskStatus.AWAITING_APPROVAL, final.status


# --------------------------------------------------------------------------- #
# The routing is keyed on the HEAD, not on provenance alone                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("by", [
    "wake", "human", "consumed_human", "orphan_recovery", "server_stop",
    "hard_kill_salvage",
])
async def test_the_same_routing_applies_to_every_resume_provenance(
        bare_repo, tmp_path, store, by):
    """AC: the `[WIP-BLOCKED]` routing applies regardless of `resume_from.by`
    — not just the three machine provenances already in
    `MACHINE_REQUEUE_PROVENANCE`. `human`/`consumed_human`/`wake` become
    ineligible through `_head_is_wip_checkpoint` (the head's own shape);
    the three machine provenances were already ineligible before this fix.
    Calls `_already_satisfied_eligible` directly — this is a claim about the
    predicate for every provenance, not about one reviewer call sequence."""
    sha = _blocked_checkpoint(bare_repo)
    repo = GitRepo(bare_repo)
    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(lambda cwd: _ok("noop")), SlackNotifier(None))
    task = Task.new("t", repo_path=str(bare_repo))
    task.context = {"resume_from": {"sha": sha, "branch": "main", "by": by}}

    eligible, why = orch._already_satisfied_eligible(task, repo, "base-behind")

    assert not eligible, (by, why)


# --------------------------------------------------------------------------- #
# Escapes the fix must NOT touch                                              #
# --------------------------------------------------------------------------- #

async def test_a_wake_resume_with_no_diff_against_base_still_takes_the_claim_gate(
        bare_repo, tmp_path, store):
    """When base and head are the SAME commit there is no diff to review at
    all — `_already_satisfied_eligible`'s `has_diff` check short-circuits, the
    hoisted `_route_unjudged_head` call is a no-op, and the ordinary
    zero-diff claim gate (`_gate_already_satisfied`, mode="already_satisfied")
    must still run exactly as it did before this fix. Guards against the
    hoist swallowing the one case it must not touch."""
    sha = _commit_on_main(bare_repo, "feature.py", "def feature():\n    return 1\n",
                          "implement the feature")
    _git(bare_repo, "push", "origin", "main")
    _git(bare_repo, "branch", "-f", "base-same", "HEAD")
    _git(bare_repo, "push", "origin", "base-same")

    claim = ("Re-checked every criterion.\n"
             "ALREADY-SATISFIED\n"
             "CRITERION: feature() returns 1 — MET — evidence: feature.py:2\n")

    def files_the_claim(cwd):
        return _ok(claim)

    reviewer = FakeReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("feature() returns 1", True, "feature.py:2 returns 1")]))
    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(files_the_claim), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("resumed, no diff at all", repo_path=str(bare_repo))
    t.acceptance_criteria = ["feature() returns 1"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)
    await store.merge_context(t.id, {
        "resume_from": {"sha": sha, "branch": "main", "by": "wake"},
        "resume_reason": "wake_condition_satisfied",
        "base_branch": "base-same",
    })

    t = await store.get_task(t.id)
    final = await orch.run_task(t)

    assert [c for c in reviewer.calls if c["mode"] == "already_satisfied"], (
        f"a genuinely no-diff resume stopped taking the claim gate: {reviewer.calls}")
    assert final.status is TaskStatus.AWAITING_APPROVAL, final.status


async def test_a_stamped_passing_round_at_this_exact_head_keeps_the_claim_escape(
        bare_repo, tmp_path, store):
    """A `[WIP-BLOCKED]` head is not doomed forever: once a completed review
    has passed EXACTLY this sha, `_already_satisfied_eligible` must credit it
    — the diff already has a verdict. Control: a PASS stamped on the PARENT
    sha (an ancestor of head, not head itself) must NOT cover the head's own
    diff — the exact-sha-equality rule the existing code already relies on,
    not `is_ancestor`."""
    sha = _blocked_checkpoint(bare_repo)
    repo = GitRepo(bare_repo)
    parent = subprocess.run(["git", "rev-parse", f"{sha}~1"], cwd=bare_repo,
                            capture_output=True, text=True).stdout.strip()

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(lambda cwd: _ok("noop")), SlackNotifier(None))
    task = Task.new("stamped", repo_path=str(bare_repo))

    task.context = {
        "resume_from": {"sha": sha, "branch": "main", "by": "wake"},
        "review_history": [{"sha": sha, "passed": True}],
    }
    eligible, why = orch._already_satisfied_eligible(task, repo, "base-behind")
    assert eligible, why

    task.context = {
        "resume_from": {"sha": sha, "branch": "main", "by": "wake"},
        "review_history": [{"sha": parent, "passed": True}],
    }
    eligible, why = orch._already_satisfied_eligible(task, repo, "base-behind")
    assert not eligible, (
        f"a PASS on the parent sha wrongly covered the head's own diff: {why}")


# --------------------------------------------------------------------------- #
# Fail-closed                                                                  #
# --------------------------------------------------------------------------- #

class _ExplodingRepo:
    """A repo double whose every read raises — pins that an unreadable
    head/subject must never buy the claim escape."""

    def head_sha(self):
        raise RuntimeError("head unreadable")

    def commits_ahead(self, base):
        raise RuntimeError("ahead unreadable")

    def _run(self, *args, **kwargs):
        raise RuntimeError("subject unreadable")


async def test_unreadable_head_and_unreadable_subject_fail_closed(tmp_path, store):
    """Two distinct fail-closed paths:
    1. `repo.head_sha()` itself raises -> ineligible outright (can't even ask
       whether there's a diff).
    2. head IS readable, `commits_ahead` raises -> assumed a diff exists (the
       existing rule); if the SUBJECT read then also raises,
       `_head_is_wip_checkpoint` must assume the unsafe (blocked) side,
       not the safe one.
    """
    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(lambda cwd: _ok("noop")), SlackNotifier(None))
    task = Task.new("t", repo_path=str(tmp_path))
    task.context = {"resume_from": {"sha": "x", "branch": "main", "by": "wake"}}

    eligible, why = orch._already_satisfied_eligible(task, _ExplodingRepo(), "base")
    assert not eligible, why

    class _HeadOnly(_ExplodingRepo):
        def head_sha(self):
            return "deadbeef"

    eligible, why = orch._already_satisfied_eligible(task, _HeadOnly(), "base")
    assert not eligible, why
    assert orch._head_is_wip_checkpoint(_HeadOnly(), "deadbeef") is True


# --------------------------------------------------------------------------- #
# Table-driven pin of the predicate itself                                    #
# --------------------------------------------------------------------------- #

def _case_repo(tmp_path, label, subject=None):
    """A fresh local repo (no remote needed — the predicate only reads
    `repo`/`task.context`): one commit tagged `base`, plus an optional
    second commit with `subject` (ahead of `base` by one commit)."""
    work = tmp_path / f"case-{label}"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "base")
    if subject is not None:
        (work / "a.py").write_text("x = 2\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", subject)
    return work


async def test_already_satisfied_eligibility_table(tmp_path, store):
    """Table-driven pin of `_already_satisfied_eligible` itself: ineligible
    iff there IS an unjudged diff AND (the head is a `[WIP-BLOCKED]` OR
    `[WIP-PARTIAL]` checkpoint OR the provenance is a machine requeue).
    Every other combination — no diff at all, an ordinary subject on a
    non-machine provenance (D15) — stays eligible. Round 3: `[WIP-PARTIAL]`
    rows now match `[WIP-BLOCKED]`'s pattern exactly (`partial-wake` flips
    from `True` to `False`; `partial-human`/`partial-consumed-human` are new
    rows mirroring the `blocked-*` rows) — `_already_satisfied_subject`
    refuses both subjects identically off the ship ref, so there is no
    provenance under which only one of them should be eligible."""
    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(lambda cwd: _ok("noop")), SlackNotifier(None))

    cases = [
        ("no-diff-at-all", None, "wake", True),
        ("ordinary-wake", "implement the feature", "wake", True),
        ("ordinary-orphan-recovery", "implement the feature", "orphan_recovery", False),
        ("blocked-wake", "[WIP-BLOCKED] parked", "wake", False),
        ("blocked-human", "[WIP-BLOCKED] parked", "human", False),
        ("blocked-consumed-human", "[WIP-BLOCKED] parked", "consumed_human", False),
        ("partial-wake", "[WIP-PARTIAL] partial", "wake", False),
        ("partial-human", "[WIP-PARTIAL] partial", "human", False),
        ("partial-consumed-human", "[WIP-PARTIAL] partial", "consumed_human", False),
        ("partial-server-stop", "[WIP-PARTIAL] partial", "server_stop", False),
        ("partial-hard-kill-salvage", "[WIP-PARTIAL] partial", "hard_kill_salvage", False),
    ]

    for label, subject, by, expect_eligible in cases:
        work = _case_repo(tmp_path, label, subject)
        repo = GitRepo(work)
        task = Task.new(label, repo_path=str(work))
        task.context = {"resume_from": {"sha": "x", "branch": "main", "by": by}}

        eligible, why = orch._already_satisfied_eligible(task, repo, "base")

        assert eligible is expect_eligible, (
            f"{label}: expected eligible={expect_eligible}, got {eligible} ({why})")
