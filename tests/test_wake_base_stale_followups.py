"""Follow-ups to `tests/test_wake_base_stale.py` closing four gaps an
independent review found in that first round of the stale-but-mergeable-PR
bugfix (task 22c4ddf6 finding #3), each a genuine mutation survivor — a
behavior the fix code implements but nothing pinned, so silently breaking it
again would not fail any test:

1. `vcs.delivered_base.measure` discarded `fetch_ok`: a failed
   ``git fetch origin <base>`` left `resolve_trunk_tip` reading a local
   mirror of unknown age, and a match against `recorded_sha` was reported
   FRESH regardless — the exact "stays fresh forever on a broken fetch" shape
   this whole bugfix exists to close, just moved one level down.
2. The shared single `gh pr view` poll each tick (`_poll_mergeable`) is
   consumed by BOTH `_check_pr_conflict` and `_check_base_stale`; nothing
   asserted it is actually paid for once, not twice, and nothing pinned that
   an explicit `info=None` (the shared poll already failed) does not trigger
   a second, redundant poll inside `_check_pr_conflict`.
3. `_check_base_stale`'s FRESH branch returned early without clearing a
   `pr_base_freshness` a PREVIOUS tick had recorded — so a PR that went
   stale and then caught back up to a fresh trunk (a corrected fetch, or a
   later landing that happened to restore the same tip) kept rendering its
   old, now-wrong verdict forever through `cli/commands.py`'s `task_show`
   reader.
4. The UNDETERMINED backfill path wrote `pr_base_sha` but not `pr_base_ref`
   for pre-bugfix tasks that never recorded either — leaving `task_show`'s
   `tctx.get('pr_base_ref') or '?'` fallback showing `'?'` forever even after
   the sha itself got backfilled.

No test here asserts on the source text of the code under test.
"""
from __future__ import annotations

from no_human.blockers.wake import WakeWatcher, _INFO_UNSET
from no_human.core.task import TaskStatus
from no_human.vcs import delivered_base

from tests.test_wake_base_stale import (
    _clone,
    _land,
    _make_branch,
    _pr_task,
    _repo,
    _trunk_sha,
    _watcher,
)


# ---------------------------------------------------------------------------
# Finding 1: a failed fetch must never let a stale local mirror read as FRESH.
# ---------------------------------------------------------------------------

async def test_measure_does_not_report_fresh_when_the_fetch_failed(
    tmp_path, monkeypatch,
):
    """Real repo, real (successful) local resolution — only `fetch_base_ref`
    is faked to report failure, exactly modeling a network blip or an
    expired credential: the local tracking ref still happens to equal
    `recorded_sha` (it was never touched), but that equality was never
    reconfirmed against the real remote this call, so it must not be
    reported FRESH."""
    work = _repo(tmp_path)
    recorded = _trunk_sha(work)

    async def failing_fetch(repo_path, base):
        return False

    monkeypatch.setattr(delivered_base, "fetch_base_ref", failing_fetch)

    result = await delivered_base.measure(str(work), "main", recorded)
    assert result.state == delivered_base.UNDETERMINED
    assert result.state != delivered_base.FRESH
    assert "fetch" in result.reason.lower(), result.reason


async def test_measure_still_reports_stale_when_the_fetch_failed(
    tmp_path, monkeypatch,
):
    """The same failed-fetch shape, but the local mirror ALREADY disagrees
    with `recorded_sha` — a real trunk landing happened before this call, so
    the local ref proves trunk moved regardless of whether the latest fetch
    also succeeded. STALE stays a safe, actionable answer; only a FRESH
    conclusion needs a confirmed fetch."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    new_sha = _land(lander, "moved.py")
    # Bring the local tracking ref forward for real, THEN fake the fetch
    # itself as failed — the local mirror already reflects the move.
    from tests.test_wake_base_stale import _git
    _git(work, "fetch", "-q", "origin", "main")
    assert _trunk_sha(work) == new_sha

    async def failing_fetch(repo_path, base):
        return False

    monkeypatch.setattr(delivered_base, "fetch_base_ref", failing_fetch)

    result = await delivered_base.measure(str(work), "main", recorded)
    assert result.state == delivered_base.STALE
    assert result.observed_sha == new_sha


async def test_measure_reports_fresh_when_the_fetch_succeeded(tmp_path):
    """Positive control: an ACTUAL successful fetch against a genuinely fresh
    trunk still reports FRESH — Finding 1's fix must not turn every FRESH
    answer into UNDETERMINED, only the ones where the fetch itself failed."""
    work = _repo(tmp_path)
    recorded = _trunk_sha(work)
    result = await delivered_base.measure(str(work), "main", recorded)
    assert result.state == delivered_base.FRESH


# ---------------------------------------------------------------------------
# Finding 4: the shared poll is genuinely shared — paid for once per tick,
# and an explicitly-already-failed poll is never retried.
# ---------------------------------------------------------------------------

async def test_check_open_pr_polls_mergeable_exactly_once_per_tick(store, tmp_path):
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    branch = "feature-shared-poll"
    _make_branch(work, branch)
    # Trunk moves so `_check_base_stale` (which runs after the conflict rung
    # in `_check_open_pr`'s ladder) has something to act on too — both rungs
    # that consume `info` are genuinely exercised in the same tick.
    _land(lander, "shared_poll.py")

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)

    poll_calls: list[str] = []

    async def counting_pr_mergeable(url):
        poll_calls.append(url)
        return {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}

    w = WakeWatcher(store, {}, pr_mergeable=counting_pr_mergeable)

    out = await w._check_open_pr(t)

    assert out == "pr_base_remeasured", out
    assert len(poll_calls) == 1, poll_calls


async def test_conflict_rung_does_not_repoll_when_the_shared_poll_already_failed(
    store, tmp_path,
):
    """`info=None` means the caller's shared poll already ran and failed —
    the sentinel-default docstring for `_INFO_UNSET` says this rung "must not
    re-poll in that case". Before the fix this used a bare `None` default,
    so "not provided" and "provided but failed" were indistinguishable and a
    failed shared poll was quietly re-paid-for inside this rung too."""
    work = _repo(tmp_path)
    t = await _pr_task(store, work, base_sha=_trunk_sha(work))

    poll_calls: list[str] = []

    async def counting_pr_mergeable(url):
        poll_calls.append(url)
        return {"mergeable": "MERGEABLE"}

    w = WakeWatcher(store, {}, pr_mergeable=counting_pr_mergeable)

    out = await w._check_pr_conflict(
        t, "https://x/pull/9", "", branch="main", info=None)

    assert out is None
    assert poll_calls == [], poll_calls


async def test_conflict_rung_polls_for_itself_when_info_is_not_provided(
    store, tmp_path,
):
    """The other side of the same guard: a DIRECT caller that never passes
    `info` at all (every pre-bugfix caller, and this file's own other tests)
    must still get the rung's own poll — `_INFO_UNSET`, not `None`, is the
    "not provided" default."""
    work = _repo(tmp_path)
    t = await _pr_task(store, work, base_sha=_trunk_sha(work))

    poll_calls: list[str] = []

    async def counting_pr_mergeable(url):
        poll_calls.append(url)
        return {"mergeable": "MERGEABLE"}

    w = WakeWatcher(store, {}, pr_mergeable=counting_pr_mergeable)

    out = await w._check_pr_conflict(t, "https://x/pull/9", "", branch="main")

    assert out is None
    assert poll_calls == ["https://x/pull/9"], poll_calls


def test_info_unset_sentinel_is_not_none():
    # Cheap guard against a future "simplification" that collapses the
    # sentinel back to a bare `None` default, which is exactly the two tests
    # above's regression.
    assert _INFO_UNSET is not None


# ---------------------------------------------------------------------------
# Finding 5: the measure()-level UNDETERMINED dedup (distinct from the
# ghost-branch / local-re-verification-failed dedup already covered by
# `test_stale_pr_undetermined_answer_does_not_repeat_forever`) — this one
# fires BEFORE the local `conflicting_paths` re-check ever runs, straight off
# `delivered_base.measure`'s own UNDETERMINED answer (here: an unreadable
# repo).
# ---------------------------------------------------------------------------

async def test_measure_level_undetermined_answer_does_not_repeat_forever(
    store, tmp_path,
):
    broken_repo = tmp_path / "does_not_exist"
    t = await _pr_task(store, broken_repo, base_sha="deadbeef" * 5)
    events: list[tuple[str, str]] = []
    w = _watcher(store, mergeable="MERGEABLE", events=events)

    first = await w._check_base_stale(
        t, "https://x/pull/9", {"mergeable": "MERGEABLE"})
    assert first == "pr_base_undetermined"
    assert len(events) == 1

    t2 = await store.get_task(t.id)
    second = await w._check_base_stale(
        t2, "https://x/pull/9", {"mergeable": "MERGEABLE"})
    assert second is None, second
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Finding 6: FRESH must clear a stale `pr_base_freshness` a previous tick left
# behind, or `task_show` renders a permanently wrong verdict.
# ---------------------------------------------------------------------------

async def test_fresh_after_stale_clears_the_stale_freshness_record(store, tmp_path):
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    branch = "feature-fresh-after-stale"
    _make_branch(work, branch)
    new_sha = _land(lander, "went_stale.py")

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN")

    # Tick 1: trunk moved, recorded as stale.
    out1 = await w._check_base_stale(
        t, "https://x/pull/9", {"mergeable": "MERGEABLE"})
    assert out1 == "pr_base_remeasured"
    after_stale = await store.get_task(t.id)
    assert after_stale.context["pr_base_freshness"]["state"] == delivered_base.STALE

    # `recorded_sha` (the value `measure()` compares against) is advanced by
    # hand here to the now-current tip — simulating whatever future consumer
    # AC2' leaves this signal for (a human, or a later rung) confirming
    # semantic safety and bumping the base. `measure()` will now see
    # recorded == observed and answer FRESH again.
    await store.merge_context(t.id, {"pr_base_sha": new_sha})
    t2 = await store.get_task(t.id)

    out2 = await w._check_base_stale(
        t2, "https://x/pull/9", {"mergeable": "MERGEABLE"})
    assert out2 is None, out2
    after_fresh = await store.get_task(t.id)
    assert "pr_base_freshness" not in (after_fresh.context or {}), after_fresh.context


# ---------------------------------------------------------------------------
# Finding 7: the UNDETERMINED backfill path must also backfill `pr_base_ref`
# for a pre-bugfix task that recorded neither.
# ---------------------------------------------------------------------------

async def test_undetermined_backfill_also_backfills_pr_base_ref(store, tmp_path):
    work = _repo(tmp_path)
    trunk = _trunk_sha(work)
    t = await _pr_task(store, work, base_sha=None)
    ctx = t.context or {}
    assert "pr_base_sha" not in ctx
    assert "pr_base_ref" not in ctx
    w = _watcher(store, mergeable="MERGEABLE")

    out = await w._check_base_stale(
        t, "https://x/pull/9", {"mergeable": "MERGEABLE"})
    assert out == "pr_base_undetermined"

    fresh = await store.get_task(t.id)
    fctx = fresh.context or {}
    assert fctx.get("pr_base_sha") == trunk
    assert fctx.get("pr_base_ref") == "main", fctx
