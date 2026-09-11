"""A task worktree must be able to build, or the coder cannot run anything.

`web/dist` is a gitignored build artifact, and it is force-included into both
the wheel and the sdist (by `hatch_build.py` since 2026-08-01; by a static
`force-include` before that). A forced source that is absent fails the build —
deliberately, so a release cut without `npm run build` fails loudly instead of
shipping a boardless wheel.

`git worktree add` never creates a gitignored path, so in a task worktree that
same deliberate loudness killed `uv run` / `uv build` / any editable install
BEFORE test collection. The coder could not run the suite at all, and the
failure did not look like a packaging problem — it looked like a broken repo.
(`hatch_build.py` since makes the EDITABLE install warn instead of die, which is
the same defect met from the other side — a clean clone could not `uv sync`.
Provisioning still matters: a boardless worktree still cannot build a wheel, and
its board tests have nothing to run against.)

Observed 2026-08-01: a task burned its entire lifetime budget (2.58M
cost-weighted tokens in one attempt) without ever reaching a green run, and its
reviewer traced the red suite to exactly this.

Same class as the `node_modules` trap `_ensure_node_deps` already solves, and
taken from the source checkout the same way rather than rebuilt. It is COPIED
where node_modules is symlinked: `vite build` writes into `web/dist`, so a link
would let a UI task rebuild into the developer's checkout. 1 MB buys isolation.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from no_human.testing.runner import _ensure_forced_build_artifacts

REPO_ROOT = Path(__file__).resolve().parents[1]

# `web/dist` is gitignored, so REPO_ROOT is itself boardless whenever this file
# runs from a task worktree rather than the primary checkout — and then there is
# nothing to provision FROM. Skipping is the honest outcome: the provisioning
# tests would otherwise assert that a copy appeared from an empty source, which
# is the same environment-dependence that made two scheduler tests pass or fail
# on whether an editor happened to be open.
needs_a_built_source = pytest.mark.skipif(
    not (REPO_ROOT / "web" / "dist" / "index.html").is_file(),
    reason="this checkout has no built board to provision from "
           "(gitignored artifact — run `cd web && npm run build`, or this is a "
           "task worktree, where the runner provisions it at test time)",
)


def test_pyproject_still_force_includes_something():
    """Guards the guard: this whole file is moot if nothing is forced.

    Both declaration sites count. `web/dist` is force-included by
    `hatch_build.py` rather than by the static table since 2026-08-01, and the
    provisioning under test reads the hook's `source` for exactly that reason —
    checking only the static table would have left this passing on `migrations`
    alone while the path this file is about had quietly stopped being
    provisioned.
    """
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = cfg["tool"]["hatch"]["build"]["targets"]
    forced = set()
    for target in targets.values():
        forced.update((target.get("force-include") or {}).keys())
        for hook in (target.get("hooks") or {}).values():
            if isinstance(hook.get("source"), str):
                forced.add(hook["source"])
    assert forced, "no force-include left — this provisioning step is dead code"
    assert "web/dist" in forced, (
        "the board is no longer a forced path anywhere, so provisioning it into "
        "a task worktree is dead code and this file's subject is gone")


def _worktree(tmp_path: Path) -> Path:
    """A real `git worktree add`, because the defect IS a property of one."""
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(wt), "HEAD"],
                   cwd=REPO_ROOT, check=True, capture_output=True)
    return wt


@pytest.fixture
def worktree(tmp_path):
    wt = _worktree(tmp_path)
    yield wt
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                   cwd=REPO_ROOT, capture_output=True)


def test_a_fresh_worktree_really_is_missing_the_forced_path(worktree):
    """The precondition. If this ever fails the defect is gone and so is the
    reason for the code under test — better to be told than to keep a passing
    test that proves nothing."""
    assert not (worktree / "web" / "dist").exists()


@needs_a_built_source
def test_provisioning_makes_the_forced_path_resolvable(worktree):
    _ensure_forced_build_artifacts(worktree, REPO_ROOT)
    dist = worktree / "web" / "dist"
    assert dist.exists(), "forced include still missing after provisioning"
    assert (dist / "index.html").is_file(), (
        "provisioned, but the board is not readable through it")


@needs_a_built_source
def test_it_is_a_copy_so_a_rebuild_cannot_reach_the_developers_checkout(worktree):
    """The isolation property, and the one place this deliberately differs from
    `_ensure_node_deps`.

    `web/package.json`'s build script is `vite build`, which WRITES into
    `web/dist`. Through a symlink, a task that touches the UI would rebuild
    straight into the developer's checkout — the same cross-contamination class
    that a hijacked venv caused on 2026-08-01. `node_modules` is linked because
    it is hundreds of megabytes; this is 1 MB, so isolation is nearly free.

    Asserted by WRITING through the provisioned path, which is what a task would
    actually do — a `is_symlink()` check alone would not prove the source is
    safe from a rebuild that replaces the directory.
    """
    _ensure_forced_build_artifacts(worktree, REPO_ROOT)
    dist = worktree / "web" / "dist"
    assert not dist.is_symlink(), "a symlink lets a task rebuild into the source"

    source_index = REPO_ROOT / "web" / "dist" / "index.html"
    before = source_index.read_bytes()
    (dist / "index.html").write_text("<html>rebuilt by a task</html>")
    assert source_index.read_bytes() == before, (
        "writing to the worktree's board changed the developer's checkout")


def test_it_never_clobbers_a_real_directory(worktree):
    """A worktree that built its own board keeps it."""
    dist = worktree / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>mine</html>")
    _ensure_forced_build_artifacts(worktree, REPO_ROOT)
    assert not dist.is_symlink()
    assert (dist / "index.html").read_text(encoding="utf-8") == "<html>mine</html>"


def test_it_is_a_no_op_without_a_source_repo(worktree):
    """`source_repo is None` means this IS the primary checkout — there is
    nothing to borrow from and nothing to fix."""
    _ensure_forced_build_artifacts(worktree, None)
    assert not (worktree / "web" / "dist").exists()


def test_it_never_raises_on_a_repo_with_no_pyproject(tmp_path):
    """Best-effort, like the node twin: a provisioning failure must not become
    the reason a test run fails."""
    (tmp_path / "empty").mkdir()
    _ensure_forced_build_artifacts(tmp_path / "empty", REPO_ROOT)  # must not raise


def test_it_never_raises_when_the_source_has_not_built_it_either(worktree, tmp_path):
    """Then the loud failure is CORRECT — there is genuinely no board to
    package — so this must decline quietly rather than invent one."""
    bare = tmp_path / "bare-source"
    bare.mkdir()
    _ensure_forced_build_artifacts(worktree, bare)
    assert not (worktree / "web" / "dist").exists()


@pytest.mark.slow
def test_the_worktree_can_actually_build_after_provisioning(worktree, tmp_path):
    """The claim, end to end, at the level the defect actually bit.

    Every test above asserts on the filesystem; none of them proves hatchling
    is satisfied. This runs the real build in a real worktree — first proving it
    FAILS without provisioning, so the test cannot pass for the wrong reason.
    """
    import shutil
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH — cannot build")
    if not (REPO_ROOT / "web" / "dist" / "index.html").is_file():
        pytest.skip("the source checkout has no built board to link")

    before = subprocess.run(["uv", "build", "--wheel", "-o", str(tmp_path / "d1")],
                            cwd=worktree, capture_output=True, text=True, timeout=600)
    assert before.returncode != 0, (
        "precondition failed: the worktree built WITHOUT provisioning, so this "
        "test would pass even if the fix were removed")
    # `hatch_build.py` replaced hatchling's raw `Forced include not found` with
    # an actionable one; a WHEEL build must still be the loud case.
    assert "npm run build" in (before.stderr + before.stdout)

    _ensure_forced_build_artifacts(worktree, REPO_ROOT)

    after = subprocess.run(["uv", "build", "--wheel", "-o", str(tmp_path / "d2")],
                           cwd=worktree, capture_output=True, text=True, timeout=600)
    assert after.returncode == 0, f"build still failed:\n{after.stderr[-2000:]}"


def test_a_tracked_forced_path_is_never_restored(worktree):
    """A deletion the branch made must stay visible to the branch's own tests.

    Found in review: `migrations/` is force-included AND tracked, so a task that
    deleted the whole directory had it silently restored here and could watch
    its own suite go green — including the test whose entire job is to assert
    the migrations exist. Provisioning exists for gitignored build artifacts;
    a tracked path is absent for exactly one reason, and that reason is news.

    The export gate refuses that deletion downstream on a count pin and nine
    content pins, so nothing could ship — but a task must not be able to mislead
    itself in the meantime.
    """
    import tomllib as _t
    cfg = _t.loads((worktree / "pyproject.toml").read_text(encoding="utf-8"))
    targets = cfg["tool"]["hatch"]["build"]["targets"]
    forced = set()
    for target in targets.values():
        forced.update((target.get("force-include") or {}).keys())
    tracked = [r for r in forced
               if subprocess.run(["git", "ls-files", "--error-unmatch", r],
                                 cwd=worktree, capture_output=True).returncode == 0]
    if not tracked:
        pytest.skip("no tracked path is force-included, so nothing can be masked")

    victim = worktree / tracked[0]
    shutil.rmtree(victim) if victim.is_dir() else victim.unlink()
    assert not victim.exists()

    _ensure_forced_build_artifacts(worktree, REPO_ROOT)

    assert not victim.exists(), (
        f"{tracked[0]} is tracked and was deleted by the branch, but "
        "provisioning restored it — the branch's own tests would go green on a "
        "deletion it made")
