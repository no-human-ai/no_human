"""Windows and Linux "Check for updates" 404s because the release never
carries `latest.yml` / `latest-linux.yml`, even though `desktop/updater.mjs`
is designed to report a newer version on an unsigned build (a plain HTTPS
fetch of the feed that only refuses the INSTALL path, not the check).

The `linux` job's "Upload the packages" step already lists
`desktop/dist/latest-linux.yml`, but the `windows` job's equivalent step
omitted `desktop/dist/latest.yml`, and both steps' preceding comments
asserted the opposite of what `updater.mjs` actually does. This test reads
the workflow (electron-builder's own output files, `desktop/dist/*.yml`)
rather than re-deriving the fix, so it fails on the pre-fix workflow and
passes once both release jobs ship the feed file with an accurate comment.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _load_workflow():
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _upload_packages_step(job):
    for step in job["steps"]:
        if str(step.get("name", "")).startswith("Upload the packages"):
            return step
    raise AssertionError("no 'Upload the packages' step found in this job")


def test_windows_release_upload_includes_latest_yml():
    workflow = _load_workflow()
    step = _upload_packages_step(workflow["jobs"]["windows"])
    paths = step["with"]["path"]
    assert "desktop/dist/latest.yml" in paths.splitlines(), (
        "the windows release upload must include desktop/dist/latest.yml "
        "(electron-builder's nsis updater feed) or every Windows "
        "'Check for updates' 404s"
    )


def test_linux_release_upload_includes_latest_linux_yml():
    workflow = _load_workflow()
    step = _upload_packages_step(workflow["jobs"]["linux"])
    paths = step["with"]["path"]
    assert "desktop/dist/latest-linux.yml" in paths.splitlines(), (
        "the linux release upload must include desktop/dist/latest-linux.yml "
        "(electron-builder's AppImage updater feed) or every Linux "
        "'Check for updates' 404s"
    )


def test_upload_comments_state_the_check_still_works_unsigned():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    # De-wrap the YAML comment continuations ("\n      # ") so a sentence
    # split across source lines can still be matched as one string.
    flat = re.sub(r"\n +# ", " ", text)

    windows_sentence = (
        "latest.yml is included to enable the in-app updater check to "
        "report newer versions; nhCanAutoUpdate=false still prevents "
        "auto-install"
    )
    linux_sentence = (
        "latest-linux.yml is included to enable the in-app updater check "
        "to report newer versions; nhCanAutoUpdate=false still prevents "
        "auto-install"
    )
    assert windows_sentence in flat, (
        "the windows upload step's comment must state why latest.yml ships "
        "despite nhCanAutoUpdate=false"
    )
    assert linux_sentence in flat, (
        "the linux upload step's comment must state why latest-linux.yml "
        "ships despite nhCanAutoUpdate=false"
    )
