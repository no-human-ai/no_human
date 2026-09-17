"""Refutation of intake's "direction 1" (process isolation/confinement) for
task reviewer-worktree-shared-config-attribution.

The intake Q&A for that task assumed process isolation could stop a
non-reviewer process (e.g. `gh pr checkout` running in the MAIN checkout)
from ever landing a write in the `.git/common/config` a reviewer worktree
shares with every other worktree and the main checkout — pointing
`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` at throwaway paths, plus
`GIT_CONFIG_NOSYSTEM=1`, to "confine" git to some redirectable scope.

Measured here: none of those variables can redirect the REPO-LOCAL config
scope. `.git/common/config` (via a worktree's `commondir` indirection, or
`.git/config` in a non-worktree repo) IS that local scope, and `git config
--file <local>` / `git remote add` (which writes the local scope by default)
write it directly, regardless of every global/system override. There is no
environment variable that redirects "the repo's own config" to somewhere
else — only the GLOBAL and SYSTEM scopes are redirectable, and `git remote
add` never touches either. This is why direction 1 was rejected in favor of
direction 3 (report the shared-config change as an unattributed
`Delta.environment` event instead of trying to prevent or attribute it) —
see `src/no_human/core/reviewer_worktree.py`'s module docstring, "Fourth
re-scope" paragraph.
"""

from __future__ import annotations

import os
import subprocess

import pytest


def _git(cwd, *args, env=None, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check, env=env,
    )


@pytest.fixture
def linked_worktree(tmp_path):
    """A real linked worktree, so `<repo>/.git` is the private admin dir and
    the ORIGINAL repo's `.git/config` is the shared common config every
    worktree (and the main checkout) resolves through."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "f.txt").write_text("v1\n")
    _git(main, "add", ".")
    _git(main, "commit", "-qm", "base")

    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "--detach", str(wt), "main")

    common_config = main / ".git" / "config"
    assert common_config.exists(), "no common config to test confinement against"
    return {"main": main, "wt": wt, "common_config": common_config,
            "env_template": dict(os.environ)}


def test_git_config_global_isolation_cannot_prevent_a_shared_config_write(
    linked_worktree, tmp_path,
):
    """Refutation of intake direction 1: pointing `GIT_CONFIG_GLOBAL` and
    `GIT_CONFIG_SYSTEM` at throwaway paths, plus `GIT_CONFIG_NOSYSTEM=1`, does
    NOT stop `git remote add` (run from the reviewer's own linked worktree)
    from writing the shared, repo-local `.git/config` — there is no
    environment-variable scope that redirects the repo-local config, only the
    global/system scopes, and `git remote add` never touches those.
    """
    wt = linked_worktree["wt"]
    common_config = linked_worktree["common_config"]
    throwaway_global = tmp_path / "throwaway-global.gitconfig"
    throwaway_system = tmp_path / "throwaway-system.gitconfig"

    before = common_config.read_text(encoding="utf-8")

    env = dict(linked_worktree["env_template"])
    env["GIT_CONFIG_GLOBAL"] = str(throwaway_global)
    env["GIT_CONFIG_SYSTEM"] = str(throwaway_system)
    env["GIT_CONFIG_NOSYSTEM"] = "1"

    _git(wt, "remote", "add", "fork1", "https://github.com/x/y.git", env=env)

    after = common_config.read_text(encoding="utf-8")
    assert after != before, (
        "the shared repo-local config was not written at all — the fixture "
        "premise is wrong, not the confinement claim")
    assert "fork1" in after, (
        "git remote add did not land in the shared repo-local config even "
        "with confinement env vars set — re-examine the fixture")

    assert not throwaway_global.exists() or "fork1" not in throwaway_global.read_text(
        encoding="utf-8"), (
        "the remote landed in the throwaway global instead of the shared "
        "repo-local config — the fixture, not the claim, would be wrong")


def test_a_plain_file_append_to_the_shared_config_is_equally_unpreventable(
    linked_worktree,
):
    """A second, even more direct route to the same conclusion: nothing about
    `.git/common/config` stops a plain, non-git file write (the shape of a
    hand-rolled script or a non-git tool) from mutating it — process-level
    "confinement" of the `git` binary's environment variables would not have
    touched this route at all, since no `git` invocation is involved.
    """
    common_config = linked_worktree["common_config"]

    before = common_config.read_text(encoding="utf-8")
    with common_config.open("a", encoding="utf-8") as f:
        f.write("[remote \"fork2\"]\n\turl = https://github.com/a/b.git\n")

    after = common_config.read_text(encoding="utf-8")
    assert after != before
    assert "fork2" in after, (
        "a plain file append to the shared config did not take effect — "
        "the fixture premise is wrong, not the confinement claim")
