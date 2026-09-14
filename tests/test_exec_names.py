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
import os
import subprocess
import sys
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
    # `*a, **k`, not a no-arg lambda: `host_folds_case` now takes `(cwd,
    # path_env)`, and a bare `lambda: False` raises `TypeError` the moment any
    # call site passes either.
    monkeypatch.setattr(exec_names, "host_folds_case", lambda *a, **k: False)


def _clear_cache_if_present():
    # Some tests/fixtures in this file temporarily replace `host_folds_case`
    # with a plain lambda (a monkeypatch, or the subprocess harness's own
    # reassignment); by the time this runs that replacement may or may not
    # have been undone yet, so `cache_clear` is optional, not assumed.
    clear = getattr(exec_names.host_folds_case, "cache_clear", None)
    if clear is not None:
        clear()


@pytest.fixture(autouse=True)
def _clear_fold_cache():
    """`host_folds_case` is `lru_cache`d per-process; without this a test that
    measures a real `tmp_path` volume, or pins the probe, can read another
    test's cached answer instead of its own.
    """
    _clear_cache_if_present()
    yield
    _clear_cache_if_present()


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

    assert exec_names._swap_probe(str(written)) is folds_here


def test_the_host_probe_is_measured_once_not_once_per_token(monkeypatch):
    """`host_folds_case` must do its filesystem work ONCE per process, per
    argument set.

    It is read on every guarded command, and `guard._looks_like_git_push`
    reads it per TOKEN -- so a command of 4000 quoted arguments could read it
    4000 times without a cache in front of the real probing work
    (`_folds_case_at`, which itself may `scandir`).

    This counts probe CALLS rather than timing them, and asserts the PROPERTY
    ("probed once per distinct argument tuple"), not the mechanism: an
    implementation that memoizes some other way than `lru_cache` still passes,
    as long as repeated calls with the SAME arguments do not re-touch the
    filesystem.
    """
    calls = []
    real_folds_case_at = exec_names._folds_case_at
    monkeypatch.setattr(
        exec_names, "_folds_case_at",
        lambda d: (calls.append(d), real_folds_case_at(d))[1])

    exec_names.host_folds_case.cache_clear()
    exec_names.host_folds_case()
    # The first call may legitimately probe several anchors (cwd, then PATH
    # entries, in union order) before it can answer -- that is the union
    # algorithm doing its job, not a caching failure. What must NOT happen is
    # that count growing on repeat calls with the SAME arguments.
    after_first = len(calls)
    for _ in range(49):
        exec_names.host_folds_case()

    assert len(calls) == after_first, (
        "host_folds_case re-probed the filesystem on a repeat call instead "
        f"of answering from its cache: {after_first} probe calls after the "
        f"first invocation, {len(calls)} after 50")


def test_the_probe_matches_this_host():
    """The question `host_folds_case()` actually answers: does the process
    cwd, or anything on `PATH`, live on a case-folding volume? Not: does the
    module's OWN source file.

    This pins the UNION logic (probe cwd, then PATH, first `True` wins, else
    fold only if nothing was determinate) by reimplementing that orchestration
    here and comparing against the real entry point -- it does NOT re-derive
    the low-level per-path measurement independently, since both this test
    and `host_folds_case` route through the same `_folds_case_at`/
    `_swap_probe` helpers. A bug inside `_swap_probe` itself (e.g. the
    `samefile` check) would not be caught by this test; that class of bug is
    covered separately by `test_a_directory_holding_both_spellings_is_not_mistaken_for_a_fold`
    and the other direct `_swap_probe`/`_folds_case_at` tests below.
    """
    import os

    candidates = [os.getcwd()]
    candidates.extend(
        p for p in os.environ.get("PATH", "").split(os.pathsep) if p)

    really_folds = None
    for candidate in candidates:
        verdict = exec_names._folds_case_at(candidate)
        if verdict is True:
            really_folds = True
            break
        if verdict is False:
            really_folds = False
    if really_folds is None:
        really_folds = True  # unmeasurable anywhere on the union: folds

    assert exec_names.host_folds_case() is really_folds
    assert bool(exec_names.case_flags()) is really_folds


def test_the_probe_never_reads_its_own_source_path(monkeypatch):
    """The shipped defect in one assertion: in a PyInstaller onedir bundle no
    `.py` files ship, so `__file__` names a path that does not exist at
    runtime. The fix must not depend on it at all, so clobbering it to a dead
    path and marking the process 'frozen' must not change the answer.
    """
    import sys as _sys

    exec_names.host_folds_case.cache_clear()
    before = exec_names.host_folds_case()

    monkeypatch.setattr(
        exec_names, "__file__",
        "/nonexistent/_internal/no_human/agent/exec_names.py")
    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    exec_names.host_folds_case.cache_clear()
    after = exec_names.host_folds_case()

    assert after is before


def test_the_probe_measures_the_cwd_volume(tmp_path):
    """`cwd` is not decoration -- it is an anchor the union actually probes."""
    import os

    expected = exec_names._folds_case_at(str(tmp_path))
    if expected is None:
        expected = True  # unmeasurable at this anchor alone folds too

    exec_names.host_folds_case.cache_clear()
    assert exec_names.host_folds_case(cwd=str(tmp_path), path_env="") is expected


def test_the_union_folds_if_any_anchor_folds(monkeypatch, tmp_path):
    """One folding anchor on the union is enough: a capitalised spelling only
    needs to resolve on SOME `PATH` entry to reach the guarded program."""
    import os

    other = tmp_path / "other"
    other.mkdir()
    answers = {str(tmp_path): False, str(other): True}
    monkeypatch.setattr(
        exec_names, "_folds_case_at", lambda d: answers.get(d, None))

    exec_names.host_folds_case.cache_clear()
    assert exec_names.host_folds_case(
        cwd=str(tmp_path), path_env=str(other)) is True


def test_an_unmeasurable_probe_folds():
    """The bug this whole change closes: an unmeasurable path/volume must
    fold (the DENY-more direction), never answer the permissive class-based
    guess this test replaces (the host's OS family name, which reads
    permissive/`False` on every POSIX host, i.e. the shipped bug).
    """
    assert exec_names._swap_probe("/no_human-nonexistent/AbC") is None
    # No case-swappable character in the basename: this is `None` (nothing
    # measured), not `False` (measured and does not fold).
    assert exec_names._swap_probe("/123") is None

    exec_names.host_folds_case.cache_clear()
    assert exec_names.host_folds_case(
        cwd="/no_human-nonexistent-probe-dir", path_env="") is True
    exec_names.host_folds_case.cache_clear()
    assert bool(exec_names.case_flags(
        cwd="/no_human-nonexistent-probe-dir", path_env="")) is True


def test_a_totally_unmeasurable_probe_still_reaches_the_final_fold(monkeypatch):
    """Coverage gap (case-fold review, BLOCKER 3): the fail-closed default
    `return True` at the very end of `host_folds_case` -- the line reached
    only when EVERY anchor, including the tier-2 last-resort candidates
    (`dirname(sys.executable)`, `tempfile.gettempdir()`), answers `None` --
    had zero test coverage. `test_an_unmeasurable_probe_folds` above looks
    like it exercises this, but its nonexistent `cwd` and empty `PATH` just
    mean the union loop's own candidates answer `None`; the tier-2 fallback
    then measures the REAL `sys.executable`/tempdir, which exist and answer
    determinately on this host, and coincidentally matches the expected
    `True` -- mutating this final line's `True` to `False` (the shipped
    bug's own polarity) left the full suite green.

    Forces true unmeasurability by making `_folds_case_at` answer `None` for
    every candidate, tier-2 included, so only the final `return True` can
    produce the result.
    """
    monkeypatch.setattr(exec_names, "_folds_case_at", lambda directory: None)
    exec_names.host_folds_case.cache_clear()
    assert exec_names.host_folds_case(cwd="/no_human-nonexistent-probe-dir", path_env="") is True


def test_the_probe_survives_a_removed_process_cwd(tmp_path, monkeypatch):
    """Crash repro (case-fold review, BLOCKER 1): `_candidate_anchors` fell
    back to a bare `os.getcwd()` whenever its own `cwd` argument was falsy --
    and `os.getcwd()` itself raises `FileNotFoundError` (a subclass of
    `OSError`) when the orchestrator's own working directory has been
    removed out from under it (a real, observed shape: a worktree cleaned up
    mid-session while the agent process is still chdir'd into it). That
    exception was unguarded, so EVERY `Bash` command evaluated with no
    explicit `cwd` crashed `guard.evaluate` outright -- denial-of-availability
    for the whole guard, not a wrong verdict.

    Reproduces the precondition directly: chdir into a scratch directory,
    delete it while it is still the process cwd, then confirm both the probe
    and the full `guard.evaluate` entry point still return an answer instead
    of raising.
    """
    doomed = tmp_path / "doomed"
    doomed.mkdir()
    original_cwd = os.getcwd()
    os.chdir(doomed)
    try:
        os.rmdir(doomed)
        assert not os.path.exists(doomed)

        exec_names.host_folds_case.cache_clear()
        # Must not raise (FileNotFoundError/OSError) -- must return a verdict.
        result = exec_names.host_folds_case(cwd=None, path_env="")
        assert result in (True, False)

        decision = guard.evaluate(
            "Bash", {"command": "pip install evilpkg"}, forbidden_paths=[],
            never_push_to=["main"], cwd=None, env={"PATH": ""})
        assert decision is not None
    finally:
        os.chdir(original_cwd)


def test_a_directory_holding_both_spellings_is_not_mistaken_for_a_fold(tmp_path):
    """`exists`-only would call this a fold; it is two distinct files on a
    case-SENSITIVE volume, and denying accordingly would be wrong."""
    import os

    (tmp_path / "Foo").write_text("upper")
    (tmp_path / "foo").write_text("lower")
    if os.path.samefile(tmp_path / "Foo", tmp_path / "foo"):
        pytest.skip("this volume folds case; both names collide onto one file")

    assert exec_names._swap_probe(str(tmp_path / "foo")) is False


def test_an_unreadable_candidate_does_not_answer_false(tmp_path):
    """A candidate this process cannot even list must not manufacture a
    `False` (case-sensitive) verdict -- that would be as wrong a guess as the
    permissive fallback this change removes, just from a different input.

    The directory is named without a case-swappable character (`123`, not
    `blocked`/`BLOCKED`) so `_swap_probe` on its own name is `None` and
    `_folds_case_at` is forced into the `scandir` fallback this test means to
    exercise, rather than answering from the directory's own swapped name --
    which, on a folding host, would resolve before ever touching permissions.
    """
    import os

    blocked = tmp_path / "123"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        if os.access(blocked, os.R_OK):
            pytest.skip("this process can read 0o000 directories (e.g. root)")
        assert exec_names._folds_case_at(str(blocked)) is None
    finally:
        blocked.chmod(0o755)


def test_no_test_asserts_the_permissive_fallback():
    """Self-guard: the permissive `os.name`-vs-`"nt"` fallback this change
    deletes must never again be asserted as the correct answer anywhere in
    this file -- that assertion IS the bug (issue title: 'the case-fold
    probe answers from a path that exists at runtime').

    The needle is assembled from parts rather than written as one literal, so
    this test's own source does not trip its own check.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    quote = chr(34)
    apostrophe = chr(39)
    needle_double = "os" + ".name" + " == " + quote + "nt" + quote
    needle_single = "os" + ".name" + " == " + apostrophe + "nt" + apostrophe
    assert needle_double not in source
    assert needle_single not in source


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


def _verdicts_with_fold(fold: bool) -> dict:
    """Guard verdicts from a fresh interpreter with the probe pinned to `fold`.

    A subprocess, not a monkeypatch: `_RM_RF`, `_GIT_DESTRUCTIVE` and the
    `_looks_like_git_push` recursion gate bake `case_flags()` into compiled
    patterns at import time, so patching after the fact reaches the name path
    only -- which is how a fix for the name half alone can look complete while
    three text gates stay open.
    """
    code = dedent(f"""
        import json
        from no_human.agent import exec_names
        exec_names.host_folds_case = lambda *a, **k: {fold!r}
        from no_human.agent.guard import evaluate
        out = {{}}
        for cmd in {list(_FOLD_SENSITIVE_ROWS)!r}:
            decision = evaluate(
                "Bash", {{"command": cmd}}, forbidden_paths=[],
                never_push_to=["main"], cwd=".", env={{"PATH": ""}})
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


def test_a_frozen_layout_still_denies_the_forge_rows():
    """AC5: the shipped PyInstaller onedir bundle ships no `.py` files, so
    `exec_names.__file__` names a path absent at runtime. This reuses the
    `_verdicts_with_fold` subprocess shape but leaves the probe REAL --
    unlike the tests above, nothing here is pinned -- and clobbers `__file__`
    plus `sys.frozen` before `guard` (and therefore its import-time compiled
    patterns) is even imported. If the fix still secretly depended on
    `__file__`, this dead path would make it answer differently from the
    unfrozen case measured by `_decide` below; the two must agree.
    """
    code = dedent(f"""
        import json, sys
        from no_human.agent import exec_names
        exec_names.__file__ = "/nonexistent/_internal/no_human/agent/exec_names.py"
        sys.frozen = True
        from no_human.agent.guard import evaluate
        out = {{}}
        for cmd in {list(_FOLD_SENSITIVE_ROWS)!r}:
            decision = evaluate(
                "Bash", {{"command": cmd}}, forbidden_paths=[],
                never_push_to=["main"], cwd=".", env={{"PATH": ""}})
            out[cmd] = not decision.allow
        print(json.dumps(out))
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    frozen_denied = json.loads(result.stdout)

    unfrozen_denied = {cmd: _decide(cmd) for cmd in _FOLD_SENSITIVE_ROWS}

    assert frozen_denied == unfrozen_denied, (
        "a dead __file__ under a simulated frozen layout changed the "
        f"verdict: frozen={frozen_denied} unfrozen={unfrozen_denied}")


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
