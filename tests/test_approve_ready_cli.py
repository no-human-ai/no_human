"""`nh approve --ready [--yes]` — list, then land, every AWAITING_APPROVAL
task whose merge_policy verdict is ready for its CURRENT branch head, and
`nh status`'s `merge-ready: N` line.

Two different algorithms are under test here, not one:

* `--ready`'s LISTING/landing path (`_go_ready` in cli/commands.py) does a
  LIVE git resolution — fetch, resolve the PR branch, `rev-parse` its tip —
  and looks the verdict up under that live head sha. A verdict stamped for
  an older commit, or one whose diff touched the policy file itself
  (`policy_changed_in_diff`), does not count as ready.
* `nh status`'s `merge-ready: N` count (`api/models.py:merge_ready_for`) is
  DB-only: it reads the LATEST attempt's `commit_sha` column and looks the
  verdict up under that — no git at all.

Idiom: real temp git repos (subprocess), `_bootstrap` patched the way
`tests/test_approve.py` does; `land_task` is always faked via
`monkeypatch.setattr(approve_merge_mod, "land_task", ...)` (the local import
inside `_land_one` re-resolves the module attribute on every call), so no
`origin`/`gh` stub is needed — `GitRepo.fetch()` no-ops on a repo with no
configured remote and `GitRepo.resolve_commitish()` resolves a purely local
branch directly.
"""

from __future__ import annotations

import asyncio
import subprocess
import unittest.mock as mock

from click.testing import CliRunner

import no_human.vcs.approve_merge as approve_merge_mod
from no_human.cli.commands import approve, status
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs.approve_merge import LandResult

# --------------------------------------------------------------------------- #
# git plumbing — real temp repos (copied from tests/test_approve.py)          #
# --------------------------------------------------------------------------- #

def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _git_out(repo_path, *args):
    return subprocess.run(["git", "-C", str(repo_path), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _repo_with_feature_branch(tmp_path, name="repo"):
    """A `main` with one commit, plus a `feature` branch one commit ahead —
    purely local, never pushed anywhere. Returns (repo_path, head_sha) where
    head_sha is `feature`'s tip — exactly what `resolve_commitish("feature")`
    + `rev-parse` resolve to inside `_go_ready`/`_land_one`."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")
    _git(repo, "checkout", "main")
    head_sha = _git_out(repo, "rev-parse", "feature")
    return repo, head_sha


# --------------------------------------------------------------------------- #
# CLI harness (copied from tests/test_approve.py)                             #
# --------------------------------------------------------------------------- #

class _Cfg:
    db_path = None
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)


def _cfg(db_path):
    c = _Cfg()
    c.db_path = db_path
    return c


def _invoke(cmd, db, args):
    import no_human.cli.commands as cmd_mod
    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=False: (_cfg(db), None)), \
         mock.patch.object(cmd_mod, "_probe_pool",
                           lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED)):
        return CliRunner().invoke(cmd, args)


def _task_state(db, task_id):
    async def _go():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            events = await store.list_events(task_id)
            return t, events
    return asyncio.run(_go())


def _awaiting_order(db):
    """The actual `store.list_tasks()` order (`ORDER BY created_at DESC`) —
    "discovery order" as `--ready` sees it must be read from the DB, never
    assumed from creation sequence (ties at timestamp resolution)."""
    async def _go():
        async with Store(db) as store:
            tasks = await store.list_tasks()
            return [t.id for t in tasks if t.status == TaskStatus.AWAITING_APPROVAL]
    return asyncio.run(_go())


_RULES = [
    {"name": "tests_pass", "passed": True, "detail": "12/12 passed"},
    {"name": "no_todo", "passed": True, "detail": ""},
]


def _ready_task(db, tmp_path, *, title, repo_name, review_passed=True,
                 mp_ready=True, mp_policy_changed=False, mp_sha=None,
                 rules=None):
    """An AWAITING_APPROVAL task with a real local feature branch, a
    `pr_watch`/`pr_branch` pair (all `resolve_task_pr` needs — no PR event
    log required), a `merge_policy` verdict, and a `review_history` round
    stamped on the branch head (all `_review_pass_evidence` needs). Returns
    (task_id, repo_path, head_sha)."""
    repo, head_sha = _repo_with_feature_branch(tmp_path, repo_name)
    verdict_sha = mp_sha if mp_sha is not None else head_sha

    async def _go():
        async with Store(db) as store:
            t = Task.new(title, repo_path=str(repo))
            t.context = {
                "pr_watch": "https://example.invalid/pr/1",
                "pr_branch": "feature",
                "merge_policy": {
                    verdict_sha: {
                        "ready": mp_ready,
                        "policy_changed_in_diff": mp_policy_changed,
                        "rules": [dict(r) for r in (rules or _RULES)],
                    },
                },
                "review_history": [{"sha": head_sha, "passed": review_passed}],
            }
            await store.create_task(t)
            await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            return t.id
    return asyncio.run(_go()), repo, head_sha


def _never_called_land_task(*args, **kwargs):
    raise AssertionError("land_task must not be called")


# --------------------------------------------------------------------------- #
# --ready (list only)                                                         #
# --------------------------------------------------------------------------- #

def test_ready_without_yes_lists_and_lands_nothing(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    id_a, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a")
    id_b, _, _ = _ready_task(db, tmp_path, title="Task B", repo_name="repo-b")

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    assert id_a[:8] in result.output
    assert id_b[:8] in result.output
    assert "Task A" in result.output
    assert "Task B" in result.output
    assert result.output.count("rules 2/2") == 2
    assert "https://example.invalid/pr/1" in result.output
    assert "2 task(s) merge-ready" in result.output
    assert "--yes to land them" in result.output

    t_a, _ = _task_state(db, id_a)
    t_b, _ = _task_state(db, id_b)
    assert t_a.status is TaskStatus.AWAITING_APPROVAL
    assert t_b.status is TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# --ready --yes                                                               #
# --------------------------------------------------------------------------- #

def test_ready_yes_lands_in_discovery_order(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    id_a, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a")
    id_b, _, _ = _ready_task(db, tmp_path, title="Task B", repo_name="repo-b")

    expected_order = _awaiting_order(db)
    assert set(expected_order) == {id_a, id_b}

    seen_order = []

    def _fake(*, task_id, **kwargs):
        seen_order.append(task_id)
        return LandResult(ok=True, step="close_pr",
                           landed_sha="ab" * 20, message="landed by fake")

    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 0, result.output
    assert seen_order == expected_order
    assert result.output.count("merged") == 2
    assert ("ab" * 20)[:12] in result.output  # landed_sha[:12], as printed

    t_a, _ = _task_state(db, id_a)
    t_b, _ = _task_state(db, id_b)
    assert t_a.status is TaskStatus.DONE
    assert t_b.status is TaskStatus.DONE


def test_ready_yes_stops_at_first_failure(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    id_a, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a")
    id_b, _, _ = _ready_task(db, tmp_path, title="Task B", repo_name="repo-b")

    expected_order = _awaiting_order(db)
    first_id, second_id = expected_order

    calls = []

    def _fake(*, task_id, **kwargs):
        calls.append(task_id)
        if len(calls) > 1:
            raise AssertionError("land_task must not be called for the second task")
        return LandResult(ok=False, step="push", stderr="")

    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 1, result.output
    assert calls == [first_id]
    assert "step 'push'" in result.output
    assert "landed 0/2 before stopping." in result.output

    t_first, _ = _task_state(db, first_id)
    t_second, _ = _task_state(db, second_id)
    assert t_first.status is TaskStatus.AWAITING_APPROVAL
    assert t_second.status is TaskStatus.AWAITING_APPROVAL


def test_ready_yes_continues_past_skipped_land(tmp_path, monkeypatch):
    """`land_task` returns `skipped=True, ok=True` on entirely normal,
    non-failure paths — `gh` not installed, or `approve_merge` disabled
    (vcs/approve_merge.py:620-629). Single-task `nh approve <task_id>`
    treats that as success ("approved ... merge the PR in your git host",
    exit 0, no sys.exit). The batch must match: a skipped land is not a
    "stop at the first failure" — it must count as a completed step and the
    batch must keep walking the remaining ready tasks."""
    db = tmp_path / "nh.db"
    id_a, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a")
    id_b, _, _ = _ready_task(db, tmp_path, title="Task B", repo_name="repo-b")

    expected_order = _awaiting_order(db)
    first_id, second_id = expected_order

    calls = []

    def _fake(*, task_id, **kwargs):
        calls.append(task_id)
        return LandResult(ok=True, step="preconditions", skipped=True,
                           message="gh CLI not found — cannot merge automatically")

    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == [first_id, second_id]
    assert "stopped at" not in result.output
    assert result.output.count("approved") == 2
    assert "gh CLI not found" in result.output

    t_first, _ = _task_state(db, first_id)
    t_second, _ = _task_state(db, second_id)
    # A skipped land never calls store.set_status(DONE) — approval is
    # recorded but the task stays awaiting_approval until a human merges
    # the PR themselves, exactly like the single-task path.
    assert t_first.status is TaskStatus.AWAITING_APPROVAL
    assert t_second.status is TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# exclusion rules — a stale-sha verdict, and one whose diff touched policy    #
# --------------------------------------------------------------------------- #

def test_stale_sha_verdict_is_not_ready(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    stale_sha = "f" * 40
    task_id, _, _ = _ready_task(db, tmp_path, title="Stale", repo_name="repo",
                                mp_sha=stale_sha)

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    assert task_id[:8] not in result.output
    assert "no awaiting_approval task is merge-ready" in result.output


def test_policy_changed_in_diff_verdict_is_not_ready(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    task_id, _, _ = _ready_task(db, tmp_path, title="Policy Changed", repo_name="repo",
                                mp_ready=True, mp_policy_changed=True)

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    assert task_id[:8] not in result.output
    assert "no awaiting_approval task is merge-ready" in result.output


# --------------------------------------------------------------------------- #
# --ready --yes still enforces the review-pass precondition                   #
# --------------------------------------------------------------------------- #

def test_ready_yes_respects_review_pass_precondition(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    task_id, _, _ = _ready_task(db, tmp_path, title="No Review", repo_name="repo",
                                review_passed=False)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 1, result.output
    assert "step 'precondition'" in result.output

    t, _ = _task_state(db, task_id)
    assert t.status is TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# mutual exclusivity refusals                                                 #
# --------------------------------------------------------------------------- #

def test_ready_with_task_id_is_rejected(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    result = _invoke(approve, db, ["--ready", "sometaskid"])

    assert result.exit_code != 0
    assert result.exit_code == 2


def test_yes_without_ready_is_rejected(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    result = _invoke(approve, db, ["--yes"])

    assert result.exit_code != 0
    assert result.exit_code == 2


# --------------------------------------------------------------------------- #
# nh status's merge-ready: N line — the DB-only algorithm (api/models.py's    #
# merge_ready_for), distinct from --ready's live git resolution above         #
# --------------------------------------------------------------------------- #

def _status_task(db, tmp_path, *, title, repo_name, status_, mp_sha=None, ready=True):
    """Same shape as `_ready_task` (real repo/branch, `pr_watch`/`pr_branch`,
    a `merge_policy` verdict) but with a caller-chosen terminal `status_` —
    `nh status`'s count only ever counts AWAITING_APPROVAL tasks, so a DONE
    one with an otherwise-ready verdict must stay excluded regardless of
    head freshness. `mp_sha` defaults to the branch's real head (fresh);
    pass a different 40-hex sha to stamp a verdict that reads as stale."""
    repo, head_sha = _repo_with_feature_branch(tmp_path, repo_name)
    verdict_sha = mp_sha if mp_sha is not None else head_sha

    async def _go():
        async with Store(db) as store:
            t = Task.new(title, repo_path=str(repo))
            t.context = {
                "pr_watch": "https://example.invalid/pr/1",
                "pr_branch": "feature",
                "merge_policy": {
                    verdict_sha: {
                        "ready": ready,
                        "policy_changed_in_diff": False,
                        "rules": [dict(r) for r in _RULES],
                    },
                },
            }
            await store.create_task(t)
            event = ({"source": "test", "kind": "human_merged", "text": ""}
                      if status_ is TaskStatus.DONE else None)
            await store.set_status(t, status_, validate=False, event=event)
            return t.id
    return asyncio.run(_go())


def test_status_prints_merge_ready_count(tmp_path):
    db = tmp_path / "nh.db"

    _status_task(db, tmp_path, title="Ready Awaiting", repo_name="repo-a",
                 status_=TaskStatus.AWAITING_APPROVAL, ready=True)

    _status_task(db, tmp_path, title="Ready But Done", repo_name="repo-b",
                 status_=TaskStatus.DONE, ready=True)

    _status_task(db, tmp_path, title="Stale Verdict Awaiting", repo_name="repo-c",
                 status_=TaskStatus.AWAITING_APPROVAL, mp_sha="d" * 40, ready=True)

    result = _invoke(status, db, [])

    assert result.exit_code == 0, result.output
    assert "merge-ready: 1" in result.output

# --------------------------------------------------------------------------- #
# head-freshness: a verdict stamped for a since-moved head is not ready on   #
# ANY surface until re-stamped for the new head (both directions, one test)  #
# --------------------------------------------------------------------------- #

def _advance_feature_branch(repo):
    """Adds a new commit to `feature` (checked out, then back to `main`,
    mirroring `_repo_with_feature_branch`) — simulating a fixup push made
    AFTER a verdict was already stamped for the old tip (e.g. a PR sent back
    for fixes). Returns the new head sha."""
    _git(repo, "checkout", "feature")
    (repo / "c.txt").write_text("more change\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-m", "fixup commit")
    _git(repo, "checkout", "main")
    return _git_out(repo, "rev-parse", "feature")


def _stamp_verdict(db, task_id, sha, *, ready=True, policy_changed_in_diff=False,
                    rules=None):
    """Adds ANOTHER merge_policy verdict, keyed by `sha`, on top of whatever
    is already there — `store.merge_context`'s RFC-7396 merge keeps the old
    entry too, exactly like a re-run orchestrator stamp would."""
    async def _go():
        async with Store(db) as store:
            await store.merge_context(task_id, {
                "merge_policy": {
                    sha: {
                        "ready": ready,
                        "policy_changed_in_diff": policy_changed_in_diff,
                        "rules": [dict(r) for r in (rules or _RULES)],
                    },
                },
            })
    asyncio.run(_go())


def test_stale_verdict_is_merge_ready_on_no_surface_until_restamped(tmp_path, monkeypatch):
    """AC1 + AC2 in ONE test, both directions: a verdict stamped for the
    CURRENT head is ready everywhere (`--ready` lists it, `status` counts
    it). Once the branch moves past that sha (a fixup push after the
    verdict), BOTH stop offering it. Once a NEW verdict is stamped for the
    NEW head, BOTH offer it again."""
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    task_id, repo, head_sha = _ready_task(db, tmp_path, title="Movable",
                                          repo_name="repo")

    fresh_ready = _invoke(approve, db, ["--ready"])
    assert fresh_ready.exit_code == 0, fresh_ready.output
    assert task_id[:8] in fresh_ready.output
    fresh_status = _invoke(status, db, [])
    assert "merge-ready: 1" in fresh_status.output

    new_head = _advance_feature_branch(repo)
    assert new_head != head_sha

    stale_ready = _invoke(approve, db, ["--ready"])
    assert stale_ready.exit_code == 0, stale_ready.output
    assert task_id[:8] not in stale_ready.output
    assert "no awaiting_approval task is merge-ready" in stale_ready.output
    stale_status = _invoke(status, db, [])
    assert "merge-ready: 0" in stale_status.output

    _stamp_verdict(db, task_id, new_head, ready=True)

    restamped_ready = _invoke(approve, db, ["--ready"])
    assert restamped_ready.exit_code == 0, restamped_ready.output
    assert task_id[:8] in restamped_ready.output
    restamped_status = _invoke(status, db, [])
    assert "merge-ready: 1" in restamped_status.output


# --------------------------------------------------------------------------- #
# AC3: no verdict at all, and a failing verdict, behave exactly as today —   #
# each with its own positive control in the same test                       #
# --------------------------------------------------------------------------- #

def _no_verdict_task(db, tmp_path, *, title, repo_name):
    """Same shape as `_ready_task` (real repo/branch, `pr_watch`/`pr_branch`)
    but with NO `merge_policy` key in context at all — a task that has
    simply never had a verdict stamped."""
    repo, head_sha = _repo_with_feature_branch(tmp_path, repo_name)

    async def _go():
        async with Store(db) as store:
            t = Task.new(title, repo_path=str(repo))
            t.context = {
                "pr_watch": "https://example.invalid/pr/1",
                "pr_branch": "feature",
                "review_history": [{"sha": head_sha, "passed": True}],
            }
            await store.create_task(t)
            await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            return t.id
    return asyncio.run(_go()), repo, head_sha


def test_no_verdict_at_all_is_merge_ready_nowhere(tmp_path, monkeypatch):
    """A task that has never had a merge-policy verdict stamped stays
    unready on both surfaces — unaffected by this change — pinned with a
    positive control (a sibling task WITH a fresh verdict) in the same
    test, so this can't pass vacuously."""
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    no_verdict_id, _, _ = _no_verdict_task(db, tmp_path, title="No Verdict",
                                           repo_name="repo-a")
    ready_id, _, _ = _ready_task(db, tmp_path, title="Ready", repo_name="repo-b")

    ready_result = _invoke(approve, db, ["--ready"])
    assert ready_result.exit_code == 0, ready_result.output
    assert no_verdict_id[:8] not in ready_result.output
    assert ready_id[:8] in ready_result.output

    status_result = _invoke(status, db, [])
    assert "merge-ready: 1" in status_result.output


def test_failing_verdict_is_merge_ready_nowhere(tmp_path, monkeypatch):
    """A task with a FRESH verdict that says `ready: False` stays unready
    on both surfaces — unaffected by this change — pinned with a positive
    control (a sibling task whose fresh verdict IS ready) in the same
    test."""
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    failing_id, _, _ = _ready_task(db, tmp_path, title="Failing", repo_name="repo-a",
                                   mp_ready=False)
    ready_id, _, _ = _ready_task(db, tmp_path, title="Ready", repo_name="repo-b")

    ready_result = _invoke(approve, db, ["--ready"])
    assert ready_result.exit_code == 0, ready_result.output
    assert failing_id[:8] not in ready_result.output
    assert ready_id[:8] in ready_result.output

    status_result = _invoke(status, db, [])
    assert "merge-ready: 1" in status_result.output


# --------------------------------------------------------------------------- #
# AC4: the head-freshness rule lives in ONE place — the landing verb and     #
# the human-facing surfaces can only ever agree over the same mixed set      #
# --------------------------------------------------------------------------- #

def test_landing_verb_and_human_surfaces_agree_over_the_same_tasks(tmp_path, monkeypatch):
    """`--ready` (the landing verb) and `nh status`'s `merge-ready: N` count
    (a human-facing surface) must never disagree about which of a MIXED set
    of tasks — fresh-ready, stale, failing, policy-changed-in-diff, and
    no-verdict-at-all — is ready. Both derive their answer from the exact
    same function (`api.models.merge_ready_for`) over the exact same kind of
    git-resolved head (`vcs.task_pr.resolve_head_sha`); this test also calls
    that function directly over every task in the set and asserts its
    verdict can only ever match what `--ready` listed — the single place the
    rule lives is demonstrated, not just asserted."""
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    fresh_id, _, _ = _ready_task(db, tmp_path, title="Fresh", repo_name="repo-fresh")
    stale_id, _, _ = _ready_task(db, tmp_path, title="Stale", repo_name="repo-stale",
                                 mp_sha="e" * 40)
    failing_id, _, _ = _ready_task(db, tmp_path, title="Failing", repo_name="repo-failing",
                                   mp_ready=False)
    policy_id, _, _ = _ready_task(db, tmp_path, title="PolicyChanged",
                                  repo_name="repo-policy", mp_policy_changed=True)
    no_verdict_id, _, _ = _no_verdict_task(db, tmp_path, title="NoVerdict",
                                           repo_name="repo-none")
    all_ids = (fresh_id, stale_id, failing_id, policy_id, no_verdict_id)

    ready_result = _invoke(approve, db, ["--ready"])
    assert ready_result.exit_code == 0, ready_result.output
    listed = {tid for tid in all_ids if tid[:8] in ready_result.output}
    assert listed == {fresh_id}

    status_result = _invoke(status, db, [])
    assert "merge-ready: 1" in status_result.output

    from no_human.api.models import merge_ready_for
    from no_human.vcs.task_pr import resolve_head_sha

    async def _go():
        async with Store(db) as store:
            results = {}
            for tid in all_ids:
                t = await store.get_task(tid)
                head_sha = await resolve_head_sha(store, t, git_cfg={}, fetch=True)
                results[tid] = merge_ready_for(t, head_sha) is True
            return results
    results = asyncio.run(_go())

    assert results == {
        fresh_id: True, stale_id: False, failing_id: False,
        policy_id: False, no_verdict_id: False,
    }
    assert {tid for tid, ok in results.items() if ok} == listed


def test_merge_ready_for_is_none_for_a_falsy_head_sha_even_with_a_verdict_on_record():
    """A falsy `head_sha` (`""`/`None` — "could not resolve a fresh head")
    must never fall back to trusting SOME stored verdict just because one
    happens to exist; that would readmit exactly the staleness bug this
    predicate exists to close. Positive control: the SAME task, asked with
    its actual verdict sha, reads the verdict normally."""
    from no_human.api.models import merge_ready_for
    from no_human.core.task import Task

    t = Task.new("T", repo_path="/tmp/x")
    t.context = {"merge_policy": {"abc123": {"ready": True}}}

    assert merge_ready_for(t, "") is None
    assert merge_ready_for(t, None) is None
    assert merge_ready_for(t, "abc123") is True


def test_merge_ready_for_is_none_when_the_verdict_omits_the_ready_key():
    """A verdict dict recorded for the CURRENT head but missing the `ready`
    key (a malformed/partial stamp) must read as "unknown", not silently as
    ready. Positive control: the same shape WITH the key present reads
    correctly."""
    from no_human.api.models import merge_ready_for
    from no_human.core.task import Task

    incomplete = Task.new("T", repo_path="/tmp/x")
    incomplete.context = {"merge_policy": {"abc123": {"summary": "no ready key yet"}}}
    assert merge_ready_for(incomplete, "abc123") is None

    complete = Task.new("T2", repo_path="/tmp/x")
    complete.context = {"merge_policy": {"abc123": {"ready": True}}}
    assert merge_ready_for(complete, "abc123") is True


# --------------------------------------------------------------------------- #
# the advisory note on the one-line --ready summary                           #
# --------------------------------------------------------------------------- #
# A verifier that never reached a verdict leaves the task READY — the rule
# passes — so `rules N/N` alone tells the operator nothing happened, when in
# fact a verifier never answered. The detail text is not restated here: it is
# produced by the shipped `_check_verifiers_all_satisfied` from real GateFacts,
# so a change to that wording breaks this test instead of silently escaping it.

def _verifiers_rule(*, unavailable: tuple[str, ...]) -> dict:
    from no_human.core.merge_policy import GateFacts, _check_verifiers_all_satisfied
    facts = GateFacts(review_passed=True, verifiers_ran=3, verifiers_failed=(),
                      verifiers_unavailable=unavailable)
    passed, detail = _check_verifiers_all_satisfied(facts, None)
    return {"name": "verifiers_all_satisfied", "passed": passed, "detail": detail}


def test_ready_line_names_a_verifier_that_never_answered(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    rules = [dict(_RULES[0]), _verifiers_rule(unavailable=("no-todo", "no-print"))]
    task_id, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a",
                                rules=rules)

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    assert task_id[:8] in result.output
    assert "rules 2/2" in result.output
    # The whole parenthetical, both verifier ids, on the same line as the task.
    line = next(ln for ln in result.output.splitlines() if task_id[:8] in ln)
    assert "2 no verdict (advisory): no-todo, no-print" in line, line


def test_ready_line_carries_no_note_when_every_verifier_answered(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    rules = [dict(_RULES[0]), _verifiers_rule(unavailable=())]
    task_id, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a",
                                rules=rules)

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    line = next(ln for ln in result.output.splitlines() if task_id[:8] in ln)
    assert "no verdict" not in line, line


# --------------------------------------------------------------------------- #
# `resolve_head_sha`'s live-fetch ladder                                      #
# --------------------------------------------------------------------------- #
# The local-first resolution ladder bug: `GitRepo.resolve_commitish` prefers a
# LOCAL branch ref over `origin/<branch>`, so a clone that never fetched sees
# its own stale tip even after another clone pushed a fixup to the shared
# remote. `resolve_head_sha(fetch=True)` must sidestep that ladder entirely
# via `git ls-remote` — never trusting the local ref — or a fixup pushed from
# a different clone/worktree than `task.repo_path` would stay invisible.

def _repo_with_remote_and_lagging_local_ref(tmp_path):
    """A bare "origin", clone `a` (has `origin` configured, `feature` at its
    ORIGINAL tip, never fetches again) and clone `b` (pushes one more commit
    to `feature` on the shared remote that `a` never learns about locally).
    Returns (repo_a_path, stale_sha, fresh_sha)."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", "main")

    a = tmp_path / "a"
    a.mkdir()
    _git(a, "init", "-b", "main")
    _git(a, "config", "user.email", "t@example.com")
    _git(a, "config", "user.name", "t")
    _git(a, "remote", "add", "origin", str(bare))
    (a / "a.txt").write_text("orig\n")
    _git(a, "add", "a.txt")
    _git(a, "commit", "-m", "initial")
    _git(a, "push", "origin", "main")
    _git(a, "checkout", "-b", "feature")
    (a / "b.txt").write_text("change\n")
    _git(a, "add", "b.txt")
    _git(a, "commit", "-m", "feature commit")
    _git(a, "push", "origin", "feature")
    _git(a, "checkout", "main")
    stale_sha = _git_out(a, "rev-parse", "feature")

    b = tmp_path / "b"
    _git(tmp_path, "clone", str(bare), str(b))
    _git(b, "config", "user.email", "t2@example.com")
    _git(b, "config", "user.name", "t2")
    _git(b, "checkout", "feature")
    (b / "c.txt").write_text("fixup from another clone\n")
    _git(b, "add", "c.txt")
    _git(b, "commit", "-m", "fixup pushed from clone b")
    _git(b, "push", "origin", "feature")
    fresh_sha = _git_out(b, "rev-parse", "feature")

    assert stale_sha != fresh_sha
    return a, stale_sha, fresh_sha


def test_resolve_head_sha_reads_the_remote_tip_not_a_lagging_local_ref(tmp_path):
    """`fetch=True` must answer with what `origin` advertises RIGHT NOW (via
    `ls_remote_exact`), not `a`'s own never-refreshed local `feature` ref —
    otherwise a fixup pushed from clone `b` never becomes visible to a task
    whose `repo_path` is clone `a`, no matter how long it waits.
    `fetch=False` is the positive control: it must still resolve to `a`'s
    local ref, proving the two arguments genuinely take different paths
    rather than `fetch` being a no-op."""
    from no_human.core.db import Store
    from no_human.core.task import Task
    from no_human.vcs.task_pr import resolve_head_sha

    db = tmp_path / "nh.db"
    repo_a, stale_sha, fresh_sha = _repo_with_remote_and_lagging_local_ref(tmp_path)

    async def _go():
        async with Store(db) as store:
            t = Task.new("Ladder", repo_path=str(repo_a))
            t.context = {"pr_watch": "https://example.invalid/pr/1",
                         "pr_branch": "feature"}
            await store.create_task(t)
            live = await resolve_head_sha(store, t, git_cfg={}, fetch=True)
            local = await resolve_head_sha(store, t, git_cfg={}, fetch=False)
            return live, local
    live, local = asyncio.run(_go())

    assert live == fresh_sha
    assert local == stale_sha


# --------------------------------------------------------------------------- #
# `head_shas_for`'s cost-bounding filter                                      #
# --------------------------------------------------------------------------- #
# A board tick must not re-resolve a head for every task that has EVER
# carried a merge_policy verdict — only for tasks that could actually show a
# MERGE-READY chip right now, i.e. sitting in AWAITING_APPROVAL. Otherwise a
# long-lived fleet where most tasks have long since landed or failed re-pays
# a git round trip per tick for all of them, unboundedly.

def test_head_shas_for_never_touches_git_for_tasks_outside_the_review_lane(tmp_path, monkeypatch):
    from no_human.core.db import Store
    from no_human.core.task import Task, TaskStatus
    import no_human.vcs.task_pr as task_pr_mod

    db = tmp_path / "nh.db"
    repo, head_sha = _repo_with_feature_branch(tmp_path, "repo")

    calls: list[str] = []
    real_resolve = task_pr_mod.resolve_head_sha

    async def _counting_resolve(store, task, **kwargs):
        calls.append(task.id)
        return await real_resolve(store, task, **kwargs)
    monkeypatch.setattr(task_pr_mod, "resolve_head_sha", _counting_resolve)

    async def _go():
        async with Store(db) as store:
            done = Task.new("Landed", repo_path=str(repo))
            done.context = {"pr_watch": "https://example.invalid/pr/1",
                            "pr_branch": "feature",
                            "merge_policy": {head_sha: {"ready": True}}}
            await store.create_task(done)
            await store.set_status(done, TaskStatus.DONE, validate=False, event={
                "source": "human", "kind": "human_merged", "sha": head_sha, "ts": 0,
            })

            failed = Task.new("Failed", repo_path=str(repo))
            failed.context = {"pr_watch": "https://example.invalid/pr/1",
                              "pr_branch": "feature",
                              "merge_policy": {head_sha: {"ready": False}}}
            await store.create_task(failed)
            await store.set_status(failed, TaskStatus.FAILED, validate=False)

            no_verdict = Task.new("Awaiting, no verdict yet", repo_path=str(repo))
            no_verdict.context = {"pr_watch": "https://example.invalid/pr/1",
                                  "pr_branch": "feature"}
            await store.create_task(no_verdict)
            await store.set_status(no_verdict, TaskStatus.AWAITING_APPROVAL,
                                    validate=False)

            # Positive control: the ONE task allowed to cost a git call.
            ready = Task.new("Awaiting, has verdict", repo_path=str(repo))
            ready.context = {"pr_watch": "https://example.invalid/pr/1",
                             "pr_branch": "feature",
                             "merge_policy": {head_sha: {"ready": True}}}
            await store.create_task(ready)
            await store.set_status(ready, TaskStatus.AWAITING_APPROVAL,
                                    validate=False)

            tasks = [done, failed, no_verdict, ready]
            heads = await task_pr_mod.head_shas_for(store, tasks, git_cfg={},
                                                     fetch=True)
            return heads, ready.id
    heads, ready_id = asyncio.run(_go())

    assert calls == [ready_id]
    assert heads == {ready_id: head_sha}
