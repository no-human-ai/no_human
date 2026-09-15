"""A stale-but-mergeable PR is never re-measured or woken (bugfix, split from
task 22c4ddf6's finding #3). `core/orchestrator.py:_finalize` never recorded
the trunk tip a delivered PR was measured against, and none of
`blockers.wake.WakeWatcher._check_open_pr`'s existing rungs matched a PR that
stayed MERGEABLE while trunk moved past it — trunk can move repeatedly while
a PR sits at AWAITING_APPROVAL, and the only way back was a human touching it
or a fresh coder attempt racing the next landing.

Every test here drives REAL, from-scratch local git repos — a bare `origin`
plus two independent working trees (`work`, the task's own checkout the
watcher reads; `lander`, a second clone that lands trunk commits the way a
human merging on GitHub or another machine would, entirely independent of
`work`). "Trunk advanced" is always a genuine `git push` to the bare origin,
never a flag any test passes to the code under test — `vcs.delivered_base
.measure` is exercised against the actual ref state exactly as
`blockers.wake.WakeWatcher._check_base_stale` calls it in production.

No test here asserts on the source text of the code under test.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from click.testing import CliRunner

import no_human.cli.commands as cli_commands
from no_human.blockers.wake import WakeWatcher
from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs import delivered_base


# ---------------------------------------------------------------------------
# git plumbing helpers (mirrors tests/test_orchestrator_pr_conflict.py's
# `_repo()`, minus the export-gate stub machinery this file has no use for)
# ---------------------------------------------------------------------------

def _run(args: list[str], *, cwd) -> subprocess.CompletedProcess:
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{args} failed rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def _git(cwd, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd)


def _repo(tmp_path: Path) -> Path:
    """A from-scratch repo (+ bare origin, pushed with ``-u`` so
    ``main@{upstream}`` resolves, matching `pr_watcher._base_tips`'s
    requirement) with one commit. Returns the working-tree path."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _run(["git", "init", "-q", "--bare", str(origin)], cwd=tmp_path)
    _run(["git", "init", "-q", "-b", "main", str(work)], cwd=tmp_path)
    _git(work, "config", "user.email", "a@example.com")
    _git(work, "config", "user.name", "a")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


def _clone(tmp_path: Path, work: Path, name: str) -> Path:
    """A second, fully independent checkout of the same origin — used to
    land trunk commits the way a human merging on GitHub, or a landing on a
    different machine, would: with zero interaction with `work` (the
    checkout `task.repo_path` points at and the watcher reads)."""
    origin = _git(work, "remote", "get-url", "origin").stdout.strip()
    dest = tmp_path / name
    _run(["git", "clone", "-q", origin, str(dest)], cwd=tmp_path)
    _git(dest, "config", "user.email", "b@example.com")
    _git(dest, "config", "user.name", "b")
    return dest


def _land(lander: Path, filename: str) -> str:
    """A genuine trunk landing: commit + push from the independent `lander`
    clone. Returns the new tip sha as observed from `lander` itself (the
    strongest evidence available that trunk really moved, obtained without
    touching `work` at all)."""
    (lander / filename).write_text(f"{filename}\n", encoding="utf-8")
    _git(lander, "add", "-A")
    _git(lander, "commit", "-qm", f"land {filename}")
    _git(lander, "push", "-q", "origin", "main")
    return _git(lander, "rev-parse", "HEAD").stdout.strip()


def _make_branch(work: Path, branch: str) -> None:
    """A real feature branch off current `main`, created directly in `work`
    and pushed to origin — gives `conflicting_paths` a genuine, DIFFERENT
    ref to merge-tree against instead of the degenerate `base == branch`
    self-merge (which returns an empty conflict set unconditionally,
    regardless of whether the real re-verification logic is even reached)."""
    _git(work, "branch", branch)
    _git(work, "push", "-q", "origin", f"{branch}:refs/heads/{branch}")


async def _pr_task(store, repo_path: Path, *, base_sha: str | None,
                    pr_branch: str = "main",
                    url: str = "https://code.example.com/dev/x/pull/9") -> Task:
    t = Task.new("stale-base", repo_path=str(repo_path))
    ctx = {"pr_watch": url, "pr_branch": pr_branch, "base_branch": "main"}
    if base_sha is not None:
        ctx["pr_base_sha"] = base_sha
    t.context = ctx
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, mergeable: str = "MERGEABLE", merge_state: str = "",
             events: list | None = None) -> WakeWatcher:
    async def pr_mergeable(url):
        return {"mergeable": mergeable, "mergeStateStatus": merge_state}

    return WakeWatcher(
        store, {},
        pr_mergeable=pr_mergeable,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


def _trunk_sha(work: Path) -> str:
    return _git(work, "rev-parse", "origin/main").stdout.strip()


# ---------------------------------------------------------------------------
# The headline acceptance-criterion test: a real landing, observed for real.
# ---------------------------------------------------------------------------

async def test_a_landing_on_trunk_remeasures_the_delivered_base(store, tmp_path):
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    # A genuinely different ref from the base — never the degenerate
    # base==branch self-merge, which would trivially return an empty
    # conflict set without exercising the real merge-tree comparison at all.
    branch = "feature-a"
    _make_branch(work, branch)

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    events = []
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=events)

    # Before anything lands: still fresh, must not be touched.
    out = await w._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert "pr_base_freshness" not in (fresh.context or {})
    assert "pr_base_remeasures" not in (fresh.context or {})

    # A real landing on trunk — made entirely through the independent
    # `lander` clone, never touching `work` or this task.
    new_sha = _land(lander, "feature_x.py")
    assert new_sha != recorded

    out = await w._check_open_pr(t)
    # The base-staleness rung's outcome is threaded through `_check_open_pr`
    # so a caller driving the ladder end-to-end — `tick()`, and the `wake`
    # CLI command through it — sees that something happened, even though
    # nothing here resumes the task or consumes a coder attempt.
    assert out == "pr_base_remeasured"

    fresh = await store.get_task(t.id)
    ctx = fresh.context or {}
    # No human touched it, no coder attempt was consumed: status is exactly
    # where it was, and none of the coder-round bookkeeping fired.
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert "pr_conflict_rounds" not in ctx
    assert not ctx.get("send_back_feedback")
    # The re-measure happened, driven by the real ref — but `git merge-tree`
    # only proves TEXTUAL mergeability, never semantic safety, so this stays
    # STALE and `pr_base_sha` is deliberately left unbumped: bumping it would
    # make the very next `measure()` see recorded == observed and answer
    # FRESH forever, destroying the only signal a later, more thorough
    # consumer could act on.
    assert ctx["pr_base_sha"] == recorded
    assert ctx["pr_base_remeasures"] == 1
    assert ctx["pr_base_freshness"]["state"] == delivered_base.STALE
    assert ctx["pr_base_freshness"]["recorded_sha"] == recorded
    assert ctx["pr_base_freshness"]["observed_sha"] == new_sha
    assert any(k == "pr_base_remeasured" for k, _ in events)


async def test_delivery_records_the_trunk_tip_it_was_measured_against(tmp_path):
    work = _repo(tmp_path)
    expected = _trunk_sha(work)
    patch = await delivered_base.record_at_delivery(str(work), "main")
    assert patch == {"pr_base_sha": expected, "pr_base_ref": "main"}


# ---------------------------------------------------------------------------
# The wake path acts on stale-but-mergeable — and a positive control showing
# it does NOT wake a still-fresh task.
# ---------------------------------------------------------------------------

async def test_the_rung_acts_on_stale_but_mergeable(store, tmp_path):
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    branch = "feature-b"
    _make_branch(work, branch)
    new_sha = _land(lander, "another.py")

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    events = []
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=events)

    out = await w._check_base_stale(t, "https://x/pull/9",
                                     {"mergeable": "MERGEABLE"})
    assert out == "pr_base_remeasured"
    fresh = await store.get_task(t.id)
    ctx = fresh.context
    # `pr_base_sha` stays at the originally recorded value — a merely
    # textually-clean re-verification is never license to bump it (see the
    # `_check_base_stale` docstring for why).
    assert ctx["pr_base_sha"] == recorded
    assert ctx["pr_base_freshness"]["state"] == delivered_base.STALE
    assert ctx["pr_base_freshness"]["observed_sha"] == new_sha
    assert fresh.status is TaskStatus.AWAITING_APPROVAL


async def test_stale_pr_is_recorded_stale_not_fresh_on_a_semantic_break(
        store, tmp_path):
    """`git merge-tree` only proves TEXTUAL mergeability, never semantic
    safety. Trunk renames `mod.py` -> `mod_renamed.py`; the PR branch
    independently adds `caller.py` that still imports the old module name.
    The two changes touch disjoint paths, so merge-tree finds zero
    conflicting paths — textually clean — even though the merged result is
    broken (an import of a module that no longer exists once merged).

    A textually-clean-but-not-semantically-safe result like this one must be
    recorded ``state: stale`` (never ``fresh``), with `pr_base_sha` left
    unbumped so the signal survives: recording ``fresh`` here (and bumping
    the recorded sha to the new tip) would make the very next `measure()`
    see recorded == observed and answer fresh forever, permanently
    destroying the only signal available to catch this."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")

    (work / "mod.py").write_text("def old_name():\n    return 1\n",
                                  encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "add mod.py")
    _git(work, "push", "-q", "origin", "main")
    recorded = _trunk_sha(work)

    branch = "feature-semantic-break"
    _make_branch(work, branch)
    _git(work, "checkout", "-q", branch)
    (work / "caller.py").write_text(
        "from mod import old_name\n\nold_name()\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "caller imports mod.old_name")
    _git(work, "push", "-q", "origin", branch)
    _git(work, "checkout", "-q", "main")

    # Trunk renames the module out from under the PR — a genuine landing via
    # the independent `lander` clone, on a path disjoint from `caller.py`,
    # so `git merge-tree` sees no conflicting path at all.
    # `lander` was cloned before "add mod.py" landed, so its local `main`
    # must be brought forward before the file it is about to rename exists
    # in its own working tree.
    _git(lander, "fetch", "-q", "origin", "main")
    _git(lander, "reset", "-q", "--hard", "origin/main")
    _git(lander, "mv", "mod.py", "mod_renamed.py")
    _git(lander, "commit", "-qm", "rename mod.py -> mod_renamed.py")
    _git(lander, "push", "-q", "origin", "main")
    new_sha = _git(lander, "rev-parse", "HEAD").stdout.strip()
    assert new_sha != recorded

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    events = []
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=events)

    out = await w._check_base_stale(t, "https://x/pull/9",
                                     {"mergeable": "MERGEABLE"})
    assert out == "pr_base_remeasured"
    fresh = await store.get_task(t.id)
    ctx = fresh.context
    # Never fresh — the merge is only textually clean, not verified safe.
    assert ctx["pr_base_freshness"]["state"] == delivered_base.STALE
    assert ctx["pr_base_freshness"]["state"] != delivered_base.FRESH
    assert ctx["pr_base_freshness"]["observed_sha"] == new_sha
    # The recorded sha must stay put so the staleness signal survives a
    # later tick — bumping it here would make the next `measure()` see
    # recorded == observed and answer FRESH forever.
    assert ctx["pr_base_sha"] == recorded
    assert any(k == "pr_base_remeasured" for k, _ in events)


async def test_stale_pr_recovers_after_a_flaky_first_enumeration(
        store, tmp_path, monkeypatch):
    """A transient enumeration failure (the common real-world cause: the
    watcher's local branch ref is present but momentarily stale/racing a
    concurrent fetch) must not be the final word — `_check_base_stale`
    fetches and retries once, exactly as `_check_pr_conflict` already does
    for the same failure shape (see
    `test_orchestrator_pr_conflict.py::test_a_raising_enumeration_recovers_after_a_ref_fetch_and_resolves_mechanically`
    for the established pattern this mirrors). Drives a REAL branch and a
    REAL trunk landing; only the first `conflicting_paths` call is faked to
    fail, so the retry that follows resolves against actual git state."""
    from no_human.vcs import derived_conflict as dc

    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    branch = "feature-flaky"
    _make_branch(work, branch)
    new_sha = _land(lander, "another.py")

    real_conflicting_paths = dc.conflicting_paths
    real_fetch = dc.fetch_conflict_refs
    calls = {"n": 0}
    fetch_calls = []

    async def flaky(repo_path, base_tip, branch_arg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bad object main")
        return await real_conflicting_paths(repo_path, base_tip, branch_arg)

    async def spying_fetch(repo_path, base, branch_arg):
        fetch_calls.append((repo_path, base, branch_arg))
        return await real_fetch(repo_path, base, branch_arg)

    monkeypatch.setattr(dc, "conflicting_paths", flaky)
    monkeypatch.setattr(dc, "fetch_conflict_refs", spying_fetch)

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN")

    out = await w._check_base_stale(t, "https://x/pull/9",
                                     {"mergeable": "MERGEABLE"})
    assert out == "pr_base_remeasured"
    assert calls["n"] == 2  # first call raised, retry (after a fetch) succeeded
    assert len(fetch_calls) == 1
    fresh = await store.get_task(t.id)
    ctx = fresh.context
    # The fetch-and-retry recovers the ABILITY to ask the merge-tree
    # question; a textually-clean answer is still only textual, never a
    # license to bump the recorded sha.
    assert ctx["pr_base_sha"] == recorded
    assert ctx["pr_base_freshness"]["state"] == delivered_base.STALE
    assert ctx["pr_base_freshness"]["observed_sha"] == new_sha


async def test_stale_pr_with_missing_branch_is_undetermined_not_fresh(store, tmp_path):
    """`conflicting_paths` returning ``None`` (the question could not be
    asked at all — here because the recorded PR branch never existed
    anywhere, so even a fetch-and-retry cannot resolve it) must never be
    coerced into a determined-fresh answer. A bare ``if conflict_paths:``
    would treat ``None`` the same as an empty (verified-clean) set and
    record ``state: fresh`` with zero verification performed."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    _land(lander, "feature_y.py")

    t = await _pr_task(store, work, base_sha=recorded, pr_branch="ghost-branch")
    events = []
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=events)

    out = await w._check_base_stale(t, "https://x/pull/9",
                                     {"mergeable": "MERGEABLE"})
    assert out == "pr_base_undetermined"
    fresh = await store.get_task(t.id)
    ctx = fresh.context
    assert ctx["pr_base_freshness"]["state"] == delivered_base.UNDETERMINED
    assert ctx["pr_base_freshness"]["state"] != delivered_base.FRESH
    # The stale recorded sha must not be silently advanced.
    assert ctx["pr_base_sha"] == recorded
    assert any(k == "pr_base_undetermined" for k, _ in events)


async def test_stale_pr_undetermined_answer_does_not_repeat_forever(store, tmp_path):
    """The undetermined record is bounded: an identical undetermined answer
    on a later tick must not re-write context or re-emit an event."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    _land(lander, "feature_z.py")

    t = await _pr_task(store, work, base_sha=recorded, pr_branch="ghost-branch-2")
    events = []
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=events)

    first = await w._check_base_stale(t, "https://x/pull/9",
                                       {"mergeable": "MERGEABLE"})
    assert first == "pr_base_undetermined"
    assert len(events) == 1

    t2 = await store.get_task(t.id)
    second = await w._check_base_stale(t2, "https://x/pull/9",
                                        {"mergeable": "MERGEABLE"})
    assert second is None
    assert len(events) == 1


async def test_stale_pr_with_a_real_conflict_is_left_for_the_conflict_rung(store, tmp_path):
    """A genuine merge-tree conflict at the new tip must not be recorded as
    fresh — that stays reachable as STALE (and eventually CONFLICTING once
    the forge catches up) for `_check_pr_conflict`'s own ladder to own."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)

    branch = "feature-conflict"
    _make_branch(work, branch)
    _git(work, "checkout", "-q", branch)
    (work / "README.md").write_text("branch change\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "branch edits README")
    _git(work, "push", "-q", "origin", branch)
    _git(work, "checkout", "-q", "main")

    # A conflicting edit lands on trunk via the independent `lander` clone.
    (lander / "README.md").write_text("trunk change\n", encoding="utf-8")
    _git(lander, "add", "-A")
    _git(lander, "commit", "-qm", "trunk edits README")
    _git(lander, "push", "-q", "origin", "main")

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    events = []
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=events)

    out = await w._check_base_stale(t, "https://x/pull/9",
                                     {"mergeable": "MERGEABLE"})
    assert out is None
    fresh = await store.get_task(t.id)
    ctx = fresh.context or {}
    assert ctx.get("pr_base_sha") == recorded
    assert "pr_base_freshness" not in ctx
    assert not any(k.startswith("pr_base_") for k, _ in events)


async def test_a_fresh_mergeable_pr_is_not_woken(store, tmp_path):
    work = _repo(tmp_path)
    recorded = _trunk_sha(work)
    # Nothing lands — trunk stays exactly where it was.
    t = await _pr_task(store, work, base_sha=recorded)
    events = []
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=events)

    out = await w._check_base_stale(t, "https://x/pull/9",
                                     {"mergeable": "MERGEABLE"})
    assert out is None
    fresh = await store.get_task(t.id)
    assert "pr_base_freshness" not in (fresh.context or {})
    assert "pr_base_remeasures" not in (fresh.context or {})
    assert not any(k == "pr_base_remeasured" for k, _ in events)


async def test_unknown_mergeable_never_remeasures(store, tmp_path):
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    _land(lander, "another.py")

    t = await _pr_task(store, work, base_sha=recorded)
    w = _watcher(store, mergeable="UNKNOWN")

    out = await w._check_base_stale(t, "https://x/pull/9", {"mergeable": "UNKNOWN"})
    assert out is None
    fresh = await store.get_task(t.id)
    assert "pr_base_freshness" not in (fresh.context or {})


async def test_behind_merge_state_alone_does_not_remeasure(store, tmp_path):
    """GitHub's own ``mergeStateStatus`` can say BEHIND while ``mergeable``
    itself is not a definite MERGEABLE — the rung must gate on ``mergeable``
    alone, never let the forge's own staleness flag substitute for the real
    ref comparison this bugfix exists to do instead."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    _land(lander, "another.py")

    t = await _pr_task(store, work, base_sha=recorded)
    w = _watcher(store, mergeable="", merge_state="BEHIND")

    out = await w._check_base_stale(t, "https://x/pull/9",
                                     {"mergeable": "", "mergeStateStatus": "BEHIND"})
    assert out is None
    fresh = await store.get_task(t.id)
    assert "pr_base_freshness" not in (fresh.context or {})


# ---------------------------------------------------------------------------
# Fail-closed: could-not-determine must never read back as fresh.
# ---------------------------------------------------------------------------

async def test_unreadable_trunk_is_undetermined_not_fresh(store, tmp_path):
    broken_repo = tmp_path / "does_not_exist"
    t = await _pr_task(store, broken_repo, base_sha="deadbeef" * 5)
    events = []
    w = _watcher(store, mergeable="MERGEABLE", events=events)

    out = await w._check_base_stale(t, "https://x/pull/9", {"mergeable": "MERGEABLE"})
    assert out == "pr_base_undetermined"
    fresh = await store.get_task(t.id)
    freshness = fresh.context["pr_base_freshness"]
    assert freshness["state"] == delivered_base.UNDETERMINED
    assert freshness["state"] != delivered_base.FRESH
    assert any(k == "pr_base_undetermined" for k, _ in events)


async def test_missing_recorded_base_sha_is_undetermined_and_backfilled(store, tmp_path):
    work = _repo(tmp_path)
    trunk = _trunk_sha(work)
    # No `pr_base_sha` was ever recorded (a pre-existing delivery).
    t = await _pr_task(store, work, base_sha=None)
    assert "pr_base_sha" not in t.context
    w = _watcher(store, mergeable="MERGEABLE")

    out = await w._check_base_stale(t, "https://x/pull/9", {"mergeable": "MERGEABLE"})
    assert out == "pr_base_undetermined"
    fresh = await store.get_task(t.id)
    ctx = fresh.context
    assert ctx["pr_base_freshness"]["state"] == delivered_base.UNDETERMINED
    # Backfilled via `git merge-base` against the resolved trunk tip — the
    # actual commit the (here, default `main`) PR branch forked from — so
    # future ticks have something honest to compare against, but the verdict
    # for THIS tick stays undetermined, never fresh. `pr_branch` defaults to
    # "main" here, the same ref as `base_branch`, so the merge-base is the
    # trunk tip itself.
    assert ctx["pr_base_sha"] == trunk
    assert ctx["pr_base_sha_source"] == "merge_base"


async def test_undetermined_with_an_unresolvable_branch_does_not_repeat_forever(
        store, tmp_path):
    """A pre-existing PR (no recorded `pr_base_sha`) whose head branch is not
    locally resolvable (the normal state of a watcher checkout that has only
    ever fetched the base — see `_check_base_stale`'s own docstring) can
    never have `merge_base_sha` succeed. Trunk still resolves fine, so
    `measure()` answers UNDETERMINED with a real `observed_sha` on every
    tick. A second, back-to-back tick with no trunk movement must not
    re-write identical context or re-emit a second `pr_base_undetermined`
    event -- the debounce must hold even though no `pr_base_sha` was ever
    backfilled, exactly the population this bugfix targets."""
    work = _repo(tmp_path)
    # No landing at all: trunk stays put, so the *only* thing that could
    # legitimately trigger a second write/emit is the buggy debounce itself.
    t = await _pr_task(store, work, base_sha=None,
                        pr_branch="ghost-branch-never-created")
    events = []
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=events)

    first = await w._check_base_stale(t, "https://x/pull/9",
                                       {"mergeable": "MERGEABLE"})
    assert first == "pr_base_undetermined"
    assert len(events) == 1
    once = await store.get_task(t.id)
    assert "pr_base_sha" not in once.context  # merge-base never resolved

    second = await w._check_base_stale(once, "https://x/pull/9",
                                        {"mergeable": "MERGEABLE"})
    assert second is None, (
        "the first UNDETERMINED branch's debounce leaked for a "
        "no-recorded-sha PR whose branch merge-base cannot resolve")
    assert len(events) == 1


async def test_a_backfilled_record_survives_a_fresh_verdict(store, tmp_path):
    """BLOCKER 1's second defect, closed: once a pre-existing delivery's
    `pr_base_sha` has been backfilled via `merge_base_sha` (never via
    today's trunk tip — see the test above), a later verdict that the
    backfilled sha now equals the trunk tip must OVERWRITE the freshness
    record with the fresh verdict, never delete `pr_base_freshness`
    outright. Deleting it here would make the record permanently
    indistinguishable from a PR that was never measured at all — the exact
    defect this bugfix exists to close, for every PR that predates this
    feature."""
    work = _repo(tmp_path)
    # No further landing: the backfilled merge-base sha (== current trunk
    # tip, since `pr_branch` defaults to "main") stays equal to trunk on the
    # very next tick, producing a determined FRESH verdict.
    t = await _pr_task(store, work, base_sha=None)
    w = _watcher(store, mergeable="MERGEABLE")

    first = await w._check_base_stale(t, "https://x/pull/9", {"mergeable": "MERGEABLE"})
    assert first == "pr_base_undetermined"
    backfilled = await store.get_task(t.id)
    assert backfilled.context["pr_base_freshness"]["state"] == delivered_base.UNDETERMINED
    assert backfilled.context["pr_base_sha_source"] == "merge_base"

    second = await w._check_base_stale(backfilled, "https://x/pull/9",
                                        {"mergeable": "MERGEABLE"})
    assert second is None
    after = await store.get_task(t.id)
    ctx = after.context
    # Present and FRESH — never deleted.
    assert "pr_base_freshness" in ctx
    assert ctx["pr_base_freshness"]["state"] == delivered_base.FRESH
    assert ctx["pr_base_sha_source"] == "merge_base"


async def test_a_pr_delivered_before_the_change_keeps_a_usable_state_after_three_ticks(
        store, tmp_path):
    """Every PR open today predates `pr_base_sha` — the exact case BLOCKER 1
    closes. Three consecutive `_check_open_pr` ticks (no landing in between,
    `pr_branch` defaulting to "main" so the merge-base equals trunk tip) must
    settle on a determined, still-present `pr_base_freshness` — never get
    backfilled to the current tip on tick 1 only to have tick 2 or 3's FRESH
    verdict delete the record outright, which would make the task
    indistinguishable from one still never measured at all."""
    work = _repo(tmp_path)
    t = await _pr_task(store, work, base_sha=None)
    assert "pr_base_sha" not in t.context
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN")

    for _ in range(3):
        current = await store.get_task(t.id)
        await w._check_open_pr(current)

    fresh = await store.get_task(t.id)
    ctx = fresh.context or {}
    # A "usable staleness state": present, and a determined state (fresh or
    # stale) -- not silently absent again as if never measured.
    assert "pr_base_freshness" in ctx
    assert ctx["pr_base_freshness"]["state"] in (
        delivered_base.FRESH, delivered_base.STALE)
    assert ctx.get("pr_base_sha_source") == "merge_base"


async def test_the_watcher_acts_on_stale_but_mergeable_end_to_end(store, tmp_path):
    """The ACT this bugfix adds, demonstrated end to end through the real
    entry point (`tick()`, not `_check_base_stale` called directly) on a
    task in exactly the stale-but-mergeable state -- and through the one
    real consumer of that state, `api.models.merge_ready_for`, which must
    withhold a `True` merge-ready verdict once the tick has recorded STALE."""
    from no_human.api.models import merge_ready_for

    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    branch = "feature-e2e"
    _make_branch(work, branch)

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    sha = "c" * 40
    await store.merge_context(t.id, {"merge_policy": {sha: {"ready": True}}})
    attempts = [{"commit_sha": sha}]

    before = await store.get_task(t.id)
    assert merge_ready_for(before, attempts) is True

    _land(lander, "e2e.py")
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN")
    actions = await w.tick()
    assert (t.id, "pr_base_remeasured") in actions

    after = await store.get_task(t.id)
    assert after.context["pr_base_freshness"]["state"] == delivered_base.STALE
    assert merge_ready_for(after, attempts) is None
    # Nothing else about the task moved: still parked, no coder round.
    assert after.status is TaskStatus.AWAITING_APPROVAL
    assert "pr_conflict_rounds" not in (after.context or {})


async def test_remeasuring_never_consumes_an_attempt(store, tmp_path):
    """The action this bugfix takes -- emitting `pr_base_remeasured` -- must
    never itself be, or trigger, a coder attempt: no `send_back_feedback`,
    no attempt-count bump, no status change away from AWAITING_APPROVAL."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    branch = "feature-no-attempt"
    _make_branch(work, branch)
    _land(lander, "no_attempt.py")

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    before_attempts = len(await store.list_attempts(t.id)) \
        if hasattr(store, "list_attempts") else 0
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN")

    out = await w._check_open_pr(t)
    assert out == "pr_base_remeasured"

    fresh = await store.get_task(t.id)
    ctx = fresh.context or {}
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert not ctx.get("send_back_feedback")
    assert "pr_conflict_rounds" not in ctx
    if hasattr(store, "list_attempts"):
        after_attempts = len(await store.list_attempts(t.id))
        assert after_attempts == before_attempts


async def test_the_stale_remeasure_is_not_re_emitted_on_a_quiet_tick(store, tmp_path):
    """AC4's dedup-guard mutation-pin: once a STALE, textually-clean verdict
    has been recorded, a second tick with NOTHING new on trunk must be
    silent -- no repeated `pr_base_remeasured` event, no repeated
    `pr_base_remeasures` bump. Deleting the dedup guard in
    `_check_base_stale`'s STALE branch (the
    ``if ctx.get("pr_base_freshness") == freshness: return None`` line) makes
    this test fail: the second tick would re-emit the same event and double
    the remeasure count."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    branch = "feature-quiet"
    _make_branch(work, branch)
    _land(lander, "quiet.py")

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    events = []
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN", events=events)

    first = await w._check_open_pr(t)
    assert first == "pr_base_remeasured"
    once = await store.get_task(t.id)
    assert once.context["pr_base_remeasures"] == 1

    # A second tick, nothing new happened on trunk.
    second = await w._check_open_pr(once)
    assert second is None
    twice = await store.get_task(t.id)
    assert twice.context["pr_base_remeasures"] == 1
    assert sum(1 for k, _ in events if k == "pr_base_remeasured") == 1


async def test_measure_is_a_three_state_answer(tmp_path):
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    original = _trunk_sha(work)

    fresh = await delivered_base.measure(str(work), "main", original)
    assert fresh.state == delivered_base.FRESH

    new_sha = _land(lander, "c.py")
    stale = await delivered_base.measure(str(work), "main", original)
    assert stale.state == delivered_base.STALE
    assert stale.observed_sha == new_sha
    assert stale.recorded_sha == original

    undetermined_no_repo = await delivered_base.measure(None, "main", original)
    assert undetermined_no_repo.state == delivered_base.UNDETERMINED

    undetermined_bad_repo = await delivered_base.measure(
        str(tmp_path / "nope"), "main", original)
    assert undetermined_bad_repo.state == delivered_base.UNDETERMINED

    undetermined_no_sha = await delivered_base.measure(str(work), "main", None)
    assert undetermined_no_sha.state == delivered_base.UNDETERMINED


# ---------------------------------------------------------------------------
# The written fields must have a reader outside this rung. `pr_base_sha` /
# `pr_base_freshness` / `pr_base_remeasures` / `pr_base_ref` /
# `pr_base_sha_source` are written by `_check_base_stale` and by
# `delivered_base.record_at_delivery`, and grepping `src/` for a reader of any
# of them outside `blockers/wake.py` / `vcs/delivered_base.py` turns up
# nothing — a freshness answer nothing consumes cannot change any decision.
# `nh task show` now renders them (see `cli/commands.py:task_show`); this
# proves that reader is real, not merely present in source, by actually
# invoking the CLI command and asserting on its rendered output.
# ---------------------------------------------------------------------------

def _make_cli_runner(path, monkeypatch):
    class _Cfg:
        data: dict = {}
        db_path = path

        def get(self, key, default=None):
            return self.data.get(key, default)

    monkeypatch.setattr(cli_commands, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cli_commands, "assert_subscription_mode", lambda **kw: None)
    return CliRunner()


def test_task_show_renders_the_stale_base_freshness(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    recorded = "a" * 40
    observed = "b" * 40

    async def _seed():
        async with Store(db) as s:
            t = Task.new("stale-base-cli", repo_path="/tmp/repo")
            t.context = {
                "pr_watch": "https://code.example.com/dev/x/pull/9",
                "pr_base_ref": "main",
                "pr_base_sha": recorded,
                "pr_base_sha_source": "backfilled",
                "pr_base_remeasures": 2,
                "pr_base_freshness": delivered_base.BaseFreshness(
                    delivered_base.STALE, "main", recorded, observed,
                    "trunk moved past the recorded base",
                ).as_dict(),
            }
            await s.create_task(t)
            await s.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            return t.id

    tid = asyncio.run(_seed())
    runner = _make_cli_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "show", tid[:8]])
    assert result.exit_code == 0, result.output
    assert delivered_base.STALE in result.output, result.output
    assert "main" in result.output, result.output
    assert recorded[:8] in result.output, result.output
    assert observed[:8] in result.output, result.output
    assert "backfilled" in result.output, result.output
    assert "2 remeasure" in result.output, result.output


def test_task_show_is_silent_when_base_freshness_was_never_recorded(tmp_path, monkeypatch):
    # No PR-freshness fields at all (a task with no delivered PR, or one
    # delivered before this change) — `task show` must not fabricate a line
    # for a field that was never written.
    db = tmp_path / "test.db"

    async def _seed():
        async with Store(db) as s:
            t = Task.new("no-base-freshness", repo_path="/tmp/repo")
            await s.create_task(t)
            return t.id

    tid = asyncio.run(_seed())
    runner = _make_cli_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "show", tid[:8]])
    assert result.exit_code == 0, result.output
    assert "PR base" not in result.output, result.output
