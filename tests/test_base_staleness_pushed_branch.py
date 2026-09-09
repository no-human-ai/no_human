"""A rebased task branch can never be delivered — the bug this file pins.

`_refresh_stale_base` rewrites a stale branch with `rebase_onto` unconditionally.
That is harmless for a branch nobody has fetched yet, but once the branch has
been PUSHED, a rebase rewrites every commit, making the previously-pushed
remote tip mutually unreachable with the new head. Delivery's ancestor check
(`Orchestrator._reconcile_remote_branch`, via `is_ancestor(remote_tip,
target)`) then refuses the branch forever: 'remote tip ... is not an ancestor
of the reviewed sha' — even though the push guard is correctly declining to
force a non-fast-forward push it never should attempt.

The fix is at the staleness DECISION, not the ancestor check: `staleness_mode`
picks `"merge"` instead of `"rebase"` whenever the branch's LIVE remote tip
(`fetch_remote_branch_sha`) is truthy. A merge commit's head is a DESCENDANT
of the branch's previous tip, so a remote tip that equalled that previous tip
stays an ancestor of the new head — delivery's fast-forward path still works,
no force anywhere. A branch that has never been pushed still rebases, exactly
as before.
"""
from __future__ import annotations

import subprocess
import types

import pytest

from no_human.config import load_config
from no_human.core.orchestrator import (
    BASE_STALENESS_REBASE_THRESHOLD,
    Orchestrator,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo, ProtectedBranch


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class _Stop(Exception):
    """Ends the attempt immediately after `_refresh_stale_base` has run —
    same pattern as `tests/test_retry_base_staleness.py`: the coder session,
    review and tests are real subprocess work and none of it is under test
    here."""


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
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    return work


def _advance_main(work, n):
    for i in range(n):
        (work / f"main-advance-{i}.py").write_text(f"m = {i}\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", f"main advances {i}")


def _make_pushed_stale_branch(work, name, n):
    """A branch pushed to `origin`, then left `n` commits behind by the time
    the retry looks at it — the exact shape that used to be un-deliverable
    after a rebase: pushed once, then rewritten."""
    _git(work, "checkout", "-q", "-b", name)
    (work / "pr_marker.py").write_text("# a PR's committed work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "PR work")
    _git(work, "push", "-q", "-u", "origin", name)
    remote_tip = _git(work, "rev-parse", name)
    _git(work, "checkout", "-q", "main")
    _advance_main(work, n)
    _git(work, "checkout", "-q", name)
    return remote_tip


def _make_pushed_conflicting_branch(work, name):
    """A 1-commit gap, pushed, where both sides rewrite the same line of
    `calc.py` — the merge genuinely conflicts."""
    _git(work, "checkout", "-q", "-b", name)
    (work / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "branch rewrites the return line")
    _git(work, "push", "-q", "-u", "origin", name)
    remote_tip = _git(work, "rev-parse", name)
    _git(work, "checkout", "-q", "main")
    (work / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "main rewrites the return line differently")
    _git(work, "checkout", "-q", name)
    return remote_tip


def _make_pushed_diverged_branch(work, name, n):
    """A branch pushed to `origin`, then rewritten LOCALLY (no further push)
    so the local head is neither an ancestor nor a descendant of the remote
    tip — the `7a7713e3` shape: attempt 3's local rebase left the branch
    diverged from what it had already pushed."""
    _git(work, "checkout", "-q", "-b", name)
    (work / "pr_marker.py").write_text("# a PR's committed work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "PR work")
    _git(work, "push", "-q", "-u", "origin", name)
    remote_tip = _git(work, "rev-parse", name)
    # Rewrite the commit in place: same parent, different tree, so the new
    # head shares no ancestry with `remote_tip` in either direction —
    # exactly what a local rebase/amend after the push produces.
    (work / "pr_marker.py").write_text("# a PR's committed work, rewritten locally\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "--amend", "-m", "PR work (rewritten locally)")
    _git(work, "checkout", "-q", "main")
    _advance_main(work, n)
    _git(work, "checkout", "-q", name)
    return remote_tip


async def _attempt(repo, tmp_path, store, monkeypatch, ctx):
    """Drive the REAL `_run_attempt` through the branch decision and
    `_refresh_stale_base`, then stop before the coder session."""
    cfg = load_config(tmp_path / "config.yaml")
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, types.SimpleNamespace(),
                        SlackNotifier(None),
                        event_sink=events.append)
    monkeypatch.setattr(
        Orchestrator, "_build_implement_prompt",
        lambda self, *a, **k: (_ for _ in ()).throw(_Stop()))

    t = Task.new("pushed stale base retry", repo_path=str(repo))
    t.context = ctx
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    with pytest.raises(_Stop):
        await orch._run_attempt(t, GitRepo(repo), 1, "main")
    return t, events, orch


def _staleness_events(events):
    return [e for e in events if e.get("kind") == "base_staleness"]


# --------------------------------------------------------------------------- #
# AC1: a pushed, behind branch is MERGED (not rebased), and the previously
# pushed remote tip provably stays an ancestor of the new head. THE
# FAILS-BEFORE TEST: before the fix, `_refresh_stale_base` rebased
# unconditionally, which rewrites every commit and makes `remote_tip`
# mutually unreachable with the new HEAD — this assertion is exactly the one
# `_reconcile_remote_branch` makes before allowing delivery to push.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_pushed_stale_branch_is_merged_and_stays_deliverable(
    repo, tmp_path, store, monkeypatch,
):
    remote_tip = _make_pushed_stale_branch(
        repo, "no-human/t1", BASE_STALENESS_REBASE_THRESHOLD)
    ctx = {"pr_branch": "no-human/t1"}

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1, [e.get("kind") for e in events]
    ev = evs[0]
    assert ev["commits_behind"] == BASE_STALENESS_REBASE_THRESHOLD
    assert ev["mode"] == "merge"
    assert ev["merged"] is True
    assert ev["rebased"] is False
    assert "merged" in ev["text"] and "rebased" not in ev["text"]

    gr = GitRepo(repo)
    _git(repo, "checkout", "-q", "no-human/t1")
    head = gr.head_sha()
    # The core proof: the tip that was ALREADY on the remote before this
    # attempt touched the branch is still an ancestor of the new head. A
    # rebase would have broken this — rewriting every commit makes the old
    # tip mutually unreachable with the new one.
    assert gr.is_ancestor(remote_tip, head), (
        "the previously-pushed remote tip is no longer an ancestor of HEAD "
        "after base staleness acted — this is the exact defect that made "
        "delivery refuse a rebased, already-pushed branch"
    )

    staleness = t.context["base_staleness"]
    assert staleness["was_behind"] == BASE_STALENESS_REBASE_THRESHOLD
    assert staleness["commits_behind"] == 0
    assert staleness["mode"] == "merge"
    assert staleness["merged"] is True
    assert staleness["rebased"] is False


# --------------------------------------------------------------------------- #
# AC1 continued: delivery's OWN ancestor gate (`_reconcile_remote_branch`)
# accepts the merged branch and fast-forwards the remote — untouched code,
# exercised end to end to prove the fix actually unblocks delivery.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_merged_branch_still_clears_the_delivery_ancestor_gate(
    repo, tmp_path, store, monkeypatch,
):
    _make_pushed_stale_branch(
        repo, "no-human/t2", BASE_STALENESS_REBASE_THRESHOLD)
    ctx = {"pr_branch": "no-human/t2"}

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    gr = GitRepo(repo)
    _git(repo, "checkout", "-q", "no-human/t2")
    target = gr.head_sha()

    # Must not raise ReviewedShaMismatch — that is precisely the refusal the
    # bug caused for a rebased, already-pushed branch.
    orch._reconcile_remote_branch(
        gr, "no-human/t2", target, human_gated_resume=False)

    origin_dir = repo.parent / "origin.git"
    pushed = subprocess.run(
        ["git", "rev-parse", "refs/heads/no-human/t2"],
        cwd=str(origin_dir), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert pushed == target, "delivery did not fast-forward the remote to the merged head"


# --------------------------------------------------------------------------- #
# THE FAILS-BEFORE TEST for the review finding: a transient failure to read
# the live remote tip (network blip, auth hiccup, `ls-remote` timeout) is
# INDISTINGUISHABLE, at the `fetch_remote_branch_sha` call site, from "never
# pushed" — both return `None`. A branch that has genuinely been pushed must
# still be MERGED, never rebased, when that read merely fails; only a
# POSITIVE confirmation of absence (`remote_branch_confirmed_absent`) may
# choose rebase. Before this fix, `staleness_mode` rebased on any falsy tip,
# so this transient failure alone reintroduced the non-ancestor delivery
# refusal for an already-pushed branch.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_transient_fetch_failure_on_a_pushed_branch_still_merges(
    repo, tmp_path, store, monkeypatch,
):
    remote_tip = _make_pushed_stale_branch(
        repo, "no-human/t5", BASE_STALENESS_REBASE_THRESHOLD)
    ctx = {"pr_branch": "no-human/t5"}

    # Simulate the live remote-tip read failing (timeout/auth/network) even
    # though the branch WAS pushed — exactly what a prior review caught:
    # `fetch_remote_branch_sha` returns `None` for this, same as "never
    # pushed". `remote_branch_confirmed_absent` is left real: it still asks
    # the (real, reachable) origin directly and correctly reports the
    # branch is NOT absent, so the fix must fall open to merge.
    monkeypatch.setattr(GitRepo, "fetch_remote_branch_sha", lambda self, *a, **k: None)

    def _boom(self, base):
        raise AssertionError(
            "rebase_onto must never be called when the remote tip merely "
            "failed to be read on an already-pushed branch — that is "
            "exactly the bug this fix closes")
    monkeypatch.setattr(GitRepo, "rebase_onto", _boom)

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["mode"] == "merge"
    assert ev["merged"] is True
    assert ev["rebased"] is False

    gr = GitRepo(repo)
    _git(repo, "checkout", "-q", "no-human/t5")
    head = gr.head_sha()
    assert gr.is_ancestor(remote_tip, head), (
        "a transient fetch failure on an already-pushed branch must still "
        "merge, keeping the real (unreadable-at-decision-time) remote tip "
        "an ancestor of the new head"
    )


# --------------------------------------------------------------------------- #
# AC2: a branch that has NEVER been pushed still rebases — mode, event text
# and behaviour are all unchanged for this case.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_never_pushed_branch_still_rebases(
    repo, tmp_path, store, monkeypatch,
):
    # `main` is pushed (so `origin` is configured and reachable), but the
    # task branch itself is never pushed — `fetch_remote_branch_sha` must
    # read that as "never pushed" (None), not "no remote configured".
    _git(repo, "checkout", "-q", "-b", "no-human/t3")
    (repo / "pr_marker.py").write_text("# never pushed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "PR work, never pushed")
    _git(repo, "checkout", "-q", "main")
    _advance_main(repo, BASE_STALENESS_REBASE_THRESHOLD)
    _git(repo, "checkout", "-q", "no-human/t3")

    ctx = {"pr_branch": "no-human/t3"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["mode"] == "rebase"
    assert ev["rebased"] is True
    assert ev["merged"] is False
    assert ev["text"] == (
        f"branch no-human/t3 is {BASE_STALENESS_REBASE_THRESHOLD} commit(s) "
        "behind main — rebased onto it"
    ), "event text for the never-pushed rebase case must be unchanged"

    staleness = t.context["base_staleness"]
    assert staleness["rebased"] is True
    assert staleness["mode"] == "rebase"
    assert staleness["commits_behind"] == 0


# --------------------------------------------------------------------------- #
# THE FAILS-BEFORE TEST for the divergence-advisory review finding: a task
# branch whose remote tip is ALREADY diverged from HEAD (neither an ancestor
# nor a descendant — the `7a7713e3` shape, left by a local rebase after the
# push) is merged with the base by `_refresh_stale_base`, the event says
# "merged main into it", and delivery still refuses it later ('remote tip
# ... is not an ancestor of the reviewed sha') with nothing in the attempt
# naming the pre-existing divergence. An operator reading only the
# `base_staleness` event sees a green-looking base refresh, then an
# unexplained refusal at delivery. Before this fix, no advisory named the
# divergence and no `diverged` field existed in the record.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_an_already_diverged_remote_tip_is_named_in_an_advisory_and_recorded(
    repo, tmp_path, store, monkeypatch,
):
    remote_tip = _make_pushed_diverged_branch(
        repo, "no-human/t6", BASE_STALENESS_REBASE_THRESHOLD)

    gr = GitRepo(repo)
    pre_merge_head = gr.head_sha()
    # Positive control: the fixture really produced a divergence (neither
    # side is an ancestor of the other) before the attempt does anything.
    assert gr.is_ancestor(remote_tip, pre_merge_head) is False
    assert gr.is_ancestor(pre_merge_head, remote_tip) is False

    ctx = {"pr_branch": "no-human/t6"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    advisories = [
        e["text"] for e in events
        if e.get("kind") == "advisory" and "diverged" in e.get("text", "")
    ]
    assert len(advisories) == 1, advisories
    advisory = advisories[0]
    assert remote_tip in advisory
    assert pre_merge_head in advisory
    assert "diverged" in advisory
    assert "is not an ancestor of the reviewed sha" in advisory

    assert t.context["base_staleness"]["diverged"] is True

    evs = _staleness_events(events)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["diverged"] is True
    assert ev["mode"] == "merge"
    assert ev["merged"] is True
    # The attempt reached `_build_implement_prompt` (`_Stop`, asserted by
    # `_attempt`) — never failed by the divergence.


@pytest.mark.asyncio
async def test_a_diverged_branch_still_merges_the_base_and_is_not_force_pushed(
    repo, tmp_path, store, monkeypatch,
):
    remote_tip = _make_pushed_diverged_branch(
        repo, "no-human/t7", BASE_STALENESS_REBASE_THRESHOLD)

    gr = GitRepo(repo)
    pre_merge_head = gr.head_sha()
    main_tip = _git(repo, "rev-parse", "main")

    def _boom(self, base):
        raise AssertionError(
            "rebase_onto must never be called for a pushed (even diverged) "
            "branch — divergence must not flip merge to rebase")
    monkeypatch.setattr(GitRepo, "rebase_onto", _boom)

    ctx = {"pr_branch": "no-human/t7"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    _git(repo, "checkout", "-q", "no-human/t7")
    head = gr.head_sha()
    assert gr.is_ancestor(pre_merge_head, head), (
        "the pre-attempt local head must still be an ancestor of the new "
        "head — a real merge, not a rewrite"
    )
    assert gr.is_ancestor(main_tip, head), (
        "the base merge must actually have happened despite the divergence"
    )

    origin_dir = repo.parent / "origin.git"
    origin_ref = subprocess.run(
        ["git", "rev-parse", "refs/heads/no-human/t7"],
        cwd=str(origin_dir), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert origin_ref == remote_tip, (
        "the divergence advisory must be pure observation — nothing may "
        "push or force-update the remote tip"
    )


@pytest.mark.asyncio
async def test_no_divergence_advisory_when_the_branch_has_no_remote_tip(
    repo, tmp_path, store, monkeypatch,
):
    # Reuses the never-pushed shape (intake answer 3): no remote tip means
    # divergence is undefined, so the check must stay silent.
    _git(repo, "checkout", "-q", "-b", "no-human/t8")
    (repo / "pr_marker.py").write_text("# never pushed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "PR work, never pushed")
    _git(repo, "checkout", "-q", "main")
    _advance_main(repo, BASE_STALENESS_REBASE_THRESHOLD)
    _git(repo, "checkout", "-q", "no-human/t8")

    ctx = {"pr_branch": "no-human/t8"}
    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    advisories = [
        e["text"] for e in events
        if e.get("kind") == "advisory" and "diverged" in e.get("text", "")
    ]
    assert advisories == []
    assert "diverged" not in t.context["base_staleness"]


# --------------------------------------------------------------------------- #
# `GitRepo.remote_branch_confirmed_absent` unit coverage: it must return
# `True` ONLY when the remote was actually reached and positively reported no
# such branch (or there is no remote to have been pushed to at all), and
# `False` for every kind of "cannot tell" — never guessing "absent" for an
# error.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_failing_divergence_check_records_no_diverged_key_and_still_merges(
    repo, tmp_path, store, monkeypatch,
):
    """The divergence check is observation only: when it RAISES (an ancestry
    query failing for any reason), the attempt records no `diverged` key at
    all — never a guessed `True` — emits the "divergence check failed"
    advisory, and still merges the base in exactly as for a healthy check."""
    remote_tip = _make_pushed_stale_branch(
        repo, "no-human/t9", BASE_STALENESS_REBASE_THRESHOLD)
    ctx = {"pr_branch": "no-human/t9"}

    def _boom(self, *a, **k):
        raise RuntimeError("ancestry query exploded")
    monkeypatch.setattr(GitRepo, "is_ancestor", _boom)

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)
    monkeypatch.undo()

    failed = [
        e["text"] for e in events
        if e.get("kind") == "advisory"
        and "divergence check failed" in e.get("text", "")
    ]
    assert len(failed) == 1, failed
    assert "ancestry query exploded" in failed[0]
    assert not [
        e for e in events
        if e.get("kind") == "advisory" and "ALREADY diverged" in e.get("text", "")
    ]
    assert "diverged" not in t.context["base_staleness"]

    evs = _staleness_events(events)
    assert len(evs) == 1
    ev = evs[0]
    # The event always carries the flag (False here); only the persisted
    # payload omits it when the branch is not known to be diverged.
    assert ev["diverged"] is False
    assert ev["mode"] == "merge"
    assert ev["merged"] is True

    gr = GitRepo(repo)
    _git(repo, "checkout", "-q", "no-human/t9")
    assert gr.is_ancestor(remote_tip, gr.head_sha())


def test_remote_branch_confirmed_absent_distinguishes_absence_from_errors(repo):
    gr = GitRepo(repo)

    # Genuinely never pushed, remote reachable: positively confirmed absent.
    assert gr.remote_branch_confirmed_absent("no-human/never-pushed") is True

    # Pushed: reachable, and the remote reports it exists — not absent.
    _git(repo, "checkout", "-q", "-b", "no-human/pushed")
    (repo / "marker.py").write_text("# pushed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pushed branch")
    _git(repo, "push", "-q", "-u", "origin", "no-human/pushed")
    assert gr.remote_branch_confirmed_absent("no-human/pushed") is False

    # No remote configured at all: nothing could have been pushed anywhere,
    # so absence is safe to assume.
    _git(repo, "remote", "remove", "origin")
    assert gr.remote_branch_confirmed_absent("no-human/never-pushed") is True


def test_remote_branch_confirmed_absent_fails_closed_on_an_unreachable_remote(repo):
    # An unreachable remote (bad path — same shape as a network/auth
    # failure: `git ls-remote` exits non-zero) must NOT be read as "absent".
    _git(repo, "remote", "set-url", "origin", "/no/such/path/at/all.git")
    gr = GitRepo(repo)
    assert gr.remote_branch_confirmed_absent("no-human/anything") is False


# --------------------------------------------------------------------------- #
# The adopted merge-conflict assumption: abort and proceed un-updated, never
# fail the attempt, never fall back to rebase (that would reintroduce the
# exact non-ancestor refusal this fix closes).
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_conflicting_merge_does_not_fail_the_attempt_and_never_falls_back_to_rebase(
    repo, tmp_path, store, monkeypatch,
):
    def _boom(self, base):
        raise AssertionError(
            "rebase_onto must never be called for a pushed branch — that "
            "would reintroduce the non-ancestor delivery refusal")
    monkeypatch.setattr(GitRepo, "rebase_onto", _boom)

    _make_pushed_conflicting_branch(repo, "no-human/t4")
    ctx = {"pr_branch": "no-human/t4"}

    t, events, orch = await _attempt(repo, tmp_path, store, monkeypatch, ctx)

    evs = _staleness_events(events)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["mode"] == "merge"
    assert ev["merged"] is False
    assert ev["rebased"] is False
    assert "merge skipped (conflict)" in ev["text"]

    # No merge in progress was left dangling.
    status = _git(repo, "status", "--porcelain")
    assert status == "", f"merge conflict was not cleanly aborted: {status!r}"

    staleness = t.context["base_staleness"]
    assert staleness["rebased"] is False
    assert "mode" not in staleness, (
        "a failed/no-op action keeps the record shape unchanged, per "
        "staleness_record's contract"
    )


# --------------------------------------------------------------------------- #
# AC3: no force-push anywhere in the two files this fix touched.
# --------------------------------------------------------------------------- #

def test_no_force_push_introduced_by_this_fix():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "no_human"
    for rel in ("vcs/git.py", "core/orchestrator.py"):
        text = (src / rel).read_text(encoding="utf-8")
        assert "merge_base_into_branch" in text or rel != "vcs/git.py"
        # The new merge path must not introduce --force / --force-with-lease
        # anywhere it wasn't already present.
        merge_fn_start = text.find("def merge_base_into_branch")
        if merge_fn_start != -1:
            merge_fn_end = text.find("\n    def ", merge_fn_start + 1)
            merge_fn_src = text[merge_fn_start:merge_fn_end if merge_fn_end != -1 else None]
            assert "--force" not in merge_fn_src
            assert "force-with-lease" not in merge_fn_src
        refresh_fn_start = text.find("async def _refresh_stale_base")
        if refresh_fn_start != -1:
            refresh_fn_end = text.find("\n    async def ", refresh_fn_start + 1)
            refresh_fn_src = text[refresh_fn_start:refresh_fn_end if refresh_fn_end != -1 else None]
            assert "--force" not in refresh_fn_src
            assert "force-with-lease" not in refresh_fn_src
            # No automatic merge of the remote tip into HEAD: the divergence
            # advisory is observation-only, so the ONLY `merge_base_into_branch`
            # call in this function must pass `base`, never `remote_tip`.
            assert "merge_base_into_branch(remote_tip)" not in refresh_fn_src
            assert refresh_fn_src.count("merge_base_into_branch(") == 1
            assert "merge_base_into_branch(base)" in refresh_fn_src


# --------------------------------------------------------------------------- #
# AC2 (concern 2): `GitRepo.merge_base_into_branch` had no protected-branch
# guard of its own — it is reachable only through the `create_branch`-guarded
# `pr_branch` today (`_branch_protected` guards create_branch, commit_all and
# commit_paths; `rebase_onto` has no guard of its own either — out of scope).
# THE FAILS-BEFORE TEST: before this fix, calling it while on a protected
# branch (e.g. `main`) would run `git merge` directly.
# --------------------------------------------------------------------------- #

def test_merge_base_into_branch_refuses_a_protected_branch_without_touching_refs(
    repo,
):
    # `repo` is checked out on `main`, which matches the default
    # `never_push_to`. Give it something to merge: an unrelated branch with
    # a real commit ahead of `main`.
    _git(repo, "checkout", "-q", "-b", "no-human/other")
    (repo / "other.py").write_text("# unrelated work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unrelated work")
    _git(repo, "checkout", "-q", "main")

    head_before = _git(repo, "rev-parse", "HEAD")
    refs_before = _git(repo, "rev-parse", "--all")
    reflog_before = _git(repo, "reflog", "show", "HEAD")

    gr = GitRepo(repo)
    with pytest.raises(ProtectedBranch, match="protected branch"):
        gr.merge_base_into_branch("no-human/other")

    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "rev-parse", "--all") == refs_before
    assert _git(repo, "reflog", "show", "HEAD") == reflog_before
