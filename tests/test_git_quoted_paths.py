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
    the `diff --name-only` producer alone — café.py is never passed in
    `paths` and never explicitly touched any other way, so the producer is
    its only route into `commit_paths`. Pre-fix, `core.quotePath` renders
    it as the literal `"caf\\303\\251.py"`, whose `Path.exists()` is False,
    so the phantom filter misclassifies it as missing and drops it.

    `app.py` is co-batched via the explicit `paths` argument — a route
    that does not depend on the (possibly broken) diff producer at all —
    so `rel_paths` can never end up empty and `commit_paths`' own `if not
    staged: stage_all()` fallback can never fire. Without that co-batch, a
    broken diff producer would empty `rel_paths` entirely, `git add` would
    stage nothing, and `stage_all()` would silently sweep café.py's real
    on-disk edit (and any other dirty side-effect) back in — passing this
    test for the wrong reason. The untouched `data/state.json` side-effect
    is the discriminator: it must stay out of the commit, proving the
    fallback never ran."""
    repo = GitRepo(quoted_paths_repo)
    cafe = repo.path / CAFE
    cafe.write_text("x = 1\n")
    _git(repo.path, "add", "-A")
    _git(repo.path, "commit", "-m", "add cafe")

    cafe.write_text("x = 2\n")
    app = repo.path / "app.py"
    app.write_text("x = 2\n")  # explicit co-batch, independent of the diff producer
    data = repo.path / "data"
    data.mkdir()
    (data / "state.json").write_text('{"updated": true}')  # unrelated side-effect
    repo.commit_paths([str(app)], "modify cafe")

    names = _committed_names(repo.path)
    assert CAFE in names
    assert "app.py" in names
    assert "state.json" not in names
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


@pytest.mark.parametrize(
    "name",
    [
        pytest.param('we"ird.txt', id="quote"),
        pytest.param("back\\slash.txt", id="backslash"),
        pytest.param("tab\tbed.txt", id="tab"),
        pytest.param("plain.txt", id="plain-ascii-positive-control"),
    ],
)
def test_a_quote_backslash_or_tab_named_leftover_is_flagged_by_the_completeness_guard(
    quoted_paths_repo, name,
):
    """`-c core.quotePath=false` only suppresses quoting for non-ASCII bytes
    (>= 0x80); git C-quotes `"`, `\\`, TAB and LF UNCONDITIONALLY regardless
    of that setting. Pre-fix, `status --porcelain` (even with
    `core.quotePath=false`) still hands these names back mangled
    (`"we\\"ird.txt"`, `"back\\\\slash.txt"`, `"tab\\tbed.txt"`), so `rel in
    coder_touched` never matches the caller's raw literal and the leftover
    is silently never flagged. Only `-z` fully disables ALL quoting. The
    plain-ASCII case is the positive control proving the guard still works
    at all for an ordinary name."""
    repo = GitRepo(quoted_paths_repo)
    leftover_path = repo.path / name
    leftover_path.write_text("notes\n")

    assert repo.uncommitted_source_files() == []
    flagged = repo.uncommitted_source_files(coder_touched={name})
    assert name in flagged


@pytest.mark.parametrize(
    "dirname",
    [
        pytest.param('we"ird', id="quote"),
        pytest.param("back\\slash", id="backslash"),
        pytest.param("plaindir", id="plain-ascii-positive-control"),
    ],
)
def test_a_quote_or_backslash_named_new_directory_is_recognised_as_newly_added(
    quoted_paths_repo, dirname,
):
    """Complements the previous test by exercising `_dirs_newly_added_by_head`
    itself, not `uncommitted_source_files`'s `coder_touched` branch: a
    non-code leftover (`notes.txt`, so only the `newly_added_dirs` predicate
    can flag it — never `_CODE_EXTS` or `coder_touched`, both left empty
    here) dropped into a directory HEAD just introduced, whose name git
    C-quotes for `"`/`\\` unconditionally regardless of `core.quotePath`.
    Pre-fix, `show --name-only` (even with `core.quotePath=false`) hands the
    directory back mangled, so `Path(rel).parent` (the raw leftover's
    parent) never matches an entry in the mangled `newly_added_dirs` set and
    the leftover is silently never flagged. Only `-z` disables the
    unconditional quoting too."""
    repo = GitRepo(quoted_paths_repo)
    d = repo.path / dirname
    d.mkdir()
    (d / "mod.py").write_text("x = 1\n")
    _git(repo.path, "add", "-A")
    _git(repo.path, "commit", "-m", f"add {dirname}/mod.py")

    (d / "notes.txt").write_text("notes\n")
    flagged = repo.uncommitted_source_files()
    assert f"{dirname}/notes.txt" in flagged
