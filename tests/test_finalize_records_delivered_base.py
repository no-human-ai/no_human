"""`core/orchestrator.py:_finalize` wires `vcs.delivered_base.record_at_delivery`
in at PR-open time; this file pins that helper's own fetch-before-resolve
behavior directly, the way `_finalize` relies on it working. See
`vcs/delivered_base.py`'s module docstring for the defect this closes and
`tests/test_wake_base_stale.py` for the watcher-side re-measure tests.
"""
from __future__ import annotations

from no_human.vcs import delivered_base

from .test_wake_base_stale import _clone, _land, _repo


async def test_record_at_delivery_fetches_before_resolving_the_tip(tmp_path):
    """`record_at_delivery` must fetch `origin/<base>` BEFORE resolving the
    tip it records — never trust whatever the local checkout's tracking ref
    already happened to have. Proven with a genuinely stale local mirror: a
    second, fully independent clone (`lander`) lands a new commit on trunk
    with zero interaction with `work` (the checkout under test), so `work`'s
    local `refs/remotes/origin/main` is provably behind the real remote tip
    until `record_at_delivery` itself fetches. Deleting the
    ``await fetch_base_ref(repo_path, base)`` call in `record_at_delivery`
    makes this test fail: the recorded sha would come back as the stale
    local tip `work` already knew about, not the new tip `lander` just
    pushed."""
    work = _repo(tmp_path)
    lander = _clone(tmp_path, work, "lander")

    # A genuine trunk landing `work` has never fetched.
    new_tip = _land(lander, "delivered.py")

    patch = await delivered_base.record_at_delivery(str(work), "main")

    assert patch == {"pr_base_sha": new_tip, "pr_base_ref": "main"}
