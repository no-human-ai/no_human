"""The `linux` job's `Install dependencies` step (`uv sync --frozen`) must
survive a genuinely cold, slow-registry sync without either burning the
whole job on a bare 5-minute timeout OR retrying silently into an opaque
`cancelled` job.

Born from CI failing on nearly every main commit: `uv sync --frozen` timed
out at the step's old 5-minute bound before the 2-attempt retry could even
finish once. A local cache-cold `uv sync --frozen` measured 7.32s and
`uv lock --check` resolved in 3ms on 2026-09-04, ruling out lock drift --
the fix is a realistic step bound plus a retry loop whose own internal
per-attempt timeout fires (and prints the real uv error) before GitHub's
step timeout ever does.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _load_workflow():
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _linux_job(workflow):
    jobs = workflow["jobs"]
    assert "linux" in jobs, "the linux job was not found in the workflow"
    return jobs["linux"]


def _steps_by_name(job):
    return {step["name"]: step for step in job["steps"] if "name" in step}


def _install_deps_body():
    workflow = _load_workflow()
    steps = _steps_by_name(_linux_job(workflow))
    return steps["Install dependencies"]["run"]


def test_install_dependencies_bound_is_realistic_for_a_cold_sync():
    workflow = _load_workflow()
    steps = _steps_by_name(_linux_job(workflow))
    value = steps["Install dependencies"]["timeout-minutes"]

    assert value > 5, "must exceed the old bound that timed out on nearly every main commit"
    assert 10 <= value <= 15, f"expected the ticket's suggested 10-15 minute range, got {value}"
    assert value < 45, "must stay well inside the job's own 45-minute budget"


def test_uv_cache_is_keyed_by_uv_lock():
    workflow = _load_workflow()
    job = _linux_job(workflow)
    steps = _steps_by_name(job)

    install_uv = steps["Install uv"]
    assert install_uv["uses"] == "astral-sh/setup-uv@v6"
    assert install_uv["with"]["enable-cache"] is True
    assert install_uv["with"]["cache-dependency-glob"] == "uv.lock"

    for step in job["steps"]:
        if step.get("name") == "Install uv":
            continue
        assert step.get("uses") != "actions/cache", (
            "a second cache step for the uv cache dir would race setup-uv's own "
            "restore/save and make the restore order nondeterministic"
        )


def test_per_attempt_bound_fires_before_the_step_timeout():
    workflow = _load_workflow()
    steps = _steps_by_name(_linux_job(workflow))
    step_timeout = steps["Install dependencies"]["timeout-minutes"]
    body = _install_deps_body()

    per_attempt_match = re.search(r"per_attempt=(\d+)m\b", body)
    max_match = re.search(r"\bmax=(\d+)\b", body)
    assert per_attempt_match, "could not find a per_attempt=<N>m assignment in the run body"
    assert max_match, "could not find a max=<N> assignment in the run body"

    per_attempt = int(per_attempt_match.group(1))
    max_attempts = int(max_match.group(1))

    backoff_seconds = sum(15 * n for n in range(1, max_attempts))
    total_minutes = per_attempt * max_attempts + backoff_seconds / 60

    assert total_minutes < step_timeout, (
        f"per_attempt({per_attempt}) * max({max_attempts}) + backoff({backoff_seconds}s) "
        f"= {total_minutes}min must stay under the step timeout ({step_timeout}min), so a "
        "stall always ends as an internal timeout, never a bare GitHub step timeout"
    )


def test_final_failure_surfaces_the_real_uv_error():
    body = _install_deps_body()

    assert "tee" in body, "must capture each attempt's log"
    assert "tail" in body, "must print the tail of the failing attempt's log"
    assert "124" in body, "must discriminate the internal-timeout exit code from a real uv error"
    assert "uv lock --check" in body, "must probe for lock drift on final failure"
    assert 'exit "$rc"' in body, "final failure must exit non-zero with the real uv/timeout code"

    assert "|| true" not in body, "must not mask the failure"
    assert "continue-on-error" not in body, "must not mask the failure"


def test_retry_is_still_two_attempts_with_backoff():
    body = _install_deps_body()

    assert re.search(r"\bmax=2\b", body), "must keep exactly the existing 2-attempt retry"
    sleep_match = re.search(r"sleep\s+\$\(\(\s*\d+\s*\*\s*attempt\s*\)\)", body)
    assert sleep_match, "sleep duration must be a function of attempt (backoff), not a constant"


def test_no_other_job_or_artifact_step_changed():
    workflow = _load_workflow()
    jobs = workflow["jobs"]

    for job_name in ("python", "windows"):
        steps = _steps_by_name(jobs[job_name])
        install = steps["Install dependencies"]
        assert install["run"] == "uv sync --frozen"
        assert "timeout-minutes" not in install

    linux_steps = _steps_by_name(_linux_job(workflow))
    assert linux_steps["Freeze the server (packaging/build-installer.sh, gates armed)"]["run"] == (
        "bash packaging/build-installer.sh"
    )
    assert linux_steps["Package the .deb and the AppImage (x64)"]["run"] == "npm run dist:linux"
    assert linux_steps["Checksums"]["run"] == (
        "sha256sum no_human-*-linux-amd64.deb no_human-*-linux-x86_64.AppImage | tee SHA256SUMS-linux.txt"
    )
