"""A rebased task branch can never be pushed again, so attempts loop.

BACKGROUND. Once a task's branch has been pushed once and is later rewritten
(rebased onto a moved base, squashed, amended), `GitRepo.push_sha_fast_
forward` correctly refuses to push it again: the remote holds a commit the
new local tip does not descend from, and this codebase never force-pushes to
route around that (see `push_sha_fast_forward`'s own docstring). Before this
fix, that refusal had no recovery path — every subsequent attempt reproduced
the exact same reviewed, green diff and hit the exact same non-fast-forward
refusal, forever, without ever reaching a human or a merged PR.

CHOSEN SHAPE: RECUT (see `vcs/recut.py`'s module docstring for the full
rationale) — cut a fresh, never-before-pushed branch name at the already-
reviewed sha and push THAT (trivially a fast-forward, since the remote has
never seen the name), leaving the old branch and its PR byte-for-byte alone.

This file exercises Hook 1 (`Orchestrator._recover_diverged_branch`), the
ALREADY-DIVERGED case: a branch that is diverged from its own pushed tip
BEFORE this run even starts (a human's `--amend`, a rebase from a prior
attempt, etc.). Hook 2 (`_reconcile_remote_branch`, the divergence-discovered-
at-delivery-time case) and the "delivery refusal is otherwise unchanged" and
"diverged-task audit" acceptance criteria are covered by
`tests/test_recut_preserves_delivery_refusal.py` and
`tests/test_diverged_audit.py` respectively.
"""
from __future__ import annotations

import subprocess

import pytest

import no_human.core.orchestrator as orch_mod
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator, ReviewedShaMismatch
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo
from no_human.vcs.receipts import Receipt
from no_human.vcs.recut import branch_stem


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path, events=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(
        store, cfg.data, _Backend(), SlackNotifier(None),
        event_sink=(events.append if events is not None else None))


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                    check=True, capture_output=True, text=True)
    return bare


@pytest.fixture
def repo(tmp_path, origin):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return work


def _make_pushed_diverged_branch(work, name):
    """Push `name`, then rewrite it LOCALLY (amend) with no further push, so
    the local head shares no ancestry with the pushed remote tip in either
    direction — the exact shape a rebase/amend after a push produces. Mirrors
    `tests/test_base_staleness_pushed_branch.py::_make_pushed_diverged_branch`
    (that file is untouched; the recipe is copied locally, not imported)."""
    _git(work, "checkout", "-q", "-b", name)
    (work / "pr_marker.py").write_text("# reviewed work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "PR work")
    _git(work, "push", "-q", "-u", "origin", name)
    remote_tip = _git(work, "rev-parse", name)
    (work / "pr_marker.py").write_text("# reviewed work, rewritten locally\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "--amend", "-m", "PR work (rewritten locally)")
    return remote_tip


async def _make_diverged_task(store, repo, origin, branch, *, extra_ctx=None):
    """A task whose `pr_branch` is already diverged from its pushed tip
    before the caller ever calls the recovery hook — the "diverged before
    the run starts" shape acceptance criterion 2 requires."""
    task = Task.new("Recut test", repo_path=str(repo))
    ctx = {"pr_branch": branch}
    if extra_ctx:
        ctx.update(extra_ctx)
    task.context = ctx
    await store.create_task(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    return task


# ---------------------------------------------------------------------------
# AC 1, AC 2: a branch diverged before the run starts is recut and pushed,
# and reaches this terminal state within the one additional attempt AC1 caps.
# ---------------------------------------------------------------------------

async def test_a_branch_diverged_before_the_run_is_recut_and_pushed(
    store, tmp_path, repo, origin,
):
    task = Task.new("Recut test", repo_path=str(repo))
    await store.create_task(task)
    stem = branch_stem({"git": {}}, task.id)
    branch = f"{stem}-6"

    remote_tip = _make_pushed_diverged_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    # Precondition demonstrated explicitly: genuinely diverged, neither side
    # is an ancestor of the other, BEFORE the hook is ever called.
    assert not gitrepo.is_ancestor(remote_tip, local_tip)
    assert not gitrepo.is_ancestor(local_tip, remote_tip)

    task.context = {"pr_branch": branch}
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    events: list[dict] = []
    orch = _orch(store, tmp_path, events=events)

    new_branch = await orch._recover_diverged_branch(task, gitrepo, branch)

    assert new_branch == f"{stem}-7", new_branch
    assert _git(origin, "rev-parse", f"refs/heads/{new_branch}") == local_tip, (
        "the new branch must be pushed to origin at the (post-divergence) "
        "reviewed sha")
    assert _git(origin, "rev-parse", f"refs/heads/{branch}") == remote_tip, (
        "the OLD branch's remote ref must be left byte-for-byte alone"
    )

    recut_events = [e for e in events if e.get("kind") == "branch_recut"]
    assert recut_events, f"no branch_recut event emitted: {events}"
    text = recut_events[0]["text"]
    assert local_tip in text, text
    assert remote_tip in text, text

    assert task.context["pr_branch"] == new_branch, task.context
    recorded = task.context.get("recut")
    assert recorded and recorded[-1]["from_branch"] == branch, recorded
    assert recorded[-1]["to_branch"] == new_branch, recorded


# ---------------------------------------------------------------------------
# AC 1: at most one recut per branch — a second divergence on the SAME
# branch (after it was already recut once) escalates instead of looping.
# ---------------------------------------------------------------------------

async def test_recut_happens_at_most_once_per_branch(store, tmp_path, repo, origin):
    task = Task.new("Recut test", repo_path=str(repo))
    await store.create_task(task)
    stem = branch_stem({"git": {}}, task.id)
    branch = f"{stem}-6"

    remote_tip = _make_pushed_diverged_branch(repo, branch)
    local_tip = _git(repo, "rev-parse", branch)
    gitrepo = GitRepo(repo)

    task.context = {
        "pr_branch": branch,
        "recut": [{
            "from_branch": branch, "from_sha": "deadbeef" * 5,
            "remote_sha": "cafebabe" * 5, "to_branch": f"{stem}-7",
            "at": "2026-01-01T00:00:00+00:00",
        }],
    }
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    events: list[dict] = []
    orch = _orch(store, tmp_path, events=events)

    branches_before = _git(origin, "for-each-ref", "--format=%(refname)")

    with pytest.raises(ReviewedShaMismatch) as excinfo:
        await orch._recover_diverged_branch(task, gitrepo, branch)

    text = str(excinfo.value)
    assert local_tip in text, text
    assert remote_tip in text, text

    branches_after = _git(origin, "for-each-ref", "--format=%(refname)")
    assert branches_before == branches_after, (
        "no new ref may be pushed once the one-shot recut budget is spent")


# ---------------------------------------------------------------------------
# AC 3: the recovery mechanism never force-pushes or rewrites published
# history. This test FAILS if a force (or any ref-mutating flag) is ever
# introduced into the git calls the recut path makes — both the direct
# `subprocess.run` calls (`ls-remote`, `push`) and every `GitRepo._run` call
# (`create_branch`, `push_sha_fast_forward`'s underlying `_run("push", ...)`)
# are recorded.
# ---------------------------------------------------------------------------

async def test_recut_never_forces(store, tmp_path, repo, origin, monkeypatch):
    task = Task.new("Recut test", repo_path=str(repo))
    await store.create_task(task)
    stem = branch_stem({"git": {}}, task.id)
    branch = f"{stem}-6"

    _make_pushed_diverged_branch(repo, branch)
    gitrepo = GitRepo(repo)

    task.context = {"pr_branch": branch}
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    recorded_subprocess: list[list[str]] = []
    real_subprocess_run = subprocess.run

    def _spy_subprocess_run(argv, *a, **k):
        if isinstance(argv, list) and argv and argv[0] == "git":
            recorded_subprocess.append(list(argv))
        return real_subprocess_run(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", _spy_subprocess_run)

    recorded_grun: list[tuple] = []
    real_grun = GitRepo._run

    def _spy_grun(self, *args, **kw):
        recorded_grun.append(args)
        return real_grun(self, *args, **kw)

    monkeypatch.setattr(GitRepo, "_run", _spy_grun)

    events: list[dict] = []
    orch = _orch(store, tmp_path, events=events)
    new_branch = await orch._recover_diverged_branch(task, gitrepo, branch)
    assert new_branch != branch  # sanity: the recut actually happened

    forbidden = ("--force", "-f", "--force-with-lease", "--delete")
    for argv in (*recorded_subprocess, *recorded_grun):
        for bad in forbidden:
            assert bad not in argv, f"forbidden flag {bad!r} in git call: {argv}"
        for arg in argv:
            if isinstance(arg, str) and arg.startswith("+"):
                raise AssertionError(f"forced refspec in git call: {argv}")

    assert recorded_grun or recorded_subprocess, "no git calls were recorded at all"


# ---------------------------------------------------------------------------
# Hook 2 / `_finalize`: a divergence discovered AT delivery time (not before
# the run, like the hook above) is recut mid-delivery, `open_pr` targets the
# NEW branch, and the superseded old PR gets exactly one upsert-keyed
# advisory comment. Reuses the `_orch` / `_finalize` shape from
# `tests/test_delivery_fast_forward.py` (copied locally; that file is
# untouched).
# ---------------------------------------------------------------------------

DELIVERY_BRANCH = "nh/attempt-1"


def _repo_with_a_commit(tmp_path):
    work = tmp_path / "delivery_work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "a@b.c")
    _git(work, "config", "user.name", "T")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "checkout", "-q", "-b", DELIVERY_BRANCH)
    return work


def _commit(work, name: str, body: str, msg: str) -> str:
    (work / name).write_text(body)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", msg)
    return _git(work, "rev-parse", "HEAD")


def _bare_remote(tmp_path, name: str = "delivery_origin.git"):
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)],
                    capture_output=True, text=True, check=True)
    return bare


def _add_origin(work, bare) -> None:
    _git(work, "remote", "add", "origin", str(bare))


def _push_sha_to_remote(work, sha: str, branch: str = DELIVERY_BRANCH) -> None:
    _git(work, "push", "origin", f"{sha}:refs/heads/{branch}")


def _stamp(ctx: dict, sha: str, *, passed: bool = True) -> dict:
    ctx = dict(ctx or {})
    ctx["review_history"] = [
        {"round": 1, "sha": sha, "passed": passed, "blocking": [], "advisory": []},
    ]
    return ctx


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
    return Receipt("pr_open", "https://github.com/o/r/pull/2", "landed", "ok")


async def _finalize_delivery(store, tmp_path, work, ctx, open_pr_stub, monkeypatch,
                              events=None, verify_stub=_landed_receipt):
    monkeypatch.setattr(orch_mod, "open_pr", open_pr_stub)
    monkeypatch.setattr(orch_mod, "verify_pr_receipt", verify_stub)

    git_repo = GitRepo(work)
    orch = _orch(store, tmp_path, events=events)
    task = Task.new("Fix the thing", repo_path=str(work))
    task.context = ctx
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)

    commit = _Commit()
    commit.sha = git_repo.head_sha()
    out = await orch._finalize(
        task, git_repo, DELIVERY_BRANCH, "main", commit, attempt_id, _Result())
    return orch, task, attempt_id, out


async def test_delivery_after_recut_opens_a_pr_on_the_new_branch(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    creation_sha = _git(work, "rev-parse", "HEAD")
    bare = _bare_remote(tmp_path)
    _add_origin(work, bare)
    _push_sha_to_remote(work, creation_sha)
    reviewed_sha = _commit(work, "feature.txt", "x\n", "add feature")

    # An independent commit lands on the remote branch — a genuine
    # divergence discovered only now, at delivery time (not before the run,
    # which is the scenario the tests above exercise).
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    _git(other, "config", "user.email", "a@b.c")
    _git(other, "config", "user.name", "T")
    _git(other, "remote", "add", "origin", str(bare))
    _git(other, "fetch", "-q", "origin", DELIVERY_BRANCH)
    _git(other, "checkout", "-q", "-b", DELIVERY_BRANCH, "origin/" + DELIVERY_BRANCH)
    diverged_sha = _commit(other, "other.txt", "y\n", "diverged commit")
    _git(other, "push", "-q", "origin", DELIVERY_BRANCH)

    calls = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append((branch, kw))
        return _FakePR("https://github.com/o/r/pull/2", repo.head_sha())

    upserts = []

    async def fake_upsert(pr_ref, message, key=""):
        upserts.append((pr_ref, message, key))
        return True

    monkeypatch.setattr(orch_mod.pr_watcher, "upsert_agent_comment", fake_upsert)

    ctx = _stamp({}, reviewed_sha)
    ctx["pr_branch"] = DELIVERY_BRANCH
    # The pre-existing (now-to-be-superseded) PR's url — must be captured by
    # `_finalize` BEFORE this attempt's own delivery overwrites it with the
    # new PR's url.
    ctx["pr_delivered_url"] = "https://github.com/o/r/pull/1"

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_delivery(
        store, tmp_path, work, ctx, fake_open_pr, monkeypatch, events=events)

    assert calls, "open_pr was never called"
    recut_branch, _ = calls[0]
    assert recut_branch != DELIVERY_BRANCH, (
        "open_pr must target the RECUT branch, not the superseded old one")

    recut_events = [e for e in events if e.get("kind") == "branch_recut"]
    assert recut_events, f"no branch_recut event: {events}"

    assert _git(bare, "rev-parse", f"refs/heads/{DELIVERY_BRANCH}") == diverged_sha, (
        "the OLD branch's remote ref must be left byte-for-byte alone")
    assert _git(bare, "rev-parse", f"refs/heads/{recut_branch}") == reviewed_sha

    assert task.context.get("pr_branch") == recut_branch, task.context

    assert upserts, "no upsert_agent_comment call for the superseded PR"
    assert len(upserts) == 1, (
        "exactly one upsert-keyed comment — the key makes repeated "
        f"deliveries update-in-place rather than spam: {upserts}")
    pr_ref, message, key = upserts[0]
    assert key == "recut", (
        "the upsert key must be the stable constant 'recut' — this is what "
        "makes a second delivery of the same recut UPDATE the same comment "
        f"instead of posting a new one: {upserts}")
    assert pr_ref == "github.com/o/r#1", pr_ref
    assert recut_branch in message, message
    assert DELIVERY_BRANCH in message, message

    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "succeeded", attempts[-1]
