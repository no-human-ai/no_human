"""Three follow-up pins for the stale-but-mergeable watcher (see
`tests/test_wake_base_stale.py` for the headline behavioral coverage and
`vcs/delivered_base.py`'s module docstring for the defect this whole feature
closes):

1. the per-cycle fetch budget genuinely bounds the SUM of base-fetch wall
   time across every parked PR in one tick, not just per-task;
2. no source text anywhere in this feature claims an amended acceptance
   criterion, a fixed/closed rung-set, or a behaviour the code does not
   perform;
3. `_check_base_stale` reads the base ref from the same key
   (``ctx["base_branch"]``) its sibling rung `_check_pr_conflict` does, never
   falling back to the separately-recorded, purely historical `pr_base_ref`.
"""
from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from no_human.vcs import delivered_base

from .test_wake_base_stale import (
    _land,
    _make_branch,
    _pr_task,
    _repo,
    _clone,
    _trunk_sha,
    _watcher,
)


# ---------------------------------------------------------------------------
# 1. the fetch budget bounds the whole tick, not one PR
# ---------------------------------------------------------------------------

async def test_the_per_cycle_base_fetch_budget_bounds_a_slow_network(
        store, tmp_path, monkeypatch):
    """`base_fetch_budget` bounds the SUM of base-fetch wall time spent
    re-measuring every parked, MERGEABLE PR in one `tick()` — not a
    per-task allowance — so a slow network cannot make the whole sweep
    exceed the watcher's poll interval no matter how many PRs are parked.

    Proven by making every `fetch_base_ref` call artificially slow (0.1s)
    and giving the watcher a budget that can only ever afford ONE such
    fetch, across three parked, otherwise-identical PRs. If the budget were
    per-task (or not enforced at all), all three fetches would run and the
    tick would take ~0.3s+; with a correctly-enforced shared budget at most
    one fetch happens and the tick finishes in a small fraction of that."""
    work = _repo(tmp_path)

    call_count = 0

    async def _slow_fetch(repo_path, base, *, timeout=None):
        nonlocal call_count
        call_count += 1
        time.sleep(0.1)  # simulates a slow network hop, not local git cost
        return True

    monkeypatch.setattr(delivered_base, "fetch_base_ref", _slow_fetch)

    for i in range(3):
        branch = f"feature-budget-{i}"
        _make_branch(work, branch)
        await _pr_task(
            store, work, base_sha=_trunk_sha(work), pr_branch=branch,
            url=f"https://x/pull/budget-{i}")

    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN")
    # Only enough allowance for roughly one slow fetch — never three.
    w.base_fetch_budget = timedelta(seconds=0.15)

    started = time.monotonic()
    await w.tick()
    elapsed = time.monotonic() - started

    assert call_count <= 2, (
        f"expected the shared per-tick budget to skip at least one of "
        f"three slow fetches, but {call_count} ran")
    assert elapsed < 0.3, (
        f"tick took {elapsed:.3f}s — the budget did not bound total fetch "
        f"time across the parked PRs it swept")


# ---------------------------------------------------------------------------
# 2. no source text claims an amendment this code does not perform
# ---------------------------------------------------------------------------

_SCANNED_FILES = [
    "src/no_human/blockers/wake.py",
    "src/no_human/vcs/delivered_base.py",
    "src/no_human/api/models.py",
    "tests/test_wake_base_stale.py",
    "tests/test_finalize_records_delivered_base.py",
    "tests/test_wake_base_stale_followups.py",
]

# Phrases a lazy or dishonest implementation of this feature might use to
# claim it satisfies less than what was actually asked for — an amended
# acceptance criterion, a closed/fixed set of ladder rungs, or narrowing
# "MERGEABLE" (the real signal this rung acts on) down to "MERGED" (a
# different, much narrower one). Deliberately specific strings, not bare
# "AC" or "closed set", which appear in this codebase for unrelated reasons
# (e.g. a compaction-counter comment elsewhere references a wholly
# different task's "AC2"). Fenced between the FORBIDDEN markers below so
# this file's own self-scan (see `_offending_phrases`) can exclude the
# literal list from the very check it defines, without which every run
# would trip on its own data.
# FORBIDDEN:BEGIN
_FORBIDDEN_PHRASES = [
    "acceptance-criteria amendment",
    "acceptance criteria amendment",
    "closed set of ladder rungs",
    "closed set of the ladder rungs",
    "the ladder rungs are a closed set",
    "acts only on merged",
    "supersedes the original acceptance",
    "amends acceptance criterion",
]
# FORBIDDEN:END


def test_no_source_text_asserts_an_acceptance_criteria_amendment():
    """Scans this feature's own source and test files for text that would
    claim the code does something it does not: narrows a resolved
    acceptance criterion, asserts the open-PR ladder is a closed/fixed set
    of rungs (`tick()`'s own docstring explicitly disclaims this — see
    `blockers/wake.py`), or otherwise amends what was actually agreed. A
    prior, failed attempt at this exact bugfix (task 9b6e928a / PR #319,
    left open as DO-NOT-LAND reference) is exactly the kind of history this
    guards against repeating."""
    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for rel in _SCANNED_FILES:
        text = (repo_root / rel).read_text(encoding="utf-8").lower()
        for phrase in _FORBIDDEN_PHRASES:
            if phrase in text:
                offenders.append(f"{rel}: {phrase!r}")
    assert not offenders, f"forbidden acceptance-criteria-amendment text found: {offenders}"


# ---------------------------------------------------------------------------
# 3. the rung reads the same base-ref key as its siblings
# ---------------------------------------------------------------------------

async def test_the_rung_measures_against_the_same_base_key_as_its_sibling_rungs(
        store, tmp_path):
    """`_check_base_stale` must resolve the base ref from ``ctx["base_branch"]``
    — the same key `_check_pr_conflict` uses — never from the separately
    recorded, purely-historical `pr_base_ref` (which only ever records what
    delivery measured against; see `delivered_base.record_at_delivery`).
    Proven with a `pr_base_ref` deliberately set to an unresolvable ref: if
    `_check_base_stale` fell back to it (``ctx.get("pr_base_ref") or
    base_branch``) instead of reading `base_branch` directly, `measure()`
    would fail to resolve the tip and answer UNDETERMINED against the wrong
    ref, not STALE against the real one."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")
    recorded = _trunk_sha(work)
    branch = "feature-basekey"
    _make_branch(work, branch)
    _land(lander, "basekey.py")

    t = await _pr_task(store, work, base_sha=recorded, pr_branch=branch)
    # A misleading, unresolvable `pr_base_ref` — if this rung used it as (or
    # ahead of) `base_branch`, the measurement would come back UNDETERMINED
    # against a ref that can never resolve, not STALE against "main".
    await store.merge_context(t.id, {"pr_base_ref": "does-not-exist-anywhere"})
    current = await store.get_task(t.id)

    w = _watcher(store, mergeable="MERGEABLE", merge_state="CLEAN")
    out = await w._check_base_stale(current, "https://x/pull/basekey",
                                     {"mergeable": "MERGEABLE"})

    assert out == "pr_base_remeasured", (
        "expected a STALE remeasure against the real base_branch ('main'); "
        "got %r — consistent with falling back to the unresolvable "
        "pr_base_ref instead" % (out,))
    fresh = await store.get_task(t.id)
    freshness = fresh.context["pr_base_freshness"]
    assert freshness["base_ref"] == "main"
