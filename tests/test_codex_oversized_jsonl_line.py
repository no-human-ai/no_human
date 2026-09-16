"""A single oversized codex JSONL event must not crash the task.

INCIDENT 2026-08-25 (task 78be079a, attempt 36): `codex_backend.stream()`
read stdout with `proc.stdout.readline()` over the default 65536-byte
asyncio pipe limit. Any single codex event over 64 KiB (routine for a large
tool output, file read, or diff) raised
``ValueError("Separator is not found, and chunk exceed the limit")``,
uncaught, straight out of `stream()`/`run()`/`_await_coder_turn`/
`_run_attempt`/`_drive`/`run_task` — the task crashed in the pool instead of
being recorded as a retryable infra failure. This is codex-only: the Claude
backend goes through the Agent SDK's own transport, not this readline.

The fix (`codex_backend.py`): raise the StreamReader limit to
`_STDOUT_LIMIT` (10 MiB) — the fast path for the overwhelming majority of
events — and, above that, catch `asyncio.LimitOverrunError` in
`_read_jsonl_line` and accumulate via `readexactly(e.consumed)` across
rounds until the newline arrives, up to `_LINE_ACCUM_CAP` (64 MiB) total. A
line that STILL can't be assembled by then (or dies at EOF mid-accumulation)
raises the module's own `_CodexLineTruncated`, which `stream()` catches and
folds into `failure = f"codex stream closed mid-line: {exc}"` — the existing
`orchestrator._classify_error` already treats any failure text containing
"stream closed" as `infra` (see `claude_backend._TRANSPORT_FAILURE_MARKERS`
for the established precedent of that exact string), so this reproduction
needs no orchestrator or scheduler change to route correctly.

Every fake "codex" CLI here is a REAL subprocess (a tiny Python script
written to `tmp_path` and chmod'd executable), not an in-memory double: the
ablation test's whole point is a stdlib exception that only reproduces over
a real OS pipe's chunked delivery, and the other tests share that same
fixture shape so they exercise the exact call site (`create_subprocess_exec`
+ `proc.stdout`) that broke in production. Contrast with
`tests/test_codex_backend.py`'s `_fake_codex`, which backs stdout with a
real (but in-memory-fed) `asyncio.StreamReader` — sufficient for that file's
job of characterizing the NORMALIZER, not this incident's transport layer.

INCIDENT 2026-09 (issue #104, run 35005108921 on 90204bf6): this file's own
``test_the_unfixed_readline_raises_the_exact_asyncio_valueerror`` flaked to
CI's 30-minute ceiling roughly 1/100 runs. Its ``finally`` did
``proc.kill()`` then an unbounded ``await proc.wait()`` with no stdout
drain. On CPython 3.12, ``BaseSubprocessTransport._wait()`` only resolves
its waiter via ``_call_connection_lost``, which only fires once *every*
pipe has disconnected. The fake CLI here still had its oversized line
sitting unread in the ``StreamReader`` buffer; when that buffer exceeds
``2 * limit`` the reader calls ``transport.pause_reading()`` — and a paused
read transport never delivers EOF, so stdout never disconnects and the
already-registered ``wait()`` waiter never wakes, even though the child is
long dead (``returncode == -9``). Draining ``proc.stdout`` to EOF before
``wait()`` resumes the transport, lets EOF arrive, and the reap completes
immediately. This is test-only: production's ``_kill_and_reap`` in
``codex_backend.py`` does not drain stdout either, but bounds the hang via
``_TEARDOWN_WAIT`` instead — a separate, not-carried ticket (b7090c45). No
production behavior changes here.
"""

from __future__ import annotations

import asyncio
import stat
import sys
import textwrap

import pytest

from no_human.agent import codex_backend as cx
from no_human.agent.backend import AgentEvent
from no_human.core.orchestrator import _classify_error
from tests.test_codex_backend import FAKE_ENV, _stub_cli

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


def _write_fake_cli(tmp_path, body: str, *, name: str = "fake-codex") -> str:
    """A real, executable ``codex`` stand-in. ``_command()`` puts this path
    at ``cmd[0]``, so ``stream()`` spawns it as a genuine subprocess and
    reads its genuine stdout pipe — the same call site the incident hit.
    """
    path = tmp_path / name
    path.write_text(_FAKE_CLI_HEADER.format(python=sys.executable, body=body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


async def _drain_stdout_then_wait(proc, *, timeout=30):
    """Bounded stdout-drain-then-reap: the actual fix for the teardown
    deadlock described in the module docstring above. A ``StreamReader``
    left PAUSED by unread output never sees EOF, so its pipe never
    disconnects and a ``wait()`` registered while the child was still alive
    never resolves on CPython 3.12 (``_call_connection_lost`` is the only
    path that wakes ``_exit_waiters``, and it needs every pipe to
    disconnect). Reading ``proc.stdout`` to EOF resumes the transport and
    lets that disconnect happen, so the reap below completes. Swallowing
    the drain's own errors is deliberate here: a drain failure must never
    stand in for whatever exception the caller's own body already raised.
    """
    try:
        await asyncio.wait_for(proc.stdout.read(), timeout)
    except Exception:  # noqa: BLE001
        pass
    return await asyncio.wait_for(proc.wait(), timeout)


_BODY_OVERSIZED_EVENT = textwrap.dedent(
    '''\
    BIG = "x" * 300_000
    emit({"type": "thread.started", "thread_id": "th_1"})
    emit({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": BIG}})
    emit({"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": ["echo", "ok"]}})
    emit({"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "aggregated_output": "ok", "exit_code": 0}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}})
    '''
)


@pytest.fixture
def oversized_event_cli(tmp_path):
    """thread.started (small) -> one 300 KiB agent_message -> a
    command_execution pair -> turn.completed. Shared by the fix's own test
    and the ablation/characterization test below: both need the same
    "small line, then an oversized one" shape, since the bug is specifically
    about a line THAT FOLLOWS successfully-read small lines, not the first
    read of the pipe."""
    return _write_fake_cli(tmp_path, _BODY_OVERSIZED_EVENT)


# ---------------------------------------------------------------------------
# AC1 — the real fix, through the real stream()/run().
# ---------------------------------------------------------------------------


async def test_an_event_over_64_kib_is_parsed_and_does_not_kill_the_stream(
        oversized_event_cli, tmp_path, monkeypatch):
    """RED before the fix: raises
    ``ValueError("Separator is not found, and chunk exceed the limit")``
    from inside stream()'s read loop, uncaught — the exact shape of task
    78be079a attempt 36 (2026-08-25). GREEN after: both the oversized event
    and the following normal event parse, and the session completes
    normally with no exception escaping."""
    _stub_cli(monkeypatch, cli=oversized_event_cli)
    seen: list[AgentEvent] = []

    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run(
            "p", cwd=tmp_path, max_turns=9, on_event=seen.append),
        30)

    assert result.is_error is False, (result.final_text or "")[:300]
    assert result.final_text is not None
    assert len(result.final_text) >= 300_000
    assert result.final_text == "x" * 300_000
    assert result.stop_reason == "end_turn"
    assert result.session_id == "th_1"
    kinds = [e.kind for e in seen]
    assert "text" in kinds, kinds
    assert "tool_use" in kinds, kinds
    assert "tool_result" in kinds, kinds
    assert kinds[-1] == "result", kinds


# ---------------------------------------------------------------------------
# AC2 — the exact stdlib exception, characterized directly (ablation target).
# ---------------------------------------------------------------------------


async def test_the_unfixed_readline_raises_the_exact_asyncio_valueerror(
        oversized_event_cli):
    """Names the SPECIFIC regression, not merely "a failure": this is the
    pre-fix call site (``readline()`` over the DEFAULT 65536-byte limit, no
    ``limit=`` override) hitting the same fake CLI used by the fix's own
    test above. If a future edit reverts the fix (drops the raised
    ``limit=_STDOUT_LIMIT`` and/or the ``LimitOverrunError`` handling), the
    AC1 test above goes red with exactly this exception, from exactly this
    code path."""
    proc = await asyncio.create_subprocess_exec(
        oversized_event_cli,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # deliberately no limit= — the default 65536-byte StreamReader
    assert proc.stdin is not None and proc.stdout is not None
    # Every await below is bounded, like the other async tests in this file.
    # Unbounded, this test deadlocked a whole -n 4 run to CI's 30-minute
    # ceiling: the child was already gone and its stdout pipe was at EOF, yet
    # the loop sat in epoll_wait with nothing left to wake it. See issue #104.
    # A named timeout failure carries a traceback; a silent half hour does not.
    try:
        proc.stdin.write(b"prompt\n")
        await asyncio.wait_for(proc.stdin.drain(), 30)
        proc.stdin.close()

        first = await asyncio.wait_for(proc.stdout.readline(), 30)
        assert first, "the small thread.started line must read fine first"

        with pytest.raises(ValueError,
                            match=r"Separator is not found, and chunk exceed the limit"):
            await asyncio.wait_for(proc.stdout.readline(), 30)
    finally:
        if proc.returncode is None:
            proc.kill()
        # The readline() above left the StreamReader's buffer past 2*limit,
        # so it PAUSED its transport; see the module docstring and
        # _drain_stdout_then_wait for why an undrained wait() can hang.
        await _drain_stdout_then_wait(proc, timeout=30)


# ---------------------------------------------------------------------------
# AC2 control — a deterministic, on-demand reproduction of the deadlock
# itself, independent of CI load (contrast with the ~1/100 flake above: the
# default 65536-byte limit and a single 300 KiB line do not reliably pause
# the reader on this machine — the readline() that raises the ValueError can
# leave the buffer empty depending on exactly how the exception unwinds).
# Forcing a small limit and a large, never-terminated write makes the pause
# unconditional.
# ---------------------------------------------------------------------------

_BODY_FLOOD_THEN_LINGER = (
    'emit({"type": "thread.started", "thread_id": "th_1"})\n'
    'sys.stdout.write("x" * 2_000_000)\n'  # never newline-terminated
    'sys.stdout.flush()\n'
    'import time; time.sleep(30)\n'  # stay alive until the test kills us
)

_PROBE_STDOUT_LIMIT = 4096  # small enough that a few KiB pauses the reader
_PROBE_DEADLOCK_WAIT = 2.0  # long enough to be a real hang, short for CI
_PROBE_DRAIN_WAIT = 5.0


@pytest.mark.parametrize("drain_stdout", [False, True], ids=["no-drain", "drain"])
async def test_a_paused_stdout_deadlocks_the_reap_unless_it_is_drained(
        tmp_path, drain_stdout):
    """Failing-before/passing-after control for the teardown fix above,
    self-contained so it does not depend on CI load or timing: with the
    reader provably paused and the ``wait()`` waiter registered while the
    child is still alive, killing the child and immediately awaiting that
    waiter times out (``no-drain``) unless ``proc.stdout`` is drained to EOF
    first (``drain``), matching ``_drain_stdout_then_wait`` above."""
    cli = _write_fake_cli(tmp_path, _BODY_FLOOD_THEN_LINGER, name="fake-flood")
    proc = await asyncio.create_subprocess_exec(
        cli,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_PROBE_STDOUT_LIMIT,
    )
    assert proc.stdin is not None and proc.stdout is not None
    waiter = None
    try:
        proc.stdin.write(b"prompt\n")
        await asyncio.wait_for(proc.stdin.drain(), 30)
        proc.stdin.close()

        first = await asyncio.wait_for(proc.stdout.readline(), 30)
        assert first, "the small thread.started line must read fine first"

        # Poll (bounded) until the reader has actually paused its transport
        # rather than reading anything more — reading here would defeat the
        # whole point of the probe.
        for _ in range(1000):
            if getattr(proc.stdout, "_paused", False):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail(
                "proc.stdout never set _paused — either the child didn't "
                "write enough to exceed 2*limit, or CPython renamed/removed "
                "StreamReader._paused; this probe proves nothing until "
                "that's addressed")
        assert len(proc.stdout._buffer) > 0

        # Register the waiter BEFORE kill: the child is provably alive here,
        # so this future lands in the transport's _exit_waiters, which only
        # _call_connection_lost (i.e. every pipe disconnecting) can resolve
        # — the same shape as the real finally blocks in this file.
        waiter = asyncio.ensure_future(proc.wait())
        await asyncio.sleep(0)

        proc.kill()

        if drain_stdout:
            await asyncio.wait_for(proc.stdout.read(), _PROBE_DRAIN_WAIT)
            result = await asyncio.wait_for(
                asyncio.shield(waiter), _PROBE_DRAIN_WAIT)
            assert result == -9
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(waiter), _PROBE_DEADLOCK_WAIT)
    finally:
        # Always drain and reap, even in the no-drain branch — otherwise
        # this probe leaks a live transport whose __del__ can raise once the
        # event loop closes (see codex_backend.py's _kill_and_reap comments
        # for the same failure mode in production).
        if proc.returncode is None:
            proc.kill()
        try:
            await asyncio.wait_for(proc.stdout.read(), _PROBE_DRAIN_WAIT)
        except Exception:  # noqa: BLE001
            pass
        if waiter is not None:
            await asyncio.wait_for(waiter, _PROBE_DRAIN_WAIT)
        else:
            await asyncio.wait_for(proc.wait(), _PROBE_DRAIN_WAIT)


# ---------------------------------------------------------------------------
# AC3 — a genuinely unassemblable line degrades to a recorded infra failure.
# ---------------------------------------------------------------------------


async def test_an_unassemblable_line_is_a_recorded_infra_failure(
        tmp_path, monkeypatch):
    """A line that can never be assembled — far more bytes than the
    accumulation cap allows, with no newline in sight — must degrade to a
    recorded, retryable infra failure: never an unhandled exception, and
    never the "crashed in pool" path the scheduler's backstop would log.
    ``_STDOUT_LIMIT``/``_LINE_ACCUM_CAP`` are patched small here (not the
    production 10 MiB / 64 MiB) purely so the test doesn't need to move
    tens of megabytes to hit the cap — the mechanism under test is the same
    ``_read_jsonl_line`` accumulation loop, only the thresholds differ."""
    monkeypatch.setattr(cx, "_STDOUT_LIMIT", 4096)
    monkeypatch.setattr(cx, "_LINE_ACCUM_CAP", 65536)
    body = (
        'emit({"type": "thread.started", "thread_id": "th_1"})\n'
        'sys.stdout.write("x" * 200_000)\n'  # no trailing newline, ever
        'sys.stdout.flush()\n'
        'import time; time.sleep(5)\n'  # stay alive; stream() must kill us
    )
    cli = _write_fake_cli(tmp_path, body, name="fake-codex-truncated")
    _stub_cli(monkeypatch, cli=cli)

    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=9),
        30)

    assert result.is_error is True
    assert result.final_text is not None
    assert "stream closed" in result.final_text, result.final_text[:300]
    assert _classify_error(
        result.stop_reason, result.final_text, result.api_error_status,
    ) == "infra", (result.stop_reason, result.final_text[:300])


async def test_process_death_mid_short_line_is_an_ordinary_failed_attempt(
        tmp_path, monkeypatch):
    """Contrast case: a process that dies partway through a SHORT dangling
    fragment (nowhere near any limit) must NOT be misread as a truncation —
    it is the pre-existing nonzero-exit-code failure path, unaffected by
    this fix. Distinguishes "genuinely unassemblable" from "died before
    writing much of anything"."""
    body = (
        'emit({"type": "thread.started", "thread_id": "th_1"})\n'
        'emit({"type": "item.completed", "item": {"id": "i0", '
        '"type": "agent_message", "text": "partial-before-death"}})\n'
        'sys.stdout.write("{almost-a-line")\n'
        'sys.stdout.flush()\n'
        'os._exit(7)\n'
    )
    cli = _write_fake_cli(tmp_path, body, name="fake-codex-dies")
    _stub_cli(monkeypatch, cli=cli)
    seen: list[AgentEvent] = []

    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run(
            "p", cwd=tmp_path, max_turns=9, on_event=seen.append),
        30)

    assert result.is_error is True
    assert result.final_text is not None
    assert "7" in result.final_text, result.final_text[:300]
    texts = [e.text for e in seen if e.kind == "text"]
    assert texts == ["partial-before-death"]
    assert seen[-1].kind == "result"


# ---------------------------------------------------------------------------
# AC4 — boundary coverage.
# ---------------------------------------------------------------------------

# _STDOUT_LIMIT (10 MiB) is the FAST-PATH boundary, not a correctness edge:
# below it a line reads in one readuntil() call; at/above it
# `_read_jsonl_line` starts accumulating via LimitOverrunError/readexactly,
# in _STDOUT_LIMIT-sized rounds, up to _LINE_ACCUM_CAP (64 MiB). 10 MiB was
# picked (per .no_human/PLAN.md) so ordinary large tool output/diffs never
# touch the accumulation path at all; 64 MiB caps a single line's memory so
# one runaway event can't blow up a daemon process shared by several tasks
# (a flat 256 MiB cap was considered and rejected for that reason). These
# sizes exercise: just under the fast-path limit, exactly at it, just over
# it, a full extra MiB over, exactly one accumulation round, and several
# rounds — all still comfortably under the 64 MiB accumulation cap.
_BOUNDARY_SIZES = [
    65535,
    65536,
    65537,
    1 * 1024 * 1024,
    cx._STDOUT_LIMIT + 1,
    cx._STDOUT_LIMIT * 3,
]


@pytest.mark.parametrize("size", _BOUNDARY_SIZES)
async def test_boundary_sizes_all_round_trip_byte_exact(
        tmp_path, monkeypatch, size):
    body = (
        'emit({"type": "thread.started", "thread_id": "th_1"})\n'
        'emit({"type": "item.completed", "item": {"id": "i0", '
        '"type": "agent_message", "text": "x" * ' + str(size) + '}})\n'
        'emit({"type": "turn.completed", "usage": {"input_tokens": 1, '
        '"cached_input_tokens": 0, "output_tokens": 1}})\n'
    )
    cli = _write_fake_cli(tmp_path, body, name=f"fake-codex-{size}")
    _stub_cli(monkeypatch, cli=cli)

    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=9),
        60)

    assert result.is_error is False, (result.final_text or "")[:300]
    assert result.final_text is not None
    assert len(result.final_text) == size
    assert result.final_text == "x" * size


# ---------------------------------------------------------------------------
# AC5 — positive control: ordinary small events still stream one-by-one.
# ---------------------------------------------------------------------------


async def test_ordinary_small_events_still_stream_one_by_one_not_buffered(
        tmp_path, monkeypatch):
    """Guards against a fix that "works" by reading the whole session before
    yielding anything. The fake CLI sleeps 1.5s between its first and second
    event; a file-existence race against the child process is not a safe
    signal here (the child can create a marker file microseconds after its
    write, long before the parent's asyncio loop gets scheduled to read it —
    that raced and flaked).

    An absolute wall-clock threshold on the FIRST event is not safe either:
    under a loaded parallel test run (`pytest -n 4` across ~10k tests) the
    parent process can itself be scheduling-delayed by hundreds of ms before
    it ever gets to await the child's pipe, which pushed a `first_seen_at <
    0.35` assertion into a false failure with no buffering involved. What
    survives contention is the GAP between the first and second observed
    event: if `stream()` buffered the whole session, both would be yielded
    back-to-back with a near-zero gap regardless of when the parent got
    scheduled; if it streams per line, the gap tracks the child's 1.5s
    sleep. Comparing two of the parent's own clock readings cancels out the
    parent's own scheduling latency, which a single absolute reading cannot."""
    body = (
        'emit({"type": "thread.started", "thread_id": "th_1"})\n'
        'emit({"type": "item.completed", "item": {"id": "i0", '
        '"type": "agent_message", "text": "first"}})\n'
        'import time; time.sleep(1.5)\n'
        'emit({"type": "item.completed", "item": {"id": "i1", '
        '"type": "agent_message", "text": "second"}})\n'
        'emit({"type": "turn.completed", "usage": {"input_tokens": 1, '
        '"cached_input_tokens": 0, "output_tokens": 1}})\n'
    )
    cli = _write_fake_cli(tmp_path, body, name="fake-codex-incremental")
    _stub_cli(monkeypatch, cli=cli)

    async def _watch():
        loop = asyncio.get_event_loop()
        text_event_times = []
        events = []
        async for event in cx.CodexBackend(env=FAKE_ENV).stream(
                "p", cwd=tmp_path, max_turns=9):
            events.append(event)
            if event.kind == "text":
                text_event_times.append(loop.time())
        return events, text_event_times

    events, text_event_times = await asyncio.wait_for(_watch(), 30)

    assert len(text_event_times) == 2, text_event_times
    gap = text_event_times[1] - text_event_times[0]
    assert gap > 0.75, (
        f"the two text events were observed only {gap:.3f}s apart, far "
        "short of the child's 1.5s sleep between writing them — the whole "
        "session is being buffered before anything is yielded")
    texts = [e.text for e in events if e.kind == "text"]
    assert texts == ["first", "second"]
