r"""A trailing `.exe` must not walk past the never-merge and never-push gates.

Issue #305. `guard.py` resolved argv[0] with `PurePosixPath(argv[0]).name` and
compared it against bare lowercase names. `nh.exe` is not `nh`, so three
`GUARD_DESTRUCTIVE` checks stopped firing, two of which enforce constraint #2,
that the agent never merges.

The issue was filed about the suffix. Measuring its five rows turned up two
more defects at the same sites, and a fix for any one alone leaves the others
open, so all three are pinned here:

    git.exe push origin main        the suffix is never stripped
    GIT push origin main            never case-folded, though on Windows
                                    `GIT` and `git` are the same file
    C:/tools/git.exe push ...       `PurePosixPath` does not split a backslash

WHAT THIS DOES NOT CLOSE, and why that is not this change's to close:
an UNQUOTED backslash path (`C:\tools\git.exe push origin main`) still passes,
because POSIX `shlex` deletes the separators before any name resolution
happens. `guard.py` does not consult `win_readings`, which is the half of #105
that PR #301 fixed for the venv guard only. `_basename`'s docstring already
says a caller is expected to have been offered the `/`-normalised spelling by
then. The quoted spelling of the same path IS closed here, because quoting
preserves the backslashes for the resolver to split. A space additionally
splits the token, which is issue #312.
"""
from __future__ import annotations

import pytest

from no_human.agent import exec_names
from no_human.agent.guard import evaluate


def _decide(command: str) -> bool:
    """True when the guard DENIES `command`."""
    decision = evaluate(
        "Bash", {"command": command},
        forbidden_paths=[], never_push_to=["main"], cwd=".", env={"PATH": ""})
    return not decision.allow


# ----------------------------------------------------------------- the reader

@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("git", "git"),
        ("git.exe", "git"),
        ("GIT.EXE", "git"),
        ("/usr/bin/git", "git"),
        ("/usr/bin/git.exe", "git"),
        ("C:/tools/git.exe", "git"),
        (r"C:\tools\git.exe", "git"),
        (r"C:\Program Files\Git\cmd\git.exe", "git"),
        (r"C:/tools\git.exe", "git"),          # mixed separators
        ("/bin/sh/", "sh"),                    # trailing separator, cf. _basename
        ("", ""),
    ],
)
def test_windows_reads_either_separator_and_folds_case(token, expected):
    assert exec_names.command_name(token, is_windows=True) == expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("git", "git"),
        ("git.exe", "git"),                    # suffix strip is NOT gated (#107)
        ("/usr/bin/git", "git"),
        ("C:/tools/git.exe", "git"),
        # A backslash is a legal character in a POSIX filename, so splitting on
        # it here would invent a name the user never wrote and could deny a
        # command they are entitled to run.
        (r"weird\name", r"weird\name"),
        (r"C:\tools\git.exe", r"c:\tools\git"),
        # `GIT` and `git` are different files on POSIX, so folding would be a
        # text match masquerading as a structural one.
        ("GIT", "GIT"),
        ("/bin/sh/", "sh"),
        ("", ""),
    ],
)
def test_posix_splits_only_on_slash_and_never_folds(token, expected):
    got = exec_names.command_name(token, is_windows=False)
    if token == r"C:\tools\git.exe":
        # Suffix stripped, separators untouched: still not the name `git`, which
        # is correct on a host where that really is one filename.
        assert got == r"C:\tools\git"
        return
    assert got == expected


def test_the_reader_is_not_vacuous():
    """The suffix strip and the fold must each be observable on their own, or a
    later 'simplification' of either could pass this file unchanged."""
    assert exec_names.command_name("git.exe", is_windows=True) == "git"
    assert exec_names.command_name("git.exe", is_windows=False) == "git"
    assert exec_names.command_name("GIT", is_windows=True) == "git"
    assert exec_names.command_name("GIT", is_windows=False) == "GIT"
    assert exec_names.command_name(r"a\b", is_windows=True) == "b"
    assert exec_names.command_name(r"a\b", is_windows=False) == r"a\b"


# ------------------------------------------------------------- the real gates

@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git.exe push origin main",
        "GIT.EXE push origin main",
        "C:/tools/git.exe push origin main",
        '"C:\\Program Files\\Git\\cmd\\git.exe" push origin main',
    ],
)
def test_never_push_to_main_survives_every_spelling(command):
    """`never_push_to` is the gate on pushing to a protected branch. Before
    #305 only the first row denied."""
    assert _decide(command), "push to a protected branch was allowed"


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 7",
        "gh.exe pr merge 7",
        "GH.EXE pr merge 7",
        "C:/tools/gh.exe pr merge 7",
        "nh approve 7",
        "nh.exe approve 7",
        "NH.exe approve 7",
    ],
)
def test_the_agent_still_never_merges(command):
    """Constraint #2. `gh pr merge` and `nh approve` are the two commands that
    land work, and a trailing `.exe` walked past both."""
    assert _decide(command), "a merge/approve command was allowed"


def test_the_unquoted_backslash_path_is_still_open_and_that_is_recorded():
    """Not a fix, a boundary.

    POSIX `shlex` deletes the separators before any name resolution, so this
    reaches the resolver as `C:toolsgit.exe` and no basename can recover it.
    Closing it means `guard.py` consulting `win_readings`, which is the half of
    #105 that #301 fixed for the venv guard only.

    Asserted rather than left unsaid so that whoever closes that half sees this
    test go red and deletes it deliberately, instead of the gap being
    rediscovered from scratch a third time.
    """
    assert not _decide(r"C:\tools\git.exe push origin main"), (
        "the unquoted backslash path now denies; #105's remaining half has "
        "landed, so delete this test and add the row to the parametrised "
        "never_push_to case above"
    )
