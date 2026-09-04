"""A resume checkpoint that is no longer in the object store must be LOUD.

The resume path branches each attempt from a checkpoint — the [WIP-BLOCKED]
commit a human gated with `nh reply`, or the [WIP-PARTIAL] the previous attempt
paid tens of turns for. **Nothing pushes a checkpoint.** The product's only two
push sites are on the success path (`orchestrator` after the review passes, so
CI can fetch the branch, and inside `vcs.open_pr`), so a parked, blocked,
escalated or timed-out attempt holds its work only in the LOCAL object store,
where a prune or a history rewrite takes it. On this operator's ledger that is
roughly half of all attempts.

Two defects, both reproduced here before they were fixed:

1. The existence check could not detect the thing it was written for.
   ``git rev-parse --verify <sha>`` accepts any full 40-hex string as a
   well-formed object NAME and exits 0 without consulting the object store, and
   every checkpoint sha in this system is a full 40-hex. So the guard passed a
   commit that had been pruned; `resume_wip` was emitted claiming a branch point
   the repo did not have; and the attempt then died in `git checkout -B` with
   "unable to read tree", leaving `branch_name` NULL and the attempt row stuck
   at `in_progress`. Observed, on the code before this file existed:

       raised   : GitError git checkout -B no-human/675ea443 5013e6c9… failed
                  (128): fatal: unable to read tree (5013e6c9…)
       events   : resume_wip | branching from WIP-BLOCKED 5013e6c9

2. When the check DOES fail (a ref, an abbreviated sha), the fallback to base
   was silent — the `resume_wip` emit sat inside the `try`, so nothing fired at
   all and a run that discarded a human-gated checkpoint was indistinguishable
   from one that resumed correctly.

Falling back to base stays non-fatal: after a history rewrite there may be
nothing left to resume onto. It is now announced, on the event stream AND on
the attempt row.
"""
from __future__ import annotations

import shutil
import subprocess
import types

import pytest
import pytest_asyncio

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import GitRepo


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _readable(cwd, sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", sha], cwd=cwd,
                          capture_output=True).returncode == 0


class _Stop(Exception):
    """Ends the attempt immediately after the branch decision. Everything below
    it (coder session, review, tests) is 40s of real subprocess work and none of
    it is under test — the decision and what it announced are."""


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    return work


def _plant_and_lose_checkpoint(work) -> str:
    """A real [WIP-PARTIAL] commit, then really pruned — the shape a rewrite or
    a `git gc` leaves behind. A hand-written fake sha would not prove the check
    detects the LIVE condition; the `cat-file -e` assertion below is the probe
    that the positive case really became negative."""
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "-b", "wip")
    (work / "wip_marker.py").write_text("# tens of turns of work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-PARTIAL] work")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")
    _git(work, "branch", "-D", "wip")
    _git(work, "reflog", "expire", "--expire=now", "--all")
    _git(work, "gc", "--prune=now", "--quiet")
    assert not _readable(work, sha), \
        "the checkpoint must really be gone, or this test proves nothing"
    assert _git(work, "rev-parse", "HEAD") == base
    return sha


async def _attempt_with_checkpoint(repo, tmp_path, store, monkeypatch, sha,
                                   *, wip_sha=None, attempt_n=1, by="human"):
    """Drive the REAL `_run_attempt` branch decision with `sha` as the task's
    resume point, and stop the moment the decision is made.

    ``wip_sha``/``attempt_n`` describe the OTHER checkpoint: the [WIP-PARTIAL]
    an earlier attempt of this run left behind, which only attempt 2+ inherits.
    ``by`` is the resume's provenance — ``"orphan_recovery"`` is the scheduler's
    crash requeue, whose checkpoint is an ORDINARY work commit.
    """
    cfg = load_config(tmp_path / "config.yaml")
    events: list[dict] = []
    # A stub coder: nothing calls it — `_build_implement_prompt` stops the
    # attempt first — but `_protect_base_branch` assigns onto it.
    orch = Orchestrator(store, cfg.data, types.SimpleNamespace(),
                        SlackNotifier(None),
                        event_sink=events.append)
    monkeypatch.setattr(
        Orchestrator, "_build_implement_prompt",
        lambda self, *a, **k: (_ for _ in ()).throw(_Stop()))

    t = Task.new("resume me", repo_path=str(repo))
    ctx = {"resume_from": {"sha": sha, "by": by}}
    if wip_sha:
        ctx["handoff"] = {"wip_sha": wip_sha}
    t.context = ctx
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    with pytest.raises(_Stop):
        await orch._run_attempt(t, GitRepo(repo), attempt_n, "main")
    return t, events, (await store.list_attempts(t.id))[-1]


# --------------------------------------------------------------------------- #
# THE REGRESSION                                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_vanished_checkpoint_falls_back_to_base_and_says_so(
    repo, tmp_path, store, monkeypatch,
):
    """BOTH halves, because either alone leaves half the bug pinnable: the run
    must proceed (from base), and it must announce that it discarded work."""
    lost = _plant_and_lose_checkpoint(repo)
    base_sha = _git(repo, "rev-parse", "main")

    _t, events, attempt = await _attempt_with_checkpoint(
        repo, tmp_path, store, monkeypatch, lost)

    # --- it proceeded, from base ---
    branch = attempt["branch_name"]
    assert branch, "the attempt must still get a branch — this is not fatal"
    assert _git(repo, "rev-parse", branch) == base_sha, \
        "a checkpoint that cannot be read must not become the branch point"

    # --- and it is impossible to miss ---
    lost_events = [e for e in events if e.get("kind") == "resume_checkpoint_lost"]
    assert len(lost_events) == 1, [e.get("kind") for e in events]
    ev = lost_events[0]
    assert ev.get("ok") is False
    assert ev.get("sha") == lost
    assert lost[:8] in ev["text"], "the event must NAME the sha that was lost"
    assert "main" in ev["text"], "...and what the run did instead"

    # The success event must NOT also fire; a run that lost its checkpoint must
    # not be indistinguishable from one that resumed correctly.
    assert not [e for e in events if e.get("kind") == "resume_wip"]

    # --- and it survives onto the attempt, where `nh logs` reads ---
    assert attempt["resume_checkpoint_lost"], \
        "nh logs shows attempts, not events — the event alone is not enough"
    assert lost[:8] in attempt["resume_checkpoint_lost"]
    assert attempt["failure_reason"] is None, \
        "the attempt is not failed; this must not masquerade as a failure"


@pytest.mark.asyncio
async def test_a_checkpoint_that_is_present_is_still_resumed_from(
    repo, tmp_path, store, monkeypatch,
):
    """The negative control. Without it, a `_commit_exists` that always
    returned False would pass the test above and silently break every real
    resume — which is the whole bug, inverted."""
    _git(repo, "checkout", "-q", "-b", "wip")
    (repo / "wip_marker.py").write_text("# tens of turns of work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "[WIP-BLOCKED] work")
    kept = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    _t, events, attempt = await _attempt_with_checkpoint(
        repo, tmp_path, store, monkeypatch, kept)

    assert _git(repo, "rev-parse", attempt["branch_name"]) == kept
    assert [e for e in events if e.get("kind") == "resume_wip"]
    assert not [e for e in events if e.get("kind") == "resume_checkpoint_lost"]
    assert attempt["resume_checkpoint_lost"] is None
    # …and the row says WHICH checkpoint it got, not merely that nothing was
    # lost. `resume_checkpoint_lost` is NULL on every attempt that resumed
    # normally AND on every attempt that never had a checkpoint at all — the
    # live DB had it NULL on all 828 rows — so on its own it cannot measure
    # whether resuming works. The success side needs its own column.
    assert attempt["resume_checkpoint"] == kept


@pytest.mark.asyncio
async def test_a_crash_requeue_is_not_announced_as_a_blocked_checkpoint(
    repo, tmp_path, store, monkeypatch,
):
    """The `resume_wip` label must describe the COMMIT, not the gate's verdict.

    It read `"WIP-PARTIAL" if branched_from_own_partial else "WIP-BLOCKED"`,
    which assumed every checkpoint is one of the loop's two WIP commits. The
    scheduler's crash requeue points at `attempt.commit_sha` — an ORDINARY work
    commit — so both labels lie, and "branching from WIP-BLOCKED" says a human
    answered a blocker that nothing ever raised.
    """
    _git(repo, "checkout", "-q", "-b", "wip")
    (repo / "shipped.py").write_text("# committed work, then the process died\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat: the dead attempt's work")
    dead = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    _t, events, attempt = await _attempt_with_checkpoint(
        repo, tmp_path, store, monkeypatch, dead, by="orphan_recovery")

    assert _git(repo, "rev-parse", attempt["branch_name"]) == dead
    assert attempt["resume_checkpoint"] == dead
    resumed = [e for e in events if e.get("kind") == "resume_wip"]
    assert len(resumed) == 1, [e.get("kind") for e in events]
    assert "WIP-BLOCKED" not in resumed[0]["text"], resumed[0]["text"]
    assert "WIP-PARTIAL" not in resumed[0]["text"], resumed[0]["text"]
    assert dead[:8] in resumed[0]["text"]


@pytest.mark.asyncio
async def test_a_crash_requeue_onto_a_vanished_commit_is_recorded_as_lost(
    repo, tmp_path, store, monkeypatch,
):
    """The other half of the measurement, on the provenance that had never
    produced a row: a machine requeue whose checkpoint the object store can no
    longer read falls back to base and SAYS so on the attempt."""
    lost = _plant_and_lose_checkpoint(repo)

    _t, _events, attempt = await _attempt_with_checkpoint(
        repo, tmp_path, store, monkeypatch, lost, by="orphan_recovery")

    assert attempt["resume_checkpoint"] is None, \
        "it branched from base — there is no checkpoint to record"
    assert attempt["resume_checkpoint_lost"], \
        "a crash requeue that lost its checkpoint recorded nothing"
    assert lost[:8] in attempt["resume_checkpoint_lost"]


@pytest.mark.asyncio
async def test_a_vanished_checkpoint_does_not_take_the_partial_work_with_it(
    repo, tmp_path, store, monkeypatch,
):
    """THE SECOND LOSS, and the one nothing announced.

    The fallback above is correct for the commit that is GONE. It was wrong for
    the one that is still here: `_resume_branch_point` asked whether the
    previous attempt's [WIP-PARTIAL] descends from the resume point, and
    `_ancestor_of` fails CLOSED, so the pruned sha — which cannot answer that
    question at all — vetoed the partial and won. Every attempt of the run then
    branched from base, discarding tens of turns of work that was READABLE,
    while the event stream announced only the loss of a commit that was already
    unrecoverable. The run said `resume_checkpoint_lost` and meant it about the
    wrong sha.
    """
    lost = _plant_and_lose_checkpoint(repo)
    # ...and now attempt 1's own checkpoint, made AFTER the prune so it survives
    # it, held on a branch exactly as the loop's own attempt branch holds it.
    _git(repo, "checkout", "-q", "-b", "no-human/deadbeef")
    (repo / "partial.py").write_text("# tens of turns of attempt 1's work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "[WIP-PARTIAL] work")
    wip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    assert _readable(repo, wip)

    _t, events, attempt = await _attempt_with_checkpoint(
        repo, tmp_path, store, monkeypatch, lost, wip_sha=wip, attempt_n=2)

    # --- the work that still exists is what the attempt starts from ---
    branch = attempt["branch_name"]
    assert _git(repo, "rev-parse", branch) == wip, (
        "attempt 2 branched from base and threw away the [WIP-PARTIAL] that "
        "survived, because a sha that did not survive vetoed it")
    assert "partial.py" in _git(repo, "ls-tree", "-r", "--name-only", branch)
    assert [e for e in events if e.get("kind") == "resume_wip"]

    # --- and the gated checkpoint it stepped over is still announced ---
    lost_events = [e for e in events if e.get("kind") == "resume_checkpoint_lost"]
    assert len(lost_events) == 1, [e.get("kind") for e in events]
    ev = lost_events[0]
    assert ev.get("ok") is False
    assert ev.get("sha") == lost
    assert lost[:8] in ev["text"], "the event must NAME the sha that was lost"
    assert wip[:8] in ev["text"], "...and the checkpoint it continued from"
    assert attempt["resume_checkpoint_lost"], \
        "nh logs shows attempts, not events — the event alone is not enough"
    assert lost[:8] in attempt["resume_checkpoint_lost"]
    assert wip[:8] in attempt["resume_checkpoint_lost"]
    assert attempt["failure_reason"] is None


@pytest.mark.asyncio
async def test_a_present_resume_point_is_not_reported_as_lost(
    repo, tmp_path, store, monkeypatch,
):
    """The negative control for the announcement above. A resume point that IS
    readable, stepped over in favour of a newer partial that descends from it,
    is the ordinary attempt-2 path — it lost nothing and must say nothing, or
    the event stops meaning anything."""
    _git(repo, "checkout", "-q", "-b", "no-human/deadbeef")
    (repo / "gated.py").write_text("the commit a human gated\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "[WIP-BLOCKED] gated")
    gated = _git(repo, "rev-parse", "HEAD")
    (repo / "partial.py").write_text("# attempt 1's work, on top of it\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "[WIP-PARTIAL] work")
    wip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    _t, events, attempt = await _attempt_with_checkpoint(
        repo, tmp_path, store, monkeypatch, gated, wip_sha=wip, attempt_n=2)

    assert _git(repo, "rev-parse", attempt["branch_name"]) == wip
    assert not [e for e in events if e.get("kind") == "resume_checkpoint_lost"]
    assert attempt["resume_checkpoint_lost"] is None


# --------------------------------------------------------------------------- #
# The existence check itself                                                    #
# --------------------------------------------------------------------------- #

def test_commit_exists_is_an_existence_check_not_a_syntax_check(repo):
    """🔴 The defect that made the fallback unreachable. `rev-parse --verify`
    on a full 40-hex is a SYNTAX check — it echoes any well-formed name back
    with exit 0 — and every checkpoint sha in this system is a full 40-hex.
    Pinned as an executable statement about git's behaviour so nobody
    re-introduces the cheaper spelling."""
    r = GitRepo(repo)
    gone = _plant_and_lose_checkpoint(repo)
    present = _git(repo, "rev-parse", "HEAD")

    # The old guard: passes BOTH. This is the bug, executable.
    assert r._run("rev-parse", "--verify", gone, check=True) == gone

    assert Orchestrator._commit_exists(r, present) is True   # positive control
    assert Orchestrator._commit_exists(r, gone) is False
    assert Orchestrator._commit_exists(r, "") is False
    assert Orchestrator._commit_exists(r, "no-such-ref") is False
    # A TREE is a readable object that is not a commit — resuming onto it would
    # fail in `checkout -B` exactly like a missing sha.
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    assert Orchestrator._commit_exists(r, tree) is False


def test_a_broken_is_own_partial_is_not_swallowed_as_a_missing_checkpoint(
    repo, monkeypatch,
):
    """The narrowed `except`. `_is_own_partial` decides whether the zero-diff
    honesty gate stays armed; a genuine bug in it is a different failure with a
    different meaning than a vanished sha, and the bare `except Exception` used
    to relabel it as one — quietly, and with `effective_base` already set to the
    checkpoint so not even the fallback ran."""
    import inspect

    src = inspect.getsource(Orchestrator._run_attempt)
    head, _, tail = src.partition("_resume_branch_point(repo, ctx, attempt_n)")
    assert tail, "the resume block moved — re-point this test"
    block = tail[:tail.index("if effective_base is None")]
    assert "except Exception" not in block, \
        "the resume branch-point block must not blanket-catch again"
    assert "_is_own_partial" in block
    # `_commit_exists` is the only fallible call still guarded, and it catches
    # GitError only.
    assert "except GitError" in inspect.getsource(Orchestrator._commit_exists)
    assert "except Exception" not in inspect.getsource(Orchestrator._commit_exists)


# --------------------------------------------------------------------------- #
# Why the local object store is load-bearing (characterisation)                  #
# --------------------------------------------------------------------------- #

def test_a_checkpoint_survives_the_worktree_teardown_that_created_it(tmp_path):
    """CHARACTERISATION, not a design change.

    Because nothing pushes a checkpoint, the ONLY thing that makes "the
    worktree is disposable" true is that its commits live in the SHARED object
    store of the parent repo, so tearing the worktree down does not take them.
    That property was verified empirically when the worktree-per-run fix landed
    (a SIGKILLed run's commit stayed reachable) but nothing pinned it — and if
    it regresses, the `resume_checkpoint_lost` path above stops being the rare
    case and becomes what every single resume does.

    Teardown here is exactly what the product does: `remove_worktree`
    (`git worktree remove --force` + `worktree prune`) followed by
    `shutil.rmtree`, per `_reap_dead_worktrees` / `run_task`'s finally-block.
    """
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-b", "main")
    _git(main, "config", "user.email", "u@e.com")
    _git(main, "config", "user.name", "u")
    (main / "a.py").write_text("base\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "base")

    main_repo = GitRepo(main)
    wt_path = tmp_path / "wt"
    wt = main_repo.add_worktree(wt_path, base="main", detach=True)  # _acquire_worktree
    branch = wt.create_branch("no-human/deadbeef", base="main")
    (wt_path / "wip_marker.py").write_text("# tens of turns of work\n")
    wt._run("add", "-A")
    wt._run("commit", "-m", "[WIP-PARTIAL] work")
    sha = wt.head_sha()

    # ...the run dies here. Teardown, as the product performs it.
    main_repo.remove_worktree(wt_path)
    shutil.rmtree(wt_path, ignore_errors=True)
    assert not wt_path.exists()

    # The commit is still READABLE from the parent repo — this is the property
    # the whole checkpoint mechanism rests on.
    assert _readable(main, sha), \
        "teardown took the checkpoint with it: every resume now starts from base"
    assert _git(main, "rev-parse", branch) == sha, \
        "the attempt branch must still resolve in the parent repo"
    assert "wip_marker.py" in _git(main, "ls-tree", "-r", "--name-only", sha)
    # And the orchestrator's own reader agrees.
    assert Orchestrator._commit_exists(main_repo, sha) is True
