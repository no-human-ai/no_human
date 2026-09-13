r"""The command name a token spells, read the way the host would read it.

Issue #305. `guard.py` resolved argv[0] with `PurePosixPath(argv[0]).name` and
compared the result against bare lowercase names at seven sites. Three of those
are `GUARD_DESTRUCTIVE`, and two of them enforce constraint #2, that the agent
never merges:

    git push origin main   DENY  |  git.exe push origin main   ALLOW
    gh pr merge 7          DENY  |  gh.exe pr merge 7          ALLOW
    nh approve 7           DENY  |  nh.exe approve 7           ALLOW

Split out of `guard.py` rather than added to it, for the same reason
`fs_roots.py` was: that file is frozen at its current length by
`tests/test_structural_budget.py` (`FROZEN_FILE_LINES`), a budget that only
ratchets down, and it sits exactly at the line. So `guard.py` gains no lines:
its existing `from . import ...` grew a name in place and the seven call sites
were edited in place.

THREE DEFECTS, NOT ONE
----------------------
The issue was filed about the `.exe` suffix. Measuring the five shapes in it
turned up two more, and a fix for any one alone leaves the other two open:

    git.exe push origin main                      the suffix is never stripped
    C:\Program Files\Git\cmd\git.exe push ...     `PurePosixPath` does not
                                                  split on a backslash, so the
                                                  basename is the whole string
    GIT push origin main                          never case-folded, though on
                                                  Windows `GIT` and `git` are
                                                  the same file

The middle and last rows are the realistic spellings on Windows, so shipping
only the suffix fix would have left branch protection open while looking like
the class was closed.

WHY `is_windows` IS A PARAMETER
-------------------------------
`venv_install_guard._basename` deliberately reads `/` on every host, and its
docstring gives the reason: `os.path.basename` splits on `\` on Windows and
not on POSIX, so the same string would reach different verdicts on different
machines while CI runs POSIX only.

That objection is about an UNGATED host check, not about host awareness as
such. Taking `is_windows` as an argument, exactly as
`fs_roots.is_windows_filesystem_root` does, keeps both readings reachable from
a POSIX runner, so the Windows behaviour is tested on the same CI that tests
the POSIX behaviour rather than trusted.

Gating matters in both directions:

* On POSIX a backslash is a legal character in a filename, so splitting on it
  would invent a name the user never wrote and could deny a command they are
  entitled to run.
* On POSIX `GIT` and `git` are different files, so folding case there would be
  a text match masquerading as a structural one. `_is_installer_name` already
  makes exactly this argument for the venv guard.

The suffix strip is NOT gated, following the `_basename` precedent from #107
rather than inventing a second rule for the same suffix. A POSIX file named
`git.exe` is unusual, and reading it as `git` errs toward denial, which is the
direction a guard should err in.

The set is every extension Windows executes, not just `.exe`. Review made the
point that decides it: a CLI installed by scoop or npm is spelled `gh.cmd`, so
`.exe` alone closes the spelling a reviewer thinks of first and leaves the one
users actually have. Trailing dots and the NTFS `::$DATA` stream suffix are
stripped too, both Windows-gated, because Win32 resolves `gh.` and
`gh.exe::$DATA` to the same programs while on POSIX a colon and a trailing dot
are ordinary filename characters.

WHAT THIS STILL DOES NOT REACH. Three shapes survive, and all of them decide
through RAW-TEXT matchers rather than through argv[0], so no name resolver can
close them:

    rm.exe -rf /                      `_RM_RF` matches the text, not a basename
    echo "gh.exe pr merge 7" | sh     the lexical merge-stack matcher
    timeout 30 git.exe push ...       `timeout` is not in `_WRAPPERS`, so this
                                      verdict comes from the raw-text half too

Widening `_RM_RF`, `_FORGE_MERGE`, `_FORGE_MENTION`, `_FORGE_WRITE`,
`_GIT_WRITE`, `_LEXICAL_MERGE_STACK` and `_LEXICAL_LIVE_SERVER` is a separate
change against the same issue.

Pure string work, no filesystem access, matching the rest of the guard.
"""

from __future__ import annotations

from pathlib import PurePosixPath

#: Every extension Windows will execute directly, longest-first so `.exe` is
#: not stripped out of a name that ends `.exe.cmd`.
#:
#: `.exe` alone closes the spelling a reviewer thinks of first and leaves the
#: one users have: a CLI installed by scoop or npm is `gh.cmd`, not `gh.exe`,
#: and `nh.bat` and `gh.ps1` are ordinary too. `.com` is included because
#: Windows still resolves it and it is one keystroke from `.cmd`.
_EXECUTABLE_SUFFIXES = (".cmd", ".bat", ".ps1", ".exe", ".com")

#: NTFS names a file's default data stream `file::$DATA`, which opens and
#: executes the same file. Windows-only syntax: on POSIX a colon is an
#: ordinary filename character.
_ADS_SEPARATOR = "::"


def command_name(token: str, *, is_windows: bool) -> str:
    r"""The command name `token` spells, or `""` if it names nothing.

    `PurePosixPath(...).name` rather than `os.path.basename`, so a trailing
    separator does not produce `""`: `os.path.basename("/bin/sh/")` is empty,
    which is in no name set, and that is how `/bin/sh/ -c "..."` once stopped
    being recognised as a shell runner. Same reasoning as
    `venv_install_guard._basename`.
    """
    name = token
    if is_windows:
        # Only the last component matters, and on Windows either separator
        # ends one. Done before PurePosixPath so a mixed spelling such as
        # `C:/tools\git.exe` resolves too.
        name = name.replace("\\", "/")
        # `gh.exe::$DATA` opens the same file as `gh.exe`.
        name = name.split(_ADS_SEPARATOR, 1)[0]
    name = PurePosixPath(name).name
    if is_windows:
        # Win32 strips trailing dots when it resolves a path, so `gh.` and
        # `nh.exe.` run `gh` and `nh`. On POSIX a trailing dot is part of the
        # name and removing it would invent a different file.
        name = name.rstrip(".")
    lowered = name.lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if lowered.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if is_windows:
        # The filesystem is case-insensitive there, so `GIT.EXE` and `git.exe`
        # are one file and must reach one verdict. Every name set this is
        # compared against is lowercase.
        name = name.lower()
    return name
