"""A Windows executable suffix must not change the guard's verdict.

Issue #305. `guard.py` resolved argv[0] with `PurePosixPath(argv[0]).name` and
compared it against bare names, so a four-character suffix walked past three
`GUARD_DESTRUCTIVE` checks -- including the two that enforce constraint #2,
the agent never merges, and the gate on pushing to a protected branch.

Not platform-gated: the suffix is simply never stripped, so the bypass is
spellable anywhere. On POSIX a coder would have to write `nh.exe`, which does
not exist there, so the guard fails open but the action does not happen; on
Windows it is both spellable and runnable.

WHY THIS FILE IS SHAPED AS A MATRIX. Its first version asserted over argv[0]
`.exe` pairs and two source-text "invariants". An adversarial review took it
apart: the invariants were decorative (replacing every site with
`posixpath.basename(` -- already imported in guard.py -- left both green while
fully reintroducing the bypass), the `.exe` half of the fix had been applied
to `git` only so `gh.exe pr merge` and `rm.exe -rf /` still walked through,
and stripping the suffix inside the operand-walking scans turned 198 commands
that main allows into false denials. Behaviour is pinned here instead, over
every guarded binary and every PATHEXT spelling, with the operand cases as
explicit negative controls.
"""

from __future__ import annotations

import itertools

import pytest

from no_human.agent import guard, venv_install_guard

PROTECTED = ["main", "master", "release/*"]

#: Every suffix Windows resolves a bare command name against, plus the case
#: variants, because the filesystem is case-insensitive there.
SUFFIXES = ["", ".exe", ".EXE", ".Exe", ".bat", ".cmd", ".com", ".ps1", ".CMD"]

#: (binary, rest-of-command) for each guarded family. The bare spelling of
#: every one is a verdict this repo already commits to -- that is what makes
#: the suffixed spellings meaningful rather than self-referential.
DANGEROUS = [
    ("nh", "approve 7"),                                   # constraint #2
    ("gh", "pr merge 7"),                                  # constraint #2
    ("glab", "mr accept 12"),                              # constraint #2
    ("gh", "api -X PUT /repos/o/r/pulls/7/merge"),         # merge via the API
    ("glab", "api -X PUT /projects/1/merge_requests/12/merge"),
    ("git", "push origin main"),                           # protected branch
    ("git", "push --force origin master"),
    ("rm", "-rf /"),                                       # destructive
    ("find", "/ -delete"),
    ("grep", "-r secret /"),
]

#: Prefixes that must not change the verdict. `timeout`/`nice` are NOT in
#: `_WRAPPERS`, so those rows exercise the raw-text fallbacks rather than argv
#: analysis -- the half the first version of this fix left bare.
PREFIXES = ["", "sudo ", "sudo -H ", "env -i ", "timeout 30 ", "nice -n 10 ", "xargs "]


def _allows(cmd: str, *, readonly: bool = False) -> bool:
    return guard.evaluate(
        "Bash", {"command": cmd},
        forbidden_paths=[], never_push_to=PROTECTED, env={"PATH": ""},
        readonly=readonly,
    ).allow


#: Commands whose BARE spelling `main` already refuses. Each is a verdict the
#: repo has committed to, which is what makes the suffixed rows meaningful.
@pytest.mark.parametrize("binary, rest", DANGEROUS)
def test_the_bare_spelling_is_refused(binary, rest):
    """The control. Without it, a guard that denied everything would pass."""
    assert not _allows(f"{binary} {rest}"), f"{binary} {rest}"


@pytest.mark.parametrize(
    "binary, rest, suffix",
    [(b, r, s) for (b, r), s in itertools.product(DANGEROUS, SUFFIXES)],
)
def test_no_pathext_spelling_changes_the_verdict(binary, rest, suffix):
    """`gh.cmd` and `gh.ps1` are what scoop and npm-style shims install on
    Windows, so matching only `.exe` closes the spelling a reviewer thinks of
    first and leaves the one users actually have."""
    assert not _allows(f"{binary}{suffix} {rest}"), f"{binary}{suffix} {rest}"


# --- the invariant, stated as PARITY rather than as an absolute verdict -----
#
# The first version of this file asserted absolute verdicts: "this command
# must be denied", "this one must be allowed". That encoded MY belief about
# what the guard should do, and two of those beliefs were wrong -- `main`
# already refuses `sudo -H sha256sum dist/find`, so demanding that its `.exe`
# twin be ALLOWED was demanding an inconsistency, not a fix.
#
# What this change actually claims is narrower and checkable: a Windows
# executable suffix must not CHANGE the verdict. Whatever `main` does with the
# bare spelling -- right or wrong -- the suffixed spelling must do the same.
# That is immune to whatever pre-existing over-blocks the guard carries, and
# it is the property a Windows user depends on.

#: (command, the EXACT substring that is the guarded binary). Spelled out
#: rather than inferred: a heuristic for "which token is the command" got
#: `30` out of `timeout 30 install …` and `mr` out of `bash -lc "glab mr …"`,
#: and a test that suffixes the wrong token measures nothing.
PARITY_CORPUS = [
    ("nh approve 7", "nh"),
    ("gh pr merge 7", "gh"),
    ("glab mr accept 12", "glab"),
    ("gh api -X PUT /repos/o/r/pulls/7/merge", "gh"),
    ("git push origin main", "git"),
    ("git push --force origin master", "git"),
    ("rm -rf /", "rm"),
    ("find / -delete", "find"),
    ("grep -r secret /", "grep"),
    # wrapper-prefixed; `timeout`/`nice` are NOT in `_WRAPPERS`, so these
    # exercise the raw-text fallbacks rather than argv analysis
    ("sudo rm -rf /", "rm"),
    ("sudo -H gh pr merge 7", "gh"),
    ("env -i nh approve 7", "nh"),
    ("timeout 30 git push origin main", "git"),
    ("nice -n 10 git push origin main", "git"),
    ("xargs gh pr merge 7", "gh"),
    # nested shell payloads: the binary inside the quotes
    ('sh -c "gh pr merge 7"', "gh"),
    ('bash -lc "glab mr accept 12"', "glab"),
    ('sh -c "git push origin main"', "git"),
    # ordinary read-only work
    ("git status", "git"),
    ("gh pr view 7", "gh"),
    ("git log --oneline -5", "git"),
]


@pytest.mark.parametrize(
    "cmd, binary, suffix",
    [(c, b, s) for (c, b), s in itertools.product(PARITY_CORPUS, SUFFIXES) if s],
)
@pytest.mark.parametrize("readonly", [False, True])
def test_the_suffix_never_changes_the_verdict(cmd, binary, suffix, readonly):
    """The claim of this change, stated exactly.

    Not "these commands are denied" -- several spellings the guard refuses are
    pre-existing over-blocks, and demanding a particular absolute verdict
    encodes a belief about what the guard SHOULD do. What this change owes is
    narrower and checkable: a Windows executable suffix must not CHANGE the
    verdict, whatever that verdict is.
    """
    suffixed = cmd.replace(binary, binary + suffix, 1)
    assert suffixed != cmd
    assert _allows(cmd, readonly=readonly) == _allows(suffixed, readonly=readonly), (
        f"{cmd!r} and {suffixed!r} disagree")


@pytest.mark.parametrize("suffix", [s for s in SUFFIXES if s])
@pytest.mark.parametrize("operand_cmd, operand", [
    ("timeout 30 install -m755 dist/git /usr/local/bin/", "dist/git"),
    ("sudo -H sha256sum dist/find", "dist/find"),
    ("env -i cat build/rg", "build/rg"),
    ("sudo cp dist/grep /tmp/", "dist/grep"),
])
def test_a_suffixed_file_argument_never_changes_the_verdict(
    operand_cmd, operand, suffix
):
    """The other direction: the suffix on an OPERAND, not on the command.

    The first version of this fix stripped it here too, so a file ARGUMENT
    named `dist/git.exe` read as the `git` command and
    `install -m755 dist/git.exe /usr/local/bin/` came back "working-tree-unsafe
    git blocked" -- a false reason on an ordinary act.

    Parity is still the rule, deliberately: `main` ALREADY refuses the bare
    `dist/find` spelling, so demanding that `dist/find.exe` be ALLOWED would
    demand an inconsistency rather than a fix. That over-block is real and
    pre-existing, and it is not this change's to carry.
    """
    suffixed = operand_cmd.replace(operand, operand + suffix, 1)
    assert _allows(operand_cmd) == _allows(suffixed), operand_cmd


HARMLESS = [
    "git status", "git.exe status", "gh pr view 7", "gh.exe pr view 7",
    "git log --oneline -5", "git.exe log --oneline -5",
    "git add build.bat", "cp deploy.cmd /tmp/", "cat setup.ps1",
]


@pytest.mark.parametrize("cmd", HARMLESS)
def test_an_ordinary_command_is_still_allowed(cmd):
    """Absolute, because these are unambiguous: a guard that denied them
    would be switched off within a day."""
    assert _allows(cmd), cmd


# --- the name must never come back EMPTY ------------------------------------
#
# The first version of this change read argv[0] with `os.path.basename`.
# `os.path.basename("/usr/bin/gh/")` is `""`, `""` is in no guarded set, and
# so "could not read a name" collapsed into "not a guarded binary" and the
# command was allowed. A shell runs `/usr/bin/gh/ pr merge 7` exactly as
# `gh pr merge 7`, so that is a live bypass, not a curiosity -- and it is not
# about `.exe` at all, which is why the matrix above could not see it.

#: (spelling of the binary, the bare name it must still read as). Trailing
#: separators, and the `.`-component forms a shell resolves identically.
SEPARATOR_SPELLINGS = [
    ("/usr/bin/gh/", "gh"),
    ("/usr/bin/git/", "git"),
    ("/usr/bin/find/", "find"),
    ("/usr/bin/nh/", "nh"),
    ("/usr/bin/glab/", "glab"),
    ("/usr/bin/grep/", "grep"),
    ("gh/", "gh"),
    ("./gh/", "gh"),
    ("/usr/bin/gh//", "gh"),
    ("/usr/bin/gh/.", "gh"),
    ("/usr/bin/gh.exe/", "gh"),
    ("/usr/bin/GH.EXE/", "gh"),
]


@pytest.mark.parametrize("spelling, bare", SEPARATOR_SPELLINGS)
def test_a_path_that_names_something_never_reads_as_an_empty_name(spelling, bare):
    """`""` must never be the answer for a path that names a binary.

    Stated on the function rather than only through a verdict, because `""`
    fails EVERY membership test the guard makes -- `in _SCAN_EXECUTABLES`,
    `== "git"`, `in {"gh", "glab"}` -- so one empty return turns every one of
    those into "not guarded" at once, and a verdict test only catches the
    sites it happens to exercise.
    """
    assert venv_install_guard._basename(spelling) == bare


@pytest.mark.parametrize("spelling, bare", SEPARATOR_SPELLINGS)
@pytest.mark.parametrize("rest", [
    "pr merge 7", "push --force origin main", "/ -delete", "approve 7",
])
def test_a_trailing_separator_reaches_the_same_verdict_as_the_bare_name(
    spelling, bare, rest
):
    """Parity again, and for the same reason as the suffix matrix: whatever
    the guard does with `gh pr merge 7`, `/usr/bin/gh/ pr merge 7` must do."""
    assert _allows(f"{spelling} {rest}") == _allows(f"{bare} {rest}"), spelling


#: The named DENY->ALLOW regressions, spelled out as absolutes. Each was
#: measured refused on `main` and allowed by the first version of this change.
TRAILING_SEPARATOR_BYPASSES = [
    "/usr/bin/gh/ pr merge 7",
    "/usr/bin/git/ push --force origin main",
    "/usr/bin/git/ push origin main",
    "/usr/bin/find/ / -delete",
    "echo 7 | xargs /usr/bin/gh/ pr merge",
    "/usr/bin/nh/ approve 7",
    "/usr/bin/glab/ mr accept 12",
]


@pytest.mark.parametrize("cmd", TRAILING_SEPARATOR_BYPASSES)
def test_a_trailing_separator_does_not_hide_a_guarded_binary(cmd):
    assert not _allows(cmd), cmd


# --- the STEM folds wherever the SUFFIX already does ------------------------
#
# `_PATHEXT_RE` was written `[Ee][Xx][Ee]` on the argument that the Windows
# filesystem is case-insensitive. That argument is about the FILENAME, not
# about its last four characters, and it was applied to the suffix only: the
# matrix above builds every row as f"{binary}{suffix}" with `binary` always
# lowercase, so it structurally could not see that `gh.EXE pr merge` was
# refused while `GH.EXE pr merge` was allowed.
#
# NOT closed, deliberately: the BARE uppercase spelling. `RM -rf /` is
# allowed on `main` and is still allowed here. On POSIX `RM` really is a
# different file from `rm`, so folding a bare name would be a text match
# dressed up as a structural one; the fold here is justified only by a
# suffix that exists nowhere but Windows.


def _case_variants(binary: str) -> list[str]:
    return [binary, binary.upper(), binary.capitalize()]


@pytest.mark.parametrize(
    "binary, rest, cased, suffix",
    [(b, r, c, s)
     for (b, r), s in itertools.product(DANGEROUS, [x for x in SUFFIXES if x])
     for c in _case_variants(b)],
)
def test_the_binary_case_does_not_change_a_suffixed_verdict(
    binary, rest, cased, suffix
):
    """`GH.EXE`, `Gh.exe` and `gh.EXE` are one file on Windows."""
    assert not _allows(f"{cased}{suffix} {rest}"), f"{cased}{suffix} {rest}"


@pytest.mark.parametrize("cmd, binary", PARITY_CORPUS)
@pytest.mark.parametrize("suffix", [s for s in SUFFIXES if s])
def test_the_binary_case_does_not_change_a_suffixed_verdict_either_way(
    cmd, binary, suffix
):
    """The parity form, so the rows that main ALLOWS are covered too."""
    lower = cmd.replace(binary, binary + suffix, 1)
    upper = cmd.replace(binary, binary.upper() + suffix, 1)
    assert lower != cmd and upper != cmd
    assert _allows(lower) == _allows(upper), f"{lower!r} vs {upper!r}"


def test_a_bare_uppercase_name_is_not_folded():
    """The boundary, asserted so the fold cannot quietly widen.

    Nothing on POSIX says `RM` and `rm` are one file, and this change does
    not claim they are. What justifies folding `RM.EXE` is the `.EXE`.
    """
    assert venv_install_guard._basename("RM") == "RM"
    assert venv_install_guard._basename("RM.EXE") == "rm"
    assert venv_install_guard._basename("Gh.Cmd") == "gh"


# --- the raw-text layer: spellings argv analysis never reaches --------------
#
# Each command below is refused ONLY by the lexical matcher named beside it:
# removing the PATHEXT handling from that ONE expression turns it back into
# ALLOW, and leaves every other row here unchanged. Measured that way, one
# mutation at a time, restored by copy between runs -- without these rows all
# of those mutations left the suite green.

#: (command, the expression that is its only catcher)
LEXICAL_ONLY_BYPASSES = [
    ("rm.exe -rf /tmp/x", "_RM_RF"),
    ("RM.EXE -rf /tmp/x", "_RM_RF"),
    ("alias c='git.exe clean -fdx'", "_GIT_DESTRUCTIVE"),
    ('echo "then git.exe reset --hard HEAD~1" >> runbook.md', "_GIT_DESTRUCTIVE"),
    ('python -c "os.system(\'git.exe push --force origin main\')"',
     "_GIT_DESTRUCTIVE"),
    ("alias m='gh.exe pr merge 7'", "_FORGE_MERGE (gh pr merge)"),
    ('echo "then gh.exe pr merge 7" >> runbook.md', "_FORGE_MERGE (gh pr merge)"),
    ('python -c "os.system(\'gh.exe pr merge 7\')"', "_FORGE_MERGE (gh pr merge)"),
    ("alias m='glab.exe mr accept 12'", "_FORGE_MERGE (glab mr accept)"),
    ('python -c "os.system(\'gh.exe api -X PUT /repos/o/r/pulls/7/merge\')"',
     "_FORGE_MERGE (gh api)"),
    ('python -c "os.system('
     "'glab.exe api -X PUT /projects/1/merge_requests/12/merge')\"",
     "_FORGE_MERGE (glab api)"),
    ('bash -lc "git.exe stash"', "_git_invocations' quoted-payload search"),
    # a global option between the binary and the noun, so the contiguous
    # `gh\s+pr\s+merge` text never matches and only the MENTION search reaches it
    ('bash -lc "gh.exe -R o/r pr merge 7"', "_FORGE_MENTION (gh)"),
    ('bash -lc "glab.exe -R o/r mr merge 12"', "_FORGE_MENTION (glab)"),
    ("cat <<'EOF' > cleanup.sh\ngit.exe clean -fdx\nEOF", "_GIT_DESTRUCTIVE"),
    ("cat <<'EOF' > m.sh\ngh.exe pr merge 7\nEOF", "_FORGE_MERGE (gh pr merge)"),
    # the pre-filter that decides whether a quoted payload is recursed into
    ('sh -c "GIT.CMD push origin main"', "_looks_like_git_push"),
]


@pytest.mark.parametrize("cmd, matcher", LEXICAL_ONLY_BYPASSES)
def test_a_lexically_matched_spelling_is_refused(cmd, matcher):
    assert not _allows(cmd), f"{cmd!r} (only {matcher} reaches this)"


#: The readonly-only half: `_GIT_WRITE`/`_FORGE_WRITE` are consulted only in a
#: read-only session, so a coder-session corpus cannot exercise them at all.
READONLY_ONLY_BYPASSES = [
    ("alias c='git.exe commit -am wip'", "_GIT_WRITE"),
    ('echo "then git.exe commit -am wip" >> runbook.md', "_GIT_WRITE"),
    ("alias c='gh.exe pr close 7'", "_FORGE_WRITE (gh)"),
    ("alias c='glab.exe mr update 12'", "_FORGE_WRITE (glab)"),
]


@pytest.mark.parametrize("cmd, matcher", READONLY_ONLY_BYPASSES)
def test_a_readonly_session_refuses_the_suffixed_write(cmd, matcher):
    assert not _allows(cmd, readonly=True), f"{cmd!r} (only {matcher} reaches this)"


@pytest.mark.parametrize("cmd, matcher", READONLY_ONLY_BYPASSES)
def test_the_same_spelling_reaches_the_bare_verdict_outside_readonly(cmd, matcher):
    """The control for the rows above: they are readonly-only on `main` too,
    so a coder session allowing them is not something this change introduced.
    Parity against the bare spelling is what it owes."""
    bare = cmd.replace(".exe", "")
    assert _allows(cmd) == _allows(bare), cmd
