"""Wiring tests for the merge-ready policy verdict (`core/merge_policy.py`)
through `Orchestrator._finalize` — the module itself (`test_merge_policy.py`)
and the PR-body row it renders from (`test_pr_evidence.py`) are covered
elsewhere; this file proves the pieces are actually WIRED TOGETHER inside a
real `_finalize` run: the verdict is computed from a single evidence-gather,
persisted per-head-sha without clobbering an older entry, emitted as an
event, rendered into the PR body, degrades to an advisory (never a block)
when the evaluator itself raises, and reads the real uncapped advisory count
rather than the capped review-history trail.

Harness pattern (repo setup, `_Backend`, `_orch`, `_git`, `_commit`, `_Commit`,
`_Result`, `_FakePR`, `_landed_receipt`) is adapted from
`tests/test_delivery_pushes_reviewed_sha.py`, the sibling file that already
exercises `Orchestrator._finalize` end-to-end against a real git repo. Two
deliberate departures from that file:

1. `git init -q -b main` (not a bare `git init -q`) — this test suite reads
   `.no_human/merge_policy.yaml` diffs, which need `base="main"` to resolve
   via `git merge-base` rather than silently degrading to `_review_base`'s
   `HEAD~1` fallback (which would still work for a single-extra-commit repo,
   but stops being correct the moment a test needs more than one commit on
   the attempt branch — pinning the branch name keeps every test in this
   file on the same, always-correct code path).
2. `_finalize_task` here takes optional `test_results=`/`review_checklist=`
   kwargs, seeded onto the attempt row via `store.update_attempt` between
   `create_attempt` and `_finalize` — the sibling file never needed this
   because none of its tests depend on gate facts beyond the review stamp.
"""
from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path


from no_human.config import load_config
import no_human.core.merge_policy as merge_policy
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo
from no_human.vcs.receipts import Receipt


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path, events=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(
        store, cfg.data, _Backend(), SlackNotifier(None),
        event_sink=(events.append if events is not None else None))


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_a_commit(tmp_path, *, policy_yaml: str | None = None):
    """A repo with an explicit `main` branch (so `base="main"` always
    resolves via `merge-base`, never the `HEAD~1` fallback) and one commit,
    checked out onto `nh/attempt-1`. When `policy_yaml` is given, it is
    written into that FIRST commit (on `main`) — i.e. present on disk but
    never part of any attempt branch's diff, the shape needed for the
    "policy unchanged" control case.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "a@b.c")
    _git(work, "config", "user.name", "T")
    if policy_yaml is not None:
        d = work / ".no_human"
        d.mkdir(parents=True, exist_ok=True)
        (d / "merge_policy.yaml").write_text(policy_yaml)
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "checkout", "-q", "-b", "nh/attempt-1")
    return work


def _commit(work, name: str, body: str, msg: str) -> str:
    path = work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", msg)
    return _git(work, "rev-parse", "HEAD")


class _Commit:
    files_changed = 1
    insertions = 1
    deletions = 0
    sha = ""


class _Result:
    final_text = "Implemented the change."
    num_turns = 3


class _FakePR:
    kind = "github"

    def __init__(self, url, pushed_sha):
        self.url = url
        self.pushed_sha = pushed_sha


def _landed_receipt(*a, **k):
    return Receipt("pr_open", "https://github.com/o/r/pull/1", "landed", "ok")


def _stamp(ctx: dict, sha: str, *, advisory: list | None = None) -> dict:
    ctx = dict(ctx or {})
    ctx["review_history"] = [
        {"round": 1, "sha": sha, "passed": True, "blocking": [],
         "advisory": advisory or []},
    ]
    return ctx


async def _finalize_task(store, tmp_path, work, ctx, open_pr_stub, monkeypatch, *,
                          events=None, verify_stub=_landed_receipt,
                          test_results=None, review_checklist=None):
    import no_human.core.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "open_pr", open_pr_stub)
    monkeypatch.setattr(orch_mod, "verify_pr_receipt", verify_stub)

    repo = GitRepo(work)
    orch = _orch(store, tmp_path, events=events)
    task = Task.new("Fix the thing", repo_path=str(work))
    task.context = ctx
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)

    fields = {}
    if test_results is not None:
        fields["test_results"] = test_results
    if review_checklist is not None:
        fields["review_checklist"] = review_checklist
    if fields:
        await store.update_attempt(attempt_id, **fields)

    commit = _Commit()
    commit.sha = repo.head_sha()
    out = await orch._finalize(
        task, repo, "nh/attempt-1", "main", commit, attempt_id, _Result())
    return orch, task, attempt_id, out


MINIMAL_POLICY_YAML = (
    "rules:\n"
    "  - tests_ran_and_passed\n"
    "  - tamper_guard_clear\n"
    "  - verifiers_all_satisfied\n"
)


# ---------------------------------------------------------------------------
# Persistence keyed by head sha: a verdict for a DIFFERENT sha survives.
# ---------------------------------------------------------------------------

async def test_a_verdict_for_a_different_sha_survives_the_write(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    stale = {"ready": True, "summary": "stale — from a prior head", "source": "default",
              "problems": [], "policy_changed_in_diff": False, "rules": []}
    ctx = _stamp({"merge_policy": {"deadbeef" * 5: stale}}, reviewed_sha)

    def fake_open_pr(repo, branch, title, body, **kw):
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch)

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    mp = (task.context or {}).get("merge_policy") or {}
    assert "deadbeef" * 5 in mp, mp
    assert mp["deadbeef" * 5] == stale
    assert reviewed_sha in mp, mp


# ---------------------------------------------------------------------------
# Event emission + PR body row rendering, default (no custom) policy.
# ---------------------------------------------------------------------------

async def test_merge_policy_event_and_pr_body_row_default_policy(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha)
    bodies = []

    def fake_open_pr(repo, branch, title, body, **kw):
        bodies.append(body)
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch, events=events)

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail

    mp_events = [e for e in events if e.get("kind") == "merge_policy"]
    assert mp_events, f"no merge_policy event: {events}"
    # DEFAULT_POLICY (no `.no_human/merge_policy.yaml`) has 6 rules; no test
    # evidence was seeded, so `tests_ran_and_passed` is the one failure and
    # `review_passed` is satisfied by the `_stamp` round matching this head.
    assert mp_events[0]["ready"] is False, mp_events[0]
    assert mp_events[0]["text"] == "not ready — 1 of 6 rules failed: tests_ran_and_passed"

    assert bodies, "open_pr was never called"
    body = bodies[0]
    assert ("| Merge policy | ❌ not ready — 1 of 6 rules failed: "
            "tests_ran_and_passed |") in body, body
    assert "<details><summary>Merge-ready policy (6 rules, source: default)" in body
    assert "review_passed" in body and "tamper_guard_clear" in body

    mp = (task.context or {}).get("merge_policy") or {}
    assert reviewed_sha in mp, mp
    assert mp[reviewed_sha]["ready"] is False


# ---------------------------------------------------------------------------
# `_gather_evidence` is called exactly once on the success path.
# ---------------------------------------------------------------------------

async def test_gather_evidence_called_exactly_once_on_success(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha)

    def fake_open_pr(repo, branch, title, body, **kw):
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    import no_human.core.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(orch_mod, "verify_pr_receipt", _landed_receipt)

    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    task = Task.new("Fix the thing", repo_path=str(work))
    task.context = ctx
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)

    count = {"n": 0}
    original = orch._gather_evidence

    def spy(*a, **k):
        count["n"] += 1
        return original(*a, **k)

    orch._gather_evidence = spy

    commit = _Commit()
    commit.sha = repo.head_sha()
    out = await orch._finalize(
        task, repo, "nh/attempt-1", "main", commit, attempt_id, _Result())

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    assert count["n"] == 1, (
        f"expected exactly one _gather_evidence call on the success path, "
        f"got {count['n']}")

    # Extended to the FAILURE path too: `_gather_evidence` is hoisted above
    # the policy `try:` now, so a broken evaluator must not cause a second
    # (re-)gather via `_pr_body`'s own fallback — the whole point of folding
    # the failure onto the SAME `policy_evidence` object via `replace(...)`
    # instead of re-gathering from scratch.
    ctx2 = _stamp({}, reviewed_sha)
    task2 = Task.new("Fix the thing", repo_path=str(work))
    task2.context = ctx2
    await store.create_task(task2)
    await store.set_status(task2, TaskStatus.TESTING, validate=False)
    attempt_id2 = await store.create_attempt(task2.id, 1)

    orch2 = _orch(store, tmp_path)
    count2 = {"n": 0}
    original2 = orch2._gather_evidence

    def spy2(*a, **k):
        count2["n"] += 1
        return original2(*a, **k)

    orch2._gather_evidence = spy2

    def raise_evaluate_repo(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(orch_mod.merge_policy, "evaluate_repo", raise_evaluate_repo)

    commit2 = _Commit()
    commit2.sha = repo.head_sha()
    out2 = await orch2._finalize(
        task2, repo, "nh/attempt-1", "main", commit2, attempt_id2, _Result())

    assert out2.status == TaskStatus.AWAITING_APPROVAL, out2.detail
    assert count2["n"] == 1, (
        f"expected exactly one _gather_evidence call on the FAILURE path too, "
        f"got {count2['n']}")


# ---------------------------------------------------------------------------
# `policy_changed_in_diff`: the ⚠️ override, and its unchanged control case.
# ---------------------------------------------------------------------------

async def test_policy_file_changed_in_this_diff_forces_the_warning_glyph(
    store, tmp_path, monkeypatch,
):
    work = _repo_with_a_commit(tmp_path)  # no policy on main
    _commit(work, ".no_human/merge_policy.yaml", MINIMAL_POLICY_YAML,
            "add merge policy")
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha)
    bodies = []

    def fake_open_pr(repo, branch, title, body, **kw):
        bodies.append(body)
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch, events=events,
        test_results={"ran": True, "passed": 1, "failed": 0})

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    body = bodies[0]
    assert ("| Merge policy | ⚠️ ready — 3 of 3 rules — "
            "POLICY FILE CHANGED IN THIS PR |") in body, body

    mp = (task.context or {}).get("merge_policy") or {}
    verdict = mp.get(reviewed_sha) or {}
    # The rules themselves all passed, but `ready` is forced False — the
    # ⚠️ glyph is not merely cosmetic, the underlying verdict really flips.
    assert verdict.get("ready") is False, verdict
    assert verdict.get("policy_changed_in_diff") is True, verdict


async def test_an_unreadable_changed_file_list_is_a_problem_not_a_pass(
    store, tmp_path, monkeypatch,
):
    """A swallowed `changed_files` failure used to substitute `[]`, which
    SATISFIES `paths_within` ("no changed paths") and hides
    `policy_changed_in_diff` — the one check that stops a coder authoring its
    own merge gate. The failure is now a policy `problem`, so the single
    `ready = ... and not all_problems ...` line forces not-ready and the
    human sees the reason instead of three rules quietly reading green.

    RED before the fix: the verdict came back `ready` with no problems."""
    work = _repo_with_a_commit(tmp_path, policy_yaml=MINIMAL_POLICY_YAML)
    _commit(work, "impl.py", "print('x')\n", "implement")
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha)

    from no_human.vcs.git import GitRepo

    def boom(self, base=None):
        raise RuntimeError("git ls-files exploded")

    monkeypatch.setattr(GitRepo, "changed_files", boom)

    def fake_open_pr(repo, branch, title, body, **kw):
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch,
        test_results={"ran": True, "passed": 1, "failed": 0})

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    verdict = ((task.context or {}).get("merge_policy") or {}).get(
        reviewed_sha) or {}
    assert verdict.get("ready") is False, verdict
    problems = verdict.get("problems") or []
    assert any("changed-file list could not be read" in p for p in problems), \
        problems
    # (the AWAITING_APPROVAL assertion above is the "PR still ships" check —
    #  the verdict is advisory, never a gate)


async def test_policy_file_unchanged_in_diff_reads_ready_normally(
    store, tmp_path, monkeypatch,
):
    work = _repo_with_a_commit(tmp_path, policy_yaml=MINIMAL_POLICY_YAML)
    _commit(work, "impl.py", "print('x')\n", "implement")
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha)
    bodies = []

    def fake_open_pr(repo, branch, title, body, **kw):
        bodies.append(body)
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch,
        test_results={"ran": True, "passed": 1, "failed": 0})

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    body = bodies[0]
    assert "| Merge policy | ✅ ready — 3 of 3 rules satisfied |" in body, body

    mp = (task.context or {}).get("merge_policy") or {}
    verdict = mp.get(reviewed_sha) or {}
    assert verdict.get("ready") is True, verdict
    assert verdict.get("policy_changed_in_diff") is False, verdict


# ---------------------------------------------------------------------------
# Evaluator failure is advisory, never a silent drop and never a block.
# ---------------------------------------------------------------------------

async def test_evaluator_failure_is_advisory_not_silence_and_never_blocks(
    store, tmp_path, monkeypatch,
):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha)
    bodies = []

    def fake_open_pr(repo, branch, title, body, **kw):
        bodies.append(body)
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    import no_human.core.orchestrator as orch_mod

    def raise_evaluate_repo(repo_path, facts):
        raise RuntimeError("boom")

    monkeypatch.setattr(orch_mod.merge_policy, "evaluate_repo", raise_evaluate_repo)

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch, events=events)

    # Delivery is never blocked by a broken evaluator.
    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    assert bodies, "open_pr was never called"

    advisories = [e["text"] for e in events if e.get("kind") == "advisory"]
    assert any(
        a.startswith("merge policy verdict missing from PR body:") for a in advisories
    ), advisories
    # Not silence: no fabricated verdict row — the row that DOES render (see
    # `test_a_failed_policy_compute_says_so_in_the_body` for the full text
    # assertion) discloses the failure, it never claims ready or not-ready.
    assert "| Merge policy | ✅" not in bodies[0], bodies[0]
    assert "| Merge policy | ❌" not in bodies[0], bodies[0]
    mp = (task.context or {}).get("merge_policy") or {}
    assert reviewed_sha not in mp, mp


# ---------------------------------------------------------------------------
# `advisory_count` reads the real, uncapped checklist — not the 5-item trail.
# ---------------------------------------------------------------------------

async def test_advisory_count_reads_the_real_checklist_not_the_capped_trail(
    store, tmp_path, monkeypatch,
):
    work = _repo_with_a_commit(tmp_path, policy_yaml="rules:\n  - no_advisory_findings\n")
    _commit(work, "impl.py", "print('x')\n", "implement")
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    # The stamped round's own `advisory` trail is capped at 2 entries — if
    # the merge-policy rule read THIS number, the wiring would be broken.
    ctx = _stamp({}, reviewed_sha, advisory=["cap-1", "cap-2"])
    checklist = json.dumps({"items": [
        {"label": f"nit {i}", "passed": False, "severity": "low",
         "evidence": "", "file": "", "line": 0, "comment": ""}
        for i in range(7)
    ]})
    bodies = []

    def fake_open_pr(repo, branch, title, body, **kw):
        bodies.append(body)
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch,
        review_checklist=checklist)

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    body = bodies[0]
    # The real, uncapped count (7) — never the capped trail's count (2).
    assert "❌ no_advisory_findings — 7 advisory findings" in body, body
    assert "2 advisory findings" not in body, body


# ---------------------------------------------------------------------------
# A failed policy compute still carries the uncapped advisory count — the bug
# this whole change exists to fix: `policy_evidence` is now hoisted before
# the policy `try:` and folded (never re-gathered) on failure, so it still
# holds the review_checklist-derived count `_pr_body` was given.
# ---------------------------------------------------------------------------

async def test_a_failed_policy_compute_still_carries_the_uncapped_advisory_count(
    store, tmp_path, monkeypatch,
):
    """This is asserted on the `evidence=` OBJECT `_pr_body` receives, not on
    rendered body text: on the failure path nothing renders `advisory_count`
    as text (only `merge_policy.facts_from_evidence` — which never ran here,
    since the evaluator raised — would have turned it into the
    `no_advisory_findings` row). So the only way to prove the hoisted, folded
    evidence object still carries the real count is to intercept the object
    itself.

    RED before the fix: on `main`, a failed policy compute set
    `policy_evidence = None`, so `_pr_body` fell back to its own
    `if evidence is None:` re-gather — WITHOUT `review_checklist=` — and
    `seen["evidence"]` would either be a *different* object than the one
    built from the real checklist (silently reading the capped trail's count
    of 2), or in a stricter reading, simply not the same object at all. Either
    way this assertion (`advisory_count == 7`, matching the uncapped
    checklist, not the capped 2-item trail) fails on unpatched `main`.
    """
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    # The stamped round's own capped trail — 2 entries. If the fix regressed
    # to reading this instead of the real checklist, this test would see 2.
    ctx = _stamp({}, reviewed_sha, advisory=["cap-1", "cap-2"])
    checklist = json.dumps({"items": [
        {"label": f"nit {i}", "passed": False, "severity": "low",
         "evidence": "", "file": "", "line": 0, "comment": ""}
        for i in range(7)
    ]})

    import no_human.core.orchestrator as orch_mod

    def raise_evaluate_repo(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(orch_mod.merge_policy, "evaluate_repo", raise_evaluate_repo)

    seen: dict = {}
    original_pr_body = Orchestrator._pr_body

    def spy_pr_body(self, *a, **k):
        seen["evidence"] = k.get("evidence")
        return original_pr_body(self, *a, **k)

    monkeypatch.setattr(Orchestrator, "_pr_body", spy_pr_body)

    def fake_open_pr(repo, branch, title, body, **kw):
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch,
        review_checklist=checklist)

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    assert seen.get("evidence") is not None, "expected _pr_body to receive evidence"
    review_verdict = seen["evidence"].review_verdict or {}
    assert review_verdict.get("advisory_count") == 7, review_verdict
    assert seen["evidence"].merge_policy is None, seen["evidence"].merge_policy


async def test_a_failed_policy_compute_says_so_in_the_body(store, tmp_path, monkeypatch):
    """The disclosure row itself: no fabricated ✅/❌ verdict, no fold that
    claims to have anything to fold, and the exception CLASS NAME is visible
    — a human reading the body sees the gap instead of reading silence as
    "no policy configured"."""
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha)
    bodies = []

    def fake_open_pr(repo, branch, title, body, **kw):
        bodies.append(body)
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    import no_human.core.orchestrator as orch_mod

    def raise_evaluate_repo(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(orch_mod.merge_policy, "evaluate_repo", raise_evaluate_repo)

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch, events=events)

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    body = bodies[0]
    assert "NOT COMPUTED — the merge-ready verdict could not be computed" in body, body
    assert "RuntimeError" in body, body
    assert "| Merge policy | ✅" not in body, body
    assert "| Merge policy | ❌" not in body, body
    assert "<details><summary>Merge-ready policy" not in body, body

    mp = (task.context or {}).get("merge_policy") or {}
    assert reviewed_sha not in mp, mp

    advisories = [e["text"] for e in events if e.get("kind") == "advisory"]
    assert any(
        a.startswith("merge policy verdict missing from PR body:") for a in advisories
    ), advisories


async def test_merge_context_failure_also_discloses(store, tmp_path, monkeypatch):
    """The `except` in `_finalize` covers the WHOLE `try:` block, not just the
    evaluator call — a failure in `store.merge_context` (persisting the
    verdict) after a successful `evaluate_repo` call must disclose exactly
    the same way, naming ITS OWN exception class."""
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha)
    bodies = []

    def fake_open_pr(repo, branch, title, body, **kw):
        bodies.append(body)
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    async def raise_merge_context(*a, **k):
        raise ValueError("disk full")

    monkeypatch.setattr(store, "merge_context", raise_merge_context)

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch)

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    body = bodies[0]
    assert "NOT COMPUTED — the merge-ready verdict could not be computed" in body, body
    assert "ValueError" in body, body
    assert "| Merge policy | ✅" not in body, body
    assert "| Merge policy | ❌" not in body, body


# ---------------------------------------------------------------------------
# The success path's rendered body is unchanged by moving to a single gather.
# ---------------------------------------------------------------------------

async def test_the_success_path_body_is_unchanged_by_the_single_gather(
    store, tmp_path, monkeypatch,
):
    """A strong CONTENT assertion — the real evaluator still produces the
    same visible verdict row it did before the gather was hoisted — not a
    byte-identity/golden-file comparison (no golden exists for this body)."""
    work = _repo_with_a_commit(tmp_path, policy_yaml="rules:\n  - no_advisory_findings\n")
    _commit(work, "impl.py", "print('x')\n", "implement")
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    ctx = _stamp({}, reviewed_sha, advisory=["cap-1", "cap-2"])
    checklist = json.dumps({"items": [
        {"label": f"nit {i}", "passed": False, "severity": "low",
         "evidence": "", "file": "", "line": 0, "comment": ""}
        for i in range(7)
    ]})
    bodies = []

    def fake_open_pr(repo, branch, title, body, **kw):
        bodies.append(body)
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    seen: dict = {}
    original_pr_body = Orchestrator._pr_body

    def spy_pr_body(self, *a, **k):
        seen["evidence"] = k.get("evidence")
        return original_pr_body(self, *a, **k)

    monkeypatch.setattr(Orchestrator, "_pr_body", spy_pr_body)

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch,
        review_checklist=checklist)

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    body = bodies[0]
    assert "❌ no_advisory_findings — 7 advisory findings" in body, body
    assert "NOT COMPUTED" not in body, body
    assert seen.get("evidence") is not None
    assert seen["evidence"].merge_policy_error is None, seen["evidence"].merge_policy_error


# ---------------------------------------------------------------------------
# Static: `_finalize` gathers evidence exactly once, and no `= None` reset.
# ---------------------------------------------------------------------------

def test_finalize_gathers_evidence_once():
    """AST-scans `Orchestrator._finalize` for calls whose attribute is
    `_gather_evidence` — exactly one is allowed in the whole function body —
    and for any `policy_evidence = None` assignment, which would reintroduce
    the bug this change fixes (a `None` reset makes `_pr_body`'s fallback
    branch reachable again, defeating the single-gather invariant).

    Modeled on `test_land_task_is_referenced_only_by_cli_and_api`'s AST-scan
    idiom in this same file.
    """
    import no_human
    src_root = Path(no_human.__file__).resolve().parent
    tree = ast.parse((src_root / "core" / "orchestrator.py").read_text(encoding="utf-8"))

    finalize_def = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_finalize":
            finalize_def = node
            break
    assert finalize_def is not None, "_finalize not found"

    gather_calls = [
        n for n in ast.walk(finalize_def)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_gather_evidence"
    ]
    assert len(gather_calls) == 1, (
        f"expected exactly one _gather_evidence call inside _finalize, "
        f"found {len(gather_calls)}")

    none_resets = [
        n for n in ast.walk(finalize_def)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "policy_evidence" for t in n.targets)
        and isinstance(n.value, ast.Constant) and n.value.value is None
    ]
    assert not none_resets, (
        "policy_evidence must never be reset to None — fold the failure "
        "with dataclasses.replace(...) instead")


# ---------------------------------------------------------------------------
# `land_task` call-site guard: only the two intended callers reference it.
# ---------------------------------------------------------------------------

def test_land_task_is_referenced_only_by_cli_and_api():
    """`land_task` (`vcs/approve_merge.py`) is the one function that actually
    merges a PR. Its *definition* and its mentions inside docstrings/comments
    are not call sites, so this scans the AST — and it scans BOTH spellings:

      * `ast.Name(id="land_task")` — a direct call or a bare callback
        reference (`asyncio.to_thread(land_task, ...)` in `api/app.py`);
      * `ast.Attribute(attr="land_task")` — `vcs.land_task(...)` or
        `approve_merge.land_task(...)`. `vcs/__init__.py` re-exports the name
        (`from .approve_merge import land_task`, and it is in `__all__`), so
        the attribute spelling reaches the same function while producing no
        `ast.Name` node at all. A guard that scanned only `ast.Name` was
        evadable by an import style already available in this repo.

    Scope is the FILE plus the enclosing function, so "only the approve
    command and the human approve handler" is what is actually asserted — a
    new merge trigger added inside those same two files, in some other
    function, fails this test instead of passing unnoticed.
    """
    import no_human
    src_root = Path(no_human.__file__).resolve().parent
    offenders: set[tuple[str, str]] = set()
    for f in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(f.relative_to(src_root))

        def _hit(node: ast.AST) -> bool:
            return ((isinstance(node, ast.Name) and node.id == "land_task")
                    or (isinstance(node, ast.Attribute)
                        and node.attr == "land_task"))

        # Attribute every hit to its OUTERMOST enclosing def: the CLI wraps
        # each command body in a nested `async def _go()`, and "it is inside
        # `_go`" says nothing about which command owns it. Walking only the
        # module-level (and class-level) defs, then descending, gives the
        # command/handler name a reader would name.
        tops: list[ast.AST] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tops.append(node)
            elif isinstance(node, ast.ClassDef):
                tops.extend(n for n in node.body
                            if isinstance(n, (ast.FunctionDef,
                                              ast.AsyncFunctionDef)))
        for top in tops:
            if any(_hit(n) for n in ast.walk(top)):
                offenders.add((rel, top.name))
        # A reference OUTSIDE every def (module level) is never a legitimate
        # merge trigger — an import is not a reference to the callable here,
        # so those are skipped by construction (an `ImportFrom` carries only
        # `alias` nodes, never a `Name`/`Attribute`).
        covered = {id(n) for top in tops for n in ast.walk(top)}
        for node in ast.walk(tree):
            if _hit(node) and id(node) not in covered:
                offenders.add((rel, "<module level>"))
    # `api/app.py`'s hit sits in `_merge_task_pr`, the helper the human
    # approve endpoint awaits (`app.py:1075`) — the endpoint itself never
    # names `land_task`. Pinning the helper is the honest assertion; pinning
    # the endpoint would be pinning a function that does not contain the call.
    assert offenders == {
        ("cli/commands.py", "approve"),
        ("api/app.py", "_merge_task_pr"),
    }, offenders


# ---------------------------------------------------------------------------
# Docs guard: the shipped `nh approve --ready` / `--yes` / board chip are
# actually described, and the advisory/never-auto-merges sentences survive.
# ---------------------------------------------------------------------------

def test_docs_describe_the_shipped_merge_ready_ui():
    repo_root = Path(__file__).resolve().parent.parent
    text = (repo_root / "docs/verification.md").read_text(encoding="utf-8")
    assert "approve --ready" in text
    assert "--yes" in text
    assert "MERGE-READY" in text
    # The advisory / nothing-auto-merges guarantees must survive the
    # `--ready`/`--yes` addition, word for word.
    assert "**This is advisory to the human; nothing merges on it.**" in text
    assert "a human still has to" in text

    # `docs/pr-body.md` is about PR body rendering, not the CLI/board — it
    # must not pick up an unrelated claim about `--ready` or the chip.
    pr_body_text = (repo_root / "docs/pr-body.md").read_text(encoding="utf-8")
    assert "approve --ready" not in pr_body_text
    assert "MERGE-READY" not in pr_body_text


# ---------------------------------------------------------------------------
# `approval.auto_merge_on_approval` stays False and unread: constraint #2
# ("the agent never merges... no auto-merge anywhere") pinned at the config
# layer, not just by convention. A new REAL read (a `.get("auto_merge_on_
# approval")`/`["auto_merge_on_approval"]` access, as opposed to the key's
# own definition in DEFAULT_CONFIG or a prose mention in a docstring/comment)
# would be the seed of exactly the auto-merge path this project's standing
# rules forbid: the agent never merges, only a human `nh approve` does.
# ---------------------------------------------------------------------------

def test_auto_merge_on_approval_stays_false_and_unread():
    from no_human.config import DEFAULT_CONFIG

    approval = DEFAULT_CONFIG.get("approval") or {}
    assert approval.get("auto_merge_on_approval") is False, approval

    import no_human
    src_root = Path(no_human.__file__).resolve().parent
    key = "auto_merge_on_approval"
    offenders: list[str] = []
    for f in sorted(src_root.rglob("*.py")):
        rel = str(f.relative_to(src_root))
        if rel == "config.py":
            continue  # the key's own definition site (DEFAULT_CONFIG dict literal)
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        def _is_key_const(node: ast.AST) -> bool:
            return isinstance(node, ast.Constant) and node.value == key

        for node in ast.walk(tree):
            # `something["auto_merge_on_approval"]`
            if isinstance(node, ast.Subscript) and _is_key_const(node.slice):
                offenders.append(rel)
                break
            # `something.get("auto_merge_on_approval", ...)`
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args and _is_key_const(node.args[0])):
                offenders.append(rel)
                break
    assert offenders == [], (
        f"auto_merge_on_approval must stay unread by src/**/*.py; found a "
        f"real read in {offenders}")
