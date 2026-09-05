"""A codex child that emits nothing but text must not hold a worker forever.

THE HOLE. `CodexBackend.stream()`'s `turns` counter (`codex_backend.py`,
inside the `for event in self._translate(msg):` loop) advances **only** on
`event.kind == "tool_use"` — a `text`/`thinking`/`usage` event never
increments it. The `max_turns` break therefore never fires for a session that
emits nothing but `agent_message` items (or `thinking`, or `usage`), whatever
envelope they arrive in (the modern `item.completed` shape or the legacy
`{"msg": {"type": "agent_message"}}` one — both funnel through the same
`_translate` yield site). The orchestrator's own watchdog
(`orchestrator._await_coder_turn`) does not catch this either: it bounds
INACTIVITY, and every yielded event — including a text one — stamps
`_last_progress_at`, so an endless flood keeps refreshing it forever. Net: a
child that loops on assistant text holds a pool worker indefinitely, with no
existing bound (`_STDOUT_LIMIT` / `_LINE_ACCUM_CAP` cap a single LINE, not the
COUNT of lines) catching it.

THE FIX. A module-level `_EVENTS_PER_TURN` / `_MAX_STREAM_EVENTS` pair and an
`_event_cap(max_turns)` helper give the stream an absolute ceiling on
EMITTED events, independent of `turns`. Crossing it ends the session exactly
like an ordinary turn exhaustion: `stop_reason="max_turns"`, the child killed
via the existing `finally: _kill_and_reap(proc)`.

TWO ARMS, same shape as `tests/test_codex_oversized_jsonl_line.py`: most
tests drive `stream()`/`run()` over `_fake_codex`'s in-memory
`asyncio.StreamReader` (fast, deterministic, no real binary needed) to
characterize the bound precisely; one test drives a REAL subprocess whose
body is a genuine `while True: emit(...)` — the only way to prove a worker
is actually released in bounded wall-clock rather than merely asserting the
counter logic in isolation. That real-subprocess test is the one that would
hang forever on today's code.
"""

from __future__ import annotations

import asyncio
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from no_human.agent import codex_backend as cx
from no_human.agent.backend import AgentEvent
from no_human.core.orchestrator import _classify_error
from tests.test_codex_backend import (
    FAKE_ENV,
    _HAPPY,
    _PUSH_TO_MAIN,
    _fake_codex,
    _run,
    _stub_cli,
)


# --------------------------------------------------------------------------- #
# 1. The cap itself — a pure function, unit-tested directly.                   #
# --------------------------------------------------------------------------- #


def test_the_event_cap_is_max_turns_times_the_per_turn_factor():
    assert cx._event_cap(0) == cx._MAX_STREAM_EVENTS
    assert cx._event_cap(-1) == cx._MAX_STREAM_EVENTS
    assert cx._event_cap(80) == 80 * cx._EVENTS_PER_TURN


# --------------------------------------------------------------------------- #
# 2. The fake-stream arm: fast, deterministic, exercises the exact counter.    #
# --------------------------------------------------------------------------- #


def _agent_message(i: int) -> dict:
    """One modern-envelope text-only event (`item.completed`/`agent_message`)
    — the shape that never increments `turns`."""
    return {"type": "item.completed",
            "item": {"id": f"i{i}", "type": "agent_message",
                      "text": f"spam {i}"}}


def _legacy_agent_message(i: int) -> dict:
    """The legacy `{"msg": {"type": "agent_message"}}` envelope — a
    DIFFERENT parse path (`_translate`'s `legacy in (...)` branch) that must
    be bounded the same way, since both funnel to the same yield site."""
    return {"msg": {"type": "agent_message", "message": f"spam {i}"}}


def test_an_endless_agent_message_stream_terminates_the_session(monkeypatch):
    monkeypatch.setattr(cx, "_EVENTS_PER_TURN", 2)
    max_turns = 3
    cap = cx._event_cap(max_turns)
    assert cap == 6
    lines = [_agent_message(i) for i in range(cap * 3)]

    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
                        max_turns=max_turns)

    assert result.stop_reason == "max_turns"
    assert result.is_error is True
    assert str(cap * 3) not in result.final_text  # emitted count, not total fed
    assert str(cap) in result.final_text
    assert proc.killed is True


def test_the_flood_text_names_the_emitted_count_and_the_cap(monkeypatch):
    monkeypatch.setattr(cx, "_EVENTS_PER_TURN", 2)
    max_turns = 3
    cap = cx._event_cap(max_turns)
    lines = [_agent_message(i) for i in range(cap * 3)]

    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
                     max_turns=max_turns)

    assert str(cap) in result.final_text, result.final_text
    # The emitted count at the moment of breach is exactly the cap (the loop
    # stops as soon as it is reached, not after draining the whole feed).
    assert str(cap) in result.final_text


def test_the_legacy_msg_envelope_floods_are_bounded_the_same_way(monkeypatch):
    monkeypatch.setattr(cx, "_EVENTS_PER_TURN", 2)
    max_turns = 3
    cap = cx._event_cap(max_turns)
    lines = [_legacy_agent_message(i) for i in range(cap * 3)]

    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
                        max_turns=max_turns)

    assert result.stop_reason == "max_turns"
    assert result.is_error is True
    assert proc.killed is True


def test_the_event_count_never_exceeds_the_cap_plus_the_terminal_result(
        monkeypatch):
    monkeypatch.setattr(cx, "_EVENTS_PER_TURN", 2)
    max_turns = 3
    cap = cx._event_cap(max_turns)
    lines = [_agent_message(i) for i in range(cap * 5)]

    events: list[AgentEvent] = []
    _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
        max_turns=max_turns, on_event=events.append)

    assert events[-1].kind == "result"
    non_terminal = [e for e in events if e.kind != "result"]
    assert len(non_terminal) <= cap, (len(non_terminal), cap)


def test_a_turn_unbounded_session_still_has_an_absolute_event_ceiling(
        monkeypatch):
    monkeypatch.setattr(cx, "_MAX_STREAM_EVENTS", 4)
    lines = [_agent_message(i) for i in range(50)]

    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
                        max_turns=0)

    assert result.stop_reason == "max_turns"
    assert result.is_error is True
    assert proc.killed is True


def test_a_flooded_stream_is_recorded_as_a_max_turns_failure(monkeypatch):
    """Producer/consumer pinned together, mirroring
    `test_an_unassemblable_line_is_a_recorded_infra_failure` in
    `tests/test_codex_oversized_jsonl_line.py`: the routing classifier must
    agree with what `stream()` actually produced."""
    monkeypatch.setattr(cx, "_EVENTS_PER_TURN", 2)
    max_turns = 3
    cap = cx._event_cap(max_turns)
    lines = [_agent_message(i) for i in range(cap * 3)]

    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
                     max_turns=max_turns)

    assert _classify_error(result.stop_reason, result.final_text) == "max_turns"


# --------------------------------------------------------------------------- #
# 3. No regression: ordinary sessions, and genuine turn exhaustion, unchanged. #
# --------------------------------------------------------------------------- #


def test_a_normal_session_is_untouched_by_the_bound(monkeypatch):
    """Production constants, the existing `_HAPPY` fixture from
    `tests/test_codex_backend.py`: every event still delivered, `num_turns`
    unchanged (tool events only), the session ends `end_turn` as before."""
    events: list[AgentEvent] = []
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, _HAPPY,
                     on_event=events.append)

    assert result.stop_reason == "end_turn"
    assert result.is_error is False
    assert result.num_turns == 2  # one command, one file change — unchanged
    kinds = [e.kind for e in events]
    assert kinds[-1] == "result"
    assert "thinking" in kinds and "text" in kinds
    assert "tool_use" in kinds and "tool_result" in kinds and "usage" in kinds


def test_a_real_max_turns_exhaustion_keeps_its_own_text(monkeypatch):
    """A tool-heavy stream that exhausts `max_turns` the ORDINARY way (via
    `turns`, not the event cap) must still yield its own, pre-existing text —
    proving the new flood text did not silently replace it."""
    lines = [
        {"type": "item.started", "item": {"id": f"i{n}", "type":
                                          "command_execution", "command": ["ls"]}}
        for n in range(10)
    ]
    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
                        max_turns=3)

    assert result.stop_reason == "max_turns"
    assert result.is_error is True
    assert "maximum number of turns" in result.final_text
    assert "event cap" not in result.final_text
    assert proc.killed is True


def test_a_guard_violation_still_wins_over_the_event_cap(monkeypatch):
    """A forbidden-path tool call must still terminate as `stop_reason="guard"`
    even when the event cap is ALSO breached in the same pass — the flood
    check must not clobber a stop_reason a preceding branch already set."""
    monkeypatch.setattr(cx, "_EVENTS_PER_TURN", 1)
    monkeypatch.setattr(cx, "_MAX_STREAM_EVENTS", 1)
    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch,
                        _PUSH_TO_MAIN, max_turns=0)

    assert result.stop_reason == "guard"
    assert result.is_error is True
    assert proc.killed is True


# --------------------------------------------------------------------------- #
# 4. The real-subprocess arm: proves a worker is released, not just counted.   #
# --------------------------------------------------------------------------- #

_FAKE_CLI_HEADER = textwrap.dedent(
    '''\
    #!{python}
    import json, os, sys
    sys.stdin.read()  # discard the prompt; codex exec reads it all upfront,
                       # there is no request/response handshake to answer.
    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()
    {body}
    '''
)


def _write_fake_cli(tmp_path, body: str, *, name: str = "fake-codex-flood") -> str:
    """A real, executable ``codex`` stand-in — same pattern as
    ``tests/test_codex_oversized_jsonl_line.py``'s ``_write_fake_cli``: not
    reproduced by import (that file's helper is itself module-private), kept
    identical here to exercise the exact `create_subprocess_exec` + real
    pipe call site the incident this bound fixes actually hangs at."""
    path = tmp_path / name
    path.write_text(_FAKE_CLI_HEADER.format(python=sys.executable, body=body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


_BODY_ENDLESS_AGENT_MESSAGE = textwrap.dedent(
    '''\
    emit({"type": "thread.started", "thread_id": "th_flood"})
    while True:
        emit({"type": "item.completed",
              "item": {"type": "agent_message", "text": "spam"}})
    '''
)


async def test_a_child_that_never_stops_emitting_is_killed_not_awaited_forever(
        tmp_path, monkeypatch):
    """THE test that would hang forever on today's code: a real subprocess
    whose body is `while True: emit(agent_message)`, with no tool call ever —
    `turns` never advances, so only the event cap can end this session. Run
    inside a generous but FINITE `asyncio.wait_for`: if the bound is broken,
    this raises `TimeoutError` instead of returning a result."""
    monkeypatch.setattr(cx, "_EVENTS_PER_TURN", 20)
    cli = _write_fake_cli(tmp_path, _BODY_ENDLESS_AGENT_MESSAGE)
    _stub_cli(monkeypatch, cli=cli)

    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=3),
        30)

    assert result.stop_reason == "max_turns"
    assert result.is_error is True


# --------------------------------------------------------------------------- #
# 5. The docs claim.                                                          #
# --------------------------------------------------------------------------- #


def test_the_docs_state_that_a_text_only_stream_is_bounded():
    docs = (Path(__file__).resolve().parent.parent / "docs" / "BACKENDS.md"
           ).read_text()
    section5 = docs.split("**5.")[1].split("**6.")[0]
    lowered = section5.lower()
    # `"event"` / `"ceiling"` alone are too weak to discriminate: the
    # PRE-EXISTING sentence about the tool-turn counter already contains both
    # ("...from the event stream... the ceiling is crossed..."), so a naive
    # substring check on those two words would pass even without this fix.
    # Anchor on phrasing that only the new, per-turn-event-ceiling sentences
    # introduce.
    assert "events-per-turn" in lowered or "per-turn" in lowered, (
        "docs §5 must state that a text-only stream is bounded by a "
        "separate per-turn event ceiling, not just the tool-turn counter")
    assert 'stop_reason="max_turns"' in section5, (
        "docs §5 must state that hitting the event ceiling is reported the "
        "same way as an ordinary turn exhaustion")
