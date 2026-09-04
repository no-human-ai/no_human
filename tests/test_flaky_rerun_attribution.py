"""A red test is only excused when it goes green ALONE *and* at suite scope.

The base-tree check (`_newly_failing_vs_base`, tests/test_base_tree_gate.py) is
the wrong instrument for a LOAD-DEPENDENT flake: it decides attribution from ONE
run of the base tree, so for a test that fails only under heavy parallel load
that run is a coin flip. Base happens to pass → the id is "newly failing" and
the attempt is BILLED for a failure the coder did not cause (17 real tasks lost
attempt 1 of max_attempts=3 this way); base happens to fail → the same id, on
the same task, is excused. `_flaky_on_rerun` re-runs on the CHANGE tree instead,
in two stages, and the second stage is the point:

  1. the attributed ids ALONE — may only refuse, never excuse;
  2. the whole original command again — the scope the failure was observed at.

Stage 2 exists because "red in the suite, green on its own" is ALSO the shape of
suite-only pollution the change introduced (import-time env mutation in a new
module). Excusing on stage 1 alone would hand every such change a free pass.

The flake stand-in here is deterministic on purpose: red on its FIRST run, green
on every run after (a marker file OUTSIDE the repo, so it neither dirties the
working tree nor rides into a worktree). That reproduces exactly the shape that
matters — red on the change run, green alone, green at suite scope — without
depending on machine load.
"""

import json as _json
import shutil
import subprocess
import sys

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.testing import runner
from no_human.vcs.git import GitRepo

from ._isolated_run import run_node_id_isolated, scrubbed_path
from .test_e2e_orchestrator import (  # noqa: F401
    FakeBackend,
    _config,
    _git,
    bare_repo,
)

PYTEST = f"{sys.executable} -m pytest -q"


def _orch(store, tmp_path, backend, sink=None):
    cfg = _config(tmp_path)
    # One attempt is enough to prove attribution; retries just repeat it slowly.
    cfg.data["bounds"] = {"max_attempts": 1}
    return Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=sink)


def _flaky_source(marker) -> str:
    """A test that is red on its first run and green on every run after.

    The marker lives outside the repo so the working tree stays clean (a dirty
    tree suppresses the reviewer-run reuse and would change what runs when).
    """
    return (
        "import os\n"
        f"MARK = {str(marker)!r}\n"
        "def test_flaky():\n"
        "    first = not os.path.exists(MARK)\n"
        "    with open(MARK, 'a') as fh:\n"
        "        fh.write('run\\n')\n"
        "    assert not first, 'red on the first run only'\n"
    )


def _persisted(attempt):
    tr = attempt["test_results"]
    return _json.loads(tr) if isinstance(tr, str) else (tr or {})


def _seed_flaky_repo(bare_repo, marker):
    (bare_repo / "pytest.ini").write_text("[pytest]\n")
    (bare_repo / "test_flaky.py").write_text(_flaky_source(marker))
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "a load-dependent flake, present on base too")


def _add_mul(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )


def _probe_repo(tmp_path, name, files) -> "GitRepo":
    """A throwaway git repo holding *files* — the helper's unit fixture."""
    root = tmp_path / name
    root.mkdir()
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    return GitRepo(str(root))


# ---------------------------------------------------------------------------
# (a) every attributed id passes both stages → the attempt is NOT billed
# ---------------------------------------------------------------------------


async def test_flaky_test_green_on_rerun_does_not_fail_the_attempt(
    bare_repo, tmp_path, store, own_pytest_on_path
):
    _seed_flaky_repo(bare_repo, tmp_path / "flaky.marker")
    events = []
    orch = _orch(store, tmp_path, FakeBackend(_add_mul), sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert outcome.pr_url is not None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] != "failed", (
        "a test that passes alone AND at suite scope on the same tree is flaky, "
        "not this change's regression: " + str(attempts[-1])
    )
    excused = _persisted(attempts[-1]).get("flaky_excused") or []
    assert any("test_flaky" in e for e in excused), (
        "the excused id must be ON THE RECORD for the reviewer, never a silent "
        "pass: " + str(_persisted(attempts[-1]))
    )
    # the note itself, not the raw FAIL tail (whose traceback also says "flaky")
    notes = [e for e in events
             if e.get("kind") == "tests" and e.get("flaky_excused")]
    assert notes and all(e.get("ok") is True for e in notes), (
        "the excuse must be emitted as an ok=True event: "
        + str([e.get("text") for e in events if e.get("kind") == "tests"])
    )
    assert any("re-run" in (e.get("text") or "") for e in notes), (
        "the event must SAY it re-ran — a bare ok=True is a silent pass: "
        + str([e.get("text") for e in notes])
    )


def test_flaky_green_on_rerun_passes_with_no_pytest_on_path(tmp_path):
    """Regression twin for the fixture above: the target test must be
    self-contained, not merely lucky about what launched THIS suite.

    See `tests/conftest.py`'s `own_pytest_on_path` docstring for the measured
    root cause. Runs the target id ALONE, in a subprocess, under a fresh HOME
    with no `pytest` resolvable on PATH; the subprocess acquires its own
    `own_pytest_on_path` fixture from this same conftest, same as any other
    test.
    """
    path = scrubbed_path()
    assert shutil.which("pytest", path=path) is None, (
        "the precondition of this guard is a pytest-free PATH — a machine "
        f"with a system pytest would make it a tautology: PATH={path!r}"
    )
    node_id = (
        "tests/test_flaky_rerun_attribution.py::"
        "test_flaky_test_green_on_rerun_does_not_fail_the_attempt"
    )
    assert node_id.rsplit("::", 1)[-1] != (
        "test_flaky_green_on_rerun_passes_with_no_pytest_on_path"
    ), "must target the fixture test, not re-enter this regression test"
    home = tmp_path / "home"
    home.mkdir()
    proc = run_node_id_isolated(node_id, home)
    tail = (proc.stdout + proc.stderr)[-4000:]
    assert proc.returncode == 0, tail
    assert "1 passed" in proc.stdout, tail


# ---------------------------------------------------------------------------
# (b) one id red again → NOTHING is excused (bounded evidence alone never
#     excuses, per the A-2 amendment: it cannot tell a flake from pollution)
# ---------------------------------------------------------------------------


async def test_a_mixed_run_excuses_nothing_and_bills_every_attributed_id(
    bare_repo, tmp_path, store
):
    """A real regression alongside a flake: the attempt fails, and BOTH ids are
    billed. The flake could only be cleared by the two-stage proof, and stage 2
    never runs once an attributed id is red again — so there is no evidence to
    excuse it with, and fail-closed means it stays on the bill."""
    _seed_flaky_repo(bare_repo, tmp_path / "flaky.marker")

    def mutate(cwd):
        # test_add is green on base and fails EVERY run — deterministic
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
    assert "flaky_excused" not in _persisted(attempts[-1]), (
        "nothing may be excused off a bounded run that never reached stage 2: "
        + str(_persisted(attempts[-1]))
    )


# ---------------------------------------------------------------------------
# (c) an inconclusive re-run must change NOTHING — fail closed
# ---------------------------------------------------------------------------


async def test_inconclusive_rerun_fails_the_attempt_exactly_as_before(
    bare_repo, tmp_path, store, monkeypatch
):
    """The re-run is evidence, not an excuse generator: when it cannot produce
    a per-id verdict the attempt fails exactly as it did before this path
    existed."""
    _seed_flaky_repo(bare_repo, tmp_path / "flaky.marker")

    async def _inconclusive(self, *a, **kw):
        return None

    monkeypatch.setattr(Orchestrator, "_flaky_on_rerun", _inconclusive)

    orch = _orch(store, tmp_path, FakeBackend(_add_mul))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    assert "test_flaky" in (attempts[-1]["failure_reason"] or "")
    assert "flaky_excused" not in _persisted(attempts[-1])


def _stub(**overrides):
    """A `run_tests` stand-in that ECHOES the command it was asked to run —
    the runner's own contract, and what the substitution guard reads. Reports
    no test identities unless a case supplies them, which is itself the
    fail-closed default: a run that names nothing clears nothing."""
    def run_tests(repo_path, command=None, **kw):
        fields = dict(ran=True, ok=False, passed=0, failed=1, errors=0,
                      command=command, output="", failing_tests=[],
                      passed_tests=[])
        fields.update(overrides)
        return runner.TestRunResult(**fields)
    return run_tests


async def test_rerun_that_did_not_run_or_errored_is_inconclusive(
    bare_repo, tmp_path, store, monkeypatch
):
    orch = _orch(store, tmp_path, FakeBackend(lambda cwd: None))
    repo = GitRepo(str(bare_repo))
    ids = ["test_calc.py::test_add"]

    monkeypatch.setattr(runner, "run_tests", _stub(ran=False, ok=True))
    assert await orch._flaky_on_rerun(repo, "pytest", ids) is None

    monkeypatch.setattr(runner, "run_tests",
                        _stub(invocation_error=True, failing_tests=list(ids)))
    assert await orch._flaky_on_rerun(repo, "pytest", ids) is None

    # red on the re-run but with NO parseable ids: cannot attribute → closed
    monkeypatch.setattr(runner, "run_tests", _stub(failing_tests=[]))
    assert await orch._flaky_on_rerun(repo, "pytest", ids) is None

    def _boom(*a, **kw):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(runner, "run_tests", _boom)
    assert await orch._flaky_on_rerun(repo, "pytest", ids) is None


async def test_a_substituted_command_is_not_the_verdict_we_asked_for(
    bare_repo, tmp_path, store, monkeypatch
):
    """`runner._fix_invocation` answers "unrecognized arguments" by DROPPING the
    node ids and re-running the whole repo. That result is green-or-red about a
    different question, so a bounded stage that silently became a full-suite run
    must not be read as a bounded verdict. The runner reports what actually ran
    in `TestRunResult.command` — that is the observable this guard uses."""
    orch = _orch(store, tmp_path, FakeBackend(lambda cwd: None))
    repo = GitRepo(str(bare_repo))

    def substituted(repo_path, command=None, **kw):
        return runner.TestRunResult(
            ran=True, ok=True, passed=9, failed=0, errors=0,
            command=f'pytest -q --override-ini="addopts=" {repo_path}',
            output="", failing_tests=[],
        )

    monkeypatch.setattr(runner, "run_tests", substituted)
    assert await orch._flaky_on_rerun(repo, "pytest", ["t.py::a"]) is None


async def test_an_id_red_again_is_never_excused(bare_repo, tmp_path, store,
                                                monkeypatch):
    orch = _orch(store, tmp_path, FakeBackend(lambda cwd: None))
    repo = GitRepo(str(bare_repo))
    ids = ["t.py::a", "t.py::b"]

    # every id accounted for BY NAME, so the only reason to refuse is that b
    # failed again — not a gap in the reporting
    monkeypatch.setattr(runner, "run_tests",
                        _stub(passed=1, failed=1, passed_tests=["t.py::a"],
                              failing_tests=["t.py::b"]))
    assert await orch._flaky_on_rerun(repo, "pytest", ids) is None

    monkeypatch.setattr(runner, "run_tests",
                        _stub(ok=True, passed=2, failed=0, failing_tests=[],
                              passed_tests=list(ids)))
    assert await orch._flaky_on_rerun(repo, "pytest", ids) == ids


# ---------------------------------------------------------------------------
# C-1: the re-run must test the WORKTREE's code, not the primary checkout's
# ---------------------------------------------------------------------------


async def test_the_rerun_tests_the_worktree_not_the_primary_checkout(
    tmp_path, store, monkeypatch
):
    """The shared venv's editable install resolves the package to the PRIMARY
    checkout (SCRUM-18), so a re-run without `source_repo` imports main's code
    and answers about the wrong tree — excusing a genuinely broken change for
    path reasons. `_run_tests_once` passes `source_repo`; so must this."""
    primary = tmp_path / "primary"
    (primary / "src" / "nhprobe").mkdir(parents=True)
    (primary / "src" / "nhprobe" / "__init__.py").write_text("VALUE = 3\n")
    (primary / "pytest.ini").write_text("[pytest]\n")
    (primary / "test_value.py").write_text(
        "from nhprobe import VALUE\n\n\ndef test_value():\n    assert VALUE == 4\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    # Hermetic identity: the landing gate runs under a scratch $HOME with no
    # global gitconfig, and a fixture that inherits ambient identity fails
    # there with "Author identity unknown" — the very environment-coupling
    # class this test file exists to police.
    _git(primary, "config", "user.email", "t@example.invalid")
    _git(primary, "config", "user.name", "t")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-qm", "primary: VALUE = 3")

    wt = tmp_path / "wt"
    _git(primary, "worktree", "add", "-q", "--detach", str(wt))
    # the CHANGE lives only in the worktree
    (wt / "src" / "nhprobe" / "__init__.py").write_text("VALUE = 4\n")

    # the editable install: the package resolves to the PRIMARY checkout
    monkeypatch.setenv("PYTHONPATH", str(primary / "src"))

    orch = _orch(store, tmp_path, FakeBackend(lambda cwd: None))
    excused = await orch._flaky_on_rerun(
        GitRepo(str(wt)), PYTEST, ["test_value.py::test_value"])

    assert excused == ["test_value.py::test_value"], (
        "the re-run imported VALUE=3 from the primary checkout instead of the "
        "worktree's VALUE=4 — it is answering about the wrong tree"
    )


# ---------------------------------------------------------------------------
# A-1: an id that never RAN is not an id that passed
# ---------------------------------------------------------------------------


async def test_early_exit_in_the_command_leaves_ids_unaccounted(
    tmp_path, store
):
    """`-x` in the project's own test command stops the run at the first
    failure. Ids after it never execute — counting them as passed manufactures
    an excuse out of a run that never asked the question."""
    repo = _probe_repo(tmp_path, "earlyexit", {
        "pytest.ini": "[pytest]\n",
        "test_zz.py": "def test_zz():\n    assert False\n",
        "test_aa.py": "def test_aa():\n    assert True\n",
    })
    # the project's command already names a path AND stops on first failure
    cmd = f"{PYTEST} -x test_zz.py"

    assert await orch_helper(store, tmp_path, repo, cmd,
                             ["test_aa.py::test_aa"]) is None


async def test_addopts_early_exit_leaves_ids_unaccounted(tmp_path, store):
    """Same truncation, but from the repo's OWN pytest.ini — nothing about the
    command we build reveals it, which is why the guard is accounting and not
    flag parsing. The test that trips `-x` is NOT one of the attributed ids, so
    accounting is the only thing standing between this and a false excuse."""
    repo = _probe_repo(tmp_path, "addoptsx", {
        "pytest.ini": "[pytest]\naddopts = -x test_zz.py\n",
        "test_zz.py": "def test_zz():\n    assert False\n",
        "test_aa.py": "def test_aa():\n    assert True\n",
    })

    assert await orch_helper(store, tmp_path, repo, PYTEST,
                             ["test_aa.py::test_aa"]) is None


async def test_addopts_deselection_leaves_ids_unaccounted(tmp_path, store):
    """`-k`/`-m` in `addopts` DESELECT: the run is green, the counts are real,
    and an id we asked about simply never ran."""
    repo = _probe_repo(tmp_path, "addoptsk", {
        "pytest.ini": '[pytest]\naddopts = -k "not skipme"\n',
        "test_keep.py": "def test_keep():\n    assert True\n",
        "test_skipme.py": "def test_skipme():\n    assert True\n",
    })
    ids = ["test_keep.py::test_keep", "test_skipme.py::test_skipme"]

    assert await orch_helper(store, tmp_path, repo, PYTEST, ids) is None


def _gate_source(marker) -> str:
    """The inverse of `_flaky_source`: GREEN on its first run, red after — a
    test that starts failing between the attempt's suite run and the re-runs,
    so it truncates a `-x` run before the attributed id is ever reached."""
    return (
        "import os\n"
        f"MARK = {str(marker)!r}\n"
        "def test_gate():\n"
        "    first = not os.path.exists(MARK)\n"
        "    with open(MARK, 'a') as fh:\n"
        "        fh.write('run\\n')\n"
        "    assert first, 'green on the first run only'\n"
    )


async def test_unrelated_passes_cannot_account_for_the_attributed_id(
    tmp_path, store
):
    """A COUNT cannot say which test ran. The project's command names a PATH
    (`pytest -q tests/`, the documented shape of an integration_test_cmd), its
    own pytest.ini stops at the first failure, and a green test earlier in the
    run supplies a `passed` of 1. Both stages then short-circuit before the
    attributed id executes at all — and an ALWAYS-RED test gets excused as
    flaky off another test's pass. Only naming the ids closes this."""
    repo = _probe_repo(tmp_path, "byname", {
        "pytest.ini": "[pytest]\naddopts = -x\n",
        "tests/test_a_green.py": "def test_a_green():\n    assert True\n",
        "tests/test_b_gate.py": _gate_source(tmp_path / "gate.marker"),
        "tests/test_c_red.py": "def test_c_red():\n    assert False\n",
    })
    cmd = f"{PYTEST} tests/"
    # the attempt's own suite run: the gate is still green here
    runner.run_tests(repo.path, cmd)

    assert await orch_helper(store, tmp_path, repo, cmd,
                             ["tests/test_c_red.py::test_c_red"]) is None, (
        "a test that fails every single time was excused because an unrelated "
        "test passed and the count added up"
    )


async def test_a_test_cannot_forge_its_way_out_by_printing_a_summary_line(
    tmp_path, store
):
    """The identity evidence must come from pytest, not from the code under
    test. `-rA` asks pytest to print the captured stdout of PASSING tests, so a
    green test that prints `PASSED <nodeid>` gets that line echoed into the same
    output the guard parses — an unauthenticated channel the change itself
    controls, minting a pass for an id that never ran. Same attack5 shape,
    plus one print: the always-red test must still be BILLED."""
    repo = _probe_repo(tmp_path, "forgery", {
        "pytest.ini": "[pytest]\naddopts = -x\n",
        "tests/test_a_green.py": (
            "def test_a_green():\n"
            "    print('PASSED tests/test_c_red.py::test_c_red')\n"
            "    assert True\n"
        ),
        "tests/test_b_gate.py": _gate_source(tmp_path / "gate.marker"),
        "tests/test_c_red.py": "def test_c_red():\n    assert False\n",
    })
    cmd = f"{PYTEST} tests/"
    runner.run_tests(repo.path, cmd)  # the attempt's own run; gate still green

    assert await orch_helper(store, tmp_path, repo, cmd,
                             ["tests/test_c_red.py::test_c_red"]) is None, (
        "a test printed the summary line for a test that never ran, and it was "
        "believed — evidence the change under test can author is not evidence"
    )


def test_printed_summary_lines_never_become_test_identities(tmp_path):
    """The parser guard underneath the test above, stated directly: only
    pytest's own short-summary section names tests. A line a test PRINTS —
    echoed back verbatim under `Captured stdout call` when `-rA` is on — must
    mint neither a pass nor a failure."""
    repo = _probe_repo(tmp_path, "parserforgery", {
        "test_g.py": (
            "def test_green():\n"
            "    print('PASSED test_ghost.py::test_ghost')\n"
            "    print('FAILED test_ghost.py::test_boo - boom')\n"
            "    assert True\n"
        ),
    })
    result = runner.run_tests(repo.path, f"{PYTEST} -rA")

    assert result.ok is True and result.failed == 0, result.output
    assert result.failing_tests == [], (
        "a printed FAILED line minted a phantom failure: " + str(result.failing_tests)
    )
    assert result.passed_tests == ["test_g.py::test_green"], (
        "only pytest's own summary may name a passing test: "
        + str(result.passed_tests)
    )


async def test_another_runners_counts_cannot_clear_a_pytest_id(tmp_path, store):
    """This repo's own web/desktop route is compound — `node --test … && uv run
    pytest …` — and `_parse_test_output` reads node's TAP tally as `passed`.
    Nothing pytest-shaped ran, so nothing can be cleared. Falling out of the
    identity check needs no compound-command special case."""
    repo = _probe_repo(tmp_path, "compound", {
        "pytest.ini": "[pytest]\n",
        "test_x.py": "def test_x():\n    assert True\n",
    })
    # the node half's TAP summary, and a trailing `# … pytest …` standing in for
    # the half that never runs — where the appended ids land after a `&&`
    cmd = "printf '# tests 5\\n# pass 5\\n# fail 0\\n' # uv run pytest -q"

    assert await orch_helper(store, tmp_path, repo, cmd,
                             ["test_x.py::test_x"]) is None


# ---------------------------------------------------------------------------
# A-2: bounded-green is not flakiness — the suite scope decides
# ---------------------------------------------------------------------------


async def test_suite_only_pollution_the_change_introduced_is_billed(
    tmp_path, store
):
    """The shape stage 2 exists for: a module the change ADDED mutates the
    environment at import time, so a test that is green on its own is
    deterministically red whenever the suite runs. "Green alone" would excuse
    it; the suite re-run bills it."""
    repo = _probe_repo(tmp_path, "pollution", {
        "pytest.ini": "[pytest]\n",
        # sorts first, so the mutation lands before test_env runs either way
        "test_a_pollute.py": (
            "import os\n\nos.environ['NH_POLLUTE'] = '1'\n\n\n"
            "def test_pollute():\n    assert True\n"
        ),
        "test_env.py": (
            "import os\n\n\ndef test_env():\n"
            "    assert 'NH_POLLUTE' not in os.environ\n"
        ),
    })

    assert await orch_helper(store, tmp_path, repo, PYTEST,
                             ["test_env.py::test_env"]) is None, (
        "deterministic suite-scope pollution is the change's fault — a bounded "
        "green run must not excuse it"
    )


async def test_a_real_flake_survives_both_stages_and_is_excused(
    tmp_path, store
):
    """The counterpart: red once, then green alone AND green at suite scope.
    Stage 2 must not turn every re-run into a refusal."""
    marker = tmp_path / "unit.marker"
    repo = _probe_repo(tmp_path, "realflake", {
        "pytest.ini": "[pytest]\n",
        "test_flaky.py": _flaky_source(marker),
    })
    # consume the first (red) run, standing in for the attempt's own suite run
    runner.run_tests(repo.path, PYTEST)

    assert await orch_helper(store, tmp_path, repo, PYTEST,
                             ["test_flaky.py::test_flaky"]) == [
        "test_flaky.py::test_flaky"]


# ---------------------------------------------------------------------------
# (d) only pytest-shaped commands can be bounded by node id — nothing else runs
# ---------------------------------------------------------------------------


async def test_non_pytest_command_never_reruns(
    bare_repo, tmp_path, store, monkeypatch
):
    orch = _orch(store, tmp_path, FakeBackend(lambda cwd: None))
    repo = GitRepo(str(bare_repo))

    def _never(*a, **kw):
        raise AssertionError("a non-pytest command must not be re-run at all")

    monkeypatch.setattr(runner, "run_tests", _never)

    assert await orch._flaky_on_rerun(repo, "npm test", ["t.py::a"]) is None
    # no ids to bound on → nothing to re-run, and nothing to excuse
    assert await orch._flaky_on_rerun(repo, "pytest", []) is None


async def orch_helper(store, tmp_path, repo, cmd, ids):
    """Call the helper on a probe repo with a real (sub-process) test run."""
    orch = _orch(store, tmp_path, FakeBackend(lambda cwd: None))
    return await orch._flaky_on_rerun(repo, cmd, ids)
