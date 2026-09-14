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

import json
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest

from no_human.agent import exec_names, guard
from no_human.agent.guard import evaluate


def _decide(command: str) -> bool:
    """True when the guard DENIES `command`."""
    decision = evaluate(
        "Bash", {"command": command},
        forbidden_paths=[], never_push_to=["main"], cwd=".", env={"PATH": ""})
    return not decision.allow


@pytest.fixture
def on_windows(monkeypatch):
    """Run the gate as a Windows host reads it, whatever host this is.

    `guard._IS_WINDOWS` is a module constant precisely so a test can flip it,
    which is what makes the Windows behaviour testable on a POSIX runner and
    the POSIX behaviour testable on a Windows one. Without this the rows below
    that cover the gated half pass on Windows and fail on CI, which is the same
    blind spot the bug itself lives in.
    """
    monkeypatch.setattr(guard, "_IS_WINDOWS", True)


@pytest.fixture
def on_posix(monkeypatch):
    monkeypatch.setattr(guard, "_IS_WINDOWS", False)


@pytest.fixture
def on_case_sensitive_host(monkeypatch):
    """A host where two spellings are two files, whatever this machine is."""
    monkeypatch.setattr(exec_names, "host_folds_case", lambda: False)


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
        # Every extension Windows executes, not only `.exe`. A CLI installed by
        # scoop or npm is spelled `gh.cmd`, which is the spelling users have.
        ("gh.cmd", "gh"),
        ("nh.bat", "nh"),
        ("gh.ps1", "gh"),
        ("git.com", "git"),
        ("GH.CMD", "gh"),
        # Win32 strips trailing dots when resolving.
        ("gh.", "gh"),
        ("nh.exe.", "nh"),
        # NTFS default data stream opens the same file.
        ("gh.exe::$DATA", "gh"),
        (r"C:\tools\gh.cmd::$DATA", "gh"),
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
        # On a case-SENSITIVE host `GIT` and `git` are different files, so
        # folding would be a text match masquerading as a structural one. The
        # host that folds is covered by `test_a_folding_host_reads_one_name`.
        ("GIT", "GIT"),
        ("/bin/sh/", "sh"),
        # A trailing dot and a colon are ordinary filename characters here, so
        # neither is stripped: doing so would name a different file.
        ("gh.", "gh."),
        ("gh.exe::$DATA", "gh.exe::$DATA"),
        # ...but the suffix set itself is ungated, like `.exe` (#107).
        ("gh.cmd", "gh"),
        ("", ""),
    ],
)
def test_posix_splits_only_on_slash_and_never_folds(token, expected):
    # `fold_case` pinned rather than left to the probe: this table is the
    # case-SENSITIVE host's contract, and it must read the same on a developer
    # machine whose own filesystem folds (#328).
    got = exec_names.command_name(token, is_windows=False, fold_case=False)
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
    assert exec_names.command_name("git.exe", is_windows=False, fold_case=False) == "git"
    assert exec_names.command_name("GIT", is_windows=True) == "git"
    assert exec_names.command_name("GIT", is_windows=False, fold_case=False) == "GIT"
    assert exec_names.command_name(r"a\b", is_windows=True) == "b"
    assert exec_names.command_name(r"a\b", is_windows=False, fold_case=False) == r"a\b"


def test_a_folding_host_reads_one_name():
    """The half #320 left open: on a case-insensitive filesystem `GIT` really
    IS `git`, so a name-set comparison against lowercase names has to fold or
    the capitalised spelling walks past every gate (#328). Separators stay
    ungated — folding case is not the same question as splitting a backslash.
    """
    assert exec_names.command_name("GIT", is_windows=False, fold_case=True) == "git"
    assert exec_names.command_name("GH.EXE", is_windows=False, fold_case=True) == "gh"
    assert exec_names.command_name("NH.Cmd", is_windows=False, fold_case=True) == "nh"
    assert exec_names.command_name(r"weird\Name", is_windows=False, fold_case=True) == r"weird\name"


def test_the_probe_measures_the_volume_it_is_asked_about(tmp_path):
    """Derived a second way so the probe cannot pass by asserting itself: write
    ONE spelling, then ask the OS whether the other names the same file.

    Note what this can and cannot catch. On a case-INSENSITIVE developer
    machine a probe hardcoded to `True` agrees with every measurement and
    survives; on a case-sensitive runner — which CI is — it fails here and at
    `test_the_probe_matches_this_host` below. The asymmetry is inherent: a host
    that folds cannot observe the difference between measuring and assuming
    that it folds.
    """
    import os

    written = tmp_path / "probe"
    written.write_text("x")
    other_spelling = tmp_path / "PROBE"
    folds_here = other_spelling.exists() and os.path.samefile(other_spelling, written)

    assert exec_names._folds_case(str(written)) is folds_here


def test_the_host_probe_is_measured_once_not_once_per_token(monkeypatch):
    """`host_folds_case` must do its filesystem work ONCE per process.

    It is read on every guarded command, and `guard._looks_like_git_push`
    reads it per TOKEN -- so a command of 4000 quoted arguments read it 4000
    times. The inner `_folds_case` memo did not help: `os.path.realpath`
    runs BEFORE it and lstats every path component, so the cache was only
    reached after paying the syscalls it exists to avoid.

    Measured: the realpath was within noise of the entire `case_flags()` cost,
    and `guard.evaluate` on that shape ran 0.132s unfixed against 0.036s with
    the probe cached. That was enough to push
    `test_unmask_is_one_pass_not_one_per_table_entry` past its 0.4s bound on a
    shared CI runner and turn trunk red.

    This counts realpath CALLS rather than timing them. The wall-clock bound that
    caught the regression is deliberately loose -- it asserts a shape, not a
    machine -- so it detected this once and should not be relied on to do it
    again.

    It asserts the PROPERTY ("probed once"), not the mechanism: the
    `cache_clear` calls below are optional, so an implementation that hoists
    the realpath to a module constant, or memoizes some other way, passes on
    its merits. An earlier version called `cache_clear()` unguarded, which
    made this fail with AttributeError under any non-`lru_cache` fix -- it
    would have rejected a correct alternative without ever reaching the
    assertion.
    """
    import os as _os
    calls = []
    real_realpath = _os.path.realpath
    monkeypatch.setattr(
        _os.path, "realpath",
        lambda p, *a, **k: (calls.append(p), real_realpath(p, *a, **k))[1])

    # Optional by design -- see the docstring. Clearing the OUTER cache does
    # not restore freshness anyway, because `_folds_case`'s memo survives it.
    clear = getattr(exec_names.host_folds_case, "cache_clear", lambda: None)
    clear()
    try:
        for _ in range(50):
            exec_names.host_folds_case()
    finally:
        clear()

    mine = [c for c in calls if str(c).endswith("exec_names.py")]
    assert len(mine) <= 1, (
        "host_folds_case re-probed the filesystem instead of answering from "
        f"its cache: {len(mine)} realpath calls for 50 invocations")


def test_the_probe_matches_this_host():
    """The same question about the volume the module itself lives on, which is
    the one `host_folds_case` answers."""
    import os

    module = Path(exec_names.__file__).resolve()
    swapped = module.with_name(module.name.swapcase())
    really_folds = swapped.exists() and os.path.samefile(str(swapped), str(module))

    assert exec_names.host_folds_case() is really_folds
    assert bool(exec_names.case_flags()) is really_folds


def test_an_unmeasurable_path_falls_back_to_the_host_class():
    """A path that cannot be stat'ed proves nothing about the volume, so the
    probe returns the `os.name` answer rather than guessing the permissive one.
    """
    import os

    missing = "/no_human-nonexistent-probe-dir/AbC"

    assert exec_names._folds_case(missing) is (os.name == "nt")


#: The four rows #328 measured as open on main, plus the flag spelling of the
#: first one. On a case-insensitive volume every one of them really invokes the
#: lowercase program the gates were written for.
_FOLD_SENSITIVE_ROWS = (
    "GH pr merge 7",
    "RM -rf /",
    "rm -RF /",
    "FIND / -delete",
    'sh -c "GIT push origin main"',
    'sh -c "GIT.CMD push origin main"',
)


def _verdicts_with_fold(fold: bool, rows=_FOLD_SENSITIVE_ROWS, readonly=False) -> dict:
    """Guard verdicts from a fresh interpreter with the probe pinned to `fold`.

    A subprocess, not a monkeypatch: `_RM_RF`, `_GIT_DESTRUCTIVE` and the
    `_looks_like_git_push` recursion gate bake `case_flags()` into compiled
    patterns at import time, so patching after the fact reaches the name path
    only -- which is how a fix for the name half alone can look complete while
    three text gates stay open. `rows` defaults to `_FOLD_SENSITIVE_ROWS` so
    the two callers below (#305/#320's original regression) are unchanged;
    the #328 runner-recursion matrix below passes its own. `readonly` defaults
    to `False`, matching every existing caller; the git-recursion test below
    passes `True` because `_git_invocations` (the function #328's runner-
    recursion fix touches) is consulted ONLY by the read-only write-block.
    The default-mode (`readonly=False`) protected-branch check goes through
    the separate `_git_push_invocations`/`_push_targets_protected` pair,
    which still misses a trailing-argv runner (`timeout`/`xargs` without
    `-c`) structurally -- that gap is closed instead by the whole-string
    lexical fallback at `evaluate`'s `git ... push` check, which now also
    carries `case_flags()` (see
    `test_a_capitalised_git_push_is_denied_in_the_default_session_too`).
    """
    code = dedent(f"""
        import json
        from no_human.agent import exec_names
        exec_names.host_folds_case = lambda: {fold!r}
        from no_human.agent.guard import evaluate
        out = {{}}
        for cmd in {list(rows)!r}:
            decision = evaluate(
                "Bash", {{"command": cmd}}, forbidden_paths=[],
                never_push_to=["main"], readonly={readonly!r}, cwd=".",
                env={{"PATH": ""}})
            out[cmd] = not decision.allow
        print(json.dumps(out))
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_folding_host_denies_every_capitalised_spelling():
    """Two of these walk past the NAME resolution and two past the TEXT gates,
    so a fix that closes either half alone fails this."""
    denied = _verdicts_with_fold(True)

    assert denied == {cmd: True for cmd in _FOLD_SENSITIVE_ROWS}, denied


def test_a_case_sensitive_host_is_not_punished():
    """The #320 position, kept intact and made observable: where `GIT` and
    `git` are two files, denying the capitalised spelling would refuse a
    command the user is entitled to run. This is also the control that shows
    the fold is what does the work above, not some unrelated tightening."""
    denied = _verdicts_with_fold(False)

    assert denied == {cmd: False for cmd in _FOLD_SENSITIVE_ROWS}, denied


# ------------------------------------------------ #328's runner-recursion half
#
# Capitalising one letter of the BINARY (`gh`/`glab`), the NOUN (`pr`/`mr`) or
# the VERB (`merge`/`accept`) of a forge merge command, wrapped in a shell
# runner (`sh -c`, `bash -c`, `timeout`, `xargs`), let a merge through that
# the bare/lowercase spelling correctly denies. Two independent defects, both
# required to close it: `_forge_invocations`/`_git_invocations` resolved a
# wrapped runner's nested command name case-blind instead of through
# `exec_names.command_name` (the same resolver the top-level `argv[0]` path
# already used), and `_forge_subcommand` compared/returned raw-case tokens so
# a capitalised noun/verb never matched `_FORGE_MERGE_PAIRS` even once found.

#: `(noun, verb)` for the two forges' merge subcommand, per `_FORGE_MERGE_PAIRS`.
_CASE_FORGE_SUBCOMMANDS = {"gh": ("pr", "merge"), "glab": ("mr", "merge")}

#: Every runner the guard recurses into for a forge command, `{cmd}` standing
#: in for the (possibly capitalised, possibly `-R`-flagged) invocation.
_CASE_MATRIX_RUNNERS = (
    "{cmd}",
    'sh -c "{cmd}"',
    'bash -c "{cmd}"',
    "timeout 30 {cmd}",
    "xargs {cmd}",
)


def _case_matrix_cell(binary: str, noun: str, verb: str, position: str,
                       flagged: bool, runner_tmpl: str) -> str:
    """One matrix cell: `binary`/`noun`/`verb` with the token named by
    `position` capitalised, optionally carrying the `-R o/r` global option
    that defeats `_FORGE_MERGE`'s contiguous lexical anchor (so only the
    structural pair-fold can reach it), wrapped in `runner_tmpl`."""
    parts = {"binary": binary, "noun": noun, "verb": verb}
    parts[position] = parts[position].upper()
    flag = "-R o/r " if flagged else ""
    cmd = f"{parts['binary']} {flag}{parts['noun']} {parts['verb']} 7"
    return runner_tmpl.format(cmd=cmd)


#: `(binary, position, flagged, runner, cell)` for every cell, kept alongside
#: the generated command so the expected-value table below can be built from
#: the SAME metadata rather than from running the guard.
_CASE_MATRIX_CELLS = [
    (binary, position, flagged, runner_tmpl,
     _case_matrix_cell(binary, noun, verb, position, flagged, runner_tmpl))
    for binary, (noun, verb) in _CASE_FORGE_SUBCOMMANDS.items()
    for position in ("binary", "noun", "verb")
    for flagged in (False, True)
    for runner_tmpl in _CASE_MATRIX_RUNNERS
]  # 2 forges x 3 positions x 2 flag flavours x 5 runners = 60 cells.

_CASE_MATRIX_EXTRA_ROWS = (
    # `glab mr accept 12` is `_FORGE_MERGE_PAIRS`'s third pair, `accept` an
    # alias of `merge` -- capitalise each of its three tokens in turn too.
    "glab mr accept 12",
    "GLAB mr accept 12",
    "glab MR accept 12",
    "glab mr ACCEPT 12",
    # Unbalanced quote: `shlex.split` raises inside `_forge_invocations` and
    # the function falls back to `.split()` -- one row exercising that
    # fallback under a capitalised binary.
    'sh -c "GH pr merge 7',
    # Lexical-only: no `_forge_subcommand` pair reaches an `api` REST call or
    # a GraphQL mutation string, so these pin `_FORGE_MERGE`'s own
    # `case_flags()` in isolation from the structural fix.
    "GH api /repos/o/r/pulls/7/merge --method PUT",
    'gh API graphql -f query="mutation{mergePullRequest(input:{})}"',
)

_CASE_MATRIX_ROWS = tuple(cell for *_meta, cell in _CASE_MATRIX_CELLS) + _CASE_MATRIX_EXTRA_ROWS


def test_every_capitalised_merge_spelling_is_denied_on_a_folding_host():
    """Asserted against the WHOLE dict, not `all(...)`, so a failure prints
    every still-open cell rather than just the first."""
    denied = _verdicts_with_fold(True, _CASE_MATRIX_ROWS)

    assert denied == {cmd: True for cmd in _CASE_MATRIX_ROWS}, denied


def test_a_case_sensitive_host_keeps_the_documented_table():
    """Answers the plan's open intake question by measurement: the fix must
    hold under BOTH `host_folds_case()` answers, because the binary-name half
    is deliberately host-gated (`GH` really is a different file on a
    case-sensitive host, the #320 position) while the noun/verb half
    deliberately is not (`gh pr MERGE` is not a runnable subcommand on any
    host, so denying it refuses nothing anyone is entitled to run).

    The expected table is built from the generating metadata (`position`),
    not from a second run of the guard -- a regression in either half of the
    fix shows up as a documented-table mismatch, not a tautology."""
    expected = {
        cell: position != "binary"
        for _binary, position, _flagged, _runner, cell in _CASE_MATRIX_CELLS
    }
    expected.update({
        "glab mr accept 12": True,
        "GLAB mr accept 12": False,
        "glab MR accept 12": True,
        "glab mr ACCEPT 12": True,
        'sh -c "GH pr merge 7': False,
        "GH api /repos/o/r/pulls/7/merge --method PUT": False,
        'gh API graphql -f query="mutation{mergePullRequest(input:{})}"': True,
    })

    denied = _verdicts_with_fold(False, _CASE_MATRIX_ROWS)

    assert denied == expected, denied


def test_the_runner_recursion_folds_a_wrapped_name_for_git_too():
    """#328's other extractor: `_git_invocations` had the identical defect,
    fixed identically (`exec_names.command_name`, not `PurePosixPath(tok).name`,
    on the trailing-argv branch; a precompiled `_GIT_MENTION` gated by
    `case_flags()` on the quoted-mention branch). The `-C .` row is the one no
    lexical git pattern reaches at all, so it pins the git half specifically
    rather than riding on some other gate. Asserted through `evaluate`, never
    `_git_invocations` directly.

    `readonly=True`: `_git_invocations` (the function this test's fix touches)
    is consulted only by the read-only session's write-block. At the default
    `readonly=False` the protected-branch check goes through the separate
    `_git_push_invocations`/`_push_targets_protected` pair, which still misses
    a trailing-argv runner (`timeout`/`xargs` without `-c`) structurally --
    see `test_a_capitalised_git_push_is_denied_in_the_default_session_too`,
    which pins that the whole-string lexical fallback in `evaluate` closes it
    instead, so asserting THIS structural row at `readonly=False` would
    either mask that gap or misattribute which gate closed it."""
    rows = (
        'sh -c "GIT push origin main"',
        'bash -c "GIT -C . push origin main"',
        "timeout 30 GIT push origin main",
        "xargs GIT push origin main",
    )

    denied_folding = _verdicts_with_fold(True, rows, readonly=True)
    assert denied_folding == {cmd: True for cmd in rows}, denied_folding

    denied_sensitive = _verdicts_with_fold(False, rows, readonly=True)
    assert denied_sensitive == {cmd: False for cmd in rows}, denied_sensitive


def test_a_capitalised_git_push_is_denied_in_the_default_session_too():
    """The default (`readonly=False`) session's protected-branch check is
    `_git_push_invocations`/`_push_targets_protected`, a wholly separate
    extractor from `_git_invocations` above. It has its own structural gap
    for a trailing-argv runner: `timeout 30 GIT push origin main` splits into
    separate tokens `('30', 'GIT', 'push', 'origin', 'main')`, and no single
    token contains both `git` and `push` for `_looks_like_git_push` to match,
    so the recursion never fires -- unlike a quoted `sh -c "GIT push ..."`,
    where the whole quoted script is one token. That structural gap is left
    untouched (out of scope, `_git_push_invocations` is not edited by this
    change); what closes it is `evaluate`'s pre-existing whole-string lexical
    fallback (`\\bgit\\s+push\\b` + `_push_targets_protected`), which this
    change gates with `exec_names.case_flags()` the same way `_FORGE_MERGE`
    and `_GIT_MENTION` already are. `_push_targets_protected` itself matches
    `push`/branch tokens verbatim (lowercase), which is host-independent, so
    only the binary (`GIT`) is capitalised here -- verb/noun capitalisation
    of `push`/`main` is a separate, undisclosed gap this row does not claim
    to close.

    NOT closed by this fix, and not claimed to be: a runner-recursion form
    that interposes a flag between the capitalised binary and `push` (`timeout
    30 GIT -C . push origin main`) defeats this contiguous lexical pattern
    too. `test_the_runner_recursion_folds_a_wrapped_name_for_git_too` above
    already covers that shape, but only in the `readonly=True` (write-block)
    path -- see its docstring."""
    rows = (
        'sh -c "GIT push origin main"',
        'bash -c "GIT push origin main"',
        "timeout 30 GIT push origin main",
        "xargs GIT push origin main",
    )

    denied_folding = _verdicts_with_fold(True, rows, readonly=False)
    assert denied_folding == {cmd: True for cmd in rows}, denied_folding

    denied_sensitive = _verdicts_with_fold(False, rows, readonly=False)
    assert denied_sensitive == {cmd: False for cmd in rows}, denied_sensitive


def test_the_widened_mention_gate_stays_linear():
    """`_FORGE_MENTION`/`_GIT_MENTION` must stay precompiled -- a per-call
    `re.compile` here would turn the documented linear recursion bound into
    quadratic-or-worse well before 1000 wrappers. Wall-clock ceiling is
    generous on purpose; this is a linearity smoke test, not a benchmark."""
    nested = "GIT push origin main"
    for _ in range(1000):
        nested = f'sh -c "{nested}"'

    started = time.monotonic()
    denied = _verdicts_with_fold(True, (nested,))
    elapsed = time.monotonic() - started

    assert isinstance(denied[nested], bool)  # completed at all, didn't hang
    assert elapsed < 30, f"linearity bound appears lost: {elapsed}s"


# ------------------------------------------------------------- the real gates

@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git.exe push origin main",          # suffix strip is ungated (#107)
        "C:/tools/git.exe push origin main",  # PurePosixPath already splits `/`
    ],
)
def test_never_push_to_main_denies_on_every_host(command):
    """Rows that need no platform gating, so they must hold on both."""
    assert _decide(command), "push to a protected branch was allowed"


@pytest.mark.parametrize(
    "command",
    [
        "GIT.EXE push origin main",
        '"C:\\tools\\git.exe" push origin main',
        '"C:\\Program Files\\Git\\cmd\\git.exe" push origin main',
    ],
)
def test_never_push_to_main_denies_the_windows_spellings(command, on_windows):
    """Rows that depend on the gated half: case folding and backslash
    splitting. Asserted with the constant flipped, so a POSIX runner proves the
    Windows behaviour rather than skipping it."""
    assert _decide(command), "push to a protected branch was allowed"


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 7",
        "gh.exe pr merge 7",
        "C:/tools/gh.exe pr merge 7",
        "nh approve 7",
        "nh.exe approve 7",
    ],
)
def test_the_agent_still_never_merges_on_every_host(command):
    """Constraint #2. `gh pr merge` and `nh approve` are the two commands that
    land work, and a trailing `.exe` walked past both."""
    assert _decide(command), "a merge/approve command was allowed"


@pytest.mark.parametrize(
    "command",
    [
        # The spellings a Windows user actually has. `gh` from scoop or npm is
        # `gh.cmd`, so `.exe` alone would close the reviewer's spelling and
        # leave the user's.
        "gh.cmd pr merge 7",
        "gh.ps1 pr merge 7",
        "nh.bat approve 7",
        "git.com push origin main",
        # Win32 resolution quirks that reach the same binary.
        "gh. pr merge 7",
        "nh.exe. approve 7",
        "gh.exe::$DATA pr merge 7",
    ],
)
def test_the_other_windows_executable_spellings_deny_too(command, on_windows):
    assert _decide(command), "a guarded binary was reachable under this spelling"


@pytest.mark.parametrize(
    "command",
    [
        "rm.exe -rf /",
        'echo "gh.exe pr merge 7" | sh',
        "timeout 30 git.exe push origin main",
    ],
)
def test_the_raw_text_matchers_are_still_open_and_that_is_recorded(command, on_windows):
    """Not a fix, a boundary, and the reason this PR says Refs and not Closes.

    These three decide through RAW-TEXT matchers (`_RM_RF`, the lexical
    merge-stack matcher, and the `timeout` wrapper which is not in `_WRAPPERS`)
    rather than through argv[0], so no name resolver can reach them. Closing
    them means widening those patterns, which is a separate change against the
    same issue.

    Asserted so that whoever widens them sees this go red and removes it
    deliberately, instead of the residual being rediscovered from scratch.
    """
    assert not _decide(command), (
        "this now denies, so the raw-text matchers have been widened: delete "
        "this test and move the row into the parametrised cases above"
    )


@pytest.mark.parametrize("command", ["GH.EXE pr merge 7", "NH.exe approve 7"])
def test_the_agent_still_never_merges_in_windows_case(command, on_windows):
    assert _decide(command), "a merge/approve command was allowed"


@pytest.mark.parametrize(
    "command",
    [
        "grep.exe -r secret /",
        "rg.exe secret /",
        "find.exe / -name x",
    ],
)
def test_whole_volume_scans_deny_under_the_exe_spelling(command, on_windows):
    """Found by measuring, not by reading.

    Review suggested reverting each of the ten sites on its own and diffing a
    corpus rather than assuming any were redundant. Three sites
    (`root_scan_denial`, `_segment_scans_and_mutates`,
    `_scan_for_install_denial`) turned out to be individually reachable, and
    the verdict that moved was this one: reverting any of them takes
    `grep.exe -r secret /` from DENY to ALLOW, a whole-volume read that the
    bare spelling refuses. Nothing in the first version of this file covered
    it.
    """
    assert _decide(command), "a whole-volume scan was allowed under .exe"


def test_the_windows_spellings_are_left_alone_on_posix(on_posix, on_case_sensitive_host):
    """The other half of the gate, and the reason it IS a gate.

    On a case-SENSITIVE POSIX host `GIT` is a genuinely different file from
    `git`, and a backslash is a legal character in a filename. Denying these
    there would be a text match masquerading as a structural one, and could
    refuse a command the user is entitled to run. The fold is pinned off so
    this stays the case-sensitive host's contract even when the developer's own
    filesystem folds (#328).
    """
    assert not _decide("GIT.EXE push origin main")
    assert not _decide('"C:\\tools\\git.exe" push origin main')
    # ...while the ungated rows still deny on POSIX.
    assert _decide("git.exe push origin main")
    assert _decide("git push origin main")


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
