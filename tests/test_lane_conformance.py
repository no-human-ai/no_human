"""Board lane routing: Python is the source of truth, and it must agree with the JS.

The lane decision used to exist only in ``web/src/boardLanes.js``. Anything else
that wanted a lane (the API, a CLI) had to reimplement it, and this repo already
shipped that failure once - PR-007, where the counts were right and the lane
LABEL lied.

Every case here is loaded from ``testdata/lane_conformance.json``, the same file
``web/src/laneConformance.test.mjs`` reads.

ONE fixture file is not enough on its own. This repo's ``.no_human.yml`` routes
``web/**`` to the node runner and has no rule for ``src/**`` or ``testdata/**``,
and ``_resolve_test_target`` fires a rule only when EVERY edited file matches it
- so a change confined to ``core/lanes.py`` + the fixture never invokes node at
all, and the shared fixture gets read by one runner. That made the anti-drift
guarantee one-sided: JS-side drift was caught, Python-side drift was not.

``test_the_js_implementation_agrees_on_every_shared_case`` closes it: THIS suite
shells out to ``node --test`` on the conformance file, so the Python suite alone
cannot go green while the two implementations disagree. One runner drives both.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from no_human.core.lanes import LANE_KEYS, LANE_STATUSES, is_waiting, lane_for
from no_human.core.task import Task, TaskStatus
from no_human.api.app import app
from no_human.api.models import TaskSummaryOut


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "testdata" / "lane_conformance.json"
WEB_DIR = REPO_ROOT / "web"
NODE_TEST_PATH = WEB_DIR / "src" / "laneConformance.test.mjs"

_FIXTURES = json.loads(FIXTURE_PATH.read_text())["cases"]

# A floor, not an equality: adding cases is fine, quietly deleting them is not.
# The reviewer removed 7 pure edge cases - including all three PRESERVED DEFECT
# pins - and both suites stayed green under the old `>= 20` node-side floor and
# no Python floor at all. Raise this deliberately when you add cases.
FIXTURE_FLOOR = 28

# The known defect this branch preserves rather than fixes (see the module
# docstring of core/lanes.py and the commit that introduced the fixture). Three
# pins, one per status that can reach it.
PRESERVED_DEFECT_PINS = 3

# ``node --test`` exits 0 on a file that declares no tests, on a file where every
# test is ``test.skip``, and on any file that reaches ``process.exit(0)`` - so
# the exit code alone cannot tell "the JS agrees" apart from "the JS never ran".
# Demonstrated: dropping ``escalated`` from boardLanes.js's answer lane makes the
# node file fail 7 subtests, and ONE inserted ``process.exit(0)`` line then puts
# the Python suite back to green with the drift still in place.
#
# The TAP epilogue is the signal that does tell them apart: all three vacuous
# exits report ``# pass 0`` or ``# pass 1``, against 92 on a real run. Requiring
# ``# fail 0`` AND a pass floor closes it.
#
# The floor is derived from the shared fixture rather than hardcoded, so it
# cannot rot as cases are added. It is deliberately ONE subtest per shared case,
# not the three the node file runs today (computeLane, isWaiting, routeTask -
# 3x28 plus 8 standalone tests = 92): the margin means restructuring the node
# file cannot turn this into a false failure, while a floor of 28 still leaves
# every vacuous exit (pass=0 or pass=1) far below it.
NODE_PASS_FLOOR = len(_FIXTURES)


def _tap_counter(stdout: str, name: str) -> int | None:
    """One counter from ``node --test``'s TAP epilogue, e.g. ``# pass 92``.

    ``None`` when the line is absent or unparseable, which the caller treats as
    a FAILURE: a reporter that stopped emitting the counters would otherwise
    silently reopen the hole this parsing exists to close.
    """
    for line in stdout.splitlines():
        if line.startswith(f"# {name} "):
            try:
                return int(line.split()[-1])
            except ValueError:
                return None
    return None


def _ids() -> list[str]:
    return [c["name"] for c in _FIXTURES]


# --------------------------------------------------------------------------- #
# The shared fixtures                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("case", _FIXTURES, ids=_ids())
def test_lane_matches_the_shared_fixture(case):
    assert lane_for(case["task"]) == case["lane"]


@pytest.mark.parametrize("case", _FIXTURES, ids=_ids())
def test_is_waiting_matches_the_shared_fixture(case):
    assert is_waiting(case["task"]) is case["waiting"]


def test_every_task_status_the_product_can_produce_has_a_fixture():
    """A new TaskStatus must arrive with a lane decision, not fall through the
    unknown-status default in silence."""
    covered = {c["task"].get("status") for c in _FIXTURES}
    missing = sorted(s.value for s in TaskStatus if s.value not in covered)
    assert missing == [], f"TaskStatus values with no lane fixture: {missing}"


def test_the_node_test_reads_this_exact_fixture_file():
    """Both runners must read ONE file. A second copy would let them agree with
    themselves while disagreeing with each other.

    This is a necessary check, not a sufficient one - it proves no second copy
    exists, never that the node test ran. The execution half is
    ``test_the_js_implementation_agrees_on_every_shared_case`` below."""
    assert NODE_TEST_PATH.exists()
    assert "testdata/lane_conformance.json" in NODE_TEST_PATH.read_text()


def test_every_fixture_expects_a_real_lane_key():
    for case in _FIXTURES:
        assert case["lane"] in LANE_KEYS, case["name"]


def test_the_fixture_has_not_shrunk():
    """A deleted case is a deleted guarantee, and nothing else notices."""
    assert len(_FIXTURES) >= FIXTURE_FLOOR, (
        f"the shared fixture is down to {len(_FIXTURES)} cases (floor {FIXTURE_FLOOR}). "
        "Cases were deleted, or the floor was never raised when cases were added."
    )


def test_the_preserved_defect_pins_are_still_in_the_fixture():
    """The three PRESERVED DEFECT cases are the record that the defect is known
    and deliberate. Losing them turns a documented defect back into an unknown
    one, and the behaviour keeps passing either way."""
    pins = [c["name"] for c in _FIXTURES if c["name"].startswith("PRESERVED DEFECT")]
    assert len(pins) >= PRESERVED_DEFECT_PINS, f"only {len(pins)} preserved-defect pins left: {pins}"


# --------------------------------------------------------------------------- #
# The node implementation, driven from HERE                                    #
# --------------------------------------------------------------------------- #

def test_the_js_implementation_agrees_on_every_shared_case():
    """Run ``web/src/laneConformance.test.mjs`` and require it to pass.

    The fixture alone only guarantees agreement when BOTH runners run. Under
    ``.no_human.yml`` a src/-only change runs the Python suite and nothing else,
    so without this the Python half could be edited into disagreement with the
    JS and stay green (demonstrated: moving ``compound_parent`` to the answer
    lane in ``lanes.py`` + the fixture left pytest at 75 passed while node
    failed 4).

    Node absence FAILS rather than skips. A skip would move the same hole to a
    new place - a machine without node would silently reacquire the one-sided
    guarantee, and that is exactly the failure mode being fixed here. Failing is
    safe because node is already a hard requirement of this repo's test story:
    ``web/package.json`` runs ``node --test src/``, ``.no_human.yml`` shells
    ``node --test`` for every web-scoped change, and ``web/dist`` is built with
    it. This particular file also needs NO ``node_modules`` - it imports only
    ``node:`` builtins and ``./boardLanes.js``, which itself imports nothing -
    so it runs in a bare checkout with no install step.
    """
    node = shutil.which("node")
    assert node is not None, (
        "node is not on PATH, so the JS half of the lane conformance cannot be "
        "checked and the anti-drift guarantee is void. Install Node (>= 18.1 for "
        "`node --test`); this suite deliberately fails rather than skips, because "
        "a skip here is indistinguishable from a pass."
    )
    proc = subprocess.run(
        [node, "--test", "src/laneConformance.test.mjs"],
        cwd=WEB_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )
    # The exit code is necessary but NOT sufficient - see NODE_PASS_FLOOR.
    npass = _tap_counter(proc.stdout, "pass")
    nfail = _tap_counter(proc.stdout, "fail")
    if proc.returncode == 0 and nfail == 0 and npass is not None and npass >= NODE_PASS_FLOOR:
        return
    # Report the failing subtests and the TAP counts, not the whole passing
    # dump: a full TAP body is ~600 lines and pytest spends ~15s rewriting it
    # into an assertion message, which buries the one line that matters.
    interesting = [
        line for line in proc.stdout.splitlines()
        if line.startswith("not ok ") or line.startswith("# fail") or line.startswith("# pass")
    ]
    detail = "\n".join(interesting) or proc.stdout[-2000:]
    if proc.returncode == 0:
        why = (
            f"node --test exited 0 but reported pass={npass} fail={nfail}, and the "
            f"floor is {NODE_PASS_FLOOR} passing subtests (one per shared fixture "
            "case). That is the signature of a node file that did not actually run "
            "its assertions - no tests declared, every test skipped, or an early "
            "process.exit(0) - so this run proves nothing about JS/Python agreement."
        )
    else:
        why = (
            "web/src/boardLanes.js disagrees with no_human/core/lanes.py - the two "
            "implementations of the lane decision have drifted.\n"
            f"node --test src/laneConformance.test.mjs exited {proc.returncode}"
        )
    raise AssertionError(f"{why}\n{detail}\n{proc.stderr[-2000:]}")


# --------------------------------------------------------------------------- #
# Routing semantics, stated directly (not only via the fixture table)          #
# --------------------------------------------------------------------------- #

def test_blocked_splits_on_the_wake_condition():
    assert lane_for({"status": "blocked", "blocker_wake_condition": "ci_green_on:main"}) == "working"
    assert lane_for({"status": "blocked"}) == "answer"


def test_a_falsy_wake_condition_is_the_same_as_no_wake_condition():
    """Stated here as well as in the fixture, so deleting the fixture cases does
    not silently delete the guarantee. `""` and None must both mean "a human
    must act" - the split is on truthiness, not on `is not None`."""
    assert lane_for({"status": "blocked", "blocker_wake_condition": None}) == "answer"
    assert lane_for({"status": "blocked", "blocker_wake_condition": ""}) == "answer"
    assert is_waiting({"status": "blocked", "blocker_wake_condition": None}) is False
    assert is_waiting({"status": "blocked", "blocker_wake_condition": ""}) is False


def test_preserved_defect_a_human_stopped_task_does_not_leave_needs_answer():
    """PRESERVED DEFECT, pinned in code as well as in the fixture.

    `blocker_human_stopped` silences the "N need you" count (boardLanes.js
    isNeedsYou) but is invisible to routing, so a terminally-answered task sits
    in Needs Answer forever. Fixing that is a product decision about what the
    lane MEANS, out of scope for the port. These three assertions fail the day
    someone changes it, which is the point: it becomes a deliberate edit."""
    assert lane_for({"status": "escalated", "blocker_human_stopped": True}) == "answer"
    assert lane_for({"status": "awaiting_input", "blocker_human_stopped": True}) == "answer"
    assert lane_for({"status": "blocked", "blocker_human_stopped": True}) == "answer"


def test_a_status_is_never_claimed_by_two_lanes():
    seen: dict[str, str] = {}
    for key, statuses in LANE_STATUSES:
        for status in statuses:
            assert status not in seen, f"{status} claimed by {seen.get(status)} and {key}"
            seen[status] = key


def test_the_outcome_lanes_still_route():
    """The trap recorded in boardLanes.js: filter the routing table down to the
    lanes the board renders and finished work reappears as in-flight."""
    assert lane_for({"status": "done"}) == "done"
    assert lane_for({"status": "failed"}) == "failed"


def test_lane_for_reads_attributes_as_well_as_keys():
    """The board endpoint routes a TaskSummaryOut, not a dict."""
    class _Summary:
        status = "blocked"
        blocker_wake_condition = "ci_green_on:main"

    assert lane_for(_Summary()) == "working"
    assert is_waiting(_Summary()) is True


def test_lane_for_accepts_a_task_status_enum():
    class _Summary:
        status = TaskStatus.AWAITING_APPROVAL

    assert lane_for(_Summary()) == "review"


def test_lane_for_survives_none():
    assert lane_for(None) == "working"
    assert is_waiting(None) is False


def test_a_summary_built_outside_the_board_path_carries_no_lane():
    """`TaskSummaryOut.lane` defaults to None on purpose, and nothing else pins
    it: changing models.py to `lane: str | None = "working"` is a real behaviour
    change that no test noticed.

    It matters because a non-None default is INDISTINGUISHABLE from a routed
    lane on the wire, and routeTask prefers any valid served key over its own
    computation - so every summary from a non-board path (POST /api/tasks,
    /api/tasks/{id}) would pin the card to that default lane and the JS fallback
    would never run. None is what makes the fallback reachable."""
    assert TaskSummaryOut.model_fields["lane"].default is None
    summary = TaskSummaryOut.from_task(Task.new("no board, no lane", repo_path="/tmp/repo"))
    assert summary.lane is None
    assert summary.model_dump()["lane"] is None


# --------------------------------------------------------------------------- #
# The board endpoint ships the lane                                            #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [
        (TaskStatus.PENDING, "working"),
        (TaskStatus.IMPLEMENTING, "working"),
        (TaskStatus.AWAITING_APPROVAL, "review"),
        (TaskStatus.AWAITING_INPUT, "answer"),
        (TaskStatus.ESCALATED, "answer"),
        (TaskStatus.PAUSED_QUOTA, "working"),
        (TaskStatus.DONE, "done"),
        (TaskStatus.FAILED, "failed"),
    ],
)
async def test_board_endpoint_ships_the_lane(client, store, status, expected):
    t = Task.new(f"lane {status.value}", repo_path="/tmp/repo")
    await store.create_task(t)
    if status != TaskStatus.PENDING:
        event = {"source": "test", "kind": "test_seed"} if status is TaskStatus.DONE else None
        await store.set_status(t, status, validate=False, event=event)

    r = await client.get("/api/tasks")
    assert r.status_code == 200
    row = next(x for x in r.json() if x["id"] == t.id)
    assert row["lane"] == expected


@pytest.mark.asyncio
async def test_board_lane_follows_the_blocker_not_just_the_status(client, store):
    parked = Task.new("parked on its own signal", repo_path="/tmp/repo")
    await store.create_task(parked)
    await store.set_status(parked, TaskStatus.BLOCKED, validate=False)
    parked.blocker = {"question": "waiting on CI", "wake_condition": "ci_green_on:main"}
    await store.update_task(parked)

    stuck = Task.new("needs a human", repo_path="/tmp/repo")
    await store.create_task(stuck)
    await store.set_status(stuck, TaskStatus.BLOCKED, validate=False)
    stuck.blocker = {"question": "which database?"}
    await store.update_task(stuck)

    rows = {x["id"]: x for x in (await client.get("/api/tasks")).json()}
    assert rows[parked.id]["lane"] == "working"
    assert rows[stuck.id]["lane"] == "answer"


@pytest.mark.asyncio
async def test_board_lane_agrees_with_lane_for_on_every_row(client, store):
    """Whatever the endpoint sends must be what the shared function says - no
    second computation living in the endpoint."""
    for status in TaskStatus:
        t = Task.new(f"row {status.value}", repo_path="/tmp/repo")
        await store.create_task(t)
        if status != TaskStatus.PENDING:
            event = {"source": "test", "kind": "test_seed"} if status is TaskStatus.DONE else None
            await store.set_status(t, status, validate=False, event=event)

    rows = (await client.get("/api/tasks")).json()
    assert len(rows) == len(list(TaskStatus))
    for row in rows:
        assert row["lane"] == lane_for(row), row["status"]
