"""An attempt's environment work must never write outside its own worktree.

Issue #128, caught live mid-approval: `nh approve` worked at 20:15 and by
21:5x every `nh` invocation failed with `ModuleNotFoundError: No module named
'no_human.cli'`. A task attempt targeting no_human itself ran its `env_setup`
editable install inside its worktree, `uv` resolved the project's environment
to the PRIMARY checkout's shared `.venv`, and the install rewrote that venv's
`_editable_impl_no_human.pth` to a path under the worktree. When the worktree
was torn down the `.pth` pointed at nothing, `import no_human` fell back to
the data-only namespace directory in site-packages, and the CLI was dead.

What made it quiet is worth recording too: the long-running board server had
already imported its modules, so it kept serving perfectly while every fresh
CLI process was broken.

`agent/venv_install_guard.py` names this exact residual in its own module
docstring and says a smarter command pattern cannot close it, because the
information does not exist before the command runs; what it needs is the
environment scoped BEFORE the command. `isolate_attempt_env` is that scoping
for the commands the orchestrator runs itself.

SCOPE, corrected on review: this is PART of #128, not a fix for the observed
event. The maintainer went back to the database and found 0 of 939 tasks have
ever configured `env_setup`, and the receipts put the write in the CODER
session instead (`uv run --active` inside the worktree, at the minute the
.pth changed). That half belongs to the venv install guard. What is here is
the invariant for the orchestrator's OWN commands, which is worth having on
its own terms.
"""
from __future__ import annotations

import ast
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from no_human.core import worktree
from no_human.core.worktree import _VENV_BIN as _BIN, isolate_attempt_env

PYPROJECT = """\
[project]
name = "demopkg"
version = "0.1.0"
requires-python = ">=3.9"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/demopkg"]
"""


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=120)


def _make_repo(root: Path) -> Path:
    """A minimal installable project in a real git repo."""
    (root / "src" / "demopkg").mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (root / "src" / "demopkg" / "__init__.py").write_text(
        "VALUE = 'primary'\n", encoding="utf-8")
    _git("init", "-q", ".", cwd=root)
    _git("add", "-A", cwd=root)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init",
         cwd=root)
    return root


def _gitdir_stub(worktree: Path, target: Path) -> None:
    """Make `worktree` look like a LINKED worktree: `.git` is a file carrying
    a `gitdir:` line, never a directory."""
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text("gitdir: %s\n" % target, encoding="utf-8")


def _site_packages_digest(venv: Path) -> str:
    """One digest over every file under site-packages: the issue asks for the
    `.pth` specifically and for site-packages generally, and a digest covers
    both without naming the platform's directory layout."""
    files = sorted(p for p in venv.rglob("site-packages/**/*") if p.is_file())
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(venv)).replace("\\", "/").encode())
        h.update(f.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------
# The unit half: WHEN the pin applies. Each case is a way to get this wrong.
# --------------------------------------------------------------------------

def test_a_primary_checkout_is_never_repointed(tmp_path):
    """`isolation.enabled: false` runs the attempt straight in the operator's
    own checkout, whose `.git` is a DIRECTORY. Repointing their venv would be
    a worse bug than the one this fixes."""
    repo = tmp_path / "primary"
    (repo / ".git").mkdir(parents=True)
    shared = str(tmp_path / "elsewhere" / ".venv")
    out = isolate_attempt_env(repo, {"VIRTUAL_ENV": shared})
    assert out["VIRTUAL_ENV"] == shared


def test_a_worktree_with_no_inherited_venv_is_left_alone(tmp_path):
    """Nothing to displace means nothing to do: pinning here would build a
    venv for every attempt on a repo that never wanted one."""
    wt = tmp_path / "wt"
    _gitdir_stub(wt, tmp_path / "g")
    out = isolate_attempt_env(wt, {"PATH": "/usr/bin"})
    assert "VIRTUAL_ENV" not in out
    assert "UV_PROJECT_ENVIRONMENT" not in out


def test_a_venv_already_inside_the_worktree_is_left_alone(tmp_path):
    """That is already the isolation this function exists to produce."""
    wt = tmp_path / "wt"
    _gitdir_stub(wt, tmp_path / "g")
    inside = str(wt / ".venv")
    out = isolate_attempt_env(wt, {"VIRTUAL_ENV": inside})
    assert out["VIRTUAL_ENV"] == inside


def test_a_shared_venv_outside_the_worktree_is_pinned_inside_it(tmp_path):
    """The defect's own shape: a linked worktree inheriting a venv that lives
    outside it. BOTH keys must move. uv honours either, so pinning one and
    leaving the other still lets the install reach the shared venv."""
    wt = tmp_path / "wt"
    _gitdir_stub(wt, tmp_path / "g")
    shared = str(tmp_path / "primary" / ".venv")
    out = isolate_attempt_env(
        wt, {"VIRTUAL_ENV": shared, "UV_PROJECT_ENVIRONMENT": shared})
    assert Path(out["VIRTUAL_ENV"]) == wt / ".venv"
    assert Path(out["UV_PROJECT_ENVIRONMENT"]) == wt / ".venv"


def test_the_worktree_venv_bin_goes_to_the_front_of_path(tmp_path):
    """Pinning VIRTUAL_ENV and UV_PROJECT_ENVIRONMENT routes uv. It does NOT
    route pip, which installs into whichever interpreter runs it, and a bare
    `pip` is chosen by PATH.

    Found on review of this PR with two real venvs: with both variables pinned
    to the worktree but PATH still leading with the shared venv's bin,
    `pip install -e .` rewrote the SHARED venv's .pth, pin and all. That is
    the #128 defect surviving the fix that was supposed to close it."""
    wt = tmp_path / "wt"
    _gitdir_stub(wt, tmp_path / "g")
    bin_dir = wt / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)

    shared_bin = str(tmp_path / "primary" / ".venv" / "bin")
    out = isolate_attempt_env(wt, {
        "VIRTUAL_ENV": str(tmp_path / "primary" / ".venv"),
        "PATH": shared_bin + os.pathsep + "/usr/bin",
    })

    assert out["PATH"].split(os.pathsep)[0] == str(bin_dir), (
        "the worktree venv's bin must come FIRST, ahead of the shared venv's, "
        "or a bare `pip` still resolves to the shared one"
    )
    assert shared_bin in out["PATH"], "the rest of PATH must be preserved"


def test_path_is_left_alone_when_the_worktree_venv_was_never_built(tmp_path):
    """Pointing PATH at a directory that does not exist would shadow nothing
    and would replace a clear failure with a confusing one."""
    wt = tmp_path / "wt"
    _gitdir_stub(wt, tmp_path / "g")  # no pyproject.toml, so no venv is built
    before = "/usr/bin" + os.pathsep + "/bin"
    out = isolate_attempt_env(
        wt, {"VIRTUAL_ENV": str(tmp_path / "primary" / ".venv"), "PATH": before})
    assert out["PATH"] == before


def test_a_failed_venv_build_still_pins_and_does_not_fall_back(
        tmp_path, monkeypatch):
    """Fail CLOSED. Falling back to the shared venv when the worktree's own
    cannot be built is exactly the bug, so the pin stands and the attempt's
    command fails loudly instead."""
    wt = tmp_path / "wt"
    _gitdir_stub(wt, tmp_path / "g")
    (wt / "src").mkdir(parents=True, exist_ok=True)
    (wt / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    shared = str(tmp_path / "primary" / ".venv")

    def always_fails(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "no interpreter")

    monkeypatch.setattr(worktree.subprocess, "run", always_fails)
    out = isolate_attempt_env(wt, {"VIRTUAL_ENV": shared})
    assert Path(out["VIRTUAL_ENV"]) == wt / ".venv"
    assert not (wt / ".venv").exists()


def test_the_venv_is_built_with_this_interpreter_and_not_uv(
        tmp_path, monkeypatch):
    """`uv venv` MAY fetch an interpreter, which would put a network call
    inside an attempt's environment setup. `sys.executable -m venv` cannot
    leave the machine, and tests/test_egress_allowlist.py records that choice.
    Pin the argv so a future edit back to `uv venv` is a visible one."""
    wt = tmp_path / "wt"
    _gitdir_stub(wt, tmp_path / "g")
    (wt / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    seen = []

    def capture(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worktree.subprocess, "run", capture)
    isolate_attempt_env(wt, {"VIRTUAL_ENV": str(tmp_path / "p" / ".venv")})
    assert seen == [[sys.executable, "-m", "venv", str(wt / ".venv")]]


# --------------------------------------------------------------------------
# The end-to-end half the issue asked for: a real editable install, a real
# populated venv, and site-packages byte-identical across it.
# --------------------------------------------------------------------------

def test_env_setup_actually_runs_through_the_isolated_environment():
    """The wiring, not the helper. Every test above would still pass if the
    orchestrator stopped passing `env=`, which is precisely the line whose
    absence caused #128 in the first place, so pin the call site itself.

    Checked over the AST, not the source text: a substring search here would
    be satisfied by the word appearing in a comment, which is a mistake this
    repo has made before."""
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "core"
    tree = ast.parse((src / "orchestrator.py").read_text(encoding="utf-8"))

    runs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and any(kw.arg == "shell" for kw in node.keywords)
        and any(kw.arg == "timeout" for kw in node.keywords)
    ]
    assert runs, "no shell subprocess.run found in orchestrator.py"

    for call in runs:
        env = [kw for kw in call.keywords if kw.arg == "env"]
        assert env, (
            "orchestrator.py line %d runs a shell command with no env=, so it "
            "inherits VIRTUAL_ENV from this process and an install inside a "
            "worktree can rewrite the shared venv (issue #128)" % call.lineno)
        callee = env[0].value
        assert (isinstance(callee, ast.Call)
                and isinstance(callee.func, ast.Name)
                and callee.func.id == "isolate_attempt_env"), (
            "orchestrator.py line %d passes an env= that does not come from "
            "isolate_attempt_env" % call.lineno)


def test_the_other_orchestrator_run_shell_path_is_isolated_too():
    """`run_setup_commands` in core/worktree.py is a SECOND path where this
    process runs a shell command inside a worktree (a profile's `setup_cmds`).
    It landed after the env_setup fix was written, and `runner._env_for`
    inherits this process's VIRTUAL_ENV whenever the worktree has no venv yet,
    so it had the same defect.

    The wiring test above only walks orchestrator.py and structurally cannot
    see this one, which is how it was missed; this walks worktree.py."""
    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "core"
    tree = ast.parse((src / "worktree.py").read_text(encoding="utf-8"))

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "run_setup_commands"), None)
    assert fn is not None, "run_setup_commands is gone or was renamed"

    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "isolate_attempt_env" in called, (
        "run_setup_commands runs a shell command in the worktree without "
        "routing its environment through isolate_attempt_env, so a profile's "
        "setup_cmds can rewrite the shared checkout's venv (issue #128)"
    )


def test_a_frozen_build_never_runs_the_nh_binary_as_python(
        tmp_path, monkeypatch):
    """In a PyInstaller build `sys.executable` IS the `nh` binary, so
    `[sys.executable, "-m", "venv", ...]` runs `nh -m venv`, which re-enters
    the click CLI and exits 2 without creating anything. The pin still stands,
    so every env_setup command in the packaged app would then fail on any repo
    with a pyproject.toml. Found on review of this PR; the same scar is
    recorded in `testing/repro_gate.py::_pytest_python` for pytest."""
    wt = tmp_path / "wt"
    _gitdir_stub(wt, tmp_path / "g")
    (wt / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")

    # The venv being displaced, with a real interpreter inside it.
    displaced = tmp_path / "primary" / ".venv"
    sub, name = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    (displaced / sub).mkdir(parents=True)
    real_python = displaced / sub / name
    real_python.write_text("", encoding="utf-8")

    seen = []

    def capture(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worktree.sys, "frozen", True, raising=False)
    monkeypatch.setattr(worktree.subprocess, "run", capture)
    isolate_attempt_env(wt, {"VIRTUAL_ENV": str(displaced)})

    assert seen, "no venv build was attempted"
    assert Path(seen[0][0]) == real_python, (
        "a frozen build must not invoke sys.executable (the nh binary) as an "
        "interpreter; got %r" % seen[0][0]
    )
    assert seen[0][0] != sys.executable


def test_a_frozen_build_with_no_usable_interpreter_still_does_not_fall_back(
        tmp_path, monkeypatch):
    """Fail closed stays fail closed: no interpreter means no venv, the pin
    still stands, and the operator's venv is still not rewritten."""
    wt = tmp_path / "wt"
    _gitdir_stub(wt, tmp_path / "g")
    (wt / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    shared = str(tmp_path / "primary" / ".venv")

    monkeypatch.setattr(worktree.sys, "frozen", True, raising=False)
    monkeypatch.setattr(worktree.shutil, "which", lambda _n: None)
    out = isolate_attempt_env(wt, {"VIRTUAL_ENV": shared})

    assert Path(out["VIRTUAL_ENV"]) == wt / ".venv"
    assert not (wt / ".venv").exists()


@pytest.mark.slow
def test_a_worktree_editable_install_leaves_the_shared_venv_byte_identical(
        tmp_path):
    """The regression test #128 specifies. The plant is the first half: run
    the same install the way the orchestrator used to, inheriting the shared
    `VIRTUAL_ENV`, and site-packages changes underneath the operator. Then run
    it through `isolate_attempt_env` and it does not."""
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")

    primary = _make_repo(tmp_path / "primary")
    venv = primary / ".venv"
    assert subprocess.run(["uv", "venv", str(venv)], capture_output=True,
                          timeout=300).returncode == 0
    assert subprocess.run(
        ["uv", "pip", "install", "-e", ".", "--python", str(venv)],
        cwd=str(primary), capture_output=True, timeout=300).returncode == 0

    baseline = _site_packages_digest(venv)

    # THE PLANT: the pre-fix behaviour, cwd in the worktree with the shared
    # VIRTUAL_ENV inherited.
    plant_wt = tmp_path / "wt_plant"
    assert _git("worktree", "add", "-q", str(plant_wt), "-b", "plant",
                cwd=primary).returncode == 0
    inherited = {**os.environ, "VIRTUAL_ENV": str(venv),
                 "UV_PROJECT_ENVIRONMENT": str(venv)}
    subprocess.run(["uv", "pip", "install", "-e", "."], cwd=str(plant_wt),
                   env=inherited, capture_output=True, timeout=300)
    assert _site_packages_digest(venv) != baseline, (
        "the plant did not reproduce: an editable install from inside a "
        "worktree was expected to rewrite the shared venv")

    # Restore, then run the same install through the isolated environment.
    assert subprocess.run(
        ["uv", "pip", "install", "-e", ".", "--python", str(venv)],
        cwd=str(primary), capture_output=True, timeout=300).returncode == 0
    restored = _site_packages_digest(venv)

    real_wt = tmp_path / "wt_real"
    assert _git("worktree", "add", "-q", str(real_wt), "-b", "real",
                cwd=primary).returncode == 0
    isolated = isolate_attempt_env(real_wt, inherited)
    assert Path(isolated["VIRTUAL_ENV"]) == real_wt / ".venv"
    proc = subprocess.run(["uv", "pip", "install", "-e", "."],
                          cwd=str(real_wt), env=isolated, capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0, (
        "install into the worktree's own venv failed:\n%s" % proc.stderr)
    assert _site_packages_digest(venv) == restored, (
        "the shared venv changed even though the attempt's environment was "
        "pinned inside its own worktree")


@pytest.mark.slow
def test_a_bare_pip_install_also_leaves_the_shared_venv_byte_identical(
        tmp_path):
    """The uv half above passes on the PIN alone. pip does not read
    VIRTUAL_ENV or UV_PROJECT_ENVIRONMENT at all: it installs into whichever
    interpreter runs it, and a bare `pip` is chosen by PATH. So this half
    passes only because `_prepend_venv_bin` puts the worktree venv's bin
    first, and it is the half that was failing on review of this PR.

    Resolution is asserted with `shutil.which(..., path=...)`, which is what a
    shell does, rather than by spawning `pip` and trusting the platform:
    Windows resolves a bare program name from the PARENT process PATH rather
    than from the env passed to the child, so spawning would quietly test
    nothing there.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")

    primary = _make_repo(tmp_path / "primary")
    venv = primary / ".venv"
    assert subprocess.run([sys.executable, "-m", "venv", str(venv)],
                          capture_output=True, timeout=300).returncode == 0
    shared_pip = shutil.which("pip", path=str(venv / _BIN))
    if shared_pip is None:
        pytest.skip("this interpreter's venv module did not seed pip")
    assert subprocess.run([shared_pip, "install", "-e", ".", "-q"],
                          cwd=str(primary), capture_output=True,
                          timeout=600).returncode == 0
    baseline = _site_packages_digest(venv)

    wt = tmp_path / "wt"
    assert _git("worktree", "add", "-q", str(wt), "-b", "pipwt",
                cwd=primary).returncode == 0

    inherited = {**os.environ,
                 "VIRTUAL_ENV": str(venv),
                 "UV_PROJECT_ENVIRONMENT": str(venv),
                 "PATH": str(venv / _BIN) + os.pathsep + os.environ.get("PATH", "")}
    isolated = isolate_attempt_env(wt, inherited)

    resolved = shutil.which("pip", path=isolated["PATH"])
    assert resolved is not None, "no pip on the isolated PATH"
    assert Path(resolved).is_relative_to(wt), (
        "a bare `pip` still resolves outside the worktree (%s), so it would "
        "install into the shared venv no matter what the pin says" % resolved
    )

    proc = subprocess.run([resolved, "install", "-e", ".", "-q"], cwd=str(wt),
                          env=isolated, capture_output=True, text=True,
                          timeout=600)
    assert proc.returncode == 0, "pip install into the worktree venv failed:\n%s" % proc.stderr
    assert _site_packages_digest(venv) == baseline, (
        "a bare pip install inside the worktree rewrote the SHARED venv; the "
        "VIRTUAL_ENV pin does not reach pip, only PATH does"
    )
