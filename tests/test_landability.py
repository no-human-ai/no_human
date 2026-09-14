"""`vcs/landability.check_landability` — the live, never-cached mergeability
probe this repo was missing: `nh approve --ready` used to answer "is this
head quality-ready" (`core/merge_policy.py`'s six rules, stamped per head
sha) without ever asking "does this branch still merge into its CURRENT
base". Every landing rewrites the generated `RELEASE_MANIFEST.txt`, so every
landing conflicts every other open PR's branch against that file — the
six-rule verdict stays "ready" while the branch has actually gone stale.

Real temp git repos via `subprocess`, same idiom as
`tests/test_approve_ready_cli.py:_make_repo`.
"""

from __future__ import annotations

import asyncio
import subprocess

from no_human.vcs.landability import check_landability


def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _git_out(repo_path, *args):
    return subprocess.run(["git", "-C", str(repo_path), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _check(repo, branch, base_hint="main"):
    return asyncio.run(check_landability(str(repo), branch, base_hint=base_hint))


def test_branch_that_merges_cleanly_is_clean(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "new.txt").write_text("new\n")
    _git(repo, "add", "new.txt")
    _git(repo, "commit", "-m", "feature adds a new file")
    _git(repo, "checkout", "main")

    result = _check(repo, "feature")

    assert result.state == "clean"
    assert result.conflicts == ()
    assert result.base_ref == "main"
    assert result.base_sha == _git_out(repo, "rev-parse", "main")


def test_branch_conflicting_with_moved_base_is_conflict(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("feature edit\n")
    _git(repo, "commit", "-am", "feature edits a.txt")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("main edit\n")
    _git(repo, "commit", "-am", "main edits a.txt after branching")

    result = _check(repo, "feature")

    assert result.state == "conflict"
    assert "a.txt" in result.conflicts
    assert result.base_ref == "main"


def test_manifest_only_conflict_is_derived_not_conflict(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "RELEASE_MANIFEST.txt").write_text("pin one\npin two\n")
    _git(repo, "add", "RELEASE_MANIFEST.txt")
    _git(repo, "commit", "-m", "add manifest")
    _git(repo, "checkout", "-b", "feature")
    (repo / "RELEASE_MANIFEST.txt").write_text("feature pin one\npin two\n")
    _git(repo, "commit", "-am", "feature regenerates manifest")
    _git(repo, "checkout", "main")
    (repo / "RELEASE_MANIFEST.txt").write_text("main pin one\npin two\n")
    _git(repo, "commit", "-am", "main regenerates manifest (another landing)")

    result = _check(repo, "feature")

    assert result.state == "derived"
    assert "RELEASE_MANIFEST.txt" in result.conflicts


def test_unresolvable_base_is_unknown_never_conflict(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "new.txt").write_text("new\n")
    _git(repo, "add", "new.txt")
    _git(repo, "commit", "-m", "feature commit")
    _git(repo, "branch", "-D", "main")

    result = _check(repo, "feature", base_hint="")

    assert result.state == "unknown"
    assert result.state != "conflict"
    assert result.conflicts == ()


def test_probe_writes_no_refs_and_leaves_worktree_clean(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("feature edit\n")
    _git(repo, "commit", "-am", "feature edits a.txt")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("main edit\n")
    _git(repo, "commit", "-am", "main edits a.txt after branching")

    before_refs = _git_out(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    before_branch = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    before_status = _git_out(repo, "status", "--porcelain")

    result = _check(repo, "feature")
    assert result.state == "conflict"  # sanity: the interesting path ran

    after_refs = _git_out(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    after_branch = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")
    after_status = _git_out(repo, "status", "--porcelain")

    assert after_refs == before_refs
    assert after_branch == before_branch
    assert after_status == before_status
