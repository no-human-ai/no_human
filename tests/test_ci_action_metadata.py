"""Cross-checks between `action.yml`, `Dockerfile.action`, the README's
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
from no_human.config import DEFAULT_CONFIG

pytestmark = pytest.mark.repoguard

#: The reviewer model this project actually measures its catch-rate on
#: (config.py's own default), read directly rather than through
#: `run.DEFAULT_MODEL` — so a hard-coded literal in run.py that happens to
#: match action.yml but has drifted from config.py still fails here.
REPO_REVIEW_MODEL = DEFAULT_CONFIG["llm"]["review_model"]


def test_run_default_model_matches_the_repos_own_reviewer_default():
    assert run.DEFAULT_MODEL == REPO_REVIEW_MODEL, (
        "ci_action.run.DEFAULT_MODEL must derive from (not merely equal) "
        "config.DEFAULT_CONFIG['llm']['review_model'], so the Action never "
        "ships a model the project has not measured as its reviewer"
    )

REPO = Path(__file__).resolve().parents[1]
ACTION_YML = REPO / "action.yml"
DOCKERFILE = REPO / "Dockerfile.action"
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
    assert runs["image"] == "Dockerfile.action"
    assert DOCKERFILE.exists()


def test_dockerfile_action_lives_at_repo_root_not_a_subdirectory():
    # Verified against a real GitHub Actions run: for a `using: docker`
    # action, the build context docker uses is the DIRECTORY CONTAINING the
    # `runs.image` Dockerfile, not the repository root. `action/Dockerfile`
    # (in a subdirectory containing only the Dockerfile itself) built with an
    # effectively empty context ("transferring context: 2B done" in the real
    # run log) and every `COPY src/ ./src/` / `COPY pyproject.toml ...`
    # instruction failed with "not found" — 100% reproducible, every run.
    # Moving the Dockerfile to the repo root (this file) makes the build
    # context the repo root, where those COPY sources actually live.
    assert DOCKERFILE.parent == REPO


def test_action_yml_has_no_double_brace_expression_anywhere():
    # Verified against a real GitHub Actions run: the runner expands
    # `${{ ... }}` wherever it appears in action.yml — including inside a
    # plain description string, not just in a `default:` field — before a
    # docker-type action's container starts, and the `github` context is not
    # available at that phase ("Unrecognized named-value: 'github'"). A
    # `${{ github.token }}` default (or even a mention of it in prose) fails
    # every single run. So the file must never contain a literal `${{`.
    text = ACTION_YML.read_text(encoding="utf-8")
    assert "${{" not in text


@pytest.mark.parametrize(
    "input_name, expected_default",
    [
        ("credential_mode", run.DEFAULT_CREDENTIAL_MODE),
        ("model", REPO_REVIEW_MODEL),
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


def test_github_token_has_no_default(action_yml):
    # A verified-live GitHub Actions run proved `default: ${{ github.token }}`
    # is rejected for a `using: docker` action's inputs before the container
    # ever starts: "Unrecognized named-value: 'github'" — the `github`
    # context is not available while input defaults are resolved. So this
    # input must have no default; the workflow passes it explicitly instead.
    github_token = action_yml["inputs"]["github_token"]
    assert github_token.get("required") is False
    assert "default" not in github_token


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
    entrypoint_match = re.search(r"ENTRYPOINT\s*\[([^\]]*)\]", text)
    assert entrypoint_match, "Dockerfile.action must declare an ENTRYPOINT"
    if "no_human.ci_action.run" in entrypoint_match.group(0):
        return  # entrypoint invokes the module directly
    # Otherwise the entrypoint must point at a script this same Dockerfile
    # writes, and that script's own content must invoke the module via
    # `-m no_human.ci_action.run` (never a bare `uv run ...` with no `-m`,
    # which would silently no-op the review gate while still exiting 0).
    assert re.search(r"-m\s+no_human\.ci_action\.run", text)


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


def test_readme_workflow_snippet_declares_contents_read_permission():
    # `actions/checkout` needs `contents: read` to read the repository at
    # all; a snippet a user copies verbatim that omits it relies on a
    # workflow-level default permission the org/repo may have locked down,
    # which is exactly the kind of silent failure this snippet must not ship.
    snippets = [s for s in _readme_workflow_snippets() if "no-human-ai/no_human" in s]
    parsed = yaml.safe_load(snippets[0])
    assert parsed.get("permissions", {}).get("contents") == "read"


def test_readme_workflow_snippet_checkout_pins_pr_head_sha():
    # `actions/checkout` defaults to the ephemeral MERGE commit on
    # `pull_request` events, not the PR's actual head — see
    # `run.py::main`'s HEAD-vs-head_sha check, which refuses to run rather
    # than review the wrong tree. The README's own example must not teach a
    # user to hit that refusal on their very first run.
    snippets = [s for s in _readme_workflow_snippets() if "no-human-ai/no_human" in s]
    parsed = yaml.safe_load(snippets[0])
    steps = next(iter(parsed["jobs"].values()))["steps"]
    checkout_step = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
    assert checkout_step.get("with", {}).get("ref") == "${{ github.event.pull_request.head.sha }}"


def test_readme_workflow_snippet_passes_github_token_explicitly(action_yml):
    # github_token has no action.yml default (see
    # test_github_token_has_no_default), so the README's own example must
    # pass it explicitly or a user copying it verbatim gets an empty-token
    # failure on their very first run.
    assert "default" not in action_yml["inputs"]["github_token"]
    snippets = [s for s in _readme_workflow_snippets() if "no-human-ai/no_human" in s]
    parsed = yaml.safe_load(snippets[0])
    steps = next(iter(parsed["jobs"].values()))["steps"]
    credential_step = next(s for s in steps if s.get("uses", "").startswith("no-human-ai/no_human"))
    assert credential_step.get("with", {}).get("github_token") == "${{ github.token }}"
