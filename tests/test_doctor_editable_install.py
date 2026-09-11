"""editable_install_problem(): warn, never gate, when the editable install a
running `nh` process resolves through does not point inside the checkout it
was launched from.

The failure this guards: a coder worktree the shared venv's editable install
was built against gets garbage-collected, its _editable_impl_no_human.pth
entry now points at a directory that no longer exists, and `import no_human`
silently falls back to an empty PEP 420 namespace package (`__file__` is
None) — the checkout then *reads* as corrupted even though nothing in the
checkout itself is wrong. A second, subtler false positive this also guards
against: running `nh` from a linked worktree whose venv is legitimately
editable-installed from the PRIMARY checkout (or vice versa) is healthy, not
dangling — the check must not flag every multi-worktree setup.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

from no_human.cli import commands
from no_human.doctor import editable_install_problem


def _fake_checkout(tmp_path: Path, name: str = "checkout") -> Path:
    checkout = tmp_path / name
    pkg = checkout / "src" / "no_human"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    return checkout


# --- AC: a spec whose origin is None produces the warning -----------------

def test_namespace_package_spec_warns(tmp_path):
    checkout = _fake_checkout(tmp_path)
    msg = editable_install_problem(spec=SimpleNamespace(origin=None), checkout=checkout)
    assert msg
    assert "uv pip install -e ." in msg
    assert str(checkout) in msg
    assert "no __file__" in msg


def test_missing_spec_warns(tmp_path, monkeypatch):
    checkout = _fake_checkout(tmp_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    msg = editable_install_problem(checkout=checkout)
    assert msg
    assert "uv pip install -e ." in msg
    assert str(checkout) in msg


# --- AC: a spec resolving outside the checkout warns, naming both paths ---

def test_spec_outside_checkout_warns_naming_both_paths(tmp_path):
    checkout = _fake_checkout(tmp_path)
    other_pkg_dir = tmp_path / "other" / "site-packages" / "no_human"
    other_pkg_dir.mkdir(parents=True)
    other_init = other_pkg_dir / "__init__.py"
    other_init.write_text("")

    msg = editable_install_problem(
        spec=SimpleNamespace(origin=str(other_init)), checkout=checkout
    )
    assert msg
    assert str(other_pkg_dir) in msg
    assert str(checkout) in msg
    assert "uv pip install -e ." in msg


def test_spec_resolving_to_a_sibling_worktree_of_the_same_repo_is_silent(tmp_path, monkeypatch):
    # A shared venv can hold an editable install built from a DIFFERENT
    # worktree of the SAME repo (e.g. the primary checkout) than the one
    # `nh` is running from right now — that is healthy, not dangling.
    primary = _fake_checkout(tmp_path, "primary")
    linked = _fake_checkout(tmp_path, "linked")

    def fake_run(cmd, cwd, capture_output, text, timeout):
        if cmd[:2] == ["git", "worktree"]:
            out = f"worktree {primary}\nworktree {linked}\n"
            return SimpleNamespace(returncode=0, stdout=out)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    msg = editable_install_problem(
        spec=SimpleNamespace(origin=str(primary / "src" / "no_human" / "__init__.py")),
        checkout=linked,
    )
    assert msg is None


# --- AC: a healthy checkout produces nothing -------------------------------

def test_healthy_checkout_is_silent(tmp_path):
    checkout = _fake_checkout(tmp_path)
    origin = checkout / "src" / "no_human" / "__init__.py"
    msg = editable_install_problem(spec=SimpleNamespace(origin=str(origin)), checkout=checkout)
    assert msg is None


def test_real_process_is_silent(monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(repo_root)
    assert editable_install_problem() is None


def test_real_process_is_silent_from_a_subdirectory(monkeypatch):
    # A prior version anchored on the literal cwd instead of resolving cwd to
    # its OWN checkout: this reproduced a false positive from a subdirectory
    # while staying silent at the toplevel. `git rev-parse --show-toplevel`
    # already resolves subdirectories correctly; running from one must be as
    # silent as running from the root.
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(repo_root / "tests")
    assert editable_install_problem() is None


def test_unrelated_directory_is_silent(tmp_path):
    # `nh` run from some other project's directory (no no_human source tree
    # here) must never speak, regardless of what the real spec resolves to.
    assert editable_install_problem(checkout=tmp_path) is None


# --- AC: never raises, never changes the exit code -------------------------

class _ExplodingOrigin:
    @property
    def origin(self):
        raise RuntimeError("boom")


def test_never_raises_on_hostile_inputs(tmp_path, monkeypatch):
    checkout = _fake_checkout(tmp_path)

    assert editable_install_problem(spec=_ExplodingOrigin(), checkout=checkout) is None
    assert editable_install_problem(spec=SimpleNamespace(origin=42), checkout=checkout) is None

    import no_human.doctor as doctor_mod

    monkeypatch.setattr(
        doctor_mod, "_running_checkout", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert editable_install_problem(spec=SimpleNamespace(origin=None)) is None


def test_cli_helper_prints_and_returns_none(monkeypatch, capsys):
    import no_human.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "editable_install_problem", lambda: "sentinel warning text")

    def _boom(*a, **k):
        raise AssertionError("must not call sys.exit")

    monkeypatch.setattr(commands.sys, "exit", _boom)

    result = commands._warn_if_editable_install_dangles()
    assert result is None
    out = capsys.readouterr().out
    assert "sentinel warning text" in out


# --- Wiring: guards against the check being dead ---------------------------

def test_start_and_serve_call_the_check():
    src = Path(commands.__file__).read_text(encoding="utf-8")
    start_body = src.split('@cli.command("start")', 1)[1]
    serve_body = src.split('@cli.command("serve")', 1)[1].split('@cli.command("start")', 1)[0]
    assert "_warn_if_editable_install_dangles()" in start_body
    assert "_warn_if_editable_install_dangles()" in serve_body
