"""Tests for the Part 22 blocker taxonomy, escalation report, and wake watcher."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from no_human.blockers import (
    Blocker,
    BlockerCategory,
    WakeWatcher,
    blocker_prompt_suffix,
    fallback_blocker,
    notification_line,
    parse_blocker,
    parse_duration,
    render_report,
    route_for,
    triage,
)
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# Taxonomy + routing                                                          #
# --------------------------------------------------------------------------- #

def test_category_coerce_aliases():
    assert BlockerCategory.coerce("spec_gap") is BlockerCategory.AMBIGUITY
    assert BlockerCategory.coerce("INVALID") is BlockerCategory.IMPOSSIBLE
    assert BlockerCategory.coerce("rate_limit") is BlockerCategory.TRANSIENT_INFRA
    assert BlockerCategory.coerce("AMBIGUITY/SPEC_GAP") is BlockerCategory.AMBIGUITY


def test_category_coerce_unknown_defaults_to_novel():
    assert BlockerCategory.coerce("banana") is BlockerCategory.NOVEL_UNKNOWN
    assert BlockerCategory.coerce("") is BlockerCategory.NOVEL_UNKNOWN


def test_missing_access_escalates_and_notifies_now():
    r = route_for(BlockerCategory.MISSING_ACCESS)
    assert r.target_status == TaskStatus.ESCALATED
    assert r.notify_now is True
    assert r.parked is False


def test_ambiguity_routes_to_awaiting_input():
    r = route_for(BlockerCategory.AMBIGUITY)
    assert r.target_status == TaskStatus.AWAITING_INPUT
    assert r.notify_now is True


def test_dependency_wait_is_parked_silent():
    r = route_for(BlockerCategory.DEPENDENCY_WAIT)
    assert r.target_status == TaskStatus.BLOCKED
    assert r.notify_now is False
    assert r.parked is True


def test_quota_parks_paused_quota():
    assert route_for(BlockerCategory.QUOTA).target_status == TaskStatus.PAUSED_QUOTA


def test_transient_infra_auto_retry_flag():
    assert route_for(BlockerCategory.TRANSIENT_INFRA).auto_retry is True


# --------------------------------------------------------------------------- #
# Triage with low-confidence override                                         #
# --------------------------------------------------------------------------- #

def test_low_confidence_parkable_escalates_instead_of_parking():
    # DEPENDENCY_WAIT normally parks, but low confidence => ask a human.
    b = Blocker(category=BlockerCategory.DEPENDENCY_WAIT, confidence=0.3,
                wake_condition="pr_merged:org/repo#1")
    route = triage(b, escalate_below_confidence=0.6)
    assert route.target_status == TaskStatus.ESCALATED
    assert route.notify_now is True


def test_high_confidence_parkable_still_parks():
    b = Blocker(category=BlockerCategory.DEPENDENCY_WAIT, confidence=0.9,
                wake_condition="pr_merged:org/repo#1")
    route = triage(b, escalate_below_confidence=0.6)
    assert route.target_status == TaskStatus.BLOCKED


def test_low_confidence_does_not_downgrade_an_escalation():
    # IMPOSSIBLE already escalates; low confidence shouldn't change that.
    b = Blocker(category=BlockerCategory.IMPOSSIBLE, confidence=0.1)
    route = triage(b)
    assert route.target_status == TaskStatus.ESCALATED


# --------------------------------------------------------------------------- #
# Blocker serialization                                                       #
# --------------------------------------------------------------------------- #

def test_blocker_roundtrip():
    b = Blocker(
        category=BlockerCategory.SCOPE_EXPLOSION,
        transient=False,
        confidence=0.8,
        tried=["a", "b"],
        question="Split into 2 tasks?",
        options=["yes", "no"],
        resume_branch="no-human/abc",
        resume_commit="deadbeef",
        goal="implement X",
        evidence="$ cmd\noutput",
    )
    restored = Blocker.from_dict(b.to_dict())
    assert restored.category is BlockerCategory.SCOPE_EXPLOSION
    assert restored.tried == ["a", "b"]
    assert [o.label for o in restored.options] == ["yes", "no"]
    assert restored.confidence == 0.8


# --------------------------------------------------------------------------- #
# Structured options + actions (D14)                                           #
# --------------------------------------------------------------------------- #

def test_options_normalise_from_strings_objects_and_old_rows():
    """Bare strings arrive from rows written before options were structured, and
    from every agent-raised blocker. They must not need a migration."""
    from no_human.blockers import BlockerOption

    b = Blocker.from_dict({
        "category": "SCOPE_EXPLOSION",
        "options": [
            "split into smaller tasks",  # an old row / an agent's answer
            {"label": "raise the limit", "action": {"set_task_config": {"max_lines_changed": 700}}},
        ],
    })
    assert all(isinstance(o, BlockerOption) for o in b.options)
    assert b.options[0].action is None
    assert b.options[1].action == {"set_task_config": {"max_lines_changed": 700}}
    # ...and survives a round trip through the tasks.blocker JSON column.
    again = Blocker.from_dict(b.to_dict())
    assert [o.label for o in again.options] == ["split into smaller tasks", "raise the limit"]
    assert again.options[1].action == {"set_task_config": {"max_lines_changed": 700}}


def test_the_agent_may_never_attach_an_action():
    """Constraint #5: an agent that could set task.config would resolve a blocker
    by raising the very limit that blocked it."""
    hostile = (
        "BLOCKER_JSON_START\n"
        '{"category": "SCOPE_EXPLOSION", "confidence": 0.9, '
        '"question": "may I?", '
        '"options": [{"label": "raise the limit", '
        '"action": {"set_task_config": {"max_lines_changed": 100000}}}]}\n'
        "BLOCKER_JSON_END"
    )
    b = parse_blocker(hostile)
    assert b is not None
    assert b.options[0].label == "raise the limit"  # the label survives
    assert b.options[0].action is None  # the action does not


def test_apply_action_writes_only_whitelisted_keys():
    from no_human.blockers import ActionError, apply_action
    from no_human.core.task import Task

    t = Task.new("x", repo_path="/tmp/x")
    assert apply_action(t, None) is None
    assert t.config == {}

    summary = apply_action(t, {"set_task_config": {"max_lines_changed": 700}})
    assert summary == "max_lines_changed=700"
    assert t.config["max_lines_changed"] == 700

    for bad in (
        {"set_task_config": {"never_push_to": []}},  # not whitelisted
        {"set_task_config": {"max_lines_changed": 0}},  # not positive
        {"set_task_config": {"max_lines_changed": "lots"}},  # not an integer
        {"set_task_config": {}},  # empty
        {"run_shell": {"cmd": "rm -rf /"}},  # unknown verb
        {"set_task_config": {"max_lines_changed": 800}, "run_shell": {}},  # smuggled
    ):
        with pytest.raises(ActionError):
            apply_action(t, bad)
    # Nothing partial was written by any rejected action.
    assert t.config == {"max_lines_changed": 700}


def test_attempt_tokens_is_settable():
    from no_human.blockers import apply_action
    from no_human.blockers.actions import ALLOWED_TASK_CONFIG_KEYS
    from no_human.core.task import Task

    assert "attempt_tokens" in ALLOWED_TASK_CONFIG_KEYS

    t = Task.new("x", repo_path="/tmp/x")
    summary = apply_action(t, {"set_task_config": {"attempt_tokens": 6_000_000}})
    assert summary == "attempt_tokens=6000000"
    assert t.config["attempt_tokens"] == 6_000_000


def test_apply_action_never_lowers_an_existing_cap():
    from no_human.blockers import apply_action
    from no_human.core.task import Task

    t = Task.new("x", repo_path="/tmp/x")
    # Both numbers in ONE unit — `budget_unit` says the stored cap is already
    # weighted. Never-lower is a comparison, and comparing a weighted request
    # against a pre-cutover RAW prior is not one; that cross-unit case has its
    # own test (test_lifetime_budget.py::
    # test_a_stale_raw_ceiling_is_correctable_and_does_not_become_permanent).
    t.config = {"lifetime_tokens": 16_000_000, "budget_unit": "weighted"}

    # A lower request keeps the existing (higher) cap and says so.
    summary = apply_action(t, {"set_task_config": {"lifetime_tokens": 8_000_000}})
    assert t.config["lifetime_tokens"] == 16_000_000
    assert "kept" in summary
    assert "16000000" in summary

    # A genuinely higher request still raises the cap.
    summary = apply_action(t, {"set_task_config": {"lifetime_tokens": 24_000_000}})
    assert t.config["lifetime_tokens"] == 24_000_000
    assert summary == "lifetime_tokens=24000000"

    # Same behaviour for a second cap key (attempt_tokens), proving it is
    # uniform across ALLOWED_TASK_CONFIG_KEYS, not special-cased.
    t.config["attempt_tokens"] = 8_000_000
    summary = apply_action(t, {"set_task_config": {"attempt_tokens": 5_000_000}})
    assert t.config["attempt_tokens"] == 8_000_000
    assert "kept" in summary
    assert "8000000" in summary


def test_apply_action_human_override_lowers_exactly():
    # SCRUM-44: human_override=True (the human CLI path) sets the exact
    # requested value, including lowering an existing cap. The default
    # (human_override=False, the blocker-option path) keeps never-lower.
    from no_human.blockers import apply_action
    from no_human.core.task import Task

    t = Task.new("x", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 16_000_000}

    summary = apply_action(
        t, {"set_task_config": {"lifetime_tokens": 8_000_000}}, human_override=True
    )
    assert t.config["lifetime_tokens"] == 8_000_000
    assert "kept" not in summary
    assert summary == "lifetime_tokens=8000000"

    # Without human_override (default), the same lowering request is rejected.
    summary = apply_action(t, {"set_task_config": {"lifetime_tokens": 4_000_000}})
    assert t.config["lifetime_tokens"] == 8_000_000
    assert "kept" in summary


# --------------------------------------------------------------------------- #
# Parsing the agent's structured emission                                     #
# --------------------------------------------------------------------------- #

def test_parse_blocker_from_text():
    text = """
    I cannot proceed without access.
    BLOCKER_JSON_START
    {"category": "MISSING_ACCESS", "confidence": 0.95,
     "question": "Grant repo write?", "root_cause_hypothesis": "token lacks scope"}
    BLOCKER_JSON_END
    """
    b = parse_blocker(text)
    assert b is not None
    assert b.category is BlockerCategory.MISSING_ACCESS
    assert b.question == "Grant repo write?"


def test_parse_blocker_absent_returns_none():
    assert parse_blocker("just some normal output") is None


def test_parse_blocker_malformed_returns_none():
    text = "BLOCKER_JSON_START\n{not valid json}\nBLOCKER_JSON_END"
    assert parse_blocker(text) is None


def test_fallback_blocker_is_novel_unknown():
    b = fallback_blocker("push failed", resume_branch="no-human/x", resume_commit="abc")
    assert b.category is BlockerCategory.NOVEL_UNKNOWN
    assert b.resume_branch == "no-human/x"
    assert b.question is not None


# --------------------------------------------------------------------------- #
# Report rendering (22.4 six-part)                                            #
# --------------------------------------------------------------------------- #

def test_render_report_has_six_sections():
    b = Blocker(
        category=BlockerCategory.AMBIGUITY, confidence=0.7,
        goal="map criterion 3", evidence="$ grep ...\nno match",
        root_cause_hypothesis="criterion 3 is contradictory",
        tried=["interpreted as A: failed", "interpreted as B: failed"],
        question="Which interpretation?", options=["A", "B"],
        resume_branch="no-human/abc123", resume_commit="cafebabe1234",
    )
    out = render_report(b, task_title="Fix login", task_id="abcdef123456")
    for heading in ["## 1. Goal", "## 2. What happened", "## 3. Why blocked",
                    "## 4. What I tried", "## 5. What I need from you",
                    "## 6. State & resume"]:
        assert heading in out
    assert "[1] A" in out and "[2] B" in out
    assert "WIP-BLOCKED" in out
    # Never a numeric self-score gate.
    assert "/10" not in out


def test_notification_line_is_actionable():
    b = Blocker(category=BlockerCategory.MISSING_ACCESS,
                question="Grant write to org/repo?")
    line = notification_line(b, task_title="T", task_id="abcdef12")
    assert "MISSING_ACCESS" in line
    assert "nh reply abcdef12" in line


def test_prompt_suffix_mentions_no_lowering_the_bar():
    s = blocker_prompt_suffix()
    assert "weakening a test" in s.lower() or "weaken" in s.lower()
    assert "BLOCKER_JSON_START" in s
    assert "/10" not in s  # never a numeric gate


# --------------------------------------------------------------------------- #
# Duration parsing                                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,seconds", [
    ("2h", 7200), ("30m", 1800), ("48h", 172800), ("1d", 86400),
    ("90s", 90), ("1h30m", 5400),
])
def test_parse_duration(text, seconds):
    d = parse_duration(text)
    assert d is not None and d.total_seconds() == seconds


def test_parse_duration_invalid():
    assert parse_duration("") is None
    assert parse_duration("soon") is None


# --------------------------------------------------------------------------- #
# Wake watcher                                                                #
# --------------------------------------------------------------------------- #


def _cfg(**over):
    base = {"blockers": {"max_park_duration": "48h"}}
    base["blockers"].update(over)
    return base


async def _park(store, *, status, blocker, updated_offset_hours=0, wake_at=None):
    t = Task.new("Parked task", repo_path="/tmp/r")
    await store.create_task(t)
    t.blocker = blocker
    t.wake_check_at = wake_at
    await store.update_task(t)
    await store.set_status(t, status, validate=False)
    return t


@pytest.mark.asyncio
async def test_after_duration_resumes(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(hours=3)).isoformat()
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                 "raised_at": raised, "confidence": 0.9},
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


@pytest.mark.asyncio
async def test_after_duration_not_yet(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(minutes=30)).isoformat()
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                 "raised_at": raised, "confidence": 0.9},
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert actions == []
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_quota_refreshed_resumes_on_wake_check_at(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.PAUSED_QUOTA,
        blocker={"category": "QUOTA", "wake_condition": "quota_refreshed",
                 "raised_at": (now - timedelta(hours=1)).isoformat(), "confidence": 1.0},
        wake_at=(now - timedelta(minutes=1)).isoformat(),
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions


@pytest.mark.asyncio
async def test_ci_green_checker_resumes(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "ci_green_on:main",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )

    async def ci_green(branch):
        return branch == "main"

    watcher = WakeWatcher(store, _cfg(), ci_green=ci_green)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions


class _FakeCIForDefaultChecker:
    """A minimal CIBackend double for exercising WakeWatcher's REAL default
    ci_green checker (not a test-injected one) — the `ci_from_config` call
    that checker makes is patched to return this instead of a real GitLabCI."""
    name = "fake"

    def __init__(self, result):
        self._result = result
        self.calls: list[str] = []

    async def trigger(self, branch, extra_variables=None):
        self.calls.append(branch)
        return self._result


@pytest.mark.asyncio
async def test_default_ci_green_checker_stays_parked_while_red(store, monkeypatch):
    """A7 red-first control: with NO ci_green injected (every WakeWatcher
    construction site used to pass none), the REAL default checker must
    rebuild the backend `_park_human_gated_ci` captured in
    `context["human_gated_ci"]["ci_conf"]` and, while it reports red, leave
    the task parked."""
    from no_human.ci.base import CIResult, PipelineStatus

    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT",
                 "wake_condition": "ci_green_on:no-human/gated-task",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )
    t.context = {"human_gated_ci": {
        "branch": "no-human/gated-task", "base": "main", "hint": "",
        "ci_conf": {"backend": "gitlab", "project": "grp/repo"},
    }}
    await store.update_task(t)

    red = _FakeCIForDefaultChecker(CIResult("1", "u", PipelineStatus.FAILED))
    monkeypatch.setattr("no_human.ci.ci_from_config", lambda cfg: red)

    watcher = WakeWatcher(store, _cfg())  # nothing injected — the real default runs
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED
    assert red.calls == ["no-human/gated-task"], "the real checker must have run"


@pytest.mark.asyncio
async def test_default_ci_green_checker_resumes_once_green(store, monkeypatch):
    """A7: the same setup as the red control, but the backend now reports
    green — the real default checker (still nothing injected) must resume
    the task. This is the false promise the audit named: before this fix,
    `ci_green_on:<branch>` could never fire for a profile-configured backend
    because the (dead) wiring only ever read the global `ci:` block."""
    from no_human.ci.base import CIResult, PipelineStatus

    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT",
                 "wake_condition": "ci_green_on:no-human/gated-task",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )
    t.context = {"human_gated_ci": {
        "branch": "no-human/gated-task", "base": "main", "hint": "",
        "ci_conf": {"backend": "gitlab", "project": "grp/repo"},
    }}
    await store.update_task(t)

    green = _FakeCIForDefaultChecker(CIResult("1", "u", PipelineStatus.SUCCESS))
    monkeypatch.setattr("no_human.ci.ci_from_config", lambda cfg: green)

    watcher = WakeWatcher(store, _cfg())  # nothing injected — the real default runs
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions
    assert green.calls == ["no-human/gated-task"]


@pytest.mark.asyncio
async def test_pr_merged_checker_resumes(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "pr_merged:org/repo#7",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )

    async def pr_merged(ref):
        return ref == "org/repo#7"

    watcher = WakeWatcher(store, _cfg(), pr_merged=pr_merged)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions


@pytest.mark.asyncio
async def test_timeout_escalates(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(hours=49)).isoformat()  # past 48h
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "pr_merged:org/repo#7",
                 "raised_at": raised, "confidence": 0.9},
    )

    async def pr_merged(ref):
        return False  # never merges

    watcher = WakeWatcher(store, _cfg(), pr_merged=pr_merged)
    actions = await watcher.tick(now=now)
    assert (t.id, "escalated_timeout") in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.ESCALATED
    assert refreshed.blocker["timed_out"] is True


@pytest.mark.asyncio
async def test_ci_terminal_checker_resumes_when_terminal(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT",
                 "wake_condition": "ci_terminal_on:12345",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )

    async def ci_terminal(pipeline_id):
        return (True, True)  # terminal + success

    watcher = WakeWatcher(store, _cfg(), ci_terminal=ci_terminal)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions


@pytest.mark.asyncio
async def test_ci_terminal_checker_not_terminal_yet(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT",
                 "wake_condition": "ci_terminal_on:12345",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )

    async def ci_terminal(pipeline_id):
        return (False, False)  # still running

    watcher = WakeWatcher(store, _cfg(), ci_terminal=ci_terminal)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_ci_terminal_checker_failure_safe(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT",
                 "wake_condition": "ci_terminal_on:12345",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )

    async def ci_terminal(pipeline_id):
        raise RuntimeError("API down")

    watcher = WakeWatcher(store, _cfg(), ci_terminal=ci_terminal)
    actions = await watcher.tick(now=now)
    # Checker failure → not satisfied, but also not crashed.
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_ci_terminal_no_checker_not_satisfied(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT",
                 "wake_condition": "ci_terminal_on:12345",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )

    # No ci_terminal checker wired → never satisfied.
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_awaiting_input_does_not_auto_resume(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.AWAITING_INPUT,
        blocker={"category": "AMBIGUITY", "wake_condition": "after:1h",
                 "raised_at": (now - timedelta(hours=2)).isoformat(), "confidence": 0.9},
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    # Not resumed by time — only a human reply resumes awaiting_input.
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.AWAITING_INPUT


@pytest.mark.asyncio
async def test_pr_comment_resumes_and_injects_feedback(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT",
                 "wake_condition": "pr_comment_on:org/repo#5",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )

    async def pr_comment(ref):
        return ["please rename the function"]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    fb = refreshed.context["send_back_feedback"]
    assert any("rename" in f["message"] for f in fb)
    assert refreshed.context["revision_rounds"] == 1


@pytest.mark.asyncio
async def test_pr_comment_revision_cap_escalates(store):
    """After max_revision_rounds autonomous comment→revise cycles, escalate (A2)."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT",
                 "wake_condition": "pr_comment_on:org/repo#5",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )
    # Already revised twice (the default cap); the next batch must escalate.
    t.context = {"revision_rounds": 2}
    await store.update_task(t)

    async def pr_comment(ref):
        return ["one more change"]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    actions = await watcher.tick(now=now)
    assert (t.id, "escalated_revisions") in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.ESCALATED
    assert refreshed.context["revision_rounds"] == 3


# --------------------------------------------------------------------------- #
# B4: auto PR-comment loop on AWAITING_APPROVAL                                #
# --------------------------------------------------------------------------- #

from no_human.vcs.pr_watcher import PrComment, parse_pr_url  # noqa: E402


async def _approval_task(store, *, since, ctx_extra=None):
    t = Task.new("PR task", repo_path="/tmp/r")
    await store.create_task(t)
    t.context = {"pr_watch": "https://code.example.com/o/r/pull/3",
                 "pr_comment_since": since, **(ctx_extra or {})}
    await store.update_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


@pytest.mark.asyncio
async def test_awaiting_approval_new_comment_triggers_revision(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _approval_task(store, since="2026-06-22T11:00:00+00:00")

    async def pr_comment(url):
        assert url == "https://code.example.com/o/r/pull/3"
        return [PrComment(author="human", body="please rename it",
                          created_at="2026-06-22T11:30:00+00:00")]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions
    r = await store.get_task(t.id)
    assert r.status == TaskStatus.IMPLEMENTING
    assert any("rename" in f["message"] for f in r.context["send_back_feedback"])
    assert r.context["pr_comment_since"] == "2026-06-22T11:30:00+00:00"
    assert r.context["revision_rounds"] == 1


@pytest.mark.asyncio
async def test_awaiting_approval_old_comment_does_not_retrigger(store):
    """A comment at/before the cursor must not cause a (duplicate) revision."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _approval_task(store, since="2026-06-22T11:30:00+00:00")

    async def pr_comment(url):
        return [PrComment(author="human", body="already handled",
                          created_at="2026-06-22T11:30:00+00:00")]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions
    r = await store.get_task(t.id)
    assert r.status == TaskStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_awaiting_approval_never_times_out(store):
    """An open PR waiting on a human must not escalate on max-park timeout."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _approval_task(store, since="2026-06-20T00:00:00+00:00")
    # Make it look very old.
    t.updated_at = (now - timedelta(hours=200)).isoformat()
    await store.update_task(t)

    async def pr_comment(url):
        return []  # no new comments

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    actions = await watcher.tick(now=now)
    assert actions == []
    r = await store.get_task(t.id)
    assert r.status == TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# restore-approval must clear the wake condition (live 2026-08-11 incident):  #
# a wake must never resume an awaiting_approval task.                        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_restored_awaiting_approval_is_disarmed_not_resumed(store):
    """A task sitting in `awaiting_approval` with a stale, still-armed wake
    condition (the shape `restore-approval` used to leave behind) must be
    disarmed, not resumed — no `pr_watch` here, so `_check_open_pr` would
    otherwise be the only thing standing between the wake-condition rung and
    a spurious resume."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.AWAITING_APPROVAL,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                 "raised_at": (now - timedelta(hours=3)).isoformat(),
                 "confidence": 0.9},
        wake_at=(now - timedelta(minutes=1)).isoformat(),
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.AWAITING_APPROVAL
    assert refreshed.wake_check_at is None
    assert (refreshed.blocker or {}).get("wake_condition") is None
    events = await store.list_events(t.id)
    kinds = [e.get("kind") for e in events]
    assert "wake_disarmed" in kinds
    assert "resumed" not in kinds


@pytest.mark.asyncio
async def test_stale_blocked_handle_restored_mid_tick_is_not_resumed(store):
    """The actual live race: `tick()` lists the task while it is still
    `blocked`; a concurrent `restore-approval` flips the DB row to
    `awaiting_approval` before `_evaluate` runs on that stale handle. The
    handle still reads `blocked` — deciding off it must not resume the task."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                 "raised_at": (now - timedelta(hours=3)).isoformat(),
                 "confidence": 0.9},
    )
    live = await store.get_task(t.id)
    await store.set_status(live, TaskStatus.AWAITING_APPROVAL, validate=False,
                            human_override=True)
    assert t.status == TaskStatus.BLOCKED  # confirms the handle stayed stale

    watcher = WakeWatcher(store, _cfg())
    action = await watcher._evaluate(t, now=now)
    assert action is None
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.AWAITING_APPROVAL
    events = await store.list_events(t.id)
    assert any(e.get("kind") == "wake_disarmed" for e in events)


@pytest.mark.asyncio
async def test_wake_disarm_is_emitted_once(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.AWAITING_APPROVAL,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                 "raised_at": (now - timedelta(hours=3)).isoformat(),
                 "confidence": 0.9},
        wake_at=(now - timedelta(minutes=1)).isoformat(),
    )
    watcher = WakeWatcher(store, _cfg())
    await watcher.tick(now=now)
    await watcher.tick(now=now)
    events = await store.list_events(t.id)
    disarm_events = [e for e in events if e.get("kind") == "wake_disarmed"]
    assert len(disarm_events) == 1


@pytest.mark.asyncio
async def test_genuinely_parked_task_still_resumes(store):
    """Regression pin: the disarm must never touch a task that is genuinely
    `blocked` — only a live `awaiting_approval`/`done` row is disarmed."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                 "raised_at": (now - timedelta(hours=3)).isoformat(),
                 "confidence": 0.9},
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


@pytest.mark.asyncio
async def test_awaiting_approval_pr_comment_resume_survives_the_disarm(store):
    """Regression pin: a genuinely `awaiting_approval` task with an open PR
    and a fresh human comment must still resume via
    `_check_approval_pr_comments` — the disarm belt must not swallow the
    legitimate awaiting-approval resume path."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _approval_task(store, since="2026-06-22T11:00:00+00:00")

    async def pr_comment(url):
        return [PrComment(author="human", body="please rename it",
                          created_at="2026-06-22T11:30:00+00:00")]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions
    r = await store.get_task(t.id)
    assert r.status == TaskStatus.IMPLEMENTING


@pytest.mark.asyncio
async def test_awaiting_approval_revision_cap_escalates(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _approval_task(store, since="2026-06-22T11:00:00+00:00",
                             ctx_extra={"revision_rounds": 2})

    async def pr_comment(url):
        return [PrComment(author="human", body="one more nit",
                          created_at="2026-06-22T11:45:00+00:00")]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    actions = await watcher.tick(now=now)
    assert (t.id, "escalated_revisions") in actions
    r = await store.get_task(t.id)
    assert r.status == TaskStatus.ESCALATED


def test_parse_pr_url():
    assert parse_pr_url("https://code.example.com/dev/test_ai_repo/pull/7") == (
        "github", "code.example.com", "dev/test_ai_repo", 7)
    assert parse_pr_url("https://github.com/o/r/pull/12")[0:1] == ("github",)
    gl = parse_pr_url("https://gitlab.com/grp/sub/repo/-/merge_requests/42")
    assert gl[0] == "gitlab" and gl[3] == 42 and "%2F" in gl[2]
    assert parse_pr_url("https://example.com/not-a-pr") is None


@pytest.mark.asyncio
async def test_the_REAL_quota_park_produces_a_task_the_watcher_can_resume(store):
    """Parks through `Orchestrator._park_quota` itself, not a hand-built
    fixture.

    Every other quota-wake test supplies `blocker={"wake_condition":
    "quota_refreshed", ...}` in its OWN fixture — a shape the real park path
    never wrote. `_park_quota` set `wake_check_at` and no blocker, and
    `wake.py` reads the condition off the blocker and short-circuits on a null
    one (`if not condition: return False`) BEFORE it ever looks at
    `wake_check_at`. So the task never auto-resumed, and PAUSED_QUOTA is not
    claimable either — it sat until the 48h park timeout escalated it. The
    fixtures asserted the contract; nothing asserted the wiring.
    """
    from no_human.blockers.wake import WakeWatcher
    from no_human.core.bounds import QuotaExhausted
    from no_human.core.orchestrator import Orchestrator

    class _Notifier:
        def notify(self, *a, **k): pass

    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store
    orch.notifier = _Notifier()
    orch.emit = lambda *a, **k: None

    task = Task.new("t", repo_path="/tmp/x")
    await store.create_task(task)
    # pending -> context is the only legal first hop; context can park.
    await store.set_status(task, TaskStatus.CONTEXT)

    outcome = await orch._park_quota(task, QuotaExhausted("You've hit your weekly limit"))
    assert outcome.status == TaskStatus.PAUSED_QUOTA

    parked = await store.get_task(task.id)
    assert parked.blocker, "no blocker => the watcher short-circuits, never resumes"
    assert parked.wake_check_at, "no wake time => nothing to be due"
    # The CLI's own reason reaches the parked task, so the board names the
    # wall instead of saying "quota exhausted".
    assert "weekly limit" in parked.blocker["root_cause_hypothesis"]

    # Tick the watcher PAST the due time. This is the assertion the fixture
    # tests could never make.
    due = datetime.fromisoformat(parked.wake_check_at) + timedelta(minutes=1)
    actions = await WakeWatcher(store, _cfg()).tick(now=due)
    assert (task.id, "resumed") in actions, actions

    # And it must NOT resume before it is due, or the pool thrashes.
    task2 = Task.new("t2", repo_path="/tmp/x")
    await store.create_task(task2)
    await store.set_status(task2, TaskStatus.CONTEXT)
    await orch._park_quota(task2, QuotaExhausted("weekly"))
    early = datetime.now(timezone.utc)
    assert (task2.id, "resumed") not in await WakeWatcher(store, _cfg()).tick(now=early)


def test_agent_cannot_claim_the_harness_only_budget_category():
    """BUDGET_EXHAUSTED is raised by the harness ledger, never the agent
    (taxonomy comment). An agent that emits it (live: SCRUM-20's coder, near
    its cap, self-declared BUDGET_EXHAUSTED with no options — a dead-end
    blocker the human cannot answer) is really reporting that the task is
    bigger than its budget: SCOPE_EXPLOSION, which routes to a human with the
    scope story intact."""
    text = (
        "cannot finish this within budget\n"
        "BLOCKER_JSON_START\n"
        '{"category": "BUDGET_EXHAUSTED", "goal": "g", '
        '"root_cause_hypothesis": "task needs 5 files", "confidence": 0.8}\n'
        "BLOCKER_JSON_END"
    )
    b = parse_blocker(text)
    assert b is not None
    assert b.category is BlockerCategory.SCOPE_EXPLOSION


def test_park_action_is_terminal_and_mutates_nothing():
    """SCRUM-22: the BUDGET_EXHAUSTED "stop" option carried no action, so
    nh reply resumed the task the human explicitly stopped (live: the stop
    landed the task straight back in the claim queue with an exhausted
    budget). A park action is a first-class terminal outcome."""
    from no_human.blockers import apply_action
    from no_human.blockers.actions import is_terminal_action
    from no_human.core.task import Task

    t = Task.new("x", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 8_000_000}
    summary = apply_action(t, {"park": True})
    assert "park" in (summary or "").lower()
    assert t.config == {"lifetime_tokens": 8_000_000}  # untouched

    assert is_terminal_action({"park": True}) is True
    assert is_terminal_action({"set_task_config": {"lifetime_tokens": 1}}) is False
    assert is_terminal_action(None) is False


def test_park_action_rejects_malformed_and_combined():
    from no_human.blockers import ActionError, apply_action
    from no_human.core.task import Task

    t = Task.new("x", repo_path="/tmp/x")
    with pytest.raises(ActionError):
        apply_action(t, {"park": False})
    with pytest.raises(ActionError):
        apply_action(t, {"park": True, "set_task_config": {"lifetime_tokens": 9}})


def test_budget_blocker_stop_option_carries_park():
    """The taxonomy-level contract: BUDGET_EXHAUSTED's second option must be
    an actionable terminal park, not a bare label."""
    import inspect

    from no_human.core import orchestrator as orch_mod

    src = inspect.getsource(orch_mod)
    # The stop option now carries the park action (source-level pin; the
    # behavioral end is covered by the apply/reply tests).
    assert '"park": True' in src or "'park': True" in src


@pytest.mark.asyncio
async def test_wake_sweep_never_touches_a_human_stopped_task(tmp_path):
    """Review 2026-07-25 (interaction bug): 'stop — keep parked' left the
    blocker intact, so the watcher's max_park branch re-escalated the task
    within 48h and any wake_condition resumed it — silently undoing the
    human's explicit stop. The human_stopped stamp must halt BOTH branches."""
    from datetime import datetime, timedelta, timezone

    from no_human.blockers.wake import WakeWatcher
    from no_human.core.db import Store
    from no_human.core.task import Task, TaskStatus

    store = await Store(tmp_path / "t.db").connect()
    try:
        t = Task.new("stopped", repo_path="/r")
        await store.create_task(t)
        t.blocker = {
            "category": "BUDGET_EXHAUSTED",
            "question": "?",
            "raised_at": "2026-01-01T00:00:00+00:00",  # far past max_park
            "wake_condition": "timeout:1s",            # would resume instantly
            "human_stopped": True,
        }
        await store.update_task_columns(t)
        await store.set_status(t, TaskStatus.ESCALATED, validate=False)

        w = WakeWatcher(store, {"blockers": {}})
        action = await w._evaluate(t, now=datetime.now(timezone.utc))
        assert action is None, f"human-stopped task must be untouchable, got {action!r}"
        fresh = await store.get_task(t.id)
        assert fresh.status is TaskStatus.ESCALATED
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# SCRUM-68: a terminal task (done/cancelled) must never be resumed             #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_done_task_with_human_merged_event_ignores_new_pr_comment(store):
    """Live incident 2026-07-26: a task was marked done via POST /shipped
    (human_merged event recorded), then the human who merged it posted a
    merge-notice comment on its PR. The pr_feedback rung counted
    that as '1 new PR comment(s)' and resumed the done task to implementing.
    Terminal is terminal — the sweep must do nothing."""
    t = Task.new("done-task", repo_path="/tmp/r")
    t.context = {"pr_watch": "https://code.example.com/o/r/pull/9"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    await store.set_status(t, TaskStatus.DONE, validate=False, event={
        "source": "human", "kind": "human_merged",
        "sha": "deadbeef", "note": "merged by hand", "ts": 0,
    })
    before = len(await store.list_events(t.id))

    async def pr_comment(url):
        return [PrComment(author="human", body="thanks for merging!",
                          created_at="2026-07-26T12:00:00+00:00")]

    w = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    actions = await w.tick(now=datetime.now(timezone.utc))
    assert actions == []
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert not (fresh.context or {}).get("send_back_feedback")
    assert len(await store.list_events(t.id)) == before


@pytest.mark.asyncio
async def test_cancelled_task_ignores_new_pr_comment(store):
    """A cancelled task (FAILED + cancel_reason — there is no separate
    'cancelled' status) must be treated exactly like done: untouchable."""
    t = Task.new("cancelled-task", repo_path="/tmp/r")
    t.context = {"pr_watch": "https://code.example.com/o/r/pull/9",
                 "cancel_reason": "superseded by a fresh run"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    await store.set_status(t, TaskStatus.FAILED, validate=False)
    before = len(await store.list_events(t.id))

    async def pr_comment(url):
        return [PrComment(author="human", body="please revive this",
                          created_at="2026-07-26T12:00:00+00:00")]

    w = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    actions = await w.tick(now=datetime.now(timezone.utc))
    assert actions == []
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert not (fresh.context or {}).get("send_back_feedback")
    assert len(await store.list_events(t.id)) == before


@pytest.mark.asyncio
async def test_evaluate_rechecks_current_db_status_not_the_stale_arg(store):
    """The exact race behind the live incident: the sweep tick fetched the
    task while it was AWAITING_APPROVAL (the in-memory `task` object handed
    to _evaluate), but a concurrent POST /shipped flipped it to done before
    this rung acted on it. The guard must re-read the STORE, not trust the
    caller's possibly-stale object."""
    t = Task.new("stale-arg", repo_path="/tmp/r")
    t.context = {"pr_watch": "https://code.example.com/o/r/pull/9"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    # `t` (held by the test, simulating the sweep's loop variable) still says
    # AWAITING_APPROVAL in memory — only the DB row is flipped to done, via a
    # second Task handle, exactly as a concurrent request would.
    t2 = await store.get_task(t.id)
    await store.set_status(t2, TaskStatus.DONE, validate=False, event={
        "source": "human", "kind": "human_merged", "sha": "abc", "ts": 0,
    })
    assert t.status is TaskStatus.AWAITING_APPROVAL  # confirms the staleness

    w = WakeWatcher(store, _cfg())
    action = await w._evaluate(t, now=datetime.now(timezone.utc))
    assert action is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE


@pytest.mark.asyncio
async def test_pr_feedback_rung_rechecks_terminal_after_the_network_poll(store):
    """Pins the mid-poll race directly: the comment poll itself is where a
    concurrent /shipped can land (it's the network await), so the recheck
    must happen AFTER the poll returns, using the comments it already
    fetched, not before."""
    t = Task.new("mid-poll", repo_path="/tmp/r")
    t.context = {"pr_watch": "https://code.example.com/o/r/pull/9"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    async def pr_comment(url):
        # Simulate a concurrent POST /shipped landing while this network call
        # is in flight.
        current = await store.get_task(t.id)
        await store.set_status(current, TaskStatus.DONE, validate=False, event={
            "source": "human", "kind": "human_merged", "sha": "abc", "ts": 0,
        })
        return [PrComment(author="human", body="great, thanks!",
                          created_at="2026-07-26T12:00:00+00:00")]

    w = WakeWatcher(store, _cfg(), pr_comment=pr_comment)
    out = await w._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert not (fresh.context or {}).get("send_back_feedback")


@pytest.mark.asyncio
async def test_state_rung_rechecks_terminal_after_the_pr_state_poll(store):
    """Reviewer finding: _check_open_pr's MERGED/CLOSED branch polled
    _pr_state and wrote DONE/ESCALATED with no post-poll terminal recheck. A
    concurrent POST /cancel landing during that poll must not be overwritten
    by a stale-read 'merged' verdict."""
    t = Task.new("state-race", repo_path="/tmp/r")
    t.context = {"pr_watch": "https://code.example.com/o/r/pull/9"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    async def pr_state(url):
        current = await store.get_task(t.id)
        # merge_context, not update_task_columns — the latter deliberately
        # never persists context (multi-writer discipline, db.py).
        await store.merge_context(t.id, {"cancel_reason": "cancelled mid-poll"})
        await store.set_status(current, TaskStatus.FAILED, validate=False)
        return "MERGED"

    events = []
    w = WakeWatcher(store, _cfg(), pr_state=pr_state,
                     on_event=lambda k, task: events.append(k))
    out = await w._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert fresh.context.get("cancel_reason") == "cancelled mid-poll"
    assert "merged" not in events


# --------------------------------------------------------------------------- #
# SCRUM-68 load-bearing guards: the WRITE HELPERS re-read the DB and refuse   #
# terminal tasks. The rung-level rechecks are early-outs; these direct-call   #
# tests pin the invariant every rung path funnels through.                    #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_resume_refuses_a_terminal_task(store):
    """Removing _resume's internal guard must fail this test: a stale handle
    (status blocked) whose DB row went done mid-race must not be resumed."""
    t = Task.new("resume-guard", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.BLOCKED, validate=False)
    stale = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    events = []
    w = WakeWatcher(store, _cfg(), on_event=lambda k, task: events.append(k))
    out = await w._resume(stale)
    assert out == "skipped_terminal"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert "resumed" not in events


@pytest.mark.asyncio
async def test_escalate_revisions_refuses_a_terminal_task(store):
    t = Task.new("rev-guard", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    stale = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    events = []
    w = WakeWatcher(store, _cfg(), on_event=lambda k, task: events.append(k))
    await w._escalate_revisions(stale, rounds=99)
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert "escalated_revisions" not in events


@pytest.mark.asyncio
async def test_escalate_timeout_refuses_a_terminal_task(store):
    t = Task.new("timeout-guard", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.BLOCKED, validate=False)
    stale = await store.get_task(t.id)
    # cancelled shape: FAILED + cancel_reason
    await store.merge_context(t.id, {"cancel_reason": "operator superseded"})
    await store.set_status(t, TaskStatus.FAILED, validate=False)
    events = []
    w = WakeWatcher(store, _cfg(), on_event=lambda k, task: events.append(k))
    await w._escalate_timeout(stale, None)
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert "escalated_timeout" not in events


@pytest.mark.asyncio
async def test_stall_watchdog_refuses_a_terminal_task(store):
    """A task shipped between the sweep's list fetch and the stall write must
    not be flipped to ESCALATED by the watchdog."""
    import time as _time
    t = Task.new("stall-guard", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    await store.save_events(t.id, [{
        "source": "agent", "kind": "tool_use", "text": "old",
        "ts": _time.time() - 7200,
    }])
    stale = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    events = []
    w = WakeWatcher(store, _cfg(stuck_active_minutes=30),
                    on_event=lambda k, task: events.append(k))
    out = await w._escalate_if_stalled(stale, now=datetime.now(timezone.utc))
    assert out is False
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert "escalated_stalled" not in events


@pytest.mark.asyncio
async def test_ci_rung_rechecks_terminal_after_the_log_fetch(store):
    """The CI rung's _ci_log fetch is a network await; a task shipped during
    it must see NO writes (no round counter, no escalation, no resume)."""
    t = Task.new("ci-log-race", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    async def pr_checks(url):
        return [{"name": "unit", "status": "fail", "link": "http://ci/1"}]

    async def ci_log(link):
        await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
        return "boom log"

    events = []
    w = WakeWatcher(store, _cfg(), pr_checks=pr_checks, ci_log=ci_log,
                    on_event=lambda k, task: events.append(k))
    out = await w._check_pr_ci(t, "https://code.example.com/o/r/pull/7")
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    assert (fresh.context or {}).get("pr_ci_rounds") is None
    assert events == []


@pytest.mark.asyncio
async def test_inject_pr_feedback_refuses_a_terminal_task(store):
    """A merge-notice comment fetched mid-race for a shipped task must not be
    injected as feedback (the exact SCRUM-68 incident shape)."""
    t = Task.new("feedback-race", repo_path="/tmp/r")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    async def pr_comment(ref):
        await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
        return [{"author": "human", "body": "merged this by hand, thanks"}]

    events = []
    w = WakeWatcher(store, _cfg(), pr_comment=pr_comment,
                    on_event=lambda k, task: events.append(k))
    out = await w._inject_pr_feedback(t, "pr_comment_on:https://x/pull/7")
    assert out is None
    fresh = await store.get_task(t.id)
    assert (fresh.context or {}).get("send_back_feedback") is None
    assert "pr_feedback" not in events


# --------------------------------------------------------------------------- #
# Death-blind resume loop guard: a machine-resumed attempt that dies before   #
# doing any work (0 priced tokens, <=1 turn) must back off (streak 1-2) and   #
# then park honestly (streak 3) instead of being resumed blindly forever.     #
# --------------------------------------------------------------------------- #

async def _machine_resumed(store, task, *, at, streak=0, attempt_ids=None):
    """Stamp the context state a real `_resume` call leaves behind for a
    MACHINE (wake-triggered) resume anchored at ``at`` — lets a test seed
    "the last resume was machine-driven" directly, the same shape
    `resume_provenance`/`_resume`'s own patch write, without going through
    the watcher."""
    await store.merge_context(task.id, {
        "resume_from": {"sha": None, "branch": None, "by": "wake"},
        "wake_dead_resumes": {
            "streak": streak,
            "attempt_ids": list(attempt_ids or []),
            "last_resume_at": at,
            "backoff_until": None,
        },
    })


async def _dead_attempt(store, task, attempt_number, *, turns=0, tokens=0):
    """A finished attempt row — dead by default (0 turns, 0 priced tokens),
    or one that did real work when given nonzero turns/tokens. Returns the
    attempt id."""
    attempt_id = await store.create_attempt(task.id, attempt_number)
    await store.update_attempt(
        attempt_id, status="failed", turns_used=turns, tokens_used=tokens)
    return attempt_id


async def _dispatched_then_died(store, task, n, *, wake_at=None):
    """What the orchestrator does after a re-dispatch: a fresh attempt row
    that dies (0 priced tokens, <=1 turn), then the task re-parks BLOCKED
    with its wake condition intact — the same shape `_park` builds a task
    with, but reusing the SAME row/id and never touching `wake_dead_resumes`
    (which `_resume`/`_backoff_dead_resume` already wrote and the next tick
    must read back unchanged)."""
    dead_id = await _dead_attempt(store, task, n, turns=0, tokens=0)
    task.wake_check_at = wake_at
    await store.update_task_columns(task)
    await store.set_status(task, TaskStatus.BLOCKED, validate=False)
    return dead_id


async def _dispatched_then_walled(store, task, n, *, wake_at=None):
    """Mirror of `_dispatched_then_died`, but the re-dispatch hits an
    ATTRIBUTED wall instead of dying blind: a fresh attempt row with a known
    cause (`infra_failure=1`, 1 turn, 0 priced tokens — the same shape as a
    quota park), then the task re-parks BLOCKED with its wake condition
    intact, never touching `wake_dead_resumes` (which the previous
    `_resume`/`_backoff_dead_resume` call already wrote and the next tick
    must read back unchanged)."""
    wall_id = await store.create_attempt(task.id, n)
    await store.update_attempt(
        wall_id, status="failed", turns_used=1, tokens_used=0,
        infra_failure=1,
        failure_reason="quota: You've hit your weekly limit · resets 6pm")
    task.wake_check_at = wake_at
    await store.update_task_columns(task)
    await store.set_status(task, TaskStatus.BLOCKED, validate=False)
    return wall_id


def _dead_resume_blocked_task_kwargs(now):
    return dict(
        status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                 "raised_at": (now - timedelta(hours=3)).isoformat(),
                 "confidence": 0.9},
    )


@pytest.mark.asyncio
async def test_dead_machine_resume_backs_off_instead_of_resuming(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(store, t, at=anchor)
    await _dead_attempt(store, t, 1, turns=0, tokens=0)

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "wake_backoff") in actions
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED
    assert refreshed.wake_check_at is not None
    pushed = datetime.fromisoformat(refreshed.wake_check_at)
    assert pushed == now + timedelta(minutes=10)
    dead_state = (refreshed.context or {}).get("wake_dead_resumes")
    assert dead_state["streak"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prior_streak,poll_interval,expected_delay",
    [
        (0, "10m", timedelta(minutes=10)),   # new streak 1: base * 2**0
        (1, "10m", timedelta(minutes=20)),   # new streak 2: base * 2**1 (doubled)
        (1, "4h", timedelta(hours=6)),       # new streak 2: 4h*2=8h -> capped at 6h
    ],
)
async def test_dead_resume_backoff_doubles_and_caps_at_six_hours(
    store, prior_streak, poll_interval, expected_delay,
):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    # `prior_ids` are already-counted dead rows from earlier rounds — deliberately
    # NOT backed by real attempt rows, since only their count (the prior streak)
    # matters here. `dead_id` is the row that's actually on the table now, and it
    # must be a DIFFERENT id from every `prior_ids` entry: under DEFECT 1's fix,
    # `_dead_resume_verdict` only advances the streak for attempt ids not already
    # in `attempt_ids` (a repeat evaluation of an already-known dead row now
    # returns "retry", not another "backoff" — see the docstring on
    # `_dead_resume_verdict`). Aliasing `dead_id` into the seeded `attempt_ids`
    # (as this test did pre-fix) would make it look already-counted and this
    # tick would retry instead of backing off, testing the very pattern the fix
    # removed instead of the doubling math this test is actually about.
    prior_ids = [f"prior-{i}" for i in range(prior_streak)]
    await _machine_resumed(
        store, t, at=anchor, streak=prior_streak, attempt_ids=prior_ids)
    dead_id = await _dead_attempt(store, t, 1, turns=0, tokens=0)
    assert dead_id not in prior_ids

    watcher = WakeWatcher(store, _cfg(wake_poll_interval=poll_interval))
    actions = await watcher.tick(now=now)

    assert (t.id, "wake_backoff") in actions
    refreshed = await store.get_task(t.id)
    pushed = datetime.fromisoformat(refreshed.wake_check_at)
    assert pushed == now + expected_delay


@pytest.mark.asyncio
async def test_dead_resume_backoff_emits_event_naming_the_streak(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    dead_id = await _dead_attempt(store, t, 1, turns=0, tokens=0)
    await _machine_resumed(store, t, at=anchor)

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    await watcher.tick(now=now)

    events = await store.list_events(t.id)
    backoff_events = [e for e in events if e.get("kind") == "wake_backoff"]
    assert len(backoff_events) == 1
    text = backoff_events[0]["text"]
    assert "streak #1/3" in text
    assert "died before doing work" in text
    assert dead_id in text


@pytest.mark.asyncio
async def test_an_expired_dead_resume_backoff_redispatches_instead_of_recounting(
    store,
):
    """Direct RED-on-main pin for DEFECT 1: a backoff window expiring with no
    NEW dead attempt since it was armed must dispatch ("retry"), not count the
    same already-known dead row as a second death. Pre-fix, this tick returned
    "backoff" again (streak 1 -> 2) purely because the timer fired — no new
    evidence, no new attempt, nothing tried since the first backoff."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(store, t, at=anchor)
    dead_id = await _dead_attempt(store, t, 1, turns=0, tokens=0)

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))

    actions = await watcher.tick(now=now)
    assert (t.id, "wake_backoff") in actions
    refreshed = await store.get_task(t.id)
    backoff_until = datetime.fromisoformat(
        refreshed.context["wake_dead_resumes"]["backoff_until"])

    later = backoff_until + timedelta(seconds=1)
    actions = await watcher.tick(now=later)
    assert (t.id, "resumed") in actions, actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    # The ladder is CARRIED, not reset — this is a re-dispatch of the same
    # rung, not a fresh streak.
    assert dead_state["streak"] == 1
    assert dead_state["attempt_ids"] == [dead_id]
    # RFC 7396 merge patch: writing `backoff_until: None` DELETES the key
    # from the stored blob rather than setting it to `null` — `.get()`, not
    # `[...]`, is the correct read (same idiom `_resume` itself uses).
    assert dead_state.get("backoff_until") is None
    # Re-anchored at THIS dispatch so the next evaluation only judges rows
    # started at/after it, not the already-counted one.
    assert dead_state["last_resume_at"] == datetime.fromisoformat(
        dead_state["last_resume_at"]).isoformat()
    assert datetime.fromisoformat(dead_state["last_resume_at"]) >= later


@pytest.mark.asyncio
async def test_third_dead_machine_resume_parks_with_an_honest_blocker(store):
    """Multi-cycle regression pin. The OLD argument — "streak 1/2 are
    deliberately never dispatched, so no new attempt row can appear between
    one evaluation and the next, so re-evaluating the same expired backoff
    window IS evidence of a repeat" — was the defect: a rung that never
    re-dispatches never actually RE-TRIES anything, so counting its expiry as
    a second death is counting a timer, not an attempt. On 2026-08-21 this
    produced exactly one real dead dispatch plus two bare timer ticks and
    escalated four tasks (2c8f23ff, 0986460c, f8de9cdf, e037008e) with "3
    consecutive dead machine resumes" naming the SAME single attempt id three
    times.

    The fix: an expired backoff window with no new dead attempt since it was
    armed returns "retry" — `_resume` DISPATCHES on it (carrying the ladder,
    re-anchoring the window), so the streak only ever advances when a
    genuinely NEW dead attempt row appears after that dispatch. This drives
    THREE real dead attempts through three full backoff-then-redispatch
    cycles to reach a park, so `streak == len(dead_attempt_ids)` by
    construction, not by coincidence."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(store, t, at=anchor)
    dead_id_1 = await _dead_attempt(store, t, 1, turns=0, tokens=0)

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))

    # Tick 1: streak 0 -> 1, backs off ~10m.
    actions = await watcher.tick(now=now)
    assert (t.id, "wake_backoff") in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED
    assert refreshed.context["wake_dead_resumes"]["streak"] == 1
    backoff_until_1 = datetime.fromisoformat(
        refreshed.context["wake_dead_resumes"]["backoff_until"])

    # Tick 2, still inside the first backoff window: no re-evaluation, no
    # second event, streak unchanged.
    mid = now + timedelta(minutes=1)
    actions = await watcher.tick(now=mid)
    assert (t.id, "wake_backoff") not in actions
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.context["wake_dead_resumes"]["streak"] == 1

    # Tick 3, past the first backoff window: the rung ENDS in a re-dispatch —
    # no new attempt row exists yet, so nothing was tried since the first
    # backoff, so this is "retry", not a second death.
    now2 = backoff_until_1 + timedelta(seconds=1)
    actions = await watcher.tick(now=now2)
    assert (t.id, "resumed") in actions, actions
    assert (t.id, "wake_backoff") not in actions
    assert (t.id, "parked_dead_resumes") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 1
    assert dead_state["attempt_ids"] == [dead_id_1]

    # That re-dispatch died too (0 priced tokens, <=1 turn) and the loop
    # parked the task again with the SAME wake condition — this is a
    # genuinely NEW dead attempt row, so it DOES advance the streak.
    dead_id_2 = await _dispatched_then_died(store, t, 2)

    actions = await watcher.tick(now=now2)
    assert (t.id, "wake_backoff") in actions, actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 2
    assert dead_state["attempt_ids"] == [dead_id_1, dead_id_2]
    backoff_until_2 = datetime.fromisoformat(dead_state["backoff_until"])
    assert backoff_until_2 > backoff_until_1
    assert backoff_until_2 - now2 == timedelta(minutes=20)

    # Past the second backoff window: another re-dispatch (retry), still no
    # NEW dead attempt yet, so streak stays 2.
    now3 = backoff_until_2 + timedelta(seconds=1)
    actions = await watcher.tick(now=now3)
    assert (t.id, "resumed") in actions, actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 2
    assert dead_state["attempt_ids"] == [dead_id_1, dead_id_2]

    # A THIRD genuinely new dead attempt: streak 2 -> 3, parks honestly
    # instead of resuming a 4th time.
    dead_id_3 = await _dispatched_then_died(store, t, 3)

    actions = await watcher.tick(now=now3)
    assert (t.id, "parked_dead_resumes") in actions
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.ESCALATED
    assert refreshed.blocker["dead_resume_streak"] == 3
    assert refreshed.blocker["dead_resume_attempt_ids"] == [
        dead_id_1, dead_id_2, dead_id_3,
    ]
    assert dead_id_1 in refreshed.blocker["question"]
    assert dead_id_2 in refreshed.blocker["question"]
    assert dead_id_3 in refreshed.blocker["question"]
    # The wake condition must never be silently dropped by the park.
    assert refreshed.blocker["wake_condition"] == "after:2h"

    # A further tick must not resume — the task already left the parked
    # statuses the watcher polls, and even if it hadn't, it must stay put.
    now4 = now3 + timedelta(hours=1)
    actions = await watcher.tick(now=now4)
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.ESCALATED


@pytest.mark.asyncio
async def test_escalation_names_one_attempt_id_per_streak_point(store):
    """The escalation text must be honest: exactly `streak` dead attempt ids,
    all of them named, and `len(dead_resume_attempt_ids) == dead_resume_streak`
    — the invariant that makes "3 consecutive dead machine resumes" true by
    construction rather than a coincidence of the old (buggy) counting."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(store, t, at=anchor)
    dead_id_1 = await _dead_attempt(store, t, 1, turns=0, tokens=0)

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))

    actions = await watcher.tick(now=now)
    assert (t.id, "wake_backoff") in actions
    refreshed = await store.get_task(t.id)
    backoff_until_1 = datetime.fromisoformat(
        refreshed.context["wake_dead_resumes"]["backoff_until"])

    now2 = backoff_until_1 + timedelta(seconds=1)
    await watcher.tick(now=now2)  # retry: re-dispatch, streak still 1
    dead_id_2 = await _dispatched_then_died(store, t, 2)
    actions = await watcher.tick(now=now2)
    assert (t.id, "wake_backoff") in actions
    refreshed = await store.get_task(t.id)
    backoff_until_2 = datetime.fromisoformat(
        refreshed.context["wake_dead_resumes"]["backoff_until"])

    now3 = backoff_until_2 + timedelta(seconds=1)
    await watcher.tick(now=now3)  # retry: re-dispatch, streak still 2
    dead_id_3 = await _dispatched_then_died(store, t, 3)
    actions = await watcher.tick(now=now3)
    assert (t.id, "parked_dead_resumes") in actions

    refreshed = await store.get_task(t.id)
    assert refreshed.blocker["dead_resume_streak"] == 3
    assert len(refreshed.blocker["dead_resume_attempt_ids"]) == \
        refreshed.blocker["dead_resume_streak"]

    events = await store.list_events(t.id)
    park_events = [e for e in events if e.get("kind") == "escalated_dead_resumes"]
    assert len(park_events) == 1
    text = park_events[0]["text"]
    assert "3 consecutive" in text
    for dead_id in (dead_id_1, dead_id_2, dead_id_3):
        assert dead_id in text


@pytest.mark.asyncio
async def test_alternating_dead_and_attributed_wall_still_reaches_the_park(store):
    """Found by the independent review of PR #596 (task f8efad06): #596
    re-anchors `last_resume_at` at every retry re-dispatch (correctly — that
    is what let the earlier 'timer tick counts as a death' defect be fixed),
    which means a previously-counted dead row falls out of scope the moment
    the loop re-dispatches into it. If the very next window then contains
    ONLY an attributed wall (no new dead attempt, but also no healthy one),
    the old code treated that as `"proceed"` and reset the ladder to streak
    0 / ids [] — so an environment alternating a death-blind dead dispatch
    with an attributed wall never reached the park; `max_park` (48h) was the
    only bound left. RED on main at step 3: main resets streak to 0 / ids []
    there instead of carrying [dead_id_1] forward."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(store, t, at=anchor)
    dead_id_1 = await _dead_attempt(store, t, 1, turns=0, tokens=0)
    # `create_attempt` stamps `started_at` off the REAL wall clock (db.py),
    # not this test's frozen `now` — but every retry re-dispatch below
    # re-anchors `last_resume_at` via `now_iso()`, which is ALSO the real
    # wall clock (wake.py). Left alone, dead_id_1's real creation instant
    # and step 2's real re-anchor instant can land in the same SQLite
    # second, and the intentional tie-inclusion at that boundary (the
    # `last_resume_floor` comment above) would then keep dead_id_1
    # "relevant" forever by accident — masking exactly the defect this
    # test exists to catch. Backdate it to the frozen `now` (2026-06-22),
    # unambiguously earlier than any real-wall-clock re-anchor by design,
    # so falling out of scope at step 3 is deterministic, not a wall-clock
    # race. It still satisfies step 1's window (`>= floor(anchor)`).
    await store.update_attempt(dead_id_1, started_at=now.isoformat())

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))

    # 1: dead dispatch #1 -> streak 0 -> 1, backs off.
    actions = await watcher.tick(now=now)
    assert (t.id, "wake_backoff") in actions, actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED
    assert refreshed.context["wake_dead_resumes"]["streak"] == 1
    backoff_until_1 = datetime.fromisoformat(
        refreshed.context["wake_dead_resumes"]["backoff_until"])

    # 2: past the backoff window -> retry-dispatch, streak still 1 (nothing
    # new tried yet since the backoff was armed), ids [dead_1].
    now2 = backoff_until_1 + timedelta(seconds=1)
    actions = await watcher.tick(now=now2)
    assert (t.id, "resumed") in actions, actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 1
    assert dead_state["attempt_ids"] == [dead_id_1]

    # 3: that re-dispatch hits an attributed wall #2 (quota), not a
    # death-blind failure. THE RED ASSERTION: the window since the retry
    # contains only the wall row, and main's `proceed` on empty `relevant`
    # wipes the streak-1 ladder here even though nothing healthy happened.
    await _dispatched_then_walled(store, t, 2)
    actions = await watcher.tick(now=now2)
    assert (t.id, "resumed") in actions, actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 1
    assert dead_state["attempt_ids"] == [dead_id_1]

    # 4: a genuinely new dead attempt #3 after that retry -> streak 1 -> 2.
    dead_id_3 = await _dispatched_then_died(store, t, 3)
    actions = await watcher.tick(now=now2)
    assert (t.id, "wake_backoff") in actions, actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 2
    assert dead_state["attempt_ids"] == [dead_id_1, dead_id_3]
    backoff_until_2 = datetime.fromisoformat(dead_state["backoff_until"])

    # 5: past window 2 -> retry-dispatch, streak still 2; that re-dispatch
    # hits attributed wall #4 -> still carried, not reset.
    now3 = backoff_until_2 + timedelta(seconds=1)
    actions = await watcher.tick(now=now3)
    assert (t.id, "resumed") in actions, actions
    refreshed = await store.get_task(t.id)
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 2
    assert dead_state["attempt_ids"] == [dead_id_1, dead_id_3]

    await _dispatched_then_walled(store, t, 4)
    actions = await watcher.tick(now=now3)
    assert (t.id, "resumed") in actions, actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 2
    assert dead_state["attempt_ids"] == [dead_id_1, dead_id_3]

    # 6: a THIRD genuinely new dead attempt #5 -> streak 2 -> 3, parks
    # honestly instead of resuming a 4th time.
    dead_id_5 = await _dispatched_then_died(store, t, 5)
    actions = await watcher.tick(now=now3)
    assert (t.id, "parked_dead_resumes") in actions, actions
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.ESCALATED
    assert refreshed.blocker["dead_resume_streak"] == 3
    assert refreshed.blocker["dead_resume_attempt_ids"] == [
        dead_id_1, dead_id_3, dead_id_5,
    ]
    assert dead_id_1 in refreshed.blocker["question"]
    assert dead_id_3 in refreshed.blocker["question"]
    assert dead_id_5 in refreshed.blocker["question"]
    # No wall attempt id ever named — the wall rows were never dead-resume
    # evidence, only carried-forward context for the existing ladder.
    # The wake condition must never be silently dropped by the park.
    assert refreshed.blocker["wake_condition"] == "after:2h"


@pytest.mark.asyncio
async def test_the_2c8f23ff_recorded_state_replays_as_proceed(store):
    """Built as an in-test fixture from the values quoted in the task
    description (2026-08-21 incident, task 2c8f23ff) — never read or point a
    test at the live `~/.no_human` DB. One attempt: turns_used=1, 0 priced
    tokens, infra_failure=1, failure_reason exactly as recorded, blocker
    wake_condition="quota_refreshed". This is an attributed wall death (AC1's
    exclusion), so the watcher must resume normally, not back off."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(
        store,
        status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "quota_refreshed",
            "raised_at": (now - timedelta(hours=3)).isoformat(),
            "confidence": 0.9,
        },
        # quota_refreshed is satisfied once wake_check_at passes — set it in
        # the past so this tick's condition check fires, same as a real
        # quota-park's recorded reset time.
        wake_at=now.isoformat(),
    )
    await _machine_resumed(store, t, at=anchor)
    wall_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        wall_id, status="failed", turns_used=1, tokens_used=0,
        infra_failure=1,
        failure_reason=(
            "quota: You have hit your weekly limit · resets 6pm "
            "(Asia/Jerusalem)"
        ),
    )

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "resumed") in actions, actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    assert refreshed.context["wake_dead_resumes"]["streak"] == 0


@pytest.mark.asyncio
async def test_an_attributed_wall_death_is_not_a_dead_resume(store):
    """f8efad06 (2026-08-21): four tasks were escalated "after 3 consecutive
    dead machine resumes" whose ONLY dead row since the last wake-resume was
    the quota wall the loop itself classified (`infra_failure = 1`,
    failure_reason `quota: ...`). An attributed death has a known cause and
    a wake; it is not the death-blind pattern this breaker exists for. The
    watcher must resume, not back off. RED on main: wake_backoff."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(store, t, at=anchor)
    wall_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        wall_id, status="failed", turns_used=1, tokens_used=0,
        infra_failure=1,
        failure_reason="quota: You've hit your weekly limit · resets 6pm")

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "resumed") in actions, actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    assert refreshed.context["wake_dead_resumes"]["streak"] == 0


@pytest.mark.asyncio
async def test_an_unattributed_dead_row_beside_a_wall_row_still_backs_off(store):
    """Negative control for the exclusion: the wall row is ignored, but a
    genuinely death-blind row (no infra attribution, no work) since the same
    resume still trips the breaker exactly as before."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(store, t, at=anchor)
    wall_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        wall_id, status="failed", turns_used=1, tokens_used=0, infra_failure=1,
        failure_reason="quota: wall")
    dead_id = await _dead_attempt(store, t, 2, turns=0, tokens=0)

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "wake_backoff") in actions, actions
    refreshed = await store.get_task(t.id)
    state = refreshed.context["wake_dead_resumes"]
    assert state["streak"] == 1
    assert state["attempt_ids"] == [dead_id]          # the wall row is not named


@pytest.mark.asyncio
async def test_human_resume_proceeds_and_resets_the_streak(store):
    """A stale dead-resume streak left over from BEFORE a human acted must
    never gate a later machine resume — `resume_from.by == "human"` since
    the streak was recorded means a human already intervened."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await store.merge_context(t.id, {
        "resume_from": {"sha": None, "branch": None, "by": "human"},
        "wake_dead_resumes": {
            "streak": 2, "attempt_ids": ["stale-id"],
            "last_resume_at": (now - timedelta(hours=2)).isoformat(),
            "backoff_until": None,
        },
    })

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "resumed") in actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 0
    assert dead_state["attempt_ids"] == []
    assert refreshed.context["resume_from"]["by"] == "wake"


@pytest.mark.asyncio
@pytest.mark.parametrize("turns,tokens", [(5, 0), (1, 1200)])
async def test_machine_resume_that_did_real_work_resets_the_streak(
    store, turns, tokens,
):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(
        store, t, at=anchor, streak=1, attempt_ids=["prior-dead-id"])
    await _dead_attempt(store, t, 1, turns=turns, tokens=tokens)

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "resumed") in actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 0
    assert dead_state["attempt_ids"] == []


@pytest.mark.asyncio
async def test_real_work_beside_an_attributed_wall_still_resets_the_streak(store):
    """Negative control for the fix above: an attributed wall row is neutral
    (it must not launder a ladder away), but it must not become a shield
    either — a genuinely healthy dispatch sharing the SAME window still
    resets the streak to 0 exactly as today, wall row or not.
    `test_machine_resume_that_did_real_work_resets_the_streak` remains the
    pure-healthy control (no wall row in the picture)."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(
        store, t, at=anchor, streak=1, attempt_ids=["prior-dead-id"])
    wall_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        wall_id, status="failed", turns_used=1, tokens_used=0,
        infra_failure=1,
        failure_reason="quota: You've hit your weekly limit · resets 6pm")
    await _dead_attempt(store, t, 2, turns=5, tokens=1200)

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "resumed") in actions, actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 0
    assert dead_state["attempt_ids"] == []


@pytest.mark.asyncio
async def test_an_attributed_wall_that_did_real_work_resets_the_streak(store):
    """BLOCKING finding from the independent review of the fix above:
    `infra_failure=1` means the loop knows WHY an attempt ended, not that
    the attempt did nothing before it ended. A quota wall that lands AFTER a
    long, priced session (turns/tokens both nonzero) is still evidence the
    worker is healthy and must reset the streak exactly like any other real
    dispatch — carrying the ladder forward here would let a task that is
    merely quota-blocked, not dead, false-escalate. This is the single-row,
    attributed-only-window case `test_real_work_beside_an_attributed_wall_
    still_resets_the_streak` does not cover (there the healthy row is a
    SEPARATE, non-attributed row sharing the window)."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(
        store, t, at=anchor, streak=1, attempt_ids=["prior-dead-id"])
    wall_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        wall_id, status="failed", turns_used=60, tokens_used=400_000,
        infra_failure=1,
        failure_reason="quota: You've hit your weekly limit · resets 6pm")

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "resumed") in actions, actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 0
    assert dead_state["attempt_ids"] == []


@pytest.mark.asyncio
async def test_dead_row_beside_a_wall_that_did_real_work_still_resets_the_streak(
    store,
):
    """NEW gap surfaced by the 2026-08-22 review: a window can contain BOTH
    an unattributed dead-blind row (no attribution, no work) AND an
    attributed row that did real work (a wall landing after a long, priced
    session), in the SAME window. The dead-blind row must never outvote the
    real-work row into counting the window as dead evidence — "did anything
    in this window do real work" has to span BOTH the attributed and
    unattributed rows, not just the unattributed ones. Pre-fix,
    `_dead_resume_verdict` only re-checked `relevant` (the unattributed list)
    for real work once `attributed` rows were filtered out of it, so this
    exact mix silently ignored the wall's real work and counted the
    dead-blind row toward the streak (backoff) instead of resetting —
    `test_an_attributed_wall_that_did_real_work_resets_the_streak` only
    covers the single-row case (nothing else in the window)."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(
        store, t, at=anchor, streak=1, attempt_ids=["prior-dead-id"])
    wall_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        wall_id, status="failed", turns_used=60, tokens_used=400_000,
        infra_failure=1,
        failure_reason="quota: You've hit your weekly limit · resets 6pm")
    await _dead_attempt(store, t, 2, turns=0, tokens=0)

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "resumed") in actions, actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 0
    assert dead_state["attempt_ids"] == []


@pytest.mark.asyncio
async def test_dead_resume_retry_after_attributed_wall_names_the_wall_not_a_timer(
    store,
):
    """MEDIUM finding from the 2026-08-22 review: the retry dispatch that
    follows an attributed-only window never had a backoff window armed for
    it — there is nothing to expire, the ladder was carried straight from a
    prior dead attempt across the wall — so the "resumed" event must not
    claim a "dead-resume backoff" timer fired.
    `test_an_expired_dead_resume_backoff_redispatches_instead_of_recounting`
    is the sibling case that DOES have a real expired backoff window and
    keeps the timer phrasing."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    await _machine_resumed(
        store, t, at=anchor, streak=1, attempt_ids=["prior-dead-id"])
    wall_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        wall_id, status="failed", turns_used=1, tokens_used=0,
        infra_failure=1,
        failure_reason="quota: You've hit your weekly limit · resets 6pm")

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "resumed") in actions, actions
    refreshed = await store.get_task(t.id)
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 1
    assert dead_state["attempt_ids"] == ["prior-dead-id"]
    events = await store.list_events(t.id)
    resumed_events = [e for e in events if e.get("kind") == "resumed"]
    assert len(resumed_events) == 1
    text = resumed_events[0]["text"]
    assert "attributed wall" in text
    assert "dead-resume backoff #" not in text


@pytest.mark.asyncio
async def test_no_attempt_row_since_last_resume_is_not_a_dead_resume(store):
    """No attempt row started at/after `last_resume_at` yet is a legitimate
    dispatch gap (the orchestrator hasn't picked the resumed task up), not
    evidence of death — must not be treated as a dead-resume streak hit."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    # A pre-existing streak of 1 with no attempt rows at all — proves the
    # empty-evidence branch resets rather than merely failing to advance.
    await _machine_resumed(
        store, t, at=anchor, streak=1, attempt_ids=["prior-dead-id"])

    watcher = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    actions = await watcher.tick(now=now)

    assert (t.id, "resumed") in actions
    assert (t.id, "wake_backoff") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 0


def test_every_blocker_category_has_a_route():
    """`Blocker.route` is a bare `_ROUTING[category]`, so a missing entry is a
    KeyError on a live path, not a default. STAGNATION was absent.

    CORRECTED 2026-08-01, and RE-CITED 2026-08-02 by symbol rather than by line
    (main's convention since "docs: cite symbols, not line numbers"). The line
    numbers this docstring used to carry had ALL rotted within a day: `:1936` had
    drifted into `_recurring_finding`, `:2661` into the middle of an unrelated
    call, `:3961-3963` into `_gate_already_satisfied`. Every one still read as a
    precise citation. A symbol survives edits above it and fails by name when it
    is renamed, which is the defect a reader actually hits.

    The first version of this docstring said the stuck detector "crashed on the
    case it exists to serve". That is FALSE, and it was the stated reason for the
    fix, so it is corrected rather than quietly dropped. The stuck detector never
    reaches `triage()` at all: `Orchestrator._drive` in `core/orchestrator.py`
    raises its STAGNATION blocker with `escalate_now=True`, and
    `Orchestrator._raise_blocker` builds `Route(ESCALATED, notify_now=True,
    parked=False)` INLINE in that branch without consulting the table.
    `triage()` — the only consumer of `_ROUTING` anywhere in `src/` — is called
    solely in the `else`.

    The reachable path is an AGENT-EMITTED stagnation blocker:
    `Orchestrator._run_attempt` in `core/orchestrator.py` calls `parse_blocker`
    on the agent's final text and passes the result straight to
    `_raise_blocker(task, emitted, repo=..., branch=...)` with no
    `escalate_now`, so it lands in the `else`, calls `triage()`, reads
    `blocker.route`, and raises KeyError. Real bug, real crash, different
    trigger — and one the stuck detector could not have produced.

    A per-category assertion would have missed it the same way the routing table
    did; this asserts over the ENUM, so the next category added without a route
    fails here instead of in production.
    """
    from no_human.blockers.taxonomy import route_for

    missing = []
    for category in BlockerCategory:
        try:
            route_for(category)
        except KeyError:
            missing.append(category.name)
    assert not missing, f"BlockerCategory with no route: {missing}"


def test_a_stagnation_blocker_escalates_rather_than_parking():
    """Parking requires a wake condition and stagnation has none — nothing
    external will change — so the honest route is to escalate with the report.
    """
    b = Blocker(category=BlockerCategory.STAGNATION, transient=False,
                confidence=0.9, goal="ship it",
                root_cause_hypothesis="review pass rate flat for 2 attempts")
    assert b.route.notify_now is True
    assert b.route.parked is False


def test_routing_stagnation_turns_it_into_a_learning_proposal():
    """The consequence of the route, which the original change did not state.

    Before, an agent-emitted STAGNATION raised KeyError and nothing downstream
    ran. Now it routes to ESCALATED with `parked=False`, and
    `Orchestrator._raise_blocker` in `core/orchestrator.py` calls
    `_propose_learning` on exactly `route.target_status == ESCALATED`. STAGNATION is deliberately NOT in
    `NON_LEARNABLE_CATEGORIES` — which holds only the categories whose cause is
    outside the agent (TRANSIENT_INFRA, QUOTA, DEPENDENCY_WAIT,
    BUDGET_EXHAUSTED, MISSING_ACCESS) — so agent-raised stagnation now feeds the
    learning queue as an anti-pattern proposal.

    That is defensible: repeated no-progress on a shape of task is exactly the
    kind of thing worth learning from. But it is a NEW behaviour that the
    routing fix switched on as a side effect, and a queue that floods has been a
    real failure mode here before, so it is pinned rather than left implicit. If
    stagnation proposals turn out to be noise, the fix is to add STAGNATION to
    NON_LEARNABLE_CATEGORIES and change this test deliberately.
    """
    from no_human.learning.queue import NON_LEARNABLE_CATEGORIES

    assert "STAGNATION" not in NON_LEARNABLE_CATEGORIES, (
        "STAGNATION is now non-learnable — that is a real product decision, but "
        "it contradicts the route added alongside this test; update both"
    )
    route = route_for(BlockerCategory.STAGNATION)
    assert route.target_status is TaskStatus.ESCALATED
    assert route.parked is False, (
        "a parked route would NOT reach _propose_learning; the learning "
        "consequence pinned here depends on parked=False"
    )


@pytest.mark.asyncio
async def test_a_long_attributed_run_is_bounded_by_dispatch_not_time(store):
    """The 2026-08-22 review recorded a DECISION — the dead-resume ladder gets
    no time-based expiry — and justified it with a claim that is false: "an
    unbounded run of attributed walls is bounded by `max_park` upstream". It
    is not, and the reason is structural: the `max_park` timeout is reached
    only by a tick that DECLINES to resume. For the `after:` condition this
    fixture uses, `WakeWatcher._evaluate` returns `_resume`'s result and never
    reaches the check. A ladder carried by `retry` resumes on every tick, so
    the timeout is not reached here, however old the blocker is. Claim only
    that much: a satisfied condition CAN reach the timeout — the
    `pr_comment_on` fall-through does exactly that — and two earlier drafts of
    this fixture's prose were refuted for claiming otherwise. (A second,
    independent reason, NOT exercised here: the real quota park rebuilds the
    blocker with a fresh `raised_at` on every park, so the 48h clock measures
    one continuous park, not a park->resume->park run.)

    The control at the end is what makes this a measurement rather than an
    assertion: the SAME task, with the SAME `raised_at` — 61 days old by then,
    over thirty times `max_park`, and already past it from day 2 of the loop
    onward — escalates on `escalated_timeout` the moment its condition stops
    being satisfied. So the age was sufficient to time out for all but the
    first pinned day; only the resume-and-return kept it alive. That is the
    whole refutation in one fixture.

    The precise rule this pins is resume-or-not, NOT satisfied-or-not: a
    satisfied condition can still reach the timeout (the `pr_comment_on`
    fall-through does exactly that). Nothing here should be read as covering
    that path.

    The real bound is dispatch: every carrying tick spends a fresh attempt.
    The row that MADE it a carry was necessarily dead-shaped (`_is_dead`: 0
    priced tokens AND <= 1 turn — not merely "did no priced work"),
    but the attempt it then spends is unconstrained — if that one does real
    work the next tick resets the streak to 0, which is the design, not an
    exception. So a long attributed run is the loop working against a walling
    environment, not the loop stalling. The closing assertions also prove the
    ladder stays LIVE rather than going inert across those 60 days."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    # `raised_at` is stamped ONCE, three hours before day 0 by the helper,
    # and never refreshed below — that "never refreshed" is the load-bearing
    # half, and the control at the end re-reads it to prove it.
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    raised_at = datetime.fromisoformat(t.blocker["raised_at"])
    dead_id_1 = await _dead_attempt(store, t, 1, turns=0, tokens=0)
    # Backdate it for the reason `..._still_reaches_the_park` documents at
    # length: `create_attempt` stamps `started_at` off the REAL wall clock,
    # and every re-dispatch re-anchors `last_resume_at` off the real clock
    # too, so left alone dead_id_1 stays "relevant" forever and every carry
    # below takes the `expired_backoff` rung instead of `attributed_wall`.
    # A 2026-08-22 review PROVED that: with this line absent, deleting all
    # 60 walls left the run byte-identical -- the walls were decorative and
    # the rung this fixture is named for was never reached.
    await store.update_attempt(dead_id_1, started_at=now.isoformat())
    await _machine_resumed(
        store, t, at=anchor, streak=1, attempt_ids=[dead_id_1])

    cfg = _cfg(wake_poll_interval="10m")
    watcher = WakeWatcher(store, cfg)
    dispatches = 0
    for day in range(1, 61):
        at = now + timedelta(days=day)
        await _dispatched_then_walled(store, t, day + 1)
        actions = await watcher.tick(now=at)

        assert (t.id, "resumed") in actions, (day, actions)
        assert (t.id, "escalated_timeout") not in actions, (day, actions)
        assert (t.id, "parked_dead_resumes") not in actions, (day, actions)
        assert (t.id, "wake_backoff") not in actions, (day, actions)
        dispatches += 1
        refreshed = await store.get_task(t.id)
        assert refreshed.status == TaskStatus.IMPLEMENTING, day
        dead_state = refreshed.context["wake_dead_resumes"]
        assert dead_state["streak"] == 1, (day, dead_state)
        assert dead_state["attempt_ids"] == [dead_id_1], (day, dead_state)

    # The bound is dispatch: one fresh attempt per carrying tick.
    assert dispatches == 60
    refreshed = await store.get_task(t.id)
    assert refreshed.status != TaskStatus.ESCALATED
    assert (refreshed.blocker or {}).get("dead_resume_streak") is None

    # The ladder is live, not inert: a real dead attempt still counts.
    end = now + timedelta(days=61)
    dead_id_2 = await _dispatched_then_died(store, t, 62)
    actions = await watcher.tick(now=end)
    assert (t.id, "wake_backoff") in actions, actions
    refreshed = await store.get_task(t.id)
    dead_state = refreshed.context["wake_dead_resumes"]
    assert dead_state["streak"] == 2
    assert dead_state["attempt_ids"] == [dead_id_1, dead_id_2]

    # CONTROL: the blocker is now 61 days old — >30x `max_park` — and has been
    # since early in the loop. Keep that same `raised_at`, swap only the
    # condition for one that is NOT satisfied, and the timeout fires at once.
    assert end - raised_at > 30 * watcher.max_park
    stale = await store.get_task(t.id)
    stale.blocker = {**stale.blocker, "wake_condition": "pr_merged:org/repo#7"}
    stale.wake_check_at = None
    await store.update_task_columns(stale)
    await store.set_status(stale, TaskStatus.BLOCKED, validate=False)

    async def never_merges(ref):
        return False

    timeout_watcher = WakeWatcher(store, cfg, pr_merged=never_merges)
    actions = await timeout_watcher.tick(now=end)
    assert (t.id, "escalated_timeout") in actions, actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.ESCALATED
    assert refreshed.blocker["timed_out"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turns,seed_ladder,expected",
    [
        # The rung fires: dead-shaped attributed row, and a ladder to protect.
        (1, True, ("retry", 1, "LADDER", "attributed_wall")),
        # `prior_ids` empty -> nothing to carry, so the rung must NOT fire even
        # though the row is attributed and dead-shaped. Kills a mutant that
        # drops `prior_ids` from `if attributed and prior_ids:`.
        (1, False, ("proceed", 0, [], None)),
        # One turn past the boundary -> not dead-shaped, so the real-work reset
        # wins. Kills a mutant that widens `turns <= 1` to `turns <= 2`.
        (2, True, ("proceed", 0, [], None)),
    ],
)
async def test_the_attributed_rung_needs_a_dead_shaped_row_and_a_ladder(
    store, turns, seed_ladder, expected,
):
    """The attributed rung (`wake.py`'s `if attributed and prior_ids:`) fires on
    a conjunction, and each case below breaks exactly one half of it.

    `_is_dead` is `priced == 0 AND turns <= 1` — not "did no priced work". All
    three rows here have 0 priced tokens, so that weaker predicate cannot tell
    them apart; the turn count and the presence of a ladder are what decide.

    WHY THESE THREE, stated precisely because two earlier versions of this test
    were refuted for adding nothing: a 2026-08-22 review measured every mutant
    of `_is_dead` and of this rung against the whole file, and found the cases
    it then contained were killed by `test_machine_resume_that_did_real_work_
    resets_the_streak` anyway — zero marginal coverage. These two survive the
    entire file today:
      * `seed_ladder=False` — attributed and dead-shaped, but `prior_ids` is
        empty, so there is nothing to carry and the rung must NOT fire. Kills a
        mutant that drops `prior_ids` from the conjunction.
      * `turns=2` — one past the boundary, so the row is not dead-shaped and the
        real-work reset wins. Kills a mutant that widens `turns <= 1`.

    THE ABLATION MATRIX, measured rather than assumed — an earlier version of
    this paragraph claimed "the FIRST case only" and was refuted:

        ablated element        [1-True]  [1-False]  [2-True]
        infra_failure=1        FAIL      FAIL       pass
        old_id backdate        FAIL      FAIL       pass
        _machine_resumed seed  FAIL      pass       pass
        status="failed"        pass      pass       pass

    So the flag and the backdate are load-bearing in BOTH `seed_ladder` cases,
    inert only for `turns=2` (which never reaches the rung anyway). Dropping
    either makes `relevant` non-empty so the rung is skipped, but the verdict
    that results differs and the difference matters: without the FLAG both
    seeded cases return `backoff`; without the BACKDATE, `[1-False]` returns
    `backoff` while `[1-True]` takes the `expired_backoff` rung instead,
    because its only dead row is already in `prior_ids` so `new_ids` is empty.
    That is the same `expired_backoff`-vs-`attributed_wall` distinction the
    traps below depend on, which is why this paragraph does not flatten it.

    TWO TRAPS THIS RECORDS so nobody trims the setup later:
    * the backdate is what EMPTIES `relevant`, and case 2's whole reason to
      exist is the `prior_ids` half of the rung's conjunction. Remove the
      backdate and that mutant goes unexercised — mutant and clean code both
      return `('backoff', 1, [old_id], None)`.
    * removing the `_machine_resumed` seed makes case 2 PASS SILENTLY, because
      `resume_from.by != "wake"` returns before the rung is reached. A green
      test there would mean nothing. That is the same shape as the decorative
      rows a review found in this file twice before.

    `status="failed"` is inert in all three — nothing reads it — and is kept
    only because the helper rows around it set it."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    anchor = (now - timedelta(hours=1)).isoformat()
    t = await _park(store, **_dead_resume_blocked_task_kwargs(now))
    old_id = await _dead_attempt(store, t, 1, turns=0, tokens=0)
    # Out of the judged window, so `relevant` is empty and the ATTRIBUTED rung
    # is the one under test. Load-bearing for BOTH seed_ladder cases — see the
    # matrix in the docstring; an earlier version of this comment said
    # seed_ladder=True only, which measurement refuted.
    await store.update_attempt(old_id, started_at="2025-05-18T12:00:00+00:00")
    await _machine_resumed(
        store, t, at=anchor,
        streak=1 if seed_ladder else 0,
        attempt_ids=[old_id] if seed_ladder else [],
    )
    wid = await store.create_attempt(t.id, 2)
    await store.update_attempt(wid, status="failed", turns_used=turns,
                               tokens_used=0, infra_failure=1,
                               failure_reason="quota wall")
    w = WakeWatcher(store, _cfg(wake_poll_interval="10m"))
    fresh = await store.get_task(t.id)
    v = await w._dead_resume_verdict(fresh, now=now)
    want = tuple(expected)
    ids = [old_id] if want[2] == "LADDER" else want[2]
    assert (v[0], v[1], v[2], v[3]) == (want[0], want[1], ids, want[3]), v


def test_prose_fields_normalise_from_agent_list_output():
    """Measured 2026-09-01 (task 019d8175): an agent emitted `evidence` as a
    JSON array of lines; `render_report`'s `.strip()` then crashed the
    scheduler (`'list' object has no attribute 'strip'`) and the attempt died
    terminally before its blocker was ever rendered. Agent-emitted shapes must
    not need a migration (same contract as options above): list prose joins to
    lines, None means absent."""
    b = Blocker.from_dict({
        "category": "NOVEL_UNKNOWN",
        "evidence": ["$ cmd", "line two"],
        "goal": ["implement", "X"],
        "root_cause_hypothesis": None,
        "question": ["should I", "continue?"],
        "wake_condition": ["ci green"],
    })
    assert b.evidence == "$ cmd\nline two"
    assert b.goal == "implement\nX"
    assert b.root_cause_hypothesis == ""
    assert b.question == "should I\ncontinue?"
    assert b.wake_condition == "ci green"   # single-element list unwraps
    # absent stays absent — render's `if b.question` branch must not flip
    empty = Blocker.from_dict({"category": "NOVEL_UNKNOWN"})
    assert empty.question is None
    assert empty.wake_condition is None
    # and the crash site itself renders the exact joined block, not a repr
    out = render_report(b, task_title="t", task_id="abcdef123456")
    assert "$ cmd\nline two" in out
    assert "['" not in out


def test_wake_condition_list_never_joins_into_a_fake_condition():
    """`wake_condition` is machine-checkable, not prose: the watcher's
    `parse_duration` sums every duration-shaped match anywhere in the string,
    so joining ["after:2h", "and PR org/repo#12 merged"] would fabricate a
    condition that self-fires at +2h12m — the 12 minutes lifted out of the PR
    number, the merge half never checked. More than one element is not ONE
    condition: it becomes None, which never self-fires and still escalates to
    a human via the max_park timeout."""
    b = Blocker.from_dict({
        "category": "NOVEL_UNKNOWN",
        "wake_condition": ["after:2h", "and PR org/repo#12 merged"],
    })
    assert b.wake_condition is None


def test_tried_and_confidence_normalise_from_agent_shapes():
    """The same unguarded parse_blocker call site: `{"tried": 3}` raised
    TypeError, `{"confidence": "high"}` raised ValueError, and a bare string
    `tried` shredded into per-character report bullets via list()."""
    b = Blocker.from_dict({
        "category": "NOVEL_UNKNOWN",
        "tried": "restarted the server",
        "confidence": "high",
    })
    assert b.tried == ["restarted the server"]
    assert b.confidence == 0.0   # unparseable -> escalate-leaning default
    assert Blocker.from_dict({"category": "NOVEL_UNKNOWN", "tried": 3}).tried == ["3"]
    out = render_report(b, task_title="t", task_id="abcdef123456")
    assert "- restarted the server" in out
    assert "- r\n" not in out
