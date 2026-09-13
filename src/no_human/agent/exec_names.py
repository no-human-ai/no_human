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

The `.exe` strip is NOT gated, following the `_basename` precedent from #107
rather than inventing a second rule for the same suffix. A POSIX file named
`git.exe` is unusual, and reading it as `git` errs toward denial, which is the
direction a guard should err in.

Pure string work, no filesystem access, matching the rest of the guard.
"""

from __future__ import annotations

from pathlib import PurePosixPath

#: Stripped on every host. See the module docstring for why this one is not
#: gated when the separator and case rules are.
_EXE_SUFFIX = ".exe"


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
    name = PurePosixPath(name).name
    if name.lower().endswith(_EXE_SUFFIX):
        name = name[: -len(_EXE_SUFFIX)]
    if is_windows:
        # The filesystem is case-insensitive there, so `GIT.EXE` and `git.exe`
        # are one file and must reach one verdict. Every name set this is
        # compared against is lowercase.
        name = name.lower()
    return name
