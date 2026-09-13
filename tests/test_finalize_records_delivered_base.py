"""`_finalize` must record the trunk tip a delivered PR was measured against.

Audited defect (task 22c4ddf6 finding #3, "a stale-but-mergeable PR is never
re-measured or woken"): `base_sha` was never recorded at delivery at all, so
nothing could later tell a fresh AWAITING_APPROVAL task from a stale one.
`core/orchestrator.py:_finalize` now calls `vcs.delivered_base
.record_at_delivery(repo_path, base)` and merges its result into
`task.context` right after `pr_delivered_url`/`pr_comment_since` are set.

This is a pure wiring test: it does not re-test `record_at_delivery`'s own
fetch/resolve logic (that belongs to `tests/test_wake_base_stale.py`'s
`test_delivery_records_the_trunk_tip_it_was_measured_against` and friends).
It exists because nothing previously pinned that `_finalize` calls it at
all — `grep -rn "record_at_delivery" tests/` before this file only matched
`test_wake_base_stale.py`, which exercises the helper directly, never through
`_finalize`. Delete the `ctx.update(await delivered_base.record_at_delivery(
...))` line from `_finalize` and this file's tests fail.
"""
import subprocess

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import delivered_base
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


def _stamp(sha: str) -> dict:
    return {"review_history": [
        {"round": 1, "sha": sha, "passed": True, "blocking": [], "advisory": []},
    ]}


async def _finalize_task(store, tmp_path, work, open_pr_stub, monkeypatch):
    import no_human.core.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "open_pr", open_pr_stub)
    monkeypatch.setattr(orch_mod, "verify_pr_receipt", _landed_receipt)

    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    reviewed_sha = repo.head_sha()
    task = Task.new("Fix the thing", repo_path=str(work))
    task.context = _stamp(reviewed_sha)
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)

    commit = _Commit()
    commit.sha = reviewed_sha
    out = await orch._finalize(
        task, repo, "nh/attempt-1", "main", commit, attempt_id, _Result())
    return orch, task, attempt_id, out


async def test_finalize_calls_record_at_delivery_and_merges_its_result(
    store, tmp_path, monkeypatch,
):
    work = _repo_with_a_commit(tmp_path)
    calls = []

    async def spy_record_at_delivery(repo_path, base):
        calls.append((repo_path, base))
        return {"pr_base_sha": "deadbeef" * 5, "pr_base_ref": base}

    monkeypatch.setattr(delivered_base, "record_at_delivery", spy_record_at_delivery)

    def fake_open_pr(repo, branch, title, body, **kw):
        return _FakePR("https://github.com/o/r/pull/500", repo.head_sha())

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, fake_open_pr, monkeypatch)

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    assert calls == [(str(work), "main")], calls

    stored = await store.get_task(task.id)
    assert stored.context.get("pr_base_sha") == "deadbeef" * 5, stored.context
    assert stored.context.get("pr_base_ref") == "main", stored.context


async def test_finalize_tolerates_record_at_delivery_returning_nothing(
    store, tmp_path, monkeypatch,
):
    """An unresolvable trunk tip at delivery time (e.g. no network) must not
    fail delivery — `record_at_delivery` already fails closed by returning
    `{}` (see its own docstring), and `_finalize` must accept that and still
    deliver; the absence of `pr_base_sha` is what later makes `measure()`
    answer UNDETERMINED rather than a false FRESH."""
    work = _repo_with_a_commit(tmp_path)

    async def empty_record_at_delivery(repo_path, base):
        return {}

    monkeypatch.setattr(delivered_base, "record_at_delivery", empty_record_at_delivery)

    def fake_open_pr(repo, branch, title, body, **kw):
        return _FakePR("https://github.com/o/r/pull/501", repo.head_sha())

    orch, task, attempt_id, out = await _finalize_task(
        store, tmp_path, work, fake_open_pr, monkeypatch)

    assert out.status == TaskStatus.AWAITING_APPROVAL, out.detail
    stored = await store.get_task(task.id)
    assert "pr_base_sha" not in (stored.context or {}), stored.context
