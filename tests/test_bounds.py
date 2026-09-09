"""Termination bounds + stuck detection (§3.5)."""

from datetime import datetime, timedelta, timezone

from no_human.core.bounds import (
    Bounds, ConvergenceTracker, QuotaExhausted, StuckDetector, error_signature,
    parse_quota_reset,
)
from no_human.core.pricing import weighted_tokens


def test_signature_stable_across_volatile_tokens():
    a = "Error at /Users/x/foo.py:42:7 ref 0x7ffabc id deadbeefcafe"
    b = "Error at /Users/y/bar.py:99:1 ref 0x1234ff id 0011223344ff"
    assert error_signature(a) == error_signature(b)


def test_signature_differs_on_real_change():
    a = "AssertionError: expected 1 got 2"
    b = "TypeError: cannot add str and int"
    assert error_signature(a) != error_signature(b)


def test_stuck_after_repeat():
    d = StuckDetector(threshold=2)
    assert d.record("AssertionError: expected 1 got 2") is False
    assert d.record("AssertionError: expected 1 got 2") is True  # same -> stuck


def test_not_stuck_on_progress():
    d = StuckDetector(threshold=2)
    assert d.record("AssertionError: expected 1 got 2") is False
    assert d.record("TypeError: different failure entirely") is False


def test_bounds_from_config_defaults():
    """Assert against the dataclass, not a literal: this test used to hardcode
    60, so it was a third copy of a default that already lived in two places."""
    b = Bounds.from_config(None)
    assert b == Bounds()
    assert b.max_attempts == Bounds().max_attempts
    assert b.max_turns_per_attempt == Bounds().max_turns_per_attempt


def test_bounds_from_config_override():
    b = Bounds.from_config({"max_attempts": 5, "max_turns_per_attempt": 10})
    assert b.max_attempts == 5
    assert b.max_turns_per_attempt == 10


def test_turns_for_simple_task_uses_base():
    b = Bounds.from_config(None)
    assert b.turns_for(complex_task=False) == b.max_turns_per_attempt


def test_turns_for_complex_task_scaled():
    b = Bounds.from_config(None)
    expected = int(b.max_turns_per_attempt * b.complex_multiplier)
    assert b.turns_for(complex_task=True) == expected
    assert expected > b.max_turns_per_attempt


def test_turns_for_multiplier_one_disables_bump():
    b = Bounds.from_config({"max_turns_per_attempt": 60, "complex_multiplier": 1.0})
    assert b.turns_for(complex_task=True) == 60


def test_complex_multiplier_from_config():
    b = Bounds.from_config({"max_turns_per_attempt": 40, "complex_multiplier": 2.0})
    assert b.turns_for(complex_task=True) == 80


# --- Phase 7e: doom-loop detection via tool-call signatures --- #

def test_doom_loop_after_three_identical():
    """Three consecutive identical tool calls → doom-loop fires."""
    d = StuckDetector(doom_loop_threshold=3)
    assert d.record_tool_call("Read", "/src/foo.py") is False
    assert d.record_tool_call("Read", "/src/foo.py") is False
    assert d.record_tool_call("Read", "/src/foo.py") is True  # 3rd → stuck


def test_doom_loop_not_triggered_by_interleaved():
    """Different calls interleaved should NOT trigger doom-loop."""
    d = StuckDetector(doom_loop_threshold=3)
    assert d.record_tool_call("Read", "/src/foo.py") is False
    assert d.record_tool_call("Edit", "/src/bar.py") is False
    assert d.record_tool_call("Read", "/src/foo.py") is False  # reset streak
    assert d.record_tool_call("Read", "/src/foo.py") is False  # only 2




def test_doom_loop_resets_on_different_call():
    d = StuckDetector(doom_loop_threshold=3)
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Grep", "pattern|/src")  # different → resets
    assert d.record_tool_call("Grep", "pattern|/src") is False  # only 2
    assert d.record_tool_call("Grep", "pattern|/src") is True   # 3rd → stuck


# --- R2.3 Layer 1: per-file edit-count loop detection --- #

def test_edit_loop_after_threshold():
    d = StuckDetector(edit_threshold=3)
    assert d.record_edit("/src/foo.py") is False
    assert d.record_edit("/src/foo.py") is False
    assert d.record_edit("/src/foo.py") is True  # 3rd edit → stuck


def test_edit_loop_different_files_dont_trigger():
    d = StuckDetector(edit_threshold=3)
    assert d.record_edit("/src/foo.py") is False
    assert d.record_edit("/src/bar.py") is False
    assert d.record_edit("/src/foo.py") is False  # foo.py only at 2


def test_edit_loop_reflected_in_stuck_reason():
    d = StuckDetector(edit_threshold=2)
    d.record_edit("/src/foo.py")
    d.record_edit("/src/foo.py")
    assert d.stuck_reason is not None
    assert "edit-loop" in d.stuck_reason
    assert "/src/foo.py" in d.stuck_reason


# --- R2.1: ping-pong (A-B-A-B) detection --- #

def test_ping_pong_detected_on_alternating_pattern():
    d = StuckDetector()
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Edit", "/b.py")
    d.record_tool_call("Read", "/a.py")
    assert d.detect_ping_pong() is False  # only 3 calls, need 4
    d.record_tool_call("Edit", "/b.py")
    assert d.detect_ping_pong() is True  # A-B-A-B


def test_ping_pong_not_detected_on_forward_progress():
    d = StuckDetector()
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Edit", "/b.py")
    d.record_tool_call("Read", "/c.py")
    d.record_tool_call("Edit", "/d.py")
    assert d.detect_ping_pong() is False


def test_ping_pong_reflected_in_stuck_reason():
    d = StuckDetector()
    for sig in [("Read", "/a.py"), ("Edit", "/b.py")] * 2:
        d.record_tool_call(*sig)
    assert d.stuck_reason is not None
    assert "ping-pong" in d.stuck_reason


def test_stuck_reason_prioritizes_doom_loop_over_others():
    """doom-loop (identical repeats) should win over a coincidental edit-loop."""
    d = StuckDetector(doom_loop_threshold=2, edit_threshold=2)
    d.record_edit("/a.py")
    d.record_edit("/a.py")  # edit-loop condition also true
    d.record_tool_call("Edit", "/a.py")
    d.record_tool_call("Edit", "/a.py")  # doom-loop condition also true
    assert d.stuck_reason is not None
    assert d.stuck_reason.startswith("doom-loop")


def test_stuck_reason_none_when_healthy():
    d = StuckDetector()
    d.record_tool_call("Read", "/a.py")
    assert d.stuck_reason is None


# ------------------- hard-abort thresholds (ARCH_REVIEW B2 #1) -------------- #
# Advisory thresholds emit telemetry; hard thresholds end the attempt. A
# recognized loop used to be allowed to burn the whole 500-turn budget (live
# precedent: 3.4M cache-read in 41 turns). The hard tier is deliberately far
# above the advisory tier so it only fires on unambiguous runaways.


def test_hard_stuck_none_below_doom_abort_threshold():
    d = StuckDetector()
    for _ in range(d.doom_loop_abort - 1):
        d.record_tool_call("Bash", "pytest -x")
    assert d.stuck_reason is not None  # advisory fired long ago
    assert d.hard_stuck_reason is None  # but no abort yet


def test_hard_stuck_doom_loop_at_abort_threshold():
    d = StuckDetector()
    for _ in range(d.doom_loop_abort):
        d.record_tool_call("Bash", "pytest -x")
    assert d.hard_stuck_reason is not None
    assert "doom-loop" in d.hard_stuck_reason


def test_hard_stuck_edit_loop_at_abort_threshold():
    d = StuckDetector()
    for _ in range(d.edit_abort - 1):
        d.record_edit("/a.py")
    assert d.hard_stuck_reason is None
    d.record_edit("/a.py")
    assert d.hard_stuck_reason is not None
    assert "edit-loop" in d.hard_stuck_reason


def test_hard_edit_count_resets_after_a_changed_test_outcome():
    """task f6e626fd: an edit followed by a test run whose outcome STATUS
    changed is progress — the hard-tier per-file count resets to 1, so the
    loop never crosses `edit_abort` no matter how many (edit, test) rounds
    it takes, as long as each test run's exit-status differs from the last.

    Send-back review round 2 (MAJOR-2/property 5): the two outcomes here
    deliberately have the SAME `result_chars`-free status shape between
    each other (alternating `exit 1` / `ok`) but a DIFFERENT status — output
    size alone is not exercised here at all, because size alone must never
    be what "progress" means (see `test_byte_size_alone_is_not_progress`
    for the negative case)."""
    d = StuckDetector()
    for i in range(20):
        d.record_edit("/a.py")
        d.note_test_run(f"call-{i}")
        status = "exit 1" if i % 2 == 0 else "ok"
        assert d.record_test_outcome(f"call-{i}", f"{status}, {i} chars") is True
        assert d.hard_stuck_reason is None


def test_byte_size_alone_is_not_progress():
    """Send-back review round 2 (MAJOR-2/property 5): two test-runner calls
    with the SAME exit status but a DIFFERENT `result_chars` are NOT
    progress — output size drifts on almost every real run (timestamps,
    log noise) with no change in outcome, so it cannot be what "progress"
    means. The hard tier must still fire at `edit_abort` when every
    (edit, test) round only ever changes the byte count."""
    d = StuckDetector()
    # +1: the very first test outcome is always "new" (nothing recorded yet)
    # and consumes one progress-reset, so it takes one extra round for the
    # per-file count to climb back up to `edit_abort` under pure byte noise.
    for i in range(d.edit_abort + 1):
        d.record_edit("/a.py")
        d.note_test_run(f"call-{i}")
        changed = d.record_test_outcome(f"call-{i}", f"exit 1, {100 + i} chars")
        assert changed is (i == 0)  # only the very first observation is "new"
    assert d.hard_stuck_reason is not None
    assert "edit-loop" in d.hard_stuck_reason


def test_identical_test_outcome_is_not_progress():
    """The counterpart: a test run whose outcome is byte-identical to the
    previous one is NOT progress — the hard tier still fires at
    `edit_abort`, and the reason text carries the last two summaries."""
    d = StuckDetector()
    d.record_edit("/a.py")
    d.note_test_run("call-0")
    assert d.record_test_outcome("call-0", "exit 1, 40 chars") is True  # first ever
    d.record_edit("/a.py")  # progress consumed here -> hard count resets to 1
    d.note_test_run("call-1")
    assert d.record_test_outcome("call-1", "exit 1, 40 chars") is False  # identical
    for _ in range(d.edit_abort - 2):
        d.record_edit("/a.py")
    assert d.hard_stuck_reason is None
    d.record_edit("/a.py")
    assert d.hard_stuck_reason is not None
    assert "edit-loop" in d.hard_stuck_reason
    assert "no observed progress" in d.hard_stuck_reason
    assert "exit 1, 40 chars" in d.hard_stuck_reason


def test_editing_a_different_file_in_between_is_not_progress():
    """Send-back review round 2 (MAJOR-1/property 3): editing a DIFFERENT
    file in between no longer resets a file's hard count on its own — an
    agent bouncing between two files with no observed outcome change is
    still a loop. (Previously this reset let exactly that escape both the
    hard tier and ping-pong; task a50b8b6c/3adc13f5 in the recorded corpus
    was saved from a real abort by nothing else.)"""
    d = StuckDetector()
    for _ in range(d.edit_abort - 1):
        d.record_edit("/a.py")
    assert d.hard_stuck_reason is None
    d.record_edit("/b.py")  # a different file in between — NOT progress for /a.py
    assert d.hard_stuck_reason is None  # /a.py is at edit_abort - 1, /b.py at 1
    d.record_edit("/a.py")
    assert d.hard_stuck_reason is not None
    assert "/a.py" in d.hard_stuck_reason


def test_ab_cross_file_loop_with_no_progress_signal_still_aborts():
    """Property 3's explicit driven test: an agent alternating between two
    files, with NO test-runner activity at all (so no progress signal ever
    fires), must still hard-abort — each file's own per-file count climbs
    independently and unconditionally now that cross-file editing is not
    treated as progress."""
    d = StuckDetector()
    aborted_at = None
    for i in range(40):
        d.record_edit("/a.py" if i % 2 == 0 else "/b.py")
        if d.hard_stuck_reason is not None:
            aborted_at = i
            break
    assert aborted_at is not None
    assert "edit-loop" in d.hard_stuck_reason
    assert "/a.py" in d.hard_stuck_reason  # /a.py lands on the even indices, hits first


def test_absolute_edit_ceiling_fires_even_under_sustained_progress():
    """Property 2: an ABSOLUTE per-file ceiling that no progress signal can
    lift. A file whose test outcome genuinely flips (real status
    transitions, not byte-size noise) every single round resets the
    progress-gated `edit_abort` tier every time — by design, since each
    round IS observed progress — but the raw, never-reset `_edit_counts`
    this class already keeps for the advisory tier keeps climbing, and
    `edit_ceiling` reads it unconditionally. (Recorded corpus attempts
    77f12aab/86f70e12, task c19a376c, showed exactly this sustained
    ok/failed flapping pattern on one file; this is the backstop that
    still bounds it if it runs long enough.)"""
    d = StuckDetector()
    aborted_at = None
    for i in range(60):
        d.record_edit("/flaky.py")
        d.note_test_run(f"c{i}")
        d.record_test_outcome(f"c{i}", "ok, 1 chars" if i % 2 == 0 else "failed, 1 chars")
        if d.hard_stuck_reason is not None:
            aborted_at = i
            break
    assert aborted_at is not None
    assert "absolute per-file ceiling" in d.hard_stuck_reason
    assert d._edit_counts["/flaky.py"] == d.edit_ceiling
    # The progress-gated tier never once crossed edit_abort — confirms the
    # ceiling, not the ordinary hard tier, is what fired.
    assert d._hard_edit_counts["/flaky.py"] < d.edit_abort


def test_unjoinable_test_result_is_not_progress():
    """A `tool_result` whose id was never registered by `note_test_run` (an
    unpaired backend, or one with no test-runner recognized) must not count
    as progress — preserves the pre-existing fallback behaviour."""
    d = StuckDetector()
    assert d.record_test_outcome("never-registered", "exit 1, 40 chars") is False
    for _ in range(d.edit_abort - 1):
        d.record_edit("/a.py")
    assert d.hard_stuck_reason is None
    d.record_edit("/a.py")
    assert d.hard_stuck_reason is not None


def test_advisory_edit_tier_still_reports_the_raw_count():
    """The advisory tier (`stuck_reason`, raw `_edit_counts`) must be totally
    unaffected by progress: it still fires purely on the raw per-file count,
    even while the hard tier keeps resetting on progress."""
    d = StuckDetector(edit_threshold=3)
    d.record_edit("/a.py")
    d.note_test_run("c0")
    d.record_test_outcome("c0", "exit 1, 1 chars")
    d.record_edit("/a.py")
    d.note_test_run("c1")
    d.record_test_outcome("c1", "exit 1, 2 chars")  # same status - not progress, but
    assert d.hard_stuck_reason is None  # far under edit_abort=15 either way
    assert d.record_edit("/a.py") is True  # advisory: raw count hit threshold=3
    assert d._edit_counts["/a.py"] == 3
    assert d.stuck_reason is not None
    assert "edit-loop" in d.stuck_reason


def test_hard_stuck_sustained_ping_pong():
    d = StuckDetector()
    for _ in range(5):  # 10 alternating calls — advisory, not abort
        d.record_tool_call("Read", "/a.py")
        d.record_tool_call("Edit", "/b.py")
    assert d.hard_stuck_reason is None
    d.record_tool_call("Read", "/a.py")
    d.record_tool_call("Edit", "/b.py")  # 12 alternating calls — sustained
    assert d.hard_stuck_reason is not None
    assert "ping-pong" in d.hard_stuck_reason


def test_hard_stuck_not_fooled_by_progress():
    d = StuckDetector()
    for i in range(30):
        d.record_tool_call("Read", f"/file{i}.py")
    assert d.hard_stuck_reason is None


def test_attempt_tokens_default_and_override():
    """Per-attempt spend cap (v6: four specs burned the whole lifetime budget
    in attempt #1). Default must clear the largest measured successful attempt
    (3.06M raw complex-tier cache-read = 306,000 cost-weighted) with headroom,
    and stay well under the lifetime cap so the bounded loop keeps at least two
    real attempts.

    Both caps are COST-WEIGHTED tokens since 2026-07-31 (core.pricing). Raised
    2026-08-03 (800k -> 2M with lifetime 1.6M -> 4M) from the honest-ledger
    sweep: the converted caps were calibrated on the pre-fix ledger whose
    subagent spend was under-counted — against honest numbers the old lifetime
    cap parked 52.9% of real tasks and the old attempt cap ended 31% of real
    attempts. Derivation and the re-sweep obligation live on core.bounds.Bounds."""
    b = Bounds()
    assert b.attempt_tokens == 2_000_000
    # The measured complex attempt (3.06M raw, ~all cache-read) in the cap's
    # own unit: 3_060_000 x CACHE_READ_WEIGHT.
    assert b.attempt_tokens > weighted_tokens(cache_read_tokens=3_060_000)
    assert b.attempt_tokens <= b.lifetime_tokens // 2
    assert Bounds.from_config({"attempt_tokens": 123}).attempt_tokens == 123


def test_investigation_overlay_keeps_base_caps():
    """Review D10: the investigation/design_doc bounds overlay must inherit
    the configured token caps, not silently revert them to dataclass
    defaults for exactly the kinds that produce reports."""
    base = {"attempt_tokens": 123, "lifetime_tokens": 456,
            "max_attempts": 3, "max_turns_per_attempt": 500}
    inv = {"max_attempts": 8, "max_turns_per_attempt": 80}
    merged = {**base, "max_attempts": inv["max_attempts"],
              "max_turns_per_attempt": inv["max_turns_per_attempt"]}
    b = Bounds.from_config(merged)
    assert b.max_attempts == 8
    assert b.max_turns_per_attempt == 80
    assert b.attempt_tokens == 123
    assert b.lifetime_tokens == 456


# ------- 2026-08-16 false doom-loop regression (ticket 32fae028) ----------- #
# Two healthy attempts were hard-aborted as "identical tool call repeated 9x"
# while window-reading one 2,805-line file: the Read signature was the path
# alone, and bounds' [:100] prefix truncation re-collapsed whatever the
# summarizer did distinguish behind a ~95-char worktree path. These tests run
# the REAL pipeline — _summarize_tool_sig output fed to record_tool_call —
# with incident-shaped inputs, in both directions (progress stays quiet, true
# repeats still fire).

from no_human.core.orchestrator import _summarize_tool_sig  # noqa: E402

# Same order of magnitude as the worktree paths in the incident (~95 chars).
_WT = ("/private/tmp/claude-501/some-project-slug/0000000000000000/scratchpad/"
       "train44/web/src/SlideOver.jsx")
assert len(_WT) > 90


def test_windowed_reads_of_one_long_path_file_are_progress():
    """The incident, replayed: nine different windows of one file must not
    fire the detector even though the path eats the whole truncation head."""
    d = StuckDetector(doom_loop_threshold=3)
    for i, offset in enumerate([1, 200, 415, 600, 800, 1000, 1400, 2000, 2400]):
        sig = _summarize_tool_sig("Read", {"file_path": _WT,
                                           "offset": offset, "limit": 200})
        assert d.record_tool_call("Read", sig) is False, f"fired at window {i}"


def test_rereading_the_same_window_is_still_a_loop():
    d = StuckDetector(doom_loop_threshold=3)
    sig = _summarize_tool_sig("Read", {"file_path": _WT,
                                       "offset": 415, "limit": 200})
    assert d.record_tool_call("Read", sig) is False
    assert d.record_tool_call("Read", sig) is False
    assert d.record_tool_call("Read", sig) is True  # 3rd identical → stuck


def test_different_edits_to_one_file_are_progress():
    d = StuckDetector(doom_loop_threshold=3)
    for i in range(9):
        sig = _summarize_tool_sig("Edit", {
            "file_path": _WT,
            "old_string": f"const before{i} = null",
            "new_string": f"const after{i} = null",
        })
        assert d.record_tool_call("Edit", sig) is False, f"fired at edit {i}"


def test_identical_edit_retried_is_still_a_loop():
    d = StuckDetector(doom_loop_threshold=3)
    inp = {"file_path": _WT, "old_string": "a", "new_string": "b"}
    assert d.record_tool_call("Edit", _summarize_tool_sig("Edit", inp)) is False
    assert d.record_tool_call("Edit", _summarize_tool_sig("Edit", inp)) is False
    assert d.record_tool_call("Edit", _summarize_tool_sig("Edit", inp)) is True


def test_distinct_bash_commands_sharing_a_long_prefix_are_progress():
    """Long worktree-path prefixes made distinct commands collide inside any
    prefix truncation; the trailing command hash must keep them apart."""
    d = StuckDetector(doom_loop_threshold=3)
    for target in ("tests/test_a.py", "tests/test_b.py", "tests/test_c.py"):
        sig = _summarize_tool_sig(
            "Bash", {"command": f"cd {_WT[:70]} && python -m pytest {target} -q"})
        assert d.record_tool_call("Bash", sig) is False


def test_identical_bash_command_retried_is_still_a_loop():
    d = StuckDetector(doom_loop_threshold=3)
    cmd = {"command": f"cd {_WT[:70]} && python -m pytest tests/test_a.py -q"}
    assert d.record_tool_call("Bash", _summarize_tool_sig("Bash", cmd)) is False
    assert d.record_tool_call("Bash", _summarize_tool_sig("Bash", cmd)) is False
    assert d.record_tool_call("Bash", _summarize_tool_sig("Bash", cmd)) is True


def test_pdf_page_walk_is_progress_and_replace_all_is_a_different_edit():
    """Review follow-ups: `pages` discriminates Read (a page-by-page PDF walk
    must not collapse to one signature), and `replace_all` discriminates Edit
    (a failed unique-match Edit retried with replace_all is a new action)."""
    d = StuckDetector(doom_loop_threshold=3)
    for pages in ("1-5", "6-10", "11-15"):
        sig = _summarize_tool_sig("Read", {"file_path": _WT, "pages": pages})
        assert d.record_tool_call("Read", sig) is False
    e1 = _summarize_tool_sig("Edit", {"file_path": _WT, "old_string": "a",
                                      "new_string": "b"})
    e2 = _summarize_tool_sig("Edit", {"file_path": _WT, "old_string": "a",
                                      "new_string": "b", "replace_all": True})
    assert e1 != e2


# --------------------------------------------------------------------------- #
# parse_quota_reset — the wall's own reset time, not a fixed hour            #
# --------------------------------------------------------------------------- #

def test_parse_quota_reset_short_form():
    now = datetime(2026, 8, 22, 1, 3, 55, tzinfo=timezone.utc)  # 04:03:55 Jerusalem
    r = parse_quota_reset(
        "You've hit your session limit · resets 4:20am (Asia/Jerusalem)",
        now=now)
    assert r == datetime(2026, 8, 22, 1, 20, 0, tzinfo=timezone.utc)


def test_parse_quota_reset_long_form_with_month_and_day():
    now = datetime(2026, 8, 22, 10, 0, 0, tzinfo=timezone.utc)  # 13:00 Jerusalem
    r = parse_quota_reset("resets Aug 22 at 2pm (Asia/Jerusalem)", now=now)
    assert r == datetime(2026, 8, 22, 11, 0, 0, tzinfo=timezone.utc)


def test_parse_quota_reset_bare_hour_no_minutes():
    now = datetime(2026, 8, 22, 1, 3, 55, tzinfo=timezone.utc)
    r = parse_quota_reset("resets 5am (Asia/Jerusalem)", now=now)
    assert r == datetime(2026, 8, 22, 2, 0, 0, tzinfo=timezone.utc)


def test_parse_quota_reset_midnight_and_noon():
    midnight_soon = datetime(2026, 8, 22, 20, 50, 0, tzinfo=timezone.utc)  # 23:50 Jerusalem
    assert parse_quota_reset("resets 12:00am (Asia/Jerusalem)", now=midnight_soon) == (
        datetime(2026, 8, 22, 21, 0, 0, tzinfo=timezone.utc))

    noon_soon = datetime(2026, 8, 22, 8, 50, 0, tzinfo=timezone.utc)  # 11:50 Jerusalem
    assert parse_quota_reset("resets 12:00pm (Asia/Jerusalem)", now=noon_soon) == (
        datetime(2026, 8, 22, 9, 0, 0, tzinfo=timezone.utc))


def test_parse_quota_reset_earlier_today_rolls_to_tomorrow():
    """23:50 local, message names 00:00 — already past today, so it must
    resolve to tomorrow's occurrence, not today's (already-passed) one."""
    now = datetime(2026, 8, 22, 20, 50, 0, tzinfo=timezone.utc)  # 23:50 Jerusalem
    r = parse_quota_reset("resets 12:00am (Asia/Jerusalem)", now=now)
    assert r is not None and r > now
    assert r == datetime(2026, 8, 22, 21, 0, 0, tzinfo=timezone.utc)


def test_parse_quota_reset_missing_zone_is_none():
    now = datetime(2026, 8, 22, 1, 3, 55, tzinfo=timezone.utc)
    assert parse_quota_reset("resets 2pm", now=now) is None


def test_parse_quota_reset_unknown_zone_is_none():
    now = datetime(2026, 8, 22, 1, 3, 55, tzinfo=timezone.utc)
    assert parse_quota_reset("resets 2pm (Not/AZone)", now=now) is None


def test_parse_quota_reset_unrecognized_text_is_none():
    now = datetime(2026, 8, 22, 1, 3, 55, tzinfo=timezone.utc)
    assert parse_quota_reset("garbage text with no reset in it", now=now) is None
    assert parse_quota_reset("", now=now) is None


def test_parse_quota_reset_invalid_hour_or_minute_is_none():
    now = datetime(2026, 8, 22, 1, 3, 55, tzinfo=timezone.utc)
    assert parse_quota_reset("resets 13pm (Asia/Jerusalem)", now=now) is None
    assert parse_quota_reset("resets 4:70am (Asia/Jerusalem)", now=now) is None


def test_parse_quota_reset_clamps_to_the_five_minute_floor():
    """A reset five seconds out is still worth a short wait, not an
    immediate re-park — clamped up to now+5min rather than trusted as-is."""
    now = datetime(2026, 8, 22, 1, 3, 55, tzinfo=timezone.utc)
    r = parse_quota_reset("resets 4:04am (Asia/Jerusalem)", now=now)
    assert r == now + timedelta(minutes=5)


def test_parse_quota_reset_beyond_six_hours_falls_back_to_none():
    """A parse landing days out is treated as wrong, not trusted — the caller
    (`QuotaExhausted`) falls back to the fixed RETRY_AFTER_S hour instead."""
    now = datetime(2026, 8, 22, 1, 3, 55, tzinfo=timezone.utc)
    r = parse_quota_reset("resets Aug 25 at 6pm (Asia/Jerusalem)", now=now)
    assert r is None


# --------------------------------------------------------------------------- #
# QuotaExhausted wiring — parse wins, fallback is unchanged                  #
# --------------------------------------------------------------------------- #

class _FixedNow(datetime):
    """A `datetime` subclass whose `.now()` always returns a fixed instant,
    swapped in for `bounds.datetime` so `QuotaExhausted.__init__` (which
    calls the real clock, not an injectable `now=`) is deterministic."""

    _fixed = datetime(2026, 8, 22, 1, 3, 55, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


def test_quota_exhausted_carries_the_walls_own_reset_time(monkeypatch):
    import no_human.core.bounds as bounds_mod
    monkeypatch.setattr(bounds_mod, "datetime", _FixedNow)

    exc = QuotaExhausted(
        "You've hit your session limit · resets 4:20am (Asia/Jerusalem)")

    assert exc.resets_at == "2026-08-22T01:20:00+00:00"


def test_quota_exhausted_falls_back_on_an_unparseable_message(monkeypatch):
    import no_human.core.bounds as bounds_mod
    monkeypatch.setattr(bounds_mod, "datetime", _FixedNow)

    exc = QuotaExhausted("You've hit your weekly limit")

    expected = (_FixedNow._fixed
                + timedelta(seconds=QuotaExhausted.RETRY_AFTER_S)).isoformat()
    assert exc.resets_at == expected


def test_quota_exhausted_explicit_resets_at_still_wins(monkeypatch):
    """A caller that already knows the reset time is never overridden by
    parsing the message."""
    import no_human.core.bounds as bounds_mod
    monkeypatch.setattr(bounds_mod, "datetime", _FixedNow)

    exc = QuotaExhausted(
        "resets 4:20am (Asia/Jerusalem)",
        resets_at="2030-01-01T00:00:00+00:00")

    assert exc.resets_at == "2030-01-01T00:00:00+00:00"


def test_quota_exhausted_beyond_six_hours_falls_back_not_days_out(monkeypatch):
    import no_human.core.bounds as bounds_mod
    monkeypatch.setattr(bounds_mod, "datetime", _FixedNow)

    exc = QuotaExhausted("resets Aug 25 at 6pm (Asia/Jerusalem)")

    expected = (_FixedNow._fixed
                + timedelta(seconds=QuotaExhausted.RETRY_AFTER_S)).isoformat()
    assert exc.resets_at == expected


# --------------------- ConvergenceTracker (P2) ------------------------- #


def test_convergence_from_config_defaults():
    c = ConvergenceTracker.from_config(None)
    assert c == ConvergenceTracker()
    assert c.enabled is True
    assert c.min_turns == 80
    assert c.window == 40


def test_convergence_from_config_override():
    c = ConvergenceTracker.from_config({
        "abort_non_converging": False,
        "convergence_check_after_turns": 10,
        "convergence_window_turns": 4,
    })
    assert c.enabled is False
    assert c.min_turns == 10
    assert c.window == 4


def test_convergence_silent_below_min_turns():
    c = ConvergenceTracker(min_turns=5, window=2)
    for _ in range(5):
        c.tick()
    assert c.non_converging_reason is None


def test_convergence_fires_past_min_turns_with_no_progress():
    c = ConvergenceTracker(min_turns=5, window=2)
    for _ in range(7):
        c.tick()
    reason = c.non_converging_reason
    assert reason is not None
    assert "no file edit or test run" in reason


def test_convergence_progress_resets_the_window():
    c = ConvergenceTracker(min_turns=5, window=3)
    for _ in range(6):
        c.tick()
    c.mark_progress()
    # Two more turns after the reset — still inside the window.
    c.tick()
    c.tick()
    assert c.non_converging_reason is None
    c.tick()
    assert c.non_converging_reason is not None


def test_convergence_disabled_never_fires():
    c = ConvergenceTracker(enabled=False, min_turns=1, window=1)
    for _ in range(50):
        c.tick()
    assert c.non_converging_reason is None


def test_convergence_from_config_cap_clamps_min_turns():
    """Round-2 review: `_REPORT_KINDS` tasks run with an 80-turn
    per-attempt cap; unclamped, the default `min_turns=80` sits almost
    exactly AT it. `cap` clamps `min_turns` to at most half of it."""
    c = ConvergenceTracker.from_config({}, cap=80)
    assert c.min_turns == 40
    # A normal 500-turn cap must leave the default untouched.
    assert ConvergenceTracker.from_config({}, cap=500).min_turns == 80
    # No cap at all (the common orchestrator call before this fix) is a no-op.
    assert ConvergenceTracker.from_config({}, cap=None).min_turns == 80


def test_convergence_from_config_cap_never_raises_an_already_small_override():
    """An operator override already below half the cap is never RAISED —
    the clamp is a ceiling, not a floor."""
    c = ConvergenceTracker.from_config(
        {"convergence_check_after_turns": 10}, cap=80)
    assert c.min_turns == 10


def test_convergence_from_config_bad_types_fall_back_to_defaults():
    """A malformed config value (an operator typo) must not raise — this is
    read EAGERLY at the top of every attempt, so a bare int() crashing here
    would kill every attempt on the task with an unrelated error, including
    with the feature turned off."""
    c = ConvergenceTracker.from_config({
        "convergence_check_after_turns": "eighty",
        "convergence_window_turns": None,
    })
    assert c.min_turns == 80
    assert c.window == 40
