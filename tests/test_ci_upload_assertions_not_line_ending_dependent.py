"""Regression test for the public-main CRLF break introduced in 72ca3a0b.

`desktop/packagedFiles.test.mjs` and `tests/test_release_updater_feed_shipped.py`
used to assert two full sentences of ci.yml *comment* prose by de-wrapping
YAML comment continuations with a `\n`-only regex
(`ciYaml.replace(/\n +# /g, " ")` / `re.sub(r"\n +# ", " ", text)`). On a
Windows runner, git checks ci.yml out with CRLF line endings, so the
continuation is "\r\n      # " and the regex never matched - the prose
assertion failed even though the release contract (the upload step actually
shipping latest.yml / latest-linux.yml) was intact. That fix deleted the
prose assertions and kept only the line-ending-independent path-list
assertions in both files.

This test pins the surviving artefact directly: it parses ci.yml (simulating
a CRLF checkout) and asserts the Windows and Linux upload steps' own `with.path`
list still ships latest.yml / latest-linux.yml, independent of line endings.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


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
