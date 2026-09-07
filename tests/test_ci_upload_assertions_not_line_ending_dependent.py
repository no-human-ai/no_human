"""Regression test for the public-main CRLF break introduced in 72ca3a0b.

`desktop/packagedFiles.test.mjs` and `tests/test_release_updater_feed_shipped.py`
each asserted two full sentences of ci.yml *comment* prose by de-wrapping YAML
comment continuations with a `\n`-only regex (`ciYaml.replace(/\n +# /g, " ")`
/ `re.sub(r"\n +# ", " ", text)`). On a Windows runner, git checks ci.yml out
with CRLF line endings, so the continuation is "\r\n      # " and the regex
never matches - the prose assertion fails even though the release contract
(the upload step actually shipping latest.yml / latest-linux.yml) is intact.

The fix deletes the prose assertions and the now-unused de-wrap in both
files, keeping only the line-ending-independent path-list assertions. This
test fails on the pre-fix files (both still contain the `flat` de-wrap and
the prose sentence) and passes once they are gone.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_TEST = REPO_ROOT / "desktop" / "packagedFiles.test.mjs"
PYTHON_TEST = REPO_ROOT / "tests" / "test_release_updater_feed_shipped.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

BANNED_SNIPPETS = (
    "nhCanAutoUpdate=false still prevents",
    "flat",
)


def test_ci_yml_comment_prose_assertions_and_dewrap_are_gone():
    for test_file in (DESKTOP_TEST, PYTHON_TEST):
        content = test_file.read_text(encoding="utf-8")
        for snippet in BANNED_SNIPPETS:
            assert snippet not in content, (
                f"{test_file.relative_to(REPO_ROOT)} still contains {snippet!r}: "
                "a comment-prose assertion (or its \\n-only de-wrap) over ci.yml "
                "survives, and it will fail on a CRLF (Windows runner) checkout"
            )


def test_release_upload_path_list_assertions_still_present():
    js = DESKTOP_TEST.read_text(encoding="utf-8")
    assert r"desktop\/dist\/latest\.yml" in js
    assert r"desktop\/dist\/latest-linux\.yml" in js

    py = PYTHON_TEST.read_text(encoding="utf-8")
    assert "desktop/dist/latest.yml" in py
    assert "desktop/dist/latest-linux.yml" in py


def test_retained_path_list_contract_survives_a_crlf_checkout():
    # Simulates the Windows runner: git checks ci.yml out with CRLF line
    # endings there. The surviving assertions must not depend on \n-only
    # parsing the way the deleted comment-prose regex did.
    lf_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    crlf_text = lf_text.replace("\r\n", "\n").replace("\n", "\r\n")
    workflow = yaml.safe_load(crlf_text)

    for job_name, feed_file in (("windows", "latest.yml"), ("linux", "latest-linux.yml")):
        upload_step = next(
            step for step in workflow["jobs"][job_name]["steps"]
            if str(step.get("name", "")).startswith("Upload the packages")
        )
        paths = upload_step["with"]["path"]
        assert f"desktop/dist/{feed_file}" in paths.splitlines(), (
            f"{job_name} upload step must ship desktop/dist/{feed_file} even "
            "when ci.yml is checked out with CRLF line endings"
        )
