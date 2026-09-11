"""A clean clone must install; a release without the board must not build.

Both halves are one decision, and for a while only one of them was made. The
board (`web/dist`) is a gitignored build artifact that a release MUST carry, so
it was declared with a static `force-include` — chosen precisely because
hatchling raises when the source is missing, which fails a release cut without
`npm run build` instead of shipping a CLI whose board never renders.

Static configuration has no idea which build is running. So the release-time
guard also fired on the editable wheel that `uv sync` and `pip install -e .`
build, and a fresh clone of this repository could not be installed at all:

    $ git clone … && cd no_human && uv sync
    FileNotFoundError: Forced include not found: <repo>/web/dist
    Call to `hatchling.build.build_editable` failed (exit status: 1)   # exit 1

`hatch_build.py` moves the declaration to where the build VERSION is visible.
The tests below hold both edges of that: an editable build proceeds (with a
warning) without a board, a distributable build still refuses, and a
distributable build WITH a board still ships it.

Two tiers, following `test_wheel_ships_board.py`. The fast tier drives the
hook's decision function directly — importable without hatchling, which lives
only inside the isolated build environment. The slow tier runs real builds
against a real clean clone, because the fast tier cannot prove that hatchling
passes the string "editable" for an editable install, and that assumption is the
whole mechanism.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_hatch_build():
    """Import the root build script as a plain module.

    Also the assertion that it CAN be: it is loaded by hatchling from a file
    path, never as a package, and the tests below could not exist if importing
    it required hatchling.
    """
    path = REPO_ROOT / "hatch_build.py"
    spec = importlib.util.spec_from_file_location("no_human_hatch_build", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hatch_build = _load_hatch_build()


# --------------------------------------------------------------------------- #
# Fast tier — the decision itself                                              #
# --------------------------------------------------------------------------- #

def _board(root: Path) -> None:
    (root / "web" / "src").mkdir(parents=True, exist_ok=True)
    (root / "web" / "src" / "App.jsx").write_text("export default null\n")
    dist = root / "web" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<html><div id='root'></div></html>")
    hatch_build.write_stamp(root)


def test_an_editable_build_without_a_board_proceeds_and_says_so(tmp_path):
    """The defect. No board, editable build: include nothing, warn, continue."""
    additions, warning = hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "editable")

    assert additions == {}, (
        "an editable build force-included a board that does not exist — "
        "hatchling would raise and the clean clone would be uninstallable again")
    assert warning, "the install proceeded silently; a developer would find an "\
                    "empty board with no explanation"
    assert "npm run build" in warning, warning
    assert str(tmp_path / "web" / "dist" / "index.html") in warning, warning


def test_a_distributable_build_without_a_board_still_fails(tmp_path):
    """The guarantee that must survive the fix.

    A wheel or sdist is what a user installs, so a boardless one is the bug
    that already shipped. `version` is anything but "editable".
    """
    with pytest.raises(hatch_build.BoardNotBuiltError) as excinfo:
        hatch_build.plan_board_inclusion(
            tmp_path, "web/dist", "no_human/web_dist", "standard",
            target_name="wheel")

    message = str(excinfo.value)
    # Actionable, not a raw traceback: the command, the path, and the target.
    assert "npm run build" in message, message
    assert str(tmp_path / "web" / "dist" / "index.html") in message, message
    assert "wheel" in message, message


def test_a_board_that_is_present_is_included_by_both_versions(tmp_path):
    """Dev behaviour must not change where the board exists."""
    _board(tmp_path)

    for version in ("editable", "standard"):
        additions, warning = hatch_build.plan_board_inclusion(
            tmp_path, "web/dist", "no_human/web_dist", version)
        assert additions == {"web/dist": "no_human/web_dist"}, version
        assert warning is None, version


def test_an_empty_board_directory_is_not_a_board(tmp_path):
    """`npm run build` writes INTO the directory, so an interrupted or cleaned
    build leaves the husk behind. Directory-existence would force-include that
    husk into a release wheel — the boardless CLI again, wearing a directory.
    `api/app.py::_resolve_web_dist` refuses the same husk for the same reason."""
    (tmp_path / "web" / "dist").mkdir(parents=True)

    with pytest.raises(hatch_build.BoardNotBuiltError):
        hatch_build.plan_board_inclusion(
            tmp_path, "web/dist", "no_human/web_dist", "standard")

    additions, warning = hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "editable")
    assert additions == {} and warning


def test_pyproject_routes_the_board_through_the_hook_and_not_the_static_table():
    """The wiring. A static entry is what made the clean clone uninstallable, so
    its ABSENCE is as load-bearing as the hook's presence."""
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = cfg["tool"]["hatch"]["build"]["targets"]

    for name, expected_target in (("wheel", "no_human/web_dist"),
                                  ("sdist", "web/dist")):
        static = targets[name].get("force-include", {})
        assert "web/dist" not in static, (
            f"the {name} target force-includes web/dist statically again — that "
            "is evaluated for the editable build too, and `uv sync` on a clean "
            "clone dies in build_editable")
        hook = targets[name]["hooks"]["custom"]
        assert hook["path"] == "hatch_build.py"
        assert hook["source"] == "web/dist"
        assert hook["target"] == expected_target


# --------------------------------------------------------------------------- #
# Editable installs BUILD the board when npm is available                      #
# --------------------------------------------------------------------------- #

def _npm_stub(calls, *, install_rc=0, build_rc=0, write_index=True):
    """A fake `runner` for `try_build_board`: records `(argv, cwd)` and, for a
    successful `npm run build`, writes the artifact the real command would."""
    def runner(argv, cwd, **kwargs):
        calls.append((list(argv), Path(cwd)))
        if argv == ["npm", "install"]:
            return subprocess.CompletedProcess(argv, install_rc)
        if argv == ["npm", "run", "build"]:
            if build_rc == 0 and write_index:
                dist = Path(cwd) / "dist"
                dist.mkdir(parents=True, exist_ok=True)
                (dist / "index.html").write_text("<html><div id='root'></div></html>")
            return subprocess.CompletedProcess(argv, build_rc)
        raise AssertionError(f"unexpected npm invocation: {argv}")
    return runner


def _checkout(root: Path) -> None:
    """A minimal stand-in for a real checkout: just enough for the
    `web/package.json` precondition that keeps the other fast tests from
    ever shelling out to a real npm."""
    (root / "web").mkdir(parents=True, exist_ok=True)
    (root / "web" / "package.json").write_text("{}")


def test_an_editable_build_runs_npm_and_ships_the_board_it_produces(
    tmp_path, monkeypatch,
):
    """npm on PATH: the build actually runs, in `web/`, in the documented
    order, and the artifact it produces is what gets shipped."""
    _checkout(tmp_path)
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: "/usr/bin/npm")
    calls = []

    built = hatch_build.try_build_board(
        tmp_path, "web/dist", runner=_npm_stub(calls))

    assert built is True
    assert (tmp_path / "web" / "dist" / "index.html").is_file()
    assert calls == [
        (["npm", "install"], tmp_path / "web"),
        (["npm", "run", "build"], tmp_path / "web"),
    ]
    for cwd in (c for _, c in calls):
        assert cwd == tmp_path / "web"
    assert hatch_build._build_it() == (
        "cd web && npm install && npm run build && cd .. "
        "&& python3 hatch_build.py --stamp")


def test_a_successful_editable_build_is_included_with_no_warning(
    tmp_path, monkeypatch,
):
    """The same success, through `plan_board_inclusion`'s public seam."""
    _checkout(tmp_path)
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: "/usr/bin/npm")
    stub_builder = lambda root, source: hatch_build.try_build_board(
        root, source, runner=_npm_stub([]))

    additions, warning = hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "editable",
        builder=stub_builder)

    assert additions == {"web/dist": "no_human/web_dist"}
    assert warning is None


def test_without_npm_the_editable_install_still_succeeds_with_the_same_warning(
    tmp_path, monkeypatch,
):
    """npm absent: the install proceeds and the warning is byte-identical to
    the pre-existing one — nothing about its text changed."""
    _checkout(tmp_path)
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: None)

    additions, warning = hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "editable")

    assert additions == {}
    expected = hatch_build.board_missing_warning(
        tmp_path / "web" / "dist" / "index.html")
    assert warning == expected


@pytest.mark.parametrize("install_rc, build_rc", [(1, 0), (0, 1)])
def test_a_failing_npm_build_warns_and_does_not_fail_the_install(
    tmp_path, monkeypatch, install_rc, build_rc,
):
    """A non-zero exit from either step never raises — it falls back to the
    warning, and the install itself still succeeds."""
    _checkout(tmp_path)
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: "/usr/bin/npm")
    runner = _npm_stub([], install_rc=install_rc, build_rc=build_rc)

    built = hatch_build.try_build_board(tmp_path, "web/dist", runner=runner)
    assert built is False

    additions, warning = hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "editable",
        builder=lambda root, source: hatch_build.try_build_board(
            root, source, runner=_npm_stub([], install_rc=install_rc,
                                            build_rc=build_rc)))
    assert additions == {}
    expected = hatch_build.board_missing_warning(
        tmp_path / "web" / "dist" / "index.html")
    assert warning == expected


def test_npm_exiting_zero_without_an_index_html_is_not_a_built_board(
    tmp_path, monkeypatch,
):
    """npm can exit 0 having produced an incomplete build — that is not a
    board, and it must not be force-included as one."""
    _checkout(tmp_path)
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: "/usr/bin/npm")
    runner = _npm_stub([], write_index=False)

    built = hatch_build.try_build_board(tmp_path, "web/dist", runner=runner)
    assert built is False

    additions, warning = hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "editable",
        builder=lambda root, source: hatch_build.try_build_board(
            root, source, runner=_npm_stub([], write_index=False)))
    assert additions == {} and warning


@pytest.mark.parametrize("exc", [
    FileNotFoundError("npm"),
    PermissionError("npm"),
    subprocess.TimeoutExpired("npm", 1),
])
def test_the_build_attempt_never_raises(tmp_path, monkeypatch, exc):
    """npm vanishing between `which` and `run`, a permissions failure, or a
    hang must all resolve to `False`, never propagate."""
    _checkout(tmp_path)
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: "/usr/bin/npm")

    def runner(argv, cwd, **kwargs):
        raise exc

    built = hatch_build.try_build_board(tmp_path, "web/dist", runner=runner)
    assert built is False


def test_a_standard_build_invokes_npm_only_when_the_board_is_stale(
    tmp_path, monkeypatch,
):
    """The no-npm contract is inverted for a *stale* board only, not a missing
    one.

    A never-built board fails immediately and never shells out — unchanged
    from before this fix, and load-bearing: `tests/test_worktree_forced_includes.py`
    (out of scope, not edited) proves with a real `uv build --wheel` that an
    unprovisioned task worktree must fail closed rather than silently invoke a
    real `npm install`/`npm run build` of its own initiative. Only a
    present-but-outdated bundle is new territory: that path rebuilds via npm
    and re-verifies before shipping.
    """
    _checkout(tmp_path)
    (tmp_path / "web" / "src").mkdir()
    (tmp_path / "web" / "src" / "App.jsx").write_text("before")
    calls = []
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: "/usr/bin/npm")

    def builder(root, source):
        calls.append((root, source))
        dist = Path(root) / source
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("built")
        hatch_build.write_stamp(root, source=source)
        return True

    # missing: never calls the builder, fails immediately.
    with pytest.raises(hatch_build.BoardNotBuiltError):
        hatch_build.plan_board_inclusion(
            tmp_path, "web/dist", "no_human/web_dist", "standard", builder=builder)
    assert calls == []

    # Build the board directly (not through the hook) so the tree has a
    # present, current bundle to go stale from.
    assert builder(tmp_path, "web/dist")
    calls.clear()
    (tmp_path / "web" / "src" / "App.jsx").write_text("after")
    assert hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "standard", builder=builder)[0]
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Real-artifact tier — a real clean clone, real builds                          #
# --------------------------------------------------------------------------- #

def _clean_clone(dest: Path) -> Path:
    """Every TRACKED file, at its working-tree content, and nothing else.

    That is what a fresh `git clone` produces, and the defect is a property of
    exactly that tree: `web/dist` is gitignored, so it is the one thing a clone
    does not carry. Copied from the index rather than `git archive HEAD` so an
    uncommitted change to the packaging is what gets tested — being told that
    HEAD is fine while the working tree is broken is the failure mode this
    file exists to prevent.
    """
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                                  text=True)
    dest.mkdir(parents=True, exist_ok=True)
    for rel in (p for p in out.split("\0") if p):
        src = REPO_ROOT / rel
        if not src.is_file():  # tracked but deleted in the working tree
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    assert not (dest / "web" / "dist").exists(), (
        "the clean clone has a board — then it cannot reproduce the defect")
    return dest


def _uv_or_skip():
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH — cannot run a real build")


@pytest.mark.slow
def test_a_real_wheel_build_of_a_clean_clone_fails_with_the_actionable_message(
    tmp_path,
):
    """The release edge with npm hidden, so an old bundle cannot be shipped."""
    _uv_or_skip()
    clone = _clean_clone(tmp_path / "clone")

    uv = shutil.which("uv")
    # Preserve uv's cache/runtime configuration; only npm must be unavailable
    # for this release-path proof.
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    build = subprocess.run([uv, "build", "--wheel", "-o", str(tmp_path / "d")],
                           cwd=clone, env=env, capture_output=True, text=True, timeout=900)

    assert build.returncode != 0, (
        "a wheel built with no board — the boardless CLI can ship again")
    output = build.stdout + build.stderr
    assert "refusing to build a wheel without the web board" in output, output[-3000:]
    assert "npm run build" in output, output[-3000:]
    assert "web/dist/index.html" in output, output[-3000:]


@pytest.mark.slow
def test_a_real_editable_install_of_a_clean_clone_succeeds(tmp_path):
    """The defect, end to end. This is the command a new contributor runs.

    `uv pip install --no-deps -e` rather than `uv sync`: it is the same
    `build_editable` call on the same tree, without resolving and downloading
    the whole dependency graph. The dependency graph was never what failed.
    """
    _uv_or_skip()
    clone = _clean_clone(tmp_path / "clone")

    venv = tmp_path / "venv"
    mk = subprocess.run(["uv", "venv", str(venv), "--python", "3.12"],
                        capture_output=True, text=True, timeout=600)
    assert mk.returncode == 0, mk.stderr
    python = venv / "bin" / "python"

    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", "-e",
         str(clone)],
        capture_output=True, text=True, timeout=900,
    )

    assert install.returncode == 0, (
        "a clean clone cannot be installed for development:\n"
        + (install.stdout + install.stderr)[-3000:])
    # It really is installed, not merely "did not crash".
    check = subprocess.run([str(python), "-c", "import no_human; print('ok')"],
                           capture_output=True, text=True, timeout=300)
    assert "ok" in check.stdout, check.stderr


@pytest.mark.slow
def test_a_wheel_built_with_a_board_still_carries_it(tmp_path):
    """The other half of the guarantee: when the board is there, it ships.

    A fix that made the build stop failing by dropping the board would pass
    every test above and lose the product.
    """
    _uv_or_skip()
    source_board = REPO_ROOT / "web" / "dist"
    if not (source_board / "index.html").is_file():
        pytest.skip("this checkout has no built board to copy "
                    "(gitignored artifact — run `cd web && npm run build`)")

    clone = _clean_clone(tmp_path / "clone")
    shutil.copytree(source_board, clone / "web" / "dist")
    hatch_build.write_stamp(clone)

    dist = tmp_path / "d"
    build = subprocess.run(["uv", "build", "--wheel", "-o", str(dist)],
                           cwd=clone, capture_output=True, text=True, timeout=900)
    assert build.returncode == 0, (build.stdout + build.stderr)[-3000:]

    (wheel,) = dist.glob("*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert "no_human/web_dist/index.html" in names, (
        "the hook stopped shipping the board — the wheel installs a CLI whose "
        "main command renders nothing")
    assert [n for n in names if n.startswith("no_human/web_dist/assets/")], (
        "index.html shipped without the bundle it references")
