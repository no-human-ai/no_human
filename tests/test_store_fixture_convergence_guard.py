"""Structural guard for the store-fixture convergence (see .no_human/PLAN.md).

The bug this convergence fixes — aiosqlite's non-daemon worker thread firing
`call_soon_threadsafe` on an already-closed test loop, poisoning an unrelated
xdist worker's next test — only manifests as a visible pytest FAILURE under
`pytest -n 4` combined with promoting `PytestUnhandledThreadExceptionWarning`
to an error (verified empirically this session: run under plain
single-process pytest, with no `-n` and no `-W error::...`, it produces zero
failures and zero warnings even with the leak reintroduced). That means the
race itself cannot be pinned as a reliable single-process repro test.

This guard instead pins the STRUCTURAL invariant the fix establishes — every
aiosqlite `Store` used by a test is owned by exactly one place (the shared
`store`/`store_factory` fixtures in tests/conftest.py, whose teardown runs in
a `finally` on every exit path) — so a regression (a reintroduced local
fixture, or a reintroduced un-guarded body-level close that can be skipped by
a raised exception) fails deterministically, in a single process, regardless
of scheduling.
"""
import glob
import os

# This file's own docstrings/comments quote the patterns being scanned for
# (as prose, not code) — exclude it from its own scan.
_SELF = os.path.relpath(__file__, os.getcwd()).replace(os.sep, "/")


def _local_store_fixtures():
    """Files (other than conftest.py) that still declare `async def store(`."""
    hits = []
    for f in sorted(glob.glob("tests/**/*.py", recursive=True)):
        if f.endswith("conftest.py") or f == _SELF:
            continue
        for i, line in enumerate(open(f).read().splitlines(), start=1):
            if line.startswith("async def store("):
                hits.append(f"{f}:{i}")
    return hits


def _bare_closes():
    """`await store.close()` not preceded by `finally:` within 4 lines — the
    exact classifier from .no_human/PLAN.md's AC2 verification command."""
    bare = []
    for f in sorted(glob.glob("tests/**/*.py", recursive=True)):
        if f == _SELF:
            continue
        lines = open(f).read().splitlines()
        for i, line in enumerate(lines):
            if "await store.close()" in line and not any(
                x.strip().startswith("finally:") for x in lines[max(0, i - 4):i]
            ):
                bare.append(f"{f}:{i + 1}")
    return bare


# The 6 files whose fixture's db filename is referenced elsewhere in the same
# file (PLAN.md's "path-coupled" list) keep a thin local `store` fixture that
# delegates to `store_factory` for a NAMED path, instead of consuming the
# plain shared `store` fixture directly.
_PATH_COUPLED_VARIANTS = {
    "tests/test_landing_actor.py",
    "tests/test_db.py",
    "tests/test_db_concurrency.py",
    "tests/test_start_single_store_connection.py",
    "tests/test_frozen_snapshot_guard.py",
    "tests/test_false_done_completion.py",
}

# The 4 sites the AC2 classifier flags that are manually verified safe:
#   - test_orchestrator_factory_parity.py:84 and
#     test_frozen_snapshot_guard.py:1037 — both `await store.close()` calls
#     sit inside `with contextlib.suppress(...)`, in a helper that unwinds a
#     real-worker boot, not a test body.
#   - test_token_split_schema.py:381 — `store = await Store(...).connect()`
#     is immediately followed by `await store.close()` with zero intervening
#     statements, so there is no exception window in which the close can be
#     skipped.
#   - test_frozen_snapshot_guard.py:1029 — a false positive of the substring
#     classifier: the matched text sits inside a docstring describing the
#     mechanism above ("...skips its `await store.close()`. Left alone..."),
#     not executable code.
_KNOWN_SAFE_BARE_CLOSES = {
    "tests/test_orchestrator_factory_parity.py:84",
    "tests/test_token_split_schema.py:381",
    "tests/test_frozen_snapshot_guard.py:1029",
    "tests/test_frozen_snapshot_guard.py:1037",
}


def test_no_undocumented_local_store_fixtures():
    """AC1: every local `async def store(` fixture outside conftest.py must
    be one of the 6 documented path-coupled variants (each of which
    delegates to `store_factory`, not a bare `Store(...)` connection)."""
    found_files = {h.rsplit(":", 1)[0] for h in _local_store_fixtures()}
    assert found_files == _PATH_COUPLED_VARIANTS, (
        "the set of files declaring a local `store` fixture drifted from "
        f"the documented path-coupled list: found={sorted(found_files)} "
        f"expected={sorted(_PATH_COUPLED_VARIANTS)}. A new bare local "
        "`store` fixture must be deleted in favor of the shared "
        "`store`/`store_factory` fixtures in tests/conftest.py; a genuine "
        "new path-coupled variant must be added to _PATH_COUPLED_VARIANTS "
        "here with a one-line reason."
    )


def test_no_undocumented_bare_store_closes():
    """AC2: every `await store.close()` not inside a `finally:` block must be
    one of the 4 documented, manually-verified-safe sites."""
    bare = set(_bare_closes())
    assert bare == _KNOWN_SAFE_BARE_CLOSES, (
        "bare (non-`finally:`-guarded) `await store.close()` call sites "
        f"drifted: found={sorted(bare)} "
        f"expected={sorted(_KNOWN_SAFE_BARE_CLOSES)}. A new bare close is "
        "unsafe (skipped whenever the test body raises before reaching it, "
        "leaking the connection) — wrap it in try/finally, or let the "
        "shared `store`/`store_factory` fixture own the close instead."
    )
