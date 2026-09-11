
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


# The realistic secured state when the OWNER is a local admin (the CI runner's
# case): /inheritance:r cannot strip the EXPLICIT Administrators/SYSTEM ACEs an
# admin-owned file carries. SYSTEM + Administrators are the platform TCB (POSIX
# root's analog) and are accepted; only a genuinely OTHER account is a defect.
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

    # Force the POSIX branch rather than relying on the HOST being POSIX — see
    # the identical note in tests/test_windows_portability.py. On a Windows host
    # this took the Windows branch and tripped its own tripwire.
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
    platform TCB) alongside it are accepted and must NOT be blamed — the message
    names only the account that is the real defect.
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
    """The common secured state — owner + SYSTEM + Administrators — is WRITTEN.

    On an admin-owned file the explicit Administrators/SYSTEM ACEs survive
    /inheritance:r; excluding them is impossible and stricter than the POSIX
    0600 contract this mirrors. So this must succeed, not fail closed.
    """
    fake = _FakeIcacls(_ICACLS_SHARED)
    monkeypatch.setattr(cfg, "_run_icacls", fake)
    target = tmp_path / ".env"
    cfg.atomic_write_0600(target, "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n")
    assert target.read_text(encoding="utf-8") == "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n"


def test_non_owner_grantees_accepts_tcb_flags_others():
    """The readback filter, directly: TCB accepted, any other account flagged."""
    grantees = {"CORP\\alice", "NT AUTHORITY\\SYSTEM",
                "BUILTIN\\Administrators", "BUILTIN\\Users"}
    # SYSTEM + Administrators + owner are accepted; only Users survives.
    assert cfg._non_owner_grantees(grantees, "CORP\\alice") == {"BUILTIN\\Users"}
    # Owner + TCB alone ⇒ nothing flagged.
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
