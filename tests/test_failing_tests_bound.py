"""A red run's failing-test id list must be BOUNDED wherever it is persisted
or rendered — never in the attribution paths that decide whose fault it is.

Observed incident: a 96,465-test red run persisted a 966KB `attempts.
test_results` row (953KB of it the raw `failing_tests` list) and, because the
evidence ledger's `tests.md` renders that same column verbatim, a 1.36MB
ledger file. `_bounded_test_results`/`_bounded_failing_ids` (core/
orchestrator.py) cap every id list actually written to the column/event/
ledger at `_MAX_PERSISTED_FAILING_TESTS` (200), recording the true remainder
as `failing_tests_dropped` — while `_owned_failing_tests` and
`_newly_failing_vs_base`, the two checks that decide attribution, still see
every id: this file proves both halves of that split with a single real
`_run_attempt` drive, not a hand-built `test_results` dict.
"""

import contextlib
import json as _json
from unittest.mock import patch

from no_human.core import evidence_ledger
from no_human.core.orchestrator import Orchestrator
from no_human.core.pr_evidence import PrEvidence
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import runner
from no_human.vcs import GitRepo

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401

#: Comfortably over the 200-id bound, and matches the ticket's dropped-count
#: math (1000 - 200 = 800) so the assertions below read as the incident math,
#: not an arbitrary fixture size.
_TOTAL = 1000
_BOUND = 200
_DROPPED = _TOTAL - _BOUND


def _make_ids(n: int) -> list[str]:
    return [f"tests/test_gen.py::test_{i}" for i in range(n)]


def _persisted(attempt):
    tr = attempt["test_results"]
    return _json.loads(tr) if isinstance(tr, str) else (tr or {})


def _mutate(cwd):
    # A benign change that touches neither the fixture's real tests nor any
    # file whose name would match the fake `tests/test_gen.py::test_N` ids —
    # ownership must come back empty on its own, no stub required for AC1.
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


# Plain assertion-shaped output — no prerequisite signature (`runner.
# missing_prerequisite_reason`) and no invocation-error text, so the run
# lands on the plain-red path (`_run_attempt`'s `else` branch), the one that
# calls `_newly_failing_vs_base` rather than the invocation-error base check.
_ASSERTION_OUTPUT = (
    "not ok 3 - the button renders disabled\n"
    "  ---\n"
    "  AssertionError: expected 2 to equal 3\n"
    "  ---\n"
)


async def _run_attempt_with_many_failures(
    store, tmp_path, bare_repo, ids, *, newly_failing=None, owned=None, capture=None,
):
    """Drives the real `_run_attempt` (the established pattern — see
    `tests/test_missing_prereq_env_classification.py::
    _run_attempt_with_stubbed_test_output`) with a `TestRunResult` carrying
    `len(ids)` failing ids.

    `_owned_failing_tests` and `_newly_failing_vs_base` are stubbed so the
    test controls attribution deterministically instead of paying for a real
    bounded re-run of 1000 fake node ids; when *capture* is a dict, each
    stub records the exact list it was called with, which is how AC2 proves
    the FULL (unbounded) list still reaches attribution. `newly_failing=None`
    (the default) means "nothing is excused" — every id counts as newly
    failing, matching the plain-red path most other tests here exercise.
    """
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate), SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("bounded failing tests", repo_path=str(bare_repo))
    await store.create_task(task)
    # `_run_attempt` transitions the task to IMPLEMENTING itself — only legal
    # from PLANNING — so walk it there first, mirroring the established
    # harness in test_missing_prereq_env_classification.py.
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    tr = runner.TestRunResult(
        ran=True, ok=False, passed=0, failed=len(ids), errors=0,
        command="pytest -q", output=_ASSERTION_OUTPUT,
        failing_tests=list(ids),
        failure_blocks=runner._tap_failure_blocks(_ASSERTION_OUTPUT),
    )

    async def fake_run_tests_once(repo, cmd, cwd=None):
        return tr, False

    async def fake_owned_failing_tests(repo, base, failing, *, cwd=None):
        if capture is not None:
            capture["owned_arg"] = list(failing)
        return list(owned or [])

    async def fake_newly_failing_vs_base(
        repo, test_cmd, base, failing, *, cwd=None, env_dependent=False,
    ):
        if capture is not None:
            capture["newly_failing_arg"] = list(failing)
        return list(failing) if newly_failing is None else list(newly_failing)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(orch, "_run_tests_once", fake_run_tests_once))
        stack.enter_context(
            patch.object(orch, "_owned_failing_tests", fake_owned_failing_tests))
        stack.enter_context(
            patch.object(orch, "_newly_failing_vs_base", fake_newly_failing_vs_base))
        outcome = await orch._run_attempt(task, repo, 1, "main")

    attempts = await store.list_attempts(task.id)
    return outcome, attempts, events


async def test_a_red_run_with_more_than_the_bound_persists_exactly_n_ids_and_the_dropped_count(
    bare_repo, tmp_path, store,
):
    ids = _make_ids(_TOTAL)
    outcome, attempts, _events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids)

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert len(attempts) == 1
    persisted = _persisted(attempts[-1])
    assert persisted["failing_tests"] == ids[:_BOUND]
    assert persisted["failing_tests_dropped"] == _DROPPED
    # The ticket's byte claim: a 1000-id run must not come anywhere near the
    # 966KB the 96,465-id incident produced.
    assert len(_json.dumps(persisted)) < 100_000


async def test_the_tests_event_carries_the_same_bounded_list_and_count(
    bare_repo, tmp_path, store,
):
    ids = _make_ids(_TOTAL)
    _outcome, _attempts, events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids)

    tests_events = [e for e in events if e.get("kind") == "tests"]
    assert tests_events
    last = tests_events[-1]
    assert last["failing_tests"] == ids[:_BOUND]
    assert last["failing_tests_dropped"] == _DROPPED


async def test_the_ledgers_tests_md_is_bounded_by_the_same_column(
    bare_repo, tmp_path, store,
):
    ids = _make_ids(_TOTAL)
    _outcome, attempts, _events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids)
    persisted = _persisted(attempts[-1])

    evidence = PrEvidence(tests=persisted)
    files = evidence_ledger.render_files(
        evidence, task_id="deadbeef", head_sha="deadbeef",
        verification_md="", review_md="", assumptions_md="",
    )

    tests_md = files["tests.md"]
    assert ids[_BOUND - 1] in tests_md, "the last KEPT id must survive the render"
    assert ids[_BOUND] not in tests_md, "the first DROPPED id must not appear"
    assert f'"failing_tests_dropped": {_DROPPED}' in tests_md
    # vs the measured 1,355,369-byte ledger file from the incident.
    assert len(tests_md) < 100_000


async def test_the_pr_body_summary_header_shows_the_true_total_not_just_the_bound(
    bare_repo, tmp_path, store,
):
    # Regression: the `<details><summary>` header used to be built from
    # `len(failing)` — the PERSISTED (bounded) count — so a 1000-failure run
    # announced "200 failing tests" in the header while the expanded body's
    # "…and N more" line correctly named the true remainder. The header and
    # the detail must agree on the same true total.
    ids = _make_ids(_TOTAL)
    _outcome, attempts, _events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids)
    persisted = _persisted(attempts[-1])

    section = Orchestrator._test_evidence_section(persisted)
    assert f"<details><summary>{_TOTAL} failing tests</summary>" in section
    assert f"…and {_TOTAL - 10} more" in section
    assert f"<details><summary>{_BOUND} failing tests</summary>" not in section


async def test_the_base_tree_and_ownership_checks_still_receive_the_full_list(
    bare_repo, tmp_path, store,
):
    ids = _make_ids(_TOTAL)
    capture: dict = {}
    outcome, _attempts, _events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids, capture=capture)

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert capture["owned_arg"] == ids, "ownership must see every id, not just the kept 200"
    assert capture["newly_failing_arg"] == ids, (
        "the base-tree re-run must see every id, not just the kept 200")


async def test_the_pre_existing_excuse_path_is_bounded_too(bare_repo, tmp_path, store):
    # `newly_failing=[]` and `owned=[]` route through the pre-existing excuse
    # site, the one write that carries BOTH `failing_tests` and
    # `pre_existing_failures` in the same dict.
    ids = _make_ids(_TOTAL)
    _outcome, attempts, _events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids, newly_failing=[], owned=[])

    row = attempts[-1]
    assert row["status"] != "failed", "a pre-existing red run must not fail the attempt"
    persisted = _persisted(row)
    assert len(persisted["failing_tests"]) == _BOUND
    assert len(persisted["pre_existing_failures"]) == _BOUND
    assert persisted["failing_tests_dropped"] == _DROPPED
