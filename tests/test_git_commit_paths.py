"""Regression coverage for the Bash-created-non-code-file blind spot.

`commit_paths` used to stage untracked files only when their extension was
in `_CODE_EXTS` — so a file a Bash command wrote (a generated report, a
Dockerfile) with no code extension was silently never staged, and the
`uncommitted_source_files` guard that exists to catch exactly this shared
the same two predicates (`_CODE_EXTS` + edit-hook `coder_touched`), so it
never flagged it either. See `src/no_human/vcs/git.py`'s `commit_paths` and
`uncommitted_source_files` docstrings for the fix and its discriminator.
"""

import ast
import os
import subprocess
from pathlib import Path

import pytest

from no_human.vcs import GitError, GitRepo


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


def _committed_files(repo_path):
    return subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    ).stdout


def _committed_names(repo_path):
    """Every path HEAD's commit touched, NUL-split so a leading/trailing
    space in a filename (which `-z` preserves but is not C-quoted) is not
    lost the way a whitespace-`.strip()`ed read would lose it."""
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", "-z", "HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    ).stdout
    return {n for n in out.split("\0") if n}


def test_bash_created_md_beside_an_edit_created_py_is_committed(repo_with_bare_remote):
    """Verbatim repro from the escalated task (aaf752ad, 18.3M tokens over 2
    attempts): an edit-tool-created harness.py explicitly passed to
    commit_paths, plus a Bash-shaped REPORT.md written straight to disk
    (never in the paths list) beside it in a brand-new directory. Both must
    land in the commit."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/impossible-bench", base="main")
    bench = repo.path / "eval" / "newtool"
    bench.mkdir(parents=True)
    (bench / "harness.py").write_text("print('ran')\n")           # edit-tool-tracked
    (bench / "REPORT.md").write_text("| class | caught |\n")     # Bash-created, non-code
    repo.commit_paths([str(bench / "harness.py")], "add the eval harness")
    files = _committed_files(repo.path)
    assert "eval/newtool/harness.py" in files
    assert "eval/newtool/REPORT.md" in files


def test_side_effect_json_in_existing_dir_is_not_committed_and_not_flagged(repo_with_bare_remote):
    """NEGATIVE CONTROL — must not regress. A genuine test side-effect
    (alert-state.json written into a directory that already existed before
    this commit) must stay uncommitted AND unflagged by the guard, even
    though its directory now also holds a file the commit IS staging."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/side-effect", base="main")
    teams = repo.path / "teams" / "test"
    teams.mkdir(parents=True)
    (teams / "existing.py").write_text("x = 1\n")
    repo.commit_all("prior: add teams/test dir")
    # Now, in a later commit, the coder edits existing.py and the test suite
    # (run as a side-effect of verification) drops alert-state.json next to it.
    (teams / "existing.py").write_text("x = 2\n")
    (teams / "alert-state.json").write_text('{"state": "updated"}')
    repo.commit_paths([str(teams / "existing.py")], "PROJ-1: tweak existing.py")
    files = _committed_files(repo.path)
    assert "teams/test/existing.py" in files
    assert "alert-state.json" not in files
    leftover = repo.uncommitted_source_files(coder_touched={"teams/test/existing.py"})
    assert "teams/test/alert-state.json" not in leftover


def test_side_effect_json_in_a_new_dir_with_no_staged_sibling_is_not_committed(repo_with_bare_remote):
    """Second half of the discriminator: a new directory alone is not
    enough — the commit must also be staging something else into it, or the
    lone file is presumed a side-effect (mirrors the pinned
    test_commit_paths_excludes_untracked_json_side_effects in test_vcs.py)."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/lone-side-effect", base="main")
    (repo.path / "feature.py").write_text("y = 2\n")
    data = repo.path / "data"
    data.mkdir()
    (data / "state.json").write_text('{"updated": true}')
    repo.commit_paths([str(repo.path / "feature.py")], "PROJ-1: add feature")
    files = _committed_files(repo.path)
    assert "feature.py" in files
    assert "state.json" not in files


def test_extensionless_deliverable_in_a_new_dir_is_committed(repo_with_bare_remote):
    """Suffix-agnostic coverage (AC-6): extensionless deliverables
    (Dockerfile, Makefile) in a brand-new directory land alongside a staged
    sibling, even though neither carries an extension in _CODE_EXTS."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/deploy", base="main")
    deploy = repo.path / "deploy"
    deploy.mkdir()
    (deploy / "entrypoint.sh").write_text("#!/bin/sh\necho hi\n")
    (deploy / "Dockerfile").write_text("FROM scratch\n")
    (deploy / "Makefile").write_text("build:\n\techo building\n")
    repo.commit_paths([str(deploy / "entrypoint.sh")], "add deploy scaffolding")
    files = _committed_files(repo.path)
    assert "deploy/entrypoint.sh" in files
    assert "deploy/Dockerfile" in files
    assert "deploy/Makefile" in files


def test_guard_flags_a_leftover_in_a_directory_the_commit_created(repo_with_bare_remote):
    """The guard's third predicate, exercised directly, WITHOUT coder_touched
    and without a _CODE_EXTS-matching suffix — proving it no longer depends
    on either of the two predicates commit_paths itself uses."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/guard-new-dir", base="main")
    x = repo.path / "eval" / "x"
    x.mkdir(parents=True)
    (x / "harness.py").write_text("print('ran')\n")
    (x / "REPORT.md").write_text("| class | caught |\n")
    # Hand-stage only harness.py, bypassing commit_paths entirely — simulates
    # a partial commit made through some other path.
    subprocess.run(["git", "add", "eval/x/harness.py"], cwd=repo.path,
                    check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "partial"], cwd=repo.path,
                    check=True, capture_output=True)
    leftover = repo.uncommitted_source_files(coder_touched=None)
    assert "eval/x/REPORT.md" in leftover


def test_unborn_repo_degrades_to_old_behaviour_without_crashing(tmp_path):
    """No HEAD at all yet (truly unborn repo — `current_branch()` itself
    requires a commit to exist, a pre-existing constraint, so the only way
    to reach this path is to call the new helpers directly, as
    `commit_paths`/`uncommitted_source_files` do internally before any
    commit exists on the branch). Both helpers must degrade to the old
    conservative (exclude / not-new) behaviour rather than raise."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    repo = GitRepo(work)
    assert repo._dir_absent_from_tree("eval/newtool", "HEAD") is False
    assert repo._dirs_newly_added_by_head() == set()


def test_root_commit_with_no_head_caret_still_flags_a_leftover(repo_with_bare_remote):
    """The guard's root-commit branch (no HEAD^ to diff against): when HEAD
    IS the repo's first-ever commit, every directory it touched counts as
    newly added, so a leftover left behind in that same directory (by a
    hand-crafted commit bypassing commit_paths, mirroring
    test_guard_flags_a_leftover_in_a_directory_the_commit_created) is still
    caught rather than silently passed through."""
    bare = repo_with_bare_remote.parent / "root.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                    capture_output=True)
    work = repo_with_bare_remote.parent / "root_work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    x = work / "eval" / "x"
    x.mkdir(parents=True)
    (x / "harness.py").write_text("print('ran')\n")
    (x / "REPORT.md").write_text("| class | caught |\n")
    _git(work, "add", "eval/x/harness.py")
    _git(work, "commit", "-m", "first commit ever, only harness.py")
    repo = GitRepo(work)
    leftover = repo.uncommitted_source_files()
    assert "eval/x/REPORT.md" in leftover


def test_a_created_then_deleted_path_no_longer_kills_the_commit(repo_with_bare_remote):
    """Pre-fix: `git add -- app.py ghost.py` exits 128 (ghost.py is neither
    on disk nor in the index — the coder created it and then deleted it
    again within this attempt) and `commit_paths` raised GitError, killing
    the WHOLE commit including the real app.py edit. Post-fix, app.py must
    still land and ghost.py must simply be dropped, not raise."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/phantom-path", base="main")
    app = repo.path / "app.py"
    app.write_text("x = 2\n")
    ghost = repo.path / "ghost.py"  # never created on disk in this branch
    repo.commit_paths([str(app), str(ghost)], "edit app.py; ghost.py never existed")
    files = _committed_files(repo.path)
    assert "app.py" in files
    assert "ghost.py" not in files

    # Positive control: when ghost.py DOES exist on disk, both land — proving
    # the fix drops only genuinely-absent paths, not the whole batch.
    repo.create_branch("no-human/phantom-path-control", base="main")
    app.write_text("x = 3\n")
    ghost.write_text("y = 1\n")
    repo.commit_paths([str(app), str(ghost)], "edit app.py; ghost.py exists this time")
    files = _committed_files(repo.path)
    assert "app.py" in files
    assert "ghost.py" in files


def test_a_leading_space_tracked_edit_is_not_dropped_by_a_whole_output_strip(repo_with_bare_remote):
    """`_run`'s `proc.stdout.strip()` is a whole-output strip: it eats the
    leading space off the FIRST path in a `-z` git call's output (a
    filename may legitimately begin with a space), even though `-z`
    itself is present and NUL survives the strip fine. `" lead.py"` sorts
    before `"app.py"` (space < 'a'), so it lands first in `git diff
    --name-only -z`'s output — exactly where the bug bites. The `-z`
    producers in `commit_paths` must route through `_run_null` (which
    never strips), not `_run`, or this tracked edit is silently dropped:
    `"lead.py"` (space stripped) does not exist on disk, the phantom
    filter misclassifies it as missing, the `ls-files` lookup can't match
    the mangled name either, and it is dropped as a phantom."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/leading-space", base="main")
    lead = repo.path / " lead.py"
    lead.write_text("a = 1\n")
    _git(repo.path, "add", "-A")
    _git(repo.path, "commit", "-m", "add lead.py")

    lead.write_text("a = 2\n")
    app = repo.path / "app.py"
    app.write_text("x = 2\n")
    repo.commit_paths([], "edit both tracked files")

    names = _committed_names(repo.path)
    assert " lead.py" in names
    assert "app.py" in names


def test_a_tracked_deletion_is_still_staged_as_a_deletion(repo_with_bare_remote):
    """A TRACKED path that was deleted must still be staged as a deletion —
    it must not share ghost.py's fate just because both are absent from
    disk. The `ls-files -z --` lookup is what tells the two apart.

    Co-batched with a real edit (`app.py`, already tracked from the
    fixture's init commit) so `rel_paths` can never end up empty and
    `commit_paths`' own `if not staged: stage_all()` fallback can never
    fire. Without that co-batch, a broken phantom/tracked split would drop
    doomed.py's deletion from `rel_paths`, `git add` would stage nothing,
    and `stage_all()` would silently sweep the deletion (and any other
    dirty side-effect) back in — passing the test for the wrong reason. The
    untouched `data/state.json` side-effect is the discriminator: it must
    stay out of the commit, proving the fallback never ran."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/tracked-deletion", base="main")
    doomed = repo.path / "doomed.py"
    doomed.write_text("z = 1\n")
    repo.commit_paths([str(doomed)], "add doomed.py")

    os.remove(doomed)
    ghost = repo.path / "ghost.py"  # never existed at all
    app = repo.path / "app.py"
    app.write_text("x = 2\n")  # a real, always-stageable co-batched edit
    data = repo.path / "data"
    data.mkdir()
    (data / "state.json").write_text('{"updated": true}')  # unrelated side-effect
    repo.commit_paths([str(doomed), str(ghost), str(app)], "remove doomed.py")

    deleted = subprocess.run(
        ["git", "show", "--diff-filter=D", "--name-only", "--format=", "HEAD"],
        cwd=repo.path, capture_output=True, text=True,
    ).stdout
    assert "doomed.py" in deleted
    tree = subprocess.run(
        ["git", "ls-tree", "HEAD", "--", "doomed.py"],
        cwd=repo.path, capture_output=True, text=True,
    ).stdout
    assert tree.strip() == ""
    files = _committed_files(repo.path)
    assert "app.py" in files
    assert "ghost.py" not in files
    assert "state.json" not in files


def test_an_add_that_fails_for_another_reason_still_raises(repo_with_bare_remote):
    """An add failure for a reason OTHER than a coder-created-then-deleted
    path (here: the path is explicitly gitignored, so it is present on disk
    the whole time and never lands in `missing`) must still raise — the fix
    must not swallow every `git add` failure."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/ignored-add-fails", base="main")
    (repo.path / ".gitignore").write_text("secret.txt\n")
    secret = repo.path / "secret.txt"
    secret.write_text("shh\n")
    with pytest.raises(GitError):
        repo.commit_paths([str(secret)], "try to add an ignored file")

    # Positive control: without the ignore entry, the same call commits fine.
    repo.create_branch("no-human/ignored-add-control", base="main")
    (repo.path / ".gitignore").unlink()
    secret.write_text("shh\n")
    repo.commit_paths([str(secret)], "add secret.txt without the ignore rule")
    files = _committed_files(repo.path)
    assert "secret.txt" in files


def test_the_missing_path_lookup_fails_closed(repo_with_bare_remote):
    """The `ls-files -z --` missing-path lookup must fail closed: if IT
    errors, `commit_paths` must raise rather than silently treating the
    lookup's empty ("") result as "nothing is tracked" and dropping the
    co-batched TRACKED deletion as if it were a phantom.

    Forces a REAL `ls-files` failure (an invalid pathspec magic in one of
    the missing paths, confirmed to exit 128) rather than monkeypatching
    `_run`/`_run_null` to raise unconditionally — a monkeypatch that raises
    regardless of the `check` kwarg can't tell `check=True` (must raise)
    apart from a `check=False` mutation (must swallow and return ""), so it
    cannot actually catch a regression to `check=False`. This can: with
    `check=True` the real git failure propagates as GitError; a mutation to
    `check=False` would instead return "" and reach `git commit`, which
    this test's `pytest.raises` would catch as a failure to raise."""
    repo = GitRepo(repo_with_bare_remote)
    repo.create_branch("no-human/lookup-fails-closed", base="main")
    doomed = repo.path / "doomed.py"
    doomed.write_text("z = 1\n")
    repo.commit_paths([str(doomed)], "add doomed.py")
    head_before = repo.head_sha()

    os.remove(doomed)
    # `:(nosuchmagic)` is not a real git pathspec magic word — `ls-files`
    # rejects it with exit 128 before it can report on anything else in the
    # same invocation, including the co-batched `doomed.py`.
    poison = repo.path / ":(nosuchmagic)ghost.py"

    with pytest.raises(GitError):
        repo.commit_paths([str(doomed), str(poison)], "remove doomed.py")

    assert repo.head_sha() == head_before


def test_every_filesystem_compared_path_output_is_nul_or_quotepath_disabled():
    """Enumeration guard: every git call in this module whose output is
    compared against the filesystem or fed back as a pathspec must carry
    `-z` (NUL-separated) or `-c core.quotePath=false` — otherwise a
    C-quoted non-ASCII path silently fails that comparison and is dropped.
    Mirrors the enumeration in `commit_paths`' docstring.

    A `-z` call must also run through `_run_null`, not `_run`: `_run`'s
    whole-output `.strip()` eats the leading space off the FIRST
    NUL-separated path, silently truncating that one filename even though
    `-z` itself is present."""
    src_path = Path(__file__).resolve().parents[1] / "src" / "no_human" / "vcs" / "git.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    def const_str(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def run_calls_in(func_node):
        calls = []
        for node in ast.walk(func_node):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("_run", "_run_null")):
                calls.append((node.func.attr, [const_str(a) for a in node.args]))
        return calls

    funcs = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    def is_safe(call):
        attr, words = call
        if "-z" in words:
            return attr == "_run_null"
        return (
            len(words) >= 2 and words[0] == "-c" and words[1] == "core.quotePath=false"
        )

    cp_calls = run_calls_in(funcs["commit_paths"])
    # `diff --cached --name-only` here is only ever checked for emptiness
    # (`if not staged: ...`) to decide whether to fall back to `stage_all`
    # — its output is never compared against the filesystem or fed back as
    # a pathspec, so C-quoting cannot cause a silent drop and it is
    # deliberately excluded from this guard.
    diff_producer = [c for c in cp_calls if c[1] and c[1][0] == "diff" and "--cached" not in c[1]]
    others_producer = [c for c in cp_calls if c[1] and c[1][0] == "ls-files" and "--others" in c[1]]
    lookup_calls = [c for c in cp_calls if c[1] and c[1][0] == "ls-files" and "--others" not in c[1]]
    assert diff_producer and all(is_safe(c) for c in diff_producer)
    assert others_producer and all(is_safe(c) for c in others_producer)
    assert lookup_calls and all(is_safe(c) for c in lookup_calls)

    usf_calls = run_calls_in(funcs["uncommitted_source_files"])
    status_calls = [c for c in usf_calls if "status" in c[1]]
    assert status_calls and all(is_safe(c) for c in status_calls)

    dir_calls = run_calls_in(funcs["_dirs_newly_added_by_head"])
    show_calls = [c for c in dir_calls if "show" in c[1]]
    assert show_calls and all(is_safe(c) for c in show_calls)

    cf_calls = run_calls_in(funcs["changed_files"])
    diff_calls = [c for c in cf_calls if c[1] and c[1][0] == "diff"]
    assert diff_calls and all(is_safe(c) for c in diff_calls)
