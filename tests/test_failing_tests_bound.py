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
import shlex
from unittest.mock import patch

from no_human.core import evidence_ledger
from no_human.core.orchestrator import Orchestrator
from no_human.core.pr_evidence import PrEvidence
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import runner
from no_human.vcs import GitRepo

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401
from .test_missing_prereq_env_classification import _not_ok_cannot_find_module  # noqa: F401

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
    invocation_error=False, on_base=None, flaky=None, output=None,
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

    `invocation_error`/`on_base`/`flaky`/`output` extend the harness (round-5
    review MAJOR-1) so every DISTINCT single-run classification branch in
    `_run_attempt`/`_failed_tests_outcome` — not just the plain-red "else" —
    can be driven with the same shared driver: `on_base` stubs
    `_invocation_error_reproduces_on_base`'s return value (only consulted
    when `invocation_error=True` and `owned` is empty), `flaky` stubs
    `_flaky_on_rerun`'s return value (only consulted in the non-owned,
    non-pre-existing plain-red path), and `output` overrides the TAP text
    (and the `failure_blocks` derived from it) so the environment classifier
    (`runner.prerequisite_reason_for`) can be made to fire or not.
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

    out = _ASSERTION_OUTPUT if output is None else output
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=0, failed=len(ids), errors=0,
        command="pytest -q", output=out,
        failing_tests=list(ids),
        failure_blocks=runner._tap_failure_blocks(out),
        invocation_error=invocation_error,
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

    async def fake_on_base(repo, test_cmd, base, cwd=None, env_dependent=False):
        return on_base

    async def fake_flaky_on_rerun(repo, test_cmd, attributed, cwd=None):
        if capture is not None:
            capture["flaky_attributed_arg"] = list(attributed)
        return None if flaky is None else list(flaky)

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(orch, "_run_tests_once", fake_run_tests_once))
        stack.enter_context(
            patch.object(orch, "_owned_failing_tests", fake_owned_failing_tests))
        stack.enter_context(
            patch.object(orch, "_newly_failing_vs_base", fake_newly_failing_vs_base))
        stack.enter_context(
            patch.object(orch, "_invocation_error_reproduces_on_base", fake_on_base))
        stack.enter_context(
            patch.object(orch, "_flaky_on_rerun", fake_flaky_on_rerun))
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


# --------------------------------------------------------------------------- #
# Round-5 review MAJOR-1: one dedicated red test per DISTINCT code path       #
# class the classifier pipeline can take, each driving the REAL              #
# `_run_attempt` with >200 failing ids. Mutation matrix (site -> test):       #
#                                                                             #
#   layered aggregate/`_layered_tests_failed_outcome` join (:6733,:12576)     #
#     -> test_layered_red_run_is_bounded_end_to_end                          #
#   `_environment_test_failure`'s `_bounded_test_results` wrap (:12137)       #
#     -> test_environment_classifier_persists_bounded_ids                    #
#   owned-invocation-error branch + join (:6864-6882)                        #
#     -> test_owned_invocation_error_bills_bounded_ids                       #
#   invocation-error not-on-base branch (:6894-6914)                         #
#     -> test_invocation_error_not_on_base_persists_bounded_ids              #
#   invocation-error on-base (environmental) branch (:6915-6938)             #
#     -> test_invocation_error_on_base_persists_bounded_ids                  #
#   flaky-excuse branch + kwargs (:12666-12692)                              #
#     -> test_flaky_excuse_bounds_kwargs_and_column                          #
#   owned-billing branch + kwargs (:12699-12767)                             #
#     -> test_owned_billing_bounds_kwargs_and_column                         #
#   pre-existing excuse join (:6974-6991) -> already covered above by        #
#     test_the_pre_existing_excuse_path_is_bounded_too                       #
#   plain-red main join (:6800-6811) -> already covered above by             #
#     test_a_red_run_with_more_than_the_bound_persists_exactly_n_ids...       #
# --------------------------------------------------------------------------- #


async def test_layered_red_run_is_bounded_end_to_end(bare_repo, tmp_path, store):
    """The LAYERED call site (`_layered_tests_failed_outcome`, driven via
    `_resolve_test_plan` + `plan_runner.run_test_plan` — a wholly different
    code path from the single-run `_run_tests_once` driver every other test
    in this file uses) must bound `failing_tests` the same way: a >200-id
    layer result must persist exactly 200 ids + `failing_tests_dropped`, and
    the billing `detail` text (built via `_bounded_join_ids`) must stay
    bounded too."""
    from no_human.testing.plan_runner import LayerResult, PlanResult
    import no_human.testing.plan_runner as plan_runner_mod
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan

    ids = _make_ids(_TOTAL)
    plan = TestPlan(layers=[
        TestLayer(name="desktop", command="node --test", gating=Gating.BLOCKING),
    ])
    tr = runner.TestRunResult(
        ran=True, ok=False, passed=0, failed=len(ids),
        errors=0, command="node --test", output=_ASSERTION_OUTPUT,
        failing_tests=list(ids),
        failure_blocks=runner._tap_failure_blocks(_ASSERTION_OUTPUT),
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
    task = Task.new("layered bounded failing tests", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)

    with patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert ids[0] in outcome.detail
    assert ids[_BOUND] not in outcome.detail, "the detail text must be bounded too"
    assert "… and 800 more" in outcome.detail

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    persisted = _persisted(attempts[-1])
    assert persisted["failing_tests"] == ids[:_BOUND]
    assert persisted["failing_tests_dropped"] == _DROPPED

    tests_events = [e for e in events if e.get("kind") == "tests"]
    assert tests_events
    assert tests_events[-1]["failing_tests"] == ids[:_BOUND]
    assert tests_events[-1]["failing_tests_dropped"] == _DROPPED


async def test_environment_classifier_persists_bounded_ids(bare_repo, tmp_path, store):
    """A red run whose failing content matches a build-prerequisite
    signature (`runner.prerequisite_reason_for`) is escalated as an
    environment error — a wholly different verdict/return path from every
    other test here — but must still persist a BOUNDED `failing_tests` list
    on a >200-id run: `_environment_test_failure` re-spreads the caller's
    dict through `_bounded_test_results` (orchestrator.py:12137), and this
    is the one test that actually drives that wrap with more than the bound."""
    ids = _make_ids(_TOTAL)
    outcome, attempts, _events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids,
        output=_not_ok_cannot_find_module(1),
    )

    assert outcome.detail.startswith("tests could not run:"), outcome.detail
    assert outcome.off_ramp is True

    assert len(attempts) == 1
    row = attempts[0]
    assert row["status"] == "failed"
    assert row["infra_failure"] == 1
    persisted = _persisted(row)
    assert persisted["environment_error"] is True
    assert persisted["failing_tests"] == ids[:_BOUND]
    assert persisted["failing_tests_dropped"] == _DROPPED


async def test_owned_invocation_error_bills_bounded_ids(bare_repo, tmp_path, store):
    """An OWNED id that also matches an invocation-error pattern always
    bills the attempt directly (orchestrator.py:6863-6885) — the simplest
    branch to reach, no base-tree check consulted. With `owned` itself over
    the bound, both the persisted column AND the `_bounded_join_ids(owned)`
    text in `detail` must be bounded."""
    ids = _make_ids(_TOTAL)
    outcome, attempts, events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids, owned=list(ids), invocation_error=True,
    )

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert "… and 800 more" in outcome.detail
    assert ids[_BOUND] not in outcome.detail

    assert len(attempts) == 1
    persisted = _persisted(attempts[-1])
    assert persisted["failing_tests"] == ids[:_BOUND]
    assert persisted["failing_tests_dropped"] == _DROPPED
    assert persisted["invocation_error"] is True

    tests_events = [e for e in events if e.get("kind") == "tests"]
    assert tests_events
    assert tests_events[-1]["failing_tests"] == ids[:_BOUND]
    assert tests_events[-1]["failing_tests_dropped"] == _DROPPED


async def test_invocation_error_not_on_base_persists_bounded_ids(bare_repo, tmp_path, store):
    """No owned ids, and the SAME invocation error does not reproduce on the
    base tree — the change itself broke the test runner
    (orchestrator.py:6890-6914). A distinct return path from the owned
    branch above: it never consults ownership at all, only `on_base`."""
    ids = _make_ids(_TOTAL)
    outcome, attempts, _events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids, owned=[], invocation_error=True, on_base=False,
    )

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert len(attempts) == 1
    persisted = _persisted(attempts[-1])
    assert persisted["failing_tests"] == ids[:_BOUND]
    assert persisted["failing_tests_dropped"] == _DROPPED
    assert persisted["reproduces_on_base"] is False


async def test_invocation_error_on_base_persists_bounded_ids(bare_repo, tmp_path, store):
    """No owned ids, and the invocation error DOES reproduce on the base
    tree — "genuinely environmental", so this branch (orchestrator.py:
    6915-6938) does NOT return early; it persists and falls through to
    continue the attempt (same fall-through shape as the pre-existing-excuse
    path above). The persisted row from THIS write must still be bounded
    regardless of what the rest of the attempt goes on to do."""
    ids = _make_ids(_TOTAL)
    _outcome, attempts, events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids, owned=[], invocation_error=True, on_base=True,
    )

    assert attempts, "the environmental write must have happened"
    row = attempts[0]
    persisted = _persisted(row)
    assert persisted["failing_tests"] == ids[:_BOUND]
    assert persisted["failing_tests_dropped"] == _DROPPED
    assert persisted["reproduces_on_base"] is True
    assert row["status"] != "failed", (
        "genuinely-environmental invocation errors must not fail the attempt")

    tests_events = [e for e in events if e.get("kind") == "tests"]
    assert tests_events, "the environmental branch must still emit a tests event"


async def test_flaky_excuse_bounds_kwargs_and_column(bare_repo, tmp_path, store):
    """No owned ids; every attributed id goes green on an identical bounded
    re-run (`_flaky_on_rerun` returns the full `attributed` list) — the
    flaky-excuse branch of `_failed_tests_outcome` (orchestrator.py:
    12659-12692), which returns `None` and falls through, same shape as the
    pre-existing excuse. Both the `flaky_excused` event kwarg and the
    persisted `test_results["flaky_excused"]` must be bounded, each with its
    own `_dropped` count — distinct keys from `failing_tests_dropped`."""
    ids = _make_ids(_TOTAL)
    _outcome, attempts, events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids, owned=[], newly_failing=list(ids), flaky=list(ids),
    )

    assert attempts
    row = attempts[0]
    assert row["status"] != "failed", "a fully-flaky-excused run must not fail the attempt"
    persisted = _persisted(row)
    assert persisted["failing_tests"] == ids[:_BOUND]
    assert persisted["failing_tests_dropped"] == _DROPPED
    assert persisted["flaky_excused"] == ids[:_BOUND]
    assert persisted["flaky_excused_dropped"] == _DROPPED

    tests_events = [e for e in events if e.get("kind") == "tests"]
    assert tests_events
    last = tests_events[-1]
    assert last["flaky_excused"] == ids[:_BOUND]
    assert last["flaky_excused_dropped"] == _DROPPED
    assert last["failing_tests"] == ids[:_BOUND]
    assert last["failing_tests_dropped"] == _DROPPED


async def test_owned_billing_bounds_kwargs_and_column(bare_repo, tmp_path, store):
    """Owned ids that are ALSO newly-failing vs base bill the attempt via
    `_failed_tests_outcome`'s final `owned_attr` branch (orchestrator.py:
    12706-12767) — a distinct return path from every other test here (it is
    the one reached through `_attributed_ids`'s union-of-owned-and-newly-
    failing branch, not the invocation-error owned shortcut). Both the
    `owned_failures` event kwarg and the persisted column must be bounded
    with their own `_dropped` count."""
    ids = _make_ids(_TOTAL)
    outcome, attempts, events = await _run_attempt_with_many_failures(
        store, tmp_path, bare_repo, ids, owned=list(ids), newly_failing=list(ids),
    )

    assert outcome.status is TaskStatus.FAILED, outcome.detail
    assert "… and 800 more" in outcome.detail
    assert ids[_BOUND] not in outcome.detail

    assert len(attempts) == 1
    persisted = _persisted(attempts[-1])
    assert persisted["failing_tests"] == ids[:_BOUND]
    assert persisted["failing_tests_dropped"] == _DROPPED
    assert persisted["owned_failures"] == ids[:_BOUND]
    assert persisted["owned_failures_dropped"] == _DROPPED

    tests_events = [e for e in events if e.get("kind") == "tests"]
    assert tests_events
    last = tests_events[-1]
    assert last["owned_failures"] == ids[:_BOUND]
    assert last["owned_failures_dropped"] == _DROPPED
    assert last["failing_tests"] == ids[:_BOUND]
    assert last["failing_tests_dropped"] == _DROPPED


# --------------------------------------------------------------------------- #
# Round-5 review MAJOR-2: TEXT joins (event message / `failure_reason`)      #
# must stay byte-bounded on a PATHOLOGICAL run — not just the id LISTS.      #
# Drives the REAL runner (a shell `test_cmd` that `cat`s a synthetic TAP)    #
# through the REAL `_run_attempt`, on the plain owned-billing path, with     #
# far more failing ids than the incident's 96,465 would ever need to prove  #
# the point cheaply: 3,000 ids is already two orders of magnitude past the  #
# 200 bound.                                                                 #
# --------------------------------------------------------------------------- #


def _pathological_owned_tap(n: int) -> str:
    """`n` failing node tests, all named so ownership (file-level, node ids
    have no AST) attributes every one of them to a single file this
    attempt's own diff touches — `x.test.mjs`."""
    lines = []
    for i in range(1, n + 1):
        lines.append(
            f"not ok {i} - it fails variant {i}\n"
            "  ---\n"
            "  AssertionError: expected true to be false\n"
            "  location: 'x.test.mjs:3:1'\n"
            "  ---\n"
        )
    lines.append(f"1..{n}\n")
    lines.append(f"# tests {n}\n")
    lines.append("# pass 0\n")
    lines.append(f"# fail {n}\n")
    return "".join(lines)


async def test_pathological_scale_event_text_and_failure_reason_stay_under_16kb(
    bare_repo, tmp_path, store,
):
    n = 3000
    tap_path = tmp_path / "pathological.tap"
    tap_path.write_text(_pathological_owned_tap(n))
    node_err_cmd = f"cat {shlex.quote(str(tap_path))}; exit 1"

    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="node",
        test_cmd=node_err_cmd,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    def _mutate_node_test(cwd):
        (cwd / "x.test.mjs").write_text("// this attempt's own edit\n")

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate_node_test), SlackNotifier(None),
                        event_sink=events.append)
    task = Task.new("pathological-scale node test", repo_path=str(bare_repo))
    await store.create_task(task)

    outcome = await orch.run_task(task)

    # `max_attempts=1` means the single billed attempt above exhausts the
    # bounds and `run_task` escalates a blocker whose evidence embeds that
    # attempt's own (already-bounded) detail text — not a `FAILED` outcome
    # itself, but the byte-bound claim is exactly as load-bearing here: the
    # embedded "Last: attempt 1: ..." text must not blow past 16KB either.
    assert outcome.status is TaskStatus.ESCALATED, outcome.detail
    assert len(outcome.detail.encode("utf-8")) < 16_000, (
        f"failure detail is {len(outcome.detail.encode('utf-8'))} bytes")

    attempts = await store.list_attempts(task.id)
    assert attempts
    row = attempts[-1]
    assert row["status"] == "failed"
    reason = row["failure_reason"] or ""
    assert len(reason.encode("utf-8")) < 16_000, (
        f"persisted failure_reason is {len(reason.encode('utf-8'))} bytes")

    tests_events = [e for e in events if e.get("kind") == "tests"]
    assert tests_events
    for ev in tests_events:
        msg = ev.get("text") or ""
        assert len(msg.encode("utf-8")) < 16_000, (
            f"tests event text is {len(msg.encode('utf-8'))} bytes")
