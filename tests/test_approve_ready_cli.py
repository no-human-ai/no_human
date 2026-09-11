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
    + `rev-parse` resolve to inside `_go_ready`/`_land_one`.

    No `origin` remote is configured — deliberately. `GitRepo.trunk_ref`'s
    ladder tries `origin/main` first and falls back to plain local `main`
    when no `origin` exists, so the staleness tests below exercise the real
    ladder's fallback rung without needing a second (bare) clone."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")
    _git(repo, "checkout", "main")
    head_sha = _git_out(repo, "rev-parse", "feature")
    return repo, head_sha


def _advance_trunk(repo):
    """Simulates trunk moving after the branch's green was measured — the
    PR #272 shape: a new commit lands on `main` (e.g. a new guard test) that
    the branch's already-recorded suite never ran against. `repo`'s
    worktree is left on `main`, matching `_repo_with_feature_branch`'s own
    ending state. Returns the new trunk tip sha."""
    _git(repo, "checkout", "main")
    (repo / "trunk_guard.txt").write_text("guard\n")
    _git(repo, "add", "trunk_guard.txt")
    _git(repo, "commit", "-m", "trunk: add a guard after the branch was cut")
    return _git_out(repo, "rev-parse", "main")


def _recut(repo):
    """Re-cuts `feature` onto the now-advanced `main` — what a human/agent
    re-running after a rebase would produce: the branch's new merge base
    with trunk is trunk's current tip. Returns the branch's new head sha.
    Leaves the worktree on `main`, matching the other helpers here."""
    _git(repo, "checkout", "feature")
    _git(repo, "rebase", "main")
    _git(repo, "checkout", "main")
    return _git_out(repo, "rev-parse", "feature")


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
                 rules=None, mp_base_sha=None, mp_tests_green=None):
    """An AWAITING_APPROVAL task with a real local feature branch, a
    `pr_watch`/`pr_branch` pair (all `resolve_task_pr` needs — no PR event
    log required), a `merge_policy` verdict, and a `review_history` round
    stamped on the branch head (all `_review_pass_evidence` needs). Returns
    (task_id, repo_path, head_sha).

    `mp_base_sha`/`mp_tests_green` are additive and OMITTED from the
    verdict dict entirely when left at their `None` default — every
    existing caller's verdict shape is byte-identical to before this
    parameter pair existed, so `stale_base_reason` (which requires both
    `tests_green` truthy and a non-empty `base_sha`) is a no-op for them."""
    repo, head_sha = _repo_with_feature_branch(tmp_path, repo_name)
    verdict_sha = mp_sha if mp_sha is not None else head_sha

    verdict = {
        "ready": mp_ready,
        "policy_changed_in_diff": mp_policy_changed,
        "rules": [dict(r) for r in (rules or _RULES)],
    }
    if mp_base_sha is not None:
        verdict["base_sha"] = mp_base_sha
    if mp_tests_green is not None:
        verdict["tests_green"] = mp_tests_green

    async def _go():
        async with Store(db) as store:
            t = Task.new(title, repo_path=str(repo))
            t.context = {
                "pr_watch": "https://example.invalid/pr/1",
                "pr_branch": "feature",
                "merge_policy": {verdict_sha: verdict},
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

def _status_task(db, *, title, status_, commit_sha, mp_sha, ready,
                  repo_path="/tmp/x", mp_base_sha=None, mp_tests_green=None):
    """`repo_path`/`mp_base_sha`/`mp_tests_green` are additive (default
    unchanged from before this parameter set existed): a caller that omits
    them gets the exact same fake `/tmp/x` task with no `base_sha`/
    `tests_green` keys in its verdict dict as always."""
    verdict = {
        "ready": ready,
        "policy_changed_in_diff": False,
        "rules": [dict(r) for r in _RULES],
    }
    if mp_base_sha is not None:
        verdict["base_sha"] = mp_base_sha
    if mp_tests_green is not None:
        verdict["tests_green"] = mp_tests_green

    async def _go():
        async with Store(db) as store:
            t = Task.new(title, repo_path=repo_path)
            t.context = {"merge_policy": {mp_sha: verdict}}
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


# --------------------------------------------------------------------------- #
# staleness: a green measured against a tree older than trunk's current tip  #
# (bug: merge-ready certifies a branch that is green alone and red merged —  #
# PR #272 shape, trunk added a guard after the branch's merge base) must not #
# be offered as merge-ready by either algorithm above, and must refuse to    #
# land with a reason naming the staleness — until the branch is re-cut onto  #
# trunk's new tip, at which point it is offered (and lands) again.           #
# --------------------------------------------------------------------------- #

def test_a_green_measured_before_trunk_moved_is_refused_then_offered_again_after_a_recut(
    tmp_path, monkeypatch,
):
    """AC1 + AC2, both directions, in one test: fresh green -> listed and
    landable; trunk moves -> excluded from the listing AND `approve <id>`
    refuses with a reason naming the staleness; re-cut onto the new trunk
    tip -> listed and landable again."""
    db = tmp_path / "nh.db"

    task_id, repo, head_sha = _ready_task(
        db, tmp_path, title="Task A", repo_name="repo-a", mp_tests_green=True)
    old_trunk_sha = _git_out(repo, "rev-parse", "main")

    async def _stamp_base():
        async with Store(db) as store:
            await store.merge_context(
                task_id, {"merge_policy": {head_sha: {"base_sha": old_trunk_sha}}})
    asyncio.run(_stamp_base())

    # --- fresh: green measured against trunk's CURRENT tip -> offered -------
    result = _invoke(approve, db, ["--ready"])
    assert result.exit_code == 0, result.output
    assert task_id[:8] in result.output
    assert "rules 2/2" in result.output

    # --- trunk moves: the same recorded green is now stale -------------------
    new_trunk_sha = _advance_trunk(repo)
    assert new_trunk_sha != old_trunk_sha

    result2 = _invoke(approve, db, ["--ready"])
    assert result2.exit_code == 0, result2.output
    # Reported, not silently dropped: the id appears on an "excluded" line,
    # never on a "ready"/"rules N/N" line.
    assert f"excluded {task_id[:8]}" in result2.output
    assert not any(
        task_id[:8] in ln and "rules" in ln
        for ln in result2.output.splitlines()
    )
    assert "no awaiting_approval task is merge-ready" in result2.output
    assert "excluded" in result2.output
    assert old_trunk_sha[:12] in result2.output
    assert new_trunk_sha[:12] in result2.output

    land_result = _invoke(approve, db, [task_id])
    assert land_result.exit_code == 1, land_result.output
    assert "cannot merge: preconditions" in land_result.output
    assert "trunk" in land_result.output
    assert old_trunk_sha[:12] in land_result.output
    assert new_trunk_sha[:12] in land_result.output

    t, _ = _task_state(db, task_id)
    assert t.status is TaskStatus.AWAITING_APPROVAL

    # --- re-cut onto the new trunk tip: offered, and lands, again -----------
    new_head_sha = _recut(repo)

    async def _restamp():
        async with Store(db) as store:
            await store.merge_context(task_id, {
                "merge_policy": {
                    new_head_sha: {
                        "ready": True,
                        "policy_changed_in_diff": False,
                        "rules": [dict(r) for r in _RULES],
                        "base_sha": new_trunk_sha,
                        "tests_green": True,
                    },
                },
                "pr_branch": "feature",
                "review_history": [{"sha": new_head_sha, "passed": True}],
            })
    asyncio.run(_restamp())

    result3 = _invoke(approve, db, ["--ready"])
    assert result3.exit_code == 0, result3.output
    assert task_id[:8] in result3.output
    assert "rules 2/2" in result3.output

    def _fake(*, task_id, **kwargs):
        return LandResult(ok=True, step="close_pr", landed_sha="ab" * 20,
                           message="landed by fake")
    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    land_result2 = _invoke(approve, db, [task_id])
    assert land_result2.exit_code == 0, land_result2.output
    assert "merged" in land_result2.output

    t2, _ = _task_state(db, task_id)
    assert t2.status is TaskStatus.DONE


def test_status_merge_ready_count_drops_when_trunk_moves_and_returns_after_a_recut(
    tmp_path,
):
    """AC1 + AC2 for the DB-only surface (`nh status`'s `merge-ready: N`,
    `api/models.py:merge_ready_for`) — distinct algorithm from `--ready`
    above (no git resolution of the branch; reads the latest attempt's
    `commit_sha` column directly), so it needs its own pin."""
    db = tmp_path / "nh.db"
    repo, head_sha = _repo_with_feature_branch(tmp_path, "repo")
    old_trunk_sha = _git_out(repo, "rev-parse", "main")

    task_id = _status_task(
        db, title="Task A", status_=TaskStatus.AWAITING_APPROVAL,
        commit_sha=head_sha, mp_sha=head_sha, ready=True,
        repo_path=str(repo), mp_base_sha=old_trunk_sha, mp_tests_green=True)

    result = _invoke(status, db, [])
    assert result.exit_code == 0, result.output
    assert "merge-ready: 1" in result.output

    from no_human.api.models import merge_ready_for
    t, _ = _task_state(db, task_id)
    assert merge_ready_for(
        t, [{"commit_sha": head_sha}], trunk_sha=old_trunk_sha) is True

    new_trunk_sha = _advance_trunk(repo)
    assert new_trunk_sha != old_trunk_sha

    result2 = _invoke(status, db, [])
    assert result2.exit_code == 0, result2.output
    assert "merge-ready: 0" in result2.output
    assert merge_ready_for(
        t, [{"commit_sha": head_sha}], trunk_sha=new_trunk_sha) is False

    new_head_sha = _recut(repo)

    async def _restamp():
        async with Store(db) as store:
            await store.merge_context(task_id, {
                "merge_policy": {
                    new_head_sha: {
                        "ready": True,
                        "policy_changed_in_diff": False,
                        "rules": [dict(r) for r in _RULES],
                        "base_sha": new_trunk_sha,
                        "tests_green": True,
                    },
                },
            })
            aid = await store.create_attempt(task_id, 2)
            await store.update_attempt(aid, commit_sha=new_head_sha)
    asyncio.run(_restamp())

    result3 = _invoke(status, db, [])
    assert result3.exit_code == 0, result3.output
    assert "merge-ready: 1" in result3.output


def test_a_task_with_no_recorded_test_result_is_unchanged_when_trunk_moves(
    tmp_path, monkeypatch,
):
    """AC4: a task whose verdict carries no `tests_green` fact at all (the
    default `_RULES`-only shape every pre-existing test in this file uses)
    must behave IDENTICALLY before and after trunk moves — listed, and
    landable, both times. Positive control in the same test: a sibling task
    with `mp_tests_green=True` and a pre-move `base_sha` IS dropped by the
    same trunk move, proving the move actually happened and this task's
    stability isn't just "nothing was ever offered"."""
    db = tmp_path / "nh.db"

    no_result_id, repo_a, _ = _ready_task(
        db, tmp_path, title="No Test Result", repo_name="repo-a")

    green_id, repo_b, green_head_sha = _ready_task(
        db, tmp_path, title="Green Sibling", repo_name="repo-b",
        mp_tests_green=True)
    old_trunk_b = _git_out(repo_b, "rev-parse", "main")

    async def _stamp_base():
        async with Store(db) as store:
            await store.merge_context(
                green_id, {"merge_policy": {green_head_sha: {"base_sha": old_trunk_b}}})
    asyncio.run(_stamp_base())

    result = _invoke(approve, db, ["--ready"])
    assert result.exit_code == 0, result.output
    assert no_result_id[:8] in result.output
    before = result.output

    _advance_trunk(repo_a)
    new_trunk_b = _advance_trunk(repo_b)
    assert new_trunk_b != old_trunk_b

    result2 = _invoke(approve, db, ["--ready"])
    assert result2.exit_code == 0, result2.output
    # Unchanged: still listed, exactly like `before`.
    assert no_result_id[:8] in result2.output
    # Positive control: the green sibling IS excluded now — reported on an
    # "excluded" line, never on a "ready"/"rules N/N" line.
    assert f"excluded {green_id[:8]}" in result2.output
    assert not any(
        green_id[:8] in ln and "rules" in ln
        for ln in result2.output.splitlines()
    )

    # Unchanged landing behavior too: this task's absent `tests_green` fact
    # never triggers the staleness refusal, trunk move or not — it lands
    # exactly as it would have before this feature existed.
    def _fake(*, task_id, **kwargs):
        return LandResult(ok=True, step="close_pr", landed_sha="cd" * 20,
                           message="landed by fake")
    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    land_result = _invoke(approve, db, [no_result_id])
    assert land_result.exit_code == 0, land_result.output
    assert "merged" in land_result.output

    t, _ = _task_state(db, no_result_id)
    assert t.status is TaskStatus.DONE


def test_a_task_whose_recorded_tests_failed_is_unchanged_when_trunk_moves(
    tmp_path, monkeypatch,
):
    """AC4: a task whose verdict explicitly recorded `tests_green: False`
    (a failing run) must never have been offered, and must stay never-offered
    across a trunk move — `stale_base_reason` only ever acts on a `True`
    `tests_green`. Positive control: the same green sibling shape as above
    IS dropped by the identical trunk move."""
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    failing_id, repo_a, failing_head_sha = _ready_task(
        db, tmp_path, title="Failing Tests", repo_name="repo-a",
        mp_ready=False, mp_tests_green=False)
    old_trunk_a = _git_out(repo_a, "rev-parse", "main")

    green_id, repo_b, green_head_sha = _ready_task(
        db, tmp_path, title="Green Sibling", repo_name="repo-b",
        mp_tests_green=True)
    old_trunk_b = _git_out(repo_b, "rev-parse", "main")

    async def _stamp_bases():
        async with Store(db) as store:
            # `base_sha` present but `tests_green: False` must still never
            # flag — `stale_base_reason` only ever acts on a truthy
            # `tests_green`.
            await store.merge_context(
                failing_id, {"merge_policy": {failing_head_sha: {"base_sha": old_trunk_a}}})
            await store.merge_context(
                green_id, {"merge_policy": {green_head_sha: {"base_sha": old_trunk_b}}})
    asyncio.run(_stamp_bases())

    before = _invoke(approve, db, ["--ready"]).output
    assert failing_id[:8] not in before
    # Positive control: the green sibling IS listed before the move.
    assert green_id[:8] in before

    _advance_trunk(repo_a)
    _advance_trunk(repo_b)

    after = _invoke(approve, db, ["--ready"]).output
    # Unchanged: still never offered, exactly as before the move.
    assert failing_id[:8] not in after
    # Positive control: the green sibling IS dropped by the same move.
    assert green_id[:8] not in after


def test_the_staleness_check_costs_no_test_run_and_one_local_ref_read_per_repo(
    tmp_path, monkeypatch,
):
    """AC3: the staleness check is a comparison of recorded facts against
    ONE local git read per repo — never a test run, never a second network
    call. Two halves: (1) `stale_base_reason` itself is pure — it answers
    correctly with all of `subprocess.run` blown up; (2) the bounded-cost
    claim — `--ready` on N candidates in the SAME repo calls
    `GitRepo.trunk_tip_sha` a bounded number of times and `GitRepo.fetch`
    exactly once per repo (the pre-existing call, no new network),  and
    never runs a test suite or `land_task`."""
    from no_human.core import merge_policy
    from no_human.vcs.git import GitRepo

    # --- (1) pure half: no I/O at all ----------------------------------------
    def _boom(*a, **k):
        raise AssertionError("stale_base_reason must not shell out")
    with monkeypatch.context() as mp:
        mp.setattr(subprocess, "run", _boom)
        assert merge_policy.stale_base_reason(
            {"tests_green": True, "base_sha": "a" * 40}, "b" * 40) is not None
        assert merge_policy.stale_base_reason(
            {"tests_green": True, "base_sha": "a" * 40}, "a" * 40) is None

    # --- (2) bounded-cost half: real repo, real CLI invocation ---------------
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    async def _stamp(task_id, sha, base):
        async with Store(db) as store:
            await store.merge_context(task_id, {"merge_policy": {sha: {"base_sha": base}}})

    ids = []
    for i in range(3):
        tid, repo, head_sha = _ready_task(
            db, tmp_path, title=f"Task {i}", repo_name=f"repo-{i}", mp_tests_green=True)
        asyncio.run(_stamp(tid, head_sha, _git_out(repo, "rev-parse", "main")))
        ids.append(tid)

    trunk_calls = {"n": 0}
    orig_trunk_tip = GitRepo.trunk_tip_sha

    def _counting_trunk_tip(self, *a, **k):
        trunk_calls["n"] += 1
        return orig_trunk_tip(self, *a, **k)
    monkeypatch.setattr(GitRepo, "trunk_tip_sha", _counting_trunk_tip)

    fetch_calls = {"n": 0}
    orig_fetch = GitRepo.fetch

    def _counting_fetch(self, *a, **k):
        fetch_calls["n"] += 1
        return orig_fetch(self, *a, **k)
    monkeypatch.setattr(GitRepo, "fetch", _counting_fetch)

    orig_run = subprocess.run

    def _guarded_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and "merge-base" in cmd and "--is-ancestor" in cmd:
            raise AssertionError("must never run merge-base --is-ancestor")
        if isinstance(cmd, (list, tuple)) and any("pytest" in str(c) for c in cmd):
            raise AssertionError("must never invoke pytest")
        return orig_run(cmd, *a, **k)
    monkeypatch.setattr(subprocess, "run", _guarded_run)

    result = _invoke(approve, db, ["--ready"])
    assert result.exit_code == 0, result.output
    for tid in ids:
        assert tid[:8] in result.output

    # One `fetch()` per repo — the pre-existing call already at the top of
    # `_approve_find_ready`, no new network call added by this feature.
    assert fetch_calls["n"] == 3, fetch_calls
    # Bounded: at most one local trunk-tip read per candidate.
    assert trunk_calls["n"] <= 3, trunk_calls
