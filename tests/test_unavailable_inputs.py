"""C2 — honest handling of unavailable inputs (deterministic detector).

Verifies the detector FIRES on genuine dangling references to inputs the agent
cannot see, does NOT fire on topical mentions (precision — a false escalation is
worse than a miss), and treats an attachment as satisfying the reference.
"""

import pytest

from no_human.intake.unavailable_inputs import (
    detect_unavailable_input_refs,
    find_input_refs,
    missing_input_blocker,
)
from no_human.blockers.taxonomy import BlockerCategory, TaskStatus, route_for


# --- SHOULD fire: clearly referential markers -------------------------------
@pytest.mark.parametrize("text", [
    "Fix the bug shown in [Image #1]",
    "The layout is broken — see [Image #4]",
    "Repro steps are in [attachment: repro.png]",
    "The expected output is in [screenshot: hero.png]",
    "Reference [screenshot #2] for the hover state",
    "See [Image #2] for the layout, then [Image #3] for the hover state",
    "As shown in the figure, the header is sticky",
    "Build the form as depicted in the mockup",
    "As illustrated in the wireframe, the nav is fixed",
    "The colors are as seen in the screenshot",
    "Match the spacing as shown in the mockup we discussed",
])
def test_referential_mentions_fire(text):
    assert find_input_refs(text), f"expected a ref in: {text!r}"


# --- SHOULD NOT fire: topical mentions (precision guard) ---------------------
# Includes every false positive found by the PR #172 review — these are the
# realistic dev-task idioms the detector must never escalate on.
@pytest.mark.parametrize("text", [
    # originals
    "Add a screenshot button to the toolbar",
    "Store the uploaded image under ~/.no_human/attachments",
    "The <img> tag needs an alt attribute",
    "Log the diagram-export duration",
    "Rename the Figure component to Chart",
    "Compress images larger than 1MB before upload",
    "Add a 'attach file' affordance to the composer",
    "Write a function that returns the photo count",
    "Document how attachments are stored",
    "Fix flaky test in test_image_pipeline.py",
    # position/size idioms with above/below (review CRITICAL)
    "Do not resize images above 2MB; reject images above the limit",
    "Images below the fold are cut off",
    "Photos below the fold should lazy-load",
    "Images above the fold lazy-load eagerly",
    # "as shown in the code below" — inline example, not a visual (review CRITICAL)
    "Update the onboarding flow as shown in the code below: def onboard(): ...",
    "Return the same shape as shown in the test above",
    # "following" list introducer (review HIGH)
    "The following documents describe the API contract: docs/api.md",
    "The following files are ignored by git",
    # bare pointer verbs see/refer/per/check (review HIGH)
    "Check the images render correctly across themes",
    "Check the screenshots directory for stale assets",
    "Per the mockup we discussed, add a sidebar",
    "See the pictures folder for the raw assets",
    "Refer to the figures in the report for the numbers",
    # "here"/enclosed filler + generic-noun collisions (review MED)
    "The pictures here look wrong after the migration",
    "The enclosed files list endpoint returns 500",
    "The attached-files list endpoint needs pagination",
    # attachment-DOMAIN reduced relatives / compounds (re-review HIGH) — the
    # whole reason the "attached/enclosed" free-text family was dropped
    "When an image attached to a comment exceeds 5MB, generate a thumbnail",
    "Cascade-delete all images attached to a ticket when it is removed",
    "Add pagination to the screenshots attached to a bug report",
    "Compress photos enclosed in the export archive before upload",
    "Fix the N+1 query when loading diagrams attached to each workflow node",
    "Validate that wireframes attached to a project are PNG or SVG",
    "Rate-limit the diagrams attached endpoint",
    "The attached images table stores blobs",
    "Match the attached screenshot component to the design system",
    "Build the enclosed mockup viewer",
    # markdown link that is NOT an image (no leading '!') must not fire
    "See the [contributing guide](docs/CONTRIBUTING.md) before starting",
    # "as shown in the <non-visual>" (inline example, not a picture)
    "Return the same JSON as shown in the example above",
    # code subscripts / generics / component refs (3rd-review CONFIRMED) — the
    # bracket rule must not read a language subscript as a pasted chat marker
    "The endpoint should return list[Image] from the gallery",
    "Annotate the helper as Dict[str, Image]",
    "Type the buffer as list[Screenshot]",
    "Read arr[image] and skip nulls",
    "Sort by df[figure] descending",
    "x = data[figure] then normalize",
    "Render the [Image] component in React",
    "Refactor the [Image] component to forwardRef",
    "Caption references like [figure 1] should be linkified",
    # "as shown in the <visual noun> <code/location noun>" (3rd-review PLAUSIBLE)
    "Fixtures live where the assets are, as shown in the images directory",
    "Wire it up as illustrated in the diagram module tests",
    "Numbers match as shown in the figures table",
    # more "as shown in the <visual noun> <content noun>" — denylist would have
    # escaped these; the following-token allowlist rejects them (4th-review LOW)
    "Model it as shown in the diagram spec",
    "Lay it out as depicted in the figure gallery",
    "Render as shown in the screenshot grid",
    "Position it as shown in the mockup panel",
    "Route as illustrated in the diagram view",
    # T-SQL / SQL Server bracket-quoted identifiers (4th-review HIGH) — bare
    # [attachment]/[screenshot] are ordinary column names, not chat markers
    "SELECT [attachment] FROM messages",
    "WHERE [screenshot] IS NULL ORDER BY id",
    "Add an index on the [attachment] column",
    "ALTER TABLE msg ADD [screenshot] NVARCHAR(MAX)",
    "SELECT t.[attachment], t.[screenshot] FROM t",
    # markdown / prose / issue-title bracket tags (4th-review MED)
    "The [screenshot] test is flaky on CI",
    "- [screenshot] flaky under xdist",
    "[Screenshot] Homepage hero is blurry on Safari",
    "See the [screenshot](https://example.com/s.png) link",
])
def test_topical_mentions_do_not_fire(text):
    assert find_input_refs(text) == [], f"false positive on: {text!r}"


def test_dangling_when_no_attachment():
    refs = detect_unavailable_input_refs("See [Image #2] for the bug", None)
    assert refs and "[image #2]" in refs[0]


def test_attachment_is_trusted_no_escalation():
    # An attachment present → the reference is assumed satisfied, no escalation.
    assert detect_unavailable_input_refs(
        "Fix the bug in [Image #1]",
        [{"name": "shot.png", "path": "/x/shot.png"}],
    ) == []


def test_no_reference_no_escalation():
    assert detect_unavailable_input_refs(
        "Add a --json flag to nh status", None) == []
    assert detect_unavailable_input_refs("", None) == []


def test_dedup_preserves_order():
    refs = find_input_refs("[Image #1] then again [Image #1], and as shown in the mockup")
    # two distinct markers, the repeated one collapsed
    assert len(refs) == 2


def test_blocker_is_awaiting_input_and_names_the_ref():
    b = missing_input_blocker(["[Image #1]"], goal="fix header")
    assert b.category == BlockerCategory.AMBIGUITY
    # AMBIGUITY routes to a reply-to-resume state (human provides the input),
    # NOT a terminal escalation.
    assert route_for(b.category).target_status == TaskStatus.AWAITING_INPUT
    assert "[Image #1]" in b.question
    assert b.transient is False


# --- orchestrator wiring (_unavailable_input_blocker) -----------------------

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover — never called here
        raise AssertionError("backend must not run for a C2 escalation")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _task(desc, *, context=None):
    t = Task.new("do the thing", repo_path="/x")
    t.description = desc
    t.context = context or {}
    return t


async def test_wiring_fires_on_dangling_ref(store, tmp_path):
    orch = _orch(store, tmp_path)
    b = orch._unavailable_input_blocker(_task("Fix the bug in [Image #1]"))
    assert b is not None and b.category == BlockerCategory.AMBIGUITY


async def test_wiring_silent_on_plain_task(store, tmp_path):
    orch = _orch(store, tmp_path)
    assert orch._unavailable_input_blocker(_task("Add a --json flag")) is None


async def test_wiring_trusts_attachment(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = _task("Match the attached screenshot",
              context={"attachments": [{"name": "s.png", "path": "/s.png"}]})
    assert orch._unavailable_input_blocker(t) is None


async def test_wiring_skips_when_already_escalated(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = _task("See [Image #1]", context={"c2_input_escalated": True})
    assert orch._unavailable_input_blocker(t) is None


async def test_wiring_skips_when_grill_completed_by_human(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = _task("See [Image #1]", context={"grill_complete": True})
    assert orch._unavailable_input_blocker(t) is None
