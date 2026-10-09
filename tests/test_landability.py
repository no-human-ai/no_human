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

from no_human.vcs.derived_conflict import conflicting_paths
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


def test_classification_count_only_conflict_is_conflict_not_derived(tmp_path):
    """Review finding on the prior attempt: `land_task`'s squash step
    (approve_merge.py ~1060) only tolerates `unmerged == {"RELEASE_MANIFEST.
    txt"}` — nothing else. `derived_conflict.mechanically_resolvable` judges
    an `EXPORT_CLASSIFICATION.txt`-only, count-drift conflict resolvable
    too, but that machinery backs a DIFFERENT resolver
    (`resolve_derived_conflict`), never `land_task`. If `check_landability`
    reused that wider eligible set, `--ready` would render `merge: clean`
    for a task `nh approve` still refuses at `squash` — the exact
    overclaim this test pins against a regression."""
    repo = _make_repo(tmp_path)
    (repo / "EXPORT_CLASSIFICATION.txt").write_text("ship: 1 files\n")
    _git(repo, "add", "EXPORT_CLASSIFICATION.txt")
    _git(repo, "commit", "-m", "add classification ledger")
    _git(repo, "checkout", "-b", "feature")
    (repo / "EXPORT_CLASSIFICATION.txt").write_text("ship: 2 files\n")
    _git(repo, "commit", "-am", "feature bumps the ledger count")
    _git(repo, "checkout", "main")
    (repo / "EXPORT_CLASSIFICATION.txt").write_text("ship: 3 files\n")
    _git(repo, "commit", "-am", "main bumps the ledger count (another landing)")

    result = _check(repo, "feature")

    assert result.state == "conflict"
    assert "EXPORT_CLASSIFICATION.txt" in result.conflicts


def test_manifest_plus_hand_authored_conflict_is_conflict_not_derived(tmp_path):
    """The mixed shape the 2026-09-14 incident actually hit (PR #356):
    RELEASE_MANIFEST.txt conflicts ALONGSIDE a hand-authored file.
    `land_task` refuses this at `squash` because `unmerged != {"RELEASE_
    MANIFEST.txt"}` — the ledger conflict alone is not the WHOLE unmerged
    set. `check_landability` must not call this "derived"."""
    repo = _make_repo(tmp_path)
    (repo / "RELEASE_MANIFEST.txt").write_text("pin one\n")
    _git(repo, "add", "RELEASE_MANIFEST.txt")
    _git(repo, "commit", "-m", "add manifest")
    _git(repo, "checkout", "-b", "feature")
    (repo / "RELEASE_MANIFEST.txt").write_text("feature pin\n")
    (repo / "a.txt").write_text("feature edit\n")
    _git(repo, "commit", "-am", "feature edits manifest and a.txt")
    _git(repo, "checkout", "main")
    (repo / "RELEASE_MANIFEST.txt").write_text("main pin\n")
    (repo / "a.txt").write_text("main edit\n")
    _git(repo, "commit", "-am", "main edits manifest and a.txt (another landing)")

    result = _check(repo, "feature")

    assert result.state == "conflict"
    assert "RELEASE_MANIFEST.txt" in result.conflicts
    assert "a.txt" in result.conflicts


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


def _make_repo_with_remote(tmp_path):
    """A repo with a bare `origin` and `main` pushed, as the watcher's
    checkout looks after cloning and fetching."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    repo = _make_repo(tmp_path, "work")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")
    return repo


def _push_delivery_branch_remote_only(repo, name, edits):
    """Create `name`, apply `edits` (path -> text), push it, then DELETE the
    local ref and fetch — so the branch exists ONLY as `origin/<name>`, the
    normal state of a pushed delivery branch that was never checked out here."""
    _git(repo, "checkout", "-b", name)
    for path, text in edits.items():
        (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"{name} work")
    _git(repo, "push", "origin", name)
    _git(repo, "checkout", "main")
    _git(repo, "branch", "-D", name)
    _git(repo, "fetch", "origin")
    return repo


def test_remote_only_branch_that_merges_is_clean_not_unknown(tmp_path):
    """#512 (sharper half): a delivery branch with no LOCAL ref (only
    `origin/<branch>`) used to make `conflicting_paths` return None, so the
    wake conflict handler escalated with an unresolvable ref even after its
    fetch retry. It now falls back to the `origin/` ref, so the bare name
    answers `clean`."""
    repo = _make_repo_with_remote(tmp_path)
    _push_delivery_branch_remote_only(repo, "no-human/deliv", {"new.txt": "new\n"})
    # precondition: the bare name does NOT resolve locally
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
         "no-human/deliv^{commit}"], capture_output=True).returncode != 0

    result = _check(repo, "no-human/deliv")

    assert result.state == "clean"  # was "unknown" before the fix
    assert result.conflicts == ()


def test_remote_only_branch_conflict_is_reported_not_unknown(tmp_path):
    """The fallback reports a real CONFLICT for a remote-only branch too, not a
    blanket clean — the origin/ ref is the pushed head, judged honestly."""
    repo = _make_repo_with_remote(tmp_path)
    # move main after branching so the branch's edit to a.txt conflicts
    _push_delivery_branch_remote_only(repo, "no-human/clash", {"a.txt": "branch edit\n"})
    (repo / "a.txt").write_text("main edit\n")
    _git(repo, "commit", "-am", "main edits a.txt after the branch")

    result = _check(repo, "no-human/clash")

    assert result.state == "conflict"
    assert "a.txt" in result.conflicts


def test_an_unresolvable_branch_is_still_unknown(tmp_path):
    """A branch that exists under neither the bare name nor `origin/` stays
    `unknown` — the fallback adds a candidate, it does not invent an answer."""
    repo = _make_repo_with_remote(tmp_path)

    result = _check(repo, "no-human/does-not-exist")

    assert result.state == "unknown"


def test_conflicting_paths_answers_a_remote_only_branch_directly(tmp_path):
    """Pin the wake-side entry point this PR is really about: `conflicting_paths`
    called with a bare delivery-branch name that has only an `origin/` ref now
    returns a real set (empty for a clean merge, the conflicted paths otherwise)
    instead of None, so the wake conflict handler stops escalating a branch that
    merges fine."""
    repo = _make_repo_with_remote(tmp_path)
    _push_delivery_branch_remote_only(repo, "no-human/direct", {"new.txt": "new\n"})
    assert asyncio.run(conflicting_paths(str(repo), "main", "no-human/direct")) == set()

    _push_delivery_branch_remote_only(repo, "no-human/direct-clash", {"a.txt": "branch\n"})
    (repo / "a.txt").write_text("main\n")
    _git(repo, "commit", "-am", "main edits a.txt after the branch")
    assert "a.txt" in asyncio.run(
        conflicting_paths(str(repo), "main", "no-human/direct-clash"))


def test_a_local_branch_wins_over_a_diverged_origin_ref(tmp_path):
    """When the bare name resolves, it is the ref asked about — even if
    `origin/<branch>` points at a different commit. Here `origin/<branch>`
    conflicts with main and the local branch merges cleanly, so the answer
    is the local branch's (empty), not origin's (`a.txt`)."""
    repo = _make_repo_with_remote(tmp_path)
    _push_delivery_branch_remote_only(repo, "no-human/split", {"a.txt": "branch\n"})
    # a LOCAL no-human/split at a different, clean commit off the same base
    _git(repo, "checkout", "-b", "no-human/split", "main")
    (repo / "new.txt").write_text("new\n")
    _git(repo, "add", "new.txt")
    _git(repo, "commit", "-m", "local split work")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("main\n")
    _git(repo, "commit", "-am", "main edits a.txt after the branch")
    # precondition: the two refs differ, and origin's really conflicts
    assert (_git_out(repo, "rev-parse", "no-human/split")
            != _git_out(repo, "rev-parse", "origin/no-human/split"))
    assert "a.txt" in asyncio.run(
        conflicting_paths(str(repo), "main", "origin/no-human/split"))

    assert asyncio.run(
        conflicting_paths(str(repo), "main", "no-human/split")) == set()
