"""Delivery must FETCH the remote branch tip live and reconcile it, never
compare a cached/unfetched value against the reviewed sha.

Audited defect (dogfood board, 2026-09-08, tasks dc3225a8 and a6cc4d39): a
PASSing review's commit was refused as "not the reviewed sha" and the task
was re-dispatched from scratch, twice, for two distinct root causes —

  (a) the reviewed commit existed only in the task worktree; the remote
      branch still pointed at the branch's creation point. Delivery never
      pushed it, it just compared and refused.
  (b) the remote branch DID already carry the reviewed sha (confirmed by
      hand with `git fetch` + `rev-parse` at the time of the refusal), yet
      the refusal text named the branch's creation point — a stale/cached
      remote-tracking ref, not a live read.

Fix under test: ``Orchestrator._assert_delivery_sha`` (via
``_reconcile_remote_branch``) fetches the remote branch tip LIVE
(``GitRepo.fetch_remote_branch_sha``, a `git ls-remote` call — never
``refs/remotes/<remote>/<branch>``) immediately before any comparison. If
the remote is behind the reviewed sha, it is fast-forwarded (never forced)
and delivery proceeds. If the remote has genuinely diverged (its tip is not
an ancestor of the reviewed sha), delivery refuses naming the sha it just
fetched. A local tip that carries commits the reviewed sha doesn't still
refuses exactly as before this fix.

This file reuses the ``_orch`` / ``_git`` / ``_stamp`` / ``_finalize_task``
shape from ``tests/test_delivery_pushes_reviewed_sha.py`` (copied locally;
that file is untouched) and adds a bare-repo-as-remote helper.
"""
import subprocess


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo
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
# AC 1: a stale local remote-tracking ref must never cause a false refusal
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
