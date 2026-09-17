"""RECUT must never weaken the delivery refusal it lives next to.

BACKGROUND. `_reconcile_remote_branch` (Hook 2, `orchestrator.py`) gained a
recut-eligibility branch alongside its pre-existing unconditional refusal
(`ReviewedShaMismatch`, "remote tip ... is not an ancestor of the reviewed
sha ..."). That refusal is the ONE thing this whole feature exists downstream
of: it is what used to make a rebased branch loop forever, and recut's job is
only to give the loop an exit, never to make the refusal itself looser. This
file pins three ways the refusal must still fire, byte-for-byte unchanged,
whenever recut does not apply:

  5. the recut budget for this branch has already been spent (one-shot, via
     `already_recut`) — the *second* divergence on the same branch is
     refused, not recut again, and the refusal names the earlier recut.
  6. the branch is merely BEHIND its remote in the other sense that matters
     here: the reviewed sha (`target`) is itself an ancestor of the remote
     tip (the remote moved further ahead, additively, on top of the very
     commit under review) — `_reconcile_remote_branch`'s own
     `if not repo.is_ancestor(target, remote_tip):` guard means the entire
     recut-eligibility branch is skipped, unconditionally, before eligibility
     is even checked. This is distinct from the ordinary "remote is behind
     local" fast-forward case (`test_delivery_fast_forwards_stale_branch` in
     `tests/test_delivery_fast_forward.py`), which is the *other* direction
     and already recuts nothing because it never refuses at all.
  7. recut is structurally unavailable (`repo.remote_url() is None`) even
     though the branch is otherwise eligible (tracked, budget unspent) — the
     refusal fires with the exact template
     `tests/test_base_staleness_pushed_branch.py::
     test_reconcile_remote_branch_raises_the_exact_string_the_guard_quotes`
     already pins for the no-task-context case; this test proves the same
     template holds with a task/context present and eligible in every way
     except the one this test disables.

All three scenarios share one invariant, asserted in every test: nothing new
ever appears on the bare "origin" remote and no `branch_recut` event fires —
a refusal must be pure observation, exactly as it always was.

This file reuses the `_orch` / `_git` / `_repo_with_a_commit` / `_commit` /
`_bare_remote` / `_add_origin` / `_push_sha_to_remote` / `_stamp` /
`_finalize_task` shape from `tests/test_delivery_fast_forward.py` (copied
locally; that file is untouched, per this codebase's convention of copying
shared test scaffolding rather than cross-importing between test files).
"""
from __future__ import annotations

import subprocess

import pytest

import no_human.core.orchestrator as orch_mod
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo
from no_human.vcs.receipts import Receipt

BRANCH = "nh/attempt-1"


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
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)],
                    capture_output=True, text=True, check=True)
    return bare


def _add_origin(work, bare) -> None:
    _git(work, "remote", "add", "origin", str(bare))


def _push_sha_to_remote(work, sha: str, branch: str = BRANCH) -> None:
    _git(work, "push", "origin", f"{sha}:refs/heads/{branch}")


class _Commit:
    files_changed = 1
    insertions = 1
    deletions = 0
    sha = ""


class _Result:
    final_text = "Implemented the change."
    num_turns = 3


def _landed_receipt(*a, **k):
    return Receipt("pr_open", "https://github.com/o/r/pull/1", "landed", "ok")


def _stamp(ctx: dict, sha: str, *, passed: bool = True) -> dict:
    ctx = dict(ctx or {})
    ctx["review_history"] = [
        {"round": 1, "sha": sha, "passed": passed, "blocking": [], "advisory": []},
    ]
    return ctx


async def _finalize_task(store, tmp_path, work, ctx, open_pr_stub, monkeypatch,
                         events=None, verify_stub=_landed_receipt):
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


def _must_not_push(*a, **k):
    raise AssertionError("open_pr must not be called: delivery must refuse, not recut")


def _independent_diverged_commit(tmp_path, bare):
    """Mirrors `test_delivery_refuses_diverged_remote`'s construction: a
    genuinely unrelated commit landed on the remote branch by some other
    clone, so neither side is an ancestor of the other."""
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
    return diverged_sha


# ---------------------------------------------------------------------------
# Test 5 (AC1/AC4): the recut budget for this branch is already spent — a
# second divergence on the SAME branch must be refused, not recut again.
# ---------------------------------------------------------------------------

async def test_delivery_refuses_a_second_divergence_after_the_recut_budget_is_spent(
    store, tmp_path, monkeypatch,
):
    work = _repo_with_a_commit(tmp_path)
    creation_sha = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, creation_sha)
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")

    diverged_sha = _independent_diverged_commit(tmp_path, bare)

    ctx = _stamp({}, reviewed_sha)
    ctx["pr_branch"] = BRANCH
    ctx["recut"] = [{
        "from_branch": BRANCH, "from_sha": "deadbeef",
        "remote_sha": "cafef00d", "to_branch": "no-human/abc12345-2",
        "at": "2026-01-01T00:00:00+00:00",
    }]

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, _must_not_push, monkeypatch, events=events)

    assert out.status == TaskStatus.ESCALATED, out.detail
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert mismatches, f"no delivery_sha_mismatch event: {events}"
    text = mismatches[0]["text"]
    assert "already recut once" in text, text
    assert "no-human/abc12345-2" in text, text
    assert diverged_sha in text, text
    assert reviewed_sha in text, text

    recuts = [e for e in events if e.get("kind") == "branch_recut"]
    assert not recuts, f"no second recut may happen: {recuts}"
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == diverged_sha, (
        "the remote must be untouched on a refused second divergence")
    for_each = _git(bare, "for-each-ref", "--format=%(refname:short)")
    assert for_each.strip() == BRANCH, (
        f"no new ref may appear on origin: {for_each!r}")
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


# ---------------------------------------------------------------------------
# Test 6 (AC3/AC4): the reviewed sha is itself an ancestor of a remote tip
# that has moved further ahead — the "behind" case in the other direction.
# `_reconcile_remote_branch`'s `if not repo.is_ancestor(target, remote_tip):`
# guard must skip the recut-eligibility branch entirely, unconditionally,
# even though every other eligibility condition (tracked branch, live
# remote, unspent budget) is satisfied here.
# ---------------------------------------------------------------------------

async def test_a_branch_merely_behind_its_remote_is_never_recut(
    store, tmp_path, monkeypatch,
):
    work = _repo_with_a_commit(tmp_path)
    creation_sha = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, creation_sha)
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")
    _push_sha_to_remote(work, reviewed_sha)  # remote now == reviewed_sha

    # A second clone builds ADDITIVELY on top of the reviewed sha and pushes
    # further — the remote moves strictly ahead of `reviewed_sha`, but
    # `reviewed_sha` stays a genuine ancestor of the new remote tip.
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "a@b.c")
    _git(other, "config", "user.name", "T")
    _git(other, "remote", "add", "origin", str(bare))
    _git(other, "fetch", "-q", "origin", BRANCH)
    _git(other, "checkout", "-q", "-b", BRANCH, "origin/" + BRANCH)
    ahead_sha = _commit(other, "more.txt", "z\n", "further remote work")
    _git(other, "push", "-q", "origin", BRANCH)

    gr = GitRepo(work)
    # `ahead_sha`'s object only exists in the "other" clone so far; fetch it
    # into `work`'s object store the same way `fetch_remote_branch_sha` does
    # (a private-ref fetch, never the tracking ref) so the ancestry checks
    # below can actually resolve it.
    assert gr.fetch_remote_branch_sha(BRANCH) == ahead_sha
    # Positive control: this really is the "target is an ancestor of the
    # remote tip" shape, not plain up-to-date and not the ordinary
    # remote-behind-local fast-forward case.
    assert gr.is_ancestor(reviewed_sha, ahead_sha) is True
    assert gr.is_ancestor(ahead_sha, reviewed_sha) is False
    assert reviewed_sha != ahead_sha

    ctx = _stamp({}, reviewed_sha)
    ctx["pr_branch"] = BRANCH  # eligible in every other respect

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, _must_not_push, monkeypatch, events=events)

    assert out.status == TaskStatus.ESCALATED, out.detail
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert mismatches, f"no delivery_sha_mismatch event: {events}"
    text = mismatches[0]["text"]
    assert ahead_sha in text, text
    assert reviewed_sha in text, text
    assert "already recut once" not in text, text

    recuts = [e for e in events if e.get("kind") == "branch_recut"]
    assert not recuts, f"the behind case must never recut: {recuts}"
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == ahead_sha, (
        "the remote must be untouched")
    for_each = _git(bare, "for-each-ref", "--format=%(refname:short)")
    assert for_each.strip() == BRANCH, (
        f"no new ref may appear on origin: {for_each!r}")
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


# ---------------------------------------------------------------------------
# Test 7 (AC4): recut is structurally unavailable (`remote_url() is None`)
# even though the branch is tracked and the budget is unspent — the refusal
# must fire with the EXACT pre-existing template, unchanged, matching
# `tests/test_base_staleness_pushed_branch.py::
# test_reconcile_remote_branch_raises_the_exact_string_the_guard_quotes`.
# ---------------------------------------------------------------------------

async def test_the_existing_ancestor_refusal_string_is_unchanged_when_recut_is_unavailable(
    store, tmp_path, monkeypatch,
):
    work = _repo_with_a_commit(tmp_path)
    creation_sha = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, creation_sha)
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")

    diverged_sha = _independent_diverged_commit(tmp_path, bare)

    monkeypatch.setattr(GitRepo, "remote_url", lambda self: None)

    ctx = _stamp({}, reviewed_sha)
    ctx["pr_branch"] = BRANCH  # otherwise eligible: tracked, unspent budget

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, _must_not_push, monkeypatch, events=events)

    assert out.status == TaskStatus.ESCALATED, out.detail
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert mismatches, f"no delivery_sha_mismatch event: {events}"
    text = mismatches[0]["text"]
    assert text == (
        f"delivery refused: branch {BRANCH} remote tip {diverged_sha} "
        f"(fetched) is not an ancestor of the reviewed sha {reviewed_sha} "
        f"(human_gated_resume=False)"
    ), text

    recuts = [e for e in events if e.get("kind") == "branch_recut"]
    assert not recuts, f"recut must never run when remote_url() is None: {recuts}"
    assert _git(bare, "rev-parse", f"refs/heads/{BRANCH}") == diverged_sha
    for_each = _git(bare, "for-each-ref", "--format=%(refname:short)")
    assert for_each.strip() == BRANCH, (
        f"no new ref may appear on origin: {for_each!r}")
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]
