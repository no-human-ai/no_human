"""The harness never executes a coder-planted git-config program, and never
hands one of its own secrets to a git subprocess.

A prompt-injected coder can write an *executing* git-config key
(`core.fsmonitor`, `filter.<x>.smudge`, `diff.external`, ...) from its worktree;
the write lands in the repo's SHARED `.git/config`; and the harness's own git
calls run OUTSIDE any OS sandbox. Two independent boundaries close this:

* `-c core.fsmonitor=false` on every `GitRepo` call, so `git status`
  (`has_changes()`) never spawns the fsmonitor program a shared-config write
  planted.
* every `GitRepo` git subprocess (and `reviewer._git_diff`) runs with an env
  scrubbed of foreign secrets, so if some other planted key still executes it
  finds no `CLAUDE_CODE_OAUTH_TOKEN`/`GITHUB_TOKEN`/cloud key to exfiltrate.

Each test carries a POSITIVE CONTROL — a raw `subprocess.run(["git", ...])`
that DOES execute the payload / DOES carry the secret — so a green result
proves the fix, not a test that could not see the payload either way.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from no_human.review import reviewer
from no_human.vcs.git import GitRepo, _git_subprocess_env


def _init_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True)
    (main / "f.txt").write_text("a\n")
    subprocess.run(["git", "add", "f.txt"], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=main, check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "feat"],
                   cwd=main, check=True)
    return main, wt


def _planted_marker_script(dir_: Path, marker: Path) -> Path:
    script = dir_ / "payload.sh"
    # Writes the secret it can see, then exits non-zero (as a real fsmonitor
    # integration would when it declines to answer).
    script.write_text(f'#!/bin/sh\necho "ran token=$GITHUB_TOKEN" > "{marker}"\nexit 1\n')
    script.chmod(0o755)
    return script


def test_git_subprocess_env_drops_foreign_secrets_and_keeps_git_needs(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-oauth")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-aws")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ambient Author")

    status_env = _git_subprocess_env("status")
    assert "GITHUB_TOKEN" not in status_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in status_env
    assert "AWS_SECRET_ACCESS_KEY" not in status_env
    # git still gets what it needs to run.
    assert "PATH" in status_env and "HOME" in status_env
    # a non-commit call leaves the identity env alone (it affects only commits).
    assert status_env.get("GIT_AUTHOR_NAME") == "Ambient Author"

    # commit-writing calls additionally drop the identity env so
    # `-c user.name=` stays authoritative.
    commit_env = _git_subprocess_env("commit")
    assert "GIT_AUTHOR_NAME" not in commit_env
    assert "GITHUB_TOKEN" not in commit_env


def test_has_changes_does_not_execute_a_planted_fsmonitor(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")
    _main, wt = _init_repo_with_worktree(tmp_path)
    marker = tmp_path / "marker.txt"
    script = _planted_marker_script(tmp_path, marker)
    # The attacker's write, from the worktree — it lands in the SHARED config.
    subprocess.run(["git", "config", "core.fsmonitor", str(script)], cwd=wt, check=True)

    # The harness's own status scan must not run it.
    GitRepo(wt).has_changes()
    assert not marker.exists(), (
        "GitRepo.has_changes() executed a coder-planted core.fsmonitor program")

    # POSITIVE CONTROL: a raw `git status` (no `-c core.fsmonitor=false`) DOES
    # run it, so the assertion above is meaningful and not vacuous.
    subprocess.run(["git", "status", "--porcelain"], cwd=wt, check=False)
    assert marker.exists() and "fake-gh-token" in marker.read_text(encoding="utf-8"), (
        "control failed: the planted fsmonitor did not run even for a raw "
        "git status, so this test cannot distinguish the fix from a no-op")


def test_reviewer_git_diff_does_not_execute_diff_external(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")
    main = tmp_path / "repo"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True)
    (main / "f.txt").write_text("one\n")
    subprocess.run(["git", "add", "f.txt"], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=main, check=True)
    (main / "f.txt").write_text("two\n")
    subprocess.run(["git", "add", "f.txt"], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "b"], cwd=main, check=True)

    marker = tmp_path / "diff-marker.txt"
    script = _planted_marker_script(tmp_path, marker)
    # `diff.external` names a program git runs INSTEAD of its own diff.
    subprocess.run(["git", "config", "diff.external", str(script)], cwd=main, check=True)

    reviewer._git_diff(main, "HEAD~1", "HEAD")
    assert not marker.exists(), (
        "reviewer._git_diff executed a planted diff.external program")

    # POSITIVE CONTROL: git diff WITHOUT --no-ext-diff runs it.
    subprocess.run(["git", "diff", "HEAD~1..HEAD"], cwd=main, check=False)
    assert marker.exists(), (
        "control failed: diff.external did not run even for a raw git diff")


def _two_commit_repo(tmp_path: Path, filename: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / filename).write_text("one\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=repo, check=True)
    (repo / filename).write_text("two\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "b"], cwd=repo, check=True)
    return repo


def test_review_routing_diff_text_does_not_execute_diff_external(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")
    from no_human.core import review_routing
    repo = _two_commit_repo(tmp_path, "prod.py")
    marker = tmp_path / "rr-marker.txt"
    script = _planted_marker_script(tmp_path, marker)
    subprocess.run(["git", "config", "diff.external", str(script)], cwd=repo, check=True)

    review_routing._diff_text(repo, "HEAD~1", "HEAD")
    assert not marker.exists(), (
        "review_routing._diff_text executed a planted diff.external program")

    # POSITIVE CONTROL: a raw full-patch git diff DOES run it.
    subprocess.run(["git", "diff", "-M", "HEAD~1..HEAD"], cwd=repo, check=False)
    assert marker.exists(), "control failed: diff.external did not run for a raw git diff"


def test_runner_test_file_diff_does_not_execute_diff_external(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")
    from no_human.testing import runner
    # test_file_diff filters to TEST files, so the changed file must be one.
    repo = _two_commit_repo(tmp_path, "test_thing.py")
    marker = tmp_path / "runner-marker.txt"
    script = _planted_marker_script(tmp_path, marker)
    subprocess.run(["git", "config", "diff.external", str(script)], cwd=repo, check=True)

    out = runner.test_file_diff(repo, "HEAD~1", "HEAD")
    assert not marker.exists(), (
        "runner.test_file_diff executed a planted diff.external program")
    assert "two" in out or out == "", "sanity: the diff text is still produced"

    # POSITIVE CONTROL.
    subprocess.run(["git", "diff", "--no-color", "HEAD~1..HEAD", "--", "test_thing.py"],
                   cwd=repo, check=False)
    assert marker.exists(), "control failed: diff.external did not run for a raw git diff"


def _smudge_repo(tmp_path: Path) -> tuple[Path, str, Path]:
    """A repo whose committed tree binds a path to an attacker-named smudge
    filter, with that filter planted in the shared config. Returns
    (repo, sha, marker_path) — the marker is written iff the smudge runs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    # The coder controls .gitattributes in their own commit.
    (repo / ".gitattributes").write_text("secret.txt filter=evil\n")
    (repo / "secret.txt").write_text("payload\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    marker = tmp_path / "smudge-marker.txt"
    script = tmp_path / "smudge.sh"
    script.write_text(
        f'#!/bin/sh\necho "gh=$GITHUB_TOKEN oauth=$CLAUDE_CODE_OAUTH_TOKEN" > "{marker}"\ncat\n')
    script.chmod(0o755)
    subprocess.run(["git", "config", "filter.evil.smudge", str(script)], cwd=repo, check=True)
    return repo, sha, marker


def test_type_evidence_worktree_checkout_smudge_gets_no_secret(tmp_path, monkeypatch):
    """Drives the REAL type_evidence._run_at_commit (not an inline copy), whose
    `git worktree add` checks out the coder's tree and runs its smudge filter.
    No `-c` flag suppresses an arbitrary filter, so the scrubbed env is the
    boundary; the filter still runs but reads no launcher secret. Reverting the
    site's env= flips this test, because it calls the site."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-oauth")
    from no_human.review import type_evidence
    repo, sha, marker = _smudge_repo(tmp_path)
    # A no-op checker: the checkout (and the smudge) happens before it runs.
    noop = type_evidence._Checker("noop", ("true",), (0,))
    type_evidence._run_at_commit(noop, repo, sha, timeout=30)

    assert marker.exists(), "sanity: the smudge filter did run on checkout"
    got = marker.read_text(encoding="utf-8")
    assert "fake-gh-token" not in got and "fake-oauth" not in got, (
        f"_run_at_commit's worktree checkout leaked a launcher secret to a "
        f"planted smudge filter: {got!r}")

    # POSITIVE CONTROL: a raw worktree add (unscrubbed env) DOES leak the secret.
    marker.unlink()
    subprocess.run(["git", "worktree", "add", "--detach", str(tmp_path / "raw"), sha],
                   cwd=repo, capture_output=True, text=True)
    assert marker.exists() and "fake-gh-token" in marker.read_text(encoding="utf-8"), (
        "control failed: the raw worktree add did not run the smudge with the "
        "secret, so this test cannot distinguish the scrub from a no-op")


def test_reviewer_worktree_status_scan_does_not_execute_fsmonitor(tmp_path, monkeypatch):
    """reviewer_worktree.snapshot() runs `git status` on the coder's attempt
    tree inside the reviewer process, bracketing every review. A planted
    core.fsmonitor must not execute there, and no secret must be reachable."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")
    from no_human.core import reviewer_worktree
    _main, wt = _init_repo_with_worktree(tmp_path)
    marker = tmp_path / "rwt-marker.txt"
    script = _planted_marker_script(tmp_path, marker)
    subprocess.run(["git", "config", "core.fsmonitor", str(script)], cwd=wt, check=True)

    reviewer_worktree.snapshot(wt, timeout=30)
    assert not marker.exists(), (
        "reviewer_worktree.snapshot() executed a planted core.fsmonitor program")

    # POSITIVE CONTROL: a raw git status in the same worktree runs it.
    subprocess.run(["git", "status", "--porcelain"], cwd=wt, check=False)
    assert marker.exists() and "fake-gh-token" in marker.read_text(encoding="utf-8"), (
        "control failed: the planted fsmonitor did not run for a raw git status")
