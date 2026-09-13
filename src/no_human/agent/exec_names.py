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
* On a case-SENSITIVE POSIX filesystem `GIT` and `git` are different files,
  so folding case there would be a text match masquerading as a structural
  one. That is not every POSIX host, though: macOS ships APFS
  case-insensitive by default, and folding is measured per-host by
  `host_folds_case`, not assumed from `is_windows`. `_is_installer_name`
  makes the same fold-where-the-filesystem-folds argument for the venv guard.

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

import os
import re
import sys
import tempfile
from functools import lru_cache
from pathlib import PurePosixPath

#: Cap on directory entries `_folds_case_at` will stat looking for a
#: case-swappable name, so a directory with many thousands of entries is
#: never fully materialised for one probe.
_MAX_ENTRY_PROBES = 8

#: Cap on `PATH` entries `host_folds_case` will probe. Command resolution
#: only needs ONE anchor to prove a fold; this bounds the cost of finding it
#: on a very long `PATH`.
_MAX_PATH_PROBES = 16

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


def case_flags(cwd: str | None = None, path_env: str | None = None) -> int:
    r"""`re.IGNORECASE` where the host folds case, else no flag.

    The name-resolution path is not the whole guard: `_RM_RF`,
    `_GIT_DESTRUCTIVE` and the `_looks_like_git_push` recursion gate read the
    command as TEXT, and a capitalised spelling walks past all three on a
    case-insensitive host (#328) — `RM -rf /`, `rm -RF /` and
    `sh -c "GIT push origin main"` were open for that reason after the name
    half was closed. Same measurement, same reasoning: folding where the
    filesystem folds denies nothing that could not already run.

    `guard.py`'s regexes are compiled at IMPORT time, before any command's
    `cwd` is known, so those call sites pass neither argument and get the
    no-`cwd` union below (this process's own `os.getcwd()` plus its own
    `PATH`). That is a documented residual, not a fix: it is the
    fail-closed side of the same question, because an unmeasured anchor
    folds rather than answering permissively.
    """
    return re.IGNORECASE if host_folds_case(cwd, path_env) else 0


def _swap_probe(path: str | None) -> bool | None:
    """Tri-state: does swapping `path`'s case prove a fold? `None` means the
    question was not answered, and must never collapse into `False`.

    * `True` — the swapped spelling exists and names the SAME file
      (`samefile`, not bare `exists`: a directory genuinely holding distinct
      `Foo` and `foo` must measure `False`, not be mistaken for a fold).
    * `False` — the swapped spelling is absent, or present but a different
      inode.
    * `None` — the basename has no case-swappable character; the path is
      empty; or the probe itself could not be answered (`OSError` — a dead
      mount, a permission-denied ancestor, a symlink loop — or `ValueError`,
      e.g. an embedded NUL).
    """
    if not path:
        return None
    try:
        directory, name = os.path.split(os.fspath(path))
        swapped_name = name.swapcase()
        if swapped_name == name:  # nothing to swap: no evidence either way
            return None
        if not os.path.exists(path):
            # Nothing to compare the swapped spelling against — this proves
            # nothing about the volume, unlike a genuinely absent swapped
            # spelling (which DOES prove `False`, see below).
            return None
        swapped = os.path.join(directory, swapped_name)
        if not os.path.exists(swapped):
            return False
        return os.path.samefile(swapped, path)
    except (OSError, ValueError):
        # An unreadable, vanished, or malformed path proves nothing. This is
        # the tri-state's whole point: guessing here is what shipped as the
        # bug, because the guessed answer was `os.name == "nt"`, which is the
        # PERMISSIVE answer on POSIX.
        return None


def _folds_case_at(directory: str) -> bool | None:
    """Whether `directory`'s own filesystem folds case, measured without
    assuming its name (or any child's name) is case-swappable.

    (a) Swap `directory`'s own basename against its parent — answers for
    `/Users/x/repo` with zero listing. (b) If that is `None` (e.g. the
    directory's name has no case-bearing character, like `/` or `/123`),
    `os.scandir` up to `_MAX_ENTRY_PROBES` entries and swap each in turn,
    first non-`None` wins. `scandir`, not `listdir`, so a directory with many
    thousands of entries is never fully materialised. A per-entry `OSError`
    (an entry that vanishes mid-scan) is skipped, never turned into a
    verdict. (c) Otherwise `None`: this anchor answered nothing.
    """
    verdict = _swap_probe(directory)
    if verdict is not None:
        return verdict
    try:
        scanner = os.scandir(directory)
    except OSError:
        return None
    try:
        for count, entry in enumerate(scanner):
            if count >= _MAX_ENTRY_PROBES:
                break
            try:
                verdict = _swap_probe(entry.path)
            except OSError:
                continue
            if verdict is not None:
                return verdict
    finally:
        scanner.close()
    return None


def _candidate_anchors(cwd: str | None, path_env: str | None):
    """The paths `host_folds_case` probes, in order: the `cwd` the command
    will actually run in, then each `PATH` entry (command resolution is a
    `PATH` question, not a cwd-only one — `/usr/bin` may fold while
    `/Volumes/dev` does not), capped and de-duplicated in-order.
    """
    yield cwd or os.getcwd()
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    probed = 0
    for entry in raw.split(os.pathsep):
        if not entry:
            continue
        if probed >= _MAX_PATH_PROBES:
            break
        probed += 1
        yield entry


@lru_cache(maxsize=None)
def host_folds_case(cwd: str | None = None, path_env: str | None = None) -> bool:
    r"""Whether the filesystem a command resolves and runs against folds case.
    Measured, not assumed — and measured against a path that EXISTS when the
    shipped artifact runs, not this module's own source file.

    `is_windows` answers the question for Windows and gets POSIX wrong: macOS
    ships APFS case-insensitive by default, and so are many Linux mounts
    (exFAT/NTFS volumes, ciopfs, a case-insensitive ZFS dataset). On such a
    host `GH pr merge 7` really invokes `gh pr merge 7`, and every gate that
    compares against a lowercase name is open to the capitalised spelling
    (#328).

    THREE FACETS, one probe. The shipped desktop server is a PyInstaller
    onedir freeze whose spec says no `.py` files ship, so `__file__` names a
    path that does not exist at runtime — probing it answered nothing, and
    the old fallback (`os.name == "nt"`) is the PERMISSIVE answer on POSIX, a
    live bypass (`GH pr merge 7` -> ALLOW). And command *resolution* is a
    `PATH` question: the volume holding this module is not necessarily the
    volume a bare `pip`/`gh`/`git` resolves against. Probing `cwd` (which
    `evaluate`/`denial_reason` already receive, and which exists in both a
    checkout and the freeze) UNION each `PATH` entry closes all three: an
    anchor exists at runtime either way, and a capitalised spelling only has
    to resolve on *some* anchor for the guarded program to be reached.

    UNION, FAIL-CLOSED. Any anchor that measures `True` settles it (folding
    somewhere on the resolution path is enough). If every measured anchor is
    determinate and none is `True`, the answer is the determinate `False`.
    Only when NOTHING could be measured (every anchor answered `None`) does
    this fall back to a couple of last-resort candidates, and only if THOSE
    are also all `None` does it default to `True` — folding, never the
    permissive answer, because an unmeasurable probe must fail toward "deny a
    spelling nobody types", not toward "allow `GH pr merge 7`".

    Cached on `(cwd, path_env)`: both are explicit arguments, so the cache
    cannot go stale within one set of arguments. The `(None, None)` key reads
    the process's OWN `os.getcwd()`/`PATH`, which `os.chdir` could invalidate
    — but this guard never calls `os.chdir`, and the failure direction is
    safe regardless: a stale `True` only ever over-denies.

    Read per TOKEN and per SEGMENT (`command_name`'s `fold_case=None` default,
    `_looks_like_git_push`'s recursion), which is why this is cached rather
    than re-probing the filesystem on every call — see
    `test_the_host_probe_is_measured_once_not_once_per_token`.
    """
    saw_false = False
    for candidate in _candidate_anchors(cwd, path_env):
        verdict = _folds_case_at(candidate)
        if verdict is True:
            return True
        if verdict is False:
            saw_false = True
    if saw_false:
        return False
    # Nothing on cwd/PATH was measurable at all. Try a couple of anchors that
    # are themselves guaranteed to exist at runtime in both a checkout and a
    # frozen bundle, before giving up and folding.
    for candidate in (os.path.dirname(sys.executable), tempfile.gettempdir()):
        verdict = _folds_case_at(candidate)
        if verdict is True:
            return True
        if verdict is False:
            saw_false = True
    if saw_false:
        return False
    # Still nothing determinate anywhere: fold. A false DENY here lands on a
    # spelling nobody types; the permissive answer is a live bypass.
    return True


def command_name(token: str, *, is_windows: bool, fold_case: bool | None = None) -> str:
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
    if fold_case is None:
        fold_case = is_windows or host_folds_case()
    if fold_case:
        # `GIT.EXE` and `git.exe` are one file wherever the filesystem folds
        # case, so they must reach one verdict. Every name set this is compared
        # against is lowercase. A parameter as well as a probe, so both answers
        # stay reachable from one runner -- the same reason `is_windows` is one.
        name = name.lower()
    return name
