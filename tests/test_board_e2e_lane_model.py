"""Browser-free drift guard for the board E2E harness (e2e/board_e2e.py).

The e2e script itself needs a browser and a running server, so it cannot run
in CI. What CAN run in CI — and does, as a plain pytest module, no
`repoguard`/`slow`/`nightly` marker — is a pin that the script's *expectations*
(lane titles, status->lane routing, demo card counts) still agree with their
three sources of truth: ``web/src/boardLanes.js`` (via ``e2e/lane_model.py``'s
regex parse), ``testdata/lane_conformance.json`` (the same fixture
``tests/test_lane_conformance.py`` and ``web/src/laneConformance.test.mjs``
already run), and ``e2e/serve_demo.py``'s own ``DEMO_TASKS``-derived counts.

If any of those three ever move again without a matching e2e update, this
file goes red without anyone needing a browser to find out.

``e2e/`` is not a package (no ``__init__.py``, and it must stay importable as
a standalone script), so its modules are loaded by file path via
``importlib.util.spec_from_file_location`` rather than a normal import.
``e2e/board_e2e.py`` itself is read as plain text for the literal-drift
guards below (it also imports ``playwright``, which is not a dependency of
the default install — see ``test_module_imports_without_playwright``).
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

from no_human.core.lanes import LANE_KEYS, lane_for

REPO_ROOT = Path(__file__).resolve().parents[1]
E2E_DIR = REPO_ROOT / "e2e"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lane_model = _load_module("_e2e_lane_model_under_test", E2E_DIR / "lane_model.py")
serve_demo = _load_module("_e2e_serve_demo_under_test", E2E_DIR / "serve_demo.py")

BOARD_E2E_PATH = E2E_DIR / "board_e2e.py"
BOARD_E2E_SRC = BOARD_E2E_PATH.read_text(encoding="utf-8")
README_PATH = E2E_DIR / "README.md"
README_TEXT = README_PATH.read_text(encoding="utf-8")

# The static (source-text) count of `check(...)` call sites in board_e2e.py
# right after this rewrite — a tamper-guard floor. Loop bodies generate many
# more *runtime* checks than this from a handful of call sites, so this is
# deliberately a floor on call SITES, not on the dynamic total the script
# prints. Raise it if the script gains checks; never lower it.
CHECK_FLOOR = 54


def test_board_lane_titles_match_boardLanes_js():
    assert lane_model.BOARD_LANE_TITLES == ("NEEDS ANSWER", "WORKING", "REVIEW PR")
    reparsed = lane_model.parse_lanes()
    reparsed_titles = tuple(l.label.upper() for l in reparsed if not l.outcome)
    assert lane_model.BOARD_LANE_TITLES == reparsed_titles


def test_outcome_lanes_are_done_and_failed_and_not_on_the_board():
    assert set(lane_model.OUTCOME_LANE_KEYS) == {"failed", "done"}
    assert set(lane_model.OUTCOME_LANE_KEYS).isdisjoint(lane_model.BOARD_LANE_KEYS)
    assert set(lane_model.OUTCOME_LANE_TITLES) == {"FAILED", "DONE"}


def test_parsed_lane_keys_match_core_lanes_py():
    js_keys = tuple(l.key for l in lane_model.LANES)
    assert js_keys == LANE_KEYS


def test_status_lane_expectations_come_from_the_conformance_fixture():
    assert lane_model.FIXTURE_CASES, "fixture must not be empty"
    statuses_seen = set()
    for case in lane_model.FIXTURE_CASES:
        task = case["task"]
        status = task.get("status")
        wake = task.get("blocker_wake_condition")
        expected_lane = case["lane"]
        statuses_seen.add(status)
        assert lane_model.fixture_lane(status, wake) == expected_lane, case["name"]
        assert lane_for(task) == expected_lane, case["name"]
    assert len(statuses_seen) >= 5, (
        f"only {len(statuses_seen)} distinct statuses exercised — fixture "
        "coverage looks vacuous"
    )


def test_no_waiting_or_parked_lane_in_boardLanes_js():
    # A real invariant on the lane model itself: no lane is literally named
    # Waiting/Parked/Needs You. Note this deliberately does NOT grep
    # board_e2e.py or README.md for these bare words — both legitimately use
    # "waiting"/"parked" as prose (a waiting *tag* on a Working-lane card, a
    # parked *task* with a real blocker), which is accurate now that there is
    # no such lane, not a sign one still exists.
    banned = ("Waiting", "WAITING", "Parked", "PARKED", "NEEDS YOU")
    js_labels = [l.label for l in lane_model.LANES]
    for term in banned:
        assert not any(term in label for label in js_labels), (
            f"{term!r} found in a lane label: {js_labels}"
        )


def test_readme_and_script_drop_the_stale_nine_lane_claim():
    # The specific false claims this ticket exists to remove — phrased as
    # claims (a lane, or a count), not bare words, so accurate prose that
    # uses "waiting"/"parked" to describe the CURRENT model doesn't trip it.
    banned_phrases = (
        "nine lane",
        "Nine lane",
        "Parked** for",
        "Parked lane",
        "Waiting lane",
        "Waiting** for",
    )
    for phrase in banned_phrases:
        assert phrase not in BOARD_E2E_SRC, f"{phrase!r} found in board_e2e.py"
        assert phrase not in README_TEXT, f"{phrase!r} found in e2e/README.md"


def test_no_hardcoded_lane_titles_in_e2e_script():
    for title in (*lane_model.BOARD_LANE_TITLES, *lane_model.OUTCOME_LANE_TITLES):
        for quote in ('"', "'"):
            literal = f"{quote}{title}{quote}"
            assert literal not in BOARD_E2E_SRC, (
                f"board_e2e.py hardcodes lane title literal {literal} instead "
                "of deriving it from lane_model"
            )


def test_no_hardcoded_demo_card_count():
    assert not re.search(r'\.task-card"\)\.count\(\)\s*==\s*\d+', BOARD_E2E_SRC), (
        "board_e2e.py hardcodes a task-card count instead of using "
        "serve_demo.board_card_count()"
    )
    assert "board_card_count()" in BOARD_E2E_SRC


def test_demo_card_count_is_derived_and_excludes_outcomes():
    independent_counts: dict[str, int] = {}
    for spec in serve_demo.DEMO_TASKS:
        task = serve_demo._synthetic_task(spec)
        key = lane_for(task)
        independent_counts[key] = independent_counts.get(key, 0) + 1

    expected_board_total = sum(
        independent_counts.get(lane.key, 0) for lane in lane_model.BOARD_LANES
    )
    assert serve_demo.board_card_count() == expected_board_total

    for lane in lane_model.BOARD_LANES:
        assert independent_counts.get(lane.key, 0) >= 1, (
            f"no DEMO_TASKS entry routes to board lane {lane.key!r}"
        )
    for lane in lane_model.OUTCOME_LANES:
        assert independent_counts.get(lane.key, 0) >= 1, (
            f"no DEMO_TASKS entry routes to outcome lane {lane.key!r}"
        )


def test_serve_demo_imports_without_argv_side_effects(tmp_path, monkeypatch):
    demo_db = tmp_path / "should_not_be_created_by_import.db"
    monkeypatch.setenv("NH_DEMO_DB", str(demo_db))
    monkeypatch.setattr(sys, "argv", ["pytest", "-q", "-n", "4", "--some-flag"])
    fresh = _load_module("_e2e_serve_demo_argv_check", E2E_DIR / "serve_demo.py")
    assert len(fresh.DEMO_TASKS) >= 1
    assert not demo_db.exists(), "importing serve_demo must not touch NH_DEMO_DB"


def test_approve_merge_disabled_in_demo_config():
    cfg = serve_demo._fake_load_config()
    assert cfg.data["approve_merge"]["enabled"] is False


def test_module_imports_without_playwright():
    tree = ast.parse(BOARD_E2E_SRC, filename=str(BOARD_E2E_PATH))
    for node in tree.body:  # top level only — inside run() is fine
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any("playwright" in n for n in names), (
            f"playwright imported at module scope: {ast.dump(node)}"
        )
    assert "_e2e_board_e2e_import_check" not in sys.modules
    # The runtime claim is that LOADING this module pulls in no playwright —
    # not that the whole test process never touched it: another test in the
    # same xdist worker may import playwright for real (the probe-parity
    # suite does, by design), which is outside this module's control. The
    # AST walk above already pins the module-scope invariant regardless.
    playwright_preloaded = "playwright" in sys.modules
    module = _load_module("_e2e_board_e2e_import_check", BOARD_E2E_PATH)
    if not playwright_preloaded:
        assert "playwright" not in sys.modules
    assert callable(module.run)


def test_readme_documents_the_current_lanes_and_recipe():
    for expected in (
        "Needs Answer",
        "Working",
        "Review PR",
        "Outcomes",
        "Done",
        "Failed",
        "NH_E2E_BASE",
        "NH_E2E_SHOTS",
        "8488",
        "tests/test_board_e2e_lane_model.py",
    ):
        assert expected in README_TEXT, f"README missing {expected!r}"
    # The false claims this ticket removes — see
    # test_readme_and_script_drop_the_stale_nine_lane_claim for the phrase
    # list; not repeated here as bare words since the README legitimately
    # uses "Waiting"/"Parked" as prose to describe their *absence* as lanes.


def test_check_count_did_not_shrink():
    call_sites = len(re.findall(r'(?<!def )\bcheck\(', BOARD_E2E_SRC))
    assert call_sites >= CHECK_FLOOR, (
        f"board_e2e.py is down to {call_sites} check(...) call sites "
        f"(floor {CHECK_FLOOR}, set to the count as of this rewrite). "
        "Never lower this floor to make a test pass — restore the check."
    )
