"""`Desktop shell` must run for a PR that touches `desktop/`, label or not.

PR #279 changed exactly one file -- `desktop/package-lock.json`, a js-yaml
security bump that ships inside the packaged app's `app.asar` -- and carried
no labels. The three packaging jobs gate on `github.event_name !=
'pull_request' || label 'desktop'`, so all three skipped, and the only desktop
signal for that change arrived after it had merged, on the push to main.

The `changed` job asks the files API what the PR touched. These tests cover
both halves of it: the workflow wiring (which is what silently regresses) and
the shell script itself, driven over a stubbed `gh` so the matching logic is
executed rather than read.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _workflow():
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _changed_script() -> str:
    """The `run:` body of the one step in the `changed` job."""
    steps = _workflow()["jobs"]["changed"]["steps"]
    runs = [s["run"] for s in steps if "run" in s]
    assert len(runs) == 1, f"expected one run step in `changed`, got {len(runs)}"
    return runs[0]


def test_desktop_job_consumes_the_changed_job():
    desktop = _workflow()["jobs"]["desktop"]
    assert desktop.get("needs") == "changed"
    condition = desktop["if"]
    # The paths clause is the new half...
    assert "needs.changed.outputs.desktop == 'true'" in condition
    # ...and neither of the two existing ways in may be dropped for it.
    assert "github.event_name != 'pull_request'" in condition
    assert "contains(github.event.pull_request.labels.*.name, 'desktop')" in condition


def test_the_changed_job_is_never_conditional():
    """A skipped dependency skips its dependents, exactly like a failed one.

    An `if:` here that excluded pushes would therefore take `Desktop shell`
    off the push to main -- the one run that covers it today.
    """
    assert "if" not in _workflow()["jobs"]["changed"]


def test_a_failing_changed_job_cannot_silence_the_push_run():
    assert _workflow()["jobs"]["desktop"]["if"].startswith("!cancelled()")


def test_the_changed_job_may_read_pull_requests():
    # `gh api .../pulls/N/files` is unauthorized for a fork PR without it.
    assert _workflow()["jobs"]["changed"]["permissions"]["pull-requests"] == "read"


def _run_changed_step(
    tmp_path: Path, *, files: list[str], gh_exit: int = 0, **env_overrides
) -> dict[str, str]:
    """Execute the `changed` job's script with `gh` stubbed to `files`.

    `gh_exit` makes the stub fail instead, which is how the API-error path is
    driven rather than read.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/bin/sh\n"
        + "".join(f'echo "{name}"\n' for name in files)
        + (f"exit {gh_exit}\n" if gh_exit else ""),
        encoding="utf-8",
    )
    stub.chmod(0o755)

    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "no-human-ai/no_human",
        "GITHUB_EVENT_NAME": "pull_request",
        "PR_NUMBER": "279",
        "CHANGED_FILES": str(len(files)),
    }
    env.update(env_overrides)

    result = subprocess.run(
        ["bash", "-c", _changed_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    return dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


@pytest.mark.parametrize(
    "files, expected",
    [
        # The PR that motivated this, verbatim.
        (["desktop/package-lock.json", "RELEASE_MANIFEST.txt"], "true"),
        (["desktop/main.mjs"], "true"),
        # The negative control: without it, a script that always says "true"
        # would pass every case above.
        (["src/no_human/config.py", "tests/test_config.py"], "false"),
        # `desktop` must anchor at the path root, not match anywhere in it.
        (["docs/desktop/notes.md", "src/desktop_helper.py"], "false"),
    ],
)
def test_the_script_matches_desktop_paths(tmp_path, files, expected):
    assert _run_changed_step(tmp_path, files=files)["desktop"] == expected


def test_a_push_has_no_pull_request_to_ask_about(tmp_path):
    outputs = _run_changed_step(
        tmp_path, files=["desktop/main.mjs"], PR_NUMBER="", GITHUB_EVENT_NAME="push"
    )
    assert outputs["desktop"] == "false"


def test_a_diff_past_the_api_cap_runs_the_job_rather_than_skipping_it(tmp_path):
    """3000 files is where the endpoint stops paginating.

    Beyond it, "no desktop files" would describe the cap, not the diff.
    """
    outputs = _run_changed_step(
        tmp_path, files=["src/no_human/config.py"], CHANGED_FILES="3001"
    )
    assert outputs["desktop"] == "true"


def test_a_failing_files_api_runs_the_job_rather_than_skipping_it(tmp_path):
    """An API error tells us nothing about the diff, so it must not read as
    "no desktop files".

    Without this, `set -e` fails the `changed` job, `outputs.desktop` comes
    back empty, and on a pull request `Desktop shell` skips -- reintroducing
    the PR #279 outcome through a different door. The push-to-main run is
    already covered by `!cancelled()`; this is the pull-request half.
    """
    outputs = _run_changed_step(
        tmp_path, files=["desktop/main.mjs"], gh_exit=1
    )
    assert outputs["desktop"] == "true"
