"""Reproduction tests for the PR #497 flakiness fix: two tests in this repo
used to assert a WALL-CLOCK bound (`time.monotonic()` / `time.process_time()`)
around a fixed number of operations, so a shared/contended CI runner could
push the measured duration over the bound without anything about the code
under test having changed — three retries on one unchanged commit is the
incident this class of test produced (PR #497).

``tests/test_wake_tick_does_not_stall_scheduler.py::test_the_same_bound_holds_at_a_scaled_down_timeout``
and ``tests/test_guard.py::test_the_gate_mention_scan_is_not_quadratic`` were
reformulated to assert MECHANISM instead of duration: recorded per-call
timeouts and reaped-process counts for the scheduler
(`src/no_human/vcs/pr_watcher.py:135`, `src/no_human/blockers/wake.py:466-473`,
`src/no_human/core/scheduler.py:2161-2177` — no aggregate timeout wraps the
tick, so the bound is arithmetic: N calls x one fixed per-call timeout each),
and a count of characters handed to `guard._unmask` for the gate-mention scan
(`src/no_human/agent/guard.py:1784-1792` — unmasked once, hoisted above the
per-segment loop).

This file proves those two reformulated tests actually demonstrate a
real behavioral difference, in the one way that is mechanically possible
under a "no production code changes" fix: `src/no_human/testing/repro_gate.py`
builds its "fails-before" worktree by checking out the merge-base commit and
then copying onto it ONLY the files that contain the declared repro test
IDs (`_test_files`). A repro test declared INSIDE
`tests/test_wake_tick_does_not_stall_scheduler.py` or `tests/test_guard.py`
itself would, at "fails-before" time, have that whole file overwritten with
its OWN already-reformulated content — so it could never observe the base
commit's original wall-clock-based version of itself; production code is
identical either way (this fix touches no `src/` file), so no test declared
in either of those two files can mechanically differ between the two runs.

Declaring the repro tests HERE, in a third file that is the only one named in
`.no_human/repro_tests.json`, means only THIS file gets copied onto the base
worktree — `tests/test_wake_tick_does_not_stall_scheduler.py` and
`tests/test_guard.py` stay at the base commit's ORIGINAL (wall-clock-based)
content there, while `import`ing them here resolves, in each tree, to
whichever version of the target test actually lives in that tree. Each test
below then injects genuine extra cost (real event-loop delay for the
scheduler test; real CPU burn for the guard test) into a shared dependency
of the imported target test, calls that target test directly, and asserts
that the injection breaks the OLD wall-clock version (proving the fails-
before requirement is real, not vacuous) while the reformulated version
remains correct regardless (proving passes-after). Neither injection touches
`src/`; both are local to this file and undone via `monkeypatch`.

Both tests here are `@pytest.mark.slow` — the injected delay/CPU-burn is the
whole point (it is what makes the old assertion deterministically false), so
these are inherently expensive (tens of seconds) by design, not something to
pay on every contributor's PR. The repro gate (`src/no_human/testing/
repro_gate.py`) selects tests by explicit node id, not by `-m` marker
expression, so marking these `slow` does not stop the gate from running them;
it only keeps them out of the default `pull_request` lane.
"""
from __future__ import annotations

import asyncio

import pytest

from no_human.agent import guard as _guard

from tests.test_guard import (
    test_the_gate_mention_scan_is_not_quadratic as _target_guard_test,
)
from tests.test_wake_tick_does_not_stall_scheduler import (
    hang_cli as _target_hang_cli,
    test_the_same_bound_holds_at_a_scaled_down_timeout as _target_scheduler_test,
)

#: pytest resolves fixture *names* against the importing module's namespace,
#: so re-exporting `hang_cli` under its original name makes it usable as a
#: fixture here too — same recipe as importing a fixture from a shared
#: conftest, just across two peer test files instead.
hang_cli = _target_hang_cli


@pytest.mark.slow
async def test_the_scaled_down_scheduler_test_fails_at_base_and_passes_here(
    store, hang_cli, monkeypatch,
):
    """Injects real extra per-call delay into the shared, real
    `asyncio.wait_for` (small timeouts only — anything above 0.1s is an
    outer hang-guard, not a per-forge-call bound, in both the old and new
    version of the target test, so it is left alone) and then calls the
    ACTUAL target test function, imported fresh from whichever tree this
    runs in.

    At base (this repo's commit prior to this fix): the target test's
    `_run_the_bound_check` awaits `sched.tick()` under a real
    `time.monotonic()` clock and asserts `elapsed <= outer_bound` (2.0s) for
    20 parked tasks at a 0.05s per-call bound (nominal ~1.0s of real work).
    The extra ~0.15s injected onto each of the 20 per-call waits adds ~3.0s
    of real, unavoidable wall-clock time — enough to push `elapsed` past
    2.0s regardless of runner speed — so the imported target test raises
    `AssertionError`, which propagates out of this test: red, as
    fails-before requires (this is not "the runner is busy": it's a fixed,
    deterministic 3.0s injected via `asyncio.sleep`, not contention).

    On this branch: the reformulated target test asserts three counts (see
    the module docstring) — none of them is a duration, so the same
    injected delay changes nothing about what is asserted, and the test
    passes: green, as passes-after requires.
    """
    real_wait_for = asyncio.wait_for

    async def delayed_wait_for(aw, timeout=None):
        try:
            return await real_wait_for(aw, timeout)
        finally:
            # Only the short, per-forge-call bound gets the extra delay —
            # the outer hang-guard/backstop wait (12s at base, 30s here) is
            # left alone so this injection cannot itself wedge the test.
            if timeout is not None and timeout <= 0.1:
                await asyncio.sleep(0.15)

    monkeypatch.setattr(asyncio, "wait_for", delayed_wait_for)

    await _target_scheduler_test(store, hang_cli, monkeypatch)


@pytest.mark.slow
def test_the_guard_quadratic_test_fails_at_base_and_passes_here(monkeypatch):
    """Injects real, deterministic extra CPU cost into `guard._unmask`
    (production code, unchanged by this fix — the injection lives entirely
    in this test) proportional to the length of the text being unmasked,
    then calls the ACTUAL target test function, imported fresh from
    whichever tree this runs in.

    At base: the target test measures `time.process_time()` around
    `guard.evaluate()` for a 400- and an 800-line script and asserts
    `large < 3.0` (CPU seconds) and `large / small < 3.0` (ratio). The
    injected burn is real CPU work (a busy loop), so it shows up in
    `process_time()` regardless of anything about the fix: on this
    checkout it pushes the 800-line run's CPU time past 3.0s on its own
    (measured: ~3.9s with this injection active, vs ~0.16s without it) —
    deterministic, not a contention artifact — so the imported target test
    raises `AssertionError`: red, as fails-before requires.

    On this branch: the reformulated target test counts the characters
    handed to `guard._unmask` during one `guard.evaluate` call — a pure
    function of the code path taken, which the injected CPU burn does not
    change (it adds cost to each call, not calls or their arguments), so
    the same injection leaves the char counts, and the test's verdict,
    unchanged: green, as passes-after requires.
    """
    real_unmask = _guard._unmask

    def bursty_unmask(tok, table):
        # Real CPU work, scaled with input length — deliberately far larger
        # than `_unmask`'s own cost so the injected slowdown dominates the
        # measurement regardless of machine speed.
        burn = len(tok) * 2000
        acc = 0
        for i in range(burn):
            acc += i * i
        return real_unmask(tok, table)

    monkeypatch.setattr(_guard, "_unmask", bursty_unmask)

    _target_guard_test()
