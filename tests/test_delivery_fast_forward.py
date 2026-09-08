"""Delivery must never refuse a commit that a passing review round actually
stamped, just because the LOCAL branch ref hasn't been walked forward to it
yet.

Audited defect (dogfood board, 2026-09-08, tasks dc3225a8 and a6cc4d39): a
PASSing review's commit was refused as "not the reviewed sha" and the task
was re-dispatched from scratch, twice. The true mechanism, confirmed by
reading the pre-fix ``_assert_delivery_sha`` (it was exactly
``tip = repo.branch_sha(branch); if tip not in shas: raise`` — it never read
a remote or a tracking ref at all, so no remote-side staleness could be the
cause): the review stamp records ``repo.head_sha()`` at review time, but the
gate compared it against ``refs/heads/<branch>``'s LOCAL ref. Those two can
differ inside the very same worktree the review ran in —

  (a) HEAD was detached at the reviewed commit (no branch ref moved at
      all), or
  (b) something reset ``refs/heads/<branch>`` back to the point it was
      created at after the reviewed commit was made on top of it —

and either way the reviewed commit is already reachable in this same object
store; the local branch ref just hasn't caught up. "A different checkout's
branch ref" is not a possible cause: linked git worktrees share
``refs/heads``, so every worktree of this repo sees the same local branch
ref.

Fix under test: ``Orchestrator._assert_delivery_sha`` fast-forwards the
LOCAL branch ref (``GitRepo.fast_forward_local_branch``) to the reviewed sha
FIRST, before touching the remote at all, whenever the branch tip isn't
already an exact stamp match. Only once the local ref is correct does it
consult the remote (``_reconcile_remote_branch`` /
``GitRepo.fetch_remote_branch_sha``, a live ``git ls-remote`` — never
``refs/remotes/<remote>/<branch>``) — that remote check guards against a
genuinely different, forward-looking problem (the remote having diverged, or
being protected), not the local-ref lag this fix addresses. A local tip that
carries commits the reviewed sha doesn't still refuses exactly as before
this fix, and so does a local ref that cannot be fast-forwarded at all.

This file reuses the ``_orch`` / ``_git`` / ``_stamp`` / ``_finalize_task``
shape from ``tests/test_delivery_pushes_reviewed_sha.py`` (copied locally;
that file is untouched) and adds a bare-repo-as-remote helper.
"""
import subprocess

import pytest

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo, ProtectedBranch
from no_human.vcs.receipts import Receipt

BRANCH = "nh/attempt-1"


def _orch(store, tmp_path, events=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(
        store, cfg.data, _Backend(), SlackNotifier(None),
        event_sink=(events.append if events is not None else None))


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_a_commit(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "a@b.c")
    _git(work, "config", "user.name", "T")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "checkout", "-q", "-b", BRANCH)
    return work


def _commit(work, name: str, body: str, msg: str) -> str:
    (work / name).write_text(body)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", msg)
    return _git(work, "rev-parse", "HEAD")


def _bare_remote(tmp_path, name: str = "origin.git"):
    """A real bare git repo standing in for the forge remote."""
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)],
                    capture_output=True, text=True, check=True)
    return bare


def _add_origin(work, bare) -> None:
    _git(work, "remote", "add", "origin", str(bare))


def _push_sha_to_remote(work, sha: str, branch: str = BRANCH) -> None:
    """Push `branch`'s creation point (or any explicit sha) directly, the way
    the test plan's helper describes: "adds it as `origin` and pushes the
    branch's creation point only" — reused here for any explicit sha so the
    same helper can plant either a lagging or an up-to-date remote."""
    _git(work, "push", "origin", f"{sha}:refs/heads/{branch}")


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


def _stamp(ctx: dict, sha: str, *, passed: bool = True) -> dict:
    ctx = dict(ctx or {})
    ctx["review_history"] = [
        {"round": 1, "sha": sha, "passed": passed, "blocking": [], "advisory": []},
    ]
    return ctx


def _stamp_rounds(ctx: dict, rounds: list[tuple[int, str]], *, passed: bool = True) -> dict:
    """Like `_stamp`, but for multiple PASS-stamped rounds in list order
    (oldest first, newest last) — the shape `review_history` actually has
    across a task's lifetime, where several attempts/rounds can each leave
    a PASS stamp behind."""
    ctx = dict(ctx or {})
    ctx["review_history"] = [
        {"round": n, "sha": sha, "passed": passed, "blocking": [], "advisory": []}
        for n, sha in rounds
    ]
    return ctx


async def _finalize_task(store, tmp_path, work, ctx, open_pr_stub, monkeypatch,
                         events=None, verify_stub=_landed_receipt):
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

    commit = _Commit()
    commit.sha = repo.head_sha()
    out = await orch._finalize(
        task, repo, BRANCH, "main", commit, attempt_id, _Result())
    return orch, task, attempt_id, out


# ---------------------------------------------------------------------------
# AC 1, 2: the remote is fetched live and a lagging branch is fast-forwarded
# ---------------------------------------------------------------------------

async def test_delivery_fast_forwards_stale_branch(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    creation_sha = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, creation_sha)  # remote stuck at the creation point
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")

    calls = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append((branch, kw))
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), fake_open_pr,
        monkeypatch, events=events)

    assert calls, "open_pr was never called"
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == reviewed_sha, (
        "the bare remote must now carry the reviewed sha (fast-forwarded)")
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "succeeded", attempts[-1]
    ffs = [e for e in events if e.get("kind") == "delivery_branch_fast_forwarded"]
    assert ffs, f"no delivery_branch_fast_forwarded event: {events}"
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert not mismatches, mismatches


# ---------------------------------------------------------------------------
# AC 3: a genuinely diverged remote still refuses, naming the FETCHED tip
# ---------------------------------------------------------------------------

async def test_delivery_refuses_diverged_remote(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    creation_sha = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, creation_sha)
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")

    # An independent commit lands on the remote branch — not an ancestor
    # of, nor a descendant of, `reviewed_sha`.
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "a@b.c")
    _git(other, "config", "user.name", "T")
    _git(other, "remote", "add", "origin", str(bare))
    _git(other, "fetch", "-q", "origin", BRANCH)
    _git(other, "checkout", "-q", "-b", BRANCH, "origin/" + BRANCH)
    diverged_sha = _commit(other, "other.txt", "y\n", "diverged commit")
    _git(other, "push", "-q", "origin", BRANCH)

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not be called on a diverged remote")

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), must_not_push,
        monkeypatch, events=events)

    assert out.status == TaskStatus.ESCALATED, out.detail
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert mismatches, f"no delivery_sha_mismatch event: {events}"
    text = mismatches[0]["text"]
    assert diverged_sha in text, text
    assert reviewed_sha in text, text
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == diverged_sha, (
        "the remote must be untouched on a genuine divergence")
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


# ---------------------------------------------------------------------------
# No-false-positive guard: a stale *tracking* ref (refs/remotes/origin/...)
# must never cause a false refusal. NOTE: this passes on the unfixed
# (pre-this-task) code too — it is not a regression test for either
# incident. The local branch ref here is exactly at `reviewed_sha` the whole
# time (the tip-in-shas fast path handles it); only the tracking ref is
# stale, and the pre-fix gate never read the tracking ref at all. It is kept
# to guard the reconciliation path's use of a live fetch rather than that
# tracking ref, which is a real (if different) hazard.
# ---------------------------------------------------------------------------

async def test_delivery_stale_local_ref_no_false_positive(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    creation_sha = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, creation_sha)
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")
    # The remote already has the reviewed sha (pushed out-of-band — another
    # process, or a human, already landed it) ...
    _push_sha_to_remote(work, reviewed_sha)
    # ... but THIS worktree's tracking ref is stale, still naming the
    # creation point — planted directly, the only way to force the stale
    # condition deterministically.
    _git(work, "update-ref", f"refs/remotes/origin/{BRANCH}", creation_sha)

    calls = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append((branch, kw))
        return _FakePR("https://github.com/o/r/pull/1", repo.head_sha())

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), fake_open_pr,
        monkeypatch, events=events)

    assert calls, "open_pr was never called"
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "succeeded", attempts[-1]
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert not mismatches, mismatches
    for e in events:
        assert creation_sha not in e.get("text", ""), (
            f"the stale tracking ref's value leaked into an event: {e}")


# ---------------------------------------------------------------------------
# Regression guard: a local tip carrying unreviewed commits still refuses,
# even with a bare remote present to fast-forward against.
# ---------------------------------------------------------------------------

async def test_local_branch_ahead_of_reviewed_sha_still_refuses(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, reviewed_sha)
    # The branch moves AFTER the review stamped `reviewed_sha` — a
    # concurrent, unreviewed commit landed on it before delivery ran.
    _commit(work, "extra.txt", "surprise\n", "unreviewed extra commit")

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not be called on a sha mismatch")

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), must_not_push,
        monkeypatch, events=events)

    assert out.status == TaskStatus.ESCALATED, out.detail
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == reviewed_sha, (
        "nothing must be pushed to the remote on an unreviewed-extra-commit refusal")
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


# ---------------------------------------------------------------------------
# MAJOR-1: the actual incident mechanism — HEAD detached at the reviewed
# commit, refs/heads/<branch> left behind at its creation point. Single
# worktree throughout: linked worktrees share refs/heads, so "a different
# checkout's branch ref" was never a possible cause.
# ---------------------------------------------------------------------------

async def test_delivery_fast_forwards_a_lagging_local_branch_ref(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    creation_sha = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, creation_sha)  # remote also stuck at the creation point
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")  # advances BRANCH

    # Reproduce the incident: HEAD ends up detached at the reviewed commit
    # while refs/heads/<branch> is left behind at its creation point.
    _git(work, "checkout", "-q", "--detach", reviewed_sha)
    _git(work, "update-ref", f"refs/heads/{BRANCH}", creation_sha)

    calls = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append((branch, kw))
        return _FakePR("https://github.com/o/r/pull/1", GitRepo(work).branch_sha(BRANCH))

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), fake_open_pr,
        monkeypatch, events=events)

    assert calls, "open_pr was never called"
    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    assert _git(work, "rev-parse", f"refs/heads/{BRANCH}") == reviewed_sha, (
        "the local branch ref must be fast-forwarded to the reviewed sha")
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == reviewed_sha, (
        "the bare remote must carry the reviewed sha")
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "succeeded", attempts[-1]
    ffs = [e for e in events if e.get("kind") == "delivery_branch_fast_forwarded"]
    assert ffs, f"no delivery_branch_fast_forwarded event: {events}"
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert not mismatches, mismatches


# ---------------------------------------------------------------------------
# MAJOR-2: HEAD wins over an older attempt's stamp even when the older
# stamp's sha sorts lexicographically first — the exact shape that made the
# pre-fix `min()` tie-break deliver an older attempt's commit instead of the
# one actually reviewed and sitting at HEAD (measured incident: chosen
# 06c280c8 while HEAD was 562c2f25).
# ---------------------------------------------------------------------------

async def test_delivery_prefers_head_over_older_attempt_stamp(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    tip0 = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, tip0)

    # Search for a pair of sibling commits (both direct descendants of tip0,
    # unrelated to each other) whose shas happen to put the OLDER one first
    # lexicographically — the exact shape the old `min()` tie-break got
    # wrong. Content-only variation; extremely likely to succeed in a few
    # tries.
    sha_a = sha_b = None
    for i in range(50):
        _git(work, "checkout", "-q", "--detach", tip0)
        cand_a = _commit(work, "a.txt", f"a{i}\n", f"round 1 attempt {i}")
        _git(work, "checkout", "-q", "--detach", tip0)
        cand_b = _commit(work, "b.txt", f"b{i}\n", f"round 2 attempt {i}")
        if cand_a < cand_b:
            sha_a, sha_b = cand_a, cand_b
            break
    assert sha_a is not None, "could not find a lexicographic ordering in 50 tries"

    # BRANCH never advances past tip0 (an older attempt's round left it
    # there); HEAD is detached at sha_b, this attempt's actual reviewed
    # commit. sha_a is round 1's (older, unrelated) PASS stamp; sha_b is
    # round 2's (newer, this attempt's) PASS stamp.
    _git(work, "update-ref", f"refs/heads/{BRANCH}", tip0)
    _git(work, "checkout", "-q", "--detach", sha_b)
    ctx = _stamp_rounds({}, [(1, sha_a), (2, sha_b)])

    calls = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append((branch, kw))
        return _FakePR("https://github.com/o/r/pull/1", GitRepo(work).branch_sha(BRANCH))

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch, events=events)

    assert calls, "open_pr was never called"
    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    assert _git(work, "rev-parse", f"refs/heads/{BRANCH}") == sha_b, (
        f"must deliver HEAD's sha {sha_b}, not the lexicographically-smaller "
        f"older stamp {sha_a}")
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == sha_b


# ---------------------------------------------------------------------------
# MAJOR-2: two stamped shas that are genuinely unrelated (neither an
# ancestor of the other) and neither matches this attempt's HEAD — no
# principled choice exists, so delivery must refuse naming both, never guess
# via a lexicographic tie-break.
# ---------------------------------------------------------------------------

async def test_delivery_refuses_two_unrelated_stamped_candidates(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    tip0 = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, tip0)

    _git(work, "checkout", "-q", "--detach", tip0)
    sha_a = _commit(work, "a.txt", "a\n", "round 1")
    _git(work, "checkout", "-q", "--detach", tip0)
    sha_b = _commit(work, "b.txt", "b\n", "round 2")
    # BRANCH (and HEAD) stay at tip0 — this attempt's own commit is neither
    # stamped candidate.
    _git(work, "checkout", "-q", BRANCH)

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not be called on an unresolvable ambiguity")

    ctx = _stamp_rounds({}, [(1, sha_a), (2, sha_b)])
    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, must_not_push, monkeypatch, events=events)

    assert out.status == TaskStatus.ESCALATED, out.detail
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert mismatches, f"no delivery_sha_mismatch event: {events}"
    text = mismatches[0]["text"]
    assert sha_a in text, text
    assert sha_b in text, text
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == tip0, (
        "the remote must be untouched when delivery cannot resolve the ambiguity")
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


# ---------------------------------------------------------------------------
# MINOR: a local fast-forward failure (merge --ff-only / the update-ref CAS
# refusing) must become a ReviewedShaMismatch with a delivery_sha_mismatch
# event, BEFORE the remote is ever touched — not an uncaught GitError.
# ---------------------------------------------------------------------------

async def test_local_fast_forward_failure_refuses_before_touching_the_remote(
    store, tmp_path, monkeypatch,
):
    work = _repo_with_a_commit(tmp_path)
    tip0 = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, tip0)

    # The reviewed commit exists, but BRANCH is left lagging at tip0.
    _git(work, "checkout", "-q", "--detach", tip0)
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")
    _git(work, "checkout", "-q", BRANCH)  # attached to BRANCH, still at tip0

    # Plant an untracked file that collides with what the fast-forward would
    # need to check out — `git merge --ff-only` refuses rather than
    # overwrite it, raising GitError from `fast_forward_local_branch`.
    (work / "feature.txt").write_text("UNCOMMITTED CONFLICT\n")

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not be called when the local "
                              "fast-forward itself fails")

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), must_not_push,
        monkeypatch, events=events)

    assert out.status == TaskStatus.ESCALATED, out.detail
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert mismatches, f"no delivery_sha_mismatch event: {events}"
    text = mismatches[0]["text"]
    # Pins the "with the git text" half of the GitError -> ReviewedShaMismatch
    # conversion: `_run` always formats `GitError` as
    # "git <args> failed (<rc>): <stderr>", so the refusal detail must carry
    # that same "git merge --ff-only <sha> failed" fragment, not just a
    # generic "not the reviewed sha" boilerplate that would also fire on a
    # False return from `fast_forward_local_branch`.
    assert "git merge --ff-only" in text, text
    assert reviewed_sha in text, text
    assert "failed" in text, text
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == tip0, (
        "the remote must be untouched when the local fast-forward itself fails")
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


# ---------------------------------------------------------------------------
# MINOR: fast_forward_local_branch must refuse a protected branch BEFORE
# writing anything to the local ref — push_sha_fast_forward already refuses
# protected branches, but on the lagging-ref path it runs AFTER the local
# ref would have been advanced; without its own check here the LOCAL ref
# moves even though the push is guaranteed to be refused next.
# ---------------------------------------------------------------------------

def test_fast_forward_local_branch_refuses_protected_branch_before_touching_ref(tmp_path):
    work = _repo_with_a_commit(tmp_path)
    creation_sha = _git(work, "rev-parse", "HEAD")
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")
    _git(work, "checkout", "-q", "--detach", reviewed_sha)
    _git(work, "update-ref", f"refs/heads/{BRANCH}", creation_sha)

    repo = GitRepo(work, never_push_to=[BRANCH])
    with pytest.raises(ProtectedBranch):
        repo.fast_forward_local_branch(BRANCH, reviewed_sha)
    assert _git(work, "rev-parse", f"refs/heads/{BRANCH}") == creation_sha, (
        "the protected branch's local ref must be untouched by the refusal")
