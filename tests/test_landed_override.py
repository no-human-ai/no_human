"""The human landed-override: `nh approve --landed <sha> --because ...`
and its API sibling `POST /api/tasks/{id}/approve-landed`.

The narrow class this exists for: a supervising session's squash train lands
a task's content, but automated containment (`vcs.pr_watcher`) honestly
refuses — a later train car's classification-decision edits, or a real
union-resolved source conflict, leave no candidate commit whose tree matches
the branch verbatim. `blockers/landed_override.py` is the shared, git-free
decision; this file drives it against REAL temp git repos (subprocess, in
the style of tests/test_pr_shipped.py) so ancestry and residue are genuine,
never mocked.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.blockers.landed_override import (
    LANDED_OVERRIDE_KIND, OverrideRefused, approve_landed_override,
)
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs.task_pr import DONE_EVIDENCE_KINDS

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# git plumbing — real temp repos, no mocking of ancestry                      #
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


def _repo_with_residue(tmp_path, name="repo"):
    """`sha` (main's tip) is a real ancestor of main, but `feature` still has
    a file (`b.txt`) that never landed there — containment must report it as
    residue, and the human is overriding that honest refusal."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature: add b.txt")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("unrelated change\n")
    _git(repo, "commit", "-am", "unrelated: change a.txt")
    return repo


def _repo_with_squash_landed(tmp_path, name="repo"):
    """The `failed_pre_pr` shape's happy path: `feature`'s content is
    hand-landed onto `main` as a SEPARATE commit (not a merge, not a
    fast-forward — the same shape a supervising session's squash train, or a
    human `git cherry-pick`, produces) whose tree is content-equivalent to
    `feature`. Returns ``(repo, feature_sha, landed_sha)``."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature: add b.txt")
    feature_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "hand-landed: add b.txt")
    landed_sha = _git_out(repo, "rev-parse", "main")
    return repo, feature_sha, landed_sha


def _declare_default(repo, branch="main"):
    """Wires up ``origin/HEAD`` -> ``origin/<branch>`` in a LOCAL-only repo
    (no real remote) — exactly enough for
    ``pr_watcher.resolve_project_default_branch``'s
    ``symbolic-ref -q refs/remotes/origin/HEAD`` probe to resolve, without
    needing a second real remote repo. Mirrors what `_base_tips` elsewhere
    in this suite already relies on for ``@{upstream}``."""
    sha = _git_out(repo, "rev-parse", branch)
    _git(repo, "update-ref", f"refs/remotes/origin/{branch}", sha)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD",
         f"refs/remotes/origin/{branch}")


def _repo_stacked_base(tmp_path, name="repo"):
    """A task's recorded ``base_branch`` names another task's STACKED branch
    (``no-human/parent-3``) — not the repo's real default — while the
    content actually lands on ``main``. ``main`` and the stacked branch
    diverge BEFORE the landing commit, so the landing commit is an ancestor
    of ``main`` only. Declares ``main`` as the repo's default via
    ``_declare_default``. Returns ``(repo, landed_sha)``."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "no-human/parent-3")
    (repo / "stacked.txt").write_text("stacked base work\n")
    _git(repo, "add", "stacked.txt")
    _git(repo, "commit", "-m", "parent task: stacked work")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("actual landed change\n")
    _git(repo, "commit", "-am", "landed: the real fix")
    landed_sha = _git_out(repo, "rev-parse", "main")
    _declare_default(repo, "main")
    return repo, landed_sha


async def _seed(store, repo_path, *, base_branch="main", pr_branch="feature",
                status=TaskStatus.AWAITING_APPROVAL) -> Task:
    t = Task.new("landed-override-check", repo_path=str(repo_path))
    t.context = {"base_branch": base_branch, "pr_branch": pr_branch}
    await store.create_task(t)
    if status is TaskStatus.DONE:
        await store.set_status(t, status, validate=False,
                               event={"source": "test", "kind": "test_seed"})
    elif status is not TaskStatus.PENDING:
        await store.set_status(t, status, validate=False)
    return t


async def _seed_failed_pre_pr(
    store, repo_path, *, branch="feature", commit_sha="",
    base_branch="main", pr_evidence=None, cancel_reason=None,
) -> Task:
    """The 5b2246c1 shape: FAILED, pre-PR (no `pr_branch` ever written to
    context — content is only locatable via attempt metadata), a
    BUDGET_EXHAUSTED blocker, and no PR event, unless `pr_evidence` is
    given."""
    t = Task.new("landed-override-check", repo_path=str(repo_path))
    t.context = {"base_branch": base_branch}
    if pr_evidence:
        t.context["pr_watch"] = pr_evidence
    if cancel_reason:
        t.context["cancel_reason"] = cancel_reason
    t.blocker = {"category": "BUDGET_EXHAUSTED",
                 "message": "8.29M/8M lifetime token cap"}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        attempt_id, branch_name=branch, commit_sha=commit_sha,
        status="failed", failure_reason="BUDGET_EXHAUSTED")
    await store.set_status(t, TaskStatus.FAILED, validate=False)
    return t


async def _seed_done_with_evidence(store, repo_path, kind) -> Task:
    """A DONE task that already carries *kind* (a member of
    ``DONE_EVIDENCE_KINDS``) on its event log — the ``done_no_evidence``
    shape must refuse these, since re-asserting a landing over evidence that
    already stands is exactly what the shape must never allow."""
    t = await _seed(store, repo_path, status=TaskStatus.DONE)
    await store.save_events(
        t.id, [{"source": "test", "kind": kind, "ts": time.time()}])
    return t


# --------------------------------------------------------------------------- #
# approve_landed_override — the core, direct                                  #
# --------------------------------------------------------------------------- #

async def test_override_completes_task_and_records_audit_event(tmp_path, store):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, pr_branch="")

    result = await approve_landed_override(
        store, t, sha, "supervisor squash train 15 — verified by eyeball diff")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE

    events = await store.list_events(t.id)
    override_events = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]
    assert len(override_events) == 1
    ev = override_events[0]
    assert ev["sha"] == sha
    assert ev["justification"] == "supervisor squash train 15 — verified by eyeball diff"
    assert "residue" in ev
    assert "HUMAN OVERRIDE" in ev["text"]
    assert "not a containment pass" in ev["text"]
    assert "shipped" not in ev["text"].lower()
    assert result["sha"] == sha


async def test_refuses_sha_not_on_default_branch(tmp_path, store):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "a.txt").write_text("side change\n")
    _git(repo, "commit", "-am", "side: never merged")
    side_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    t = await _seed(store, repo)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, side_sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert (await store.list_events(t.id)) == []


async def test_refuses_empty_justification(tmp_path, store):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, sha, "   ")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert fresh.context.get("landed_override_sha") is None
    assert (await store.list_events(t.id)) == []


@pytest.mark.parametrize(
    "status", [TaskStatus.IMPLEMENTING, TaskStatus.ESCALATED])
async def test_refuses_task_not_awaiting_approval(tmp_path, store, status):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, status=status)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is status


async def test_override_touches_no_git_state(tmp_path, store, monkeypatch):
    import no_human.vcs.approve_merge as approve_merge_mod
    import no_human.vcs.git as git_mod

    def _boom(*a, **kw):
        raise AssertionError("landed override must never touch git-mutating code")

    monkeypatch.setattr(approve_merge_mod, "land_task", _boom)
    monkeypatch.setattr(git_mod, "GitRepo", _boom)

    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    before_tip = _git_out(repo, "rev-parse", "main")
    before_status = _git_out(repo, "status", "--porcelain")
    t = await _seed(store, repo, pr_branch="")

    await approve_landed_override(store, t, sha, "no git mutation happens here")

    after_tip = _git_out(repo, "rev-parse", "main")
    after_status = _git_out(repo, "status", "--porcelain")
    assert after_tip == before_tip
    assert after_status == before_status
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE

    # same poisoned monkeypatches, the failed-pre-PR shape
    repo2, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path, name="repo2")
    before_tip2 = _git_out(repo2, "rev-parse", "main")
    before_status2 = _git_out(repo2, "status", "--porcelain")
    t2 = await _seed_failed_pre_pr(
        store, repo2, branch="feature", commit_sha=feature_sha)

    await approve_landed_override(
        store, t2, landed_sha, "no git mutation happens here either")

    after_tip2 = _git_out(repo2, "rev-parse", "main")
    after_status2 = _git_out(repo2, "status", "--porcelain")
    assert after_tip2 == before_tip2
    assert after_status2 == before_status2
    fresh2 = await store.get_task(t2.id)
    assert fresh2.status is TaskStatus.DONE


async def test_residue_recorded_when_containment_refuses(tmp_path, store):
    repo = _repo_with_residue(tmp_path)
    sha = _git_out(repo, "rev-parse", "main")
    t = await _seed(store, repo, pr_branch="feature")

    result = await approve_landed_override(
        store, t, sha, "landed elsewhere; feature's extra file is intentional debt")

    assert result["residue"] == ["b.txt"]
    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["residue"] == ["b.txt"]
    assert "b.txt" in ev["text"]


# --------------------------------------------------------------------------- #
# the "failed_pre_pr" shape — budget-failed-no-PR, live incident 5b2246c1     #
# --------------------------------------------------------------------------- #

async def test_failed_pre_pr_task_with_landed_content_completes(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha)

    result = await approve_landed_override(
        store, t, landed_sha, "hand-landed by operator")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE

    events = await store.list_events(t.id)
    override_events = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]
    assert len(override_events) == 1
    ev = override_events[0]
    assert ev["sha"] == landed_sha
    assert ev["justification"] == "hand-landed by operator"
    assert ev["shape"] == "failed_pre_pr"
    assert ev["prior_status"] == "failed"
    assert "HUMAN OVERRIDE" in ev["text"]

    assert fresh.context.get("landed_override_sha") == landed_sha
    assert "landed_sha" not in fresh.context
    assert result["sha"] == landed_sha
    assert result["shape"] == "failed_pre_pr"
    assert result["prior_status"] == "failed"


async def test_failed_pre_pr_records_content_equivalence(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha)

    result = await approve_landed_override(
        store, t, landed_sha, "content matches byte for byte")

    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["equivalence"] == "content_equivalent"
    assert ev["residue"] == []
    assert result["residue"] == []


async def test_failed_pre_pr_with_residue_completes_on_assertion(tmp_path, store):
    repo = _repo_with_residue(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_failed_pre_pr(store, repo, branch="feature", commit_sha="")

    result = await approve_landed_override(
        store, t, main_sha,
        "landed elsewhere; feature's extra file is intentional debt")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["equivalence"] == "asserted_with_residue"
    assert ev["residue"] == ["b.txt"]
    assert result["residue"] == ["b.txt"]


async def test_failed_pre_pr_refuses_sha_not_on_base(tmp_path, store):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "a.txt").write_text("side change\n")
    _git(repo, "commit", "-am", "side: never merged")
    side_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    t = await _seed_failed_pre_pr(store, repo, branch="side", commit_sha=side_sha)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, side_sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert (await store.list_events(t.id)) == []


async def test_failed_task_with_pr_evidence_is_refused(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha,
        pr_evidence="https://example.com/pull/1")

    with pytest.raises(OverrideRefused, match="restore-approval"):
        await approve_landed_override(store, t, landed_sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert (await store.list_events(t.id)) == []


async def test_failed_cancelled_task_is_refused(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha,
        cancel_reason="operator")

    with pytest.raises(OverrideRefused, match="cancelled"):
        await approve_landed_override(store, t, landed_sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert (await store.list_events(t.id)) == []


async def test_failed_pre_pr_requires_justification(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, landed_sha, "   ")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert fresh.context.get("landed_override_sha") is None
    assert (await store.list_events(t.id)) == []


# --------------------------------------------------------------------------- #
# the "pending_never_ran" shape — hand-landed before any coder attempt ran,   #
# live incident 855f1263                                                     #
# --------------------------------------------------------------------------- #

async def _seed_pending(store, repo_path, *, pr_evidence=None) -> Task:
    """A task no coder attempt ever dispatched for: PENDING, empty context —
    no `base_branch`, since the dispatch-time write that would have recorded
    one never ran."""
    t = Task.new("landed-override-check", repo_path=str(repo_path))
    t.context = {}
    if pr_evidence:
        t.context["pr_watch"] = pr_evidence
    await store.create_task(t)
    return t


async def test_pending_task_completes_with_explicit_base(tmp_path, store):
    """F1 fix: the only way a pending task's missing base is ever filled in
    is an explicit human `base=` — never a guess."""
    repo = _make_repo(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_pending(store, repo)

    result = await approve_landed_override(
        store, t, main_sha, "hand-landed by the supervising session",
        base="main")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert fresh.context["base_branch"] == "main"

    events = await store.list_events(t.id)
    override_events = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]
    assert len(override_events) == 1
    ev = override_events[0]
    assert ev["shape"] == "pending_never_ran"
    assert ev["base"] == "main"
    assert ev["base_source"] == "human_asserted"
    assert ev["prior_status"] == "pending"
    assert "prior status: pending" in ev["text"]
    assert result["shape"] == "pending_never_ran"
    assert result["prior_status"] == "pending"


async def test_pending_task_refused_when_sha_not_on_default_branch(
    tmp_path, store,
):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "a.txt").write_text("side change\n")
    _git(repo, "commit", "-am", "side: never merged")
    side_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    t = await _seed_pending(store, repo)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(
            store, t, side_sha, "asserting anyway", base="main")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PENDING
    assert (await store.list_events(t.id)) == []


async def test_pending_task_refused_without_justification(tmp_path, store):
    repo = _make_repo(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_pending(store, repo)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(
            store, t, main_sha, "   ", base="main")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PENDING
    assert fresh.context.get("landed_override_sha") is None
    assert (await store.list_events(t.id)) == []


async def test_pending_task_with_pr_evidence_is_refused(tmp_path, store):
    repo = _make_repo(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_pending(
        store, repo, pr_evidence="https://example.com/pull/1")

    with pytest.raises(OverrideRefused, match="restore-approval"):
        await approve_landed_override(store, t, main_sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PENDING
    assert (await store.list_events(t.id)) == []


async def test_pending_task_refuses_without_explicit_base(tmp_path, store):
    """F1: no recorded base_branch and no `base=` — refuses. This is the
    replacement for the old (flattering-only, F3-flagged)
    `test_pending_base_defaults_from_the_repo_default_branch`: silently
    trusting `resolve_default_branch`'s checkout-branch fallback as a VALUE
    is exactly the false-completion risk that got this fix sent back."""
    repo = _make_repo(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_pending(store, repo)

    with pytest.raises(OverrideRefused, match="--base"):
        await approve_landed_override(
            store, t, main_sha, "hand-landed by the supervising session")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PENDING
    assert fresh.context.get("landed_override_sha") is None
    assert (await store.list_events(t.id)) == []


async def test_pending_task_refuses_without_explicit_base_on_a_side_branch_checkout(
    tmp_path, store,
):
    """F3's dangerous configuration, previously untested: the repo's CURRENT
    checkout is a side branch that was never merged anywhere and no
    `origin/HEAD` is configured — the exact shape of every onboarded profile
    on this machine (empty `ProjectProfile.default_branch`, no
    `git remote set-head` caller anywhere in the product). Must refuse, not
    silently complete against "side"."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "a.txt").write_text("side change\n")
    _git(repo, "commit", "-am", "side: never merged")
    t = await _seed_pending(store, repo)
    main_sha = _git_out(repo, "rev-parse", "main")

    with pytest.raises(OverrideRefused, match="--base"):
        await approve_landed_override(
            store, t, main_sha, "hand-landed by the supervising session")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PENDING
    assert (await store.list_events(t.id)) == []


async def test_pending_task_refusal_hint_names_the_checkout_branch(
    tmp_path, store,
):
    """The refusal message may carry a non-binding hint (`_base_hint`) — but
    it is text, never a value the call trusts."""
    repo = _make_repo(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_pending(store, repo)

    with pytest.raises(OverrideRefused) as exc_info:
        await approve_landed_override(
            store, t, main_sha, "hand-landed by the supervising session")

    assert "'main'" in exc_info.value.reason
    assert "--base" in exc_info.value.reason


async def test_pending_task_with_cancel_request_is_refused(tmp_path, store):
    """A cancel racing a hand-land must not be silently dropped: the DB's
    `cancel_requested` column (set by `nh task cancel` before a live task's
    status flip lands) gates the pending shape exactly like `cancel_reason`
    gates the failed shape."""
    repo = _make_repo(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_pending(store, repo)
    await store.request_cancel(t.id, "no longer needed")

    with pytest.raises(OverrideRefused, match="cancellation request"):
        await approve_landed_override(
            store, t, main_sha, "hand-landed by the supervising session",
            base="main")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PENDING
    assert (await store.list_events(t.id)) == []


# --------------------------------------------------------------------------- #
# API: POST /api/tasks/{id}/approve-landed                                    #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


async def test_api_approve_landed_endpoint(tmp_path, store, client):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, pr_branch="")

    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": sha, "justification": "supervisor squash train 15"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["sha"] == sha

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    assert any(e["kind"] == LANDED_OVERRIDE_KIND for e in events)


async def test_api_approve_landed_refuses(tmp_path, store, client):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")

    # 400 on empty justification
    t1 = await _seed(store, repo, pr_branch="")
    r = await client.post(
        f"/api/tasks/{t1.id}/approve-landed",
        json={"sha": sha, "justification": "   "},
    )
    assert r.status_code == 400, r.text

    # 409 on a non-awaiting task
    t2 = await _seed(store, repo, pr_branch="", status=TaskStatus.IMPLEMENTING)
    r = await client.post(
        f"/api/tasks/{t2.id}/approve-landed",
        json={"sha": sha, "justification": "asserting anyway"},
    )
    assert r.status_code == 409, r.text

    # a repeated call: the task is now DONE, which the pre-check accepts
    # (DONE is itself an eligible shape, `done_no_evidence`) — but the first
    # call's own `approved_landed_override` event is standing evidence, so
    # the resolver's own refusal fires and it's a 400, not a 409.
    t3 = await _seed(store, repo, pr_branch="")
    r = await client.post(
        f"/api/tasks/{t3.id}/approve-landed",
        json={"sha": sha, "justification": "first call"},
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        f"/api/tasks/{t3.id}/approve-landed",
        json={"sha": sha, "justification": "second call"},
    )
    assert r.status_code == 400, r.text


async def test_api_approve_landed_completes_failed_pre_pr(tmp_path, store, client):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha)

    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": landed_sha, "justification": "hand-landed by operator"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["sha"] == landed_sha

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    assert any(e["kind"] == LANDED_OVERRIDE_KIND for e in events)

    # replay cannot append a duplicate override event — same mechanism as
    # test_api_approve_landed_refuses's repeated-call case: the task is now
    # DONE (an eligible pre-check shape), and the resolver's own
    # standing-evidence refusal fires, so this is a 400, not a 409.
    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": landed_sha, "justification": "replay"},
    )
    assert r.status_code == 400, r.text


async def test_approve_landed_endpoint_accepts_a_pending_task(
    tmp_path, store, client,
):
    repo = _make_repo(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_pending(store, repo)

    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": main_sha,
              "justification": "hand-landed by the supervising session",
              "base": "main"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["sha"] == main_sha

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["shape"] == "pending_never_ran"
    assert ev["base_source"] == "human_asserted"


async def test_approve_landed_endpoint_refuses_a_pending_task_without_base(
    tmp_path, store, client,
):
    """API twin of F1: the CLI and API must not drift — neither guesses a
    base for a pending task that never recorded one."""
    repo = _make_repo(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_pending(store, repo)

    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": main_sha,
              "justification": "hand-landed by the supervising session"},
    )
    assert r.status_code == 400, r.text
    assert "--base" in r.json()["detail"]

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PENDING


async def test_approve_landed_endpoint_accepts_a_done_no_evidence_task(
    tmp_path, store, client,
):
    """API twin of the `done_no_evidence` shape (BLOCKING 1, independent
    review of nh67): the CLI and API must not drift — the endpoint's
    pre-check used to hard-code `(AWAITING_APPROVAL, FAILED, PENDING)`,
    refusing a DONE task the CLI-side resolver correctly accepts, directly
    contradicting this module's own "cannot drift" docstring claim."""
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, status=TaskStatus.DONE, pr_branch="")

    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": sha,
              "justification": "repairing a pre-mechanism hand-landing"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["sha"] == sha

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["shape"] == "done_no_evidence"
    assert ev["prior_status"] == "done"


# --------------------------------------------------------------------------- #
# ts must be the shared REAL clock, not an ISO string (SQLite orders TEXT     #
# above REAL, so an ISO ts sorts above every real event forever)              #
# --------------------------------------------------------------------------- #

async def test_override_event_ts_is_real_and_orders_before_later_event(
    tmp_path, store,
):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, pr_branch="")

    result = await approve_landed_override(
        store, t, sha, "supervisor squash train 15 — verified by eyeball diff")

    rows = await store.query(
        "SELECT typeof(ts) AS t, ts FROM task_events "
        "WHERE json_extract(data,'$.kind')=?",
        (LANDED_OVERRIDE_KIND,),
    )
    assert len(rows) == 1
    assert rows[0]["t"] == "real"

    # a normal event emitted afterwards must sort AFTER the override, not
    # before it — the bug made every override look newer than everything.
    await store.save_events(
        t.id, [{"kind": "note", "source": "test", "ts": time.time()}])

    ordered = await store.query(
        "SELECT json_extract(data,'$.kind') AS k FROM task_events "
        "ORDER BY ts ASC, id ASC",
    )
    kinds = [r["k"] for r in ordered]
    assert kinds.index(LANDED_OVERRIDE_KIND) < kinds.index("note")

    fresh = await store.get_task(t.id)
    approved_at = fresh.context["approved_at"]
    # still ISO, still parseable — the drawer and web/e2e/drawer.mjs depend
    # on this shape; only the event-level ts column changed type.
    datetime.fromisoformat(approved_at)
    assert result["sha"] == sha


# --------------------------------------------------------------------------- #
# recorded base_branch pointing at another task's STACKED branch, while the   #
# content actually lands on the repo's real default branch                    #
# --------------------------------------------------------------------------- #

async def test_landed_sha_on_default_branch_accepted_when_base_is_stacked(
    tmp_path, store,
):
    """AC1: a task's recorded base_branch names another task's stacked
    branch, not the repo's real default. The content genuinely lands on the
    default branch — this must now be ACCEPTED, and the confirmation must
    name which branch matched."""
    repo, landed_sha = _repo_stacked_base(tmp_path)
    t = await _seed(store, repo, base_branch="no-human/parent-3", pr_branch="")

    result = await approve_landed_override(
        store, t, landed_sha, "landed on main; base_branch recorded the "
        "parent task's stacked branch by dispatch-time accident")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert result["matched_branch"] == "main"
    assert result["base_source"] == "default_branch"
    assert "main" in result["text"]

    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["base"] == "main"
    assert ev["base_source"] == "default_branch"
    assert ev["matched_branch"] == "main"

    # the original recorded base is NEVER overwritten by a resolved default
    assert fresh.context["base_branch"] == "no-human/parent-3"
    assert fresh.context["landed_override_base"] == "main"
    assert fresh.context["landed_override_base_source"] == "default_branch"


async def test_landed_sha_on_default_branch_accepted_when_local_main_is_absent(
    tmp_path, store,
):
    """AC1 edge case: the repo's default is declared via `origin/HEAD` ->
    `refs/remotes/origin/main`, but the LOCAL `main` ref itself is gone —
    exactly the shape a long-lived checkout is left in once a task's content
    is squash-landed and pushed from a throwaway worktree that advances
    `refs/remotes/origin/main` while this checkout never fetches a local
    `main` of its own. `_base_tips` resolves a BARE branch's
    remote-tracking counterpart only through `<bare>@{upstream}`, which is
    undefined once no local branch of that name exists — so the default
    candidate must be stored as `origin/main`, not bare `main`, or the
    ancestry check has nothing left to resolve and a genuinely landed commit
    is refused."""
    repo, landed_sha = _repo_stacked_base(tmp_path)
    _git(repo, "checkout", "no-human/parent-3")
    _git(repo, "branch", "-D", "main")

    t = await _seed(store, repo, base_branch="no-human/parent-3", pr_branch="")

    result = await approve_landed_override(
        store, t, landed_sha, "landed on main; local main was never "
        "fetched into this checkout")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert result["matched_branch"] == "origin/main"
    assert result["base_source"] == "default_branch"

    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["matched_branch"] == "origin/main"
    assert ev["base_source"] == "default_branch"

    assert fresh.context["base_branch"] == "no-human/parent-3"
    assert fresh.context["landed_override_base"] == "origin/main"
    assert fresh.context["landed_override_base_source"] == "default_branch"


async def test_landed_sha_on_recorded_base_still_accepted_and_names_it(
    tmp_path, store,
):
    """AC2 (regression): when the recorded base_branch IS the repo's real
    default, the two candidates collapse to one and the match is still
    reported as `"recorded"` — the priority rule only matters when the two
    disagree."""
    repo = _make_repo(tmp_path)
    _declare_default(repo, "main")
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, base_branch="main", pr_branch="")

    result = await approve_landed_override(
        store, t, sha, "landed exactly where recorded")

    assert result["matched_branch"] == "main"
    assert result["base_source"] == "recorded"

    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["base_source"] == "recorded"
    assert ev["matched_branch"] == "main"


async def test_refusal_names_both_branches_and_tips(tmp_path, store):
    """AC3: a sha that is an ancestor of NEITHER the recorded base nor the
    repo's default must still be refused, and the refusal must name both
    branches and both tips — not just the one that used to be checked."""
    repo, _landed_sha = _repo_stacked_base(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "side.txt").write_text("never merged anywhere\n")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-m", "side: never merged")
    side_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    main_tip = _git_out(repo, "rev-parse", "--short=12", "main")
    parent_tip = _git_out(repo, "rev-parse", "--short=12", "no-human/parent-3")

    t = await _seed(store, repo, base_branch="no-human/parent-3", pr_branch="")

    with pytest.raises(OverrideRefused) as exc_info:
        await approve_landed_override(store, t, side_sha, "asserting anyway")

    reason = str(exc_info.value)
    assert "main" in reason
    assert "no-human/parent-3" in reason
    assert main_tip in reason
    assert parent_tip in reason

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert (await store.list_events(t.id)) == []


async def test_explicit_base_narrows_check_on_task_with_recorded_base(
    tmp_path, store,
):
    """AC4: `--base` must be HONOURED — as a narrowing assertion — even on a
    task that already has a recorded base_branch. The content here lands on
    neither the recorded base nor the repo's default, only on a third branch
    the human explicitly names."""
    repo = _make_repo(tmp_path)
    _declare_default(repo, "main")
    _git(repo, "checkout", "-b", "release/9")
    (repo / "a.txt").write_text("release-only change\n")
    _git(repo, "commit", "-am", "release/9: hotfix")
    release_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")

    t = await _seed(store, repo, base_branch="main", pr_branch="")

    result = await approve_landed_override(
        store, t, release_sha, "landed on the release branch, not main",
        base="release/9")

    assert result["matched_branch"] == "release/9"
    assert result["base_source"] == "human_asserted"

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    # narrowing an already-recorded base with --base never overwrites it
    assert fresh.context["base_branch"] == "main"
    assert fresh.context["landed_override_base"] == "release/9"
    assert fresh.context["landed_override_base_source"] == "human_asserted"


async def test_explicit_base_unknown_branch_refused_before_ancestry(
    tmp_path, store,
):
    """AC4: `--base` must still name a branch that actually exists — refused
    before any ancestry work is attempted, and no events are written."""
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, base_branch="main", pr_branch="")

    with pytest.raises(OverrideRefused, match="does not name a branch"):
        await approve_landed_override(
            store, t, sha, "typo'd branch name", base="mian")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert (await store.list_events(t.id)) == []


async def test_override_event_records_matched_branch(tmp_path, store):
    """AC5: the recorded evidence (event + context) must carry which branch
    was matched, on the plain happy path (no stacked-base wrinkle)."""
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, base_branch="main", pr_branch="")

    await approve_landed_override(store, t, sha, "plain happy path")

    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["matched_branch"] == "main"
    assert ev["base_source"] == "recorded"

    fresh = await store.get_task(t.id)
    assert fresh.context["landed_override_base"] == "main"
    assert fresh.context["landed_override_base_source"] == "recorded"


async def test_explicit_base_resolvable_only_as_origin_ref_passes_ancestry(
    tmp_path, store,
):
    """AC2 (round-2 review finding): `--base` may name a branch that exists
    *only* as `origin/<name>` — the local branch was never fetched, only its
    remote-tracking ref was declared. `_preferred_ref_form` must resolve to
    the `origin/<name>` form so ancestry actually has something to walk,
    exactly like the already-covered "local main absent" default-branch case
    (`test_landed_sha_on_default_branch_accepted_when_local_main_is_absent`),
    but here for a human-supplied `--base` instead of the auto-resolved
    default."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "release/9")
    (repo / "a.txt").write_text("release-only change\n")
    _git(repo, "commit", "-am", "release/9: hotfix")
    release_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/release/9", release_sha)
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-D", "release/9")

    # not vacuous: the bare ref is genuinely gone, only origin/release/9 exists
    rc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         "release/9^{commit}"],
        capture_output=True, text=True,
    )
    assert rc.returncode != 0

    t = await _seed(store, repo, base_branch="main", pr_branch="")

    result = await approve_landed_override(
        store, t, release_sha, "landed on release/9, which was never "
        "fetched as a local branch in this checkout", base="release/9")

    assert result["matched_branch"] == "origin/release/9"
    assert result["base_source"] == "human_asserted"
    assert "tip unknown" not in result["text"]

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert fresh.context["landed_override_base"] == "origin/release/9"
    assert fresh.context["landed_override_base_source"] == "human_asserted"

    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["matched_branch"] == "origin/release/9"
    assert ev["base_source"] == "human_asserted"
    assert "tip unknown" not in str(ev)


async def test_default_branch_candidate_matches_when_local_main_is_stale_and_untracked(
    tmp_path, store,
):
    """AC3: an auto-resolved default-branch candidate must resolve
    `origin/main` even when local `main` exists but is STALE (present,
    behind the landed sha) and untracked (no upstream) — not just when it
    is absent (the already-covered
    `test_landed_sha_on_default_branch_accepted_when_local_main_is_absent`).

    This closes a real gap found while writing this test: `_preferred_ref_form`
    (`landed_override.py:250`) used to prefer the bare local branch whenever
    `refs_resolvable` proved it existed AT ALL, even when its tip was stale
    — it only fell back to `origin/<name>` when the bare ref was entirely
    absent. A stale-but-present branch IS resolvable (existence, not
    ancestry), so the "absent" fallback never fired for it and the task was
    refused. `_preferred_ref_form` now takes the `sha`/`is_ancestor` that
    `approve_landed_override` already has in scope, and defers to
    `origin/<name>` whenever the bare ref resolves but fails ancestry while
    the origin form succeeds — see its docstring for the full rationale.
    """
    repo, landed_sha = _repo_stacked_base(tmp_path)
    _git(repo, "checkout", "no-human/parent-3")
    pre_landing_sha = _git_out(repo, "rev-parse", "main~1")
    _git(repo, "branch", "-f", "main", pre_landing_sha)

    # not vacuous: local main truly has no upstream configured
    rc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "main@{upstream}"],
        capture_output=True, text=True,
    )
    assert rc.returncode != 0

    t = await _seed(store, repo, base_branch="no-human/parent-3", pr_branch="")

    result = await approve_landed_override(
        store, t, landed_sha, "landed on main; local main is stale and "
        "was never fetched into this checkout")

    assert result["matched_branch"] == "origin/main"
    assert result["base_source"] == "default_branch"
    assert "tip unknown" not in result["text"]


# --------------------------------------------------------------------------- #
# independent review of d6249458f (F1/F2/F3) — resolved this round            #
# --------------------------------------------------------------------------- #

async def test_pending_task_refuses_even_when_default_branch_resolves(
    tmp_path, store,
):
    """F2 fix (independent review of d6249458f): before this fix, the
    candidate-building code added the repo's resolved default branch for
    EVERY shape unconditionally, including `pending_never_ran` — so a task
    that never dispatched, with no recorded `base_branch` and no `--base`,
    silently completed against a resolvable project default the moment one
    existed (e.g. via `_declare_default`, exactly what an onboarded profile
    with `origin/HEAD` configured looks like). That reopens live incident
    855f1263's exact risk under a new name: this shape's own docstring says
    it is "the ONLY shape that accepts an explicit --base ... NEVER guessed
    from git or a profile", and the default-branch candidate is a guess.
    `pending_never_ran` must refuse regardless of whether a default branch
    resolves; only an explicit human `--base` may complete it. This is the
    named repro test in `.no_human/repro_tests.json` — it fails (silently
    completes) on the pre-fix candidate-building code and passes (refuses)
    with the fix."""
    repo = _make_repo(tmp_path)
    _declare_default(repo, "main")
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_pending(store, repo)

    with pytest.raises(OverrideRefused, match="--base"):
        await approve_landed_override(
            store, t, main_sha, "hand-landed by the supervising session")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.PENDING
    assert fresh.context.get("landed_override_sha") is None
    assert (await store.list_events(t.id)) == []


async def test_landed_override_trusts_origin_by_name_even_when_upstream_disagrees(
    tmp_path, store,
):
    """F1 pin (independent review of d6249458f, NOT a bug fix):
    `_preferred_ref_form`'s `origin/<name>` fallback hardcodes trust in a
    remote literally named `origin`, whatever it actually points to — a
    narrower reopening of exactly the OSS-fork false-positive risk
    `_base_tips` (vcs/pr_watcher.py) deliberately avoids by refusing to glob
    every `refs/remotes/*/<base>`. This is accepted, documented risk (see
    this module's docstring, "A narrower reopening of the same class of
    risk"), pinned here rather than left to drift silently: a checkout with
    two remote-tracking refs of the SAME branch name — `origin` (stand-in
    for a contributor's fork, genuinely carrying the landed commit) and
    `upstream` (stand-in for canonical, whose copy of the same name does
    NOT) — still completes via `origin/<name>` alone; `upstream` is never
    consulted."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "release/9")
    (repo / "a.txt").write_text("fork-only change\n")
    _git(repo, "commit", "-am", "release/9: fork-only change")
    landed_sha = _git_out(repo, "rev-parse", "release/9")
    _git(repo, "update-ref", "refs/remotes/origin/release/9", landed_sha)
    # upstream's copy of the same branch name never received this commit.
    main_sha = _git_out(repo, "rev-parse", "main")
    _git(repo, "update-ref", "refs/remotes/upstream/release/9", main_sha)
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-D", "release/9")  # bare form now unresolvable

    t = await _seed(store, repo, base_branch="main")

    result = await approve_landed_override(
        store, t, landed_sha, "hand-landed, verified on the fork",
        base="release/9")

    assert result["matched_branch"] == "origin/release/9"
    assert result["base_source"] == "human_asserted"


async def test_explicit_base_equal_to_landed_sha_is_a_tautology_but_passes(
    tmp_path, store,
):
    """F3 pin (independent review of d6249458f, NOT a bug fix):
    `refs_resolvable` accepts any git commit-ish — a branch, a tag, or a
    bare sha — not only a branch, despite older docstrings/CLI help saying
    "branch" (the CLI help was corrected this round; see this module's
    docstring, "What 'must still name a branch' actually means"). A raw sha
    is trivially its own ancestor, so `--base <sha>` where `<sha>` is the
    same value as `--landed` "passes" while asserting nothing about where
    the content actually landed. This is accepted, documented behavior —
    `--because` is the only real control against it — pinned here rather
    than closed, since closing it would also close legitimate
    `--base <tag>` / `--base <sha>` uses this module intentionally
    supports."""
    repo = _make_repo(tmp_path)
    landed_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed(store, repo, base_branch="main")

    result = await approve_landed_override(
        store, t, landed_sha,
        "typo'd the same value into --landed and --base",
        base=landed_sha)

    assert result["matched_branch"] == landed_sha
    assert result["base_source"] == "human_asserted"


async def test_default_branch_candidate_merges_into_recorded_label_when_they_coincide(
    tmp_path, store,
):
    """F4 (independent review of d6249458f, ride-along): when the resolved
    default-branch candidate and the recorded ``base_branch`` are literally
    the same ref form, `approve_landed_override`'s candidate-building code
    (`blockers/landed_override.py:582-583`) takes the merge branch —
    `if default_branch and default_branch == recorded_base_form:
    candidates.append((recorded_base_form, "recorded"))` — rather than
    appending both as separate candidates. Before this test, that branch was
    completely unexercised: `test_override_event_records_matched_branch`
    (the only other plain-happy-path AC5 test) never declares a default
    branch, so `default_branch` is always empty there and the `else` arm
    fires instead. This pins the deliberate choice of label for the
    coincidental-match case: the event and context both say ``"recorded"``,
    never ``"default_branch"``, even though a resolvable project default
    also independently vouches for the same branch. That label is a
    judgment call, not an obviously-right one — the recorded human intent
    (`base_branch`) is what the task's own history actually says, and the
    auto-resolved default is treated as confirmation, not as a separate,
    more-authoritative source; this test exists so a future change to that
    ordering is a deliberate, reviewed decision rather than an
    accidentally-observed side effect of which branch of the `if` a given
    call happened to take."""
    repo = _make_repo(tmp_path)
    _declare_default(repo, "main")  # resolve_project_default_branch() -> "main"
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, base_branch="main", pr_branch="")

    result = await approve_landed_override(
        store, t, sha, "plain happy path, default branch and recorded base "
        "coincide")

    assert result["matched_branch"] == "main"
    assert result["base_source"] == "recorded"

    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["matched_branch"] == "main"
    assert ev["base_source"] == "recorded"

    fresh = await store.get_task(t.id)
    assert fresh.context["landed_override_base"] == "main"
    assert fresh.context["landed_override_base_source"] == "recorded"


# --------------------------------------------------------------------------- #
# F5 (independent review of d6249458f, ride-along): direct unit coverage for  #
# the pr_watcher git-plumbing this module leans on, plus the profile         #
# precedence `_resolve_default_branch_value` documents but never tested.     #
# Co-located here (not tests/test_pr_watcher.py) because the real-git-repo   #
# helpers these need (`_make_repo`, `_declare_default`, `_git_out`) already  #
# live in this file, and test_pr_watcher.py's suite is built around         #
# mocked/`cli_recorder` forge calls, not real git plumbing.                  #
# --------------------------------------------------------------------------- #

async def test_resolve_project_default_branch_reads_origin_head_only(tmp_path):
    """`resolve_project_default_branch` had zero direct test coverage —
    every existing exercise of it went through `approve_landed_override`,
    which only ever observes it indirectly (as one candidate among several).
    Pins its two documented properties directly: it answers "" when
    `origin/HEAD` is unset, resolves to the branch `origin/HEAD` points at
    once declared, and — the property its own docstring calls out as the
    deliberate difference from `resolve_default_branch` — never falls back
    to the checkout's current branch, even when that branch has a name."""
    from no_human.vcs.pr_watcher import resolve_project_default_branch

    repo = _make_repo(tmp_path)

    # no origin/HEAD declared yet: fails soft to "", not the current branch
    assert await resolve_project_default_branch(str(repo)) == ""

    _git(repo, "checkout", "-b", "scratch")
    assert await resolve_project_default_branch(str(repo)) == "", (
        "must not fall back to the checkout's current branch"
    )
    _git(repo, "checkout", "main")

    _declare_default(repo, "main")
    assert await resolve_project_default_branch(str(repo)) == "main"


async def test_ref_tip_sha_resolves_the_short_sha_and_fails_soft(tmp_path):
    """`ref_tip_sha` also had zero direct test coverage. Pins its documented
    contract: the 12-char tip sha for anything `_base_tips` can resolve —
    not only a branch, since `_base_tips` walks whatever git commit-ish it
    is handed — "" (never raising) for a ref that resolves nowhere, and ""
    for a falsy `repo_path`/`ref` without ever shelling out. (`_base_tips`'s
    own second tip, `<ref>@{upstream}`, is only ever computed when `ref`
    already resolves as a local branch — see its docstring, "defined only
    for a LOCAL BRANCH of that name" — so for `ref_tip_sha` specifically the
    observable contract is "the ref's own tip, or \"\"", not a fallback to a
    differently-named remote form; that distinct fallback belongs to
    `_preferred_ref_form`, covered elsewhere in this file.)"""
    from no_human.vcs.pr_watcher import ref_tip_sha

    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")

    assert await ref_tip_sha(str(repo), "main") == sha[:12]
    assert await ref_tip_sha(str(repo), sha) == sha[:12]  # a raw sha resolves too
    assert await ref_tip_sha(str(repo), "no-such-branch") == ""
    assert await ref_tip_sha(str(repo), "") == ""
    assert await ref_tip_sha("", "main") == ""

    # a remote-tracking ref passed directly (not synthesized) resolves too —
    # `_base_tips([ref])` always tries the bare name it was given first.
    _git(repo, "checkout", "-b", "release/9")
    (repo / "a.txt").write_text("release-only\n")
    _git(repo, "commit", "-am", "release/9 work")
    release_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/release/9", release_sha)
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-D", "release/9")

    assert await ref_tip_sha(str(repo), "release/9") == "", (
        "the bare form is gone and _base_tips does not glob refs/remotes/* "
        "for it — see _base_tips's own docstring on why not"
    )
    assert await ref_tip_sha(str(repo), "origin/release/9") == release_sha[:12]


async def test_profile_configured_default_branch_beats_origin_head_probe(
    tmp_path, store,
):
    """F5: `_resolve_default_branch_value`'s docstring states an order — the
    project profile's configured `default_branch` wins over
    `resolve_project_default_branch`'s `origin/HEAD` probe — but nothing
    exercised BOTH sources disagreeing at once before this test. Builds a
    repo where `origin/HEAD` genuinely advertises `main` (which does NOT
    carry the landed sha) while the profile is configured for
    `release-line` (which does) — so the two sources point at different
    branches and only one can be the accepted candidate. If precedence ever
    silently flipped, this would fail by refusing (main never gets the sha)
    rather than by a mislabeled pass, staying fail-closed."""
    from no_human.profile import ProjectProfile
    from no_human.vcs.pr_watcher import resolve_project_default_branch

    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "release-line")
    (repo / "a.txt").write_text("release-line work\n")
    _git(repo, "commit", "-am", "release-line: the real fix")
    landed_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")  # main never receives this commit

    _declare_default(repo, "main")  # origin/HEAD -> main, NOT release-line
    assert await resolve_project_default_branch(str(repo)) == "main"

    await store.upsert_profile(
        ProjectProfile(repo_path=str(repo), default_branch="release-line"))

    t = await _seed(store, repo, base_branch="unrelated-branch", pr_branch="")

    result = await approve_landed_override(
        store, t, landed_sha, "hand-landed on release-line per the "
        "profile's configured default_branch, not origin/HEAD's main")

    assert result["matched_branch"] == "release-line"
    assert result["base_source"] == "default_branch"


# --------------------------------------------------------------------------- #
# "done_no_evidence" shape — nh67: a DONE task from before the completion-   #
# evidence mechanism existed (hand-landed, no DONE_EVIDENCE_KINDS event on  #
# record) that `_resolve_shape` used to refuse outright, leaving `nh doctor`#
# reporting it as an evidence gap forever with no verb able to repair it.   #
# --------------------------------------------------------------------------- #

async def test_done_task_with_no_evidence_is_accepted_and_records_override(
    tmp_path, store,
):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, status=TaskStatus.DONE, pr_branch="")

    result = await approve_landed_override(
        store, t, sha,
        "repairing a pre-mechanism hand-landing (task 16f850ae)")

    assert result["shape"] == "done_no_evidence"
    assert result["prior_status"] == "done"

    events = await store.list_events(t.id)
    override_events = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]
    assert len(override_events) == 1
    ev = override_events[0]
    assert ev["sha"] == sha
    assert ev["justification"] == (
        "repairing a pre-mechanism hand-landing (task 16f850ae)")
    assert "HUMAN OVERRIDE" in ev["text"]
    assert "prior status: done" in ev["text"]

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert fresh.context["landed_override_sha"] == sha


@pytest.mark.parametrize("kind", sorted(DONE_EVIDENCE_KINDS))
async def test_done_task_with_standing_evidence_is_refused(tmp_path, store, kind):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed_done_with_evidence(store, repo, kind)
    before = await store.list_events(t.id)

    with pytest.raises(OverrideRefused) as exc:
        await approve_landed_override(store, t, sha, "asserting anyway")

    assert kind in str(exc.value)
    assert "evidence" in str(exc.value)
    # No NEW event was appended (the seeded `kind` event may itself already
    # be a LANDED_OVERRIDE_KIND event when kind == "approved_landed_override"
    # — the refusal must add nothing on top of it, not merely leave zero).
    after = await store.list_events(t.id)
    assert after == before
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE


async def test_second_override_on_the_repaired_row_is_refused(tmp_path, store):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, status=TaskStatus.DONE, pr_branch="")

    await approve_landed_override(store, t, sha, "first repair")

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, sha, "second repair attempt")

    events = await store.list_events(t.id)
    override_events = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]
    assert len(override_events) == 1


async def test_done_shape_refuses_a_sha_that_is_not_an_ancestor(tmp_path, store):
    repo = _repo_with_residue(tmp_path)
    feature_sha = _git_out(repo, "rev-parse", "feature")
    t = await _seed(store, repo, status=TaskStatus.DONE, pr_branch="")

    with pytest.raises(OverrideRefused) as exc:
        await approve_landed_override(store, t, feature_sha, "asserting anyway")

    assert "main" in str(exc.value)
    events = await store.list_events(t.id)
    assert not [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE


@pytest.mark.parametrize("because", ["", "   "])
async def test_done_shape_refuses_blank_because(tmp_path, store, because):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, status=TaskStatus.DONE, pr_branch="")

    with pytest.raises(OverrideRefused) as exc:
        await approve_landed_override(store, t, sha, because)

    assert "justification must not be empty" in str(exc.value)
    fresh = await store.get_task(t.id)
    assert fresh.context.get("landed_override_sha") is None
    events = await store.list_events(t.id)
    assert not [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]


async def test_done_shape_refuses_when_the_event_log_cannot_be_read(
    tmp_path, store, monkeypatch,
):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, status=TaskStatus.DONE, pr_branch="")

    async def _boom(task_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store, "list_events", _boom)

    with pytest.raises(OverrideRefused) as exc:
        await approve_landed_override(store, t, sha, "asserting anyway")

    assert "could not read" in str(exc.value)


async def test_done_task_with_outstanding_pr_evidence_is_refused(tmp_path, store):
    """MAJOR 1 (independent review of nh67): `DONE_EVIDENCE_KINDS` is an
    event-KIND check only — it misses a draft PR recorded solely in
    `context["pr_draft_created"]` (never itself a `DONE_EVIDENCE_KINDS`
    event kind, per the live incident `task_pr.task_has_pr_evidence`'s own
    docstring cites, 8c8b36b5). Without this guard, a DONE task with a
    still-open, un-merged draft PR would get a landing stamped on it AND
    have that PR closed out from under it by
    `pr_closeout.close_task_prs_on_completion` — this must be refused
    exactly like the FAILED/PENDING shapes refuse standing PR evidence,
    pointing at `nh task restore-approval` instead."""
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, status=TaskStatus.DONE, pr_branch="")
    await store.merge_context(
        t.id, {"pr_draft_created": "https://example.com/pull/253"})
    t = await store.get_task(t.id)

    with pytest.raises(OverrideRefused, match="restore-approval") as exc:
        await approve_landed_override(store, t, sha, "asserting anyway")

    assert "pull/253" in str(exc.value)
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    assert not [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]


async def test_done_task_with_cancel_request_is_refused(tmp_path, store):
    """MAJOR 2 (independent review of nh67): the DONE shape had no cancel
    guard, unlike `failed_pre_pr` (`context["cancel_reason"]`) and
    `pending_never_ran` (`Store.get_cancel_request`). A cancel racing a
    hand-land must not be silently dropped by the override here either —
    mirrors `test_pending_task_with_cancel_request_is_refused`."""
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, status=TaskStatus.DONE, pr_branch="")
    await store.request_cancel(t.id, "no longer needed")

    with pytest.raises(OverrideRefused, match="cancellation request"):
        await approve_landed_override(store, t, sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    assert not [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]
