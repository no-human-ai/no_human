"""`_run_cli` must not hang forever on a dead/slow `gh`/`glab` process.

MEASURED on the operator's live dogfood server (2026-09-14, twice in one day):
`Scheduler.tick()` stalled with idle workers and a full queue —
`tick_stalled: true`, `seconds_since_last_tick: 647`. The chain: `tick()`
awaits `WakeWatcher.tick()`, which loops every parked task SEQUENTIALLY and
calls `pr_watcher._run_cli` for each one; `_run_cli`'s own
`await proc.communicate()` had no bound at all, unlike its sibling `_git_rc`
(same file), which already wraps its `communicate()` in
`asyncio.wait_for(..., _GIT_TIMEOUT)`. One slow/hung `gh` call therefore
stalled the whole scheduler for as long as it hung. This file proves the
per-call bound (`_CLI_TIMEOUT`) added to `_run_cli` fixes that half of the
chain; ``tests/test_wake_tick_does_not_stall_scheduler.py`` proves the
sequential-loop half (AC3).

Every assertion that could hang is wrapped in an outer
``asyncio.wait_for(..., N)`` so "fails before the fix" reads as a clean
red/timeout, never a wedged test session.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

from no_human.vcs import pr_watcher as pw

# `asyncio_mode = "auto"` (pyproject.toml) auto-runs every `async def test_*`
# here; the module has a mix of async and sync (source-inspection) tests, so
# no blanket `pytestmark = pytest.mark.asyncio` is applied.


class _HungProcess:
    """Stand-in for `asyncio.subprocess.Process` whose `communicate()` never
    resolves — models a `gh`/`glab` invocation stuck on a dead TLS connect,
    an auth prompt reading from a closed stdin, or a non-terminating
    `--paginate` walk."""

    def __init__(self, argv: list[str]):
        self.argv = list(argv)
        self.pid = 424242
        self.returncode = None
        self.killed = False
        self._never = asyncio.Event()

    async def communicate(self):
        await self._never.wait()  # resolves never
        return b"", b""  # pragma: no cover — unreachable

    def kill(self):
        self.killed = True


@pytest.fixture
def hang_cli(monkeypatch):
    """Patch `asyncio.create_subprocess_exec` (NOT `_run_cli`) so every call
    `_run_cli` makes spawns a `_HungProcess` and records its argv — the only
    honest way to exercise `_run_cli`'s own bound rather than mocking it
    away. Also monkeypatches `_CLI_TIMEOUT` down to 0.05s so a hang resolves
    in well under a second, and turns `os.killpg`/`os.getpgid` into
    recording no-ops so the fake pid (which names no real process) is
    harmless to "reap"."""
    procs: list[_HungProcess] = []
    killpg_calls: list[tuple[int, int]] = []

    async def fake_create_subprocess_exec(*argv, **kwargs):
        proc = _HungProcess(argv)
        procs.append(proc)
        return proc

    def fake_getpgid(pid):
        return pid

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(os, "getpgid", fake_getpgid)
    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(pw, "_CLI_TIMEOUT", 0.05)
    return procs, killpg_calls


async def test_a_hanging_cli_call_returns_none_within_the_bound(hang_cli, caplog,
                                                                 monkeypatch):
    """AC1. `_run_cli` internally raises `asyncio.TimeoutError` (via
    `asyncio.wait_for` firing) and converts it, at its own boundary, to the
    `None` every other failure mode already returns — no new exception
    reaches callers, matching the documented `str | None` contract.

    Converted from `elapsed < 1.0`: the outer `asyncio.wait_for(..., 5)`
    below is the test's own hang-watchdog (kept, deliberately generous, a
    requested ceiling not a speed claim) — a contended box that legitimately
    took over a second to unwind the timeout and log it would trip the old
    bound despite `_run_cli` behaving exactly as documented. The actual
    claim this test exists for is that `_run_cli` opens its OWN internal
    bound at exactly `_CLI_TIMEOUT`, not at some larger, accidentally
    inherited timeout — spied directly on `asyncio.wait_for`, the primitive
    `_run_cli` uses to open it.
    """
    procs, _ = hang_cli
    opened_timeouts: list[float] = []
    original_wait_for = pw.asyncio.wait_for

    async def _recording_wait_for(fut, timeout=None, **kwargs):
        opened_timeouts.append(timeout)
        return await original_wait_for(fut, timeout=timeout, **kwargs)

    monkeypatch.setattr(pw.asyncio, "wait_for", _recording_wait_for)

    with caplog.at_level(logging.WARNING, logger="no_human.pr_watcher"):
        result = await asyncio.wait_for(pw._run_cli(["gh", "pr", "view", "1"]), 5)

    assert result is None
    assert pw._CLI_TIMEOUT in opened_timeouts, (
        f"_run_cli never opened its own bound at _CLI_TIMEOUT "
        f"({pw._CLI_TIMEOUT}); saw {opened_timeouts}")
    assert len(procs) == 1
    assert any("timed out" in rec.message for rec in caplog.records)


async def test_the_hung_process_is_reaped(hang_cli):
    procs, killpg_calls = hang_cli
    await asyncio.wait_for(pw._run_cli(["gh", "pr", "view", "1"]), 5)
    assert killpg_calls, "os.killpg was never called on the hung process group"
    assert procs[0].killed, "proc.kill() was never called as the fallback reap"


def test_the_timeout_constant_is_documented_and_aligned():
    """AC2. 120s, aligned with (not exceeding) `_GIT_TIMEOUT`'s precedent,
    with a comment naming why: every call site is a forge NETWORK command,
    where a healthy call is sub-second but a degraded forge's own
    retry/backoff is minutes-scale — 120s is a ceiling on that, not a
    typical-latency budget."""
    assert pw._CLI_TIMEOUT == 120.0
    assert pw._CLI_TIMEOUT <= pw._GIT_TIMEOUT

    with open(pw.__file__, encoding="utf-8") as f:
        src = f.read()
    marker = "_CLI_TIMEOUT = 120.0"
    idx = src.index(marker)
    comment_block = src[max(0, idx - 2500):idx]
    assert "network" in comment_block.lower() or "forge" in comment_block.lower()
    assert "gh" in comment_block and "glab" in comment_block


async def test_write_side_calls_are_bounded_too(hang_cli):
    """AC4, executable rather than commentary. Both a non-idempotent write
    (`post_reply_comment`) and an idempotent, marker-deduped write
    (`upsert_agent_comment`) must fail closed (``False``) inside the bound
    under a hang — there is no timeout exemption for write-side calls."""
    from no_human.vcs import pr_watcher as pw_mod
    procs, _ = hang_cli
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pw_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

        reply_ok = await asyncio.wait_for(
            pw_mod.post_reply_comment("host/o/r#5", "x"), 5)
        assert reply_ok is False

        upsert_ok = await asyncio.wait_for(
            pw_mod.upsert_agent_comment("host/o/r#5", "x", key="ci_gate"), 5)
        assert upsert_ok is False

    all_argv = [p.argv for p in procs]
    assert any("comment" in a for a in all_argv), "post_reply_comment never shelled out"
    assert any("-X" in a and "POST" in a for a in all_argv), \
        "upsert_agent_comment's create fallback never used -X POST"

    # Pin that there never is an exemption parameter on `_run_cli` itself —
    # a future write-only bypass would be exactly the hole AC4 closes.
    import inspect
    sig = inspect.signature(pw_mod._run_cli)
    assert list(sig.parameters) == ["cmd"]


async def test_timeout_never_reads_as_a_positive_signal(hang_cli, monkeypatch):
    """Nothing in this change makes a timeout look like a real answer: under
    a hang, `default_pr_mergeable` degrades to the same "unknown" shape as
    every other `_run_cli` failure (never e.g. `"MERGEABLE"`), and
    `default_pr_state` degrades to `""` (never e.g. `"OPEN"`/`"MERGED"`)."""
    monkeypatch.setattr(pw.shutil, "which", lambda name: "/usr/bin/gh")

    mergeable = await asyncio.wait_for(pw.default_pr_mergeable("o/r#5"), 5)
    assert mergeable == {"mergeable": "", "mergeStateStatus": ""}

    state = await asyncio.wait_for(pw.default_pr_state("o/r#5"), 5)
    assert state == ""


def test_no_comment_claims_the_per_call_bound_bounds_the_tick():
    """AC5, machine-enforced. The `_CLI_TIMEOUT` block must carry the same
    disclaimer shape as `_GIT_TIMEOUT`'s (this file, `_git_rc`): it bounds
    ONE invocation, not a `WakeWatcher` tick or a `Scheduler.tick`. And no
    comment/docstring anywhere near it may claim otherwise."""
    with open(pw.__file__, encoding="utf-8") as f:
        src = f.read()
    idx = src.index("_CLI_TIMEOUT = 120.0")
    block = src[max(0, idx - 2500):idx]

    assert "ONE invocation" in block or "one invocation" in block.lower()
    assert "does not bound" in block.lower() or "does NOT bound" in block

    # The disclaimer must explicitly name what it does NOT bound.
    assert "watcher" in block.lower() and "tick" in block.lower()

    # And it must not, anywhere nearby, claim to bound Scheduler.tick itself
    # (only that it does NOT).
    claim_region = block.lower()
    not_bound_idxs = [i for i in range(len(claim_region))
                       if claim_region.startswith("does not bound", i)]
    # Every mention of "scheduler.tick" near the constant must be inside a
    # "does not bound ... Scheduler.tick" disclaimer sentence, not a bare
    # positive claim.
    tick_mentions = [i for i in range(len(claim_region))
                      if claim_region.startswith("scheduler.tick", i)]
    for t in tick_mentions:
        assert any(n < t < n + 200 for n in not_bound_idxs), (
            "a mention of Scheduler.tick near _CLI_TIMEOUT is not wrapped in "
            "a 'does not bound' disclaimer")


async def test_normal_call_is_unaffected(monkeypatch):
    """Guards the 20+ existing `_run_cli`-monkeypatching tests in
    `tests/test_pr_watcher.py`: a non-hanging process still returns decoded
    stdout exactly as before."""

    class _FastProcess:
        returncode = 0
        pid = 1

        async def communicate(self):
            return b"hello\n", b""

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return _FastProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    result = await asyncio.wait_for(pw._run_cli(["gh", "pr", "view", "1"]), 5)
    assert result == "hello\n"
