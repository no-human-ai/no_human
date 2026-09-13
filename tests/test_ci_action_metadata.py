"""Cross-checks between `action.yml`, `action/Dockerfile`, the README's
GitHub Action snippet, and `no_human.ci_action.run`'s own constants.

These are the "two places say the same thing" guards: a default changed in one
without the other is a silent behavior drift a user would only discover by
reading source, which defeats the point of `action.yml` documenting the
contract at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from no_human.ci_action import run

pytestmark = pytest.mark.repoguard

REPO = Path(__file__).resolve().parents[1]
ACTION_YML = REPO / "action.yml"
DOCKERFILE = REPO / "action" / "Dockerfile"
README = REPO / "README.md"


@pytest.fixture(scope="module")
def action_yml() -> dict:
    return yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))


def test_action_yml_exists_and_parses(action_yml):
    assert action_yml["name"]
    assert "inputs" in action_yml and "outputs" in action_yml


def test_runs_using_docker_image_matches_dockerfile_path(action_yml):
    runs = action_yml["runs"]
    assert runs["using"] == "docker"
    assert runs["image"] == "action/Dockerfile"
    assert DOCKERFILE.exists()


@pytest.mark.parametrize(
    "input_name, expected_default",
    [
        ("credential_mode", run.DEFAULT_CREDENTIAL_MODE),
        ("model", run.DEFAULT_MODEL),
        ("max_files", str(run.DEFAULT_MAX_FILES)),
        ("fail_on_findings", run.DEFAULT_FAIL_ON_FINDINGS),
        ("dry_run", run.DEFAULT_DRY_RUN),
    ],
)
def test_action_yml_defaults_match_run_py_constants(action_yml, input_name, expected_default):
    default = action_yml["inputs"][input_name]["default"]
    assert str(default) == expected_default, (
        f"action.yml's `{input_name}` default ({default!r}) does not match "
        f"run.py's constant ({expected_default!r}) — they must be kept in sync"
    )


def test_credential_input_is_required_with_no_default(action_yml):
    credential = action_yml["inputs"]["credential"]
    assert credential.get("required") is True
    assert "default" not in credential


def test_github_token_defaults_to_the_job_token(action_yml):
    assert action_yml["inputs"]["github_token"]["default"] == "${{ github.token }}"


def test_outputs_declare_verdict_comment_url_and_skipped(action_yml):
    outputs = action_yml["outputs"]
    assert set(outputs) >= {"verdict", "comment_url", "skipped"}


def test_dockerfile_installs_the_claude_cli():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "@anthropic-ai/claude-code" in text


def test_dockerfile_never_bakes_in_a_credential_literal():
    text = DOCKERFILE.read_text(encoding="utf-8")
    # A literal `VAR=value` assignment would bake a credential into a layer;
    # the credential must only ever arrive at `docker run` time via the
    # `credential` input (see action.yml / ci_action/run.py).
    assert not re.search(r"ANTHROPIC_API_KEY\s*=\s*\S", text)
    assert not re.search(r"CLAUDE_CODE_OAUTH_TOKEN\s*=\s*\S", text)


def test_dockerfile_entrypoint_is_the_one_shot_module():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "no_human.ci_action.run" in text
    assert re.search(r"ENTRYPOINT\s*\[.*no_human\.ci_action\.run.*\]", text)


def _readme_workflow_snippets() -> list[str]:
    text = README.read_text(encoding="utf-8")
    return re.findall(r"```yaml\n(.*?)```", text, re.S)


def test_readme_has_a_github_action_section():
    text = README.read_text(encoding="utf-8")
    assert "## GitHub Action" in text


def test_readme_workflow_snippet_parses_and_uses_this_action():
    snippets = [s for s in _readme_workflow_snippets() if "no_human review gate" in s or "no-human-ai/no_human" in s]
    assert snippets, "README's GitHub Action section must include a ```yaml workflow snippet"
    parsed = yaml.safe_load(snippets[0])
    assert "pull_request" in parsed.get(True, parsed.get("on", {}))
    jobs = parsed["jobs"]
    steps = next(iter(jobs.values()))["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert any(u.startswith("no-human-ai/no_human") for u in uses)
    credential_step = next(s for s in steps if s.get("uses", "").startswith("no-human-ai/no_human"))
    assert "credential" in credential_step.get("with", {})
