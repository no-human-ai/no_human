"""Git ops + local PR-open path against a real bare repo. Never merges."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from no_human import vcs
from no_human.vcs import GitRepo, ProtectedBranch, PushBehindRemote


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_bare_remote(tmp_path):
    """A work repo with one commit on `main`, wired to a local bare remote."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def test_branch_and_commit_under_agent_identity(repo_with_bare_remote, monkeypatch):
    # ROOT-CAUSED 2026-07-25 (4 tasks burned; SCRUM-40's excerpt capture
    # finally surfaced it): coder/reviewer sessions export the CONFIGURED
    # agent identity via _agent_git_env (e.g. no-human@users.noreply.github.com),
    # and git's env vars beat GitRepo's `-c user.email`. This test verifies the
    # `-c` mechanism itself, so it must be hermetic against inherited identity
    # env — env-vs-`-c` precedence is git's contract, not ours to test.
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                   identity_email="no-human@acme.com")
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    result = repo.commit_all("PROJ-1: add feature")
    assert result.branch == "no-human/abc123"
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"],
                            cwd=repo.path, capture_output=True, text=True).stdout.strip()
    assert author == "no_human <no-human@acme.com>", f"author={author!r}"


def test_refuses_to_create_protected_branch(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    with pytest.raises(ProtectedBranch):
        repo.create_branch("main")


def test_refuses_to_commit_on_protected_branch(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    (repo.path / "x.py").write_text("z = 3\n")
    with pytest.raises(ProtectedBranch):
        repo.commit_all("should not commit on main")


def test_refuses_to_push_protected_branch(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    with pytest.raises(ProtectedBranch):
        repo.push("main")


def test_stage_excludes_ephemeral_artifacts(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    cache = repo.path / "__pycache__"
    cache.mkdir()
    (cache / "feature.cpython-312.pyc").write_bytes(b"\x00\x01")
    (repo.path / "stale.pyc").write_bytes(b"\x00")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "__pycache__" not in files
    assert ".pyc" not in files


def test_stage_excludes_no_human_settings(repo_with_bare_remote):
    """Regression: .no_human/project.yml must never be committed in a PR."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    nh_dir = repo.path / ".no_human"
    nh_dir.mkdir()
    (nh_dir / "project.yml").write_text("ecosystem: node\ntest_cmd: npm test\n")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert ".no_human" not in files


def test_commit_paths_only_stages_listed_files(repo_with_bare_remote):
    """Regression: test side-effects (e.g. alert-state.json) must not be committed."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    # Agent intentionally edits feature.py
    (repo.path / "feature.py").write_text("y = 2\n")
    # Test run creates a side-effect file
    teams = repo.path / "teams" / "test"
    teams.mkdir(parents=True)
    (teams / "alert-state.json").write_text('{"state": "updated"}')
    # commit_paths only stages the agent's file
    result = repo.commit_paths(
        [str(repo.path / "feature.py")], "PROJ-1: add feature"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "alert-state.json" not in files


def test_commit_paths_falls_back_when_no_tracked_paths(repo_with_bare_remote):
    """If agent only used Bash (no Write/Edit tracked), commit_paths falls back to commit_all."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "script_output.txt").write_text("created by bash\n")
    # Pass empty list — should fall back to stage_all
    result = repo.commit_paths([], "PROJ-1: bash work")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "script_output.txt" in files


def test_commit_paths_includes_modified_tracked_files(repo_with_bare_remote):
    """Modified tracked files (e.g. requirements.txt edited via Bash) must be
    committed even when the agent only Edit-tracked a test file."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    # Set up: add a tracked file to the repo first
    (repo.path / "requirements.txt").write_text("flask\n")
    repo.commit_all("initial: add requirements.txt")
    # Now: agent creates a test file via Edit tool (tracked by orchestrator)
    (repo.path / "tests_new.py").write_text("def test_x(): pass\n")
    # And: agent modifies the existing tracked file via Bash (not tracked)
    (repo.path / "requirements.txt").write_text("flask\nredis\n")
    # commit_paths is called with only the test file (Edit-tracked)
    result = repo.commit_paths(
        [str(repo.path / "tests_new.py")], "add tests + update requirements"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "tests_new.py" in files       # explicitly listed (Edit-tracked)
    assert "requirements.txt" in files   # modified tracked file auto-included


def test_commit_paths_includes_untracked_code_files(repo_with_bare_remote):
    """New .py files created via Bash should be auto-staged."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    # Agent edits one file via Edit tool
    (repo.path / "test_thing.py").write_text("assert True\n")
    # Agent creates a new source file via Bash (untracked)
    src = repo.path / "src"
    src.mkdir()
    (src / "limiter.py").write_text("class Limiter: pass\n")
    result = repo.commit_paths(
        [str(repo.path / "test_thing.py")], "add tests + limiter"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "test_thing.py" in files
    assert "src/limiter.py" in files  # untracked .py auto-staged


def test_commit_paths_excludes_untracked_json_side_effects(repo_with_bare_remote):
    """Untracked .json files (test side-effects) must still be excluded."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    # Test side-effect: a .json state file
    data = repo.path / "data"
    data.mkdir()
    (data / "state.json").write_text('{"updated": true}')
    result = repo.commit_paths(
        [str(repo.path / "feature.py")], "PROJ-1: add feature"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "state.json" not in files  # untracked .json excluded


def test_commit_paths_excludes_venv_py_files(repo_with_bare_remote):
    """Untracked .py files inside .venv*/ dirs must not be committed."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    # .venv312 with .py files (common in real repos)
    venv = repo.path / ".venv312" / "lib" / "site-packages" / "redis"
    venv.mkdir(parents=True)
    (venv / "__init__.py").write_text("")
    (venv / "client.py").write_text("class Redis: pass\n")
    result = repo.commit_paths(
        [str(repo.path / "feature.py")], "PROJ-1: add feature"
    )
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert ".venv312" not in files   # venv excluded by _EPHEMERAL


def test_commit_paths_includes_new_source_when_only_an_edit_is_tracked(repo_with_bare_remote):
    """The PR #2 bug: the hook tracked only the EDITED file (App.jsx); the coder
    also created a new .js helper + a .mjs test. `git add -- <paths>
    :(exclude,glob)…` silently dropped the untracked ones, so App.jsx shipped
    importing a module that wasn't committed — a broken PR. All coder-created
    source (incl. .mjs) must land; ephemeral venv files must not."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/pr2", base="main")
    (repo.path / "App.jsx").write_text("import {b} from './helper.js'; export const a=b;\n")
    (repo.path / "helper.js").write_text("export const b=3;\n")          # new .js
    (repo.path / "helper.test.mjs").write_text("import {b} from './helper.js';\n")  # new .mjs
    venv = repo.path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "junk.py").write_text("x=1\n")                               # ephemeral
    # Simulate the hook having tracked ONLY the edited App.jsx.
    repo.commit_paths([str(repo.path / "App.jsx")], "add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "App.jsx" in files
    assert "helper.js" in files          # the .js that was dropped in PR #2
    assert "helper.test.mjs" in files    # the .mjs (ext was missing from _CODE_EXTS)
    assert ".venv" not in files          # ephemeral still excluded


def test_uncommitted_source_files_flags_an_incomplete_commit(repo_with_bare_remote):
    """The committed-state guard: a coder-created source file left OUT of the
    commit must be reported (it would ship a broken PR); non-code/ephemeral
    files are ignored; a complete commit reports nothing."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/guard", base="main")
    (repo.path / "app.js").write_text("import './helper.js';\n")
    (repo.path / "helper.js").write_text("export const b=1;\n")   # source, must land
    (repo.path / "notes.txt").write_text("scratch\n")             # non-code, ignored
    (repo.path / ".venv").mkdir()
    (repo.path / ".venv" / "x.py").write_text("y=1\n")            # ephemeral, ignored
    # Simulate the PR #2 bug: commit only app.js, leaving helper.js behind.
    subprocess.run(["git", "add", "app.js"], cwd=repo.path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "partial"], cwd=repo.path, check=True, capture_output=True)
    leftover = repo.uncommitted_source_files()
    assert "helper.js" in leftover                    # the broken-PR file
    assert "notes.txt" not in leftover                # non-code data ignored
    assert not any(".venv" in f for f in leftover)    # ephemeral ignored
    # After committing everything, the guard is clean.
    repo.commit_all("rest")
    assert repo.uncommitted_source_files() == []


def test_is_github_remote_detects_github_and_ghe():
    from no_human.vcs import github
    assert github.is_github_remote("https://github.com/org/repo.git")
    assert github.is_github_remote("git@github.com:org/repo.git")
    # GHE host only when configured.
    ghe = "https://code.example.com/dev/repo.git"
    assert not github.is_github_remote(ghe)
    assert github.is_github_remote(ghe, ["code.example.com"])
    # unrelated host stays false
    assert not github.is_github_remote("https://gitlab.com/org/repo.git", ["code.example.com"])


def test_open_pr_caller_inventory_comment_names_both_callers():
    """The "OPT-IN ONLY" comment above `update_existing_body` narrates who is
    allowed to pass True. It used to claim ``_finalize`` was the only caller,
    which the already-satisfied gate landing (`_gate_already_satisfied` also
    passes True, guarded by its own stricter identity check) made false — a
    review caught the stale inventory. Pin the fix: the comment must name
    both callers, not just `_finalize`.
    """
    import inspect
    from no_human.vcs import github
    src = inspect.getsource(github.open_pr)
    assert "_finalize" in src
    assert "_gate_already_satisfied" in src, (
        "the caller-inventory comment must enumerate _gate_already_satisfied "
        "as a second update_existing_body=True caller, not just _finalize")


# --------------------------------------------------------------------------- #
# PR labels: the pr_labels feature (attach labels when opening a PR/MR) was   #
# removed — operator directive 2026-08-15. Opening a PR/MR must never send    #
# a --label flag, and the labels= kwarg must no longer exist anywhere.        #
# --------------------------------------------------------------------------- #


def _capture_argv(monkeypatch, module, returncode=0, stdout="https://pr/1"):
    """Record every argv the forge CLI would be invoked with."""
    calls = []

    class _Proc:
        pass

    def fake_run(argv, **kwargs):
        calls.append(argv)
        proc = _Proc()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = ""
        return proc

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return calls


def test_github_open_pr_never_sends_a_label_flag(monkeypatch, tmp_path):
    from no_human.vcs import github
    calls = _capture_argv(monkeypatch, github)
    url = github.open_pr(tmp_path, "br", "PROJ-1: t", "body", base="dev")
    assert url == "https://pr/1"
    assert len(calls) == 1  # no retry-without-labels loop left to trigger
    argv = calls[0]
    assert argv[:3] == ["gh", "pr", "create"]
    assert "--label" not in argv
    assert "--draft" in argv  # never merges
    with pytest.raises(TypeError):
        github.open_pr(tmp_path, "br", "t", "b", base="dev", labels=["x"])


def test_gitlab_open_mr_never_sends_a_label_flag(monkeypatch, tmp_path):
    from no_human.vcs import gitlab
    calls = _capture_argv(monkeypatch, gitlab)
    url = gitlab.open_mr(tmp_path, "br", "t", "body", base="dev")
    assert url == "https://pr/1"
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:3] == ["glab", "mr", "create"]
    assert "--label" not in argv
    assert "--no-merge" in argv  # never merges
    with pytest.raises(TypeError):
        gitlab.open_mr(tmp_path, "br", "t", "b", base="dev", labels=["x"])


def test_open_pr_facade_rejects_labels_kwarg():
    with pytest.raises(TypeError):
        vcs.open_pr(None, "br", "t", "b", labels=["x"])


def test_pr_labels_config_key_removed():
    import no_human.config as config
    assert "pr_labels" not in config.DEFAULT_CONFIG["git"]


def test_open_pr_local_path(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    repo.commit_all("PROJ-1: add feature")
    pr = vcs.open_pr(repo, "no-human/abc123", "PROJ-1: add feature", "body", base="main")
    assert pr.kind == "local"
    assert "no-human/abc123" in pr.url
    # branch made it to the bare remote
    branches = subprocess.run(["git", "branch", "--list"], cwd=repo.path,
                              capture_output=True, text=True).stdout
    assert "no-human/abc123" in branches


# --------------------------------------------------------------------------- #
# Worktree isolation (Phase 7 foundation)                                      #
# --------------------------------------------------------------------------- #

def test_worktree_is_isolated_working_tree(repo_with_bare_remote, tmp_path):
    """Two worktrees off the same repo have independent files/branches; an edit
    in one is invisible in the other and on the main tree."""
    repo = GitRepo(repo_with_bare_remote)
    wt_a = repo.add_worktree(tmp_path / "wt-a", base="main", branch="no-human/a")
    wt_b = repo.add_worktree(tmp_path / "wt-b", base="main", branch="no-human/b")

    (wt_a.path / "a_only.py").write_text("a = 1\n")
    wt_a.commit_all("PROJ-1: a change")

    assert (wt_a.path / "a_only.py").exists()
    assert not (wt_b.path / "a_only.py").exists()        # isolated from sibling
    assert not (repo.path / "a_only.py").exists()         # and from the main tree
    assert wt_a.current_branch() == "no-human/a"
    assert wt_b.current_branch() == "no-human/b"
    # Both share the object store but have distinct HEADs.
    assert wt_a.head_sha() != wt_b.head_sha()


def test_worktree_refuses_protected_branch(repo_with_bare_remote, tmp_path):
    repo = GitRepo(repo_with_bare_remote)
    with pytest.raises(ProtectedBranch):
        repo.add_worktree(tmp_path / "wt-main", base="main", branch="main")


def test_worktree_context_manager_cleans_up(repo_with_bare_remote, tmp_path):
    repo = GitRepo(repo_with_bare_remote)
    wt_path = tmp_path / "wt-scoped"
    with repo.worktree(wt_path, base="main", branch="no-human/scoped") as wt:
        (wt.path / "f.py").write_text("z = 3\n")
        wt.commit_all("PROJ-9: scoped")
        assert wt_path.exists()
        assert str(wt_path.resolve()) in [w for w in repo.list_worktrees()]
    # Removed on exit; main repo no longer lists it.
    assert str(wt_path.resolve()) not in [w for w in repo.list_worktrees()]


# --------------------------------------------------------------------------- #
# Commit message sanitization (I2)                                             #
# --------------------------------------------------------------------------- #


def test_commit_strips_claude_attribution(repo_with_bare_remote):
    """Co-authored-by: Claude trailer must be silently removed."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    msg = "PROJ-1: add feature\n\nCo-authored-by: Claude <noreply@anthropic.com>"
    result = repo.commit_all(msg)
    log = subprocess.run(["git", "log", "-1", "--format=%B"],
                         cwd=repo.path, capture_output=True, text=True).stdout.strip()
    assert "Claude" not in log
    assert "anthropic" not in log
    assert "PROJ-1: add feature" in log


def test_commit_strips_openai_attribution(repo_with_bare_remote):  # term-ok: must match a real third-party trailer
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    msg = "PROJ-1: fix\n\nCo-authored-by: OpenAI <noreply@openai.com>"  # term-ok: real third-party trailer text
    result = repo.commit_all(msg)
    log = subprocess.run(["git", "log", "-1", "--format=%B"],
                         cwd=repo.path, capture_output=True, text=True).stdout.strip()
    assert "OpenAI" not in log  # term-ok: real third-party trailer text


def test_commit_preserves_legitimate_coauthor(repo_with_bare_remote):
    """A real human co-author must NOT be stripped."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    msg = "PROJ-1: fix\n\nCo-authored-by: Jane Doe <jane@example.com>"
    result = repo.commit_all(msg)
    log = subprocess.run(["git", "log", "-1", "--format=%B"],
                         cwd=repo.path, capture_output=True, text=True).stdout.strip()
    assert "Jane Doe" in log


# --------------------------------------------------------------------------- #
# Expanded ephemeral exclusions (I6)                                           #
# --------------------------------------------------------------------------- #


def test_stage_excludes_windsurf_devin_claude_dirs(repo_with_bare_remote):
    """IDE/agent config directories must never be committed."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    for dirname in (".windsurf", ".devin", ".claude"):  # term-ok: real on-disk IDE config dirs
        d = repo.path / dirname
        d.mkdir()
        (d / "settings.json").write_text("{}")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert ".windsurf" not in files  # term-ok: real on-disk IDE config dir
    assert ".devin" not in files  # term-ok: real on-disk IDE config dir
    assert ".claude" not in files


def test_stage_excludes_handover_md(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/abc123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    (repo.path / "HANDOVER.md").write_text("# handover notes\n")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "HANDOVER.md" not in files


def test_stage_excludes_plan_md(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/plan123", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    (repo.path / "PLAN.md").write_text("## FILES TO CHANGE\n- feature.py\n")
    result = repo.commit_all("PROJ-1: add feature")
    files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                           cwd=repo.path, capture_output=True, text=True).stdout
    assert "feature.py" in files
    assert "PLAN.md" not in files


def test_open_pr_refuses_an_unrecognized_forge_before_pushing(repo_with_bare_remote):
    """An https remote we can't classify must never receive a push — the old
    behavior pushed first and reported a fake local-pr:// marker as success."""
    import pytest
    from no_human.vcs import open_pr

    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                   identity_email="no-human@acme.com")
    _git(repo_with_bare_remote, "remote", "set-url", "origin",
         "https://forge.example.com/team/repo.git")
    repo.create_branch("nh-test")
    with pytest.raises(RuntimeError, match="not recognized"):
        open_pr(repo, "nh-test", "t", "b", base="main")


def test_open_pr_still_pushes_to_a_local_bare_remote(repo_with_bare_remote, tmp_path):
    from no_human.vcs import open_pr

    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                   identity_email="no-human@acme.com")
    repo.create_branch("nh-local")
    pr = open_pr(repo, "nh-local", "t", "b", base="main")
    assert pr.kind == "local" and pr.url.startswith("local-pr://")


def test_worktree_attach_existing_branch_for_resume(repo_with_bare_remote, tmp_path):
    """create=False attaches an existing branch — the resume-a-parked-task path."""
    repo = GitRepo(repo_with_bare_remote)
    wt1 = repo.add_worktree(tmp_path / "wt1", base="main", branch="no-human/resume")
    (wt1.path / "wip.py").write_text("w = 1\n")
    wt1.commit_all("PROJ-7: wip")
    sha = wt1.head_sha()
    repo.remove_worktree(tmp_path / "wt1")
    # Re-attach the same branch in a fresh worktree (no -b): work is preserved.
    wt2 = repo.add_worktree(tmp_path / "wt2", base="main",
                            branch="no-human/resume", create=False)
    assert wt2.head_sha() == sha
    assert (wt2.path / "wip.py").exists()


# --- C3: default-branch auto-detection (origin/HEAD) --- #

def test_default_branch_via_remote_show_fallback(repo_with_bare_remote):
    """A plain `git init` + `remote add` + `push` (this fixture, and what
    no_human's own clone path does) never populates refs/remotes/origin/HEAD —
    only `git clone` does that automatically. Must fall back to
    `git remote show origin` and still resolve correctly."""
    repo = GitRepo(repo_with_bare_remote)
    assert repo.default_branch() == "main"


def test_default_branch_via_symbolic_ref_when_set(repo_with_bare_remote):
    """When origin/HEAD IS set locally (e.g. after a real `git clone`), use it
    directly without the slower `remote show` round-trip."""
    repo = GitRepo(repo_with_bare_remote)
    subprocess.run(["git", "remote", "set-head", "origin", "main"],
                   cwd=repo.path, check=True, capture_output=True)
    assert repo.default_branch() == "main"


def test_default_branch_empty_when_remote_show_reports_unknown(tmp_path):
    """git prints 'HEAD branch: (unknown)' when the remote's HEAD points at a
    ref the local repo has no knowledge of. Must be treated as undetermined,
    not returned literally as a fake branch name."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "master")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "master")  # bare's "main" ref never created
    repo = GitRepo(work)
    assert repo.default_branch() == ""


def test_default_branch_empty_when_no_origin(tmp_path):
    """No remote at all (e.g. a fresh local-only repo) → best-effort empty,
    never raises."""
    work = tmp_path / "solo"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    repo = GitRepo(work)
    assert repo.default_branch() == ""


def test_coder_touched_non_code_files_flagged_as_leftovers(repo_with_bare_remote):
    """B2 #7: a coder-CREATED Dockerfile/yaml left out of the commit shipped a
    broken PR past every gate (the favicon/commit_paths class, non-code shape).
    A leftover the coder itself touched is flagged regardless of extension;
    untouched non-code leftovers (test side-effects) stay ignored."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/b27", base="main")
    (Path(repo.path) / "Dockerfile").write_text("FROM python:3.12\n")
    (Path(repo.path) / "alert-state.json").write_text("{}")  # side-effect

    plain = repo.uncommitted_source_files()
    assert "Dockerfile" not in plain and "alert-state.json" not in plain

    flagged = repo.uncommitted_source_files(
        coder_touched={"Dockerfile"})
    assert "Dockerfile" in flagged
    assert "alert-state.json" not in flagged


# --------------------------------------------------------------------------
# DELIVERY: a rebased branch cannot fast-forward, and that stranded real work.
#
# Reproduces the measured incident (2026-08-11). `git reflog` on a stranded
# branch read:
#     rebase (finish): refs/heads/no-human/fd53d187-2 onto <new main>
#     commit: ...                                  <- already PUSHED
# The agent rebases its own pushed branch (agent/guard.py permits this as the
# legitimate "rebase base into my branch" workflow), so `_finalize`'s plain
# `git push -u` is rejected non-fast-forward. The attempt held a PASSING review
# and a green suite and was escalated anyway. 81 rejections in one week; 0 of
# the 7 tasks that ever hit one reached `done`.
# --------------------------------------------------------------------------

def _rebased_pushed_branch(work):
    """Push a branch, then rebase it onto a moved main — the incident's shape."""
    repo = GitRepo(work, identity_name="no_human", identity_email="a@b.invalid")
    repo.create_branch("no-human/t1")
    (work / "feature.py").write_text("y = 1\n")
    repo.commit_all("the work")
    repo.push("no-human/t1")                      # delivered, remote holds this
    _git(work, "checkout", "main")
    (work / "other.py").write_text("z = 1\n")     # main moves under the branch
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "main moves on")
    _git(work, "checkout", "no-human/t1")
    _git(work, "rebase", "main")                  # rewrites the pushed commit
    return repo


def test_a_rebased_branch_cannot_be_delivered_by_a_plain_push(repo_with_bare_remote):
    """The incident's shape, pinned: a plain push of a rebased branch is
    rejected. True before and after the fix — this is the failure the retry
    detects, not a behaviour the fix changes."""
    repo = _rebased_pushed_branch(repo_with_bare_remote)
    with pytest.raises(Exception) as exc:
        repo.push("no-human/t1")
    assert "rejected" in str(exc.value).lower()


def test_force_with_lease_delivers_the_rebased_branch(repo_with_bare_remote):
    """The fix: the same push succeeds, and the remote ends at OUR commit."""
    repo = _rebased_pushed_branch(repo_with_bare_remote)
    local = repo._run("rev-parse", "no-human/t1")
    pushed = repo.push("no-human/t1", force_with_lease=True)
    assert pushed == local
    remote = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/no-human/t1"],
        cwd=repo_with_bare_remote, capture_output=True, text=True, check=True,
    ).stdout.split()[0]
    assert remote == local, "the remote did not end at the branch we pushed"


def test_force_with_lease_still_refuses_a_protected_branch(repo_with_bare_remote):
    """The protection check runs BEFORE the force flag is ever consulted, so
    the escape hatch can never reach main — constraint #2 is unaffected."""
    repo = GitRepo(repo_with_bare_remote, never_push_to=["main"])
    with pytest.raises(ProtectedBranch):
        repo.push("main", force_with_lease=True)


# The detector decides whether a retry may FORCE-push, so a false positive is a
# force-push on an unrelated error. It must key on git's rejection vocabulary,
# not on the word "rejected" alone — protection refusals and auth failures both
# contain it.
@pytest.mark.parametrize("msg,expected", [
    ("! [rejected] no-human/t1 -> no-human/t1 (non-fast-forward)", True),
    ("! [rejected] main -> main (fetch first)", True),
    ("refusing to push protected branch: main", False),
    ("remote: Permission to org/repo.git denied; push rejected", False),
    ("fatal: could not read Username: terminal prompts disabled", False),
    ("error: failed to push some refs", False),
    ("", False),
])
def test_only_a_real_non_fast_forward_earns_a_force_push(msg, expected):
    from no_human.core.orchestrator import _is_non_fast_forward
    assert _is_non_fast_forward(RuntimeError(msg)) is expected


def _remote_tip(bare_root, branch="no-human/t1"):
    return subprocess.run(
        ["git", "ls-remote", str(bare_root), f"refs/heads/{branch}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()[0]


def test_the_lease_refuses_a_commit_we_have_never_seen(repo_with_bare_remote, tmp_path):
    """THE property the lease exists for: someone else moved the branch since
    our last contact — a human fixup via the revision flow, another attempt,
    anything. The push must be REFUSED (git's ``stale info``), never delivered,
    and the third party's commit must survive on the remote. The refusal
    surfaces as the ordinary push failure, so the delivery retry's second
    failure escalates honestly instead of destroying work.

    Regression test for the 2026-08-11 review finding: a pre-push fetch of the
    branch refreshed the very remote-tracking ref the lease is judged against,
    making the lease vacuous — equivalent to a plain ``--force`` — and this
    exact scenario silently destroyed the third party's commit.
    """
    repo = _rebased_pushed_branch(repo_with_bare_remote)
    bare = tmp_path / "remote.git"
    intruder = tmp_path / "intruder"
    subprocess.run(["git", "clone", "-q", str(bare), str(intruder)],
                   check=True, capture_output=True)
    _git(intruder, "config", "user.email", "h@example.com")
    _git(intruder, "config", "user.name", "h")
    _git(intruder, "checkout", "no-human/t1")
    (intruder / "human_fixup.py").write_text("fix = 1\n")
    _git(intruder, "add", "-A")
    _git(intruder, "commit", "-m", "human fixup on the PR branch")
    _git(intruder, "push", "origin", "no-human/t1")
    theirs = _remote_tip(bare)

    with pytest.raises(Exception) as exc:
        repo.push("no-human/t1", force_with_lease=True)
    assert "rejected" in str(exc.value).lower() or "stale" in str(exc.value).lower()
    assert _remote_tip(bare) == theirs, (
        "the third party's commit was destroyed — the lease did not protect it")


def test_the_lease_without_a_tracking_ref_refuses_rather_than_guesses(
        repo_with_bare_remote, tmp_path):
    """A fresh checkout with no remote-tracking ref for the branch has no basis
    for a lease. The push must refuse (an honest escalation upstream), never
    silently force over whatever the remote holds."""
    repo = _rebased_pushed_branch(repo_with_bare_remote)
    _git(repo_with_bare_remote, "update-ref", "-d",
         "refs/remotes/origin/no-human/t1")
    before = _remote_tip(tmp_path / "remote.git")
    with pytest.raises(Exception):
        repo.push("no-human/t1", force_with_lease=True)
    assert _remote_tip(tmp_path / "remote.git") == before


# --------------------------------------------------------------------------
# DELIVERY: a BEHIND branch must never be forced — the opposite incident.
#
# `_rebased_pushed_branch` above reproduces DIVERGED (a rebase of our OWN
# already-pushed branch): force-with-lease is the correct remedy. This block
# reproduces the OTHER shape that produces the identical git error string: the
# local branch is an ANCESTOR of its own remote tip because someone else (an
# earlier attempt) already pushed further commits. The two are indistinguishable
# by error text alone; only ancestry tells them apart (task e6719bd8: a retry
# force-pushed a BEHIND branch, the lease correctly refused, and the attempt
# then swallowed the failure and burned 4.9M tokens reviewing an unpublishable
# tree).
# --------------------------------------------------------------------------

def _behind_pushed_branch(work, tmp_path):
    """Push a branch, then have a second, independent worktree push one more
    commit onto the SAME remote branch — leaving `work`'s local ref a strict
    ancestor of the remote tip. This is task e6719bd8's shape: one attempt
    delivered, and a later attempt ran from an older tree and tried to push
    the same branch again."""
    repo = GitRepo(work, identity_name="no_human", identity_email="a@b.invalid")
    repo.create_branch("no-human/t1")
    (work / "feature.py").write_text("y = 1\n")
    repo.commit_all("first attempt's work")
    repo.push("no-human/t1")                      # delivered, remote holds this

    bare = tmp_path / "remote.git"
    other = tmp_path / "other-attempt"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)],
                   check=True, capture_output=True)
    _git(other, "checkout", "no-human/t1")
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "o")
    (other / "more.py").write_text("z = 1\n")      # a later attempt pushes further
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "second attempt pushed more")
    _git(other, "push", "origin", "no-human/t1")
    return repo                                    # `work`'s ref never learned of this


def test_remote_branch_relation_is_behind_when_local_is_an_ancestor_of_remote(
        repo_with_bare_remote, tmp_path):
    repo = _behind_pushed_branch(repo_with_bare_remote, tmp_path)
    assert repo.remote_branch_relation("no-human/t1") == "behind"


def test_remote_branch_relation_is_diverged_when_neither_is_an_ancestor(
        repo_with_bare_remote):
    """Negative control on the classifier itself: the rebase shape above must
    classify as diverged, not behind."""
    repo = _rebased_pushed_branch(repo_with_bare_remote)
    assert repo.remote_branch_relation("no-human/t1") == "diverged"


def test_remote_branch_relation_is_up_to_date_right_after_a_clean_push(
        repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                    identity_email="a@b.invalid")
    repo.create_branch("no-human/t1")
    (repo_with_bare_remote / "feature.py").write_text("y = 1\n")
    repo.commit_all("the work")
    repo.push("no-human/t1")
    assert repo.remote_branch_relation("no-human/t1") == "up_to_date"


def test_remote_branch_relation_is_unknown_for_an_unpushed_branch(repo_with_bare_remote):
    """Fail-open: a branch with no remote tip at all has no PROVEN ancestry, so
    it must not be classified as behind (that would newly block a push that
    works today)."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/never-pushed")
    assert repo.remote_branch_relation("no-human/never-pushed") == "unknown"


def test_a_behind_branch_never_reaches_a_force_push(
        repo_with_bare_remote, tmp_path, monkeypatch):
    """The real guarantee, asserted on the push ARGUMENTS rather than the
    outcome: a plain push of a BEHIND branch raises PushBehindRemote, and no
    subprocess call this triggers ever carries --force-with-lease."""
    from no_human.core.orchestrator import _is_non_fast_forward

    repo = _behind_pushed_branch(repo_with_bare_remote, tmp_path)
    calls = []
    real_run = subprocess.run

    def _spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)

    with pytest.raises(PushBehindRemote) as exc:
        repo.push("no-human/t1")

    assert calls, "expected at least the rejected push attempt to be recorded"
    assert not any("--force-with-lease" in c for c in calls), calls
    assert _is_non_fast_forward(exc.value) is False


def test_a_diverged_branch_still_forces_with_lease(repo_with_bare_remote):
    """Negative control: DIVERGED must keep TODAY's exact behaviour — this is
    the already-working rebase-recovery path and the fix must not touch it."""
    from no_human.core.orchestrator import _is_non_fast_forward

    repo = _rebased_pushed_branch(repo_with_bare_remote)
    assert repo.remote_branch_relation("no-human/t1") == "diverged"
    assert _is_non_fast_forward(RuntimeError(
        "! [rejected] no-human/t1 -> no-human/t1 (non-fast-forward)")) is True

    local = repo._run("rev-parse", "no-human/t1")
    pushed = repo.push("no-human/t1", force_with_lease=True)
    assert pushed == local
    remote = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/no-human/t1"],
        cwd=repo_with_bare_remote, capture_output=True, text=True, check=True,
    ).stdout.split()[0]
    assert remote == local, "the remote did not end at the branch we pushed"


def test_behind_classification_never_precedes_the_protection_check(
        repo_with_bare_remote, monkeypatch):
    """The protected-branch refusal in `push` runs FIRST, untouched — before
    any ancestry classification, regardless of what the branch's relation to
    its remote tip is. No ls-remote/fetch/push subprocess call should ever
    happen for a protected branch."""
    repo = GitRepo(repo_with_bare_remote, never_push_to=["main"])
    calls = []
    real_run = subprocess.run

    def _spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)

    with pytest.raises(ProtectedBranch):
        repo.push("main")
    with pytest.raises(ProtectedBranch):
        repo.push("main", force_with_lease=True)

    assert calls == [], (
        "the protection check must refuse before any git subprocess runs "
        f"(saw: {calls})")


def test_behind_error_names_the_behind_condition(repo_with_bare_remote, tmp_path):
    """BEHIND must be reported in human-actionable terms — naming the branch
    and the word "behind" — not swallowed into a generic advisory that lets
    the attempt keep reviewing an unpublishable tree (the 4.9M-token incident)."""
    repo = _behind_pushed_branch(repo_with_bare_remote, tmp_path)
    with pytest.raises(PushBehindRemote) as exc:
        repo.push("no-human/t1")
    msg = str(exc.value).lower()
    assert "behind" in msg
    assert "no-human/t1" in msg


def test_is_non_fast_forward_refuses_to_force_a_behind_error():
    from no_human.core.orchestrator import _is_non_fast_forward
    assert _is_non_fast_forward(PushBehindRemote("refusing: behind")) is False


def test_classification_does_not_refresh_the_remote_tracking_ref(
        repo_with_bare_remote, tmp_path):
    """`remote_branch_relation` must never write `refs/remotes/<remote>/<branch>`
    — that is the ref `--force-with-lease` is judged against; refreshing it
    here would make a later lease check vacuous."""
    repo = _behind_pushed_branch(repo_with_bare_remote, tmp_path)
    tracking_ref = "refs/remotes/origin/no-human/t1"
    before = repo._run("rev-parse", "--verify", "--quiet", tracking_ref, check=False)

    relation = repo.remote_branch_relation("no-human/t1")

    assert relation == "behind"
    after = repo._run("rev-parse", "--verify", "--quiet", tracking_ref, check=False)
    assert after == before


# --------------------------------------------------------------------------
# THE HOLE: force_with_lease=True requested UP FRONT never produces a
# GitError, so the failure-path classification above never runs. A caller
# that asks for the force from the start could destroy a behind branch's
# remote commits before the retry-path check above ever gets a chance to
# object. The pre-push classification below closes it.
# --------------------------------------------------------------------------

def test_force_with_lease_on_a_behind_branch_refuses_before_pushing(
        repo_with_bare_remote, tmp_path, monkeypatch):
    """The hole this ticket closes: a force requested UP FRONT (not only one
    earned by a prior rejection) must still be classified before the push
    ever runs. A behind branch must never be forced -- the remote's commits
    (an earlier attempt's already-published work) must survive untouched,
    and no push subprocess call may happen at all.

    The tracking ref is refreshed with a real `fetch` first (as would
    naturally happen between attempts, e.g. before deriving a base branch) so
    the lease's own compare-and-swap is SATISFIED -- exactly the "perfectly
    current tracking ref" shape the ticket describes, where
    `--force-with-lease` alone does not protect a behind branch and only
    ancestry classification does."""
    repo = _behind_pushed_branch(repo_with_bare_remote, tmp_path)
    repo.fetch("origin", prune=False)
    bare = tmp_path / "remote.git"
    theirs = _remote_tip(bare)

    calls = []
    real_run = subprocess.run

    def _spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)

    with pytest.raises(PushBehindRemote):
        repo.push("no-human/t1", force_with_lease=True)

    assert _remote_tip(bare) == theirs, (
        "the remote's commits were destroyed by a force requested up front")
    assert not any("push" in c for c in calls), (
        f"the push must never run once the branch is classified behind: saw {calls}")


def test_force_with_lease_still_forces_when_the_relation_is_unknown(
        repo_with_bare_remote, tmp_path):
    """Negative control: UNKNOWN must keep forcing exactly as today -- an
    unpushed branch has no PROVEN ancestry, so it must not newly block a
    force that works today."""
    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                    identity_email="a@b.invalid")
    repo.create_branch("no-human/never-pushed")
    (repo_with_bare_remote / "feature.py").write_text("y = 1\n")
    repo.commit_all("the work")
    assert repo.remote_branch_relation("no-human/never-pushed") == "unknown"

    local = repo._run("rev-parse", "no-human/never-pushed")
    pushed = repo.push("no-human/never-pushed", force_with_lease=True)
    assert pushed == local
    assert _remote_tip(tmp_path / "remote.git", branch="no-human/never-pushed") == local


def test_force_with_lease_still_forces_when_classification_raises(
        repo_with_bare_remote, monkeypatch):
    """Fail-open pinned directly on the pre-push check: if
    `remote_branch_relation` itself raises (network error, auth failure,
    anything), the force must proceed rather than propagate the exception --
    an unreachable remote must never silently disable delivery."""
    repo = _rebased_pushed_branch(repo_with_bare_remote)

    def _boom(self, branch, *, remote="origin", timeout=30):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(GitRepo, "remote_branch_relation", _boom)

    local = repo._run("rev-parse", "no-human/t1")
    pushed = repo.push("no-human/t1", force_with_lease=True)
    assert pushed == local


def test_protection_refusal_precedes_classification_on_a_diverged_branch(
        repo_with_bare_remote, tmp_path, monkeypatch):
    """The protected-branch refusal must run before any ancestry
    classification -- unconditionally, not merely when the relation happens
    to be "behind". Even a genuinely diverged protected branch must be
    refused with zero git subprocess calls (no ls-remote, no fetch, no
    push)."""
    bare = tmp_path / "remote.git"
    intruder = tmp_path / "intruder-main"
    subprocess.run(["git", "clone", "-q", str(bare), str(intruder)],
                   check=True, capture_output=True)
    _git(intruder, "config", "user.email", "h@example.com")
    _git(intruder, "config", "user.name", "h")
    (intruder / "remote_change.py").write_text("r = 1\n")
    _git(intruder, "add", "-A")
    _git(intruder, "commit", "-m", "remote-side change to main")
    _git(intruder, "push", "origin", "main")

    (repo_with_bare_remote / "local_change.py").write_text("l = 1\n")
    _git(repo_with_bare_remote, "add", "-A")
    _git(repo_with_bare_remote, "commit", "-m", "local-side change to main")

    repo = GitRepo(repo_with_bare_remote, never_push_to=["main"])
    assert repo.remote_branch_relation("main") == "diverged"

    calls = []
    real_run = subprocess.run

    def _spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)

    with pytest.raises(ProtectedBranch):
        repo.push("main", force_with_lease=True)

    assert calls == [], (
        "no git subprocess call should happen before the protection check "
        f"(saw: {calls})")


def test_a_plain_push_does_not_classify_the_remote(repo_with_bare_remote, monkeypatch):
    """No extra network round-trip on the ordinary path: classification is
    gated on `force_with_lease`, so a plain, non-forcing push must never call
    `ls-remote`."""
    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                    identity_email="a@b.invalid")
    repo.create_branch("no-human/t1")
    (repo_with_bare_remote / "feature.py").write_text("y = 1\n")
    repo.commit_all("the work")

    calls = []
    real_run = subprocess.run

    def _spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)

    repo.push("no-human/t1")

    assert not any("ls-remote" in c for c in calls), (
        f"a plain push must not classify the remote: saw {calls}")


# ---- manifest-gate refusal repair (2026-08-11 task_crashed incident) -------- #
# Three finished tasks died at the pipeline's own commit because the manifest
# pre-commit gate refused (changed pinned files, manifest not re-approved) and
# the GitError propagated as task_crashed. The commit path must perform the
# gate's own documented FIX for exactly that refusal shape, and ONLY that one.

_REFUSAL = """no_human pre-commit gate: REFUSED.

These staged file(s) are pinned in RELEASE_MANIFEST.txt, but their staged content does
not match their pin — the file changed and its manifest row did not,
so this commit would ship a file the export ledger has not approved:
  src/pkg/mod.py
      pinned 26d6294b11f3…  staged 42408d9ff027…
  tests/test_mod.py
      pinned 8adc2a3c8e17…  staged c1decc14a38b…

  FIX: re-pin each file and stage the manifest in the SAME commit:
    uv run python scripts/export_guard.py approve src/pkg/mod.py tests/test_mod.py
    git add RELEASE_MANIFEST.txt
"""


def test_parse_manifest_refusal_extracts_exactly_the_pinned_paths():
    from no_human.vcs import parse_manifest_refusal
    assert parse_manifest_refusal(_REFUSAL) == ["src/pkg/mod.py", "tests/test_mod.py"]


def test_parse_manifest_refusal_rejects_other_hook_failures():
    from no_human.vcs import parse_manifest_refusal
    assert parse_manifest_refusal("husky: commit-msg check failed") is None
    # The gate's OTHER refusal shape (unclassified new file) must not parse:
    # adding a file to the ship set is a ledger decision, never auto-repaired.
    assert parse_manifest_refusal(
        "no_human pre-commit gate: REFUSED.\n\n"
        "These staged file(s) are not classified in EXPORT_CLASSIFICATION.txt:\n"
        "  src/pkg/new_file.py\n"
    ) is None


def _repo_with_manifest_gate(repo_with_bare_remote):
    """Wire the fixture repo with a refusing pre-commit hook + a stub
    export_guard whose `approve` writes the manifest, satisfying the hook.

    The proactive `approve --all --prune` call (`approve_pending_pins`, run
    before every commit attempt) also lands on this stub now. It is a total
    no-op here — no ship-classification is configured in this fixture, so
    it finds nothing to pin and leaves both RELEASE_MANIFEST.txt and
    approve_called.txt untouched — which is exactly what keeps the four
    tests below asserting the REACTIVE path's behaviour unmodified."""
    work = repo_with_bare_remote
    (work / "scripts").mkdir()
    (work / "scripts" / "export_guard.py").write_text(
        "import pathlib, sys\n"
        "assert sys.argv[1] == 'approve'\n"
        "root = pathlib.Path(__file__).resolve().parent.parent\n"
        "(root / 'guard_calls.txt').open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if '--all' in sys.argv[2:]:\n"
        "    sys.exit(0)\n"
        "(root / 'RELEASE_MANIFEST.txt').write_text('\\n'.join(sys.argv[2:]) + '\\n')\n"
        "(root / 'approve_called.txt').write_text(' '.join(sys.argv[2:]))\n"
    )
    (work / "RELEASE_MANIFEST.txt").write_text("stale\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "gate fixture")
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        # The hook refuses until the stub's approve has rewritten the manifest.
        "if grep -q stale RELEASE_MANIFEST.txt; then\n"
        "cat >&2 <<'TXT'\n" + _REFUSAL + "TXT\n"
        "exit 1\n"
        "fi\n"
    )
    hook.chmod(0o755)
    return work


def test_commit_repairs_the_changed_pinned_refusal_and_retries_once(
        repo_with_bare_remote):
    from no_human.vcs import commit_with_manifest_repair
    work = _repo_with_manifest_gate(repo_with_bare_remote)
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/cc1", base="main")
    (work / "src").mkdir()
    (work / "src" / "mod.py").write_text("y = 2\n")
    repairs = []
    result = commit_with_manifest_repair(
        repo, ["src/mod.py"], "feat: change",
        on_repair=lambda p, note: repairs.append(p))
    assert result.sha
    # The ledger change is reported to the caller (never silent).
    assert repairs == [["src/pkg/mod.py", "tests/test_mod.py"]]
    # The gate's documented FIX ran with exactly the refused paths…
    assert (work / "approve_called.txt").read_text() == \
        "src/pkg/mod.py tests/test_mod.py"
    # …and the re-derived manifest is IN the commit (not left dirty).
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=work, capture_output=True, text=True, check=True).stdout
    assert "RELEASE_MANIFEST.txt" in committed


def test_commit_propagates_a_non_manifest_hook_failure(repo_with_bare_remote):
    from no_human.vcs import GitError, commit_with_manifest_repair
    work = repo_with_bare_remote
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint failed' >&2\nexit 1\n")
    hook.chmod(0o755)
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/cc2", base="main")
    (work / "z.py").write_text("z = 3\n")
    with pytest.raises(GitError, match="lint failed"):
        commit_with_manifest_repair(repo, ["z.py"], "feat: z")


def test_protected_branch_passes_through_the_repair_wrapper(
        repo_with_bare_remote):
    """ProtectedBranch is a GitError subclass, but a pipeline commit aimed at
    main is a wiring bug — it must propagate unchanged (no repair attempt,
    the caller re-raises it loudly instead of failing the attempt)."""
    from no_human.vcs import commit_with_manifest_repair
    work = repo_with_bare_remote
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y")
    (work / "p.py").write_text("p = 5\n")
    with pytest.raises(ProtectedBranch):
        commit_with_manifest_repair(repo, ["p.py"], "feat: p")


def test_a_second_refusal_after_repair_propagates(repo_with_bare_remote):
    """If approve runs but the hook still refuses, the retry's error must
    surface — one repair round, never a loop."""
    from no_human.vcs import GitError, commit_with_manifest_repair
    work = _repo_with_manifest_gate(repo_with_bare_remote)
    # Break the stub: approve succeeds but leaves the manifest stale.
    (work / "scripts" / "export_guard.py").write_text(
        "import sys\nassert sys.argv[1] == 'approve'\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "fixture", "--no-verify")
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/cc3", base="main")
    (work / "w.py").write_text("w = 4\n")
    with pytest.raises(GitError, match="REFUSED"):
        commit_with_manifest_repair(repo, ["w.py"], "feat: w")


# ---- proactive pin maintenance (task: export self-scan tests fail every -- #
# in-progress worktree; the real incident, task 27e7352b: a brand-new
# ship-classified file shipped with NO approval pin — the pre-commit gate
# above never sees it (it only guards already-pinned files), so nothing
# short of a proactive `approve --all --prune` before every commit attempt
# catches it in time to land in the SAME commit.

# scripts/export_guard.py's own real wording, reused verbatim so the stub's
# refusal text is not a paraphrase a real caller would never actually see.
_UNCLASSIFIED_REFUSAL = (
    "approve: REFUSED — not ship-classified (classify each in "
    "EXPORT_CLASSIFICATION.txt first):\n  {name}\n"
)

# Mirrors _cmd_approve's own three print lines exactly (scripts/export_guard.py):
#   print(f"approved  {digest[:12]}  {rel} ({state})")
#   print(f"pruned    {rel} (no longer ships)")
#   print(f"REFUSED   {rel} — {n} scan hit(s); review, then re-run ...")
_STUB_GUARD_ALL_SRC = """\
import hashlib, json, pathlib, subprocess, sys

root = pathlib.Path(__file__).resolve().parent.parent
argv = sys.argv[1:]
(root / "guard_calls.txt").open("a").write(" ".join(argv) + "\\n")
assert argv and argv[0] == "approve"
rest = argv[1:]

if "--all" not in rest:
    explicit = [a for a in rest if not a.startswith("--")]
    (root / "RELEASE_MANIFEST.txt").write_text("\\n".join(explicit) + "\\n")
    (root / "approve_called.txt").write_text(" ".join(explicit))
    sys.exit(0)

scenario_path = root / "_stub_scenario.json"
scenario = json.loads(scenario_path.read_text()) if scenario_path.exists() else {}

if scenario.get("unclassified"):
    sys.stderr.write(
        "approve: REFUSED \\u2014 not ship-classified (classify each in "
        "EXPORT_CLASSIFICATION.txt first):\\n  " + scenario["unclassified"] + "\\n")
    sys.exit(2)

# Ship-set membership comes ONLY from `git ls-files` (tracked), exactly like
# the real guard (scripts/export_guard.py:_tracked) — an untracked file is
# invisible here too, which is what proves the caller staged it first.
tracked = set(subprocess.run(
    ["git", "ls-files"], cwd=root, capture_output=True, text=True
).stdout.split())
ship = [p for p in scenario.get("ship", []) if p in tracked]

manifest_path = root / "RELEASE_MANIFEST.txt"
pins = {}
if manifest_path.exists():
    for line in manifest_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            pins[parts[1]] = parts[0]

refuse = set(scenario.get("refuse_scan", []))
approved = []
for rel in sorted(ship):
    digest = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    if pins.get(rel) == digest:
        continue
    if rel in refuse:
        print(f"REFUSED   {rel} \\u2014 1 scan hit(s); review, then re-run "
              "with --acknowledge if every hit is a false positive:")
        continue
    pins[rel] = digest
    approved.append(rel)
    print(f"approved  {digest[:12]}  {rel} (new)")

pruned = []
if "--prune" in rest:
    for stale in [p for p in list(pins) if p not in ship]:
        del pins[stale]
        pruned.append(stale)
        print(f"pruned    {stale} (no longer ships)")

if approved or pruned:
    rows = "".join(f"{d}  {p}\\n" for p, d in sorted(pins.items()))
    manifest_path.write_text(rows)

if refuse & set(ship):
    sys.exit(1)
sys.exit(0)
"""

# A pre-commit hook that mirrors scripts/precommit_manifest_gate.py's own
# narrow check (staged pinned file vs staged pin), not the crude
# grep-for-"stale" hook above — this fixture needs the REAL distinction
# between "a pin genuinely matches" and "it doesn't", which the crude hook
# can't make (it would refuse or pass regardless of what actually changed).
_HOOK_CHECK_SRC = """\
import hashlib, subprocess, sys

def staged_blob(rel):
    r = subprocess.run(["git", "cat-file", "blob", ":" + rel], capture_output=True)
    return r.stdout if r.returncode == 0 else None

blob = staged_blob("RELEASE_MANIFEST.txt")
pins = {}
if blob:
    for line in blob.decode().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            pins[parts[1]] = parts[0]

staged = subprocess.run(
    ["git", "diff", "--cached", "--name-only", "--diff-filter=AM"],
    capture_output=True, text=True,
).stdout.split()

offenders = []
for rel in staged:
    if rel in pins:
        b = staged_blob(rel)
        if b is None:
            continue
        actual = hashlib.sha256(b).hexdigest()
        if actual != pins[rel]:
            offenders.append((rel, pins[rel], actual))

if offenders:
    sys.stderr.write("no_human pre-commit gate: REFUSED.\\n\\n")
    sys.stderr.write(
        "These staged file(s) are pinned in RELEASE_MANIFEST.txt, but their staged content does\\n"
        "not match their pin \\u2014 the file changed and its manifest row did not,\\n"
        "so this commit would ship a file the export ledger has not approved:\\n")
    for rel, pinned, actual in offenders:
        sys.stderr.write(f"  {rel}\\n")
        sys.stderr.write(f"      pinned {pinned[:12]}\\u2026  staged {actual[:12]}\\u2026\\n")
    sys.exit(1)
sys.exit(0)
"""


def _repo_with_realistic_manifest_gate(
    repo_with_bare_remote, *, ship=None, refuse_scan=None, unclassified=None,
):
    """A stub `export_guard.py` that supports BOTH invocation shapes
    (`approve <paths>` reactive, `approve --all --prune` proactive) plus a
    pre-commit hook that only refuses a genuine staged-pin mismatch."""
    work = repo_with_bare_remote
    (work / "scripts").mkdir()
    (work / "scripts" / "export_guard.py").write_text(_STUB_GUARD_ALL_SRC)
    (work / "RELEASE_MANIFEST.txt").write_text("")
    scenario = {}
    if ship is not None:
        scenario["ship"] = list(ship)
    if refuse_scan is not None:
        scenario["refuse_scan"] = list(refuse_scan)
    if unclassified is not None:
        scenario["unclassified"] = unclassified
    (work / "_stub_scenario.json").write_text(json.dumps(scenario))
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "gate fixture")
    hook_py = work / ".git" / "hooks" / "_check_manifest.py"
    hook_py.write_text(_HOOK_CHECK_SRC)
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text('#!/bin/sh\nexec python3 "$(dirname "$0")/_check_manifest.py"\n')
    hook.chmod(0o755)
    return work


def test_a_changed_pinned_file_is_re_pinned_in_the_commit_itself(
        repo_with_bare_remote):
    """The real incident's sibling shape: an already-pinned file the coder
    edits must be re-pinned IN the coder's own commit — proactively, never
    by falling back to the reactive repair (which would mean the pre-commit
    gate had to refuse first)."""
    from no_human.vcs import commit_with_manifest_repair
    work = _repo_with_realistic_manifest_gate(
        repo_with_bare_remote, ship=["src/pkg/mod.py"])
    (work / "src" / "pkg").mkdir(parents=True)
    (work / "src" / "pkg" / "mod.py").write_text("y = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "add mod")
    old_digest = hashlib.sha256(b"y = 1\n").hexdigest()
    (work / "RELEASE_MANIFEST.txt").write_text(f"{old_digest}  src/pkg/mod.py\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "pin mod")

    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/rt1", base="main")
    (work / "src" / "pkg" / "mod.py").write_text("y = 2\n")
    repairs = []
    result = commit_with_manifest_repair(
        repo, ["src/pkg/mod.py"], "feat: change",
        on_repair=lambda p, note: repairs.append(p))

    assert result.sha
    assert repairs == [["src/pkg/mod.py"]]
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=work, capture_output=True, text=True, check=True).stdout
    assert "RELEASE_MANIFEST.txt" in committed
    manifest_at_head = subprocess.run(
        ["git", "show", "HEAD:RELEASE_MANIFEST.txt"],
        cwd=work, capture_output=True, text=True, check=True).stdout
    new_digest = hashlib.sha256(b"y = 2\n").hexdigest()
    assert f"{new_digest}  src/pkg/mod.py" in manifest_at_head
    # The reactive path never ran — the proactive pass already satisfied the
    # pre-commit gate before `git commit` was even attempted.
    assert not (work / "approve_called.txt").exists()


def test_a_new_ship_classified_file_is_pinned_before_the_commit(
        repo_with_bare_remote):
    """The exact tests/test_pr_body_layout.py incident (task 27e7352b): a
    brand-new ship-classified file must ship WITH a pin, in the same commit,
    with no pre-commit refusal ever involved (a new file is not this gate's
    job — check scripts/precommit_manifest_gate.py's own docstring)."""
    from no_human.vcs import commit_with_manifest_repair
    work = _repo_with_realistic_manifest_gate(
        repo_with_bare_remote, ship=["src/new_thing.py"])
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/rt2", base="main")
    (work / "src").mkdir()
    (work / "src" / "new_thing.py").write_text("NEW = True\n")

    result = commit_with_manifest_repair(repo, ["src/new_thing.py"], "feat: new thing")

    assert result.sha
    manifest_at_head = subprocess.run(
        ["git", "show", "HEAD:RELEASE_MANIFEST.txt"],
        cwd=work, capture_output=True, text=True, check=True).stdout
    digest = hashlib.sha256(b"NEW = True\n").hexdigest()
    assert f"{digest}  src/new_thing.py" in manifest_at_head


def test_a_partial_approval_on_refusal_still_reaches_on_repair(
        repo_with_bare_remote):
    """A single `--all` run can approve some files and refuse others in the
    SAME invocation (the real guard writes the manifest for whatever
    approved cleanly, then returns 1 for whatever it refused). That write
    must never be silent: on_repair must fire for the file(s) that were
    actually pinned, even though the run overall exits non-zero."""
    from no_human.vcs import commit_with_manifest_repair
    work = _repo_with_realistic_manifest_gate(
        repo_with_bare_remote, ship=["clean.py", "leaky.py"],
        refuse_scan=["leaky.py"])
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/rt8", base="main")
    (work / "clean.py").write_text("CLEAN = 1\n")
    (work / "leaky.py").write_text("SECRET = 1\n")
    repairs = []

    result = commit_with_manifest_repair(
        repo, ["clean.py", "leaky.py"], "feat: mixed",
        on_repair=lambda p, note: repairs.append(p))

    assert result.sha
    manifest_at_head = subprocess.run(
        ["git", "show", "HEAD:RELEASE_MANIFEST.txt"],
        cwd=work, capture_output=True, text=True, check=True).stdout
    clean_digest = hashlib.sha256(b"CLEAN = 1\n").hexdigest()
    assert f"{clean_digest}  clean.py" in manifest_at_head
    assert "leaky.py" not in manifest_at_head
    # The partial write is reported, never silent (review finding: a prior
    # version wrote the manifest on a non-zero exit without ever calling
    # on_repair for what it actually approved).
    assert repairs == [["clean.py"]]


def test_a_scan_refusal_never_becomes_an_approval(repo_with_bare_remote, caplog):
    """A planted leak/mismatch must still be refused — the proactive pass
    approving OTHER files in the same run must never launder this one in."""
    from no_human.vcs import commit_with_manifest_repair
    work = _repo_with_realistic_manifest_gate(
        repo_with_bare_remote, ship=["x.py"], refuse_scan=["x.py"])
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/rt3", base="main")
    (work / "x.py").write_text("SECRET = 1\n")
    repairs = []

    with caplog.at_level("WARNING"):
        result = commit_with_manifest_repair(
            repo, ["x.py"], "feat: x",
            on_repair=lambda p, note: repairs.append((p, note)))

    assert result.sha
    manifest_at_head = subprocess.run(
        ["git", "show", "HEAD:RELEASE_MANIFEST.txt"],
        cwd=work, capture_output=True, text=True, check=True).stdout
    assert "x.py" not in manifest_at_head
    assert repairs == []  # nothing was actually approved -> no ledger entry
    assert any("scan hit" in r.message for r in caplog.records)


def test_an_unclassified_file_refusal_is_reported_and_never_repaired(
        repo_with_bare_remote, caplog):
    """An unclassified refusal must never be silently patched over — the
    manifest stays exactly as it was, and the FIX text reaches the log."""
    from no_human.vcs import commit_with_manifest_repair
    work = _repo_with_realistic_manifest_gate(
        repo_with_bare_remote, unclassified="bogus.py")
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/rt4", base="main")
    (work / "bogus.py").write_text("z = 1\n")
    manifest_before = (work / "RELEASE_MANIFEST.txt").read_text()
    repairs = []

    with caplog.at_level("WARNING"):
        result = commit_with_manifest_repair(
            repo, ["bogus.py"], "feat: bogus",
            on_repair=lambda p, note: repairs.append((p, note)))

    assert result.sha
    manifest_at_head = subprocess.run(
        ["git", "show", "HEAD:RELEASE_MANIFEST.txt"],
        cwd=work, capture_output=True, text=True, check=True).stdout
    assert manifest_at_head == manifest_before
    assert repairs == []
    assert any("not ship-classified" in r.message for r in caplog.records)


def test_approve_is_never_invoked_with_acknowledge(repo_with_bare_remote):
    from no_human.vcs import commit_with_manifest_repair
    work = _repo_with_realistic_manifest_gate(repo_with_bare_remote, ship=["a.py"])
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/rt5", base="main")
    (work / "a.py").write_text("a = 1\n")

    commit_with_manifest_repair(repo, ["a.py"], "feat: a")

    calls = (work / "guard_calls.txt").read_text()
    assert "--acknowledge" not in calls


def test_a_repo_without_the_export_guard_spawns_nothing(repo_with_bare_remote):
    from no_human.vcs import commit_with_manifest_repair
    work = repo_with_bare_remote
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/rt6", base="main")
    (work / "q.py").write_text("q = 1\n")

    result = commit_with_manifest_repair(repo, ["q.py"], "feat: q")

    assert result.sha
    assert not (work / "guard_calls.txt").exists()


# ---- public-tree repair route: check_release_manifest.py --write (dogfood
# ---- incident, this repo's own tree has no scripts/export_guard.py) -------

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Prefix shared by both "exploding" check_release_manifest.py stubs below: it
# still imports cleanly (scripts/precommit_manifest_gate.py loads
# parse_manifest/MANIFEST_NAME/CLASSIFICATION_NAME from it on every commit,
# including the FIRST one that must refuse normally) but only misbehaves when
# actually RUN as `--write`, i.e. under `__main__`.
_EXPLODING_STUB_PREFIX = """\
import importlib.util, sys
from pathlib import Path

_real_path = Path(__file__).resolve().parent / "_real_check_release_manifest.py"
_spec = importlib.util.spec_from_file_location(
    "_nh_real_check_release_manifest", _real_path)
_real = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _real
_spec.loader.exec_module(_real)

parse_manifest = _real.parse_manifest
MANIFEST_NAME = _real.MANIFEST_NAME
CLASSIFICATION_NAME = _real.CLASSIFICATION_NAME

"""


def _swap_in_exploding_check_release_manifest(work, main_body):
    """Move the REAL check_release_manifest.py aside and replace it with a
    stub whose `--write` misbehaves per *main_body*, while still importing
    cleanly for the gate's own use of its manifest grammar."""
    real = work / "scripts" / "check_release_manifest.py"
    stub = work / "scripts" / "_real_check_release_manifest.py"
    real.rename(stub)
    (work / "scripts" / "check_release_manifest.py").write_text(
        _EXPLODING_STUB_PREFIX + main_body)


def _repo_with_real_public_manifest_gate(repo_with_bare_remote):
    """Wire the fixture repo to the REAL public-tree gate scripts copied from
    THIS tree (which has no scripts/export_guard.py / EXPORT_CLASSIFICATION.txt
    itself — the exact dogfood shape), via `core.hooksPath` so the hook is
    really installed rather than hand-rolled."""
    work = repo_with_bare_remote
    (work / "scripts" / "hooks").mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "scripts" / "check_release_manifest.py",
                work / "scripts" / "check_release_manifest.py")
    shutil.copy(_REPO_ROOT / "scripts" / "precommit_manifest_gate.py",
                work / "scripts" / "precommit_manifest_gate.py")
    shutil.copy(_REPO_ROOT / "scripts" / "hooks" / "pre-commit",
                work / "scripts" / "hooks" / "pre-commit")
    (work / "scripts" / "hooks" / "pre-commit").chmod(0o755)
    _git(work, "config", "core.hooksPath", str(work / "scripts" / "hooks"))

    (work / "src" / "pkg").mkdir(parents=True)
    (work / "src" / "pkg" / "mod.py").write_text("y = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "add mod")

    subprocess.run(
        [sys.executable, str(work / "scripts" / "check_release_manifest.py"),
         "--write"],
        cwd=work, check=True, capture_output=True, text=True)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "pin mod", "--no-verify")

    assert not (work / "scripts" / "export_guard.py").exists()
    assert not (work / "EXPORT_CLASSIFICATION.txt").exists()
    return work


def test_public_tree_refusal_is_repaired_with_check_release_manifest_write(
        repo_with_bare_remote):
    """The dogfood incident itself: no export_guard.py, so the reactive
    repair must fall back to `check_release_manifest.py --write` and stage
    RELEASE_MANIFEST.txt in the SAME retried commit."""
    from no_human.vcs import commit_with_manifest_repair
    work = _repo_with_real_public_manifest_gate(repo_with_bare_remote)
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/pub1", base="main")
    (work / "src" / "pkg" / "mod.py").write_text("y = 2\n")
    repairs = []
    notes = []

    result = commit_with_manifest_repair(
        repo, ["src/pkg/mod.py"], "feat: change",
        on_repair=lambda p, note: (repairs.append(p), notes.append(note)))

    assert result.sha
    assert repairs == [["src/pkg/mod.py"]]
    assert notes and notes[0]
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=work, capture_output=True, text=True, check=True).stdout
    assert "src/pkg/mod.py" in committed
    assert "RELEASE_MANIFEST.txt" in committed
    manifest_at_head = subprocess.run(
        ["git", "show", "HEAD:RELEASE_MANIFEST.txt"],
        cwd=work, capture_output=True, text=True, check=True).stdout
    new_digest = hashlib.sha256(b"y = 2\n").hexdigest()
    assert f"{new_digest}  src/pkg/mod.py" in manifest_at_head


def test_the_real_pre_commit_gate_refuses_the_control_commit(
        repo_with_bare_remote):
    """Proves the real gate was actually installed and would have refused
    the plain commit `commit_with_manifest_repair` above had to repair —
    without this, the passing test above could just mean nothing ever ran."""
    from no_human.vcs import GitError, parse_manifest_refusal
    work = _repo_with_real_public_manifest_gate(repo_with_bare_remote)
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/pub2", base="main")
    (work / "src" / "pkg" / "mod.py").write_text("y = 2\n")

    with pytest.raises(GitError, match="REFUSED") as excinfo:
        repo.commit_paths(["src/pkg/mod.py"], "feat: change")

    assert parse_manifest_refusal(str(excinfo.value)) == ["src/pkg/mod.py"]


def test_export_classification_keeps_the_export_guard_route(
        repo_with_bare_remote):
    """EXPORT_CLASSIFICATION.txt present (private-tree shape) but no
    export_guard.py installed: the new `--write` route must NOT fire — it
    would forge approvals with no term scan — so the original refusal must
    propagate untouched."""
    from no_human.vcs import GitError, commit_with_manifest_repair
    work = _repo_with_real_public_manifest_gate(repo_with_bare_remote)
    (work / "EXPORT_CLASSIFICATION.txt").write_text("ship: **\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "classify", "--no-verify")
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/pub3", base="main")
    (work / "src" / "pkg" / "mod.py").write_text("y = 2\n")
    manifest_before = (work / "RELEASE_MANIFEST.txt").read_text()
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True).stdout.strip()
    repairs = []

    with pytest.raises(GitError, match="REFUSED"):
        commit_with_manifest_repair(
            repo, ["src/pkg/mod.py"], "feat: change",
            on_repair=lambda p, note: repairs.append(p))

    assert repairs == []
    assert (work / "RELEASE_MANIFEST.txt").read_text() == manifest_before
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True).stdout.strip()
    assert head_after == head_before


def test_a_failing_manifest_write_raises_with_the_tool_stderr(
        repo_with_bare_remote):
    """A `--write` that exits non-zero must raise GitError with the tool's
    own stderr embedded — never a swallowed or generic failure."""
    from no_human.vcs import GitError, commit_with_manifest_repair, is_gate_refusal
    work = _repo_with_real_public_manifest_gate(repo_with_bare_remote)
    _swap_in_exploding_check_release_manifest(
        work,
        'if __name__ == "__main__":\n'
        '    sys.stderr.write("check_release_manifest: exploded on purpose\\n")\n'
        '    sys.exit(2)\n')
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/pub4", base="main")
    (work / "src" / "pkg" / "mod.py").write_text("y = 2\n")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True).stdout.strip()

    with pytest.raises(GitError, match="check_release_manifest: exploded on purpose") \
            as excinfo:
        commit_with_manifest_repair(repo, ["src/pkg/mod.py"], "feat: change")

    assert "manifest re-approve failed" in str(excinfo.value)
    assert is_gate_refusal(str(excinfo.value))
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True).stdout.strip()
    assert head_after == head_before


def test_a_hanging_manifest_write_times_out_into_a_git_error(
        repo_with_bare_remote, monkeypatch):
    """A `--write` that hangs must be bounded by the same timeout ceiling as
    the existing approve path, and its timeout must still classify as a gate
    refusal (the checkpoint bypass seam depends on that)."""
    from no_human.vcs import (
        GitError,
        commit_with_manifest_repair,
        is_gate_refusal,
        manifest_repair,
    )
    work = _repo_with_real_public_manifest_gate(repo_with_bare_remote)
    _swap_in_exploding_check_release_manifest(
        work,
        'import time\n'
        'if __name__ == "__main__":\n'
        '    time.sleep(30)\n')
    monkeypatch.setattr(manifest_repair, "_MANIFEST_WRITE_TIMEOUT_S", 1)
    repo = GitRepo(work, identity_name="agent", identity_email="a@x.y",
                   never_push_to=[])
    repo.create_branch("no-human/pub5", base="main")
    (work / "src" / "pkg" / "mod.py").write_text("y = 2\n")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True).stdout.strip()

    with pytest.raises(GitError, match="timed out") as excinfo:
        commit_with_manifest_repair(repo, ["src/pkg/mod.py"], "feat: change")

    assert is_gate_refusal(str(excinfo.value))
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work,
        capture_output=True, text=True, check=True).stdout.strip()
    assert head_after == head_before


# --- lock-contention retry (main-6cec2140 booked two specs `crashed` on a
# --- `git add` and a `git checkout -B` that failed on briefly-held locks) ----


def _lock_proc(cmd):
    return subprocess.CompletedProcess(
        cmd, 128, stdout="",
        stderr="fatal: Unable to create '/w/.git/index.lock': File exists.")


def test_run_retries_lock_contention_then_succeeds(repo_with_bare_remote,
                                                   monkeypatch):
    """A lock-contention failure is another process holding the lock for a
    moment — one retried call must absorb it instead of crashing the task."""
    from no_human.vcs import git as git_mod
    monkeypatch.setattr(git_mod, "_GIT_RETRY_BACKOFFS_S", (0.0, 0.0))
    repo = GitRepo(repo_with_bare_remote, identity_name="a",
                   identity_email="a@x.y")
    calls = []
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            return _lock_proc(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(git_mod.subprocess, "run", fake_run)
    assert repo._run("rev-parse", "--verify", "HEAD")
    assert len(calls) == 2


def test_run_does_not_retry_a_genuine_failure(repo_with_bare_remote,
                                              monkeypatch):
    """Only the lock-contention class retries: a real failure (bad ref,
    conflict) raises on the FIRST attempt — retrying it would just repeat and
    mask it."""
    from no_human.vcs import git as git_mod
    from no_human.vcs.git import GitError
    monkeypatch.setattr(git_mod, "_GIT_RETRY_BACKOFFS_S", (0.0, 0.0))
    repo = GitRepo(repo_with_bare_remote, identity_name="a",
                   identity_email="a@x.y")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 128, stdout="", stderr="fatal: bad revision 'nope'")

    monkeypatch.setattr(git_mod.subprocess, "run", fake_run)
    with pytest.raises(GitError):
        repo._run("rev-parse", "--verify", "nope")
    assert len(calls) == 1


def test_run_gives_up_after_bounded_lock_retries(repo_with_bare_remote,
                                                 monkeypatch):
    """A PERSISTENTLY held lock still fails, after exactly the bounded number
    of retries — the retry absorbs a blip, it does not wait out a wedged
    process forever."""
    from no_human.vcs import git as git_mod
    from no_human.vcs.git import GitError
    monkeypatch.setattr(git_mod, "_GIT_RETRY_BACKOFFS_S", (0.0, 0.0))
    repo = GitRepo(repo_with_bare_remote, identity_name="a",
                   identity_email="a@x.y")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _lock_proc(cmd)

    monkeypatch.setattr(git_mod.subprocess, "run", fake_run)
    with pytest.raises(GitError, match="index.lock"):
        repo._run("add", "--", "app.py")
    assert len(calls) == 3          # first attempt + the two bounded retries


def test_bench_git_helper_shares_the_lock_retry(tmp_path, monkeypatch):
    """The bench sandbox's own `_git` (eval/northstar.py) absorbs the same
    contention class — that call site is where the two crashed specs died."""
    from no_human.eval import northstar as ns
    from no_human.vcs import git as git_mod
    monkeypatch.setattr(git_mod, "_GIT_RETRY_BACKOFFS_S", (0.0, 0.0))
    calls = []
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            return _lock_proc(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(ns.subprocess, "run", fake_run)
    work = tmp_path / "w"
    work.mkdir()
    ns._git(work, "init", "-b", "main")
    assert len(calls) == 2
    assert (work / ".git").exists()


def test_shipped_git_retry_backoffs_are_two_positive_delays():
    """Every retry test above zeroes _GIT_RETRY_BACKOFFS_S for speed, so
    nothing else pins the value that actually ships: setting it to () would
    silently disable lock retrying entirely and the rest of the suite would
    stay green."""
    from no_human.vcs.git import _GIT_RETRY_BACKOFFS_S
    assert len(_GIT_RETRY_BACKOFFS_S) == 2
    assert all(backoff > 0 for backoff in _GIT_RETRY_BACKOFFS_S)


# --------------------------------------------------------------------------
# RETRY BASE STALENESS: a retry that reuses its branch meets whatever base it
# was cut from, forever, unless something measures the gap and rebases past a
# threshold. Reproduces `db9da7f7` (5 behind, attempt 2 burned 6.9M tokens
# re-fixing a test main had already deleted) and `e6719bd8` (140 behind, 6
# attempts, 37.9M tokens).
# --------------------------------------------------------------------------

def _stale_branch(work, n=5):
    """Branch off `main`, commit on the branch, then advance `main` `n`
    times — the branch's own commit is still there, but it now trails `main`
    by exactly `n` commits it never saw."""
    repo = GitRepo(work, identity_name="no_human", identity_email="a@b.invalid")
    repo.create_branch("no-human/t1")
    (work / "feature.py").write_text("y = 1\n")
    repo.commit_all("the work")
    _git(work, "checkout", "main")
    for i in range(n):
        (work / f"main-advance-{i}.py").write_text(f"m = {i}\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", f"main advances {i}")
    _git(work, "checkout", "no-human/t1")
    return repo


def test_commits_behind_measures_the_gap(repo_with_bare_remote):
    repo = _stale_branch(repo_with_bare_remote, n=5)
    assert repo.commits_behind("main") == 5


def test_commits_behind_is_zero_at_base(repo_with_bare_remote):
    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                    identity_email="a@b.invalid")
    repo.create_branch("no-human/t1")
    (repo_with_bare_remote / "feature.py").write_text("y = 1\n")
    repo.commit_all("the work")
    assert repo.commits_behind("main") == 0


def test_rebase_onto_replays_the_branch_and_returns_true(repo_with_bare_remote):
    repo = _stale_branch(repo_with_bare_remote, n=5)
    before = repo.head_sha()
    assert repo.rebase_onto("main") is True
    assert repo.head_sha() != before
    assert repo.commits_behind("main") == 0
    assert (repo_with_bare_remote / "feature.py").exists()
    assert (repo_with_bare_remote / "main-advance-4.py").exists()


def test_rebase_onto_a_branch_already_at_base_is_a_no_op(repo_with_bare_remote):
    """Negative control: nothing to rebase means no rebase happens, and HEAD
    does not move."""
    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                    identity_email="a@b.invalid")
    repo.create_branch("no-human/t1")
    (repo_with_bare_remote / "feature.py").write_text("y = 1\n")
    repo.commit_all("the work")
    before = repo.head_sha()
    assert repo.rebase_onto("main") is False
    assert repo.head_sha() == before


def test_rebase_onto_a_conflict_aborts_and_leaves_the_branch_usable(repo_with_bare_remote):
    """A CONFLICTING rebase must not fail the attempt: abort, report False,
    and leave a clean, committable tree — no rebase left in progress."""
    repo = GitRepo(repo_with_bare_remote, identity_name="no_human",
                    identity_email="a@b.invalid")
    repo.create_branch("no-human/t1")
    (repo_with_bare_remote / "shared.py").write_text("branch = 1\n")
    repo.commit_all("branch edits shared.py")
    before = repo.head_sha()

    _git(repo_with_bare_remote, "checkout", "main")
    (repo_with_bare_remote / "shared.py").write_text("main = 1\n")
    _git(repo_with_bare_remote, "add", "-A")
    _git(repo_with_bare_remote, "commit", "-m", "main edits shared.py too")
    _git(repo_with_bare_remote, "checkout", "no-human/t1")

    assert repo.rebase_onto("main") is False
    assert repo.head_sha() == before, "a conflicting rebase must not move HEAD"
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=repo_with_bare_remote,
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == "", f"a conflict/rebase must not be left in progress: {status!r}"
    # Proceeding un-rebased must still be possible — the tree is usable.
    (repo_with_bare_remote / "unrelated.py").write_text("z = 1\n")
    repo.commit_all("more work after the aborted rebase")


def test_a_rebased_branch_reads_diverged_not_behind_against_its_own_remote(
        repo_with_bare_remote):
    """The mechanism the whole fix leans on: after rebasing an already-PUSHED
    branch onto a moved base, `remote_branch_relation` reports "diverged"
    (never "behind") against its own prior remote tip — the classification
    that routes delivery to --force-with-lease instead of raising
    PushBehindRemote."""
    repo = _stale_branch(repo_with_bare_remote, n=5)
    repo.push("no-human/t1")
    assert repo.remote_branch_relation("no-human/t1") == "up_to_date"
    assert repo.rebase_onto("main") is True
    assert repo.remote_branch_relation("no-human/t1") == "diverged"
