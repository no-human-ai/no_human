"""The two `--deselect` lists must say the same thing.

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


def test_the_two_deselect_lists_are_identical():
    workflow = _workflow_deselects()
    nightly = _nightly_lane_deselects()
    assert workflow == nightly, (
        "the --deselect lists in .github/workflows/ci.yml and "
        "scripts/run_tests.sh's nightly lane have drifted apart.\n"
        f"  only in ci.yml:        {sorted(workflow - nightly)}\n"
        f"  only in run_tests.sh:  {sorted(nightly - workflow)}\n"
        "A test deselected in one but not the other means a green local run "
        "stops predicting CI. Change both, or neither.")


def test_both_lists_are_non_empty_and_name_real_tests():
    """A control. If a refactor made either extractor return nothing, the
    equality test above would pass vacuously and prove nothing."""
    workflow = _workflow_deselects()
    assert workflow, "extracted no deselects from ci.yml -- the extractor broke"
    for node_id in workflow:
        path, sep, name = node_id.partition("::")
        assert sep and name, f"{node_id!r} is not a path::test node id"
        assert (ROOT / path).is_file(), (
            f"ci.yml deselects {node_id!r} but {path} does not exist -- a "
            "deselect for a test that moved or was deleted is dead config")
