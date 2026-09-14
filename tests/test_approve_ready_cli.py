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


def _repo_with_conflicting_feature_branch(tmp_path, name="repo"):
    """Like `_repo_with_feature_branch`, except `main` moves AFTER `feature`
    branches off, and both edit the same file (`a.txt`) — the shape of the
    real incident this bug report is about: the six merge_policy rules were
    stamped for `feature`'s head and still say ready, but `feature` no
    longer merges into `main`'s current tip. Returns (repo_path, head_sha)
    for `feature`."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("feature edit\n")
    _git(repo, "commit", "-am", "feature edits a.txt")
    head_sha = _git_out(repo, "rev-parse", "feature")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("main edit\n")
    _git(repo, "commit", "-am", "main edits a.txt after branching")
    return repo, head_sha


def _repo_with_remote_only_conflicting_branch(tmp_path, name="repo"):
    """Like `_repo_with_conflicting_feature_branch`, except `feature` is
    pushed to a real `origin` remote and then deleted LOCALLY, so the only
    ref naming it afterwards is `refs/remotes/origin/feature` — the norm for
    this repo's squash-from-worktree landing checkout, where a PR branch
    routinely has no local ref at all (`git.py:389-401`'s
    `resolve_commitish` docstring; `pr_watcher._base_tips`'s docstring lines
    991-1001). Reproduces the exact failure mode this fixture exists to
    catch: `check_landability` called with the BARE branch name would ask
    `refs_resolvable(repo_path, "feature")` — a plain `rev-parse --verify
    feature^{commit}` with no `origin/` fallback — which fails for this
    fixture and silently degrades the verdict to `state="unknown"`, masking
    a real conflict as fail-open-landable instead of reporting `state=
    "conflict"`. Returns (repo_path, head_sha) for `feature`, exactly as its
    sibling fixtures do."""
    upstream = tmp_path / f"{name}-upstream.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(upstream)],
                    check=True, capture_output=True)

    repo = _make_repo(tmp_path, name)
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "push", "origin", "main")

    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("feature edit\n")
    _git(repo, "commit", "-am", "feature edits a.txt")
    head_sha = _git_out(repo, "rev-parse", "feature")
    _git(repo, "push", "origin", "feature")

    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("main edit\n")
    _git(repo, "commit", "-am", "main edits a.txt after branching")

    _git(repo, "branch", "-D", "feature")
    # Confirm the fixture actually built the "no local ref" shape it claims.
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         "feature^{commit}"],
        capture_output=True).returncode != 0
    assert _git_out(repo, "rev-parse", "origin/feature") == head_sha
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
                 rules=None, repo_factory=_repo_with_feature_branch):
    """An AWAITING_APPROVAL task with a real local feature branch, a
    `pr_watch`/`pr_branch` pair (all `resolve_task_pr` needs — no PR event
    log required), a `merge_policy` verdict, and a `review_history` round
    stamped on the branch head (all `_review_pass_evidence` needs). Returns
    (task_id, repo_path, head_sha). `repo_factory` defaults to a clean
    feature branch; pass `_repo_with_conflicting_feature_branch` to build a
    task whose rules verdict is ready but whose branch currently conflicts
    with its base."""
    repo, head_sha = repo_factory(tmp_path, repo_name)
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
# live base mergeability — quality rules passing is not the whole story: the  #
# base can move (a sibling PR landing rewrites RELEASE_MANIFEST.txt) and      #
# leave a rules-passing verdict pointing at a branch that no longer merges    #
# --------------------------------------------------------------------------- #

def test_quality_ready_but_conflicting_task_is_not_presented_as_ready_to_land(
        tmp_path, monkeypatch):
    """The exact bug this module fixes: rules 2/2, but the branch conflicts
    with its CURRENT base. It must still be printed (never hidden), but must
    NOT be folded back into "ready to land" — no CONFLICT verdict is allowed
    to disappear from the summary or the --yes landing set."""
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    task_id, _, _ = _ready_task(
        db, tmp_path, title="Conflicted", repo_name="repo",
        repo_factory=_repo_with_conflicting_feature_branch)

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    # Not hidden: it is still listed, with its rules verdict.
    assert task_id[:8] in result.output
    assert "rules 2/2" in result.output
    # But visibly not landable: a CONFLICT marker on its line...
    line = next(ln for ln in result.output.splitlines() if task_id[:8] in ln)
    assert "CONFLICT" in line, line
    # ...and it must not be counted among the ready-to-land tasks.
    assert "0 task(s) ready to land" in result.output
    assert "1 task(s) pass the quality rules but do NOT merge" in result.output
    assert "re-run with --yes to land the ready one(s)" not in result.output


def test_clean_task_that_passes_rules_is_still_listed_as_ready(tmp_path, monkeypatch):
    """Counter-case: rules 2/2 AND the branch merges cleanly into its
    current base — must still show up as ready, with a clean merge marker,
    not swept up by the new conflict-detection code path."""
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    task_id, _, _ = _ready_task(db, tmp_path, title="Clean", repo_name="repo")

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    assert task_id[:8] in result.output
    line = next(ln for ln in result.output.splitlines() if task_id[:8] in ln)
    assert "rules 2/2" in line, line
    assert "merge: clean" in line, line
    assert "CONFLICT" not in line, line
    assert "1 task(s) merge-ready" in result.output
    assert "--yes to land them" in result.output


def test_ready_output_separates_rules_verdict_from_merge_verdict(tmp_path, monkeypatch):
    """A single --ready run with one clean task and one conflicting task
    must show both halves independently on each line, not fold conflicted
    tasks' merge status into their rules status or vice versa."""
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    clean_id, _, _ = _ready_task(db, tmp_path, title="Clean", repo_name="repo-clean")
    conflicted_id, _, _ = _ready_task(
        db, tmp_path, title="Conflicted", repo_name="repo-conflicted",
        repo_factory=_repo_with_conflicting_feature_branch)

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    clean_line = next(ln for ln in result.output.splitlines() if clean_id[:8] in ln)
    conflicted_line = next(
        ln for ln in result.output.splitlines() if conflicted_id[:8] in ln)

    assert "rules 2/2" in clean_line and "merge: clean" in clean_line
    assert "rules 2/2" in conflicted_line and "CONFLICT" in conflicted_line
    # Both pass the same quality rules — only the merge half differs.
    assert "1 task(s) ready to land" in result.output
    assert "1 task(s) pass the quality rules but do NOT merge" in result.output


def test_conflicted_task_is_visible_and_not_auto_resolved(tmp_path, monkeypatch):
    """`--ready --yes` must never call `land_task` for a conflicted task —
    no auto-rebase, no auto-resolve — and must print it as not landed
    rather than silently dropping it from the run."""
    db = tmp_path / "nh.db"
    calls = []

    def _fake(*, task_id, **kwargs):
        calls.append(task_id)
        return LandResult(ok=True, step="close_pr",
                           landed_sha="ab" * 20, message="landed by fake")

    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    task_id, _, _ = _ready_task(
        db, tmp_path, title="Conflicted", repo_name="repo",
        repo_factory=_repo_with_conflicting_feature_branch)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == []  # land_task never invoked for the conflicted task
    assert "not landed" in result.output
    assert task_id[:8] in result.output

    t, _ = _task_state(db, task_id)
    assert t.status is TaskStatus.AWAITING_APPROVAL


def test_remote_only_pr_branch_is_probed_and_reported_as_conflict(
        tmp_path, monkeypatch):
    """The PR branch exists ONLY as `origin/feature` (no local ref) — the
    routine shape for this repo's squash-from-worktree landing checkout.
    `_approve_find_ready` must probe landability using the resolved HEAD
    SHA, not the bare `pr_branch` name: passing the bare name makes
    `check_landability` -> `conflicting_paths` -> `refs_resolvable` fail to
    resolve `feature` at all (no `origin/` fallback there) and silently
    degrade to `state="unknown"` — which this command's fail-open contract
    then treats as landable, exactly reproducing the incident (a real
    conflict slips through `--ready` and `--yes` hands it to `land_task`,
    which fails at squash). With the fix, the CONFLICT is detected and
    reported instead of masked."""
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    task_id, _, _ = _ready_task(
        db, tmp_path, title="Remote Only", repo_name="repo",
        repo_factory=_repo_with_remote_only_conflicting_branch)

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    assert task_id[:8] in result.output
    line = next(ln for ln in result.output.splitlines() if task_id[:8] in ln)
    # Must be reported as an actual CONFLICT, never degrade to "unknown"
    # just because the branch has no local ref.
    assert "CONFLICT" in line, line
    assert "unknown" not in line, line
    assert "0 task(s) ready to land" in result.output
    assert "1 task(s) pass the quality rules but do NOT merge" in result.output

    # --yes must never hand this conflicted, remote-only branch to land_task.
    result_yes = _invoke(approve, db, ["--ready", "--yes"])
    assert result_yes.exit_code == 0, result_yes.output
    assert "not landed" in result_yes.output


def test_unknown_base_degrades_open_and_still_lands(tmp_path, monkeypatch):
    """When the base cannot be resolved at all (fail-open contract), the
    task is listed with an "unknown" merge marker, is NOT treated as a
    conflict, and --yes still lands it — a git failure must never turn a
    genuinely landable task into a refusal."""
    db = tmp_path / "nh.db"

    def _repo_with_no_base(tmp_path, name="repo"):
        repo, head_sha = _repo_with_feature_branch(tmp_path, name)
        _git(repo, "checkout", "feature")
        _git(repo, "branch", "-D", "main")
        return repo, head_sha

    calls = []

    def _fake(*, task_id, **kwargs):
        calls.append(task_id)
        return LandResult(ok=True, step="close_pr",
                           landed_sha="cd" * 20, message="landed by fake")

    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    task_id, _, _ = _ready_task(
        db, tmp_path, title="No Base", repo_name="repo",
        repo_factory=_repo_with_no_base)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == [task_id]
    assert "merged" in result.output

    t, _ = _task_state(db, task_id)
    assert t.status is TaskStatus.DONE


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

def _status_task(db, *, title, status_, commit_sha, mp_sha, ready):
    async def _go():
        async with Store(db) as store:
            t = Task.new(title, repo_path="/tmp/x")
            t.context = {
                "merge_policy": {
                    mp_sha: {
                        "ready": ready,
                        "policy_changed_in_diff": False,
                        "rules": [dict(r) for r in _RULES],
                    },
                },
            }
            await store.create_task(t)
            aid = await store.create_attempt(t.id, 1)
            await store.update_attempt(aid, commit_sha=commit_sha)
            event = ({"source": "test", "kind": "human_merged", "text": ""}
                      if status_ is TaskStatus.DONE else None)
            await store.set_status(t, status_, validate=False, event=event)
            return t.id
    return asyncio.run(_go())


def test_status_prints_merge_ready_count(tmp_path):
    db = tmp_path / "nh.db"

    sha_ready_awaiting = "a" * 40
    _status_task(db, title="Ready Awaiting", status_=TaskStatus.AWAITING_APPROVAL,
                 commit_sha=sha_ready_awaiting, mp_sha=sha_ready_awaiting, ready=True)

    sha_ready_done = "b" * 40
    _status_task(db, title="Ready But Done", status_=TaskStatus.DONE,
                 commit_sha=sha_ready_done, mp_sha=sha_ready_done, ready=True)

    sha_live = "c" * 40
    sha_stale_verdict = "d" * 40
    _status_task(db, title="Stale Verdict Awaiting", status_=TaskStatus.AWAITING_APPROVAL,
                 commit_sha=sha_live, mp_sha=sha_stale_verdict, ready=True)

    result = _invoke(status, db, [])

    assert result.exit_code == 0, result.output
    assert "merge-ready: 1" in result.output


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
    assert "rules 2/2" in line, line
