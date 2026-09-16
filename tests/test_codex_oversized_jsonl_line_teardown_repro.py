"""Repro control for the stdout-drain teardown fix added to
``test_codex_oversized_jsonl_line.py`` (see that module's docstring for the
full CPython 3.12 ``_call_connection_lost``/paused-reader mechanism).

This lives in its OWN file rather than next to the fix on purpose: the fix
is entirely test code (there is no production change to diff against), so
a before/after check needs something that genuinely differs between the
unfixed and fixed trees. A test confined to
``test_codex_oversized_jsonl_line.py`` itself cannot demonstrate that,
because that whole file is what's being compared — it is identical on both
sides of any such comparison by construction. Importing the fix's own
extracted helper, ``_drain_stdout_then_wait``, gives a real difference: the
helper does not exist before the fix (this file fails to collect —
``ImportError``) and exists and works after it (this file passes).

This directly exercises the production of the actual fix (not a
reimplementation of it): it builds the same kind of paused-stdout hazard,
registers a ``wait()`` waiter while the child is still alive (the same
shape that hung before the fix), kills the child, then calls
``_drain_stdout_then_wait`` and asserts both it and the pre-registered
waiter resolve — proving the transport actually gets unstuck rather than
merely that the import succeeds.
"""

from __future__ import annotations

import asyncio

from tests.test_codex_oversized_jsonl_line import (
    _drain_stdout_then_wait,
    _write_fake_cli,
)

_BODY_FLOOD_THEN_LINGER = (
    'emit({"type": "thread.started", "thread_id": "th_1"})\n'
    'sys.stdout.write("x" * 2_000_000)\n'  # never newline-terminated
    'sys.stdout.flush()\n'
    'import time; time.sleep(30)\n'  # stay alive until the test kills us
)


async def test_drain_stdout_then_wait_reaps_a_process_paused_by_unread_output(
        tmp_path):
    cli = _write_fake_cli(tmp_path, _BODY_FLOOD_THEN_LINGER, name="fake-flood-repro")
    proc = await asyncio.create_subprocess_exec(
        cli,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=4096,
    )
    assert proc.stdin is not None and proc.stdout is not None
    waiter = None
    try:
        proc.stdin.write(b"prompt\n")
        await asyncio.wait_for(proc.stdin.drain(), 30)
        proc.stdin.close()

        first = await asyncio.wait_for(proc.stdout.readline(), 30)
        assert first, "the small thread.started line must read fine first"

        for _ in range(1000):
            if getattr(proc.stdout, "_paused", False):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(
                "proc.stdout never paused its transport; this probe proves "
                "nothing until the child writes enough to trigger it")
        assert len(proc.stdout._buffer) > 0

        # Register the waiter BEFORE kill, while the child is provably
        # alive: this is exactly the shape that never woke pre-fix, since
        # only _call_connection_lost (every pipe disconnecting) resolves it.
        waiter = asyncio.ensure_future(proc.wait())
        await asyncio.sleep(0)

        proc.kill()

        result = await asyncio.wait_for(
            asyncio.shield(_drain_stdout_then_wait(proc, timeout=5)), 10)
        assert result == -9

        waited = await asyncio.wait_for(asyncio.shield(waiter), 5)
        assert waited == -9
    finally:
        if proc.returncode is None:
            proc.kill()
        try:
            await asyncio.wait_for(proc.stdout.read(), 5)
        except Exception:  # noqa: BLE001
            pass
        if waiter is not None:
            await asyncio.wait_for(waiter, 10)
        else:
            await asyncio.wait_for(proc.wait(), 10)
