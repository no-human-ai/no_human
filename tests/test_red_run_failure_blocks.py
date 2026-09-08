"""A red test run's `tests` event must carry its FAILING blocks, not the last
1200 bytes of the stream.

Incident (tasks ba602e95 and f8af7f46, 2026-09-08): both orchestrator runs
were failed by the same two node tests, but nothing on disk explained why —
the `tests` event only carried `fail_tail = (test_result.output or "")[-1200:]`,
which for a 435-test suite captures trailing `ok` blocks and the summary,
never the `not ok` failure blocks sitting thousands of bytes earlier. The
runner already parses failing blocks via `TestRunResult.failure_blocks`
(populated by `_tap_failure_blocks`), but the orchestrator never surfaced
them.

Fix: `runner.failure_report_blocks` / `runner.render_failure_blocks` (bounded
block rendering, node TAP + pytest FAILED/traceback) and
`Orchestrator._red_test_detail` (the one seam every red-run call site uses to
turn a run's results into event text + a full-output artifact pointer).

This file reuses the established harness pattern from
`tests/test_missing_prereq_env_classification.py:321-380`: build a
`runner.TestRunResult` by hand, stub `Orchestrator._run_tests_once`, drive
the real `orch._run_attempt(task, repo, 1, "main")`, and read back the
emitted `events` + `store.list_attempts`.
"""

import json as _json
import re
from pathlib import Path
from unittest.mock import patch

from no_human.core import evidence_ledger
from no_human.core.orchestrator import Orchestrator
from no_human.core.pr_evidence import PrEvidence
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import runner
from no_human.vcs import GitRepo

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401


def _persisted(attempt):
    tr = attempt["test_results"]
    return _json.loads(tr) if isinstance(tr, str) else (tr or {})


def _mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


async def _run_attempt_with_result(
    store, tmp_path, bare_repo, tr, *, mutate=_mutate, owned=None,
):
    """Drive the real `_run_attempt` with `_run_tests_once` stubbed to return
    *tr* — same pattern as `_run_attempt_with_stubbed_test_output` in
    `tests/test_missing_prereq_env_classification.py`, generalised to accept
    a caller-built `TestRunResult` directly (some cases here need fields
    `_tap_failure_blocks(output)` alone would not reproduce, e.g. a
    hand-supplied `traceback_excerpts` or an explicit `full_output`).

    `owned`, when given, stubs `_owned_failing_tests` (same idiom as
    `_run_attempt_with_stubbed_test_output`) so a red run can be routed
    through the owned-attribution + billing path (`_failed_tests_outcome`'s
    `owned_attr` branch) without needing a real git diff that names the
    failing test id.
    """
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(task)
    # `_run_attempt` transitions the task to IMPLEMENTING itself — only legal
    # from PLANNING — so walk it there first, like `_drive` would have.
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    async def fake_run_tests_once(repo, cmd, cwd=None):
        return tr, False

    async def fake_owned_failing_tests(repo, base, failing, *, cwd=None):
        return list(owned)

    with patch.object(orch, "_run_tests_once", fake_run_tests_once):
        if owned is not None:
            with patch.object(orch, "_owned_failing_tests", fake_owned_failing_tests):
                outcome = await orch._run_attempt(task, repo, 1, "main")
        else:
            outcome = await orch._run_attempt(task, repo, 1, "main")

    attempts = await store.list_attempts(task.id)
    return outcome, attempts, events, task


# --------------------------------------------------------------------------- #
# A faithful, full-size node TAP stream — two real assertion failures, not   #
# environment errors, buried far from the last 1200 (or even 8000) bytes.   #
# --------------------------------------------------------------------------- #

_PAD = (
    "keeps its own invariant green after every refactor lands and stays "
    "boringly fast on every worker, every single time it runs in CI"
)


def _ok_block(n: int) -> str:
    return f"ok {n} - subtest {n} {_PAD}\n"


def _not_ok_save_failure(n: int) -> str:
    return (
        f"not ok {n} - mainSaveFailure persists the failing write to disk "
        "before surfacing the error to the renderer\n"
        "  ---\n"
        "  AssertionError: expected save() to reject with ENOSPC but it resolved\n"
        "  at desktop/mainSaveFailure.test.mjs:42:9\n"
        "  ---\n"
    )


def _not_ok_startup_failure(n: int) -> str:
    return (
        f"not ok {n} - mainStartupFailure does not leak an unhandled "
        "rejection when config parsing throws before the window is created\n"
        "  ---\n"
        "  AssertionError: expected 0 unhandled rejections but got 1\n"
        "  at desktop/mainStartupFailure.test.mjs:17:3\n"
        "  ---\n"
    )


def _big_tap(total: int = 435, first: int = 20, second: int = 40) -> str:
    lines = []
    for n in range(1, total + 1):
        if n == first:
            lines.append(_not_ok_save_failure(n))
        elif n == second:
            lines.append(_not_ok_startup_failure(n))
        else:
            lines.append(_ok_block(n))
    lines.append(f"1..{total}\n")
    lines.append(f"# tests {total}\n")
    lines.append(f"# pass {total - 2}\n")
    lines.append("# fail 2\n")
    return "".join(lines)


async def test_node_tap_failing_blocks_survive_a_ten_kilobyte_tail(bare_repo, tmp_path, store):
    tap = _big_tap()
    encoded = tap.encode("utf-8")
    for needle in (b"mainSaveFailure persists", b"mainStartupFailure does not leak"):
        idx = encoded.index(needle)
        distance_from_end = len(encoded) - idx
        assert distance_from_end > 10_000, (needle, distance_from_end)

    tr = runner.TestRunResult(
        ran=True, ok=False, passed=433, failed=2, errors=0,
        command="node --test",
        output=tap[-8000:],
        full_output=tap,
        failure_blocks=runner._tap_failure_blocks(tap),
    )
    # The OLD behaviour's tail must not carry either signature — pins the
    # exact truncation the incident hit, same idiom as
    # test_missing_prereq_env_classification.py's kilobyte-tail test.
    old_tail = (tr.output or "")[-1200:]
    assert "mainSaveFailure persists" not in old_tail
    assert "mainStartupFailure does not leak" not in old_tail

    outcome, attempts, events, task = await _run_attempt_with_result(
        store, tmp_path, bare_repo, tr)

    test_events = [e for e in events if e["kind"] == "tests"]
    assert test_events, events
    text = test_events[0]["text"]
    assert "mainSaveFailure persists" in text
    assert "mainStartupFailure does not leak" in text

    persisted = _persisted(attempts[-1])
    blocks = persisted["failure_blocks"]
    assert any("mainSaveFailure persists" in b for b in blocks), blocks
    assert any("mainStartupFailure does not leak" in b for b in blocks), blocks


async def test_an_owned_attributed_failure_keeps_its_blocks_through_billing(
    bare_repo, tmp_path, store,
):
    """A red run whose failing id is OWNED by this attempt's own diff is
    routed to `_failed_tests_outcome`'s `owned_attr` billing branch
    (orchestrator.py ~12336-12359), not the plain branch's write (~6557-
    6566). That branch calls `store.update_attempt(..., test_results=...)`
    a SECOND time for the same attempt row — and `update_attempt` REPLACES
    the whole `test_results` column rather than merging it (see the comment
    on the pre-existing-excuse write above) — so unless this second write
    also carries `failure_blocks`, it silently drops the blocks the first
    write (plain branch) had just persisted. Same TAP fixture as the
    kilobyte-tail test above, but with `failing_tests`/`_owned_failing_tests`
    wired so `owned_attr` is non-empty and billing actually happens here.
    """
    tap = _big_tap()
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=433, failed=2, errors=0,
        command="node --test",
        output=tap[-8000:],
        full_output=tap,
        failure_blocks=runner._tap_failure_blocks(tap),
        failing_tests=["desktop/mainSaveFailure.test.mjs"],
    )

    outcome, attempts, events, task = await _run_attempt_with_result(
        store, tmp_path, bare_repo, tr,
        owned=["desktop/mainSaveFailure.test.mjs"],
    )

    # Confirms the run was actually billed through the owned branch, not
    # excused some other way — otherwise this test would not be exercising
    # `_failed_tests_outcome`'s owned_attr write at all.
    assert outcome.status is TaskStatus.FAILED, outcome.detail
    failed_rows = [a for a in attempts if a.get("status") == "failed"]
    assert failed_rows, attempts
    row = failed_rows[-1]
    assert _persisted(row).get("owned_failures") == ["desktop/mainSaveFailure.test.mjs"], row

    # Two "tests" events fire in this flow: the plain branch's first pass
    # (carries the blocks, already covered by the kilobyte-tail test above)
    # and `_failed_tests_outcome`'s owned-attribution note. Only the record
    # PERSISTED to `attempts.test_results` (asserted below) is this test's
    # subject — assert here only that a "tests" event exists and names the
    # owned id, confirming the owned branch actually fired.
    test_events = [e for e in events if e["kind"] == "tests"]
    assert test_events, events
    owned_events = [e for e in test_events if e.get("owned_failures")]
    assert owned_events, events
    assert any("mainSaveFailure persists" in e["text"] for e in test_events), test_events

    persisted = _persisted(row)
    blocks = persisted["failure_blocks"]
    assert any("mainSaveFailure persists" in b for b in blocks), persisted


# --------------------------------------------------------------------------- #
# Pytest-shaped FAILED summary + traceback excerpts                          #
# --------------------------------------------------------------------------- #


async def test_pytest_failed_sections_are_surfaced(bare_repo, tmp_path, store):
    full_output = (
        "============================= test session starts ==============================\n"
        "collected 10 items\n"
        "\n"
        "=================================== FAILURES ====================================\n"
        "_________________________________ test_foo _________________________________\n"
        "\n"
        "    def test_foo():\n"
        ">       assert 1 == 2\n"
        "E       AssertionError: boom\n"
        "\n"
        "tests/test_x.py:5: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_x.py::test_foo - AssertionError: boom\n"
        "======================= 1 failed, 9 passed in 0.12s =========================\n"
    )
    # `failing_tests=[]` deliberately: this test proves the event text is
    # built from `full_output` + `traceback_excerpts` alone (the pytest
    # branch of `runner.failure_report_blocks`), not from `failing_tests` —
    # and keeps `_owned_failing_tests`/`_newly_failing_vs_base` (which both
    # no-op on an empty `failing_tests` list before touching the base tree)
    # cheap and hermetic even though the command names "pytest".
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=9, failed=1, errors=0,
        command="pytest -q",
        output=full_output[-8000:],
        full_output=full_output,
        failing_tests=[],
        traceback_excerpts={
            "tests/test_x.py::test_foo": "assert 1 == 2\nAssertionError: boom",
        },
    )

    outcome, attempts, events, task = await _run_attempt_with_result(
        store, tmp_path, bare_repo, tr)

    test_events = [e for e in events if e["kind"] == "tests"]
    assert test_events, events
    text = test_events[0]["text"]
    assert "FAILED tests/test_x.py::test_foo" in text
    assert "AssertionError: boom" in text


# --------------------------------------------------------------------------- #
# Round-2 review BLOCKER: `_tap_failure_blocks` caps its return to           #
# `_EXCERPT_MAX_TESTS` (3) for its own consumer (`prerequisite_reason_for`)  #
# — but `failure_report_blocks` used to reuse that already-capped list       #
# directly as the report's source, so a suite with MORE than 3 failing      #
# tests silently dropped the 4th+ failure from both the `tests` event text  #
# and the persisted `test_results.failure_blocks`, and the overflow line     #
# could never fire because only <=3 (well under the 12000-char bound) ever  #
# reached `render_failure_blocks`. Fixed by re-parsing every `not ok` block  #
# off `full_output` (`_tap_blocks_uncapped`) for the report, while           #
# `_tap_failure_blocks`'s 3-block cap is left unchanged for its existing     #
# consumer.                                                                  #
# --------------------------------------------------------------------------- #


def _not_ok_numbered(n: int, i: int) -> str:
    return (
        f"not ok {n} - uniqueFailureSignature{i} asserts its own invariant "
        "and fails deterministically every time\n"
        "  ---\n"
        f"  AssertionError: expected uniqueFailureSignature{i} to pass but "
        "it did not\n"
        f"  at desktop/failure{i}.test.mjs:{i}:1\n"
        "  ---\n"
    )


def _five_failing_tap(total: int = 60) -> str:
    fail_positions = {5: 1, 15: 2, 25: 3, 35: 4, 45: 5}
    lines = []
    for n in range(1, total + 1):
        if n in fail_positions:
            lines.append(_not_ok_numbered(n, fail_positions[n]))
        else:
            lines.append(_ok_block(n))
    lines.append(f"1..{total}\n")
    lines.append(f"# tests {total}\n")
    lines.append(f"# pass {total - 5}\n")
    lines.append("# fail 5\n")
    return "".join(lines)


async def test_all_five_failing_blocks_survive_not_just_the_first_three(
    bare_repo, tmp_path, store,
):
    """Measured proof of the round-2 BLOCKER: on the pre-fix code, a TAP
    stream with 5 `not ok` blocks left `_tap_failure_blocks` (and therefore
    `failure_report_blocks`, which reused it) holding only 3 — the 4th and
    5th failures vanished from both the `tests` event text and the persisted
    `test_results.failure_blocks`, with no overflow line to even hint at the
    loss (3 blocks is well under the 12000-char report budget). Fixed by
    re-parsing every block off `full_output` for the report while
    `TestRunResult.failure_blocks` (fed by the UNCHANGED `_tap_failure_blocks`)
    keeps its own 3-block cap for `prerequisite_reason_for`.
    """
    tap = _five_failing_tap()
    # `TestRunResult.failure_blocks` keeps the OLD 3-block cap — this is
    # exactly what the unchanged `_tap_failure_blocks` still produces, and
    # what `prerequisite_reason_for` still consumes. The report must still
    # recover all 5 despite this field holding only 3.
    capped = runner._tap_failure_blocks(tap)
    assert len(capped) == 3, capped

    tr = runner.TestRunResult(
        ran=True, ok=False, passed=55, failed=5, errors=0,
        command="node --test",
        output=tap[-8000:],
        full_output=tap,
        failure_blocks=capped,
    )

    outcome, attempts, events, task = await _run_attempt_with_result(
        store, tmp_path, bare_repo, tr)

    test_events = [e for e in events if e["kind"] == "tests"]
    assert test_events, events
    text = test_events[0]["text"]
    for i in range(1, 6):
        assert f"uniqueFailureSignature{i}" in text, (i, text)

    persisted = _persisted(attempts[-1])
    blocks = persisted["failure_blocks"]
    assert len(blocks) == 5, blocks
    for i in range(1, 6):
        assert any(f"uniqueFailureSignature{i}" in b for b in blocks), (i, blocks)


# --------------------------------------------------------------------------- #
# Round-2 review MAJOR: the class of bug behind the original incident exists #
# one site over — `_environment_test_failure` and three per-site            #
# invocation-error writes in the plain branch of `_run_attempt` each fire a  #
# SECOND (or third) `update_attempt(..., test_results=...)` for the SAME     #
# attempt row. `update_attempt` REPLACES the whole column rather than        #
# merging it, so any of these writes that built its own dict from scratch    #
# instead of the shared `base_test_results` silently dropped                 #
# `failure_blocks` from the persisted record. Fixed by building the red      #
# run's `test_results` dict ONCE (`base_test_results`, with                  #
# `failure_blocks`) and reusing it (spread + override) at every later write. #
# --------------------------------------------------------------------------- #


def _not_ok_missing_package(n: int) -> str:
    return (
        f"not ok {n} - the plugin loads its runtime dependency before use\n"
        "  ---\n"
        "  error: Cannot find module 'left-pad'\n"
        "  Require stack:\n"
        "  - /repo/desktop/plugin.cjs\n"
        "  ---\n"
    )


def _environment_tap(total: int = 30, at: int = 12) -> str:
    lines = []
    for n in range(1, total + 1):
        if n == at:
            lines.append(_not_ok_missing_package(n))
        else:
            lines.append(_ok_block(n))
    lines.append(f"1..{total}\n")
    return "".join(lines)


async def test_the_environment_classifier_exit_keeps_its_failure_blocks(
    bare_repo, tmp_path, store,
):
    """Measured proof of the round-2 MAJOR: on the pre-fix code, an
    environment-routed red run persisted `['environment_error', 'errors',
    'failed', 'failing_tests', 'ok', 'passed', 'ran', 'tamper_flag']` with NO
    `failure_blocks` key at all — `_environment_test_failure`'s own
    `update_attempt` write REPLACED the column the plain branch's first
    write had just populated. Fixed by passing the same `base_test_results`
    dict (which already carries `failure_blocks`) into
    `_environment_test_failure` as its `test_results=` argument.
    """
    tap = _environment_tap()
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=29, failed=1, errors=0,
        command="node --test",
        output=tap[-8000:],
        full_output=tap,
        failure_blocks=runner._tap_failure_blocks(tap),
    )

    outcome, attempts, events, task = await _run_attempt_with_result(
        store, tmp_path, bare_repo, tr)

    failed_rows = [a for a in attempts if a.get("status") == "failed"]
    assert failed_rows, attempts
    row = failed_rows[-1]
    persisted = _persisted(row)
    assert persisted.get("environment_error") is True, persisted
    blocks = persisted.get("failure_blocks")
    assert blocks, persisted
    assert any("Cannot find module 'left-pad'" in b for b in blocks), blocks


def _not_ok_module_not_found(n: int) -> str:
    return (
        f"not ok {n} - the added test cannot resolve its own import\n"
        "  ---\n"
        "  Error [ERR_MODULE_NOT_FOUND]: Cannot find module './widget.mjs'\n"
        "  ---\n"
    )


def _invocation_error_tap(total: int = 10, at: int = 4) -> str:
    lines = []
    for n in range(1, total + 1):
        if n == at:
            lines.append(_not_ok_module_not_found(n))
        else:
            lines.append(_ok_block(n))
    lines.append(f"1..{total}\n")
    return "".join(lines)


async def test_an_owned_invocation_error_exit_keeps_its_failure_blocks(
    bare_repo, tmp_path, store,
):
    """Same class, one site over: an OWNED failing id that also matches
    `_INVOCATION_ERROR_PATTERNS` bills the attempt directly from the plain
    branch's own inline check (never consulting the base tree) — a further
    `update_attempt(..., test_results=...)` write for the same attempt that,
    pre-fix, built its dict from scratch and dropped `failure_blocks` the
    same way the environment-classifier exit did above.
    """
    tap = _invocation_error_tap()
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=9, failed=1, errors=0,
        command="node --test",
        output=tap[-8000:],
        full_output=tap,
        failure_blocks=runner._tap_failure_blocks(tap),
        failing_tests=["desktop/widget.test.mjs"],
        invocation_error=True,
    )

    outcome, attempts, events, task = await _run_attempt_with_result(
        store, tmp_path, bare_repo, tr,
        owned=["desktop/widget.test.mjs"],
    )

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    failed_rows = [a for a in attempts if a.get("status") == "failed"]
    assert failed_rows, attempts
    row = failed_rows[-1]
    persisted = _persisted(row)
    assert persisted.get("invocation_error") is True, persisted
    blocks = persisted.get("failure_blocks")
    assert blocks, persisted
    assert any("ERR_MODULE_NOT_FOUND" in b for b in blocks), blocks


# --------------------------------------------------------------------------- #
# Bounding: each block capped, whole addition capped with an overflow line   #
# --------------------------------------------------------------------------- #


def test_the_block_addition_is_bounded_with_an_overflow_line():
    raw_blocks = [f"not ok {i} - failure block {i}\n" + ("x" * 4000) for i in range(20)]
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=0, failed=20, errors=0,
        command="node --test", output="irrelevant",
        failure_blocks=raw_blocks,
    )

    blocks = runner.failure_report_blocks(tr)
    assert len(blocks) == len(raw_blocks)
    marker_slack = len("\n… [truncated]") + 4
    for b in blocks:
        assert len(b) <= runner.FAILURE_BLOCK_MAX_CHARS + marker_slack, len(b)

    text = runner.render_failure_blocks(blocks)
    assert len(text) <= runner.FAILURE_REPORT_MAX_CHARS + 200, len(text)
    m = re.search(r"\.\.\. (\d+) more failing blocks$", text)
    assert m, text
    assert int(m.group(1)) > 0

    # Zero parsed blocks (no `failure_blocks`, no pytest summary section) ->
    # `failure_report_blocks` yields nothing, and the ORCHESTRATOR's fallback
    # (mirrored here at the unit level) is the plain 1200-byte tail, used
    # verbatim — the pre-fix, no-blocks-parsed contract must survive.
    long_output = ("y" * 5000) + "MARKER_TAIL_END"
    tr2 = runner.TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command="node --test", output=long_output,
    )
    assert runner.failure_report_blocks(tr2) == []
    tail = (tr2.output or "")[-1200:]
    assert "MARKER_TAIL_END" in tail


# --------------------------------------------------------------------------- #
# Full output written to an attempt-scoped artifact, path named in the event #
# --------------------------------------------------------------------------- #


async def test_the_full_output_is_written_to_the_attempt_log(bare_repo, tmp_path, store):
    marker = "ARTIFACT_MARKER_XYZ"
    full_output = marker + "\n" + ("z" * 15000)
    assert len(full_output) - full_output.index(marker) > 10_000

    tr = runner.TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command="node --test",
        output=full_output[-8000:],
        full_output=full_output,
        failure_blocks=["not ok 1 - boom\n  ---\n  AssertionError: boom\n  ---\n"],
    )

    outcome, attempts, events, task = await _run_attempt_with_result(
        store, tmp_path, bare_repo, tr)

    log_path = Path.home() / ".no_human" / "artifacts" / task.id / "tests-attempt-1.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert marker in content

    test_events = [e for e in events if e["kind"] == "tests"]
    assert test_events, events
    text = test_events[0]["text"]
    assert f"~/.no_human/artifacts/{task.id}/tests-attempt-1.log" in text


# --------------------------------------------------------------------------- #
# A green run: no blocks, no artifact file, `failure_blocks` is `[]`         #
# --------------------------------------------------------------------------- #


async def test_a_green_run_emits_no_blocks_and_writes_no_file(bare_repo, tmp_path, store):
    tr = runner.TestRunResult(
        ran=True, ok=True, passed=10, failed=0, errors=0,
        command="node --test", output="all good",
    )

    outcome, attempts, events, task = await _run_attempt_with_result(
        store, tmp_path, bare_repo, tr)

    test_events = [e for e in events if e["kind"] == "tests"]
    assert test_events, events
    text = test_events[0]["text"]
    assert "not ok" not in text
    assert "full test output:" not in text

    persisted = _persisted(attempts[-1])
    assert persisted["failure_blocks"] == []

    log_path = Path.home() / ".no_human" / "artifacts" / task.id / "tests-attempt-1.log"
    assert not log_path.exists()


# --------------------------------------------------------------------------- #
# Round-3 review MAJOR: the PERSISTED `failure_blocks` list must be bounded   #
# by the SAME walk as the rendered event text — not the full, unbounded      #
# concatenation `_red_test_detail` used to return. A 435-failure TAP stream  #
# persisted 435 blocks / 672KB into one `attempts.test_results` column (and, #
# verbatim, into the evidence ledger's `tests.md`) while the event said      #
# "... 428 more failing blocks". Fixed via `runner.bound_failure_blocks`,    #
# the one walk both `render_failure_blocks` (text) and `_red_test_detail`    #
# (persisted list) now share.                                               #
# --------------------------------------------------------------------------- #


def _not_ok_long(n: int, i: int) -> str:
    diag_lines = "\n".join(
        f"  diagnostic context line {i}-{j:03d} padded so this block "
        "comfortably exceeds the per-block character cap on its own"
        for j in range(45)
    )
    return (
        f"not ok {n} - uniqueLongFailure{i} certain to exceed the per-block "
        "cap after this many diagnostic lines\n"
        "  ---\n"
        f"  AssertionError: expected uniqueLongFailure{i} to pass but it did "
        "not\n"
        f"{diag_lines}\n"
        "  ---\n"
    )


def _many_long_failing_tap(num_failures: int = 20, total: int = 400) -> str:
    step = max(1, total // (num_failures + 1))
    fail_positions = {(i * step): i for i in range(1, num_failures + 1)}
    lines = []
    for n in range(1, total + 1):
        if n in fail_positions:
            lines.append(_not_ok_long(n, fail_positions[n]))
        else:
            lines.append(_ok_block(n))
    lines.append(f"1..{total}\n")
    lines.append(f"# tests {total}\n")
    lines.append(f"# pass {total - num_failures}\n")
    lines.append(f"# fail {num_failures}\n")
    return "".join(lines)


async def test_persisted_failure_blocks_are_bounded_not_the_full_unbounded_list(
    bare_repo, tmp_path, store,
):
    """Round-3 review MAJOR (the main fix). Uses `full_output` re-parsing —
    not a hand-supplied `failure_blocks` — with 20 long `not ok` blocks
    (comfortably more than the 9 the send-back asked for), each padded past
    `FAILURE_BLOCK_MAX_CHARS` so only some of them fit under the whole-report
    bound and a real drop happens: this exercises the exact re-parse path
    (`runner.failure_report_blocks` off `full_output`) the incident hit, not
    a synthetic `failure_blocks` list.
    """
    tap = _many_long_failing_tap(num_failures=20)
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=380, failed=20, errors=0,
        command="node --test",
        output=tap[-8000:],
        full_output=tap,
        failure_blocks=runner._tap_failure_blocks(tap),
    )

    # Ground truth: the SAME bounded walk the fix ties text and persistence
    # to, computed independently here so the assertions below are pinned to
    # a real, measured drop rather than an assumption about the fixture.
    all_blocks = runner.failure_report_blocks(tr)
    assert len(all_blocks) >= 9, len(all_blocks)
    expected_kept, expected_dropped = runner.bound_failure_blocks(all_blocks)
    assert expected_dropped > 0, "fixture must actually overflow the bound"
    assert len(expected_kept) < len(all_blocks)

    outcome, attempts, events, task = await _run_attempt_with_result(
        store, tmp_path, bare_repo, tr)

    test_events = [e for e in events if e["kind"] == "tests"]
    assert test_events, events
    text = test_events[0]["text"]
    assert f"... {expected_dropped} more failing blocks" in text, text

    persisted = _persisted(attempts[-1])
    assert persisted["failure_blocks"] == expected_kept, (
        len(persisted["failure_blocks"]), len(expected_kept))
    assert persisted["failure_blocks_dropped"] == expected_dropped, persisted

    # The evidence ledger's tests.md is a raw JSON dump of this SAME
    # `test_results` dict (`evidence_ledger.render_files`, whose `evidence.
    # tests` is exactly `PrEvidence.tests`) — it must inherit the bound, not
    # the full unbounded list this test proves was fixed above.
    evidence = PrEvidence(tests=persisted)
    files = evidence_ledger.render_files(
        evidence, task_id=task.id, head_sha="deadbeef",
        verification_md="", review_md="", assumptions_md="",
    )
    tests_md = files["tests.md"]
    # Generous slack over the raw bound for JSON escaping/indentation and
    # the other (small) fields in the dict — nowhere near the ~30KB+ the
    # unbounded 20-block list would have produced.
    assert len(tests_md) < runner.FAILURE_REPORT_MAX_CHARS + 4000, len(tests_md)


# --------------------------------------------------------------------------- #
# Round-3 send-back "also": the LAYERED branch's environment-classifier exit  #
# (`_layered_tests_failed_outcome`'s `env_outcome` call) used to build its    #
# OWN `test_results` dict instead of reusing the caller's already-built and   #
# already-persisted one (`_run_attempt`'s `layer_test_results`, ~6501) — the  #
# layered counterpart of `test_the_environment_classifier_exit_keeps_its_     #
# failure_blocks` above, which proved the same class of bug in the PLAIN     #
# branch. Drives the layered path the same way `tests/test_missing_prereq_   #
# env_classification.py::test_layered_missing_prerequisite_never_retries`    #
# does: stub `_resolve_test_plan` + `plan_runner.run_test_plan`.             #
# --------------------------------------------------------------------------- #


async def test_the_layered_environment_classifier_exit_keeps_its_failure_blocks(
    bare_repo, tmp_path, store,
):
    from no_human.testing.plan_runner import LayerResult, PlanResult
    import no_human.testing.plan_runner as plan_runner_mod
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan

    tap = _environment_tap()
    plan = TestPlan(layers=[
        TestLayer(name="desktop", command="node --test", gating=Gating.BLOCKING),
    ])
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=29, failed=1, errors=0,
        command="node --test", output=tap[-8000:],
        full_output=tap,
        failure_blocks=runner._tap_failure_blocks(tap),
    )
    lr = LayerResult(layer_name="desktop", gating=Gating.BLOCKING, result=tr)

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        return PlanResult(layer_results=[lr])

    async def fake_resolve_test_plan(task):
        return plan

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate), SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("desktop npm test", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    with patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.detail.startswith("tests could not run:"), outcome.detail
    attempts = await store.list_attempts(task.id)
    assert attempts, "no attempt row persisted"
    row = attempts[-1]
    persisted = _persisted(row)
    assert persisted.get("environment_error") is True, persisted
    blocks = persisted.get("failure_blocks")
    assert blocks, persisted
    assert any("Cannot find module 'left-pad'" in b for b in blocks), blocks
    # The layered aggregate write (`_run_attempt` ~6501) computed no drop for
    # this small fixture — carried through unchanged, not dropped, by the
    # pass-through refactor.
    assert persisted.get("failure_blocks_dropped") == 0, persisted
