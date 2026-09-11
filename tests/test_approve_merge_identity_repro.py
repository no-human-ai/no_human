"""Repro pins for the ticket "nh approve's squash-to-main commit uses the
AGENT identity, violating the repo-owner identity rule for main history".

Ticket premise, restated: the squash commit `nh approve` lands on the
default branch should carry the TARGET REPO's own git identity, never the
agent's runtime identity, while task-branch commits/pushes keep the distinct
agent identity unchanged.

Measured against `src/no_human/vcs/approve_merge.py::land_task` (the ONE
merge-to-default-branch path, pinned by
`tests/test_merge_policy_wiring.py::test_land_task_is_referenced_only_by_cli_and_api`):
`_resolve_approve_identity` (`approve_merge.py:142-166`) already resolves the
squash-commit identity from `git.approve_identity` config, falling back to
`git config --get user.name/user.email` resolved IN THE TARGET REPO — never
from `git.agent_identity_name/_email` — and REFUSES at the `preconditions`
step rather than falling back to the agent identity when no identity can be
resolved. `tests/test_approve_merge.py` already carries pins for this
(`test_identity_defaults_to_repo_git_config`, `test_commit_uses_operator_identity`,
`test_identity_is_never_the_agent_identity_when_git_config_empty`). This
module adds NET-NEW assertions unmodified source already satisfies: full
author+committer coverage (the existing pins check author only), immunity to
an ambient `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env leak, the absence of any
added commit trailer, and an explicit mirror proving the task-branch/agent
identity is untouched by the same run that lands under the repo identity.

Reuses the `land_env` fixture and `_git`/`_commit_identity`/
`_clear_repo_local_identity`/`LandEnv` helpers from `tests/test_approve_merge.py`
verbatim (per that module's docstring, its harness doubles
`export_guard.py`/`build_public_export.py` so tests here never touch the
real ones) — `tests/test_approve_merge.py` itself is untouched.
"""

from __future__ import annotations

from tests.test_approve_merge import (  # noqa: F401 — fixtures re-exported on purpose
    LandEnv,
    _clear_repo_local_identity,
    _commit_identity,
    _git,
    land_env,
)

from no_human.vcs.approve_merge import land_task
from no_human.vcs.git import GitRepo

_AGENT_NAME = "no_human"
_AGENT_EMAIL = "no-human@acme.com"


def test_squash_commit_author_and_committer_are_the_repo_identity(land_env):
    """AC: with repo-local `merge-owner`/`owner@example.invalid` set on the
    clone, `git.agent_identity_name/_email` set to the agent identity in
    config, and NO `git.approve_identity` override, the landed squash
    commit's author AND committer are both the repo identity — never the
    agent identity — and the commit carries no added trailer."""
    _git(land_env.clone, "config", "user.name", "merge-owner")
    _git(land_env.clone, "config", "user.email", "owner@example.invalid")
    branch, head_sha = land_env.cut_branch("no-human/t-full-identity")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr

    out = _git(land_env.origin, "show", "-s",
                "--format=%an%x09%ae%x09%cn%x09%ce", result.landed_sha).stdout.strip()
    an, ae, cn, ce = out.split("\t")
    assert (an, ae) == ("merge-owner", "owner@example.invalid")
    assert (cn, ce) == ("merge-owner", "owner@example.invalid")
    assert _AGENT_NAME not in (an, cn)
    assert _AGENT_EMAIL not in (ae, ce)

    message = _git(land_env.origin, "show", "-s", "--format=%B", result.landed_sha).stdout
    assert "Co-Authored-By" not in message


def test_ambient_agent_identity_env_does_not_leak_into_the_merge_commit(land_env, monkeypatch):
    """AC: even when the process environment carries the agent identity in
    all four `GIT_AUTHOR_*`/`GIT_COMMITTER_*` vars (the shape a coder/agent
    sandbox sets — see `approve_merge.py:851-857`), the landed squash
    commit's author/committer are still the repo identity, pinning the scrub
    at `approve_merge.py:870-873`."""
    _git(land_env.clone, "config", "user.name", "merge-owner")
    _git(land_env.clone, "config", "user.email", "owner@example.invalid")
    monkeypatch.setenv("GIT_AUTHOR_NAME", _AGENT_NAME)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", _AGENT_EMAIL)
    monkeypatch.setenv("GIT_COMMITTER_NAME", _AGENT_NAME)
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", _AGENT_EMAIL)

    branch, head_sha = land_env.cut_branch("no-human/t-ambient-env")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr

    out = _git(land_env.origin, "show", "-s",
                "--format=%an%x09%ae%x09%cn%x09%ce", result.landed_sha).stdout.strip()
    an, ae, cn, ce = out.split("\t")
    assert (an, ae, cn, ce) == (
        "merge-owner", "owner@example.invalid", "merge-owner", "owner@example.invalid")


def test_task_branch_commits_keep_the_agent_identity(land_env):
    """Mirror AC: a commit made on the task BRANCH through `GitRepo(
    identity_name=..., identity_email=...)` — exactly the construction
    `core/worktree.py:210-211` uses for the agent's own commits — is authored
    by the agent identity both before AND after the branch is landed; only
    the LANDED squash on the default branch carries the repo identity."""
    _git(land_env.clone, "config", "user.name", "merge-owner")
    _git(land_env.clone, "config", "user.email", "owner@example.invalid")
    branch, head_sha = land_env.cut_branch("no-human/t-mirror")

    repo = GitRepo(
        land_env.clone, identity_name=_AGENT_NAME, identity_email=_AGENT_EMAIL,
        never_push_to=land_env.config["git"]["never_push_to"],
    )
    (land_env.clone / "src" / "agent_followup.py").write_text(
        "def followup():\n    return 4\n")
    # `cut_branch` already bumped `ship .. src/*.py` to 3 for its own new
    # file; this commit adds a second new `src/*.py` file, so the declared
    # count must move to 4 in the SAME commit or the stub export guard
    # refuses on count drift (same arithmetic the real guard enforces).
    cls_path = land_env.clone / "EXPORT_CLASSIFICATION.txt"
    cls_path.write_text(cls_path.read_text(encoding="utf-8").replace("ship 3 src/*.py", "ship 4 src/*.py"))
    commit_result = repo.commit_all("no-human: agent follow-up commit")
    repo.push(branch)

    branch_an, branch_ae = _commit_identity(land_env, commit_result.sha)
    assert (branch_an, branch_ae) == (_AGENT_NAME, _AGENT_EMAIL)

    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr

    # The branch head is still agent-authored — landing never rewrites the
    # source branch's own history.
    branch_head_after = _git(land_env.clone, "rev-parse", branch).stdout.strip()
    assert branch_head_after == commit_result.sha
    branch_an_after, branch_ae_after = _commit_identity(land_env, branch_head_after)
    assert (branch_an_after, branch_ae_after) == (_AGENT_NAME, _AGENT_EMAIL)

    # The landed squash on the default branch is the repo identity, not the
    # agent's.
    landed_an, landed_ae = _commit_identity(land_env, result.landed_sha)
    assert (landed_an, landed_ae) == ("merge-owner", "owner@example.invalid")
    assert landed_an != _AGENT_NAME
    assert landed_ae != _AGENT_EMAIL


def test_flat_merge_identity_config_keys_are_honored(land_env):
    """AC: new `git.merge_identity_name`/`git.merge_identity_email` config
    keys are honored by the squash commit as a second-tier alias — lower
    precedence than `git.approve_identity`, higher precedence than the
    repo-local `git config` resolution. The clone's repo-local identity is
    deliberately set to a DIFFERENT value (`clone-user`/`clone@example.invalid`)
    than the configured `merge_identity_*` pair, and no `approve_identity` is
    set, so a landed commit under the repo-local identity (rather than the
    configured `merge_identity_*` pair) proves these keys are being ignored.
    On unmodified source (which reads only `approve_identity` and falls
    through straight to repo-local git config) this FAILS; after wiring
    `_resolve_approve_identity` to also read these flat keys, it PASSES."""
    _git(land_env.clone, "config", "user.name", "clone-user")
    _git(land_env.clone, "config", "user.email", "clone@example.invalid")
    land_env.config["git"]["merge_identity_name"] = "configured-owner"
    land_env.config["git"]["merge_identity_email"] = "configured-owner@example.invalid"

    branch, head_sha = land_env.cut_branch("no-human/t-flat-merge-identity")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr

    out = _git(land_env.origin, "show", "-s",
                "--format=%an%x09%ae%x09%cn%x09%ce", result.landed_sha).stdout.strip()
    an, ae, cn, ce = out.split("\t")
    assert (an, ae) == ("configured-owner", "configured-owner@example.invalid")
    assert (cn, ce) == ("configured-owner", "configured-owner@example.invalid")
    assert an != "clone-user"
    assert ae != "clone@example.invalid"


def test_approve_identity_still_wins_over_flat_merge_identity_keys(land_env):
    """Precedence AC: when BOTH `git.approve_identity` and the new flat
    `merge_identity_name`/`merge_identity_email` keys are set, the nested
    `approve_identity` still wins — the flat keys are a lower-precedence
    alias, not a replacement, per PLAN.md's documented contingency
    ("approve_identity retaining precedence")."""
    _git(land_env.clone, "config", "user.name", "clone-user")
    _git(land_env.clone, "config", "user.email", "clone@example.invalid")
    land_env.config["git"]["merge_identity_name"] = "alias-owner"
    land_env.config["git"]["merge_identity_email"] = "alias-owner@example.invalid"
    land_env.config["git"]["approve_identity"] = {
        "name": "nested-owner", "email": "nested-owner@example.invalid",
    }

    branch, head_sha = land_env.cut_branch("no-human/t-precedence")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )
    assert result.ok, result.stderr

    out = _git(land_env.origin, "show", "-s",
                "--format=%an%x09%ae", result.landed_sha).stdout.strip()
    an, ae = out.split("\t")
    assert (an, ae) == ("nested-owner", "nested-owner@example.invalid")


def test_no_repo_identity_preserves_current_behavior(land_env, monkeypatch):
    """Fallback AC: with no repo-local git identity AND no `git.approve_identity`
    config, the run must NOT fall back to the agent identity — measured
    current behavior (pinned independently by
    `tests/test_approve_merge.py::test_identity_is_never_the_agent_identity_when_git_config_empty`)
    is an outright refusal at `preconditions`, leaving `origin/main` unmoved
    and the agent identity absent from stderr. This test asserts exactly
    that shape stays intact."""
    empty_global = land_env.tmp_path / "empty-global-gitconfig-repro"
    empty_global.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global))
    _clear_repo_local_identity(land_env.clone)

    tip_before = land_env.tip_sha()
    branch, head_sha = land_env.cut_branch("no-human/t-no-identity-repro")
    result = land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature", review_evidence="review PASS",
        config=land_env.config,
    )

    assert result.ok is False
    assert result.step == "preconditions"
    assert _AGENT_NAME not in result.stderr
    assert _AGENT_EMAIL not in result.stderr
    assert land_env.tip_sha() == tip_before
