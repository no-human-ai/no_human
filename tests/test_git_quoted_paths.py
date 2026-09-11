"""Regression coverage for `core.quotePath` silently masking real work.

`core.quotePath` is on by default, so a non-ASCII path (e.g. `café.py`) is
rendered by `git diff --name-only`/`git status --porcelain`/`git show
--name-only` as a C-quoted literal (`"caf\\303\\251.py"`). Any site that then
compares that literal against the filesystem (`Path.exists()`), a
`coder_touched` set of raw paths, or feeds it back to git as a pathspec gets
NOTHING back — the real path never matches the mangled one — and the change
is dropped without ever raising. See `src/no_human/vcs/git.py`'s
`_null_paths` helper and the `-c core.quotePath=false` sites for the fix.
"""

import subprocess
import unicodedata

import pytest

from no_human.vcs import GitRepo


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def quoted_paths_repo(tmp_path):
    """A work repo with `core.precomposeunicode=true` set (the historical
    HFS+ normalization guard) plus one commit on `main`."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.com")
    _git(work, "config", "user.name", "u")
    _git(work, "config", "core.precomposeunicode", "true")
    (work / "app.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "checkout", "-b", "no-human/quoted-paths")  # main is protected
    return work


def _committed_names(repo_path):
    """Every path HEAD's commit touched, NUL-split (never C-quoted) and
    NFC-normalized, mirroring `commit_paths`' own `_null_paths` handling."""
    out = subprocess.run(
        ["git", "show", "--name-only", "--format=", "-z", "HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    ).stdout
    return {unicodedata.normalize("NFC", n) for n in out.split("\0") if n}


CAFE = unicodedata.normalize("NFC", "café.py")
NIHONGO = unicodedata.normalize("NFC", "日本語.py")  # non-decomposing: no NFC/NFD split
RESUME_PY = unicodedata.normalize("NFC", "résumé.py")
RESUME_TXT = unicodedata.normalize("NFC", "résumé.txt")
DONNEES = unicodedata.normalize("NFC", "données")


def test_a_c_quoted_modified_tracked_file_is_committed(quoted_paths_repo):
    """A MODIFIED TRACKED file whose name git C-quotes must be committed by
    the `diff --name-only` producer alone (`paths=[]`, so it is the only
    source feeding `commit_paths`). Pre-fix, `core.quotePath` renders it as
    the literal `"caf\\303\\251.py"`, whose `Path.exists()` is False, so the
    phantom filter (once added) would misclassify it as missing and the
    commit would silently succeed WITHOUT the change ever landing."""
    repo = GitRepo(quoted_paths_repo)
    cafe = repo.path / CAFE
    cafe.write_text("x = 1\n")
    _git(repo.path, "add", "-A")
    _git(repo.path, "commit", "-m", "add cafe")

    cafe.write_text("x = 2\n")
    repo.commit_paths([], "modify cafe")

    assert CAFE in _committed_names(repo.path)
    # Content, not just the name, must have landed — this is what makes a
    # silent drop (name absent from the diff but file still on disk)
    # distinguishable from a genuine no-op commit.
    diff = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", CAFE], cwd=repo.path,
    )
    assert diff.returncode == 0


def test_a_c_quoted_untracked_file_is_committed(quoted_paths_repo):
    """A brand-new UNTRACKED file with a non-decomposing non-ASCII name,
    alongside an accented untracked sibling and an ordinary tracked edit,
    must all land in the same commit."""
    repo = GitRepo(quoted_paths_repo)
    nihongo = repo.path / NIHONGO
    nihongo.write_text("print('x')\n")
    sibling = repo.path / RESUME_PY
    sibling.write_text("print('y')\n")
    app = repo.path / "app.py"
    app.write_text("x = 2\n")

    repo.commit_paths([str(app)], "add non-ascii files")

    names = _committed_names(repo.path)
    assert NIHONGO in names
    assert RESUME_PY in names
    assert "app.py" in names


def test_a_c_quoted_leftover_is_flagged_by_the_completeness_guard(quoted_paths_repo):
    """`uncommitted_source_files`'s `coder_touched` membership check
    compares the RAW path string. Pre-fix, `status --porcelain` hands back
    this leftover C-quoted/escaped, so `rel in coder_touched` never matches
    the caller's raw literal and the leftover is silently never flagged —
    the guard's own blind spot for a non-ASCII, non-code-extension name
    (résumé.txt is not in `_CODE_EXTS`, so only `coder_touched` governs it,
    isolating the quoting bug from the unconditional code-extension path)."""
    repo = GitRepo(quoted_paths_repo)
    leftover_path = repo.path / RESUME_TXT
    leftover_path.write_text("notes\n")

    assert repo.uncommitted_source_files() == []
    flagged = repo.uncommitted_source_files(coder_touched={RESUME_TXT})
    assert RESUME_TXT in flagged


def test_a_c_quoted_new_directory_is_recognised_as_newly_added(quoted_paths_repo):
    """`_dirs_newly_added_by_head` must report a brand-new non-ASCII
    directory in RAW form — proving the `show --name-only` -> `ls-tree`
    pathspec round-trip survives `-c core.quotePath=false` rather than
    comparing/feeding back a mangled literal."""
    repo = GitRepo(quoted_paths_repo)
    d = repo.path / DONNEES
    d.mkdir()
    (d / "rapport.py").write_text("print('r')\n")
    _git(repo.path, "add", "-A")
    _git(repo.path, "commit", "-m", "add données/rapport.py")

    assert DONNEES in repo._dirs_newly_added_by_head()
