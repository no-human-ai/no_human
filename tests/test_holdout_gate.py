"""A failing held-out suite is a deterministic FAIL (docs/ARCH_REVIEW.md B2 #8).

tests/held_out/ exists precisely because the implementer never sees it — but
its result used to reach the pipeline only as prompt text the reviewer could
weigh away (and in advisory mode it never even ran). A verifiable signal the
system already computed must gate deterministically: held-out failed → the
attempt fails BEFORE any reviewer tokens are spent.
"""

import shutil

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.selfcheck import ChecklistItem
from no_human.review.reviewer import ReviewDecision

from ._isolated_run import run_node_id_isolated, scrubbed_path
from .test_e2e_orchestrator import (  # noqa: F401
    FakeBackend,
    _config,
    _git,
    bare_repo,
)

# Self-contained sys.path bootstrap: pytest runs tests/held_out/ without the
# repo root importable (no ini in the fixture repo).
_HELD_OUT_TEST = """\
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from calc import mul


def test_mul_hidden():
    assert mul(2, 3) == 6
"""


def _seed_held_out(repo):
    held = repo / "tests" / "held_out"
    held.mkdir(parents=True)
    (held / "test_hidden.py").write_text(_HELD_OUT_TEST)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "held-out suite")


def _cfg(tmp_path):
    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    # The realistic shape: the visible gate's command is SCOPED and does not
    # collect tests/held_out/ (a repo-root `pytest -q` in this tiny fixture
    # would sweep it up and mask the exact hole under test).
    cfg.data["tests"] = {"command": "pytest -q test_calc.py"}
    return cfg


def _wrong_mul(cwd):
    # self-consistent but WRONG: the visible artifacts agree with each other,
    # only the held-out suite knows mul(2,3) must be 6
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b + 1\n"
    )


class _StubReviewer:
    def __init__(self):
        self.calls = []
        self._on_event = None

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                     before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        self.calls.append(task.id)
        return ReviewDecision(
            passed=True,
            checklist=[ChecklistItem("looks right", True, "stub")],
        )


async def test_failing_held_out_fails_before_the_reviewer_runs(
    bare_repo, tmp_path, store
):
    _seed_held_out(bare_repo)
    cfg = _cfg(tmp_path)
    reviewer = _StubReviewer()
    orch = Orchestrator(store, cfg.data, FakeBackend(_wrong_mul),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.pr_url is None
    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert reviewer.calls == [], (
        "held-out is deterministic and must gate BEFORE reviewer tokens are spent"
    )
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    assert "held-out" in (attempts[-1]["failure_reason"] or "").lower()


async def test_advisory_mode_cannot_rubber_stamp_past_held_out(
    bare_repo, tmp_path, store
):
    """allow_advisory skips the LLM reviewer — it must not skip a
    deterministic signal that already exists on disk."""
    _seed_held_out(bare_repo)
    cfg = _cfg(tmp_path)  # allow_advisory=true, no reviewer
    orch = Orchestrator(store, cfg.data, FakeBackend(_wrong_mul), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.pr_url is None
    assert outcome.status is not TaskStatus.AWAITING_APPROVAL


async def test_passing_held_out_does_not_block(
    bare_repo, tmp_path, store, own_pytest_on_path
):
    def correct_mul(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
        )

    _seed_held_out(bare_repo)
    cfg = _cfg(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(correct_mul), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None


def test_passing_holdout_passes_with_no_pytest_on_path(tmp_path):
    """Regression twin: `run_held_out_tests` (runner.py:1158) hardcodes a
    bare `pytest -q <path>` shell command with no venv-relative fallback, so
    the target test above must supply its own resolvable `pytest` rather than
    depending on the ambient PATH of whatever launched this suite — see
    `tests/conftest.py`'s `own_pytest_on_path` docstring for the measured
    root cause.
    """
    path = scrubbed_path()
    assert shutil.which("pytest", path=path) is None, (
        "the precondition of this guard is a pytest-free PATH — a machine "
        f"with a system pytest would make it a tautology: PATH={path!r}"
    )
    node_id = "tests/test_holdout_gate.py::test_passing_held_out_does_not_block"
    assert node_id.rsplit("::", 1)[-1] != (
        "test_passing_holdout_passes_with_no_pytest_on_path"
    ), "must target the fixture test, not re-enter this regression test"
    home = tmp_path / "home"
    home.mkdir()
    proc = run_node_id_isolated(node_id, home)
    tail = (proc.stdout + proc.stderr)[-4000:]
    assert proc.returncode == 0, tail
    assert "1 passed" in proc.stdout, tail
