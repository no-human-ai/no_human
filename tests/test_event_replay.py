"""Tests for `no_human.eval.event_replay` (no_human#422): replaying a
recorded attempt's events through the real `StuckDetector`.

Acceptance-criteria mapping (see PR description for the full text):
  1. `test_fixture_reproduces_hard_abort` + `test_wrong_repo_root_suppresses_the_fire`
  2. `test_mutated_threshold_loses_the_fire`
  3. covered separately by `tests/test_stuck_abort.py` + `tests/test_bounds.py`
     staying green after the `drive_stuck_detector` extraction (cited in the
     PR description, not re-tested here).
  4. `test_classify_outcome_*`
  5. `test_windows_more_than_a_minute_before_started_at`,
     `test_windows_survive_counter_restart_after_resume`,
     `test_load_attempt_events_windows_two_attempts_separately`
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from no_human.core.bounds import StuckDetector
from no_human.eval import event_replay as er
from tests._event_replay_fixture import TASK_TITLE

FIXTURE_DB = Path(__file__).resolve().parent.parent / "testdata" / "event_replay_fixture.db"


def _first_attempt_id() -> str:
    """Look up the fixture's own attempt-1 id directly from the checked-in
    DB rather than hardcoding it — `Task.new`/`create_attempt` assign random
    ids, so a hardcoded id would silently drift stale the next time
    `tests/_event_replay_fixture.py` regenerates the fixture."""
    con = sqlite3.connect(str(FIXTURE_DB))
    try:
        row = con.execute(
            "SELECT attempts.id FROM attempts JOIN tasks ON tasks.id = attempts.task_id "
            "WHERE tasks.title = ? AND attempts.attempt_number = 1",
            (TASK_TITLE,),
        ).fetchone()
    finally:
        con.close()
    assert row is not None, f"no attempt 1 found for task titled {TASK_TITLE!r}"
    return row[0]


FIXTURE_ATTEMPT_ID = _first_attempt_id()


# ---------------------------------------------------------------------------
# Acceptance criterion 1 + 2: the checked-in fixture.
# ---------------------------------------------------------------------------

def test_fixture_reproduces_hard_abort():
    """`testdata/event_replay_fixture.db` holds a real recorded attempt's
    events (anonymized; see `tests/_event_replay_fixture.py` for provenance)
    that hard-aborted on an edit-loop. Replaying it through today's default
    `StuckDetector`, via the production entry point (`drive_stuck_detector`,
    imported by `event_replay`, never reimplemented), must reproduce that
    fire."""
    attempt = er.load_attempt_events(str(FIXTURE_DB), FIXTURE_ATTEMPT_ID)
    assert attempt.task_id
    assert attempt.events, "fixture attempt has no windowed events"

    fires = er.replay_fires(attempt.events, StuckDetector(), repo_root=attempt.repo_root)
    assert "hard-abort:edit-loop" in fires
    assert "advisory:edit-loop" in fires


def test_wrong_repo_root_suppresses_the_fire():
    """TRAP 2, made concrete: if `repo_root` is wrongly taken to be
    `tasks.repo_path` instead of the recovered worktree root, every edit in
    the fixture reads as agent-owned (`.no_human` is a path component of the
    worktree-style edit paths) and the hard-abort never fires. This is
    exactly the failure mode the module's TRAP 2 exists to avoid, and this
    fixture is built so it can be demonstrated directly."""
    attempt = er.load_attempt_events(str(FIXTURE_DB), FIXTURE_ATTEMPT_ID)
    # `attempt.repo_root` was recovered from the worktree prefix; confirm the
    # (synthetic) task repo_path is a genuinely different, wrong root before
    # using it to prove the suppression.
    wrong_root = "/workspace/example-repo"
    assert attempt.repo_root != wrong_root
    assert attempt.repo_root.startswith(wrong_root + "/.no_human/worktrees/")

    fires = er.replay_fires(attempt.events, StuckDetector(), repo_root=wrong_root)
    assert "hard-abort:edit-loop" not in fires
    assert "advisory:edit-loop" not in fires


def test_mutated_threshold_loses_the_fire():
    """Acceptance criterion 2: mutating the hard-tier threshold the fixture's
    recorded abort depends on (`edit_abort`) must make `diff` report the
    edit-loop hard-abort as a LOST fire — this is the exact regression class
    the task brief describes (a change to the edit-loop hard tier that once
    passed review despite silencing 20 of 26 recorded hard aborts)."""
    attempt = er.load_attempt_events(str(FIXTURE_DB), FIXTURE_ATTEMPT_ID)

    before = er.replay_fires(attempt.events, StuckDetector(), repo_root=attempt.repo_root)
    after = er.replay_fires(
        attempt.events,
        StuckDetector(edit_abort=1000, edit_ceiling=2000),
        repo_root=attempt.repo_root,
    )

    result = er.diff(before, after)
    assert result["lost"] == {"hard-abort:edit-loop"}
    assert result["new"] == set()


def test_mutated_progress_gate_also_loses_the_fire():
    """The same acceptance criterion, but isolating the progress-gated half
    of the edit-loop hard tier from the absolute-ceiling backstop: only
    `edit_abort` is raised here (`bounds.py`'s `hard_stuck_reason` compares
    it against `_hard_edit_counts`, the counter `record_edit` resets on an
    observed progress signal — see that field's comment). `edit_ceiling`
    (the raw, progress-blind backstop) is left at its default (30); the
    fixture's real recorded edit count (15) never reaches it, so the loss
    below is caused by the progress-gated tier alone, not by also disabling
    the ceiling — proving `edit_abort` on its own is "the progress gate"
    this criterion names, distinct from `test_mutated_threshold_loses_the_fire`
    above, which raises both knobs together."""
    attempt = er.load_attempt_events(str(FIXTURE_DB), FIXTURE_ATTEMPT_ID)

    before = er.replay_fires(attempt.events, StuckDetector(), repo_root=attempt.repo_root)
    after = er.replay_fires(
        attempt.events,
        StuckDetector(edit_abort=1000),
        repo_root=attempt.repo_root,
    )

    result = er.diff(before, after)
    assert result["lost"] == {"hard-abort:edit-loop"}
    assert result["new"] == set()


# ---------------------------------------------------------------------------
# Acceptance criterion 4: classify_outcome.
# ---------------------------------------------------------------------------

def _ev(kind: str, ts: float) -> dict:
    return {"kind": kind, "ts": ts}


def test_classify_outcome_quota_pause():
    events = [_ev("tool_use", 1.0), _ev("paused_quota", 2.0)]
    assert er.classify_outcome(events) == "paused_quota"


def test_classify_outcome_escalation():
    events = [_ev("tool_use", 1.0), _ev("escalated", 2.0)]
    assert er.classify_outcome(events) == "escalated"


def test_classify_outcome_commit():
    events = [_ev("tool_use", 1.0), _ev("commit", 2.0)]
    assert er.classify_outcome(events) == "committed"


def test_classify_outcome_blocked():
    events = [_ev("tool_use", 1.0), _ev("blocked", 2.0)]
    assert er.classify_outcome(events) == "blocked"


def test_classify_outcome_cancelled():
    events = [_ev("tool_use", 1.0), _ev("cancelled", 2.0)]
    assert er.classify_outcome(events) == "cancelled"


def test_classify_outcome_cancelled_hard_maps_to_cancelled():
    events = [_ev("tool_use", 1.0), _ev("cancelled_hard", 2.0)]
    assert er.classify_outcome(events) == "cancelled"


def test_classify_outcome_no_significant_event_dies():
    """A stream with no significant event (only tool activity, no
    paused_quota/escalated/blocked/cancelled/commit) classifies as
    ``died`` — the module's label for "the attempt just ended", which is
    what today's per-attempt metrics mislabel as 'interrupted: superseded'
    in the ~17-of-100 case the task brief cites."""
    events = [_ev("tool_use", 1.0), _ev("tool_result", 2.0), _ev("thinking", 3.0)]
    assert er.classify_outcome(events) == "died"


def test_classify_outcome_empty_stream_dies():
    assert er.classify_outcome([]) == "died"


def test_classify_outcome_takes_the_last_significant_event():
    """Not the first, not a count — the LAST significant event before the
    window ends, per the task brief's framing ("the last significant event
    before the next attempt")."""
    events = [
        _ev("tool_use", 1.0),
        _ev("paused_quota", 2.0),
        _ev("tool_use", 3.0),
        _ev("commit", 4.0),
    ]
    assert er.classify_outcome(events) == "committed"


# ---------------------------------------------------------------------------
# Acceptance criterion 5: attempt windowing.
# ---------------------------------------------------------------------------

def test_windows_more_than_a_minute_before_started_at():
    """An `attempt_start` event whose `ts` precedes the attempt row's
    `started_at` by more than a minute must still anchor that attempt's
    window (the window start is the LAST qualifying `attempt_start`, not one
    clamped to a short tolerance)."""
    t1 = er._parse_started_at("2026-01-01 00:00:00")
    t2 = er._parse_started_at("2026-01-01 01:00:00")
    events = [
        {"kind": "attempt_start", "ts": t1 - 90.0},  # 90s before row 1's started_at
        {"kind": "attempt_start", "ts": t2 - 0.3},
    ]
    windows = er._build_attempt_windows(
        [("a1", "2026-01-01 00:00:00"), ("a2", "2026-01-01 01:00:00")], events
    )
    assert windows["a1"] == (t1 - 90.0, t2 - 0.3)
    assert windows["a2"] == (t2 - 0.3, None)


def test_windows_survive_counter_restart_after_resume():
    """The `n` counter embedded in an `attempt_start` event's text
    (``"attempt {n}/{max}"``) can restart after a resume (a real instance of
    this is in the fixture's own source task: attempt_number 3's recorded
    `attempt_start` text reads "attempt 1/3", not "3/3"). Windowing must
    never parse that text — only compare `ts` — or a resumed attempt's
    window collapses onto an earlier one."""
    t1 = er._parse_started_at("2026-01-01 00:00:00")
    t2 = er._parse_started_at("2026-01-02 00:00:00")  # one full day later
    events = [
        {"kind": "attempt_start", "text": "attempt 1/5", "ts": t1 + 0.1},
        {"kind": "attempt_start", "text": "attempt 1/5", "ts": t2 + 0.1},
    ]
    windows = er._build_attempt_windows(
        [("a1", "2026-01-01 00:00:00"), ("a2", "2026-01-02 00:00:00")], events
    )
    assert windows["a1"] == (t1 + 0.1, t2 + 0.1)
    assert windows["a2"][1] is None


def test_load_attempt_events_windows_two_attempts_separately():
    """End-to-end (via the checked-in fixture DB): the fixture's task has a
    second, later `attempt_start` event (the real recorded "attempt 2/3")
    immediately after the hard-abort. `load_attempt_events` for the FIRST
    attempt must not include any event at or after that second window's
    start."""
    attempt = er.load_attempt_events(str(FIXTURE_DB), FIXTURE_ATTEMPT_ID)
    # The fixture's own second attempt_start event (real recorded text
    # "attempt 2/3") marks where attempt 1's window ends.
    second_starts = [
        e["ts"] for e in attempt.events if e.get("kind") == "attempt_start"
    ]
    # Only the attempt's OWN attempt_start (the "attempt 1/3" one) should be
    # inside its window; the fixture's next attempt_start was recorded ~1.8s
    # after the hard-abort fired and must be excluded.
    assert len(second_starts) == 1


def test_load_attempt_events_unknown_attempt_raises():
    with pytest.raises(KeyError):
        er.load_attempt_events(str(FIXTURE_DB), "not-a-real-attempt-id")


# ---------------------------------------------------------------------------
# diff()
# ---------------------------------------------------------------------------

def test_diff_reports_lost_and_new():
    before = {"hard-abort:edit-loop", "advisory:doom-loop"}
    after = {"advisory:doom-loop", "hard-abort:ping-pong"}
    result = er.diff(before, after)
    assert result == {"lost": {"hard-abort:edit-loop"}, "new": {"hard-abort:ping-pong"}}


def test_diff_identical_sets_is_empty():
    fires = {"hard-abort:edit-loop"}
    assert er.diff(fires, set(fires)) == {"lost": set(), "new": set()}
