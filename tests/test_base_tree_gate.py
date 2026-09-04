"""Invocation errors are "infrastructure" only if the BASE tree shows them too
(docs/ARCH_REVIEW.md B2 #4).

An import/collection error used to be classified infrastructure uncondition-
ally: the reviewer was told to discount it and the test gate proceeded without
evidence — so a coder-INTRODUCED import breakage (a test importing a module
that doesn't exist, a gutted __init__) shipped as a PR with zero test signal.
The deterministic tiebreaker: run the same command on the base tree. Base
errors the same way → genuinely environmental, proceed as before. Base runs →
the change broke the runner; the attempt FAILS.
"""

import subprocess

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import (  # noqa: F401
    FakeBackend,
    _config,
    _git,
    bare_repo,
)


def _orch(store, tmp_path, backend):
    cfg = _config(tmp_path)
    # One attempt is enough to prove the gate; retries just repeat it slowly.
    cfg.data["bounds"] = {"max_attempts": 1}
    return Orchestrator(store, cfg.data, backend, SlackNotifier(None))


async def test_coder_introduced_import_breakage_fails_the_attempt(
    bare_repo, tmp_path, store
):
    # the fixture repo has no pytest marker — without one detect_command()
    # finds nothing and the whole test phase is skipped
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "pytest marker")

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )
        # a new test that cannot even be collected — import breakage the
        # tamper guard does not flag (test count goes UP)
        (cwd / "test_mul.py").write_text(
            "import nonexistent_dep_xyz  # noqa\nfrom calc import mul\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    reason = (attempts[-1]["failure_reason"] or "").lower()
    assert "base" in reason, (
        "the failure must SAY the base-tree comparison decided it: " + reason
    )


async def test_env_dependent_project_downgrades_reproduces_to_undeterminable(
    bare_repo, tmp_path, store
):
    """Review F1: a bare detached worktree gets none of the attempt's
    env_setup, so on a setup-dependent project a base-tree invocation error
    proves nothing. The verdict must downgrade to undeterminable (None), not
    confidently read 'environmental'."""
    (bare_repo / "conftest.py").write_text("import nonexistent_dep_xyz  # noqa\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "error on base too")

    from no_human.vcs.git import GitRepo
    orch = _orch(store, tmp_path, FakeBackend(lambda cwd: None))
    repo = GitRepo(str(bare_repo))

    confident = await orch._invocation_error_reproduces_on_base(
        repo, None, "main", env_dependent=False)
    downgraded = await orch._invocation_error_reproduces_on_base(
        repo, None, "main", env_dependent=True)

    assert confident is True          # no setup needed → verdict stands
    assert downgraded is None         # setup-dependent → undeterminable


async def test_env_invocation_error_still_proceeds_without_evidence(
    bare_repo, tmp_path, store
):
    """The pre-existing behaviour holds when the base tree errors the same
    way — a broken conftest the task did not touch is not the coder's fault."""
    (bare_repo / "conftest.py").write_text("import nonexistent_dep_xyz  # noqa\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "break the env on base")

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # advisory as before: the change ships to its human gate, with the
    # invocation error on the record — not silently failed
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None


async def test_node_missing_deps_invocation_error_does_not_fail_attempt(
    bare_repo, tmp_path, store
):
    """SCRUM-35: a node module-resolution failure (the SCRUM-33 "2335 passed,
    1 failed" shape — missing `node_modules` in the worktree) must ride the
    SAME boundary as any other invocation error: it does not consume a
    coder attempt as a code failure, and never reaches the tamper guard —
    max_attempts=1 here, so if this were fed back as a plain test failure the
    attempt would end FAILED instead of reaching the human gate."""
    node_err_cmd = (
        "printf '# tests 1\\n# pass 0\\n# fail 0\\n'; "
        "printf 'Error [ERR_MODULE_NOT_FOUND]: Cannot find package "
        "\"left-pad\"\\n' 1>&2; "
        "exit 1"
    )
    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="node",
        test_cmd=node_err_cmd,
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # reproduces identically on the base tree (a fixed canned command) →
    # classified environmental → proceeds to the human gate, not FAILED.
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 1, "an INFRA failure must not consume a second attempt"
    assert attempts[-1]["status"] != "failed", (
        "a dependency-resolution crash must never be recorded as a failed attempt: "
        + str(attempts[-1])
    )


# ---------------------------------------------------------------------------
# The PLAIN-RED twin of the invocation-error base-check: a plain test failure
# must be re-run on the base tree and only NEWLY-failing ids fail the attempt.
# A test already red on base (flaky / env-dependent / pre-existing) used to be
# blamed on the change, making any such repo structurally unpassable.
# ---------------------------------------------------------------------------


async def test_pre_existing_red_test_does_not_fail_the_attempt(
    bare_repo, tmp_path, store
):
    """A test that ALREADY fails on the base tree is not the change's fault:
    the base recheck sees it red on both trees and does NOT fail the attempt —
    it proceeds to the human gate, with the pre-existing failure surfaced."""
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    # deterministic, env-independent red test — present on the BASE tree
    (bare_repo / "test_preexisting.py").write_text(
        "def test_preexisting():\n    assert False, 'red before the change'\n"
    )
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "pre-existing red test on base")

    def mutate(cwd):
        # a benign change that touches neither failing test nor any test file
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert outcome.pr_url is not None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] != "failed", (
        "a failure that reproduces on the base tree must not be blamed on the "
        "change: " + str(attempts[-1])
    )


async def test_newly_failing_test_still_fails_the_attempt(
    bare_repo, tmp_path, store
):
    """THE GATE IS NOT WEAKENED: a test that PASSES on base but the change
    breaks is a real regression and must still fail the attempt, named as
    newly failing."""
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "pytest marker — base is GREEN")

    def mutate(cwd):
        # break add() so the existing (green-on-base) test_add now fails
        (cwd / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("touch add()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    reason = (attempts[-1]["failure_reason"] or "").lower()
    assert "test_add" in reason, reason
    assert "newly failing" in reason, (
        "a real regression must be named as newly failing vs base: " + reason
    )


async def test_mixed_failures_fail_only_on_the_newly_failing_test(
    bare_repo, tmp_path, store
):
    """Mixed run — one pre-existing red test + one the change newly breaks: the
    attempt FAILS (there is a real regression), and the failure names ONLY the
    newly-failing test, never the pre-existing one."""
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    (bare_repo / "test_preexisting.py").write_text(
        "def test_preexisting():\n    assert False, 'red before the change'\n"
    )
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "pre-existing red test + marker")

    def mutate(cwd):
        # newly break test_add (green on base) while leaving the pre-existing
        # red test exactly as it was
        (cwd / "calc.py").write_text("def add(a, b):\n    return a - b\n")

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("touch add()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    reason = attempts[-1]["failure_reason"] or ""
    assert "test_add" in reason, reason
    assert "newly failing" in reason.lower(), reason
    assert "test_preexisting" not in reason, (
        "the pre-existing failure must NOT be blamed on the change: " + reason
    )


async def test_unparseable_red_run_fails_closed(bare_repo, tmp_path, store):
    """A red run whose output carries counts but NO test node ids to bound a
    base recheck on (empty failing_tests) keeps the current behaviour — the
    attempt FAILS. Never silently pass a red run that cannot be attributed."""
    from no_human.profile import ProjectProfile
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="python",
        # unittest-style summary: real counts, exit 1, but no `path::test` ids
        test_cmd="printf 'Passed: 1\\nFailed: 1\\nErrors: 0\\n'; exit 1",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"


async def test_base_recheck_inconclusive_fails_closed(bare_repo, tmp_path, store):
    """When the bounded base rerun cannot establish a per-id verdict it must
    fail CLOSED (never PASS on an inconclusive base check). A failing test the
    change ADDED does not exist on base, so the bounded rerun errors (nothing
    to collect) → inconclusive → the attempt FAILS (a newly-added failing test
    is the change's fault anyway)."""
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "pytest marker")

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )
        # a NEW test file, absent on base, whose test fails
        (cwd / "test_mul.py").write_text(
            "from calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 7\n"
        )

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
