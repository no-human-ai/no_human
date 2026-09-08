"""A built bundle launched by CI must not tag itself `environment=real`.

Issue #120, measured in PostHog on 2026-09-07: 48 distinct instance ids from
US GitHub-Actions IPs each sent exactly one `app_started` with
`environment=real` in 36 hours. They inflate the real-install count 1:1, so
`environment=real` stopped meaning "an install".

`telemetry.environment()` recognises a CI runner by the platform's own marker
(`_CI_MARKERS`: GITHUB_ACTIONS, GITLAB_CI, ...), which works for anything
running directly in the job's shell. It cannot work for a launch that does not
carry those markers through to the process that emits the event, and a frozen
bundle started through a desktop launcher is exactly that shape.

The fix declares the environment instead of inferring it. `NH_ENV` is rule 1
in `environment()`, above the marker probe and above `PYTEST_CURRENT_TEST`, so
a step that sets `NH_ENV=ci` is correct no matter what strips the rest of the
environment on the way to the bundle.

The tests below pin both halves: the precedence in the function, and the fact
that every ci.yml step which launches a built artefact actually sets it. The
second half is the one that rots, because the fix lives in a workflow file
that no Python test would otherwise read.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from no_human import telemetry

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"

#: Fragments that mean "this step starts a BUILT artefact", as opposed to
#: running the test suite or building something. Each is a launch that
#: `environment()` cannot classify from the runner's own markers.
_BUNDLE_LAUNCHES = (
    "linux-acceptance.mjs",              # the .deb and AppImage acceptance runs
    "/opt/no_human/resources/nh-server/nh",  # the installed frozen server
    "$UV_TOOL_BIN_DIR/nh",               # the wheel, installed as a user would
    # The container the images job builds and runs on EVERY ci.yml run. Its
    # own lifespan emits `app_started`, and `docker run` carries none of the
    # runner's environment, so this one launch accounted for most of the 48
    # events a day. The probe builds its own `docker run` line, so the step's
    # `env:` only reaches the container because the probe forwards NH_ENV.
    "mcp_handshake_probe.py",
)


def _steps():
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_name, step


# ------------------------------ the function ------------------------------ #

def test_nh_env_ci_outranks_everything_a_bundle_might_lack(monkeypatch):
    """The whole point of using NH_ENV rather than adding another marker: it
    is rule 1, so it survives an environment stripped of CI markers."""
    for marker in telemetry._CI_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("NH_ENV", "ci")
    assert telemetry.environment() == "ci"


def test_without_a_marker_a_bundle_falls_through_to_real(monkeypatch):
    """The defect itself. A frozen bundle is not a source checkout, so the
    `dev` branch cannot catch it either and it lands on `real`."""
    for marker in telemetry._CI_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("NH_ENV", raising=False)
    monkeypatch.setattr(telemetry, "_is_source_checkout", lambda: False)
    assert telemetry.environment() == "real"


def test_ci_is_a_valid_environment_with_a_stable_sentinel_id():
    """A CI run must collapse onto the shared `ci` id rather than minting a
    fresh uuid4 per run, which is what made 48 runs read as 48 installs."""
    assert "ci" in telemetry._VALID_ENVIRONMENTS
    assert telemetry._ENV_SENTINEL_IDS["ci"]


# ------------------------------- the workflow ------------------------------ #

def test_every_bundle_launching_step_declares_nh_env_ci():
    """The half that rots. `environment()` can be perfect and the events
    still wrong, because the classification depends on a workflow file no
    other Python test reads."""
    launches = [
        (job, step) for job, step in _steps()
        if any(frag in (step.get("run") or "") for frag in _BUNDLE_LAUNCHES)
    ]
    assert launches, (
        "found no bundle-launching steps in ci.yml, so this guard is inert; "
        "the _BUNDLE_LAUNCHES fragments have drifted from the workflow"
    )
    untagged = [
        "%s: %s" % (job, step.get("name") or (step.get("run") or "")[:60])
        for job, step in launches
        if (step.get("env") or {}).get("NH_ENV") != "ci"
    ]
    assert not untagged, (
        "these ci.yml steps launch a built artefact without NH_ENV=ci, so it "
        "will tag itself environment=real and inflate the real-install count "
        "(issue #120):\n  " + "\n  ".join(untagged)
    )


def test_the_mcp_probe_forwards_nh_env_into_the_container():
    """The step's `env:` reaches the container ONLY because the probe puts
    `-e NH_ENV` on the `docker run` line it builds itself. Delete that and the
    workflow test above still passes while the container goes back to tagging
    itself `real`, which is the exact defect. So pin the argv.

    `-e NH_ENV` with no `=value` forwards the host's value when set and passes
    nothing when unset, so this cannot invent an `NH_ENV` outside CI."""
    probe = ROOT / "packaging" / "mcp_handshake_probe.py"
    tree = ast.parse(probe.read_text(encoding="utf-8"))

    runs = [
        [e.value for e in node.elts if isinstance(e, ast.Constant)]
        for node in ast.walk(tree)
        if isinstance(node, ast.List)
        and node.elts
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "docker"
    ]
    assert runs, "no `docker run` argv literal found in mcp_handshake_probe.py"
    for argv in runs:
        assert "-e" in argv and "NH_ENV" in argv, (
            "mcp_handshake_probe.py builds a docker command that does not "
            "forward NH_ENV, so the container tags app_started as "
            "environment=real on every CI run (issue #120): %r" % argv
        )
        assert argv[argv.index("-e") + 1] == "NH_ENV", (
            "`-e` must be followed by a bare NH_ENV (no '=value'), so the "
            "host's value is forwarded only when it is actually set: %r" % argv
        )


@pytest.mark.parametrize("fragment", _BUNDLE_LAUNCHES)
def test_each_known_launch_shape_is_still_present_in_the_workflow(fragment):
    """Guards the guard above from passing vacuously: if a launch is renamed
    or removed, this fails and says so instead of the test silently checking
    nothing."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert fragment in text, (
        "ci.yml no longer contains %r, so the NH_ENV guard above no longer "
        "covers that launch; update _BUNDLE_LAUNCHES" % fragment
    )
