"""The flaky-on-rerun and pre-existing excuses classify a failing test by
whether the TREE already fails elsewhere (`_flaky_on_rerun`,
`_newly_failing_vs_base` — tests/test_flaky_rerun_attribution.py,
tests/test_base_tree_gate.py). Neither ever asks whether the CURRENT attempt
itself wrote or edited the failing test function. That is a real hole:

* A brand-new test the attempt adds, red on its first run and green on every
  run after, can only reach the flaky excuse today — which is false by
  construction, since it never ran before this attempt and there is no tree
  for it to be "flaky on".
* A test the attempt modifies, that also happens to fail on the base tree
  (perhaps because the base tree's version of it was ALSO broken, or by
  coincidence), is wrongly excused as pre-existing — exactly the change a
  reviewer most needs to see.

`no_human.testing.ownership.owned_failing_ids` answers "does this attempt's
own diff name the failing test function" (added or modified, per-function not
per-file). `_attributed_ids` and the wiring in `orchestrator.py`'s plain-red
branch make an owned id always billed, never excused by either path — see
`.no_human/PLAN.md` for the full spec these three acceptance tests and the
regression twin below are drawn from.
"""

import json as _json
import shutil

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from ._isolated_run import run_node_id_isolated, scrubbed_path
from .test_e2e_orchestrator import (  # noqa: F401
    FakeBackend,
    _config,
    _git,
    bare_repo,
)


def _orch(store, tmp_path, backend, sink=None):
    cfg = _config(tmp_path)
    # One attempt is enough to prove attribution; retries just repeat it slowly.
    cfg.data["bounds"] = {"max_attempts": 1}
    return Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=sink)


def _persisted(attempt):
    tr = attempt["test_results"]
    return _json.loads(tr) if isinstance(tr, str) else (tr or {})


def _flaky_source(func_name: str, marker) -> str:
    """A test that is red on its first run and green on every run after — the
    same deterministic flake stand-in test_flaky_rerun_attribution.py uses,
    parameterized by function name so AC1/AC2 can each pick their own."""
    return (
        "import os\n"
        f"MARK = {str(marker)!r}\n"
        f"def {func_name}():\n"
        "    first = not os.path.exists(MARK)\n"
        "    with open(MARK, 'a') as fh:\n"
        "        fh.write('run\\n')\n"
        "    assert not first, 'red on the first run only'\n"
    )


# ---------------------------------------------------------------------------
# AC1: a test the attempt's OWN diff adds, red-then-green shaped exactly like
# a real flake, must never reach the flaky excuse and must be billed.
# ---------------------------------------------------------------------------


async def test_a_new_flaky_test_the_change_added_is_billed_not_excused(
    bare_repo, tmp_path, store
):
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    (bare_repo / "test_existing.py").write_text(
        "def test_existing():\n    assert True\n"
    )
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "green base, no test_new.py yet")

    marker = tmp_path / "new.marker"

    def mutate(cwd):
        # a brand-new test file this attempt's own diff adds
        (cwd / "test_new.py").write_text(_flaky_source("test_x", marker))

    events = []
    orch = _orch(store, tmp_path, FakeBackend(mutate), sink=events.append)
    t = Task.new("add a new test", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    reason = attempts[-1]["failure_reason"] or ""
    assert "test_new.py::test_x" in reason, reason
    assert "this change's own test" in reason, reason
    persisted = _persisted(attempts[-1])
    assert persisted.get("owned_failures") == ["test_new.py::test_x"], persisted
    assert not persisted.get("flaky_excused"), (
        "a test this attempt's own diff added must never be excused as "
        "flaky — it never ran before this attempt: " + str(persisted)
    )
    notes = [e for e in events
             if e.get("kind") == "tests" and e.get("flaky_excused")]
    assert notes == [], (
        "no event may carry a non-empty flaky_excused for an owned id: "
        + str(notes)
    )


# ---------------------------------------------------------------------------
# AC2: an untouched function in a MODIFIED file must still be excusable — the
# ownership check is per-function, never per-file.
# ---------------------------------------------------------------------------


async def test_an_untouched_function_in_a_modified_file_is_still_excused(
    bare_repo, tmp_path, store, own_pytest_on_path
):
    marker = tmp_path / "sibling.marker"
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    (bare_repo / "test_ab.py").write_text(
        "def test_a():\n    assert True\n\n\n"
        + _flaky_source("test_b", marker)
    )
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "test_a green, test_b a load-dependent flake")

    def mutate(cwd):
        # touch ONLY test_a's own body — test_b's span is untouched
        (cwd / "test_ab.py").write_text(
            "def test_a():\n    assert True  # a harmless comment-free edit\n"
            "    assert 1 + 1 == 2\n\n\n" + _flaky_source("test_b", marker)
        )

    events = []
    orch = _orch(store, tmp_path, FakeBackend(mutate), sink=events.append)
    t = Task.new("touch test_a only", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert outcome.pr_url is not None
    attempts = await store.list_attempts(t.id)
    persisted = _persisted(attempts[-1])
    excused = persisted.get("flaky_excused") or []
    assert any("test_b" in e for e in excused), (
        "an untouched sibling function in a modified FILE must still be "
        "excusable — ownership is per-function, not per-file: " + str(persisted)
    )
    assert "owned_failures" not in persisted, (
        "test_b was never touched by this attempt's diff and must not be "
        "reported as owned: " + str(persisted)
    )


def test_untouched_function_excuse_passes_with_no_pytest_on_path(tmp_path):
    """Regression twin: the target test's flaky re-run path is rescued by the
    rc=127 invocation retry (runner.py:1014), but the substituted command is
    then discarded (orchestrator.py:10702) — so, like its two siblings, it
    must supply its own resolvable `pytest` rather than depending on the
    ambient PATH of whatever launched this suite. See `tests/conftest.py`'s
    `own_pytest_on_path` docstring for the measured root cause.
    """
    path = scrubbed_path()
    assert shutil.which("pytest", path=path) is None, (
        "the precondition of this guard is a pytest-free PATH — a machine "
        f"with a system pytest would make it a tautology: PATH={path!r}"
    )
    node_id = (
        "tests/test_owned_test_attribution.py::"
        "test_an_untouched_function_in_a_modified_file_is_still_excused"
    )
    assert node_id.rsplit("::", 1)[-1] != (
        "test_untouched_function_excuse_passes_with_no_pytest_on_path"
    ), "must target the fixture test, not re-enter this regression test"
    home = tmp_path / "home"
    home.mkdir()
    proc = run_node_id_isolated(node_id, home)
    tail = (proc.stdout + proc.stderr)[-4000:]
    assert proc.returncode == 0, tail
    assert "1 passed" in proc.stdout, tail


# ---------------------------------------------------------------------------
# AC3: a test the attempt MODIFIES that also fails on the base tree is not
# excused as pre-existing — the attempt's own edit is exactly what a reviewer
# needs to see, and a genuinely untouched sibling stays out of the bill.
# ---------------------------------------------------------------------------


async def test_a_modified_test_that_also_fails_on_base_is_not_excused_as_pre_existing(
    bare_repo, tmp_path, store
):
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    (bare_repo / "test_ac.py").write_text(
        "def test_a():\n    assert False, 'red before the change'\n\n\n"
        "def test_c():\n    assert False, 'also red before the change'\n"
    )
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "test_a and test_c both red on base")

    def mutate(cwd):
        # edit test_a's body (still red) — test_c is left byte-for-byte alone
        (cwd / "test_ac.py").write_text(
            "def test_a():\n    assert False, 'still red after the edit'\n\n\n"
            "def test_c():\n    assert False, 'also red before the change'\n"
        )

    orch = _orch(store, tmp_path, FakeBackend(mutate))
    t = Task.new("touch test_a only", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    persisted = _persisted(attempts[-1])
    assert persisted.get("owned_failures") == ["test_ac.py::test_a"], persisted
    assert "pre_existing_failures" not in persisted, (
        "a test this attempt's own diff modified must never be excused as "
        "pre-existing, even though it also fails on base: " + str(persisted)
    )
    assert "test_ac.py::test_c" not in (persisted.get("owned_failures") or []), (
        "a genuinely untouched sibling that is ALSO red on base must not be "
        "reported as owned by this attempt: " + str(persisted)
    )


# ---------------------------------------------------------------------------
# Regression twin: an untouched pre-existing red test, unrelated to anything
# ownership resolves, must still be excused exactly as before this change.
# ---------------------------------------------------------------------------


async def test_an_untouched_pre_existing_red_test_is_still_excused(
    bare_repo, tmp_path, store
):
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    (bare_repo / "test_preexisting.py").write_text(
        "def test_preexisting():\n    assert False, 'red before the change'\n"
    )
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "pre-existing red test on base")

    def mutate(cwd):
        # a benign change that touches neither the failing test nor any test file
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
        "a failure that reproduces on the base tree, untouched by this "
        "attempt's diff, must not be blamed on the change: " + str(attempts[-1])
    )
    persisted = _persisted(attempts[-1])
    assert persisted.get("pre_existing_failures") == [
        "test_preexisting.py::test_preexisting"
    ], persisted
    assert "owned_failures" not in persisted, persisted


# ---------------------------------------------------------------------------
# Wiring: the routed test cwd reaches the ownership lookup (review finding on
# PR #570 — without it the guard is silently inert for any project whose
# test-routing rule runs pytest from a subdirectory). Semantics of the prefix
# itself are pinned in test_ownership_unit.py; this pins the call.
# ---------------------------------------------------------------------------


async def test_the_routed_test_cwd_reaches_the_ownership_lookup(
        store, tmp_path, monkeypatch):
    from no_human.core import orchestrator as orch_mod
    from no_human.vcs import GitRepo

    root = tmp_path / "routed"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "u@e.com")
    _git(root, "config", "user.name", "u")
    (root / "pkg" / "x.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    (root / "pkg" / "x.py").write_text("x = 2\n")
    _git(root, "commit", "-qam", "change")
    repo = GitRepo(root, identity_name="agent", identity_email="a@x.y", never_push_to=[])

    seen: list[dict] = []

    def spy(repo_path, before_ref, after_ref, node_ids, *, cwd=None, timeout=0):
        seen.append({"cwd": cwd, "ids": list(node_ids)})
        return ["tests/test_a.py::test_a"]

    monkeypatch.setattr(orch_mod.ownership, "owned_failing_ids", spy)
    orch = _orch(store, tmp_path, FakeBackend(lambda *_a, **_k: None))
    ids = ["tests/test_a.py::test_a"]

    # Routed into pkg/: the lookup is told the repo-relative cwd.
    assert await orch._owned_failing_tests(repo, None, ids, cwd=str(root / "pkg")) == ids
    assert seen[-1]["cwd"] == "pkg"
    # Run from the repo root: an empty prefix, not None-by-accident.
    assert await orch._owned_failing_tests(repo, None, ids, cwd=str(root)) == ids
    assert seen[-1]["cwd"] == ""
    # No cwd at all: today's behaviour.
    await orch._owned_failing_tests(repo, None, ids)
    assert seen[-1]["cwd"] is None
    # A cwd OUTSIDE the repo cannot own anything in this diff: nothing is
    # looked up, nothing is owned.
    calls_before = len(seen)
    assert await orch._owned_failing_tests(repo, None, ids, cwd=str(tmp_path)) == []
    assert len(seen) == calls_before
