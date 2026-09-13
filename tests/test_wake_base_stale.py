"""A stale-but-mergeable PR is never re-measured or woken (bugfix, split from
task 22c4ddf6's finding #3). `core/orchestrator.py:_finalize` never recorded
the trunk tip a delivered PR was measured against, and
`blockers.wake.WakeWatcher._check_open_pr`'s ladder acts only on MERGED /
CLOSED / CONFLICTING / new-comments — a PR that stays MERGEABLE while trunk
moves past it matches none of those rungs, so it was never re-measured or
woken again short of a human touching it or a fresh coder attempt racing the
next landing.

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

import subprocess
from pathlib import Path

from no_human.blockers.wake import WakeWatcher
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
    # (blocking finding #2 of this round's send-back: it used to be
    # discarded with a bare `await self._check_base_stale(...)`) so a caller
    # driving the ladder end-to-end — `tick()`, and the `wake` CLI command
    # through it — sees that something happened, even though nothing here
    # resumes the task or consumes a coder attempt.
    assert out == "pr_base_remeasured"

    fresh = await store.get_task(t.id)
    ctx = fresh.context or {}
    # No human touched it, no coder attempt was consumed: status is exactly
    # where it was, and none of the coder-round bookkeeping fired.
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert "pr_conflict_rounds" not in ctx
    assert not ctx.get("send_back_feedback")
    # The re-measure happened, driven by the real ref — but AC2' forbids
    # recording "fresh" for a base `git merge-tree` only proved textually
    # clean, never semantically safe, so this stays STALE and `pr_base_sha`
    # is deliberately left unbumped: bumping it would make the very next
    # `measure()` see recorded == observed and answer FRESH forever,
    # destroying the only signal a later, more thorough consumer could act
    # on.
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
    # `pr_base_sha` stays at the originally recorded value — AC2' forbids
    # bumping it on a merely textually-clean re-verification (see the
    # `_check_base_stale` docstring's AC2' paragraph for why).
    assert ctx["pr_base_sha"] == recorded
    assert ctx["pr_base_freshness"]["state"] == delivered_base.STALE
    assert ctx["pr_base_freshness"]["observed_sha"] == new_sha
    assert fresh.status is TaskStatus.AWAITING_APPROVAL


async def test_stale_pr_is_recorded_stale_not_fresh_on_a_semantic_break(
        store, tmp_path):
    """AC2''s planted case, reproduced against REAL git state (blocking
    finding #1 of this round's send-back): `git merge-tree` only proves
    TEXTUAL mergeability, never semantic safety. Trunk renames `mod.py` ->
    `mod_renamed.py`; the PR branch independently adds `caller.py` that
    still imports the old module name. The two changes touch disjoint
    paths, so merge-tree finds zero conflicting paths — textually clean —
    even though the merged result is broken (an import of a module that no
    longer exists once merged).

    Before this fix, an empty `conflicting_paths` result here was recorded
    as ``state: fresh`` and bumped `pr_base_sha` to the new tip, which would
    have made the very next `measure()` see recorded == observed and
    answered fresh forever — permanently destroying the only signal
    available to catch this. AC2' requires this be recorded `stale` (never
    `fresh`), with `pr_base_sha` left unbumped so the signal survives."""
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
    # question; a textually-clean answer is still only textual (AC2'), never
    # a license to bump the recorded sha.
    assert ctx["pr_base_sha"] == recorded
    assert ctx["pr_base_freshness"]["state"] == delivered_base.STALE
    assert ctx["pr_base_freshness"]["observed_sha"] == new_sha


async def test_stale_pr_with_missing_branch_is_undetermined_not_fresh(store, tmp_path):
    """Blocker-1 regression: `conflicting_paths` returning ``None`` (the
    question could not be asked at all — here because the recorded PR
    branch never existed anywhere, so even a fetch-and-retry cannot resolve
    it) must never be coerced into a determined-fresh answer. Before the
    fix, a bare ``if conflict_paths:`` treated ``None`` the same as an
    empty (verified-clean) set and recorded ``state: fresh`` with zero
    verification performed."""
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
    # Backfilled so future ticks have something to compare against, but the
    # verdict for THIS tick stays undetermined, never fresh.
    assert ctx["pr_base_sha"] == trunk
    assert ctx["pr_base_sha_source"] == "backfilled"


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
