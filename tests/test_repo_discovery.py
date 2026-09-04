"""Repository auto-discovery — scan the conventional clone roots so onboarding
and the composer can offer a list instead of demanding a typed path.

Every test builds a fake HOME on tmp_path and points the discovery at it, so
nothing here reads the operator's real machine.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from no_human import repo_discovery
from no_human.repo_discovery import (
    CONVENTIONAL_ROOTS,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_RESULTS,
    discover_repos,
)

def _assert_unreadable(p: Path) -> None:
    """Prove the ``chmod`` actually bites before trusting the test that follows.

    A permission test that silently stops reproducing anything is worse than no
    test: it reports green forever. Under root the bits mean nothing, so the
    precondition is simply not claimed there — the test still runs and its
    assertions still hold, it just proves less on that one uid. This is checked
    inline rather than with a skip marker: skipping is what the tamper guard
    exists to notice, and a test that runs everywhere owes it no exception.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return
    with pytest.raises(PermissionError):
        (p / ".git").exists()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def _real_repo(path: Path, *, dirty: bool = False, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    (path / "README.md").write_text("hello\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    if dirty:
        (path / "README.md").write_text("edited\n")
    return path


def _fake_repo(path: Path) -> Path:
    """A .git directory with a HEAD but no objects — enough for the cheap scan,
    and what most of these tests need (they assert on shape, not git state).
    HEAD is real so the dirty probe still runs, and the timing test therefore
    still pays the subprocess cost it is measuring."""
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def _by_name(result) -> dict[str, dict]:
    return {r["name"]: r for r in result["repos"]}


@pytest.fixture
def fake_home(tmp_path):
    home = tmp_path / "home"
    (home / "git").mkdir(parents=True)
    return home


# --------------------------------------------------------------------------- #
# Home is a depth-1 root; rows carry mtime; root= scans one folder             #
# --------------------------------------------------------------------------- #

def test_repos_directly_under_home_are_found(fake_home):
    _fake_repo(fake_home / "ai-realtime")   # HEAD is the evidence _is_repo needs
    out = discover_repos(home=fake_home)
    assert any(r["path"].endswith("ai-realtime") for r in out["repos"]) and out["home_direct"] == 1
def test_rows_carry_mtime_and_sort_newest_first(fake_home):
    old, new = fake_home / "git" / "old", fake_home / "git" / "new"
    for p in (old, new): (p / ".git").mkdir(parents=True); (p / ".git" / "HEAD").write_text("ref")
    os.utime(old / ".git" / "HEAD", (1_000, 1_000)); os.utime(new / ".git" / "HEAD", (2_000, 2_000))
    names = [r["name"] for r in discover_repos(home=fake_home)["repos"]]
    assert names.index("new") < names.index("old")
def test_root_param_scans_only_that_root(fake_home):
    _fake_repo(fake_home / "git" / "a"); _fake_repo(fake_home / "elsewhere" / "b")
    out = discover_repos(home=fake_home, root=str(fake_home / "elsewhere"))
    assert [r["name"] for r in out["repos"]] == ["b"] and out["roots"] == [str(fake_home / "elsewhere")]
def test_a_missing_typed_root_is_not_refused(fake_home, tmp_path):
    """A user-typed ``root`` outside home is scanned, not refused (see
    ``tests/test_repo_discovery_typed_root.py`` for that coverage) - a typed
    root that simply does not exist is ``roots_missing``, never
    ``roots_refused``, so the UI keeps its honest "no repositories" wording
    for that case."""
    out = discover_repos(home=fake_home, root=str(tmp_path / "x"))
    assert out["repos"] == [] and out["refused"] == []
    assert str(tmp_path / "x") in out["roots_missing"]
def test_home_scan_never_enters_protected_dirs(fake_home, monkeypatch):
    touched = []
    real = Path.iterdir
    def spy(self): touched.append(str(self)); return real(self)
    monkeypatch.setattr(Path, "iterdir", spy)
    (fake_home / "Documents" / "secret" / ".git").mkdir(parents=True)
    discover_repos(home=fake_home)
    assert not any(t.endswith("/Documents") or "/Documents/" in t for t in touched)


# --------------------------------------------------------------------------- #
# Roots                                                                        #
# --------------------------------------------------------------------------- #

def test_conventional_roots_cover_the_standard_clone_locations():
    assert CONVENTIONAL_ROOTS == (
        "Projects", "Code", "Development", "Dev", "repos", "git", "workspace", "src",
    )


def test_discovers_across_every_conventional_root(tmp_path):
    for i, root in enumerate(CONVENTIONAL_ROOTS):
        _fake_repo(tmp_path / root / f"proj-{i}")
    res = discover_repos(home=tmp_path)
    names = set(_by_name(res))
    assert names == {f"proj-{i}" for i in range(len(CONVENTIONAL_ROOTS))}
    # The roots that actually existed are reported, so the UI can say where it looked.
    assert set(res["roots_scanned"]) == {str(tmp_path / r) for r in CONVENTIONAL_ROOTS}
    assert res["roots_missing"] == []


def test_conventional_roots_match_case_variants_on_case_sensitive_filesystems(tmp_path):
    """Linux keeps ~/code and ~/Code apart; APFS/NTFS fold them. Measured on a
    real Ubuntu 24.04 desktop (2026-08-18): a user's ~/code/calc was invisible
    to the scan because the list spells the root "Code". Every on-disk case
    variant of a conventional name is a root; the canonical spelling is still
    reported missing when no variant exists."""
    _fake_repo(tmp_path / "code" / "calc")          # lowercase, the Linux convention
    res = discover_repos(home=tmp_path)
    assert "calc" in _by_name(res)
    assert str(tmp_path / "code") in res["roots_scanned"]
    # No double scan when the filesystem folds case (macOS): one entry per real dir.
    assert len(set(res["roots_scanned"])) == len(res["roots_scanned"])


def test_missing_roots_are_reported_not_errors(tmp_path):
    (tmp_path / "git").mkdir()
    res = discover_repos(home=tmp_path)
    assert res["roots_scanned"] == [str(tmp_path / "git")]
    assert len(res["roots_missing"]) == len(CONVENTIONAL_ROOTS) - 1


def test_operator_configured_extra_roots_are_scanned(tmp_path):
    _fake_repo(tmp_path / "work" / "acme-api")
    res = discover_repos(home=tmp_path, extra_roots=["~/work"])
    assert "acme-api" in _by_name(res)


def test_extra_roots_outside_home_are_refused(tmp_path):
    outside = tmp_path.parent / "outside-home"
    _fake_repo(outside / "secret-repo")
    home = tmp_path / "home"
    home.mkdir()
    res = discover_repos(home=home, extra_roots=[str(outside)])
    assert res["repos"] == []
    assert str(outside) in res["roots_refused"]


def test_an_unresolvable_extra_root_is_refused_rather_than_crashing_the_scan(tmp_path):
    """A NUL byte in a configured root aborted the ENTIRE scan with a traceback.

    ``_resolved`` guarded ``p.resolve()`` against ``OSError`` only, but an
    embedded NUL makes the underlying ``lstat`` raise ``ValueError`` - and
    ``extra_roots`` is operator config, so this reaches ``resolve()`` before any
    containment check can reject it. ``discover_repos`` documents that it "does
    not raise on anything it merely cannot read", and every OTHER bad root
    (missing, outside home, symlinked away) is reported in a list.

    The behaviour tested is the one the picker sees: the good root still
    produces its repo, and the bad one is NAMED in ``roots_refused``.
    """
    _fake_repo(tmp_path / "work" / "acme-api")

    res = discover_repos(home=tmp_path, extra_roots=["~/work", "bad\0root"])

    assert "acme-api" in _by_name(res), "one bad root must not cost the good ones"
    assert any("\0" in r for r in res["roots_refused"]), res["roots_refused"]


def test_a_symlink_escaping_home_is_not_followed(tmp_path):
    home = tmp_path / "home"
    (home / "git").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    _fake_repo(outside / "secret-repo")
    (home / "git" / "link").symlink_to(outside)
    res = discover_repos(home=home)
    assert "secret-repo" not in _by_name(res)


def test_a_conventional_root_symlinked_out_of_home_is_refused(tmp_path):
    """``~/Code -> /Volumes/BigDisk/code`` is an ordinary developer setup, and
    it used to walk straight out of home: the eight conventional roots were the
    only paths built without the resolve-and-contain check every other entry
    point gets. The module docstring promises the walk never leaves ``home``,
    so this is the promise, tested.
    """
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    _fake_repo(outside / "secretrepo")
    (home / "Dev").symlink_to(outside)

    res = discover_repos(home=home)

    assert _by_name(res) == {}, "a repo outside home must never reach the picker"
    assert str(home / "Dev") not in res["roots_scanned"]
    assert any("Dev" in r for r in res["roots_refused"]), res["roots_refused"]


def test_a_conventional_root_symlinked_inside_home_is_still_scanned(tmp_path):
    """Containment, not "no symlinks" - a link that stays inside home is fine."""
    home = tmp_path / "home"
    real = home / "actual-clones"
    _fake_repo(real / "svc")
    (home / "Code").symlink_to(real)
    got = _by_name(discover_repos(home=home))
    assert "svc" in got
    assert discover_repos(home=home)["roots_refused"] == []


# --------------------------------------------------------------------------- #
# What comes back per repo                                                     #
# --------------------------------------------------------------------------- #

def test_reports_path_name_git_branch_and_dirty(tmp_path):
    root = tmp_path / "git"
    clean = _real_repo(root / "clean-svc", branch="main")
    dirty = _real_repo(root / "dirty-svc", dirty=True, branch="feat/x")

    res = discover_repos(home=tmp_path)
    got = _by_name(res)

    assert got["clean-svc"]["path"] == str(clean)
    assert got["clean-svc"]["is_git"] is True
    assert got["clean-svc"]["branch"] == "main"
    assert got["clean-svc"]["dirty"] is False

    assert got["dirty-svc"]["path"] == str(dirty)
    assert got["dirty-svc"]["branch"] == "feat/x"
    assert got["dirty-svc"]["dirty"] is True


def test_untracked_file_counts_as_dirty(tmp_path):
    repo = _real_repo(tmp_path / "git" / "svc")
    (repo / "scratch.txt").write_text("wip\n")
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty"] is True
    assert got["dirty_scan"] == "complete"


def test_a_clean_repo_reports_a_complete_scan(tmp_path):
    _real_repo(tmp_path / "git" / "svc")
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty"] is False
    assert got["dirty_scan"] == "complete"


def test_a_tracked_edit_is_reported_without_waiting_on_the_untracked_scan(tmp_path, monkeypatch):
    """The untracked scan is the expensive half. A repo whose TRACKED files are
    already modified is dirty regardless, so that scan must not run at all."""
    repo = _real_repo(tmp_path / "git" / "svc", dirty=True)
    calls: list[tuple[str, ...]] = []
    real = repo_discovery._git_status

    def spy(path, untracked, timeout):
        calls.append(untracked)
        return real(path, untracked, timeout)

    monkeypatch.setattr(repo_discovery, "_git_status", spy)
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty"] is True
    assert calls == ["no"], "the untracked scan must be skipped once tracked edits are found"


def test_a_slow_untracked_scan_degrades_to_a_partial_answer_not_a_hang(tmp_path, monkeypatch):
    """A huge working tree can take seconds to scan for untracked files. The
    picker must still come back: report what the cheap probe proved and mark
    the answer partial rather than blocking the list on it."""
    _real_repo(tmp_path / "git" / "svc")
    real = repo_discovery._git_status

    def slow_untracked(path, untracked, timeout):
        if untracked == "no":
            return real(path, untracked, timeout)
        return None  # what a timeout looks like to the caller

    monkeypatch.setattr(repo_discovery, "_git_status", slow_untracked)
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty"] is False
    assert got["dirty_scan"] == "partial"


def test_the_untracked_scan_has_a_tighter_timeout_than_the_tracked_one(tmp_path):
    assert repo_discovery.UNTRACKED_TIMEOUT_S < repo_discovery.GIT_TIMEOUT_S


def test_detached_head_reports_the_short_sha_not_a_branch(tmp_path):
    repo = _real_repo(tmp_path / "git" / "svc")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    _git(repo, "checkout", "-q", "--detach", sha)
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["branch"] == sha[:8]
    assert got["detached"] is True


def test_non_git_project_directory_is_offered_with_is_git_false(tmp_path):
    proj = tmp_path / "Projects" / "sketch"
    proj.mkdir(parents=True)
    (proj / "package.json").write_text("{}")
    got = _by_name(discover_repos(home=tmp_path))["sketch"]
    assert got["is_git"] is False
    assert got["branch"] == ""
    assert got["dirty"] is False


def test_a_plain_directory_with_no_repo_and_no_manifest_is_not_offered(tmp_path):
    (tmp_path / "git" / "just-notes").mkdir(parents=True)
    assert discover_repos(home=tmp_path)["repos"] == []


def test_ecosystem_is_carried_through_for_the_existing_repo_list_ui(tmp_path):
    node = _fake_repo(tmp_path / "git" / "web-app")
    (node / "package.json").write_text("{}")
    py = _fake_repo(tmp_path / "git" / "svc")
    (py / "pyproject.toml").write_text("[project]\n")
    got = _by_name(discover_repos(home=tmp_path))
    assert got["web-app"]["ecosystem"] == "node"
    assert got["svc"]["ecosystem"] == "python"


# --------------------------------------------------------------------------- #
# Bounds: depth, exclusions, caps                                              #
# --------------------------------------------------------------------------- #

def test_default_depth_is_three_and_reaches_host_owner_repo_layouts(tmp_path):
    # The layout this machine actually uses: ~/git/<host>/<branchdir>/<repo>.
    assert DEFAULT_MAX_DEPTH == 3
    _fake_repo(tmp_path / "git" / "example-host" / "master" / "deep-svc")
    assert "deep-svc" in _by_name(discover_repos(home=tmp_path))


def test_repos_below_the_depth_limit_are_not_returned(tmp_path):
    _fake_repo(tmp_path / "git" / "a" / "b" / "c" / "too-deep")
    assert discover_repos(home=tmp_path)["repos"] == []


def test_a_repo_is_a_leaf_nested_repos_are_not_descended_into(tmp_path):
    outer = _fake_repo(tmp_path / "git" / "outer")
    _fake_repo(outer / "inner")
    got = _by_name(discover_repos(home=tmp_path))
    assert set(got) == {"outer"}


@pytest.mark.parametrize("junk", ["node_modules", ".venv", "vendor", ".hidden", "venv"])
def test_excluded_directories_are_never_descended_into(tmp_path, junk):
    _fake_repo(tmp_path / "git" / junk / "buried")
    assert discover_repos(home=tmp_path)["repos"] == []


def test_results_are_capped_and_the_cap_is_announced_not_silent(tmp_path):
    root = tmp_path / "git"
    for i in range(12):
        _fake_repo(root / f"r{i:02d}")
    res = discover_repos(home=tmp_path, max_results=5)
    assert len(res["repos"]) == 5
    assert res["capped"] is True
    assert res["limit"] == 5
    assert res["note"], "a capped result must carry a human-readable note"
    assert "5" in res["note"]


def test_an_uncapped_result_says_so(tmp_path):
    _fake_repo(tmp_path / "git" / "only")
    res = discover_repos(home=tmp_path)
    assert res["capped"] is False
    assert res["note"] == ""
    assert res["limit"] == DEFAULT_MAX_RESULTS


def test_results_are_sorted_by_name_when_mtime_ties(tmp_path):
    # The primary sort key is mtime desc; name.lower() is the tiebreaker. Pin
    # every .git/HEAD to the same mtime so this exercises the tiebreaker, which
    # keeps the list stable when recency does not separate the repos.
    for n in ("zeta", "alpha", "Mid"):
        _fake_repo(tmp_path / "git" / n)
        os.utime(tmp_path / "git" / n / ".git" / "HEAD", (5_000, 5_000))
    names = [r["name"] for r in discover_repos(home=tmp_path)["repos"]]
    assert names == ["alpha", "Mid", "zeta"]


def test_elapsed_ms_is_reported_so_a_slow_scan_is_visible(tmp_path):
    _fake_repo(tmp_path / "git" / "only")
    res = discover_repos(home=tmp_path)
    assert isinstance(res["elapsed_ms"], int)
    assert res["elapsed_ms"] >= 0


def test_a_wide_tree_stays_fast_enough_for_a_ui(tmp_path):
    """60 repos across three roots must scan well inside a UI budget."""
    for root in ("git", "Projects", "Code"):
        for i in range(20):
            _fake_repo(tmp_path / root / f"r{i:02d}")
    t0 = time.perf_counter()
    res = discover_repos(home=tmp_path)
    wall = time.perf_counter() - t0
    assert len(res["repos"]) == 60
    assert wall < 3.0, f"discovery took {wall:.2f}s for 60 repos"


def test_the_untracked_pass_stops_at_a_shared_budget(tmp_path, monkeypatch):
    """Twelve slow repositories must not cost twelve timeouts in a row.

    Per-probe timeouts alone let total wall time grow with the number of large
    checkouts, which is exactly the machine where discovery matters most. The
    untracked pass gets ONE budget for the whole scan; repos it does not reach
    come back partial instead of extending the wait.
    """
    for i in range(12):
        _real_repo(tmp_path / "git" / f"r{i:02d}")

    seen: list[str] = []

    def slow(path, untracked, timeout):
        seen.append(untracked)
        if untracked == "no":
            return ""          # tracked files clean
        time.sleep(0.2)
        return None            # the expensive pass never answers

    monkeypatch.setattr(repo_discovery, "_git_status", slow)
    monkeypatch.setattr(repo_discovery, "DIRTY_BUDGET_S", 0.05)

    res = discover_repos(home=tmp_path)
    assert seen.count("no") == 12, "every repo still gets the cheap probe"
    assert seen.count("normal") < 12, "the budget must cut the expensive pass short"
    assert all(r["dirty_scan"] == "partial" for r in res["repos"])


def test_the_budget_does_not_bite_when_the_scan_is_fast(tmp_path):
    for i in range(4):
        _real_repo(tmp_path / "git" / f"r{i}")
    res = discover_repos(home=tmp_path)
    assert [r["dirty_scan"] for r in res["repos"]] == ["complete"] * 4


def test_no_row_leaks_an_internal_scan_state(tmp_path):
    _real_repo(tmp_path / "git" / "svc")
    (tmp_path / "Projects" / "sketch").mkdir(parents=True)
    (tmp_path / "Projects" / "sketch" / "go.mod").write_text("module x\n")
    for r in discover_repos(home=tmp_path)["repos"]:
        assert r["dirty_scan"] in {"complete", "partial", "unavailable", "not-a-repo"}


def test_one_budget_covers_both_passes_not_just_the_expensive_one(tmp_path, monkeypatch):
    """The cheap probe is not free either: on a very large checkout `git status
    --untracked-files=no` still refreshes the index and was measured at 1.6s on
    this machine. Budgeting only the untracked pass leaves total wall time
    unbounded, so ONE deadline covers both.
    """
    for i in range(4):
        _real_repo(tmp_path / "git" / f"r{i}")
    calls: list[str] = []

    def spy(path, untracked, timeout):
        calls.append(untracked)
        return ""

    monkeypatch.setattr(repo_discovery, "_git_status", spy)
    monkeypatch.setattr(repo_discovery, "DIRTY_BUDGET_S", -1.0)  # already spent

    res = discover_repos(home=tmp_path)
    assert calls == [], "no git probe may run once the budget is gone"
    assert {r["dirty_scan"] for r in res["repos"]} == {"unavailable"}


def test_an_unreached_repo_reports_unavailable_rather_than_clean(tmp_path, monkeypatch):
    _real_repo(tmp_path / "git" / "svc")
    monkeypatch.setattr(repo_discovery, "DIRTY_BUDGET_S", -1.0)
    got = _by_name(discover_repos(home=tmp_path))["svc"]
    assert got["dirty_scan"] == "unavailable"
    assert got["dirty"] is False   # false, but the row says the check never ran
    # The cheap metadata still comes back - it costs no subprocess at all.
    assert got["branch"] == "main"
    assert got["is_git"] is True


def test_the_budget_bounds_the_whole_scan_not_each_repo(tmp_path, monkeypatch):
    for i in range(16):
        _real_repo(tmp_path / "git" / f"r{i:02d}")

    def slow(path, untracked, timeout):
        time.sleep(0.3)
        return ""

    monkeypatch.setattr(repo_discovery, "_git_status", slow)
    monkeypatch.setattr(repo_discovery, "DIRTY_BUDGET_S", 0.3)
    monkeypatch.setattr(repo_discovery, "GIT_TIMEOUT_S", 1.0)
    t0 = time.perf_counter()
    res = discover_repos(home=tmp_path)
    wall = time.perf_counter() - t0
    assert len(res["repos"]) == 16
    # 16 serial 0.3s probes would be 4.8s; the budget plus one in-flight probe
    # is the ceiling regardless of how many repos there are.
    assert wall < 2.0, f"scan took {wall:.2f}s"


# --------------------------------------------------------------------------- #
# A hostile filesystem: unreadable directories, junk .git, aliases, bare repos #
# --------------------------------------------------------------------------- #

def test_one_unreadable_directory_does_not_take_the_whole_scan_down(tmp_path):
    """One ``chmod 000`` directory beside a healthy repo used to raise
    ``PermissionError`` out of the walk. ``Path.exists()`` swallows ENOENT but
    NOT EACCES, and the probe sat outside the ``try/except OSError`` that
    guards ``os.scandir``. There is no exception handler on the API app, so the
    result was an unhandled 500 on the first-run onboarding path - losing the
    entire list, including the repos that scanned fine.
    """
    root = tmp_path / "Projects"
    _fake_repo(root / "healthy")
    locked = root / "locked"
    (locked / ".git").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        _assert_unreadable(locked)
        res = discover_repos(home=tmp_path)
    finally:
        locked.chmod(0o755)

    assert "healthy" in _by_name(res), "a readable repo must survive an unreadable sibling"
    assert "locked" not in _by_name(res)


def test_an_unreadable_root_is_skipped_not_raised(tmp_path):
    locked_root = tmp_path / "git"
    locked_root.mkdir()
    _fake_repo(tmp_path / "Projects" / "healthy")
    locked_root.chmod(0o000)
    try:
        _assert_unreadable(locked_root)
        res = discover_repos(home=tmp_path)
    finally:
        locked_root.chmod(0o755)
    assert "healthy" in _by_name(res)


def test_describing_an_unreadable_repo_answers_instead_of_raising(tmp_path):
    """The same EACCES class one layer down: the ``.exists()`` probes inside
    the per-row describe (`is_git`, `_quick_ecosystem`) were unguarded too."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        _assert_unreadable(locked)
        row = repo_discovery._describe_cheap(locked, time.monotonic() + 5)
    finally:
        locked.chmod(0o755)
    assert row["is_git"] is False
    assert row["ecosystem"] == ""
    assert row["dirty_scan"] == "not-a-repo"


def test_a_directory_merely_named_dot_git_is_not_a_repo(tmp_path):
    """A *directory* called ``.git`` is not proof of a repository - an unpacked
    archive or a backup folder has one. Worse than the false row: the entry was
    treated as a leaf, so the genuine repos nested beneath it were never found.
    """
    fake = tmp_path / "git" / "backup-2019"
    (fake / ".git").mkdir(parents=True)      # no HEAD - not a git directory
    _fake_repo(fake / "real-svc")

    got = _by_name(discover_repos(home=tmp_path))
    assert "backup-2019" not in got, "a bare .git name must not pass as a repository"
    assert "real-svc" in got, "a repo nested under the false positive must still be found"


def test_a_symlinked_alias_of_a_repo_is_not_a_second_row(tmp_path):
    """``sorted(set(found))`` deduped literal paths, so an alias and its target
    were two rows for one checkout - and picking either starts the same task."""
    root = tmp_path / "Projects"
    plain = _fake_repo(root / "plain")
    (root / "alias").symlink_to(plain)

    res = discover_repos(home=tmp_path)
    assert [r["path"] for r in res["repos"]] == [str(plain)]
    assert res["total_found"] == 1


def test_a_bare_repository_is_skipped_and_never_enumerated(tmp_path, monkeypatch):
    """A bare repo has no working tree to point a task at, so offering it would
    be wrong - but descending into it to enumerate ``objects/`` and ``refs/``
    is wrong regardless of that call."""
    bare = tmp_path / "git" / "mirror.git"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)],
                   check=True, capture_output=True)
    _fake_repo(tmp_path / "git" / "worktree-repo")

    seen: list[str] = []
    real = repo_discovery._scandir

    def spy(d):
        seen.append(str(d))
        return real(d)

    monkeypatch.setattr(repo_discovery, "_scandir", spy)
    got = _by_name(discover_repos(home=tmp_path))

    assert "mirror.git" not in got
    assert "worktree-repo" in got
    assert not any("mirror.git" in s for s in seen), \
        f"the walk enumerated a bare repository's internals: {seen}"


# --------------------------------------------------------------------------- #
# The walk has its own wall-clock budget                                       #
# --------------------------------------------------------------------------- #

def test_a_slow_root_cannot_stall_the_request_forever(tmp_path, monkeypatch):
    """``DIRTY_BUDGET_S`` covers only the git-probing phase. A network-mounted
    or otherwise slow root stalled the walk itself with nothing to stop it.
    """
    for i in range(12):
        _fake_repo(tmp_path / "git" / f"d{i:02d}" / "repo")

    real = repo_discovery._scandir

    def slow(d):
        time.sleep(0.1)
        return real(d)

    monkeypatch.setattr(repo_discovery, "_scandir", slow)
    monkeypatch.setattr(repo_discovery, "WALK_BUDGET_S", 0.25)

    t0 = time.perf_counter()
    res = discover_repos(home=tmp_path)
    wall = time.perf_counter() - t0

    # 13 scandir calls at 0.1s each is 1.3s with no budget in the way.
    assert wall < 1.0, f"the walk ran for {wall:.2f}s past its budget"
    assert res["walk_truncated"] is True
    assert res["note"], "a truncated search must say so, not return a short list silently"


def test_a_truncated_walk_still_returns_what_it_found(tmp_path, monkeypatch):
    """Degrade honestly: partial results plus a flag, the way the git probe
    already does - not an empty list and not an exception."""
    # "Projects" is scanned before "git", so the fast root finishes first and
    # the slow one is what the budget cuts off.
    for i in range(6):
        _fake_repo(tmp_path / "Projects" / f"r{i}")
    (tmp_path / "git" / "slow-tree" / "buried").mkdir(parents=True)
    _fake_repo(tmp_path / "git" / "slow-tree" / "buried" / "unreached")

    real = repo_discovery._scandir
    calls = {"n": 0}

    def slow_after_the_first(d):
        calls["n"] += 1
        if calls["n"] > 1:
            time.sleep(0.4)
        return real(d)

    monkeypatch.setattr(repo_discovery, "_scandir", slow_after_the_first)
    monkeypatch.setattr(repo_discovery, "WALK_BUDGET_S", 0.2)

    res = discover_repos(home=tmp_path)
    names = set(_by_name(res))
    assert names == {f"r{i}" for i in range(6)}, \
        "the root that finished must keep its results"
    assert "unreached" not in names
    assert res["walk_truncated"] is True


def test_an_untruncated_walk_says_so(tmp_path):
    _fake_repo(tmp_path / "git" / "only")
    res = discover_repos(home=tmp_path)
    assert res["walk_truncated"] is False
    assert res["note"] == ""
