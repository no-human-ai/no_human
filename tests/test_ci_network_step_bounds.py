"""The `linux` job's network-touching steps must fail fast, not burn the
job's whole 45-minute timeout.

Born from the `apt lists + Xvfb` step consuming 42 of 45 minutes against a
stalled mirror, twice in a row -- and GitHub reporting the timed-out job as
`cancelled`, not `failure`, so the run did not read as broken at a glance.
Every step here gets a per-step `timeout-minutes` bound, and the two
genuinely idempotent commands (`apt-get update`, `npm install -g`) get a
bounded 2-attempt retry -- without masking a real package/install failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"

# The six network-touching steps this ticket names, and the exact bound the
# PR body/plan commit to for each.
EXPECTED_TIMEOUTS = {
    "Install uv": 3,
    # 15, not 5: a cache-cold `uv sync --frozen` measured 7.32s locally on
    # 2026-09-04 (`uv lock --check` resolved in 3ms -- not lock drift). The
    # prior 5-minute bound was chronically hit by a GitHub-runner-only
    # registry/network stall this sandbox cannot reproduce; 15 is the
    # ticket's own suggested ceiling and gives real headroom while the
    # step's internal per-attempt bound still fires before it (see
    # tests/test_ci_uv_sync_install_step.py).
    "Install dependencies": 15,
    "apt lists + Xvfb": 3,
    "Install the .deb the way a user would": 5,
    "Install the Claude Code CLI the way a user would": 5,
    "Uninstall keeps the user's data": 5,
}

EXPECTED_JOB_TIMEOUTS = {
    "cla": 5,
    "inventory": 5,
    "python": 30,
    "web": 15,
    "wheel": 20,
    "desktop": 20,
    "windows": 45,
    "linux": 45,
}

# The one step allowed to use `|| true` -- an evidence-gathering step gated
# on `if: always()`, not a gate that can silently pass.
ALLOWED_OR_TRUE_STEPS = {"Gather the evidence under one root"}


def _load_workflow():
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _linux_job(workflow):
    jobs = workflow["jobs"]
    assert "linux" in jobs, "the linux job was not found in the workflow"
    return jobs["linux"]


def _steps_by_name(job):
    return {step["name"]: step for step in job["steps"] if "name" in step}


def test_the_six_steps_exist():
    workflow = _load_workflow()
    job = _linux_job(workflow)
    steps = _steps_by_name(job)
    for name in EXPECTED_TIMEOUTS:
        assert name in steps, f"step {name!r} not found in the linux job"


def test_workflow_parses():
    workflow = _load_workflow()
    assert isinstance(workflow, dict)
    assert "jobs" in workflow


def test_apt_step_is_bounded_at_three_minutes():
    workflow = _load_workflow()
    steps = _steps_by_name(_linux_job(workflow))
    assert steps["apt lists + Xvfb"]["timeout-minutes"] == 3


def test_apt_update_is_retried_but_install_is_not():
    workflow = _load_workflow()
    steps = _steps_by_name(_linux_job(workflow))
    body = steps["apt lists + Xvfb"]["run"]

    done_idx = body.index("done")
    update_idx = body.index("apt-get update")
    install_matches = [m.start() for m in re.finditer(r"apt-get install -y xvfb", body)]

    assert update_idx < done_idx, "apt-get update must be inside the retry loop"
    assert len(install_matches) == 1, "apt-get install -y xvfb must appear exactly once"
    assert install_matches[0] > done_idx, "apt-get install must run strictly after the retry loop"
    assert "sleep 15" in body
    assert "exit 1" in body


def test_all_six_network_steps_are_bounded():
    workflow = _load_workflow()
    steps = _steps_by_name(_linux_job(workflow))
    for name, expected in EXPECTED_TIMEOUTS.items():
        value = steps[name]["timeout-minutes"]
        assert isinstance(value, int), f"{name}: timeout-minutes must be an int, got {value!r}"
        assert 0 < value < 45, f"{name}: timeout-minutes={value} out of (0, 45)"
        assert value == expected, f"{name}: timeout-minutes={value}, expected {expected}"


def test_cli_install_retries_only_the_npm_half():
    workflow = _load_workflow()
    steps = _steps_by_name(_linux_job(workflow))
    body = steps["Install the Claude Code CLI the way a user would"]["run"]

    done_idx = body.index("done")
    npm_idx = body.index("npm install -g @anthropic-ai/claude-code")
    version_matches = [m.start() for m in re.finditer(r"claude --version", body)]

    assert npm_idx < done_idx, "npm install -g must be inside the retry loop"
    assert len(version_matches) == 1, "claude --version must appear exactly once"
    assert version_matches[0] > done_idx, "claude --version must run strictly after the retry loop"


def test_non_retried_steps_have_no_retry_loop():
    workflow = _load_workflow()
    steps = _steps_by_name(_linux_job(workflow))
    for name in ("Install the .deb the way a user would", "Uninstall keeps the user's data"):
        body = steps[name]["run"]
        assert "until " not in body, f"{name}: unexpected retry loop (until)"
        assert re.search(r"\bfor\b", body) is None, f"{name}: unexpected retry loop (for)"


def test_job_timeouts_are_not_increased():
    """No EXISTING job's budget grows, and every job carries one.

    Deliberately NOT an equality assertion over the whole map. It was, and a
    ninth job (`images`, added by another lane between this branch's base and
    main) turned the suite red on the merge result while increasing nothing —
    the failure said "not increased" and meant "the set of jobs changed". A
    pin that fires on an unrelated ADDITION does not measure what its name
    claims, and it would have failed again on the next job anyone added.

    A NEW job is therefore allowed, but it must still declare a bound: an
    unbounded job is the very hazard this file exists to prevent (the Linux
    job burned 45 minutes twice with no per-step timeout).
    """
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    actual = {name: job.get("timeout-minutes") for name, job in jobs.items()}

    unbounded = sorted(n for n, t in actual.items() if t is None)
    assert not unbounded, f"job(s) with no timeout-minutes: {unbounded}"

    increased = {
        name: (EXPECTED_JOB_TIMEOUTS[name], actual[name])
        for name in EXPECTED_JOB_TIMEOUTS
        if name in actual and actual[name] > EXPECTED_JOB_TIMEOUTS[name]
    }
    assert not increased, f"job timeout(s) increased (was, now): {increased}"

    missing = sorted(set(EXPECTED_JOB_TIMEOUTS) - set(actual))
    assert not missing, f"job(s) disappeared from the workflow: {missing}"


def test_no_step_can_silently_pass():
    workflow = _load_workflow()
    job = _linux_job(workflow)

    or_true_steps = set()
    for step in job["steps"]:
        name = step.get("name", "<unnamed>")
        run = step.get("run")
        if run and "|| true" in run:
            or_true_steps.add(name)
        assert "continue-on-error" not in step, f"{name}: continue-on-error is not allowed"

    assert or_true_steps == ALLOWED_OR_TRUE_STEPS
