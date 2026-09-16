import inspect
import os
import shutil
import sys

from no_human.proc import (
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    hidden_console_kwargs,
    real_python,
)
from no_human.testing import ui_evidence


def test_windows_flags_hide_console_and_new_group():
    kw = hidden_console_kwargs(new_group=True, platform="win32")
    assert kw == {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}


def test_windows_flags_hide_console_only():
    assert hidden_console_kwargs(platform="win32") == {"creationflags": CREATE_NO_WINDOW}


def test_posix_new_group():
    assert hidden_console_kwargs(new_group=True, platform="darwin") == {"start_new_session": True}


def test_posix_plain():
    assert hidden_console_kwargs(platform="linux") == {}


# --- real_python: in a PyInstaller build `sys.executable` is the frozen `nh` --
# --- binary, not a Python, so handing it a script re-enters the click CLI. ----
# --- Four modules had written this fallback out inline; the resolution lives --
# --- here now (issue #402). ---------------------------------------------------


def _plant(venv, *, windows_shape=False):
    """A venv root with an interpreter FILE inside it, both layouts."""
    sub, name = ("Scripts", "python.exe") if windows_shape else ("bin", "python")
    (venv / sub).mkdir(parents=True)
    exe = venv / sub / name
    exe.write_text("", encoding="utf-8")
    return exe


def test_an_ordinary_install_is_just_sys_executable(monkeypatch, tmp_path):
    """Not frozen: no probing at all, and a venv argument is ignored — the
    interpreter running nh already has nh's dependencies."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    _plant(tmp_path / "venv")
    assert real_python(tmp_path / "venv") == sys.executable


def test_a_frozen_build_prefers_the_venv_over_path(monkeypatch, tmp_path):
    exe = _plant(tmp_path / "venv", windows_shape=(os.name == "nt"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/python3")
    assert real_python(tmp_path / "venv") == str(exe)


def test_a_frozen_build_takes_the_first_venv_that_has_an_interpreter(
        monkeypatch, tmp_path):
    """Ordered preference, and a directory that merely exists is not enough:
    `_builder_python` and `_pytest_python` both pass a venv that may have
    been torn down between the probe and the call."""
    (tmp_path / "empty" / "bin").mkdir(parents=True)
    exe = _plant(tmp_path / "second", windows_shape=(os.name == "nt"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert real_python(tmp_path / "empty", tmp_path / "second") == str(exe)


def test_a_frozen_build_skips_none_entries(monkeypatch, tmp_path):
    """Callers pass `None` for "this repo has no venv", so it must not be
    probed as a path — `Path(None)` would raise."""
    exe = _plant(tmp_path / "venv", windows_shape=(os.name == "nt"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert real_python(None, tmp_path / "venv", None) == str(exe)


def test_a_frozen_build_falls_back_to_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}" if n == "python3" else None)
    assert real_python(tmp_path / "absent") == "/usr/bin/python3"


def test_a_frozen_build_with_no_interpreter_anywhere_is_none(monkeypatch, tmp_path):
    """The whole point of returning None rather than `sys.executable`: the
    caller must fail closed and say so, never hand the argv to the CLI."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert real_python(tmp_path / "absent", None) is None


def test_a_frozen_build_never_returns_the_frozen_binary(monkeypatch, tmp_path):
    """The defect itself, as an assertion: whatever comes back, it is not the
    `nh` binary `sys.executable` points at in the bundle."""
    nh = tmp_path / "nh"
    nh.write_text("", encoding="utf-8")
    exe = _plant(tmp_path / "venv", windows_shape=(os.name == "nt"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(nh))
    assert real_python(tmp_path / "venv") == str(exe) != str(nh)


def test_ui_evidence_is_named_as_a_deliberate_non_consumer():
    """`testing/ui_evidence.py::_hermetic_start_argv` deliberately runs the
    frozen `nh` binary itself (there is no separate Python to hand a module
    path to in a freeze) — the one call site this helper must NOT absorb.
    The docstring says so, and the source still does it, so nobody "fixes"
    it into a fifth `real_python` call site."""
    assert "ui_evidence" in real_python.__doc__
    source = inspect.getsource(ui_evidence._hermetic_start_argv)
    assert '[sys.executable] if getattr(sys, "frozen", False)' in source
