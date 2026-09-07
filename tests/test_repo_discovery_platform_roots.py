"""Platform-aware root scanning — Windows developers get found too.

Every test builds a fake HOME on ``tmp_path`` and passes an explicit
``darwin=`` so behaviour is pinned regardless of which OS actually runs the
suite (see ``test_no_platform_reads_inside_the_walk`` below for why that
matters).
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

from no_human import repo_discovery
from no_human.repo_discovery import (
    CONVENTIONAL_ROOTS,
    NON_MAC_ROOT_DEPTH,
    NON_MAC_ROOTS,
    discover_repos,
    ends_with_sep,
    expand_home,
    home_skip,
    normalize_typed_path,
    platform_roots,
)


def _fake_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def _by_name(result) -> dict[str, dict]:
    return {r["name"]: r for r in result["repos"]}


def _win_home(tmp_path: Path) -> Path:
    """A fake ``%USERPROFILE%`` with repos at Windows' actual clone spots:
    GitHub Desktop's ``Documents\\GitHub``, a repo dropped on the Desktop, and
    Visual Studio's ``source\\repos`` default. Also a repo under Downloads,
    which must NEVER be offered as a root, on any platform."""
    home = tmp_path / "home"
    _fake_repo(home / "Documents" / "GitHub" / "web-app")
    _fake_repo(home / "Desktop" / "scratch")
    _fake_repo(home / "source" / "repos" / "vs-proj")
    _fake_repo(home / "Downloads" / "unpacked")
    return home


# --------------------------------------------------------------------------- #
# discover_repos(darwin=...)                                                   #
# --------------------------------------------------------------------------- #

def test_non_mac_scan_finds_windows_clone_locations(tmp_path):
    home = _win_home(tmp_path)
    res = discover_repos(home=home, darwin=False)
    names = set(_by_name(res))
    assert "web-app" in names
    assert "scratch" in names
    assert "vs-proj" in names
    assert "unpacked" not in names, "Downloads must never be a root"


def test_macos_scan_skips_desktop_and_documents_entirely(tmp_path):
    home = _win_home(tmp_path)
    res = discover_repos(home=home, darwin=True)
    names = set(_by_name(res))
    # source/repos/vs-proj is still found: "source" is a conventional root on
    # every platform.
    assert names == {"vs-proj"}
    assert not any(
        str(home / "Documents") in s or str(home / "Desktop") in s
        for s in res["roots_scanned"]
    )


def test_darwin_none_follows_sys_platform(tmp_path, monkeypatch):
    home = _win_home(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    res_darwin = discover_repos(home=home, darwin=None)
    monkeypatch.setattr(sys, "platform", "linux")
    res_linux = discover_repos(home=home, darwin=None)
    assert set(_by_name(res_darwin)) == {"vs-proj"}
    assert set(_by_name(res_linux)) == {"web-app", "scratch", "vs-proj"}


def test_home_depth_one_walk_runs_before_documents(tmp_path):
    """A wide ~/Documents (lots of manifest-bearing folders that are not
    themselves repos) must never push a repo cloned straight under ~ out of
    the result ceiling — pin the walk order, not just its existence."""
    home = tmp_path / "home"
    _fake_repo(home / "myrepo")
    for i in range(1100):
        (home / "Documents" / f"folder-{i}").mkdir(parents=True)
    res = discover_repos(home=home, darwin=False, max_results=50)
    assert "myrepo" in _by_name(res)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def test_platform_roots_and_home_skip():
    assert platform_roots(True) == CONVENTIONAL_ROOTS
    assert platform_roots(False) == CONVENTIONAL_ROOTS + NON_MAC_ROOTS
    assert home_skip(True) == frozenset(repo_discovery.PROTECTED_HOME_DIRS)
    assert home_skip(False) == frozenset({"Library"})
    assert NON_MAC_ROOT_DEPTH == 2


def test_normalize_typed_path():
    assert normalize_typed_path("D:", windows=True) == "D:\\"
    assert normalize_typed_path("D:\\repos", windows=True) == "D:\\repos"
    assert normalize_typed_path("D:", windows=False) == "D:"
    assert normalize_typed_path("", windows=True) == ""
    assert normalize_typed_path("  D:  ", windows=True) == "D:\\"


def test_expand_home(tmp_path):
    home = tmp_path / "home"
    assert expand_home("~", home, windows=True) == home
    assert expand_home("~/code", home, windows=True) == home / "code"
    assert expand_home("~\\code", home, windows=True) == home / "code"
    # Off Windows, a leading "~\" is just a literal filename, not an expansion.
    assert expand_home("~\\code", home, windows=False) == Path("~\\code")
    assert expand_home("D:", home, windows=True) == Path("D:\\")


def test_ends_with_sep():
    assert ends_with_sep("C:\\repos\\", windows=True) is True
    assert ends_with_sep("C:\\repos\\", windows=False) is False
    assert ends_with_sep("/home/x/", windows=True) is True
    assert ends_with_sep("/home/x/", windows=False) is True
    assert ends_with_sep("no-sep", windows=True) is False


def test_typed_root_uses_expand_home(tmp_path):
    home = tmp_path / "home"
    _fake_repo(home / "elsewhere" / "b")
    res = discover_repos(home=home, root="~/elsewhere", darwin=True)
    assert [r["name"] for r in res["repos"]] == ["b"]


# --------------------------------------------------------------------------- #
# Guards                                                                       #
# --------------------------------------------------------------------------- #

def test_no_platform_reads_inside_the_walk():
    """``os.name``/``sys.platform`` may only be read as a default-argument
    expression (module import time / function-signature evaluation) — never
    inside the walk itself, or behaviour becomes host-dependent and untestable
    from the "other" platform."""
    src = inspect.getsource(repo_discovery)
    walk_src = inspect.getsource(repo_discovery._walk)
    discover_src = inspect.getsource(repo_discovery.discover_repos)
    for body in (walk_src,):
        assert "os.name" not in body
        assert "sys.platform" not in body
    # discover_repos itself may only touch sys.platform on the documented
    # "darwin is None" fallback line — the docstring mentions it too, so only
    # count lines that are actual code (no leading "``"/prose markers).
    platform_code_lines = [
        line for line in discover_src.splitlines()
        if "sys.platform" in line and "darwin is None" in line
    ]
    assert len(platform_code_lines) == 1
    all_mentions = [line for line in discover_src.splitlines() if "sys.platform" in line]
    assert len(all_mentions) == 2, "one code line + one docstring mention, no more"
    assert src  # module parses/imports fine as a sanity check


def test_module_docstring_no_longer_claims_universal_skip():
    doc = repo_discovery.__doc__ or ""
    assert "macOS-only" in doc or "macOS only" in doc
    assert "NON_MAC_ROOTS" in doc
