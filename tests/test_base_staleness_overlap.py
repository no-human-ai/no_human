"""A base gap can be SMALL and RELATED, and a commit count cannot see it.

Issue #141. `BASE_STALENESS_REBASE_THRESHOLD` rebases only past a 5-commit
gap, and that number was bought with real incidents, so nothing here lowers
it. But on 2026-09-07/08 three tasks each paid a full conflict-resolution
round on gaps of one to three commits that touched the branch's own files:
`0e1edabb` against `d071a751`, `05b71017` against `4cbf73e3`, and `82644133`
against `72ca3a0b`. That is what a fleet landing several fixes in one
subsystem back to back looks like, and every follower pays a round.

These tests use REAL git repositories rather than stubs, because the risky
part is the range syntax, not the set logic: a two-dot `base HEAD` reports
commits that landed on base since we branched as deletions of ours, so it
returns the union of both sides and EVERY gap would look like an overlap.
`vcs/git.py::head_commit` already records that trap; the test named for it
below pins that this code did not fall into it.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from no_human.core.base_staleness import (
    GENERATED_PATHS,
    base_gap_overlap,
    overlapping_paths,
    should_rebase,
    staleness_record,
)
from no_human.vcs.git import GitRepo

THRESHOLD = 5


def _run(*args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, "git %s failed: %s" % (args[0], proc.stderr)
    return proc.stdout


def _commit(root: Path, name: str, text: str, message: str):
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text, encoding="utf-8")
    _run("add", "-A", cwd=root)
    _run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message,
         cwd=root)


@pytest.fixture()
def repo(tmp_path):
    """A repo on `main` with one commit, plus a `work` branch forked from it.

    Callers then land commits on `main` and switch back to `work`, which is
    the exact shape a retry meets: a branch cut from a base that moved.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _run("init", "-q", "-b", "main", ".", cwd=root)
    _commit(root, "src/app.py", "ORIGINAL\n", "init")
    _commit(root, "src/other.py", "ORIGINAL\n", "second file")
    _run("checkout", "-q", "-b", "work", cwd=root)
    return root


def _land_on_main(root: Path, files: dict, message: str = "landing"):
    """Land one commit on main touching `files`, then return to `work`."""
    _run("checkout", "-q", "main", cwd=root)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    _run("add", "-A", cwd=root)
    _run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message,
         cwd=root)
    _run("checkout", "-q", "work", cwd=root)


def _behind(root: Path) -> int:
    return int(_run("rev-list", "--count", "HEAD..main", cwd=root).strip())


# --------------------------------------------------------------------------
# The pure half.
# --------------------------------------------------------------------------

def test_overlapping_paths_is_the_intersection_minus_generated_pins():
    assert overlapping_paths(["a.py", "b.py"], ["b.py", "c.py"]) == ["b.py"]
    assert overlapping_paths(["a.py"], ["b.py"]) == []


def test_the_generated_manifest_never_counts_as_an_overlap():
    """RELEASE_MANIFEST.txt pins every shipped file, so it changes on nearly
    every landing AND nearly every branch, and it is regenerated during the
    merge anyway. Counting it would rebase on essentially every gap and
    quietly undo the threshold.

    This is not hypothetical. Measured while writing this change: a branch
    three commits behind main intersected on RELEASE_MANIFEST.txt and nothing
    else, so without this exclusion it would have rebased for nothing."""
    assert "RELEASE_MANIFEST.txt" in GENERATED_PATHS
    assert overlapping_paths(
        ["RELEASE_MANIFEST.txt", "src/mine.py"],
        ["RELEASE_MANIFEST.txt", "src/theirs.py"]) == []


def test_should_rebase_keeps_the_threshold_and_adds_overlap():
    assert should_rebase(THRESHOLD, THRESHOLD, []) is True     # today's rule
    assert should_rebase(THRESHOLD + 90, THRESHOLD, []) is True
    assert should_rebase(1, THRESHOLD, []) is False            # still noise
    assert should_rebase(1, THRESHOLD, ["src/app.py"]) is True  # the new rule


def test_the_record_says_why_a_below_threshold_gap_was_acted_on():
    """Without this a reader sees a rebase on a 1-commit gap and no reason."""
    rec = staleness_record(1, True, ["src/app.py"])
    assert rec["was_behind"] == 1
    assert rec["commits_behind"] == 0          # current staleness after rebase
    assert rec["overlapping_files"] == ["src/app.py"]


# --------------------------------------------------------------------------
# The git half. Real repositories, real ranges.
# --------------------------------------------------------------------------

def test_THE_PLANT_one_related_commit_now_rebases(repo):
    """The issue's plant: main gains ONE commit touching a file the branch
    also touches. Below the threshold, so today it is ignored and the round
    is paid at the approve gate."""
    _commit(repo, "src/app.py", "MINE\n", "branch work")
    _land_on_main(repo, {"src/app.py": "THEIRS\n"})

    behind = _behind(repo)
    overlap = base_gap_overlap(GitRepo(repo), "main", behind)
    assert behind == 1
    assert overlap == ["src/app.py"]
    assert should_rebase(behind, THRESHOLD, overlap) is True


def test_THE_CONTROL_one_unrelated_commit_still_does_not_rebase(repo):
    """The decision the threshold was bought with, and the one this change
    must not damage: most gaps ARE noise, an unrelated commit landing on main
    mid-attempt, and those must still be left alone."""
    _commit(repo, "src/app.py", "MINE\n", "branch work")
    _land_on_main(repo, {"src/other.py": "THEIRS\n"})

    behind = _behind(repo)
    overlap = base_gap_overlap(GitRepo(repo), "main", behind)
    assert behind == 1
    assert overlap == []
    assert should_rebase(behind, THRESHOLD, overlap) is False


def test_a_large_disjoint_gap_still_rebases_exactly_as_today(repo):
    """The unconditional threshold is untouched."""
    _commit(repo, "src/app.py", "MINE\n", "branch work")
    for i in range(THRESHOLD):
        _land_on_main(repo, {"src/unrelated%d.py" % i: "x\n"}, "landing %d" % i)

    behind = _behind(repo)
    overlap = base_gap_overlap(GitRepo(repo), "main", behind)
    assert behind == THRESHOLD
    assert overlap == []
    assert should_rebase(behind, THRESHOLD, overlap) is True


def test_a_manifest_only_intersection_does_not_rebase(repo):
    """The live case measured on this very branch: both sides touched the
    generated pin file and nothing else."""
    _commit(repo, "RELEASE_MANIFEST.txt", "mine\n", "repin")
    _land_on_main(repo, {"RELEASE_MANIFEST.txt": "theirs\n"})

    behind = _behind(repo)
    overlap = base_gap_overlap(GitRepo(repo), "main", behind)
    assert behind == 1
    assert overlap == []
    assert should_rebase(behind, THRESHOLD, overlap) is False


def test_the_two_dot_trap_would_have_made_every_gap_look_related(repo):
    """`vcs/git.py::head_commit` records this trap and this pins that the
    overlap measurement avoids it. A two-dot `main HEAD` returns the union of
    both sides, so a DISJOINT gap would read as an overlap and the control
    above would flip. Three-dot asks each side what it alone changed."""
    _commit(repo, "src/app.py", "MINE\n", "branch work")
    _land_on_main(repo, {"src/other.py": "THEIRS\n"})
    git = GitRepo(repo)

    two_dot = set(git.changed_files("main"))
    assert {"src/app.py", "src/other.py"} <= two_dot, (
        "the two-dot range is expected to contain BOTH sides; if this ever "
        "stops being true the trap this test guards has changed shape")

    assert git.files_changed_since_fork("main") == ["src/app.py"]
    assert git.files_changed_since_fork("main", on_base=True) == ["src/other.py"]


def test_an_unmeasurable_base_degrades_to_the_count_only_behaviour(repo):
    """`_refresh_stale_base` must never fail an attempt, so a base git cannot
    resolve reports no overlap rather than raising or guessing."""
    assert base_gap_overlap(GitRepo(repo), "no-such-branch", 3) == []
    assert base_gap_overlap(GitRepo(repo), None, 3) == []
    assert base_gap_overlap(GitRepo(repo), "main", 0) == []


# --------------------------------------------------------------------------
# The wiring.
# --------------------------------------------------------------------------

def test_refresh_stale_base_actually_consults_the_overlap():
    """Every test above would still pass if `_refresh_stale_base` kept
    comparing the raw count, so pin the call site. Read over the AST: a
    substring search would be satisfied by the word appearing in a comment,
    and this repo has been caught by that before."""
    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "core"
    tree = ast.parse((src / "orchestrator.py").read_text(encoding="utf-8"))

    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef)
         and n.name == "_refresh_stale_base"), None)
    assert fn is not None, "_refresh_stale_base is gone or was renamed"

    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "base_gap_overlap" in called, (
        "_refresh_stale_base no longer measures file overlap, so a small "
        "related gap is silently back to being ignored (issue #141)")
    assert "should_rebase" in called, (
        "_refresh_stale_base decides with a bare comparison again rather "
        "than should_rebase, so the overlap cannot affect the decision")

    compares = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]
    bare = [n for n in compares
            if any(isinstance(op, ast.GtE) for op in n.ops)
            and isinstance(n.comparators[0], ast.Name)
            and n.comparators[0].id == "BASE_STALENESS_REBASE_THRESHOLD"]
    assert not bare, (
        "a bare `behind >= BASE_STALENESS_REBASE_THRESHOLD` is back in "
        "_refresh_stale_base; the decision belongs in should_rebase so the "
        "count and the overlap cannot drift apart")


def test_the_coder_preamble_decides_with_should_rebase_too():
    """Follow-up to the above: `_refresh_stale_base` deciding with
    `should_rebase` is not enough on its own — the coder-facing preamble in
    `_build_implement_prompt` re-derives its OWN decision of whether to
    narrate staleness, and until this follow-up it still used the bare
    count. That meant a 1-4 commit overlapping gap whose rebase conflicted
    was measured, attempted, and aborted, but the coder was told nothing and
    paid the exact conflict round the overlap detection exists to prevent.
    Pin the call site the same way as above, over the AST so a comment
    mentioning `should_rebase` cannot satisfy this."""
    src = Path(__file__).resolve().parents[1] / "src" / "no_human" / "core"
    tree = ast.parse((src / "orchestrator.py").read_text(encoding="utf-8"))

    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and n.name == "_build_implement_prompt"), None)
    assert fn is not None, "_build_implement_prompt is gone or was renamed"

    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "should_rebase" in called, (
        "_build_implement_prompt no longer decides the staleness narration "
        "with should_rebase, so an overlap-triggered failed rebase (1-4 "
        "commits, below threshold) narrates nothing to the coder")

    compares = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]
    bare = [n for n in compares
            if any(isinstance(op, ast.GtE) for op in n.ops)
            and isinstance(n.comparators[0], ast.Name)
            and n.comparators[0].id == "BASE_STALENESS_REBASE_THRESHOLD"]
    assert not bare, (
        "a bare `commits_behind >= BASE_STALENESS_REBASE_THRESHOLD` is back "
        "in _build_implement_prompt; a below-threshold overlapping gap that "
        "fails to rebase must still narrate, which the bare count cannot do")
