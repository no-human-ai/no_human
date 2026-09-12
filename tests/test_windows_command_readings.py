"""A Windows-spelled command must reach the same verdict as its POSIX twin.

Issue #105. Both command guards tokenise with POSIX `shlex`, where `\\` is an
escape character, so every separator in `C:\\Users\\me\\.venv\\Scripts\\pip.exe`
is deleted before resolution is attempted. The venv guard's one allow-and-log
fallback then stops being an edge case and becomes the default, and the silent
form logs nothing at all -- a guard that cannot fire looks exactly like a guard
that passed.

WHAT THESE TESTS SIMULATE, AND WHAT THEY DO NOT. They flip the `_IS_WINDOWS`
module constants (the seam `fs_roots.is_windows_filesystem_root` already uses)
and hand the guards backslash-spelled paths, on a POSIX filesystem. That
covers the lexing and the resolution logic, which is where the defect lives.
It does NOT cover Windows filesystem semantics -- drive letters, case folding,
8.3 names -- and a green run here is not a substitute for running the suite on
a Windows host.

The design under test is deliberately shell-agnostic. Whether the coder's
Bash tool runs Git Bash (where `\\` really is an escape and POSIX lexing is
correct) or cmd/PowerShell (where it is not) is not settled, so the guards
check BOTH readings and deny if either denies. Under Git Bash the extra
reading only adds denials for strings Git Bash could not have run; under
cmd/PowerShell it closes the fail-open.
"""

from __future__ import annotations

import importlib
import os
import stat

import pytest

from no_human.agent import guard, venv_install_guard, win_readings


def _make_venv_bin(venv_dir):
    bindir = venv_dir / "bin"
    bindir.mkdir(parents=True)
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin\n")
    for name in ("python", "python3", "pip", "pip3", "uv"):
        path = bindir / name
        path.write_text("#!/bin/sh\nexit 0\n")
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def primary(tmp_path, monkeypatch):
    """The shared checkout whose venv every session must leave alone."""
    root = tmp_path / "primary"
    (root / "src" / "no_human").mkdir(parents=True)
    (root / "src" / "no_human" / "__init__.py").write_text("")
    _make_venv_bin(root / ".venv")
    monkeypatch.setattr(guard, "_primary_checkout", lambda: root)
    return root


@pytest.fixture
def worktree(tmp_path):
    """Where the session actually runs. Installing into ITS venv is fine;
    reaching back into the primary's is what these cases are about."""
    root = tmp_path / "worktree"
    _make_venv_bin(root / ".venv")
    return root


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(guard, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", True, raising=False)


@pytest.fixture
def on_posix(monkeypatch):
    monkeypatch.setattr(guard, "_IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", False, raising=False)


def _backslashed(text: str) -> str:
    return text.replace("/", "\\")


def _env_pointing_at(venv_dir) -> dict[str, str]:
    """A PATH constructed, never inherited.

    `tests/test_guard.py::test_installs_into_the_worktree_own_venv_are_allowed`
    records why: passing no `env` lets `os.environ` decide, and the verdict
    then depends on the PATH of whatever process happens to run the suite.
    """
    return {"PATH": str(venv_dir / "bin")}


# The POSIX spellings below are lifted from the cases
# `tests/test_guard.py::test_installing_into_the_primary_venv_is_refused`
# already refuses, so the comparison is against a verdict this repo has
# already committed to -- not against one invented here.
BYPASS_TEMPLATES = [
    "{p}/.venv/bin/pip install foo",
    "VIRTUAL_ENV={p}/.venv pip install -e .",
    "source {p}/.venv/bin/activate && uv pip install -e .",
    "uv pip install --python {p}/.venv/bin/python -e .",
    "cd {p} && uv sync",
    'sh -c "{p}/.venv/bin/pip install foo && echo ok"',
    "env -i {p}/.venv/bin/pip install foo",
    "timeout 300 {p}/.venv/bin/pip install foo",
]


@pytest.mark.parametrize("template", BYPASS_TEMPLATES)
def test_the_posix_spelling_is_refused(primary, worktree, template, on_posix):
    """The control. Without it, a guard that denied everything would pass."""
    cmd = template.format(p=primary)
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(primary / ".venv"),
    ) is not None, cmd


@pytest.mark.parametrize("template", BYPASS_TEMPLATES)
def test_the_windows_spelling_is_refused_too(primary, worktree, template, on_windows):
    cmd = _backslashed(template.format(p=primary))
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(primary / ".venv"),
    ) is not None, cmd


@pytest.mark.parametrize("template", BYPASS_TEMPLATES)
def test_the_whole_guard_refuses_the_windows_spelling(primary, worktree, template, on_windows):
    """Same cases through `guard.evaluate`, the PreToolUse entry point."""
    cmd = _backslashed(template.format(p=primary))
    decision = guard.evaluate(
        "Bash", {"command": cmd},
        forbidden_paths=[], never_push_to=[], cwd=str(worktree),
        env=_env_pointing_at(primary / ".venv"),
    )
    assert not decision.allow, cmd


def test_a_windows_spelling_is_still_allowed_when_the_posix_twin_is(worktree, on_windows):
    """The negative control for the whole design.

    Denying on the alternate reading must not turn the guard into one that
    denies every backslash. An install into the session's OWN worktree venv
    is the ordinary case and stays allowed in both spellings.
    """
    env = _env_pointing_at(worktree / ".venv")
    allowed = f"{worktree}/.venv/bin/pip install foo"
    assert venv_install_guard.denial_reason(allowed, cwd=str(worktree), env=env) is None
    assert venv_install_guard.denial_reason(
        _backslashed(allowed), cwd=str(worktree), env=env
    ) is None


def test_posix_hosts_keep_posix_escape_semantics(on_posix):
    """`\\ ` is a real escape on POSIX and must stay one.

    This is what stops the fix from being "treat backslash as literal
    everywhere", which would change how every POSIX command is tokenised.
    """
    assert venv_install_guard._lex(r"pip\ install foo") == ["pip install", "foo"]


def test_the_original_reading_is_still_checked_on_windows(tmp_path, monkeypatch, on_windows):
    """Both readings, not just the normalised one.

    `C:\\Program Files\\...` is the everyday Windows path with a space in it.
    Escaped POSIX-style, `/tmp/my\\ proj/.venv/bin/pip` is ONE token and
    resolves; normalising the backslash to `/` splits it into two and it
    resolves to nothing. So a fix that only read the normalised spelling
    would allow exactly the install this guard exists to refuse -- and would
    do it on the path shape Windows users have.
    """
    spaced = tmp_path / "my proj"
    (spaced / "src" / "no_human").mkdir(parents=True)
    (spaced / "src" / "no_human" / "__init__.py").write_text("")
    _make_venv_bin(spaced / ".venv")
    monkeypatch.setattr(guard, "_primary_checkout", lambda: spaced)

    session = tmp_path / "worktree"
    _make_venv_bin(session / ".venv")

    escaped = str(spaced).replace(" ", r"\ ")
    cmd = f"{escaped}/.venv/bin/pip install foo"
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(session), env=_env_pointing_at(spaced / ".venv"),
    ) is not None, cmd


def test_a_posix_host_does_not_gain_the_windows_reading(primary, worktree, on_posix):
    """The platform gate has to be load-bearing, not decoration.

    On a POSIX host `C:\\Users\\...\\pip.exe install foo` is not an install
    command -- `\\` is an escape, the token is nonsense, and nothing runs. The
    guard must keep allowing it. Without this test the whole `is_windows`
    parameter could be deleted and the suite would stay green, which is
    exactly what a mutation run showed before it was added: 325 tests passed
    with the alternate reading applied unconditionally.
    """
    windows_spelled = _backslashed(f"{primary}/.venv/bin/pip install foo")
    assert venv_install_guard.denial_reason(
        windows_spelled, cwd=str(worktree), env=_env_pointing_at(primary / ".venv"),
    ) is None


# Round 2 of issue #105, found by an adversarial review of the first fix.
# `_flatten` decided "is this a shell runner whose payload I must recurse
# into" with `PurePosixPath(tok).name` against five bare POSIX names, so the
# Windows spelling of the SAME command walked past both readings with the
# payload intact -- and silently, because a quoted payload containing spaces
# resolves to no installer name, so not even the WARNING fired. Quoting is not
# exotic here: it is mandatory as soon as the path holds a space, which is
# `C:\Program Files\...`, the shape this whole issue is about.
NESTED_SHELL_TEMPLATES = [
    'bash.exe -c "{p}/.venv/bin/pip install foo"',
    'sh.exe -c "{p}/.venv/bin/pip install foo"',
    'cmd /c "{p}/.venv/bin/pip install foo"',
    'cmd.exe /c "{p}/.venv/bin/pip install foo"',
    'cmd /C "{p}/.venv/bin/pip install foo"',
    'powershell -Command "{p}/.venv/bin/pip install foo"',
    'pwsh -c "{p}/.venv/bin/pip install foo"',
]


@pytest.mark.parametrize("template", NESTED_SHELL_TEMPLATES)
def test_a_windows_nested_shell_cannot_launder_the_payload(
    primary, worktree, template, on_windows
):
    cmd = _backslashed(template.format(p=primary))
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(primary / ".venv"),
    ) is not None, cmd


def test_the_posix_nested_shell_control_still_holds(primary, worktree, on_posix):
    """`sh -c` was already refused before any of this. If this ever goes red
    the runner change broke the case it was modelled on."""
    cmd = f'sh -c "{primary}/.venv/bin/pip install foo"'
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(primary / ".venv"),
    ) is not None


def test_a_nested_shell_running_something_harmless_is_still_allowed(worktree, on_windows):
    """Negative control: recursing into payloads must not deny every nested
    shell. Installing into the session's OWN venv stays allowed."""
    cmd = _backslashed(f'cmd /c "{worktree}/.venv/bin/pip install foo"')
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(worktree / ".venv"),
    ) is None


# --- round 3: what the round-2 fix still let through ------------------------
# An adversarial re-review refuted the round-2 commit's own title. Each test
# below is a command it found ALLOWED.


@pytest.fixture
def spaced_primary(tmp_path, monkeypatch):
    """A primary checkout under a path with a space, as `C:\\Program Files`
    has. The round-2 NESTED_SHELL_TEMPLATES all use a space-free path, so the
    parametrisation structurally could not see this."""
    root = tmp_path / "Program Files" / "proj"
    (root / "src" / "no_human").mkdir(parents=True)
    (root / "src" / "no_human" / "__init__.py").write_text("")
    _make_venv_bin(root / ".venv")
    monkeypatch.setattr(guard, "_primary_checkout", lambda: root)
    return root


SPACED_NESTED = [
    'cmd /c "{p}/.venv/bin/pip install requests"',
    'cmd.exe /c "{p}/.venv/bin/pip install requests"',
    'bash.exe -c "{p}/.venv/bin/pip install requests"',
    'powershell -Command "{p}/.venv/bin/pip install requests"',
    'pwsh -c "{p}/.venv/bin/pip install requests"',
]


@pytest.mark.parametrize("template", SPACED_NESTED)
def test_a_nested_shell_cannot_launder_a_path_with_a_space(
    spaced_primary, worktree, template, on_windows
):
    """The outer lex eats the payload's quoting, so re-lexing splits the path
    at the space -- in BOTH readings, because the split is at the space and
    not the separator. Silent, too: the mangled token names no installer."""
    cmd = _backslashed(template.format(p=spaced_primary))
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(spaced_primary / ".venv"),
    ) is not None, cmd


def test_the_direct_spaced_path_still_denies(spaced_primary, worktree, on_windows):
    """Control: without a nested shell this already worked, and must keep
    working -- otherwise the fix above is masking a regression."""
    cmd = _backslashed(f'"{spaced_primary}/.venv/bin/pip" install requests')
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(spaced_primary / ".venv"),
    ) is not None


@pytest.mark.parametrize("installer, args", [
    ("pip.exe", "install foo"), ("uv.exe", "pip install foo"),
    ("PIP.EXE", "install foo"), ("Pip.Exe", "install foo"),
])
def test_a_bare_windows_installer_name_resolves(
    primary, worktree, installer, args, on_windows, monkeypatch
):
    """`_resolve_installer`'s no-separator branch tested the RAW token, so
    `pip.exe` was not an installer name and it returned before `shutil.which`
    AND before the WARNING. A real Windows venv's Scripts/ contains exactly
    these spellings."""
    scripts = primary / ".venv" / "bin"
    src = scripts / ("uv" if installer.lower().startswith("uv") else "pip")
    (scripts / installer).write_bytes(src.read_bytes())
    (scripts / installer).chmod(0o755)
    assert venv_install_guard.denial_reason(
        f"{installer} {args}", cwd=str(worktree),
        env=_env_pointing_at(primary / ".venv"),
    ) is not None, installer


# --- the residual, pinned as xfail so it cannot be quietly "fixed" by drift --
# Issue #312. `_spaced_path_candidates` closes the nested, installer-first
# shapes and nothing more. These are the shapes an adversarial review measured
# still open at this tip. They are xfail(strict=True) rather than deleted: if
# one starts passing, that is news and the suite says so, and meanwhile the
# file states the gap instead of implying the class is closed.


@pytest.mark.xfail(strict=True, reason="issue #312: top-level spaced path is not reconstructed")
def test_a_top_level_spaced_path_is_not_yet_covered(spaced_primary, worktree, on_windows):
    cmd = _backslashed(f"{spaced_primary}/.venv/bin/pip install foo")
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(spaced_primary / ".venv"),
    ) is not None


@pytest.mark.xfail(strict=True, reason="issue #312: the rejoin anchors at token 0")
def test_a_wrapper_inside_the_payload_is_not_yet_covered(
    spaced_primary, worktree, on_windows
):
    cmd = _backslashed(
        f'cmd /c "cd {spaced_primary} && {spaced_primary}/.venv/bin/pip install foo"')
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(spaced_primary / ".venv"),
    ) is not None


# --- what the Windows change cost POSIX ------------------------------------
#
# Lowercasing the script-flag test was added for cmd/PowerShell (`/C`,
# `-Command`). `"-C".lower()` is `"-c"`, and `-C` is `--directory` in this
# same module's `_TARGET_FLAGS`. So after a runner token, `-C` was read as
# "hand me the next token as a script", the payload was emitted BEFORE the
# flag, and `_mutating_subcommand` -- which walks RIGHT from the installer --
# read the directory as pip's subcommand instead of `install`:
#
#     main: [... 'pip', '-C', '<dir>', 'install', ...] -> 'install' -> DENY
#     here: [... 'pip', '<dir>', '-C', 'install', ...] -> '<dir>'   -> ALLOW
#
# Not Windows-gated, and nothing in the Windows corpus above could see it:
# every row there is a Windows spelling, and this is a POSIX regression.

@pytest.fixture
def outside_dir(tmp_path):
    """A directory that is not inside the session worktree, so an install
    resolving to it is one the guard already refuses."""
    d = tmp_path / "outside"
    d.mkdir()
    return d


#: Shapes where a runner token precedes the installer, so `seen_runner` is
#: set when `-C` is reached. `./cmd` is in there because `_basename` reads it
#: as the Windows shell name, which is how an ordinary relative path lands in
#: the runner set at all.
DASH_C_TEMPLATES = [
    "bash x.sh pip -C {d} install requests",
    "sh x.sh pip -C {d} install requests",
    "zsh pip -C {d} install requests",
    "./cmd pip -C {d} install requests",
    "powershell pip -C {d} install requests",
    "bash x.sh uv pip install -C {d} -e .",
]


@pytest.mark.parametrize("template", DASH_C_TEMPLATES)
@pytest.mark.parametrize("windows", [False, True])
def test_a_capital_C_after_a_runner_is_still_the_directory_flag(
    primary, worktree, outside_dir, template, windows, monkeypatch
):
    """`-C` is `--directory`, not `--command`, on BOTH platforms."""
    monkeypatch.setattr(venv_install_guard, "_IS_WINDOWS", windows, raising=False)
    cmd = template.format(d=outside_dir)
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(primary / ".venv"),
    ) is not None, cmd


@pytest.mark.parametrize("template", DASH_C_TEMPLATES)
def test_the_whole_guard_refuses_the_capital_C_shape(
    primary, worktree, outside_dir, template, on_posix
):
    """The same rows through `guard.evaluate`, the PreToolUse entry point."""
    cmd = template.format(d=outside_dir)
    decision = guard.evaluate(
        "Bash", {"command": cmd},
        forbidden_paths=[], never_push_to=[], cwd=str(worktree),
        env=_env_pointing_at(primary / ".venv"),
    )
    assert not decision.allow, cmd


def test_a_lowercase_c_after_a_runner_is_still_a_script_flag(
    primary, worktree, on_posix
):
    """The control for the row above: `-c` must keep meaning `--command`, or
    the fix would have closed the regression by disabling the recursion."""
    cmd = f'sh -c "{primary}/.venv/bin/pip install foo"'
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(primary / ".venv"),
    ) is not None


def test_the_windows_switches_are_the_only_ones_folded():
    """Stated on the predicate, because that is where the asymmetry lives:
    folding the POSIX set is what collided `-C` with `-c`."""
    assert venv_install_guard._is_script_flag("-c")
    assert not venv_install_guard._is_script_flag("-C")
    assert not venv_install_guard._is_script_flag("-LC")
    assert venv_install_guard._is_script_flag("/c")
    assert venv_install_guard._is_script_flag("/C")
    assert venv_install_guard._is_script_flag("-Command")
    assert venv_install_guard._is_script_flag("-command")


# --- a trailing separator must not hide the runner -------------------------
#
# `_flatten` read the runner name with `os.path.basename`, which returns `""`
# for `/bin/sh/` -- and `""` is in no name set, so the token stopped being a
# runner and the payload behind its `-c` was never expanded. A shell runs
# `/bin/sh/ -c ...` exactly as `/bin/sh -c ...`.

TRAILING_SEPARATOR_RUNNERS = [
    "/bin/sh/ -c",
    "/bin/bash/ -c",
    "sh/ -c",
    "/usr/bin/env/../bin/sh -c",
]


@pytest.mark.parametrize("runner", TRAILING_SEPARATOR_RUNNERS)
def test_a_trailing_separator_does_not_hide_the_shell_runner(
    primary, worktree, outside_dir, runner, on_posix
):
    cmd = f'{runner} "pip -C {outside_dir} install requests"'
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(primary / ".venv"),
    ) is not None, cmd


@pytest.mark.parametrize("spelling, bare", [
    ("/bin/sh/", "sh"), ("/bin/bash/", "bash"), ("sh/", "sh"),
    ("/bin/sh//", "sh"), ("/bin/sh/.", "sh"),
    ("/Scripts/pip.exe/", "pip"), ("/bin/cmd.EXE/", "cmd"),
])
def test_a_path_that_names_something_never_reads_as_an_empty_name(spelling, bare):
    """`""` fails every name test this module makes at once -- runner,
    installer, package manager -- so one empty return disarms all of them."""
    assert venv_install_guard._basename(spelling) == bare


# --- the shipped activation -------------------------------------------------
#
# Every Windows test above monkeypatches `venv_install_guard._IS_WINDOWS`,
# which is the very constant deciding whether the fix is live as SHIPPED.
# Setting it to a literal `False` -- the whole Windows path permanently off --
# therefore left the suite fully green: 396 passed, 2 xfailed. The tests
# proved the logic and proved nothing about whether it ever runs.


def test_the_platform_constants_are_computed_from_os_name(monkeypatch):
    """Asked on the OTHER platform, because value alone cannot answer it.

    On a POSIX host `os.name == "nt"` is False, so a hard-coded
    `_IS_WINDOWS = False` and the real expression are the same value and no
    assertion comparing values can tell them apart -- which is exactly why
    hard-coding it left the suite green. What distinguishes them is whether
    the constant CHANGES when the platform does, so this re-imports the
    modules under a patched `os.name` and requires that it does.

    `win_readings` imports nothing but `os`, and `guard` is re-imported into
    its own existing namespace; both are restored in `finally`.
    """
    assert win_readings._IS_WINDOWS == (os.name == "nt")
    assert guard._IS_WINDOWS == (os.name == "nt")
    monkeypatch.setattr(os, "name", "nt")
    try:
        assert importlib.reload(win_readings)._IS_WINDOWS is True
        assert importlib.reload(guard)._IS_WINDOWS is True
    finally:
        monkeypatch.undo()
        importlib.reload(win_readings)
        importlib.reload(guard)
    assert win_readings._IS_WINDOWS == (os.name == "nt")
    assert guard._IS_WINDOWS == (os.name == "nt")


def test_the_venv_guard_binds_the_shared_constant(monkeypatch):
    """The other half of the wiring, and it fails the same way on its own:
    `venv_install_guard._IS_WINDOWS = False` is indistinguishable from the
    binding by value on a POSIX host. It is distinguishable by whether it
    FOLLOWS `win_readings`, so that is what is asserted.
    """
    assert venv_install_guard._IS_WINDOWS is win_readings._IS_WINDOWS
    monkeypatch.setattr(win_readings, "_IS_WINDOWS", True)
    try:
        assert importlib.reload(venv_install_guard)._IS_WINDOWS is True
    finally:
        monkeypatch.undo()
        importlib.reload(venv_install_guard)
    assert venv_install_guard._IS_WINDOWS is win_readings._IS_WINDOWS


def test_readings_are_gated_on_the_flag_it_is_given():
    """The positive control for the assertion above: the constant is worth
    pinning only because passing it actually changes the answer."""
    backslashed = r"C:\proj\.venv\Scripts\pip.exe install foo"
    assert win_readings.readings(backslashed, is_windows=False) == [backslashed]
    assert len(win_readings.readings(backslashed, is_windows=True)) == 2


# --- an UPPERCASE runner name ----------------------------------------------
#
# `_basename(tok).lower() in _SHELL_RUNNERS`: dropping the `.lower()` left the
# suite green, because no row above spells a runner in upper case. On Windows
# `CMD` and `cmd` are one file, which is the same argument that put `cmd` in
# the set at all.

@pytest.mark.parametrize("runner", [
    "CMD /c", "CMD.EXE /C", "Cmd.exe /c", "POWERSHELL -Command", "PWSH -c",
    "BASH.EXE -c",
])
def test_an_uppercase_runner_name_is_still_a_runner(
    primary, worktree, runner, on_windows
):
    cmd = _backslashed(f'{runner} "{primary}/.venv/bin/pip install foo"')
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(primary / ".venv"),
    ) is not None, cmd


# --- more than one space in the path ---------------------------------------
#
# `_MAX_PREFIX_JOIN` is 8, and every spaced row above uses `Program Files` --
# exactly ONE space, so two tokens, so `k=2`. Lowering the cap to 2 left the
# suite green. `C:\Program Files\My Project\...` has two.


@pytest.fixture
def double_spaced_primary(tmp_path, monkeypatch):
    root = tmp_path / "Program Files" / "My Project"
    (root / "src" / "no_human").mkdir(parents=True)
    (root / "src" / "no_human" / "__init__.py").write_text("")
    _make_venv_bin(root / ".venv")
    monkeypatch.setattr(guard, "_primary_checkout", lambda: root)
    return root


@pytest.mark.parametrize("template", SPACED_NESTED)
def test_a_path_with_two_spaces_is_reconstructed_too(
    double_spaced_primary, worktree, template, on_windows
):
    """Three tokens, so the rejoin has to reach `k=3`. The one-space rows
    above are satisfied by `k=2` and cannot measure the cap at all."""
    cmd = _backslashed(template.format(p=double_spaced_primary))
    assert venv_install_guard.denial_reason(
        cmd, cwd=str(worktree), env=_env_pointing_at(double_spaced_primary / ".venv"),
    ) is not None, cmd


def test_the_rejoin_stops_at_its_bound(double_spaced_primary, worktree, on_windows):
    """The bound is a bound: a path with `_MAX_PREFIX_JOIN` spaces or more is
    NOT reconstructed, and issue #312 carries that. Asserted so raising the
    cap is a deliberate act rather than a side effect.
    """
    toks = ["x"] * (venv_install_guard._MAX_PREFIX_JOIN + 2)
    assert venv_install_guard._spaced_path_candidates(" ".join(toks)) == []
