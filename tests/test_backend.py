"""Backend-level guards for the Claude Agent SDK wrapper.

Regression for the shadow-validation finding (2026-06-22): the SDK signals
hitting max_turns by *raising* a bare Exception from inside query(), not by
emitting a ResultMessage. The backend must convert that into a normal is_error
result so the orchestrator's bounded loop treats it as a failed attempt instead
of crashing the whole process.
"""

import pytest

# The whole module exercises the REAL ClaudeBackend over a monkeypatched SDK
# seam (claude_backend.query) — exempt from the hermetic stub by design.
pytestmark = pytest.mark.real_backend

from claude_agent_sdk import ResultMessage

from no_human.agent import claude_backend
from no_human.agent.claude_backend import ClaudeBackend


def _fake_query(error_message):
    async def _q(*args, **kwargs):
        raise Exception(error_message)
        yield  # pragma: no cover — makes this an async generator
    return _q


def _result_then_raise(result_msg, error_message):
    """Mimic the real SDK: emit a ResultMessage ('agent done'), THEN raise the
    terminal error — the exact sequence that crashed the orchestrator."""
    async def _q(*args, **kwargs):
        yield result_msg
        raise Exception(error_message)
    return _q


async def test_max_turns_exception_becomes_error_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_backend, "query",
        _fake_query("Claude Code returned an error result: Reached maximum number of turns (40)"),
    )
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=40)

    assert result.is_error is True
    assert result.stop_reason == "max_turns"
    assert result.num_turns == 40
    assert "maximum number of turns" in result.final_text.lower()


async def test_other_sdk_error_becomes_error_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_backend, "query", _fake_query("transport blew up"),
    )
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=40)

    # Any terminal SDK error is surfaced as a failed run, never raised.
    assert result.is_error is True
    assert result.stop_reason == "error"
    assert "transport blew up" in result.final_text


async def test_result_message_then_raise_is_corrected_to_error(tmp_path, monkeypatch):
    # The real-world crash: SDK emits a ResultMessage (is_error=False), then
    # raises the max-turns error. The corrective result event must win, so run()
    # returns is_error=True while preserving the real turn/token counts.
    rm = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=13, session_id="abc", result="agent done",
        usage={"input_tokens": 7000, "output_tokens": 347},
    )
    monkeypatch.setattr(
        claude_backend, "query",
        _result_then_raise(rm, "Claude Code returned an error result: Reached maximum number of turns (12)"),
    )
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=12)

    assert result.is_error is True
    assert result.stop_reason == "max_turns"
    assert result.num_turns == 13           # preserved from the ResultMessage
    assert result.tokens_used == 7347       # preserved from the ResultMessage
    assert result.session_id == "abc"


async def test_stream_yields_a_result_event_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_backend, "query", _fake_query("Reached maximum number of turns (40)"),
    )
    backend = ClaudeBackend(model="claude-opus-5")

    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=40)]

    assert len(events) == 1
    assert events[0].kind == "result"
    assert events[0].meta["is_error"] is True
    assert events[0].meta["stop_reason"] == "max_turns"


def _fake_query_result(msg):
    """The NORMAL path: the SDK ends the session with a ResultMessage and
    never raises — unlike `_fake_query`, which mimics the terminal-exception
    path."""
    async def _q(*args, **kwargs):
        yield msg
    return _q


async def test_normal_path_max_turns_result_reports_max_turns(tmp_path, monkeypatch):
    rm = ResultMessage(
        subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=24, session_id="s",
        result="Reached maximum number of turns (24)",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    monkeypatch.setattr(claude_backend, "query", _fake_query_result(rm))
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=24)

    assert result.is_error is True
    assert result.stop_reason == "max_turns"
    assert result.num_turns == 24


async def test_a_result_that_merely_quotes_the_phrase_is_not_turn_exhaustion(tmp_path, monkeypatch):
    # Negative control: model prose that quotes the phrase must not be read
    # as turn exhaustion — the derivation is keyed on the structured
    # subtype, never on the text.
    rm = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=5, session_id="s", stop_reason=None,
        result="...Reached maximum number of turns...",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    monkeypatch.setattr(claude_backend, "query", _fake_query_result(rm))
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=24)

    assert result.stop_reason != "max_turns"


async def test_normal_path_result_does_not_invent_max_turns_for_other_subtypes(tmp_path, monkeypatch):
    rm = ResultMessage(
        subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=5, session_id="s",
        result="something else went wrong",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    monkeypatch.setattr(claude_backend, "query", _fake_query_result(rm))
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=24)

    assert result.stop_reason != "max_turns"


async def test_normal_path_max_turns_result_then_raise_survives_the_terminal_exception(
        tmp_path, monkeypatch):
    # The real end-to-end sequence: the CLI emits the error_max_turns
    # ResultMessage, THEN the process exits non-zero and the SDK rewrites that
    # into "Claude Code returned an error result: error_max_turns" (empty
    # errors list — the wording carries no "maximum number of turns" text).
    # The terminal-exception handler must not clobber the structurally-derived
    # stop_reason with a text match that cannot possibly hit.
    rm = ResultMessage(
        subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=24, session_id="s",
        result="Reached maximum number of turns (24)",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    monkeypatch.setattr(
        claude_backend, "query",
        _result_then_raise(rm, "Claude Code returned an error result: error_max_turns"),
    )
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=24)

    assert result.is_error is True
    assert result.stop_reason == "max_turns"
    assert result.num_turns == 24


async def test_genuine_error_preserves_traceback_for_diagnosis(tmp_path, monkeypatch):
    """A bare "'bool' object is not subscriptable" with no file:line burned 3
    attempts undiagnosably (task 6cfdb936). Genuine errors must carry the
    traceback so the crash is diagnosable."""
    monkeypatch.setattr(
        claude_backend, "query", _fake_query("'bool' object is not subscriptable"),
    )
    result = await ClaudeBackend(model="claude-opus-5").run(
        "do it", cwd=tmp_path, max_turns=40)
    assert result.is_error is True
    assert "'bool' object is not subscriptable" in result.final_text
    assert "Traceback" in result.final_text  # the diagnostic frames are preserved


async def test_max_turns_stays_clean_without_a_traceback(tmp_path, monkeypatch):
    """max_turns is not a crash — its message stays clean, no traceback noise."""
    monkeypatch.setattr(
        claude_backend, "query",
        _fake_query("Claude Code returned an error result: Reached maximum number of turns (40)"),
    )
    result = await ClaudeBackend(model="claude-opus-5").run(
        "x", cwd=tmp_path, max_turns=40)
    assert "Traceback" not in result.final_text


def test_thinking_is_a_dict_not_a_bool(tmp_path):
    """The SDK's `thinking` is a ThinkingConfig dict; passing True crashed every
    complex (thinking-enabled) task with 'bool' object is not subscriptable
    (task 6cfdb936)."""
    b = ClaudeBackend(model="claude-opus-5")
    opts = b._options(tmp_path, 40, thinking=True)
    assert isinstance(opts.thinking, dict) and opts.thinking["type"] == "adaptive"

    opts_budget = b._options(tmp_path, 40, thinking=True, max_thinking_tokens=8000)
    assert opts_budget.thinking == {"type": "enabled", "budget_tokens": 8000}

    opts_off = b._options(tmp_path, 40, thinking=False)
    assert not opts_off.thinking  # None / absent when not requested


def test_precompact_hook_registered_when_on_compact_set(tmp_path):
    """C1(a): compaction visibility. When the orchestrator passes on_compact,
    the SDK options must carry a PreCompact hook — the only way to KNOW whether
    the CLI's auto-compaction ever fires for coder sessions (it never has been
    observed; sessions end ~160k tokens, under the ~92% threshold)."""
    b = ClaudeBackend(model="claude-opus-5")
    opts = b._options(tmp_path, 40, on_compact=lambda trigger: None)
    assert "PreCompact" in opts.hooks

    opts_off = b._options(tmp_path, 40)
    assert "PreCompact" not in (opts_off.hooks or {})


async def test_precompact_hook_reports_trigger_and_allows():
    """The hook is pure telemetry: it forwards the trigger and never blocks."""
    calls = []
    hook = claude_backend._make_compact_hook(calls.append)
    out = await hook({"trigger": "auto"}, None, None)
    assert out == {}
    assert calls == ["auto"]


def test_coder_sessions_are_project_scoped(tmp_path):
    """C1-i2: passing skills= must pin setting_sources to ["project"] — the
    SDK's implicit default adds "user", which loads the operator's plugins
    (superpowers et al.), personal settings, and EVERY user skill into every
    coder session's context. Relevant user skills are delivered by copying
    them into the working tree instead (see _materialize_skills)."""
    b = ClaudeBackend(model="claude-opus-5")
    opts = b._options(tmp_path, 40, skills=["a", "b"])
    assert opts.setting_sources == ["project"]

    # A coder session with NO skills is still project-scoped — otherwise the
    # target repo's CLAUDE.md would not load for it (and consistency of the
    # prompt prefix across tasks would break).
    opts_bare = b._options(tmp_path, 40)
    assert opts_bare.setting_sources == ["project"]

    # Read-only sessions (reviewer/planner/supervisor) are hermetic — and the
    # value must be the EMPTY LIST, never None.
    #
    # This assertion used to read `is None`, which is how the hole stayed
    # invisible: it encoded the belief that None means "no sources". None means
    # "no --setting-sources flag is emitted", and the CLI then applies its own
    # default, which loads `user` and `project`. Proven with a live canary — a
    # read-only session pointed at a directory whose only file was a project
    # instruction file carrying a canary word returned that word. (Generic on
    # purpose: the export drops that document, and the line above already spends
    # this file's one declared citation of it.)
    #
    # The consequence was that the repository under review supplied instructions
    # into the context of the reviewer judging it. `[]` emits the flag with no
    # sources, which is the only value that actually means hermetic.
    r = ClaudeBackend(model="claude-opus-5", readonly=True)
    opts_ro = r._options(tmp_path, 40)
    assert opts_ro.setting_sources == [], (
        "None does not mean hermetic — it means the CLI picks the default")

    # A read-only session WITH skills still needs project scope to find them.
    opts_ro_skills = r._options(tmp_path, 40, skills=["a"])
    assert opts_ro_skills.setting_sources == ["project"]


async def test_the_clis_own_reason_survives_the_sdk_wrapper(tmp_path, monkeypatch):
    """A spend-limit rejection must not be reported as "error result: success".

    The real incident: every attempt recorded turns=1, tokens=0 and
    "agent run did not complete: Claude Code returned an error result: success"
    — the SDK's wrapper, whose subtype for this rejection is literally the word
    "success". The CLI had already said "You've hit your monthly spend limit" on
    the preceding result event, and run() keeps the LAST event, so the only
    message naming the cause was overwritten. It cost a day of debugging a
    perfectly healthy codebase.
    """
    rm = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=1, session_id="abc",
        result="You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
        usage={"input_tokens": 0, "output_tokens": 0},
    )
    monkeypatch.setattr(
        claude_backend, "query",
        _result_then_raise(rm, "Claude Code returned an error result: success"),
    )
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=12)

    assert result.is_error is True
    assert "monthly spend limit" in result.final_text, result.final_text
    # The wrapper is still there for context — it is just no longer the ONLY
    # thing a human sees.
    assert "error result: success" in result.final_text


async def test_a_SUCCESSFUL_run_does_not_donate_its_prose_to_a_later_error(
        tmp_path, monkeypatch):
    """The mirror image of the bug this file's other test fixes.

    Capturing the result text unconditionally meant a run that FINISHED
    NORMALLY and then hit a transport error ("Stream closed" is an observed
    failure here) inherited the agent's own summary into the error. When that
    summary happened to mention rate limits — routine in THIS codebase, which
    is full of quota-handling code — `_quota_signal` fired and parked a
    perfectly healthy task as PAUSED_QUOTA, aborting its bounded loop. The
    fix is to capture only from a result that is ITSELF an error.
    """
    from no_human.core.orchestrator import _quota_signal

    rm = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False,                      # <-- the run SUCCEEDED
        num_turns=4, session_id="abc",
        result="Added retry handling for rate limit exceeded responses in client.py.",
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    monkeypatch.setattr(claude_backend, "query",
                        _result_then_raise(rm, "Stream closed unexpectedly"))
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=12)

    assert result.is_error is True                 # the transport error stands
    assert "rate limit exceeded" not in result.final_text, result.final_text
    assert "Stream closed" in result.final_text    # the REAL reason survives
    # The consequence that made this worth a Critical.
    assert _quota_signal(result.final_text) is False


async def test_the_clis_own_quota_phrasings_route_to_the_quota_park(tmp_path):
    """`_quota_signal` decides between "billing wall" (park with a wake
    condition) and "broken task" (burn all 3 attempts). Both phrasings below
    were observed live from the CLI, and NEITHER matched the original literal
    list — so the exact failure this branch exists to surface was still
    classified as a generic error."""
    from no_human.core.orchestrator import _quota_signal

    assert _quota_signal("You've hit your monthly spend limit") is True
    assert _quota_signal("You've hit your weekly limit · resets 1:10pm") is True
    assert _quota_signal("You’ve hit your weekly limit") is True  # curly '
    # Still not a quota problem — these must keep burning attempts normally.
    assert _quota_signal("TypeError: 'bool' object is not subscriptable") is False
    assert _quota_signal("2 tests failed in test_limit_parser.py") is False


async def test_the_api_error_status_reaches_the_classifier(tmp_path, monkeypatch):
    """`AgentResult` had no such field, so the orchestrator's
    `getattr(result, "api_error_status", None)` was permanently None and
    `_classify_error`'s 429/529 branch was dead code. The SDK sets this
    precisely when is_error is true and the subtype is "success" — this
    incident's exact shape — so it is the STRUCTURED twin of the free-text
    reason, and it does not depend on matching English."""
    from no_human.core.orchestrator import _classify_error

    rm = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=1, session_id="abc", result="upstream said no",
        usage={"input_tokens": 0, "output_tokens": 0},
        api_error_status=429,
    )
    monkeypatch.setattr(
        claude_backend, "query",
        _result_then_raise(rm, "Claude Code returned an error result: success"))
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=12)

    assert result.api_error_status == 429
    # The branch that could never fire before.
    assert _classify_error(result.stop_reason, result.final_text,
                           result.api_error_status) == "rate_limited"


async def test_the_prepended_reason_is_capped_like_everything_beside_it(
        tmp_path, monkeypatch):
    """`final_text` is persisted on the event AND fed to `error_signature`.
    The traceback next to it is capped at 3000/4000 chars; an uncapped result
    let a single huge message dominate both."""
    rm = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=1, session_id="abc", result="Q" * 200_000,
        usage={"input_tokens": 0, "output_tokens": 0},
    )
    monkeypatch.setattr(
        claude_backend, "query",
        _result_then_raise(rm, "Claude Code returned an error result: success"))
    backend = ClaudeBackend(model="claude-opus-5")

    result = await backend.run("do it", cwd=tmp_path, max_turns=12)

    assert result.final_text.count("Q") == 4000
    assert len(result.final_text) < 12_000, len(result.final_text)


async def test_a_file_path_can_never_be_mistaken_for_a_billing_wall():
    """Found by the test above failing for the wrong reason.

    `final_text` carries a TRACEBACK, and the old signal matched the bare
    substring "quota" — so any stack frame through a directory or module whose
    name contains it was read as a billing wall. This codebase is FULL of
    quota-handling code, so a crash inside it would park a healthy task as
    PAUSED_QUOTA on a limit it never hit, and the operator would go looking for
    a billing problem that does not exist. Every term now contains a space or
    is a full API error type; paths contain neither.
    """
    from no_human.core.orchestrator import _classify_error, _quota_signal

    tb = ('Traceback (most recent call last):\n'
          '  File "/Users/x/git/no_human/src/no_human/core/quota_park.py", '
          'line 12, in park\n'
          '  File "/tmp/wt-quota/tests/test_rate_limit_helper.py", line 3\n'
          'TypeError: bad operand')
    assert _quota_signal(tb) is False, tb
    # And the structural signal is no longer shadowed by that false positive:
    # _quota_signal is checked FIRST, so a path match used to win over a real
    # HTTP status and misreport a 429 as a quota park.
    assert _classify_error("error", tb, 429) == "rate_limited"


def test_a_quota_park_always_carries_a_WAKE_TIME(monkeypatch):
    """Without one, two separate mechanisms silently do nothing.

    `blockers/wake.py` resumes a PAUSED_QUOTA task only when `wake_check_at`
    is set AND due, so a bare `QuotaExhausted()` meant nothing ever
    auto-resumed. Worse, `scheduler.py` arms its pool-wide cooldown only when
    that value parses — so with None the pool immediately dispatched the next
    queued task into the same billing wall and parked it too, one at a time.
    That is the observed incident: 4 tasks, 12 attempts, one wall.

    Asserted on the EXCEPTION rather than the call site on purpose. My first
    version tested a helper the raise site happened to call, which a bare
    `QuotaExhausted()` would have left correct and simply unused — the wiring
    could break with the test green. Defaulting inside the class makes the
    invariant structural: no raise site, present or future, can produce a park
    that never wakes.

    The clock `bounds.py` reads (`datetime.now(timezone.utc)`, the single use
    site inside the module) is frozen here rather than sampled live and
    compared to a `60 < delta <= 3660`-shaped literal window: that older form
    measured a live delta between two separate `now()` calls (one inside
    `__init__`, one in the test) and was therefore a wall-clock-vs-literal
    assertion in the exact shape this suite forbids elsewhere — a box slow
    enough to blow 60s between construction and the assertion would have
    flipped it despite the code being correct. Freezing the clock turns it
    into an exact, deterministic happens-after fact instead.
    """
    from datetime import datetime, timedelta, timezone

    from no_human.core import bounds
    from no_human.core.bounds import QuotaExhausted

    fixed_now = datetime(2030, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedClock:
        @staticmethod
        def now(tz=None):
            return fixed_now

    monkeypatch.setattr(bounds, "datetime", _FixedClock)

    exc = QuotaExhausted()                      # the bare form, as it was
    assert exc.resets_at, "a quota park with no wake time never resumes"
    parsed = datetime.fromisoformat(exc.resets_at)
    assert parsed.tzinfo is not None, "naive: the scheduler cannot parse it"
    # Exact, not bounded: with the clock frozen, the default is deterministic
    # — RETRY_AFTER_S past the frozen instant, no more and no less.
    expected = fixed_now + timedelta(seconds=QuotaExhausted.RETRY_AFTER_S)
    assert parsed == expected, parsed
    # A caller that genuinely knows the reset time still wins.
    assert QuotaExhausted("m", resets_at="2030-01-01T00:00:00+00:00"
                          ).resets_at == "2030-01-01T00:00:00+00:00"


def test_the_park_detail_names_the_wall_without_the_traceback():
    """`final_text` leads with the CLI's reason and then carries a traceback.
    The park detail is what a human reads on the board — it must name the wall
    and nothing else."""
    from no_human.core.orchestrator import _quota_reason

    text = ("You've hit your monthly spend limit · raise it at claude.ai\n\n"
            "Traceback (most recent call last):\n  File \"x.py\", line 1\n")
    reason = _quota_reason(text)
    assert reason.startswith("You've hit your monthly spend limit")
    assert "Traceback" not in reason
    assert len(reason) <= 200
    # Never empty, or the park reports nothing at all.
    assert _quota_reason("") and _quota_reason("\n\n  \n")


def test_the_quota_regex_does_not_fire_on_ordinary_english():
    """The period is one or two words; the English false positive a character
    bound admits needs three. Bounding by WORDS separates them, so the
    classifier does not depend on its caller for correctness."""
    from no_human.core.orchestrator import _quota_signal

    for t in ("You've hit your monthly spend limit",
              "You've hit your weekly limit",
              "You have hit your team's weekly limit",
              "You’ve hit your organization’s monthly limit",
              "You have hit your limit"):
        assert _quota_signal(t) is True, t
    for t in ("You did not hit your head on the limit switch",
              "FAILED tests/test_hit_your_retry_limit.py",
              "TypeError: 'bool' object is not subscriptable"):
        assert _quota_signal(t) is False, t


def test_coder_options_unchanged_by_the_advisory_seam(tmp_path):
    """The advisory seam (`agent/advisory.py`) adds `tools`/`system_prompt`
    params to `ClaudeBackend`, both defaulting to `None`. The default (coder)
    construction must keep serializing exactly as before: no `tools`, no
    `system_prompt` override, `project` settings."""
    opts = ClaudeBackend(model="claude-sonnet-5")._options(tmp_path, 40)
    assert opts.tools is None
    assert opts.system_prompt is None
    assert opts.setting_sources == ["project"]


@pytest.fixture
def fake_query(monkeypatch):
    """Records the ClaudeAgentOptions query() was called with and yields one
    ResultMessage the test can pre-load with `structured_output`."""
    class _Recorder:
        def __init__(self):
            self.options = None
            self.result_message = ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="s", result="ok",
                usage={"input_tokens": 1, "output_tokens": 1},
            )

        async def __call__(self, *, prompt, options):
            self.options = options
            yield self.result_message

    rec = _Recorder()
    monkeypatch.setattr(claude_backend, "query", rec)
    return rec


async def test_output_format_reaches_options_and_structured_output_is_returned(fake_query, tmp_path):
    fake_query.result_message.structured_output = {"architecture": "x", "modules": "y", "conventions": "z"}
    backend = ClaudeBackend(model="claude-sonnet-5")
    res = await backend.run("p", cwd=tmp_path, max_turns=2,
                            output_format={"type": "json_schema", "schema": {"type": "object"}})
    assert fake_query.options.output_format == {"type": "json_schema", "schema": {"type": "object"}}
    assert res.structured_output == {"architecture": "x", "modules": "y", "conventions": "z"}


async def test_no_output_format_leaves_option_unset(fake_query, tmp_path):
    backend = ClaudeBackend(model="claude-sonnet-5")
    await backend.run("p", cwd=tmp_path, max_turns=2)
    assert fake_query.options.output_format is None


def test_permission_mode_defaults_to_bypass_and_never_falls_back_silently():
    """The default is the shipped behaviour; anything unrecognised is loud.

    The empty cases are the point. This key exists because a silent fallback
    reproduces the exact failure it cures — the default is the mode a
    policy-bound organisation disables, so an operator who wrote the key and
    left it blank must not be handed that mode back without a word. ABSENT is
    the only spelling that means "use the default".
    """
    from no_human.config import AuthError, PERMISSION_MODES, permission_mode

    assert permission_mode({}) == "bypassPermissions"
    assert permission_mode({"llm": {}}) == "bypassPermissions"
    assert permission_mode(
        {"llm": {"permission_mode": "acceptEdits"}}) == "acceptEdits"

    for blank in ("", None, "   "):
        with pytest.raises(AuthError):
            permission_mode({"llm": {"permission_mode": blank}})

    # Case-sensitive on purpose: the SDK spellings are camelCase.
    with pytest.raises(AuthError) as exc:
        permission_mode({"llm": {"permission_mode": "acceptedits"}})
    for legal in PERMISSION_MODES:
        assert legal in str(exc.value)


def test_the_tool_allowlist_is_sent_only_when_the_mode_needs_it(tmp_path):
    """`bypassPermissions` approves every tool itself, so an allowlist is dead
    weight there and is not sent.

    The `tools == []` row is the advisory seam, which ships no tool schema at
    all. It is skipped because an allowlist over a session holding no tools is
    meaningless — NOT because an allowlist could undo that saving; `--tools`
    and `--allowedTools` are separate flags and the second cannot repopulate
    the first.
    """
    from no_human.agent.claude_backend import PRE_APPROVED_TOOLS

    def allowed(mode, tools=None):
        opts = ClaudeBackend(model="m", permission_mode=mode,
                             tools=tools)._options(tmp_path, 5)
        return list(getattr(opts, "allowed_tools", []) or [])

    assert allowed("bypassPermissions") == []
    assert allowed("bypassPermissions", ["Read"]) == []
    assert allowed("acceptEdits") == list(PRE_APPROVED_TOOLS)
    assert allowed("acceptEdits", ["Read"]) == list(PRE_APPROVED_TOOLS)
    assert allowed("acceptEdits", []) == []


def test_make_backend_reads_the_mode_from_config_and_an_argument_still_wins():
    """Resolved inside the factory, so both production call sites get it.

    `core/runtime.py` (coder) and `review/reviewer.py` (reviewer) are the two,
    and both pass `config=`. The reviewer needs this as much as the coder: it
    runs the test suite through Bash.
    """
    from no_human.agent.backend import make_backend

    assert make_backend(
        model="m", config={}).permission_mode == "bypassPermissions"
    assert make_backend(
        model="m", config={"llm": {"permission_mode": "acceptEdits"}},
    ).permission_mode == "acceptEdits"
    assert make_backend(
        model="m", config={}, permission_mode="acceptEdits",
    ).permission_mode == "acceptEdits"
