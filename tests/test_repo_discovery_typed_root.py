"""A user-typed ``root`` ("Search another folder") is scanned wherever it
resolves, even outside home — the same path configured as ``extra_roots``
stays home-contained. See ``src/no_human/repo_discovery.py`` module docstring
for the rule this pins.
"""
from __future__ import annotations

from pathlib import Path

from no_human.repo_discovery import DEFAULT_MAX_DEPTH, discover_repos


def _fake_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def test_a_typed_root_outside_home_is_scanned(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    _fake_repo(outside / "a-repo")

    res = discover_repos(home=home, root=str(outside))

    assert "a-repo" in {r["name"] for r in res["repos"]}
    assert res["roots_refused"] == []
    assert res["refusals"] == []
    assert res["roots_scanned"] == [str(outside)]


def test_the_same_path_as_a_configured_extra_root_is_still_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    _fake_repo(outside / "a-repo")

    res = discover_repos(home=home, extra_roots=[str(outside)])

    assert res["repos"] == []
    assert str(outside) in res["roots_refused"]
    assert res["refusals"] == [
        {"path": str(outside), "reason": "outside home directory"}
    ]


def test_a_typed_root_outside_home_keeps_every_other_bound(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # A repo nested deeper than DEFAULT_MAX_DEPTH is never reached, whatever
    # the containment rule is.
    deep = outside
    for i in range(DEFAULT_MAX_DEPTH):
        deep = deep / f"level{i}"
    _fake_repo(deep / "too-deep")

    # A symlink inside the typed root pointing back OUT of the typed root
    # must not be followed — the boundary for a typed root's walk is the
    # typed root itself, not home.
    sibling = tmp_path / "sibling"
    _fake_repo(sibling / "escaped-repo")
    (outside / "link").symlink_to(sibling)

    res = discover_repos(home=home, root=str(outside))

    names = {r["name"] for r in res["repos"]}
    assert "too-deep" not in names
    assert "escaped-repo" not in names


def test_a_missing_typed_root_is_missing_not_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    missing = tmp_path / "nope"

    res = discover_repos(home=home, root=str(missing))

    assert res["repos"] == []
    assert res["roots_refused"] == []
    assert str(missing) in res["roots_missing"]
