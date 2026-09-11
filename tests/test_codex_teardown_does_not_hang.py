"""`stream()`'s teardown must not strand a worker slot forever.

MEASURED (2026-08-25). The `finally` in `CodexBackend.stream()` killed the
child and then did an UNBOUNDED `await proc.wait()`. `proc.stdout` is a
`StreamReader` with an explicit `limit`; once its buffer exceeds that limit it
PAUSES its transport, and a paused read transport never sees EOF, so the pipe
never closes and `_UnixSubprocessTransport` never finishes. The wait then
blocks forever even though the child is already dead — reproduced standalone
with `returncode=-9` still hanging past 25 seconds:

    NO drain, NO bound   -> proc.wait() DID NOT RETURN in 25.00s (returncode=-9)
    drain only           -> returned code=-9 after 0.00s
    drain + bound        -> returned code=-9 after 0.00s

SIGKILL is not blockable, so the child was dead in every row; what hung was
asyncio's transport bookkeeping. The gap is categorical — instant or never —
which is why a wall-clock assertion is a legitimate discriminator here and
would not be for an ordinary slow path.

ONLY THE BOUND IS CARRIED, and only because only the bound could be pinned.
A stdout drain also fixes the mechanism (0.00s instead of never) but no test
here could be made to go red when it was removed — because `stream()` could
not be driven into the paused state at all — so it would have shipped as a
line of code nothing holds in place, justified by a hang nobody has shown this
function can reach. A test asserting merely that the teardown RETURNS was
written, measured against unmodified main, found to pass there too, and
deleted: a test that cannot fail advertises coverage that does not exist.
See ticket b7090c45 for the open question.
"""

import asyncio
import re
import pathlib
import time

import pytest

import no_human.agent.codex_backend as cx

from .test_codex_oversized_jsonl_line import (  # noqa: F401
    FAKE_ENV, _stub_cli, _write_fake_cli,
)

pytestmark = pytest.mark.asyncio


# Small enough that a modest write pauses the reader, so the test moves
# kilobytes rather than the production 10 MiB. A test-local literal: deriving
# it from cx._STDOUT_LIMIT would make the test blind to changes in it.
_TINY_STDOUT_LIMIT = 4096

# The budget the TESTS in this repo's codex files impose. NOT "every real
# caller": production budgets on `backend.run(...)` are 120s
# (`orchestrator.py` nudge and supervisor challenge), 300s (intake evaluator)
# and 600s (eval judge), and the main coder path has NO `wait_for` at all. 30s
# is the tightest budget any caller here applies, which makes it the right
# ceiling to assert against — but calling it universal was wrong. Tests in
# tests/test_codex_oversized_jsonl_line.py wrap it in
# `asyncio.wait_for(..., 30)`, so a teardown bound only helps if the whole
# teardown finishes well inside this. Used by the wall-clock assertion below,
# which is what makes `_TEARDOWN_WAIT`'s VALUE load-bearing rather than
# decorative: at 60 the bound is inert (measured: the defect still reproduces
# at 30.3s) while every "is it bounded?" test stays green.
_CALLER_BUDGET_SECONDS = 30.0
_TEARDOWN_MUST_FINISH_WITHIN = 15.0


def _cli_that_floods_then_lingers(tmp_path):
    """Writes far more than the reader's limit, never terminates the line, and
    stays alive — so `stream()` must kill it and then tear down against a
    reader whose transport is paused."""
    body = (
        'emit({"type": "thread.started", "thread_id": "th_1"})\n'
        'sys.stdout.write("x" * 2_000_000)\n'
        'sys.stdout.flush()\n'
        'import time; time.sleep(30)\n'
    )
    return _write_fake_cli(tmp_path, body, name="fake-codex-flood")


async def test_a_wait_that_never_resolves_still_lets_the_teardown_finish(
        tmp_path, monkeypatch):
    """A `proc.wait()` that never resolves — for the paused-reader reason or
    any other — must still let the teardown finish and the worker slot go
    back. Modelled by a wait that never completes, which is the state the
    standalone probe measured at `returncode=-9` past 25 seconds."""
    monkeypatch.setattr(cx, "_STDOUT_LIMIT", _TINY_STDOUT_LIMIT)
    monkeypatch.setattr(cx, "_LINE_ACCUM_CAP", 65536)
    _stub_cli(monkeypatch, cli=_cli_that_floods_then_lingers(tmp_path))

    real_create = asyncio.create_subprocess_exec

    async def _create_with_a_hanging_wait(*a, **k):
        proc = await real_create(*a, **k)

        async def _never():
            await asyncio.Event().wait()  # resolves never

        proc.wait = _never
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _create_with_a_hanging_wait)
    monkeypatch.setattr(cx.asyncio, "create_subprocess_exec",
                        _create_with_a_hanging_wait, raising=False)

    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=9), 90)

    assert result.is_error is True


async def test_a_prompt_exit_still_reports_its_real_code(tmp_path, monkeypatch):
    """POSITIVE CONTROL for the SHORT-CIRCUIT half only.

    Without it, a teardown that always short-circuits (`with_code = -9`
    unconditionally) would pass both tests above while throwing away every
    real exit code — verified: that mutation turns this red.

    It does NOT catch "a bound that always fires". An earlier docstring
    claimed it did; then a corrected version claimed
    `test_the_teardown_finishes_well_inside_the_callers_budget` covered it.
    Both were wrong — measured, `_TEARDOWN_WAIT = 0` leaves ALL FIVE tests in
    this file green. The mutation IS caught, by a test in another file that
    neither claim mentioned:

        tests/test_codex_backend.py::test_a_nonzero_exit_with_no_json_still_yields_a_result_event

    The mechanism is guarded; two successive claims about WHAT guards it were
    false. Naming the wrong protector is the same defect as naming none.
    """
    body = (
        'emit({"type": "thread.started", "thread_id": "th_1"})\n'
        'sys.stderr.write("codex blew up\\n")\n'
        'sys.exit(7)\n'
    )
    cli = _write_fake_cli(tmp_path, body, name="fake-codex-exit7")
    _stub_cli(monkeypatch, cli=cli)

    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=9), 30)

    assert result.is_error is True
    assert "7" in result.final_text or "blew up" in result.final_text, (
        result.final_text)


async def test_the_teardown_bound_is_shorter_than_every_callers_budget():
    """The ordering invariant, asserted instead of commented.

    The teardown's true ceiling is `_TEARDOWN_WAIT + _STDERR_DRAIN_WAIT` —
    both waits run on the same exit path — and the callers' budgets live as
    independent literals in another file. Only their ORDER matters, and
    nothing related them: raising `_TEARDOWN_WAIT` to 60 kept every
    "is it bounded?" test green while making the bound completely inert,
    because the caller's own `wait_for` fires first every time. Asserting
    `_TEARDOWN_WAIT` alone was the round-4 finding: widening only the stderr
    drain to 25 kept every test green while a probed teardown ran 30.4s —
    past the tightest 30s budget — so the SUM is what is asserted.
    """
    # READ the real caller literals instead of comparing against a constant
    # this file owns. An earlier version asserted against
    # `_CALLER_BUDGET_SECONDS` — a fourth literal, defined here — so tightening
    # an actual caller's budget to 3 left this test green while the invariant
    # it names was violated. It now parses the budgets the sibling codex tests
    # really apply, so drift on EITHER side of the inequality is caught.
    sibling = pathlib.Path(__file__).with_name("test_codex_oversized_jsonl_line.py")
    budgets = [float(m) for m in re.findall(
        r"asyncio\.wait_for\(\s*(?:.|\n)*?,\s*([0-9]+(?:\.[0-9]+)?)\s*\)",
        sibling.read_text(encoding="utf-8"))]
    assert budgets, "no caller budget parsed — this assertion would be vacuous"
    tightest = min(budgets)
    ceiling = cx._TEARDOWN_WAIT + cx._STDERR_DRAIN_WAIT
    assert ceiling < tightest, (
        f"_TEARDOWN_WAIT + _STDERR_DRAIN_WAIT = {ceiling} is not shorter "
        f"than the tightest budget the callers actually impose ({tightest}s, "
        f"parsed from {sibling.name}), so the teardown cannot finish in time "
        "to rescue anything")
    assert ceiling <= _TEARDOWN_MUST_FINISH_WITHIN


async def test_the_teardown_finishes_well_inside_the_callers_budget(
        tmp_path, monkeypatch):
    """Wall-clock, not just "it returned".

    This is the assertion that makes the VALUE load-bearing. With a
    never-resolving `proc.wait()` the whole run must still come back inside
    the budget a real caller allows; at `_TEARDOWN_WAIT = 60` it would not.
    """
    monkeypatch.setattr(cx, "_STDOUT_LIMIT", _TINY_STDOUT_LIMIT)
    monkeypatch.setattr(cx, "_LINE_ACCUM_CAP", 65536)
    _stub_cli(monkeypatch, cli=_cli_that_floods_then_lingers(tmp_path))

    real_create = asyncio.create_subprocess_exec

    async def _create_with_a_hanging_wait(*a, **k):
        proc = await real_create(*a, **k)

        async def _never():
            await asyncio.Event().wait()

        proc.wait = _never
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _create_with_a_hanging_wait)
    monkeypatch.setattr(cx.asyncio, "create_subprocess_exec",
                        _create_with_a_hanging_wait, raising=False)

    started = time.monotonic()
    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=9),
        _CALLER_BUDGET_SECONDS + 30)
    elapsed = time.monotonic() - started

    assert result.is_error is True
    assert elapsed < _TEARDOWN_MUST_FINISH_WITHIN, (
        f"the teardown took {elapsed:.1f}s, which is not comfortably inside "
        f"the {_CALLER_BUDGET_SECONDS}s a real caller allows — the bound is "
        "present but too large to rescue anything")


async def test_a_teardown_timeout_does_not_manufacture_a_failure(
        tmp_path, monkeypatch):
    """The `-9` fallback, and WHY it needed forcing to test at all.

    The code asserts a teardown timeout "does not manufacture a failure the
    run did not have". Substituting `1` for `-9` left 92 tests green, and two
    further attempts to pin it also stayed green. Measured rather than guessed
    the third time: with a probe reading `proc.returncode` at the end of a
    hanging teardown, it is **0** — `proc.kill()` plus the child watcher reap
    the process before that line runs, so `proc.returncode is not None` and the
    `else` arm never executes. The fallback is defensive code for a race the
    watcher normally wins, which is exactly why no ordinary scenario pins it.

    So the branch is forced here: `returncode` is held at None to put the
    process in the state the fallback exists for. That is the only way to
    assert the property the comment claims, and asserting it is worth more
    than leaving a security-adjacent fallback unexercised.
    """
    monkeypatch.setattr(cx, "_STDOUT_LIMIT", _TINY_STDOUT_LIMIT)
    monkeypatch.setattr(cx, "_LINE_ACCUM_CAP", 65536)
    body = (
        'emit({"type": "thread.started", "thread_id": "th_1"})\n'
        'emit({"type": "turn.completed", "usage": {"input_tokens": 10, '
        '"cached_input_tokens": 0, "output_tokens": 1}})\n'
        'import time; time.sleep(5)\n'
    )
    _stub_cli(monkeypatch, cli=_write_fake_cli(tmp_path, body, name="fake-codex-clean"))

    real_create = asyncio.create_subprocess_exec

    class _Unreaped:
        """Proxies the real process but reports `returncode is None`.

        An earlier version of this test patched `returncode` onto the
        asyncio.subprocess.Process CLASS. That is global for the whole
        interpreter: it leaked past the `finally` and broke three tests in
        tests/test_codex_oversized_jsonl_line.py in the same run. A proxy
        touches nothing outside this test.
        """

        def __init__(self, proc):
            object.__setattr__(self, "_proc", proc)

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_proc"), name)

        @property
        def returncode(self):
            return None

        async def wait(self):
            await asyncio.Event().wait()

    async def _create_unreaped_with_a_hanging_wait(*a, **k):
        return _Unreaped(await real_create(*a, **k))

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _create_unreaped_with_a_hanging_wait)
    monkeypatch.setattr(cx.asyncio, "create_subprocess_exec",
                        _create_unreaped_with_a_hanging_wait, raising=False)

    result = await asyncio.wait_for(
        cx.CodexBackend(env=FAKE_ENV).run("p", cwd=tmp_path, max_turns=9), 90)

    assert "codex exited" not in (result.final_text or ""), (
        "the teardown timeout invented an exit-code failure for a child that "
        f"completed cleanly: {(result.final_text or '')[:200]}")
