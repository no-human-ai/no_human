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

from no_human.agent import guard

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
