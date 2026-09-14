"""Structural venv-install guard (task: venv-install guard v2).

Task 16a798c1 ("refuse installs into the shared dev venv from a coder
session") failed three review rounds. All three attempts used LEXICAL
enforcement — a raw-text separator split (``_CMD_SEP``-style) and/or
argv[0]-positional matching over the command string — and all three were
defeated by shell segmentation, not by a missing pattern:

  1. **Wrapper / nested-shell laundering** (attempt 1's checklist) —
     ``bash -lc '<primary>/.venv/bin/pip install foo'``,
     ``sh -c "VIRTUAL_ENV=<primary>/.venv pip install -e ."``,
     ``xargs <primary>/.venv/bin/pip install``,
     ``uv run pip install --python <primary>/.venv/bin/python foo``,
     ``env -i pip install foo`` / ``sudo -H pip install foo`` (argv[0] is a
     flag, not the installer) and ``timeout 300 <pip> install foo`` (argv[1]
     is a number, not the installer) all passed because the guard asked
     "what is argv[0]/argv[1]" instead of "what does this command actually
     resolve to and where does it write".
  2. **Separator inside the quoted payload** (attempt 2's checklist) — a
     raw-text split on ``;``/``&&``/``\\n`` run BEFORE quote-aware
     tokenising, so ``sh -c "<primary>/.venv/bin/pip install foo && echo
     ok"`` and ``bash -lc "cd <primary> && uv sync"`` were split into
     segments that looked unrelated to the git-style analysis; plus
     ``uv sync --project <primary>``, whose target flag was not on the
     list of flags examined.
  3. **Punctuation-run / group tokens** (attempt 3's checklist) — a
     tokeniser using ``shlex.shlex(..., punctuation_chars=True)`` without
     quote-aware pre-segmentation glued punctuation onto adjacent words
     (``cd <primary>\\n\\nuv sync``, ``true &\\n<primary>/.venv/bin/pip
     install foo``, ``(cd <primary> && uv sync)``, ``{ cd <primary> && uv
     sync; }``, ``pushd <primary> && uv sync``), so a positional "argv[0]
     is a subshell/group marker" check missed the real command inside.

Common root cause across all three (recorded in every verdict): these are
segmentation + argv-position failures, not missing patterns. A fourth
lexical/regex rule would repeat the class (memory: *a lexical guard cannot
enforce a capability*). This module is therefore POSITIONLESS and
STRUCTURAL: it never asks "what is argv[0]" or matches the command text
against a policy pattern. It instead:

  1. Tokenises the whole command with a single quote-aware lexer
     (:func:`_lex`) that treats shell metacharacters (``;``, ``&&``, ``&``,
     ``(``, ``)``, ``{``, ``}``, ``\\n``) as token boundaries EVEN WHEN
     GLUED to a word, while text inside quotes is never split — the
     opposite order from the guards that were defeated, which split first
     and tokenised second.
  2. Recurses (bounded depth) into the quoted script argument of a known
     shell runner's ``-c``/``-lc`` flag, so ``sh -c "..."`` and
     ``bash -lc "..."`` are analysed as their own token stream — this is
     what lets a shell-launder attempt collapse to the same flat analysis
     as the direct spelling. Recursion is scoped to actual shell runners
     (not e.g. ``echo``), so ``echo "pip install foo"`` — argument text
     that is never executed — is left alone (over-blocking regression
     guard).
  3. Resolves every token that could name an installer executable to its
     CANONICAL, symlink-followed path (:func:`_resolve_installer`) and
     asks only whether the *resolved basename* is a known installer — a
     property of the resolved file, never a text match on the command.
  4. Resolves every signal that determines WHERE that installer would
     write — explicit ``--target``/``--prefix``/``--root``/``--python``/
     ``--project``/``--directory`` flag values, a COMMAND-LEVEL
     ``VIRTUAL_ENV``/``UV_PROJECT_ENVIRONMENT`` assignment (``VIRTUAL_ENV=…
     pip install`` — verdict 1's laundering trick), the venv that owns a
     resolved ``pip``/``python`` installer (a `pyvenv.cfg` probe two
     directories up — NOT applied to ``uv``/``uvx``, which are project-based
     environment managers: their own binary's location is irrelevant to
     install target, only where they are POINTED matters, and that is
     already covered by the other signals in this list), and every
     path-like token that resolves to an existing directory — the last of
     these subsumes ``cd``/``pushd``/subshell-group operands without
     needing to thread a running "current directory" through segments,
     which is exactly the thread that broke in verdict 3.
     Deliberately NOT a signal: the merely-INHERITED ``VIRTUAL_ENV``/
     ``UV_PROJECT_ENVIRONMENT`` value (``env["VIRTUAL_ENV"]`` when the
     command never assigns it itself). A structural-guard review round
     found that every real coder session inherits ``os.environ`` from the
     backend process, whose ``VIRTUAL_ENV`` already names the shared dev
     venv unconditionally — so trusting the inherited value here denied
     `uv sync` / `uv run pytest -q` in EVERY session, not just a
     laundering one. See the residual-risk register below.
  5. Denies unless EVERY resolved candidate is inside the session's
     worktree (``cwd``) — an allow-list, not a deny-list, so a spelling
     nobody has thought of yet still resolves to "outside the worktree"
     and is denied by construction rather than by a missing pattern.
  6. Fails closed (memory: *gates must fail closed*) whenever install
     intent is present but a resolution step cannot be completed:
     shell/variable expansion (``$``, backticks) in any token, no
     resolvable installer, or no ``cwd``.

RESIDUAL RISK REGISTER (the "document residual risks" half of this
ticket — deliberately NOT built here, memory: *a lexical guard cannot
enforce a capability*): this module resolves what the COMMAND TEXT says
pre-execution. It cannot see:

  - values produced by expansion this process does not perform —
    ``pip install $PKG``, `` `cmd` ``, ``$(cmd)`` — these are denied
    (fails closed on the unresolved token) rather than silently allowed;
  - arguments streamed from stdin (``xargs ... < list``, a piped
    generator) — the guard sees the installer/flags on the command line
    only, never the fed values;
  - a script read from disk and executed (``bash script.sh``) — the
    script's own content is not read by this guard;
  - runtime mutation of ``PATH``/``os.chdir``/env inside a long-running
    process this guard evaluated once before it started;
  - ``sudo -u other-user`` — a real UID change this process cannot see
    ahead of time;
  - a command that relies SOLELY on an inherited ``VIRTUAL_ENV``/
    ``UV_PROJECT_ENVIRONMENT`` (e.g. bare ``uv pip install foo`` with no
    project context, no explicit flags, and a ``PATH`` that does not
    itself resolve to the shared venv) is not caught. A structural-guard
    review round measured that production backends inherit ``os.environ``
    unmodified, so every coder session's ``VIRTUAL_ENV`` already names the
    shared dev venv — not just a laundering command's. Treating that
    inherited value as authoritative denied ordinary, safe commands
    (``uv sync``, ``uv run pytest -q``) in every session, so this module
    only trusts a COMMAND-LEVEL ``VIRTUAL_ENV=…`` assignment (see item 4
    above).

    **NARROWED 2026-09-09 (#128, coder half).** ``--active`` is now caught,
    because it is a COMMAND-LEVEL statement of intent to use the inherited
    value, which is the same class of signal as the assignment above rather
    than the "solely inherited" case this paragraph is about. It is worth
    catching on its own: ``uv run`` SYNCS the project into its target
    environment before running anything, so ``uv run --active ruff check .``
    carries no install word and still rewrote the shared venv's editable path
    in the incident that raised #128. Plain ``uv run`` is untouched, and is in
    fact already correct: measured, it leaves the shared venv alone and builds
    the worktree's own ``.venv``.

    **STILL OPEN.** A command that relies solely on the inherited value with
    no ``--active`` and no assignment (bare ``uv pip install foo`` with a
    ``PATH`` that does not itself resolve to the shared venv) is still not
    caught, for the false-positive reason above. So is
    ``uv run --python <shared>/bin/python …``: ``--python`` is already in
    `_TARGET_FLAGS`, so it would be caught the moment ``run`` counted as a
    mutating subcommand, but making ``run`` mutating in general puts every
    ordinary ``uv run pytest -q`` through target resolution, which is the
    broad reading this register warns about. Closing the remainder still
    wants the session's environment scoped BEFORE the command runs
    (``VIRTUAL_ENV``/``UV_PROJECT_ENVIRONMENT`` pinned to the worktree's own
    venv for the whole session) — capability-level. Note that pinning alone
    would ALSO not be enough: pip reads neither variable and follows ``PATH``
    (measured on PR #145), and a session pinned to an EMPTY worktree venv
    would break ``uv run pytest`` for every task, which is the cost that
    keeps this at the capability level rather than the environment level.

None of these can be closed by adding a smarter pattern — the information
needed does not exist before the command runs. The real fix for this
residual set is CAPABILITY-level: run coder sessions with the shared dev
venv not writable by the session's UID (or a read-only bind-mount) and
with ``VIRTUAL_ENV``/``UV_PROJECT_ENVIRONMENT`` pinned to the worktree's
own venv for the whole session — not attempted in this ticket.

  - **Bugfix (this ticket): an unreadable ``pyvenv.cfg`` used to ALLOW.**
    ``chmod`` on a venv directory (e.g. ``chmod 600``/``chmod -x``) strips
    its execute bit, so anything inside it cannot be stat'd, while the
    directory itself still stats fine from its parent. ``os.path.isfile``
    swallows the resulting ``PermissionError`` and reports ``False`` —
    indistinguishable from "no ``pyvenv.cfg`` here" — so ``_venv_root_of``
    concluded "owns no venv" and the shared venv stopped being a write
    candidate. The bare-token branch of ``_resolve_installer`` had the
    same swallow one layer deeper: it resolved PATH entries via
    ``shutil.which``, which calls ``os.path.exists`` internally and
    reports the same ``PermissionError`` as "not found" — and because
    this branch runs BEFORE the venv-root probe below it, a bare ``pip
    install evilpkg`` (the spelling a coder actually types, no explicit
    path) never reached ``_venv_root_of`` at all: it fell through to the
    unresolvable-installer allow-and-log branch instead. Fixed by probing
    tri-state (``_probe_is_file``/``_probe_is_dir``: ``True``/``False``/
    ``None`` = "undetermined") and failing closed (``is not False``) at
    every site that used to ask ``os.path.isfile``/``os.path.isdir``/
    ``shutil.which`` directly (``None`` means undetermined). Scoped
    honestly: in the DEFAULT layout the primary checkout's own ``.venv``
    was never the hole (``guard._protected_venvs``'s ``is_dir`` branch is
    unaffected by this particular ``chmod``) — what this closes is the
    structural resolution in this module (``_venv_root_of``,
    ``_resolve_installer``'s explicit-path AND bare-token branches, the
    ``--python``/directory-token probes above) and the ``sys.prefix``
    backstop in ``guard.py``, all of which failed open on exactly this
    input before this fix.
"""

from __future__ import annotations

import logging
import os
import shlex
import stat
from pathlib import Path, PurePosixPath
from typing import Mapping

from . import exec_names, win_readings

#: Flipped by tests; see `win_readings` for why both spellings are read.
_IS_WINDOWS = win_readings._IS_WINDOWS

_LOG = logging.getLogger(__name__)

#: The extension list cpython's `shutil.which` falls back to when `PATHEXT`
#: is unset OR empty (`shutil._WIN_DEFAULT_PATHEXT`). Held as a tuple, not the
#: `;`-joined string the stdlib keeps, so reading it does not depend on
#: `os.pathsep`: the stdlib spelling is a Windows literal whose separator is
#: `;` by definition, while `os.pathsep` is `:` on the POSIX hosts that run
#: this suite with `_IS_WINDOWS` monkeypatched. Pinned against the stdlib's
#: own value by a test, so it cannot drift silently.
_WIN_DEFAULT_PATHEXT = (".COM", ".EXE", ".BAT", ".CMD", ".VBS", ".JS", ".WS", ".MSC")

#: Shell interpreters whose ``-c``/``-lc`` argument is a script to execute —
#: recursion is scoped to these so `echo "pip install foo"` (argument text
#: that is never executed) is never mistaken for an invocation.
# `cmd`/`powershell`/`pwsh` sit beside the POSIX five because laundering a
# payload through a nested shell is verdict 1 of the three review rounds this
# module exists to survive, and on Windows those are the shells that do it.
# Names are matched through `_basename`, so `bash.exe` and a fully-spelled
# `C:/Program Files/Git/bin/bash.exe` reach the same entry (issue #105 round 2:
# `PurePosixPath(tok).name` matched the bare five ONLY, so `bash.exe -c "..."`
# and `cmd /c "..."` walked past both readings with the payload intact -- and
# silently, because a payload with spaces resolves to no installer name).
_SHELL_RUNNERS = frozenset({"sh", "bash", "zsh", "dash", "ksh",
                            "cmd", "powershell", "pwsh"})
#: POSIX shell script flags, compared EXACTLY. Case matters here and the two
#: sets must stay apart: `"-C".lower()` is `"-c"`, and `-C` is `--directory`
#: in this module's own `_TARGET_FLAGS`. Folding the whole set turned
#: `<runner> pip -C <dir> install requests` into a stream where the payload
#: was emitted BEFORE the flag, so `_mutating_subcommand` read `<dir>` as
#: pip's subcommand instead of `install` and the command was allowed --
#: measured refused on main, on POSIX, with no Windows anywhere in it.
_POSIX_SCRIPT_FLAGS = frozenset({"-c", "-lc", "-cl", "--command"})

#: cmd/PowerShell switches, compared case-INSENSITIVELY, because those shells
#: are: `/C` and `-Command` are the same flag as `/c` and `-command`. None of
#: these collides with a flag this module gives another meaning, which is what
#: makes folding safe HERE and unsafe above.
_WINDOWS_SCRIPT_FLAGS = frozenset({"/c", "/k", "-command"})

_SCRIPT_FLAGS = _POSIX_SCRIPT_FLAGS | _WINDOWS_SCRIPT_FLAGS


def _is_script_flag(tok: str) -> bool:
    """Whether `tok` hands the NEXT token to a shell as a script to run."""
    return tok in _POSIX_SCRIPT_FLAGS or tok.lower() in _WINDOWS_SCRIPT_FLAGS


_SEGMENT_BREAKS = frozenset({";", "&", "&&", "||", "|", "(", ")", "{", "}"})
_MAX_RECURSE_DEPTH = 3

_EXACT_INSTALLERS = frozenset({"pip", "pip3", "uv", "uvx", "python", "python3"})
_VERSIONED_PREFIXES = ("pip3.", "python3.")

#: Subcommands that mean "this RESOLVED installer will mutate an
#: environment" — pip's {install, uninstall} and uv's {sync, add, remove,
#: install, uninstall}. `run`/`venv`/`tool` were REMOVED (structural-guard
#: v2 review round, this ticket): they over-blocked any command that merely
#: *mentioned* one of these words as an argument (`pip show sync`, `pip list
#: -v` matching nothing, but `uv run pytest -k add` matching "add"), because
#: the old intent test was bare-token membership — `any(tok in
#: _MUTATING_VERBS for tok in tokens)` — which cannot distinguish a verb
#: naming the ACTUAL subcommand of a resolved installer invocation from the
#: same word appearing anywhere else in the command (an argument, a grep
#: pattern, another program's own subcommand). Intent is now decided
#: structurally instead: for every token that RESOLVES to an installer
#: executable, `_mutating_subcommand` walks right from that token, past
#: flags/values/inner-installer-names, to the subcommand it would actually
#: invoke, and only THAT word is tested against this set. `uv run pip
#: install foo` is still denied — not through "run", but through the inner
#: `pip` token's own adjacent "install". `uv run pytest -q` is now
#: correctly allowed: "run"'s own subcommand is "pytest", never a member of
#: this set, and pytest itself never resolves as an installer.
_MUTATING_SUBCOMMANDS = frozenset({"install", "uninstall", "sync", "add", "remove"})

#: Flags whose value names an install target, in both `--flag value` and
#: `--flag=value` forms. `--python`/`-p` names an interpreter file, not a
#: directory, so its value is resolved through `_venv_root_of` like a
#: resolved installer rather than used as a directory literally.
_TARGET_FLAGS = frozenset({
    "--target", "-t", "--prefix", "--root", "--python", "-p",
    "--project", "--directory", "-C",
})

#: Alias: the same flags, named for their role in `_mutating_subcommand`'s
#: walk (their value is a separate token to be skipped, not the subcommand).
_VALUE_FLAGS = _TARGET_FLAGS

#: uv subcommands that hand the rest of the line to ANOTHER PROGRAM. After the
#: program's own token, a flag belongs to that program and not to uv, so
#: `uv run nh learnings --active` is this repo's own CLI flag. Every other
#: subcommand (`add`, `sync`, `remove`, `lock`, `export`, ...) invokes nothing,
#: so a flag anywhere on those lines is uv's.
_PROGRAM_INVOKING_SUBCOMMANDS = frozenset({"run"})

#: Flags that tell uv NOT to sync the project into the target environment.
#: The sync is the only reason `--active` is intent at all: measured against
#: uv 0.12.5 from inside a worktree whose VIRTUAL_ENV named the shared
#: checkout, plain `--active` moved the shared venv's editable `.pth` to the
#: worktree, while `--active --no-sync` and `--active --no-project` left it
#: exactly where it was.
_NO_SYNC_FLAGS = frozenset({"--no-sync", "--no-project"})

_ENV_VARS = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")

#: uv's opt-in to the ALREADY-ACTIVE environment, i.e. the inherited
#: ``VIRTUAL_ENV``, in place of the project's own ``.venv``.
#:
#: This is the one flag that makes an inherited value authoritative, and it is
#: why the residual register's "relies SOLELY on an inherited VIRTUAL_ENV"
#: exclusion does not cover it: ``--active`` is a COMMAND-LEVEL statement of
#: intent, exactly the class of signal this module already trusts in a
#: ``VIRTUAL_ENV=<path> pip install`` assignment. Trusting it here therefore
#: does not reintroduce the false positives that shaped the current design;
#: plain ``uv run``, ``uv sync`` and ``uv run pytest -q`` are untouched.
#:
#: It matters because ``uv run`` SYNCS the project into its target environment
#: before running anything, so the command need not be install-shaped to
#: rewrite a venv. Measured on 2026-09-09 in a throwaway repo, from inside a
#: linked worktree with ``VIRTUAL_ENV`` naming the primary checkout's venv::
#:
#:     uv run --active python -c "print('ran')"
#:     -> "Uninstalled 1 package / Installed 1 package"
#:     -> the SHARED venv's .pth moved from <primary>/src to <worktree>/src
#:
#: and the control, the same command without ``--active``, left the shared
#: venv untouched and built the worktree's own ``.venv`` instead. That is the
#: coder-session half of issue #128.
_ACTIVE_FLAG = "--active"

_UNRESOLVABLE_CHARS = ("$", "`")


def _basename(path: str) -> str:
    r"""The command name `path` spells, with a `.exe` suffix removed.

    `PurePosixPath(...).name`, NOT `os.path.basename`, for two reasons:

    * Trailing separators. `os.path.basename("/bin/sh/")` is `""`, which is in
      no name set, so `_flatten` stopped recognising the token as a shell
      runner and never expanded the payload behind its `-c`. Measured as a
      DENY->ALLOW on `/bin/sh/ -c "pip -C <dir> install requests"`, which a
      shell runs exactly as `/bin/sh -c ...`.
    * Host independence. `os.path.basename` splits on `\` on Windows and not
      on POSIX, so the same string would reach different verdicts on
      different machines while CI runs POSIX only. `PurePosixPath` reads `/`
      on every host, and that is the right reading here precisely because
      `win_readings.readings` has already offered the `/`-normalised spelling
      of any backslashed command by the time this is called.
    """
    name = PurePosixPath(path).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _is_installer_name(name: str, cwd: str | None = None) -> bool:
    # Folded on Windows, where the filesystem always is, AND wherever this
    # HOST's filesystem folds case (#328) -- macOS ships APFS
    # case-insensitive by default, so `PIP install evilpkg` really runs `pip`
    # there too, landing the install in the very venv this guard protects.
    # The old reasoning here ("on POSIX `PIP` is a genuinely different
    # file") is true of a case-SENSITIVE filesystem only; measured on the
    # macOS default: `PIP install evilpkg` -> ALLOW while `pip install
    # evilpkg` -> DENY, same fixture, same PATH. Round 3 of #105 found
    # `…\Scripts\PIP.EXE install requests` allowed while the lowercase
    # spelling was refused; #328 is the same shape on a different host class.
    if _IS_WINDOWS or exec_names.host_folds_case(cwd):
        name = name.lower()
    if name in _EXACT_INSTALLERS:
        return True
    return any(
        name.startswith(prefix) and name[len(prefix):].replace(".", "").isdigit()
        for prefix in _VERSIONED_PREFIXES
    )


def _lex(text: str) -> list[str]:
    """Quote-aware token stream for one command string.

    `punctuation_chars=True` makes `;`, `&`, `&&`, `||`, `(`, `)` their own
    tokens even when glued to a word (`sync;` -> `sync`, `;`); text inside
    quotes is never split. This is the reverse order from a guard that
    splits on a raw separator regex first and tokenises second — that order
    is exactly what let a separator hidden inside a quoted payload (verdict
    2) and a punctuation run (verdict 3) through.
    """
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        # Unbalanced quote — never let a malformed command escape analysis;
        # fall back to a plain split so downstream checks still run (and,
        # for an install-intent command, still fail closed on it).
        return text.split()


#: Cap on how many leading tokens of a nested payload are rejoined. A guard
#: must not become a parser with unbounded work.
_MAX_PREFIX_JOIN = 8


def _spaced_path_candidates(payload: str) -> list[str]:
    """The installer path a nested payload's own quoting would have kept whole.

    Round 3 of #105. `cmd /c "C:\\Program Files\\proj\\.venv\\Scripts\\pip.exe
    install requests"`: the OUTER lex consumes the payload's quoting, so
    re-lexing it splits the path at the space in `Program Files` and the
    installer token is destroyed -- in BOTH readings, because the split is at
    the SPACE, not the separator. The alternate reading cannot help, and the
    miss is silent: `Filesproj.venvScriptspip.exe` names no installer, so not
    even the WARNING fires.

    `cmd` itself resolves the longest leading prefix that names an executable.
    This rebuilds those prefixes and keeps only one that actually names an
    installer, emitting it followed by its own arguments so the adjacent
    mutating-subcommand check still sees `install` next to it.

    WHAT THIS DOES NOT COVER -- issue #312, measured, not supposed:

    * it runs only on a NESTED payload, so a top-level
      `C:\\Program Files\\p\\.venv\\Scripts\\pip install x` is untouched;
    * it anchors at token 0, so `cd X && <spaced path>\\pip install x` and the
      `echo ... &&` / `timeout 5` / `env -i` / `VAR=1` forms move the installer
      off the front and are untouched;
    * `_MAX_PREFIX_JOIN` is a bound, and therefore also a limit: a path with 8
      or more spaces is not reconstructed. Removing the bound is O(n^2)
      (measured: 4x per doubling), so raising it is not the fix either.

    Reconstructing lost quoting by guessing token boundaries fights an
    information loss; #312 carries the class and sketches two approaches that
    do not. This is kept because the shapes it DOES close are real, not
    because it closes the class.
    """
    toks = _lex(payload)
    out: list[str] = []
    for k in range(2, min(len(toks), _MAX_PREFIX_JOIN) + 1):
        joined = " ".join(toks[:k])
        # `cwd` is not lexically available here (this helper only sees the
        # raw payload text). Left at the default, `_is_installer_name` falls
        # back to `exec_names.host_folds_case(None)`, which unions the
        # PROCESS's own cwd with PATH -- NOT a superset of a call-site-scoped
        # answer: measured, `host_folds_case()` (no cwd) can be `False` while
        # `host_folds_case(<call-site cwd>)` is `True` for a specific cwd
        # whose volume folds even though the process cwd's does not. This is
        # a real, accepted narrowing (not a hole reachable by any corpus row
        # measured so far) rather than the "can only fold more" guarantee
        # this comment used to claim.
        if _is_installer_name(_basename(joined)):
            out.append(joined)
            out.extend(toks[k:])
    return out


def _flatten(text: str, _depth: int = 0) -> list[str]:
    """The full token stream for `text`, with shell-runner script arguments
    recursively expanded in place. Positionless by construction: the
    resulting list is one flat multiset in which no caller ever asks "what
    is argv[0]" — `(`, `\\n\\n`, `&`, `;` and a quoted payload are all
    structurally irrelevant to what gets resolved next.
    """
    tokens = _lex(text)
    if _depth >= _MAX_RECURSE_DEPTH:
        return tokens
    out: list[str] = []
    seen_runner = False
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in _SEGMENT_BREAKS:
            seen_runner = False
            out.append(tok)
            i += 1
            continue
        name = _basename(tok)
        if name.lower() in _SHELL_RUNNERS:
            seen_runner = True
        if seen_runner and _is_script_flag(tok) and i + 1 < n:
            out.extend(_flatten(tokens[i + 1], _depth + 1))
            out.extend(_spaced_path_candidates(tokens[i + 1]))
            seen_runner = False
            out.append(tok)
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def _looks_like_path(token: str) -> bool:
    return token in (".", "..") or "/" in token


def _join(cwd: str | None, value: str) -> str:
    if os.path.isabs(value):
        return value
    return os.path.join(cwd or "", value)


def _safe_realpath(path: str) -> str | None:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):  # pragma: no cover - defensive
        return None


def _probe_is_file(path: str) -> bool | None:
    """True = is a regular file; False = definitively NOT there (or not a
    file); None = COULD NOT BE DETERMINED (e.g. a `chmod` that blocks
    stat'ing it or a parent directory).

    Deliberately not `os.path.isfile`, which catches every `OSError` inside
    the stdlib and reports `False` — making "unreadable" indistinguishable
    from "absent". Callers that must fail closed on an undetermined probe
    test `is not False`.
    """
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        return None


def _probe_is_dir(path: str) -> bool | None:
    """Same tri-state contract as `_probe_is_file`, for directories."""
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        return None


def _venv_root_of(exe_path: str) -> str | None:
    """The venv root owning `exe_path` (`<root>/pyvenv.cfg` exists), or None
    ONLY when a venv is definitively absent there.

    A filesystem probe, not a text match — this is what lets `source
    .../activate && pip install foo` resolve correctly even though
    `activate` is never itself treated as an installer: `pip` resolves via
    PATH to the same venv's `bin/pip`, and this probe finds its root.

    Tri-state via `_probe_is_file`: a root whose `pyvenv.cfg` cannot be
    determined (`None` — e.g. the venv directory's execute bit was
    stripped) is treated as A VENV ROOT, not as "no venv here" — a root
    that cannot be determined must fail closed so the write candidate is
    kept and the install refused. Only a determinate `False` (no
    `pyvenv.cfg` at all) returns `None` here. (The old `except OSError:
    pass` this replaced was unreachable dead code: `os.path.isfile`,
    which it guarded, already swallows every `OSError` itself one line
    above — that swallow, not a missing guard, was the bug.)
    """
    parent = os.path.dirname(exe_path)
    root = os.path.dirname(parent)
    if not root:
        return None
    if _probe_is_file(os.path.join(root, "pyvenv.cfg")) is not False:
        return _safe_realpath(root) or root
    return None


def _is_worth_remembering(real: str, cwd: str | None) -> bool:
    """Is `real` — a PATH candidate whose file type could NOT be determined —
    the one to remember as the possible install target?

    Skipping a venv INSIDE `cwd` is not an optimisation, it is the selection
    rule: among candidates the guard admits it cannot read, prefer the one
    that would be DENIED. A venv inside the session's own worktree would be
    waved through, so keeping it discards the only candidate whose verdict
    differs from the default.

    Without this rule the caller repeats, one level in, the bug this
    remembering was added to fix: first-one-wins over a set the code cannot
    distinguish, so PATH ORDER ALONE decides ALLOW vs DENY between entries
    that are equally indeterminate. Measured with two venvs both chmod'd
    0600: own venv first gave ALLOW, foreign venv first gave DENY, same two
    directories. And the bad ordering is the ORDINARY production one, not a
    contrived PATH -- `uv run` prepends the project's own venv ahead of the
    inherited shared venv, so a coder session running under it manufactures
    own-venv-first for free.
    """
    root = _venv_root_of(real)
    if not root:
        return False
    cwd_real = _safe_realpath(cwd) if cwd else None
    return not (cwd_real and _is_within(root, cwd_real))


def _displaced_by_indeterminate_venv(
    token: str, real: str, venv_fallback: str | None
) -> str | None:
    """The remembered indeterminate venv when it must displace the
    determinate winner `real`, else None.

    A determinate match settles the install TARGET only when that binary
    belongs to a venv. When it does not -- a system `pip` -- resolution has
    NOT established where the install lands, and an earlier PATH entry that
    could not be stat'd is the one piece of evidence it was unable to rule
    out. Preferring it is fail-closed on an indeterminate read, which is this
    module's standing rule for a tri-state probe; skipping it reports "no venv
    is involved", a positive claim nothing established, and the `chmod` buys
    the ALLOW.

    DO NOT restate this as "an installer outside a venv installs into
    whatever VIRTUAL_ENV names". That was the first justification written
    here and it is FALSE, measured three ways: `pip` decides venv membership
    by `sys.prefix != sys.base_prefix` (its own `_internal/utils/virtualenv.py`)
    and never reads the variable -- `grep -rn VIRTUAL_ENV pip/_internal`
    returns nothing against a 9-file positive control, and
    `VIRTUAL_ENV=/tmp/fake /usr/bin/python3` still reports the system purelib;
    and `uv sync` prints "does not match the project environment path `.venv`
    and will be ignored". Only `uv pip` honours it. The justification is
    indeterminacy, not a claim about any installer's target-selection rule.

    Measured on a Linux runner, where `/usr/bin/pip` exists and on the macOS
    dev host it does not: `PATH=<foreign venv>/bin:/usr/bin:/bin` with that
    venv chmod'd 0600 resolved to `/usr/bin/pip`, owning venv None, and
    `pip install evilpkg` flipped DENY -> ALLOW.

    Requiring the WINNER to own no venv is what keeps an unreadable decoy
    ahead of the session's OWN venv resolving to the session's venv: a
    determinate, venv-owning answer wins outright.

    A known and accepted imprecision, stated rather than hidden:
    `_venv_root_of` treats an UNDETERMINED `pyvenv.cfg` probe as a venv root
    (see its docstring), so a non-venv directory whose own parent is
    unreadable reads as a venv here and produces a DENY for a path no install
    would reach. That is the fail-closed side of a deliberate tri-state and it
    is not a bug, but it means this branch cannot claim to fire only on real
    venvs.
    """
    if venv_fallback is None or _venv_root_of(real) is not None:
        return None
    _LOG.warning(
        "venv guard: %r resolved via PATH to %r, which belongs to no "
        "virtualenv, while an earlier PATH entry %r could not be stat'd "
        "(permission denied?) and so could not be shown NOT to be a "
        "virtualenv; using the earlier entry, because this resolution has "
        "not established where the install would land",
        token, real, venv_fallback,
    )
    return venv_fallback


def _resolve_installer(token: str, cwd: str | None, env: Mapping[str, str]) -> str | None:
    """The canonical, symlink-followed path of `token` iff it resolves to an
    existing installer executable — a property of the RESOLVED file's
    basename, never a text match against the command.

    When `token` names an installer but cannot be resolved (not on `PATH`,
    or a relative/explicit path that does not exist), structure genuinely
    cannot decide — so this is the one allow-and-log fallback in the module
    (criterion: no new lexical pattern to close this gap): the command is
    allowed, and the miss is logged at WARNING so it stays observable.
    """
    try:
        if "/" in token:
            real = _safe_realpath(_join(cwd, token))
            if real and _is_installer_name(_basename(real), cwd):
                probe = _probe_is_file(real)
                # `None` (undeterminable — e.g. a `chmod` on the venv
                # directory two levels up makes even stat'ing this file
                # raise `PermissionError`) is NOT the same as `False`
                # (genuinely absent): a resolution that cannot be verified
                # must still count as resolved, or the install-target probe
                # downstream (`_venv_root_of` via `_effective_prefixes`)
                # never sees this installer at all.
                if probe is not False:
                    if probe is None:
                        _LOG.warning(
                            "venv guard: %r resolved to %r but its file "
                            "type could not be verified (permission "
                            "denied?); treating it as a resolved installer "
                            "rather than assuming it is absent", token, real,
                        )
                    return real
            if _is_installer_name(_basename(token), cwd):
                _LOG.warning(
                    "venv guard: %r names an installer but could not be "
                    "resolved via PATH; allowing", token,
                )
            return None
        # `_basename`, not the raw token: it strips `.exe`, so `pip.exe` and
        # `uv.exe` -- what a real Windows venv's Scripts/ actually contains --
        # reach the same verdict as their POSIX spellings. This was the SECOND
        # site left on the raw name; round 2 of #105 fixed only the one in
        # `_flatten` and its commit message claimed there was one. Without it
        # this returns before `shutil.which` AND before the WARNING, so a bare
        # `pip.exe install foo` was allowed in silence.
        if not _is_installer_name(_basename(token), cwd):
            return None
        # Deliberately NOT `shutil.which`: it resolves via `os.path.exists`
        # internally, which swallows `PermissionError` exactly like
        # `os.path.isfile` did above — a `chmod` on a `PATH` directory (or
        # the venv it leads into) would make `which` report "not found",
        # indistinguishable from "genuinely not on PATH", and this bare-
        # token spelling (`pip install evilpkg`, no explicit path) is the
        # one a coder actually types. Walked by hand with `_probe_is_file`
        # so an undetermined probe still counts as resolved.
        #
        # `env.get("PATH")` returning `None` (the key is simply absent from
        # a caller-supplied `env` mapping) is not the same as an explicit,
        # empty `PATH=""` — `shutil.which(path=None)` falls back to the
        # real process `PATH` in that case, and this mirrors it, so a caller
        # that omits the key entirely searches the same PATH trunk did.
        path_value = env.get("PATH")
        if path_value is None:
            path_value = os.environ.get("PATH", os.defpath)
        # PATHEXT parity with `shutil.which`: on Windows a bare `pip` on
        # `PATH` names `pip.exe` on disk, not a file literally called `pip`.
        # `shutil.which` expands `PATHEXT` internally; hand-walking `PATH`
        # without doing the same would make `os.path.join(directory, token)`
        # name a file that never exists, so the bare-token spelling this
        # module's own comment calls "the spelling a coder actually types"
        # would stop resolving on Windows and fall through to allow-and-log.
        # Read the way cpython's win32 branch reads it, which is not the same
        # as splitting the raw value — three divergences, each measured as a
        # DENY->ALLOW on a Windows-shaped venv before this spelling:
        #
        #   * `os.getenv("PATHEXT") or _WIN_DEFAULT_PATHEXT` — an UNSET *or
        #     EMPTY* `PATHEXT` falls back to the DEFAULT extension list. It
        #     does not mean "no expansion": `cmd.exe` still runs `pip.EXE`
        #     when `PATHEXT` is empty, so a guard that stops expanding there
        #     simply stops resolving the installer.
        #   * `[... for ext in pathext if ext]` — an EMPTY entry is DROPPED,
        #     matching `which`. DO NOT REMOVE THIS FILTER. It is load-bearing
        #     for the ORDER condition below, which asks whether the token
        #     already ends with a PATHEXT entry — and `"PIP".endswith("")` is
        #     TRUE FOR EVERY TOKEN. So one empty entry, which the ordinary
        #     Windows spelling `.COM;.EXE;.BAT;` produces, would flip every
        #     token to bare-token-first and reinstate the false DENY that
        #     order exists to prevent. Measured on the row-H shape: with the
        #     filter, `.COM;.EXE;.BAT;` resolves the OWN venv (ALLOW); with
        #     `if True` it resolves the FOREIGN one (DENY).
        #   * `ext.rstrip('.')` — a trailing dot is stripped.
        #
        # The ONE place this deliberately does not copy `which`: the bare
        # token is ALWAYS tried as well, not only when it already carries one
        # of those extensions (`which` adds it conditionally, via
        # `files.insert(0, cmd)`). `which`'s job is to predict what will
        # execute, so declining to consider a candidate is free; this
        # function's job is to find every path an install could WRITE
        # through, and an unconsidered candidate is a silent ALLOW.
        # Measured: making it exact parity instead turned 15 existing
        # `test_windows_command_readings.py` cases (a POSIX-named `pip` on a
        # `PATH` read with `_IS_WINDOWS` true) from DENY to ALLOW.
        #
        # ORDER IS LOAD-BEARING, and a wider candidate SET is not by itself
        # safe. `_resolve_installer` returns the FIRST match, so an extra
        # candidate placed ahead of `which`'s own winner REPLACES it rather
        # than adding to it. With the bare token first, a single Windows
        # `PATH` directory holding both `pip` (foreign venv) and `pip.EXE`
        # (this session's venv) resolved to the foreign one and produced a
        # DENY the shell would never have earned — and its mirror produced a
        # fresh ALLOW. The order below therefore mirrors `which`'s CONDITION
        # rather than picking a fixed side of it.
        if _IS_WINDOWS:
            pathext_value = env.get("PATHEXT")
            if pathext_value is None:
                pathext_value = os.environ.get("PATHEXT")
            if pathext_value:
                pathext = [
                    ext.rstrip(".")
                    for ext in pathext_value.split(os.pathsep)
                    if ext
                ]
            else:
                pathext = list(_WIN_DEFAULT_PATHEXT)
            # `which`'s own order is CONDITIONAL: it puts the bare token
            # FIRST when the token already ends with a PATHEXT entry, and
            # omits it entirely otherwise. Neither fixed order can match
            # both cases -- measured, each fixed order produces a false
            # DENY in the case the other gets right. So mirror the
            # condition, and APPEND the bare token in the branch `which`
            # omits it: that append is what carries the 15
            # `test_windows_command_readings.py` refusals, and deleting it
            # turns them red.
            suffixes = list(pathext)
            if any(token.upper().endswith(ext.upper()) for ext in pathext):
                suffixes.insert(0, "")
            else:
                suffixes.append("")
        else:
            suffixes = [""]
        # A candidate whose file type could not be determined (permission
        # denied on an ancestor directory, a dead NFS mount, ...) is kept as
        # a FALLBACK rather than returned immediately: a `PATH` entry that
        # merely could not be stat'd must not pre-empt a LATER, determinate
        # match — a POSIX shell skips an EACCES entry and keeps walking, so
        # `command -v`/`type -p` resolve past it to the same later match a
        # determinate scan below finds. Returning the undetermined entry
        # first (measured: an unreadable directory placed ahead of the
        # session's own venv on `PATH`) resolved to a path that could never
        # execute and DENIED an install that would have gone into the
        # coder's own venv — a false DENY, not a fail-closed one. Only when
        # the WHOLE scan turns up no determinate match does the remembered
        # fallback get used, and even then it counts as "resolved" rather
        # than "absent" — undetermined must not collapse into
        # found-to-be-missing either.
        fallback = None
        venv_fallback = None
        directories = path_value.split(os.pathsep)
        if _IS_WINDOWS:
            # Same reading as the empty entry below, one platform over:
            # `shutil.which` prepends the current directory on win32
            # because that is what `cmd.exe` searches. `which` gates this on
            # `_win_path_needs_curdir` (`_winapi.NeedCurrentDirectoryForExePath`,
            # false when `NoDefaultCurrentDirectoryInExePath` is set); this
            # inserts unconditionally. On a real Windows host `_winapi` IS
            # importable, so this is a CHOICE, not an impossibility: it is the
            # POSIX hosts running this suite with `_IS_WINDOWS` monkeypatched
            # that cannot read it, and over-searching is the fail-closed side. That is a
            # deliberate over-search, not parity: with the registry key set,
            # this considers a directory the shell would skip. It is also
            # not certain to be the shell's cwd at exec time — `cd foo &&
            # pip install ...` moves it. Both are accepted as fail-closed.
            directories.insert(0, cwd or os.curdir)
        for directory in directories:
            if not directory:
                # An EMPTY `PATH` entry is THE CURRENT DIRECTORY, not a hole
                # to skip. POSIX: "a zero-length prefix ... indicates the
                # current working directory". cpython's `shutil.which` says
                # it outright — "PATH='' doesn't match, whereas PATH=':'
                # looks in the current directory" — and implements it by
                # leaving `os.path.join("", thefile)` relative. Skipping it
                # made `PATH=:<dir>` plus a `pip` in the session's own cwd
                # resolve to NOTHING and fall through to allow-and-log, while
                # the shell the coder types into runs that `pip`. Resolved
                # against the COMMAND's `cwd`, not this process's, because
                # that is where the command actually runs.
                directory = cwd or os.curdir
            for suffix in suffixes:
                candidate = os.path.join(directory, token + suffix)
                probe = _probe_is_file(candidate)
                if probe is False:
                    continue
                if probe is None:
                    real = _safe_realpath(candidate) or candidate
                    if _is_installer_name(_basename(real), cwd):
                        if fallback is None:
                            fallback = real
                        # Tracked SEPARATELY from `fallback`, and this is the
                        # whole point: `fallback` is the first undetermined
                        # candidate with an INSTALLER NAME, which is not the
                        # same thing as the first one that could be a venv. A
                        # single unreadable non-venv directory ahead of an
                        # unreadable venv claims the `fallback` slot, and the
                        # venv's evidence — the only reason to prefer an
                        # unstat'able entry at all — is then silently dropped.
                        # Measured: PATH=<unreadable non-venv>:<unreadable
                        # foreign venv>/bin:<sysbin> resolved to the system
                        # pip and ALLOWED `pip install evilpkg`, reinstating
                        # the DENY->ALLOW this whole branch exists to close.
                        if venv_fallback is None and _is_worth_remembering(
                                real, cwd):
                            venv_fallback = real
                    continue
                # probe is True: a real, stat'able candidate — `shutil.which`
                # also filters on executability before accepting a match, so
                # this mirrors that (not the swallowing part, just the filter).
                if not os.access(candidate, os.X_OK):
                    continue
                real = _safe_realpath(candidate)
                if real and _is_installer_name(_basename(real), cwd):
                    displaced = _displaced_by_indeterminate_venv(
                        token, real, venv_fallback)
                    if displaced is not None:
                        return displaced
                    return real
        # `venv_fallback` first, for the same reason it is preferred over a
        # determinate non-venv winner above: when NOTHING on PATH could be
        # stat'd, the candidates are still not interchangeable. Returning
        # whichever came first means PATH order decides ALLOW vs DENY across
        # entries the guard cannot read — the first-one-wins shape this
        # module has now had to close at three separate points. Preferring
        # the candidate that would be DENIED is the same rule applied to the
        # same ambiguity, so the two paths cannot disagree about one PATH.
        remembered = venv_fallback if venv_fallback is not None else fallback
        if remembered is not None:
            _LOG.warning(
                "venv guard: %r resolved via PATH to %r but its file type "
                "could not be verified (permission denied?); treating it "
                "as a resolved installer rather than assuming it is absent",
                token, remembered,
            )
            return remembered
        _LOG.warning(
            "venv guard: %r names an installer but could not be "
            "resolved via PATH; allowing", token,
        )
        return None
    except (OSError, ValueError):  # pragma: no cover - defensive
        return None


def _flag_value(tok: str, nxt: str | None, flags: frozenset[str]) -> str | None:
    for flag in flags:
        if tok == flag:
            return nxt
        prefix = flag + "="
        if tok.startswith(prefix):
            return tok[len(prefix):]
    return None


def _mutating_subcommand(tokens: list[str], start: int) -> str | None:
    """The subcommand `tokens[start]` (a RESOLVED installer) would invoke,
    or None.

    Scans right from the installer token, skipping (a) flags, (b) the value
    token of a value-taking flag (`_VALUE_FLAGS`), (c) `VAR=value`
    assignments, and (d) further installer NAMES (`uv pip install`, `python
    -m pip install` — the inner name is a sub-invocation prefix, not a
    subcommand). Stops at the first segment break: a subcommand never
    crosses `;`/`&&`/`|`/`(`/`)`/newline — the installer at `start` has no
    subcommand in that case.

    Positionless within THIS bounded walk only in the sense that flag/value
    adjacency (not argv index) drives it, same as `_effective_prefixes`'s
    own flag scan; it still starts from a specific resolved-installer
    position because "the subcommand of installer X" is inherently about
    that occurrence, not the whole flat token multiset.
    """
    n = len(tokens)
    i = start + 1
    while i < n:
        tok = tokens[i]
        if tok in _SEGMENT_BREAKS:
            return None
        head = tok.split("=", 1)[0]
        if "=" in tok and head in _ENV_VARS:
            i += 1
            continue
        if tok.startswith("-"):
            # `tok in _VALUE_FLAGS` is the EXACT-form match (`--directory`
            # followed by a separate value token, e.g. `--directory foo`) —
            # skip both. The `--flag=value` embedded form never matches this
            # membership test (the "=value" suffix makes it a different
            # string), so it correctly falls through to the plain +1 skip
            # below, its value already inside this one token.
            i += 2 if tok in _VALUE_FLAGS else 1
            continue
        # No `cwd` in scope here either (this walks an already-tokenised
        # list with no path context) — same accepted narrowing as
        # `_spaced_path_candidates` above: the default folds using the
        # process cwd + PATH union, which is NOT guaranteed a superset of a
        # narrower cwd-scoped answer (measured: `host_folds_case()` can be
        # `False` where `host_folds_case(<a specific cwd>)` is `True`), so
        # this can in principle deny less on that one cwd, not strictly more.
        if _is_installer_name(tok):
            i += 1
            continue
        return tok
    return None


def _uses_active_env(tokens: list[str], start: int) -> bool:
    """True when the installer at `tokens[start]` is itself given `--active`.

    Scoped to that installer's OWN segment, exactly like
    `_mutating_subcommand` above and for the same reason its docstring gives:
    a flag reaches a process only through that process's argv, so it is about
    that occurrence and not about the whole flat token multiset.

    A whole-stream scan was the first version and it was wrong. Review of PR
    #195 measured it in a session whose VIRTUAL_ENV is the shared checkout,
    which is what a coder actually gets::

        echo --active && uv run pytest -q                  -> DENIED
        grep -- --active notes.txt && uv run pytest -q      -> DENIED

    Neither touches the shared venv, and the denial message named an install
    target that did not exist. The second is not contrived: a task working on
    this guard greps for the flag and then runs the suite on the same line.
    Being "only more conservative" is not harmless when the reason given is
    false.

    Both spellings are read, since `--active` takes no value: the bare flag,
    and a `--active=`-prefixed form for symmetry with the rest of this
    module's flag handling. `--no-active` is a real uv flag and is correctly
    left alone, because it is neither.

    THREE NARROWINGS, from measurement rather than reasoning. Review round 3
    pulled every distinct `--active` command out of the fleet's own history:
    128 of them, of which the first version denied 124, and 77 of those
    touched no shared venv at all. Each narrowing below removes one family
    without moving a single true denial.

    1. `--no-sync` / `--no-project` NEGATE, because the sync is the only
       reason `--active` is intent. 61 and 10 of the 77 respectively.
    2. A `VIRTUAL_ENV=` assignment PREFIXING this installer's own command
       clears it, because the command sets the variable for its own child. 6
       of the 77. Scoped to the prefix so `echo VIRTUAL_ENV= && uv run
       --active pytest` stays denied, the assignment being another command's.
    3. After a program-invoking subcommand's PROGRAM token, flags belong to
       the program: `uv run nh learnings --active` is this repo's own CLI
       flag, not uv's.

    Placement is the whole difficulty in (1) and (3), and getting it wrong
    reopens the incident rather than merely over-denying. A negation is only
    honoured while the walk is still certainly inside uv's OWN argv, so
    `uv run --active -- echo --no-sync` and
    `uv run --active python -m this --no-sync` stay DENIED: uv still syncs,
    and the flag there is the program's.
    """
    if _assignment_clears_active(tokens, start):
        return False

    n = len(tokens)
    subcommand = _mutating_subcommand(tokens, start)
    expects_program = (
        subcommand in _PROGRAM_INVOKING_SUBCOMMANDS
        or _basename(tokens[start]).startswith("uvx")
    )
    seen_subcommand = subcommand is None
    active = False
    i = start + 1
    while i < n:
        tok = tokens[i]
        if tok in _SEGMENT_BREAKS:
            break
        # `--` ends uv's own flags: everything after it is the program and its
        # arguments, so a negation there is not uv's to read.
        if tok == "--":
            break
        if tok.startswith("-"):
            if tok == _ACTIVE_FLAG or tok.startswith(_ACTIVE_FLAG + "="):
                active = True
            elif tok in _NO_SYNC_FLAGS:
                return False
            i += 2 if tok in _VALUE_FLAGS else 1
            continue
        head = tok.split("=", 1)[0]
        if "=" in tok and head in _ENV_VARS:
            i += 1
            continue
        if not seen_subcommand and tok == subcommand:
            seen_subcommand = True
            i += 1
            continue
        # Same reasoning as the other token-list walkers above: no `cwd`
        # is threaded through this call chain, so the default falls back
        # to the process cwd + PATH union — NOT guaranteed to fold (deny)
        # at least as much as a cwd-scoped answer would; see the note in
        # `_spaced_path_candidates` for the measured counter-example.
        if _is_installer_name(tok) and not expects_program:
            i += 1
            continue
        # A bare positional once a program is expected: this is the program,
        # and every flag after it is that program's own.
        if expects_program:
            break
        i += 1
    return active


def _assignment_clears_active(tokens: list[str], start: int) -> bool:
    """True when a `VIRTUAL_ENV=`/`UV_PROJECT_ENVIRONMENT=` assignment
    PREFIXES the installer at `tokens[start]`.

    An assignment binds to the command it prefixes, so the command is setting
    the variable for its own child and `--active` then names that value, not
    the session's. In `echo VIRTUAL_ENV= && uv run --active pytest` the
    assignment belongs to `echo`, and that shape stays denied.

    A prefix is an UNBROKEN run of assignments reaching back to the start of
    the command, so everything between the assignment and the installer must
    itself be an assignment. Requiring the run rather than merely finding an
    assignment somewhere behind is what closes a bypass this function shipped
    with and its own test caught: a NEWLINE separates tokens without emitting
    a `_SEGMENT_BREAKS` token, so in::

        echo VIRTUAL_ENV=
        uv run --active pytest -q

    the flat stream is `[echo, VIRTUAL_ENV=, uv, run, --active, ...]` with
    nothing between the assignment and `uv`. Scanning back for the nearest
    assignment read `echo`'s own ARGUMENT as uv's prefix and allowed the
    command. Walking back over assignments ONLY stops at `echo` and denies it,
    without depending on a break token that is not there.
    """
    seen_assignment = False
    i = start - 1
    while i >= 0:
        tok = tokens[i]
        if tok in _SEGMENT_BREAKS:
            return seen_assignment
        head = tok.split("=", 1)[0]
        if "=" in tok and head in _ENV_VARS:
            seen_assignment = True
            i -= 1
            continue
        return False
    return seen_assignment


def _effective_prefixes(
    tokens: list[str],
    cwd: str | None,
    installers: list[str],
    env: Mapping[str, str] | None = None,
    uses_active: bool = False,
) -> set[str]:
    candidates: set[str] = set()
    if cwd:
        real_cwd = _safe_realpath(cwd)
        if real_cwd:
            candidates.add(real_cwd)

    # VIRTUAL_ENV / UV_PROJECT_ENVIRONMENT — ONLY a command-level assignment
    # token (`VIRTUAL_ENV=<path> pip install ...`, verdict 1's laundering
    # trick) counts as a signal. The value merely INHERITED from `env` is
    # deliberately never used as a candidate: production backends inherit
    # `os.environ` unmodified, so every coder session's `VIRTUAL_ENV` is the
    # shared dev venv unconditionally, not just a laundering command's —
    # trusting it here denied ordinary commands (`uv sync`,
    # `uv run pytest -q`) in EVERY session (see the module's residual-risk
    # register for the resulting coverage gap and why it is not closed by
    # a pattern here).
    for tok in tokens:
        for var in _ENV_VARS:
            prefix = var + "="
            if tok.startswith(prefix):
                real = _safe_realpath(_join(cwd, tok[len(prefix):]))
                if real:
                    candidates.add(real)

    # `--active` is the ONE case where the inherited value becomes a signal,
    # and it does so for the same reason a command-level assignment does: the
    # command SAYS to use it. Without this the target of `uv run --active` is
    # unknown to this module and the command reads as harmless, which is the
    # coder-session half of #128.
    if env is not None and uses_active:
        for var in _ENV_VARS:
            value = env.get(var)
            if not value:
                continue
            real = _safe_realpath(_join(cwd, value))
            if real:
                candidates.add(real)

    # Explicit --target/--prefix/--root/--python/--project/--directory —
    # flag -> value adjacency is the only positional read in this module,
    # and it is order-independent within the flat stream: nothing about a
    # wrapper or a nested shell can shift a flag away from its own value.
    n = len(tokens)
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < n else None
        val = _flag_value(tok, nxt, _TARGET_FLAGS)
        if not val:
            continue
        joined = _join(cwd, val)
        real = _safe_realpath(joined)
        if not real:
            continue
        # `_venv_root_of` is asked about `joined` (the value AS GIVEN,
        # cwd-resolved but symlinks NOT followed) rather than `real` (fully
        # symlink-followed). `--python .venv/bin/python3` names a file
        # inside the worktree's own venv, but `.venv/bin/python3` is
        # routinely a SYMLINK to a base interpreter that lives entirely
        # elsewhere — uv's managed-Python cache (`~/.cache/uv/...`),
        # Homebrew, pyenv, `/usr/bin`, all bookkeeping/shared locations a
        # `uv`/venv invocation touches without them being the install
        # DESTINATION. Following that symlink before asking "which venv
        # owns this" answers "where does the interpreter file physically
        # live", not "which venv did the invocation name" — the wrong
        # question. A `pyvenv.cfg` two directories above the NAMED path
        # decides it instead: `.venv`'s own pyvenv.cfg is found before the
        # symlink is ever followed, so the worktree's own venv resolves
        # in-tree regardless of where its interpreter binary is symlinked
        # to. A value naming no venv at all (a bare system interpreter,
        # e.g. `--python /usr/bin/python3.11`) still falls through to
        # `real` unchanged, so a genuine out-of-tree target is still
        # blocked.
        owning = _venv_root_of(joined) if _probe_is_file(real) is not False else None
        candidates.add(owning or real)

    # The venv owning each resolved installer — for pip/python only. `pip`/
    # `python` always install into the environment their OWN resolved binary
    # belongs to, so that binary's owning venv is a real signal. `uv`/`uvx`
    # are not: uv is a project-based environment manager whose own binary
    # location is irrelevant to install target — it resolves the target via
    # `cwd` (nearest `pyproject.toml`, already a baseline candidate above),
    # an explicit --target/--prefix/--project flag, or a command-level
    # VIRTUAL_ENV assignment (both already captured above). Treating a
    # resolved `uv`'s own directory as a candidate is a false positive: a
    # production PATH commonly resolves `uv` from inside the shared dev
    # venv's `bin/` (it is installed there like any other tool) even though
    # `uv sync`/`uv run pytest -q` correctly target the worktree via `cwd`.
    for exe in installers:
        # `.lower()`, not a bare comparison: `_is_installer_name` (the check
        # that populated `installers` in the first place) already folds case
        # where the host folds it, so a bare `_basename(exe) in (...)` here
        # disagreed with its own upstream classifier -- `UV sync` measured as
        # an installer invocation but not as `uv` for this exclusion, so it
        # fell through to being treated like `pip`/`python` and got denied
        # even though `uv sync` (lowercase) is allowed on the identical host.
        if _basename(exe).lower() in ("uv", "uvx"):
            continue
        owning = _venv_root_of(exe)
        if owning:
            candidates.add(owning)

    # Every path-like token that resolves to an EXISTING directory — the
    # structural stand-in for `cd`/`pushd`/subshell-group operands. This
    # subsumes them without threading a running "current directory" through
    # segments, which is the mechanism that broke in verdict 3.
    for tok in tokens:
        if not _looks_like_path(tok):
            continue
        real = _safe_realpath(_join(cwd, tok))
        if real and _probe_is_dir(real) is not False:
            candidates.add(real)

    return candidates


def _is_within(path: str, root: str) -> bool:
    try:
        return Path(path).is_relative_to(root)
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False


def denial_reason(cmd: str, *, cwd: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """Why this command's install must be denied, or None to allow it.

    Structural, not lexical: this resolves canonical executable/target
    paths and compares them to `cwd` (the session's worktree). No text
    pattern is matched against `cmd` to make the allow/deny decision.

    On Windows a native path reaches POSIX `shlex` as an escape sequence and
    is destroyed before resolution is attempted (issue #105), so every
    spelling `win_readings.readings` offers is resolved and the FIRST denial
    wins. On POSIX, and for any command with no backslash in it, that is
    exactly one reading and this costs a list construction.
    """
    for reading in win_readings.readings(cmd, is_windows=_IS_WINDOWS):
        reason = _denial_reason_for_reading(reading, cwd=cwd, env=env)
        if reason is not None:
            return reason
    return None


def _denial_reason_for_reading(
    cmd: str, *, cwd: str | None, env: Mapping[str, str] | None = None
) -> str | None:
    """`denial_reason` for ONE spelling of the command. See its docstring."""
    if env is None:
        env = os.environ
    if not cmd or not cmd.strip():
        return None

    tokens = _flatten(cmd)
    if not tokens:
        return None

    resolved_positions = [
        (i, resolved) for i, tok in enumerate(tokens)
        if (resolved := _resolve_installer(tok, cwd, env)) is not None
    ]
    installers = [resolved for _, resolved in resolved_positions]
    if not installers:
        return None
    # Structural intent: NOT "does any token spell a mutating word"
    # (bare-token membership over-blocks `pip show sync`, `uv run pytest -k
    # add`) but "does a RESOLVED installer's own adjacent subcommand mutate"
    # — both halves (the executable and the subcommand) are structural, no
    # text pattern is matched against `cmd` for this decision.
    # `--active` is intent on its own, without a mutating SUBCOMMAND, because
    # `uv run` syncs the project into its target environment before running
    # whatever it was given: `uv run --active ruff check .` carries no install
    # word and still rewrites the active venv's editable path (#128). Adding
    # `run` to `_MUTATING_SUBCOMMANDS` would catch that too, and would also
    # catch `uv run --python <shared>/bin/python`, but it would put every
    # ordinary `uv run pytest -q` through the target resolution below, which
    # is the broad reading this module's residual register warns about. This
    # is the narrow half: `--active` is rare, explicit, and always names an
    # environment.
    uses_active = any(_uses_active_env(tokens, i) for i, _ in resolved_positions)
    intent = uses_active or any(
        _mutating_subcommand(tokens, i) in _MUTATING_SUBCOMMANDS
        for i, _ in resolved_positions
    )
    if not intent:
        return None

    for tok in tokens:
        if any(ch in tok for ch in _UNRESOLVABLE_CHARS):
            return (
                f"cannot verify this install's target: {tok!r} contains "
                "shell/variable expansion, so the effective install "
                "location cannot be resolved before the command runs. "
                "Spell the literal path instead (your worktree's own "
                ".venv)."
            )

    if cwd is None:
        return (
            "blocked: this command mutates a Python environment and no "
            "session worktree is known, so the target cannot be proven "
            "safe. Re-run it with a known cwd inside your worktree."
        )

    cwd_real = _safe_realpath(cwd)
    if cwd_real is None:
        return f"blocked: session worktree {cwd!r} could not be resolved."

    candidates = _effective_prefixes(tokens, cwd, installers, env, uses_active)
    if not candidates:
        return (
            f"blocked: could not resolve where this install would write, "
            f"so it cannot be proven safe: {cmd}"
        )

    outside = sorted(c for c in candidates if not _is_within(c, cwd_real))
    if outside:
        alt = os.path.join(cwd_real, ".venv", "bin", "python")
        return (
            f"install blocked: resolves to {outside[0]}, outside this "
            f"session's worktree ({cwd_real}) — not the worktree's own "
            f".venv. Installers must target the worktree's own .venv, e.g. "
            f"`{alt} -m pip install ...` or `uv sync` with no --python/"
            f"--target/--prefix/--project pointing elsewhere."
        )
    return None
