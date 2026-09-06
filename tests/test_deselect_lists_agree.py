"""The two `--deselect` lists must say the same thing, apart from entries that
are deliberately and temporarily different.

`.github/workflows/ci.yml` deselects tests in the `Python` job, and
`scripts/run_tests.sh` deselects them again in its `nightly` lane. Nothing
checked that the two agreed, and they are kept in step by hand.

That is not hypothetical. Issue #19 asked for KI-1's test to be re-enabled and
named only `ci.yml`; the deselect was in both files, and removing one without
the other would have left the local nightly lane skipping a test CI runs. Both
files already warn about exactly this in prose -- run_tests.sh says "the list
that drifts is the list nobody reconciles", and its header says "if the
selectors here and there disagree, a green local run stops predicting the CI
result" -- so this is that reconciliation, as a test rather than a hope.

One divergence is currently intended, and it is listed below with its reason.
The point of writing it down is that an intended difference and an accidental
one look identical in a diff; only one of them has an entry here.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
RUN_TESTS_PATH = ROOT / "scripts" / "run_tests.sh"

#: `--deselect <node id>`, in either file. The node id is the next token.
_DESELECT_RE = re.compile(r"--deselect\s+(\S+)")

#: Node ids the nightly lane deselects that `ci.yml` does not, each with why.
#: An entry here is a promise to remove it, not a permanent exemption.
_NIGHTLY_ONLY: dict[str, str] = {
    "tests/test_scheduler.py::test_two_repos_run_concurrently_in_worktrees": (
        "KI-1. Re-measured clean (0 failures in 400 serial runs and 13 "
        "whole-suite -n 4 runs) and selected again in ci.yml, but held back "
        "from the nightly lane until main has accumulated push history, "
        "because scripts/nightly_eval.sh makes this lane's exit code its own "
        "verdict, so a residual rate would mask the eval signal rather than "
        "just failing one job. Remove this entry and the deselect together."
    ),
}


def _workflow_deselects() -> set[str]:
    """Node ids deselected by the `Run tests` step of the `Python` job."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["python"]["steps"]
    run_steps = [s for s in steps if s.get("name") == "Run tests"]
    assert len(run_steps) == 1, (
        f"expected exactly one 'Run tests' step in the Python job, "
        f"found {len(run_steps)}")
    return set(_DESELECT_RE.findall(run_steps[0]["run"]))


def _nightly_lane_deselects() -> set[str]:
    """Node ids deselected by `run_tests.sh`'s `nightly` case arm only.

    Scoped to that arm rather than the whole file so a deselect added to a
    different mode cannot silently satisfy this comparison.
    """
    text = RUN_TESTS_PATH.read_text(encoding="utf-8")
    start = text.index("\n  nightly)")
    end = text.index("\n    ;;", start)
    return set(_DESELECT_RE.findall(text[start:end]))


def test_the_two_deselect_lists_agree_apart_from_documented_exceptions():
    workflow = _workflow_deselects()
    nightly = _nightly_lane_deselects()

    only_in_nightly = nightly - workflow
    only_in_workflow = workflow - nightly

    assert only_in_workflow == set(), (
        "ci.yml deselects tests the nightly lane runs: "
        f"{sorted(only_in_workflow)}.\n"
        "There is no exception list for this direction, because a test CI "
        "skips and the nightly lane runs is a local red that CI will never "
        "reproduce. Deselect it in both, or neither.")

    undocumented = only_in_nightly - set(_NIGHTLY_ONLY)
    assert undocumented == set(), (
        "the nightly lane deselects tests ci.yml runs, with no reason "
        f"recorded: {sorted(undocumented)}.\n"
        "A green local nightly run then stops predicting CI. Either deselect "
        "them in ci.yml too, or add an entry to _NIGHTLY_ONLY in this file "
        "saying why the difference is deliberate and what removes it.")


def test_every_documented_exception_is_still_needed():
    """The exception list is itself a list that can drift.

    If a node id in `_NIGHTLY_ONLY` is no longer deselected in the nightly
    lane, the entry has outlived its reason and should go, or the next reader
    inherits a stale explanation for a difference that no longer exists.
    """
    nightly = _nightly_lane_deselects()
    stale = set(_NIGHTLY_ONLY) - nightly
    assert stale == set(), (
        f"_NIGHTLY_ONLY still documents {sorted(stale)}, but the nightly lane "
        "no longer deselects it. Delete the entry.")


def test_both_lists_are_non_empty_and_name_real_tests():
    """A control. If a refactor made either extractor return nothing, the
    comparison above would pass vacuously and prove nothing."""
    workflow = _workflow_deselects()
    assert workflow, "extracted no deselects from ci.yml -- the extractor broke"
    assert _nightly_lane_deselects(), (
        "extracted no deselects from run_tests.sh -- the extractor broke")
    for node_id in workflow | _nightly_lane_deselects():
        path, sep, name = node_id.partition("::")
        assert sep and name, f"{node_id!r} is not a path::test node id"
        assert (ROOT / path).is_file(), (
            f"{node_id!r} is deselected but {path} does not exist -- a "
            "deselect for a test that moved or was deleted is dead config")
