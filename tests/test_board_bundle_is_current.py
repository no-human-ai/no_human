"""Content-freshness tests for the board Hatchling force-includes."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from no_human.testing.runner import _ensure_forced_build_artifacts


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_hatch_build():
    spec = importlib.util.spec_from_file_location(
        "board_current_hatch_build", REPO_ROOT / "hatch_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hatch_build = _load_hatch_build()


def _board_tree(root: Path) -> Path:
    web = root / "web"
    (web / "src").mkdir(parents=True)
    (web / "dist").mkdir()
    (web / "index.html").write_text("<div id='root'></div>")
    (web / "package.json").write_text("{\"scripts\": {\"build\": \"vite build\"}}")
    (web / "src" / "App.jsx").write_text("export default function App() { return 'old'; }\n")
    (web / "dist" / "index.html").write_text("<div id='root'></div>")
    hatch_build.write_stamp(root)
    return web


def _successful_rebuild(root: Path, source: str) -> bool:
    hatch_build.write_stamp(root, source=source)
    return True


def test_reverting_a_source_without_rebuilding_is_detected(tmp_path):
    web = _board_tree(tmp_path)
    app = web / "src" / "App.jsx"
    original = app.read_text(encoding="utf-8")

    assert hatch_build.board_state(tmp_path)[0] == "current"
    app.write_text(original + "// rebuilt source\n")
    state, reason = hatch_build.board_state(tmp_path)
    assert state == "stale" and "digest" in reason

    # `try_build_board` writes this immediately after its successful npm build.
    hatch_build.write_stamp(tmp_path)
    assert hatch_build.board_state(tmp_path)[0] == "current"
    app.write_text(original)
    assert hatch_build.board_state(tmp_path)[0] == "stale"
    app.write_text(original + "// rebuilt source\n")
    assert hatch_build.board_state(tmp_path)[0] == "current"


def test_identical_mtimes_still_detect_a_stale_bundle(tmp_path):
    web = _board_tree(tmp_path)
    (web / "src" / "App.jsx").write_text("changed")
    for path in web.rglob("*"):
        if path.is_file():
            os.utime(path, (1, 1))
    assert hatch_build.board_state(tmp_path)[0] == "stale"


def test_the_verdict_ignores_mtimes(tmp_path):
    web = _board_tree(tmp_path)
    before = hatch_build.board_state(tmp_path)
    for path in web.rglob("*"):
        if path.is_file():
            os.utime(path, (2_000_000_000, 2_000_000_000))
    assert hatch_build.board_state(tmp_path) == before


def test_bundle_older_than_every_source_is_current_when_content_matches(tmp_path):
    web = _board_tree(tmp_path)
    os.utime(web / "dist" / "index.html", (1, 1))
    for path in (web / "index.html", web / "package.json", web / "src" / "App.jsx"):
        os.utime(path, (2_000_000_000, 2_000_000_000))
    assert hatch_build.board_state(tmp_path)[0] == "current"


def test_a_stale_standard_build_without_npm_refuses_the_existing_bundle(tmp_path, monkeypatch):
    web = _board_tree(tmp_path)
    (web / "src" / "App.jsx").write_text("changed")
    calls = []
    monkeypatch.setattr(hatch_build.shutil, "which", lambda _: None)

    def forbidden(root, source):
        calls.append((root, source))
        return True

    with pytest.raises(hatch_build.BoardStaleError) as excinfo:
        hatch_build.plan_board_inclusion(
            tmp_path, "web/dist", "no_human/web_dist", "standard", builder=forbidden)
    assert calls == []
    message = str(excinfo.value)
    assert "stale" in message and "npm" in message and "refused, not shipped" in message


def test_a_stale_standard_build_rebuilds_and_keeps_the_same_include(tmp_path, monkeypatch):
    web = _board_tree(tmp_path)
    (web / "src" / "App.jsx").write_text("changed")
    monkeypatch.setattr(hatch_build.shutil, "which", lambda _: "/usr/bin/npm")
    additions, warning = hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "standard", builder=_successful_rebuild)
    assert additions == {"web/dist": "no_human/web_dist"}
    assert warning is None
    assert hatch_build.board_state(tmp_path)[0] == "current"


def test_a_current_bundle_is_not_rebuilt(tmp_path):
    _board_tree(tmp_path)

    def forbidden(root, source):
        raise AssertionError("current board was rebuilt")

    assert hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "standard", builder=forbidden) == (
            {"web/dist": "no_human/web_dist"}, None)


@pytest.mark.parametrize("stamp", [None, "", "not json", "{}",
    '{"version": 0, "algorithm": "sha256", "digest": "' + "0" * 64 + '"}',
    '{"version": 1, "algorithm": "sha1", "digest": "' + "0" * 64 + '"}',
    '{"version": 1, "algorithm": "sha256", "digest": "short"}',
    '{"version": 1, "algorithm": "sha256", "digest": null}',
])
def test_bad_or_missing_stamp_is_stale(tmp_path, stamp):
    web = _board_tree(tmp_path)
    path = web / ".board-stamp.json"
    if stamp is None:
        path.unlink()
    else:
        path.write_text(stamp)
    assert hatch_build.board_state(tmp_path)[0] == "stale"


def test_a_tree_with_no_web_sources_falls_back_to_presence_only(tmp_path, monkeypatch):
    index = tmp_path / "web" / "dist" / "index.html"
    index.parent.mkdir(parents=True)
    index.write_text("built from sdist")
    monkeypatch.setattr(hatch_build.shutil, "which", lambda _: (_ for _ in ()).throw(AssertionError()))
    assert hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "standard") == (
            {"web/dist": "no_human/web_dist"}, None)


def test_a_stale_editable_install_warns_and_never_raises(tmp_path):
    web = _board_tree(tmp_path)
    (web / "src" / "App.jsx").write_text("changed")
    additions, warning = hatch_build.plan_board_inclusion(
        tmp_path, "web/dist", "no_human/web_dist", "editable")
    assert additions == {"web/dist": "no_human/web_dist"}
    assert warning and "stale" in warning


def test_the_documented_build_command_leaves_a_current_board(tmp_path):
    """`_build_it()` is the exact text every warning/error message tells a
    developer to run by hand. Before this fix it was bare `npm install &&
    npm run build`, which never writes `web/.board-stamp.json` — only
    `try_build_board` (run automatically by the Hatchling hook) or the
    undocumented `--stamp` flag did. A developer who followed the docs to
    the letter and produced a genuinely correct bundle still read back
    "stale" forever. This proves the documented command, executed verbatim
    with nothing else run, now leaves the board `current`."""
    web = _board_tree(tmp_path)
    (web / "src" / "App.jsx").write_text("changed")
    assert hatch_build.board_state(tmp_path)[0] == "stale"
    shutil.copy2(REPO_ROOT / "hatch_build.py", tmp_path / "hatch_build.py")

    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "npm").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "run" ]; then\n'
        "  mkdir -p dist\n"
        '  echo "<div id=root></div>" > dist/index.html\n'
        "fi\n"
        "exit 0\n"
    )
    (bin_dir / "npm").chmod(0o755)
    os.symlink(sys.executable, bin_dir / "python3")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(
        hatch_build._build_it(), shell=True, cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert hatch_build.board_state(tmp_path) == (
        "current", f"source digest {hatch_build.source_digest(tmp_path)}")


def test_the_hook_is_the_gate(tmp_path, monkeypatch):
    web = _board_tree(tmp_path)
    (web / "src" / "App.jsx").write_text("changed")
    monkeypatch.setattr(hatch_build.shutil, "which", lambda _: None)
    hook = hatch_build.BoardBuildHook.__new__(hatch_build.BoardBuildHook)
    hook.root = str(tmp_path)
    hook.config = {"source": "web/dist", "target": "no_human/web_dist"}
    hook.target_name = "wheel"
    hook.app = type("App", (), {"display_warning": lambda self, warning: None})()
    with pytest.raises(hatch_build.BoardStaleError):
        hook.initialize("standard", {"force_include": {}})


def test_the_stamp_is_not_inside_the_shipped_board(tmp_path):
    web = _board_tree(tmp_path)
    assert not (web / "dist" / ".board-stamp.json").exists()
    assert hatch_build.stamp_path(tmp_path) == web / ".board-stamp.json"


def test_worktree_provisioning_copies_a_missing_sibling_stamp(tmp_path):
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    for root in (source, worktree):
        (root / "web" / "dist").mkdir(parents=True)
        (root / "web" / "dist" / "index.html").write_text("board")
        (root / "pyproject.toml").write_text(
            "[tool.hatch.build.targets.wheel.hooks.custom]\nsource = 'web/dist'\n")
    (source / "web" / ".board-stamp.json").write_text('{"digest": "source"}\n')

    _ensure_forced_build_artifacts(worktree, source)

    assert (worktree / "web" / ".board-stamp.json").read_text(encoding="utf-8") == (
        source / "web" / ".board-stamp.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("result", [False, True])
def test_a_standard_stale_build_never_includes_an_unverified_bundle(
    tmp_path, monkeypatch, result,
):
    web = _board_tree(tmp_path)
    (web / "src" / "App.jsx").write_text("changed")
    monkeypatch.setattr(hatch_build.shutil, "which", lambda _: "/usr/bin/npm")
    with pytest.raises(hatch_build.BoardStaleError):
        hatch_build.plan_board_inclusion(
            tmp_path, "web/dist", "no_human/web_dist", "standard",
            builder=lambda root, source: result)


@pytest.mark.slow
def test_a_real_fresh_clone_detects_a_staled_bundle(tmp_path):
    """A clone has no useful source mtimes, so this drives content-only CLI."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    board = REPO_ROOT / "web" / "dist"
    clone = tmp_path / "clone"
    copied = subprocess.run(["git", "clone", str(REPO_ROOT), str(clone)],
                            capture_output=True, text=True, timeout=120)
    assert copied.returncode == 0, copied.stderr
    # The test must exercise the working-tree implementation before its commit.
    shutil.copy2(REPO_ROOT / "hatch_build.py", clone / "hatch_build.py")
    if (board / "index.html").is_file():
        shutil.copytree(board, clone / "web" / "dist")
    else:
        # The checker judges bundle presence by index.html; this represents the
        # pre-existing local bundle without requiring this checkout to build it.
        (clone / "web" / "dist").mkdir()
        (clone / "web" / "dist" / "index.html").write_text("old board")
    stamped = subprocess.run([sys.executable, "hatch_build.py", "--stamp", "."],
                             cwd=clone, capture_output=True, text=True, timeout=30)
    assert stamped.returncode == 0, stamped.stderr
    for path in (clone / "web").rglob("*"):
        if path.is_file():
            os.utime(path, (1, 1))
    app = clone / "web" / "src" / "App.jsx"
    app.write_text(app.read_text(encoding="utf-8") + "\n// stale after clone\n")
    os.utime(app, (1, 1))
    checked = subprocess.run([sys.executable, "hatch_build.py", "--check", "."],
                             cwd=clone, capture_output=True, text=True, timeout=30)
    assert checked.returncode == 1
    assert "stale" in checked.stderr and "digest" in checked.stderr
