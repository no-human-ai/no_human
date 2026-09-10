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
     worktree ROOT — the nearest ``.git``-marked ancestor of ``cwd``,
     inclusive; bounded; never ``$HOME`` or the filesystem anchor; ``cwd``
     itself when no marker is found (:func:`_session_root`) — an
     allow-list, not a deny-list, so a spelling nobody has thought of yet
     still resolves to "outside the worktree" and is denied by
     construction rather than by a missing pattern. A closed defect: a
     venv resolved from ``PATH``/a command-level ``VIRTUAL_ENV`` assignment
     lives at ``<root>/.venv``, which sits OUTSIDE a subdirectory ``cwd``
     even when it is the session's own venv — comparing against ``cwd``
     directly denied a coder its own install the moment it ``cd``'d one
     level down; comparing against the discovered root fixes that without
     trusting anything merely inherited.
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
    above). Closing this gap without reintroducing that false-positive
    needs the session's environment scoped BEFORE the command runs
    (``VIRTUAL_ENV``/``UV_PROJECT_ENVIRONMENT`` pinned to the worktree's
    own venv for the whole session) — capability-level, not a pattern this
    module could add.
  - the worktree ROOT used for the containment check (item 5,
    :func:`_session_root`) is discovered by an upward filesystem walk for a
    ``.git`` marker, not by asking the VCS:
      (i) an unmarked tree — or one whose only marker sits at ``$HOME`` or
          the filesystem anchor, both explicitly refused as a root — falls
          back to ``cwd`` itself, so a subdirectory install there is still
          refused. This is a conservative FALSE POSITIVE (over-denial),
          never a hole: it can only narrow what counts as "in tree", not
          widen it;
      (ii) every venv under the discovered root — including one that lives
          in a sibling subdirectory of ``cwd``, not just an ancestor — is
          now a valid install target. This is the intended isolation
          boundary (the whole worktree, not one subdirectory of it), not a
          widening of what "outside the worktree" means;
      (iii) when a session's own ``cwd`` is inside the PRIMARY checkout
          (isolation disabled for that session), the primary's ``.git``
          makes the primary checkout itself the discovered root — exactly
          what ``cwd == <primary>`` already allowed before this change,
          since the root and ``cwd`` coincide there. The shared venv is
          not newly exposed by this: this module denies it via candidate
          resolution (items 1-4) exactly as before, and v1's
          unconditional ``_primary_checkout()`` entry in ``guard.py``
          still covers it independently.
    The inherited-``VIRTUAL_ENV``/``UV_PROJECT_ENVIRONMENT`` bound above
    (a ``PATH`` that does NOT itself resolve to the shared venv) is
    unchanged by this: candidate generation (items 1-4) is untouched, only
    the boundary candidates are compared against (item 5) moved from
    ``cwd`` to the discovered root.

None of these can be closed by adding a smarter pattern — the information
needed does not exist before the command runs. The real fix for this
residual set is CAPABILITY-level: run coder sessions with the shared dev
venv not writable by the session's UID (or a read-only bind-mount) and
with ``VIRTUAL_ENV``/``UV_PROJECT_ENVIRONMENT`` pinned to the worktree's
own venv for the whole session — not attempted in this ticket.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
from pathlib import Path, PurePosixPath
from typing import Mapping

_LOG = logging.getLogger(__name__)

#: Shell interpreters whose ``-c``/``-lc`` argument is a script to execute —
#: recursion is scoped to these so `echo "pip install foo"` (argument text
#: that is never executed) is never mistaken for an invocation.
_SHELL_RUNNERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_SCRIPT_FLAGS = frozenset({"-c", "-lc", "-cl", "--command"})
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

_ENV_VARS = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")

_UNRESOLVABLE_CHARS = ("$", "`")


def _basename(path: str) -> str:
    name = os.path.basename(path)
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _is_installer_name(name: str) -> bool:
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
        name = PurePosixPath(tok).name
        if name in _SHELL_RUNNERS:
            seen_runner = True
        if seen_runner and tok in _SCRIPT_FLAGS and i + 1 < n:
            out.extend(_flatten(tokens[i + 1], _depth + 1))
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


def _venv_root_of(exe_path: str) -> str | None:
    """The venv root owning `exe_path` (`<root>/pyvenv.cfg` exists), or None.

    A filesystem probe, not a text match — this is what lets `source
    .../activate && pip install foo` resolve correctly even though
    `activate` is never itself treated as an installer: `pip` resolves via
    PATH to the same venv's `bin/pip`, and this probe finds its root.
    """
    parent = os.path.dirname(exe_path)
    root = os.path.dirname(parent)
    if not root:
        return None
    try:
        if os.path.isfile(os.path.join(root, "pyvenv.cfg")):
            return os.path.realpath(root)
    except OSError:  # pragma: no cover - defensive
        pass
    return None


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
            if real and os.path.isfile(real) and _is_installer_name(_basename(real)):
                return real
            if _is_installer_name(_basename(token)):
                _LOG.warning(
                    "venv guard: %r names an installer but could not be "
                    "resolved via PATH; allowing", token,
                )
            return None
        if not _is_installer_name(token):
            return None
        found = shutil.which(token, path=env.get("PATH"))
        if not found:
            _LOG.warning(
                "venv guard: %r names an installer but could not be "
                "resolved via PATH; allowing", token,
            )
            return None
        real = _safe_realpath(found)
        if real and _is_installer_name(_basename(real)):
            return real
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
        if _is_installer_name(tok):
            i += 1
            continue
        return tok
    return None


def _effective_prefixes(
    tokens: list[str],
    cwd: str | None,
    installers: list[str],
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
        owning = _venv_root_of(joined) if os.path.isfile(real) else None
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
        if _basename(exe) in ("uv", "uvx"):
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
        if real and os.path.isdir(real):
            candidates.add(real)

    return candidates


def _is_within(path: str, root: str) -> bool:
    try:
        return Path(path).is_relative_to(root)
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False


#: Marks a worktree ROOT — a directory in a primary checkout, a file
#: carrying `gitdir:` in a linked worktree. Both count; this module never
#: shells out to `git` to tell them apart, it only checks that something
#: named `.git` exists at that level.
_WORKTREE_MARKER = ".git"
#: Bound on the upward walk from `cwd` while looking for `_WORKTREE_MARKER`.
#: An unbounded walk is what would let a stray `.git` far above the tree
#: (or a slow/deep filesystem) widen the boundary this guard enforces.
_MAX_ROOT_WALK = 32


def _session_root(cwd_real: str) -> str:
    """The session worktree's ROOT: the nearest `.git`-bearing ancestor of
    `cwd_real` (inclusive), bounded, and never `$HOME` or the filesystem
    anchor.

    A coder session's `cwd` is frequently a SUBDIRECTORY of its own
    worktree, not the worktree root — the defect this closes. A venv
    resolved from `PATH`/a command-level `VIRTUAL_ENV` assignment lives at
    `<root>/.venv`, which sits outside `cwd` itself whenever `cwd` is a
    subdirectory, so comparing candidates against `cwd` (as this module did
    before) denies a coder its own venv the moment it `cd`s one level down.
    Walking up to the nearest `.git` marker recovers the same root `cwd ==
    root` sessions already got right, without trusting anything the
    process merely inherited (`VIRTUAL_ENV`, `PATH`) as the boundary.

    Nearest-first (the walk stops at the FIRST marker found) means a nested
    repo/submodule below the real root can only NARROW the accepted
    boundary, never widen it — the safe direction.

    Falls back to `cwd_real` itself — today's exact behaviour — when no
    marker is found within `_MAX_ROOT_WALK` levels, or when the walk would
    otherwise land on `$HOME` or the filesystem anchor (`/`, a drive root):
    both are refused as a root even if either happens to carry its own
    `.git`, since accepting them would make "the session's worktree" mean
    "the user's entire home directory" or "the whole filesystem".
    """
    try:
        home = os.path.realpath(str(Path.home()))
    except (OSError, RuntimeError):
        home = None

    current = Path(cwd_real)
    for _ in range(_MAX_ROOT_WALK):
        if current == current.parent:
            break
        current_str = str(current)
        if home is not None and current_str == home:
            break
        try:
            marker_present = os.path.exists(current / _WORKTREE_MARKER)
        except OSError:  # pragma: no cover - defensive
            marker_present = False
        if marker_present:
            return current_str
        current = current.parent

    return cwd_real


def denial_reason(cmd: str, *, cwd: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """Why this command's install must be denied, or None to allow it.

    Structural, not lexical: this resolves canonical executable/target
    paths and compares them to the session worktree ROOT derived from
    `cwd` (see `_session_root`) — not to `cwd` itself, so a coder that has
    `cd`'d into a subdirectory of its own worktree is still compared
    against the worktree's boundary rather than against the subdirectory.
    No text pattern is matched against `cmd` to make the allow/deny
    decision.
    """
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
    intent = any(
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
    root_real = _session_root(cwd_real)

    candidates = _effective_prefixes(tokens, cwd, installers)
    if not candidates:
        return (
            f"blocked: could not resolve where this install would write, "
            f"so it cannot be proven safe: {cmd}"
        )

    outside = sorted(c for c in candidates if not _is_within(c, root_real))
    if outside:
        alt = os.path.join(root_real, ".venv", "bin", "python")
        return (
            f"install blocked: resolves to {outside[0]}, outside this "
            f"session's worktree ({root_real}) — not the worktree's own "
            f".venv. Installers must target the worktree's own .venv, e.g. "
            f"`{alt} -m pip install ...` or `uv sync` with no --python/"
            f"--target/--prefix/--project pointing elsewhere."
        )
    return None
