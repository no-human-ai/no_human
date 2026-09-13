"""F1 (review round on task 92e48491's refile): `slot_wait.pool_paused_text`
is a THIRD renderer of `paused_reason` — reached by `nh task show` via
`cli/commands.py:1673` -> `cli/pool_probe.py`'s field-renaming pass-through
of `/api/queue/health` (`paused_reason` -> `reason`, `paused_until` ->
`until`, `paused_profile` -> `profile`) -> `slot_wait.pool_paused_text`.

It was missed by the original fix (which corrected `cli/commands.py`'s `nh
status` path, `web/src/drainChip.js`, and `web/src/App.jsx`, but not this
one) and, before this fix, rendered a lost pool lease as
"pool paused — lease_lost cooldown, resumes unknown" — a false claim that
a permanent, restart-only failure would resolve on its own. It rendered any
other unrecognised reason the same way, with the reason's own name spliced
into a "cooldown"/"resumes" sentence that only "quota" and "infra" (which
this module has always understood) actually mean.

These tests call `pool_paused_text` directly — no CLI runner needed, since
the defect and the fix are entirely inside this one pure function.
"""
from __future__ import annotations

import pytest

from no_human.core import slot_wait


def test_quota_is_a_genuine_cooldown_with_a_resume_time_and_profile():
    text = slot_wait.pool_paused_text({
        "reason": "quota", "until": "2026-08-20T17:20:00+00:00", "profile": "personal2",
    })
    assert text == "pool paused — quota cooldown, resumes 2026-08-20T17:20:00+00:00 (personal2 profile)"


def test_infra_is_a_genuine_cooldown_with_a_resume_time():
    text = slot_wait.pool_paused_text({
        "reason": "infra", "until": "2026-08-20T17:20:00+00:00", "profile": None,
    })
    assert text == "pool paused — infra cooldown, resumes 2026-08-20T17:20:00+00:00"


def test_quota_with_no_until_falls_back_to_unknown_time_never_crashes():
    text = slot_wait.pool_paused_text({"reason": "quota"})
    assert text == "pool paused — quota cooldown, resumes unknown"


@pytest.mark.parametrize("pause", [
    {"reason": "lease_lost", "until": "2026-08-20T17:20:00+00:00", "profile": None},
    {"reason": "some_future_reason_nobody_wrote_a_branch_for", "until": "2026-08-20T17:20:00+00:00"},
    {"reason": None, "until": "2026-08-20T17:20:00+00:00"},
    {},
], ids=["lease_lost", "novel_reason", "none_reason", "empty_pause"])
def test_lease_lost_and_every_unrecognised_reason_never_reads_as_a_cooldown(pause):
    """`lease_lost` is permanent by design (no clearing site anywhere in
    `core/scheduler.py` — a restart is the only way back), and a reason this
    function has never heard of gets the exact same honest treatment: no
    "cooldown", no "resumes", and — the closed PR #251's exact bug — never
    the word "quota" for a reason that is not literally "quota"."""
    text = slot_wait.pool_paused_text(pause)
    assert "resumes" not in text, text
    assert "cooldown" not in text, text
    assert "quota" not in text, text


def test_lease_lost_names_a_restart_not_a_wait():
    text = slot_wait.pool_paused_text({"reason": "lease_lost", "until": "2026-08-20T17:20:00+00:00"})
    assert text == "pool paused — pool lease lost; restart required"


def test_a_reason_this_module_has_never_seen_names_itself_as_unrecognised():
    text = slot_wait.pool_paused_text({"reason": "some_future_reason_nobody_wrote_a_branch_for"})
    assert text == "pool paused — reason unrecognised (some_future_reason_nobody_wrote_a_branch_for)"


def test_a_missing_reason_renders_unknown_not_quota():
    text = slot_wait.pool_paused_text({"until": "2026-08-20T17:20:00+00:00"})
    assert text == "pool paused — reason unrecognised (unknown)"
