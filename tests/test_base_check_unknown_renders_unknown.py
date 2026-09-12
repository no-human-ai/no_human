"""Every indeterminate base-tree-check outcome must render as UNKNOWN to the
reviewer — never as "pre-existing" / "not this change's fault".

`Orchestrator._newly_failing_vs_base` is fail-closed by design: it never
GUESSES whether a failing id is new or pre-existing when the base-tree
re-run did not produce a trustworthy verdict — it returns `None`, which
`_attribution_buckets` (src/no_human/core/orchestrator.py) then puts in the
"unknown" bucket instead of silently defaulting to "pre-existing" (the
mistake this test file exists to keep from creeping back in: a `None` that
downstream code treats as falsy-therefore-pre-existing would exculpate a
regression the harness never actually checked).

This file drives the REAL `_newly_failing_vs_base` (not a stub) against 7
named indeterminate outcomes, each hitting a specific fail-closed branch in
that function:

  1. base run errored               -> `not result.ran`
  2. different collected set        -> `not set(failing) <= reported`
  3. renamed/absent test id         -> `not set(failing) <= reported`
  4. timeout (no names reported)    -> `not set(failing) <= reported`
  5. empty result (0 collected)     -> `not set(failing) <= reported`
  6. non-pytest command             -> `"pytest" not in cmd.lower()`
  7. worktree-add failure           -> `except Exception` around `worktree add`

...and then feeds the (necessarily `None`) verdict through the REAL
`_FailureAttribution` / `_attribution_buckets` / `_render_failing_attribution`
pipeline, asserting every one of the 7 renders every requested id under
"ATTRIBUTION UNKNOWN", never under "ALSO RED ON THE BASE TREE", and never
paired with exculpatory wording.

Each negative case is paired with a positive control that flips exactly the
one input the branch keys off of and asserts the verdict stops being `None`
— proving the negative result is actually caused by the condition under
test, not by some other mistake in the harness (an always-`None` stub would
pass every negative case here for the wrong reason).

Only `no_human.core.orchestrator.runner.run_tests` is ever mocked (a plain
`Mock`, not `AsyncMock` — `_newly_failing_vs_base` calls it via
`asyncio.to_thread`, so an `AsyncMock` return value would be an unawaited
coroutine object, not a `TestRunResult`, and the bug would be invisible).
Everything else — the repo, the worktree add/remove, `_review_base` — is the
real thing, on a real throwaway git repo built per test.
"""

import shlex
import subprocess
from unittest.mock import Mock, patch

from no_human.core.orchestrator import (
    Orchestrator,
    _attribution_buckets,
    _FailureAttribution,
    _render_failing_attribution,
)
from no_human.testing import runner
from no_human.vcs.git import GitRepo

RUN_TESTS = "no_human.core.orchestrator.runner.run_tests"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _two_commit_repo(tmp_path, name="work") -> GitRepo:
    """A real repo where `HEAD~1` resolves, so a real (unmocked)
    `git worktree add --detach <dir> HEAD~1` succeeds — needed for every
    scenario except #7, which relies on `HEAD~1` NOT resolving."""
    work = tmp_path / name
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    (work / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "second")
    return GitRepo(work)


def _one_commit_repo(tmp_path, name="work1") -> GitRepo:
    """A real repo with no `HEAD~1` at all — `_review_base(repo, None)`
    still returns the literal string `"HEAD~1"`, but the real (unmocked)
    `git worktree add --detach <dir> HEAD~1` this drives fails outright,
    exercising the worktree-add-failure branch without any mocking of
    `repo._run` itself."""
    work = tmp_path / name
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    return GitRepo(work)


def _orch() -> Orchestrator:
    # `_newly_failing_vs_base` only ever touches `self._review_base` — itself
    # stateless beyond its `repo`/`base` arguments — never `self.config` /
    # `self.store` / anything else `__init__` sets up. `Orchestrator.__new__`
    # (skipping `__init__` entirely) is the same pattern the class's own
    # class-level-default comments document existing tests already rely on
    # (`_active_task_id`, `_attempt_backend`, `config = {}`), so this is a
    # faithful call to the real method, not a stub reimplementation of it.
    return Orchestrator.__new__(Orchestrator)


def _tr(failing_ids: list[str]) -> runner.TestRunResult:
    blocks = [f"FAILED {i} - AssertionError: assert 5 == 6" for i in failing_ids]
    return runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=len(failing_ids), errors=0,
        command="pytest -q",
        output="\n".join(blocks) + f"\n{len(failing_ids)} failed, 1 passed in 0.01s\n",
        full_output="",
        failure_blocks=blocks,
        failing_tests=list(failing_ids),
    )


def _bounded_cmd(test_cmd: str, failing_ids: list[str]) -> str:
    """Reproduces the exact `bounded_cmd` `_newly_failing_vs_base` builds
    (`cmd + " -rA " + " ".join(shlex.quote(t) for t in failing_tests)`) so a
    mocked `TestRunResult.command` matches it — the function discards any
    result whose `.command` differs (the runner-substituted-a-command guard)
    BEFORE it ever reaches the by-name subset check, so a mismatched
    `.command` here would make every scenario below pass for the wrong
    reason (a command mismatch) instead of the branch each one names."""
    return test_cmd + " -rA " + " ".join(shlex.quote(t) for t in failing_ids)


def _forbid_run_tests_call() -> Mock:
    def _boom(*a, **k):  # pragma: no cover - only reached on regression
        raise AssertionError(
            "runner.run_tests must never be called on this branch"
        )
    return Mock(side_effect=_boom)


async def _newly(repo, failing_ids, *, test_cmd="pytest -q", run_tests_mock):
    orch = _orch()
    with patch(RUN_TESTS, run_tests_mock):
        return await orch._newly_failing_vs_base(
            repo, test_cmd, None, failing_ids, cwd=None, env_dependent=False,
        )


def _assert_renders_unknown(failing_ids, newly, owned=None) -> str:
    assert newly is None, newly
    tr = _tr(failing_ids)
    att = _FailureAttribution(
        result=tr, failing=list(failing_ids), newly=newly, owned=owned or [],
    )
    new_ids, pre_existing_ids, unknown_ids = _attribution_buckets(att)
    assert new_ids == [], new_ids
    assert pre_existing_ids == [], pre_existing_ids
    assert sorted(unknown_ids) == sorted(failing_ids), unknown_ids

    rendered = _render_failing_attribution(att)
    assert "ATTRIBUTION UNKNOWN" in rendered, rendered
    assert "NEW — failing here" not in rendered, rendered
    assert "ALSO RED ON THE BASE TREE" not in rendered, rendered
    for fid in failing_ids:
        assert fid in rendered, rendered
    for phrase in (
        "not this change's fault", "not this change's defect",
        "already red before this change", "pre-existing",
    ):
        assert phrase not in rendered, (phrase, rendered)
    return rendered


def _assert_renders_split(failing_ids, newly, *, expect_new, expect_pre_existing,
                           owned=None) -> str:
    """The positive-control counterpart of `_assert_renders_unknown`: a
    trustworthy verdict must land ids in their TRUE bucket, never UNKNOWN."""
    assert newly is not None, "expected a real (non-None) verdict"
    tr = _tr(failing_ids)
    att = _FailureAttribution(
        result=tr, failing=list(failing_ids), newly=newly, owned=owned or [],
    )
    new_ids, pre_existing_ids, unknown_ids = _attribution_buckets(att)
    assert sorted(new_ids) == sorted(expect_new), new_ids
    assert sorted(pre_existing_ids) == sorted(expect_pre_existing), pre_existing_ids
    assert unknown_ids == [], unknown_ids

    rendered = _render_failing_attribution(att)
    assert "ATTRIBUTION UNKNOWN" not in rendered, rendered
    if expect_new:
        assert "NEW — failing here" in rendered, rendered
    if expect_pre_existing:
        assert "ALSO RED ON THE BASE TREE" in rendered, rendered
    return rendered


# ---------------------------------------------------------------------------
# 1. base run errored
# ---------------------------------------------------------------------------

async def test_base_run_errored_renders_unknown(tmp_path):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    errored = runner.TestRunResult(
        ran=False, ok=False, passed=0, failed=0, errors=0,
        command="pytest -q", output="",
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=errored))
    _assert_renders_unknown(ids, newly)


async def test_base_run_errored_positive_control_clean_run_is_not_unknown(tmp_path):
    """Flips the one thing scenario 1 keys off of (`result.ran`) and nothing
    else — proves the `None` above came from `ran=False`, not from some
    unrelated bug that always returns `None`."""
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    clean = runner.TestRunResult(
        ran=True, ok=True, passed=2, failed=0, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="",
        passed_tests=list(ids), failing_tests=[],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=clean))
    _assert_renders_split(ids, newly, expect_new=ids, expect_pre_existing=[])


# ---------------------------------------------------------------------------
# 2. different collected set
# ---------------------------------------------------------------------------

async def test_different_collected_set_renders_unknown(tmp_path):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    unrelated = runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=1, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="",
        passed_tests=["tests/test_other.py::test_x"],
        failing_tests=["tests/test_other.py::test_y"],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=unrelated))
    _assert_renders_unknown(ids, newly)


async def test_different_collected_set_positive_control_matching_set_is_not_unknown(
    tmp_path,
):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    matching = runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=1, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="",
        passed_tests=[ids[1]], failing_tests=[ids[0]],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=matching))
    _assert_renders_split(ids, newly, expect_new=[ids[1]], expect_pre_existing=[ids[0]])


# ---------------------------------------------------------------------------
# 3. renamed / absent test id (partial collected-set mismatch)
# ---------------------------------------------------------------------------

async def test_renamed_test_id_renders_unknown(tmp_path):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    partial = runner.TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="",
        # `test_add` renamed on base — it simply isn't in the reported set.
        passed_tests=[], failing_tests=[ids[0]],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=partial))
    _assert_renders_unknown(ids, newly)


async def test_renamed_test_id_positive_control_both_present_is_not_unknown(tmp_path):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    both_present = runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=1, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="",
        passed_tests=[ids[1]], failing_tests=[ids[0]],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=both_present))
    _assert_renders_split(ids, newly, expect_new=[ids[1]], expect_pre_existing=[ids[0]])


# ---------------------------------------------------------------------------
# 4. timeout (a timed-out run reports no names at all)
# ---------------------------------------------------------------------------

async def test_timeout_renders_unknown(tmp_path):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    timed_out = runner.TestRunResult(
        ran=True, ok=False, passed=0, failed=0, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="TIMEOUT after 600s",
        passed_tests=[], failing_tests=[],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=timed_out))
    _assert_renders_unknown(ids, newly)


async def test_timeout_positive_control_names_reported_is_not_unknown(tmp_path):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    completed = runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=1, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="",
        passed_tests=[ids[1]], failing_tests=[ids[0]],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=completed))
    _assert_renders_split(ids, newly, expect_new=[ids[1]], expect_pre_existing=[ids[0]])


# ---------------------------------------------------------------------------
# 5. empty result (0 collected on base — e.g. a marker/path only added later)
# ---------------------------------------------------------------------------

async def test_empty_result_renders_unknown(tmp_path):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    empty = runner.TestRunResult(
        ran=True, ok=True, passed=0, failed=0, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="collected 0 items",
        passed_tests=[], failing_tests=[],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=empty))
    _assert_renders_unknown(ids, newly)


async def test_empty_result_positive_control_nonempty_collection_is_not_unknown(
    tmp_path,
):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    collected = runner.TestRunResult(
        ran=True, ok=True, passed=2, failed=0, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="",
        passed_tests=list(ids), failing_tests=[],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=collected))
    _assert_renders_split(ids, newly, expect_new=ids, expect_pre_existing=[])


# ---------------------------------------------------------------------------
# 6. non-pytest command
# ---------------------------------------------------------------------------

async def test_non_pytest_command_renders_unknown(tmp_path):
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    newly = await _newly(
        repo, ids, test_cmd="npm test", run_tests_mock=_forbid_run_tests_call(),
    )
    _assert_renders_unknown(ids, newly)


async def test_non_pytest_command_positive_control_pytest_command_is_not_unknown(
    tmp_path,
):
    """Flips only the command string — same repo, same ids, same mocked base
    run — proving scenario 6's `None` comes from the command name, not from
    the repo/worktree machinery this positive control also exercises."""
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    completed = runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=1, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="",
        passed_tests=[ids[1]], failing_tests=[ids[0]],
    )
    newly = await _newly(
        repo, ids, test_cmd="pytest -q", run_tests_mock=Mock(return_value=completed),
    )
    _assert_renders_split(ids, newly, expect_new=[ids[1]], expect_pre_existing=[ids[0]])


# ---------------------------------------------------------------------------
# 7. worktree-add failure
# ---------------------------------------------------------------------------

async def test_worktree_add_failure_renders_unknown(tmp_path):
    repo = _one_commit_repo(tmp_path)  # no HEAD~1 -> a real `worktree add` fails
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    newly = await _newly(repo, ids, run_tests_mock=_forbid_run_tests_call())
    _assert_renders_unknown(ids, newly)


async def test_worktree_add_failure_positive_control_resolvable_base_is_not_unknown(
    tmp_path,
):
    """Flips only whether `HEAD~1` resolves (one commit vs. two) — same ids,
    same mocked base run once the worktree exists — proving scenario 7's
    `None` comes from the failed `worktree add`, not from the ids or the
    mocked run."""
    repo = _two_commit_repo(tmp_path)
    ids = ["tests/test_calc.py::test_mul", "tests/test_calc.py::test_add"]
    completed = runner.TestRunResult(
        ran=True, ok=False, passed=1, failed=1, errors=0,
        command=_bounded_cmd("pytest -q", ids), output="",
        passed_tests=[ids[1]], failing_tests=[ids[0]],
    )
    newly = await _newly(repo, ids, run_tests_mock=Mock(return_value=completed))
    _assert_renders_split(ids, newly, expect_new=[ids[1]], expect_pre_existing=[ids[0]])


# ---------------------------------------------------------------------------
# Ownership annotation survives an UNKNOWN verdict (belt-and-braces: AC3
# already pins this at the full-round level in
# tests/test_pre_review_red_attribution.py — this repeats the check at the
# narrower base-check-function level this file otherwise covers).
# ---------------------------------------------------------------------------

async def test_owned_id_still_annotated_when_base_check_is_inconclusive(tmp_path):
    repo = _one_commit_repo(tmp_path)
    owned_id = "tests/test_calc.py::test_mul"
    other_id = "tests/test_calc.py::test_add"
    ids = [owned_id, other_id]
    newly = await _newly(repo, ids, run_tests_mock=_forbid_run_tests_call())
    rendered = _assert_renders_unknown(ids, newly, owned=[owned_id])
    owned_lines = [ln for ln in rendered.splitlines() if f"`{owned_id}`" in ln]
    assert owned_lines, rendered
    assert "[MODIFIED BY THIS DIFF]" in owned_lines[0], owned_lines
