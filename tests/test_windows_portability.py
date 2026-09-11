"""Windows-correctness tests, driven from POSIX.

No Windows host or runner is available to this project, so every Windows branch
in the shipped code is written to be reachable from a POSIX process: the
platform test is a module constant (`config._IS_WINDOWS`,
`app._IS_WINDOWS`, ...) rather than an inline `os.name` check, and the one
external command each branch depends on (`icacls`, `taskkill`) is behind a
seam a test can drive.

What these tests CAN prove: that the branch is taken, that it issues the right
command shape, that it parses the right output, and — most importantly — that
it does NOT silently succeed when the platform cannot honour the request. What
they CANNOT prove is that Windows itself behaves as documented; that is stated
per-branch in the source and in the report, and needs a real Windows boot.

The paired POSIX assertions are the regression half: every test that exercises
a Windows branch has a sibling proving the POSIX behaviour is byte-identical to
what it was before.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from no_human import config as cfg  # noqa: E402

# --------------------------------------------------------------------------- #
# Defect 1 — the credential file's 0600 is a silent no-op on Windows           #
# --------------------------------------------------------------------------- #

# A faithful sample of `icacls <path>` output, in the shape Windows emits it:
# the path is repeated on the first line, subsequent grantees are indented, and
# a two-line summary follows a blank line.
_ICACLS_SHARED = (
    "{path} NT AUTHORITY\\SYSTEM:(F)\n"
    "        BUILTIN\\Administrators:(F)\n"
    "        CORP\\alice:(R,W)\n"
    "\n"
    "Successfully processed 1 files; Failed processing 0 files\n"
)
_ICACLS_OWNER_ONLY = (
    "{path} CORP\\alice:(R,W)\n"
    "\n"
    "Successfully processed 1 files; Failed processing 0 files\n"
)
# Owner + SYSTEM + Administrators + a genuinely OTHER account. SYSTEM and the
# local Administrators group are the platform TCB (POSIX root's analog, which
# 0600 also cannot exclude) and are accepted; only BUILTIN\Users is a defect.
_ICACLS_OTHER_USER = (
    "{path} NT AUTHORITY\\SYSTEM:(F)\n"
    "        BUILTIN\\Administrators:(F)\n"
    "        CORP\\alice:(R,W)\n"
    "        BUILTIN\\Users:(RX)\n"
    "\n"
    "Successfully processed 1 files; Failed processing 0 files\n"
)


class _FakeIcacls:
    """Stands in for `_run_icacls`, recording argv and scripting the readback."""

    def __init__(self, readback: str, *, grant_rc: int = 0, read_rc: int = 0):
        self.readback = readback
        self.grant_rc = grant_rc
        self.read_rc = read_rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        if "/grant:r" in args:
            return self.grant_rc, ""
        return self.read_rc, self.readback.format(path=args[0])


@pytest.fixture
def as_windows(monkeypatch):
    """Take every Windows branch, with a nameable owner account."""
    monkeypatch.setattr(cfg, "_IS_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "CORP")


def test_atomic_write_0600_posix_still_chmods(tmp_path):
    """The POSIX path is unchanged: 0600 from the first byte, real chmod."""
    target = tmp_path / ".env"
    cfg.atomic_write_0600(target, "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n")
    assert target.read_text(encoding="utf-8") == "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n"
    assert (target.stat().st_mode & 0o777) == 0o600
    # And no temp file survives the write.
    assert not (tmp_path / ".env.tmp").exists()


def test_atomic_write_0600_posix_does_not_shell_out(tmp_path, monkeypatch):
    """No subprocess on POSIX — the ACL work must not leak onto the fast path."""
    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("POSIX write must not spawn a process")

    # Force the POSIX branch rather than relying on the HOST being POSIX. Every
    # "windows" test in this file pins `_IS_WINDOWS = True`; this one asserted a
    # POSIX property while leaving the flag at whatever the host happened to be,
    # so on a Windows host it took the WINDOWS branch, called _run_icacls, and
    # tripped its own tripwire. The flag exists precisely so neither branch
    # depends on which machine runs the suite.
    monkeypatch.setattr(cfg, "_IS_WINDOWS", False)
    monkeypatch.setattr(cfg, "_run_icacls", _boom)
    cfg.atomic_write_0600(tmp_path / ".env", "K=v\n")
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "K=v\n"


def test_atomic_write_0600_windows_restricts_acl_before_writing(
    tmp_path, monkeypatch, as_windows
):
    """The Windows branch replaces the inherited ACL and reads it back.

    The order matters and is asserted: the grant happens while the file is
    still EMPTY, so a token is never written into a file whose permissions are
    unproven.
    """
    fake = _FakeIcacls(_ICACLS_OWNER_ONLY)
    monkeypatch.setattr(cfg, "_run_icacls", fake)
    target = tmp_path / ".env"
    cfg.atomic_write_0600(target, "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n")

    assert target.read_text(encoding="utf-8") == "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n"
    grant = fake.calls[0]
    assert grant[1:] == ["/inheritance:r", "/grant:r", "CORP\\alice:(R,W)"]
    assert grant[0].endswith(".env.tmp"), (
        "the ACL must be applied to the TEMP file, before os.replace")
    assert len(fake.calls) == 2 and "/grant:r" not in fake.calls[1], (
        "the grant must be followed by a readback that verifies it")


def test_atomic_write_0600_windows_refuses_when_icacls_is_absent(
    tmp_path, as_windows
):
    """No icacls ⇒ no way to secure the file ⇒ refuse to write it.

    This is the test that would have caught the original defect: before the
    fix, taking the Windows branch wrote the token and returned successfully
    having applied no protection at all. `icacls` genuinely does not exist on
    this POSIX host, so nothing is stubbed here.
    """
    target = tmp_path / ".env"
    with pytest.raises(cfg.CredentialPermissionError) as exc:
        cfg.atomic_write_0600(target, "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n")
    assert "icacls" in str(exc.value)
    assert not target.exists(), "no credential may be left behind"
    assert not (tmp_path / ".env.tmp").exists(), "no temp file may be left behind"


def test_atomic_write_0600_windows_refuses_when_others_retain_access(
    tmp_path, monkeypatch, as_windows
):
    """A grant that leaves a NON-TCB, non-owner account ⇒ refuse, and name it.

    `icacls` exiting 0 is not evidence: this is the readback catching a file
    another standard user can still reach. SYSTEM and Administrators (the
    platform TCB) alongside it are accepted and must NOT be blamed.
    """
    fake = _FakeIcacls(_ICACLS_OTHER_USER)
    monkeypatch.setattr(cfg, "_run_icacls", fake)
    target = tmp_path / ".env"
    with pytest.raises(cfg.CredentialPermissionError) as exc:
        cfg.atomic_write_0600(target, "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n")
    msg = str(exc.value)
    readable = msg.split("readable by")[1].split(".")[0]
    assert "BUILTIN\\Users" in readable
    assert "NT AUTHORITY\\SYSTEM" not in readable
    assert "BUILTIN\\Administrators" not in readable
    assert "CORP\\alice" not in readable
    assert not target.exists()


def test_atomic_write_0600_windows_accepts_tcb_and_owner(
    tmp_path, monkeypatch, as_windows
):
    """Owner + SYSTEM + Administrators is the common secured state on an
    admin-owned file (the CI runner's), and must be WRITTEN, not refused."""
    fake = _FakeIcacls(_ICACLS_SHARED)
    monkeypatch.setattr(cfg, "_run_icacls", fake)
    target = tmp_path / ".env"
    cfg.atomic_write_0600(target, "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n")
    assert target.read_text(encoding="utf-8") == "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n"


def test_non_owner_grantees_accepts_tcb_flags_others():
    """The readback filter, directly: TCB accepted, any other account flagged."""
    grantees = {"CORP\\alice", "NT AUTHORITY\\SYSTEM",
                "BUILTIN\\Administrators", "BUILTIN\\Users"}
    assert cfg._non_owner_grantees(grantees, "CORP\\alice") == {"BUILTIN\\Users"}
    assert cfg._non_owner_grantees(
        {"CORP\\alice", "NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators"},
        "CORP\\alice") == set()


def test_is_windows_tcb_principal_names_and_sids():
    """SYSTEM and Administrators by localized name AND well-known SID; nobody
    else — icacls prints a raw SID when it cannot resolve a name."""
    for g in ("NT AUTHORITY\\SYSTEM", "builtin\\administrators",
              "S-1-5-18", "*S-1-5-32-544"):
        assert cfg._is_windows_tcb_principal(g), g
    for g in ("BUILTIN\\Users", "Everyone", "CORP\\bob", "S-1-5-11"):
        assert not cfg._is_windows_tcb_principal(g), g


def test_atomic_write_0600_windows_refuses_when_grant_fails(
    tmp_path, monkeypatch, as_windows
):
    """A non-zero icacls exit is fatal, not a warning."""
    monkeypatch.setattr(cfg, "_run_icacls", _FakeIcacls(_ICACLS_OWNER_ONLY,
                                                        grant_rc=5))
    with pytest.raises(cfg.CredentialPermissionError):
        cfg.atomic_write_0600(tmp_path / ".env", "K=v\n")
    assert not (tmp_path / ".env").exists()


def test_atomic_write_0600_windows_refuses_without_a_named_account(
    tmp_path, monkeypatch
):
    """No USERNAME ⇒ no grantee to name ⇒ refuse rather than guess."""
    monkeypatch.setattr(cfg, "_IS_WINDOWS", True)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USERDOMAIN", raising=False)
    with pytest.raises(cfg.CredentialPermissionError) as exc:
        cfg.atomic_write_0600(tmp_path / ".env", "K=v\n")
    assert "USERNAME" in str(exc.value)
    assert not (tmp_path / ".env").exists()


def test_owner_principal_omits_the_domain_when_unset(monkeypatch):
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.delenv("USERDOMAIN", raising=False)
    assert cfg._windows_owner_principal() == "alice"


def test_icacls_grantees_parses_windows_output(tmp_path):
    """The readback parser, against the real output shape."""
    p = tmp_path / ".env"
    grantees = cfg._icacls_grantees(p, _ICACLS_SHARED.format(path=p))
    assert grantees == {"NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators",
                        "CORP\\alice"}
    assert cfg._icacls_grantees(p, _ICACLS_OWNER_ONLY.format(path=p)) == {
        "CORP\\alice"}


def test_icacls_grantees_survives_a_drive_letter_in_the_path(tmp_path):
    """`C:\\Users\\...` contains a colon; the parser must not split on it."""
    p = Path("C:\\Users\\alice\\.no_human\\.env")
    out = (f"{p} CORP\\alice:(R,W)\n\n"
           "Successfully processed 1 files; Failed processing 0 files\n")
    assert cfg._icacls_grantees(p, out) == {"CORP\\alice"}


def test_owner_only_check_is_case_insensitive(tmp_path, monkeypatch, as_windows):
    """Windows account names are case-insensitive; a case difference on a
    correctly secured file must not fail closed."""
    monkeypatch.setattr(cfg, "_run_icacls", _FakeIcacls(
        "{path} corp\\ALICE:(R,W)\n\nSuccessfully processed 1 files\n"))
    cfg.windows_assert_owner_only(tmp_path / ".env")  # must not raise


def test_owner_only_check_rejects_an_empty_grantee_list(
    tmp_path, monkeypatch, as_windows
):
    """"icacls said nothing" is not "icacls said it is private"."""
    monkeypatch.setattr(cfg, "_run_icacls", _FakeIcacls(
        "\nSuccessfully processed 1 files; Failed processing 0 files\n"))
    with pytest.raises(cfg.CredentialPermissionError) as exc:
        cfg.windows_assert_owner_only(tmp_path / ".env")
    assert "no grantees" in str(exc.value)


def test_upsert_env_var_fails_closed_on_windows(tmp_path, as_windows):
    """The real credential writer, not just its primitive, fails closed."""
    env = tmp_path / ".env"
    env.write_text("EXISTING=1\n", encoding="utf-8")
    with pytest.raises(cfg.CredentialPermissionError):
        cfg.upsert_env_var(env, "CLAUDE_CODE_OAUTH_TOKEN", "sk-tok")
    assert env.read_text(encoding="utf-8") == "EXISTING=1\n", (
        "a refused write must leave the previous .env untouched")


def test_secure_credential_file_chmods_on_posix(tmp_path):
    p = tmp_path / "jenkins_storage_state.json"
    p.write_text("{}", encoding="utf-8")
    p.chmod(0o644)
    cfg.secure_credential_file(p)
    assert (p.stat().st_mode & 0o777) == 0o600


def test_secure_credential_file_raises_on_windows(tmp_path, as_windows):
    p = tmp_path / "jenkins_storage_state.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(cfg.CredentialPermissionError):
        cfg.secure_credential_file(p)


def test_ensure_private_dir_warns_but_does_not_raise_on_windows(
    tmp_path, monkeypatch, caplog, as_windows
):
    """The DIRECTORY downgrades to a warning — it holds no credential, and the
    .env inside is secured (and fails closed) independently."""
    d = tmp_path / "no_human"
    with caplog.at_level("WARNING"):
        assert cfg.ensure_private_dir(d) == d
    assert d.is_dir()
    assert any("could not secure" in r.message for r in caplog.records)


def test_ensure_private_dir_grants_directory_inheritance_on_windows(
    tmp_path, monkeypatch, as_windows
):
    """A directory ACE needs (OI)(CI) to reach the files created inside it."""
    monkeypatch.setattr(cfg, "_run_icacls", _FakeIcacls(_ICACLS_OWNER_ONLY))
    d = tmp_path / "no_human"
    cfg.ensure_private_dir(d)
    assert "CORP\\alice:(OI)(CI)(F)" in cfg._run_icacls.calls[0]


def test_env_file_round_trips_non_ascii(tmp_path):
    """Explicit UTF-8 on both ends: the locale default is cp1252 on Windows."""
    env = tmp_path / ".env"
    cfg.upsert_env_var(env, "SSO_PASSWORD", "pässwörd—ok")
    assert cfg._read_env_file(env)["SSO_PASSWORD"] == "pässwörd—ok"
    assert env.read_bytes().decode("utf-8")


# --------------------------------------------------------------------------- #
# Defect 2 — the claude CLI is resolved through POSIX-only paths               #
# --------------------------------------------------------------------------- #

def test_cli_fallbacks_are_windows_shaped(monkeypatch, tmp_path):
    """On Windows the npm shim is `claude.cmd`, not an extensionless `claude`.

    Before the fix the suffix was computed for the BUNDLED binary only; every
    fallback location still looked for an extensionless `claude`, so a working
    npm-global install was reported missing and `nh doctor` told the operator
    to install a CLI they already had.
    """
    from no_human.agent import backend_check as bc

    # The SDK's bundled CLI wins step 1 and short-circuits before the fallback
    # list this test exercises. On a real Windows runner that bundled binary is
    # `claude.exe` and DOES exist, so null the SDK out to reach the fallbacks —
    # the same isolation `test_posix_cli_resolution_is_unchanged` performs.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    monkeypatch.setattr(bc, "_IS_WINDOWS", True)
    monkeypatch.setattr(bc.shutil, "which", lambda _n: None)
    monkeypatch.setattr(bc.Path, "home", staticmethod(lambda: tmp_path))
    shim = tmp_path / "AppData" / "Roaming" / "npm" / "claude.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    assert bc.find_claude_cli() == str(shim)


def test_cli_fallbacks_cover_the_posix_npm_layout_on_windows(
    monkeypatch, tmp_path
):
    """Git-Bash / WSL-style installs put it at `.npm-global/bin/claude.cmd`."""
    from no_human.agent import backend_check as bc

    # Null the SDK so its bundled CLI does not win step 1 (see sibling test).
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    monkeypatch.setattr(bc, "_IS_WINDOWS", True)
    monkeypatch.setattr(bc.shutil, "which", lambda _n: None)
    monkeypatch.setattr(bc.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)
    shim = tmp_path / ".npm-global" / "bin" / "claude.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off", encoding="utf-8")
    assert bc.find_claude_cli() == str(shim)


def test_cli_does_not_probe_usr_local_bin_on_windows(monkeypatch, tmp_path):
    """`/usr/local/bin/claude` is meaningless on Windows and must not be
    reported — on a machine with a POSIX-layout mount it would name a binary
    Windows cannot execute."""
    from no_human.agent import backend_check as bc

    monkeypatch.setattr(bc, "_IS_WINDOWS", True)
    monkeypatch.setattr(bc.shutil, "which", lambda _n: None)
    monkeypatch.setattr(bc.Path, "home", staticmethod(lambda: tmp_path))
    probed: list[str] = []
    real_is_file = Path.is_file

    def _spy(self):
        probed.append(str(self))
        return real_is_file(self)

    monkeypatch.setattr(bc.Path, "is_file", _spy)
    bc.find_claude_cli()
    assert not any(p.startswith("/usr/") for p in probed), probed


def test_posix_cli_resolution_is_unchanged(monkeypatch, tmp_path):
    """The macOS path keeps looking for an extensionless `claude`, in the same
    locations, including /usr/local/bin."""
    from no_human.agent import backend_check as bc

    # This host has the SDK installed, and its bundled CLI wins step 1. Take it
    # out so the FALLBACK list — the thing under test — is what answers.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    monkeypatch.setattr(bc, "_IS_WINDOWS", False)
    monkeypatch.setattr(bc.shutil, "which", lambda _n: None)
    monkeypatch.setattr(bc.Path, "home", staticmethod(lambda: tmp_path))
    binary = tmp_path / ".npm-global" / "bin" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh", encoding="utf-8")
    assert bc.find_claude_cli() == str(binary)


def test_which_is_asked_for_the_bare_name_on_windows(monkeypatch, tmp_path):
    """`shutil.which("claude")` is CORRECT on Windows and must stay: `which`
    expands PATHEXT, so the bare name is what resolves the `claude.cmd` npm
    shim. Asking for "claude.exe" would narrow the search and miss it."""
    from no_human.agent import backend_check as bc

    # Null the SDK so step 1 (bundled CLI) does not answer before `which` is
    # ever consulted — on a real Windows runner the bundled `claude.exe` exists.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    asked: list[str] = []
    monkeypatch.setattr(bc, "_IS_WINDOWS", True)
    monkeypatch.setattr(bc.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(bc.shutil, "which",
                        lambda n: asked.append(n) or None)
    bc.find_claude_cli()
    assert asked == ["claude"]


# --------------------------------------------------------------------------- #
# Defect 3 — process termination has no Windows equivalent                    #
# --------------------------------------------------------------------------- #

class _Ran:
    """Records argv and scripts a stdout, standing in for `subprocess.run`."""

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return self


def _run_async(coro):
    import asyncio as _asyncio
    return _asyncio.run(coro)


def test_kill_task_processes_enumerates_and_taskkills_on_windows(monkeypatch):
    """`pkill` does not exist on Windows: cancelling a task there raised
    FileNotFoundError, was swallowed as "best-effort", and every SDK/pytest
    child of the cancelled task kept running and holding its worktree."""
    api_app = importlib.import_module("no_human.api.app")

    ran = _Ran(stdout="4321\n8765\n")
    monkeypatch.setattr(api_app, "_IS_WINDOWS", True)
    monkeypatch.setattr(api_app.subprocess, "run", ran)
    task_id = "a" * 32
    assert _run_async(api_app._kill_task_processes(task_id)) == 1

    enum = ran.calls[0]
    assert enum[0] == "powershell" and "-NoProfile" in enum
    assert f"'*{task_id}*'" in enum[-1], enum[-1]
    assert "$_.ProcessId -ne $PID" in enum[-1], (
        "the enumerating PowerShell carries the id in its own command line "
        "and would otherwise match — and kill — itself")
    assert f"-ne {os.getpid()}" in enum[-1], (
        "our own process must be excluded, or cancelling a task kills the "
        "server that is cancelling it")
    assert ran.calls[1:] == [["taskkill", "/F", "/T", "/PID", "4321"],
                             ["taskkill", "/F", "/T", "/PID", "8765"]]


def test_kill_task_processes_refuses_an_unsafe_id_on_windows(monkeypatch):
    """The id is interpolated into a PowerShell string. Anything that could
    carry quoting is refused, not escaped."""
    api_app = importlib.import_module("no_human.api.app")

    ran = _Ran()
    monkeypatch.setattr(api_app, "_IS_WINDOWS", True)
    monkeypatch.setattr(api_app.subprocess, "run", ran)
    assert _run_async(
        api_app._kill_task_processes("abcdef123456'; rm -rf *; '")) == 0
    assert ran.calls == [], "no command may be built from an unsafe id"


def test_kill_task_processes_still_uses_pkill_on_posix(monkeypatch):
    api_app = importlib.import_module("no_human.api.app")

    ran = _Ran()
    monkeypatch.setattr(api_app, "_IS_WINDOWS", False)
    monkeypatch.setattr(api_app.subprocess, "run", ran)
    assert _run_async(api_app._kill_task_processes("b" * 32)) == 1
    assert ran.calls == [["pkill", "-9", "-f", "b" * 32]]


def test_kill_task_processes_still_refuses_a_short_id(monkeypatch):
    """The too-broad-pattern guard survives the platform split."""
    api_app = importlib.import_module("no_human.api.app")

    ran = _Ran()
    monkeypatch.setattr(api_app, "_IS_WINDOWS", True)
    monkeypatch.setattr(api_app.subprocess, "run", ran)
    assert _run_async(api_app._kill_task_processes("abc")) == 0
    assert ran.calls == []


# --------------------------------------------------------------------------- #
# Defect 4 — `os.kill(pid, 0)` TERMINATES the process on Windows              #
# --------------------------------------------------------------------------- #

class _FakeKernel32:
    """A scriptable stand-in for the Win32 handle-query API."""

    def __init__(self, handle: int, exit_code: int = 259, last_error: int = 0,
                 get_exit_ok: int = 1):
        self.handle = handle
        self.exit_code = exit_code
        self.last_error = last_error
        self.get_exit_ok = get_exit_ok
        self.calls: list[str] = []

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - Win32 name
        self.calls.append(f"OpenProcess({access:#x},{pid})")
        return self.handle

    def GetExitCodeProcess(self, handle, out):  # noqa: N802 - Win32 name
        self.calls.append("GetExitCodeProcess")
        out._obj.value = self.exit_code
        return self.get_exit_ok

    def CloseHandle(self, handle):  # noqa: N802 - Win32 name
        self.calls.append("CloseHandle")


def test_probe_pid_never_signals_on_windows(monkeypatch):
    """On Windows `os.kill` ALWAYS calls TerminateProcess — signal 0 included.

    So the instance lock's liveness probe did not test whether another `nh` was
    running: it KILLED it, then reported the lock free and took it. The Windows
    branch must not reach os.kill at all.
    """
    cmds = importlib.import_module("no_human.cli.commands")

    monkeypatch.setattr(cmds, "_IS_WINDOWS", True)
    monkeypatch.setattr(cmds.os, "kill", lambda *a: (_ for _ in ()).throw(
        AssertionError("os.kill must never be reached on Windows")))
    fake = _FakeKernel32(handle=7)
    monkeypatch.setattr(cfg, "_kernel32", lambda: fake)  # the probe now lives in config
    assert cmds._probe_pid(1234) is True
    assert fake.calls == ["OpenProcess(0x1000,1234)", "GetExitCodeProcess",
                          "CloseHandle"]


def test_probe_pid_reports_an_exited_process_as_gone(monkeypatch):
    """A handle can still be opened on a process that has exited; the exit code
    is what distinguishes it from a live one."""
    cmds = importlib.import_module("no_human.cli.commands")

    monkeypatch.setattr(cmds, "_IS_WINDOWS", True)
    monkeypatch.setattr(cfg, "_kernel32",
                        lambda: _FakeKernel32(handle=7, exit_code=0))
    assert cmds._probe_pid(1234) is False


def test_probe_pid_distinguishes_denied_from_gone_on_windows(monkeypatch):
    """ERROR_ACCESS_DENIED means the process exists and is someone else's —
    the tri-state the POSIX branch gets from EPERM vs ESRCH."""
    cmds = importlib.import_module("no_human.cli.commands")
    import ctypes

    monkeypatch.setattr(cmds, "_IS_WINDOWS", True)
    monkeypatch.setattr(cfg, "_kernel32", lambda: _FakeKernel32(handle=0))
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    assert cmds._probe_pid(1234) is None
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87, raising=False)
    assert cmds._probe_pid(1234) is False


def test_probe_pid_still_uses_signal_zero_on_posix(monkeypatch):
    """THE LIVE-PID PROBE IS POSIX-HOST-ONLY, and the reason is a scar, not a
    style choice. This test pins ``_IS_WINDOWS = False`` and then ran the REAL
    ``os.kill(os.getpid(), 0)`` — but on a Windows HOST, signal 0 is
    ``signal.CTRL_C_EVENT``, and ``os.kill`` implements it with
    ``GenerateConsoleCtrlEvent``: a Ctrl-C BROADCAST to the console process
    group. Not a no-op liveness probe, not even a kill of the target — a kill
    of EVERYTHING sharing the console. Delivery is asynchronous, so pytest died
    a few tests LATER with a KeyboardInterrupt at a misleading location ("after
    exactly 40 tests, inside Thread.join"), and the harness session driving the
    suite — same console — died with it, three sessions running. The mocked
    half below pins the branch's wiring on every host; the real-signal half
    proves an OS guarantee and belongs only to hosts whose signal 0 IS that
    guarantee."""
    cmds = importlib.import_module("no_human.cli.commands")

    monkeypatch.setattr(cmds, "_IS_WINDOWS", False)
    if os.name != "nt":
        assert cmds._probe_pid(os.getpid()) is True

    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(cmds.os, "kill", lambda p, s: seen.append((p, s)))
    assert cmds._probe_pid(4321) is True
    assert seen == [(4321, 0)]


def test_probe_pid_posix_tristate_is_unchanged(monkeypatch):
    cmds = importlib.import_module("no_human.cli.commands")
    monkeypatch.setattr(cmds, "_IS_WINDOWS", False)

    def _raise(exc):
        def _k(p, s):
            raise exc
        return _k

    monkeypatch.setattr(cmds.os, "kill", _raise(ProcessLookupError()))
    assert cmds._probe_pid(1) is False
    monkeypatch.setattr(cmds.os, "kill", _raise(PermissionError()))
    assert cmds._probe_pid(1) is None


def test_try_kill_uses_taskkill_on_windows(monkeypatch):
    """There is no SIGTERM/SIGKILL to send on Windows, and naming
    `signal.SIGKILL` at all is an AttributeError there."""
    cmds = importlib.import_module("no_human.cli.commands")
    import subprocess as sp

    monkeypatch.setattr(cmds, "_IS_WINDOWS", True)
    ran = _Ran()

    class _P:
        returncode = 0
        stdout = "SUCCESS"
        stderr = ""

    monkeypatch.setattr(sp, "run", lambda argv, **kw: ran.calls.append(argv) or _P())
    assert cmds._try_kill(4321, cmds._KILL_TERM) is True
    assert cmds._try_kill(4321, cmds._KILL_FORCE) is True
    assert ran.calls == [["taskkill", "/T", "/PID", "4321"],
                         ["taskkill", "/F", "/T", "/PID", "4321"]]


def test_try_kill_reads_taskkill_outcomes(monkeypatch):
    """"not found" is "already gone", "denied" is "not ours" — and an unknown
    failure must NOT be reported as gone, or `nh stop` deletes the pidfile of a
    process that is still running."""
    cmds = importlib.import_module("no_human.cli.commands")
    import subprocess as sp

    monkeypatch.setattr(cmds, "_IS_WINDOWS", True)

    def _scripted(rc, out):
        class _P:
            returncode = rc
            stdout = out
            stderr = ""
        monkeypatch.setattr(sp, "run", lambda argv, **kw: _P())

    _scripted(128, 'ERROR: The process "4321" not found.')
    assert cmds._try_kill(4321) is False
    _scripted(1, "ERROR: Access is denied.")
    assert cmds._try_kill(4321) is None
    _scripted(1, "ERROR: something else entirely")
    assert cmds._try_kill(4321) is True


def test_try_kill_still_signals_on_posix(monkeypatch):
    import signal as _signal

    cmds = importlib.import_module("no_human.cli.commands")
    monkeypatch.setattr(cmds, "_IS_WINDOWS", False)
    seen: list[tuple[int, int]] = []
    # `signal.SIGKILL` does not exist on Windows, so forcing the POSIX branch on
    # a Windows host made _try_kill raise AttributeError at commands.py:4352 —
    # the very shape test_stop_path_never_names_sigkill_at_module_scope below
    # exists to fence. Supplying the constant lets the POSIX branch be exercised
    # from either host; the assertion is unchanged. 9 is SIGKILL's POSIX value.
    monkeypatch.setattr(_signal, "SIGKILL", getattr(_signal, "SIGKILL", 9),
                        raising=False)
    monkeypatch.setattr(cmds.os, "kill", lambda p, s: seen.append((p, s)))
    assert cmds._try_kill(4321, cmds._KILL_TERM) is True
    assert cmds._try_kill(4321, cmds._KILL_FORCE) is True
    assert seen == [(4321, _signal.SIGTERM), (4321, _signal.SIGKILL)]


def test_stop_path_never_names_sigkill_at_module_scope():
    """A regression fence for the AttributeError shape: `signal.SIGKILL` must
    not be reachable except behind the POSIX branch of `_try_kill`."""
    cmds = importlib.import_module("no_human.cli.commands")
    src = Path(cmds.__file__).read_text(encoding="utf-8")
    hits = [ln.strip() for ln in src.splitlines()
            if "signal.SIGKILL" in ln and not ln.strip().startswith("#")]
    assert hits == ["sig = signal.SIGKILL if level == _KILL_FORCE else signal.SIGTERM"], hits


# --------------------------------------------------------------------------- #
# Sweep fixes — the rest of the POSIX-only class                              #
# --------------------------------------------------------------------------- #

def test_venv_bin_finds_the_windows_layout(monkeypatch, tmp_path):
    """A Windows venv is `<venv>\\Scripts\\python.exe`. Probing only
    `<venv>/bin/python` found NO venv there, ever — so the PATH/VIRTUAL_ENV
    injection in `_env_for` silently did nothing and the test command ran
    against whatever interpreter was first on PATH."""
    runner = importlib.import_module("no_human.testing.runner")

    monkeypatch.setattr(runner, "_IS_WINDOWS", True)
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    assert runner._venv_bin(tmp_path) == scripts


def test_venv_bin_posix_layout_is_unchanged(monkeypatch, tmp_path):
    runner = importlib.import_module("no_human.testing.runner")

    monkeypatch.setattr(runner, "_IS_WINDOWS", False)
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("", encoding="utf-8")
    assert runner._venv_bin(tmp_path) == bin_dir
    # And a Windows-shaped venv must NOT be picked up on POSIX.
    other = tmp_path / "venv" / "Scripts"
    other.mkdir(parents=True)
    (other / "python.exe").write_text("", encoding="utf-8")
    assert runner._venv_bin(tmp_path) == bin_dir


def test_env_for_injects_the_windows_scripts_dir(monkeypatch, tmp_path):
    """The end-to-end consequence: the venv actually reaches PATH."""
    runner = importlib.import_module("no_human.testing.runner")

    monkeypatch.setattr(runner, "_IS_WINDOWS", True)
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("", encoding="utf-8")
    env = runner._env_for(tmp_path)
    assert env["PATH"].startswith(str(scripts))
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


def test_popen_kwargs_are_platform_correct():
    """`start_new_session` is POSIX-only; on Windows Popen silently accepts and
    ignores it, so the group-creating kwargs must come from _NEW_GROUP_KWARGS.
    On Windows that constant is ``hidden_console_kwargs(new_group=True)`` —
    CREATE_NEW_PROCESS_GROUP **combined with** CREATE_NO_WINDOW, because the
    streaming child is both in a new group AND console-suppressed (Windows N=0).
    This pins the CONSTANT (both flags); the wiring of the streaming Popen site
    to it is pinned by the sibling test below."""
    runner = importlib.import_module("no_human.testing.runner")

    if os.name == "nt":  # pragma: no cover - not this host
        from no_human.proc import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW
        assert runner._NEW_GROUP_KWARGS == {
            "creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    else:
        assert runner._NEW_GROUP_KWARGS == {"start_new_session": True}


def test_streaming_popen_is_wired_to_the_platform_kwargs(monkeypatch):
    """`_run_shell_streaming` passed a literal `start_new_session=True` — the
    one Popen site the platform kwargs did not reach, so on Windows its child
    was never in a new group and the timeout path called the raw `os.killpg`
    (AttributeError there). The wiring is what a literal can silently bypass,
    so the wiring is what this pins: substitute _NEW_GROUP_KWARGS with a
    sentinel and the streaming Popen must carry it — under BOTH platform
    settings the constant itself can take (the previous test pins which of
    those settings each platform gets)."""
    runner = importlib.import_module("no_human.testing.runner")

    for kwargs in ({"start_new_session": True}, {"creationflags": 0x200}):
        sentinel = dict(kwargs)
        monkeypatch.setattr(runner, "_NEW_GROUP_KWARGS", sentinel)
        seen: dict[str, object] = {}

        class _Proc:
            pid = 977
            returncode = 0
            stdout = iter(())

            def wait(self, timeout=None):
                return 0

        def fake_popen(cmd, **kw):
            seen.update(kw)
            return _Proc()

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(runner, "_register", lambda *a: None)
        monkeypatch.setattr(runner, "_deregister", lambda *a: None)

        rc, out, timed_out = runner._run_shell_streaming(
            "true", Path("."), 5, {}, lambda line: None)
        assert timed_out is False
        for key, value in sentinel.items():
            assert seen.get(key) == value, (key, seen)
        assert "start_new_session" not in seen or \
            sentinel.get("start_new_session") == seen["start_new_session"], (
            "a literal start_new_session bypassed _NEW_GROUP_KWARGS")


def test_kill_process_tree_uses_taskkill_on_windows(monkeypatch):
    """`os.killpg`/`os.getpgid` DO NOT EXIST on Windows — the timeout path
    raised AttributeError and a wedged test run could never be reaped."""
    runner = importlib.import_module("no_human.testing.runner")

    monkeypatch.setattr(runner, "_IS_WINDOWS", True)
    # raising=False because `os.killpg` DOES NOT EXIST on a Windows host, and
    # monkeypatch.setattr refuses to patch a missing attribute. Without it this
    # test — the one that proves the Windows branch reaches taskkill — was the
    # single test in this file that could not run ON Windows, failing during
    # setup with AttributeError before its body executed. It passed everywhere
    # it was not needed and errored on the only platform it describes.
    #
    # Nothing is weakened: this ACE is a tripwire, and creating it on Windows
    # keeps the tripwire armed. If _kill_process_tree ever reaches killpg there,
    # the lambda still throws. On POSIX the attribute exists and this behaves
    # exactly as before.
    monkeypatch.setattr(runner.os, "killpg", lambda *a: (_ for _ in ()).throw(
        AssertionError("killpg must never be reached on Windows")), raising=False)
    ran = _Ran()

    class _P:
        returncode = 0

    monkeypatch.setattr(runner.subprocess, "run",
                        lambda argv, **kw: ran.calls.append(argv) or _P())

    class _Proc:
        pid = 4321

    assert runner._kill_process_tree(_Proc()) is True
    assert ran.calls == [["taskkill", "/F", "/T", "/PID", "4321"]]


def test_kill_process_tree_still_killpgs_on_posix(monkeypatch):
    runner = importlib.import_module("no_human.testing.runner")
    import signal as _signal

    monkeypatch.setattr(runner, "_IS_WINDOWS", False)
    seen: list[tuple[int, int]] = []
    # raising=False on all three: `os.getpgid`, `os.killpg` and `signal.SIGKILL`
    # DO NOT EXIST on a Windows host, so monkeypatch refused to create them and
    # this test errored during setup there. That is the mirror image of the
    # reason the Windows branches are gated behind a patchable `_IS_WINDOWS`
    # flag in the first place — "so the Windows branches are reachable (and
    # therefore testable) from any platform", per config.py. The symmetry has to
    # hold in both directions or half this file is dead on whichever host runs it.
    #
    # The assertion is unchanged and still proves the POSIX branch signals the
    # process GROUP with SIGKILL; only the constants are supplied on a platform
    # that does not define them. 9 is SIGKILL's real POSIX value.
    monkeypatch.setattr(_signal, "SIGKILL", getattr(_signal, "SIGKILL", 9),
                        raising=False)
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(runner.os, "killpg", lambda g, s: seen.append((g, s)),
                        raising=False)

    class _Proc:
        pid = 4321

    assert runner._kill_process_tree(_Proc()) is True
    assert seen == [(4321, _signal.SIGKILL)]
    # A dead process is reported as "tree kill did not happen", so the caller
    # still falls back to killing the direct child — unchanged behaviour.
    monkeypatch.setattr(runner.os, "killpg", lambda g, s: (_ for _ in ()).throw(
        ProcessLookupError()), raising=False)
    assert runner._kill_process_tree(_Proc()) is False


def test_forbidden_path_guard_matches_backslash_paths(monkeypatch):
    """The SDK reports `C:\\repo\\secrets\\key.pem` on Windows. Every rule in
    `_path_forbidden` is written in `/` terms, so the guard silently stopped
    matching — a guard that stops matching fails OPEN."""
    guard = importlib.import_module("no_human.agent.guard")

    monkeypatch.setattr(guard, "_IS_WINDOWS", True)
    assert guard._path_forbidden("src\\secrets\\key.pem", ["secrets/"]) is True
    assert guard._path_forbidden("C:\\repo\\.env", ["*.env"]) is True
    assert guard._path_forbidden("src\\app.py", ["secrets/"]) is False


def test_forbidden_path_guard_posix_is_unchanged(monkeypatch):
    """A backslash is a legal character in a POSIX filename, so it must NOT be
    reinterpreted as a separator there."""
    guard = importlib.import_module("no_human.agent.guard")

    monkeypatch.setattr(guard, "_IS_WINDOWS", False)
    assert guard._path_forbidden("src/secrets/key.pem", ["secrets/"]) is True
    assert guard._path_forbidden("src\\secrets\\key.pem", ["secrets/"]) is False


def test_scope_guard_compares_posix_separators(tmp_path, monkeypatch):
    """Plan files are `/`-separated (they come from the plan text and from
    git). `str(WindowsPath)` yields backslashes, so the membership test never
    matched and EVERY edit was reported out of scope.

    The POSIX half below cannot fail: `str()` and `as_posix()` agree on a
    PosixPath, so it passed with the defect still in place (measured by
    reverting the fix). The discriminating half swaps `scope_guard.Path` for
    `PureWindowsPath` so the separator logic runs as it would on Windows, and
    it reads the separator out of the WARNING TEXT rather than out of an
    in-scope verdict: a plan file whose full path matches always has a
    matching basename too, so the fuzzy fallback returns None either way and
    no in-scope assertion can tell the fix from the defect.
    """
    from pathlib import PureWindowsPath

    sg = importlib.import_module("no_human.agent.scope_guard")

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    edited = root / "src" / "app.py"
    edited.write_text("x", encoding="utf-8")
    assert sg.check_scope(str(edited), {"src/app.py"}, repo_root=str(root)) is None

    monkeypatch.setattr(sg, "Path", PureWindowsPath)
    warning = sg.check_scope("C:\\repo\\src\\other.py", {"src/app.py"},
                             repo_root="C:\\repo")
    assert warning is not None
    assert "'src/other.py'" in warning
    assert "\\" not in warning


def test_gitlab_probe_does_not_name_usr_bin_true_on_windows(monkeypatch):
    """`/usr/bin/true` does not exist on Windows; git would fail to spawn the
    askpass and fall back to prompting — the anti-hang guard becoming the
    hang."""
    ints = importlib.import_module("no_human.integrations")

    # The probe builds `env = {**os.environ, ...}`, so the assertions below —
    # "the POSIX branch did not ADD GCM_INTERACTIVE", "the Windows branch did
    # not inherit GIT_ASKPASS" — only hold against a clean baseline. A real
    # Windows runner has Git Credential Manager installed and carries an
    # ambient GCM_INTERACTIVE; strip both so each assertion reflects what the
    # branch under test sets, not what the host happens to export.
    monkeypatch.delenv("GCM_INTERACTIVE", raising=False)
    monkeypatch.delenv("GIT_ASKPASS", raising=False)

    captured: dict[str, dict] = {}

    class _P:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(ints, "_run_probe",
                        lambda cmd, **kw: captured.update(kw) or _P())
    monkeypatch.setattr(ints, "_IS_WINDOWS", True)
    ints._probe_gitlab_ambient()
    env = captured["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_ASKPASS" not in env
    assert env["GCM_INTERACTIVE"] == "never"

    captured.clear()
    monkeypatch.setattr(ints, "_IS_WINDOWS", False)
    ints._probe_gitlab_ambient()
    assert captured["env"]["GIT_ASKPASS"] == "/usr/bin/true"
    assert "GCM_INTERACTIVE" not in captured["env"]


def test_git_env_allowlist_carries_a_home_on_windows():
    """Git for Windows resolves `~` from USERPROFILE (or HOMEDRIVE+HOMEPATH),
    never from HOME. With only "HOME" allowed through, the sanitised git env
    had no home at all on Windows and ~/.gitconfig — identity, credential
    helper, core.autocrlf — was silently never read."""
    api_app = importlib.import_module("no_human.api.app")

    keep = api_app._GIT_ENV_KEEP
    assert {"USERPROFILE", "HOMEDRIVE", "HOMEPATH"} <= keep
    assert {"SystemRoot", "COMSPEC", "PATHEXT"} <= keep, (
        "a Windows process cannot start without these")
    # The POSIX set is untouched — this is an addition, not a replacement.
    assert {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ"} <= keep
    # And nothing that redirects git's writes or identity has been let in.
    assert not any(k.startswith("GIT_") for k in keep)
