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

import json
import os
import re
import shutil
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
    # The paths clause is the new half. `!= 'false'`, never `== 'true'`:
    # a `changed` job that dies leaves the output EMPTY, and empty is not
    # `'true'`, so `== 'true'` skipped Desktop shell on exactly the failures
    # the script's own fail-open branch cannot reach.
    assert "needs.changed.outputs.desktop != 'false'" in condition
    assert "== 'true'" not in condition
    # ...and neither of the two existing ways in may be dropped for it.
    assert "github.event_name != 'pull_request'" in condition
    assert "contains(github.event.pull_request.labels.*.name, 'desktop')" in condition


def test_the_changed_job_is_never_conditional():
    """A skipped dependency skips its dependents, exactly like a failed one.

    An `if:` here that excluded pushes would therefore take `Desktop shell`
    off the push to main -- the one run that covers it today.
    """
    assert "if" not in _workflow()["jobs"]["changed"]


def test_a_dead_changed_job_runs_desktop_shell_rather_than_silencing_it():
    """Pins the PROPERTY, not the spelling.

    An earlier round asserted the literal `== 'true'` and thereby forbade the
    correct expression; the round after it asserted `"always()" not in
    condition` and did the same thing in the other direction, on a question
    neither round had measured. Both are the same mistake: freezing an answer
    a test was never in a position to decide.

    What actually has to hold is that a `changed` job which DIED still runs
    Desktop shell. Two spellings survive an upstream failure, so either is
    accepted here; the `ci.yml` comment records why `always()` is the one
    chosen and what would settle it.
    """
    condition = _workflow()["jobs"]["desktop"]["if"]
    assert condition.startswith(("always()", "!cancelled()")), condition
    # The real guard: empty (a dead job) is not 'false', so the job runs.
    assert "needs.changed.outputs.desktop != 'false'" in condition


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
    argv_log = tmp_path / "gh_argv"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {argv_log}\n'
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

    # `bash -e <file>`, matching the runner. GitHub invokes a `run:` step as
    # `/usr/bin/bash -e {0}` (confirmed in the job log), and `bash -c` does
    # NOT imply `-e`: deleting `set -euo pipefail` from the script left this
    # suite green while the same script under the runner's shell exited 1 with
    # an empty $GITHUB_OUTPUT -- which is precisely the "dead changed job"
    # state the gate above exists to survive.
    script_file = tmp_path / "changed_step.sh"
    script_file.write_text(_changed_script(), encoding="utf-8")
    result = subprocess.run(
        ["bash", "-e", str(script_file)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"script failed: {result.stderr}"
    if argv_log.exists():
        (tmp_path / "gh_argv_seen").write_text(
            argv_log.read_text(encoding="utf-8"), encoding="utf-8"
        )
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
    already covered by `always()`; this is the pull-request half.
    """
    outputs = _run_changed_step(
        tmp_path, files=["desktop/main.mjs"], gh_exit=1
    )
    assert outputs["desktop"] == "true"

# --- what the first version of this file did NOT pin -------------------------
# An adversarial review mutated the workflow eleven ways and nine stayed
# green, including two that make the whole mechanism inert. Each test below
# exists because a specific one of those mutations passed.


def test_the_output_is_wired_to_the_step_that_produces_it():
    """Deleting the `outputs:` block left every test green.

    With it gone, `needs.changed.outputs.desktop` is permanently empty and
    the gate silently reverts to label-only -- the exact PR #279 outcome this
    job exists to prevent.
    """
    changed = _workflow()["jobs"]["changed"]
    assert changed["outputs"]["desktop"] == "${{ steps.areas.outputs.desktop }}"


def test_the_producing_step_still_carries_that_id():
    """Typoing `id: areas` left every test green, for the same reason."""
    ids = [s.get("id") for s in _workflow()["jobs"]["changed"]["steps"]]
    assert "areas" in ids, f"no step with id 'areas'; ids are {ids}"


def test_the_script_asks_the_files_endpoint_for_both_path_fields(tmp_path):
    """Pins the endpoint and the jq, which a stubbed `gh` cannot exercise.

    `previous_filename` is not decoration: for a RENAME the API reports only
    the new path in `filename`, so a file moved OUT of `desktop/` would read
    as "no desktop change" while being exactly what the desktop job needs to
    see.
    """
    _run_changed_step(tmp_path, files=["src/no_human/config.py"])
    argv = (tmp_path / "gh_argv_seen").read_text(encoding="utf-8")
    assert "/pulls/279/files" in argv, argv
    assert "--paginate" in argv, argv
    assert ".filename" in argv, argv
    assert "previous_filename" in argv, argv


def test_a_large_diff_that_touches_desktop_is_not_silently_missed(tmp_path):
    """The SIGPIPE bug, which is the reason this job was wrong to begin with.

    `printf ... | grep -q` makes `grep` exit at the first match, `printf` die
    on SIGPIPE, and `pipefail` turn that into a non-zero pipeline -- so the
    `if` took the else branch and reported "no desktop files" for a PR that
    had them. Measured wrong from ~2000 filenames and NONDETERMINISTIC below
    that, silent either way.

    `desktop/` sorts before `docs/`, `src/`, `tests/` and `web/`, and the API
    returns paths in order, so the match is early in a big diff -- precisely
    the case that triggers it.
    """
    files = ["desktop/main.mjs"] + [
        f"src/no_human/module_with_a_realistic_length_name_{i}.py"
        for i in range(2500)
    ]
    assert _run_changed_step(tmp_path, files=files)["desktop"] == "true"


def test_the_changed_job_is_inside_the_workflow_cost_ratchet():
    """`tests/test_ci_network_step_bounds.py` freezes every job's timeout and
    only ratchets down, so a new job outside that dict can grow its bound with
    nothing red. The runner is pinned separately below -- the timeout dict does
    not cover it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_bounds", Path(__file__).resolve().parent / "test_ci_network_step_bounds.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "changed" in mod.EXPECTED_JOB_TIMEOUTS, (
        "add `changed` to EXPECTED_JOB_TIMEOUTS so its cost is ratcheted "
        "like every other job's")
    # Measured, because this docstring used to claim the ratchet covered a
    # move to a 2x-billed runner and it did not: `EXPECTED_JOB_TIMEOUTS` pins
    # timeouts only, and `runs-on: windows-latest` stayed green across all 15
    # files that read ci.yml (it would also break the job outright -- the
    # default shell there is pwsh and this script is bash).
    assert _workflow()["jobs"]["changed"]["runs-on"] == "ubuntu-latest"


def test_the_network_step_carries_its_own_bound():
    """`gh api --paginate` is up to 100 sequential requests. The repo's
    network-step test is scoped to `jobs.linux` and cannot see this one, so
    nothing else pins it."""
    steps = _workflow()["jobs"]["changed"]["steps"]
    areas = [s for s in steps if s.get("id") == "areas"][0]
    assert areas.get("timeout-minutes"), "the gh api step lost its bound"


def _jq_filter() -> str:
    """The `--jq` expression the `changed` job actually ships."""
    match = re.search(r"--jq '([^']+)'", _changed_script())
    assert match, "no --jq expression found in the changed job"
    return match.group(1)


def test_the_jq_filter_is_executed_not_merely_spelled(tmp_path):
    """Runs the REAL filter over a renamed entry.

    `test_the_script_asks_the_files_endpoint_for_both_path_fields` greps the
    stub's argv for the string `previous_filename`. That is not the same as
    the filter working: the stub replaces `gh` wholesale, so `--jq` is never
    executed anywhere in the suite. Changing `//` to `/` in the expression
    leaves real jq exiting 0 while silently dropping the renamed path, and
    every structural test stays green -- measured, which is why this exists.

    For a RENAME the files API reports the new path in `filename` and the old
    one in `previous_filename`, so a file moved OUT of `desktop/` is only
    visible through the second field.
    """
    jq = shutil.which("jq")
    if not jq:  # pragma: no cover - jq ships on the ubuntu runners
        pytest.skip("jq not installed; CI's ubuntu-latest image has it")

    payload = tmp_path / "files.json"
    payload.write_text(json.dumps([
        {"filename": "desktop/a.mjs", "status": "modified"},
        # moved OUT of desktop/: only `previous_filename` names it
        {"filename": "shared/preload.mjs", "status": "renamed",
         "previous_filename": "desktop/preload.mjs"},
        {"filename": "src/no_human/config.py", "status": "modified"},
    ]), encoding="utf-8")

    out = subprocess.run([jq, "-r", _jq_filter(), str(payload)],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    emitted = [line for line in out.stdout.splitlines() if line.strip()]

    assert "desktop/preload.mjs" in emitted, (
        "the renamed-out path is missing, so a file moved OUT of desktop/ "
        f"would read as no desktop change; filter emitted {emitted}")
    assert "desktop/a.mjs" in emitted
    # No stray `null` lines from a filter that dropped the `// empty` guard.
    assert "null" not in emitted, emitted


def test_the_step_keeps_its_shell_safety_flags():
    """`-o pipefail` is the mechanism the SIGPIPE comment block rests on, and
    `-u` is what makes the `:-` defaults meaningful. Neither was pinned: the
    script ran fine without them under the harness's old `bash -c`."""
    assert _changed_script().lstrip().startswith("set -euo pipefail")


@pytest.mark.parametrize("files, expected", [
    # M3: the `/` in `^desktop/` was unpinned -- the old negative fixture only
    # exercised the `^` anchor, so `^desktop` (no slash) stayed green and a
    # root-level `desktop.md` would have bought a spurious 2-minute job.
    (["desktopfoo/x.mjs", "desktop.md"], "false"),
    (["desktop/x.mjs"], "true"),
])
def test_the_match_requires_the_path_separator(tmp_path, files, expected):
    assert _run_changed_step(tmp_path, files=files)["desktop"] == expected
