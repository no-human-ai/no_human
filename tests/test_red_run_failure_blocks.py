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

from no_human.core.orchestrator import Orchestrator
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
