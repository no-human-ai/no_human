"""`nh changelog-check` and `vcs.changelog_gap`: the decidable half of "did
this release ship without a CHANGELOG.md entry" — which commits in a range
changed a user-visible surface and did not themselves touch CHANGELOG.md.
Never mocked: a real temp git repo built by each fixture, never the live
checkout (measured live: 13 of 14 commits since v0.2.3 touched no
CHANGELOG.md, caught only because a session happened to walk the range by
hand — see the task this file exists for).
"""

from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.vcs.changelog_gap import is_user_visible_path

# --------------------------------------------------------------------------- #
# git plumbing — real temp repos, no mocking (idiom copied from               #
# tests/test_landed_override.py / tests/test_approve_ready_cli.py)            #
# --------------------------------------------------------------------------- #

def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _git_out(repo_path, *args):
    return subprocess.run(["git", "-C", str(repo_path), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


def _make_fixture_repo(tmp_path, name="repo"):
    """A real git repo: one root commit tagged `v0.1.0`. Callers add
    whatever commits their scenario needs after that."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "tag", "v0.1.0")
    return repo


def _commit(repo, touches, message):
    """Write/stage every (path, content) pair in *touches* and commit them
    together. Returns the new commit's full sha."""
    for path, content in touches:
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git_out(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------------------- #
# the range walk, via the CLI                                                 #
# --------------------------------------------------------------------------- #

def test_range_with_gaps_lists_them_and_exits_nonzero(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    sha_a = _commit(repo, [("src/no_human/updates.py", "a\n")],
                     "fix: rescue frozen build test-runner")
    sha_b = _commit(repo, [("src/no_human/doctor.py", "b\n"),
                            ("CHANGELOG.md", "- doctor fix\n")],
                     "fix: doctor check covers the new case")
    sha_c = _commit(repo, [("tests/test_x.py", "c\n")],
                     "test: add coverage for doctor check")

    result = CliRunner().invoke(cli, ["changelog-check", "--repo", str(repo)])

    assert result.exit_code == 1, result.output
    assert sha_a[:12] in result.output
    assert "fix: rescue frozen build test-runner" in result.output
    assert sha_b[:12] not in result.output
    assert "doctor check covers the new case" not in result.output
    assert sha_c[:12] not in result.output
    assert "add coverage for doctor check" not in result.output


def test_range_without_gaps_exits_zero_and_says_so(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    _commit(repo, [("src/no_human/doctor.py", "b\n"),
                    ("CHANGELOG.md", "- doctor fix\n")],
            "fix: doctor check covers the new case")
    _commit(repo, [("tests/test_x.py", "c\n")],
            "test: add coverage for doctor check")

    result = CliRunner().invoke(cli, ["changelog-check", "--repo", str(repo)])

    # An empty list must be provably "nothing missing", not silence that
    # could equally mean "the check found nothing to say" — the printed
    # message names the exact range it checked.
    assert result.exit_code == 0, result.output
    assert "v0.1.0" in result.output


def test_default_range_is_since_the_newest_tag(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    first_sha = _commit(repo, [("README.md", "hello\n")], "initial")
    pretag_sha = _commit(repo, [("src/no_human/config.py", "x\n")],
                          "fix: pretag change")
    _git(repo, "tag", "v0.1.0")

    default_result = CliRunner().invoke(
        cli, ["changelog-check", "--repo", str(repo)])
    assert default_result.exit_code == 0, default_result.output
    assert pretag_sha[:12] not in default_result.output

    explicit_result = CliRunner().invoke(
        cli, ["changelog-check", f"{first_sha}..HEAD", "--repo", str(repo)])
    assert explicit_result.exit_code == 1, explicit_result.output
    assert pretag_sha[:12] in explicit_result.output
    assert "fix: pretag change" in explicit_result.output


def test_no_tags_falls_back_to_whole_history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    sha = _commit(repo, [("src/no_human/config.py", "x\n")],
                  "fix: no tag change")

    result = CliRunner().invoke(cli, ["changelog-check", "--repo", str(repo)])

    assert result.exit_code == 1, result.output
    assert sha[:12] in result.output
    assert "whole history (no tags)" in result.output


def test_bad_range_exits_two(tmp_path):
    repo = _make_fixture_repo(tmp_path)

    result = CliRunner().invoke(
        cli, ["changelog-check", "nosuchref..HEAD", "--repo", str(repo)])

    assert result.exit_code == 2, result.output
    # Never claim "nothing missing" for a range that could not be resolved.
    assert "no commit" not in result.output
    assert "error" in result.output.lower()


def test_merge_commit_is_not_reported(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    feature_sha = _commit(repo, [("src/no_human/feature.py", "x\n")],
                           "feat: add feature")
    _git(repo, "checkout", "main")
    main_sha = _commit(repo, [("src/no_human/other.py", "y\n")],
                        "feat: unrelated main change")
    _git(repo, "merge", "--no-ff", "feature", "-m", "Merge branch 'feature'")
    merge_sha = _git_out(repo, "rev-parse", "HEAD")

    result = CliRunner().invoke(cli, ["changelog-check", "--repo", str(repo)])

    assert result.exit_code == 1, result.output
    # The two real (non-merge) gap commits are still reported...
    assert feature_sha[:12] in result.output
    assert main_sha[:12] in result.output
    # ...but the merge commit itself never is, even though its own diff
    # (parents combined) would otherwise also look like a gap.
    assert merge_sha[:12] not in result.output
    assert "Merge branch" not in result.output


# --------------------------------------------------------------------------- #
# surface classification — pinned in both directions                         #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", [
    "src/no_human/cli/commands.py",
    "desktop/main.mjs",
    "web/src/boardLanes.js",
    "docs/quickstart.md",
    "README.md",
    "migrations/001.sql",
])
def test_surface_classification_user_visible(path):
    assert is_user_visible_path(path) is True


@pytest.mark.parametrize("path", [
    "tests/test_vcs.py",
    "desktop/updater.test.mjs",
    "web/tests/x.spec.js",
    "eval/golden_tasks/a.yaml",
    "testdata/x.json",
    "docs/design/memory-lifecycle-triage.md",
    ".github/workflows/ci.yml",
    ".gitlab-ci.yml",
    "CHANGELOG.md",
])
def test_surface_classification_not_user_visible(path):
    assert is_user_visible_path(path) is False
