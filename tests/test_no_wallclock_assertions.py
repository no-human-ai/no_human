"""Merge-gate guard: no test asserts a measured wall-clock duration against
an absolute literal.

Motivating incident: `tests/test_vcs_git_ls_remote_exact.py`'s
`elapsed_ms < 100` passed on an idle box and failed at 129.7ms on a
contended one (5f99b7af, load 67, isolated worktree) — the SAME code, a
SLOWER box. Every `nh approve` runs the full suite under `-n 4`, so any one
of these assertions can redden an unrelated land. This is a CLASS, not one
test: a widened sweep (see the four extra `git grep` patterns and the
per-file manual review recorded in the PR body) found ~21 such assertions
across 17 files, independent of the two the ticket's own two narrower
patterns already named.

Every genuine timing property those assertions were defending has a
load-insensitive replacement (a call count, a happens-before proof via
`tests/_timing.BlockingGate`, a `tests/_timing.calibrated_budget`
comparison, or a requested — not measured — bound like `timeout=`): see the
per-site table in the PR body, one row per (file, line, property,
replacement assertion, mutation that breaks it).

`tests/_clock_assertion_guard.py` holds the AST detector; this file wires it
up as a `repoguard` test (so it runs on every land regardless of which
directory changed) plus the detector's own unit tests over inline source
strings, so a change to the detector cannot silently stop catching what it
used to catch.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._clock_assertion_guard import find_wallclock_assertions

pytestmark = pytest.mark.repoguard

REPO = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO / "tests"

#: Owned by ticket d41812aa (in rework at the time of this change). Not one
#: byte of either file is touched here. Two different reasons land in the
#: same set:
#:
#: * `tests/test_guard.py` (:686, :1614, :1826) — direct
#:   `assert <clock-derived expr> < <literal>` sites the AST detector below
#:   flags today, same as anywhere else in `tests/`.
#: * `tests/test_wake_tick_does_not_stall_scheduler.py` (:113) —
#:   `assert elapsed <= outer_bound`, where `outer_bound` is a bare
#:   parameter at the assert site, not a literal; the literal is injected
#:   one call frame up (`outer_bound=22.0` / `2.0` / `1.0`, :131/:139/:150).
#:   The detector correctly does NOT flag this — it is a documented BLIND
#:   SPOT (interprocedural taint across a function boundary; see
#:   `tests/_clock_assertion_guard.py`'s docstring), not a detector miss to
#:   fix. It is named here purely for land coordination.
#:
#: Because those two reasons are different, "is this carve-out still
#: earning its keep" is proved two different ways below:
#: `test_the_allowlist_is_not_stale` for the detector-visible one, and
#: `test_the_wake_tick_blind_spot_is_not_stale` for the one the detector
#: cannot see by design.
_OWNED_BY_D41812AA = frozenset({
    "tests/test_guard.py",
    "tests/test_wake_tick_does_not_stall_scheduler.py",
})

#: Subset of `_OWNED_BY_D41812AA` where the detector finds the assertion
#: directly. Deliberately excludes
#: `tests/test_wake_tick_does_not_stall_scheduler.py` — see the comment
#: above.
_DETECTOR_VISIBLE_OWNED = frozenset({"tests/test_guard.py"})


def _scan_repo_tests() -> dict[str, list]:
    findings: dict[str, list] = {}
    for path in sorted(TESTS_DIR.glob("*.py")):
        rel = f"tests/{path.name}"
        source = path.read_text(encoding="utf-8")
        found = find_wallclock_assertions(source, rel)
        if found:
            findings[rel] = found
    return findings


def test_no_test_asserts_a_wallclock_duration_against_a_literal():
    findings = _scan_repo_tests()
    unowned = {
        rel: found for rel, found in findings.items()
        if rel not in _OWNED_BY_D41812AA
    }
    if unowned:
        rendered = "\n".join(str(f) for found in unowned.values() for f in found)
        pytest.fail(
            "found wall-clock-vs-literal assertion(s) outside the "
            "d41812aa carve-out:\n" + rendered
        )


def test_the_allowlist_is_not_stale():
    """If d41812aa lands and fixes `tests/test_guard.py`, this carve-out
    must start failing (proving it is still needed) rather than silently
    doing nothing forever. Covers only the detector-visible half of the
    carve-out — see `test_the_wake_tick_blind_spot_is_not_stale` for the
    other file, which the detector cannot see by design."""
    findings = _scan_repo_tests()
    for rel in _DETECTOR_VISIBLE_OWNED:
        assert rel in findings, (
            f"{rel} is allowlisted for ticket d41812aa but the guard found "
            "no wall-clock assertion in it anymore — remove it from "
            "_OWNED_BY_D41812AA in this file"
        )


def test_the_wake_tick_blind_spot_is_not_stale():
    """`tests/test_wake_tick_does_not_stall_scheduler.py` is carved out for
    a shape the AST detector cannot see directly: `outer_bound` is a bare
    parameter at the `assert elapsed <= outer_bound` site, and the literal
    is injected one call frame up. Since the detector will never flag this
    file, the "still needed" proof has to read the source directly instead
    — so this carve-out cannot silently outlive the shape it was named
    for."""
    source = (TESTS_DIR / "test_wake_tick_does_not_stall_scheduler.py").read_text(
        encoding="utf-8"
    )
    assert "assert elapsed <= outer_bound" in source, (
        "tests/test_wake_tick_does_not_stall_scheduler.py no longer has the "
        "parameter-injected-literal shape this carve-out exists for — "
        "remove it from _OWNED_BY_D41812AA in this file"
    )
    assert re.search(r"outer_bound=[\d.]", source), (
        "the outer_bound call sites in "
        "tests/test_wake_tick_does_not_stall_scheduler.py no longer pass a "
        "literal — remove it from _OWNED_BY_D41812AA in this file"
    )


def test_the_allowlist_never_grows():
    assert _OWNED_BY_D41812AA == {
        "tests/test_guard.py",
        "tests/test_wake_tick_does_not_stall_scheduler.py",
    }, (
        "the d41812aa carve-out changed shape — a third file must be fixed, "
        "not allowlisted"
    )


def test_the_guard_names_the_file_and_the_bound():
    """AC2's in-repo mutation proof: reintroducing a clock-vs-literal
    assertion must turn this guard red, and the failure must name the file
    and the offending bound (not just "something failed somewhere")."""
    source = (
        "import time\n\n"
        "def test_mutation_probe():\n"
        "    t0 = time.monotonic()\n"
        "    elapsed = time.monotonic() - t0\n"
        "    assert elapsed < 0.2\n"
    )
    findings = find_wallclock_assertions(source, "tests/test_mutation_probe.py")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.path == "tests/test_mutation_probe.py"
    assert finding.lineno == 6
    assert finding.bound == "0.2"
    assert "elapsed" in finding.expr_src


# ---------------------------------------------------------------------------
# Detector self-tests over inline source strings.
# ---------------------------------------------------------------------------

_POSITIVE_CASES = {
    "inline_var": (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    elapsed = time.monotonic() - t0\n"
        "    assert elapsed < 100\n"
    ),
    "aliased_import": (
        "import time as _time\n"
        "def test_x():\n"
        "    t0 = _time.monotonic()\n"
        "    elapsed = _time.monotonic() - t0\n"
        "    assert elapsed < 100\n"
    ),
    "aliased_bare_function": (
        "from time import monotonic as _m\n"
        "def test_x():\n"
        "    t0 = _m()\n"
        "    elapsed = _m() - t0\n"
        "    assert elapsed < 5\n"
    ),
    "module_constant_bound": (
        "import time\n"
        "_BOUND = 0.5\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    elapsed = time.monotonic() - t0\n"
        "    assert elapsed < _BOUND\n"
    ),
    "max_over_appended_list": (
        "import time\n"
        "def test_x():\n"
        "    gaps = []\n"
        "    last = time.perf_counter()\n"
        "    now = time.perf_counter()\n"
        "    gaps.append(now - last)\n"
        "    assert max(gaps) < 0.25\n"
    ),
    "scaled_milliseconds": (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    elapsed_ms = (time.monotonic() - t0) * 1000\n"
        "    assert elapsed_ms < 100\n"
    ),
    "reversed_direction_literal_first": (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    elapsed = time.monotonic() - t0\n"
        "    assert 100 > elapsed\n"
    ),
    "le_operator": (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    elapsed = time.monotonic() - t0\n"
        "    assert elapsed <= 2.0\n"
    ),
    "renamed_local": (
        "import time\n"
        "def test_x():\n"
        "    t0 = time.perf_counter()\n"
        "    wall = time.perf_counter() - t0\n"
        "    assert wall < 2.0\n"
    ),
}

_NEGATIVE_CASES = {
    "byte_ratio": (
        "def test_x():\n"
        "    a = b'x' * 10\n"
        "    b = b'y' * 100\n"
        "    ratio = len(a) / len(b)\n"
        "    assert ratio < 0.15\n"
    ),
    "token_ratio": (
        "def test_x():\n"
        "    measured = 12\n"
        "    baseline = 100\n"
        "    assert measured / baseline < 0.30\n"
    ),
    "static_ratio": (
        "def test_x():\n"
        "    words = 9\n"
        "    window = 4\n"
        "    assert words / window <= 3.0\n"
    ),
    "tainted_vs_tainted": (
        "import time\n"
        "def test_x():\n"
        "    windows = []\n"
        "    start = time.monotonic()\n"
        "    windows.append(time.monotonic() - start)\n"
        "    windows.append(time.monotonic() - start)\n"
        "    assert windows[1] < windows[0] * 0.75\n"
    ),
    "timeout_kwarg_is_not_a_compare": (
        "import asyncio\n"
        "async def test_x():\n"
        "    await asyncio.wait_for(asyncio.sleep(0), timeout=0.05)\n"
    ),
    "calibrated_budget_call": (
        "import time\n"
        "from tests._timing import calibrated_budget\n"
        "def test_x():\n"
        "    t0 = time.monotonic()\n"
        "    elapsed = time.monotonic() - t0\n"
        "    assert elapsed < calibrated_budget(reference=lambda: None, multiple=6)\n"
    ),
    "fixture_timestamp_not_an_assert": (
        "import time\n"
        "def test_x():\n"
        "    old = time.time() - 3 * 3600\n"
        "    assert old is not None\n"
    ),
}


@pytest.mark.parametrize("name", sorted(_POSITIVE_CASES))
def test_detector_flags_known_positive_shapes(name):
    source = _POSITIVE_CASES[name]
    findings = find_wallclock_assertions(source, "tests/test_fixture.py")
    assert findings, f"case {name!r} should have been flagged but was not"


@pytest.mark.parametrize("name", sorted(_NEGATIVE_CASES))
def test_detector_does_not_flag_known_negative_shapes(name):
    source = _NEGATIVE_CASES[name]
    findings = find_wallclock_assertions(source, "tests/test_fixture.py")
    assert not findings, (
        f"case {name!r} should NOT have been flagged, but got: "
        + ", ".join(str(f) for f in findings)
    )
