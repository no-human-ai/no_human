"""Delivery must push the review-approved sha, never bare branch HEAD.

Audited defect: review PASS and push happen sequentially in one process, but
nothing asserted the pushed object was the reviewed commit — a branch ref
moved between review and push would ship unreviewed code while the receipt
still read "landed" (``receipts.py`` only compares the forge head to the
pushed sha, never to the reviewed sha).

Fix under test: ``Orchestrator._assert_delivery_sha`` resolves the branch tip
BEFORE ``open_pr`` and requires exact-string match to a sha a passing review
round stamped into ``task.context["review_history"]`` (the same exact-match
rule ``_already_satisfied_eligible`` already uses for orphan recovery — see
``tests/test_interrupted_review_resume.py``). A mismatch, an unreadable tip,
or absent/unstamped history all escalate — never push. A second check right
after ``open_pr`` asserts ``pr.pushed_sha`` equals that same pre-push tip,
closing the race between the assertion and the push itself.
"""
import subprocess


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitError, GitRepo
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


def _repo_with_a_commit(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "a@b.c")
    _git(work, "config", "user.name", "T")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "checkout", "-q", "-b", "nh/attempt-1")
    return work


def _commit(work, name: str, body: str, msg: str) -> str:
    (work / name).write_text(body)
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
        task, repo, "nh/attempt-1", "main", commit, attempt_id, _Result())
    return orch, task, attempt_id, out


# ---------------------------------------------------------------------------
# AC: a branch ref moved after the stamped PASS must never be pushed
# ---------------------------------------------------------------------------

async def test_a_branch_moved_after_the_pass_is_never_pushed(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    # The branch moves AFTER the review stamped `reviewed_sha` — a concurrent
    # commit landed on it before delivery ran.
    _commit(work, "extra.txt", "surprise\n", "unreviewed extra commit")

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not be called on a sha mismatch")

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), must_not_push,
        monkeypatch, events=events)

    assert out.status == TaskStatus.ESCALATED, out.detail
    mismatches = [e for e in events if e.get("kind") == "delivery_sha_mismatch"]
    assert mismatches, f"no delivery_sha_mismatch event: {events}"
    text = mismatches[0]["text"]
    assert reviewed_sha in text and _git(work, "rev-parse", "HEAD") in text, text
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


async def test_an_unmoved_branch_still_delivers(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")
    calls = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append((branch, kw))
        return _FakePR("https://github.com/o/r/pull/500", repo.head_sha())

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), fake_open_pr, monkeypatch)

    assert len(calls) == 1, calls
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "succeeded", attempts[-1]
    assert attempts[-1]["pr_url"] == "https://github.com/o/r/pull/500"


# ---------------------------------------------------------------------------
# AC: fail closed on an unreadable tip
# ---------------------------------------------------------------------------

async def test_an_unreadable_branch_tip_refuses_to_push(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")

    def raise_unreadable(self, branch):
        raise GitError(f"cannot resolve branch tip for {branch!r}")

    monkeypatch.setattr(GitRepo, "branch_sha", raise_unreadable)

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not be called on an unreadable tip")

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), must_not_push, monkeypatch)

    assert out.status == TaskStatus.ESCALATED, out.detail
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


# ---------------------------------------------------------------------------
# AC: fail closed on absent/unstamped review history
# ---------------------------------------------------------------------------

async def test_no_review_stamp_refuses_to_push(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not be called with no review stamp")

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, {}, must_not_push, monkeypatch)

    assert out.status == TaskStatus.ESCALATED, out.detail
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


async def test_an_unstamped_round_is_not_a_stamp(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    ctx = {"review_history": [
        {"round": 1, "sha": "", "passed": True, "blocking": [], "advisory": []},
    ]}

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not be called on an unstamped round")

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, ctx, must_not_push, monkeypatch)

    assert out.status == TaskStatus.ESCALATED, out.detail


async def test_a_failing_round_stamped_with_this_tip_is_not_a_pass(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not be called on a FAILing round")

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha, passed=False),
        must_not_push, monkeypatch)

    assert out.status == TaskStatus.ESCALATED, out.detail


# ---------------------------------------------------------------------------
# AC: pr.pushed_sha must equal the pre-push resolved tip
# ---------------------------------------------------------------------------

async def test_a_push_that_lands_a_different_sha_escalates(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")

    def wrong_sha_open_pr(repo, branch, title, body, **kw):
        return _FakePR("https://github.com/o/r/pull/9", "deadbeef" * 5)

    receipt_calls = []

    def must_not_verify(*a, **k):
        receipt_calls.append((a, k))
        raise AssertionError("verify_pr_receipt must not run after a pushed-sha mismatch")

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), wrong_sha_open_pr, monkeypatch,
        verify_stub=must_not_verify)

    assert not receipt_calls, "verify_pr_receipt ran despite the pushed-sha mismatch"
    assert out.status == TaskStatus.ESCALATED, out.detail
    assert out.status != TaskStatus.AWAITING_APPROVAL
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "failed", attempts[-1]


async def test_a_push_reporting_no_sha_is_advisory_not_fatal(store, tmp_path, monkeypatch):
    work = _repo_with_a_commit(tmp_path)
    reviewed_sha = _git(work, "rev-parse", "HEAD")

    def no_sha_open_pr(repo, branch, title, body, **kw):
        return _FakePR("https://github.com/o/r/pull/9", "")

    events: list[dict] = []
    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, _stamp({}, reviewed_sha), no_sha_open_pr, monkeypatch,
        events=events)

    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "succeeded", attempts[-1]
    advisories = [e["text"] for e in events if e.get("kind") == "advisory"]
    assert any("pushed-object identity unverified" in a for a in advisories), advisories


# ---------------------------------------------------------------------------
# AC: the human-gated-CI resume flow still delivers
# ---------------------------------------------------------------------------

async def test_the_human_gated_resume_still_delivers(store, tmp_path, monkeypatch):
    import no_human.core.orchestrator as orch_mod

    work = _repo_with_a_commit(tmp_path)
    tip = _git(work, "rev-parse", "HEAD")

    calls = []

    def fake_open_pr(repo, branch, title, body, **kw):
        calls.append((branch, kw))
        return _FakePR("https://github.com/o/r/pull/700", repo.head_sha())

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(orch_mod, "verify_pr_receipt", _landed_receipt)

    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    task = Task.new("Fix the thing", repo_path=str(work))
    task.context = _stamp(
        {"human_gated_ci": {"branch": "nh/attempt-1", "base": "main"}}, tip)
    await store.create_task(task)

    out = await orch._resume_human_gated(
        task, repo, {"branch": "nh/attempt-1", "base": "main"})

    assert calls, "open_pr never ran for the human-gated resume"
    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    attempts = await store.list_attempts(task.id)
    assert attempts[-1]["status"] == "succeeded", attempts[-1]


async def test_the_human_gated_resume_still_fails_closed_on_a_moved_branch(
    store, tmp_path, monkeypatch,
):
    """The `human_gated_resume` flag names the flow in the escalation message —
    it never waives the sha match. A branch that moved between the pre-parking
    stamp and resume (should never happen in the real flow, since it checks
    out the parked branch, but the gate must not trust that structurally)
    still refuses."""
    import no_human.core.orchestrator as orch_mod

    work = _repo_with_a_commit(tmp_path)
    stale_sha = _git(work, "rev-parse", "HEAD")
    _commit(work, "extra.txt", "surprise\n", "moved after the stamp")

    def must_not_push(*a, **k):
        raise AssertionError("open_pr must not run on a moved parked branch")

    monkeypatch.setattr(orch_mod, "open_pr", must_not_push)
    monkeypatch.setattr(orch_mod, "verify_pr_receipt", _landed_receipt)

    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    task = Task.new("Fix the thing", repo_path=str(work))
    task.context = _stamp(
        {"human_gated_ci": {"branch": "nh/attempt-1", "base": "main"}}, stale_sha)
    await store.create_task(task)

    out = await orch._resume_human_gated(
        task, repo, {"branch": "nh/attempt-1", "base": "main"})

    assert out.status == TaskStatus.ESCALATED, out.detail
